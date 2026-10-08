from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.integrations.google_calendar import (
    CalendarIntegrationError,
    CalendarEventSnapshot,
    GoogleCalendarReadonlyAdapter,
    MeetingPrepService,
    persist_calendar_event_binding,
    canonical_event_key,
    calendar_artifact_path_for_job,
    calendar_input_digest,
    calendar_input_payload,
    digest,
    read_calendar_result_bytes,
    refresh_event_binding_from_snapshot,
    write_calendar_result_bytes,
)
from src.db.models import CalendarEventBinding, CalendarReadConsent, GoogleServiceConnection
from src.vault import decrypt, encrypt


def test_event_identity_keeps_same_provider_id_separate_per_calendar() -> None:
    event = {
        "id": "provider-event-42",
        "summary": "Same provider identifier",
        "start": {"dateTime": "2026-10-01T09:00:00Z"},
        "end": {"dateTime": "2026-10-01T10:00:00Z"},
    }
    first = canonical_event_key("operator:one", "connection:one", "calendar:one", event)
    second = canonical_event_key("operator:one", "connection:one", "calendar:two", event)
    assert first.startswith("sha256:")
    assert second.startswith("sha256:")
    assert first != second


def test_calendar_result_writer_and_reader_are_job_bound(tmp_path) -> None:
    job_id = "calendar-prep:0123456789abcdef0123456789abcdef"
    path = calendar_artifact_path_for_job(job_id)
    payload = json.dumps({"schema_version": 1, "summary": "bounded"}, separators=(",", ":")).encode()

    write_calendar_result_bytes(path, payload, workspace_root=tmp_path)

    assert read_calendar_result_bytes(path, workspace_root=tmp_path) == payload
    assert read_calendar_result_bytes("artifacts/work-board/calendar/../result-" + "0" * 32 + ".json", workspace_root=tmp_path) is None
    assert read_calendar_result_bytes(path.replace("calendar", "browser"), workspace_root=tmp_path) is None


def test_calendar_input_digest_matches_persisted_durable_envelope() -> None:
    typed_input = {"event_binding_id": "event-1", "event_revision": "sha256:" + "a" * 64}
    handoff = {"parent_handoff_context": [], "parent_handoff_digest": ""}
    payload = calendar_input_payload(typed_input, parent_handoff=handoff)

    assert payload == {"input": typed_input, "parent_handoff": handoff}
    assert calendar_input_digest(typed_input, parent_handoff=handoff) == digest(payload)


def test_calendar_model_output_rejects_bool_schema_and_non_string_summary() -> None:
    base = {
        "schema_version": 1,
        "event_key": "sha256:" + "a" * 64,
        "event_revision": "sha256:" + "b" * 64,
        "summary": "A bounded summary",
        "agenda": [],
        "questions": [],
        "risks": [],
        "preparation_steps": [],
    }

    invalid_schema = {**base, "schema_version": True}
    with pytest.raises(CalendarIntegrationError) as schema_error:
        MeetingPrepService.validate_model_output(
            invalid_schema,
            event_key=base["event_key"],
            event_revision=base["event_revision"],
        )
    assert schema_error.value.code == "calendar_model_output_invalid"

    invalid_summary = {**base, "summary": None}
    with pytest.raises(CalendarIntegrationError) as summary_error:
        MeetingPrepService.validate_model_output(
            invalid_summary,
            event_key=base["event_key"],
            event_revision=base["event_revision"],
        )
    assert summary_error.value.code == "calendar_provider_schema_invalid"


@pytest.mark.asyncio
async def test_calendar_prepare_rejects_changed_second_read() -> None:
    first = {
        "id": "event-1",
        "summary": "First version",
        "start": {"dateTime": "2026-10-01T09:00:00Z"},
        "end": {"dateTime": "2026-10-01T10:00:00Z"},
    }
    second = {**first, "summary": "Changed after synthesis"}

    class FakeAdapter:
        owner_principal_id = "operator:calendar"
        connection = SimpleNamespace(connection_id="connection:calendar")

        def __init__(self) -> None:
            self.events = [first, second]

        def _scrub(self, value):
            return value

        async def get_event(self, _calendar_id, _provider_event_id, *, allowed_fields=None):
            del allowed_fields
            return self.events.pop(0)

    async def model_call(payload):
        return {
            "schema_version": 1,
            "event_key": payload["event_key"],
            "event_revision": payload["event_revision"],
            "summary": "Prepared from the first read",
            "agenda": [],
            "questions": [],
            "risks": [],
            "preparation_steps": [],
        }

    with pytest.raises(CalendarIntegrationError) as error:
        await MeetingPrepService(FakeAdapter()).prepare(
            "primary",
            "event-1",
            allowed_fields={"summary"},
            model_call=model_call,
        )
    assert error.value.code == "stale_event_after_synthesis"


