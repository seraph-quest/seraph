"""Actual authenticated paired event→private reviewed Task; no provider or model."""
import asyncio
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


async def selected_pair(client, monkeypatch, registry=None):
    from src.api.work_board import dispatcher
    service = GeneralTaskService(registry or Registry())
    service.start()
    monkeypatch.setattr(dispatcher, "general_tasks", service)
    boundary = SyntheticTelegramHTTP()
    adapter = TelegramTransportAdapter(transport=boundary)
    monkeypatch.setattr("src.api.telegram.default_telegram_transport", adapter)
    monkeypatch.setattr("src.extensions.telegram_transport.default_telegram_transport", adapter)
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
    revision = first.json()["channel_task_capture"]["task_revision"]
    inspected = await client.post("/api/telegram/task-actions", json={
        "task_id": task_id, "action": "inspect", "expected_revision": revision})
    assert inspected.status_code == 200, inspected.text
    assert inspected.json() == {"task_id": task_id, "task_revision": revision,
        "task_status": "triage", "action": "inspect", "review_required": True, "no_learning": True}
    assert "intent" not in inspected.text and "reservation" not in inspected.text
    invalid = await client.post("/api/telegram/task-actions", json={
        "task_id": task_id, "action": "inspect", "expected_revision": revision, "approve": True})
    assert invalid.status_code == 422
    stale = await client.post("/api/telegram/task-actions", json={
        "task_id": task_id, "action": "inspect", "expected_revision": revision + 1})
    assert stale.status_code == 409
    async with async_db() as db:
        assert len((await db.execute(select(WorkBoardTask))).scalars().all()) == 1
        assert not (await db.execute(select(WorkflowRunState))).scalars().all()
        reserved = (await db.execute(select(TelegramInboundUpdate))).scalars().one()
        original = json.loads(reserved.receipt_json)["channel_task_capture"]["reservation"]
        assert original["proposal_group"]["max_inference_calls"] == 0
        assert original["proposal_group"]["max_cost_microusd"] == 0
        assert original["conversation_session_id"] == first.json()["session_id"]
    assert service.registry.calls == [] and boundary.messages == []
    async with async_db() as db:
        published = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
        original_marker = published.channel_capture_origin_json
        original_idempotency = published.idempotency_binding
        assert original_marker and len(original_marker.encode()) <= 4096
        assert json.loads(original_marker)["origin"]["task_id"] == task_id
    def private_read_trap(_task):
        raise AssertionError("revoked source reached private Task input")
    monkeypatch.setattr("src.work_board.dispatcher._parse_typed_input", private_read_trap)
    revoked = await client.post("/api/telegram/consent/telegram_transit/revoke")
    assert revoked.status_code == 200, revoked.text
    blocked = await client.post("/api/telegram/task-actions", json={
        "task_id": task_id, "action": "inspect", "expected_revision": revision})
    assert blocked.status_code != 200 and "intent" not in blocked.text
    blocked_plan = await client.get(f"/api/work-board/tasks/{task_id}/plan")
    assert blocked_plan.status_code in (403, 409), blocked_plan.text
    async with async_db() as db:
        published = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
        assert published.channel_capture_origin_json == original_marker
        assert published.idempotency_binding == original_idempotency
    await boundary.http.aclose()


