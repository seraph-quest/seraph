"""Rendered production UI -> real finite-auth ASGI -> file SQLite receipt.

No response fixtures: Playwright mechanically forwards API requests to the
owning app. Only public source and GitHub transport boundaries are intercepted.
The managed host backend is deliberately never used for fixture API traffic.
"""
from datetime import datetime, timedelta, timezone
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from sqlalchemy import select

from tests.test_attention_recovery_journey import (
    async_db, authenticated_setup_operator, setup_workspace,
    explicit_test_external_permission, forbid_unintercepted_http,
    _prepare_actual_github_task,
)
from config.settings import settings
from src.app import create_app
from src.db.models import ApprovalRequest, OperatorSession, WorkBoardAttempt, WorkBoardTask
from src.extensions.github_followthrough import GitHubFollowthroughService
from src.work_board.dispatcher import WorkBoardDispatcher
from src.workflows.job_runtime import durable_job_repository

RECEIPTS = Path("/tmp/seraph-915-browser-receipts")
FRONTEND = "http://127.0.0.1:13001"


@pytest.mark.skipif(os.environ.get("SERAPH_RUN_REAL_ATTENTION_BROWSER") != "1", reason="explicit managed-frontend/Chromium acceptance proof")
@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["verified", "deny", "cancel", "expire", "expired_root"])
async def test_rendered_attention_actual_persisted_task(client, async_db, setup_workspace, monkeypatch, decision):
    from playwright.async_api import async_playwright, expect
    RECEIPTS.mkdir(exist_ok=True)
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", FRONTEND + ",http://localhost:3001")
    monkeypatch.setattr("src.memory.m5.get_session", async_db)
    app = create_app()
    remote, external, api, websocket = {}, [], [], []
    fail_readback = [True]
    async def github_transport(request):
        external.append({"method": request.method, "path": request.url.path})
        if request.method == "POST":
            assert sum(row["method"] == "POST" for row in external) == 1
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
    attempt_id = task["latest_attempt"]["attempt_id"]
    errors = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 1000}, timezone_id="Europe/Warsaw")
        # Playwright 1.58's route implementation indexes omitted close-event
        # fields. Supply ordinary cleanup defaults only; no data frames or
        # server authority are synthesized by this browser tooling shim.
        await context.add_init_script("const originalClose = WebSocket.prototype.close; WebSocket.prototype.close = function(code = 1000, reason = '') { return originalClose.call(this, code, reason); };")
        # A genuine finite session cookie returned by /api/auth/login, never a
        # forged principal or auth bypass. Domain changes only bind the ASGI
        # test hostname to the browser's actual loopback hostname.
        await context.add_cookies([{"name": cookie.name, "value": cookie.value, "domain": "127.0.0.1", "path": cookie.path, "httpOnly": True, "secure": False, "sameSite": "Lax"} for cookie in client.cookies.jar])
        async def bridge(route):
            request = route.request
            headers = await request.all_headers()
            # Preserve browser method, URL, Origin, cookies and request bytes.
            # New clients ensure no setup cookie can silently replace an absent
            # browser cookie. Response bytes/status/headers are unmodified.
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as forwarding:
                response = await forwarding.request(request.method, request.url, headers=headers, content=request.post_data_buffer)
            api.append({"method": request.method, "path": request.url.split("/api", 1)[1], "status": response.status_code})
            await route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.content)
        await context.route("**/api/**", bridge)
        socket_tasks = []
        async def socket_bridge(socket):
            # Run the actual ASGI websocket endpoint. Its emitted frames and
            # origin/cookie checks remain authoritative, not harness fixtures.
            url = urlsplit(socket.url)
            stats = {"path": url.path, "connect": 1, "incoming": 0, "outgoing": 0, "accepted": 0, "closed": 0}
            websocket.append(stats)
            cookies = await context.cookies(socket.url.replace("ws://", "http://"))
            queue = asyncio.Queue()
            await queue.put({"type": "websocket.connect"})
            socket.on_message(lambda message: queue.put_nowait({"type": "websocket.receive", "text" if isinstance(message, str) else "bytes": message}))
            # Do not register Playwright 1.58's close callback: Chromium can
            # omit its optional fields while closing before connect. All real
            # ASGI tasks are cancelled and awaited in the finally block.
            async def send(message):
                if message["type"] == "websocket.send":
                    stats["outgoing"] += 1
                    socket.send(message.get("text", message.get("bytes")))
                elif message["type"] == "websocket.accept":
                    stats["accepted"] += 1
                elif message["type"] == "websocket.close":
                    stats["closed"] += 1
                    await socket.close(code=message.get("code", 1000), reason=message.get("reason", ""))
            async def receive():
                message = await queue.get()
                stats["incoming"] += message["type"] == "websocket.receive"
                return message
            scope = {"type": "websocket", "asgi": {"version": "3.0", "spec_version": "2.4"}, "scheme": "ws", "path": url.path, "raw_path": url.path.encode(), "query_string": url.query.encode(), "root_path": "", "headers": [(b"host", url.netloc.encode()), (b"origin", FRONTEND.encode()), (b"cookie", "; ".join(f"{cookie['name']}={cookie['value']}" for cookie in cookies).encode())], "client": ("127.0.0.1", 23456), "server": (url.hostname, url.port), "subprotocols": []}
            socket_tasks.append(asyncio.create_task(app(scope, receive, send)))
        # Leave Vite's development HMR transport attached to its managed server.
        await context.route_web_socket("**/ws/**", socket_bridge)
        page = await context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        async def confirm_action(dialog):
            assert dialog.type == "confirm" and dialog.message == f"Confirm cancel for {task['title']}?"
            await dialog.accept()
        page.on("dialog", confirm_action)
        async def refresh_task():
            await page.get_by_role("button", name="Close task details", exact=True).click()
            await page.get_by_role("button", name="Refresh board", exact=True).click()
            await page.get_by_role("button", name=f"Open task {task['title']}", exact=True).click()
        try:
            await page.goto(FRONTEND, wait_until="domcontentloaded")
            await expect(page.locator("body")).to_contain_text("Needs attention", timeout=30000)
            assert not await page.locator("vite-error-overlay").count()
            attention = page.locator("[data-attention-id]").filter(has_text=task["title"])
            await expect(attention).to_have_count(1, timeout=30000)
            await attention.click()
            approve = page.get_by_role("button", name="Approve exact action", exact=True)
            await expect(approve).to_be_enabled(timeout=30000)
            await page.screenshot(path=str(RECEIPTS / f"{decision}-pending.png"), full_page=True)
            if decision == "verified":
                async with page.expect_response(lambda response: response.url.endswith(f"/api/approvals/{approval_id}/approve")) as approval_response:
                    await approve.click()
                assert (await approval_response.value).status == 200
                async with async_db() as db:
                    assert (await db.get(ApprovalRequest, approval_id)).status == "approved"
                assert external == []
                await dispatcher.run_pass()
                unknown = (await client.get(f"/api/work-board/tasks/{task['task_id']}")).json()["task"]
                assert unknown["block_kind"] == "unknown_effect"
                assert unknown["latest_attempt"]["attempt_id"] == attempt_id
                await async_db.engine.dispose()
                # Recreate app plus engine reconnect over the same database.
                app = create_app()
                await refresh_task()
                reconcile = page.get_by_role("button", name="Reconcile recorded GitHub effect", exact=True)
                await expect(reconcile).to_be_enabled(timeout=15000)
                await page.screenshot(path=str(RECEIPTS / "verified-restarted-unknown.png"), full_page=True)
                fail_readback[0] = False
                before = len(external)
                async with page.expect_response(lambda response: response.url.endswith(f"/api/capabilities/github/jobs/{job['job_id']}/reconcile")) as reconciled_response:
                    await reconcile.click()
                reconciled = await reconciled_response.value
                assert reconciled.status == 200 and (await reconciled.json())["status"] == "succeeded"
                assert external[before:] == [{"method": "GET", "path": "/repos/example/repo/issues/481"}]
                await WorkBoardDispatcher(session_provider=async_db).run_pass()
                await refresh_task()
                await expect(page.get_by_text("Current state: Done", exact=True)).to_be_visible(timeout=15000)
            elif decision == "deny":
                async with page.expect_response(lambda response: response.url.endswith(f"/api/approvals/{approval_id}/deny")) as deny_response:
                    await page.get_by_role("button", name="Deny exact action", exact=True).click()
                assert (await deny_response.value).status == 200
                async with async_db() as db:
                    assert (await db.get(ApprovalRequest, approval_id)).status == "denied"
                await dispatcher.run_pass()
            elif decision == "cancel":
                async with page.expect_response(lambda response: response.url.endswith(f"/api/work-board/tasks/{task['task_id']}/actions")) as cancel_response:
                    await page.get_by_role("button", name="Request durable cancellation", exact=True).click()
                assert (await cancel_response.value).status == 200
                await dispatcher.run_pass()
            elif decision == "expire":
                async with async_db() as db:
                    row = await db.get(ApprovalRequest, approval_id)
                    row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                    db.add(row)
                await page.get_by_role("button", name="Refresh exact approval", exact=True).click()
                await expect(approve).to_be_disabled(timeout=15000)
                await expect(page.get_by_text("No exact current-attempt approval is pending", exact=False)).to_be_visible()
                await dispatcher.run_pass()
            else:
                async with async_db() as db:
                    row = await db.get(OperatorSession, auth["session_id"])
                    row.idle_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                    db.add(row)
                await page.get_by_role("button", name="Refresh exact approval", exact=True).click()
                await expect(page.get_by_label("Operator password")).to_be_visible(timeout=15000)
                assert not await page.get_by_role("button", name="Approve exact action", exact=True).count()
                assert any(row["status"] == 401 for row in api)
                await dispatcher.run_pass()
            await async_db.engine.dispose()
            canonical = await durable_job_repository.get_job(job["job_id"])
            async with async_db() as db:
                attempts = (await db.execute(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == task["task_id"]))).scalars().all()
                stored_task = (await db.execute(select(WorkBoardTask).where(WorkBoardTask.task_id == task["task_id"]))).scalar_one()
            assert len(attempts) == 1 and attempts[0].attempt_id == attempt_id
            if decision == "verified":
                confirmed = (await client.get(f"/api/work-board/tasks/{task['task_id']}")).json()["task"]
                assert confirmed["status"] == "done" and confirmed["readback_status"] == "verified"
                assert canonical["status"] == "succeeded"
                assert sum(row["method"] == "POST" for row in external) == 1
            elif decision == "expired_root":
                assert external == [] and canonical["status"] == "awaiting_approval" and canonical["effects"] == []
                assert stored_task.status.value != "done"
            else:
                assert external == [] and canonical["status"] == "cancelled" and canonical["effects"] == []
            await page.screenshot(path=str(RECEIPTS / f"{decision}-final.png"), full_page=True)
            assert errors == []
            (RECEIPTS / f"{decision}.json").write_text(json.dumps({"decision": decision, "task_id": task["task_id"], "task_status": stored_task.status.value, "task_readback_status": confirmed["readback_status"] if decision == "verified" else None, "job_id": job["job_id"], "attempt_id": attempt_id, "approval_id": approval_id, "session_id": auth["session_id"], "database": str(async_db.engine.url), "job_status": canonical["status"], "effects": canonical["effects"], "api": api, "websocket": websocket, "external": external, "page_errors": errors, "bridge": "actual ASGI HTTP responses and WebSocket data frames, finite real login cookie; not managed-host API acceptance. Playwright 1.58 close-field compatibility shim supplies cleanup code=1000/reason='' only."}, indent=2, default=str))
        finally:
            (RECEIPTS / f"{decision}-diagnostic.json").write_text(json.dumps({"api": api, "websocket": websocket, "external": external, "page_errors": errors, "body": await page.locator("body").inner_text(), "socket_errors": [str(pending.exception()) for pending in socket_tasks if pending.done() and not pending.cancelled()]}, indent=2, default=str))
            await page.screenshot(path=str(RECEIPTS / f"{decision}-diagnostic.png"), full_page=True)
            await context.close()
            await browser.close()
            for pending in socket_tasks:
                pending.cancel()
            await asyncio.gather(*socket_tasks, return_exceptions=True)
