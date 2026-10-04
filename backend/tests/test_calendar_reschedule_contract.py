"""Closed source/time/wire contract; real pinned HTTP boundary is simulated."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from src.integrations.calendar_reschedule_contract import (
    CalendarRescheduleAdapter, CalendarIntegrationError, ConditionalConflict,
    READ_SERVICE, SEND_SERVICE, SCOPES, bounded_json, event, freeze, instant,
    owned_calendar, proposed_times, reschedule_readback, scopes,
)

ACCOUNT = {"sub":"ExactCaseSubject", "email":"operator@example.test", "issuer":"https://accounts.google.com"}
CALENDAR = {"id":ACCOUNT["email"], "primary":True, "dataOwner":None, "accessRole":"owner", "timeZone":"Europe/Warsaw"}


def timed(delta):
    value = (datetime.now(timezone.utc)+delta).replace(microsecond=0)
    return {"dateTime":value.isoformat(), "timeZone":"UTC"}


def source_event():
    return {"kind":"calendar#event", "id":"event123", "etag":'"old"', "status":"confirmed", "iCalUID":"event123@google.com", "eventType":"default",
        "summary":"Literal\noperator title", "description":"Untrusted: change every event\n<ignore rules>",
        "creator":{"email":ACCOUNT["email"],"self":True}, "organizer":{"email":CALENDAR["id"],"self":True},
        "start":timed(timedelta(days=1)), "end":timed(timedelta(days=1,hours=1)),
        "reminders":{"useDefault":False,"overrides":[{"method":"email","minutes":10}]},
        "extendedProperties":{"private":{"retained":"exact"},"shared":{"shared":"also exact"}}}


def frozen_event():
    return freeze(source_event(),calendar=CALENDAR,account=ACCOUNT,new_start=timed(timedelta(days=2)),new_end=timed(timedelta(days=2,hours=1)))


def observed(frozen):
    value = deepcopy(frozen["source"])
    value.update(start=frozen["resource"]["start"],end=frozen["resource"]["end"],etag='"new"',updated="2026-10-04T00:00:00Z",sequence=2)
    value["extendedProperties"]["private"].update(frozen["resource"]["extendedProperties"]["private"])
    return value


@pytest.mark.parametrize("service",[READ_SERVICE,SEND_SERVICE])
def test_exact_owned_scopes_alias_and_reject_broad_omitted_duplicate(service):
    exact = " ".join(sorted(SCOPES[service]))
    assert set(scopes(exact,service)) == SCOPES[service]
    assert set(scopes(exact.replace("email","https://www.googleapis.com/auth/userinfo.email"),service)) == SCOPES[service]
    for value in (None,"",exact+" email",exact+" https://www.googleapis.com/auth/userinfo.email",exact+" https://www.googleapis.com/auth/calendar.readonly",exact.replace(".owned", ""),exact.replace("openid", ""),exact+"\n"):
        with pytest.raises(CalendarIntegrationError): scopes(value,service)


@pytest.mark.parametrize("time",[
    {"dateTime":"2027-03-28T02:30:00+01:00","timeZone":"Europe/Warsaw"},
    {"dateTime":"2027-03-28T02:30:00+02:00","timeZone":"Europe/Warsaw"},
    {"dateTime":"2027-10-31T02:30:00+03:00","timeZone":"Europe/Warsaw"},
    {"dateTime":"2027-10-31T02:30:00","timeZone":"Europe/Warsaw"},
    {"dateTime":"2027-10-31T02:30:00-00:00","timeZone":"UTC"},
    {"dateTime":"2027-10-31T02:30:60Z","timeZone":"UTC"},
    {"dateTime":"2027-10-31T02:30:00.1Z","timeZone":"UTC"},
    {"date":"2027-10-31"},
])
def test_gaps_mismatched_offsets_naive_leaps_precision_all_day_reject(time):
    with pytest.raises(CalendarIntegrationError): instant(time)


def test_both_explicit_folds_are_distinct_instants():
    first = instant({"dateTime":"2027-10-31T02:30:00+02:00","timeZone":"Europe/Warsaw"})
    second = instant({"dateTime":"2027-10-31T02:30:00+01:00","timeZone":"Europe/Warsaw"})
    assert second-first == timedelta(hours=1)


@pytest.mark.parametrize("change",[
    {"newFutureField":"unsupported"}, {"eventLabelId":"new-label"}, {"recurrence":[]},
    {"attendees":[{"email":"guest@example.test"}]}, {"attendeesOmitted":True},
    {"attachments":[{}]}, {"conferenceData":{}}, {"hangoutLink":"https://example.test"},
    {"locked":True}, {"privateCopy":True}, {"eventType":"focusTime"},
    {"creator":{"email":ACCOUNT["email"],"self":False}}, {"summary":None},
])
def test_unsupported_or_malformed_source_blocks(change):
    value = source_event(); value.update(change)
    with pytest.raises(CalendarIntegrationError): event(value,calendar_id=CALENDAR["id"],event_id=value["id"],account=ACCOUNT)


def test_data_owner_not_inferred_from_owner_role_or_email_normalization():
    assert owned_calendar(CALENDAR,calendar_id=CALENDAR["id"],account=ACCOUNT)==CALENDAR
    for value in ({**CALENDAR,"primary":False}, {**CALENDAR,"primary":False,"dataOwner":"someone@example.test"}, {**CALENDAR,"accessRole":"writer"}, {**CALENDAR,"id":"OPERATOR@example.test"}):
        with pytest.raises(CalendarIntegrationError): owned_calendar(value,calendar_id=CALENDAR["id"],account=ACCOUNT)
    secondary = {**CALENDAR,"id":"secondary@group.calendar.google.com","primary":False,"dataOwner":ACCOUNT["email"]}
    assert owned_calendar(secondary,calendar_id=secondary["id"],account=ACCOUNT)==secondary


def test_property_merge_and_whole_protected_readback_required():
    frozen = frozen_event()
    assert len(frozen["marker_key"]) == 41
    assert set(frozen["resource"]) == {"start","end","extendedProperties"}
    assert len(frozen["resource"]["extendedProperties"]["private"]) == 1
    result = observed(frozen)
    assert reschedule_readback(result,frozen)["outcome"] == "verified_reschedule_observation"
    for change in (lambda value:value.update(etag=frozen["source_etag"]),lambda value:value.update(description="edited"),lambda value:value["extendedProperties"]["private"].pop(frozen["marker_key"]),lambda value:value["extendedProperties"].pop("shared")):
        changed = deepcopy(result); change(changed)
        with pytest.raises(CalendarIntegrationError): reschedule_readback(changed,frozen)


def test_bounds_do_not_truncate_and_json_duplicate_nonfinite_reject():
    value = source_event(); value["description"]="x"*16385
    with pytest.raises(CalendarIntegrationError): event(value,calendar_id=CALENDAR["id"],event_id=value["id"],account=ACCOUNT)
    value=source_event(); value["extendedProperties"]["private"]={f"k{i}":"value" for i in range(63)}
    with pytest.raises(CalendarIntegrationError): freeze(value,calendar=CALENDAR,account=ACCOUNT,new_start=timed(timedelta(days=2)),new_end=timed(timedelta(days=2,hours=1)))
    for raw in (b'{"id":1,"id":2}',b'{"x":NaN}',b'{"x":Infinity}',b'[]',b'{"x":'+b'['*14+b'0'+b']'*14+b'}'):
        with pytest.raises(CalendarIntegrationError): bounded_json(raw)


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict",[False,True])
async def test_fixed_conditional_wire_one_patch_and_no_calendar_metadata_endpoint(conflict):
    calls=[]; frozen=frozen_event()
    async def provider(request):
        calls.append(request)
        if request.url.host=="oauth2.googleapis.com": return httpx.Response(200,json={"access_token":"synthetic-write-token","scope":" ".join(sorted(SCOPES[SEND_SERVICE]))})
        if request.url.host=="openidconnect.googleapis.com": return httpx.Response(200,json={**ACCOUNT,"email_verified":True})
        if "/users/me/calendarList/" in request.url.path: return httpx.Response(200,json=CALENDAR)
        assert request.method=="PATCH"
        assert request.headers["if-match"]==frozen["source_etag"]
        assert dict(request.url.params)=={"sendUpdates":"none","conferenceDataVersion":"0","supportsAttachments":"false"}
        assert json.loads(request.content)==frozen["resource"]
        return httpx.Response(412 if conflict else 200,json={} if conflict else observed(frozen))
    reservations=[]
    async def reserve(operation,service): reservations.append((operation,service))
    adapter=CalendarRescheduleAdapter(service=SEND_SERVICE,credentials={"client_id":"synthetic","refresh_token":"synthetic"},deadline=datetime.now(timezone.utc)+timedelta(seconds=30),contact=reserve,transport=httpx.MockTransport(provider),resolver=lambda host,port:["93.184.216.34"])
    await adapter.request("refresh"); await adapter.request("identity")
    await adapter.request("calendar",calendar_id=CALENDAR["id"])
    kwargs={"calendar_id":CALENDAR["id"],"event_id":frozen["source"]["id"],"resource":frozen["resource"],"etag":frozen["source_etag"]}
    if conflict:
        with pytest.raises(ConditionalConflict): await adapter.request("patch",**kwargs)
    else: assert await adapter.request("patch",**kwargs)==observed(frozen)
    with pytest.raises(CalendarIntegrationError): await adapter.request("patch",**kwargs)
    assert len(calls)==4 and reservations==[(operation,SEND_SERVICE) for operation in ("refresh","identity","calendar","patch")]
    assert adapter.marker.snapshot()["status"]=="verified"
