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
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx
from apscheduler.triggers.cron import CronTrigger
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from config.settings import settings
from src.approval.repository import approval_repository, fingerprint_tool_call
from src.artifacts.registry import artifact_id_for, build_artifact_record
from src.audit.runtime import log_integration_event
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
from src.goals.repository import deserialize_admission_budget, serialize_admission_budget
from src.observer.native_notification_queue import (
    NativeNotificationBudgetDenied,
    native_notification_queue,
)
from src.security.http_transport import (
    PinnedTransportError,
    fetch_pinned_https,
    parse_public_https_url,
)
from src.tools.filesystem_tool import (
    _assert_not_secret_like_path,
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
SOURCE_READ_DEADLINE_SECONDS = 10
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
    (re.compile(r"(?i)\b(?:sk|rk|pk|ghp|github_pat|xox[baprs])-[A-Za-z0-9_-]{12,}"), "[REDACTED_TOKEN]"),
    (re.compile(r"(?i)\beyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "[REDACTED_JWT]"),
    (re.compile(r"(?s)-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----"), "[REDACTED_PRIVATE_KEY]"),
)

_SENSITIVE_EXPORT_KEYS = {
    "baseline_text",
    "changed_text",
    "source_text",
    "raw_source",
    "raw_content",
    "content",
    "secret",
    "token",
    "password",
    "api_key",
    "apikey",
}
_SENSITIVE_QUERY_NAMES = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "client_secret",
    "credential",
    "password",
    "secret",
    "sig",
    "signature",
    "token",
    "x_apikey",
    "x_api_key",
    "xapikey",
}


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


def _failed_recovery_projection(durable_status: str | None) -> dict[str, str]:
    """Project failed-job settlement without hiding uncertain effects."""

    status = _text(durable_status) or "blocked"
    uncertain = status in {"unknown_external_effect", "cost_liability"}
    public_status = status if uncertain else ("cancelled" if status == "cancelled" else "blocked")
    return {
        "status": public_status,
        "watch_status": "blocked" if uncertain or public_status != "cancelled" else "cancelled",
        "recovery": (
            "failed_occurrence_reconciliation_required"
            if uncertain
            else "failed_occurrence_settled"
        ),
        "reason_code": (
            f"{status}_pending_reconciliation"
            if uncertain
            else "failed_occurrence_reconciled"
        ),
        "durable_status": status,
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
    if not bool(getattr(goal, "proactive_enabled", False)):
        return False, "goal_proactive_disabled", budget
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


def _recovery_binding_error(
    *,
    watch_id: str,
    goal_id: str,
    goal_revision: int,
    plan_revision: int,
    source_set_digest: str,
    criteria_digest: str,
    sources_json: str,
    job_id: str,
    job: Mapping[str, Any],
    packet: GuardianDecisionPacket | Mapping[str, Any] | None,
) -> str | None:
    """Validate every durable recovery identity against the live watch plan."""

    def value(item: GuardianDecisionPacket | Mapping[str, Any], key: str, default: Any = None) -> Any:
        if isinstance(item, Mapping):
            return item.get(key, default)
        return getattr(item, key, default)

    authority = job.get("declared_authority") if isinstance(job.get("declared_authority"), Mapping) else {}
    if (
        _text(job.get("job_id")) != _text(job_id)
        or _text(job.get("job_kind")) != "guardian_source_watch"
        or _text(job.get("goal_id")) != _text(goal_id)
        or int(job.get("goal_revision") or 0) != int(goal_revision)
        or int(job.get("plan_revision") or 0) != int(plan_revision)
        or _text(authority.get("goal_id")) != _text(goal_id)
        or int(authority.get("goal_revision") or 0) != int(goal_revision)
        or int(authority.get("plan_revision") or 0) != int(plan_revision)
        or _text(authority.get("source_set_digest")) != _text(source_set_digest)
        or _text(authority.get("criteria_digest")) != _text(criteria_digest)
    ):
        return "durable_job_binding_mismatch"
    if packet is None:
        return None
    if (
        _text(value(packet, "source_watch_id")) != _text(watch_id)
        or _text(value(packet, "watch_id")) != _text(watch_id)
        or _text(value(packet, "run_identity")) != _text(job_id)
        or _text(value(packet, "goal_id")) != _text(goal_id)
        or int(value(packet, "goal_revision") or 0) != int(goal_revision)
        or int(value(packet, "plan_revision") or 0) != int(plan_revision)
        or _text(value(packet, "criteria_digest")) != _text(criteria_digest)
    ):
        return "recovery_packet_binding_mismatch"
    checkpoint = _load(value(packet, "observed_checkpoint_json"), {})
    if not isinstance(checkpoint, Mapping) or checkpoint.get("schema") != "seraph.guardian.source-observation.v1":
        return "recovery_packet_checkpoint_mismatch"
    try:
        expected_sources = {
            item.source_key: item.identity_digest
            for item in parse_sources(_load(sources_json, []))
        }
    except (SourceWatchError, TypeError, ValueError):
        return "recovery_source_set_stale"
    observed_sources = {
        _text(item.get("source_key")): _text(item.get("identity_digest"))
        for item in checkpoint.get("sources", [])
        if isinstance(item, Mapping)
    }
    # Older packets may have been written before a first-seen/rebaselined
    # source was removed from the action projection.  Recovery must reject a
    # packet that names an unknown source or a mismatched identity, while
    # allowing a deliberately smaller checkpoint to be replayed against the
    # same canonical plan.  New packets still carry the complete set.
    if any(
        source_key not in expected_sources
        or expected_sources[source_key] != identity_digest
        for source_key, identity_digest in observed_sources.items()
    ):
        return "recovery_source_set_stale"
    return None


def _sha(value: str | bytes) -> str:
    raw = value if isinstance(value, bytes) else value.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _safe_export_value(value: Any, *, key: str = "", depth: int = 0) -> Any:
    """Project operator/API values without retaining source or secret text."""

    if depth > 5:
        return "[REDACTED_DEPTH]"
    normalized_key = key.casefold().replace("-", "_")
    if normalized_key in _SENSITIVE_EXPORT_KEYS:
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(item_key): _safe_export_value(item_value, key=str(item_key), depth=depth + 1)
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe_export_value(item, depth=depth + 1) for item in value]
    if isinstance(value, str):
        return redact_export_text(value)[0]
    return value


def _safe_source_projection(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return _safe_export_value(value)
    result: dict[str, Any] = {}
    kind = _text(value.get("kind"))
    for key, item in value.items():
        key_text = str(key)
        if key_text in {"baseline_text", "changed_text"}:
            continue
        if key_text == "target" and kind == "public_https_text":
            result[key_text] = _safe_public_source_target(item)
        else:
            result[key_text] = _safe_export_value(item, key=key_text)
    return result


def _safe_public_source_target(value: Any) -> str:
    """Project a legacy public URL without exposing userinfo or bad input."""

    raw = _text(value)
    try:
        parsed = urlsplit(raw)
        # Parse userinfo before the strict validator so a legacy row can be
        # projected safely even though new admission rejects it.
        if parsed.username or parsed.password:
            host = parsed.hostname or ""
            query = _safe_public_query(parsed.query)
            return urlunsplit((parsed.scheme, host, parsed.path, query, ""))
        parse_public_https_url(raw)
    except (PinnedTransportError, ValueError):
        return "[REDACTED_INVALID_SOURCE_URL]"
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            _safe_public_query(parsed.query),
            "",
        )
    )


def _query_name_is_sensitive(name: str) -> bool:
    normalized = name.casefold().replace("-", "_")
    return normalized in _SENSITIVE_QUERY_NAMES or any(
        marker in normalized for marker in ("token", "secret", "password", "credential", "signature", "auth")
    )


def _query_has_credentials(query: str) -> bool:
    return any(_query_name_is_sensitive(name) for name, _ in parse_qsl(query, keep_blank_values=True))


def _safe_public_query(query: str) -> str:
    values = [
        (name, "[REDACTED]" if _query_name_is_sensitive(name) else value)
        for name, value in parse_qsl(query, keep_blank_values=True)
    ]
    return urlencode(values, doseq=True)


def _completion_notification_body(
    *,
    packet_id: str,
    status: str,
    dossier_sha256: str,
    task_sha256: str,
) -> str:
    """Build the bounded post-readback body without source material."""

    return (
        f"Guardian source watch completed: packet={_text(packet_id)}; "
        f"status={_text(status)[:32]}; dossier_sha256={_text(dossier_sha256)}; "
        f"task_sha256={_text(task_sha256)}."
    )


def _restore_prior_criteria(
    current: Mapping[str, Any],
    *,
    prior_status: str,
    prior_before_json: str | None,
    prior_after_json: str | None,
) -> dict[str, Any]:
    """Validate and restore one prior criteria snapshot for an undo CAS."""

    before = _load(prior_before_json, {})
    after = _load(prior_after_json, {})
    if (
        prior_status not in {"active", "applied"}
        or not isinstance(before, Mapping)
        or not isinstance(after, Mapping)
        or dict(after) != dict(current)
    ):
        raise SourceWatchError("correction_undo_target_stale")
    restored = dict(before)
    parse_criteria(restored)
    return restored


async def _audit_watch_event(
    watch: GuardianSourceWatch | Mapping[str, Any] | None,
    outcome: str,
    **details: Any,
) -> None:
    """Emit one canonical, source-content-free watch lifecycle receipt."""

    if watch is None:
        return
    get = watch.get if isinstance(watch, Mapping) else lambda key, default=None: getattr(watch, key, default)
    watch_id = _text(get("id"))
    if not watch_id:
        return
    safe_details: dict[str, Any] = {
        "watch_id": watch_id,
        "goal_id": _text(get("goal_id")),
        "goal_revision": int(get("goal_revision") or 0),
        "plan_revision": int(get("plan_revision") or 0),
        "source_set_digest": _text(get("source_set_digest")),
        "criteria_digest": _text(get("criteria_digest")),
        **details,
    }
    safe_details = _safe_export_value(safe_details)
    await log_integration_event(
        integration_type=SERVICE_ID,
        name="source_watch",
        outcome=outcome,
        details=safe_details,
        session_id=_text(get("owner_session_id")) or None,
        actor=SERVICE_PRINCIPAL,
        principal_id=_text(get("owner_principal_id")) or SERVICE_PRINCIPAL,
    )


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
        if kind == "public_https_text":
            if not target.lower().startswith("https://"):
                raise SourceWatchError("source_https_required")
            try:
                parse_public_https_url(target)
            except (PinnedTransportError, ValueError) as exc:
                raise SourceWatchError("source_url_invalid", str(exc)) from exc
            parsed_target = urlsplit(target)
            if _query_has_credentials(parsed_target.query):
                raise SourceWatchError("source_url_credentials_blocked")
        if kind == "workspace_text":
            try:
                _assert_not_secret_like_path(target, "source_watch_read")
            except ValueError as exc:
                raise SourceWatchError("workspace_secret_path_blocked", str(exc)) from exc
            if target.startswith("/") or ".." in Path(target).parts or target.startswith("~"):
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
    # A source without a canonical baseline is initialized by this occurrence;
    # it must not create a packet/task before the operator has a prior value to
    # compare against.
    if observation.old_hash is None or observation.old_hash == observation.new_hash or observation.rebaseline:
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


def _baseline_only_observations(
    observations: Sequence[SourceObservation],
) -> tuple[tuple[SourceObservation, ...], str | None]:
    """Identify observations that must initialize or replace a baseline."""

    baseline_only = tuple(
        item
        for item in observations
        if item.status == "observed" and (item.old_hash is None or item.rebaseline)
    )
    if not baseline_only:
        return (), None
    status = "rebaseline_required" if any(item.rebaseline for item in baseline_only) else "baseline_initialized"
    return baseline_only, status


