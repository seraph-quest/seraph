"""Actual authenticated paired event→private reviewed Task; no provider or model."""
import json

import pytest
from sqlalchemy import select

from src.db.models import WorkBoardTask, WorkflowRunState, TelegramInboundUpdate
from src.extensions.telegram_transport import TelegramTransportAdapter
from src.work_board.general_task import GeneralTaskService
from tests.test_general_task_contract import Registry
from tests.test_first_result_setup import authenticated_setup_operator, setup_workspace
from tests.test_telegram_task_controls import SyntheticTelegramHTTP

pytestmark = [pytest.mark.asyncio, pytest.mark.parametrize("async_db", ["file"], indirect=True)]


async def selected_pair(client, monkeypatch):
    from src.api.work_board import dispatcher
    service = GeneralTaskService(Registry())
    service.start()
    monkeypatch.setattr(dispatcher, "general_tasks", service)
    boundary = SyntheticTelegramHTTP()
    adapter = TelegramTransportAdapter(transport=boundary)
    monkeypatch.setattr("src.api.telegram.default_telegram_transport", adapter)
    goal = await client.post("/api/goals", json={"title": "Selected capture Goal"})
    assert goal.status_code == 200, goal.text
    paired = await client.post("/api/telegram/pair", json={"operator_id": 42, "chat_id": 77, "bot_token": "synthetic-only"})
    assert paired.status_code == 200, paired.text
    consent = await client.post("/api/telegram/consent", json={"boundary": "telegram_transit"})
    assert consent.status_code == 200, consent.text
    consent = await client.post("/api/telegram/consent", json={"boundary": "openrouter_inference"})
    assert consent.status_code == 200, consent.text
    current = (await client.get("/api/telegram/status")).json()
    chosen = await client.put("/api/telegram/capture-selection", json={
        "expected_revision": current["state_revision"], "enabled": True,
        "goal_id": goal.json()["id"], "goal_revision": goal.json()["revision"],
        "requested_output": {"type": "object"},
        "limits": {"max_inference_calls": 0, "max_cost_microusd": 0},
        "inference_egress_acknowledged": False,
    })
    assert chosen.status_code == 200, chosen.text
    return adapter, boundary, chosen.json(), service


async def test_selected_original_provider_event_creates_one_task_and_replays_original_group(client, async_db, setup_workspace, monkeypatch):
    adapter, boundary, chosen, service = await selected_pair(client, monkeypatch)
    event = {"update_id": 1, "message": {"message_id": 101,
        "from": {"id": 42}, "chat": {"id": 77}, "text": "/task Prepare a local reviewed proposal"}}
    first = await client.post("/api/telegram/updates", json=event)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "accepted", first.text
    task_id = first.json()["channel_task_capture"]["task_id"]
    assert "reservation" not in first.json()["channel_task_capture"]
    repeated = await client.post("/api/telegram/updates", json=event)
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["channel_task_capture"]["task_id"] == task_id
    assert repeated.json()["channel_task_capture"]["idempotent_replay"] is True
    read = await client.get(f"/api/work-board/tasks/{task_id}/plan")
    assert read.status_code == 200, read.text
    assert read.json()["plan"] is None
    assert read.json()["proposal_error"] == "channel_intent_review_required"
    assert read.json()["accepted"] is False and read.json()["no_learning"] is True
    async with async_db() as db:
        assert len((await db.execute(select(WorkBoardTask))).scalars().all()) == 1
        assert not (await db.execute(select(WorkflowRunState))).scalars().all()
        reserved = (await db.execute(select(TelegramInboundUpdate))).scalars().one()
        original = json.loads(reserved.receipt_json)["channel_task_capture"]["reservation"]
        assert original["proposal_group"]["max_inference_calls"] == 0
        assert original["proposal_group"]["max_cost_microusd"] == 0
        assert original["conversation_session_id"] == first.json()["session_id"]
    assert service.registry.calls == [] and boundary.messages == []
    await boundary.http.aclose()
