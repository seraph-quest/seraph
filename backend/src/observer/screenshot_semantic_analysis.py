"""Provider-backed semantic analysis for local screenshot images."""

from __future__ import annotations

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
from src.observer.screenshot_analysis_contract import (
    ScreenshotAnalysis,
    ScreenshotAnalysisContractError,
    SCREENSHOT_ANALYSIS_PROMPT_VERSION,
    SCREENSHOT_ANALYSIS_SCHEMA_VERSION,
    parse_screenshot_analysis_output,
    screenshot_analysis_prompt,
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
    """Return true when Seraph should call the local VLM screenshot analyzer."""
    return settings.screen_analysis_provider.strip().lower() == "local-vlm" and bool(
        settings.local_vlm_base_url.strip()
    )


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
) -> str:
    """Serialize semantic analysis status for idempotency and reanalysis decisions."""
    payload = {
        "status": status,
        "provider": settings.screen_analysis_provider or "not_configured",
        "model": settings.local_vlm_model or None,
        "schema_version": SCREENSHOT_ANALYSIS_SCHEMA_VERSION,
        "prompt_version": SCREENSHOT_ANALYSIS_PROMPT_VERSION,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    if reason:
        payload["reason"] = _bounded_reason(reason)
    if reanalysis_reason:
        payload["reanalysis_reason"] = reanalysis_reason
    return SCREENSHOT_ANALYSIS_STATUS_DETAIL_PREFIX + json.dumps(payload, sort_keys=True, separators=(",", ":"))


def screenshot_analysis_error_detail(reason: str) -> str:
    """Serialize a bounded analyzer failure for ScreenObservation details."""
    return SCREENSHOT_ANALYSIS_ERROR_DETAIL_PREFIX + json.dumps(
        {"provider": settings.screen_analysis_provider or "unknown", "reason": _bounded_reason(reason)},
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
        or status.get("model") != (settings.local_vlm_model or None)
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
    endpoint = settings.local_vlm_base_url.rstrip("/") + "/v1/analyze-file"
    data = {
        "prompt": prompt,
        **local_runtime_profile_form_fields("screenshot_fast"),
    }
    if settings.local_vlm_model.strip():
        data["model"] = settings.local_vlm_model.strip()
    headers = local_runtime_profile_headers("screenshot_fast")
    if settings.local_vlm_api_key.strip():
        headers["Authorization"] = f"Bearer {settings.local_vlm_api_key.strip()}"

    try:
        async with httpx.AsyncClient(timeout=max(settings.local_vlm_timeout_seconds, 1)) as client:
            with image_path.open("rb") as image_file:
                response = await client.post(
                    endpoint,
                    data=data,
                    files={"file": (image_path.name, image_file, _image_media_type(image_path))},
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
