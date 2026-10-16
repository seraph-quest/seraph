"""Passive Python providers for the canonical runtime domains (#1024, A3.1).

Python always hosts ``state``, ``authority``, ``audit`` and ``tasks``: they are
activated first, in that order, and a failure of any of them aborts startup.
``goals``, ``memory`` and ``artifacts`` follow and may only degrade; their
Cordis service methods then return a typed block. The Cordis plugins of the
same names report readiness and own no activation (operator decision on
#1024, 2026-10-16), because the database must be open before the ownership
table can be read or the Node child started.

Importing this module creates nothing: no session, connection, task, thread,
scheduler job or file. A provider reuses the existing owner (``init_db`` /
``close_db``, ``runtime_workspace_owner``, the auth, audit and durable job
repositories); there is no second store.

The ``state`` provider owns the ``get_session`` gate (``db.engine``): open
while activating and active, closed (typed ``StateProviderUnavailable``) once
activation failed or shutdown began, and detached again at the end of the
worker's life.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
import stat

from src.db.engine import close_db, init_db

logger = logging.getLogger(__name__)

MANDATORY = ("state", "authority", "audit", "tasks")
DEGRADABLE = ("goals", "memory", "artifacts")
PROVIDERS = (*MANDATORY, *DEGRADABLE)
# Fixed activation dependencies; a provider never activates before these.
DEPENDENCIES = {
    "state": (), "authority": ("state",), "audit": ("state",), "tasks": ("state", "authority"),
    "goals": ("state", "authority"), "artifacts": ("state", "authority"),
    "memory": ("state", "authority", "audit"),
}
# Runtime domain served by each provider, for typed Cordis method blocks.
DOMAIN_PROVIDERS = {f"seraph.{name}.v1": name for name in PROVIDERS}
_AUTHORITY_TABLES = ("operator_sessions", "sessions")
_TASK_TABLES = ("workflow_run_states", "work_board_tasks", "work_board_attempts")
_GOAL_TABLES = ("goals",)
_MEMORY_TABLES = ("memories", "memory_tombstones", "memory_proposals")


class ProviderActivationError(RuntimeError):
    def __init__(self, provider: str, reason_code: str):
        super().__init__(f"{provider}: {reason_code}")
        self.provider = provider
        self.reason_code = reason_code


@dataclass
class ProviderStatus:
    name: str
    state: str = "inactive"  # inactive | active | degraded | failed | deactivated
    reason_code: str | None = None

    def payload(self) -> dict[str, str | None]:
        return {"provider": self.name, "state": self.state, "reason_code": self.reason_code}


async def _missing_tables(provider: str, names) -> list[str]:
    """Table presence for one provider; a storage failure is that provider's typed reason."""
    from sqlalchemy import text
    from src.db import engine as db_engine
    try:
        async with db_engine.get_session() as db:
            rows = (await db.execute(text(
                "SELECT name FROM sqlite_master WHERE type = 'table'"))).scalars().all()
    except Exception as exc:  # noqa: BLE001 - startup reports one typed reason
        raise ProviderActivationError(provider, f"{provider}_storage_unavailable") from exc
    present = set(rows)
    return [name for name in names if name not in present]


def _pbkdf2_shape_valid(value: str) -> bool:
    """The exact encoded shape ``src.auth.service`` verifies (as the preflight checks)."""
    import base64
    import binascii
    parts = value.split("$")
    if len(parts) != 4 or parts[0] != "pbkdf2_sha256" or parts[1] != "600000":
        return False
    try:
        salt = base64.b64decode(parts[2].encode("ascii"), altchars=b"-_", validate=True)
        digest = base64.b64decode(parts[3].encode("ascii"), altchars=b"-_", validate=True)
    except (UnicodeEncodeError, ValueError, binascii.Error):
        return False
    return len(salt) == 16 and len(digest) == 32


def _vault_key_resolves() -> str | None:
    """Configured key, or a readable 0600 key file; absent is generated on first use."""
    from cryptography.fernet import Fernet
    from config.settings import settings
    configured = settings.vault_encryption_key
    try:
        if configured:
            Fernet(configured.encode() if isinstance(configured, str) else configured)
            return None
        from src.workspace import canonical_workspace_root
        path = canonical_workspace_root(settings.workspace_dir) / ".vault-key"
        if not os.path.lexists(path):
            return None
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            return "authority_vault_key_unsafe"
        Fernet(Path(path).read_bytes().strip())
        return None
    except (OSError, ValueError, TypeError):
        return "authority_vault_key_unavailable"


