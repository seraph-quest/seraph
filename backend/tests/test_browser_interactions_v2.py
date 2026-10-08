"""Actual local TCP + Chromium regression/security checks; no model calls."""
import asyncio
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import uuid

import httpx
import pytest
from pydantic import ValidationError

from config.settings import settings
from src.browser.interaction_contracts import BrowserActionV2, InteractionError, digest
from src.browser.task_runner import ProfiledInteractionPage
from src.security.http_transport import request_pinned_https
from tests.test_inference_accounting import accounting_db

FORM = b'''<!doctype html><html><body><form method="post" action="/post">
<label>Customer name: <input name="custname"></label>
<label>Telephone: <input type="tel" name="custtel"></label>
<label>E-mail address: <input type="email" name="custemail"></label>
<label>Small <input type="radio" name="size" value="small"></label>
<label>Large <input type="radio" name="size" value="large"></label>
<label>Cheese <input type="checkbox" name="topping" value="cheese"></label>
<label>Delivery time <select name="delivery"><option value="noon">Noon</option>
<option value="evening">Evening</option></select></label>
<label>Instructions <textarea name="comments"></textarea></label>
<button>Submit order</button></form>
<script>fetch('https://openrouter.ai/api/v1/chat/completions',{method:'POST'});</script>
</body></html>'''


class LocalTCP(httpx.AsyncBaseTransport):
    def __init__(self, port):
        self.port = port

    async def handle_async_request(self, request):
        assert request.headers["host"] == "httpbin.org"
        assert request.extensions["sni_hostname"] in ("httpbin.org", b"httpbin.org")
        assert request.method == "GET" and request.url.path == "/forms/post"
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.get(f"http://127.0.0.1:{self.port}/forms/post")
            return httpx.Response(response.status_code, content=response.content,
                headers=response.headers, request=request)


@pytest.fixture
def local_form(monkeypatch):
    contacts = []
    response_spec = {"body": FORM, "status": 200, "type": "text/html", "headers": {}}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            contacts.append(("GET", self.path))
            self.send_response(response_spec["status"])
            self.send_header("Content-Type", response_spec["type"])
            for name, value in response_spec["headers"].items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(response_spec["body"])
        def do_POST(self):
            contacts.append(("POST", self.path))
            self.send_response(403)
            self.end_headers()
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    original = socket.socket.connect
    denied = []
    def local_only(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6) and address[0] not in {"127.0.0.1", "::1"}:
            denied.append(address[0])
            raise AssertionError("external sockets forbidden during browser acceptance")
        return original(sock, address)
    monkeypatch.setattr(socket.socket, "connect", local_only)
    request = partial(request_pinned_https, resolver=lambda host, port: ["1.1.1.1"],
        transport=LocalTCP(server.server_port))
    request.fixture_response = response_spec
    yield request, contacts, denied
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def action(page, kind, name=None, value=None):
    snapshot = page.latest
    locator = next((n.node_id for n in snapshot.accessible_nodes if n.name == name), None)
    return BrowserActionV2(kind=kind, locator_ref=locator,
        expected_page_revision=snapshot.document_digest,
        input_value_ref="value-" + uuid.uuid4().hex if kind in {"fill", "select", "check"} else None)


