"""Operator configuration, status, and bounded manual model-fabric canaries."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import threading
import time
from uuid import uuid4

import httpx
from config.settings import settings
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

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
    OPENROUTER_ENV_CREDENTIAL_REF,
    OPENROUTER_SETUP_SCHEMA_VERSION,
    OPENROUTER_VAULT_CREDENTIAL_REF,
    OpenRouterSetup,
    WorkloadPolicy,
    credential_ref_allowed,
    effective_workload_policy,
    normalize_openrouter_model_id,
    openrouter_policy_for_setup,
    openrouter_profile_for_setup,
    hydrate_openrouter_credential,
    _openrouter_setup_payload,
    read_model_fabric_configuration,
    validate_active_model_fabric_configuration,
    write_model_fabric_configuration,
)
from src.model_fabric.contracts import (
    MODEL_FABRIC_SCHEMA_VERSION,
    OPENROUTER_API_BASE,
    OPENROUTER_PROVIDER_KIND,
    InferenceRequestContext,
    InferenceRequirements,
    InferenceWorkload,
    transport_endpoint,
)
from src.model_fabric.probe import CapabilityProbeObservation, run_capability_probe
from src.model_fabric.proofs import proof_is_fresh
from src.model_fabric.receipts import sanitized_endpoint
from src.model_fabric.repository import model_fabric_repository
from src.model_fabric.runtime_status import latest_receipt_persistence, publish_receipt_persistence
from src.model_fabric.selector import (
    active_provider_exclusion_reason,
)
from src.vault.repository import vault_repository
from src.model_fabric.remote_inference_admission import (
    remote_inference_admission_broker,
)
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


class OpenRouterSetupInput(BaseModel):
    """Write-only operator setup fields for the fixed OpenRouter route."""

    model_config = ConfigDict(extra="forbid")

    profile_id: str = "openrouter"
    provider_kind: str = OPENROUTER_PROVIDER_KIND
    api_base: str = OPENROUTER_API_BASE
    model_ids: tuple[str, ...] = ()
    # ``models`` and ``model_id`` keep the API forgiving for clients that use
    # singular or concise naming, while the persisted contract is always
    # ``model_ids``.
    models: tuple[str, ...] | None = None
    model_id: str | None = None
    capabilities: tuple[str, ...] = ()
    modalities: tuple[str, ...] | None = None
    temperature: float = Field(default=0.7, ge=0, le=2)
    max_output_tokens: int = Field(default=4096, ge=1, le=131_072)
    timeout_seconds: float = Field(default=120.0, ge=1, le=120)
    timeout: float | None = Field(default=None, ge=1, le=120)
    allowed_upstreams: tuple[str, ...] = ()
    allow_fallbacks: bool = False
    # Keep the policy name used by WorkloadPolicy available at the request
    # edge; the canonical setup stores only allow_fallbacks.
    fallback_allowed: bool | None = None
    require_parameters: bool = True
    # These are intentionally required at the request boundary.  Persisted
    # dataclasses keep safe defaults for backwards-compatible readback, but a
    # new operator save must explicitly acknowledge the deny policy.
    data_collection: str = Field(..., min_length=1)
    data_retention_policy: str = Field(..., min_length=1)
    zero_data_retention: bool = False
    egress_class: EgressClass = EgressClass.LOCAL_ONLY
    cloud_egress: EgressClass | None = None
    cloud_egress_acknowledged: bool = False
    spend_ceiling_microusd: int | None = Field(default=None, ge=1, le=1_000_000_000)
    max_cost_microusd: int | None = Field(default=None, ge=1, le=1_000_000_000)
    max_queued: int = Field(default=64, ge=1, le=64)
    max_queue_size: int | None = Field(default=None, ge=1, le=64)
    max_inflight: int = Field(default=1, ge=1, le=1)
    max_outstanding_per_owner: int = Field(default=16, ge=1, le=16)
    max_retries: int = Field(default=2, ge=0, le=2)
    credential_ref: str | None = None
    # SecretStr prevents accidental repr/model dump exposure. The value is
    # consumed only by the trusted PUT handler and never enters a response.
    api_key: SecretStr | None = Field(default=None, repr=False)


class ModelFabricConfigurationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profiles: tuple[ModelFabricProfileInput, ...] = ()
    workload_policies: tuple[WorkloadPolicyInput, ...] = ()
    openrouter: OpenRouterSetupInput | None = None
    openrouter_setup: OpenRouterSetupInput | None = None

    @model_validator(mode="after")
    def one_openrouter_setup_field(self):
        if self.openrouter is not None and self.openrouter_setup is not None:
            raise ValueError("send only one OpenRouter setup object")
        return self


class CapabilityCanaryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str
    capability: str
    timeout_seconds: float = Field(default=10.0, ge=1.0, le=_MAX_CANARY_TIMEOUT_SECONDS)
    proof_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    transformation_digest: str = DigestSentinel.NO_TRANSFORMATION
    redaction_applied: bool = False


def _openrouter_setup_from_input(
    body: OpenRouterSetupInput,
    *,
    existing: OpenRouterSetup | None = None,
) -> OpenRouterSetup:
    """Convert request aliases into the canonical persisted setup shape."""
    if body.provider_kind != OPENROUTER_PROVIDER_KIND:
        raise ValueError("OpenRouter setup accepts only the openrouter provider")
    if body.api_base.rstrip("/") != OPENROUTER_API_BASE:
        raise ValueError("OpenRouter endpoint is fixed to https://openrouter.ai/api/v1")
    raw_models = tuple(body.model_ids)
    if body.models is not None:
        if raw_models and tuple(body.models) != raw_models:
            raise ValueError("OpenRouter model_ids and models disagree")
        raw_models = tuple(body.models)
    if body.model_id is not None:
        if raw_models and raw_models != (body.model_id,):
            raise ValueError("OpenRouter model_id and model_ids disagree")
        raw_models = (body.model_id,)
    model_ids = tuple(normalize_openrouter_model_id(value) for value in raw_models)
    capabilities = tuple(body.modalities if body.modalities is not None else body.capabilities)
    if body.fallback_allowed is True:
        raise ValueError("OpenRouter fallbacks must be disabled")
    if body.fallback_allowed is False and body.allow_fallbacks:
        raise ValueError("OpenRouter fallback policy fields disagree")
    egress_class = body.cloud_egress or body.egress_class
    spend_ceiling = body.spend_ceiling_microusd
    if body.max_cost_microusd is not None:
        if spend_ceiling is not None and body.max_cost_microusd != spend_ceiling:
            raise ValueError("OpenRouter spend ceiling fields disagree")
        spend_ceiling = body.max_cost_microusd
    credential_ref = body.credential_ref
    if credential_ref == "OPENROUTER_API_KEY":
        credential_ref = OPENROUTER_ENV_CREDENTIAL_REF
    supplied_key = body.api_key.get_secret_value() if body.api_key is not None else ""
    if (
        existing is not None
        and not supplied_key.strip()
        and body.credential_ref is not None
        and credential_ref != existing.credential_ref
    ):
        raise ValueError("blank API key input cannot replace the persisted credential reference")
    return OpenRouterSetup(
        profile_id=body.profile_id,
        model_ids=model_ids,
        capabilities=capabilities,
        temperature=body.temperature,
        max_output_tokens=body.max_output_tokens,
        timeout_seconds=body.timeout if body.timeout is not None else body.timeout_seconds,
        allowed_upstreams=tuple(item.strip() for item in body.allowed_upstreams),
        allow_fallbacks=body.allow_fallbacks,
        require_parameters=body.require_parameters,
        data_collection=body.data_collection.strip().lower(),
        data_retention_policy=body.data_retention_policy.strip().lower(),
        zero_data_retention=body.zero_data_retention,
        egress_class=egress_class,
        cloud_egress_acknowledged=body.cloud_egress_acknowledged,
        spend_ceiling_microusd=spend_ceiling,
        max_queued=body.max_queue_size if body.max_queue_size is not None else body.max_queued,
        max_inflight=body.max_inflight,
        max_outstanding_per_owner=body.max_outstanding_per_owner,
        max_retries=body.max_retries,
        credential_ref=credential_ref or (
            existing.credential_ref if existing is not None else OPENROUTER_VAULT_CREDENTIAL_REF
        ),
        credential_fingerprint=existing.credential_fingerprint if existing is not None else None,
        schema_version=OPENROUTER_SETUP_SCHEMA_VERSION,
    )


def _configured_openrouter_key() -> str:
    """Read the trusted process configuration without including it in output."""
    return str(settings.openrouter_api_key or os.getenv("OPENROUTER_API_KEY", "") or "").strip()


def _fingerprint_secret(value: str | None) -> str | None:
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


async def _store_setup_credential(
    setup: OpenRouterSetup,
    api_key: SecretStr | None,
) -> OpenRouterSetup:
    """Store a newly supplied key in the existing encrypted vault."""
    raw_key = api_key.get_secret_value() if api_key is not None else ""
    if not raw_key.strip():
        # A blank write means "keep the current credential".  In particular,
        # an already hydrated vault credential must remain vault-backed; it
        # must never be silently relabelled as an environment secret.
        configured_key = (
            str(settings.openrouter_api_key or "").strip()
            if setup.credential_ref == OPENROUTER_VAULT_CREDENTIAL_REF
            else _configured_openrouter_key()
        )
        if not configured_key and setup.credential_ref == OPENROUTER_VAULT_CREDENTIAL_REF:
            try:
                configured_key = str(await vault_repository.get("openrouter_api_key") or "").strip()
            except Exception:
                # Policy-only keyless saves remain safe when the vault is
                # temporarily unavailable.  Preserve the prior reference and
                # fingerprint so status can report configuration_required.
                configured_key = ""
        if configured_key and setup.credential_ref in {
            OPENROUTER_VAULT_CREDENTIAL_REF,
            OPENROUTER_ENV_CREDENTIAL_REF,
        }:
            return OpenRouterSetup(
                **{
                    **setup.__dict__,
                    "credential_fingerprint": _fingerprint_secret(configured_key),
                }
            )
        return setup
    if len(raw_key) > 512 or any(ord(character) < 32 or ord(character) == 127 for character in raw_key):
        raise ValueError("OpenRouter API key contains unsafe characters")
    if setup.credential_ref not in {OPENROUTER_VAULT_CREDENTIAL_REF, OPENROUTER_ENV_CREDENTIAL_REF}:
        raise ValueError("OpenRouter credential reference must use the trusted vault or env reference")
    try:
        await vault_repository.store(
            "openrouter_api_key",
            raw_key,
            description="Seraph OpenRouter API key (write-only settings input)",
        )
    except Exception as exc:
        raise RuntimeError("OpenRouter credential persistence failed") from exc
    # Keep the current process usable after save. The persisted source remains
    # the encrypted vault; this assignment is never returned or logged here.
    settings.openrouter_api_key = raw_key
    return OpenRouterSetup(
        **{
            **setup.__dict__,
            "credential_ref": OPENROUTER_VAULT_CREDENTIAL_REF,
            "credential_fingerprint": _fingerprint_secret(raw_key),
        }
    )


async def _snapshot_setup_credential() -> str | None:
    """Read the prior vault value for a compensating credential update."""
    try:
        return await vault_repository.get("openrouter_api_key")
    except Exception as exc:
        # Do not write a replacement when the previous value cannot be
        # recovered.  That would make a later configuration failure unable to
        # restore the operator's credential safely.
        raise RuntimeError("OpenRouter credential snapshot failed") from exc


async def _restore_setup_credential(
    previous_vault_value: str | None,
    previous_process_value: str,
) -> None:
    """Restore both credential sources after a failed configuration write."""
    try:
        if previous_vault_value is None:
            await vault_repository.delete("openrouter_api_key")
        else:
            await vault_repository.store(
                "openrouter_api_key",
                previous_vault_value,
                description="Seraph OpenRouter API key (write-only settings input)",
            )
    except Exception as exc:
        settings.openrouter_api_key = previous_process_value
        raise RuntimeError("OpenRouter credential rollback failed") from exc
    settings.openrouter_api_key = previous_process_value


def _setup_configuration(
    setup: OpenRouterSetup,
    *,
    profiles: tuple[ModelFabricProfileInput, ...],
    policies: tuple[WorkloadPolicyInput, ...],
) -> ModelFabricConfiguration:
    generated_profile = openrouter_profile_for_setup(setup)
    if profiles:
        raise ValueError(
            "OpenRouter setup owns the canonical profile; omit API-supplied profiles"
        )
    else:
        configured_profiles = (generated_profile,)
    if policies:
        raise ValueError(
            "OpenRouter setup owns the canonical workload policies; omit API-supplied policies"
        )
    else:
        configured_policies = tuple(
            openrouter_policy_for_setup(setup, runtime_path)
            for runtime_path in CANONICAL_ROUTE_SPECS
        )
    return ModelFabricConfiguration(
        profiles=configured_profiles,
        workload_policies=configured_policies,
        status="ready",
        openrouter_setup=setup,
    )


@router.get("/settings/model-fabric")
async def get_model_fabric_settings():
    return await model_fabric_settings_payload()


@router.put("/settings/model-fabric")
async def put_model_fabric_settings(body: ModelFabricConfigurationRequest, request: Request):
    if not _is_local_request(request):
        raise HTTPException(status_code=403, detail="Model-fabric settings require localhost access")
    credential_mutated = False
    previous_vault_value: str | None = None
    previous_process_value = str(settings.openrouter_api_key or "")
    try:
        setup_input = body.openrouter_setup or body.openrouter
        persisted = read_model_fabric_configuration()
        existing = persisted.openrouter_setup
        if setup_input is not None:
            # Build and validate the complete profile before writing a new
            # credential. An invalid policy must never leave a usable secret
            # behind in the vault.
            setup = _openrouter_setup_from_input(setup_input, existing=existing)
            configuration = _setup_configuration(
                setup,
                profiles=body.profiles,
                policies=body.workload_policies,
            )
            validate_active_model_fabric_configuration(configuration)
            raw_key = (
                setup_input.api_key.get_secret_value()
                if setup_input.api_key is not None
                else ""
            )
            if raw_key.strip():
                previous_vault_value = await _snapshot_setup_credential()
            stored_setup = await _store_setup_credential(setup, setup_input.api_key)
            credential_mutated = bool(raw_key.strip())
            if stored_setup != setup:
                setup = stored_setup
                configuration = _setup_configuration(
                    setup,
                    profiles=body.profiles,
                    policies=body.workload_policies,
                )
                validate_active_model_fabric_configuration(configuration)
        else:
            if existing is not None:
                raise ValueError(
                    "persisted OpenRouter setup must be included; refusing destructive replacement"
                )
            if persisted.status == "degraded":
                raise ValueError(
                    "persisted model-fabric settings are unreadable; refusing destructive replacement"
                )
            configuration = ModelFabricConfiguration(
                profiles=tuple(ProviderProfile(**item.model_dump()) for item in body.profiles),
                workload_policies=tuple(WorkloadPolicy(**item.model_dump()) for item in body.workload_policies),
                status="ready",
            )
        validate_active_model_fabric_configuration(configuration)
        write_model_fabric_configuration(configuration)
    except Exception as exc:
        if credential_mutated:
            try:
                await _restore_setup_credential(previous_vault_value, previous_process_value)
            except RuntimeError as rollback_error:
                raise rollback_error
        if isinstance(exc, ValueError):
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if isinstance(exc, RuntimeError):
            raise HTTPException(status_code=503, detail="Model-fabric credential persistence failed") from exc
        if isinstance(exc, OSError):
            raise HTTPException(status_code=503, detail="Model-fabric settings persistence failed") from exc
        raise
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
        admission_broker=remote_inference_admission_broker,
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
    if reason := active_provider_exclusion_reason(profile):
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
    openrouter_setup_status = await _openrouter_setup_status(configured.openrouter_setup)
    proof_metadata_degraded = False
    try:
        proofs = await _profile_proof_statuses()
        all_profiles = _operator_profile_statuses(proofs)
    except Exception:
        proof_metadata_degraded = True
        all_profiles = _operator_profile_statuses(())
    profiles = [item for item in all_profiles if item["model_fabric_eligible"]]
    excluded_profiles = [item for item in all_profiles if not item["model_fabric_eligible"]]
    if proof_metadata_degraded:
        status = "degraded"
    elif openrouter_setup_status is not None and openrouter_setup_status["status"] == "configuration_required":
        status = "configuration_required"
    else:
        status = configured.status
    return {
        "schema_version": "seraph.model-fabric.settings.v1",
        "status": "degraded" if configured.status == "degraded" else status,
        "error_code": configured.error_code or ("proof_metadata_unavailable" if proof_metadata_degraded else None),
        "configuration_status": configured.status,
        "updated_at": configured.updated_at,
        "profiles": profiles,
        "excluded_profiles": excluded_profiles,
        "persisted_profile_ids": [profile.id for profile in configured.profiles],
        "workload_policies": [_policy_payload(policy) for policy in configured.workload_policies],
        "defaults": {"egress_class": EgressClass.LOCAL_ONLY.value, "fallback_allowed": False},
        "canary_endpoint": "/api/settings/model-fabric/canary",
        "openrouter_setup": openrouter_setup_status,
    }


async def _openrouter_setup_status(setup: OpenRouterSetup | None) -> dict[str, object] | None:
    """Return setup metadata while keeping credentials backend-only."""
    if setup is None:
        return None
    credential = (
        str(settings.openrouter_api_key or "").strip()
        if setup.credential_ref == OPENROUTER_VAULT_CREDENTIAL_REF
        else _configured_openrouter_key()
    )
    credential_store_error: str | None = None
    if not credential and setup.credential_ref == OPENROUTER_VAULT_CREDENTIAL_REF:
        try:
            await hydrate_openrouter_credential()
            credential = (
                str(settings.openrouter_api_key or "").strip()
                if setup.credential_ref == OPENROUTER_VAULT_CREDENTIAL_REF
                else _configured_openrouter_key()
            )
        except Exception:
            credential_store_error = "credential_store_unavailable"
    payload = _openrouter_setup_payload(setup)
    payload.update(
        {
            "api_base": OPENROUTER_API_BASE,
            "provider_kind": OPENROUTER_PROVIDER_KIND,
            "credential_configured": bool(credential),
            "credential_fingerprint": _fingerprint_secret(credential) if credential else None,
            "status": "configured_unverified" if credential else "configuration_required",
            "error_code": credential_store_error or (None if credential else "credential_missing"),
            "provider_calls": "manual_canary_only",
        }
    )
    # Defensive deletion protects this boundary if the dataclass ever gains a
    # credential-like field in a later schema revision.
    for secret_field in ("api_key", "secret", "token", "authorization"):
        payload.pop(secret_field, None)
    return payload


async def model_fabric_runtime_status(active_profile: str | None) -> dict[str, object]:
    configured = read_model_fabric_configuration()
    openrouter_setup_status = await _openrouter_setup_status(configured.openrouter_setup)
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
    inference_readiness = _active_profile_readiness(
        all_profiles,
        active_profile=active_profile,
    )
    return {
        "status": (
            "degraded"
            if degraded
            else "configuration_required"
            if openrouter_setup_status is not None
            and openrouter_setup_status["status"] == "configuration_required"
            else inference_readiness["status"]
        ),
        "configuration_status": configured.status,
        "configuration_error": configured.error_code,
        "configured_chat_profile": active_profile,
        "inference_readiness": inference_readiness,
        "profiles": [item for item in all_profiles if item["model_fabric_eligible"]],
        "excluded_profiles": [item for item in all_profiles if not item["model_fabric_eligible"]],
        "workload_policies": [_policy_payload(policy) for policy in configured.workload_policies],
        "openrouter_setup": openrouter_setup_status,
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


def _active_profile_readiness(
    profiles: list[dict[str, object]],
    *,
    active_profile: str | None,
) -> dict[str, object]:
    """Return explicit active-route readiness without probing the provider."""
    profile_id = str(active_profile or "").strip()
    reasons: list[str] = []
    profile = next(
        (
            item
            for item in profiles
            if isinstance(item, dict) and str(item.get("id") or "") == profile_id
        ),
        None,
    )
    if profile is None:
        reasons.append("active_profile_missing")
    else:
        if profile.get("model_fabric_eligible") is not True:
            reasons.append(
                "profile_ineligible:" + str(profile.get("model_fabric_exclusion_reason") or "unknown")
            )
        if profile.get("routable") is not True:
            non_routable = profile.get("non_routable_reasons")
            if isinstance(non_routable, list):
                reasons.extend(str(reason) for reason in non_routable if str(reason).strip())
            if not non_routable:
                reasons.append("profile_not_routable")

    policy = effective_workload_policy("chat_agent")
    if policy.egress_class is EgressClass.LOCAL_ONLY:
        reasons.append("chat_cloud_egress_not_allowed")
    if not policy.cloud_egress_acknowledged:
        reasons.append("chat_cloud_consent_missing")
    if policy.max_cost_microusd is None:
        reasons.append("chat_cost_ceiling_missing")
    if set(policy.allowed_provider_kinds) != {"openrouter"}:
        reasons.append("chat_provider_policy_missing")

    deduped_reasons = list(dict.fromkeys(reasons))
    return {
        "status": "ready" if not deduped_reasons else "configuration_required",
        "profile_id": profile_id or None,
        "provider": "openrouter",
        "reasons": deduped_reasons,
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
        provider_reason = active_provider_exclusion_reason(profile)
        schema_provider_eligible = (
            profile.schema_version == MODEL_FABRIC_SCHEMA_VERSION and provider_reason is None
        )
        non_routable: list[str] = []
        if reason := active_provider_exclusion_reason(profile):
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
