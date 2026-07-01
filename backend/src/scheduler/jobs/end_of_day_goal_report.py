"""End-of-day goal report built from screen observations and goals."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import smtplib
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from email.message import EmailMessage
from html import escape
from pathlib import Path
from time import perf_counter
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from config.settings import settings
from src.audit.runtime import log_integration_event, log_scheduler_job_event
from src.db.models import GoalStatus, MemoryEpisode, MemoryEpisodeType, ScreenObservation
from src.llm_runtime import completion_with_fallback
from src.scheduler.screen_llm_policy import screen_derived_llm_decision
from src.observer.image_metadata import image_metadata_label

logger = logging.getLogger(__name__)

_REPORT_PROMPT = """\
You are Seraph. Write an end-of-day report for the human.

Use only the stored LLM-authored screenshot digests, observation metadata, and goals.
Do not invent screen details. If sensitive content appears, summarize it safely instead of copying it.

## Date
{report_date}

## Activity Summary
- Total tracked time: {total_minutes} minutes
- Context switches: {switch_count}
- Activity mix:
{activity_breakdown}
- Project mix:
{project_breakdown}
- Source mix:
{source_breakdown}
## Screenshot Digest Text
{screenshot_digest_text}

## Goals
Active goals:
{active_goals}

Completed today:
{completed_goals}

Write:
1. A short summary of what the day was mostly about.
2. A "Goals vs day" section that critically says what aligned, what drifted, what was blocked, what is unclear, and whether the user pushed the needle.
3. 3 practical tips for tomorrow.

