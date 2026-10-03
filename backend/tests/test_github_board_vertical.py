"""Actual source producer, private typed input, Board admission and recovery.

Only the external GitHub HTTP transport is intercepted. No job, authority,
approval, verification or Board projection is supplied by a receipt fixture.
"""
import hashlib
import json
import uuid

import httpx
import pytest
from sqlalchemy import select, update

from src.db.models import Goal, WorkBoardAttempt, WorkflowRunState
from src.extensions.github_followthrough import GitHubFollowthroughService
from src.work_board.dispatcher import WorkBoardDispatcher
from src.workflows.job_runtime import durable_job_repository as jobs
from tests.test_github_connection_consent import ORIGIN
from tests.test_github_finite_consent_vertical import actual_source_dossier


def test_fixed_github_selector_uses_semantic_effect_after_multiple_observations():
    semantic = {"effect_type": "github_publication", "receipt_kind": "readback",
        "status": "succeeded", "content_sha256": "a" * 64,
        "readback_id": "github-readback:canonical", "verified_at": "2026-10-03T00:00:00Z",
        "details": {"verified": True}}
    observations = [{**semantic, "content_sha256": value * 64,
        "details": {"observation_only": True, "original_effect_id": "original"}}
        for value in ("b", "c")]
    projection = {"run_identity": "canonical", "job_kind": "github_followthrough_v1",
        "effects": [*observations, semantic]}
    assert WorkBoardDispatcher._workflow_readback(projection, "canonical")["content_sha256"] == "a" * 64
    assert WorkBoardDispatcher._workflow_readback({**projection, "effects": observations}, "canonical") is None
    assert WorkBoardDispatcher._workflow_readback(projection, "wrong-root") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.parametrize("stale_goal,read_fault", [(False, None), (False, "wrong-original"), (False, "missing-protected"), (True, None)])
