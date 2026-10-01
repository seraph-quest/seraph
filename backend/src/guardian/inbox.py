"""Durable, owner-bound inbox for verified guardian source packets.

The inbox is a disposition projection over the existing source-watch packet
and WorkBoard contracts.  It never stores source prose and never admits an
executable job: accepting an item creates one owner-bound triage task in the
existing board transaction.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError

from src.artifacts.registry import artifact_id_for
from src.db import engine as db_engine
from src.db.models import (
    GoogleServiceConnection,
    Goal,
    GuardianDecisionPacket,
    GuardianInboxAction,
    GuardianInboxDisposition,
    GuardianSourceWatch,
    GovernedScheduleBinding,
    MailMessageBinding,
    MailReadConsent,
    MailWatchState,
    WorkflowRunState,
    WorkBoardTask,
    WorkBoardStatus,
)
from src.goals.repository import deserialize_admission_budget
from src.vault import redaction as vault_redaction
from src.work_board.contracts import WorkBoardOwner, WorkBoardTaskCreate
from src.work_board.repository import (
    BoardError,
    WorkBoardRepository,
    _begin_sqlite_immediate,
)


SOURCE_KIND = "source_packet"
MAIL_SOURCE_KIND = "mail_notice"
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SAFE_KEY = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
_MAX_CURSOR_BYTES = 512
_MAX_DISPLAY = 240
_REPAIR_LIMIT = 20
_ACTION_HISTORY_LIMIT = 20
_ACTION_HISTORY_QUERY_LIMIT = _ACTION_HISTORY_LIMIT + 1
_SAFE_ACTIONS = frozenset({"accept_followup", "snooze", "dismiss"})
_SAFE_ACTION_OUTCOMES = frozenset({"accepted", "snoozed", "dismissed"})


class InboxError(Exception):
    """Typed failure mapped by the inbox API to a stable response."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 409,
        recovery_action: str | None = None,
        current_revision: int | None = None,
        state: str | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        self.recovery_action = recovery_action
        self.current_revision = current_revision
        self.state = state
        super().__init__(message)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    normalized = _utc(value)
    return normalized.isoformat() if normalized is not None else None


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _parse_material_keys(packet: GuardianDecisionPacket) -> list[str]:
    try:
        value = json.loads(packet.material_source_keys_json or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(value, list):
        return []
    return [str(item)[:128] for item in value if isinstance(item, str) and item.strip()][:64]


def _parse_json_list(value: str | None) -> list[dict[str, Any]]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []


def _parse_verified_at(value: object) -> datetime | None:
    """Accept only an explicit, parseable UTC verification timestamp."""

    raw = str(value or "").strip()
    if not raw or len(raw) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return _utc(parsed)
    except (TypeError, ValueError):
        return None


def _job_has_verified_readbacks(
    run: WorkflowRunState | None,
    *,
    packet: GuardianDecisionPacket,
    watch: GuardianSourceWatch,
) -> bool:
    """Require the durable source job and both canonical readback receipts."""

    if run is None:
        return False
    if (
        str(run.status) != "succeeded"
        or str(run.job_kind) != "guardian_source_watch"
        or str(run.owner_kind) != "service"
        or str(run.owner_principal_id or "") != "service:guardian-source-watch"
        or str(run.goal_id or "") != str(packet.goal_id)
        or int(run.goal_revision or 0) != int(packet.goal_revision or 0)
        or int(run.plan_revision or 0) != int(packet.plan_revision or 0)
        or str(run.run_identity) != str(packet.run_identity)
        or bool(run.lease_owner)
        or run.lease_expires_at is not None
    ):
        return False
    effects = _parse_json_list(run.effect_receipts_json)
    if any(
        str(item.get("status") or "") in {"unknown", "unknown_external_effect", "cost_liability", "dispatched", "intent"}
        for item in effects
    ):
        return False
    expected_paths = _expected_artifact_paths(watch.id, packet.id)
    expected_digests = (packet.dossier_sha256, packet.task_sha256)
    for path, expected_digest in zip(expected_paths, expected_digests):
        safe_digest = _valid_digest(expected_digest)
        expected_readback_id = f"guardian_readback:{_digest(chr(0).join((str(packet.run_identity), path, safe_digest or '')))}"
        matching = [
            item
            for item in effects
            if str(item.get("receipt_kind") or "") == "readback"
            and str(item.get("target_path") or "") == path
            and str(item.get("status") or "") == "succeeded"
            and _valid_digest(item.get("content_sha256") or item.get("target_digest")) == safe_digest
            and isinstance(item.get("details"), dict)
            and item["details"].get("verified") is True
            and item["details"].get("output_exists") is True
            and item["details"].get("workspace_contained") is True
            and str(item.get("readback_id") or "") == expected_readback_id
            and _parse_verified_at(item.get("verified_at")) is not None
        ]
        if not matching:
            return False
    return True


def _valid_digest(value: object) -> str | None:
    candidate = str(value or "").strip().lower()
    return candidate if _DIGEST.fullmatch(candidate) else None


def _safe_id(value: object, *, max_length: int = 256) -> str | None:
    candidate = str(value or "").strip()
    if not candidate or len(candidate) > max_length or not _SAFE_KEY.fullmatch(candidate):
        return None
    return candidate


async def _safe_goal_label(goal: Goal | None) -> str:
    # Goal titles are deliberately left to the owner-bound goal surface.  A
    # redaction lookup here would turn a passive inbox read into a vault/audit
    # write, and copying mutable goal prose into this projection is unnecessary
    # for deciding whether the item is actionable.
    return "the watched goal"


def _expected_artifact_paths(watch_id: str, packet_id: str) -> tuple[str, str]:
    return (
        f"guardian/source-watches/{watch_id}/packets/{packet_id}.md",
        f"guardian/source-watches/{watch_id}/tasks/{packet_id}.md",
    )


def _artifact_metadata_status(
    packet: GuardianDecisionPacket | None,
    watch: GuardianSourceWatch | None,
    run: WorkflowRunState | None,
) -> tuple[str, list[dict[str, Any]]]:
    """Project cached packet/readback truth without reading artifact bytes."""

    refs: list[dict[str, Any]] = []
    if packet is None or watch is None:
        return "blocked", refs
    expected_dossier, expected_task = _expected_artifact_paths(watch.id, packet.id)
    paths = (
        ("dossier", expected_dossier, packet.dossier_path, packet.dossier_artifact_id, packet.dossier_sha256, "guardian_decision_dossier"),
        ("task", expected_task, packet.task_path, packet.task_artifact_id, packet.task_sha256, "guardian_local_task"),
    )
    packet_metadata_verified = (
        str(packet.status) == "succeeded"
        and str(packet.verification_status) == "passed"
        and bool(_parse_material_keys(packet))
        and str(watch.goal_id or "") == str(packet.goal_id or "")
        and _job_has_verified_readbacks(run, packet=packet, watch=watch)
    )
    verified_at = None
    if packet_metadata_verified:
        expected_paths_set = {expected_dossier, expected_task}
        readback_times = [
            parsed
            for effect in _parse_json_list(run.effect_receipts_json if run is not None else None)
            if str(effect.get("receipt_kind") or "") == "readback"
            and str(effect.get("target_path") or "") in expected_paths_set
            and str(effect.get("status") or "") == "succeeded"
            for parsed in [_parse_verified_at(effect.get("verified_at"))]
            if parsed is not None
        ]
        verified_at = max(readback_times, default=None)
    overall = "verified"
    for kind, expected_path, path, artifact_id, expected_sha, artifact_type in paths:
        safe_artifact = _safe_id(artifact_id)
        safe_sha = _valid_digest(expected_sha)
        expected_artifact_id = (
            artifact_id_for(
                file_path=expected_path,
                artifact_type=artifact_type,
                producer="guardian.research-watch.v1",
                run_id=packet.run_identity,
                content_sha256=safe_sha,
            )
            if safe_sha is not None
            else None
        )
        status = "verified" if (
            packet_metadata_verified
            and path == expected_path
            and safe_artifact is not None
            and safe_artifact == expected_artifact_id
            and safe_sha is not None
        ) else "blocked"
        if status != "verified":
            overall = "blocked"
        refs.append(
            {
                "kind": kind,
                "artifact_type": artifact_type,
                "file_path": expected_path,
                "artifact_id": safe_artifact,
                "sha256": safe_sha,
                "status": status,
                "verification": "cached_readback" if status == "verified" else "metadata_blocked",
                "last_verified_at": _iso(verified_at),
                "workflow_run_id": _safe_id(getattr(run, "run_identity", None)),
                "owner_session_id": _safe_id(getattr(watch, "owner_session_id", None)),
            }
        )
    return overall, refs


def _artifact_status(
    packet: GuardianDecisionPacket | None,
    watch: GuardianSourceWatch | None,
    run: WorkflowRunState | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Verify bounded local artifact bytes for explicit detail/action paths."""

    metadata_status, metadata_refs = _artifact_metadata_status(packet, watch, run)
    if metadata_status != "verified":
        return metadata_status, metadata_refs
    expected_dossier, expected_task = _expected_artifact_paths(watch.id, packet.id)
    paths = (
        ("dossier", expected_dossier, packet.dossier_path, packet.dossier_artifact_id, packet.dossier_sha256, 72 * 1024),
        ("task", expected_task, packet.task_path, packet.task_artifact_id, packet.task_sha256, 8 * 1024),
    )
    overall = "verified"
    # Importing the filesystem helpers here keeps source_watch -> inbox
    # completion hooks free of an import cycle.
    from src.tools.filesystem_tool import _read_workspace_text_bounded, _safe_resolve

    for index, (kind, expected_path, path, artifact_id, expected_sha, max_bytes) in enumerate(paths):
        safe_artifact = _safe_id(artifact_id)
        safe_sha = _valid_digest(expected_sha)
        status = "verified"
        if path != expected_path or safe_artifact is None or safe_sha is None:
            status = "blocked"
        else:
            try:
                content, truncated = _read_workspace_text_bounded(
                    _safe_resolve(expected_path), max_bytes=max_bytes
                )
                content_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
                expected_artifact_id = artifact_id_for(
                    file_path=expected_path,
                    artifact_type=(
                        "guardian_decision_dossier"
                        if kind == "dossier"
                        else "guardian_local_task"
                    ),
                    producer="guardian.research-watch.v1",
                    run_id=packet.run_identity,
                    content_sha256=content_sha,
                )
                if (
                    truncated
                    or content_sha != safe_sha
                    or safe_artifact != expected_artifact_id
                ):
                    status = "blocked"
            except (OSError, ValueError):
                status = "blocked"
        if status != "verified":
            overall = "blocked"
        ref = dict(metadata_refs[index])
        ref["artifact_id"] = safe_artifact
        ref["sha256"] = safe_sha
        ref["status"] = status
        ref["verification"] = "byte_hash" if status == "verified" else "byte_hash_blocked"
        metadata_refs[index] = ref
    return overall, metadata_refs


async def _redact_preview_text(db: Any, text: str, *, max_chars: int) -> str:
    """Redact known vault values through the caller's read-only DB session.

    ``vault_repository.list_secret_values`` is intentionally audited for its
    normal CRUD/redaction callers.  A GET detail projection cannot open that
    nested session because it would turn a passive read into a write, so this
    narrow preview path reads the same canonical ``Secret`` rows directly and
    fails closed when the lookup or decryption is unavailable.
    """

    redacted = await vault_redaction.redact_secrets_in_text_readonly(
        db,
        text,
        fail_closed=True,
    )
    return redacted[:max_chars]


async def _load_action_history(
    db: Any,
    *,
    owner_principal_id: str,
    owner_session_id: str,
    item_id: str,
) -> tuple[list[dict[str, Any]], bool]:
    """Project bounded, owner-fenced action receipts for an item detail.

    The query applies the principal, session, and disposition fence before the
    bounded fetch.  Only typed receipt fields are returned; the original
    idempotency key, payload digest, and result JSON never cross this boundary.
    Read-only vault redaction deliberately reuses the caller's session so a
    passive detail request cannot create audit rows or nested writer sessions.
    """

    query = (
        select(GuardianInboxAction)
        .where(
            GuardianInboxAction.owner_principal_id == owner_principal_id,
            GuardianInboxAction.owner_session_id == owner_session_id,
            GuardianInboxAction.item_id == item_id,
        )
        .order_by(
            GuardianInboxAction.created_at.desc(),
            GuardianInboxAction.id.desc(),
        )
        .limit(_ACTION_HISTORY_QUERY_LIMIT)
    )
    rows = list((await db.execute(query)).scalars().all())
    truncated = len(rows) > _ACTION_HISTORY_LIMIT
    rows = rows[:_ACTION_HISTORY_LIMIT]

    task_ids = {
        safe_task_id
        for safe_task_id in (_safe_id(row.task_id) for row in rows)
        if safe_task_id is not None
    }
    owner_task_ids: set[str] = set()
    if task_ids:
        owner_task_ids = set(
            (
                await db.execute(
                    select(WorkBoardTask.task_id).where(
                        WorkBoardTask.task_id.in_(task_ids),
                        WorkBoardTask.owner_principal_id == owner_principal_id,
                        WorkBoardTask.owner_session_id == owner_session_id,
                    )
                )
            )
            .scalars()
            .all()
        )

    history: list[dict[str, Any]] = []
    for row in rows:
        safe_reason: str | None
        if row.safe_reason is None:
            safe_reason = None
            reason_state = "unavailable"
        elif str(row.safe_reason) == "":
            safe_reason = ""
            reason_state = "not_provided"
        else:
            safe_reason = await _redact_preview_text(
                db,
                str(row.safe_reason)[:500],
                max_chars=500,
            )
            reason_state = (
                "unavailable"
                if safe_reason == "[redaction unavailable]"
                else "provided"
            )

        outcome = "unavailable"
        # Legacy/corrupt rows must not make a passive projection parse an
        # unbounded JSON blob.  M1 receipts are tiny; oversized values are
        # treated as unavailable rather than partially exposing them.
        raw_result_json = str(row.safe_result_json or "")
        if len(raw_result_json) <= 8192:
            try:
                result_payload = json.loads(raw_result_json or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                result_payload = {}
        else:
            result_payload = {}
        if isinstance(result_payload, dict):
            candidate = result_payload.get("state")
            if isinstance(candidate, str) and candidate in _SAFE_ACTION_OUTCOMES:
                outcome = candidate

        task_id = _safe_id(row.task_id)
        action = str(row.action or "")
        history.append(
            {
                "receipt_id": _safe_id(row.id),
                "action": action if action in _SAFE_ACTIONS else "unavailable",
                "created_at": _iso(row.created_at),
                "expected_revision": int(row.prior_revision or 0),
                "result_revision": int(row.result_revision or 0),
                "task_id": task_id if task_id in owner_task_ids else None,
                "outcome": outcome,
                "reason_state": reason_state,
                "safe_reason": safe_reason,
            }
        )
    return history, truncated


async def _evidence_previews(
    db: Any,
    *,
    refs: list[dict[str, Any]],
    owner_session_id: str,
    workflow_run_id: str | None,
) -> list[dict[str, Any]]:
    """Return bounded, redacted detail previews after byte/hash verification."""

    from src.tools.filesystem_tool import _read_workspace_text_bounded, _safe_resolve

    previews: list[dict[str, Any]] = []
    for ref in refs:
        if ref.get("status") != "verified":
            return []
        file_path = str(ref.get("file_path") or "")
        artifact_id = _safe_id(ref.get("artifact_id"))
        expected_sha = _valid_digest(ref.get("sha256"))
        max_bytes = 72 * 1024 if ref.get("kind") == "dossier" else 8 * 1024
        if not file_path or artifact_id is None or expected_sha is None:
            return []
        try:
            content, truncated = _read_workspace_text_bounded(
                _safe_resolve(file_path), max_bytes=max_bytes
            )
        except (OSError, ValueError):
            return []
        content_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if truncated or content_sha != expected_sha:
            return []
        previews.append(
            {
                "artifact_id": artifact_id,
                "artifact_type": str(ref.get("artifact_type") or "")[:128],
                "file_path": file_path[:256],
                "sha256": expected_sha,
                "owner_session_id": _safe_id(owner_session_id),
                "workflow_run_id": _safe_id(workflow_run_id),
                "text": await _redact_preview_text(db, content, max_chars=max_bytes),
                "trust": "untrusted_source_evidence",
            }
        )
    return previews


def _job_detail_projection(
    run: WorkflowRunState | None,
    *,
    packet: GuardianDecisionPacket | None,
    watch: GuardianSourceWatch | None,
) -> dict[str, Any] | None:
    """Return only the selected packet's safe durable job/readback metadata."""

    if run is None or packet is None or watch is None:
        return None
    if (
        str(run.run_identity or "") != str(packet.run_identity or "")
        or str(run.job_kind or "") != "guardian_source_watch"
        or str(run.goal_id or "") != str(packet.goal_id or "")
        or int(run.goal_revision or 0) != int(packet.goal_revision or 0)
        or int(run.plan_revision or 0) != int(packet.plan_revision or 0)
    ):
        return None
    expected_paths = set(_expected_artifact_paths(watch.id, packet.id))
    readbacks: list[dict[str, Any]] = []
    for effect in _parse_json_list(run.effect_receipts_json):
        if (
            str(effect.get("receipt_kind") or "") != "readback"
            or str(effect.get("target_path") or "") not in expected_paths
        ):
            continue
        readbacks.append(
            {
                "readback_id": _safe_id(effect.get("readback_id")),
                "verified_at": _iso(_parse_verified_at(effect.get("verified_at"))),
                "digest": _valid_digest(effect.get("content_sha256") or effect.get("target_digest")),
                "status": str(effect.get("status") or "unknown")[:64],
                "target_path": str(effect.get("target_path") or "")[:256],
            }
        )
    readbacks.sort(key=lambda value: str(value.get("target_path") or ""))
    first = readbacks[0] if readbacks else {}
    return {
        "id": str(run.run_identity),
        "status": str(run.status or "unknown"),
        "attempt_count": int(run.attempt_count or 0),
        "max_attempts": int(run.max_attempts or 0),
        "readback_id": first.get("readback_id"),
        "verified_at": first.get("verified_at"),
        "digest": first.get("digest"),
        "readback_status": first.get("status"),
        "readbacks": readbacks,
    }


def _mail_notice_message_key(disposition: GuardianInboxDisposition) -> str | None:
    """Extract the opaque message key from a mail notice identity."""

    prefix = f"mail-notice:{str(disposition.watch_id or '').strip()}:"
    source_id = str(disposition.source_id or "")
    if not source_id.startswith(prefix):
        return None
    key = source_id[len(prefix) :]
    return key if _safe_id(key, max_length=128) is not None else None


def _mail_notice_digest(
    *,
    source_id: str,
    message: MailMessageBinding,
    binding: GovernedScheduleBinding,
) -> str:
    """Recreate the metadata-only notice digest used by the scheduler."""

    prefix = f"mail-notice:{str(binding.binding_id)}:"
    key = source_id[len(prefix) :] if source_id.startswith(prefix) else ""
    return "sha256:" + _digest(
        _json(
            {
                "source_id": source_id,
                "message_key": key,
                "message_revision": str(message.message_revision or ""),
                "watch_id": str(binding.binding_id),
                "goal_id": str(binding.goal_id),
                "goal_revision": int(binding.goal_revision or 0),
            }
        )
    )


async def _load_mail_projection_row(
    db: Any,
    *,
    item_id: str,
    owner_principal_id: str,
    owner_session_id: str,
) -> tuple[
    GuardianInboxDisposition,
    GovernedScheduleBinding | None,
    MailWatchState | None,
    Goal | None,
    MailReadConsent | None,
    GoogleServiceConnection | None,
    MailMessageBinding | None,
] | None:
    """Load one owner-bound metadata notice and its authority graph."""

    disposition = (
        await db.execute(
            select(GuardianInboxDisposition).where(
                GuardianInboxDisposition.id == item_id,
                GuardianInboxDisposition.owner_principal_id == owner_principal_id,
                GuardianInboxDisposition.owner_session_id == owner_session_id,
                GuardianInboxDisposition.source_kind == MAIL_SOURCE_KIND,
            )
        )
    ).scalar_one_or_none()
    if disposition is None:
        return None
    message_key = _mail_notice_message_key(disposition)
    query = (
        select(
            GuardianInboxDisposition,
            GovernedScheduleBinding,
            MailWatchState,
            Goal,
            MailReadConsent,
            GoogleServiceConnection,
            MailMessageBinding,
        )
        .outerjoin(
            GovernedScheduleBinding,
            GovernedScheduleBinding.binding_id == GuardianInboxDisposition.watch_id,
        )
        .outerjoin(MailWatchState, MailWatchState.binding_id == GuardianInboxDisposition.watch_id)
        .outerjoin(Goal, Goal.id == GuardianInboxDisposition.goal_id)
        .outerjoin(MailReadConsent, MailReadConsent.consent_id == MailWatchState.consent_id)
        .outerjoin(
            GoogleServiceConnection,
            GoogleServiceConnection.connection_id == MailWatchState.connection_id,
        )
        .outerjoin(
            MailMessageBinding,
            and_(
                MailMessageBinding.owner_principal_id == owner_principal_id,
                MailMessageBinding.owner_session_id == owner_session_id,
                MailMessageBinding.connection_id == MailWatchState.connection_id,
                MailMessageBinding.message_key == (message_key or "\0"),
            ),
        )
        .where(
            GuardianInboxDisposition.id == item_id,
            GuardianInboxDisposition.owner_principal_id == owner_principal_id,
            GuardianInboxDisposition.owner_session_id == owner_session_id,
            GuardianInboxDisposition.source_kind == MAIL_SOURCE_KIND,
        )
    )
    row = (await db.execute(query)).first()
    if row is None:
        return None
    result = tuple(row)
    if not _mail_projection_owner_safe(
        *result,
        owner_principal_id=owner_principal_id,
        owner_session_id=owner_session_id,
    ):
        return None
    return result  # type: ignore[return-value]


def _mail_projection_owner_safe(
    disposition: GuardianInboxDisposition,
    binding: GovernedScheduleBinding | None,
    state: MailWatchState | None,
    goal: Goal | None,
    consent: MailReadConsent | None,
    connection: GoogleServiceConnection | None,
    message: MailMessageBinding | None,
    *,
    owner_principal_id: str,
    owner_session_id: str,
) -> bool:
    """Fail closed when any existing mail authority row belongs elsewhere."""

    for row in (binding, state, goal, consent, connection, message):
        if row is None:
            continue
        if (
            str(getattr(row, "owner_principal_id", "") or "") != owner_principal_id
            or str(getattr(row, "owner_session_id", "") or "") != owner_session_id
        ):
            return False
    if binding is not None and (
        str(binding.binding_id) != str(disposition.watch_id)
        or str(binding.goal_id) != str(disposition.goal_id)
    ):
        return False
    if state is not None and (
        str(state.binding_id) != str(disposition.watch_id)
        or str(state.goal_id) != str(disposition.goal_id)
    ):
        return False
    if goal is not None and str(goal.id) != str(disposition.goal_id):
        return False
    if consent is not None and state is not None and (
        str(consent.consent_id) != str(state.consent_id)
        or str(consent.connection_id) != str(state.connection_id)
    ):
        return False
    if connection is not None and state is not None and str(connection.connection_id) != str(state.connection_id):
        return False
    if message is not None and state is not None and str(message.connection_id) != str(state.connection_id):
        return False
    return True


def _mail_authority_projection(
    disposition: GuardianInboxDisposition,
    binding: GovernedScheduleBinding | None,
    state: MailWatchState | None,
    goal: Goal | None,
    consent: MailReadConsent | None,
    connection: GoogleServiceConnection | None,
    message: MailMessageBinding | None,
    *,
    now: datetime,
) -> tuple[bool, str | None, str | None]:
    """Validate the exact mail watch/message/goal chain before an action."""

    if any(row is None for row in (binding, state, goal, consent, connection, message)):
        return False, "the watched Mail metadata is unavailable; refresh the watch", "refresh_mail_watch"
    assert binding is not None and state is not None and goal is not None
    assert consent is not None and connection is not None and message is not None
    key = _mail_notice_message_key(disposition)
    if key is None or message.message_key != key:
        return False, "the watched message binding is unavailable; rescan Mail", "rescan_mail_messages"
    if (
        binding.action_type != "gmail.scan_metadata.v1"
        or binding.capability_id != "gmail.scan_metadata.v1"
        or binding.state != "active"
        or _utc(binding.expires_at) <= now
        or state.binding_id != binding.binding_id
        or state.owner_principal_id != disposition.owner_principal_id
        or state.owner_session_id != disposition.owner_session_id
        or state.goal_id != disposition.goal_id
        or int(state.goal_revision or 0) != int(disposition.goal_revision or 0)
        or int(binding.goal_revision or 0) != int(disposition.goal_revision or 0)
        or not bool(state.baseline_complete)
        or state.state not in {"active", "coverage_blocked", "baseline_complete"}
    ):
        return False, "the Mail watch authority changed; review the current watch", "refresh_mail_watch"
    if (
        goal.id != disposition.goal_id
        or int(goal.revision or 0) != int(disposition.goal_revision or 0)
        or str(getattr(goal.status, "value", goal.status) or "") != "active"
        or not bool(getattr(goal, "proactive_enabled", False))
    ):
        return False, "the watched goal is no longer active", "review_goal_and_watch"
    budget = deserialize_admission_budget(goal)
    period_expires_at = _utc(getattr(budget, "period_expires_at", None)) if budget is not None else None
    period_started_at = _utc(getattr(budget, "period_started_at", None)) if budget is not None else None
    if (
        budget is None
        or not bool(getattr(budget, "reviewed_grant", False))
        or not str(getattr(budget, "grant_id", "") or "").strip()
        or period_expires_at is None
        or period_expires_at <= now
        or period_started_at is not None and period_started_at > now
    ):
        return False, "the reviewed goal budget is unavailable or expired", "review_goal_and_watch"
    if (
        consent.consent_id != state.consent_id
        or consent.connection_id != connection.connection_id
        or int(consent.connection_revision or 0) != int(connection.revision or 0)
        or int(consent.source_revision or 0) != int(state.source_consent_revision or 0)
        or consent.state != "active"
        or not bool(consent.source_read_allowed)
        or _utc(consent.expires_at) <= now
        or connection.state != "active"
        or int(message.connection_revision or 0) != int(connection.revision or 0)
        or message.source_consent_id != consent.consent_id
        or int(message.source_consent_revision or 0) != int(consent.source_revision or 0)
        or message.status != "present"
        or disposition.source_digest != _mail_notice_digest(
            source_id=disposition.source_id,
            message=message,
            binding=binding,
        )
    ):
        return False, "the Mail source or connection authority changed", "rescan_mail_messages"
    return True, None, None


def _project_mail_item(
    disposition: GuardianInboxDisposition,
    binding: GovernedScheduleBinding | None,
    state: MailWatchState | None,
    goal: Goal | None,
    consent: MailReadConsent | None,
    connection: GoogleServiceConnection | None,
    message: MailMessageBinding | None,
    *,
    now: datetime,
    detail: bool = False,
    action_history: list[dict[str, Any]] | None = None,
    action_history_truncated: bool = False,
) -> dict[str, Any]:
    effective_state = disposition.state
    if effective_state in {"pending", "snoozed"} and _utc(disposition.expires_at) <= now:
        effective_state = "expired"
    authority_ok, authority_reason, authority_recovery = _mail_authority_projection(
        disposition,
        binding,
        state,
        goal,
        consent,
        connection,
        message,
        now=now,
    )
    if authority_ok:
        source_status = str(state.state if state is not None else "observed")
        policy_reason = "New Mail metadata is available; accepting creates a neutral triage task only"
    else:
        source_status = "stale_authority"
        policy_reason = authority_reason or "Mail watch authority requires recovery"
    allowed_actions: list[str] = []
    if authority_ok and effective_state in {"pending", "snoozed"} and (
        effective_state == "pending"
        or _utc(disposition.snoozed_until) is None
        or _utc(disposition.snoozed_until) <= now
    ):
        allowed_actions = ["accept_followup", "snooze", "dismiss"]
    item: dict[str, Any] = {
        "id": disposition.id,
        "revision": disposition.revision,
        "state": effective_state,
        "source_kind": MAIL_SOURCE_KIND,
        "source_id": disposition.source_id,
        "source_digest": disposition.source_digest,
        "title": "New message in watched mailbox",
        "summary": "A new message was observed. Open the private Mail view to inspect it.",
        "why_now": "A metadata-only Mail watch observed a new message for the selected goal.",
        "goal_id": disposition.goal_id,
        "goal_revision": disposition.goal_revision,
        "watch_id": disposition.watch_id,
        "plan_revision": disposition.plan_revision,
        "task_id": disposition.task_id,
        "expires_at": _iso(disposition.expires_at),
        "snoozed_until": _iso(disposition.snoozed_until),
        "created_at": _iso(disposition.created_at),
        "updated_at": _iso(disposition.updated_at),
        "evidence": {"status": "metadata_only", "memory_status": "no_learning"},
        "evidence_refs": [],
        "evidence_status": "metadata_verified" if authority_ok else "blocked",
        "last_verified_at": _iso(message.updated_at if message is not None else None),
        "source": {
            "status": source_status,
            "observed_at": _iso(message.updated_at if message is not None else disposition.updated_at),
        },
        "verification_status": "metadata_verified" if message is not None else "blocked",
        "memory_status": "no_learning",
        "policy_reason": policy_reason,
        "allowed_actions": allowed_actions,
    }
    if authority_recovery:
        item["recovery_action"] = authority_recovery
    if detail:
        item["links"] = {
            "mail_watch": f"/api/capabilities/mail/watches/{disposition.watch_id}",
            "board_task": f"/api/work-board/tasks/{disposition.task_id}" if disposition.task_id else None,
            "mail_message_read": (
                f"/api/capabilities/mail/messages/{message.message_binding_id}/read"
                if message is not None
                else None
            ),
        }
        item["mail"] = {
            "message_binding_id": message.message_binding_id if message is not None else None,
            "message_revision": message.message_revision if message is not None else None,
            "received_at": _iso(message.received_at if message is not None else None),
            "status": message.status if message is not None else "unavailable",
            "private": True,
        }
        item["goal"] = {"id": disposition.goal_id, "revision": disposition.goal_revision}
        item["action_history"] = action_history or []
        item["action_history_truncated"] = bool(action_history_truncated)
    return item


async def _load_projection_row(
    db: Any,
    *,
    item_id: str | None = None,
    owner_principal_id: str,
    owner_session_id: str,
) -> tuple[
    GuardianInboxDisposition,
    GuardianDecisionPacket | None,
    GuardianSourceWatch | None,
    Goal | None,
] | None:
    query = (
        select(GuardianInboxDisposition, GuardianDecisionPacket, GuardianSourceWatch, Goal)
        .outerjoin(
            GuardianDecisionPacket,
            and_(
                GuardianInboxDisposition.source_kind == SOURCE_KIND,
                GuardianDecisionPacket.id == GuardianInboxDisposition.source_id,
            ),
        )
        .outerjoin(GuardianSourceWatch, GuardianSourceWatch.id == GuardianDecisionPacket.source_watch_id)
        .outerjoin(Goal, Goal.id == GuardianDecisionPacket.goal_id)
        .where(
            GuardianInboxDisposition.owner_principal_id == owner_principal_id,
            GuardianInboxDisposition.owner_session_id == owner_session_id,
            GuardianInboxDisposition.source_kind == SOURCE_KIND,
            or_(
                GuardianDecisionPacket.id.is_(None),
                and_(
                    GuardianDecisionPacket.id == GuardianInboxDisposition.source_id,
                    GuardianDecisionPacket.goal_id == GuardianInboxDisposition.goal_id,
                    GuardianDecisionPacket.source_watch_id == GuardianInboxDisposition.watch_id,
                ),
            ),
            or_(
                GuardianSourceWatch.id.is_(None),
                and_(
                    GuardianSourceWatch.owner_principal_id == owner_principal_id,
                    GuardianSourceWatch.owner_session_id == owner_session_id,
                    GuardianSourceWatch.id == GuardianInboxDisposition.watch_id,
                ),
            ),
            or_(
                Goal.id.is_(None),
                and_(
                    Goal.owner_principal_id == owner_principal_id,
                    Goal.owner_session_id == owner_session_id,
                    Goal.id == GuardianInboxDisposition.goal_id,
                ),
            ),
        )
    )
    if item_id is not None:
        query = query.where(GuardianInboxDisposition.id == item_id)
    row = (await db.execute(query)).first()
    if row is None:
        return None
    disposition, packet, watch, goal = row
    # A packet is not itself owner-bearing, so a stale or manually altered
    # graph must never become an operator projection or an authority binding.
    if not _projection_owner_safe(
        disposition,
        packet,
        watch,
        goal,
        owner_principal_id=owner_principal_id,
        owner_session_id=owner_session_id,
    ):
        return None
    return disposition, packet, watch, goal


def _projection_owner_safe(
    disposition: GuardianInboxDisposition,
    packet: GuardianDecisionPacket | None,
    watch: GuardianSourceWatch | None,
    goal: Goal | None,
    *,
    owner_principal_id: str,
    owner_session_id: str,
) -> bool:
    """Allow only a fully matching graph or a genuinely missing dependency."""

    if packet is not None and (
        str(packet.goal_id) != str(disposition.goal_id)
        or str(packet.source_watch_id) != str(disposition.watch_id)
        or str(packet.id) != str(disposition.source_id)
    ):
        return False
    if watch is not None and (
        str(watch.owner_principal_id) != owner_principal_id
        or str(watch.owner_session_id) != owner_session_id
        or str(watch.id) != str(disposition.watch_id)
    ):
        return False
    if goal is not None and (
        str(goal.owner_principal_id or "") != owner_principal_id
        or str(goal.owner_session_id or "") != owner_session_id
        or str(goal.id) != str(disposition.goal_id)
    ):
        return False
    return True


def _authority_projection(
    disposition: GuardianInboxDisposition,
    packet: GuardianDecisionPacket | None,
    watch: GuardianSourceWatch | None,
    goal: Goal | None,
    *,
    now: datetime,
) -> tuple[bool, str | None, str | None]:
    """Describe whether a pending projection still has live authority.

    Inbox rows are durable history.  A goal or watch can therefore change
    after the row was created.  Keep the row visible for recovery, but do not
    advertise acceptance once its immutable binding or current admission
    budget is stale.
    """

    if packet is None or watch is None or goal is None:
        return False, "source binding is stale or unavailable; review recovery", "refresh_inbox"
    if (
        str(packet.goal_id or "") != str(disposition.goal_id or "")
        or str(packet.source_watch_id or "") != str(disposition.watch_id or "")
        or str(packet.input_digest or "") != str(disposition.source_digest or "")
        or int(packet.goal_revision or 0) != int(disposition.goal_revision or 0)
        or int(packet.plan_revision or 0) != int(disposition.plan_revision or 0)
        or str(watch.goal_id or "") != str(disposition.goal_id or "")
        or int(watch.goal_revision or 0) != int(disposition.goal_revision or 0)
        or int(watch.plan_revision or 0) != int(disposition.plan_revision or 0)
        or int(goal.revision or 0) != int(disposition.goal_revision or 0)
    ):
        return False, "goal or source watch authority changed; review the current plan", "review_goal_and_watch"
    if str(getattr(goal.status, "value", goal.status) or "") != "active":
        return False, "the watched goal is no longer active; review the current plan", "review_goal_and_watch"
    if not bool(getattr(goal, "proactive_enabled", False)):
        return False, "proactive work is disabled for this goal; review the current plan", "review_goal_and_watch"
    if str(watch.state or "") != "active":
        return False, "the source watch is paused or blocked; recover the source watch", "recover_source_watch"
    budget = deserialize_admission_budget(goal)
    period_expires_at = _utc(getattr(budget, "period_expires_at", None)) if budget is not None else None
    period_started_at = _utc(getattr(budget, "period_started_at", None)) if budget is not None else None
    if (
        budget is None
        or not bool(getattr(budget, "reviewed_grant", False))
        or not str(getattr(budget, "grant_id", "") or "").strip()
        or period_expires_at is None
    ):
        return False, "the reviewed goal budget is missing or revoked; review admission", "review_goal_and_watch"
    if period_expires_at <= now:
        return False, "the reviewed goal budget has expired; review admission", "review_goal_and_watch"
    if period_started_at is not None and period_started_at > now:
        return False, "the reviewed goal budget has not started; review admission", "review_goal_and_watch"
    return True, None, None


def _project_item(
    disposition: GuardianInboxDisposition,
    packet: GuardianDecisionPacket | None,
    watch: GuardianSourceWatch | None,
    goal: Goal | None,
    *,
    goal_label: str,
    artifact_status: str,
    evidence_refs: list[dict[str, Any]],
    now: datetime,
    run: WorkflowRunState | None = None,
    detail: bool = False,
    evidence_previews: list[dict[str, Any]] | None = None,
    action_history: list[dict[str, Any]] | None = None,
    action_history_truncated: bool = False,
) -> dict[str, Any]:
    effective_state = disposition.state
    if effective_state in {"pending", "snoozed"} and _utc(disposition.expires_at) <= now:
        effective_state = "expired"
    authority_ok, authority_reason, authority_recovery = _authority_projection(
        disposition,
        packet,
        watch,
        goal,
        now=now,
    )
    material_count = len(_parse_material_keys(packet)) if packet is not None else 0
    observed_at = _utc(packet.updated_at if packet is not None else disposition.updated_at)
    age_seconds = max(0, int((now - observed_at).total_seconds())) if observed_at else None
    verification_status = str(getattr(packet, "verification_status", "blocked") or "blocked")
    memory_status = str(getattr(packet, "memory_status", "unknown") or "unknown")
    last_verified_at = next(
        (
            str(ref.get("last_verified_at"))
            for ref in evidence_refs
            if ref.get("last_verified_at")
        ),
        None,
    )
    if packet is None or watch is None or goal is None:
        policy_reason = "source binding is stale or unavailable; review recovery"
        source_status = "orphaned"
    elif not authority_ok and authority_reason:
        policy_reason = authority_reason
        source_status = "stale_authority"
    elif artifact_status != "verified":
        policy_reason = "verified packet metadata exists but local evidence needs recovery"
        source_status = str(watch.last_status or "stale")
    else:
        policy_reason = "verified local output; accepting creates a triage follow-up only"
        source_status = str(watch.last_status or "succeeded")
    allowed_actions: list[str] = []
    if authority_ok and effective_state in {"pending", "snoozed"} and (
        effective_state == "pending"
        or _utc(disposition.snoozed_until) is None
        or _utc(disposition.snoozed_until) <= now
    ):
        allowed_actions = ["snooze", "dismiss"]
        if authority_ok and artifact_status == "verified":
            allowed_actions.insert(0, "accept_followup")
    item: dict[str, Any] = {
        "id": disposition.id,
        "revision": disposition.revision,
        "state": effective_state,
        "source_kind": disposition.source_kind,
        "source_id": disposition.source_id,
        "source_digest": disposition.source_digest,
        "title": "Watched source changed",
        "summary": "A verified local dossier is ready for your review",
        "why_now": (
            f"{material_count} permitted source change(s) were verified for "
            f"{goal_label} at {_iso(disposition.created_at)}."
        )[:_MAX_DISPLAY],
        "goal_id": disposition.goal_id,
        "goal_revision": disposition.goal_revision,
        "watch_id": disposition.watch_id,
        "plan_revision": disposition.plan_revision,
        "task_id": disposition.task_id,
        "expires_at": _iso(disposition.expires_at),
        "snoozed_until": _iso(disposition.snoozed_until),
        "created_at": _iso(disposition.created_at),
        "updated_at": _iso(disposition.updated_at),
        "evidence": {
            "dossier_artifact_id": _safe_id(getattr(packet, "dossier_artifact_id", None)),
            "dossier_sha256": _valid_digest(getattr(packet, "dossier_sha256", None)),
            "task_artifact_id": _safe_id(getattr(packet, "task_artifact_id", None)),
            "task_sha256": _valid_digest(getattr(packet, "task_sha256", None)),
        },
        "evidence_refs": evidence_refs,
        "evidence_status": artifact_status,
        "last_verified_at": last_verified_at,
        "source": {
            "status": source_status,
            "observed_at": _iso(observed_at),
            "age_seconds": age_seconds,
        },
        "verification_status": verification_status,
        "memory_status": memory_status,
        "policy_reason": policy_reason,
        "allowed_actions": allowed_actions,
    }
    if authority_recovery:
        item["recovery_action"] = authority_recovery
    if detail:
        item["job"] = _job_detail_projection(run, packet=packet, watch=watch)
        item["evidence_previews"] = evidence_previews or []
        item["action_history"] = action_history or []
        item["action_history_truncated"] = bool(action_history_truncated)
    if detail:
        links: dict[str, str | None] = {
            "source_watch": f"/api/capabilities/source-watches/{disposition.watch_id}",
            "board_task": (
                f"/api/work-board/tasks/{disposition.task_id}" if disposition.task_id else None
            ),
            # There is no safe read-only packet endpoint in the current
            # source-watch API; do not invent a URL that implies one exists.
            "packet": None,
        }
        item["links"] = links
        item["goal"] = {"id": disposition.goal_id, "revision": disposition.goal_revision, "label": goal_label}
    return item


def _encode_cursor(created_at: datetime, item_id: str) -> str:
    payload = _json({"created_at": _iso(created_at), "id": item_id}).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(value: str | None) -> tuple[datetime, str] | None:
    if not value:
        return None
    if len(value) > _MAX_CURSOR_BYTES:
        raise InboxError("invalid_cursor", "The inbox cursor is invalid", status_code=422)
    try:
        padded = value + "=" * (-len(value) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
        parsed = datetime.fromisoformat(str(payload["created_at"]))
        item_id = _safe_id(payload["id"])
        if item_id is None:
            raise ValueError
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        raise InboxError("invalid_cursor", "The inbox cursor is invalid", status_code=422) from None
    normalized = _utc(parsed)
    if normalized is None:
        raise InboxError("invalid_cursor", "The inbox cursor is invalid", status_code=422)
    return normalized, item_id


async def list_owned_items(
    *,
    owner_principal_id: str,
    owner_session_id: str,
    limit: int = 50,
    cursor: str | None = None,
) -> dict[str, Any]:
    if limit < 1 or limit > 50:
        raise InboxError("invalid_limit", "limit must be between 1 and 50", status_code=422)
    cursor_value = _decode_cursor(cursor)
    now = _now()
    async with db_engine.get_session() as db:
        query = (
            select(GuardianInboxDisposition, GuardianDecisionPacket, GuardianSourceWatch, Goal)
            .outerjoin(
                GuardianDecisionPacket,
                and_(
                    GuardianInboxDisposition.source_kind == SOURCE_KIND,
                    GuardianDecisionPacket.id == GuardianInboxDisposition.source_id,
                ),
            )
            .outerjoin(GuardianSourceWatch, GuardianSourceWatch.id == GuardianDecisionPacket.source_watch_id)
            .outerjoin(Goal, Goal.id == GuardianDecisionPacket.goal_id)
            .where(
                GuardianInboxDisposition.owner_principal_id == owner_principal_id,
                GuardianInboxDisposition.owner_session_id == owner_session_id,
                GuardianInboxDisposition.source_kind == SOURCE_KIND,
                or_(
                    GuardianDecisionPacket.id.is_(None),
                    and_(
                        GuardianDecisionPacket.id == GuardianInboxDisposition.source_id,
                        GuardianDecisionPacket.goal_id == GuardianInboxDisposition.goal_id,
                        GuardianDecisionPacket.source_watch_id == GuardianInboxDisposition.watch_id,
                    ),
                ),
                or_(
                    GuardianSourceWatch.id.is_(None),
                    and_(
                        GuardianSourceWatch.owner_principal_id == owner_principal_id,
                        GuardianSourceWatch.owner_session_id == owner_session_id,
                        GuardianSourceWatch.id == GuardianInboxDisposition.watch_id,
                    ),
                ),
                or_(
                    Goal.id.is_(None),
                    and_(
                        Goal.owner_principal_id == owner_principal_id,
                        Goal.owner_session_id == owner_session_id,
                        Goal.id == GuardianInboxDisposition.goal_id,
                    ),
                ),
            )
            .order_by(GuardianInboxDisposition.created_at.asc(), GuardianInboxDisposition.id.asc())
            .limit(limit + 1)
        )
        if cursor_value is not None:
            cursor_at, cursor_id = cursor_value
            query = query.where(
                or_(
                    GuardianInboxDisposition.created_at > cursor_at,
                    and_(
                        GuardianInboxDisposition.created_at == cursor_at,
                        GuardianInboxDisposition.id > cursor_id,
                    ),
                )
            )
        rows = list((await db.execute(query)).all())
        rows_for_page = rows[:limit]
        items: list[dict[str, Any]] = []
        for disposition, packet, watch, goal in rows_for_page:
            if not _projection_owner_safe(
                disposition,
                packet,
                watch,
                goal,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
            ):
                # A mismatched existing dependency is private to its owner;
                # omit it. Truly missing dependencies remain a neutral,
                # blocked projection for recovery visibility.
                continue
            goal_label = await _safe_goal_label(goal)
            run = None
            if packet is not None:
                run = (
                    await db.execute(
                        select(WorkflowRunState).where(
                            WorkflowRunState.run_identity == packet.run_identity,
                        )
                    )
                ).scalars().first()
            status, refs = _artifact_metadata_status(packet, watch, run)
            items.append(
                _project_item(
                    disposition,
                    packet,
                    watch,
                    goal,
                    goal_label=goal_label,
                    artifact_status=status,
                    evidence_refs=refs,
                    now=now,
                    run=run,
                )
            )
        mail_query = (
            select(
                GuardianInboxDisposition,
                GovernedScheduleBinding,
                MailWatchState,
                Goal,
                MailReadConsent,
                GoogleServiceConnection,
            )
            .outerjoin(
                GovernedScheduleBinding,
                GovernedScheduleBinding.binding_id == GuardianInboxDisposition.watch_id,
            )
            .outerjoin(MailWatchState, MailWatchState.binding_id == GuardianInboxDisposition.watch_id)
            .outerjoin(Goal, Goal.id == GuardianInboxDisposition.goal_id)
            .outerjoin(MailReadConsent, MailReadConsent.consent_id == MailWatchState.consent_id)
            .outerjoin(
                GoogleServiceConnection,
                GoogleServiceConnection.connection_id == MailWatchState.connection_id,
            )
            .where(
                GuardianInboxDisposition.owner_principal_id == owner_principal_id,
                GuardianInboxDisposition.owner_session_id == owner_session_id,
                GuardianInboxDisposition.source_kind == MAIL_SOURCE_KIND,
            )
            .order_by(GuardianInboxDisposition.created_at.asc(), GuardianInboxDisposition.id.asc())
            .limit(limit + 1)
        )
        if cursor_value is not None:
            cursor_at, cursor_id = cursor_value
            mail_query = mail_query.where(
                or_(
                    GuardianInboxDisposition.created_at > cursor_at,
                    and_(
                        GuardianInboxDisposition.created_at == cursor_at,
                        GuardianInboxDisposition.id > cursor_id,
                    ),
                )
            )
        mail_rows = list((await db.execute(mail_query)).all())
        for mail_row in mail_rows[:limit]:
            (
                disposition,
                binding,
                state,
                goal,
                consent,
                connection,
            ) = mail_row
            message = None
            if connection is not None:
                message_key = _mail_notice_message_key(disposition)
                if message_key is not None:
                    message = (
                        await db.execute(
                            select(MailMessageBinding).where(
                                MailMessageBinding.owner_principal_id == owner_principal_id,
                                MailMessageBinding.owner_session_id == owner_session_id,
                                MailMessageBinding.connection_id == connection.connection_id,
                                MailMessageBinding.message_key == message_key,
                            )
                        )
                    ).scalar_one_or_none()
            if not _mail_projection_owner_safe(
                disposition,
                binding,
                state,
                goal,
                consent,
                connection,
                message,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
            ):
                continue
            items.append(
                _project_mail_item(
                    disposition,
                    binding,
                    state,
                    goal,
                    consent,
                    connection,
                    message,
                    now=now,
                )
            )
        items.sort(
            key=lambda item: (
                _utc(datetime.fromisoformat(str(item.get("created_at"))))
                if item.get("created_at")
                else datetime.min.replace(tzinfo=timezone.utc),
                str(item.get("id") or ""),
            )
        )
        has_more = len(rows) > limit or len(mail_rows) > limit
        page_items = items[:limit]
        next_cursor = None
        if has_more and page_items:
            last_created = _utc(datetime.fromisoformat(str(page_items[-1]["created_at"])))
            if last_created is not None:
                next_cursor = _encode_cursor(last_created, str(page_items[-1]["id"]))
        items = page_items
    return {"items": items, "next_cursor": next_cursor, "last_confirmed_at": now.isoformat()}


async def get_owned_item(
    *, owner_principal_id: str, owner_session_id: str, item_id: str, detail: bool = True
) -> dict[str, Any]:
    async with db_engine.get_session() as db:
        mail_row = await _load_mail_projection_row(
            db,
            item_id=item_id,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )
        if mail_row is not None:
            (
                disposition,
                binding,
                state,
                goal,
                consent,
                connection,
                message,
            ) = mail_row
            if detail:
                action_history, action_history_truncated = await _load_action_history(
                    db,
                    owner_principal_id=owner_principal_id,
                    owner_session_id=owner_session_id,
                    item_id=disposition.id,
                )
            else:
                action_history, action_history_truncated = [], False
            return _project_mail_item(
                disposition,
                binding,
                state,
                goal,
                consent,
                connection,
                message,
                now=_now(),
                detail=detail,
                action_history=action_history,
                action_history_truncated=action_history_truncated,
            )
        row = await _load_projection_row(
            db,
            item_id=item_id,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )
        if row is None:
            raise InboxError("inbox_item_not_found", "The inbox item does not exist", status_code=404)
        disposition, packet, watch, goal = row
        goal_label = await _safe_goal_label(goal)
        run = None
        if packet is not None:
            run = (
                await db.execute(
                    select(WorkflowRunState).where(
                        WorkflowRunState.run_identity == packet.run_identity,
                    )
                )
            ).scalars().first()
        status, refs = _artifact_status(packet, watch, run)
        if detail:
            action_history, action_history_truncated = await _load_action_history(
                db,
                owner_principal_id=owner_principal_id,
                owner_session_id=owner_session_id,
                item_id=disposition.id,
            )
        else:
            action_history, action_history_truncated = [], False
        previews = (
            await _evidence_previews(
                db,
                refs=refs,
                owner_session_id=owner_session_id,
                workflow_run_id=getattr(run, "run_identity", None),
            )
            if detail and status == "verified"
            else []
        )
        return _project_item(
            disposition,
            packet,
            watch,
            goal,
            goal_label=goal_label,
            artifact_status=status,
            evidence_refs=refs,
            now=_now(),
            run=run,
            detail=detail,
            evidence_previews=previews,
            action_history=action_history,
            action_history_truncated=action_history_truncated,
        )


async def ensure_inbox_disposition(*, packet_id: str) -> GuardianInboxDisposition | None:
    """Create or return the one disposition for a verified packet."""

    async with db_engine.get_session() as db:
        packet = (
            await db.execute(select(GuardianDecisionPacket).where(GuardianDecisionPacket.id == packet_id))
        ).scalars().first()
        if packet is None:
            return None
        if str(packet.status) != "succeeded" or str(packet.verification_status) != "passed":
            if getattr(packet, "inbox_pending", False):
                packet.inbox_pending = False
                db.add(packet)
            return None
        if not _parse_material_keys(packet) or _valid_digest(packet.input_digest) is None:
            if getattr(packet, "inbox_pending", False):
                packet.inbox_pending = False
                db.add(packet)
            return None
        watch = (
            await db.execute(select(GuardianSourceWatch).where(GuardianSourceWatch.id == packet.source_watch_id))
        ).scalars().first()
        goal = await db.get(Goal, packet.goal_id)
        if watch is None or goal is None:
            if getattr(packet, "inbox_pending", False):
                packet.inbox_pending = False
                db.add(packet)
            return None
        principal_id = str(watch.owner_principal_id or "")
        session_id = str(watch.owner_session_id or "")
        if (
            not principal_id
            or not session_id
            or principal_id != str(goal.owner_principal_id or "")
            or session_id != str(goal.owner_session_id or "")
            or str(watch.goal_id or "") != str(packet.goal_id or "")
            or str(packet.watch_id or "") != str(packet.source_watch_id or "")
            or int(goal.revision or 0) != int(packet.goal_revision or 0)
            or int(watch.goal_revision or 0) != int(packet.goal_revision or 0)
            or int(watch.plan_revision or 0) != int(packet.plan_revision or 0)
        ):
            if getattr(packet, "inbox_pending", False):
                packet.inbox_pending = False
                db.add(packet)
            return None
        run = (
            await db.execute(
                select(WorkflowRunState).where(
                    WorkflowRunState.run_identity == packet.run_identity,
                )
            )
        ).scalars().first()
        if not _job_has_verified_readbacks(run, packet=packet, watch=watch):
            # Packet finalization may commit just before its durable job reaches
            # terminal success. Keep that transient marker while the job is still
            # progressing, but clear terminal/orphaned misses so bounded repair
            # cannot be starved by an old malformed row.
            run_status = str(getattr(run, "status", "") or "") if run is not None else ""
            if run is None or run_status not in {"accepted", "queued", "running"}:
                if getattr(packet, "inbox_pending", False):
                    packet.inbox_pending = False
                    db.add(packet)
            return None
        budget = deserialize_admission_budget(goal)
        period_expires_at = _utc(getattr(budget, "period_expires_at", None)) if budget is not None else None
        if period_expires_at is None:
            if getattr(packet, "inbox_pending", False):
                packet.inbox_pending = False
                db.add(packet)
            return None
        created_at = _utc(packet.created_at) or _now()
        expires_at = min(created_at + timedelta(days=7), period_expires_at)
        if expires_at <= created_at:
            if getattr(packet, "inbox_pending", False):
                packet.inbox_pending = False
                db.add(packet)
            return None
        packet_source_digest = str(packet.input_digest)
        packet_identifier = str(packet.id)
        packet_goal_id = str(packet.goal_id)
        packet_watch_id = str(packet.source_watch_id)
        packet_goal_revision = int(packet.goal_revision or 1)
        packet_plan_revision = int(packet.plan_revision or 1)
        existing = (
            await db.execute(
                select(GuardianInboxDisposition).where(
                    GuardianInboxDisposition.owner_principal_id == principal_id,
                    GuardianInboxDisposition.source_kind == SOURCE_KIND,
                    GuardianInboxDisposition.source_id == packet_identifier,
                )
            )
        ).scalars().first()
        if existing is not None:
            if (
                existing.source_digest != packet_source_digest
                or existing.owner_session_id != session_id
                or existing.goal_id != packet_goal_id
                or existing.watch_id != packet_watch_id
                or int(existing.goal_revision or 0) != packet_goal_revision
                or int(existing.plan_revision or 0) != packet_plan_revision
            ):
                raise InboxError("source_digest_conflict", "The source packet digest changed", status_code=409)
            if getattr(packet, "inbox_pending", False):
                packet.inbox_pending = False
                db.add(packet)
            return existing
        row = GuardianInboxDisposition(
            owner_principal_id=principal_id,
            owner_session_id=session_id,
            source_kind=SOURCE_KIND,
            source_id=packet_identifier,
            source_digest=packet_source_digest,
            goal_id=packet_goal_id,
            goal_revision=packet_goal_revision,
            watch_id=packet_watch_id,
            plan_revision=packet_plan_revision,
            state="pending",
            revision=1,
            expires_at=expires_at,
        )
        db.add(row)
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            existing = (
                await db.execute(
                    select(GuardianInboxDisposition).where(
                        GuardianInboxDisposition.owner_principal_id == principal_id,
                        GuardianInboxDisposition.source_kind == SOURCE_KIND,
                        GuardianInboxDisposition.source_id == packet_identifier,
                    )
                )
            ).scalars().first()
            if existing is None:
                return None
            if (
                existing.source_digest != packet_source_digest
                or existing.owner_session_id != session_id
                or existing.goal_id != packet_goal_id
                or existing.watch_id != packet_watch_id
                or int(existing.goal_revision or 0) != packet_goal_revision
                or int(existing.plan_revision or 0) != packet_plan_revision
            ):
                raise InboxError("source_digest_conflict", "The source packet digest changed", status_code=409)
            return existing
        packet.inbox_pending = False
        db.add(packet)
        return row


async def repair_inbox_dispositions(*, limit: int = _REPAIR_LIMIT) -> int:
    """Repair only a bounded set of verified packet completion gaps."""

    bounded_limit = max(1, min(int(limit), _REPAIR_LIMIT))
    async with db_engine.get_session() as db:
        rows = list(
            (
                await db.execute(
                    select(GuardianDecisionPacket.id)
                    .where(
                        GuardianDecisionPacket.inbox_pending.is_(True),
                        GuardianDecisionPacket.status == "succeeded",
                        GuardianDecisionPacket.verification_status == "passed",
                    )
                    .order_by(GuardianDecisionPacket.updated_at.asc(), GuardianDecisionPacket.id.asc())
                    .limit(bounded_limit)
                )
            ).scalars()
        )
    repaired = 0
    for packet_id in rows:
        try:
            if await ensure_inbox_disposition(packet_id=str(packet_id)) is not None:
                repaired += 1
        except InboxError:
            continue
    return repaired


async def expire_inbox_items(*, limit: int = _REPAIR_LIMIT) -> int:
    """Expire a bounded set of due dispositions during the managed tick."""

    bounded_limit = max(1, min(int(limit), _REPAIR_LIMIT))
    now = _now()
    async with db_engine.get_session() as db:
        ids = list(
            (
                await db.execute(
                    select(GuardianInboxDisposition.id)
                    .where(
                        GuardianInboxDisposition.state.in_(("pending", "snoozed")),
                        GuardianInboxDisposition.expires_at <= now,
                    )
                    .order_by(GuardianInboxDisposition.expires_at.asc(), GuardianInboxDisposition.id.asc())
                    .limit(bounded_limit)
                )
            ).scalars()
        )
        if ids:
            await db.execute(
                update(GuardianInboxDisposition)
                .where(GuardianInboxDisposition.id.in_(ids))
                .values(state="expired", revision=GuardianInboxDisposition.revision + 1, updated_at=now)
            )
    return len(ids)


def _safe_action_reason(reason: str | None) -> str:
    if not reason:
        return ""
    # Reasons are never used to authorize execution.  Keep only a redacted,
    # bounded receipt marker; the async vault redaction occurs in apply_action.
    return str(reason).strip()[:500]


async def _verify_accept_artifacts(
    *, owner_principal_id: str, owner_session_id: str, item_id: str
) -> tuple[str, str, str, str, str, str, str, str, str, str, str]:
    async with db_engine.get_session() as db:
        row = await _load_projection_row(
            db,
            item_id=item_id,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )
        if row is None:
            raise InboxError("inbox_item_not_found", "The inbox item does not exist", status_code=404)
        disposition, packet, watch, goal = row
        if packet is None or watch is None or goal is None:
            raise InboxError("stale_binding", "The inbox item's source binding is stale", recovery_action="refresh_inbox")
        run = (
            await db.execute(
                select(WorkflowRunState).where(
                    WorkflowRunState.run_identity == packet.run_identity,
                )
            )
        ).scalars().first()
        status, refs = _artifact_status(packet, watch, run)
        if status != "verified":
            raise InboxError("artifact_blocked", "The verified local evidence is missing or changed", recovery_action="recover_source_watch", state="blocked")
        return (
            disposition.source_digest,
            packet.id,
            watch.id,
            goal.id,
            str(packet.run_identity),
            str(refs[0]["file_path"]),
            str(refs[1]["file_path"]),
            str(refs[0]["artifact_id"]),
            str(refs[1]["artifact_id"]),
            str(refs[0]["sha256"]),
            str(refs[1]["sha256"]),
        )


async def _apply_mail_action(
    *,
    owner_principal_id: str,
    owner_session_id: str,
    item_id: str,
    action: str,
    expected_revision: int,
    idempotency_key: str,
    until: datetime | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """Apply an existing inbox action to a metadata-only Mail notice.

    Mail notices have no source dossier to accept and no model action to
    authorize.  Acceptance therefore only confirms the already-created (or
    creates one) neutral triage task and records the normal inbox receipt.
    """

    normalized_until = _utc(until)
    normalized_reason = _safe_action_reason(reason)
    payload_digest = _digest(
        _json(
            {
                "action": action,
                "expected_revision": expected_revision,
                "idempotency_key": idempotency_key,
                "item_id": item_id,
                "reason": normalized_reason,
                "until": _iso(normalized_until),
            }
        )
    )
    safe_reason = normalized_reason
    if safe_reason:
        safe_reason = await vault_redaction.redact_secrets_in_text(
            safe_reason,
            fail_closed=True,
        )
        safe_reason = str(safe_reason)[:500]
    owner = WorkBoardOwner(principal_id=owner_principal_id, session_id=owner_session_id)
    async with db_engine.get_session() as db:
        await _begin_sqlite_immediate(db)
        replay = (
            await db.execute(
                select(GuardianInboxAction).where(
                    GuardianInboxAction.owner_principal_id == owner_principal_id,
                    GuardianInboxAction.owner_session_id == owner_session_id,
                    GuardianInboxAction.idempotency_key == idempotency_key,
                )
            )
        ).scalars().first()
        if replay is not None:
            if replay.item_id != item_id or replay.payload_digest != payload_digest:
                raise InboxError("idempotency_conflict", "The idempotency key was used with another payload")
            return json.loads(replay.safe_result_json or "{}")
        row = await _load_mail_projection_row(
            db,
            item_id=item_id,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )
        if row is None:
            raise InboxError("inbox_item_not_found", "The inbox item does not exist", status_code=404)
        disposition, binding, state, goal, consent, connection, message = row
        now = _now()
        if disposition.revision != expected_revision:
            raise InboxError(
                "stale_revision",
                "The inbox item changed before this action was applied",
                current_revision=disposition.revision,
                state=disposition.state,
                recovery_action="refresh_inbox",
            )
        if disposition.state in {"pending", "snoozed"} and _utc(disposition.expires_at) <= now:
            disposition.state = "expired"
            disposition.revision += 1
            disposition.updated_at = now
            db.add(disposition)
            await db.flush()
            await db.commit()
            raise InboxError(
                "expired",
                "The inbox item's authority has expired",
                recovery_action="configure_new_watch",
                state="expired",
                current_revision=disposition.revision,
            )
        if action == "snooze":
            if normalized_until is None:
                raise InboxError("snooze_until_required", "Snooze requires until", status_code=422)
            if normalized_until < now + timedelta(minutes=15) or normalized_until > now + timedelta(days=7):
                raise InboxError("invalid_snooze_window", "Snooze must be between 15 minutes and 7 days", status_code=422)
        if disposition.state == "snoozed":
            snoozed_until = _utc(disposition.snoozed_until)
            if snoozed_until is None or snoozed_until > now:
                raise InboxError(
                    "snoozed",
                    "The inbox item is snoozed until its next review time",
                    recovery_action="wait_for_snooze",
                    state="snoozed",
                    current_revision=disposition.revision,
                )
        if disposition.state not in {"pending", "snoozed"}:
            raise InboxError(
                "inbox_item_unavailable",
                "The inbox item has already been resolved",
                state=disposition.state,
                recovery_action="open_task" if disposition.task_id else "refresh_inbox",
                current_revision=disposition.revision,
            )
        authority_ok, authority_reason, authority_recovery = _mail_authority_projection(
            disposition,
            binding,
            state,
            goal,
            consent,
            connection,
            message,
            now=now,
        )
        if not authority_ok:
            raise InboxError(
                "stale_authority",
                authority_reason or "The Mail watch authority changed",
                recovery_action=authority_recovery or "refresh_mail_watch",
                state="blocked",
            )
        if action == "snooze":
            assert normalized_until is not None
            if normalized_until > _utc(disposition.expires_at):
                raise InboxError(
                    "snooze_exceeds_expiry",
                    "Snooze cannot outlive the inbox authority",
                    recovery_action="configure_new_watch",
                )
            result_state = "snoozed"
            task_id = disposition.task_id
        elif action == "dismiss":
            result_state = "dismissed"
            task_id = disposition.task_id
        else:
            source_key = str(disposition.source_id)
            existing_task = None
            if disposition.task_id:
                existing_task = await db.get(WorkBoardTask, disposition.task_id)
                if existing_task is not None and (
                    existing_task.owner_principal_id != owner_principal_id
                    or existing_task.owner_session_id != owner_session_id
                    or existing_task.goal_id != disposition.goal_id
                    or existing_task.capability_id is not None
                ):
                    existing_task = None
            if existing_task is None:
                existing_task = (
                    await db.execute(
                        select(WorkBoardTask).where(
                            WorkBoardTask.owner_principal_id == owner_principal_id,
                            WorkBoardTask.owner_session_id == owner_session_id,
                            WorkBoardTask.idempotency_scope == f"guardian-inbox:{disposition.id}",
                            WorkBoardTask.idempotency_key == source_key,
                        )
                    )
                ).scalars().first()
            if existing_task is None:
                try:
                    mutation = await WorkBoardRepository().create_task(
                        db,
                        owner,
                        WorkBoardTaskCreate(
                            title="New message in watched mailbox",
                            body="A new message is available in the private Mail view.",
                            goal_id=disposition.goal_id,
                            goal_revision=disposition.goal_revision,
                            status=WorkBoardStatus.triage,
                            priority=55,
                            idempotency_scope=f"guardian-inbox:{disposition.id}",
                            idempotency_key=source_key,
                        ),
                        origin_session_id=owner_session_id,
                    )
                except BoardError as exc:
                    raise InboxError(exc.code, exc.message, status_code=exc.status_code) from exc
                existing_task = mutation.task
            task_id = existing_task.task_id
            result_state = "accepted"
        result_revision = int(disposition.revision) + 1
        receipt = GuardianInboxAction(
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            item_id=disposition.id,
            idempotency_key=idempotency_key,
            payload_digest=payload_digest,
            action=action,
            prior_revision=disposition.revision,
            result_revision=result_revision,
            task_id=task_id,
            safe_result_json="{}",
            safe_reason=safe_reason,
        )
        result = {
            "id": disposition.id,
            "revision": result_revision,
            "state": result_state,
            "task_id": task_id,
            "receipt_id": receipt.id,
            "recovery_action": "open_task" if task_id else None,
        }
        receipt.safe_result_json = _json(result)
        disposition.state = result_state
        disposition.revision = result_revision
        disposition.snoozed_until = normalized_until if action == "snooze" else None
        disposition.task_id = task_id or disposition.task_id
        disposition.last_action_receipt_id = receipt.id
        disposition.updated_at = now
        db.add(receipt)
        db.add(disposition)
        await db.flush()
        return result


async def apply_action(
    *,
    owner_principal_id: str,
    owner_session_id: str,
    item_id: str,
    action: str,
    expected_revision: int,
    idempotency_key: str,
    until: datetime | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    if action not in {"accept_followup", "snooze", "dismiss"}:
        raise InboxError("invalid_action", "The requested inbox action is unsupported", status_code=422)
    if not _SAFE_KEY.fullmatch(idempotency_key or ""):
        raise InboxError("invalid_idempotency_key", "idempotency_key must be a bounded safe key", status_code=422)
    if expected_revision < 1:
        raise InboxError("invalid_revision", "expected_revision must be positive", status_code=422)
    if until is not None and (until.tzinfo is None or until.utcoffset() is None):
        raise InboxError(
            "invalid_until",
            "until must include an explicit UTC offset",
            status_code=422,
        )
    normalized_until = _utc(until)
    normalized_reason = _safe_action_reason(reason)
    async with db_engine.get_session() as lookup_db:
        source_kind = (
            await lookup_db.execute(
                select(GuardianInboxDisposition.source_kind).where(
                    GuardianInboxDisposition.id == item_id,
                    GuardianInboxDisposition.owner_principal_id == owner_principal_id,
                    GuardianInboxDisposition.owner_session_id == owner_session_id,
                )
            )
        ).scalar_one_or_none()
    if source_kind == MAIL_SOURCE_KIND:
        return await _apply_mail_action(
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            item_id=item_id,
            action=action,
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            until=until,
            reason=reason,
        )
    payload_digest = _digest(
        _json(
            {
                "action": action,
                "expected_revision": expected_revision,
                "idempotency_key": idempotency_key,
                "item_id": item_id,
                "reason": normalized_reason,
                "until": _iso(normalized_until),
            }
        )
    )
    owner = WorkBoardOwner(principal_id=owner_principal_id, session_id=owner_session_id)

    # A replay is authoritative even if the source artifact or goal has since
    # changed.  Acquire the writer lock before the idempotency lookup.
    async with db_engine.get_session() as db:
        await _begin_sqlite_immediate(db)
        replay = (
            await db.execute(
                select(GuardianInboxAction).where(
                    GuardianInboxAction.owner_principal_id == owner_principal_id,
                    GuardianInboxAction.owner_session_id == owner_session_id,
                    GuardianInboxAction.idempotency_key == idempotency_key,
                )
            )
        ).scalars().first()
        if replay is not None:
            if replay.item_id != item_id or replay.payload_digest != payload_digest:
                raise InboxError("idempotency_conflict", "The idempotency key was used with another payload")
            return json.loads(replay.safe_result_json or "{}")

    # Artifact bytes are verified outside the write lock.  Immutable metadata
    # is re-read under the lock below before any task or receipt is persisted.
    verified_artifacts: tuple[str, ...] | None = None
    if action == "accept_followup":
        verified_artifacts = await _verify_accept_artifacts(
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            item_id=item_id,
        )
    if action == "snooze":
        if normalized_until is None:
            raise InboxError("snooze_until_required", "Snooze requires until", status_code=422)
        now = _now()
        if normalized_until < now + timedelta(minutes=15) or normalized_until > now + timedelta(days=7):
            raise InboxError("invalid_snooze_window", "Snooze must be between 15 minutes and 7 days", status_code=422)
    # Persist only the bounded, server-redacted reason for every action.  The
    # digest intentionally uses ``normalized_reason`` above so replay and
    # M1 payload identity remain unchanged by redaction output.
    safe_reason = normalized_reason
    if safe_reason:
        safe_reason = await vault_redaction.redact_secrets_in_text(
            safe_reason,
            fail_closed=True,
        )
        safe_reason = str(safe_reason)[:500]

    async with db_engine.get_session() as db:
        await _begin_sqlite_immediate(db)
        replay = (
            await db.execute(
                select(GuardianInboxAction).where(
                    GuardianInboxAction.owner_principal_id == owner_principal_id,
                    GuardianInboxAction.owner_session_id == owner_session_id,
                    GuardianInboxAction.idempotency_key == idempotency_key,
                )
            )
        ).scalars().first()
        if replay is not None:
            if replay.item_id != item_id or replay.payload_digest != payload_digest:
                raise InboxError("idempotency_conflict", "The idempotency key was used with another payload")
            return json.loads(replay.safe_result_json or "{}")
        row = await _load_projection_row(
            db,
            item_id=item_id,
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
        )
        if row is None:
            raise InboxError("inbox_item_not_found", "The inbox item does not exist", status_code=404)
        disposition, packet, watch, goal = row
        now = _now()
        if action == "snooze":
            assert normalized_until is not None
            if normalized_until < now + timedelta(minutes=15) or normalized_until > now + timedelta(days=7):
                raise InboxError("invalid_snooze_window", "Snooze must be between 15 minutes and 7 days", status_code=422)
        if disposition.revision != expected_revision:
            raise InboxError(
                "stale_revision",
                "The inbox item changed before this action was applied",
                current_revision=disposition.revision,
                state=disposition.state,
                recovery_action="refresh_inbox",
            )
        if disposition.state in {"pending", "snoozed"} and _utc(disposition.expires_at) <= now:
            disposition.state = "expired"
            disposition.revision += 1
            disposition.updated_at = now
            db.add(disposition)
            await db.flush()
            # Preserve the explicit expiry transition before returning the
            # structured conflict; the session context rolls back exceptions.
            await db.commit()
            raise InboxError("expired", "The inbox item's authority has expired", recovery_action="configure_new_watch", state="expired", current_revision=disposition.revision)
        if disposition.state == "snoozed":
            snoozed_until = _utc(disposition.snoozed_until)
            if snoozed_until is None or snoozed_until > now:
                raise InboxError(
                    "snoozed",
                    "The inbox item is snoozed until its next review time",
                    recovery_action="wait_for_snooze",
                    state="snoozed",
                    current_revision=disposition.revision,
                )
        if disposition.state not in {"pending", "snoozed"}:
            raise InboxError(
                "inbox_item_unavailable",
                "The inbox item has already been resolved",
                state=disposition.state,
                recovery_action="open_task" if disposition.task_id else "refresh_inbox",
                current_revision=disposition.revision,
            )
        if packet is None or watch is None or goal is None:
            raise InboxError("stale_binding", "The inbox item's source binding is stale", recovery_action="refresh_inbox")
        if (
            str(watch.owner_principal_id) != owner_principal_id
            or str(watch.owner_session_id) != owner_session_id
            or str(goal.owner_principal_id or "") != owner_principal_id
            or str(goal.owner_session_id or "") != owner_session_id
            or packet.input_digest != disposition.source_digest
            or packet.status != "succeeded"
            or packet.verification_status != "passed"
            or not _parse_material_keys(packet)
        ):
            raise InboxError("stale_authority", "The source or goal authority changed", recovery_action="review_goal_and_watch", state="blocked")
        authority_ok, authority_reason, authority_recovery = _authority_projection(
            disposition,
            packet,
            watch,
            goal,
            now=now,
        )
        if not authority_ok:
            raise InboxError(
                "stale_authority",
                authority_reason or "The source or goal authority changed",
                recovery_action=authority_recovery or "review_goal_and_watch",
                state="blocked",
            )
        if action == "snooze":
            assert normalized_until is not None
            if normalized_until > _utc(disposition.expires_at):
                raise InboxError("snooze_exceeds_expiry", "Snooze cannot outlive the inbox authority", recovery_action="configure_new_watch")
            result_state = "snoozed"
            task_id = None
        elif action == "dismiss":
            result_state = "dismissed"
            task_id = None
        else:
            assert verified_artifacts is not None
            (
                verified_source_digest,
                verified_packet_id,
                verified_watch_id,
                verified_goal_id,
                verified_run_identity,
                verified_dossier_path,
                verified_task_path,
                verified_dossier_artifact_id,
                verified_task_artifact_id,
                verified_dossier_sha,
                verified_task_sha,
            ) = verified_artifacts
            if (
                verified_source_digest != disposition.source_digest
                or verified_packet_id != packet.id
                or verified_watch_id != watch.id
                or verified_goal_id != goal.id
                or verified_run_identity != str(packet.run_identity)
                or verified_dossier_path != str(packet.dossier_path)
                or verified_task_path != str(packet.task_path)
                or verified_dossier_artifact_id != str(packet.dossier_artifact_id)
                or verified_task_artifact_id != str(packet.task_artifact_id)
                or verified_dossier_sha != str(packet.dossier_sha256)
                or verified_task_sha != str(packet.task_sha256)
            ):
                raise InboxError("artifact_stale", "The local evidence changed during acceptance", recovery_action="refresh_inbox", state="blocked")
            # The byte verification above is intentionally outside this write
            # transaction.  Re-read the canonical durable job and its
            # immutable packet bindings while the SQLite writer lock is held;
            # a changed job, revision, lease, or unknown effect cannot race a
            # verified artifact into a committed board task.
            locked_run = (
                await db.execute(
                    select(WorkflowRunState).where(
                        WorkflowRunState.run_identity == packet.run_identity,
                    )
                )
            ).scalars().first()
            if not _job_has_verified_readbacks(locked_run, packet=packet, watch=watch):
                raise InboxError(
                    "readback_blocked",
                    "The durable source readback is no longer authoritative",
                    recovery_action="recover_source_watch",
                    state="blocked",
                )
            task_request = WorkBoardTaskCreate(
                title="Review watched source change",
                body=(
                    "Verified local dossier is ready. "
                    f"source_packet_id={packet.id}; "
                    f"dossier_artifact_id={packet.dossier_artifact_id}; "
                    f"task_artifact_id={packet.task_artifact_id}; "
                    f"source_digest={packet.input_digest}."
                ),
                goal_id=goal.id,
                goal_revision=goal.revision,
                status=WorkBoardStatus.triage,
                idempotency_scope=f"guardian-inbox:{disposition.id}",
                idempotency_key="accept",
            )
            try:
                mutation = await WorkBoardRepository().create_task(db, owner, task_request)
            except BoardError as exc:
                raise InboxError(exc.code, exc.message, status_code=exc.status_code) from exc
            task_id = mutation.task.task_id
            result_state = "accepted"
        result_revision = int(disposition.revision) + 1
        receipt = GuardianInboxAction(
            owner_principal_id=owner_principal_id,
            owner_session_id=owner_session_id,
            item_id=disposition.id,
            idempotency_key=idempotency_key,
            payload_digest=payload_digest,
            action=action,
            prior_revision=disposition.revision,
            result_revision=result_revision,
            task_id=task_id,
            safe_result_json="{}",
            safe_reason=safe_reason,
        )
        result = {
            "id": disposition.id,
            "revision": result_revision,
            "state": result_state,
            "task_id": task_id,
            "receipt_id": receipt.id,
            "recovery_action": "open_task" if task_id else None,
        }
        receipt.safe_result_json = _json(result)
        disposition.state = result_state
        disposition.revision = result_revision
        disposition.snoozed_until = normalized_until if action == "snooze" else None
        disposition.task_id = task_id or disposition.task_id
        disposition.last_action_receipt_id = receipt.id
        disposition.updated_at = now
        db.add(receipt)
        db.add(disposition)
        await db.flush()
        return result


__all__ = [
    "InboxError",
    "apply_action",
    "ensure_inbox_disposition",
    "expire_inbox_items",
    "get_owned_item",
    "list_owned_items",
    "repair_inbox_dispositions",
]
