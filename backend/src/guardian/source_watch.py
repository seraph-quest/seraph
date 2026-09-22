"""Bounded, deterministic guardian source watches.

M1 deliberately has no model dependency.  It observes public HTTPS text or
workspace text, produces a redacted local dossier/task, and uses the existing
durable job and approval contracts for the only write boundary.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import html
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from config.settings import settings
from src.approval.repository import approval_repository, fingerprint_tool_call
from src.artifacts.registry import artifact_id_for, build_artifact_record
from src.db import engine as db_engine
from src.db.models import (
    Goal,
    GuardianDecisionPacket,
    GuardianSourceBaseline,
    GuardianSourceWatch,
    ScheduledJob,
    ScheduledJobRun,
    StrategyDelta,
)
from src.db.session_refs import ensure_sessions_exist
from src.goals.repository import deserialize_admission_budget
from src.security.http_transport import PinnedTransportError, fetch_pinned_https
from src.tools.filesystem_tool import (
    _read_workspace_text_bounded,
    _safe_resolve,
    _write_workspace_text_bounded,
)
from src.workflows.job_runtime import (
    DurableJobIdentity,
    DurableJobSpec,
    DurableJobTransitionError,
    durable_job_repository,
)


CAPABILITY_ID = "guardian.research-watch.v1"
CAPABILITY_VERSION = "1"
SERVICE_ID = "guardian-source-watch"
SERVICE_PRINCIPAL = "service:guardian-source-watch"
SERVICE_OWNER_KIND = "service"
MAX_SOURCES = 10
MAX_SOURCE_BYTES = 256 * 1024
MAX_NORMALIZED_BYTES = 128 * 1024
SCAN_DEADLINE_SECONDS = 60
JOB_DEADLINE_SECONDS = 600
APPROVAL_TTL_SECONDS = 5 * 60
PACKET_MAX_BYTES = 72 * 1024
TASK_MAX_BYTES = 8 * 1024
NO_LEARNING = "no_learning"

_HTML_SCRIPT_RE = re.compile(r"<(script|style|noscript)\b[^>]*>.*?</\1\s*>", re.I | re.S)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_REDACTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?im)(authorization\s*:\s*bearer\s+)[^\s]+"), r"\1[REDACTED]"),
    (re.compile(r"(?im)(\b(?:api[_-]?key|secret|token|password)\b\s*[:=]\s*[\"']?)[^\s,\"']+"), r"\1[REDACTED]"),
    (re.compile(r"(?s)-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----"), "[REDACTED_PRIVATE_KEY]"),
)


class SourceWatchError(ValueError):
    """A source-watch request or execution is outside its bounded contract."""

    def __init__(self, code: str, message: str | None = None):
        self.code = code
        super().__init__(message or code)


def _no_learning_result(status: str, reason_code: str) -> dict[str, Any]:
    """Build the typed terminal result used when an occurrence aborts."""

    return {
        "status": status,
        "reason_code": reason_code,
        "learning": NO_LEARNING,
        "memory_status": NO_LEARNING,
        "operator_visible": True,
    }


@dataclass(frozen=True)
class SourceSpec:
    source_key: str
    kind: str
    target: str
    label: str
    priority: int
    identity_digest: str


@dataclass(frozen=True)
class WatchCriteria:
    include_terms: tuple[str, ...] = ()
    exclude_terms: tuple[str, ...] = ()
    min_changed_lines: int = 1
    min_changed_chars: int = 1
    max_material_sources: int = 3


@dataclass(frozen=True)
class SourceObservation:
    source: SourceSpec
    old_hash: str | None
    new_hash: str | None
    status: str
    changed_lines: int = 0
    changed_chars: int = 0
    before_excerpt: str = ""
    after_excerpt: str = ""
    # Complete changed-line text is process-local scan evidence.  It is used
    # for deterministic criteria matching and is deliberately omitted from
    # checkpoints, packets, and exported artifacts.
    changed_text: str = ""
    error_code: str | None = None
    redactions: int = 0
    baseline_text: str | None = None
    etag: str | None = None
    last_modified: str | None = None
    material: bool = False
    rebaseline: bool = False


@dataclass(frozen=True)
class ScanResult:
    observations: tuple[SourceObservation, ...]
    material: tuple[SourceObservation, ...]
    checkpoint: dict[str, Any]
    checkpoint_sha256: str
    input_digest: str
    baseline_updates: tuple[SourceObservation, ...]
    successful_sources: int
    degraded: bool


def _goal_admission(goal: Goal) -> tuple[bool, str, Any | None]:
    """Apply the standing goal consent and quiet-hours fence before I/O.

    Source observation is deliberately non-interrupting: it writes no
    notification and never invokes inference.  It still requires the same
    reviewed goal budget as other proactive work so a scheduler occurrence
    cannot outlive a revoked owner, grant, period, or quiet-hours policy.
    """

    owner = _text(getattr(goal, "owner_principal_id", ""))
    session = _text(getattr(goal, "owner_session_id", ""))
    if not owner or not session:
        return False, "goal_owner_binding_missing", None
    budget = deserialize_admission_budget(goal)
    if budget is None:
        return False, "goal_budget_missing_reviewed_grant", None
    if not bool(budget.reviewed_grant) or not _text(budget.grant_id):
        return False, "goal_budget_missing_reviewed_grant", budget
    now = _now()
    if budget.period_expires_at is not None and budget.period_expires_at <= now:
        return False, "goal_budget_period_expired", budget
    if budget.period_started_at is not None and budget.period_started_at > now:
        return False, "goal_budget_period_not_started", budget
    if budget.quiet_hours_start is not None:
        try:
            local_hour = now.astimezone(ZoneInfo(budget.timezone)).hour
        except Exception:
            return False, "goal_budget_timezone_invalid", budget
        start, end = int(budget.quiet_hours_start), int(budget.quiet_hours_end)
        quiet = local_hour >= start or local_hour < end if start > end else start <= local_hour < end
        if quiet:
            return False, "goal_quiet_hours", budget
    return True, "admitted", budget


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _load(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return fallback
    return parsed


def _sha(value: str | bytes) -> str:
    raw = value if isinstance(value, bytes) else value.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _text(value: Any) -> str:
    return str(value or "").strip()


def source_identity_digest(kind: str, target: str) -> str:
    return _sha(_dump({"kind": _text(kind), "target": _text(target)}))


def parse_sources(raw: Sequence[Mapping[str, Any]]) -> tuple[SourceSpec, ...]:
    if len(raw) > MAX_SOURCES:
        raise SourceWatchError("source_limit", "A watch may contain at most ten sources.")
    result: list[SourceSpec] = []
    keys: set[str] = set()
    for item in raw:
        key = _text(item.get("source_key"))
        kind = _text(item.get("kind"))
        target = _text(item.get("target"))
        if not key or key in keys:
            raise SourceWatchError("source_key_invalid")
        if kind not in {"public_https_text", "workspace_text"}:
            raise SourceWatchError("source_kind_invalid")
        if not target:
            raise SourceWatchError("source_target_missing")
        if kind == "public_https_text" and not target.lower().startswith("https://"):
            raise SourceWatchError("source_https_required")
        if kind == "workspace_text" and (
            target.startswith("/") or ".." in Path(target).parts or target.startswith("~")
        ):
            raise SourceWatchError("workspace_path_invalid")
        priority = item.get("priority", 3)
        if isinstance(priority, bool) or not 1 <= int(priority) <= 5:
            raise SourceWatchError("source_priority_invalid")
        keys.add(key)
        result.append(
            SourceSpec(
                source_key=key,
                kind=kind,
                target=target,
                label=_text(item.get("label")) or key,
                priority=int(priority),
                identity_digest=source_identity_digest(kind, target),
            )
        )
    return tuple(sorted(result, key=lambda item: item.source_key))


def parse_criteria(raw: Mapping[str, Any]) -> WatchCriteria:
    def terms(key: str) -> tuple[str, ...]:
        value = raw.get(key, [])
        if not isinstance(value, list) or len(value) > 20:
            raise SourceWatchError("criteria_terms_invalid")
        normalized = tuple(sorted({_text(item).casefold() for item in value if _text(item)}))
        if any(len(item) > 80 for item in normalized):
            raise SourceWatchError("criteria_term_too_long")
        return normalized

    try:
        min_lines = int(raw.get("min_changed_lines", 1))
        min_chars = int(raw.get("min_changed_chars", 1))
        max_material = int(raw.get("max_material_sources", 3))
    except (TypeError, ValueError) as exc:
        raise SourceWatchError("criteria_number_invalid") from exc
    if not 1 <= min_lines <= 1000 or not 1 <= min_chars <= 10000:
        raise SourceWatchError("criteria_threshold_invalid")
    if not 1 <= max_material <= 3:
        raise SourceWatchError("criteria_material_limit_invalid")
    return WatchCriteria(
        include_terms=terms("include_terms"),
        exclude_terms=terms("exclude_terms"),
        min_changed_lines=min_lines,
        min_changed_chars=min_chars,
        max_material_sources=max_material,
    )


def _validate_schedule(schedule: Mapping[str, Any]) -> tuple[str, str]:
    cron = _text(schedule.get("cron"))
    timezone_name = _text(schedule.get("timezone")) or "UTC"
    try:
        trigger = CronTrigger.from_crontab(cron, timezone=timezone_name)
        now = _now()
        first = trigger.get_next_fire_time(None, now)
        second = trigger.get_next_fire_time(first, first) if first is not None else None
    except Exception as exc:
        raise SourceWatchError("schedule_invalid") from exc
    if first is None or second is None or (second - first).total_seconds() < 15 * 60:
        raise SourceWatchError("schedule_cadence_too_frequent")
    return cron, timezone_name


def normalize_source_text(content: str, *, html_content: bool = False) -> str:
    value = str(content or "").replace("\r\n", "\n").replace("\r", "\n")
    if html_content:
        value = _COMMENT_RE.sub("", value)
        value = _HTML_SCRIPT_RE.sub("\n", value)
        value = re.sub(r"</(p|div|li|h[1-6]|br|tr|section|article)>", "\n", value, flags=re.I)
        value = _HTML_TAG_RE.sub("", value)
        value = html.unescape(value)
    lines = [" ".join(line.split()) for line in value.split("\n")]
    normalized = "\n".join(line for line in lines if line).strip()
    if len(normalized.encode("utf-8")) > MAX_NORMALIZED_BYTES:
        raise SourceWatchError("source_normalized_too_large")
    return normalized


def redact_export_text(content: str) -> tuple[str, dict[str, Any]]:
    value = str(content or "")
    count = 0
    for pattern, replacement in _REDACTION_PATTERNS:
        value, replaced = pattern.subn(replacement, value)
        count += replaced
    return value, {"redacted": count > 0, "replacement_count": count}


def _diff(before: str, after: str) -> tuple[int, int, str, str, str]:
    old_lines, new_lines = before.splitlines(), after.splitlines()
    changed_lines = 0
    changed_chars = 0
    before_parts: list[str] = []
    after_parts: list[str] = []
    changed_parts: list[str] = []
    for tag, old_start, old_end, new_start, new_end in difflib.SequenceMatcher(
        a=old_lines, b=new_lines, autojunk=False
    ).get_opcodes():
        if tag == "equal":
            continue
        old_part = old_lines[old_start:old_end]
        new_part = new_lines[new_start:new_end]
        changed_lines += max(len(old_part), len(new_part))
        changed_chars += len("\n".join(old_part)) + len("\n".join(new_part))
        before_parts.extend(old_part)
        after_parts.extend(new_part)
        changed_parts.extend(old_part)
        changed_parts.extend(new_part)
    return (
        changed_lines,
        changed_chars,
        "\n".join(before_parts)[:4096],
        "\n".join(after_parts)[:4096],
        "\n".join(changed_parts),
    )


def material_change(
    observation: SourceObservation,
    criteria: WatchCriteria,
) -> bool:
    if observation.old_hash == observation.new_hash or observation.rebaseline:
        return False
    # The excerpts are only a display compatibility field.  Criteria must be
    # evaluated against every changed line, otherwise a relevant term after
    # the first 4 KiB can be silently missed.
    changed = (
        observation.changed_text
        or f"{observation.before_excerpt}\n{observation.after_excerpt}"
    ).casefold()
    if observation.changed_lines < criteria.min_changed_lines and observation.changed_chars < criteria.min_changed_chars:
        return False
    if criteria.include_terms and not any(term in changed for term in criteria.include_terms):
        return False
    if any(term in changed for term in criteria.exclude_terms):
        return False
    return True


def compute_input_digest(
    *,
    watch_id: str,
    goal_revision: int,
    plan_revision: int,
    source_set_digest: str,
    criteria_digest: str,
    capability_version: str,
    observations: Sequence[SourceObservation],
) -> str:
    pairs = [
        {
            "source_key": item.source.source_key,
            "identity_digest": item.source.identity_digest,
            "old_hash": item.old_hash,
            "new_hash": item.new_hash or "unavailable",
        }
        for item in sorted(observations, key=lambda value: value.source.source_key)
    ]
    return _sha(
        _dump(
            {
                "watch_id": watch_id,
                "goal_revision": goal_revision,
                "plan_revision": plan_revision,
                "source_set_digest": source_set_digest,
                "criteria_digest": criteria_digest,
                "capability_version": capability_version,
                "observations": pairs,
            }
        )
    )


def _safe_excerpt(value: str) -> tuple[str, dict[str, Any]]:
    return redact_export_text(value[:4096])


def _observation_checkpoint(item: SourceObservation) -> dict[str, Any]:
    """Return the metadata-only durable observation projection.

    Source text and excerpts are never exported in a dossier or API receipt.
    The normalized baseline is retained in this owner-bound local database
    checkpoint so an approved resume can commit exactly what was observed
    without fetching the source again.
    """

    return {
        "source_key": item.source.source_key,
        "source_label": item.source.label,
        "source_target": item.source.target,
        "source_priority": item.source.priority,
        "identity_digest": item.source.identity_digest,
        "old_hash": item.old_hash,
        "new_hash": item.new_hash or "unavailable",
        "status": item.status,
        "changed_lines": item.changed_lines,
        "changed_chars": item.changed_chars,
        "material": item.material,
        "error_code": item.error_code,
        "redactions": item.redactions,
        # This field is local canonical state, never copied into the dossier,
        # artifact receipts, or operator-facing packet projections.
        "baseline_text": item.baseline_text,
    }


def build_dossier(
    *,
    packet_id: str,
    watch_id: str,
    goal_id: str,
    goal_revision: int,
    plan_revision: int,
    material: Sequence[SourceObservation],
    checkpoint_sha256: str,
    redaction_manifest: Mapping[str, Any],
) -> str:
    lines = [
        "Seraph decision dossier",
        "schema: seraph.guardian.research-dossier.v1",
        "status: UNSYNTHESIZED",
        f"source_watch_id: {watch_id}",
        f"packet_id: {packet_id}",
        f"goal_id: {goal_id}",
        f"goal_revision: {goal_revision}",
        f"plan_revision: {plan_revision}",
        f"observed_checkpoint_sha256: {checkpoint_sha256}",
        f"redaction_manifest: {_dump(dict(redaction_manifest))}",
        "",
        "Material changes (deterministically prioritized):",
    ]
    for index, item in enumerate(material, 1):
        lines.extend(
            [
                f"{index}. {item.source.label} [{item.source.source_key}] priority={item.source.priority}",
                f"   target: {item.source.target}",
                f"   old_sha256: {item.old_hash or 'none'}",
                f"   new_sha256: {item.new_hash or 'none'}",
                f"   changed_lines: {item.changed_lines}; changed_chars: {item.changed_chars}",
                f"   citation: source_key={item.source.source_key}; identity={item.source.identity_digest}",
            ]
        )
    lines.extend(
        [
            "",
            "Options:",
            "1. Review the cited source changes in this dossier.",
            "2. Complete the local follow-up checklist.",
            "3. Defer until the next bounded watch cycle.",
            "",
            "Recommendation: review the highest-priority cited change first.",
        ]
    )
    return "\n".join(lines) + "\n"


def build_task(
    *,
    packet_id: str,
    watch_id: str,
    goal_id: str,
    material: Sequence[SourceObservation],
) -> str:
    lines = [
        "Seraph local follow-up task",
        "schema: seraph.guardian.research-task.v1",
        f"packet_id: {packet_id}",
        f"source_watch_id: {watch_id}",
        f"goal_id: {goal_id}",
        "",
        "Checklist:",
    ]
    for item in material:
        lines.append(f"- Review cited change for source {item.source.source_key}.")
    lines.extend(
        [
            "- Record the local decision or deferment in the dossier.",
            "- Read back the completed local artifact before treating it as done.",
        ]
    )
    return "\n".join(lines) + "\n"


async def _read_source(source: SourceSpec) -> tuple[str, dict[str, str]]:
    if source.kind == "public_https_text":
        try:
            response = await fetch_pinned_https(source.target)
        except (PinnedTransportError, OSError, TimeoutError) as exc:
            raise SourceWatchError("source_transport_blocked", str(exc)) from exc
        if response.status_code != 200:
            raise SourceWatchError(f"source_http_{response.status_code}")
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type not in {"text/plain", "text/html", "application/xhtml+xml"}:
            raise SourceWatchError("source_content_type_blocked")
        try:
            content = response.content.decode("utf-8", errors="replace")
        except Exception as exc:
            raise SourceWatchError("source_decode_failed") from exc
        return content, {
            "etag": response.headers.get("etag", ""),
            "last_modified": response.headers.get("last-modified", ""),
            "content_type": content_type,
        }
    try:
        resolved = _safe_resolve(source.target)
        stat_result = resolved.stat()
        if not resolved.is_file() or stat_result.st_nlink != 1:
            raise SourceWatchError("workspace_source_not_regular")
        content, truncated = _read_workspace_text_bounded(
            resolved,
            max_bytes=MAX_SOURCE_BYTES,
        )
        if truncated:
            raise SourceWatchError("workspace_source_too_large")
        return content, {}
    except SourceWatchError:
        raise
    except (OSError, ValueError) as exc:
        raise SourceWatchError("workspace_source_blocked", str(exc)) from exc


class SourceWatchService:
    """Persistence and execution adapter for one bounded source watch."""

    def __init__(
        self,
        *,
        fetcher: Callable[[SourceSpec], Awaitable[tuple[str, dict[str, str]]]] | None = None,
    ) -> None:
        self._fetcher = fetcher

    async def create_watch(
        self,
        *,
        owner_principal_id: str,
        owner_session_id: str,
        goal_id: str,
        expected_goal_revision: int,
        sources: Sequence[Mapping[str, Any]],
        criteria: Mapping[str, Any],
        schedule: Mapping[str, Any],
        write_mode: str,
        reviewed_grant_id: str | None = None,
    ) -> dict[str, Any]:
        parsed_sources = parse_sources(sources)
        parsed_criteria = parse_criteria(criteria)
        if write_mode not in {"approval_each_run", "standing_reviewed"}:
            raise SourceWatchError("write_mode_invalid")
        if write_mode == "standing_reviewed" and not _text(reviewed_grant_id):
            raise SourceWatchError("standing_grant_required")
        cron, timezone_name = _validate_schedule(schedule)
        watch_id = str(uuid.uuid4())
        scheduled_job_id = str(uuid.uuid4())
        source_json = [
            {
                "source_key": item.source_key,
                "kind": item.kind,
                "target": item.target,
                "label": item.label,
                "priority": item.priority,
                "identity_digest": item.identity_digest,
            }
            for item in parsed_sources
        ]
        source_set_digest = _sha(_dump(source_json))
        criteria_json = {
            "include_terms": list(parsed_criteria.include_terms),
            "exclude_terms": list(parsed_criteria.exclude_terms),
            "min_changed_lines": parsed_criteria.min_changed_lines,
            "min_changed_chars": parsed_criteria.min_changed_chars,
            "max_material_sources": parsed_criteria.max_material_sources,
        }
        criteria_digest = _sha(_dump(criteria_json))
        async with db_engine.get_session() as db:
            goal = (await db.execute(select(Goal).where(Goal.id == goal_id))).scalars().first()
            if goal is None or int(goal.revision or 1) != int(expected_goal_revision):
                raise SourceWatchError("goal_revision_stale")
            if _text(getattr(getattr(goal, "status", ""), "value", getattr(goal, "status", ""))) != "active":
                raise SourceWatchError("goal_not_active")
            if (
                _text(goal.owner_principal_id) != owner_principal_id
                or _text(goal.owner_session_id) != owner_session_id
            ):
                raise SourceWatchError("goal_owner_mismatch")
            goal_budget = deserialize_admission_budget(goal)
            if goal_budget is None or not bool(goal_budget.reviewed_grant) or not _text(goal_budget.grant_id):
                raise SourceWatchError("goal_budget_missing_reviewed_grant")
            if write_mode == "standing_reviewed" and _text(reviewed_grant_id) != _text(goal_budget.grant_id):
                raise SourceWatchError("standing_grant_mismatch")
            await ensure_sessions_exist(db, [owner_session_id])
            watch = GuardianSourceWatch(
                id=watch_id,
                goal_id=goal_id,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                state="active",
                capability_id=CAPABILITY_ID,
                capability_version=CAPABILITY_VERSION,
                goal_revision=int(expected_goal_revision),
                plan_revision=1,
                sources_json=_dump(source_json),
                criteria_json=_dump(criteria_json),
                schedule_spec_json=_dump({"cron": cron, "timezone": timezone_name}),
                read_authority_json=_dump({"source_keys": [item.source_key for item in parsed_sources]}),
                write_authority_json=_dump(
                    {
                        "packet_prefix": f"guardian/source-watches/{watch_id}/packets/",
                        "task_prefix": f"guardian/source-watches/{watch_id}/tasks/",
                        "grant_id": reviewed_grant_id,
                    }
                ),
                write_mode=write_mode,
                scheduled_job_id=scheduled_job_id,
                source_set_digest=source_set_digest,
                criteria_digest=criteria_digest,
            )
            scheduled_job = ScheduledJob(
                id=scheduled_job_id,
                name=f"Guardian source watch {watch_id[:8]}",
                enabled=True,
                trigger_type="cron",
                trigger_spec_json=_dump({"cron": cron, "timezone": timezone_name}),
                action_type="run_source_watch",
                action_spec_json=_dump({"watch_id": watch_id}),
                session_id=owner_session_id,
                created_by_session_id=owner_session_id,
            )
            db.add(watch)
            db.add(scheduled_job)
            await db.flush()
        return await self.get_watch(watch_id, owner_principal_id=owner_principal_id)

    async def get_watch(self, watch_id: str, *, owner_principal_id: str) -> dict[str, Any] | None:
        async with db_engine.get_session() as db:
            watch = (
                await db.execute(
                    select(GuardianSourceWatch).where(
                        GuardianSourceWatch.id == watch_id,
                        GuardianSourceWatch.owner_principal_id == owner_principal_id,
                    )
                )
            ).scalars().first()
            if watch is None:
                return None
            baselines = (
                await db.execute(
                    select(GuardianSourceBaseline).where(GuardianSourceBaseline.watch_id == watch_id)
                )
            ).scalars().all()
            packet = (
                await db.execute(
                    select(GuardianDecisionPacket)
                    .where(GuardianDecisionPacket.watch_id == watch_id)
                    .order_by(GuardianDecisionPacket.created_at.desc())
                )
            ).scalars().first()
            return {
                "id": watch.id,
                "goal_id": watch.goal_id,
                "owner_principal_id": watch.owner_principal_id,
                "owner_session_id": watch.owner_session_id,
                "state": watch.state,
                "capability_id": watch.capability_id,
                "capability_version": watch.capability_version,
                "goal_revision": watch.goal_revision,
                "plan_revision": watch.plan_revision,
                "sources": _load(watch.sources_json, []),
                "criteria": _load(watch.criteria_json, {}),
                "schedule": _load(watch.schedule_spec_json, {}),
                "write_mode": watch.write_mode,
                "scheduled_job_id": watch.scheduled_job_id,
                "active_job_id": watch.active_job_id,
                "active_job_fence": watch.active_job_fence,
                "last_status": watch.last_status,
                "last_error_code": watch.last_error_code,
                "baselines": [
                    {
                        "source_key": item.source_key,
                        "identity_digest": item.identity_digest,
                        "generation": item.generation,
                        "sha256": item.baseline_sha256,
                        "state": item.state,
                        "observed_at": item.observed_at.isoformat(),
                    }
                    for item in baselines
                ],
                "latest_packet": self._packet_json(packet) if packet else None,
            }

    @staticmethod
    def _packet_json(packet: GuardianDecisionPacket) -> dict[str, Any]:
        return {
            "id": packet.id,
            "source_watch_id": packet.source_watch_id,
            "goal_id": packet.goal_id,
            "goal_revision": packet.goal_revision,
            "plan_revision": packet.plan_revision,
            "run_identity": packet.run_identity,
            "input_digest": packet.input_digest,
            "status": packet.status,
            "approval_id": packet.approval_id,
            "dossier_path": packet.dossier_path,
            "dossier_artifact_id": packet.dossier_artifact_id,
            "dossier_sha256": packet.dossier_sha256,
            "task_path": packet.task_path,
            "task_artifact_id": packet.task_artifact_id,
            "task_sha256": packet.task_sha256,
            "verification_status": packet.verification_status,
            "memory_status": packet.memory_status,
            "outcome": _load(packet.outcome_json, {}),
            "failure_code": packet.failure_code,
        }

    async def list_watches(self, *, owner_principal_id: str) -> list[dict[str, Any]]:
        async with db_engine.get_session() as db:
            rows = (
                await db.execute(
                    select(GuardianSourceWatch)
                    .where(GuardianSourceWatch.owner_principal_id == owner_principal_id)
                    .order_by(GuardianSourceWatch.updated_at.desc())
                )
            ).scalars().all()
        result = []
        for row in rows:
            item = await self.get_watch(row.id, owner_principal_id=owner_principal_id)
            if item is not None:
                result.append(item)
        return result

    async def update_watch(
        self,
        *,
        watch_id: str,
        owner_principal_id: str,
        owner_session_id: str,
        expected_plan_revision: int,
        sources: Sequence[Mapping[str, Any]] | None = None,
        criteria: Mapping[str, Any] | None = None,
        schedule: Mapping[str, Any] | None = None,
        write_mode: str | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        parsed_sources = parse_sources(sources) if sources is not None else None
        parsed_criteria = parse_criteria(criteria) if criteria is not None else None
        if write_mode is not None and write_mode not in {"approval_each_run", "standing_reviewed"}:
            raise SourceWatchError("write_mode_invalid")
        if state is not None and state not in {"active", "paused", "revoked", "blocked"}:
            raise SourceWatchError("watch_state_invalid")
        async with db_engine.get_session() as db:
            watch = (
                await db.execute(
                    select(GuardianSourceWatch).where(
                        GuardianSourceWatch.id == watch_id,
                        GuardianSourceWatch.owner_principal_id == owner_principal_id,
                        GuardianSourceWatch.owner_session_id == owner_session_id,
                    )
                )
            ).scalars().first()
            if watch is None:
                raise SourceWatchError("watch_not_found")
            if watch.plan_revision != expected_plan_revision:
                raise SourceWatchError("watch_plan_revision_stale")
            if watch.active_job_id:
                raise SourceWatchError("watch_active_job")
            if schedule is not None:
                cron, timezone_name = _validate_schedule(schedule)
                watch.schedule_spec_json = _dump({"cron": cron, "timezone": timezone_name})
                job = (
                    await db.execute(
                        select(ScheduledJob).where(ScheduledJob.id == watch.scheduled_job_id)
                    )
                ).scalars().first()
                if job is not None:
                    job.trigger_spec_json = watch.schedule_spec_json
                    job.updated_at = _now()
                    db.add(job)
            if parsed_sources is not None:
                source_json = [
                    {
                        "source_key": item.source_key,
                        "kind": item.kind,
                        "target": item.target,
                        "label": item.label,
                        "priority": item.priority,
                        "identity_digest": item.identity_digest,
                    }
                    for item in parsed_sources
                ]
                old_sources = _load(watch.sources_json, [])
                old_identities = {
                    _text(item.get("source_key")): _text(item.get("identity_digest"))
                    for item in old_sources
                    if isinstance(item, Mapping)
                }
                changed_identity = any(
                    old_identities.get(item.source_key) != item.identity_digest
                    for item in parsed_sources
                ) or set(old_identities) != {item.source_key for item in parsed_sources}
                if changed_identity and watch.write_mode == "standing_reviewed":
                    raise SourceWatchError("fresh_grant_required_for_source_identity")
                watch.sources_json = _dump(source_json)
                watch.source_set_digest = _sha(_dump(source_json))
            if parsed_criteria is not None:
                criteria_json = {
                    "include_terms": list(parsed_criteria.include_terms),
                    "exclude_terms": list(parsed_criteria.exclude_terms),
                    "min_changed_lines": parsed_criteria.min_changed_lines,
                    "min_changed_chars": parsed_criteria.min_changed_chars,
                    "max_material_sources": parsed_criteria.max_material_sources,
                }
                watch.criteria_json = _dump(criteria_json)
                watch.criteria_digest = _sha(_dump(criteria_json))
            if write_mode is not None:
                watch.write_mode = write_mode
            if state is not None:
                watch.state = state
            watch.plan_revision += 1
            watch.updated_at = _now()
            db.add(watch)
            await db.flush()
        return await self.get_watch(watch_id, owner_principal_id=owner_principal_id) or {}

    async def _claim_watch(self, watch_id: str, job_id: str, occurrence_id: str) -> tuple[GuardianSourceWatch | None, str]:
        async with db_engine.get_session() as db:
            now = _now()
            result = await db.execute(
                update(GuardianSourceWatch)
                .where(
                    GuardianSourceWatch.id == watch_id,
                    GuardianSourceWatch.state == "active",
                    GuardianSourceWatch.active_job_id.is_(None),
                )
                .values(
                    active_job_id=job_id,
                    active_job_fence=GuardianSourceWatch.active_job_fence + 1,
                    active_job_started_at=now,
                    last_run_identity=occurrence_id,
                    updated_at=now,
                )
            )
            if getattr(result, "rowcount", 0) != 1:
                return None, "watch_active_or_not_admissible"
            watch = (
                await db.execute(select(GuardianSourceWatch).where(GuardianSourceWatch.id == watch_id))
            ).scalars().first()
            if watch is None:
                return None, "watch_not_found"
            return watch, "claimed"

    async def _release_watch(
        self,
        watch_id: str,
        job_id: str,
        fence: int,
        status: str,
        error: str | None = None,
    ) -> bool:
        async with db_engine.get_session() as db:
            result = await db.execute(
                update(GuardianSourceWatch)
                .where(
                    GuardianSourceWatch.id == watch_id,
                    GuardianSourceWatch.active_job_id == job_id,
                    GuardianSourceWatch.active_job_fence == fence,
                )
                .values(
                    active_job_id=None,
                    active_job_started_at=None,
                    last_status=status,
                    last_error_code=error,
                    updated_at=_now(),
                )
            )
            return getattr(result, "rowcount", 0) == 1

    async def _scan(self, watch: GuardianSourceWatch) -> ScanResult:
        sources = parse_sources(_load(watch.sources_json, []))
        criteria = parse_criteria(_load(watch.criteria_json, {}))
        async with db_engine.get_session() as db:
            baseline_rows = (
                await db.execute(
                    select(GuardianSourceBaseline).where(GuardianSourceBaseline.watch_id == watch.id)
                )
            ).scalars().all()
            baselines = {row.source_key: row for row in baseline_rows}
        observations: list[SourceObservation] = []
        successful = 0
        deadline = _now() + timedelta(seconds=SCAN_DEADLINE_SECONDS)
        for source in sources:
            if _now() >= deadline:
                observations.append(
                    SourceObservation(source, None, None, "error", error_code="scan_deadline")
                )
                continue
            baseline = baselines.get(source.source_key)
            identity_changed = bool(
                baseline and baseline.identity_digest != source.identity_digest
            )
            try:
                raw = ""
                metadata: dict[str, str] = {}
                for attempt in range(2):
                    try:
                        raw, metadata = (
                            await self._fetcher(source)
                            if self._fetcher is not None
                            else await _read_source(source)
                        )
                        break
                    except SourceWatchError as exc:
                        retryable = exc.code == "source_transport_blocked" or exc.code.startswith("source_http_5")
                        if not retryable or attempt == 1 or _now() + timedelta(milliseconds=100) >= deadline:
                            raise
                        await asyncio.sleep(0.1)
                normalized = normalize_source_text(raw, html_content=metadata.get("content_type") in {"text/html", "application/xhtml+xml"})
                new_hash = _sha(normalized)
                old_text = baseline.baseline_text if baseline and not identity_changed else ""
                old_hash = baseline.baseline_sha256 if baseline and not identity_changed else None
                if len(old_text.encode("utf-8")) > MAX_NORMALIZED_BYTES:
                    raise SourceWatchError("baseline_normalized_too_large")
                changed_lines, changed_chars, before, after, changed_text = _diff(old_text, normalized)
                before_safe, before_manifest = _safe_excerpt(before)
                after_safe, after_manifest = _safe_excerpt(after)
                _, changed_manifest = redact_export_text(changed_text)
                redactions = int(changed_manifest["replacement_count"])
                observation = SourceObservation(
                    source=source,
                    old_hash=old_hash,
                    new_hash=new_hash,
                    status="observed",
                    changed_lines=changed_lines,
                    changed_chars=changed_chars,
                    before_excerpt=before_safe,
                    after_excerpt=after_safe,
                    changed_text=changed_text,
                    redactions=redactions,
                    baseline_text=normalized,
                    etag=metadata.get("etag"),
                    last_modified=metadata.get("last_modified"),
                    rebaseline=identity_changed,
                )
                observation = SourceObservation(
                    **{**observation.__dict__, "material": material_change(observation, criteria)}
                )
                observations.append(observation)
                successful += 1
            except SourceWatchError as exc:
                observations.append(
                    SourceObservation(
                        source,
                        baseline.baseline_sha256 if baseline else None,
                        None,
                        "error",
                        error_code=exc.code,
                    )
                )
        ordered_material = sorted(
            (item for item in observations if item.material),
            key=lambda item: (
                -item.source.priority,
                -item.changed_chars,
                -item.changed_lines,
                item.source.source_key,
            ),
        )[: criteria.max_material_sources]
        checkpoint = {
            "schema": "seraph.guardian.source-observation.v1",
            "sources": [_observation_checkpoint(item) for item in observations],
        }
        checkpoint_sha = _sha(_dump(checkpoint))
        input_digest = compute_input_digest(
            watch_id=watch.id,
            goal_revision=watch.goal_revision,
            plan_revision=watch.plan_revision,
            source_set_digest=watch.source_set_digest,
            criteria_digest=watch.criteria_digest,
            capability_version=watch.capability_version,
            observations=observations,
        )
        return ScanResult(
            observations=tuple(observations),
            material=tuple(ordered_material),
            checkpoint=checkpoint,
            checkpoint_sha256=checkpoint_sha,
            input_digest=input_digest,
            baseline_updates=tuple(item for item in observations if item.status == "observed"),
            successful_sources=successful,
            degraded=successful < len(sources),
        )

    def _scan_from_packet(self, watch: GuardianSourceWatch, packet: GuardianDecisionPacket) -> ScanResult:
        """Rehydrate the approved observation checkpoint without I/O.

        Approval execution must operate on the immutable observation that was
        presented to the operator.  A changed source is handled by the next
        watch occurrence, never by refetching during resume.
        """
        checkpoint = _load(packet.observed_checkpoint_json, {})
        if not isinstance(checkpoint, Mapping) or checkpoint.get("schema") != "seraph.guardian.source-observation.v1":
            raise SourceWatchError("observed_checkpoint_invalid")
        sources = {item.source_key: item for item in parse_sources(_load(watch.sources_json, []))}
        observations: list[SourceObservation] = []
        for raw in checkpoint.get("sources", []):
            if not isinstance(raw, Mapping):
                raise SourceWatchError("observed_checkpoint_invalid")
            source = sources.get(_text(raw.get("source_key")))
            if source is None or _text(raw.get("identity_digest")) != source.identity_digest:
                raise SourceWatchError("observed_checkpoint_stale")
            observations.append(
                SourceObservation(
                    source=source,
                    old_hash=_text(raw.get("old_hash")) or None,
                    new_hash=_text(raw.get("new_hash")) or None,
                    status=_text(raw.get("status")) or "error",
                    changed_lines=int(raw.get("changed_lines") or 0),
                    changed_chars=int(raw.get("changed_chars") or 0),
                    error_code=_text(raw.get("error_code")) or None,
                    redactions=int(raw.get("redactions") or 0),
                    # Read legacy checkpoints for recovery compatibility, but
                    # never emit this field in new durable projections.
                    baseline_text=raw.get("baseline_text") if isinstance(raw.get("baseline_text"), str) else None,
                    material=bool(raw.get("material")),
                )
            )
        checkpoint_sha = _sha(_dump(checkpoint))
        if packet.observed_checkpoint_sha256 and checkpoint_sha != packet.observed_checkpoint_sha256:
            raise SourceWatchError("observed_checkpoint_digest_mismatch")
        criteria = parse_criteria(_load(watch.criteria_json, {}))
        material = tuple(
            sorted(
                (item for item in observations if item.material),
                key=lambda item: (-item.source.priority, -item.changed_chars, -item.changed_lines, item.source.source_key),
            )[: criteria.max_material_sources]
        )
        return ScanResult(
            observations=tuple(observations),
            material=material,
            checkpoint=dict(checkpoint),
            checkpoint_sha256=checkpoint_sha,
            input_digest=packet.input_digest,
            baseline_updates=tuple(item for item in observations if item.status == "observed"),
            successful_sources=sum(1 for item in observations if item.status == "observed"),
            degraded=any(item.status != "observed" for item in observations),
        )

    async def _admit_job(
        self,
        watch: GuardianSourceWatch,
        occurrence_id: str,
        *,
        budget: Any,
    ) -> dict[str, Any]:
        job_id = f"source-watch:{watch.id}:{occurrence_id}"
        sources = parse_sources(_load(watch.sources_json, []))
        # Source priority is the only deterministic queue signal in this
        # capability. Keep it inside the existing durable priority range.
        priority = 40 + (max((item.priority for item in sources), default=1) * 10)
        max_runtime = min(JOB_DEADLINE_SECONDS, int(getattr(budget, "max_runtime_seconds", JOB_DEADLINE_SECONDS)))
        max_attempts = min(2, max(1, int(getattr(budget, "max_attempts", 1))))
        authority = {
            "principal": SERVICE_PRINCIPAL,
            "owner_kind": "service",
            "service_id": SERVICE_ID,
            "session_id": watch.owner_session_id,
            "goal_owner_principal_id": watch.owner_principal_id,
            "goal_owner_session_id": watch.owner_session_id,
            "capability_id": CAPABILITY_ID,
            "permissions": ["source_observation", "workspace_write"],
            "budget_microusd": 0,
            "budget_grant_id": _text(getattr(budget, "grant_id", "")),
            "quiet_hours": {
                "start": getattr(budget, "quiet_hours_start", None),
                "end": getattr(budget, "quiet_hours_end", None),
                "timezone": _text(getattr(budget, "timezone", "UTC")) or "UTC",
            },
            "interruption_cost": "none",
            "delivery_surface": "cockpit_approval_queue",
            "priority": priority,
        }
        identity = DurableJobIdentity(
            job_id=job_id,
            owner_kind="service",
            owner_principal_id=SERVICE_PRINCIPAL,
            job_kind="guardian_source_watch",
            capability_version=CAPABILITY_VERSION,
            idempotency_scope="guardian-source-watch",
            idempotency_key=f"{watch.id}:{watch.plan_revision}:{occurrence_id}",
        )
        admitted = await durable_job_repository.admit_job(
            DurableJobSpec(
                identity=identity,
                inputs={"watch_id": watch.id, "occurrence_id": occurrence_id},
                session_id=watch.owner_session_id,
                operator_session_id=watch.owner_session_id,
                goal_id=watch.goal_id,
                goal_revision=watch.goal_revision,
                plan_revision=watch.plan_revision,
                declared_authority=authority,
            deadline_at=_now() + timedelta(seconds=max_runtime),
            max_attempts=max_attempts,
            max_outstanding_jobs=int(getattr(budget, "max_outstanding_jobs", 1)),
            priority=priority,
            service_id=SERVICE_ID,
        )
        )
        if admitted.get("status") == "accepted":
            admitted = await durable_job_repository.queue_job(job_id, expected_revision=admitted.get("revision"))
            admitted = await durable_job_repository.claim_job(
                job_id,
                owner=SERVICE_PRINCIPAL,
                expected_revision=admitted.get("revision"),
                expected_fencing_token=admitted.get("lease", {}).get("fencing_token"),
                lease_seconds=JOB_DEADLINE_SECONDS,
            )
        return admitted

    async def run_watch(
        self,
        watch_id: str,
        *,
        occurrence_id: str | None = None,
        expected_plan_revision: int | None = None,
        expected_scheduled_job_id: str | None = None,
        expected_owner_session_id: str | None = None,
    ) -> dict[str, Any]:
        occurrence = occurrence_id or str(uuid.uuid4())
        job_id = f"source-watch:{watch_id}:{occurrence}"
        # Admit the durable occurrence before reserving the watch.  A crash
        # between these operations leaves a recoverable job instead of a
        # watch fenced to an identity that does not exist.
        async with db_engine.get_session() as db:
            watch = (
                await db.execute(select(GuardianSourceWatch).where(GuardianSourceWatch.id == watch_id))
            ).scalars().first()
            goal = (
                await db.execute(select(Goal).where(Goal.id == watch.goal_id))
            ).scalars().first() if watch is not None else None
        if watch is None:
            return {"status": "blocked", "reason_code": "watch_not_found", "operator_visible": True}
        if expected_scheduled_job_id is not None and str(watch.scheduled_job_id) != str(expected_scheduled_job_id):
            return {"status": "blocked", "reason_code": "scheduled_job_binding_stale", "operator_visible": True}
        if expected_owner_session_id is not None and str(watch.owner_session_id) != str(expected_owner_session_id):
            return {"status": "blocked", "reason_code": "watch_session_mismatch", "operator_visible": True}
        if expected_plan_revision is not None and int(watch.plan_revision) != int(expected_plan_revision):
            return {"status": "blocked", "reason_code": "watch_plan_revision_stale", "operator_visible": True}
        if goal is None:
            return {"status": "blocked", "reason_code": "goal_not_found", "operator_visible": True}
        goal_status = _text(getattr(goal.status, "value", goal.status))
        if goal_status != "active":
            return {"status": "blocked", "reason_code": "goal_not_active", "operator_visible": True}
        if (
            _text(goal.owner_principal_id) != _text(watch.owner_principal_id)
            or _text(goal.owner_session_id) != _text(watch.owner_session_id)
            or int(goal.revision or 0) != int(watch.goal_revision or 0)
        ):
            return {"status": "blocked", "reason_code": "goal_binding_stale", "operator_visible": True}
        admitted, admission_reason, budget = _goal_admission(goal)
        if not admitted or budget is None:
            return {
                "status": "deferred" if admission_reason in {"goal_quiet_hours", "goal_budget_period_not_started"} else "blocked",
                "reason_code": admission_reason,
                "operator_visible": True,
                "goal_id": goal.id,
                "goal_revision": goal.revision,
            }
        if watch.write_mode == "standing_reviewed":
            write_authority = _load(watch.write_authority_json, {})
            if _text(write_authority.get("grant_id")) != _text(budget.grant_id):
                return {
                    "status": "blocked",
                    "reason_code": "standing_grant_stale",
                    "operator_visible": True,
                    "goal_id": goal.id,
                    "goal_revision": goal.revision,
                }
        job: dict[str, Any] | None = None
        fence = 0
        packet: GuardianDecisionPacket | None = None
        try:
            job = await self._admit_job(watch, occurrence, budget=budget)
            if job.get("status") != "running":
                return {"status": "blocked", "reason_code": "durable_job_not_running", "job": job}
            reserved_watch, claim_status = await self._claim_watch(watch_id, job_id, occurrence)
            if reserved_watch is None:
                await self._settle_observation_job(job_id, status="blocked", reason=claim_status)
                return {"status": "blocked", "reason_code": claim_status, "job_id": job_id}
            watch = reserved_watch
            fence = int(watch.active_job_fence)
            # The first admission check happens before the watch fence is
            # claimed. Re-read the canonical goal after the claim and before
            # any source transport so a pause, revision, quiet-hours change,
            # or grant revocation cannot race into source I/O.
            async with db_engine.get_session() as db:
                live_goal = (
                    await db.execute(select(Goal).where(Goal.id == watch.goal_id))
                ).scalars().first()
            live_reason: str | None = None
            live_budget = None
            if live_goal is None:
                live_reason = "goal_not_found"
            elif (
                _text(getattr(getattr(live_goal, "status", ""), "value", getattr(live_goal, "status", ""))) != "active"
                or _text(live_goal.owner_principal_id) != _text(watch.owner_principal_id)
                or _text(live_goal.owner_session_id) != _text(watch.owner_session_id)
                or int(live_goal.revision or 0) != int(watch.goal_revision or 0)
            ):
                live_reason = "goal_binding_stale"
            else:
                admitted_now, admission_reason_now, live_budget = _goal_admission(live_goal)
                if not admitted_now or live_budget is None:
                    live_reason = admission_reason_now
                elif watch.write_mode == "standing_reviewed":
                    authority_now = _load(watch.write_authority_json, {})
                    if _text(authority_now.get("grant_id")) != _text(live_budget.grant_id):
                        live_reason = "standing_grant_stale"
            if live_reason is not None:
                await self._settle_observation_job(job_id, status="blocked", reason=live_reason)
                await self._release_watch(watch.id, job_id, fence, "blocked", live_reason)
                return {
                    "status": "deferred" if live_reason in {"goal_quiet_hours", "goal_budget_period_not_started"} else "blocked",
                    "reason_code": live_reason,
                    "job_id": job_id,
                    "operator_visible": True,
                }
            scan = await self._scan(watch)
            current = await durable_job_repository.get_job(job_id)
            if current is None:
                raise SourceWatchError("durable_job_missing")
            lease = current.get("lease") or {}
            owner = str(lease.get("owner") or SERVICE_PRINCIPAL)
            durable_fence = int(lease.get("fencing_token") or 0)
            revision = int(current.get("revision") or 0)
            await durable_job_repository.record_checkpoint(
                job_id,
                checkpoint_id=f"source-observation:{watch.id}:{occurrence}",
                state={"checkpoint_sha256": scan.checkpoint_sha256, "input_digest": scan.input_digest},
                owner=owner,
                fencing_token=durable_fence,
                expected_revision=revision,
            )
            if scan.successful_sources == 0:
                await self._settle_observation_job(job_id, status="blocked", reason="source_unavailable")
                await self._release_watch(watch.id, job_id, fence, "blocked", "source_unavailable")
                return {"status": "blocked", "reason_code": "source_unavailable", "job_id": job_id}
            if not scan.material:
                baseline_status = (
                    "rebaseline_initialized"
                    if any(item.rebaseline for item in scan.observations)
                    else ("degraded" if scan.degraded else "no_change")
                )
                await self._commit_baselines(watch, scan, status=baseline_status)
                await self._settle_observation_job(
                    job_id,
                    status="succeeded",
                    reason=baseline_status,
                )
                await self._release_watch(
                    watch.id,
                    job_id,
                    fence,
                    baseline_status,
                )
                return {"status": baseline_status, "job_id": job_id}
            packet = await self._create_packet(watch, job_id, scan)
            if watch.write_mode == "approval_each_run":
                approval_id = await self._hold_for_approval(
                    watch,
                    packet,
                    await durable_job_repository.get_job(job_id) or current,
                )
                return {
                    "status": "awaiting_approval",
                    "packet_id": packet.id,
                    "approval_id": approval_id,
                    "job_id": job_id,
                }
            await self._execute_packet(watch, packet, job_id, scan=scan)
            return {"status": "degraded" if scan.degraded else "succeeded", "packet_id": packet.id, "job_id": job_id}
        except Exception as exc:
            if packet is not None:
                try:
                    await self._mark_packet_failure(packet.id, type(exc).__name__)
                except Exception:
                    pass
            if job is not None:
                try:
                    current = await durable_job_repository.get_job(job_id)
                    if current and current.get("status") == "running":
                        lease = current.get("lease") or {}
                        await durable_job_repository.transition_job(
                            job_id,
                            "failed",
                            owner=lease.get("owner"),
                            fencing_token=lease.get("fencing_token"),
                            expected_revision=current.get("revision"),
                            reason=type(exc).__name__,
                            result=_no_learning_result("failed", type(exc).__name__),
                            result_summary="source-watch occurrence failed without learning",
                        )
                except Exception:
                    pass
            await self._release_watch(watch.id, job_id, fence, "blocked", type(exc).__name__)
            return {
                **_no_learning_result("blocked", type(exc).__name__),
                "job_id": job_id,
            }

    async def cancel_watch_job(
        self,
        *,
        watch_id: str,
        job_id: str,
        expected_plan_revision: int,
        expected_fencing_token: int,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> dict[str, Any]:
        """Cancel one active occurrence through the durable owner/fence CAS."""

        async with db_engine.get_session() as db:
            watch = (
                await db.execute(
                    select(GuardianSourceWatch).where(
                        GuardianSourceWatch.id == watch_id,
                        GuardianSourceWatch.owner_principal_id == owner_principal_id,
                        GuardianSourceWatch.owner_session_id == owner_session_id,
                    )
                )
            ).scalars().first()
            if watch is None:
                raise SourceWatchError("watch_not_found")
            if int(watch.plan_revision or 0) != int(expected_plan_revision):
                raise SourceWatchError("watch_plan_revision_stale")
            if _text(watch.active_job_id) != _text(job_id):
                raise SourceWatchError("active_job_mismatch")
            if int(watch.active_job_fence or 0) != int(expected_fencing_token):
                raise SourceWatchError("cancel_job_fence_stale")
            packet = (
                await db.execute(
                    select(GuardianDecisionPacket).where(
                        GuardianDecisionPacket.watch_id == watch_id,
                        GuardianDecisionPacket.run_identity == job_id,
                    )
                )
            ).scalars().first()

        job = await durable_job_repository.get_job(job_id)
        if job is None:
            raise SourceWatchError("durable_job_missing")
        authority = job.get("declared_authority") if isinstance(job.get("declared_authority"), Mapping) else {}
        if (
            str(job.get("job_kind") or "") != "guardian_source_watch"
            or str(job.get("goal_id") or "") != str(watch.goal_id)
            or int(job.get("goal_revision") or 0) != int(watch.goal_revision or 0)
            or int(job.get("plan_revision") or 0) != int(watch.plan_revision or 0)
            or str(authority.get("session_id") or "") != str(owner_session_id)
        ):
            raise SourceWatchError("durable_job_binding_mismatch")
        if job.get("status") in {"succeeded", "degraded", "cancelled"}:
            raise SourceWatchError("job_terminal")
        if job.get("status") in {"unknown_external_effect", "cost_liability"}:
            raise SourceWatchError("cancel_requires_reconciliation")

        lease = job.get("lease") if isinstance(job.get("lease"), Mapping) else {}
        job_fence = int(lease.get("fencing_token") or 0)
        if job_fence != int(expected_fencing_token):
            raise SourceWatchError("cancel_job_fence_stale")
        lease_owner = _text(lease.get("owner"))
        expected_revision = int(job.get("revision") or 0)
        if job.get("status") == "running":
            if lease_owner != SERVICE_PRINCIPAL:
                raise SourceWatchError("cancel_job_owner_mismatch")
            cancelled = await durable_job_repository.transition_job(
                job_id,
                "cancelled",
                owner=SERVICE_PRINCIPAL,
                fencing_token=expected_fencing_token,
                expected_revision=expected_revision,
                reason="operator_cancelled",
                result=_no_learning_result("cancelled", "operator_cancelled"),
                result_summary="source-watch occurrence cancelled by operator",
            )
        elif lease_owner or lease.get("expires_at"):
            raise SourceWatchError("cancel_job_lease_mismatch")
        else:
            cancelled = await durable_job_repository.transition_job(
                job_id,
                "cancelled",
                expected_fencing_token=expected_fencing_token,
                expected_revision=expected_revision,
                reason="operator_cancelled",
                result=_no_learning_result("cancelled", "operator_cancelled"),
                result_summary="source-watch occurrence cancelled by operator",
            )
        if str(cancelled.get("status") or "") != "cancelled":
            raise SourceWatchError("cancel_requires_reconciliation")
        if packet is not None:
            await self._mark_packet_terminal(packet.id, "cancelled", "operator_cancelled")
        released = await self._release_watch(
            watch.id,
            job_id,
            int(watch.active_job_fence or 0),
            "cancelled",
            "operator_cancelled",
        )
        if not released:
            raise SourceWatchError("active_job_release_conflict")
        return {
            "status": "cancelled",
            "job_id": job_id,
            "watch_id": watch.id,
            "fencing_token": expected_fencing_token,
            "learning": NO_LEARNING,
            "memory_status": NO_LEARNING,
            "operator_visible": True,
        }

    async def _mark_packet_failure(self, packet_id: str, reason_code: str) -> None:
        """Leave an operator-visible no-learning packet terminal marker."""

        async with db_engine.get_session() as db:
            row = (
                await db.execute(select(GuardianDecisionPacket).where(GuardianDecisionPacket.id == packet_id))
            ).scalars().first()
            if row is None or row.status in {"succeeded", "degraded", "cancelled"}:
                return
            row.status = "blocked"
            row.failure_code = _text(reason_code)[:120] or "source_watch_failed"
            row.verification_status = "failed"
            row.memory_status = NO_LEARNING
            row.outcome_json = _dump(_no_learning_result("blocked", row.failure_code))
            row.updated_at = _now()
            db.add(row)

    async def _mark_packet_terminal(self, packet_id: str, status: str, reason_code: str) -> None:
        """Persist a bounded terminal packet marker without source content."""

        async with db_engine.get_session() as db:
            row = (
                await db.execute(select(GuardianDecisionPacket).where(GuardianDecisionPacket.id == packet_id))
            ).scalars().first()
            if row is None or row.status in {"succeeded", "degraded"}:
                return
            row.status = status
            row.failure_code = _text(reason_code)[:120] or None
            row.memory_status = NO_LEARNING
            row.outcome_json = _dump(_no_learning_result(status, row.failure_code or "terminal"))
            row.updated_at = _now()
            db.add(row)

    async def _create_packet(self, watch: GuardianSourceWatch, job_id: str, scan: ScanResult) -> GuardianDecisionPacket:
        redaction_count = sum(item.redactions for item in scan.material)
        redaction = {"redacted": redaction_count > 0, "replacement_count": redaction_count}
        packet_id = str(uuid.uuid4())
        dossier = build_dossier(
            packet_id=packet_id,
            watch_id=watch.id,
            goal_id=watch.goal_id,
            goal_revision=watch.goal_revision,
            plan_revision=watch.plan_revision,
            material=scan.material,
            checkpoint_sha256=scan.checkpoint_sha256,
            redaction_manifest=redaction,
        )
        task = build_task(
            packet_id=packet_id,
            watch_id=watch.id,
            goal_id=watch.goal_id,
            material=scan.material,
        )
        if len(dossier.encode("utf-8")) > PACKET_MAX_BYTES or len(task.encode("utf-8")) > TASK_MAX_BYTES:
            raise SourceWatchError("packet_size_limit")
        packet = GuardianDecisionPacket(
            id=packet_id,
            source_watch_id=watch.id,
            watch_id=watch.id,
            goal_id=watch.goal_id,
            goal_revision=watch.goal_revision,
            plan_revision=watch.plan_revision,
            run_identity=job_id,
            input_digest=scan.input_digest,
            criteria_digest=watch.criteria_digest,
            source_observation_json=_dump(scan.checkpoint),
            material_source_keys_json=_dump([item.source.source_key for item in scan.material]),
            proposal_text=dossier,
            task_text=task,
            status="prepared",
            observed_checkpoint_json=_dump(scan.checkpoint),
            observed_checkpoint_sha256=scan.checkpoint_sha256,
            redaction_manifest_json=_dump(redaction),
        )
        async with db_engine.get_session() as db:
            existing = (
                await db.execute(
                    select(GuardianDecisionPacket).where(
                        GuardianDecisionPacket.watch_id == watch.id,
                        GuardianDecisionPacket.input_digest == scan.input_digest,
                    )
                )
            ).scalars().first()
            if existing is not None:
                return existing
            db.add(packet)
            try:
                await db.flush()
            except IntegrityError:
                await db.rollback()
                existing = (
                    await db.execute(
                        select(GuardianDecisionPacket).where(
                            GuardianDecisionPacket.watch_id == watch.id,
                            GuardianDecisionPacket.input_digest == scan.input_digest,
                        )
                    )
                ).scalars().first()
                if existing is None:
                    raise
                return existing
        return packet

    async def _hold_for_approval(
        self,
        watch: GuardianSourceWatch,
        packet: GuardianDecisionPacket,
        current: Mapping[str, Any],
    ) -> str:
        authority_digest = str(current.get("authority_digest") or "")
        budget_digest = str(current.get("budget_digest") or "")
        fingerprint = fingerprint_tool_call(
            "guardian:source-watch-write",
            {
                "watch_id": watch.id,
                "packet_id": packet.id,
                "dossier_sha256": _sha(packet.proposal_text),
                "task_sha256": _sha(packet.task_text),
            },
        )
        details = {
            "approval_operator_principal_id": watch.owner_principal_id,
            "approval_owner_principal_id": watch.owner_principal_id,
            "approval_owner_operator_session_id": watch.owner_session_id,
            "operator_session_id": watch.owner_session_id,
            "approval_conversation_id": watch.owner_session_id,
            "approval_execution_session_id": watch.owner_session_id,
            "approval_execution_owner_principal_id": SERVICE_PRINCIPAL,
            "durable_job_id": current["job_id"],
            "durable_owner_kind": "service",
            "durable_owner_principal_id": SERVICE_PRINCIPAL,
            "durable_service_id": SERVICE_ID,
            "durable_authority_digest": authority_digest,
            "durable_goal_id": watch.goal_id,
            "durable_goal_revision": watch.goal_revision,
            "durable_plan_revision": watch.plan_revision,
            "durable_capability_version": CAPABILITY_VERSION,
            "durable_budget_digest": budget_digest,
            "packet_id": packet.id,
            "packet_digest": _sha(packet.proposal_text + packet.task_text),
            "expires_at": (_now() + timedelta(seconds=APPROVAL_TTL_SECONDS)).timestamp(),
        }
        approval = await approval_repository.get_or_create_pending(
            session_id=watch.owner_session_id,
            tool_name="guardian:source-watch-write",
            risk_level="high",
            summary=f"Write source-watch dossier and task for goal {watch.goal_id}",
            fingerprint=fingerprint,
            details=details,
        )
        lease = current.get("lease") or {}
        bound = await durable_job_repository.bind_approval_id(
            current["job_id"],
            approval.id,
            owner=str(lease.get("owner") or SERVICE_PRINCIPAL),
            fencing_token=int(lease.get("fencing_token") or 0),
            expected_revision=int(current.get("revision") or 0),
        )
        current_authority = str(bound.get("authority_digest") or "")
        await approval_repository.update_pending_details(
            approval.id,
            owner_principal_id=watch.owner_principal_id,
            operator_session_id=watch.owner_session_id,
            updates={
                "durable_authority_digest": current_authority,
                "authority_digest": current_authority,
                "approval_expires_at": approval.expires_at.timestamp() if approval.expires_at else None,
            },
        )
        bound_lease = bound.get("lease") or {}
        await durable_job_repository.transition_job(
            current["job_id"],
            "awaiting_approval",
            owner=str(bound_lease.get("owner") or SERVICE_PRINCIPAL),
            fencing_token=int(bound_lease.get("fencing_token") or 0),
            expected_revision=int(bound.get("revision") or 0),
            reason="source_watch_local_write_approval_required",
        )
        async with db_engine.get_session() as db:
            row = (
                await db.execute(
                    select(GuardianDecisionPacket).where(GuardianDecisionPacket.id == packet.id)
                )
            ).scalars().first()
            if row is not None:
                row.approval_id = approval.id
                row.status = "awaiting_approval"
                row.updated_at = _now()
                db.add(row)
        return approval.id

    async def execute_packet(
        self,
        *,
        watch_id: str,
        packet_id: str,
        expected_packet_digest: str,
        approval_id: str | None,
        expected_approval_revision: int | None,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> dict[str, Any]:
        async with db_engine.get_session() as db:
            watch = (
                await db.execute(
                    select(GuardianSourceWatch).where(
                        GuardianSourceWatch.id == watch_id,
                        GuardianSourceWatch.owner_principal_id == owner_principal_id,
                        GuardianSourceWatch.owner_session_id == owner_session_id,
                    )
                )
            ).scalars().first()
            packet = (
                await db.execute(
                    select(GuardianDecisionPacket).where(
                        GuardianDecisionPacket.id == packet_id,
                        GuardianDecisionPacket.watch_id == watch_id,
                    )
                )
            ).scalars().first()
            if watch is None or packet is None:
                raise SourceWatchError("packet_not_found")
            if packet.status != "awaiting_approval":
                raise SourceWatchError("packet_not_awaiting_approval")
            if (
                packet.goal_id != watch.goal_id
                or int(packet.goal_revision or 0) != int(watch.goal_revision or 0)
                or int(packet.plan_revision or 0) != int(watch.plan_revision or 0)
            ):
                raise SourceWatchError("packet_binding_stale")
            if _sha(packet.proposal_text + packet.task_text) != expected_packet_digest:
                raise SourceWatchError("packet_digest_stale")
            goal = (await db.execute(select(Goal).where(Goal.id == watch.goal_id))).scalars().first()
            if goal is None or _text(goal.owner_principal_id) != _text(owner_principal_id) or _text(goal.owner_session_id) != _text(owner_session_id):
                raise SourceWatchError("goal_owner_mismatch")
            if int(goal.revision or 0) != int(watch.goal_revision or 0) or _text(getattr(goal.status, "value", goal.status)) != "active":
                raise SourceWatchError("goal_binding_stale")
            admitted, admission_reason, budget = _goal_admission(goal)
            if not admitted or budget is None:
                raise SourceWatchError(admission_reason)
            if watch.write_mode == "standing_reviewed":
                write_authority = _load(watch.write_authority_json, {})
                if _text(write_authority.get("grant_id")) != _text(budget.grant_id):
                    raise SourceWatchError("standing_grant_stale")
            run_identity = packet.run_identity
        current = await durable_job_repository.get_job(run_identity)
        if current is None:
            raise SourceWatchError("durable_job_missing")
        current_authority = current.get("declared_authority") if isinstance(current.get("declared_authority"), Mapping) else {}
        if (
            str(current.get("goal_id") or "") != str(watch.goal_id)
            or int(current.get("goal_revision") or 0) != int(watch.goal_revision or 0)
            or int(current.get("plan_revision") or 0) != int(watch.plan_revision or 0)
            or str(current_authority.get("session_id") or "") != str(owner_session_id)
        ):
            raise SourceWatchError("durable_job_binding_mismatch")
        approval = await approval_repository.get(approval_id or str(current.get("declared_authority", {}).get("approval_id") or ""))
        if approval is None or approval.status != "approved":
            raise SourceWatchError("approval_not_current")
        if str(approval.operator_session_id or approval.session_id or "") != str(owner_session_id):
            raise SourceWatchError("approval_owner_session_mismatch")
        details = _load(approval.details_json, {})
        expires_at = float(details.get("approval_expires_at", details.get("expires_at", 0)))
        receipt = {
            "status": "approved",
            "authenticated": True,
            "operator_principal_id": owner_principal_id,
            "operator_session_id": str(approval.operator_session_id or approval.session_id or ""),
            "owner_kind": current["owner"]["kind"],
            "owner_principal_id": current["owner"]["principal_id"],
            "service_id": current["owner"]["service_id"],
            "approval_id": approval.id,
            "authority_digest": current["authority_digest"],
            "goal_id": current.get("goal_id"),
            "goal_revision": current.get("goal_revision"),
            "plan_revision": current.get("plan_revision"),
            "capability_version": current.get("capability_version"),
            "budget_microusd": 0,
            "budget_digest": current.get("budget_digest"),
            "expires_at": expires_at,
        }
        resumed = await durable_job_repository.resume_approved_job(
            run_identity,
            approval_receipt=receipt,
            approval_id=approval.id,
            authority_digest=str(current["authority_digest"]),
            goal_id=current.get("goal_id"),
            goal_revision=current.get("goal_revision"),
            plan_revision=current.get("plan_revision"),
            capability_version=str(current.get("capability_version") or ""),
            owner_kind=str(current["owner"]["kind"]),
            owner_principal_id=str(current["owner"]["principal_id"]),
            service_id=current["owner"].get("service_id"),
            budget_microusd=0,
            budget_digest=str(current.get("budget_digest") or ""),
            operator_principal_id=owner_principal_id,
            operator_session_id=str(approval.operator_session_id or approval.session_id or ""),
            expires_at=expires_at,
            expected_revision=expected_approval_revision or current.get("revision"),
        )
        queued_lease = resumed.get("lease") or {}
        claimed = await durable_job_repository.claim_job(
            run_identity,
            owner=SERVICE_PRINCIPAL,
            expected_revision=resumed.get("revision"),
            expected_fencing_token=queued_lease.get("fencing_token"),
            lease_seconds=JOB_DEADLINE_SECONDS,
        )
        scan = self._scan_from_packet(watch, packet)
        if scan.input_digest != packet.input_digest:
            raise SourceWatchError("packet_observation_stale")
        await self._execute_packet(watch, packet, run_identity, claimed=claimed, scan=scan)
        result = await self.get_watch(watch_id, owner_principal_id=owner_principal_id)
        return result or {"status": "succeeded", "packet_id": packet_id}

    @staticmethod
    def _packet_output_paths(watch_id: str, packet_id: str) -> tuple[str, str]:
        return (
            f"guardian/source-watches/{watch_id}/packets/{packet_id}.md",
            f"guardian/source-watches/{watch_id}/tasks/{packet_id}.md",
        )

    async def _read_verified_packet_outputs(
        self,
        watch: GuardianSourceWatch,
        packet: GuardianDecisionPacket,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Read exact prepared files without accepting caller-supplied hashes."""

        dossier_path, task_path = self._packet_output_paths(watch.id, packet.id)
        expected = (
            (dossier_path, packet.dossier_sha256 or _sha(packet.proposal_text), PACKET_MAX_BYTES),
            (task_path, packet.task_sha256 or _sha(packet.task_text), TASK_MAX_BYTES),
        )
        records: list[dict[str, Any]] = []
        for path, expected_sha, limit in expected:
            try:
                content, truncated = _read_workspace_text_bounded(_safe_resolve(path), max_bytes=limit)
            except (OSError, ValueError) as exc:
                raise SourceWatchError("recovery_artifact_unreadable") from exc
            if truncated or _sha(content) != expected_sha:
                raise SourceWatchError("recovery_artifact_mismatch")
            records.append(
                build_artifact_record(
                    file_path=path,
                    artifact_type=("guardian_decision_dossier" if path == dossier_path else "guardian_local_task"),
                    producer=CAPABILITY_ID,
                    run_id=packet.run_identity,
                    session_id=watch.owner_session_id,
                    content=content,
                    trust_boundary="local_workspace",
                )
            )
        return records[0], records[1]

    @staticmethod
    def _has_verified_output_effects(job: Mapping[str, Any], paths: Sequence[str]) -> bool:
        path_set = set(paths)
        return any(
            isinstance(item, Mapping)
            and _text(item.get("receipt_kind")) == "readback"
            and _text(item.get("status")) == "succeeded"
            and _text(item.get("target_path")) in path_set
            and isinstance(item.get("details"), Mapping)
            and item["details"].get("verified") is True
            for item in (job.get("effects") or [])
        )

    async def _repair_recovery_baselines(
        self,
        watch: GuardianSourceWatch,
        packet: GuardianDecisionPacket,
        scan: ScanResult,
    ) -> None:
        """Accept existing canonical baselines or repair legacy local input."""

        legacy_updates = tuple(item for item in scan.baseline_updates if item.baseline_text is not None)
        if legacy_updates:
            await self._commit_baselines(watch, scan, status="recovered")
            return
        expected = {
            item.source.source_key: (item.source.identity_digest, item.new_hash)
            for item in scan.observations
            if item.status == "observed" and item.new_hash and item.new_hash != "unavailable"
        }
        async with db_engine.get_session() as db:
            rows = (
                await db.execute(select(GuardianSourceBaseline).where(GuardianSourceBaseline.watch_id == watch.id))
            ).scalars().all()
        actual = {row.source_key: row for row in rows}
        for source_key, (identity_digest, new_hash) in expected.items():
            row = actual.get(source_key)
            if (
                row is None
                or _text(row.identity_digest) != identity_digest
                or _text(row.baseline_sha256) != new_hash
            ):
                raise SourceWatchError("recovery_baseline_unverified")

    async def _repair_recovery_receipts(
        self,
        *,
        job_id: str,
        watch: GuardianSourceWatch,
        packet: GuardianDecisionPacket,
        records: Sequence[Mapping[str, Any]],
        owner: str,
        fencing_token: int,
        revision: int,
    ) -> int:
        """Record missing local evidence after exact file readback."""

        current = await durable_job_repository.get_job(job_id) or {}
        artifacts = current.get("artifacts") if isinstance(current.get("artifacts"), list) else []
        effects = current.get("effects") if isinstance(current.get("effects"), list) else []
        for record in records:
            path = str(record["file_path"])
            content = _read_workspace_text_bounded(_safe_resolve(path), max_bytes=(PACKET_MAX_BYTES if "packets/" in path else TASK_MAX_BYTES))[0]
            if not any(
                isinstance(item, Mapping)
                and _text(item.get("file_path")) == path
                and _text(item.get("content_sha256")) == str(record["content_sha256"])
                for item in artifacts
            ):
                artifact = await durable_job_repository.record_artifact(
                    job_id,
                    file_path=path,
                    artifact_type=str(record["artifact_type"]),
                    content=content,
                    owner=owner,
                    fencing_token=fencing_token,
                    expected_revision=revision,
                )
                revision = int(artifact.get("revision") or revision)
            effect = next(
                (
                    item
                    for item in effects
                    if isinstance(item, Mapping)
                    and _text(item.get("effect_type")) == "workspace_write"
                    and _text(item.get("target_path")) == path
                    and _text(item.get("target_digest")) == str(record["content_sha256"])
                ),
                None,
            )
            if effect is None:
                effect_result = await durable_job_repository.record_effect(
                    job_id,
                    effect_type="workspace_write",
                    target_path=path,
                    target_digest=str(record["content_sha256"]),
                    content_sha256=str(record["content_sha256"]),
                    status="succeeded",
                    details={"verified": True, "output_exists": True, "workspace_contained": True},
                    owner=owner,
                    fencing_token=fencing_token,
                    expected_revision=revision,
                )
                revision = int(effect_result.get("revision") or revision)
                effect_id = (effect_result.get("receipt") or {}).get("effect_id")
            else:
                effect_id = effect.get("effect_id")
            if not any(
                isinstance(item, Mapping)
                and _text(item.get("receipt_kind")) == "readback"
                and _text(item.get("target_path")) == path
                and _text(item.get("content_sha256")) == str(record["content_sha256"])
                and _text(item.get("status")) == "succeeded"
                for item in effects
            ):
                readback = await durable_job_repository.record_readback(
                    job_id,
                    target_path=path,
                    effect_id=effect_id,
                    effect_type="workspace_write",
                    target_digest=str(record["content_sha256"]),
                    content_sha256=str(record["content_sha256"]),
                    status="succeeded",
                    details={"verified": True, "output_exists": True, "workspace_contained": True},
                    owner=owner,
                    fencing_token=fencing_token,
                    expected_revision=revision,
                )
                revision = int(readback.get("revision") or revision)
            current = await durable_job_repository.get_job(job_id) or current
            artifacts = current.get("artifacts") if isinstance(current.get("artifacts"), list) else artifacts
            effects = current.get("effects") if isinstance(current.get("effects"), list) else effects
        return revision

    async def recover_job(
        self,
        *,
        watch_id: str,
        job_id: str,
        expected_plan_revision: int,
        owner_principal_id: str,
        owner_session_id: str,
    ) -> dict[str, Any]:
        """Inspect and resume only the persisted local completion boundary.

        Recovery never accepts a caller assertion or starts a fresh scan.  A
        packet with verified artifacts can finish local metadata; an unresolved
        or missing effect remains blocked for operator action.
        """
        async with db_engine.get_session() as db:
            watch = (
                await db.execute(
                    select(GuardianSourceWatch).where(
                        GuardianSourceWatch.id == watch_id,
                        GuardianSourceWatch.owner_principal_id == owner_principal_id,
                    )
                )
            ).scalars().first()
            if watch is None:
                raise SourceWatchError("watch_not_found")
            if int(watch.plan_revision) != int(expected_plan_revision):
                raise SourceWatchError("watch_plan_revision_stale")
            if _text(watch.owner_session_id) != _text(owner_session_id):
                raise SourceWatchError("watch_session_mismatch")
            packet = (
                await db.execute(
                    select(GuardianDecisionPacket).where(
                        GuardianDecisionPacket.watch_id == watch_id,
                        GuardianDecisionPacket.run_identity == job_id,
                    )
                )
            ).scalars().first()
        job = await durable_job_repository.get_job(job_id)
        if job is None:
            raise SourceWatchError("durable_job_missing")
        authority = job.get("declared_authority") if isinstance(job.get("declared_authority"), Mapping) else {}
        if (
            str(job.get("job_kind") or "") != "guardian_source_watch"
            or str(job.get("goal_id") or "") != str(watch.goal_id)
            or int(job.get("goal_revision") or 0) != int(watch.goal_revision)
            or str(authority.get("goal_id") or "") != str(watch.goal_id)
            or int(authority.get("goal_revision") or 0) != int(watch.goal_revision)
            or str(authority.get("session_id") or "") != str(watch.owner_session_id)
        ):
            raise SourceWatchError("durable_job_binding_mismatch")
        if job.get("status") == "running":
            # A live lease cannot be inspected or adopted by this route.  An
            # expired lease is settled through the targeted canonical CAS so
            # unrelated jobs are never swept as a recovery side effect.
            lease = job.get("lease") if isinstance(job.get("lease"), Mapping) else {}
            expires_at = lease.get("expires_at")
            try:
                expired = expires_at is None or datetime.fromisoformat(str(expires_at).replace("Z", "+00:00")) <= _now()
            except (TypeError, ValueError):
                expired = False
            if not expired:
                return {
                    "status": "blocked",
                    "job_id": job_id,
                    "reason_code": "recovery_lease_active",
                    "operator_visible": True,
                }
            job = await durable_job_repository.recover_stale_job(job_id, now=_now())
        if packet is None:
            return {"status": job.get("status", "blocked"), "job_id": job_id, "recovery": "no_packet"}
        if packet.status in {"succeeded", "degraded"} and packet.verification_status == "passed":
            # A process can die after the packet/baseline transaction commits
            # but before the durable job transition.  Verify both local files
            # again, then use the canonical lease-free reconciliation CAS to
            # finish the service-owned job.  Never promote a packet based on
            # metadata alone.
            artifact_ok = True
            artifact_paths = (
                (packet.dossier_path, packet.dossier_sha256),
                (packet.task_path, packet.task_sha256),
            )
            for path, expected_digest in artifact_paths:
                if not path or not expected_digest:
                    artifact_ok = False
                    break
                try:
                    resolved = _safe_resolve(path)
                    content, truncated = _read_workspace_text_bounded(
                        resolved, max_bytes=PACKET_MAX_BYTES if path == packet.dossier_path else TASK_MAX_BYTES
                    )
                    if (
                        truncated
                        or not resolved.is_file()
                        or resolved.is_symlink()
                        or resolved.stat().st_nlink != 1
                        or _sha(content) != str(expected_digest)
                    ):
                        artifact_ok = False
                        break
                except (OSError, ValueError):
                    artifact_ok = False
                    break
            if not artifact_ok:
                return {
                    "status": "blocked",
                    "job_id": job_id,
                    "packet_id": packet.id,
                    "reason_code": "artifact_readback_missing",
                    "operator_visible": True,
                }
            if job.get("status") not in {"succeeded", "degraded"}:
                try:
                    job = await durable_job_repository.finalize_reconciled_job(
                        job_id,
                        owner_kind=SERVICE_OWNER_KIND,
                        owner_principal_id=SERVICE_PRINCIPAL,
                        expected_revision=job.get("revision"),
                        result={
                            "source_watch_id": watch_id,
                            "packet_id": packet.id,
                            "status": packet.status,
                            "learning": NO_LEARNING,
                            "memory_status": NO_LEARNING,
                        },
                        result_summary="source-watch packet and local artifacts reconciled after restart",
                    )
                except Exception:
                    return {
                        "status": "blocked",
                        "job_id": job_id,
                        "packet_id": packet.id,
                        "reason_code": "durable_job_reconciliation_required",
                        "operator_visible": True,
                    }
            final_status = "degraded" if packet.status == "degraded" else "succeeded"
            await self._release_watch(watch_id, job_id, int(watch.active_job_fence), final_status)
            return {"status": final_status, "job_id": job_id, "packet_id": packet.id, "recovery": "verified_and_reconciled", "learning": NO_LEARNING}
        return {
            "status": "blocked",
            "job_id": job_id,
            "packet_id": packet.id,
            "reason_code": packet.failure_code or "reconciliation_required",
            "operator_visible": True,
        }

    async def _execute_packet(
        self,
        watch: GuardianSourceWatch,
        packet: GuardianDecisionPacket,
        job_id: str,
        *,
        claimed: Mapping[str, Any] | None = None,
        scan: ScanResult | None = None,
    ) -> None:
        current = dict(claimed or await durable_job_repository.get_job(job_id) or {})
        lease = current.get("lease") or {}
        owner = str(lease.get("owner") or SERVICE_PRINCIPAL)
        fence = int(lease.get("fencing_token") or 0)
        revision = int(current.get("revision") or 0)
        dossier_path = f"guardian/source-watches/{watch.id}/packets/{packet.id}.md"
        task_path = f"guardian/source-watches/{watch.id}/tasks/{packet.id}.md"
        intent = await durable_job_repository.record_checkpoint(
            job_id,
            checkpoint_id="workspace_write_intent",
            state={
                "phase": "workspace_write_intent",
                "packet_id": packet.id,
                "paths": [dossier_path, task_path],
                "digests": {
                    dossier_path: _sha(packet.proposal_text),
                    task_path: _sha(packet.task_text),
                },
            },
            checkpoint_payload={
                "packet_id": packet.id,
                "paths": [dossier_path, task_path],
                "dossier_sha256": _sha(packet.proposal_text),
                "task_sha256": _sha(packet.task_text),
            },
            safe=True,
            owner=owner,
            fencing_token=fence,
            expected_revision=revision,
        )
        revision = int(intent.get("revision") or revision)
        _write_workspace_text_bounded(_safe_resolve(dossier_path), packet.proposal_text, max_bytes=PACKET_MAX_BYTES)
        _write_workspace_text_bounded(_safe_resolve(task_path), packet.task_text, max_bytes=TASK_MAX_BYTES)
        dossier_read, dossier_truncated = _read_workspace_text_bounded(_safe_resolve(dossier_path), max_bytes=PACKET_MAX_BYTES)
        task_read, task_truncated = _read_workspace_text_bounded(_safe_resolve(task_path), max_bytes=TASK_MAX_BYTES)
        if dossier_truncated or task_truncated or _sha(dossier_read) != _sha(packet.proposal_text) or _sha(task_read) != _sha(packet.task_text):
            raise SourceWatchError("artifact_readback_mismatch")
        dossier_record = build_artifact_record(
            file_path=dossier_path,
            artifact_type="guardian_decision_dossier",
            producer=CAPABILITY_ID,
            run_id=job_id,
            session_id=watch.owner_session_id,
            content=dossier_read,
            trust_boundary="local_workspace",
        )
        task_record = build_artifact_record(
            file_path=task_path,
            artifact_type="guardian_local_task",
            producer=CAPABILITY_ID,
            run_id=job_id,
            session_id=watch.owner_session_id,
            content=task_read,
            trust_boundary="local_workspace",
        )
        for record in (dossier_record, task_record):
            artifact_receipt = await durable_job_repository.record_artifact(
                job_id,
                file_path=str(record["file_path"]),
                artifact_type=str(record["artifact_type"]),
                content=dossier_read if record is dossier_record else task_read,
                owner=owner,
                fencing_token=fence,
                expected_revision=revision,
            )
            revision = int(artifact_receipt.get("revision") or revision)
        for path, record in ((dossier_path, dossier_record), (task_path, task_record)):
            effect = await durable_job_repository.record_effect(
                job_id,
                effect_type="workspace_write",
                target_path=path,
                target_digest=str(record["content_sha256"]),
                content_sha256=str(record["content_sha256"]),
                status="succeeded",
                details={"verified": True, "output_exists": True, "workspace_contained": True},
                owner=owner,
                fencing_token=fence,
                expected_revision=revision,
            )
            revision = int(effect.get("revision") or revision)
            effect_id = (effect.get("receipt") or {}).get("effect_id")
            readback = await durable_job_repository.record_readback(
                job_id,
                target_path=path,
                effect_id=effect_id,
                effect_type="workspace_write",
                target_digest=str(record["content_sha256"]),
                content_sha256=str(record["content_sha256"]),
                status="succeeded",
                details={"verified": True, "output_exists": True, "workspace_contained": True},
                owner=owner,
                fencing_token=fence,
                expected_revision=revision,
            )
            revision = int(readback.get("revision") or revision)
        # Finalize the packet and successful baselines first.  If the durable
        # job CAS below loses a race or the process dies, recovery can inspect
        # the already verified packet/files instead of exposing a successful
        # job whose canonical packet is still prepared.
        await self._finalize_packet(
            watch,
            packet,
            dossier_path=dossier_path,
            task_path=task_path,
            dossier_record=dossier_record,
            task_record=task_record,
            scan=scan,
        )
        await durable_job_repository.transition_job(
            job_id,
            "succeeded",
            owner=owner,
            fencing_token=fence,
            expected_revision=revision,
            result={
                "source_watch_id": watch.id,
                "packet_id": packet.id,
                "dossier_artifact_id": dossier_record["artifact_id"],
                "dossier_sha256": dossier_record["content_sha256"],
                "status": "degraded" if scan is not None and scan.degraded else "succeeded",
                "learning": NO_LEARNING,
                "memory_status": NO_LEARNING,
            },
            result_summary="verified source-watch dossier and local task readback",
        )

    async def _settle_observation_job(self, job_id: str, *, status: str, reason: str) -> None:
        current = await durable_job_repository.get_job(job_id)
        if current is None or current.get("status") != "running":
            return
        lease = current.get("lease") or {}
        owner = lease.get("owner") or SERVICE_PRINCIPAL
        fence = int(lease.get("fencing_token") or 0)
        revision = int(current.get("revision") or 0)
        effect = await durable_job_repository.record_effect(
            job_id,
            effect_type="source_observation",
            target_path=f"source-watch:{job_id}",
            target_digest=_sha(reason),
            status="succeeded",
            details={"verified": True, "output_exists": True, "workspace_contained": True, "reason": reason},
            owner=owner,
            fencing_token=fence,
            expected_revision=revision,
        )
        revision = int(effect.get("revision") or revision)
        effect_id = (effect.get("receipt") or {}).get("effect_id")
        readback = await durable_job_repository.record_readback(
            job_id,
            target_path=f"source-watch:{job_id}",
            effect_id=effect_id,
            effect_type="source_observation",
            target_digest=_sha(reason),
            status="succeeded",
            details={"verified": True, "output_exists": True, "workspace_contained": True},
            owner=owner,
            fencing_token=fence,
            expected_revision=revision,
        )
        await durable_job_repository.transition_job(
            job_id,
            status,
            owner=owner,
            fencing_token=fence,
            expected_revision=readback.get("revision"),
            reason=reason,
            result={
                "status": status,
                "learning": "no_learning",
                "memory_status": "no_learning",
            },
            result_summary=reason,
        )

    async def _commit_baselines(self, watch: GuardianSourceWatch, scan: ScanResult, *, status: str) -> None:
        async with db_engine.get_session() as db:
            for item in scan.baseline_updates:
                if item.baseline_text is None:
                    continue
                row = (
                    await db.execute(
                        select(GuardianSourceBaseline).where(
                            GuardianSourceBaseline.watch_id == watch.id,
                            GuardianSourceBaseline.source_key == item.source.source_key,
                        )
                    )
                ).scalars().first()
                if row is None:
                    row = GuardianSourceBaseline(
                        watch_id=watch.id,
                        source_key=item.source.source_key,
                        kind=item.source.kind,
                        target=item.source.target,
                        identity_digest=item.source.identity_digest,
                        generation=1,
                    )
                elif item.rebaseline:
                    row.generation = int(row.generation or 1) + 1
                row.baseline_text = item.baseline_text
                row.baseline_sha256 = item.new_hash or ""
                row.identity_digest = item.source.identity_digest
                row.state = "ready"
                row.etag = item.etag
                row.last_modified = item.last_modified
                row.observed_at = _now()
                row.updated_at = _now()
                db.add(row)
            current = (
                await db.execute(select(GuardianSourceWatch).where(GuardianSourceWatch.id == watch.id))
            ).scalars().first()
            if current is not None:
                current.last_status = status
                current.updated_at = _now()
                db.add(current)

    async def _finalize_packet(
        self,
        watch: GuardianSourceWatch,
        packet: GuardianDecisionPacket,
        *,
        dossier_path: str,
        task_path: str,
        dossier_record: Mapping[str, Any],
        task_record: Mapping[str, Any],
        scan: ScanResult | None,
    ) -> None:
        async with db_engine.get_session() as db:
            row = (
                await db.execute(select(GuardianDecisionPacket).where(GuardianDecisionPacket.id == packet.id))
            ).scalars().first()
            if row is None:
                raise SourceWatchError("packet_not_found")
            row.status = "degraded" if scan is not None and scan.degraded else "succeeded"
            row.verification_status = "passed"
            row.memory_status = "no_learning"
            row.dossier_path = dossier_path
            row.dossier_artifact_id = str(dossier_record["artifact_id"])
            row.dossier_sha256 = str(dossier_record["content_sha256"])
            row.task_path = task_path
            row.task_artifact_id = str(task_record["artifact_id"])
            row.task_sha256 = str(task_record["content_sha256"])
            row.outcome_json = _dump(
                {
                    "source_watch_id": watch.id,
                    "packet_id": packet.id,
                    "dossier_artifact_id": row.dossier_artifact_id,
                    "dossier_sha256": row.dossier_sha256,
                    "task_artifact_id": row.task_artifact_id,
                    "task_sha256": row.task_sha256,
                    "readback": "passed",
                }
            )
            row.updated_at = _now()
            db.add(row)
        if scan is not None:
            await self._commit_baselines(
                watch,
                scan,
                status="degraded" if scan.degraded else "succeeded",
            )

    async def correct(
        self,
        *,
        watch_id: str,
        owner_principal_id: str,
        owner_session_id: str,
        expected_goal_revision: int,
        expected_plan_revision: int,
        include_terms: Sequence[str],
        exclude_terms: Sequence[str],
        reason: str,
    ) -> dict[str, Any]:
        criteria = parse_criteria(
            {
                "include_terms": list(include_terms),
                "exclude_terms": list(exclude_terms),
                "min_changed_lines": 1,
                "min_changed_chars": 1,
                "max_material_sources": 3,
            }
        )
        async with db_engine.get_session() as db:
            watch = (
                await db.execute(
                    select(GuardianSourceWatch).where(
                        GuardianSourceWatch.id == watch_id,
                        GuardianSourceWatch.owner_principal_id == owner_principal_id,
                        GuardianSourceWatch.owner_session_id == owner_session_id,
                    )
                )
            ).scalars().first()
            if watch is None:
                raise SourceWatchError("watch_not_found")
            goal = (await db.execute(select(Goal).where(Goal.id == watch.goal_id))).scalars().first()
            if goal is None or goal.revision != expected_goal_revision or watch.plan_revision != expected_plan_revision:
                raise SourceWatchError("correction_revision_stale")
            old = _load(watch.criteria_json, {})
            new = {
                **old,
                "include_terms": list(criteria.include_terms),
                "exclude_terms": list(criteria.exclude_terms),
            }
            next_goal_revision = int(goal.revision) + 1
            delta = StrategyDelta(
                goal_id=watch.goal_id,
                scope="goal",
                field_name="research_watch_criteria",
                before_json=_dump(old),
                after_json=_dump(new),
                source_event_id=f"source-watch-correction:{watch.id}:{watch.plan_revision}",
                author_id=owner_principal_id,
                evaluator_id=owner_principal_id,
                goal_revision_before=goal.revision,
                goal_revision_after=next_goal_revision,
                status="active",
                reason=_text(reason)[:1000],
            )
            goal.revision = next_goal_revision
            goal.updated_at = _now()
            watch.goal_revision = next_goal_revision
            watch.plan_revision += 1
            watch.criteria_json = _dump(new)
            watch.criteria_digest = _sha(watch.criteria_json)
            watch.updated_at = _now()
            db.add(goal)
            db.add(watch)
            db.add(delta)
            await db.flush()
            return {
                "status": "succeeded",
                "watch_id": watch.id,
                "goal_revision": watch.goal_revision,
                "plan_revision": watch.plan_revision,
                "strategy_delta_id": delta.delta_id,
                "memory_status": "no_learning",
            }