Keep it useful and concise. No private raw screen text, no copied secrets, no long logs."""

_SCREENSHOT_DIGEST_TOOL_NAME = "screenshot_observation_digest"
_SCREENSHOT_DIGEST_SCHEMA_VERSION = "seraph.screenshot_observation_digest.v1"


@dataclass(frozen=True)
class EmailDeliveryResult:
    status: str
    reason: str | None = None
    recipient_hash: str | None = None
    provider_receipt: str | None = None


def _parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _recipient_hash(recipient: str) -> str:
    return hashlib.sha256(recipient.strip().lower().encode("utf-8")).hexdigest()[:16]


def _report_archive_root() -> Path:
    configured = settings.report_archive_dir.strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(settings.workspace_dir).expanduser().resolve() / "artifacts" / "reports"


def _report_receipt_dir(report_date: str) -> Path:
    return _report_archive_root() / "receipts" / report_date


def _report_artifact_paths(report: dict[str, Any]) -> dict[str, str]:
    report_date = str(report.get("date") or "unknown")
    digest = hashlib.sha256(
        json.dumps(report, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    report_dir = _report_archive_root() / "end-of-day" / report_date
    stem = f"end-of-day-{report_date}-{digest}"
    return {
        "report_text_path": str(report_dir / f"{stem}.txt"),
        "report_json_path": str(report_dir / f"{stem}.json"),
    }


def archive_end_of_day_goal_report(report: dict[str, Any]) -> dict[str, Any]:
    """Persist a durable report artifact bundle for future re-analysis."""
    paths = _report_artifact_paths(report)
    text_path = Path(paths["report_text_path"])
    json_path = Path(paths["report_json_path"])
    text_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    text_path.parent.chmod(0o700)

    text_payload = str(report.get("body") or "")
    json_payload = {
        "artifact_schema": "seraph.end_of_day_goal_report.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "report": report,
    }
    _write_private_text(text_path, text_payload)
    _write_private_text(json_path, json.dumps(json_payload, indent=2, sort_keys=True))

    return {
        **paths,
        "report_text_sha256": hashlib.sha256(text_payload.encode("utf-8")).hexdigest(),
        "report_json_sha256": hashlib.sha256(json_path.read_bytes()).hexdigest(),
    }


def archive_end_of_day_report_receipt(
    *,
    action: str,
    report: dict[str, Any] | None = None,
    episode_id: str | None = None,
    email_result: EmailDeliveryResult | None = None,
    status: str = "succeeded",
    reason: str | None = None,
) -> dict[str, Any]:
    """Persist a private operator receipt for manual/scheduled report actions."""
    report_date = str((report or {}).get("date") or datetime.now(timezone.utc).date().isoformat())
    created_at = datetime.now(timezone.utc).isoformat()
    digest_input = json.dumps(
        {
            "action": action,
            "created_at": created_at,
            "report_date": report_date,
            "episode_id": episode_id,
            "email_status": email_result.status if email_result else None,
        },
        sort_keys=True,
    )
    digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:16]
    receipt_dir = _report_receipt_dir(report_date)
    receipt_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    receipt_dir.chmod(0o700)
    receipt_path = receipt_dir / f"{action}-{report_date}-{digest}.json"
    payload = {
        "artifact_schema": "seraph.end_of_day_report_receipt.v1",
        "created_at": created_at,
        "action": action,
        "status": status,
        "reason": reason,
        "report_date": report_date,
        "episode_id": episode_id,
        "report_artifacts": (report or {}).get("artifacts"),
        "analysis_provider": (report or {}).get("analysis_provider"),
        "email": {
            "status": email_result.status if email_result else None,
            "reason": email_result.reason if email_result else None,
            "recipient_hash": email_result.recipient_hash if email_result else None,
            "provider_receipt": email_result.provider_receipt if email_result else None,
        },
    }
    _write_private_text(receipt_path, json.dumps(payload, indent=2, sort_keys=True))
    return {
        "receipt_id": receipt_path.stem,
        "receipt_path": str(receipt_path),
        "receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
        "status": status,
        "reason": reason,
    }


def public_report_artifacts(artifacts: dict[str, Any] | None) -> dict[str, Any]:
    """Return report artifact metadata without exposing private filesystem paths."""
    if not isinstance(artifacts, dict):
        return {}
    return {
        "report_text_sha256": artifacts.get("report_text_sha256"),
        "report_json_sha256": artifacts.get("report_json_sha256"),
        "raw_artifact_path_exposed": False,
    }


def public_report_receipt(receipt: dict[str, Any] | None) -> dict[str, Any]:
    """Return receipt metadata that is safe for API/audit surfaces."""
    if not isinstance(receipt, dict):
        return {}
    return {
        "receipt_id": receipt.get("receipt_id"),
        "receipt_sha256": receipt.get("receipt_sha256"),
        "status": receipt.get("status"),
        "reason": receipt.get("reason"),
        "raw_receipt_path_exposed": False,
    }


def _write_private_text(path: Path, content: str) -> None:
    fd = open(
        path,
        "w",
        encoding="utf-8",
        opener=lambda file_path, flags: os.open(file_path, flags, 0o600),
    )
    try:
        fd.write(content)
    finally:
        fd.close()
        path.chmod(0o600)


def _safe_timezone() -> ZoneInfo:
    try:
        return ZoneInfo(settings.user_timezone)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


async def _screen_summary_for_local_day(report_day: date, tz: ZoneInfo) -> dict[str, Any]:
    local_start = datetime.combine(report_day, time.min, tzinfo=tz)
    local_end = datetime.combine(report_day, time.max, tzinfo=tz)
    start_utc = local_start.astimezone(timezone.utc)
    end_utc = local_end.astimezone(timezone.utc)

    from sqlmodel import col, select
    from src.db.engine import get_session

    async with get_session() as db:
        result = await db.execute(
            select(ScreenObservation)
            .where(col(ScreenObservation.timestamp) >= start_utc)
            .where(col(ScreenObservation.timestamp) <= end_utc)
            .where(col(ScreenObservation.blocked) == False)  # noqa: E712
            .order_by(col(ScreenObservation.timestamp))
        )
        observations = list(result.scalars().all())

    by_activity: dict[str, int] = {}
    by_project: dict[str, int] = {}
    by_app: dict[str, int] = {}
    by_source: dict[str, int] = {}
    source_observations: dict[str, int] = {}
    details: list[str] = []
    screenshot_samples: list[str] = []
    total_seconds = 0
    for index, obs in enumerate(observations):
        source = _observation_source(obs)
        next_obs = observations[index + 1] if index + 1 < len(observations) else None
        duration = _observation_report_duration(obs, next_obs=next_obs, source=source)
        total_seconds += duration
        by_activity[obs.activity_type] = by_activity.get(obs.activity_type, 0) + duration
        by_app[obs.app_name] = by_app.get(obs.app_name, 0) + duration
        by_source[source] = by_source.get(source, 0) + duration
        source_observations[source] = source_observations.get(source, 0) + 1
        if obs.project:
            by_project[obs.project] = by_project.get(obs.project, 0) + duration
        if obs.summary and obs.summary not in details:
            details.append(obs.summary)
        screenshot_sample = _screenshot_image_sample(obs)
        if screenshot_sample and screenshot_sample not in screenshot_samples:
            screenshot_samples.append(screenshot_sample)

    return {
        "date": report_day.isoformat(),
        "timezone": str(tz),
        "total_observations": len(observations),
        "total_tracked_minutes": total_seconds // 60,
        "switch_count": len(observations),
        "by_activity": dict(sorted(by_activity.items(), key=lambda item: -item[1])),
        "by_project": dict(sorted(by_project.items(), key=lambda item: -item[1])),
        "by_app": dict(sorted(by_app.items(), key=lambda item: -item[1])),
        "by_source": dict(sorted(by_source.items(), key=lambda item: -item[1])),
        "source_observations": dict(
            sorted(source_observations.items(), key=lambda item: (-item[1], item[0]))
        ),
        "samples": details[:8],
        "screenshot_samples": screenshot_samples[:8],
    }


async def _screenshot_digests_for_local_day(report_day: date, tz: ZoneInfo) -> dict[str, Any]:
    local_start = datetime.combine(report_day, time.min, tzinfo=tz)
    local_end = datetime.combine(report_day, time.max, tzinfo=tz)
    start_utc = local_start.astimezone(timezone.utc)
    end_utc = local_end.astimezone(timezone.utc)

    from sqlmodel import col, select
    from src.db.engine import get_session

    async with get_session() as db:
        result = await db.execute(
            select(MemoryEpisode)
            .where(col(MemoryEpisode.source_tool_name) == _SCREENSHOT_DIGEST_TOOL_NAME)
            .where(col(MemoryEpisode.observed_at) >= start_utc)
            .where(col(MemoryEpisode.observed_at) <= end_utc)
            .order_by(col(MemoryEpisode.observed_at))
        )
        episodes = list(result.scalars().all())

    digests: list[dict[str, Any]] = []
    observation_ids: list[str] = []
    text_chunks: list[str] = []
    for episode in episodes:
        metadata = _episode_metadata(episode)
        if metadata.get("artifact_schema") != _SCREENSHOT_DIGEST_SCHEMA_VERSION:
            continue
        ids = [str(item) for item in metadata.get("observation_ids", []) if item]
        observation_ids.extend(ids)
        content = str(episode.content or "")[:1400]
        digests.append(
            {
                "episode_id": episode.id,
                "window_start": metadata.get("window_start"),
                "window_end": metadata.get("window_end"),
                "observation_count": int(metadata.get("observation_count") or len(ids)),
                "observation_ids": ids,
                "content": content,
            }
        )
        if content:
            text_chunks.append(content)

    return {
        "count": len(digests),
        "observation_ids": _unique_values(observation_ids)[:100],
        "digests": digests[:16],
        "digest_text": "\n\n".join(text_chunks)[:8000],
    }


def _observation_source(observation: ScreenObservation) -> str:
    if observation.details_json:
        try:
            details = json.loads(observation.details_json)
        except json.JSONDecodeError:
            details = []
        if isinstance(details, list):
            for item in details:
                if isinstance(item, str) and item.startswith("capture_artifacts:"):
                    try:
                        artifacts = json.loads(item.removeprefix("capture_artifacts:"))
                    except json.JSONDecodeError:
                        continue
                    if isinstance(artifacts, dict):
                        provider = str(artifacts.get("provider") or artifacts.get("source") or "").strip()
                        if provider == "screenshot_folder":
                            return provider
    if observation.app_name == "Screenshot Folder":
        return "screenshot_folder"
    return "observer_daemon"


def _observation_report_duration(
    observation: ScreenObservation,
    *,
    next_obs: ScreenObservation | None,
    source: str,
) -> int:
    if source != "screenshot_folder":
        return max(int(observation.duration_s or 0), 0)
    if next_obs is None:
        return 0
    current_ts = _ensure_aware_utc(observation.timestamp)
    next_ts = _ensure_aware_utc(next_obs.timestamp)
    delta = int((next_ts - current_ts).total_seconds())
    if delta <= 0:
        return 0
    return min(delta, 5 * 60)


def _ensure_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _screenshot_image_sample(observation: ScreenObservation) -> str | None:
    if not observation.details_json:
        return None
    try:
        details = json.loads(observation.details_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(details, list):
        return None
    for item in details:
        if not (isinstance(item, str) and item.startswith("capture_artifacts:")):
            continue
        try:
            artifacts = json.loads(item.removeprefix("capture_artifacts:"))
        except json.JSONDecodeError:
            continue
        if not isinstance(artifacts, dict):
            continue
        provider = str(artifacts.get("provider") or artifacts.get("source") or "").strip()
        if provider != "screenshot_folder":
            continue
        image_name = Path(str(artifacts.get("image_path") or observation.window_title or "screenshot")).name
        metadata_label = image_metadata_label(
            {
                "file_format": artifacts.get("file_format"),
                "width": artifacts.get("width"),
                "height": artifacts.get("height"),
                "image_bytes": artifacts.get("image_bytes"),
            }
        )
        label = f"{image_name} ({metadata_label})" if metadata_label else image_name
        return label[:160]
    return None


async def _goals_for_report(report_day: date) -> tuple[list[Any], list[Any]]:
    from src.goals.repository import goal_repository

    active_goals, completed_goals = await asyncio.gather(
        goal_repository.list_goals(status=GoalStatus.active.value),
        goal_repository.list_goals(status=GoalStatus.completed.value),
    )
    completed_today = [
        goal for goal in completed_goals
        if goal.updated_at and goal.updated_at.date() == report_day
    ]
    return active_goals, completed_today


def _format_seconds_map(values: dict[str, int], *, empty: str) -> str:
    if not values:
        return empty
    lines = []
    for name, seconds in list(values.items())[:8]:
        lines.append(f"- {str(name)[:80]}: {seconds // 60}m")
    return "\n".join(lines)


def _format_source_mix(
    seconds_by_source: dict[str, int],
    counts_by_source: dict[str, int],
    *,
    empty: str,
) -> str:
    if not seconds_by_source and not counts_by_source:
        return empty
    names = sorted(
        set(seconds_by_source) | set(counts_by_source),
        key=lambda name: (-counts_by_source.get(name, 0), -seconds_by_source.get(name, 0), name),
    )
    lines = []
    for name in names[:8]:
        count = counts_by_source.get(name, 0)
        noun = "observation" if count == 1 else "observations"
        lines.append(
            f"- {str(name)[:80]}: {count} {noun}, "
            f"{seconds_by_source.get(name, 0) // 60}m"
        )
    return "\n".join(lines)


def _format_goals(goals: list[Any]) -> str:
    if not goals:
        return "- None"
    return "\n".join(
        f"- {str(goal.title)[:120]} ({goal.domain})"
        for goal in goals[:12]
    )


def _html_lines(text: str) -> str:
    clean = str(text or "")[:7000]
    return "<br>".join(escape(line) for line in clean.splitlines())


def _report_email_html(report: dict[str, Any]) -> str:
    """Render a self-contained HTML email while keeping plain text as fallback."""
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    screenshot_digests = (
        report.get("screenshot_digests") if isinstance(report.get("screenshot_digests"), dict) else {}
    )
    date_label = escape(str(report.get("date") or "unknown"))
    timezone_label = escape(str(report.get("timezone") or "local"))
    provider_label = escape(str(report.get("analysis_provider") or "local"))
    total_minutes = int(summary.get("total_tracked_minutes") or 0)
    switch_count = int(summary.get("switch_count") or 0)
    observation_count = int(summary.get("total_observations") or 0)
    digest_count = int(screenshot_digests.get("count") or 0)
    body_html = _html_lines(str(report.get("body") or "No report generated."))

    stat_cards = [
        ("Tracked", f"{total_minutes}m"),
        ("Switches", str(switch_count)),
        ("Screens", str(observation_count)),
        ("Digests", str(digest_count)),
    ]
    stat_html = "".join(
        f"""
        <td style="width:25%;padding:8px;">
          <div style="border:1px solid #1f4f63;background:#081923;padding:14px 12px;">
            <div style="font-size:11px;letter-spacing:0.08em;text-transform:uppercase;color:#7fb4c7;">{escape(label)}</div>
            <div style="font-size:24px;line-height:1.2;color:#e8fbff;font-weight:700;margin-top:6px;">{escape(value)}</div>
          </div>
        </td>
        """
        for label, value in stat_cards
    )

    return f"""\