@pytest.mark.asyncio
async def test_actual_chromium_offline_multifield_preview_and_guards(local_form):
    request, contacts, denied = local_form
    events = []
    async def authority(): pass
    async def record(event): events.append(event)
    page = ProfiledInteractionPage(authority=authority, intent=record, result=record, request=request, source_digest=digest(FORM))
    try:
        snapshot = await page.start()
        assert snapshot["origin"] == "https://httpbin.org"
        await page.apply(action(page, "fill", "Customer name:"), "Operator private value")
        await page.apply(action(page, "fill", "Instructions"), "Literal preview")
        await page.apply(action(page, "select", "Delivery time"), "evening")
        await page.apply(action(page, "check", "Cheese"), True)
        await page.apply(action(page, "click", "Large"))
        preview = await page.apply(action(page, "extract"))
        values = {n["field"]: n["value"] for n in preview["preview"]}
        assert values["custname"] == "Operator private value" and values["delivery"] == "evening"
        assert next(n for n in preview["preview"] if n["field"] == "topping")["checked"]
        assert "Operator private value" not in json.dumps(events)
        assert await page.page.evaluate("() => typeof window.unapproved") == "undefined"
        with pytest.raises(InteractionError, match="exact_effect_authority_required"):
            await page.apply(action(page, "click", "Submit order"))
        stale = action(page, "fill", "Customer name:")
        await page.page.evaluate("() => document.querySelector('input').value = 'drift'")
        with pytest.raises(InteractionError, match="fresh_snapshot_required"):
            await page.apply(stale, "must not write")
        await page.snapshot()
        with pytest.raises(Exception):
            await page.page.goto("https://denied.example/")
        assert page.transport.denials >= 1
        assert contacts == [("GET", "/forms/post")]
        assert denied == []
    finally:
        assert await page.stop()


@pytest.mark.asyncio
async def test_ambiguous_nodes_action_limits_and_unknown_cleanup(local_form):
    request, contacts, _ = local_form
    async def authority(): pass
    async def record(event): pass
    page = ProfiledInteractionPage(authority=authority, intent=record, result=record, request=request, source_digest=digest(FORM))
    try:
        await page.start()
        await page.page.evaluate("() => document.querySelector('label').after(document.querySelector('label').cloneNode(true))")
        await page.snapshot()
        names = [n for n in page.latest.accessible_nodes if n.name == "Customer name:"]
        assert len(names) == 2 and all(n.actions == [] for n in names)
        with pytest.raises(InteractionError, match="exact_effect_authority_required"):
            await page.apply(action(page, "fill", "Customer name:"), "no guessed edit")
        for _ in range(19):
            await page.apply(action(page, "wait"))
        with pytest.raises(InteractionError, match="action_limit"):
            await page.apply(action(page, "wait"))
        assert contacts == [("GET", "/forms/post")]
    finally:
        assert await page.stop()
    page.resources.launch_attempted = True
    page.resources.context = page.resources.browser = page.resources.playwright = page.resources.session = None
    assert await page.stop() is False


