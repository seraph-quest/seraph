"""Read-only, deterministic workspace inventory primitives.

This module deliberately owns no lifecycle, scheduler, vault, archive, or
database-engine behavior.  A caller supplies an explicit workspace identity
and a relative path classification allow-list.  Synthetic fixtures and the
configured production workspace use the same registry for path classification
and inventory.  The bounded backup and restore helpers in ``lifecycle.py``
consume this registry contract.

Secret/recovery files are never opened.  Their manifest ``sha256`` is a
deterministic digest of redacted metadata, marked by ``digest_scope``; this
keeps the manifest useful for change detection without loading key material.
Filesystem enumeration and regular-file hashing are bounded by the explicit
limits on ``WorkspaceConfig``.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import sqlite3
import stat
from typing import Any
from urllib.parse import quote


class WorkspaceStateError(ValueError):
    """Base error for invalid or unsafe workspace state."""


class AmbiguousWorkspaceRootsError(WorkspaceStateError):
    """Raised when two classifications could own the same path."""


class UnknownWorkspacePathError(WorkspaceStateError):
    """Raised when an entry is not covered by the explicit registry."""


class UnsupportedWorkspaceEntryError(WorkspaceStateError):
    """Raised for links, devices, sockets, FIFOs, or other unsafe entries."""


_INVENTORY_LIMIT_REASON_CODES: dict[str, str] = {
    "max_entries": "workspace_inventory_entry_limit_exceeded",
    "max_depth": "workspace_inventory_depth_limit_exceeded",
    "max_total_bytes": "workspace_inventory_total_bytes_limit_exceeded",
    "max_file_bytes": "workspace_inventory_file_bytes_limit_exceeded",
}


class WorkspaceInventoryLimitError(WorkspaceStateError):
    """Raised when inventory work reaches a configured resource bound."""

    def __init__(self, limit_name: str, configured_limit: int, observed: int) -> None:
        if limit_name not in _INVENTORY_LIMIT_REASON_CODES:
            raise ValueError(f"unknown workspace inventory limit: {limit_name}")
        self.limit_name = limit_name
        self.configured_limit = configured_limit
        self.observed = observed
        super().__init__(f"workspace inventory {limit_name} limit exceeded")

    @property
    def reason_code(self) -> str:
        return _INVENTORY_LIMIT_REASON_CODES[self.limit_name]

    def as_receipt(self) -> dict[str, int | str]:
        """Return bounded, non-sensitive operator metadata for this breach."""
        return {
            "name": self.limit_name,
            "configured": self.configured_limit,
            "observed": self.observed,
        }


# These defaults bound ordinary production inventory while leaving room for
# the current local workspace's reports, screenshots, and runtime metadata.
DEFAULT_MAX_INVENTORY_ENTRIES = 4096
DEFAULT_MAX_INVENTORY_DEPTH = 32
DEFAULT_MAX_INVENTORY_TOTAL_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_INVENTORY_FILE_BYTES = 128 * 1024 * 1024


def _positive_inventory_limit(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise WorkspaceStateError(f"{field_name} must be a positive integer")
    return value


class WorkspaceRootKind(str, Enum):
    """Root scopes supported by the workspace inventory contract."""

    SYNTHETIC_FIXTURE = "synthetic_fixture"
    PRODUCTION = "production"


class WorkspaceStateClass(str, Enum):
    CANONICAL = "canonical"
    SECRET_RECOVERY = "secret/recovery"
    SECRET = "secret"
    DERIVED = "derived"
    CACHE = "cache"
    EXTERNAL_REFERENCE = "external-reference"
    DISPOSABLE = "disposable"


# This is intentionally a small, stable vocabulary for operator and migration
# consumers.  The state class remains the authoritative classification; these
# roles make the payload/rebuild/cache/secret boundary explicit in a manifest
# without exposing host paths or file contents.
_STATE_CLASS_ROLES: dict[str, str] = {
    WorkspaceStateClass.CANONICAL.value: "canonical_payload",
    WorkspaceStateClass.SECRET_RECOVERY.value: "secret_recovery",
    WorkspaceStateClass.SECRET.value: "secret",
    WorkspaceStateClass.DERIVED.value: "rebuildable",
    WorkspaceStateClass.CACHE.value: "cache",
    WorkspaceStateClass.EXTERNAL_REFERENCE.value: "external_reference",
    WorkspaceStateClass.DISPOSABLE.value: "disposable",
}


class ExternalReferencePolicy(str, Enum):
    """Non-secret policies for logical references that are not local paths."""

    CONSENTED_LOGICAL_ROOT = "consented-logical-root"
    PAIRED_EDGE_REFERENCE = "paired-edge-reference"


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: object) -> str:
    return _sha256_bytes(_canonical_json(value).encode("utf-8"))


def _inventory_failure_reason(exc: WorkspaceStateError) -> str:
    """Map an internal inventory failure to a redacted operator code."""
    if isinstance(exc, WorkspaceInventoryLimitError):
        return exc.reason_code
    if isinstance(exc, UnsupportedWorkspaceEntryError):
        return "unsupported_or_symlink_entry"
    if isinstance(exc, UnknownWorkspacePathError):
        return "unknown_workspace_path"
    if isinstance(exc, AmbiguousWorkspaceRootsError):
        return "ambiguous_workspace_roots"
    message = str(exc).lower()
    if "escapes root" in message:
        return "workspace_path_escape"
    if "database" in message or "sqlite" in message:
        return "database_inventory_failed"
    if "required secret workspace path" in message:
        return "required_secret_missing"
    if "declared workspace path" in message:
        return "declared_path_missing"
    if "root" in message:
        return "workspace_root_invalid"
    return "workspace_inventory_failed"


def _inventory_receipt_id(
    workspace_id: str,
    root_kind: str,
    status: str,
    reasons: list[str],
) -> str:
    digest = _sha256_json(
        {
            "workspace_id": workspace_id,
            "root_kind": root_kind,
            "status": status,
            "reasons": sorted(reasons),
        }
    )
    return f"workspace-inventory-{digest[:24]}"


def _normalize_relative_path(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkspaceStateError(f"{field_name} must be a non-empty relative path")
    raw = value.strip().replace("\\", "/")
    if "\x00" in raw or raw.startswith("~") or raw.lower().startswith("file:"):
        raise WorkspaceStateError(f"{field_name} contains an unsafe path form")
    windows = PureWindowsPath(raw)
    if raw.startswith("/") or windows.is_absolute() or windows.drive:
        raise WorkspaceStateError(f"{field_name} must not be absolute: {value!r}")
    parts = PurePosixPath(raw).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise WorkspaceStateError(f"{field_name} must not contain '.', '..', or empty path parts")
    normalized = "/".join(parts)
    if normalized != raw:
        raise WorkspaceStateError(f"{field_name} is not normalized: {value!r}")
    return normalized


@dataclass(frozen=True)
class WorkspaceIdentity:
    """Stable logical identity plus the one explicit local workspace root."""

    workspace_id: str
    root: Path
    root_kind: WorkspaceRootKind = WorkspaceRootKind.SYNTHETIC_FIXTURE
    synthetic_marker: str = ".seraph-synthetic-workspace"

    def __post_init__(self) -> None:
        workspace_id = str(self.workspace_id or "").strip()
        if (
            not workspace_id
            or workspace_id in {".", ".."}
            or "/" in workspace_id
            or "\\" in workspace_id
            or len(workspace_id) > 64
            or not workspace_id.islower()
            or not (workspace_id.startswith("synthetic-") or workspace_id.startswith("workspace-"))
            or any(
                part in {"secret", "key", "token", "password", "credential", "private"}
                for part in workspace_id.split("-")
            )
        ):
            raise WorkspaceStateError("workspace_id must be a non-secret workspace/synthetic slug")
        root = Path(self.root)
        if not root.is_absolute():
            raise WorkspaceStateError("workspace root must be absolute")
        try:
            root_kind = WorkspaceRootKind(self.root_kind)
        except (TypeError, ValueError) as exc:
            raise WorkspaceStateError(f"unknown workspace root kind: {self.root_kind!r}") from exc
        marker = _normalize_relative_path(self.synthetic_marker, field_name="synthetic_marker")
        if "/" in marker:
            raise WorkspaceStateError("synthetic_marker must be a single path component")
        object.__setattr__(self, "workspace_id", workspace_id)
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "root_kind", root_kind)
        object.__setattr__(self, "synthetic_marker", marker)


@dataclass(frozen=True)
class WorkspacePathSpec:
    """A relative path owned by one explicit workspace state class."""

    logical_path: str
    state_class: WorkspaceStateClass
    # Requiredness is currently used for secret/recovery material.  Production
    # integrations may declare optional credentials without weakening the
    # fail-closed rule for recovery keys and other required secrets.
    required: bool = True

    def __post_init__(self) -> None:
        logical_path = _normalize_relative_path(self.logical_path, field_name="logical_path")
        try:
            state_class = WorkspaceStateClass(self.state_class)
        except (TypeError, ValueError) as exc:
            raise WorkspaceStateError(f"unknown workspace state class: {self.state_class!r}") from exc
        if state_class is WorkspaceStateClass.EXTERNAL_REFERENCE:
            raise WorkspaceStateError("external-reference state must use ExternalReferenceSpec, not a local path")
        if not isinstance(self.required, bool):
            raise WorkspaceStateError("workspace path requiredness must be boolean")
        if not self.required and state_class is not WorkspaceStateClass.SECRET:
            raise WorkspaceStateError(
                "only ordinary secret credentials may be declared optional"
            )
        object.__setattr__(self, "logical_path", logical_path)
        object.__setattr__(self, "state_class", state_class)
        object.__setattr__(self, "required", self.required)


@dataclass(frozen=True)
class ExternalReferenceSpec:
    """A logical external root; the registry never resolves its host path."""

    reference_id: str
    policy: ExternalReferencePolicy | str

    def __post_init__(self) -> None:
        reference_id = str(self.reference_id or "").strip()
        if (
            not reference_id
            or reference_id in {".", ".."}
            or "\x00" in reference_id
            or "/" in reference_id
            or "\\" in reference_id
            or len(reference_id) > 64
            or not reference_id.islower()
            or any(
                part in {"secret", "key", "token", "password", "credential", "private"}
                for part in reference_id.split("-")
            )
        ):
            raise WorkspaceStateError("external reference id must be a non-secret logical slug")
        try:
            policy = ExternalReferencePolicy(self.policy)
        except (TypeError, ValueError) as exc:
            raise WorkspaceStateError("external reference policy must be a declared non-secret policy") from exc
        object.__setattr__(self, "reference_id", reference_id)
        object.__setattr__(self, "policy", policy)


@dataclass(frozen=True)
class WorkspaceDatabaseObjectSpec:
    """Typed ownership and redaction contract for one SQLite schema object."""

    name: str
    object_type: str
    state_class: WorkspaceStateClass
    redaction_policy: str = "metadata-only"

    def __post_init__(self) -> None:
        name = str(self.name or "").strip()
        object_type = str(self.object_type or "").strip().lower()
        try:
            state_class = WorkspaceStateClass(self.state_class)
        except (TypeError, ValueError) as exc:
            raise WorkspaceStateError(f"unknown database state class: {self.state_class!r}") from exc
        if (
            not name
            or "\x00" in name
            or "." in name
            or "/" in name
            or "\\" in name
            or not name.isidentifier()
        ):
            raise WorkspaceStateError("database object names must be plain identifiers")
        if object_type not in {"table", "index", "trigger", "view"}:
            raise WorkspaceStateError(f"unsupported database object type: {object_type!r}")
        if state_class is WorkspaceStateClass.EXTERNAL_REFERENCE:
            raise WorkspaceStateError("database objects cannot own external-reference state")
        if self.redaction_policy != "metadata-only":
            raise WorkspaceStateError("database inventory only supports metadata-only redaction")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "object_type", object_type)
        object.__setattr__(self, "state_class", state_class)
        object.__setattr__(self, "redaction_policy", "metadata-only")


@dataclass(frozen=True)
class WorkspaceConfig:
    """Explicit inventory configuration; no implicit workspace roots exist.

    The four inventory limits are part of the caller-supplied contract.  They
    bound filesystem enumeration and regular-file hashing for both synthetic
    and production roots; secret/recovery entries are still metadata-only.
    """

    identity: WorkspaceIdentity
    declared_paths: tuple[WorkspacePathSpec, ...]
    database_path: str = "seraph.db"
    workspace_version: int = 1
    external_references: tuple[ExternalReferenceSpec, ...] = ()
    expected_database_objects: tuple[WorkspaceDatabaseObjectSpec, ...] = ()
    max_entries: int = DEFAULT_MAX_INVENTORY_ENTRIES
    max_depth: int = DEFAULT_MAX_INVENTORY_DEPTH
    max_total_bytes: int = DEFAULT_MAX_INVENTORY_TOTAL_BYTES
    max_file_bytes: int = DEFAULT_MAX_INVENTORY_FILE_BYTES

    def __post_init__(self) -> None:
        declared_paths = tuple(self.declared_paths)
        if not declared_paths:
            raise WorkspaceStateError("workspace configuration must declare at least one path")
        if any(not isinstance(item, WorkspacePathSpec) for item in declared_paths):
            raise WorkspaceStateError("declared_paths must contain WorkspacePathSpec values")
        database_path = _normalize_relative_path(self.database_path, field_name="database_path")
        if not isinstance(self.workspace_version, int) or self.workspace_version < 1:
            raise WorkspaceStateError("workspace_version must be a positive integer")
        max_entries = _positive_inventory_limit(self.max_entries, field_name="max_entries")
        max_depth = _positive_inventory_limit(self.max_depth, field_name="max_depth")
        max_total_bytes = _positive_inventory_limit(
            self.max_total_bytes,
            field_name="max_total_bytes",
        )
        max_file_bytes = _positive_inventory_limit(
            self.max_file_bytes,
            field_name="max_file_bytes",
        )
        names = [item.logical_path for item in declared_paths]
        if len(names) != len(set(names)):
            raise AmbiguousWorkspaceRootsError("duplicate workspace path classifications")
        for index, left in enumerate(declared_paths):
            for right in declared_paths[index + 1 :]:
                if left.logical_path.startswith(f"{right.logical_path}/") or right.logical_path.startswith(
                    f"{left.logical_path}/"
                ):
                    raise AmbiguousWorkspaceRootsError(
                        f"overlapping workspace roots are ambiguous: {left.logical_path!r} and {right.logical_path!r}"
                    )
        owners = [
            item
            for item in declared_paths
            if database_path == item.logical_path or database_path.startswith(f"{item.logical_path}/")
        ]
        if len(owners) != 1 or owners[0].state_class is not WorkspaceStateClass.CANONICAL:
            raise AmbiguousWorkspaceRootsError("database_path must be covered by exactly one canonical path")
        external_references = tuple(self.external_references)
        if any(not isinstance(item, ExternalReferenceSpec) for item in external_references):
            raise WorkspaceStateError("external_references must contain ExternalReferenceSpec values")
        reference_ids = [item.reference_id for item in external_references]
        if len(reference_ids) != len(set(reference_ids)):
            raise AmbiguousWorkspaceRootsError("duplicate external reference ids")
        expected_objects = tuple(self.expected_database_objects)
        if any(not isinstance(item, WorkspaceDatabaseObjectSpec) for item in expected_objects):
            raise WorkspaceStateError("expected_database_objects must contain typed object specifications")
        expected_names = [item.name for item in expected_objects]
        if len(expected_names) != len(set(expected_names)):
            raise AmbiguousWorkspaceRootsError("duplicate expected database object names")
        object.__setattr__(self, "declared_paths", declared_paths)
        object.__setattr__(self, "database_path", database_path)
        object.__setattr__(self, "external_references", external_references)
        object.__setattr__(self, "expected_database_objects", expected_objects)
        object.__setattr__(self, "max_entries", max_entries)
        object.__setattr__(self, "max_depth", max_depth)
        object.__setattr__(self, "max_total_bytes", max_total_bytes)
        object.__setattr__(self, "max_file_bytes", max_file_bytes)


@dataclass(frozen=True)
class _Entry:
    logical_path: str
    path: Path
    state_class: WorkspaceStateClass
    required: bool
    file_type: str
    mode: int
    size_bytes: int
    sha256: str
    digest_scope: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "logical_path": self.logical_path,
            "state_class": self.state_class.value,
            "state_role": _STATE_CLASS_ROLES[self.state_class.value],
            "required": self.required,
            "file_type": self.file_type,
            "mode": self.mode,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "digest_scope": self.digest_scope,
        }


class WorkspaceStateRegistry:
    """Build a deterministic, redacted manifest from an explicit config."""

    manifest_version = 1

    def __init__(self, config: WorkspaceConfig):
        if not isinstance(config, WorkspaceConfig):
            raise WorkspaceStateError("registry requires a WorkspaceConfig")
        self.config = config

    def _inventory_limits_payload(self) -> dict[str, int]:
        """Return the explicit limits without exposing the workspace root."""
        return {
            "max_entries": self.config.max_entries,
            "max_depth": self.config.max_depth,
            "max_total_bytes": self.config.max_total_bytes,
            "max_file_bytes": self.config.max_file_bytes,
        }

    def build_manifest(self) -> dict[str, Any]:
        root = self._validated_root()
        self._assert_within_root(root, root / self.config.database_path, self.config.database_path)
        missing_declared_paths: list[str] = []
        entries = self._inventory_entries(root, missing_declared_paths=missing_declared_paths)
        sqlite = self._sqlite_inventory(root / self.config.database_path)
        entry_dicts = [entry.as_dict() for entry in entries]
        state_counts = Counter(entry.state_class.value for entry in entries)
        file_type_counts = Counter(entry.file_type for entry in entries)
        missing_declared_paths.sort()
        inventory_status = "degraded" if missing_declared_paths else "ready"
        body: dict[str, Any] = {
            "manifest_version": self.manifest_version,
            "workspace_version": self.config.workspace_version,
            "workspace_id": self.config.identity.workspace_id,
            "root_kind": self.config.identity.root_kind.value,
            "inventory_limits": self._inventory_limits_payload(),
            "inventory": {
                "status": inventory_status,
                "missing_declared_paths": missing_declared_paths,
                "state_roles": dict(sorted(_STATE_CLASS_ROLES.items())),
                "required_secret_paths": sorted(
                    spec.logical_path
                    for spec in self.config.declared_paths
                    if spec.required
                    and spec.state_class
                    in {WorkspaceStateClass.SECRET, WorkspaceStateClass.SECRET_RECOVERY}
                ),
                "optional_secret_paths": sorted(
                    spec.logical_path
                    for spec in self.config.declared_paths
                    if not spec.required
                    and spec.state_class
                    in {WorkspaceStateClass.SECRET, WorkspaceStateClass.SECRET_RECOVERY}
                ),
            },
            "database": sqlite,
            "counts": {
                "entries": len(entries),
                "files": sum(entry.file_type in {"file", "sqlite"} for entry in entries),
                "directories": sum(entry.file_type == "directory" for entry in entries),
                "external_references": len(self.config.external_references),
                "missing_declared_paths": len(missing_declared_paths),
                "by_state_class": dict(sorted(state_counts.items())),
                "by_file_type": dict(sorted(file_type_counts.items())),
            },
            "entries": entry_dicts,
            "external_references": [
                {
                    "reference_id": item.reference_id,
                    "state_class": WorkspaceStateClass.EXTERNAL_REFERENCE.value,
                    "policy": item.policy.value,
                }
                for item in sorted(self.config.external_references, key=lambda value: value.reference_id)
            ],
        }
        return {**body, "manifest_sha256": _sha256_json(body)}

    def build_inventory_receipt(self) -> dict[str, Any]:
        """Return a redacted, operator-readable production inventory receipt.

        ``build_manifest`` intentionally raises on an unsafe or incomplete
        inventory so callers that need strict behavior can stop immediately.
        Operator surfaces often need a bounded status instead.  This method
        converts those failures into a stable blocked receipt without exposing
        the host root, exception text, or any file contents.
        """
        identity = self.config.identity
        base: dict[str, Any] = {
            "schema_version": "seraph.workspace.inventory.v1",
            "workspace_id": identity.workspace_id,
            "root_kind": identity.root_kind.value,
            "status": "blocked",
            "operator_status": "workspace_inventory_blocked",
            "manifest": None,
            "manifest_sha256": None,
            "missing_declared_paths": [],
            "inventory_limits": self._inventory_limits_payload(),
            "limit_breach": None,
            "required_secret_paths": sorted(
                spec.logical_path
                for spec in self.config.declared_paths
                if spec.required
                and spec.state_class
                in {WorkspaceStateClass.SECRET, WorkspaceStateClass.SECRET_RECOVERY}
            ),
            "optional_secret_paths": sorted(
                spec.logical_path
                for spec in self.config.declared_paths
                if not spec.required
                and spec.state_class
                in {WorkspaceStateClass.SECRET, WorkspaceStateClass.SECRET_RECOVERY}
            ),
            "degraded_reasons": [],
            "blocked_reasons": [],
            "secret_values_included": False,
        }
        try:
            manifest = self.build_manifest()
        except WorkspaceStateError as exc:
            reason = _inventory_failure_reason(exc)
            if isinstance(exc, WorkspaceInventoryLimitError):
                base["limit_breach"] = exc.as_receipt()
            base["blocked_reasons"] = [reason]
            base["reason_code"] = reason
            base["receipt_id"] = _inventory_receipt_id(
                identity.workspace_id,
                identity.root_kind.value,
                "blocked",
                [reason],
            )
            return base

        inventory = manifest.get("inventory")
        if not isinstance(inventory, dict):
            # This branch is defensive; manifests produced by this class
            # always include the inventory envelope.
            reason = "inventory_envelope_missing"
            base["blocked_reasons"] = [reason]
            base["reason_code"] = reason
            base["receipt_id"] = _inventory_receipt_id(
                identity.workspace_id,
                identity.root_kind.value,
                "blocked",
                [reason],
            )
            return base

        status = str(inventory.get("status") or "blocked")
        if status not in {"ready", "degraded"}:
            status = "blocked"
        missing = inventory.get("missing_declared_paths")
        missing_paths = sorted(
            item for item in (missing if isinstance(missing, list) else []) if isinstance(item, str)
        )
        degraded_reasons = ["declared_paths_missing"] if missing_paths else []
        required_secret_paths = inventory.get("required_secret_paths")
        optional_secret_paths = inventory.get("optional_secret_paths")
        base.update(
            {
                "status": status,
                "operator_status": f"workspace_inventory_{status}",
                "manifest": manifest,
                "manifest_sha256": manifest.get("manifest_sha256"),
                "missing_declared_paths": missing_paths,
                "required_secret_paths": sorted(
                    item
                    for item in (required_secret_paths if isinstance(required_secret_paths, list) else [])
                    if isinstance(item, str)
                ),
                "optional_secret_paths": sorted(
                    item
                    for item in (optional_secret_paths if isinstance(optional_secret_paths, list) else [])
                    if isinstance(item, str)
                ),
                "degraded_reasons": degraded_reasons,
                "blocked_reasons": [],
                "reason_code": degraded_reasons[0] if degraded_reasons else None,
                "receipt_id": _inventory_receipt_id(
                    identity.workspace_id,
                    identity.root_kind.value,
                    status,
                    missing_paths,
                ),
            }
        )
        return base

    def build_manifest_json(self) -> str:
        """Return canonical JSON without host paths or file contents."""
        return _canonical_json(self.build_manifest())

    def classify_path(self, logical_path: str) -> WorkspaceStateClass:
        """Classify one declared logical path without reading its contents.

        Runtime persistence callers use this method to bind their path to the
        same registry as inventory.  Unknown paths fail closed; callers that
        need a full inventory should use :meth:`build_manifest`, which also
        verifies the synthetic fixture marker and entry types.
        """
        normalized = _normalize_relative_path(logical_path, field_name="logical_path")
        return self._declared_state_class(normalized)

    def path_spec(self, logical_path: str) -> WorkspacePathSpec:
        """Return the explicit declaration that owns one logical path.

        Lifecycle and persistence callers use this to consume the same
        requiredness and state-class contract as inventory without duplicating
        declaration matching rules.
        """
        normalized = _normalize_relative_path(logical_path, field_name="logical_path")
        return self._declared_spec(normalized)

    def _validated_root(self) -> Path:
        root = self.config.identity.root
        try:
            root_stat = root.lstat()
        except OSError as exc:
            raise WorkspaceStateError("workspace root is not readable") from exc
        if stat.S_ISLNK(root_stat.st_mode):
            raise UnsupportedWorkspaceEntryError("workspace root must not be a symlink")
        if not stat.S_ISDIR(root_stat.st_mode):
            raise WorkspaceStateError("workspace root must be a directory")
        self._assert_no_symlink_components(root)
        try:
            resolved_root = root.resolve(strict=True)
        except OSError as exc:
            raise WorkspaceStateError("workspace root cannot be resolved") from exc
        if self.config.identity.root_kind is WorkspaceRootKind.SYNTHETIC_FIXTURE:
            marker = resolved_root / self.config.identity.synthetic_marker
            try:
                marker_stat = marker.lstat()
            except OSError as exc:
                raise WorkspaceStateError("synthetic workspace marker is missing") from exc
            if stat.S_ISLNK(marker_stat.st_mode) or not stat.S_ISREG(marker_stat.st_mode):
                raise UnsupportedWorkspaceEntryError("synthetic workspace marker must be a regular file")
            try:
                if marker.read_bytes() != b"seraph-synthetic-workspace-v1\n":
                    raise WorkspaceStateError("synthetic workspace marker is invalid")
            except OSError as exc:
                raise WorkspaceStateError("synthetic workspace marker is not readable") from exc
        elif self.config.identity.root_kind is not WorkspaceRootKind.PRODUCTION:
            # WorkspaceIdentity currently validates the enum, but keep this
            # guard at the inventory boundary if another enum value is added.
            raise WorkspaceStateError("workspace root kind is not supported")
        return resolved_root

    @staticmethod
    def _assert_no_symlink_components(path: Path) -> None:
        current = Path(path.anchor)
        for component in path.parts[1:]:
            current /= component
            try:
                if stat.S_ISLNK(current.lstat().st_mode):
                    raise UnsupportedWorkspaceEntryError("workspace root has a symlinked parent")
            except FileNotFoundError as exc:
                raise WorkspaceStateError("workspace root is not readable") from exc

    def _inventory_entries(
        self,
        root: Path,
        *,
        missing_declared_paths: list[str] | None = None,
    ) -> list[_Entry]:
        for spec in self.config.declared_paths:
            candidate = root / spec.logical_path
            self._assert_within_root(root, candidate, spec.logical_path)
            try:
                candidate_stat = candidate.lstat()
            except FileNotFoundError as exc:
                if (
                    self.config.identity.root_kind is WorkspaceRootKind.PRODUCTION
                    and missing_declared_paths is not None
                ):
                    if spec.state_class in {
                        WorkspaceStateClass.SECRET,
                        WorkspaceStateClass.SECRET_RECOVERY,
                    } and spec.required:
                        raise WorkspaceStateError(
                            f"required secret workspace path is missing: {spec.logical_path}"
                        ) from exc
                    missing_declared_paths.append(spec.logical_path)
                    continue
                raise WorkspaceStateError(f"declared workspace path is missing: {spec.logical_path}") from exc
            except OSError as exc:
                raise WorkspaceStateError(f"declared workspace path is missing: {spec.logical_path}") from exc
            if stat.S_ISLNK(candidate_stat.st_mode):
                raise UnsupportedWorkspaceEntryError(f"declared path must not be a symlink: {spec.logical_path}")

        entries: list[_Entry] = []
        total_bytes = [0]
        self._walk(root, root, entries, total_bytes=total_bytes)
        entries.sort(key=lambda entry: entry.logical_path)
        return entries

    def _walk(
        self,
        root: Path,
        current: Path,
        entries: list[_Entry],
        *,
        total_bytes: list[int],
    ) -> None:
        try:
            current_stat = current.lstat()
        except OSError as exc:
            raise WorkspaceStateError("workspace entry is not readable") from exc
        if stat.S_ISLNK(current_stat.st_mode):
            raise UnsupportedWorkspaceEntryError("symlink entries are not allowed")
        relative = current.relative_to(root)
        logical_path = "" if relative == Path(".") else relative.as_posix()
        if "\\" in logical_path:
            raise WorkspaceStateError("workspace paths containing backslashes are ambiguous")
        if logical_path:
            self._check_depth(logical_path)

        if stat.S_ISDIR(current_stat.st_mode):
            if logical_path:
                self._reserve_entry(entries, logical_path)
                state_class = self._classify(root, logical_path)
                entries.append(
                    self._make_entry(
                        logical_path,
                        current,
                        state_class,
                        file_type="directory",
                        mode=stat.S_IMODE(current_stat.st_mode),
                        size_bytes=0,
                        total_bytes=total_bytes,
                    )
                )
            children = self._bounded_children(current, logical_path, entries)
            for child in children:
                try:
                    if stat.S_ISLNK(child.lstat().st_mode):
                        raise UnsupportedWorkspaceEntryError("symlink entries are not allowed")
                except OSError as exc:
                    raise WorkspaceStateError("workspace entry is not readable") from exc
                self._assert_within_root(root, child, child.name)
                self._walk(root, child, entries, total_bytes=total_bytes)
            return

        if not stat.S_ISREG(current_stat.st_mode):
            raise UnsupportedWorkspaceEntryError(f"unsupported workspace entry type: {logical_path}")
        self._reserve_entry(entries, logical_path)
        self._account_file_size(total_bytes, current_stat.st_size, logical_path)
        state_class = self._classify(root, logical_path)
        entries.append(
            self._make_entry(
                logical_path,
                current,
                state_class,
                file_type="sqlite" if logical_path == self.config.database_path else "file",
                mode=stat.S_IMODE(current_stat.st_mode),
                size_bytes=current_stat.st_size,
                total_bytes=total_bytes,
            )
        )

    def _reserve_entry(self, entries: list[_Entry], logical_path: str) -> None:
        observed = len(entries) + 1
        if observed > self.config.max_entries:
            raise WorkspaceInventoryLimitError(
                "max_entries",
                self.config.max_entries,
                observed,
            )

    def _check_depth(self, logical_path: str) -> None:
        observed = len(PurePosixPath(logical_path).parts)
        if observed > self.config.max_depth:
            raise WorkspaceInventoryLimitError(
                "max_depth",
                self.config.max_depth,
                observed,
            )

    def _bounded_children(
        self,
        current: Path,
        logical_path: str,
        entries: list[_Entry],
    ) -> list[Path]:
        remaining = self.config.max_entries - len(entries)
        if remaining < 0:
            raise WorkspaceInventoryLimitError(
                "max_entries",
                self.config.max_entries,
                len(entries),
            )
        children: list[Path] = []
        try:
            for child in current.iterdir():
                children.append(child)
                if len(children) > remaining:
                    raise WorkspaceInventoryLimitError(
                        "max_entries",
                        self.config.max_entries,
                        len(entries) + len(children),
                    )
        except WorkspaceInventoryLimitError:
            raise
        except OSError as exc:
            raise WorkspaceStateError(
                f"workspace directory is not readable: {logical_path}"
            ) from exc
        return sorted(children, key=lambda child: child.name)

    def _account_file_size(
        self,
        total_bytes: list[int],
        size_bytes: int,
        logical_path: str,
    ) -> None:
        if size_bytes < 0:
            raise WorkspaceStateError(f"workspace file size is invalid: {logical_path}")
        if size_bytes > self.config.max_file_bytes:
            raise WorkspaceInventoryLimitError(
                "max_file_bytes",
                self.config.max_file_bytes,
                size_bytes,
            )
        observed_total = total_bytes[0] + size_bytes
        if observed_total > self.config.max_total_bytes:
            raise WorkspaceInventoryLimitError(
                "max_total_bytes",
                self.config.max_total_bytes,
                observed_total,
            )
        total_bytes[0] = observed_total

    def _classify(self, root: Path, logical_path: str) -> WorkspaceStateClass:
        try:
            return self.classify_path(logical_path)
        except UnknownWorkspacePathError:
            pass

        # Preserve the more useful writable-path detail for full fixture
        # inventories while keeping the public classifier deterministic.
        try:
            mode = stat.S_IMODE((root / logical_path).lstat().st_mode)
        except OSError as exc:
            raise UnknownWorkspacePathError(f"unknown workspace path: {logical_path}") from exc
        writable = bool(mode & 0o222)
        qualifier = "unknown writable" if writable else "unknown"
        raise UnknownWorkspacePathError(f"{qualifier} workspace path: {logical_path}")

    def _declared_state_class(self, logical_path: str) -> WorkspaceStateClass:
        return self._declared_spec(logical_path).state_class

    def _declared_spec(self, logical_path: str) -> WorkspacePathSpec:
        direct = [
            spec
            for spec in self.config.declared_paths
            if logical_path == spec.logical_path or logical_path.startswith(f"{spec.logical_path}/")
        ]
        if direct:
            classes = {spec.state_class for spec in direct}
            if len(classes) != 1:
                raise AmbiguousWorkspaceRootsError(f"workspace path has multiple owners: {logical_path}")
            return direct[0]

        descendants = [
            spec
            for spec in self.config.declared_paths
            if spec.logical_path.startswith(f"{logical_path}/")
        ]
        if descendants:
            classes = {spec.state_class for spec in descendants}
            if len(classes) != 1:
                raise AmbiguousWorkspaceRootsError(f"workspace directory has multiple owners: {logical_path}")
            return descendants[0]
        raise UnknownWorkspacePathError(f"unknown workspace path: {logical_path}")

    def _make_entry(
        self,
        logical_path: str,
        path: Path,
        state_class: WorkspaceStateClass,
        *,
        file_type: str,
        mode: int,
        size_bytes: int,
        total_bytes: list[int],
    ) -> _Entry:
        if (
            file_type in {"directory", "sqlite"}
            or state_class in {WorkspaceStateClass.SECRET_RECOVERY, WorkspaceStateClass.SECRET}
        ):
            digest_scope = "redacted_metadata"
            sha256 = _sha256_json(
                {
                    "logical_path": logical_path,
                    "state_class": state_class.value,
                    "file_type": file_type,
                    "mode": mode,
                    "size_bytes": size_bytes,
                }
            )
        else:
            digest_scope = "content"
            sha256 = self._hash_file(
                path,
                expected_size=size_bytes,
                total_bytes=total_bytes,
            )
        return _Entry(
            logical_path=logical_path,
            path=path,
            state_class=state_class,
            required=self._declared_spec(logical_path).required,
            file_type=file_type,
            mode=mode,
            size_bytes=size_bytes,
            sha256=sha256,
            digest_scope=digest_scope,
        )

    def _hash_file(
        self,
        path: Path,
        *,
        expected_size: int,
        total_bytes: list[int],
    ) -> str:
        digest = hashlib.sha256()
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
            )
            descriptor_stat = os.fstat(descriptor)
            if not stat.S_ISREG(descriptor_stat.st_mode):
                os.close(descriptor)
                raise UnsupportedWorkspaceEntryError("workspace file changed to a non-regular entry")
            if descriptor_stat.st_size > self.config.max_file_bytes:
                os.close(descriptor)
                raise WorkspaceInventoryLimitError(
                    "max_file_bytes",
                    self.config.max_file_bytes,
                    descriptor_stat.st_size,
                )
            bytes_read = 0
            extra_bytes_accounted = 0
            with os.fdopen(descriptor, "rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    bytes_read += len(chunk)
                    if bytes_read > self.config.max_file_bytes:
                        raise WorkspaceInventoryLimitError(
                            "max_file_bytes",
                            self.config.max_file_bytes,
                            bytes_read,
                        )
                    extra_bytes = max(0, bytes_read - expected_size)
                    if extra_bytes > extra_bytes_accounted:
                        delta = extra_bytes - extra_bytes_accounted
                        observed_total = total_bytes[0] + delta
                        if observed_total > self.config.max_total_bytes:
                            raise WorkspaceInventoryLimitError(
                                "max_total_bytes",
                                self.config.max_total_bytes,
                                observed_total,
                            )
                        total_bytes[0] = observed_total
                        extra_bytes_accounted = extra_bytes
                    digest.update(chunk)
        except WorkspaceStateError:
            raise
        except (OSError, ValueError) as exc:
            raise WorkspaceStateError("workspace file is not readable") from exc
        return digest.hexdigest()

    @staticmethod
    def _assert_within_root(root: Path, candidate: Path, logical_path: str) -> None:
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise WorkspaceStateError(f"workspace path escapes root: {logical_path}") from exc

    def _sqlite_inventory(self, database_path: Path) -> dict[str, Any]:
        if not database_path.is_file() or database_path.is_symlink():
            raise WorkspaceStateError("database_path must be a regular, non-symlink file")
        uri = f"file:{quote(str(database_path), safe='/')}?mode=ro"
        try:
            connection = sqlite3.connect(uri, uri=True)
        except sqlite3.Error as exc:
            raise WorkspaceStateError("database_path is not a readable SQLite database") from exc
        try:
            objects = connection.execute(
                "SELECT type, name, tbl_name, sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' "
                "AND type IN ('table', 'index', 'trigger', 'view') "
                "ORDER BY type, name LIMIT ?",
                (self.config.max_entries + 1,),
            ).fetchall()
            if len(objects) > self.config.max_entries:
                raise WorkspaceInventoryLimitError(
                    "max_entries",
                    self.config.max_entries,
                    len(objects),
                )
            actual_objects = {(str(row[0]), str(row[1])) for row in objects}
            expected_by_name = {
                spec.name: spec for spec in self.config.expected_database_objects
            }
            expected_names = set(expected_by_name)
            actual_names = {name for _, name in actual_objects}
            if expected_by_name:
                unknown_objects = sorted(actual_names - expected_names)
                if unknown_objects:
                    raise UnknownWorkspacePathError(
                        "unknown SQLite schema objects: " + ", ".join(unknown_objects)
                    )
                missing_objects = sorted(expected_names - actual_names)
                if missing_objects:
                    raise WorkspaceStateError(
                        "expected SQLite schema objects are missing: " + ", ".join(missing_objects)
                    )
                type_mismatches = sorted(
                    f"{name} (expected {expected_by_name[name].object_type}, found {object_type})"
                    for object_type, name in actual_objects
                    if expected_by_name[name].object_type != object_type
                )
                if type_mismatches:
                    raise WorkspaceStateError(
                        "SQLite schema object type mismatch: " + ", ".join(type_mismatches)
                    )
            schema_objects: list[dict[str, str]] = []
            tables: list[dict[str, Any]] = []
            for object_type, name, table_name, sql in objects:
                object_name = str(name)
                object_type = str(object_type)
                object_spec = expected_by_name.get(object_name)
                if object_spec is None:
                    if self.config.identity.root_kind is not WorkspaceRootKind.PRODUCTION:
                        raise UnknownWorkspacePathError(
                            "unknown SQLite schema objects: " + object_name
                        )
                    # Production schemas evolve with the SQLModel metadata and
                    # migrations.  When no explicit schema contract is
                    # supplied, inventory the schema as metadata and use the
                    # conservative table/trigger versus derived index/view
                    # split.  No row values or SQL definitions are returned.
                    inferred_state = (
                        WorkspaceStateClass.DERIVED
                        if object_type in {"index", "view"}
                        else WorkspaceStateClass.CANONICAL
                    )
                    try:
                        object_spec = WorkspaceDatabaseObjectSpec(
                            object_name,
                            object_type,
                            inferred_state,
                        )
                    except WorkspaceStateError as exc:
                        raise UnknownWorkspacePathError(
                            "unknown SQLite schema objects: " + object_name
                        ) from exc
                object_payload = {
                    "type": object_type,
                    "name": object_name,
                    "table_name": str(table_name or ""),
                    "sql_sha256": _sha256_bytes(str(sql or "").encode("utf-8")),
                    "state_class": object_spec.state_class.value,
                    "redaction_policy": object_spec.redaction_policy,
                }
                schema_objects.append(
                    {
                        "type": object_payload["type"],
                        "name": object_payload["name"],
                        "state_class": object_payload["state_class"],
                        "redaction_policy": object_payload["redaction_policy"],
                        "sha256": _sha256_json(object_payload),
                    }
                )
                if object_type != "table":
                    continue
                columns = connection.execute(
                    f'PRAGMA table_info({self._quote_identifier(object_name)})'
                ).fetchall()
                column_payload = [
                    {
                        "cid": int(row[0]),
                        "name": str(row[1]),
                        "type": str(row[2] or ""),
                        "not_null": int(row[3]),
                        "default_sha256": _sha256_json(row[4]) if row[4] is not None else None,
                        "primary_key_position": int(row[5]),
                    }
                    for row in columns
                ]
                index_payload = []
                for index_row in connection.execute(
                    f"PRAGMA index_list({self._quote_identifier(object_name)})"
                ).fetchall():
                    index_name = str(index_row[1])
                    index_columns = [
                        {
                            "sequence": int(info[0]),
                            "column_number": int(info[1]),
                            "name": str(info[2] or ""),
                        }
                        for info in connection.execute(
                            f"PRAGMA index_info({self._quote_identifier(index_name)})"
                        ).fetchall()
                    ]
                    index_payload.append(
                        {
                            "name": index_name,
                            "unique": int(index_row[2]),
                            "origin": str(index_row[3] or ""),
                            "partial": int(index_row[4]),
                            "columns": index_columns,
                        }
                    )
                index_payload.sort(key=lambda value: value["name"])
                foreign_key_payload = [
                    {
                        "id": int(row[0]),
                        "sequence": int(row[1]),
                        "table": str(row[2]),
                        "from": str(row[3]),
                        "to": str(row[4] or ""),
                        "on_update": str(row[5]),
                        "on_delete": str(row[6]),
                        "match": str(row[7]),
                    }
                    for row in connection.execute(
                        f"PRAGMA foreign_key_list({self._quote_identifier(object_name)})"
                    ).fetchall()
                ]
                row_count = int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {self._quote_identifier(object_name)}"
                    ).fetchone()[0]
                )
                tables.append(
                    {
                        "name": object_name,
                        "state_class": object_spec.state_class.value,
                        "redaction_policy": object_spec.redaction_policy,
                        "row_count": row_count,
                        "schema_fingerprint": _sha256_json(
                            {
                                "definition": object_payload,
                                "columns": column_payload,
                                "indexes": index_payload,
                                "foreign_keys": foreign_key_payload,
                            }
                        ),
                    }
                )
        except sqlite3.Error as exc:
            raise WorkspaceStateError("SQLite schema inventory failed") from exc
        finally:
            connection.close()

        schema_objects.sort(key=lambda item: (item["type"], item["name"]))
        tables.sort(key=lambda item: item["name"])
        return {
            "logical_path": self.config.database_path,
            "schema_fingerprint": _sha256_json(schema_objects),
            "schema_object_count": len(schema_objects),
            "table_count": len(tables),
            "row_count": sum(table["row_count"] for table in tables),
            "tables": tables,
        }

    @staticmethod
    def _quote_identifier(identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'


__all__ = [
    "AmbiguousWorkspaceRootsError",
    "ExternalReferencePolicy",
    "ExternalReferenceSpec",
    "UnknownWorkspacePathError",
    "UnsupportedWorkspaceEntryError",
    "WorkspaceConfig",
    "WorkspaceDatabaseObjectSpec",
    "WorkspaceIdentity",
    "WorkspaceInventoryLimitError",
    "WorkspacePathSpec",
    "WorkspaceRootKind",
    "WorkspaceStateClass",
    "WorkspaceStateError",
    "WorkspaceStateRegistry",
    "DEFAULT_MAX_INVENTORY_ENTRIES",
    "DEFAULT_MAX_INVENTORY_DEPTH",
    "DEFAULT_MAX_INVENTORY_TOTAL_BYTES",
    "DEFAULT_MAX_INVENTORY_FILE_BYTES",
    "canonical_workspace_config",
    "canonical_workspace_database_path",
    "canonical_workspace_inventory",
    "canonical_workspace_registry",
    "canonical_workspace_root",
    "production_workspace_inventory",
]


# These are the only runtime-owned workspace roots.  The paths are logical
# and deliberately do not include backup or restore-staging siblings: those
# are future lifecycle surfaces and must never become active workspace owners.
_RUNTIME_PATH_SPECS = (
    ("seraph.db", WorkspaceStateClass.CANONICAL),
    ("soul.md", WorkspaceStateClass.CANONICAL),
    ("artifacts", WorkspaceStateClass.CANONICAL),
    ("extensions", WorkspaceStateClass.CANONICAL),
    ("skills", WorkspaceStateClass.CANONICAL),
    ("workflows", WorkspaceStateClass.CANONICAL),
    ("runbooks", WorkspaceStateClass.CANONICAL),
    ("plans", WorkspaceStateClass.CANONICAL),
    ("reports", WorkspaceStateClass.CANONICAL),
    ("notes", WorkspaceStateClass.CANONICAL),
    ("mcp-servers.json", WorkspaceStateClass.CANONICAL),
    ("stdio-proxies.json", WorkspaceStateClass.CANONICAL),
    ("extensions-state.json", WorkspaceStateClass.CANONICAL),
    ("starter-packs.json", WorkspaceStateClass.CANONICAL),
    ("model-fabric-settings.json", WorkspaceStateClass.CANONICAL),
    ("screen-analysis-settings.json", WorkspaceStateClass.CANONICAL),
    ("daemon-status.json", WorkspaceStateClass.DERIVED),
    ("local-runtime-profile-receipts", WorkspaceStateClass.DERIVED),
    ("lance", WorkspaceStateClass.DERIVED),
    (".seraph-extension-snapshots", WorkspaceStateClass.DERIVED),
    (".seraph-workspace-maintenance.lock", WorkspaceStateClass.DERIVED),
    ("seraph.db-wal", WorkspaceStateClass.DERIVED),
    ("seraph.db-shm", WorkspaceStateClass.DERIVED),
    ("seraph.db-journal", WorkspaceStateClass.DERIVED),
    ("cache", WorkspaceStateClass.CACHE),
    ("tmp", WorkspaceStateClass.DISPOSABLE),
    (".vault-key", WorkspaceStateClass.SECRET_RECOVERY),
    ("google_calendar_token.json", WorkspaceStateClass.SECRET),
)
_OPTIONAL_RUNTIME_SECRET_PATHS = frozenset({"google_calendar_token.json"})


def canonical_workspace_root(root: str | os.PathLike[str]) -> Path:
    """Resolve the one workspace root used by runtime persistence paths."""
    candidate = Path(root).expanduser()
    if not candidate.is_absolute():
        raise WorkspaceStateError("canonical workspace root must be absolute")
    try:
        metadata = candidate.lstat()
    except FileNotFoundError:
        return candidate.resolve(strict=False)
    except OSError as exc:
        raise WorkspaceStateError("canonical workspace root is not readable") from exc
    if stat.S_ISLNK(metadata.st_mode):
        raise UnsupportedWorkspaceEntryError("canonical workspace root must not be a symlink")
    if not stat.S_ISDIR(metadata.st_mode):
        raise WorkspaceStateError("canonical workspace root must be a directory")
    return candidate.resolve(strict=True)


def canonical_workspace_config(root: str | os.PathLike[str]) -> WorkspaceConfig:
    """Build the shared runtime classification config for one root."""
    resolved_root = canonical_workspace_root(root)
    return WorkspaceConfig(
        identity=WorkspaceIdentity(
            workspace_id="workspace-runtime",
            root=resolved_root,
            root_kind=WorkspaceRootKind.PRODUCTION,
        ),
        declared_paths=tuple(
            WorkspacePathSpec(
                path,
                state_class,
                required=path not in _OPTIONAL_RUNTIME_SECRET_PATHS,
            )
            for path, state_class in _RUNTIME_PATH_SPECS
        ),
        database_path="seraph.db",
    )


def canonical_workspace_registry(root: str | os.PathLike[str]) -> WorkspaceStateRegistry:
    """Return the registry shared by canonical persistence callers."""
    return WorkspaceStateRegistry(canonical_workspace_config(root))


def production_workspace_inventory(root: str | os.PathLike[str]) -> dict[str, Any]:
    """Build an operator receipt for the explicitly configured production root."""
    try:
        registry = canonical_workspace_registry(root)
    except WorkspaceStateError as exc:
        reason = _inventory_failure_reason(exc)
        return {
            "schema_version": "seraph.workspace.inventory.v1",
            "workspace_id": "workspace-runtime",
            "root_kind": WorkspaceRootKind.PRODUCTION.value,
            "status": "blocked",
            "operator_status": "workspace_inventory_blocked",
            "manifest": None,
            "manifest_sha256": None,
            "missing_declared_paths": [],
            "inventory_limits": {
                "max_entries": DEFAULT_MAX_INVENTORY_ENTRIES,
                "max_depth": DEFAULT_MAX_INVENTORY_DEPTH,
                "max_total_bytes": DEFAULT_MAX_INVENTORY_TOTAL_BYTES,
                "max_file_bytes": DEFAULT_MAX_INVENTORY_FILE_BYTES,
            },
            "limit_breach": None,
            "degraded_reasons": [],
            "blocked_reasons": [reason],
            "reason_code": reason,
            "secret_values_included": False,
            "receipt_id": _inventory_receipt_id(
                "workspace-runtime",
                WorkspaceRootKind.PRODUCTION.value,
                "blocked",
                [reason],
            ),
        }
    return registry.build_inventory_receipt()


def canonical_workspace_inventory(root: str | os.PathLike[str]) -> dict[str, Any]:
    """Compatibility name for the production inventory receipt boundary."""
    return production_workspace_inventory(root)


def canonical_workspace_database_path(root: str | os.PathLike[str]) -> Path:
    """Resolve ``seraph.db`` only after the registry proves canonical ownership."""
    resolved_root = canonical_workspace_root(root)
    registry = canonical_workspace_registry(resolved_root)
    if registry.classify_path("seraph.db") is not WorkspaceStateClass.CANONICAL:
        raise AmbiguousWorkspaceRootsError("seraph.db is not canonically owned")
    return resolved_root / "seraph.db"
