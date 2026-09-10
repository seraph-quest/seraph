"""Bounded local backup/restore lifecycle for the canonical workspace.

This module is deliberately a preparation slice for #742.  It owns no
scheduler or application lifecycle.  A caller supplies the already resolved
workspace registry; this module derives sibling archive/staging directories
from that one root and never treats either sibling as an active workspace.

Archives are versioned ZIP containers.  ``manifest.json`` contains the
registry's redacted inventory and per-member checksums.  Canonical files are
stored under ``payload/``.  Secret/recovery files, derived indexes, caches, and
disposable files are represented as metadata only; secret material is
preserved from the active workspace during a restore and is never serialized.

Restore journals are also bounded, self-checksummed state records.  Recovery
validates their identity, sidecar names, and state before moving a workspace
root; a damaged or unknown journal state fails closed instead of being treated
as an already-completed operation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sqlite3
import stat
import uuid
import zipfile
from typing import Any, Callable

from src.workspace.state_registry import (
    WorkspaceStateError,
    WorkspaceStateClass,
    WorkspaceStateRegistry,
    WorkspaceRootKind,
    canonical_workspace_root,
    _canonical_json,
    _sha256_bytes,
)


ARCHIVE_FORMAT = "seraph.workspace.backup"
ARCHIVE_VERSION = 1
JOURNAL_VERSION = 2
MANIFEST_MEMBER = "manifest.json"
PAYLOAD_PREFIX = "payload/"
SYNTHETIC_MARKER_BYTES = b"seraph-synthetic-workspace-v1\n"
DEFAULT_RETENTION = 3
MAX_ARCHIVE_MEMBER_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 4096
MAX_JOURNAL_BYTES = 1024 * 1024
_SECRET_STATE_CLASSES = frozenset(
    {WorkspaceStateClass.SECRET.value, WorkspaceStateClass.SECRET_RECOVERY.value}
)
_ARCHIVEABLE_STATE_CLASSES = frozenset({WorkspaceStateClass.CANONICAL.value})
_RESTORE_ID_PATTERN = re.compile(r"^restore-[a-z0-9][a-z0-9-]{7,79}$")
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_JOURNAL_STATUSES = frozenset(
    {
        "staged",
        "active_moved",
        "promoted",
        "recovered_promoted",
        "recovered_rollback",
        "staging_discarded",
        "rollback_active_moved",
        "rolled_back",
    }
)
_RECOVERY_IN_FLIGHT_STATUSES = frozenset({"staged", "active_moved", "rollback_active_moved"})
_TERMINAL_JOURNAL_STATUSES = frozenset(
    {"promoted", "recovered_promoted", "recovered_rollback", "staging_discarded", "rolled_back"}
)
_RECEIPT_SECRET_PATTERN = re.compile(
    r"(?:secret|password|credential|api[_-]?key|private[_-]?key|authorization|bearer|cookie|token)",
    re.IGNORECASE,
)
_SAFE_RECEIPT_SECRET_IDENTIFIERS = frozenset({"google_calendar_token.json"})


class WorkspaceLifecycleError(WorkspaceStateError):
    """Base error for an unsafe or incomplete workspace lifecycle operation."""


class InvalidWorkspaceArchiveError(WorkspaceLifecycleError):
    """Raised when a backup archive is corrupt, incomplete, or unsupported."""


class MissingSecretMaterialError(WorkspaceLifecycleError):
    """Raised when a restore cannot preserve required active secret material."""


class InterruptedWorkspaceRestore(WorkspaceLifecycleError):
    """Raised by the testable interruption hook after the active root moved."""


@dataclass(frozen=True)
class _LoadedArchive:
    path: Path
    manifest: dict[str, Any]
    payloads: dict[str, bytes]


def _digest_json(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _refresh_manifest_digest(manifest: dict[str, Any]) -> None:
    """Refresh the self-checksum after changing a manifest body in memory."""
    body = dict(manifest)
    body.pop("manifest_sha256", None)
    manifest["manifest_sha256"] = _digest_json(body)


def _validate_manifest_digest(manifest: dict[str, Any], *, label: str) -> None:
    digest_body = dict(manifest)
    actual_digest = digest_body.pop("manifest_sha256", None)
    if not isinstance(actual_digest, str) or _digest_json(digest_body) != actual_digest:
        raise InvalidWorkspaceArchiveError(f"{label} checksum mismatch")


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _root_name(root: Path) -> str:
    name = root.name
    if not name or name in {".", ".."}:
        raise WorkspaceLifecycleError("workspace root must have a safe directory name")
    return name


def workspace_backup_dir(root: str | os.PathLike[str]) -> Path:
    """Return the derived sibling directory for archives and restore records."""
    resolved = canonical_workspace_root(root)
    return resolved.parent / f"{_root_name(resolved)}.backups"


def workspace_restore_staging_dir(root: str | os.PathLike[str]) -> Path:
    """Return the derived sibling directory for staged restore roots."""
    resolved = canonical_workspace_root(root)
    return resolved.parent / f"{_root_name(resolved)}.restore-staging"


def _registry_root(root: Path, registry: WorkspaceStateRegistry) -> None:
    configured = canonical_workspace_root(registry.config.identity.root)
    if configured != root:
        raise WorkspaceLifecycleError(
            "workspace registry root does not match the active canonical workspace"
        )


def _assert_not_symlink(path: Path, *, label: str) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise WorkspaceLifecycleError(f"{label} is not readable") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise WorkspaceLifecycleError(f"{label} must not be a symlink")


def _assert_no_symlink_components(path: Path, *, label: str) -> None:
    """Reject a path whose existing lexical components include a symlink.

    ``Path.resolve`` follows links, so checking only its result would allow an
    archive path to escape through a linked parent or linked destination.  Walk
    the lexical path first and stop at the first missing component; later
    components cannot already contain a symlink when that ancestor is absent.
    """
    candidate = path if path.is_absolute() else path.absolute()
    current = Path(candidate.anchor)
    for component in candidate.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise WorkspaceLifecycleError(f"{label} is not readable") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise WorkspaceLifecycleError(f"{label} must not contain symlink components")


def _path_present(path: Path) -> bool:
    """Return lstat-style existence, including dangling symlinks."""
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise WorkspaceLifecycleError(f"workspace path is not readable: {path.name}") from exc
    return True


def _ensure_directory(path: Path, *, label: str) -> None:
    _assert_not_symlink(path, label=label)
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise WorkspaceLifecycleError(f"{label} cannot be created") from exc
    _assert_not_symlink(path, label=label)
    try:
        if not path.is_dir():
            raise WorkspaceLifecycleError(f"{label} must be a directory")
    except OSError as exc:
        raise WorkspaceLifecycleError(f"{label} is not readable") from exc


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as exc:
        raise WorkspaceLifecycleError(f"cannot sync workspace sidecar: {path.name}") from exc
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_file(path: Path) -> None:
    try:
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
    except OSError as exc:
        raise WorkspaceLifecycleError(f"cannot sync workspace file: {path.name}") from exc


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    _ensure_directory(path.parent, label="workspace journal directory")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    encoded = _canonical_json(payload).encode("utf-8")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise WorkspaceLifecycleError(f"cannot atomically write {path.name}") from exc


def _refresh_journal_digest(journal: dict[str, Any]) -> None:
    """Refresh the journal's corruption-detection checksum in place."""
    body = dict(journal)
    body.pop("journal_sha256", None)
    journal["journal_sha256"] = _digest_json(body)


def _workspace_root_digest(root: Path) -> str:
    """Return a non-reversible binding for the canonical lexical root."""
    return hashlib.sha256(str(root).encode("utf-8")).hexdigest()


def _bounded_identifier_list(value: object, *, label: str) -> None:
    """Allow only short logical identifiers in durable operator receipts."""
    if not isinstance(value, list) or len(value) > MAX_ARCHIVE_MEMBERS:
        raise WorkspaceLifecycleError(f"{label} must be a bounded identifier list")
    for identifier in value:
        if not isinstance(identifier, str) or not identifier or len(identifier) > 256:
            raise WorkspaceLifecycleError(f"{label} contains an invalid identifier")
        if identifier in _SAFE_RECEIPT_SECRET_IDENTIFIERS:
            continue
        if any(ord(character) < 32 or ord(character) == 127 for character in identifier):
            raise WorkspaceLifecycleError(f"{label} contains a control character")
        if _RECEIPT_SECRET_PATTERN.search(identifier):
            raise WorkspaceLifecycleError(f"{label} contains a secret-like identifier")
        if "\\" in identifier:
            raise WorkspaceLifecycleError(f"{label} contains an unsafe identifier")
        normalized = identifier.replace("\\", "/")
        parts = PurePosixPath(normalized).parts
        if (
            normalized.startswith("/")
            or not parts
            or any(part in {"", ".", ".."} for part in parts)
            or normalized != "/".join(parts)
        ):
            raise WorkspaceLifecycleError(f"{label} contains an unsafe identifier")