class SourceWatchSourceRequest(BaseModel):
    source_key: str
    kind: str
    target: str
    label: str = ""
    priority: int = Field(default=3, ge=1, le=5)


class SourceWatchCreateRequest(BaseModel):
    goal_id: str
    expected_goal_revision: int = Field(ge=1)
    sources: list[SourceWatchSourceRequest] = Field(min_length=1, max_length=MAX_SOURCES)
    criteria: dict[str, Any] = Field(default_factory=dict)
    schedule: dict[str, Any]
    write_mode: str = "approval_each_run"
    reviewed_grant_id: str | None = None


class SourceWatchUpdateRequest(BaseModel):
    expected_plan_revision: int = Field(ge=1)
    sources: list[SourceWatchSourceRequest] | None = None
    criteria: dict[str, Any] | None = None
    schedule: dict[str, Any] | None = None
    write_mode: str | None = None
    state: str | None = None


class SourceWatchExecuteRequest(BaseModel):
    expected_packet_digest: str
    approval_id: str | None = None
    expected_approval_revision: int | None = None


class SourceWatchRunRequest(BaseModel):
    expected_plan_revision: int = Field(ge=1)
    occurrence_id: str | None = None


class SourceWatchRecoveryRequest(BaseModel):
    job_id: str
    expected_plan_revision: int = Field(ge=1)


