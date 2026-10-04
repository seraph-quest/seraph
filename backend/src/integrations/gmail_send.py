"""Fixed reply identity/send routes; no legacy display authority or retries."""
from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
import hashlib
import json
import re
from urllib.parse import quote, urlencode

from src.integrations.gmail_read import GmailReadError, GMAIL_READONLY_SCOPE
from src.security.http_transport import request_pinned_https, _TransportLifecycleMarker

READ_SERVICE = "gmail_reply_read"
SEND_SERVICE = "gmail_reply_send"
SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
EMAIL_SCOPE = "https://www.googleapis.com/auth/userinfo.email"
SCOPES = {READ_SERVICE: frozenset({GMAIL_READONLY_SCOPE, "openid", "email"}),
    SEND_SERVICE: frozenset({SEND_SCOPE, "openid", "email"})}
MAX_RESPONSE = 256 * 1024
MAX_MIME = 16 * 1024
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
PROVIDER_ID = re.compile(r"^[A-Za-z0-9_-]{1,512}$")
RFC_ID = re.compile(r"^<[^<>\s@]{1,128}@[^<>\s@]{1,128}>$")


def fail(code):
    raise GmailReadError("mail_reply_" + code, "The exact Gmail reply binding is unavailable", recovery_action="inspect_original_reply")


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def provider_id(value):
    if type(value) is not str or PROVIDER_ID.fullmatch(value) is None: fail("provider_id_invalid")
    return value


def scopes(value, service):
    if service not in SCOPES or type(value) is not str or not value or len(value.encode()) > 2048 or CONTROL.search(value):
        fail("scope_unverified")
    tokens = value.split(" ")
    if any(not token for token in tokens): fail("scope_unverified")
    canonical = ["email" if token == EMAIL_SCOPE else token for token in tokens]
    if len(canonical) != len(set(canonical)) or frozenset(canonical) != SCOPES[service]: fail("scope_not_exact")
    return tuple(sorted(canonical))


def bounded_json(raw):
    if len(raw) > MAX_RESPONSE: fail("response_bound")
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value: fail("response_duplicate")
            value[key] = item
        return value
    def invalid(value): fail("response_invalid")
    try: value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, UnicodeError, RecursionError): fail("response_invalid")
    count = 0
    def visit(item, depth=0):
        nonlocal count
        count += 1
        if depth > 12 or count > 8192: fail("response_bound")
        if isinstance(item, dict):
            for key, child in item.items():
                if len(key.encode()) > 256: fail("response_bound")
                visit(child, depth+1)
        elif isinstance(item, list):
            for child in item: visit(child, depth+1)
    visit(value)
    if not isinstance(value, dict): fail("response_invalid")
    return value


