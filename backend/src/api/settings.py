"""Settings API — runtime mode management."""

import json
import logging
import os
import stat
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlmodel import select

from config.settings import settings
from src.db.engine import get_session as get_db
from src.db.models import UserProfile
from src.observer.manager import context_manager
from src.observer.user_state import InterruptionMode
from src.tools.policy import MCP_POLICY_MODES, TOOL_POLICY_MODES

logger = logging.getLogger(__name__)
router = APIRouter()


class InterruptionModeRequest(BaseModel):
    mode: str


class CaptureModeRequest(BaseModel):
    mode: str


class ScreenAnalysisSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    provider: str | None = None
    model: str | None = None
    preserve_captures: bool | None = None
    archive_dir: str | None = None
    screenshot_folder: str | None = None
    min_seconds_between_captures: int | None = None
    max_daily_captures: int | None = None
    archive_retention_days: int | None = None
    archive_max_mb: int | None = None


class ManualReportRequest(BaseModel):
    send_email: bool = False
    preview_acknowledged: bool = False
    report_date: date | None = None


class ToolPolicyModeRequest(BaseModel):
    mode: str


class ApprovalModeRequest(BaseModel):
    mode: str


class McpPolicyModeRequest(BaseModel):
    mode: str


_VALID_CAPTURE_MODES = {"on_switch", "balanced", "detailed"}
_VALID_SCREEN_ANALYSIS_PROVIDERS = {"apple-vision", "codex-local", "openrouter"}
_VALID_TOOL_POLICY_MODES = set(TOOL_POLICY_MODES)
_VALID_MCP_POLICY_MODES = set(MCP_POLICY_MODES)
_VALID_APPROVAL_MODES = {"off", "high_risk"}
_TRUE_VALUES = {"1", "true", "yes", "on"}
_SCREENSHOT_FOLDER_ENV = "SERAPH_SCREENSHOT_FOLDER"
_LEGACY_FRAMEKEEPER_SCREENSHOT_FOLDER_ENV = "SERAPH_FRAMEKEEPER_SCREENSHOT_FOLDER"
_LEGACY_FRAMEKEEPER_ARTIFACT_ROOT_ENV = "SERAPH_FRAMEKEEPER_ARTIFACT_ROOT"