async def _state_predicate(*, run_init: bool) -> None:
    from sqlalchemy import text
    from src.db import engine as db_engine
    from .ownership import CompositionBindingError, inventory
    if run_init:
        try:
            await init_db()
        except Exception as exc:  # noqa: BLE001 - startup reports one typed reason
            raise ProviderActivationError("state", "state_init_db_failed") from exc
    try:
        async with db_engine.get_session() as db:
            await db.execute(text("SELECT 1"))
            await inventory(db)
    except CompositionBindingError as exc:
        raise ProviderActivationError("state", "composition_inventory_invalid") from exc
    except Exception as exc:  # noqa: BLE001
        raise ProviderActivationError("state", "state_read_unavailable") from exc


async def _authority_predicate() -> None:
    from config.settings import settings
    from src.auth.service import validate_auth_configuration
    try:
        validate_auth_configuration()
    except RuntimeError as exc:
        raise ProviderActivationError("authority", "authority_configuration_invalid") from exc
    if (settings.deployment_environment.strip().lower() in {"prod", "production"}
            and settings.operator_auth_secret_hash and not _pbkdf2_shape_valid(settings.operator_auth_secret_hash)):
        raise ProviderActivationError("authority", "authority_secret_hash_invalid")
    if await _missing_tables("authority", _AUTHORITY_TABLES):
        raise ProviderActivationError("authority", "authority_tables_missing")
    reason = _vault_key_resolves()
    if reason is not None:
        raise ProviderActivationError("authority", reason)


async def _audit_predicate() -> None:
    from src.db import engine as db_engine
    from src.db.models import AuditEvent
    try:
        async with db_engine.get_session() as db:
            # Probe write inside a savepoint that is always rolled back.
            savepoint = await db.begin_nested()
            db.add(AuditEvent(session_id=None, actor="runtime", event_type="runtime_audit_ready",
                              summary="Audit provider readiness probe", details_json="{}"))
            await db.flush()
            await savepoint.rollback()
            await db.rollback()
    except Exception as exc:  # noqa: BLE001
        raise ProviderActivationError("audit", "audit_write_unavailable") from exc


async def _tasks_predicate() -> None:
    from sqlalchemy import select
    from src.db.models import WorkflowRunState
    if await _missing_tables("tasks", _TASK_TABLES):
        raise ProviderActivationError("tasks", "tasks_tables_missing")
    try:
        from src.workflows.job_runtime import durable_job_repository
        async with durable_job_repository._session() as db:
            await db.execute(select(WorkflowRunState.run_identity).limit(1))
    except Exception as exc:  # noqa: BLE001
        raise ProviderActivationError("tasks", "tasks_repository_unavailable") from exc


async def _goals_predicate() -> None:
    if await _missing_tables("goals", _GOAL_TABLES):
        raise ProviderActivationError("goals", "goals_tables_missing")


async def _memory_predicate() -> None:
    if await _missing_tables("memory", _MEMORY_TABLES):
        raise ProviderActivationError("memory", "memory_tables_missing")


async def _artifacts_predicate() -> None:
    from config.settings import settings
    from src.workspace import canonical_workspace_registry
    try:
        canonical_workspace_registry(settings.workspace_dir).classify_path("artifacts/runtime-service-read/probe")
    except Exception as exc:  # noqa: BLE001
        raise ProviderActivationError("artifacts", "artifacts_root_unavailable") from exc


_PREDICATES = {
    "authority": _authority_predicate, "audit": _audit_predicate, "tasks": _tasks_predicate,
    "goals": _goals_predicate, "memory": _memory_predicate, "artifacts": _artifacts_predicate,
}


