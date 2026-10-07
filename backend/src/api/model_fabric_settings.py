"""Operator configuration, status, and bounded manual model-fabric canaries."""

from __future__ import annotations

import asyncio

import base64
from datetime import datetime, timezone
from decimal import Decimal
from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from typing import Literal
from uuid import uuid4

import httpx
from config.settings import settings
from fastapi import APIRouter, HTTPException, Request, Query
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from src.approval.runtime import get_current_trust_principal
from src.llm_runtime import provider_profiles
from src.model_fabric import (
    ModelCapability,
    ProviderProfile,
    candidate_from_profile,
    finalized_openai_compatible_body,
    finalized_openai_compatible_embeddings_body,
)
from src.model_fabric.caller_context import CANONICAL_ROUTE_SPECS
from src.model_fabric.configuration import (
    ModelFabricConfiguration,
    OPENROUTER_ENV_CREDENTIAL_REF,
    OPENROUTER_SETUP_SCHEMA_VERSION,
    OPENROUTER_SETUP_V2_SCHEMA_VERSION,
    OPENROUTER_ROUTE_SLOTS,
    OPENROUTER_VAULT_CREDENTIAL_REF,
    OpenRouterSetup,
    OpenRouterRoute,
    NearTextSetup,
    deployment_spend_ceiling,
    validate_near_text_setup,
    WorkloadPolicy,
    credential_ref_allowed,
    effective_workload_policy,
    normalize_openrouter_model_id,
    openrouter_policy_for_setup,
    openrouter_profile_for_setup,
    openrouter_profiles_for_setup,
    migrate_openrouter_setup_v1_to_v2,
    hydrate_openrouter_credential,
    _openrouter_setup_payload,
    _configuration_payload,
    read_model_fabric_configuration,
    validate_active_model_fabric_configuration,
    validate_openrouter_setup,
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


class OpenRouterRouteInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model_id: str
    enabled: bool
    capabilities: list[str]
    allowed_upstreams: list[str]
    temperature: float = Field(ge=0, le=2, allow_inf_nan=False)
    max_output_tokens: int = Field(ge=1, le=131_072)
    timeout_seconds: float = Field(ge=1, le=120, allow_inf_nan=False)
    zero_data_retention: bool
    request_cost_bound_microusd: int = Field(ge=1, le=1_000_000_000)


class OpenRouterRouteSlotsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: OpenRouterRouteInput | None = None
    vision: OpenRouterRouteInput | None = None
    embedding: OpenRouterRouteInput | None = None


class OpenRouterSetupInput(BaseModel):
    """Write-only operator setup fields for the fixed OpenRouter route."""

    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["seraph.openrouter.setup.v1", "seraph.openrouter.setup.v2"] = OPENROUTER_SETUP_SCHEMA_VERSION
    routes: OpenRouterRouteSlotsInput | None = None
    vision_egress_acknowledged: bool = Field(default=False, strict=True)
    embedding_egress_acknowledged: bool = Field(default=False, strict=True)

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
    spend_ceiling_microusd: int | None = Field(default=None, ge=1, le=1_000_000_000, strict=True)
    request_cost_bound_microusd: int | None = Field(default=None, ge=1, le=1_000_000_000, strict=True)
    max_cost_microusd: int | None = Field(default=None, ge=1, le=1_000_000_000, strict=True)
    max_queued: int = Field(default=64, ge=1, le=64)
    max_queue_size: int | None = Field(default=None, ge=1, le=64)
    max_inflight: int = Field(default=1, ge=1, le=1)
    max_outstanding_per_owner: int = Field(default=16, ge=1, le=16)
    max_retries: int = Field(default=2, ge=0, le=2)
    credential_ref: str | None = None
    # SecretStr prevents accidental repr/model dump exposure. The value is
    # consumed only by the trusted PUT handler and never enters a response.
    api_key: SecretStr | None = Field(default=None, repr=False)

    @model_validator(mode="before")
    @classmethod
    def closed_v2_contract(cls, data):
        if isinstance(data, dict) and data.get("schema_version") == OPENROUTER_SETUP_V2_SCHEMA_VERSION:
            legacy = {"model_ids", "models", "model_id", "capabilities", "modalities", "temperature", "max_output_tokens", "timeout_seconds", "timeout", "allowed_upstreams", "zero_data_retention", "request_cost_bound_microusd", "cloud_egress", "max_cost_microusd", "max_queue_size", "fallback_allowed"}
            if legacy.intersection(data):
                raise ValueError("v2 route fields belong inside routes")
            if not isinstance(data.get("routes"), dict):
                raise ValueError("v2 requires routes")
            for field in ("cloud_egress_acknowledged", "allow_fallbacks", "require_parameters"):
                if field in data and type(data[field]) is not bool:
                    raise ValueError("v2 shared booleans must be literal")
            for field in ("max_queued", "max_inflight", "max_outstanding_per_owner", "max_retries"):
                if field in data and type(data[field]) is not int:
                    raise ValueError("v2 shared integers must be strict")
        return data


class NearTextSetupInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["seraph.near.text.v1"] = "seraph.near.text.v1"
    enabled: bool = Field(default=False, strict=True)
    profile_id: Literal["near.text"] = "near.text"
    model_id: Literal["z-ai/glm-5.3-flash"] = "z-ai/glm-5.3-flash"
    api_base: Literal["https://cloud-api.near.ai/v1"] = "https://cloud-api.near.ai/v1"
    max_output_tokens: int = Field(default=1024, strict=True, ge=1, le=1024)
    timeout_seconds: float = Field(default=45, ge=1, le=45, allow_inf_nan=False)
    request_cost_bound_microusd: int = Field(strict=True, ge=1, le=1_000_000_000)
    spend_ceiling_microusd: int = Field(strict=True, ge=1, le=1_000_000_000)
    plaintext_provider_egress_acknowledged: bool = Field(default=False, strict=True)
    api_key: SecretStr | None = Field(default=None, repr=False)

    @model_validator(mode="before")
    @classmethod
    def literal_timeout(cls, value):
        if isinstance(value, dict) and "timeout_seconds" in value and type(value["timeout_seconds"]) not in (int, float):
            raise ValueError("NEAR timeout must be numeric")
        return value


class ModelFabricConfigurationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profiles: tuple[ModelFabricProfileInput, ...] = ()
    workload_policies: tuple[WorkloadPolicyInput, ...] = ()
    openrouter: OpenRouterSetupInput | None = None
    openrouter_setup: OpenRouterSetupInput | None = None
    near_text: NearTextSetupInput | None = None
    expected_policy_revision: int | None = Field(default=None, ge=1, strict=True)

    @model_validator(mode="after")
    def one_openrouter_setup_field(self):
        if self.openrouter is not None and self.openrouter_setup is not None:
            raise ValueError("send only one OpenRouter setup object")
        if "near_text" in self.model_fields_set:
            if self.near_text is None:
                raise ValueError("disable NEAR with enabled=false, not null")
            if self.openrouter is not None or self.openrouter_setup is not None:
                raise ValueError("send only one provider setup mutation")
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
        request_cost_bound_microusd=body.request_cost_bound_microusd,
        max_queued=body.max_queue_size if body.max_queue_size is not None else body.max_queued,
        max_inflight=body.max_inflight,
        max_outstanding_per_owner=body.max_outstanding_per_owner,
        max_retries=body.max_retries,
        credential_ref=credential_ref or (
            existing.credential_ref if existing is not None else OPENROUTER_VAULT_CREDENTIAL_REF
        ),
        credential_fingerprint=existing.credential_fingerprint if existing is not None else None,
        schema_version=body.schema_version,
        routes={slot: OpenRouterRoute(**{**route.model_dump(),
            "model_id": normalize_openrouter_model_id(route.model_id),
            "capabilities": tuple(route.capabilities), "allowed_upstreams": tuple(route.allowed_upstreams)}) if route is not None else None
            for slot, route in ((slot, getattr(body.routes, slot)) for slot in OPENROUTER_ROUTE_SLOTS)} if body.routes is not None else None,
        purpose_consents=existing.purpose_consents if existing is not None and body.schema_version == OPENROUTER_SETUP_V2_SCHEMA_VERSION else None,
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
    existing_profiles: tuple[ProviderProfile, ...] = (),
) -> ModelFabricConfiguration:
    generated_profiles = openrouter_profiles_for_setup(setup, existing=existing_profiles)
    if profiles:
        raise ValueError(
            "OpenRouter setup owns the canonical profile; omit API-supplied profiles"
        )
    else:
        configured_profiles = generated_profiles
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


