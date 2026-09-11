"""Governed capability-pack v2 contracts and local lifecycle receipts.

The extension registry predates the capability-pack lifecycle contract and is
still the compatibility surface for existing bundled and workspace manifests.
This module is the narrow v2 seam used by a pack installer/reviewer.  It keeps
the package immutable after review, stores one atomic active-version pointer,
and provides deterministic canary fixtures.  It deliberately contains no
provider transport, executable hook loader, marketplace client, or authority
granting logic.

Pack signatures in this milestone are provenance/integrity metadata only.  A
digest and a locally recomputable ``seraph-sha256-v1`` value cannot establish
publisher trust; activation always requires an exact local review binding.
"""

from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile
import tempfile
import threading
from typing import Any, Callable, Iterable, Iterator, Mapping
import zipfile
from urllib.parse import urlparse

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fails closed at runtime.
    fcntl = None

import yaml
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from src.extensions.layout import reject_symlink_entries, resolve_package_reference
from src.extensions.state import state_path as extension_state_path


CAPABILITY_PACK_SCHEMA_VERSION = 2
CAPABILITY_PACK_SCHEMA_V1 = 1
CAPABILITY_PACK_LIFECYCLE_SCHEMA = "seraph.capability-pack.lifecycle.v1"
CAPABILITY_PACK_CANARY_SCHEMA = "seraph.capability-pack.canary.v1"
CAPABILITY_PACK_SIGNATURE_ALGORITHM = "seraph-sha256-v1"
CAPABILITY_PACK_ROUTE = "#743 durable admission -> #747 capability runtime"
CAPABILITY_PACK_EXECUTION_SCHEMA = "seraph.capability-pack.execution.v1"
CAPABILITY_PACK_LOCAL_EXECUTION_SCHEMA = "seraph.capability-pack.local-execution.v1"
CAPABILITY_PACK_RUNTIME_BLOCKED_REASON = "governed_runtime_adapter_unavailable"

MAX_PACK_MEMBER_BYTES = 100 * 1024 * 1024
MAX_PACK_TOTAL_BYTES = 250 * 1024 * 1024
MAX_PACK_MEMBERS = 10_000
MAX_RUNTIME_SECONDS = 86_400
MAX_ARTIFACT_BYTES = 100 * 1024 * 1024
MAX_INFERENCE_COST_MICROUSD = 1_000_000_000
MAX_PACK_JOBS = 256
MAX_LOCAL_SOURCE_BYTES = 2 * 1024 * 1024

_PACK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+\-]{0,255}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_INFERENCE_PRIORITY_RANK = {
    "interactive_chat": 100,
    "approved_operator": 80,
    "accepted_scheduled_goal": 60,
    "reports_research_memory": 40,
    "screenshot_background": 20,
}


class InferencePriority(str, Enum):
    """Provider-neutral queue priority labels shared by governed runtimes."""

    INTERACTIVE_CHAT = "interactive_chat"
    APPROVED_OPERATOR = "approved_operator"
    ACCEPTED_SCHEDULED_GOAL = "accepted_scheduled_goal"
    REPORTS_RESEARCH_MEMORY = "reports_research_memory"
    SCREENSHOT_BACKGROUND = "screenshot_background"


_SAFE_PRIORITY_ALIASES = {
    "interactive": InferencePriority.INTERACTIVE_CHAT,
    "chat": InferencePriority.INTERACTIVE_CHAT,
    "onboarding": InferencePriority.INTERACTIVE_CHAT,
    "interactive_chat": InferencePriority.INTERACTIVE_CHAT,
    "approved_operator": InferencePriority.APPROVED_OPERATOR,
    "operator": InferencePriority.APPROVED_OPERATOR,
    "high": InferencePriority.ACCEPTED_SCHEDULED_GOAL,
    "scheduled": InferencePriority.ACCEPTED_SCHEDULED_GOAL,
    "scheduled_goal": InferencePriority.ACCEPTED_SCHEDULED_GOAL,
    "accepted_scheduled_goal": InferencePriority.ACCEPTED_SCHEDULED_GOAL,
    "normal": InferencePriority.REPORTS_RESEARCH_MEMORY,
    "report": InferencePriority.REPORTS_RESEARCH_MEMORY,
    "research": InferencePriority.REPORTS_RESEARCH_MEMORY,
    "memory": InferencePriority.REPORTS_RESEARCH_MEMORY,
    "reports_research_memory": InferencePriority.REPORTS_RESEARCH_MEMORY,
    "screenshot": InferencePriority.SCREENSHOT_BACKGROUND,
    "background": InferencePriority.SCREENSHOT_BACKGROUND,
    "screenshot_background": InferencePriority.SCREENSHOT_BACKGROUND,
}
_HOOK_STATES = {"required", "optional", "none"}
_PRIVILEGED_TOOL_NAMES = {
    "exec",
    "execute_code",
    "run_command",
    "shell",
    "shell_execute",
    "sudo",
    "start_process",
    "list_processes",
    "read_process_output",
    "stop_process",
    "process",
    "processes",
}
_SAFE_FILESYSTEM_SCOPES = {
    "workspace_read",
    "workspace_write",
    "artifact_read",
    "artifact_write",
    "temporary_read",
    "temporary_write",
}
_SECRET_VALUE_MARKERS = (
    "sk-",
    "bearer ",
    "api_key=",
    "apikey=",
    "secret=",
    "token=",
    "password=",
)
_MIGRATED_DEFAULT_LIFECYCLE = {
    "hooks": {
        "activate": "required",
        "pause": "required",
        "update": "required",
        "revoke": "required",
        "uninstall": "required",
    },
    "artifact_migration": "preserve",
    "revoke_running_jobs": "cancel_at_safe_checkpoint",
}


class CapabilityPackError(ValueError):
    """Raised when pack validation or lifecycle policy fails."""


class CapabilityPackManifestError(CapabilityPackError):
    """Raised when a v2 manifest cannot be parsed."""

    def __init__(self, source: str, message: str, *, errors: list[dict[str, Any]] | None = None):
        super().__init__(f"{source}: {message}")
        self.source = source
        self.message = message
        self.errors = errors or []


class CapabilityPackLifecycleError(CapabilityPackError):
    """Raised when a lifecycle transition would violate a reviewed binding."""


def _normalize_strings(values: Iterable[Any] | None, *, field_name: str) -> list[str]:
    if values is None:
        return []
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{field_name} must be a list of strings")
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"{field_name} must contain non-empty strings")
        value = raw.strip()
        if value not in seen:
            normalized.append(value)
            seen.add(value)
    return normalized


def _validate_reference(value: str, *, field_name: str) -> str:
    value = value.strip()
    if not value or len(value) > 256 or not _REFERENCE_RE.fullmatch(value):
        raise ValueError(f"{field_name} contains an unsafe reference")
    return value


def _validate_secret_reference(value: str) -> str:
    """Accept names/opaque refs while rejecting values that look secret-like."""

    normalized = _validate_reference(value, field_name="secrets")
    lowered = normalized.lower()
    if any(marker in lowered for marker in _SECRET_VALUE_MARKERS):
        raise ValueError("secrets must contain references, never inline secret values")
    return normalized


def _is_privileged_tool_reference(value: str) -> bool:
    """Reject process/shell aliases before they can become pack authority."""

    normalized = value.lower().replace("-", "_")
    leaf = normalized.rsplit(".", 1)[-1]
    if leaf in _PRIVILEGED_TOOL_NAMES:
        return True
    # Namespaced aliases such as ``native.process.start`` and variants such as
    # ``process_manager`` must remain behind the governed tool policy too.
    parts = [part for part in re.split(r"[.:/]", normalized) if part]
    return any(
        part in {"exec", "shell", "sudo", "process", "processes"}
        or "process" in part
        for part in parts
    )


def _validate_contribution_reference(value: str, *, field_name: str) -> str:
    """Validate a declarative contribution identity or package-relative path."""

    normalized = _validate_reference(value, field_name=field_name)
    if field_name == "capabilities":
        return normalized
    if "\\" in normalized:
        raise ValueError(f"{field_name} path must use POSIX separators")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts or not path.suffix:
        raise ValueError(f"{field_name} path must point to a package file")
    expected_prefix = _CONTRIBUTION_PREFIXES.get(field_name)
    if expected_prefix and not normalized.startswith(expected_prefix):
        raise ValueError(f"{field_name} path must live under {expected_prefix}")
    return normalized


def _validate_pack_id(value: str) -> str:
    value = value.strip()
    if not _PACK_ID_RE.fullmatch(value):
        raise ValueError("id must use lowercase letters, numbers, dots, hyphens, or underscores")
    if value[-1] in ".-_":
        raise ValueError("id must not end with punctuation")
    return value


def _validate_goal_id(value: str) -> str:
    if not isinstance(value, str):
        raise CapabilityPackLifecycleError("goal_id must be a non-empty identifier")
    normalized = value.strip()
    if not normalized or len(normalized) > 256 or any(character in normalized for character in "\r\n\x00"):
        raise CapabilityPackLifecycleError("goal_id must be a non-empty identifier")
    return normalized


def _validate_goal_snapshot_binding(
    snapshot: Mapping[str, Any] | str | None,
    *,
    goal_id: str,
    owner_principal_id: str,
    session_id: str,
) -> dict[str, Any]:
    """Require a server-issued, current goal row before writing a snapshot.

    The public API fills this shape from the canonical ``goals`` table.  The
    lifecycle still validates every field at the durable execution boundary so
    a caller cannot substitute another goal, operator, session, revision, or
    terminal goal after the API check.
    """

    if not isinstance(snapshot, Mapping):
        raise CapabilityPackLifecycleError("goal snapshot must be a canonical persisted goal mapping")
    snapshot_goal_id = _validate_goal_id(str(snapshot.get("goal_id") or ""))
    if snapshot_goal_id != goal_id:
        raise CapabilityPackLifecycleError("goal snapshot identity conflicts with the pinned goal")
    revision = snapshot.get("revision", snapshot.get("goal_revision"))
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise CapabilityPackLifecycleError("goal snapshot revision is invalid")
    if str(snapshot.get("status") or "").strip().lower() != "active":
        raise CapabilityPackLifecycleError("goal snapshot requires an active canonical goal")
    snapshot_owner = _validate_goal_id(str(snapshot.get("owner_principal_id") or ""))
    snapshot_session = _validate_goal_id(str(snapshot.get("session_id") or ""))
    if snapshot_owner != owner_principal_id or snapshot_session != session_id:
        raise CapabilityPackLifecycleError("goal snapshot owner or session identity conflicts with the authenticated operator")
    if str(snapshot.get("canonical_source") or "").strip() != "goals":
        raise CapabilityPackLifecycleError("goal snapshot is missing the canonical persisted-goal source")
    return {
        **dict(snapshot),
        "goal_id": snapshot_goal_id,
        "revision": revision,
        "owner_principal_id": snapshot_owner,
        "session_id": snapshot_session,
        "status": "active",
        "canonical_source": "goals",
    }


