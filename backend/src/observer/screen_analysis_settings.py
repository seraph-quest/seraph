"""Shared runtime settings for screenshot analysis."""

from __future__ import annotations

import json
import os
from pathlib import Path

from config.settings import settings

# The active phase has one semantic-analysis provider. Historical provider
# values are invalidated on read instead of remaining an executable escape
# hatch to local VLM or Apple Vision routes.
VALID_SCREEN_ANALYSIS_PROVIDERS = {"", "openrouter"}
# ADR-035 (PR4): the NEAR TEE vision slot. The operator opts in by choosing
# provider ``near_tee`` here (an explicit ``""`` keeps background screenshot
# analysis off even while the slot stays configured); the model and its
# availability always come from the canonical model-fabric route.
NEAR_TEE_SCREEN_ANALYSIS_PROVIDER = "near_tee"
NEAR_TEE_VISION_UNAVAILABLE_REASON = "near_tee_vision_slot_unavailable"
NEAR_TEE_VISION_PROFILE_ID = "near_tee.vision"
# ADR-035 retires OpenRouter by policy. A stored ``openrouter`` value is
# projected as no cloud provider (with a stable reason) and cannot be newly
# selected; NEAR TEE vision arrives in a later slice.
RETIRED_SCREEN_ANALYSIS_PROVIDERS = {"openrouter"}


def selectable_screen_analysis_providers() -> set[str]:
    from src.model_fabric.retirement import openrouter_retired
    selectable = {""} if openrouter_retired() else set(VALID_SCREEN_ANALYSIS_PROVIDERS)
    if near_tee_vision_route() is not None:
        selectable.add(NEAR_TEE_SCREEN_ANALYSIS_PROVIDER)
    return selectable


def near_tee_vision_route() -> tuple[str, str] | None:
    """(profile id, canonical model) when the canonical screenshot route policy
    resolves to the NEAR TEE vision slot, else None.

    That policy exists only while the configuration is readable and not
    revoked and the slot is configured, enabled, has current purpose consent,
    a credential fingerprint and a reserve within the inference-lane ceiling.
    It does not look at the screen-analysis provider choice or enablement,
    the vault key itself, the catalog receipt or capability proofs; those are
    checked at dispatch, where a failure becomes a visible blocked status.
    Local only; never contacts NEAR."""
    try:
        from src.model_fabric.configuration import (
            effective_workload_policy, near_tee_slot, read_model_fabric_configuration,
        )
        policy = effective_workload_policy("screenshot_image_analysis")
        if (tuple(policy.allowed_provider_kinds) != (NEAR_TEE_SCREEN_ANALYSIS_PROVIDER,)
                or tuple(policy.allowed_profile_ids) != (NEAR_TEE_VISION_PROFILE_ID,)):
            return None
        slot = near_tee_slot(read_model_fabric_configuration(), "vision")
    except Exception:
        return None
    if slot is None or not slot.enabled:
        return None
    return NEAR_TEE_VISION_PROFILE_ID, slot.model_id
OPENROUTER_SCREEN_MODEL_PREFIX = "openrouter/"
SCREENSHOT_FOLDER_ENV = "SERAPH_SCREENSHOT_FOLDER"


def _saved_vision_route():
    from src.model_fabric.configuration import read_model_fabric_configuration, OPENROUTER_SETUP_V2_SCHEMA_VERSION
    setup = read_model_fabric_configuration().openrouter_setup
    if setup is not None and setup.schema_version == OPENROUTER_SETUP_V2_SCHEMA_VERSION:
        return True, (setup.routes or {}).get("vision")
    return False, None


def normalize_openrouter_model_identifier(value: object) -> str:
    """Return an explicitly OpenRouter-qualified model id or an empty value.

    Screenshot settings are persisted across runtime migrations. Requiring the
    provider prefix makes a stale local/VLM model unexecutable even when a
    historical settings file still contains it.
    """
    candidate = str(value or "").strip()
    if not candidate.startswith(OPENROUTER_SCREEN_MODEL_PREFIX):
        return ""
    model_id = candidate.removeprefix(OPENROUTER_SCREEN_MODEL_PREFIX)
    if (
        not model_id
        or "/" not in model_id
        or any(character.isspace() or ord(character) < 32 for character in model_id)
        or len(model_id) > 256
    ):
        return ""
    return f"{OPENROUTER_SCREEN_MODEL_PREFIX}{model_id}"