def _carry_near_consent(setup, persisted, revision):
    if setup is None:
        return None
    prior = persisted.near_text
    unchanged = prior is not None and replace(prior, spend_ceiling_microusd=setup.spend_ceiling_microusd,
        plaintext_egress_consent_revision=None) == replace(setup, plaintext_egress_consent_revision=None)
    current = prior is not None and prior.enabled and not persisted.egress_revoked and prior.plaintext_egress_consent_revision == persisted.egress_revision
    return replace(setup, plaintext_egress_consent_revision=revision if setup.enabled and unchanged and current else None)


async def _put_near_text(body, persisted):
    from src.workspace.accounting_witness import maintenance_accounting_lock, PolicyRevisionConflict, configuration_digest, policy_continuity
    from src.workspace.production import read_lifecycle_receipt
    from src.workflows.job_runtime import durable_job_repository
    revision = persisted.egress_revision
    if body.expected_policy_revision != revision or persisted.status == "degraded":
        raise HTTPException(status_code=409, detail="provider_policy_revision_changed")
    if {"profiles", "workload_policies"}.intersection(body.model_fields_set):
        raise HTTPException(status_code=422, detail="NEAR owns its fixed dedicated route")
    incoming = body.near_text
    fields = incoming.model_dump(exclude={"api_key", "plaintext_provider_egress_acknowledged"})
    prior = persisted.near_text
    setup = NearTextSetup(**fields, credential_fingerprint=prior.credential_fingerprint if prior else None)
    raw_key = incoming.api_key.get_secret_value() if incoming.api_key is not None else ""
    if raw_key.strip():
        if len(raw_key) > 512 or any(ord(char) < 32 or ord(char) == 127 for char in raw_key):
            raise HTTPException(status_code=422, detail="NEAR API key contains unsafe characters")
        setup = replace(setup, credential_fingerprint=_fingerprint_secret(raw_key))
    validate_near_text_setup(setup)
    if persisted.openrouter_setup is not None and setup.spend_ceiling_microusd != persisted.openrouter_setup.spend_ceiling_microusd:
        raise HTTPException(status_code=409, detail="deployment_spend_ceiling_mismatch")
    final_revision = revision + 2
    setup = _carry_near_consent(setup, persisted, final_revision)
    if setup.enabled and setup.plaintext_egress_consent_revision is None:
        if not incoming.plaintext_provider_egress_acknowledged:
            raise HTTPException(status_code=403, detail="near_text_plaintext_acknowledgement_required")
        setup = replace(setup, plaintext_egress_consent_revision=final_revision)
    old_or = persisted.openrouter_setup
    if old_or is not None:
        consents = {slot: final_revision for slot, epoch in (old_or.purpose_consents or {}).items()
            if not persisted.egress_revoked and epoch == revision}
        old_or = replace(old_or, purpose_consents=consents if old_or.routes is not None else old_or.purpose_consents,
            cloud_egress_acknowledged=old_or.cloud_egress_acknowledged and not persisted.egress_revoked)
    target = replace(persisted, near_text=setup, openrouter_setup=old_or, status="ready", error_code=None,
        egress_revision=final_revision, egress_revoked=persisted.egress_revoked and not setup.enabled,
        egress_revocation_key=None, updated_at=datetime.now(timezone.utc).isoformat())
    validate_active_model_fabric_configuration(target)
    previous_key = await vault_repository.get("near_text_api_key") if raw_key.strip() else None
    mutated = False
    root = Path(settings.workspace_dir).resolve()
    try:
        with maintenance_accounting_lock(root) as workspace:
            write_model_fabric_configuration(replace(target, egress_revision=revision + 1, egress_revoked=True),
                expected_revision=revision, publication_workspace=workspace)
            try:
                if raw_key.strip():
                    mutated = True
                    await vault_repository.store("near_text_api_key", raw_key, description="Seraph NEAR key (write-only settings input)")
                bounds = [setup.request_cost_bound_microusd] if setup.enabled else []
                if old_or is not None:
                    bounds.extend(route.request_cost_bound_microusd for route in (old_or.routes or {}).values() if route is not None and route.enabled)
                    if old_or.routes is None and old_or.request_cost_bound_microusd is not None:
                        bounds.append(old_or.request_cost_bound_microusd)
                review = max(bounds, default=None)
                ceiling = deployment_spend_ceiling(target)
                configured = await durable_job_repository.configure_inference_accounting(ceiling,
                    reserve_review_microusd=review, continuity_workspace=workspace)
                accounting = await durable_job_repository.inference_accounting_snapshot(continuity_workspace=workspace)
                retained_review = accounting.get("request_reserve_review")
                exact_review = review is None or isinstance(retained_review, dict) and retained_review.get("bound_microusd") == review and retained_review.get("settings_revision") == accounting.get("settings_revision") and retained_review.get("accounting_revision") == configured.get("revision", 0) - 1
                if accounting.get("status") != "ready" or accounting.get("ceiling_microusd") != ceiling or accounting.get("overrun_max_cost_microusd", 0) or not exact_review or any(accounting.get(field) != configured.get(field) for field in ("deployment_id", "revision", "ledger_digest")):
                    raise RuntimeError("accounting_settings_revision_unavailable")
                write_model_fabric_configuration(target, expected_revision=revision + 1, publication_workspace=workspace)
            except Exception:
                if mutated:
                    path = root / "model-fabric-settings.json"
                    actual = json.loads(path.read_text()) if path.is_file() else None
                    witness = read_lifecycle_receipt(workspace) or {}
                    active = isinstance(actual, dict) and configuration_digest(actual) == configuration_digest(_configuration_payload(target)) and policy_continuity(workspace, actual)[0]
                    if not active and isinstance(actual, dict) and actual.get("egress_revoked") is True and witness.get("provider_policy", {}).get("state") == "revoked":
                        if previous_key is None:
                            await vault_repository.delete("near_text_api_key")
                        else:
                            await vault_repository.store("near_text_api_key", previous_key)
                raise
    except PolicyRevisionConflict as exc:
        raise HTTPException(status_code=409, detail="provider_policy_revision_changed") from exc
    except (RuntimeError, OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="provider_policy_publication_unavailable") from exc
    return await model_fabric_settings_payload()