class SourceWatchCancelRequest(BaseModel):
    job_id: str
    expected_plan_revision: int = Field(ge=1)
    expected_fencing_token: int = Field(ge=1)


class SourceWatchCorrectionRequest(BaseModel):
    expected_goal_revision: int = Field(ge=1)
    expected_plan_revision: int = Field(ge=1)
    include_terms: list[str] = Field(default_factory=list)
    exclude_terms: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1, max_length=1000)


source_watch_router = APIRouter()
source_watch_service = SourceWatchService()


def _operator(request: Request):
    from src.api.capabilities import _require_authenticated_capability_operator

    return _require_authenticated_capability_operator(request)


def _http_error(exc: SourceWatchError) -> HTTPException:
    code = exc.code
    status = 409 if "stale" in code or "revision" in code or "mismatch" in code else 400
    if code in {"watch_not_found", "packet_not_found"}:
        status = 404
    return HTTPException(status_code=status, detail={"code": code, "message": str(exc)})


@source_watch_router.post("/capabilities/source-watches")
async def create_source_watch(req: SourceWatchCreateRequest, request: Request):
    operator = _operator(request)
    try:
        return await source_watch_service.create_watch(
            owner_principal_id=operator.principal.principal_id,
            owner_session_id=operator.session_id,
            goal_id=req.goal_id,
            expected_goal_revision=req.expected_goal_revision,
            sources=[item.model_dump() for item in req.sources],
            criteria=req.criteria,
            schedule=req.schedule,
            write_mode=req.write_mode,
            reviewed_grant_id=req.reviewed_grant_id,
        )
    except SourceWatchError as exc:
        raise _http_error(exc) from exc


