"""Settings API — runtime mode management."""

import asyncio
import json
import logging
import os
import stat
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, update
from sqlmodel import col, select

from config.settings import settings
from src.db.engine import get_session as get_db
from src.db.models import MemoryEpisode, ScreenObservation, UserProfile
from src.local_runtime_profile_verifier import (
    PROFILE_VERIFIER_VERSION,
    latest_local_runtime_profile_proof,
    local_runtime_profile_receipt_dir,
)
from src.local_runtime_profiles import local_runtime_profile_statuses
from src.model_fabric.configuration import effective_workload_policy
from src.security.trust_contract import EgressClass
from src.vlm_runtime import (
    deferred_vlm_live_probe,
    effective_vlm_base_url,
    effective_vlm_chat_api_base,
    effective_vlm_status,
)
from src.observer.manager import context_manager
from src.observer.screen_analysis_settings import (
    SCREENSHOT_FOLDER_ENV,
    effective_screen_analysis_model,
    effective_screen_analysis_provider,
    normalize_openrouter_model_identifier,
    read_screen_analysis_settings,
    screen_analysis_settings_path,
    write_screen_analysis_settings,
)
from src.observer.screenshot_semantic_analysis import semantic_analysis_status_from_details
from src.observer.user_state import InterruptionMode
from src.tools.policy import MCP_POLICY_MODES, TOOL_POLICY_MODES

logger = logging.getLogger(__name__)
router = APIRouter()


class InterruptionModeRequest(BaseModel):
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


class ScreenshotFolderPickResponse(BaseModel):
    screenshot_folder: str
    screenshot_folder_source: str


class ToolPolicyModeRequest(BaseModel):
    mode: str


class ApprovalModeRequest(BaseModel):
    mode: str


class McpPolicyModeRequest(BaseModel):
    mode: str


# Active screenshot semantic analysis is OpenRouter-only. Retained settings
# from the historical local/Apple routes are normalized away by the reader.
_VALID_SCREEN_ANALYSIS_PROVIDERS = frozenset({"", "openrouter"})
_VALID_TOOL_POLICY_MODES = set(TOOL_POLICY_MODES)
_VALID_MCP_POLICY_MODES = set(MCP_POLICY_MODES)
_VALID_APPROVAL_MODES = {"off", "high_risk"}
_SCREENSHOT_FOLDER_ENV = SCREENSHOT_FOLDER_ENV
_SCREENSHOT_DIGEST_TOOL_NAME = "screenshot_observation_digest"
_SCREENSHOT_STALE_STATUSES = {"source_missing", "stale_root"}
_SCREENSHOT_FOLDER_SUMMARY_TIMEOUT_S = 0.25
_SCREENSHOT_PIPELINE_SUMMARY_TIMEOUT_S = 0.25
_REPORT_RECEIPT_SUMMARY_TIMEOUT_S = 0.25
_LOCAL_RUNTIME_PROOF_SUMMARY_TIMEOUT_S = 0.25


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
    settings_configured = str(payload.get("screenshot_folder") or "").strip()
    if settings_configured:
        return Path(settings_configured).expanduser().resolve(), "screen-analysis-settings"
    return Path(settings.workspace_dir).expanduser().resolve() / "artifacts" / "screenshot-folder", "default"


def _report_archive_dir() -> tuple[Path, str]:
    configured = settings.report_archive_dir.strip()
    if configured:
        return Path(configured).expanduser().resolve(), "REPORT_ARCHIVE_DIR"
    return Path(settings.workspace_dir).expanduser().resolve() / "artifacts" / "reports", "default"


def _screen_analysis_settings_path() -> Path:
    return screen_analysis_settings_path()


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


def _read_screen_analysis_settings() -> dict[str, object]:
    return read_screen_analysis_settings()


def _write_screen_analysis_settings(payload: dict[str, object]) -> None:
    write_screen_analysis_settings(payload)


def _is_local_request(request: Request) -> bool:
    client_host = request.client.host if request.client is not None else ""
    return client_host in {"127.0.0.1", "::1", "localhost", "testclient"}


