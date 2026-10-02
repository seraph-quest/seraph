"""Bounded, read-only Google Calendar integration for M5.

The module intentionally owns only the small Calendar surface required by the
operator cockpit.  Credentials are accepted as write-only values by the API
and are read from the encrypted vault here; provider identities are never
returned as a generic URL or passed to the model as authority.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import quote, urlencode, urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.db.models import (
    CalendarEventBinding,
    CalendarReadConsent,
    GoogleServiceConnection as GoogleServiceConnectionRow,
)
from src.security.http_transport import (
    MAX_RESPONSE_BYTES,
    PinnedTransportError,
    _TransportLifecycleMarker,
    default_resolver,
    request_pinned_https,
)
from src.vault import decrypt, encrypt, vault_repository
from src.workspace import canonical_workspace_root
from config.settings import settings


GoogleServiceConnection = GoogleServiceConnectionRow
GOOGLE_API_ORIGIN = "https://www.googleapis.com"
GOOGLE_TOKEN_ORIGIN = "https://oauth2.googleapis.com"
CALENDAR_LIST_PATH = "/calendar/v3/users/me/calendarList"
EVENTS_PATH = "/calendar/v3/calendars"
MAX_PROVIDER_RESPONSE_BYTES = 256 * 1024
MAX_CALENDAR_RESULT_BYTES = 64 * 1024
MAX_EVENTS = 50
MAX_CALENDARS = 50
MAX_PAGES = 3
MAX_TEXT = 4000
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.:@+,-]{1,1024}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_CALENDAR_RESULT_PATH = re.compile(
    r"^artifacts/work-board/calendar/result-[0-9a-f]{32}\.json$"
)
_JSON_CONTENT_TYPE = re.compile(
    r"^application/json(?:\s*;\s*charset\s*=\s*[\"']?utf-8[\"']?)?\s*$",
    re.IGNORECASE,
)


class CalendarIntegrationError(RuntimeError):
    """Safe, operator-visible failure without provider payloads."""

    def __init__(self, code: str, message: str, *, status_code: int = 409, recovery_action: str | None = None):
        self.code = str(code)[:128]
        self.status_code = int(status_code)
        self.recovery_action = recovery_action
        super().__init__(str(message)[:500])


def _utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def calendar_artifact_path_for_job(job_id: str) -> str:
    """Return the one deterministic result path for a prep durable root."""

    return f"artifacts/work-board/calendar/result-{digest(str(job_id))[:32]}.json"


def _open_calendar_result_parent(
    relative_path: str,
    *,
    workspace_root: str | Path | None = None,
    create_parents: bool = False,
) -> int | None:
    candidate = PurePosixPath(str(relative_path))
    if not _CALENDAR_RESULT_PATH.fullmatch(candidate.as_posix()):
        return None
    try:
        root = canonical_workspace_root(workspace_root or settings.workspace_dir)
    except (OSError, TypeError, ValueError):
        return None
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow | cloexec
    parent_fd: int | None = None
    try:
        parent_fd = os.open(root, directory_flags)
        for component in candidate.parts[:-1]:
            if create_parents:
                try:
                    os.mkdir(component, 0o700, dir_fd=parent_fd)
                except FileExistsError:
                    pass
            next_fd = os.open(component, directory_flags, dir_fd=parent_fd)
            os.close(parent_fd)
            parent_fd = next_fd
        if parent_fd is None or not stat.S_ISDIR(os.fstat(parent_fd).st_mode):
            return None
        result = parent_fd
        parent_fd = None
        return result
    except (OSError, TypeError, ValueError):
        return None
    finally:
        if parent_fd is not None:
            try:
                os.close(parent_fd)
            except OSError:
                pass


def _open_calendar_result_descriptor(
    relative_path: str,
    *,
    workspace_root: str | Path | None = None,
) -> int | None:
    parent_fd = _open_calendar_result_parent(relative_path, workspace_root=workspace_root)
    if parent_fd is None:
        return None
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(
            PurePosixPath(relative_path).name,
            os.O_RDONLY | nofollow | cloexec,
            dir_fd=parent_fd,
        )
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
            os.close(descriptor)
            return None
        return descriptor
    except (OSError, TypeError, ValueError):
        return None
    finally:
        try:
            os.close(parent_fd)
        except OSError:
            pass


def write_calendar_result_bytes(
    relative_path: str,
    payload: bytes,
    *,
    workspace_root: str | Path | None = None,
) -> None:
    """Atomically publish a bounded calendar result under held no-follow fds."""

    if not isinstance(payload, bytes) or not 0 < len(payload) <= MAX_CALENDAR_RESULT_BYTES:
        raise OSError("calendar artifact payload is invalid")
    parent_fd = _open_calendar_result_parent(
        relative_path,
        workspace_root=workspace_root,
        create_parents=True,
    )
    if parent_fd is None:
        raise OSError("calendar artifact directory is unavailable")
    final_name = PurePosixPath(relative_path).name
    # A deterministic temporary name lets concurrent idempotent publishers
    # unlink one another's in-progress file during loser cleanup.
    temporary_name = f".{final_name}.{uuid.uuid4().hex}.tmp"
    temp_fd: int | None = None
    published = False
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    try:
        temp_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow | cloexec,
            0o600,
            dir_fd=parent_fd,
        )
        view = memoryview(payload)
        while view:
            written = os.write(temp_fd, view)
            if written <= 0:
                raise OSError("calendar artifact write made no progress")
            view = view[written:]
        os.fchmod(temp_fd, 0o600)
        os.fsync(temp_fd)
        os.close(temp_fd)
        temp_fd = None
        os.replace(temporary_name, final_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        published = True
        os.fsync(parent_fd)
    finally:
        if temp_fd is not None:
            try:
                os.close(temp_fd)
            except OSError:
                pass
        if not published:
            try:
                os.unlink(temporary_name, dir_fd=parent_fd)
            except OSError:
                pass
        try:
            os.close(parent_fd)
        except OSError:
            pass


def read_calendar_result_bytes(
    relative_path: str,
    *,
    workspace_root: str | Path | None = None,
    max_bytes: int = MAX_CALENDAR_RESULT_BYTES,
) -> bytes | None:
    """Read one canonical prep result with a bounded, inode-stable descriptor."""

    if type(max_bytes) is not int or max_bytes < 1:
        return None
    descriptor = _open_calendar_result_descriptor(relative_path, workspace_root=workspace_root)
    if descriptor is None:
        return None
    try:
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                return None
            payload = handle.read(max_bytes + 1)
            after = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(after.st_mode)
                or after.st_nlink != 1
                or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or before.st_ctime_ns != after.st_ctime_ns
            ):
                return None
        return payload if len(payload) <= max_bytes else None
    except (OSError, ValueError):
        try:
            os.close(descriptor)
        except OSError:
            pass
        return None


def _bounded_text(value: Any, *, limit: int, field: str, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise CalendarIntegrationError("calendar_provider_schema_invalid", f"Calendar {field} is malformed", status_code=502)
    normalized = value.strip()
    if not normalized or len(normalized) > limit or _CONTROL.search(normalized):
        raise CalendarIntegrationError("calendar_provider_schema_invalid", f"Calendar {field} is malformed", status_code=502)
    return normalized


def _validate_json_content_type(response: Any) -> None:
    """Require an explicit bounded JSON media type on every provider response."""

    headers = getattr(response, "headers", None)
    if headers is None:
        raise CalendarIntegrationError(
            "calendar_provider_schema_invalid",
            "Calendar provider response headers are missing",
            status_code=502,
        )
    try:
        content_type = next(
            (str(value) for key, value in headers.items() if str(key).lower() == "content-type"),
            None,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise CalendarIntegrationError(
            "calendar_provider_schema_invalid",
            "Calendar provider response content type is invalid",
            status_code=502,
        ) from exc
    if (
        not content_type
        or len(content_type) > 128
        or _CONTROL.search(content_type)
        or _JSON_CONTENT_TYPE.fullmatch(content_type) is None
    ):
        raise CalendarIntegrationError(
            "calendar_provider_schema_invalid",
            "Calendar provider response content type is invalid",
            status_code=502,
        )


def _calendar_segment(value: str, *, field: str) -> str:
    """Validate one provider identity and quote it exactly once."""
    raw = _bounded_text(value, limit=1024, field=field) or ""
    if raw in {".", ".."} or any(char in raw for char in "/\\?%"):
        raise CalendarIntegrationError("calendar_identity_invalid", f"Calendar {field} is not a supported provider identity", status_code=422)
    encoded = quote(raw, safe="")
    if not encoded or "/" in encoded or "%2F" in encoded.upper():
        raise CalendarIntegrationError("calendar_identity_invalid", f"Calendar {field} is not a supported provider identity", status_code=422)
    return encoded


def _fixed_url(origin: str, path: str, query: list[tuple[str, str]] | None = None) -> str:
    url = f"{origin}{path}"
    if query:
        url += "?" + urlencode(query, doseq=True)
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in {"www.googleapis.com", "oauth2.googleapis.com"} or parsed.port not in {None, 443}:
        raise CalendarIntegrationError("calendar_url_invalid", "Calendar provider URL is not allowed", status_code=422)
    if parsed.fragment or _CONTROL.search(url):
        raise CalendarIntegrationError("calendar_url_invalid", "Calendar provider URL is not allowed", status_code=422)
    return url


def _iso(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise CalendarIntegrationError("calendar_provider_schema_invalid", f"Calendar {field} is malformed", status_code=502)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CalendarIntegrationError("calendar_provider_schema_invalid", f"Calendar {field} is malformed", status_code=502) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CalendarIntegrationError("calendar_provider_schema_invalid", f"Calendar {field} has no timezone", status_code=502)
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _event_time(event: Mapping[str, Any], name: str) -> str | None:
    value = event.get(name)
    if not isinstance(value, Mapping):
        return None
    date_time = value.get("dateTime")
    if date_time is None:
        date_only = value.get("date")
        if isinstance(date_only, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_only):
            try:
                date.fromisoformat(date_only)
            except ValueError as exc:
                raise CalendarIntegrationError("calendar_event_time_invalid", f"Calendar {name} is malformed", status_code=502) from exc
            return date_only
        return None
    return _iso(date_time, field=name)


def _recurrence_identity(event: Mapping[str, Any]) -> str:
    recurring = event.get("recurringEventId")
    original = event.get("originalStartTime")
    if recurring is not None:
        if not isinstance(recurring, str) or not recurring or not isinstance(original, Mapping):
            raise CalendarIntegrationError("calendar_provider_schema_invalid", "Calendar recurrence identity is malformed", status_code=502)
        original_value = original.get("dateTime") or original.get("date")
        if not isinstance(original_value, str) or not original_value:
            raise CalendarIntegrationError("calendar_provider_schema_invalid", "Calendar recurrence identity is malformed", status_code=502)
        return f"{recurring}:{original_value}"
    return "single"


def canonical_event_key(owner_principal_id: str, connection_id: str, calendar_id: str, event: Mapping[str, Any]) -> str:
    provider_event_id = _bounded_text(event.get("id"), limit=1024, field="event id") or ""
    recurrence = _recurrence_identity(event)
    return "sha256:" + digest((owner_principal_id, connection_id, calendar_id, provider_event_id, recurrence))


def _selected_event(event: Mapping[str, Any], *, allowed_fields: set[str]) -> dict[str, Any]:
    event_id = _bounded_text(event.get("id"), limit=1024, field="event id")
    if event_id is None:
        raise CalendarIntegrationError("calendar_provider_schema_invalid", "Calendar event identity is missing", status_code=502)
    start = _event_time(event, "start")
    end = _event_time(event, "end")
    if not start or not end:
        raise CalendarIntegrationError("calendar_event_time_invalid", "Calendar event time is unavailable", status_code=502)
    start_is_date = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", start))
    end_is_date = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", end))
    if start_is_date != end_is_date:
        raise CalendarIntegrationError("calendar_event_time_invalid", "Calendar event start and end use different time kinds", status_code=502)
    if start_is_date:
        if date.fromisoformat(end) <= date.fromisoformat(start):
            raise CalendarIntegrationError("calendar_event_time_invalid", "Calendar event end is not after start", status_code=502)
    else:
        if datetime.fromisoformat(end.replace("Z", "+00:00")) <= datetime.fromisoformat(start.replace("Z", "+00:00")):
            raise CalendarIntegrationError("calendar_event_time_invalid", "Calendar event end is not after start", status_code=502)
    result: dict[str, Any] = {
        "provider_event_id": event_id,
        "recurrence_identity": _recurrence_identity(event),
        "summary": _bounded_text(event.get("summary") or "Untitled event", limit=1200, field="summary") or "Untitled event",
        "start": start,
        "end": end,
        "location": _bounded_text(event.get("location"), limit=1200, field="location", nullable=True) if "location" in allowed_fields else None,
        "description": _bounded_text(event.get("description"), limit=4000, field="description", nullable=True) if "description" in allowed_fields else None,
        "attendees": None,
        "etag": _bounded_text(event.get("etag"), limit=256, field="etag", nullable=True),
        "updated": _bounded_text(event.get("updated"), limit=128, field="updated", nullable=True),
        "status": _bounded_text(event.get("status") or "confirmed", limit=32, field="status") or "confirmed",
    }
    if "attendees" in allowed_fields:
        attendees = event.get("attendees") or []
        if not isinstance(attendees, list) or len(attendees) > 50:
            raise CalendarIntegrationError("calendar_provider_schema_invalid", "Calendar attendees are malformed", status_code=502)
        attendee_values: list[str] = []
        for item in attendees:
            if not isinstance(item, Mapping):
                raise CalendarIntegrationError(
                    "calendar_provider_schema_invalid",
                    "Calendar attendees are malformed",
                    status_code=502,
                )
            label = item.get("displayName") or item.get("email")
            if label is None:
                raise CalendarIntegrationError(
                    "calendar_provider_schema_invalid",
                    "Calendar attendees are malformed",
                    status_code=502,
                )
            attendee_values.append(_bounded_text(label, limit=200, field="attendee") or "")
        result["attendees"] = attendee_values[:50]
    return result


def event_revision(selected: Mapping[str, Any]) -> str:
    return "sha256:" + digest({
        "provider_event_id": selected.get("provider_event_id"),
        "recurrence_identity": selected.get("recurrence_identity"),
        "selected": {key: selected.get(key) for key in ("summary", "start", "end", "location", "description", "attendees")},
        "etag": selected.get("etag"),
        "updated": selected.get("updated"),
        "status": selected.get("status"),
    })


def calendar_job_id(owner_principal_id: str, task_id: str, attempt_id: str) -> str:
    value = uuid.uuid5(uuid.NAMESPACE_URL, f"seraph:calendar-prep:{owner_principal_id}:{task_id}:{attempt_id}")
    return f"calendar-prep:{value.hex}"


def calendar_input_payload(inputs: Mapping[str, Any], *, parent_handoff: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return the one canonical durable input envelope for Calendar prep."""

    return {"input": dict(inputs), "parent_handoff": dict(parent_handoff or {})}