def _env_enabled(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUE_VALUES


def _screen_archive_dir() -> tuple[Path, str]:
    seraph_configured = os.environ.get("SERAPH_SCREEN_CAPTURE_ARCHIVE_DIR", "").strip()
    if seraph_configured:
        return Path(seraph_configured).expanduser().resolve(), "SERAPH_SCREEN_CAPTURE_ARCHIVE_DIR"
    settings_configured = settings.screen_capture_archive_dir.strip()
    if settings_configured:
        return Path(settings_configured).expanduser().resolve(), "SCREEN_CAPTURE_ARCHIVE_DIR"
    return (
        Path("~/Library/Application Support/Seraph/artifacts/screen-captures").expanduser().resolve(),
        "default",
    )


def _screenshot_folder() -> tuple[Path, str]:
    configured = os.environ.get(_SCREENSHOT_FOLDER_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve(), _SCREENSHOT_FOLDER_ENV
    payload = _read_screen_analysis_settings()
    settings_configured = str(
        payload.get("screenshot_folder")
        or payload.get("framekeeper_screenshot_folder")
        or payload.get("framekeeper_artifact_root")
        or ""
    ).strip()
    if settings_configured:
        return Path(settings_configured).expanduser().resolve(), "screen-analysis-settings"
    for env_name in (_LEGACY_FRAMEKEEPER_SCREENSHOT_FOLDER_ENV, _LEGACY_FRAMEKEEPER_ARTIFACT_ROOT_ENV):
        configured = os.environ.get(env_name, "").strip()
        if configured:
            return Path(configured).expanduser().resolve(), env_name
    return Path(settings.workspace_dir).expanduser().resolve() / "artifacts" / "screenshot-folder", "default"


def _report_archive_dir() -> tuple[Path, str]:
    configured = settings.report_archive_dir.strip()
    if configured:
        return Path(configured).expanduser().resolve(), "REPORT_ARCHIVE_DIR"
    return Path(settings.workspace_dir).expanduser().resolve() / "artifacts" / "reports", "default"


def _screen_analysis_settings_path() -> Path:
    return Path(settings.workspace_dir).expanduser().resolve() / "screen-analysis-settings.json"


def _daemon_status_file_path() -> Path:
    configured = os.environ.get("SERAPH_DAEMON_STATUS_FILE", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(settings.workspace_dir).expanduser().resolve() / "daemon-status.json"


def _read_daemon_status(max_age_seconds: float = 45) -> dict[str, object]:
    path = _daemon_status_file_path()
    status: dict[str, object] = {
        "state": "unknown",
        "screen_analysis": "unknown",
        "capture_ready": False,
        "alive": False,
        "last_error": None,
        "last_error_kind": None,
        "updated_at": None,
        "status_source": "daemon-status-file",
    }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return status
    if not isinstance(payload, dict):
        return status
    for key in (
        "state",
        "screen_analysis",
        "last_error",
        "last_error_kind",
        "updated_at",
        "active_window",
        "frontmost_app",
        "window_title",
        "last_poll_at",
        "last_capture_at",
        "last_context_post_at",
    ):
        value = payload.get(key)
        if value is None or isinstance(value, str):
            status[key] = value
    status["capture_ready"] = bool(payload.get("capture_ready", False))
    updated_at = status["updated_at"]
    if isinstance(updated_at, str) and status["state"] == "running":
        try:
            parsed = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            status["alive"] = (datetime.now(timezone.utc) - parsed).total_seconds() < max_age_seconds
        except ValueError:
            status["alive"] = False
    return status


def _default_screen_analysis_settings() -> dict[str, object]:
    screen_archive_dir, _ = _screen_archive_dir()
    provider = os.environ.get("SERAPH_SCREEN_ANALYSIS_PROVIDER", "").strip() or "codex-local"
    if provider not in _VALID_SCREEN_ANALYSIS_PROVIDERS:
        provider = "codex-local"
    return {
        "enabled": _env_enabled("SERAPH_SCREEN_ANALYSIS_ENABLED", True),
        "provider": provider,
        "model": os.environ.get("SERAPH_SCREEN_ANALYSIS_MODEL", "").strip()
        or settings.codex_local_model,
        "preserve_captures": _env_enabled("SERAPH_PRESERVE_SCREEN_CAPTURES", True),
        "archive_dir": str(screen_archive_dir),
        "min_seconds_between_captures": max(0, settings.screen_analysis_min_seconds_between_captures),
        "max_daily_captures": max(0, settings.screen_analysis_max_daily_captures),
        "archive_retention_days": max(1, settings.screen_capture_archive_retention_days),
        "archive_max_mb": max(0, settings.screen_capture_archive_max_mb),
    }


def _read_screen_analysis_settings() -> dict[str, object]:
    payload = _default_screen_analysis_settings()
    path = _screen_analysis_settings_path()
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
                "framekeeper_screenshot_folder",
                "framekeeper_artifact_root",
            ):
                if key in loaded:
                    payload[key] = loaded[key]
    provider = str(payload.get("provider") or "codex-local")
    if provider not in _VALID_SCREEN_ANALYSIS_PROVIDERS:
        provider = "codex-local"
    payload["provider"] = provider
    payload["enabled"] = bool(payload.get("enabled"))
    payload["preserve_captures"] = bool(payload.get("preserve_captures"))
    payload["model"] = str(payload.get("model") or "")
    payload["archive_dir"] = str(Path(str(payload.get("archive_dir") or "")).expanduser().resolve())
    screenshot_folder = str(
        payload.get("screenshot_folder")
        or payload.get("framekeeper_screenshot_folder")
        or payload.get("framekeeper_artifact_root")
        or ""
    ).strip()
    if screenshot_folder:
        normalized_folder = str(Path(screenshot_folder).expanduser().resolve())
        payload["screenshot_folder"] = normalized_folder
        payload.pop("framekeeper_screenshot_folder", None)
        payload.pop("framekeeper_artifact_root", None)
    else:
        payload.pop("screenshot_folder", None)
        payload.pop("framekeeper_screenshot_folder", None)
        payload.pop("framekeeper_artifact_root", None)
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


def _write_screen_analysis_settings(payload: dict[str, object]) -> None:
    path = _screen_analysis_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    path.chmod(0o600)


def _normalize_screenshot_folder_for_save(configured_root: str) -> str:
    candidate = Path(configured_root).expanduser()
    if str(candidate) and not candidate.is_absolute():
        raise HTTPException(status_code=422, detail="screenshot_folder must be an absolute path")
    if ".." in candidate.parts:
        raise HTTPException(status_code=422, detail="screenshot_folder must not contain '..' path traversal components")
    normalized = candidate.resolve()
    from src.observer.screenshot_folder_source import ScreenshotFolderImageError, validate_screenshot_folder_root

    try:
        validate_screenshot_folder_root(normalized)
    except ScreenshotFolderImageError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return str(normalized)


def _screen_artifact_summary(archive_dir: Path) -> dict[str, object]:
    if not archive_dir.exists():
        return {"artifact_count": 0, "last_artifact_at": None}
    try:
        image_mtimes = []
        for path in archive_dir.rglob("*.png"):
            if not path.is_file():
                continue
            try:
                image_mtimes.append(path.stat().st_mtime)
            except OSError as exc:
                logger.warning("Screen artifact stat skipped: %s", exc)
    except OSError as exc:
        logger.warning("Screen artifact filesystem summary failed: %s", exc)
        return {"artifact_count": 0, "last_artifact_at": None}
    if not image_mtimes:
        return {"artifact_count": 0, "last_artifact_at": None}
    latest_mtime = max(image_mtimes)
    return {
        "artifact_count": len(image_mtimes),
        "last_artifact_at": datetime.fromtimestamp(latest_mtime, timezone.utc).isoformat(),
    }


def _screenshot_folder_summary(root: Path) -> dict[str, object]:
    if not root.exists():
        return {
            "status": "not_found",
            "image_count": 0,
            "last_image_at": None,
            "exists": False,
            "readable": False,
        }
    if not root.is_dir():
        return {
            "status": "invalid_root",
            "image_count": 0,
            "last_image_at": None,
            "exists": True,
            "readable": False,
        }
    image_mtimes: list[float] = []
    try:
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg"}:
                image_mtimes.append(path.stat().st_mtime)
    except OSError as exc:
        logger.warning("Screenshot folder source summary failed: %s", exc)
        return {
            "status": "read_error",
            "image_count": 0,
            "last_image_at": None,
            "exists": True,
            "readable": False,
        }
    return {
        "status": "ready" if image_mtimes else "empty",
        "image_count": len(image_mtimes),
        "last_image_at": datetime.fromtimestamp(max(image_mtimes), timezone.utc).isoformat()
        if image_mtimes
        else None,
        "exists": True,
        "readable": True,
    }


def _report_receipt_summary(report_dir: Path) -> dict[str, object]:
    receipts_dir = report_dir / "receipts"
    if not receipts_dir.exists():
        return {"receipt_count": 0, "last_receipt_at": None}
    receipt_mtimes = []
    try:
        for path in receipts_dir.rglob("*.json"):
            if path.is_file():
                receipt_mtimes.append(path.stat().st_mtime)
    except OSError as exc:
        logger.warning("Report receipt filesystem summary failed: %s", exc)
        return {"receipt_count": 0, "last_receipt_at": None}
    if not receipt_mtimes:
        return {"receipt_count": 0, "last_receipt_at": None}
    return {
        "receipt_count": len(receipt_mtimes),
        "last_receipt_at": datetime.fromtimestamp(max(receipt_mtimes), timezone.utc).isoformat(),
    }


def _archive_dir_status(path: Path) -> dict[str, object]:
    creation_error = None
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
    except OSError as exc:
        creation_error = str(exc)
    mode_private = False
    if path.exists() and path.is_dir():
        try:
            mode_private = stat.S_IMODE(path.stat().st_mode) == 0o700
        except OSError:
            mode_private = False
    return {
        "exists": path.exists(),
        "writable": path.is_dir() and os.access(path, os.W_OK),
        "private": mode_private,
        "creation_error": creation_error,
    }


@router.get("/settings/interruption-mode")
async def get_interruption_mode():
    """Get current interruption mode, attention budget, and user state."""
    ctx = context_manager.get_context()
    return {
        "mode": ctx.interruption_mode,
        "attention_budget_remaining": ctx.attention_budget_remaining,
        "user_state": ctx.user_state,
    }


@router.put("/settings/interruption-mode")
async def set_interruption_mode(body: InterruptionModeRequest):
    """Update interruption mode. Resets attention budget to mode default."""
    # Validate mode
    valid_modes = {m.value for m in InterruptionMode}
    if body.mode not in valid_modes:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid mode '{body.mode}'. Must be one of: {', '.join(sorted(valid_modes))}",
        )

    # Update in-memory context
    context_manager.update_interruption_mode(body.mode)

    # Persist to DB
    async with get_db() as db:
        result = await db.execute(
            select(UserProfile).where(UserProfile.id == "singleton")
        )
        profile = result.scalars().first()
        if profile:
            profile.interruption_mode = body.mode
            profile.updated_at = datetime.now(timezone.utc)
            db.add(profile)

    ctx = context_manager.get_context()
    return {
        "mode": ctx.interruption_mode,
        "attention_budget_remaining": ctx.attention_budget_remaining,
        "user_state": ctx.user_state,
    }


@router.get("/settings/capture-mode")
async def get_capture_mode():
    """Get current capture mode setting."""
    ctx = context_manager.get_context()
    return {"mode": ctx.capture_mode}


@router.put("/settings/capture-mode")
async def set_capture_mode(body: CaptureModeRequest):
    """Update capture mode (on_switch | balanced | detailed)."""
    if body.mode not in _VALID_CAPTURE_MODES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid mode '{body.mode}'. Must be one of: {', '.join(sorted(_VALID_CAPTURE_MODES))}",
        )

    # Update in-memory context
    context_manager.update_capture_mode(body.mode)

    # Persist to DB
    async with get_db() as db:
        result = await db.execute(
            select(UserProfile).where(UserProfile.id == "singleton")
        )
        profile = result.scalars().first()
        if profile:
            profile.capture_mode = body.mode
            profile.updated_at = datetime.now(timezone.utc)
            db.add(profile)

    return {"mode": body.mode}


