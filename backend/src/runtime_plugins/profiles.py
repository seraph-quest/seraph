"""ADR-036 §4/§6 bundle catalogue, named profiles and the layered patch validator.

This is part of Seraph's composition bootstrap, not a plugin. It holds no
policy, agent loop or domain state. It answers one question before boot: which
reviewed composition does this process run, and is it allowed?

* ``BUNDLES`` is the reviewed bundle catalogue (ADR-036 §4). Domain bundles
  carry the runtime domains ``trust.DOMAIN_BUNDLES`` maps to them; app bundles
  carry no domain row yet (their interface domains are later A3.3 slices).
* ``PROFILES`` are the reviewed named compositions ``local``, ``prod`` and
  ``headless`` (§6). ``test`` stays a test-process environment, not a profile.
* ``PATCH_ROWS`` is the closed catalogue of patchable rows. A row is
  catalogued only when it belongs to a non-pinned domain, grants no
  authority, enables no consent-gated provider and carries no credential.
* ``compose_profile`` is the composition-time validator: it refuses a profile
  or patch that omits, replaces or patches a pinned service (§3), and applies
  the layers in the fixed order profile bundles, profile patch, operator patch.

Security switches (operator authentication, local no-login, bind host,
Host/Origin checks, deployment environment) are never rows: they stay
startup-validated environment settings. The environment file is still the base
layer of each catalogued row until its composition flag is removed.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any

from .protocol import ProtocolError, decode_json
from .trust import DOMAIN_BUNDLES, TRUST_BUNDLE

PATCH_SCHEMA = "seraph-composition-patch.v1"
OPERATOR_PATCH_FILE = "composition.patch.json"
MAX_PATCH_BYTES = 16 * 1024
MAX_PATCH_ROWS = 64
PRODUCTION_ENVIRONMENTS = frozenset({"prod", "production"})


class CompositionProfileInvalid(ValueError):
    """The selected profile or a patch layer failed validation; boot refuses it."""

    def __init__(self, reason: str, *, row_id: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.row_id = row_id


@dataclass(frozen=True)
class Bundle:
    name: str
    # trust: pinned, always first. core: carries runtime domain rows, so a
    # profile cannot drop it until domain deactivation exists. app: optional.
    kind: str
    responsibilities: tuple[str, ...]

    @property
    def domains(self) -> tuple[str, ...]:
        from .ownership import DOMAINS
        return tuple(domain for domain in DOMAINS if DOMAIN_BUNDLES.get(domain) == self.name)


# ADR-036 §4, in table order.
BUNDLES: tuple[Bundle, ...] = (
    Bundle(TRUST_BUNDLE, "trust", ("canonical_store", "authority", "audit", "job_and_effect_ledger",
                                   "inference_policy", "execution_guard", "operator_authentication",
                                   "credential_vault")),
    Bundle("seraph-agent", "core", ("agent_loop", "agent_presets", "guardian", "delegation", "board_workers",
                                    "system_prompt", "capability_registry")),
    Bundle("seraph-models", "core", ("near_tee_text_slot", "near_tee_vision_slot", "near_tee_embedding_slot")),
    Bundle("seraph-capabilities", "core", ("research", "source_extraction", "browser", "repository", "filesystem",
                                           "sandbox", "mail", "calendar", "github", "documents",
                                           "screen_observation", "external_adapters", "authored_packs")),
    Bundle("seraph-domains", "core", ("goals", "memory", "artifacts", "conversation", "connections")),
    Bundle("seraph-lifecycle", "core", ("scheduler", "dispatch")),
    Bundle("seraph-cockpit", "app", ("web_cockpit", "http_api", "websocket_api")),
    Bundle("seraph-channels", "app", ("paired_channels", "webhooks")),
    Bundle("seraph-edge", "app", ("screen_daemon", "capture_upload", "selected_text_companion")),
    Bundle("seraph-headless", "app", ("headless_runner",)),
)
BUNDLE_NAMES = tuple(bundle.name for bundle in BUNDLES)
_BUNDLES = {bundle.name: bundle for bundle in BUNDLES}
CORE_BUNDLES = tuple(bundle.name for bundle in BUNDLES if bundle.kind == "core")


@dataclass(frozen=True)
class PatchRow:
    row_id: str
    runtime_domain: str
    # The settings attribute (environment flag) that is this row's base layer.
    env_setting: str
    env_name: str


# The closed patchable-row catalogue. ADR-036 §6: each composition-selecting
# environment flag gets a row; SCHEDULER_ENABLED is the first.
PATCH_ROWS: dict[str, PatchRow] = {
    "seraph.scheduler.v1#enabled": PatchRow("seraph.scheduler.v1#enabled", "seraph.scheduler.v1",
                                            "scheduler_enabled", "SCHEDULER_ENABLED"),
}

# Startup-validated security switches: never a profile or patch row (§6).
SECURITY_SETTINGS = frozenset({
    "deployment_environment", "backend_bind_host", "operator_auth_secret", "operator_auth_secret_hash",
    "operator_auth_local_no_login", "operator_auth_allowed_hosts", "operator_auth_allowed_origins",
    "operator_auth_trusted_proxy_ips", "operator_auth_allow_unauthenticated_tests", "operator_auth_cookie_secure",
    "capability_journal_secret", "capability_journal_secret_hash", "vault_key",
})
_ROW_ID = re.compile(r"(seraph\.[a-z0-9-]+\.v[0-9]+)#([a-z][a-z0-9_]{0,63})\Z")


@dataclass(frozen=True)
class Profile:
    profile_id: str
    bundles: tuple[str, ...]
    # Which DEPLOYMENT_ENVIRONMENT values it accepts: "production" (prod/production),
    # "non_production" (anything else, matching what operator authentication and
    # the worker already accept) or "any".
    environments: str
    # The environment file the managed launcher reads; None means either.
    env_file: str | None
    patch: tuple[dict[str, Any], ...] = ()


_ALL = BUNDLE_NAMES[:BUNDLE_NAMES.index("seraph-headless")]
PROFILES: dict[str, Profile] = {
    "local": Profile("local", _ALL, "non_production", ".env.dev"),
    "prod": Profile("prod", _ALL, "production", ".env.prod"),
    "headless": Profile("headless", (TRUST_BUNDLE, *CORE_BUNDLES, "seraph-channels", "seraph-headless"),
                        "any", None),
}


@dataclass(frozen=True)
class ComposedProfile:
    profile: Profile
    source: str  # "explicit" (COMPOSITION_PROFILE) or "deployment_environment"
    # Rows a patch layer set, in application order: (row_id, value, layer).
    applied: tuple[tuple[str, Any, str], ...]
    operator_patch_present: bool

    def value(self, row_id: str) -> tuple[bool, Any]:
        """(set_by_patch, value) for one catalogued row; the last layer wins."""
        found: tuple[bool, Any] = (False, None)
        for applied_id, value, _layer in self.applied:
            if applied_id == row_id:
                found = (True, value)
        return found

    @property
    def effective_patch(self) -> list[dict[str, Any]]:
        latest: dict[str, tuple[Any, str]] = {}
        for row_id, value, layer in self.applied:
            latest[row_id] = (value, layer)
        return [{"id": row_id, "value": value, "layer": layer} for row_id, (value, layer) in latest.items()]

    @property
    def digest(self) -> str:
        canonical = json.dumps({"profile_id": self.profile.profile_id, "bundles": list(self.profile.bundles),
                                "effective_patch": self.effective_patch},
                               sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def payload(self) -> dict[str, Any]:
        return {"state": "valid", "reason": None, "profile_id": self.profile.profile_id, "source": self.source,
                "env_file": self.profile.env_file, "bundles": list(self.profile.bundles),
                "operator_patch": "present" if self.operator_patch_present else "absent", "digest": self.digest}


def _environment(value: str) -> str:
    return str(value or "").strip().lower()


def validate_bundles(bundles: Any) -> tuple[str, ...]:
    """A profile's bundle list: seraph-trust first, every core bundle, no unknown or repeated bundle."""
    if type(bundles) not in (tuple, list) or not bundles or bundles[0] != TRUST_BUNDLE:
        raise CompositionProfileInvalid("profile_trust_bundle_missing")
    seen: set[str] = set()
    for name in bundles:
        if type(name) is not str or name not in _BUNDLES:
            raise CompositionProfileInvalid("profile_bundle_unknown")
        if name in seen:
            raise CompositionProfileInvalid("profile_bundle_duplicate")
        seen.add(name)
    if any(name not in seen for name in CORE_BUNDLES):
        raise CompositionProfileInvalid("profile_core_bundle_missing")
    return tuple(bundles)


