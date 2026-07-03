"""Provider-backed semantic analysis for screenshot-folder images."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any
from datetime import datetime, timezone

import httpx

from config.settings import settings
from src.local_runtime_profiles import (
    local_runtime_profile_form_fields,
    local_runtime_profile_headers,
)
from src.observer.screen_analysis_settings import (
    effective_screen_analysis_enabled,
    effective_screen_analysis_model,
    effective_screen_analysis_provider,
)
from src.observer.screenshot_analysis_contract import (
    ScreenshotAnalysis,
    ScreenshotAnalysisContractError,
    SCREENSHOT_ANALYSIS_PROMPT_VERSION,
    SCREENSHOT_ANALYSIS_SCHEMA_VERSION,
    parse_screenshot_analysis_output,
    screenshot_analysis_prompt,
)
from src.vlm_runtime import (
    effective_vlm_api_key,
    effective_vlm_base_url,
    effective_vlm_feeder_window,
)

logger = logging.getLogger(__name__)

SCREENSHOT_ANALYSIS_DETAIL_PREFIX = "screenshot_analysis:"
SCREENSHOT_ANALYSIS_ERROR_DETAIL_PREFIX = "screenshot_analysis_error:"
SCREENSHOT_ANALYSIS_STATUS_DETAIL_PREFIX = "screenshot_analysis_status:"
REANALYSIS_REASONS = {
    "prompt_version_changed",
    "model_version_changed",
    "provider_failure_retry",
    "manual_operator_request",
}


class ScreenshotSemanticAnalysisError(RuntimeError):
    """Raised when the configured semantic screenshot analyzer fails."""


def screenshot_semantic_analysis_enabled() -> bool:
    """Return true when Seraph should call the configured VLM screenshot analyzer."""
    return (
        effective_screen_analysis_enabled()
        and effective_screen_analysis_provider().lower() == "local-vlm"
        and bool(effective_vlm_base_url())
    )


async def screenshot_semantic_analysis_ready(*, timeout_seconds: float = 2.0) -> bool:
    """Return true when the configured VLM screenshot analyzer is reachable."""
    status = await _screenshot_semantic_analysis_health(timeout_seconds=timeout_seconds)
    return status is not None


async def screenshot_semantic_analysis_accepting_background_work(*, timeout_seconds: float = 2.0) -> bool:
    """Return true when the VLM wrapper can accept one background image job."""
    return await screenshot_semantic_analysis_background_slots(timeout_seconds=timeout_seconds) > 0


async def screenshot_semantic_analysis_background_slots(*, timeout_seconds: float = 2.0) -> int:
    """Return the number of Seraph background image jobs the VLM wrapper can accept now."""
    status = await _screenshot_semantic_analysis_queue_status(timeout_seconds=timeout_seconds)
    if status is None:
        return 0
    if not await _screenshot_semantic_analysis_backend_ready(timeout_seconds=timeout_seconds):
        return 0
    queue = _normalized_queue_status(status)
    if queue is None:
        return 0
    active, queued, workers = queue
    capacity_window = max(min(effective_vlm_feeder_window(), workers + 1), 1)
    return max(capacity_window - active - queued, 0)


def _normalized_queue_status(status: dict[str, Any]) -> tuple[int, int, int] | None:
    queue_status = status.get("queue")
    if isinstance(queue_status, dict):
        status = queue_status
    try:
        active = int(status.get("active", 0))
        queued = int(status.get("queued", 0))
        workers = int(status.get("workers", 1))
    except (TypeError, ValueError):
        return None
    return max(active, 0), max(queued, 0), max(workers, 1)


async def _screenshot_semantic_analysis_health(*, timeout_seconds: float = 2.0) -> dict[str, Any] | None:
    """Return the VLM wrapper health payload when reachable."""
    if not screenshot_semantic_analysis_enabled():
        return None
    endpoint = effective_vlm_base_url() + "/health"
    try:
        async with httpx.AsyncClient(timeout=max(timeout_seconds, 0.25)) as client:
            response = await client.get(endpoint)
        if not (200 <= response.status_code < 500):
            return None
        payload = response.json()
        return payload if isinstance(payload, dict) else {}
    except (httpx.HTTPError, ValueError):
        return None


async def _screenshot_semantic_analysis_queue_status(*, timeout_seconds: float = 2.0) -> dict[str, Any] | None:
    """Return the VLM wrapper queue status payload when reachable."""
    if not screenshot_semantic_analysis_enabled():
        return None
    endpoint = effective_vlm_base_url() + "/queue/status"
    try:
        async with httpx.AsyncClient(timeout=max(timeout_seconds, 0.25)) as client:
            response = await client.get(endpoint)
        if not (200 <= response.status_code < 500):
            return None
        payload = response.json()
        return payload if isinstance(payload, dict) else {}
    except (httpx.HTTPError, ValueError):
        return None


async def _screenshot_semantic_analysis_backend_ready(*, timeout_seconds: float = 2.0) -> bool:
    """Return true when the wrapper's configured GPU backend is reachable."""
    if not screenshot_semantic_analysis_enabled():
        return False
    endpoint = effective_vlm_base_url() + "/health/backend"
    try:
        async with httpx.AsyncClient(timeout=max(timeout_seconds, 0.25)) as client:
            response = await client.get(endpoint)
        return 200 <= response.status_code < 300
    except httpx.HTTPError:
        return False