def decode_raw(value, maximum=MAX_MIME):
    if type(value) is not str or len(value) > (maximum*4//3+8) or not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", value): fail("raw_invalid")
    try: raw = base64.b64decode(value.rstrip("=")+"="*(-len(value.rstrip("="))%4), altchars=b"-_", validate=True)
    except ValueError: fail("raw_invalid")
    if not raw or len(raw) > maximum: fail("raw_bound")
    if base64.urlsafe_b64encode(raw).decode().rstrip("=") != value.rstrip("="): fail("raw_invalid")
    return raw


def header(message, name, *, required=True, maximum=2048):
    values = message.get_all(name, [])
    if len(values) != (1 if required else min(len(values), 1)): fail("header_duplicate_or_missing")
    if not values: return ""
    value = str(values[0])
    if CONTROL.search(value) or len(value.encode()) > maximum: fail("header_invalid")
    return value


def address(message, name):
    header(message, name)
    value = message[name]
    if not hasattr(value, "addresses") or len(value.addresses) != 1: fail("single_address_required")
    result = value.addresses[0].addr_spec
    if len(result.encode()) > 254 or not re.fullmatch(r"[^\s<>@,;]+@[^\s<>@,;]+", result): fail("address_invalid")
    return result


def references(value):
    items = value.split() if value else []
    if len(items) > 20 or len(value.encode()) > 2048 or any(RFC_ID.fullmatch(item) is None for item in items): fail("references_invalid")
    return items


def parse_mime(raw, *, outgoing=False):
    if len(raw) > (MAX_MIME if outgoing else MAX_RESPONSE): fail("mime_bound")
    try: message = BytesParser(policy=policy.default.clone(raise_on_defect=True)).parsebytes(raw)
    except Exception: fail("mime_invalid")
    if message.defects or any(getattr(value, "defects", ()) for _, value in message.items()): fail("mime_invalid")
    for name in ("From", "To", "Subject", "Message-ID", "Reply-To", "References", "In-Reply-To", "Content-Type", "Content-Transfer-Encoding", "MIME-Version"):
        if len(message.get_all(name, [])) > 1: fail("header_duplicate_or_missing")
    for name in ("Cc", "Bcc"):
        if outgoing and message.get_all(name): fail("extra_recipient")
    rfc_id = header(message, "Message-ID", maximum=260)
    if RFC_ID.fullmatch(rfc_id) is None: fail("message_id_invalid")
    result = {"from": address(message, "From"), "subject": header(message, "Subject", maximum=200),
        "message_id": rfc_id, "references": references(header(message, "References", required=False))}
    if outgoing:
        if message.is_multipart() or message.get_content_type() != "text/plain" or message.get_content_charset() != "utf-8": fail("mime_encoding_invalid")
        if header(message, "Content-Transfer-Encoding").lower() != "base64" or header(message, "MIME-Version") != "1.0": fail("mime_encoding_invalid")
        encoded = message.get_payload()
        if type(encoded) is not str: fail("mime_encoding_invalid")
        try: body = base64.b64decode(re.sub(r"\r?\n", "", encoded), validate=True).decode("utf-8")
        except (ValueError, UnicodeError): fail("mime_encoding_invalid")
        result.update(to=address(message, "To"), in_reply_to=header(message, "In-Reply-To", maximum=260), body=plain_body(body))
        if body != result["body"]: fail("body_not_canonical")
    else:
        result["reply_to"] = address(message, "Reply-To") if message.get_all("Reply-To") else result["from"]
    return result


def plain_body(value):
    if type(value) is not str: fail("body_invalid")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    if not value or len(value) > 4000 or len(value.encode()) > 8192 or re.search(r"[\x00-\x08\x0b-\x1f\x7f]", value): fail("body_bound")
    return value


def source(message, thread):
    message_id, thread_id = provider_id(message.get("id")), provider_id(message.get("threadId"))
    if provider_id(thread.get("id")) != thread_id: fail("source_thread_changed")
    messages = thread.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 10: fail("thread_bound")
    if any(not isinstance(item, dict) or provider_id(item.get("threadId")) != thread_id for item in messages): fail("source_thread_changed")
    if sum(item.get("id") == message_id for item in messages) != 1: fail("source_message_unconfirmed")
    normalized = parse_mime(decode_raw(message.get("raw"), MAX_RESPONSE))
    return {**normalized, "provider_message_id": message_id, "thread_id": thread_id,
        "source_digest": digest([message, thread])}


def freeze_mime(source, *, sender, body, message_id):
    body = plain_body(body)
    if RFC_ID.fullmatch(message_id) is None: fail("message_id_invalid")
    refs = source["references"] + [source["message_id"]]
    references(" ".join(refs))
    message = EmailMessage(policy=policy.SMTP)
    for key, value in [("From", sender), ("To", source["reply_to"]), ("Subject", source["subject"]),
        ("Message-ID", message_id), ("In-Reply-To", source["message_id"]), ("References", " ".join(refs)),
        ("MIME-Version", "1.0"), ("Content-Type", 'text/plain; charset="utf-8"'), ("Content-Transfer-Encoding", "base64")]:
        if type(value) is not str or CONTROL.search(value): fail("header_invalid")
        message[key] = value
    message.set_payload(base64.encodebytes(body.encode()).decode().replace("\n", "\r\n"))
    raw = message.as_bytes()
    expected = parse_mime(raw, outgoing=True)
    if expected["from"] != sender or expected["to"] != source["reply_to"] or expected["body"] != body: fail("mime_changed")
    resource = {"raw": base64.urlsafe_b64encode(raw).decode().rstrip("="), "threadId": provider_id(source["thread_id"])}
    return {"resource": resource, "expected": expected, "mime_digest": digest(raw), "request_digest": digest(resource)}


def validate_resource(frozen):
    resource = frozen["resource"]
    if set(resource) != {"raw", "threadId"}: fail("send_resource_changed")
    provider_id(resource["threadId"])
    raw = decode_raw(resource["raw"])
    if digest(raw) != frozen["mime_digest"] or digest(resource) != frozen["request_digest"] or parse_mime(raw, outgoing=True) != frozen["expected"]: fail("send_resource_changed")
    return resource


def sent_readback(value, frozen, *, provider_message_id=None):
    validate_resource(frozen)
    provider_id(value.get("id"))
    if provider_message_id is not None and value["id"] != provider_message_id: fail("sent_id_changed")
    labels = value.get("labelIds")
    if not isinstance(labels, list) or len(labels) > 64 or "SENT" not in labels: fail("sent_label_unproven")
    if provider_id(value.get("threadId")) != frozen["resource"]["threadId"]: fail("sent_thread_changed")
    if parse_mime(decode_raw(value.get("raw")), outgoing=True) != frozen["expected"]: fail("sent_content_changed")
    return {"outcome": "verified_sent_observation", "response_digest": digest(value), "no_learning": True}


class GmailReplyAdapter:
    def __init__(self, *, service, credentials, deadline, contact, transport=None, resolver=None):
        if service not in SCOPES: fail("profile_invalid")
        self.service, self.credentials, self.deadline, self.contact = service, dict(credentials), deadline, contact
        self.transport, self.resolver = transport, resolver
        self.marker = _TransportLifecycleMarker()
        self.token = None
        self.identity = None
        self.mailbox_verified = False
        self.send_started = False

    async def request(self, operation, *, provider_id_value=None, resource=None, rfc_message_id=None):
        if operation == "refresh":
            if self.token is not None: fail("refresh_slot_consumed")
            url, method = "https://oauth2.googleapis.com/token", "POST"
            fields = {"grant_type": "refresh_token", **self.credentials}
            if set(fields) - {"grant_type", "client_id", "client_secret", "refresh_token"} or not {"client_id", "refresh_token"} <= set(fields): fail("credential_invalid")
            if any(type(value) is not str or not value or CONTROL.search(value) for value in fields.values()): fail("credential_invalid")
            payload = {"form_body": urlencode(fields).encode()}
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
        else:
            if self.token is None: fail("token_unverified")
            headers = {"Authorization": "Bearer " + self.token}
            method, payload = "GET", {}
            if operation == "identity": url = "https://openidconnect.googleapis.com/v1/userinfo"
            elif operation == "send" and self.service == SEND_SERVICE:
                if self.identity is None or self.send_started or type(resource) is not dict or set(resource) != {"raw", "threadId"}: fail("send_resource_changed")
                provider_id(resource["threadId"])
                parsed = parse_mime(decode_raw(resource["raw"]), outgoing=True)
                if parsed["from"] != self.identity["email"]: fail("sender_changed")
                self.send_started = True
                url, method = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send", "POST"
                payload = {"json_body": resource}
            elif self.service == READ_SERVICE:
                if self.identity is None or (operation != "profile" and not self.mailbox_verified): fail("mailbox_unverified")
                prefix = "https://gmail.googleapis.com/gmail/v1/users/me"
                if operation == "profile": url = prefix + "/profile"
                elif operation == "raw": url = prefix + "/messages/" + quote(provider_id(provider_id_value), safe="") + "?format=raw"
                elif operation == "thread": url = prefix + "/threads/" + quote(provider_id(provider_id_value), safe="") + "?format=metadata"
                elif operation == "search" and type(rfc_message_id) is str and RFC_ID.fullmatch(rfc_message_id):
                    url = prefix + "/messages?" + urlencode({"q": "in:sent rfc822msgid:" + rfc_message_id, "maxResults": 5})
                else: fail("route_invalid")
            else: fail("route_invalid")
        absolute = self.deadline.replace(tzinfo=self.deadline.tzinfo or timezone.utc).astimezone(timezone.utc)
        remaining = (absolute-datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0: fail("deadline_expired")
        kwargs = {"method": method, "headers": {"Accept": "application/json", **headers},
            "timeout_seconds": min(10, remaining), "max_bytes": MAX_RESPONSE, **payload,
            "_lifecycle_marker": self.marker, "authority_check": lambda: self.contact(operation, self.service)}
        if self.transport is not None: kwargs["transport"] = self.transport
        if self.resolver is not None: kwargs["resolver"] = self.resolver
        async with asyncio.timeout(remaining): response = await request_pinned_https(url, **kwargs)
        if response.status_code != 200 or response.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json": fail("provider_refused")
        value = bounded_json(response.content)
        if operation == "refresh":
            observed = scopes(value.get("scope"), self.service)
            token = value.get("access_token")
            if type(token) is not str or not token or len(token) > 8192 or CONTROL.search(token): fail("token_invalid")
            self.token = token
            self.scope_evidence = {"observed": list(observed), "digest": digest(value["scope"])}
        elif operation == "identity":
            sub, email = value.get("sub"), value.get("email")
            if type(sub) is not str or not sub or len(sub) > 255 or CONTROL.search(sub) or value.get("email_verified") is not True: fail("identity_unverified")
            if type(email) is not str or len(email) > 254 or not re.fullmatch(r"[^\s<>@,;]+@[^\s<>@,;]+", email): fail("identity_unverified")
            self.identity = {"sub": sub, "email": email, "issuer": "https://accounts.google.com", "scope": self.scope_evidence}
        return value

    async def authenticate(self):
        await self.request("refresh")
        await self.request("identity")
        if self.service == READ_SERVICE:
            profile = await self.request("profile")
            if profile.get("emailAddress") != self.identity["email"]: fail("mailbox_changed")
            self.mailbox_verified = True
        return self.identity
