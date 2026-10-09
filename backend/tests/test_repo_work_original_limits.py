"""Original Source limits remain immutable before private reads or contact."""
import json
import asyncio
from dataclasses import replace

import pytest

from src.db.models import Goal, InferenceCostReservation
from sqlalchemy import select
from src.auth.service import authenticate_session
from src.model_fabric.effective_policy import configuration_mutation_lock
from src.workflows.job_runtime import DurableJobLeaseError, _digest
from src.workflows.repo_repair_source import (
    prepare_repository_native_source, prepare_repository_iteration,
    read_repository_inventory, read_repository_original,
)
from tests.test_general_task_planner import accounting_db, forbid_external_inference
from tests.repository_admission_lifecycle import repository_admission_signer
from tests.test_repo_work_task_publication import actual_native_source, _actual_source_callback_journey


async def actual_original_limits(accounting_db, monkeypatch):
    factory, owner, service, jobs, binding, request = await actual_native_source(
        accounting_db, monkeypatch, goal_capacity=2, claim_child=False)
    operator = await authenticate_session(owner.session_id, touch=False)
    prepared = await prepare_repository_native_source(service, jobs, binding,
        child_owner="actual-original-limits-worker", principal=operator.principal)
    return factory, owner, service.repository_source_service, jobs, prepared["repository_job_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["goal_budget", "goal_revision", "goal_owner", "goal_revoked",
    "policy_bound_lowered", "inventory_missing_limits", "inventory_digest", "inventory_unknown_field",
    "inventory_wrong_version", "inventory_mixed_version", "inventory_extra_identity", "inventory_missing_identity"])
