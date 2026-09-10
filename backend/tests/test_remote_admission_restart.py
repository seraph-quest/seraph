"""End-to-end local proof for durable remote-admission operation fencing."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import time
from types import SimpleNamespace

import pytest

from src.model_fabric import (
    EndpointClass,
    InferenceRequestContext,
    InferenceRequirements,
    InferenceWorkload,
    ModelCapability,
    ProviderProfile,
    RemoteInferenceAdmissionBroker,
    RemoteInferenceReceiptBinding,
    RemoteInferenceAdmissionUncertainError,
    bind_remote_inference_receipt,
    candidate_from_profile,
    execute_streaming,
    execute_sync_adapter,
    reset_remote_inference_receipt_binding,
    set_remote_inference_receipt_binding,
)
from src.model_fabric.contracts import OPENROUTER_API_BASE
from src.llm_runtime import _execute_sync_with_gpu_admission
from src.model_fabric.proofs import build_model_route_proof
from src.security.trust_contract import (
    AuthorityGrant,
    ContentOrigin,
    EgressClass,
    PrincipalType,
    TrustPrincipal,
    TrustProvenance,
    canonical_digest,
)
from src.workflows.job_runtime import (
    DurableJobIdentity,
    DurableJobRepository,
    DurableJobSpec,
    DurableJobIdempotencyConflict,
    durable_job_repository,
)


OWNER_ID = "service:remote-inference"
LEASE_OWNER = "runner:remote-inference"
SESSION_ID = "remote-inference-session"


def _profile(*, now: float) -> ProviderProfile:
    return ProviderProfile(
        id="fixture-openrouter-text",
        provider_kind="openrouter",
        model="openai/gpt-4o-mini",
        api_base=OPENROUTER_API_BASE,
        secret_env="OPENROUTER_API_KEY",
        options={
            "provider": {
                "only": ["openai"],
                "allow_fallbacks": False,
                "require_parameters": True,
                "data_collection": "deny",
                "data_retention_policy": "deny",
            }
        },
        capabilities=(ModelCapability.TEXT.value,),
        task_class="interactive_chat",
        task_classes=("interactive_chat",),
        transport_adapter="openai_compatible_chat",
        context_window_tokens=8192,
        max_output_tokens=1024,
        max_latency_ms=5000,
        cost_microusd=1,
        cost_source="fixture",
        cost_source_updated_at=now,
    )


def _context(*, job_id: str, request_id: str, profile: ProviderProfile) -> InferenceRequestContext:
    payload = ({"role": "user", "content": "fixture request"},)
    principal = TrustPrincipal(
        principal_id=OWNER_ID,
        principal_type=PrincipalType.SERVICE,
        grants=(AuthorityGrant.MODEL_INFERENCE,),
        session_id=SESSION_ID,
        job_id=job_id,
    )
    transformation = canonical_digest({"fixture": "redacted-v1"})
    return InferenceRequestContext(
        principal=principal,
        session_id=SESSION_ID,
        job_id=job_id,
        provenance=(
            TrustProvenance(
                origin=ContentOrigin.CANONICAL_MEMORY,
                source_id="remote-admission-fixture",
                data_digest=canonical_digest(payload),
                egress_class=EgressClass.CLOUD_ALLOWED_REDACTED,
            ),
        ),
        data_digest=canonical_digest(payload),
        egress_class=EgressClass.CLOUD_ALLOWED_REDACTED,
        transformation_digest=transformation,
        request_id=request_id,
        runtime_path="chat_agent",
        workload=InferenceWorkload.INTERACTIVE,
        requirements=InferenceRequirements(
            capabilities=(ModelCapability.TEXT.value,),
            context_tokens=64,
            output_tokens=64,
            max_cost_microusd=100,
            max_local_resource_ms=None,
            max_latency_ms=5000,
            task_class="interactive_chat",
        ),
        deadline_at=time.time() + 120,
        fallback_allowed=False,
        gpu_priority="interactive",
        allowed_profile_ids=(profile.id,),
        allowed_provider_kinds=("openrouter",),
        redaction_applied=True,
        estimated_cost_microusd=64,
        owner_budget_microusd=100,
    )


def _proofs(profile: ProviderProfile, *, now: float):
    candidate = candidate_from_profile(profile)
    return tuple(
        build_model_route_proof(
            profile=profile,
            endpoint_class=EndpointClass.REMOTE,
            adapter=profile.transport_adapter,
            capability=capability,
            canary_version="fixture-v1",
            outcome="passed",
            checked_at=now - 1,
            expires_at=now + 120,
            probe_receipt_id=f"fixture-{capability}",
            probe_receipt_hash="a" * 64,
            proven_value=value,
        )
        for capability, value in (
            (ModelCapability.TEXT.value, "supported"),
            ("health", "healthy"),
            ("latency_ms", 5000),
        )
    )


def _spec(job_id: str, *, dedupe_key: str) -> DurableJobSpec:
    return DurableJobSpec(
        identity=DurableJobIdentity(
            job_id=job_id,
            owner_kind="service",
            owner_principal_id=OWNER_ID,
            job_kind="remote_inference_fixture",
            capability_version="remote-text-v1",
            idempotency_scope="remote-inference-fixture",
            idempotency_key=dedupe_key,
        ),
        inputs={"prompt": "redacted fixture", "secret_token": "must-not-persist"},
        session_id=SESSION_ID,
        priority=90,
        resource_claims=("remote_inference",),
        declared_authority={
            "principal": OWNER_ID,
            "service_id": OWNER_ID,
        },
        deadline_at=datetime.now(timezone.utc) + timedelta(seconds=120),
        max_attempts=1,
        service_id=OWNER_ID,
    )


async def _admit_and_claim(job_id: str, *, dedupe_key: str):
    admitted = await durable_job_repository.admit_job(_spec(job_id, dedupe_key=dedupe_key))
    await durable_job_repository.queue_job(job_id)
    claimed = await durable_job_repository.claim_job(job_id, owner=LEASE_OWNER)
    return admitted, claimed


def _remote_effect(job: dict[str, object]) -> dict[str, object]:
    effects = [item for item in job["effects"] if item["effect_type"] == "remote_inference_admission"]
    assert len(effects) == 1
    return effects[0]


@pytest.mark.asyncio
async def test_sync_injected_transport_round_trip_is_durably_fenced(async_db):
    now = time.time()
    profile = _profile(now=now)
    context = _context(job_id="job-744-sync", request_id="request-744-sync", profile=profile)
    proofs = _proofs(profile, now=now)
    _admitted, claimed = await _admit_and_claim("job-744-sync", dedupe_key="sync")
    calls: list[dict[str, object]] = []

    def transport(candidate, stream):
        calls.append({"model": candidate.profile.model, "stream": stream})
        return {"choices": [{"message": {"content": "fixture-response"}}]}

    with bind_remote_inference_receipt(
        repository=durable_job_repository,
        job_id=context.job_id,
        owner=LEASE_OWNER,
        fencing_token=claimed["lease"]["fencing_token"],
    ):
        result = execute_sync_adapter(
            context=context,
            candidates=(candidate_from_profile(profile),),
            proofs=proofs,
            adapter=transport,
            now=now,
        )

    assert result["choices"][0]["message"]["content"] == "fixture-response"
    assert calls == [{"model": profile.model, "stream": False}]
    job = await durable_job_repository.get_job(context.job_id)
    effect = _remote_effect(job)
    assert effect["status"] == "succeeded"
    assert effect["details"]["admission_status"] == "succeeded"
    assert "fixture-response" not in str(job)
    assert "secret_token" not in str(job)
    assert effect["details"]["receipt"]["resource_class"] == "remote_inference"


@pytest.mark.asyncio
async def test_llm_runtime_sync_caller_uses_the_same_durable_fence(async_db, monkeypatch):
    # The runtime wrapper and the lower-level execution seam share the same
    # broker contract. Keep this injected callback local and isolate its
    # broker so no unresolved state can leak between tests.
    broker = RemoteInferenceAdmissionBroker()
    monkeypatch.setattr("src.llm_runtime.gpu_admission_broker", broker)
    now = time.time()
    profile = _profile(now=now)
    context = _context(job_id="job-744-llm-runtime", request_id="request-744-llm-runtime", profile=profile)
    _admitted, claimed = await _admit_and_claim("job-744-llm-runtime", dedupe_key="llm-runtime")
    calls = 0

    def injected_transport():
        nonlocal calls
        calls += 1
        return {"choices": [{"message": {"content": "runtime-fixture"}}]}

    with bind_remote_inference_receipt(
        repository=durable_job_repository,
        job_id=context.job_id,
        owner=LEASE_OWNER,
        fencing_token=claimed["lease"]["fencing_token"],
    ):
        result = await asyncio.to_thread(
            _execute_sync_with_gpu_admission,
            context=context,
            decision=SimpleNamespace(selected=SimpleNamespace(profile=profile)),
            operation_id="fresh-attempt-id-must-not-be-used",
            operation=injected_transport,
        )

    assert result["choices"][0]["message"]["content"] == "runtime-fixture"
    assert calls == 1
    job = await durable_job_repository.get_job(context.job_id)
    effect = _remote_effect(job)
    assert effect["status"] == "succeeded"
    assert effect["target_digest"].startswith("remote:job-744-llm-runtime:")
    assert effect["target_digest"] == effect["adapter_idempotency_key"]


@pytest.mark.asyncio
async def test_streaming_injected_transport_round_trip_uses_same_durable_fence(async_db):
    now = time.time()
    profile = _profile(now=now)
    context = _context(job_id="job-744-stream", request_id="request-744-stream", profile=profile)
    proofs = _proofs(profile, now=now)
    _admitted, claimed = await _admit_and_claim("job-744-stream", dedupe_key="stream")
    calls: list[dict[str, object]] = []

    async def transport(candidate, body, follow_redirects):
        calls.append(
            {
                "model": candidate.profile.model,
                "stream": body["stream"],
                "follow_redirects": follow_redirects,
            }
        )
        yield "fixture-"
        yield "stream"

    class Hooks:
        async def attempt_started(self, **_kwargs):
            return None

        async def attempt_finished(self, **_kwargs):
            return None

    with bind_remote_inference_receipt(
        repository=durable_job_repository,
        job_id=context.job_id,
        owner=LEASE_OWNER,
        fencing_token=claimed["lease"]["fencing_token"],
    ):
        chunks = [
            item
            async for item in execute_streaming(
                context=context,
                candidates=(candidate_from_profile(profile),),
                proofs=proofs,
                messages=({"role": "user", "content": "fixture request"},),
                transport=transport,
                hooks=Hooks(),
                temperature=0.0,
                max_tokens=64,
                now=now,
            )
        ]

    assert chunks == ["fixture-", "stream"]
    assert calls == [{"model": profile.model, "stream": True, "follow_redirects": False}]
    job = await durable_job_repository.get_job(context.job_id)
    effect = _remote_effect(job)
    assert effect["status"] == "succeeded"
    assert effect["details"]["receipt"]["status"] == "succeeded"


@pytest.mark.asyncio
async def test_provider_error_persists_uncertain_blocked_receipt_without_fallback(async_db, monkeypatch):
    from src.model_fabric import execution

    # Keep the intentionally unresolved provider outcome isolated from later
    # tests; the production broker correctly holds its active lease until an
    # operator reconciliation settles the unknown cost.
    monkeypatch.setattr(execution, "gpu_admission_broker", RemoteInferenceAdmissionBroker())
    now = time.time()
    profile = _profile(now=now)
    context = _context(job_id="job-744-provider-error", request_id="request-744-provider-error", profile=profile)
    proofs = _proofs(profile, now=now)
    _admitted, claimed = await _admit_and_claim("job-744-provider-error", dedupe_key="provider-error")
    calls = 0

    def transport(_candidate, _stream):
        nonlocal calls
        calls += 1
        raise RuntimeError("fixture provider failure")

    with bind_remote_inference_receipt(
        repository=durable_job_repository,
        job_id=context.job_id,
        owner=LEASE_OWNER,
        fencing_token=claimed["lease"]["fencing_token"],
    ):
        with pytest.raises(RemoteInferenceAdmissionUncertainError):
            execute_sync_adapter(
                context=context,
                candidates=(candidate_from_profile(profile),),
                proofs=proofs,
                adapter=transport,
                now=now,
            )

    assert calls == 1
    job = await durable_job_repository.get_job(context.job_id)
    effect = _remote_effect(job)
    assert effect["status"] == "blocked"
    assert effect["details"]["admission_status"] == "blocked"
    assert effect["details"]["receipt"]["reconciliation_required"] is True


@pytest.mark.asyncio
async def test_restart_identity_fence_rejects_duplicate_dispatch(async_db):
    now = time.time()
    profile = _profile(now=now)
    context = _context(job_id="job-744-restart", request_id="request-744-restart", profile=profile)
    proofs = _proofs(profile, now=now)
    _admitted, claimed = await _admit_and_claim("job-744-restart", dedupe_key="restart")
    calls = 0

    def transport(_candidate, _stream):
        nonlocal calls
        calls += 1
        return {"choices": [{"message": {"content": "once"}}]}

    fence = claimed["lease"]["fencing_token"]
    with bind_remote_inference_receipt(
        repository=durable_job_repository,
        job_id=context.job_id,
        owner=LEASE_OWNER,
        fencing_token=fence,
    ):
        first = execute_sync_adapter(
            context=context,
            candidates=(candidate_from_profile(profile),),
            proofs=proofs,
            adapter=transport,
            now=now,
        )
    assert first["choices"][0]["message"]["content"] == "once"
    assert calls == 1

    # A fresh process has no broker history, but the durable effect identity
    # remains.  A new binding therefore fails before the injected transport.
    replacement_repository = DurableJobRepository()
    with bind_remote_inference_receipt(
        repository=replacement_repository,
        job_id=context.job_id,
        owner=LEASE_OWNER,
        fencing_token=fence,
    ):
        with pytest.raises(ValueError, match="durable remote inference"):
            execute_sync_adapter(
                context=context,
                candidates=(candidate_from_profile(profile),),
                proofs=proofs,
                adapter=transport,
                now=now,
            )
    assert calls == 1

    job = await durable_job_repository.get_job(context.job_id)
    assert _remote_effect(job)["status"] == "succeeded"


@pytest.mark.asyncio
async def test_queued_cancelled_job_never_reaches_injected_transport(async_db):
    now = time.time()
    profile = _profile(now=now)
    context = _context(job_id="job-744-cancelled", request_id="request-744-cancelled", profile=profile)
    proofs = _proofs(profile, now=now)
    admitted = await durable_job_repository.admit_job(
        _spec("job-744-cancelled", dedupe_key="cancelled")
    )
    await durable_job_repository.queue_job(admitted["job_id"])
    cancelled = await durable_job_repository.cancel_job(admitted["job_id"])
    assert cancelled["status"] == "cancelled"
    calls = 0

    def transport(_candidate, _stream):
        nonlocal calls
        calls += 1
        return {"choices": [{"message": {"content": "must-not-run"}}]}

    # A cancelled durable job has no valid lease. A stale binding still fails
    # at the pre-dispatch intent fence, so the injected transport is untouched.
    token = set_remote_inference_receipt_binding(
        RemoteInferenceReceiptBinding(
            repository=durable_job_repository,
            job_id=context.job_id,
            owner=LEASE_OWNER,
            fencing_token=1,
        )
    )
    try:
        with pytest.raises(ValueError, match="durable remote inference"):
            execute_sync_adapter(
                context=context,
                candidates=(candidate_from_profile(profile),),
                proofs=proofs,
                adapter=transport,
                now=now,
            )
    finally:
        reset_remote_inference_receipt_binding(token)
    assert calls == 0