def calendar_input_digest(inputs: Mapping[str, Any], *, parent_handoff: Mapping[str, Any] | None = None) -> str:
    return digest(calendar_input_payload(inputs, parent_handoff=parent_handoff))


def calendar_authority(
    *,
    task: Any,
    attempt: Any,
    executor_id: str | None = None,
    priority: int | None = None,
) -> dict[str, Any]:
    return {
        "principal": str(task.owner_principal_id),
        "owner_kind": "user",
        "service_id": None,
        "session_id": str(task.owner_session_id),
        "operator_session_id": str(task.owner_session_id),
        "goal_id": str(task.goal_id),
        "goal_revision": int(task.goal_revision),
        "capability_id": "calendar.meeting-prep.v1",
        "capability_version": "1",
        "executor_id": str(executor_id or task.executor_id or ""),
        "priority": int(task.priority if priority is None else priority),
        "attempt_id": str(attempt.attempt_id),
        "finite_authority": True,
        "runtime_cap": 180,
    }


def calendar_authority_digest(*, task: Any, attempt: Any, executor_id: str | None = None, priority: int | None = None) -> str:
    return digest(calendar_authority(task=task, attempt=attempt, executor_id=executor_id, priority=priority))


def refresh_event_binding_from_snapshot(
    binding: Any,
    snapshot: Any,
    *,
    connection_id: str,
    connection_revision: int,
    consent_id: str,
    consent_revision: int,
    observed_at: datetime | None = None,
) -> bool:
    """Advance a selected event binding only when its authority changes.

    A repeated observation of an unchanged event must retain the original
    selection/list provenance and revision so queued work remains idempotent.
    A changed event or owner/consent/connection fence creates the next
    immutable binding revision and records the fresh snapshot provenance.
    """

    event_changed = (
        str(getattr(binding, "event_key", "") or "") != str(getattr(snapshot, "event_key", "") or "")
        or str(getattr(binding, "event_revision", "") or "")
        != str(getattr(snapshot, "event_revision", "") or "")
    )
    authority_changed = (
        str(getattr(binding, "connection_id", "") or "") != str(connection_id)
        or int(getattr(binding, "connection_revision", 0) or 0) != int(connection_revision)
        or str(getattr(binding, "consent_id", "") or "") != str(consent_id)
        or int(getattr(binding, "consent_revision", 0) or 0) != int(consent_revision)
    )
    changed = event_changed or authority_changed
    if changed:
        binding.connection_id = str(connection_id)
        binding.connection_revision = int(connection_revision)
        binding.consent_id = str(consent_id)
        binding.consent_revision = int(consent_revision)
        binding.event_key = str(getattr(snapshot, "event_key", "") or "")
        binding.event_revision = str(getattr(snapshot, "event_revision", "") or "")
        binding.calendar_list_revision = str(getattr(snapshot, "calendar_list_revision", "") or "")
        binding.revision = int(getattr(binding, "revision", 0) or 0) + 1
    # The selection/list revision is intentionally left alone for an
    # unchanged event, while the latest bounded observation remains available
    # for expiry/readback diagnostics.
    if hasattr(binding, "snapshot_digest"):
        binding.snapshot_digest = digest(getattr(snapshot, "fields", {}) or {})
    if hasattr(binding, "fetched_at"):
        binding.fetched_at = getattr(snapshot, "fetched_at", None) or observed_at or datetime.now(timezone.utc)
    binding.updated_at = observed_at or datetime.now(timezone.utc)
    return changed


