"""Real canonical jobs, private artifacts and disposable source fixtures.

No inference, account credential, operator workspace or external socket is used.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio
from fastapi import HTTPException
from src.work_board.repository import BoardError
from src.workflows.job_runtime import DurableJobError
from sqlalchemy import select

from config.settings import settings
from src.api import mail
from src.db.models import CalendarEventBinding, CalendarReadConsent, GoogleServiceConnection, Goal, MailLabelBinding, MailMessageBinding, MailReadConsent, OperatorIdentity, OperatorSession, Session, WorkflowRunState
from src.integrations import connection_sync as sync
from src.integrations.gmail_read import GMAIL_READONLY_SCOPE, GoogleGmailReadonlyAdapter, message_key
from src.integrations.google_calendar import GoogleCalendarReadonlyAdapter, canonical_event_key
from src.vault import crypto, encrypt, vault_repository
from src.work_board.contracts import WorkBoardOwner
from src.workflows.job_runtime import durable_job_repository

OWNER = WorkBoardOwner(principal_id="operator:test-bypass", session_id="test-auth-bypass")


@pytest_asyncio.fixture
async def source(async_db, monkeypatch, tmp_path):
    monkeypatch.setattr(sync, "get_session", async_db)
    monkeypatch.setattr(mail, "get_session", async_db)
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    monkeypatch.setattr(settings, "deployment_environment", "test")
    monkeypatch.setattr(crypto, "_fernet", None)
    timestamp = datetime.now(timezone.utc)
    from dataclasses import replace
    from src.auth.service import test_bypass_operator
    original_operator = test_bypass_operator()
    fixture_operator = replace(original_operator, _token_hash="fixture-only", operator_identity_id="fixture-stable-operator")
    monkeypatch.setattr("src.auth.middleware.test_bypass_operator", lambda: fixture_operator)
    async with async_db() as db:
        db.add(OperatorIdentity(id="fixture-stable-operator"))
        db.add(OperatorSession(id=OWNER.session_id, principal_id=OWNER.principal_id, operator_identity_id="fixture-stable-operator", token_hash="fixture-only", last_seen_at=timestamp, idle_expires_at=timestamp + timedelta(hours=1), absolute_expires_at=timestamp + timedelta(hours=1)))
        db.add(Session(id=OWNER.session_id))
        db.add(Goal(id="goal", title="Connected source task", owner_principal_id=OWNER.principal_id, owner_session_id=OWNER.session_id, revision=1))
        connection = GoogleServiceConnection(connection_id="connection", owner_principal_id=OWNER.principal_id, owner_session_id=OWNER.session_id, service="gmail_readonly", vault_secret_key="fixture-google", credential_fingerprint="fixture-fingerprint", state="active", revision=1, declared_scopes_json=json.dumps([GMAIL_READONLY_SCOPE]))
        db.add(connection)
        db.add(MailLabelBinding(label_id="label", owner_principal_id=OWNER.principal_id, owner_session_id=OWNER.session_id, connection_id="connection", connection_revision=1, provider_label_id_ciphertext=encrypt("INBOX"), provider_label_digest="fixture-label-digest", state="active"))
        db.add(MailReadConsent(consent_id="grant", owner_principal_id=OWNER.principal_id, owner_session_id=OWNER.session_id, connection_id="connection", connection_revision=1, goal_id="goal", goal_revision=1, label_ids_json='["label"]', sync_metadata_limit=50, max_messages=10, source_read_allowed=True, source_revision=1, source_digest="fixture-source-digest", allowed_body_fields_json='["plainbody"]', expires_at=timestamp + timedelta(hours=1), created_at=timestamp))
    await vault_repository.store("fixture-google", json.dumps({"client_id": "fixture-client", "refresh_token": "fixture-refresh"}))
    runtime = sync.ConnectionSyncService()
    await runtime.start()
    original_synchronize = runtime.synchronize
    async def fixture_synchronize(owner, value, **kwargs):
        kwargs.setdefault("authenticated_token_hash", "fixture-only")
        return await original_synchronize(owner, value, **kwargs)
    monkeypatch.setattr(runtime, "synchronize", fixture_synchronize)
    yield runtime, timestamp, async_db, tmp_path
    await runtime.stop()


def request(timestamp, *, uuid="run", max_items=50, private=(), threads=(), reset=False):
    return sync.SyncRequest.model_validate({"input": {"goal_ref": {"id": "goal", "revision": 1}, "connection_ref": {"id": "connection", "revision": 1}, "source_scope": {"provider": "gmail", "consents": [{"id": "grant", "revision": 1}], "label_ids": ["label"], "thread_keys": list(threads), "selected_private_items": list(private), "acknowledge_private_read": bool(private), "reset_cursor": reset}, "window": {"start": (timestamp - timedelta(days=1)).isoformat(), "end": timestamp.isoformat()}, "max_items": max_items}, "request_uuid": uuid})


class Provider:
    def __init__(self, timestamp, *, count=5, page_size=2, fail_page=None, deleted=(), rate_limit=0):
        self.timestamp = timestamp
        self.count = count
        self.page_size = page_size
        self.fail_page = fail_page
        self.deleted = set(deleted)
        self.rate_limit = rate_limit
        self.calls = []

    def response(self, req):
        self.calls.append(req)
        if req.url.path == "/token":
            return httpx.Response(200, json={"access_token": "fixture-access", "scope": GMAIL_READONLY_SCOPE})
        if req.url.path.endswith("/messages"):
            offset = int(req.url.params.get("pageToken", "0"))
            if self.rate_limit:
                self.rate_limit -= 1
                return httpx.Response(429, json={"error": "fixture"})
            if offset == self.fail_page:
                raise httpx.ReadTimeout("fixture interruption")
            limit = min(int(req.url.params["maxResults"]), self.page_size)
            ids = list(range(offset, min(offset + limit, self.count)))
            payload = {"messages": [{"id": f"m{i}"} for i in ids]}
            if offset + len(ids) < self.count:
                payload["nextPageToken"] = str(offset + len(ids))
            return httpx.Response(200, json=payload)
        identifier = req.url.path.rsplit("/", 1)[-1]
        if identifier in self.deleted:
            return httpx.Response(404, json={"error": "fixture-deleted"})
        payload = {"id": identifier, "threadId": "t" + identifier, "historyId": "h1", "internalDate": str(int((self.timestamp - timedelta(hours=1)).timestamp() * 1000)), "snippet": "private-preview", "labelIds": ["INBOX"], "payload": {"headers": [{"name": "Subject", "value": "private-subject"}], "mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(b"private-body").decode()}}}
        return httpx.Response(200, json=payload)


def install_provider(monkeypatch, provider, *, transport=None):
    owned_transport = transport or httpx.MockTransport(provider.response)
    # Resolver evidence is provider-shaped; transport alone redirects to the
    # fixture. Production destination policy/authority remains in the adapter.
    monkeypatch.setattr(sync, "GoogleGmailReadonlyAdapter", lambda connection, **kwargs: GoogleGmailReadonlyAdapter(connection, transport=owned_transport, resolver=lambda *_: ["8.8.8.8"], **kwargs))


@pytest.mark.asyncio
async def test_paginated_real_jobs_private_readback_and_exact_replay(source, monkeypatch):
    runtime, timestamp, db_factory, workspace = source
    provider = Provider(timestamp)
    install_provider(monkeypatch, provider)
    selected = message_key(OWNER.principal_id, "connection", "m0")
    body = request(timestamp, private=[selected])
    result = await runtime.synchronize(OWNER, body)
    assert result["coverage"]["pages_read"] == 3
    assert result["coverage"]["metadata_examined"] == 5
    assert result["coverage"]["more_available"] is False
    assert len(result["items"]) == 5
    assert not any(secret in json.dumps(result) for secret in ("fixture-refresh", "fixture-access", "private-subject", "private-body", "page_token", "m0"))
    read = await runtime.read_item(OWNER, "connection", selected)
    assert read["item"]["content"]["body"] == "private-body"
    assert read["item"]["content"]["subject"] == "private-subject"
    contacts = len(provider.calls)
    replay = await runtime.synchronize(OWNER, body)
    assert replay["replayed"] is True
    assert replay["items"] == result["items"]
    assert len(provider.calls) == contacts
    async with db_factory() as db:
        connection = await db.get(GoogleServiceConnection, "connection")
        assert connection.sync_cursor_revision == 3
        assert connection.sync_active_job_id is None
        rows = (await db.execute(select(MailMessageBinding))).scalars().all()
        assert len(rows) == 5 and {row.revision for row in rows} == {1}
        assert all(row.source_consent_id == "grant" for row in rows)
    for path in workspace.rglob("*.enc"):
        assert b"private-body" not in path.read_bytes()
        assert b"fixture-refresh" not in path.read_bytes()


@pytest.mark.asyncio
async def test_three_page_and_fifty_item_caps(source, monkeypatch):
    runtime, timestamp, db_factory, _ = source
    provider = Provider(timestamp, count=80, page_size=20)
    install_provider(monkeypatch, provider)
    result = await runtime.synchronize(OWNER, request(timestamp))
    assert result["coverage"]["metadata_examined"] == 50
    assert result["coverage"]["pages_read"] == 3
    assert result["coverage"]["partial"] is True
    assert len(result["items"]) == 50
    assert len([call for call in provider.calls if call.url.params.get("format") == "metadata"]) == 50
    assert all(int(call.url.params["maxResults"]) <= 20 for call in provider.calls if "maxResults" in call.url.params)
    async with db_factory() as db:
        connection = await db.get(GoogleServiceConnection, "connection")
        assert connection.sync_cursor_revision == 3


@pytest.mark.asyncio
async def test_interrupted_next_page_retains_previous_cursor_and_recovers_without_duplicates(source, monkeypatch):
    runtime, timestamp, db_factory, _ = source
    provider = Provider(timestamp, fail_page=2)
    install_provider(monkeypatch, provider)
    body = request(timestamp)
    with pytest.raises(sync.GmailReadError):
        await runtime.synchronize(OWNER, body)
    async with db_factory() as db:
        connection = await db.get(GoogleServiceConnection, "connection")
        job_id = connection.sync_active_job_id
        assert connection.sync_cursor_revision == 1
        rows = (await db.execute(select(MailMessageBinding))).scalars().all()
        assert len(rows) == 2
    root = await durable_job_repository.get_job(job_id)
    assert root["status"] == "unknown_external_effect"
    contacts = len(provider.calls)
    with pytest.raises(sync.SyncError):
        await runtime.synchronize(OWNER, body)
    assert len(provider.calls) == contacts
    receipt = await runtime.reconcile(OWNER, "connection", job_id, root["revision"], expected_cursor_revision=1, authenticated_token_hash="fixture-only")
    unchanged = await durable_job_repository.get_job(job_id)
    assert unchanged["status"] == "unknown_external_effect" and unchanged["effects"] == root["effects"]
    status = await runtime.status(OWNER, "connection")
    assert status["reservation_state"] == "available" and status["unresolved_jobs"][0]["job_id"] == job_id
    assert receipt["provider_contacts"] == 0
    provider.fail_page = None
    result = await runtime.synchronize(OWNER, request(timestamp, uuid="resume"))
    assert len(result["items"]) == 5
    async with db_factory() as db:
        rows = (await db.execute(select(MailMessageBinding))).scalars().all()
        assert len(rows) == 5 and {row.revision for row in rows} == {1}
    assert [call.url.params.get("pageToken") for call in provider.calls if call.url.path.endswith("/messages")][-2:] == ["2", "4"]


@pytest.mark.asyncio
async def test_revocation_blocks_contact_and_private_readback_retains_audit(source, monkeypatch):
    runtime, timestamp, db_factory, _ = source
    provider = Provider(timestamp)
    install_provider(monkeypatch, provider)
    result = await runtime.synchronize(OWNER, request(timestamp))
    contacts = len(provider.calls)
    async with db_factory() as db:
        consent = await db.get(MailReadConsent, "grant")
        consent.state = "revoked"
        consent.source_read_allowed = False
        consent.source_revision += 1
    with pytest.raises(sync.GmailReadError):
        await runtime.synchronize(OWNER, request(timestamp, uuid="revoked"))
    with pytest.raises(sync.GmailReadError):
        await runtime.read_item(OWNER, "connection", result["items"][0]["opaque_id"])
    assert len(provider.calls) == contacts
    root = await durable_job_repository.get_job(result["job_id"])
    assert root["effects"] and root["status"] == "succeeded"
    status = await runtime.status(OWNER, "connection")
    assert status["state"] == "blocked" and status["items"] == []


@pytest.mark.asyncio
async def test_metadata_grant_is_explicit_and_body_selection_is_separate(source, monkeypatch):
    runtime, timestamp, db_factory, _ = source
    provider = Provider(timestamp)
    install_provider(monkeypatch, provider)
    async with db_factory() as db:
        consent = await db.get(MailReadConsent, "grant")
        consent.sync_metadata_limit = 0
    with pytest.raises(sync.SyncError, match="Explicit bounded"):
        await runtime.synchronize(OWNER, request(timestamp))
    assert provider.calls == []
    schema = request(timestamp).model_dump(mode="json")
    schema["input"]["source_scope"]["selected_private_items"] = ["selected"]
    with pytest.raises(ValueError, match="separate explicit"):
        sync.SyncRequest.model_validate(schema)


@pytest.mark.asyncio
async def test_private_reads_capped_at_ten_and_only_selected(source, monkeypatch):
    runtime, timestamp, _, _ = source
    provider = Provider(timestamp, count=30, page_size=20)
    install_provider(monkeypatch, provider)
    selected = [message_key(OWNER.principal_id, "connection", f"m{i}") for i in range(10)]
    result = await runtime.synchronize(OWNER, request(timestamp, private=selected))
    assert len([call for call in provider.calls if call.url.params.get("format") == "full"]) == 10
    other = await runtime.read_item(OWNER, "connection", result["items"][20]["opaque_id"])
    assert "body" not in other["item"]["content"]
    malformed = request(timestamp).model_dump(mode="json")
    malformed["input"]["source_scope"]["selected_private_items"] = selected + ["eleventh"]
    malformed["input"]["source_scope"]["acknowledge_private_read"] = True
    with pytest.raises(ValueError):
        sync.SyncRequest.model_validate(malformed)


@pytest.mark.asyncio
async def test_no_import_activation_and_stop_rejects_new_work(source):
    runtime, timestamp, _, _ = source
    inactive = sync.ConnectionSyncService()
    assert inactive.started is False
    with pytest.raises(sync.SyncError, match="inactive"):
        await inactive.synchronize(OWNER, request(timestamp))
    await runtime.stop()
    with pytest.raises(sync.SyncError, match="inactive"):
        await runtime.synchronize(OWNER, request(timestamp))


@pytest.mark.asyncio
async def test_real_local_http_source_fixture_and_selected_task_citation(source, monkeypatch):
    """Literal fixture HTTP readback, with exact loopback transport ownership."""
    runtime, timestamp, _, _ = source
    provider = Provider(timestamp, count=4, page_size=2)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def handle_request(self):
            length = int(self.headers.get("Content-Length", "0"))
            content = self.rfile.read(length) if length else b""
            response = provider.response(httpx.Request(self.command, "https://fixture.invalid" + self.path, content=content))
            self.send_response(response.status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

        do_GET = handle_request
        do_POST = handle_request

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = "http://127.0.0.1:" + str(server.server_port)

    class LocalFixtureTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, req):
            assert req.url.host in {"oauth2.googleapis.com", "gmail.googleapis.com"}
            # This exact transport is injected in the source owner only;
            # production fixed URLs, scope checks and authority still run.
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=2) as client:
                response = await client.request(req.method, origin + req.url.raw_path.decode(), content=await req.aread(), headers={"Content-Type": req.headers.get("Content-Type", "application/json")})
            return httpx.Response(response.status_code, content=response.content, headers={"Content-Type": "application/json"})

    install_provider(monkeypatch, provider, transport=LocalFixtureTransport())
    try:
        result = await runtime.synchronize(OWNER, request(timestamp))
        from src.extensions.source_operations import collect_connected_source_items
        context = await collect_connected_source_items(runtime, OWNER, "connection", [result["items"][0]])
        assert context["items"][0]["content"]["subject"] == "private-subject"
        assert context["coverage"]["metadata_examined"] == 4
        assert len(provider.calls) == 7  # token, two list pages, four metadata reads
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_same_connection_precontact_reservation_serializes_two_runtime_instances(source, async_db, monkeypatch):
    runtime, timestamp, db_factory, _ = source
    entered = asyncio.Event()
    release = asyncio.Event()
    provider = Provider(timestamp, count=1)

    async def respond(req):
        if req.url.path == "/token":
            entered.set()
            await release.wait()
        return provider.response(req)

    install_provider(monkeypatch, provider, transport=httpx.MockTransport(respond))
    second = sync.ConnectionSyncService()
    await second.start()
    first_task = asyncio.create_task(runtime.synchronize(OWNER, request(timestamp, uuid="first")))
    await asyncio.wait_for(entered.wait(), timeout=5)
    contacts = len(provider.calls)
    with pytest.raises(sync.SyncError, match="Another source"):
        await second.synchronize(OWNER, request(timestamp, uuid="second"), authenticated_token_hash="fixture-only")
    assert len(provider.calls) == contacts
    release.set()
    assert (await first_task)["status"] == "succeeded"
    await second.stop()
    async with db_factory() as db:
        connection = await db.get(GoogleServiceConnection, "connection")
        assert connection.sync_cursor_revision == 1 and connection.sync_active_job_id is None


@pytest.mark.asyncio
async def test_scope_change_requires_explicit_reset_without_token_reuse(source, monkeypatch):
    runtime, timestamp, _, _ = source
    provider = Provider(timestamp, count=9, page_size=1)
    install_provider(monkeypatch, provider)
    result = await runtime.synchronize(OWNER, request(timestamp))
    assert result["coverage"]["partial"]
    contacts = len(provider.calls)
    selected_thread = sync.thread_key(OWNER.principal_id, "connection", "tm0")
    with pytest.raises(sync.SyncError, match="explicitly reset"):
        await runtime.synchronize(OWNER, request(timestamp, uuid="scope-change", threads=[selected_thread]))
    assert len(provider.calls) == contacts
    result = await runtime.synchronize(OWNER, request(timestamp, uuid="reset", threads=[selected_thread], reset=True))
    lists = [call for call in provider.calls if call.url.path.endswith("/messages")]
    assert lists[-3].url.params.get("pageToken") is None
    assert result["coverage"]["scope_reset"] is True
    assert len(result["items"]) == 1


@pytest.mark.asyncio
async def test_one_rate_limit_future_retry_keeps_original_grant_deadline(source, monkeypatch):
    runtime, timestamp, _, _ = source
    provider = Provider(timestamp, count=1, rate_limit=1)
    install_provider(monkeypatch, provider)
    result = await runtime.synchronize(OWNER, request(timestamp))
    root = await durable_job_repository.get_job(result["job_id"])
    cooldown = next(item["payload"] for item in root["checkpoints"] if item["checkpoint_id"] == "connection-sync-cooldown")
    assert cooldown["retry_count"] == 1
    assert cooldown["original_deadline"] == root["deadline_at"]
    assert root["attempt_count"] == 1 and root["max_attempts"] == 1
    assert len([call for call in provider.calls if call.url.path.endswith("/messages")]) == 2


@pytest.mark.asyncio
async def test_second_rate_limit_is_not_retried(source, monkeypatch):
    runtime, timestamp, _, _ = source
    provider = Provider(timestamp, rate_limit=2)
    install_provider(monkeypatch, provider)
    with pytest.raises(sync.GmailReadError, match="rate limited"):
        await runtime.synchronize(OWNER, request(timestamp))
    assert len([call for call in provider.calls if call.url.path.endswith("/messages")]) == 2


@pytest.mark.asyncio
async def test_expired_grant_changed_credential_and_foreign_owner_fail_closed(source, monkeypatch):
    runtime, timestamp, db_factory, _ = source
    provider = Provider(timestamp)
    install_provider(monkeypatch, provider)
    result = await runtime.synchronize(OWNER, request(timestamp))
    contacts = len(provider.calls)
    with pytest.raises(sync.SyncError):
        await runtime.read_item(WorkBoardOwner(principal_id="foreign", session_id=OWNER.session_id), "connection", result["items"][0]["opaque_id"])
    async with db_factory() as db:
        connection = await db.get(GoogleServiceConnection, "connection")
        connection.credential_fingerprint = "changed"
    with pytest.raises(sync.SyncError, match="authority changed"):
        await runtime.read_item(OWNER, "connection", result["items"][0]["opaque_id"])
    async with db_factory() as db:
        grant = await db.get(MailReadConsent, "grant")
        grant.expires_at = timestamp - timedelta(seconds=1)
    with pytest.raises(sync.GmailReadError):
        await runtime.synchronize(OWNER, request(timestamp, uuid="expired"))
    assert len(provider.calls) == contacts


@pytest.mark.asyncio
async def test_deleted_selected_item_is_tombstoned_without_omission_inference(source, monkeypatch):
    runtime, timestamp, db_factory, _ = source
    provider = Provider(timestamp, count=1)
    install_provider(monkeypatch, provider)
    first = await runtime.synchronize(OWNER, request(timestamp))
    provider.deleted.add("m0")
    second = await runtime.synchronize(OWNER, request(timestamp, uuid="delete"))
    assert second["items"][0]["opaque_id"] == first["items"][0]["opaque_id"]
    read = await runtime.read_item(OWNER, "connection", second["items"][0]["opaque_id"])
    assert read["item"]["content"] == {"status": "deleted"}
    async with db_factory() as db:
        row = (await db.execute(select(MailMessageBinding))).scalar_one()
        assert row.status == "deleted" and row.revision == 2


@pytest.mark.asyncio
async def test_calendar_selected_details_and_explicit_canceled_tombstone(source, monkeypatch, app, client):
    runtime, timestamp, db_factory, workspace = source
    async with db_factory() as db:
        connection = await db.get(GoogleServiceConnection, "connection")
        connection.service = "calendar_readonly"
        db.add(CalendarReadConsent(consent_id="calendar-grant", owner_principal_id=OWNER.principal_id, owner_session_id=OWNER.session_id, connection_id="connection", connection_revision=1, calendar_id=encrypt("calendar"), goal_id="goal", goal_revision=1, allowed_fields_json='["summary","start","end","description","attendees"]', window_minutes=10080, max_events=50, sync_metadata_limit=50, expires_at=timestamp + timedelta(hours=1), created_at=timestamp, consent_digest="calendar-fixture-digest"))
    events = [{"id": "deleted", "status": "cancelled", "etag": "deleted-revision"}, {"id": "present", "summary": "Selected meeting", "start": {"dateTime": (timestamp + timedelta(hours=1)).isoformat()}, "end": {"dateTime": (timestamp + timedelta(hours=2)).isoformat()}, "description": "private-calendar-details", "attendees": [{"email": "private@example.invalid"}], "etag": "present-revision"}]
    key = canonical_event_key(OWNER.principal_id, "connection", "calendar", events[1])
    calls = []

    def respond(req):
        calls.append(req)
        if req.url.path == "/token":
            return httpx.Response(200, json={"access_token": "fixture-access", "scope": "https://www.googleapis.com/auth/calendar.readonly"})
        if req.url.path.endswith("/calendarList"):
            return httpx.Response(200, json={"items": [{"id": "calendar", "summary": "Selected calendar"}]})
        if req.url.path.endswith("/present"):
            return httpx.Response(200, json=events[1])
        assert "description" not in req.url.params["fields"]
        return httpx.Response(200, json={"items": events})

    monkeypatch.setattr(sync, "GoogleCalendarReadonlyAdapter", lambda connection, **kwargs: GoogleCalendarReadonlyAdapter(connection, transport=httpx.MockTransport(respond), resolver=lambda *_: ["8.8.8.8"], **kwargs))
    body = request(timestamp).model_dump(mode="json")
    body["input"]["source_scope"] = {"provider": "calendar", "consents": [{"id": "calendar-grant", "revision": 1}], "selected_private_items": [key], "acknowledge_private_read": True}
    body["input"]["window"] = {"start": timestamp.isoformat(), "end": (timestamp + timedelta(days=7)).isoformat()}
    result = await runtime.synchronize(OWNER, sync.SyncRequest.model_validate(body))
    assert len(result["items"]) == 2 and len(calls) == 3
    assert "private-calendar-details" not in json.dumps(result)
    read = await runtime.read_item(OWNER, "connection", key)
    assert read["item"]["content"]["description"] == "private-calendar-details"
    async with db_factory() as db:
        rows = (await db.execute(select(CalendarEventBinding))).scalars().all()
        assert {row.state for row in rows} == {"selected", "deleted"}
        assert all(row.consent_id == "calendar-grant" for row in rows)
    assert all(b"private-calendar-details" not in path.read_bytes() for path in workspace.rglob("*.enc"))
    old_revision = read["item"]["ref"]["revision"]
    async with db_factory() as db:
        (await db.get(Goal, "goal")).revision = 2
        old_grant = (await db.get(CalendarReadConsent, "calendar-grant")).model_dump()
    from src.api import calendar as calendar_api
    monkeypatch.setattr(calendar_api, "GoogleCalendarReadonlyAdapter", lambda connection, **kwargs: GoogleCalendarReadonlyAdapter(connection, transport=httpx.MockTransport(respond), resolver=lambda *_: ["8.8.8.8"], **kwargs))
    verified = await client.post("/api/calendar/connections/connection/verify", json={"expected_revision": 1, "idempotency_key": "calendar-new-grant-membership"})
    assert verified.status_code == 200, verified.text
    renewed = await client.post("/api/calendar/read-consents", json={
        "schema_version": 1, "connection_id": "connection", "calendar_id": "calendar",
        "goal_id": "goal", "goal_revision": 2,
        "allowed_fields": ["summary", "start", "end", "description", "attendees"],
        "window_minutes": 10080, "max_events": 50, "allow_remote_model": False,
        "acknowledge_sync_metadata": True,
        "expires_at": (timestamp + timedelta(minutes=30)).isoformat(), "idempotency_key": "calendar-new-grant"})
    assert renewed.status_code == 201, renewed.text
    new_id = renewed.json()["consent"]["consent_id"]
    body["request_uuid"] = "calendar-new-generation"
    new_start = datetime.now(timezone.utc)
    body["input"]["window"] = {"start": new_start.isoformat(), "end": (new_start + timedelta(days=6)).isoformat()}
    body["input"]["goal_ref"]["revision"] = 2
    body["input"]["source_scope"]["consents"] = [{"id": new_id, "revision": 1}]
    body["input"]["source_scope"]["reset_cursor"] = True
    await runtime.synchronize(OWNER, sync.SyncRequest.model_validate(body))
    assert (await runtime.read_item(OWNER, "connection", key))["item"]["ref"]["revision"] != old_revision
    async with db_factory() as db:
        assert (await db.get(CalendarReadConsent, "calendar-grant")).model_dump() == old_grant
        row = (await db.execute(select(CalendarEventBinding).where(CalendarEventBinding.event_key == key))).scalar_one()
        assert row.consent_id == new_id and row.revision == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_separate_process_cannot_contact_same_reserved_connection(source, async_db, monkeypatch):
    runtime, timestamp, db_factory, workspace = source
    entered = asyncio.Event()
    release = asyncio.Event()
    provider = Provider(timestamp, count=1)

    original_authority = runtime._authority
    paused = False
    async def reserve_then_wait(*args, **kwargs):
        nonlocal paused
        if kwargs.get("lease") is not None and not paused:
            paused = True
            entered.set()
            await release.wait()
        return await original_authority(*args, **kwargs)

    monkeypatch.setattr(runtime, "_authority", reserve_then_wait)
    install_provider(monkeypatch, provider)
    first_task = asyncio.create_task(runtime.synchronize(OWNER, request(timestamp, uuid="process-first")))
    await asyncio.wait_for(entered.wait(), timeout=5)
    async with db_factory() as db:
        database_url = str(db.bind.url)
    input_path = workspace / "process-input.json"
    input_path.write_text(request(timestamp, uuid="process-second").model_dump_json())
    input_path.chmod(0o600)
    environment = {name: value for name, value in os.environ.items() if not any(marker in name.upper() for marker in ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"))}
    try:
        result = await asyncio.to_thread(subprocess.run, [sys.executable, str(Path(__file__).parent / "fixtures" / "connection_sync_process.py"), database_url, str(workspace), str(input_path)], capture_output=True, text=True, timeout=30, env=environment)
        assert result.returncode == 0, result.stderr
        receipt = json.loads(result.stdout.strip().splitlines()[-1])
        assert receipt == {"code": "connection_sync_busy", "external_contacts": 0}
    finally:
        release.set()
        await first_task


@pytest.mark.asyncio
async def test_additive_sync_migration_preserves_legacy_grants_and_is_rerunnable():
    from sqlalchemy.ext.asyncio import create_async_engine
    from src.db.engine import _ensure_connection_sync_columns
    engine = create_async_engine("sqlite+aiosqlite://")
    try:
        async with engine.begin() as connection:
            await connection.exec_driver_sql("CREATE TABLE google_service_connections (connection_id TEXT PRIMARY KEY)")
            await connection.exec_driver_sql("CREATE TABLE mail_read_consents (consent_id TEXT PRIMARY KEY, max_messages INTEGER)")
            await connection.exec_driver_sql("CREATE TABLE calendar_read_consents (consent_id TEXT PRIMARY KEY, window_minutes INTEGER)")
            await connection.exec_driver_sql("INSERT INTO mail_read_consents VALUES ('legacy-mail', 10)")
            await connection.exec_driver_sql("INSERT INTO calendar_read_consents VALUES ('legacy-calendar', 60)")
            await _ensure_connection_sync_columns(connection)
            await _ensure_connection_sync_columns(connection)
            mail_row = (await connection.exec_driver_sql("SELECT max_messages, sync_metadata_limit FROM mail_read_consents")).one()
            calendar_row = (await connection.exec_driver_sql("SELECT window_minutes, sync_metadata_limit FROM calendar_read_consents")).one()
            assert tuple(mail_row) == (10, 0)
            assert tuple(calendar_row) == (60, 0)
            columns = {row[1] for row in (await connection.exec_driver_sql("PRAGMA table_info(google_service_connections)")).all()}
            assert "sync_active_job_id" in columns and "sync_cursor_job_id" in columns
            assert "sync_cursor_ciphertext" not in columns
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_real_asgi_sync_routes_reject_inactive_foreign_and_unacknowledged_read(source, monkeypatch, app, client):
    runtime, timestamp, _, _ = source
    provider = Provider(timestamp, count=1)
    install_provider(monkeypatch, provider)
    route = "/api/capabilities/mail/connections/connection/sync"
    headers = {"origin": "http://localhost:3001"}
    response = await client.get(route)
    assert response.status_code == 503 and provider.calls == []
    app.state.connection_sync_runtime = runtime
    response = await client.post(route, json=request(timestamp).model_dump(mode="json"), headers=headers)
    assert response.status_code == 200, response.text
    public = response.json()
    assert "private-subject" not in response.text
    status = await client.get(route)
    assert status.status_code == 200 and status.json()["state"] == "ready"
    opaque_id = public["items"][0]["opaque_id"]
    read = await client.post(route + "/items/" + opaque_id, json={}, headers=headers)
    assert read.status_code == 422
    read = await client.post(route + "/items/" + opaque_id, json={"acknowledge_private_read": True}, headers=headers)
    assert read.status_code == 200 and read.json()["item"]["content"]["subject"] == "private-subject"
    wrong_source = await client.get("/api/calendar/connections/connection/sync")
    assert wrong_source.status_code == 404
    malformed = request(timestamp).model_dump(mode="json")
    malformed["owner_principal_id"] = "foreign"
    response = await client.post(route, json=malformed, headers=headers)
    assert response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("token_response,error", [({"access_token": "fixture-access"}, "source_scope_missing"), ({"error": "invalid_grant"}, "mail_token_refresh_failed")])
async def test_missing_scope_or_expired_provider_token_is_visible_without_secret(source, monkeypatch, token_response, error, caplog):
    runtime, timestamp, _, _ = source
    calls = []
    def respond(req):
        calls.append(req)
        return httpx.Response(200 if "access_token" in token_response else 400, json=token_response)
    install_provider(monkeypatch, Provider(timestamp), transport=httpx.MockTransport(respond))
    with pytest.raises(sync.GmailReadError):
        await runtime.synchronize(OWNER, request(timestamp))
    status = await runtime.status(OWNER, "connection")
    assert status["state"] == "unknown_external_effect"
    assert status["last_error_code"] == error
    assert len(calls) == 1 and calls[0].url.path == "/token"
    assert status["cursor_revision"] == 0 and status["items"] == []
    assert not any(secret in caplog.text + json.dumps(status) for secret in ("fixture-refresh", "fixture-access"))


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_revocation_during_page_prevents_any_adoption_and_private_read(source, async_db, monkeypatch):
    runtime, timestamp, db_factory, _ = source
    provider = Provider(timestamp)
    revoked = False
    async def respond(req):
        nonlocal revoked
        if req.url.params.get("format") == "metadata" and not revoked:
            revoked = True
            async with db_factory() as db:
                consent = await db.get(MailReadConsent, "grant")
                consent.state = "revoked"
                consent.source_revision += 1
        return provider.response(req)
    install_provider(monkeypatch, provider, transport=httpx.MockTransport(respond))
    from fastapi import HTTPException
    with pytest.raises((sync.GmailReadError, HTTPException)):
        await runtime.synchronize(OWNER, request(timestamp))
    async with db_factory() as db:
        connection = await db.get(GoogleServiceConnection, "connection")
        assert connection.sync_cursor_revision == 0
        assert (await db.execute(select(MailMessageBinding))).scalars().all() == []
    assert len(provider.calls) <= 4


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_contact_timeout_remains_two_seconds_and_records_unknown(source, async_db, monkeypatch):
    runtime, timestamp, db_factory, _ = source
    provider = Provider(timestamp)
    async def respond(req):
        await asyncio.sleep(3)
        return provider.response(req)
    install_provider(monkeypatch, provider, transport=httpx.MockTransport(respond))
    started = asyncio.get_running_loop().time()
    with pytest.raises(sync.GmailReadError, match="authorization is unavailable"):
        await runtime.synchronize(OWNER, request(timestamp))
    elapsed = asyncio.get_running_loop().time() - started
    assert 1.9 <= elapsed < 5
    status = await runtime.status(OWNER, "connection")
    assert status["state"] == "unknown_external_effect" and status["cursor_revision"] == 0
    async with db_factory() as db:
        connection = await db.get(GoogleServiceConnection, "connection")
        root = await durable_job_repository.get_job(connection.sync_active_job_id)
    deadline = datetime.fromisoformat(root["deadline_at"])
    started_at = datetime.fromisoformat(root["started_at"])
    assert (deadline - started_at).total_seconds() <= 120


@pytest.mark.asyncio
async def test_real_asgi_provider_failure_is_typed_and_secret_free(source, monkeypatch, app, client):
    runtime, timestamp, _, _ = source
    app.state.connection_sync_runtime = runtime
    def respond(req):
        return httpx.Response(400, json={"error": "invalid_grant", "private": "fixture-refresh"})
    install_provider(monkeypatch, Provider(timestamp), transport=httpx.MockTransport(respond))
    response = await client.post("/api/capabilities/mail/connections/connection/sync", json=request(timestamp).model_dump(mode="json"), headers={"origin": "http://localhost:3001"})
    assert response.status_code == 502, response.text
    assert response.json()["detail"]["code"] == "mail_token_refresh_failed"
    assert "fixture-refresh" not in response.text


@pytest.mark.asyncio
async def test_mail_metadata_acknowledgement_creates_distinct_explicit_grant(source, client):
    runtime, timestamp, _, _ = source
    body = {"connection_id": "connection", "expected_connection_revision": 1, "goal_id": "goal", "expected_goal_revision": 1, "label_ids": ["label"], "expires_at": (timestamp + timedelta(hours=1)).isoformat(), "max_messages": 10, "allowed_body_fields": ["subject"], "acknowledge_source_read": True, "idempotency_key": "explicit-sync-grant"}
    route = "/api/capabilities/mail/read-consents"
    headers = {"origin": "http://localhost:3001"}
    accepted = await client.post(route, json={**body, "acknowledge_sync_metadata": True}, headers=headers)
    assert accepted.status_code in {200, 201}, accepted.text
    consent = accepted.json()["consent"]
    assert consent["sync_metadata_limit"] == 50 and consent["max_messages"] == 10
    private = request(timestamp, private=[message_key(OWNER.principal_id, "connection", "m0")]).model_dump(mode="json")
    private["input"]["source_scope"]["consents"] = [{"id": consent["consent_id"], "revision": consent["source_revision"]}]
    with pytest.raises(sync.SyncError, match="original body consent"):
        await runtime.synchronize(OWNER, sync.SyncRequest.model_validate(private))
    changed = await client.post(route, json=body, headers=headers)
    assert changed.status_code == 409
    legacy = await client.post(route, json={**body, "idempotency_key": "legacy-source-grant"}, headers=headers)
    assert legacy.status_code in {200, 201}, legacy.text
    assert legacy.json()["consent"]["sync_metadata_limit"] == 0


@pytest.mark.asyncio
async def test_physical_owner_requires_exact_awaited_callback_not_elapsed_time():
    from src.integrations.native_physical_owner import NativeCallbackOwners, process_identity
    owners = NativeCallbackOwners()
    witness = {**process_identity(), "runtime_nonce": owners.nonce}
    entered = asyncio.Event()
    release = asyncio.Event()
    async def callback():
        owners.bind("job", witness)
        entered.set()
        await release.wait()
    task = asyncio.create_task(callback())
    await entered.wait()
    assert owners.proof("job", witness) is None
    release.set()
    await task
    assert owners.proof("job", witness) is None
    owners.awaited.add(task)
    assert owners.proof("job", witness) == "owned_positive_close"
    assert owners.proof("job", {**witness, "runtime_nonce": "other"}) is None


def test_live_or_foreign_namespace_process_is_not_positive_death():
    from src.integrations.native_physical_owner import positive_owner_death, process_identity
    witness = process_identity()
    assert positive_owner_death(witness) is None
    assert positive_owner_death({**witness, "pid_namespace": witness["pid_namespace"] + 1}) is None
    assert positive_owner_death({**witness, "pid_start_ticks": str(int(witness["pid_start_ticks"]) + 1)}) == "positive_process_death"


@pytest.mark.asyncio
@pytest.mark.parametrize("goal_change", ["after_unknown", "during_contact"])
async def test_physical_cleanup_after_goal_change_preserves_external_unknown(source, monkeypatch, goal_change):
    runtime, timestamp, db_factory, _ = source
    provider = Provider(timestamp, fail_page=2 if goal_change == "after_unknown" else None)
    changed = False
    async def respond(req):
        nonlocal changed
        response = provider.response(req)
        if goal_change == "during_contact" and req.url.path == "/token" and not changed:
            changed = True
            async with db_factory() as db:
                goal = await db.get(Goal, "goal")
                goal.revision += 1
        return response
    install_provider(monkeypatch, provider, transport=httpx.MockTransport(respond))
    with pytest.raises((sync.GmailReadError, HTTPException, BoardError)):
        await runtime.synchronize(OWNER, request(timestamp))
    async with db_factory() as db:
        connection = await db.get(GoogleServiceConnection, "connection")
        job_id, cursor_revision = connection.sync_active_job_id, connection.sync_cursor_revision
        if goal_change == "after_unknown":
            goal = await db.get(Goal, "goal")
            goal.revision += 1
    root = await durable_job_repository.get_job(job_id)
    assert root["status"] == ("unknown_external_effect" if goal_change == "after_unknown" else "running")
    assert any(effect.get("details", {}).get("contact_class") == "oauth_refresh_post_and_source_get" and not effect.get("details", {}).get("read_only") for effect in root["effects"])
    contacts = len(provider.calls)
    monkeypatch.setattr(runtime, "_page", lambda *_: (_ for _ in ()).throw(AssertionError("cleanup must not decrypt a page")))
    released = await runtime.reconcile(OWNER, "connection", job_id, root["revision"], expected_cursor_revision=cursor_revision, authenticated_token_hash="fixture-only")
    assert released["physical_slot_released"] and released["provider_contacts"] == 0
    unchanged = await durable_job_repository.get_job(job_id)
    assert unchanged["status"] == root["status"] and unchanged["effects"] == root["effects"]
    assert len(provider.calls) == contacts
    async with db_factory() as db:
        connection = await db.get(GoogleServiceConnection, "connection")
        assert connection.sync_active_job_id is None and connection.sync_cursor_revision == cursor_revision
    monkeypatch.delattr(runtime, "_page")
    status = await runtime.status(OWNER, "connection")
    assert status["reservation_state"] == "available"
    assert status["unresolved_jobs"][0]["job_id"] == job_id
    assert status["unresolved_jobs"][0]["external_effect_state"] == "unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["principal", "pointer", "revision", "witness"])
async def test_physical_cleanup_rejects_mismatched_original_proof(source, monkeypatch, mismatch):
    runtime, timestamp, db_factory, _ = source
    provider = Provider(timestamp, fail_page=2)
    install_provider(monkeypatch, provider)
    with pytest.raises(sync.GmailReadError):
        await runtime.synchronize(OWNER, request(timestamp))
    async with db_factory() as db:
        connection = await db.get(GoogleServiceConnection, "connection")
        job_id, cursor_revision = connection.sync_active_job_id, connection.sync_cursor_revision
        if mismatch == "pointer":
            connection.sync_active_job_id = "other-root"
    root = await durable_job_repository.get_job(job_id)
    contacts = len(provider.calls)
    actor = WorkBoardOwner(principal_id="other-principal", session_id=OWNER.session_id) if mismatch == "principal" else OWNER
    if mismatch == "witness":
        runtime._physical_owners.callbacks.clear()
    with pytest.raises((sync.GmailReadError, DurableJobError)):
        await runtime.reconcile(actor, "connection", job_id, root["revision"], expected_cursor_revision=cursor_revision + (mismatch == "revision"), authenticated_token_hash="fixture-only")
    unchanged = await durable_job_repository.get_job(job_id)
    assert unchanged["revision"] == root["revision"] and unchanged["effects"] == root["effects"]
    assert len(provider.calls) == contacts


@pytest.mark.asyncio
async def test_actual_login_enrollment_is_required_before_sync_job_and_contact(source, monkeypatch, client, app):
    runtime, timestamp, db_factory, _ = source
    app.state.connection_sync_runtime = runtime
    monkeypatch.setattr(settings, "operator_auth_secret", "sync-enrollment-fixture-password")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "test")
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", "http://localhost:3001")
    monkeypatch.setattr(settings, "operator_auth_cookie_secure", False)
    headers = {"origin": "http://localhost:3001"}
    from src.api.auth import _reset_login_throttle_for_tests
    _reset_login_throttle_for_tests()
    login = await client.post("/api/auth/login", json={"password": "sync-enrollment-fixture-password"}, headers=headers)
    assert login.status_code == 200, login.text
    actor = login.json()
    async with db_factory() as db:
        root = await db.get(OperatorSession, actor["session_id"])
        assert root.operator_identity_id is None
        db.add(Session(id=actor["session_id"]))
        for model, key in ((Goal, "goal"), (GoogleServiceConnection, "connection"), (MailReadConsent, "grant"), (MailLabelBinding, "label")):
            row = await db.get(model, key)
            row.owner_principal_id = actor["principal_id"]
            row.owner_session_id = actor["session_id"]
    provider = Provider(timestamp, count=1)
    install_provider(monkeypatch, provider)
    route = "/api/capabilities/mail/connections/connection/sync"
    body = request(timestamp).model_dump(mode="json")
    blocked = await client.post(route, json=body, headers=headers)
    assert blocked.status_code == 403 and blocked.json()["detail"]["code"] == "source_sync_operator_continuity_required"
    assert provider.calls == []
    async with db_factory() as db:
        assert (await db.execute(select(WorkflowRunState))).scalars().all() == []
    enrolled = await client.post("/api/auth/ownership/enroll", headers=headers)
    assert enrolled.status_code == 200, enrolled.text
    accepted = await client.post(route, json=body, headers=headers)
    assert accepted.status_code == 200, accepted.text
    assert len(accepted.json()["items"]) == 1
    contacts = len(provider.calls)
    async with db_factory() as db:
        root = await db.get(OperatorSession, actor["session_id"])
        identity = await db.get(OperatorIdentity, root.operator_identity_id)
        identity.revoked_at = datetime.now(timezone.utc)
    revoked = await client.post(route, json={**body, "request_uuid": "revoked-identity"}, headers=headers)
    assert revoked.status_code == 403 and len(provider.calls) == contacts
    _reset_login_throttle_for_tests()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["before_journal", "after_journal"])
async def test_failure_around_physical_journal_never_strands_connection_pointer(source, monkeypatch, boundary):
    runtime, timestamp, db_factory, _ = source
    original = durable_job_repository.reserve_native_physical_resource
    async def interrupted(*args, **kwargs):
        if boundary == "after_journal":
            await original(*args, **kwargs)
        raise RuntimeError("fixture process interruption before pointer")
    monkeypatch.setattr(durable_job_repository, "reserve_native_physical_resource", interrupted)
    provider = Provider(timestamp)
    install_provider(monkeypatch, provider)
    with pytest.raises(RuntimeError, match="before pointer"):
        await runtime.synchronize(OWNER, request(timestamp, reset=True))
    assert provider.calls == []
    async with db_factory() as db:
        connection = await db.get(GoogleServiceConnection, "connection")
        assert connection.sync_active_job_id is None and connection.sync_scope_digest == "" and connection.sync_cursor_revision == 0
        roots = (await db.execute(select(WorkflowRunState))).scalars().all()
        assert len(roots) == 1
        reservations = [item for item in json.loads(roots[0].checkpoint_receipts_json) if item.get("checkpoint_id") == "native-physical-resource-reservation"]
        assert len(reservations) == (boundary == "after_journal")


@pytest.mark.asyncio
async def test_unsupported_process_identity_is_typed_before_admission(source, monkeypatch, app, client):
    runtime, timestamp, db_factory, _ = source
    app.state.connection_sync_runtime = runtime
    def unsupported():
        raise FileNotFoundError("fixture macOS host has no /proc")
    monkeypatch.setattr(sync, "process_identity", unsupported)
    provider = Provider(timestamp)
    install_provider(monkeypatch, provider)
    response = await client.post("/api/capabilities/mail/connections/connection/sync", json=request(timestamp).model_dump(mode="json"), headers={"origin": "http://localhost:3001"})
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "connection_sync_platform_unsupported"
    assert "/proc" not in response.text and provider.calls == []
    async with db_factory() as db:
        assert (await db.execute(select(WorkflowRunState))).scalars().all() == []
        assert (await db.get(GoogleServiceConnection, "connection")).sync_active_job_id is None


@pytest.mark.asyncio
async def test_physical_reservation_is_durable_before_first_provider_contact(source, monkeypatch):
    runtime, timestamp, db_factory, _ = source
    provider = Provider(timestamp, count=1)
    async def respond(req):
        if req.url.path == "/token":
            async with db_factory() as db:
                connection = await db.get(GoogleServiceConnection, "connection")
                assert connection.sync_active_job_id is not None
                root = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == connection.sync_active_job_id))).scalar_one()
                reservations = [item for item in json.loads(root.checkpoint_receipts_json) if item.get("checkpoint_id") == "native-physical-resource-reservation"]
                assert len(reservations) == 1 and reservations[0]["payload"]["witness"]["scope_digest"] == connection.sync_scope_digest
        return provider.response(req)
    install_provider(monkeypatch, provider, transport=httpx.MockTransport(respond))
    result = await runtime.synchronize(OWNER, request(timestamp))
    assert result["status"] == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_dead_original_process_positive_identity_releases_only_physical_slot(source, async_db):
    runtime, timestamp, db_factory, workspace = source
    async with db_factory() as db:
        database_url = str(db.bind.url)
    input_path = workspace / "owned-process-request.json"
    input_path.write_text(request(timestamp, uuid="old-process").model_dump_json())
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    result = await asyncio.to_thread(subprocess.run, [sys.executable, str(Path(__file__).parent / "fixtures" / "connection_sync_process.py"), database_url, str(workspace), str(input_path), "unknown-owner"], capture_output=True, text=True, timeout=30, env=environment)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1])["external_contacts"] == 0
    async with db_factory() as db:
        connection = await db.get(GoogleServiceConnection, "connection")
        job_id = connection.sync_active_job_id
    root = await durable_job_repository.get_job(job_id)
    assert root["status"] == "unknown_external_effect" and job_id not in runtime._physical_owners.callbacks
    released = await runtime.reconcile(OWNER, "connection", job_id, root["revision"], expected_cursor_revision=0, authenticated_token_hash="fixture-only")
    assert released["provider_contacts"] == 0 and released["physical_slot_released"]
    unchanged = await durable_job_repository.get_job(job_id)
    assert unchanged["status"] == "unknown_external_effect" and unchanged["effects"] == root["effects"]
    receipts = [item for item in unchanged["checkpoints"] if item.get("checkpoint_id") == "native-physical-resource-cleanup"]
    assert receipts[0]["payload"]["proof_kind"] == "positive_process_death"


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_process_crash_before_first_effect_preserves_none_and_releases_capacity(source, async_db):
    runtime, timestamp, db_factory, workspace = source
    async with db_factory() as db:
        database_url = str(db.bind.url)
    input_path = workspace / "owned-pre-effect-request.json"
    input_path.write_text(request(timestamp, uuid="pre-effect-process").model_dump_json())
    result = await asyncio.to_thread(subprocess.run, [sys.executable, str(Path(__file__).parent / "fixtures" / "connection_sync_process.py"), database_url, str(workspace), str(input_path), "crash-before-effect"], capture_output=True, text=True, timeout=30, env=dict(os.environ))
    assert result.returncode == 17, result.stderr
    async with db_factory() as db:
        connection = await db.get(GoogleServiceConnection, "connection")
        job_id = connection.sync_active_job_id
    root = await durable_job_repository.get_job(job_id)
    assert root["status"] == "running" and root["effects"] == []
    released = await runtime.reconcile(OWNER, "connection", job_id, root["revision"], expected_cursor_revision=0, authenticated_token_hash="fixture-only")
    assert released["status"] == "running" and released["provider_contacts"] == 0
    unchanged = await durable_job_repository.get_job(job_id)
    assert unchanged["effects"] == [] and unchanged["status"] == "running"
    status = await runtime.status(OWNER, "connection")
    assert status["reservation_state"] == "available" and status["external_effect_state"] == "none"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["failed", "succeeded"])
async def test_transition_before_pointer_clear_remains_physically_recoverable(source, monkeypatch, status):
    runtime, timestamp, db_factory, _ = source
    provider = Provider(timestamp, count=1)
    install_provider(monkeypatch, provider)
    async def interrupt_release(*args, **kwargs):
        raise RuntimeError("fixture transition committed before pointer clear")
    monkeypatch.setattr(runtime, "_release", interrupt_release)
    if status == "failed":
        original_ready = runtime._ready
        ready_calls = 0
        def fail_after_pointer():
            nonlocal ready_calls
            ready_calls += 1
            if ready_calls > 1:
                raise sync.SyncError("fixture_no_contact", "fixture pre-contact failure")
            original_ready()
        monkeypatch.setattr(runtime, "_ready", fail_after_pointer)
    with pytest.raises((RuntimeError, sync.SyncError)):
        await runtime.synchronize(OWNER, request(timestamp))
    async with db_factory() as db:
        connection = await db.get(GoogleServiceConnection, "connection")
        job_id, cursor_revision = connection.sync_active_job_id, connection.sync_cursor_revision
    root = await durable_job_repository.get_job(job_id)
    assert root["status"] == status
    if status == "failed":
        monkeypatch.delattr(runtime, "_ready")
    contacts = len(provider.calls)
    monkeypatch.setattr(runtime, "_page", lambda *_: (_ for _ in ()).throw(AssertionError("cleanup must not read artifacts")))
    released = await runtime.reconcile(OWNER, "connection", job_id, root["revision"], expected_cursor_revision=cursor_revision, authenticated_token_hash="fixture-only")
    assert released["status"] == status and released["physical_slot_released"]
    assert len(provider.calls) == contacts
    unchanged = await durable_job_repository.get_job(job_id)
    assert unchanged["effects"] == root["effects"] and unchanged["lease"] == {**root["lease"], "revision": unchanged["revision"]}
    replay = await runtime.reconcile(OWNER, "connection", job_id, unchanged["revision"], expected_cursor_revision=cursor_revision, authenticated_token_hash="fixture-only")
    assert replay["physical_slot_released"] and (await durable_job_repository.get_job(job_id))["revision"] == unchanged["revision"]


@pytest.mark.asyncio
async def test_fresh_goal_and_api_grant_reset_rebinds_current_item_without_renewing_unknown(source, monkeypatch, app, client):
    runtime, timestamp, db_factory, _ = source
    provider = Provider(timestamp, fail_page=2)
    install_provider(monkeypatch, provider)
    key = message_key(OWNER.principal_id, "connection", "m0")
    with pytest.raises(sync.GmailReadError):
        await runtime.synchronize(OWNER, request(timestamp, private=[key]))
    old_read = await runtime.read_item(OWNER, "connection", key)
    async with db_factory() as db:
        connection = await db.get(GoogleServiceConnection, "connection")
        old_id, cursor_revision = connection.sync_active_job_id, connection.sync_cursor_revision
        old_grant = await db.get(MailReadConsent, "grant")
        original_grant = old_grant.model_dump()
        goal = await db.get(Goal, "goal")
        goal.revision = 2
    old_root = await durable_job_repository.get_job(old_id)
    await runtime.reconcile(OWNER, "connection", old_id, old_root["revision"], expected_cursor_revision=cursor_revision, authenticated_token_hash="fixture-only")
    old_after_release = await durable_job_repository.get_job(old_id)
    with pytest.raises((BoardError, HTTPException, sync.SyncError)):
        await runtime.read_item(OWNER, "connection", key)
    response = await client.post("/api/capabilities/mail/read-consents", json={
        "connection_id": "connection", "expected_connection_revision": 1,
        "goal_id": "goal", "expected_goal_revision": 2, "label_ids": ["label"],
        "expires_at": (timestamp + timedelta(minutes=30)).isoformat(),
        "acknowledge_source_read": True, "acknowledge_sync_metadata": True,
        "allowed_body_fields": ["plainbody"], "idempotency_key": "new-grant"})
    assert response.status_code == 201, response.text
    new_grant_id = response.json()["consent"]["consent_id"]
    body = request(timestamp, uuid="new-goal-new-root", private=[key], reset=True).model_dump(mode="json")
    body["input"]["goal_ref"]["revision"] = 2
    body["input"]["source_scope"]["consents"] = [{"id": new_grant_id, "revision": 1}]
    provider.fail_page = None
    new_result = await runtime.synchronize(OWNER, sync.SyncRequest.model_validate(body))
    new_read = await runtime.read_item(OWNER, "connection", key)
    assert new_read["item"]["content"]["body"] == "private-body"
    assert new_read["item"]["ref"]["revision"] != old_read["item"]["ref"]["revision"]
    assert new_result["job_id"] != old_id
    unchanged = await durable_job_repository.get_job(old_id)
    assert unchanged == old_after_release
    assert unchanged["status"] == "unknown_external_effect"
    async with db_factory() as db:
        assert (await db.get(MailReadConsent, "grant")).model_dump() == original_grant
        row = (await db.execute(select(MailMessageBinding).where(MailMessageBinding.message_key == key))).scalar_one()
        assert row.source_consent_id == new_grant_id and row.revision == 2
    from src.extensions.source_operations import collect_connected_source_items
    with pytest.raises(sync.SyncError, match="citation changed"):
        await collect_connected_source_items(runtime, OWNER, "connection", [old_read["item"]["ref"]])


@pytest.mark.asyncio
async def test_darwin_fixture_same_process_unknown_cleanup_without_proc(source, monkeypatch):
    """Darwin ABI fixture on this host; not a native macOS execution receipt."""
    from src.integrations import native_physical_owner as native
    runtime, timestamp, db_factory, _ = source
    monkeypatch.setattr(native, "_host_platform", lambda: "darwin")
    monkeypatch.setattr(native, "_darwin_boot_id", lambda: "12345678-1234-4234-8234-123456789abc")
    monkeypatch.setattr(native, "_darwin_start", lambda pid: (1700000000, 42))
    provider = Provider(timestamp, fail_page=2)
    install_provider(monkeypatch, provider)
    with pytest.raises(sync.GmailReadError):
        await runtime.synchronize(OWNER, request(timestamp))
    async with db_factory() as db:
        connection = await db.get(GoogleServiceConnection, "connection")
        job_id, cursor_revision = connection.sync_active_job_id, connection.sync_cursor_revision
    root = await durable_job_repository.get_job(job_id)
    reservation = next(item for item in root["checkpoints"] if item["checkpoint_id"] == "native-physical-resource-reservation")
    assert reservation["payload"]["witness"]["platform"] == "darwin"
    assert "pid_namespace" not in reservation["payload"]["witness"]
    contacts = len(provider.calls)
    released = await runtime.reconcile(OWNER, "connection", job_id, root["revision"], expected_cursor_revision=cursor_revision, authenticated_token_hash="fixture-only")
    assert released["physical_slot_released"]
    assert len(provider.calls) == contacts
    assert (await durable_job_repository.get_job(job_id))["effects"] == root["effects"]


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["checkpoint_sha", "readback_page", "unresolved_effect", "readback_target", "unverified_detail"])
async def test_succeeded_cleanup_rejects_changed_final_readback_tuple(source, monkeypatch, mismatch):
    runtime, timestamp, db_factory, _ = source
    provider = Provider(timestamp, count=1)
    install_provider(monkeypatch, provider)
    async def interrupt_release(*args, **kwargs):
        raise RuntimeError("fixture transition before pointer clear")
    monkeypatch.setattr(runtime, "_release", interrupt_release)
    with pytest.raises(RuntimeError):
        await runtime.synchronize(OWNER, request(timestamp))
    async with db_factory() as db:
        connection = await db.get(GoogleServiceConnection, "connection")
        job_id, cursor_revision = connection.sync_active_job_id, connection.sync_cursor_revision
        row = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id))).scalar_one()
        if mismatch == "checkpoint_sha":
            checkpoints = json.loads(row.checkpoint_receipts_json)
            next(item for item in checkpoints if item["checkpoint_id"] == "connection-sync-page-1")["payload"]["artifact_sha256"] = "0" * 64
            row.checkpoint_receipts_json = json.dumps(checkpoints)
        else:
            effects = json.loads(row.effect_receipts_json)
            readback = next(item for item in effects if item.get("receipt_kind") == "readback")
            if mismatch == "readback_page":
                readback["details"]["page"] = 2
            elif mismatch == "readback_target":
                readback["target_digest"] = "0" * 64
            elif mismatch == "unverified_detail":
                readback["details"]["verified"] = False
            else:
                effects.remove(readback)
            row.effect_receipts_json = json.dumps(effects)
    root = await durable_job_repository.get_job(job_id)
    contacts = len(provider.calls)
    with pytest.raises(sync.SyncError):
        await runtime.reconcile(OWNER, "connection", job_id, root["revision"], expected_cursor_revision=cursor_revision, authenticated_token_hash="fixture-only")
    assert len(provider.calls) == contacts
    async with db_factory() as db:
        assert (await db.get(GoogleServiceConnection, "connection")).sync_active_job_id == job_id
