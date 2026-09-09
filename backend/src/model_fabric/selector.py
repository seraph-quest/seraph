"""Fail-closed, transport-free model route selector."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import ipaddress
import json
import time
from urllib.parse import urlparse
from uuid import uuid4

from config.settings import settings
from src.security.trust_contract import (
    AuthorityGrant,
    DestinationClass,
    DigestSentinel,
    TrustDestination,
    TrustOperation,
    TrustRequest,
    TrustResource,
    authority_scope_digest,
    evaluate_trust,
)

from .contracts import (
    EndpointClass,
    InferenceRequestContext,
    InferenceWorkload,
    ModelRouteCandidate,
    ModelRouteProof,
    ProviderProfile,
    RouteDecision,
    RouteRejection,
    transport_endpoint,
    has_inline_secret_options,
    SUPPORTED_TRANSPORT_ADAPTERS,
    credential_ref_allowed,
    OPENROUTER_API_BASE,
    OPENROUTER_PROVIDER_KIND,
    transport_model_for_provider,
)


SUPPORTED_PROVIDER_KINDS = frozenset({"local", "ollama", "openrouter", "openai", "openai_compatible"})
ACTIVE_PROVIDER_KINDS = frozenset({OPENROUTER_PROVIDER_KIND})
SYNTHETIC_PROBE_CAPABILITIES = frozenset({"health", "latency_ms"})

OPENROUTER_POLICY_REASON = "provider_kind_not_allowed"
OPENROUTER_ENDPOINT_REASON = "openrouter_endpoint_not_canonical"
OPENROUTER_CREDENTIAL_REASON = "openrouter_credential_required"
OPENROUTER_ADAPTER_REASON = "openrouter_transport_adapter_not_allowed"
OPENROUTER_EMBEDDING_ADAPTER_REASON = "openrouter_embedding_adapter_requires_embedding_capability"
OPENROUTER_EMBEDDING_CAPABILITY_REASON = "openrouter_embedding_capability_requires_embedding_adapter"
OPENROUTER_FALLBACK_REASON = "openrouter_fallbacks_forbidden"
OPENROUTER_PROVIDER_POLICY_REASON = "openrouter_provider_policy_missing"
OPENROUTER_UPSTREAMS_REASON = "openrouter_upstream_allowlist_missing"
OPENROUTER_PARAMETERS_REASON = "openrouter_parameters_required"
OPENROUTER_DATA_POLICY_REASON = "openrouter_data_policy_missing"
OPENROUTER_ZDR_REASON = "openrouter_zdr_required_for_vision"
OPENROUTER_MODEL_REASON = "openrouter_model_not_qualified"
OPENROUTER_TARGET_MODEL_REASON = "openrouter_model_not_allowed"
OPENROUTER_CREDENTIAL_OVERRIDE_REASON = "openrouter_credential_override_forbidden"


_TARGET_API_KEY_UNSET = object()


def provider_family_exclusion_reason(provider_kind: str) -> str | None:
    return None if provider_kind in SUPPORTED_PROVIDER_KINDS else "provider_family_excluded"


def _openrouter_provider_policy(options: dict[str, object] | None) -> dict[str, object] | None:
    if not isinstance(options, dict):
        return None
    policy = options.get("provider")
    return policy if isinstance(policy, dict) else None


def active_provider_exclusion_reason(profile: ProviderProfile) -> str | None:
    """Return the active-phase provider policy denial for a profile.

    ``SUPPORTED_PROVIDER_KINDS`` remains intentionally broader for legacy
    configuration/readback.  This function is the narrower active inference
    policy used by route selection and operator status.
    """
    family_reason = provider_family_exclusion_reason(profile.provider_kind)
    if family_reason is not None:
        return family_reason
    if profile.provider_kind not in ACTIVE_PROVIDER_KINDS:
        return OPENROUTER_POLICY_REASON
    if profile.api_base != OPENROUTER_API_BASE:
        return OPENROUTER_ENDPOINT_REASON
    if profile.follow_redirects:
        return "redirects_forbidden"
    if profile.transport_adapter not in {
        "openai_compatible_chat",
        "openai_compatible_embeddings",
    }:
        return OPENROUTER_ADAPTER_REASON
    if (
        profile.transport_adapter == "openai_compatible_embeddings"
        and "embedding" not in profile.capabilities
    ):
        return OPENROUTER_EMBEDDING_ADAPTER_REASON
    if (
        "embedding" in profile.capabilities
        and profile.transport_adapter != "openai_compatible_embeddings"
    ):
        return OPENROUTER_EMBEDDING_CAPABILITY_REASON
    if profile.secret_env != "OPENROUTER_API_KEY" or profile.keyless:
        return OPENROUTER_CREDENTIAL_REASON
    if profile.fallback_models:
        return OPENROUTER_FALLBACK_REASON
    configured_model = str(profile.routing_model or profile.model).strip()
    exact_model = transport_model_for_provider(OPENROUTER_PROVIDER_KIND, configured_model)
    model_provider, separator, model_name = exact_model.partition("/")
    if (
        not exact_model
        or not separator
        or not model_provider.strip()
        or not model_name.strip()
        or len(exact_model) > 256
        or any(character.isspace() or ord(character) < 32 for character in exact_model)
        or model_provider.lower() in {"local", "ollama", "openai-compatible", "openai_compatible"}
        or (
            profile.routing_model
            and profile.model != transport_model_for_provider(
                OPENROUTER_PROVIDER_KIND,
                profile.routing_model,
            )
        )
    ):
        return OPENROUTER_MODEL_REASON
    provider_policy = _openrouter_provider_policy(profile.options)
    if provider_policy is None:
        return OPENROUTER_PROVIDER_POLICY_REASON
    upstreams = provider_policy.get("only")
    if not isinstance(upstreams, (list, tuple)) or not upstreams or any(
        not isinstance(upstream, str) or not upstream.strip() for upstream in upstreams
    ):
        return OPENROUTER_UPSTREAMS_REASON
    if provider_policy.get("allow_fallbacks") is not False:
        return OPENROUTER_FALLBACK_REASON
    if provider_policy.get("require_parameters") is not True:
        return OPENROUTER_PARAMETERS_REASON
    if provider_policy.get("data_collection") != "deny":
        return OPENROUTER_DATA_POLICY_REASON
    if "vision" in profile.capabilities and provider_policy.get("zdr") is not True:
        return OPENROUTER_ZDR_REASON
    return None


def profile_exclusion_reason(profile: ProviderProfile) -> str | None:
    # Keep the broad provider contract readable for historical compatibility
    # tests and retained configuration.  The active phase narrows it only
    # when the operator has enabled OpenRouter-only mode; callers that need an
    # unconditional active check use ``active_provider_exclusion_reason``.
    if family_reason := provider_family_exclusion_reason(profile.provider_kind):
        return family_reason
    if bool(getattr(settings, "openrouter_provider_only", True)):
        if reason := active_provider_exclusion_reason(profile):
            return reason
    if has_inline_secret_options(profile.options or {}):
        return "inline_secret_option_forbidden"
    if not credential_ref_allowed(profile):
        return "credential_ref_not_allowed"
    if profile.transport_adapter == "litellm_chat":
        return "legacy_transport_not_governed"
    if profile.transport_adapter not in SUPPORTED_TRANSPORT_ADAPTERS:
        return "transport_adapter_excluded"
    return None


_PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
)


def classify_endpoint(endpoint: str) -> EndpointClass:
    parsed = urlparse(str(endpoint or ""))
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return EndpointClass.INVALID
    if hostname == "localhost":
        return EndpointClass.LOCAL
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return EndpointClass.REMOTE
    if address.is_unspecified:
        return EndpointClass.INVALID
    if address.is_loopback:
        return EndpointClass.LOCAL
    if address.is_link_local or any(address in network for network in _PRIVATE_NETWORKS):
        return EndpointClass.TRUSTED_LAN
    return EndpointClass.REMOTE


def candidate_from_profile(profile: ProviderProfile, *, source: str = "primary") -> ModelRouteCandidate:
    endpoint = transport_endpoint(profile)
    return ModelRouteCandidate(
        profile=profile,
        endpoint=endpoint,
        endpoint_class=classify_endpoint(endpoint),
        adapter=profile.transport_adapter,
        source=source,
    )


def _credential_destination_reason(candidate: ModelRouteCandidate) -> str | None:
    """Bind provider credentials to their provider family and destination class."""
    profile = candidate.profile
    secret_ref = profile.secret_env
    if not secret_ref:
        return None
    host = (urlparse(candidate.endpoint).hostname or "").lower()
    if secret_ref == "OPENAI_API_KEY":
        return None if profile.provider_kind == "openai" and host == "api.openai.com" else "credential_destination_mismatch"
    if secret_ref == "OPENROUTER_API_KEY":
        trusted_host = host == "openrouter.ai" or host.endswith(".openrouter.ai")
        return None if profile.provider_kind == "openrouter" and trusted_host else "credential_destination_mismatch"
    if secret_ref in {"LOCAL_LLM_API_KEY", "SERAPH_VLM_API_KEY"}:
        return (
            None
            if candidate.endpoint_class in {EndpointClass.LOCAL, EndpointClass.TRUSTED_LAN}
            else "credential_destination_mismatch"
        )
    if secret_ref == "LLM_API_KEY":
        return None if profile.provider_kind == "openai_compatible" else "credential_destination_mismatch"
    return "credential_destination_mismatch"


def preflight_candidate(
    context: InferenceRequestContext,
    candidate: ModelRouteCandidate,
    proofs: tuple[ModelRouteProof, ...],
    *,
    now: float | None = None,
    replayed_attempt_ids: tuple[str, ...] = (),
    replayed_replay_ids: tuple[str, ...] = (),
    target_model_id: str | None = None,
    target_api_key: str | None | object = _TARGET_API_KEY_UNSET,
) -> tuple[TrustRequest | None, str | None, str | None]:
    checked_at = time.time() if now is None else float(now)
    profile = candidate.profile
    # Canonical caller contexts carry an explicit OpenRouter provider allowlist.
    # Apply the narrow active-phase contract at that boundary even when a
    # stale compatibility flag is false.  Standalone selector compatibility
    # tests/readback can still exercise the broad provider contract without
    # creating an executable production route.
    active_context = "openrouter" in context.allowed_provider_kinds
    exclusion = active_provider_exclusion_reason if active_context else profile_exclusion_reason
    if reason := exclusion(profile):
        return None, None, reason
    if active_context:
        if target_model_id is not None:
            requested_model = transport_model_for_provider(
                profile.provider_kind,
                str(target_model_id).strip(),
            )
            if requested_model != profile.model:
                return None, None, OPENROUTER_TARGET_MODEL_REASON
        if target_api_key is not _TARGET_API_KEY_UNSET:
            configured_key = profile.api_key if not profile.keyless else ""
            supplied_key = "" if target_api_key is None else str(target_api_key)
            if supplied_key != configured_key:
                return None, None, OPENROUTER_CREDENTIAL_OVERRIDE_REASON
    if context.deadline_at <= checked_at:
        return None, None, "request_deadline_expired"
    if candidate.endpoint != transport_endpoint(profile) or candidate.endpoint_class is not classify_endpoint(candidate.endpoint):
        return None, None, "candidate_endpoint_mismatch"
    if candidate.adapter != profile.transport_adapter:
        return None, None, "candidate_adapter_mismatch"
    if context.allowed_profile_ids and profile.id not in context.allowed_profile_ids:
        return None, None, "profile_not_allowed"
    if context.allowed_provider_kinds and profile.provider_kind not in context.allowed_provider_kinds:
        return None, None, "provider_kind_not_allowed"
    if candidate.source != "primary" and not context.fallback_allowed:
        return None, None, "fallback_forbidden"
    if not profile.enabled:
        return None, None, "profile_disabled"
    if not credential_ref_allowed(profile):
        return None, None, "credential_ref_not_allowed"
    if credential_reason := _credential_destination_reason(candidate):
        return None, None, credential_reason
    if not profile.keyless and not profile.api_key:
        return None, None, "credential_missing"
    if profile.schema_version != "seraph.model-fabric.v1":
        return None, None, "profile_schema_unknown"
    if candidate.endpoint_class is EndpointClass.INVALID:
        return None, None, "endpoint_invalid"
    if profile.follow_redirects:
        return None, None, "redirects_forbidden"
    if context.workload is InferenceWorkload.CAPABILITY_PROBE:
        if not context.requested_profile_id or profile.id != context.requested_profile_id or candidate.source != "primary":
            return None, None, "probe_route_not_exact"
        proof_reason = _proof_reason(context, candidate, (), now=checked_at, require_proofs=False)
        if proof_reason is not None:
            return None, None, proof_reason
    else:
        proof_reason = _proof_reason(context, candidate, proofs, now=checked_at)
        if proof_reason is not None:
            return None, None, proof_reason

    destination = TrustDestination(
        destination_id=f"model:{hashlib.sha256(candidate.endpoint.encode()).hexdigest()}",
        destination_class=(
            DestinationClass.LOCAL_RUNTIME
            if candidate.endpoint_class is EndpointClass.LOCAL
            else DestinationClass.TRUSTED_LAN_RUNTIME
            if candidate.endpoint_class is EndpointClass.TRUSTED_LAN
            else DestinationClass.REMOTE_PROVIDER
        ),
        endpoint=candidate.endpoint,
    )
    resource = TrustResource("model_endpoint", destination.destination_id, DigestSentinel.NO_OBJECT)
    token = uuid4().hex
    request = TrustRequest(
        principal=context.principal,
        provenance=context.provenance,
        destination=destination,
        operation=TrustOperation.MODEL_INFERENCE,
        required_grant=AuthorityGrant.MODEL_INFERENCE,
        capability_id=f"model:{profile.id}",
        capability_version=profile.contract_hash,
        data_digest=context.data_digest,
        secret_scope_digest=DigestSentinel.NO_SECRET_SCOPE,
        resource_limits_digest=_digest(context.requirements),
        transformation_digest=context.transformation_digest,
        authority_scope_digest="",
        resource=resource,
        session_id=context.session_id,
        job_id=context.job_id,
        request_id=context.request_id,
        attempt_id=f"attempt:{token}",
        replay_id=f"replay:{token}",
        decision_expires_at=checked_at + 60.0,
        egress_class=context.egress_class,
        redaction_applied=context.redaction_applied,
    )
    request = replace(
        request,
        authority_scope_digest=authority_scope_digest(
            required_grant=request.required_grant,
            capability_id=request.capability_id,
            destination=request.destination,
            resource=request.resource,
        ),
    )
    decision = evaluate_trust(
        request,
        now=checked_at,
        replayed_attempt_ids=replayed_attempt_ids,
        replayed_replay_ids=replayed_replay_ids,
    )
    return request, decision.decision_id, None if decision.allowed else decision.reason_code


def select_route(
    context: InferenceRequestContext,
    candidates: tuple[ModelRouteCandidate, ...],
    proofs: tuple[ModelRouteProof, ...],
    *,
    now: float | None = None,
    target_model_id: str | None = None,
    target_api_key: str | None | object = _TARGET_API_KEY_UNSET,
) -> RouteDecision:
    rejections: list[RouteRejection] = []
    for candidate in candidates:
        request, decision_id, reason = preflight_candidate(
            context,
            candidate,
            proofs,
            now=now,
            target_model_id=target_model_id,
            target_api_key=target_api_key,
        )
        if reason is None and request is not None:
            from src.security.trust_contract import trust_request_digest
            from .proofs import proof_is_fresh

            required_proofs = set(context.requirements.capabilities) | {"health", "latency_ms"}
            used_proof_hashes = tuple(
                proof.proof_hash
                for proof in proofs
                if proof.capability in required_proofs
                and proof.profile_contract_hash == candidate.profile.contract_hash
                and proof.profile_id == candidate.profile.id
                and proof.model == candidate.profile.model
                and proof.endpoint == candidate.endpoint
                and proof_is_fresh(proof, now=now)
            )

            return RouteDecision(
                candidate,
                trust_request_digest(request),
                decision_id,
                tuple(rejections),
                request.attempt_id,
                request.replay_id,
                f"route:{hashlib.sha256((context.request_id + decision_id).encode()).hexdigest()}",
                used_proof_hashes,
            )
        rejections.append(RouteRejection(candidate.profile.id, reason or "route_noncompliant"))
        if context.workload is InferenceWorkload.CAPABILITY_PROBE:
            break
    rejection_digest = hashlib.sha256(
        json.dumps(
            [(item.profile_id, item.reason_code) for item in rejections],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return RouteDecision(
        None,
        None,
        None,
        tuple(rejections),
        None,
        None,
        f"route:{hashlib.sha256((context.request_id + rejection_digest).encode()).hexdigest()}",
        (),
    )


def _proof_reason(
    context: InferenceRequestContext,
    candidate: ModelRouteCandidate,
    proofs: tuple[ModelRouteProof, ...],
    *,
    now: float,
    require_proofs: bool = True,
) -> str | None:
    undeclared = {
        capability
        for capability in context.requirements.capabilities
        if capability not in candidate.profile.capabilities
        and not (
            context.workload is InferenceWorkload.CAPABILITY_PROBE
            and capability in SYNTHETIC_PROBE_CAPABILITIES
        )
    }
    if undeclared:
        return "capability_not_declared"
    if candidate.profile.context_window_tokens is None or candidate.profile.context_window_tokens < context.requirements.context_tokens:
        return "profile_limit_unknown_or_insufficient:context_tokens"
    if candidate.profile.max_output_tokens is None or candidate.profile.max_output_tokens < context.requirements.output_tokens:
        return "profile_limit_unknown_or_insufficient:output_tokens"
    if candidate.profile.max_latency_ms is None or candidate.profile.max_latency_ms > context.requirements.max_latency_ms:
        return "profile_limit_unknown_or_insufficient:latency_ms"
    declared_tasks = candidate.profile.task_classes or ((candidate.profile.task_class,) if candidate.profile.task_class else ())
    if context.requirements.task_class not in declared_tasks:
        return "profile_task_unknown_or_mismatch"
    required = set(context.requirements.capabilities)
    required.update({"latency_ms", "health"})
    if candidate.endpoint_class is EndpointClass.REMOTE:
        if context.requirements.max_cost_microusd is None or candidate.profile.cost_microusd is None:
            return "profile_cost_unknown"
        if (
            not candidate.profile.cost_source
            or candidate.profile.cost_source_updated_at is None
            or candidate.profile.cost_source_updated_at < now - 2_592_000
            or candidate.profile.cost_source_updated_at > now
        ):
            return "profile_cost_source_missing_or_stale"
        if candidate.profile.cost_microusd > context.requirements.max_cost_microusd:
            return "profile_cost_noncompliant"
    else:
        if context.requirements.max_local_resource_ms is None or candidate.profile.local_resource_ms is None:
            return "profile_local_resource_unknown"
        if candidate.profile.local_resource_ms > context.requirements.max_local_resource_ms:
            return "profile_local_resource_noncompliant"
    if not require_proofs:
        return None
    exact: dict[str, ModelRouteProof] = {}
    from .proofs import proof_is_fresh, validate_model_route_proof

    for proof in proofs:
        try:
            validate_model_route_proof(proof)
        except ValueError:
            continue
        if (
            proof.profile_schema_version == candidate.profile.schema_version
            and proof.profile_contract_hash == candidate.profile.contract_hash
            and proof.profile_id == candidate.profile.id
            and proof.model == candidate.profile.model
            and proof.endpoint == candidate.endpoint
            and proof.endpoint_class is candidate.endpoint_class
            and proof.adapter == candidate.adapter
            and proof_is_fresh(proof, now=now)
        ):
            exact[proof.capability] = proof
    if missing := sorted(required - exact.keys()):
        return f"proof_missing:{missing[0]}"
    values = {
        "latency_ms": (context.requirements.max_latency_ms, lambda actual, limit: actual <= limit),
        "health": ("healthy", lambda actual, need: actual == need),
    }
    for capability, (required_value, predicate) in values.items():
        actual = exact[capability].proven_value
        if actual is None or not predicate(actual, required_value):
            return f"requirement_noncompliant:{capability}"
    return None


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