def test_no_selector_script_secret_or_unknown_profile_grammar():
    for extra in ({"selector": "input"}, {"script": "fetch('https://openrouter.ai')"}, {"url": "https://evil.example"}):
        with pytest.raises(ValidationError):
            BrowserActionV2.model_validate({"kind": "extract", "expected_page_revision": "a" * 64, **extra})
    with pytest.raises(ValidationError):
        BrowserActionV2(kind="fill", locator_ref="input", expected_page_revision="a" * 64,
            input_value_ref="value-" + uuid.uuid4().hex)


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_case", ["normal", "root_revoked", "goal_changed", "deadline", "audit_cas", "prelaunch"])
async def test_native_api_durable_history_private_inputs_and_shared_lane(accounting_db, local_form, monkeypatch, cleanup_case):
    from fastapi import FastAPI
    from src.api import auth, browser, goals
    from src.auth.middleware import OperatorAuthMiddleware
    from src.browser.sessions import ProfiledInteractionSessions
    from src.browser.task_lane import BrowserTaskLane, browser_task_lane_wait_reason
    from src.db.models import InferenceCostReservation, WorkflowRunState
    from sqlmodel import select

    root, _, factory = accounting_db
    request, contacts, denied = local_form
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "browser-native-root")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "test,localhost,127.0.0.1")
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", "http://localhost:3001")
    monkeypatch.setattr(settings, "operator_auth_cookie_secure", False)
    auth._reset_login_throttle_for_tests()
    service = ProfiledInteractionSessions(request=request, source_digest=digest(FORM))
    monkeypatch.setattr(browser, "profiled_interaction_sessions", service)
    await service.start()
    app = FastAPI()
    app.add_middleware(OperatorAuthMiddleware)
    app.include_router(auth.router, prefix="/api/auth")
    app.include_router(goals.router, prefix="/api")
    app.include_router(browser.router, prefix="/api")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
            headers={"origin": "http://localhost:3001"}) as client:
        assert (await client.post("/api/auth/login", json={"password": "browser-native-root"})).status_code == 200
        inventory = (await client.get("/api/capabilities/browser-interactions/profiles")).json()
        assert inventory["runtime_state"] == "inactive" and inventory["blocked_reason"] == "browser_operator_continuity_required"
        assert (await client.post("/api/auth/ownership/enroll")).status_code == 200
        goal = (await client.post("/api/goals", json={"title": "Preview the registered public form"})).json()
        payload = {"profile_id": "httpbin.forms.v1", "goal_id": goal["id"], "goal_revision": goal["revision"],
            "request_key": str(uuid.uuid4()), "read_ack": True}
        if cleanup_case == "prelaunch":
            from src.browser import task_runner
            monkeypatch.setattr(task_runner, "_playwright_browser_executable_present", lambda: False)
            inventory = (await client.get("/api/capabilities/browser-interactions/profiles")).json()
            assert inventory["runtime_state"] == "inactive" and inventory["blocked_reason"] == "browser_interaction_runtime_unavailable"
        response = await client.post("/api/capabilities/browser-interactions/jobs", json=payload)
        if cleanup_case == "prelaunch":
            assert response.status_code == 503 and response.json()["detail"]["code"] == "browser_interaction_runtime_unavailable"
            assert service.active == {} and contacts == [] and denied == []
            assert browser_task_lane_wait_reason(root) is None
            lane = BrowserTaskLane(root)
            assert lane.try_acquire() is True
            lane.release()
            async with factory.accounting_sessions() as db:
                stored = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.job_kind == "browser_interact_v2"))
                row = await service.jobs.get_job(stored.run_identity)
            assert row["status"] == "blocked" and row["effects"][-1]["status"] == "succeeded"
            return
        assert response.status_code == 200, response.text
        opened = response.json()
        browser_version = service.active[opened["job_id"]]["page"].resources.browser.version
        assert opened["live"] and opened["history"][0]["status"] == "intent"
        lane = BrowserTaskLane(root)
        assert lane.try_acquire() is False
        second = await client.post("/api/capabilities/browser-interactions/jobs", json={**payload, "request_key": str(uuid.uuid4())})
        assert second.status_code == 409 and second.json()["detail"]["code"] == "browser_lane_busy"
        job_id = opened["job_id"]
        node = next(n for n in opened["page"]["accessible_nodes"] if n["name"] == "Customer name:")
        body = {"expected_revision": opened["revision"], "fencing_token": opened["fencing_token"],
            "action": {"kind": "fill", "locator_ref": node["node_id"],
                "expected_page_revision": opened["page"]["document_digest"], "input_value_ref": "value-" + uuid.uuid4().hex},
            "private_input": "Private native literal"}
        response = await client.post(f"/api/capabilities/browser-interactions/jobs/{job_id}/actions", json=body)
        assert response.status_code == 200, response.text
        updated = response.json()
        assert updated["history"][-1]["status"] == "completed"
        assert "Private native literal" not in json.dumps(updated)
        for kind, name, value in [("fill", "Instructions", "Private local comment"),
                                  ("select", "Delivery time", "evening"), ("check", "Cheese", True)]:
            node = next(n for n in updated["page"]["accessible_nodes"] if n["name"] == name)
            response = await client.post(f"/api/capabilities/browser-interactions/jobs/{job_id}/actions", json={
                "expected_revision": updated["revision"], "fencing_token": updated["fencing_token"],
                "action": {"kind": kind, "locator_ref": node["node_id"],
                    "expected_page_revision": updated["page"]["document_digest"],
                    "input_value_ref": "value-" + uuid.uuid4().hex}, "private_input": value})
            assert response.status_code == 200, response.text
            updated = response.json()
        button = next(n for n in updated["page"]["accessible_nodes"] if n["name"] == "Submit order")
        blocked = await client.post(f"/api/capabilities/browser-interactions/jobs/{job_id}/actions", json={
            "expected_revision": updated["revision"], "fencing_token": updated["fencing_token"],
            "action": {"kind": "click", "locator_ref": button["node_id"],
                "expected_page_revision": updated["page"]["document_digest"]}})
        assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "browser_exact_effect_authority_required"
        updated = (await client.get(f"/api/capabilities/browser-interactions/jobs/{job_id}")).json()
        assert updated["history"][-1]["status"] == "blocked"
        if cleanup_case == "normal":
            # Cached GET retains handles; only this authority-fenced endpoint
            # captures fresh DOM and rotates every opaque locator generation.
            old_ids = {n["node_id"] for n in updated["page"]["accessible_nodes"]}
            await service.active[job_id]["page"].page.evaluate("() => document.querySelector('input').value = 'fresh local drift'")
            refreshed = await client.post(f"/api/capabilities/browser-interactions/jobs/{job_id}/snapshot", json={
                "expected_revision": updated["revision"], "fencing_token": updated["fencing_token"]})
            assert refreshed.status_code == 200, refreshed.text
            updated = refreshed.json()
            assert old_ids.isdisjoint(n["node_id"] for n in updated["page"]["accessible_nodes"])
            assert updated["history"][-1]["kind"] == "snapshot" and updated["history"][-1]["status"] == "completed"
            # A distinct live authenticated Root cannot refresh or close this
            # original context, even with exact public job/fence metadata.
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                    headers={"origin": "http://localhost:3001"}) as foreign:
                assert (await foreign.post("/api/auth/login", json={"password": "browser-native-root"})).status_code == 200
                for operation in ("snapshot", "close"):
                    rejected = await foreign.post(f"/api/capabilities/browser-interactions/jobs/{job_id}/{operation}", json={
                        "expected_revision": updated["revision"], "fencing_token": updated["fencing_token"]})
                    assert rejected.status_code == 404, rejected.text
            assert service.active[job_id]["closing"] is False
            # Reprepare explicitly after the test-owned drift; no replay.
            node = next(n for n in updated["page"]["accessible_nodes"] if n["name"] == "Customer name:")
            prepared = await client.post(f"/api/capabilities/browser-interactions/jobs/{job_id}/actions", json={
                "expected_revision": updated["revision"], "fencing_token": updated["fencing_token"],
                "action": {"kind": "fill", "locator_ref": node["node_id"], "expected_page_revision": updated["page"]["document_digest"],
                    "input_value_ref": "value-" + uuid.uuid4().hex}, "private_input": "Private native literal"})
            assert prepared.status_code == 200, prepared.text
            updated = prepared.json()
        preview = await client.post(f"/api/capabilities/browser-interactions/jobs/{job_id}/actions", json={
            "expected_revision": updated["revision"], "fencing_token": updated["fencing_token"],
            "action": {"kind": "extract", "expected_page_revision": updated["page"]["document_digest"]}})
        assert preview.status_code == 200, preview.text
        updated = preview.json()
        assert next(n for n in updated["preview"] if n["field"] == "custname")["value"] == "Private native literal"
        assert (await client.post("/api/capabilities/browser-interactions/jobs", json=payload)).json()["job_id"] == job_id
        if cleanup_case != "normal":
            from datetime import datetime, timedelta, timezone
            from src.db.models import OperatorSession, Goal
            state = service.active[job_id]
            if cleanup_case == "audit_cas":
                async def failed_audit(*args, **kwargs):
                    async with factory.accounting_sessions() as db:
                        target = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id))
                        target.revision += 1
                        await db.commit()
                    raise RuntimeError("simulated durable CAS loss after positive physical cleanup")
                monkeypatch.setattr(service.jobs, "record_effect", failed_audit)
            else:
                async with factory.accounting_sessions() as db:
                    if cleanup_case == "root_revoked":
                        target = await db.scalar(select(OperatorSession).where(OperatorSession.id == state["owner"].session_id))
                        target.revoked_at = datetime.now(timezone.utc)
                    elif cleanup_case == "goal_changed":
                        target = await db.scalar(select(Goal).where(Goal.id == goal["id"]))
                        target.revision += 1
                    else:
                        target = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id))
                        target.deadline_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                    await db.commit()
            await service._close(job_id, reason="operator_closed")
            row = await service.jobs.get_job(job_id)
            assert row["status"] == ("running" if cleanup_case == "audit_cas" else "cancelled")
            if cleanup_case in {"root_revoked", "deadline"}:
                assert row["effects"][-1]["status"] == "succeeded"
            assert job_id not in service.active
            assert browser_task_lane_wait_reason(root) is None
            assert lane.try_acquire() is True
            lane.release()
            assert state["page"].page.is_closed()
            assert not state["page"].resources.browser.is_connected()
            assert contacts == [("GET", "/forms/post")] and denied == []
            return
        closed = await client.post(f"/api/capabilities/browser-interactions/jobs/{job_id}/close",
            json={"expected_revision": updated["revision"], "fencing_token": updated["fencing_token"]})
        assert closed.status_code == 200, closed.text
        assert closed.json()["status"] == "succeeded"
        assert lane.try_acquire() is True
        lane.release()
        restarted = ProfiledInteractionSessions(request=request, source_digest=digest(FORM))
        monkeypatch.setattr(browser, "profiled_interaction_sessions", restarted)
        historical = await client.get(f"/api/capabilities/browser-interactions/jobs/{job_id}")
        assert historical.status_code == 200, historical.text
        assert historical.json()["history"][-1]["status"] == "completed"
        assert historical.json()["live"] is False
        listed = await client.get("/api/capabilities/browser-interactions/jobs")
        assert listed.status_code == 200 and listed.json()["jobs"][0]["job_id"] == job_id
        discovered = await client.get(f"/api/capabilities/browser-interactions/requests/{payload['request_key']}")
        assert discovered.status_code == 200 and discovered.json()["job_id"] == job_id
        assert "Private native literal" not in listed.text + discovered.text + historical.text
        assert contacts == [("GET", "/forms/post")] and denied == []
        async with factory.accounting_sessions() as db:
            row = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id))
            assert "Private native literal" not in row.checkpoint_receipts_json
            assert row.budget_digest == digest({"budget_microusd": 0})
            assert (await db.execute(select(InferenceCostReservation))).scalars().all() == []
        assert all(b"Private native literal" not in p.read_bytes() for p in (root / "artifacts").rglob("*.json"))
        assert browser_task_lane_wait_reason(root) is None
        import os
        from pathlib import Path
        receipt_path = os.environ.get("SERAPH_BROWSER_INTERACTION_RECEIPT")
        if receipt_path:
            receipt = {"schema": "seraph.browser.interact.local-receipt.v1",
                "capability_id": "browser.interact.v2", "profile_id": "httpbin.forms.v1",
                "browser_version": browser_version, "job_id": job_id,
                "actual_local_tcp_contacts": contacts, "external_contacts": 0,
                "inference_reservations": 0, "provider_spend_microusd": 0,
                "completed_status": closed.json()["status"],
                "history_after_service_recreation": historical.json()["history"],
                "private_input_ciphertext_readback": "verified",
                "current_literal_preview_readback": "verified",
                "positive_original_context_cleanup": "verified",
                "lane_reacquisition_after_cleanup": "verified", "no_learning": True,
                "limits": "local Linux fixture; not external site availability or model quality"}
            destination = Path(receipt_path)
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            destination.write_text(json.dumps(receipt, indent=2) + "\n")
            destination.chmod(0o600)
    await service.stop()


