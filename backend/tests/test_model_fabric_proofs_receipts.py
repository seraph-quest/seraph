from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import time

import pytest
from sqlalchemy import text

from config.settings import settings
from src.model_fabric import (
    EndpointClass,
    InferenceRequestContext,
    InferenceRequirements,
    InferenceWorkload,
    ProviderProfile,
    RouteDecision,
    candidate_from_profile,
)
from src.model_fabric.hooks import PersistedRouteReceiptHooks, RouteReceiptSession, persist_denied_route
from src.model_fabric.probe import CapabilityProbeObservation, run_capability_probe
from src.model_fabric.proofs import (
    build_model_route_proof,
    proof_is_fresh,
    validate_model_route_proof,
)
from src.model_fabric.receipts import (
    CostEstimate,
    ReceiptPersistenceResult,
    RouteAttemptReceipt,
    RouteReceipt,
    TokenUsage,
)
from src.model_fabric.repository import (
    ModelFabricRepository,
    ProofPersistenceResult,
    _attempt_from_record,
    _attempt_record,
)
from src.security.trust_contract import (
    AuthorityGrant,
    ContentOrigin,
    DigestSentinel,
    EgressClass,
    PrincipalType,
    TrustPrincipal,
    TrustProvenance,
    canonical_digest,
)


@pytest.fixture(autouse=True)
def allow_legacy_probe_contract_fixture(monkeypatch):
    """Exercise transport-free probe receipts without selecting a live route.

    These tests validate the lower-level receipt/proof state machine with
    synthetic historical profiles.  Active production selection remains
    OpenRouter-only and is covered by the focused policy suites.
    """
    monkeypatch.setattr(settings, "openrouter_provider_only", False)


def _profile(*, endpoint: str = "http://192.168.1.26:8001/v1") -> ProviderProfile:
    remote = endpoint.startswith("https://")
    return ProviderProfile(
        id="local-gemma",
        provider_kind="openai_compatible",
        model="gemma-test",
        api_base=endpoint,
        transport_adapter="openai_compatible_chat",
        keyless=True,
        capabilities=("structured_output",),
        context_window_tokens=1024,
        max_output_tokens=128,
        max_latency_ms=1000,
        task_class="probe",
        task_classes=("probe",),
        local_resource_ms=None if remote else 500,
        cost_microusd=100 if remote else None,
        cost_source="test_pricing" if remote else None,
        cost_source_updated_at=time.time() - 10 if remote else None,
    )


def _context(profile: ProviderProfile, *, now: float, workload=InferenceWorkload.CAPABILITY_PROBE):
    data_digest = canonical_digest({"fixture": "non-sensitive-canary"})
    remote = profile.api_base.startswith("https://")
    return InferenceRequestContext(
        principal=TrustPrincipal(
            principal_id="seraph-probe",
            principal_type=PrincipalType.SERAPH_RUNTIME,
            grants=(AuthorityGrant.MODEL_INFERENCE,),
            session_id="session-probe",
        ),
        session_id="session-probe",
        job_id="",
        provenance=(
            TrustProvenance(
                origin=ContentOrigin.SERAPH_CONTROL,
                source_id="canary-fixture-v1",
                data_digest=data_digest,
                egress_class=EgressClass.LOCAL_ONLY,
            ),
        ),
        data_digest=data_digest,
        egress_class=EgressClass.LOCAL_ONLY,
        transformation_digest=DigestSentinel.NO_TRANSFORMATION,
        request_id=f"request-{workload.value}-1",
        runtime_path=workload.value,
        workload=workload,
        requirements=InferenceRequirements(
            capabilities=("structured_output",),
            context_tokens=128,
            output_tokens=32,
            max_cost_microusd=1000 if remote else None,
            max_local_resource_ms=None if remote else 1000,
            max_latency_ms=1000,
            task_class="probe",
        ),
        deadline_at=now + 5,
        requested_profile_id=profile.id if workload is InferenceWorkload.CAPABILITY_PROBE else "",
    )


def _proof(
    *,
    checked_at: float = 100.0,
    expires_at: float = 200.0,
    proven_value=None,
    outcome: str = "passed",
    probe_receipt_id: str = "probe-receipt-1",
    probe_receipt_hash: str = "b" * 64,
):
    profile = _profile()
    return build_model_route_proof(
        profile=profile,
        endpoint_class=EndpointClass.TRUSTED_LAN,
        adapter="openai_compatible_chat",
        capability="structured_output",
        canary_version="canary-v1",
        outcome=outcome,
        checked_at=checked_at,
        expires_at=expires_at,
        probe_receipt_id=probe_receipt_id,
        probe_receipt_hash=probe_receipt_hash,
        proven_value=proven_value,
    )