async def analyze_screenshot_image(image_path: Path, artifacts: dict[str, Any]) -> ScreenshotAnalysis | None:
    """Analyze one screenshot image through the configured Seraph VLM provider."""
    if not screenshot_semantic_analysis_enabled():
        return None
    return await _analyze_with_local_vlm(image_path, artifacts)


def screenshot_analysis_detail(analysis: ScreenshotAnalysis) -> str:
    """Serialize a validated screenshot analysis for ScreenObservation details."""
    payload = analysis.model_dump(mode="json")
    return SCREENSHOT_ANALYSIS_DETAIL_PREFIX + json.dumps(payload, sort_keys=True, separators=(",", ":"))


def screenshot_analysis_status_detail(
    status: str,
    *,
    reason: str | None = None,
    reanalysis_reason: str | None = None,
    attempts: int | None = None,
) -> str:
    """Serialize semantic analysis status for idempotency and reanalysis decisions."""
    payload = {
        "status": status,
        "provider": effective_screen_analysis_provider() or "not_configured",
        "model": effective_screen_analysis_model() or None,
        "schema_version": SCREENSHOT_ANALYSIS_SCHEMA_VERSION,
        "prompt_version": SCREENSHOT_ANALYSIS_PROMPT_VERSION,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    if reason:
        payload["reason"] = _bounded_reason(reason)
    if reanalysis_reason:
        payload["reanalysis_reason"] = reanalysis_reason
    if attempts is not None:
        payload["attempts"] = max(int(attempts), 0)
    return SCREENSHOT_ANALYSIS_STATUS_DETAIL_PREFIX + json.dumps(payload, sort_keys=True, separators=(",", ":"))


def screenshot_analysis_error_detail(reason: str) -> str:
    """Serialize a bounded analyzer failure for ScreenObservation details."""
    return SCREENSHOT_ANALYSIS_ERROR_DETAIL_PREFIX + json.dumps(
        {"provider": effective_screen_analysis_provider() or "unknown", "reason": _bounded_reason(reason)},
        sort_keys=True,
        separators=(",", ":"),
    )


def semantic_analysis_from_details(details: list[Any]) -> dict[str, Any] | None:
    """Extract the persisted semantic analysis payload from observation details."""
    for item in details:
        if not isinstance(item, str) or not item.startswith(SCREENSHOT_ANALYSIS_DETAIL_PREFIX):
            continue
        try:
            payload = json.loads(item.removeprefix(SCREENSHOT_ANALYSIS_DETAIL_PREFIX))
        except json.JSONDecodeError:
            return None
        if isinstance(payload, dict):
            return payload
    return None


def semantic_analysis_error_from_details(details: list[Any]) -> dict[str, Any] | None:
    """Extract the persisted semantic analysis failure payload from observation details."""
    for item in details:
        if not isinstance(item, str) or not item.startswith(SCREENSHOT_ANALYSIS_ERROR_DETAIL_PREFIX):
            continue
        try:
            payload = json.loads(item.removeprefix(SCREENSHOT_ANALYSIS_ERROR_DETAIL_PREFIX))
        except json.JSONDecodeError:
            return None
        if isinstance(payload, dict):
            return payload
    return None


def semantic_analysis_status_from_details(details: list[Any]) -> dict[str, Any] | None:
    """Extract the latest semantic analysis status payload from observation details."""
    latest: dict[str, Any] | None = None
    for item in details:
        if not isinstance(item, str) or not item.startswith(SCREENSHOT_ANALYSIS_STATUS_DETAIL_PREFIX):
            continue
        try:
            payload = json.loads(item.removeprefix(SCREENSHOT_ANALYSIS_STATUS_DETAIL_PREFIX))
        except json.JSONDecodeError:
            return None
        if isinstance(payload, dict):
            latest = payload
    return latest


def semantic_analysis_needs_reanalysis(details: list[Any]) -> bool:
    """Return true when stored analysis exists but belongs to an old prompt or model contract."""
    analysis = semantic_analysis_from_details(details)
    status = semantic_analysis_status_from_details(details)
    if analysis is None or status is None:
        return False
    return (
        analysis.get("prompt_version") != SCREENSHOT_ANALYSIS_PROMPT_VERSION
        or analysis.get("schema_version") != SCREENSHOT_ANALYSIS_SCHEMA_VERSION
        or status.get("model") != (effective_screen_analysis_model() or None)
    )


def validate_reanalysis_reason(reason: str) -> str:
    """Validate the explicit operator reason required before reanalysis."""
    normalized = str(reason or "").strip()
    if normalized not in REANALYSIS_REASONS:
        allowed = ", ".join(sorted(REANALYSIS_REASONS))
        raise ValueError(f"reanalysis_reason must be one of: {allowed}")
    return normalized


def replace_semantic_analysis_details(
    details: list[Any],
    *,
    analysis: ScreenshotAnalysis | None,
    error_reason: str | None,
    reanalysis_reason: str,
) -> list[str]:
    """Replace existing semantic analysis details while preserving capture metadata."""
    next_details = [
        item
        for item in details
        if not (
            isinstance(item, str)
            and (
                item.startswith(SCREENSHOT_ANALYSIS_DETAIL_PREFIX)
                or item.startswith(SCREENSHOT_ANALYSIS_ERROR_DETAIL_PREFIX)
                or item.startswith(SCREENSHOT_ANALYSIS_STATUS_DETAIL_PREFIX)
            )
        )
    ]
    if analysis is not None:
        next_details.append(screenshot_analysis_detail(analysis))
        next_details.append(
            screenshot_analysis_status_detail("succeeded", reanalysis_reason=reanalysis_reason)
        )
    else:
        reason = error_reason or "unknown"
        next_details.append(screenshot_analysis_error_detail(reason))
        next_details.append(
            screenshot_analysis_status_detail(
                "failed",
                reason=reason,
                reanalysis_reason=reanalysis_reason,
            )
        )
    return [str(item) for item in next_details if isinstance(item, str)]


async def _analyze_with_local_vlm(image_path: Path, artifacts: dict[str, Any]) -> ScreenshotAnalysis:
    metadata = {
        "captured_at": artifacts.get("created_at"),
        "source": "screenshot_folder",
        "filename": image_path.name,
        "image_sha256": artifacts.get("image_sha256"),
        "file_format": artifacts.get("file_format"),
        "width": artifacts.get("width"),
        "height": artifacts.get("height"),
    }
    prompt = screenshot_analysis_prompt(metadata)
    endpoint = effective_vlm_base_url() + "/v1/analyze-file"
    data = {
        "prompt": prompt,
        **local_runtime_profile_form_fields("screenshot_fast"),
    }
    model = effective_screen_analysis_model()
    if model:
        data["model"] = model
    headers = local_runtime_profile_headers("screenshot_fast")
    api_key = effective_vlm_api_key()
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        image_bytes = await asyncio.to_thread(image_path.read_bytes)
        async with httpx.AsyncClient(timeout=max(settings.local_vlm_timeout_seconds, 1)) as client:
            response = await client.post(
                endpoint,
                data=data,
                files={"file": (image_path.name, image_bytes, _image_media_type(image_path))},
                headers=headers,
            )
        response.raise_for_status()
        payload = response.json()
        return parse_screenshot_analysis_output(_provider_analysis_payload(payload))
    except (OSError, httpx.HTTPError, ValueError, ScreenshotAnalysisContractError) as exc:
        logger.warning("screenshot semantic analysis failed for %s: %s", image_path, exc)
        raise ScreenshotSemanticAnalysisError(str(exc)) from exc


def _provider_analysis_payload(payload: Any) -> str | dict[str, Any]:
    if isinstance(payload, dict):
        for key in ("analysis", "output", "result", "content", "text"):
            value = payload.get(key)
            if isinstance(value, (str, dict)):
                return value
        return payload
    if isinstance(payload, str):
        return payload
    raise ScreenshotAnalysisContractError("local VLM response must be JSON text or object")


def _image_media_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".png":
        return "image/png"
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    return "application/octet-stream"


def _bounded_reason(reason: str) -> str:
    return " ".join(str(reason or "unknown").strip().split())[:240] or "unknown"