@pytest.mark.parametrize("change", [
    {"status": 403}, {"type": "application/json"}, {"headers": {"Set-Cookie": "session=must-not-enter-context"}},
    {"status": 302, "headers": {"Location": "/post"}}, {"body": FORM + b"<!-- drift -->"},
    {"body": b"X" * 65537},
])
@pytest.mark.asyncio
async def test_actual_response_bounds_source_drift_and_cookie_redirect_denial(local_form, change):
    request, contacts, denied = local_form
    request.fixture_response.update(change)
    async def authority(): pass
    async def record(event): pass
    page = ProfiledInteractionPage(authority=authority, intent=record, result=record,
        request=request, source_digest=digest(FORM))
    try:
        with pytest.raises(InteractionError):
            await page.start()
        assert contacts == [("GET", "/forms/post")] and denied == []
    finally:
        assert await page.stop()


@pytest.mark.asyncio
async def test_snapshot_control_and_field_bounds_are_not_published(local_form):
    request, _, _ = local_form
    async def authority(): pass
    async def record(event): pass
    page = ProfiledInteractionPage(authority=authority, intent=record, result=record,
        request=request, source_digest=digest(FORM))
    try:
        await page.start()
        with pytest.raises(InteractionError, match="private_field_bounds"):
            await page.apply(action(page, "fill", "Customer name:"), "X" * 2049)
        await page.page.evaluate("() => document.querySelector('textarea').value='X'.repeat(2049)")
        with pytest.raises(InteractionError, match="profile_field_bounds"):
            await page.snapshot()
        await page.page.evaluate("() => {document.querySelector('textarea').value='';for(let i=0;i<65;i++) document.forms[0].append(document.querySelector('input').cloneNode(true))}")
        with pytest.raises(InteractionError, match="document_bounds"):
            await page.snapshot()
    finally:
        assert await page.stop()


