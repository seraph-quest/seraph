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
@pytest.mark.parametrize("invalid", [set(), {"*"}, {"summary,description"}, {"attachments"}, {1}, ["summary"], {"attendees(email)"}, {"attendees/responseStatus"}, {"start(timeZone)"}, {"organizer"}, {"extendedProperties"}, {"summary": {"nested": "*"}}])
async def test_invalid_consent_denied_before_any_contact(invalid):
    calls = []
    value = adapter(httpx.MockTransport(lambda req: calls.append(req)))
    value._access_token = None  # Invalid input must not reach OAuth/metadata either.
    with pytest.raises(CalendarIntegrationError, match="fields are invalid"):
        await value.get_event("calendar", "event", allowed_fields=invalid)
    with pytest.raises(CalendarIntegrationError, match="fields are invalid"):
        await value.list_sync_page("calendar", time_min=datetime.now(timezone.utc), time_max=datetime.now(timezone.utc) + timedelta(hours=1), max_events=1, allowed_fields=invalid)
    with pytest.raises(CalendarIntegrationError, match="fields are invalid"):
        await value.list_events("calendar", time_min=datetime.now(timezone.utc), time_max=datetime.now(timezone.utc) + timedelta(hours=1), allowed_fields=invalid)
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


@pytest.mark.asyncio
@pytest.mark.parametrize('field,optional_selector', [('summary','summary'),('start',None),('end',None),('location','location'),('description','description'),('attendees','attendees(displayName,email)')])
async def test_primary_list_each_selected_field_closed_subfields_on_every_page(field, optional_selector):
    """The transport sees the finite mask before any event resource arrives."""
    calls=[]
    expected_fields = {
        'summary':'end(date,dateTime),start(date,dateTime),summary',
        'start':'end(date,dateTime),start(date,dateTime)',
        'end':'end(date,dateTime),start(date,dateTime)',
        'location':'end(date,dateTime),location,start(date,dateTime)',
        'description':'description,end(date,dateTime),start(date,dateTime)',
        'attendees':'attendees(displayName,email),end(date,dateTime),start(date,dateTime)',
    }
    expected='nextPageToken,items(id,etag,recurringEventId,originalStartTime(date,dateTime),'+expected_fields[field]+')'
    def response(request):
        calls.append(request)
        assert request.url.params['fields']==expected
        assert 'attachments' not in expected and 'organizer' not in expected and '*' not in expected
        if optional_selector: assert optional_selector in expected
        index=len(calls)
        event={'id':f'event-{index}','etag':f'etag-{index}','recurringEventId':'series','originalStartTime':{'dateTime':f'2026-10-0{index}T09:00:00Z'},'start':{'dateTime':f'2026-10-0{index}T09:00:00Z'},'end':{'dateTime':f'2026-10-0{index}T10:00:00Z'}}
        if field=='attendees': event['attendees']=[{'displayName':'Selected person','email':'selected@example.invalid'}]
        elif field in {'summary','location','description'}: event[field]='Selected '+field
        payload={'items':[event]}
        if index<3: payload['nextPageToken']=f'next-{index}'
        return httpx.Response(200,json=payload)
    result, revision=await adapter(httpx.MockTransport(response)).list_events('calendar',time_min=datetime(2026,10,1,tzinfo=timezone.utc),time_max=datetime(2026,10,5,tzinfo=timezone.utc),allowed_fields={field},max_events=3)
    assert len(result)==3 and revision.pages_read==3 and revision.truncated  # Existing conservative three-page cap.
    assert [request.url.params.get('pageToken') for request in calls]==[None,'next-1','next-2']
    assert all(set(request.url.params)=={'timeMin','timeMax','singleEvents','orderBy','maxResults','fields'} | ({'pageToken'} if index else set()) for index,request in enumerate(calls))


