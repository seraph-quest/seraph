"""Actual authenticated attention/recovery APIs, durable tasks and intercepted effects.

Only the external transports and the test operator's server-side external grant
are substituted. Capability execution, approvals, storage and readback are real.
"""
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import asyncio
import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

from config.settings import settings
from tests.conftest import _PATCH_TARGETS
from tests.test_first_result_setup import authenticated_setup_operator, setup_workspace, progress, run_task
from src.auth import service as auth_service
from src.approval.repository import approval_repository
from src.db.models import ApprovalRequest, GuardianDecisionPacket, WorkBoardAttempt
from src.extensions.github_followthrough import GitHubFollowthroughService
from src.security.trust_contract import AuthorityGrant
from src.vault.repository import vault_repository
from src.work_board.dispatcher import WorkBoardDispatcher
from src.workflows.job_runtime import durable_job_repository


@pytest_asyncio.fixture
async def async_db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'attention.sqlite'}", connect_args={"timeout": 20})
    from src.db.engine import _configure_sqlite_connection
    event.listen(engine.sync_engine, "connect", _configure_sqlite_connection)
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    @asynccontextmanager
    async def get_session():
        async with factory() as db:
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise
    patches = [patch(target, get_session) for target in _PATCH_TARGETS]
    for target in patches: target.start()
    get_session.engine = engine
    try: yield get_session
    finally:
        for target in reversed(patches): target.stop()
        await engine.dispose()


@pytest.fixture(autouse=True)
def explicit_test_external_permission(monkeypatch):
    # The baseline operator policy has no external-mutation grant. This test
    # supplies that explicit server-side permission; browser requests cannot
    # create it, and actual auth-root/session expiry/ownership remain enforced.
    original = auth_service._principal
    def granted(session_id, principal_id):
        principal = original(session_id, principal_id)
        return replace(principal, grants=(*principal.grants, AuthorityGrant.EXTERNAL_MUTATION))
    monkeypatch.setattr(auth_service, "_principal", granted)


@pytest.fixture(autouse=True)
def forbid_unintercepted_http(monkeypatch):
    async def deny_async_transport(*args, **kwargs):
        raise AssertionError("Unintercepted outbound HTTP is forbidden in the attention proof")
    def deny_sync_transport(*args, **kwargs):
        raise AssertionError("Unintercepted outbound HTTP is forbidden in the attention proof")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", deny_async_transport)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", deny_sync_transport)