def test_v2_lane_marker_survives_owner_fd_loss_and_preserves_v1(tmp_path):
    import os
    from src.browser.task_lane import BrowserTaskLane, BrowserTaskLaneError, browser_task_lane_wait_reason
    root = tmp_path / "workspace"
    root.mkdir()
    owner = BrowserTaskLane(root).acquire()
    witness = owner.require_positive_cleanup("original-browser-job")
    with pytest.raises(BrowserTaskLaneError):
        owner.confirm_positive_cleanup({**witness, "context_nonce": "wrong"})
    # OS descriptor loss models the owner death boundary; the persisted witness
    # must remain blocked even though flock is now free.
    os.close(owner._descriptor)
    owner._descriptor = None
    assert BrowserTaskLane(root).try_acquire() is False
    assert browser_task_lane_wait_reason(root) == "browser_cleanup_required"
    with pytest.raises(BrowserTaskLaneError):
        owner.confirm_positive_cleanup(witness)

    clean_root = tmp_path / "clean-workspace"
    clean_root.mkdir()
    clean = BrowserTaskLane(clean_root).acquire()
    clean_witness = clean.require_positive_cleanup("positively-closed-job")
    clean.confirm_positive_cleanup(clean_witness)
    clean.release()
    v1 = BrowserTaskLane(clean_root).acquire()
    v1.release()
    assert browser_task_lane_wait_reason(clean_root) is None