async def persist_calendar_event_binding(
    db: Any,
    *,
    owner_principal_id: str,
    owner_session_id: str,
    connection: Any,
    consent: Any,
    snapshot: "CalendarEventSnapshot",
) -> CalendarEventBinding:
    """Persist one owner-bound event selection in the caller's transaction.

    The provider identity digest is the sole lookup key for a selected event,
    and includes the operator, connection, calendar, provider event, and
    recurrence identity.  The encrypted identity columns are retained only so
    the already-authorized durable job can perform its later provider read;
    this helper never returns provider identifiers through a generic payload.
    """

    principal = str(owner_principal_id or "")
    session = str(owner_session_id or "")
    if not principal or not session:
        raise CalendarIntegrationError(
            "calendar_binding_owner_invalid",
            "Calendar event binding ownership is unavailable",
            status_code=403,
        )
    if (
        str(getattr(connection, "owner_principal_id", "") or "") != principal
        or str(getattr(connection, "owner_session_id", "") or "") != session
        or str(getattr(consent, "owner_principal_id", "") or "") != principal
        or str(getattr(consent, "owner_session_id", "") or "") != session
        or str(getattr(consent, "connection_id", "") or "") != str(getattr(connection, "connection_id", "") or "")
    ):
        raise CalendarIntegrationError(
            "calendar_binding_owner_mismatch",
            "Calendar event binding ownership is invalid",
            status_code=403,
        )
    try:
        calendar_id = decrypt(str(getattr(consent, "calendar_id", "") or ""))
    except Exception as exc:
        raise CalendarIntegrationError(
            "calendar_binding_unavailable",
            "Calendar event binding identity is unavailable",
            status_code=409,
            recovery_action="restore_prerequisite",
        ) from exc
    connection_id = str(getattr(connection, "connection_id", "") or "")
    consent_id = str(getattr(consent, "consent_id", "") or "")
    if not connection_id or not consent_id:
        raise CalendarIntegrationError(
            "calendar_binding_identity_invalid",
            "Calendar event binding identity is unavailable",
            status_code=409,
            recovery_action="refresh_event",
        )
    provider_event_id = _bounded_text(
        getattr(snapshot, "provider_event_id", None), limit=1024, field="event id"
    ) or ""
    recurrence_identity = _bounded_text(
        getattr(snapshot, "recurrence_identity", None), limit=1024, field="recurrence identity"
    ) or ""
    provider_identity = (principal, connection_id, calendar_id, provider_event_id, recurrence_identity)
    provider_digest = digest(provider_identity)
    event_key = str(getattr(snapshot, "event_key", "") or "")
    event_revision = str(getattr(snapshot, "event_revision", "") or "")
    calendar_list_revision = str(getattr(snapshot, "calendar_list_revision", "") or "")
    if (
        event_key != "sha256:" + digest(provider_identity)
        or not _DIGEST.fullmatch(event_revision.removeprefix("sha256:"))
        or not _DIGEST.fullmatch(calendar_list_revision.removeprefix("sha256:"))
    ):
        raise CalendarIntegrationError(
            "calendar_binding_identity_invalid",
            "Calendar event binding identity is invalid",
            status_code=409,
            recovery_action="refresh_event",
        )
    existing = (
        await db.execute(
            select(CalendarEventBinding).where(
                CalendarEventBinding.owner_principal_id == principal,
                CalendarEventBinding.owner_session_id == session,
                CalendarEventBinding.connection_id == connection_id,
                CalendarEventBinding.provider_identity_digest == provider_digest,
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        candidate = CalendarEventBinding(
            owner_principal_id=principal,
            owner_session_id=session,
            connection_id=connection_id,
            connection_revision=int(getattr(connection, "revision", 0) or 0),
            consent_id=consent_id,
            consent_revision=int(getattr(consent, "revision", 0) or 0),
            calendar_id_private=encrypt(calendar_id),
            provider_event_id_private=encrypt(provider_event_id),
            recurrence_identity_private=encrypt(recurrence_identity),
            provider_identity_digest=provider_digest,
            event_key=event_key,
            event_revision=event_revision,
            calendar_list_revision=calendar_list_revision,
            fetched_at=getattr(snapshot, "fetched_at", None) or datetime.now(timezone.utc),
            state="selected",
            revision=1,
            snapshot_digest=digest(getattr(snapshot, "fields", {}) or {}),
        )
        dialect_name = getattr(getattr(db, "bind", None), "dialect", None)
        dialect_name = getattr(dialect_name, "name", "")
        if dialect_name == "sqlite":
            values = {
                column.name: getattr(candidate, column.name)
                for column in CalendarEventBinding.__table__.columns
            }
            await db.execute(
                sqlite_insert(CalendarEventBinding)
                .values(values)
                .on_conflict_do_nothing(
                    index_elements=[
                        CalendarEventBinding.owner_principal_id,
                        CalendarEventBinding.owner_session_id,
                        CalendarEventBinding.connection_id,
                        CalendarEventBinding.provider_identity_digest,
                    ]
                )
            )
            existing = (
                await db.execute(
                    select(CalendarEventBinding).where(
                        CalendarEventBinding.owner_principal_id == principal,
                        CalendarEventBinding.owner_session_id == session,
                        CalendarEventBinding.connection_id == connection_id,
                        CalendarEventBinding.provider_identity_digest == provider_digest,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                raise CalendarIntegrationError(
                    "calendar_binding_conflict",
                    "Calendar event binding could not be persisted",
                    status_code=409,
                    recovery_action="refresh_event",
                )
        else:
            db.add(candidate)
            await db.flush()
            existing = candidate
    else:
        refresh_event_binding_from_snapshot(
            existing,
            snapshot,
            connection_id=connection_id,
            connection_revision=int(getattr(connection, "revision", 0) or 0),
            consent_id=consent_id,
            consent_revision=int(getattr(consent, "revision", 0) or 0),
            observed_at=getattr(snapshot, "fetched_at", None),
        )
    await db.flush()
    return existing


@dataclass(frozen=True)
class CalendarEventSnapshot:
    event_key: str
    event_revision: str
    calendar_list_revision: str
    provider_event_id: str
    recurrence_identity: str
    fields: dict[str, Any]
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class CalendarListRevision:
    digest: str
    pages_read: int
    truncated: bool


class GoogleCalendarReadonlyAdapter:
    """Bounded Google REST adapter using the shared pinned transport."""

    def __init__(
        self,
        connection: GoogleServiceConnectionRow,
        *,
        owner_principal_id: str,
        transport: Any = None,
        resolver: Any = None,
        authority_check: Callable[[], Awaitable[None]] | None = None,
        contact_observer: Callable[[], None] | None = None,
    ):
        self.connection = connection
        self.owner_principal_id = owner_principal_id
        self.transport = transport
        self.resolver = resolver
        self.authority_check = authority_check
        self.contact_observer = contact_observer
        self._access_token: str | None = None
        self._credential_values: tuple[str, ...] = ()
        self._transport_lifecycle = _TransportLifecycleMarker()

    def transport_quiescence(self) -> dict[str, int | str]:
        """Return server-owned proof of Calendar read transport settlement.

        The projection contains counters only.  ``verified`` means every
        request started by this adapter either had its real HTTPX client
        closed successfully or failed before an HTTPX client could exist.
        An unresolved timeout, cancellation, or failed close remains
        ``unknown`` and must stay quarantined by the durable caller.
        """

        return self._transport_lifecycle.snapshot()

    @property
    def active_operations(self) -> int:
        """Current number of Calendar transport operations still active."""

        return int(self._transport_lifecycle.snapshot()["active_operations"])

    @property
    def unsettled_operations(self) -> int:
        """Current number of Calendar operations without close proof."""

        return int(self._transport_lifecycle.snapshot()["unsettled_operations"])

    async def _check_authority(self) -> None:
        if self.authority_check is not None:
            await self.authority_check()

    def _mark_contact(self) -> None:
        """Record that a provider request is about to cross the trust boundary."""

        if self.contact_observer is not None:
            self.contact_observer()

    async def _credentials(self) -> dict[str, str]:
        if self.connection.state != "active":
            raise CalendarIntegrationError("calendar_connection_unavailable", "The Calendar connection is not active", status_code=409, recovery_action="restore_prerequisite")
        await self._check_authority()
        raw = await vault_repository.get(self.connection.vault_secret_key)
        if not raw:
            raise CalendarIntegrationError("credential_unavailable", "The Calendar credential is unavailable", status_code=409, recovery_action="restore_prerequisite")
        try:
            values = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise CalendarIntegrationError("credential_unavailable", "The Calendar credential is unavailable", status_code=409) from exc
        if not isinstance(values, dict):
            raise CalendarIntegrationError("credential_unavailable", "The Calendar credential is unavailable", status_code=409)
        required = {key: values.get(key) for key in ("client_id", "refresh_token")}
        if any(not isinstance(value, str) or not value for value in required.values()):
            raise CalendarIntegrationError("credential_unavailable", "The Calendar credential is unavailable", status_code=409)
        if any(_CONTROL.search(value) for value in required.values()):
            raise CalendarIntegrationError("credential_unavailable", "The Calendar credential is unavailable", status_code=409)
        if "client_secret" in values and values["client_secret"] is not None and not isinstance(values["client_secret"], str):
            raise CalendarIntegrationError("credential_unavailable", "The Calendar credential is unavailable", status_code=409)
        if isinstance(values.get("client_secret"), str) and _CONTROL.search(values["client_secret"]):
            raise CalendarIntegrationError("credential_unavailable", "The Calendar credential is unavailable", status_code=409)
        result = {key: value for key, value in values.items() if key in {"client_id", "refresh_token", "client_secret"} and isinstance(value, str)}
        self._credential_values = tuple(value for value in result.values() if value)
        return result

    def _scrub(self, value: Any) -> Any:
        if isinstance(value, str):
            result = value
            for secret in self._credential_values:
                if secret:
                    result = result.replace(secret, "[redacted]")
            return result
        if isinstance(value, list):
            return [self._scrub(item) for item in value[:50]]
        if isinstance(value, dict):
            return {str(key): self._scrub(item) for key, item in value.items()}
        return value

    async def _token(self) -> str:
        if self._access_token:
            return self._access_token
        credentials = await self._credentials()
        form: list[tuple[str, str]] = [("grant_type", "refresh_token"), ("client_id", credentials["client_id"]), ("refresh_token", credentials["refresh_token"])]
        if credentials.get("client_secret"):
            form.append(("client_secret", credentials["client_secret"]))
        await self._check_authority()
        self._mark_contact()
        try:
            response = await request_pinned_https(
                _fixed_url(GOOGLE_TOKEN_ORIGIN, "/token"),
                method="POST",
                headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
                form_body=urlencode(form).encode("utf-8"),
                resolver=self.resolver or default_resolver,
                transport=self.transport,
                timeout_seconds=10,
                max_bytes=MAX_PROVIDER_RESPONSE_BYTES,
                _lifecycle_marker=self._transport_lifecycle,
                authority_check=self._check_authority,
            )
        except (PinnedTransportError, TimeoutError, OSError, RuntimeError, httpx.HTTPError) as exc:
            raise CalendarIntegrationError(
                "calendar_provider_unavailable",
                "Calendar authorization endpoint is unavailable",
                status_code=503,
                recovery_action="retry",
            ) from exc
        await self._check_authority()
        if response.status_code != 200:
            raise CalendarIntegrationError("calendar_token_refresh_failed", "Calendar authorization could not be refreshed", status_code=502, recovery_action="restore_prerequisite")
        _validate_json_content_type(response)
        try:
            body = json.loads(response.content.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise CalendarIntegrationError("calendar_token_refresh_failed", "Calendar authorization response is invalid", status_code=502) from exc
        token = body.get("access_token") if isinstance(body, Mapping) else None
        if not isinstance(token, str) or not token or _CONTROL.search(token):
            raise CalendarIntegrationError("calendar_token_refresh_failed", "Calendar authorization response is invalid", status_code=502)
        self._access_token = token
        self._credential_values = tuple((*self._credential_values, token))
        return token

    async def _get(self, url: str) -> dict[str, Any]:
        await self._check_authority()
        self._mark_contact()
        try:
            response = await request_pinned_https(
                url,
                headers={"Accept": "application/json"},
                resolver=self.resolver or default_resolver,
                transport=self.transport,
                timeout_seconds=10,
                max_bytes=MAX_PROVIDER_RESPONSE_BYTES,
                _lifecycle_marker=self._transport_lifecycle,
                authority_check=self._check_authority,
            )
        except (PinnedTransportError, TimeoutError, OSError, RuntimeError, httpx.HTTPError) as exc:
            raise CalendarIntegrationError("calendar_provider_unavailable", "Calendar provider read is unavailable", status_code=503, recovery_action="retry") from exc
        await self._check_authority()
        if response.status_code in {401, 403}:
            raise CalendarIntegrationError("calendar_provider_unauthorized", "Calendar authorization was refused", status_code=403, recovery_action="restore_prerequisite")
        if response.status_code != 200:
            raise CalendarIntegrationError("calendar_provider_read_failed", "Calendar provider read failed", status_code=502, recovery_action="retry")
        _validate_json_content_type(response)
        try:
            value = json.loads(response.content.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise CalendarIntegrationError("calendar_provider_schema_invalid", "Calendar provider response is invalid", status_code=502) from exc
        if not isinstance(value, dict):
            raise CalendarIntegrationError("calendar_provider_schema_invalid", "Calendar provider response is invalid", status_code=502)
        return self._scrub(value)

    async def _authorized_get(self, url: str) -> dict[str, Any]:
        token = await self._token()
        # The shared test transport sees the authorization header; the value is
        # never persisted or returned by this module.
        await self._check_authority()
        self._mark_contact()
        try:
            response = await request_pinned_https(
                url,
                headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
                resolver=self.resolver or default_resolver,
                transport=self.transport,
                timeout_seconds=10,
                max_bytes=MAX_PROVIDER_RESPONSE_BYTES,
                _lifecycle_marker=self._transport_lifecycle,
                authority_check=self._check_authority,
            )
        except (PinnedTransportError, TimeoutError, OSError, RuntimeError, httpx.HTTPError) as exc:
            raise CalendarIntegrationError("calendar_provider_unavailable", "Calendar provider read is unavailable", status_code=503, recovery_action="retry") from exc
        await self._check_authority()
        if response.status_code in {401, 403}:
            raise CalendarIntegrationError("calendar_provider_unauthorized", "Calendar authorization was refused", status_code=403, recovery_action="restore_prerequisite")
        if response.status_code != 200:
            raise CalendarIntegrationError("calendar_provider_read_failed", "Calendar provider read failed", status_code=502, recovery_action="retry")
        _validate_json_content_type(response)
        try:
            value = json.loads(response.content.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise CalendarIntegrationError("calendar_provider_schema_invalid", "Calendar provider response is invalid", status_code=502) from exc
        if not isinstance(value, dict):
            raise CalendarIntegrationError("calendar_provider_schema_invalid", "Calendar provider response is invalid", status_code=502)
        return self._scrub(value)

    async def list_calendars(self) -> tuple[list[dict[str, str]], CalendarListRevision]:
        payload = await self._authorized_get(_fixed_url(GOOGLE_API_ORIGIN, CALENDAR_LIST_PATH, [("maxResults", "50")]))
        items = payload.get("items")
        if not isinstance(items, list):
            raise CalendarIntegrationError("calendar_provider_schema_invalid", "Calendar list response is invalid", status_code=502)
        calendars: list[dict[str, str]] = []
        for item in items[:MAX_CALENDARS]:
            if not isinstance(item, Mapping):
                raise CalendarIntegrationError("calendar_provider_schema_invalid", "Calendar list response is invalid", status_code=502)
            calendar_id = _bounded_text(item.get("id"), limit=1024, field="calendar id") or ""
            summary = _bounded_text(item.get("summary") or item.get("summaryOverride") or calendar_id, limit=500, field="calendar summary") or calendar_id
            calendars.append({"calendar_id": calendar_id, "summary": summary})
        revision = CalendarListRevision(
            digest="sha256:" + digest({"request": CALENDAR_LIST_PATH, "items": [(item["calendar_id"], item["summary"]) for item in calendars], "etag": payload.get("etag"), "updated": payload.get("updated")}),
            pages_read=1,
            truncated=len(items) > MAX_CALENDARS,
        )
        return calendars, revision

    async def list_events(
        self,
        calendar_id: str,
        *,
        time_min: datetime,
        time_max: datetime,
        allowed_fields: set[str] | None = None,
        max_events: int = MAX_EVENTS,
    ) -> tuple[list[CalendarEventSnapshot], CalendarListRevision]:
        encoded_calendar = _calendar_segment(calendar_id, field="calendar id")
        max_events = max(1, min(int(max_events), MAX_EVENTS))
        fields = allowed_fields or {"summary", "start", "end", "location"}
        items: list[Mapping[str, Any]] = []
        pages = 0
        truncated = False
        page_token: str | None = None
        request_identity: list[dict[str, Any]] = []
        aggregate_bytes = 0
        while pages < MAX_PAGES and len(items) < max_events:
            query: list[tuple[str, str]] = [
                ("timeMin", _utc(time_min).isoformat().replace("+00:00", "Z")),
                ("timeMax", _utc(time_max).isoformat().replace("+00:00", "Z")),
                ("singleEvents", "true"),
                ("orderBy", "startTime"),
                ("maxResults", str(min(MAX_EVENTS, max_events - len(items)))),
            ]
            if page_token:
                if len(page_token.encode("utf-8")) > 2048 or _CONTROL.search(page_token):
                    raise CalendarIntegrationError("calendar_provider_schema_invalid", "Calendar pagination is invalid", status_code=502)
                query.append(("pageToken", page_token))
            url = _fixed_url(GOOGLE_API_ORIGIN, f"{EVENTS_PATH}/{encoded_calendar}/events", query)
            payload = await self._authorized_get(url)
            pages += 1
            raw_items = payload.get("items")
            if not isinstance(raw_items, list):
                raise CalendarIntegrationError("calendar_provider_schema_invalid", "Calendar event response is invalid", status_code=502)
            page_bytes = len(_canonical(payload))
            if aggregate_bytes + page_bytes > MAX_PROVIDER_RESPONSE_BYTES:
                truncated = True
                break
            aggregate_bytes += page_bytes
            request_identity.append({"etag": payload.get("etag"), "updated": payload.get("updated"), "count": len(raw_items)})
            page_token_value = payload.get("nextPageToken")
            if page_token_value is not None and not isinstance(page_token_value, str):
                raise CalendarIntegrationError("calendar_provider_schema_invalid", "Calendar pagination is invalid", status_code=502)
            remaining = max_events - len(items)
            for item in raw_items:
                if not isinstance(item, Mapping):
                    raise CalendarIntegrationError("calendar_provider_schema_invalid", "Calendar event response is invalid", status_code=502)
                items.append(item)
                if len(items) >= max_events:
                    break
            if len(raw_items) > remaining:
                truncated = True
            if len(items) >= max_events:
                truncated = truncated or bool(page_token_value)
                break
            if not page_token_value:
                break
            if len(page_token_value.encode("utf-8")) > 2048 or _CONTROL.search(page_token_value):
                raise CalendarIntegrationError("calendar_provider_schema_invalid", "Calendar pagination is invalid", status_code=502)
            page_token = page_token_value
        if page_token and pages >= MAX_PAGES:
            truncated = True
        selected: list[CalendarEventSnapshot] = []
        seen: set[str] = set()
        list_revision = "sha256:" + digest({"calendar_id": calendar_id, "pages": request_identity, "items": [(item.get("id"), _recurrence_identity(item), item.get("etag"), item.get("updated")) for item in items]})
        for item in items:
            # Provider-controlled text is untrusted data.  Scrub all active
            # credential values before a snapshot can cross into scheduler
            # titles, model prompts, task bodies, or durable artifacts.
            projection = self._scrub(_selected_event(item, allowed_fields=fields))
            key = canonical_event_key(self.owner_principal_id, self.connection.connection_id, calendar_id, item)
            if key in seen:
                raise CalendarIntegrationError("calendar_duplicate_event_identity", "Calendar returned duplicate event identities", status_code=502)
            seen.add(key)
            selected.append(CalendarEventSnapshot(
                event_key=key,
                event_revision=event_revision(projection),
                calendar_list_revision=list_revision,
                provider_event_id=projection.pop("provider_event_id"),
                recurrence_identity=projection.pop("recurrence_identity"),
                fields=projection,
            ))
        return selected, CalendarListRevision(digest=list_revision, pages_read=pages, truncated=truncated)

    async def get_event(self, calendar_id: str, provider_event_id: str, *, allowed_fields: set[str] | None = None) -> dict[str, Any]:
        path = f"{EVENTS_PATH}/{_calendar_segment(calendar_id, field='calendar id')}/events/{_calendar_segment(provider_event_id, field='event id')}"
        return await self._authorized_get(_fixed_url(GOOGLE_API_ORIGIN, path))


@dataclass
class MeetingPrepService:
    """Pure bounded two-read preparation helper used by the direct adapter."""

    adapter: GoogleCalendarReadonlyAdapter

    @staticmethod
    def validate_model_output(value: Any, *, event_key: str, event_revision: str) -> dict[str, Any]:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError as exc:
                raise CalendarIntegrationError("calendar_model_output_invalid", "Calendar preparation output is invalid", status_code=502) from exc
        if not isinstance(value, Mapping) or set(value) != {"schema_version", "event_key", "event_revision", "summary", "agenda", "questions", "risks", "preparation_steps"}:
            raise CalendarIntegrationError("calendar_model_output_invalid", "Calendar preparation output is invalid", status_code=502)
        if type(value.get("schema_version")) is not int or value.get("schema_version") != 1 or value.get("event_key") != event_key or value.get("event_revision") != event_revision:
            raise CalendarIntegrationError("calendar_model_output_invalid", "Calendar preparation output is bound to another event", status_code=502)
        output: dict[str, Any] = {"schema_version": 1, "event_key": event_key, "event_revision": event_revision}
        output["summary"] = _bounded_text(value.get("summary"), limit=1200, field="model summary")
        for key in ("agenda", "questions", "risks", "preparation_steps"):
            items = value.get(key)
            if not isinstance(items, list) or len(items) > 8:
                raise CalendarIntegrationError("calendar_model_output_invalid", "Calendar preparation output is invalid", status_code=502)
            output[key] = [_bounded_text(item, limit=400, field=f"model {key}") for item in items]
        if len(_canonical(output)) > 64 * 1024:
            raise CalendarIntegrationError("calendar_model_output_too_large", "Calendar preparation output exceeds the bounded limit", status_code=502)
        return output

    async def prepare(
        self,
        calendar_id: str,
        provider_event_id: str,
        *,
        allowed_fields: set[str],
        expected_event_key: str | None = None,
        expected_event_revision: str | None = None,
        before_boundary: Callable[[], Awaitable[None]] | None = None,
        model_call: Callable[[dict[str, Any]], Awaitable[Any]] | None = None,
    ) -> dict[str, Any]:
        if before_boundary is not None:
            await before_boundary()
        first = await self.adapter.get_event(calendar_id, provider_event_id, allowed_fields=allowed_fields)
        read_1_verified_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        first_selected = _selected_event(first, allowed_fields=allowed_fields)
        event_key = canonical_event_key(self.adapter.owner_principal_id, self.adapter.connection.connection_id, calendar_id, first)
        first_revision = event_revision(first_selected)
        if (
            (expected_event_key is not None and expected_event_key != event_key)
            or (expected_event_revision is not None and expected_event_revision != first_revision)
        ):
            raise CalendarIntegrationError(
                "calendar_event_revision_stale",
                "The Calendar event changed before preparation",
                status_code=409,
                recovery_action="refresh_event",
            )
        if before_boundary is not None:
            await before_boundary()
        prompt_fields = self.adapter._scrub({key: first_selected.get(key) for key in ("summary", "start", "end", "location", "description", "attendees") if key in allowed_fields or key in {"summary", "start", "end"}})
        if model_call is None:
            raise CalendarIntegrationError("calendar_model_unavailable", "The governed preparation route is unavailable", status_code=503, recovery_action="restore_prerequisite")
        proposed = await model_call({"event_key": event_key, "event_revision": first_revision, "event": prompt_fields})
        output = self.validate_model_output(proposed, event_key=event_key, event_revision=first_revision)
        # Provider credentials are process-local, but a compromised or
        # prompt-injected model could echo them.  Scrub before any durable
        # artifact/readback is created; no secret is silently accepted as a
        # successful preparation value.
        output = self.adapter._scrub(output)
        if before_boundary is not None:
            await before_boundary()
        second = await self.adapter.get_event(calendar_id, provider_event_id, allowed_fields=allowed_fields)
        read_2_verified_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        second_selected = _selected_event(second, allowed_fields=allowed_fields)
        second_revision = event_revision(second_selected)
        if canonical_event_key(self.adapter.owner_principal_id, self.adapter.connection.connection_id, calendar_id, second) != event_key or second_revision != first_revision:
            raise CalendarIntegrationError("stale_event_after_synthesis", "The Calendar event changed during preparation", status_code=409, recovery_action="retry")
        return {
            "event_key": event_key,
            "event_revision": first_revision,
            "read_1": {
                "status": "succeeded",
                "request_digest": "sha256:" + digest({"operation": "events.get", "calendar_id": calendar_id, "event_id": provider_event_id, "read": 1}),
                "response_digest": "sha256:" + digest(first_selected),
                "verified_at": read_1_verified_at,
            },
            "read_2": {
                "status": "succeeded",
                "request_digest": "sha256:" + digest({"operation": "events.get", "calendar_id": calendar_id, "event_id": provider_event_id, "read": 2}),
                "response_digest": "sha256:" + digest(second_selected),
                "verified_at": read_2_verified_at,
            },
            "output": output,
            "memory_status": "no_learning",
        }


__all__ = [
    "CalendarEventSnapshot",
    "CalendarIntegrationError",
    "CalendarListRevision",
    "GoogleCalendarReadonlyAdapter",
    "GoogleServiceConnection",
    "MeetingPrepService",
    "canonical_event_key",
    "calendar_artifact_path_for_job",
    "calendar_authority",
    "calendar_authority_digest",
    "persist_calendar_event_binding",
    "refresh_event_binding_from_snapshot",
    "calendar_input_digest",
    "calendar_input_payload",
    "calendar_job_id",
    "digest",
    "event_revision",
    "read_calendar_result_bytes",
    "write_calendar_result_bytes",
]
