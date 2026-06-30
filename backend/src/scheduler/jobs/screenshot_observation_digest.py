"""Rolling privacy-safe digest windows for screenshot-folder observations."""

from __future__ import annotations

import hashlib
import json
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_
from sqlmodel import col, select

from config.settings import settings
from src.audit.runtime import log_scheduler_job_event
from src.db.engine import get_session
from src.db.models import MemoryEpisode, MemoryEpisodeType, ScreenObservation
from src.observer.screenshot_semantic_analysis import (
    semantic_analysis_error_from_details,
    semantic_analysis_from_details,
    semantic_analysis_status_from_details,
)
from src.scheduler.jobs.end_of_day_goal_report import _redact_text

logger = logging.getLogger(__name__)

SCREENSHOT_DIGEST_SCHEMA_VERSION = "seraph.screenshot_observation_digest.v1"
SCREENSHOT_DIGEST_TOOL_NAME = "screenshot_observation_digest"


@dataclass(frozen=True)
class ScreenshotDigestResult:
    status: str
    window_start: datetime
    window_end: datetime
    observation_count: int
    episode_id: str | None = None
    digest_key: str | None = None


async def run_screenshot_observation_digest() -> None:
    """Scheduler entrypoint for the current rolling screenshot digest window."""
    if not settings.screenshot_observation_digest_enabled:
        await log_scheduler_job_event(
            job_name=SCREENSHOT_DIGEST_TOOL_NAME,
            outcome="skipped",
            details={"reason": "disabled"},
        )
        return
    try:
        result = await build_current_screenshot_observation_digest()
        await log_scheduler_job_event(
            job_name=SCREENSHOT_DIGEST_TOOL_NAME,
            outcome="succeeded",
            details={
                "status": result.status,
                "window_start": result.window_start.isoformat(),
                "window_end": result.window_end.isoformat(),
                "observation_count": result.observation_count,
                "episode_id": result.episode_id,
                "digest_key": result.digest_key,
            },
        )
    except Exception as exc:
        await log_scheduler_job_event(
            job_name=SCREENSHOT_DIGEST_TOOL_NAME,
            outcome="failed",
            details={"error": exc.__class__.__name__},
        )
        logger.exception("screenshot observation digest failed")


async def build_current_screenshot_observation_digest(
    *,
    now: datetime | None = None,
) -> ScreenshotDigestResult:
    """Build or update the current rolling digest window."""
    window_end = _floor_window(now or datetime.now(timezone.utc), _window_minutes())
    window_start = window_end - timedelta(minutes=_window_minutes())
    return await build_screenshot_observation_digest(window_start=window_start, window_end=window_end)


async def build_screenshot_observation_digest(
    *,
    window_start: datetime,
    window_end: datetime,
) -> ScreenshotDigestResult:
    """Build or update one screenshot digest window."""
    start = _ensure_utc(window_start)
    end = _ensure_utc(window_end)
    observations = await _screenshot_observations(start, end)
    digest_key = _digest_key(start, end)
    if not observations:
        return ScreenshotDigestResult(
            status="empty",
            window_start=start,
            window_end=end,
            observation_count=0,
            digest_key=digest_key,
        )

    payload = _digest_payload(start, end, observations, digest_key=digest_key)
    content = _digest_content(payload)
    payload_digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()

    async with get_session() as db:
        existing = await _existing_digest_episode(db, digest_key)
        metadata = {
            "kind": SCREENSHOT_DIGEST_TOOL_NAME,
            "artifact_schema": SCREENSHOT_DIGEST_SCHEMA_VERSION,
            "digest_key": digest_key,
            "payload_sha256": payload_digest,
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "observation_ids": payload["observation_ids"],
            "observation_count": payload["observation_count"],
            "content_char_count": len(content),
        }
        if existing is not None:
            existing_metadata = _metadata(existing)
            if existing_metadata.get("payload_sha256") == payload_digest:
                return ScreenshotDigestResult(
                    status="unchanged",
                    window_start=start,
                    window_end=end,
                    observation_count=len(observations),
                    episode_id=existing.id,
                    digest_key=digest_key,
                )
            existing.summary = _digest_summary(payload)
            existing.content = content
            existing.metadata_json = json.dumps(metadata, sort_keys=True)
            existing.observed_at = end
            db.add(existing)
            return ScreenshotDigestResult(
                status="updated",
                window_start=start,
                window_end=end,
                observation_count=len(observations),
                episode_id=existing.id,
                digest_key=digest_key,
            )

        episode = MemoryEpisode(
            episode_type=MemoryEpisodeType.observer,
            summary=_digest_summary(payload),
            content=content,
            source_tool_name=SCREENSHOT_DIGEST_TOOL_NAME,
            salience=0.55,
            confidence=0.75,
            metadata_json=json.dumps(metadata, sort_keys=True),
            observed_at=end,
        )
        db.add(episode)
        await db.flush()
        episode_id = episode.id

    return ScreenshotDigestResult(
        status="created",
        window_start=start,
        window_end=end,
        observation_count=len(observations),
        episode_id=episode_id,
        digest_key=digest_key,
    )


async def _screenshot_observations(start: datetime, end: datetime) -> list[ScreenObservation]:
    async with get_session() as db:
        result = await db.execute(
            select(ScreenObservation)
            .where(col(ScreenObservation.timestamp) >= start)
            .where(col(ScreenObservation.timestamp) < end)
            .where(col(ScreenObservation.blocked) == False)  # noqa: E712
            .where(
                or_(
                    col(ScreenObservation.app_name) == "Screenshot Folder",
                    col(ScreenObservation.details_json).contains('"provider":"screenshot_folder"'),
                    col(ScreenObservation.details_json).contains('"provider": "screenshot_folder"'),
                )
            )
            .order_by(col(ScreenObservation.timestamp))
        )
        return list(result.scalars().all())