@pytest.mark.parametrize("ack", [1, "true", False])
def test_contact_acknowledgement_is_literal_true(ack):
    from src.browser.interaction_contracts import InteractionPrepare
    with pytest.raises(ValidationError):
        InteractionPrepare(profile_id="httpbin.forms.v1", goal_id="goal", goal_revision=1,
            request_key=str(uuid.uuid4()), read_ack=ack)


@pytest.mark.asyncio
async def test_missing_playwright_import_is_proven_prechild_cleanup(tmp_path, monkeypatch):
    import builtins
    from src.browser.task_lane import BrowserTaskLane, browser_task_lane_wait_reason
    original_import = builtins.__import__
    def missing_playwright(name, *args, **kwargs):
        if name == "playwright.async_api":
            raise ImportError("fixture missing Playwright")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", missing_playwright)
    async def authority(): pass
    async def record(event):
        pytest.fail("No public contact or document event is allowed before a driver exists")
    lane = BrowserTaskLane(tmp_path).acquire()
    witness = lane.require_positive_cleanup("prechild-import-failure")
    page = ProfiledInteractionPage(authority=authority, intent=record, result=record)
    with pytest.raises(InteractionError, match="browser_interaction_runtime_unavailable"):
        await page.start()
    assert page.resources.context_not_started
    assert await page.stop()
    lane.confirm_positive_cleanup(witness)
    lane.release()
    assert browser_task_lane_wait_reason(tmp_path) is None
    next_owner = BrowserTaskLane(tmp_path).acquire()
    next_owner.release()