@router.get("/settings/model-fabric")
async def get_model_fabric_settings():
    return await model_fabric_settings_payload()


@router.put("/settings/model-fabric")
async def put_model_fabric_settings(body: ModelFabricConfigurationRequest, request: Request):
    from src.model_fabric.effective_policy import configuration_mutation_lock
    async with configuration_mutation_lock:
        return await _put_model_fabric_settings_locked(body, request)


async def _put_model_fabric_settings_locked(body: ModelFabricConfigurationRequest, request: Request):
    if not _is_local_request(request):
        raise HTTPException(
            status_code=403,
            detail="Model-fabric settings require a loopback request or an authenticated operator on the configured host/origin boundary",
        )
    credential_mutated = False
    previous_vault_value: str | None = None
    previous_process_value = str(settings.openrouter_api_key or "")
    try:
        setup_input = body.openrouter_setup or body.openrouter
        persisted = read_model_fabric_configuration()
        if body.near_text is not None:
            return await _put_near_text(body, persisted)
        if setup_input is None and persisted.near_text is not None:
            if {"profiles", "workload_policies"}.intersection(body.model_fields_set):
                raise HTTPException(status_code=422, detail="provider setups own their fixed routes")
            # An omitted optional setup is preservation, never deletion/regrant.
            return await model_fabric_settings_payload()
        existing = persisted.openrouter_setup
        if setup_input is not None and (setup_input.schema_version == OPENROUTER_SETUP_V2_SCHEMA_VERSION or persisted.near_text is not None):
            if existing is not None and existing.schema_version == OPENROUTER_SETUP_V2_SCHEMA_VERSION and setup_input.schema_version != OPENROUTER_SETUP_V2_SCHEMA_VERSION:
                raise HTTPException(status_code=409, detail="setup_schema_upgrade_required")
            return await _put_openrouter_v2(body, setup_input, persisted)
        if existing is not None and existing.schema_version == OPENROUTER_SETUP_V2_SCHEMA_VERSION:
            raise HTTPException(status_code=409, detail="setup_schema_upgrade_required")
        if persisted.egress_revoked and body.expected_policy_revision != persisted.egress_revision:
            raise HTTPException(status_code=409, detail="Explicit current policy revision is required to re-grant egress")
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
        configuration = replace(configuration, egress_revision=persisted.egress_revision + 1,
            egress_revoked=False, egress_revocation_key=None)
        if configuration.openrouter_setup is not None:
            from src.workflows.job_runtime import durable_job_repository
            review = (configuration.openrouter_setup.request_cost_bound_microusd or configuration.openrouter_setup.spend_ceiling_microusd) if setup_input is not None and "request_cost_bound_microusd" in setup_input.model_fields_set else None
            await durable_job_repository.configure_inference_accounting(configuration.openrouter_setup.spend_ceiling_microusd,
                reserve_review_microusd=review)
        write_model_fabric_configuration(configuration)
    except Exception as exc:
        if credential_mutated:
            try:
                await _restore_setup_credential(previous_vault_value, previous_process_value)
            except RuntimeError as rollback_error:
                raise rollback_error
        from src.workflows.inference_accounting import InferenceAccountingError
        if isinstance(exc, InferenceAccountingError):
            raise HTTPException(status_code=503, detail=exc.code) from exc
        if isinstance(exc, ValueError):
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if isinstance(exc, RuntimeError):
            raise HTTPException(status_code=503, detail="Model-fabric credential persistence failed") from exc
        if isinstance(exc, OSError):
            raise HTTPException(status_code=503, detail="Model-fabric settings persistence failed") from exc
        raise
    return await model_fabric_settings_payload()


