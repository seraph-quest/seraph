"""Post-commit delivery for persisted work-board events."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from datetime import datetime

from src.db.models import WorkBoardEvent, WorkBoardStatus
from src.work_board.repository import safe_workflow_run_id

logger = logging.getLogger(__name__)
_SAFE_EVENT_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SAFE_EVENT_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SAFE_EVENT_STATUSES = frozenset(item.value for item in WorkBoardStatus)
_SAFE_EVENT_BLOCK_KINDS = frozenset(
    {
        "operator",
        "unknown_effect",
        "cost_liability",
        "reconcile_admission_binding",
        "capability",
        "needs_input",
        "transient",
        "cancelled",
    }
)
_SAFE_EVENT_OUTCOMES = frozenset(
    {
        "accepted",
        "queued",
        "running",
        "succeeded",
        "degraded",
        "settled",
        "failed",
        "blocked",
        "cancelled",
        "awaiting_approval",
        "needs_input",
        "capability",
        "transient",
        "read_back",
        "reconciled",
        "verified",
        "unknown",
        "unknown_external_effect",
        "cost_liability",
        "intent",
        "dispatched",
        "no_external_effect",
        "not_dispatched",
    }
)
_SAFE_EVENT_RECOVERY_ACTIONS = frozenset(
    {
        "unblock",
        "retry",
        "cancel",
        "approve_existing_run",
        "reconcile_external_effect",
        "reconcile_admission_binding",
        "restore_prerequisite",
    }
)
_SAFE_EVENT_CHANGED_FIELDS = frozenset(
    {
        "title",
        "body",
        "priority",
        "capability_id",
        "typed_input_ref",
        "typed_input_digest",
        "executor_id",
        "assignee_id",
        "scheduled_at",
        "status",
        "task_revision",
        "updated_at",
    }
)
_SAFE_EVENT_REFERENCE_FIELDS = frozenset(
    {"parent_task_id", "child_task_id", "comment_id", "attempt_id", "workflow_run_id"}
)


def _safe_event_metadata(value: object) -> dict[str, object]:
    """Redact legacy event rows again at the API and websocket boundary."""
    if not isinstance(value, dict):
        return {}
    safe: dict[str, object] = {}
    for key, candidate in value.items():
        if key in {"task_revision", "expected_revision", "event_id"}:
            if isinstance(candidate, int) and not isinstance(candidate, bool) and 0 <= candidate <= 2**63 - 1:
                safe[key] = candidate
        elif key == "ready_demoted":
            if isinstance(candidate, bool):
                safe[key] = candidate
        elif key in {"status", "from_status"}:
            if isinstance(candidate, str) and candidate in _SAFE_EVENT_STATUSES:
                safe[key] = candidate
        elif key == "block_kind":
            if isinstance(candidate, str) and candidate in _SAFE_EVENT_BLOCK_KINDS:
                safe[key] = candidate
        elif key in {"outcome", "reason_code", "error_code"}:
            if isinstance(candidate, str) and candidate in _SAFE_EVENT_OUTCOMES:
                safe[key] = candidate
        elif key == "recovery_action":
            if isinstance(candidate, str) and candidate in _SAFE_EVENT_RECOVERY_ACTIONS:
                safe[key] = candidate
        elif key in _SAFE_EVENT_REFERENCE_FIELDS:
            reference = safe_workflow_run_id(candidate)
            if reference is not None:
                safe[key] = reference
        elif key == "body_digest":
            if isinstance(candidate, str) and _SAFE_EVENT_DIGEST.fullmatch(candidate.lower()):
                safe[key] = candidate.lower()
        elif key == "changed_fields":
            if isinstance(candidate, (list, tuple)):
                fields = [
                    item
                    for item in candidate[:32]
                    if isinstance(item, str) and item in _SAFE_EVENT_CHANGED_FIELDS
                ]
                if fields:
                    safe[key] = fields
    return safe


def _safe_event_kind(value: object) -> str:
    if isinstance(value, str) and _SAFE_EVENT_TOKEN.fullmatch(value):
        return value
    return "event.unknown"


def _event_payload(event: WorkBoardEvent) -> dict[str, object]:
    try:
        metadata = json.loads(event.metadata_json or "{}")
    except (TypeError, ValueError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    created_at = event.created_at
    if isinstance(created_at, datetime):
        created_at = created_at.isoformat()
    elif hasattr(created_at, "value"):
        created_at = created_at.value
    return {
        "event_id": event.event_id,
        "task_id": event.task_id,
        "kind": _safe_event_kind(event.kind),
        "metadata": _safe_event_metadata(metadata),
        "created_at": created_at,
    }


async def publish_work_board_events(events: Iterable[WorkBoardEvent]) -> None:
    """Deliver committed, redacted events to their authenticated live session.

    Database persistence is authoritative.  A socket or serializer failure
    must not turn an already committed operator action into an API failure;
    clients can recover from the persisted event cursor after reconnect.
    """
    try:
        from src.scheduler.connection_manager import ws_manager

        for event in events:
            if event.event_id is None:
                continue
            await ws_manager.broadcast_work_board_event(
                _event_payload(event),
                owner_principal_id=event.owner_principal_id,
                operator_session_id=event.owner_session_id,
            )
    except Exception:
        logger.exception("Committed work-board events could not be queued for live delivery")
