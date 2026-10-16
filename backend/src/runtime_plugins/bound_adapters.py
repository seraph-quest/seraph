"""Gmail and Calendar bound adapters behind ``connections.invokeBoundAdapter`` (#1092).

Each finite operation runs through the existing Python owner for that data:

* ``gmail.read_message`` -> ``api.mail.read_bound_message`` (source consent, label
  scope, binding, the durable mail source control job and its encrypted artifact);
* ``calendar.read_events`` -> ``api.calendar.read_consented_events`` with
  ``publish_selection=False``: a Cordis read only observes and never publishes the
  ``selected`` bindings that ``/calendar/prep`` treats as operator authority;
* ``gmail.send_reviewed_reply`` -> ``mail_reply_runtime.execute`` under ``run_owned``,
  for a send the operator approved in the same authenticated request.

The governed job (#1018) is the outer fence; the owner's own job and callbacks are
the inner one. The child receives only the job reference. Python seals digests,
never mail or event text: plaintext reaches the Python caller once, in memory, and
Gmail's only plaintext at rest stays the owner's existing encrypted control
artifact, purged for Cordis reads on consent revocation, expiry or a 24 h TTL.
Every item is labelled external and untrusted; nothing in it carries authority.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import re

from .contracts import succeeded
from .dispatch import NativeServiceBlocked, _append_receipt, _canonical, _digest, _run_candidate, _writer, cas_run, read_context

logger = logging.getLogger(__name__)
OPERATIONS = {"gmail.read_message": "gmail_readonly", "calendar.read_events": "calendar_readonly",
              "gmail.send_reviewed_reply": None}
SEND_OPERATION = "gmail.send_reviewed_reply"
SEND_ROUTE_SCOPE = "mail_reply_cordis_execute_route"
# Failures after this invocation claimed the send-once intent: the message may have gone.
SEND_IN_DOUBT = frozenset({"mail_reply_send_in_doubt", "native_adapter_send_unverified"})
RESULT_TTL = timedelta(hours=24)
MAX_RESULT_BYTES = 64 * 1024
_TEXT_FIELDS = ("summary", "location", "description", "attendees")
_REF = re.compile(r"[A-Za-z0-9_.:-]{1,256}\Z")
_REASON = re.compile(r"[a-z0-9_]{1,96}\Z")
# Plaintext handoff to the Python caller of this exact governed job, popped once
# (also on failure) and dropped after HANDOFF_TTL if no caller ever takes it.
_handoff: dict[str, tuple[float, dict]] = {}
HANDOFF_TTL_SECONDS = 300.0
PURGE_SCAN_LIMIT = 500
PURGED_EVENT = "cordis_adapter_result_purged"


def owner_errors() -> tuple[type[BaseException], ...]:
    """The owners' own typed refusals (each carries a stable ``code``)."""
    from src.integrations.calendar_controls import CalendarControlError
    from src.integrations.gmail_controls import GmailControlError
    from src.integrations.gmail_read import GmailReadError
    from src.integrations.google_calendar import CalendarIntegrationError
    return (GmailReadError, GmailControlError, CalendarIntegrationError, CalendarControlError)


def _text(value, *, limit=256) -> bool:
    return type(value) is str and 0 < len(value) <= limit and _REF.fullmatch(value) is not None


def _positive(value) -> bool:
    return type(value) is int and value >= 1


