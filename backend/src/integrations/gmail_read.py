"""Bounded, read-only Gmail source transport for M7.

The adapter deliberately owns only the fixed Gmail REST reads needed by the
Mail source routes.  Provider identifiers and credentials stay inside this
module until the authenticated API has established the owner/consent fences.
It does not expose a generic URL or method escape hatch and has no write
operation.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import quote, urlencode

import httpx

from src.db.models import GoogleServiceConnection
from src.security.http_transport import (
    MAX_RESPONSE_BYTES,
    PinnedTransportError,
    _TransportLifecycleMarker,
    default_resolver,
    request_pinned_https,
)
from src.vault import vault_repository


GOOGLE_API_ORIGIN = "https://gmail.googleapis.com"
GOOGLE_TOKEN_ORIGIN = "https://oauth2.googleapis.com"
GMAIL_API_PREFIX = "/gmail/v1/users/me"
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_SERVICE = "gmail_readonly"
MAX_PROVIDER_RESPONSE_BYTES = 256 * 1024
MAX_LABELS = 200
MAX_MESSAGE_IDS = 10
MAX_CONCURRENT_METADATA = 2
MAX_SUBJECT_BYTES = 200
MAX_PREVIEW_BYTES = 240
MAX_BODY_BYTES = 8 * 1024
MAX_MIME_DEPTH = 8
MAX_MIME_PARTS = 32
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_PROVIDER_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,512}$")
_JSON_CONTENT_TYPE = re.compile(
    r"^application/json(?:\s*;\s*charset\s*=\s*[\"']?utf-8[\"']?)?\s*$",
    re.IGNORECASE,
)


class GmailReadError(RuntimeError):
    """Safe, operator-visible failure without provider payloads."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 409,
        recovery_action: str | None = None,
    ) -> None:
        self.code = str(code)[:128]
        self.status_code = int(status_code)
        self.recovery_action = recovery_action
        super().__init__(str(message)[:500])


@dataclass(frozen=True)
class GmailLabel:
    provider_id: str
    name: str
    label_type: str


@dataclass(frozen=True)
class GmailMessageIdPage:
    provider_ids: tuple[str, ...]
    next_page_token: str | None


@dataclass(frozen=True)
class GmailMessageMetadata:
    provider_message_id: str
    provider_thread_id: str
    subject: str
    preview: str
    received_at: datetime | None
    read_status: str
    history_id: str
    label_ids: tuple[str, ...]
    message_revision: str


@dataclass(frozen=True)
class GmailMessageBody:
    metadata: GmailMessageMetadata
    body: str
    truncated: bool


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def message_key(owner_principal_id: str, connection_id: str, provider_message_id: str) -> str:
    return "sha256:" + digest(
        {
            "namespace": "seraph.gmail.message.v1",
            "owner_principal_id": owner_principal_id,
            "connection_id": connection_id,
            "provider_message_id": provider_message_id,
        }
    )


def thread_key(owner_principal_id: str, connection_id: str, provider_thread_id: str) -> str:
    return "sha256:" + digest(
        {
            "namespace": "seraph.gmail.thread.v1",
            "owner_principal_id": owner_principal_id,
            "connection_id": connection_id,
            "provider_thread_id": provider_thread_id,
        }
    )


def _fixed_url(path: str, params: list[tuple[str, str]] | None = None) -> str:
    if not path.startswith(GMAIL_API_PREFIX + "/"):
        raise GmailReadError("mail_resource_invalid", "The Gmail resource is unavailable")
    return GOOGLE_API_ORIGIN + path + (("?" + urlencode(params, doseq=True)) if params else "")


def _fixed_token_url() -> str:
    return GOOGLE_TOKEN_ORIGIN + "/token"