<!doctype html>
<html>
  <body style="margin:0;background:#06111a;color:#d7eef5;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Arial,sans-serif;">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#06111a;padding:28px 14px;">
      <tr>
        <td align="center">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:760px;border:1px solid #1e4150;background:#071722;">
            <tr>
              <td style="padding:24px 24px 12px;border-bottom:1px solid #1e4150;">
                <div style="font-size:12px;letter-spacing:0.12em;text-transform:uppercase;color:#66ffc8;font-weight:700;">Seraph</div>
                <h1 style="margin:8px 0 4px;font-size:28px;line-height:1.15;color:#f2fdff;font-weight:800;">End-of-day report</h1>
                <div style="font-size:13px;color:#8fb5c4;">{date_label} · {timezone_label} · {provider_label}</div>
              </td>
            </tr>
            <tr>
              <td style="padding:14px 16px 4px;">
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr>{stat_html}</tr></table>
              </td>
            </tr>
            <tr>
              <td style="padding:16px 24px 28px;">
                <div style="border-top:1px solid #1e4150;padding-top:18px;font-size:15px;line-height:1.62;color:#d7eef5;">
                  {body_html}
                </div>
              </td>
            </tr>
            <tr>
              <td style="padding:14px 24px;border-top:1px solid #1e4150;color:#6e95a5;font-size:12px;">
                Local-first report generated from Seraph observations. Raw screenshots are not attached.
              </td>
            </tr>
          </table>
        </td>
      </tr>
    </table>
  </body>
