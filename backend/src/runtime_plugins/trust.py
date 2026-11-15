"""The pinned ``seraph-trust`` services (ADR-036 §3) and the one pin predicate.

ADR-036 names the trust services Seraph pins. This module is the single code
copy of that table. ``is_pinned_domain`` answers "is this domain's owner row
frozen": every cutover entry point (``register_cutover_domain``,
``transition_domain``, the operator API) and the low-level fenced CAS
(``ownership.transition_owner``) consult it. A pinned row stays the legacy
Python owner with the fixed legacy digest. Its epoch may still advance as a
legacy fence (draining, blocked, ready), because that never moves ownership.

A pinned service whose pin is still a target (``owner_row_frozen=False``) is
named here so the composition dump shows it, but nothing refuses its moves yet.
"""
from __future__ import annotations

from dataclasses import dataclass
import json

TRUST_BUNDLE = "seraph-trust"
PIN_REFUSED_EVENT = "runtime_composition_pin_refused"
# The stable code #1024 already used for state, authority and audit.
PIN_REFUSED_REASON = "composition_owner_not_transferable"


@dataclass(frozen=True)
class TrustService:
    service: str
    identity: str
    # The owner row this service is pinned through; None for a responsibility
    # inside another identity or a service with no domain row yet.
    runtime_domain: str | None
    # True only when code refuses every owner change of that row today.
    owner_row_frozen: bool
    pinned_by: str

    def payload(self) -> dict:
        return {"service": self.service, "identity": self.identity, "runtime_domain": self.runtime_domain,
                "owner_row_frozen": self.owner_row_frozen, "pinned_by": self.pinned_by}


# ADR-036 §3, in table order.
TRUST_SERVICES: tuple[TrustService, ...] = (
    TrustService("canonical_store", "seraph.state.v1", "seraph.state.v1", True, "#1024"),
    TrustService("authority", "seraph.authority.v1", "seraph.authority.v1", True, "#1024"),
    TrustService("audit", "seraph.audit.v1", "seraph.audit.v1", True, "#1024"),
    TrustService("job_and_effect_ledger", "seraph.tasks.v1", "seraph.tasks.v1", True, "#1198"),
    TrustService("inference_policy", "seraph.inference.v1", "seraph.inference.v1", False, "target (ADR-036 §5)"),
    TrustService("execution_guard", "seraph.authority.v1", None, False, "target (part of authority)"),
    TrustService("operator_authentication", "seraph.authority.v1", None, False, "target (part of authority)"),
    TrustService("credential_vault", "seraph.vault.v1", "seraph.vault.v1", True, "#1199"),
)

PINNED_DOMAINS = frozenset(item.runtime_domain for item in TRUST_SERVICES
                           if item.owner_row_frozen and item.runtime_domain is not None)


def is_pinned_domain(runtime_domain: str) -> bool:
    """Whether this domain's owner row is frozen at the legacy Python owner."""
    return runtime_domain in PINNED_DOMAINS


# ADR-036 §5: the bundle each runtime domain ends up in (its 20, plus the
# vault row #1199 added under §3). Bundles are declared here only; no bundle
# loader or profile exists yet.
DOMAIN_BUNDLES = {
    "seraph.state.v1": TRUST_BUNDLE, "seraph.authority.v1": TRUST_BUNDLE, "seraph.audit.v1": TRUST_BUNDLE,
    "seraph.tasks.v1": TRUST_BUNDLE, "seraph.inference.v1": TRUST_BUNDLE, "seraph.vault.v1": TRUST_BUNDLE,
    "seraph.agent-presets.v1": "seraph-agent", "seraph.agent-loop.v1": "seraph-agent",
    "seraph.guardian.v1": "seraph-agent", "seraph.delegation.v1": "seraph-agent",
    "seraph.board-workers.v1": "seraph-agent", "seraph.capabilities.v1": "seraph-agent",
    "seraph.research.v1": "seraph-capabilities", "seraph.source-extraction.v1": "seraph-capabilities",
    "seraph.goals.v1": "seraph-domains", "seraph.memory.v1": "seraph-domains", "seraph.artifacts.v1": "seraph-domains",
    "seraph.conversation.v1": "seraph-domains", "seraph.connections.v1": "seraph-domains",
    "seraph.scheduler.v1": "seraph-lifecycle", "seraph.dispatch.v1": "seraph-lifecycle",
}


async def record_pin_refusal(runtime_domain: str, *, target: str, actor: str, path: str) -> None:
    """Audit one refused transfer of a pinned domain in its own transaction.

    Called before any composition writer opens, so the row survives the
    refusal. Best effort: an unwritable audit never turns the refusal into an
    allowed move.
    """
    import logging
    from src.db.engine import get_session
    from src.db.models import AuditEvent
    details = {"runtime_domain": runtime_domain, "target": str(target)[:32], "path": path,
               "reason_code": PIN_REFUSED_REASON}
    try:
        async with get_session() as db:
            db.add(AuditEvent(session_id=None, actor=str(actor)[:200], event_type=PIN_REFUSED_EVENT,
                              risk_level="high", summary=f"Refused owner transfer of pinned domain {runtime_domain}",
                              details_json=json.dumps(details, sort_keys=True)))
    except Exception:  # noqa: BLE001 - the refusal itself still stands
        logging.getLogger(__name__).warning("Could not audit the refused transfer of %s", runtime_domain,
                                            exc_info=True)


__all__ = ["DOMAIN_BUNDLES", "PINNED_DOMAINS", "PIN_REFUSED_EVENT", "PIN_REFUSED_REASON", "TRUST_BUNDLE",
           "TRUST_SERVICES", "TrustService", "is_pinned_domain", "record_pin_refusal"]