async def test_captured_origin_tampering_and_missing_source_deny_before_private_input(client, async_db, setup_workspace, monkeypatch):
    adapter, boundary, chosen, service = await selected_pair(client, monkeypatch)
    response = await client.post("/api/telegram/updates", json={"update_id": 31, "message": {
        "message_id": 131, "from": {"id": 42}, "chat": {"id": 77}, "text": "/task Keep original provenance"}})
    assert response.status_code == 200, response.text
    task_id = response.json()["channel_task_capture"]["task_id"]
    async with async_db() as db:
        task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
        original = task.channel_capture_origin_json
    second = await client.post("/api/telegram/updates", json={"update_id": 32, "message": {
        "message_id": 132, "from": {"id": 42}, "chat": {"id": 77}, "text": "/task Independent original provenance"}})
    assert second.status_code == 200, second.text
    second_id = second.json()["channel_task_capture"]["task_id"]
    async with async_db() as db:
        other = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == second_id))
        other.channel_capture_origin_json = original
        await db.commit()
    def private_read_trap(_task):
        raise AssertionError("changed capture reached private input")
    monkeypatch.setattr("src.work_board.dispatcher._parse_typed_input", private_read_trap)
    copied = await client.get(f"/api/work-board/tasks/{second_id}/plan")
    assert copied.status_code == 409, copied.text
    changed = json.loads(original)
    changed["origin"]["source_id"] = "different-event"
    for marker in (None, "{}", json.dumps(changed)):
        async with async_db() as db:
            task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
            task.channel_capture_origin_json = marker
            await db.commit()
        blocked = await client.get(f"/api/work-board/tasks/{task_id}/plan")
        assert blocked.status_code == 409, blocked.text
    async with async_db() as db:
        task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
        task.channel_capture_origin_json = original
        source = await db.scalar(select(TelegramInboundUpdate).where(
            TelegramInboundUpdate.idempotency_key == json.loads(original)["origin"]["source_id"]))
        original_receipt = source.receipt_json
        source.receipt_json = "{malformed"
        await db.commit()
    malformed = await client.get(f"/api/work-board/tasks/{task_id}/plan")
    assert malformed.status_code == 409, malformed.text
    async with async_db() as db:
        source = await db.scalar(select(TelegramInboundUpdate).where(
            TelegramInboundUpdate.idempotency_key == json.loads(original)["origin"]["source_id"]))
        source.receipt_json = original_receipt
        await db.delete(source)
        await db.commit()
    blocked = await client.get(f"/api/work-board/tasks/{task_id}/plan")
    assert blocked.status_code == 409, blocked.text
    await boundary.http.aclose()