def _attempt(*, endpoint: str = "https://models.example/v1", outcome: str = "succeeded"):
    started = datetime(2026, 7, 10, 10, 0, tzinfo=timezone.utc)
    return RouteAttemptReceipt(
        attempt_id="attempt-1",
        attempt_index=0,
        profile_id="remote-1",
        model="model-1",
        endpoint=endpoint,
        adapter="openai_compatible_chat",
        destination_class="remote",
        egress_class="cloud_allowed_full",
        trust_decision_id="decision-1",
        capability_proof_hashes=("a" * 64,),
        outcome=outcome,
        error_code=None if outcome == "succeeded" else "provider_unavailable",
        started_at=started,
        finished_at=started + timedelta(milliseconds=12),
        latency_ms=12,
    )


def test_provider_profile_rejects_unknown_secret_ref(monkeypatch):
    monkeypatch.setenv("UNRELATED_SENSITIVE_TOKEN", "must-not-escape")
    profile = replace(_profile(), secret_env="UNRELATED_SENSITIVE_TOKEN", keyless=False)

    with pytest.raises(ValueError, match="unsupported secret"):
        _ = profile.api_key


@pytest.mark.asyncio
async def test_denied_route_persists_zero_attempt_receipt():
    now = time.time()
    profile = _profile()
    context = _context(profile, now=now, workload=InferenceWorkload.INTERACTIVE)
    decision = RouteDecision(
        selected=None,
        trust_request_digest=None,
        trust_decision_id=None,
        rejections=(),
        route_decision_id="route:denied-test",
    )
    class CapturingRepository:
        receipt = None

        async def persist_route_receipt(self, receipt):
            self.receipt = receipt
            return ReceiptPersistenceResult.success(receipt)

    repository = CapturingRepository()

    result = await persist_denied_route(
        context=context,
        decision=decision,
        reason_codes=("no_compliant_route", "proof_missing:text"),
        repository=repository,
    )

    assert result.persisted is True
    receipt = repository.receipt
    assert receipt.attempts == ()
    assert receipt.fallback_reason_code == "no_compliant_route"
    assert receipt.degradation_codes == ("no_compliant_route", "proof_missing:text")
    assert receipt.usage == TokenUsage()


@pytest.mark.asyncio
async def test_attempt_usage_cost_and_degradation_round_trip():
    attempt = replace(
        _attempt(),
        usage=TokenUsage(input_tokens=12, output_tokens=5, total_tokens=17),
        cost=CostEstimate(
            kind="estimated",
            amount=0.002,
            currency="USD",
            source="provider_pricing",
            source_updated_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        ),
        degradation_code="fallback_used",
    )
    loaded = _attempt_from_record(_attempt_record("receipt-usage", attempt))
    assert loaded.usage == attempt.usage
    assert loaded.cost == attempt.cost
    assert loaded.degradation_code == "fallback_used"


def _receipt(*, receipt_id: str = "receipt-1", workload: str = "interactive", finished_offset: int = 12):
    attempt = replace(_attempt(), attempt_id=f"attempt-{receipt_id}")
    return RouteReceipt(
        receipt_id=receipt_id,
        request_id=f"request-{receipt_id}",
        route_decision_id=f"decision-{receipt_id}",
        runtime_path=workload,
        workload=workload,
        outcome="succeeded",
        egress_class="cloud_allowed_full",
        actual_profile_id="remote-1",
        actual_model="model-1",
        actual_adapter="openai_compatible_chat",
        destination_class="remote",
        trust_decision_id="decision-1",
        cost=CostEstimate(
            kind="estimated",
            amount=0.001,
            currency="USD",
            source="provider_pricing",
            source_updated_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        ),
        started_at=attempt.started_at,
        finished_at=attempt.started_at + timedelta(milliseconds=finished_offset),
        latency_ms=finished_offset,
        attempts=(attempt,),
    )


def test_capability_proof_hash_freshness_and_exact_binding():
    proof = _proof(proven_value=8192)

    validate_model_route_proof(proof)
    assert proof_is_fresh(proof, now=150.0) is True
    assert proof_is_fresh(proof, now=200.0) is False
    assert proof_is_fresh(replace(proof, model="other-model"), now=150.0) is False


