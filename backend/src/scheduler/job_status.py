"""The durable per-job status record (#1025 D2): ``background_job_status``.

One row per scheduled job (system job or ``user_cron:<id>``) or lifecycle
domain (``lifecycle:scheduler``, ``lifecycle:dispatch``). It holds status,
never work: nothing here is a queue, is replayed or starts anything.

Every write is one ``BEGIN IMMEDIATE`` transaction (read, decide, write), so
concurrent writers in the single workspace-owner process serialise on
SQLite's writer lock. Writes are a monotonic-epoch upsert: a write that names
an epoch older than the stored one changes nothing, so a stale tick can never
overwrite (or clear) a newer epoch's status. A write with no epoch keeps the
stored epoch. There is deliberately no ``domain_ready`` fence on these writes:
a blocked status must be writable while the owner row is not ready.

The notification that announces a transition into ``blocked`` is claimed in
the same transaction that records the block (``notified_for`` = the block's
``since``) and delivered after commit. It is therefore at most once per
transition, across restarts: a crash between the claim and the delivery loses
that one notification (the persisted block stays the visible record) rather
than ever repeating it.

#1020 PR1's JSON record (``background-inference-status.json``) is folded into
this table once per workspace on first use, then removed, so the repository
keeps one status record.

Importing this module creates nothing.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select, text

from src.utils.clock import get_clock

logger = logging.getLogger(__name__)

STATES = ("ok", "skipped", "blocked", "disabled")
SOURCE_LIFECYCLE = "lifecycle"
SOURCE_GUARDIAN = "guardian"
SOURCE_INFERENCE = "inference"
DOMAIN_KEY_PREFIX = "lifecycle:"
LEGACY_SCHEMA_VERSION = "seraph.background-inference.v1"
_MAX_LEGACY_BYTES = 262_144
# Workspaces whose legacy JSON record was already folded in by this process.
_legacy_folded: set[str] = set()


def _now() -> datetime:
    return get_clock().now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def domain_key(short: str) -> str:
    return DOMAIN_KEY_PREFIX + short


def legacy_status_path() -> Path:
    from config.settings import settings
    return Path(settings.workspace_dir).expanduser().resolve() / "background-inference-status.json"


class _Write:
    """One serialised read-decide-write unit on the status table, unfenced by design."""

    def __init__(self, job_id: str, epoch: int | None):
        self.job_id = job_id
        self.epoch = epoch
        self.row = None
        self.stale = False

    async def __aenter__(self):
        from src.db.engine import commit_guard, get_session
        from src.db.models import BackgroundJobStatus
        self._guard = commit_guard(None)
        self._guard.__enter__()
        self._scope = get_session()
        try:
            self.db = await self._scope.__aenter__()
            await _fold_legacy(self.db)
            if self.db.get_bind().dialect.name == "sqlite":
                if self.db.in_transaction():
                    await self.db.commit()
                await self.db.execute(text("BEGIN IMMEDIATE"))
            self.row = (await self.db.execute(select(BackgroundJobStatus).where(
                BackgroundJobStatus.job_id == self.job_id))).scalar_one_or_none()
            self.stale = (self.epoch is not None and self.row is not None and self.row.epoch is not None
                          and self.epoch < self.row.epoch)
        except BaseException as exc:
            await self._scope.__aexit__(type(exc), exc, exc.__traceback__)
            self._guard.__exit__(None, None, None)
            raise
        return self

    def put(self, **values: Any) -> Any:
        """Insert or replace the row's fields; a stale epoch writes nothing."""
        from src.db.models import BackgroundJobStatus
        if self.stale:
            return self.row
        if self.epoch is not None:
            values["epoch"] = self.epoch
        if self.row is None:
            now = _iso(_now())
            values.setdefault("since", now)
            values.setdefault("last_attempt_at", now)
            self.row = BackgroundJobStatus(job_id=self.job_id, **values)
            self.db.add(self.row)
        else:
            for key, value in values.items():
                setattr(self.row, key, value)
        return self.row

    async def __aexit__(self, exc_type, exc, tb):
        try:
            return await self._scope.__aexit__(exc_type, exc, tb)
        finally:
            self._guard.__exit__(None, None, None)