async def _put_openrouter_v2(body, setup_input, persisted):
    """Witnessed revoke→credential/accounting→activate, never cross-store atomic."""
    from src.workspace.accounting_witness import maintenance_accounting_lock, PolicyRevisionConflict, policy_continuity
    from src.workspace.production import read_lifecycle_receipt
    from src.workflows.job_runtime import durable_job_repository
    from src.workflows.inference_accounting import InferenceAccountingError

    revision = persisted.egress_revision
    if body.expected_policy_revision != revision:
        raise HTTPException(status_code=409, detail="provider_policy_revision_changed")
    if persisted.status == "degraded":
        raise HTTPException(status_code=409, detail="provider_policy_reconciliation_required")
    existing = persisted.openrouter_setup
    prior = migrate_openrouter_setup_v1_to_v2(existing, egress_revision=revision,
        allow_unconsented=persisted.near_text is not None) if existing and setup_input.schema_version == OPENROUTER_SETUP_V2_SCHEMA_VERSION else existing
    setup = _openrouter_setup_from_input(setup_input, existing=prior)
    raw_key = setup_input.api_key.get_secret_value() if setup_input.api_key is not None else ""
    if raw_key.strip() and (len(raw_key) > 512 or any(ord(char) < 32 or ord(char) == 127 for char in raw_key)):
        raise ValueError("OpenRouter API key contains unsafe characters")
    if raw_key.strip():
        setup = replace(setup, credential_ref=OPENROUTER_VAULT_CREDENTIAL_REF,
            credential_fingerprint=_fingerprint_secret(raw_key))
    final_revision = revision + 2
    consents = {}
    for slot in ("vision", "embedding"):
        route = (setup.routes or {}).get(slot)
        if route is None or not route.enabled:
            continue
        previous_route = (prior.routes or {}).get(slot) if prior else None
        preserved = prior is not None and not persisted.egress_revoked and route == previous_route and (prior.purpose_consents or {}).get(slot) == revision
        if not preserved and not getattr(setup_input, f"{slot}_egress_acknowledged"):
            raise HTTPException(status_code=403, detail=f"{slot}_egress_acknowledgement_required")
        consents[slot] = final_revision
    setup = replace(setup, purpose_consents=consents)
    target = _setup_configuration(setup, profiles=body.profiles, policies=body.workload_policies,
        existing_profiles=persisted.profiles)
    snapshot = persisted.v1_rollback_snapshot
    if existing is not None and existing.schema_version == OPENROUTER_SETUP_SCHEMA_VERSION:
        snapshot = _configuration_payload(persisted)
    target = replace(target, egress_revision=final_revision, egress_revoked=False,
        updated_at=datetime.now(timezone.utc).isoformat(),
        v1_rollback_snapshot=snapshot)
    if persisted.near_text is not None:
        near = replace(persisted.near_text, spend_ceiling_microusd=setup.spend_ceiling_microusd)
        target = replace(target, near_text=_carry_near_consent(near, persisted, final_revision))
    validate_active_model_fabric_configuration(target)
    previous_process_value = str(settings.openrouter_api_key or "")
    previous_vault_value = await _snapshot_setup_credential() if raw_key.strip() else None
    credential_mutated = False
    root = Path(settings.workspace_dir).resolve()
    try:
        with maintenance_accounting_lock(root) as workspace:
            revoked = replace(target, egress_revision=revision + 1, egress_revoked=True)
            write_model_fabric_configuration(revoked, expected_revision=revision, publication_workspace=workspace)
            try:
                if raw_key.strip():
                    # Mark before the await: a failing vault write may already have committed.
                    credential_mutated = True
                    stored = await _store_setup_credential(setup, setup_input.api_key)
                    if stored != setup:
                        raise RuntimeError("credential target changed")
                review = max((route.request_cost_bound_microusd for route in (setup.routes or {}).values() if route is not None and route.enabled), default=None)
                if setup.routes is None:
                    review = setup.request_cost_bound_microusd
                if target.near_text is not None and target.near_text.enabled:
                    review = max(review or 0, target.near_text.request_cost_bound_microusd)
                configured_accounting = await durable_job_repository.configure_inference_accounting(setup.spend_ceiling_microusd,
                    reserve_review_microusd=review, continuity_workspace=workspace)
                accounting = await durable_job_repository.inference_accounting_snapshot(continuity_workspace=workspace)
                persisted_review = accounting.get("request_reserve_review")
                exact_review = review is None or (
                    isinstance(persisted_review, dict)
                    and persisted_review.get("settings_revision") == accounting.get("settings_revision")
                    and persisted_review.get("bound_microusd") == review
                    and type(persisted_review.get("accounting_revision")) is int
                    and persisted_review["accounting_revision"] == configured_accounting.get("revision", 0) - 1
                )
                exact_witness = all(accounting.get(field) == configured_accounting.get(field)
                    for field in ("deployment_id", "revision", "ledger_digest"))
                if accounting.get("status") != "ready" or accounting.get("ceiling_microusd") != setup.spend_ceiling_microusd or accounting.get("overrun_max_cost_microusd", 0) or not exact_review or not exact_witness:
                    raise InferenceAccountingError("accounting_settings_revision_unavailable")
                write_model_fabric_configuration(target, expected_revision=revision + 1, publication_workspace=workspace)
            except Exception:
                if credential_mutated:
                    # Publication can raise after replacing either the witness or file.
                    # Re-read both before deciding whether restoring the old key is safe.
                    path = root / "model-fabric-settings.json"
                    actual = json.loads(path.read_text()) if path.is_file() else None
                    witness = read_lifecycle_receipt(workspace) or {}
                    from src.workspace.accounting_witness import configuration_digest
                    exact_active = isinstance(actual, dict) and configuration_digest(actual) == configuration_digest(_configuration_payload(target)) and policy_continuity(workspace, actual)[0]
                    if not exact_active:
                        witnessed_revoked = witness.get("provider_policy", {}).get("state") == "revoked"
                        if isinstance(actual, dict) and actual.get("egress_revoked") is True and witnessed_revoked:
                            await _restore_setup_credential(previous_vault_value, previous_process_value)
                raise
    except PolicyRevisionConflict as exc:
        raise HTTPException(status_code=409, detail="provider_policy_revision_changed") from exc
    except RuntimeError as exc:
        if "busy" in str(exc):
            raise HTTPException(status_code=409, detail="provider_policy_revision_changed") from exc
        raise HTTPException(status_code=503, detail=getattr(exc, "code", "provider_policy_publication_unavailable")) from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="provider_policy_publication_unavailable") from exc
    return await model_fabric_settings_payload()


class InferenceSettlementInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str = Field(min_length=1, max_length=256)
    job_id: str = Field(min_length=1, max_length=256)
    expected_revision: int = Field(ge=1, strict=True)
    actual_cost_microusd: int = Field(ge=0, le=1_000_000_000, strict=True)
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=128)


@router.get("/settings/model-fabric/accounting")
async def get_inference_accounting(job_id: str | None = Query(default=None, min_length=1, max_length=256)):
    from src.workflows.job_runtime import durable_job_repository
    return await durable_job_repository.inference_accounting_snapshot(job_id=job_id)


class InferencePeriodReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    period_id: str = Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")
    expected_revision: int = Field(strict=True, ge=1)


@router.post("/settings/model-fabric/accounting/period")
async def acknowledge_inference_period(body: InferencePeriodReviewInput, request: Request):
    from src.auth.service import authenticate_principal
    from src.workspace.accounting_continuity import acknowledge_accounting_period
    principal = getattr(getattr(request.state, "operator", None), "principal", None)
    if principal is None or not principal.authenticated or not _is_local_request(request):
        raise HTTPException(status_code=403, detail="Authenticated deployment accounting owner required")
    await authenticate_principal(principal.principal_id)
    try:
        return await asyncio.to_thread(acknowledge_accounting_period, root=Path(settings.workspace_dir).resolve(),
            period=body.period_id, expected_revision=body.expected_revision, actor=principal.principal_id)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/settings/model-fabric/accounting/settle")