def _stale_baseline_source_keys(
    old_sources: Sequence[Mapping[str, Any]],
    new_sources: Sequence[SourceSpec],
) -> tuple[str, ...]:
    """Return source keys whose prior baseline cannot cross a plan edit."""

    old_identities = {
        _text(item.get("source_key")): _text(item.get("identity_digest"))
        for item in old_sources
        if isinstance(item, Mapping) and _text(item.get("source_key"))
    }
    new_identities = {item.source_key: item.identity_digest for item in new_sources}
    return tuple(
        sorted(
            key
            for key, identity_digest in old_identities.items()
            if key not in new_identities or new_identities[key] != identity_digest
        )
    )


def _partition_baseline_observations(
    scan: ScanResult,
    *,
    watch_id: str,
    goal_revision: int,
    plan_revision: int,
    source_set_digest: str,
    criteria_digest: str,
    capability_version: str,
) -> tuple[tuple[SourceObservation, ...], str | None, ScanResult]:
    """Remove baseline-only sources from the action projection of one scan.

    The full scan remains the durable observation receipt, while packets and
    no-change projections contain only sources that were already comparable.
    This lets one first-seen/rebaselined source advance its baseline without
    suppressing an eligible change from another source in the same occurrence.
    """

    baseline_only, status = _baseline_only_observations(scan.observations)
    if not baseline_only:
        return (), None, scan
    baseline_keys = {item.source.source_key for item in baseline_only}
    action_observations = tuple(
        item for item in scan.observations if item.source.source_key not in baseline_keys
    )
    action_material = tuple(
        item for item in scan.material if item.source.source_key not in baseline_keys
    )
    action_updates = tuple(
        item for item in scan.baseline_updates if item.source.source_key not in baseline_keys
    )
    # Keep the complete source set in the immutable packet checkpoint.  The
    # action projection excludes baseline-only material, but recovery must be
    # able to bind every source in the watch plan and retain the comparable
    # source that triggered the action.
    checkpoint = {
        "schema": "seraph.guardian.source-observation.v1",
        "sources": [_observation_checkpoint(item) for item in scan.observations],
    }
    checkpoint_sha = _sha(_dump(checkpoint))
    return baseline_only, status, replace(
        scan,
        observations=action_observations,
        material=action_material,
        checkpoint=checkpoint,
        checkpoint_sha256=checkpoint_sha,
        # ``scan.input_digest`` is occurrence-bound by ``_scan``.  Reusing a
        # digest recomputed from the reduced action projection here would make
        # a mixed baseline/material packet fail recovery validation.
        input_digest=scan.input_digest,
        baseline_updates=action_updates,
    )


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


def _bind_occurrence_input_digest(input_digest: str, occurrence_id: str | None) -> str:
    """Make packet identity unique to one durable occurrence.

    The observation content remains the input, while the durable occurrence
    prevents a cancelled/failed approval packet from being reused by a later
    watch job with a different active fence.
    """

    if not _text(occurrence_id):
        return input_digest
    return _sha(_dump({"observation_input_digest": input_digest, "occurrence_id": _text(occurrence_id)}))


def _safe_excerpt(value: str) -> tuple[str, dict[str, Any]]:
    return redact_export_text(value[:4096])