@router.get("/settings/screen-analysis")
async def get_screen_analysis_settings():
    """Return runtime screen-analysis settings consumed by the native daemon."""
    payload = _read_screen_analysis_settings()
    ctx = context_manager.get_context()
    daemon_status = _read_daemon_status()
    payload.update(
        {
            "capture_mode": ctx.capture_mode,
            "cadence_seconds": 60 if ctx.capture_mode == "detailed" else 300 if ctx.capture_mode == "balanced" else None,
            "daemon_connected": context_manager.is_daemon_connected(),
            "daemon_alive": bool(daemon_status["alive"]),
            "artifact_count": 0,
            "last_artifact_at": None,
        }
    )
    return payload


@router.put("/settings/screen-analysis")
async def set_screen_analysis_settings(body: ScreenAnalysisSettingsRequest):
    """Persist runtime screen-analysis settings for the native daemon."""
    payload = _read_screen_analysis_settings()
    if body.enabled is not None:
        payload["enabled"] = body.enabled
    if body.provider is not None:
        if body.provider not in _VALID_SCREEN_ANALYSIS_PROVIDERS:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Invalid provider '{body.provider}'. Must be one of: "
                    f"{', '.join(sorted(_VALID_SCREEN_ANALYSIS_PROVIDERS))}"
                ),
            )
        payload["provider"] = body.provider
    if body.model is not None:
        payload["model"] = body.model.strip()
    if body.preserve_captures is not None:
        payload["preserve_captures"] = body.preserve_captures
    if body.archive_dir is not None:
        archive_dir = Path(body.archive_dir).expanduser().resolve()
        archive_status = _archive_dir_status(archive_dir)
        if (
            archive_status["creation_error"]
            or not archive_status["exists"]
            or not archive_status["writable"]
            or not archive_status["private"]
        ):
            raise HTTPException(status_code=422, detail="Screen archive directory must be private and writable")
        payload["archive_dir"] = str(archive_dir)
    if body.screenshot_folder is not None:
        configured_root = body.screenshot_folder.strip()
        if configured_root:
            payload["screenshot_folder"] = _normalize_screenshot_folder_for_save(configured_root)
            payload.pop("framekeeper_screenshot_folder", None)
            payload.pop("framekeeper_artifact_root", None)
        else:
            payload.pop("screenshot_folder", None)
            payload.pop("framekeeper_screenshot_folder", None)
            payload.pop("framekeeper_artifact_root", None)
    for field_name in (
        "min_seconds_between_captures",
        "max_daily_captures",
        "archive_max_mb",
    ):
        value = getattr(body, field_name)
        if value is not None:
            if value < 0:
                raise HTTPException(status_code=422, detail=f"{field_name} must be >= 0")
            payload[field_name] = value
    if body.archive_retention_days is not None:
        if body.archive_retention_days < 1:
            raise HTTPException(status_code=422, detail="archive_retention_days must be >= 1")
        payload["archive_retention_days"] = body.archive_retention_days
    _write_screen_analysis_settings(payload)
    return await get_screen_analysis_settings()


