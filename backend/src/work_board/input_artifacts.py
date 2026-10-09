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
from src.work_board.authored_packages import capability_spec, stage_package_request, is_authored


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


@dataclass(frozen=True)
class InputArtifactWitness:
    artifact_id: str
    revision: int
    metadata_digest: str
    payload: bytes
    input_bytes: bytes


async def stage_input_artifact(db, owner, *, artifact_id, capability_id, goal_id, goal_revision):
    resolved = await resolve_input_artifact_for_task(db, owner, artifact_id=artifact_id,
        capability_id=capability_id, goal_id=goal_id, goal_revision=goal_revision)
    return InputArtifactWitness(artifact_id, resolved.row.revision,
        _metadata_digest(resolved.row), bytes(resolved.payload), _canonical_json(resolved.input))


async def recheck_staged_input(db, owner, request, *, witness: InputArtifactWitness):
    if not isinstance(witness, InputArtifactWitness) or request.input_artifact_id != witness.artifact_id:
        raise BoardError("pipeline_input_changed", "The staged private input is required", status_code=409)
    row = await db.get(WorkBoardInputArtifact, witness.artifact_id, populate_existing=True)
    if (row is None or row.owner_principal_id != owner.principal_id
        or row.owner_session_id != owner.session_id or row.revision != witness.revision
        or row.metadata_digest != witness.metadata_digest or _metadata_digest(row) != witness.metadata_digest
        or row.capability_id != request.capability_id or row.goal_id != request.goal_id
        or row.goal_revision != request.goal_revision or row.state != "pending"
        or row.bound_task_id is not None or _utc(row.expires_at) <= _now()
        or hashlib.sha256(witness.payload).hexdigest() != row.payload_sha256):
        raise BoardError("pipeline_input_changed", "The staged private input changed", status_code=409)
    staged_input = _decode_and_validate_payload(row, witness.payload)
    await _verify_general_proposal(db, row, staged_input)
    if _canonical_json(staged_input) != witness.input_bytes:
        raise BoardError("pipeline_input_changed", "The staged input envelope changed", status_code=409)
    return ResolvedInputArtifact(row, staged_input, witness.payload)


@dataclass(frozen=True)
class InputRetirementWitness:
    artifact_id: str
    revision: int
    metadata_digest: str
    task_id: str
    task_revision: int
    typed_input_ref: str
    content_sha256: str
    size_bytes: int
    workspace_identity: bytes


@dataclass(frozen=True)
class RetiredInputCleanupWitness:
    operation_id: str
    owner_principal_id: str
    original_root_id: str
    proposal_request_digest: str
    accepted_request_digest: str
    intent_bytes: bytes
    entry_bytes: bytes
    workspace_identity: bytes
    workspace_path: str


@dataclass(frozen=True)
class RetiredInputCleanupReadback:
    intent_digest: str
    artifact_id: str
    outcome: str
    reason_code: str | None


