from __future__ import annotations

import pytest

from src.workflows.routine_steps import RoutineStepContext, guardian_watch_run
from src.workflows.routine_templates import (
    ROUTINE_STEP_IDS,
    render_runbook,
    render_workflow,
    validate_generated_files,
)
from src.workflows.routines import RoutineService, _child_job_id, _verified_readback


ROUTINE_ID = "0123456789abcdef0123456789abcdef"


def test_routine_template_is_fixed_order_and_not_user_invocable():
    workflow = render_workflow(routine_id=ROUTINE_ID, version=1, name="Research follow-through")
    runbook = render_runbook(routine_id=ROUTINE_ID, version=1, name="Research follow-through")
    result = validate_generated_files(
        workflow=workflow,
        runbook=runbook,
        routine_id=ROUTINE_ID,
        version=1,
    )
    assert result["valid"] is True
    assert result["step_order"] == list(ROUTINE_STEP_IDS)
    assert "user_invocable: false" in workflow
    assert "command:" not in runbook
    assert "routine_invocation_job_id" in workflow


def test_verified_readback_requires_successful_durable_job():
    assert not _verified_readback({"status": "succeeded", "effects": []})
    assert not _verified_readback(
        {
            "status": "succeeded",
            "effects": [{"receipt_kind": "readback", "status": "unknown", "reconciled": False}],
        }
    )
    assert _verified_readback(
        {
            "status": "succeeded",
            "effects": [{"receipt_kind": "readback", "status": "succeeded", "reconciled": True}],
        }
    )


@pytest.mark.asyncio
async def test_generic_routine_step_without_trusted_context_fails_closed():
    with pytest.raises(PermissionError):
        await guardian_watch_run("routine-invocation:job", context=None)


def test_routine_service_is_single_existing_job_surface():
    assert isinstance(RoutineService(), RoutineService)


def test_routine_children_are_uuid5_bound_to_invocation_and_step():
    invocation = "01234567-89ab-cdef-0123-456789abcdef"
    watch = _child_job_id(invocation, "watch")
    publication = _child_job_id(invocation, "publication")
    assert watch == _child_job_id(invocation, "watch")
    assert watch.startswith("routine-child:")
    assert publication.startswith("routine-child:")
    assert watch != publication


def test_routine_template_does_not_expose_arbitrary_step_arguments():
    workflow = render_workflow(routine_id=ROUTINE_ID, version=2, name="Guarded")
    assert "command" not in workflow
    assert "routine_invocation_job_id" in workflow
    assert "url" not in workflow
    assert workflow.index("guardian_watch_run") < workflow.index("github_followthrough")


def test_generated_routine_steps_are_available_through_native_loader():
    from src.native_tools.loader import reload_tools

    names = {item.name for item in reload_tools()}
    assert {"guardian_watch_run", "github_followthrough"}.issubset(names)


@pytest.mark.asyncio
async def test_watch_wrapper_dispatches_persisted_child_and_records_no_learning(monkeypatch):
    child_id = "routine-child:watch-test"
    child = {
        "job_id": child_id,
        "status": "running",
        "session_id": "session-1",
        "owner": {"principal_id": "principal-1"},
        "lease": {"owner": "runner-1", "fencing_token": 7},
        "revision": 1,
        "declared_authority": {
            "step_id": "guardian_watch_run",
            "parent_job_id": "routine-invocation-1",
            "routine_invocation_job_id": "routine-invocation-1",
            "source_watch_id": "watch-1",
            "source_watch_revision": 2,
        },
        "checkpoints": [],
    }

    class FakeJobs:
        async def get_job(self, _job_id):
            return child

        async def record_checkpoint(self, _job_id, **kwargs):
            child["revision"] += 1
            child["checkpoints"].append({"checkpoint_id": kwargs["checkpoint_id"], "payload": kwargs.get("checkpoint_payload")})
            return child

        async def record_effect(self, _job_id, **_kwargs):
            child["revision"] += 1
            return {"revision": child["revision"], "receipt": {"effect_id": "effect-1"}}

        async def record_readback(self, _job_id, **_kwargs):
            child["revision"] += 1
            return {"revision": child["revision"]}

        async def transition_job(self, _job_id, status, **_kwargs):
            child["status"] = status
            child["revision"] += 1
            return child

    class FakeWatch:
        async def run_watch(self, watch_id, **kwargs):
            assert watch_id == "watch-1"
            assert kwargs["occurrence_id"] == child_id
            assert kwargs["expected_plan_revision"] == 2
            return {"status": "no_change", "job_id": "m1-child-1"}

    import src.workflows.routines as routines_module

    monkeypatch.setattr(routines_module, "durable_job_repository", FakeJobs())
    monkeypatch.setattr(routines_module, "source_watch_service", FakeWatch())
    result = await RoutineService().execute_watch_step(
        child_id,
        context=RoutineStepContext("principal-1", "session-1", "runner-1", 7),
    )
    assert result["status"] == "no_change"
    assert result["child_status"] == "succeeded"
    assert child["status"] == "succeeded"


