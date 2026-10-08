"""ASGI contract tests for the owner-bound Calendar M5 surface.

These tests deliberately exercise the real FastAPI routes, canonical SQLite
fixture, encrypted vault repository, and the real Google adapter.  Provider
contact is replaced only at the pinned transport seam with bounded
provider-shaped responses; no Calendar account or network is contacted.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from config.settings import settings
from src.db.models import CalendarEventBinding, CalendarReadConsent, Goal, GovernedScheduleBinding, GovernedScheduleOccurrence, GoogleServiceConnection, OperatorSession, ScheduledJob, WorkBoardInputArtifact, WorkBoardTask, WorkflowRunState
from src.security.http_transport import PinnedResponse
from src.vault import vault_repository
from src.api.calendar import ConnectionCreate, _connection_credential_fingerprint, _connection_request_digest
from src.work_board.repository import BoardError
from src.integrations.calendar_controls import (
    CONTROL_MAX_RUNTIME_SECONDS,
    CalendarControlReconciliationRequired,
    CalendarControlRequest,
    control_job_id,
    run_control,
)
import src.integrations.calendar_controls as calendar_controls
from src.workflows.job_runtime import durable_job_repository


ORIGIN_HEADERS = {"origin": "http://localhost:3001"}
OWNER_PRINCIPAL = "operator:test-bypass"
OWNER_SESSION = "test-auth-bypass"
MAX_SETUP_BYTES = 16 * 1024


@pytest.fixture(autouse=True)
def enable_test_operator_bypass(monkeypatch):
    """Use the middleware's explicit test identity, never a client identity."""

    monkeypatch.setattr(settings, "deployment_environment", "test")
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", True)
    monkeypatch.setattr(settings, "operator_auth_secret", "")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "test,localhost,127.0.0.1")
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", "http://localhost:3001")


@pytest_asyncio.fixture(autouse=True)
async def seed_persisted_bypass_operator_session(async_db):
    """Keep the synthetic test identity backed by the canonical session row."""

    now = datetime.now(timezone.utc)
    async with async_db() as db:
        db.add(
            OperatorSession(
                id=OWNER_SESSION,
                token_hash="calendar-test-bypass-token-hash",
                last_seen_at=now,
                idle_expires_at=now + timedelta(hours=1),
                absolute_expires_at=now + timedelta(hours=1),
            )
        )


class _ChunkedBody(httpx.AsyncByteStream):
    def __init__(self, body: bytes, *, chunk_size: int = 1024):
        self.body = body
        self.chunk_size = chunk_size

    async def __aiter__(self):
        for offset in range(0, len(self.body), self.chunk_size):
            yield self.body[offset : offset + self.chunk_size]


class _CountingBody(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]):
        self.chunks = chunks
        self.consumed = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.consumed += 1
            yield chunk


class _GoogleTransportScript:
    """Provider-shaped transport responses consumed by the real adapter."""

    def __init__(self, *, event: dict[str, Any] | None = None, calendar_items: list[dict[str, Any]] | None = None, events: list[dict[str, Any]] | None = None):
        self.calls: list[dict[str, Any]] = []
        self.calendar_items = calendar_items or [{"id": "owner-calendar", "summary": "Operator calendar"}]
        self.event = event or {
            "id": "provider-event-opaque",
            "etag": '"event-etag-1"',
            "updated": "2026-09-30T08:00:00Z",
            "summary": "Planning review",
            "start": {"dateTime": "2026-09-30T09:00:00Z"},
            "end": {"dateTime": "2026-09-30T10:00:00Z"},
            "location": "Room 4",
            "description": "Bring the current operator brief",
            "attendees": [{"displayName": "Operator"}],
        }
        self.events = events or [self.event]

    async def __call__(self, url: str, **kwargs: Any) -> PinnedResponse:
        method = str(kwargs.get("method", "GET")).upper()
        self.calls.append({"url": url, "method": method, "headers": dict(kwargs.get("headers") or {})})
        if url == "https://oauth2.googleapis.com/token" and method == "POST":
            payload = {"access_token": "transport-access-token"}
        elif "/calendar/v3/users/me/calendarList" in url and method == "GET":
            payload = {
                "etag": '"calendar-list-etag"',
                "items": list(self.calendar_items),
            }
        elif "/calendar/v3/calendars/owner-calendar/events" in url and method == "GET":
            payload = {"etag": '"events-etag"', "items": list(self.events)}
        else:
            raise AssertionError(f"unexpected provider request: {method} {url}")
        return PinnedResponse(
            url=url,
            status_code=200,
            headers={"content-type": "application/json"},
            content=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            pinned_address="8.8.8.8",
        )


def _utc_after(days: int = 1) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat().replace("+00:00", "Z")


def _safe_detail(response: httpx.Response, *, status: int | tuple[int, ...] | None = None) -> dict[str, Any]:
    if status is not None:
        expected = (status,) if isinstance(status, int) else status
        assert response.status_code in expected, response.text
    payload = response.json()
    assert set(payload) == {"detail"}
    detail = payload["detail"]
    assert set(detail) == {"code", "message", "recovery_action"}
    assert isinstance(detail["code"], str) and len(detail["code"]) <= 128
    assert isinstance(detail["message"], str) and 0 < len(detail["message"]) <= 500
    assert detail["recovery_action"] is None or (
        isinstance(detail["recovery_action"], str) and len(detail["recovery_action"]) <= 128
    )
    return detail


async def _calendar_prep_fixture(client, async_db, monkeypatch, *, prefix: str) -> tuple[_GoogleTransportScript, dict[str, Any]]:
    await _seed_goal(async_db)
    provider = _GoogleTransportScript()
    monkeypatch.setattr("src.integrations.google_calendar.request_pinned_https", provider)
    connection = await _create_connection(client, key=f"{prefix}-connection-key")
    verify = await client.post(
        f"/api/calendar/connections/{connection['connection_id']}/verify",
        headers=ORIGIN_HEADERS,
        json={"expected_revision": connection["revision"], "idempotency_key": f"{prefix}-verify-key"},
    )
    assert verify.status_code in {200, 201}, verify.text
    consent = await _create_consent(
        client,
        connection_id=connection["connection_id"],
        key=f"{prefix}-consent-key",
    )
    events_response = await client.get(
        f"/api/calendar/connections/{connection['connection_id']}/events",
        params={"consent_id": consent["consent_id"], "max_events": 10},
    )
    assert events_response.status_code == 200, events_response.text
    event = events_response.json()["events"][0]
    return provider, {
        "schema_version": 1,
        "input": {
            "schema_version": 1,
            "consent_id": consent["consent_id"],
            "event_binding_id": event["event_binding_id"],
            "expected_event_binding_revision": event["event_binding_revision"],
            "expected_consent_revision": consent["revision"],
            "expected_connection_revision": connection["revision"],
            "event_revision": event["event_revision"],
            "calendar_list_revision": event["calendar_list_revision"],
            "goal_id": "calendar-goal",
            "goal_revision": 1,
            "purpose": "Prepare the operator meeting brief",
        },
        "title": "Prepare meeting brief",
        "idempotency_key": f"{prefix}-prep-key",
    }


async def _seed_goal(async_db, goal_id: str = "calendar-goal") -> Goal:
    async with async_db() as db:
        goal = Goal(
            id=goal_id,
            title="Calendar operator goal",
            status="active",
            revision=1,
            owner_principal_id=OWNER_PRINCIPAL,
            owner_session_id=OWNER_SESSION,
        )
        db.add(goal)
        await db.flush()
        return goal


async def _seed_running_occurrence(async_db, *, connection_id: str) -> tuple[str, str, str]:
    """Create one active schedule slot without contacting a provider."""

    async with async_db() as db:
        consent = CalendarReadConsent(
            owner_principal_id=OWNER_PRINCIPAL,
            owner_session_id=OWNER_SESSION,
            connection_id=connection_id,
            creation_idempotency_key="seed-occurrence-consent-key",
            creation_request_digest="sha256:" + "1" * 64,
            connection_revision=2,
            calendar_id="encrypted-calendar-id",
            goal_id="calendar-goal",
            goal_revision=1,
            allowed_fields_json='["summary","start","end"]',
            window_minutes=60,
            max_events=5,
            allow_remote_model=False,
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            state="active",
            revision=1,
            consent_digest="sha256:" + "2" * 64,
        )
        db.add(consent)
        job = ScheduledJob(
            name="Calendar active occurrence",
            enabled=True,
            trigger_type="governed",
            trigger_spec_json='{"kind":"hourly","timezone":"UTC","daily_hour":null,"daily_minute":null}',
            action_type="calendar.observe_due_events.v1",
            action_spec_json='{"binding_id":"pending"}',
        )
        db.add(job)
        await db.flush()
        binding = GovernedScheduleBinding(
            scheduled_job_id=job.id,
            owner_principal_id=OWNER_PRINCIPAL,
            owner_session_id=OWNER_SESSION,
            goal_id="calendar-goal",
            goal_revision=1,
            capability_id="calendar.observe_due_events.v1",
            action_type="calendar.observe_due_events.v1",
            input_artifact_id="calendar-occurrence-artifact",
            input_digest="sha256:" + "3" * 64,
            action_digest="sha256:" + "4" * 64,
            consent_kind="calendar_read",
            read_consent_id=consent.consent_id,
            consent_revision=1,
            consent_digest=consent.consent_digest,
            schedule_idempotency_key="seed-occurrence-schedule-key",
            schedule_request_digest="sha256:" + "5" * 64,
            cadence_kind="hourly",
            timezone="UTC",
            binding_revision=1,
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            state="active",
        )
        db.add(binding)
        await db.flush()
        occurrence = GovernedScheduleOccurrence(
            binding_id=binding.binding_id,
            binding_revision=binding.binding_revision,
            slot_utc=datetime.now(timezone.utc),
            idempotency_key="seed-occurrence-key",
            request_digest="sha256:" + "6" * 64,
            claim_token="active-claim-token",
            fencing_token=1,
            lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=4),
            state="running",
            metadata_json='{"provider_contact":"in_progress"}',
        )
        db.add(occurrence)
        await db.flush()
        return consent.consent_id, binding.binding_id, occurrence.occurrence_id


