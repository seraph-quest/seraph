"""Strict real HTTP transport boundary; no native admission substitution."""
import base64
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from src.integrations.gmail_send import (GmailReplyAdapter, READ_SERVICE, SEND_SERVICE,
    SCOPES, EMAIL_SCOPE, GmailReadError, freeze_mime, source, sent_readback, validate_resource)


def originals():
    raw = b"From: Origin <origin@example.test>\r\nTo: mailbox@example.test\r\nSubject: Exact original subject\r\nMessage-ID: <original@example.test>\r\nContent-Type: text/plain\r\n\r\nUntrusted source"
    message = {"id": "original-message", "threadId": "actual-thread", "raw": base64.urlsafe_b64encode(raw).decode().rstrip("=")}
    return message, {"id": "actual-thread", "messages": [{"id": "original-message", "threadId": "actual-thread"}]}


@pytest.mark.asyncio
@pytest.mark.parametrize("service", [READ_SERVICE, SEND_SERVICE])
@pytest.mark.parametrize("evidence", ["exact", "alias", "missing", "null", "empty", "extra", "duplicate", "both_email", "controls"])
async def test_every_refresh_requires_actual_exact_scope_before_identity(service, evidence):
    calls = []
    scope = " ".join(sorted(SCOPES[service]))
    if evidence == "alias": scope = scope.replace("email", EMAIL_SCOPE)
    elif evidence == "empty": scope = ""
    elif evidence == "extra": scope += " profile"
    elif evidence == "duplicate": scope += " email"
    elif evidence == "both_email": scope += " " + EMAIL_SCOPE
    elif evidence == "controls": scope += "\t"
    elif evidence == "null": scope = None
    async def provider(request):
        calls.append(str(request.url))
        if request.url.host == "oauth2.googleapis.com":
            response = {"access_token": "dummy_access_same_token"}
            if evidence != "missing": response["scope"] = scope
            return httpx.Response(200, json=response)
        assert request.headers["authorization"] == "Bearer dummy_access_same_token"
        if request.url.host == "openidconnect.googleapis.com":
            return httpx.Response(200, json={"sub": "CaseSensitiveSubject", "email": "mailbox@example.test", "email_verified": True})
        return httpx.Response(200, json={"emailAddress": "mailbox@example.test"})
    async def contact(operation, profile): pass
    adapter = GmailReplyAdapter(service=service, credentials={"client_id": "dummy_client", "refresh_token": "dummy_refresh"},
        deadline=datetime.now(timezone.utc)+timedelta(seconds=10), contact=contact,
        transport=httpx.MockTransport(provider), resolver=lambda host, port: ["93.184.216.34"])
    if evidence in {"exact", "alias"}:
        result = await adapter.authenticate()
        assert result["sub"] == "CaseSensitiveSubject" and result["issuer"] == "https://accounts.google.com"
        assert len(calls) == (3 if service == READ_SERVICE else 2)
    else:
        with pytest.raises(GmailReadError): await adapter.authenticate()
        assert len(calls) == 1 and adapter.token is None


def test_exact_frozen_resource_and_independent_sent_raw():
    parent = source(*originals())
    body = "  Literal <script> never execute </script>\nExact trailing space \n"
    frozen = freeze_mime(parent, sender="mailbox@example.test", body=body, message_id="<once@seraph.invalid>")
    assert set(validate_resource(frozen)) == {"raw", "threadId"}
    observed = {"id": "sent-one", "threadId": "actual-thread", "raw": frozen["resource"]["raw"], "labelIds": ["SENT"]}
    assert sent_readback(observed, frozen, provider_message_id="sent-one")["outcome"] == "verified_sent_observation"
    assert frozen["expected"]["body"] == body and frozen["expected"]["subject"] == parent["subject"]
    for bad in [{**observed, "threadId": "other"}, {**observed, "labelIds": ["INBOX"]}, {**observed, "id": "other"}]:
        with pytest.raises(GmailReadError): sent_readback(bad, frozen, provider_message_id="sent-one")


@pytest.mark.parametrize("mode", ["missing_thread", "empty_thread", "wrong_thread", "duplicate_header"])
def test_real_provider_source_cannot_use_display_fallback(mode):
    message, thread = originals()
    if mode == "missing_thread": message.pop("threadId")
    elif mode == "empty_thread": message["threadId"] = ""
    elif mode == "wrong_thread": message["threadId"] = "other"
    else:
        raw = base64.urlsafe_b64decode(message["raw"]+"="*(-len(message["raw"])%4))
        message["raw"] = base64.urlsafe_b64encode(b"Message-ID: <duplicate@example.test>\r\n"+raw).decode()
    with pytest.raises(GmailReadError): source(message, thread)


@pytest.mark.asyncio
async def test_actual_send_json_one_post_and_thread_resource():
    frozen = freeze_mime(source(*originals()), sender="mailbox@example.test", body="Exact reply", message_id="<once@seraph.invalid>")
    contacts, posts = [], []
    async def provider(request):
        if request.url.host == "oauth2.googleapis.com": return httpx.Response(200, json={"access_token": "dummy_token", "scope": " ".join(sorted(SCOPES[SEND_SERVICE]))})
        if request.url.host == "openidconnect.googleapis.com": return httpx.Response(200, json={"sub": "subject", "email": "mailbox@example.test", "email_verified": True})
        assert request.url.path == "/gmail/v1/users/me/messages/send" and request.method == "POST"
        actual = json.loads(request.content)
        assert actual == frozen["resource"] and actual["threadId"] == "actual-thread"
        posts.append(actual)
        return httpx.Response(200, json={"id": "sent-one", "threadId": "actual-thread"})
    async def contact(operation, profile): contacts.append(operation)
    adapter = GmailReplyAdapter(service=SEND_SERVICE, credentials={"client_id": "dummy", "refresh_token": "dummy"},
        deadline=datetime.now(timezone.utc)+timedelta(seconds=10), contact=contact,
        transport=httpx.MockTransport(provider), resolver=lambda host, port: ["93.184.216.34"])
    await adapter.authenticate()
    with pytest.raises(GmailReadError): await adapter.request("send", resource={"raw": frozen["resource"]["raw"]})
    assert posts == []
    result = await adapter.request("send", resource=validate_resource(frozen))
    assert result["id"] == "sent-one" and len(posts) == 1
    with pytest.raises(GmailReadError): await adapter.request("send", resource=frozen["resource"])
    assert contacts == ["refresh", "identity", "send"] and len(posts) == 1
