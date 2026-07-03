"""Shared runtime settings for screenshot analysis."""

from __future__ import annotations

import json
import os
from pathlib import Path

from config.settings import settings

VALID_SCREEN_ANALYSIS_PROVIDERS = {"", "apple-vision", "local-vlm", "openrouter"}
SCREENSHOT_FOLDER_ENV = "SERAPH_SCREENSHOT_FOLDER"


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
    if provider not in VALID_SCREEN_ANALYSIS_PROVIDERS:
        provider = ""
    payload["provider"] = provider
    payload["enabled"] = bool(payload.get("enabled"))
    payload["preserve_captures"] = bool(payload.get("preserve_captures"))
    payload["model"] = str(payload.get("model") or "")
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
    return settings.local_vlm_model.strip() or str(read_screen_analysis_settings().get("model") or "").strip()


def _default_screen_analysis_settings() -> dict[str, object]:
    provider = (
        os.environ.get("SERAPH_SCREEN_ANALYSIS_PROVIDER", "").strip()
        or settings.screen_analysis_provider.strip()
    )
    model = settings.local_vlm_model.strip() or settings.codex_local_model.strip()
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