@router.get("/settings/artifact-storage")
async def get_artifact_storage_settings():
    """Return operator-visible evidence/report archive configuration."""
    report_archive_dir, report_archive_source = _report_archive_dir()
    report_dir_status = _archive_dir_status(report_archive_dir)
    report_receipts = _report_receipt_summary(report_archive_dir)
    screenshot_folder, screenshot_folder_source = _screenshot_folder()
    screenshot_source = _screenshot_folder_summary(screenshot_folder)
    screen_analysis = await get_screen_analysis_settings()
    screen_archive_dir = Path(str(screen_analysis["archive_dir"]))
    screen_dir_status = _archive_dir_status(screen_archive_dir)
    screen_summary = _screen_artifact_summary(screen_archive_dir)
    return {
        "screen": {
            "analysis_enabled": screen_analysis["enabled"],
            "provider": screen_analysis["provider"],
            "model": screen_analysis["model"],
            "capture_mode": screen_analysis["capture_mode"],
            "cadence_seconds": screen_analysis["cadence_seconds"],
            "daemon_connected": screen_analysis["daemon_connected"],
            "daemon_alive": screen_analysis["daemon_alive"],
            "artifact_count": screen_summary["artifact_count"],
            "last_artifact_at": screen_summary["last_artifact_at"],
            "budget": {
                "min_seconds_between_captures": screen_analysis["min_seconds_between_captures"],
                "max_daily_captures": screen_analysis["max_daily_captures"],
                "archive_retention_days": screen_analysis["archive_retention_days"],
                "archive_max_mb": screen_analysis["archive_max_mb"],
            },
            "preservation_enabled": screen_analysis["preserve_captures"],
            "archive_dir": str(screen_analysis["archive_dir"]),
            "archive_dir_source": "screen-analysis-settings",
            **screen_dir_status,
            "stored_artifacts": ["image", "provider_output", "analysis_json"],
            "inspection_endpoint": "/api/observer/screen-artifacts",
            "inspection_visibility": "localhost_only",
            "daemon_status": _read_daemon_status(),
            "control_env": {
                "enabled": "SERAPH_PRESERVE_SCREEN_CAPTURES",
                "archive_dir": "SERAPH_SCREEN_CAPTURE_ARCHIVE_DIR or SCREEN_CAPTURE_ARCHIVE_DIR",
            },
        },
        "screenshot_folder": {
            "enabled": True,
            "provider": "screenshot_folder",
            "path": str(screenshot_folder),
            "path_source": screenshot_folder_source,
            "image_count": screenshot_source["image_count"],
            "last_image_at": screenshot_source["last_image_at"],
            "status": screenshot_source["status"],
            "exists": screenshot_source["exists"],
            "readable": screenshot_source["readable"],
            "stored_artifacts": ["image"],
            "auto_ingest_enabled": settings.screenshot_folder_ingest_enabled,
            "auto_ingest_interval_min": settings.screenshot_folder_ingest_interval_min,
            "auto_ingest_limit": settings.screenshot_folder_ingest_limit,
            "scan_endpoint": "/api/observer/screenshot-folder/scan",
            "inspection_endpoint": "/api/observer/screen-artifacts",
            "inspection_visibility": "localhost_only",
            "control_env": {
                "path": _SCREENSHOT_FOLDER_ENV,
                "auto_ingest_enabled": "SCREENSHOT_FOLDER_INGEST_ENABLED",
                "auto_ingest_interval": "SCREENSHOT_FOLDER_INGEST_INTERVAL_MIN",
                "auto_ingest_limit": "SCREENSHOT_FOLDER_INGEST_LIMIT",
            },
        },
        "reports": {
            "enabled": settings.end_of_day_report_enabled,
            "hour": settings.end_of_day_report_hour,
            "analysis_provider": "llm" if settings.end_of_day_report_llm_enabled else "deterministic-local",
            "archive_dir": str(report_archive_dir),
            "archive_dir_source": report_archive_source,
            **report_dir_status,
            "stored_artifacts": ["report_text", "report_json"],
            "receipt_count": report_receipts["receipt_count"],
            "last_receipt_at": report_receipts["last_receipt_at"],
            "control_env": {
                "archive_dir": "REPORT_ARCHIVE_DIR",
                "enabled": "END_OF_DAY_REPORT_ENABLED",
                "llm": "END_OF_DAY_REPORT_LLM_ENABLED",
            },
        },
        "email": {
            "enabled": settings.email_reports_enabled,
            "preview_required": settings.email_reports_preview_required,
            "smtp_configured": bool(settings.smtp_host.strip()),
            "recipient_configured": bool(settings.email_reports_to.strip()),
            "allowlist_configured": bool(settings.email_reports_to_allowlist.strip()),
            "sender_configured": bool(settings.email_reports_from.strip()),
            "control_env": {
                "enabled": "EMAIL_REPORTS_ENABLED",
                "preview_required": "EMAIL_REPORTS_PREVIEW_REQUIRED",
                "smtp_host": "SMTP_HOST",
                "recipient": "EMAIL_REPORTS_TO",
                "allowlist": "EMAIL_REPORTS_TO_ALLOWLIST",
            },
        },
    }