async def test_actual_board_source_unknown_restart_readback_same_attempt(
    client, async_db, monkeypatch, tmp_path, stale_goal, read_fault
):
    owner, consent, goal, dossier_id, dossier_sha, producer = await actual_source_dossier(
        client, monkeypatch, tmp_path
    )
    inputs = {"dossier_artifact_id": dossier_id, "dossier_sha256": dossier_sha,
        "connection_revision": consent["revision"], "action": "create_issue",
        "title": "Actual observed deadline", "body": "Approved source dossier update"}
    # This fixed capability deliberately uses the existing private workspace
    # input route; its public artifact endpoint remains privacy-blocked.
    input_path = tmp_path / "workspace" / "github-input.json"
    raw = json.dumps({"schema_version": 1, "capability_id": "work.github-followthrough.v1", "input": inputs}, sort_keys=True).encode()
    input_path.write_bytes(raw)
    created = await client.post("/api/work-board/tasks", json={
        "title": "Publish verified source change", "goal_id": goal.id,
        "goal_revision": 1, "status": "todo",
        "capability_id": "work.github-followthrough.v1",
        "typed_input_ref": "workspace-json:github-input.json",
        "typed_input_digest": hashlib.sha256(raw).hexdigest(),
        "idempotency_key": str(uuid.uuid4())}, headers=ORIGIN)
    assert created.status_code == 200, created.text
    task_id = created.json()["task"]["task_id"]
    calls, remote = [], {}
    async def resolver(*_args):
        return ["93.184.216.34"]
    def transport(request):
        calls.append(request.method)
        if request.method == "POST":
            remote.update(json.loads(request.content), number=7)
            raise httpx.ReadTimeout("accepted object, response lost", request=request)
        assert request.method == "GET"
        return httpx.Response(200, json=remote)
    service = GitHubFollowthroughService(resolver=resolver, transport=httpx.MockTransport(transport))
    # Dispatcher constructs the capability's existing service; only its
    # transport dependencies are selected here, with every governed method real.
    monkeypatch.setattr("src.extensions.github_followthrough.GitHubFollowthroughService", lambda: service)
    monkeypatch.setattr("src.extensions.github_followthrough.github_followthrough_service", service)
    dispatcher = WorkBoardDispatcher()
    admitted = await dispatcher.run_pass()
    detail = (await client.get(f"/api/work-board/tasks/{task_id}")).json()
    assert admitted["admitted"] == 1, (admitted, detail)
    async with async_db() as db:
        attempt = (await db.execute(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == task_id))).scalar_one()
        attempt_id, job_id = attempt.attempt_id, attempt.workflow_run_id
    original = await jobs.get_job(job_id)
    approval_id = original["declared_authority"]["approval_id"]
    assert calls == []
    approved = await client.post(f"/api/approvals/{approval_id}/approve", headers=ORIGIN)
    assert approved.status_code == 200, approved.text
    await dispatcher.run_pass()
    unknown = await jobs.get_job(job_id)
    assert unknown["status"] == "unknown_external_effect" and calls == ["POST"], unknown
    blocked = (await client.get(f"/api/work-board/tasks/{task_id}")).json()
    assert blocked["task"]["status"] == "blocked", blocked
    stopped = await client.post("/api/capabilities/github/connection/revoke",
        json={"expected_revision": consent["revision"]}, headers=ORIGIN)
    assert stopped.status_code == 200, stopped.text
    async with async_db() as db:
        physical_engine = db.bind
        if stale_goal:
            await db.execute(update(Goal).where(Goal.id == goal.id).values(revision=2))
    await physical_engine.dispose()
    # Restore the actual constructor to create a fresh adapter after reopening.
    fresh_service = type(service)(resolver=resolver, transport=httpx.MockTransport(transport))
    monkeypatch.setattr("src.extensions.github_followthrough.GitHubFollowthroughService", lambda: fresh_service)
    monkeypatch.setattr("src.extensions.github_followthrough.github_followthrough_service", fresh_service)
    recovered = await client.post(f"/api/capabilities/github/jobs/{job_id}/reconcile",
        json={"remote_id": 7, "acknowledged_readback": True,
            "expected_connection_revision": stopped.json()["revision"]}, headers=ORIGIN)
    assert recovered.status_code == 200, recovered.text
    assert calls == ["POST", "GET"]
    if read_fault:
        # Mutate actual canonical rows to prove candidate projections cannot
        # supply missing protected authority or replace the sealed effect.
        async with async_db() as db:
            protected_root = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id))).scalar_one()
            if read_fault == "missing-protected":
                protected_root.github_read_revision_json = None
            else:
                ledger = json.loads(protected_root.effect_receipts_json)
                for effect in ledger:
                    if effect.get("details", {}).get("verified") is True:
                        effect["content_sha256"] = "0" * 64
                protected_root.effect_receipts_json = json.dumps(ledger)
    await WorkBoardDispatcher().reconcile_linked_attempts()
    final = (await client.get(f"/api/work-board/tasks/{task_id}")).json()
    async with async_db() as db:
        attempts = (await db.execute(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == task_id))).scalars().all()
        assert len(attempts) == 1 and attempts[0].attempt_id == attempt_id
        root = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id))).scalar_one()
        read_revision = json.loads(root.github_read_revision_json or "null")
    if read_fault:
        assert final["task"]["status"] == "blocked", final
        assert calls == ["POST", "GET"]
    elif stale_goal:
        assert recovered.json()["observation_only"] is True
        assert final["task"]["status"] == "blocked", final
        connection = await fresh_service._get_connection_row(owner["principal_id"])
        current = await jobs.get_job(job_id)
        body = {"acknowledged_capacity_close": True, "expected_job_revision": current["revision"],
            "expected_connection_revision": connection.revision,
            "expected_connection_fence": connection.active_fence,
            "idempotency_key": str(uuid.uuid4()), "remote_id": 7}
        closed = await client.post(f"/api/capabilities/github/jobs/{job_id}/close-capacity", json=body, headers=ORIGIN)
        assert closed.status_code == 200, closed.text
        assert closed.json()["status"] == "unknown_external_effect"
        await WorkBoardDispatcher().reconcile_linked_attempts()
        assert (await client.get(f"/api/work-board/tasks/{task_id}")).json()["task"]["status"] == "blocked"
        assert (await fresh_service._get_connection_row(owner["principal_id"])).active_job_id is None
    else:
        assert recovered.json()["status"] == "succeeded", recovered.text
        assert final["task"]["status"] == "done", final
        assert read_revision["finalized_job_revision"] == root.revision
        assert (await fresh_service._get_connection_row(owner["principal_id"])).active_job_id is None
    proof = tmp_path / "actual-board-source-recovery.json"
    proof.write_text(json.dumps({"producer": producer, "task_id": task_id, "attempt_id": attempt_id,
        "job_id": job_id, "admitted": admitted, "recovery": recovered.json(),
        "canonical_negative": read_fault, "final_board": final, "protected_read_revision": read_revision, "methods": calls,
        "boundary": "external GitHub HTTP only; real file SQLite pool reopen"}, sort_keys=True))
    print("ACTUAL_BOARD_SOURCE_RECOVERY=" + str(proof))
