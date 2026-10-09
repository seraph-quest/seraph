"""Source-owned, planless capture into the existing reviewed C1 publication path.

Reservations preserve the first selected Task allowance. They authorize no
inference or effects; only the private source issuer can seal a publication.
"""
from dataclasses import dataclass
import json
from typing import Literal

from pydantic import Field, field_validator, model_validator

from src.work_board.contracts import (
    ClosedTaskModel, GeneralTaskInput, TaskLimits, TaskProposalGroupV1,
)
from src.work_board.repository import BoardError
from datetime import datetime


class TelegramCaptureSelection(ClosedTaskModel):
    expected_revision: int = Field(ge=1)
    enabled: bool = False
    goal_id: str = Field(min_length=1, max_length=128)
    goal_revision: int = Field(ge=1)
    requested_output: dict
    limits: TaskLimits = Field(default_factory=lambda: TaskLimits(max_inference_calls=0, max_cost_microusd=0))
    inference_egress_acknowledged: bool = False


class ChannelReplyContext(ClosedTaskModel):
    capture_binding_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    conversation_session_id: str = Field(min_length=1, max_length=256)


class ChannelTaskIngress(ClosedTaskModel):
    pairing_id: str = Field(min_length=1, max_length=128)
    event_id: str = Field(min_length=1, max_length=256)
    text_or_transcript_ref: str = Field(pattern=r"^message:[A-Za-z0-9_-]{1,128}$")
    attachment_refs: list[str] = Field(max_length=8)
    reply_context: ChannelReplyContext


class ChannelAction(ClosedTaskModel):
    task_id: str = Field(min_length=1, max_length=128)
    action: Literal["inspect", "pause", "resume", "cancel", "open_exact_review"]
    expected_revision: int = Field(ge=1)


class TelegramCaptureBindingV1(ClosedTaskModel):
    schema_version: Literal["telegram-task-capture-binding.v1"] = "telegram-task-capture-binding.v1"
    enabled: bool
    owner_principal_id: str = Field(min_length=1, max_length=128)
    original_root_id: str = Field(min_length=1, max_length=128)
    pairing_id: str = Field(min_length=1, max_length=128)
    transit_consent_reference: str = Field(min_length=1, max_length=128)
    workspace_identity_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    goal_id: str = Field(min_length=1, max_length=128)
    goal_revision: int = Field(ge=1)
    requested_output: dict
    limits: TaskLimits
    inference_egress_acknowledged: bool
    expires_at: datetime

    _utc_timestamp = field_validator("expires_at", mode="before")(
        TaskProposalGroupV1.utc_timestamp.__func__)


def current_workspace_digest():
    from config.settings import settings
    from src.workspace import canonical_workspace_root
    from src.work_board.general_task import digest
    root = canonical_workspace_root(settings.workspace_dir)
    identity = root.stat()
    return digest([str(root), identity.st_dev, identity.st_ino])


