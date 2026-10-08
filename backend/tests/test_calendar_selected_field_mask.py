"""Provider-side selected consent masks; no external sockets or credentials."""
import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from src.db.models import GoogleServiceConnection
from src.integrations.google_calendar import CalendarIntegrationError, GoogleCalendarReadonlyAdapter, canonical_event_key


def adapter(transport):
    connection = GoogleServiceConnection(connection_id="fixture", owner_principal_id="fixture-owner", owner_session_id="fixture-session", state="active")
    value = GoogleCalendarReadonlyAdapter(connection, owner_principal_id="fixture-owner", transport=transport, resolver=lambda *_: ["8.8.8.8"])
    value._access_token = "fixture-only-token"
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize("field,selector", [("summary", "summary"), ("start", "start(date,dateTime)"), ("end", "end(date,dateTime)"), ("location", "location"), ("description", "description"), ("attendees", "attendees(displayName,email)")])
async def test_each_closed_consent_field_maps_to_exact_provider_selector(field, selector):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"id": "event"})
    await adapter(httpx.MockTransport(respond)).get_event("calendar", "event", allowed_fields={field})
    assert len(calls) == 1
    assert calls[0].url.params["fields"] == "id,etag,recurringEventId,originalStartTime(date,dateTime)," + selector
    assert set(calls[0].url.params) == {"fields"}


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [set(), {"*"}, {"summary,description"}, {"attachments"}, {1}, ["summary"]])
async def test_invalid_consent_denied_before_any_contact(invalid):
    calls = []
    value = adapter(httpx.MockTransport(lambda req: calls.append(req)))
    with pytest.raises(CalendarIntegrationError, match="fields are invalid"):
        await value.get_event("calendar", "event", allowed_fields=invalid)
    with pytest.raises(CalendarIntegrationError, match="fields are invalid"):
        await value.list_sync_page("calendar", time_min=datetime.now(timezone.utc), time_max=datetime.now(timezone.utc) + timedelta(hours=1), max_events=1, allowed_fields=invalid)
    assert calls == []


@pytest.mark.asyncio
async def test_legacy_none_keeps_existing_unmasked_event_read():
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"description": "legacy authorized whole event"})
    assert (await adapter(httpx.MockTransport(respond)).get_event("calendar", "event"))["description"] == "legacy authorized whole event"
    assert "fields" not in calls[0].url.params


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_description", [False, True])
async def test_selected_sync_uses_real_local_http_partial_response_without_unselected_canary(allow_description):
    event = {"id": "event", "etag": "etag-one", "recurringEventId": "series", "originalStartTime": {"dateTime": "2026-10-01T09:00:00Z"},
             "summary": "Selected", "start": {"dateTime": "2026-10-01T09:00:00Z"}, "end": {"dateTime": "2026-10-01T10:00:00Z"}}
    sent = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            request = httpx.Request("GET", "https://www.googleapis.com" + self.path)
            fields = request.url.params["fields"]
            if request.url.path.endswith("/events"):
                payload = {"items": [event]}
            else:
                expected = "id,etag,recurringEventId,originalStartTime(date,dateTime)," + ("description," if allow_description else "") + "end(date,dateTime),start(date,dateTime),summary"
                assert fields == expected
                payload = dict(event)
                if allow_description:
                    payload["description"] = "explicitly selected description"
                # This provider fixture contains private canaries but sends none
                # for attendees/location/attachments outside the exact mask.
                private = {"attendees": "unselected-attendee-canary", "location": "unselected-location-canary", "attachments": "unselected-attachment-canary"}
                assert not any(key in fields for key in private)
            raw = json.dumps(payload).encode()
            sent.append((fields, raw))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    class LocalTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            assert request.url.host == "www.googleapis.com"
            async with httpx.AsyncClient(trust_env=False, timeout=2) as client:
                response = await client.get(f"http://127.0.0.1:{server.server_port}" + request.url.raw_path.decode())
            return httpx.Response(response.status_code, content=response.content, headers={"Content-Type": "application/json"})
    try:
        value = adapter(LocalTransport())
        key = canonical_event_key("fixture-owner", "fixture", "calendar", event)
        fields = {"summary", "start", "end"} | ({"description"} if allow_description else set())
        result, token = await value.list_sync_page("calendar", time_min=datetime(2026, 10, 1, tzinfo=timezone.utc), time_max=datetime(2026, 10, 2, tzinfo=timezone.utc), max_events=1, private_event_keys={key}, allowed_fields=fields)
        assert token is None and len(result) == 1 and result[0].event_key == key
        assert result[0].fields["description"] == ("explicitly selected description" if allow_description else None)
        assert len(sent) == 2  # Existing metadata read + one selected private read.
        assert b"canary" not in b"".join(raw for _, raw in sent)
        assert value.transport_quiescence()["status"] == "verified"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