@pytest.mark.asyncio
async def test_primary_legacy_none_preserves_unmasked_list_and_default_projection():
    calls=[]
    def response(request):
        calls.append(request)
        return httpx.Response(200,json={'items':[{'id':'legacy','summary':'Legacy','start':{'date':'2026-10-01'},'end':{'date':'2026-10-02'},'location':'Legacy location','description':'legacy entire-resource description'}]})
    result, revision=await adapter(httpx.MockTransport(response)).list_events('calendar',time_min=datetime(2026,10,1,tzinfo=timezone.utc),time_max=datetime(2026,10,2,tzinfo=timezone.utc))
    assert 'fields' not in calls[0].url.params
    assert result[0].fields['location']=='Legacy location' and result[0].fields['description'] is None
    assert revision.pages_read==1


@pytest.mark.asyncio
@pytest.mark.parametrize('allow_description',[False,True])
async def test_primary_list_actual_local_http_partial_response_has_no_unselected_private_canary(allow_description):
    """Provider-side omission, rather than local projection, enforces consent."""
    sent=[]
    mask='nextPageToken,items(id,etag,recurringEventId,originalStartTime(date,dateTime),'+('description,' if allow_description else '')+'end(date,dateTime),start(date,dateTime),summary)'
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            request=httpx.Request('GET','https://www.googleapis.com'+self.path)
            assert request.url.params['fields']==mask
            event={'id':'primary','etag':'primary-etag','summary':'Selected summary','start':{'dateTime':'2026-10-01T09:00:00Z'},'end':{'dateTime':'2026-10-01T10:00:00Z'}}
            if allow_description: event['description']='Explicit selected description'
            # The provider holds these fields; the exact request excludes them
            # before the backend receives or buffers a response body.
            private={'attendees':'unselected-attendee-canary','location':'unselected-location-canary','attachments':'unselected-attachment-canary'}
            assert not any(key in mask for key in private)
            raw=json.dumps({'items':[event]}).encode()
            sent.append(raw)
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True)
    thread.start()
    class LocalTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self,request):
            assert request.url.host=='www.googleapis.com'
            async with httpx.AsyncClient(trust_env=False,timeout=2) as client:
                response=await client.get(f'http://127.0.0.1:{server.server_port}'+request.url.raw_path.decode())
            return httpx.Response(response.status_code,content=response.content,headers={'Content-Type':'application/json'})
    try:
        fields={'summary','start','end'} | ({'description'} if allow_description else set())
        value=adapter(LocalTransport())
        result,revision=await value.list_events('calendar',time_min=datetime(2026,10,1,tzinfo=timezone.utc),time_max=datetime(2026,10,2,tzinfo=timezone.utc),allowed_fields=fields,max_events=1)
        assert len(result)==1 and revision.pages_read==1
        assert result[0].fields['description']==('Explicit selected description' if allow_description else None)
        assert len(sent)==1 and b'canary' not in sent[0]
        assert value.transport_quiescence()['status']=='verified'
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)


@pytest.mark.asyncio
@pytest.mark.parametrize('selected,content_selector',[
    ({'summary','start','end'},'end(date,dateTime),start(date,dateTime),summary'),
    ({'summary','start','end','location','description','attendees'},'attendees(displayName,email),description,end(date,dateTime),location,start(date,dateTime),summary'),
])
async def test_primary_group_mask_has_only_closed_content_and_minimal_binding_metadata(selected,content_selector):
    calls=[]
    def response(request):
        calls.append(request)
        assert request.url.params['fields']=='nextPageToken,items(id,etag,recurringEventId,originalStartTime(date,dateTime),'+content_selector+')'
        return httpx.Response(200,json={'items':[{'id':'group','etag':'etag','summary':'Selected','start':{'date':'2026-10-01'},'end':{'date':'2026-10-02'}}]})
    events,revision=await adapter(httpx.MockTransport(response)).list_events('calendar',time_min=datetime(2026,10,1,tzinfo=timezone.utc),time_max=datetime(2026,10,2,tzinfo=timezone.utc),allowed_fields=selected,max_events=1)
    assert len(events)==len(calls)==1 and revision.pages_read==1
    assert 'updated' not in calls[0].url.params['fields'] and 'status' not in calls[0].url.params['fields']
