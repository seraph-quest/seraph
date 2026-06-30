"""End-of-day goal report built from screen observations and goals."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import smtplib
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from email.message import EmailMessage
from pathlib import Path
from time import perf_counter
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from config.settings import settings
from src.audit.runtime import log_integration_event, log_scheduler_job_event
from src.db.models import GoalStatus, MemoryEpisodeType, ScreenObservation
from src.llm_runtime import completion_with_fallback

logger = logging.getLogger(__name__)

_REPORT_PROMPT = """\
You are Seraph. Write an end-of-day report for the human.

Use only the redacted structured inputs. Do not invent screen details.

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

## Goals
Active goals:
{active_goals}

Completed today:
{completed_goals}

## Alignment Hints
{alignment_hints}

Write:
1. A short summary of what the day was mostly about.
2. A "Goals vs day" section that says what aligned, what drifted, and what is unclear.
3. 3 practical tips for tomorrow.

Keep it useful and concise. No private raw screen text, no copied secrets, no long logs."""

_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*[^\s,;]+"),
    re.compile(r"\b(sk-[A-Za-z0-9_-]{12,})\b"),
    re.compile(r"\b([A-Za-z0-9_./+=-]{24,})\b"),
)


@dataclass(frozen=True)
class EmailDeliveryResult:
    status: str
    reason: str | None = None
    recipient_hash: str | None = None
    provider_receipt: str | None = None


def _redact_text(value: str | None, *, limit: int = 240) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    text = " ".join(value.strip().split())
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return text[:limit]


def _redact_report_body(value: str | None, *, limit: int = 5000) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    lines = []
    for line in value.strip().splitlines():
        text = " ".join(line.strip().split())
        for pattern in _SECRET_PATTERNS:
            text = pattern.sub("[redacted]", text)
        lines.append(text)
    return "\n".join(lines)[:limit]


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
    total_seconds = 0
    for obs in observations:
        duration = obs.duration_s or 0
        source = _observation_source(obs)
        total_seconds += duration
        by_activity[obs.activity_type] = by_activity.get(obs.activity_type, 0) + duration
        by_app[obs.app_name] = by_app.get(obs.app_name, 0) + duration
        by_source[source] = by_source.get(source, 0) + duration
        source_observations[source] = source_observations.get(source, 0) + 1
        if obs.project:
            by_project[obs.project] = by_project.get(obs.project, 0) + duration
        if obs.summary:
            redacted = _redact_text(obs.summary, limit=160)
            if redacted and redacted not in details:
                details.append(redacted)

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
        "redacted_samples": details[:8],
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
                        if provider:
                            return provider
    if observation.app_name == "Screenshot Folder":
        return "screenshot_folder"
    if observation.app_name == "Framekeeper":
        return "framekeeper"
    return "observer_daemon"


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


def _tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]{3,}", value.lower())
        if token not in {"the", "and", "for", "with", "today", "goal"}
    }


def _goal_alignment(summary: dict[str, Any], active_goals: list[Any]) -> list[dict[str, Any]]:
    context_text = " ".join(
        [
            " ".join(summary.get("by_project", {}).keys()),
            " ".join(summary.get("by_activity", {}).keys()),
            " ".join(summary.get("by_app", {}).keys()),
            " ".join(summary.get("redacted_samples", [])),
        ]
    )
    context_tokens = _tokens(context_text)
    results: list[dict[str, Any]] = []
    for goal in active_goals:
        goal_text = " ".join(
            item for item in [goal.title, goal.description or "", goal.domain] if item
        )
        overlap = sorted(_tokens(goal_text) & context_tokens)
        results.append(
            {
                "goal_id": goal.id,
                "title": _redact_text(goal.title, limit=120),
                "domain": goal.domain,
                "status": "aligned" if overlap else "unclear",
                "evidence_tokens": overlap[:6],
            }
        )
    return results


def _format_seconds_map(values: dict[str, int], *, empty: str) -> str:
    if not values:
        return empty
    lines = []
    for name, seconds in list(values.items())[:8]:
        lines.append(f"- {_redact_text(name, limit=80)}: {seconds // 60}m")
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
            f"- {_redact_text(name, limit=80)}: {count} {noun}, "
            f"{seconds_by_source.get(name, 0) // 60}m"
        )
    return "\n".join(lines)


def _format_goals(goals: list[Any]) -> str:
    if not goals:
        return "- None"
    return "\n".join(
        f"- {_redact_text(goal.title, limit=120)} ({goal.domain})"
        for goal in goals[:12]
    )


def _format_alignment(alignment: list[dict[str, Any]]) -> str:
    if not alignment:
        return "- No active goals to compare."
    lines = []
    for item in alignment[:12]:
        evidence = ", ".join(item["evidence_tokens"]) or "no clear screen-observation match"
        lines.append(f"- {item['title']}: {item['status']} ({evidence})")
    return "\n".join(lines)


def _deterministic_report_body(
    *,
    report_day: date,
    summary: dict[str, Any],
    alignment: list[dict[str, Any]],
    completed_goals: list[Any],
) -> str:
    activity = _format_seconds_map(summary.get("by_activity", {}), empty="- No tracked activity")
    projects = _format_seconds_map(summary.get("by_project", {}), empty="- No project labels detected")
    sources = _format_source_mix(
        summary.get("by_source", {}),
        summary.get("source_observations", {}),
        empty="- No observation sources detected",
    )
    aligned = [item for item in alignment if item["status"] == "aligned"]
    unclear = [item for item in alignment if item["status"] != "aligned"]
    completed = _format_goals(completed_goals)

    tips = [
        "Start tomorrow by naming the one goal the first focus block should serve.",
        "Keep high-switching work in a deliberate admin block so deep work stays protected.",
        "If a goal stayed unclear today, add a visible project label or next action before starting.",
    ]
    if summary.get("total_tracked_minutes", 0) == 0:
        tips[0] = "Start tomorrow by setting a concrete daily goal before opening work apps."
    elif not summary.get("by_project"):
        tips[2] = "Add clearer project labels or goal titles so Seraph can connect screen activity to commitments."

    return "\n".join(
        [
            f"End-of-day report for {report_day.isoformat()}",
            "",
            f"Today had {summary.get('total_tracked_minutes', 0)} tracked minutes across "
            f"{summary.get('switch_count', 0)} context switches.",
            "",
            "Activity mix:",
            activity,
            "",
            "Project mix:",
            projects,
            "",
            "Source mix:",
            sources,
            "",
            "Goals vs day:",
            f"- Aligned goals: {len(aligned)}",
            f"- Unclear goals: {len(unclear)}",
            f"- Completed today: {len(completed_goals)}",
            completed,
            "",
            "Useful tips for tomorrow:",
            *(f"- {tip}" for tip in tips),
        ]
    )


async def build_end_of_day_goal_report(report_day: date | None = None) -> dict[str, Any]:
    tz = _safe_timezone()
    target_day = report_day or datetime.now(tz).date()
    summary, goal_results = await asyncio.gather(
        _screen_summary_for_local_day(target_day, tz),
        _goals_for_report(target_day),
    )
    active_goals, completed_goals = goal_results
    alignment = _goal_alignment(summary, active_goals)

    if settings.end_of_day_report_llm_enabled:
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
            active_goals=_format_goals(active_goals),
            completed_goals=_format_goals(completed_goals),
            alignment_hints=_format_alignment(alignment),
        )

        response = await completion_with_fallback(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.4,
            max_tokens=900,
            timeout=settings.agent_briefing_timeout,
            runtime_path="end_of_day_goal_report",
        )
        body = _redact_report_body(response.choices[0].message.content, limit=5000)
    else:
        body = _redact_report_body(
            _deterministic_report_body(
                report_day=target_day,
                summary=summary,
                alignment=alignment,
                completed_goals=completed_goals,
            ),
            limit=5000,
        )
    if not body:
        body = "No report generated."

    return {
        "date": target_day.isoformat(),
        "timezone": str(tz),
        "body": body,
        "summary": summary,
        "goal_alignment": alignment,
        "completed_goal_count": len(completed_goals),
        "active_goal_count": len(active_goals),
        "analysis_provider": "llm" if settings.end_of_day_report_llm_enabled else "deterministic-local",
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
    if not settings.smtp_host:
        return EmailDeliveryResult(
            status="skipped",
            reason="smtp_host_missing",
            recipient_hash=recipient_digest,
        )

    message = EmailMessage()
    message["Subject"] = f"Seraph end-of-day report: {report['date']}"
    message["From"] = sender
    message["To"] = recipient
    if settings.email_reports_reply_to:
        message["Reply-To"] = settings.email_reports_reply_to
    message.set_content(report["body"])

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
        "analysis_provider": "deterministic-local",
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
