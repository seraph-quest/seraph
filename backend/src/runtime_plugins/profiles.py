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
Host/Origin checks, deployment environment, and the egress, consent, privacy,
execution and pinned-inference switches #1206 classified) are never rows: they
stay startup-validated environment settings. The environment is still the base
layer of each catalogued row until its flag is removed: each row carries an
``env_stage`` on the removal path (supported, deprecated, retired) and the
issue that removes it. ``inventory.ENV_FLAGS`` classifies every flag.
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


# The stages of a row's environment base layer (ADR-036 §6 removal path, #1206):
# * supported: the environment flag is the base layer below every patch layer.
# * deprecated: still the base layer, so behaviour is unchanged, but each boot
#   that still sets it logs a warning naming the row and writes one
#   ``composition_env_flag_deprecated`` audit event.
# * retired: the flag is no longer read. A value still set to the default is
#   ignored (audited ``composition_env_flag_ignored``), as is any value a patch
#   row covers; a non-default value with no patch row refuses boot with
#   ``env_flag_retired`` so the install never changes behaviour silently.
ENV_STAGES = ("supported", "deprecated", "retired")
ENV_DEPRECATED_EVENT = "composition_env_flag_deprecated"
ENV_IGNORED_EVENT = "composition_env_flag_ignored"


@dataclass(frozen=True)
class PatchRow:
    row_id: str
    runtime_domain: str
    # The settings attribute (environment flag) that is this row's base layer.
    env_setting: str
    env_name: str
    # The row's typed schema: the values a patch layer may set. ``(False,)`` is
    # disable-only: the flag starts observation or a screen-derived prompt, so a
    # patch may turn it off but never on (ADR-036 §6: a patch never enables a
    # consent-gated provider or restores an opt-out).
    values: tuple[bool, ...] = (False, True)
    # The value when neither a patch layer nor the environment sets it; it equals
    # the settings field default, so retiring the env base changes nothing.
    default: bool = True
    env_stage: str = "supported"
    # The tracked issue that removes the environment base layer.
    removal_issue: str = ""

    def payload(self) -> dict[str, Any]:
        return {"id": self.row_id, "env": self.env_name, "values": list(self.values), "default": self.default,
                "env_stage": self.env_stage, "removal_issue": self.removal_issue}


def _row(row_id: str, env_setting: str, *, values: tuple[bool, ...] = (False, True), default: bool,
         env_stage: str = "supported", removal_issue: str) -> PatchRow:
    return PatchRow(row_id, row_id.partition("#")[0], env_setting, env_setting.upper(), values, default, env_stage,
                    removal_issue)


# The closed patchable-row catalogue. ADR-036 §6: each composition-selecting
# environment flag gets a row and a removal issue; SCHEDULER_ENABLED was the
# first row (#1204) and is the first on the removal path (#1206). Every other
# boolean setting and launcher flag is classified in ``inventory.ENV_FLAGS``.
PATCH_ROWS: dict[str, PatchRow] = {row.row_id: row for row in (
    _row("seraph.scheduler.v1#enabled", "scheduler_enabled", default=True, env_stage="deprecated",
         removal_issue="#1337"),
    _row("seraph.delegation.v1#enabled", "use_delegation", default=False, removal_issue="#1338"),
    _row("seraph.scheduler.v1#screenshot_folder_ingest", "screenshot_folder_ingest_enabled", values=(False,),
         default=True, removal_issue="#1339"),
    _row("seraph.scheduler.v1#screenshot_observation_digest", "screenshot_observation_digest_enabled",
         values=(False,), default=True, removal_issue="#1340"),
    _row("seraph.scheduler.v1#end_of_day_report", "end_of_day_report_enabled", default=True,
         removal_issue="#1341"),
)}
SCHEDULER_ROW = "seraph.scheduler.v1#enabled"
DELEGATION_ROW = "seraph.delegation.v1#enabled"
SCREENSHOT_FOLDER_INGEST_ROW = "seraph.scheduler.v1#screenshot_folder_ingest"
SCREENSHOT_OBSERVATION_DIGEST_ROW = "seraph.scheduler.v1#screenshot_observation_digest"
END_OF_DAY_REPORT_ROW = "seraph.scheduler.v1#end_of_day_report"

