"""Closed repository stop projection and legacy cancellation serialization."""

from datetime import datetime, timezone
import hashlib
import json

import pytest
from pydantic import ValidationError
from src.db.models import WorkBoardAttempt
from src.work_board.contracts import (
    GeneralTaskNativeCancelChildV1,
    GeneralTaskNativeChildBindingV1,
    GeneralTaskToolClosureV1,
    WorkBoardOwner,
    RepositoryNativeStopClosureV1,
)
from src.workflows.general_task_guard import read_manifest
from src.workflows.job_runtime import DurableJobLeaseError
from tests.test_general_task_native_guard import running_task
from tests.test_general_task_persistence import task_runtime
from tests.test_work_board_m6_provider_free_journey import OWNER, SESSION, isolated_runtime


def _binding(**changes):
    values = {
        "parent_job_id": "parent-job",
        "task_id": "task-id",
        "attempt_id": "attempt-id",
        "original_root_id": "root-session",
        "owner_principal_id": "principal",
        "goal_id": "goal-id",
        "goal_revision": 1,
        "original_deadline_at": datetime(2030, 1, 1, tzinfo=timezone.utc),
        "native_deadline_at": datetime(2030, 1, 1, tzinfo=timezone.utc),
        "original_envelope_digest": "a" * 64,
        "parent_authority_digest": "b" * 64,
        "creation_digest": "c" * 64,
        "creation_job_fence": 1,
        "creation_board_fence": 1,
        "plan_revision": 1,
        "plan_digest": "d" * 64,
        "step_id": "step-id",
        "invocation_id": "child-job",
        "input_digest": "e" * 64,
        "descriptor_digest": "f" * 64,
        "selected_grant_digest": "0" * 64,
        "phase_revision": 1,
        "phase_digest": "1" * 64,
        "live_root_digest": "2" * 64,
    }
    values.update(changes)
    return GeneralTaskNativeChildBindingV1(**values)


def _stop(binding):
    return RepositoryNativeStopClosureV1(
        original_binding=binding,
        repository_job_id="repository-root",
        repository_attempt_id="repository-attempt",
        repository_fence=1,
        original_input_digest=binding.input_digest,
        source_checkpoint_digest="3" * 64,
        original_group_digest="4" * 64,
        original_deadline_at=binding.original_deadline_at,
        original_claim_fence=0,
        iteration_ids=["iteration-1"],
        stop_reason="operator_cancelled",
        stop_intent_digest="5" * 64,
        model_quiescence_digest="6" * 64,
        process_quiescence_digest="7" * 64,
        all_original_accounting_digest="8" * 64,
        request_response_approval_digest="9" * 64,
        source_binding_digest="a" * 64,
    )


def _ordinary_closure(binding):
    return GeneralTaskToolClosureV1(
        original_binding_digest="3" * 64,
        invocation_id=binding.invocation_id,
        child_fence=1,
        descriptor_digest=binding.descriptor_digest,
        input_digest=binding.input_digest,
        outcome="approval_precontact",
        approval_id="approval-id",
        approval_fingerprint="4" * 64,
    )


def _child(*, binding=None, closure=None, repository_closure=None):
    binding = binding or _binding()
    return GeneralTaskNativeCancelChildV1(
        original_binding=binding,
        original_binding_digest="3" * 64,
        original_attempt_count=0,
        original_claim_fence=0,
        original_revision=1,
        current_child_fence=1,
        current_child_revision=1,
        effect_digest="4" * 64,
        artifact_digest="5" * 64,
        checkpoint_digest="6" * 64,
        closure=closure,
        repository_closure=repository_closure,
        effect_debt=False,
    )


def test_absent_repository_closure_preserves_legacy_null_and_hash_bytes():
    child = _child()
    serialized = child.model_dump_json()

    # This is the pre-R102 byte receipt for the same closed legacy payload.
    assert hashlib.sha256(serialized.encode()).hexdigest() == (
        "21da8f8e2e077c7e3be96ee4b01e439a0e5b131496751999c61f91290f45ed67"
    )
    payload = json.loads(serialized)
    assert "repository_closure" not in payload
    assert payload["closure"] is None
    assert child.model_dump(mode="json") == payload
    assert GeneralTaskNativeCancelChildV1.model_validate_json(serialized).model_dump_json() == serialized


