"""Actual compiled cockpit -> real Auth/API/database -> signed native provider.

Only execute under the independently reviewed disposable local fixture group.
No substituted UI DOM, route JSON, provider HTML or native submitter.
"""
import hashlib
import json
import mimetypes
import os
from pathlib import Path
from urllib.parse import urlsplit
import uuid

import httpx
import pytest

from tests.test_inference_accounting import accounting_db
from tests.test_forgejo_form_journeys_actual import FormLoopback


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["forgejo.issue-create.v1", "forgejo.issue-comment.v1"])
async def test_actual_built_ui_form_journey(accounting_db, monkeypatch, profile):
    credential_file = os.environ.get("FORGEJO_TEST_CREDENTIAL_FILE")
    if not credential_file:
        pytest.skip("requires separately reviewed signed confined fixture")
    credentials = json.loads(Path(credential_file).read_text())
    from config.settings import settings
    from fastapi import FastAPI
    from starlette.middleware.cors import CORSMiddleware
    from playwright.async_api import BrowserType, async_playwright, expect
    from src.api import auth, forgejo, goals, vault
    from src.auth.middleware import OperatorAuthMiddleware
    from src.browser.forgejo_profile import ForgejoTitleBrowser
    from src.db.models import Goal
    from src.integrations.forgejo_controls import ForgejoService
    from src.vault.repository import vault_repository

    launch = BrowserType.launch
    async def confined_launch(self, *args, **kwargs):
        kwargs["executable_path"] = str(Path(credential_file).parent.parent / "forgejo-chromium-confined.py")
        return await launch(self, *args, **kwargs)
    monkeypatch.setattr(BrowserType, "launch", confined_launch)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/home/pawel/.cache/ms-playwright/chromium_headless_shell-1208")
    root, db_engine, factory = accounting_db
    os.chmod(root, 0o700)
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "ui-form-disposable-root")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "localhost,127.0.0.1")
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", "http://localhost:3001")
    monkeypatch.setattr(settings, "operator_auth_cookie_secure", False)
    auth._reset_login_throttle_for_tests()
    provider = FormLoopback(int(os.environ["FORGEJO_TEST_TCP_PORT"]))
    def service():
        return ForgejoService(browser=ForgejoTitleBrowser(local_transport=provider, resolver=lambda h,p:["1.1.1.1"]))
    monkeypatch.setattr(forgejo, "forgejo_service", service())
    app = FastAPI(); app.add_middleware(OperatorAuthMiddleware)
    app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:3001"],
                       allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
    app.include_router(auth.router, prefix="/api/auth")
    for router in (goals.router, vault.router, forgejo.router):
        app.include_router(router, prefix="/api")
    asgi = httpx.ASGITransport(app=app)
    dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    assert (dist / "index.html").is_file(), "actual built frontend required"
    bundle_hashes = {str(p.relative_to(dist)):hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in dist.rglob("*") if p.is_file()}
    local_calls = []; rejected = []
    async def intercept(route):
        request = route.request
        url = urlsplit(request.url)
        if url.scheme != "http" or url.hostname != "localhost" or url.port not in (3001,8004):
            rejected.append(request.url)
            await route.abort(); return
        if url.port == 8004 and url.path.startswith("/api/"):
            # The browser's actual cookie, Origin, pinned Root header and body
            # reach the real middleware. No HTTPX cookie jar can add authority.
            headers = await request.all_headers()
            response = await asgi.handle_async_request(httpx.Request(request.method, request.url,
                headers=headers, content=request.post_data_buffer or b""))
            body = await response.aread()
            local_calls.append({"method":request.method,"path":url.path,"status":response.status_code})
            result_headers = dict(response.headers)
            result_headers.pop("content-length",None)
            await route.fulfill(status=response.status_code, headers=result_headers, body=body)
            await response.aclose(); return
        if url.port == 3001:
            relative = url.path.lstrip("/") or "index.html"
            asset = (dist / relative).resolve()
            if dist.resolve() not in asset.parents or not asset.is_file():
                await route.fulfill(status=404, body="Actual local build asset absent"); return
            assert hashlib.sha256(asset.read_bytes()).hexdigest() == bundle_hashes[relative]
            await route.fulfill(status=200, content_type=mimetypes.guess_type(str(asset))[0] or "application/octet-stream", body=asset.read_bytes())
            return
        rejected.append(request.url); await route.abort()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(java_script_enabled=True, service_workers="block")
        await context.route("**/*", intercept)
        await context.route_web_socket("**/*", lambda websocket:websocket.close())
        page = await context.new_page()
        page.set_default_timeout(10000)
        try:
            await page.goto("http://localhost:3001/")
            await page.get_by_label("Operator password", exact=True).fill("ui-form-disposable-root")
            await page.get_by_role("button", name="Sign in", exact=True).click()
            await expect(page.get_by_label("Operator password", exact=True)).to_have_count(0)
            cookie = "; ".join(c["name"]+"="+c["value"] for c in await context.cookies("http://localhost:8004"))
            assert cookie, "Root cookie must come from actual browser login"
            async with httpx.AsyncClient(transport=asgi, base_url="http://localhost:8004",
                headers={"origin":"http://localhost:3001","cookie":cookie}) as setup:
                response = await setup.post("/api/auth/ownership/enroll")
                assert response.status_code == 200, response.text
                response = await setup.post("/api/goals",json={"title":"UI exact ordinary form owned Goal"})
                assert response.status_code == 200, response.text
                goal = response.json()
                async with factory.accounting_sessions() as db:
                    principal = (await db.get(Goal, goal["id"])).owner_principal_id
                await vault_repository.store("ui-form-disposable-input",json.dumps(credentials),owner_principal_id=principal)
                await page.reload()
                async def open_forms():
                    await page.get_by_role("button",name="Settings",exact=True).click()
                    await page.get_by_role("button",name="Forgejo",exact=True).click()
                    return page.get_by_role("region",name="Forgejo exact issue forms")
                form = await open_forms()
                await page.get_by_label("Forgejo credential Vault key").select_option("ui-form-disposable-input")
                await page.get_by_role("button",name="Configure fixed site",exact=True).click()
                await page.get_by_label("Forgejo finite Goal").select_option(goal["id"])
                read_ack = page.get_by_label("Acknowledge Forgejo finite private reads")
                await expect(read_ack).not_to_be_checked()
                await read_ack.check()
                await page.get_by_role("button",name="Grant finite read consent",exact=True).click()
                await page.get_by_role("button",name="Prepare backend session job",exact=True).click()
                await page.get_by_role("button",name="Run original job once",exact=True).click()
                await page.get_by_role("button",name="Read current state",exact=True).click()
                await form.get_by_label(profile,exact=True).check()
                activation = form.get_by_label("Acknowledge reviewed Forgejo form profiles")
                await expect(activation).not_to_be_checked()
                before = len(provider.requests)
                await activation.check()
                await form.get_by_role("button",name="Save reviewed form availability",exact=True).click()
                await expect(activation).not_to_be_checked()
                assert len(provider.requests) == before
                await form.get_by_label("Forgejo exact form operation").select_option(profile)
                await form.get_by_label("Forgejo form repository owner",exact=True).fill(credentials["user_name"])
                await form.get_by_label("Forgejo form repository",exact=True).fill("fixture1013")
                title = "UI literal title "+uuid.uuid4().hex[:8]
                if profile.endswith("create.v1"):
                    await form.get_by_label("Forgejo form title").fill(title)
                else:
                    await form.get_by_label("Forgejo form issue number").fill("1")

                async def approve_body(body):
                    await form.get_by_label("Forgejo form body").fill(body)
                    await form.get_by_role("button",name="Prepare exact form preview",exact=True).click()
                    await form.get_by_role("button",name="Run exact form job once",exact=True).click()
                    await form.get_by_role("button",name="Read protected form preview or receipt",exact=True).click()
                    preview = form.get_by_label("Protected literal Forgejo form preview")
                    await expect(preview).to_contain_text(body)
                    await expect(preview).to_contain_text(credentials["user_name"]+"/fixture1013")
                    await form.get_by_role("button",name="Prepare approval for this saved form",exact=True).click()
                    ack = form.get_by_label("Acknowledge exact Forgejo form effect")
                    await expect(ack).not_to_be_checked()
                    await expect(form.get_by_role("button",name="Approve saved exact form once",exact=True)).to_be_disabled()
                    await expect(form.get_by_role("button",name="Run exact form job once",exact=True)).to_be_disabled()
                    await ack.check()
                    await form.get_by_role("button",name="Approve saved exact form once",exact=True).click()

                body = "UI exact literal body "+uuid.uuid4().hex
                await approve_body(body)
                def form_posts():
                    return sum(method=="POST" and (path.endswith("/issues/new") or path.endswith("/comments"))
                               for method,path in provider.requests)
                before_post = form_posts()
                await form.get_by_role("button",name="Run exact form job once",exact=True).click()
                await form.get_by_role("button",name="Read protected form preview or receipt",exact=True).click()
                receipt = form.get_by_label("Verified exact Forgejo form receipt")
                await expect(receipt).to_contain_text(body)
                await expect(receipt).to_contain_text("Verified numeric ID")
                await expect(receipt).to_contain_text("no_learning")
                after_post = form_posts()
                assert after_post == before_post+1
                success_text = await receipt.inner_text()
                # A committed effect with a lost response remains Unknown in the
                # actual UI, and reload/restart cannot repeat it.
                await approve_body("UI lost response body "+uuid.uuid4().hex)
                provider.lose_form_response = True
                await form.get_by_role("button",name="Run exact form job once",exact=True).click()
                await form.get_by_role("button",name="Inspect exact form history",exact=True).click()
                await expect(form.get_by_label("Exact Forgejo form job")).to_contain_text("unknown_external_effect")
                assert form_posts() == after_post+1
                await expect(form.get_by_role("button",name="Run exact form job once",exact=True)).to_be_disabled()
                await expect(form.get_by_role("button",name="Prepare exact-ID GET-only recovery",exact=True)).to_have_count(0)
                before_reload = len(provider.requests)
                monkeypatch.setattr(forgejo,"forgejo_service",service())
                # Close the pool actually bound to the fixture's session factory.
                # Reload opens new sessions on the same file in this process.
                await db_engine.dispose()
                await page.reload()
                form = await open_forms()
                await expect(form.get_by_label("Exact Forgejo form job")).to_contain_text("unknown_external_effect")
                assert len(provider.requests) == before_reload
                await expect(form.get_by_role("button",name="Run exact form job once",exact=True)).to_be_disabled()
                assert not rejected, rejected
                outcome = {"profile":profile,"actual_build_hashes":bundle_hashes,"actual_local_api_calls":local_calls,
                    "success_ui":success_text,"unknown_ui":await form.inner_text(),"provider_requests":provider.requests,
                    "reload_restart_provider_contacts":0,"native_success_posts":1,"no_learning":True}
                path = root/(profile+".actual-ui-journey.json")
                path.write_text(json.dumps(outcome,indent=2)); os.chmod(path,0o600)
        finally:
            await context.close(); await browser.close(); await asgi.aclose()