class PackPublisher(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    provenance: str = "local-reviewed"

    @field_validator("name", "provenance")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value


class PackSignature(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: str = "unsigned-local"
    signer: str | None = None
    algorithm: str | None = None
    digest: str | None = None

    @field_validator("state", "signer", "algorithm", "digest")
    @classmethod
    def _normalize_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("digest")
    @classmethod
    def _validate_digest(cls, value: str | None) -> str | None:
        if value is not None and not _DIGEST_RE.fullmatch(value.lower()):
            raise ValueError("digest must be a lowercase SHA-256 hex digest")
        return value.lower() if value else value

    @model_validator(mode="after")
    def _validate_state(self) -> "PackSignature":
        if self.state not in {"unsigned-local", "integrity-checked", "cryptographic-unavailable"}:
            raise ValueError("cryptographic publisher verification is unavailable in this milestone")
        if self.algorithm and self.algorithm != CAPABILITY_PACK_SIGNATURE_ALGORITHM:
            raise ValueError("unsupported pack signature algorithm")
        if self.state == "integrity-checked" and (self.algorithm != CAPABILITY_PACK_SIGNATURE_ALGORITHM or not self.digest):
            raise ValueError("integrity-checked signatures require the local digest and algorithm")
        return self


class PackCompatibility(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seraph: str

    @field_validator("seraph")
    @classmethod
    def _validate_specifier(cls, value: str) -> str:
        value = value.strip()
        if not value or len(value) > 256:
            raise ValueError("must be non-empty and at most 256 characters")
        try:
            SpecifierSet(value)
        except InvalidSpecifier as exc:
            raise ValueError(f"invalid specifier: {value}") from exc
        return value

    def is_compatible_with(self, version: str) -> bool:
        try:
            return Version(version) in SpecifierSet(self.seraph)
        except InvalidVersion as exc:
            raise ValueError(f"invalid Seraph version: {version}") from exc


class PackDependency(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    version: str = "*"
    digest: str

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        return _validate_pack_id(value)

    @field_validator("version")
    @classmethod
    def _version(cls, value: str) -> str:
        value = value.strip()
        if len(value) > 256:
            raise ValueError("dependency version constraint is too long")
        if value == "*":
            return value
        try:
            SpecifierSet(value)
        except InvalidSpecifier as exc:
            raise ValueError(f"invalid dependency version: {value}") from exc
        return value

    @field_validator("digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value.lower()):
            raise ValueError("dependency digest must be a lowercase SHA-256 hex digest")
        return value.lower()


class PackContributions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capabilities: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    workflows: list[str] = Field(default_factory=list)
    prompts: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    reports: list[str] = Field(default_factory=list)
    evals: list[str] = Field(default_factory=list)
    runbooks: list[str] = Field(default_factory=list)

    @field_validator(
        "capabilities",
        "skills",
        "workflows",
        "prompts",
        "sources",
        "reports",
        "evals",
        "runbooks",
    )
    @classmethod
    def _references(cls, value: list[str], info: Any) -> list[str]:
        field_name = str(info.field_name)
        return [
            _validate_contribution_reference(item, field_name=field_name)
            for item in _normalize_strings(value, field_name=field_name)
        ]

    @model_validator(mode="after")
    def _unique_references(self) -> "PackContributions":
        references: list[str] = []
        for field_name in (
            "capabilities",
            "skills",
            "workflows",
            "prompts",
            "sources",
            "reports",
            "evals",
            "runbooks",
        ):
            references.extend(getattr(self, field_name))
        if len(references) != len(set(references)):
            raise ValueError("contribution references must be unique")
        return self


class PackAuthority(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tools: list[str] = Field(default_factory=list)
    filesystem: list[str] = Field(default_factory=list)
    network: bool = False
    secrets: list[str] = Field(default_factory=list)
    approval: str = "on_authority_expansion"

    @field_validator("tools", "filesystem", "secrets")
    @classmethod
    def _scopes(cls, value: list[str], info: Any) -> list[str]:
        field_name = str(info.field_name)
        values = _normalize_strings(value, field_name=field_name)
        if field_name == "secrets":
            return [_validate_secret_reference(item) for item in values]
        normalized = [_validate_reference(item, field_name=field_name) for item in values]
        if field_name == "tools" and any(_is_privileged_tool_reference(item) for item in normalized):
            raise ValueError("privileged process tools are not valid capability-pack authority")
        if field_name == "filesystem":
            for item in normalized:
                path = PurePosixPath(item)
                if path.is_absolute() or ".." in path.parts or "\\" in item:
                    raise ValueError("filesystem authority must be a bounded package scope")
                if "/" not in item and item not in _SAFE_FILESYSTEM_SCOPES:
                    raise ValueError("filesystem authority must use a bounded workspace/artifact scope")
        return normalized

    @model_validator(mode="after")
    def _unique_scopes(self) -> "PackAuthority":
        for field_name in ("tools", "filesystem", "secrets"):
            values = getattr(self, field_name)
            if len(values) != len(set(values)):
                raise ValueError(f"{field_name} must not contain duplicate scopes")
        return self

    @field_validator("network", mode="before")
    @classmethod
    def _strict_network(cls, value: Any) -> bool:
        if not isinstance(value, bool):
            raise ValueError("network must be a boolean")
        return value

    @field_validator("approval")
    @classmethod
    def _approval(cls, value: str) -> str:
        value = value.strip()
        if value not in {"never", "on_authority_expansion", "always"}:
            raise ValueError("approval must be never, on_authority_expansion, or always")
        return value

    @model_validator(mode="after")
    def _sensitive_authority_requires_review(self) -> "PackAuthority":
        if (self.network or self.secrets) and self.approval == "never":
            raise ValueError("network and secret authority require an explicit review policy")
        return self


class PackResources(BaseModel):
    model_config = ConfigDict(extra="forbid")

    inference_priority: InferencePriority = Field(...)
    max_inference_cost_microusd: int = Field(..., ge=0, le=MAX_INFERENCE_COST_MICROUSD)
    max_runtime_seconds: int = Field(default=300, gt=0, le=MAX_RUNTIME_SECONDS)
    max_artifact_bytes: int = Field(default=10 * 1024 * 1024, gt=0, le=MAX_ARTIFACT_BYTES)

    @field_validator("inference_priority", mode="before")
    @classmethod
    def _priority(cls, value: Any) -> InferencePriority:
        normalized = str(value or "").strip().lower().replace("-", "_")
        try:
            return _SAFE_PRIORITY_ALIASES[normalized]
        except KeyError as exc:
            raise ValueError(f"unknown inference priority: {value!r}") from exc

    @field_validator("max_inference_cost_microusd", "max_runtime_seconds", "max_artifact_bytes", mode="before")
    @classmethod
    def _integers(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("resource limits must be finite integers")
        return value


class PackDataPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    classes: list[str] = Field(default_factory=lambda: ["public"])
    egress: list[str] = Field(default_factory=list)

    @field_validator("classes", "egress")
    @classmethod
    def _values(cls, value: list[str], info: Any) -> list[str]:
        field_name = str(info.field_name)
        normalized = _normalize_strings(value, field_name=field_name)
        output: list[str] = []
        for item in normalized:
            item = _validate_reference(item, field_name=field_name)
            if field_name == "egress" and (
                re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", item)
                or item.startswith("//")
                or "\\" in item
                or ".." in PurePosixPath(item).parts
            ):
                raise ValueError("egress must use a named allow-list entry, not a URL or path")
            output.append(item)
        return output

    @model_validator(mode="after")
    def _network_egress(self) -> "PackDataPolicy":
        # An empty egress list is a deliberate local-only declaration.  The
        # authority model decides whether network access is requested.
        return self


class PackLifecycle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hooks: dict[str, str] = Field(default_factory=lambda: deepcopy(_MIGRATED_DEFAULT_LIFECYCLE["hooks"]))
    artifact_migration: str = "preserve"
    revoke_running_jobs: str = "cancel_at_safe_checkpoint"

    @field_validator("hooks")
    @classmethod
    def _hooks(cls, value: dict[str, str]) -> dict[str, str]:
        required = {"activate", "pause", "update", "revoke", "uninstall"}
        if set(value) != required:
            raise ValueError("lifecycle hooks must declare activate, pause, update, revoke, and uninstall")
        normalized: dict[str, str] = {}
        for name, state in value.items():
            if state not in _HOOK_STATES:
                raise ValueError("lifecycle hook values are declarative states; executable hooks are not allowed")
            normalized[name] = state
        return normalized

    @field_validator("artifact_migration")
    @classmethod
    def _artifact_migration(cls, value: str) -> str:
        if value not in {"preserve", "explicit_review", "none"}:
            raise ValueError("unsupported artifact migration policy")
        return value

    @field_validator("revoke_running_jobs")
    @classmethod
    def _revoke_jobs(cls, value: str) -> str:
        if value not in {"cancel_at_safe_checkpoint", "leave_pinned_until_completion"}:
            raise ValueError("unsupported revoke-running-jobs policy")
        return value


class CapabilityPackManifest(BaseModel):
    """Strict v2 manifest accepted by the governed pack lifecycle."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(...)
    id: str
    version: str
    kind: str = "capability-pack"
    publisher: PackPublisher
    signature: PackSignature = Field(default_factory=PackSignature)
    compatibility: PackCompatibility
    dependencies: list[PackDependency] = Field(default_factory=list)
    contributes: PackContributions
    authority: PackAuthority
    resources: PackResources
    data_policy: PackDataPolicy
    policy_overlays: list[str] = Field(default_factory=list)
    lifecycle: PackLifecycle = Field(default_factory=PackLifecycle)
    display_name: str | None = None
    summary: str | None = None
    description: str | None = None

    @field_validator("schema_version")
    @classmethod
    def _version_number(cls, value: int) -> int:
        if isinstance(value, bool) or value != CAPABILITY_PACK_SCHEMA_VERSION:
            raise ValueError("capability-pack v2 requires schema_version: 2; run the explicit v1 migration")
        return value

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        return _validate_pack_id(value)

    @field_validator("version")
    @classmethod
    def _pack_version(cls, value: str) -> str:
        value = value.strip()
        try:
            Version(value)
        except InvalidVersion as exc:
            raise ValueError(f"invalid pack version: {value}") from exc
        return value

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        if value != "capability-pack":
            raise ValueError("v2 manifests must have kind: capability-pack")
        return value

    @field_validator("dependencies")
    @classmethod
    def _unique_dependencies(cls, value: list[PackDependency]) -> list[PackDependency]:
        ids = [item.id for item in value]
        if len(ids) != len(set(ids)):
            raise ValueError("dependencies must not contain duplicate pack ids")
        return value

    @model_validator(mode="after")
    def _dependency_graph(self) -> "CapabilityPackManifest":
        if any(dependency.id == self.id for dependency in self.dependencies):
            raise ValueError("a capability pack cannot depend on itself")
        return self

    @model_validator(mode="after")
    def _requires_contribution(self) -> "CapabilityPackManifest":
        if not any(
            getattr(self.contributes, field_name)
            for field_name in (
                "capabilities",
                "skills",
                "workflows",
                "prompts",
                "sources",
                "reports",
                "evals",
                "runbooks",
            )
        ):
            raise ValueError("capability packs must declare at least one contribution")
        return self

    @field_validator("policy_overlays")
    @classmethod
    def _policy_overlays(cls, value: list[str]) -> list[str]:
        return [_validate_reference(item, field_name="policy_overlays") for item in _normalize_strings(value, field_name="policy_overlays")]

    @model_validator(mode="before")
    @classmethod
    def _reject_mixed_v1_fields(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        if value.get("schema_version") == CAPABILITY_PACK_SCHEMA_V1:
            raise ValueError("schema_version 1 requires explicit v1 migration via migrate_capability_pack_v1")
        resources = value.get("resources")
        if isinstance(resources, Mapping) and "gpu_class" in resources:
            raise ValueError("v2 replaces resources.gpu_class; run the explicit v1 migration")
        return value

    @model_validator(mode="after")
    def _safe_remote_policy(self) -> "CapabilityPackManifest":
        if self.authority.network and not self.data_policy.egress:
            raise ValueError("network authority requires an explicit data_policy.egress allow-list")
        if not self.authority.network and self.data_policy.egress:
            raise ValueError("data_policy.egress requires authority.network: true")
        if self.resources.max_inference_cost_microusd == 0 and self.authority.network:
            raise ValueError("network-enabled packs require a finite positive inference cost ceiling")
        if self.display_name is None:
            self.display_name = self.id
        return self

    @property
    def inference_priority(self) -> str:
        return self.resources.inference_priority.value

    @property
    def authority_digest(self) -> str:
        return canonical_digest(self.authority.model_dump(mode="json"), self.data_policy.model_dump(mode="json"))


def canonical_digest(*values: Any) -> str:
    encoded = json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def parse_capability_pack_manifest(content: str | Mapping[str, Any], *, source: str = "<memory>") -> CapabilityPackManifest:
    """Parse a v2 manifest and fail explicitly for v1/mixed input."""
    if isinstance(content, Mapping):
        payload = dict(content)
    else:
        try:
            payload = yaml.safe_load(content)
        except yaml.YAMLError as exc:
            raise CapabilityPackManifestError(source, f"invalid YAML: {exc}") from exc
    if not isinstance(payload, dict):
        raise CapabilityPackManifestError(source, "manifest root must be a mapping")
    try:
        return CapabilityPackManifest.model_validate(payload)
    except ValidationError as exc:
        errors = [
            {"loc": [str(part) for part in error.get("loc", ())], "message": error.get("msg", "validation error"), "type": error.get("type", "value_error")}
            for error in exc.errors()
        ]
        raise CapabilityPackManifestError(source, errors[0]["message"] if errors else "validation error", errors=errors) from exc


@dataclass(frozen=True)
class PackMigration:
    """Dry-run v1 migration result; activation always needs a new review."""

    payload: dict[str, Any]
    changes: tuple[str, ...]
    requires_review: bool = True
    remote_spending_default_microusd: int = 0
    cloud_egress_default: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version_from": CAPABILITY_PACK_SCHEMA_V1,
            "schema_version_to": CAPABILITY_PACK_SCHEMA_VERSION,
            "payload": deepcopy(self.payload),
            "changes": list(self.changes),
            "requires_review": self.requires_review,
            "remote_spending_default_microusd": self.remote_spending_default_microusd,
            "cloud_egress_default": list(self.cloud_egress_default),
        }


def _legacy_priority(value: Any) -> InferencePriority:
    normalized = str(value or "").strip().lower().replace("-", "_")
    try:
        return _SAFE_PRIORITY_ALIASES[normalized]
    except KeyError as exc:
        raise CapabilityPackManifestError("<migration>", f"unknown legacy gpu_class: {value!r}") from exc


def migrate_capability_pack_v1(payload: Mapping[str, Any], *, dry_run: bool = True) -> PackMigration:
    """Translate only known v1 fields into a review-required v2 payload.

    The conversion is intentionally conservative: a local GPU allowance never
    becomes cloud authority, and any v2 field mixed into a v1 payload is an
    error instead of a silent reinterpretation.
    """
    if not isinstance(payload, Mapping):
        raise CapabilityPackManifestError("<migration>", "v1 manifest must be a mapping")
    source = deepcopy(dict(payload))
    schema_version = source.get("schema_version", CAPABILITY_PACK_SCHEMA_V1)
    if schema_version != CAPABILITY_PACK_SCHEMA_V1:
        raise CapabilityPackManifestError("<migration>", "only schema_version 1 can be migrated")
    resources = source.get("resources")
    if not isinstance(resources, Mapping):
        resources = {}
    if "inference_priority" in resources or "max_inference_cost_microusd" in resources:
        raise CapabilityPackManifestError("<migration>", "v1 payload mixes v2 resource fields")
    if "gpu_class" not in resources:
        raise CapabilityPackManifestError("<migration>", "v1 payload requires resources.gpu_class")

    priority = _legacy_priority(resources["gpu_class"])
    migrated_resources = dict(resources)
    migrated_resources.pop("gpu_class", None)
    migrated_resources["inference_priority"] = priority.value
    migrated_resources["max_inference_cost_microusd"] = 0
    migrated_resources.setdefault("max_runtime_seconds", 300)
    migrated_resources.setdefault("max_artifact_bytes", 10 * 1024 * 1024)

    authority = source.get("authority")
    authority = dict(authority) if isinstance(authority, Mapping) else {}
    authority["network"] = False
    authority.pop("egress", None)
    data_policy = source.get("data_policy")
    data_policy = dict(data_policy) if isinstance(data_policy, Mapping) else {}
    data_policy["classes"] = list(data_policy.get("classes") or ["public"])
    data_policy["egress"] = []

    migrated = dict(source)
    migrated["schema_version"] = CAPABILITY_PACK_SCHEMA_VERSION
    migrated["kind"] = "capability-pack"
    migrated["resources"] = migrated_resources
    migrated["authority"] = authority
    migrated["data_policy"] = data_policy
    migrated.setdefault("publisher", {"name": "Unknown", "provenance": "migrated-v1"})
    migrated.setdefault("signature", {"state": "unsigned-local", "signer": None})
    migrated.setdefault("dependencies", [])
    migrated.setdefault("contributes", {})
    migrated.setdefault("policy_overlays", [])
    migrated.setdefault("lifecycle", deepcopy(_MIGRATED_DEFAULT_LIFECYCLE))
    changes = (
        "schema_version: 1 -> 2",
        f"resources.gpu_class -> resources.inference_priority:{priority.value}",
        "resources.max_inference_cost_microusd: 0 (remote spending remains disabled)",
        "authority.network: false (legacy local GPU allowance is not cloud authority)",
        "data_policy.egress: [] (cloud egress requires fresh review)",
        "activation requires a fresh review bound to migrated digest/version/goal",
    )
    # ``dry_run`` is retained as an explicit API signal.  The conversion never
    # writes to disk, regardless of its value; callers decide when to persist.
    _ = dry_run
    return PackMigration(payload=migrated, changes=changes)


def migrate_v1_to_v2(payload: Mapping[str, Any], *, dry_run: bool = True) -> PackMigration:
    """Compatibility alias for callers using the shorter migration name."""
    return migrate_capability_pack_v1(payload, dry_run=dry_run)


def _safe_archive_name(raw_name: str) -> str:
    name = str(raw_name or "").replace("\\", "/")
    if not name or name.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", name):
        raise CapabilityPackError("archive member path must be relative")
    path = PurePosixPath(name)
    if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise CapabilityPackError("archive member path traverses outside the package")
    normalized = path.as_posix()
    if normalized in {".", ""}:
        raise CapabilityPackError("archive member path must name a file or directory")
    return normalized


@dataclass(frozen=True)
class ArchiveValidationResult:
    ok: bool
    archive: str
    members: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    total_bytes: int = 0
    regular_files: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "archive": self.archive,
            "members": list(self.members),
            "regular_files": list(self.regular_files),
            "errors": list(self.errors),
            "total_bytes": self.total_bytes,
        }


def validate_capability_pack_archive(
    archive_path: str | Path,
    *,
    max_member_bytes: int = MAX_PACK_MEMBER_BYTES,
    max_total_bytes: int = MAX_PACK_TOTAL_BYTES,
    max_members: int = MAX_PACK_MEMBERS,
) -> ArchiveValidationResult:
    """Inspect zip/tar metadata without extracting untrusted entries."""
    path = Path(archive_path)
    members: list[str] = []
    regular_files: list[str] = []
    errors: list[str] = []
    total_bytes = 0
    seen: set[str] = set()
    if any(
        isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0
        for limit in (max_member_bytes, max_total_bytes, max_members)
    ):
        return ArchiveValidationResult(
            ok=False,
            archive=str(path),
            errors=("archive limits must be positive finite integers",),
        )
    try:
        if path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as archive:
                infos = archive.infolist()
                if len(infos) > max_members:
                    errors.append("archive contains too many members")
                for info in infos[: max_members + 1]:
                    try:
                        name = _safe_archive_name(info.filename)
                    except CapabilityPackError as exc:
                        errors.append(str(exc))
                        continue
                    if name in seen:
                        errors.append(f"duplicate archive member: {name}")
                    seen.add(name)
                    members.append(name)
                    mode = (info.external_attr >> 16) & 0xFFFF
                    is_directory = info.is_dir() or stat.S_ISDIR(mode)
                    is_special = stat.S_ISLNK(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode) or stat.S_ISFIFO(mode)
                    if is_special:
                        errors.append(f"archive member is a link or special file: {name}")
                    elif not is_directory and stat.S_IFMT(mode) not in {0, stat.S_IFREG}:
                        errors.append(f"archive member is not a regular file: {name}")
                    elif not is_directory:
                        regular_files.append(name)
                        if stat.S_IMODE(mode) & 0o111:
                            errors.append(f"archive member is executable: {name}")
                    unsafe_reason = _unsafe_package_member(name)
                    if unsafe_reason:
                        errors.append(f"{unsafe_reason}: {name}")
                    if name in {"manifest.yaml", "manifest.yml"} and not is_directory and name not in regular_files:
                        errors.append("root manifest must be a regular file")
                    if is_directory and name in {"manifest.yaml", "manifest.yml"}:
                        errors.append("root manifest must be a regular file")
                    if info.file_size > max_member_bytes:
                        errors.append(f"archive member exceeds size limit: {name}")
                    total_bytes += max(0, info.file_size)
        elif tarfile.is_tarfile(path):
            with tarfile.open(path, "r:*") as archive:
                infos = archive.getmembers()
                if len(infos) > max_members:
                    errors.append("archive contains too many members")
                for info in infos[: max_members + 1]:
                    try:
                        name = _safe_archive_name(info.name)
                    except CapabilityPackError as exc:
                        errors.append(str(exc))
                        continue
                    if name in seen:
                        errors.append(f"duplicate archive member: {name}")
                    seen.add(name)
                    members.append(name)
                    is_directory = info.isdir()
                    is_regular = info.isfile()
                    is_special = info.issym() or info.islnk() or info.isdev() or info.isfifo()
                    if is_special:
                        errors.append(f"archive member is a link or special file: {name}")
                    elif not is_directory and not is_regular:
                        errors.append(f"archive member is not a regular file: {name}")
                    elif is_regular:
                        regular_files.append(name)
                        if int(info.mode) & 0o111:
                            errors.append(f"archive member is executable: {name}")
                    unsafe_reason = _unsafe_package_member(name)
                    if unsafe_reason:
                        errors.append(f"{unsafe_reason}: {name}")
                    if name in {"manifest.yaml", "manifest.yml"} and not is_regular:
                        errors.append("root manifest must be a regular file")
                    size = max(0, int(info.size))
                    if size > max_member_bytes:
                        errors.append(f"archive member exceeds size limit: {name}")
                    total_bytes += size
        else:
            errors.append("unsupported or unreadable archive format")
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as exc:
        errors.append(f"archive could not be inspected safely: {type(exc).__name__}")
    if total_bytes > max_total_bytes:
        errors.append("archive exceeds total uncompressed size limit")
    if "manifest.yaml" not in seen and "manifest.yml" not in seen:
        errors.append("archive must contain a root manifest.yaml or manifest.yml")
    if "manifest.yaml" in seen and "manifest.yml" in seen:
        errors.append("archive must contain only one root manifest")
    return ArchiveValidationResult(
        ok=not errors,
        archive=str(path),
        members=tuple(sorted(set(members))),
        regular_files=tuple(sorted(set(regular_files))),
        errors=tuple(dict.fromkeys(errors)),
        total_bytes=total_bytes,
    )


_CONTRIBUTION_PATH_FIELDS = ("skills", "workflows", "prompts", "sources", "reports", "evals", "runbooks")
_CONTRIBUTION_PREFIXES = {
    "skills": "skills/",
    "workflows": "workflows/",
    "prompts": "prompts/",
    "sources": "sources/",
    "reports": "reports/",
    "evals": "evals/",
    "runbooks": "runbooks/",
}


def _declared_package_files(manifest: CapabilityPackManifest) -> set[str]:
    return {
        reference
        for field_name in _CONTRIBUTION_PATH_FIELDS
        for reference in getattr(manifest.contributes, field_name)
    }


def _unsafe_package_member(name: str) -> str | None:
    parts = PurePosixPath(name).parts
    if any(part.lower() in {"hook", "hooks"} for part in parts):
        return "package hooks are not executable contribution files"
    return None


def _scan_capability_pack_directory(root: Path) -> tuple[list[str], list[str], int, list[str]]:
    """Run the bounded directory scan shared by validation and digesting."""

    members: list[str] = []
    regular_files: list[str] = []
    errors: list[str] = []
    total_bytes = 0
    try:
        reject_symlink_entries(root)
        for entry in sorted(root.rglob("*")):
            relative = entry.relative_to(root).as_posix()
            try:
                entry_stat = entry.lstat()
            except OSError as exc:
                errors.append(f"package member could not be inspected: {relative}")
                continue
            if stat.S_ISDIR(entry_stat.st_mode):
                continue
            members.append(relative)
            if not stat.S_ISREG(entry_stat.st_mode):
                errors.append(f"package member is not a regular file: {relative}")
                continue
            regular_files.append(relative)
            if stat.S_IMODE(entry_stat.st_mode) & 0o111:
                errors.append(f"package member is executable: {relative}")
            unsafe_reason = _unsafe_package_member(relative)
            if unsafe_reason:
                errors.append(f"{unsafe_reason}: {relative}")
            size = max(0, int(entry_stat.st_size))
            if size > MAX_PACK_MEMBER_BYTES:
                errors.append(f"package member exceeds size limit: {relative}")
            total_bytes += size
    except ValueError as exc:
        errors.append(str(exc))
    if len(members) > MAX_PACK_MEMBERS:
        errors.append("package contains too many members")
    if total_bytes > MAX_PACK_TOTAL_BYTES:
        errors.append("package exceeds total uncompressed size limit")
    if "manifest.yaml" not in members and "manifest.yml" not in members:
        errors.append("package must contain a root manifest.yaml or manifest.yml")
    if "manifest.yaml" in members and "manifest.yml" in members:
        errors.append("package must contain only one root manifest")
    return members, list(dict.fromkeys(errors)), total_bytes, regular_files


def _validate_declared_archive_files(
    manifest: CapabilityPackManifest,
    members: Iterable[str],
    regular_files: Iterable[str],
) -> list[str]:
    member_set = set(members)
    regular_file_set = set(regular_files)
    errors: list[str] = []
    for reference in sorted(_declared_package_files(manifest)):
        if reference not in member_set:
            errors.append(f"declared contribution file is missing from package: {reference}")
        elif reference not in regular_file_set:
            errors.append(f"declared contribution file is not a regular file: {reference}")
    return errors


def validate_capability_pack_path(
    package_root: str | Path,
    manifest: CapabilityPackManifest | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate a checked-out package and every declared contribution path."""
    root = Path(package_root)
    errors: list[str] = []
    members, scan_errors, _total_bytes, regular_files = _scan_capability_pack_directory(root)
    errors.extend(scan_errors)
    if not root.exists() or not root.is_dir():
        return {"ok": False, "path": str(root), "errors": ["package root must be a directory"]}
    manifest_path = root / "manifest.yaml"
    if not manifest_path.is_file():
        manifest_path = root / "manifest.yml"
    if not manifest_path.is_file():
        errors.append("package must contain a root manifest.yaml or manifest.yml")
    if (root / "manifest.yaml").is_file() and (root / "manifest.yml").is_file():
        errors.append("package must contain only one root manifest")
    parsed: CapabilityPackManifest | None = None
    on_disk_manifest: CapabilityPackManifest | None = None
    if manifest_path.is_file():
        try:
            on_disk_manifest = parse_capability_pack_manifest(
                manifest_path.read_text(encoding="utf-8"),
                source=str(manifest_path),
            )
        except (OSError, UnicodeDecodeError, CapabilityPackManifestError) as exc:
            errors.append(str(exc))
    if manifest is not None:
        try:
            parsed = manifest if isinstance(manifest, CapabilityPackManifest) else parse_capability_pack_manifest(manifest, source=str(manifest_path))
        except CapabilityPackManifestError as exc:
            errors.append(exc.message)
    else:
        parsed = on_disk_manifest
    if parsed is not None and on_disk_manifest is not None:
        supplied_payload = parsed.model_dump(mode="json")
        disk_payload = on_disk_manifest.model_dump(mode="json")
        supplied_payload.pop("signature", None)
        disk_payload.pop("signature", None)
        if canonical_digest(supplied_payload) != canonical_digest(disk_payload):
            errors.append("supplied manifest does not match the package manifest")
    references: list[str] = []
    if parsed is not None:
        errors.extend(_validate_declared_archive_files(parsed, members, regular_files))
        for field_name in _CONTRIBUTION_PATH_FIELDS:
            field_references = list(getattr(parsed.contributes, field_name))
            references.extend(field_references)
            expected_prefix = _CONTRIBUTION_PREFIXES.get(field_name, "")
            for reference in field_references:
                # Contribution models require path-shaped references.  Keep
                # this check at the package boundary as well for callers that
                # constructed a model before the stricter validator existed.
                if "/" not in reference:
                    errors.append(f"{field_name} contribution must be a package-relative path: {reference}")
                    continue
                try:
                    path = PurePosixPath(reference)
                    if path.is_absolute() or ".." in path.parts or "\\" in reference:
                        raise CapabilityPackError("contribution path must be relative and traversal-free")
                    if expected_prefix and not reference.startswith(expected_prefix):
                        raise CapabilityPackError(f"contribution path must live under {expected_prefix}")
                    resolved = resolve_package_reference(root, reference)
                    if not resolved.is_file() or resolved.is_symlink():
                        raise CapabilityPackError(f"contribution path is not a regular package file: {reference}")
                except (CapabilityPackError, ValueError) as exc:
                    errors.append(str(exc))
    return {
        "ok": not errors,
        "path": str(root),
        "manifest": parsed.model_dump(mode="json") if parsed is not None else None,
        "errors": list(dict.fromkeys(errors)),
        "references": sorted(set(references)),
    }


def validate_capability_pack_package(
    package: str | Path,
    *,
    manifest: CapabilityPackManifest | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate either a package directory or an archive path."""
    path = Path(package)
    if path.is_dir():
        return validate_capability_pack_path(path, manifest)
    archive = validate_capability_pack_archive(path)
    errors = list(archive.errors)
    parsed: CapabilityPackManifest | None = None
    if archive.ok:
        manifest_name = next(
            (name for name in ("manifest.yaml", "manifest.yml") if name in archive.members),
            None,
        )
        try:
            if path.suffix.lower() == ".zip":
                with zipfile.ZipFile(path) as handle:
                    content = handle.read(manifest_name or "manifest.yaml").decode("utf-8")
            else:
                with tarfile.open(path, "r:*") as handle:
                    member = handle.getmember(manifest_name or "manifest.yaml")
                    stream = handle.extractfile(member)
                    if stream is None:
                        raise CapabilityPackError("archive manifest is not a regular file")
                    content = stream.read(MAX_PACK_MEMBER_BYTES + 1).decode("utf-8")
            if len(content.encode("utf-8")) > MAX_PACK_MEMBER_BYTES:
                raise CapabilityPackError("archive manifest exceeds size limit")
            parsed = parse_capability_pack_manifest(content, source=f"{path}:{manifest_name}")
            errors.extend(_validate_declared_archive_files(parsed, archive.members, archive.regular_files))
            if manifest is not None:
                supplied = manifest if isinstance(manifest, CapabilityPackManifest) else parse_capability_pack_manifest(manifest)
                supplied_payload = supplied.model_dump(mode="json")
                archive_payload = parsed.model_dump(mode="json")
                supplied_payload.pop("signature", None)
                archive_payload.pop("signature", None)
                if canonical_digest(supplied_payload) != canonical_digest(archive_payload):
                    errors.append("supplied manifest does not match the archive manifest")
        except (CapabilityPackError, CapabilityPackManifestError, OSError, UnicodeDecodeError, tarfile.TarError, zipfile.BadZipFile) as exc:
            errors.append(str(exc))
    result = {"ok": not errors, "archive": archive.as_dict(), "errors": list(dict.fromkeys(errors))}
    if parsed is not None:
        result["manifest"] = parsed.model_dump(mode="json")
    return result


_LOCAL_WORKFLOW_TOOLS = {
    "get_goals",
    "read_file",
    "write_file",
    "local_source",
    # The existing web-brief workflow is parsed for its step contract, but
    # this local executor routes its source step through the injected
    # intercepted transport and never invokes the network tool.
    "web_search",
    "render_markdown",
}


def load_capability_pack_workflows(
    package_root: str | Path,
    manifest: CapabilityPackManifest | Mapping[str, Any] | None = None,
) -> tuple[CapabilityPackLoadedWorkflow, ...]:
    """Parse pack workflows with the existing loader and no code hooks.

    Workflow markdown is declarative, but a pack still cannot introduce an
    arbitrary executable step.  Parsing is kept behind the existing workflow
    loader and this narrow allow-list is the local proof boundary.
    """

    root = Path(package_root)
    pack = manifest if isinstance(manifest, CapabilityPackManifest) else (
        parse_capability_pack_manifest(manifest, source=str(root / "manifest.yaml"))
        if manifest is not None
        else None
    )
    if pack is None:
        manifest_path = root / "manifest.yaml"
        if not manifest_path.is_file():
            manifest_path = root / "manifest.yml"
        pack = parse_capability_pack_manifest(
            manifest_path.read_text(encoding="utf-8"),
            source=str(manifest_path),
        )
    try:
        from src.workflows.loader import parse_workflow_content
    except Exception as exc:  # pragma: no cover - import failure is surfaced to review
        raise CapabilityPackLifecycleError("existing workflow loader is unavailable") from exc

    loaded: list[CapabilityPackLoadedWorkflow] = []
    for reference in pack.contributes.workflows:
        path = resolve_package_reference(root, reference)
        if not path.is_file() or path.is_symlink():
            raise CapabilityPackLifecycleError(f"workflow contribution is not a regular file: {reference}")
        errors: list[dict[str, Any]] = []
        try:
            workflow = parse_workflow_content(
                path.read_text(encoding="utf-8"),
                path=str(path),
                errors=errors,
            )
        except (OSError, UnicodeDecodeError) as exc:
            raise CapabilityPackLifecycleError(f"workflow contribution could not be read: {reference}") from exc
        if workflow is None:
            message = errors[0].get("message") if errors else "workflow could not be parsed"
            raise CapabilityPackLifecycleError(f"invalid workflow contribution {reference}: {message}")
        step_tools = tuple(str(step.tool).strip() for step in workflow.steps)
        unsupported = sorted({tool for tool in step_tools if tool not in _LOCAL_WORKFLOW_TOOLS})
        if unsupported:
            raise CapabilityPackLifecycleError(
                f"workflow contribution {reference} requests unsupported local tools: {', '.join(unsupported)}"
            )
        loaded.append(
            CapabilityPackLoadedWorkflow(
                name=str(workflow.name),
                file_path=str(path),
                step_tools=step_tools,
                steps=tuple(
                    CapabilityPackWorkflowStep(
                        step_id=str(step.id),
                        tool=str(step.tool),
                        arguments=deepcopy(dict(step.arguments)),
                    )
                    for step in workflow.steps
                ),
                result_template=str(workflow.result_template or ""),
            )
        )
    return tuple(loaded)


def validate_capability_pack_dependencies(
    manifest: CapabilityPackManifest | Mapping[str, Any],
    available: Mapping[str, Any] | Iterable[Mapping[str, Any]] | None,
) -> tuple[str, ...]:
    """Return dependency errors for the reviewed digest/version graph.

    ``available`` accepts the lifecycle's ``pack_id -> digest`` mapping or a
    sequence of version records.  An empty dependency list is always valid.
    """

    pack = manifest if isinstance(manifest, CapabilityPackManifest) else parse_capability_pack_manifest(manifest)
    if available is None:
        available_map: dict[str, Any] = {}
    elif isinstance(available, Mapping):
        available_map = dict(available)
    else:
        available_map = {}
        for record in available:
            if not isinstance(record, Mapping):
                continue
            item_id = record.get("pack_id", record.get("id"))
            if isinstance(item_id, str):
                available_map[item_id] = record
    errors: list[str] = []
    for dependency in pack.dependencies:
        found = available_map.get(dependency.id)
        if found is None:
            errors.append(f"dependency is unavailable: {dependency.id}")
            continue
        if isinstance(found, Mapping):
            if bool(found.get("revoked")):
                errors.append(f"dependency is revoked: {dependency.id}")
                continue
            found_digest = found.get("digest")
            found_version = found.get("version")
        else:
            found_digest = found
            found_version = None
        if dependency.digest != found_digest:
            errors.append(f"dependency digest mismatch: {dependency.id}")
        if found_version is not None:
            found_version_text = str(found_version).strip()
            if dependency.version != "*" and found_version_text != dependency.version:
                try:
                    version_satisfies = Version(found_version_text) in SpecifierSet(dependency.version)
                except InvalidVersion:
                    # Older state records sometimes stored the constraint text
                    # itself.  Preserve that representation only when it is an
                    # exact match; every other non-version value fails closed.
                    version_satisfies = False
                if not version_satisfies:
                    errors.append(f"dependency version mismatch: {dependency.id}")
            elif dependency.version == "*" and found_version_text != "*":
                try:
                    Version(found_version_text)
                except InvalidVersion:
                    errors.append(f"dependency version mismatch: {dependency.id}")
    return tuple(errors)


def _dependency_records_from_state(
    state: Mapping[str, Any],
    manifest: CapabilityPackManifest,
) -> dict[str, dict[str, Any]]:
    """Project only durable dependency records for one reviewed manifest.

    External availability maps are useful for an advisory preflight, but they
    cannot establish authority.  Lifecycle decisions must always re-read the
    reviewed version and revocation ledger from the same durable state file.
    """

    versions = state.get("versions")
    revoked = state.get("revoked")
    records: dict[str, dict[str, Any]] = {}
    for dependency in manifest.dependencies:
        dependency_versions = versions.get(dependency.id, {}) if isinstance(versions, Mapping) else {}
        candidate = (
            dependency_versions.get(dependency.digest)
            if isinstance(dependency_versions, Mapping)
            else None
        )
        if not isinstance(candidate, Mapping):
            continue
        record = dict(candidate)
        revoked_digests = revoked.get(dependency.id, []) if isinstance(revoked, Mapping) else []
        record["revoked"] = bool(record.get("revoked")) or dependency.digest in revoked_digests
        records[dependency.id] = record
    return records


def capability_pack_digest(package_root: str | Path) -> str:
    """Hash package content while excluding mutable top-level signature text."""
    root = Path(package_root)
    if not root.is_dir():
        raise CapabilityPackError("package root must be a directory")
    _members, scan_errors, _total_bytes, _regular_files = _scan_capability_pack_directory(root)
    if scan_errors:
        raise CapabilityPackError("; ".join(scan_errors))
    hasher = hashlib.sha256()
    manifest_names = {"manifest.yaml", "manifest.yml"}
    for file_path in sorted(path for path in root.rglob("*") if path.is_file()):
        relative = file_path.relative_to(root).as_posix()
        hasher.update(relative.encode("utf-8"))
        hasher.update(b"\0")
        if relative in manifest_names:
            try:
                payload = yaml.safe_load(file_path.read_text(encoding="utf-8"))
            except Exception:
                payload = None
            if isinstance(payload, dict):
                payload = dict(payload)
                payload.pop("signature", None)
                content = yaml.safe_dump(payload, sort_keys=True).encode("utf-8")
            else:
                content = file_path.read_bytes()
            hasher.update(content)
        else:
            hasher.update(file_path.read_bytes())
    return hasher.hexdigest()


def publisher_trust_status(
    manifest: CapabilityPackManifest,
    *,
    package_root: str | Path | None = None,
) -> dict[str, Any]:
    """Return explicit provenance status; local integrity is never publisher trust.

    An ``integrity-checked`` declaration is meaningful only after the declared
    signature digest has been compared with the package content.  Callers that
    do not have the package root receive an unverified status instead of a
    trust-like assertion based on metadata alone.
    """
    signature = manifest.signature
    if not isinstance(signature, PackSignature):
        signature = PackSignature.model_validate(signature)
    integrity_checked = False
    integrity_digest_match: bool | None = None
    computed_digest: str | None = None
    reason = "signature and publisher label are provenance/integrity only; trusted publisher keys are unavailable"
    if signature.state == "integrity-checked":
        if package_root is None:
            reason = "signature digest cannot be checked without the package root"
        else:
            try:
                computed_digest = capability_pack_digest(package_root)
            except (CapabilityPackError, OSError, ValueError):
                reason = "signature digest could not be checked against package content"
            else:
                integrity_digest_match = signature.digest == computed_digest
                integrity_checked = integrity_digest_match
                if not integrity_digest_match:
                    reason = "declared signature digest does not match package content"
    return {
        "publisher_verified": False,
        "trust": "local_review_required",
        "provenance": manifest.publisher.provenance,
        "signature_state": signature.state,
        "integrity_checked": integrity_checked,
        "integrity_digest_match": integrity_digest_match,
        "declared_digest": signature.digest,
        "computed_digest": computed_digest,
        "reason": reason,
    }


def authority_delta(
    previous: CapabilityPackManifest | Mapping[str, Any] | None,
    candidate: CapabilityPackManifest | Mapping[str, Any],
) -> dict[str, Any]:
    """Compute additive authority and egress changes using stable categories."""
    old = previous if isinstance(previous, CapabilityPackManifest) else parse_capability_pack_manifest(previous) if previous is not None else None
    new = candidate if isinstance(candidate, CapabilityPackManifest) else parse_capability_pack_manifest(candidate)
    old_authority = old.authority.model_dump(mode="json") if old else {"tools": [], "filesystem": [], "network": False, "secrets": [], "approval": "never"}
    new_authority = new.authority.model_dump(mode="json")
    old_policy = old.data_policy.model_dump(mode="json") if old else {"egress": []}
    new_policy = new.data_policy.model_dump(mode="json")
    delta = _authority_delta_from_payloads(old_authority, old_policy, new_authority, new_policy)
    return {
        **delta,
        "authority_digest_before": old.authority_digest if old else None,
        "authority_digest_after": new.authority_digest,
    }


def _authority_delta_from_payloads(
    old_authority: Mapping[str, Any] | None,
    old_policy: Mapping[str, Any] | None,
    new_authority: Mapping[str, Any],
    new_policy: Mapping[str, Any],
) -> dict[str, Any]:
    old_authority = old_authority if isinstance(old_authority, Mapping) else {}
    old_policy = old_policy if isinstance(old_policy, Mapping) else {}
    added: dict[str, Any] = {}
    for key in ("tools", "filesystem", "secrets"):
        values = sorted(set(new_authority.get(key, [])) - set(old_authority.get(key, [])))
        if values:
            added[key] = values
    if new_authority.get("network") and not old_authority.get("network"):
        added["network"] = True
    egress = sorted(set(new_policy.get("egress", [])) - set(old_policy.get("egress", [])))
    if egress:
        added["egress"] = egress
    removed: dict[str, Any] = {
        key: sorted(set(old_authority.get(key, [])) - set(new_authority.get(key, [])))
        for key in ("tools", "filesystem", "secrets")
        if set(old_authority.get(key, [])) - set(new_authority.get(key, []))
    }
    if old_authority.get("network") and not new_authority.get("network"):
        removed["network"] = True
    removed_egress = sorted(set(old_policy.get("egress", [])) - set(new_policy.get("egress", [])))
    if removed_egress:
        removed["egress"] = removed_egress
    return {
        "added": added,
        "removed": removed,
        "requires_approval": bool(added),
    }


def _review_digest(*, pack_id: str, version: str, digest: str, goal_id: str, authority_digest: str) -> str:
    return canonical_digest("pack-review", pack_id, version, digest, goal_id, authority_digest)


def _dependency_bindings(manifest: CapabilityPackManifest) -> list[dict[str, str]]:
    return [
        {"id": dependency.id, "version": dependency.version, "digest": dependency.digest}
        for dependency in manifest.dependencies
    ]


def _dependencies_digest(manifest: CapabilityPackManifest) -> str:
    return canonical_digest(_dependency_bindings(manifest))


def _approval_digest(
    *,
    action: str,
    pack_id: str,
    version: str,
    digest: str,
    goal_id: str,
    current_digest: str | None,
    authority_delta_payload: Mapping[str, Any],
    owner_principal_id: str | None = None,
    session_id: str | None = None,
    content_digest: str | None = None,
    authority_digest: str | None = None,
) -> str:
    return canonical_digest(
        "capability-pack-operator-approval-v1",
        action,
        pack_id,
        version,
        digest,
        goal_id,
        current_digest,
        authority_delta_payload,
        owner_principal_id,
        session_id,
        content_digest,
        authority_digest,
    )


@dataclass(frozen=True)
class CapabilityPackExecutionContract:
    """The bounded execution fields a future #743/#747 adapter must consume."""

    job_id: str
    idempotency_key: str
    goal_id: str
    deadline_at: str
    priority: int
    max_inference_cost_microusd: int
    max_artifact_bytes: int
    cancel_on_revoke: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CAPABILITY_PACK_EXECUTION_SCHEMA,
            "job_id": self.job_id,
            "idempotency_key": self.idempotency_key,
            "goal_id": self.goal_id,
            "deadline_at": self.deadline_at,
            "priority": self.priority,
            "max_inference_cost_microusd": self.max_inference_cost_microusd,
            "max_artifact_bytes": self.max_artifact_bytes,
            "cancel_on_revoke": self.cancel_on_revoke,
        }


@dataclass(frozen=True)
class CapabilityPackLocalExecutionRequest:
    """Inputs for one bounded, provider-free capability-pack execution.

    ``transport`` is deliberately not part of this serializable request.  A
    caller must inject a test/local transport for the research domain; this
    keeps the lifecycle module from ever reaching the network itself.
    """

    pack_id: str
    goal_id: str
    job_id: str
    owner_principal_id: str
    session_id: str
    domain: str
    artifact_path: str
    source_url: str | None = None
    query: str | None = None
    goal_snapshot: Mapping[str, Any] | str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CAPABILITY_PACK_LOCAL_EXECUTION_SCHEMA,
            "pack_id": self.pack_id,
            "goal_id": self.goal_id,
            "job_id": self.job_id,
            "owner_principal_id": self.owner_principal_id,
            "session_id": self.session_id,
            "domain": self.domain,
            "artifact_path": self.artifact_path,
            "source_url": self.source_url,
            "query": self.query,
            "goal_snapshot": self.goal_snapshot,
        }


@dataclass(frozen=True)
class CapabilityPackLoadedWorkflow:
    """A parsed declarative workflow contribution from a reviewed pack."""

    name: str
    file_path: str
    step_tools: tuple[str, ...]
    # Keep the parsed step contract private to the local host.  It is never
    # exposed in operator readback because arguments may contain untrusted
    # caller data, but retaining the steps lets execution prove that the
    # reviewed workflow was actually traversed.
    steps: tuple["CapabilityPackWorkflowStep", ...] = ()
    result_template: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "file_path": self.file_path,
            "step_tools": list(self.step_tools),
        }


@dataclass(frozen=True)
class CapabilityPackWorkflowStep:
    """The immutable subset of one reviewed declarative workflow step."""

    step_id: str
    tool: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class CapabilityPackGovernedWorkflowResult:
    """Provider-free result produced by the internal capability-pack host."""

    content: str
    steps: tuple[dict[str, Any], ...]
    executor: str = "capability_pack_internal_governed_host_v1"
    provider_calls: int = 0
    live_network_calls: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "executor": self.executor,
            "status": "succeeded",
            "provider_calls": self.provider_calls,
            "live_network_calls": self.live_network_calls,
            "steps": [deepcopy(step) for step in self.steps],
            "content_digest": canonical_digest(self.content),
        }


class _CapabilityPackLocalWorkflowHost:
    """Execute the reviewed pack workflow through a fixed local tool host.

    The general workflow manager accepts the application's live tool graph,
    including provider and connector tools.  A capability-pack local proof
    has a stricter boundary: its source is an already intercepted value and
    its only effect is the lifecycle-owned artifact commit.  This adapter
    therefore consumes the same parsed declarative step contract while
    allowing only the provider-free tools already admitted by
    ``load_capability_pack_workflows``.  It has no callback or dynamic import
    seam and never performs network or model work.
    """

    _SOURCE_TOOLS = {"get_goals", "local_source", "web_search"}
    _OUTPUT_TOOLS = {"render_markdown", "write_file"}
    _ALLOWED_TOOLS = _SOURCE_TOOLS | _OUTPUT_TOOLS

    @staticmethod
    def _bounded_text(value: Any) -> str:
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if isinstance(value, (dict, list)):
            return json.dumps(value, sort_keys=True, ensure_ascii=True)
        return str(value or "").strip()

    def execute(
        self,
        workflow: CapabilityPackLoadedWorkflow,
        request: CapabilityPackLocalExecutionRequest,
        *,
        source_payload: Any = None,
        goal_snapshot: Mapping[str, Any] | str | None = None,
        max_artifact_bytes: int,
    ) -> CapabilityPackGovernedWorkflowResult:
        if not workflow.steps:
            raise CapabilityPackLifecycleError(
                "reviewed local workflow has no executable step contract"
            )
        source_text = self._bounded_text(source_payload)
        snapshot_text = self._bounded_text(goal_snapshot)
        if request.domain == "primary" and not source_text:
            raise CapabilityPackLifecycleError("governed local source step returned no content")
        if request.domain == "secondary" and not snapshot_text:
            raise CapabilityPackLifecycleError("governed goal snapshot step returned no content")
        if len((source_text or snapshot_text).encode("utf-8")) > MAX_LOCAL_SOURCE_BYTES:
            raise CapabilityPackLifecycleError("governed local input exceeds the source limit")

        if request.domain == "primary":
            content = (
                f"# Local research brief\n\nGoal: {request.goal_id}\n"
                f"Query: {request.query or request.goal_id}\n"
                f"Source: {request.source_url}\n\n{source_text}\n"
            )
            expected_source_tools = {"web_search", "local_source"}
        else:
            content = f"# Goal snapshot\n\nGoal: {request.goal_id}\n\n{snapshot_text}\n"
            expected_source_tools = {"get_goals"}

        step_receipts: list[dict[str, Any]] = []
        source_seen = False
        output_seen = False
        for step in workflow.steps:
            tool_name = str(step.tool).strip()
            if tool_name not in self._ALLOWED_TOOLS:
                raise CapabilityPackLifecycleError(
                    f"reviewed local workflow requests unsupported tool: {tool_name}"
                )
            if tool_name in self._SOURCE_TOOLS:
                if tool_name not in expected_source_tools:
                    raise CapabilityPackLifecycleError(
                        f"governed local workflow source tool does not match {request.domain} domain"
                    )
                result = source_text if request.domain == "primary" else snapshot_text
                source_seen = True
                effect = "intercepted_input"
            elif tool_name == "render_markdown":
                result = content
                effect = "staged_output"
                output_seen = True
            else:
                # ``write_file`` is intentionally staged.  The lifecycle
                # transaction owns the sole filesystem effect after its final
                # authority and cancellation check.
                result = content
                effect = "staged_artifact_write"
                output_seen = True
            step_receipts.append(
                {
                    "step_id": step.step_id,
                    "tool": tool_name,
                    "status": "succeeded",
                    "effect": effect,
                    "result_digest": canonical_digest(result),
                }
            )

        if not source_seen:
            raise CapabilityPackLifecycleError(
                "reviewed local workflow must contain one admitted source step"
            )
        if not output_seen:
            raise CapabilityPackLifecycleError(
                "reviewed local workflow must contain one staged output step"
            )
        if f"Goal: {request.goal_id}" not in content.splitlines():
            raise CapabilityPackLifecycleError(
                "governed local workflow output is missing the canonical goal identity"
            )
        if len(content.encode("utf-8")) > max_artifact_bytes:
            raise CapabilityPackLifecycleError(
                "governed local workflow output exceeds the reviewed artifact limit"
            )
        return CapabilityPackGovernedWorkflowResult(
            content=content,
            steps=tuple(step_receipts),
        )


def _local_scope_requirements(workflow: CapabilityPackLoadedWorkflow) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return the reviewed scopes required by one local workflow invocation."""

    required_tools = tuple(dict.fromkeys(tool.strip() for tool in workflow.step_tools if tool.strip()))
    # Local input is injected by the caller and never read from the host.  The
    # only filesystem effect performed by this seam is the bounded artifact.
    required_filesystem = ("artifact_write",)
    return required_tools, required_filesystem


@dataclass(frozen=True)
class ActiveVersionPointer:
    pack_id: str
    version: str
    digest: str
    goal_id: str
    review_id: str
    authority_digest: str
    owner_principal_id: str
    session_id: str
    status: str = "active"
    previous_version: str | None = None
    previous_digest: str | None = None
    root_path: str | None = None

    def as_dict(self, *, include_path: bool = False) -> dict[str, Any]:
        payload = {
            "pack_id": self.pack_id,
            "version": self.version,
            "digest": self.digest,
            "goal_id": self.goal_id,
            "review_id": self.review_id,
            "authority_digest": self.authority_digest,
            "owner_principal_id": self.owner_principal_id,
            "session_id": self.session_id,
            "status": self.status,
            "previous_version": self.previous_version,
            "previous_digest": self.previous_digest,
        }
        if include_path:
            payload["root_path"] = self.root_path
        return payload


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_receipt_id(*parts: Any) -> str:
    return f"capability-pack:{canonical_digest(*parts)[:24]}"


def _safe_pack_path(path: str | Path) -> str:
    return str(Path(path).resolve())


def _default_seraph_version() -> str:
    """Read the repository runtime version without importing the registry."""

    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    try:
        content = pyproject.read_text(encoding="utf-8")
    except OSError:
        return "0"
    match = re.search(r"(?m)^version\s*=\s*['\"]([^'\"]+)['\"]", content)
    return match.group(1) if match else "0"


def _public_pointer(pointer: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(pointer, Mapping):
        return None
    return {key: deepcopy(value) for key, value in pointer.items() if key != "root_path"}


def _safe_canary_artifact_root(path: str | Path) -> Path:
    """Create a local artifact directory without following a package link."""

    candidate = Path(path)
    for parent in (candidate, *candidate.parents):
        try:
            mode = parent.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise CapabilityPackLifecycleError("canary artifact root cannot contain symlinked directories")
        if parent == parent.parent:
            break
    try:
        candidate.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CapabilityPackLifecycleError("canary artifact root could not be created safely") from exc
    try:
        reject_symlink_entries(candidate)
    except ValueError as exc:
        raise CapabilityPackLifecycleError(str(exc)) from exc
    if not candidate.is_dir():
        raise CapabilityPackLifecycleError("canary artifact root must be a directory")
    return candidate


def _write_canary_artifact(path: Path, content: bytes) -> None:
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as exc:
        raise CapabilityPackLifecycleError("canary artifact could not be written safely") from exc
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception as exc:
        raise CapabilityPackLifecycleError("canary artifact could not be written safely") from exc


class CapabilityPackLifecycle:
    """Reviewed lifecycle over the existing extension state path.

    The state file is protected by an OS advisory lock in addition to the
    in-process re-entrant lock.  Platforms without ``fcntl`` fail closed
    rather than pretending that a JSON write is a cross-process CAS.
    """

    _lock_guard = threading.Lock()
    _locks: dict[str, threading.RLock] = {}

    def __init__(self, state_path: str | Path | None = None, *, seraph_version: str | None = None):
        # Use the existing extension state location by default.  Tests and
        # isolated installers may still provide a dedicated state file.
        self.state_path = Path(state_path or extension_state_path())
        self.lock_path = Path(f"{self.state_path}.lock")
        self.seraph_version = str(seraph_version or _default_seraph_version()).strip()
        if not self.seraph_version:
            raise CapabilityPackLifecycleError("Seraph runtime version is required for pack compatibility")
        key = str(self.state_path.resolve())
        with self._lock_guard:
            self._lock = self._locks.setdefault(key, threading.RLock())

    @contextmanager
    def _state_lock(self, *, shared: bool = False) -> Iterator[None]:
        """Hold the process and OS lock for one complete state transaction."""

        if fcntl is None:
            raise CapabilityPackLifecycleError("cross-process lifecycle lock is unavailable")
        with self._lock:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            try:
                try:
                    os.fchmod(descriptor, 0o600)
                    operation = fcntl.LOCK_SH if shared else fcntl.LOCK_EX
                    fcntl.flock(descriptor, operation)
                except OSError as exc:
                    raise CapabilityPackLifecycleError("cross-process lifecycle lock could not be acquired") from exc
                yield
            finally:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
                os.close(descriptor)

    def _empty_state(self) -> dict[str, Any]:
        return {
            "schema_version": CAPABILITY_PACK_LIFECYCLE_SCHEMA,
            "generation": 0,
            "active": {},
            "versions": {},
            "reviews": {},
            "revoked": {},
            "receipts": [],
            "jobs": {},
            "approvals": {},
            "canary_attempts": {},
            "local_executions": {},
            "reconciliation": {"status": "clean", "updated_at": None, "changes": []},
        }

    def _load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return self._empty_state()
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CapabilityPackLifecycleError("capability-pack lifecycle state is unreadable") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") not in {None, CAPABILITY_PACK_LIFECYCLE_SCHEMA}:
            raise CapabilityPackLifecycleError("unsupported capability-pack lifecycle state schema")
        state = self._empty_state()
        state.update(payload)
        for key in (
            "active",
            "versions",
            "reviews",
            "revoked",
            "jobs",
            "approvals",
            "canary_attempts",
            "local_executions",
        ):
            if not isinstance(state.get(key), dict):
                raise CapabilityPackLifecycleError(f"lifecycle state field {key} is invalid")
        if not isinstance(state.get("receipts"), list):
            raise CapabilityPackLifecycleError("lifecycle receipts are invalid")
        return state

    def _atomic_save(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.state_path.name}.", dir=str(self.state_path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
            os.chmod(self.state_path, 0o600)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def _record_receipt(self, state: dict[str, Any], *, action: str, status: str, pack_id: str, details: Mapping[str, Any] | None = None) -> dict[str, Any]:
        receipt = {
            "id": _stable_receipt_id(pack_id, action, state.get("generation", 0), details or {}),
            "schema_version": CAPABILITY_PACK_LIFECYCLE_SCHEMA,
            "action": action,
            "status": status,
            "pack_id": pack_id,
            "created_at": _utc_now(),
            "details": deepcopy(dict(details or {})),
        }
        state.setdefault("receipts", []).append(receipt)
        state["receipts"] = state["receipts"][-200:]
        return receipt

    @staticmethod
    def _cancel_pack_jobs(
        state: dict[str, Any],
        pack_id: str,
        *,
        digest: str | None,
        reason: str,
        statuses: set[str] | frozenset[str] | None = None,
    ) -> int:
        jobs = state.setdefault("jobs", {})
        cancelled = 0
        for job in jobs.values():
            if not isinstance(job, dict) or job.get("pack_id") != pack_id:
                continue
            if digest is not None and job.get("digest") != digest:
                continue
            if statuses is not None and job.get("status") not in statuses:
                continue
            if job.get("status") in {"succeeded", "failed", "cancelled", "expired"}:
                continue
            job["status"] = "cancelled"
            job["cancel_requested"] = True
            job["cancel_reason"] = reason
            cancelled += 1
        return cancelled

    @staticmethod
    def _record_depends_on(
        record: Mapping[str, Any],
        *,
        dependency_pack_id: str,
        dependency_digest: str,
    ) -> bool:
        dependencies = record.get("dependencies")
        if not isinstance(dependencies, (list, tuple)):
            return False
        return any(
            isinstance(dependency, Mapping)
            and dependency.get("id") == dependency_pack_id
            and dependency.get("digest") == dependency_digest
            for dependency in dependencies
        )

    def _cascade_dependency_revocation(
        self,
        state: dict[str, Any],
        *,
        dependency_pack_id: str,
        dependency_digest: str,
    ) -> list[dict[str, Any]]:
        """Quarantine every reviewed version that transitively needs a revoke.

        Revoking a dependency is an authority change for all consumers.  The
        cascade is applied while the caller holds the lifecycle transaction
        lock, so no dependent pointer or pinned job can cross the revocation
        boundary between the dependency check and the state commit.
        """

        pending = [(dependency_pack_id, dependency_digest)]
        seen: set[tuple[str, str]] = set()
        affected: list[dict[str, Any]] = []
        versions_by_pack = state.get("versions")
        while pending:
            revoked_pack, revoked_digest = pending.pop(0)
            source = (revoked_pack, revoked_digest)
            if source in seen:
                continue
            seen.add(source)
            if not isinstance(versions_by_pack, Mapping):
                break
            for candidate_pack_id, raw_versions in versions_by_pack.items():
                if not isinstance(raw_versions, Mapping):
                    continue
                for raw_digest, raw_record in raw_versions.items():
                    if not isinstance(raw_record, Mapping):
                        continue
                    candidate_digest = str(raw_digest)
                    if not _DIGEST_RE.fullmatch(candidate_digest):
                        continue
                    if not self._record_depends_on(
                        raw_record,
                        dependency_pack_id=revoked_pack,
                        dependency_digest=revoked_digest,
                    ):
                        continue
                    candidate_id = str(candidate_pack_id)
                    if candidate_id == revoked_pack and candidate_digest == revoked_digest:
                        continue
                    revoked_digests = state.setdefault("revoked", {}).setdefault(candidate_id, [])
                    was_revoked = candidate_digest in revoked_digests
                    if not was_revoked:
                        revoked_digests.append(candidate_digest)
                    pointer = state.setdefault("active", {}).get(candidate_id)
                    pointer_fenced = False
                    if (
                        isinstance(pointer, Mapping)
                        and pointer.get("digest") == candidate_digest
                        and pointer.get("status") in {"active", "paused"}
                    ):
                        next_pointer = dict(pointer)
                        next_pointer.update(
                            {
                                "status": "revoked",
                                "revoked_by_dependency": revoked_pack,
                                "revoked_dependency_digest": revoked_digest,
                            }
                        )
                        state["active"][candidate_id] = next_pointer
                        pointer_fenced = True
                    cancelled_jobs = self._cancel_pack_jobs(
                        state,
                        candidate_id,
                        digest=candidate_digest,
                        reason="dependency_revoked",
                    )
                    if not was_revoked or pointer_fenced or cancelled_jobs:
                        affected.append(
                            {
                                "pack_id": candidate_id,
                                "digest": candidate_digest,
                                "pointer_fenced": pointer_fenced,
                                "cancelled_jobs": cancelled_jobs,
                                "dependency_pack_id": revoked_pack,
                                "dependency_digest": revoked_digest,
                            }
                        )
                    pending.append((candidate_id, candidate_digest))
        return affected

    def register_job(
        self,
        pack_id: str,
        *,
        goal_id: str,
        job_id: str,
        status: str = "accepted",
        now: datetime | None = None,
        owner_principal_id: str | None = None,
        session_id: str | None = None,
        request_fingerprint: str | None = None,
        request_contract: Mapping[str, Any] | None = None,
        required_tools: Iterable[str] = (),
        required_filesystem: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Persist one bounded job contract for a future governed runtime.

        This is admission metadata only.  Until the #743/#747 adapter is
        *actually* wired, ``run_canary`` remains blocked and this method never
        dispatches or claims provider work.
        """

        if status not in {"accepted", "queued", "running", "paused"}:
            raise CapabilityPackLifecycleError("unsupported capability-pack job status")
        pack_id = _validate_pack_id(pack_id)
        goal_id = _validate_goal_id(goal_id)
        job_id = _validate_goal_id(job_id)
        required_tools = tuple(str(item).strip() for item in required_tools if str(item).strip())
        required_filesystem = tuple(str(item).strip() for item in required_filesystem if str(item).strip())
        normalized_tools = tuple(sorted({item for item in required_tools if item}))
        normalized_filesystem = tuple(sorted({item for item in required_filesystem if item}))
        normalized_request_contract = deepcopy(dict(request_contract or {}))
        with self._state_lock():
            state = self._load()
            pointer = state["active"].get(pack_id)
            if not isinstance(pointer, Mapping) or pointer.get("status") != "active":
                raise CapabilityPackLifecycleError("job admission requires an active pack")
            if pointer.get("goal_id") != goal_id or not self._pointer_binding_valid(state, pack_id, pointer):
                raise CapabilityPackLifecycleError("job admission binding is invalid")
            pointer_owner, pointer_session = self._require_pointer_identity(
                pointer,
                owner_principal_id=(
                    owner_principal_id
                    if owner_principal_id is not None
                    else str(pointer.get("owner_principal_id") or "")
                ),
                session_id=(
                    session_id
                    if session_id is not None
                    else str(pointer.get("session_id") or "")
                ),
            )
            canonical_request_fingerprint = canonical_digest(
                "capability-pack-job-v3",
                pack_id,
                goal_id,
                job_id,
                pointer.get("version"),
                pointer.get("digest"),
                normalized_request_contract,
                normalized_tools,
                normalized_filesystem,
            )
            if request_fingerprint is not None:
                supplied_request_fingerprint = str(request_fingerprint)
                if not _DIGEST_RE.fullmatch(supplied_request_fingerprint):
                    raise CapabilityPackLifecycleError("job request fingerprint must be a SHA-256 digest")
                if supplied_request_fingerprint != canonical_request_fingerprint:
                    raise CapabilityPackLifecycleError("job request fingerprint does not cover the immutable request contract")
            request_fingerprint = canonical_request_fingerprint
            existing = state["jobs"].get(job_id)
            contract = self._execution_contract_from_state(
                state,
                pack_id=pack_id,
                goal_id=goal_id,
                job_id=job_id,
                now=now,
                required_tools=required_tools,
                required_filesystem=required_filesystem,
            )
            if isinstance(existing, Mapping):
                if existing.get("idempotency_key") != contract.idempotency_key:
                    raise CapabilityPackLifecycleError("job idempotency key conflicts with an existing job")
                if existing.get("request_fingerprint") != request_fingerprint:
                    raise CapabilityPackLifecycleError("job request fingerprint conflicts with the immutable job pin")
                existing_contract = existing.get("request_contract")
                if not isinstance(existing_contract, Mapping) or canonical_digest(dict(existing_contract)) != canonical_digest(normalized_request_contract):
                    raise CapabilityPackLifecycleError("job request contract conflicts with the immutable job pin")
                existing_tools = tuple(sorted({str(item).strip() for item in (existing.get("required_tools") or []) if str(item).strip()}))
                existing_filesystem = tuple(sorted({str(item).strip() for item in (existing.get("required_filesystem") or []) if str(item).strip()}))
                if existing_tools != normalized_tools or existing_filesystem != normalized_filesystem:
                    raise CapabilityPackLifecycleError("job authority scopes conflict with the immutable job pin")
                if existing.get("owner_principal_id") != pointer_owner:
                    raise CapabilityPackLifecycleError("job owner identity conflicts with the immutable job pin")
                if existing.get("session_id") != pointer_session:
                    raise CapabilityPackLifecycleError("job session identity conflicts with the immutable job pin")
                return {"status": "deduped", "job": deepcopy(dict(existing))}
            if len(state["jobs"]) >= MAX_PACK_JOBS:
                raise CapabilityPackLifecycleError("capability-pack job ledger is full")
            job = {
                **contract.as_dict(),
                "pack_id": pack_id,
                "version": pointer.get("version"),
                "digest": pointer.get("digest"),
                "status": status,
                "cancel_requested": False,
                "owner_principal_id": pointer_owner,
                "session_id": pointer_session,
                "request_fingerprint": request_fingerprint,
                "request_contract": normalized_request_contract,
                "required_tools": list(normalized_tools),
                "required_filesystem": list(normalized_filesystem),
            }
            state["jobs"][job_id] = job
            receipt = self._record_receipt(state, action="job:admit", status=status, pack_id=pack_id, details={key: value for key, value in job.items() if key not in {"root_path"}})
            self._commit(state)
        return {"status": status, "job": deepcopy(job), "receipt": receipt}

    def _commit(self, state: dict[str, Any]) -> None:
        state["generation"] = int(state.get("generation") or 0) + 1
        self._atomic_save(state)

    @staticmethod
    def _coerce_manifest(manifest: CapabilityPackManifest | Mapping[str, Any]) -> CapabilityPackManifest:
        return manifest if isinstance(manifest, CapabilityPackManifest) else parse_capability_pack_manifest(manifest)

    def _assert_compatible(self, pack: CapabilityPackManifest) -> None:
        try:
            compatible = pack.compatibility.is_compatible_with(self.seraph_version)
        except ValueError as exc:
            raise CapabilityPackLifecycleError(str(exc)) from exc
        if not compatible:
            raise CapabilityPackLifecycleError(
                f"pack compatibility {pack.compatibility.seraph!r} excludes Seraph {self.seraph_version}"
            )

    @staticmethod
    def _record_delta(previous: Mapping[str, Any] | None, candidate: CapabilityPackManifest) -> dict[str, Any]:
        previous = previous if isinstance(previous, Mapping) else {}
        delta = _authority_delta_from_payloads(
            previous.get("authority") if isinstance(previous.get("authority"), Mapping) else {},
            previous.get("data_policy") if isinstance(previous.get("data_policy"), Mapping) else {},
            candidate.authority.model_dump(mode="json"),
            candidate.data_policy.model_dump(mode="json"),
        )
        delta["authority_digest_before"] = previous.get("authority_digest")
        delta["authority_digest_after"] = candidate.authority_digest
        return delta

    @staticmethod
    def _store_approval(
        state: dict[str, Any],
        *,
        action: str,
        pack_id: str,
        version: str,
        digest: str,
        goal_id: str,
        current_digest: str | None,
        authority_delta_payload: Mapping[str, Any],
        approved_by: str,
        owner_principal_id: str | None = None,
        session_id: str | None = None,
        content_digest: str | None = None,
        authority_digest: str | None = None,
    ) -> dict[str, Any]:
        owner_principal_id = str(owner_principal_id or approved_by).strip()
        session_id = str(session_id or "legacy-local-session").strip()
        content_digest = str(content_digest or digest).strip()
        authority_digest = str(authority_digest or "").strip() or None
        approval_digest = _approval_digest(
            action=action,
            pack_id=pack_id,
            version=version,
            digest=digest,
            goal_id=goal_id,
            current_digest=current_digest,
            authority_delta_payload=authority_delta_payload,
            owner_principal_id=owner_principal_id,
            session_id=session_id,
            content_digest=content_digest,
            authority_digest=authority_digest,
        )
        approval_id = f"capability-pack-approval:{approval_digest[:24]}"
        approval = {
            "approval_id": approval_id,
            "status": "approved",
            "action": action,
            "pack_id": pack_id,
            "version": version,
            "digest": digest,
            "goal_id": goal_id,
            "current_digest": current_digest,
            "authority_delta": deepcopy(dict(authority_delta_payload)),
            "authority_delta_digest": canonical_digest(authority_delta_payload),
            "approval_digest": approval_digest,
            "approved_by": approved_by,
            "owner_principal_id": owner_principal_id,
            "session_id": session_id,
            "content_digest": content_digest,
            "authority_digest": authority_digest,
            "approved_at": _utc_now(),
        }
        existing = state.setdefault("approvals", {}).get(approval_id)
        if isinstance(existing, Mapping) and any(existing.get(key) != approval.get(key) for key in (
            "action", "pack_id", "version", "digest", "goal_id", "current_digest", "authority_delta_digest",
            "owner_principal_id", "session_id", "content_digest", "authority_digest",
        )):
            raise CapabilityPackLifecycleError("approval identity is already bound to a different action")
        state["approvals"][approval_id] = approval
        return approval

    @staticmethod
    def _require_approval(
        state: Mapping[str, Any],
        *,
        approval_id: str | None,
        action: str,
        pack_id: str,
        version: str,
        digest: str,
        goal_id: str,
        current_digest: str | None,
        authority_delta_payload: Mapping[str, Any],
        owner_principal_id: str | None = None,
        session_id: str | None = None,
        content_digest: str | None = None,
        authority_digest: str | None = None,
    ) -> Mapping[str, Any]:
        approvals = state.get("approvals")
        approval = approvals.get(approval_id) if isinstance(approvals, Mapping) and approval_id else None
        # Legacy callers did not carry an authenticated identity.  When the
        # identity fields are omitted, bind the check to the persisted
        # approval values; authenticated callers must provide all four exact
        # fields and therefore cannot replay another operator's approval.
        requested_owner_principal_id = str(owner_principal_id or "").strip() or None
        requested_session_id = str(session_id or "").strip() or None
        requested_content_digest = str(content_digest or "").strip() or None
        requested_authority_digest = str(authority_digest or "").strip() or None
        approval_owner_principal_id = requested_owner_principal_id
        approval_session_id = requested_session_id
        approval_content_digest = requested_content_digest or digest
        approval_authority_digest = requested_authority_digest
        if isinstance(approval, Mapping):
            approval_owner_principal_id = str(approval.get("owner_principal_id") or approval_owner_principal_id or "").strip() or None
            approval_session_id = str(approval.get("session_id") or approval_session_id or "").strip() or None
            approval_content_digest = str(approval.get("content_digest") or approval_content_digest or digest).strip()
            approval_authority_digest = str(approval.get("authority_digest") or approval_authority_digest or "").strip() or None
        expected_digest = _approval_digest(
            action=action,
            pack_id=pack_id,
            version=version,
            digest=digest,
            goal_id=goal_id,
            current_digest=current_digest,
            authority_delta_payload=authority_delta_payload,
            owner_principal_id=approval_owner_principal_id,
            session_id=approval_session_id,
            content_digest=approval_content_digest,
            authority_digest=approval_authority_digest,
        )
        if (
            not isinstance(approval, Mapping)
            or approval.get("status") != "approved"
            or approval.get("approval_digest") != expected_digest
            or approval.get("approval_id") != f"capability-pack-approval:{expected_digest[:24]}"
            or (requested_owner_principal_id is not None and approval.get("owner_principal_id") != requested_owner_principal_id)
            or (requested_session_id is not None and approval.get("session_id") != requested_session_id)
            or (requested_content_digest is not None and approval.get("content_digest") != requested_content_digest)
            or (requested_authority_digest is not None and approval.get("authority_digest") != requested_authority_digest)
        ):
            raise CapabilityPackLifecycleError(
                f"durable operator approval is required for {action} with the exact goal/digest/authority delta"
            )
        return approval

    def create_operator_approval(
        self,
        pack_id: str,
        *,
        action: str,
        goal_id: str,
        digest: str | None = None,
        version: str | None = None,
        current_digest: str | None = None,
        authority_delta_payload: Mapping[str, Any] | None = None,
        approved_by: str = "operator",
        owner_principal_id: str | None = None,
        owner_id: str | None = None,
        session_id: str | None = None,
        operator_session_id: str | None = None,
        content_digest: str | None = None,
        approval_content_digest: str | None = None,
        authority_digest: str | None = None,
    ) -> dict[str, Any]:
        """Persist one exact action/goal/digest/authority operator approval."""

        pack_id = _validate_pack_id(pack_id)
        goal_id = _validate_goal_id(goal_id)
        approved_by = _validate_goal_id(approved_by)
        owner_principal_id = owner_principal_id or owner_id or approved_by
        session_id = session_id or operator_session_id or "legacy-local-session"
        owner_principal_id = _validate_goal_id(owner_principal_id)
        session_id = _validate_goal_id(session_id)
        if content_digest is not None and approval_content_digest is not None and content_digest != approval_content_digest:
            raise CapabilityPackLifecycleError("approval content digest aliases disagree")
        content_digest = content_digest or approval_content_digest
        if action not in {"activate", "update", "pause", "revoke", "uninstall", "rollback"}:
            raise CapabilityPackLifecycleError("unsupported operator approval action")
        with self._state_lock():
            state = self._load()
            pointer = state["active"].get(pack_id)
            if digest is None and isinstance(pointer, Mapping):
                digest = str(pointer.get("digest") or "")
            if version is None and isinstance(pointer, Mapping) and pointer.get("digest") == digest:
                version = str(pointer.get("version") or "")
            if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
                raise CapabilityPackLifecycleError("approval requires a reviewed package digest")
            record = state["versions"].get(pack_id, {}).get(digest)
            if not isinstance(record, Mapping):
                raise CapabilityPackLifecycleError("approval requires a reviewed package version")
            if not isinstance(version, str) or not version.strip():
                version = str(record.get("version") or "")
            if not version:
                raise CapabilityPackLifecycleError("approval requires a reviewed package version")
            if record.get("version") != version or record.get("goal_id") != goal_id:
                raise CapabilityPackLifecycleError("approval must match the reviewed version and goal")
            expected_content_digest = str(content_digest or digest)
            if expected_content_digest != digest or not _DIGEST_RE.fullmatch(expected_content_digest):
                raise CapabilityPackLifecycleError("approval content digest must exactly match the reviewed package digest")
            expected_authority_digest = str(authority_digest or record.get("authority_digest") or "")
            if expected_authority_digest != str(record.get("authority_digest") or ""):
                raise CapabilityPackLifecycleError("approval authority digest must exactly match the reviewed authority")
            if current_digest is None and action in {"activate", "update", "rollback"} and isinstance(pointer, Mapping) and pointer.get("status") in {"active", "paused"}:
                current_digest = str(pointer.get("digest") or "") or None
            if authority_delta_payload is not None and not isinstance(authority_delta_payload, Mapping):
                raise CapabilityPackLifecycleError("authority delta must be a mapping")
            delta = authority_delta_payload if authority_delta_payload is not None else {}
            if action in {"activate", "update", "rollback"}:
                current_record = None
                if isinstance(pointer, Mapping) and pointer.get("status") in {"active", "paused"}:
                    current_record = state["versions"].get(pack_id, {}).get(pointer.get("digest"))
                    if not isinstance(current_record, Mapping):
                        current_record = None
                expected_delta = _authority_delta_from_payloads(
                    current_record.get("authority") if isinstance(current_record, Mapping) else {},
                    current_record.get("data_policy") if isinstance(current_record, Mapping) else {},
                    record.get("authority") if isinstance(record.get("authority"), Mapping) else {},
                    record.get("data_policy") if isinstance(record.get("data_policy"), Mapping) else {},
                )
                expected_delta["authority_digest_before"] = current_record.get("authority_digest") if isinstance(current_record, Mapping) else None
                expected_delta["authority_digest_after"] = record.get("authority_digest")
                if authority_delta_payload is None:
                    delta = expected_delta
                elif canonical_digest(delta) != canonical_digest(expected_delta):
                    raise CapabilityPackLifecycleError(
                        "approval authority delta does not match the reviewed transition"
                    )
            approval = self._store_approval(
                state,
                action=action,
                pack_id=pack_id,
                version=version,
                digest=digest,
                goal_id=goal_id,
                current_digest=current_digest,
                authority_delta_payload=delta,
                approved_by=approved_by,
                owner_principal_id=owner_principal_id,
                session_id=session_id,
                content_digest=expected_content_digest,
                authority_digest=expected_authority_digest,
            )
            receipt = self._record_receipt(
                state,
                action=f"approval:{action}",
                status="approved",
                pack_id=pack_id,
                details={"approval_id": approval["approval_id"], "version": version, "digest": digest, "goal_id": goal_id, "authority_delta_digest": approval["authority_delta_digest"]},
            )
            self._commit(state)
        return {"approval": deepcopy(approval), "receipt": receipt}

    @staticmethod
    def _execution_contract_from_state(
        state: Mapping[str, Any],
        *,
        pack_id: str,
        goal_id: str,
        job_id: str,
        now: datetime | None = None,
        required_tools: Iterable[str] = (),
        required_filesystem: Iterable[str] = (),
    ) -> CapabilityPackExecutionContract:
        pointer = state.get("active", {}).get(pack_id) if isinstance(state.get("active"), Mapping) else None
        if not isinstance(pointer, Mapping) or pointer.get("status") != "active":
            raise CapabilityPackLifecycleError("execution contract requires an active pack")
        if pointer.get("goal_id") != goal_id:
            raise CapabilityPackLifecycleError("execution contract binding is invalid")
        records = state.get("versions", {}).get(pack_id) if isinstance(state.get("versions"), Mapping) else None
        record = records.get(pointer.get("digest")) if isinstance(records, Mapping) else None
        if not isinstance(record, Mapping):
            raise CapabilityPackLifecycleError("execution contract version is unavailable")
        authority = record.get("authority")
        if not isinstance(authority, Mapping) or authority.get("approval") != "always":
            raise CapabilityPackLifecycleError(
                "execution contract requires authority.approval: always"
            )
        granted_tools_raw = authority.get("tools")
        granted_filesystem_raw = authority.get("filesystem")
        granted_tools = {
            str(item).strip()
            for item in granted_tools_raw
            if str(item).strip()
        } if isinstance(granted_tools_raw, (list, tuple, set)) else set()
        granted_filesystem = {
            str(item).strip()
            for item in granted_filesystem_raw
            if str(item).strip()
        } if isinstance(granted_filesystem_raw, (list, tuple, set)) else set()
        required_tool_set = {str(item).strip() for item in required_tools if str(item).strip()}
        required_filesystem_set = {str(item).strip() for item in required_filesystem if str(item).strip()}
        if not granted_tools or not granted_filesystem:
            raise CapabilityPackLifecycleError("reviewed authority scopes are empty")
        missing_tools = sorted(required_tool_set - granted_tools)
        if missing_tools:
            raise CapabilityPackLifecycleError(
                f"reviewed authority is missing required tools: {', '.join(missing_tools)}"
            )
        missing_filesystem = sorted(required_filesystem_set - granted_filesystem)
        if missing_filesystem:
            raise CapabilityPackLifecycleError(
                f"reviewed authority is missing required filesystem scopes: {', '.join(missing_filesystem)}"
            )
        resources = record.get("resources") if isinstance(record.get("resources"), Mapping) else {}
        runtime_seconds = int(resources.get("max_runtime_seconds", 0) or 0)
        artifact_bytes = int(resources.get("max_artifact_bytes", 0) or 0)
        cost = int(resources.get("max_inference_cost_microusd", 0) or 0)
        priority = _INFERENCE_PRIORITY_RANK.get(str(resources.get("inference_priority")), 0)
        if runtime_seconds <= 0 or artifact_bytes <= 0 or priority <= 0:
            raise CapabilityPackLifecycleError("reviewed resource limits are incomplete")
        started = now or datetime.now(timezone.utc)
        deadline = started.astimezone(timezone.utc).timestamp() + runtime_seconds
        deadline_at = datetime.fromtimestamp(deadline, timezone.utc).isoformat()
        return CapabilityPackExecutionContract(
            job_id=job_id,
            idempotency_key=f"capability-pack:{canonical_digest(pack_id, pointer.get('version'), pointer.get('digest'), goal_id, job_id)}",
            goal_id=goal_id,
            deadline_at=deadline_at,
            priority=priority,
            max_inference_cost_microusd=cost,
            max_artifact_bytes=artifact_bytes,
            cancel_on_revoke=str(
                (record.get("lifecycle") if isinstance(record.get("lifecycle"), Mapping) else {}).get(
                    "revoke_running_jobs", "cancel_at_safe_checkpoint"
                )
            ) != "leave_pinned_until_completion",
        )

    def build_execution_contract(
        self,
        pack_id: str,
        *,
        goal_id: str,
        job_id: str,
        now: datetime | None = None,
    ) -> CapabilityPackExecutionContract:
        """Build bounded #743/#747 inputs without dispatching a provider call."""

        pack_id = _validate_pack_id(pack_id)
        goal_id = _validate_goal_id(goal_id)
        job_id = _validate_goal_id(job_id)
        with self._state_lock(shared=True):
            state = self._load()
            pointer = state.get("active", {}).get(pack_id) if isinstance(state.get("active"), Mapping) else None
            if not isinstance(pointer, Mapping) or not self._pointer_binding_valid(state, pack_id, pointer):
                raise CapabilityPackLifecycleError("execution contract binding is invalid")
            return self._execution_contract_from_state(state, pack_id=pack_id, goal_id=goal_id, job_id=job_id, now=now)

    @staticmethod
    def _review_from_state(state: Mapping[str, Any], review_id: str) -> Mapping[str, Any]:
        reviews = state.get("reviews")
        review = reviews.get(review_id) if isinstance(reviews, Mapping) else None
        if not isinstance(review, Mapping):
            raise CapabilityPackLifecycleError("exact reviewed pack binding is required")
        return review

    @staticmethod
    def _canonical_package_binding(
        root_path: str | Path,
    ) -> tuple[CapabilityPackManifest, str, dict[str, Any]] | None:
        """Recompute the reviewed package identity from its current contents."""

        root = Path(root_path)
        try:
            canonical_root = _safe_pack_path(root)
            if canonical_root != str(root):
                return None
            validation = validate_capability_pack_path(root)
            if not validation.get("ok"):
                return None
            payload = validation.get("manifest")
            if not isinstance(payload, Mapping):
                return None
            manifest = parse_capability_pack_manifest(payload, source=str(root / "manifest.yaml"))
            digest = capability_pack_digest(root)
            publisher_trust = publisher_trust_status(manifest, package_root=root)
            if manifest.signature.state == "integrity-checked" and not publisher_trust["integrity_checked"]:
                return None
            return manifest, digest, publisher_trust
        except (CapabilityPackError, CapabilityPackManifestError, OSError, UnicodeDecodeError, ValueError, RuntimeError):
            return None

    @staticmethod
    def _pointer_binding_valid(
        state: Mapping[str, Any],
        pack_id: str,
        pointer: Mapping[str, Any],
    ) -> bool:
        try:
            pointer_owner = _validate_goal_id(str(pointer.get("owner_principal_id") or ""))
            pointer_session = _validate_goal_id(str(pointer.get("session_id") or ""))
        except CapabilityPackLifecycleError:
            # Rows written before authenticated lifecycle identity existed must
            # not regain authority through a compatibility fallback.
            return False
        versions = state.get("versions")
        records = versions.get(pack_id) if isinstance(versions, Mapping) else None
        record = records.get(pointer.get("digest")) if isinstance(records, Mapping) else None
        reviews = state.get("reviews")
        review = reviews.get(pointer.get("review_id")) if isinstance(reviews, Mapping) else None
        if not isinstance(record, Mapping) or not isinstance(review, Mapping):
            return False
        root_path = record.get("root_path")
        if not isinstance(root_path, str):
            return False
        canonical = CapabilityPackLifecycle._canonical_package_binding(root_path)
        if canonical is None:
            return False
        manifest, digest, publisher_trust = canonical
        dependency_errors = validate_capability_pack_dependencies(
            manifest,
            _dependency_records_from_state(state, manifest),
        )
        if dependency_errors:
            return False
        review_id = _review_digest(
            pack_id=manifest.id,
            version=manifest.version,
            digest=digest,
            goal_id=str(pointer.get("goal_id") or ""),
            authority_digest=manifest.authority_digest,
        )
        canonical_record = {
            "pack_id": manifest.id,
            "version": manifest.version,
            "digest": digest,
            "goal_id": pointer.get("goal_id"),
            "authority_digest": manifest.authority_digest,
            "authority": manifest.authority.model_dump(mode="json"),
            "resources": manifest.resources.model_dump(mode="json"),
            "data_policy": manifest.data_policy.model_dump(mode="json"),
            "compatibility": manifest.compatibility.model_dump(mode="json"),
            "dependencies": _dependency_bindings(manifest),
            "dependencies_digest": _dependencies_digest(manifest),
            "root_path": _safe_pack_path(root_path),
            "lifecycle": manifest.lifecycle.model_dump(mode="json"),
            "review_id": review_id,
            "revoked": False,
        }
        record_fields = tuple(canonical_record)
        if any(record.get(field_name) != canonical_record[field_name] for field_name in record_fields):
            return False
        canonical_review = {
            "review_id": review_id,
            "status": "approved",
            "pack_id": manifest.id,
            "version": manifest.version,
            "digest": digest,
            "goal_id": pointer.get("goal_id"),
            "authority_digest": manifest.authority_digest,
            "dependencies": _dependency_bindings(manifest),
            "dependencies_digest": _dependencies_digest(manifest),
            "publisher_trust": publisher_trust,
        }
        review_fields = tuple(canonical_review)
        if any(review.get(field_name) != canonical_review[field_name] for field_name in review_fields):
            return False
        canonical_pointer = {
            "pack_id": manifest.id,
            "version": manifest.version,
            "digest": digest,
            "goal_id": pointer.get("goal_id"),
            "review_id": review_id,
            "authority_digest": manifest.authority_digest,
            "dependencies_digest": _dependencies_digest(manifest),
            "root_path": _safe_pack_path(root_path),
            "owner_principal_id": pointer_owner,
            "session_id": pointer_session,
        }
        return (
            all(pointer.get(field_name) == value for field_name, value in canonical_pointer.items())
            and not bool(record.get("revoked"))
        )

    @staticmethod
    def _require_pointer_identity(
        pointer: Mapping[str, Any],
        *,
        owner_principal_id: str | None,
        session_id: str | None,
    ) -> tuple[str, str]:
        """Require the exact authenticated owner/session bound at activation."""

        try:
            expected_owner = _validate_goal_id(str(pointer.get("owner_principal_id") or ""))
            expected_session = _validate_goal_id(str(pointer.get("session_id") or ""))
        except CapabilityPackLifecycleError as exc:
            raise CapabilityPackLifecycleError("pack lifecycle identity binding is unavailable") from exc
        if owner_principal_id is None or session_id is None:
            raise CapabilityPackLifecycleError("authenticated owner and session identity are required")
        supplied_owner = _validate_goal_id(owner_principal_id)
        supplied_session = _validate_goal_id(session_id)
        if supplied_owner != expected_owner or supplied_session != expected_session:
            raise CapabilityPackLifecycleError("pack lifecycle owner or session identity conflicts with the immutable binding")
        return expected_owner, expected_session

    def review(
        self,
        manifest: CapabilityPackManifest | Mapping[str, Any],
        *,
        root_path: str | Path,
        goal_id: str,
        reviewed_by: str = "operator",
        authority_expansion_approved: bool = False,
        available_dependencies: Mapping[str, Any] | Iterable[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Record local review for one immutable digest/version/goal binding."""
        pack = self._coerce_manifest(manifest)
        self._assert_compatible(pack)
        goal_id = _validate_goal_id(goal_id)
        reviewed_by = _validate_goal_id(reviewed_by)
        validation = validate_capability_pack_path(root_path, pack)
        if not validation["ok"]:
            raise CapabilityPackLifecycleError("; ".join(validation["errors"]))
        try:
            load_capability_pack_workflows(root_path, pack)
        except (CapabilityPackError, OSError, UnicodeDecodeError) as exc:
            raise CapabilityPackLifecycleError(str(exc)) from exc
        # External dependency records are an advisory preflight only.  The
        # durable state transaction below must independently contain each
        # reviewed dependency at the exact digest and version, so a caller
        # cannot make a phantom dependency look available.
        if available_dependencies is not None:
            dependency_errors = validate_capability_pack_dependencies(pack, available_dependencies)
            if dependency_errors:
                raise CapabilityPackLifecycleError("; ".join(dependency_errors))
        digest = capability_pack_digest(root_path)
        publisher_trust = publisher_trust_status(pack, package_root=root_path)
        if pack.signature.state == "integrity-checked" and not publisher_trust["integrity_checked"]:
            raise CapabilityPackLifecycleError(
                "integrity-checked signature digest does not match package content"
            )
        review_id = _review_digest(
            pack_id=pack.id,
            version=pack.version,
            digest=digest,
            goal_id=goal_id,
            authority_digest=pack.authority_digest,
        )
        review = {
            "review_id": review_id,
            "status": "approved",
            "pack_id": pack.id,
            "version": pack.version,
            "digest": digest,
            "goal_id": goal_id,
            "authority_digest": pack.authority_digest,
            "dependencies": _dependency_bindings(pack),
            "dependencies_digest": _dependencies_digest(pack),
            "reviewed_by": reviewed_by,
            "reviewed_at": _utc_now(),
            "authority_expansion_approved": bool(authority_expansion_approved),
            "publisher_trust": publisher_trust,
        }
        with self._state_lock():
            state = self._load()
            state_dependency_errors = validate_capability_pack_dependencies(
                pack,
                _dependency_records_from_state(state, pack),
            )
            if state_dependency_errors:
                raise CapabilityPackLifecycleError("; ".join(state_dependency_errors))
            state["reviews"][review_id] = review
            versions = state["versions"].setdefault(pack.id, {})
            existing_version = versions.get(digest)
            if isinstance(existing_version, Mapping):
                for field_name, expected in {
                    "version": pack.version,
                    "goal_id": goal_id,
                    "authority_digest": pack.authority_digest,
                    "dependencies_digest": _dependencies_digest(pack),
                }.items():
                    if existing_version.get(field_name) != expected:
                        raise CapabilityPackLifecycleError(
                            "digest is already bound to a different reviewed version, goal, or authority"
                        )
            versions.setdefault(digest, {
                "pack_id": pack.id,
                "version": pack.version,
                "digest": digest,
                "goal_id": goal_id,
                "authority_digest": pack.authority_digest,
                "authority": pack.authority.model_dump(mode="json"),
                "resources": pack.resources.model_dump(mode="json"),
                "data_policy": pack.data_policy.model_dump(mode="json"),
                "compatibility": pack.compatibility.model_dump(mode="json"),
                "dependencies": _dependency_bindings(pack),
                "dependencies_digest": _dependencies_digest(pack),
                "root_path": _safe_pack_path(root_path),
                "lifecycle": pack.lifecycle.model_dump(mode="json"),
                "review_id": review_id,
                "revoked": False,
            })
            versions[digest]["root_path"] = _safe_pack_path(root_path)
            receipt = self._record_receipt(state, action="review", status="approved", pack_id=pack.id, details={"version": pack.version, "digest": digest, "goal_id": goal_id, "review_id": review_id})
            self._commit(state)
        return {"review": deepcopy(review), "receipt": receipt}

    def _prepare_activation(
        self,
        state: dict[str, Any],
        pack: CapabilityPackManifest,
        *,
        root_path: str | Path,
        goal_id: str,
        review_id: str,
        approval_id: str | None,
        action: str,
        allow_replace: bool,
        owner_principal_id: str | None = None,
        session_id: str | None = None,
        content_digest: str | None = None,
        authority_digest: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        self._assert_compatible(pack)
        validation = validate_capability_pack_path(root_path, pack)
        if not validation["ok"]:
            raise CapabilityPackLifecycleError("; ".join(validation["errors"]))
        digest = capability_pack_digest(root_path)
        review = self._review_from_state(state, review_id)
        expected = {
            "pack_id": pack.id,
            "version": pack.version,
            "digest": digest,
            "goal_id": goal_id,
            "authority_digest": pack.authority_digest,
            "dependencies_digest": _dependencies_digest(pack),
        }
        for key, value in expected.items():
            if review.get(key) != value or review.get("status") != "approved":
                raise CapabilityPackLifecycleError(f"review binding mismatch for {key}")
        dependency_errors = validate_capability_pack_dependencies(
            pack,
            _dependency_records_from_state(state, pack),
        )
        if dependency_errors:
            raise CapabilityPackLifecycleError("; ".join(dependency_errors))
        revoked = state["revoked"].get(pack.id, [])
        if digest in revoked:
            raise CapabilityPackLifecycleError("reviewed pack digest is revoked")
        existing = state["active"].get(pack.id)
        previous_manifest = None
        previous_digest = None
        candidate_delta: dict[str, Any] = self._record_delta(None, pack)
        idempotent_existing = False
        if isinstance(existing, Mapping) and existing.get("status") in {"active", "paused"}:
            if not self._pointer_binding_valid(state, pack.id, existing):
                raise CapabilityPackLifecycleError("active pointer binding is invalid")
            previous_digest = str(existing.get("digest") or "") or None
            if existing.get("goal_id") != goal_id:
                raise CapabilityPackLifecycleError("active pack is bound to a different goal")
            if existing.get("digest") == digest and existing.get("version") == pack.version and existing.get("goal_id") == goal_id:
                idempotent_existing = True
            elif not allow_replace:
                raise CapabilityPackLifecycleError("a different version is active; use update or rollback")
            old_version = state["versions"].get(pack.id, {}).get(existing.get("digest"))
            if isinstance(old_version, Mapping):
                previous_manifest = old_version
            candidate_delta = self._record_delta(old_version, pack)
        approval = self._require_approval(
            state,
            approval_id=approval_id,
            action=action,
            pack_id=pack.id,
            version=pack.version,
            digest=digest,
            goal_id=goal_id,
            current_digest=previous_digest,
            authority_delta_payload=candidate_delta,
            owner_principal_id=owner_principal_id,
            session_id=session_id,
            content_digest=content_digest or digest,
            authority_digest=authority_digest or pack.authority_digest,
        )
        bound_owner = _validate_goal_id(str(owner_principal_id or approval.get("owner_principal_id") or ""))
        bound_session = _validate_goal_id(str(session_id or approval.get("session_id") or ""))
        if isinstance(existing, Mapping) and existing.get("status") in {"active", "paused"}:
            self._require_pointer_identity(existing, owner_principal_id=bound_owner, session_id=bound_session)
        if idempotent_existing:
            return dict(existing), {"digest": digest, "idempotent": True, "approval_id": approval_id}
        record = state["versions"].setdefault(pack.id, {}).setdefault(digest, {
            "pack_id": pack.id,
            "version": pack.version,
            "digest": digest,
            "goal_id": goal_id,
            "authority_digest": pack.authority_digest,
            "authority": pack.authority.model_dump(mode="json"),
            "resources": pack.resources.model_dump(mode="json"),
            "data_policy": pack.data_policy.model_dump(mode="json"),
            "compatibility": pack.compatibility.model_dump(mode="json"),
            "dependencies": _dependency_bindings(pack),
            "dependencies_digest": _dependencies_digest(pack),
            "root_path": _safe_pack_path(root_path),
            "lifecycle": pack.lifecycle.model_dump(mode="json"),
            "review_id": review_id,
            "revoked": False,
        })
        record["root_path"] = _safe_pack_path(root_path)
        pointer = {
            "pack_id": pack.id,
            "version": pack.version,
            "digest": digest,
            "goal_id": goal_id,
            "review_id": review_id,
            "authority_digest": pack.authority_digest,
            "owner_principal_id": bound_owner,
            "session_id": bound_session,
            "dependencies_digest": _dependencies_digest(pack),
            "status": "paused" if isinstance(existing, Mapping) and existing.get("status") == "paused" else "active",
            "previous_version": existing.get("version") if isinstance(existing, Mapping) else None,
            "previous_digest": existing.get("digest") if isinstance(existing, Mapping) else None,
            "root_path": record.get("root_path"),
        }
        return pointer, {"digest": digest, "previous": previous_manifest, "authority_delta": candidate_delta, "approval_id": approval_id}

    def activate(
        self,
        manifest: CapabilityPackManifest | Mapping[str, Any],
        *,
        root_path: str | Path,
        goal_id: str,
        review_id: str,
        approval_id: str | None = None,
        owner_principal_id: str | None = None,
        owner_id: str | None = None,
        session_id: str | None = None,
        operator_session_id: str | None = None,
        content_digest: str | None = None,
        approval_content_digest: str | None = None,
        authority_digest: str | None = None,
    ) -> dict[str, Any]:
        pack = self._coerce_manifest(manifest)
        goal_id = _validate_goal_id(goal_id)
        if content_digest is not None and approval_content_digest is not None and content_digest != approval_content_digest:
            raise CapabilityPackLifecycleError("activation content digest aliases disagree")
        with self._state_lock():
            state = self._load()
            pointer, details = self._prepare_activation(state, pack, root_path=root_path, goal_id=goal_id, review_id=review_id, approval_id=approval_id, action="activate", allow_replace=False, owner_principal_id=owner_principal_id or owner_id, session_id=session_id or operator_session_id, content_digest=content_digest or approval_content_digest, authority_digest=authority_digest)
            state["active"][pack.id] = pointer
            receipt = self._record_receipt(state, action="activate", status="active", pack_id=pack.id, details={**_public_pointer(pointer), "authority_delta": details.get("authority_delta", {}), "approval_id": approval_id})
            self._commit(state)
        return {"status": "active", "pointer": _public_pointer(pointer), "receipt": receipt}

    def update(
        self,
        manifest: CapabilityPackManifest | Mapping[str, Any],
        *,
        root_path: str | Path,
        goal_id: str,
        review_id: str,
        approval_id: str | None = None,
        owner_principal_id: str | None = None,
        owner_id: str | None = None,
        session_id: str | None = None,
        operator_session_id: str | None = None,
        content_digest: str | None = None,
        approval_content_digest: str | None = None,
        authority_digest: str | None = None,
    ) -> dict[str, Any]:
        pack = self._coerce_manifest(manifest)
        goal_id = _validate_goal_id(goal_id)
        if content_digest is not None and approval_content_digest is not None and content_digest != approval_content_digest:
            raise CapabilityPackLifecycleError("update content digest aliases disagree")
        with self._state_lock():
            state = self._load()
            pointer, details = self._prepare_activation(state, pack, root_path=root_path, goal_id=goal_id, review_id=review_id, approval_id=approval_id, action="update", allow_replace=True, owner_principal_id=owner_principal_id or owner_id, session_id=session_id or operator_session_id, content_digest=content_digest or approval_content_digest, authority_digest=authority_digest)
            state["active"][pack.id] = pointer
            receipt = self._record_receipt(state, action="update", status="active", pack_id=pack.id, details={**_public_pointer(pointer), "authority_delta": details.get("authority_delta", {}), "approval_id": approval_id})
            self._commit(state)
        return {"status": "active", "pointer": _public_pointer(pointer), "receipt": receipt}

    def _transition(
        self,
        pack_id: str,
        *,
        action: str,
        status: str,
        approval_id: str | None,
        reason: str = "",
        owner_principal_id: str | None = None,
        session_id: str | None = None,
        content_digest: str | None = None,
        authority_digest: str | None = None,
    ) -> dict[str, Any]:
        with self._state_lock():
            state = self._load()
            pointer = state["active"].get(pack_id)
            if not isinstance(pointer, Mapping):
                raise CapabilityPackLifecycleError(f"pack '{pack_id}' has no active-version pointer")
            current_status = pointer.get("status")
            if action == "pause" and current_status != "active":
                raise CapabilityPackLifecycleError("pause requires an active pack")
            if action == "uninstall" and current_status not in {"active", "paused", "revoked"}:
                raise CapabilityPackLifecycleError("uninstall requires an active, paused, or revoked pack")
            target_digest = str(pointer.get("digest") or "")
            target_version = str(pointer.get("version") or "")
            target_goal = str(pointer.get("goal_id") or "")
            approval = self._require_approval(
                state,
                approval_id=approval_id,
                action=action,
                pack_id=pack_id,
                version=target_version,
                digest=target_digest,
                goal_id=target_goal,
                current_digest=None,
                authority_delta_payload={},
                owner_principal_id=owner_principal_id,
                session_id=session_id,
                content_digest=content_digest or target_digest,
                authority_digest=authority_digest or str(
                    state["versions"].get(pack_id, {}).get(target_digest, {}).get("authority_digest") or ""
                ),
            )
            self._require_pointer_identity(
                pointer,
                owner_principal_id=owner_principal_id or str(approval.get("owner_principal_id") or ""),
                session_id=session_id or str(approval.get("session_id") or ""),
            )
            lifecycle = state["versions"].get(pack_id, {}).get(target_digest, {}).get("lifecycle", {})
            revoke_policy = str(lifecycle.get("revoke_running_jobs") or "cancel_at_safe_checkpoint") if isinstance(lifecycle, Mapping) else "cancel_at_safe_checkpoint"
            cancelled_jobs = self._cancel_pack_jobs(state, pack_id, digest=target_digest, reason=f"{action}_requested")
            next_pointer = dict(pointer)
            next_pointer["status"] = status
            details = {"version": pointer.get("version"), "digest": pointer.get("digest"), "goal_id": pointer.get("goal_id"), "reason_code": canonical_digest(reason or action)[:16], "approval_id": approval_id, "cancelled_jobs": cancelled_jobs, "revoke_running_jobs": revoke_policy}
            state["active"][pack_id] = next_pointer
            receipt = self._record_receipt(state, action=action, status=status, pack_id=pack_id, details=details)
            self._commit(state)
        return {"status": status, "pointer": _public_pointer(next_pointer), "receipt": receipt}

    def pause(
        self,
        pack_id: str,
        *,
        approval_id: str | None = None,
        reason: str = "operator_pause",
        owner_principal_id: str | None = None,
        owner_id: str | None = None,
        session_id: str | None = None,
        operator_session_id: str | None = None,
        content_digest: str | None = None,
        approval_content_digest: str | None = None,
        authority_digest: str | None = None,
    ) -> dict[str, Any]:
        pack_id = _validate_pack_id(pack_id)
        if content_digest is not None and approval_content_digest is not None and content_digest != approval_content_digest:
            raise CapabilityPackLifecycleError("pause content digest aliases disagree")
        return self._transition(pack_id, action="pause", status="paused", approval_id=approval_id, reason=reason, owner_principal_id=owner_principal_id or owner_id, session_id=session_id or operator_session_id, content_digest=content_digest or approval_content_digest, authority_digest=authority_digest)

    def revoke(
        self,
        pack_id: str,
        *,
        digest: str | None = None,
        approval_id: str | None = None,
        reason: str = "operator_revoke",
        owner_principal_id: str | None = None,
        owner_id: str | None = None,
        session_id: str | None = None,
        operator_session_id: str | None = None,
        content_digest: str | None = None,
        approval_content_digest: str | None = None,
        authority_digest: str | None = None,
    ) -> dict[str, Any]:
        pack_id = _validate_pack_id(pack_id)
        if content_digest is not None and approval_content_digest is not None and content_digest != approval_content_digest:
            raise CapabilityPackLifecycleError("revoke content digest aliases disagree")
        owner_principal_id = owner_principal_id or owner_id
        session_id = session_id or operator_session_id
        content_digest = content_digest or approval_content_digest
        with self._state_lock():
            state = self._load()
            pointer = state["active"].get(pack_id)
            target_digest = digest or (pointer.get("digest") if isinstance(pointer, Mapping) else None)
            if not isinstance(target_digest, str):
                raise CapabilityPackLifecycleError(f"pack '{pack_id}' has no digest to revoke")
            if not _DIGEST_RE.fullmatch(target_digest):
                raise CapabilityPackLifecycleError("revoke digest must be a lowercase SHA-256 digest")
            target_record = state["versions"].get(pack_id, {}).get(target_digest)
            target_version = str(target_record.get("version") or "") if isinstance(target_record, Mapping) else ""
            target_goal = str(target_record.get("goal_id") or "") if isinstance(target_record, Mapping) else ""
            approval = self._require_approval(
                state,
                approval_id=approval_id,
                action="revoke",
                pack_id=pack_id,
                version=target_version,
                digest=target_digest,
                goal_id=target_goal,
                current_digest=None,
                authority_delta_payload={},
                owner_principal_id=owner_principal_id,
                session_id=session_id,
                content_digest=content_digest or target_digest,
                authority_digest=authority_digest or str(target_record.get("authority_digest") or "") if isinstance(target_record, Mapping) else authority_digest,
            )
            revoked = state["revoked"].setdefault(pack_id, [])
            if target_digest not in revoked:
                revoked.append(target_digest)
            dependency_dependents = self._cascade_dependency_revocation(
                state,
                dependency_pack_id=pack_id,
                dependency_digest=target_digest,
            )
            next_pointer = dict(pointer) if isinstance(pointer, Mapping) and pointer.get("digest") == target_digest else None
            if next_pointer is not None:
                next_pointer["status"] = "revoked"
                state["active"][pack_id] = next_pointer
            lifecycle = target_record.get("lifecycle", {}) if isinstance(target_record, Mapping) else {}
            revoke_policy = str(lifecycle.get("revoke_running_jobs") or "cancel_at_safe_checkpoint") if isinstance(lifecycle, Mapping) else "cancel_at_safe_checkpoint"
            if isinstance(pointer, Mapping) and pointer.get("digest") == target_digest:
                self._require_pointer_identity(
                    pointer,
                    owner_principal_id=owner_principal_id or str(approval.get("owner_principal_id") or ""),
                    session_id=session_id or str(approval.get("session_id") or ""),
                )
            cancelled_jobs = (
                self._cancel_pack_jobs(state, pack_id, digest=target_digest, reason="revoke_requested")
                if revoke_policy == "cancel_at_safe_checkpoint"
                else self._cancel_pack_jobs(
                    state,
                    pack_id,
                    digest=target_digest,
                    reason="revoke_queued_job_requested",
                    statuses={"accepted", "queued", "paused"},
                )
            )
            pinned_jobs = sum(
                1
                for job in state.get("jobs", {}).values()
                if isinstance(job, Mapping)
                and job.get("pack_id") == pack_id
                and job.get("digest") == target_digest
                and job.get("status") == "running"
            )
            receipt = self._record_receipt(
                state,
                action="revoke",
                status="revoked",
                pack_id=pack_id,
                details={
                    "digest": target_digest,
                    "reason_code": canonical_digest(reason)[:16],
                    "approval_id": approval_id,
                    "cancelled_jobs": cancelled_jobs,
                    "pinned_running_jobs": pinned_jobs,
                    "revoke_running_jobs": revoke_policy,
                    "dependency_dependents": dependency_dependents,
                },
            )
            self._commit(state)
        return {"status": "revoked", "pointer": _public_pointer(next_pointer), "receipt": receipt}

    def uninstall(
        self,
        pack_id: str,
        *,
        approval_id: str | None = None,
        reason: str = "operator_uninstall",
        owner_principal_id: str | None = None,
        owner_id: str | None = None,
        session_id: str | None = None,
        operator_session_id: str | None = None,
        content_digest: str | None = None,
        approval_content_digest: str | None = None,
        authority_digest: str | None = None,
    ) -> dict[str, Any]:
        # Keep the pointer and all receipts as a tombstone.  Canonical goal and
        # outcome references remain readable even after bounded pack cleanup.
        pack_id = _validate_pack_id(pack_id)
        if content_digest is not None and approval_content_digest is not None and content_digest != approval_content_digest:
            raise CapabilityPackLifecycleError("uninstall content digest aliases disagree")
        return self._transition(pack_id, action="uninstall", status="uninstalled", approval_id=approval_id, reason=reason, owner_principal_id=owner_principal_id or owner_id, session_id=session_id or operator_session_id, content_digest=content_digest or approval_content_digest, authority_digest=authority_digest)

    def rollback(
        self,
        pack_id: str,
        *,
        goal_id: str | None = None,
        approval_id: str | None = None,
        owner_principal_id: str | None = None,
        owner_id: str | None = None,
        session_id: str | None = None,
        operator_session_id: str | None = None,
        content_digest: str | None = None,
        approval_content_digest: str | None = None,
        authority_digest: str | None = None,
    ) -> dict[str, Any]:
        pack_id = _validate_pack_id(pack_id)
        if content_digest is not None and approval_content_digest is not None and content_digest != approval_content_digest:
            raise CapabilityPackLifecycleError("rollback content digest aliases disagree")
        owner_principal_id = owner_principal_id or owner_id
        session_id = session_id or operator_session_id
        content_digest = content_digest or approval_content_digest
        with self._state_lock():
            state = self._load()
            pointer = state["active"].get(pack_id)
            if not isinstance(pointer, Mapping):
                raise CapabilityPackLifecycleError(f"pack '{pack_id}' has no active-version pointer")
            if pointer.get("status") not in {"active", "paused"}:
                raise CapabilityPackLifecycleError("rollback requires an active or paused pack")
            if not self._pointer_binding_valid(state, pack_id, pointer):
                raise CapabilityPackLifecycleError("active pointer binding is invalid")
            previous_digest = pointer.get("previous_digest")
            previous_version = pointer.get("previous_version")
            if (
                not isinstance(previous_digest, str)
                or not _DIGEST_RE.fullmatch(previous_digest)
                or not isinstance(previous_version, str)
            ):
                raise CapabilityPackLifecycleError("pack has no rollback version")
            if previous_digest in state["revoked"].get(pack_id, []):
                raise CapabilityPackLifecycleError("rollback target digest is revoked")
            record = state["versions"].get(pack_id, {}).get(previous_digest)
            if not isinstance(record, Mapping) or record.get("revoked"):
                raise CapabilityPackLifecycleError("rollback target is quarantined or unavailable")
            root_path = record.get("root_path")
            if not isinstance(root_path, str):
                raise CapabilityPackLifecycleError("rollback target package root is unavailable")
            canonical = self._canonical_package_binding(root_path)
            if canonical is None:
                raise CapabilityPackLifecycleError(
                    "rollback target package root, manifest, signature, or contributions are invalid"
                )
            target_manifest, target_digest, target_publisher_trust = canonical
            if target_manifest.id != pack_id or target_manifest.version != previous_version:
                raise CapabilityPackLifecycleError("rollback target manifest identity does not match the reviewed version")
            if target_digest != previous_digest:
                raise CapabilityPackLifecycleError("rollback target content digest does not match the reviewed digest")
            try:
                self._assert_compatible(target_manifest)
            except CapabilityPackLifecycleError as exc:
                raise CapabilityPackLifecycleError(f"rollback target is incompatible with this Seraph runtime: {exc}") from exc
            target_goal = _validate_goal_id(goal_id) if goal_id is not None else _validate_goal_id(str(pointer.get("goal_id") or ""))
            if record.get("goal_id") != target_goal:
                raise CapabilityPackLifecycleError("rollback target is bound to a different goal")
            review_id = str(record.get("review_id") or "")
            expected_review_id = _review_digest(
                pack_id=target_manifest.id,
                version=target_manifest.version,
                digest=target_digest,
                goal_id=target_goal,
                authority_digest=target_manifest.authority_digest,
            )
            canonical_record = {
                "pack_id": target_manifest.id,
                "version": target_manifest.version,
                "digest": target_digest,
                "goal_id": target_goal,
                "authority_digest": target_manifest.authority_digest,
                "authority": target_manifest.authority.model_dump(mode="json"),
                "resources": target_manifest.resources.model_dump(mode="json"),
                "data_policy": target_manifest.data_policy.model_dump(mode="json"),
                "compatibility": target_manifest.compatibility.model_dump(mode="json"),
                "dependencies": _dependency_bindings(target_manifest),
                "dependencies_digest": _dependencies_digest(target_manifest),
                "root_path": _safe_pack_path(root_path),
                "lifecycle": target_manifest.lifecycle.model_dump(mode="json"),
                "review_id": expected_review_id,
                "revoked": False,
            }
            if any(record.get(field_name) != expected for field_name, expected in canonical_record.items()):
                raise CapabilityPackLifecycleError("rollback target version binding is stale")
            review = self._review_from_state(state, review_id)
            canonical_review = {
                "review_id": expected_review_id,
                "status": "approved",
                "pack_id": target_manifest.id,
                "version": target_manifest.version,
                "digest": target_digest,
                "goal_id": target_goal,
                "authority_digest": target_manifest.authority_digest,
                "dependencies": _dependency_bindings(target_manifest),
                "dependencies_digest": _dependencies_digest(target_manifest),
                "publisher_trust": target_publisher_trust,
            }
            if any(review.get(field_name) != expected for field_name, expected in canonical_review.items()):
                raise CapabilityPackLifecycleError("rollback review binding is stale")
            current_record = state["versions"].get(pack_id, {}).get(pointer.get("digest"))
            rollback_delta = _authority_delta_from_payloads(
                current_record.get("authority") if isinstance(current_record, Mapping) else {},
                current_record.get("data_policy") if isinstance(current_record, Mapping) else {},
                target_manifest.authority.model_dump(mode="json"),
                target_manifest.data_policy.model_dump(mode="json"),
            )
            rollback_delta["authority_digest_before"] = current_record.get("authority_digest") if isinstance(current_record, Mapping) else None
            rollback_delta["authority_digest_after"] = target_manifest.authority_digest
            self._require_approval(
                state,
                approval_id=approval_id,
                action="rollback",
                pack_id=pack_id,
                version=previous_version,
                digest=previous_digest,
                goal_id=target_goal,
                current_digest=str(pointer.get("digest") or ""),
                authority_delta_payload=rollback_delta,
                owner_principal_id=owner_principal_id,
                session_id=session_id,
                content_digest=content_digest or previous_digest,
                authority_digest=authority_digest or target_manifest.authority_digest,
            )
            cancelled_jobs = self._cancel_pack_jobs(state, pack_id, digest=str(pointer.get("digest") or ""), reason="rollback_requested")
            next_pointer = dict(pointer)
            next_pointer.update({"version": target_manifest.version, "digest": target_digest, "goal_id": target_goal, "review_id": expected_review_id, "authority_digest": target_manifest.authority_digest, "dependencies_digest": _dependencies_digest(target_manifest), "status": "active", "previous_version": pointer.get("version"), "previous_digest": pointer.get("digest"), "root_path": _safe_pack_path(root_path)})
            state["active"][pack_id] = next_pointer
            receipt = self._record_receipt(state, action="rollback", status="active", pack_id=pack_id, details={"version": target_manifest.version, "digest": target_digest, "goal_id": target_goal, "authority_delta": rollback_delta, "approval_id": approval_id, "cancelled_jobs": cancelled_jobs})
            self._commit(state)
        return {"status": "active", "pointer": _public_pointer(next_pointer), "receipt": receipt}

    @staticmethod
    def _safe_local_artifact_path(path: str | Path, *, base_root: str | Path, max_bytes: int) -> Path:
        """Resolve one local artifact path without following symlinks."""

        relative = Path(path)
        if relative.is_absolute():
            raise CapabilityPackLifecycleError("local artifact path must be relative to the artifact root")
        if "\\" in str(path):
            raise CapabilityPackLifecycleError("local artifact path must use POSIX separators")
        if not relative.parts or any(part in {"..", ""} for part in relative.parts):
            raise CapabilityPackLifecycleError("local artifact path must stay within the artifact root")
        base = Path(base_root)
        # Check the original path before resolving it.  Resolving first makes a
        # symlinked parent indistinguishable from a normal directory.
        _safe_canary_artifact_root(base)
        base_resolved = base.resolve(strict=True)
        current = base
        for part in relative.parts[:-1]:
            current = current / part
            try:
                if current.is_symlink():
                    raise CapabilityPackLifecycleError("local artifact path cannot contain symlinked directories")
                if current.exists() and not current.is_dir():
                    raise CapabilityPackLifecycleError("local artifact parent must be a directory")
            except OSError as exc:
                raise CapabilityPackLifecycleError("local artifact path could not be inspected safely") from exc
        candidate = base / relative
        resolved = candidate.resolve(strict=False)
        try:
            resolved.relative_to(base_resolved)
        except ValueError as exc:
            raise CapabilityPackLifecycleError("local artifact path escapes the artifact root") from exc
        # Recheck after resolution to catch a symlink introduced between the
        # initial ancestor walk and the final write.
        parent = candidate.parent
        _safe_canary_artifact_root(parent)
        try:
            if candidate.is_symlink():
                raise CapabilityPackLifecycleError("local artifact path cannot be a symlink")
            if candidate.exists() and not candidate.is_file():
                raise CapabilityPackLifecycleError("local artifact path must be a regular file")
            if candidate.exists() and candidate.stat().st_size > max_bytes:
                raise CapabilityPackLifecycleError("local artifact already exceeds the reviewed size limit")
        except OSError as exc:
            raise CapabilityPackLifecycleError("local artifact path could not be inspected safely") from exc
        return candidate

    def _set_local_job_status(
        self,
        job_id: str,
        *,
        status: str,
        details: Mapping[str, Any] | None = None,
        receipt_action: str | None = None,
        expected_statuses: set[str] | frozenset[str] | None = None,
        pack_id: str | None = None,
        owner_principal_id: str | None = None,
        session_id: str | None = None,
        expected_digest: str | None = None,
    ) -> dict[str, Any]:
        if status not in {"running", "succeeded", "failed", "cancelled", "blocked", "recovered"}:
            raise CapabilityPackLifecycleError("unsupported local job terminal status")
        with self._state_lock():
            state = self._load()
            job = state["jobs"].get(job_id)
            if not isinstance(job, Mapping):
                raise CapabilityPackLifecycleError("local execution job is unavailable")
            if pack_id is not None and job.get("pack_id") != pack_id:
                raise CapabilityPackLifecycleError("local execution job pack binding is invalid")
            if owner_principal_id is not None and job.get("owner_principal_id") != owner_principal_id:
                raise CapabilityPackLifecycleError("local execution job owner binding is invalid")
            if session_id is not None and job.get("session_id") != session_id:
                raise CapabilityPackLifecycleError("local execution job session binding is invalid")
            if expected_digest is not None and job.get("digest") != expected_digest:
                raise CapabilityPackLifecycleError("local execution job digest binding is invalid")
            current_status = str(job.get("status") or "")
            if expected_statuses is not None and current_status not in expected_statuses:
                # A concurrent cancel/revoke/reconcile transition is the
                # durable winner. Never resurrect it with a stale worker
                # result or failure receipt.
                return {"job": deepcopy(dict(job)), "receipt": None, "transition_applied": False}
            mutable_job = dict(job)
            mutable_job["status"] = status
            mutable_job.update(deepcopy(dict(details or {})))
            if status in {"succeeded", "failed", "cancelled", "blocked", "recovered"}:
                mutable_job.setdefault("finished_at", _utc_now())
            state["jobs"][job_id] = mutable_job
            receipt = self._record_receipt(
                state,
                action=receipt_action or f"job:{status}",
                status=status,
                pack_id=str(mutable_job.get("pack_id") or ""),
                details={key: value for key, value in mutable_job.items() if key not in {"root_path"}},
            )
            self._commit(state)
            return {"job": deepcopy(mutable_job), "receipt": receipt}

    def reconcile(
        self,
        pack_id: str | None = None,
        *,
        owner_principal_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Reconcile interrupted local jobs before a new job can load a pack."""

        selected_pack = _validate_pack_id(pack_id) if pack_id is not None else None
        scope_key = selected_pack or "*"
        with self._state_lock():
            state = self._load()
            if selected_pack is not None:
                selected_pointer = state.get("active", {}).get(selected_pack)
                if isinstance(selected_pointer, Mapping) and (owner_principal_id is not None or session_id is not None):
                    self._require_pointer_identity(
                        selected_pointer,
                        owner_principal_id=owner_principal_id,
                        session_id=session_id,
                    )
            changes: list[dict[str, Any]] = []
            active_by_pack = state.get("active", {})
            for item_job_id, raw_job in list(state.get("jobs", {}).items()):
                if not isinstance(raw_job, Mapping):
                    continue
                job_pack_id = str(raw_job.get("pack_id") or "")
                if selected_pack is not None and job_pack_id != selected_pack:
                    continue
                job_status = str(raw_job.get("status") or "")
                if job_status == "succeeded" and item_job_id not in state.get("local_executions", {}):
                    reason = "succeeded_local_job_missing_execution_receipt"
                    next_status = "blocked"
                elif job_status in {"succeeded", "failed", "cancelled", "expired", "blocked", "recovered"}:
                    continue
                else:
                    pointer = active_by_pack.get(job_pack_id)
                    pointer_digest = pointer.get("digest") if isinstance(pointer, Mapping) else None
                    reason: str | None = None
                    next_status = "blocked"
                    if not isinstance(pointer, Mapping) or pointer.get("status") not in {"active", "paused", "revoked"}:
                        reason = "active_pointer_unavailable"
                        next_status = "cancelled"
                    elif pointer_digest != raw_job.get("digest"):
                        reason = "job_pack_digest_is_no_longer_active"
                        next_status = "cancelled"
                    elif raw_job.get("digest") in state.get("revoked", {}).get(job_pack_id, []):
                        # A leave-pinned row remains durable, but restart must
                        # fence it into explicit operator recovery rather than
                        # leaving a revoked job looking runnable.
                        if pointer.get("status") == "revoked" and raw_job.get("cancel_on_revoke") is False and job_status == "running":
                            reason = "revoked_running_job_requires_operator_recovery"
                            next_status = "blocked"
                        else:
                            reason = "job_pack_digest_revoked"
                            next_status = "cancelled"
                    elif job_status == "running":
                        # A process restart cannot safely infer whether the
                        # workflow wrote an effect before its final receipt.
                        reason = "interrupted_local_execution_requires_operator_recovery"
                    elif job_status in {"accepted", "queued"}:
                        # A restart cannot prove that an accepted row was
                        # never claimed by a worker.  Keep the job visible and
                        # require an explicit operator recovery decision rather
                        # than silently leaving it looking runnable.
                        reason = "interrupted_accepted_job_requires_operator_recovery"
                if reason is None:
                    continue
                mutable_job = dict(raw_job)
                mutable_job.update(
                    {
                        "status": next_status,
                        "reconciliation_required": next_status == "blocked",
                        "recovery_action": "inspect durable artifact/readback receipt before retry",
                        "reconciliation_reason": reason,
                        "reconciled_at": _utc_now(),
                    }
                )
                state["jobs"][item_job_id] = mutable_job
                changes.append({"job_id": item_job_id, "status": next_status, "reason": reason})
            current = state.get("reconciliation") if isinstance(state.get("reconciliation"), Mapping) else {}
            by_pack = current.get("by_pack") if isinstance(current.get("by_pack"), Mapping) else {}
            previous_scope = by_pack.get(scope_key) if isinstance(by_pack, Mapping) else None
            unresolved = any(
                isinstance(item, Mapping) and item.get("status") == "blocked"
                and (selected_pack is None or item.get("pack_id") == selected_pack)
                for item in state.get("jobs", {}).values()
            )
            scope_status = "blocked" if unresolved or any(item["status"] == "blocked" for item in changes) else "clean"
            if changes or not isinstance(previous_scope, Mapping) or previous_scope.get("status") != scope_status:
                scope_reconciliation = {
                    "scope": selected_pack,
                    "status": scope_status,
                    "updated_at": _utc_now(),
                    "changes": changes,
                }
                updated_by_pack = {str(key): deepcopy(value) for key, value in by_pack.items()}
                updated_by_pack[scope_key] = scope_reconciliation
                state["reconciliation"] = {
                    "scope": selected_pack,
                    "status": scope_status,
                    "updated_at": scope_reconciliation["updated_at"],
                    "changes": changes,
                    "by_pack": updated_by_pack,
                }
                if changes:
                    self._record_receipt(
                        state,
                        action="reconcile",
                        status=scope_status,
                        pack_id=selected_pack or "lifecycle",
                        details=scope_reconciliation,
                    )
                self._commit(state)
                return deepcopy(scope_reconciliation)
            if isinstance(previous_scope, Mapping):
                return deepcopy(dict(previous_scope))
            return {
                "scope": selected_pack,
                "status": scope_status,
                "updated_at": None,
                "changes": [],
            }

    def resolve_reconciliation(
        self,
        pack_id: str,
        *,
        job_id: str,
        action: str = "cancel",
        owner_principal_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        """Resolve one blocked restart job through an explicit operator action."""

        pack_id = _validate_pack_id(pack_id)
        job_id = _validate_goal_id(job_id)
        if action not in {"cancel", "recover"}:
            raise CapabilityPackLifecycleError("reconciliation action must be cancel or recover")
        with self._state_lock():
            state = self._load()
            pointer = state.get("active", {}).get(pack_id)
            if not isinstance(pointer, Mapping):
                raise CapabilityPackLifecycleError("reconciliation requires an active pack binding")
            self._require_pointer_identity(
                pointer,
                owner_principal_id=owner_principal_id,
                session_id=session_id,
            )
            job = state.get("jobs", {}).get(job_id)
            if not isinstance(job, Mapping) or job.get("pack_id") != pack_id or job.get("status") != "blocked":
                raise CapabilityPackLifecycleError("reconciliation job is not blocked for this pack")
            if job.get("owner_principal_id") != owner_principal_id or job.get("session_id") != session_id:
                raise CapabilityPackLifecycleError("reconciliation job identity conflicts with the immutable binding")
            mutable_job = dict(job)
            mutable_job.update(
                {
                    "status": "cancelled" if action == "cancel" else "recovered",
                    "reconciliation_required": False,
                    "recovery_action": f"operator_{action}",
                    "reconciliation_resolved_at": _utc_now(),
                }
            )
            state["jobs"][job_id] = mutable_job
            unresolved = any(
                isinstance(item, Mapping)
                and item.get("status") == "blocked"
                and item.get("pack_id") == pack_id
                for item in state.get("jobs", {}).values()
            )
            updated_at = _utc_now()
            resolution = {
                "scope": pack_id,
                "status": "blocked" if unresolved else "clean",
                "updated_at": updated_at,
                "changes": [],
                "resolved_job_id": job_id,
                "resolution_action": action,
            }
            current_reconciliation = state.get("reconciliation") if isinstance(state.get("reconciliation"), Mapping) else {}
            by_pack = current_reconciliation.get("by_pack") if isinstance(current_reconciliation.get("by_pack"), Mapping) else {}
            updated_by_pack = {str(key): deepcopy(value) for key, value in by_pack.items()}
            updated_by_pack[pack_id] = resolution
            state["reconciliation"] = {**resolution, "by_pack": updated_by_pack}
            receipt = self._record_receipt(
                state,
                action="reconcile:resolve",
                status=resolution["status"],
                pack_id=pack_id,
                details={
                    "job_id": job_id,
                    "action": action,
                    "owner_principal_id": owner_principal_id,
                    "session_id": session_id,
                },
            )
            self._commit(state)
            return {
                "status": state["reconciliation"]["status"],
                "job": deepcopy(mutable_job),
                "receipt": receipt,
                "reconciliation": deepcopy(resolution),
            }

    def execute_local(
        self,
        pack_id: str,
        *,
        goal_id: str,
        job_id: str,
        domain: str,
        artifact_path: str | Path | None = None,
        artifact_root: str | Path | None = None,
        owner_principal_id: str = "operator:local",
        session_id: str = "local-session",
        source_url: str | None = None,
        query: str | None = None,
        goal_snapshot: Mapping[str, Any] | str | None = None,
        source_payload: Any = None,
        source_payload_digest: str | None = None,
        intercepted_transport: Callable[..., Any] | None = None,
        transport: Callable[..., Any] | None = None,
    ) -> dict[str, Any]:
        """Execute one real local workflow through the internal governed host.

        The primary domain accepts either an already intercepted source value
        or the legacy test-only ``intercepted_transport`` seam; no HTTP client
        or provider path exists in this method.  The secondary domain consumes
        a caller-provided canonical goal snapshot.  Both paths traverse the
        reviewed declarative workflow through ``_CapabilityPackLocalWorkflowHost``,
        write and read back a bounded artifact, and persist a pinned
        job/outcome receipt.  There is deliberately no caller-supplied
        workflow callback.
        """

        pack_id = _validate_pack_id(pack_id)
        goal_id = _validate_goal_id(goal_id)
        job_id = _validate_goal_id(job_id)
        owner_principal_id = _validate_goal_id(owner_principal_id)
        session_id = _validate_goal_id(session_id)
        if source_payload is not None and source_payload_digest is None:
            source_payload_digest = canonical_digest(source_payload)
        if source_payload_digest is not None and not _DIGEST_RE.fullmatch(str(source_payload_digest)):
            raise CapabilityPackLifecycleError("source payload digest must be a SHA-256 digest")
        if source_payload is not None and source_payload_digest != canonical_digest(source_payload):
            raise CapabilityPackLifecycleError("source payload digest does not match the intercepted value")
        domain_aliases = {
            "research": "primary",
            "research_brief": "primary",
            "goal_snapshot": "secondary",
            "snapshot": "secondary",
        }
        domain = domain_aliases.get(str(domain).strip().lower(), str(domain).strip().lower())
        if domain not in {"primary", "secondary"}:
            raise CapabilityPackLifecycleError("local execution domain must be primary or secondary")
        if domain == "primary" and intercepted_transport is None:
            intercepted_transport = transport
        if domain == "primary" and source_payload is not None and intercepted_transport is not None:
            raise CapabilityPackLifecycleError(
                "provide source_payload or an intercepted transport, not both"
            )
        if domain == "primary" and source_payload is None and not callable(intercepted_transport):
            raise CapabilityPackLifecycleError("primary local execution requires an intercepted transport")
        if domain == "secondary" and goal_snapshot is None:
            raise CapabilityPackLifecycleError("secondary local execution requires a goal snapshot")
        canonical_goal_snapshot: dict[str, Any] | None = None
        if domain == "secondary":
            canonical_goal_snapshot = _validate_goal_snapshot_binding(
                goal_snapshot,
                goal_id=goal_id,
                owner_principal_id=owner_principal_id,
                session_id=session_id,
            )
        if source_url is not None:
            parsed_url = urlparse(str(source_url))
            if parsed_url.scheme not in {"http", "https", "local"} or (parsed_url.scheme in {"http", "https"} and not parsed_url.netloc):
                raise CapabilityPackLifecycleError("source URL must be a controlled local/intercepted reference")

        # Reconcile stale running work before admitting a fresh job.
        reconciliation = self.reconcile(
            pack_id,
            owner_principal_id=owner_principal_id,
            session_id=session_id,
        )
        if reconciliation.get("status") == "blocked":
            raise CapabilityPackLifecycleError("local execution is blocked until interrupted work is reconciled")
        with self._state_lock(shared=True):
            state = self._load()
            pointer = state["active"].get(pack_id)
            if not isinstance(pointer, Mapping) or pointer.get("status") != "active":
                raise CapabilityPackLifecycleError("local execution requires an active pack")
            if pointer.get("goal_id") != goal_id or not self._pointer_binding_valid(state, pack_id, pointer):
                raise CapabilityPackLifecycleError("local execution binding is invalid")
            self._require_pointer_identity(
                pointer,
                owner_principal_id=owner_principal_id,
                session_id=session_id,
            )
            record = state["versions"].get(pack_id, {}).get(pointer.get("digest"))
            if not isinstance(record, Mapping):
                raise CapabilityPackLifecycleError("local execution version is unavailable")
            max_artifact_bytes = int((record.get("resources") or {}).get("max_artifact_bytes", 0) or 0)
            if max_artifact_bytes <= 0:
                raise CapabilityPackLifecycleError("reviewed artifact limit is unavailable")
            root_path = str(record.get("root_path") or "")
            pinned_version = str(pointer.get("version") or "")
            pinned_digest = str(pointer.get("digest") or "")
            pinned_authority_digest = str(pointer.get("authority_digest") or "")
        loaded_workflows = load_capability_pack_workflows(root_path)
        workflow = next(
            (
                item for item in loaded_workflows
                if (domain == "primary" and item.name in {"web-brief-to-file", "research-brief", "local-research-brief"})
                or (domain == "secondary" and item.name in {"goal-snapshot-to-file", "goal-snapshot", "local-goal-snapshot"})
            ),
            None,
        )
        if workflow is None:
            # A capability-only pack can still use the two canonical local
            # workflows; the loader receipt makes the fallback explicit.
            workflow = CapabilityPackLoadedWorkflow(
                name="web-brief-to-file" if domain == "primary" else "goal-snapshot-to-file",
                file_path="builtin:seraph-local-workflow",
                step_tools=("web_search", "write_file") if domain == "primary" else ("get_goals", "write_file"),
                steps=(
                    CapabilityPackWorkflowStep(
                        step_id="source",
                        tool="web_search" if domain == "primary" else "get_goals",
                        arguments={},
                    ),
                    CapabilityPackWorkflowStep(
                        step_id="save",
                        tool="write_file",
                        arguments={},
                    ),
                ),
            )
        required_tools, required_filesystem = _local_scope_requirements(workflow)
        artifact_root_value = artifact_root or (self.state_path.parent / "capability-pack-artifacts")
        artifact_path_value = str(artifact_path or (Path("capability-pack") / f"{job_id}.md"))
        # Validate the requested artifact before durable admission so a path
        # rejection cannot leave an accepted job that can never be written.
        self._safe_local_artifact_path(
            artifact_path_value,
            base_root=artifact_root_value,
            max_bytes=max_artifact_bytes,
        )
        request = CapabilityPackLocalExecutionRequest(
            pack_id=pack_id,
            goal_id=goal_id,
            job_id=job_id,
            owner_principal_id=owner_principal_id,
            session_id=session_id,
            domain=domain,
            artifact_path=artifact_path_value,
            source_url=source_url,
            query=query,
            goal_snapshot=canonical_goal_snapshot if canonical_goal_snapshot is not None else goal_snapshot,
        )
        request_contract = {
            **request.as_dict(),
            "artifact_root": str(artifact_root_value),
            "source_payload_digest": source_payload_digest,
        }
        admission = self.register_job(
            pack_id,
            goal_id=goal_id,
            job_id=job_id,
            status="accepted",
            owner_principal_id=owner_principal_id,
            session_id=session_id,
            request_contract=request_contract,
            required_tools=required_tools,
            required_filesystem=required_filesystem,
        )
        pinned_job = admission["job"]
        if pinned_job.get("digest") != pinned_digest or pinned_job.get("version") != pinned_version:
            raise CapabilityPackLifecycleError("local job pin changed during admission")
        if admission.get("status") == "deduped":
            with self._state_lock(shared=True):
                existing_state = self._load()
                existing_execution = existing_state.get("local_executions", {}).get(job_id)
                existing_job = existing_state.get("jobs", {}).get(job_id)
            if isinstance(existing_execution, Mapping) and isinstance(existing_job, Mapping) and existing_job.get("status") == "succeeded":
                return {
                    "status": "deduped",
                    "request": request.as_dict(),
                    "execution": deepcopy(dict(existing_execution)),
                    "job": deepcopy(dict(existing_job)),
                    "receipt": None,
                }
            if isinstance(existing_job, Mapping) and existing_job.get("status") in {"cancelled", "blocked", "failed", "recovered"}:
                raise CapabilityPackLifecycleError("idempotent local job is already terminal; use a new job_id")
            if isinstance(existing_job, Mapping) and existing_job.get("status") in {"accepted", "queued", "running"}:
                return {
                    "status": "in_progress",
                    "request": request.as_dict(),
                    "execution": deepcopy(dict(existing_execution)) if isinstance(existing_execution, Mapping) else None,
                    "job": deepcopy(dict(existing_job)),
                    "receipt": None,
                }
        self._set_local_job_status(
            job_id,
            status="running",
            details={"execution_mode": "local_functional", "domain": domain, "workflow": workflow.as_dict()},
            receipt_action="local:running",
            expected_statuses={"accepted", "queued", "running"},
            pack_id=pack_id,
            owner_principal_id=owner_principal_id,
            session_id=session_id,
            expected_digest=pinned_digest,
        )
        try:
            resolved_source_payload: Any = source_payload
            if domain == "primary":
                if not source_url:
                    raise CapabilityPackLifecycleError("primary local execution requires a controlled source URL")
                if resolved_source_payload is None:
                    assert intercepted_transport is not None
                    try:
                        resolved_source_payload = intercepted_transport(source_url, query=query)
                    except TypeError:
                        resolved_source_payload = intercepted_transport(source_url)
                outcome = "local_research_brief_verified"
                source_refs = [f"intercepted:{canonical_digest(source_url, resolved_source_payload)}"]
            else:
                outcome = "local_goal_snapshot_verified"
                resolved_source_payload = canonical_goal_snapshot if canonical_goal_snapshot is not None else goal_snapshot
                source_refs = [f"goal-snapshot:{canonical_digest(goal_id, resolved_source_payload)}"]
            governed_workflow = _CapabilityPackLocalWorkflowHost().execute(
                workflow,
                request,
                source_payload=resolved_source_payload if domain == "primary" else None,
                goal_snapshot=resolved_source_payload if domain == "secondary" else None,
                max_artifact_bytes=max_artifact_bytes,
            )
            content = governed_workflow.content
            goal_marker = f"Goal: {goal_id}"
            if not any(line.strip() == goal_marker for line in content.splitlines()):
                raise CapabilityPackLifecycleError("local artifact is missing the canonical goal identity")
            if len(content.encode("utf-8")) > max_artifact_bytes:
                raise CapabilityPackLifecycleError("local artifact exceeds the reviewed pack artifact limit")
            data = content.encode("utf-8")
            # The authority recheck, artifact write/readback, and durable
            # success receipt share one exclusive lifecycle transaction.  A
            # concurrent revoke or replacement therefore linearizes either
            # before the effect (and blocks it) or after a fully recorded
            # effect; it cannot leave an unaccounted artifact behind.
            with self._state_lock():
                state = self._load()
                final_pointer = state["active"].get(pack_id)
                final_job = state["jobs"].get(job_id)
                allow_revoked_completion = (
                    isinstance(final_pointer, Mapping)
                    and final_pointer.get("digest") == pinned_digest
                    and final_pointer.get("status") == "revoked"
                    and not final_pointer.get("revoked_by_dependency")
                    and pinned_job.get("cancel_on_revoke") is False
                )
                if (
                    not isinstance(final_pointer, Mapping)
                    or (final_pointer.get("status") != "active" and not allow_revoked_completion)
                    or final_pointer.get("digest") != pinned_digest
                    or not isinstance(final_job, Mapping)
                    or final_job.get("status") != "running"
                    or (final_pointer.get("status") == "active" and not self._pointer_binding_valid(state, pack_id, final_pointer))
                    or (pinned_digest in state.get("revoked", {}).get(pack_id, []) and not allow_revoked_completion)
                ):
                    mutable_job = dict(final_job) if isinstance(final_job, Mapping) else {"job_id": job_id}
                    mutable_job.update(
                        {
                            "status": "cancelled",
                            "cancel_requested": True,
                            "cancel_reason": "pack_revoked_or_replaced_before_receipt_commit",
                            "finished_at": _utc_now(),
                        }
                    )
                    state["jobs"][job_id] = mutable_job
                    self._record_receipt(
                        state,
                        action="local:cancelled",
                        status="cancelled",
                        pack_id=pack_id,
                        details={"job_id": job_id, "reason": mutable_job["cancel_reason"]},
                    )
                    self._commit(state)
                    raise CapabilityPackLifecycleError(mutable_job["cancel_reason"])
                artifact = self._safe_local_artifact_path(
                    request.artifact_path,
                    base_root=artifact_root or (self.state_path.parent / "capability-pack-artifacts"),
                    max_bytes=max_artifact_bytes,
                )
                _write_canary_artifact(artifact, data)
                readback = artifact.read_bytes()
                if readback != data:
                    raise CapabilityPackLifecycleError("local artifact readback digest mismatch")
                if not any(line.strip() == goal_marker for line in readback.decode("utf-8", errors="strict").splitlines()):
                    raise CapabilityPackLifecycleError("local artifact readback is missing the canonical goal identity")
                content_digest = hashlib.sha256(data).hexdigest()
                execution = {
                    "schema_version": CAPABILITY_PACK_LOCAL_EXECUTION_SCHEMA,
                    "execution_mode": "local_functional",
                    "domain": domain,
                    "workflow": workflow.as_dict(),
                    "pack_id": pack_id,
                    "version": pinned_version,
                    "digest": pinned_digest,
                    "goal_id": goal_id,
                    "goal_revision": canonical_goal_snapshot.get("revision") if canonical_goal_snapshot else None,
                    "canonical_goal_identity": {
                        "goal_id": goal_id,
                        "revision": canonical_goal_snapshot.get("revision") if canonical_goal_snapshot else None,
                        "status": canonical_goal_snapshot.get("status") if canonical_goal_snapshot else None,
                        "owner_principal_id": owner_principal_id,
                        "session_id": session_id,
                        "source": canonical_goal_snapshot.get("canonical_source") if canonical_goal_snapshot else None,
                    },
                    "job_id": job_id,
                    "owner_principal_id": owner_principal_id,
                    "session_id": session_id,
                    "authority_digest": pinned_authority_digest,
                    "transport": "intercepted" if domain == "primary" else "none",
                    "provider_calls": 0,
                    "live_network_calls": 0,
                    "source_refs": source_refs,
                    "artifact": {
                        "path": str(artifact),
                        "bytes": len(data),
                        "digest": content_digest,
                        "readback_digest": hashlib.sha256(readback).hexdigest(),
                        "readback_ok": True,
                    },
                    "outcome": outcome,
                    "governed_workflow": governed_workflow.as_dict(),
                    "memory": {"status": "no_learning", "canonical_authority": "guardian_canonical_memory"},
                }
                mutable_job = dict(final_job)
                mutable_job.update(
                    {
                        "status": "succeeded",
                        "execution_mode": "local_functional",
                        "domain": domain,
                        "artifact_digest": content_digest,
                        "artifact_path": str(artifact),
                        "readback_ok": True,
                        "outcome": outcome,
                        "authority_digest": pinned_authority_digest,
                        "owner_principal_id": owner_principal_id,
                        "session_id": session_id,
                        "finished_at": _utc_now(),
                    }
                )
                state["jobs"][job_id] = mutable_job
                self._record_receipt(
                    state,
                    action=f"local:{domain}",
                    status="succeeded",
                    pack_id=pack_id,
                    details={key: value for key, value in mutable_job.items() if key not in {"root_path"}},
                )
                state["local_executions"][job_id] = execution
                local_receipt = self._record_receipt(
                    state,
                    action=f"local:execute:{domain}",
                    status="succeeded",
                    pack_id=pack_id,
                    details=execution,
                )
                self._commit(state)
            return {"status": "succeeded", "request": request.as_dict(), "execution": execution, "job": deepcopy(mutable_job), "receipt": local_receipt}
        except Exception as exc:
            reason = str(exc) or type(exc).__name__
            try:
                failed = self._set_local_job_status(
                    job_id,
                    status="failed",
                    details={"execution_mode": "local_functional", "domain": domain, "failure_reason": reason},
                    receipt_action=f"local:{domain}:failed",
                    expected_statuses={"accepted", "queued", "running"},
                    pack_id=pack_id,
                    owner_principal_id=owner_principal_id,
                    session_id=session_id,
                    expected_digest=pinned_digest,
                )
            except CapabilityPackLifecycleError:
                failed = {"job": {"job_id": job_id, "status": "blocked"}, "receipt": None}
            raise CapabilityPackLifecycleError(reason) from exc

    # Explicit aliases keep the operator surface discoverable while retaining
    # one implementation and one durable receipt path.
    def run_local(self, pack_id: str, **kwargs: Any) -> dict[str, Any]:
        return self.execute_local(pack_id, **kwargs)

    def execute_local_job(self, pack_id: str, **kwargs: Any) -> dict[str, Any]:
        return self.execute_local(pack_id, **kwargs)

    def status(
        self,
        pack_id: str,
        *,
        owner_principal_id: str | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        pack_id = _validate_pack_id(pack_id)
        reconciliation = self.reconcile(
            pack_id,
            owner_principal_id=owner_principal_id,
            session_id=session_id,
        )
        with self._state_lock(shared=True):
            state = self._load()
            pointer = state["active"].get(pack_id)
            if isinstance(pointer, Mapping) and (owner_principal_id is not None or session_id is not None):
                self._require_pointer_identity(
                    pointer,
                    owner_principal_id=owner_principal_id,
                    session_id=session_id,
                )
            versions = state["versions"].get(pack_id, {})
            active = _public_pointer(pointer)
            if (
                isinstance(pointer, Mapping)
                and pointer.get("status") != "revoked"
                and not self._pointer_binding_valid(state, pack_id, pointer)
            ):
                active = {
                    "pack_id": pack_id,
                    "status": "invalid",
                    "failure_reason": "active_pointer_binding_invalid",
                }
            return {
                "schema_version": CAPABILITY_PACK_LIFECYCLE_SCHEMA,
                "pack_id": pack_id,
                "active": active,
                "available_versions": [
                    {key: value for key, value in record.items() if key != "root_path"}
                    for record in versions.values()
                    if isinstance(record, Mapping)
                ],
                "revoked_digests": list(state["revoked"].get(pack_id, [])),
                "jobs": [deepcopy(job) for job in state["jobs"].values() if isinstance(job, Mapping) and job.get("pack_id") == pack_id],
                "local_executions": [
                    deepcopy(execution)
                    for execution in state.get("local_executions", {}).values()
                    if isinstance(execution, Mapping) and execution.get("pack_id") == pack_id
                ],
                "receipts": [deepcopy(item) for item in state["receipts"] if isinstance(item, Mapping) and item.get("pack_id") == pack_id],
                # Reconciliation is scoped per pack.  The top-level state
                # retains the latest scope for compatibility, while callers
                # asking for one pack must never inherit another pack's
                # blocked/clean result.
                "reconciliation": deepcopy(reconciliation),
                "generation": state.get("generation", 0),
            }

    def active_pointer(self, pack_id: str) -> dict[str, Any] | None:
        pack_id = _validate_pack_id(pack_id)
        status = self.status(pack_id)
        active = status.get("active")
        if not isinstance(active, Mapping) or active.get("status") not in {"active", "paused"}:
            return None
        return active

    def _run_canary(
        self,
        pack_id: str,
        *,
        goal_id: str,
        kind: str,
        artifact_root: str | Path | None,
        deterministic_fixture: bool,
    ) -> dict[str, Any]:
        pack_id = _validate_pack_id(pack_id)
        goal_id = _validate_goal_id(goal_id)
        if kind not in {"primary", "secondary"}:
            raise CapabilityPackLifecycleError("canary kind must be primary or secondary")
        with self._state_lock():
            state = self._load()
            pointer = state["active"].get(pack_id)
            if not isinstance(pointer, Mapping) or pointer.get("status") != "active":
                raise CapabilityPackLifecycleError("canary requires an active, unpaused pack")
            if not self._pointer_binding_valid(state, pack_id, pointer):
                raise CapabilityPackLifecycleError("active pointer review binding is invalid")
            if pointer.get("goal_id") != goal_id:
                raise CapabilityPackLifecycleError("canary goal does not match active pointer")
            record = state["versions"].get(pack_id, {}).get(pointer.get("digest"))
            if not isinstance(record, Mapping):
                raise CapabilityPackLifecycleError("active pack version record is unavailable")
            digest = str(pointer.get("digest"))
            if digest in state["revoked"].get(pack_id, []):
                raise CapabilityPackLifecycleError("canary target digest is revoked")
            authority = record.get("authority") if isinstance(record.get("authority"), Mapping) else {}
            data_policy = record.get("data_policy") if isinstance(record.get("data_policy"), Mapping) else {}
            resources = record.get("resources") if isinstance(record.get("resources"), Mapping) else {}
            needs_remote = kind == "primary"
            blocked_reason = None
            if needs_remote and (not authority.get("network") or not data_policy.get("egress") or int(resources.get("max_inference_cost_microusd", 0) or 0) <= 0):
                blocked_reason = "canary_source_network_inference_authority_missing"
            elif not deterministic_fixture:
                # No production adapter exists on this branch.  A fixture
                # receipt must never be presented as governed runtime proof.
                blocked_reason = CAPABILITY_PACK_RUNTIME_BLOCKED_REASON
            key = f"{pack_id}:{pointer.get('version')}:{digest}:{goal_id}:{kind}"
            attempt = int(state["canary_attempts"].get(key, 0) or 0) + 1
            state["canary_attempts"][key] = attempt
            canary_id = _stable_receipt_id(pack_id, pointer.get("version"), digest, goal_id, kind, attempt)
            execution_contract = None
            if not blocked_reason:
                execution_contract = self._execution_contract_from_state(
                    state,
                    pack_id=pack_id,
                    goal_id=goal_id,
                    job_id=f"pack-canary:{canary_id}",
                )
            receipt: dict[str, Any] = {
                "id": canary_id,
                "schema_version": CAPABILITY_PACK_CANARY_SCHEMA,
                "kind": kind,
                "attempt": attempt,
                "execution_mode": "deterministic_fixture" if deterministic_fixture else "production_blocked",
                "status": "blocked" if blocked_reason else "succeeded",
                "pack_id": pack_id,
                "version": pointer.get("version"),
                "digest": digest,
                "goal_id": goal_id,
                "job_id": f"pack-canary:{canary_id}",
                "artifact_id": f"artifact:{canary_id}",
                "runtime_route": CAPABILITY_PACK_ROUTE,
                "provider_calls": 0,
                "live_network_calls": 0,
                "memory": {"canonical_authority": "guardian_canonical_memory", "status": "no_learning", "receipt": "outcome_not_written_by_fixture"},
                "execution_contract": execution_contract.as_dict() if execution_contract else None,
                "failure_reason": blocked_reason,
            }
            if execution_contract is not None:
                artifact_root_path = _safe_canary_artifact_root(
                    artifact_root
                    if artifact_root is not None
                    else self.state_path.parent / "capability-pack-canary-artifacts"
                )
                artifact_path = artifact_root_path / f"{canary_id.replace(':', '_')}.json"
                result = {
                    "outcome": "deterministic_fixture_passed",
                    "sources": ["fixture://local"] if kind == "primary" else [],
                    "readback": True,
                }
                request = {
                    "canary_id": canary_id,
                    "pack_id": pack_id,
                    "version": pointer.get("version"),
                    "digest": digest,
                    "goal_id": goal_id,
                    "kind": kind,
                    "route": CAPABILITY_PACK_ROUTE,
                }
                content = json.dumps({"request": request, "execution_contract": execution_contract.as_dict(), "result": result}, sort_keys=True, separators=(",", ":")).encode("utf-8")
                if len(content) > execution_contract.max_artifact_bytes:
                    raise CapabilityPackLifecycleError("canary artifact exceeds the reviewed pack artifact limit")
                _write_canary_artifact(artifact_path, content)
                artifact_digest = hashlib.sha256(content).hexdigest()
                readback = artifact_path.read_bytes()
                receipt["artifact"] = {"bytes": len(content), "digest": artifact_digest, "readback_digest": hashlib.sha256(readback).hexdigest(), "readback_ok": readback == content}
                receipt["outcome"] = result["outcome"]
            state_receipt = self._record_receipt(state, action=f"canary:{kind}", status=receipt["status"], pack_id=pack_id, details=receipt)
            receipt["lifecycle_receipt_id"] = state_receipt["id"]
            self._commit(state)
            return receipt

    def run_canary(self, pack_id: str, *, goal_id: str, kind: str = "primary", artifact_root: str | Path | None = None) -> dict[str, Any]:
        """Production canary entry point; blocked until the governed adapter exists."""

        return self._run_canary(pack_id, goal_id=goal_id, kind=kind, artifact_root=artifact_root, deterministic_fixture=False)

    def run_deterministic_canary(self, pack_id: str, *, goal_id: str, kind: str = "primary", artifact_root: str | Path | None = None) -> dict[str, Any]:
        """Test-only local fixture; it never represents runtime execution proof."""

        return self._run_canary(pack_id, goal_id=goal_id, kind=kind, artifact_root=artifact_root, deterministic_fixture=True)

    def run_primary_canary(self, pack_id: str, *, goal_id: str, artifact_root: str | Path | None = None) -> dict[str, Any]:
        return self.run_canary(pack_id, goal_id=goal_id, kind="primary", artifact_root=artifact_root)

    def run_secondary_canary(self, pack_id: str, *, goal_id: str, artifact_root: str | Path | None = None) -> dict[str, Any]:
        return self.run_canary(pack_id, goal_id=goal_id, kind="secondary", artifact_root=artifact_root)

    def run_deterministic_primary_canary(self, pack_id: str, *, goal_id: str, artifact_root: str | Path | None = None) -> dict[str, Any]:
        return self.run_deterministic_canary(pack_id, goal_id=goal_id, kind="primary", artifact_root=artifact_root)

    def run_deterministic_secondary_canary(self, pack_id: str, *, goal_id: str, artifact_root: str | Path | None = None) -> dict[str, Any]:
        return self.run_deterministic_canary(pack_id, goal_id=goal_id, kind="secondary", artifact_root=artifact_root)


__all__ = [
    "ActiveVersionPointer",
    "ArchiveValidationResult",
    "CAPABILITY_PACK_CANARY_SCHEMA",
    "CAPABILITY_PACK_EXECUTION_SCHEMA",
    "CAPABILITY_PACK_LOCAL_EXECUTION_SCHEMA",
    "CAPABILITY_PACK_LIFECYCLE_SCHEMA",
    "CAPABILITY_PACK_ROUTE",
    "CAPABILITY_PACK_RUNTIME_BLOCKED_REASON",
    "CAPABILITY_PACK_SCHEMA_V1",
    "CapabilityPackExecutionContract",
    "CapabilityPackLocalExecutionRequest",
    "CapabilityPackLoadedWorkflow",
    "MAX_INFERENCE_COST_MICROUSD",
    "MAX_PACK_JOBS",
    "CAPABILITY_PACK_SCHEMA_VERSION",
    "CAPABILITY_PACK_SIGNATURE_ALGORITHM",
    "CapabilityPackError",
    "CapabilityPackLifecycle",
    "CapabilityPackLifecycleError",
    "CapabilityPackManifest",
    "CapabilityPackManifestError",
    "InferencePriority",
    "PackAuthority",
    "PackCompatibility",
    "PackContributions",
    "PackDataPolicy",
    "PackDependency",
    "PackLifecycle",
    "PackMigration",
    "PackPublisher",
    "PackResources",
    "PackSignature",
    "authority_delta",
    "canonical_digest",
    "capability_pack_digest",
    "load_capability_pack_workflows",
    "migrate_capability_pack_v1",
    "migrate_v1_to_v2",
    "parse_capability_pack_manifest",
    "publisher_trust_status",
    "validate_capability_pack_archive",
    "validate_capability_pack_package",
    "validate_capability_pack_path",
    "validate_capability_pack_dependencies",
]