async def settle_inference_accounting(body: InferenceSettlementInput, request: Request):
    from src.auth.service import authenticate_principal
    from src.workflows.job_runtime import durable_job_repository
    from src.workflows.inference_accounting import InferenceAccountingError
    principal = getattr(getattr(request.state, "operator", None), "principal", None) or get_current_trust_principal()
    if principal is None or not _is_local_request(request):
        raise HTTPException(status_code=403, detail="Authenticated deployment accounting owner required")
    await authenticate_principal(principal.principal_id)
    try:
        row = await durable_job_repository.settle_inference_cost(**body.model_dump(),
            operator_id=principal.principal_id, reason="explicit_operator_account_settlement")
    except InferenceAccountingError as exc:
        raise HTTPException(status_code=409, detail=exc.code) from exc
    result = {"status": row["state"], "operation": row, "memory_status": "no_learning",
        "authority_scope": "deployment_accounting", "job_authority_changed": False}
    if row.get("runtime_path") == "near_text_native" and row.get("profile_id") == "near.text":
        try:
            result["lane_recovery"] = await remote_inference_admission_broker.reconcile_settled_near_operation(
                operation_id=row["operation_id"], job_id=row["job_id"],
                expected_revision=row["revision"], operator=getattr(request.state, "operator", None),
            )
        except Exception:
            # Debt settlement already committed; local lane recovery cannot undo it.
            result["lane_recovery"] = {"status": "deferred", "reason_code": "lane_recovery_unavailable"}
    return result


@router.post("/settings/model-fabric/canary")
async def run_model_fabric_canary(body: CapabilityCanaryRequest, request: Request):
    if body.profile_id == "near.text":
        raise HTTPException(status_code=403, detail="near_text_dedicated_native_required")
    if not _is_local_request(request):
        raise HTTPException(
            status_code=403,
            detail="Model-fabric canaries require a loopback request or an authenticated operator on the configured host/origin boundary",
        )
    if body.capability not in _PROBE_CAPABILITIES:
        raise HTTPException(status_code=422, detail="Unsupported model capability")
    principal = getattr(getattr(request.state, "operator", None), "principal", None) or get_current_trust_principal()
    if principal is None:
        raise HTTPException(status_code=401, detail="Authenticated model-inference principal required")
    if AuthorityGrant.MODEL_INFERENCE not in principal.grants:
        raise HTTPException(status_code=403, detail="Principal lacks model-inference authority")
    if not principal.session_id and not principal.job_id:
        raise HTTPException(status_code=403, detail="Model-inference principal requires a bound session or job")
    profile = provider_profiles().get(body.profile_id)
    if profile is None:
        raise HTTPException(status_code=404, detail="Model-fabric profile not found")
    configured = read_model_fabric_configuration()
    setup = configured.openrouter_setup
    if setup is not None and setup.schema_version == OPENROUTER_SETUP_V2_SCHEMA_VERSION:
        # This existing proof generator is an explicit target exception, never
        # an ordinary task-class mapping or an unknown-to-text fallback.
        slot = next((slot for slot in OPENROUTER_ROUTE_SLOTS if profile.id == f"openrouter.{slot}"), None)
        if slot is None:
            raise HTTPException(status_code=403, detail="canary_exact_slot_required")
        runtime_path = {"text": "chat_agent", "vision": "screenshot_image_analysis", "embedding": "memory_embedding"}[slot]
        policy = effective_workload_policy(runtime_path)
    else:
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
            output_tokens=int(fixture.get("output_tokens", 64)),
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
    if profile.transport_adapter == "openai_compatible_embeddings":
        payload = finalized_openai_compatible_embeddings_body(model_id=profile.model,
            inputs="Seraph fixed non-sensitive embedding canary.", options=profile.options)
        return {"json": payload, "digest_payload": payload, "output_tokens": 1}
    controls = profile.options.get("_seraph_openrouter", {})
    payload = _chat_canary_payload(profile.model, capability, options=profile.options,
        output_tokens=min(64, int(controls.get("output_limit", 64))))
    return {
        "json": payload,
        "digest_payload": payload,
        "output_tokens": payload["max_tokens"],
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
    output_tokens = 1 if profile.transport_adapter == "openai_compatible_embeddings" else min(64, int(profile.options.get("_seraph_openrouter", {}).get("output_limit", 64)))
    if profile.max_output_tokens is None or profile.max_output_tokens < output_tokens:
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
    openrouter_setup_status = await _openrouter_setup_status(configured.openrouter_setup, configuration=configured)
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
    from src.workflows.job_runtime import durable_job_repository
    accounting = await durable_job_repository.inference_accounting_snapshot()
    near_status = await _near_text_setup_status(configured, accounting)
    if openrouter_setup_status is not None and (configured.egress_revoked or not configured.openrouter_setup.cloud_egress_acknowledged or accounting["status"] != "ready"):
        openrouter_setup_status["status"] = "blocked"
        openrouter_setup_status["error_code"] = configured.error_code or "provider_policy_revoked" if configured.egress_revoked else "openrouter_egress_acknowledgement_required" if not configured.openrouter_setup.cloud_egress_acknowledged else accounting.get("reason_code")
        status = "blocked"
        _block_setup_slots(openrouter_setup_status, openrouter_setup_status["error_code"])
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
        "near_text": near_status,
        "inference_accounting": accounting,
        "egress_revision": configured.egress_revision,
        "egress_revoked": configured.egress_revoked,
    }


