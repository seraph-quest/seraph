"""Closed selected-text protocol. Pure validation; no I/O or execution grants."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Annotated, Literal
from urllib.parse import urlsplit
import uuid

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

CAPABILITY_ID = "work.context.selected_text.v1"
JOB_KIND = "selected_context_v1"
VERSION = "browser-selected-text-v1"
ADAPTER_BUILD_DIGEST = "1e2d5dac160e52ecd8fd97a29f263285fa12f0f76334e0043b1a5f3d471337b9"
COMPANION_ORIGIN = "chrome-extension://agjkohpodkhhflnioopocboanalpgajn"
PAIRED_PATHS = frozenset("/api/context/selected-text/paired/" + action for action in ("target", "prepare", "ticket", "upload"))
MAX_TEXT_BYTES = 32768
MAX_ENVELOPE_BYTES = 49152
MAX_RETAINED_BYTES = 2 * 1024 * 1024
MAX_RETAINED_CAPTURES = 64
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Identifier = Annotated[str, Field(min_length=1, max_length=256)]
UUID = Annotated[str, Field(pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")]


class SelectedContextError(Exception):
    def __init__(self, code: str, status: int = 409):
        self.code, self.status = code, status
        super().__init__(code)


def deny(code: str, status: int = 409):
    raise SelectedContextError(code, status)


class Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PairLocator(Closed):
    extension_id: Identifier
    reference: Identifier
    name: Identifier
    device_id: Identifier
    pairing_id: Identifier


class Target(Closed):
    schema_version: Literal[1]
    target_revision: int = Field(ge=1)
    owner_principal_id: Identifier
    original_root_session_id: Identifier
    task_id: Identifier
    task_revision: int = Field(ge=1)
    goal_id: Identifier
    goal_revision: int = Field(ge=1)
    pair_generation: int = Field(ge=0)
    pair_digest: Digest
    vault_binding_digest: Digest
    expires_at: int = Field(ge=1)


class Source(Closed):
    origin: str = Field(min_length=1, max_length=512)
    path: str = Field(min_length=1, max_length=1024)
    document_id: Identifier
    frame_id: Literal[0]
    source_revision_digest: Digest
    captured_at: int = Field(ge=1)
    reviewed_origin: StrictBool
    protected_surface_checked: StrictBool

    @field_validator("origin")
    @classmethod
    def ordinary_origin(cls, value):
        url = urlsplit(value)
        if url.scheme not in {"https", "http"} or not url.hostname or url.username or url.password or url.path or url.query or url.fragment:
            raise ValueError("ordinary source origin required")
        if url.scheme == "http" and url.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("insecure source origin unsupported")
        return value

    @field_validator("path")
    @classmethod
    def sanitized_path(cls, value):
        if not value.startswith("/") or any(c in value for c in "?#\x00\r\n"):
            raise ValueError("sanitized source path required")
        return value


class Metadata(Closed):
    schema_version: Literal[1]
    adapter_profile: Literal["browser-selected-text-v1"]
    adapter_version: Literal["1"]
    adapter_build_digest: Digest
    pair: PairLocator
    target: Target
    capture_uuid: UUID
    source: Source
    reviewed_utf8_sha256: Digest
    reviewed_byte_count: int = Field(ge=1, le=MAX_TEXT_BYTES)
    expires_at: int = Field(ge=1)
    request_uuid: UUID
    privacy_reviewed: StrictBool


class SignedQuery(Closed):
    pair: PairLocator
    request_uuid: UUID
    expires_at: int = Field(ge=1)


class TicketQuery(SignedQuery):
    capture_uuid: UUID


class Upload(Closed):
    metadata: Metadata
    text: str = Field(min_length=1, max_length=MAX_TEXT_BYTES)


class BindTarget(Closed):
    pair: PairLocator
    expected_state_revision: int = Field(ge=0)
    expected_task_revision: int = Field(ge=1)
    goal_id: Identifier
    goal_revision: int = Field(ge=1)
    acknowledge_local_selected_text: StrictBool


class Decision(Closed):
    decision: Literal["approved", "denied"]
    expected_digest: Digest


class Discard(Closed):
    expected_revision: int = Field(ge=0)
    request_uuid: UUID


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def content_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="strict")).hexdigest()


def job_id(owner: str, capture_uuid: str) -> str:
    return "selected-context:" + uuid.uuid5(uuid.NAMESPACE_URL, canonical(["seraph.selected-context.identity.v1", owner, capture_uuid])).hex


def _ordered(value):
    """Fixed closed schemas become recursively ordered field/value arrays."""
    if isinstance(value, dict):
        return [[key, _ordered(value[key])] for key in sorted(value)]
    if isinstance(value, list):
        return [_ordered(item) for item in value]
    return value


def signature(credential: str, action: str, body: dict) -> str:
    if action not in {"target", "prepare", "ticket", "upload"}:
        raise ValueError("unsupported selected-context signing domain")
    key = hmac.new(credential.encode(), b"seraph.selected-context.key.v1\0", hashlib.sha256).digest()
    message = ("seraph.selected-context." + action + ".v1\0").encode() + canonical(_ordered(body)).encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def verify_signature(credential: str, action: str, body: dict, supplied: str):
    if not isinstance(supplied, str) or len(supplied) != 64 or not hmac.compare_digest(signature(credential, action, body), supplied):
        deny("selected_context_signature_invalid", 403)


def validate_metadata(metadata: Metadata):
    current = int(time.time())
    if metadata.adapter_build_digest != ADAPTER_BUILD_DIGEST:
        deny("selected_context_adapter_build_unsupported", 422)
    if not metadata.privacy_reviewed or not metadata.source.reviewed_origin or not metadata.source.protected_surface_checked:
        deny("selected_context_privacy_review_required", 422)
    if not current < metadata.expires_at <= min(current + 120, metadata.target.expires_at):
        deny("selected_context_ticket_expired")
    if not current - 300 <= metadata.source.captured_at <= current + 5:
        deny("selected_context_capture_expired")


def validate_text(upload: Upload) -> bytes:
    try:
        raw = upload.text.encode("utf-8", errors="strict")
    except UnicodeError:
        deny("selected_context_utf8_invalid", 422)
    if any(ord(c) < 32 and c not in "\n\r\t" for c in upload.text):
        deny("selected_context_control_character", 422)
    if len(raw) != upload.metadata.reviewed_byte_count or len(raw) > MAX_TEXT_BYTES or hashlib.sha256(raw).hexdigest() != upload.metadata.reviewed_utf8_sha256:
        deny("selected_context_content_changed", 422)
    return raw


def parse_body(raw: bytes, schema):
    if not raw or len(raw) > MAX_ENVELOPE_BYTES:
        deny("selected_context_envelope_bound", 413)
    def unique(items):
        result = {}
        for key, value in items:
            if key in result:
                deny("selected_context_duplicate_field", 422)
            result[key] = value
        return result
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=unique,
            parse_constant=lambda _: deny("selected_context_nonfinite_json", 422))
        return schema.model_validate(value)
    except (ValueError, TypeError, UnicodeError):
        deny("selected_context_schema_invalid", 422)
