"""Actual optional signed-provider fixture; never a success receipt factory."""
import json
import os
from pathlib import Path
import uuid

import httpx
import pytest

from tests.test_inference_accounting import accounting_db
from config.settings import settings
from src.auth.middleware import OperatorAuthMiddleware
from src.db.models import Goal
from src.vault.repository import vault_repository


class OwnedStream(httpx.AsyncByteStream):
    def __init__(self,response,client): self.response,self.client=response,client
    async def __aiter__(self):
        async for block in self.response.aiter_raw(): yield block
    async def aclose(self):
        await self.response.aclose();await self.client.aclose()


class ActualLoopback(httpx.AsyncBaseTransport):
    """Only logical Host+TLS SNI codeberg maps to the confined local provider."""
    def __init__(self,port): self.port=port;self.requests=[]
    async def handle_async_request(self,request):
        assert request.url.scheme=="https" and request.headers.get("host")=="codeberg.org"
        assert request.extensions.get("sni_hostname") in ("codeberg.org",b"codeberg.org")
        self.requests.append((request.method,request.url.path))
        client=httpx.AsyncClient(trust_env=False,follow_redirects=False)
        mapped=client.build_request(request.method,"http://127.0.0.1:"+str(self.port)+request.url.raw_path.decode(),
            headers={**request.headers,"host":"codeberg.org"},content=await request.aread())
        try:response=await client.send(mapped,stream=True)
        except BaseException:
            await client.aclose();raise
        return httpx.Response(response.status_code,headers=response.headers,
            stream=OwnedStream(response,client),request=request)
    async def aclose(self):pass


@pytest.mark.asyncio
async def test_actual_signed_provider_auth_vault_native_chrome_title(accounting_db,monkeypatch):
    credential_file=os.environ.get("FORGEJO_TEST_CREDENTIAL_FILE")
    if not credential_file:pytest.skip("requires verified signed confined local Forgejo fixture")
    fixture_credentials=json.loads(Path(credential_file).read_text())
    credentials={key:fixture_credentials[key] for key in ("user_name","password")}
    from fastapi import FastAPI
    from src.api import auth,forgejo,goals
    from src.browser.forgejo_profile import ForgejoTitleBrowser
    from src.integrations.forgejo_controls import ForgejoService
    root,db_engine,factory=accounting_db;os.chmod(root,0o700)
    monkeypatch.setattr(settings,"operator_auth_allow_unauthenticated_tests",False)
    monkeypatch.setattr(settings,"operator_auth_secret","forgejo-native-test-root")
    monkeypatch.setattr(settings,"operator_auth_secret_hash","")
    monkeypatch.setattr(settings,"operator_auth_allowed_hosts","test,localhost,127.0.0.1")
    monkeypatch.setattr(settings,"operator_auth_allowed_origins","http://localhost:3001")
    monkeypatch.setattr(settings,"operator_auth_cookie_secure",False)
    auth._reset_login_throttle_for_tests()
    transport=ActualLoopback(int(os.environ.get("FORGEJO_TEST_TCP_PORT","35251")))
    service=ForgejoService(browser=ForgejoTitleBrowser(local_transport=transport,resolver=lambda h,p:["1.1.1.1"]))
    monkeypatch.setattr(forgejo,"forgejo_service",service)
    app=FastAPI();app.add_middleware(OperatorAuthMiddleware)
    app.include_router(auth.router,prefix="/api/auth");app.include_router(goals.router,prefix="/api")
    app.include_router(forgejo.router,prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test",
        headers={"origin":"http://localhost:3001"}) as client:
        login=await client.post("/api/auth/login",json={"password":"forgejo-native-test-root"})
        assert login.status_code==200
        goal=await client.post("/api/goals",json={"title":"Exactly approve one confined local issue title"})
        assert goal.status_code==200,goal.text
        goal=goal.json()
        async with factory.accounting_sessions() as db:
            goal_row=await db.get(Goal,goal["id"]);principal=goal_row.owner_principal_id
        await vault_repository.store("forgejo-native-input",json.dumps(credentials),owner_principal_id=principal)
        configured=await client.put("/api/capabilities/forgejo/connection",json={"vault_key":"forgejo-native-input","expected_revision":0})
        assert configured.status_code==200,configured.text
        consent=await client.put("/api/capabilities/forgejo/connection/read-consent",
            json={"expected_revision":1,"duration_seconds":900,"read_ack":True})
        assert consent.status_code==200,consent.text
        common={"goal_id":goal["id"],"goal_revision":goal["revision"],"expected_revision":1}
        async def prepare(operation,**kwargs):
            response=await client.post("/api/capabilities/forgejo/jobs",
                json={**common,"operation":operation,"request_key":str(uuid.uuid4()),**kwargs})
            assert response.status_code==200,response.text
            return response.json()
        async def execute(job):
            response=await client.post("/api/capabilities/forgejo/jobs/"+job["job_id"]+"/execute",
                json={"expected_revision":job["revision"],"fencing_token":job["lease"]["fencing_token"]})
            assert response.status_code==200,response.text
            assert response.json()["status"]=="succeeded",response.text
            return response.json()
        provision=await execute(await prepare("provision"))
        assert provision["no_learning"] and len(provision["forgejo"]["calls"])==4
        preview=await execute(await prepare("preview",fields={"owner":credentials["user_name"],
            "repository":"fixture925","issue_index":1,"new_title":"Approved native summary"}))
        read=await client.get("/api/capabilities/forgejo/jobs/"+preview["job_id"]+"/output")
        assert read.status_code==200,read.text
        from src.browser.forgejo_issue_title import digest
        title=await prepare("title",preview_job_id=preview["job_id"],preview_digest=digest(read.json()))
        approved=await client.post("/api/capabilities/forgejo/jobs/"+title["job_id"]+"/approve",
            json={"approval_id":title["approval"]["id"],"decision":"approved","exact_ack":True})
        assert approved.status_code==200,approved.text
        title=await execute(approved.json())
        assert title["forgejo"]["cleanup"]["status"]=="verified"
        assert len([c for c in title["forgejo"]["calls"] if c["operation"]=="title_submission"])==1
        result=await client.get("/api/capabilities/forgejo/jobs/"+title["job_id"]+"/output")
        assert result.status_code==200,result.text
        assert result.json()["readback_title"]=="Approved native summary"
        assert result.json()["matching_title_event_ids"] and result.json()["no_learning"]
        assert len([v for v in transport.requests if v[0]=="POST" and v[1].endswith("/title")])==1
        # Retained receipt contains only canonical nonsecret fields, never
        # response headers/auth cookies, Vault values or full provider HTML.
        (root/"actual-native-receipt.json").write_text(json.dumps({"goal":goal,
            "provision":provision,"preview":preview,"title":title,"output":result.json()},indent=2))
        os.chmod(root/"actual-native-receipt.json",0o600)