def test_repository_closure_is_closed_metadata_only_and_binds_original_child():
    binding = _binding()
    child = _child(binding=binding, repository_closure=_stop(binding))
    payload = child.model_dump(mode="json")
    assert payload["repository_closure"]["schema_version"] == "repository.native_stop_closure.v1"
    assert payload["repository_closure"]["original_binding"] == binding.model_dump(mode="json")

    with pytest.raises(ValidationError, match="mutually exclusive"):
        _child(binding=binding, closure=_ordinary_closure(binding), repository_closure=_stop(binding))

    foreign = binding.model_copy(update={"task_id": "foreign-task"})
    with pytest.raises(ValidationError, match="binding changed"):
        _child(binding=binding, repository_closure=_stop(foreign))


async def _actual_zero_claim_child(task_runtime):
    from src.work_board.general_task_native import admit_native_step

    sessions, dispatcher, service, envelope, original = await running_task(task_runtime)
    jobs = dispatcher.jobs
    step = envelope.plan.steps[0]
    descriptor = next(item for item in service.registry.descriptors() if item.tool_id == step.tool_id)
    binding, _ = await admit_native_step(jobs, original["job"]["job_id"],
        owner=original["job"]["lease"]["owner"], fence=original["job"]["lease"]["fencing_token"],
        step=step, descriptor=descriptor, inputs=step.input)
    async with sessions() as db:
        manifest = read_manifest(await jobs._fetch(db, binding.parent_job_id))
    return sessions, jobs, binding, manifest


@pytest.mark.asyncio
async def test_public_repository_stop_projection_fails_closed_without_cas(task_runtime, monkeypatch):
    """A copied/public DTO cannot activate generic cancellation."""
    sessions, jobs, binding, manifest = await _actual_zero_claim_child(task_runtime)
    before_parent = await jobs.get_job(binding.parent_job_id)
    before_child = await jobs.get_job(binding.invocation_id)

    # The source-owned validator is intentionally the only issuer.  This
    # projection has the right shape but no private source seal.
    monkeypatch.setattr("src.work_board.pipelines.root_binding",
        lambda: (_ for _ in ()).throw(AssertionError("source stop must use staged Root binding")))
    with pytest.raises(DurableJobLeaseError):
        await jobs.cancel_general_task_native_parent(binding.parent_job_id,
            operator_owner=WorkBoardOwner(principal_id=OWNER, session_id=SESSION),
            expected_task_revision=manifest.task_revision,
            repository_stop_witness=_stop(binding),
        )

    assert await jobs.get_job(binding.parent_job_id) == before_parent
    assert await jobs.get_job(binding.invocation_id) == before_child
    async with sessions() as db:
        assert (await db.get(WorkBoardAttempt, binding.attempt_id)).cancel_requested_at is None


@pytest.mark.asyncio
async def test_repository_stop_current_sql_binding_is_rechecked_before_cas(task_runtime, monkeypatch):
    """A source-shaped witness with a foreign current binding cannot fence rows."""
    from src.workflows import repo_repair_source

    sessions, jobs, binding, manifest = await _actual_zero_claim_child(task_runtime)
    before_parent = await jobs.get_job(binding.parent_job_id)
    before_child = await jobs.get_job(binding.invocation_id)
    foreign = binding.model_copy(update={"task_id": "foreign-task"})

    async def forged_validator(db, witness, *, parent, task, attempt, children):
        return {binding.invocation_id: _stop(foreign)}

    monkeypatch.setattr(repo_repair_source, "validate_repository_stop_witness", forged_validator,
        raising=False)
    monkeypatch.setattr("src.work_board.pipelines.root_binding",
        lambda: (_ for _ in ()).throw(AssertionError("source stop must use staged Root binding")))
    with pytest.raises(DurableJobLeaseError, match="actual source-owned repository stop witness|required|current binding"):
        await jobs.cancel_general_task_native_parent(binding.parent_job_id,
            operator_owner=WorkBoardOwner(principal_id=OWNER, session_id=SESSION),
            expected_task_revision=manifest.task_revision,
            repository_stop_witness=object(),
        )

    assert await jobs.get_job(binding.parent_job_id) == before_parent
    assert await jobs.get_job(binding.invocation_id) == before_child
    async with sessions() as db:
        assert (await db.get(WorkBoardAttempt, binding.attempt_id)).cancel_requested_at is None


@pytest.mark.asyncio
async def test_repository_observer_owns_nonempty_source_result():
    from src.work_board.general_task import GeneralTaskService

    calls = []

    class Source:
        native_iteration_adapter = None

        async def observe_repository_stop(self, **kwargs):
            calls.append(kwargs)
            return {"source_stop": "observed"}

    service = GeneralTaskService(None, repository_source_service=Source())
    jobs = object()
    result = await service.observe_native_cancellation(jobs, "parent-id")
    assert result == {"source_stop": "observed"}
    assert calls == [{"general_task_service": service, "jobs": jobs, "parent_id": "parent-id"}]