async def _fold_legacy(db) -> None:
    """Import #1020 PR1's JSON status record once per workspace, then remove it."""
    path = legacy_status_path()
    key = str(path)
    if key in _legacy_folded:
        return
    _legacy_folded.add(key)
    if not path.exists():
        return
    from src.db.models import BackgroundJobStatus
    from src.scheduler.background_inference import status_job_known
    entries: dict[str, Any] = {}
    try:
        if path.stat().st_size <= _MAX_LEGACY_BYTES:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and payload.get("schema_version") == LEGACY_SCHEMA_VERSION:
                entries = payload.get("jobs") or {}
    except (OSError, ValueError):
        logger.warning("The legacy background-inference status file is unreadable; it is not imported")
    imported = 0
    for job_id, value in (entries.items() if isinstance(entries, dict) else ()):
        if not isinstance(value, dict) or not status_job_known(job_id):
            continue
        if await db.get(BackgroundJobStatus, job_id) is not None:
            continue  # the table is newer than the file
        status = str(value.get("status") or "")
        state = {"succeeded": "ok", "skipped": "skipped", "blocked": "blocked"}.get(status)
        at = str(value.get("at") or "") or _iso(_now())
        if state is None:
            continue
        db.add(BackgroundJobStatus(
            job_id=job_id, state=state, reason_code=str(value.get("reason") or ""),
            recovery=str(value["recovery"]) if value.get("recovery") else None,
            source=str(value.get("source") or (SOURCE_GUARDIAN if state == "blocked" else SOURCE_INFERENCE)),
            since=str(value.get("since") or at), last_attempt_at=at,
            last_success_at=at if state == "ok" else None,
            # A block carried over from the file was already announced under the old record.
            notified_for=str(value.get("since") or at) if state == "blocked" else None,
            audited_at=str(value["_audited_at"]) if value.get("_audited_at") else None,
            activity_at=str(value["activity_at"]) if value.get("activity_at") else None))
        imported += 1
    await db.commit()
    for leftover in (path, path.with_suffix(".tmp")):
        try:
            leftover.unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove the folded legacy status file %s", leftover.name)
    logger.info("Folded %d legacy background job status entr%s into background_job_status", imported,
                "y" if imported == 1 else "ies")


# ------------------------------------------------------------------ writers


async def record_skip(job_id: str, reason: str, *, audit_interval: timedelta,
                      source: str = SOURCE_INFERENCE, epoch: int | None = None) -> bool:
    """A visible skip; return whether it is due an audit event (one per transition, then per interval)."""
    now = _now()
    async with _Write(job_id, epoch) as write:
        row = write.row
        same = row is not None and row.state == "skipped" and row.reason_code == reason
        audited = _parse(row.audited_at) if same else None
        due = audited is None or now - audited >= audit_interval
        write.put(state="skipped", reason_code=reason, recovery=None, source=source,
                  since=row.since if same else _iso(now), last_attempt_at=_iso(now),
                  audited_at=_iso(now) if due else row.audited_at)
    return due and not write.stale


async def record_run(job_id: str) -> None:
    """The job ran: its next skip is a new transition (clears the audit marker only)."""
    async with _Write(job_id, None) as write:
        if write.row is not None and write.row.audited_at is not None:
            write.put(audited_at=None)


async def record_blocked(job_id: str, reason: str, recovery: str, *, source: str,
                         activity_interval: timedelta | None = None, epoch: int | None = None,
                         keep_since_on_reason_change: bool = True) -> dict[str, bool]:
    """Persist a block. Returns ``notify`` (this transition's one notification is claimed now),
    ``transition`` (entering blocked) and ``activity_due`` (at most once per interval)."""
    now = _now()
    async with _Write(job_id, epoch) as write:
        row = write.row
        blocked = row is not None and row.state == "blocked"
        keep = blocked and (keep_since_on_reason_change or row.reason_code == reason)
        since = row.since if keep else _iso(now)
        last_activity = _parse(row.activity_at) if blocked else None
        due = activity_interval is None or last_activity is None or now - last_activity >= activity_interval
        notify = row is None or row.notified_for != since
        write.put(state="blocked", reason_code=reason, recovery=recovery, source=source, since=since,
                  last_attempt_at=_iso(now), notified_for=since,
                  activity_at=_iso(now) if due else row.activity_at)
    if write.stale:
        return {"transition": False, "activity_due": False, "notify": False}
    return {"transition": not blocked, "activity_due": due, "notify": notify}