def validate_patch(rows: Any, *, layer: str, bundles: tuple[str, ...]) -> list[tuple[str, Any, str]]:
    """One patch layer's rows. Closed allow-list: anything not catalogued is refused."""
    if type(rows) not in (tuple, list):
        raise CompositionProfileInvalid("patch_invalid")
    if len(rows) > MAX_PATCH_ROWS:
        raise CompositionProfileInvalid("patch_too_large")
    applied: list[tuple[str, Any, str]] = []
    seen: set[str] = set()
    for row in rows:
        if type(row) is not dict or "id" not in row or type(row["id"]) is not str:
            raise CompositionProfileInvalid("patch_row_invalid")
        row_id = row["id"]
        match = _ROW_ID.fullmatch(row_id)
        field = match[2] if match else row_id.strip().lower().replace("-", "_")
        if field in SECURITY_SETTINGS or field.startswith("operator_auth"):
            raise CompositionProfileInvalid("patch_row_security_setting", row_id=row_id[:96])
        if match is None:
            raise CompositionProfileInvalid("patch_row_invalid", row_id=row_id[:96])
        domain = match[1]
        if DOMAIN_BUNDLES.get(domain) == TRUST_BUNDLE:
            # ADR-036 §3: no layer may touch, replace or reconfigure a pinned service.
            raise CompositionProfileInvalid("patch_row_pinned", row_id=row_id)
        if set(row) == {"id", "provider"}:
            # Adding a provider row needs a reviewed provider for that seam; none is catalogued yet.
            raise CompositionProfileInvalid("patch_provider_unreviewed", row_id=row_id)
        if set(row) != {"id", "value"}:
            raise CompositionProfileInvalid("patch_row_invalid", row_id=row_id)
        spec = PATCH_ROWS.get(row_id)
        if spec is None:
            raise CompositionProfileInvalid("patch_row_unknown", row_id=row_id)
        if DOMAIN_BUNDLES.get(spec.runtime_domain) not in bundles:
            raise CompositionProfileInvalid("patch_row_bundle_not_selected", row_id=row_id)
        if type(row["value"]) is not bool:
            # Every catalogued row is a boolean today; no string can carry a credential.
            raise CompositionProfileInvalid("patch_value_invalid", row_id=row_id)
        if row_id in seen:
            raise CompositionProfileInvalid("patch_row_duplicate", row_id=row_id)
        seen.add(row_id)
        applied.append((row_id, row["value"], layer))
    return applied