@pytest.mark.asyncio
async def test_calendar_consent_nonempty_creation_key_is_owner_session_unique(async_db) -> None:
    expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    first = CalendarReadConsent(
        owner_principal_id="operator:calendar-owner",
        owner_session_id="session:calendar-owner",
        connection_id="connection:first",
        creation_idempotency_key="consent-create-1",
        calendar_id=encrypt("primary"),
        goal_id="goal:calendar",
        expires_at=expires_at,
    )
    async with async_db() as db:
        db.add(first)
        await db.flush()

    duplicate = CalendarReadConsent(
        owner_principal_id=first.owner_principal_id,
        owner_session_id=first.owner_session_id,
        connection_id="connection:second",
        creation_idempotency_key=first.creation_idempotency_key,
        calendar_id=encrypt("secondary"),
        goal_id=first.goal_id,
        expires_at=expires_at,
    )
    with pytest.raises(IntegrityError):
        async with async_db() as db:
            db.add(duplicate)
            await db.flush()


def test_unchanged_event_binding_keeps_selection_revision_and_provenance() -> None:
    binding = SimpleNamespace(
        connection_id="connection-1",
        connection_revision=2,
        consent_id="consent-1",
        consent_revision=3,
        event_key="sha256:" + "1" * 64,
        event_revision="sha256:" + "2" * 64,
        calendar_list_revision="sha256:" + "3" * 64,
        revision=4,
        updated_at=None,
    )
    snapshot = SimpleNamespace(
        event_key=binding.event_key,
        event_revision=binding.event_revision,
        calendar_list_revision="sha256:" + "4" * 64,
    )

    changed = refresh_event_binding_from_snapshot(
        binding,
        snapshot,
        connection_id="connection-1",
        connection_revision=2,
        consent_id="consent-1",
        consent_revision=3,
    )

    assert changed is False
    assert binding.revision == 4
    assert binding.calendar_list_revision == "sha256:" + "3" * 64


def test_changed_event_binding_advances_revision_and_selection_provenance() -> None:
    binding = SimpleNamespace(
        connection_id="connection-1",
        connection_revision=2,
        consent_id="consent-1",
        consent_revision=3,
        event_key="sha256:" + "1" * 64,
        event_revision="sha256:" + "2" * 64,
        calendar_list_revision="sha256:" + "3" * 64,
        revision=4,
        updated_at=None,
    )
    snapshot = SimpleNamespace(
        event_key="sha256:" + "9" * 64,
        event_revision="sha256:" + "8" * 64,
        calendar_list_revision="sha256:" + "7" * 64,
    )

    changed = refresh_event_binding_from_snapshot(
        binding,
        snapshot,
        connection_id="connection-1",
        connection_revision=2,
        consent_id="consent-1",
        consent_revision=3,
    )

    assert changed is True
    assert binding.revision == 5
    assert binding.event_key == snapshot.event_key
    assert binding.calendar_list_revision == snapshot.calendar_list_revision