async def test_actual_original_limits_drift_blocks_before_private_read(accounting_db, monkeypatch, repository_admission_signer, drift):
    factory, owner, source, jobs, root_id = await actual_original_limits(accounting_db, monkeypatch)
    async with factory() as db:
        root = await jobs._fetch(db, root_id)
        original = read_repository_original(root)[0]
        assert set(original) == {"schema_version", "repository_job_id", "repository_task_id",
            "repository_attempt_id", "repository_input_artifact_digest", "original_input", "compiled_input",
            "original_deadline_at", "original_max_cost_microusd", "group", "native_binding", "source_binding"}
        assert len(original["original_input"]) == 7
        inventory = read_repository_inventory(root)
        assert inventory["schema"] == "repository.checkpoint_inventory.v2"
        assert len(inventory["identities"]) == 45
        assert inventory["original_limits"]["original_server_bound_microusd"] > 0
        goal = await db.get(Goal, "goal:fixture")
        if drift == "goal_budget":
            budget = json.loads(goal.admission_budget_json)
            budget["max_outstanding_jobs"] = 3
            goal.admission_budget_json = json.dumps(budget)
        elif drift == "goal_revision":
            goal.revision += 1
        elif drift == "goal_owner":
            goal.owner_principal_id = "operator:changed"
        elif drift == "goal_revoked":
            goal.status = "paused"
        elif drift.startswith("inventory_"):
            journal = json.loads(root.checkpoint_receipts_json)
            record = next(item for item in journal if item["checkpoint_id"] == "repository:inventory:v1")
            if drift == "inventory_missing_limits":
                del record["payload"]["original_limits"]
            elif drift == "inventory_digest":
                record["payload"]["original_limits_digest"] = "0" * 64
            elif drift == "inventory_wrong_version":
                record["payload"]["schema"] = "repository.checkpoint_inventory.v3"
            elif drift == "inventory_mixed_version":
                record["payload"]["schema"] = "repository.checkpoint_inventory.v1"
            elif drift == "inventory_extra_identity":
                record["payload"]["identities"].append("repository:foreign:v1")
            elif drift == "inventory_missing_identity":
                record["payload"]["identities"].remove("repository:stop-uncertainty-successor:v1")
            else:
                record["payload"]["caller_authority"] = True
            record["state_digest"] = _digest(record["payload"])
            root.checkpoint_receipts_json = json.dumps(journal)
        await db.commit()
    if drift == "policy_bound_lowered":
        from src.model_fabric.configuration import read_model_fabric_configuration, write_model_fabric_configuration
        async with configuration_mutation_lock:
            configured = read_model_fabric_configuration()
            setup = replace(configured.openrouter_setup, request_cost_bound_microusd=1)
            write_model_fabric_configuration(replace(configured, openrouter_setup=setup,
                egress_revision=configured.egress_revision + 1), expected_revision=configured.egress_revision)
    reads = []
    def private_read(*args, **kwargs):
        reads.append(args)
        raise AssertionError("Changed original authority cannot open private Source bytes")
    monkeypatch.setattr(source, "_read_private_artifact", private_read)
    with pytest.raises(DurableJobLeaseError):
        await prepare_repository_iteration(source, jobs, job_id=root_id, owner=owner, iteration_index=1)
    assert reads == []
    async with factory() as db:
        root = await jobs._fetch(db, root_id)
        assert root.status == "running"
        assert jobs._repo_repair_reservation_state(root)["status"] == "held"
        assert list((await db.scalars(select(InferenceCostReservation))).all()) == []
    assert root_id in source._iterative_lanes


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["test_python", "test_node"])
@pytest.mark.parametrize("cause", ["cost_exhausted", "shared_group_exhausted"])
async def test_actual_original_limit_after_failed_patch_releases_only_terminal(accounting_db, monkeypatch, repository_admission_signer, language, cause):
    from src.work_board.contracts import TaskLimits
    task_limits = TaskLimits(max_inference_calls=1 if cause == "shared_group_exhausted" else 5,
        max_cost_microusd=500, wall_seconds=900)
    work_limits = {"max_iterations": 3, "max_total_seconds": 900,
        "max_cost_usd": 0.000100 if cause == "cost_exhausted" else 0.0010019}
    await _actual_source_callback_journey(accounting_db, monkeypatch, True, language,
        stop_at=cause, task_limits=task_limits, work_limits=work_limits,
        actual_model_cost_microusd=100 if cause == "cost_exhausted" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("rollback", [False, True])
async def test_actual_automatic_stop_fence_blocks_policy_mutation_through_writer(accounting_db, monkeypatch, repository_admission_signer, rollback):
    from types import SimpleNamespace
    from src.workflows import repo_repair_source as source_module
    from src.model_fabric import effective_policy
    from src.model_fabric.configuration import read_model_fabric_configuration
    # Each pytest case has a different event loop. All real owners import
    # this same configuration lock; no Source authority is replaced.
    monkeypatch.setattr(effective_policy, "configuration_mutation_lock", asyncio.Lock())
    factory, owner, service, jobs, binding, _ = await actual_native_source(accounting_db, monkeypatch,
        goal_capacity=2, claim_child=False,
        work_limits={"max_iterations": 3, "max_total_seconds": 900, "max_cost_usd": 0.000050})
    operator = await authenticate_session(owner.session_id, touch=False)
    configured = read_model_fabric_configuration()
    reached, release = asyncio.Event(), asyncio.Event()
    original_validator = source_module.validate_repository_stop_witness
    original_cancel = jobs.cancel_general_task_native_parent
    async def actual_validator(*args, **kwargs):
        result = await original_validator(*args, **kwargs)
        reached.set()
        await release.wait()
        if rollback:
            raise DurableJobLeaseError("forced actual automatic writer rollback")
        return result
    monkeypatch.setattr(source_module, "validate_repository_stop_witness", actual_validator)
    def forbidden(*args, **kwargs):
        raise AssertionError("Terminal writer cannot read policy or private artifacts")
    async def actual_cancel(*args, **kwargs):
        with monkeypatch.context() as writer:
            writer.setattr(source_module, "_repository_policy_limits", forbidden)
            writer.setattr(service.repository_source_service, "_read_private_artifact", forbidden)
            writer.setattr(effective_policy, "read_model_fabric_configuration", forbidden)
            return await original_cancel(*args, **kwargs)
    monkeypatch.setattr(jobs, "cancel_general_task_native_parent", actual_cancel)
    preparing = asyncio.create_task(prepare_repository_native_source(service, jobs, binding,
        child_owner="actual-limit-race-worker", principal=operator.principal))
    await asyncio.wait_for(reached.wait(), timeout=15)
    mutation = asyncio.create_task(effective_policy.revoke_effective_policy(
        SimpleNamespace(state=SimpleNamespace(operator=operator)),
        SimpleNamespace(grant_id="provider_policy:openrouter", expected_revision=configured.egress_revision,
            idempotency_key="actual-automatic-stop-revoke")))
    await asyncio.sleep(0.1)
    assert not mutation.done()
    release.set()
    outcome = await asyncio.wait_for(preparing, timeout=15)
    actual_mutation = await asyncio.wait_for(mutation, timeout=15)
    assert actual_mutation["status"] == "revoked"
    root_id = outcome["repository_job_id"]
    assert outcome["stop_reason"] == "cost_exhausted" and outcome["stop_pending"] is rollback
    async with factory() as db:
        root = await jobs._fetch(db, root_id)
        assert jobs._repo_repair_reservation_state(root)["status"] == ("held" if rollback else "released")
        assert root.status == ("running" if rollback else "failed")
        assert list((await db.scalars(select(InferenceCostReservation))).all()) == []
    assert (root_id in service.repository_source_service._iterative_lanes) is rollback


@pytest.mark.asyncio
@pytest.mark.parametrize("cutoff_kind", ["repository_wall", "c1_wall", "goal_runtime", "goal_due", "goal_period"])
async def test_actual_original_temporal_cutoff_stops_without_renewal(accounting_db, monkeypatch, repository_admission_signer, cutoff_kind):
    from datetime import datetime, timedelta, timezone
    from src.work_board.contracts import TaskLimits
    from src.workflows.repo_repair_source import _repository_record
    from src.workflows.repo_repair_stop import stop_repository_root
    now = datetime.now(timezone.utc)
    task_limits = TaskLimits(max_inference_calls=5, max_cost_microusd=500,
        wall_seconds=10 if cutoff_kind == "c1_wall" else 900)
    work_limits = {"max_iterations": 3, "max_total_seconds": 10 if cutoff_kind == "repository_wall" else 900,
        "max_cost_usd": 0.0010019}
    goal_limits = {"budget": {"max_runtime_seconds": 10}} if cutoff_kind == "goal_runtime" else (
        {"due_date": now + timedelta(seconds=10)} if cutoff_kind == "goal_due" else
        {"budget": {"period_started_at": now - timedelta(seconds=1),
            "period_expires_at": now + timedelta(seconds=10)}} if cutoff_kind == "goal_period" else None)
    factory, owner, service, jobs, binding, _ = await actual_native_source(accounting_db, monkeypatch,
        goal_capacity=2, claim_child=False, task_limits=task_limits, work_limits=work_limits, goal_limits=goal_limits)
    operator = await authenticate_session(owner.session_id, touch=False)
    prepared = await prepare_repository_native_source(service, jobs, binding,
        child_owner="actual-original-cutoff-worker", principal=operator.principal)
    root_id = prepared["repository_job_id"]
    async with factory() as db:
        root = await jobs._fetch(db, root_id)
        before = read_repository_original(root)
        cutoff = datetime.fromisoformat(before[0]["original_deadline_at"])
        native_before = await jobs._fetch(db, binding.invocation_id)
        original_fence, original_deadline = native_before.fencing_token, native_before.deadline_at
    await asyncio.sleep(max(0, (cutoff - datetime.now(timezone.utc)).total_seconds()) + 0.05)
    reason = "goal_limit_exhausted" if cutoff_kind.startswith("goal_") else "deadline_exhausted"
    stopped = await stop_repository_root(service.repository_source_service, jobs, job_id=root_id,
        owner=owner, general_task_service=service, reason=reason)
    assert stopped["pending"] is False
    async with factory() as db:
        root = await jobs._fetch(db, root_id)
        assert root.status == "failed"
        assert read_repository_original(root)[0] == before[0]
        assert jobs._repo_repair_reservation_state(root)["status"] == "released"
        closure = _repository_record(root, "repository:terminal:v1")["closure"]
        assert closure["stop_reason"] == reason and closure["limit_evidence"]["group_calls"] == 0
        native_after = await jobs._fetch(db, binding.invocation_id)
        assert native_after.fencing_token == original_fence + 1 and native_after.deadline_at == original_deadline
        assert native_after.attempt_count == 1 and native_after.status == "cancelled"
        assert list((await db.scalars(select(InferenceCostReservation))).all()) == []
    assert root_id not in service.repository_source_service._iterative_lanes


@pytest.mark.asyncio
async def test_actual_later_goal_cutoff_cannot_govern_original_repository_stop(accounting_db, monkeypatch, repository_admission_signer):
    from datetime import datetime, timedelta, timezone
    from src.workflows.repo_repair_stop import stop_repository_root
    now = datetime.now(timezone.utc)
    factory, owner, service, jobs, binding, _ = await actual_native_source(
        accounting_db, monkeypatch, goal_capacity=2, claim_child=False,
        work_limits={"max_iterations": 3, "max_total_seconds": 10, "max_cost_usd": 0.0010019},
        goal_limits={"due_date": now + timedelta(seconds=15)})
    operator = await authenticate_session(owner.session_id, touch=False)
    prepared = await prepare_repository_native_source(service, jobs, binding,
        child_owner="actual-later-goal-worker", principal=operator.principal)
    root_id, source = prepared["repository_job_id"], service.repository_source_service
    async with factory() as db:
        root = await jobs._fetch(db, root_id)
        original = read_repository_original(root)[0]
        limits = read_repository_inventory(root)["original_limits"]
        cutoff = datetime.fromisoformat(original["original_deadline_at"])
        goal_cutoff = datetime.fromisoformat(limits["original_goal_limits"]["due_date"])
        assert cutoff < goal_cutoff
    await asyncio.sleep(max(0, (goal_cutoff - datetime.now(timezone.utc)).total_seconds()) + 0.05)
    reads = []
    original_read = source._read_private_artifact
    def forbidden_read(*args, **kwargs):
        reads.append(args)
        raise AssertionError("Wrong governing Goal cause cannot reopen private Source")
    monkeypatch.setattr(source, "_read_private_artifact", forbidden_read)
    with pytest.raises(DurableJobLeaseError, match="limit cause is unproven"):
        await stop_repository_root(source, jobs, job_id=root_id, owner=owner,
            general_task_service=service, reason="goal_limit_exhausted")
    assert reads == []
    async with factory() as db:
        root = await jobs._fetch(db, root_id)
        assert root.status == "running" and jobs._repo_repair_reservation_state(root)["status"] == "held"
        assert read_repository_original(root)[0] == original
        assert list((await db.scalars(select(InferenceCostReservation))).all()) == []
    monkeypatch.setattr(source, "_read_private_artifact", original_read)
    stopped = await stop_repository_root(source, jobs, job_id=root_id, owner=owner,
        general_task_service=service, reason="deadline_exhausted")
    assert stopped["pending"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("zero_limit", ["root_cost", "group_cost", "group_calls"])
async def test_actual_original_zero_allowance_stops_before_contact(accounting_db, monkeypatch, repository_admission_signer, zero_limit):
    from src.work_board.contracts import TaskLimits
    task_limits = TaskLimits(max_inference_calls=0 if zero_limit == "group_calls" else 5,
        max_cost_microusd=0 if zero_limit == "group_cost" else 500, wall_seconds=900)
    work_limits = {"max_iterations": 3, "max_total_seconds": 900,
        "max_cost_usd": 0.0 if zero_limit == "root_cost" else 0.0010019}
    factory, owner, service, jobs, binding, _ = await actual_native_source(accounting_db, monkeypatch,
        goal_capacity=2, claim_child=False, task_limits=task_limits, work_limits=work_limits)
    operator = await authenticate_session(owner.session_id, touch=False)
    stopped = await prepare_repository_native_source(service, jobs, binding,
        child_owner="actual-original-zero-worker", principal=operator.principal)
    assert stopped["stop_pending"] is False
    assert stopped["stop_reason"] == ("cost_exhausted" if zero_limit == "root_cost" else "shared_group_exhausted")
    async with factory() as db:
        root = await jobs._fetch(db, stopped["repository_job_id"])
        assert root.status == "failed" and jobs._repo_repair_reservation_state(root)["status"] == "released"
        assert list((await db.scalars(select(InferenceCostReservation))).all()) == []
    assert stopped["repository_job_id"] not in service.repository_source_service._iterative_lanes


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["artifact_metadata", "artifact_ttl", "envelope_group", "envelope_source",
    "wrong_actor", "no_automatic_cause"])
async def test_actual_expired_cleanup_does_not_grant_from_changed_artifact(accounting_db, monkeypatch, repository_admission_signer, drift):
    from datetime import datetime, timedelta, timezone
    from src.db.models import WorkBoardTask, WorkBoardInputArtifact
    from src.work_board.contracts import TaskLimits, WorkBoardOwner
    from src.work_board.repository import BoardError
    from src.workflows.inference_accounting import InferenceAccountingError
    from src.workflows.repo_repair_stop import stop_repository_root
    from src.work_board.input_artifacts import _payload_path
    factory, owner, service, jobs, binding, _ = await actual_native_source(accounting_db, monkeypatch,
        goal_capacity=2, claim_child=False,
        task_limits=TaskLimits(max_inference_calls=5, max_cost_microusd=500, wall_seconds=8))
    operator = await authenticate_session(owner.session_id, touch=False)
    prepared = await prepare_repository_native_source(service, jobs, binding,
        child_owner="actual-expired-original-worker", principal=operator.principal)
    root_id = prepared["repository_job_id"]
    async with factory() as db:
        root = await jobs._fetch(db, root_id)
        cutoff = datetime.fromisoformat(read_repository_original(root)[0]["original_deadline_at"])
    await asyncio.sleep(max(0, (cutoff - datetime.now(timezone.utc)).total_seconds()) + 0.05)
    async with factory() as db:
        task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == binding.task_id))
        artifact = await db.get(WorkBoardInputArtifact, task.input_artifact_id)
        if drift == "artifact_metadata":
            artifact.metadata_digest = "0" * 64
        elif drift == "artifact_ttl":
            artifact.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        elif drift.startswith("envelope_"):
            path = _payload_path(artifact)
            payload = json.loads(path.read_bytes())
            if drift == "envelope_group":
                payload["input"]["proposal_group"]["max_inference_calls"] = 12
            else:
                payload["input"]["repository_source"]["original_input_digest"] = "0" * 64
            path.write_bytes(json.dumps(payload).encode())
        await db.commit()
    reads = []
    def private_read(*args, **kwargs):
        reads.append(args)
        raise AssertionError("Changed expired cleanup cannot reopen selected Source")
    monkeypatch.setattr(service.repository_source_service, "_read_private_artifact", private_read)
    actual_owner = WorkBoardOwner(principal_id="operator:wrong", session_id=owner.session_id) if drift == "wrong_actor" else owner
    reason = "operator_cancelled" if drift == "no_automatic_cause" else "deadline_exhausted"
    with pytest.raises((DurableJobLeaseError, BoardError, InferenceAccountingError)):
        await stop_repository_root(service.repository_source_service, jobs,
            job_id=root_id, owner=actual_owner, general_task_service=service, reason=reason)
    assert reads == []
    async with factory() as db:
        root = await jobs._fetch(db, root_id)
        assert root.status == "running" and jobs._repo_repair_reservation_state(root)["status"] == "held"
        assert list((await db.scalars(select(InferenceCostReservation))).all()) == []
    assert root_id in service.repository_source_service._iterative_lanes
