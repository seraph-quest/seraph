"""Scheduler and durable dispatcher lifecycle under the domain owner (#1025, A3.2).

Two runtime domains own the timer lifecycle, and Python keeps the mechanism:

* ``seraph.scheduler.v1``: the system jobs and the user cron jobs (which
  include governed schedules and source watches) in the single APScheduler.
* ``seraph.dispatch.v1``: the WorkBoard dispatcher (``work_board_dispatch``,
  a job in the same APScheduler) and its live-worker registry.

Activation of a domain is decided by its owner row. A ``legacy`` row that is
``ready`` activates directly; a ``cordis`` row additionally needs the running
reviewed child's boot proof for that domain. Nothing else differs between
owners: the same APScheduler, jobs, wrappers and stores run either way, and no
second queue, lane or budget exists.

* One activation per domain and process, recorded with its epoch and audited.
* Gated jobs are registered (or resumed) only on activation and paused on
  deactivation; deactivation closes admission, then drains in-flight work within
  its original bounds before the owner may move.
* Retention and cleanup jobs are exempt (lead decision on the #1025 review):
  they keep running whatever the owner rows say.
* ``SCHEDULER_ENABLED=false`` is ``disabled``, never ``blocked``.

Like the #1024 ``get_session`` gate, the lifecycle is only enforced once the
backend lifespan attaches it. A process that never attaches it (tests, host
CLIs, evals) keeps today's behaviour: every job registers at start and nothing
is gated. Importing this module creates nothing.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
import logging

logger = logging.getLogger(__name__)

SCHEDULER_DOMAIN = "seraph.scheduler.v1"
DISPATCH_DOMAIN = "seraph.dispatch.v1"
LIFECYCLE_DOMAINS = (SCHEDULER_DOMAIN, DISPATCH_DOMAIN)
# Deletion and retention jobs never wait on the Cordis gate (#1025 review, lead decision).
RETENTION_JOBS = frozenset({"screen_cleanup", "audio_ingress_cleanup"})
# The dispatcher's own timer; every other gated job belongs to the scheduler domain.
DISPATCH_JOBS = frozenset({"work_board_dispatch"})
# Bounded wait for an in-flight gated scheduler wrapper at deactivation.
SCHEDULER_DRAIN_SECONDS = 120.0
ACTIVATED_EVENT = "runtime_lifecycle_activated"
QUIESCED_EVENT = "runtime_lifecycle_quiesced"

# The activation a running gated job belongs to; durable writes made inside it
# repeat the owner check in their own transaction (``domain_ready``).
current_activation: ContextVar["Activation | None"] = ContextVar("seraph_lifecycle_activation", default=None)


class LifecycleBlocked(RuntimeError):
    """A lifecycle domain cannot run work now; the reason is operator-visible."""

    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class Activation:
    domain: str
    owner_kind: str
    epoch: int
    activated_at: str


def job_domain(job_id: str) -> str | None:
    """The lifecycle domain gating one APScheduler job, or None for an exempt job."""
    if job_id in RETENTION_JOBS:
        return None
    if job_id in DISPATCH_JOBS:
        return DISPATCH_DOMAIN
    return SCHEDULER_DOMAIN


def lifecycle_recovery(reason: str | None) -> str | None:
    if reason is None:
        return None
    if reason.startswith("cordis_"):
        return ("Start the Cordis runtime (./manage.sh -e dev local up locally, or ./manage.sh -e prod up -d --build "
                "under Docker), or roll the domain back to the legacy owner from Settings.")
    if reason == "composition_dependency_missing":
        return "Restart Seraph to seed the runtime inventory."
    if reason.endswith("_draining") or reason.endswith("_blocked"):
        return "Finish the owner transition (cutover or rollback) from Settings → Cordis runtime host."
    if reason == "scheduler_disabled":
        return "The scheduler is turned off (SCHEDULER_ENABLED=false)."
    return "Restart Seraph; the runtime inventory could not be read."


def _short(domain: str) -> str:
    return domain.removeprefix("seraph.").removesuffix(".v1")


class RuntimeLifecycle:
    """The process's scheduler and dispatch activations."""

    def __init__(self):
        self.attached = False
        self.activations: dict[str, Activation] = {}
        self.blocked: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._inflight: dict[str, set[asyncio.Task]] = {domain: set() for domain in LIFECYCLE_DOMAINS}

    # ---------------------------------------------------------------- decisions

    async def _row_decision(self, domain: str, host) -> tuple[str | None, object | None]:
        """(reason, row): reason is None only when the domain may be active now."""
        from config.settings import settings
        from src.db.engine import get_session
        from .ownership import CompositionBindingError, _boot_proved, inventory
        if not settings.scheduler_enabled:
            return "scheduler_disabled", None
        try:
            async with get_session() as db:
                rows = await inventory(db)
        except CompositionBindingError:
            return "composition_inventory_invalid", None
        row = rows.get(domain)
        if row is None:
            # An entirely unseeded inventory has never been cut over (today's behaviour).
            return ("composition_dependency_missing", None) if rows else (None, None)
        if row.state != "ready":
            return f"{_short(domain)}_owner_{row.state}", row
        if row.owner_kind == "cordis":
            if host is None or not getattr(host, "admitting", False) or getattr(host, "reviewed", None) is None:
                snapshot = host.snapshot() if host is not None else {}
                return "cordis_" + str(snapshot.get("reason") or "host_unavailable"), row
            if not _boot_proved(domain, "cordis", row.composition_digest, host):
                return "cordis_plugin_not_ready", row
        return None, row

    def active(self, domain: str) -> Activation | None:
        return self.activations.get(domain)

    def attach(self) -> None:
        """The backend lifespan enforces the lifecycle from here until ``detach``."""
        self.attached = True

    def detach(self) -> None:
        self.attached = False
        self.activations.clear()
        self.blocked.clear()

    def enforced(self) -> bool:
        return self.attached

    # ---------------------------------------------------------------- activation

    async def activate(self, domain: str, host=None, *, sync_user_jobs=None) -> Activation | None:
        """Activate one domain at its current epoch, or record why it is blocked.

        A second activation of an active domain is a no-op returning it.
        """
        from src.scheduler import engine
        async with self._lock:
            if domain in self.activations:
                return self.activations[domain]
            reason, row = await self._row_decision(domain, host)
            if reason is not None:
                self.blocked[domain] = reason
                logger.warning("Runtime lifecycle %s not activated: %s", domain, reason)
                return None
            activation = Activation(domain=domain, owner_kind=row.owner_kind if row is not None else "legacy",
                                    epoch=row.epoch if row is not None else 0,
                                    activated_at=datetime.now(timezone.utc).isoformat())
            await _audit(ACTIVATED_EVENT, {"runtime_domain": domain, "owner_kind": activation.owner_kind,
                                           "epoch": activation.epoch,
                                           "boot_nonce": str(getattr(host, "boot_nonce", "") or "")[:24]})
            self.activations[domain] = activation
            self.blocked.pop(domain, None)
        # Timers start only now, under the recorded epoch.
        engine.start_domain_jobs(domain)
        if domain == SCHEDULER_DOMAIN and sync_user_jobs is not None:
            await sync_user_jobs()
        return activation

    async def deactivate(self, domain: str, reason: str) -> None:
        """Close admission, drain within the original bounds, then quiesce."""
        from src.scheduler import engine
        async with self._lock:
            activation = self.activations.pop(domain, None)
            self.blocked[domain] = reason
        if activation is None:
            return
        engine.pause_domain_jobs(domain)
        await self._drain(domain)
        await _audit(QUIESCED_EVENT, {"runtime_domain": domain, "epoch": activation.epoch, "reason": reason})

    async def _drain(self, domain: str) -> None:
        tasks = {task for task in self._inflight[domain] if not task.done()}
        if domain == DISPATCH_DOMAIN:
            from .lifecycle_dispatch import drain_dispatch
            await drain_dispatch()
        if not tasks:
            return
        done, pending = await asyncio.wait(tasks, timeout=SCHEDULER_DRAIN_SECONDS)
        for task in pending:
            # Past the bound: the wrapper stops here; its durable work keeps its own lease.
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=5.0)

    # ---------------------------------------------------------------- the job gate

    def admit(self, job_id: str) -> tuple[Activation | None, str | None]:
        """(activation, blocked_reason) for one gated tick; (None, None) for an exempt job."""
        domain = job_domain(job_id)
        if domain is None or not self.attached:
            return None, None
        activation = self.activations.get(domain)
        if activation is None:
            return None, self.blocked.get(domain) or f"{_short(domain)}_owner_inactive"
        return activation, None

    def track(self, domain: str, task: asyncio.Task) -> None:
        self._inflight[domain].add(task)
        task.add_done_callback(self._inflight[domain].discard)

    # ---------------------------------------------------------------- status

    async def status(self, domain: str, host=None) -> dict:
        activation = self.activations.get(domain)
        if activation is not None:
            return {"owner": activation.owner_kind, "epoch": activation.epoch, "state": "active", "reason": None,
                    "recovery": None}
        reason, row = await self._row_decision(domain, host)
        reason = self.blocked.get(domain) or reason
        state = "disabled" if reason == "scheduler_disabled" else "blocked" if reason else "inactive"
        return {"owner": getattr(row, "owner_kind", None), "epoch": getattr(row, "epoch", None), "state": state,
                "reason": reason, "recovery": lifecycle_recovery(reason)}


