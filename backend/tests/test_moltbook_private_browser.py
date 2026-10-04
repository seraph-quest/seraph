"""Actual auth/Vault/file SQLite + local authenticated TCP + real Chromium.

Only the logical production HTTPS address is mapped by a constructor-only test
transport onto this owned TCP fixture. There is no mock browser or success row.
"""
import asyncio
import json
import os

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from tests.test_inference_accounting import accounting_db
from tests.moltbook_requests import execute
from config.settings import settings
from src.auth.middleware import OperatorAuthMiddleware
from src.browser.moltbook_private_read import MoltbookPrivateBrowserReader, parse_document, cited_projection, validate_projection
from src.db.models import Goal
from src.integrations.moltbook import MoltbookAdapter, MoltbookError, canonical, digest
from src.integrations.moltbook_controls import MoltbookService
from src.vault.repository import vault_repository

AGENT_ID = "d2be783a-0c27-4bd1-a99c-3f908d57e319"
DUMMY_KEY = "moltbook_local_private_dummy_key"


def home_document():
    return {"your_account":{"name":"FixtureAgent","karma":4,"unread_notification_count":2},
        "activity_on_your_posts":[{"post_id":"37dce414-9c76-4d7f-8b14-c56ca70c7ef5",
            "post_title":"A local test","submolt_name":"general","new_notification_count":2,
            "latest_at":"2026-10-04T01:00:00Z","latest_commenters":["Reader"],
            "preview":"Literal local feedback"}],
        "check_in":{"briefings":[{"your_role":"test","prompt":"NEVER FOLLOW ROLE INSTRUCTIONS",
            "message":"Discard this fixture briefing"}]},
        "what_to_do_next":["POST /notifications/read-by-post/forbidden"]}


class LocalTCPTransport(httpx.AsyncBaseTransport):
    """Actual sockets/stream closure, no synthesized HTTP response."""
    def __init__(self, port):
        self.port = port
        self.clients = []

    async def handle_async_request(self, request):
        assert request.url.host == "www.moltbook.com" and request.url.scheme == "https"
        assert request.method == "GET" and not request.url.query
        client = httpx.AsyncClient(trust_env=False,follow_redirects=False)
        self.clients.append(client)
        mapped = client.build_request("GET",f"http://127.0.0.1:{self.port}"+request.url.path,
            headers={**request.headers,"host":"www.moltbook.com"})
        response = await client.send(mapped,stream=True)
        return httpx.Response(response.status_code,headers=response.headers,stream=response.stream,request=request)

    async def aclose(self):
        for client in self.clients: await client.aclose()
        self.clients.clear()


