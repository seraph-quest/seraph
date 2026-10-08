"""Combined actual authenticated local snapshot and explicit recovery proof."""
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from config.settings import settings
from src.auth.service import authenticate_session, authenticate_token, AuthFailure
from src.db.models import (Goal, OperatorSession, OperatorRecoveryJournal, WorkBoardTask,
    WorkBoardAttempt, WorkBoardProposal, WorkBoardEvent, WorkflowRunState,
    InferenceCostReservation, ApprovalRequest)
from tests.test_first_result_setup import async_db, setup_workspace, create_snapshot, run_task, progress
from tests.test_operator_identity import auth, login, enroll, recover, HEADERS


@pytest.mark.asyncio
@pytest.mark.parametrize("reentry", ["logout", "idle_expires_at", "absolute_expires_at"])
async def test_actual_identity_starter_snapshot_recovery_and_scoped_evidence(client, async_db, setup_workspace, monkeypatch, reentry):
    from src.api import task_evidence
    monkeypatch.setattr(task_evidence, "get_session", async_db)
    from src.work_board import triage
    transport = AsyncMock(side_effect=AssertionError("Recovered source reached model transport"))
    monkeypatch.setattr(triage, "completion_with_fallback", transport)
    client.headers.update(HEADERS)
    first = await login(client)
    enrollment = await enroll(client)
    originals = []
    for label in ("project-a", "project-b"):
        goal, task = await create_snapshot(client, journey=label)
        task = await run_task(client, task, async_db)
        saved = await client.put("/api/user/onboarding/progress", json=progress(
            journey_id=label, step="task_saved", goal_id=goal["id"], goal_revision=goal["revision"], task_id=task["task_id"]))
        assert saved.status_code == 200, saved.text
        opened = await client.post(f"/api/user/onboarding/result/{task['task_id']}/open")
        assert opened.status_code == 200, opened.text
        old_packet = await client.post(f"/api/work-board/tasks/{task['task_id']}/evidence", json={
            "expected_task_revision": task["task_revision"], "expected_packet_revision": 0, "query": ""})
        assert old_packet.status_code == 200, old_packet.text
        async with async_db() as db:
            child = (await db.execute(select(WorkflowRunState).where(
                WorkflowRunState.run_identity.like(f"goal-snapshot-work-board:{task['task_id']}:%")))).scalar_one()
            artifact = json.loads(child.artifact_receipts_json)[0]
        originals.append((goal, task, artifact, old_packet.json()))
    if reentry == "logout":
        assert (await client.post("/api/auth/logout")).status_code == 204
    else:
        async with async_db() as db:
            root = await db.get(OperatorSession, first["session_id"])
            setattr(root, reentry, datetime.now(timezone.utc) - timedelta(seconds=1))
        assert (await client.get("/api/auth/session")).status_code == 401
    with pytest.raises(AuthFailure):
        await authenticate_session(first["session_id"])
    current = await login(client)
    assert current["session_id"] != first["session_id"]
    assert current["principal_id"] != first["principal_id"]
    assert current["operator_identity_id"] == enrollment["operator_identity_id"]
    selections = [{"kind": kind, "record_id": rid} for goal, task, artifact, _ in originals
        for kind, rid in (("goal", goal["id"]), ("task", task["task_id"]), ("output_artifact", artifact["artifact_id"]))]
    journal, _ = await recover(client, selections, key=f"real-snapshot-{reentry}")
    async with async_db() as db:
        stored = await db.get(OperatorRecoveryJournal, journal["journal_id"])
        assert len(json.loads(stored.selections_json)) == 6
        assert stored.current_session_id == current["session_id"]
    fresh = await client.post(f"/api/auth/ownership/recovery/{journal['journal_id']}/fresh-work")
    assert fresh.status_code == 200, fresh.text
    fresh_work = fresh.json()["fresh_work"]
    fresh_by_source = {item["source_task_id"]: item["task_id"] for item in fresh_work["tasks"]}
    operator = await authenticate_token(client.cookies.get(settings.operator_auth_cookie_name))
    from src.auth.ownership import fresh_work_source_scope
    # Both projects really are selected in the same durable journal. Selection
    # of B must not become source permission for A's new current intent.
    async with async_db() as db:
        assert await fresh_work_source_scope(operator, fresh_by_source[originals[0][1]["task_id"]],
            "output_artifact", originals[1][2]["artifact_id"], db=db) is None
        assert await fresh_work_source_scope(operator, fresh_by_source[originals[0][1]["task_id"]],
            "goal", originals[1][0]["id"], db=db) is None
    for goal, task, artifact, old_packet in originals:
        historical = await client.get(f"/api/work-board/tasks/{task['task_id']}/evidence")
        assert historical.status_code == 200, historical.text
        assert historical.json()["ownership_access"] == "recovered_read_only"
        assert (await client.post(f"/api/work-board/tasks/{task['task_id']}/evidence/adoption", json={
            "expected_task_revision": task["task_revision"], "expected_packet_revision": old_packet["revision"],
            "expected_packet_digest": old_packet["digest"], "allow_model_context": True})).status_code in {403, 404}
        assert (await client.post(f"/api/work-board/tasks/{task['task_id']}/evidence", json={
            "expected_task_revision": task["task_revision"], "expected_packet_revision": 1})).status_code in {403, 404}
        new_id = fresh_by_source[task["task_id"]]
        detail = (await client.get(f"/api/work-board/tasks/{new_id}")).json()["task"]
        assert detail["owner_session_id"] == current["session_id"]
        assert detail["status"] == "triage" and detail["capability_id"] is None
        assert detail["input_artifact_id"] is None and detail["typed_input_ref"] is None
        packet = await client.post(f"/api/work-board/tasks/{new_id}/evidence", json={
            "expected_task_revision": detail["task_revision"], "expected_packet_revision": 0, "query": ""})
        assert packet.status_code == 200, packet.text
        cited = packet.json()
        assert cited["claims"], cited
        assert {c["source_digest"] for c in cited["claims"]} == {artifact["content_sha256"]}
        assert all(c["owner_session_id"] == first["session_id"] and not c["model_context_allowed"] for c in cited["claims"])
        assert cited["allow_model_context"] is False
        unseen = await client.post(f"/api/work-board/tasks/{new_id}/evidence/adoption", json={
            "expected_task_revision": detail["task_revision"], "expected_packet_revision": cited["revision"],
            "expected_packet_digest": "0" * 64, "allow_model_context": True})
        assert unseen.status_code == 409
        adopted = await client.post(f"/api/work-board/tasks/{new_id}/evidence/adoption", json={
            "expected_task_revision": detail["task_revision"], "expected_packet_revision": cited["revision"],
            "expected_packet_digest": cited["digest"], "allow_model_context": True})
        assert adopted.status_code == 200, adopted.text
        # Packet review grants neither recovered-source model purpose nor a
        # proposal/job/approval. The source-purpose guard runs before admission.
        effects = (WorkBoardProposal, WorkflowRunState, WorkBoardAttempt,
                   InferenceCostReservation, ApprovalRequest, WorkBoardEvent)
        async with async_db() as db:
            before_effects = {model.__name__: sorted(row.model_dump_json()
                for row in (await db.execute(select(model))).scalars()) for model in effects}
            before_task = (await db.execute(select(WorkBoardTask).where(
                WorkBoardTask.task_id == new_id))).scalar_one().model_dump_json()
            before_goal = (await db.get(Goal, detail["goal_id"])).model_dump_json()
        specified = await client.post(f"/api/work-board/tasks/{new_id}/specify", json={
            "expected_revision": detail["task_revision"], "idempotency_key": f"historical-egress-{new_id}"})
        assert specified.status_code == 403, specified.text
        assert specified.json()["detail"]["code"] == "evidence_source_purpose_consent_required"
        transport.assert_not_awaited()
        async with async_db() as db:
            after_effects = {model.__name__: sorted(row.model_dump_json()
                for row in (await db.execute(select(model))).scalars()) for model in effects}
            assert after_effects == before_effects
            assert (await db.execute(select(WorkBoardTask).where(
                WorkBoardTask.task_id == new_id))).scalar_one().model_dump_json() == before_task
            assert (await db.get(Goal, detail["goal_id"])).model_dump_json() == before_goal
            source_goal = await db.get(Goal, goal["id"])
            new_goal = await db.get(Goal, detail["goal_id"])
            assert source_goal.owner_session_id == first["session_id"]
            assert new_goal.admission_budget_json is None and new_goal.proactive_enabled is False
            assert new_goal.success_criterion_json is None
            assert (await db.execute(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == new_id))).first() is None
        still_readable = await client.get(f"/api/work-board/tasks/{new_id}/evidence")
        assert still_readable.status_code == 200, still_readable.text
        retained = still_readable.json()
        assert (retained["revision"], retained["digest"]) == (cited["revision"], cited["digest"])
        assert retained["allow_model_context"] is True  # exact packet adoption only
        assert retained["claims"] == adopted.json()["claims"]
        assert all(not claim["model_context_allowed"] for claim in retained["claims"])
    transport.assert_not_awaited()
    # Fresh intent prevents rolling back old selections behind the operator's
    # current work, without resurrecting historical dispatch authority.
    rollback = await client.post(f"/api/auth/ownership/recovery/{journal['journal_id']}/rollback")
    assert rollback.status_code == 409
    a_goal, a_task, a_artifact, _ = originals[0]
    fresh_a = fresh_by_source[a_task["task_id"]]
    (setup_workspace / a_artifact["file_path"]).write_text("Changed unreviewed historical snapshot")
    changed = await client.get(f"/api/work-board/tasks/{fresh_a}/evidence")
    assert changed.status_code == 200, changed.text
    assert changed.json()["claims"] == [] and changed.json()["invalidated_count"] > 0
    assert changed.json()["allow_model_context"] is False
    b_goal, b_task, b_artifact, _ = originals[1]
    fresh_b = fresh_by_source[b_task["task_id"]]
    (setup_workspace / b_artifact["file_path"]).unlink()
    deleted = await client.get(f"/api/work-board/tasks/{fresh_b}/evidence")
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["claims"] == [] and deleted.json()["invalidated_count"] > 0
    assert (await client.post("/api/auth/ownership/revoke")).status_code == 204
    assert (await client.get(f"/api/work-board/tasks/{fresh_a}/evidence")).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("fence", ["rollback", "modified", "deleted", "identity_revoke", "device_forget",
    "source_running", "source_review", "unknown_effect", "missing_child", "forged_child"])
