"""Operator configuration, status, and bounded manual model-fabric canaries."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import re
import threading
import time
from uuid import uuid4

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from src.approval.runtime import get_current_trust_principal
from src.llm_runtime import provider_profiles
from src.model_fabric import (
    ModelCapability,
    ProviderProfile,
    candidate_from_profile,
    finalized_openai_compatible_body,
)
from src.model_fabric.caller_context import CANONICAL_ROUTE_SPECS
from src.model_fabric.configuration import (
    ModelFabricConfiguration,
    WorkloadPolicy,
    credential_ref_allowed,
    effective_workload_policy,
    read_model_fabric_configuration,
    write_model_fabric_configuration,
)
from src.model_fabric.contracts import MODEL_FABRIC_SCHEMA_VERSION, InferenceRequestContext, InferenceRequirements, InferenceWorkload, transport_endpoint
from src.model_fabric.probe import CapabilityProbeObservation, run_capability_probe
from src.model_fabric.proofs import proof_is_fresh
from src.model_fabric.receipts import sanitized_endpoint
from src.model_fabric.repository import model_fabric_repository
from src.model_fabric.runtime_status import latest_receipt_persistence, publish_receipt_persistence
from src.model_fabric.selector import profile_exclusion_reason, provider_family_exclusion_reason
from src.security.trust_contract import (
    AuthorityGrant,
    ContentOrigin,
    DigestSentinel,
    EgressClass,
    TrustProvenance,
    canonical_digest,
)


router = APIRouter()
_CANARY_VERSION = "seraph.manual-canary.v1"
_MANUAL_CANARY_LOCK = threading.Lock()
_MAX_CANARY_TIMEOUT_SECONDS = 180
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PROBE_CAPABILITIES = {
    *(capability.value for capability in ModelCapability),
    "latency_ms",
    "health",
}
_EMPIRICAL_PROOF_CAPABILITIES = frozenset(_PROBE_CAPABILITIES)
_ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2nGQAAAAASUVORK5CYII="
)


class ModelFabricProfileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    provider_kind: str
    model: str
    api_base: str
    secret_env: str = ""
    options: dict[str, object] | None = None
    capabilities: tuple[str, ...] = ()
    cost_tier: str | None = None
    latency_tier: str | None = None
    task_class: str | None = None
    task_classes: tuple[str, ...] = ()
    budget_class: str | None = None
    fallback_models: tuple[str, ...] = ()
    enabled: bool = True
    keyless: bool = False
    safety_notes: str = ""
    transport_adapter: str = "openai_compatible_chat"
    context_window_tokens: int | None = Field(default=None, ge=0)
    max_output_tokens: int | None = Field(default=None, ge=0)
    cost_microusd: int | None = Field(default=None, ge=0)
    cost_source: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$",
    )
    cost_source_updated_at: float | None = Field(default=None, ge=0)
    local_resource_ms: int | None = Field(default=None, ge=0)
    max_latency_ms: int | None = Field(default=None, ge=0)
    follow_redirects: bool = False
    schema_version: str = "seraph.model-fabric.v1"


class WorkloadPolicyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runtime_path: str
    egress_class: EgressClass = EgressClass.LOCAL_ONLY
    cloud_egress_acknowledged: bool = False
    allowed_profile_ids: tuple[str, ...] = ()
    allowed_provider_kinds: tuple[str, ...] = ()
    fallback_allowed: bool = False
    max_cost_microusd: int | None = Field(default=None, ge=0)


class ModelFabricConfigurationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profiles: tuple[ModelFabricProfileInput, ...] = ()
    workload_policies: tuple[WorkloadPolicyInput, ...] = ()


class CapabilityCanaryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str
    capability: str
    timeout_seconds: float = Field(default=10.0, ge=1.0, le=_MAX_CANARY_TIMEOUT_SECONDS)
    proof_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    transformation_digest: str = DigestSentinel.NO_TRANSFORMATION
    redaction_applied: bool = False


@router.get("/settings/model-fabric")
async def get_model_fabric_settings():
    return await model_fabric_settings_payload()


@router.put("/settings/model-fabric")
async def put_model_fabric_settings(body: ModelFabricConfigurationRequest, request: Request):
    if not _is_local_request(request):
        raise HTTPException(status_code=403, detail="Model-fabric settings require localhost access")
    configuration = ModelFabricConfiguration(
        profiles=tuple(ProviderProfile(**item.model_dump()) for item in body.profiles),
        workload_policies=tuple(WorkloadPolicy(**item.model_dump()) for item in body.workload_policies),
        status="ready",
    )
    try:
        write_model_fabric_configuration(configuration)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=503, detail="Model-fabric settings persistence failed") from exc
    return await model_fabric_settings_payload()


@router.post("/settings/model-fabric/canary")
async def run_model_fabric_canary(body: CapabilityCanaryRequest, request: Request):
    if not _is_local_request(request):
        raise HTTPException(status_code=403, detail="Model-fabric canaries require localhost access")
    if body.capability not in _PROBE_CAPABILITIES:
        raise HTTPException(status_code=422, detail="Unsupported model capability")
    principal = get_current_trust_principal()
    if principal is None:
        raise HTTPException(status_code=401, detail="Authenticated model-inference principal required")
    if AuthorityGrant.MODEL_INFERENCE not in principal.grants:
        raise HTTPException(status_code=403, detail="Principal lacks model-inference authority")
    if not principal.session_id and not principal.job_id:
        raise HTTPException(status_code=403, detail="Model-inference principal requires a bound session or job")
    profile = provider_profiles().get(body.profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Model-fabric profile not found")
    policy = effective_workload_policy("capability_probe")
    try:
        candidate = candidate_from_profile(profile)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="Model-fabric profile endpoint is not routable") from exc
    if policy.allowed_profile_ids and profile.id not in policy.allowed_profile_ids:
        raise HTTPException(status_code=403, detail="Capability probe profile is not allowed by operator policy")
    if policy.allowed_provider_kinds and profile.provider_kind not in policy.allowed_provider_kinds:
        raise HTTPException(status_code=403, detail="Capability probe provider is not allowed by operator policy")
    if candidate.endpoint_class.value == "remote" and policy.egress_class is EgressClass.LOCAL_ONLY:
        raise HTTPException(status_code=403, detail="Remote capability probe requires explicit cloud egress policy")
    if policy.egress_class is EgressClass.CLOUD_ALLOWED_REDACTED and (
        not body.redaction_applied
        or _SHA256.fullmatch(body.transformation_digest) is None
    ):
        raise HTTPException(status_code=403, detail="Redacted cloud probe requires exact transformation evidence")
    static_reasons = _static_profile_route_reasons(
        profile,
        candidate,
        capability=body.capability,
        policy=policy,
        timeout_seconds=body.timeout_seconds,
    )
    if static_reasons:
        raise HTTPException(status_code=422, detail={"non_routable_reasons": static_reasons})
    fixture = _canary_fixture(profile, body.capability)
    if not _MANUAL_CANARY_LOCK.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Another manual model canary is already running")
    try:
        return await _run_model_fabric_canary_locked(body, profile, policy, candidate, principal, fixture)
    finally:
        _MANUAL_CANARY_LOCK.release()


async def _run_model_fabric_canary_locked(body, profile, policy, candidate, principal, fixture):

    now = time.time()
    data_digest = canonical_digest(fixture["digest_payload"])
    context = InferenceRequestContext(
        principal=principal,
        session_id=principal.session_id,
        job_id=principal.job_id,
        provenance=(
            TrustProvenance(
                origin=ContentOrigin.SERAPH_CONTROL,
                source_id=_CANARY_VERSION,
                data_digest=data_digest,
                egress_class=policy.egress_class,
            ),
        ),
        data_digest=data_digest,
        egress_class=policy.egress_class,
        transformation_digest=body.transformation_digest,
        request_id=f"canary:{uuid4().hex}",
        runtime_path="capability_probe",
        workload=InferenceWorkload.CAPABILITY_PROBE,
        requirements=InferenceRequirements(
            capabilities=(body.capability,),
            context_tokens=128,
            output_tokens=64,
            max_cost_microusd=policy.max_cost_microusd,
            max_local_resource_ms=int(body.timeout_seconds * 1000),
            max_latency_ms=int(body.timeout_seconds * 1000),
            task_class=(profile.task_classes or ((profile.task_class,) if profile.task_class else ()))[0],
        ),
        deadline_at=now + body.timeout_seconds,
        allowed_profile_ids=(profile.id,),
        allowed_provider_kinds=(profile.provider_kind,),
        requested_profile_id=profile.id,
        redaction_applied=body.redaction_applied,
    )

    async def transport(candidate, _trust_request, requirements):
        return await _execute_canary_transport(
            candidate.profile,
            capability=body.capability,
            timeout_seconds=body.timeout_seconds,
            requirements=requirements,
            fixture=fixture,
        )

    result = await run_capability_probe(
        context=context,
        candidate=candidate,
        capability=body.capability,
        canary_version=_CANARY_VERSION,
        proof_ttl_seconds=body.proof_ttl_seconds,
        transport=transport,
        now=now,
    )
    publish_receipt_persistence(
        runtime_path="capability_probe",
        status=result.route_persistence.status,
        error_code=result.route_persistence.error_code,
        receipt_id=result.route_persistence.receipt_id,
    )
    authorizing = bool(
        result.outcome == "passed"
        and result.route_persistence.persisted
        and result.proof is not None
        and result.proof_persistence is not None
        and result.proof_persistence.persisted
    )
    operator_outcome = result.outcome if authorizing or result.outcome != "passed" else "degraded"
    operator_error = result.error_code
    if result.outcome == "passed" and not authorizing and operator_error is None:
        operator_error = (
            result.route_persistence.error_code
            or (result.proof_persistence.error_code if result.proof_persistence else None)
            or "canary_persistence_incomplete"
        )
    return {
        "profile_id": profile.id,
        "capability": body.capability,
        "outcome": operator_outcome,
        "error_code": operator_error,
        "proof": _proof_summary(result.proof),
        "receipt_persistence": result.route_persistence.status,
        "proof_persistence": result.proof_persistence.status if result.proof_persistence else "not_persisted",
    }


def _canary_fixture(profile: ProviderProfile, capability: str) -> dict[str, object]:
    """Finalize the exact transport body whose digest is bound into trust."""
    if profile.transport_adapter == "vlm_analyze_file":
        prompt = "Describe whether this fixed canary image is readable."
        if capability == ModelCapability.STRUCTURED_OUTPUT.value:
            prompt = (
                "Return the structured Seraph screenshot-analysis object for this image, "
                "including schema_version and summary fields."
            )
        form = {
            "model": profile.model,
            "prompt": prompt,
        }
        file_metadata = {
            "filename": "canary.png",
            "content_type": "image/png",
            "sha256": hashlib.sha256(_ONE_PIXEL_PNG).hexdigest(),
        }
        return {
            "form": form,
            "digest_payload": {
                "fields": form,
                "file": file_metadata,
            },
        }
    payload = _chat_canary_payload(profile.model, capability)
    return {
        "json": payload,
        "digest_payload": payload,
    }


def _static_profile_route_reasons(
    profile: ProviderProfile,
    candidate,
    *,
    capability: str,
    policy: WorkloadPolicy,
    timeout_seconds: float,
    now: float | None = None,
) -> list[str]:
    """Return non-proof guardrail failures that a bootstrap probe may not bypass."""
    checked_at = time.time() if now is None else now
    reasons: list[str] = []
    if profile.schema_version != MODEL_FABRIC_SCHEMA_VERSION:
        reasons.append("profile_schema_unknown")
    if reason := profile_exclusion_reason(profile):
        reasons.append(reason)
    if not credential_ref_allowed(profile):
        reasons.append("credential_ref_not_allowed")
    elif not profile.keyless and not profile.api_key:
        reasons.append("credential_missing")
    if not profile.enabled:
        reasons.append("profile_disabled")
    if capability in {item.value for item in ModelCapability} and capability not in profile.capabilities:
        reasons.append(f"capability_not_declared:{capability}")
    if profile.transport_adapter == "vlm_analyze_file" and capability not in {
        ModelCapability.VISION.value,
        ModelCapability.STRUCTURED_OUTPUT.value,
        "health",
        "latency_ms",
    }:
        reasons.append("adapter_capability_mismatch")
    if profile.transport_adapter == "openai_compatible_chat" and capability == ModelCapability.VISION.value:
        if ModelCapability.VISION.value not in profile.capabilities:
            reasons.append("adapter_capability_mismatch")
    if profile.context_window_tokens is None or profile.context_window_tokens < 128:
        reasons.append("profile_limit_unknown_or_insufficient:context_tokens")
    if profile.max_output_tokens is None or profile.max_output_tokens < 64:
        reasons.append("profile_limit_unknown_or_insufficient:output_tokens")
    timeout_ms = int(timeout_seconds * 1000)
    if profile.max_latency_ms is None or profile.max_latency_ms > timeout_ms:
        reasons.append("profile_limit_unknown_or_insufficient:latency_ms")
    if not (profile.task_classes or ((profile.task_class,) if profile.task_class else ())):
        reasons.append("profile_task_unknown")
    if candidate.endpoint_class.value == "remote":
        if policy.max_cost_microusd is None or profile.cost_microusd is None:
            reasons.append("profile_cost_unknown")
        elif profile.cost_microusd > policy.max_cost_microusd:
            reasons.append("profile_cost_noncompliant")
        if (
            not profile.cost_source
            or profile.cost_source_updated_at is None
            or profile.cost_source_updated_at < checked_at - 2_592_000
            or profile.cost_source_updated_at > checked_at
        ):
            reasons.append("profile_cost_source_missing_or_stale")
    elif profile.local_resource_ms is None or profile.local_resource_ms > timeout_ms:
        reasons.append("profile_local_resource_unknown_or_noncompliant")
    return list(dict.fromkeys(reasons))


async def model_fabric_settings_payload() -> dict[str, object]:
    configured = read_model_fabric_configuration()
    proof_metadata_degraded = False
    try:
        proofs = await _profile_proof_statuses()
        all_profiles = _operator_profile_statuses(proofs)
    except Exception:
        proof_metadata_degraded = True
        all_profiles = _operator_profile_statuses(())
    profiles = [item for item in all_profiles if item["model_fabric_eligible"]]
    excluded_profiles = [item for item in all_profiles if not item["model_fabric_eligible"]]
    return {
        "schema_version": "seraph.model-fabric.settings.v1",
        "status": "degraded" if configured.status == "degraded" or proof_metadata_degraded else configured.status,
        "error_code": configured.error_code or ("proof_metadata_unavailable" if proof_metadata_degraded else None),
        "configuration_status": configured.status,
        "updated_at": configured.updated_at,
        "profiles": profiles,
        "excluded_profiles": excluded_profiles,
        "persisted_profile_ids": [profile.id for profile in configured.profiles],
        "workload_policies": [_policy_payload(policy) for policy in configured.workload_policies],
        "defaults": {"egress_class": EgressClass.LOCAL_ONLY.value, "fallback_allowed": False},
        "canary_endpoint": "/api/settings/model-fabric/canary",
    }


async def model_fabric_runtime_status(active_profile: str | None) -> dict[str, object]:
    configured = read_model_fabric_configuration()
    runtime_paths = {}
    degraded = configured.status == "degraded"
    status_paths = (*CANONICAL_ROUTE_SPECS, "capability_probe")
    try:
        latest_routes = await model_fabric_repository.latest_routes_for_runtime_paths(status_paths)
        latest_successes = await model_fabric_repository.latest_routes_for_runtime_paths(
            status_paths,
            outcome="succeeded",
        )
    except Exception:
        latest_routes = {}
        latest_successes = {}
        degraded = True
    for runtime_path in status_paths:
        latest = latest_routes.get(runtime_path)
        latest_success = latest_successes.get(runtime_path)
        persistence_observation = latest_receipt_persistence(runtime_path)
        runtime_paths[runtime_path] = _workload_status(
            latest,
            latest_success,
            persistence_observation,
        )
        if persistence_observation is not None and persistence_observation.status == "degraded":
            degraded = True
    proof_statuses = []
    try:
        proof_statuses = await _profile_proof_statuses()
    except Exception:
        degraded = True
    workloads = {
        "interactive": runtime_paths["chat_agent"],
        "vision": runtime_paths["screenshot_image_analysis"],
        "capability_probe": runtime_paths["capability_probe"],
    }
    all_profiles = _operator_profile_statuses(proof_statuses)
    return {
        "status": "degraded" if degraded else "ready",
        "configuration_status": configured.status,
        "configuration_error": configured.error_code,
        "configured_chat_profile": active_profile,
        "profiles": [item for item in all_profiles if item["model_fabric_eligible"]],
        "excluded_profiles": [item for item in all_profiles if not item["model_fabric_eligible"]],
        "workload_policies": [_policy_payload(policy) for policy in configured.workload_policies],
        "proofs": proof_statuses,
        "topology": {
            "text": [
                path for path, spec in CANONICAL_ROUTE_SPECS.items()
                if spec.workload is not InferenceWorkload.VISION
            ],
            "vlm": [
                path for path, spec in CANONICAL_ROUTE_SPECS.items()
                if spec.workload is InferenceWorkload.VISION
            ],
        },
        "runtime_paths": runtime_paths,
        "workloads": workloads,
    }


async def _execute_canary_transport(
    profile,
    *,
    capability: str,
    timeout_seconds: float,
    requirements,
    fixture,
):
    endpoint = transport_endpoint(profile)
    headers = {"Content-Type": "application/json"}
    if profile.api_key:
        headers["Authorization"] = f"Bearer {profile.api_key}"
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=False) as client:
        if profile.transport_adapter == "vlm_analyze_file":
            response = await client.post(
                endpoint,
                data=fixture["form"],
                files={"file": ("canary.png", _ONE_PIXEL_PNG, "image/png")},
                headers={key: value for key, value in headers.items() if key != "Content-Type"},
            )
            response.raise_for_status()
            if not _validate_vlm_canary_response(response.json(), capability):
                return CapabilityProbeObservation(False, error_code="canary_shape_invalid")
        else:
            payload = fixture["json"]
            if capability == ModelCapability.STREAMING.value:
                async with client.stream("POST", endpoint, headers=headers, json=payload) as response:
                    response.raise_for_status()
                    observed = False
                    async for line in response.aiter_lines():
                        if _streaming_canary_delta(line):
                            observed = True
                            break
                if not observed:
                    return CapabilityProbeObservation(False, error_code="stream_empty")
            else:
                response = await client.post(endpoint, headers=headers, json=payload)
                response.raise_for_status()
                if not _validate_chat_canary_response(response.json(), capability):
                    return CapabilityProbeObservation(False, error_code="canary_shape_invalid")
    elapsed_ms = max(int((time.monotonic() - started) * 1000), 0)
    value = _proven_value(capability, elapsed_ms)
    if value is None:
        return CapabilityProbeObservation(False, error_code="proof_value_unknown")
    return CapabilityProbeObservation(True, proven_value=value)


def _chat_canary_payload(model: str, capability: str) -> dict[str, object]:
    content: object = "Reply with CANARY_OK only."
    messages: list[dict[str, object]] = [{"role": "user", "content": content}]
    additional_fields: dict[str, object] = {}
    streaming = False
    if capability == ModelCapability.STRUCTURED_OUTPUT.value:
        messages = [{"role": "user", "content": 'Return exactly {"ok":true}.'}]
        additional_fields["response_format"] = {"type": "json_object"}
    elif capability == ModelCapability.TOOL_USE.value:
        additional_fields["tools"] = [{"type": "function", "function": {"name": "canary_ok", "description": "Canary", "parameters": {"type": "object", "properties": {}}}}]
        additional_fields["tool_choice"] = {"type": "function", "function": {"name": "canary_ok"}}
    elif capability == ModelCapability.VISION.value:
        messages = [{"role": "user", "content": [{"type": "text", "text": "Describe this canary image."}, {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{base64.b64encode(_ONE_PIXEL_PNG).decode()}"}}]}]
    elif capability == ModelCapability.STREAMING.value:
        streaming = True
    return finalized_openai_compatible_body(
        model_id=model,
        messages=messages,
        max_tokens=64,
        stream=True if streaming else None,
        additional_fields=additional_fields,
    )


def _validate_chat_canary_response(payload: object, capability: str) -> bool:
    try:
        message = payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return False
    if capability == ModelCapability.TOOL_USE.value:
        return bool(message.get("tool_calls"))
    content = message.get("content")
    if capability == ModelCapability.STRUCTURED_OUTPUT.value:
        try:
            return isinstance(json.loads(content), dict)
        except (TypeError, json.JSONDecodeError):
            return False
    return bool(content)


def _streaming_canary_delta(line: str) -> bool:
    stripped = line.strip()
    if not stripped.startswith("data:"):
        return False
    data = stripped[5:].strip()
    if not data or data == "[DONE]":
        return False
    try:
        payload = json.loads(data)
        delta = payload["choices"][0]["delta"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        return False
    return isinstance(delta, dict) and isinstance(delta.get("content"), str) and bool(delta["content"])


def _validate_vlm_canary_response(payload: object, capability: str = ModelCapability.VISION.value) -> bool:
    if not isinstance(payload, dict):
        return False
    if capability == ModelCapability.STRUCTURED_OUTPUT.value:
        analysis = payload.get("analysis")
        return bool(
            isinstance(analysis, dict)
            and isinstance(analysis.get("schema_version"), str)
            and analysis["schema_version"].strip()
            and isinstance(analysis.get("summary"), str)
            and analysis["summary"].strip()
        )
    for key in ("analysis", "output", "content", "text"):
        value = payload.get(key)
        if isinstance(value, str) and bool(value.strip()):
            return True
    result = payload.get("result")
    if isinstance(result, str):
        return bool(result.strip())
    return _validate_vlm_canary_response(result, capability) if isinstance(result, dict) else False


def _proven_value(capability: str, elapsed_ms: int) -> int | str | None:
    values = {
        "latency_ms": elapsed_ms,
        "health": "healthy",
    }
    return values.get(capability, "verified" if capability in _PROBE_CAPABILITIES else None)


def _proof_summary(proof) -> dict[str, object] | None:
    if proof is None:
        return None
    return {
        "proof_hash": proof.proof_hash,
        "profile_id": proof.profile_id,
        "model": proof.model,
        "adapter": proof.adapter,
        "capability": proof.capability,
        "outcome": proof.outcome,
        "checked_at": datetime.fromtimestamp(proof.checked_at, tz=timezone.utc).isoformat(),
        "expires_at": datetime.fromtimestamp(proof.expires_at, tz=timezone.utc).isoformat(),
        "proven_value": proof.proven_value,
    }


def _policy_payload(policy: WorkloadPolicy) -> dict[str, object]:
    return {
        "runtime_path": policy.runtime_path,
        "egress_class": policy.egress_class.value,
        "cloud_egress_acknowledged": policy.cloud_egress_acknowledged,
        "allowed_profile_ids": list(policy.allowed_profile_ids),
        "allowed_provider_kinds": list(policy.allowed_provider_kinds),
        "fallback_allowed": policy.fallback_allowed,
        "max_cost_microusd": policy.max_cost_microusd,
    }


def _workload_status(latest, latest_success, persistence_observation=None) -> dict[str, object]:
    attempts = latest.attempts if latest is not None else ()
    persistence_status = (
        persistence_observation.status
        if persistence_observation is not None
        else "persisted" if latest is not None else "unknown"
    )
    persistence_error = persistence_observation.error_code if persistence_observation is not None else None
    return {
        "selected": _attempt_summary(attempts[0]) if attempts else None,
        "attempted": _attempt_summary(attempts[-1]) if attempts else None,
        "attempt_count": len(attempts),
        "last_outcome": latest.outcome if latest is not None else None,
        "fallback_used": latest.fallback_used if latest is not None else None,
        "fallback_reason_code": latest.fallback_reason_code if latest is not None else None,
        "degradation_codes": list(latest.degradation_codes) if latest is not None else [],
        "succeeded": {
            "profile_id": latest_success.actual_profile_id,
            "model": latest_success.actual_model,
            "adapter": latest_success.actual_adapter,
            "receipt_id": latest_success.receipt_id,
            "finished_at": latest_success.finished_at.isoformat(),
        } if latest_success is not None else None,
        "persistence": persistence_status,
        "persistence_error_code": persistence_error,
        "persistence_observed_at": (
            persistence_observation.observed_at if persistence_observation is not None else None
        ),
        "receipt_persistence_degraded": persistence_status == "degraded",
    }


async def _profile_proof_statuses() -> list[dict[str, object]]:
    statuses: list[dict[str, object]] = []
    now = time.time()
    profiles = tuple(provider_profiles().values())
    latest_proofs = await model_fabric_repository.latest_capability_proofs_for_profiles(
        tuple(profile.id for profile in profiles)
    )
    proof_index = {
        (
            proof.profile_contract_hash,
            proof.profile_id,
            proof.model,
            proof.endpoint,
            proof.adapter,
            proof.capability,
        ): proof
        for proof in latest_proofs
    }
    for profile in profiles:
        try:
            candidate = candidate_from_profile(profile)
        except ValueError:
            continue
        capabilities = sorted(
            (set(profile.capabilities) & _EMPIRICAL_PROOF_CAPABILITIES)
            | {"latency_ms", "health"}
        )
        for capability in capabilities:
            proof = proof_index.get(
                (
                    profile.contract_hash,
                    profile.id,
                    profile.model,
                    candidate.endpoint,
                    candidate.adapter,
                    capability,
                )
            )
            status = "missing"
            if proof is not None:
                status = (
                    "failed"
                    if proof.outcome != "passed"
                    else "fresh" if proof_is_fresh(proof, now=now) else "stale"
                )
            statuses.append(
                {
                    "profile_id": profile.id,
                    "capability": capability,
                    "status": status,
                    "outcome": proof.outcome if proof is not None else None,
                    "checked_at": (
                        datetime.fromtimestamp(proof.checked_at, tz=timezone.utc).isoformat()
                        if proof is not None else None
                    ),
                    "expires_at": (
                        datetime.fromtimestamp(proof.expires_at, tz=timezone.utc).isoformat()
                        if proof is not None else None
                    ),
                }
            )
    return statuses


def _operator_profile_statuses(proofs) -> list[dict[str, object]]:
    proof_index = {
        (item.get("profile_id"), item.get("capability")): item.get("status")
        for item in proofs
    }
    statuses: list[dict[str, object]] = []
    now = time.time()
    for profile in provider_profiles().values():
        provider_reason = provider_family_exclusion_reason(profile.provider_kind)
        schema_provider_eligible = (
            profile.schema_version == MODEL_FABRIC_SCHEMA_VERSION and provider_reason is None
        )
        non_routable: list[str] = []
        if reason := profile_exclusion_reason(profile):
            non_routable.append(reason)
        if not credential_ref_allowed(profile):
            non_routable.append("credential_ref_not_allowed")
            secret_configured = False
        else:
            secret_configured = profile.keyless or bool(profile.api_key)
            if not secret_configured:
                non_routable.append("credential_missing")
        if not profile.enabled:
            non_routable.append("profile_disabled")
        try:
            candidate = candidate_from_profile(profile)
            safe_api_base = sanitized_endpoint(profile.api_base)
        except ValueError:
            candidate = None
            safe_api_base = ""
            non_routable.append("endpoint_invalid")
        if not profile.capabilities:
            non_routable.append("capabilities_missing")
        if profile.context_window_tokens is None:
            non_routable.append("context_bound_missing")
        if profile.max_output_tokens is None:
            non_routable.append("output_bound_missing")
        if profile.max_latency_ms is None:
            non_routable.append("latency_bound_missing")
        if not (profile.task_classes or ((profile.task_class,) if profile.task_class else ())):
            non_routable.append("task_class_missing")
        if candidate is not None and candidate.endpoint_class.value == "remote":
            if profile.cost_microusd is None:
                non_routable.append("cost_bound_missing")
            if not profile.cost_source or profile.cost_source_updated_at is None:
                non_routable.append("cost_source_missing")
            elif profile.cost_source_updated_at < now - 2_592_000 or profile.cost_source_updated_at > now:
                non_routable.append("cost_source_stale")
        elif candidate is not None and profile.local_resource_ms is None:
            non_routable.append("local_resource_bound_missing")
        for capability in sorted(
            (set(profile.capabilities) & _EMPIRICAL_PROOF_CAPABILITIES)
            | {"health", "latency_ms"}
        ):
            proof_status = proof_index.get((profile.id, capability), "missing")
            if proof_status != "fresh":
                non_routable.append(f"proof_{proof_status}:{capability}")
        statuses.append(
            {
                "id": profile.id,
                "provider_kind": profile.provider_kind,
                "model": profile.model,
                "api_base": safe_api_base,
                "enabled": profile.enabled,
                "keyless": profile.keyless,
                "secret_configured": secret_configured,
                "missing_secret": not secret_configured,
                "capabilities": list(profile.capabilities),
                "transport_adapter": profile.transport_adapter,
                "cost_microusd": profile.cost_microusd,
                "cost_source": profile.cost_source,
                "cost_source_updated_at": profile.cost_source_updated_at,
                "model_fabric_eligible": schema_provider_eligible,
                "model_fabric_exclusion_reason": (
                    "profile_schema_unknown"
                    if profile.schema_version != MODEL_FABRIC_SCHEMA_VERSION
                    else provider_reason
                ),
                "routable": schema_provider_eligible and not non_routable,
                "non_routable_reasons": list(dict.fromkeys(non_routable)),
                "canary_timeout_seconds": _profile_canary_timeout_seconds(profile),
            }
        )
    return statuses


def _profile_canary_timeout_seconds(profile: ProviderProfile) -> int:
    """Return a bounded timeout that satisfies the profile's declared hard limits."""
    declared_ms = max(
        (value for value in (profile.max_latency_ms, profile.local_resource_ms) if value is not None),
        default=10_000,
    )
    return min(max((declared_ms + 999) // 1000, 1), _MAX_CANARY_TIMEOUT_SECONDS)


def _attempt_summary(attempt) -> dict[str, object]:
    return {
        "profile_id": attempt.profile_id,
        "model": attempt.model,
        "adapter": attempt.adapter,
        "destination_class": attempt.destination_class,
        "outcome": attempt.outcome,
        "latency_ms": attempt.latency_ms,
    }


def _is_local_request(request: Request) -> bool:
    host = request.client.host if request.client is not None else ""
    return host in {"127.0.0.1", "::1", "localhost", "testclient"}