@pytest.mark.asyncio
async def test_repository_round_trips_proven_value_and_latest_success(async_db):
    repository = ModelFabricRepository(async_db)
    probe_receipt = _receipt(receipt_id="probe-receipt-1", workload="capability_probe")
    assert (await repository.persist_route_receipt(probe_receipt)).persisted is True
    proof = _proof(
        proven_value="json_object",
        probe_receipt_id=probe_receipt.receipt_id,
        probe_receipt_hash=probe_receipt.receipt_hash,
    )
    proof_result = await repository.persist_capability_proof(proof)
    assert proof_result.persisted is True

    loaded = await repository.latest_capability_proof(
        profile_schema_version=proof.profile_schema_version,
        profile_contract_hash=proof.profile_contract_hash,
        profile_id=proof.profile_id,
        model=proof.model,
        endpoint=proof.endpoint,
        endpoint_class=proof.endpoint_class,
        adapter=proof.adapter,
        capability=proof.capability,
    )
    assert loaded == proof

    first = _receipt(receipt_id="receipt-first", finished_offset=10)
    second = _receipt(receipt_id="receipt-second", finished_offset=20)
    assert (await repository.persist_route_receipt(first)).persisted is True
    assert (await repository.persist_route_receipt(second)).persisted is True
    latest = await repository.latest_successful_route(workload="interactive")
    assert latest is not None
    assert latest.receipt_id == "receipt-second"
    assert latest.receipt_hash == second.receipt_hash


@pytest.mark.asyncio
async def test_bulk_proof_loader_rejects_unbound_or_hash_mismatched_proof(async_db):
    repository = ModelFabricRepository(async_db)
    unbound = _proof(
        proven_value="verified",
        probe_receipt_id="missing-probe-receipt",
        probe_receipt_hash="f" * 64,
    )
    assert (await repository.persist_capability_proof(unbound)).persisted is True

    assert await repository.latest_capability_proofs_for_profiles((unbound.profile_id,)) == ()

    receipt = _receipt(receipt_id="bound-probe", workload="capability_probe")
    assert (await repository.persist_route_receipt(receipt)).persisted is True
    wrong_hash = _proof(
        proven_value="healthy",
        probe_receipt_id=receipt.receipt_id,
        probe_receipt_hash="e" * 64,
    )
    assert (await repository.persist_capability_proof(wrong_hash)).persisted is True
    assert await repository.latest_capability_proofs_for_profiles((wrong_hash.profile_id,)) == ()

    succeeded_receipt = _receipt(receipt_id="wrong-outcome-probe", workload="capability_probe")
    assert (await repository.persist_route_receipt(succeeded_receipt)).persisted is True
    failed_proof = _proof(
        outcome="failed",
        probe_receipt_id=succeeded_receipt.receipt_id,
        probe_receipt_hash=succeeded_receipt.receipt_hash,
    )
    assert (await repository.persist_capability_proof(failed_proof)).persisted is True
    assert await repository.latest_capability_proofs_for_profiles((failed_proof.profile_id,)) == ()


@pytest.mark.asyncio
async def test_receipt_storage_strips_secret_canaries_and_rejects_raw_errors(async_db):
    secret = "sk-secret-canary"
    attempt = _attempt(
        endpoint=f"https://user:{secret}@models.example/v1?api_key={secret}#fragment"
    )
    receipt = replace(_receipt(), attempts=(attempt,))
    repository = ModelFabricRepository(async_db)
    assert (await repository.persist_route_receipt(receipt)).persisted is True

    async with async_db() as db:
        route_row = (await db.execute(text("SELECT * FROM model_route_receipts"))).mappings().one()
        attempt_row = (await db.execute(text("SELECT * FROM model_route_attempt_receipts"))).mappings().one()
    serialized = json.dumps({"route": dict(route_row), "attempt": dict(attempt_row)}, default=str)
    assert secret not in serialized
    assert attempt_row["endpoint"] == "https://models.example/v1"

    with pytest.raises(ValueError, match="bounded safe identifier"):
        replace(attempt, outcome="failed", error_code=f"raw failure included {secret}")


@pytest.mark.asyncio
async def test_persistence_failure_is_explicitly_degraded():
    @asynccontextmanager
    async def broken_session():
        raise RuntimeError("database unavailable")
        yield

    repository = ModelFabricRepository(broken_session)
    result = await repository.persist_route_receipt(_receipt())

    assert result.persisted is False
    assert result.status == "degraded"
    assert result.receipt_hash is None
    assert result.error_code == "receipt_persistence_failed"


