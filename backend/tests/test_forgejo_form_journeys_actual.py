"""Complete real native form journeys; require separately reviewed signed fixture."""
import json
import os
from pathlib import Path
import uuid

import httpx
import pytest

from tests.test_inference_accounting import accounting_db
from tests.test_forgejo_native_actual import ActualLoopback


class FormLoopback(ActualLoopback):
    lose_form_response = False

    async def handle_async_request(self, request):
        response = await super().handle_async_request(request)
        if self.lose_form_response and request.method == "POST" and (
            request.url.path.endswith("/issues/new") or request.url.path.endswith("/comments")
        ):
            self.lose_form_response = False
            await response.aread()
            await response.aclose()
            raise httpx.ReadError("deliberately lost actual committed local form response", request=request)
        return response


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["forgejo.issue-create.v1", "forgejo.issue-comment.v1"])
async def test_actual_signed_form_complete_journey(accounting_db, monkeypatch, profile):
    credential_file = os.environ.get("FORGEJO_TEST_CREDENTIAL_FILE")
    if not credential_file:
        pytest.skip("requires independently reviewed confined signed Forgejo 15.0.9")
    credentials = json.loads(Path(credential_file).read_text())
    from config.settings import settings
    from fastapi import FastAPI
    from playwright.async_api import BrowserType
    from src.api import auth, forgejo, goals
    from src.auth.middleware import OperatorAuthMiddleware
    from src.browser.forgejo_issue_title import digest
    from src.browser.forgejo_profile import ForgejoTitleBrowser
    from src.db.models import Goal
    from src.integrations.forgejo_controls import ForgejoService
    from src.vault.repository import vault_repository

    # Select the reviewed executable wrapper only. The current native runner,
    # Playwright browser/context, signed provider HTML and POST remain actual.
    launch = BrowserType.launch
    async def confined_launch(self, *args, **kwargs):
        kwargs["executable_path"] = str(Path(credential_file).parent.parent / "forgejo-chromium-confined.py")
        return await launch(self, *args, **kwargs)
    monkeypatch.setattr(BrowserType, "launch", confined_launch)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/home/pawel/.cache/ms-playwright/chromium_headless_shell-1208")
    root, db_engine, factory = accounting_db
    os.chmod(root, 0o700)
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "form-native-disposable-root")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "test,localhost,127.0.0.1")
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", "http://localhost:3001")
    monkeypatch.setattr(settings, "operator_auth_cookie_secure", False)
    auth._reset_login_throttle_for_tests()
    transport = FormLoopback(int(os.environ["FORGEJO_TEST_TCP_PORT"]))
    def service():
        return ForgejoService(browser=ForgejoTitleBrowser(local_transport=transport, resolver=lambda h,p:["1.1.1.1"]))
    monkeypatch.setattr(forgejo, "forgejo_service", service())
    app = FastAPI(); app.add_middleware(OperatorAuthMiddleware)
    app.include_router(auth.router, prefix="/api/auth")
    app.include_router(goals.router, prefix="/api")
    app.include_router(forgejo.router, prefix="/api")
    base = "/api/capabilities/forgejo"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
        headers={"origin":"http://localhost:3001"}) as client:
        assert (await client.post("/api/auth/login", json={"password":"form-native-disposable-root"})).status_code == 200
        assert (await client.post("/api/auth/ownership/enroll")).status_code == 200
        response = await client.post("/api/goals", json={"title":"Exactly review one native ordinary form"})
        assert response.status_code == 200, response.text
        goal = response.json()
        async with factory.accounting_sessions() as db:
            principal = (await db.get(Goal, goal["id"])).owner_principal_id
        await vault_repository.store("form-disposable-input", json.dumps(credentials), owner_principal_id=principal)
        configured = await client.put(base+"/connection", json={"vault_key":"form-disposable-input","expected_revision":0})
        assert configured.status_code == 200, configured.text
        assert configured.json()["reviewed_form_profile_ids"] == []
        assert (await client.put(base+"/connection/read-consent", json={"expected_revision":1,"duration_seconds":900,"read_ack":True})).status_code == 200
        common = {"goal_id":goal["id"],"goal_revision":goal["revision"],"expected_revision":1}
        async def prepare(operation, **kwargs):
            response = await client.post(base+"/jobs", json={**common,"operation":operation,"request_key":str(uuid.uuid4()),**kwargs})
            assert response.status_code == 200, response.text
            return response.json()
        async def execute(job):
            packet = {"expected_revision":job["revision"],"fencing_token":job["lease"]["fencing_token"]}
            response = await client.post(base+"/jobs/"+job["job_id"]+"/execute", json=packet)
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "succeeded", response.text
            return response.json(), packet
        provision, _ = await execute(await prepare("provision", fields={}))
        connection = (await client.get(base+"/connection")).json()
        before = len(transport.requests)
        activated = await client.put(base+"/connection/form-profiles", json={"expected_revision":connection["revision"],
            "expected_form_profiles_revision":connection["form_profiles_revision"],"profile_ids":[profile],"profile_ack":True})
        assert activated.status_code == 200, activated.text
        assert len(transport.requests) == before
        connection = activated.json(); common["expected_revision"] = connection["revision"]
        assert connection["browser_connections"][0]["read_scope"] == "forgejo_private_read"
        assert "password" not in json.dumps(connection)

        async def reviewed(body):
            fields = {"profile":profile,"owner":credentials["user_name"],"repository":"fixture1013","content":body}
            fields.update({"title":"Exact native title "+uuid.uuid4().hex[:8]} if profile.endswith("create.v1") else {"issue_index":1})
            preview, _ = await execute(await prepare("form-prepare", fields=fields))
            output = await client.get(base+"/jobs/"+preview["job_id"]+"/output")
            assert output.status_code == 200, output.text
            assert output.json()["target"]["content"] == body
            submit = await prepare("form-submit", fields={}, preview_job_id=preview["job_id"], preview_digest=digest(output.json()))
            before = len(transport.requests)
            unapproved = await client.post(base+"/jobs/"+submit["job_id"]+"/execute", json={
                "expected_revision":submit["revision"],"fencing_token":submit["lease"]["fencing_token"]})
            assert unapproved.status_code == 409 and len(transport.requests) == before
            pending = (await client.get(base+"/jobs/"+submit["job_id"])).json()
            assert pending["status"] == "accepted" and "execution_request" not in pending["forgejo"]
            ack = await client.post(base+"/jobs/"+submit["job_id"]+"/approve", json={
                "approval_id":submit["approval"]["id"],"decision":"approved","exact_ack":True})
            assert ack.status_code == 200, ack.text
            return ack.json(), output.json()

        approved, preview_output = await reviewed("Exact literal native body "+uuid.uuid4().hex)
        success, packet = await execute(approved)
        assert success["forgejo"]["cleanup"]["status"] == "verified"
        assert success["forgejo"]["capacity_closed"] is True
        assert sum(call["method"] == "POST" for call in success["forgejo"]["calls"]) == 1
        result = await client.get(base+"/jobs/"+success["job_id"]+"/output")
        assert result.status_code == 200 and result.json()["no_learning"] is True
        assert result.json()["readback_body"] == preview_output["target"]["content"]
        before = len(transport.requests)
        assert (await client.post(base+"/jobs/"+success["job_id"]+"/execute", json=packet)).json() == success
        assert len(transport.requests) == before

        loss, _ = await reviewed("Lost response actual native body "+uuid.uuid4().hex)
        loss_packet = {"expected_revision":loss["revision"],"fencing_token":loss["lease"]["fencing_token"]}
        transport.lose_form_response = True
        # The deliberate transport fault follows the actual signed provider POST.
        failed = await client.post(base+"/jobs/"+loss["job_id"]+"/execute", json=loss_packet)
        assert failed.status_code == 409, failed.text
        original = (await client.get(base+"/jobs/"+loss["job_id"])).json()
        assert original["status"] == "unknown_external_effect"
        assert original["forgejo"]["capacity_closed"] is False
        assert original["forgejo"]["cleanup"]["status"] == "verified"
        assert "exact_destination_id" not in original["forgejo"]
        before = len(transport.requests)
        # Close the pool actually bound to the fixture's session factory.
        # Subsequent sessions reopen the same file in this Python process.
        await db_engine.dispose()
        monkeypatch.setattr(forgejo, "forgejo_service", service())
        assert (await client.get(base+"/jobs/"+loss["job_id"])).json() == original
        assert (await client.post(base+"/jobs/"+loss["job_id"]+"/execute", json=loss_packet)).json() == original
        recovery = await client.post(base+"/jobs/"+loss["job_id"]+"/read-only-recovery", json={
            "expected_revision":connection["revision"],"original_job_revision":original["revision"],
            "original_fencing_token":original["lease"]["fencing_token"],"request_key":str(uuid.uuid4()),"read_ack":True})
        assert recovery.status_code == 409
        assert len(transport.requests) == before
        receipt = root/(profile+".actual-journey.json")
        receipt.write_text(json.dumps({"provision":provision,"success":success,"private_output":result.json(),
            "lost_response_unknown":original,"restart_contacts_unchanged":True,"no_learning":True}, indent=2))
        os.chmod(receipt, 0o600)
