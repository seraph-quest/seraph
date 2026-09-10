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
from typing import Any, Callable, Iterable, Mapping
import zipfile

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

MAX_PACK_MEMBER_BYTES = 100 * 1024 * 1024
MAX_PACK_TOTAL_BYTES = 250 * 1024 * 1024
MAX_PACK_MEMBERS = 10_000
MAX_RUNTIME_SECONDS = 86_400
MAX_ARTIFACT_BYTES = 100 * 1024 * 1024
MAX_INFERENCE_COST_MICROUSD = 1_000_000_000

_PACK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/@+\-]{0,255}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


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
    "sudo",
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
    digest: str | None = None

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
    def _digest(cls, value: str | None) -> str | None:
        if value is not None and not _DIGEST_RE.fullmatch(value.lower()):
            raise ValueError("dependency digest must be a lowercase SHA-256 hex digest")
        return value.lower() if value else value


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
        if field_name == "tools" and any(
            item.rsplit(".", 1)[-1].lower() in _PRIVILEGED_TOOL_NAMES
            for item in normalized
        ):
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

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "archive": self.archive,
            "members": list(self.members),
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
                    if stat.S_ISLNK(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode) or stat.S_ISFIFO(mode):
                        errors.append(f"archive member is a link or special file: {name}")
                    if info.is_dir() and name in {"manifest.yaml", "manifest.yml"}:
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
                    if info.issym() or info.islnk() or info.isdev() or info.isfifo():
                        errors.append(f"archive member is a link or special file: {name}")
                    if info.isdir() and name in {"manifest.yaml", "manifest.yml"}:
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


