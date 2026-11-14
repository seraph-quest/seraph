"""Scheduler and WorkBoard dispatcher lifecycle under the domain epoch (#1025, A3.2).

Two runtime domains own a lifecycle, not a store: ``seraph.scheduler.v1`` (the
timer lifecycle of system jobs, user cron jobs and governed schedules) and
``seraph.dispatch.v1`` (the WorkBoard dispatcher loop and its live-worker
registry). "Cordis-owned" means Cordis owns the decision and Python owns the
mechanism: the retained Python provider (the one ``AsyncIOScheduler`` and
``WorkBoardDispatcher.run_pass``) is activated for a domain only while

* its owner row is ``ready`` at epoch E, and
* for an owner of kind ``cordis``: the host is admitting and the running child
  proves exactly this domain's reviewed plugin digest (``_boot_proved``).

The legacy owner activates the same provider by the same code path with no
Cordis precondition, so "who decides" is the only thing that differs. The
child never starts a timer, never executes work and stores nothing; the
existing durable stores (``ScheduledJob``/``ScheduledJobRun``, governed
occurrences, ``WorkflowRunState``, ``WorkBoardTask``/``WorkBoardAttempt``) stay
the only queues.

Mechanics:

* One ``AsyncIOScheduler`` holds every job. Its timers keep their phase; a
  gated job whose domain is not active returns at entry and records a visible
  block instead of pausing/resuming the instance (so a cutover or host loss
  never drops a cron fire through a 1 s misfire grace on resume).
* Retention and cleanup jobs (``RETENTION_JOBS``) are exempt and keep running
  while a domain is blocked or Cordis is missing (lead decision on #1025).
* Every activation records the epoch it was granted and an audit event. A tick
  carries that activation in a context variable; the durable effects of the
  tick (``ScheduledJobRun`` insert, governed reserve/claim, WorkBoard
  promote/claim/block) re-check ``domain_ready`` inside their own transaction.
  The in-memory activation only fails fast; the transaction is the guarantee.
* Deactivation closes admission first, then drains in-flight work within the
  original bounds (scheduler wrappers: ``SCHEDULER_DRAIN_SECONDS``; WorkBoard
  attempts: their own runtime bound plus settlement), then cancels what is left
  (cancellation runs the attempts' own recovery paths; nothing is forced or
  retried), then audits ``runtime_lifecycle_quiesced``.

Importing this module creates nothing: no task, timer, session or file.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import time
from typing import Any, Awaitable, Callable

from .ownership import DISPATCH_DOMAIN, SCHEDULER_DOMAIN, CompositionBindingError, domain_ready_predicate

logger = logging.getLogger(__name__)

LIFECYCLE_DOMAINS = (SCHEDULER_DOMAIN, DISPATCH_DOMAIN)
_SHORT = {SCHEDULER_DOMAIN: "scheduler", DISPATCH_DOMAIN: "dispatch"}
ACTIVATED_EVENT = "runtime_lifecycle_activated"
QUIESCED_EVENT = "runtime_lifecycle_quiesced"

# Closed classification of every system job registered by ``init_scheduler``.
# Retention/cleanup (deletion) jobs are exempt from the Cordis gate.
RETENTION_JOBS = frozenset({"screen_cleanup", "audio_ingress_cleanup", "cordis_mail_retention"})
DISPATCH_JOBS = frozenset({"work_board_dispatch"})
SCHEDULER_GATED_JOBS = frozenset({
    "programme_daily_digest", "goal_public_discovery", "memory_consolidation", "goal_check", "calendar_scan",
    "strategist_tick", "guardian_opportunity_assessment", "daily_briefing", "evening_review", "activity_digest",
    "end_of_day_goal_report", "weekly_activity_review", "screenshot_folder_ingest", "screenshot_folder_analysis",
    "screenshot_observation_digest", "inference_ledger_verification",
})
USER_CRON_PREFIX = "user_cron:"
# Original bounds (never new ones): an in-flight scheduler wrapper (at most one
# per job, ``max_instances=1``) and a WorkBoard attempt (its runtime bound plus
# the settlement window).
SCHEDULER_DRAIN_SECONDS = 120.0
SETTLEMENT_SECONDS = 30.0
# A domain that is not active is re-evaluated on a gated tick at most this often.
REACTIVATE_INTERVAL_SECONDS = 5.0


class LifecycleBlocked(RuntimeError):
    """A tick or launch refused because its lifecycle domain is not active; nothing was contacted."""

    def __init__(self, reason_code: str, recovery: str | None = None):
        self.reason_code = reason_code
        self.recovery = recovery or lifecycle_recovery(reason_code)
        super().__init__(reason_code)


class LifecycleFenced(LifecycleBlocked):
    """A durable effect saw that the activation's owner row moved (stale epoch)."""