@pytest.mark.asyncio
async def test_persist_event_binding_is_owner_bound_and_keeps_unchanged_list_provenance(async_db) -> None:
    owner_principal_id = "operator:calendar-test"
    owner_session_id = "session:calendar-test"
    connection = GoogleServiceConnection(
        owner_principal_id=owner_principal_id,
        owner_session_id=owner_session_id,
        vault_secret_key="vault:calendar-test",
        revision=2,
        state="active",
    )
    consent = CalendarReadConsent(
        owner_principal_id=owner_principal_id,
        owner_session_id=owner_session_id,
        connection_id=connection.connection_id,
        calendar_id=encrypt("calendar:one"),
        consent_digest="sha256:" + "c" * 64,
        goal_id="goal:calendar-test",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        revision=3,
    )
    snapshot = CalendarEventSnapshot(
        event_key=canonical_event_key(
            owner_principal_id,
            connection.connection_id,
            "calendar:one",
            {"id": "provider-event-1"},
        ),
        event_revision="sha256:" + "2" * 64,
        calendar_list_revision="sha256:" + "3" * 64,
        provider_event_id="provider-event-1",
        recurrence_identity="single",
        fields={"summary": "Planning"},
    )

    async with async_db() as db:
        db.add(connection)
        db.add(consent)
        await db.flush()
        binding = await persist_calendar_event_binding(
            db,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            connection=connection,
            consent=consent,
            snapshot=snapshot,
        )
        assert binding.owner_principal_id == owner_principal_id
        assert binding.owner_session_id == owner_session_id
        assert decrypt(binding.calendar_id_private) == "calendar:one"
        assert decrypt(binding.provider_event_id_private) == "provider-event-1"
        assert binding.revision == 1
        assert binding.calendar_list_revision == snapshot.calendar_list_revision

        changed_list = CalendarEventSnapshot(
            **{
                **snapshot.__dict__,
                "calendar_list_revision": "sha256:" + "4" * 64,
            }
        )
        unchanged = await persist_calendar_event_binding(
            db,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            connection=connection,
            consent=consent,
            snapshot=changed_list,
        )
        assert unchanged.event_binding_id == binding.event_binding_id
        assert unchanged.revision == 1
        assert unchanged.calendar_list_revision == snapshot.calendar_list_revision

        changed_event = CalendarEventSnapshot(
            **{
                **changed_list.__dict__,
                "event_key": snapshot.event_key,
                "event_revision": "sha256:" + "8" * 64,
                "calendar_list_revision": "sha256:" + "7" * 64,
            }
        )
        advanced = await persist_calendar_event_binding(
            db,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            connection=connection,
            consent=consent,
            snapshot=changed_event,
        )
        assert advanced.event_binding_id == binding.event_binding_id
        assert advanced.revision == 2
        assert advanced.event_revision == changed_event.event_revision
        assert advanced.calendar_list_revision == changed_event.calendar_list_revision


