"""Read-only API for stored end-of-day goal reports.

These routes only read report episodes that ``store_end_of_day_goal_report``
already wrote (plus, for the detail view, that report's own verified JSON
archive).  They make no inference or external call and write nothing.  Private
filesystem paths and raw screen samples are never returned.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from src.api.observer import _require_local_artifact_request
from src.db.models import MemoryEpisode
from src.memory.repository import (
    END_OF_DAY_REPORT_LIST_MAX,
    END_OF_DAY_REPORT_SCHEMA,
    end_of_day_report_metadata,
    memory_repository,
)
from src.scheduler.jobs.end_of_day_goal_report import _report_archive_root, public_report_artifacts

router = APIRouter()

# The report builder marks every analysis it did not run (screen-data policy,
# operator disable, guardian block) with an ``analysis_provider`` of
# ``blocked:<reason>``; the stored body is then a notice, not a report.
_BLOCKED_PROVIDER_PREFIX = "blocked:"
_ARCHIVE_MAX_BYTES = 2 * 1024 * 1024
_MIX_MAX_ENTRIES = 25
_MIX_KEY_MAX = 120


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _non_negative_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float) and value == value:
        return max(0, int(value))
    return 0


def _optional_str(value: Any, max_len: int = 256) -> str | None:
    # Bounded so one oversized stored label can never make a client reject
    # the whole list (critic finding 1).
    return value[:max_len] if isinstance(value, str) and value else None


def _utf16_prefix(value: str, max_units: int) -> str:
    """Truncate by UTF-16 code units, matching the cockpit's ``length`` check."""
    out: list[str] = []
    units = 0
    for char in value:
        width = 2 if ord(char) > 0xFFFF else 1
        if units + width > max_units:
            break
        out.append(char)
        units += width
    return "".join(out)


def _status(analysis_provider: str | None) -> tuple[str, str | None]:
    if analysis_provider and analysis_provider.startswith(_BLOCKED_PROVIDER_PREFIX):
        reason = analysis_provider[len(_BLOCKED_PROVIDER_PREFIX):][:256] or "unspecified"
        return "blocked", reason
    return "ready", None


def _list_item(episode: MemoryEpisode) -> dict[str, Any]:
    metadata = end_of_day_report_metadata(episode)
    provider = _optional_str(metadata.get("analysis_provider"))
    status, blocked_reason = _status(provider)
    return {
        "episode_id": episode.id,
        "date": _optional_str(metadata.get("date"), 32),
        "timezone": _optional_str(metadata.get("timezone"), 64),
        "created_at": _iso(episode.created_at),
        "observed_at": _iso(episode.observed_at),
        "status": status,
        "blocked_reason": blocked_reason,
        "analysis_provider": provider,
        "active_goal_count": _non_negative_int(metadata.get("active_goal_count")),
        "completed_goal_count": _non_negative_int(metadata.get("completed_goal_count")),
        "total_tracked_minutes": _non_negative_int(metadata.get("total_tracked_minutes")),
    }


def _seconds_mix(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    merged: dict[str, int] = {}
    for key, seconds in value.items():
        if not isinstance(key, str) or not key:
            continue
        short = _utf16_prefix(key, _MIX_KEY_MAX)
        # Keys that collide after truncation add up instead of overwriting.
        merged[short] = merged.get(short, 0) + _non_negative_int(seconds)
    items = sorted(merged.items(), key=lambda item: (-item[1], item[0]))
    return dict(items[:_MIX_MAX_ENTRIES])


def _archived_summary(metadata: dict[str, Any]) -> dict[str, Any] | None:
    """Read the report's own JSON archive, only if it verifies.

    The path must resolve under the configured report archive's
    ``end-of-day`` directory, be a regular file within a size cap, match the
    stored SHA-256 and carry the report schema.  Anything else yields ``None``.
    """

    artifacts = metadata.get("artifacts")
    if not isinstance(artifacts, dict):
        return None
    raw_path = artifacts.get("report_json_path")
    expected_sha = artifacts.get("report_json_sha256")
    if not isinstance(raw_path, str) or not raw_path or not isinstance(expected_sha, str):
        return None
    try:
        allowed_root = (_report_archive_root() / "end-of-day").resolve()
        path = Path(raw_path).resolve()
        if not path.is_relative_to(allowed_root):
            return None
        # Open without following a final symlink, check the opened file itself,
        # and read at most the cap + 1 byte so growth after a stat cannot bypass it.
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            with os.fdopen(fd, "rb", closefd=False) as handle:
                payload_bytes = handle.read(_ARCHIVE_MAX_BYTES + 1)
        finally:
            os.close(fd)
        if len(payload_bytes) > _ARCHIVE_MAX_BYTES:
            return None
    except (OSError, ValueError):
        return None
    if hashlib.sha256(payload_bytes).hexdigest() != expected_sha:
        return None
    try:
        payload = json.loads(payload_bytes)
    except ValueError:
        return None
    if not isinstance(payload, dict) or payload.get("artifact_schema") != END_OF_DAY_REPORT_SCHEMA:
        return None
    report = payload.get("report")
    summary = report.get("summary") if isinstance(report, dict) else None
    return summary if isinstance(summary, dict) else None


def _public_summary(metadata: dict[str, Any]) -> dict[str, Any]:
    archived = _archived_summary(metadata)
    if archived is None:
        return {
            "source": "episode_metadata",
            "total_observations": _non_negative_int(metadata.get("screen_observation_count")),
            "total_tracked_minutes": _non_negative_int(metadata.get("total_tracked_minutes")),
            "switch_count": None,
            "by_activity": {},
            "by_project": {},
            "by_source": {},
        }
    return {
        "source": "archive",
        "total_observations": _non_negative_int(archived.get("total_observations")),
        "total_tracked_minutes": _non_negative_int(archived.get("total_tracked_minutes")),
        "switch_count": _non_negative_int(archived.get("switch_count")),
        "by_activity": _seconds_mix(archived.get("by_activity")),
        "by_project": _seconds_mix(archived.get("by_project")),
        "by_source": _seconds_mix(archived.get("by_source")),
    }


@router.get("/reports/end-of-day")
async def list_end_of_day_reports(request: Request, limit: int = Query(30, ge=1, le=END_OF_DAY_REPORT_LIST_MAX)):
    # Screen-derived content: same authenticated-operator + localhost guard as
    # /api/observer/screen-artifacts.
    _require_local_artifact_request(request)
    episodes = await memory_repository.list_end_of_day_report_episodes(limit=limit)
    return {"items": [_list_item(episode) for episode in episodes], "limit": limit}


@router.get("/reports/end-of-day/{episode_id}")
async def get_end_of_day_report(request: Request, episode_id: str):
    _require_local_artifact_request(request)
    episode = await memory_repository.get_end_of_day_report_episode(episode_id)
    if episode is None:
        raise HTTPException(status_code=404, detail="End-of-day report not found")
    metadata = end_of_day_report_metadata(episode)
    return {
        **_list_item(episode),
        "body": episode.content or "",
        "summary": _public_summary(metadata),
        "artifacts": public_report_artifacts(metadata.get("artifacts")),
    }