async def _near_text_setup_status(configured, accounting):
    setup = configured.near_text
    if setup is None:
        return None
    key_present = False
    key_unavailable = False
    try:
        key = await vault_repository.get("near_text_api_key")
        key_present = isinstance(key, str) and bool(key) and _fingerprint_secret(key) == setup.credential_fingerprint
    except Exception:
        key_unavailable = True
    consent = not configured.egress_revoked and setup.enabled and setup.plaintext_egress_consent_revision == configured.egress_revision
    reason = None
    if not setup.enabled:
        status = "disabled"
    elif configured.status != "ready" or configured.egress_revoked or not consent:
        status = "blocked"
        reason = configured.error_code or ("provider_policy_revoked" if configured.egress_revoked else "near_text_plaintext_consent_required")
    elif not key_present:
        status = "configuration_required"
        reason = "near_text_credential_unavailable" if key_unavailable else "near_text_credential_required"
    elif accounting.get("status") != "ready" or accounting.get("ceiling_microusd") != setup.spend_ceiling_microusd:
        status = "blocked"
        reason = accounting.get("reason_code") or "inference_accounting_unavailable"
    else:
        status = "configured"
    return {**setup.__dict__, "key_present": key_present, "consent_current": consent,
        "status": status, "reason_code": reason, "tls_transport": True, "tee_verified": False,
        "e2ee": False, "provider_plaintext_disclosure": "NEAR receives the question in plaintext over HTTPS."}


def _block_setup_slots(payload, reason):
    for slot, state in payload.get("slot_statuses", {}).items():
        route = payload.get("routes", {}).get(slot)
        if route is not None and route.get("enabled"):
            state.update(status="blocked", error_code=reason)
            route.update(state)


async def _openrouter_setup_status(setup: OpenRouterSetup | None, *, configuration=None) -> dict[str, object] | None:
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
    original_setup = setup
    revision = configuration.egress_revision if configuration is not None else 1
    setup = migrate_openrouter_setup_v1_to_v2(setup, egress_revision=revision,
        allow_unconsented=configuration is not None and configuration.near_text is not None)
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
    payload.pop("purpose_consents", None)
    payload["slot_statuses"] = {}
    for slot in OPENROUTER_ROUTE_SLOTS:
        route = (setup.routes or {}).get(slot)
        payload["routes"].setdefault(slot, None)
        state = {"status": "configuration_required", "error_code": "route_disabled" if route is not None else "route_missing", "proof_expires_at": None}
        if route is not None and route.enabled:
            state.update(status="blocked", error_code=credential_store_error or "credential_missing" if not credential else "capability_proof_missing")
            if not credential:
                state["status"] = "configuration_required"
            elif original_setup.schema_version == OPENROUTER_SETUP_SCHEMA_VERSION:
                state["error_code"] = "setup_schema_upgrade_required"
            elif slot != "text" and (setup.purpose_consents or {}).get(slot) != revision:
                state["error_code"] = "purpose_consent_stale"
            else:
                profile = provider_profiles().get(f"openrouter.{slot}")
                if profile is not None:
                    candidate = candidate_from_profile(profile)
                    proofs = []
                    try:
                        for capability in {*route.capabilities, "health", "latency_ms"}:
                            proof = await model_fabric_repository.latest_capability_proof(
                                profile_schema_version=profile.schema_version, profile_contract_hash=profile.contract_hash,
                                profile_id=profile.id, model=profile.model, endpoint=candidate.endpoint,
                                endpoint_class=candidate.endpoint_class, adapter=candidate.adapter, capability=capability)
                            if proof is None or not proof_is_fresh(proof):
                                break
                            proofs.append(proof)
                        else:
                            state.update(status="ready", error_code=None,
                                proof_expires_at=datetime.fromtimestamp(min(proof.expires_at for proof in proofs), timezone.utc).isoformat())
                    except Exception:
                        state["error_code"] = "proof_metadata_unavailable"
            if original_setup.schema_version == OPENROUTER_SETUP_SCHEMA_VERSION:
                try:
                    validate_openrouter_setup(replace(setup, routes={slot: route}))
                except ValueError:
                    state.update(status="blocked", error_code="legacy_route_capabilities_require_review")
        if route is not None:
            payload["routes"][slot].update(state)
        payload["slot_statuses"][slot] = state
    if credential:
        payload["status"] = "ready" if any(state["status"] == "ready" for state in payload["slot_statuses"].values()) else "blocked"
    return payload


