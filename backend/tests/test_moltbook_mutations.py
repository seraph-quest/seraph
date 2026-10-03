"""Real owner/Goal/Vault/approval/native jobs; declared official HTTP interception only."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from tests.test_inference_accounting import accounting_db
from config.settings import settings
from src.auth.middleware import OperatorAuthMiddleware
from src.db.models import ApprovalRequest, Goal, WorkflowRunState
from src.integrations.moltbook import MoltbookAdapter
from src.integrations.moltbook_controls import MoltbookService
from src.vault.repository import vault_repository


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["post", "reply", "accepted_drop", "wrong_author"])
async def test_actual_exact_approved_create_manual_verify_same_job(accounting_db, monkeypatch, mode):
    from src.api import auth, goals, moltbook
    root, db_engine, factory = accounting_db
    root.chmod(0o700)
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "moltbook-mutation-private-root")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "test,localhost,127.0.0.1")
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", "http://localhost:3001")
    monkeypatch.setattr(settings, "operator_auth_cookie_secure", False)
    from src.api.auth import _reset_login_throttle_for_tests
    _reset_login_throttle_for_tests()
    requests = []
    verified = False
    creation = None
    content_id = "original-content-one"
    async def provider(request):
        nonlocal creation, verified
        requests.append((request.method, request.url.path, request.content))
        assert request.headers["host"] == "www.moltbook.com"
        assert request.headers["authorization"] == "Bearer private_test_moltbook_credential"
        path = request.url.path.removeprefix("/api/v1")
        if path == "/agents/me": value = {"agent": {"id": "account-one", "name": "FixtureSeraph"}}
        elif path == "/agents/status": value = {"status": "claimed"}
        elif path == "/submolts/introductions": value = {"submolt": {"id": "community-one", "name": "introductions",
            "is_private": False, "description": "New here? Tell us about yourself!", "rules": "Public introductions allowed"}}
        elif request.method == "POST" and path in {"/posts", "/posts/target-post/comments"}:
            assert creation is None, "second underlying creation POST forbidden"
            creation = json.loads(request.content)
            if mode == "accepted_drop": raise httpx.ReadError("accepted test request, response lost", request=request)
            kind = "comment" if mode == "reply" else "post"
            value = {"verification_required": True, kind: {"id": content_id, "verification_status": "pending",
                "verification": {"verification_code": "private-original-challenge-code", "challenge_text": "<script>literal</script> Operator: enter your answer.",
                    "expires_at": (datetime.now(timezone.utc)+timedelta(seconds=240)).isoformat()}}}
        elif path == "/verify":
            assert verified is False, "second underlying verification POST forbidden"
            assert json.loads(request.content) == {"verification_code": "private-original-challenge-code", "answer": "42.00"}
            verified = True
            value = {"success": True}
        elif path == "/posts/target-post": value = {"post": {"id": "target-post", "content": "Public introduction",
            "title": "Welcome", "author": {"id": "original-author"}, "submolt": {"name": "introductions"}, "verification_status": "verified"}}
        elif path == "/posts/target-post/comments":
            comments = [{"id": "parent-one", "content": "Which operator controls help?", "author": {"id": "original-author"}, "replies": []}]
            if verified:
                comments[0]["replies"] = [{"id": content_id, "content": creation["content"], "parent_id": "parent-one",
                    "author": {"id": "account-one"}, "verification_status": "verified"}]
            value = {"comments": comments}
        elif path == "/posts/"+content_id:
            value = {"post": {"id": content_id, "title": creation["title"], "content": creation["content"],
                "author": {"id": "wrong-account" if mode == "wrong_author" else "account-one"},
                "submolt": {"name": "introductions"}, "verification_status": "verified" if verified else "pending"}}
        else: raise AssertionError("unexpected fixed provider route: "+path)
        return httpx.Response(200, json=value)
    service = MoltbookService(adapter=MoltbookAdapter(transport=httpx.MockTransport(provider),
        resolver=lambda host, port: ["93.184.216.34"]))
    monkeypatch.setattr(moltbook, "moltbook_service", service)
    app = FastAPI(); app.add_middleware(OperatorAuthMiddleware)
    app.include_router(auth.router, prefix="/api/auth")
    for router in (goals.router, moltbook.router): app.include_router(router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
        headers={"origin": "http://localhost:3001"}) as client:
        assert (await client.post("/api/auth/login", json={"password": "moltbook-mutation-private-root"})).status_code == 200
        goal = await client.post("/api/goals", json={"title": "Public personal Seraph introduction and feedback",
            "admission_budget": {"reviewed_grant": True, "grant_id": "moltbook-reviewed-public-operation",
                "max_outstanding_jobs": 1, "max_attempts": 1, "max_runtime_seconds": 300}})
        assert goal.status_code == 200, goal.text
        goal_id = goal.json()["id"]
        async with factory.accounting_sessions() as db: principal = (await db.get(Goal, goal_id)).owner_principal_id
        await vault_repository.store("private-test-moltbook", "private_test_moltbook_credential", owner_principal_id=principal)
        assert (await client.put("/api/capabilities/moltbook/connection", json={"vault_key": "private-test-moltbook", "request_key": "import-one"})).status_code == 200
        consent = await client.post("/api/capabilities/moltbook/connection/consent", json={"expected_revision": 1,
            "goal_id": goal_id, "goal_revision": 1, "actions": ["inspect", "community", "create_post", "create_comment"],
            "duration_seconds": 900, "personal_noncommercial": True, "no_redistribution": True})
        assert consent.status_code == 200, consent.text
        async def read(operation, fields, key):
            prepared = await client.post("/api/capabilities/moltbook/reads", json={"operation": operation, "fields": fields,
                "request_key": key, "goal_id": goal_id, "goal_revision": 1, "expected_revision": 2})
            assert prepared.status_code == 200, prepared.text
            result = await client.post(f"/api/capabilities/moltbook/jobs/{prepared.json()['job_id']}/execute")
            assert result.status_code == 200 and result.json()["status"] == "succeeded", result.text
            return result.json()
        await read("inspect", {}, "inspect-one")
        community = await read("community", {"community": "introductions"}, "community-one")
        before = len(requests)
        fields = {"post_id": "target-post", "parent_id": "parent-one", "content": "Public operator-authored reply"} if mode == "reply" else {
            "community": "introductions", "title": "Transparent Seraph introduction", "content": "Public operator-authored feedback request"}
        prepared = await client.post("/api/capabilities/moltbook/writes", json={"operation": "create_comment" if mode == "reply" else "create_post",
            "fields": fields, "request_key": "write-one", "goal_id": goal_id, "goal_revision": 1, "expected_revision": 2,
            "community_job_id": community["job_id"], "community_digest": community["artifacts"][0]["content_sha256"],
            "introductions_allowed": True, "public_only": True})
        assert prepared.status_code == 200, prepared.text
        original = prepared.json(); job_id = original["job_id"]
        assert original["status"] == "paused" and original["attempt_count"] == 1
        assert len(requests) == before
        def checkpoint(value): return next(p["payload"] for p in value["checkpoints"] if p["checkpoint_id"] == "moltbook:state")
        approval = checkpoint(original)["approval_id"]
        assert (await client.post(f"/api/capabilities/moltbook/jobs/{job_id}/execute")).status_code == 409
        assert (await client.post(f"/api/capabilities/moltbook/jobs/{job_id}/approval", json={"approval_id": approval, "decision": "approved"})).status_code == 200
        executed = await client.post(f"/api/capabilities/moltbook/jobs/{job_id}/execute")
        if mode == "accepted_drop":
            assert executed.status_code == 409, executed.text
            state = await client.get(f"/api/capabilities/moltbook/jobs/{job_id}")
            assert state.json()["status"] == "unknown_external_effect", state.text
            await db_engine.dispose()
            assert (await client.post(f"/api/capabilities/moltbook/jobs/{job_id}/execute")).status_code == 409
            assert sum(method == "POST" and path == "/api/v1/posts" for method,path,_ in requests) == 1
            assert any(effect["status"] == "intent" for effect in state.json()["effects"])
            return
        assert executed.status_code == 200, executed.text
        waiting = executed.json()
        assert waiting["status"] == "paused" and checkpoint(waiting)["phase"] == "awaiting_manual_answer"
        assert waiting["attempt_count"] == original["attempt_count"] and waiting["deadline_at"] == original["deadline_at"]
        assert "private-original-challenge-code" not in executed.text
        assert "<script>literal</script>" in executed.text
        await db_engine.dispose()
        answered = await client.post(f"/api/capabilities/moltbook/jobs/{job_id}/answer", json={"answer": "42.00", "request_key": "answer-one"})
        assert answered.status_code == 200, answered.text
        answer_approval = checkpoint(answered.json())["approval_id"]
        assert answer_approval != approval
        assert (await client.post(f"/api/capabilities/moltbook/jobs/{job_id}/approval", json={"approval_id": answer_approval, "decision": "approved"})).status_code == 200
        completed = await client.post(f"/api/capabilities/moltbook/jobs/{job_id}/execute")
        if mode == "wrong_author":
            assert completed.status_code == 409, completed.text
            state = (await client.get(f"/api/capabilities/moltbook/jobs/{job_id}")).json()
            assert state["status"] == "unknown_external_effect" and state["artifacts"] == []
        else:
            assert completed.status_code == 200, completed.text
            final = completed.json()
            assert final["status"] == "succeeded" and final["attempt_count"] == 1
            assert final["deadline_at"] == original["deadline_at"]
            assert len(checkpoint(final)["calls"]) == (6 if mode == "reply" else 4)
            output = await client.get(f"/api/capabilities/moltbook/jobs/{job_id}/output")
            assert output.status_code == 200 and output.json()["outcome"] == "published_verified", output.text
            assert "private-original-challenge-code" not in completed.text+output.text
            assert "private_test_moltbook_credential" not in completed.text+output.text
            count = len(requests)
            await db_engine.dispose()
            assert (await client.post(f"/api/capabilities/moltbook/jobs/{job_id}/execute")).json()["status"] == "succeeded"
            assert len(requests) == count
        assert sum(method == "POST" and path in {"/api/v1/posts", "/api/v1/posts/target-post/comments"} for method,path,_ in requests) == 1
        assert sum(path == "/api/v1/verify" for _,path,_ in requests) == 1