async def _choose_screenshot_folder_with_native_dialog() -> str:
    try:
        process = await asyncio.create_subprocess_exec(
            "osascript",
            "-e",
            'POSIX path of (choose folder with prompt "Choose Seraph screenshot folder")',
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        raise HTTPException(status_code=503, detail="Native folder picker is unavailable") from exc
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=90)
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.communicate()
        raise HTTPException(status_code=504, detail="Native folder picker timed out") from exc
    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip() or "Native folder picker was cancelled"
        raise HTTPException(status_code=400, detail=detail)
    selected = stdout.decode("utf-8", errors="replace").strip()
    if not selected:
        raise HTTPException(status_code=400, detail="Native folder picker returned no folder")
    return selected


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
    from src.observer.screenshot_folder_source import SUPPORTED_IMAGE_EXTENSIONS, _capture_timestamp

    resolved_root = root.resolve()
    if not root.exists():
        return {
            "status": "not_found",
            "image_count": 0,
            "last_image_at": None,
            "last_image_at_source": None,
            "exists": False,
            "readable": False,
        }
    if not root.is_dir():
        return {
            "status": "invalid_root",
            "image_count": 0,
            "last_image_at": None,
            "last_image_at_source": None,
            "exists": True,
            "readable": False,
        }
    image_count = 0
    latest_captured_at: datetime | None = None
    latest_captured_at_source: str | None = None
    try:
        for path in root.rglob("*"):
            if path.is_file() and path.suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS:
                image_count += 1
                captured_at, captured_at_source = _capture_timestamp(path, resolved_root)
                if latest_captured_at is None or captured_at > latest_captured_at:
                    latest_captured_at = captured_at
                    latest_captured_at_source = captured_at_source
    except OSError as exc:
        logger.warning("Screenshot folder source summary failed: %s", exc)
        return {
            "status": "read_error",
            "image_count": 0,
            "last_image_at": None,
            "last_image_at_source": None,
            "exists": True,
            "readable": False,
        }
    return {
        "status": "ready" if image_count else "empty",
        "image_count": image_count,
        "last_image_at": latest_captured_at.isoformat() if latest_captured_at is not None else None,
        "last_image_at_source": latest_captured_at_source,
        "exists": True,
        "readable": True,
        "summary_status": "ready",
        "summary_failure": None,
    }


def _fallback_screenshot_folder_summary(root: Path, *, status: str) -> dict[str, object]:
    exists = root.exists()
    readable = root.is_dir() and os.access(root, os.R_OK)
    return {
        "status": status,
        "image_count": 0,
        "last_image_at": None,
        "last_image_at_source": None,
        "exists": exists,
        "readable": readable,
        "summary_status": "partial",
        "summary_failure": status,
    }


async def _screenshot_folder_summary_fast(root: Path) -> dict[str, object]:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_screenshot_folder_summary, root),
            timeout=_SCREENSHOT_FOLDER_SUMMARY_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.warning("Screenshot folder filesystem summary timed out: %s", root)
        return _fallback_screenshot_folder_summary(root, status="summary_timeout")
    except OSError as exc:
        logger.warning("Screenshot folder filesystem summary failed: %s", exc)
        return _fallback_screenshot_folder_summary(root, status="read_error")


def _screen_observation_details(details_json: str | None) -> list[object]:
    try:
        payload = json.loads(details_json or "[]")
    except json.JSONDecodeError:
        return []
    return payload if isinstance(payload, list) else []


def _utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _is_screenshot_folder_observation(observation: ScreenObservation) -> bool:
    if observation.app_name == "Screenshot Folder":
        return True
    for item in _screen_observation_details(observation.details_json):
        if not (isinstance(item, str) and item.startswith("capture_artifacts:")):
            continue
        try:
            artifacts = json.loads(item.removeprefix("capture_artifacts:"))
        except json.JSONDecodeError:
            continue
        if isinstance(artifacts, dict) and artifacts.get("provider") == "screenshot_folder":
            return True
    return False


def _screenshot_capture_artifacts(details: list[object]) -> dict[str, object] | None:
    for item in details:
        if not (isinstance(item, str) and item.startswith("capture_artifacts:")):
            continue
        try:
            artifacts = json.loads(item.removeprefix("capture_artifacts:"))
        except json.JSONDecodeError:
            continue
        if isinstance(artifacts, dict) and artifacts.get("provider") == "screenshot_folder":
            return artifacts
    return None