async def select_telegram_capture(adapter, owner, selection):
    from src.auth.service import authenticate_session
    from src.db import engine
    from src.extensions.telegram_transport import telegram_state_revision, _now, _aware
    from src.work_board.repository import WorkBoardRepository, _begin_sqlite_immediate
    from src.work_board.general_task import validate_schema, canonical, digest
    operator = await authenticate_session(owner.session_id, touch=False)
    if operator.principal.principal_id != owner.principal_id:
        raise BoardError("channel_capture_owner_changed", "Original operator changed", status_code=403)
    canonical(selection.requested_output)
    validate_schema(selection.requested_output, check_value=False)
    async with adapter._lock:
        async with engine.get_session() as db:
            await _begin_sqlite_immediate(db)
            pairing = await adapter._state(db)
            if (pairing is None or pairing.owner_principal_id != owner.principal_id
                or pairing.operator_session_id != owner.session_id):
                raise BoardError("channel_capture_pairing_changed", "Select the current owned pairing", status_code=403)
            await adapter._assert_active_row(pairing, current=_now())
            if telegram_state_revision(pairing) != selection.expected_revision:
                raise BoardError("channel_capture_selection_stale", "Refresh current pairing before selecting capture", status_code=409)
            await WorkBoardRepository()._validate_goal(db, owner, goal_id=selection.goal_id,
                goal_revision=selection.goal_revision)
            if not _aware(pairing.transit_consent_expires_at):
                raise BoardError("channel_capture_expiry_required", "Current transit expiry is required", status_code=409)
            cutoffs = [operator.idle_expires_at, operator.absolute_expires_at,
                _aware(pairing.transit_consent_expires_at)]
            if _aware(pairing.pairing_expires_at):
                cutoffs.append(_aware(pairing.pairing_expires_at))
            binding = TelegramCaptureBindingV1(enabled=selection.enabled,
                owner_principal_id=owner.principal_id, original_root_id=owner.session_id,
                pairing_id=pairing.pairing_id, transit_consent_reference=pairing.transit_consent_reference,
                workspace_identity_digest=current_workspace_digest(), goal_id=selection.goal_id,
                goal_revision=selection.goal_revision, requested_output=selection.requested_output,
                limits=selection.limits, inference_egress_acknowledged=selection.inference_egress_acknowledged,
                expires_at=min(cutoffs))
            pairing.capture_binding_json = binding.model_dump_json()
            pairing.updated_at = _now()
            await db.commit()
            return {"capture_binding": binding.model_dump(mode="json"),
                "capture_binding_digest": digest(binding.model_dump(mode="json")),
                "state_revision": telegram_state_revision(pairing)}


async def reserve_telegram_event_capture(db, pairing, message, receipt, *, event_key, adapter, service):
    """Reserve the original selected group in the existing event writer before Task staging."""
    from src.auth.service import authenticate_session
    from src.db.models import TelegramTransportState
    from src.work_board.contracts import WorkBoardOwner, GeneralTaskCreate
    from src.work_board.repository import _begin_sqlite_immediate
    from src.work_board.general_task import digest
    from src.work_board.general_task_proposal import new_group
    from src.workflows.general_task_accounting import validate_group_owner
    from src.extensions.telegram_transport import _now
    if not message.content.startswith("/task "):
        return
    if not pairing.capture_binding_json:
        receipt["channel_task_capture"] = {"phase": "blocked", "reason": "channel_capture_selection_required"}
        return
    # SessionManager already committed the canonical Message. The event writer
    # now fences current pairing/Goal/context and records its original group.
    pairing_id = pairing.pairing_id
    await _begin_sqlite_immediate(db)
    pairing = await db.get(TelegramTransportState, pairing.id, populate_existing=True)
    if pairing.pairing_id != pairing_id:
        raise BoardError("channel_capture_pairing_changed", "Original pairing changed", status_code=409)
    await adapter._assert_active_row(pairing, current=_now())
    binding = TelegramCaptureBindingV1.model_validate_json(pairing.capture_binding_json)
    owner = WorkBoardOwner(principal_id=pairing.owner_principal_id, session_id=pairing.operator_session_id)
    if (not binding.enabled or binding.expires_at <= _now()
        or binding.owner_principal_id != owner.principal_id or binding.original_root_id != owner.session_id
        or binding.pairing_id != pairing_id or binding.workspace_identity_digest != current_workspace_digest()
        or binding.transit_consent_reference != pairing.transit_consent_reference):
        raise BoardError("channel_capture_binding_changed", "Current capture selection is unavailable", status_code=409)
    await service.repository._validate_goal(db, owner, goal_id=binding.goal_id, goal_revision=binding.goal_revision)
    operator = await authenticate_session(owner.session_id, touch=False)
    descriptors, tool_digest = service.snapshot()
    selected = GeneralTaskCreate(goal_revision=binding.goal_revision,
        idempotency_key="channel-task:" + digest([pairing_id, event_key, message.id, message.content])[:64],
        input=GeneralTaskInput(goal_ref=binding.goal_id, intent=message.content,
            requested_output=binding.requested_output, limits=binding.limits,
            inference_egress_acknowledged=binding.inference_egress_acknowledged))
    task_input = selected.input.model_copy(update={"tool_set_digest": tool_digest})
    group = new_group(owner, task_input, descriptors, goal_revision=selected.goal_revision,
        request_key=selected.idempotency_key,
        expires_at=min(binding.expires_at, operator.idle_expires_at, operator.absolute_expires_at))
    await validate_group_owner(db, group)
    import hashlib
    reservation = ChannelCaptureReservationV1(owner_principal_id=owner.principal_id,
        original_root_id=owner.session_id, source_kind="telegram", source_id=event_key,
        canonical_message_id=message.id, conversation_session_id=message.session_id,
        source_digest=hashlib.sha256(message.content.encode()).hexdigest(),
        request_digest=digest(selected.model_dump(mode="json")), goal_revision=selected.goal_revision,
        idempotency_key=selected.idempotency_key, task_input=task_input, proposal_group=group)
    receipt["channel_task_capture"] = {"phase": "reserved",
        "pairing_id": pairing_id,
        "binding_digest": digest(binding.model_dump(mode="json")),
        "reservation": reservation.model_dump(mode="json")}