class ProviderWorker:
    """One host's passive providers; construction has no side effects."""

    def __init__(self, *, hold_workspace_lock: bool = False, workspace_dir: str | None = None):
        self.hold_workspace_lock = hold_workspace_lock
        self.workspace_dir = workspace_dir
        self.status = {name: ProviderStatus(name) for name in PROVIDERS}
        self._owner = None
        self._activated = False
        self._closed = False

    # ---------------------------------------------------------------- lock

    def acquire_workspace(self) -> None:
        """Take the single runtime workspace owner lease (production only).

        Idempotent in one process; a second process fails closed with
        ``DuplicateWorkspaceOwnerError`` (``LOCK_NB``). The lease is held from
        here until ``release_workspace`` at final shutdown, with no gap.
        """
        if not self.hold_workspace_lock or self._owner is not None:
            return
        from config.settings import settings
        from src.workspace.production import runtime_workspace_owner
        owner = runtime_workspace_owner(self.workspace_dir or settings.workspace_dir)
        owner.__enter__()
        self._owner = owner

    def release_workspace(self) -> None:
        owner, self._owner = self._owner, None
        if owner is not None:
            owner.__exit__(None, None, None)

    # ---------------------------------------------------------------- activation

    async def activate(self) -> None:
        """State, authority, audit and tasks or abort; then degradable providers."""
        from src.db.engine import set_state_gate
        if self._activated:
            return
        if self._closed:
            raise ProviderActivationError("state", "state_provider_closed")
        set_state_gate("activating")
        try:
            await _state_predicate(run_init=True)
            self.status["state"].state = "active"
            set_state_gate("active")
            for name in MANDATORY[1:]:
                await _PREDICATES[name]()
                self.status[name].state = "active"
        except ProviderActivationError as exc:
            self.status[exc.provider].state = "failed"
            self.status[exc.provider].reason_code = exc.reason_code
            set_state_gate("failed", exc.reason_code)
            logger.error("Mandatory provider %s failed to activate: %s", exc.provider, exc.reason_code)
            raise
        for name in DEGRADABLE:
            try:
                await _PREDICATES[name]()
                self.status[name].state = "active"
            except ProviderActivationError as exc:
                self.status[name].state, self.status[name].reason_code = "degraded", exc.reason_code
                logger.warning("Provider %s degraded: %s", name, exc.reason_code)
        self._activated = True

    async def readiness(self) -> dict:
        """Re-run every activated provider's predicate; never activates anything.

        A mandatory provider whose predicate now fails becomes ``failed`` with
        its typed reason (its Cordis methods block); a degraded or failed
        provider whose predicate passes again recovers to ``active``.
        """
        if not self._activated or self._closed:
            return self.snapshot()
        checks = {"state": lambda: _state_predicate(run_init=False), **_PREDICATES}
        for name in PROVIDERS:
            if self.status[name].state not in {"active", "degraded", "failed"}:
                continue
            try:
                await checks[name]()
                self.status[name].state, self.status[name].reason_code = "active", None
            except ProviderActivationError as exc:
                self.status[name].state = "failed" if name in MANDATORY else "degraded"
                self.status[name].reason_code = exc.reason_code
        return self.snapshot()

    async def current_reports(self) -> list[dict]:
        """Fresh readiness, as closed reports for a (re)started Cordis child."""
        await self.readiness()
        return await self.provider_reports()

    def snapshot(self) -> dict:
        return {"providers": [self.status[name].payload() for name in PROVIDERS],
                "mandatory_ready": all(self.status[name].state == "active" for name in MANDATORY)}

    async def provider_reports(self) -> list[dict]:
        """Closed five-field reports for the child's facades: no handle or key."""
        from src.db import engine as db_engine
        from .ownership import inventory
        async with db_engine.get_session() as db:
            rows = await inventory(db)
        reports = []
        for name in PROVIDERS:
            row = rows.get(f"seraph.{name}.v1")
            status = self.status[name]
            if row is None or status.state not in {"active", "degraded", "failed"}:
                continue
            reports.append({"domain": row.runtime_domain, "state": status.state, "epoch": row.epoch,
                            "composition_digest": row.composition_digest, "reason_code": status.reason_code})
        return reports

    def provider_ready(self, provider: str) -> bool:
        return self.status[provider].state == "active"

    # ---------------------------------------------------------------- shutdown

    def close_gate(self) -> None:
        """Step 3 of shutdown: later session requests fail typed, never hang."""
        from src.db.engine import set_state_gate
        set_state_gate("deactivating", "state_provider_deactivating")
        for name in reversed(PROVIDERS):
            if self.status[name].state in {"active", "degraded"}:
                self.status[name].state = "deactivated"
        self._closed = True

    async def close_state(self) -> None:
        """Step 4: dispose the canonical engine owned by the state provider."""
        await close_db()

    def detach(self) -> None:
        """End of the worker's life: the gate returns to its unattached state."""
        from src.db.engine import set_state_gate
        set_state_gate(None)
        _set_current(None)


_CURRENT: list[ProviderWorker | None] = [None]


def _set_current(worker: ProviderWorker | None) -> None:
    _CURRENT[0] = worker


def attach(worker: ProviderWorker) -> ProviderWorker:
    if type(worker) is not ProviderWorker:
        raise TypeError("provider worker required")
    _set_current(worker)
    return worker


def current_worker() -> ProviderWorker | None:
    return _CURRENT[0]


def domain_provider_unavailable(*runtime_domains: str) -> str | None:
    """Typed reason for the first domain whose attached-worker provider is not active."""
    worker = current_worker()
    if worker is None:
        return None
    for runtime_domain in runtime_domains:
        provider = DOMAIN_PROVIDERS.get(runtime_domain)
        if provider is not None and worker.status[provider].state != "active":
            return f"native_provider_{provider}_unavailable"
    return None


__all__ = ["DEGRADABLE", "DEPENDENCIES", "MANDATORY", "PROVIDERS", "ProviderActivationError", "ProviderWorker",
           "attach", "current_worker", "domain_provider_unavailable"]