</html>
"""


def _template_value(value: Any, *, limit: int = 2000) -> str | int:
    if isinstance(value, int):
        return value
    return str(value or "")[:limit]


def _resend_api_key() -> str:
    if settings.resend_api_key.strip():
        return settings.resend_api_key.strip()
    if (
        settings.smtp_host.strip().lower() == "smtp.resend.com"
        and settings.smtp_username.strip().lower() == "resend"
    ):
        return settings.smtp_password.strip()
    return ""


def _resend_template_variables(report: dict[str, Any]) -> dict[str, str | int]:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    screenshot_digests = (
        report.get("screenshot_digests") if isinstance(report.get("screenshot_digests"), dict) else {}
    )
    return {
        "REPORT_DATE": _template_value(report.get("date")),
        "TIMEZONE": _template_value(report.get("timezone")),
        "ANALYSIS_PROVIDER": _template_value(report.get("analysis_provider")),
        "TOTAL_TRACKED_MINUTES": int(summary.get("total_tracked_minutes") or 0),
        "CONTEXT_SWITCHES": int(summary.get("switch_count") or 0),
        "SCREEN_OBSERVATIONS": int(summary.get("total_observations") or 0),
        "SCREENSHOT_DIGESTS": int(screenshot_digests.get("count") or 0),
        "REPORT_BODY": _template_value(report.get("body"), limit=2000),
    }


async def _deliver_report_resend_template(
    report: dict[str, Any],
    *,
    recipient: str,
    sender: str,
    subject: str,
) -> EmailDeliveryResult:
    api_key = _resend_api_key()
    if not api_key:
        return EmailDeliveryResult(status="skipped", reason="resend_api_key_missing")
    payload: dict[str, Any] = {
        "from": sender,
        "to": [recipient],
        "subject": subject,
        "template": {
            "id": settings.resend_template_id.strip(),
            "variables": _resend_template_variables(report),
        },
    }
    if settings.email_reports_reply_to.strip():
        payload["reply_to"] = settings.email_reports_reply_to.strip()

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            settings.resend_api_url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
        response.raise_for_status()
    response_payload = response.json()
    provider_receipt = response_payload.get("id") if isinstance(response_payload, dict) else None
    return EmailDeliveryResult(status="sent", provider_receipt=provider_receipt)


def _episode_metadata(episode: MemoryEpisode) -> dict[str, Any]:
    try:
        payload = json.loads(episode.metadata_json or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _unique_values(values: list[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        text = str(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _report_llm_label(runtime_profile: str) -> str:
    model = (
        settings.local_model.strip()
        or settings.local_vlm_model.strip()
        or settings.default_model.strip()
    )
    if runtime_profile:
        return f"{runtime_profile}:{model}"
    return model


async def build_end_of_day_goal_report(report_day: date | None = None) -> dict[str, Any]:
    tz = _safe_timezone()
    target_day = report_day or datetime.now(tz).date()
    summary, screenshot_digests, goal_results = await asyncio.gather(
        _screen_summary_for_local_day(target_day, tz),
        _screenshot_digests_for_local_day(target_day, tz),
        _goals_for_report(target_day),
    )
    active_goals, completed_goals = goal_results
    decision = screen_derived_llm_decision("end_of_day_goal_report")
    if not decision.allowed:
        body = (
            "End-of-day report LLM generation was blocked by Seraph's screen-data privacy policy. "
            f"Reason: {decision.reason}. Configure a verified local runtime profile or explicitly allow remote routing."
        )
        return {
            "date": target_day.isoformat(),
            "timezone": str(tz),
            "body": body,
            "summary": summary,
            "screenshot_digests": screenshot_digests,
            "goal_alignment": [],
            "completed_goal_count": len(completed_goals),
            "active_goal_count": len(active_goals),
            "analysis_provider": f"blocked:{decision.reason}",
            "artifact_schema": "seraph.end_of_day_goal_report.v1",
        }

    prompt = _REPORT_PROMPT.format(
        report_date=target_day.isoformat(),
        total_minutes=summary.get("total_tracked_minutes", 0),
        switch_count=summary.get("switch_count", 0),
        activity_breakdown=_format_seconds_map(summary.get("by_activity", {}), empty="- No tracked activity"),
        project_breakdown=_format_seconds_map(summary.get("by_project", {}), empty="- No project labels detected"),
        source_breakdown=_format_source_mix(
            summary.get("by_source", {}),
            summary.get("source_observations", {}),
            empty="- No observation sources detected",
        ),
        screenshot_digest_text=str(screenshot_digests.get("digest_text") or ""),
        active_goals=_format_goals(active_goals),
        completed_goals=_format_goals(completed_goals),
    )

    response = await completion_with_fallback(
        messages=[{"role": "user", "content": prompt}],
        temperature=0.4,
        max_tokens=900,
        timeout=settings.agent_briefing_timeout,
        runtime_path="end_of_day_goal_report",
        local_runtime_only=not settings.screen_derived_llm_allow_remote,
    )
    body = str(response.choices[0].message.content or "").strip()
    if not body:
        body = "No report generated."

    return {
        "date": target_day.isoformat(),
        "timezone": str(tz),
        "body": body,
        "summary": summary,
        "screenshot_digests": screenshot_digests,
        "goal_alignment": [],
        "completed_goal_count": len(completed_goals),
        "active_goal_count": len(active_goals),
        "analysis_provider": f"llm:{_report_llm_label(decision.runtime_profile)}",
        "artifact_schema": "seraph.end_of_day_goal_report.v1",
    }


async def store_end_of_day_goal_report(report: dict[str, Any]) -> str:
    from src.memory.repository import memory_repository

    report_artifacts = archive_end_of_day_goal_report(report)
    report["artifacts"] = report_artifacts
    episode = await memory_repository.create_episode(
        episode_type=MemoryEpisodeType.observer,
        summary=f"End-of-day goal report for {report['date']}",
        content=report["body"],
        source_tool_name="end_of_day_goal_report",
        salience=0.7,
        confidence=0.8,
        metadata={
            "kind": "end_of_day_goal_report",
            "date": report["date"],
            "timezone": report["timezone"],
            "screen_observation_count": report["summary"].get("total_observations", 0),
            "screenshot_digest_count": (report.get("screenshot_digests") or {}).get("count", 0),
            "screenshot_digest_observation_ids": (report.get("screenshot_digests") or {}).get(
                "observation_ids", []
            ),
            "total_tracked_minutes": report["summary"].get("total_tracked_minutes", 0),
            "active_goal_count": report["active_goal_count"],
            "completed_goal_count": report["completed_goal_count"],
            "goal_alignment": report["goal_alignment"],
            "analysis_provider": report.get("analysis_provider"),
            "artifact_schema": report.get("artifact_schema"),
            "artifacts": report_artifacts,
        },
    )
    return episode.id


async def deliver_report_email(
    report: dict[str, Any],
    *,
    preview_acknowledged: bool = False,
) -> EmailDeliveryResult:
    recipient = settings.email_reports_to.strip()
    sender = settings.email_reports_from.strip()

    if not settings.email_reports_enabled:
        return EmailDeliveryResult(status="skipped", reason="email_reports_disabled")
    if settings.email_reports_preview_required and not preview_acknowledged:
        return EmailDeliveryResult(status="preview_required", reason="operator_preview_required")
    if not recipient or not sender:
        return EmailDeliveryResult(status="skipped", reason="missing_recipient_or_sender")
    allowed = {_recipient_hash(item) for item in _parse_csv(settings.email_reports_to_allowlist)}
    recipient_digest = _recipient_hash(recipient)
    if recipient_digest not in allowed:
        return EmailDeliveryResult(
            status="blocked",
            reason="recipient_not_allowlisted",
            recipient_hash=recipient_digest,
        )
    subject = f"Seraph end-of-day report: {report['date']}"
    if settings.resend_template_id.strip():
        try:
            result = await _deliver_report_resend_template(
                report,
                recipient=recipient,
                sender=sender,
                subject=subject,
            )
        except Exception as exc:
            await log_integration_event(
                integration_type="email_report",
                name="resend_template",
                outcome="failed",
                details={"reason": exc.__class__.__name__, "recipient_hash": recipient_digest},
            )
            raise
        await log_integration_event(
            integration_type="email_report",
            name="resend_template",
            outcome="succeeded" if result.status == "sent" else "skipped",
            details={
                "reason": result.reason,
                "recipient_hash": recipient_digest,
                "provider_receipt": result.provider_receipt,
            },
        )
        return EmailDeliveryResult(
            status=result.status,
            reason=result.reason,
            recipient_hash=recipient_digest,
            provider_receipt=result.provider_receipt,
        )

    if not settings.smtp_host:
        return EmailDeliveryResult(
            status="skipped",
            reason="smtp_host_missing",
            recipient_hash=recipient_digest,
        )

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = recipient
    if settings.email_reports_reply_to:
        message["Reply-To"] = settings.email_reports_reply_to
    message.set_content(report["body"])
    message.add_alternative(_report_email_html(report), subtype="html")

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as smtp:
            if settings.smtp_use_tls:
                smtp.starttls()
            if settings.smtp_username or settings.smtp_password:
                smtp.login(settings.smtp_username, settings.smtp_password)
            smtp.send_message(message)
    except Exception as exc:
        await log_integration_event(
            integration_type="email_report",
            name="smtp",
            outcome="failed",
            details={"reason": exc.__class__.__name__, "recipient_hash": recipient_digest},
        )
        raise

    await log_integration_event(
        integration_type="email_report",
        name="smtp",
        outcome="succeeded",
        details={"recipient_hash": recipient_digest},
    )
    return EmailDeliveryResult(status="sent", recipient_hash=recipient_digest)


async def run_manual_end_of_day_goal_report(
    *,
    send_email: bool = False,
    preview_acknowledged: bool = False,
    report_day: date | None = None,
) -> dict[str, Any]:
    report = await build_end_of_day_goal_report(report_day)
    episode_id = await store_end_of_day_goal_report(report)
    email_result = (
        await deliver_report_email(report, preview_acknowledged=preview_acknowledged)
        if send_email
        else EmailDeliveryResult(status="preview_only", reason="manual_preview")
    )
    receipt = archive_end_of_day_report_receipt(
        action="manual-send" if send_email else "manual-preview",
        report=report,
        episode_id=episode_id,
        email_result=email_result,
    )
    return {
        "status": "ok",
        "action": "manual-send" if send_email else "manual-preview",
        "report": {
            "date": report["date"],
            "timezone": report["timezone"],
            "body": report["body"],
            "summary": report["summary"],
            "goal_alignment": report["goal_alignment"],
            "analysis_provider": report.get("analysis_provider"),
            "artifacts": public_report_artifacts(report.get("artifacts")),
        },
        "episode_id": episode_id,
        "email": {
            "status": email_result.status,
            "reason": email_result.reason,
            "recipient_hash": email_result.recipient_hash,
        },
        "receipt": public_report_receipt(receipt),
    }


async def send_end_of_day_report_test_email() -> dict[str, Any]:
    test_report = {
        "date": datetime.now(_safe_timezone()).date().isoformat(),
        "timezone": str(_safe_timezone()),
        "body": "Seraph test email: end-of-day report delivery is configured.",
        "analysis_provider": "test-email",
        "artifact_schema": "seraph.end_of_day_report_test_email.v1",
    }
    email_result = await deliver_report_email(test_report, preview_acknowledged=True)
    receipt = archive_end_of_day_report_receipt(
        action="test-email",
        report=test_report,
        email_result=email_result,
        status=email_result.status,
        reason=email_result.reason,
    )
    return {
        "status": email_result.status,
        "reason": email_result.reason,
        "recipient_hash": email_result.recipient_hash,
        "receipt": public_report_receipt(receipt),
    }


async def run_end_of_day_goal_report() -> None:
    if not settings.end_of_day_report_enabled:
        await log_scheduler_job_event(
            job_name="end_of_day_goal_report",
            outcome="skipped",
            details={"reason": "disabled"},
        )
        return

    started_at = perf_counter()
    try:
        report = await build_end_of_day_goal_report()
        episode_id = await store_end_of_day_goal_report(report)
        email_result = await deliver_report_email(report)
        receipt = archive_end_of_day_report_receipt(
            action="scheduled",
            report=report,
            episode_id=episode_id,
            email_result=email_result,
        )
        await log_scheduler_job_event(
            job_name="end_of_day_goal_report",
            outcome="succeeded",
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "episode_id": episode_id,
                "date": report["date"],
                "total_tracked_minutes": report["summary"].get("total_tracked_minutes", 0),
                "active_goal_count": report["active_goal_count"],
                "completed_goal_count": report["completed_goal_count"],
                "email_status": email_result.status,
                "email_reason": email_result.reason,
                "email_recipient_hash": email_result.recipient_hash,
                "report_artifacts": public_report_artifacts(report.get("artifacts")),
                "report_receipt": public_report_receipt(receipt),
            },
        )
    except Exception as exc:
        await log_scheduler_job_event(
            job_name="end_of_day_goal_report",
            outcome="failed",
            details={
                "duration_ms": int((perf_counter() - started_at) * 1000),
                "error": exc.__class__.__name__,
            },
        )
        logger.exception("end_of_day_goal_report failed")