class ChannelCaptureReservationV1(ClosedTaskModel):
    schema_version: Literal["channel-task-capture.v1"] = "channel-task-capture.v1"
    owner_principal_id: str = Field(min_length=1, max_length=128)
    original_root_id: str = Field(min_length=1, max_length=128)
    source_kind: Literal["audio", "telegram"]
    source_id: str = Field(min_length=1, max_length=256)
    canonical_message_id: str = Field(min_length=1, max_length=128)
    conversation_session_id: str = Field(min_length=1, max_length=256)
    source_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    goal_revision: int = Field(ge=1)
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    task_input: GeneralTaskInput
    proposal_group: TaskProposalGroupV1

    @model_validator(mode="after")
    def exact_group(self):
        from src.work_board.general_task import digest
        group = self.proposal_group
        if (group.owner_principal_id != self.owner_principal_id
            or group.owner_session_id != self.original_root_id
            or group.goal_id != self.task_input.goal_ref
            or group.goal_revision != self.goal_revision
            or group.creation_request_key != self.idempotency_key
            or group.initial_input_digest != digest(self.task_input.model_dump(mode="json"))
            or self.task_input.tool_set_digest is None):
            raise ValueError("exact original channel Task group required")
        return self


_CAPTURE_SEAL = object()


@dataclass(frozen=True)
class CaptureIntentPublication:
    reservation: ChannelCaptureReservationV1
    source_check: object
    source_scope: object
    _seal: object


