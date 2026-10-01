"""Pure contracts and private artifact helpers for governed Mail replies.

The WorkBoard dispatcher owns the durable root and lease.  This module keeps
the reply-specific grammar, deterministic identities, bounded model payload,
and encrypted local draft artifact in one narrow seam so the dispatcher does
not grow another execution state machine.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence
import uuid

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from config.settings import settings
from src.vault import decrypt, encrypt
from src.workspace import canonical_workspace_root


CAPABILITY_ID = "work.mail-reply-draft.v1"
CAPABILITY_VERSION = "1"
MAX_RUNTIME_SECONDS = 120
MAX_SUBJECT_CHARS = 200
MAX_BODY_CHARS = 4000
MAX_CAVEATS = 5
MAX_CAVEAT_CHARS = 300
ARTIFACT_ROOT = "artifacts/mail/private/reply-drafts"


class ReplyDraftOutput(BaseModel):
    """Strict model output; arbitrary actions/tools cannot cross this seam."""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    schema_version: int = Field(ge=1, le=1)
    message_revision: str = Field(min_length=8, max_length=128)
    subject: str = Field(min_length=1, max_length=MAX_SUBJECT_CHARS)
    plainbody: str = Field(min_length=1, max_length=MAX_BODY_CHARS)
    caveats: list[str] = Field(default_factory=list, max_length=MAX_CAVEATS)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def reply_job_id(owner_principal_id: str, task_id: str, attempt_id: str) -> str:
    seed = f"seraph:mail-reply:{owner_principal_id}:{task_id}:{attempt_id}"
    return f"mail-reply-draft:{uuid.uuid5(uuid.NAMESPACE_URL, seed)}"


def input_payload(inputs: Mapping[str, Any]) -> dict[str, Any]:
    """Return the immutable, body-free durable input projection."""

    keys = (
        "schema_version",
        "connection_id",
        "expected_connection_revision",
        "message_binding_id",
        "expected_message_revision",
        "mail_consent_id",
        "expected_source_consent_revision",
        "expected_model_consent_revision",
        "goal_id",
        "expected_goal_revision",
        "reply_intent",
        "style",
    )
    return {key: inputs[key] for key in keys}


def input_digest(inputs: Mapping[str, Any]) -> str:
    return canonical_digest(input_payload(inputs))


def authority_payload(*, task: Any, inputs: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "capability_id": CAPABILITY_ID,
        "capability_version": CAPABILITY_VERSION,
        "principal": str(task.owner_principal_id),
        "owner_kind": "user",
        "service_id": None,
        "session_id": str(task.owner_session_id),
        "operator_session_id": str(task.owner_session_id),
        "connection_id": str(inputs["connection_id"]),
        "connection_revision": int(inputs["expected_connection_revision"]),
        "message_binding_id": str(inputs["message_binding_id"]),
        "message_revision": str(inputs["expected_message_revision"]),
        "consent_id": str(inputs["mail_consent_id"]),
        "source_consent_revision": int(inputs["expected_source_consent_revision"]),
        "model_consent_revision": int(inputs["expected_model_consent_revision"]),
        "goal_id": str(task.goal_id),
        "goal_revision": int(task.goal_revision),
        "finite_authority": True,
        "runtime_cap_seconds": MAX_RUNTIME_SECONDS,
        "budget_microusd": None,
    }


def model_payload(
    *,
    metadata: Mapping[str, Any],
    body: str,
    reply_intent: str,
    style: str,
    allowed_body_fields: Sequence[str],
) -> dict[str, Any]:
    """Build the ordered, explicitly consented model payload.

    Provider identity, sender/address, labels, and other metadata are never
    copied into this payload.  ``replyintent`` is the only operator supplied
    field and is bounded by the strict input model before reaching here.
    """

    allowed = set(allowed_body_fields)
    payload: dict[str, Any] = {}
    if "subject" in allowed:
        payload["subject"] = str(metadata.get("subject") or "")[:MAX_SUBJECT_CHARS]
    if "plainbody" in allowed:
        payload["plainbody"] = str(body)[:MAX_BODY_CHARS]
    if "replyintent" in allowed:
        payload["replyintent"] = reply_intent
    payload["style"] = style
    return payload


def parse_model_output(raw: Any, *, expected_message_revision: str) -> ReplyDraftOutput:
    if hasattr(raw, "choices"):
        try:
            raw = raw.choices[0].message.content
        except (AttributeError, IndexError, TypeError) as exc:
            raise ValueError("mail reply model output is unavailable") from exc
    elif hasattr(raw, "content"):
        raw = raw.content
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("mail reply model output is not JSON") from exc
    if not isinstance(raw, Mapping):
        raise ValueError("mail reply model output must be an object")
    try:
        parsed = ReplyDraftOutput.model_validate(dict(raw))
    except ValidationError as exc:
        raise ValueError("mail reply model output failed the strict schema") from exc
    if parsed.message_revision != expected_message_revision:
        raise ValueError("mail reply model output revision does not match the reviewed message")
    if any(len(item) > MAX_CAVEAT_CHARS for item in parsed.caveats):
        raise ValueError("mail reply model caveat is too long")
    return parsed


def artifact_path_for_job(job_id: str) -> str:
    return f"{ARTIFACT_ROOT}/{hashlib.sha256(str(job_id).encode('utf-8')).hexdigest()[:32]}.enc"


def _path(relative_path: str) -> Path:
    root = canonical_workspace_root(settings.workspace_dir)
    candidate = root / relative_path
    resolved_parent = candidate.parent.resolve(strict=False)
    resolved_parent.relative_to(root.resolve())
    return candidate


def prepare_private_draft(job_id: str, payload: Mapping[str, Any]) -> tuple[str, str, bytes]:
    """Prepare bounded encrypted bytes without publishing them.

    The caller must persist the returned path and ciphertext digest in the
    durable job checkpoint before calling :func:`publish_private_draft`.
    Keeping this split here prevents a process death between publication and
    the durable artifact receipt from becoming an untraceable private file.
    """
    plain = json.dumps(dict(payload), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    encrypted = encrypt(plain).encode("utf-8")
    if not encrypted or len(encrypted) > 96 * 1024:
        raise OSError("mail reply artifact exceeds bounded size")
    relative = artifact_path_for_job(job_id)
    return relative, hashlib.sha256(encrypted).hexdigest(), encrypted


def publish_private_draft(relative: str, encrypted: bytes) -> None:
    """Atomically publish bytes prepared by :func:`prepare_private_draft`."""

    target = _path(relative)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(encrypted)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        directory_fd = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def write_private_draft(job_id: str, payload: Mapping[str, Any]) -> tuple[str, str, bytes]:
    """Encrypt and atomically publish one private draft, returning its hash.

    This compatibility wrapper is used by small local callers.  The governed
    dispatcher uses the explicit prepare/checkpoint/publish sequence above.
    """

    relative, digest, encrypted = prepare_private_draft(job_id, payload)
    publish_private_draft(relative, encrypted)
    return relative, digest, encrypted


def read_private_draft(relative_path: str, expected_sha256: str) -> dict[str, Any]:
    target = _path(relative_path)
    descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if not os.path.isfile(target) or before.st_nlink != 1 or before.st_size > 96 * 1024:
            raise OSError("mail reply artifact is not a regular private file")
        encrypted = os.read(descriptor, 96 * 1024 + 1)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        len(encrypted) > 96 * 1024
        or before.st_size != after.st_size
        or hashlib.sha256(encrypted).hexdigest() != expected_sha256
    ):
        raise OSError("mail reply artifact digest mismatch")
    decoded = json.loads(decrypt(encrypted.decode("utf-8")))
    if not isinstance(decoded, dict):
        raise OSError("mail reply artifact payload is invalid")
    return decoded


__all__ = [
    "ARTIFACT_ROOT",
    "CAPABILITY_ID",
    "CAPABILITY_VERSION",
    "MAX_RUNTIME_SECONDS",
    "ReplyDraftOutput",
    "artifact_path_for_job",
    "authority_payload",
    "canonical_digest",
    "input_digest",
    "input_payload",
    "model_payload",
    "prepare_private_draft",
    "parse_model_output",
    "publish_private_draft",
    "read_private_draft",
    "reply_job_id",
    "write_private_draft",
]