def cleanup_retired_input(witness: RetiredInputCleanupWitness) -> RetiredInputCleanupReadback:
    """Exact held-root/private-parent unlink, or narrow positive leaf absence."""
    entry = json.loads(witness.entry_bytes)
    identity = json.loads(witness.workspace_identity)
    parent_fd = descriptor = -1
    reason = None
    try:
        reference = entry["typed_input_ref"]
        relative = reference.removeprefix("workspace-json:")
        if (not reference.startswith(f"workspace-json:{INPUT_ARTIFACT_ROOT}/")
            or any(part in {"", ".", ".."} for part in relative.split("/"))):
            raise _InputArtifactCleanupUnverified("cleanup_reference_invalid")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        parent_fd = os.open(witness.workspace_path, flags | getattr(os, "O_DIRECTORY", 0))
        root_stat = os.fstat(parent_fd)
        if (root_stat.st_dev != identity["device"] or root_stat.st_ino != identity["inode"]
            or hashlib.sha256(witness.workspace_path.encode()).hexdigest() != identity["path_digest"]
            or not stat.S_ISDIR(root_stat.st_mode) or root_stat.st_uid not in {0, os.getuid()}):
            raise _InputArtifactCleanupUnverified("cleanup_root_changed")
        components = relative.split("/")
        for component in components[:-1]:
            next_fd = os.open(component, flags | getattr(os, "O_DIRECTORY", 0), dir_fd=parent_fd)
            os.close(parent_fd); parent_fd = next_fd
            metadata = os.fstat(parent_fd)
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
                raise _InputArtifactCleanupUnverified("cleanup_target_metadata_mismatch")
        leaf = components[-1]
        try:
            descriptor = os.open(leaf, flags, dir_fd=parent_fd)
        except FileNotFoundError:
            # Only this leaf under the proved held parent may be absent.
            os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            raise _InputArtifactCleanupUnverified("cleanup_target_replaced")
        metadata = os.fstat(descriptor)
        if not _private_input_file_metadata(metadata) or metadata.st_size != entry["size_bytes"]:
            raise _InputArtifactCleanupUnverified("cleanup_target_metadata_mismatch")
        chunks, remaining = [], INPUT_ARTIFACT_MAX_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 65536))
            if not chunk: break
            chunks.append(chunk); remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) != entry["size_bytes"] or hashlib.sha256(raw).hexdigest() != entry["content_sha256"]:
            raise _InputArtifactCleanupUnverified("cleanup_digest_mismatch")
        named = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
        if any(getattr(named, field) != getattr(metadata, field) for field in ("st_dev", "st_ino", "st_mode", "st_uid", "st_nlink", "st_size")):
            raise _InputArtifactCleanupUnverified("cleanup_target_replaced")
        os.unlink(leaf, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except FileNotFoundError:
        # Positive only when all parent components were opened and leaf open
        # failed. A missing root/parent never reaches this descriptor state.
        if parent_fd < 0 or 'leaf' not in locals() or descriptor >= 0:
            reason = "cleanup_target_missing"
    except _InputArtifactCleanupUnverified as exc:
        reason = exc.reason
    except OSError:
        reason = "cleanup_target_unavailable"
    finally:
        if descriptor >= 0: os.close(descriptor)
        if parent_fd >= 0: os.close(parent_fd)
    return RetiredInputCleanupReadback(hashlib.sha256(witness.intent_bytes).hexdigest(),
        entry["artifact_ref"], "cleanup_required" if reason else "absent", reason)


async def _revoke_input_artifact_locked(db, owner, *, witness: InputRetirementWitness):
    if not isinstance(witness, InputRetirementWitness):
        raise BoardError("pipeline_input_changed", "Input retirement proof is required", status_code=409)
    row = await db.get(WorkBoardInputArtifact, witness.artifact_id, populate_existing=True)
    if (row is None or row.owner_principal_id != owner.principal_id
        or row.owner_session_id != owner.session_id or row.revision != witness.revision
        or _metadata_digest(row) != witness.metadata_digest or row.metadata_digest != witness.metadata_digest
        or row.bound_task_id != witness.task_id or row.typed_input_ref != witness.typed_input_ref
        or row.payload_sha256 != witness.content_sha256 or row.size_bytes != witness.size_bytes
        or row.state != "bound"):
        raise BoardError("pipeline_input_changed", "The retirement binding changed", status_code=409)
    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == witness.task_id).execution_options(populate_existing=True))
    if task is None or task.task_revision != witness.task_revision or task.input_artifact_id != row.artifact_id:
        raise BoardError("pipeline_task_changed", "The retiring consumer changed", status_code=409)
    prior_revision, prior_state = row.revision, row.state
    row.revision, row.state = prior_revision + 1, "revoked"
    tombstone_digest = _metadata_digest(row)
    row.revision, row.state = prior_revision, prior_state
    changed = await db.execute(update(WorkBoardInputArtifact).where(
        WorkBoardInputArtifact.artifact_id == row.artifact_id,
        WorkBoardInputArtifact.revision == witness.revision,
        WorkBoardInputArtifact.metadata_digest == witness.metadata_digest,
        WorkBoardInputArtifact.state == "bound").values(state="revoked", revision=prior_revision+1,
        metadata_digest=tombstone_digest).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise BoardError("pipeline_input_changed", "The retirement CAS changed", status_code=409)
    await db.refresh(row)
    return row.revision, tombstone_digest


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
                    if row.capability_id in {"work.document-compare.v1", "document.read.v1", "document.build.v1", "inference.near-text.v1"} else {}),
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
    publication_population=None,
    general_task_publication=None,
) -> tuple[dict[str, Any], str, str]:
    if (request.capability_id == "agent.task.v1" and isinstance(request.input, Mapping)
            and request.input.get("repository_source") is not None
            and general_task_publication is None):
        raise BoardError("repository_source_publication_required",
            "Repository source binding requires its fixed inspected Task publisher", status_code=422)
    if general_task_publication is not None:
        from src.work_board.general_task_proposal import recheck_proposal_publication
        envelope = await recheck_proposal_publication(db, owner, general_task_publication)
        if (request.capability_id != "agent.task.v1" or request.goal_id != envelope.task_input.goal_ref
            or request.goal_revision != general_task_publication.goal_revision):
            raise BoardError("general_task_publication_binding_changed", "Exact native task publication required", status_code=409)
    if request.capability_id == "memory.opportunity-preference.v1":
        from src.guardian.opportunity_preferences import PopulationWitness, recheck_population
        if (not isinstance(publication_population, PopulationWitness)
            or publication_population.owner_principal_id != owner.principal_id
            or publication_population.original_root_id != owner.session_id
            or request.goal_id != publication_population.goal_id
            or request.goal_revision != publication_population.goal_revision
            or request.input != publication_population.cpu_input().model_dump(mode="json")):
            raise BoardError("opportunity_recommendation_system_only", "Use the authenticated recommendation request", status_code=409)
        await recheck_population(db, witness=publication_population)
    try:
        inputs = validate_capability_input(
            request.capability_id,
            request.input,
            allow_scheduler=allow_scheduler,
            general_task_publication=general_task_publication,
        )
    except TypedInputError as exc:
        raise _raise_input_error(exc) from exc
    spec = capability_spec(request.capability_id)
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
    if row.capability_id == "agent.task.v1":
        from src.work_board.general_task_proposal import stored_scan_input
        from src.work_board.contracts import GeneralTaskEnvelope
        scan = stored_scan_input(row, raw_input)
        validate_capability_input(row.capability_id, scan, allow_scheduler=allow_scheduler)
        return GeneralTaskEnvelope.model_validate(raw_input).model_dump(mode="json", exclude_none=True)
    try:
        return validate_capability_input(
            row.capability_id,
            raw_input,
            allow_scheduler=allow_scheduler,
        )
    except TypedInputError as exc:
        raise _raise_input_error(exc) from exc