@source_watch_router.get("/capabilities/source-watches")
async def list_source_watches(request: Request):
    operator = _operator(request)
    return await source_watch_service.list_watches(owner_principal_id=operator.principal.principal_id)


@source_watch_router.get("/capabilities/source-watches/{watch_id}")
async def get_source_watch(watch_id: str, request: Request):
    operator = _operator(request)
    result = await source_watch_service.get_watch(
        watch_id,
        owner_principal_id=operator.principal.principal_id,
    )
    if result is None:
        raise HTTPException(status_code=404, detail={"code": "watch_not_found"})
    return result


@source_watch_router.patch("/capabilities/source-watches/{watch_id}")
async def update_source_watch(
    watch_id: str,
    req: SourceWatchUpdateRequest,
    request: Request,
):
    operator = _operator(request)
    try:
        return await source_watch_service.update_watch(
            watch_id=watch_id,
            owner_principal_id=operator.principal.principal_id,
            owner_session_id=operator.session_id,
            expected_plan_revision=req.expected_plan_revision,
            sources=[item.model_dump() for item in req.sources] if req.sources is not None else None,
            criteria=req.criteria,
            schedule=req.schedule,
            write_mode=req.write_mode,
            state=req.state,
        )
    except SourceWatchError as exc:
        raise _http_error(exc) from exc


