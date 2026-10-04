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
@pytest.mark.parametrize("mode", ["configure_revoke", "vault_stage_drift", "root_replaced", "expired_unstarted", "expired_started"])
async def test_actual_auth_vault_connection_cas_without_provider_contact(accounting_db, monkeypatch, mode):
    from src.api import auth, forgejo, goals
    from src.integrations import forgejo_controls
    if mode.startswith("expired_"):
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
        if mode.startswith("expired_"):
            assert saved.status_code==200,saved.text
            assert (await client.put("/api/capabilities/forgejo/connection/read-consent",json={
                "expected_revision":1,"duration_seconds":900,"read_ack":True})).status_code==200
            import uuid
            prepared=await client.post("/api/capabilities/forgejo/jobs",json={"operation":"provision","fields":{},
                "request_key":str(uuid.uuid4()),"goal_id":goal.json()["id"],"goal_revision":goal.json()["revision"],"expected_revision":1})
            assert prepared.status_code==200,prepared.text
            bound=prepared.json()
            if mode=="expired_started":
                with pytest.raises(AssertionError,match="negative fixture forbids"):
                    await client.post("/api/capabilities/forgejo/jobs/"+bound["job_id"]+"/execute",json={
                        "expected_revision":bound["revision"],"fencing_token":bound["lease"]["fencing_token"]})
                bound=(await client.get("/api/capabilities/forgejo/jobs/"+bound["job_id"])).json()
                assert bound["status"]=="unknown_external_effect" and bound["attempt_count"]==1
            # Declared expiry test boundary: shorten this actual admitted job's
            # deadline only. Never extend authority or insert a success proof.
            from datetime import datetime,timedelta,timezone
            from sqlmodel import select
            from src.db.models import WorkflowRunState
            async with factory.accounting_sessions() as db:
                run=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==bound["job_id"]))
                run.deadline_at=datetime.now(timezone.utc)-timedelta(seconds=1);db.add(run)
            before=(await client.get("/api/capabilities/forgejo/jobs/"+bound["job_id"])).json()
            body={"expected_revision":before["revision"],"fencing_token":before["lease"]["fencing_token"]}
            cancelled=await client.post("/api/capabilities/forgejo/jobs/"+bound["job_id"]+"/cancel",json=body)
            if mode=="expired_unstarted":
                assert cancelled.status_code==200,cancelled.text
                assert cancelled.json()["status"]=="cancelled" and cancelled.json()["forgejo"]["calls"]==[]
                assert cancelled.json()["forgejo"]["capacity_closed"]
                replay=await client.post("/api/capabilities/forgejo/jobs/"+bound["job_id"]+"/cancel",json=body)
                assert replay.status_code==200 and replay.json()==cancelled.json()
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
            wrong = await client.post("/api/capabilities/forgejo/connection/revoke", json={"expected_revision": 20})
            assert wrong.status_code == 409
            revoked = await client.post("/api/capabilities/forgejo/connection/revoke", json={"expected_revision": 1})
            assert revoked.status_code == 200, revoked.text
            assert revoked.json()["state"] == "revoked" and revoked.json()["revision"] == 2
            assert revoked.json()["read_consent_revision"] == 1