def _screenshot_root_matches_current(artifacts: dict[str, object] | None, root: Path | None) -> bool:
    if root is None:
        return True
    if not artifacts:
        return False
    try:
        artifact_root_value = str(artifacts.get("screenshot_folder") or "").strip()
        if artifact_root_value:
            return Path(artifact_root_value).expanduser().resolve() == root
        image_path_value = str(artifacts.get("image_path") or "").strip()
        if not image_path_value:
            return False
        return Path(image_path_value).expanduser().resolve().is_relative_to(root)
    except (OSError, RuntimeError):
        return False


def _screenshot_status_from_details(details: list[object]) -> dict[str, object]:
    status = semantic_analysis_status_from_details(details) or {}
    return status if status else {"status": "unknown"}


def _classify_screenshot_observation(
    observation: ScreenObservation,
    *,
    root: Path | None,
) -> tuple[str, dict[str, object], dict[str, object] | None]:
    details = _screen_observation_details(observation.details_json)
    status = _screenshot_status_from_details(details)
    state = str(status.get("status") or "unknown").strip().lower() or "unknown"
    artifacts = _screenshot_capture_artifacts(details)
    if state == "succeeded":
        return "succeeded", status, artifacts
    if state in _SCREENSHOT_STALE_STATUSES:
        return state, status, artifacts
    if artifacts is not None and not _screenshot_root_matches_current(artifacts, root):
        return "stale_root", status, artifacts
    return state, status, artifacts


async def _screenshot_folder_pipeline_summary(root: Path | None = None) -> dict[str, object]:
    from src.observer.screenshot_folder_source import screenshot_folder_persistence_status

    async with get_db() as db:
        base_filters = [
            col(ScreenObservation.blocked) == False,  # noqa: E712
            col(ScreenObservation.app_name) == "Screenshot Folder",
        ]

        status_counts: dict[str, int] = {
            "pending": 0,
            "succeeded": 0,
            "failed": 0,
            "blocked": 0,
            "needs_reanalysis": 0,
            "source_missing": 0,
            "stale_root": 0,
            "unknown": 0,
        }
        current_observation_count = 0
        current_latest_observation_at: datetime | None = None
        latest_analyzed_at: str | None = None
        latest_failure: str | None = None
        visual_detail_payloads: list[str] = []
        classification_result = await db.execute(
            select(ScreenObservation)
            .where(*base_filters)
            .order_by(col(ScreenObservation.timestamp).desc())
        )
        observations = list(classification_result.scalars().all())
        total_observations = len(observations)
        for observation in observations:
            state, status, artifacts = _classify_screenshot_observation(observation, root=root)
            status_counts[state if state in status_counts else "unknown"] += 1
            if state in _SCREENSHOT_STALE_STATUSES:
                continue
            visual_detail_payloads.append(str(observation.details_json or ""))
            if _screenshot_root_matches_current(artifacts, root):
                current_observation_count += 1
                if current_latest_observation_at is None:
                    current_latest_observation_at = observation.timestamp
            if state == "succeeded" and latest_analyzed_at is None:
                recorded_at = status.get("recorded_at")
                if isinstance(recorded_at, str):
                    latest_analyzed_at = recorded_at
            if state in {"failed", "blocked"} and latest_failure is None:
                latest_failure = str(status.get("reason") or "analysis failed")

        visual_runs = _screenshot_visual_run_summary(visual_detail_payloads)
        latest_observation_at = _utc_iso(current_latest_observation_at)

        digest_result = await db.execute(
            select(MemoryEpisode)
            .where(col(MemoryEpisode.source_tool_name) == _SCREENSHOT_DIGEST_TOOL_NAME)
            .order_by(col(MemoryEpisode.observed_at).desc())
            .limit(1)
        )
        latest_digest = digest_result.scalar_one_or_none()
        digest_count_result = await db.execute(
            select(func.count())
            .select_from(MemoryEpisode)
            .where(col(MemoryEpisode.source_tool_name) == _SCREENSHOT_DIGEST_TOOL_NAME)
        )
        digest_count = int(digest_count_result.scalar_one() or 0)

    return {
        "observation_count": current_observation_count if root is not None else total_observations,
        "total_observation_count": total_observations,
        "analysis_status": status_counts,
        "analysis_backlog": status_counts["pending"] + status_counts["needs_reanalysis"] + status_counts["unknown"],
        "analysis_failures": status_counts["failed"],
        "analysis_blocked": status_counts["blocked"],
        "stale_count": status_counts["source_missing"] + status_counts["stale_root"],
        "source_missing_count": status_counts["source_missing"],
        "stale_root_count": status_counts["stale_root"],
        "visual_run_count": visual_runs["visual_run_count"],
        "visual_suppressed_count": visual_runs["visual_suppressed_count"],
        "persistence": screenshot_folder_persistence_status(),
        "latest_observation_at": latest_observation_at,
        "latest_analyzed_at": latest_analyzed_at,
        "latest_failure": latest_failure,
        "digest_count": digest_count,
        "latest_digest_at": _utc_iso(latest_digest.observed_at) if latest_digest is not None else None,
        "metadata_status": "ready",
        "metadata_failure": None,
    }