async def reserve_confirmed_audio_capture(db, owner, request, *, service, jobs,
                                         request_id, message_id, confirmed_digest):
    """Issue only from the original paired writer's confirmed canonical Message."""
    from src.auth.service import authenticate_session
    from src.work_board.general_task import digest
    from src.work_board.general_task_proposal import new_group
    from src.workflows.job_runtime import DurableJobRepository
    if (not isinstance(jobs, DurableJobRepository) or request.plan is not None
        or request.accept or request.input.document_source is not None):
        raise BoardError("channel_capture_source_required", "Select a confirmed native audio Message", status_code=403)
    source = dict(owner=owner, request_id=request_id, message_id=message_id,
                  confirmed_digest=confirmed_digest)
    _audio, message = await jobs.check_confirmed_audio_task_source(db, **source)
    if request.input.intent != message.content:
        raise BoardError("channel_capture_source_changed", "Use the exact confirmed Message", status_code=409)
    await service.repository._validate_goal(db, owner, goal_id=request.input.goal_ref,
                                           goal_revision=request.goal_revision)
    operator = await authenticate_session(owner.session_id, touch=False)
    if operator.principal.principal_id != owner.principal_id:
        raise BoardError("channel_capture_owner_changed", "Original operator changed", status_code=403)
    descriptors, tool_digest = service.snapshot()
    if request.input.tool_set_digest not in (None, tool_digest):
        raise BoardError("general_task_tool_set_changed", "Refresh the current tool contract", status_code=409)
    task_input = request.input.model_copy(update={"tool_set_digest": tool_digest})
    group = new_group(owner, task_input, descriptors, goal_revision=request.goal_revision,
        request_key=request.idempotency_key,
        expires_at=min(operator.idle_expires_at, operator.absolute_expires_at))
    candidate = ChannelCaptureReservationV1(owner_principal_id=owner.principal_id,
        original_root_id=owner.session_id, source_kind="audio", source_id=request_id,
        canonical_message_id=message_id, source_digest=confirmed_digest,
        conversation_session_id=message.session_id,
        request_digest=digest(request.model_dump(mode="json")),
        goal_revision=request.goal_revision, idempotency_key=request.idempotency_key,
        task_input=task_input, proposal_group=group)
    reservation = await jobs.reserve_audio_task_capture_metadata(db, **source, reservation=candidate)
    async def source_check(db):
        current_audio, current_message = await jobs.check_confirmed_audio_task_source(db, **source)
        stored = json.loads(current_audio.metadata_json).get("channel_task_capture.v1")
        if (current_message.content != reservation.task_input.intent
            or stored != reservation.model_dump(mode="json")):
            raise BoardError("channel_capture_source_changed", "Confirmed source changed", status_code=409)
    def source_scope():
        return jobs.confirmed_audio_task_source_scope(**source)
    capture = CaptureIntentPublication(reservation, source_check, source_scope, _CAPTURE_SEAL)
    await check_capture_publication(db, owner, request, capture)
    return capture


async def check_capture_publication(db, owner, request, capture):
    from src.work_board.general_task import digest
    from src.workflows.general_task_accounting import validate_group_owner
    if (not isinstance(capture, CaptureIntentPublication)
        or capture._seal is not _CAPTURE_SEAL
        or not callable(capture.source_check) or not callable(capture.source_scope)):
        raise BoardError("channel_capture_source_required", "Use the original confirmed channel source", status_code=403)
    reservation = capture.reservation
    if (request.plan is not None or request.accept
        or reservation.owner_principal_id != owner.principal_id
        or reservation.original_root_id != owner.session_id
        or reservation.request_digest != digest(request.model_dump(mode="json"))
        or reservation.goal_revision != request.goal_revision
        or reservation.idempotency_key != request.idempotency_key):
        raise BoardError("channel_capture_request_changed", "Original capture request changed", status_code=409)
    await capture.source_check(db)
    await validate_group_owner(db, reservation.proposal_group)
    return reservation