# Startup-validated security switches: never a profile or patch row (§6). The
# authentication and bind settings (#1204), plus the switches #1206 classified:
# egress, consent, privacy, execution and pinned-inference settings. A row id
# naming any of them is refused as a security setting, not as an unknown row.
SECURITY_SETTINGS = frozenset({
    "deployment_environment", "backend_bind_host", "operator_auth_secret", "operator_auth_secret_hash",
    "operator_auth_local_no_login", "operator_auth_allowed_hosts", "operator_auth_allowed_origins",
    "operator_auth_trusted_proxy_ips", "operator_auth_allow_unauthenticated_tests", "operator_auth_cookie_secure",
    "capability_journal_secret", "capability_journal_secret_hash", "vault_key",
    # #1206 (inventory.ENV_FLAGS, kind security_switch).
    "openrouter_provider_only", "openrouter_allow_fallbacks", "openrouter_require_parameters",
    "openrouter_zero_data_retention", "near_tee_catalog_refresh_enabled", "screen_derived_llm_allow_remote",
    "screen_derived_llm_require_profile_proof", "end_of_day_report_llm_enabled", "email_reports_enabled",
    "email_reports_preview_required", "smtp_use_tls", "llm_log_enabled", "llm_log_content",
    "repo_sandbox__enabled", "repo_sandbox__require_isolation", "screen_analysis_provider", "seraph_vlm_mode",
    "daemon_enabled", "proxy_enabled",
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
class EnvBase:
    """How one catalogued row's environment base layer stood at composition time."""
    row_id: str
    env_set: bool  # the environment (process env or env file) sets the flag explicitly
    # "patch" (a patch layer sets the row), "env" (the environment value is the
    # base), "default" (nothing sets it) or "ignored" (a retired flag still set).
    source: str

    def payload(self) -> dict[str, Any]:
        spec = PATCH_ROWS[self.row_id]
        return {"id": self.row_id, "env": spec.env_name, "env_stage": spec.env_stage, "env_set": self.env_set,
                "source": self.source, "removal_issue": spec.removal_issue}


@dataclass(frozen=True)
class ComposedProfile:
    profile: Profile
    source: str  # "explicit" (COMPOSITION_PROFILE) or "deployment_environment"
    # Rows a patch layer set, in application order: (row_id, value, layer).
    applied: tuple[tuple[str, Any, str], ...]
    operator_patch_present: bool
    # Each catalogued row's environment base layer; None when the environment
    # was not read (the offline dump, which never reads settings).
    env_base: tuple[EnvBase, ...] | None = None

    def env_base_payload(self) -> dict[str, Any]:
        if self.env_base is None:
            return {"read": False, "rows": []}
        return {"read": True, "rows": [item.payload() for item in self.env_base]}

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
        if row["value"] not in spec.values:
            # A disable-only row: a patch never turns observation or a screen-derived prompt on.
            raise CompositionProfileInvalid("patch_value_consent_gated", row_id=row_id)
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


def env_base_layer(applied: list[tuple[str, Any, str]], env_values: dict[str, Any],
                   rows: dict[str, PatchRow] | None = None) -> tuple[EnvBase, ...]:
    """Each row's environment base layer; refuse a retired flag whose value would otherwise change silently.

    ``env_values`` holds only the settings the environment sets explicitly, by
    settings attribute.
    """
    catalogue = PATCH_ROWS if rows is None else rows
    patched = {row_id for row_id, _value, _layer in applied}
    found: list[EnvBase] = []
    for row_id, spec in catalogue.items():
        if spec.env_stage not in ENV_STAGES:
            raise CompositionProfileInvalid("env_stage_unknown", row_id=row_id)
        env_set = spec.env_setting in env_values
        if row_id in patched:
            source = "patch"
        elif not env_set:
            source = "default"
        elif spec.env_stage != "retired":
            source = "env"
        elif bool(env_values[spec.env_setting]) == spec.default:
            source = "ignored"
        else:
            # ADR-036 §6 removal path: the flag is gone, and dropping its value
            # would change behaviour. The operator moves it into a patch row.
            raise CompositionProfileInvalid("env_flag_retired", row_id=row_id)
        found.append(EnvBase(row_id, env_set, source))
    return tuple(found)


def compose_profile(requested: str | None, *, deployment_environment: str, local_no_login: bool,
                    operator_patch: list[Any] | None, profiles: dict[str, Profile] | None = None,
                    env_values: dict[str, Any] | None = None) -> ComposedProfile:
    """Validate the selected profile and its layers; raise ``CompositionProfileInvalid`` before boot.

    ``env_values`` are the row settings the environment sets explicitly; None
    means the environment was not read (the offline dump).
    """
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
    env_base = None if env_values is None else env_base_layer(applied, env_values)
    return ComposedProfile(profile, source, tuple(applied), operator_patch is not None, env_base)


# The composition this process booted through (one per process, set by the
# lifespan before any provider activates and cleared on shutdown).
_booted: ComposedProfile | None = None


def boot_profile() -> ComposedProfile:
    """Validate and record this process's composition from settings and the workspace patch."""
    global _booted
    from config.settings import settings
    composed = compose_profile(settings.composition_profile, deployment_environment=settings.deployment_environment,
                               local_no_login=bool(settings.operator_auth_local_no_login),
                               operator_patch=load_operator_patch(Path(settings.workspace_dir)),
                               env_values=explicit_env_values(settings))
    _booted = composed
    return composed


def explicit_env_values(settings_object: Any) -> dict[str, Any]:
    """The row settings the environment (process env or env file) sets explicitly.

    Pydantic records a field in ``model_fields_set`` only when a source supplied
    it, so a default is never mistaken for an operator's choice.
    """
    explicit = set(getattr(settings_object, "model_fields_set", ()) or ())
    return {spec.env_setting: getattr(settings_object, spec.env_setting) for spec in PATCH_ROWS.values()
            if spec.env_setting in explicit}


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
    if spec.env_stage == "retired":
        # The removed env flag is never read again; boot already refused a value it would drop.
        return spec.default
    if base is not _UNSET:
        return base
    from config.settings import settings
    return getattr(settings, spec.env_setting)


def scheduler_enabled(base: Any = _UNSET) -> bool:
    return bool(effective_row(SCHEDULER_ROW, base))


def delegation_enabled(base: Any = _UNSET) -> bool:
    """USE_DELEGATION, or the ``seraph.delegation.v1#enabled`` row."""
    return bool(effective_row(DELEGATION_ROW, base))


def screenshot_folder_ingest_enabled(base: Any = _UNSET) -> bool:
    """SCREENSHOT_FOLDER_INGEST_ENABLED, or the disable-only screenshot-folder ingest row."""
    return bool(effective_row(SCREENSHOT_FOLDER_INGEST_ROW, base))


def screenshot_observation_digest_enabled(base: Any = _UNSET) -> bool:
    """SCREENSHOT_OBSERVATION_DIGEST_ENABLED, or the disable-only digest row."""
    return bool(effective_row(SCREENSHOT_OBSERVATION_DIGEST_ROW, base))


def end_of_day_report_enabled(base: Any = _UNSET) -> bool:
    """END_OF_DAY_REPORT_ENABLED, or the end-of-day report row."""
    return bool(effective_row(END_OF_DAY_REPORT_ROW, base))


def env_base_notices(composed: ComposedProfile | None) -> list[dict[str, Any]]:
    """The deprecated or retired env flags this boot still sees set, as audit-ready records."""
    if composed is None or composed.env_base is None:
        return []
    notices = []
    for item in composed.env_base:
        spec = PATCH_ROWS[item.row_id]
        if not item.env_set or spec.env_stage == "supported":
            continue
        event = ENV_DEPRECATED_EVENT if item.source == "env" else ENV_IGNORED_EVENT
        notices.append({"event_type": event, **item.payload()})
    return notices


async def record_env_base_layer() -> list[dict[str, Any]]:
    """Log and audit each deprecated or retired env flag this boot still sees set.

    Runs once per boot after the provider worker opens the state gate. Best
    effort: an unwritable audit never changes the composition.
    """
    import logging
    logger = logging.getLogger(__name__)
    notices = env_base_notices(_booted)
    for notice in notices:
        if notice["event_type"] == ENV_DEPRECATED_EVENT:
            summary = (f"{notice['env']} is deprecated; move it to the patch row {notice['id']} in "
                       f"<workspace>/{OPERATOR_PATCH_FILE} ({notice['removal_issue']})")
        else:
            summary = (f"{notice['env']} is retired and was ignored; the patch row {notice['id']} or its "
                       f"default applies ({notice['removal_issue']})")
        logger.warning("Composition: %s", summary)
        try:
            from src.audit.repository import audit_repository
            await audit_repository.log_event(event_type=notice["event_type"], actor="system",
                                             tool_name="composition_profile", risk_level="low",
                                             summary=summary,
                                             details={key: notice[key] for key in (
                                                 "id", "env", "env_stage", "source", "removal_issue")})
        except Exception:  # noqa: BLE001 - the composition already booted
            logger.warning("Could not audit the %s environment flag notice", notice["env"], exc_info=True)
    return notices


def recovery_text(error: CompositionProfileInvalid) -> str:
    """Operator-facing fix for a refused composition; names files and settings, never values."""
    if error.reason == "env_flag_retired" and error.row_id in PATCH_ROWS:
        spec = PATCH_ROWS[error.row_id]
        return (f"Composition refused: {spec.env_name} was removed ({spec.removal_issue}). Add the row "
                f'{{"id": "{spec.row_id}", "value": ...}} to <workspace>/{OPERATOR_PATCH_FILE} with the value '
                f"you set, or delete {spec.env_name} from the environment file, then restart.")
    if error.reason.startswith("operator_patch") or error.reason.startswith("patch_"):
        # Only a catalogued row id is echoed; operator file text never reaches the log.
        where = "" if error.row_id is None else f" (row {error.row_id if error.row_id in PATCH_ROWS else 'redacted'})"
        return (f"Composition refused: {error.reason}{where}. Fix or remove <workspace>/{OPERATOR_PATCH_FILE} "
                "(rows: " + ", ".join(sorted(PATCH_ROWS)) + "), then restart.")
    return (f"Composition refused: {error.reason}. Set COMPOSITION_PROFILE to one of "
            + ", ".join(PROFILES) + " that matches DEPLOYMENT_ENVIRONMENT (or leave it empty), then restart.")


__all__ = ["BUNDLES", "BUNDLE_NAMES", "CORE_BUNDLES", "DELEGATION_ROW", "END_OF_DAY_REPORT_ROW", "ENV_DEPRECATED_EVENT",
           "ENV_IGNORED_EVENT", "ENV_STAGES", "Bundle", "ComposedProfile", "CompositionProfileInvalid", "EnvBase",
           "OPERATOR_PATCH_FILE", "PATCH_ROWS", "PATCH_SCHEMA", "PROFILES", "PatchRow", "Profile",
           "SCHEDULER_ROW", "SCREENSHOT_FOLDER_INGEST_ROW", "SCREENSHOT_OBSERVATION_DIGEST_ROW",
           "SECURITY_SETTINGS", "boot_profile", "booted_profile", "compose_profile", "decode_operator_patch",
           "delegation_enabled", "effective_row", "end_of_day_report_enabled", "env_base_layer",
           "env_base_notices", "explicit_env_values", "load_operator_patch", "record_env_base_layer",
           "recovery_text", "release_profile", "resolve_profile_id", "scheduler_enabled",
           "screenshot_folder_ingest_enabled", "screenshot_observation_digest_enabled", "validate_bundles",
           "validate_patch"]
