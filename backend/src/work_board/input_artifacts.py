"""Owner-bound typed input artifacts for executable WorkBoard tasks.

This module is the only writer for the canonical ``artifacts/work-board/inputs``
payloads.  A reservation is persisted before filesystem I/O, the file is
written atomically and reread, and only then does the row receive a verified
metadata digest.  Task binding is a separate CAS that the repository performs
inside the same SQLite writer transaction as task creation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Awaitable, Callable, Mapping
import uuid

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import settings
from src.db.models import Goal, WorkBoardInputArtifact, WorkBoardStatus, WorkBoardTask
from src.db.engine import get_session
from src.work_board.contracts import (
    WorkBoardInputArtifactCreate,
    WorkBoardOwner,
)
from src.work_board.dispatcher import (
    REGISTERED_CAPABILITIES,
    TypedInputError,
    validate_capability_input,
)
from src.work_board.repository import (
    BoardError,
    WorkBoardRepository,
)
from src.workspace import canonical_workspace_root


INPUT_ARTIFACT_SCHEMA_VERSION = 1
INPUT_ARTIFACT_MAX_BYTES = 64 * 1024
INPUT_ARTIFACT_TTL = timedelta(hours=24)
SCHEDULE_INPUT_ARTIFACT_MAX_RETENTION = timedelta(days=7)
INPUT_ARTIFACT_ROOT = "artifacts/work-board/inputs"
_ARTIFACT_NAMESPACE = uuid.UUID("2b5b3f8d-6d2f-5b4f-91f3-3dcb22bc7697")
_ALLOWED_STATES = frozenset({"pending", "bound", "consumed", "expired", "revoked", "deleted"})
_EXECUTABLE_STATES = frozenset({"pending", "bound"})


class _InputArtifactCleanupUnverified(OSError):
    """The exact terminal artifact file could not be proven safe to remove."""

    def __init__(self, reason: str) -> None:
        self.reason = str(reason or "cleanup_unverified")[:128]
        super().__init__(self.reason)


@dataclass(frozen=True)
class InputArtifactMetadata:
    artifact_id: str
    typed_input_ref: str
    typed_input_digest: str
    capability_id: str
    capability_version: str
    goal_id: str
    goal_revision: int
    expires_at: datetime
    state: str
    size_bytes: int
    bound_task_id: str | None
    bound_task_revision: int | None
    revision: int


@dataclass(frozen=True)
class ResolvedInputArtifact:
    row: WorkBoardInputArtifact
    input: dict[str, Any]
    payload: bytes

    @property
    def metadata(self) -> InputArtifactMetadata:
        return _metadata(self.row)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _metadata_digest(row: WorkBoardInputArtifact) -> str:
    return hashlib.sha256(
        _canonical_json(
            {
                "artifact_id": row.artifact_id,
                "owner_principal_id": row.owner_principal_id,
                "owner_session_id": row.owner_session_id,
                "goal_id": row.goal_id,
                "goal_revision": row.goal_revision,
                "capability_id": row.capability_id,
                "capability_version": row.capability_version,
                "idempotency_key": row.idempotency_key,
                "payload_sha256": row.payload_sha256,
                "typed_input_ref": row.typed_input_ref,
                "size_bytes": row.size_bytes,
                "state": row.state,
                "bound_task_id": row.bound_task_id,
                "bound_task_revision": row.bound_task_revision,
                "created_at": _utc(row.created_at).isoformat(),
                "expires_at": _utc(row.expires_at).isoformat(),
                "consumed_at": _utc(row.consumed_at).isoformat() if row.consumed_at else None,
                "revision": row.revision,
                **({"document_metadata_json": row.document_metadata_json,
                    "document_reserved_bytes": row.document_reserved_bytes}
                    if row.capability_id == "work.document-compare.v1" else {}),
            }
        )
    ).hexdigest()


def _metadata(row: WorkBoardInputArtifact) -> InputArtifactMetadata:
    return InputArtifactMetadata(
        artifact_id=row.artifact_id,
        typed_input_ref=row.typed_input_ref,
        typed_input_digest=row.payload_sha256,
        capability_id=row.capability_id,
        capability_version=row.capability_version,
        goal_id=row.goal_id,
        goal_revision=int(row.goal_revision),
        expires_at=_utc(row.expires_at),
        state=str(row.state),
        size_bytes=int(row.size_bytes),
        bound_task_id=row.bound_task_id,
        bound_task_revision=row.bound_task_revision,
        revision=int(row.revision),
    )


def _artifact_id(owner: WorkBoardOwner, request: WorkBoardInputArtifactCreate) -> str:
    key = "|".join(
        (
            owner.principal_id,
            owner.session_id,
            request.capability_id,
            request.goal_id,
            str(request.goal_revision),
            request.idempotency_key,
        )
    )
    return str(uuid.uuid5(_ARTIFACT_NAMESPACE, key))


def _payload_path(artifact: WorkBoardInputArtifact) -> Path:
    reference = str(artifact.typed_input_ref or "")
    prefix = "workspace-json:"
    if not reference.startswith(prefix):
        raise BoardError("input_artifact_ref_invalid", "The input artifact reference is invalid", status_code=409)
    relative = reference[len(prefix) :]
    if not relative.startswith(f"{INPUT_ARTIFACT_ROOT}/") or ".." in Path(relative).parts:
        raise BoardError("input_artifact_ref_invalid", "The input artifact reference is invalid", status_code=409)
    root = canonical_workspace_root(settings.workspace_dir)
    candidate = root / relative
    try:
        resolved_parent = candidate.parent.resolve(strict=False)
        resolved_parent.relative_to(root)
    except (OSError, ValueError) as exc:
        raise BoardError("input_artifact_path_escape", "The input artifact path is outside the workspace", status_code=409) from exc
    return candidate


def _open_input_artifact_parent(path: Path, *, create: bool) -> tuple[int, str]:
    """Open the typed-input parent through held no-follow directory handles."""

    root = canonical_workspace_root(settings.workspace_dir)
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise OSError("input artifact path escapes the canonical workspace") from exc
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise OSError("input artifact path is invalid")

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow | cloexec
    parent_fd = -1
    try:
        if create:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent_fd = os.open(root, directory_flags)
        root_metadata = os.fstat(parent_fd)
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_uid not in {0, os.getuid()}
        ):
            raise OSError("input artifact workspace root is untrusted")

        for component in relative.parts[:-1]:
            if create:
                try:
                    os.mkdir(component, 0o700, dir_fd=parent_fd)
                except FileExistsError:
                    pass
            next_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
            metadata = os.fstat(parent_fd)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.getuid()
            ):
                raise OSError("input artifact parent is not a private directory")
            if create:
                # A previously-created canonical artifact directory may have
                # inherited the live workspace's group/other bits.  Repair it
                # through the held descriptor after proving it is a current
                # user's real directory; the canonical workspace root itself
                # is intentionally left untouched.
                os.fchmod(parent_fd, 0o700)
                metadata = os.fstat(parent_fd)
            if metadata.st_mode & 0o077:
                raise OSError("input artifact parent is not a private directory")
        return parent_fd, relative.parts[-1]
    except BaseException:
        if parent_fd >= 0:
            try:
                os.close(parent_fd)
            except OSError:
                pass
        raise


def _private_input_file_metadata(metadata: os.stat_result) -> bool:
    return bool(
        stat.S_ISREG(metadata.st_mode)
        and not metadata.st_mode & 0o077
        and metadata.st_uid == os.getuid()
        and metadata.st_nlink == 1
    )


def _safe_file_bytes(path: Path, *, expected_digest: str, expected_size: int) -> bytes:
    parent_fd = -1
    descriptor = -1
    try:
        parent_fd, filename = _open_input_artifact_parent(path, create=False)
        descriptor = os.open(
            filename,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_fd,
        )
    except OSError as exc:
        if parent_fd >= 0:
            try:
                os.close(parent_fd)
            except OSError:
                pass
        raise BoardError("input_artifact_unavailable", "The input artifact file is unavailable", status_code=409) from exc
    try:
        stat_result = os.fstat(descriptor)
        if not _private_input_file_metadata(stat_result) or stat_result.st_size != int(expected_size):
            raise BoardError("input_artifact_file_invalid", "The input artifact is not the verified regular file", status_code=409)
        if stat_result.st_size > INPUT_ARTIFACT_MAX_BYTES:
            raise BoardError("input_artifact_too_large", "The input artifact exceeds 64 KiB", status_code=409)
        chunks: list[bytes] = []
        remaining = stat_result.st_size
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if parent_fd >= 0:
            os.close(parent_fd)
    if len(payload) != int(expected_size) or hashlib.sha256(payload).hexdigest() != expected_digest:
        raise BoardError("input_artifact_digest_mismatch", "The input artifact digest does not match", status_code=409)
    return payload


async def _begin_immediate(db: AsyncSession) -> None:
    bind = db.get_bind()
    if getattr(getattr(bind, "dialect", None), "name", "") != "sqlite":
        return
    if db.in_transaction():
        if db.new or db.dirty or db.deleted:
            raise BoardError(
                "artifact_transaction_boundary",
                "Input artifact writes require a fresh transaction",
                status_code=409,
            )
        await db.commit()
    await db.execute(text("BEGIN IMMEDIATE"))


def _raise_input_error(exc: TypedInputError) -> BoardError:
    code = str(exc.code or "typed_input_invalid")
    status = 404 if code == "capability_unregistered" else 422
    return BoardError(code, "The typed input does not satisfy the registered capability contract", status_code=status)


async def _validate_request(
    db: AsyncSession,
    owner: WorkBoardOwner,
    request: WorkBoardInputArtifactCreate,
    *,
    allow_scheduler: bool = False,
) -> tuple[dict[str, Any], str, str]:
    try:
        inputs = validate_capability_input(
            request.capability_id,
            request.input,
            allow_scheduler=allow_scheduler,
        )
    except TypedInputError as exc:
        raise _raise_input_error(exc) from exc
    spec = REGISTERED_CAPABILITIES.get(request.capability_id)
    if spec is None or spec.secret_like:
        raise BoardError("secret_like_capability_blocked", "This capability cannot use public typed input storage", status_code=422)
    if spec.input_category == "scheduler" and not allow_scheduler:
        raise BoardError(
            "typed_input_category_invalid",
            "Scheduler configuration artifacts cannot be used as task input",
            status_code=422,
        )
    try:
        await WorkBoardRepository._validate_goal(
            db,
            owner,
            goal_id=request.goal_id,
            goal_revision=request.goal_revision,
        )
    except BoardError:
        raise
    envelope = {
        "schema_version": INPUT_ARTIFACT_SCHEMA_VERSION,
        "capability_id": request.capability_id,
        "input": inputs,
    }
    payload = _canonical_json(envelope)
    if len(payload) > INPUT_ARTIFACT_MAX_BYTES:
        raise BoardError("input_artifact_too_large", "The typed input exceeds 64 KiB", status_code=422)
    return inputs, payload.hex(), hashlib.sha256(payload).hexdigest()


def _decode_and_validate_payload(
    row: WorkBoardInputArtifact,
    payload: bytes,
    *,
    allow_scheduler: bool = False,
) -> dict[str, Any]:
    try:
        envelope = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BoardError("input_artifact_json_invalid", "The input artifact is not valid JSON", status_code=409) from exc
    if not isinstance(envelope, Mapping) or set(envelope) != {"schema_version", "capability_id", "input"}:
        raise BoardError("input_artifact_envelope_invalid", "The input artifact envelope is invalid", status_code=409)
    if envelope.get("schema_version") != INPUT_ARTIFACT_SCHEMA_VERSION or envelope.get("capability_id") != row.capability_id:
        raise BoardError("input_artifact_envelope_invalid", "The input artifact envelope binding is invalid", status_code=409)
    raw_input = envelope.get("input")
    if not isinstance(raw_input, Mapping):
        raise BoardError("input_artifact_input_invalid", "The input artifact input is invalid", status_code=409)
    try:
        return validate_capability_input(
            row.capability_id,
            raw_input,
            allow_scheduler=allow_scheduler,
        )
    except TypedInputError as exc:
        raise _raise_input_error(exc) from exc


async def _finalize_pending(
    db: AsyncSession,
    row: WorkBoardInputArtifact,
    payload: bytes,
    *,
    inputs: Mapping[str, Any] | None = None,
    allow_scheduler: bool = False,
) -> None:
    path = _payload_path(row)
    verified = _safe_file_bytes(path, expected_digest=row.payload_sha256, expected_size=row.size_bytes)
    parsed = _decode_and_validate_payload(
        row,
        verified,
        allow_scheduler=allow_scheduler,
    )
    if inputs is not None and parsed != dict(inputs):
        raise BoardError("input_artifact_digest_mismatch", "The input artifact input changed", status_code=409)
    row.revision = max(int(row.revision), 1) + 1
    row.metadata_digest = _metadata_digest(row)
    await db.execute(
        update(WorkBoardInputArtifact)
        .where(
            WorkBoardInputArtifact.artifact_id == row.artifact_id,
            WorkBoardInputArtifact.state == "pending",
            WorkBoardInputArtifact.metadata_digest.is_(None),
            WorkBoardInputArtifact.revision == row.revision - 1,
        )
        .values(revision=row.revision, metadata_digest=row.metadata_digest)
        .execution_options(synchronize_session=False)
    )
    await db.flush()


def _write_payload(path: Path, payload: bytes) -> None:
    parent_fd = -1
    descriptor = -1
    temporary_name = ""
    try:
        parent_fd, filename = _open_input_artifact_parent(path, create=True)
        for _attempt in range(5):
            temporary_name = f".{filename}.{uuid.uuid4().hex}.tmp"
            try:
                descriptor = os.open(
                    temporary_name,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    0o600,
                    dir_fd=parent_fd,
                )
                break
            except FileExistsError:
                temporary_name = ""
        if descriptor < 0:
            raise OSError("input artifact temporary file could not be reserved")
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

        # A hard-link publication makes the final name no-clobber while the
        # directory descriptor remains held.  An existing name is accepted
        # only when its private metadata and bytes are an exact replay.
        try:
            os.link(
                temporary_name,
                filename,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileExistsError:
            existing_fd = -1
            try:
                existing_fd = os.open(
                    filename,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=parent_fd,
                )
                existing_metadata = os.fstat(existing_fd)
                if not _private_input_file_metadata(existing_metadata):
                    raise OSError("existing input artifact is not private")
                existing = os.read(existing_fd, INPUT_ARTIFACT_MAX_BYTES + 1)
                if len(existing) > INPUT_ARTIFACT_MAX_BYTES or existing != payload:
                    raise OSError("input artifact collision")
            finally:
                if existing_fd >= 0:
                    os.close(existing_fd)
        else:
            os.unlink(temporary_name, dir_fd=parent_fd)
            temporary_name = ""
        os.fsync(parent_fd)
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if parent_fd >= 0 and temporary_name:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except OSError:
                pass
        if parent_fd >= 0:
            try:
                os.close(parent_fd)
            except OSError:
                pass


def _cleanup_private_input_file(
    path: Path,
    *,
    expected_digest: str,
    expected_size: int,
) -> None:
    """Remove one terminal input only after exact descriptor-bound proof.

    The parent directory stays open and no-follow for the complete operation.
    The target is opened no-follow, checked against the recorded owner/mode/
    link/size/digest, then compared with a fresh name-relative stat immediately
    before unlinking.  A missing or replaced target is an unknown cleanup
    outcome, never permission to remove another file.
    """

    parent_fd = -1
    descriptor = -1
    try:
        try:
            parent_fd, filename = _open_input_artifact_parent(path, create=False)
            descriptor = os.open(
                filename,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent_fd,
            )
        except FileNotFoundError as exc:
            raise _InputArtifactCleanupUnverified("cleanup_target_missing") from exc
        except OSError as exc:
            raise _InputArtifactCleanupUnverified("cleanup_target_unavailable") from exc

        metadata = os.fstat(descriptor)
        try:
            expected_size_int = int(expected_size)
        except (TypeError, ValueError, OverflowError) as exc:
            raise _InputArtifactCleanupUnverified("cleanup_size_invalid") from exc
        if (
            expected_size_int < 0
            or expected_size_int > INPUT_ARTIFACT_MAX_BYTES
            or not _private_input_file_metadata(metadata)
            or metadata.st_size != expected_size_int
        ):
            raise _InputArtifactCleanupUnverified("cleanup_target_metadata_mismatch")

        chunks: list[bytes] = []
        remaining = INPUT_ARTIFACT_MAX_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        if len(payload) != expected_size_int or hashlib.sha256(payload).hexdigest() != str(expected_digest):
            raise _InputArtifactCleanupUnverified("cleanup_digest_mismatch")

        # The open descriptor proves the bytes and metadata we inspected.  A
        # final no-follow name stat prevents a replacement that happened after
        # open/read from being unlinked through the held parent descriptor.
        try:
            named_metadata = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise _InputArtifactCleanupUnverified("cleanup_target_replaced") from exc
        if any(
            getattr(named_metadata, field, None) != getattr(metadata, field, None)
            for field in ("st_dev", "st_ino", "st_mode", "st_uid", "st_nlink", "st_size")
        ):
            raise _InputArtifactCleanupUnverified("cleanup_target_replaced")
        if not _private_input_file_metadata(named_metadata):
            raise _InputArtifactCleanupUnverified("cleanup_target_metadata_mismatch")
        try:
            os.unlink(filename, dir_fd=parent_fd)
        except FileNotFoundError as exc:
            raise _InputArtifactCleanupUnverified("cleanup_target_replaced") from exc
        os.fsync(parent_fd)
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if parent_fd >= 0:
            try:
                os.close(parent_fd)
            except OSError:
                pass


def _cleanup_required_error(
    row: WorkBoardInputArtifact,
    *,
    state: str,
    reason: str,
    cleanup_required_artifact_ids: list[str] | None = None,
) -> BoardError:
    """Return the stable operator receipt for a terminal cleanup unknown."""

    extra: dict[str, Any] = {
        "artifact_id": str(row.artifact_id),
        "state": str(state),
        "revision": int(row.revision),
        "cleanup_status": "cleanup_required",
        "reason_code": str(reason or "cleanup_unverified")[:128],
        "recovery_action": "reconcile_input_artifact_cleanup",
    }
    if cleanup_required_artifact_ids is not None:
        extra["cleanup_required_artifact_ids"] = list(cleanup_required_artifact_ids)
        extra["cleanup_required_count"] = len(cleanup_required_artifact_ids)
    return BoardError(
        "input_artifact_cleanup_required",
        "The input artifact terminal cleanup requires reconciliation",
        status_code=503,
        **extra,
    )


async def prepare_input_artifact(
    db: AsyncSession,
    owner: WorkBoardOwner,
    request: WorkBoardInputArtifactCreate,
    *,
    now: datetime | None = None,
    allow_scheduler: bool = False,
    retention_deadline: datetime | None = None,
) -> InputArtifactMetadata:
    """Reserve, write, reread, and verify one deterministic input artifact.

    ``retention_deadline`` is a server-only extension for the v2 schedule seed.
    Ordinary callers retain the fixed 24-hour lifetime; the narrow shape check
    below prevents a public task artifact from selecting the longer retention.
    """

    observed_at = _utc(now or _now())
    if request.capability_id == "work.document-compare.v1":
        raise BoardError("document_pair_reservation_required", "Select and stream a private document pair first", status_code=422)
    inputs, _payload_hex, payload_digest = await _validate_request(
        db,
        owner,
        request,
        allow_scheduler=allow_scheduler,
    )
    if retention_deadline is not None:
        schedule_invocation = inputs.get("invocation_uuid") if isinstance(inputs, Mapping) else None
        is_procedure_schedule_seed = (
            request.capability_id == "guardian-routine.v2"
            and request.idempotency_key.startswith("schedule:")
            and schedule_invocation == request.idempotency_key
        )
        is_mail_watch_seed = (
            request.capability_id == "gmail.scan_metadata.v1"
            and request.idempotency_key.startswith("mail-watch:")
            and isinstance(inputs, Mapping)
            and inputs.get("schema_version") == 1
            and inputs.get("connection_id")
            and inputs.get("consent_id")
        )
        if not (is_procedure_schedule_seed or is_mail_watch_seed):
            raise BoardError(
                "input_artifact_retention_invalid",
                "Extended input retention is reserved for a reviewed schedule or Mail watch seed",
                status_code=422,
            )
        requested_deadline = _utc(retention_deadline)
        maximum_deadline = observed_at + SCHEDULE_INPUT_ARTIFACT_MAX_RETENTION
        if requested_deadline <= observed_at or requested_deadline > maximum_deadline:
            raise BoardError(
                "input_artifact_retention_invalid",
                "The reviewed schedule seed retention is outside the bounded window",
                status_code=422,
            )
    else:
        requested_deadline = None
    envelope = {
        "schema_version": INPUT_ARTIFACT_SCHEMA_VERSION,
        "capability_id": request.capability_id,
        "input": inputs,
    }
    payload = _canonical_json(envelope)
    artifact_id = _artifact_id(owner, request)
    expires_at = requested_deadline or (observed_at + INPUT_ARTIFACT_TTL)
    typed_input_ref = f"workspace-json:{INPUT_ARTIFACT_ROOT}/{artifact_id}-{payload_digest}.json"

    await _begin_immediate(db)
    existing = (
        await db.execute(
            select(WorkBoardInputArtifact).where(
                WorkBoardInputArtifact.owner_principal_id == owner.principal_id,
                WorkBoardInputArtifact.owner_session_id == owner.session_id,
                WorkBoardInputArtifact.capability_id == request.capability_id,
                WorkBoardInputArtifact.goal_id == request.goal_id,
                WorkBoardInputArtifact.goal_revision == request.goal_revision,
                WorkBoardInputArtifact.idempotency_key == request.idempotency_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.payload_sha256 != payload_digest:
            raise BoardError("input_artifact_idempotency_conflict", "The idempotency key is bound to another input", status_code=409)
        if requested_deadline is not None and _utc(existing.expires_at) != requested_deadline:
            raise BoardError(
                "input_artifact_idempotency_conflict",
                "The idempotency key is bound to another schedule retention",
                status_code=409,
            )
        if existing.state in {"expired", "revoked", "deleted"}:
            return _metadata(existing)
        if existing.metadata_digest is None:
            await db.commit()
            try:
                _write_payload(_payload_path(existing), payload)
            except OSError as exc:
                raise BoardError("input_artifact_write_failed", "The input artifact could not be written", status_code=503) from exc
            async with db.begin():
                refreshed = (
                    await db.execute(select(WorkBoardInputArtifact).where(WorkBoardInputArtifact.artifact_id == existing.artifact_id))
                ).scalar_one_or_none()
                if refreshed is None:
                    raise BoardError("input_artifact_missing", "The input artifact reservation disappeared", status_code=409)
                await _finalize_pending(
                    db,
                    refreshed,
                    payload,
                    inputs=inputs,
                    allow_scheduler=allow_scheduler,
                )
                return _metadata(refreshed)
        # An idempotent replay is only valid while the previously verified
        # canonical bytes are still present and structurally valid.  A stale
        # metadata row must fail closed instead of returning a receipt that a
        # later task bind could not execute.
        verified = _safe_file_bytes(
            _payload_path(existing),
            expected_digest=existing.payload_sha256,
            expected_size=existing.size_bytes,
        )
        parsed = _decode_and_validate_payload(
            existing,
            verified,
            allow_scheduler=allow_scheduler,
        )
        if parsed != inputs:
            raise BoardError(
                "input_artifact_digest_mismatch",
                "The input artifact input changed",
                status_code=409,
            )
        return _metadata(existing)

    row = WorkBoardInputArtifact(
        artifact_id=artifact_id,
        owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id,
        goal_id=request.goal_id,
        goal_revision=request.goal_revision,
        capability_id=request.capability_id,
        capability_version=REGISTERED_CAPABILITIES[request.capability_id].version,
        idempotency_key=request.idempotency_key,
        payload_sha256=payload_digest,
        typed_input_ref=typed_input_ref,
        size_bytes=len(payload),
        state="pending",
        expires_at=expires_at,
        revision=1,
    )
    db.add(row)
    await db.flush()
    await db.commit()
    try:
        _write_payload(_payload_path(row), payload)
    except OSError as exc:
        raise BoardError("input_artifact_write_failed", "The input artifact could not be written", status_code=503) from exc
    async with db.begin():
        refreshed = (
            await db.execute(select(WorkBoardInputArtifact).where(WorkBoardInputArtifact.artifact_id == artifact_id))
        ).scalar_one_or_none()
        if refreshed is None:
            raise BoardError("input_artifact_missing", "The input artifact reservation disappeared", status_code=409)
        await _finalize_pending(
            db,
            refreshed,
            payload,
            inputs=inputs,
            allow_scheduler=allow_scheduler,
        )
        return _metadata(refreshed)


async def resolve_input_artifact_for_task(
    db: AsyncSession,
    owner: WorkBoardOwner,
    *,
    artifact_id: str,
    goal_id: str,
    goal_revision: int,
    capability_id: str,
    expected_task_id: str | None = None,
    now: datetime | None = None,
) -> ResolvedInputArtifact:
    """Resolve and verify an owner-bound artifact for dispatcher execution."""

    row = (
        await db.execute(
            select(WorkBoardInputArtifact).where(
                WorkBoardInputArtifact.artifact_id == artifact_id,
                WorkBoardInputArtifact.owner_principal_id == owner.principal_id,
                WorkBoardInputArtifact.owner_session_id == owner.session_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise BoardError("input_artifact_not_found", "The input artifact is unavailable", status_code=404)
    if row.state not in _EXECUTABLE_STATES or not row.metadata_digest:
        raise BoardError("input_artifact_not_executable", "The input artifact is not executable", status_code=409)
    if _utc(row.expires_at) <= _utc(now or _now()):
        raise BoardError("input_artifact_expired", "The input artifact has expired", status_code=409)
    if row.goal_id != goal_id or int(row.goal_revision) != int(goal_revision) or row.capability_id != capability_id:
        raise BoardError("input_artifact_binding_mismatch", "The input artifact binding does not match the task", status_code=409)
    expected_version = REGISTERED_CAPABILITIES.get(capability_id)
    if expected_version is None or row.capability_version != expected_version.version:
        raise BoardError("input_artifact_capability_stale", "The input artifact capability version is stale", status_code=409)
    if expected_version.input_category != "task":
        raise BoardError(
            "typed_input_category_invalid",
            "Scheduler configuration artifacts cannot be executed as a WorkBoard task",
            status_code=422,
        )
    if expected_task_id is not None and row.bound_task_id not in {None, expected_task_id}:
        raise BoardError("input_artifact_task_conflict", "The input artifact is bound to another task", status_code=409)
    if _metadata_digest(row) != row.metadata_digest:
        raise BoardError("input_artifact_metadata_mismatch", "The input artifact metadata changed", status_code=409)
    payload = _safe_file_bytes(_payload_path(row), expected_digest=row.payload_sha256, expected_size=row.size_bytes)
    parsed = _decode_and_validate_payload(row, payload)
    return ResolvedInputArtifact(row=row, input=parsed, payload=payload)


async def read_input_artifact_metadata(
    db: AsyncSession,
    owner: WorkBoardOwner,
    *,
    artifact_id: str,
) -> InputArtifactMetadata:
    """Return owner-fenced metadata without reading or touching the payload.

    Metadata reads intentionally do not repair a digest, expire a row, or
    inspect the JSON file.  The lifecycle jobs and the execution resolver are
    the only paths that perform those checks or mutations.
    """

    row = (
        await db.execute(
            select(WorkBoardInputArtifact).where(
                WorkBoardInputArtifact.artifact_id == artifact_id,
                WorkBoardInputArtifact.owner_principal_id == owner.principal_id,
                WorkBoardInputArtifact.owner_session_id == owner.session_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise BoardError("input_artifact_not_found", "The input artifact is unavailable", status_code=404)
    return _metadata(row)


async def resolve_input_artifact_for_copy(
    db: AsyncSession,
    owner: WorkBoardOwner,
    *,
    typed_input_ref: str,
    typed_input_digest: str,
    capability_id: str,
    goal_id: str,
    goal_revision: int,
    allow_goal_change: bool = False,
    now: datetime | None = None,
) -> ResolvedInputArtifact:
    """Read an immutable prior input solely to materialize a fresh leaf.

    Procedure invocation creates a new owner-bound artifact for each native
    leaf.  The reviewed plan stores only the prior reference and digest, so
    this narrow server helper resolves that reference without treating the
    prior row as executable authority or binding the new task to it.  Consumed
    rows remain copyable while their verified file and metadata are present;
    expired, revoked, or deleted rows fail closed.  Ordinary callers remain
    bound to ``goal_id``/``goal_revision``.  The fixed procedure runtime may
    set ``allow_goal_change`` after it has independently verified the reviewed
    source reference and current invocation goal; this preserves source proof
    when a new goal revision owns the copied leaf.
    """

    reference = str(typed_input_ref or "")
    digest = str(typed_input_digest or "").lower()
    prefix = "workspace-json:"
    root_prefix = f"{prefix}{INPUT_ARTIFACT_ROOT}/"
    if not reference.startswith(root_prefix) or not re.fullmatch(r"[A-Za-z0-9_.:-]+-[0-9a-f]{64}\.json", reference[len(root_prefix) :]):
        raise BoardError("input_artifact_ref_invalid", "The input artifact reference is invalid", status_code=409)
    filename = reference[len(root_prefix) :]
    artifact_id = filename[: -len(digest) - 6] if digest and filename.endswith(f"-{digest}.json") else ""
    if not artifact_id or len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise BoardError("input_artifact_binding_mismatch", "The input artifact binding is invalid", status_code=409)
    row = (
        await db.execute(
            select(WorkBoardInputArtifact).where(
                WorkBoardInputArtifact.artifact_id == artifact_id,
                WorkBoardInputArtifact.owner_principal_id == owner.principal_id,
                WorkBoardInputArtifact.owner_session_id == owner.session_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise BoardError("input_artifact_not_found", "The input artifact is unavailable", status_code=404)
    if (
        row.typed_input_ref != reference
        or row.payload_sha256 != digest
        or row.capability_id != capability_id
        or (
            not allow_goal_change
            and (
                row.goal_id != goal_id
                or int(row.goal_revision) != int(goal_revision)
            )
        )
        or row.state not in _EXECUTABLE_STATES | {"consumed"}
        or not row.metadata_digest
    ):
        raise BoardError("input_artifact_binding_mismatch", "The input artifact binding does not match the reviewed plan", status_code=409)
    if _utc(row.expires_at) <= _utc(now or _now()):
        raise BoardError("input_artifact_expired", "The input artifact has expired", status_code=409)
    expected_version = REGISTERED_CAPABILITIES.get(capability_id)
    if expected_version is None or row.capability_version != expected_version.version:
        raise BoardError("input_artifact_capability_stale", "The input artifact capability version is stale", status_code=409)
    if _metadata_digest(row) != row.metadata_digest:
        raise BoardError("input_artifact_metadata_mismatch", "The input artifact metadata changed", status_code=409)
    payload = _safe_file_bytes(_payload_path(row), expected_digest=row.payload_sha256, expected_size=row.size_bytes)
    parsed = _decode_and_validate_payload(row, payload)
    return ResolvedInputArtifact(row=row, input=parsed, payload=payload)


async def bind_input_artifact(
    db: AsyncSession,
    owner: WorkBoardOwner,
    *,
    artifact: ResolvedInputArtifact,
    task_id: str,
    task_revision: int,
) -> None:
    """Bind an artifact to exactly one task using an owner/fence CAS."""

    started_transaction = not db.in_transaction()
    if started_transaction:
        await _begin_immediate(db)
    row = artifact.row
    if row.owner_principal_id != owner.principal_id or row.owner_session_id != owner.session_id:
        raise BoardError("input_artifact_owner_mismatch", "The input artifact is not owned by this session", status_code=404)
    if row.bound_task_id is not None:
        if row.bound_task_id == task_id and row.bound_task_revision == task_revision:
            return
        raise BoardError("input_artifact_task_conflict", "The input artifact is already bound", status_code=409)
    current_revision = int(row.revision)
    next_revision = current_revision + 1
    next_state = "bound"
    # Compute the post-CAS metadata digest without dirtying the ORM identity
    # before the SQL predicate runs; autoflush would otherwise make the
    # pending row disappear from its own revision/state fence.
    prior_state = row.state
    prior_task_id = row.bound_task_id
    prior_task_revision = row.bound_task_revision
    prior_revision = row.revision
    row.state = next_state
    row.bound_task_id = task_id
    row.bound_task_revision = task_revision
    row.revision = next_revision
    next_metadata_digest = _metadata_digest(row)
    row.state = prior_state
    row.bound_task_id = prior_task_id
    row.bound_task_revision = prior_task_revision
    row.revision = prior_revision
    observed_at = _utc(_now())
    result = await db.execute(
        update(WorkBoardInputArtifact)
        .where(
            WorkBoardInputArtifact.artifact_id == row.artifact_id,
            WorkBoardInputArtifact.owner_principal_id == owner.principal_id,
            WorkBoardInputArtifact.owner_session_id == owner.session_id,
            WorkBoardInputArtifact.goal_id == row.goal_id,
            WorkBoardInputArtifact.goal_revision == row.goal_revision,
            WorkBoardInputArtifact.capability_id == row.capability_id,
            WorkBoardInputArtifact.capability_version == row.capability_version,
            WorkBoardInputArtifact.state == "pending",
            WorkBoardInputArtifact.bound_task_id.is_(None),
            WorkBoardInputArtifact.revision == current_revision,
            WorkBoardInputArtifact.payload_sha256 == row.payload_sha256,
            WorkBoardInputArtifact.metadata_digest == row.metadata_digest,
            WorkBoardInputArtifact.expires_at > observed_at,
        )
        .values(
            state=next_state,
            bound_task_id=task_id,
            bound_task_revision=task_revision,
            revision=next_revision,
            metadata_digest=next_metadata_digest,
        )
        .execution_options(synchronize_session=False)
    )
    if int(result.rowcount or 0) != 1:
        raise BoardError("input_artifact_task_conflict", "The input artifact binding changed", status_code=409)
    await db.flush()
    await db.refresh(row)
    if started_transaction:
        await db.commit()


async def consume_input_artifact(
    db: AsyncSession,
    owner: WorkBoardOwner,
    *,
    task_id: str,
    task_revision: int,
    artifact_id: str,
    now: datetime | None = None,
) -> None:
    observed_at = _utc(now or _now())
    # The metadata digest covers lifecycle state and revision.  Keep it in the
    # same CAS update as consumption so a replay cannot mistake a bound row
    # for the current authoritative metadata.
    next_revision = WorkBoardInputArtifact.revision + 1
    result = await db.execute(
        update(WorkBoardInputArtifact)
        .where(
            WorkBoardInputArtifact.artifact_id == artifact_id,
            WorkBoardInputArtifact.owner_principal_id == owner.principal_id,
            WorkBoardInputArtifact.owner_session_id == owner.session_id,
            WorkBoardInputArtifact.bound_task_id == task_id,
            WorkBoardInputArtifact.bound_task_revision == task_revision,
            WorkBoardInputArtifact.state == "bound",
        )
        .values(state="consumed", consumed_at=observed_at, revision=next_revision)
        .execution_options(synchronize_session=False)
    )
    if int(result.rowcount or 0) != 1:
        raise BoardError("input_artifact_consume_conflict", "The input artifact could not be consumed", status_code=409)
    refreshed = (
        await db.execute(
            select(WorkBoardInputArtifact).where(
                WorkBoardInputArtifact.artifact_id == artifact_id,
                WorkBoardInputArtifact.owner_principal_id == owner.principal_id,
                WorkBoardInputArtifact.owner_session_id == owner.session_id,
            )
        )
    ).scalar_one_or_none()
    if refreshed is not None:
        await db.refresh(refreshed)
        refreshed.metadata_digest = _metadata_digest(refreshed)
        await db.flush()


async def _set_terminal_state(
    db: AsyncSession,
    owner: WorkBoardOwner,
    *,
    artifact_id: str,
    state: str,
    expected_revision: int | None = None,
    now: datetime | None = None,
    require_pending_unbound: bool = False,
    publication_guard: Callable[[AsyncSession, WorkBoardInputArtifact], Awaitable[bool]] | None = None,
) -> InputArtifactMetadata:
    if state not in {"revoked", "deleted", "expired"}:
        raise ValueError("invalid terminal input artifact state")
    # Terminal transitions are serialized with the same SQLite writer fence
    # used by reservation and binding.  The owner/revision predicate below is
    # still required: the writer fence prevents interleaving, while the CAS
    # prevents a stale caller from changing a row after it has been refreshed.
    await _begin_immediate(db)
    row = (
        await db.execute(
            select(WorkBoardInputArtifact).where(
                WorkBoardInputArtifact.artifact_id == artifact_id,
                WorkBoardInputArtifact.owner_principal_id == owner.principal_id,
                WorkBoardInputArtifact.owner_session_id == owner.session_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise BoardError("input_artifact_not_found", "The input artifact is unavailable", status_code=404)
    if expected_revision is not None and int(row.revision) != int(expected_revision):
        raise BoardError("input_artifact_revision_stale", "The input artifact metadata revision is stale", status_code=409)
    if require_pending_unbound and (
        row.state != "pending"
        or row.bound_task_id is not None
        or row.metadata_digest is None
    ):
        raise BoardError(
            "input_artifact_publication_protected",
            "The input artifact publication state must be reconciled",
            status_code=409,
        )
    if publication_guard is not None:
        try:
            guard_allows_revoke = await publication_guard(db, row)
        except BoardError:
            raise
        except Exception as exc:
            raise BoardError(
                "input_artifact_publication_cleanup_unconfirmed",
                "The input artifact cleanup could not be confirmed",
                status_code=503,
            ) from exc
        if not guard_allows_revoke:
            raise BoardError(
                "input_artifact_publication_protected",
                "The input artifact publication state must be reconciled",
                status_code=409,
            )
    if row.bound_task_id:
        task = (
            await db.execute(
                select(WorkBoardTask).where(
                    WorkBoardTask.task_id == row.bound_task_id,
                    WorkBoardTask.owner_principal_id == owner.principal_id,
                    WorkBoardTask.owner_session_id == owner.session_id,
                )
            )
        ).scalar_one_or_none()
        if task is not None and task.status not in {
            WorkBoardStatus.done,
            WorkBoardStatus.blocked,
            WorkBoardStatus.archived,
            WorkBoardStatus.review,
        }:
            raise BoardError("input_artifact_task_active", "An active task must be reconciled before deletion", status_code=409)
    try:
        path: Path | None = _payload_path(row)
    except BoardError:
        # Preserve the lifecycle tombstone even when a legacy/corrupt
        # reference can no longer be resolved to a cleanup path.
        path = None
    current_revision = int(row.revision)
    next_revision = current_revision + 1
    prior_state = row.state
    prior_revision = row.revision
    row.state = state
    row.revision = next_revision
    next_metadata_digest = _metadata_digest(row)
    row.state = prior_state
    row.revision = prior_revision

    # Tombstone and advance the revision before touching the file.  A failed
    # cleanup therefore cannot leave bytes that a resolver still considers
    # executable, and a concurrent terminal caller cannot win the same fence.
    result = await db.execute(
        update(WorkBoardInputArtifact)
        .where(
            WorkBoardInputArtifact.artifact_id == artifact_id,
            WorkBoardInputArtifact.owner_principal_id == owner.principal_id,
            WorkBoardInputArtifact.owner_session_id == owner.session_id,
            WorkBoardInputArtifact.state == prior_state,
            WorkBoardInputArtifact.revision == current_revision,
            *(
                (WorkBoardInputArtifact.revision == int(expected_revision),)
                if expected_revision is not None
                else ()
            ),
        )
        .values(
            state=state,
            revision=next_revision,
            metadata_digest=next_metadata_digest,
        )
        .execution_options(synchronize_session=False)
    )
    if int(result.rowcount or 0) != 1:
        raise BoardError(
            "input_artifact_revision_stale",
            "The input artifact metadata revision is stale",
            status_code=409,
        )
    await db.refresh(row)
    # The terminal fence must be durable before filesystem mutation.  If the
    # process dies after this commit, the row remains non-executable and the
    # exact cleanup receipt can be reconciled without reopening the input.
    await db.commit()
    try:
        if path is None:
            raise _InputArtifactCleanupUnverified("cleanup_reference_invalid")
        _cleanup_private_input_file(
            path,
            expected_digest=row.payload_sha256,
            expected_size=row.size_bytes,
        )
    except _InputArtifactCleanupUnverified as exc:
        raise _cleanup_required_error(row, state=state, reason=exc.reason) from exc
    except OSError as exc:
        raise _cleanup_required_error(row, state=state, reason="cleanup_unverified") from exc
    return _metadata(row)


async def revoke_input_artifact(
    db: AsyncSession,
    owner: WorkBoardOwner,
    *,
    artifact_id: str,
    expected_revision: int | None = None,
) -> InputArtifactMetadata:
    return await _set_terminal_state(
        db,
        owner,
        artifact_id=artifact_id,
        state="revoked",
        expected_revision=expected_revision,
    )


async def revoke_unpublished_input_artifact(
    db: AsyncSession,
    owner: WorkBoardOwner,
    *,
    artifact_id: str,
    expected_revision: int,
    publication_guard: Callable[[AsyncSession, WorkBoardInputArtifact], Awaitable[bool]],
) -> InputArtifactMetadata:
    """Revoke a prepared artifact only before any canonical publication.

    The guard runs after the lifecycle writer fence and exact owner/revision
    lookup.  It is intentionally server-internal: callers must provide the
    canonical task/schedule reference check, while ordinary artifact
    revocation keeps its existing behavior.
    """

    return await _set_terminal_state(
        db,
        owner,
        artifact_id=artifact_id,
        state="revoked",
        expected_revision=expected_revision,
        require_pending_unbound=True,
        publication_guard=publication_guard,
    )


async def delete_input_artifact(
    db: AsyncSession,
    owner: WorkBoardOwner,
    *,
    artifact_id: str,
    expected_revision: int | None = None,
) -> InputArtifactMetadata:
    row = await db.scalar(select(WorkBoardInputArtifact).where(
        WorkBoardInputArtifact.artifact_id == artifact_id,
        WorkBoardInputArtifact.owner_principal_id == owner.principal_id,
        WorkBoardInputArtifact.owner_session_id == owner.session_id))
    if row is not None and row.capability_id == "work.document-compare.v1":
        raise BoardError("document_pair_cleanup_required", "Use the private pair discard control to verify physical cleanup")
    return await _set_terminal_state(
        db,
        owner,
        artifact_id=artifact_id,
        state="deleted",
        expected_revision=expected_revision,
    )


async def expire_input_artifacts(
    db: AsyncSession | None = None,
    *,
    limit: int = 20,
    now: datetime | None = None,
) -> int:
    """Boundedly tombstone expired artifacts and remove their payload files."""

    if db is None:
        async with get_session() as owned_db:
            return await expire_input_artifacts(owned_db, limit=limit, now=now)
    observed_at = _utc(now or _now())
    await _begin_immediate(db)
    rows = (
        await db.execute(
            select(WorkBoardInputArtifact)
            .where(
                WorkBoardInputArtifact.expires_at <= observed_at,
                WorkBoardInputArtifact.state.in_(tuple(_EXECUTABLE_STATES)),
            )
            .order_by(WorkBoardInputArtifact.expires_at.asc(), WorkBoardInputArtifact.artifact_id.asc())
            .limit(max(1, min(int(limit), 20)))
        )
    ).scalars().all()
    expired_rows: list[WorkBoardInputArtifact] = []
    for row in rows:
        current_revision = int(row.revision)
        next_revision = current_revision + 1
        prior_state = row.state
        prior_revision = row.revision
        row.state = "expired"
        row.revision = next_revision
        next_metadata_digest = _metadata_digest(row)
        row.state = prior_state
        row.revision = prior_revision
        result = await db.execute(
            update(WorkBoardInputArtifact)
            .where(
                WorkBoardInputArtifact.artifact_id == row.artifact_id,
                WorkBoardInputArtifact.state == prior_state,
                WorkBoardInputArtifact.revision == current_revision,
                WorkBoardInputArtifact.expires_at <= observed_at,
                WorkBoardInputArtifact.metadata_digest == row.metadata_digest,
            )
            .values(
                state="expired",
                revision=next_revision,
                metadata_digest=next_metadata_digest,
            )
            .execution_options(synchronize_session=False)
        )
        if int(result.rowcount or 0) != 1:
            continue
        await db.refresh(row)
        expired_rows.append(row)

    # Expiry uses one bounded writer transaction, so make every terminal
    # fence durable before touching any payload.  A failed cleanup below
    # leaves an expired, non-executable row and raises an explicit receipt.
    await db.commit()
    cleanup_failures: list[tuple[WorkBoardInputArtifact, str]] = []
    for row in expired_rows:
        try:
            path = _payload_path(row)
            _cleanup_private_input_file(
                path,
                expected_digest=row.payload_sha256,
                expected_size=row.size_bytes,
            )
        except BoardError as exc:
            cleanup_failures.append((row, exc.code))
        except _InputArtifactCleanupUnverified as exc:
            cleanup_failures.append((row, exc.reason))
        except OSError as exc:
            cleanup_failures.append((row, "cleanup_unverified"))
    if cleanup_failures:
        first_row, first_reason = cleanup_failures[0]
        raise _cleanup_required_error(
            first_row,
            state="expired",
            reason=first_reason,
            cleanup_required_artifact_ids=[str(row.artifact_id) for row, _reason in cleanup_failures],
        )
    return len(expired_rows)


__all__ = [
    "INPUT_ARTIFACT_MAX_BYTES",
    "INPUT_ARTIFACT_ROOT",
    "SCHEDULE_INPUT_ARTIFACT_MAX_RETENTION",
    "InputArtifactMetadata",
    "ResolvedInputArtifact",
    "bind_input_artifact",
    "consume_input_artifact",
    "delete_input_artifact",
    "expire_input_artifacts",
    "prepare_input_artifact",
    "read_input_artifact_metadata",
    "resolve_input_artifact_for_copy",
    "resolve_input_artifact_for_task",
    "revoke_input_artifact",
    "revoke_unpublished_input_artifact",
]
