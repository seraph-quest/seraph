"""Passive Python providers for the canonical runtime domains (#1024, A3.1).

Python always hosts ``state``, ``authority``, ``audit``, ``tasks`` and
``vault``: they are activated first, in that order, and a failure of any of
them aborts startup. ``vault`` is the pinned credential vault (ADR-036 §3,
#1199): a Python-only trust service with its own owner row and no Cordis
facade, so its readiness is never reported to the child.
``goals``, ``memory`` and ``artifacts`` follow and may only degrade; their
Cordis service methods then return a typed block. The Cordis state,
authority and audit facades report readiness and own no activation (operator
decision on #1024, 2026-10-16), because the database must be open before the
ownership table can be read or the Node child started.

Facade activation (#1024, #1026 §1): ``goals``, ``memory`` and ``artifacts``
are the facade providers. While a facade domain's owner row is ``legacy``,
Python decides and its predicate alone activates the provider. While the row
is ``cordis``, the facade decides and Python is the mechanism: the provider is
``awaiting_facade`` until the running child's facade answers a host-sent
``provider.activate`` for exactly that row (owner, epoch, reviewed digest) in
the current boot, and every Cordis callback that depends on the domain is
checked against that receipt (boot nonce, epoch and digest). A child restart,
a new epoch or a deactivation therefore needs a fresh activation.

In-process maintenance handoff (#1024): ``maintenance()`` closes the
``get_session`` gate (``maintenance``), waits for the open sessions to close,
disposes the engine and runs the maintenance step under the workspace owner
lease this worker already holds (the lifecycle fence marker; no second lock
is opened, so the live lease is handed to the step instead of racing it), then
re-runs the mandatory predicates and reopens the gate, or leaves it ``failed``.
Only ``backup`` runs in process; restore and rollback replace the live
workspace root and stay with the host CLI against a stopped backend.

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

if __name__ == "__main__":
    # The passive provider-worker entry (#1207): it hardens the process and
    # checks its invocation before any import below can load a setting or a
    # secret, then runs the real ``src.runtime_plugins.worker`` module's
    # ProviderWorker. This ``__main__`` copy never defines anything.
    from src.runtime_plugins.worker_entry import main as _entry_main
    raise SystemExit(_entry_main())

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import stat

from src.db.engine import close_db, init_db

logger = logging.getLogger(__name__)

MANDATORY = ("state", "authority", "audit", "tasks", "vault")
DEGRADABLE = ("goals", "memory", "artifacts")
PROVIDERS = (*MANDATORY, *DEGRADABLE)
# Fixed activation dependencies; a provider never activates before these.
DEPENDENCIES = {
    "state": (), "authority": ("state",), "audit": ("state",), "tasks": ("state", "authority"),
    "goals": ("state", "authority"), "artifacts": ("state", "authority"),
    "memory": ("state", "authority", "audit"), "vault": ("state",),
}
# Runtime domain served by each provider, for typed Cordis method blocks.
DOMAIN_PROVIDERS = {f"seraph.{name}.v1": name for name in PROVIDERS}
# Providers with no Cordis facade: Python-only trust services whose readiness
# never crosses IPC (the child's closed report set has no such domain).
PYTHON_ONLY = frozenset({"vault"})
# Facade providers: activated by their Cordis facade while their domain is
# Cordis-owned (never a pinned trust provider).
FACADE_PROVIDERS = DEGRADABLE
# Maintenance steps the running backend may perform through the state provider.
MAINTENANCE_OPERATIONS = frozenset({"backup"})
MAINTENANCE_EVENT = "runtime_state_maintenance"
_AUTHORITY_TABLES = ("operator_sessions", "sessions")
_TASK_TABLES = ("workflow_run_states", "work_board_tasks", "work_board_attempts")
_GOAL_TABLES = ("goals",)
_MEMORY_TABLES = ("memories", "memory_tombstones", "memory_proposals")
_VAULT_TABLES = ("secrets",)


class ProviderActivationError(RuntimeError):
    def __init__(self, provider: str, reason_code: str):
        super().__init__(f"{provider}: {reason_code}")
        self.provider = provider
        self.reason_code = reason_code


class StateMaintenanceRefused(RuntimeError):
    """The in-process maintenance handoff refused before it closed anything."""

    def __init__(self, reason_code: str):
        super().__init__(reason_code)
        self.reason_code = reason_code


@dataclass
class ProviderStatus:
    name: str
    # inactive | active | awaiting_facade | degraded | failed | maintenance | deactivated
    state: str = "inactive"
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


def _vault_key_resolves(prefix: str = "authority_vault") -> str | None:
    """Configured key, or a readable 0600 key file; absent is generated on first use.

    Returns only a fixed ``<prefix>_key_*`` reason code; never the key, its file
    contents or a secret name. Authority keeps this check because its local
    signing keys derive from the vault key; the vault provider repeats it.
    """
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
            return f"{prefix}_key_unsafe"
        Fernet(Path(path).read_bytes().strip())
        return None
    except (OSError, ValueError, TypeError):
        return f"{prefix}_key_unavailable"


async def _state_predicate(*, run_init: bool) -> None:
    from sqlalchemy import text
    from src.db import engine as db_engine
    from .ownership import CompositionBindingError, inventory
    if run_init:
        try:
            await init_db()
        except CompositionBindingError as exc:
            # A corrupt pre-#1024 inventory refuses the state introduction typed.
            raise ProviderActivationError("state", exc.reason_code) from exc
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


class ProbeContended(RuntimeError):
    """A write probe met a held SQLite writer lock: unknown, never failed."""


# Bounded retry of the audit write probe under writer contention.
_AUDIT_PROBE_BACKOFF = (0.05, 0.2, 0.5)


_SQLITE_BUSY, _SQLITE_LOCKED = 5, 6
_SQLITE_LOCK_MESSAGES = ("database is locked", "database table is locked")


def _is_lock_contention(exc: BaseException) -> bool:
    """Exactly SQLite writer contention: SQLITE_BUSY/SQLITE_LOCKED or their messages."""
    import sqlite3
    original = getattr(exc, "orig", None) or exc
    if not isinstance(original, sqlite3.OperationalError):
        return False
    code = getattr(original, "sqlite_errorcode", None)
    if code is not None:
        return code & 0xFF in {_SQLITE_BUSY, _SQLITE_LOCKED}
    return str(original).strip().lower() in _SQLITE_LOCK_MESSAGES


async def _audit_write_probe() -> None:
    """Probe write inside a savepoint that is always rolled back.

    It takes the SQLite writer lock, so it runs only at activation and on a
    Cordis child (re)start, never on a status read. A held lock is retried
    with bounded backoff and then reported as ``ProbeContended`` (unknown).
    """
    from src.db import engine as db_engine
    from src.db.models import AuditEvent
    for attempt, delay in enumerate((*_AUDIT_PROBE_BACKOFF, None)):
        try:
            async with db_engine.get_session() as db:
                savepoint = await db.begin_nested()
                db.add(AuditEvent(session_id=None, actor="runtime", event_type="runtime_audit_ready",
                                  summary="Audit provider readiness probe", details_json="{}"))
                await db.flush()
                await savepoint.rollback()
                await db.rollback()
            return
        except Exception as exc:  # noqa: BLE001
            if not _is_lock_contention(exc):
                raise ProviderActivationError("audit", "audit_write_unavailable") from exc
            if delay is None:
                raise ProbeContended("audit_probe_contended") from exc
            await asyncio.sleep(delay)


async def _audit_predicate() -> None:
    try:
        await _audit_write_probe()
    except ProbeContended as exc:
        # At activation nothing else may hold the writer; contention there is a failure.
        raise ProviderActivationError("audit", "audit_write_contended") from exc


async def _audit_read_predicate() -> None:
    """Non-mutating status recheck: the audit table is present and readable."""
    if await _missing_tables("audit", ("audit_events",)):
        raise ProviderActivationError("audit", "audit_tables_missing")


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


async def _vault_predicate() -> None:
    """The pinned vault: its key resolves and its encrypted store is present and readable.

    Reads only the row-id column, never a key name or ciphertext, and
    decrypts nothing, so a status recheck can never surface vault content.
    """
    from sqlalchemy import text
    from src.db import engine as db_engine
    reason = _vault_key_resolves("vault")
    if reason is not None:
        raise ProviderActivationError("vault", reason)
    if await _missing_tables("vault", _VAULT_TABLES):
        raise ProviderActivationError("vault", "vault_tables_missing")
    try:
        async with db_engine.get_session() as db:
            await db.execute(text("SELECT id FROM secrets LIMIT 1"))
    except Exception as exc:  # noqa: BLE001 - startup reports one typed reason
        raise ProviderActivationError("vault", "vault_storage_unavailable") from exc


_PREDICATES = {
    "authority": _authority_predicate, "audit": _audit_predicate, "tasks": _tasks_predicate,
    "vault": _vault_predicate,
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
        # Providers in the order activation evaluated them (mandatory first; #1207 reports it).
        self.activation_order: list[str] = []
        # Serializes every status mutation (activation, rechecks, shutdown marks).
        self._status_lock = asyncio.Lock()
        # Last audit write probe: {"checked_at", "outcome", "reason_code"}.
        self.audit_probe: dict | None = None
        # Providers failed by a write probe: only a passing write probe recovers them.
        self._write_failed: set[str] = set()
        # Facade providers' owner rows as last read: name -> (owner_kind, epoch, digest, state).
        self._facade_rows: dict[str, tuple[str, int, str, str]] = {}
        # This boot's facade activations: name -> {"boot_nonce", "epoch", "digest"}.
        self._facades: dict[str, dict] = {}
        self._facade_host = None
        self._maintenance: str | None = None
        # Set while no maintenance step runs; shutdown waits on it before the lease goes.
        self._maintenance_idle = asyncio.Event()
        self._maintenance_idle.set()
        # Shutdown wakes a step still draining (nothing copied yet) and aborts it.
        self._maintenance_wake: asyncio.Event | None = None
        # draining | copying | resuming while a step runs.
        self._maintenance_phase: str | None = None
        self._maintenance_abort: str | None = None
        self._maintenance_task: asyncio.Task | None = None
        # The running step's handle once it entered its context (resume needs ``copied``).
        self._maintenance_handle: dict | None = None
        self._shutting_down = False
        # Statuses held across a maintenance window, restored by its predicates.
        self.last_maintenance: dict | None = None

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
        """State, authority, audit, tasks and vault or abort; then degradable providers."""
        async with self._status_lock:
            await self._activate()

    async def _activate(self) -> None:
        from src.db.engine import set_state_gate
        if self._activated:
            return
        if self._closed:
            raise ProviderActivationError("state", "state_provider_closed")
        set_state_gate("activating")
        try:
            self.activation_order.append("state")
            await _state_predicate(run_init=True)
            self.status["state"].state = "active"
            set_state_gate("active")
            for name in MANDATORY[1:]:
                self.activation_order.append(name)
                await _PREDICATES[name]()
                self.status[name].state = "active"
                if name == "audit":
                    self._record_audit_probe("passed", None)
        except ProviderActivationError as exc:
            self.status[exc.provider].state = "failed"
            self.status[exc.provider].reason_code = exc.reason_code
            set_state_gate("failed", exc.reason_code)
            logger.error("Mandatory provider %s failed to activate: %s", exc.provider, exc.reason_code)
            raise
        await self._read_facade_rows()
        for name in DEGRADABLE:
            self.activation_order.append(name)
            try:
                await _PREDICATES[name]()
                self._facade_passed(name)
            except ProviderActivationError as exc:
                self.status[name].state, self.status[name].reason_code = "degraded", exc.reason_code
                logger.warning("Provider %s degraded: %s", name, exc.reason_code)
        self._activated = True

    # ---------------------------------------------------------------- facades

    async def _read_facade_rows(self) -> None:
        from src.db import engine as db_engine
        from .ownership import inventory
        async with db_engine.get_session() as db:
            rows = await inventory(db)
        self._facade_rows = {
            name: (row.owner_kind, row.epoch, row.composition_digest, row.state)
            for name in FACADE_PROVIDERS if (row := rows.get(f"seraph.{name}.v1")) is not None}

    def _receipt_current(self, name: str) -> bool:
        """This boot's facade answer still names the facade domain's current owner row."""
        receipt, row, host = self._facades.get(name), self._facade_rows.get(name), self._facade_host
        return (receipt is not None and row is not None and host is not None
                and receipt["boot_nonce"] == getattr(host, "boot_nonce", None)
                and getattr(host, "state", None) in {"starting", "ready"}
                and (receipt["epoch"], receipt["digest"]) == (row[1], row[2]) and row[0] == "cordis"
                and row[3] == "ready")

    def _facade_passed(self, name: str) -> None:
        """A facade provider's predicate passed: who activates depends on its owner row."""
        row = self._facade_rows.get(name)
        status = self.status[name]
        if row is not None and row[0] == "cordis" and not self._receipt_current(name):
            status.state = "awaiting_facade"
            if status.reason_code is None or not status.reason_code.startswith("provider_facade_"):
                status.reason_code = "provider_facade_not_activated"
            return
        status.state, status.reason_code = "active", None

    async def activate_facades(self, host) -> list[dict]:
        """Ask the running child's facades to activate each Cordis-owned facade domain.

        Called by the host after the provider reports and before it admits
        anything, on every child (re)start. (No production path changes a
        facade domain's owner yet; a future cutover slice must call this after
        its transition.) The facade's answer is the decision: the provider activates
        only on ``activated`` with the plugin digest the Cordis owner row and
        the reviewed build both name. A legacy-owned facade domain is never
        asked (Python decides). Never touches a mandatory provider.
        """
        async with self._status_lock:
            if not self._activated or self._closed or self._maintenance:
                return []
            await self._read_facade_rows()
            self._facade_host = host
            results = []
            for name in FACADE_PROVIDERS:
                domain = f"seraph.{name}.v1"
                row, status = self._facade_rows.get(name), self.status[name]
                self._facades.pop(name, None)
                if row is None or row[0] != "cordis":
                    if status.state == "awaiting_facade":
                        status.state, status.reason_code = "active", None
                    continue
                if status.state not in {"active", "awaiting_facade"}:
                    continue  # its own predicate failed: nothing to activate
                if row[3] != "ready":
                    status.state, status.reason_code = "awaiting_facade", "provider_facade_owner_not_ready"
                    continue
                result = await host.activate_facade(
                    {"domain": domain, "owner_kind": "cordis", "epoch": row[1], "composition_digest": row[2]})
                reviewed = (getattr(getattr(host, "reviewed", None), "plugin_digests", None) or {}).get(domain)
                if result["state"] == "activated" and result["plugin_digest"] == row[2] == reviewed:
                    self._facades[name] = {"boot_nonce": host.boot_nonce, "epoch": row[1], "digest": row[2]}
                    status.state, status.reason_code = "active", None
                else:
                    reason = {"facade_not_composed": "provider_facade_not_composed"}.get(
                        result["reason_code"], "provider_facade_digest_mismatch")
                    status.state, status.reason_code = "awaiting_facade", reason
                    if result["state"] == "activated":
                        # The facade activated against a digest the reviewed build does not name.
                        await host.deactivate_facade(domain, row[1], "facade_digest_mismatch")
                results.append({"domain": domain, "state": status.state, "epoch": row[1],
                                "reason_code": status.reason_code})
            return results

    def facade_unavailable(self, runtime_domain: str, dependency, boot_nonce: str | None) -> str | None:
        """Typed reason when a Cordis-owned facade dependency lacks this boot's activation."""
        name = DOMAIN_PROVIDERS.get(runtime_domain)
        if name not in FACADE_PROVIDERS:
            return None
        if dependency is None:
            # Unbound in the vector: fail closed while the current row is Cordis-owned.
            row = self._facade_rows.get(name)
            return f"native_provider_{name}_facade_inactive" if row is not None and row[0] == "cordis" else None
        if dependency.owner_kind != "cordis":
            return None
        receipt = self._facades.get(name)
        host = self._facade_host
        if (receipt is None or boot_nonce is None or receipt["boot_nonce"] != boot_nonce
                or host is None or getattr(host, "boot_nonce", None) != boot_nonce
                or (receipt["epoch"], receipt["digest"]) != (dependency.epoch, dependency.composition_digest)):
            return f"native_provider_{name}_facade_inactive"
        return None

    def _record_audit_probe(self, outcome: str, reason_code: str | None) -> None:
        from datetime import datetime, timezone
        self.audit_probe = {"checked_at": datetime.now(timezone.utc).isoformat(), "outcome": outcome,
                            "reason_code": reason_code}

    async def readiness(self, *, probe_writes: bool = False) -> dict:
        """Re-run every activated provider's predicate; never activates anything.

        A mandatory provider whose predicate now fails becomes ``failed`` with
        its typed reason (its Cordis methods block); a degraded or failed
        provider whose predicate passes again recovers to ``active``.

        Status reads (``probe_writes=False``) are non-mutating: audit is
        rechecked read-only and the last write probe is reported with its
        timestamp. A Cordis child (re)start probes writes; a write probe that
        stays contended after its bounded retries is ``unknown`` and leaves
        the audit provider's state unchanged rather than failing it.
        """
        async with self._status_lock:
            if not self._activated or self._closed or self._maintenance:
                return self.snapshot()
            try:
                await self._read_facade_rows()
            except Exception:  # noqa: BLE001 - the state recheck below reports the typed reason
                pass
            checks = {"state": lambda: _state_predicate(run_init=False), **_PREDICATES,
                      "audit": _audit_write_probe if probe_writes else _audit_read_predicate}
            for name in PROVIDERS:
                if self._closed:
                    break  # shutdown began mid-recheck: its marks win
                if self.status[name].state not in {"active", "awaiting_facade", "degraded", "failed"}:
                    continue
                if name in self._write_failed and not probe_writes:
                    # A read-only recheck can never revive a provider whose writes failed.
                    continue
                try:
                    await checks[name]()
                    if self._closed:
                        break
                    if name in FACADE_PROVIDERS:
                        self._facade_passed(name)
                    else:
                        self.status[name].state, self.status[name].reason_code = "active", None
                    if name == "audit" and probe_writes:
                        self._write_failed.discard(name)
                        self._record_audit_probe("passed", None)
                except ProbeContended as exc:
                    self._record_audit_probe("unknown", str(exc))
                    logger.warning("Audit write probe contended; audit state left unchanged")
                except ProviderActivationError as exc:
                    if self._closed:
                        break
                    self.status[name].state = "failed" if name in MANDATORY else "degraded"
                    self.status[name].reason_code = exc.reason_code
                    if name == "audit" and probe_writes:
                        self._write_failed.add(name)
                        self._record_audit_probe("failed", exc.reason_code)
            return self.snapshot()

    async def current_reports(self) -> list[dict]:
        """Fresh readiness (with the audit write probe) as closed reports for a (re)started child."""
        await self.readiness(probe_writes=True)
        return await self.provider_reports()

    def _effective(self, name: str) -> dict[str, str | None]:
        payload = self.status[name].payload()
        row = self._facade_rows.get(name)
        if (name in FACADE_PROVIDERS and payload["state"] == "active" and row is not None and row[0] == "cordis"
                and not self._receipt_current(name)):
            # The answering boot stopped or the row moved since: awaiting a fresh activation.
            payload = {**payload, "state": "awaiting_facade", "reason_code": "provider_facade_not_activated"}
        if name in FACADE_PROVIDERS:
            payload["owner_kind"] = None if row is None else row[0]
            payload["facade"] = ("activated" if self._receipt_current(name)
                                 else "not_required" if row is None or row[0] != "cordis" else "inactive")
        return payload

    def snapshot(self) -> dict:
        return {"providers": [self._effective(name) for name in PROVIDERS],
                "mandatory_ready": all(self.status[name].state == "active" for name in MANDATORY),
                "maintenance": self._maintenance,
                "maintenance_status": self.maintenance_status(),
                "last_maintenance": None if self.last_maintenance is None else dict(self.last_maintenance),
                "audit_write_probe": None if self.audit_probe is None else dict(self.audit_probe)}

    async def provider_reports(self) -> list[dict]:
        """Closed five-field reports for the child's facades: no handle or key."""
        from src.db import engine as db_engine
        from .ownership import inventory
        async with db_engine.get_session() as db:
            rows = await inventory(db)
        reports = []
        for name in PROVIDERS:
            if name in PYTHON_ONLY:
                continue
            row = rows.get(f"seraph.{name}.v1")
            status = self.status[name]
            if row is None or status.state not in {"active", "awaiting_facade", "degraded", "failed"}:
                continue
            # Awaiting its facade is not ready: the closed report says degraded, with the reason.
            state = "degraded" if status.state == "awaiting_facade" else status.state
            reports.append({"domain": row.runtime_domain, "state": state, "epoch": row.epoch,
                            "composition_digest": row.composition_digest, "reason_code": status.reason_code})
        return reports

    def provider_ready(self, provider: str) -> bool:
        return self.status[provider].state == "active"

    # ---------------------------------------------------------------- maintenance

    async def _begin_maintenance(self, operation: str) -> None:
        """Refuse typed, or close the gate for ``operation``; nothing else is touched."""
        from src.db.engine import set_maintenance_control, set_state_gate
        if operation not in MAINTENANCE_OPERATIONS:
            raise StateMaintenanceRefused("state_maintenance_operation_unsupported")
        async with self._status_lock:
            if self._maintenance is not None:
                raise StateMaintenanceRefused("state_maintenance_in_progress")
            if (not self._activated or self._closed or self._shutting_down
                    or not all(self.status[name].state == "active" for name in MANDATORY)):
                raise StateMaintenanceRefused("state_maintenance_unavailable")
            if self.hold_workspace_lock and self._owner is None:
                raise StateMaintenanceRefused("state_maintenance_lease_missing")
            self._maintenance, self._maintenance_phase = operation, "draining"
            self._maintenance_wake, self._maintenance_abort = asyncio.Event(), None
            self._maintenance_idle.clear()
            set_state_gate("maintenance", "state_provider_maintenance")
            # Until the copy starts, the maintenance status and abort routes may authenticate.
            set_maintenance_control(True)
            self.status["state"].state, self.status["state"].reason_code = "maintenance", "state_provider_maintenance"

    @asynccontextmanager
    async def maintenance(self, operation: str, *, _begun: bool = False):
        """The in-process maintenance handoff through the state provider.

        Refuses typed, before closing anything, unless every mandatory
        provider is active, no other maintenance runs, the worker is not
        shutting down and (production) it holds the workspace owner lease.
        Then: the gate closes (``maintenance``: later session requests fail
        typed, never hang), dependent work blocks with
        ``native_provider_state_unavailable``, open sessions drain (woken by
        the last one to close, or by an operator abort or shutdown, which
        refuse ``state_maintenance_aborted``/``state_maintenance_shutdown``
        and reopen the gate at once: nothing was copied), the engine is
        disposed, the WAL is checkpointed into the database file (busy refuses
        typed), and the step runs under the held lease. The gate stays closed
        while the state provider re-verifies every mandatory provider (inside
        ``state_provider_scope``) and records the outcome; it reopens only if
        all of them pass, and is left ``failed`` on any failure or interruption.
        Yields a handle the step fills with its ``receipt`` or failure.
        """
        from src.db.engine import checkpoint_wal, drain_open_sessions, set_maintenance_control
        from src.workspace.lifecycle import lifecycle_fence_marker
        if not _begun:
            await self._begin_maintenance(operation)
        handle = {"operation": operation, "outcome": "completed", "reason_code": None, "receipt": None,
                  "copied": False}
        self._maintenance_handle = handle
        try:
            while True:
                await drain_open_sessions(self._maintenance_wake)
                if self._maintenance_abort is not None:
                    # Woken by an operator abort or by shutdown before anything was copied.
                    raise StateMaintenanceRefused(self._maintenance_abort)
                # Close control admission and read the open count in one step: a status or
                # abort request admitted after the wake would otherwise be open (and may
                # write auth bookkeeping) during the dispose and the copy. Nothing more can
                # be admitted after this, so a second drain always ends.
                if set_maintenance_control(False) == 0:
                    break
                self._maintenance_wake = asyncio.Event()
            self._maintenance_phase = "copying"
            handle["copied"] = True
            await close_db()
            if not await checkpoint_wal():
                # Another connection still holds the WAL: the file is not a whole copy.
                raise StateMaintenanceRefused("state_maintenance_checkpoint_busy")
            with lifecycle_fence_marker():
                yield handle
        except StateMaintenanceRefused as exc:
            handle["outcome"], handle["reason_code"] = "failed", exc.reason_code
            raise
        except BaseException:
            handle["outcome"], handle["reason_code"] = "failed", handle["reason_code"] or "state_maintenance_interrupted"
            raise
        finally:
            await self._resume_after_maintenance(handle)

    async def _resume_after_maintenance(self, handle: dict) -> None:
        """Reopen the gate only on proof; a resume failure surfaces typed, never raw."""
        from src.db.engine import set_maintenance_control, set_state_gate, state_provider_scope
        failure: BaseException | None = None
        try:
            await self._status_lock.acquire()
        except BaseException:
            # Cancelled before the resume could start: fail closed, never stay in ``maintenance``.
            if self._maintenance is not None:
                if not self._closed:
                    set_state_gate("failed", "state_maintenance_resume_failed")
                self._end_maintenance()
            raise
        try:
            self._maintenance_phase = "resuming"
            set_maintenance_control(False)
            try:
                if not handle["copied"]:
                    # Aborted while draining: the engine and database were never touched,
                    # so the gate reopens as it was and the outcome is audited normally.
                    if self._closed:
                        return  # close_gate already ran: its marks win
                    set_state_gate("active")
                    self.status["state"].state, self.status["state"].reason_code = "active", None
                    try:
                        self.last_maintenance = await self._record_maintenance(handle)
                    except Exception:  # noqa: BLE001 - reported, the gate is already open
                        self.last_maintenance = {**self._maintenance_details(handle), "audited": False}
                    return
                if self._closed or self._shutting_down:
                    # Shutdown began during the copy: the gate stays closed for it.
                    self.status["state"].state = "deactivated"
                    self.status["state"].reason_code = "state_maintenance_shutdown"
                    self.last_maintenance = {**self._maintenance_details(handle), "audited": False,
                                             "resume_reason_code": "state_maintenance_shutdown"}
                    return
                try:
                    with state_provider_scope():
                        await _state_predicate(run_init=False)
                        self.status["state"].state, self.status["state"].reason_code = "active", None
                        for name in MANDATORY[1:]:
                            await _PREDICATES[name]()
                            self.status[name].state, self.status[name].reason_code = "active", None
                            if name == "audit":
                                self._write_failed.discard(name)
                                self._record_audit_probe("passed", None)
                        await self._read_facade_rows()
                        for name in DEGRADABLE:
                            try:
                                await _PREDICATES[name]()
                                self._facade_passed(name)
                            except ProviderActivationError as exc:
                                self.status[name].state, self.status[name].reason_code = "degraded", exc.reason_code
                        # Last step before the gate reopens, so the row never says more than happened.
                        self.last_maintenance = await self._record_maintenance(handle)
                except BaseException as exc:
                    typed = isinstance(exc, ProviderActivationError)
                    provider = exc.provider if typed else "state"
                    reason = exc.reason_code if typed else "state_maintenance_resume_failed"
                    self.status[provider].state, self.status[provider].reason_code = "failed", reason
                    set_state_gate("failed", reason)
                    logger.error("Provider %s failed after maintenance: %s", provider, reason)
                    # The receipt (archive and manifest hash) is kept even though the gate stays closed.
                    self.last_maintenance = {**self._maintenance_details(handle), "audited": False,
                                             "resume_reason_code": reason}
                    if not typed:
                        failure = exc
                else:
                    set_state_gate("active")
            finally:
                self._end_maintenance()
        finally:
            self._status_lock.release()
        if failure is not None:
            if isinstance(failure, asyncio.CancelledError):
                raise failure
            raise StateMaintenanceRefused("state_maintenance_resume_failed") from failure

    def _end_maintenance(self) -> None:
        self._maintenance = None
        self._maintenance_wake = self._maintenance_phase = self._maintenance_abort = None
        self._maintenance_handle = None
        self._maintenance_idle.set()

    def abort_maintenance(self, reason_code: str = "state_maintenance_aborted") -> bool:
        """Wake a step still draining so it refuses ``reason_code``; a copying step is never cut."""
        if self._maintenance_phase != "draining" or self._maintenance_wake is None:
            return False
        self._maintenance_abort = reason_code
        self._maintenance_wake.set()
        return True

    def begin_shutdown(self) -> None:
        """First teardown step: no new step starts and a draining one is aborted.

        Called before the lifespan services stop, so a step whose drain only
        lifespan-owned holders blocked never turns into a backup during
        shutdown; a step already copying is waited for by ``wait_maintenance``.
        """
        self._shutting_down = True
        self.abort_maintenance("state_maintenance_shutdown")

    async def wait_maintenance(self) -> None:
        """Shutdown waits for a running maintenance step before releasing the lease.

        A tracked step cancelled before its first instruction never ran its
        own resume; it is resumed here so the gate is never left stranded.
        """
        self.begin_shutdown()
        task = self._maintenance_task
        if task is not None and task.done() and self._maintenance is not None:
            # Rescue with the step's real handle when it had one, so a copy is never taken for
            # an abort (and the gate never reopens over a copy that ran).
            await self._resume_after_maintenance(self._maintenance_handle or self._unentered_handle())
        await self._maintenance_idle.wait()

    async def start_backup(self) -> dict:
        """Close the gate now (or refuse typed) and run the backup as a worker-owned task.

        The HTTP route returns at once, so a server shutdown never waits on an
        in-flight request that is itself waiting on the drain.
        """
        from src.utils.background import track_task
        await self._begin_maintenance("backup")
        self._maintenance_task = track_task(self._background_backup(), name="state-maintenance-backup")
        return self.maintenance_status()

    async def _background_backup(self) -> None:
        try:
            await self.maintenance_backup(_begun=True)
        except StateMaintenanceRefused as exc:
            logger.warning("In-process backup refused: %s", exc.reason_code)
        finally:
            if self._maintenance is not None and self._maintenance_handle is None:
                # The gate closed in ``start_backup`` but the step never entered its
                # context (so no resume ran): nothing was copied, reopen through resume.
                await self._resume_after_maintenance(self._unentered_handle())

    def _unentered_handle(self) -> dict:
        return {"operation": self._maintenance or "backup", "outcome": "failed",
                "reason_code": "state_maintenance_interrupted", "receipt": None, "copied": False}

    def maintenance_status(self) -> dict:
        from src.db.engine import open_session_count
        return {"operation": self._maintenance, "phase": self._maintenance_phase,
                "draining_sessions": open_session_count() if self._maintenance_phase == "draining" else None,
                "last_maintenance": None if self.last_maintenance is None else dict(self.last_maintenance)}

    async def maintenance_backup(self, *, _begun: bool = False) -> dict:
        """Take a workspace backup in process, through the maintenance handoff.

        The existing ``backup_workspace`` owner writes the archive to the
        workspace's default backup directory. The step cannot be abandoned
        midway: a cancelled caller still waits for the archive step to end
        before the gate can reopen, so live writes never meet a running copy.
        The resume step records one ``runtime_state_maintenance`` audit event
        (a resume that fails before it can write is reported, not audited).
        """
        import contextvars
        import functools
        from config.settings import settings
        from src.workspace import canonical_workspace_registry, canonical_workspace_root
        from src.workspace.lifecycle import WorkspaceLifecycleError, backup_workspace
        cancelled = False
        async with self.maintenance("backup", _begun=_begun) as handle:
            try:
                root = canonical_workspace_root(self.workspace_dir or settings.workspace_dir)
                registry = canonical_workspace_registry(root)
            except Exception as exc:  # noqa: BLE001 - typed refusal; the resume reopens the gate
                raise StateMaintenanceRefused("state_maintenance_workspace_unavailable") from exc
            context = contextvars.copy_context()
            future = asyncio.get_running_loop().run_in_executor(
                None, functools.partial(context.run, backup_workspace, root, registry=registry))
            while not future.done():
                try:
                    await asyncio.shield(future)
                except asyncio.CancelledError:
                    cancelled = True
                except Exception:  # noqa: BLE001 - read below through future.exception()
                    break
            error = future.exception()
            if error is None:
                handle["receipt"] = future.result()
            else:
                handle["outcome"] = "failed"
                handle["reason_code"] = ("state_maintenance_backup_failed" if isinstance(error, WorkspaceLifecycleError)
                                         else "state_maintenance_backup_unavailable")
        if cancelled:
            raise asyncio.CancelledError
        if handle["reason_code"] is not None:
            raise StateMaintenanceRefused(handle["reason_code"])
        return handle["receipt"]

    @staticmethod
    def _maintenance_details(handle: dict) -> dict:
        receipt = handle.get("receipt") or {}
        return {"operation": handle["operation"], "outcome": handle["outcome"], "reason_code": handle["reason_code"],
                "archive_path": receipt.get("archive_path"),
                "archive_manifest_sha256": receipt.get("archive_manifest_sha256"),
                "member_count": receipt.get("member_count"), "secret_values_included": False}

    async def _record_maintenance(self, handle: dict) -> dict:
        """Runs inside the resume scope (or with the gate reopened after an abort)."""
        import json
        from datetime import datetime, timezone
        from src.db import engine as db_engine
        from src.db.models import AuditEvent
        details = self._maintenance_details(handle)
        audited = {key: value for key, value in details.items() if key != "archive_path"}
        async with db_engine.get_session() as db:
            db.add(AuditEvent(session_id=None, actor="runtime", event_type=MAINTENANCE_EVENT,
                              summary="In-process state maintenance", details_json=json.dumps(audited, sort_keys=True)))
        return {**details, "audited": True, "finished_at": datetime.now(timezone.utc).isoformat()}

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


def domain_provider_unavailable(*runtime_domains: str, vector=(), boot_nonce: str | None = None) -> str | None:
    """Typed reason for the first domain whose attached-worker provider is not active.

    ``vector`` is the invocation's bound dependency vector: a Cordis-owned
    facade dependency also needs this boot's facade activation at exactly its
    bound epoch and digest (#1024).
    """
    worker = current_worker()
    if worker is None:
        return None
    bound = {item.runtime_domain: item for item in vector}
    for runtime_domain in runtime_domains:
        provider = DOMAIN_PROVIDERS.get(runtime_domain)
        if provider is None:
            continue
        if worker.status[provider].state != "active":
            return f"native_provider_{provider}_unavailable"
        facade = worker.facade_unavailable(runtime_domain, bound.get(runtime_domain), boot_nonce)
        if facade is not None:
            return facade
    return None


__all__ = ["DEGRADABLE", "DEPENDENCIES", "FACADE_PROVIDERS", "MAINTENANCE_OPERATIONS", "MANDATORY", "PROVIDERS",
           "PYTHON_ONLY", "ProviderActivationError", "ProviderWorker", "StateMaintenanceRefused", "attach",
           "current_worker", "domain_provider_unavailable"]