def _digest_payload(
    start: datetime,
    end: datetime,
    observations: list[ScreenObservation],
    *,
    digest_key: str,
) -> dict[str, Any]:
    activities: Counter[str] = Counter()
    projects: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    apps: Counter[str] = Counter()
    summaries: list[str] = []
    snippets: list[str] = []
    blockers: list[str] = []
    drift: list[str] = []
    confidence_values: list[float] = []

    for obs in observations:
        details = _details(obs)
        analysis = semantic_analysis_from_details(details) or {}
        status = semantic_analysis_status_from_details(details) or {}
        error = semantic_analysis_error_from_details(details) or {}
        activity = str(analysis.get("activity_type") or obs.activity_type or "unknown")
        project = str(analysis.get("project") or obs.project or "unknown")
        activities[activity] += 1
        projects[project] += 1
        apps[obs.app_name or "unknown"] += 1
        statuses[str(status.get("status") or ("failed" if error else "pending"))] += 1
        summary = _redact_text(str(analysis.get("summary") or obs.summary or ""), limit=180)
        if summary and summary not in summaries:
            summaries.append(summary)
        for text in analysis.get("key_visible_text") or []:
            snippet = _redact_text(str(text), limit=120)
            if snippet and snippet not in snippets:
                snippets.append(snippet)
        alignment = analysis.get("goal_alignment") if isinstance(analysis.get("goal_alignment"), dict) else {}
        alignment_status = str(alignment.get("status") or "")
        if alignment_status == "blocked":
            blockers.extend(_redacted_list(alignment.get("evidence"), limit=120))
        if alignment_status == "drifted":
            drift.extend(_redacted_list(alignment.get("evidence"), limit=120))
        confidence = analysis.get("confidence")
        if isinstance(confidence, (int, float)):
            confidence_values.append(float(confidence))

    return {
        "artifact_schema": SCREENSHOT_DIGEST_SCHEMA_VERSION,
        "digest_key": digest_key,
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "observation_count": len(observations),
        "observation_ids": [obs.id for obs in observations],
        "activity_mix": dict(activities.most_common(8)),
        "project_mix": dict(projects.most_common(8)),
        "app_mix": dict(apps.most_common(8)),
        "analysis_status": dict(statuses.most_common()),
        "average_confidence": round(sum(confidence_values) / len(confidence_values), 3)
        if confidence_values
        else None,
        "progression": summaries[:12],
        "privacy_safe_snippets": snippets[:12],
        "blockers": _unique(blockers)[:8],
        "drift": _unique(drift)[:8],
    }


def _digest_content(payload: dict[str, Any]) -> str:
    blockers = [f"- {item}" for item in payload["blockers"]] or ["- None"]
    drift = [f"- {item}" for item in payload["drift"]] or ["- None"]
    snippets = [f"- {item}" for item in payload["privacy_safe_snippets"]] or ["- None"]
    lines = [
        f"Screenshot digest {payload['window_start']} to {payload['window_end']}",
        f"Observations: {payload['observation_count']}",
        f"Analysis status: {payload['analysis_status']}",
        f"Activity mix: {payload['activity_mix']}",
        f"Project mix: {payload['project_mix']}",
        f"Average confidence: {payload['average_confidence']}",
        "",
        "Progression:",
        *[f"- {item}" for item in payload["progression"]],
        "",
        "Privacy-safe snippets:",
        *snippets,
        "",
        "Evidence observation IDs:",
        *[f"- {item}" for item in payload["observation_ids"]],
        "",
        "Blockers:",
        *blockers,
        "",
        "Drift:",
        *drift,
    ]
    content = "\n".join(lines)
    return content[: max(settings.screenshot_observation_digest_max_chars, 500)]


def _digest_summary(payload: dict[str, Any]) -> str:
    project = next(iter(payload["project_mix"]), "unknown")
    return (
        f"Screenshot digest {payload['window_start']} to {payload['window_end']}: "
        f"{payload['observation_count']} observations, main project {project}"
    )


async def _existing_digest_episode(db: Any, digest_key: str) -> MemoryEpisode | None:
    result = await db.execute(
        select(MemoryEpisode)
        .where(col(MemoryEpisode.source_tool_name) == SCREENSHOT_DIGEST_TOOL_NAME)
        .where(col(MemoryEpisode.metadata_json).contains(f'"digest_key": "{digest_key}"'))
        .limit(1)
    )
    return result.scalar_one_or_none()


def _digest_key(start: datetime, end: datetime) -> str:
    raw = "|".join([SCREENSHOT_DIGEST_SCHEMA_VERSION, start.isoformat(), end.isoformat()])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _floor_window(value: datetime, minutes: int) -> datetime:
    timestamp = _ensure_utc(value)
    minute = (timestamp.minute // minutes) * minutes
    return timestamp.replace(minute=minute, second=0, microsecond=0)


def _window_minutes() -> int:
    raw = settings.screenshot_observation_digest_window_min
    if not isinstance(raw, int) or raw < 1 or raw > 240:
        return 30
    return raw


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _details(observation: ScreenObservation) -> list[Any]:
    try:
        payload = json.loads(observation.details_json or "[]")
    except json.JSONDecodeError:
        return []
    return payload if isinstance(payload, list) else []


def _metadata(episode: MemoryEpisode) -> dict[str, Any]:
    try:
        payload = json.loads(episode.metadata_json or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _redacted_list(values: Any, *, limit: int) -> list[str]:
    if not isinstance(values, list):
        return []
    return [_redact_text(str(value), limit=limit) for value in values if _redact_text(str(value), limit=limit)]


def _unique(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