def validate_arguments(operation: str, arguments) -> dict:
    """Closed per-operation schema; the read routes' own typed model validates Gmail."""
    if operation not in OPERATIONS or type(arguments) is not dict:
        raise NativeServiceBlocked("native_adapter_operation_unknown")
    if operation == "gmail.read_message":
        keys = {"connection_ref", "expected_connection_revision", "mail_consent_id",
                "expected_source_consent_revision", "message_binding_id", "expected_message_revision"}
        if set(arguments) != keys:
            raise NativeServiceBlocked("native_adapter_arguments_invalid")
        _message_read(arguments, request_uuid="cordis-validation")
    elif operation == "calendar.read_events":
        if (set(arguments) != {"connection_ref", "expected_connection_revision", "calendar_consent_id",
                               "expected_consent_revision", "max_events"}
                or not _text(arguments["connection_ref"]) or not _text(arguments["calendar_consent_id"])
                or not _positive(arguments["expected_connection_revision"])
                or not _positive(arguments["expected_consent_revision"])
                or type(arguments["max_events"]) is not int or not 1 <= arguments["max_events"] <= 50):
            raise NativeServiceBlocked("native_adapter_arguments_invalid")
    elif (set(arguments) != {"reply_send_job_id", "expected_reply_send_revision"}
          or not _text(arguments["reply_send_job_id"]) or not _positive(arguments["expected_reply_send_revision"])):
        raise NativeServiceBlocked("native_adapter_arguments_invalid")
    return dict(arguments)


def _message_read(arguments: dict, *, request_uuid: str):
    from pydantic import ValidationError
    from src.api.mail import MessageRead
    try:
        return MessageRead(connection_id=arguments["connection_ref"],
                           expected_connection_revision=arguments["expected_connection_revision"],
                           mail_consent_id=arguments["mail_consent_id"],
                           expected_source_consent_revision=arguments["expected_source_consent_revision"],
                           message_binding_id=arguments["message_binding_id"],
                           expected_message_revision=arguments["expected_message_revision"],
                           acknowledge_selected_body_read=True, request_uuid=request_uuid)
    except (ValidationError, TypeError) as exc:
        raise NativeServiceBlocked("native_adapter_arguments_invalid") from exc


def adapter_scope(run) -> dict:
    scope = json.loads(run.declared_authority_json or "{}").get("adapter_scope")
    return scope if isinstance(scope, dict) else {}


def _blocked(exc) -> NativeServiceBlocked:
    """An owner refusal as an operator-visible typed block (never the owner's message text)."""
    code = str(getattr(exc, "code", "") or getattr(exc, "reason_code", "") or "")
    return NativeServiceBlocked("native_adapter_" + code if _REASON.fullmatch(code) else "native_adapter_owner_refused")


def owner_of(run):
    """The governed run's own owner; a test-bypass identity is never an adapter principal."""
    from src.work_board.contracts import WorkBoardOwner
    if not run.owner_principal_id or run.owner_principal_id == "operator:test-bypass" or not run.operator_session_id:
        raise NativeServiceBlocked("native_adapter_principal_invalid")
    return WorkBoardOwner(principal_id=run.owner_principal_id, session_id=run.operator_session_id)


async def operator_of(run):
    """Re-authenticate the run's session and bind it to the session's current token."""
    from dataclasses import replace
    from src.auth.service import AuthFailure, authenticate_websocket_session
    from src.db.engine import get_session
    from src.db.models import OperatorSession
    owner_of(run)
    try:
        operator = await authenticate_websocket_session(run.operator_session_id, touch=False)
    except AuthFailure as exc:
        raise NativeServiceBlocked("native_adapter_session_unavailable") from exc
    if operator.session_id != run.operator_session_id or operator.principal.principal_id != run.owner_principal_id:
        raise NativeServiceBlocked("native_adapter_principal_invalid")
    async with get_session() as db:
        row = await db.get(OperatorSession, run.operator_session_id, populate_existing=True)
    if row is None or row.revoked_at is not None:
        raise NativeServiceBlocked("native_adapter_session_unavailable")
    return replace(operator, _token_hash=row.token_hash)


async def _connection(run, arguments, service: str) -> None:
    """The caller's current connection in the owners' vocabulary (``active``)."""
    from sqlalchemy import select
    from src.db.engine import get_session
    from src.db.models import GoogleServiceConnection
    async with get_session() as db:
        row = (await db.execute(select(GoogleServiceConnection.revision, GoogleServiceConnection.state,
                                       GoogleServiceConnection.service).where(
            GoogleServiceConnection.connection_id == arguments["connection_ref"],
            GoogleServiceConnection.owner_principal_id == run.owner_principal_id,
            GoogleServiceConnection.owner_session_id == run.operator_session_id))).one_or_none()
    if row is None or row.revision != arguments["expected_connection_revision"]:
        raise NativeServiceBlocked("native_connection_candidate_changed")
    if row.state != "active":
        raise NativeServiceBlocked("native_connection_unavailable")
    if row.service != service:
        raise NativeServiceBlocked("native_adapter_operation_unknown")