@pytest.mark.parametrize("completion", ["delivered", "unknown", "revoked_before_resume"])
async def test_actual_captured_task_native_pause_resume_keeps_original_attempt_and_reads_physical_output(client, async_db, setup_workspace, monkeypatch, completion):
    from src.native_tools.registry import ToolRegistry
    from src.api.work_board import dispatcher
    from src.work_board.contracts import WorkBoardOwner, GeneralTaskEnvelope
    from src.work_board.dispatcher import _parse_typed_input
    from src.work_board.general_task_runtime_artifacts import initial_native_manifest
    from src.workflows.job_runtime import _digest
    from src.db.models import WorkBoardAttempt
    registry = ToolRegistry(); registry.start()
    adapter, boundary, chosen, service = await selected_pair(client, monkeypatch, registry)
    from config.settings import settings
    from pathlib import Path
    physical = Path(settings.workspace_dir) / "captured-original.txt"
    physical.write_text("Original private physical source.\n")
    first = await client.post("/api/telegram/updates", json={"update_id": 2, "message": {
        "message_id": 102, "from": {"id": 42}, "chat": {"id": 77}, "text": "/task Read captured-original.txt"}})
    assert first.status_code == 200, first.text
    task_id = first.json()["channel_task_capture"]["task_id"]
    revision = first.json()["channel_task_capture"]["task_revision"]
    descriptor = next(item for item in service.snapshot()[0] if item.tool_id == "read_file")
    edited = await client.post(f"/api/work-board/tasks/{task_id}/plan", json={
        "expected_revision": revision, "expected_plan_revision": 0, "idempotency_key": "captured-read-plan",
        "plan": {"revision": 1, "steps": [{"step_id": "read", "tool_id": "read_file",
            "input": {"file_path": physical.name}, "output_contract": descriptor.output_schema}]}})
    assert edited.status_code == 200, edited.text
    card = edited.json()["task"]
    accepted = await client.post(f"/api/work-board/tasks/{task_id}/actions", json={"action": "promote", "expected_revision": card["task_revision"]})
    assert accepted.status_code == 200, accepted.text
    async with async_db() as db:
        task = (await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))).scalars().one()
        ready = await service.repository.promote_task_ready(db, task_id, expected_revision=task.task_revision,
            actor_principal_id=dispatcher.runner_id, actor_session_id=dispatcher.runner_session)
    async with async_db() as db:
        claimed = await service.repository.claim_ready_task(db, task_id, expected_revision=ready.task.task_revision, lease_owner=dispatcher.runner_id)
    task, attempt = claimed.task, claimed.attempt
    spec, inputs, *_ = dispatcher._build_spec(task, attempt)
    admitted = await dispatcher.jobs.admit_job(spec)
    identity = {"job_id": spec.identity.job_id, "owner_kind": spec.identity.owner_kind,
        "owner_principal_id": spec.identity.owner_principal_id, "service_id": spec.service_id,
        "operator_session_id": spec.operator_session_id, "session_id": spec.session_id,
        "goal_id": spec.goal_id, "goal_revision": spec.goal_revision,
        "job_kind": spec.identity.job_kind, "capability_version": spec.identity.capability_version,
        "input_digest": _digest(spec.inputs), "authority_digest": _digest(spec.declared_authority),
        "run_fingerprint": spec.run_fingerprint, "idempotency_scope": spec.identity.idempotency_scope,
        "idempotency_key": spec.identity.idempotency_key}
    async with async_db() as db:
        linked = await service.repository.link_attempt_workflow_run(db, task_id, attempt.attempt_id,
            workflow_run_id=spec.identity.job_id, expected_revision=task.task_revision,
            board_fence=attempt.fencing_token, lease_owner=attempt.lease_owner,
            workflow_projection=admitted, expected_identity=identity)
    task, attempt = linked.task, linked.attempt
    await dispatcher.jobs.queue_job(spec.identity.job_id)
    parent = await dispatcher.jobs.claim_job(spec.identity.job_id, owner=f"{dispatcher.runner_id}:{attempt.attempt_id}")
    envelope = GeneralTaskEnvelope.model_validate(inputs)
    async with async_db() as db:
        row = await dispatcher.jobs._fetch(db, spec.identity.job_id)
        manifest = initial_native_manifest(row, task, attempt, envelope)
    native = await dispatcher.jobs.replace_general_task_manifest(spec.identity.job_id, manifest=manifest,
        owner=parent["lease"]["owner"], fencing_token=parent["lease"]["fencing_token"], expected_revision=parent["revision"])
    original_parent = await dispatcher.jobs.get_job(spec.identity.job_id)
    paused = await client.post("/api/telegram/task-actions", json={"task_id": task_id, "action": "pause", "expected_revision": native["manifest"]["task_revision"]})
    assert paused.status_code == 200, paused.text
    assert paused.json()["task_status"] == "blocked"
    if completion == "revoked_before_resume":
        original_control = dispatcher.control_general_task
        concurrent = TelegramTransportAdapter(transport=boundary)
        async def revoke_between_inspect_and_native(owner, selected_task, **bindings):
            await concurrent.revoke_consent(owner_principal_id=owner.principal_id,
                operator_session_id=owner.session_id, boundary="openrouter_inference")
            return await original_control(owner, selected_task, **bindings)
        monkeypatch.setattr(dispatcher, "control_general_task", revoke_between_inspect_and_native)
        def private_read_trap(*_args, **_kwargs):
            raise AssertionError("revoked channel entered private native input reader")
        monkeypatch.setattr("src.work_board.general_task_runtime_artifacts.verify_general_task_manifest", private_read_trap)
        refused = await asyncio.wait_for(client.post("/api/telegram/task-actions", json={
            "task_id": task_id, "action": "resume", "expected_revision": paused.json()["task_revision"]}), timeout=15)
        assert refused.status_code in (403, 409), refused.text
        current_parent = await dispatcher.jobs.get_job(spec.identity.job_id)
        assert current_parent["status"] == "paused"
        assert current_parent["deadline_at"] == original_parent["deadline_at"]
        assert current_parent["attempt_count"] == original_parent["attempt_count"] == 1
        async with async_db() as db:
            retained_task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
            original_attempt = await db.get(WorkBoardAttempt, attempt.attempt_id)
            assert retained_task.task_revision == paused.json()["task_revision"]
            assert original_attempt.ended_at is None and original_attempt.workflow_run_id == spec.identity.job_id
        assert not boundary.messages
        await boundary.http.aclose()
        return
    resumed = await asyncio.wait_for(client.post("/api/telegram/task-actions", json={"task_id": task_id, "action": "resume", "expected_revision": paused.json()["task_revision"]}), timeout=15)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["task_status"] == "review"
    final_parent = await dispatcher.jobs.get_job(spec.identity.job_id)
    assert final_parent["job_id"] == original_parent["job_id"] and final_parent["deadline_at"] == original_parent["deadline_at"]
    assert final_parent["attempt_count"] == original_parent["attempt_count"] == 1
    from src.work_board.channel_capture import ChannelAction, read_captured_terminal_output, issue_output_handle
    async with async_db() as db:
        historical = list((await db.execute(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == task_id))).scalars().all())
        assert len(historical) == 1 and historical[0].attempt_id == attempt.attempt_id and historical[0].ended_at is not None
        owner = WorkBoardOwner(principal_id=task.owner_principal_id, session_id=task.owner_session_id)
        output = await read_captured_terminal_output(db, owner, ChannelAction(task_id=task_id,
            action="inspect", expected_revision=resumed.json()["task_revision"]), adapter=adapter)
        assert output.attempt_id == attempt.attempt_id and output.parent_job_id == spec.identity.job_id
        assert output.reference["exists"] is True
        handle = issue_output_handle(owner, output)
    reviewed = await client.get("/api/telegram/output-review", params={"handle": handle})
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["reference"]["artifact_id"] == output.reference["artifact_id"]
    assert reviewed.json()["reference"]["content_sha256"] == output.reference["content_sha256"]
    assert reviewed.json()["task_id"] == task_id and reviewed.json()["attempt_id"] == attempt.attempt_id
    assert reviewed.json()["no_learning"] is True and "Original private physical source" not in reviewed.text
    Path("/home/pawel/repos/seraph/.agent-evidence/986/c7/channel-native-output-wire-r1.json").write_text(
        json.dumps(reviewed.json(), sort_keys=True, indent=2) + "\n")
    from src.db.models import TelegramTransportOutbox, Message
    from src.work_board.channel_capture import maybe_publish_channel_output
    async with async_db() as db:
        completed = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
        queued = (await db.execute(select(TelegramTransportOutbox))).scalars().one()
        assert queued.status == "queued" and queued.correlation_id.startswith("channel-output:")
        original_outbox_id, original_deadline = queued.id, queued.deadline_at
        message = await db.get(Message, queued.message_id)
        retained_output = json.loads(message.metadata_json)["channel_output.v1"]
        assert retained_output["relative_path"] == "/?channel_output=" + handle
        assert "http" not in retained_output["relative_path"]
        assert "ready for review" in message.content and "Original private physical source" not in message.content
    replayed_output = await maybe_publish_channel_output(completed, attempt.attempt_id, adapter=adapter)
    assert replayed_output["id"] == original_outbox_id
    async with async_db() as db:
        retained = (await db.execute(select(TelegramTransportOutbox))).scalars().one()
        assert retained.deadline_at == original_deadline
    boundary.lose_send = completion == "unknown"
    delivered = await client.post(f"/api/telegram/outbox/{original_outbox_id}/deliver")
    expected_status = "unknown" if completion == "unknown" else "delivered"
    assert delivered.status_code == 200 and delivered.json()["status"] == expected_status, delivered.text
    assert len(boundary.messages) == 1 and "ready for review" in boundary.messages[0]["text"]
    repeated_delivery = await client.post(f"/api/telegram/outbox/{original_outbox_id}/deliver")
    assert repeated_delivery.status_code == 200 and len(boundary.messages) == 1
    if completion == "unknown":
        replayed_unknown = await maybe_publish_channel_output(completed, attempt.attempt_id, adapter=adapter)
        assert replayed_unknown["id"] == original_outbox_id and replayed_unknown["status"] == "unknown"
        refused_retry = await client.post(f"/api/telegram/outbox/{original_outbox_id}/reconcile", json={"resolution": "retry"})
        assert refused_retry.status_code != 200 and len(boundary.messages) == 1
    inspected_outbox = await client.get(f"/api/telegram/outbox/{original_outbox_id}")
    assert inspected_outbox.status_code == 200, inspected_outbox.text
    assert inspected_outbox.json()["channel_output_review_path"] == "/?channel_output=" + handle
    invalid_handle = handle[:-1] + ("0" if handle[-1] != "0" else "1")
    invalid = await client.get("/api/telegram/output-review", params={"handle": invalid_handle})
    assert invalid.status_code == 403
    assert physical.read_text() == "Original private physical source.\n"
    final_path = Path(settings.workspace_dir) / output.reference["file_path"]
    final_path.write_bytes(b"Changed physical output")
    drifted = await client.get("/api/telegram/output-review", params={"handle": handle})
    assert drifted.status_code != 200 and "Original private physical source" not in drifted.text
    await boundary.http.aclose()