def validate_capability_pack_path(
    package_root: str | Path,
    manifest: CapabilityPackManifest | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Validate a checked-out package and every declared contribution path."""
    root = Path(package_root)
    errors: list[str] = []
    if not root.exists() or not root.is_dir():
        return {"ok": False, "path": str(root), "errors": ["package root must be a directory"]}
    try:
        reject_symlink_entries(root)
    except ValueError as exc:
        errors.append(str(exc))
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


def capability_pack_digest(package_root: str | Path) -> str:
    """Hash package content while excluding mutable top-level signature text."""
    root = Path(package_root)
    if not root.is_dir():
        raise CapabilityPackError("package root must be a directory")
    reject_symlink_entries(root)
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


def publisher_trust_status(manifest: CapabilityPackManifest) -> dict[str, Any]:
    """Return explicit provenance status; local integrity is never publisher trust."""
    signature = manifest.signature
    if not isinstance(signature, PackSignature):
        signature = PackSignature.model_validate(signature)
    return {
        "publisher_verified": False,
        "trust": "local_review_required",
        "provenance": manifest.publisher.provenance,
        "signature_state": signature.state,
        "integrity_checked": signature.state in {"integrity-checked", "cryptographic-unavailable"},
        "reason": "signature and publisher label are provenance/integrity only; trusted publisher keys are unavailable",
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
    added: dict[str, Any] = {}
    for key in ("tools", "filesystem", "secrets"):
        values = sorted(set(new_authority.get(key, [])) - set(old_authority.get(key, [])))
        if values:
            added[key] = values
    if new_authority.get("network") and not old_authority.get("network"):
        added["network"] = True
    old_policy = old.data_policy.model_dump(mode="json") if old else {"egress": []}
    new_policy = new.data_policy.model_dump(mode="json")
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
        "authority_digest_before": old.authority_digest if old else None,
        "authority_digest_after": new.authority_digest,
    }


def _review_digest(*, pack_id: str, version: str, digest: str, goal_id: str, authority_digest: str) -> str:
    return canonical_digest("pack-review", pack_id, version, digest, goal_id, authority_digest)


@dataclass(frozen=True)
class ActiveVersionPointer:
    pack_id: str
    version: str
    digest: str
    goal_id: str
    review_id: str
    authority_digest: str
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


def _safe_canary_result(result: Mapping[str, Any]) -> dict[str, Any]:
    """Keep an optional deterministic runner from placing arbitrary data in receipts."""

    if not isinstance(result, Mapping):
        raise CapabilityPackLifecycleError("canary runner must return a mapping")
    outcome = str(result.get("outcome") or "deterministic_fixture_passed").strip()[:256]
    if not outcome:
        outcome = "deterministic_fixture_passed"
    if any(marker in outcome.lower() for marker in _SECRET_VALUE_MARKERS):
        outcome = "[redacted]"
    sources: list[str] = []
    raw_sources = result.get("sources", [])
    if isinstance(raw_sources, (list, tuple)):
        for item in raw_sources[:32]:
            if isinstance(item, str) and item.strip():
                normalized = item.strip()[:256]
                if any(marker in normalized.lower() for marker in _SECRET_VALUE_MARKERS):
                    continue
                sources.append(normalized)
    readback = result.get("readback")
    return {
        "outcome": outcome,
        "sources": sources,
        "readback": readback if isinstance(readback, bool) else True,
    }


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
    """Atomic reviewed-pointer lifecycle over the existing workspace state path."""

    _lock_guard = threading.Lock()
    _locks: dict[str, threading.RLock] = {}

    def __init__(self, state_path: str | Path | None = None):
        # Use the existing extension state location by default.  Tests and
        # isolated installers may still provide a dedicated state file.
        self.state_path = Path(state_path or extension_state_path())
        key = str(self.state_path.resolve())
        with self._lock_guard:
            self._lock = self._locks.setdefault(key, threading.RLock())

    def _empty_state(self) -> dict[str, Any]:
        return {
            "schema_version": CAPABILITY_PACK_LIFECYCLE_SCHEMA,
            "generation": 0,
            "active": {},
            "versions": {},
            "reviews": {},
            "revoked": {},
            "receipts": [],
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
        for key in ("active", "versions", "reviews", "revoked"):
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

    def _commit(self, state: dict[str, Any]) -> None:
        state["generation"] = int(state.get("generation") or 0) + 1
        self._atomic_save(state)

    @staticmethod
    def _coerce_manifest(manifest: CapabilityPackManifest | Mapping[str, Any]) -> CapabilityPackManifest:
        return manifest if isinstance(manifest, CapabilityPackManifest) else parse_capability_pack_manifest(manifest)

    @staticmethod
    def _review_from_state(state: Mapping[str, Any], review_id: str) -> Mapping[str, Any]:
        reviews = state.get("reviews")
        review = reviews.get(review_id) if isinstance(reviews, Mapping) else None
        if not isinstance(review, Mapping):
            raise CapabilityPackLifecycleError("exact reviewed pack binding is required")
        return review

    @staticmethod
    def _pointer_binding_valid(
        state: Mapping[str, Any],
        pack_id: str,
        pointer: Mapping[str, Any],
    ) -> bool:
        versions = state.get("versions")
        records = versions.get(pack_id) if isinstance(versions, Mapping) else None
        record = records.get(pointer.get("digest")) if isinstance(records, Mapping) else None
        reviews = state.get("reviews")
        review = reviews.get(pointer.get("review_id")) if isinstance(reviews, Mapping) else None
        if not isinstance(record, Mapping) or not isinstance(review, Mapping):
            return False
        fields = ("pack_id", "version", "digest", "goal_id", "authority_digest", "review_id")
        bound = all(
            pointer.get(field_name) == record.get(field_name) == review.get(field_name)
            for field_name in fields
        ) and not bool(record.get("revoked"))
        if not bound:
            return False
        root_path = record.get("root_path")
        if not isinstance(root_path, str):
            return False
        try:
            return capability_pack_digest(root_path) == pointer.get("digest")
        except (CapabilityPackError, OSError, ValueError):
            return False

    def review(
        self,
        manifest: CapabilityPackManifest | Mapping[str, Any],
        *,
        root_path: str | Path,
        goal_id: str,
        reviewed_by: str = "operator",
        authority_expansion_approved: bool = False,
    ) -> dict[str, Any]:
        """Record local review for one immutable digest/version/goal binding."""
        pack = self._coerce_manifest(manifest)
        goal_id = _validate_goal_id(goal_id)
        reviewed_by = _validate_goal_id(reviewed_by)
        validation = validate_capability_pack_path(root_path, pack)
        if not validation["ok"]:
            raise CapabilityPackLifecycleError("; ".join(validation["errors"]))
        digest = capability_pack_digest(root_path)
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
            "reviewed_by": reviewed_by,
            "reviewed_at": _utc_now(),
            "authority_expansion_approved": bool(authority_expansion_approved),
            "publisher_trust": publisher_trust_status(pack),
        }
        with self._lock:
            state = self._load()
            state["reviews"][review_id] = review
            versions = state["versions"].setdefault(pack.id, {})
            existing_version = versions.get(digest)
            if isinstance(existing_version, Mapping):
                for field_name, expected in {
                    "version": pack.version,
                    "goal_id": goal_id,
                    "authority_digest": pack.authority_digest,
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
                "root_path": _safe_pack_path(root_path),
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
        approval_granted: bool,
        action: str,
        allow_replace: bool,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
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
        }
        for key, value in expected.items():
            if review.get(key) != value or review.get("status") != "approved":
                raise CapabilityPackLifecycleError(f"review binding mismatch for {key}")
        revoked = state["revoked"].get(pack.id, [])
        if digest in revoked:
            raise CapabilityPackLifecycleError("reviewed pack digest is revoked")
        existing = state["active"].get(pack.id)
        previous_manifest = None
        if isinstance(existing, Mapping) and existing.get("status") in {"active", "paused"}:
            if existing.get("goal_id") != goal_id:
                raise CapabilityPackLifecycleError("active pack is bound to a different goal")
            if existing.get("digest") == digest and existing.get("version") == pack.version and existing.get("goal_id") == goal_id:
                return dict(existing), {"digest": digest, "idempotent": True}
            if not allow_replace:
                raise CapabilityPackLifecycleError("a different version is active; use update or rollback")
            old_version = state["versions"].get(pack.id, {}).get(existing.get("digest"))
            if isinstance(old_version, Mapping):
                previous_manifest = old_version
            old_authority = {
                "tools": old_version.get("authority", {}).get("tools", []) if isinstance(old_version, Mapping) else [],
                "filesystem": old_version.get("authority", {}).get("filesystem", []) if isinstance(old_version, Mapping) else [],
                "network": old_version.get("authority", {}).get("network", False) if isinstance(old_version, Mapping) else False,
                "secrets": old_version.get("authority", {}).get("secrets", []) if isinstance(old_version, Mapping) else [],
            }
            old_policy = old_version.get("data_policy", {"egress": []}) if isinstance(old_version, Mapping) else {"egress": []}
            added = {}
            new_authority = pack.authority.model_dump(mode="json")
            for key in ("tools", "filesystem", "secrets"):
                values = sorted(set(new_authority.get(key, [])) - set(old_authority.get(key, [])))
                if values:
                    added[key] = values
            if new_authority.get("network") and not old_authority.get("network"):
                added["network"] = True
            egress = sorted(set(pack.data_policy.egress) - set(old_policy.get("egress", [])))
            if egress:
                added["egress"] = egress
            if added and not (approval_granted or bool(review.get("authority_expansion_approved"))):
                raise CapabilityPackLifecycleError("authority or egress expansion requires exact operator approval")
        record = state["versions"].setdefault(pack.id, {}).setdefault(digest, {
            "pack_id": pack.id,
            "version": pack.version,
            "digest": digest,
            "goal_id": goal_id,
            "authority_digest": pack.authority_digest,
            "authority": pack.authority.model_dump(mode="json"),
            "resources": pack.resources.model_dump(mode="json"),
            "data_policy": pack.data_policy.model_dump(mode="json"),
            "root_path": _safe_pack_path(root_path),
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
            "status": "active",
            "previous_version": existing.get("version") if isinstance(existing, Mapping) else None,
            "previous_digest": existing.get("digest") if isinstance(existing, Mapping) else None,
            "root_path": record.get("root_path"),
        }
        return pointer, {"digest": digest, "previous": previous_manifest}

    def activate(
        self,
        manifest: CapabilityPackManifest | Mapping[str, Any],
        *,
        root_path: str | Path,
        goal_id: str,
        review_id: str,
        approval_granted: bool = False,
    ) -> dict[str, Any]:
        pack = self._coerce_manifest(manifest)
        goal_id = _validate_goal_id(goal_id)
        with self._lock:
            state = self._load()
            pointer, details = self._prepare_activation(state, pack, root_path=root_path, goal_id=goal_id, review_id=review_id, approval_granted=approval_granted, action="activate", allow_replace=False)
            state["active"][pack.id] = pointer
            receipt = self._record_receipt(state, action="activate", status="active", pack_id=pack.id, details=_public_pointer(pointer))
            self._commit(state)
        return {"status": "active", "pointer": _public_pointer(pointer), "receipt": receipt}

    def update(
        self,
        manifest: CapabilityPackManifest | Mapping[str, Any],
        *,
        root_path: str | Path,
        goal_id: str,
        review_id: str,
        approval_granted: bool = False,
    ) -> dict[str, Any]:
        pack = self._coerce_manifest(manifest)
        goal_id = _validate_goal_id(goal_id)
        with self._lock:
            state = self._load()
            pointer, _ = self._prepare_activation(state, pack, root_path=root_path, goal_id=goal_id, review_id=review_id, approval_granted=approval_granted, action="update", allow_replace=True)
            state["active"][pack.id] = pointer
            receipt = self._record_receipt(state, action="update", status="active", pack_id=pack.id, details=_public_pointer(pointer))
            self._commit(state)
        return {"status": "active", "pointer": _public_pointer(pointer), "receipt": receipt}

    def _transition(self, pack_id: str, *, action: str, status: str, reason: str = "") -> dict[str, Any]:
        with self._lock:
            state = self._load()
            pointer = state["active"].get(pack_id)
            if not isinstance(pointer, Mapping):
                raise CapabilityPackLifecycleError(f"pack '{pack_id}' has no active-version pointer")
            current_status = pointer.get("status")
            if action == "pause" and current_status != "active":
                raise CapabilityPackLifecycleError("pause requires an active pack")
            if action == "uninstall" and current_status not in {"active", "paused"}:
                raise CapabilityPackLifecycleError("uninstall requires an active or paused pack")
            next_pointer = dict(pointer)
            next_pointer["status"] = status
            details = {"version": pointer.get("version"), "digest": pointer.get("digest"), "goal_id": pointer.get("goal_id"), "reason_code": canonical_digest(reason or action)[:16]}
            state["active"][pack_id] = next_pointer
            receipt = self._record_receipt(state, action=action, status=status, pack_id=pack_id, details=details)
            self._commit(state)
        return {"status": status, "pointer": _public_pointer(next_pointer), "receipt": receipt}

    def pause(self, pack_id: str, *, reason: str = "operator_pause") -> dict[str, Any]:
        pack_id = _validate_pack_id(pack_id)
        return self._transition(pack_id, action="pause", status="paused", reason=reason)

    def revoke(self, pack_id: str, *, digest: str | None = None, reason: str = "operator_revoke") -> dict[str, Any]:
        pack_id = _validate_pack_id(pack_id)
        with self._lock:
            state = self._load()
            pointer = state["active"].get(pack_id)
            target_digest = digest or (pointer.get("digest") if isinstance(pointer, Mapping) else None)
            if not isinstance(target_digest, str):
                raise CapabilityPackLifecycleError(f"pack '{pack_id}' has no digest to revoke")
            if not _DIGEST_RE.fullmatch(target_digest):
                raise CapabilityPackLifecycleError("revoke digest must be a lowercase SHA-256 digest")
            revoked = state["revoked"].setdefault(pack_id, [])
            if target_digest not in revoked:
                revoked.append(target_digest)
            next_pointer = dict(pointer) if isinstance(pointer, Mapping) and pointer.get("digest") == target_digest else None
            if next_pointer is not None:
                next_pointer["status"] = "revoked"
                state["active"][pack_id] = next_pointer
            receipt = self._record_receipt(state, action="revoke", status="revoked", pack_id=pack_id, details={"digest": target_digest, "reason_code": canonical_digest(reason)[:16]})
            self._commit(state)
        return {"status": "revoked", "pointer": _public_pointer(next_pointer), "receipt": receipt}

    def uninstall(self, pack_id: str, *, reason: str = "operator_uninstall") -> dict[str, Any]:
        # Keep the pointer and all receipts as a tombstone.  Canonical goal and
        # outcome references remain readable even after bounded pack cleanup.
        pack_id = _validate_pack_id(pack_id)
        return self._transition(pack_id, action="uninstall", status="uninstalled", reason=reason)

    def rollback(self, pack_id: str, *, goal_id: str | None = None) -> dict[str, Any]:
        pack_id = _validate_pack_id(pack_id)
        with self._lock:
            state = self._load()
            pointer = state["active"].get(pack_id)
            if not isinstance(pointer, Mapping):
                raise CapabilityPackLifecycleError(f"pack '{pack_id}' has no active-version pointer")
            if pointer.get("status") not in {"active", "paused"}:
                raise CapabilityPackLifecycleError("rollback requires an active or paused pack")
            previous_digest = pointer.get("previous_digest")
            previous_version = pointer.get("previous_version")
            if not isinstance(previous_digest, str) or not isinstance(previous_version, str):
                raise CapabilityPackLifecycleError("pack has no rollback version")
            if previous_digest in state["revoked"].get(pack_id, []):
                raise CapabilityPackLifecycleError("rollback target digest is revoked")
            record = state["versions"].get(pack_id, {}).get(previous_digest)
            if not isinstance(record, Mapping) or record.get("revoked"):
                raise CapabilityPackLifecycleError("rollback target is quarantined or unavailable")
            target_goal = str(goal_id or pointer.get("goal_id") or "")
            if record.get("goal_id") != target_goal:
                raise CapabilityPackLifecycleError("rollback target is bound to a different goal")
            review_id = str(record.get("review_id") or "")
            review = self._review_from_state(state, review_id)
            if review.get("digest") != previous_digest or review.get("version") != previous_version or review.get("goal_id") != target_goal:
                raise CapabilityPackLifecycleError("rollback review binding is stale")
            next_pointer = dict(pointer)
            next_pointer.update({"version": previous_version, "digest": previous_digest, "goal_id": target_goal, "review_id": review_id, "authority_digest": record.get("authority_digest"), "status": "active", "previous_version": pointer.get("version"), "previous_digest": pointer.get("digest"), "root_path": record.get("root_path")})
            state["active"][pack_id] = next_pointer
            receipt = self._record_receipt(state, action="rollback", status="active", pack_id=pack_id, details={"version": previous_version, "digest": previous_digest, "goal_id": target_goal})
            self._commit(state)
        return {"status": "active", "pointer": _public_pointer(next_pointer), "receipt": receipt}

    def status(self, pack_id: str) -> dict[str, Any]:
        pack_id = _validate_pack_id(pack_id)
        with self._lock:
            state = self._load()
            pointer = state["active"].get(pack_id)
            versions = state["versions"].get(pack_id, {})
            active = _public_pointer(pointer)
            if isinstance(pointer, Mapping) and not self._pointer_binding_valid(state, pack_id, pointer):
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
                "receipts": [deepcopy(item) for item in state["receipts"] if isinstance(item, Mapping) and item.get("pack_id") == pack_id],
                "generation": state.get("generation", 0),
            }

    def active_pointer(self, pack_id: str) -> dict[str, Any] | None:
        pack_id = _validate_pack_id(pack_id)
        status = self.status(pack_id)
        active = status.get("active")
        if not isinstance(active, Mapping) or active.get("status") not in {"active", "paused"}:
            return None
        return active

    def run_canary(
        self,
        pack_id: str,
        *,
        goal_id: str,
        kind: str = "primary",
        artifact_root: str | Path | None = None,
        runner: Callable[[dict[str, Any]], Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Run a deterministic local canary fixture and persist its receipt.

        ``runner`` is an optional deterministic test seam.  No default or
        supplied runner is allowed to be treated as a provider transport; the
        caller owns any additional proof and the receipt always declares zero
        live-provider calls.
        """
        pack_id = _validate_pack_id(pack_id)
        goal_id = _validate_goal_id(goal_id)
        if kind not in {"primary", "secondary"}:
            raise CapabilityPackLifecycleError("canary kind must be primary or secondary")
        with self._lock:
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
            # Primary research needs source/network/inference authority.  The
            # intentionally zero-budget example therefore produces a durable
            # blocked receipt rather than accidentally spending or egressing.
            needs_remote = kind == "primary"
            authority = record.get("authority") if isinstance(record.get("authority"), Mapping) else {}
            data_policy = record.get("data_policy") if isinstance(record.get("data_policy"), Mapping) else {}
            blocked_reason = None
            if needs_remote and (not authority.get("network") or not data_policy.get("egress") or int(record.get("resources", {}).get("max_inference_cost_microusd", 0) or 0) <= 0):
                # Older state records may not carry resources.  The pointer is
                # still safe; this conservative check blocks such canaries.
                blocked_reason = "canary_source_network_inference_authority_missing"
            canary_id = _stable_receipt_id(pack_id, pointer.get("version"), digest, goal_id, kind)
            artifact_root_path = _safe_canary_artifact_root(
                artifact_root
                if artifact_root is not None
                else self.state_path.parent / "capability-pack-canary-artifacts"
            )
            artifact_path = artifact_root_path / f"{canary_id.replace(':', '_')}.json"
            receipt: dict[str, Any] = {
                "id": canary_id,
                "schema_version": CAPABILITY_PACK_CANARY_SCHEMA,
                "kind": kind,
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
                "failure_reason": blocked_reason,
            }
            if not blocked_reason:
                request = {"canary_id": canary_id, "pack_id": pack_id, "version": pointer.get("version"), "digest": digest, "goal_id": goal_id, "kind": kind, "route": CAPABILITY_PACK_ROUTE}
                result = _safe_canary_result(runner(request)) if runner is not None else {"outcome": "deterministic_fixture_passed", "sources": ["fixture://local"] if kind == "primary" else [], "readback": True}
                content = json.dumps({"request": request, "result": result}, sort_keys=True, separators=(",", ":")).encode("utf-8")
                if len(content) > MAX_ARTIFACT_BYTES:
                    raise CapabilityPackLifecycleError("canary artifact exceeds pack limit")
                _write_canary_artifact(artifact_path, content)
                artifact_digest = hashlib.sha256(content).hexdigest()
                readback = artifact_path.read_bytes()
                receipt["artifact"] = {"bytes": len(content), "digest": artifact_digest, "readback_digest": hashlib.sha256(readback).hexdigest(), "readback_ok": readback == content}
                receipt["outcome"] = str(result.get("outcome") or "deterministic_fixture_passed")
            with self._lock:
                state = self._load()
                state_receipt = self._record_receipt(state, action=f"canary:{kind}", status=receipt["status"], pack_id=pack_id, details=receipt)
                receipt["lifecycle_receipt_id"] = state_receipt["id"]
                self._commit(state)
            return receipt

    def run_primary_canary(self, pack_id: str, *, goal_id: str, artifact_root: str | Path | None = None, runner: Callable[[dict[str, Any]], Mapping[str, Any]] | None = None) -> dict[str, Any]:
        return self.run_canary(pack_id, goal_id=goal_id, kind="primary", artifact_root=artifact_root, runner=runner)

    def run_secondary_canary(self, pack_id: str, *, goal_id: str, artifact_root: str | Path | None = None, runner: Callable[[dict[str, Any]], Mapping[str, Any]] | None = None) -> dict[str, Any]:
        return self.run_canary(pack_id, goal_id=goal_id, kind="secondary", artifact_root=artifact_root, runner=runner)


__all__ = [
    "ActiveVersionPointer",
    "ArchiveValidationResult",
    "CAPABILITY_PACK_CANARY_SCHEMA",
    "CAPABILITY_PACK_LIFECYCLE_SCHEMA",
    "CAPABILITY_PACK_ROUTE",
    "CAPABILITY_PACK_SCHEMA_V1",
    "MAX_INFERENCE_COST_MICROUSD",
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
    "migrate_capability_pack_v1",
    "migrate_v1_to_v2",
    "parse_capability_pack_manifest",
    "publisher_trust_status",
    "validate_capability_pack_archive",
    "validate_capability_pack_package",
    "validate_capability_pack_path",
]