async def test_actual_selected_snapshot_recovery_fences(client, async_db, setup_workspace, monkeypatch, fence):
    from src.api import task_evidence
    monkeypatch.setattr(task_evidence, "get_session", async_db)
    client.headers.update(HEADERS)
    first = await login(client)
    await enroll(client)
    goal, task = await create_snapshot(client, journey="fence-snapshot")
    task = await run_task(client, task, async_db)
    endpoint = f"/api/work-board/tasks/{task['task_id']}/evidence"
    packet = await client.post(endpoint, json={"expected_task_revision": task["task_revision"],
        "expected_packet_revision": 0, "query": ""})
    assert packet.status_code == 200 and packet.json()["claims"], packet.text
    async with async_db() as db:
        child = (await db.execute(select(WorkflowRunState).where(
            WorkflowRunState.run_identity.like(f"goal-snapshot-work-board:{task['task_id']}:%")))).scalar_one()
        artifact = json.loads(child.artifact_receipts_json)[0]
    await client.post("/api/auth/logout")
    await login(client)
    selections = [{"kind": kind, "record_id": rid} for kind, rid in (
        ("goal", goal["id"]), ("task", task["task_id"]), ("output_artifact", artifact["artifact_id"]))]
    journal, _ = await recover(client, selections)
    recovered = await client.get(endpoint)
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["claims"]
    assert recovered.json()["ownership_access"] == "recovered_read_only"
    # Canonical mutation/dispatch controls cannot consume recovered read scopes.
    forbidden = await client.post(f"/api/work-board/tasks/{task['task_id']}/actions", json={
        "action": "retry", "expected_revision": task["task_revision"]})
    assert forbidden.status_code in {403, 404}, forbidden.text
    if fence in {"source_running", "source_review", "unknown_effect", "missing_child", "forged_child"}:
        current = (await client.get("/api/auth/session")).json()
        async with async_db() as db:
            source = (await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == task["task_id"]))).scalar_one()
            run = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == child.run_identity))).scalar_one()
            if fence == "source_running": source.status = "running"
            elif fence == "source_review": source.status = "review"
            elif fence == "unknown_effect": source.block_kind = "unknown_effect"
            elif fence == "missing_child": await db.delete(run)
            else: run.owner_principal_id = "service:forged"
        denied = await client.post(f"/api/auth/ownership/recovery/{journal['journal_id']}/fresh-work")
        assert denied.status_code in {403, 404, 409}, denied.text
        async with async_db() as db:
            assert (await db.execute(select(WorkBoardTask).where(
                WorkBoardTask.owner_session_id == current["session_id"]))).first() is None
            stored = await db.get(OperatorRecoveryJournal, journal["journal_id"])
            assert stored.fresh_work_json is None
    elif fence == "rollback":
        assert (await client.post(f"/api/auth/ownership/recovery/{journal['journal_id']}/rollback")).status_code == 200
        result = await client.get(endpoint)
        assert result.status_code in {403, 404}, result.text
    elif fence in {"modified", "deleted"}:
        path = setup_workspace / artifact["file_path"]
        if fence == "modified": path.write_text("unreviewed changed output")
        else: path.unlink()
        result = await client.get(endpoint)
        assert result.status_code == 200, result.text
        assert result.json()["claims"] == [] and result.json()["invalidated_count"] > 0
        invalid_selection = await client.post("/api/auth/ownership/recovery/preview", json={"selections": selections})
        assert invalid_selection.status_code in {403, 404}, invalid_selection.text
    elif fence == "identity_revoke":
        assert (await client.post("/api/auth/ownership/revoke")).status_code == 204
        assert (await client.get(endpoint)).status_code == 401
    else:
        from src.api.auth import _continuity_cookie_name
        revoked_proof = client.cookies.get(_continuity_cookie_name())
        assert (await client.post("/api/auth/ownership/forget-device")).status_code == 204
        # Forgetting this browser proof blocks continuity login, while current
        # authenticated root data reads retain their finite existing authority.
        await client.post("/api/auth/logout")
        failed_login = await client.post("/api/auth/login", json={"password": "identity-only-test-password"},
            headers={**HEADERS, "cookie": f"{_continuity_cookie_name()}={revoked_proof}"})
        assert failed_login.status_code == 401, failed_login.text
        assert (await client.get(endpoint)).status_code == 401
