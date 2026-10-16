"""Production owner cutover for Cordis runtime domains (#1019; reused by #1020).

An ordered manifest names each domain whose default owner becomes Cordis, with
its prerequisites. At boot, after the host has started, every registered domain
that has never been cut over is moved forward through the real fenced
``transition_owner`` sequence (ready -> draining -> blocked at epoch+1 with owner
``cordis`` -> ready on the running child's boot proof) in one writer
transaction, together with a once-marker audit event. A domain whose Cordis
provider is not built or not admitting stays ``blocked`` with the host's exact
fix; a later boot with a ready host completes it. Once marked, a domain is
never re-cut by boot: an operator rollback to legacy is sticky.

Operators move a domain either way through the authenticated runtime
composition API, which only persists an audited intent: the serving process
never transitions a live owner, because the maintenance fence's exclusive lock
conflicts with the backend's own owner lease. The next boot applies the intent
(at a higher epoch) before chat is served. Legacy turns are not bound to the
owner row, so a process-local gate also defers a domain's boot step while one is
active, and no new legacy turn starts while the step runs.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import logging

from .ownership import (DOMAINS, RECOVERY_EVENT, CompositionBindingError, CompositionDependency, _checked_row,
                        composition_writer, inventory, legacy_composition_digest, transition_owner)

logger = logging.getLogger(__name__)
CUTOVER_EVENT = "runtime_composition_domain_cutover"
OPERATOR_EVENT = "runtime_composition_operator_transition"
INTENT_EVENT = "runtime_composition_operator_intent"
TARGETS = frozenset({"legacy", "cordis"})


@dataclass(frozen=True)
class CutoverDomain:
    domain: str
    prerequisites: tuple[str, ...] = ()


_MANIFEST: list[CutoverDomain] = []


def register_cutover_domain(domain: str, *, prerequisites: tuple[str, ...] = ()) -> CutoverDomain:
    """Add one domain to the ordered default-cutover manifest.

    Prerequisites must already be registered, so manifest order is a valid
    cutover order. Registering the same domain twice is refused.
    """
    known = {entry.domain for entry in _MANIFEST}
    if domain not in DOMAINS or domain in known:
        raise CompositionBindingError("composition_cutover_domain_invalid")
    if any(item not in known for item in prerequisites):
        raise CompositionBindingError("composition_cutover_prerequisite_unknown")
    entry = CutoverDomain(domain, tuple(prerequisites))
    _MANIFEST.append(entry)
    return entry


def cutover_manifest() -> tuple[CutoverDomain, ...]:
    return tuple(_MANIFEST)


register_cutover_domain("seraph.agent-loop.v1")


# ---------------------------------------------------------------- legacy turn gate

class LegacyTurnGate:
    """Process-local registry of legacy turns per domain (single-process host)."""

    def __init__(self):
        self.active: dict[str, int] = {}
        self.closed: set[str] = set()

    def enter(self, domain: str) -> None:
        if domain in self.closed:
            raise CompositionBindingError("composition_transition_in_progress")
        self.active[domain] = self.active.get(domain, 0) + 1

    def leave(self, domain: str) -> None:
        if self.active.get(domain, 0) > 0:
            self.active[domain] -= 1

    @contextmanager
    def quiesced(self, domain: str):
        # Check and close in one synchronous step: no await between them.
        if self.active.get(domain, 0) > 0:
            raise CompositionBindingError("composition_not_quiescent")
        self.closed.add(domain)
        try:
            yield
        finally:
            self.closed.discard(domain)


legacy_turns = LegacyTurnGate()


# ---------------------------------------------------------------- transitions

def _pending_digest(domain: str) -> str:
    """Owner digest of a Cordis provider that is not yet built in this boot."""
    return hashlib.sha256(json.dumps({"schema": "seraph-cordis-pending.v1", "runtime_domain": domain},
                                     sort_keys=True).encode()).hexdigest()


def _host_digest(host, domain: str) -> str | None:
    reviewed = getattr(host, "reviewed", None)
    if host is None or not getattr(host, "admitting", False) or reviewed is None:
        return None
    return (reviewed.plugin_digests or {}).get(domain)


async def _step(db, domain, *, owner_kind, epoch, digest, state, host=None):
    row = (await inventory(db))[domain]
    expected = _checked_row(row)
    await transition_owner(db, runtime_domain=domain, expected=expected, expected_state=row.state, owner_kind=owner_kind,
                           epoch=epoch, composition_digest=digest, state=state, boot_host=host)


async def _move(db, domain: str, target: str, host) -> None:
    """The fenced forward sequence inside one composition writer transaction."""
    row = (await inventory(db))[domain]
    current = _checked_row(row)
    if target == "legacy":
        target_digest = legacy_composition_digest(domain)
    else:
        target_digest = _host_digest(host, domain) or _pending_digest(domain)
    if (current.owner_kind, current.composition_digest, row.state) == (target, target_digest, "ready"):
        return
    if row.state == "ready":
        await _step(db, domain, owner_kind=current.owner_kind, epoch=current.epoch,
                    digest=current.composition_digest, state="draining")
    if not ((current.owner_kind, current.composition_digest) == (target, target_digest) and row.state == "blocked"):
        await _step(db, domain, owner_kind=target, epoch=current.epoch + 1, digest=target_digest, state="blocked")
    # Ready only on a boot proof: the fixed legacy digest, or this boot's admitting child.
    if target == "legacy" or _host_digest(host, domain) == target_digest:
        row = (await inventory(db))[domain]
        await _step(db, domain, owner_kind=target, epoch=row.epoch, digest=target_digest, state="ready", host=host)


async def _audit(db, event_type: str, details: dict, *, actor: str) -> None:
    from src.db.models import AuditEvent
    db.add(AuditEvent(session_id=None, actor=actor, event_type=event_type,
                      summary="Runtime composition owner cutover", details_json=json.dumps(details, sort_keys=True)))
    await db.flush()


async def _events(db, *event_types: str) -> list[dict]:
    from sqlalchemy import select
    from src.db.models import AuditEvent
    rows = (await db.execute(select(AuditEvent.details_json).where(AuditEvent.event_type.in_(event_types))
                             .order_by(AuditEvent.id))).scalars()
    events = []
    for raw in rows:
        try:
            value = json.loads(raw or "{}")
        except ValueError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


async def _marked_domains(db) -> set[str]:
    # The default cutover marker, or any operator intent, marks a domain: after
    # that, the default step never moves it again (an operator rollback is sticky).
    return {event.get("runtime_domain") for event in await _events(db, CUTOVER_EVENT, INTENT_EVENT)}


async def _pending_intent(db, domain: str) -> dict | None:
    """The newest unresolved operator intent for one domain."""
    resolved = {event.get("intent_id") for event in await _events(db, OPERATOR_EVENT)}
    pending = [event for event in await _events(db, INTENT_EVENT)
               if event.get("runtime_domain") == domain and event.get("intent_id") not in resolved]
    return pending[-1] if pending else None


async def record_operator_intent(domain: str, target: str, *, expected_epoch: int, actor: str,
                                 reason: str = "operator") -> dict:
    """Persist an audited operator owner change; the next boot applies it.

    The serving process never transitions a live owner: the maintenance fence's
    exclusive lock conflicts with the backend's own owner lease, so changes run
    only in the boot cutover step, before chat is served.
    """
    import uuid
    from src.db.engine import get_session
    if domain not in {entry.domain for entry in _MANIFEST} or target not in TARGETS:
        raise CompositionBindingError("composition_cutover_domain_invalid")
    async with get_session() as db:
        row = (await inventory(db)).get(domain)
        if row is None:
            raise CompositionBindingError("composition_dependency_missing")
        if row.epoch != expected_epoch:
            raise CompositionBindingError("composition_owner_changed")
        intent = {"intent_id": uuid.uuid4().hex, "runtime_domain": domain, "target": target,
                  "expected_epoch": expected_epoch, "reason": reason, "actor": actor,
                  "prior": _checked_row(row).payload() | {"state": row.state}}
        await _audit(db, INTENT_EVENT, intent, actor=actor)
    return intent


async def pending_intents() -> dict[str, dict]:
    from src.db.engine import get_session
    async with get_session() as db:
        found = {}
        for entry in _MANIFEST:
            intent = await _pending_intent(db, entry.domain)
            if intent is not None:
                found[entry.domain] = intent
        return found


async def _boot_domain(db, entry: CutoverDomain, host) -> dict | None:
    rows = await inventory(db)
    row = rows.get(entry.domain)
    if row is None:
        from .ownership import DOMAIN_INTRODUCTIONS, introduce_domain
        if entry.domain not in DOMAIN_INTRODUCTIONS or not rows:
            return None
        # A domain introduced after this inventory existed (#1020) is seeded as
        # its legacy owner in this same writer, then cut over like any other.
        await introduce_domain(db, entry.domain)
        rows = await inventory(db)
        row = rows[entry.domain]
    intent = await _pending_intent(db, entry.domain)
    if intent is not None:
        if row.epoch != intent["expected_epoch"]:
            # The owner moved since the operator asked: the intent is refused, visibly.
            details = {"intent_id": intent["intent_id"], "runtime_domain": entry.domain, "outcome": "rejected",
                       "reason": "composition_owner_changed", "epoch": row.epoch}
            await _audit(db, OPERATOR_EVENT, details, actor="runtime")
            return {**details, "action": "intent_rejected"}
        await _move(db, entry.domain, intent["target"], host)
        current = (await inventory(db))[entry.domain]
        details = {"intent_id": intent["intent_id"], "runtime_domain": entry.domain, "outcome": "applied",
                   "target": intent["target"], "owner_kind": current.owner_kind, "epoch": current.epoch,
                   "state": current.state}
        # Recorded only after the transition succeeded, in the same writer.
        await _audit(db, OPERATOR_EVENT, details, actor="runtime")
        return {**details, "action": "intent_applied"}
    if any(rows.get(item) is None or rows[item].owner_kind != "cordis" for item in entry.prerequisites):
        return None
    if entry.domain in await _marked_domains(db):
        # A marked domain is only completed (cordis awaiting its boot proof), never re-cut.
        if row.owner_kind != "cordis" or row.state == "ready":
            return None
        await _move(db, entry.domain, "cordis", host)
        current = (await inventory(db))[entry.domain]
        return {"runtime_domain": entry.domain, "action": "completed", "state": current.state, "epoch": current.epoch}
    prior = _checked_row(row).payload() | {"state": row.state}
    await _move(db, entry.domain, "cordis", host)
    current = (await inventory(db))[entry.domain]
    details = {"runtime_domain": entry.domain, "prior": prior, "target": "cordis", "reason": "default_cutover",
               "owner_kind": current.owner_kind, "epoch": current.epoch, "state": current.state}
    await _audit(db, CUTOVER_EVENT, details, actor="runtime")
    return {**details, "action": "cut_over"}


async def run_boot_cutover(host) -> list[dict]:
    """Apply pending operator intents, then the forward-only default cutover, per domain.

    Idempotent from any row state. Each domain runs in one composition writer:
    the transitions and their marker commit together, or a failed precondition
    rolls everything back and the domain is retried at the next boot (blocked
    meanwhile, with its reason).
    """
    from src.db.engine import get_session
    from src.workspace.lifecycle import lifecycle_fence_marker
    receipts = []
    for entry in _MANIFEST:
        try:
            with legacy_turns.quiesced(entry.domain), lifecycle_fence_marker():
                async with get_session() as db:
                    async with composition_writer(db):
                        receipt = await _boot_domain(db, entry, host)
            if receipt is not None:
                receipts.append(receipt)
        except CompositionBindingError as exc:
            logger.warning("Cordis owner cutover for %s deferred: %s", entry.domain, exc.reason_code)
            receipts.append({"runtime_domain": entry.domain, "action": "deferred", "reason": exc.reason_code})
    return receipts


# Domain owners register their manifest entries at import; importing them here
# makes the manifest complete before any boot step, whatever imported first.
from . import guardian_turns  # noqa: E402,F401

__all__ = ["CUTOVER_EVENT", "INTENT_EVENT", "OPERATOR_EVENT", "RECOVERY_EVENT", "CompositionDependency", "CutoverDomain",
           "cutover_manifest", "legacy_turns", "pending_intents", "record_operator_intent", "register_cutover_domain",
           "run_boot_cutover"]
