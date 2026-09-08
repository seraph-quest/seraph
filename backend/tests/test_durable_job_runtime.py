"""Focused proof for the bounded #743 durable invocation contract."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from src.db.engine import _ensure_legacy_columns, _map_legacy_workflow_status
from src.db.models import WorkflowRunState

from src.workflows.job_runtime import (
    DURABLE_JOB_STATUSES,
    DURABLE_JOB_TRANSITIONS,
    DurableJobIdentity,
    DurableJobIdempotencyConflict,
    DurableJobSpec,
    DurableJobLeaseError,
    DurableJobTransitionError,
    _admission_conflicts,
    _canonical_reconciliation_receipt,
    _digest,
    _safe_inputs_digest,
    _validate_admission_authority,
    _validate_retry_actor,
    durable_job_repository,
)


def test_transition_table_is_the_single_normative_lifecycle_contract():
    assert DURABLE_JOB_STATUSES == tuple(DURABLE_JOB_TRANSITIONS)
    assert DURABLE_JOB_TRANSITIONS["accepted"] == frozenset({"queued", "blocked", "failed", "cancelled"})
    assert DURABLE_JOB_TRANSITIONS["failed"] == frozenset({"queued"})
    assert DURABLE_JOB_TRANSITIONS["succeeded"] == frozenset()
    assert DURABLE_JOB_TRANSITIONS["cancelled"] == frozenset()


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
    )
    assert _admission_conflicts(
        existing,
        spec=spec,
        input_digest=input_digest,
        authority_digest=_digest(spec.declared_authority),
        deadline=None,
    ) == []
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
        {"status": "read_back", "secret_token": "must-not-persist"}
    )
    assert digest
    assert "must-not-persist" not in canonical


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
    retried = await durable_job_repository.retry_job(
        admitted["job_id"],
        owner_kind="service",
        owner_principal_id="service:strategist",
        service_id="service:strategist",
        reconciled=True,
        reconciliation_receipt={
            "effect_id": "destination-write-1",
            "status": "read_back",
            "readback_digest": "digest-1",
        },
    )

    assert failed["status"] == "failed"
    assert retried["status"] == "queued"
    assert retried["receipt"]["reconciliation_receipt_digest"]
    assert retried["effects"][0]["status"] == "reconciled"


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