@pytest.mark.asyncio
async def test_persist_event_binding_concurrent_same_identity_returns_one_row(async_db) -> None:
    owner_principal_id = "operator:calendar-race"
    owner_session_id = "session:calendar-race"
    connection = GoogleServiceConnection(
        owner_principal_id=owner_principal_id,
        owner_session_id=owner_session_id,
        vault_secret_key="vault:calendar-race",
        revision=1,
        state="active",
    )
    consent = CalendarReadConsent(
        owner_principal_id=owner_principal_id,
        owner_session_id=owner_session_id,
        connection_id=connection.connection_id,
        calendar_id=encrypt("calendar:race"),
        goal_id="goal:calendar-race",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        revision=1,
    )
    provider_event = {"id": "provider-event-race"}
    snapshot = CalendarEventSnapshot(
        event_key=canonical_event_key(
            owner_principal_id,
            connection.connection_id,
            "calendar:race",
            provider_event,
        ),
        event_revision="sha256:" + "a" * 64,
        calendar_list_revision="sha256:" + "b" * 64,
        provider_event_id="provider-event-race",
        recurrence_identity="single",
        fields={"summary": "Race"},
    )
    barrier = asyncio.Barrier(2)

    async def persist_once() -> str:
        async with async_db() as db:
            await barrier.wait()
            row = await persist_calendar_event_binding(
                db,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                connection=connection,
                consent=consent,
                snapshot=snapshot,
            )
            await db.commit()
            return row.event_binding_id

    first, second = await asyncio.gather(persist_once(), persist_once())
    assert first == second
    async with async_db() as db:
        rows = (
            await db.execute(
                select(CalendarEventBinding).where(
                    CalendarEventBinding.provider_identity_digest == digest(
                        (
                            owner_principal_id,
                            connection.connection_id,
                            "calendar:race",
                            "provider-event-race",
                            "single",
                        )
                    )
                )
            )
        ).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_calendar_adapter_checks_authority_before_token_and_read(monkeypatch) -> None:
    connection = GoogleServiceConnection(
        owner_principal_id="operator:calendar-test",
        owner_session_id="session:calendar-test",
        vault_secret_key="vault:calendar-test",
        state="active",
    )
    calls: list[str] = []

    async def authority_check() -> None:
        calls.append("authority")

    async def vault_get(_key: str) -> str:
        return json.dumps(
            {
                "client_id": "client-id-sentinel",
                "refresh_token": "refresh-token-sentinel",
                "client_secret": "client-secret-sentinel",
            }
        )

    async def request(url: str, **kwargs):
        if kwargs.get("method") == "POST":
            return SimpleNamespace(
                status_code=200,
                headers={"content-type": "application/json"},
                content=b'{"access_token":"access-token-sentinel"}',
            )
        if "calendarList" in url:
            return SimpleNamespace(
                status_code=200,
                headers={"content-type": "application/json"},
                content=b'{"items":[]}',
            )
        return SimpleNamespace(
            status_code=200,
            headers={"content-type": "application/json"},
            content=json.dumps(
                {
                    "items": [
                        {
                            "id": "event-1",
                            "summary": "access-token-sentinel",
                            "start": {"dateTime": "2026-10-01T09:00:00Z"},
                            "end": {"dateTime": "2026-10-01T10:00:00Z"},
                        }
                    ]
                }
            ).encode(),
        )

    monkeypatch.setattr("src.integrations.google_calendar.vault_repository.get", vault_get)
    monkeypatch.setattr("src.integrations.google_calendar.request_pinned_https", request)
    adapter = GoogleCalendarReadonlyAdapter(
        connection,
        owner_principal_id=connection.owner_principal_id,
        authority_check=authority_check,
    )

    assert await adapter._token() == "access-token-sentinel"
    assert calls == ["authority", "authority", "authority"]
    scrubbed = adapter._scrub(
        "client-id-sentinel refresh-token-sentinel client-secret-sentinel access-token-sentinel"
    )
    assert "sentinel" not in scrubbed

    await adapter.list_calendars()
    assert calls[-1] == "authority"
    assert len(calls) == 5
    events, _revision = await adapter.list_events(
        "calendar:one",
        time_min=datetime(2026, 10, 1, tzinfo=timezone.utc),
        time_max=datetime(2026, 10, 1, 1, tzinfo=timezone.utc),
    )
    assert events[0].fields["summary"] == "[redacted]"
    assert "sentinel" not in json.dumps(events[0].fields)
    assert len(calls) == 7


@pytest.mark.asyncio
async def test_calendar_adapter_maps_httpx_transport_failure_to_safe_unavailable(monkeypatch) -> None:
    connection = GoogleServiceConnection(
        owner_principal_id="operator:calendar-test",
        owner_session_id="session:calendar-test",
        vault_secret_key="vault:calendar-test",
        state="active",
    )

    async def vault_get(_key: str) -> str:
        return json.dumps({"client_id": "client-id", "refresh_token": "refresh-token"})

    async def failed_request(_url: str, **_kwargs):
        raise httpx.ConnectError("private provider detail", request=httpx.Request("GET", "https://www.googleapis.com"))

    monkeypatch.setattr("src.integrations.google_calendar.vault_repository.get", vault_get)
    monkeypatch.setattr("src.integrations.google_calendar.request_pinned_https", failed_request)
    adapter = GoogleCalendarReadonlyAdapter(
        connection,
        owner_principal_id=connection.owner_principal_id,
    )

    with pytest.raises(CalendarIntegrationError) as error:
        await adapter.list_events(
            "calendar:one",
            time_min=datetime(2026, 10, 1, tzinfo=timezone.utc),
            time_max=datetime(2026, 10, 1, 1, tzinfo=timezone.utc),
        )
    assert error.value.code == "calendar_provider_unavailable"
    assert error.value.status_code == 503
    assert "private provider detail" not in str(error.value)