async def preflight(run, candidate) -> None:
    """Every cheap owner check, before the intent: no intent, no vault read, no contact."""
    operation, arguments = candidate["operation"], candidate["arguments"]
    scope = adapter_scope(run)
    if scope.get("operation") != operation:
        raise NativeServiceBlocked("native_adapter_scope_changed")
    owner = owner_of(run)
    if operation == SEND_OPERATION:
        if scope.get("admitted_by") != SEND_ROUTE_SCOPE:
            raise NativeServiceBlocked("native_adapter_send_route_required")
        await _send_preflight(run, arguments)
        return
    await _connection(run, arguments, OPERATIONS[operation])
    try:
        if operation == "gmail.read_message":
            from src.api.mail import _assert_live_session, preflight_bound_message
            from src.db.engine import get_session
            async with get_session() as db:
                await _assert_live_session(db, owner)
            await preflight_bound_message(owner, arguments["message_binding_id"],
                                          _message_read(arguments, request_uuid="cordis:" + run.run_identity))
        else:
            from src.api.calendar import preflight_consented_events
            consent, _connection_row = await preflight_consented_events(
                owner, connection_id=arguments["connection_ref"], consent_id=arguments["calendar_consent_id"])
            if consent.revision != arguments["expected_consent_revision"]:
                raise NativeServiceBlocked("native_adapter_consent_changed")
    except owner_errors() as exc:
        raise _blocked(exc) from exc


async def _send_preflight(run, arguments) -> None:
    from src.db.models import ApprovalRequest
    from src.integrations import mail_reply_runtime as runtime
    from src.integrations.mail_reply_send import authority, state
    operator = await operator_of(run)
    try:
        async with runtime.session() as db:
            reply = await runtime.get_run(operator, arguments["reply_send_job_id"], db=db)
            if reply.job_kind != runtime.SEND_KIND or int(reply.revision or 0) != arguments["expected_reply_send_revision"]:
                raise NativeServiceBlocked("native_adapter_send_changed")
            preview = state(reply).get("preview")
            if reply.status != "paused" or not isinstance(preview, dict) or state(reply).get("intent"):
                raise NativeServiceBlocked("mail_reply_not_awaiting_send")
            approval = await db.get(ApprovalRequest, preview["approval_id"], populate_existing=True)
            if approval is None or approval.status != "approved":
                raise NativeServiceBlocked("native_adapter_approval_required")
            # The frozen gmail_reply_read / gmail_reply_send pair and the source scope.
            await authority(db, operator, reply)
    except NativeServiceBlocked:
        raise
    except Exception as exc:
        raise _blocked(exc) from exc


# ---------------------------------------------------------------- effects

def _provenance(source_id: str, data: object, item: dict) -> dict:
    return {"origin": "external_untrusted", "instruction_authority": False, "source_id": source_id,
            "data_digest": _digest(data), **item}


def _injection(text: str) -> dict:
    from src.security.secure_host import prompt_injection_receipt
    receipt = prompt_injection_receipt(text)
    # A label, never a filter: "clear" relaxes nothing anywhere.
    return {"status": receipt["status"], "finding_codes": receipt["finding_codes"]}