@pytest.mark.asyncio
async def test_capability_probe_bootstraps_without_existing_proof_and_returns_no_output(async_db):
    now = time.time()
    profile = _profile()
    candidate = candidate_from_profile(profile)
    context = _context(profile, now=now)

    async def transport(_candidate, _trust_request, requirements):
        assert requirements.output_tokens == 32
        return CapabilityProbeObservation(True, proven_value="json_object")

    repository = ModelFabricRepository(async_db)
    result = await run_capability_probe(
        context=context,
        candidate=candidate,
        capability="structured_output",
        canary_version="canary-v1",
        proof_ttl_seconds=60,
        transport=transport,
        repository=repository,
        now=now,
    )

    assert result.outcome == "passed"
    assert result.proof is not None
    assert result.proof_persistence is not None and result.proof_persistence.persisted is True
    assert not hasattr(result, "output")
    loaded_proof = await repository.latest_capability_proof(
        profile_schema_version=result.proof.profile_schema_version,
        profile_contract_hash=result.proof.profile_contract_hash,
        profile_id=result.proof.profile_id,
        model=result.proof.model,
        endpoint=result.proof.endpoint,
        endpoint_class=result.proof.endpoint_class,
        adapter=result.proof.adapter,
        capability=result.proof.capability,
    )
    assert loaded_proof == result.proof
    latest = await repository.latest_successful_route(workload="capability_probe")
    assert latest is not None
    assert latest.cost.kind == "local_resource"


@pytest.mark.asyncio
async def test_health_canary_runs_before_health_is_declared_or_proven():
    now = time.time()
    profile = replace(_profile(), capabilities=("structured_output",))
    candidate = candidate_from_profile(profile)
    context = replace(
        _context(profile, now=now),
        requirements=replace(_context(profile, now=now).requirements, capabilities=("health",)),
    )
    transported = False

    async def transport(_candidate, _trust_request, _requirements):
        nonlocal transported
        transported = True
        return CapabilityProbeObservation(True, proven_value="healthy")

    class Repository:
        async def persist_route_receipt(self, receipt):
            return ReceiptPersistenceResult.success(receipt)

        async def persist_capability_proof(self, proof):
            return ProofPersistenceResult(proof.proof_hash, "persisted", True)

    result = await run_capability_probe(
        context=context,
        candidate=candidate,
        capability="health",
        canary_version="health-v1",
        proof_ttl_seconds=60,
        transport=transport,
        repository=Repository(),
        now=now,
    )

    assert transported is True
    assert result.outcome == "passed"
    assert result.proof is not None and result.proof.capability == "health"


@pytest.mark.asyncio
async def test_failed_probe_is_persisted_but_never_returned_as_authority(async_db):
    now = time.time()
    profile = _profile()
    repository = ModelFabricRepository(async_db)

    async def transport(_candidate, _trust_request, _requirements):
        return CapabilityProbeObservation(False, error_code="schema_mismatch")

    result = await run_capability_probe(
        context=_context(profile, now=now),
        candidate=candidate_from_profile(profile),
        capability="structured_output",
        canary_version="canary-v1",
        proof_ttl_seconds=60,
        transport=transport,
        repository=repository,
        now=now,
    )

    assert result.outcome == "failed"
    assert result.proof is None
    assert result.proof_persistence is not None and result.proof_persistence.persisted is True
    candidate = candidate_from_profile(profile)
    authorizing = await repository.latest_capability_proof(
        profile_schema_version=profile.schema_version,
        profile_contract_hash=profile.contract_hash,
        profile_id=profile.id,
        model=profile.model,
        endpoint=candidate.endpoint,
        endpoint_class=candidate.endpoint_class,
        adapter=candidate.adapter,
        capability="structured_output",
    )
    assert authorizing is None
    operator_evidence = await repository.latest_capability_proofs_for_profiles((profile.id,))
    assert len(operator_evidence) == 1
    assert operator_evidence[0].outcome == "failed"


@pytest.mark.asyncio
async def test_probe_persistence_degradation_never_returns_authorizing_proof():
    @asynccontextmanager
    async def broken_session():
        raise RuntimeError("database unavailable")
        yield

    now = time.time()
    profile = _profile()

    async def transport(_candidate, _trust_request, _requirements):
        return CapabilityProbeObservation(True, proven_value="json_object")

    result = await run_capability_probe(
        context=_context(profile, now=now),
        candidate=candidate_from_profile(profile),
        capability="structured_output",
        canary_version="canary-v1",
        proof_ttl_seconds=60,
        transport=transport,
        repository=ModelFabricRepository(broken_session),
        now=now,
    )

    assert result.route_persistence.status == "degraded"
    assert result.proof is None
    assert result.proof_persistence is None
    assert result.error_code == "receipt_persistence_failed"