async def _verify_general_proposal(db, row, parsed, *, _repository_stop_context=None):
    if row.capability_id != "agent.task.v1" or parsed.get("proposal_group") is None:
        return
    from src.work_board.contracts import GeneralTaskEnvelope
    from src.work_board.general_task_proposal import seal_proposal_publication
    await seal_proposal_publication(db, WorkBoardOwner(principal_id=row.owner_principal_id,
        session_id=row.owner_session_id), GeneralTaskEnvelope.model_validate(parsed), goal_revision=row.goal_revision, _repository_stop_context=_repository_stop_context)


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
    await _verify_general_proposal(db, row, parsed)
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


@stage_package_request
async def prepare_input_artifact(
    db: AsyncSession,
    owner: WorkBoardOwner,
    request: WorkBoardInputArtifactCreate,
    *,
    now: datetime | None = None,
    allow_scheduler: bool = False,
    retention_deadline: datetime | None = None,
    publication_population=None,
    general_task_publication=None,
) -> InputArtifactMetadata:
    """Reserve, write, reread, and verify one deterministic input artifact.

    ``retention_deadline`` is a server-only extension for the v2 schedule seed.
    Ordinary callers retain the fixed 24-hour lifetime; the narrow shape check
    below prevents a public task artifact from selecting the longer retention.
    """

    observed_at = _utc(now or _now())
    if request.capability_id == "work.document-compare.v1":
        raise BoardError("document_pair_reservation_required", "Select and stream a private document pair first", status_code=422)
    near_authority = None
    if request.capability_id == "inference.near-text.v1":
        from src.work_board.near_text_native import seal_input_authority
        near_authority = await seal_input_authority(db,owner,request)
    inputs, _payload_hex, payload_digest = await _validate_request(
        db,
        owner,
        request,
        allow_scheduler=allow_scheduler,
        publication_population=publication_population,
        general_task_publication=general_task_publication,
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

    authored_replay=None
    if is_authored(request.capability_id):
        staged_row=await db.get(WorkBoardInputArtifact,artifact_id,populate_existing=True)
        if staged_row is not None and staged_row.metadata_digest is not None and staged_row.state not in {"expired","revoked","deleted"}:
            verified=_safe_file_bytes(_payload_path(staged_row),expected_digest=staged_row.payload_sha256,expected_size=staged_row.size_bytes)
            parsed=_decode_and_validate_payload(staged_row,verified,allow_scheduler=allow_scheduler)
            authored_replay=(tuple(str(getattr(staged_row,column.name)) for column in staged_row.__table__.columns),parsed)

    await _begin_immediate(db)
    if general_task_publication is not None:
        from src.work_board.general_task_proposal import recheck_proposal_publication
        await recheck_proposal_publication(db, owner, general_task_publication)
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
        if is_authored(request.capability_id):
            binding=tuple(str(getattr(existing,column.name)) for column in existing.__table__.columns)
            if authored_replay is None or authored_replay!=(binding,inputs):
                raise BoardError("input_artifact_staged_replay_changed","Retry the exact input request after current physical staging",status_code=409)
            return _metadata(existing)
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
        capability_version=capability_spec(request.capability_id).version,
        idempotency_key=request.idempotency_key,
        payload_sha256=payload_digest,
        typed_input_ref=typed_input_ref,
        size_bytes=len(payload),
        state="pending",
        expires_at=expires_at,
        revision=1,
        document_metadata_json=near_authority,
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


async def _resolve_input_artifact_metadata_for_task(
    db: AsyncSession,
    owner: WorkBoardOwner,
    *,
    artifact_id: str,
    goal_id: str,
    goal_revision: int,
    capability_id: str,
    expected_task_id: str | None = None,
    now: datetime | None = None,
) -> WorkBoardInputArtifact:
    """Original task resolver metadata predicates, with no physical reads."""

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
    expected_version = capability_spec(capability_id)
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
    return row


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

    row = await _resolve_input_artifact_metadata_for_task(db, owner,
        artifact_id=artifact_id, goal_id=goal_id, goal_revision=goal_revision,
        capability_id=capability_id, expected_task_id=expected_task_id, now=now)
    payload = _safe_file_bytes(_payload_path(row), expected_digest=row.payload_sha256, expected_size=row.size_bytes)
    parsed = _decode_and_validate_payload(row, payload)
    await _verify_general_proposal(db, row, parsed)
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
    expected_version = capability_spec(capability_id)
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


async def _verify_general_proposal_staged(db, row, parsed, physical):
    """Original Stop's read-only pre-issuance proposal validation."""
    if row.capability_id != "agent.task.v1" or parsed.get("proposal_group") is None:
        if physical is not None:
            raise BoardError("specialist_handoff_changed", "Original proposal scope changed", status_code=409)
        return
    from src.work_board.contracts import GeneralTaskEnvelope
    from src.work_board.general_task_proposal import _verify_proposal_publication_staged
    await _verify_proposal_publication_staged(db, WorkBoardOwner(principal_id=row.owner_principal_id,
        session_id=row.owner_session_id), GeneralTaskEnvelope.model_validate(parsed),
        goal_revision=row.goal_revision, physical=physical)
