"""ADR-035 provider retirement policy.

OpenRouter is retired by policy: no route may make new OpenRouter contact.
Retained configuration, credentials, receipts, ledger rows and settlement
stay readable so contacted liabilities can still be reconciled.

Retirement is a code-level policy, not a setting. No environment variable,
settings file or API request can re-enable OpenRouter; reactivating any
provider requires a superseding ADR. The module flag exists only so isolated
regression tests that pin historical OpenRouter mechanics can opt back in
through an explicit fixture (``tests/conftest.py::openrouter_legacy_reenabled``).
"""

from __future__ import annotations

from contextlib import contextmanager
from urllib.parse import urlsplit

from .contracts import NoCompliantModelRouteError

OPENROUTER_RETIRED_REASON = "openrouter_retired"
NO_ACTIVE_PROVIDER_REASON = "no_active_provider"
OPENROUTER_RETIRED_LABEL = "Retired (ADR-035) — no new requests; existing costs can still be settled"

# Test-only override seam; production code never assigns this.
_OPENROUTER_RETIRED = True


class ProviderRetiredError(PermissionError):
    """A precontact denial: the selected provider is retired by policy.

    It stays a ``PermissionError`` so existing fail-closed callers keep
    treating it as unavailable authority. Chat and agent boundaries convert it
    to ``NoCompliantModelRouteError`` with the same stable reason through
    ``retired_as_route_error`` so operators see ``openrouter_retired``.
    """

    def __init__(self, code: str = OPENROUTER_RETIRED_REASON) -> None:
        super().__init__(code)
        self.code = code
        self.reason_codes = (code,)


class OpenRouterRetiredRouteError(NoCompliantModelRouteError):
    """The canonical no-contact route denial for retired OpenRouter.

    A ``NoCompliantModelRouteError`` so every existing route-denial handler
    applies; its ``str`` and ``reason_codes`` carry ``openrouter_retired`` so
    callers that only log ``str(exc)`` still record the stable reason.
    """

    code = OPENROUTER_RETIRED_REASON

    def __init__(self, reason_codes: tuple[str, ...] = (OPENROUTER_RETIRED_REASON,)) -> None:
        super().__init__(reason_codes)


@contextmanager
def retired_as_route_error():
    """Re-raise a retirement denial as the canonical no-contact route error."""
    try:
        yield
    except ProviderRetiredError as exc:
        raise OpenRouterRetiredRouteError((exc.code,)) from exc


def openrouter_retired() -> bool:
    return _OPENROUTER_RETIRED is not False


def provider_retirement_reason(provider_kind: object) -> str | None:
    """Return the stable retirement reason for a provider kind, if any."""
    if provider_kind == "openrouter" and openrouter_retired():
        return OPENROUTER_RETIRED_REASON
    return None


def is_openrouter_destination(*, api_base: object = None, model_id: object = None) -> bool:
    host = (urlsplit(str(api_base or "")).hostname or "").lower()
    if host == "openrouter.ai" or host.endswith(".openrouter.ai"):
        return True
    return str(model_id or "").strip().lower().startswith("openrouter/")


def assert_openrouter_contact_allowed(*, provider_kind: object = None, api_base: object = None,
        model_id: object = None) -> None:
    """Fail closed before any OpenRouter transport is built."""
    if not openrouter_retired():
        return
    if provider_kind == "openrouter" or is_openrouter_destination(api_base=api_base, model_id=model_id):
        raise ProviderRetiredError()


NO_ACTIVE_PROVIDER_LABEL = "No active inference provider — configure the NEAR TEE slot for this route in Settings"
PROVIDER_POLICY_REVOKED_REASON = "provider_policy_revoked"
NEAR_TEE_ROUTE_UNSUPPORTED_REASON = "near_tee_route_unsupported"
NEAR_TEE_ROUTE_UNSUPPORTED_LABEL = ("This capability is not yet available on NEAR TEE; tracked in #1159. "
    "Its model step still uses the retired OpenRouter adapter, so nothing was contacted")