@pytest.mark.asyncio
async def test_calendar_adapter_rejects_non_json_content_type_and_malformed_attendee(monkeypatch) -> None:
    connection = GoogleServiceConnection(
        owner_principal_id="operator:calendar-test",
        owner_session_id="session:calendar-test",
        vault_secret_key="vault:calendar-test",
        state="active",
    )

    async def vault_get(_key: str) -> str:
        return json.dumps({"client_id": "client-id", "refresh_token": "refresh-token"})

    response_mode = "content-type"

    async def response_request(_url: str, **kwargs):
        if kwargs.get("method") == "POST":
            return SimpleNamespace(
                status_code=200,
                headers={"content-type": "application/json"},
                content=b'{"access_token":"access-token"}',
            )
        if response_mode == "missing-headers":
            return SimpleNamespace(status_code=200, content=b'{"items":[]}')
        if response_mode == "content-type":
            return SimpleNamespace(
                status_code=200,
                headers={"content-type": "text/html"},
                content=b'{"items":[]}',
            )
        return SimpleNamespace(
            status_code=200,
            headers={"content-type": "application/json; charset=utf-8"},
            content=json.dumps(
                {
                    "items": [
                        {
                            "id": "event-1",
                            "summary": "Calendar event",
                            "start": {"dateTime": "2026-10-01T09:00:00Z"},
                            "end": {"dateTime": "2026-10-01T10:00:00Z"},
                            "attendees": ["malformed-row"],
                        }
                    ]
                }
            ).encode(),
        )

    monkeypatch.setattr("src.integrations.google_calendar.vault_repository.get", vault_get)
    monkeypatch.setattr("src.integrations.google_calendar.request_pinned_https", response_request)
    adapter = GoogleCalendarReadonlyAdapter(
        connection,
        owner_principal_id=connection.owner_principal_id,
    )

    with pytest.raises(CalendarIntegrationError) as content_error:
        await adapter.list_calendars()
    assert content_error.value.code == "calendar_provider_schema_invalid"

    response_mode = "missing-headers"
    with pytest.raises(CalendarIntegrationError) as missing_headers_error:
        await adapter.list_calendars()
    assert missing_headers_error.value.code == "calendar_provider_schema_invalid"

    response_mode = "attendee"
    with pytest.raises(CalendarIntegrationError) as attendee_error:
        await adapter.list_events(
            "calendar:one",
            time_min=datetime(2026, 10, 1, tzinfo=timezone.utc),
            time_max=datetime(2026, 10, 1, 1, tzinfo=timezone.utc),
            allowed_fields={"summary", "attendees"},
        )
    assert attendee_error.value.code == "calendar_provider_schema_invalid"


@pytest.mark.asyncio
async def test_calendar_event_pagination_is_aggregate_bounded_and_rejects_bad_cursor(monkeypatch) -> None:
    connection = GoogleServiceConnection(
        owner_principal_id="operator:calendar-test",
        owner_session_id="session:calendar-test",
        vault_secret_key="vault:calendar-test",
        state="active",
    )

    async def vault_get(_key: str) -> str:
        return json.dumps({"client_id": "client-id", "refresh_token": "refresh-token"})

    mode = "aggregate"
    page = 0

    async def paged_request(_url: str, **kwargs):
        nonlocal page
        if kwargs.get("method") == "POST":
            return SimpleNamespace(
                status_code=200,
                headers={"content-type": "application/json"},
                content=b'{"access_token":"access-token"}',
            )
        headers = {"content-type": "application/json"}
        if mode == "bad-cursor":
            body = {
                "items": [],
                "nextPageToken": {"unexpected": "object"},
            }
        elif page == 0:
            page += 1
            body = {
                "items": [
                    {
                        "id": "event-1",
                        "summary": "First page",
                        "start": {"dateTime": "2026-10-01T09:00:00Z"},
                        "end": {"dateTime": "2026-10-01T10:00:00Z"},
                    }
                ],
                "nextPageToken": "next",
                "filler": "a" * 180_000,
            }
        else:
            body = {
                "items": [
                    {
                        "id": "event-2",
                        "summary": "Second page",
                        "start": {"dateTime": "2026-10-01T11:00:00Z"},
                        "end": {"dateTime": "2026-10-01T12:00:00Z"},
                    }
                ],
                "filler": "b" * 100_000,
            }
        return SimpleNamespace(status_code=200, headers=headers, content=json.dumps(body).encode())

    monkeypatch.setattr("src.integrations.google_calendar.vault_repository.get", vault_get)
    monkeypatch.setattr("src.integrations.google_calendar.request_pinned_https", paged_request)
    adapter = GoogleCalendarReadonlyAdapter(
        connection,
        owner_principal_id=connection.owner_principal_id,
    )
    events, revision = await adapter.list_events(
        "calendar:one",
        time_min=datetime(2026, 10, 1, tzinfo=timezone.utc),
        time_max=datetime(2026, 10, 1, 1, tzinfo=timezone.utc),
    )
    assert [event.provider_event_id for event in events] == ["event-1"]
    assert revision.pages_read == 2
    assert revision.truncated is True

    mode = "bad-cursor"
    page = 0
    adapter = GoogleCalendarReadonlyAdapter(
        connection,
        owner_principal_id=connection.owner_principal_id,
    )
    with pytest.raises(CalendarIntegrationError) as cursor_error:
        await adapter.list_events(
            "calendar:one",
            time_min=datetime(2026, 10, 1, tzinfo=timezone.utc),
            time_max=datetime(2026, 10, 1, 1, tzinfo=timezone.utc),
        )
    assert cursor_error.value.code == "calendar_provider_schema_invalid"


