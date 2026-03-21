"""Canonical manifest schema for Seraph extension packages."""

from __future__ import annotations

from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

_CONTRIBUTION_FIELDS = (
    "skills",
    "workflows",
    "runbooks",
    "starter_packs",
    "provider_presets",
    "prompt_packs",
    "scheduled_routines",
    "mcp_servers",
    "managed_connectors",
    "observer_definitions",
    "observer_connectors",
    "channel_adapters",
    "workspace_adapters",
)

_CONNECTOR_FIELDS = {
    "mcp_servers",
    "managed_connectors",
    "observer_connectors",
    "channel_adapters",
    "workspace_adapters",
}


class ExtensionKind(str, Enum):
    CAPABILITY_PACK = "capability-pack"
    CONNECTOR_PACK = "connector-pack"


class ExtensionTrust(str, Enum):
    BUNDLED = "bundled"
    LOCAL = "local"
    VERIFIED = "verified"


class ExtensionManifestError(ValueError):
    """Raised when a manifest cannot be parsed or validated."""

    def __init__(self, source: str, message: str, *, errors: list[dict[str, Any]] | None = None):
        super().__init__(f"{source}: {message}")
        self.source = source
        self.message = message
        self.errors = errors or []


def _normalize_relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value.strip():
        raise ValueError("must not be empty")
    if path.is_absolute():
        raise ValueError("must be relative")
    if ".." in path.parts:
        raise ValueError("must not traverse outside the package")
    if path.parts and path.parts[0] == ".":
        raise ValueError("must not start with './'")
    normalized = path.as_posix()
    if normalized in {"", "."}:
        raise ValueError("must point to a file inside the package")
    if path.suffix == "":
        raise ValueError("must reference a file, not a directory")
    return normalized


class ExtensionPublisher(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    homepage: str | None = None
    support: str | None = None

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value


class ExtensionCompatibility(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seraph: str

    @field_validator("seraph")
    @classmethod
    def _validate_seraph_specifier(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        try:
            SpecifierSet(value)
        except InvalidSpecifier as exc:
            raise ValueError(f"invalid specifier: {value}") from exc
        return value

    def is_compatible_with(self, version: str) -> bool:
        try:
            parsed_version = Version(version)
        except InvalidVersion as exc:
            raise ValueError(f"invalid Seraph version: {version}") from exc
        return parsed_version in SpecifierSet(self.seraph)


class ExtensionPermissions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tools: list[str] = Field(default_factory=list)
    execution_boundaries: list[str] = Field(default_factory=list)
    network: bool = False
    secrets: list[str] = Field(default_factory=list)
    env: list[str] = Field(default_factory=list)

    @field_validator("tools", "execution_boundaries", "secrets", "env")
    @classmethod
    def _validate_string_list(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            if not isinstance(value, str) or not value.strip():
                raise ValueError("must contain non-empty strings")
            item = value.strip()
            if item not in seen:
                normalized.append(item)
                seen.add(item)
        return normalized


class ExtensionContributionPaths(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skills: list[str] = Field(default_factory=list)
    workflows: list[str] = Field(default_factory=list)
    runbooks: list[str] = Field(default_factory=list)
    starter_packs: list[str] = Field(default_factory=list)
    provider_presets: list[str] = Field(default_factory=list)
    prompt_packs: list[str] = Field(default_factory=list)
    scheduled_routines: list[str] = Field(default_factory=list)
    mcp_servers: list[str] = Field(default_factory=list)
    managed_connectors: list[str] = Field(default_factory=list)
    observer_definitions: list[str] = Field(default_factory=list)
    observer_connectors: list[str] = Field(default_factory=list)
    channel_adapters: list[str] = Field(default_factory=list)
    workspace_adapters: list[str] = Field(default_factory=list)

    @field_validator(*_CONTRIBUTION_FIELDS)
    @classmethod
    def _validate_paths(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            if not isinstance(value, str):
                raise ValueError("must contain path strings")
            path = _normalize_relative_path(value)
            if path in seen:
                raise ValueError(f"duplicate path: {path}")
            normalized.append(path)
            seen.add(path)
        return normalized

    def is_empty(self) -> bool:
        return not any(getattr(self, field_name) for field_name in _CONTRIBUTION_FIELDS)

    def contributed_types(self) -> set[str]:
        return {
            field_name
            for field_name in _CONTRIBUTION_FIELDS
            if getattr(self, field_name)
        }


class ExtensionManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    version: str
    display_name: str
    kind: ExtensionKind
    compatibility: ExtensionCompatibility
    publisher: ExtensionPublisher
    trust: ExtensionTrust
    permissions: ExtensionPermissions = Field(default_factory=ExtensionPermissions)
    contributes: ExtensionContributionPaths
    summary: str | None = None
    description: str | None = None

    @field_validator("id")
    @classmethod
    def _validate_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        allowed = set("abcdefghijklmnopqrstuvwxyz0123456789._-")
        if any(char not in allowed for char in value):
            raise ValueError("must use lowercase letters, numbers, dots, hyphens, or underscores")
        if value[0] in "._-" or value[-1] in "._-":
            raise ValueError("must not start or end with punctuation")
        return value

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        try:
            Version(value)
        except InvalidVersion as exc:
            raise ValueError(f"invalid version: {value}") from exc
        return value

    @field_validator("display_name")
    @classmethod
    def _validate_non_empty_string(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("summary", "description")
    @classmethod
    def _validate_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @model_validator(mode="after")
    def _validate_kind_against_contributions(self) -> "ExtensionManifest":
        contributions = self.contributes.contributed_types()
        if not contributions:
            raise ValueError("must contribute at least one typed surface")

        if self.kind == ExtensionKind.CONNECTOR_PACK and not contributions.intersection(_CONNECTOR_FIELDS):
            raise ValueError("connector-pack manifests must contribute at least one connector surface")

        return self

    def contributed_types(self) -> set[str]:
        return self.contributes.contributed_types()

    def is_compatible_with(self, seraph_version: str) -> bool:
        return self.compatibility.is_compatible_with(seraph_version)


def _format_validation_errors(exc: ValidationError) -> list[dict[str, Any]]:
    formatted: list[dict[str, Any]] = []
    for error in exc.errors():
        formatted.append(
            {
                "loc": [str(part) for part in error.get("loc", ())],
                "message": error.get("msg", "validation error"),
                "type": error.get("type", "value_error"),
            }
        )
    return formatted


def parse_extension_manifest(content: str, *, source: str = "<memory>") -> ExtensionManifest:
    """Parse YAML manifest content into a validated extension manifest."""
    try:
        payload = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise ExtensionManifestError(source, f"invalid YAML: {exc}") from exc

    if not isinstance(payload, dict):
        raise ExtensionManifestError(source, "manifest root must be a mapping")

    try:
        return ExtensionManifest.model_validate(payload)
    except ValidationError as exc:
        errors = _format_validation_errors(exc)
        first_error = errors[0]["message"] if errors else "validation error"
        raise ExtensionManifestError(source, first_error, errors=errors) from exc


def load_extension_manifest(path: str | Path) -> ExtensionManifest:
    """Load and validate an extension manifest from disk."""
    manifest_path = Path(path)
    try:
        content = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ExtensionManifestError(str(manifest_path), f"failed to read manifest: {exc}") from exc
    return parse_extension_manifest(content, source=str(manifest_path))