async def _prepare_actual_github_task(client, async_db, setup_workspace, monkeypatch):
    source = ["Baseline release\n" * 8]
    async def public_transport(url):
        assert url == "https://example.org/attention.txt"
        return SimpleNamespace(status_code=200, headers={"content-type": "text/plain"}, content=source[0].encode())
    monkeypatch.setattr("src.guardian.source_watch.fetch_pinned_https", public_transport)
    prepared = await client.post("/api/user/onboarding/starter", json=progress(starter="public_watch", journey_id="attention", title="Verified attention journey", source="https://example.org/attention.txt"))
    assert prepared.status_code == 200, prepared.text
    goal, original_watch = prepared.json()["goal"], prepared.json()["watch"]
    paused = await client.patch(f"/api/capabilities/source-watches/{original_watch['id']}", json={"expected_plan_revision": original_watch["plan_revision"], "state": "paused"})
    assert paused.status_code == 200, paused.text
    reviewed = await client.post("/api/capabilities/source-watches", json={"goal_id": goal["id"], "expected_goal_revision": goal["revision"], "sources": [{"source_key": "primary", "kind": "public_https_text", "target": "https://example.org/attention.txt"}], "schedule": {"enabled": False, "cron": "0 * * * *", "timezone": "UTC"}, "write_mode": "standing_reviewed", "reviewed_grant_id": goal["admission_budget"]["grant_id"], "criteria": {"min_changed_lines": 1, "min_changed_chars": 1}})
    assert reviewed.status_code == 200, reviewed.text
    watch = reviewed.json()
    for sequence in (0, 1):
        if sequence: source[0] = "Material release version two with actionable verified changes\n" * 8
        reserved = await client.post("/api/work-board/input-artifacts", json={"schema_version": 1, "capability_id": "guardian.research-watch.v1", "goal_id": goal["id"], "goal_revision": goal["revision"], "input": {"watch_id": watch["id"], "expected_plan_revision": watch["plan_revision"]}, "idempotency_key": f"attention-source-input-{sequence}"})
        assert reserved.status_code == 200, reserved.text
        created = await client.post("/api/work-board/tasks", json={"title": "Observe reviewed source", "goal_id": goal["id"], "goal_revision": goal["revision"], "status": "todo", "capability_id": "guardian.research-watch.v1", "input_artifact_id": reserved.json()["artifact_id"], "priority": 80, "idempotency_key": f"attention-source-{sequence}"})
        assert created.status_code == 200, created.text
        source_task = await run_task(client, created.json()["task"], async_db)
    async with async_db() as db:
        packet = (await db.execute(select(GuardianDecisionPacket).where(GuardianDecisionPacket.run_identity == source_task["latest_attempt"]["workflow_run_id"]))).scalar_one()
    assert packet.dossier_artifact_id and packet.dossier_sha256
    auth = (await client.get("/api/auth/session")).json()
    await vault_repository.store("attention-github", "intercepted-only-not-a-real-token", owner_principal_id=auth["principal_id"])
    connection = await client.put("/api/capabilities/github/connection", json={"repository": "example/repo", "vault_key": "attention-github", "mode": "active", "expected_revision": 0})
    assert connection.status_code == 200, connection.text
    typed = {"schema_version": 1, "capability_id": "work.github-followthrough.v1", "input": {"dossier_artifact_id": packet.dossier_artifact_id, "dossier_sha256": packet.dossier_sha256, "connection_revision": connection.json()["revision"], "action": "create_issue", "title": "Attention verified follow-through", "body": "A bounded source-backed follow-through."}}
    content = json.dumps(typed, sort_keys=True, separators=(",", ":"))
    path = setup_workspace / "artifacts/work-board/attention-github.json"
    path.parent.mkdir(parents=True, exist_ok=True); path.write_text(content)
    created = await client.post("/api/work-board/tasks", json={"title": "Approve and reconcile follow-through", "goal_id": goal["id"], "goal_revision": goal["revision"], "status": "todo", "capability_id": typed["capability_id"], "typed_input_ref": "workspace-json:artifacts/work-board/attention-github.json", "typed_input_digest": hashlib.sha256(content.encode()).hexdigest(), "priority": 80, "idempotency_key": "attention-github"})
    assert created.status_code == 200, created.text
    dispatcher = WorkBoardDispatcher(session_provider=async_db)
    await dispatcher.run_pass()
    await dispatcher.run_pass()  # Canonical admission then pending-approval projection.
    task = (await client.get(f"/api/work-board/tasks/{created.json()['task']['task_id']}")).json()["task"]
    assert task["status"] in {"running", "blocked"}, task
    assert task["recovery_action"] in {"cancel", "approve_existing_run"}, task
    job = await durable_job_repository.get_job(task["latest_attempt"]["workflow_run_id"])
    assert job and job["status"] == "awaiting_approval"
    return dispatcher, task, job, auth