@pytest.mark.asyncio
async def test_followthrough_wrapper_uses_only_persisted_m3_child(monkeypatch):
    child_id = "routine-child:publication-test"
    child = {
        "job_id": child_id,
        "status": "running",
        "session_id": "session-1",
        "owner": {"principal_id": "principal-1"},
        "lease": {"owner": "runner-1", "fencing_token": 9},
        "revision": 1,
        "declared_authority": {
            "step_id": "github_followthrough",
            "parent_job_id": "routine-invocation-1",
            "routine_invocation_job_id": "routine-invocation-1",
        },
        "checkpoints": [{"checkpoint_id": "routine-child:prepared", "payload": {"m3_job_id": "ghfollow_1"}}],
    }

    class FakeJobs:
        async def get_job(self, _job_id):
            return child

        async def record_checkpoint(self, _job_id, **kwargs):
            child["revision"] += 1
            child["checkpoints"].append({"checkpoint_id": kwargs["checkpoint_id"], "payload": kwargs.get("checkpoint_payload")})
            return child

        async def record_effect(self, _job_id, **_kwargs):
            child["revision"] += 1
            return {"revision": child["revision"], "receipt": {"effect_id": "effect-2"}}

        async def record_readback(self, _job_id, **_kwargs):
            child["revision"] += 1
            return {"revision": child["revision"]}

        async def transition_job(self, _job_id, status, **_kwargs):
            child["status"] = status
            child["revision"] += 1
            return child

    class FakeFollowthrough:
        async def execute(self, *, owner_principal_id, job_id, owner_session_id, external_mutation_granted):
            assert owner_principal_id == "principal-1"
            assert job_id == "ghfollow_1"
            assert owner_session_id == "session-1"
            assert external_mutation_granted is True
            return {"status": "succeeded", "job_id": job_id}

    import src.workflows.routines as routines_module

    monkeypatch.setattr(routines_module, "durable_job_repository", FakeJobs())
    monkeypatch.setattr(routines_module, "GitHubFollowthroughService", FakeFollowthrough)
    result = await RoutineService().execute_followthrough_step(
        child_id,
        context=RoutineStepContext("principal-1", "session-1", "runner-1", 9, external_mutation_granted=True),
    )
    assert result["status"] == "succeeded"
    assert result["m3_job_id"] == "ghfollow_1"
    assert result["child_status"] == "succeeded"
    assert child["status"] == "succeeded"


@pytest.mark.asyncio
async def test_pause_cancels_m3_child_through_canonical_adapter(monkeypatch):
    """M4 owns the parent/child link; M3 owns cancellation of its job."""

    m4_child = {
        "job_id": "routine-child:publication-cancel",
        "status": "awaiting_approval",
        "revision": 4,
        "owner": {"principal_id": "principal-1"},
        "declared_authority": {
            "routine_id": "routine-1",
            "step_id": "github_followthrough",
        },
        "checkpoints": [
            {
                "checkpoint_id": "routine-child:prepared",
                "payload": {"m3_job_id": "ghfollow_pending"},
            }
        ],
    }
    cancelled_m4: list[str] = []
    cancelled_m3: list[tuple[str, str]] = []

    class FakeJobs:
        async def list_jobs(self, *, limit):
            assert limit == 100
            return [m4_child]

        async def cancel_job(self, job_id, **_kwargs):
            cancelled_m4.append(job_id)
            return {"job_id": job_id, "status": "cancelled"}

    class FakeFollowthrough:
        async def cancel(self, *, owner_principal_id, job_id):
            cancelled_m3.append((owner_principal_id, job_id))
            return {"job_id": job_id, "status": "cancelled"}

    import src.workflows.routines as routines_module

    monkeypatch.setattr(routines_module, "durable_job_repository", FakeJobs())
    monkeypatch.setattr(routines_module, "GitHubFollowthroughService", FakeFollowthrough)
    await RoutineService()._cancel_pending_jobs("routine-1", reason="routine_paused:user_request")

    assert cancelled_m3 == [("principal-1", "ghfollow_pending")]
    assert cancelled_m4 == ["routine-child:publication-cancel"]