def lifecycle_recovery(reason: str) -> str:
    if reason.startswith("cordis_") or reason == "composition_boot_proof_missing":
        return ("Start the Cordis runtime (npm ci --prefix runtime/cordis --ignore-scripts, then "
                "./manage.sh local up); scheduled work resumes on the next tick once it reports ready.")
    if reason.endswith("_owner_draining"):
        return "An owner transition is draining in-flight work; scheduled work resumes when it finishes."
    if reason.endswith("_owner_blocked"):
        return ("Finish the owner transition (cutover or rollback) at a higher epoch, or restart Seraph with the "
                "Cordis runtime available.")
    if reason == "composition_dependency_changed":
        return "The owner changed while this tick ran; nothing was contacted and the next tick runs under the new owner."
    if reason == "composition_dependency_missing":
        return "Restart Seraph to seed the runtime inventory."
    if reason == "composition_inventory_invalid":
        return "Repair the runtime composition inventory (a malformed owner row fails closed)."
    if reason == "lifecycle_not_started":
        return "Restart Seraph; the scheduler lifecycle did not start."
    if reason == "scheduler_disabled":
        return "The scheduler is disabled (SCHEDULER_ENABLED=false); enable it and restart to run scheduled work."
    return "Restart Seraph; if it stays blocked check /api/runtime/status for the owning domain."


def job_domain(job_id: str) -> str | None:
    """The lifecycle domain gating one scheduler job, or ``None`` for an exempt retention job.

    An unclassified id fails closed: a new system job must be classified here.
    """
    if job_id in RETENTION_JOBS:
        return None
    if job_id in DISPATCH_JOBS:
        return DISPATCH_DOMAIN
    if job_id in SCHEDULER_GATED_JOBS or job_id.startswith(USER_CRON_PREFIX):
        return SCHEDULER_DOMAIN
    raise LifecycleBlocked("lifecycle_job_unclassified", "Classify the scheduler job in runtime_plugins.lifecycle.")


@dataclass(frozen=True)
class Activation:
    domain: str
    owner_kind: str
    epoch: int
    composition_digest: str
    boot_nonce: str | None
    activated_at: str

    def payload(self) -> dict[str, Any]:
        return {"domain": self.domain, "owner_kind": self.owner_kind, "epoch": self.epoch,
                "activated_at": self.activated_at}


# The activation a running tick was admitted under; durable effects fence on it.
_TICK: ContextVar[Activation | None] = ContextVar("seraph_lifecycle_tick", default=None)


def current_tick() -> Activation | None:
    return _TICK.get()