@pytest.mark.asyncio
async def test_actual_approval_unknown_readback_restart_and_owner_denial(client, async_db, setup_workspace, monkeypatch):
    requests = []
    remote = {}
    fail_readback = [True]
    async def github_transport(request):
        requests.append(request.method)
        if request.method == "POST":
            assert requests.count("POST") == 1, "Unknown effects must never be resent"
            posted = json.loads(request.content)
            remote.update(number=481, title=posted["title"], body=posted["body"], html_url="https://github.com/example/repo/issues/481")
            return httpx.Response(201, json=remote, request=request)
        assert request.method == "GET" and request.url.path == "/repos/example/repo/issues/481"
        return httpx.Response(503 if fail_readback[0] else 200, json={} if fail_readback[0] else remote, request=request)
    async def resolver(host, port):
        assert host == "api.github.com" and port == 443
        return ["93.184.216.34"]
    original_init = GitHubFollowthroughService.__init__
    def intercepted_init(self, **kwargs):
        original_init(self, resolver=resolver, transport=httpx.MockTransport(github_transport), sleep=kwargs.get("sleep", asyncio.sleep))
    monkeypatch.setattr(GitHubFollowthroughService, "__init__", intercepted_init)
    monkeypatch.setattr("src.extensions.github_followthrough.github_followthrough_service", GitHubFollowthroughService())
    dispatcher, task, job, auth = await _prepare_actual_github_task(client, async_db, setup_workspace, monkeypatch)
    approval_id = job["declared_authority"]["approval_id"]
    pending = await client.get(f"/api/approvals/pending?approval_id={approval_id}&limit=1")
    assert pending.status_code == 200 and len(pending.json()) == 1, pending.text
    row = pending.json()[0]
    assert row["owner_principal_id"] == auth["principal_id"] and row["operator_session_id"] == auth["session_id"]
    assert row["approval_context"]["authority"]["job_id"] == job["job_id"]
    approved = await client.post(f"/api/approvals/{approval_id}/approve")
    assert approved.status_code == 200, approved.text
    assert requests == []
    await dispatcher.run_pass()
    unknown = (await client.get(f"/api/work-board/tasks/{task['task_id']}")).json()["task"]
    assert unknown["status"] == "blocked" and unknown["block_kind"] == "unknown_effect", unknown
    assert unknown["recovery_action"] == "reconcile_external_effect"
    actual_job = await durable_job_repository.get_job(job["job_id"])
    assert actual_job["status"] == "unknown_external_effect"
    assert requests.count("POST") == 1

    retry = await client.post(f"/api/work-board/tasks/{task['task_id']}/actions", json={"action": "retry", "expected_revision": unknown["task_revision"]})
    assert retry.status_code == 409, retry.text
    await async_db.engine.dispose()  # File-backed restart; the exact original operation survives.
    owning = await client.get(f"/api/capabilities/github/jobs/{job['job_id']}")
    assert owning.status_code == 200 and owning.json()["status"] == "unknown_external_effect"
    fail_readback[0] = False
    before = len(requests)
    reconciliation_errors = []
    original_reconcile = GitHubFollowthroughService.reconcile
    async def traced_reconcile(self, **kwargs):
        try:
            return await original_reconcile(self, **kwargs)
        except Exception as exc:
            reconciliation_errors.append(f"{type(exc).__name__}: {exc}")
            raise
    monkeypatch.setattr(GitHubFollowthroughService, "reconcile", traced_reconcile)
    reconciled = await client.post(f"/api/capabilities/github/jobs/{job['job_id']}/reconcile", json={})
    assert reconciled.status_code == 200, (reconciled.text, reconciliation_errors)
    assert reconciled.json()["status"] == "succeeded"
    assert reconciled.json()["operation_id"] == owning.json()["operation_id"]
    assert requests[before:] == ["GET"] and requests.count("POST") == 1
    reopened = await durable_job_repository.get_job(job["job_id"])
    assert any(effect.get("receipt_kind") == "readback" and effect.get("status") == "succeeded" and effect.get("details", {}).get("verified") is True for effect in reopened["effects"])
    await WorkBoardDispatcher(session_provider=async_db).run_pass()
    confirmed = (await client.get(f"/api/work-board/tasks/{task['task_id']}")).json()["task"]
    assert confirmed["status"] == "done" and confirmed["readback_status"] == "verified", confirmed
    assert confirmed["latest_attempt"]["attempt_id"] == unknown["latest_attempt"]["attempt_id"]
    async with async_db() as db:
        attempts = (await db.execute(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == task["task_id"]))).scalars().all()
    assert len(attempts) == 1
    await client.post("/api/auth/logout")
    login = await client.post("/api/auth/login", json={"password": "first-result-test-secret", "start_new_scope": True})
    assert login.status_code == 200, login.text
    denied = await client.get(f"/api/capabilities/github/jobs/{job['job_id']}")
    assert denied.status_code in {403, 404} and "preview" not in denied.json()
    denied_task = await client.get(f"/api/work-board/tasks/{task['task_id']}")
    assert denied_task.status_code == 404
    assert requests.count("POST") == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["deny", "expire", "cancel"])
async def test_pending_attention_terminal_recovery_never_contacts_remote(client, async_db, setup_workspace, monkeypatch, decision):
    dispatcher, task, job, auth = await _prepare_actual_github_task(client, async_db, setup_workspace, monkeypatch)
    approval_id = job["declared_authority"]["approval_id"]
    if decision == "deny":
        response = await client.post(f"/api/approvals/{approval_id}/deny")
        assert response.status_code == 200 and response.json()["status"] == "denied", response.text
    elif decision == "expire":
        async with async_db() as db:
            approval = await db.get(ApprovalRequest, approval_id)
            approval.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.add(approval)
        response = await client.get(f"/api/approvals/pending?approval_id={approval_id}&limit=1")
        assert response.status_code == 200 and response.json() == [], response.text
        forbidden = await client.post(f"/api/approvals/{approval_id}/approve")
        assert forbidden.status_code == 409, forbidden.text
    else:
        response = await client.post(f"/api/work-board/tasks/{task['task_id']}/actions", json={"action": "cancel", "expected_revision": task["task_revision"]})
        assert response.status_code == 200, response.text
    await dispatcher.run_pass()
    await async_db.engine.dispose()
    owning = await client.get(f"/api/capabilities/github/jobs/{job['job_id']}")
    assert owning.status_code == 200 and owning.json()["status"] == "cancelled", owning.text
    canonical = await durable_job_repository.get_job(job["job_id"])
    assert canonical["effects"] == []
    confirmed = await client.get(f"/api/work-board/tasks/{task['task_id']}")
    assert confirmed.status_code == 200 and confirmed.json()["task"]["status"] != "done", confirmed.text
    assert confirmed.json()["task"]["latest_attempt"]["workflow_run_id"] == job["job_id"]