@router.post("/settings/end-of-day-report/manual")
async def run_manual_end_of_day_report(body: ManualReportRequest):
    """Build/store a manual end-of-day report preview or operator-acknowledged send."""
    from src.scheduler.jobs.end_of_day_goal_report import run_manual_end_of_day_goal_report

    return await run_manual_end_of_day_goal_report(
        send_email=body.send_email,
        preview_acknowledged=body.preview_acknowledged,
        report_day=body.report_date,
    )


@router.post("/settings/end-of-day-report/test-email")
async def send_end_of_day_report_test_email():
    """Send a guarded test email using the configured report email transport."""
    from src.scheduler.jobs.end_of_day_goal_report import send_end_of_day_report_test_email

    return await send_end_of_day_report_test_email()


@router.get("/settings/tool-policy-mode")
async def get_tool_policy_mode():
    """Get current tool policy mode."""
    ctx = context_manager.get_context()
    return {"mode": ctx.tool_policy_mode}


@router.put("/settings/tool-policy-mode")
async def set_tool_policy_mode(body: ToolPolicyModeRequest):
    """Update tool policy mode (safe | balanced | full)."""
    if body.mode not in _VALID_TOOL_POLICY_MODES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Invalid mode '{body.mode}'. Must be one of: "
                f"{', '.join(sorted(_VALID_TOOL_POLICY_MODES))}"
            ),
        )

    context_manager.update_tool_policy_mode(body.mode)

    async with get_db() as db:
        result = await db.execute(
            select(UserProfile).where(UserProfile.id == "singleton")
        )
        profile = result.scalars().first()
        if profile:
            profile.tool_policy_mode = body.mode
            profile.updated_at = datetime.now(timezone.utc)
            db.add(profile)

    return {"mode": body.mode}


