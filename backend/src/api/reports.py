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
import re
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
# #1264: GitHub evidence. A link is shown only for a URL of exactly these shapes.
_GITHUB_URL = re.compile(
    r"^https://github\.com/[\w.-]+/[\w.-]+/(commit/[0-9a-f]{40}|pull/\d+|issues/\d+)(#[\w-]+)?$", re.ASCII)
_GITHUB_STATUSES = {"not_connected", "complete", "partial", "failed", "blocked", "revoked", "stopped",
                    "not_collected", "running", "unavailable"}
_GITHUB_KINDS = {"commit", "pr_opened", "pr_merged", "pr_closed", "issue_opened", "issue_closed"}
_GOAL_EVIDENCE_STATUSES = {"mapped", "unparsed", "not_requested", "blocked"}
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", re.ASCII)
_LOGIN = re.compile(r"^[A-Za-z0-9-]{1,39}$", re.ASCII)
_CODE = re.compile(r"^[a-z0-9_]{1,64}$", re.ASCII)
_G_REF = re.compile(r"^G\d{1,4}$", re.ASCII)
_A_REF = re.compile(r"^A\d{1,4}$", re.ASCII)
_GITHUB_ITEMS_MAX = 200
_GOALS_MAX = 24


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
        "github_activity_status": _github_status(metadata.get("github_activity_status")),
        "github_activity_item_count": _non_negative_int(metadata.get("github_activity_item_count")),
    }


def _github_status(value: Any) -> str | None:
    return value if isinstance(value, str) and value in _GITHUB_STATUSES else None


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


def _archived_report(metadata: dict[str, Any]) -> dict[str, Any] | None:
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
    return report if isinstance(report, dict) else None


def _public_summary(report: dict[str, Any] | None, metadata: dict[str, Any]) -> dict[str, Any]:
    archived = report.get("summary") if isinstance(report, dict) else None
    if not isinstance(archived, dict):
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
    report = _archived_report(metadata)
    return {
        **_list_item(episode),
        "body": episode.content or "",
        "summary": _public_summary(report, metadata),
        "artifacts": public_report_artifacts(metadata.get("artifacts")),
        "github_activity": _public_github_activity(report, metadata),
        "goal_evidence": _public_goal_evidence(report),
    }


def _text(value: Any, max_len: int) -> str | None:
    return _utf16_prefix(value, max_len) if isinstance(value, str) else None


def _github_item(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    ref, repository, kind = value.get("ref"), value.get("repository"), value.get("kind")
    if (not isinstance(ref, str) or not _G_REF.fullmatch(ref) or not isinstance(repository, str)
            or not _REPOSITORY.fullmatch(repository) or kind not in _GITHUB_KINDS):
        return None
    number = value.get("number")
    short_sha = value.get("short_sha")
    url = value.get("url")
    return {
        "ref": ref, "repository": repository, "kind": kind,
        "number": number if isinstance(number, int) and not isinstance(number, bool) and number > 0 else None,
        "short_sha": short_sha if isinstance(short_sha, str) and re.fullmatch(r"[0-9a-f]{7}", short_sha) else None,
        "title": _text(value.get("title"), 300) or "",
        # Re-validated: anything but a GitHub commit/PR/issue URL is dropped.
        "url": url if isinstance(url, str) and _GITHUB_URL.fullmatch(url) else None,
        "occurred_at": _text(value.get("occurred_at"), 64),
        "same_title_count": max(1, _non_negative_int(value.get("same_title_count"))),
    }


def _limitations(value: Any) -> list[dict[str, str]]:
    result = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("code"), str) or not _CODE.fullmatch(item["code"]):
            continue
        entry = {"code": item["code"]}
        if isinstance(item.get("repository"), str) and _REPOSITORY.fullmatch(item["repository"]):
            entry["repository"] = item["repository"]
        result.append(entry)
    return result[:50]


