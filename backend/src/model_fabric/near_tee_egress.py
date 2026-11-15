"""Consented exact-body binding for NEAR TEE code egress (#1184, ADR-035).

Repository repair (the iterative ``repo_repair_source`` callback and the
single-shot ``engineering.repo-repair.v1`` proposal) consents to one exact
NEAR TEE request: the body digest and the route digest of the NEAR target
(fixed base, slot profile and its complete contract hash, canonical model,
``x-no-aliasing``). The owner binds that pair for the duration of its model
call with ``bind_near_tee_egress``; every NEAR TEE text call made under the
binding is checked twice:

* in ``prepare_near_tee_call``, before any admission or reservation, and
* in ``near_tee_precontact_reason``, under the contact writer immediately
  before bytes leave.

Any other body, target or request shape (stream, tools, a missing
``x-no-aliasing``, another runtime path) is refused with the binding's
stable code; nothing is reserved or sent. Calls made without a binding are
unaffected. This module belongs to the model fabric and imports no workflow
owner; owners depend on it, never the reverse.

No import-time side effects.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import re
from typing import Any, Iterator

from .contracts import NEAR_TEE_PROVIDER_KIND

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SEAL = object()


@dataclass(frozen=True)
class NearTeeEgressBinding:
    """The one consented NEAR TEE request an owner may send."""

    runtime_path: str
    body_digest: str
    route_digest: str
    refusal_code: str
    _seal: object = field(default=None, repr=False, compare=False)


_binding: ContextVar[NearTeeEgressBinding | None] = ContextVar("near_tee_egress_binding", default=None)


def near_tee_target(*, profile_id: str, profile_contract_hash: str, model_id: str) -> dict[str, Any]:
    """The NEAR TEE target a consented route digest binds.

    The fixed NEAR base, the slot profile and its complete contract hash
    (model and slot settings, cost bound included), the canonical model id
    and ``x-no-aliasing``. A changed slot is a changed route.
    """
    from .configuration import NEAR_TEE_API_BASE
    return {"source": "primary", "api_base": NEAR_TEE_API_BASE, "profile": profile_id, "model_id": model_id,
        "options": {"provider_kind": NEAR_TEE_PROVIDER_KIND, "profile_contract_hash": profile_contract_hash,
            "x-no-aliasing": True}}


def transport_route_digest(target: dict[str, Any], runtime_path: str) -> str:
    """The digest of one exact primary transport target (every field binds)."""
    from src.security.trust_contract import canonical_digest
    return canonical_digest({"runtime_path": runtime_path, "source": target.get("source"),
        "endpoint": str(target.get("api_base") or "").rstrip("/"),
        "profile": target.get("profile"), "model_id": target.get("model_id"),
        "options": target.get("options")})


def is_near_tee_target(target: object) -> bool:
    from .configuration import NEAR_TEE_API_BASE
    options = target.get("options") if isinstance(target, dict) else None
    return (isinstance(options, dict) and target.get("source") == "primary"
        and str(target.get("api_base") or "").rstrip("/") == NEAR_TEE_API_BASE
        and options.get("provider_kind") == NEAR_TEE_PROVIDER_KIND and options.get("x-no-aliasing") is True)


@contextmanager
def bind_near_tee_egress(*, runtime_path: str, body_digest: str, route_digest: str,
                         refusal_code: str) -> Iterator[NearTeeEgressBinding]:
    """Allow only this exact consented NEAR TEE request inside the block."""
    if (not isinstance(runtime_path, str) or not runtime_path or not isinstance(refusal_code, str)
            or not refusal_code or not isinstance(body_digest, str) or not _SHA256.fullmatch(body_digest)
            or not isinstance(route_digest, str) or not _SHA256.fullmatch(route_digest)):
        raise PermissionError(refusal_code or "near_tee_egress_binding_invalid")
    if _binding.get() is not None:
        # One consented request per model call; bindings never nest or widen.
        raise PermissionError(refusal_code)
    binding = NearTeeEgressBinding(runtime_path=runtime_path, body_digest=body_digest,
        route_digest=route_digest, refusal_code=refusal_code, _seal=_SEAL)
    token = _binding.set(binding)
    try:
        yield binding
    finally:
        _binding.reset(token)


def current_near_tee_egress_binding() -> NearTeeEgressBinding | None:
    binding = _binding.get()
    return binding if binding is not None and binding._seal is _SEAL else None


def near_tee_egress_reason(call: Any) -> str | None:
    """Why a prepared NEAR TEE call is not the bound consented request, or None.

    None without a binding. Pure and local: digests only, no contact.
    """
    binding = current_near_tee_egress_binding()
    if binding is None:
        return None
    from src.security.trust_contract import canonical_digest
    from .near_client import NO_ALIASING_HEADER
    profile = getattr(getattr(getattr(call, "decision", None), "selected", None), "profile", None)
    body = getattr(call, "body", None)
    headers = {str(key).lower(): value for key, value in dict(getattr(call, "headers", {}) or {}).items()}
    if (profile is None or not isinstance(body, dict) or getattr(call, "slot_name", None) != "text"
            or getattr(call, "stream", True) is not False or tuple(getattr(call, "tool_names", ()) or ()) != ()
            or headers.get(NO_ALIASING_HEADER.lower()) != "true"
            or getattr(getattr(call, "context", None), "runtime_path", None) != binding.runtime_path
            or getattr(profile, "model", None) != body.get("model")):
        return binding.refusal_code
    target = near_tee_target(profile_id=profile.id, profile_contract_hash=profile.contract_hash,
        model_id=str(body.get("model")))
    if (canonical_digest(body) != binding.body_digest
            or transport_route_digest(target, binding.runtime_path) != binding.route_digest):
        return binding.refusal_code
    return None