@router.get("/settings/approval-mode")
async def get_approval_mode():
    """Get current approval mode for high-risk actions."""
    ctx = context_manager.get_context()
    return {"mode": ctx.approval_mode}


@router.get("/settings/mcp-policy-mode")
async def get_mcp_policy_mode():
    """Get current MCP access policy mode."""
    ctx = context_manager.get_context()
    return {"mode": ctx.mcp_policy_mode}


@router.put("/settings/mcp-policy-mode")
async def set_mcp_policy_mode(body: McpPolicyModeRequest):
    """Update MCP policy mode (disabled | approval | full)."""
    if body.mode not in _VALID_MCP_POLICY_MODES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Invalid mode '{body.mode}'. Must be one of: "
                f"{', '.join(sorted(_VALID_MCP_POLICY_MODES))}"
            ),
        )

    context_manager.update_mcp_policy_mode(body.mode)

    async with get_db() as db:
        result = await db.execute(
            select(UserProfile).where(UserProfile.id == "singleton")
        )
        profile = result.scalars().first()
        if profile:
            profile.mcp_policy_mode = body.mode
            profile.updated_at = datetime.now(timezone.utc)
            db.add(profile)

    return {"mode": body.mode}


@router.put("/settings/approval-mode")
async def set_approval_mode(body: ApprovalModeRequest):
    """Update approval mode (off | high_risk)."""
    if body.mode not in _VALID_APPROVAL_MODES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Invalid mode '{body.mode}'. Must be one of: "
                f"{', '.join(sorted(_VALID_APPROVAL_MODES))}"
            ),
        )

    context_manager.update_approval_mode(body.mode)

    async with get_db() as db:
        result = await db.execute(
            select(UserProfile).where(UserProfile.id == "singleton")
        )
        profile = result.scalars().first()
        if profile:
            profile.approval_mode = body.mode
            profile.updated_at = datetime.now(timezone.utc)
            db.add(profile)

    return {"mode": body.mode}