async def check_telegram_capture_source(db, owner, ingress, *, adapter):
    from sqlalchemy import select
    from src.db.models import TelegramInboundUpdate, TelegramTransportState, Message
    from src.extensions.telegram_transport import TelegramTransportAdapter, _now
    from src.work_board.general_task import digest
    from src.workflows.general_task_accounting import validate_group_owner
    import hashlib
    if type(ingress) is not ChannelTaskIngress or not isinstance(adapter, TelegramTransportAdapter):
        raise BoardError("channel_capture_source_required", "Use the paired provider event", status_code=403)
    if ingress.attachment_refs:
        raise BoardError("channel_document_source_unsealed", "Upload and seal selected documents before capture", status_code=409)
    pairing = (await db.execute(select(TelegramTransportState).where(
        TelegramTransportState.pairing_id == ingress.pairing_id).execution_options(populate_existing=True))).scalars().one_or_none()
    if (pairing is None or pairing.owner_principal_id != owner.principal_id
        or pairing.operator_session_id != owner.session_id or not pairing.capture_binding_json):
        raise BoardError("channel_capture_pairing_changed", "Current original pairing is unavailable", status_code=403)
    await adapter._assert_active_row(pairing, current=_now())
    binding = TelegramCaptureBindingV1.model_validate_json(pairing.capture_binding_json)
    if (not binding.enabled or binding.expires_at <= _now()
        or binding.pairing_id != ingress.pairing_id
        or binding.owner_principal_id != owner.principal_id or binding.original_root_id != owner.session_id
        or binding.transit_consent_reference != pairing.transit_consent_reference
        or binding.workspace_identity_digest != current_workspace_digest()
        or digest(binding.model_dump(mode="json")) != ingress.reply_context.capture_binding_digest):
        raise BoardError("channel_capture_binding_changed", "Original capture selection changed or expired", status_code=409)
    event = (await db.execute(select(TelegramInboundUpdate).where(
        TelegramInboundUpdate.idempotency_key == ingress.event_id,
        TelegramInboundUpdate.owner_principal_id == owner.principal_id,
        TelegramInboundUpdate.operator_session_id == owner.session_id).execution_options(populate_existing=True))).scalars().one_or_none()
    stored = json.loads(event.receipt_json).get("channel_task_capture") if event else None
    if (event is None or event.status != "accepted" or not stored
        or stored.get("binding_digest") != ingress.reply_context.capture_binding_digest
        or ingress.text_or_transcript_ref != "message:" + str(event.canonical_message_id)
        or event.session_id != ingress.reply_context.conversation_session_id):
        raise BoardError("channel_capture_event_changed", "Original reserved provider event is unavailable", status_code=409)
    reservation = ChannelCaptureReservationV1.model_validate(stored.get("reservation"))
    if (reservation.owner_principal_id != owner.principal_id or reservation.original_root_id != owner.session_id
        or reservation.task_input.goal_ref != binding.goal_id or reservation.goal_revision != binding.goal_revision
        or reservation.task_input.requested_output != binding.requested_output
        or reservation.task_input.limits != binding.limits
        or reservation.task_input.inference_egress_acknowledged != binding.inference_egress_acknowledged):
        raise BoardError("channel_capture_reservation_changed", "Original source selection changed", status_code=409)
    await validate_group_owner(db, reservation.proposal_group)
    message = await db.get(Message, event.canonical_message_id, populate_existing=True)
    if (message is None or message.session_id != event.session_id or message.role != "user"
        or hashlib.sha256(message.content.encode()).hexdigest() != event.content_digest
        or reservation.source_kind != "telegram" or reservation.source_id != event.idempotency_key
        or reservation.canonical_message_id != message.id or reservation.conversation_session_id != message.session_id
        or reservation.source_digest != event.content_digest or reservation.task_input.intent != message.content):
        raise BoardError("channel_capture_source_changed", "Original canonical Message changed", status_code=409)
    return event, reservation


async def reserved_telegram_capture(db, owner, ingress, *, adapter):
    """Seal only an already-reserved original provider event, without renewal."""
    from src.work_board.contracts import GeneralTaskCreate
    _event, reservation = await check_telegram_capture_source(db, owner, ingress, adapter=adapter)
    # Capture requests are server-resolved from the immutable reserved input.
    request = GeneralTaskCreate(goal_revision=reservation.goal_revision,
        idempotency_key=reservation.idempotency_key,
        input=reservation.task_input.model_copy(update={"tool_set_digest": None}))
    async def source_check(db):
        _event, current = await check_telegram_capture_source(db, owner, ingress, adapter=adapter)
        if current != reservation:
            raise BoardError("channel_capture_reservation_changed", "Original reservation changed", status_code=409)
    capture = CaptureIntentPublication(reservation, source_check, lambda: adapter._lock, _CAPTURE_SEAL)
    await check_capture_publication(db, owner, request, capture)
    return request, capture
