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


@contextmanager
def retired_as_route_error():
    """Re-raise a retirement denial as the canonical no-contact route error."""
    from .contracts import NoCompliantModelRouteError
    try:
        yield
    except ProviderRetiredError as exc:
        raise NoCompliantModelRouteError((exc.code,)) from exc


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