def decode_operator_patch(data: bytes) -> list[Any]:
    """The operator patch document: ``{"schema": PATCH_SCHEMA, "rows": [...]}``, closed."""
    if len(data) > MAX_PATCH_BYTES:
        raise CompositionProfileInvalid("operator_patch_too_large")
    try:
        document = decode_json(data)
    except ProtocolError as exc:
        raise CompositionProfileInvalid("operator_patch_invalid") from exc
    if type(document) is not dict or set(document) != {"schema", "rows"} or document["schema"] != PATCH_SCHEMA:
        raise CompositionProfileInvalid("operator_patch_invalid")
    return document["rows"]


def load_operator_patch(workspace: Path) -> list[Any] | None:
    """Read ``<workspace>/composition.patch.json`` safely; ``None`` when absent."""
    path = Path(workspace) / OPERATOR_PATCH_FILE
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CompositionProfileInvalid("operator_patch_unsafe") from exc
    if not stat.S_ISREG(before.st_mode):
        # Never open a symlink, FIFO or device node.
        raise CompositionProfileInvalid("operator_patch_unsafe")
    try:
        # The service user, root, or whoever owns the workspace directory (a
        # host-written file in a container bind mount).
        owners = {os.getuid(), 0, os.stat(workspace).st_uid}
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError as exc:
        raise CompositionProfileInvalid("operator_patch_unsafe") from exc
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or (metadata.st_dev, metadata.st_ino) != (before.st_dev, before.st_ino)
                or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH) or metadata.st_uid not in owners):
            raise CompositionProfileInvalid("operator_patch_unsafe")
        if metadata.st_size > MAX_PATCH_BYTES:
            raise CompositionProfileInvalid("operator_patch_too_large")
        data = os.read(descriptor, MAX_PATCH_BYTES + 1)
    finally:
        os.close(descriptor)
    return decode_operator_patch(data)


def resolve_profile_id(requested: str | None, deployment_environment: str) -> tuple[str, str]:
    """(profile_id, source): an explicit ``COMPOSITION_PROFILE``, else the launcher's environment."""
    explicit = str(requested or "").strip()
    if explicit:
        return explicit, "explicit"
    return ("prod" if _environment(deployment_environment) in PRODUCTION_ENVIRONMENTS else "local",
            "deployment_environment")