def screen_analysis_settings_path() -> Path:
    """Return the local persisted screen-analysis settings path."""
    return Path(settings.workspace_dir).expanduser().resolve() / "screen-analysis-settings.json"


def read_screen_analysis_settings() -> dict[str, object]:
    """Return persisted screen-analysis settings with env/config overrides applied."""
    payload = _default_screen_analysis_settings()
    path = screen_analysis_settings_path()
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = {}
        if isinstance(loaded, dict):
            for key in (
                "enabled",
                "provider",
                "model",
                "preserve_captures",
                "archive_dir",
                "min_seconds_between_captures",
                "max_daily_captures",
                "archive_retention_days",
                "archive_max_mb",
                "screenshot_folder",
                "screenshot_folder_source",
            ):
                if key in loaded:
                    payload[key] = loaded[key]

    env_provider = os.environ.get("SERAPH_SCREEN_ANALYSIS_PROVIDER", "").strip()
    configured_provider = settings.screen_analysis_provider.strip()
    if env_provider or configured_provider:
        payload["provider"] = env_provider or configured_provider

    env_screenshot_folder = os.environ.get(SCREENSHOT_FOLDER_ENV, "").strip()
    if env_screenshot_folder:
        payload["screenshot_folder"] = str(Path(env_screenshot_folder).expanduser().resolve())
        payload["screenshot_folder_source"] = SCREENSHOT_FOLDER_ENV

    provider = str(payload.get("provider") or "")
    if provider not in VALID_SCREEN_ANALYSIS_PROVIDERS | {NEAR_TEE_SCREEN_ANALYSIS_PROVIDER}:
        provider = ""
    selected_near = provider == NEAR_TEE_SCREEN_ANALYSIS_PROVIDER
    payload["provider"] = provider
    payload["enabled"] = bool(payload.get("enabled"))
    payload["preserve_captures"] = bool(payload.get("preserve_captures"))
    payload["model"] = normalize_openrouter_model_identifier(payload.get("model"))
    v2, route = _saved_vision_route()
    if v2:
        # The capture toggle stays independently owned; route identity is canonical.
        payload["provider"] = "openrouter" if route is not None and route.enabled else ""
        payload["model"] = route.model_id if route is not None and route.enabled else ""
    from src.model_fabric.retirement import OPENROUTER_RETIRED_REASON, openrouter_retired
    payload["provider_blocked_reason"] = None
    if openrouter_retired() and payload["provider"] in RETIRED_SCREEN_ANALYSIS_PROVIDERS:
        # Tell every reader (the daemon included) that no cloud provider is
        # active, instead of handing out a retired route.
        payload["provider"] = ""
        payload["model"] = ""
        payload["provider_blocked_reason"] = OPENROUTER_RETIRED_REASON
    near_route = near_tee_vision_route()
    payload["near_tee_vision_available"] = near_route is not None
    if selected_near:
        if near_route is not None:
            # The operator chose the NEAR TEE vision slot and it is usable.
            payload["provider"] = NEAR_TEE_SCREEN_ANALYSIS_PROVIDER
            payload["model"] = near_route[1]
            payload["provider_blocked_reason"] = None
        else:
            payload["provider"] = ""
            payload["model"] = ""
            payload["provider_blocked_reason"] = NEAR_TEE_VISION_UNAVAILABLE_REASON
    # The stored choice survives a temporarily unavailable slot; a retired
    # OpenRouter value is still rewritten to empty on the next save.
    payload["provider_selected"] = NEAR_TEE_SCREEN_ANALYSIS_PROVIDER if selected_near else payload["provider"]
    # The daemon makes no cloud call: it uploads captures to this backend,
    # which analyses them on the governed route named by ``provider``.
    payload["daemon_cloud_provider"] = None
    payload["daemon_capture_handoff"] = "local_upload"
    payload["archive_dir"] = str(Path(str(payload.get("archive_dir") or "")).expanduser().resolve())

    screenshot_folder = str(payload.get("screenshot_folder") or "").strip()
    if screenshot_folder:
        payload["screenshot_folder"] = str(Path(screenshot_folder).expanduser().resolve())
    else:
        payload.pop("screenshot_folder", None)
        if payload.get("screenshot_folder_source") != SCREENSHOT_FOLDER_ENV:
            payload.pop("screenshot_folder_source", None)

    for key in (
        "min_seconds_between_captures",
        "max_daily_captures",
        "archive_max_mb",
    ):
        try:
            payload[key] = max(0, int(payload.get(key) or 0))
        except (TypeError, ValueError):
            payload[key] = 0
    try:
        payload["archive_retention_days"] = max(1, int(payload.get("archive_retention_days") or 365))
    except (TypeError, ValueError):
        payload["archive_retention_days"] = 365
    return payload