def _bounded_receipt_count(value: object, *, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_ARCHIVE_MEMBERS:
        raise WorkspaceLifecycleError(f"{label} is outside the bounded receipt range")


def _validate_stage_receipt(receipt: object) -> dict[str, Any]:
    """Validate the metadata-only receipt shape persisted in a journal.

    A strict allowlist prevents a reconciliation hook from accidentally
    persisting arbitrary provider output, credential material, or free-form
    model text in the recovery record.
    """
    if not isinstance(receipt, dict):
        raise WorkspaceLifecycleError("restore journal stage receipt is missing")
    required = {
        "canonical_entry_count",
        "secret_entry_count",
        "derived_entries_rebuilt",
        "database_verified",
    }
    allowed = required | {"restore_reconciliation"}
    if set(receipt) - allowed or not required.issubset(receipt):
        raise WorkspaceLifecycleError("restore journal stage receipt contains unsupported fields")
    _bounded_receipt_count(receipt["canonical_entry_count"], label="canonical entry count")
    _bounded_receipt_count(receipt["secret_entry_count"], label="secret entry count")
    for field in ("derived_entries_rebuilt", "database_verified"):
        if not isinstance(receipt[field], bool):
            raise WorkspaceLifecycleError(f"restore journal {field} receipt is invalid")

    reconciliation = receipt.get("restore_reconciliation")
    if reconciliation is None:
        return receipt
    if not isinstance(reconciliation, dict):
        raise WorkspaceLifecycleError("restore reconciliation receipt is invalid")
    reconciliation_required = {
        "status",
        "derived_rebuild",
        "authority_invalidation",
        "token_invalidation",
        "secret_values_included",
    }
    if set(reconciliation) != reconciliation_required:
        raise WorkspaceLifecycleError("restore reconciliation receipt contains unsupported fields")
    if reconciliation["status"] != "ready" or reconciliation["secret_values_included"] is not False:
        raise WorkspaceLifecycleError("restore reconciliation receipt is not secret-safe")

    derived = reconciliation["derived_rebuild"]
    if not isinstance(derived, dict) or set(derived) != {
        "status",
        "rebuilt_directories",
        "stored_derived_files",
    }:
        raise WorkspaceLifecycleError("derived rebuild receipt is invalid")
    if derived["status"] != "clean_targets_recreated":
        raise WorkspaceLifecycleError("derived rebuild receipt is not ready")
    _bounded_identifier_list(derived["rebuilt_directories"], label="rebuilt directory list")
    _bounded_receipt_count(derived["stored_derived_files"], label="stored derived file count")

    authority = reconciliation["authority_invalidation"]
    if not isinstance(authority, dict) or set(authority) != {
        "status",
        "tables_present",
        "operator_sessions_invalidated",
        "workflow_authority_rows_blocked",
    }:
        raise WorkspaceLifecycleError("authority invalidation receipt is invalid")
    if authority["status"] != "applied":
        raise WorkspaceLifecycleError("authority invalidation receipt is not applied")
    _bounded_identifier_list(authority["tables_present"], label="authority table list")
    _bounded_receipt_count(
        authority["operator_sessions_invalidated"],
        label="invalidated session count",
    )
    _bounded_receipt_count(
        authority["workflow_authority_rows_blocked"],
        label="blocked authority count",
    )

    tokens = reconciliation["token_invalidation"]
    if not isinstance(tokens, dict) or set(tokens) != {
        "status",
        "optional_credentials_invalidated",
    }:
        raise WorkspaceLifecycleError("token invalidation receipt is invalid")
    if tokens["status"] != "applied":
        raise WorkspaceLifecycleError("token invalidation receipt is not applied")
    _bounded_identifier_list(
        tokens["optional_credentials_invalidated"],
        label="invalidated credential list",
    )
    return receipt


def _validate_journal(
    journal: object,
    *,
    expected_restore_id: str | None = None,
    expected_workspace_id: str | None = None,
    expected_root_digest: str | None = None,
) -> dict[str, Any]:
    """Validate journal identity and shape before recovery can move roots."""
    if not isinstance(journal, dict):
        raise WorkspaceLifecycleError("restore journal must contain an object")
    if journal.get("journal_version") != JOURNAL_VERSION:
        raise WorkspaceLifecycleError("unsupported restore journal version")

    restore_id = journal.get("restore_id")
    if not isinstance(restore_id, str) or not _RESTORE_ID_PATTERN.fullmatch(restore_id):
        raise WorkspaceLifecycleError("restore journal has an invalid restore_id")
    if expected_restore_id is not None and restore_id != expected_restore_id:
        raise WorkspaceLifecycleError("restore journal identity does not match its record")

    status = journal.get("status")
    if not isinstance(status, str) or status not in _JOURNAL_STATUSES:
        raise WorkspaceLifecycleError("restore journal has an unknown state")

    workspace_id = journal.get("workspace_id")
    if not isinstance(workspace_id, str) or not workspace_id or len(workspace_id) > 128:
        raise WorkspaceLifecycleError("restore journal has an invalid workspace identity")
    if expected_workspace_id is not None and workspace_id != expected_workspace_id:
        raise WorkspaceLifecycleError("restore journal workspace identity does not match target")
    root_digest = journal.get("workspace_root_digest")
    if not isinstance(root_digest, str) or not _DIGEST_PATTERN.fullmatch(root_digest):
        raise WorkspaceLifecycleError("restore journal root identity is invalid")
    if expected_root_digest is not None and root_digest != expected_root_digest:
        raise WorkspaceLifecycleError("restore journal root identity does not match target")
    if journal.get("previous_name") != f"{restore_id}/previous-workspace":
        raise WorkspaceLifecycleError("restore journal previous-root binding is invalid")
    if journal.get("stage_name") != restore_id:
        raise WorkspaceLifecycleError("restore journal staging-root binding is invalid")
    created_at = journal.get("created_at")
    if not isinstance(created_at, str) or not created_at:
        raise WorkspaceLifecycleError("restore journal creation timestamp is missing")
    try:
        parsed_created_at = datetime.fromisoformat(created_at)
    except ValueError as exc:
        raise WorkspaceLifecycleError("restore journal creation timestamp is invalid") from exc
    if parsed_created_at.tzinfo is None:
        raise WorkspaceLifecycleError("restore journal creation timestamp must include timezone")
    archive_digest = journal.get("archive_manifest_sha256")
    if not isinstance(archive_digest, str) or not _DIGEST_PATTERN.fullmatch(archive_digest):
        raise WorkspaceLifecycleError("restore journal archive identity is invalid")
    _validate_stage_receipt(journal.get("stage_receipt"))

    actual_digest = journal.get("journal_sha256")
    digest_body = dict(journal)
    digest_body.pop("journal_sha256", None)
    if not isinstance(actual_digest, str) or not _DIGEST_PATTERN.fullmatch(actual_digest):
        raise WorkspaceLifecycleError("restore journal checksum is missing")
    if _digest_json(digest_body) != actual_digest:
        raise WorkspaceLifecycleError("restore journal checksum mismatch")
    return journal


def _write_journal(path: Path, journal: dict[str, Any]) -> None:
    """Write a validated, self-checksummed journal and update its caller."""
    candidate = dict(journal)
    _refresh_journal_digest(candidate)
    _validate_journal(candidate, expected_restore_id=candidate.get("restore_id"))
    encoded = _canonical_json(candidate).encode("utf-8")
    if len(encoded) > MAX_JOURNAL_BYTES:
        raise WorkspaceLifecycleError("restore journal exceeds bounded size")
    _atomic_json_write(path, candidate)
    journal.clear()
    journal.update(candidate)


def _safe_logical_path(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidWorkspaceArchiveError(f"{field} must be a non-empty logical path")
    normalized = value.replace("\\", "/")
    parts = PurePosixPath(normalized).parts
    if (
        not parts
        or normalized.startswith("/")
        or any(part in {"", ".", ".."} for part in parts)
        or normalized != "/".join(parts)
    ):
        raise InvalidWorkspaceArchiveError(f"{field} contains unsafe path traversal")
    return normalized


def _safe_restore_id(restore_id: str | None) -> str:
    value = restore_id or f"restore-{uuid.uuid4().hex}"
    if not isinstance(value, str) or not _RESTORE_ID_PATTERN.fullmatch(value):
        raise WorkspaceLifecycleError("restore_id must be a bounded restore-* slug")
    return value


def _safe_entry_path(root: Path, logical_path: str, registry: WorkspaceStateRegistry) -> Path:
    normalized = _safe_logical_path(logical_path, field="workspace path")
    registry.classify_path(normalized)
    candidate = root.joinpath(*PurePosixPath(normalized).parts)
    current = root
    for component in PurePosixPath(normalized).parts:
        current /= component
        _assert_not_symlink(current, label=f"workspace path {normalized}")
    try:
        resolved_root = root.resolve(strict=False)
        candidate.resolve(strict=False).relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise WorkspaceLifecycleError(f"workspace path escapes root: {normalized}") from exc
    return candidate


def _regular_file_bytes(
    path: Path,
    *,
    label: str,
    max_bytes: int | None = None,
) -> bytes:
    _assert_not_symlink(path, label=label)
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
        )
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            os.close(descriptor)
            raise WorkspaceLifecycleError(f"{label} must be a regular file")
        with os.fdopen(descriptor, "rb") as handle:
            if max_bytes is not None and metadata.st_size > max_bytes:
                raise WorkspaceLifecycleError(f"{label} exceeds bounded size")
            payload = handle.read(max_bytes + 1 if max_bytes is not None else -1)
            if max_bytes is not None and len(payload) > max_bytes:
                raise WorkspaceLifecycleError(f"{label} exceeds bounded size")
            return payload
    except OSError as exc:
        raise WorkspaceLifecycleError(f"{label} is not readable") from exc


def _manifest_entries(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw_entries = manifest.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise InvalidWorkspaceArchiveError("workspace manifest entries are missing")
    entries: dict[str, dict[str, Any]] = {}
    for raw in raw_entries:
        if not isinstance(raw, dict):
            raise InvalidWorkspaceArchiveError("workspace manifest entry is not an object")
        logical_path = _safe_logical_path(raw.get("logical_path"), field="manifest logical_path")
        if logical_path in entries:
            raise InvalidWorkspaceArchiveError(f"duplicate manifest path: {logical_path}")
        if "required" in raw and not isinstance(raw["required"], bool):
            raise InvalidWorkspaceArchiveError(f"manifest requiredness is invalid: {logical_path}")
        entries[logical_path] = raw
    return entries


def _archive_manifest(
    source_manifest: dict[str, Any],
    registry: WorkspaceStateRegistry,
) -> dict[str, Any]:
    source_entries = _manifest_entries(source_manifest)
    archive_entries: list[dict[str, Any]] = []
    for logical_path in sorted(source_entries):
        source_entry = source_entries[logical_path]
        try:
            state_class = registry.classify_path(logical_path).value
        except WorkspaceStateError as exc:
            raise WorkspaceLifecycleError(f"manifest path is not registry-owned: {logical_path}") from exc
        if source_entry.get("state_class") != state_class:
            raise WorkspaceLifecycleError(f"manifest state classification drift: {logical_path}")
        file_type = source_entry.get("file_type")
        if state_class in _SECRET_STATE_CLASSES and file_type != "file":
            raise WorkspaceLifecycleError(f"secret material must be a regular file: {logical_path}")
        archived = bool(
            file_type in {"file", "sqlite"}
            and state_class in _ARCHIVEABLE_STATE_CLASSES
        )
        if state_class in _SECRET_STATE_CLASSES and archived:
            raise WorkspaceLifecycleError(f"secret material cannot be archived: {logical_path}")
        archive_entries.append(
            {
                "logical_path": logical_path,
                "state_class": state_class,
                "file_type": str(file_type or ""),
                "mode": int(source_entry.get("mode", 0)),
                "size_bytes": int(source_entry.get("size_bytes", 0)),
                "sha256": str(source_entry.get("sha256", "")),
                "digest_scope": str(source_entry.get("digest_scope", "")),
                "required": bool(source_entry.get("required", True)),
                # Registry SQLite digests intentionally cover only redacted
                # metadata.  The archive needs an independent payload digest
                # so its bytes can still be checked without exposing them in
                # the source inventory contract.
                "payload_sha256": None,
                "archived": archived,
                "excluded_reason": None if archived else (
                    "secret_metadata_only"
                    if state_class in _SECRET_STATE_CLASSES
                    else "rebuild_or_disposable"
                ),
            }
        )
    body = {
        "archive_format": ARCHIVE_FORMAT,
        "archive_version": ARCHIVE_VERSION,
        "workspace_id": source_manifest.get("workspace_id"),
        "workspace_version": source_manifest.get("workspace_version"),
        "source_manifest_sha256": source_manifest.get("manifest_sha256"),
        "workspace_manifest": source_manifest,
        "archive_entries": archive_entries,
    }
    body["manifest_sha256"] = _digest_json(body)
    return body


def _archive_path(root: Path, archive_path: str | os.PathLike[str] | None) -> Path:
    backup_root = workspace_backup_dir(root)
    if archive_path is None:
        return backup_root / f"backup-{uuid.uuid4().hex}.seraph.zip"
    candidate = Path(archive_path).expanduser()
    if not candidate.is_absolute():
        candidate = backup_root / candidate
    # Keep the lexical path for the eventual write.  Resolving first would
    # follow a symlinked destination/parent and make the safety check observe a
    # different path than the one opened by ZipFile.
    candidate = candidate.absolute()
    _assert_no_symlink_components(candidate, label="backup archive path")
    resolved_candidate = candidate.resolve(strict=False)
    if resolved_candidate == root or root in resolved_candidate.parents:
        raise WorkspaceLifecycleError("backup archive must not be stored inside active workspace")
    _assert_not_symlink(candidate.parent, label="backup archive parent")
    return candidate


def _zip_info(name: str, mode: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | (mode & 0o7777)) << 16
    return info


def backup_workspace(
    root: str | os.PathLike[str],
    *,
    registry: WorkspaceStateRegistry,
    archive_path: str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """Create an atomic, versioned archive from the registry inventory."""
    resolved_root = canonical_workspace_root(root)
    _registry_root(resolved_root, registry)
    source_manifest = registry.build_manifest()
    archive_manifest = _archive_manifest(source_manifest, registry)
    destination = _archive_path(resolved_root, archive_path)
    _ensure_directory(destination.parent, label="backup archive directory")
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    source_entries = _manifest_entries(source_manifest)
    archive_entries = {
        item["logical_path"]: item
        for item in archive_manifest["archive_entries"]
        if item["archived"]
    }
    payloads: dict[str, bytes] = {}
    try:
        for logical_path in sorted(archive_entries):
            entry = archive_entries[logical_path]
            source_path = _safe_entry_path(resolved_root, logical_path, registry)
            payload = _regular_file_bytes(source_path, label=f"workspace file {logical_path}")
            expected = source_entries[logical_path]
            if len(payload) != int(expected.get("size_bytes", -1)):
                raise WorkspaceLifecycleError(f"workspace changed during backup: {logical_path}")
            digest_scope = expected.get("digest_scope")
            if digest_scope == "content":
                if _sha256_bytes(payload) != expected.get("sha256"):
                    raise WorkspaceLifecycleError(f"workspace changed during backup: {logical_path}")
            elif digest_scope == "redacted_metadata" and logical_path == registry.config.database_path:
                expected_database = source_manifest.get("database")
                if isinstance(expected_database, dict):
                    actual_database = registry._sqlite_inventory(source_path)  # type: ignore[attr-defined]
                    for field in ("schema_fingerprint", "table_count", "row_count", "schema_object_count"):
                        if actual_database.get(field) != expected_database.get(field):
                            raise WorkspaceLifecycleError(f"workspace changed during backup: {logical_path}")
            elif digest_scope != "redacted_metadata":
                raise WorkspaceLifecycleError(f"unsupported workspace digest scope: {logical_path}")
            entry["payload_sha256"] = _sha256_bytes(payload)
            payloads[logical_path] = payload
        _refresh_manifest_digest(archive_manifest)
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr(_zip_info(MANIFEST_MEMBER, 0o600), _canonical_json(archive_manifest).encode("utf-8"))
            for logical_path in sorted(archive_entries):
                entry = archive_entries[logical_path]
                archive.writestr(
                    _zip_info(f"{PAYLOAD_PREFIX}{logical_path}", int(entry["mode"])),
                    payloads[logical_path],
                )
        _fsync_file(temporary)
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    except WorkspaceLifecycleError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        if isinstance(exc, WorkspaceLifecycleError):
            raise
        raise WorkspaceLifecycleError("workspace backup archive could not be written") from exc
    return {
        "status": "created",
        "archive_format": ARCHIVE_FORMAT,
        "archive_version": ARCHIVE_VERSION,
        "archive_path": str(destination),
        "archive_manifest_sha256": archive_manifest["manifest_sha256"],
        "source_manifest_sha256": source_manifest["manifest_sha256"],
        "member_count": len(archive_entries),
        "size_bytes": destination.stat().st_size,
        "secret_values_included": False,
        "derived_state_included": False,
    }


def _read_json_member(archive: zipfile.ZipFile, name: str) -> dict[str, Any]:
    try:
        info = archive.getinfo(name)
        if info.file_size < 0 or info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
            raise InvalidWorkspaceArchiveError("archive member exceeds bounded size")
        raw = archive.read(info)
        value = json.loads(raw.decode("utf-8"))
    except InvalidWorkspaceArchiveError:
        raise
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
        raise InvalidWorkspaceArchiveError(f"archive {name} is missing or invalid") from exc
    if not isinstance(value, dict):
        raise InvalidWorkspaceArchiveError(f"archive {name} must contain an object")
    return value


def _validate_zip_member(info: zipfile.ZipInfo, *, expected_prefix: str | None = None) -> str:
    name = info.filename
    if not isinstance(name, str) or not name or "\\" in name:
        raise InvalidWorkspaceArchiveError("archive member has an unsafe name")
    if info.is_dir() or name.startswith("/"):
        raise InvalidWorkspaceArchiveError("archive directories are not supported as members")
    mode = (info.external_attr >> 16) & 0o170000
    if mode == stat.S_IFLNK:
        raise InvalidWorkspaceArchiveError("archive symlink members are not allowed")
    if expected_prefix is not None and not name.startswith(expected_prefix):
        raise InvalidWorkspaceArchiveError("archive contains an unexpected member")
    return name


def _load_archive(
    archive_path: Path,
    registry: WorkspaceStateRegistry,
) -> _LoadedArchive:
    try:
        _assert_no_symlink_components(archive_path, label="workspace backup archive path")
        _assert_not_symlink(archive_path, label="workspace backup archive")
    except WorkspaceLifecycleError as exc:
        raise InvalidWorkspaceArchiveError("workspace backup archive must not be a symlink") from exc
    try:
        archive_metadata = archive_path.stat()
    except OSError as exc:
        raise InvalidWorkspaceArchiveError("workspace backup archive is missing") from exc
    if not archive_path.is_file() or not stat.S_ISREG(archive_metadata.st_mode):
        raise InvalidWorkspaceArchiveError("workspace backup archive is missing")
    if archive_metadata.st_size > MAX_ARCHIVE_BYTES:
        raise InvalidWorkspaceArchiveError("workspace backup archive exceeds bounded size")
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_MEMBERS:
                raise InvalidWorkspaceArchiveError("workspace backup archive has too many members")
            names = [_validate_zip_member(info) for info in infos]
            if len(names) != len(set(names)):
                raise InvalidWorkspaceArchiveError("archive contains duplicate members")
            if names.count(MANIFEST_MEMBER) != 1:
                raise InvalidWorkspaceArchiveError("archive must contain exactly one manifest.json")
            manifest = _read_json_member(archive, MANIFEST_MEMBER)
            digest_body = dict(manifest)
            actual_digest = digest_body.pop("manifest_sha256", None)
            if not isinstance(actual_digest, str) or _digest_json(digest_body) != actual_digest:
                raise InvalidWorkspaceArchiveError("archive manifest checksum mismatch")
            if manifest.get("archive_format") != ARCHIVE_FORMAT or manifest.get("archive_version") != ARCHIVE_VERSION:
                raise InvalidWorkspaceArchiveError("unsupported workspace archive version")
            source_manifest = manifest.get("workspace_manifest")
            if not isinstance(source_manifest, dict):
                raise InvalidWorkspaceArchiveError("archive workspace manifest is missing")
            _validate_manifest_digest(source_manifest, label="source workspace manifest")
            if source_manifest.get("manifest_sha256") != manifest.get("source_manifest_sha256"):
                raise InvalidWorkspaceArchiveError("source workspace manifest checksum mismatch")
            if (
                manifest.get("workspace_id") != source_manifest.get("workspace_id")
                or manifest.get("workspace_version") != source_manifest.get("workspace_version")
            ):
                raise InvalidWorkspaceArchiveError("archive workspace identity metadata mismatch")
            source_entries = _manifest_entries(source_manifest)
            archive_entries = manifest.get("archive_entries")
            if not isinstance(archive_entries, list):
                raise InvalidWorkspaceArchiveError("archive entry index is missing")
            indexed: dict[str, dict[str, Any]] = {}
            for raw in archive_entries:
                if not isinstance(raw, dict):
                    raise InvalidWorkspaceArchiveError("archive entry index is invalid")
                logical_path = _safe_logical_path(raw.get("logical_path"), field="archive logical_path")
                if logical_path in indexed or logical_path not in source_entries:
                    raise InvalidWorkspaceArchiveError(f"archive entry is duplicate or unknown: {logical_path}")
                indexed[logical_path] = raw
                if not isinstance(raw.get("archived"), bool):
                    raise InvalidWorkspaceArchiveError(f"archive entry flag is invalid: {logical_path}")
                source_entry = source_entries[logical_path]
                try:
                    path_spec = registry.path_spec(logical_path)
                    state_class = path_spec.state_class.value
                except WorkspaceStateError as exc:
                    raise InvalidWorkspaceArchiveError(
                        f"archive path is not registry-owned: {logical_path}"
                    ) from exc
                if raw.get("state_class") != state_class or source_entry.get("state_class") != state_class:
                    raise InvalidWorkspaceArchiveError(f"archive state classification drift: {logical_path}")
                if state_class in _SECRET_STATE_CLASSES and source_entry.get("file_type") != "file":
                    raise InvalidWorkspaceArchiveError(f"secret material must be a regular file: {logical_path}")
                expected_archived = bool(
                    source_entry.get("file_type") in {"file", "sqlite"}
                    and state_class in _ARCHIVEABLE_STATE_CLASSES
                )
                if raw.get("archived") != expected_archived:
                    raise InvalidWorkspaceArchiveError(f"archive inclusion drift: {logical_path}")
                for field in ("file_type", "mode", "size_bytes", "sha256", "digest_scope"):
                    if raw.get(field) != source_entry.get(field):
                        raise InvalidWorkspaceArchiveError(f"archive metadata drift: {logical_path}")
                source_has_required = "required" in source_entry
                raw_has_required = "required" in raw
                source_required = source_entry.get("required", path_spec.required)
                raw_required = raw.get("required", path_spec.required)
                if source_has_required and not isinstance(source_required, bool):
                    raise InvalidWorkspaceArchiveError(f"source requiredness is invalid: {logical_path}")
                if raw_has_required and not isinstance(raw_required, bool):
                    raise InvalidWorkspaceArchiveError(f"archive requiredness is invalid: {logical_path}")
                if source_has_required != raw_has_required:
                    raise InvalidWorkspaceArchiveError(f"archive requiredness drift: {logical_path}")
                if source_required != raw_required or source_required != path_spec.required:
                    raise InvalidWorkspaceArchiveError(f"current requiredness drift: {logical_path}")
                if raw.get("archived") and not isinstance(raw.get("payload_sha256"), str):
                    raise InvalidWorkspaceArchiveError(f"archive payload digest is missing: {logical_path}")
                if not raw.get("archived") and raw.get("payload_sha256") is not None:
                    raise InvalidWorkspaceArchiveError(f"excluded archive entry has payload: {logical_path}")
                if state_class in _SECRET_STATE_CLASSES and raw.get("archived"):
                    raise InvalidWorkspaceArchiveError("archive contains secret material")
            if set(indexed) != set(source_entries):
                raise InvalidWorkspaceArchiveError("archive entry index does not cover the source manifest")
            current_required_secrets = {
                spec.logical_path
                for spec in registry.config.declared_paths
                if spec.required and spec.state_class in _SECRET_STATE_CLASSES
            }
            missing_required_secrets = sorted(current_required_secrets - set(source_entries))
            if missing_required_secrets:
                raise InvalidWorkspaceArchiveError(
                    "archive is missing required secret material metadata: "
                    + ", ".join(missing_required_secrets)
                )
            payloads: dict[str, bytes] = {}
            expected_payload_members = {
                f"{PAYLOAD_PREFIX}{logical_path}"
                for logical_path, entry in indexed.items()
                if entry.get("archived")
            }
            actual_payload_members: set[str] = set()
            for name in names:
                if name.startswith(PAYLOAD_PREFIX):
                    _safe_logical_path(name[len(PAYLOAD_PREFIX):], field="archive payload path")
                    actual_payload_members.add(name)
            if actual_payload_members != expected_payload_members:
                raise InvalidWorkspaceArchiveError("archive payload members do not match manifest")
            for info in infos:
                name = info.filename
                if name == MANIFEST_MEMBER:
                    continue
                _validate_zip_member(info, expected_prefix=PAYLOAD_PREFIX)
                logical_path = name[len(PAYLOAD_PREFIX):]
                logical_path = _safe_logical_path(logical_path, field="archive payload path")
                if info.file_size < 0 or info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                    raise InvalidWorkspaceArchiveError("archive member exceeds bounded size")
                payload = archive.read(info)
                if len(payload) > MAX_ARCHIVE_MEMBER_BYTES:
                    raise InvalidWorkspaceArchiveError("archive member exceeds bounded size")
                entry = indexed[logical_path]
                payload_digest = entry.get("payload_sha256")
                if not isinstance(payload_digest, str):
                    raise InvalidWorkspaceArchiveError(f"archive payload digest is missing: {logical_path}")
                if len(payload) != int(entry.get("size_bytes", -1)) or _sha256_bytes(payload) != payload_digest:
                    raise InvalidWorkspaceArchiveError(f"archive payload checksum mismatch: {logical_path}")
                if entry.get("digest_scope") == "content" and _sha256_bytes(payload) != entry.get("sha256"):
                    raise InvalidWorkspaceArchiveError(f"archive payload checksum mismatch: {logical_path}")
                payloads[logical_path] = payload
            return _LoadedArchive(archive_path, manifest, payloads)
    except InvalidWorkspaceArchiveError:
        raise
    except (OSError, TypeError, ValueError, OverflowError, zipfile.BadZipFile, KeyError) as exc:
        raise InvalidWorkspaceArchiveError("workspace backup archive is corrupt or unreadable") from exc


def _write_regular(path: Path, payload: bytes, mode: int) -> None:
    _assert_not_symlink(path, label=f"staged workspace path {path.name}")
    _ensure_directory(path.parent, label="restore staging directory")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            mode & 0o7777 or 0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode & 0o7777 or 0o600)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except OSError as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise WorkspaceLifecycleError(f"cannot materialize staged file {path.name}") from exc


def _copy_secret(source: Path, destination: Path, mode: int) -> None:
    payload = _regular_file_bytes(source, label="active secret material")
    _write_regular(destination, payload, mode)


def _remove_tree(path: Path, *, label: str) -> None:
    _assert_not_symlink(path, label=label)
    if not path.exists():
        return
    if not path.is_dir():
        raise WorkspaceLifecycleError(f"{label} must be a directory")
    try:
        shutil.rmtree(path)
    except OSError as exc:
        raise WorkspaceLifecycleError(f"cannot remove {label}") from exc


def _is_empty_directory(path: Path, *, label: str) -> bool:
    """Return whether an existing sidecar is an empty, safe directory."""
    if not _path_present(path):
        return True
    _assert_no_symlink_components(path, label=label)
    _assert_not_symlink(path, label=label)
    if not path.is_dir():
        raise WorkspaceLifecycleError(f"{label} must be a directory")
    try:
        next(path.iterdir())
    except StopIteration:
        return True
    except OSError as exc:
        raise WorkspaceLifecycleError(f"{label} is not readable") from exc
    return False


def _materialize_stage(
    *,
    root: Path,
    stage: Path,
    loaded: _LoadedArchive,
    registry: WorkspaceStateRegistry,
) -> dict[str, Any]:
    source_manifest = loaded.manifest["workspace_manifest"]
    entries = _manifest_entries(source_manifest)
    _ensure_directory(stage, label="restore staging root")
    for logical_path in sorted(entries):
        entry = entries[logical_path]
        state_class = str(entry.get("state_class"))
        file_type = str(entry.get("file_type"))
        if state_class in _SECRET_STATE_CLASSES and file_type != "file":
            raise InvalidWorkspaceArchiveError(f"secret material must be a regular file: {logical_path}")
        target = _safe_entry_path(stage, logical_path, registry)
        if file_type == "directory":
            _ensure_directory(target, label=f"staged directory {logical_path}")
            try:
                os.chmod(target, int(entry.get("mode", 0)) & 0o7777)
            except OSError as exc:
                raise WorkspaceLifecycleError(f"cannot set staged directory mode: {logical_path}") from exc
            continue
        if state_class in _ARCHIVEABLE_STATE_CLASSES:
            payload = loaded.payloads.get(logical_path)
            if payload is None:
                raise InvalidWorkspaceArchiveError(f"canonical payload is missing: {logical_path}")
            _write_regular(target, payload, int(entry.get("mode", 0)))
            continue
        if state_class in _SECRET_STATE_CLASSES:
            source = _safe_entry_path(root, logical_path, registry)
            if not source.is_file() or source.is_symlink():
                if not registry.path_spec(logical_path).required:
                    continue
                raise MissingSecretMaterialError(
                    f"active secret material is missing: {logical_path}"
                )
            _copy_secret(source, target, int(source.stat().st_mode) & 0o7777)
            continue
        # Derived/cache/disposable regular files are intentionally omitted.

    marker = registry.config.identity.synthetic_marker
    if registry.config.identity.root_kind is WorkspaceRootKind.SYNTHETIC_FIXTURE:
        marker_path = _safe_entry_path(stage, marker, registry)
        marker_mode = int(entries.get(marker, {}).get("mode", 0o600)) & 0o7777
        _write_regular(marker_path, SYNTHETIC_MARKER_BYTES, marker_mode)
    for current, _dirs, _files in os.walk(stage, topdown=False, followlinks=False):
        _fsync_directory(Path(current))
    return _validate_stage(stage, loaded, registry)


def _sqlite_receipt(path: Path, registry: WorkspaceStateRegistry) -> dict[str, Any]:
    try:
        return registry._sqlite_inventory(path)  # type: ignore[attr-defined]
    except (OSError, sqlite3.Error, WorkspaceStateError) as exc:
        raise WorkspaceLifecycleError("staged SQLite database is not readable") from exc


def _validate_stage(
    stage: Path,
    loaded: _LoadedArchive,
    registry: WorkspaceStateRegistry,
    *,
    allow_database_mutation: bool = False,
) -> dict[str, Any]:
    source_manifest = loaded.manifest["workspace_manifest"]
    entries = _manifest_entries(source_manifest)
    archive_entries = {
        item["logical_path"]: item
        for item in loaded.manifest.get("archive_entries", [])
        if isinstance(item, dict) and isinstance(item.get("logical_path"), str)
    }
    expected_paths = {
        logical_path
        for logical_path, entry in entries.items()
        if entry.get("file_type") == "directory"
        or entry.get("state_class") in _ARCHIVEABLE_STATE_CLASSES
        or entry.get("state_class") in _SECRET_STATE_CLASSES
    }
    required_paths = {
        logical_path
        for logical_path, entry in entries.items()
        if entry.get("file_type") == "directory"
            or entry.get("state_class") in _ARCHIVEABLE_STATE_CLASSES
            or (
                entry.get("state_class") in _SECRET_STATE_CLASSES
                and registry.path_spec(logical_path).required
            )
    }
    actual_paths: set[str] = set()
    for current, dirs, files in os.walk(stage, topdown=True, followlinks=False):
        current_path = Path(current)
        for name in list(dirs) + list(files):
            candidate = current_path / name
            _assert_not_symlink(candidate, label="staged workspace entry")
            logical_path = candidate.relative_to(stage).as_posix()
            registry.classify_path(logical_path)
            actual_paths.add(logical_path)
        dirs.sort()
        files.sort()
    allowed_extra = {registry.config.identity.synthetic_marker}
    if not actual_paths.issubset(expected_paths | allowed_extra):
        unknown = sorted(actual_paths - expected_paths - allowed_extra)
        raise WorkspaceLifecycleError("staged workspace contains unknown entries: " + ", ".join(unknown))
    missing = sorted(required_paths - actual_paths)
    if missing:
        raise WorkspaceLifecycleError("staged workspace is missing required entries: " + ", ".join(missing))
    for logical_path in sorted(expected_paths):
        entry = entries[logical_path]
        target = _safe_entry_path(stage, logical_path, registry)
        if entry.get("file_type") == "directory":
            if not target.is_dir():
                raise WorkspaceLifecycleError(f"staged path is not a directory: {logical_path}")
            if stat.S_IMODE(target.stat().st_mode) != (int(entry.get("mode", 0)) & 0o7777):
                raise WorkspaceLifecycleError(f"staged directory permissions mismatch: {logical_path}")
            continue
        if entry.get("state_class") in _SECRET_STATE_CLASSES:
            if not target.is_file() or target.is_symlink():
                if not registry.path_spec(logical_path).required:
                    continue
                raise MissingSecretMaterialError(f"staged secret material is unavailable: {logical_path}")
            continue
        payload = _regular_file_bytes(target, label=f"staged workspace file {logical_path}")
        if allow_database_mutation and logical_path == registry.config.database_path:
            # Restore reconciliation may revoke sessions and block authority
            # rows.  The schema and row counts remain covered below; payload
            # bytes are intentionally allowed to differ from the archive.
            continue
        archive_entry = archive_entries.get(logical_path)
        if not isinstance(archive_entry, dict):
            raise WorkspaceLifecycleError(f"staged canonical archive metadata is missing: {logical_path}")
        payload_digest = archive_entry.get("payload_sha256")
        if (
            len(payload) != int(entry.get("size_bytes", -1))
            or not isinstance(payload_digest, str)
            or _sha256_bytes(payload) != payload_digest
        ):
            raise WorkspaceLifecycleError(f"staged canonical hash mismatch: {logical_path}")
        if entry.get("digest_scope") == "content" and _sha256_bytes(payload) != entry.get("sha256"):
            raise WorkspaceLifecycleError(f"staged canonical hash mismatch: {logical_path}")
        if stat.S_IMODE(target.stat().st_mode) != (int(entry.get("mode", 0)) & 0o7777):
            raise WorkspaceLifecycleError(f"staged canonical permissions mismatch: {logical_path}")
    database_path = _safe_entry_path(stage, registry.config.database_path, registry)
    expected_database = source_manifest.get("database")
    if isinstance(expected_database, dict):
        actual_database = _sqlite_receipt(database_path, registry)
        for field in ("schema_fingerprint", "table_count", "row_count", "schema_object_count"):
            if actual_database.get(field) != expected_database.get(field):
                raise WorkspaceLifecycleError(f"staged database {field} mismatch")
    return {
        "canonical_entry_count": sum(
            1
            for entry in entries.values()
            if entry.get("state_class") in _ARCHIVEABLE_STATE_CLASSES
        ),
        "secret_entry_count": sum(
            1 for entry in entries.values() if entry.get("state_class") in _SECRET_STATE_CLASSES
        ),
        "derived_entries_rebuilt": False,
        "database_verified": isinstance(expected_database, dict),
    }


def _journal_path(backup_root: Path, restore_id: str) -> Path:
    return backup_root / restore_id / "restore-journal.json"


def _read_journal(
    path: Path,
    *,
    expected_restore_id: str | None = None,
    expected_workspace_id: str | None = None,
    expected_root_digest: str | None = None,
) -> dict[str, Any]:
    _assert_no_symlink_components(path, label="restore journal path")
    _assert_not_symlink(path, label="restore journal")
    try:
        raw = _regular_file_bytes(path, label="restore journal", max_bytes=MAX_JOURNAL_BYTES)
        value = json.loads(raw.decode("utf-8"))
    except WorkspaceLifecycleError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkspaceLifecycleError("restore journal is unreadable") from exc
    return _validate_journal(
        value,
        expected_restore_id=expected_restore_id,
        expected_workspace_id=expected_workspace_id,
        expected_root_digest=expected_root_digest,
    )


def _journal_paths(root: Path, restore_id: str) -> tuple[Path, Path, Path, Path]:
    backup_root = workspace_backup_dir(root)
    record_root = backup_root / restore_id
    previous = record_root / "previous-workspace"
    current = record_root / "current-workspace"
    stage = workspace_restore_staging_dir(root) / restore_id
    return record_root, previous, current, stage


def _recovery_action(
    status: str,
    restore_id: str,
    *,
    active_present: bool,
    previous_present: bool,
    current_present: bool,
    stage_present: bool,
) -> str | None:
    """Plan one recovery transition without changing filesystem state."""
    if status == "staged":
        if not active_present and previous_present and stage_present:
            return "promoted_staging"
        if not active_present and previous_present and not stage_present:
            return "restored_previous"
        if active_present and stage_present and not previous_present:
            return "discarded_staging"
        raise WorkspaceLifecycleError(f"staged restore {restore_id} has an ambiguous root state")

    if status == "active_moved":
        if active_present and previous_present and not stage_present:
            return "confirmed_promotion"
        if active_present:
            raise WorkspaceLifecycleError(
                f"interrupted restore {restore_id} has an ambiguous active root"
            )
        if stage_present:
            return "promoted_staging"
        if previous_present:
            return "restored_previous"
        raise WorkspaceLifecycleError(f"interrupted restore {restore_id} has no recoverable root")

    if status == "rollback_active_moved":
        if active_present and current_present and not previous_present:
            return "confirmed_rollback"
        if active_present:
            raise WorkspaceLifecycleError(f"rollback {restore_id} has an ambiguous active root")
        if previous_present:
            return "restored_previous"
        if current_present:
            return "restored_current"
        raise WorkspaceLifecycleError(f"rollback {restore_id} has no previous root")

    if status == "promoted":
        if active_present:
            return None
        if previous_present and current_present:
            return "restored_previous"
        raise WorkspaceLifecycleError(f"promoted restore {restore_id} has no active root")

    if status in {
        "recovered_promoted",
        "recovered_rollback",
        "staging_discarded",
        "rolled_back",
    }:
        if not active_present:
            raise WorkspaceLifecycleError(f"completed restore {restore_id} has no active root")
        return None

    # _validate_journal rejects this before the planner is reached.  Keep the
    # explicit branch so a future status cannot accidentally become a no-op.
    raise WorkspaceLifecycleError(f"restore {restore_id} has an unsupported recovery state")


def restore_workspace(
    root: str | os.PathLike[str],
    archive_path: str | os.PathLike[str],
    *,
    registry: WorkspaceStateRegistry,
    confirm: bool = False,
    restore_id: str | None = None,
    interrupt_after_active_move: bool = False,
    retention: int | None = DEFAULT_RETENTION,
    reconcile_restore: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Stage, verify, and atomically promote a workspace archive.

    The optional interruption hook exists solely for deterministic recovery
    tests.  It leaves the journal at ``active_moved`` so
    :func:`recover_interrupted_restore` can complete or roll back the move.
    """
    if not confirm:
        raise WorkspaceLifecycleError("restore requires explicit confirm=True")
    resolved_root = canonical_workspace_root(root)
    _registry_root(resolved_root, registry)
    if (
        registry.config.identity.root_kind is WorkspaceRootKind.PRODUCTION
        and reconcile_restore is None
    ):
        raise WorkspaceLifecycleError(
            "production restore requires an authority and derived-state reconciliation hook"
        )
    recover_interrupted_restore(resolved_root, registry=registry)
    # Keep the lexical path intact until lstat: resolving first would turn a
    # symlinked archive into its target and defeat the fail-closed check.
    loaded = _load_archive(Path(archive_path).expanduser(), registry)
    if loaded.manifest.get("workspace_id") != registry.config.identity.workspace_id:
        raise InvalidWorkspaceArchiveError("archive workspace identity does not match target")
    restore_id = _safe_restore_id(restore_id)
    backup_root = workspace_backup_dir(resolved_root)
    staging_root = workspace_restore_staging_dir(resolved_root)
    record_root, previous, _current, stage = _journal_paths(resolved_root, restore_id)
    _ensure_directory(backup_root, label="workspace backup directory")
    _ensure_directory(staging_root, label="workspace restore staging directory")
    if _path_present(record_root) or _path_present(stage):
        raise WorkspaceLifecycleError("restore_id already has an active record")
    _ensure_directory(record_root, label="workspace restore record")
    _ensure_directory(stage, label="workspace restore staging root")
    journal_file = _journal_path(backup_root, restore_id)
    try:
        stage_receipt = _materialize_stage(
            root=resolved_root,
            stage=stage,
            loaded=loaded,
            registry=registry,
        )
        if reconcile_restore is not None:
            reconciliation = reconcile_restore(
                root=resolved_root,
                stage=stage,
                source_manifest=loaded.manifest["workspace_manifest"],
                registry=registry,
            )
            if not isinstance(reconciliation, dict) or reconciliation.get("status") != "ready":
                raise WorkspaceLifecycleError("restore reconciliation did not produce a ready receipt")
            # Re-check the stage after the hook has changed the database and
            # intentionally removed stale optional credentials/derived state.
            validated_receipt = _validate_stage(
                stage,
                loaded,
                registry,
                allow_database_mutation=True,
            )
            stage_receipt.update(validated_receipt)
            stage_receipt["restore_reconciliation"] = reconciliation
        journal = {
            "journal_version": JOURNAL_VERSION,
            "restore_id": restore_id,
            "status": "staged",
            "workspace_id": registry.config.identity.workspace_id,
            "workspace_root_digest": _workspace_root_digest(resolved_root),
            "archive_manifest_sha256": loaded.manifest["manifest_sha256"],
            "previous_name": f"{restore_id}/previous-workspace",
            "stage_name": f"{restore_id}",
            "created_at": _utc_timestamp(),
            "stage_receipt": stage_receipt,
        }
        _write_journal(journal_file, journal)
        os.replace(resolved_root, previous)
        _fsync_directory(resolved_root.parent)
        journal["status"] = "active_moved"
        _write_journal(journal_file, journal)
        if interrupt_after_active_move:
            raise InterruptedWorkspaceRestore("restore interrupted after active workspace move")
        os.replace(stage, resolved_root)
        _fsync_directory(resolved_root.parent)
        journal["status"] = "promoted"
        journal["completed_at"] = _utc_timestamp()
        _write_journal(journal_file, journal)
        cleanup_receipt = (
            cleanup_workspace_backups(resolved_root, keep=retention)
            if retention is not None
            else {"status": "skipped"}
        )
        return {
            "status": "restored",
            "restore_id": restore_id,
            "archive_manifest_sha256": loaded.manifest["manifest_sha256"],
            "stage_receipt": stage_receipt,
            "secret_values_included": False,
            "rollback_available": previous.exists(),
            "cleanup": cleanup_receipt,
        }
    except InterruptedWorkspaceRestore:
        raise
    except Exception:
        # Preserve the journal and moved roots for explicit recovery if the
        # promotion fails.  A future process can make the state whole without
        # guessing which root is authoritative.
        if not _path_present(journal_file):
            _remove_tree(stage, label="failed restore staging root")
            _remove_tree(record_root, label="failed restore record")
        raise


def recover_interrupted_restore(
    root: str | os.PathLike[str],
    *,
    registry: WorkspaceStateRegistry | None = None,
) -> dict[str, Any]:
    """Recover journaled staged/half-promoted restores without guessing.

    Every candidate journal and sidecar state is preflighted before the first
    recovery transition.  ``registry`` is optional for direct callers; the
    journal's root binding is always checked, and a supplied registry adds its
    workspace identity to that binding.
    """
    resolved_root = canonical_workspace_root(root)
    if registry is not None:
        _registry_root(resolved_root, registry)
    expected_workspace_id = registry.config.identity.workspace_id if registry is not None else None
    expected_root_digest = _workspace_root_digest(resolved_root)
    backup_root = workspace_backup_dir(resolved_root)
    staging_root = workspace_restore_staging_dir(resolved_root)
    if not _path_present(backup_root):
        return {"status": "clean", "recovered": []}
    _assert_no_symlink_components(backup_root, label="workspace backup directory")
    _assert_not_symlink(backup_root, label="workspace backup directory")
    _assert_no_symlink_components(staging_root, label="workspace restore staging directory")
    _assert_not_symlink(staging_root, label="workspace restore staging directory")

    # Do not mutate a valid earlier record until every later candidate has
    # passed journal, identity, symlink, and root-state validation.
    preflight_candidates: list[dict[str, Any]] = []
    orphan_records: list[tuple[Path, Path]] = []
    orphan_ids: set[str] = set()
    for record_root in sorted(backup_root.iterdir(), key=lambda item: item.name):
        if not _RESTORE_ID_PATTERN.fullmatch(record_root.name):
            continue
        _assert_no_symlink_components(record_root, label="restore record path")
        _assert_not_symlink(record_root, label="restore record root")
        if not record_root.is_dir():
            raise WorkspaceLifecycleError(f"restore record {record_root.name} is not a directory")
        journal_file = record_root / "restore-journal.json"
        _assert_no_symlink_components(journal_file, label="restore journal path")
        if not _path_present(journal_file):
            restore_id = record_root.name
            _record_root, previous, current, stage = _journal_paths(resolved_root, restore_id)
            for path, label in (
                (resolved_root, "active workspace root"),
                (previous, "previous workspace root"),
                (current, "rollback workspace root"),
                (stage, "restore staging root"),
            ):
                _assert_no_symlink_components(path, label=label)
                _assert_not_symlink(path, label=label)
            if (
                _path_present(resolved_root)
                and not _path_present(previous)
                and not _path_present(current)
                and _is_empty_directory(record_root, label="unjournaled restore record")
                and _is_empty_directory(stage, label="unjournaled restore staging root")
            ):
                orphan_records.append((record_root, stage))
                orphan_ids.add(restore_id)
                continue
            raise WorkspaceLifecycleError(
                f"restore record {record_root.name} journal is missing; manual recovery is required"
            )
        restore_id = record_root.name
        journal = _read_journal(
            journal_file,
            expected_restore_id=restore_id,
            expected_workspace_id=expected_workspace_id,
            expected_root_digest=expected_root_digest,
        )
        _record_root, previous, current, stage = _journal_paths(resolved_root, restore_id)
        for path, label in (
            (resolved_root, "active workspace root"),
            (previous, "previous workspace root"),
            (current, "rollback workspace root"),
            (stage, "restore staging root"),
        ):
            _assert_no_symlink_components(path, label=label)
            _assert_not_symlink(path, label=label)
        preflight_candidates.append(
            {
                "record_root": record_root,
                "journal_file": journal_file,
                "journal": journal,
                "restore_id": restore_id,
                "previous": previous,
                "current": current,
                "stage": stage,
                "active_present": _path_present(resolved_root),
                "previous_present": _path_present(previous),
                "current_present": _path_present(current),
                "stage_present": _path_present(stage),
            }
        )

    in_flight = [
        candidate
        for candidate in preflight_candidates
        if candidate["journal"]["status"] in _RECOVERY_IN_FLIGHT_STATUSES
    ]
    newest_in_flight = (
        max(
            in_flight,
            key=lambda candidate: (
                datetime.fromisoformat(candidate["journal"]["created_at"]).timestamp(),
                candidate["restore_id"],
            ),
        )
        if in_flight
        else None
    )
    superseded_terminal_ids: set[str] = set()
    if newest_in_flight is not None:
        newest_key = (
            datetime.fromisoformat(newest_in_flight["journal"]["created_at"]).timestamp(),
            newest_in_flight["restore_id"],
        )
        for candidate in preflight_candidates:
            candidate_key = (
                datetime.fromisoformat(candidate["journal"]["created_at"]).timestamp(),
                candidate["restore_id"],
            )
            if (
                candidate["journal"]["status"] in _TERMINAL_JOURNAL_STATUSES
                and not candidate["active_present"]
                and candidate_key < newest_key
            ):
                superseded_terminal_ids.add(candidate["restore_id"])

    actionable_records = 0
    for candidate in preflight_candidates:
        if candidate["restore_id"] in superseded_terminal_ids:
            continue
        action = _recovery_action(
            candidate["journal"]["status"],
            candidate["restore_id"],
            active_present=candidate["active_present"],
            previous_present=candidate["previous_present"],
            current_present=candidate["current_present"],
            stage_present=candidate["stage_present"],
        )
        candidate["action"] = action
        if action is not None:
            actionable_records += 1
    if actionable_records > 1:
        raise WorkspaceLifecycleError("multiple restore records require recovery")

    recovered: list[dict[str, Any]] = []
    for record_root, stage in orphan_records:
        _remove_tree(stage, label="empty unjournaled restore staging root")
        _remove_tree(record_root, label="empty unjournaled restore record")
        recovered.append(
            {"restore_id": record_root.name, "action": "discarded_unjournaled_record"}
        )
    if orphan_records:
        _fsync_directory(backup_root)
        if _path_present(staging_root):
            _fsync_directory(staging_root)

    for record_root in sorted(backup_root.iterdir(), key=lambda item: item.name):
        if not _RESTORE_ID_PATTERN.fullmatch(record_root.name):
            continue
        if record_root.name in superseded_terminal_ids or record_root.name in orphan_ids:
            continue
        _assert_not_symlink(record_root, label="restore record root")
        if not record_root.is_dir():
            raise WorkspaceLifecycleError(f"restore record {record_root.name} is not a directory")
        journal_file = record_root / "restore-journal.json"
        if not _path_present(journal_file):
            raise WorkspaceLifecycleError(f"restore record {record_root.name} journal is missing")
        restore_id = record_root.name
        journal = _read_journal(
            journal_file,
            expected_restore_id=restore_id,
            expected_workspace_id=expected_workspace_id,
            expected_root_digest=expected_root_digest,
        )
        status = journal.get("status")
        _record_root, previous, current, stage = _journal_paths(resolved_root, restore_id)
        for path, label in (
            (resolved_root, "active workspace root"),
            (previous, "previous workspace root"),
            (current, "rollback workspace root"),
            (stage, "restore staging root"),
        ):
            _assert_not_symlink(path, label=label)
        if status == "staged":
            active_present = _path_present(resolved_root)
            previous_present = _path_present(previous)
            stage_present = _path_present(stage)
            if not active_present and previous_present and stage_present:
                os.replace(stage, resolved_root)
                _fsync_directory(resolved_root.parent)
                journal["status"] = "recovered_promoted"
                action = "promoted_staging"
            elif not active_present and previous_present and not stage_present:
                os.replace(previous, resolved_root)
                _fsync_directory(resolved_root.parent)
                journal["status"] = "recovered_rollback"
                action = "restored_previous"
            elif active_present and stage_present and not previous_present:
                _remove_tree(stage, label="abandoned restore staging root")
                journal["status"] = "staging_discarded"
                action = "discarded_staging"
            else:
                raise WorkspaceLifecycleError(f"staged restore {restore_id} has an ambiguous root state")
            journal["recovered_at"] = _utc_timestamp()
            _write_journal(journal_file, journal)
            recovered.append({"restore_id": restore_id, "action": action})
            continue
        if status == "active_moved":
            active_present = _path_present(resolved_root)
            previous_present = _path_present(previous)
            stage_present = _path_present(stage)
            if active_present and previous_present and not stage_present:
                journal["status"] = "recovered_promoted"
                journal["recovered_at"] = _utc_timestamp()
                _write_journal(journal_file, journal)
                recovered.append({"restore_id": restore_id, "action": "confirmed_promotion"})
                continue
            if active_present:
                raise WorkspaceLifecycleError(
                    f"interrupted restore {restore_id} has an ambiguous active root"
                )
            if stage_present:
                os.replace(stage, resolved_root)
                _fsync_directory(resolved_root.parent)
                journal["status"] = "recovered_promoted"
                action = "promoted_staging"
            elif previous_present:
                os.replace(previous, resolved_root)
                _fsync_directory(resolved_root.parent)
                journal["status"] = "recovered_rollback"
                action = "restored_previous"
            else:
                raise WorkspaceLifecycleError(f"interrupted restore {restore_id} has no recoverable root")
            journal["recovered_at"] = _utc_timestamp()
            _write_journal(journal_file, journal)
            recovered.append({"restore_id": restore_id, "action": action})
            continue
        if status == "rollback_active_moved":
            active_present = _path_present(resolved_root)
            previous_present = _path_present(previous)
            current_present = _path_present(current)
            if active_present and current_present and not previous_present:
                journal["status"] = "recovered_rollback"
                journal["recovered_at"] = _utc_timestamp()
                _write_journal(journal_file, journal)
                recovered.append({"restore_id": restore_id, "action": "confirmed_rollback"})
                continue
            if active_present:
                raise WorkspaceLifecycleError(
                    f"rollback {restore_id} has an ambiguous active root"
                )
            if previous_present:
                os.replace(previous, resolved_root)
                _fsync_directory(resolved_root.parent)
                journal["status"] = "recovered_rollback"
                journal["recovered_at"] = _utc_timestamp()
                _write_journal(journal_file, journal)
                recovered.append({"restore_id": restore_id, "action": "restored_previous"})
            elif current_present:
                os.replace(current, resolved_root)
                _fsync_directory(resolved_root.parent)
                journal["status"] = "recovered_rollback"
                journal["recovered_at"] = _utc_timestamp()
                _write_journal(journal_file, journal)
                recovered.append({"restore_id": restore_id, "action": "restored_current"})
            else:
                raise WorkspaceLifecycleError(f"rollback {restore_id} has no previous root")
        if status == "promoted" and not _path_present(resolved_root):
            # A rollback may have moved the promoted root before its journal
            # update was durably written.  Restore the retained pre-restore
            # root only when both rename endpoints make that state explicit.
            if _path_present(previous) and _path_present(current):
                os.replace(previous, resolved_root)
                _fsync_directory(resolved_root.parent)
                journal["status"] = "recovered_rollback"
                journal["recovered_at"] = _utc_timestamp()
                _write_journal(journal_file, journal)
                recovered.append({"restore_id": restore_id, "action": "restored_previous"})
            else:
                raise WorkspaceLifecycleError(f"promoted restore {restore_id} has no active root")
    return {"status": "recovered" if recovered else "clean", "recovered": recovered}


def rollback_workspace(
    root: str | os.PathLike[str],
    restore_id: str,
    *,
    registry: WorkspaceStateRegistry | None = None,
) -> dict[str, Any]:
    """Atomically swap the current root with a retained pre-restore root.

    A registry is mandatory so the journal workspace identity is checked
    before any rollback transition can move a root.
    """
    if registry is None:
        raise WorkspaceLifecycleError("rollback requires a workspace registry identity")
    resolved_root = canonical_workspace_root(root)
    _registry_root(resolved_root, registry)
    restore_id = _safe_restore_id(restore_id)
    backup_root = workspace_backup_dir(resolved_root)
    journal_file = _journal_path(backup_root, restore_id)
    journal = _read_journal(
        journal_file,
        expected_restore_id=restore_id,
        expected_workspace_id=registry.config.identity.workspace_id,
        expected_root_digest=_workspace_root_digest(resolved_root),
    )
    if journal.get("status") not in {"promoted", "recovered_promoted"}:
        raise WorkspaceLifecycleError("restore is not in a rollback-capable state")
    _record_root, previous, current, stage = _journal_paths(resolved_root, restore_id)
    if not resolved_root.is_dir() or resolved_root.is_symlink():
        raise WorkspaceLifecycleError("active workspace root is not a safe directory")
    if not previous.is_dir() or previous.is_symlink():
        raise WorkspaceLifecycleError("pre-restore workspace is unavailable")
    _assert_not_symlink(current, label="rollback workspace root")
    _assert_not_symlink(stage, label="restore staging root")
    if _path_present(current) or _path_present(stage):
        raise WorkspaceLifecycleError("rollback target has an ambiguous sidecar")
    os.replace(resolved_root, current)
    _fsync_directory(resolved_root.parent)
    journal["status"] = "rollback_active_moved"
    _write_journal(journal_file, journal)
    try:
        os.replace(previous, resolved_root)
        _fsync_directory(resolved_root.parent)
    except Exception:
        raise
    journal["status"] = "rolled_back"
    journal["rolled_back_at"] = _utc_timestamp()
    _write_journal(journal_file, journal)
    return {
        "status": "rolled_back",
        "restore_id": restore_id,
        "rollback_available": False,
        "secret_values_included": False,
    }


def cleanup_workspace_backups(
    root: str | os.PathLike[str],
    *,
    keep: int = DEFAULT_RETENTION,
) -> dict[str, Any]:
    """Remove only old derived backup records/archives, with bounded retention."""
    if not isinstance(keep, int) or keep < 0 or keep > 100:
        raise WorkspaceLifecycleError("backup retention must be an integer from 0 through 100")
    resolved_root = canonical_workspace_root(root)
    backup_root = workspace_backup_dir(resolved_root)
    if not backup_root.exists():
        return {"status": "clean", "retained": [], "removed": []}
    _assert_not_symlink(backup_root, label="workspace backup directory")
    candidates = [
        item
        for item in backup_root.iterdir()
        if (item.name.startswith("backup-") and item.name.endswith(".seraph.zip"))
        or _RESTORE_ID_PATTERN.fullmatch(item.name)
    ]
    candidates.sort(key=lambda item: item.stat().st_mtime_ns, reverse=True)
    retained = candidates[:keep]
    removed: list[str] = []
    for candidate in candidates[keep:]:
        if candidate.is_symlink():
            raise WorkspaceLifecycleError("backup retention encountered a symlink")
        if candidate.is_dir():
            journal_file = candidate / "restore-journal.json"
            if not _path_present(journal_file):
                # A crash before the journal write may leave an empty record.
                # Only that provably empty record and its empty staging sibling
                # may be discarded by retention; any evidence is operator
                # recovery material and must remain fail-closed.
                stage = workspace_restore_staging_dir(resolved_root) / candidate.name
                if not _is_empty_directory(candidate, label=f"unjournaled backup record {candidate.name}"):
                    continue
                if not _is_empty_directory(stage, label=f"unjournaled staging root {candidate.name}"):
                    continue
                _remove_tree(stage, label=f"empty unjournaled staging root {candidate.name}")
                _remove_tree(candidate, label=f"empty unjournaled backup record {candidate.name}")
                removed.append(candidate.name)
                continue
            journal = _read_journal(
                journal_file,
                expected_restore_id=candidate.name,
                expected_root_digest=_workspace_root_digest(resolved_root),
            )
            if journal.get("status") in {"active_moved", "rollback_active_moved"}:
                continue
            _remove_tree(candidate, label=f"old backup record {candidate.name}")
        elif candidate.is_file():
            candidate.unlink()
        else:
            raise WorkspaceLifecycleError(f"backup retention found unsupported entry: {candidate.name}")
        removed.append(candidate.name)
    if removed:
        _fsync_directory(backup_root)
    return {
        "status": "cleaned" if removed else "clean",
        "retained": [item.name for item in retained],
        "removed": removed,
        "retention": keep,
    }


__all__ = [
    "ARCHIVE_FORMAT",
    "ARCHIVE_VERSION",
    "DEFAULT_RETENTION",
    "MAX_ARCHIVE_BYTES",
    "MAX_ARCHIVE_MEMBERS",
    "MAX_ARCHIVE_MEMBER_BYTES",
    "MAX_JOURNAL_BYTES",
    "InterruptedWorkspaceRestore",
    "InvalidWorkspaceArchiveError",
    "MissingSecretMaterialError",
    "WorkspaceLifecycleError",
    "backup_workspace",
    "cleanup_workspace_backups",
    "recover_interrupted_restore",
    "restore_workspace",
    "rollback_workspace",
    "workspace_backup_dir",
    "workspace_restore_staging_dir",
]