async def record_ok(job_id: str, *, only_source: str | None = None, clear_states: tuple[str, ...] = ("blocked",),
                    epoch: int | None = None) -> bool:
    """A successful run clears a block (``state=ok``); return whether a block was cleared."""
    now = _iso(_now())
    async with _Write(job_id, epoch) as write:
        row = write.row
        if row is None:
            return False
        clears = row.state in clear_states and (only_source is None or row.source == only_source)
        if clears:
            write.put(state="ok", reason_code="", recovery=None, since=now, last_attempt_at=now,
                      last_success_at=now, notified_for=None)
        elif row.state == "ok":
            write.put(last_attempt_at=now, last_success_at=now)
    return clears and not write.stale


async def record_missed(job_id: str) -> None:
    """APScheduler dropped a fire past its misfire grace; a block stays the more specific record."""
    now = _iso(_now())
    async with _Write(job_id, None) as write:
        row = write.row
        if row is not None and row.state == "blocked":
            return
        same = row is not None and row.state == "skipped" and row.reason_code == "missed_fire"
        write.put(state="skipped", reason_code="missed_fire", recovery=None, source=SOURCE_LIFECYCLE,
                  since=row.since if same else now, last_attempt_at=now)


async def record_disabled(job_id: str, reason: str, recovery: str, *, epoch: int | None = None) -> None:
    """The operator disabled this work (``SCHEDULER_ENABLED=false``): visible, never blocked, never notified."""
    now = _iso(_now())
    async with _Write(job_id, epoch) as write:
        row = write.row
        same = row is not None and row.state == "disabled" and row.reason_code == reason
        write.put(state="disabled", reason_code=reason, recovery=recovery, source=SOURCE_LIFECYCLE,
                  since=row.since if same else now, last_attempt_at=now, notified_for=None)


# ------------------------------------------------------------------ readers


def _projection(row) -> dict[str, Any]:
    return {"status": "succeeded" if row.state == "ok" else row.state, "state": row.state,
            "reason": row.reason_code, "recovery": row.recovery, "at": row.last_attempt_at, "since": row.since,
            "source": row.source, "epoch": row.epoch, "last_success_at": row.last_success_at}


async def statuses(predicate: Callable[[Any], bool] | None = None) -> dict[str, dict[str, Any]]:
    """Every stored status, keyed by job id (projected; private markers are left out)."""
    from src.db.engine import commit_guard, get_session
    from src.db.models import BackgroundJobStatus
    with commit_guard(None):
        async with get_session() as db:
            await _fold_legacy(db)
            rows = (await db.execute(select(BackgroundJobStatus).order_by(BackgroundJobStatus.job_id))).scalars()
            return {row.job_id: _projection(row) for row in rows if predicate is None or predicate(row)}


async def status_row(job_id: str) -> dict[str, Any] | None:
    """One row with its private markers, for tests and diagnostics."""
    from src.db.engine import commit_guard, get_session
    from src.db.models import BackgroundJobStatus
    with commit_guard(None):
        async with get_session() as db:
            await _fold_legacy(db)
            row = await db.get(BackgroundJobStatus, job_id)
            return None if row is None else {**_projection(row), "notified_for": row.notified_for,
                                             "audited_at": row.audited_at, "activity_at": row.activity_at}


__all__ = ["DOMAIN_KEY_PREFIX", "SOURCE_GUARDIAN", "SOURCE_INFERENCE", "SOURCE_LIFECYCLE", "STATES", "domain_key",
           "legacy_status_path", "record_blocked", "record_disabled", "record_missed", "record_ok", "record_run",
           "record_skip", "status_row", "statuses"]