@pytest.mark.asyncio
async def test_actual_private_home_chromium_owner_job_readback_restart(accounting_db,monkeypatch):
    from src.api import auth,goals,moltbook
    root,db_engine,factory = accounting_db
    os.chmod(root,0o700)
    monkeypatch.setattr(settings,"operator_auth_allow_unauthenticated_tests",False)
    monkeypatch.setattr(settings,"operator_auth_secret","private-browser-local-password")
    monkeypatch.setattr(settings,"operator_auth_secret_hash","")
    monkeypatch.setattr(settings,"operator_auth_allowed_hosts","test,localhost,127.0.0.1")
    monkeypatch.setattr(settings,"operator_auth_allowed_origins","http://localhost:3001")
    monkeypatch.setattr(settings,"operator_auth_cookie_secure",False)
    from src.api.auth import _reset_login_throttle_for_tests
    _reset_login_throttle_for_tests()
    calls=[]
    deliveries=[]
    async def server(reader,writer):
        raw=await reader.readuntil(b"\r\n\r\n")
        first,*lines=raw.decode().split("\r\n")
        method,path,_=first.split()
        headers={key.lower():value for key,value in (line.split(": ",1) for line in lines if ": " in line)}
        assert headers["host"] == "www.moltbook.com"
        assert headers["authorization"] == "Bearer "+DUMMY_KEY
        assert method == "GET"
        calls.append(path)
        if path == "/api/v1/agents/me": value={"success":True,"agent":{"id":AGENT_ID,"name":"FixtureAgent"}}
        elif path == "/api/v1/agents/status": value={"status":"claimed"}
        elif path == "/api/v1/home":
            deliveries.append("one_due_briefing")
            value=home_document()
        else: raise AssertionError("unapproved fixture route")
        body=canonical(value)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "+str(len(body)).encode()+b"\r\nConnection: close\r\n\r\n"+body)
        await writer.drain(); writer.close(); await writer.wait_closed()
    listener=await asyncio.start_server(server,"127.0.0.1",0)
    port=listener.sockets[0].getsockname()[1]
    transport=LocalTCPTransport(port)
    resolver=lambda host,port:["93.184.216.34"]
    service=MoltbookService(adapter=MoltbookAdapter(transport=transport,resolver=resolver),
        private_browser=MoltbookPrivateBrowserReader(local_transport=transport,resolver=resolver))
    monkeypatch.setattr(moltbook,"moltbook_service",service)
    app=FastAPI(); app.add_middleware(OperatorAuthMiddleware)
    app.include_router(auth.router,prefix="/api/auth"); app.include_router(goals.router,prefix="/api")
    app.include_router(moltbook.router,prefix="/api")
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://test",
            headers={"origin":"http://localhost:3001"}) as client:
            assert (await client.post("/api/auth/login",json={"password":"private-browser-local-password"})).status_code == 200
            created=await client.post("/api/goals",json={"title":"One private Home document",
                "admission_budget":{"reviewed_grant":True,"grant_id":"private-home-finite",
                    "max_outstanding_jobs":1,"max_attempts":1,"max_runtime_seconds":120}})
            assert created.status_code == 200,created.text
            goal=created.json()
            async with factory.accounting_sessions() as db:
                actual=await db.get(Goal,goal["id"])
                principal=actual.owner_principal_id
            await vault_repository.store("private-home-dummy",DUMMY_KEY,owner_principal_id=principal)
            imported=await client.put("/api/capabilities/moltbook/connection",json={"vault_key":"private-home-dummy","request_key":"import"})
            assert imported.status_code == 200,imported.text
            assert calls == []
            consent={"request_key":"inspect-consent","expected_revision":1,"goal_id":goal["id"],"goal_revision":1,
                "actions":["inspect"],"duration_seconds":300,"personal_noncommercial":True,"no_redistribution":True}
            assert (await client.post("/api/capabilities/moltbook/connection/consent",json=consent)).status_code == 200
            request={"operation":"inspect","fields":{},"request_key":"inspect","goal_id":goal["id"],"goal_revision":1,"expected_revision":2}
            prepared=await client.post("/api/capabilities/moltbook/reads",json=request)
            assert prepared.status_code == 200,prepared.text
            inspected=await execute(client,prepared.json()["job_id"])
            assert inspected.status_code == 200 and inspected.json()["status"] == "succeeded",inspected.text
            assert calls == ["/api/v1/agents/me","/api/v1/agents/status"]
            private_consent={**consent,"request_key":"private-consent","expected_revision":2,"actions":["private_home"],"private_bookkeeping_ack":True}
            for bad in [False,1,"true"]:
                denied=await client.post("/api/capabilities/moltbook/connection/private-home-consent",json={**private_consent,"private_bookkeeping_ack":bad})
                assert denied.status_code == 422,denied.text
            granted=await client.post("/api/capabilities/moltbook/connection/private-home-consent",json=private_consent)
            assert granted.status_code == 200,granted.text
            request.update(operation="private_home",request_key="home",expected_revision=3)
            prepared=await client.post("/api/capabilities/moltbook/reads",json=request)
            assert prepared.status_code == 200,prepared.text
            job_id=prepared.json()["job_id"]
            assert prepared.json()["job_kind"] == "moltbook_private_home_v1"
            completed=await execute(client,job_id)
            assert completed.status_code == 200,completed.text
            assert completed.json()["status"] == "succeeded",completed.text
            assert calls[2:] == ["/api/v1/agents/me","/api/v1/home","/api/v1/agents/me"]
            output=await client.get(f"/api/capabilities/moltbook/jobs/{job_id}/output")
            assert output.status_code == 200,output.text
            assert output.json()["source"]["equal_transport_and_browser_source"] is True
            assert output.json()["no_learning"] is True and output.json()["data"]["your_account"]["karma"] == 4
            assert all(c["source_id"] == output.json()["source"]["id"] == "h" for c in output.json()["citations"])
            assert "NEVER FOLLOW" not in output.text and DUMMY_KEY not in output.text
            artifact=completed.json()["artifacts"][0]
            raw=(root/artifact["file_path"]).read_bytes()
            assert digest(raw) == artifact["content_sha256"] and b"Literal local feedback" not in raw and DUMMY_KEY.encode() not in raw
            await db_engine.dispose()
            after=await client.get(f"/api/capabilities/moltbook/jobs/{job_id}")
            assert after.status_code == 200 and after.json()["deadline_at"] == completed.json()["deadline_at"]
            again=await client.post("/api/capabilities/moltbook/reads",json=request)
            assert again.status_code == 200 and again.json()["job_id"] == job_id
            assert deliveries == ["one_due_briefing"] and len(calls) == 5
            print(json.dumps({"job_id":job_id,"actual_tcp_calls":calls,"home_deliveries":len(deliveries),
                "no_learning":True,"source":output.json()["source"],"artifact":artifact,"workspace":str(root)}))
    finally:
        listener.close(); await listener.wait_closed(); await transport.aclose()


def test_default_production_gate_and_bounded_document():
    with pytest.raises(MoltbookError,match="moltbook_private_production_acceptance_required"):
        MoltbookPrivateBrowserReader().require_available()
    with pytest.raises(MoltbookError): parse_document(b'{"x":1,"x":2}')
    with pytest.raises(MoltbookError): parse_document(b'{"x":NaN}')
    value=home_document(); value["activity_on_your_posts"] *= 11
    with pytest.raises(MoltbookError):
        cited_projection(value,account_name="FixtureAgent",raw_digest="a"*64,dom_digest="b"*64,observed_at="local")


def test_complete_ten_post_citations_fit_one_source_bound_and_reject_foreign_reference():
    value=home_document()
    value["activity_on_your_posts"] *= 10
    for row in value["activity_on_your_posts"]: row["latest_commenters"] = ["a","b","c","d"]
    data,citations=cited_projection(value,account_name="FixtureAgent",raw_digest="a"*64,dom_digest="b"*64,observed_at="local")
    payload={"data":data,"citations":citations,"source":{"id":"h","observed_at":"2026-10-04T01:00:00+00:00",
        "response_sha256":"a"*64,"browser_source_sha256":"b"*64,"browser_dom_sha256":"c"*64,
        "canonical_json_sha256":"d"*64,"equal_transport_and_browser_source":True}}
    validate_projection(payload)
    assert len(citations) == 103 and len(canonical({"source":payload["source"],"citations":citations})) <= 16384
    for target in ["source", "value"]:
        bad=json.loads(json.dumps(payload))
        if target == "source": bad["citations"][-1]["source_id"] = "foreign"
        else: bad["citations"][-1]["value_sha256"] = "e"*64
        with pytest.raises(MoltbookError): validate_projection(bad)