@pytest.mark.asyncio
async def test_calendar_adapter_marks_provider_contact_before_token_and_each_read(monkeypatch) -> None:
    connection = GoogleServiceConnection(
        owner_principal_id="operator:calendar-test",
        owner_session_id="session:calendar-test",
        vault_secret_key="vault:calendar-test",
        state="active",
    )
    contacts: list[str] = []

    async def vault_get(_key: str) -> str:
        return json.dumps({"client_id": "client-id", "refresh_token": "refresh-token"})

    async def request(_url: str, **kwargs):
        if kwargs.get("method") == "POST":
            return SimpleNamespace(
                status_code=200,
                headers={"content-type": "application/json"},
                content=b'{"access_token":"access-token"}',
            )
        return SimpleNamespace(
            status_code=200,
            headers={"content-type": "application/json"},
            content=b'{"items":[]}',
        )

    monkeypatch.setattr("src.integrations.google_calendar.vault_repository.get", vault_get)
    monkeypatch.setattr("src.integrations.google_calendar.request_pinned_https", request)
    adapter = GoogleCalendarReadonlyAdapter(
        connection,
        owner_principal_id=connection.owner_principal_id,
        contact_observer=lambda: contacts.append("contact"),
    )

    await adapter._token()
    await adapter.list_calendars()
    assert contacts == ["contact", "contact"]


@pytest.mark.asyncio
async def test_calendar_adapter_exposes_httpx_transport_quiescence_proof() -> None:
    connection = GoogleServiceConnection(
        owner_principal_id="operator:calendar-test",
        owner_session_id="session:calendar-test",
        vault_secret_key="vault:calendar-test",
        state="active",
    )

    async def resolver(_host: str, _port: int) -> list[str]:
        return ["93.184.216.34"]

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b'{"items":[]}',
            request=request,
        )

    adapter = GoogleCalendarReadonlyAdapter(
        connection,
        owner_principal_id=connection.owner_principal_id,
        resolver=resolver,
        transport=httpx.MockTransport(handler),
    )
    payload = await adapter._get("https://www.googleapis.com/calendar/v3/test")

    assert payload == {"items": []}
    assert adapter.transport_quiescence() == {
        "status": "verified",
        "active_operations": 0,
        "unsettled_operations": 0,
        "requests_started": 1,
        "requests_settled": 1,
    }
    assert adapter.active_operations == 0
    assert adapter.unsettled_operations == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["token", "authorized_get"])