async def _audit(event_type: str, details: dict) -> None:
    import json
    from src.db.engine import get_session
    from src.db.models import AuditEvent
    async with get_session() as db:
        db.add(AuditEvent(session_id=None, actor="runtime", event_type=event_type,
                          summary="Runtime lifecycle " + event_type.rsplit("_", 1)[-1],
                          details_json=json.dumps(details, sort_keys=True)))


runtime_lifecycle = RuntimeLifecycle()


async def activate_runtime_lifecycle(host=None, *, sync_user_jobs=None) -> dict[str, Activation | None]:
    """Boot order: the scheduler domain, then dispatch (after the host and the boot cutover)."""
    return {SCHEDULER_DOMAIN: await runtime_lifecycle.activate(SCHEDULER_DOMAIN, host, sync_user_jobs=sync_user_jobs),
            DISPATCH_DOMAIN: await runtime_lifecycle.activate(DISPATCH_DOMAIN, host)}


async def deactivate_runtime_lifecycle(reason: str = "runtime_shutdown") -> None:
    """Shutdown order: dispatch first, then the scheduler domain."""
    for domain in reversed(LIFECYCLE_DOMAINS):
        await runtime_lifecycle.deactivate(domain, reason)


__all__ = ["ACTIVATED_EVENT", "DISPATCH_DOMAIN", "DISPATCH_JOBS", "LIFECYCLE_DOMAINS", "QUIESCED_EVENT",
           "RETENTION_JOBS", "SCHEDULER_DOMAIN", "Activation", "LifecycleBlocked", "RuntimeLifecycle",
           "activate_runtime_lifecycle", "current_activation", "deactivate_runtime_lifecycle", "job_domain",
           "runtime_lifecycle"]
