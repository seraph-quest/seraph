"""C4.3: a project watch prepares a blocker/review response without publishing it.

The project sync materializes the selected repository's items as workspace
text; the existing GuardianSourceWatch turns a relevant change into its
normal reviewed decision packet and dossier; the exact GitHub follow-through
then prepares the comment, which waits for its own approval. A local GitHub
fixture serves only GETs, and the test asserts that no POST is ever sent.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import uuid

import httpx
import pytest
from sqlalchemy import select

from config.settings import settings
from src.db import engine
from src.db.models import ApprovalRequest, Goal, GuardianDecisionPacket, ProjectSourceLink
from src.extensions.github_followthrough import GitHubFollowthroughService
from src.goals.contracts import GoalAdmissionBudget
from src.goals.repository import serialize_admission_budget
from src.integrations.project_source import ProjectSourceRef
from tests.test_github_connection_consent import ORIGIN, active_connection
from tests.test_project_source import REPOSITORY, IssuesFixture, github_item


@pytest.mark.asyncio
async def test_project_watch_prepares_a_review_response_awaiting_approval_without_publishing(
        client, async_db, monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir(mode=0o700)
    monkeypatch.setattr(settings, "workspace_dir", str(workspace))
    owner, row, _, request = await active_connection(client, monkeypatch)
    request["expected_revision"] = row.revision
    request["consent"]["actions"] = ["github_comment_write"]
    consent = await client.put("/api/capabilities/github/connection", json=request, headers=ORIGIN)
    assert consent.status_code == 200, consent.text
    now = datetime.now(timezone.utc)
    budget = GoalAdmissionBudget(reviewed_grant=True, grant_id="project-watch-grant", max_outstanding_jobs=2,
        max_attempts=2, max_runtime_seconds=120, period_started_at=now - timedelta(minutes=1),
        period_expires_at=now + timedelta(minutes=10), timezone="UTC")
    goal = Goal(id="project-watch-goal", title="Keep the example repository unblocked", proactive_enabled=True,
        owner_principal_id=owner["principal_id"], owner_session_id=owner["session_id"],
        admission_budget_json=serialize_admission_budget(budget))
    async with engine.get_session() as db:
        db.add(goal)

    issues = IssuesFixture([github_item(3, body="Steady issue")])
    async def resolver(_host, _port):
        return ["93.184.216.34"]
    monkeypatch.setattr("src.extensions.github_followthrough.github_followthrough_service",
        GitHubFollowthroughService(resolver=resolver, transport=httpx.MockTransport(issues.handler)))
    base = "/api/capabilities/github/project-source"

    async def sync_view():
        synced = await client.post(base + "/sync", json={"connection_id": row.id, "repository_id": REPOSITORY}, headers=ORIGIN)
        assert synced.status_code == 200, synced.text
        view = await client.post(base + "/watch-view", json={"snapshot_id": synced.json()["snapshot_id"]}, headers=ORIGIN)
        assert view.status_code == 200, view.text
        return synced.json(), view.json()

    first, view = await sync_view()
    created = await client.post("/api/capabilities/source-watches", json={"goal_id": goal.id, "expected_goal_revision": 1,
        "sources": [{"source_key": "project", "kind": "workspace_text", "target": view["path"]}],
        "criteria": {"min_changed_lines": 1, "min_changed_chars": 1},
        "schedule": {"cron": "*/30 * * * *", "timezone": "UTC", "enabled": False},
        "write_mode": "approval_each_run"}, headers=ORIGIN)
    assert created.status_code == 200, created.text
    watch_id = created.json()["id"]
    baseline = await client.post(f"/api/capabilities/source-watches/{watch_id}/run", json={"expected_plan_revision": 1}, headers=ORIGIN)
    assert baseline.json()["status"] == "baseline_initialized", baseline.text

    # A review is requested on a pull request: the next sync is a relevant change.
    issues.items = [github_item(3, body="Steady issue"),
        github_item(7, pr=True, body="Please review the calculator fix.", labels=("review-requested",),
            updated="2026-10-15T23:00:00+00:00")]
    synced, _ = await sync_view()
    pending = await client.post(f"/api/capabilities/source-watches/{watch_id}/run", json={"expected_plan_revision": 1}, headers=ORIGIN)
    assert pending.json()["status"] == "awaiting_approval", pending.text
    approved = await client.post(f"/api/approvals/{pending.json()['approval_id']}/approve", headers=ORIGIN)
    assert approved.status_code == 200, approved.text
    async with engine.get_session() as db:
        packet = await db.get(GuardianDecisionPacket, pending.json()["packet_id"])
        packet_digest = hashlib.sha256((packet.proposal_text + packet.task_text).encode()).hexdigest()
    produced = await client.post(f"/api/capabilities/source-watches/{watch_id}/packets/{packet.id}/execute",
        json={"expected_packet_digest": packet_digest, "approval_id": pending.json()["approval_id"]}, headers=ORIGIN)
    assert produced.status_code == 200, produced.text
    async with engine.get_session() as db:
        packet = await db.get(GuardianDecisionPacket, packet.id)
        assert packet.status == "succeeded" and packet.verification_status == "passed"
    ref = ProjectSourceRef.model_validate(next(item["source_ref"] for item in synced["items"] if item["number"] == 7))
    assert ref.kind == "pull_request"

    async def prepare(key, *, source_ref=None, **extra):
        return await client.post(base + "/responses", json={"source_ref": (source_ref or ref).model_dump(mode="json"),
            "kind": "review_request", "packet_id": packet.id,
            "body": "Draft review: the fix looks right; please add a float test.", "idempotency_key": key, **extra},
            headers=ORIGIN)

    # The dossier is the watch packet's own: a caller cannot name one.
    forged = await prepare(str(uuid.uuid4()), dossier_artifact_id=packet.dossier_artifact_id,
        dossier_sha256=packet.dossier_sha256)
    assert forged.status_code == 422, forged.text
    # The packet observed the second snapshot's view; the baseline snapshot's
    # reference (same item absent there) or another snapshot is not bound to it.
    baseline_ref = ProjectSourceRef.model_validate(first["items"][0]["source_ref"])
    unbound = await prepare(str(uuid.uuid4()), source_ref=baseline_ref)
    assert unbound.status_code == 409 and unbound.json()["detail"]["code"] == "project_response_packet_not_bound", unbound.text

    # A changed item stops the response for a decision instead of preparing it.
    original = list(issues.items)
    issues.items = [original[0], {**original[1], "updated_at": "2026-10-16T01:00:00+00:00", "state": "closed"}]
    stopped = await prepare(str(uuid.uuid4()))
    assert stopped.status_code == 409 and stopped.json()["detail"]["code"] == "project_source_changed_decision_required"
    issues.items = original

    prepared = await prepare(str(uuid.uuid4()))
    assert prepared.status_code == 200, prepared.text
    view = prepared.json()
    assert view["status"] == "awaiting_approval", view
    async with engine.get_session() as db:
        approval = await db.get(ApprovalRequest, view["approval_id"])
        assert approval.status == "pending"
        link = (await db.execute(select(ProjectSourceLink).where(
            ProjectSourceLink.link_kind == "github_followthrough"))).scalars().one()
        assert (link.target_id, link.item_number, link.item_digest, link.finding_kind) == (
            view["job_id"], 7, ref.digest, "review_request")
    # Only reads ever reached GitHub: the response is prepared, not published.
    assert issues.calls and all("/issues" in call for call in issues.calls)