@pytest.mark.asyncio
async def test_denied_remote_probe_preserves_actual_trust_decision_id(async_db):
    now = time.time()
    profile = _profile(endpoint="https://models.example/v1")
    repository = ModelFabricRepository(async_db)

    async def transport(_candidate, _trust_request, _requirements):
        raise AssertionError("denied probe must not execute transport")

    result = await run_capability_probe(
        context=_context(profile, now=now),
        candidate=candidate_from_profile(profile),
        capability="structured_output",
        canary_version="canary-v1",
        proof_ttl_seconds=60,
        transport=transport,
        repository=repository,
        now=now,
    )

    assert result.outcome == "denied"
    assert result.proof is None
    async with async_db() as db:
        stored = (await db.execute(text("SELECT trust_decision_id FROM model_route_receipts"))).scalar_one()
    assert stored not in {"not-evaluated", "probe-denied"}


@pytest.mark.asyncio
async def test_persisted_hooks_finalize_adapter_receipt(async_db):
    now = time.time()
    profile = _profile()
    candidate = candidate_from_profile(profile)
    context = _context(profile, now=now, workload=InferenceWorkload.INTERACTIVE)
    decision = RouteDecision(
        selected=candidate,
        trust_request_digest="c" * 64,
        trust_decision_id="decision-hook",
        rejections=(),
        attempt_id="attempt:trust-evaluated",
        replay_id="replay:trust-evaluated",
        route_decision_id="route:hook",
    )
    repository = ModelFabricRepository(async_db)
    hooks = PersistedRouteReceiptHooks(
        repository=repository,
        capability_proof_hashes=("a" * 64,),
    )

    await hooks.attempt_started(context=context, decision=decision)
    await hooks.attempt_finished(
        context=context,
        decision=decision,
        outcome="succeeded",
        error_code=None,
    )

    persistence = await hooks.persistence_result(context.request_id)
    assert persistence is not None and persistence.persisted is True
    latest = await repository.latest_successful_route(workload="interactive")
    assert latest is not None
    assert latest.actual_profile_id == profile.id
    assert latest.cost.kind == "local_resource"
    assert latest.attempts[0].attempt_id == decision.attempt_id


@pytest.mark.asyncio
async def test_receipt_session_aggregates_primary_failure_and_fallback_success(async_db):
    now = time.time()
    primary_profile = _profile()
    fallback_profile = replace(primary_profile, id="local-fallback", model="gemma-fallback")
    primary = candidate_from_profile(primary_profile)
    fallback = candidate_from_profile(fallback_profile, source="fallback")
    context = _context(primary_profile, now=now, workload=InferenceWorkload.INTERACTIVE)
    repository = ModelFabricRepository(async_db)
    session = RouteReceiptSession(
        context=context,
        repository=repository,
        capability_proof_hashes=("a" * 64,),
    )
    primary_decision = RouteDecision(
        selected=primary,
        trust_request_digest="b" * 64,
        trust_decision_id="decision-primary",
        rejections=(),
        attempt_id="attempt:primary",
        replay_id="replay:primary",
        route_decision_id="route:primary",
    )
    fallback_decision = RouteDecision(
        selected=fallback,
        trust_request_digest="c" * 64,
        trust_decision_id="decision-fallback",
        rejections=(),
        attempt_id="attempt:fallback",
        replay_id="replay:fallback",
        route_decision_id="route:fallback",
    )

    session.attempt_started(
        primary_decision,
        capability_proof_hashes=("a" * 64,),
    )
    session.attempt_finished(
        decision=primary_decision,
        outcome="failed",
        error_code="provider_unavailable",
    )
    session.attempt_started(
        fallback_decision,
        capability_proof_hashes=("b" * 64,),
    )
    session.attempt_finished(
        decision=fallback_decision,
        outcome="succeeded",
        error_code=None,
    )
    result = await session.finalize(
        outcome="succeeded",
        fallback_reason_code="primary_unavailable",
    )

    assert result.persisted is True
    latest = await repository.latest_successful_route(workload="interactive")
    assert latest is not None
    assert latest.actual_profile_id == fallback_profile.id
    assert latest.fallback_used is True
    assert latest.fallback_reason_code == "primary_unavailable"
    assert latest.route_decision_id == "route:fallback"
    assert [attempt.attempt_id for attempt in latest.attempts] == [
        "attempt:primary",
        "attempt:fallback",
    ]
    assert latest.attempts[0].capability_proof_hashes == ("a" * 64,)
    assert latest.attempts[1].capability_proof_hashes == ("b" * 64,)