def _public_github_activity(report: dict[str, Any] | None, metadata: dict[str, Any]) -> dict[str, Any] | None:
    """GitHub evidence from the verified archive; status and counts only when it does not verify."""
    archived = report.get("github_activity") if isinstance(report, dict) else None
    if not isinstance(archived, dict):
        status = _github_status(metadata.get("github_activity_status"))
        if status is None:
            return None
        from src.integrations.github_activity import REPORT_FIXED_SENTENCES
        reason = metadata.get("github_activity_reason_code")
        # Metadata keeps only status and reason code; the sentence is a fixed one.
        return {"source": "episode_metadata", "status": status,
                "message": REPORT_FIXED_SENTENCES.get(status),
                "item_count": _non_negative_int(metadata.get("github_activity_item_count")),
                "items_available": False,
                "reason_code": reason if isinstance(reason, str) and _CODE.fullmatch(reason) else None,
                "limitations": [], "identity_login": None,
                "model_context": None, "counts": {}, "repositories": [], "items": []}
    status = _github_status(archived.get("status"))
    if status is None:
        return None
    counts = {key: _non_negative_int(value) for key, value in (archived.get("counts") or {}).items()
              if key in _GITHUB_KINDS} if isinstance(archived.get("counts"), dict) else {}
    items = [item for item in (_github_item(value) for value in (archived.get("items") or [])[:_GITHUB_ITEMS_MAX]
                               if isinstance(archived.get("items"), list)) if item is not None]
    login = archived.get("identity_login")
    reason = archived.get("reason_code")
    model_context = archived.get("model_context")
    return {
        "source": "archive", "status": status, "message": _text(archived.get("message"), 1000),
        "item_count": sum(counts.values()), "items_available": True,
        "reason_code": reason if isinstance(reason, str) and _CODE.fullmatch(reason) else None,
        "limitations": _limitations(archived.get("limitations")),
        "identity_login": login if isinstance(login, str) and _LOGIN.fullmatch(login) else None,
        "model_context": model_context if model_context in {"included", "not_allowed", "not_sent"} else None,
        "counts": counts,
        "repositories": [repo for repo in (archived.get("repositories") or [])[:20]
                         if isinstance(repo, str) and _REPOSITORY.fullmatch(repo)]
        if isinstance(archived.get("repositories"), list) else [],
        "items": items,
    }


def _refs(value: Any, pattern: re.Pattern[str], limit: int = _GITHUB_ITEMS_MAX) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and pattern.fullmatch(item)][:limit]


def _public_goal_evidence(report: dict[str, Any] | None) -> dict[str, Any] | None:
    archived = report.get("goal_evidence") if isinstance(report, dict) else None
    if not isinstance(archived, dict) or archived.get("status") not in _GOAL_EVIDENCE_STATUSES:
        return None
    goals = []
    for goal in (archived.get("goals") or [])[:_GOALS_MAX] if isinstance(archived.get("goals"), list) else []:
        if not isinstance(goal, dict) or not isinstance(goal.get("ref"), str) or not _A_REF.fullmatch(goal["ref"]):
            continue
        parent = goal.get("parent_ref")
        goals.append({
            "ref": goal["ref"], "title": _text(goal.get("title"), 120) or "",
            "level": _text(goal.get("level"), 32) or "", "domain": _text(goal.get("domain"), 32) or "",
            "parent_ref": parent if isinstance(parent, str) and _A_REF.fullmatch(parent) else None,
            "github": _refs(goal.get("github"), _G_REF), "screen": goal.get("screen") is True,
            "rolled_up_from": _refs(goal.get("rolled_up_from"), _A_REF, _GOALS_MAX),
        })
    rejected = archived.get("rejected") if isinstance(archived.get("rejected"), dict) else {}
    return {
        "status": archived["status"], "goals": goals,
        "goals_without_evidence": _refs(archived.get("goals_without_evidence"), _A_REF, _GOALS_MAX),
        "github_without_goal": _refs(archived.get("github_without_goal"), _G_REF),
        "rejected": {key: _non_negative_int(rejected.get(key))
                     for key in ("unknown_evidence", "unknown_goals", "urls_removed")},
    }