def _bounded_text(value: Any, *, limit: int, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or _CONTROL.search(value):
        raise GmailReadError("mail_provider_schema_invalid", "The Gmail response is invalid", status_code=502)
    encoded = value.encode("utf-8")
    if len(encoded) > limit:
        return encoded[:limit].decode("utf-8", errors="ignore")
    return value


def _provider_id(value: Any) -> str:
    if not isinstance(value, str) or not _PROVIDER_ID.fullmatch(value):
        raise GmailReadError("mail_provider_schema_invalid", "The Gmail response is invalid", status_code=502)
    return value


def _validate_json_response(response: Any) -> dict[str, Any]:
    content_type = str(getattr(response, "headers", {}).get("content-type", ""))
    if not _JSON_CONTENT_TYPE.fullmatch(content_type.strip()):
        raise GmailReadError("mail_provider_schema_invalid", "The Gmail response is invalid", status_code=502)
    try:
        value = json.loads(response.content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise GmailReadError("mail_provider_schema_invalid", "The Gmail response is invalid", status_code=502) from exc
    if not isinstance(value, dict):
        raise GmailReadError("mail_provider_schema_invalid", "The Gmail response is invalid", status_code=502)
    return value


def _scrub_text(value: str, secrets: tuple[str, ...]) -> str:
    result = value
    for secret in secrets:
        if secret:
            result = result.replace(secret, "[redacted]")
    return result


def _header_map(payload: Mapping[str, Any]) -> dict[str, str]:
    raw_headers = payload.get("payload", {}).get("headers", [])
    if not isinstance(raw_headers, list):
        raise GmailReadError("mail_provider_schema_invalid", "The Gmail response is invalid", status_code=502)
    result: dict[str, str] = {}
    for item in raw_headers[:64]:
        if not isinstance(item, Mapping):
            raise GmailReadError("mail_provider_schema_invalid", "The Gmail response is invalid", status_code=502)
        name = item.get("name")
        value = item.get("value")
        if not isinstance(name, str) or not isinstance(value, str) or _CONTROL.search(value):
            continue
        key = name.strip().casefold()
        if key and key not in result:
            result[key] = value[:1024]
    return result


def _received_at(value: Any, headers: Mapping[str, str]) -> datetime | None:
    if isinstance(value, str) and value.isdigit():
        try:
            return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            pass
    raw_date = headers.get("date", "")
    if raw_date:
        try:
            parsed = parsedate_to_datetime(raw_date)
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except (TypeError, ValueError, OverflowError):
            return None
    return None


def _normalize_preview(value: str, limit: int) -> str:
    return _bounded_text(" ".join(value.split()), limit=limit, field="preview")


def _metadata_from_payload(
    payload: Mapping[str, Any],
    *,
    provider_message_id: str | None = None,
    secrets: tuple[str, ...] = (),
) -> GmailMessageMetadata:
    message_id = _provider_id(payload.get("id") if provider_message_id is None else provider_message_id)
    thread_id = _provider_id(payload.get("threadId") or message_id)
    labels = payload.get("labelIds", [])
    if not isinstance(labels, list) or len(labels) > 100:
        raise GmailReadError("mail_provider_schema_invalid", "The Gmail response is invalid", status_code=502)
    label_ids = tuple(sorted({_provider_id(item) for item in labels}))
    headers = _header_map(payload)
    subject = _normalize_preview(_scrub_text(headers.get("subject", ""), secrets), MAX_SUBJECT_BYTES)
    preview = _normalize_preview(
        _scrub_text(str(payload.get("snippet") or ""), secrets),
        MAX_PREVIEW_BYTES,
    )
    history_id = _bounded_text(str(payload.get("historyId") or ""), limit=128, field="historyId")
    received = _received_at(payload.get("internalDate"), headers)
    normalized = {
        "headers": {
            name: _normalize_preview(headers.get(name, ""), 1024)
            for name in ("subject", "from", "to", "date", "message-id")
        },
        "history_id": history_id,
        "label_ids": list(label_ids),
    }
    revision = "sha256:" + digest(normalized)
    return GmailMessageMetadata(
        provider_message_id=message_id,
        provider_thread_id=thread_id,
        subject=subject,
        preview=preview,
        received_at=received,
        read_status="read" if "UNREAD" not in label_ids else "unread",
        history_id=history_id,
        label_ids=label_ids,
        message_revision=revision,
    )


def _decode_body_data(value: Any) -> bytes:
    if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_PROVIDER_RESPONSE_BYTES:
        return b""
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError):
        return b""


def _strip_html(value: str) -> str:
    value = re.sub(r"(?is)<(script|style|iframe|object).*?</\1>", " ", value)
    value = re.sub(r"(?is)<[^>]+>", " ", value)
    return html.unescape(value)


def _find_body(part: Mapping[str, Any], *, depth: int, count: list[int]) -> tuple[str, bool, int]:
    if depth > MAX_MIME_DEPTH or count[0] >= MAX_MIME_PARTS:
        return "", True, count[0]
    count[0] += 1
    mime = str(part.get("mimeType") or "").casefold()
    body = part.get("body") if isinstance(part.get("body"), Mapping) else {}
    raw = _decode_body_data(body.get("data"))
    if raw and mime == "text/plain":
        text = raw.decode("utf-8", errors="replace")
        return text, len(raw) > MAX_BODY_BYTES, count[0]
    if raw and mime == "text/html":
        text = _strip_html(raw.decode("utf-8", errors="replace"))
        return text, len(raw) > MAX_BODY_BYTES, count[0]
    parts = part.get("parts")
    if isinstance(parts, list):
        html_candidate: tuple[str, bool] | None = None
        for child in parts[:MAX_MIME_PARTS]:
            if not isinstance(child, Mapping):
                continue
            text, truncated, _ = _find_body(child, depth=depth + 1, count=count)
            if not text:
                continue
            if str(child.get("mimeType") or "").casefold() == "text/plain":
                return text, truncated, count[0]
            if html_candidate is None:
                html_candidate = (text, truncated)
        if html_candidate is not None:
            return html_candidate[0], html_candidate[1], count[0]
    return "", False, count[0]


def extract_message_body(payload: Mapping[str, Any], *, secrets: tuple[str, ...] = ()) -> tuple[str, bool]:
    root = payload.get("payload")
    if not isinstance(root, Mapping):
        return "", False
    text, truncated, _ = _find_body(root, depth=0, count=[0])
    text = _scrub_text(text, secrets)
    text = "\n".join(line.rstrip() for line in text.splitlines()).strip()
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_BODY_BYTES:
        return encoded[:MAX_BODY_BYTES].decode("utf-8", errors="ignore"), True
    return text, truncated


class GoogleGmailReadonlyAdapter:
    """Fixed Gmail read adapter using the shared pinned transport."""

    def __init__(
        self,
        connection: GoogleServiceConnection,
        *,
        owner_principal_id: str,
        transport: Any = None,
        resolver: Any = None,
        authority_check: Callable[[], Awaitable[None]] | None = None,
        contact_observer: Callable[[], None] | None = None,
    ) -> None:
        self.connection = connection
        self.owner_principal_id = owner_principal_id
        self.transport = transport
        self.resolver = resolver
        self.authority_check = authority_check
        self.contact_observer = contact_observer
        self._access_token: str | None = None
        self._credential_values: tuple[str, ...] = ()
        self._observed_provider_scopes: tuple[str, ...] | None = None
        self._transport_lifecycle = _TransportLifecycleMarker()

    def transport_quiescence(self) -> dict[str, int | str]:
        return self._transport_lifecycle.snapshot()

    @property
    def active_operations(self) -> int:
        return int(self._transport_lifecycle.snapshot()["active_operations"])

    @property
    def unsettled_operations(self) -> int:
        return int(self._transport_lifecycle.snapshot()["unsettled_operations"])

    @property
    def observed_provider_scopes(self) -> tuple[str, ...] | None:
        """Exact readonly scope evidence returned by the token endpoint.

        ``None`` is intentionally distinct from an empty tuple: an omitted
        provider scope claim remains unverified and must never be projected as
        positive privilege evidence.
        """
        return self._observed_provider_scopes

    async def _check_authority(self) -> None:
        if self.authority_check is not None:
            await self.authority_check()

    def _mark_contact(self) -> None:
        if self.contact_observer is not None:
            self.contact_observer()

    async def _credentials(self) -> dict[str, str]:
        if self.connection.service != GMAIL_SERVICE or self.connection.state != "active":
            raise GmailReadError("mail_connection_unavailable", "The Gmail connection is not active", recovery_action="restore_prerequisite")
        declared = _load_string_list(getattr(self.connection, "declared_scopes_json", "[]"))
        if declared != [GMAIL_READONLY_SCOPE]:
            raise GmailReadError("mail_scope_invalid", "The Gmail connection does not declare the read-only scope", recovery_action="recreate_connection")
        await self._check_authority()
        raw = await vault_repository.get(self.connection.vault_secret_key)
        if not raw:
            raise GmailReadError("mail_credential_unavailable", "The Gmail credential is unavailable", recovery_action="restore_prerequisite")
        try:
            values = json.loads(raw)
        except (TypeError, ValueError, UnicodeDecodeError) as exc:
            raise GmailReadError("mail_credential_unavailable", "The Gmail credential is unavailable") from exc
        if not isinstance(values, dict):
            raise GmailReadError("mail_credential_unavailable", "The Gmail credential is unavailable")
        required = {key: values.get(key) for key in ("client_id", "refresh_token")}
        if any(not isinstance(value, str) or not value or _CONTROL.search(value) for value in required.values()):
            raise GmailReadError("mail_credential_unavailable", "The Gmail credential is unavailable")
        client_secret = values.get("client_secret")
        if client_secret is not None and (not isinstance(client_secret, str) or _CONTROL.search(client_secret)):
            raise GmailReadError("mail_credential_unavailable", "The Gmail credential is unavailable")
        result = {key: value for key, value in (*required.items(), ("client_secret", client_secret)) if isinstance(value, str) and value}
        self._credential_values = tuple(result.values())
        return result

    def _scrub(self, value: Any) -> Any:
        if isinstance(value, str):
            return _scrub_text(value, self._credential_values)
        if isinstance(value, list):
            return [self._scrub(item) for item in value[:MAX_LABELS]]
        if isinstance(value, dict):
            return {str(key): self._scrub(item) for key, item in value.items()}
        return value

    async def _token(self) -> str:
        if self._access_token:
            return self._access_token
        credentials = await self._credentials()
        form = [
            ("grant_type", "refresh_token"),
            ("client_id", credentials["client_id"]),
            ("refresh_token", credentials["refresh_token"]),
        ]
        if credentials.get("client_secret"):
            form.append(("client_secret", credentials["client_secret"]))
        await self._check_authority()
        self._mark_contact()
        try:
            response = await request_pinned_https(
                _fixed_token_url(),
                method="POST",
                headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
                form_body=urlencode(form).encode("utf-8"),
                resolver=self.resolver or default_resolver,
                transport=self.transport,
                timeout_seconds=10,
                max_bytes=MAX_PROVIDER_RESPONSE_BYTES,
                _lifecycle_marker=self._transport_lifecycle,
            )
        except (PinnedTransportError, TimeoutError, OSError, RuntimeError, httpx.HTTPError) as exc:
            raise GmailReadError("mail_provider_unavailable", "Gmail authorization is unavailable", status_code=503, recovery_action="retry") from exc
        if response.status_code != 200:
            raise GmailReadError("mail_token_refresh_failed", "Gmail authorization was refused", status_code=502, recovery_action="restore_prerequisite")
        body = _validate_json_response(response)
        token = body.get("access_token")
        if not isinstance(token, str) or not token or _CONTROL.search(token):
            raise GmailReadError("mail_token_refresh_failed", "Gmail authorization response is invalid", status_code=502)
        provider_scope = body.get("scope")
        if provider_scope is not None:
            if not isinstance(provider_scope, str):
                raise GmailReadError("mail_scope_unverified", "Gmail authorization scope evidence is invalid", status_code=502)
            scopes = sorted(set(provider_scope.split()))
            if any(scope != GMAIL_READONLY_SCOPE for scope in scopes):
                raise GmailReadError("mail_scope_broader_than_declared", "The Gmail credential has broader provider privileges", status_code=403, recovery_action="recreate_connection")
            if scopes != [GMAIL_READONLY_SCOPE]:
                raise GmailReadError("mail_scope_unverified", "Gmail authorization scope evidence is invalid", status_code=502)
            self._observed_provider_scopes = tuple(scopes)
        else:
            self._observed_provider_scopes = None
        self._access_token = token
        self._credential_values = tuple((*self._credential_values, token))
        return token

    async def _authorized_get(self, url: str) -> dict[str, Any]:
        if not url.startswith(GOOGLE_API_ORIGIN + GMAIL_API_PREFIX + "/"):
            raise GmailReadError("mail_resource_invalid", "The Gmail resource is unavailable")
        token = await self._token()
        await self._check_authority()
        self._mark_contact()
        try:
            response = await request_pinned_https(
                url,
                method="GET",
                headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
                resolver=self.resolver or default_resolver,
                transport=self.transport,
                timeout_seconds=10,
                max_bytes=MAX_PROVIDER_RESPONSE_BYTES,
                _lifecycle_marker=self._transport_lifecycle,
            )
        except (PinnedTransportError, TimeoutError, OSError, RuntimeError, httpx.HTTPError) as exc:
            raise GmailReadError("mail_provider_unavailable", "Gmail provider read is unavailable", status_code=503, recovery_action="retry") from exc
        if response.status_code in {401, 403}:
            raise GmailReadError("mail_provider_unauthorized", "Gmail authorization was refused", status_code=403, recovery_action="restore_prerequisite")
        if response.status_code != 200:
            raise GmailReadError("mail_provider_read_failed", "Gmail provider read failed", status_code=502, recovery_action="retry")
        return self._scrub(_validate_json_response(response))

    async def list_labels(self) -> tuple[GmailLabel, ...]:
        payload = await self._authorized_get(_fixed_url(GMAIL_API_PREFIX + "/labels"))
        raw = payload.get("labels")
        if not isinstance(raw, list) or len(raw) > MAX_LABELS:
            raise GmailReadError("mail_provider_schema_invalid", "The Gmail label response is invalid", status_code=502)
        labels: list[GmailLabel] = []
        for item in raw:
            if not isinstance(item, Mapping):
                raise GmailReadError("mail_provider_schema_invalid", "The Gmail label response is invalid", status_code=502)
            labels.append(
                GmailLabel(
                    provider_id=_provider_id(item.get("id")),
                    name=_bounded_text(item.get("name"), limit=200, field="label name"),
                    label_type=_bounded_text(item.get("type") or "user", limit=32, field="label type"),
                )
            )
        return tuple(labels)

    async def list_message_ids(
        self,
        provider_label_ids: list[str],
        *,
        received_after: datetime,
        max_messages: int,
    ) -> GmailMessageIdPage:
        if not 1 <= len(provider_label_ids) <= 3:
            raise GmailReadError("mail_request_invalid", "The selected Gmail labels are invalid", status_code=422)
        if not 1 <= max_messages <= MAX_MESSAGE_IDS:
            raise GmailReadError("mail_request_invalid", "The Gmail message limit is invalid", status_code=422)
        labels = [_provider_id(value) for value in provider_label_ids]
        after = received_after.astimezone(timezone.utc).strftime("%Y/%m/%d")
        params: list[tuple[str, str]] = [("labelIds", value) for value in labels]
        params.extend(
            [
                ("q", f"after:{after} -label:spam -label:trash"),
                ("maxResults", str(max_messages)),
            ]
        )
        payload = await self._authorized_get(_fixed_url(GMAIL_API_PREFIX + "/messages", params))
        raw = payload.get("messages", [])
        if (
            not isinstance(raw, list)
            or len(raw) > MAX_MESSAGE_IDS
            or len(raw) > max_messages
        ):
            raise GmailReadError("mail_provider_schema_invalid", "The Gmail message list response is invalid", status_code=502)
        ids: list[str] = []
        for item in raw[:MAX_MESSAGE_IDS]:
            if not isinstance(item, Mapping):
                raise GmailReadError("mail_provider_schema_invalid", "The Gmail message list response is invalid", status_code=502)
            ids.append(_provider_id(item.get("id")))
        token = payload.get("nextPageToken")
        if token is not None and (not isinstance(token, str) or len(token.encode("utf-8")) > 2048 or _CONTROL.search(token)):
            raise GmailReadError("mail_provider_schema_invalid", "The Gmail message list response is invalid", status_code=502)
        return GmailMessageIdPage(tuple(ids), token or None)

    async def get_message_metadata(self, provider_message_id: str) -> GmailMessageMetadata:
        provider_message_id = _provider_id(provider_message_id)
        params = [
            ("format", "metadata"),
            ("metadataHeaders", "Subject"),
            ("metadataHeaders", "From"),
            ("metadataHeaders", "To"),
            ("metadataHeaders", "Date"),
            ("metadataHeaders", "Message-ID"),
        ]
        payload = await self._authorized_get(
            _fixed_url(f"{GMAIL_API_PREFIX}/messages/{quote(provider_message_id, safe='')}", params)
        )
        return _metadata_from_payload(payload, provider_message_id=provider_message_id, secrets=self._credential_values)

    async def get_message_full(self, provider_message_id: str) -> GmailMessageBody:
        provider_message_id = _provider_id(provider_message_id)
        payload = await self._authorized_get(
            _fixed_url(
                f"{GMAIL_API_PREFIX}/messages/{quote(provider_message_id, safe='')}",
                [("format", "full")],
            )
        )
        metadata = _metadata_from_payload(payload, provider_message_id=provider_message_id, secrets=self._credential_values)
        body, truncated = extract_message_body(payload, secrets=self._credential_values)
        return GmailMessageBody(metadata=metadata, body=body, truncated=truncated)


def _load_string_list(value: Any) -> list[str]:
    try:
        decoded = json.loads(value or "[]") if isinstance(value, str) else value
    except (TypeError, ValueError):
        return []
    if not isinstance(decoded, list):
        return []
    return sorted({item.strip() for item in decoded if isinstance(item, str) and item.strip()})


__all__ = [
    "GMAIL_API_PREFIX",
    "GMAIL_READONLY_SCOPE",
    "GMAIL_SERVICE",
    "GmailLabel",
    "GmailMessageBody",
    "GmailMessageIdPage",
    "GmailMessageMetadata",
    "GmailReadError",
    "GoogleGmailReadonlyAdapter",
    "digest",
    "extract_message_body",
    "message_key",
    "thread_key",
]
