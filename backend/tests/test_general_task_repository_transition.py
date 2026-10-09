"""C1 repository transition fail-closed and SQLite CAS guards.

The source owner is the only issuer of repository witnesses.  These tests do
not construct a positive source witness: they prove that caller projections,
unsealed objects, and missing source-owner validators cannot move the original
native child or parent.
"""

import pytest

from tests.test_general_task_persistence import task_runtime
from tests.test_work_board_m6_provider_free_journey import isolated_runtime

from src.work_board.general_task_native import _is_repository_work_child
from src.workflows.general_task_guard import (
    _assert_repository_child_final_witness_shape,
    _assert_repository_child_wait_witness_shape,
    issue_repository_child_wait_witness,
)
from src.workflows.job_runtime import DurableJobLeaseError


def test_repository_route_requires_private_durable_input():
    assert _is_repository_work_child({
        "arguments_json": '{"tool_id":"repository_work"}',
    })
    assert _is_repository_work_child({
        "arguments": {"tool_id": "repository_work"},
    })
    # A public projection's tool_name or caller-selected field cannot select
    # the fixed source route.
    assert not _is_repository_work_child({"tool_name": "repository_work"})
    assert not _is_repository_work_child({"arguments_json": "{}"})


def test_arbitrary_projection_cannot_mint_repository_wait_witness():
    from src.workflows.repo_repair_source import _CanonicalRepositorySource

    class CallerProjection:
        def projection(self):
            return {"source_checkpoint": "caller-controlled"}

    sources = [
        {"source_checkpoint": "caller-controlled"},
        CallerProjection(),
        _CanonicalRepositorySource(
            parent_revision=1, parent_checkpoint_json="{}", parent_authority_json="{}",
            task_revision=1, parent_attempt_fence=1, repository_attempt_fence=1,
            child_revision=1, child_fence=1, child_authority_json="{}",
            child_arguments_json="{}", native_binding_json="{}", parent_row_json="{}",
            parent_envelope_json="{}",
            task_row_json="{}", parent_attempt_row_json="{}",
            repository_attempt_row_json="{}", child_row_json="{}",
            repository_task_row_json="{}", input_artifact_row_json="{}",
            consent_row_json="{}", consent_id="consent", _seal=None,
        ),
    ]
    for source in sources:
        with pytest.raises(DurableJobLeaseError):
            issue_repository_child_wait_witness(
                native_binding=object(),
                source_binding=source,
                repository_job_id="repo-root",
                repository_attempt_id="repo-attempt",
                repository_fence=1,
                iteration_index=1,
                iteration_id="a" * 64,
                source_checkpoint_digest="b" * 64,
                request_body_digest="c" * 64,
                response_readback_digest="d" * 64,
                callback_quiescence_digest="e" * 64,
            )


def test_unsealed_wait_and_final_objects_are_not_transition_authority():
    with pytest.raises(DurableJobLeaseError, match="sealed repository child wait"):
        _assert_repository_child_wait_witness_shape(object())
    with pytest.raises(DurableJobLeaseError, match="sealed repository child final"):
        _assert_repository_child_final_witness_shape(object())


@pytest.mark.asyncio
async def test_sqlite_wait_writer_rejects_unsealed_source_without_mutating_child(task_runtime):
    from tests.test_general_task_native_guard import admitted_child
    from src.work_board.general_task_native import publish_positive_claim

    _sessions, jobs, _binding_unused, _admitted_unused = await admitted_child(task_runtime)
    binding = _binding_unused
    await jobs.queue_job(binding.invocation_id)
    await jobs.claim_job(binding.invocation_id, owner="native-worker")
    await publish_positive_claim(jobs, binding, child_owner="native-worker", child_fence=1)
    before_child = await jobs.get_job(binding.invocation_id)
    before_parent = await jobs.get_job(binding.parent_job_id)

    with pytest.raises(DurableJobLeaseError, match="sealed repository child wait"):
        await jobs.publish_repository_child_wait(
            binding.invocation_id,
            owner="native-worker",
            fencing_token=1,
            expected_parent_revision=before_parent["revision"],
            producer_witness=object(),
        )

    after_child = await jobs.get_job(binding.invocation_id)
    after_parent = await jobs.get_job(binding.parent_job_id)
    assert after_child["status"] == before_child["status"] == "running"
    assert after_child["revision"] == before_child["revision"]
    assert after_child["lease"] == before_child["lease"]
    assert after_parent["revision"] == before_parent["revision"]
    assert not any(str(item.get("checkpoint_id", "")).startswith("repository:")
                   for item in after_parent["checkpoints"])


@pytest.mark.asyncio
async def test_sqlite_final_writer_rejects_unsealed_source_without_mutating_child(task_runtime):
    from tests.test_general_task_native_guard import admitted_child
    from src.work_board.general_task_native import publish_positive_claim

    _sessions, jobs, _binding_unused, _admitted_unused = await admitted_child(task_runtime)
    binding = _binding_unused
    await jobs.queue_job(binding.invocation_id)
    await jobs.claim_job(binding.invocation_id, owner="native-worker")
    await publish_positive_claim(jobs, binding, child_owner="native-worker", child_fence=1)
    before_child = await jobs.get_job(binding.invocation_id)
    before_parent = await jobs.get_job(binding.parent_job_id)

    with pytest.raises(DurableJobLeaseError, match="sealed repository child final"):
        await jobs.publish_general_task_step_receipt(
            binding.parent_job_id,
            staged_artifact=object(),
            child_id=binding.invocation_id,
            owner="native-worker",
            fencing_token=1,
            expected_parent_revision=before_parent["revision"],
            repository_final_witness=object(),
        )

    after_child = await jobs.get_job(binding.invocation_id)
    after_parent = await jobs.get_job(binding.parent_job_id)
    assert after_child["status"] == before_child["status"] == "running"
    assert after_child["revision"] == before_child["revision"]
    assert after_child["lease"] == before_child["lease"]
    assert after_parent["revision"] == before_parent["revision"]
    assert not any(str(item.get("checkpoint_id", "")).startswith("repository:")
                   for item in after_parent["checkpoints"])