async def model_fabric_runtime_status(active_profile: str | None) -> dict[str, object]:
    configured = read_model_fabric_configuration()
    openrouter_setup_status = await _openrouter_setup_status(configured.openrouter_setup, configuration=configured)
    from src.workflows.job_runtime import durable_job_repository
    accounting = await durable_job_repository.inference_accounting_snapshot()
    accounting_reason = configured.error_code or "provider_policy_revoked" if configured.egress_revoked else accounting.get("reason_code") if accounting["status"] != "ready" else None
    if openrouter_setup_status is not None and accounting_reason:
        openrouter_setup_status["status"] = "blocked"
        openrouter_setup_status["error_code"] = accounting_reason
        _block_setup_slots(openrouter_setup_status, accounting_reason)
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
    if accounting_reason:
        inference_readiness = {**inference_readiness, "status": "blocked",
            "reasons": list(dict.fromkeys([*inference_readiness["reasons"], accounting_reason]))}
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
        "inference_accounting": {key: value for key, value in accounting.items() if key != "operations"},
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
    from src.model_fabric.accounting import assert_current_inference_policy, capture_inference_usage, capture_response_usage
    endpoint = transport_endpoint(profile)
    headers = {"Content-Type": "application/json"}
    if profile.api_key:
        headers["Authorization"] = f"Bearer {profile.api_key}"
    started = time.monotonic()
    embedding_dimension = None
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
                assert_current_inference_policy()
                async with client.stream("POST", endpoint, headers=headers, json=payload) as response:
                    response.raise_for_status()
                    observed = False
                    async for line in response.aiter_lines():
                        if line.strip().startswith("data:"):
                            try:
                                capture_inference_usage(json.loads(line.strip()[5:].strip(), parse_float=Decimal))
                            except (ValueError, TypeError):
                                pass
                        if _streaming_canary_delta(line):
                            observed = True
                if not observed:
                    return CapabilityProbeObservation(False, error_code="stream_empty")
            else:
                assert_current_inference_policy()
                response = await client.post(endpoint, headers=headers, json=payload)
                capture_response_usage(response)
                response.raise_for_status()
                if profile.transport_adapter == "openai_compatible_embeddings":
                    embedding_dimension = _embedding_canary_dimension(response.json())
                    valid = embedding_dimension is not None
                else:
                    valid = _validate_chat_canary_response(response.json(), capability)
                if not valid:
                    return CapabilityProbeObservation(False, error_code="canary_shape_invalid")
    elapsed_ms = max(int((time.monotonic() - started) * 1000), 0)
    value = embedding_dimension if capability == ModelCapability.EMBEDDING.value else _proven_value(capability, elapsed_ms)
    if value is None:
        return CapabilityProbeObservation(False, error_code="proof_value_unknown")
    return CapabilityProbeObservation(True, proven_value=value)


def _chat_canary_payload(model: str, capability: str, *, options=None, output_tokens=64) -> dict[str, object]:
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
        max_tokens=output_tokens,
        options=options,
        stream=True if streaming else None,
        additional_fields=additional_fields,
    )


def _embedding_canary_dimension(payload: object) -> int | None:
    """Measure one bounded finite vector without retaining its contents."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list) or len(payload["data"]) != 1:
        return None
    row = payload["data"][0]
    if not isinstance(row, dict) or type(row.get("index")) is not int or row["index"] != 0:
        return None
    vector = row.get("embedding")
    if not isinstance(vector, list) or not 1 <= len(vector) <= 65_536:
        return None
    try:
        if any(type(value) not in {int, float} or not math.isfinite(value) for value in vector):
            return None
    except OverflowError:
        return None
    if not any(value != 0 for value in vector):
        return None
    return len(vector)


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
    if host in {"127.0.0.1", "::1", "localhost", "testclient"}:
        return True
    # The endpoint is already behind OperatorAuthMiddleware, which validates
    # the configured host/origin allow-list and binds a server-minted
    # principal. Permit an authenticated operator on the configured LAN
    # profile without treating arbitrary remote callers as local. This keeps
    # the keyless setup path usable across the frontend/backend ports while
    # preserving the existing auth and origin boundary.
    operator = getattr(getattr(request, "state", None), "operator", None)
    return bool(getattr(operator, "principal", None) and getattr(operator.principal, "authenticated", False))