# Local NEAR TEE slot reasons ``near_tee_slot_block_reason`` can return for a
# configured slot (status ready). With the fixed codes above they are the whole
# closed set a vertical route block can carry.
NEAR_TEE_SLOT_BLOCK_REASONS = frozenset({
    "near_tee_slot_unconfigured", "near_tee_slot_disabled", "near_tee_embedding_evidence_required",
    "near_tee_purpose_consent_required", "near_tee_credential_required", "inference_lane_required",
    "near_tee_request_reserve_exceeds_lane_ceiling",
})
ROUTE_BLOCK_REASONS = frozenset({OPENROUTER_RETIRED_REASON, NO_ACTIVE_PROVIDER_REASON,
    PROVIDER_POLICY_REVOKED_REASON, NEAR_TEE_ROUTE_UNSUPPORTED_REASON}) | NEAR_TEE_SLOT_BLOCK_REASONS


def inference_block_reason(configured, *, near_tee_ready: bool, near_tee_reason: str | None) -> str | None:
    """The one precedence for "why inference is blocked" (Settings and verticals).

    ADR-035, with OpenRouter retired: none when a NEAR TEE slot is ready; else
    a global egress revocation first, then the NEAR TEE slot's own reason
    (credential missing, receipt stale, ...), then ``openrouter_retired`` when
    retained OpenRouter configuration exists, else ``no_active_provider``.
    """
    if near_tee_ready or not openrouter_retired():
        return None
    if getattr(configured, "egress_revoked", False):
        return PROVIDER_POLICY_REVOKED_REASON
    if near_tee_reason:
        return near_tee_reason
    return OPENROUTER_RETIRED_REASON if getattr(configured, "openrouter_setup", None) is not None \
        else NO_ACTIVE_PROVIDER_REASON


def inactive_route_reason(runtime_path: str) -> str | None:
    """Why a canonical route has no active provider, or None when NEAR TEE serves it.

    Read-only and provider-free. The NEAR TEE reason is the route's own slot's
    local (no-contact) block reason; Settings passes its live slot status to
    the same ``inference_block_reason`` precedence.
    """
    if not openrouter_retired():
        return None
    from .caller_context import canonical_route_spec
    from .configuration import (NEAR_TEE_PROVIDER_KIND, effective_workload_policy, near_tee_slot,
        near_tee_slot_block_reason, read_model_fabric_configuration, route_slot_for_task_class)
    try:
        configured = read_model_fabric_configuration()
        policy = effective_workload_policy(runtime_path)
    except Exception:
        # An unreadable policy is never a route (same as caller_context).
        return OPENROUTER_RETIRED_REASON
    near_ready = tuple(getattr(policy, "allowed_provider_kinds", ()) or ()) == (NEAR_TEE_PROVIDER_KIND,)
    near_reason = None
    if not near_ready and configured.status == "ready" and configured.near_tee is not None:
        try:
            slot_name = route_slot_for_task_class(canonical_route_spec(runtime_path).task_class)
        except ValueError:
            slot_name = None
        if slot_name is not None and near_tee_slot(configured, slot_name) is not None:
            near_reason = near_tee_slot_block_reason(configured, slot_name)
    return inference_block_reason(configured, near_tee_ready=near_ready, near_tee_reason=near_reason)


def openrouter_only_route_block(runtime_path: str) -> str | None:
    """Route block for a vertical whose model step has no NEAR TEE adapter yet.

    The inactive-route reason when no provider serves the route; otherwise,
    when NEAR TEE serves it, ``near_tee_route_unsupported`` (#1159), because
    the vertical cannot use NEAR TEE and must not imply a provider outage.
    None only in the explicit legacy test mode.
    """
    if not openrouter_retired():
        return None
    return inactive_route_reason(runtime_path) or NEAR_TEE_ROUTE_UNSUPPORTED_REASON


def inactive_route_label(reason: str) -> str:
    """Operator text for a route block; carries the stable code verbatim."""
    label = {OPENROUTER_RETIRED_REASON: OPENROUTER_RETIRED_LABEL,
        NO_ACTIVE_PROVIDER_REASON: NO_ACTIVE_PROVIDER_LABEL,
        PROVIDER_POLICY_REVOKED_REASON: "Inference egress is revoked in Settings",
        NEAR_TEE_ROUTE_UNSUPPORTED_REASON: NEAR_TEE_ROUTE_UNSUPPORTED_LABEL,
    }.get(reason, "The NEAR TEE slot for this route is not ready — see Settings")
    return f"{label} ({reason})"
