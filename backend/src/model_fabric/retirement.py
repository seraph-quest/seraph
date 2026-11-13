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


def inactive_route_reason(runtime_path: str) -> str | None:
    """The stable, user-visible reason a canonical route has no active provider.

    ADR-035: with OpenRouter retired, a route is active only when a usable
    NEAR TEE slot serves it. Otherwise verticals report the same reason as
    Settings' ``inference_block_reason``: ``openrouter_retired`` when retained
    OpenRouter configuration exists, else ``no_active_provider``. Read-only and
    provider-free; returns None when a NEAR TEE slot serves the route (or in
    the explicit legacy test mode).
    """
    if not openrouter_retired():
        return None
    from .configuration import NEAR_TEE_PROVIDER_KIND, effective_workload_policy, read_model_fabric_configuration
    try:
        policy = effective_workload_policy(runtime_path)
    except Exception:
        # An unreadable policy is never a route (same as caller_context).
        return OPENROUTER_RETIRED_REASON
    if tuple(getattr(policy, "allowed_provider_kinds", ()) or ()) == (NEAR_TEE_PROVIDER_KIND,):
        return None
    try:
        retained = read_model_fabric_configuration().openrouter_setup is not None
    except Exception:
        retained = True
    return OPENROUTER_RETIRED_REASON if retained else NO_ACTIVE_PROVIDER_REASON


INACTIVE_ROUTE_REASONS = frozenset({OPENROUTER_RETIRED_REASON, NO_ACTIVE_PROVIDER_REASON})


def inactive_route_label(reason: str) -> str:
    """Operator text for an inactive-route block; carries the stable code verbatim."""
    label = OPENROUTER_RETIRED_LABEL if reason == OPENROUTER_RETIRED_REASON else NO_ACTIVE_PROVIDER_LABEL
    return f"{label} ({reason})"
