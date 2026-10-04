"""Closed Calendar reschedule wire contract; no model or provider inference."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import secrets
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.security.http_transport import request_pinned_https, _TransportLifecycleMarker
from src.integrations.google_calendar import CalendarIntegrationError

READ_SERVICE = "calendar_reschedule_read"
SEND_SERVICE = "calendar_reschedule_write"
LIST_SCOPE = "https://www.googleapis.com/auth/calendar.calendarlist.readonly"
SCOPES = {
    READ_SERVICE: frozenset({"https://www.googleapis.com/auth/calendar.events.owned.readonly", LIST_SCOPE, "openid", "email"}),
    SEND_SERVICE: frozenset({"https://www.googleapis.com/auth/calendar.events.owned", LIST_SCOPE, "openid", "email"}),
}
MAX_RESPONSE = 64 * 1024
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})$")
EVENT_FIELDS = frozenset({"kind", "etag", "id", "status", "htmlLink", "created", "updated", "summary", "description", "location", "colorId", "creator", "organizer", "start", "end", "endTimeUnspecified", "recurrence", "recurringEventId", "originalStartTime", "transparency", "visibility", "iCalUID", "sequence", "attendees", "attendeesOmitted", "extendedProperties", "hangoutLink", "conferenceData", "gadget", "anyoneCanAddSelf", "guestsCanInviteOthers", "guestsCanModify", "guestsCanSeeOtherGuests", "privateCopy", "locked", "reminders", "source", "attachments", "eventType", "birthdayProperties", "focusTimeProperties", "outOfOfficeProperties", "workingLocationProperties"})
MUTABLE_SERVER_FIELDS = frozenset({"etag", "updated", "sequence", "htmlLink", "start", "end"})


def fail(code):
    raise CalendarIntegrationError("calendar_reschedule_" + code,
        "The exact Calendar reschedule binding is unavailable", recovery_action="inspect_original_reschedule")


class ConditionalConflict(CalendarIntegrationError):
    def __init__(self):
        super().__init__("calendar_reschedule_precondition_conflict", "Google refused the stale event version; create a fresh preview", recovery_action="refresh_event")


class ProviderRefusal(CalendarIntegrationError):
    """A received definite non-success; never used for transport uncertainty."""
    def __init__(self, status):
        self.provider_status = status
        super().__init__("calendar_reschedule_provider_refused", "Google refused this request", recovery_action="inspect_original_reschedule")


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def text(value, maximum, *, multiline=False):
    if type(value) is not str or not value or len(value.encode()) > maximum:
        fail("field_bound")
    controls = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]") if multiline else CONTROL
    if controls.search(value): fail("field_invalid")
    return value


def provider_id(value, maximum=512):
    text(value, maximum)
    if value in {"primary", ".", ".."} or any(c in value for c in "/?#\\"):
        fail("provider_id_invalid")
    return value


def scopes(value, service):
    text(value, 2048)
    if service not in SCOPES: fail("profile_invalid")
    tokens = value.split(" ")
    canonical = ["email" if token == "https://www.googleapis.com/auth/userinfo.email" else token for token in tokens]
    if any(not token for token in canonical) or len(canonical) != len(set(canonical)) or frozenset(canonical) != SCOPES[service]:
        fail("scope_not_exact")
    return tuple(sorted(canonical))


def bounded_json(raw):
    if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE: fail("response_bound")
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result: fail("response_duplicate")
            result[key] = item
        return result
    def invalid(_): fail("response_invalid")
    try: result = json.loads(raw.decode(), object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, UnicodeError, RecursionError): fail("response_invalid")
    count = 0
    def visit(value, depth=0):
        nonlocal count
        count += 1
        if depth > 12 or count > 4096: fail("response_bound")
        if isinstance(value, dict):
            for key, child in value.items():
                if len(key.encode()) > 256: fail("response_bound")
                visit(child, depth+1)
        elif isinstance(value, list):
            for child in value: visit(child, depth+1)
    visit(result)
    if type(result) is not dict: fail("response_invalid")
    return result


def instant(value):
    if type(value) is not dict or set(value) != {"dateTime", "timeZone"}: fail("timed_event_required")
    raw, zone = text(value["dateTime"], 128), text(value["timeZone"], 128)
    if RFC3339.fullmatch(raw) is None or raw.endswith("-00:00"): fail("time_offset_invalid")
    try:
        local = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        tz = ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError): fail("timezone_invalid")
    observed = local.astimezone(timezone.utc).astimezone(tz)
    if observed.replace(tzinfo=None) != local.replace(tzinfo=None) or observed.utcoffset() != local.utcoffset():
        fail("timezone_offset_or_gap")
    return local.astimezone(timezone.utc)


def proposed_times(start, end, *, current=None):
    current = current or datetime.now(timezone.utc)
    first, last = instant(start), instant(end)
    if start["timeZone"] != end["timeZone"] or not timedelta(minutes=1) <= last-first <= timedelta(hours=24): fail("duration_invalid")
    if not current < first <= current+timedelta(days=365): fail("start_outside_window")
    return {"start_utc": first.isoformat(), "end_utc": last.isoformat()}


def owned_calendar(value, *, calendar_id, account):
    if type(value) is not dict or value.get("id") != provider_id(calendar_id, 1024) or value.get("accessRole") != "owner" or value.get("deleted", False) is not False:
        fail("calendar_owner_unproven")
    primary = value.get("primary", False)
    if type(primary) is not bool or not ((primary and calendar_id == account["email"]) or (not primary and value.get("dataOwner") == account["email"])):
        fail("calendar_data_owner_unproven")
    zone = text(value.get("timeZone"), 128)
    try: ZoneInfo(zone)
    except ZoneInfoNotFoundError: fail("timezone_invalid")
    return {"id": calendar_id, "primary": primary, "dataOwner": value.get("dataOwner"), "accessRole": "owner", "timeZone": zone}


def property_maps(value):
    if type(value) is not dict or set(value)-{"private", "shared"}: fail("properties_invalid")
    total, size = 0, 0
    for label, values in value.items():
        if type(values) is not dict: fail("properties_invalid")
        for key, item in values.items():
            text(key, 44); text(item, 1024, multiline=True)
            total += 1; size += len(key.encode()) + len(item.encode())
    if total > 64 or size > 8192: fail("properties_bound")
    return total, size


def event(value, *, calendar_id, event_id, account):
    # A closed resource schema prevents PATCH omissions from destroying fields
    # introduced by the provider after this version was reviewed.
    if type(value) is not dict or set(value)-EVENT_FIELDS: fail("event_field_unsupported")
    if value.get("id") != provider_id(event_id) or value.get("status") != "confirmed" or value.get("eventType", "default") != "default": fail("event_identity_invalid")
    text(value.get("etag"), 512); text(value.get("iCalUID"), 1024)
    if value.get("kind", "calendar#event") != "calendar#event": fail("event_identity_invalid")
    for field in ("recurrence", "recurringEventId", "originalStartTime", "conferenceData", "hangoutLink", "gadget", "birthdayProperties", "focusTimeProperties", "outOfOfficeProperties", "workingLocationProperties"):
        if field in value: fail("event_kind_unsupported")
    for field in ("endTimeUnspecified", "attendeesOmitted", "privateCopy", "locked"):
        if field in value and value[field] is not False: fail("event_kind_unsupported")
    for field in ("attendees", "attachments"):
        if field in value and value[field] != []: fail("event_kind_unsupported")
    for field in ("anyoneCanAddSelf", "guestsCanInviteOthers", "guestsCanModify", "guestsCanSeeOtherGuests"):
        if field in value and type(value[field]) is not bool: fail("field_invalid")
    for field, maximum in (("summary",1024), ("description",16384), ("location",2048), ("created",128), ("updated",128), ("htmlLink",2048), ("colorId",128), ("transparency",128), ("visibility",128)):
        if field in value and value[field] != "": text(value[field], maximum, multiline=field in {"summary","description","location"})
        elif field in value and type(value[field]) is not str: fail("field_invalid")
    for field, expected in (("creator",account["email"]), ("organizer",calendar_id)):
        identity = value.get(field)
        if type(identity) is not dict or set(identity)-{"id","email","displayName","self"} or identity.get("self") is not True or identity.get("email") != expected:
            fail("event_owner_unproven")
        for key in {"id","email","displayName"} & set(identity): text(identity[key], 1024, multiline=key=="displayName")
    start, end = instant(value.get("start")), instant(value.get("end"))
    if start >= end or value["start"]["timeZone"] != value["end"]["timeZone"]: fail("source_time_invalid")
    if "sequence" in value and (type(value["sequence"]) is not int or value["sequence"] < 0): fail("field_invalid")
    if "reminders" in value:
        reminders = value["reminders"]
        if type(reminders) is not dict or set(reminders)-{"useDefault","overrides"} or type(reminders.get("useDefault")) is not bool: fail("reminders_invalid")
        overrides = reminders.get("overrides", [])
        if type(overrides) is not list or len(overrides)>5: fail("reminders_invalid")
        for item in overrides:
            if type(item) is not dict or set(item)!={"method","minutes"} or item["method"] not in {"email","popup"} or type(item["minutes"]) is not int or not 0<=item["minutes"]<=40320: fail("reminders_invalid")
    if "source" in value:
        source = value["source"]
        if type(source) is not dict or set(source)-{"url","title"}: fail("field_invalid")
        for key, child in source.items(): text(child, 2048, multiline=key=="title")
    property_maps(value.get("extendedProperties", {}))
    return value


def protected(value):
    return {key: child for key, child in value.items() if key not in MUTABLE_SERVER_FIELDS}


def freeze(value, *, calendar, account, new_start, new_end):
    event(value, calendar_id=calendar["id"], event_id=value.get("id"), account=account)
    utc_times = proposed_times(new_start, new_end)
    if instant(value["start"]) == instant(new_start) and instant(value["end"]) == instant(new_end): fail("no_time_change")
    key, marker = "seraphReschedule_"+secrets.token_hex(12), secrets.token_hex(32)
    count, size = property_maps(value.get("extendedProperties", {}))
    if count>=64 or size+len(key)+len(marker)>8192 or key in value.get("extendedProperties",{}).get("private",{}): fail("marker_capacity")
    resource = {"start": new_start, "end": new_end, "extendedProperties": {"private": {key: marker}}}
    return {"resource": resource, "request_digest": digest(resource), "source_digest": digest(value), "source_etag": value["etag"], "source": value, "calendar": calendar, "account": account, "marker_key": key, "marker_value": marker, "utc": utc_times}


def validate_resource(frozen):
    resource = frozen["resource"]
    if set(resource)!={"start","end","extendedProperties"} or resource["extendedProperties"]!={"private":{frozen["marker_key"]:frozen["marker_value"]}} or digest(resource)!=frozen["request_digest"] or digest(frozen["source"])!=frozen["source_digest"] or frozen["source"]["etag"]!=frozen["source_etag"]:
        fail("patch_resource_changed")
    if re.fullmatch(r"seraphReschedule_[0-9a-f]{24}", frozen["marker_key"]) is None or re.fullmatch(r"[0-9a-f]{64}", frozen["marker_value"]) is None: fail("marker_invalid")
    return resource


def reschedule_readback(value, frozen):
    resource = validate_resource(frozen)
    original = frozen["source"]
    event(value, calendar_id=frozen["calendar"]["id"], event_id=original["id"], account=frozen["account"])
    if value["etag"] == original["etag"] or value["start"] != resource["start"] or value["end"] != resource["end"]: fail("readback_time_or_version_changed")
    expected = json.loads(json.dumps(protected(original)))
    expected.setdefault("extendedProperties",{}).setdefault("private",{})[frozen["marker_key"]] = frozen["marker_value"]
    if protected(value) != expected: fail("readback_protected_changed")
    return {"outcome":"verified_reschedule_observation", "response_digest":digest(value), "no_learning":True}


class CalendarRescheduleAdapter:
    """Fixed one-token profile, actual owned transport and sole conditional PATCH."""
    def __init__(self, *, service, credentials, deadline, contact, transport=None, resolver=None):
        if service not in SCOPES: fail("profile_invalid")
        self.service, self.credentials, self.deadline, self.contact = service, dict(credentials), deadline, contact
        self.transport, self.resolver = transport, resolver
        self.marker = _TransportLifecycleMarker()
        self.token = self.identity = None
        self.calendar_verified = None
        self.patch_started = False

    async def request(self, operation, *, calendar_id=None, event_id=None, resource=None, etag=None):
        method, payload, headers = "GET", {}, {}
        if operation == "refresh":
            if self.token is not None: fail("refresh_slot_consumed")
            fields = {"grant_type":"refresh_token", **self.credentials}
            if set(fields)-{"grant_type","client_id","client_secret","refresh_token"} or not {"client_id","refresh_token"}<=set(fields): fail("credential_invalid")
            for child in fields.values(): text(child, 8192)
            url, method = "https://oauth2.googleapis.com/token", "POST"
            payload, headers = {"form_body":urlencode(fields).encode()}, {"Content-Type":"application/x-www-form-urlencoded"}
        else:
            if self.token is None: fail("token_unverified")
            headers = {"Authorization":"Bearer "+self.token}
            if operation == "identity":
                if self.identity is not None: fail("identity_slot_consumed")
                url = "https://openidconnect.googleapis.com/v1/userinfo"
            else:
                if self.identity is None: fail("identity_unverified")
                calendar_id = provider_id(calendar_id, 1024)
                if operation == "calendar": url = "https://www.googleapis.com/calendar/v3/users/me/calendarList/"+quote(calendar_id,safe="")
                else:
                    if self.calendar_verified is None or self.calendar_verified["id"] != calendar_id: fail("calendar_owner_unproven")
                    url = "https://www.googleapis.com/calendar/v3/calendars/"+quote(calendar_id,safe="")+"/events/"+quote(provider_id(event_id),safe="")
                    if operation == "patch" and self.service == SEND_SERVICE:
                        if self.patch_started or type(resource) is not dict or set(resource)!={"start","end","extendedProperties"}: fail("patch_slot_consumed")
                        instant(resource["start"]); instant(resource["end"]); property_maps(resource["extendedProperties"])
                        self.patch_started = True
                        method, payload = "PATCH", {"json_body":resource}
                        headers.update({"If-Match":text(etag,512)})
                        url += "?"+urlencode({"sendUpdates":"none","conferenceDataVersion":0,"supportsAttachments":"false"})
                    elif operation != "event" or self.service != READ_SERVICE: fail("route_invalid")
        absolute = self.deadline.replace(tzinfo=self.deadline.tzinfo or timezone.utc).astimezone(timezone.utc)
        remaining = (absolute-datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0: fail("deadline_expired")
        kwargs = {"method":method, "headers":{"Accept":"application/json",**headers}, "timeout_seconds":min(10,remaining), "max_bytes":MAX_RESPONSE, **payload, "_lifecycle_marker":self.marker, "authority_check":lambda:self.contact(operation,self.service)}
        if self.transport is not None: kwargs["transport"]=self.transport
        if self.resolver is not None: kwargs["resolver"]=self.resolver
        async with asyncio.timeout(remaining): response = await request_pinned_https(url,**kwargs)
        if operation == "patch" and response.status_code == 412: raise ConditionalConflict()
        if response.status_code in {400,401,403,404,405,409,410,412,422,429}: raise ProviderRefusal(response.status_code)
        if response.status_code != 200 or response.headers.get("content-type", "").split(";",1)[0].lower() != "application/json": fail("provider_response_uncertain")
        value = bounded_json(response.content)
        if operation == "refresh":
            observed = scopes(value.get("scope"),self.service)
            self.token = text(value.get("access_token"),8192)
            self.scope_evidence = {"observed":list(observed), "digest":digest(value["scope"])}
        elif operation == "identity":
            sub, email = text(value.get("sub"),255), text(value.get("email"),254)
            if value.get("email_verified") is not True or re.fullmatch(r"[^\s<>@,;]+@[^\s<>@,;]+",email) is None: fail("identity_unverified")
            self.identity = {"sub":sub,"email":email,"issuer":"https://accounts.google.com","scope":self.scope_evidence}
        elif operation == "calendar":
            self.calendar_verified = owned_calendar(value,calendar_id=calendar_id,account=self.identity)
        return value