async def _create_connection(client, *, key: str = "calendar-connection-key", secret: str = "refresh-token-only") -> dict[str, Any]:
    response = await client.post(
        "/api/calendar/connections",
        headers=ORIGIN_HEADERS,
        json={
            "schema_version": 1,
            "service": "calendar_readonly",
            "label": "Operator Google",
            "client_id": "client-id-opaque",
            "client_secret": "client-secret-opaque",
            "refresh_token": secret,
            "idempotency_key": key,
        },
    )
    assert response.status_code in {200, 201}, response.text
    payload = response.json()
    assert set(payload) == {"connection"}
    return payload["connection"]


async def _create_consent(client, *, connection_id: str, goal_id: str = "calendar-goal", key: str = "calendar-consent-key", allow_remote_model: bool = True) -> dict[str, Any]:
    response = await client.post(
        "/api/calendar/read-consents",
        headers=ORIGIN_HEADERS,
        json={
            "schema_version": 1,
            "connection_id": connection_id,
            "calendar_id": "owner-calendar",
            "goal_id": goal_id,
            "goal_revision": 1,
            "allowed_fields": ["summary", "start", "end", "location"],
            "window_minutes": 120,
            "max_events": 10,
            "allow_remote_model": allow_remote_model,
            "expires_at": _utc_after(1),
            "idempotency_key": key,
        },
    )
    assert response.status_code in {200, 201}, response.text
    payload = response.json()
    assert set(payload) == {"consent"}
    return payload["consent"]


async def _login_with_persisted_operator_session(client, monkeypatch) -> str:
    monkeypatch.setattr(settings, "operator_auth_secret", "calendar-live-auth-secret")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "operator_auth_allowed_hosts", "test")
    monkeypatch.setattr(settings, "operator_auth_allowed_origins", "http://localhost:3001")
    monkeypatch.setattr(settings, "operator_auth_cookie_secure", False)
    response = await client.post(
        "/api/auth/login",
        headers={"host": "test", "origin": "http://localhost:3001"},
        json={"password": "calendar-live-auth-secret"},
    )
    assert response.status_code == 200, response.text
    token = response.cookies.get(settings.operator_auth_cookie_name)
    assert token
    client.cookies.set(settings.operator_auth_cookie_name, token)
    return token


def _assert_connection_shape(connection: dict[str, Any]) -> None:
    assert set(connection) == {
        "connection_id",
        "service",
        "label",
        "credential_fingerprint",
        "state",
        "revision",
        "created_at",
        "updated_at",
    }
    assert connection["service"] == "calendar_readonly"
    assert connection["state"] == "active"
    assert "refresh" not in json.dumps(connection).casefold()
    assert "client" not in json.dumps(connection).casefold()


@pytest.mark.asyncio
async def test_connection_setup_uses_encrypted_vault_and_exact_write_only_metadata(client, async_db):
    secret = "refresh-token-write-only-9f4e"
    connection = await _create_connection(client, secret=secret)
    _assert_connection_shape(connection)
    response_text = json.dumps(connection)
    assert secret not in response_text
    assert "client-secret-opaque" not in response_text

    async with async_db() as db:
        row = (
            await db.execute(
                select(GoogleServiceConnection).where(
                    GoogleServiceConnection.connection_id == connection["connection_id"],
                    GoogleServiceConnection.owner_principal_id == OWNER_PRINCIPAL,
                    GoogleServiceConnection.owner_session_id == OWNER_SESSION,
                )
            )
        ).scalar_one()
        assert row.vault_secret_key.startswith("calendar:")
        assert secret not in row.vault_secret_key
        vault_key = row.vault_secret_key
    stored = await vault_repository.get(vault_key)
    assert stored is not None
    assert json.loads(stored) == {
        "client_id": "client-id-opaque",
        "client_secret": "client-secret-opaque",
        "refresh_token": secret,
    }

    replay = await client.post(
        "/api/calendar/connections",
        headers=ORIGIN_HEADERS,
        json={
            "schema_version": 1,
            "service": "calendar_readonly",
            "label": "Operator Google",
            "client_id": "client-id-opaque",
            "client_secret": "client-secret-opaque",
            "refresh_token": secret,
            "idempotency_key": "calendar-connection-key",
        },
    )
    assert replay.status_code == 200
    assert replay.json() == {"connection": connection}