def cap_events(events: list[dict]) -> tuple[list[dict], bool]:
    """A deterministic 64 KiB cap: an equal per-event byte budget for the text fields."""
    def size(items):
        return len(_canonical(items).encode())
    if size(events) <= MAX_RESULT_BYTES - 2048:
        return events, False
    budget = max(64, (MAX_RESULT_BYTES - 2048) // max(1, len(events)) - 400)
    capped = []
    for event in events:
        item = dict(event)
        for name in _TEXT_FIELDS:
            value = item.get(name)
            if isinstance(value, str) and len(value.encode()) > budget // len(_TEXT_FIELDS):
                limit = budget // len(_TEXT_FIELDS)
                item[name] = value.encode()[:limit].decode("utf-8", errors="ignore") + " [truncated]"
            elif isinstance(value, list):
                # Attendees share the event budget: at most 8 names of 64 characters.
                item[name] = [str(entry)[:64] for entry in value[:8]]
        capped.append(item)
    while size(capped) > MAX_RESULT_BYTES - 2048 and capped:
        capped.pop()
    return capped, True


async def execute(dispatcher, scope, run, candidate) -> tuple[dict, dict]:
    """The governed effect; returns (sealed digest-only outcome, in-memory plaintext result)."""
    from .governed_services import _redacted_value
    operation, arguments = candidate["operation"], candidate["arguments"]
    method = "connections.invokeBoundAdapter"

    async def outer_check() -> None:
        # The governed job's lease, fence, deadline, Root and epoch, before every contact.
        async with dispatcher.jobs._session() as db:
            await dispatcher.current(db, scope, method)
    if operation == SEND_OPERATION:
        return await _send(run, arguments, outer_check)
    owner = owner_of(run)
    try:
        if operation == "gmail.read_message":
            from src.api.mail import read_bound_message
            payload = await read_bound_message(owner, arguments["message_binding_id"],
                                               _message_read(arguments, request_uuid="cordis:" + run.run_identity),
                                               outer_check=outer_check)
            text = await _redacted_value({"subject": payload["subject"], "plain_text": payload["plain_text"]})
            item = _provenance("mail:" + payload["message_key"], text, {
                "message_key": payload["message_key"], "thread_key": payload["thread_key"],
                "message_revision": payload["message_revision"], "subject": text["subject"],
                "plain_text": text["plain_text"], "truncated": payload["truncated"],
                "received_at": payload["received_at"],
                "prompt_injection": _injection(text["subject"] + "\n" + text["plain_text"])})
            result = {"operation": operation, "items": [item], "truncated": bool(payload["truncated"]),
                      "consent_id": arguments["mail_consent_id"], "inner_job_id": payload["control_job_id"]}
        else:
            from src.api.calendar import read_consented_events
            payload = await read_consented_events(owner, connection_id=arguments["connection_ref"],
                                                  consent_id=arguments["calendar_consent_id"],
                                                  max_events=arguments["max_events"], publish_selection=False,
                                                  outer_check=outer_check,
                                                  expected_consent_revision=arguments["expected_consent_revision"])
            events, capped = cap_events(await _redacted_value(payload["events"]))
            items = [_provenance("calendar:" + event["event_key"], event, {
                **event, "prompt_injection": _injection(" ".join(str(event.get(name) or "") for name in _TEXT_FIELDS))})
                     for event in events]
            # The cap holds for the labelled items too: drop whole trailing items, never fail.
            while items and len(_canonical(items).encode()) > MAX_RESULT_BYTES:
                items.pop()
                capped = True
            result = {"operation": operation, "items": items, "truncated": bool(payload["truncated"] or capped),
                      "consent_id": arguments["calendar_consent_id"], "inner_job_id": None,
                      "selection_published": False}
    except NativeServiceBlocked:
        raise
    except owner_errors() as exc:
        raise _blocked(exc) from exc
    sealed = {"operation": operation, "result_digest": _digest(result["items"]), "item_count": len(result["items"]),
              "truncated": result["truncated"], "consent_id": result["consent_id"],
              "inner_job_id": result["inner_job_id"], "origin": "external_untrusted",
              "instruction_authority": False, "sealed_at": datetime.now(timezone.utc).isoformat(), "purged": False}
    return sealed, result


async def _send(run, arguments, outer_check) -> tuple[dict, dict]:
    from src.integrations import mail_reply_runtime as runtime
    from src.integrations.mail_reply_send import state
    operator = await operator_of(run)
    ident = arguments["reply_send_job_id"]
    report: dict = {}
    await outer_check()
    try:
        await runtime.run_owned(operator, ident, lambda: runtime.execute(operator, ident, report=report))
    except asyncio.CancelledError as exc:
        # The owner's own cancel (not ours) after the send-once intent is in doubt too.
        task = asyncio.current_task()
        if not report.get("intent_claimed") or (task is not None and task.cancelling()):
            raise
        raise NativeServiceBlocked("mail_reply_send_in_doubt") from exc
    except Exception as exc:
        if report.get("intent_claimed"):
            # After the send-once intent a failure is ambiguous: the message may have gone.
            raise NativeServiceBlocked("mail_reply_send_in_doubt") from exc
        raise _blocked(exc) from exc
    async with runtime.session() as db:
        reply = await runtime.get_run(operator, ident, db=db)
        checkpoint = state(reply)
    intent = checkpoint.get("intent") or {}
    if not report.get("intent_claimed"):
        # A send made by another caller (the UI) is never reported as this invocation's.
        raise NativeServiceBlocked("mail_reply_already_sent")
    if (reply.status != "succeeded" or checkpoint.get("outcome") != "verified_in_sender_sent_mailbox"
            or intent.get("fencing_token") != report.get("fencing_token")):
        # This invocation claimed the intent, so the message may have gone: the
        # route reports this as in doubt ("verify in Sent"), never a plain failure.
        raise NativeServiceBlocked("native_adapter_send_unverified")
    sealed = {"operation": SEND_OPERATION, "reply_send_job_id": ident, "outcome": checkpoint["outcome"],
              "intent_digest": report["intent_digest"], "fencing_token": report["fencing_token"],
              "result_digest": _digest({"outcome": checkpoint["outcome"], "intent": report["intent_digest"]}),
              "item_count": 0, "purged": False, "origin": "external_untrusted", "instruction_authority": False}
    return sealed, {"operation": SEND_OPERATION, "reply_send_job_id": ident, "outcome": checkpoint["outcome"]}


async def dispatch_adapter(dispatcher, frame, payload, scope):
    """Replay check -> owner preflight -> intent -> owner effect -> digest-only seal."""
    from .governed_services import GOVERNED_JOB_KIND, wire_inputs
    method = frame["method"]
    jobs = dispatcher.jobs
    async with jobs._session() as db:
        await _writer(db)
        run, _, _ = await dispatcher.current(db, scope, method)
        if run.job_kind != GOVERNED_JOB_KIND:
            raise NativeServiceBlocked("native_task_candidate_not_bound")
        candidate = _run_candidate(run)
        context = read_context(run)
        if payload != wire_inputs(run.run_identity, method) or candidate["method"] != method:
            raise NativeServiceBlocked("native_governed_reference_not_bound")
        # The governed replay check is the first fence (Calendar's only inner one).
        if context["operations"] or context["result"] is not None:
            raise NativeServiceBlocked("native_governed_replay_denied")
        from types import SimpleNamespace
        frozen = SimpleNamespace(**{name: getattr(run, name) for name in (
            "run_identity", "owner_principal_id", "operator_session_id", "declared_authority_json")})
        await db.rollback()
    # Owner predicates open their own sessions, so they run outside the job writer.
    await preflight(frozen, candidate)
    async with jobs._session() as db:
        await _writer(db)
        run, _, _ = await dispatcher.current(db, scope, method)
        context = read_context(run)
        if context["operations"] or context["result"] is not None:
            raise NativeServiceBlocked("native_governed_replay_denied")
        context["operations"].append({"op": "intent", "method": method, "call_ref": frame["request_id"],
                                      "no_learning": True})
        intent = {"effect_id": "cordis-governed:" + run.run_identity, "receipt_kind": "effect",
                  "effect_type": "cordis_governed_connections", "status": "intent",
                  "target_path": "cordis-governed:" + candidate["operation"], "target_digest": run.input_digest,
                  "recorded_at": datetime.now(timezone.utc).isoformat(), "fencing_token": run.fencing_token,
                  "details": {"no_learning": True}}
        await cas_run(db, run, checkpoint_context_json=_canonical(context),
                      effect_receipts_json=_append_receipt(run.effect_receipts_json, intent, key="effect_id"))
        await db.commit()
        await db.refresh(run)
    sealed, plaintext = await execute(dispatcher, scope, run, candidate)
    result = succeeded(method, {"receipt_ref": "cordis-adapter:" + hashlib.sha256(
        sealed["result_digest"].encode()).hexdigest()[:48], "artifact_refs": []})
    async with jobs._session() as db:
        await _writer(db)
        # Late or revoked: a changed attempt, deadline or authority never seals output.
        run, _, _ = await dispatcher.current(db, scope, method)
        context = read_context(run)
        if len(context["operations"]) != 1 or context["operations"][0]["call_ref"] != frame["request_id"]:
            raise NativeServiceBlocked("native_governed_operation_changed")
        context["operations"][0].update(op="settled", result_digest=_digest(result))
        # Digests only: no mail or event text ever enters the job context.
        context["result"] = {"result_digest": _digest(result), "payload": result, "outcome": sealed,
                             "no_learning": True}
        now = datetime.now(timezone.utc).isoformat()
        readback = {"effect_id": "cordis-governed:" + run.run_identity, "receipt_kind": "readback",
                    "effect_type": "cordis_governed_connections", "status": "succeeded",
                    "target_path": "cordis-adapter:" + sealed["result_digest"], "content_sha256": sealed["result_digest"],
                    "target_digest": sealed["result_digest"], "verified_at": now, "recorded_at": now,
                    "readback_id": "cordis-governed-readback:" + run.run_identity, "reconciled": True,
                    "reconciliation_status": "resolved", "fencing_token": run.fencing_token,
                    "details": {"verified": True, "no_learning": True}}
        effects = [item for item in json.loads(run.effect_receipts_json or "[]")
                   if not (isinstance(item, dict) and item.get("effect_id") == readback["effect_id"])]
        await cas_run(db, run, checkpoint_context_json=_canonical(context),
                      effect_receipts_json=_canonical([*effects, readback]))
        await db.commit()
    _expire_handoff()
    import time as _time
    _handoff[run.run_identity] = (_time.monotonic(), plaintext)
    return result


def _expire_handoff() -> None:
    import time as _time
    cutoff = _time.monotonic() - HANDOFF_TTL_SECONDS
    for job_id in [key for key, (stored, _value) in _handoff.items() if stored < cutoff]:
        _handoff.pop(job_id, None)


def take_result(job_id: str) -> dict | None:
    _expire_handoff()
    entry = _handoff.pop(job_id, None)
    return entry[1] if entry else None


# ---------------------------------------------------------------- retention

def inner_job_id(run_identity: str, owner_principal_id: str, operator_session_id: str, connection_ref: str) -> str:
    """The deterministic mail source control job of a Cordis read (``request_uuid = cordis:<run>``)."""
    from src.integrations.gmail_controls import MailSourceRequest
    return MailSourceRequest(operation="mail_message_read", owner_principal_id=owner_principal_id,
                             owner_session_id=operator_session_id, connection_id=connection_ref, connection_revision=0,
                             request_uuid="cordis:" + run_identity, request_digest="").job_id


async def purge_cordis_mail_results(*, owner=None, consent_id: str | None = None, now: datetime | None = None) -> int:
    """Delete the encrypted owner artifacts of Cordis-originated Gmail reads once their
    consent is revoked or expired, or after the TTL, whatever the read's outcome.

    The inner control job id is derived from the governed run, so a read that
    failed after the owner published its artifact is purged too. Each purge is an
    audit event. SQL selects only terminal Gmail reads with no purge audit yet, and
    the scan pages through all of them, so no number of newer runs starves an
    older unpurged read."""
    from sqlalchemy import exists, func, select, update
    from src.api.mail import _cleanup_mail_artifacts
    from src.db.engine import get_session
    from src.db.models import AuditEvent, MailReadConsent, WorkflowRunState
    from src.work_board.contracts import WorkBoardOwner
    from .governed_services import GOVERNED_JOB_KIND
    observed = now or datetime.now(timezone.utc)
    context_json = func.iif(func.json_valid(WorkflowRunState.checkpoint_context_json),
                            WorkflowRunState.checkpoint_context_json, "{}")
    purge_json = func.iif(func.json_valid(AuditEvent.details_json), AuditEvent.details_json, "{}")
    query = select(WorkflowRunState).where(
        WorkflowRunState.job_kind == GOVERNED_JOB_KIND,
        WorkflowRunState.status.not_in(("accepted", "queued", "running")),
        func.json_extract(context_json, "$.candidate.operation") == "gmail.read_message",
        ~exists().where(AuditEvent.event_type == PURGED_EVENT,
                        func.json_extract(purge_json, "$.job_id") == WorkflowRunState.run_identity))
    if owner is not None:
        query = query.where(WorkflowRunState.owner_principal_id == owner.principal_id,
                            WorkflowRunState.operator_session_id == owner.session_id)
    due: list[tuple] = []
    after = ""
    while True:
        async with get_session() as db:
            rows = (await db.execute(query.where(WorkflowRunState.id > after).order_by(WorkflowRunState.id)
                                     .limit(PURGE_SCAN_LIMIT))).scalars().all()
            for run in rows:
                try:
                    candidate = read_context(run)["candidate"]
                except NativeServiceBlocked:
                    continue
                if candidate.get("operation") != "gmail.read_message":
                    continue
                arguments = candidate["arguments"]
                consent = await db.get(MailReadConsent, arguments["mail_consent_id"])
                started = run.started_at.replace(tzinfo=run.started_at.tzinfo or timezone.utc)
                expired = (consent is None or consent.state != "active"
                           or consent.expires_at.replace(tzinfo=consent.expires_at.tzinfo or timezone.utc) <= observed)
                if consent_id == arguments["mail_consent_id"] or expired or observed - started >= RESULT_TTL:
                    due.append((run.run_identity, run.owner_principal_id, run.operator_session_id,
                                inner_job_id(run.run_identity, run.owner_principal_id, run.operator_session_id,
                                             arguments["connection_ref"])))
        if len(rows) < PURGE_SCAN_LIMIT:
            break
        after = rows[-1].id
    purged = 0
    for job_id, principal_id, session_id, inner in due:
        complete = await _cleanup_mail_artifacts(WorkBoardOwner(principal_id=principal_id, session_id=session_id),
                                                 lambda run_id, _inputs, target=inner: run_id == target,
                                                 require_live_session=False)
        if not complete:
            continue  # An active or ambiguous owner job stays for the next sweep.
        async with get_session() as db:
            run = (await db.execute(select(WorkflowRunState).where(
                WorkflowRunState.run_identity == job_id))).scalar_one_or_none()
            if run is not None and run.checkpoint_context_json:
                context = json.loads(run.checkpoint_context_json)
                outcome = (context.get("result") or {}).get("outcome")
                if isinstance(outcome, dict):
                    outcome.update(purged=True, purged_at=observed.isoformat())
                    await db.execute(update(WorkflowRunState).where(
                        WorkflowRunState.run_identity == job_id, WorkflowRunState.revision == run.revision)
                        .values(checkpoint_context_json=_canonical(context), revision=run.revision + 1))
            await _audit_purge(db, job_id, inner, observed)
        purged += 1
    return purged


async def _audit_purge(db, job_id: str, inner: str, observed: datetime) -> None:
    from src.db.models import AuditEvent
    db.add(AuditEvent(session_id=None, actor="runtime", event_type=PURGED_EVENT,
                      summary="Cordis mail result retention purge",
                      details_json=json.dumps({"job_id": job_id, "inner_job_id": inner,
                                               "purged_at": observed.isoformat()}, sort_keys=True)))


async def run_cordis_mail_retention() -> None:
    """Scheduler entry: periodic retention, so an idle server never keeps bodies past the TTL."""
    # An untaken in-memory handoff must not outlive its TTL just because no
    # later adapter call or caller came along to expire it.
    _expire_handoff()
    try:
        await purge_cordis_mail_results()
    except Exception:
        logger.warning("Cordis mail result retention sweep failed", exc_info=True)