def _observation_checkpoint(item: SourceObservation) -> dict[str, Any]:
    """Return the metadata-only durable observation projection.

    Source text and excerpts are never exported in a dossier or API receipt.
    The canonical baseline remains in the owner-bound baseline table; durable
    packet/checkpoint projections retain only hashes and bounded metadata.
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
        safe_label = redact_export_text(item.source.label)[0]
        safe_key = redact_export_text(item.source.source_key)[0]
        lines.extend(
            [
                f"{index}. {safe_label} [{safe_key}] priority={item.source.priority}",
                f"   target: {redact_export_text(item.source.target)[0]}",
                f"   old_sha256: {item.old_hash or 'none'}",
                f"   new_sha256: {item.new_hash or 'none'}",
                f"   changed_lines: {item.changed_lines}; changed_chars: {item.changed_chars}",
                f"   citation: source_key={safe_key}; identity={item.source.identity_digest}",
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
        lines.append(f"- Review cited change for source {redact_export_text(item.source.source_key)[0]}.")
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
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise SourceWatchError("source_transport_timeout", str(exc)) from exc
        except (PinnedTransportError, OSError) as exc:
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
        _assert_not_secret_like_path(source.target, "source_watch_read")
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
            if not bool(getattr(goal, "proactive_enabled", False)):
                raise SourceWatchError("goal_proactive_disabled")
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
                read_authority_json=_dump(
                    {
                        "source_keys": [item.source_key for item in parsed_sources],
                        "grant_id": _text(goal_budget.grant_id),
                    }
                ),
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
        result = await self.get_watch(
            watch_id,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )
        await _audit_watch_event(result, "created", write_mode=write_mode)
        return result or {}

    async def get_watch(
        self,
        watch_id: str,
        *,
        owner_principal_id: str,
        owner_session_id: str | None = None,
    ) -> dict[str, Any] | None:
        async with db_engine.get_session() as db:
            predicates = [
                GuardianSourceWatch.id == watch_id,
                GuardianSourceWatch.owner_principal_id == owner_principal_id,
            ]
            if owner_session_id is not None:
                predicates.append(GuardianSourceWatch.owner_session_id == owner_session_id)
            watch = (
                await db.execute(
                    select(GuardianSourceWatch).where(*predicates)
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
                "sources": [
                    _safe_source_projection(item)
                    for item in _load(watch.sources_json, [])
                    if isinstance(item, Mapping)
                ],
                "criteria": _safe_export_value(_load(watch.criteria_json, {})),
                "schedule": _load(watch.schedule_spec_json, {}),
                "read_authority": _safe_export_value(_load(watch.read_authority_json, {})),
                "write_authority": _safe_export_value(_load(watch.write_authority_json, {})),
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
            "criteria_digest": packet.criteria_digest,
            # The cockpit may submit this opaque digest for the exact packet
            # it displayed without receiving proposal/task source text.
            "packet_digest": _sha(packet.proposal_text + packet.task_text),
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
            "strategy_delta_id": packet.strategy_delta_id,
            "outcome": _safe_export_value(_load(packet.outcome_json, {})),
            "failure_code": packet.failure_code,
        }

    async def list_watches(
        self,
        *,
        owner_principal_id: str,
        owner_session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        async with db_engine.get_session() as db:
            predicates = [GuardianSourceWatch.owner_principal_id == owner_principal_id]
            if owner_session_id is not None:
                predicates.append(GuardianSourceWatch.owner_session_id == owner_session_id)
            rows = (
                await db.execute(
                    select(GuardianSourceWatch)
                    .where(*predicates)
                    .order_by(GuardianSourceWatch.updated_at.desc())
                )
            ).scalars().all()
        result = []
        for row in rows:
            item = await self.get_watch(
                row.id,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
            )
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
        reviewed_grant_id: str | None = None,
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
            active_job_id = _text(watch.active_job_id) or None
            active_job_fence = int(watch.active_job_fence or 0)
            # A pause or revoke is itself a cancellation boundary and must be
            # usable while an occurrence is scanning or awaiting approval.
            # Other edits still require an idle watch so a running job cannot
            # observe a half-rotated plan.
            state_only_stop = bool(
                active_job_id
                and state in {"paused", "revoked"}
                and sources is None
                and criteria is None
                and schedule is None
                and write_mode is None
                and reviewed_grant_id is None
            )
            if active_job_id and not state_only_stop:
                raise SourceWatchError("watch_active_job")
            goal = (
                await db.execute(select(Goal).where(Goal.id == watch.goal_id))
            ).scalars().first()
            if goal is None:
                raise SourceWatchError("goal_not_found")
            if (
                _text(goal.owner_principal_id) != _text(owner_principal_id)
                or _text(goal.owner_session_id) != _text(owner_session_id)
                or int(goal.revision or 0) != int(watch.goal_revision or 0)
            ):
                raise SourceWatchError("goal_binding_stale")
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
            goal_budget = deserialize_admission_budget(goal)
            changed_identity = False
            rotated_grant_id: str | None = None
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
                stale_baseline_keys = _stale_baseline_source_keys(old_sources, parsed_sources)
                changed_identity = any(
                    old_identities.get(item.source_key) != item.identity_digest
                    for item in parsed_sources
                ) or set(old_identities) != {item.source_key for item in parsed_sources}
                if changed_identity:
                    goal_budget = deserialize_admission_budget(goal)
                    existing_authority = _load(watch.read_authority_json, {})
                    existing_write_authority = _load(watch.write_authority_json, {})
                    existing_grants = {
                        grant
                        for grant in (
                            _text(existing_authority.get("grant_id")),
                            _text(existing_write_authority.get("grant_id")),
                            _text(goal_budget.grant_id) if goal_budget is not None else "",
                        )
                        if grant
                    }
                    rotated_grant_id = _text(reviewed_grant_id)
                    if (
                        goal_budget is None
                        or not bool(goal_budget.reviewed_grant)
                        or not _text(goal_budget.grant_id)
                        or not rotated_grant_id
                        or rotated_grant_id in existing_grants
                    ):
                        raise SourceWatchError("fresh_grant_required_for_source_identity")
                    # Source identity changes rotate the canonical reviewed
                    # grant and its goal revision in the same transaction as
                    # the watch plan.  The next occurrence therefore cannot
                    # reuse an old read/write authority or baseline generation.
                    rotated_budget = goal_budget.model_copy(update={"grant_id": rotated_grant_id})
                    next_goal_revision = int(goal.revision or 0) + 1
                    goal_update = await db.execute(
                        update(Goal)
                        .execution_options(synchronize_session=False)
                        .where(
                            Goal.id == goal.id,
                            Goal.revision == int(goal.revision or 0),
                        )
                        .values(
                            admission_budget_json=serialize_admission_budget(rotated_budget),
                            revision=next_goal_revision,
                            updated_at=_now(),
                        )
                    )
                    if getattr(goal_update, "rowcount", 0) != 1:
                        raise SourceWatchError("goal_binding_stale")
                    goal.admission_budget_json = serialize_admission_budget(rotated_budget)
                    goal.revision = next_goal_revision
                    goal.updated_at = _now()
                    goal_budget = rotated_budget
                    watch.goal_revision = next_goal_revision
                watch.sources_json = _dump(source_json)
                watch.source_set_digest = _sha(_dump(source_json))
                if changed_identity:
                    watch.read_authority_json = _dump(
                        {
                            "source_keys": [item.source_key for item in parsed_sources],
                            "grant_id": _text(reviewed_grant_id),
                        }
                    )
                    write_authority = _load(watch.write_authority_json, {})
                    write_authority["grant_id"] = rotated_grant_id
                    watch.write_authority_json = _dump(write_authority)
                if stale_baseline_keys:
                    # A removed or identity-rotated source may never carry its
                    # old baseline into a later re-add.  Delete the rows in
                    # the same plan transaction as the source-set revision.
                    await db.execute(
                        delete(GuardianSourceBaseline).where(
                            GuardianSourceBaseline.watch_id == watch.id,
                            GuardianSourceBaseline.source_key.in_(stale_baseline_keys),
                        )
                    )
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
            if write_mode == "standing_reviewed" or (
                write_mode is None and watch.write_mode == "standing_reviewed"
            ):
                goal_budget = goal_budget or deserialize_admission_budget(goal)
                if (
                    goal_budget is None
                    or not bool(goal_budget.reviewed_grant)
                    or not _text(goal_budget.grant_id)
                    or (
                        _text(reviewed_grant_id)
                        and _text(reviewed_grant_id) != _text(goal_budget.grant_id)
                    )
                ):
                    raise SourceWatchError("standing_grant_mismatch")
                if not _text(reviewed_grant_id):
                    current_grant = _text(_load(watch.write_authority_json, {}).get("grant_id"))
                    if not current_grant or current_grant != _text(goal_budget.grant_id):
                        raise SourceWatchError("standing_grant_required")
            if write_mode is not None:
                watch.write_mode = write_mode
            if state is not None:
                watch.state = state
            watch.plan_revision += 1
            watch.updated_at = _now()
            db.add(watch)
            await db.flush()
        result = await self.get_watch(
            watch_id,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        ) or {}
        cancellation: dict[str, Any] | None = None
        if state_only_stop and active_job_id:
            try:
                cancellation = await self.cancel_watch_job(
                    watch_id=watch_id,
                    job_id=active_job_id,
                    expected_plan_revision=expected_plan_revision,
                    expected_fencing_token=active_job_fence,
                    owner_principal_id=owner_principal_id,
                    owner_session_id=owner_session_id,
                )
            except SourceWatchError as exc:
                # The state transition is durable even if a worker won the
                # cancellation race.  Expose the recovery action instead of
                # pretending that the active occurrence was stopped.
                cancellation = {
                    "status": "blocked",
                    "job_id": active_job_id,
                    "reason_code": exc.code,
                    "operator_action": "recover_or_cancel",
                    "operator_visible": True,
                }
            result = await self.get_watch(
                watch_id,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
            ) or result
            result["cancellation"] = cancellation
        await _audit_watch_event(result, "updated", state=state, write_mode=write_mode)
        return result

    async def _claim_watch(
        self,
        watch_id: str,
        job_id: str,
        occurrence_id: str,
        *,
        expected_plan_revision: int,
    ) -> tuple[GuardianSourceWatch | None, str]:
        async with db_engine.get_session() as db:
            now = _now()
            result = await db.execute(
                update(GuardianSourceWatch)
                .where(
                    GuardianSourceWatch.id == watch_id,
                    GuardianSourceWatch.state == "active",
                    GuardianSourceWatch.active_job_id.is_(None),
                    GuardianSourceWatch.plan_revision == int(expected_plan_revision),
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
                current = (
                    await db.execute(select(GuardianSourceWatch).where(GuardianSourceWatch.id == watch_id))
                ).scalars().first()
                if current is None:
                    return None, "watch_not_found"
                if int(current.plan_revision or 0) != int(expected_plan_revision):
                    return None, "watch_plan_revision_stale"
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

    async def _scan(self, watch: GuardianSourceWatch, *, occurrence_id: str | None = None) -> ScanResult:
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
                        remaining = (deadline - _now()).total_seconds()
                        if remaining <= 0:
                            raise SourceWatchError("scan_deadline")
                        reader = (
                            self._fetcher(source)
                            if self._fetcher is not None
                            else _read_source(source)
                        )
                        raw, metadata = await asyncio.wait_for(
                            reader,
                            timeout=min(float(SOURCE_READ_DEADLINE_SECONDS), remaining),
                        )
                        break
                    except asyncio.TimeoutError as exc:
                        raise SourceWatchError("source_transport_timeout") from exc
                    except SourceWatchError as exc:
                        # Retry only transient transport failures and upstream
                        # 5xx responses.  SSRF/policy, content-type, size, and
                        # other contract errors are deterministic and must
                        # remain visible without multiplying the read.
                        retryable = exc.code == "source_transport_timeout" or exc.code.startswith("source_http_5")
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
        input_digest = _bind_occurrence_input_digest(input_digest, occurrence_id)
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
        base_input_digest = compute_input_digest(
            watch_id=watch.id,
            goal_revision=watch.goal_revision,
            plan_revision=watch.plan_revision,
            source_set_digest=watch.source_set_digest,
            criteria_digest=watch.criteria_digest,
            capability_version=watch.capability_version,
            observations=observations,
        )
        expected_input_digest = _bind_occurrence_input_digest(base_input_digest, packet.run_identity)
        # Packets written before occurrence binding used the observation
        # digest directly. Keep those receipts recoverable while every new
        # packet remains unique to its durable job identity.
        if packet.input_digest not in {expected_input_digest, base_input_digest}:
            raise SourceWatchError("observed_input_digest_mismatch")
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
        work_board_task_id: str | None = None,
        work_board_attempt_id: str | None = None,
    ) -> dict[str, Any]:
        if (work_board_task_id is None) != (work_board_attempt_id is None):
            raise SourceWatchError("work_board_binding_invalid")
        job_id = f"source-watch:{watch.id}:{occurrence_id}"
        async with db_engine.get_session() as db:
            live_watch = (
                await db.execute(select(GuardianSourceWatch).where(GuardianSourceWatch.id == watch.id))
            ).scalars().first()
            if live_watch is None or _text(live_watch.state) != "active":
                raise SourceWatchError("watch_not_active")
            goal = (await db.execute(select(Goal).where(Goal.id == watch.goal_id))).scalars().first()
        if goal is None:
            raise SourceWatchError("goal_not_found")
        if _text(getattr(getattr(goal, "status", ""), "value", getattr(goal, "status", ""))) != "active":
            raise SourceWatchError("goal_not_active")
        if not bool(getattr(goal, "proactive_enabled", False)):
            raise SourceWatchError("goal_proactive_disabled")
        if (
            _text(getattr(goal, "owner_principal_id", "")) != _text(watch.owner_principal_id)
            or _text(getattr(goal, "owner_session_id", "")) != _text(watch.owner_session_id)
            or int(getattr(goal, "revision", 0) or 0) != int(watch.goal_revision or 0)
        ):
            raise SourceWatchError("goal_binding_stale")
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
            "goal_id": watch.goal_id,
            "goal_revision": watch.goal_revision,
            "plan_revision": watch.plan_revision,
            "goal_owner_principal_id": watch.owner_principal_id,
            "goal_owner_session_id": watch.owner_session_id,
            "capability_id": CAPABILITY_ID,
            "permissions": ["source_observation", "workspace_write"],
            "budget_microusd": 0,
            "budget_grant_id": _text(getattr(budget, "grant_id", "")),
            "read_grant_id": _text(_load(watch.read_authority_json, {}).get("grant_id")),
            "source_set_digest": _text(watch.source_set_digest),
            "criteria_digest": _text(watch.criteria_digest),
            "quiet_hours": {
                "start": getattr(budget, "quiet_hours_start", None),
                "end": getattr(budget, "quiet_hours_end", None),
                "timezone": _text(getattr(budget, "timezone", "UTC")) or "UTC",
            },
            "interruption_cost": "none",
            "delivery_surface": "cockpit_approval_queue",
            "priority": priority,
        }
        idempotency_scope = "work-board-attempt" if work_board_task_id else "guardian-source-watch"
        idempotency_key = (
            f"{work_board_task_id}:{work_board_attempt_id}"
            if work_board_task_id
            else f"{watch.id}:{watch.plan_revision}:{occurrence_id}"
        )
        identity = DurableJobIdentity(
            job_id=job_id,
            owner_kind="service",
            owner_principal_id=SERVICE_PRINCIPAL,
            job_kind="guardian_source_watch",
            capability_version=CAPABILITY_VERSION,
            idempotency_scope=idempotency_scope,
            idempotency_key=idempotency_key,
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

    async def _resume_admitted_job(self, job: Mapping[str, Any]) -> dict[str, Any]:
        """Finish the accepted/queued half of an idempotent admission.

        Admission, queueing, and claiming are separate durable CAS writes.
        If the process dies between them, the next scheduler occurrence must
        adopt the same job instead of treating a deduped row as settled.
        """

        current = dict(job)
        job_id = _text(current.get("job_id"))
        if not job_id:
            return current
        status = _text(current.get("status"))
        if status == "accepted":
            current = await durable_job_repository.queue_job(
                job_id,
                expected_revision=current.get("revision"),
                expected_fencing_token=int(current.get("fencing_token") or 0),
            )
            status = _text(current.get("status"))
        if status == "queued":
            lease = current.get("lease") if isinstance(current.get("lease"), Mapping) else {}
            current = await durable_job_repository.claim_job(
                job_id,
                owner=SERVICE_PRINCIPAL,
                expected_revision=current.get("revision"),
                expected_fencing_token=lease.get("fencing_token", current.get("fencing_token")),
                lease_seconds=JOB_DEADLINE_SECONDS,
            )
        return current

    async def run_watch(
        self,
        watch_id: str,
        *,
        occurrence_id: str | None = None,
        expected_plan_revision: int | None = None,
        expected_scheduled_job_id: str | None = None,
        expected_owner_session_id: str | None = None,
        work_board_task_id: str | None = None,
        work_board_attempt_id: str | None = None,
        admit_only: bool = False,
    ) -> dict[str, Any]:
        if (work_board_task_id is None) != (work_board_attempt_id is None):
            return {"status": "blocked", "reason_code": "work_board_binding_invalid", "operator_visible": True}
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
        await _audit_watch_event(watch, "run_requested", occurrence_id=occurrence)
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
        if _text(watch.state) != "active":
            watch_released = False
            if _text(watch.active_job_id) == _text(job_id):
                watch_released = await self._release_watch(
                    watch.id,
                    job_id,
                    int(watch.active_job_fence or 0),
                    "deferred" if _text(watch.state) == "paused" else "blocked",
                    "watch_paused" if _text(watch.state) == "paused" else "watch_not_active",
                )
            return {
                "status": "deferred" if _text(watch.state) == "paused" else "blocked",
                "reason_code": "watch_paused" if _text(watch.state) == "paused" else "watch_not_active",
                "watch_released": watch_released,
                "operator_visible": True,
                "watch_id": watch.id,
            }
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
        expected_claim_plan_revision = int(watch.plan_revision or 0)
        try:
            job = await self._admit_job(
                watch,
                occurrence,
                budget=budget,
                work_board_task_id=work_board_task_id,
                work_board_attempt_id=work_board_attempt_id,
            )
            job = await self._resume_admitted_job(job)
            if admit_only:
                # Board dispatch uses this server-only phase to persist the
                # durable source-watch root and link it to the pending board
                # attempt before any source transport or watch fence effect.
                # A later call with the same exact binding resumes this job
                # through the normal execution path.
                return {
                    "status": _text(job.get("status")) or "blocked",
                    "reason_code": None,
                    "job_id": job_id,
                    "job": job,
                    "admission_only": True,
                }
            if job.get("status") != "running":
                receipt = job.get("receipt") if isinstance(job.get("receipt"), Mapping) else {}
                if _text(receipt.get("status")) == "deduped":
                    return {
                        "status": _text(job.get("status")) or "blocked",
                        "reason_code": "occurrence_already_settled",
                        "job_id": job_id,
                        "job": job,
                        "operator_visible": True,
                    }
                return {"status": "blocked", "reason_code": "durable_job_not_running", "job": job}
            reserved_watch, claim_status = await self._claim_watch(
                watch_id,
                job_id,
                occurrence,
                expected_plan_revision=expected_claim_plan_revision,
            )
            if reserved_watch is None:
                admission_receipt = job.get("receipt") if isinstance(job.get("receipt"), Mapping) else {}
                async with db_engine.get_session() as db:
                    current_watch = (
                        await db.execute(select(GuardianSourceWatch).where(GuardianSourceWatch.id == watch_id))
                    ).scalars().first()
                if (
                    _text(admission_receipt.get("status")) == "deduped"
                    and current_watch is not None
                    and _text(current_watch.active_job_id) == job_id
                ):
                    # The duplicate occurrence is observing the winner's
                    # durable fence. It must not settle the shared job.
                    return {
                        "status": "deduped",
                        "reason_code": "occurrence_already_active",
                        "job_id": job_id,
                        "operator_visible": True,
                    }
                await self._settle_observation_job(job_id, status="blocked", reason=claim_status)
                return {"status": "blocked", "reason_code": claim_status, "job_id": job_id}
            watch = reserved_watch
            fence = int(watch.active_job_fence)
            # The first admission check happens before the watch fence is
            # claimed. Re-read the canonical goal after the claim and before
            # any source transport so a pause, revision, quiet-hours change,
            # or grant revocation cannot race into source I/O.
            async with db_engine.get_session() as db:
                live_watch = (
                    await db.execute(select(GuardianSourceWatch).where(GuardianSourceWatch.id == watch.id))
                ).scalars().first()
                live_goal = (
                    await db.execute(select(Goal).where(Goal.id == watch.goal_id))
                ).scalars().first()
            live_reason: str | None = None
            live_budget = None
            if live_watch is None:
                live_reason = "watch_not_found"
            elif int(live_watch.plan_revision or 0) != int(watch.plan_revision or 0):
                live_reason = "watch_plan_revision_stale"
            elif live_goal is None:
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
            try:
                scan = await asyncio.wait_for(
                    self._scan(watch, occurrence_id=job_id),
                    timeout=float(SCAN_DEADLINE_SECONDS),
                )
            except asyncio.TimeoutError as exc:
                raise SourceWatchError("scan_deadline") from exc
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
                await _audit_watch_event(
                    watch,
                    "blocked",
                    job_id=job_id,
                    reason_code="source_unavailable",
                )
                return {"status": "blocked", "reason_code": "source_unavailable", "job_id": job_id}
            baseline_only, baseline_status, action_scan = _partition_baseline_observations(
                scan,
                watch_id=watch.id,
                goal_revision=int(watch.goal_revision or 0),
                plan_revision=int(watch.plan_revision or 0),
                source_set_digest=_text(watch.source_set_digest),
                criteria_digest=_text(watch.criteria_digest),
                capability_version=watch.capability_version,
            )
            if baseline_only:
                # Advance only the first-seen/rebaselined rows now.  The
                # remaining observed rows stay in the action projection and
                # are committed by the no-change or packet finalization path.
                baseline_scan = replace(scan, baseline_updates=baseline_only)
                await self._commit_baselines(
                    watch,
                    baseline_scan,
                    status=baseline_status or "baseline_initialized",
                    job_id=job_id,
                    fencing_token=durable_fence,
                )
            if baseline_only and not action_scan.observations:
                await self._settle_observation_job(
                    job_id,
                    status="succeeded",
                    reason=baseline_status or "baseline_initialized",
                )
                if not await self._release_watch(
                    watch.id,
                    job_id,
                    fence,
                    baseline_status or "baseline_initialized",
                ):
                    raise SourceWatchError("active_job_release_conflict")
                await _audit_watch_event(
                    watch,
                    baseline_status or "baseline_initialized",
                    job_id=job_id,
                    observed_sources=len(baseline_only),
                    packet_id=None,
                )
                return {"status": baseline_status or "baseline_initialized", "job_id": job_id}
            if not action_scan.material:
                baseline_status = (
                    "degraded" if action_scan.degraded else "no_change"
                )
                # Advance the canonical local baseline before recording the
                # no-change receipt.  If the process dies between these two
                # local writes, the next occurrence observes the new hash and
                # produces the same no-change outcome instead of blocking on
                # an unverifiable recovery packet.
                await self._commit_baselines(
                    watch,
                    action_scan,
                    status=baseline_status,
                    job_id=job_id,
                    fencing_token=durable_fence,
                )
                packet = await self._create_no_change_packet(
                    watch,
                    job_id,
                    action_scan,
                    reason_code=baseline_status,
                )
                await self._settle_observation_job(
                    job_id,
                    status="succeeded",
                    reason=baseline_status,
                    packet_id=packet.id,
                )
                await self._release_watch(
                    watch.id,
                    job_id,
                    fence,
                    baseline_status,
                )
                await _audit_watch_event(
                    watch,
                    "no_change",
                    job_id=job_id,
                    packet_id=packet.id,
                    reason_code=baseline_status,
                )
                return {"status": baseline_status, "packet_id": packet.id, "job_id": job_id}
            packet = await self._create_packet(watch, job_id, action_scan)
            if watch.write_mode == "approval_each_run":
                approval_id = await self._hold_for_approval(
                    watch,
                    packet,
                    await durable_job_repository.get_job(job_id) or current,
                )
                await _audit_watch_event(
                    watch,
                    "awaiting_approval",
                    job_id=job_id,
                    packet_id=packet.id,
                    approval_id=approval_id,
                )
                return {
                    "status": "awaiting_approval",
                    "packet_id": packet.id,
                    "approval_id": approval_id,
                    "job_id": job_id,
                }
            execution_receipt = await self._execute_packet(watch, packet, job_id, scan=action_scan)
            final_status = "degraded" if action_scan.degraded else "succeeded"
            if not await self._release_watch(watch.id, job_id, fence, final_status):
                raise SourceWatchError("active_job_release_conflict")
            return {
                "status": final_status,
                "packet_id": packet.id,
                "job_id": job_id,
                **execution_receipt,
            }
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
            try:
                await _audit_watch_event(
                    watch,
                    "failed",
                    job_id=job_id,
                    packet_id=packet.id if packet is not None else None,
                    reason_code=type(exc).__name__,
                )
            except Exception:
                pass
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
            plan_matches = int(watch.plan_revision or 0) == int(expected_plan_revision)
            stopped_after_revision = (
                _text(watch.state) in {"paused", "revoked"}
                and int(watch.plan_revision or 0) == int(expected_plan_revision) + 1
            )
            if not plan_matches and not stopped_after_revision:
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
            or int(job.get("plan_revision") or 0)
            not in {
                int(watch.plan_revision or 0),
                int(expected_plan_revision),
            }
            or str(authority.get("session_id") or "") != str(owner_session_id)
        ):
            raise SourceWatchError("durable_job_binding_mismatch")
        if job.get("status") in {"succeeded", "degraded", "cancelled"}:
            raise SourceWatchError("job_terminal")
        if job.get("status") in {"unknown_external_effect", "cost_liability"}:
            raise SourceWatchError("cancel_requires_reconciliation")

        watch_fence = int(watch.active_job_fence or 0)
        lease = job.get("lease") if isinstance(job.get("lease"), Mapping) else {}
        durable_fence = int(lease.get("fencing_token") or job.get("fencing_token") or 0)
        if durable_fence < 1:
            raise SourceWatchError("durable_job_fence_missing")
        lease_owner = _text(lease.get("owner"))
        expected_revision = int(job.get("revision") or 0)
        if job.get("status") == "running":
            if lease_owner != SERVICE_PRINCIPAL:
                raise SourceWatchError("cancel_job_owner_mismatch")
            job = await self._retain_unverified_workspace_artifacts(job)
            lease = job.get("lease") if isinstance(job.get("lease"), Mapping) else lease
            expected_revision = int(job.get("revision") or expected_revision)
            durable_fence = int((lease or {}).get("fencing_token") or durable_fence)
            cancelled = await durable_job_repository.transition_job(
                job_id,
                "cancelled",
                owner=SERVICE_PRINCIPAL,
                fencing_token=durable_fence,
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
                expected_fencing_token=durable_fence,
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
        result = {
            "status": "cancelled",
            "job_id": job_id,
            "watch_id": watch.id,
            # Watch reservation and durable lease fences are independent
            # domains. Keep both in the receipt instead of comparing them.
            "fencing_token": watch_fence,
            "watch_fencing_token": watch_fence,
            "durable_fencing_token": durable_fence,
            "learning": NO_LEARNING,
            "memory_status": NO_LEARNING,
            "operator_visible": True,
        }
        await _audit_watch_event(
            watch,
            "cancelled",
            job_id=job_id,
            watch_fencing_token=watch_fence,
            durable_fencing_token=durable_fence,
        )
        return result

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

    async def _settle_preclaim_packet_failure(
        self,
        watch: GuardianSourceWatch,
        packet: GuardianDecisionPacket,
        job: Mapping[str, Any],
        *,
        watch_fence: int,
        reason_code: str,
    ) -> dict[str, Any]:
        """Block an approval packet that failed before durable execution claim."""

        job_id = _text(packet.run_identity)
        reason = _text(reason_code)[:120] or "approval_packet_validation_failed"
        settled: Mapping[str, Any] = {}
        transition_failed = False
        try:
            settled = await durable_job_repository.transition_job(
                job_id,
                "blocked",
                expected_state="awaiting_approval",
                expected_revision=job.get("revision"),
                reason=reason,
                result=_no_learning_result("blocked", reason),
                result_summary="source-watch approval packet blocked before execution",
            )
        except Exception:
            transition_failed = True
            try:
                settled = await durable_job_repository.get_job(job_id) or {}
            except Exception:
                settled = {}
        durable_status = _text(settled.get("status")) or "unknown"
        settled_statuses = {
            "blocked",
            "failed",
            "cancelled",
            "unknown_external_effect",
            "cost_liability",
        }
        if transition_failed and durable_status not in settled_statuses:
            # Do not mark the packet or clear the watch while the durable
            # approval-held row is still live. The operator can retry this
            # exact bounded settlement after the storage/CAS fault clears.
            await _audit_watch_event(
                watch,
                "blocked",
                job_id=job_id,
                packet_id=packet.id,
                reason_code="approval_settlement_required",
                watch_released=False,
            )
            return {
                **_no_learning_result("blocked", "approval_settlement_required"),
                "job_id": job_id,
                "packet_id": packet.id,
                "durable_status": durable_status,
                "watch_released": False,
            }
        watch_released = False
        if durable_status in settled_statuses:
            try:
                await self._mark_packet_failure(packet.id, reason)
            except Exception:
                pass
            watch_released = await self._release_watch(
                watch.id,
                job_id,
                watch_fence,
                "blocked",
                reason,
            )
        await _audit_watch_event(
            watch,
            "blocked",
            job_id=job_id,
            packet_id=packet.id,
            reason_code=reason,
            watch_released=watch_released,
        )
        return {
            **_no_learning_result("blocked", reason),
            "job_id": job_id,
            "packet_id": packet.id,
            "durable_status": durable_status,
            "watch_released": watch_released,
        }

    async def _create_packet(self, watch: GuardianSourceWatch, job_id: str, scan: ScanResult) -> GuardianDecisionPacket:
        async with db_engine.get_session() as db:
            goal = (await db.execute(select(Goal).where(Goal.id == watch.goal_id))).scalars().first()
        if goal is None:
            raise SourceWatchError("goal_not_found")
        if not bool(getattr(goal, "proactive_enabled", False)):
            raise SourceWatchError("goal_proactive_disabled")
        # A mixed scan may have a reduced action projection but its checkpoint
        # must retain every observed source so recovery recomputes the same
        # occurrence-bound input digest.
        checkpoint = dict(scan.checkpoint)
        checkpoint_sha256 = scan.checkpoint_sha256
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
            checkpoint_sha256=checkpoint_sha256,
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
            source_observation_json=_dump(checkpoint),
            material_source_keys_json=_dump([item.source.source_key for item in scan.material]),
            proposal_text=dossier,
            task_text=task,
            status="prepared",
            observed_checkpoint_json=_dump(checkpoint),
            observed_checkpoint_sha256=checkpoint_sha256,
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

    async def _create_no_change_packet(
        self,
        watch: GuardianSourceWatch,
        job_id: str,
        scan: ScanResult,
        *,
        reason_code: str,
    ) -> GuardianDecisionPacket:
        """Persist the bounded observation receipt even when no action is due."""

        checkpoint = dict(scan.checkpoint)
        checkpoint_sha256 = scan.checkpoint_sha256
        redaction_count = sum(item.redactions for item in scan.observations)
        redaction = {"redacted": redaction_count > 0, "replacement_count": redaction_count}
        outcome = {
            "status": "no_change",
            "reason_code": _text(reason_code)[:120] or "no_change",
            "learning": NO_LEARNING,
            "memory_status": NO_LEARNING,
            "operator_visible": True,
        }
        packet = GuardianDecisionPacket(
            id=str(uuid.uuid4()),
            source_watch_id=watch.id,
            watch_id=watch.id,
            goal_id=watch.goal_id,
            goal_revision=watch.goal_revision,
            plan_revision=watch.plan_revision,
            run_identity=job_id,
            input_digest=scan.input_digest,
            criteria_digest=watch.criteria_digest,
            source_observation_json=_dump(checkpoint),
            material_source_keys_json="[]",
            proposal_text="",
            task_text="",
            status="no_change",
            verification_status="not_applicable",
            memory_status=NO_LEARNING,
            observed_checkpoint_json=_dump(checkpoint),
            observed_checkpoint_sha256=checkpoint_sha256,
            redaction_manifest_json=_dump(redaction),
            outcome_json=_dump(outcome),
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
            if (
                _text(watch.state) != "active"
                or _text(watch.active_job_id) != _text(run_identity)
            ):
                raise SourceWatchError("watch_lease_stale")
            watch_fence = int(watch.active_job_fence or 0)
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
        try:
            # Validate and rehydrate the immutable observation while the job is
            # still approval-held.  A digest mismatch or source reread failure
            # must not strand a newly claimed execution lease.
            scan = self._scan_from_packet(watch, packet)
            if scan.input_digest != packet.input_digest:
                raise SourceWatchError("packet_observation_stale")
            scan = await self._rehydrate_recovery_baselines(watch, packet, scan)
        except SourceWatchError as exc:
            return await self._settle_preclaim_packet_failure(
                watch,
                packet,
                current,
                watch_fence=watch_fence,
                reason_code=exc.code,
            )
        except Exception as exc:
            return await self._settle_preclaim_packet_failure(
                watch,
                packet,
                current,
                watch_fence=watch_fence,
                reason_code=type(exc).__name__,
            )
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
        execution_receipt = await self._execute_packet(
            watch,
            packet,
            run_identity,
            claimed=claimed,
            scan=scan,
        )
        final_status = "degraded" if scan.degraded else "succeeded"
        if not await self._release_watch(watch_id, run_identity, watch_fence, final_status):
            raise SourceWatchError("active_job_release_conflict")
        result = await self.get_watch(
            watch_id,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )
        if execution_receipt and result is not None:
            result["notification_id"] = execution_receipt.get("notification_id")
        await _audit_watch_event(
            watch,
            "executed",
            job_id=run_identity,
            packet_id=packet.id,
            notification_id=execution_receipt.get("notification_id"),
            watch_fencing_token=watch_fence,
            durable_fencing_token=int((claimed.get("lease") or {}).get("fencing_token") or 0),
        )
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
        if not path_set:
            return False
        verified_paths = {
            _text(item.get("target_path"))
            for item in (job.get("effects") or [])
            if isinstance(item, Mapping)
            and _text(item.get("receipt_kind")) == "readback"
            and _text(item.get("status")) == "succeeded"
            and _text(item.get("target_path")) in path_set
            and isinstance(item.get("details"), Mapping)
            and item["details"].get("verified") is True
        }
        return path_set.issubset(verified_paths)

    async def _repair_recovery_baselines(
        self,
        watch: GuardianSourceWatch,
        packet: GuardianDecisionPacket,
        scan: ScanResult,
        *,
        job: Mapping[str, Any] | None = None,
    ) -> None:
        """Accept existing canonical baselines or repair legacy local input."""

        rehydrated = await self._rehydrate_recovery_baselines(watch, packet, scan)
        if not rehydrated.baseline_updates:
            return
        lease = job.get("lease") if isinstance(job, Mapping) and isinstance(job.get("lease"), Mapping) else {}
        await self._commit_baselines(
            watch,
            rehydrated,
            status="recovered",
            job_id=packet.run_identity,
            fencing_token=int(lease.get("fencing_token") or 0) or None,
        )

    async def _rehydrate_recovery_baselines(
        self,
        watch: GuardianSourceWatch,
        packet: GuardianDecisionPacket,
        scan: ScanResult,
    ) -> ScanResult:
        """Recover baseline bytes without persisting source text in packets.

        New checkpoints carry hashes and bounded metadata only.  When the
        canonical baseline row is not already at the approved hash, read the
        exact source again through the same policy and accept it only when its
        normalized digest still matches the immutable observation.  Callers
        that are finalizing a packet can then apply the returned update in the
        same transaction as the terminal packet state.
        """

        legacy_updates = tuple(item for item in scan.baseline_updates if item.baseline_text is not None)
        if legacy_updates:
            return scan
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
        refresh: list[SourceObservation] = []
        observations_by_key = {item.source.source_key: item for item in scan.observations}
        for source_key, (identity_digest, new_hash) in expected.items():
            row = actual.get(source_key)
            if (
                row is None
                or _text(row.identity_digest) != identity_digest
                or _text(row.baseline_sha256) != new_hash
            ):
                # A missing canonical row has no trusted prior generation to
                # reconcile.  Keep that case blocked; a digest-bound reread is
                # only safe when an owner-bound baseline row already exists.
                if row is None:
                    raise SourceWatchError("recovery_baseline_unverified")
                observed = observations_by_key.get(source_key)
                if observed is None or not observed.new_hash:
                    raise SourceWatchError("recovery_baseline_unverified")
                try:
                    reader = self._fetcher(observed.source) if self._fetcher is not None else _read_source(observed.source)
                    raw, metadata = await asyncio.wait_for(
                        reader,
                        timeout=SOURCE_READ_DEADLINE_SECONDS,
                    )
                    normalized = normalize_source_text(
                        raw,
                        html_content=metadata.get("content_type") in {"text/html", "application/xhtml+xml"},
                    )
                except (asyncio.TimeoutError, SourceWatchError, OSError) as exc:
                    raise SourceWatchError("recovery_baseline_unreadable") from exc
                if _sha(normalized) != new_hash:
                    raise SourceWatchError("recovery_baseline_changed")
                refresh.append(
                    replace(
                        observed,
                        baseline_text=normalized,
                        etag=metadata.get("etag"),
                        last_modified=metadata.get("last-modified"),
                    )
                )
        return replace(scan, baseline_updates=tuple(refresh)) if refresh else scan

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

    async def _reacquire_recovery_lease(
        self,
        job: Mapping[str, Any],
    ) -> tuple[dict[str, Any], str | None]:
        """Reclaim a stale blocked occurrence before reading its outputs."""

        job_id = _text(job.get("job_id"))
        if not job_id or _text(job.get("status")) != "blocked":
            return dict(job), None
        try:
            queued = await durable_job_repository.queue_job(
                job_id,
                expected_revision=int(job.get("revision") or 0),
                expected_fencing_token=int((job.get("lease") or {}).get("fencing_token") or 0),
            )
            queued_lease = queued.get("lease") if isinstance(queued.get("lease"), Mapping) else {}
            claimed = await durable_job_repository.claim_job(
                job_id,
                owner=SERVICE_PRINCIPAL,
                expected_revision=queued.get("revision"),
                expected_fencing_token=queued_lease.get("fencing_token"),
                lease_seconds=JOB_DEADLINE_SECONDS,
            )
            return claimed, None
        except Exception:
            # Queueing is a separate durable transition.  If claiming fails
            # because the bounded attempt budget or a fence changed, settle
            # the queued row back to an explicit operator-visible block.
            current = await durable_job_repository.get_job(job_id)
            if current is not None and _text(current.get("status")) == "queued":
                try:
                    await durable_job_repository.transition_job(
                        job_id,
                        "blocked",
                        expected_revision=current.get("revision"),
                        expected_fencing_token=int(
                            (current.get("lease") or {}).get("fencing_token") or 0
                        ),
                        reason="recovery_lease_reacquire_unavailable",
                        result=_no_learning_result(
                            "blocked",
                            "recovery_lease_reacquire_unavailable",
                        ),
                        result_summary="source-watch recovery could not reacquire its durable lease",
                    )
                except Exception:
                    pass
            return current or dict(job), "recovery_lease_reacquire_unavailable"

    async def _release_recovery_lease(
        self,
        job: Mapping[str, Any],
        *,
        reason: str,
        result: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Drop a recovery lease through the same owner/fence CAS."""

        if _text(job.get("status")) != "running":
            return dict(job)
        lease = job.get("lease") if isinstance(job.get("lease"), Mapping) else {}
        try:
            return await durable_job_repository.transition_job(
                _text(job.get("job_id")),
                "blocked",
                owner=_text(lease.get("owner")) or SERVICE_PRINCIPAL,
                fencing_token=int(lease.get("fencing_token") or 0),
                expected_revision=int(job.get("revision") or 0),
                reason=reason,
                result=result or _no_learning_result("blocked", reason),
                result_summary="source-watch recovery remains blocked pending operator reconciliation",
            )
        except Exception:
            return await durable_job_repository.get_job(_text(job.get("job_id"))) or dict(job)

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
                        GuardianSourceWatch.owner_session_id == owner_session_id,
                    )
                )
            ).scalars().first()
            if watch is None:
                raise SourceWatchError("watch_not_found")
            if int(watch.plan_revision) != int(expected_plan_revision):
                raise SourceWatchError("watch_plan_revision_stale")
            packet = (
                await db.execute(
                    select(GuardianDecisionPacket).where(
                        GuardianDecisionPacket.watch_id == watch_id,
                        GuardianDecisionPacket.run_identity == job_id,
                    )
                )
                ).scalars().first()
        await _audit_watch_event(watch, "recovery_requested", job_id=job_id)
        job = await durable_job_repository.get_job(job_id)
        if job is None:
            raise SourceWatchError("durable_job_missing")
        binding_error = _recovery_binding_error(
            watch_id=watch.id,
            goal_id=watch.goal_id,
            goal_revision=int(watch.goal_revision or 0),
            plan_revision=int(watch.plan_revision or 0),
            source_set_digest=_text(watch.source_set_digest),
            criteria_digest=_text(watch.criteria_digest),
            sources_json=watch.sources_json,
            job_id=job_id,
            job=job,
            packet=packet,
        )
        if binding_error is not None:
            raise SourceWatchError(binding_error)
        if _text(job.get("status")) == "failed":
            # Failed source-watch rows are terminal execution failures, but
            # the durable runtime keeps them outstanding until an explicit
            # terminal cancellation.  Settle that receipt without replaying
            # source I/O, then release any matching watch fence.
            try:
                settled = await durable_job_repository.cancel_job(
                    job_id,
                    expected_revision=job.get("revision"),
                    reason="failed_occurrence_reconciled",
                )
            except Exception as exc:
                watch_released = False
                if _text(watch.active_job_id) == _text(job_id):
                    try:
                        watch_released = await self._release_watch(
                            watch.id,
                            job_id,
                            int(watch.active_job_fence or 0),
                            "blocked",
                            "failed_occurrence_reconciliation_required",
                        )
                    except Exception:
                        watch_released = False
                return {
                    "status": "blocked",
                    "job_id": job_id,
                    "packet_id": packet.id if packet is not None else None,
                    "reason_code": "failed_occurrence_reconciliation_required",
                    "recovery_error": type(exc).__name__,
                    "watch_released": watch_released,
                    "operator_visible": True,
                }
            projection = _failed_recovery_projection(_text(settled.get("status")))
            durable_status = projection["durable_status"]
            reason_code = projection["reason_code"]
            if packet is not None:
                try:
                    await self._mark_packet_failure(packet.id, reason_code)
                except Exception:
                    pass
            watch_released = False
            if _text(watch.active_job_id) == _text(job_id):
                watch_released = await self._release_watch(
                    watch.id,
                    job_id,
                    int(watch.active_job_fence or 0),
                    projection["watch_status"],
                    reason_code,
                )
            return {
                "status": projection["status"],
                "job_id": job_id,
                "packet_id": packet.id if packet is not None else None,
                "recovery": projection["recovery"],
                "reason_code": reason_code,
                "watch_released": watch_released,
                "learning": NO_LEARNING,
                "operator_visible": True,
                "durable_status": durable_status,
            }
        recovery_lease_acquired = False
        # Admission, queueing, and watch reservation are independent writes.
        # A restart can leave an accepted/queued occurrence with no packet;
        # adopt that same durable identity long enough to settle it and clear
        # any matching watch fence.  Recovery never starts a fresh scan.
        if packet is None and _text(job.get("status")) in {"accepted", "queued"}:
            try:
                job = await self._resume_admitted_job(job)
                if _text(job.get("status")) == "running":
                    job = await self._release_recovery_lease(
                        job,
                        reason="recovery_packet_missing",
                        result=_no_learning_result("blocked", "recovery_packet_missing"),
                    )
            except Exception:
                job = await durable_job_repository.get_job(job_id) or job
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
            if _text(job.get("status")) != "blocked":
                return {
                    "status": "blocked",
                    "job_id": job_id,
                    "reason_code": "recovery_retry_unavailable",
                    "recovery": "stale_lease_settled_without_reclaim",
                    "operator_visible": True,
                }
            job, recovery_error = await self._reacquire_recovery_lease(job)
            if recovery_error is not None:
                return {
                    "status": "blocked",
                    "job_id": job_id,
                    "reason_code": recovery_error,
                    "recovery": "lease_reacquire_failed",
                    "operator_visible": True,
                }
            recovery_lease_acquired = True
        elif _text(job.get("status")) == "blocked" and packet is not None:
            # A previous recovery may have left the occurrence blocked.  Any
            # new packet/artifact inspection still needs a fresh durable fence.
            job, recovery_error = await self._reacquire_recovery_lease(job)
            if recovery_error is not None:
                return {
                    "status": "blocked",
                    "job_id": job_id,
                    "packet_id": packet.id,
                    "reason_code": recovery_error,
                    "recovery": "lease_reacquire_failed",
                    "operator_visible": True,
                }
            recovery_lease_acquired = _text(job.get("status")) == "running"
        if packet is None:
            if recovery_lease_acquired:
                job = await self._release_recovery_lease(
                    job,
                    reason="recovery_packet_missing",
                    result=_no_learning_result("blocked", "recovery_packet_missing"),
                )
            watch_released = False
            if _text(watch.active_job_id) == _text(job_id):
                watch_released = await self._release_watch(
                    watch.id,
                    job_id,
                    int(watch.active_job_fence or 0),
                    "blocked",
                    "recovery_packet_missing",
                )
            return {
                "status": "blocked",
                "job_id": job_id,
                "reason_code": "recovery_packet_missing",
                "recovery": "no_packet",
                "watch_released": watch_released,
                "operator_visible": True,
            }
        if _text(watch.state) != "active":
            watch_released = False
            if recovery_lease_acquired:
                job = await self._release_recovery_lease(
                    job,
                    reason="watch_paused" if _text(watch.state) == "paused" else "watch_not_active",
                    result=_no_learning_result(
                        "deferred" if _text(watch.state) == "paused" else "blocked",
                        "watch_paused" if _text(watch.state) == "paused" else "watch_not_active",
                    ),
                )
            if _text(watch.active_job_id) == _text(job_id):
                watch_released = await self._release_watch(
                    watch.id,
                    job_id,
                    int(watch.active_job_fence or 0),
                    "deferred" if _text(watch.state) == "paused" else "blocked",
                    "watch_paused" if _text(watch.state) == "paused" else "watch_not_active",
                )
            return {
                "status": "deferred" if _text(watch.state) == "paused" else "blocked",
                "reason_code": "watch_paused" if _text(watch.state) == "paused" else "watch_not_active",
                "watch_released": watch_released,
                "operator_visible": True,
                "watch_id": watch.id,
            }
        try:
            # Rehydrate the immutable checkpoint before looking at any output.
            # This checks the current source identity set and recomputes the
            # input digest against the current watch plan.
            recovery_scan = self._scan_from_packet(watch, packet)
        except SourceWatchError as exc:
            if recovery_lease_acquired:
                await self._release_recovery_lease(job, reason=exc.code)
            return {
                "status": "blocked",
                "job_id": job_id,
                "packet_id": packet.id,
                "reason_code": exc.code,
                "recovery": "packet_validation_failed",
                "operator_visible": True,
            }

        # Packet finalization and baseline advancement are separate durable
        # records.  A restart in that gap must verify or repair the canonical
        # baseline before this route can declare the packet successful.
        if packet.status == "no_change" or (
            packet.status in {"succeeded", "degraded"}
            and packet.verification_status == "passed"
        ):
            try:
                await self._repair_recovery_baselines(watch, packet, recovery_scan, job=job)
            except SourceWatchError as exc:
                if recovery_lease_acquired:
                    await self._release_recovery_lease(job, reason=exc.code)
                return {
                    "status": "blocked",
                    "job_id": job_id,
                    "packet_id": packet.id,
                    "reason_code": exc.code,
                    "recovery": "baseline_validation_failed",
                    "operator_visible": True,
                }
            except Exception:
                if recovery_lease_acquired:
                    await self._release_recovery_lease(
                        job,
                        reason="recovery_baseline_reconciliation_required",
                    )
                return {
                    "status": "blocked",
                    "job_id": job_id,
                    "packet_id": packet.id,
                    "reason_code": "recovery_baseline_reconciliation_required",
                    "recovery": "baseline_validation_failed",
                    "operator_visible": True,
                }

        if packet.status == "no_change":
            # A no-change packet has no workspace outputs.  Reuse the durable
            # source-observation readback when present; otherwise settle it
            # under the newly reacquired lease exactly once.
            no_change_reason = _text(_load(packet.outcome_json, {}).get("reason_code")) or "no_change"
            source_target = f"source-watch:{job_id}"
            try:
                if recovery_lease_acquired:
                    current = await durable_job_repository.get_job(job_id) or job
                    if not self._has_verified_output_effects(current, [source_target]):
                        await self._settle_observation_job(
                            job_id,
                            status="succeeded",
                            reason=no_change_reason,
                            packet_id=packet.id,
                        )
                    job = await durable_job_repository.get_job(job_id) or current
                if _text(job.get("status")) in {"succeeded", "degraded"}:
                    await self._release_watch(
                        watch_id,
                        job_id,
                        int(watch.active_job_fence),
                        "no_change",
                    )
                    return {
                        "status": "no_change",
                        "job_id": job_id,
                        "packet_id": packet.id,
                        "recovery": "verified_and_reconciled",
                        "learning": NO_LEARNING,
                        "memory_status": NO_LEARNING,
                    }
            except Exception:
                if recovery_lease_acquired:
                    current = await durable_job_repository.get_job(job_id) or job
                    await self._release_recovery_lease(
                        current,
                        reason="recovery_no_change_reconciliation_required",
                    )
            return {
                "status": "blocked",
                "job_id": job_id,
                "packet_id": packet.id,
                "reason_code": "recovery_no_change_reconciliation_required",
                "operator_visible": True,
            }

        if packet.status not in {"succeeded", "degraded"}:
            retained: Any = []
            try:
                if recovery_lease_acquired:
                    current = await durable_job_repository.get_job(job_id) or job
                    current = await self._retain_unverified_workspace_artifacts(current)
                    retained = current.get("artifacts") if isinstance(current.get("artifacts"), list) else []
                    job = await self._release_recovery_lease(
                        current,
                        reason="unverified_artifacts_retained" if retained else "recovery_reconciliation_required",
                    )
                else:
                    retained = await self._retain_recovery_unverified_artifacts(watch, packet, job)
            except Exception:
                if recovery_lease_acquired:
                    current = await durable_job_repository.get_job(job_id) or job
                    job = await self._release_recovery_lease(
                        current,
                        reason="recovery_reconciliation_required",
                    )
            if retained:
                await self._mark_packet_failure(packet.id, "unverified_artifacts_retained")
                return {
                    "status": "blocked",
                    "job_id": job_id,
                    "packet_id": packet.id,
                    "reason_code": "unverified_artifacts_retained",
                    "artifacts": retained,
                    "learning": NO_LEARNING,
                    "memory_status": NO_LEARNING,
                    "operator_visible": True,
                }

        if packet.status in {"succeeded", "degraded"} and packet.verification_status == "passed":
            # Only the canonical paths generated for this watch and packet may
            # be read during recovery.  The bounded no-follow reader then
            # rejects symlink and workspace-escape attempts before ingestion.
            expected_dossier, expected_task = self._packet_output_paths(watch.id, packet.id)
            if packet.dossier_path != expected_dossier or packet.task_path != expected_task:
                if recovery_lease_acquired:
                    await self._release_recovery_lease(job, reason="recovery_artifact_path_mismatch")
                return {
                    "status": "blocked",
                    "job_id": job_id,
                    "packet_id": packet.id,
                    "reason_code": "recovery_artifact_path_mismatch",
                    "operator_visible": True,
                }
            try:
                records = await self._read_verified_packet_outputs(watch, packet)
            except SourceWatchError as exc:
                if recovery_lease_acquired:
                    await self._release_recovery_lease(job, reason=exc.code)
                return {
                    "status": "blocked",
                    "job_id": job_id,
                    "packet_id": packet.id,
                    "reason_code": exc.code,
                    "operator_visible": True,
                }
            artifact_paths = [expected_dossier, expected_task]
            if recovery_lease_acquired:
                try:
                    current = await durable_job_repository.get_job(job_id) or job
                    lease = current.get("lease") if isinstance(current.get("lease"), Mapping) else {}
                    if not self._has_verified_output_effects(current, artifact_paths):
                        await self._repair_recovery_receipts(
                            job_id=job_id,
                            watch=watch,
                            packet=packet,
                            records=records,
                            owner=_text(lease.get("owner")) or SERVICE_PRINCIPAL,
                            fencing_token=int(lease.get("fencing_token") or 0),
                            revision=int(current.get("revision") or 0),
                        )
                    current = await durable_job_repository.get_job(job_id) or current
                    job = await self._release_recovery_lease(
                        current,
                        reason="recovery_readback_verified",
                        result={
                            "source_watch_id": watch_id,
                            "packet_id": packet.id,
                            "status": packet.status,
                            "learning": NO_LEARNING,
                            "memory_status": NO_LEARNING,
                        },
                    )
                except Exception:
                    current = await durable_job_repository.get_job(job_id) or job
                    await self._release_recovery_lease(
                        current,
                        reason="durable_job_reconciliation_required",
                    )
                    return {
                        "status": "blocked",
                        "job_id": job_id,
                        "packet_id": packet.id,
                        "reason_code": "durable_job_reconciliation_required",
                        "operator_visible": True,
                    }
            if _text(job.get("status")) not in {"succeeded", "degraded"}:
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
            notification_id = await self._repair_packet_notification(watch, packet)
            await self._release_watch(watch_id, job_id, int(watch.active_job_fence), final_status)
            return {
                "status": final_status,
                "job_id": job_id,
                "packet_id": packet.id,
                "notification_id": notification_id,
                "recovery": "verified_and_reconciled",
                "learning": NO_LEARNING,
            }
        if recovery_lease_acquired:
            await self._release_recovery_lease(job, reason=packet.failure_code or "reconciliation_required")
        return {
            "status": "blocked",
            "job_id": job_id,
            "packet_id": packet.id,
            "reason_code": packet.failure_code or "reconciliation_required",
            "operator_visible": True,
        }

    @staticmethod
    def _workspace_write_intent(job: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
        """Extract only the bounded write-intent paths/digests from a job."""

        checkpoints = job.get("checkpoints") if isinstance(job.get("checkpoints"), list) else []
        for checkpoint in reversed(checkpoints):
            if not isinstance(checkpoint, Mapping) or _text(checkpoint.get("checkpoint_id")) != "workspace_write_intent":
                continue
            payload = checkpoint.get("payload") if isinstance(checkpoint.get("payload"), Mapping) else {}
            paths = payload.get("paths") if isinstance(payload.get("paths"), list) else []
            digests = payload.get("digests") if isinstance(payload.get("digests"), Mapping) else {}
            result = tuple(
                (_text(path), _text(digests.get(path)))
                for path in paths
                if _text(path) and _text(digests.get(path))
            )
            if result:
                return result
        return ()

    async def _assert_execution_fence(
        self,
        job_id: str,
        *,
        owner: str,
        fencing_token: int,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Re-read the durable lease immediately before a workspace effect."""

        current = await durable_job_repository.get_job(job_id)
        lease = current.get("lease") if isinstance(current, Mapping) and isinstance(current.get("lease"), Mapping) else {}
        if (
            current is None
            or _text(current.get("status")) != "running"
            or _text(lease.get("owner")) != _text(owner)
            or int(lease.get("fencing_token") or 0) != int(fencing_token)
            or int(current.get("revision") or 0) != int(expected_revision)
        ):
            raise SourceWatchError("durable_job_fence_stale")
        expires_at = lease.get("expires_at")
        if expires_at:
            try:
                if datetime.fromisoformat(str(expires_at).replace("Z", "+00:00")) <= _now():
                    raise SourceWatchError("durable_job_fence_expired")
            except ValueError as exc:
                raise SourceWatchError("durable_job_fence_invalid") from exc
        return dict(current)

    async def _retain_unverified_workspace_artifacts(
        self,
        job: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Record partial files before cancellation while the durable fence is live."""

        job_id = _text(job.get("job_id"))
        lease = job.get("lease") if isinstance(job.get("lease"), Mapping) else {}
        owner = _text(lease.get("owner")) or SERVICE_PRINCIPAL
        fencing_token = int(lease.get("fencing_token") or 0)
        revision = int(job.get("revision") or 0)
        if not job_id or _text(job.get("status")) != "running":
            return dict(job)
        current = await self._assert_execution_fence(
            job_id,
            owner=owner,
            fencing_token=fencing_token,
            expected_revision=revision,
        )
        for path, expected_digest in self._workspace_write_intent(current):
            try:
                resolved = _safe_resolve(path)
                if resolved.is_symlink() or not resolved.is_file() or resolved.stat().st_nlink != 1:
                    continue
                content, truncated = _read_workspace_text_bounded(
                    resolved,
                    max_bytes=PACKET_MAX_BYTES if "/packets/" in path else TASK_MAX_BYTES,
                )
                if truncated:
                    continue
            except (OSError, ValueError):
                continue
            artifact = await durable_job_repository.record_artifact(
                job_id,
                file_path=path,
                artifact_type="guardian_unverified_workspace_artifact",
                content=content,
                owner=owner,
                fencing_token=fencing_token,
                expected_revision=int(current.get("revision") or revision),
            )
            revision = int(artifact.get("revision") or revision)
            effect = await durable_job_repository.record_effect(
                job_id,
                effect_type="workspace_write",
                effect_id=f"workspace-write-unverified:{path}",
                target_path=path,
                target_digest=expected_digest,
                content_sha256=_sha(content),
                status="unknown",
                details={
                    "verified": False,
                    "unverified": True,
                    "output_exists": True,
                    "workspace_contained": True,
                },
                owner=owner,
                fencing_token=fencing_token,
                expected_revision=revision,
            )
            revision = int(effect.get("revision") or revision)
            current = await durable_job_repository.get_job(job_id) or current
        return current

    async def _retain_recovery_unverified_artifacts(
        self,
        watch: GuardianSourceWatch,
        packet: GuardianDecisionPacket,
        job: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """Register files left by a crashed worker without claiming success."""

        if _text(job.get("status")) == "cancelled":
            return []
        records: list[dict[str, Any]] = []
        revision = int(job.get("revision") or 0)
        for path, expected_digest in self._workspace_write_intent(job):
            try:
                resolved = _safe_resolve(path)
                if resolved.is_symlink() or not resolved.is_file() or resolved.stat().st_nlink != 1:
                    continue
                content, truncated = _read_workspace_text_bounded(
                    resolved,
                    max_bytes=PACKET_MAX_BYTES if "/packets/" in path else TASK_MAX_BYTES,
                )
                if truncated:
                    continue
            except (OSError, ValueError):
                continue
            receipt = await durable_job_repository.record_recovery_artifact(
                packet.run_identity,
                owner_kind=SERVICE_OWNER_KIND,
                owner_principal_id=SERVICE_PRINCIPAL,
                file_path=path,
                artifact_type="guardian_unverified_workspace_artifact",
                content=content,
                expected_revision=revision,
            )
            revision = int(receipt.get("revision") or revision)
            records.append(
                {
                    "file_path": path,
                    "artifact_id": (receipt.get("receipt") or {}).get("artifact_id"),
                    "content_sha256": _sha(content),
                    "expected_sha256": expected_digest,
                    "verified": False,
                }
            )
        return records

    async def _execute_packet(
        self,
        watch: GuardianSourceWatch,
        packet: GuardianDecisionPacket,
        job_id: str,
        *,
        claimed: Mapping[str, Any] | None = None,
        scan: ScanResult | None = None,
    ) -> dict[str, Any]:
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
                "digests": {
                    dossier_path: _sha(packet.proposal_text),
                    task_path: _sha(packet.task_text),
                },
                "dossier_sha256": _sha(packet.proposal_text),
                "task_sha256": _sha(packet.task_text),
            },
            safe=True,
            owner=owner,
            fencing_token=fence,
            expected_revision=revision,
        )
        revision = int(intent.get("revision") or revision)
        await self._assert_execution_fence(
            job_id,
            owner=owner,
            fencing_token=fence,
            expected_revision=revision,
        )
        _write_workspace_text_bounded(_safe_resolve(dossier_path), packet.proposal_text, max_bytes=PACKET_MAX_BYTES)
        await self._assert_execution_fence(
            job_id,
            owner=owner,
            fencing_token=fence,
            expected_revision=revision,
        )
        _write_workspace_text_bounded(_safe_resolve(task_path), packet.task_text, max_bytes=TASK_MAX_BYTES)
        await self._assert_execution_fence(
            job_id,
            owner=owner,
            fencing_token=fence,
            expected_revision=revision,
        )
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
        # Re-read the durable lease immediately before packet/baseline
        # finalization.  The watch reservation fence is separate from this
        # durable job fence; only the latter authorizes these writes.
        await self._assert_execution_fence(
            job_id,
            owner=owner,
            fencing_token=fence,
            expected_revision=revision,
        )
        await self._finalize_packet(
            watch,
            packet,
            job_id=job_id,
            owner=owner,
            fencing_token=fence,
            expected_revision=revision,
            dossier_path=dossier_path,
            task_path=task_path,
            dossier_record=dossier_record,
            task_record=task_record,
            scan=scan,
        )
        notification = await self._enqueue_completion_notification(
            watch,
            packet,
            dossier_sha256=str(dossier_record["content_sha256"]),
            task_sha256=str(task_record["content_sha256"]),
            status="degraded" if scan is not None and scan.degraded else "succeeded",
        )
        notification_id = _text(getattr(notification, "id", None)) or None
        await self._record_packet_notification(
            packet.id,
            notification_id,
            status="queued" if notification_id else "budget_denied",
        )
        current = await self._assert_execution_fence(
            job_id,
            owner=owner,
            fencing_token=fence,
            expected_revision=revision,
        )
        revision = int(current.get("revision") or revision)
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
                "notification_id": notification_id,
                "learning": NO_LEARNING,
                "memory_status": NO_LEARNING,
            },
            result_summary="verified source-watch dossier and local task readback",
        )
        return {"notification_id": notification_id}

    async def _settle_observation_job(
        self,
        job_id: str,
        *,
        status: str,
        reason: str,
        packet_id: str | None = None,
    ) -> None:
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
        result = {
            "status": status,
            "learning": "no_learning",
            "memory_status": "no_learning",
        }
        if packet_id:
            result["packet_id"] = packet_id
        await durable_job_repository.transition_job(
            job_id,
            status,
            owner=owner,
            fencing_token=fence,
            expected_revision=readback.get("revision"),
            reason=reason,
            result=result,
            result_summary=reason,
        )

    async def _apply_baseline_updates(
        self,
        db: Any,
        watch_id: str,
        updates: Sequence[SourceObservation],
    ) -> None:
        """Apply source baselines in the caller's transaction."""

        for item in updates:
            if item.baseline_text is None:
                continue
            row = (
                await db.execute(
                    select(GuardianSourceBaseline).where(
                        GuardianSourceBaseline.watch_id == watch_id,
                        GuardianSourceBaseline.source_key == item.source.source_key,
                    )
                )
            ).scalars().first()
            if row is None:
                row = GuardianSourceBaseline(
                    watch_id=watch_id,
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

    async def _commit_baselines(
        self,
        watch: GuardianSourceWatch,
        scan: ScanResult,
        *,
        status: str,
        job_id: str | None = None,
        fencing_token: int | None = None,
    ) -> None:
        if job_id and fencing_token:
            current_job = await durable_job_repository.get_job(job_id)
            if current_job is None:
                raise SourceWatchError("durable_job_missing")
            lease = current_job.get("lease") if isinstance(current_job.get("lease"), Mapping) else {}
            await self._assert_execution_fence(
                job_id,
                owner=_text(lease.get("owner")) or SERVICE_PRINCIPAL,
                fencing_token=int(fencing_token),
                expected_revision=int(current_job.get("revision") or 0),
            )
        async with db_engine.get_session() as db:
            current = (
                await db.execute(select(GuardianSourceWatch).where(GuardianSourceWatch.id == watch.id))
            ).scalars().first()
            if current is None:
                raise SourceWatchError("watch_not_found")
            if (
                _text(current.state) != "active"
                or int(current.plan_revision or 0) != int(watch.plan_revision or 0)
            ):
                raise SourceWatchError("watch_plan_revision_stale")
            if job_id and (
                _text(current.active_job_id) != _text(job_id)
                or int(current.active_job_fence or 0) <= 0
            ):
                raise SourceWatchError("watch_execution_fence_stale")
            await self._apply_baseline_updates(db, watch.id, scan.baseline_updates)
            current.last_status = status
            current.updated_at = _now()
            db.add(current)

    async def _finalize_packet(
        self,
        watch: GuardianSourceWatch,
        packet: GuardianDecisionPacket,
        *,
        job_id: str,
        owner: str,
        fencing_token: int,
        expected_revision: int,
        dossier_path: str,
        task_path: str,
        dossier_record: Mapping[str, Any],
        task_record: Mapping[str, Any],
        scan: ScanResult | None,
    ) -> None:
        await self._assert_execution_fence(
            job_id,
            owner=owner,
            fencing_token=fencing_token,
            expected_revision=expected_revision,
        )
        async with db_engine.get_session() as db:
            row = (
                await db.execute(select(GuardianDecisionPacket).where(GuardianDecisionPacket.id == packet.id))
            ).scalars().first()
            if row is None:
                raise SourceWatchError("packet_not_found")
            # Packet terminal metadata and the comparable baseline advance are
            # one local transaction.  A restart cannot observe a successful
            # packet with an uncommitted baseline (or vice versa).
            if scan is not None:
                current_watch = (
                    await db.execute(select(GuardianSourceWatch).where(GuardianSourceWatch.id == watch.id))
                ).scalars().first()
                if current_watch is None:
                    raise SourceWatchError("watch_not_found")
                if (
                    _text(current_watch.state) != "active"
                    or int(current_watch.plan_revision or 0) != int(watch.plan_revision or 0)
                    or _text(current_watch.active_job_id) != _text(job_id)
                    or int(current_watch.active_job_fence or 0) <= 0
                ):
                    raise SourceWatchError("watch_execution_fence_stale")
                await self._apply_baseline_updates(db, watch.id, scan.baseline_updates)
                current_watch.last_status = "degraded" if scan.degraded else "succeeded"
                current_watch.updated_at = _now()
                db.add(current_watch)
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

    async def _enqueue_completion_notification(
        self,
        watch: GuardianSourceWatch,
        packet: GuardianDecisionPacket,
        *,
        dossier_sha256: str,
        task_sha256: str,
        status: str,
    ) -> Any:
        """Queue one idempotent, source-content-free post-readback receipt."""

        body = _completion_notification_body(
            packet_id=packet.id,
            status=status,
            dossier_sha256=dossier_sha256,
            task_sha256=task_sha256,
        )
        notification_budget: dict[str, Any] = {}
        async with db_engine.get_session() as db:
            goal = (await db.execute(select(Goal).where(Goal.id == watch.goal_id))).scalars().first()
        if goal is not None:
            budget = deserialize_admission_budget(goal)
            if budget is not None:
                period_started_at = getattr(budget, "period_started_at", None)
                period_key = (
                    period_started_at.astimezone(timezone.utc).isoformat()
                    if isinstance(period_started_at, datetime)
                    else _now().date().isoformat()
                )
                notification_budget = {
                    "goal_id": watch.goal_id,
                    "goal_revision": max(int(goal.revision or 1), 1),
                    "budget_period_key": period_key,
                    "budget_limit": int(getattr(budget, "notifications_per_day", 0)),
                }
        try:
            return await native_notification_queue.enqueue(
                intervention_id=f"guardian-source-watch:{packet.id}",
                idempotency_key=f"guardian-source-watch:completion:{packet.id}",
                title="Guardian source watch completed",
                body=body,
                intervention_type="guardian_source_watch",
                urgency=3,
                surface="notification",
                session_id=watch.owner_session_id,
                owner_principal_id=watch.owner_principal_id,
                operator_session_id=watch.owner_session_id,
                channel="native_notification",
                transport="native_notification",
                correlation_id=packet.id,
                causation_id=packet.run_identity,
                **notification_budget,
            )
        except NativeNotificationBudgetDenied:
            # The completed capability remains durable even when its optional
            # delivery receipt reaches the canonical daily cap.  The packet
            # outcome records the denial so the operator can distinguish it
            # from a transport failure.
            return None
        except Exception as exc:
            raise SourceWatchError("notification_enqueue_failed", str(exc)) from exc

    async def _record_packet_notification(
        self,
        packet_id: str,
        notification_id: str | None,
        *,
        status: str = "queued",
    ) -> None:
        async with db_engine.get_session() as db:
            row = (
                await db.execute(select(GuardianDecisionPacket).where(GuardianDecisionPacket.id == packet_id))
            ).scalars().first()
            if row is None:
                raise SourceWatchError("packet_not_found")
            outcome = _load(row.outcome_json, {})
            if not isinstance(outcome, Mapping):
                outcome = {}
            row.outcome_json = _dump(
                {
                    **dict(outcome),
                    "notification_id": _text(notification_id) or None,
                    "notification_status": _text(status) or "queued",
                }
            )
            row.updated_at = _now()
            db.add(row)

    async def _repair_packet_notification(
        self,
        watch: GuardianSourceWatch,
        packet: GuardianDecisionPacket,
    ) -> str | None:
        """Repair the idempotent completion receipt after a restart gap."""

        outcome = _load(packet.outcome_json, {})
        if isinstance(outcome, Mapping):
            if _text(outcome.get("notification_id")):
                return _text(outcome.get("notification_id"))
            if _text(outcome.get("notification_status")) in {"budget_denied", "unavailable"}:
                return None
        try:
            notification = await self._enqueue_completion_notification(
                watch,
                packet,
                dossier_sha256=_text(packet.dossier_sha256) or _sha(packet.proposal_text),
                task_sha256=_text(packet.task_sha256) or _sha(packet.task_text),
                status="degraded" if packet.status == "degraded" else "succeeded",
            )
            notification_id = _text(getattr(notification, "id", None)) or None
            await self._record_packet_notification(
                packet.id,
                notification_id,
                status="queued" if notification_id else "budget_denied",
            )
            return notification_id
        except SourceWatchError:
            # Recovery of the verified artifact must remain possible even if
            # the optional native delivery transport is unavailable.  Persist
            # the absence explicitly so the operator can distinguish it from
            # a missing capability result.
            try:
                await self._record_packet_notification(packet.id, None, status="unavailable")
            except Exception:
                pass
            return None

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
        prior_delta_id: str | None = None,
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
            packet_query = select(GuardianDecisionPacket).where(
                GuardianDecisionPacket.watch_id == watch.id,
                GuardianDecisionPacket.source_watch_id == watch.id,
                GuardianDecisionPacket.goal_id == watch.goal_id,
            )
            if prior_delta_id:
                packet_query = packet_query.where(
                    GuardianDecisionPacket.strategy_delta_id == _text(prior_delta_id)
                )
            packet = (
                await db.execute(
                    packet_query.order_by(
                        GuardianDecisionPacket.created_at.desc(),
                        GuardianDecisionPacket.id.desc(),
                    )
                )
            ).scalars().first()
            if packet is None:
                raise SourceWatchError("correction_packet_required")
            if not _text(packet.run_identity):
                raise SourceWatchError("correction_packet_revision_stale")
            if packet.status not in {"succeeded", "degraded"} or packet.verification_status != "passed":
                raise SourceWatchError("correction_packet_not_verified")
            if prior_delta_id is None and (
                int(packet.goal_revision or 0) != int(expected_goal_revision)
                or int(packet.plan_revision or 0) != int(expected_plan_revision)
            ):
                raise SourceWatchError("correction_packet_revision_stale")
            if prior_delta_id is None and packet.strategy_delta_id:
                raise SourceWatchError("correction_already_applied")
            old = _load(watch.criteria_json, {})
            prior_delta = None
            if prior_delta_id is not None:
                prior_delta = (
                    await db.execute(
                        select(StrategyDelta).where(
                            StrategyDelta.delta_id == _text(prior_delta_id),
                            StrategyDelta.goal_id == watch.goal_id,
                            StrategyDelta.field_name == "research_watch_criteria",
                        )
                    )
                ).scalars().first()
                if prior_delta is None:
                    raise SourceWatchError("correction_undo_target_missing")
                new = _restore_prior_criteria(
                    old,
                    prior_status=prior_delta.status,
                    prior_before_json=prior_delta.before_json,
                    prior_after_json=prior_delta.after_json,
                )
            else:
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
                source_event_id=(
                    f"guardian-decision-packet:{packet.id}:undo:{prior_delta.delta_id}"
                    if prior_delta is not None
                    else f"guardian-decision-packet:{packet.id}"
                ),
                author_id=owner_principal_id,
                evaluator_id=owner_principal_id,
                goal_revision_before=goal.revision,
                goal_revision_after=next_goal_revision,
                status="active",
                rollback_target_id=prior_delta.delta_id if prior_delta is not None else None,
                reason=_text(reason)[:1000],
            )
            now = _now()
            goal_update = await db.execute(
                update(Goal)
                .where(
                    Goal.id == watch.goal_id,
                    Goal.revision == expected_goal_revision,
                    Goal.owner_principal_id == owner_principal_id,
                    Goal.owner_session_id == owner_session_id,
                )
                .values(revision=next_goal_revision, updated_at=now)
            )
            if getattr(goal_update, "rowcount", 0) != 1:
                raise SourceWatchError("correction_revision_stale")
            next_plan_revision = int(expected_plan_revision) + 1
            next_criteria_json = _dump(new)
            watch_update = await db.execute(
                update(GuardianSourceWatch)
                .where(
                    GuardianSourceWatch.id == watch_id,
                    GuardianSourceWatch.owner_principal_id == owner_principal_id,
                    GuardianSourceWatch.owner_session_id == owner_session_id,
                    GuardianSourceWatch.goal_revision == expected_goal_revision,
                    GuardianSourceWatch.plan_revision == expected_plan_revision,
                )
                .values(
                    goal_revision=next_goal_revision,
                    plan_revision=next_plan_revision,
                    criteria_json=next_criteria_json,
                    criteria_digest=_sha(next_criteria_json),
                    updated_at=now,
                )
            )
            if getattr(watch_update, "rowcount", 0) != 1:
                raise SourceWatchError("correction_revision_stale")
            if prior_delta is not None:
                prior_delta.status = "rolled_back"
                prior_delta.rollback_target_id = prior_delta.delta_id
                prior_delta.updated_at = now
                db.add(prior_delta)
            db.add(delta)
            packet.strategy_delta_id = delta.delta_id
            packet.updated_at = now
            db.add(packet)
            await db.flush()
            result = {
                "status": "succeeded",
                "watch_id": watch.id,
                "goal_revision": next_goal_revision,
                "plan_revision": next_plan_revision,
                "strategy_delta_id": delta.delta_id,
                "rollback_target_id": delta.rollback_target_id,
                "memory_status": "no_learning",
            }
        await _audit_watch_event(
            {
                "id": watch_id,
                "goal_id": watch.goal_id,
                "owner_principal_id": owner_principal_id,
                "owner_session_id": owner_session_id,
                "goal_revision": result["goal_revision"],
                "plan_revision": result["plan_revision"],
                "source_set_digest": watch.source_set_digest,
                "criteria_digest": _sha(_dump(new)),
            },
            "corrected",
            strategy_delta_id=result["strategy_delta_id"],
            rollback_target_id=result.get("rollback_target_id"),
            source_event_id=(
                f"guardian-decision-packet:{packet.id}:undo:{prior_delta_id}"
                if prior_delta_id
                else f"guardian-decision-packet:{packet.id}"
            ),
        )
        return result


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
    reviewed_grant_id: str | None = None


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
    prior_delta_id: str | None = None


