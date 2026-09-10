"""End-to-end local proof for durable remote-admission operation fencing."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import time
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

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
    DurableJobLeaseError,
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


async def _admit_and_claim(job_id: str, *, dedupe_key: str, lease_seconds: int = 300):
    admitted = await durable_job_repository.admit_job(_spec(job_id, dedupe_key=dedupe_key))
    await durable_job_repository.queue_job(job_id)
    claimed = await durable_job_repository.claim_job(
        job_id,
        owner=LEASE_OWNER,
        lease_seconds=lease_seconds,
    )
    return admitted, claimed


def _remote_effect(job: dict[str, object]) -> dict[str, object]:
    effects = [
        item
        for item in job["effects"]
        if item["effect_type"] == "remote_inference_admission"
        and item.get("receipt_kind") == "effect"
    ]
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
    assert effect["target_digest"] == "remote:job-744-llm-runtime"
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


@pytest.mark.asyncio
async def test_concurrent_intents_have_one_durable_cas_winner(async_db, monkeypatch, tmp_path):
    """Two workers cannot reserve the same remote effect identity."""
    # The default async_db fixture intentionally uses StaticPool. Two
    # concurrent sessions on that single connection can roll back each
    # other's transaction, so use a temporary file-backed SQLite seam here to
    # exercise the repository CAS with independent connections.
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'remote-intent-race.db'}",
        connect_args={"timeout": 5},
    )
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)

    @asynccontextmanager
    async def file_session():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    monkeypatch.setattr("src.workflows.durable_state.get_session", file_session)
    try:
        _admitted, claimed = await _admit_and_claim(
            "job-744-intent-race",
            dedupe_key="intent-race",
        )
        repository_a = DurableJobRepository()
        repository_b = DurableJobRepository()
        kwargs = {
            "operation_id": "remote:job-744-intent-race",
            "job_id": "job-744-intent-race",
            "owner_id": OWNER_ID,
            "runtime_path": "chat_agent",
            "profile_id": "fixture-openrouter-text",
            "priority": "interactive_chat",
            "capability_version": "remote-text-v1",
            "owner": LEASE_OWNER,
            "fencing_token": claimed["lease"]["fencing_token"],
        }

        results = await asyncio.gather(
            repository_a.record_remote_inference_intent(**kwargs),
            repository_b.record_remote_inference_intent(**kwargs),
            return_exceptions=True,
        )
        successes = [item for item in results if isinstance(item, dict)]
        failures = [item for item in results if isinstance(item, BaseException)]
        assert len(successes) == 1
        assert len(failures) == 1
        assert isinstance(failures[0], (DurableJobLeaseError, DurableJobIdempotencyConflict))
        job = await durable_job_repository.get_job("job-744-intent-race")
        effects = [
            item
            for item in job["effects"]
            if item.get("effect_id") == "remote_inference:remote:job-744-intent-race"
        ]
        assert len(effects) == 1
        assert effects[0]["status"] == "intent"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_remote_intent_rejects_route_profile_drift_for_same_job_identity(async_db):
    """A refreshed route cannot reuse a job-bound operation key."""
    _admitted, claimed = await _admit_and_claim(
        "job-744-route-drift",
        dedupe_key="route-drift",
    )
    repository = DurableJobRepository()
    common = {
        "operation_id": "remote:job-744-route-drift",
        "job_id": "job-744-route-drift",
        "owner_id": OWNER_ID,
        "runtime_path": "chat_agent",
        "priority": "interactive_chat",
        "capability_version": "remote-text-v1",
        "owner": LEASE_OWNER,
        "fencing_token": claimed["lease"]["fencing_token"],
    }
    await repository.record_remote_inference_intent(
        **common,
        profile_id="fixture-openrouter-text-v1",
    )
    with pytest.raises(DurableJobIdempotencyConflict, match="route/profile"):
        await repository.record_remote_inference_intent(
            **common,
            profile_id="fixture-openrouter-text-v2",
        )
    job = await repository.get_job("job-744-route-drift")
    effect = _remote_effect(job)
    assert effect["details"]["profile_id"] == "fixture-openrouter-text-v1"


@pytest.mark.asyncio
async def test_restart_after_terminal_remote_success_recovers_without_dispatch(async_db, monkeypatch):
    """A crash after broker settlement promotes the durable job exactly once."""
    from src.model_fabric import execution

    broker = RemoteInferenceAdmissionBroker()
    monkeypatch.setattr(execution, "gpu_admission_broker", broker)
    now = time.time()
    profile = _profile(now=now)
    context = _context(
        job_id="job-744-terminal-recovery",
        request_id="request-744-terminal-recovery",
        profile=profile,
    )
    proofs = _proofs(profile, now=now)
    _admitted, claimed = await _admit_and_claim(
        "job-744-terminal-recovery",
        dedupe_key="terminal-recovery",
        lease_seconds=1,
    )
    calls = 0

    def transport(_candidate, _stream):
        nonlocal calls
        calls += 1
        return {"choices": [{"message": {"content": "settled-once"}}]}

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
    assert result["choices"][0]["message"]["content"] == "settled-once"
    assert calls == 1
    running = await durable_job_repository.get_job(context.job_id)
    assert running["status"] == "running"
    assert _remote_effect(running)["status"] == "succeeded"
    assert _remote_effect(running)["details"]["profile_id"] == profile.id

    replacement_repository = DurableJobRepository()
    recovered = await replacement_repository.recover_stale_jobs(
        now=datetime.now(timezone.utc) + timedelta(seconds=5),
    )
    assert len(recovered) == 1
    assert recovered[0]["status"] == "succeeded"
    assert recovered[0]["receipt"]["reason"] == "remote_terminal_settlement_recovered"
    assert recovered[0]["receipt"]["operator_action"] == "resume_already_settled_remote_operation"
    settled = await replacement_repository.get_job(context.job_id)
    assert settled["status"] == "succeeded"
    readbacks = [
        item
        for item in settled["effects"]
        if item.get("kind") == "remote_inference_terminal_recovery"
    ]
    assert len(readbacks) == 1
    assert readbacks[0]["receipt_kind"] == "readback"
    assert readbacks[0]["status"] == "succeeded"
    assert readbacks[0]["details"]["verified"] is True
    assert await replacement_repository.recover_stale_jobs(
        now=datetime.now(timezone.utc) + timedelta(seconds=5),
    ) == []

    # A replacement worker has no process-local broker history. The terminal
    # durable state still rejects the operation before a second callback.
    with bind_remote_inference_receipt(
        repository=replacement_repository,
        job_id=context.job_id,
        owner=LEASE_OWNER,
        fencing_token=claimed["lease"]["fencing_token"],
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