async def assert_tick_fence(db) -> None:
    """Inside the caller's transaction: the tick's owner row is still the exact ready owner.

    Outside a lifecycle tick (API or manual writes) this is a no-op.
    """
    activation = _TICK.get()
    if activation is None:
        return
    from sqlalchemy import select
    ready = await db.scalar(select(domain_ready_predicate(
        activation.domain, activation.owner_kind, activation.epoch, activation.composition_digest)))
    if not ready:
        raise LifecycleFenced("composition_dependency_changed")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RuntimeLifecycle:
    """Process-local lifecycle owner of the scheduler and dispatch providers (single process).

    The workspace owner lock (#1024) admits one process; within it, one
    activation per domain at a time, keyed by the epoch it was granted.
    """

    def __init__(self) -> None:
        self.host: Any = None
        self.managed = False
        self.scheduler_enabled = True
        self._active: dict[str, Activation] = {}
        self._status: dict[str, dict[str, Any]] = {}
        self._inflight: dict[str, set[asyncio.Task]] = {domain: set() for domain in LIFECYCLE_DOMAINS}
        self._locks: dict[str, asyncio.Lock] = {}
        self._transitioning: dict[str, str] = {}
        self._last_attempt: dict[str, float] = {}
        self._notified: dict[str, str] = {}
        self.worker_registry: Callable[[], dict] | None = None
        for domain in LIFECYCLE_DOMAINS:
            self._set_status(domain, "inactive", "lifecycle_not_started")

    # ------------------------------------------------------------ status

    def _set_status(self, domain: str, state: str, reason: str | None, activation: Activation | None = None,
                    **extra: Any) -> dict[str, Any]:
        previous = self._status.get(domain) or {}
        since = previous.get("since") if (previous.get("state"), previous.get("reason")) == (state, reason) else _now()
        status = {"domain": domain, "state": state, "reason": reason,
                  "recovery": None if reason is None else lifecycle_recovery(reason),
                  "owner_kind": activation.owner_kind if activation else previous.get("owner_kind"),
                  "epoch": activation.epoch if activation else previous.get("epoch"),
                  "since": since, **extra}
        self._status[domain] = status
        return status

    def status(self, domain: str) -> dict[str, Any]:
        self.current(domain)  # host loss is reflected without a write
        status = dict(self._status[domain])
        status["inflight"] = len([task for task in self._inflight[domain] if not task.done()])
        if domain == DISPATCH_DOMAIN and self.worker_registry is not None:
            status["live_workers"] = len([task for task in self.worker_registry().values()
                                          if task is not None and not task.done()])
        if domain in self._transitioning:
            status["transition"] = self._transitioning[domain]
        return status

    def _lock(self, domain: str) -> asyncio.Lock:
        lock = self._locks.get(domain)
        if lock is None:
            lock = self._locks[domain] = asyncio.Lock()
        return lock

    # ------------------------------------------------------------ activation

    def current(self, domain: str) -> Activation | None:
        """The live activation, or ``None``; a lost Cordis host deactivates it at once."""
        activation = self._active.get(domain)
        if activation is None or activation.owner_kind != "cordis":
            return activation
        host = self.host
        if host is None or not getattr(host, "admitting", False) or getattr(host, "boot_nonce", None) != activation.boot_nonce:
            # Close admission now; in-flight ticks finish and their effects keep their fences.
            self._active.pop(domain, None)
            snapshot = host.snapshot() if host is not None and hasattr(host, "snapshot") else {}
            reason = "cordis_" + str(snapshot.get("reason") or "host_unavailable")
            self._set_status(domain, "blocked", reason, activation)
            logger.warning("Lifecycle %s deactivated: the Cordis host stopped admitting (%s)", domain, reason)
            return None
        return activation

    async def _evaluate(self, domain: str):
        """Read the owner row; return (activation_candidate, None) or (None, reason)."""
        from src.db.engine import get_session
        from .ownership import _boot_proved, inventory
        try:
            async with get_session() as db:
                rows = await inventory(db)
        except CompositionBindingError:
            return None, "composition_inventory_invalid"
        row = rows.get(domain)
        if row is None:
            return None, "composition_dependency_missing"
        if row.state != "ready":
            return None, f"{_SHORT[domain]}_owner_{row.state}"
        boot_nonce = None
        if row.owner_kind == "cordis":
            host = self.host
            if host is None or not getattr(host, "admitting", False) or getattr(host, "reviewed", None) is None:
                snapshot = host.snapshot() if host is not None and hasattr(host, "snapshot") else {}
                return None, "cordis_" + str(snapshot.get("reason") or "host_unavailable")
            if not _boot_proved(domain, row.owner_kind, row.composition_digest, host):
                return None, "composition_boot_proof_missing"
            boot_nonce = host.boot_nonce
        return Activation(domain, row.owner_kind, row.epoch, row.composition_digest, boot_nonce, _now()), None

    async def activate(self, domain: str) -> dict[str, Any]:
        """Activate one domain's provider at its current ready epoch; a second call is a no-op."""
        if domain not in LIFECYCLE_DOMAINS:
            raise CompositionBindingError("composition_domain_unknown")
        if not self.managed:
            return self.status(domain)
        if domain == SCHEDULER_DOMAIN and not self.scheduler_enabled:
            self._set_status(domain, "disabled", "scheduler_disabled")
            return self.status(domain)
        async with self._lock(domain):
            if domain in self._transitioning or self.current(domain) is not None:
                return self.status(domain)
            self._last_attempt[domain] = time.monotonic()
            candidate, reason = await self._evaluate(domain)
            if candidate is None:
                await self._enter_blocked(domain, reason)
                return self.status(domain)
            # Re-check the exact ready owner and audit the activation in one transaction.
            from sqlalchemy import select
            from src.db.engine import get_session
            from src.db.models import AuditEvent
            async with get_session() as db:
                ready = await db.scalar(select(domain_ready_predicate(
                    domain, candidate.owner_kind, candidate.epoch, candidate.composition_digest)))
                if not ready:
                    await db.rollback()
                    await self._enter_blocked(domain, "composition_dependency_changed")
                    return self.status(domain)
                db.add(AuditEvent(session_id=None, actor="runtime", event_type=ACTIVATED_EVENT,
                                  summary=f"Runtime lifecycle {domain} activated",
                                  details_json=json.dumps({**candidate.payload(), "boot_nonce_present":
                                                           candidate.boot_nonce is not None}, sort_keys=True)))
            self._active[domain] = candidate
            was_blocked = self._notified.pop(domain, None) is not None
            self._set_status(domain, "active", None, candidate)
            logger.info("Lifecycle %s active under %s@%d", domain, candidate.owner_kind, candidate.epoch)
            if was_blocked:
                await self._activity(domain, "recovered", None)
            return self.status(domain)

    async def ensure(self, domain: str) -> Activation | None:
        """The live activation, re-evaluating a non-active domain at most every few seconds."""
        activation = self.current(domain)
        if activation is not None or not self.managed or domain in self._transitioning:
            return activation
        if time.monotonic() - self._last_attempt.get(domain, 0.0) < REACTIVATE_INTERVAL_SECONDS:
            return None
        await self.activate(domain)
        return self.current(domain)

    # ------------------------------------------------------------ deactivation

    async def deactivate(self, domain: str, reason: str, *, drain_seconds: float | None = None,
                         mark_jobs: bool = True) -> dict[str, Any]:
        """Close admission, drain within the original bounds, cancel what is left, audit quiescence."""
        if domain not in LIFECYCLE_DOMAINS:
            raise CompositionBindingError("composition_domain_unknown")
        async with self._lock(domain):
            activation = self._active.pop(domain, None)
            self._set_status(domain, "draining", reason, activation)
            bound = drain_seconds
            if bound is None:
                bound = SCHEDULER_DRAIN_SECONDS if domain == SCHEDULER_DOMAIN else _dispatch_drain_seconds()
            cancelled = await self._drain(domain, bound)
            if activation is not None:
                await self._audit_quiesced(activation, reason, cancelled)
            await self._enter_blocked(domain, reason, activation=activation, mark_jobs=mark_jobs)
            return self.status(domain)

    async def _drain(self, domain: str, bound: float) -> int:
        def pending() -> list[asyncio.Task]:
            tasks = [task for task in self._inflight[domain] if not task.done()]
            if domain == DISPATCH_DOMAIN and self.worker_registry is not None:
                tasks.extend(task for task in self.worker_registry().values()
                             if task is not None and not task.done() and task not in tasks)
            current = asyncio.current_task()
            return [task for task in tasks if task is not current]
        deadline = time.monotonic() + max(bound, 0.0)
        while pending() and time.monotonic() < deadline:
            await asyncio.wait(pending(), timeout=min(0.5, max(deadline - time.monotonic(), 0.01)))
        remaining = pending()
        for task in remaining:
            # Past the original bound: cancellation runs the work's own recovery
            # path (stale-lease recovery leaves an unresolved effect unknown).
            task.cancel()
        if remaining:
            await asyncio.wait(remaining, timeout=5.0)
            logger.warning("Lifecycle %s drain cancelled %d task(s) past their bound", domain, len(remaining))
        return len(remaining)

    async def _audit_quiesced(self, activation: Activation, reason: str, cancelled: int) -> None:
        from src.db.engine import get_session
        from src.db.models import AuditEvent
        try:
            async with get_session() as db:
                db.add(AuditEvent(session_id=None, actor="runtime", event_type=QUIESCED_EVENT,
                                  summary=f"Runtime lifecycle {activation.domain} quiesced",
                                  details_json=json.dumps({**activation.payload(), "reason": reason,
                                                           "cancelled": cancelled}, sort_keys=True)))
        except Exception:  # noqa: BLE001 - shutdown with a closed state gate
            logger.warning("Lifecycle %s quiescence audit unavailable", activation.domain, exc_info=True)

    async def _enter_blocked(self, domain: str, reason: str, *, activation: Activation | None = None,
                             mark_jobs: bool = True) -> None:
        self._set_status(domain, "blocked", reason, activation)
        if not mark_jobs:
            return
        if self._notified.get(domain) is None:
            # One notification and one Activity entry per domain transition into blocked.
            self._notified[domain] = reason
            await self._activity(domain, "blocked", reason)
            await self._notify(domain, reason)
        for job_id in self.gated_job_ids(domain):
            record_job_blocked(job_id, reason)

    def gated_job_ids(self, domain: str) -> list[str]:
        from src.scheduler.engine import get_scheduler
        scheduler = get_scheduler()
        if scheduler is None:
            return []
        result = []
        for job in scheduler.get_jobs():
            try:
                if job_domain(job.id) == domain:
                    result.append(job.id)
            except LifecycleBlocked:
                continue
        return result

    async def _activity(self, domain: str, outcome: str, reason: str | None) -> None:
        try:
            from src.audit.runtime import log_scheduler_job_event
            await log_scheduler_job_event(job_name=f"lifecycle:{_SHORT[domain]}", outcome=outcome, details={
                "runtime_domain": domain, **({"reason": reason, "recovery": lifecycle_recovery(reason)}
                                             if reason else {})})
        except Exception:  # noqa: BLE001 - the status stays the visible record
            logger.warning("Lifecycle %s Activity entry unavailable", domain, exc_info=True)

    async def _notify(self, domain: str, reason: str) -> None:
        if reason in {"lifecycle_shutdown", "scheduler_disabled"}:
            return
        try:
            from src.models.schemas import WSResponse
            from src.observer.delivery import deliver_or_queue
            label = "Scheduled work" if domain == SCHEDULER_DOMAIN else "Work Board dispatch"
            await deliver_or_queue(WSResponse(type="proactive",
                                              content=f"{label} is blocked: {lifecycle_recovery(reason)}",
                                              intervention_type="advisory", urgency=3,
                                              reasoning=f"Runtime lifecycle {domain} blocked ({reason})"),
                                   is_scheduled=True)
        except Exception:  # noqa: BLE001
            logger.warning("Could not announce the %s block", domain, exc_info=True)

    # ------------------------------------------------------------ ticks and launches

    async def run_gated(self, job_id: str, run: Callable[[], Awaitable[Any]]) -> Any:
        """Run one scheduler tick under its domain's activation, or record a visible block."""
        domain = job_domain(job_id)
        if domain is None:
            return await run()
        if not self.managed:
            record_job_blocked(job_id, "lifecycle_not_started")
            return None
        activation = await self.ensure(domain)
        if activation is None:
            status = self._status.get(domain) or {}
            if status.get("state") != "disabled":
                record_job_blocked(job_id, str(status.get("reason") or "lifecycle_not_started"))
            return None
        task = asyncio.current_task()
        token = _TICK.set(activation)
        if task is not None:
            self._inflight[domain].add(task)
        try:
            result = await run()
        except LifecycleBlocked as exc:
            record_job_blocked(job_id, exc.reason_code)
            logger.warning("Scheduled job %s blocked by its lifecycle: %s", job_id, exc.reason_code)
            return None
        finally:
            _TICK.reset(token)
            if task is not None:
                self._inflight[domain].discard(task)
        record_job_ok(job_id)
        return result

    def require_dispatch_active(self) -> None:
        """API-launched workers enter the live-worker registry only while dispatch is active."""
        if not self.managed:
            return
        if self.current(DISPATCH_DOMAIN) is None:
            raise LifecycleBlocked("dispatch_owner_not_active",
                                   (self._status.get(DISPATCH_DOMAIN) or {}).get("recovery")
                                   or lifecycle_recovery("dispatch_owner_blocked"))

    @asynccontextmanager
    async def dispatch_launch(self):
        """Bind an API-launched WorkBoard worker to the live dispatch activation."""
        self.require_dispatch_active()
        activation = self.current(DISPATCH_DOMAIN)
        token = _TICK.set(activation) if activation is not None else None
        try:
            yield activation
        finally:
            if token is not None:
                _TICK.reset(token)

    # ------------------------------------------------------------ process lifecycle

    async def start(self, host, *, scheduler_enabled: bool) -> list[dict[str, Any]]:
        """Called once by the lifespan after the host started and the boot cutover ran."""
        self.host = host
        self.managed = True
        self.scheduler_enabled = scheduler_enabled
        if self.worker_registry is None:
            from src.work_board.dispatcher import _ACTIVE_WORKER_TASKS
            self.worker_registry = lambda: _ACTIVE_WORKER_TASKS
        return [await self.activate(domain) for domain in LIFECYCLE_DOMAINS]

    async def stop(self) -> None:
        """Shutdown: close admission and keep today's bounded shutdown (no new wait)."""
        try:
            for domain in reversed(LIFECYCLE_DOMAINS):
                await self.deactivate(domain, "lifecycle_shutdown", drain_seconds=0.0
                                      if domain == DISPATCH_DOMAIN else 5.0, mark_jobs=False)
        finally:
            self.managed = False
            self.host = None

    def claim_transition(self, domain: str) -> None:
        """Synchronously reserve the one live transition of a domain (no await in between)."""
        if domain not in LIFECYCLE_DOMAINS:
            raise CompositionBindingError("composition_domain_unknown")
        if domain in self._transitioning:
            raise CompositionBindingError("composition_transition_in_progress")
        self._transitioning[domain] = "queued"

    async def transition(self, domain: str, target: str, *, actor: str, expected_epoch: int | None,
                         reason: str, claimed: bool = False) -> dict[str, Any]:
        """Live owner transition of a lifecycle domain: drain at E, flip, reactivate at E+1.

        Drain-before-flip (review item 1): admission closes and in-flight work
        settles while the row is still ready at E; only then does the fenced
        writer move it, so no heartbeat or settle interleaves with ``draining``.
        """
        from .bridge import cordis_host
        from .cutover import transition_domain
        if not claimed:
            self.claim_transition(domain)
        self._transitioning[domain] = "draining"
        try:
            await self.deactivate(domain, f"{_SHORT[domain]}_owner_draining", mark_jobs=False)
            self._transitioning[domain] = "transitioning"
            receipt = await transition_domain(domain, target, host=self.host or cordis_host, actor=actor,
                                              expected_epoch=expected_epoch, reason=reason)
        except BaseException:
            self._transitioning.pop(domain, None)
            # Whatever happened, the row decides: reactivate if it is still ready.
            self._last_attempt.pop(domain, None)
            await self.activate(domain)
            raise
        self._transitioning.pop(domain, None)
        self._last_attempt.pop(domain, None)
        await self.activate(domain)
        self._set_status(domain, self._status[domain]["state"], self._status[domain]["reason"],
                         self._active.get(domain), last_transition=receipt)
        return receipt


