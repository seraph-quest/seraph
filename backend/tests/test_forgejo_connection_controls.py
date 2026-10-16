import json
import os

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event

from tests.test_inference_accounting import accounting_db
from config.settings import settings
from src.auth.middleware import OperatorAuthMiddleware
from src.db.models import Goal
from src.vault.repository import vault_repository


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["configure_revoke", "vault_stage_drift", "root_replaced", "expired_unstarted", "expired_started",
    "local_revoke", "local_read_expired", "local_rotate", "local_goal_changed", "local_wrong_root",
    "local_effect_intent", "local_started_revoked"])
async def test_actual_auth_vault_connection_cas_without_provider_contact(accounting_db, monkeypatch, mode):
    from src.api import auth, forgejo, goals
    from src.integrations import forgejo_controls
    if mode.startswith(("expired_","local_")):
        from src.browser.forgejo_profile import ForgejoTitleBrowser
        class DeniedTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self,request):
                raise AssertionError("negative fixture forbids every provider contact")
        service=forgejo_controls.ForgejoService(browser=ForgejoTitleBrowser(local_transport=DeniedTransport(),resolver=lambda h,p:["1.1.1.1"]))
        monkeypatch.setattr(forgejo,"forgejo_service",service)
    root, db_engine, factory = accounting_db
    os.chmod(root, 0o700)
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "forgejo-test-root")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "test,localhost,127.0.0.1")
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", "http://localhost:3001")
    monkeypatch.setattr(settings, "operator_auth_cookie_secure", False)
    auth._reset_login_throttle_for_tests()
    writers = set()
    def begin(connection, cursor, statement, parameters, context, many):
        if statement.strip().upper().startswith("BEGIN IMMEDIATE"): writers.add(id(connection))
    def end(connection): writers.discard(id(connection))
    event.listen(db_engine.sync_engine, "before_cursor_execute", begin)
    event.listen(db_engine.sync_engine, "commit", end)
    event.listen(db_engine.sync_engine, "rollback", end)
    real_encrypt = forgejo_controls.encrypt
    def outside_writer(value):
        assert not writers, "crypto/signing-key inspection inside canonical writer"
        return real_encrypt(value)
    monkeypatch.setattr(forgejo_controls, "encrypt", outside_writer)
    app = FastAPI(); app.add_middleware(OperatorAuthMiddleware)
    app.include_router(auth.router, prefix="/api/auth")
    app.include_router(goals.router, prefix="/api")
    app.include_router(forgejo.router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                headers={"origin": "http://localhost:3001"}) as client:
        assert (await client.get("/api/capabilities/forgejo/connection")).status_code == 401
        assert (await client.post("/api/auth/login", json={"password": "forgejo-test-root"})).status_code == 200
        goal = await client.post("/api/goals", json={"title": "Configure an optional fixed-site adapter"})
        assert goal.status_code == 200, goal.text
        async with factory.accounting_sessions() as db:
            row = await db.get(Goal, goal.json()["id"])
            principal = row.owner_principal_id
        raw = json.dumps({"user_name": "local", "password": "local-fixture-password"})
        await vault_repository.store("forgejo-input", raw, owner_principal_id=principal)
        real_snapshot = vault_repository.snapshot
        async def staged(*args, **kwargs):
            assert not writers, "Vault nested session/decrypt inside canonical writer"
            result = await real_snapshot(*args, **kwargs)
            if mode == "vault_stage_drift":
                await vault_repository.store("forgejo-input", json.dumps({"user_name": "local", "password": "changed-fixture-password"}),
                                             owner_principal_id=principal)
            if mode == "root_replaced":
                assert (await client.post("/api/auth/refresh")).status_code == 200
            return result
        monkeypatch.setattr(vault_repository, "snapshot", staged)
        saved = await client.put("/api/capabilities/forgejo/connection",
                                 json={"vault_key": "forgejo-input", "expected_revision": 0})
        if mode.startswith(("expired_","local_")):
            assert saved.status_code==200,saved.text
            assert (await client.put("/api/capabilities/forgejo/connection/read-consent",json={
                "expected_revision":1,"duration_seconds":900,"read_ack":True})).status_code==200
            import uuid
            prepared=await client.post("/api/capabilities/forgejo/jobs",json={"operation":"provision","fields":{},
                "request_key":str(uuid.uuid4()),"goal_id":goal.json()["id"],"goal_revision":goal.json()["revision"],"expected_revision":1})
            assert prepared.status_code==200,prepared.text
            bound=prepared.json()
            if mode in {"expired_started","local_started_revoked"}:
                with pytest.raises(AssertionError,match="negative fixture forbids"):
                    await client.post("/api/capabilities/forgejo/jobs/"+bound["job_id"]+"/execute",json={
                        "expected_revision":bound["revision"],"fencing_token":bound["lease"]["fencing_token"]})
                bound=(await client.get("/api/capabilities/forgejo/jobs/"+bound["job_id"])).json()
                assert bound["status"]=="unknown_external_effect" and bound["attempt_count"]==1
            # Declared expiry test boundary: shorten this actual admitted job's
            # deadline only. Never extend authority or insert a success proof.
            from datetime import datetime,timedelta,timezone
            from sqlmodel import select
            from src.db.models import WorkflowRunState,ForgejoConnection
            if mode.startswith("expired_"):
                async with factory.accounting_sessions() as db:
                    run=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==bound["job_id"]))
                    run.deadline_at=datetime.now(timezone.utc)-timedelta(seconds=1);db.add(run)
            connection_revision=1
            goal_revision=goal.json()["revision"]
            if mode in {"local_revoke","local_started_revoked"}:
                revoked=await client.post("/api/capabilities/forgejo/connection/revoke",json={"expected_revision":1})
                assert revoked.status_code==200,revoked.text
                connection_revision=revoked.json()["revision"]
            elif mode=="local_rotate":
                rotated=await client.put("/api/capabilities/forgejo/connection",json={"vault_key":"forgejo-input","expected_revision":1})
                assert rotated.status_code==200,rotated.text
                connection_revision=rotated.json()["revision"]
            elif mode=="local_read_expired":
                async with factory.accounting_sessions() as db:
                    row=await db.scalar(select(ForgejoConnection).where(ForgejoConnection.owner_principal_id==principal))
                    row.read_consent_expires_at=datetime.now(timezone.utc)-timedelta(seconds=1);db.add(row)
            elif mode=="local_goal_changed":
                changed=await client.patch("/api/goals/"+goal.json()["id"],json={"description":"Withdraw old preparation after Goal correction","status":"completed","expected_revision":goal_revision})
                assert changed.status_code==200,changed.text
                goal_revision=changed.json()["revision"]
            elif mode=="local_effect_intent":
                # Metadata-only malformed accepted-state negative, never a
                # fabricated successful outcome or a provider contact.
                async with factory.accounting_sessions() as db:
                    run=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==bound["job_id"]))
                    run.effect_receipts_json=json.dumps([{"effect_id":"negative-intent","status":"intent"}]);db.add(run)
            before=(await client.get("/api/capabilities/forgejo/jobs/"+bound["job_id"])).json()
            body={"expected_revision":before["revision"],"fencing_token":before["lease"]["fencing_token"]}
            if mode=="local_wrong_root":
                assert (await client.post("/api/auth/login",json={"password":"forgejo-test-root"})).status_code==200
            if mode.startswith("local_") and mode!="local_wrong_root":
                for field in ("expected_revision","fencing_token"):
                    wrong=await client.post("/api/capabilities/forgejo/jobs/"+bound["job_id"]+"/cancel",json={**body,field:body[field]+1})
                    assert wrong.status_code==409,wrong.text
                    assert (await client.get("/api/capabilities/forgejo/jobs/"+bound["job_id"])).json()==before
            # Cancellation must not stage files, inspect/decrypt Vault or
            # contact a provider, including after credential revocation.
            from src.browser import forgejo_native
            def no_private_io(*args,**kwargs):raise AssertionError("local withdrawal performed private I/O")
            with monkeypatch.context() as isolated:
                isolated.setattr(forgejo_native,"stage",no_private_io)
                isolated.setattr(forgejo_native,"decrypt",no_private_io)
                isolated.setattr(forgejo_native,"encrypt",no_private_io)
                isolated.setattr(vault_repository,"snapshot",no_private_io)
                cancelled=await client.post("/api/capabilities/forgejo/jobs/"+bound["job_id"]+"/cancel",json=body)
            positive=mode in {"expired_unstarted","local_revoke","local_read_expired","local_rotate","local_goal_changed"}
            if positive:
                assert cancelled.status_code==200,cancelled.text
                assert cancelled.json()["status"]=="cancelled" and cancelled.json()["forgejo"]["calls"]==[]
                assert cancelled.json()["forgejo"]["capacity_closed"]
                replay=await client.post("/api/capabilities/forgejo/jobs/"+bound["job_id"]+"/cancel",json=body)
                assert replay.status_code==200 and replay.json()==cancelled.json()
                assert cancelled.json()["deadline_at"]==before["deadline_at"]
                if mode.startswith("local_"):
                    # A fresh explicit configuration/read window and fresh
                    # request can now be admitted; the old job cannot execute.
                    assert (await client.post("/api/capabilities/forgejo/jobs/"+bound["job_id"]+"/execute",json=body)).status_code==409
                    if mode=="local_goal_changed":
                        # Withdrawal is allowed on the now-completed Goal;
                        # fresh execution still requires an explicit active Goal.
                        reopened=await client.patch("/api/goals/"+goal.json()["id"],json={"status":"active","expected_revision":goal_revision})
                        assert reopened.status_code==200,reopened.text
                        goal_revision=reopened.json()["revision"]
                    configured=await client.put("/api/capabilities/forgejo/connection",json={"vault_key":"forgejo-input","expected_revision":connection_revision})
                    assert configured.status_code==200,configured.text
                    connection_revision=configured.json()["revision"]
                    assert (await client.put("/api/capabilities/forgejo/connection/read-consent",json={"expected_revision":connection_revision,"duration_seconds":900,"read_ack":True})).status_code==200
                    fresh=await client.post("/api/capabilities/forgejo/jobs",json={"operation":"provision","fields":{},"request_key":str(uuid.uuid4()),"goal_id":goal.json()["id"],"goal_revision":goal_revision,"expected_revision":connection_revision})
                    assert fresh.status_code==200 and fresh.json()["status"]=="accepted",fresh.text
                    assert fresh.json()["job_id"]!=bound["job_id"]
                    assert (await client.post("/api/capabilities/forgejo/jobs/"+bound["job_id"]+"/cancel",json=body)).json()==cancelled.json()
            elif mode=="local_wrong_root":
                assert cancelled.status_code==404,cancelled.text
                async with factory.accounting_sessions() as db:
                    run=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==bound["job_id"]))
                    assert run.status=="accepted" and run.revision==before["revision"]
            else:
                assert cancelled.status_code==409,cancelled.text
                assert (await client.get("/api/capabilities/forgejo/jobs/"+bound["job_id"])).json()==before
            (root/(mode+"-receipt.json")).write_text(json.dumps({"boundary":"actual Root/Goal/Vault/admission; job deadline shortened for expiry negative only; no provider contact or success proof","before":before,"status":cancelled.status_code,"after":cancelled.json()},indent=2))
            os.chmod(root/(mode+"-receipt.json"),0o600)
        elif mode != "configure_revoke":
            assert saved.status_code in {403, 409}, saved.text
            assert (await client.get("/api/capabilities/forgejo/connection")).json()["configured"] is False
        else:
            assert saved.status_code == 200, saved.text
            value = saved.json()
            assert value["configured"] and value["state"] == "configured"
            assert value["provider_user_id"] is None and value["read_consent_expires_at"] is None
            assert value["available"] is False and value["production_acceptance"] == "blocked_unverified"
            assert "local-fixture-password" not in saved.text
            assert value["reviewed_form_profile_ids"] == []
            profile_revision=value["form_profiles_revision"]
            activation={"expected_revision":1,"expected_form_profiles_revision":profile_revision,
                        "profile_ids":[],"profile_ack":True}
            route="/api/capabilities/forgejo/connection/form-profiles"
            for fields in ({"profile_ack":False},{"profile_ack":1},{"profile_ids":["other"]},
                           {"profile_ids":["forgejo.issue-comment.v1"]*2},
                           {"profile_ids":["forgejo.issue-create.v1","forgejo.issue-comment.v1"]}):
                denied=await client.put(route,json={**activation,**fields})
                assert denied.status_code==422,denied.text
                assert (await client.get("/api/capabilities/forgejo/connection")).json()==value
            for fields in ({"expected_revision":20},{"expected_form_profiles_revision":profile_revision+1}):
                assert (await client.put(route,json={**activation,**fields})).status_code==409
            unavailable=await client.put(route,json={**activation,"profile_ids":["forgejo.issue-create.v1"]})
            assert unavailable.status_code==409,unavailable.text
            unchanged=await client.put(route,json=activation)
            assert unchanged.status_code==200 and unchanged.json()==value
            wrong = await client.post("/api/capabilities/forgejo/connection/revoke", json={"expected_revision": 20})
            assert wrong.status_code == 409
            revoked = await client.post("/api/capabilities/forgejo/connection/revoke", json={"expected_revision": 1})
            assert revoked.status_code == 200, revoked.text
            assert revoked.json()["state"] == "revoked" and revoked.json()["revision"] == 2
            assert revoked.json()["read_consent_revision"] == 1
            assert revoked.json()["reviewed_form_profile_ids"]==[]
            assert revoked.json()["form_profiles_revision"]==profile_revision+1
