"""Capture boundary negatives; genuine source publication is tested separately."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from src.work_board.channel_capture import (
    CaptureIntentPublication, ChannelCaptureReservationV1,
)
from src.work_board.contracts import WorkBoardOwner
from src.work_board.general_task import GeneralTaskService, digest
from src.work_board.general_task_proposal import new_group
from src.work_board.repository import BoardError
from tests.test_general_task_contract import Registry, request
from tests.test_audio_native_execution import execution
from tests.test_audio_worker import audio_worker_authority


def reservation():
    owner = WorkBoardOwner(principal_id="operator:fixture", session_id="root-original")
    selected = request().model_copy(update={"plan": None, "expected_plan_revision": None})
    service = GeneralTaskService(Registry())
    service.start()
    descriptors, tool_digest = service.snapshot()
    task_input = selected.input.model_copy(update={"tool_set_digest": tool_digest})
    group = new_group(owner, task_input, descriptors, goal_revision=selected.goal_revision,
        request_key=selected.idempotency_key,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10))
    return owner, selected, service, ChannelCaptureReservationV1(
        owner_principal_id=owner.principal_id, original_root_id=owner.session_id,
        source_kind="audio", source_id="capture-original", canonical_message_id="message-original",
        conversation_session_id="conversation-original",
        source_digest="1" * 64, request_digest=digest(selected.model_dump(mode="json")),
        goal_revision=selected.goal_revision, idempotency_key=selected.idempotency_key,
        task_input=task_input, proposal_group=group)


@pytest.mark.parametrize("changes", [
    {"extra_authority": True}, {"goal_revision": True}, {"source_kind": "unpaired"},
    {"source_digest": "A" * 64}, {"original_root_id": "other-root"},
])
def test_closed_reservation_rejects_malformed_or_changed_original_identity(changes):
    original = reservation()[3]
    with pytest.raises(ValidationError):
        ChannelCaptureReservationV1.model_validate({**original.model_dump(), **changes})


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["mapping", "forged_seal", "missing"])
async def test_publication_denies_unissued_capture_before_private_read_or_source_callback(shape):
    owner, selected, service, original = reservation()
    source_check = AsyncMock()
    capture = (original.model_dump() if shape == "mapping" else
        CaptureIntentPublication(original, source_check, lambda: None, object())
        if shape == "forged_seal" else None)
    with pytest.raises(BoardError) as denied:
        await service.capture_intent(None, owner, selected, capture=capture)
    assert denied.value.code == "channel_capture_source_required"
    source_check.assert_not_called()
    assert service.registry.calls == []


@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.asyncio
async def test_actual_confirmed_native_message_publishes_one_review_task_without_audio_budget_transfer(execution, async_db):
    """Documentary issuer is scripted by execution; production audio stays blocked."""
    from sqlalchemy import select
    from src.db.models import Goal, WorkBoardTask, WorkflowRunState, InferenceCostReservation
    from src.work_board.contracts import GeneralTaskCreate, GeneralTaskInput, TaskLimits
    from src.work_board.channel_capture import reserve_confirmed_audio_capture
    from src.workflows.job_runtime import durable_job_repository as jobs
    from src.work_board.dispatcher import _parse_typed_input
    worker, source, contacts = execution
    result = await worker.process(source.request_id, audio_budget_microusd=100,
        owner_principal_id=source.owner_principal_id, operator_session_id=source.operator_session_id)
    confirmed = await worker.confirm_transcript(source.request_id, "Current confirmed Task intent",
        expected_transcript_digest=result.transcript_digest,
        owner_principal_id=source.owner_principal_id, operator_session_id=source.operator_session_id)
    owner = WorkBoardOwner(principal_id=source.owner_principal_id, session_id=source.operator_session_id)
    async with async_db() as db:
        db.add(Goal(id="channel-goal", title="Selected Task Goal", status="active",
            owner_principal_id=owner.principal_id, owner_session_id=owner.session_id, revision=1))
    service = GeneralTaskService(Registry(), planner=AsyncMock())
    service.start()
    selected = GeneralTaskCreate(goal_revision=1, idempotency_key="confirmed-original-task",
        input=GeneralTaskInput(goal_ref="channel-goal", intent="Current confirmed Task intent",
            requested_output={"type": "object"}, inference_egress_acknowledged=True,
            limits=TaskLimits(max_inference_calls=2, max_cost_microusd=500)))
    async def capture_once():
        async with async_db() as db:
            capture = await reserve_confirmed_audio_capture(db, owner, selected, service=service, jobs=jobs,
                request_id=source.request_id, message_id=confirmed.message_id,
                confirmed_digest=confirmed.confirmed_transcript_digest)
            mutation = await service.capture_intent(db, owner, selected, capture=capture)
            return mutation.task.task_id, capture.reservation
    task_id, original = await capture_once()
    same_task, replay = await capture_once()
    assert same_task == task_id and replay == original
    async with async_db() as db:
        tasks = (await db.execute(select(WorkBoardTask))).scalars().all()
        assert len(tasks) == 1
        task = tasks[0]
        assert task.status.value == "triage" and task.requires_review
        assert task.origin_session_id == source.session_id
        envelope = _parse_typed_input(task)
        assert envelope.get("plan") is None and envelope["proposal_error"] == "channel_intent_review_required"
        assert envelope["proposal_group"] == original.proposal_group.model_dump(mode="json")
        assert len((await db.execute(select(WorkflowRunState))).scalars().all()) == 1
        cost = (await db.execute(select(InferenceCostReservation))).scalars().one()
        assert cost.actual_cost_microusd == 66 and cost.state == "settled"
        assert cost.job_id == result.workflow_job_id
    service.planner.propose.assert_not_called()
    assert len(contacts) == 1 and service.registry.calls == []


@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.asyncio
async def test_authenticated_audio_api_separate_fresh_goal_task_action_and_exact_replay(client, execution, async_db, monkeypatch):
    from tests.test_audio_native_execution import test_authenticated_api_capture_execution_confirmation_and_source_read
    from src.api.work_board import dispatcher
    from src.db.models import AudioIngressJob, WorkBoardTask, InferenceCostReservation
    from sqlalchemy import select
    # Reuse the actual native producer's login, issued grants, physical capture,
    # final transport, settlement and exact corrected Message confirmation.
    await test_authenticated_api_capture_execution_confirmation_and_source_read(client, execution, monkeypatch)
    async with async_db() as db:
        audio = (await db.execute(select(AudioIngressJob).where(AudioIngressJob.status == "confirmed"))).scalars().one()
    service = GeneralTaskService(Registry(), planner=AsyncMock())
    service.start()
    monkeypatch.setattr(dispatcher, "general_tasks", service)
    origin = {"Origin": "http://localhost:3001"}
    goal = await client.post("/api/goals", json={"title": "Fresh explicit Task Goal"}, headers=origin)
    assert goal.status_code == 200, goal.text
    payload = {"confirmed_transcript_digest": audio.confirmed_transcript_digest,
        "task": {"goal_revision": goal.json()["revision"], "idempotency_key": "api-confirmed-original",
            "input": {"goal_ref": goal.json()["id"], "intent": "Explicit corrected API intent",
                "requested_output": {"type": "object"}, "inference_egress_acknowledged": True,
                "limits": {"max_cost_microusd": 500, "max_inference_calls": 2}}}}
    first = await client.post(f"/api/audio/ptt/{audio.request_id}/task", json=payload, headers=origin)
    assert first.status_code == 200, first.text
    assert first.json()["audio_budget_transferred"] is False
    source_read = await client.get(f"/api/audio/ptt/{audio.request_id}", headers=origin)
    assert source_read.status_code == 200, source_read.text
    from pathlib import Path
    import json
    from config.settings import settings
    capture_path = Path(settings.workspace_dir).parent / "channel-audio-task-wire-r6.json"
    capture_path.write_text(
        json.dumps({"source": source_read.json(), "request": payload, "receipt": first.json()}, sort_keys=True, indent=2) + "\n")
    repeated = await client.post(f"/api/audio/ptt/{audio.request_id}/task", json=payload, headers=origin)
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["idempotent_replay"] is True
    assert repeated.json()["task"]["task_id"] == first.json()["task"]["task_id"]
    plan = await client.get(f"/api/work-board/tasks/{first.json()['task']['task_id']}/plan")
    assert plan.status_code == 200, plan.text
    assert plan.json()["plan"] is None and plan.json()["proposal_error"] == "channel_intent_review_required"
    assert plan.json()["accepted"] is False and plan.json()["no_learning"] is True
    async with async_db() as db:
        assert len((await db.execute(select(WorkBoardTask))).scalars().all()) == 1
        cost = (await db.execute(select(InferenceCostReservation))).scalars().one()
        assert cost.actual_cost_microusd == 66 and cost.bound_microusd == 100
    service.planner.propose.assert_not_called()
    assert len(execution[2]) == 1 and service.registry.calls == []
