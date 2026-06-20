"""Settings API — runtime mode management."""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
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


class ToolPolicyModeRequest(BaseModel):
    mode: str


class ApprovalModeRequest(BaseModel):
    mode: str


class McpPolicyModeRequest(BaseModel):
    mode: str


_VALID_CAPTURE_MODES = {"on_switch", "balanced", "detailed"}
_VALID_TOOL_POLICY_MODES = set(TOOL_POLICY_MODES)
_VALID_MCP_POLICY_MODES = set(MCP_POLICY_MODES)
_VALID_APPROVAL_MODES = {"off", "high_risk"}
_TRUE_VALUES = {"1", "true", "yes", "on"}


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


def _report_archive_dir() -> tuple[Path, str]:
    configured = settings.report_archive_dir.strip()
    if configured:
        return Path(configured).expanduser().resolve(), "REPORT_ARCHIVE_DIR"
    return Path(settings.workspace_dir).expanduser().resolve() / "artifacts" / "reports", "default"


def _archive_dir_status(path: Path) -> dict[str, object]:
    creation_error = None
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
    except OSError as exc:
        creation_error = str(exc)
    return {
        "exists": path.exists(),
        "writable": path.is_dir() and os.access(path, os.W_OK),
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


@router.get("/settings/artifact-storage")
async def get_artifact_storage_settings():
    """Return operator-visible evidence/report archive configuration."""
    screen_archive_dir, screen_archive_source = _screen_archive_dir()
    report_archive_dir, report_archive_source = _report_archive_dir()
    screen_dir_status = _archive_dir_status(screen_archive_dir)
    report_dir_status = _archive_dir_status(report_archive_dir)
    return {
        "screen": {
            "preservation_enabled": _env_enabled("SERAPH_PRESERVE_SCREEN_CAPTURES", False),
            "archive_dir": str(screen_archive_dir),
            "archive_dir_source": screen_archive_source,
            **screen_dir_status,
            "stored_artifacts": ["image", "provider_output", "analysis_json"],
            "inspection_endpoint": "/api/observer/screen-artifacts",
            "inspection_visibility": "localhost_only",
            "control_env": {
                "enabled": "SERAPH_PRESERVE_SCREEN_CAPTURES",
                "archive_dir": "SERAPH_SCREEN_CAPTURE_ARCHIVE_DIR or SCREEN_CAPTURE_ARCHIVE_DIR",
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
            "control_env": {
                "enabled": "EMAIL_REPORTS_ENABLED",
                "preview_required": "EMAIL_REPORTS_PREVIEW_REQUIRED",
                "smtp_host": "SMTP_HOST",
                "recipient": "EMAIL_REPORTS_TO",
                "allowlist": "EMAIL_REPORTS_TO_ALLOWLIST",
            },
        },
    }


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