@source_watch_router.post("/capabilities/source-watches/{watch_id}/run")
async def run_source_watch(watch_id: str, req: SourceWatchRunRequest, request: Request):
    operator = _operator(request)
    watch = await source_watch_service.get_watch(
        watch_id,
        owner_principal_id=operator.principal.principal_id,
    )
    if watch is None:
        raise HTTPException(status_code=404, detail={"code": "watch_not_found"})
    if int(watch.get("plan_revision", 0)) != int(req.expected_plan_revision):
        raise HTTPException(status_code=409, detail={"code": "watch_plan_revision_stale"})
    result = await source_watch_service.run_watch(
        watch_id,
        occurrence_id=req.occurrence_id or str(uuid.uuid4()),
        expected_plan_revision=req.expected_plan_revision,
        expected_owner_session_id=operator.session_id,
    )
    return result


@source_watch_router.post("/capabilities/source-watches/{watch_id}/recover")
async def recover_source_watch(
    watch_id: str,
    req: SourceWatchRecoveryRequest,
    request: Request,
):
    operator = _operator(request)
    try:
        return await source_watch_service.recover_job(
            watch_id=watch_id,
            job_id=req.job_id,
            expected_plan_revision=req.expected_plan_revision,
            owner_principal_id=operator.principal.principal_id,
            owner_session_id=operator.session_id,
        )
    except SourceWatchError as exc:
        raise _http_error(exc) from exc


