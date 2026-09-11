"""Focused proof for the bounded #743 durable invocation contract."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import update
from sqlmodel import select
from src.db.engine import _ensure_legacy_columns, _map_legacy_workflow_status
from src.db.models import ApprovalRequest, WorkflowRunState

from src.workflows.job_runtime import (
    DURABLE_JOB_STATUSES,
    DURABLE_JOB_TRANSITIONS,
    DurableJobIdentity,
    DurableJobIdempotencyConflict,
    DurableJobSpec,
    DurableJobNotFound,
    DurableJobLeaseError,
    DurableJobRepository,
    DurableJobTransitionError,
    REMOTE_INFERENCE_EFFECT_STATUSES,
    _admission_conflicts,
    _canonical_reconciliation_receipt,
    _canonical_remote_inference_receipt,
    _bounded_effect_ledger,
    _digest,
    _effect_ledger_or_raise,
    _reconciliation_matches_effect,
    _validate_approval_resume_receipt,
    _validate_no_effect_retry_receipt,
    _verified_readback_exists,
    _safe_inputs_digest,
    _safe_structure,
    _validate_admission_authority,
    _validate_retry_actor,
    durable_job_repository,
)


def test_transition_table_is_the_single_normative_lifecycle_contract():
    assert DURABLE_JOB_STATUSES == tuple(DURABLE_JOB_TRANSITIONS)
    assert DURABLE_JOB_TRANSITIONS["accepted"] == frozenset({
        "queued",
        "blocked",
        "unknown_external_effect",
        "cost_liability",
        "failed",
        "cancelled",
    })
    assert DURABLE_JOB_TRANSITIONS["failed"] == frozenset({"queued"})
    assert DURABLE_JOB_TRANSITIONS["succeeded"] == frozenset()
    assert DURABLE_JOB_TRANSITIONS["cancelled"] == frozenset()
    assert "queued" in DURABLE_JOB_TRANSITIONS["awaiting_approval"]


def test_success_requires_verified_capability_readback_and_unresolved_history_is_retained():
    assert not _verified_readback_exists([
        {"receipt_kind": "effect", "status": "succeeded", "effect_type": "write"}
    ])
    assert _verified_readback_exists([
        {
            "receipt_kind": "readback",
            "status": "succeeded",
            "target_path": "workspace:result",
            "content_sha256": "digest",
            "details": {"verified": True},
        }
    ])
    receipts = [
        {"effect_id": "settled-0", "status": "succeeded"},
        {"effect_id": "settled-1", "status": "succeeded"},
        {"effect_id": "intent-1", "status": "intent"},
    ]
    retained = _bounded_effect_ledger(receipts, limit=2)
    assert [item["effect_id"] for item in retained] == ["intent-1", "settled-1"]


def test_corrupt_effect_history_is_not_coerced_to_empty():
    assert _effect_ledger_or_raise(None) == []
    with pytest.raises(DurableJobTransitionError, match="malformed"):
        _effect_ledger_or_raise("not-json")
    with pytest.raises(DurableJobTransitionError, match="malformed"):
        _effect_ledger_or_raise('{"effect": "not-a-list"}')


def test_invalid_legacy_statuses_block_and_idempotency_index_is_unique():
    assert _map_legacy_workflow_status("running") == (
        "blocked",
        "migration_requires_reconciliation",
    )
    assert _map_legacy_workflow_status("future_status") == (
        "blocked",
        "legacy_status_unmapped",
    )
    idempotency_indexes = [
        index
        for index in WorkflowRunState.__table__.indexes
        if index.name == "ux_workflow_run_states_idempotency_binding"
    ]
    assert len(idempotency_indexes) == 1
    assert idempotency_indexes[0].unique is True


def test_admission_binds_authority_and_all_immutable_identity_fields():
    spec = _spec()
    _validate_admission_authority(spec)

    with pytest.raises(ValueError, match="principal"):
        _validate_admission_authority(
            replace(spec, declared_authority={"principal": "service:other", "service_id": "service:strategist"})
        )
    with pytest.raises(ValueError, match="service_id"):
        _validate_admission_authority(replace(spec, service_id=None, declared_authority={"principal": "service:strategist"}))

    input_digest, _ = _safe_inputs_digest(spec.inputs)
    existing = SimpleNamespace(
        run_identity=spec.identity.job_id,
        input_digest=input_digest,
        job_kind=spec.identity.job_kind,
        capability_version=spec.identity.capability_version,
        owner_kind=spec.identity.owner_kind,
        owner_principal_id=spec.identity.owner_principal_id,
        service_id=spec.service_id,
        authority_digest=_digest(spec.declared_authority),
        dependencies_json="[]",
        resource_claims_json='["cpu"]',
        deadline_at=None,
        priority=spec.priority,
        max_attempts=spec.max_attempts,
        session_id=spec.session_id,
        parent_job_id=spec.parent_job_id,
        goal_id=spec.goal_id,
        goal_revision=spec.goal_revision,
        plan_revision=spec.plan_revision,
        candidate_id=spec.candidate_id,
        run_fingerprint=input_digest,
        budget_digest=_digest({"budget_microusd": None}),
        declared_authority_json=json.dumps(spec.declared_authority),
    )
    assert _admission_conflicts(
        existing,
        spec=spec,
        input_digest=input_digest,
        authority_digest=_digest(spec.declared_authority),
        deadline=None,
    ) == []
    existing.run_fingerprint = "changed-fingerprint"
    assert "run_fingerprint" in _admission_conflicts(
        existing,
        spec=spec,
        input_digest=input_digest,
        authority_digest=_digest(spec.declared_authority),
        deadline=None,
    )
    existing.run_fingerprint = input_digest
    existing.budget_digest = _digest({"budget_microusd": 1})
    assert "budget_digest" in _admission_conflicts(
        existing,
        spec=spec,
        input_digest=input_digest,
        authority_digest=_digest(spec.declared_authority),
        deadline=None,
    )
    existing.budget_digest = _digest({"budget_microusd": None})
    existing.priority = 1
    assert _admission_conflicts(
        existing,
        spec=spec,
        input_digest=input_digest,
        authority_digest=_digest(spec.declared_authority),
        deadline=None,
    ) == ["priority"]
    existing.priority = spec.priority

    immutable_mutations = (
        ("job_kind", "other_kind", "job_kind"),
        ("capability_version", "2", "capability_version"),
        ("authority_digest", "other-authority", "authority_digest"),
        ("dependencies_json", '["other-dependency"]', "dependencies"),
        ("resource_claims_json", '["gpu"]', "resource_claims"),
        ("deadline_at", datetime.now(timezone.utc), "deadline_at"),
        ("owner_kind", "user", "owner_kind"),
        ("owner_principal_id", "user:other", "owner_principal_id"),
        ("service_id", "service:other", "service_id"),
    )
    for field_name, changed_value, conflict_name in immutable_mutations:
        setattr(existing, field_name, changed_value)
        conflicts = _admission_conflicts(
            existing,
            spec=spec,
            input_digest=input_digest,
            authority_digest=_digest(spec.declared_authority),
            deadline=None,
        )
        assert conflict_name in conflicts
        setattr(existing, field_name, {
            "job_kind": spec.identity.job_kind,
            "capability_version": spec.identity.capability_version,
            "authority_digest": _digest(spec.declared_authority),
            "dependencies_json": "[]",
            "resource_claims_json": '["cpu"]',
            "deadline_at": None,
            "owner_kind": spec.identity.owner_kind,
            "owner_principal_id": spec.identity.owner_principal_id,
            "service_id": spec.service_id,
        }[field_name])


def test_scheduler_goal_snapshot_deduplication_allows_a_new_parent_lineage():
    spec = replace(
        _spec(job_id="job-snapshot-1", dedupe_key="candidate-snapshot"),
        identity=replace(
            _spec(job_id="job-snapshot-1", dedupe_key="candidate-snapshot").identity,
            job_kind="goal-snapshot-to-file",
            idempotency_scope="goal-snapshot-to-file-scheduler",
        ),
        parent_job_id="strategist_tick:1",
    )
    input_digest, _ = _safe_inputs_digest(spec.inputs)
    existing = SimpleNamespace(
        run_identity=spec.identity.job_id,
        input_digest=input_digest,
        job_kind=spec.identity.job_kind,
        capability_version=spec.identity.capability_version,
        owner_kind=spec.identity.owner_kind,
        owner_principal_id=spec.identity.owner_principal_id,
        service_id=spec.service_id,
        authority_digest=_digest(spec.declared_authority),
        dependencies_json="[]",
        resource_claims_json='["cpu"]',
        deadline_at=datetime.now(timezone.utc),
        priority=spec.priority,
        max_attempts=spec.max_attempts,
        session_id=spec.session_id,
        parent_job_id="strategist_tick:previous",
        goal_id=spec.goal_id,
        goal_revision=spec.goal_revision,
        plan_revision=spec.plan_revision,
        candidate_id=spec.candidate_id,
    )
    assert _admission_conflicts(
        existing,
        spec=spec,
        input_digest=input_digest,
        authority_digest=_digest(spec.declared_authority),
        deadline=None,
    ) == []


def test_retry_requires_owner_identity_and_canonical_reconciliation_receipt():
    run = SimpleNamespace(
        owner_kind="service",
        owner_principal_id="service:strategist",
        service_id="service:strategist",
    )
    with pytest.raises(DurableJobLeaseError):
        _validate_retry_actor(
            run,
            owner_kind="service",
            owner_principal_id="service:other",
            service_id="service:other",
        )
    with pytest.raises(ValueError):
        _canonical_reconciliation_receipt({})
    with pytest.raises(ValueError):
        _canonical_reconciliation_receipt(None)
    canonical, digest = _canonical_reconciliation_receipt(
        {
            "effect_id": "effect-1",
            "effect_type": "destination_write",
            "status": "read_back",
            "outcome": "absent",
            "secret_token": "must-not-persist",
        }
    )
    assert digest
    assert "must-not-persist" not in canonical
    with pytest.raises(ValueError, match="provider_operation_id or adapter_idempotency_key"):
        _canonical_reconciliation_receipt(
            {
                "effect_id": "effect-cost",
                "effect_type": "remote_inference",
                "status": "settled",
                "actual_cost_microusd": 3,
            }
        )
    with pytest.raises(ValueError, match="nonnegative integer"):
        _canonical_reconciliation_receipt(
            {
                "effect_id": "effect-cost",
                "effect_type": "remote_inference",
                "status": "settled",
                "actual_cost_microusd": -1,
                "adapter_idempotency_key": "operation-cost",
            }
        )
    with pytest.raises(ValueError, match="nonnegative integer"):
        _canonical_reconciliation_receipt(
            {
                "effect_id": "effect-cost",
                "effect_type": "remote_inference",
                "status": "settled",
                "actual_cost_microusd": True,
                "adapter_idempotency_key": "operation-cost",
            }
        )


def test_reconciliation_receipts_bind_to_the_exact_effect_and_no_effect_retry_job():
    effect = {
        "effect_id": "effect-1",
        "effect_type": "destination_write",
        "target_path": "controlled-ledger",
        "target_digest": "target-1",
        "status": "intent",
    }
    with pytest.raises(DurableJobTransitionError, match="one durable effect"):
        _reconciliation_matches_effect(
            effect,
            {
                "effect_id": "other-effect",
                "effect_type": "destination_write",
                "status": "read_back",
                "target_path": "controlled-ledger",
                "outcome": "absent",
            },
        )
    with pytest.raises(DurableJobIdempotencyConflict, match="target digest"):
        _reconciliation_matches_effect(
            effect,
            {
                "effect_id": "effect-1",
                "effect_type": "destination_write",
                "status": "read_back",
                "target_path": "controlled-ledger",
                "target_digest": "different-target",
                "outcome": "absent",
            },
        )
    with pytest.raises(DurableJobTransitionError, match="intended target digest"):
        _reconciliation_matches_effect(
            effect,
            {
                "effect_id": "effect-1",
                "effect_type": "destination_write",
                "status": "read_back",
                "target_path": "controlled-ledger",
                "outcome": "absent",
            },
        )
    with pytest.raises(DurableJobTransitionError, match="unresolved"):
        _reconciliation_matches_effect(
            {**effect, "status": "succeeded"},
            {
                "effect_id": "effect-1",
                "effect_type": "destination_write",
                "status": "read_back",
                "target_path": "controlled-ledger",
                "target_digest": "target-1",
                "outcome": "present",
            },
        )
    _validate_no_effect_retry_receipt(
        "job-no-effect",
        {
            "effect_id": "job-failure:job-no-effect",
            "effect_type": "job_failure",
            "target_path": "job:job-no-effect",
            "status": "read_back",
            "outcome": "no_external_effect",
        },
    )
    with pytest.raises(DurableJobTransitionError, match="job-bound"):
        _validate_no_effect_retry_receipt(
            "job-no-effect",
            {
                "effect_id": "other-effect",
                "effect_type": "destination_write",
                "target_path": "controlled-ledger",
                "status": "read_back",
                "outcome": "absent",
            },
        )


def test_approval_resume_receipt_rechecks_current_authority_and_execution_contract():
    run = SimpleNamespace(
        declared_authority_json='{"principal":"service:strategist","approval_id":"approval-1","budget_microusd":25}',
        authority_digest="authority-1",
        owner_kind="service",
        owner_principal_id="service:strategist",
        service_id="service:strategist",
        goal_id="goal-1",
        goal_revision=4,
        plan_revision=2,
        capability_version="strategist-tick-v1",
    )
    receipt = {
        "status": "approved",
        "authenticated": True,
        "operator_principal_id": "operator:test",
        "operator_session_id": "operator-session:test",
        "owner_kind": "service",
        "owner_principal_id": "service:strategist",
        "service_id": "service:strategist",
        "approval_id": "approval-1",
        "authority_digest": "authority-1",
        "goal_id": "goal-1",
        "goal_revision": 4,
        "plan_revision": 2,
        "capability_version": "strategist-tick-v1",
        "budget_microusd": 25,
        "budget_digest": _digest({"budget_microusd": 25}),
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp(),
    }
    validated = _validate_approval_resume_receipt(
        run,
        receipt,
        now=datetime.now(timezone.utc),
    )
    assert validated["kind"] == "approval_resume"
    for field_name, changed_value in (
        ("authority_digest", "stale-authority"),
        ("goal_revision", 5),
        ("budget_microusd", 26),
    ):
        stale = dict(receipt)
        stale[field_name] = changed_value
        with pytest.raises(DurableJobTransitionError, match="stale|changed"):
            _validate_approval_resume_receipt(
                run,
                stale,
                now=datetime.now(timezone.utc),
            )
    expired = dict(receipt)
    expired["expires_at"] = float("nan")
    with pytest.raises(DurableJobTransitionError, match="expired"):
        _validate_approval_resume_receipt(
            run,
            expired,
            now=datetime.now(timezone.utc),
        )


def test_remote_admission_receipts_are_allowlisted_and_redacted():
    safe, digest = _canonical_remote_inference_receipt(
        {
            "schema_version": "seraph.remote-inference-admission.v1",
            "operation_id": "operation-1",
            "job_id": "job-1",
            "owner_id": "service:strategist",
            "status": "blocked",
            "reason_code": "provider_result_uncertain",
            "reconciliation_required": True,
            "prompt": "must not be copied",
            "api_key": "must not be copied",
        }
    )

    assert safe["status"] == "blocked"
    assert safe["reason_code"] == "provider_result_uncertain"
    assert "prompt" not in safe
    assert "api_key" not in safe
    assert digest

    with pytest.raises(ValueError, match="unsupported"):
        _canonical_remote_inference_receipt(
            {"operation_id": "operation-1", "job_id": "job-1", "owner_id": "service:strategist", "status": "unknown"}
        )


def test_durable_receipts_redact_secret_key_variants_and_error_payloads():
    safe = _safe_structure(
        {
            "details": {
                "api-key": "secret-api-key",
                "apikey": "secret-apikey",
                "x-api-key": "secret-x-api-key",
                "Authorization": "Bearer secret-authorization",
                "original_error": "provider response included secret-original-error",
                "safe_reason": "provider_timeout",
            }
        }
    )
    assert safe["details"]["api-key"] == "[redacted]"
    assert safe["details"]["apikey"] == "[redacted]"
    assert safe["details"]["x-api-key"] == "[redacted]"
    assert safe["details"]["Authorization"] == "[redacted]"
    assert safe["details"]["original_error"] == "[redacted]"
    assert safe["details"]["safe_reason"] == "provider_timeout"


@pytest.mark.asyncio
async def test_remote_receipt_adapter_maps_status_without_mutating_job_lifecycle():
    class RecordingRepository(DurableJobRepository):
        async def get_job(self, job_id):
            return {"job_id": job_id, "owner": {"principal_id": "service:strategist"}}

        async def record_effect(self, job_id, **kwargs):
            self.calls.append((job_id, kwargs))
            return {"persisted": True, "job_id": job_id, "effect": kwargs}

        def __init__(self):
            self.calls = []

    repository = RecordingRepository()
    for status, expected_effect_status in (
        ("queued", "unknown"),
        ("blocked", "blocked"),
        ("settled", "succeeded"),
        ("rejected", "failed"),
    ):
        result = await repository.record_remote_inference_receipt(
            {
                "operation_id": f"operation-{status}",
                "job_id": "job-1",
                "owner_id": "service:strategist",
                "status": status,
                "reconciliation_required": status == "blocked",
                "secret_token": "must-not-persist",
            }
        )
        assert result["persisted"] is True
        _, kwargs = repository.calls[-1]
        assert kwargs["status"] == expected_effect_status
        assert kwargs["details"]["admission_status"] == status
        assert "secret_token" not in str(kwargs["details"])

    assert set(REMOTE_INFERENCE_EFFECT_STATUSES) >= {"queued", "blocked", "settled", "rejected"}


@pytest.mark.asyncio
async def test_remote_receipt_adapter_requires_an_existing_canonical_job():
    class MissingJobRepository:
        async def get_job(self, _job_id):
            return None

    adapter = DurableJobRepository()
    adapter.get_job = MissingJobRepository().get_job
    with pytest.raises(DurableJobNotFound):
        await adapter.record_remote_inference_receipt(
            {
                "operation_id": "operation-missing",
                "job_id": "job-missing",
                "owner_id": "service:strategist",
                "status": "queued",
            }
        )


class _AsyncSQLiteConnection:
    """Small adapter so migration SQL can be tested without aiosqlite."""

    def __init__(self) -> None:
        self.raw = sqlite3.connect(":memory:")

    async def exec_driver_sql(self, statement: str, parameters=None):
        return self.raw.execute(statement, parameters or {})


@pytest.mark.asyncio
async def test_legacy_migration_is_fail_closed_and_index_creation_is_repeatable():
    connection = _AsyncSQLiteConnection()
    connection.raw.execute(
        "CREATE TABLE workflow_run_states "
        "(id INTEGER PRIMARY KEY, status VARCHAR, run_fingerprint VARCHAR, metadata_json VARCHAR)"
    )
    connection.raw.executemany(
        "INSERT INTO workflow_run_states (id, status, run_fingerprint, metadata_json) VALUES (?, ?, ?, ?)",
        (
            (1, "running", "fingerprint-1", "{}"),
            (2, "future_status", "fingerprint-2", "{invalid-json"),
        ),
    )

    await _ensure_legacy_columns(connection)
    await _ensure_legacy_columns(connection)

    statuses = dict(connection.raw.execute("SELECT id, status FROM workflow_run_states").fetchall())
    assert statuses == {1: "blocked", 2: "blocked"}
    reasons = dict(connection.raw.execute("SELECT id, failure_reason FROM workflow_run_states").fetchall())
    assert reasons == {1: "migration_requires_reconciliation", 2: "legacy_status_unmapped"}
    index_rows = connection.raw.execute("PRAGMA index_list(workflow_run_states)").fetchall()
    matching_indexes = [row for row in index_rows if row[1] == "ux_workflow_run_states_idempotency_binding"]
    assert len(matching_indexes) == 1
    assert matching_indexes[0][2] == 1


@pytest.mark.asyncio
async def test_legacy_migration_reports_duplicate_bindings_before_unique_index():
    connection = _AsyncSQLiteConnection()
    connection.raw.execute(
        "CREATE TABLE workflow_run_states "
        "(id INTEGER PRIMARY KEY, status VARCHAR, run_fingerprint VARCHAR, metadata_json VARCHAR, "
        "idempotency_binding VARCHAR)"
    )
    connection.raw.executemany(
        "INSERT INTO workflow_run_states "
        "(id, status, run_fingerprint, metadata_json, idempotency_binding) VALUES (?, ?, ?, ?, ?)",
        (
            (1, "accepted", "fingerprint-1", "{}", "duplicate-binding"),
            (2, "accepted", "fingerprint-2", "{}", "duplicate-binding"),
        ),
    )

    await _ensure_legacy_columns(connection)
    rows = connection.raw.execute(
        "SELECT id, status, failure_reason, metadata_json FROM workflow_run_states ORDER BY id"
    ).fetchall()
    assert [row[1:3] for row in rows] == [
        ("blocked", "idempotency_binding_conflict"),
        ("blocked", "idempotency_binding_conflict"),
    ]
    assert all(json.loads(row[3])["durable_job_migration"]["idempotency_conflict"] for row in rows)
    assert all(
        json.loads(row[3])["durable_job_migration"]["original_idempotency_binding"]
        == "duplicate-binding"
        for row in rows
    )
    bindings = connection.raw.execute(
        "SELECT idempotency_binding FROM workflow_run_states ORDER BY id"
    ).fetchall()
    assert bindings == [(None,), (None,)]
    index_rows = connection.raw.execute("PRAGMA index_list(workflow_run_states)").fetchall()
    matching_indexes = [row for row in index_rows if row[1] == "ux_workflow_run_states_idempotency_binding"]
    assert len(matching_indexes) == 1
    assert matching_indexes[0][2] == 1


def _spec(*, job_id: str = "job-743-1", dedupe_key: str = "candidate-1") -> DurableJobSpec:
    return DurableJobSpec(
        identity=DurableJobIdentity(
            job_id=job_id,
            owner_kind="service",
            owner_principal_id="service:strategist",
            job_kind="strategist_tick",
            capability_version="1",
            idempotency_scope="goal-candidate",
            idempotency_key=dedupe_key,
        ),
        inputs={"goal_id": "goal-1", "secret_token": "do-not-persist"},
        session_id="job-session",
        goal_id="goal-1",
        goal_revision=4,
        plan_revision=2,
        candidate_id="candidate-1",
        priority=90,
        resource_claims=("cpu",),
        declared_authority={
            "principal": "service:strategist",
            "service_id": "service:strategist",
            "approval_id": "approval-1",
        },
        max_attempts=2,
        service_id="service:strategist",
    )


def test_goal_revision_requires_goal_id_before_admission():
    spec = replace(_spec(), goal_id=None, goal_revision=4)
    with pytest.raises(ValueError, match="goal_revision requires a canonical goal"):
        _validate_admission_authority(spec)


def test_declared_service_session_must_match_durable_session():
    spec = replace(
        _spec(),
        goal_id=None,
        goal_revision=None,
        declared_authority={
            "principal": "service:strategist",
            "service_id": "service:strategist",
            "session_id": "different-session",
        },
    )
    with pytest.raises(ValueError, match="declared authority session_id"):
        _validate_admission_authority(spec)


@pytest.mark.asyncio
async def test_admission_is_idempotent_and_lifecycle_records_safe_receipts(async_db):
    admitted = await durable_job_repository.admit_job(_spec())
    duplicate = await durable_job_repository.admit_job(_spec())

    assert admitted["status"] == "accepted"
    assert duplicate["status"] == "accepted"
    assert duplicate["receipt"]["status"] == "deduped"
    assert duplicate["job_id"] == admitted["job_id"]
    assert duplicate["idempotency"]["binding"] == admitted["idempotency"]["binding"]
    assert "do-not-persist" not in str(admitted)

    await durable_job_repository.queue_job(admitted["job_id"])
    claimed = await durable_job_repository.claim_job(admitted["job_id"], owner="runner-a")
    token = claimed["lease"]["fencing_token"]
    with pytest.raises(DurableJobLeaseError):
        await durable_job_repository.record_artifact(
            admitted["job_id"], file_path="reports/unsafe.json"
        )
    checkpointed = await durable_job_repository.record_checkpoint(
        admitted["job_id"],
        checkpoint_id="step-1",
        state={"cursor": 2, "secret": "do-not-persist"},
        owner="runner-a",
        fencing_token=token,
    )
    artifact = await durable_job_repository.record_artifact(
        admitted["job_id"],
        file_path="reports/job-743.json",
        content="private result payload",
        owner="runner-a",
        fencing_token=token,
    )

    assert checkpointed["checkpoints"][0]["state_digest"]
    assert "do-not-persist" not in str(checkpointed["checkpoints"])
    assert artifact["artifacts"][0]["content_sha256"]
    assert "private result payload" not in str(artifact["artifacts"])

    failed = await durable_job_repository.transition_job(
        admitted["job_id"],
        "failed",
        owner="runner-a",
        fencing_token=token,
        reason="controlled_failure",
    )
    with pytest.raises(DurableJobTransitionError, match="explicit retry"):
        await durable_job_repository.queue_job(admitted["job_id"])
    retried = await durable_job_repository.retry_job(
        admitted["job_id"],
        owner_kind="service",
        owner_principal_id="service:strategist",
        service_id="service:strategist",
        reconciled=True,
        reconciliation_receipt={
            "effect_id": f"job-failure:{admitted['job_id']}",
            "effect_type": "job_failure",
            "target_path": f"job:{admitted['job_id']}",
            "status": "read_back",
            "outcome": "no_external_effect",
        },
    )

    assert failed["status"] == "failed"
    assert retried["status"] == "queued"
    assert retried["receipt"]["reconciliation_receipt_digest"]
    assert retried["effects"][0]["status"] == "reconciled"


@pytest.mark.asyncio
async def test_terminal_replay_is_idempotent_with_stale_execution_receipt(async_db):
    admitted = await durable_job_repository.admit_job(
        _spec(job_id="job-743-terminal-replay", dedupe_key="candidate-terminal-replay")
    )
    await durable_job_repository.queue_job(admitted["job_id"])
    claimed = await durable_job_repository.claim_job(admitted["job_id"], owner="runner-terminal-replay")
    verified = await durable_job_repository.record_readback(
        admitted["job_id"],
        target_path="result:terminal-replay",
        target_digest="result-digest",
        content_sha256="result-digest",
        status="succeeded",
        details={"verified": True, "capability": "terminal-replay"},
        owner="runner-terminal-replay",
        fencing_token=claimed["lease"]["fencing_token"],
        expected_revision=claimed["revision"],
    )
    completed = await durable_job_repository.transition_job(
        admitted["job_id"],
        "succeeded",
        owner="runner-terminal-replay",
        fencing_token=claimed["lease"]["fencing_token"],
        expected_state="running",
        expected_revision=verified["revision"],
    )

    replay = await durable_job_repository.transition_job(
        admitted["job_id"],
        "succeeded",
        owner="stale-runner",
        fencing_token=claimed["lease"]["fencing_token"],
        expected_state="running",
        expected_revision=claimed["revision"],
    )

    assert completed["status"] == "succeeded"
    assert replay["status"] == "succeeded"
    assert replay["receipt"]["status"] == "deduped"
    assert replay["revision"] == completed["revision"]


@pytest.mark.asyncio
async def test_dependency_failure_is_recorded_before_runner_claim(async_db):
    dependency = await durable_job_repository.admit_job(
        _spec(job_id="job-743-dependency", dedupe_key="candidate-dependency")
    )
    await durable_job_repository.queue_job(dependency["job_id"])
    dependency_claim = await durable_job_repository.claim_job(
        dependency["job_id"], owner="runner-dependency"
    )
    await durable_job_repository.transition_job(
        dependency["job_id"],
        "failed",
        owner="runner-dependency",
        fencing_token=dependency_claim["lease"]["fencing_token"],
        reason="controlled_failure",
    )

    child = await durable_job_repository.admit_job(
        replace(
            _spec(job_id="job-743-dependent", dedupe_key="candidate-dependent"),
            dependencies=(dependency["job_id"],),
        )
    )
    await durable_job_repository.queue_job(child["job_id"])
    failed = await durable_job_repository.claim_job(child["job_id"], owner="runner-dependent")

    assert failed["status"] == "failed"
    assert failed["failure_reason"] == "dependency_failed"
    assert failed["receipt"]["dependency_id"] == dependency["job_id"]
    assert failed["receipt"]["dependency_status"] == "failed"
    assert failed["lease"]["owner"] is None


@pytest.mark.asyncio
async def test_pending_dependency_does_not_admit_runner_or_consume_attempt(async_db):
    dependency = await durable_job_repository.admit_job(
        _spec(job_id="job-743-pending-dependency", dedupe_key="candidate-pending-dependency")
    )
    child = await durable_job_repository.admit_job(
        replace(
            _spec(job_id="job-743-pending-child", dedupe_key="candidate-pending-child"),
            dependencies=(dependency["job_id"],),
        )
    )
    await durable_job_repository.queue_job(child["job_id"])
    pending = await durable_job_repository.claim_job(child["job_id"], owner="runner-pending")

    assert pending["status"] == "queued"
    assert pending["receipt"]["status"] == "blocked"
    assert pending["receipt"]["reason"] == "dependency_pending"
    assert pending["receipt"]["state_unchanged"] is True
    assert pending["attempt_count"] == 0


@pytest.mark.asyncio
async def test_approval_held_job_cannot_be_resumed_without_a_fresh_authority_route(async_db):
    admitted = await durable_job_repository.admit_job(
        _spec(job_id="job-743-approval-replay", dedupe_key="candidate-approval-replay")
    )
    await durable_job_repository.queue_job(admitted["job_id"])
    claimed = await durable_job_repository.claim_job(admitted["job_id"], owner="runner-approval-replay")
    held = await durable_job_repository.transition_job(
        admitted["job_id"],
        "awaiting_approval",
        owner="runner-approval-replay",
        fencing_token=claimed["lease"]["fencing_token"],
    )
    approval_expires_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()
    approval_details = {
        "approval_owner_operator_session_id": "operator-session:test",
        "approval_operator_principal_id": "operator:test",
        "durable_job_id": admitted["job_id"],
        "durable_owner_kind": admitted["owner"]["kind"],
        "durable_owner_principal_id": admitted["owner"]["principal_id"],
        "durable_service_id": admitted["owner"]["service_id"],
        "durable_approval_id": admitted["declared_authority"]["approval_id"],
        "durable_authority_digest": admitted["authority_digest"],
        "durable_goal_id": admitted["goal_id"],
        "durable_goal_revision": admitted["goal_revision"],
        "durable_plan_revision": admitted["plan_revision"],
        "durable_capability_version": admitted["capability_version"],
        "durable_budget_digest": _digest({"budget_microusd": None}),
        "approval_expires_at": approval_expires_at,
    }
    async with async_db() as db:
        db.add(
            ApprovalRequest(
                id="approval-1",
                session_id="job-session",
                tool_name="strategist_tick",
                status="approved",
                fingerprint="approval-resume-fingerprint",
                summary="resume durable job",
                details_json=json.dumps(approval_details),
            )
        )
    with pytest.raises(DurableJobTransitionError, match="illegal"):
        await durable_job_repository.resume_job(
            admitted["job_id"],
            expected_revision=held["revision"],
        )
    with pytest.raises(DurableJobTransitionError, match="stale|changed"):
        stale_receipt = {
            "status": "approved",
            "authenticated": True,
            "operator_principal_id": "operator:test",
            "operator_session_id": "operator-session:test",
            "owner_kind": admitted["owner"]["kind"],
            "owner_principal_id": admitted["owner"]["principal_id"],
            "service_id": admitted["owner"]["service_id"],
            "approval_id": "approval-1",
            "authority_digest": "old-authority",
            "goal_id": admitted["goal_id"],
            "goal_revision": admitted["goal_revision"],
            "plan_revision": admitted["plan_revision"],
            "capability_version": admitted["capability_version"],
            "budget_microusd": None,
            "budget_digest": _digest({"budget_microusd": None}),
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp(),
        }
        await durable_job_repository.resume_approved_job(
            admitted["job_id"],
            approval_receipt={
                "status": "approved",
                "authenticated": True,
                "operator_principal_id": stale_receipt["operator_principal_id"],
                "operator_session_id": stale_receipt["operator_session_id"],
            },
            approval_id=stale_receipt["approval_id"],
            authority_digest=stale_receipt["authority_digest"],
            goal_id=stale_receipt["goal_id"],
            goal_revision=stale_receipt["goal_revision"],
            plan_revision=stale_receipt["plan_revision"],
            capability_version=stale_receipt["capability_version"],
            owner_kind=stale_receipt["owner_kind"],
            owner_principal_id=stale_receipt["owner_principal_id"],
            service_id=stale_receipt["service_id"],
            budget_microusd=stale_receipt["budget_microusd"],
            budget_digest=stale_receipt["budget_digest"],
            operator_principal_id=stale_receipt["operator_principal_id"],
            operator_session_id=stale_receipt["operator_session_id"],
            expires_at=stale_receipt["expires_at"],
            expected_revision=held["revision"],
        )
    resumed = await durable_job_repository.resume_approved_job(
        admitted["job_id"],
        approval_receipt={
            "status": "approved",
            "authenticated": True,
            "operator_principal_id": "operator:test",
            "operator_session_id": "operator-session:test",
        },
        approval_id=admitted["declared_authority"]["approval_id"],
        authority_digest=admitted["authority_digest"],
        goal_id=admitted["goal_id"],
        goal_revision=admitted["goal_revision"],
        plan_revision=admitted["plan_revision"],
        capability_version=admitted["capability_version"],
        owner_kind=admitted["owner"]["kind"],
        owner_principal_id=admitted["owner"]["principal_id"],
        service_id=admitted["owner"]["service_id"],
        budget_microusd=None,
        budget_digest=_digest({"budget_microusd": None}),
        operator_principal_id="operator:test",
        operator_session_id="operator-session:test",
        expires_at=approval_expires_at,
        expected_revision=held["revision"],
    )
    assert resumed["status"] == "queued"
    assert any(
        item.get("kind") == "approval_resume" and item.get("approval_id") == "approval-1"
        for item in resumed["effects"]
    )
    with pytest.raises(DurableJobTransitionError, match="ApprovalRequest"):
        await durable_job_repository.resume_approved_job(
            admitted["job_id"],
            approval_receipt={
                "status": "approved",
                "authenticated": True,
                "operator_principal_id": "operator:test",
                "operator_session_id": "operator-session:test",
            },
            approval_id="approval-1",
            authority_digest=admitted["authority_digest"],
            goal_id=admitted["goal_id"],
            goal_revision=admitted["goal_revision"],
            plan_revision=admitted["plan_revision"],
            capability_version=admitted["capability_version"],
            owner_kind=admitted["owner"]["kind"],
            owner_principal_id=admitted["owner"]["principal_id"],
            service_id=admitted["owner"]["service_id"],
            budget_microusd=None,
            budget_digest=_digest({"budget_microusd": None}),
            operator_principal_id="operator:test",
            operator_session_id="operator-session:test",
            expires_at=approval_expires_at,
        )


@pytest.mark.asyncio
async def test_durable_resume_attachment_quarantine_survives_transition_rollback(async_db):
    admitted = await durable_job_repository.admit_job(
        _spec(job_id="job-743-resume-attachment-quarantine", dedupe_key="candidate-resume-attachment-quarantine")
    )
    await durable_job_repository.queue_job(admitted["job_id"])
    claimed = await durable_job_repository.claim_job(
        admitted["job_id"], owner="runner-resume-attachment-quarantine"
    )
    held = await durable_job_repository.transition_job(
        admitted["job_id"],
        "awaiting_approval",
        owner="runner-resume-attachment-quarantine",
        fencing_token=claimed["lease"]["fencing_token"],
    )
    approval_expires_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).timestamp()
    approval_details = {
        "approval_owner_operator_session_id": "operator-session:resume-attachment-quarantine",
        "approval_operator_principal_id": "operator:resume-attachment-quarantine",
        "durable_job_id": admitted["job_id"],
        "durable_owner_kind": admitted["owner"]["kind"],
        "durable_owner_principal_id": admitted["owner"]["principal_id"],
        "durable_service_id": admitted["owner"]["service_id"],
        "durable_approval_id": admitted["declared_authority"]["approval_id"],
        "durable_authority_digest": admitted["authority_digest"],
        "durable_goal_id": admitted["goal_id"],
        "durable_goal_revision": admitted["goal_revision"],
        "durable_plan_revision": admitted["plan_revision"],
        "durable_capability_version": admitted["capability_version"],
        "durable_budget_digest": _digest({"budget_microusd": None}),
        "approval_expires_at": approval_expires_at,
    }
    async with async_db() as db:
        db.add(
            ApprovalRequest(
                id="approval-1",
                session_id="job-session",
                operator_session_id="operator-session:resume-attachment-quarantine",
                attachment_refs_json="{malformed-attachment-refs",
                status="approved",
                tool_name="strategist_tick",
                fingerprint="resume-attachment-quarantine-fingerprint",
                summary="resume durable job with malformed attachment refs",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                details_json=json.dumps(approval_details),
            )
        )

    with pytest.raises(DurableJobTransitionError, match="ApprovalRequest"):
        await durable_job_repository.resume_approved_job(
            admitted["job_id"],
            approval_receipt={
                "status": "approved",
                "authenticated": True,
                "operator_principal_id": "operator:resume-attachment-quarantine",
                "operator_session_id": "operator-session:resume-attachment-quarantine",
            },
            approval_id=admitted["declared_authority"]["approval_id"],
            authority_digest=admitted["authority_digest"],
            goal_id=admitted["goal_id"],
            goal_revision=admitted["goal_revision"],
            plan_revision=admitted["plan_revision"],
            capability_version=admitted["capability_version"],
            owner_kind=admitted["owner"]["kind"],
            owner_principal_id=admitted["owner"]["principal_id"],
            service_id=admitted["owner"]["service_id"],
            budget_microusd=None,
            budget_digest=_digest({"budget_microusd": None}),
            operator_principal_id="operator:resume-attachment-quarantine",
            operator_session_id="operator-session:resume-attachment-quarantine",
            expires_at=approval_expires_at,
            expected_revision=held["revision"],
        )

    async with async_db() as db:
        row = (
            await db.execute(
                select(ApprovalRequest).where(ApprovalRequest.id == "approval-1")
            )
        ).scalar_one()
        details = json.loads(row.details_json or "{}")
        assert row.status == "expired"
        assert row.attachment_refs_json == "[]"
        assert details["attachment_refs"] == []
        assert details["attachment_refs_status"] == "unavailable"


@pytest.mark.asyncio
async def test_malformed_effect_history_is_blocked_before_claim(async_db):
    admitted = await durable_job_repository.admit_job(
        _spec(job_id="job-743-malformed-claim", dedupe_key="candidate-malformed-claim")
    )
    await durable_job_repository.queue_job(admitted["job_id"])
    async with async_db() as db:
        await db.execute(
            update(WorkflowRunState)
            .where(WorkflowRunState.run_identity == admitted["job_id"])
            .values(effect_receipts_json="not-json")
        )
    blocked = await durable_job_repository.claim_job(admitted["job_id"], owner="runner-malformed-claim")
    assert blocked["status"] == "blocked"
    assert blocked["failure_reason"] == "malformed_effect_history_requires_reconciliation"
    assert blocked["attempt_count"] == 0


@pytest.mark.asyncio
async def test_accepted_effect_requires_an_authenticated_owner_lease(async_db):
    admitted = await durable_job_repository.admit_job(
        _spec(job_id="job-743-unresolved-claim", dedupe_key="candidate-unresolved-claim")
    )
    with pytest.raises(DurableJobLeaseError, match="accepted jobs require an authenticated owner lease"):
        await durable_job_repository.record_effect(
            admitted["job_id"],
            effect_id="unresolved-claim-effect",
            effect_type="destination_write",
            target_path="controlled-ledger",
            status="intent",
            owner=None,
            fencing_token=None,
        )


@pytest.mark.asyncio
async def test_effect_receipt_updates_keep_one_stable_external_identity(async_db):
    admitted = await durable_job_repository.admit_job(
        _spec(job_id="job-743-effect-update", dedupe_key="candidate-effect-update")
    )
    await durable_job_repository.queue_job(admitted["job_id"])
    claimed = await durable_job_repository.claim_job(admitted["job_id"], owner="runner-effect-update")
    token = claimed["lease"]["fencing_token"]
    intent = await durable_job_repository.record_effect(
        admitted["job_id"],
        effect_id="effect-stable-1",
        effect_type="destination_write",
        target_path="controlled-ledger",
        target_digest="target-1",
        approval_id="approval-1",
        adapter_idempotency_key="adapter-key-1",
        status="intent",
        owner="runner-effect-update",
        fencing_token=token,
    )
    dispatched = await durable_job_repository.record_effect(
        admitted["job_id"],
        effect_id="effect-stable-1",
        effect_type="destination_write",
        target_path="controlled-ledger",
        target_digest="target-1",
        approval_id="approval-1",
        adapter_idempotency_key="adapter-key-1",
        status="dispatched",
        owner="runner-effect-update",
        fencing_token=token,
        expected_revision=intent["revision"],
    )
    settled = await durable_job_repository.record_readback(
        admitted["job_id"],
        effect_id="effect-stable-1",
        effect_type="destination_write",
        target_path="controlled-ledger",
        target_digest="target-1",
        status="succeeded",
        content_sha256="readback-1",
        details={"verified": True, "readback": "controlled-ledger"},
        owner="runner-effect-update",
        fencing_token=token,
        expected_revision=dispatched["revision"],
    )
    completed = await durable_job_repository.transition_job(
        admitted["job_id"],
        "succeeded",
        owner="runner-effect-update",
        fencing_token=token,
        expected_revision=settled["revision"],
    )

    assert intent["effects"][0]["effect_id"] == "effect-stable-1"
    assert len(settled["effects"]) == 1
    assert settled["effects"][0]["status"] == "succeeded"
    assert settled["effects"][0]["approval_id"] == "approval-1"
    assert settled["effects"][0]["adapter_idempotency_key"] == "adapter-key-1"
    assert completed["status"] == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("readback_status", ["failed", "blocked"])
async def test_non_success_readback_does_not_clear_unresolved_effect(async_db, readback_status):
    admitted = await durable_job_repository.admit_job(
        replace(
            _spec(job_id=f"job-743-readback-{readback_status}", dedupe_key=f"candidate-readback-{readback_status}"),
            max_attempts=2,
        )
    )
    await durable_job_repository.queue_job(admitted["job_id"])
    claimed = await durable_job_repository.claim_job(
        admitted["job_id"], owner=f"runner-readback-{readback_status}"
    )
    token = claimed["lease"]["fencing_token"]
    intent = await durable_job_repository.record_effect(
        admitted["job_id"],
        effect_id="effect-readback-stays-uncertain",
        effect_type="destination_write",
        target_path="controlled-ledger",
        status="intent",
        owner=f"runner-readback-{readback_status}",
        fencing_token=token,
    )
    observed = await durable_job_repository.record_readback(
        admitted["job_id"],
        effect_id="effect-readback-stays-uncertain",
        effect_type="destination_write",
        target_path="controlled-ledger",
        status=readback_status,
        details={"verified": False, "reason": "readback_not_proven"},
        owner=f"runner-readback-{readback_status}",
        fencing_token=token,
        expected_revision=int(intent["revision"]),
    )
    effect_entries = observed["effects"]
    assert any(
        item.get("effect_id") == "effect-readback-stays-uncertain"
        and item.get("status") == "intent"
        for item in effect_entries
    )
    diagnostic = next(
        item for item in effect_entries if item.get("original_effect_id") == "effect-readback-stays-uncertain"
    )
    assert diagnostic["details"]["readback_observation_only"] is True
    with pytest.raises(DurableJobTransitionError, match="unresolved external effect"):
        await durable_job_repository.transition_job(
            admitted["job_id"],
            "succeeded",
            owner=f"runner-readback-{readback_status}",
            fencing_token=token,
            expected_revision=observed["revision"],
        )


@pytest.mark.asyncio
async def test_unresolved_effect_cannot_be_marked_succeeded(async_db):
    admitted = await durable_job_repository.admit_job(
        replace(_spec(job_id="job-743-uncertain-success", dedupe_key="candidate-uncertain-success"), max_attempts=2)
    )
    await durable_job_repository.queue_job(admitted["job_id"])
    claimed = await durable_job_repository.claim_job(admitted["job_id"], owner="runner-uncertain-success")
    await durable_job_repository.record_effect(
        admitted["job_id"],
        effect_type="destination_write",
        status="unknown",
        details={"destination": "controlled"},
        owner="runner-uncertain-success",
        fencing_token=claimed["lease"]["fencing_token"],
    )
    with pytest.raises(DurableJobTransitionError, match="unresolved external effect"):
        await durable_job_repository.transition_job(
            admitted["job_id"],
            "succeeded",
            owner="runner-uncertain-success",
            fencing_token=claimed["lease"]["fencing_token"],
        )
    current = await durable_job_repository.get_job(admitted["job_id"])
    assert current is not None
    assert current["status"] == "running"


@pytest.mark.asyncio
async def test_effect_bound_retry_rejects_corrupt_or_missing_history(async_db):
    for job_id, raw_effects in (
        ("job-743-corrupt-effects", "not-json"),
        # SQLite enforces the production column's non-null contract; an empty
        # ledger is the persisted representation of missing history.
        ("job-743-missing-effects", ""),
    ):
        admitted = await durable_job_repository.admit_job(
            replace(_spec(job_id=job_id, dedupe_key=job_id), max_attempts=2)
        )
        await durable_job_repository.queue_job(admitted["job_id"])
        claimed = await durable_job_repository.claim_job(admitted["job_id"], owner=f"runner:{job_id}")
        await durable_job_repository.transition_job(
            admitted["job_id"],
            "failed",
            owner=f"runner:{job_id}",
            fencing_token=claimed["lease"]["fencing_token"],
            reason="provider_error",
        )
        async with async_db() as db:
            await db.execute(
                update(WorkflowRunState)
                .where(WorkflowRunState.run_identity == job_id)
                .values(effect_receipts_json=raw_effects)
            )
        with pytest.raises(DurableJobTransitionError, match="effect history"):
            await durable_job_repository.retry_job(
                job_id,
                owner_kind="service",
                owner_principal_id="service:strategist",
                service_id="service:strategist",
                reconciliation_receipt={
                    "effect_id": "missing",
                    "effect_type": "remote_inference_admission",
                    "status": "read_back",
                    "outcome": "absent",
                },
            )


@pytest.mark.asyncio
async def test_failed_unresolved_effect_can_be_reconciled_before_retry(async_db):
    admitted = await durable_job_repository.admit_job(
        replace(
            _spec(job_id="job-743-failed-effect", dedupe_key="candidate-failed-effect"),
            max_attempts=2,
        )
    )
    await durable_job_repository.queue_job(admitted["job_id"])
    claimed = await durable_job_repository.claim_job(admitted["job_id"], owner="runner-failed-effect")
    effect = await durable_job_repository.record_effect(
        admitted["job_id"],
        effect_type="destination_write",
        target_path="controlled-ledger",
        target_digest="target-1",
        status="intent",
        details={"payload_digest": "payload-digest"},
        owner="runner-failed-effect",
        fencing_token=claimed["lease"]["fencing_token"],
    )
    await durable_job_repository.transition_job(
        admitted["job_id"],
        "failed",
        owner="runner-failed-effect",
        fencing_token=claimed["lease"]["fencing_token"],
        reason="dispatch_timeout",
    )

    receipt = {
        "effect_id": effect["receipt"]["effect_id"],
        "effect_type": "destination_write",
        "status": "read_back",
        "target_path": "controlled-ledger",
        "target_digest": "target-1",
        "outcome": "absent",
    }
    with pytest.raises(DurableJobTransitionError, match="unknown external effect"):
        await durable_job_repository.retry_job(
            admitted["job_id"],
            owner_kind="service",
            owner_principal_id="service:strategist",
            service_id="service:strategist",
            reconciliation_receipt=receipt,
        )

    reconciled = await durable_job_repository.reconcile_external_effect(
        admitted["job_id"],
        owner_kind="service",
        owner_principal_id="service:strategist",
        service_id="service:strategist",
        reconciliation_receipt=receipt,
    )
    assert reconciled["status"] == "failed"
    assert reconciled["receipt"]["from"] == "failed"
    retried = await durable_job_repository.retry_job(
        admitted["job_id"],
        owner_kind="service",
        owner_principal_id="service:strategist",
        service_id="service:strategist",
        reconciliation_receipt=receipt,
        expected_revision=reconciled["revision"],
    )
    assert retried["status"] == "queued"


@pytest.mark.asyncio
async def test_retry_rejects_expired_deadline_and_exhausted_attempt_budget(async_db):
    expired = await durable_job_repository.admit_job(
        replace(
            _spec(job_id="job-743-expired-retry", dedupe_key="candidate-expired-retry"),
            deadline_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        )
    )
    assert expired["status"] == "failed"
    with pytest.raises(DurableJobTransitionError, match="deadline"):
        await durable_job_repository.queue_job(expired["job_id"])
    with pytest.raises(DurableJobTransitionError, match="deadline"):
        await durable_job_repository.retry_job(
            expired["job_id"],
            owner_kind="service",
            owner_principal_id="service:strategist",
            service_id="service:strategist",
            reconciliation_receipt={
                "effect_id": "expired-effect",
                "effect_type": "destination_write",
                "status": "read_back",
                "outcome": "absent",
            },
        )

    exhausted = await durable_job_repository.admit_job(
        replace(
            _spec(job_id="job-743-exhausted-retry", dedupe_key="candidate-exhausted-retry"),
            max_attempts=1,
        )
    )
    await durable_job_repository.queue_job(exhausted["job_id"])
    claimed = await durable_job_repository.claim_job(exhausted["job_id"], owner="runner-exhausted")
    await durable_job_repository.transition_job(
        exhausted["job_id"],
        "failed",
        owner="runner-exhausted",
        fencing_token=claimed["lease"]["fencing_token"],
        reason="controlled_failure",
    )
    with pytest.raises(DurableJobTransitionError, match="attempt budget"):
        await durable_job_repository.retry_job(
            exhausted["job_id"],
            owner_kind="service",
            owner_principal_id="service:strategist",
            service_id="service:strategist",
            reconciliation_receipt={
                "effect_id": "exhausted-effect",
                "effect_type": "destination_write",
                "status": "read_back",
                "outcome": "absent",
            },
        )


@pytest.mark.asyncio
async def test_child_admission_requires_the_current_parent_fence(async_db):
    parent = await durable_job_repository.admit_job(
        _spec(job_id="parent-strategist", dedupe_key="parent-strategist")
    )
    await durable_job_repository.queue_job(parent["job_id"])
    claimed = await durable_job_repository.claim_job(parent["job_id"], owner="runner-parent")
    token = claimed["lease"]["fencing_token"]
    child = replace(
        _spec(job_id="child-snapshot", dedupe_key="child-snapshot"),
        identity=replace(
            _spec(job_id="child-snapshot", dedupe_key="child-snapshot").identity,
            job_kind="goal-snapshot-to-file",
            idempotency_scope="goal-snapshot-to-file-scheduler",
        ),
        parent_job_id=parent["job_id"],
        parent_fencing_token=token,
    )
    admitted = await durable_job_repository.admit_job(child)
    assert admitted["status"] == "accepted"

    with pytest.raises(DurableJobLeaseError, match="parent job fence"):
        await durable_job_repository.admit_job(
            replace(
                child,
                identity=replace(child.identity, job_id="child-snapshot-stale"),
                parent_fencing_token=token - 1,
            )
        )


@pytest.mark.asyncio
async def test_illegal_transition_stale_lease_and_restart_recovery_are_fail_closed(async_db):
    admitted = await durable_job_repository.admit_job(_spec(job_id="job-743-2", dedupe_key="candidate-2"))
    with pytest.raises(DurableJobTransitionError):
        await durable_job_repository.transition_job(admitted["job_id"], "running")

    await durable_job_repository.queue_job(admitted["job_id"])
    claimed = await durable_job_repository.claim_job(admitted["job_id"], owner="runner-a", lease_seconds=1)
    token = claimed["lease"]["fencing_token"]
    recovered = await durable_job_repository.recover_stale_jobs(
        now=datetime.now(timezone.utc) + timedelta(seconds=5)
    )

    recovered_job = next(item for item in recovered if item["job_id"] == admitted["job_id"])
    assert recovered_job["status"] == "blocked"
    assert recovered_job["failure_reason"] == "stale_lease_requires_reconciliation"
    assert recovered_job["receipt"]["operator_action"] == "reconcile_effects_then_retry_or_cancel"

    with pytest.raises(DurableJobLeaseError):
        await durable_job_repository.record_checkpoint(
            admitted["job_id"],
            checkpoint_id="stale",
            state={"cursor": 3},
            owner="runner-a",
            fencing_token=token,
        )
    with pytest.raises(DurableJobLeaseError):
        await durable_job_repository.record_artifact(
            admitted["job_id"],
            file_path="reports/stale.json",
            owner="runner-a",
            fencing_token=token,
        )


@pytest.mark.asyncio
async def test_conflicting_idempotency_binding_is_rejected(async_db):
    await durable_job_repository.admit_job(_spec(job_id="job-743-3", dedupe_key="candidate-3"))
    with pytest.raises(DurableJobIdempotencyConflict):
        await durable_job_repository.admit_job(_spec(job_id="job-743-other", dedupe_key="candidate-3"))


@pytest.mark.asyncio
async def test_replay_with_changed_execution_contract_is_rejected(async_db):
    await durable_job_repository.admit_job(_spec(job_id="job-743-4", dedupe_key="candidate-4"))
    with pytest.raises(DurableJobIdempotencyConflict, match="priority"):
        await durable_job_repository.admit_job(
            replace(_spec(job_id="job-743-4", dedupe_key="candidate-4"), priority=10)
        )


@pytest.mark.asyncio
async def test_unclaimed_failure_uses_atomic_service_owner_fence(async_db):
    admitted = await durable_job_repository.admit_job(_spec(job_id="job-743-5", dedupe_key="candidate-5"))

    wrong_owner = await durable_job_repository.fail_unclaimed_job(
        admitted["job_id"],
        owner_principal_id="service:other",
        service_id="service:other",
        reason="queue_error",
    )
    assert wrong_owner["status"] == "accepted"
    assert wrong_owner["receipt"]["status"] == "not_recorded"

    failed = await durable_job_repository.fail_unclaimed_job(
        admitted["job_id"],
        owner_principal_id="service:strategist",
        service_id="service:strategist",
        reason="queue_error",
    )
    assert failed["status"] == "failed"
    assert failed["failure_reason"] == "queue_error"


@pytest.mark.asyncio
async def test_claim_heartbeat_and_terminal_transition_share_revision_cas(async_db):
    admitted = await durable_job_repository.admit_job(
        _spec(job_id="job-743-cas", dedupe_key="candidate-cas")
    )
    await durable_job_repository.queue_job(admitted["job_id"], expected_revision=admitted["revision"])
    claimed = await durable_job_repository.claim_job(
        admitted["job_id"],
        owner="runner-cas",
        expected_revision=admitted["revision"] + 1,
    )
    token = claimed["lease"]["fencing_token"]
    heartbeated = await durable_job_repository.heartbeat_job(
        admitted["job_id"],
        owner="runner-cas",
        fencing_token=token,
        expected_revision=claimed["revision"],
    )
    verified = await durable_job_repository.record_readback(
        admitted["job_id"],
        target_path="result:cas",
        target_digest="result-cas",
        content_sha256="result-cas",
        status="succeeded",
        details={"verified": True, "capability": "cas"},
        owner="runner-cas",
        fencing_token=token,
        expected_revision=heartbeated["revision"],
    )

    with pytest.raises(DurableJobLeaseError, match="revision"):
        await durable_job_repository.transition_job(
            admitted["job_id"],
            "succeeded",
            owner="runner-cas",
            fencing_token=token,
            expected_revision=claimed["revision"],
        )

    terminal = await durable_job_repository.transition_job(
        admitted["job_id"],
        "succeeded",
        owner="runner-cas",
        fencing_token=token,
        expected_state="running",
        expected_revision=verified["revision"],
    )
    assert terminal["status"] == "succeeded"
    assert terminal["revision"] == verified["revision"] + 1


@pytest.mark.asyncio
async def test_expired_lease_transfer_requires_source_fence_and_increments_both_counters(async_db):
    admitted = await durable_job_repository.admit_job(
        _spec(job_id="job-743-transfer", dedupe_key="candidate-transfer")
    )
    await durable_job_repository.queue_job(admitted["job_id"])
    claimed = await durable_job_repository.claim_job(
        admitted["job_id"], owner="runner-old", lease_seconds=300
    )
    async with async_db() as db:
        await db.execute(
            update(WorkflowRunState)
            .where(WorkflowRunState.run_identity == admitted["job_id"])
            .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )

    with pytest.raises(DurableJobLeaseError):
        await durable_job_repository.transfer_lease(
            admitted["job_id"],
            owner="runner-new",
            expected_owner="runner-old",
            fencing_token=claimed["lease"]["fencing_token"] - 1,
            expected_revision=claimed["revision"],
        )
    transferred = await durable_job_repository.transfer_lease(
        admitted["job_id"],
        owner="runner-new",
        expected_owner="runner-old",
        fencing_token=claimed["lease"]["fencing_token"],
        expected_revision=claimed["revision"],
    )
    assert transferred["lease"]["owner"] == "runner-new"
    assert transferred["lease"]["fencing_token"] == claimed["lease"]["fencing_token"] + 1
    assert transferred["revision"] == claimed["revision"] + 1


@pytest.mark.asyncio
async def test_restart_recovery_keeps_unknown_effect_and_cost_liability_out_of_retry(async_db):
    admitted = await durable_job_repository.admit_job(
        _spec(job_id="job-743-unknown", dedupe_key="candidate-unknown")
    )
    await durable_job_repository.queue_job(admitted["job_id"])
    claimed = await durable_job_repository.claim_job(
        admitted["job_id"], owner="runner-unknown", lease_seconds=1
    )
    token = claimed["lease"]["fencing_token"]
    await durable_job_repository.record_effect(
        admitted["job_id"],
        effect_type="destination_write",
        target_path="controlled-ledger",
        target_digest="target-unknown",
        status="intent",
        details={"destination_ledger": "controlled", "payload": "redacted"},
        owner="runner-unknown",
        fencing_token=token,
    )
    recovered = await durable_job_repository.recover_stale_jobs(
        now=datetime.now(timezone.utc) + timedelta(seconds=5)
    )
    recovered_job = next(item for item in recovered if item["job_id"] == admitted["job_id"])
    assert recovered_job["status"] == "unknown_external_effect"
    assert recovered_job["receipt"]["recovery_state"] == "unknown_external_effect"
    with pytest.raises(DurableJobTransitionError, match="reconciliation"):
        await durable_job_repository.retry_job(
            admitted["job_id"],
            owner_kind="service",
            owner_principal_id="service:strategist",
            service_id="service:strategist",
            reconciliation_receipt={
                "effect_id": "missing-effect",
                "effect_type": "destination_write",
                "status": "read_back",
                "outcome": "absent",
            },
        )

    reconciled = await durable_job_repository.reconcile_external_effect(
        admitted["job_id"],
        owner_kind="service",
        owner_principal_id="service:strategist",
        service_id="service:strategist",
        reconciliation_receipt={
            "effect_id": next(
                item["effect_id"]
                for item in recovered_job["effects"]
                if item.get("status") == "intent"
            ),
            "effect_type": "destination_write",
            "status": "read_back",
            "target_path": "controlled-ledger",
            "target_digest": "target-unknown",
            "outcome": "absent",
        },
    )
    retried = await durable_job_repository.retry_job(
        admitted["job_id"],
        owner_kind="service",
        owner_principal_id="service:strategist",
        service_id="service:strategist",
        reconciliation_receipt={
            "effect_id": next(
                item["effect_id"]
                for item in reconciled["effects"]
                if item.get("effect_type") == "destination_write"
            ),
            "effect_type": "destination_write",
            "status": "read_back",
            "target_path": "controlled-ledger",
            "target_digest": "target-unknown",
            "outcome": "absent",
        },
        expected_revision=reconciled["revision"],
    )
    assert retried["status"] == "queued"

    cost_job = await durable_job_repository.admit_job(
        _spec(job_id="job-743-cost", dedupe_key="candidate-cost")
    )
    await durable_job_repository.queue_job(cost_job["job_id"])
    cost_claimed = await durable_job_repository.claim_job(
        cost_job["job_id"], owner="runner-cost", lease_seconds=1
    )
    cost_effect = await durable_job_repository.record_effect(
        cost_job["job_id"],
        effect_id="cost-effect",
        effect_type="remote_inference_admission",
        adapter_idempotency_key="operation-cost",
        status="unknown",
        details={"unknown_cost_outstanding": True},
        owner="runner-cost",
        fencing_token=cost_claimed["lease"]["fencing_token"],
    )
    cost_recovery = await durable_job_repository.recover_stale_jobs(
        now=datetime.now(timezone.utc) + timedelta(seconds=5)
    )
    cost_recovered = next(item for item in cost_recovery if item["job_id"] == cost_job["job_id"])
    assert cost_recovered["status"] == "cost_liability"
    with pytest.raises(DurableJobTransitionError, match="reconciliation"):
        await durable_job_repository.retry_job(
            cost_job["job_id"],
            owner_kind="service",
            owner_principal_id="service:strategist",
            service_id="service:strategist",
            reconciliation_receipt={
                "effect_id": cost_effect["receipt"]["effect_id"],
                "effect_type": "remote_inference_admission",
                "status": "settled",
                "actual_cost_microusd": 0,
                "adapter_idempotency_key": "operation-cost",
            },
        )
    with pytest.raises(DurableJobTransitionError, match="cost liability requires a settled receipt"):
        await durable_job_repository.reconcile_external_effect(
            cost_job["job_id"],
            owner_kind="service",
            owner_principal_id="service:strategist",
            service_id="service:strategist",
            reconciliation_receipt={
                "effect_id": cost_effect["receipt"]["effect_id"],
                "effect_type": "remote_inference_admission",
                "status": "read_back",
                "outcome": "absent",
            },
        )
    settled = await durable_job_repository.reconcile_external_effect(
        cost_job["job_id"],
        owner_kind="service",
        owner_principal_id="service:strategist",
        service_id="service:strategist",
        reconciliation_receipt={
            "effect_id": cost_effect["receipt"]["effect_id"],
            "effect_type": "remote_inference_admission",
            "status": "settled",
            "actual_cost_microusd": 0,
            "adapter_idempotency_key": "operation-cost",
        },
    )
    assert settled["status"] == "failed"
