"""Real deterministic source producer -> finite consent -> governed GitHub effect."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import uuid

import httpx
import pytest
from sqlalchemy import update

from config.settings import settings
from src.db import engine
from src.db.models import Goal, GuardianDecisionPacket
from src.goals.contracts import GoalAdmissionBudget
from src.goals.repository import serialize_admission_budget
from src.extensions.github_followthrough import GitHubFollowthroughService
from tests.test_github_connection_consent import active_connection, ORIGIN


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
@pytest.mark.parametrize("action,stale_goal", [("create_issue", False), ("create_comment", True)])
async def test_actual_source_producer_finite_consent_unknown_readback(client, async_db, monkeypatch, tmp_path, action, stale_goal):
    workspace = tmp_path / "workspace"; workspace.mkdir()
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    owner, row, _, request = await active_connection(client, monkeypatch)
    request["expected_revision"] = row.revision
    request["consent"]["actions"] = ["github_issue_write", "github_comment_write"]
    consent = await client.put("/api/capabilities/github/connection", json=request, headers=ORIGIN)
    assert consent.status_code == 200, consent.text
    now = datetime.now(timezone.utc)
    budget = GoalAdmissionBudget(reviewed_grant=True, grant_id="source-proof-grant", max_outstanding_jobs=2, max_attempts=2, max_runtime_seconds=120,
        period_started_at=now-timedelta(minutes=1), period_expires_at=now+timedelta(minutes=10), timezone="UTC")
    goal = Goal(id="source-publication-goal", title="Monitor local deadlines", proactive_enabled=True,
        owner_principal_id=owner["principal_id"], owner_session_id=owner["session_id"], admission_budget_json=serialize_admission_budget(budget))
    async with engine.get_session() as db: db.add(goal)
    source = workspace / "notes.txt"; source.write_text("Old deadline Friday\n")
    created = await client.post("/api/capabilities/source-watches", json={"goal_id": goal.id, "expected_goal_revision": 1,
        "sources": [{"source_key": "local", "kind": "workspace_text", "target": "notes.txt"}],
        "criteria": {"min_changed_lines": 1, "min_changed_chars": 1}, "schedule": {"cron": "*/30 * * * *", "timezone": "UTC", "enabled": False}, "write_mode": "approval_each_run"}, headers=ORIGIN)
    assert created.status_code == 200, created.text
    watch_id = created.json()["id"]
    initial = await client.post(f"/api/capabilities/source-watches/{watch_id}/run", json={"expected_plan_revision": 1}, headers=ORIGIN)
    assert initial.json()["status"] == "baseline_initialized", initial.text
    source.write_text("New deadline Wednesday\n")
    pending = await client.post(f"/api/capabilities/source-watches/{watch_id}/run", json={"expected_plan_revision": 1}, headers=ORIGIN)
    assert pending.json()["status"] == "awaiting_approval", pending.text
    source_approval = pending.json()["approval_id"]
    approved = await client.post(f"/api/approvals/{source_approval}/approve", headers=ORIGIN)
    assert approved.status_code == 200, approved.text
    async with engine.get_session() as db:
        packet = await db.get(GuardianDecisionPacket, pending.json()["packet_id"])
        packet_digest = hashlib.sha256((packet.proposal_text+packet.task_text).encode()).hexdigest()
    produced = await client.post(f"/api/capabilities/source-watches/{watch_id}/packets/{packet.id}/execute", json={"expected_packet_digest": packet_digest, "approval_id": source_approval}, headers=ORIGIN)
    assert produced.status_code == 200, produced.text
    async with engine.get_session() as db:
        packet = await db.get(GuardianDecisionPacket, packet.id)
        assert packet.status == "succeeded" and packet.verification_status == "passed", produced.text
        dossier_id, dossier_sha = packet.dossier_artifact_id, packet.dossier_sha256
    calls, remote = [], {}
    async def resolver(*_args): return ["93.184.216.34"]
    def transport(request):
        calls.append(request.method)
        if request.method == "POST":
            body = json.loads(request.content)
            remote.update(body, **({"number": 7} if action == "create_issue" else {"id": 9, "issue_url": "https://api.github.com/repos/acme/example/issues/7"}))
            raise httpx.ReadTimeout("accepted remote object, response lost", request=request)
        assert request.method == "GET"
        return httpx.Response(200, json=remote)
    service = GitHubFollowthroughService(resolver=resolver, transport=httpx.MockTransport(transport))
    monkeypatch.setattr("src.extensions.github_followthrough.github_followthrough_service", service)
    req = {"conversation_id": owner["session_id"], "goal_id": goal.id, "goal_revision": 1, "dossier_artifact_id": dossier_id, "dossier_sha256": dossier_sha,
        "connection_revision": consent.json()["revision"], "action": action, "body": "Approved exact observed deadline", "idempotency_key": str(uuid.uuid4())}
    req.update({"title": "Observed deadline"} if action == "create_issue" else {"issue_number": 7})
    prepared = await client.post("/api/capabilities/github/prepare", json=req, headers=ORIGIN)
    assert prepared.status_code == 200, prepared.text
    view = prepared.json(); job_id = view["job_id"]
    approved = await client.post(f"/api/approvals/{view['approval_id']}/approve", headers=ORIGIN)
    assert approved.status_code == 200, approved.text
    executed = await client.post(f"/api/capabilities/github/jobs/{job_id}/execute", headers=ORIGIN)
    assert executed.status_code == 200 and executed.json()["status"] == "unknown_external_effect", executed.text
    assert calls == ["POST"]
    stopped = await client.post("/api/capabilities/github/connection/revoke", json={"expected_revision": consent.json()["revision"]}, headers=ORIGIN)
    assert stopped.status_code == 200, stopped.text
    if stale_goal:
        async with engine.get_session() as db: await db.execute(update(Goal).where(Goal.id == goal.id).values(revision=2))
    recovered = await client.post(f"/api/capabilities/github/jobs/{job_id}/reconcile", json={"remote_id": 7 if action == "create_issue" else 9,
        "acknowledged_readback": True, "expected_connection_revision": stopped.json()["revision"]}, headers=ORIGIN)
    assert recovered.status_code == 200, recovered.text
    assert calls == ["POST", "GET"]
    result = recovered.json()
    if stale_goal:
        assert result["status"] == "unknown_external_effect" and result["observation_only"] is True and result["reason_code"] == "github_current_goal_changed", result
    else:
        assert result["status"] == "succeeded", result
    proof = tmp_path / f"actual-{action}-finite-consent-recovery.json"
    proof.write_text(json.dumps({"result": result, "source_producer": produced.json(), "methods": calls}, sort_keys=True))
    print("ACTUAL_FINITE_GITHUB_RECOVERY=" + str(proof))