def compose_profile(requested: str | None, *, deployment_environment: str, local_no_login: bool,
                    operator_patch: list[Any] | None, profiles: dict[str, Profile] | None = None) -> ComposedProfile:
    """Validate the selected profile and its layers; raise ``CompositionProfileInvalid`` before boot."""
    catalogue = PROFILES if profiles is None else profiles
    profile_id, source = resolve_profile_id(requested, deployment_environment)
    profile = catalogue.get(profile_id)
    if profile is None:
        raise CompositionProfileInvalid("profile_unknown")
    environment = _environment(deployment_environment)
    production = environment in PRODUCTION_ENVIRONMENTS
    if profile.environments not in {"production", "non_production", "any"} or (
            profile.environments == "production" and not production) or (
            profile.environments == "non_production" and production):
        raise CompositionProfileInvalid("profile_environment_mismatch")
    if local_no_login and environment in PRODUCTION_ENVIRONMENTS:
        # Startup validation already refuses this; a profile never carries it either.
        raise CompositionProfileInvalid("profile_no_login_forbidden")
    bundles = validate_bundles(profile.bundles)
    applied = validate_patch(list(profile.patch), layer="profile", bundles=bundles)
    if operator_patch is not None:
        applied += validate_patch(operator_patch, layer="operator", bundles=bundles)
    return ComposedProfile(profile, source, tuple(applied), operator_patch is not None)


# The composition this process booted through (one per process, set by the
# lifespan before any provider activates and cleared on shutdown).
_booted: ComposedProfile | None = None


def boot_profile() -> ComposedProfile:
    """Validate and record this process's composition from settings and the workspace patch."""
    global _booted
    from config.settings import settings
    composed = compose_profile(settings.composition_profile, deployment_environment=settings.deployment_environment,
                               local_no_login=bool(settings.operator_auth_local_no_login),
                               operator_patch=load_operator_patch(Path(settings.workspace_dir)))
    _booted = composed
    return composed


def release_profile() -> None:
    global _booted
    _booted = None


def booted_profile() -> ComposedProfile | None:
    return _booted


_UNSET = object()


def effective_row(row_id: str, base: Any = _UNSET) -> Any:
    """A catalogued row's effective value: the last patch layer, else its environment base.

    ``base`` is the caller's own reading of the environment setting; without it
    the live ``settings`` attribute is read.
    """
    spec = PATCH_ROWS[row_id]
    if _booted is not None:
        set_by_patch, value = _booted.value(row_id)
        if set_by_patch:
            return value
    if base is not _UNSET:
        return base
    from config.settings import settings
    return getattr(settings, spec.env_setting)


def scheduler_enabled(base: Any = _UNSET) -> bool:
    return bool(effective_row("seraph.scheduler.v1#enabled", base))


def recovery_text(error: CompositionProfileInvalid) -> str:
    """Operator-facing fix for a refused composition; names files and settings, never values."""
    if error.reason.startswith("operator_patch") or error.reason.startswith("patch_"):
        # Only a catalogued row id is echoed; operator file text never reaches the log.
        where = "" if error.row_id is None else f" (row {error.row_id if error.row_id in PATCH_ROWS else 'redacted'})"
        return (f"Composition refused: {error.reason}{where}. Fix or remove <workspace>/{OPERATOR_PATCH_FILE} "
                "(rows: " + ", ".join(sorted(PATCH_ROWS)) + "), then restart.")
    return (f"Composition refused: {error.reason}. Set COMPOSITION_PROFILE to one of "
            + ", ".join(PROFILES) + " that matches DEPLOYMENT_ENVIRONMENT (or leave it empty), then restart.")


__all__ = ["BUNDLES", "BUNDLE_NAMES", "CORE_BUNDLES", "Bundle", "ComposedProfile", "CompositionProfileInvalid",
           "OPERATOR_PATCH_FILE", "PATCH_ROWS", "PATCH_SCHEMA", "PROFILES", "PatchRow", "Profile",
           "SECURITY_SETTINGS", "boot_profile", "booted_profile", "compose_profile", "decode_operator_patch",
           "effective_row", "load_operator_patch", "recovery_text", "release_profile", "resolve_profile_id",
           "scheduler_enabled", "validate_bundles", "validate_patch"]