def write_screen_analysis_settings(payload: dict[str, object]) -> None:
    """Persist local screen-analysis settings with private permissions."""
    path = screen_analysis_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    path.chmod(0o600)


def effective_screen_analysis_provider() -> str:
    """Return the provider Seraph should use for screenshot semantic analysis."""
    return str(read_screen_analysis_settings().get("provider") or "").strip()


def effective_screen_analysis_enabled() -> bool:
    """Return whether persisted screen-analysis settings allow semantic analysis."""
    return bool(read_screen_analysis_settings().get("enabled"))


def effective_screen_analysis_model() -> str:
    """Return the model label Seraph should send/report for screenshot semantic analysis."""
    current = read_screen_analysis_settings()
    if current.get("provider") == NEAR_TEE_SCREEN_ANALYSIS_PROVIDER:
        return str(current.get("model") or "")
    from src.model_fabric.retirement import openrouter_retired
    if openrouter_retired():
        return ""
    v2, route = _saved_vision_route()
    if v2:
        return route.model_id if route is not None and route.enabled else ""
    configured = normalize_openrouter_model_identifier(settings.screen_analysis_model)
    if configured:
        return configured
    return normalize_openrouter_model_identifier(read_screen_analysis_settings().get("model"))


def _default_screen_analysis_settings() -> dict[str, object]:
    provider = (
        os.environ.get("SERAPH_SCREEN_ANALYSIS_PROVIDER", "").strip()
        or settings.screen_analysis_provider.strip()
    )
    model = normalize_openrouter_model_identifier(settings.screen_analysis_model)
    screen_archive_dir = _screen_archive_dir()
    env_screenshot_folder = os.environ.get(SCREENSHOT_FOLDER_ENV, "").strip()
    payload: dict[str, object] = {
        "enabled": _env_enabled("SERAPH_SCREEN_ANALYSIS_ENABLED", True),
        "provider": provider,
        "model": model,
        "preserve_captures": _env_enabled("SERAPH_PRESERVE_SCREEN_CAPTURES", True),
        "archive_dir": str(screen_archive_dir),
        "min_seconds_between_captures": max(0, settings.screen_analysis_min_seconds_between_captures),
        "max_daily_captures": max(0, settings.screen_analysis_max_daily_captures),
        "archive_retention_days": max(1, settings.screen_capture_archive_retention_days),
        "archive_max_mb": max(0, settings.screen_capture_archive_max_mb),
        "screenshot_folder_source": SCREENSHOT_FOLDER_ENV if env_screenshot_folder else "default",
    }
    if env_screenshot_folder:
        payload["screenshot_folder"] = str(Path(env_screenshot_folder).expanduser().resolve())
    return payload


def _screen_archive_dir() -> Path:
    configured = os.environ.get("SERAPH_SCREEN_CAPTURE_ARCHIVE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if settings.screen_capture_archive_dir.strip():
        return Path(settings.screen_capture_archive_dir).expanduser().resolve()
    return Path("~/Library/Application Support/Seraph/artifacts/screen-captures").expanduser().resolve()


def _env_enabled(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