@source_watch_router.post("/capabilities/source-watches/{watch_id}/cancel")
async def cancel_source_watch(
    watch_id: str,
    req: SourceWatchCancelRequest,
    request: Request,
):
    operator = _operator(request)
    try:
        return await source_watch_service.cancel_watch_job(
            watch_id=watch_id,
            job_id=req.job_id,
            expected_plan_revision=req.expected_plan_revision,
            expected_fencing_token=req.expected_fencing_token,
            owner_principal_id=operator.principal.principal_id,
            owner_session_id=operator.session_id,
        )
    except SourceWatchError as exc:
        raise _http_error(exc) from exc


@source_watch_router.post("/capabilities/source-watches/{watch_id}/packets/{packet_id}/execute")
async def execute_source_watch_packet(
    watch_id: str,
    packet_id: str,
    req: SourceWatchExecuteRequest,
    request: Request,
):
    operator = _operator(request)
    try:
        return await source_watch_service.execute_packet(
            watch_id=watch_id,
            packet_id=packet_id,
            expected_packet_digest=req.expected_packet_digest,
            approval_id=req.approval_id,
            expected_approval_revision=req.expected_approval_revision,
            owner_principal_id=operator.principal.principal_id,
            owner_session_id=operator.session_id,
        )
    except SourceWatchError as exc:
        raise _http_error(exc) from exc


@source_watch_router.post("/capabilities/source-watches/{watch_id}/corrections")
async def correct_source_watch(
    watch_id: str,
    req: SourceWatchCorrectionRequest,
    request: Request,
):
    operator = _operator(request)
    try:
        return await source_watch_service.correct(
            watch_id=watch_id,
            owner_principal_id=operator.principal.principal_id,
            owner_session_id=operator.session_id,
            expected_goal_revision=req.expected_goal_revision,
            expected_plan_revision=req.expected_plan_revision,
            include_terms=req.include_terms,
            exclude_terms=req.exclude_terms,
            reason=req.reason,
        )
    except SourceWatchError as exc:
        raise _http_error(exc) from exc


__all__ = [
    "CAPABILITY_ID",
    "CAPABILITY_VERSION",
    "SourceObservation",
    "SourceSpec",
    "SourceWatchCancelRequest",
    "SourceWatchService",
    "WatchCriteria",
    "build_dossier",
    "build_task",
    "compute_input_digest",
    "material_change",
    "normalize_source_text",
    "parse_criteria",
    "parse_sources",
    "redact_export_text",
    "source_identity_digest",
    "source_watch_router",
    "source_watch_service",
]
