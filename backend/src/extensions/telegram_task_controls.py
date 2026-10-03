"""Finite paired Telegram controls over canonical Work and approval records.

No bot runtime, public webhook, model call or remote approval authority lives
here. Transport delivery, decision outcome and canonical task truth are separate.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, update
from sqlmodel import select

from config.settings import settings
from src.approval.repository import approval_decision_digest, approval_repository
from src.db import engine as db_engine
from src.db.models import (ApprovalRequest, AuditEvent, Goal, Message, OperatorSession,
    TelegramTaskCallback, TelegramTransportOutbox, TelegramTransportState,
    WorkBoardAttempt, WorkBoardEvent, WorkBoardStatus, WorkBoardTask, WorkflowRunState)
from src.workspace import canonical_workspace_root_identity
from src.work_board.contracts import WorkBoardOwner
from src.work_board.repository import _begin_sqlite_immediate

NOTICE = "A Seraph task needs attention. Review its current status."
PREFIX = "stc1:"
TTL_SECONDS = 300
MAX_DETAIL_BYTES = 1024
MAX_CALLBACK_BYTES = 8192


class TaskControlResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capability_id: Literal["telegram.task-control.v1"] = "telegram.task-control.v1"
    status: Literal["reviewed", "denied", "cancelled", "unknown"]
    effect: Literal["review", "deny", "cancel"]
    task_status: WorkBoardStatus
    task_revision: int = Field(ge=1)
    memory_status: Literal["no_learning"] = "no_learning"
    cockpit_required_for: list[str] = Field(default_factory=lambda: ["approve", "retry", "unblock", "reconcile"])


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
        default=lambda item: aware(item).isoformat() if isinstance(item, datetime) else str(item)).encode()).hexdigest()


def aware(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def now():
    return datetime.now(timezone.utc)


def fail(code: str):
    from src.extensions.telegram_transport import TelegramTransportError
    raise TelegramTransportError(code, "Task control unavailable. Review the current task in the cockpit.")


def root_digest():
    try:
        return digest(canonical_workspace_root_identity(settings.workspace_dir))
    except Exception:
        fail("telegram_root_unavailable")


def effect_digest(row):
    return digest([row.owner_principal_id, row.operator_session_id, row.pairing_id,
        row.transit_reference, row.actor_id, row.chat_id, row.root_digest,
        row.task_id, row.task_revision, row.goal_id, row.goal_revision, row.effect,
        row.approval_id, row.approval_digest, row.attempt_id, row.board_fence,
        row.lease_owner, row.workflow_run_id, row.workflow_binding_digest,
        row.outbox_id, row.nonce_digest, row.expires_at])


def workflow_binding_digest(run):
    return digest([run.run_identity, run.owner_principal_id, run.owner_kind,
        run.operator_session_id, run.goal_id, run.goal_revision, run.job_kind,
        run.capability_version, run.input_digest, run.authority_digest,
        run.declared_authority_json, run.run_fingerprint])


async def current(db, owner: str, session: str):
    """Database-only current authority checks inside the canonical writer."""
    t = now()
    from src.auth.service import authenticate_principal, AuthFailure
    from src.security.trust_contract import AuthorityGrant
    try:
        operator = await authenticate_principal(owner, db=db)
    except AuthFailure:
        fail("telegram_owner_reconnect_required")
    if (operator.session_id != session or AuthorityGrant.INGRESS not in operator.principal.grants
        or AuthorityGrant.CAPABILITY_EXECUTE not in operator.principal.grants):
        fail("telegram_control_forbidden")
    auth = await db.get(OperatorSession, session)
    if (auth is None or auth.principal_id != owner or auth.revoked_at is not None
        or auth.replaced_by_id or auth.is_bearer_tombstone
        or aware(auth.idle_expires_at) <= t or aware(auth.absolute_expires_at) <= t):
        fail("telegram_owner_reconnect_required")
    pairing = await db.get(TelegramTransportState, "telegram")
    if (pairing is None or pairing.owner_principal_id != owner
        or pairing.operator_session_id != session or pairing.pairing_state != "active"
        or pairing.revoked_at is not None or not pairing.pairing_id
        or (pairing.pairing_expires_at and aware(pairing.pairing_expires_at) <= t)
        or not pairing.transit_consent_reference or not pairing.transit_consent_expires_at
        or aware(pairing.transit_consent_expires_at) <= t):
        fail("telegram_pairing_or_consent_unavailable")
    return pairing


async def task_owned(db, owner, session, task_id):
    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
    if task is None or task.owner_principal_id != owner or task.owner_session_id != session:
        fail("telegram_task_not_found")
    goal = await db.get(Goal, task.goal_id)
    if (goal is None or goal.owner_principal_id != owner or goal.owner_session_id != session
        or goal.status != "active" or goal.revision != task.goal_revision):
        fail("telegram_goal_changed")
    return task


async def approval_for_task(db, task, approval_id=None):
    candidates = (await db.execute(select(ApprovalRequest).where(
        ApprovalRequest.owner_principal_id == task.owner_principal_id,
        ApprovalRequest.operator_session_id == task.owner_session_id,
        ApprovalRequest.status == "pending").limit(100))).scalars().all()
    attempts = (await db.execute(select(WorkBoardAttempt).where(
        WorkBoardAttempt.task_id == task.task_id))).scalars().all()
    job_ids = {a.workflow_run_id for a in attempts if a.workflow_run_id}
    for approval in candidates:
        if approval_id and approval.id != approval_id:
            continue
        try:
            details = json.loads(approval.details_json or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(details, dict):
            continue
        linked = details.get("work_board_task_id", details.get("board_task_id")) == task.task_id
        # Native job authority provides the canonical link when approval details
        # do not duplicate the board identifier.
        for job_id in job_ids:
            job = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id))
            if job is None or job.owner_principal_id != task.owner_principal_id:
                continue
            try:
                authority = json.loads(job.declared_authority_json or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(authority, dict):
                continue
            linked = linked or (authority.get("board_task_id") == task.task_id
                and (authority.get("approval_id") == approval.id
                     or details.get("job_id") == job_id))
        if linked and (not approval.expires_at or aware(approval.expires_at) > now()):
            return approval
    if approval_id:
        fail("telegram_approval_binding_changed")
    return None


def result(task, *, status="reviewed", effect="review"):
    return TaskControlResult(status=status, effect=effect, task_status=task.status,
        task_revision=task.task_revision).model_dump(mode="json")


class TelegramTaskControls:
    def __init__(self, adapter):
        self.adapter = adapter

    async def _mint(self, db, pairing, task, outbox, effect, *, approval=None, attempt=None):
        wire = PREFIX + secrets.token_urlsafe(32)
        expiry = now() + timedelta(seconds=TTL_SECONDS)
        auth = await db.get(OperatorSession, task.owner_session_id)
        for value in [pairing.pairing_expires_at, pairing.transit_consent_expires_at,
                      approval.expires_at if approval else None,
                      auth.idle_expires_at, auth.absolute_expires_at]:
            if value:
                expiry = min(expiry, aware(value))
        workflow = await db.scalar(select(WorkflowRunState).where(
            WorkflowRunState.run_identity == attempt.workflow_run_id)) if attempt else None
        if attempt and workflow is None:
            fail("telegram_cancel_binding_changed")
        row = TelegramTaskCallback(nonce_digest=digest(wire),
            owner_principal_id=task.owner_principal_id, operator_session_id=task.owner_session_id,
            pairing_id=pairing.pairing_id, transit_reference=pairing.transit_consent_reference,
            actor_id=pairing.operator_id, chat_id=pairing.chat_id, root_digest=root_digest(),
            task_id=task.task_id, task_revision=task.task_revision, goal_id=task.goal_id,
            goal_revision=task.goal_revision, outbox_id=outbox.id, effect=effect,
            effect_digest="", approval_id=approval.id if approval else None,
            approval_digest=approval_decision_digest(approval) if approval else None,
            attempt_id=attempt.attempt_id if attempt else None,
            workflow_run_id=attempt.workflow_run_id if attempt else None,
            workflow_binding_digest=workflow_binding_digest(workflow) if workflow else None,
            board_fence=attempt.fencing_token if attempt else None,
            lease_owner=attempt.lease_owner if attempt else None, expires_at=expiry)
        row.effect_digest = effect_digest(row)
        db.add(row)
        return {"text": {"review": "Review status (send metadata)", "deny": "Deny this request",
                         "cancel": "Cancel this attempt"}[effect], "callback_data": wire}

    async def notice(self, task_id, *, owner_principal_id, operator_session_id,
                     expected_revision: int, idempotency_key: str):
        # Initial read avoids copying any private task content into chat. The
        # final writer rechecks authority and revisions before minting markup.
        async with db_engine.get_session() as db:
            task = await task_owned(db, owner_principal_id, operator_session_id, task_id)
            if task.task_revision != expected_revision:
                fail("telegram_task_revision_changed")
            await current(db, owner_principal_id, operator_session_id)
        payload = await self.adapter.enqueue_outbound(NOTICE,
            owner_principal_id=owner_principal_id, operator_session_id=operator_session_id,
            idempotency_key=f"telegram-task:{task_id}:{expected_revision}:{idempotency_key}")
        async with db_engine.get_session() as db:
            await _begin_sqlite_immediate(db)
            pairing = await current(db, owner_principal_id, operator_session_id)
            task = await task_owned(db, owner_principal_id, operator_session_id, task_id)
            if task.task_revision != expected_revision:
                fail("telegram_task_revision_changed")
            outbox = await db.get(TelegramTransportOutbox, payload["id"])
            if outbox.task_control_markup_json:
                return {**payload, "memory_status": "no_learning"}
            if outbox.status != "queued" or outbox.attempt_count:
                fail("telegram_notice_already_dispatched")
            # A fresh explicit notice retires all older pending callbacks for
            # this task, including those attached to uncertain delivery.
            await db.execute(update(TelegramTaskCallback).where(
                TelegramTaskCallback.task_id == task_id,
                TelegramTaskCallback.owner_principal_id == owner_principal_id,
                TelegramTaskCallback.operator_session_id == operator_session_id,
                TelegramTaskCallback.status == "pending").values(status="retired"))
            markup = {"inline_keyboard": [[await self._mint(db, pairing, task, outbox, "review")]]}
            outbox.task_control_markup_json = json.dumps(markup, sort_keys=True)
            outbox.task_control_markup_digest = digest(markup)
            db.add(outbox)
        return {**payload, "memory_status": "no_learning"}

    async def _validate(self, db, row, *, owner, session, query, update_id, request_digest,
                        revision_delta=0):
        pairing = await current(db, owner, session)
        if (row.owner_principal_id != owner or row.operator_session_id != session
            or row.pairing_id != pairing.pairing_id
            or row.transit_reference != pairing.transit_consent_reference
            or row.actor_id != pairing.operator_id or row.chat_id != pairing.chat_id
            or row.root_digest != root_digest() or aware(row.expires_at) <= now()
            or not hmac.compare_digest(row.effect_digest, effect_digest(row))):
            fail("telegram_callback_authority_changed")
        message = query["message"]
        if (query["from"]["id"] != row.actor_id or message["chat"]["id"] != row.chat_id
            or message["chat"].get("type") != "private"
            or type(message.get("date")) is not int or message["date"] <= 0):
            fail("telegram_callback_actor_mismatch")
        outbox = await db.get(TelegramTransportOutbox, row.outbox_id)
        if (outbox is None or outbox.status != "delivered" or not outbox.external_message_id
            or str(message["message_id"]) != outbox.external_message_id):
            fail("telegram_delivery_unverified")
        task = await task_owned(db, owner, session, row.task_id)
        if task.goal_id != row.goal_id or task.goal_revision != row.goal_revision:
            fail("telegram_goal_changed")
        if row.status != "pending":
            if row.query_id != query["id"] or row.request_digest != request_digest:
                fail("telegram_callback_replayed")
            return pairing, task
        if task.task_revision != row.task_revision + revision_delta:
            fail("telegram_task_revision_changed")
        if update_id <= pairing.cursor:
            fail("telegram_callback_out_of_order")
        count = await db.scalar(select(func.count()).select_from(TelegramTaskCallback).where(
            TelegramTaskCallback.pairing_id == pairing.pairing_id,
            TelegramTaskCallback.consumed_at >= now() - timedelta(seconds=60)))
        if count >= 20:
            fail("telegram_callback_rate_limited")
        return pairing, task

    async def _claim(self, db, row, query, update_id, request_digest):
        claimed = await db.execute(update(TelegramTaskCallback).where(
            TelegramTaskCallback.id == row.id, TelegramTaskCallback.status == "pending")
            .values(status="consumed", query_id=query["id"], update_id=update_id,
                    request_digest=request_digest, consumed_at=now())
            .execution_options(synchronize_session=False))
        if claimed.rowcount != 1:
            fail("telegram_callback_replayed")
        await db.refresh(row)
        pairing = await db.get(TelegramTransportState, "telegram")
        pairing.cursor = update_id
        db.add(pairing)

    async def _reply(self, db, row, task, payload, *, buttons=None):
        original = await db.get(TelegramTransportOutbox, row.outbox_id)
        text = (f"Task status: {payload['task_status']}. Revision: {payload['task_revision']}. "
                f"Decision: {payload['status']}. Sensitive details, approval and recovery require the cockpit.")
        if len(text.encode()) > MAX_DETAIL_BYTES:
            fail("telegram_detail_too_large")
        outbox = TelegramTransportOutbox(idempotency_key=f"telegram-task-result:{row.id}:{payload['status']}",
            payload_digest=digest([row.id, payload]), owner_principal_id=row.owner_principal_id,
            operator_session_id=row.operator_session_id, chat_id=row.chat_id,
            session_id=original.session_id, conversation_id=original.conversation_id,
            thread_id=original.thread_id, correlation_id=f"telegram-control:{row.id}",
            content=text, content_digest=hashlib.sha256(text.encode()).hexdigest(),
            max_attempts=self.adapter.max_attempts,
            deadline_at=min(aware(row.expires_at), now()+timedelta(seconds=300)))
        db.add(outbox)
        canonical_message = Message(session_id=original.session_id,
            conversation_id=original.conversation_id, thread_id=original.thread_id,
            owner_principal_id=row.owner_principal_id, operator_session_id=row.operator_session_id,
            channel="telegram", transport="telegram", role="assistant", content=text,
            correlation_id=outbox.correlation_id, causation_id=original.message_id,
            metadata_json=json.dumps({"telegram_task_control": payload,
                "memory_status": "no_learning"}, sort_keys=True))
        db.add(canonical_message)
        outbox.message_id = canonical_message.id
        if buttons:
            pairing = await current(db, row.owner_principal_id, row.operator_session_id)
            keyboard = []
            for effect, approval, attempt in buttons:
                keyboard.append(await self._mint(db, pairing, task, outbox, effect,
                    approval=approval, attempt=attempt))
            markup = {"inline_keyboard": [keyboard]}
            outbox.task_control_markup_json = json.dumps(markup, sort_keys=True)
            outbox.task_control_markup_digest = digest(markup)
        payload["outbox_id"] = outbox.id
        row.result_json = json.dumps(payload, sort_keys=True)
        db.add(row)
        db.add(AuditEvent(actor="user", event_type="telegram_task_control",
            summary="Paired finite task control", details_json=json.dumps({
                "effect": row.effect, "status": payload["status"], "memory_status": "no_learning"})))
        return payload

    async def callback(self, payload, *, owner_principal_id, operator_session_id):
        try:
            if len(json.dumps(payload, ensure_ascii=True).encode()) > MAX_CALLBACK_BYTES:
                fail("telegram_callback_too_large")
        except (TypeError, ValueError, RecursionError):
            fail("telegram_callback_invalid")
        query = payload.get("callback_query")
        if (not isinstance(query, dict) or not isinstance(query.get("id"), str)
            or not 1 <= len(query["id"]) <= 128 or query.get("inline_message_id")
            or not isinstance(query.get("from"), dict) or not isinstance(query.get("message"), dict)
            or not isinstance(query["message"].get("chat"), dict)
            or type(query["from"].get("id")) is not int
            or type(query["message"].get("message_id")) is not int
            or type(query["message"]["chat"].get("id")) is not int
            or type(payload.get("update_id")) is not int or payload["update_id"] < 1
            or not isinstance(query.get("data"), str) or not query["data"].startswith(PREFIX)
            or not 1 <= len(query["data"].encode()) <= 64):
            fail("telegram_callback_invalid")
        request_digest = digest([payload["update_id"], query["id"], query["from"]["id"],
            query["message"]["chat"]["id"], query["message"]["message_id"], digest(query["data"])])
        async with db_engine.get_session() as db:
            await _begin_sqlite_immediate(db)
            row = await db.scalar(select(TelegramTaskCallback).where(
                TelegramTaskCallback.nonce_digest == digest(query["data"])))
            if row is None:
                fail("telegram_callback_not_found")
            pairing, task = await self._validate(db, row, owner=owner_principal_id,
                session=operator_session_id, query=query, update_id=payload["update_id"],
                request_digest=request_digest)
            if row.status != "pending":
                response = json.loads(row.result_json)
                if row.status == "cancel_intent":
                    response = await self._cancel_readback(db, row, task)
                response = {**response, "task_status": task.status.value,
                            "task_revision": task.task_revision}
            elif row.effect == "review":
                await self._claim(db, row, query, payload["update_id"], request_digest)
                buttons = []
                approval = await approval_for_task(db, task)
                if approval:
                    buttons.append(("deny", approval, None))
                active = await db.scalar(select(WorkBoardAttempt).where(
                    WorkBoardAttempt.task_id == task.task_id, WorkBoardAttempt.ended_at.is_(None)))
                if task.status is WorkBoardStatus.running and active and active.cancel_requested_at is None:
                    buttons.append(("cancel", None, active))
                response = await self._reply(db, row, task, result(task), buttons=buttons)
            elif row.effect == "deny":
                approval = await approval_for_task(db, task, row.approval_id)
                if approval_decision_digest(approval) != row.approval_digest:
                    fail("telegram_approval_binding_changed")
                await self._claim(db, row, query, payload["update_id"], request_digest)
                denied = await approval_repository.resolve_exact_in_session(db, row.approval_id,
                    "denied", expected_digest=row.approval_digest)
                if denied is None or denied.status != "denied":
                    fail("telegram_approval_unavailable")
                response = await self._reply(db, row, task, result(task, status="denied", effect="deny"))
            elif row.effect == "cancel":
                response = None  # Claim and intent occur inside canonical request_cancel writer.
            else:
                fail("telegram_callback_effect_forbidden")
            row_id = row.id
            task_id, task_revision = row.task_id, row.task_revision
        if response is None:
            from src.api.work_board import dispatcher
            async def guard(db, actual_task, actual_attempt):
                guarded = await db.get(TelegramTaskCallback, row_id)
                await self._validate(db, guarded, owner=owner_principal_id,
                    session=operator_session_id, query=query, update_id=payload["update_id"],
                    request_digest=request_digest)
                if (guarded.status != "pending" or guarded.attempt_id != actual_attempt.attempt_id
                    or guarded.board_fence != actual_attempt.fencing_token
                    or guarded.lease_owner != actual_attempt.lease_owner
                    or actual_attempt.cancel_requested_at is not None):
                    fail("telegram_cancel_binding_changed")
                workflow = await db.scalar(select(WorkflowRunState).where(
                    WorkflowRunState.run_identity == actual_attempt.workflow_run_id))
                if (workflow is None or actual_attempt.workflow_run_id != guarded.workflow_run_id
                    or workflow_binding_digest(workflow) != guarded.workflow_binding_digest):
                    fail("telegram_cancel_binding_changed")
                await self._claim(db, guarded, query, payload["update_id"], request_digest)
                guarded.status = "cancel_intent"
                guarded.result_json = json.dumps(result(actual_task, status="unknown", effect="cancel"))
                db.add(guarded)
                return guarded.id
            await dispatcher.cancel_task(WorkBoardOwner(principal_id=owner_principal_id,
                session_id=operator_session_id), task_id, expected_revision=task_revision,
                intent_guard=guard)
            async with db_engine.get_session() as db:
                await _begin_sqlite_immediate(db)
                row = await db.get(TelegramTaskCallback, row_id)
                task = await task_owned(db, owner_principal_id, operator_session_id, task_id)
                response = await self._cancel_readback(db, row, task)
        # Acknowledgment is a transport effect only; never replay a decision if
        # this call or its response is lost. The durable result exists first.
        response = dict(response)
        response["ack_status"] = await self._ack(query["id"], owner_principal_id, operator_session_id)
        return response

    async def _cancel_readback(self, db, row, task):
        attempt = await db.scalar(select(WorkBoardAttempt).where(
            WorkBoardAttempt.task_id == task.task_id, WorkBoardAttempt.attempt_id == row.attempt_id))
        if (attempt is None or attempt.cancel_requested_at is None
            or attempt.fencing_token != row.board_fence
            or attempt.workflow_run_id != row.workflow_run_id
            or (attempt.ended_at is None and attempt.lease_owner != row.lease_owner)):
            fail("telegram_cancel_binding_changed")
        events = (await db.execute(select(WorkBoardEvent).where(
            WorkBoardEvent.task_id == task.task_id,
            WorkBoardEvent.kind == "attempt.cancel_requested"))).scalars().all()
        def exact_intent(event):
            metadata = json.loads(event.metadata_json or "{}")
            return (metadata.get("request_identity") == row.id
                and metadata.get("attempt_id") == row.attempt_id
                and metadata.get("board_fence") == row.board_fence
                and metadata.get("workflow_run_id") == row.workflow_run_id
                and metadata.get("lease_owner") == row.lease_owner)
        if not any(exact_intent(event) for event in events):
            fail("telegram_cancel_intent_unverified")
        if (attempt.ended_at is None or task.status is not WorkBoardStatus.blocked
            or task.block_kind != "cancelled" or attempt.outcome != "cancelled"):
            prior = json.loads(row.result_json)
            if prior.get("status") == "unknown" and prior.get("outbox_id"):
                return {**prior, "task_status": task.status.value, "task_revision": task.task_revision}
            return await self._reply(db, row, task, result(task, status="unknown", effect="cancel"))
        if row.status == "cancel_intent":
            row.status = "consumed"
            return await self._reply(db, row, task, result(task, status="cancelled", effect="cancel"))
        return json.loads(row.result_json)

    async def _ack(self, query_id, owner, session):
        from src.vault.repository import vault_repository
        try:
            async with db_engine.get_session() as db:
                pairing = await current(db, owner, session)
                secret_ref = pairing.token_secret_ref
            token = await vault_repository.get(secret_ref or "")
            if not token or not hasattr(self.adapter.transport, "answer_callback_query"):
                return "unavailable"
            acknowledgment = await asyncio.wait_for(self.adapter.transport.answer_callback_query(
                token=token, callback_query_id=query_id), timeout=self.adapter.effect_timeout_seconds)
            return "acknowledged" if isinstance(acknowledgment, dict) and acknowledgment.get("ok") is True else "unknown"
        except Exception:
            return "unknown"

    async def validate_delivery(self, db, outbox):
        if not outbox.task_control_markup_json:
            return
        try:
            markup = json.loads(outbox.task_control_markup_json)
        except (TypeError, ValueError):
            fail("telegram_control_delivery_unbound")
        if digest(markup) != outbox.task_control_markup_digest:
            fail("telegram_control_delivery_unbound")
        rows = (await db.execute(select(TelegramTaskCallback).where(
            TelegramTaskCallback.outbox_id == outbox.id))).scalars().all()
        if not rows:
            fail("telegram_control_delivery_unbound")
        try:
            wire_digests = {digest(button["callback_data"])
                for buttons in markup["inline_keyboard"] for button in buttons}
        except (KeyError, TypeError):
            fail("telegram_control_delivery_unbound")
        if wire_digests != {row.nonce_digest for row in rows}:
            fail("telegram_control_delivery_unbound")
        pairing = await current(db, outbox.owner_principal_id, outbox.operator_session_id)
        for row in rows:
            task = await task_owned(db, row.owner_principal_id, row.operator_session_id, row.task_id)
            if (row.status != "pending" or row.root_digest != root_digest()
                or row.pairing_id != pairing.pairing_id
                or row.transit_reference != pairing.transit_consent_reference
                or aware(row.expires_at) <= now() or row.task_revision != task.task_revision
                or effect_digest(row) != row.effect_digest):
                fail("telegram_control_delivery_stale")
            if row.approval_id:
                approval = await approval_for_task(db, task, row.approval_id)
                if approval_decision_digest(approval) != row.approval_digest:
                    fail("telegram_approval_binding_changed")
            if row.effect == "cancel":
                attempt = await db.get(WorkBoardAttempt, row.attempt_id)
                workflow = await db.scalar(select(WorkflowRunState).where(
                    WorkflowRunState.run_identity == row.workflow_run_id))
                if (attempt is None or attempt.ended_at is not None
                    or attempt.cancel_requested_at is not None
                    or attempt.workflow_run_id != row.workflow_run_id
                    or attempt.fencing_token != row.board_fence or attempt.lease_owner != row.lease_owner
                    or workflow is None or workflow_binding_digest(workflow) != row.workflow_binding_digest):
                    fail("telegram_cancel_binding_changed")