def _empty_screenshot_folder_pipeline_summary(*, latest_failure: str | None = None) -> dict[str, object]:
    from src.observer.screenshot_folder_source import screenshot_folder_persistence_status

    return {
        "observation_count": 0,
        "analysis_status": {
            "pending": 0,
            "succeeded": 0,
            "failed": 0,
            "needs_reanalysis": 0,
            "source_missing": 0,
            "stale_root": 0,
            "unknown": 0,
        },
        "total_observation_count": 0,
        "analysis_backlog": 0,
        "analysis_failures": 0,
        "stale_count": 0,
        "source_missing_count": 0,
        "stale_root_count": 0,
        "visual_run_count": 0,
        "visual_suppressed_count": 0,
        "persistence": screenshot_folder_persistence_status(),
        "latest_observation_at": None,
        "latest_analyzed_at": None,
        "latest_failure": latest_failure,
        "digest_count": 0,
        "latest_digest_at": None,
        "metadata_status": "partial",
        "metadata_failure": latest_failure,
    }


def _screenshot_visual_run_summary(details_payloads: list[str]) -> dict[str, int]:
    visual_run_count = 0
    visual_suppressed_count = 0
    for payload in details_payloads:
        for item in _screen_observation_details(payload):
            if not (isinstance(item, str) and item.startswith("screenshot_visual_run:")):
                continue
            try:
                visual_run = json.loads(item.removeprefix("screenshot_visual_run:"))
            except json.JSONDecodeError:
                continue
            if not isinstance(visual_run, dict):
                continue
            visual_run_count += 1
            try:
                visual_suppressed_count += max(int(visual_run.get("suppressed_count") or 0), 0)
            except (TypeError, ValueError):
                continue
    return {
        "visual_run_count": visual_run_count,
        "visual_suppressed_count": visual_suppressed_count,
    }


async def _screenshot_folder_pipeline_summary_fast(root: Path | None = None) -> dict[str, object]:
    try:
        return await asyncio.wait_for(
            _screenshot_folder_pipeline_summary(root),
            timeout=_SCREENSHOT_PIPELINE_SUMMARY_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.warning("Screenshot folder pipeline summary timed out")
        return _empty_screenshot_folder_pipeline_summary(latest_failure="analysis metadata timed out")
    except Exception as exc:
        logger.warning("Screenshot folder pipeline summary failed: %s", exc)
        return _empty_screenshot_folder_pipeline_summary(latest_failure="analysis metadata unavailable")


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


async def _report_receipt_summary_fast(report_dir: Path) -> dict[str, object]:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_report_receipt_summary, report_dir),
            timeout=_REPORT_RECEIPT_SUMMARY_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.warning("Report receipt filesystem summary timed out: %s", report_dir)
        return {"receipt_count": 0, "last_receipt_at": None}
    except OSError as exc:
        logger.warning("Report receipt filesystem summary failed: %s", exc)
        return {"receipt_count": 0, "last_receipt_at": None}


def _local_runtime_profile_receipt_dir() -> Path:
    return local_runtime_profile_receipt_dir()