def _dispatch_drain_seconds() -> float:
    from src.work_board.dispatcher import MAX_RUNTIME_SECONDS
    return float(MAX_RUNTIME_SECONDS) + SETTLEMENT_SECONDS


def record_job_blocked(job_id: str, reason: str) -> None:
    from src.scheduler.background_inference import record_scheduler_job_blocked
    try:
        record_scheduler_job_blocked(job_id, reason, lifecycle_recovery(reason))
    except Exception:  # noqa: BLE001 - the domain status stays the visible record
        logger.warning("Could not record the %s block", job_id, exc_info=True)


def record_job_ok(job_id: str) -> None:
    from src.scheduler.background_inference import record_scheduler_job_ok
    try:
        record_scheduler_job_ok(job_id)
    except Exception:  # noqa: BLE001
        logger.warning("Could not clear the %s block", job_id, exc_info=True)


runtime_lifecycle = RuntimeLifecycle()


async def lifecycle_runtime_status(domain: str) -> dict[str, Any]:
    """``{owner, epoch, state, reason, recovery}`` for one lifecycle domain, from the row and the activation."""
    from src.db.engine import get_session
    from .ownership import inventory
    status = runtime_lifecycle.status(domain)
    row_payload: dict[str, Any] = {"owner": None, "owner_epoch": None, "owner_state": None}
    try:
        async with get_session() as db:
            row = (await inventory(db)).get(domain)
        if row is not None:
            row_payload = {"owner": row.owner_kind, "owner_epoch": row.epoch, "owner_state": row.state}
    except CompositionBindingError:
        row_payload["owner_state"] = "invalid"
    result = {**status, **row_payload}
    if domain == SCHEDULER_DOMAIN:
        from src.scheduler.background_inference import scheduler_job_blocks
        result["blocked_jobs"] = scheduler_job_blocks()
    return result


__all__ = ["ACTIVATED_EVENT", "Activation", "DISPATCH_JOBS", "LIFECYCLE_DOMAINS", "LifecycleBlocked",
           "LifecycleFenced", "QUIESCED_EVENT", "RETENTION_JOBS", "SCHEDULER_GATED_JOBS", "assert_tick_fence",
           "current_tick", "job_domain", "lifecycle_recovery", "lifecycle_runtime_status", "runtime_lifecycle"]