@pytest.mark.asyncio
async def test_connection_initial_creation_is_201_and_exact_replay_is_200(client):
    body = {
        "schema_version": 1,
        "service": "calendar_readonly",
        "label": "Operator Google",
        "client_id": "client-id-status",
        "client_secret": "client-secret-status",
        "refresh_token": "refresh-status",
        "idempotency_key": "connection-status-key",
    }
    first = await client.post("/api/calendar/connections", headers=ORIGIN_HEADERS, json=body)
    assert first.status_code == 201, first.text
    replay = await client.post("/api/calendar/connections", headers=ORIGIN_HEADERS, json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()


@pytest.mark.asyncio
async def test_connection_same_key_concurrent_requests_store_once_and_share_winner(client, monkeypatch):
    original_store = vault_repository.store
    store_calls = 0

    async def counted_store(key: str, value: str, *, description: str | None = None):
        nonlocal store_calls
        store_calls += 1
        return await original_store(key, value, description=description)

    monkeypatch.setattr("src.api.calendar.vault_repository.store", counted_store)
    body = {
        "schema_version": 1,
        "service": "calendar_readonly",
        "label": "Concurrent Calendar",
        "client_id": "client-id-concurrent",
        "client_secret": None,
        "refresh_token": "refresh-concurrent",
        "idempotency_key": "connection-concurrent-key",
    }
    responses = await asyncio.gather(
        *(client.post("/api/calendar/connections", headers=ORIGIN_HEADERS, json=body) for _ in range(2))
    )
    assert sorted(response.status_code for response in responses) == [200, 201], [response.text for response in responses]
    assert store_calls == 1
    payloads = [response.json() for response in responses]
    assert payloads[0] == payloads[1]
    assert payloads[0]["connection"]["state"] == "active"


@pytest.mark.asyncio
async def test_connection_observed_cross_process_winner_replays_as_200(client, async_db, monkeypatch):
    """An active winner observed after our vault store is still an exact replay."""

    original_store = vault_repository.store

    async def store_then_complete_winner(key: str, value: str, *, description: str | None = None):
        await original_store(key, value, description=description)
        async with async_db() as db:
            row = (
                await db.execute(
                    select(GoogleServiceConnection).where(
                        GoogleServiceConnection.setup_idempotency_key == "connection-observed-winner-key"
                    )
                )
            ).scalar_one()
            row.state = "active"
            row.revision += 1
            await db.flush()

    monkeypatch.setattr("src.api.calendar.vault_repository.store", store_then_complete_winner)
    response = await client.post(
        "/api/calendar/connections",
        headers=ORIGIN_HEADERS,
        json={
            "schema_version": 1,
            "service": "calendar_readonly",
            "label": "Observed Winner",
            "client_id": "client-id-observed-winner",
            "client_secret": None,
            "refresh_token": "refresh-observed-winner",
            "idempotency_key": "connection-observed-winner-key",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["connection"]["state"] == "active"
    async with async_db() as db:
        row = (
            await db.execute(
                select(GoogleServiceConnection).where(
                    GoogleServiceConnection.setup_idempotency_key == "connection-observed-winner-key"
                )
            )
        ).scalar_one()
        assert row.state == "active"
        vault_key = row.vault_secret_key
    assert await vault_repository.get(vault_key) is not None


@pytest.mark.asyncio
async def test_connection_store_failure_after_commit_compensates_and_blocks_no_secret(client, async_db, monkeypatch):
    original_store = vault_repository.store

    async def store_then_raise(key: str, value: str, *, description: str | None = None):
        await original_store(key, value, description=description)
        raise RuntimeError("simulated post-commit audit failure")

    monkeypatch.setattr("src.api.calendar.vault_repository.store", store_then_raise)
    response = await client.post(
        "/api/calendar/connections",
        headers=ORIGIN_HEADERS,
        json={
            "schema_version": 1,
            "service": "calendar_readonly",
            "label": "Compensated Calendar",
            "client_id": "client-id-compensated",
            "client_secret": "client-secret-compensated",
            "refresh_token": "refresh-compensated",
            "idempotency_key": "connection-compensated-key",
        },
    )
    detail = _safe_detail(response, status=503)
    assert detail["code"] == "calendar_internal_error"
    async with async_db() as db:
        row = (
            await db.execute(
                select(GoogleServiceConnection).where(
                    GoogleServiceConnection.setup_idempotency_key == "connection-compensated-key"
                )
            )
        ).scalar_one()
        assert row.state == "blocked"
        vault_key = row.vault_secret_key
    assert await vault_repository.get(vault_key) is None


@pytest.mark.asyncio
async def test_connection_stale_preparing_without_secret_becomes_blocked(client, async_db):
    """A persisted crash before vault storage is reconciled on exact retry."""

    body = {
        "schema_version": 1,
        "service": "calendar_readonly",
        "label": "Stale Missing Calendar",
        "client_id": "client-id-stale-missing",
        "client_secret": None,
        "refresh_token": "refresh-stale-missing",
        "idempotency_key": "connection-stale-missing-key",
    }
    parsed = ConnectionCreate.model_validate(body)
    stale_at = datetime.now(timezone.utc) - timedelta(seconds=CONTROL_MAX_RUNTIME_SECONDS + 5)
    secret_key = "calendar:operator:test-bypass:stale-missing-key"
    async with async_db() as db:
        db.add(
            GoogleServiceConnection(
                owner_principal_id=OWNER_PRINCIPAL,
                owner_session_id=OWNER_SESSION,
                service="calendar_readonly",
                label=parsed.label,
                vault_secret_key=secret_key,
                credential_fingerprint=_connection_credential_fingerprint(parsed),
                setup_idempotency_key=parsed.idempotency_key,
                setup_request_digest=_connection_request_digest(parsed),
                state="preparing",
                revision=1,
                created_at=stale_at,
                updated_at=stale_at,
            )
        )

    response = await client.post("/api/calendar/connections", headers=ORIGIN_HEADERS, json=body)
    detail = _safe_detail(response, status=409)
    assert detail["code"] == "calendar_connection_reconciliation_required"
    async with async_db() as db:
        row = (
            await db.execute(
                select(GoogleServiceConnection).where(
                    GoogleServiceConnection.setup_idempotency_key == parsed.idempotency_key
                )
            )
        ).scalar_one()
        assert row.state == "blocked"
        assert row.revision >= 2
    assert await vault_repository.get(secret_key) is None

    retry = await client.post("/api/calendar/connections", headers=ORIGIN_HEADERS, json=body)
    retry_detail = _safe_detail(retry, status=409)
    assert retry_detail["code"] == "calendar_connection_reconciliation_required"


@pytest.mark.asyncio
async def test_connection_recent_preparing_stays_pending_without_vault_cleanup(client, async_db, monkeypatch):
    """A live cross-process reservation is not tombstoned during its grace window."""

    body = {
        "schema_version": 1,
        "service": "calendar_readonly",
        "label": "Recent Preparing Calendar",
        "client_id": "client-id-recent-preparing",
        "client_secret": None,
        "refresh_token": "refresh-recent-preparing",
        "idempotency_key": "connection-recent-preparing-key",
    }
    parsed = ConnectionCreate.model_validate(body)
    secret_key = "calendar:operator:test-bypass:recent-preparing-key"
    async with async_db() as db:
        db.add(
            GoogleServiceConnection(
                owner_principal_id=OWNER_PRINCIPAL,
                owner_session_id=OWNER_SESSION,
                service="calendar_readonly",
                label=parsed.label,
                vault_secret_key=secret_key,
                credential_fingerprint=_connection_credential_fingerprint(parsed),
                setup_idempotency_key=parsed.idempotency_key,
                setup_request_digest=_connection_request_digest(parsed),
                state="preparing",
                revision=1,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
        )

    async def must_not_read(_key: str):
        raise AssertionError("a recent reservation must not be cleaned up by a retry")

    monkeypatch.setattr("src.api.calendar.vault_repository.get", must_not_read)
    response = await client.post("/api/calendar/connections", headers=ORIGIN_HEADERS, json=body)
    detail = _safe_detail(response, status=409)
    assert detail["code"] == "calendar_connection_reconciliation_required"
    async with async_db() as db:
        row = (
            await db.execute(
                select(GoogleServiceConnection).where(
                    GoogleServiceConnection.setup_idempotency_key == parsed.idempotency_key
                )
            )
        ).scalar_one()
        assert row.state == "preparing"
        assert row.revision == 1


@pytest.mark.asyncio
async def test_connection_store_then_cas_failure_reconciles_persisted_secret(client, async_db, monkeypatch):
    """A failed activation CAS deletes its own stored value and stays blocked."""

    original_store = vault_repository.store

    async def store_then_break_cas(key: str, value: str, *, description: str | None = None):
        await original_store(key, value, description=description)
        async with async_db() as db:
            row = (
                await db.execute(
                    select(GoogleServiceConnection).where(
                        GoogleServiceConnection.setup_idempotency_key == "connection-cas-failure-key"
                    )
                )
            ).scalar_one()
            row.state = "blocked"
            await db.flush()

    monkeypatch.setattr("src.api.calendar.vault_repository.store", store_then_break_cas)
    response = await client.post(
        "/api/calendar/connections",
        headers=ORIGIN_HEADERS,
        json={
            "schema_version": 1,
            "service": "calendar_readonly",
            "label": "CAS Failure Calendar",
            "client_id": "client-id-cas-failure",
            "client_secret": None,
            "refresh_token": "refresh-cas-failure",
            "idempotency_key": "connection-cas-failure-key",
        },
    )
    detail = _safe_detail(response, status=409)
    assert detail["code"] == "calendar_connection_reconciliation_required"
    async with async_db() as db:
        row = (
            await db.execute(
                select(GoogleServiceConnection).where(
                    GoogleServiceConnection.setup_idempotency_key == "connection-cas-failure-key"
                )
            )
        ).scalar_one()
        assert row.state == "blocked"
        vault_key = row.vault_secret_key
    assert await vault_repository.get(vault_key) is None


@pytest.mark.asyncio
async def test_connection_preparing_replay_reads_existing_secret_without_second_store(client, async_db, monkeypatch):
    body = {
        "schema_version": 1,
        "service": "calendar_readonly",
        "label": "Recovered Calendar",
        "client_id": "client-id-recovered",
        "client_secret": None,
        "refresh_token": "refresh-recovered",
        "idempotency_key": "connection-recovered-key",
    }
    parsed = ConnectionCreate.model_validate(body)
    secret_key = "calendar:operator:test-bypass:recovery-key"
    stale_at = datetime.now(timezone.utc) - timedelta(seconds=CONTROL_MAX_RUNTIME_SECONDS + 5)
    async with async_db() as db:
        row = GoogleServiceConnection(
            owner_principal_id=OWNER_PRINCIPAL,
            owner_session_id=OWNER_SESSION,
            service="calendar_readonly",
            label=parsed.label,
            vault_secret_key=secret_key,
            credential_fingerprint=_connection_credential_fingerprint(parsed),
            setup_idempotency_key=parsed.idempotency_key,
            setup_request_digest=_connection_request_digest(parsed),
            state="preparing",
            revision=1,
            created_at=stale_at,
            updated_at=stale_at,
        )
        db.add(row)
        await db.flush()
    await vault_repository.store(
        secret_key,
        json.dumps({"client_id": parsed.client_id, "refresh_token": parsed.refresh_token}, separators=(",", ":")),
        description="recovery test",
    )

    async def fail_if_stored(*_args, **_kwargs):
        raise AssertionError("preparing replay must never store a second secret")

    monkeypatch.setattr("src.api.calendar.vault_repository.store", fail_if_stored)
    replay = await client.post("/api/calendar/connections", headers=ORIGIN_HEADERS, json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json()["connection"]["state"] == "active"
    async with async_db() as db:
        current = await db.get(GoogleServiceConnection, replay.json()["connection"]["connection_id"])
        assert current is not None and current.state == "active" and current.revision == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutator",
    [
        lambda body: {**body, "unknown": "must-be-rejected"},
        lambda body: {**body, "refresh_token": 12345},
    ],
)
async def test_connection_validation_never_echoes_write_only_credentials(client, mutator):
    secret = "validation-secret-opaque"
    body = {
        "schema_version": 1,
        "service": "calendar_readonly",
        "label": "Operator Google",
        "client_id": "client-id-opaque",
        "refresh_token": secret,
        "idempotency_key": "invalid-connection-key",
    }
    response = await client.post("/api/calendar/connections", headers=ORIGIN_HEADERS, json=mutator(body))
    _safe_detail(response, status=422)
    assert secret not in response.text


@pytest.mark.asyncio
async def test_connection_request_size_cap_handles_forged_and_chunked_lengths(client):
    oversized = json.dumps(
        {
            "schema_version": 1,
            "service": "calendar_readonly",
            "label": "Operator Google",
            "client_id": "client-id-opaque",
            "refresh_token": "size-bound-secret",
            "idempotency_key": "oversized-key",
        }
    ).encode() + b" " * (MAX_SETUP_BYTES + 1)
    forged = await client.post(
        "/api/calendar/connections",
        headers={**ORIGIN_HEADERS, "content-length": str(MAX_SETUP_BYTES + 1), "content-type": "application/json"},
        content=oversized[:256],
    )
    _safe_detail(forged, status=413)
    assert "size-bound-secret" not in forged.text

    chunked = await client.post(
        "/api/calendar/connections",
        headers={**ORIGIN_HEADERS, "content-type": "application/json"},
        content=_ChunkedBody(oversized),
    )
    _safe_detail(chunked, status=413)
    assert "size-bound-secret" not in chunked.text

    counted = _CountingBody(
        [b"x" * (MAX_SETUP_BYTES + 1), b'"refresh_token":"late-secret-never-read"']
    )
    early = await client.post(
        "/api/calendar/connections",
        headers={**ORIGIN_HEADERS, "content-type": "application/json"},
        content=counted,
    )
    _safe_detail(early, status=413)
    assert counted.consumed == 1
    assert "late-secret-never-read" not in early.text


@pytest.mark.asyncio
async def test_setup_cleanup_failure_persists_blocked_cleanup_state(client, async_db, monkeypatch):
    """A post-vault setup mismatch must retain a cleanup reconciliation row."""

    original_store = vault_repository.store

    async def store_then_invalidate(key: str, value: str, *, description: str | None = None):
        await original_store(key, value, description=description)
        async with async_db() as db:
            row = (
                await db.execute(
                    select(GoogleServiceConnection).where(
                        GoogleServiceConnection.setup_idempotency_key == "setup-cleanup-mismatch-key"
                    )
                )
            ).scalar_one()
            row.state = "blocked"
            await db.flush()

    async def fail_delete(_key: str):
        raise OSError("simulated setup cleanup failure")

    monkeypatch.setattr("src.api.calendar.vault_repository.store", store_then_invalidate)
    monkeypatch.setattr("src.api.calendar.vault_repository.delete", fail_delete)
    response = await client.post(
        "/api/calendar/connections",
        headers=ORIGIN_HEADERS,
        json={
            "schema_version": 1,
            "service": "calendar_readonly",
            "label": "Operator Google",
            "client_id": "client-id-opaque",
            "client_secret": "client-secret-opaque",
            "refresh_token": "setup-cleanup-secret",
            "idempotency_key": "setup-cleanup-mismatch-key",
        },
    )
    detail = _safe_detail(response, status=503)
    assert detail["code"] == "calendar_connection_cleanup_blocked"
    async with async_db() as db:
        row = (
            await db.execute(
                select(GoogleServiceConnection).where(
                    GoogleServiceConnection.setup_idempotency_key == "setup-cleanup-mismatch-key"
                )
            )
        ).scalar_one()
        assert row.state == "blocked_cleanup"


@pytest.mark.asyncio
async def test_calendar_auth_and_foreign_owner_reads_are_not_disclosed(client, async_db, monkeypatch):
    connection = await _create_connection(client)

    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "configured-auth-secret")
    anonymous = await client.get("/api/calendar/connections")
    _safe_detail(anonymous, status=401)
    monkeypatch.setattr(settings, "operator_auth_secret", "")
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", True)

    async with async_db() as db:
        row = await db.get(GoogleServiceConnection, connection["connection_id"])
        assert row is not None
        row.owner_principal_id = "operator:foreign"
        row.owner_session_id = "foreign-session"
        await db.flush()

    foreign = await client.post(
        f"/api/calendar/connections/{connection['connection_id']}/verify",
        headers=ORIGIN_HEADERS,
        json={"expected_revision": connection["revision"], "idempotency_key": "foreign-verify"},
    )
    detail = _safe_detail(foreign, status=404)
    assert connection["connection_id"] not in foreign.text
    assert detail["code"] == "calendar_connection_not_found"


@pytest.mark.asyncio
async def test_calendar_controls_use_persisted_active_revoked_and_expired_sessions(client, async_db, monkeypatch):
    token = await _login_with_persisted_operator_session(client, monkeypatch)
    connection = await _create_connection(client, key="live-session-connection-key")
    active = await client.get("/api/calendar/connections")
    assert active.status_code == 200, active.text
    assert active.json()["connections"][0]["connection_id"] == connection["connection_id"]

    logout = await client.post(
        "/api/auth/logout",
        headers={"host": "test", "origin": "http://localhost:3001"},
    )
    assert logout.status_code == 204, logout.text
    client.cookies.set(settings.operator_auth_cookie_name, token)
    revoked = await client.post(
        f"/api/calendar/connections/{connection['connection_id']}/verify",
        headers=ORIGIN_HEADERS,
        json={"expected_revision": connection["revision"], "idempotency_key": "revoked-session-key"},
    )
    revoked_detail = _safe_detail(revoked, status=401)
    assert revoked_detail["code"] == "session_revoked"

    fresh_token = await _login_with_persisted_operator_session(client, monkeypatch)
    async with async_db() as db:
        session = (
            await db.execute(
                select(OperatorSession).where(OperatorSession.token_hash.is_not(None)).order_by(OperatorSession.created_at.desc())
            )
        ).scalars().first()
        assert session is not None
        session.idle_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db.flush()
    client.cookies.set(settings.operator_auth_cookie_name, fresh_token)
    expired = await client.get("/api/calendar/connections")
    expired_detail = _safe_detail(expired, status=401)
    assert expired_detail["code"] == "session_expired"


@pytest.mark.asyncio
async def test_verify_control_replay_preserves_key_and_does_not_contact_provider_twice(client, monkeypatch):
    connection = await _create_connection(client)
    provider = _GoogleTransportScript()
    monkeypatch.setattr("src.integrations.google_calendar.request_pinned_https", provider)
    unavailable = await client.get(
        f"/api/calendar/connections/{connection['connection_id']}/calendars",
        headers=ORIGIN_HEADERS,
    )
    detail = _safe_detail(unavailable, status=409)
    assert detail["code"] == "calendar_connection_reconciliation_required"
    assert provider.calls == []

    request = {"expected_revision": connection["revision"], "idempotency_key": "verify-control-key"}

    first = await client.post(
        f"/api/calendar/connections/{connection['connection_id']}/verify",
        headers=ORIGIN_HEADERS,
        json=request,
    )
    assert first.status_code in {200, 201}, first.text
    first_payload = first.json()
    assert set(first_payload) == {"connection", "calendars", "calendar_list_revision", "pages_read", "truncated", "provider_status"}
    assert first_payload["provider_status"] == "verified"
    calls_after_first = len(provider.calls)
    assert calls_after_first == 2  # token refresh and one CalendarList read

    replay = await client.post(
        f"/api/calendar/connections/{connection['connection_id']}/verify",
        headers=ORIGIN_HEADERS,
        json=request,
    )
    assert replay.status_code == 200
    assert replay.json() == first_payload
    assert len(provider.calls) == calls_after_first

    stored = await client.get(
        f"/api/calendar/connections/{connection['connection_id']}/calendars",
        headers=ORIGIN_HEADERS,
    )
    assert stored.status_code == 200, stored.text
    assert stored.json() == first_payload
    assert len(provider.calls) == calls_after_first


@pytest.mark.asyncio
async def test_calendar_lookup_uses_connection_verified_pointer_and_rejects_wrong_root(client, async_db, monkeypatch):
    connection = await _create_connection(client, key="pointer-connection-key")
    provider = _GoogleTransportScript()
    monkeypatch.setattr("src.integrations.google_calendar.request_pinned_https", provider)
    verified = await client.post(
        f"/api/calendar/connections/{connection['connection_id']}/verify",
        headers=ORIGIN_HEADERS,
        json={"expected_revision": connection["revision"], "idempotency_key": "pointer-verify-key"},
    )
    assert verified.status_code == 200, verified.text
    async with async_db() as db:
        row = await db.get(GoogleServiceConnection, connection["connection_id"])
        assert row is not None and row.verified_setup_job_id
        row.verified_setup_job_id = "calendar-control:wrong-root"
        await db.flush()
    response = await client.get(
        f"/api/calendar/connections/{connection['connection_id']}/calendars",
        headers=ORIGIN_HEADERS,
    )
    detail = _safe_detail(response, status=409)
    assert detail["code"] == "calendar_connection_reconciliation_required"
    assert provider.calls and len(provider.calls) == 2

@pytest.mark.asyncio
async def test_verify_control_same_key_changed_revision_is_a_typed_conflict(client, monkeypatch):
    connection = await _create_connection(client, key="verify-conflict-connection-key")
    provider = _GoogleTransportScript()
    monkeypatch.setattr("src.integrations.google_calendar.request_pinned_https", provider)
    request = {
        "expected_revision": connection["revision"],
        "idempotency_key": "verify-conflict-key",
    }
    first = await client.post(
        f"/api/calendar/connections/{connection['connection_id']}/verify",
        headers=ORIGIN_HEADERS,
        json=request,
    )
    assert first.status_code == 200, first.text
    calls_after_first = len(provider.calls)

    conflict = await client.post(
        f"/api/calendar/connections/{connection['connection_id']}/verify",
        headers=ORIGIN_HEADERS,
        json={**request, "expected_revision": connection["revision"] + 1},
    )
    detail = _safe_detail(conflict, status=409)
    assert detail["code"] == "calendar_control_idempotency_conflict"
    assert len(provider.calls) == calls_after_first


@pytest.mark.asyncio
async def test_verify_control_same_key_concurrent_requests_share_one_durable_root(client, monkeypatch):
    connection = await _create_connection(client, key="verify-concurrent-connection-key")
    provider = _GoogleTransportScript()
    monkeypatch.setattr("src.integrations.google_calendar.request_pinned_https", provider)
    request = {
        "expected_revision": connection["revision"],
        "idempotency_key": "verify-concurrent-key",
    }

    responses = await asyncio.gather(
        *(
            client.post(
                f"/api/calendar/connections/{connection['connection_id']}/verify",
                headers=ORIGIN_HEADERS,
                json=request,
            )
            for _ in range(2)
        )
    )
    assert any(response.status_code == 200 for response in responses), [response.text for response in responses]
    assert all(response.status_code in {200, 409} for response in responses), [response.text for response in responses]
    assert len(provider.calls) <= 2
    # The public connection DTO deliberately omits the pointer. Inspect the
    # durable repository by its deterministic control identity instead.
    control = CalendarControlRequest(
        operation="calendar_connection_verify",
        target_id=connection["connection_id"],
        owner_principal_id=OWNER_PRINCIPAL,
        owner_session_id=OWNER_SESSION,
        expected_revision=connection["revision"],
        idempotency_key=request["idempotency_key"],
        request_digest="sha256:" + "0" * 64,
    )
    durable = await durable_job_repository.get_job(control_job_id(control))
    assert durable is not None
    assert durable["job_kind"] == "calendar_connection_verify"


@pytest.mark.asyncio
async def test_verify_controls_replay_a_after_b_without_new_provider_contact(client, monkeypatch):
    connection = await _create_connection(client, key="verify-ab-a-connection-key")
    provider = _GoogleTransportScript()
    monkeypatch.setattr("src.integrations.google_calendar.request_pinned_https", provider)
    request_a = {"expected_revision": connection["revision"], "idempotency_key": "verify-a-key"}
    request_b = {"expected_revision": connection["revision"], "idempotency_key": "verify-b-key"}
    first_a = await client.post(
        f"/api/calendar/connections/{connection['connection_id']}/verify",
        headers=ORIGIN_HEADERS,
        json=request_a,
    )
    assert first_a.status_code == 200, first_a.text
    calls_after_a = len(provider.calls)
    second_b = await client.post(
        f"/api/calendar/connections/{connection['connection_id']}/verify",
        headers=ORIGIN_HEADERS,
        json=request_b,
    )
    assert second_b.status_code == 200, second_b.text
    calls_after_b = len(provider.calls)
    assert calls_after_b > calls_after_a
    replay_a = await client.post(
        f"/api/calendar/connections/{connection['connection_id']}/verify",
        headers=ORIGIN_HEADERS,
        json=request_a,
    )
    assert replay_a.status_code == 200, replay_a.text
    assert replay_a.json() == first_a.json()
    assert len(provider.calls) == calls_after_b


@pytest.mark.asyncio
async def test_control_crash_after_intent_stays_unknown_and_cannot_recontact(async_db):
    request = CalendarControlRequest(
        operation="calendar_connection_verify",
        target_id="connection-crash-window",
        owner_principal_id=OWNER_PRINCIPAL,
        owner_session_id=OWNER_SESSION,
        expected_revision=1,
        idempotency_key="crash-window-key",
        request_digest="sha256:" + "1" * 64,
    )
    calls = 0

    async def crash_after_intent(_lease):
        nonlocal calls
        calls += 1
        raise RuntimeError("simulated worker crash")

    with pytest.raises(CalendarControlReconciliationRequired):
        await run_control(request, crash_after_intent)
    stored = await durable_job_repository.get_job(control_job_id(request))
    assert stored is not None
    assert stored["status"] == "unknown_external_effect"
    assert any(
        item.get("status") == "unknown" and item.get("effect_type") == request.operation
        for item in stored["effects"]
    )

    async def must_not_recontact(_lease):
        raise AssertionError("unknown control was re-executed")

    with pytest.raises(CalendarControlReconciliationRequired):
        await run_control(request, must_not_recontact)
    assert calls == 1


@pytest.mark.asyncio
async def test_control_cancellation_after_intent_persists_unknown_and_cannot_recontact(async_db):
    request = CalendarControlRequest(
        operation="calendar_connection_verify",
        target_id="connection-cancel-window",
        owner_principal_id=OWNER_PRINCIPAL,
        owner_session_id=OWNER_SESSION,
        expected_revision=1,
        idempotency_key="cancel-window-key",
        request_digest="sha256:" + "4" * 64,
    )
    calls = 0

    async def cancel_after_intent(_lease):
        nonlocal calls
        calls += 1
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await run_control(request, cancel_after_intent)

    stored = await durable_job_repository.get_job(control_job_id(request))
    assert stored is not None
    assert stored["status"] == "unknown_external_effect"
    assert stored["failure_reason"] == "cancelled_after_intent"
    assert any(
        item.get("status") == "unknown"
        and item.get("effect_type") == request.operation
        and item.get("details", {}).get("cancellation_after_intent") is True
        for item in stored["effects"]
    )

    async def must_not_recontact(_lease):
        raise AssertionError("cancelled control was re-executed")

    with pytest.raises(CalendarControlReconciliationRequired):
        await run_control(request, must_not_recontact)
    assert calls == 1


@pytest.mark.asyncio
async def test_control_deadline_timeout_after_intent_stays_unknown_and_cannot_recontact(async_db, monkeypatch):
    request = CalendarControlRequest(
        operation="calendar_connection_verify",
        target_id="connection-timeout-window",
        owner_principal_id=OWNER_PRINCIPAL,
        owner_session_id=OWNER_SESSION,
        expected_revision=1,
        idempotency_key="timeout-window-key",
        request_digest="sha256:" + "2" * 64,
    )
    monkeypatch.setattr(calendar_controls, "_remaining_control_seconds", lambda _value: 0.01)
    calls = 0

    async def slow_after_intent(_lease):
        nonlocal calls
        calls += 1
        await asyncio.sleep(1)
        raise AssertionError("the bounded control should time out first")

    with pytest.raises(CalendarControlReconciliationRequired):
        await run_control(request, slow_after_intent)
    stored = await durable_job_repository.get_job(control_job_id(request))
    assert stored is not None
    assert stored["status"] == "unknown_external_effect"
    assert calls == 1

    async def must_not_recontact(_lease):
        raise AssertionError("timed out control was re-executed")

    with pytest.raises(CalendarControlReconciliationRequired):
        await run_control(request, must_not_recontact)


@pytest.mark.asyncio
async def test_control_deadline_covers_artifact_and_readback_publication(async_db, monkeypatch):
    request = CalendarControlRequest(
        operation="calendar_connection_verify",
        target_id="connection-timeout-publication",
        owner_principal_id=OWNER_PRINCIPAL,
        owner_session_id=OWNER_SESSION,
        expected_revision=1,
        idempotency_key="timeout-publication-key",
        request_digest="sha256:" + "3" * 64,
    )
    monkeypatch.setattr(calendar_controls, "_remaining_control_seconds", lambda _value: 0.01)
    original_record_artifact = durable_job_repository.record_artifact

    async def slow_record_artifact(*args, **kwargs):
        await asyncio.sleep(1)
        return await original_record_artifact(*args, **kwargs)

    monkeypatch.setattr(durable_job_repository, "record_artifact", slow_record_artifact)

    async def completed_execution(_lease):
        return calendar_controls.CalendarControlExecution(payload={"status": "verified"})

    with pytest.raises(CalendarControlReconciliationRequired):
        await run_control(request, completed_execution)

    stored = await durable_job_repository.get_job(control_job_id(request))
    assert stored is not None
    assert stored["status"] == "unknown_external_effect"
    assert any(
        item.get("status") == "unknown" and item.get("effect_type") == request.operation
        for item in stored["effects"]
    )


@pytest.mark.asyncio
async def test_valid_rfc3339_consent_and_schedule_have_strict_bounded_envelopes(client, async_db, monkeypatch):
    await _seed_goal(async_db)
    provider = _GoogleTransportScript()
    monkeypatch.setattr("src.integrations.google_calendar.request_pinned_https", provider)
    connection = await _create_connection(client)
    verified = await client.post(
        f"/api/calendar/connections/{connection['connection_id']}/verify",
        headers=ORIGIN_HEADERS,
        json={"expected_revision": connection["revision"], "idempotency_key": "schedule-verify-key"},
    )
    assert verified.status_code == 200, verified.text
    consent = await _create_consent(client, connection_id=connection["connection_id"])
    assert set(consent) == {
        "consent_id",
        "connection_id",
        "connection_revision",
        "goal_id",
        "goal_revision",
        "allowed_fields",
        "window_minutes",
        "max_events",
        "sync_metadata_limit",
        "allow_remote_model",
        "expires_at",
        "state",
        "revision",
        "consent_digest",
        "created_at",
        "updated_at",
    }
    assert consent["sync_metadata_limit"] == 0  # No implicit sync grant for legacy consent.
    assert "calendar_id" not in consent
    assert consent["state"] == "active"
    assert consent["expires_at"].endswith("Z")

    schedule = await client.post(
        "/api/calendar/schedules",
        headers=ORIGIN_HEADERS,
        json={
            "consent_id": consent["consent_id"],
            "goal_id": "calendar-goal",
            "goal_revision": 1,
            "calendar_id": "owner-calendar",
            "cadence": {"kind": "hourly", "timezone": "Europe/Warsaw", "daily_hour": None, "daily_minute": None},
            "expires_at": _utc_after(1),
            "idempotency_key": "calendar-schedule-key",
        },
    )
    assert schedule.status_code in {200, 201}, schedule.text
    binding = schedule.json()["binding"]
    assert binding["cadence"] == {"kind": "hourly", "timezone": "Europe/Warsaw", "daily_hour": None, "daily_minute": None}
    assert binding["state"] == "active"
    assert binding["consent_id"] == consent["consent_id"]

    too_long = await client.post(
        "/api/calendar/schedules",
        headers=ORIGIN_HEADERS,
        json={
            "consent_id": consent["consent_id"],
            "goal_id": "calendar-goal",
            "goal_revision": 1,
            "calendar_id": "owner-calendar",
            "cadence": {"kind": "hourly", "timezone": "Europe/Warsaw", "daily_hour": None, "daily_minute": None},
            "expires_at": _utc_after(2),
            "idempotency_key": "calendar-schedule-too-long-key",
        },
    )
    too_long_detail = _safe_detail(too_long, status=422)
    assert too_long_detail["code"] == "calendar_schedule_expiry_invalid"

    naive = await client.post(
        "/api/calendar/read-consents",
        headers=ORIGIN_HEADERS,
        json={
            "schema_version": 1,
            "connection_id": connection["connection_id"],
            "calendar_id": "owner-calendar",
            "goal_id": "calendar-goal",
            "goal_revision": 1,
            "allowed_fields": ["summary", "start", "end"],
            "window_minutes": 30,
            "max_events": 1,
            "allow_remote_model": False,
            "expires_at": (datetime.now() + timedelta(days=1)).isoformat(),
            "idempotency_key": "naive-consent-key",
        },
    )
    detail = _safe_detail(naive, status=422)
    assert detail["code"] == "calendar_request_invalid"


@pytest.mark.asyncio
async def test_governed_schedule_controls_require_action_key_revision_and_reason(client, async_db):
    """Pause/resume/revoke use the fixed control envelope, not a bare state write."""

    async with async_db() as db:
        job = ScheduledJob(
            name="Calendar contract schedule",
            enabled=True,
            trigger_type="governed",
            trigger_spec_json='{"kind":"hourly","timezone":"UTC","daily_hour":null,"daily_minute":null}',
            action_type="calendar.observe_due_events.v1",
            action_spec_json='{"binding_id":"pending"}',
            session_id=None,
            created_by_session_id=None,
        )
        db.add(job)
        await db.flush()
        binding = GovernedScheduleBinding(
            scheduled_job_id=job.id,
            owner_principal_id=OWNER_PRINCIPAL,
            owner_session_id=OWNER_SESSION,
            goal_id="calendar-goal",
            goal_revision=1,
            capability_id="calendar.observe_due_events.v1",
            action_type="calendar.observe_due_events.v1",
            input_artifact_id="calendar-input-artifact",
            input_digest="sha256:" + "1" * 64,
            action_digest="sha256:" + "2" * 64,
            consent_kind="calendar_read",
            read_consent_id="calendar-consent",
            consent_revision=1,
            consent_digest="sha256:" + "3" * 64,
            schedule_idempotency_key="seed-schedule-key",
            schedule_request_digest="sha256:" + "4" * 64,
            cadence_kind="hourly",
            timezone="UTC",
            binding_revision=1,
            expires_at=datetime.now(timezone.utc) + timedelta(days=1),
            state="active",
        )
        db.add(binding)
        await db.flush()
        binding_id = binding.binding_id

    pause_body = {
        "action": "pause",
        "expected_binding_revision": 1,
        "idempotency_key": "schedule-pause-key",
    }
    paused = await client.patch(
        f"/api/governed-schedules/{binding_id}",
        headers=ORIGIN_HEADERS,
        json=pause_body,
    )
    assert paused.status_code == 200, paused.text
    paused_binding = paused.json()["binding"]
    assert paused_binding["state"] == "paused"
    assert paused_binding["binding_revision"] == 2

    replay = await client.patch(
        f"/api/governed-schedules/{binding_id}",
        headers=ORIGIN_HEADERS,
        json=pause_body,
    )
    assert replay.status_code == 200
    assert replay.json() == paused.json()

    revoked = await client.post(
        f"/api/governed-schedules/{binding_id}/revoke",
        headers=ORIGIN_HEADERS,
        json={
            "expected_binding_revision": 2,
            "idempotency_key": "schedule-revoke-key",
            "reason": "The operator no longer needs this read-only schedule.",
        },
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["binding"]["state"] == "revoked"


@pytest.mark.asyncio
async def test_provider_events_and_prep_return_redacted_event_and_exact_seven_field_artifact(client, async_db, monkeypatch):
    await _seed_goal(async_db)
    provider = _GoogleTransportScript()
    monkeypatch.setattr("src.integrations.google_calendar.request_pinned_https", provider)
    connection = await _create_connection(client)

    verify = await client.post(
        f"/api/calendar/connections/{connection['connection_id']}/verify",
        headers=ORIGIN_HEADERS,
        json={"expected_revision": connection["revision"], "idempotency_key": "provider-verify-key"},
    )
    assert verify.status_code in {200, 201}, verify.text
    consent = await _create_consent(client, connection_id=connection["connection_id"])
    events_response = await client.get(
        f"/api/calendar/connections/{connection['connection_id']}/events",
        params={"consent_id": consent["consent_id"], "max_events": 10},
    )
    assert events_response.status_code == 200, events_response.text
    events_payload = events_response.json()
    assert set(events_payload) == {
        "events",
        "consent_id",
        "consent_revision",
        "connection_revision",
        "calendar_list_revision",
        "fetched_at",
        "pages_read",
        "truncated",
    }
    event = events_payload["events"][0]
    assert set(event) == {
        "event_binding_id",
        "event_binding_revision",
        "event_key",
        "event_revision",
        "calendar_list_revision",
        "summary",
        "start",
        "end",
        "location",
        "description",
        "attendees",
    }
    assert "provider-event-opaque" not in events_response.text
    assert "owner-calendar" not in events_response.text
    assert event["start"].endswith("Z") and event["end"].endswith("Z")

    prep = await client.post(
        "/api/calendar/prep",
        headers=ORIGIN_HEADERS,
        json={
            "schema_version": 1,
            "input": {
                "schema_version": 1,
                "consent_id": consent["consent_id"],
                "event_binding_id": event["event_binding_id"],
                "expected_event_binding_revision": event["event_binding_revision"],
                "expected_consent_revision": consent["revision"],
                "expected_connection_revision": connection["revision"],
                "event_revision": event["event_revision"],
                "calendar_list_revision": event["calendar_list_revision"],
                "goal_id": "calendar-goal",
                "goal_revision": 1,
                "purpose": "Prepare the operator meeting brief",
            },
            "title": "Prepare meeting brief",
            "idempotency_key": "calendar-prep-key",
        },
    )
    assert prep.status_code in {200, 201}, prep.text
    payload = prep.json()
    assert set(payload) == {"input_artifact", "task", "idempotent_replay"}
    artifact = payload["input_artifact"]
    assert set(artifact) == {
        "artifact_id",
        "typed_input_ref",
        "typed_input_digest",
        "capability_id",
        "goal_id",
        "goal_revision",
        "expires_at",
    }
    assert artifact["capability_id"] == "calendar.meeting-prep.v1"
    assert payload["task"]["input_artifact_id"] == artifact["artifact_id"]
    assert payload["task"]["goal_id"] == artifact["goal_id"] == "calendar-goal"
    assert payload["task"]["goal_revision"] == artifact["goal_revision"] == 1

    # Revoke the canonical consent after the artifact commit but before the
    # repository writer callback. The callback must observe the fresh row,
    # reject publication, and let the route tombstone the unbound artifact.
    import src.api.calendar as calendar_api

    original_create_task = calendar_api.repository.create_task

    async def revoke_before_publication(db, owner, task_request, **kwargs):
        async with async_db() as revoke_db:
            current_consent = await revoke_db.get(CalendarReadConsent, consent["consent_id"])
            assert current_consent is not None
            current_consent.state = "revoked"
            current_consent.revision += 1
            await revoke_db.flush()
        return await original_create_task(db, owner, task_request, **kwargs)

    monkeypatch.setattr(calendar_api.repository, "create_task", revoke_before_publication)
    raced_prep = await client.post(
        "/api/calendar/prep",
        headers=ORIGIN_HEADERS,
        json={
            "schema_version": 1,
            "input": {
                "schema_version": 1,
                "consent_id": consent["consent_id"],
                "event_binding_id": event["event_binding_id"],
                "expected_event_binding_revision": event["event_binding_revision"],
                "expected_consent_revision": consent["revision"],
                "expected_connection_revision": connection["revision"],
                "event_revision": event["event_revision"],
                "calendar_list_revision": event["calendar_list_revision"],
                "goal_id": "calendar-goal",
                "goal_revision": 1,
                "purpose": "Prepare after consent was revoked at publication",
            },
            "title": "Prepare after consent revoke",
            "idempotency_key": "calendar-prep-revoked-during-publication-key",
        },
    )
    raced_detail = _safe_detail(raced_prep, status=409)
    assert raced_detail["code"] == "calendar_revision_stale"
    async with async_db() as db:
        artifact_row = (
            await db.execute(
                select(WorkBoardInputArtifact).where(
                    WorkBoardInputArtifact.idempotency_key == "calendar-prep-revoked-during-publication-key"
                )
            )
        ).scalar_one()
        assert artifact_row.state == "revoked"
        assert artifact_row.bound_task_id is None
        tasks = (
            await db.execute(
                select(WorkBoardTask).where(
                    WorkBoardTask.idempotency_key == "calendar-prep-revoked-during-publication-key"
                )
            )
        ).scalars().all()
        assert tasks == []

    # Restore the fixture's consent so the following independent binding
    # revocation assertion remains focused on the event relationship.
    async with async_db() as db:
        current_consent = await db.get(CalendarReadConsent, consent["consent_id"])
        assert current_consent is not None
        current_consent.state = "active"
        current_consent.revision = consent["revision"]
        await db.flush()

    # A row that is still owner-visible but no longer selected cannot be used
    # as an authority handoff.  The prep route must reject it before creating
    # another input artifact or task.
    async with async_db() as db:
        binding_row = await db.get(CalendarEventBinding, event["event_binding_id"])
        assert binding_row is not None
        binding_row.state = "revoked"
        await db.flush()
    stale_prep = await client.post(
        "/api/calendar/prep",
        headers=ORIGIN_HEADERS,
        json={
            "schema_version": 1,
            "input": {
                "schema_version": 1,
                "consent_id": consent["consent_id"],
                "event_binding_id": event["event_binding_id"],
                "expected_event_binding_revision": event["event_binding_revision"],
                "expected_consent_revision": consent["revision"],
                "expected_connection_revision": connection["revision"],
                "event_revision": event["event_revision"],
                "calendar_list_revision": event["calendar_list_revision"],
                "goal_id": "calendar-goal",
                "goal_revision": 1,
                "purpose": "Prepare meeting brief after selection was revoked",
            },
            "title": "Prepare meeting brief after revoke",
            "idempotency_key": "calendar-prep-revoked-binding-key",
        },
    )
    stale_detail = _safe_detail(stale_prep, status=409)
    assert stale_detail["code"] == "calendar_event_revision_stale"


@pytest.mark.asyncio
async def test_prep_replay_drift_maps_artifact_conflict_without_new_task_or_provider(client, async_db, monkeypatch):
    provider, prep_body = await _calendar_prep_fixture(
        client,
        async_db,
        monkeypatch,
        prefix="prep-replay-drift",
    )
    first = await client.post("/api/calendar/prep", headers=ORIGIN_HEADERS, json=prep_body)
    assert first.status_code == 201, first.text
    calls_after_first = len(provider.calls)

    drifted = json.loads(json.dumps(prep_body))
    drifted["input"]["purpose"] = "Prepare a different meeting brief"
    drifted["title"] = "Different meeting brief"
    replay = await client.post("/api/calendar/prep", headers=ORIGIN_HEADERS, json=drifted)
    detail = _safe_detail(replay, status=409)
    assert detail["code"] == "input_artifact_idempotency_conflict"
    assert len(provider.calls) == calls_after_first

    async with async_db() as db:
        tasks = (
            await db.execute(
                select(WorkBoardTask).where(
                    WorkBoardTask.owner_principal_id == OWNER_PRINCIPAL,
                    WorkBoardTask.owner_session_id == OWNER_SESSION,
                    WorkBoardTask.idempotency_key == prep_body["idempotency_key"],
                )
            )
        ).scalars().all()
        assert len(tasks) == 1
        assert tasks[0].title == "Prepare meeting brief"
        artifact = (
            await db.execute(
                select(WorkBoardInputArtifact).where(
                    WorkBoardInputArtifact.owner_principal_id == OWNER_PRINCIPAL,
                    WorkBoardInputArtifact.owner_session_id == OWNER_SESSION,
                    WorkBoardInputArtifact.idempotency_key == prep_body["idempotency_key"],
                )
            )
        ).scalar_one()
        assert artifact.state == "bound"
        assert artifact.bound_task_id == tasks[0].task_id


@pytest.mark.asyncio
async def test_prep_artifact_write_failure_is_bounded_and_retryable(client, async_db, monkeypatch):
    provider, prep_body = await _calendar_prep_fixture(
        client,
        async_db,
        monkeypatch,
        prefix="prep-write-failure",
    )
    calls_before_prep = len(provider.calls)

    async def failed_prepare(*_args, **_kwargs):
        raise BoardError(
            "input_artifact_write_failed",
            "private write detail",
            status_code=503,
        )

    monkeypatch.setattr("src.api.calendar.prepare_input_artifact", failed_prepare)
    response = await client.post("/api/calendar/prep", headers=ORIGIN_HEADERS, json=prep_body)
    detail = _safe_detail(response, status=503)
    assert detail["code"] == "input_artifact_write_failed"
    assert detail["message"] == "The input artifact could not be written"
    assert detail["recovery_action"] == "retry_existing_request"
    assert "private write detail" not in response.text
    assert len(provider.calls) == calls_before_prep

    async with async_db() as db:
        tasks = (
            await db.execute(
                select(WorkBoardTask).where(
                    WorkBoardTask.owner_principal_id == OWNER_PRINCIPAL,
                    WorkBoardTask.owner_session_id == OWNER_SESSION,
                    WorkBoardTask.idempotency_key == prep_body["idempotency_key"],
                )
            )
        ).scalars().all()
        assert tasks == []
        artifacts = (
            await db.execute(
                select(WorkBoardInputArtifact).where(
                    WorkBoardInputArtifact.owner_principal_id == OWNER_PRINCIPAL,
                    WorkBoardInputArtifact.owner_session_id == OWNER_SESSION,
                    WorkBoardInputArtifact.idempotency_key == prep_body["idempotency_key"],
                )
            )
        ).scalars().all()
        assert artifacts == []


@pytest.mark.asyncio
async def test_unchanged_event_keeps_selection_provenance_when_unrelated_event_changes(client, async_db, monkeypatch):
    await _seed_goal(async_db)
    provider = _GoogleTransportScript()
    monkeypatch.setattr("src.integrations.google_calendar.request_pinned_https", provider)
    connection = await _create_connection(client, key="provenance-connection-key")
    verified = await client.post(
        f"/api/calendar/connections/{connection['connection_id']}/verify",
        headers=ORIGIN_HEADERS,
        json={"expected_revision": connection["revision"], "idempotency_key": "provenance-verify-key"},
    )
    assert verified.status_code == 200, verified.text
    consent = await _create_consent(client, connection_id=connection["connection_id"], key="provenance-consent-key")

    first = await client.get(
        f"/api/calendar/connections/{connection['connection_id']}/events",
        params={"consent_id": consent["consent_id"], "max_events": 10},
    )
    assert first.status_code == 200, first.text
    first_payload = first.json()
    original = provider.events[0]
    unrelated = {
        "id": "provider-unrelated-opaque",
        "etag": '"unrelated-etag"',
        "updated": "2026-09-30T08:30:00Z",
        "summary": "Unrelated event",
        "start": {"dateTime": "2026-09-30T11:00:00Z"},
        "end": {"dateTime": "2026-09-30T12:00:00Z"},
    }
    provider.events = [original, unrelated]
    second = await client.get(
        f"/api/calendar/connections/{connection['connection_id']}/events",
        params={"consent_id": consent["consent_id"], "max_events": 10},
    )
    assert second.status_code == 200, second.text
    second_payload = second.json()
    assert second_payload["calendar_list_revision"] != first_payload["calendar_list_revision"]
    first_event = first_payload["events"][0]
    second_event = second_payload["events"][0]
    assert second_event["event_key"] == first_event["event_key"]
    assert second_event["event_revision"] == first_event["event_revision"]
    assert second_event["event_binding_revision"] == first_event["event_binding_revision"]
    assert second_event["calendar_list_revision"] == first_event["calendar_list_revision"]

    prep = await client.post(
        "/api/calendar/prep",
        headers=ORIGIN_HEADERS,
        json={
            "schema_version": 1,
            "input": {
                "schema_version": 1,
                "consent_id": consent["consent_id"],
                "event_binding_id": second_event["event_binding_id"],
                "expected_event_binding_revision": second_event["event_binding_revision"],
                "expected_consent_revision": consent["revision"],
                "expected_connection_revision": connection["revision"],
                "event_revision": second_event["event_revision"],
                "calendar_list_revision": second_event["calendar_list_revision"],
                "goal_id": "calendar-goal",
                "goal_revision": 1,
                "purpose": "Prepare the unchanged selected event",
            },
            "title": "Prepare unchanged selected event",
            "idempotency_key": "provenance-prep-key",
        },
    )
    assert prep.status_code == 201, prep.text


@pytest.mark.asyncio
async def test_event_publication_rechecks_revoked_session_after_provider_read(client, async_db, monkeypatch):
    await _seed_goal(async_db)
    provider = _GoogleTransportScript()
    monkeypatch.setattr("src.integrations.google_calendar.request_pinned_https", provider)
    connection = await _create_connection(client, key="publication-session-connection-key")
    verified = await client.post(
        f"/api/calendar/connections/{connection['connection_id']}/verify",
        headers=ORIGIN_HEADERS,
        json={"expected_revision": connection["revision"], "idempotency_key": "publication-session-verify-key"},
    )
    assert verified.status_code == 200, verified.text
    consent = await _create_consent(client, connection_id=connection["connection_id"], key="publication-session-consent-key")
    revoke_before_publication = True
    original_transport = provider.__call__

    async def revoking_transport(url: str, **kwargs: Any):
        response = await original_transport(url, **kwargs)
        if revoke_before_publication and "/calendar/v3/calendars/owner-calendar/events" in url:
            async with async_db() as db:
                session = await db.get(OperatorSession, OWNER_SESSION)
                assert session is not None
                session.revoked_at = datetime.now(timezone.utc)
                await db.flush()
        return response

    monkeypatch.setattr("src.integrations.google_calendar.request_pinned_https", revoking_transport)
    response = await client.get(
        f"/api/calendar/connections/{connection['connection_id']}/events",
        params={"consent_id": consent["consent_id"], "max_events": 10},
    )
    detail = _safe_detail(response, status=409)
    assert detail["code"] == "calendar_control_reconciliation_required"
    async with async_db() as db:
        assert (
            await db.execute(
                select(CalendarEventBinding).where(
                    CalendarEventBinding.owner_principal_id == OWNER_PRINCIPAL,
                    CalendarEventBinding.owner_session_id == OWNER_SESSION,
                )
            )
        ).scalars().first() is None


@pytest.mark.asyncio
async def test_safe_provider_failure_is_bounded_and_does_not_echo_transport_or_credentials(client, monkeypatch):
    connection = await _create_connection(client, secret="provider-secret-not-for-errors")

    async def failed_transport(*_args, **_kwargs):
        return PinnedResponse(
            url="https://oauth2.googleapis.com/token",
            status_code=503,
            headers={},
            content=b'{"error":"provider-secret-not-for-errors"}',
            pinned_address="8.8.8.8",
        )

    monkeypatch.setattr("src.integrations.google_calendar.request_pinned_https", failed_transport)
    response = await client.post(
        f"/api/calendar/connections/{connection['connection_id']}/verify",
        headers=ORIGIN_HEADERS,
        json={"expected_revision": connection["revision"], "idempotency_key": "provider-failure-key"},
    )
    detail = _safe_detail(response, status=409)
    assert "provider-secret-not-for-errors" not in response.text
    assert "oauth2.googleapis.com" not in response.text
    assert detail["code"] == "calendar_control_reconciliation_required"


@pytest.mark.asyncio
async def test_consent_requires_verified_calendar_membership(client, async_db, monkeypatch):
    await _seed_goal(async_db)
    connection = await _create_connection(client, key="membership-connection-key")
    consent_body = {
        "schema_version": 1,
        "connection_id": connection["connection_id"],
        "calendar_id": "owner-calendar",
        "goal_id": "calendar-goal",
        "goal_revision": 1,
        "allowed_fields": ["summary", "start", "end"],
        "window_minutes": 60,
        "max_events": 5,
        "allow_remote_model": True,
        "expires_at": _utc_after(1),
        "idempotency_key": "membership-consent-key",
    }
    before_verify = await client.post("/api/calendar/read-consents", headers=ORIGIN_HEADERS, json=consent_body)
    detail = _safe_detail(before_verify, status=409)
    assert detail["code"] == "calendar_setup_membership_required"

    provider = _GoogleTransportScript()
    monkeypatch.setattr("src.integrations.google_calendar.request_pinned_https", provider)
    verify = await client.post(
        f"/api/calendar/connections/{connection['connection_id']}/verify",
        headers=ORIGIN_HEADERS,
        json={"expected_revision": connection["revision"], "idempotency_key": "membership-verify-key"},
    )
    assert verify.status_code == 200, verify.text

    unknown = {**consent_body, "calendar_id": "not-in-verified-membership", "idempotency_key": "unknown-calendar-key"}
    unknown_response = await client.post("/api/calendar/read-consents", headers=ORIGIN_HEADERS, json=unknown)
    detail = _safe_detail(unknown_response, status=409)
    assert detail["code"] == "calendar_setup_membership_required"

    created = await client.post("/api/calendar/read-consents", headers=ORIGIN_HEADERS, json=consent_body)
    assert created.status_code in {200, 201}, created.text
    consent = created.json()["consent"]
    assert consent["connection_id"] == connection["connection_id"]

    revoke_request = {"expected_revision": consent["revision"], "idempotency_key": "membership-revoke-key"}
    revoked = await client.request(
        "DELETE",
        f"/api/calendar/read-consents/{consent['consent_id']}",
        headers=ORIGIN_HEADERS,
        json=revoke_request,
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["consent"]["state"] == "revoked"
    revoke_replay = await client.request(
        "DELETE",
        f"/api/calendar/read-consents/{consent['consent_id']}",
        headers=ORIGIN_HEADERS,
        json=revoke_request,
    )
    assert revoke_replay.status_code == 200, revoke_replay.text
    assert revoke_replay.json() == revoked.json()


@pytest.mark.asyncio
async def test_consent_same_key_concurrent_requests_share_one_row(client, async_db, monkeypatch):
    await _seed_goal(async_db)
    provider = _GoogleTransportScript()
    monkeypatch.setattr("src.integrations.google_calendar.request_pinned_https", provider)
    connection = await _create_connection(client, key="concurrent-consent-connection-key")
    verified = await client.post(
        f"/api/calendar/connections/{connection['connection_id']}/verify",
        headers=ORIGIN_HEADERS,
        json={"expected_revision": connection["revision"], "idempotency_key": "concurrent-consent-verify-key"},
    )
    assert verified.status_code == 200, verified.text
    request = {
        "schema_version": 1,
        "connection_id": connection["connection_id"],
        "calendar_id": "owner-calendar",
        "goal_id": "calendar-goal",
        "goal_revision": 1,
        "allowed_fields": ["summary", "start", "end"],
        "window_minutes": 60,
        "max_events": 5,
        "allow_remote_model": False,
        "expires_at": _utc_after(1),
        "idempotency_key": "concurrent-consent-key",
    }
    responses = await asyncio.gather(
        *(
            client.post("/api/calendar/read-consents", headers=ORIGIN_HEADERS, json=request)
            for _ in range(2)
        )
    )
    assert sorted(response.status_code for response in responses) == [200, 201], [response.text for response in responses]
    consent_ids = {response.json()["consent"]["consent_id"] for response in responses}
    assert len(consent_ids) == 1
    async with async_db() as db:
        rows = (
            await db.execute(
                select(CalendarReadConsent).where(
                    CalendarReadConsent.owner_principal_id == OWNER_PRINCIPAL,
                    CalendarReadConsent.owner_session_id == OWNER_SESSION,
                    CalendarReadConsent.creation_idempotency_key == "concurrent-consent-key",
                )
            )
        ).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_connection_revoke_is_durable_cascaded_and_exactly_replayable(client, async_db):
    connection = await _create_connection(client, key="revoke-connection-key")
    async with async_db() as db:
        row = await db.get(GoogleServiceConnection, connection["connection_id"])
        assert row is not None
        vault_key = row.vault_secret_key

    request = {"expected_revision": connection["revision"], "idempotency_key": "revoke-control-key"}
    revoked = await client.request(
        "DELETE",
        f"/api/calendar/connections/{connection['connection_id']}",
        headers=ORIGIN_HEADERS,
        json=request,
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["connection"]["state"] == "revoked"
    assert await vault_repository.get(vault_key) is None

    replay = await client.request(
        "DELETE",
        f"/api/calendar/connections/{connection['connection_id']}",
        headers=ORIGIN_HEADERS,
        json=request,
    )
    assert replay.status_code == 200, replay.text
    assert replay.json() == revoked.json()

    async with async_db() as db:
        jobs = (
            await db.execute(
                select(WorkflowRunState).where(
                    WorkflowRunState.owner_principal_id == OWNER_PRINCIPAL,
                    WorkflowRunState.session_id == OWNER_SESSION,
                    WorkflowRunState.job_kind == "calendar_connection_revoke",
                )
            )
        ).scalars().all()
        assert len(jobs) == 1
        assert jobs[0].status == "succeeded"
        effects = json.loads(jobs[0].effect_receipts_json or "[]")
        assert any(item.get("receipt_kind") == "readback" and item.get("status") == "succeeded" for item in effects)


@pytest.mark.asyncio
async def test_connection_revoke_preserves_active_occurrence_lane_occupancy(client, async_db):
    connection = await _create_connection(client, key="revoke-active-occurrence-connection-key")
    _consent_id, binding_id, occurrence_id = await _seed_running_occurrence(
        async_db, connection_id=connection["connection_id"]
    )
    response = await client.request(
        "DELETE",
        f"/api/calendar/connections/{connection['connection_id']}",
        headers=ORIGIN_HEADERS,
        json={"expected_revision": connection["revision"], "idempotency_key": "revoke-active-occurrence-key"},
    )
    assert response.status_code == 200, response.text
    async with async_db() as db:
        occurrence = await db.get(GovernedScheduleOccurrence, occurrence_id)
        binding = await db.get(GovernedScheduleBinding, binding_id)
        assert occurrence is not None and occurrence.state == "running"
        assert binding is not None and binding.state == "revoked"


@pytest.mark.asyncio
async def test_consent_revoke_preserves_unknown_occurrence_lane_occupancy(client, async_db):
    connection = await _create_connection(client, key="revoke-unknown-occurrence-connection-key")
    consent_id, binding_id, occurrence_id = await _seed_running_occurrence(
        async_db, connection_id=connection["connection_id"]
    )
    async with async_db() as db:
        occurrence = await db.get(GovernedScheduleOccurrence, occurrence_id)
        assert occurrence is not None
        occurrence.state = "unknown"
        await db.flush()
    response = await client.request(
        "DELETE",
        f"/api/calendar/read-consents/{consent_id}",
        headers=ORIGIN_HEADERS,
        json={"expected_revision": 1, "idempotency_key": "revoke-unknown-occurrence-key"},
    )
    assert response.status_code == 200, response.text
    async with async_db() as db:
        occurrence = await db.get(GovernedScheduleOccurrence, occurrence_id)
        binding = await db.get(GovernedScheduleBinding, binding_id)
        assert occurrence is not None and occurrence.state == "unknown"
        assert binding is not None and binding.state == "revoked"


@pytest.mark.asyncio
async def test_connection_revoke_cleanup_failure_is_unknown_and_does_not_retry(client, monkeypatch):
    connection = await _create_connection(client, key="revoke-failure-connection-key")
    calls = 0

    async def fail_delete(_key: str):
        nonlocal calls
        calls += 1
        raise OSError("simulated vault cleanup failure")

    monkeypatch.setattr("src.api.calendar.vault_repository.delete", fail_delete)
    request = {"expected_revision": connection["revision"], "idempotency_key": "revoke-failure-key"}
    first = await client.request(
        "DELETE",
        f"/api/calendar/connections/{connection['connection_id']}",
        headers=ORIGIN_HEADERS,
        json=request,
    )
    detail = _safe_detail(first, status=503)
    assert detail["code"] == "calendar_connection_cleanup_blocked"
    assert calls == 1

    retry = await client.request(
        "DELETE",
        f"/api/calendar/connections/{connection['connection_id']}",
        headers=ORIGIN_HEADERS,
        json=request,
    )
    _safe_detail(retry, status=409)
    assert calls == 1


@pytest.mark.asyncio
async def test_seven_day_sync_consent_requires_explicit_acknowledgement(client, async_db, monkeypatch):
    await _seed_goal(async_db)
    provider = _GoogleTransportScript()
    monkeypatch.setattr("src.integrations.google_calendar.request_pinned_https", provider)
    connection = await _create_connection(client, key="bounded-sync-connection")
    verified = await client.post(f"/api/calendar/connections/{connection['connection_id']}/verify", headers=ORIGIN_HEADERS, json={"expected_revision": connection["revision"], "idempotency_key": "bounded-sync-verify"})
    assert verified.status_code == 200, verified.text
    body = {"schema_version": 1, "connection_id": connection["connection_id"], "calendar_id": "owner-calendar", "goal_id": "calendar-goal", "goal_revision": 1, "allowed_fields": ["summary", "start", "end"], "window_minutes": 10080, "max_events": 50, "allow_remote_model": False, "expires_at": _utc_after(1), "idempotency_key": "bounded-sync-consent"}
    refused = await client.post("/api/calendar/read-consents", headers=ORIGIN_HEADERS, json=body)
    assert _safe_detail(refused, status=422)["code"] == "calendar_sync_acknowledgement_required"
    accepted = await client.post("/api/calendar/read-consents", headers=ORIGIN_HEADERS, json={**body, "acknowledge_sync_metadata": True})
    assert accepted.status_code in {200, 201}, accepted.text
    consent = accepted.json()["consent"]
    assert consent["window_minutes"] == 10080 and consent["sync_metadata_limit"] == 50
    replay = await client.post("/api/calendar/read-consents", headers=ORIGIN_HEADERS, json={**body, "acknowledge_sync_metadata": True})
    assert replay.status_code in {200, 201} and replay.json()["consent"]["consent_id"] == consent["consent_id"]
    changed = await client.post("/api/calendar/read-consents", headers=ORIGIN_HEADERS, json={**body, "window_minutes": 60})
    assert changed.status_code == 409