def test_actual_linux_boot_reader_and_controlled_exact_boot_cleanup(tmp_path, monkeypatch):
    import os
    import sys
    from pathlib import Path
    from src.browser import task_lane
    if sys.platform == "linux":
        actual = task_lane.linux_boot_session_id()
        assert actual == Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        assert str(uuid.UUID(actual)) == actual
    old_boot, new_boot = str(uuid.uuid4()), str(uuid.uuid4())
    monkeypatch.setattr(task_lane, "linux_boot_session_id", lambda: old_boot)
    original = task_lane.BrowserTaskLane(tmp_path).acquire()
    witness = original.require_positive_cleanup("exact-job")
    assert witness["linux_boot_id"] == old_boot
    os.close(original._descriptor)
    original._descriptor = None
    assert not task_lane.BrowserTaskLane(tmp_path).try_acquire()
    with pytest.raises(task_lane.BrowserTaskLaneError, match="witness mismatch"):
        task_lane.acquire_browser_cleanup_lane(tmp_path, "wrong-job")
    observer = task_lane.acquire_browser_cleanup_lane(tmp_path, "exact-job")
    with pytest.raises(task_lane.BrowserTaskLaneBusy, match="this boot"):
        observer.cleanup_proof("exact-job")
    observer.close_cleanup_observer()
    monkeypatch.setattr(task_lane, "linux_boot_session_id", lambda: None)
    observer = task_lane.acquire_browser_cleanup_lane(tmp_path, "exact-job")
    with pytest.raises(task_lane.BrowserTaskLaneBusy):
        observer.cleanup_proof("exact-job")
    observer.close_cleanup_observer()
    monkeypatch.setattr(task_lane, "linux_boot_session_id", lambda: new_boot)
    observer = task_lane.acquire_browser_cleanup_lane(tmp_path, "exact-job")
    current, proof = observer.cleanup_proof("exact-job")
    assert current == witness and proof == "linux_boot_changed"
    with pytest.raises(task_lane.BrowserTaskLaneError):
        observer.commit_cleanup_receipt({**witness, "context_nonce": "wrong"}, proof)
    observer.commit_cleanup_receipt(witness, proof)
    observer.release()
    next_job = task_lane.BrowserTaskLane(tmp_path).acquire()
    next_job.release()


def test_positive_cleanup_witness_retained_until_exact_receipt_commit(tmp_path):
    from src.browser import task_lane
    lane = task_lane.BrowserTaskLane(tmp_path).acquire()
    witness = lane.require_positive_cleanup("closed-original")
    lane.retain_positive_cleanup(witness)
    lane.quarantine("closed-original")
    assert not task_lane.BrowserTaskLane(tmp_path).try_acquire()
    assert json.loads(lane.lock_path.read_text())["positive_cleanup_required"] is True
    same = task_lane.acquire_browser_cleanup_lane(tmp_path, "closed-original")
    assert same is lane
    exact, proof = same.cleanup_proof("closed-original")
    assert exact == witness and proof == "owned_positive_close"
    same.commit_cleanup_receipt(exact, proof)
    same.release()
    next_job = task_lane.BrowserTaskLane(tmp_path).acquire()
    next_job.release()
