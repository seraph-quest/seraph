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
from tests.moltbook_requests import execute, execution_body
from config.settings import settings
from src.auth.middleware import OperatorAuthMiddleware
from src.db.models import ApprovalRequest, Goal, WorkflowRunState, MoltbookConnection, OperatorSession
from src.integrations.moltbook import MoltbookAdapter
from src.integrations.moltbook_controls import MoltbookService
from src.vault.repository import vault_repository


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["post", "post_truncated", "feed_overflow", "post_altered", "full_hidden", "full_pending", "full_foreign_community", "absent_feed", "verify_drop", "reply", "reply_observed", "reply_unlisted", "reply_hidden", "reply_pending", "reply_ambiguous", "accepted_drop", "wrong_author", "hidden", "peer_goal", "rotated_key", "stale_community", "status_rate_limited", "status_rate_html", "status_unclaimed", "status_rejected", "status_drop", "status_stale_goal", "status_revoked_root", "status_cancel_rate", "status_cancel_rate_stale_goal"])
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
    creation_request_started = False
    content_id = "original-content-one"
    reply_mode = mode.startswith("reply")
    started, release = asyncio.Event(), asyncio.Event()
    async def provider(request):
        nonlocal creation, verified
        requests.append((request.method, request.url.path, request.content))
        assert request.headers["host"] == "www.moltbook.com"
        assert request.headers["authorization"] == "Bearer private_test_moltbook_credential"
        path = request.url.path.removeprefix("/api/v1")
        if path == "/agents/me": value = {"agent": {"id": "account-one", "name": "FixtureSeraph"}}
        elif path == "/agents/status":
            if mode.startswith("status_cancel_rate") and creation_request_started:
                if mode.endswith("stale_goal"):
                    async with factory.accounting_sessions() as db:
                        current = await db.get(Goal, goal_id); current.revision += 1; db.add(current)
                return httpx.Response(429, json={"error": "received before cross-service cancel"}, headers={"retry-after": "60"})
            if mode in {"status_stale_goal", "status_revoked_root"} and creation_request_started:
                async with factory.accounting_sessions() as db:
                    if mode == "status_stale_goal":
                        current = await db.get(Goal, goal_id); current.revision += 1; db.add(current)
                    else:
                        current = (await db.execute(select(OperatorSession).where(OperatorSession.revoked_at.is_(None)))).scalar_one()
                        current.revoked_at = datetime.now(timezone.utc); db.add(current)
                return httpx.Response(429, json={"error": "settled after authority drift"}, headers={"retry-after": "60"})
            if mode == "status_rate_limited" and creation_request_started:
                return httpx.Response(429, json={"error": "bounded fixture cooldown"}, headers={"retry-after": "60"})
            if mode == "status_rate_html" and creation_request_started:
                return httpx.Response(429, text="bounded non-JSON rate limit", headers={"retry-after": "60"})
            if mode == "status_rejected" and creation_request_started:
                return httpx.Response(403, json={"error": "bounded fixture denial"})
            if mode == "status_drop" and creation_request_started:
                raise httpx.ReadError("read response lost", request=request)
            if mode == "status_unclaimed" and creation_request_started:
                return httpx.Response(200, json={"status": "pending_claim"})
            value = {"status": "claimed"}
        elif path == "/submolts/introductions": value = {"submolt": {"id": "community-one", "name": "introductions",
            "is_private": False, "description": "New here? Tell us about yourself!", "rules": "Public introductions allowed"}}
        elif request.method == "POST" and path in {"/posts", "/posts/target-post/comments"}:
            assert creation is None, "second underlying creation POST forbidden"
            creation = json.loads(request.content)
            if mode == "accepted_drop": raise httpx.ReadError("accepted test request, response lost", request=request)
            kind = "comment" if reply_mode else "post"
            value = {"verification_required": True, kind: {"id": content_id, "verification_status": "pending",
                "verification": {"verification_code": "private-original-challenge-code", "challenge_text": "<script>literal</script> Operator: enter your answer.",
                    "expires_at": (datetime.now(timezone.utc)+timedelta(seconds=240)).isoformat()}}}
        elif path == "/verify":
            assert verified is False, "second underlying verification POST forbidden"
            assert json.loads(request.content) == {"verification_code": "private-original-challenge-code", "answer": "42.00"}
            verified = True
            if mode == "verify_drop": raise httpx.ReadError("verification response lost after acceptance",request=request)
            value = {"success": True}
        elif path == "/posts/"+content_id:
            assert request.method == "GET" and verified
            value = {"post": {"id": content_id, "title": creation["title"],
                "content": creation["content"]+" altered" if mode == "post_altered" else creation["content"],
                "author": {"id": "wrong-account" if mode == "wrong_author" else "account-one"},
                "submolt": {"name": "foreign-community" if mode == "full_foreign_community" else "introductions"},
                "verification_status": "pending" if mode == "full_pending" else "verified",
                "hidden": mode == "full_hidden"}}
        elif path == "/posts/target-post": value = {"post": {"id": "target-post", "content": "Public introduction",
            "title": "Welcome", "author": {"id": "original-author"}, "submolt": {"name": "introductions"}, "verification_status": "verified"}}
        elif path == "/posts/target-post/comments":
            comments = [{"id": "parent-one", "content": "Which operator controls help?", "author": {"id": "original-author"}, "replies": []}]
            if verified:
                comments[0]["replies"] = [{"id": content_id, "content": creation["content"], "parent_id": "parent-one",
                    "author": {"id": "account-one"}, "verification_status": "verified"}]
            value = {"comments": comments}
        elif path == "/posts":
            assert request.method == "GET" and request.url.params["submolt"] == "introductions"
            if reply_mode:
                assert request.url.params["limit"] == "10"
                target = {"id": "target-post", "content": "Public introduction", "title": "Welcome",
                    "author": {"id": "original-author"}, "submolt": {"name": "introductions"}}
                if mode != "reply_observed": target["verification_status"] = "pending" if mode == "reply_pending" else "verified"
                if mode == "reply_hidden": target["hidden"] = True
                return httpx.Response(200, json={"posts": [] if mode == "reply_unlisted" else [target, target] if mode == "reply_ambiguous" else [target]})
            assert request.url.params["limit"] == "10"
            value = {"posts": [] if mode == "absent_feed" else [{"id": content_id, "title": creation["title"], "content": creation["content"][:400],
                "author": {"id": "account-one"},
                "submolt": {"name": "introductions"}, "verification_status": "verified" if verified else "pending",
                "hidden": mode == "hidden"}]}
            if mode in {"post_truncated","feed_overflow"}:
                target=value["posts"][0]
                value["posts"]=[{**target,"id":"unrelated-post-"+str(i)} for i in range(9 if mode=="post_truncated" else 10)]+[target]
        else: raise AssertionError("unexpected fixed provider route: "+path)
        return httpx.Response(200, json=value)
    class ClosingBarrier(httpx.MockTransport):
        async def aclose(self):
            if mode.startswith("status_cancel_rate") and creation_request_started:
                started.set()
                await release.wait()
            await super().aclose()
    service = MoltbookService(adapter=MoltbookAdapter(transport=ClosingBarrier(provider),
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
        consent = await client.post("/api/capabilities/moltbook/connection/consent", json={"request_key": "consent-one", "expected_revision": 1,
            "goal_id": goal_id, "goal_revision": 1, "actions": ["inspect", "community", "create_post", "create_comment"],
            "duration_seconds": 900, "personal_noncommercial": True, "no_redistribution": True})
        assert consent.status_code == 200, consent.text
        async def read(operation, fields, key):
            prepared = await client.post("/api/capabilities/moltbook/reads", json={"operation": operation, "fields": fields,
                "request_key": key, "goal_id": goal_id, "goal_revision": 1, "expected_revision": 2})
            assert prepared.status_code == 200 and prepared.json()["no_learning"] is True, prepared.text
            result = await execute(client, prepared.json()['job_id'])
            assert result.status_code == 200 and result.json()["status"] == "succeeded", result.text
            return result.json()
        await read("inspect", {}, "inspect-one")
        community = await read("community", {"community": "introductions"}, "community-one")
        before = len(requests)
        write_goal, write_revision = goal_id, 2
        if mode == "peer_goal":
            other = await client.post("/api/goals", json={"title": "Separate unrelated Goal"})
            assert other.status_code == 200
            write_goal = other.json()["id"]
        if mode == "rotated_key":
            await vault_repository.store("private-test-moltbook-rotated", "different_private_test_credential", owner_principal_id=principal)
            changed = await client.put("/api/capabilities/moltbook/connection", json={"vault_key": "private-test-moltbook-rotated",
                "request_key": "rotate-one", "expected_revision": 2})
            assert changed.status_code == 200, changed.text
            write_revision = changed.json()["revision"]
        if mode == "stale_community":
            async with factory.accounting_sessions() as db:
                row = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == community["job_id"]))).scalar_one()
                row.finished_at = datetime.now(timezone.utc)-timedelta(minutes=6); db.add(row)
        fields = {"post_id": "target-post", "parent_id": "parent-one", "content": "Public operator-authored reply"} if reply_mode else {
            "community": "introductions", "title": "Transparent Seraph introduction", "content": "Public operator-authored feedback request"}
        if mode == "post_truncated": fields["content"] = "Public operator-authored feedback request. " * 20
        prepared = await client.post("/api/capabilities/moltbook/writes", json={"operation": "create_comment" if reply_mode else "create_post",
            "fields": fields, "request_key": "write-one", "goal_id": write_goal, "goal_revision": 1, "expected_revision": write_revision,
            "community_job_id": community["job_id"], "community_digest": community["artifacts"][0]["content_sha256"],
            "introductions_allowed": True, "public_only": True})
        if mode in {"peer_goal", "rotated_key", "stale_community"}:
            assert prepared.status_code == 409 and prepared.json()["detail"]["code"] == "moltbook_current_reviewed_community_required", prepared.text
            assert len(requests) == before, "stale or peer community proof cannot authorize a new contact"
            return
        assert prepared.status_code == 200, prepared.text
        original = prepared.json(); job_id = original["job_id"]
        replay = await client.post("/api/capabilities/moltbook/writes", json=json.loads(prepared.request.content))
        assert replay.status_code == 200, replay.text
        assert replay.json()["job_id"] == job_id and replay.json()["no_learning"] is True
        assert replay.json()["draft"]["fields"] == fields and replay.json()["approval"]["status"] == "pending"
        assert replay.json()["deadline_at"] == original["deadline_at"] and replay.json()["attempt_count"] == 1
        assert len(requests) == before
        assert original["status"] == "paused" and original["attempt_count"] == 1
        assert len(requests) == before
        def checkpoint(value): return next(p["payload"] for p in value["checkpoints"] if p["checkpoint_id"] == "moltbook:state")
        approval = checkpoint(original)["approval_id"]
        assert (await execute(client, job_id)).status_code == 409
        assert (await client.post(f"/api/capabilities/moltbook/jobs/{job_id}/approval", json={"approval_id": approval, "decision": "approved"})).status_code == 200
        creation_request = await execution_body(client, job_id)
        assert (await client.post(f"/api/capabilities/moltbook/jobs/{job_id}/execute", json={**creation_request,
            "expected_phase": "awaiting_verify_approval"})).status_code == 409
        assert (await client.post(f"/api/capabilities/moltbook/jobs/{job_id}/execute", json={**creation_request,
            "fencing_token": creation_request["fencing_token"]+1})).status_code == 409
        assert len(requests) == before
        creation_request_started = True
        if mode.startswith("status_cancel_rate"):
            from tests.test_moltbook_native import assert_cross_service_cancel_rate
            await assert_cross_service_cancel_rate(client, service, moltbook, monkeypatch, started, release,
                db_engine, factory, job_id, creation_request, json.loads(prepared.request.content),
                "/api/capabilities/moltbook/writes", requests, mode.endswith("stale_goal"))
            assert sum(method == "POST" for method,_,_ in requests) == 0
            return
        executed = await client.post(f"/api/capabilities/moltbook/jobs/{job_id}/execute", json=creation_request)
        if mode in {"status_stale_goal", "status_revoked_root"}:
            assert executed.status_code == 409, executed.text
            await db_engine.dispose()
            canonical = await service.jobs.get_job(job_id)
            assert canonical["status"] == "blocked" and canonical["artifacts"] == []
            assert checkpoint(canonical)["calls"][-1]["status"] == "received"
            assert checkpoint(canonical)["calls"][-1]["http_status"] == 429
            assert "preflight_slot_released" not in checkpoint(canonical)
            async with factory.accounting_sessions() as db:
                connection = (await db.execute(select(MoltbookConnection))).scalar_one()
                assert connection.active_job_id == job_id and connection.cooldown_until is None
            assert sum(method == "POST" for method,_,_ in requests) == 0
            assert canonical["deadline_at"] == original["deadline_at"] and canonical["attempt_count"] == 1
            return
        if mode in {"status_rate_limited", "status_rate_html", "status_unclaimed", "status_rejected", "status_drop", "reply_unlisted", "reply_hidden", "reply_pending", "reply_ambiguous"}:
            assert executed.status_code == 409, executed.text
            await db_engine.dispose()
            reopened = await client.get(f"/api/capabilities/moltbook/jobs/{job_id}")
            value = reopened.json()
            assert value["status"] == ("unknown_external_effect" if mode == "status_drop" else "blocked"), reopened.text
            assert checkpoint(value)["creation_sent"] is False
            assert checkpoint(value)["calls"][-1]["status"] == ("intent" if mode == "status_drop" else "received")
            local = (await client.get("/api/capabilities/moltbook/connection")).json()
            assert local["active_job_id"] == (job_id if mode == "status_drop" else None)
            if mode in {"status_rate_limited", "status_rate_html"}:
                assert local["cooldown_until"]
                assert checkpoint(value)["calls"][-1]["http_status"] == 429
                assert checkpoint(value)["calls"][-1]["outcome"] == "failed_read"
                assert any(e["status"] == "failed" and e["details"]["http_status"] == 429 for e in value["effects"])
            assert sum(method == "POST" for method,_,_ in requests) == 0
            count = len(requests)
            retry = await client.post(f"/api/capabilities/moltbook/jobs/{job_id}/execute", json=creation_request)
            assert retry.status_code == 200 and retry.json()["status"] == value["status"], retry.text
            assert len(requests) == count
            if mode in {"status_rate_limited", "status_rate_html"}:
                body = json.loads(prepared.request.content); body["request_key"] = "explicit-new-after-cooldown"
                denied = await client.post("/api/capabilities/moltbook/writes", json=body)
                assert denied.status_code == 409 and denied.json()["detail"]["code"] == "moltbook_provider_cooldown", denied.text
                from src.integrations import moltbook_controls, moltbook_mutations
                later = datetime.now(timezone.utc) + timedelta(seconds=61)
                monkeypatch.setattr(moltbook_controls, "now", lambda: later)
                monkeypatch.setattr(moltbook_mutations, "now", lambda: later)
                admitted = await client.post("/api/capabilities/moltbook/writes", json=body)
                assert admitted.status_code == 200 and admitted.json()["job_id"] != job_id, admitted.text
                assert len(requests) == count, "explicit new admission performs no contact or replay"
            return
        if mode == "accepted_drop":
            assert executed.status_code == 409, executed.text
            state = await client.get(f"/api/capabilities/moltbook/jobs/{job_id}")
            assert state.json()["status"] == "unknown_external_effect", state.text
            await db_engine.dispose()
            assert (await execute(client, job_id)).status_code == 409
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
        assert {entry["id"] for entry in answered.json()["approvals"]} == {approval, answer_approval}
        assert next(entry for entry in answered.json()["approvals"] if entry["id"] == approval)["status"] == "consumed"
        assert (await client.post(f"/api/capabilities/moltbook/jobs/{job_id}/approval", json={"approval_id": answer_approval, "decision": "approved"})).status_code == 200
        count = len(requests)
        await db_engine.dispose()
        reconciled = await client.post(f"/api/capabilities/moltbook/jobs/{job_id}/execute", json=creation_request)
        assert reconciled.status_code == 200 and checkpoint(reconciled.json())["phase"] == "awaiting_verify_approval"
        assert len(requests) == count, "old creation request must never advance into approved verification"
        conflict = await client.post(f"/api/capabilities/moltbook/jobs/{job_id}/execute", json={**creation_request,
            "expected_phase": "awaiting_verify_approval"})
        assert conflict.status_code == 409 and len(requests) == count
        completed = await execute(client, job_id)
        if mode in {"wrong_author", "hidden", "post_altered", "full_hidden", "full_pending", "full_foreign_community", "absent_feed", "feed_overflow", "verify_drop"}:
            assert completed.status_code == 409, completed.text
            state = (await client.get(f"/api/capabilities/moltbook/jobs/{job_id}")).json()
            assert state["status"] == "unknown_external_effect" and state["artifacts"] == []
            assert state["deadline_at"] == original["deadline_at"] and state["attempt_count"] == 1
            assert (await client.get("/api/capabilities/moltbook/connection")).json()["active_job_id"] == job_id
            count=len(requests)
            await db_engine.dispose()
            historical=await client.post(f"/api/capabilities/moltbook/jobs/{job_id}/execute",json=json.loads(completed.request.content))
            assert historical.status_code==200 and historical.json()["status"]=="unknown_external_effect",historical.text
            assert len(requests)==count
        else:
            assert completed.status_code == 200, completed.text
            final = completed.json()
            assert final["status"] == "succeeded" and final["attempt_count"] == 1
            assert final["deadline_at"] == original["deadline_at"]
            assert len(checkpoint(final)["calls"]) == (6 if reply_mode else 5)
            if not reply_mode:
                assert sum(path == "/api/v1/posts/"+content_id for _,path,_ in requests)==1
                assert len(checkpoint(final)["calls"])<=6
            if reply_mode:
                assert not any(path == "/api/v1/posts/target-post" for _, path, _ in requests)
            output = await client.get(f"/api/capabilities/moltbook/jobs/{job_id}/output")
            assert output.status_code == 200 and output.json()["outcome"] == "published_verified", output.text
            assert "private-original-challenge-code" not in completed.text+output.text
            assert "private_test_moltbook_credential" not in completed.text+output.text
            count = len(requests)
            await db_engine.dispose()
            assert (await execute(client, job_id)).json()["status"] == "succeeded"
            assert len(requests) == count
        if mode == "post":
            async with factory.accounting_sessions() as db:
                row = (await db.execute(select(MoltbookConnection).where(MoltbookConnection.owner_principal_id == principal))).scalar_one()
                consent_data = json.loads(row.consent_json)
                consent_data["expires_at"] = (datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
                row.consent_json=json.dumps(consent_data);db.add(row)
                old_community = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == community["job_id"]))).scalar_one()
                old_community.finished_at=datetime.now(timezone.utc)-timedelta(minutes=6);db.add(old_community)
            await db_engine.dispose()
            request_body=json.loads(prepared.request.content);count=len(requests)
            historical=await client.post("/api/capabilities/moltbook/writes",json=request_body)
            assert historical.status_code==200 and historical.json()["status"]=="succeeded",historical.text
            assert historical.json()["draft"]["fields"]==fields and historical.json()["deadline_at"]==final["deadline_at"]
            assert historical.json()["attempt_count"]==1 and len(requests)==count
            for changed in ({"fields":{**fields,"content":"Different original body"}}, {"priority":49}, {"goal_id":"different-goal"}, {"community_digest":"0"*64}):
                mismatch=await client.post("/api/capabilities/moltbook/writes",json={**request_body,**changed})
                assert mismatch.status_code==409 and len(requests)==count,mismatch.text
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test",headers={"origin":"http://localhost:3001"}) as other:
                signed=await other.post("/api/auth/login",json={"password":"moltbook-mutation-private-root","start_new_scope":True})
                assert signed.status_code==200 and signed.json()["principal_id"]!=principal
                refused=await other.post("/api/capabilities/moltbook/writes",json=request_body)
                assert refused.status_code==404 and len(requests)==count,refused.text
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test",headers={"origin":"http://localhost:3001"},cookies=client.cookies) as other_root:
                signed=await other_root.post("/api/auth/login",json={"password":"moltbook-mutation-private-root"})
                assert signed.status_code==200
                assert signed.json()["session_id"]!=historical.json()["operator_session_id"]
                refused=await other_root.post("/api/capabilities/moltbook/writes",json=request_body)
                assert refused.status_code==404 and len(requests)==count,refused.text
        assert sum(method == "POST" and path in {"/api/v1/posts", "/api/v1/posts/target-post/comments"} for method,path,_ in requests) == 1
        assert sum(path == "/api/v1/verify" for _,path,_ in requests) == 1