async def test_calendar_adapter_maps_real_httpx_close_failure_without_releasing_lifecycle(
    monkeypatch,
    operation: str,
) -> None:
    connection = GoogleServiceConnection(
        owner_principal_id="operator:calendar-test",
        owner_session_id="session:calendar-test",
        vault_secret_key="vault:calendar-test",
        state="active",
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = {"access_token": "access-token"} if request.method == "POST" else {"items": []}
        return httpx.Response(200, headers={"content-type": "application/json"}, json=payload, request=request)

    class CloseFailureTransport(httpx.MockTransport):
        async def aclose(self) -> None:
            await super().aclose()
            raise RuntimeError("private close detail")

    if operation == "token":
        async def vault_get(_key: str) -> str:
            return json.dumps({"client_id": "client-id", "refresh_token": "refresh-token"})

        monkeypatch.setattr("src.integrations.google_calendar.vault_repository.get", vault_get)

    adapter = GoogleCalendarReadonlyAdapter(
        connection,
        owner_principal_id=connection.owner_principal_id,
        resolver=lambda _host, _port: ["93.184.216.34"],
        transport=CloseFailureTransport(handler),
    )
    if operation == "authorized_get":
        adapter._access_token = "access-token"

    with pytest.raises(CalendarIntegrationError) as error:
        if operation == "token":
            await adapter._token()
        else:
            await adapter.list_calendars()

    assert error.value.code == "calendar_provider_unavailable"
    assert error.value.status_code == 503
    assert "private close detail" not in str(error.value)
    assert adapter.transport_quiescence() == {
        "status": "unknown",
        "active_operations": 1,
        "unsettled_operations": 1,
        "requests_started": 1,
        "requests_settled": 0,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["token", "authorized_get"])
async def test_calendar_adapter_maps_resolver_oserror_to_safe_no_contact_failure(
    monkeypatch,
    operation: str,
) -> None:
    connection = GoogleServiceConnection(
        owner_principal_id="operator:calendar-test",
        owner_session_id="session:calendar-test",
        vault_secret_key="vault:calendar-test",
        state="active",
    )
    contacted = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal contacted
        contacted = True
        return httpx.Response(200, headers={"content-type": "application/json"}, json={"items": []}, request=request)

    async def resolver(_host: str, _port: int) -> list[str]:
        raise OSError("private resolver detail")

    if operation == "token":
        async def vault_get(_key: str) -> str:
            return json.dumps({"client_id": "client-id", "refresh_token": "refresh-token"})

        monkeypatch.setattr("src.integrations.google_calendar.vault_repository.get", vault_get)

    adapter = GoogleCalendarReadonlyAdapter(
        connection,
        owner_principal_id=connection.owner_principal_id,
        resolver=resolver,
        transport=httpx.MockTransport(handler),
    )
    if operation == "authorized_get":
        adapter._access_token = "access-token"

    with pytest.raises(CalendarIntegrationError) as error:
        if operation == "token":
            await adapter._token()
        else:
            await adapter.list_calendars()

    assert error.value.code == "calendar_provider_unavailable"
    assert error.value.status_code == 503
    assert "private resolver detail" not in str(error.value)
    assert contacted is False
    assert adapter.transport_quiescence() == {
        "status": "verified",
        "active_operations": 0,
        "unsettled_operations": 0,
        "requests_started": 1,
        "requests_settled": 1,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("reader", ["_get", "_authorized_get"])
async def test_calendar_adapter_preserves_authority_runtime_error(reader: str) -> None:
    connection = GoogleServiceConnection(
        owner_principal_id="operator:calendar-test",
        owner_session_id="session:calendar-test",
        vault_secret_key="vault:calendar-test",
        state="active",
    )

    async def authority_check() -> None:
        raise RuntimeError("authority detail")

    adapter = GoogleCalendarReadonlyAdapter(
        connection,
        owner_principal_id=connection.owner_principal_id,
        authority_check=authority_check,
    )
    if reader == "_authorized_get":
        adapter._access_token = "access-token"

    with pytest.raises(RuntimeError, match="authority detail"):
        if reader == "_get":
            await adapter._get("https://www.googleapis.com/calendar/v3/test")
        else:
            await adapter.list_calendars()

    assert adapter.transport_quiescence() == {
        "status": "verified",
        "active_operations": 0,
        "unsettled_operations": 0,
        "requests_started": 0,
        "requests_settled": 0,
    }


@pytest.mark.asyncio
async def test_calendar_prepare_stops_before_provider_read_after_authority_revoke(monkeypatch) -> None:
    connection = GoogleServiceConnection(
        owner_principal_id="operator:calendar-test",
        owner_session_id="session:calendar-test",
        vault_secret_key="vault:calendar-test",
        state="active",
    )
    authority_calls = 0
    provider_requests: list[str] = []

    async def authority_check() -> None:
        nonlocal authority_calls
        authority_calls += 1
        if authority_calls == 4:
            raise CalendarIntegrationError(
                "calendar_revision_stale",
                "Calendar authority was revoked",
                recovery_action="refresh_event",
            )

    async def vault_get(_key: str) -> str:
        return json.dumps({"client_id": "client-id", "refresh_token": "refresh-token"})

    async def request(_url: str, **kwargs):
        provider_requests.append(str(kwargs.get("method", "GET")))
        if kwargs.get("method") == "POST":
            return SimpleNamespace(
                status_code=200,
                headers={"content-type": "application/json"},
                content=b'{"access_token":"access-token"}',
            )
        return SimpleNamespace(
            status_code=200,
            headers={"content-type": "application/json"},
            content=json.dumps(
                {
                    "id": "event-1",
                    "summary": "Stable event",
                    "start": {"dateTime": "2026-10-01T09:00:00Z"},
                    "end": {"dateTime": "2026-10-01T10:00:00Z"},
                }
            ).encode(),
        )

    monkeypatch.setattr("src.integrations.google_calendar.vault_repository.get", vault_get)
    monkeypatch.setattr("src.integrations.google_calendar.request_pinned_https", request)
    adapter = GoogleCalendarReadonlyAdapter(
        connection,
        owner_principal_id=connection.owner_principal_id,
        authority_check=authority_check,
    )

    async def model_call(payload):
        return {
            "schema_version": 1,
            "event_key": payload["event_key"],
            "event_revision": payload["event_revision"],
            "summary": "Prepared result",
            "agenda": [],
            "questions": [],
            "risks": [],
            "preparation_steps": [],
        }

    with pytest.raises(CalendarIntegrationError) as error:
        await MeetingPrepService(adapter).prepare(
            "primary",
            "event-1",
            allowed_fields={"summary"},
            model_call=model_call,
        )
    assert error.value.code == "calendar_revision_stale"
    assert provider_requests == ["POST"]


@pytest.mark.asyncio
async def test_calendar_list_revision_includes_final_page_and_provider_payload_is_scrubbed(monkeypatch) -> None:
    connection = GoogleServiceConnection(
        owner_principal_id="operator:calendar-test",
        owner_session_id="session:calendar-test",
        vault_secret_key="vault:calendar-test",
        state="active",
    )
    final_marker = {"value": "final-one"}

    async def vault_get(_key: str) -> str:
        return json.dumps({"client_id": "client-id", "refresh_token": "refresh-token"})

    async def request(url: str, **kwargs):
        if kwargs.get("method") == "POST":
            return SimpleNamespace(
                status_code=200,
                headers={"content-type": "application/json"},
                content=b'{"access_token":"access-token"}',
            )
        if "pageToken=next" in url:
            body = {
                "etag": final_marker["value"],
                "items": [
                    {
                        "id": "event-2",
                        "summary": "refresh-token",
                        "start": {"dateTime": "2026-10-01T10:00:00Z"},
                        "end": {"dateTime": "2026-10-01T11:00:00Z"},
                    }
                ],
            }
        else:
            body = {
                "etag": "first",
                "items": [
                    {
                        "id": "event-1",
                        "summary": "First",
                        "start": {"dateTime": "2026-10-01T09:00:00Z"},
                        "end": {"dateTime": "2026-10-01T10:00:00Z"},
                    }
                ],
                "nextPageToken": "next",
            }
        return SimpleNamespace(
            status_code=200,
            headers={"content-type": "application/json"},
            content=json.dumps(body).encode(),
        )

    monkeypatch.setattr("src.integrations.google_calendar.vault_repository.get", vault_get)
    monkeypatch.setattr("src.integrations.google_calendar.request_pinned_https", request)

    async def read_revision() -> tuple[str, list[CalendarEventSnapshot]]:
        adapter = GoogleCalendarReadonlyAdapter(
            connection,
            owner_principal_id=connection.owner_principal_id,
        )
        snapshots, revision = await adapter.list_events(
            "calendar:one",
            time_min=datetime(2026, 10, 1, tzinfo=timezone.utc),
            time_max=datetime(2026, 10, 1, 12, tzinfo=timezone.utc),
            max_events=2,
        )
        return revision.digest, snapshots

    first_revision, first_snapshots = await read_revision()
    assert len(first_snapshots) == 2
    assert first_snapshots[1].fields["summary"] == "[redacted]"
    final_marker["value"] = "final-two"
    second_revision, _ = await read_revision()
    assert second_revision != first_revision
