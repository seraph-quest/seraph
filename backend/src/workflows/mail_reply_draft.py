"""Pure contracts and private artifact helpers for governed Mail replies.

The WorkBoard dispatcher owns the durable root and lease.  This module keeps
the reply-specific grammar, deterministic identities, bounded model payload,
and encrypted local draft artifact in one narrow seam so the dispatcher does
not grow another execution state machine.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import PurePosixPath
import re
import stat
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
# Gmail source extraction is bounded by bytes.  Keep that input bound
# separate from the smaller model-output bound below so an approved source
# body is never silently shortened before inference.
MAX_SOURCE_BODY_BYTES = 8 * 1024
MAX_BODY_CHARS = 4000
MAX_CAVEATS = 5
MAX_CAVEAT_CHARS = 300
ARTIFACT_ROOT = "artifacts/mail/private/reply-drafts"
_PRIVATE_FILE_MODE = 0o600
_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_NAME = re.compile(r"^[0-9a-f]{32}\.enc$")


class ReplyDraftOutput(BaseModel):
    """Strict model output; server-owned provenance stays outside the model."""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    subject: str = Field(min_length=1, max_length=MAX_SUBJECT_CHARS)
    body: str = Field(min_length=1, max_length=MAX_BODY_CHARS)
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
        body_text = str(body)
        if len(body_text.encode("utf-8")) > MAX_SOURCE_BODY_BYTES:
            raise ValueError("mail source body exceeds the reviewed input bound")
        payload["plainbody"] = body_text
    if "replyintent" in allowed:
        payload["replyintent"] = reply_intent
    payload["style"] = style
    return payload


def parse_model_output(raw: Any) -> ReplyDraftOutput:
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
    if any(len(item) > MAX_CAVEAT_CHARS for item in parsed.caveats):
        raise ValueError("mail reply model caveat is too long")
    return parsed


def artifact_path_for_job(job_id: str) -> str:
    return f"{ARTIFACT_ROOT}/{hashlib.sha256(str(job_id).encode('utf-8')).hexdigest()[:32]}.enc"


def _private_relative_parts(relative_path: str) -> tuple[str, ...]:
    relative = PurePosixPath(str(relative_path))
    root_parts = PurePosixPath(ARTIFACT_ROOT).parts
    parts = relative.parts
    if (
        relative.is_absolute()
        or len(parts) != len(root_parts) + 1
        or parts[: len(root_parts)] != root_parts
        or not _PRIVATE_FILE_NAME.fullmatch(parts[-1])
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise OSError("mail reply artifact path is invalid")
    return parts


@contextmanager
def _open_private_parent(relative_path: str, *, create_parents: bool = False):
    """Open the private artifact parent through no-follow descriptors."""

    parts = _private_relative_parts(relative_path)
    root = canonical_workspace_root(settings.workspace_dir)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow | cloexec
    getuid = getattr(os, "getuid", None)
    if getuid is None:
        raise OSError("mail reply artifact owner checks are unavailable")
    owner_uid = int(getuid())
    try:
        root_fd = os.open(root, directory_flags)
    except OSError as exc:
        raise OSError("mail reply artifact workspace is unavailable") from exc
    parent_fd = root_fd
    try:
        root_stat = os.fstat(root_fd)
        if not stat.S_ISDIR(root_stat.st_mode) or root_stat.st_uid != owner_uid:
            raise OSError("mail reply artifact workspace ownership is unsafe")
        for index, part in enumerate(parts[:-1]):
            created = False
            next_fd: int | None = None
            try:
                next_fd = os.open(part, directory_flags, dir_fd=parent_fd)
            except FileNotFoundError:
                if not create_parents:
                    raise
                os.mkdir(part, _PRIVATE_DIRECTORY_MODE, dir_fd=parent_fd)
                created = True
                next_fd = os.open(part, directory_flags, dir_fd=parent_fd)
            try:
                directory_stat = os.fstat(next_fd)
                if not stat.S_ISDIR(directory_stat.st_mode) or directory_stat.st_uid != owner_uid:
                    raise OSError("mail reply artifact ancestor ownership is unsafe")
                if created:
                    os.fchmod(next_fd, _PRIVATE_DIRECTORY_MODE)
                    directory_stat = os.fstat(next_fd)
                # ``artifacts`` is shared with other owner-scoped artifact
                # families and may have their broader directory mode.  The
                # Mail private subtree itself remains strictly 0700.
                if index > 0 and stat.S_IMODE(directory_stat.st_mode) != _PRIVATE_DIRECTORY_MODE:
                    raise OSError("mail reply artifact ancestor permissions are unsafe")
                previous_fd = parent_fd
                parent_fd = next_fd
                next_fd = None
                if previous_fd != root_fd:
                    os.close(previous_fd)
            finally:
                if next_fd is not None:
                    try:
                        os.close(next_fd)
                    except OSError:
                        pass
        yield parent_fd, parts[-1], nofollow, cloexec, owner_uid
    except OSError as exc:
        if exc.errno in {
            getattr(os, "ELOOP", 40),
            getattr(os, "ENOTDIR", 20),
        }:
            raise OSError("mail reply artifact path contains a symlink") from exc
        raise
    finally:
        if parent_fd != root_fd:
            try:
                os.close(parent_fd)
            except OSError:
                pass
        try:
            os.close(root_fd)
        except OSError:
            pass


@contextmanager
def _open_private_draft(
    relative_path: str,
    *,
    flags: int,
    create_parents: bool = False,
):
    """Open a private draft through no-follow directory descriptors."""

    final_fd: int | None = None
    with _open_private_parent(relative_path, create_parents=create_parents) as (
        parent_fd,
        filename,
        nofollow,
        cloexec,
        owner_uid,
    ):
        try:
            final_fd = os.open(
                filename,
                flags | nofollow | cloexec,
                _PRIVATE_FILE_MODE,
                dir_fd=parent_fd,
            )
            if flags & os.O_CREAT:
                os.fchmod(final_fd, _PRIVATE_FILE_MODE)
            file_stat = os.fstat(final_fd)
            if (
                not stat.S_ISREG(file_stat.st_mode)
                or file_stat.st_nlink != 1
                or file_stat.st_uid != owner_uid
                or stat.S_IMODE(file_stat.st_mode) != _PRIVATE_FILE_MODE
            ):
                raise OSError("mail reply artifact is not a private regular file")
            yield final_fd, parent_fd
            final_fd = None
        finally:
            if final_fd is not None:
                try:
                    os.close(final_fd)
                except OSError:
                    pass


def _read_private_ciphertext(relative_path: str) -> bytes:
    with _open_private_draft(relative_path, flags=os.O_RDONLY) as (descriptor, _parent_fd):
        # The descriptor is owned by this context until the yielded value is
        # consumed.  Wrap it in a close-owning file object so every read path
        # releases the descriptor even when fstat/read/decryption fails.
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            before = os.fstat(handle.fileno())
            if before.st_size > 96 * 1024:
                raise OSError("mail reply artifact exceeds bounded size")
            encrypted = handle.read(96 * 1024 + 1)
            after = os.fstat(handle.fileno())
    if len(encrypted) > 96 * 1024 or before.st_size != after.st_size or len(encrypted) != before.st_size:
        raise OSError("mail reply artifact changed while reading")
    return encrypted


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
    """Publish one exact private file, preserving crash-safe replay identity."""

    if not isinstance(encrypted, bytes) or not encrypted or len(encrypted) > 96 * 1024:
        raise OSError("mail reply artifact exceeds bounded size")
    # Write to a unique sibling, fsync its complete contents, then hard-link
    # it into the canonical name.  link(2) gives us no-clobber publication
    # without the overwrite race inherent in a check-then-rename sequence.
    # The temporary name is removed only after the canonical link exists; a
    # crash before that point cannot leave a partial canonical artifact.
    with _open_private_parent(relative, create_parents=True) as (
        parent_fd,
        filename,
        nofollow,
        cloexec,
        owner_uid,
    ):
        temporary = f".{filename}.{uuid.uuid4().hex}.tmp"
        temporary_fd: int | None = None
        linked = False

        def remove_temporary() -> None:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            except OSError:
                # The canonical result is never removed as part of uncertain
                # cleanup.  A remaining temp is reconciled on a later retry.
                pass

        try:
            temporary_fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow | cloexec,
                _PRIVATE_FILE_MODE,
                dir_fd=parent_fd,
            )
            os.fchmod(temporary_fd, _PRIVATE_FILE_MODE)
            temporary_stat = os.fstat(temporary_fd)
            if (
                not stat.S_ISREG(temporary_stat.st_mode)
                or temporary_stat.st_nlink != 1
                or temporary_stat.st_uid != owner_uid
                or stat.S_IMODE(temporary_stat.st_mode) != _PRIVATE_FILE_MODE
            ):
                raise OSError("mail reply temporary artifact is not private")
            written = 0
            while written < len(encrypted):
                count = os.write(temporary_fd, encrypted[written:])
                if count <= 0:
                    raise OSError("mail reply artifact write made no progress")
                written += count
            os.fsync(temporary_fd)
        finally:
            if temporary_fd is not None:
                try:
                    os.close(temporary_fd)
                except OSError:
                    pass
                temporary_fd = None

        try:
            try:
                os.link(
                    temporary,
                    filename,
                    src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                linked = True
            except FileExistsError:
                # A response-loss retry may find a file published by the
                # original worker.  Reuse it only when exact checkpoint bytes
                # match; a symlink, hardlink, partial file, or other mismatch
                # remains an explicit unknown rather than being replaced.
                remove_temporary()
                if _read_private_ciphertext(relative) != encrypted:
                    raise OSError("mail reply artifact already contains different bytes")
                return

            # The link is durable only after its directory entry is flushed.
            # Remove the temporary hardlink after the canonical entry exists.
            os.unlink(temporary, dir_fd=parent_fd)
            temporary = ""
            os.fsync(parent_fd)
        except BaseException:
            # If linking did not happen, cleanup is safe.  Once linked, retain
            # the canonical winner and never delete it on an uncertain error.
            if not linked and temporary:
                remove_temporary()
            raise


def write_private_draft(job_id: str, payload: Mapping[str, Any]) -> tuple[str, str, bytes]:
    """Encrypt and publish one private draft, returning its hash."""

    relative, digest, encrypted = prepare_private_draft(job_id, payload)
    publish_private_draft(relative, encrypted)
    return relative, digest, encrypted


def read_private_draft(relative_path: str, expected_sha256: str) -> dict[str, Any]:
    encrypted = _read_private_ciphertext(relative_path)
    if hashlib.sha256(encrypted).hexdigest() != expected_sha256:
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
    "MAX_SOURCE_BODY_BYTES",
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
