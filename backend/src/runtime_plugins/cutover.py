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

Operators move a domain either way, live, through the authenticated runtime
composition API, at a higher epoch, with an audit event. Moving a domain to
Cordis requires its prerequisites to be Cordis-owned, and a rollback to legacy
is refused while a dependent domain is Cordis-owned. In production the backend
lifespan holds the exclusive workspace owner lease for its whole life
(``runtime_workspace_owner``), so no backup or restore can observe a
transition; a production process without that lease refuses every transition.
Dev and test processes take no lease, so a backup taken there while a
transition runs has no such protection.

In-flight legacy turns are not bound to the owner row, so a process-local gate
tracks them: a transition refuses while one is active, and no new legacy turn
starts while a transition runs.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import logging

from .ownership import (DOMAINS, PYTHON_HOSTED_DOMAINS, RECOVERY_EVENT, CompositionBindingError,
                        CompositionDependency, _checked_row, composition_writer, inventory,
                        legacy_composition_digest, transition_owner)

logger = logging.getLogger(__name__)
CUTOVER_EVENT = "runtime_composition_domain_cutover"
OPERATOR_EVENT = "runtime_composition_operator_transition"
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
    if domain not in DOMAINS or domain in known or domain in PYTHON_HOSTED_DOMAINS:
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
        # A count, not a set: overlapping transitions keep the gate closed until the last ends.
        self.closed: Counter[str] = Counter()

    def enter(self, domain: str) -> None:
        if self.closed[domain] > 0:
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
        self.closed[domain] += 1
        try:
            yield
        finally:
            self.closed[domain] -= 1
            if self.closed[domain] <= 0:
                del self.closed[domain]


legacy_turns = LegacyTurnGate()


# ---------------------------------------------------------------- transitions

def _require_owner_lease() -> None:
    """Production transitions run only while this process holds the workspace owner lease."""
    from config.settings import settings
    from src.workspace.production import runtime_workspace_owner_held
    if settings.deployment_environment.strip().lower() in {"prod", "production"} and not runtime_workspace_owner_held():
        logger.error("Refusing a Cordis owner transition: this production process does not hold the runtime "
                     "workspace owner lease, so a backup or restore could observe it")
        raise CompositionBindingError("composition_owner_lease_missing")


def _check_dependencies(rows, domain: str, target: str) -> None:
    """Cordis needs Cordis-owned prerequisites; legacy needs no Cordis-owned dependent."""
    entry = next(item for item in _MANIFEST if item.domain == domain)
    if target == "cordis" and any(rows.get(item) is None or rows[item].owner_kind != "cordis"
                                  for item in entry.prerequisites):
        raise CompositionBindingError("composition_prerequisite_not_cordis")
    if target == "legacy" and any(domain in item.prerequisites and rows.get(item.domain) is not None
                                  and rows[item.domain].owner_kind == "cordis" for item in _MANIFEST):
        raise CompositionBindingError("composition_dependent_cordis_owned")


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


async def _cut_over_domains(db) -> set[str]:
    from sqlalchemy import select
    from src.db.models import AuditEvent
    # Either the default cutover or any operator transition marks the domain: after
    # that, boot never moves it again (an operator rollback is sticky).
    rows = (await db.execute(select(AuditEvent.details_json).where(
        AuditEvent.event_type.in_((CUTOVER_EVENT, OPERATOR_EVENT))))).scalars()
    done = set()
    for raw in rows:
        try:
            done.add(json.loads(raw or "{}").get("runtime_domain"))
        except ValueError:
            continue
    return done