def _local_runtime_profile_proof_summary() -> dict[str, object]:
    receipts_dir = _local_runtime_profile_receipt_dir()
    summary: dict[str, object] = {
        "schema_version": PROFILE_VERIFIER_VERSION,
        "receipt_count": 0,
        "last_receipt_at": None,
        "last_receipt_sha256": None,
        "last_receipt_path": None,
        "status": "missing",
        "per_request_reasoning_control": "unverified",
        "safe_for_single_backend_profile_routing": False,
        "notes": [],
    }
    try:
        receipts = sorted((path for path in receipts_dir.glob("*.json") if path.is_file()))
    except OSError as exc:
        logger.warning("Local runtime profile receipt summary failed: %s", exc)
        summary["status"] = "read_error"
        summary["notes"] = ["profile receipt directory could not be read"]
        return summary
    summary["receipt_count"] = len(receipts)
    if not receipts:
        return summary

    proof = latest_local_runtime_profile_proof(
        expected_base_url=effective_vlm_chat_api_base() or effective_vlm_base_url(),
        expected_model=settings.local_model or settings.local_vlm_model or settings.default_model,
    )
    summary.update(
        {
            "last_receipt_at": proof.get("finished_at"),
            "last_receipt_sha256": proof.get("sha256"),
            "last_receipt_path": proof.get("receipt_path"),
            "status": proof.get("status", "unsafe"),
            "per_request_reasoning_control": str(proof.get("per_request_reasoning_control") or "unverified"),
            "safe_for_single_backend_profile_routing": bool(
                proof.get("safe_for_single_backend_profile_routing") is True
            ),
            "notes": proof.get("notes") if isinstance(proof.get("notes"), list) else [],
            "base_url": proof.get("base_url"),
            "model": proof.get("model"),
        }
    )
    return summary