class SourceWatchCorrectionUndoRequest(BaseModel):
    expected_goal_revision: int = Field(ge=1)
    expected_plan_revision: int = Field(ge=1)
    prior_strategy_delta_id: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=1000)



source_watch_router = APIRouter()
source_watch_service = SourceWatchService()


def _operator(request: Request):
    from src.api.capabilities import _require_authenticated_capability_operator

    return _require_authenticated_capability_operator(request)


def _http_error(exc: SourceWatchError) -> HTTPException:
    code = exc.code
    status = (
        409
        if "stale" in code
        or "revision" in code
        or "mismatch" in code
        or "packet" in code
        or "already_applied" in code
        else 400
    )
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
    return await source_watch_service.list_watches(
        owner_principal_id=operator.principal.principal_id,
        owner_session_id=operator.session_id,
    )


@source_watch_router.get("/capabilities/source-watches/{watch_id}")
async def get_source_watch(watch_id: str, request: Request):
    operator = _operator(request)
    result = await source_watch_service.get_watch(
        watch_id,
        owner_principal_id=operator.principal.principal_id,
        owner_session_id=operator.session_id,
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
            reviewed_grant_id=req.reviewed_grant_id,
        )
    except SourceWatchError as exc:
        raise _http_error(exc) from exc


@source_watch_router.post("/capabilities/source-watches/{watch_id}/run")
async def run_source_watch(watch_id: str, req: SourceWatchRunRequest, request: Request):
    operator = _operator(request)
    watch = await source_watch_service.get_watch(
        watch_id,
        owner_principal_id=operator.principal.principal_id,
        owner_session_id=operator.session_id,
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
            prior_delta_id=req.prior_delta_id,
        )
    except SourceWatchError as exc:
        raise _http_error(exc) from exc


@source_watch_router.post("/capabilities/source-watches/{watch_id}/corrections/undo")
async def undo_source_watch_correction(
    watch_id: str,
    req: SourceWatchCorrectionUndoRequest,
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
            include_terms=[],
            exclude_terms=[],
            reason=req.reason,
            prior_delta_id=req.prior_strategy_delta_id,
        )
    except SourceWatchError as exc:
        raise _http_error(exc) from exc



__all__ = [
    "CAPABILITY_ID",
    "CAPABILITY_VERSION",
    "SourceObservation",
    "SourceSpec",
    "SourceWatchCancelRequest",
    "SourceWatchCorrectionUndoRequest",
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