async def transition_domain(domain: str, target: str, *, host, actor: str, expected_epoch: int | None = None,
                            reason: str = "operator") -> dict:
    """Move one manifest domain to ``target`` at a higher epoch; returns the new row."""
    from src.db.engine import get_session
    from src.workspace.lifecycle import lifecycle_fence_marker
    if domain not in {entry.domain for entry in _MANIFEST} or target not in TARGETS:
        raise CompositionBindingError("composition_cutover_domain_invalid")
    _require_owner_lease()
    with legacy_turns.quiesced(domain), lifecycle_fence_marker():
        async with get_session() as db:
            async with composition_writer(db):
                rows = await inventory(db)
                row = rows.get(domain)
                if row is None:
                    raise CompositionBindingError("composition_dependency_missing")
                if expected_epoch is not None and row.epoch != expected_epoch:
                    raise CompositionBindingError("composition_owner_changed")
                _check_dependencies(rows, domain, target)
                prior = _checked_row(row).payload() | {"state": row.state}
                await _move(db, domain, target, host)
                current = (await inventory(db))[domain]
                details = {"runtime_domain": domain, "prior": prior, "target": target, "reason": reason,
                           "owner_kind": current.owner_kind, "epoch": current.epoch, "state": current.state}
                await _audit(db, OPERATOR_EVENT if actor != "boot" else CUTOVER_EVENT, details, actor=actor)
                return details


async def run_boot_cutover(host) -> list[dict]:
    """Forward-only default cutover for every registered domain, once per workspace.

    A marked Cordis domain is completed when it still awaits its boot proof, and a
    ready one whose stored digest differs from this boot's reviewed build is moved
    to that build's digest at a higher epoch, so status and turn bindings never
    name a stale composition.
    """
    from src.db.engine import get_session
    from src.workspace.lifecycle import lifecycle_fence_marker
    try:
        _require_owner_lease()
    except CompositionBindingError as exc:
        return [{"runtime_domain": entry.domain, "action": "deferred", "reason": exc.reason_code}
                for entry in _MANIFEST]
    receipts = []
    for entry in _MANIFEST:
        try:
            with legacy_turns.quiesced(entry.domain), lifecycle_fence_marker():
                async with get_session() as db:
                    async with composition_writer(db):
                        rows = await inventory(db)
                        row = rows.get(entry.domain)
                        if row is None:
                            continue
                        if any(rows.get(item) is None or rows[item].owner_kind != "cordis"
                               for item in entry.prerequisites):
                            continue
                        marked = entry.domain in await _cut_over_domains(db)
                        if marked:
                            # Sticky: a marked domain is only completed or refreshed, never re-cut from legacy.
                            if row.owner_kind != "cordis":
                                continue
                            built = _host_digest(host, entry.domain)
                            if row.state == "ready" and built in (None, row.composition_digest):
                                continue
                            action = "completed" if row.state != "ready" else "digest_refreshed"
                            await _move(db, entry.domain, "cordis", host)
                            current = (await inventory(db))[entry.domain]
                            receipts.append({"runtime_domain": entry.domain, "action": action,
                                             "state": current.state, "epoch": current.epoch})
                            continue
                        prior = _checked_row(row).payload() | {"state": row.state}
                        await _move(db, entry.domain, "cordis", host)
                        current = (await inventory(db))[entry.domain]
                        details = {"runtime_domain": entry.domain, "prior": prior, "target": "cordis",
                                   "reason": "default_cutover", "owner_kind": current.owner_kind,
                                   "epoch": current.epoch, "state": current.state}
                        await _audit(db, CUTOVER_EVENT, details, actor="runtime")
                        receipts.append({**details, "action": "cut_over"})
        except CompositionBindingError as exc:
            logger.warning("Cordis default cutover for %s deferred: %s", entry.domain, exc.reason_code)
            receipts.append({"runtime_domain": entry.domain, "action": "deferred", "reason": exc.reason_code})
    return receipts


__all__ = ["CUTOVER_EVENT", "OPERATOR_EVENT", "RECOVERY_EVENT", "CompositionDependency", "CutoverDomain",
           "cutover_manifest", "legacy_turns", "register_cutover_domain", "run_boot_cutover", "transition_domain"]