async def _local_runtime_profile_proof_summary_fast() -> dict[str, object]:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_local_runtime_profile_proof_summary),
            timeout=_LOCAL_RUNTIME_PROOF_SUMMARY_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.warning("Local runtime profile receipt summary timed out")
        return {
            "schema_version": PROFILE_VERIFIER_VERSION,
            "receipt_count": 0,
            "last_receipt_at": None,
            "last_receipt_sha256": None,
            "last_receipt_path": None,
            "status": "summary_timeout",
            "per_request_reasoning_control": "unverified",
            "safe_for_single_backend_profile_routing": False,
            "notes": ["profile receipt summary timed out"],
        }
    except Exception as exc:
        logger.warning("Local runtime profile receipt summary failed: %s", exc)
        return {
            "schema_version": PROFILE_VERIFIER_VERSION,
            "receipt_count": 0,
            "last_receipt_at": None,
            "last_receipt_sha256": None,
            "last_receipt_path": None,
            "status": "summary_unavailable",
            "per_request_reasoning_control": "unverified",
            "safe_for_single_backend_profile_routing": False,
            "notes": ["profile receipt summary unavailable"],
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


@router.get("/settings/screen-analysis")
async def get_screen_analysis_settings():
    """Return screenshot analysis settings for local image-folder ingestion."""
    return _read_screen_analysis_settings()


@router.put("/settings/screen-analysis")
async def set_screen_analysis_settings(body: ScreenAnalysisSettingsRequest):
    """Persist screenshot analysis settings for local image-folder ingestion."""
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
        model = normalize_openrouter_model_identifier(body.model)
        if body.model.strip() and not model:
            raise HTTPException(
                status_code=422,
                detail="Screenshot analysis model must be an OpenRouter-qualified id such as openrouter/provider/model",
            )
        payload["model"] = model
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
        else:
            payload.pop("screenshot_folder", None)
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


@router.post("/settings/screen-analysis/screenshot-folder/pick", response_model=ScreenshotFolderPickResponse)
async def pick_screenshot_folder(request: Request):
    """Open a local native folder picker and persist the selected screenshot folder."""
    if not _is_local_request(request):
        raise HTTPException(status_code=403, detail="Screenshot folder picker is only available from localhost")
    if os.environ.get(_SCREENSHOT_FOLDER_ENV, "").strip():
        raise HTTPException(status_code=409, detail="Screenshot folder is locked by SERAPH_SCREENSHOT_FOLDER")
    selected = await _choose_screenshot_folder_with_native_dialog()
    normalized = _normalize_screenshot_folder_for_save(selected)
    payload = _read_screen_analysis_settings()
    payload["screenshot_folder"] = normalized
    payload["screenshot_folder_source"] = "screen-analysis-settings"
    _write_screen_analysis_settings(payload)
    return ScreenshotFolderPickResponse(
        screenshot_folder=normalized,
        screenshot_folder_source="screen-analysis-settings",
    )


@router.post("/settings/screen-analysis/screenshot-folder/clear-stale")
async def clear_stale_screenshot_folder_observations(request: Request):
    """Archive stale incomplete screenshot-folder rows without deleting analyzed history."""
    if not _is_local_request(request):
        raise HTTPException(status_code=403, detail="Screenshot folder cleanup is only available from localhost")
    screenshot_folder, _ = _screenshot_folder()
    async with get_db() as db:
        result = await db.execute(
            select(ScreenObservation)
            .where(col(ScreenObservation.blocked) == False)  # noqa: E712
            .where(col(ScreenObservation.app_name) == "Screenshot Folder")
            .order_by(col(ScreenObservation.timestamp).desc())
        )
        stale_ids: list[str] = []
        source_missing = 0
        stale_root = 0
        for observation in result.scalars().all():
            state, _status, _artifacts = _classify_screenshot_observation(observation, root=screenshot_folder)
            if state == "succeeded":
                continue
            if state == "source_missing":
                source_missing += 1
                stale_ids.append(observation.id)
            elif state == "stale_root":
                stale_root += 1
                stale_ids.append(observation.id)
        if stale_ids:
            await db.execute(
                update(ScreenObservation)
                .where(col(ScreenObservation.id).in_(stale_ids))
                .values(blocked=True)
            )
    return {
        "archived": len(stale_ids),
        "source_missing": source_missing,
        "stale_root": stale_root,
        "screenshot_folder": str(screenshot_folder),
    }


@router.get("/settings/artifact-storage")
async def get_artifact_storage_settings():
    """Return operator-visible evidence/report archive configuration."""
    report_archive_dir, report_archive_source = _report_archive_dir()
    report_dir_status = _archive_dir_status(report_archive_dir)
    screenshot_folder, screenshot_folder_source = _screenshot_folder()
    screenshot_source_result, report_receipts_result, screenshot_pipeline_result, local_runtime_proof_result = await asyncio.gather(
        _screenshot_folder_summary_fast(screenshot_folder),
        _report_receipt_summary_fast(report_archive_dir),
        _screenshot_folder_pipeline_summary_fast(screenshot_folder),
        _local_runtime_profile_proof_summary_fast(),
        return_exceptions=True,
    )
    screenshot_source = (
        _fallback_screenshot_folder_summary(screenshot_folder, status="summary_unavailable")
        if isinstance(screenshot_source_result, Exception)
        else screenshot_source_result
    )
    report_receipts = (
        {"receipt_count": 0, "last_receipt_at": None}
        if isinstance(report_receipts_result, Exception)
        else report_receipts_result
    )
    screenshot_pipeline = (
        _empty_screenshot_folder_pipeline_summary(latest_failure="analysis metadata unavailable")
        if isinstance(screenshot_pipeline_result, Exception)
        else screenshot_pipeline_result
    )
    local_runtime_proof = None if isinstance(local_runtime_proof_result, Exception) else local_runtime_proof_result
    screenshot_image_count = int(screenshot_source["image_count"] or 0)
    screenshot_observation_count = int(screenshot_pipeline["observation_count"] or 0)
    screenshot_processed_count = int(
        dict(screenshot_pipeline["analysis_status"]).get("succeeded", 0)
        if isinstance(screenshot_pipeline.get("analysis_status"), dict)
        else 0
    )
    screenshot_pipeline.update(
        {
            "folder_image_count": screenshot_image_count,
            "ingested_count": screenshot_observation_count,
            "remaining_to_ingest": max(screenshot_image_count - screenshot_observation_count, 0),
            "processed_count": screenshot_processed_count,
            "remaining_to_analyze": max(screenshot_observation_count - screenshot_processed_count, 0),
            "folder_remaining_to_analyze": max(screenshot_image_count - screenshot_processed_count, 0),
        }
    )
    screen_analysis = await get_screen_analysis_settings()
    vlm_status = effective_vlm_status(live_probe=deferred_vlm_live_probe())
    chat_policy = effective_workload_policy("chat_agent")
    inference_policy = {
        "provider": "openrouter",
        "active_only": bool(settings.openrouter_provider_only),
        "api_base": "https://openrouter.ai/api/v1",
        "credential_configured": bool(settings.openrouter_api_key.strip()),
        "allowed_upstreams": [
            item.strip()
            for item in settings.openrouter_allowed_upstreams.split(",")
            if item.strip()
        ],
        "data_collection": settings.openrouter_data_collection,
        "require_parameters": bool(settings.openrouter_require_parameters),
        "zero_data_retention": bool(settings.openrouter_zero_data_retention),
        "fallbacks_allowed": bool(settings.openrouter_allow_fallbacks),
        "chat_cloud_egress": chat_policy.egress_class.value,
        "chat_cloud_consent": bool(chat_policy.cloud_egress_acknowledged),
        "chat_budget_microusd": chat_policy.max_cost_microusd,
        "status": (
            "ready"
            if (
                settings.openrouter_provider_only
                and settings.openrouter_api_key.strip()
                and chat_policy.egress_class is not EgressClass.LOCAL_ONLY
                and chat_policy.cloud_egress_acknowledged
                and bool(settings.openrouter_allowed_upstreams.strip())
                and not settings.openrouter_allow_fallbacks
                and settings.openrouter_require_parameters
                and settings.openrouter_data_collection == "deny"
                and set(chat_policy.allowed_provider_kinds) == {"openrouter"}
            )
            else "configuration_required"
        ),
    }
    return {
        "inference": inference_policy,
        "screen": {
            "analysis_enabled": screen_analysis["enabled"],
            "provider": screen_analysis["provider"],
            "model": screen_analysis["model"],
        },
        "screenshot_folder": {
            "enabled": True,
            "provider": "screenshot_folder",
            "path": str(screenshot_folder),
            "path_source": screenshot_folder_source,
            "image_count": screenshot_source["image_count"],
            "last_image_at": screenshot_source["last_image_at"],
            "last_image_at_source": screenshot_source["last_image_at_source"],
            "status": screenshot_source["status"],
            "exists": screenshot_source["exists"],
            "readable": screenshot_source["readable"],
            "summary_status": screenshot_source.get("summary_status", "ready"),
            "summary_failure": screenshot_source.get("summary_failure"),
            "stored_artifacts": ["image"],
            "analysis": {
                "provider": effective_screen_analysis_provider() or "not_configured",
                "model": effective_screen_analysis_model(),
                "base_url_configured": bool(settings.openrouter_api_key.strip()),
                "runtime": vlm_status,
                **screenshot_pipeline,
            },
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
        "local_runtime": {
            # The legacy provider flag can disable the active cloud phase, but
            # it must never make the retired local/GPU route executable again.
            "active": False,
            "disabled_reason": "local_inference_disabled_openrouter_only",
            "gateway_configured": bool(
                settings.local_llm_api_base.strip() or effective_vlm_base_url()
            ),
            "llm_base_url_configured": bool(settings.local_llm_api_base.strip()),
            "vlm_base_url_configured": bool(effective_vlm_base_url()),
            "vlm_runtime": vlm_status,
            "model": settings.local_model or settings.local_vlm_model or "",
            "profiles": local_runtime_profile_statuses(),
            "profile_proof": local_runtime_proof,
            "proof_command": (
                "PYTHONPATH=. uv run python ../scripts/verify_local_gemma_profiles.py "
                "--base-url ${LOCAL_LLM_API_BASE:-${SERAPH_VLM_BASE_URL}/v1}"
            ),
        },
        "reports": {
            "enabled": settings.end_of_day_report_enabled,
            "hour": settings.end_of_day_report_hour,
            "analysis_provider": "llm" if settings.end_of_day_report_llm_enabled else "llm_disabled",
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
