"""Source-owned, planless capture into the existing reviewed C1 publication path.

Reservations preserve the first selected Task allowance. They authorize no
inference or effects; only the private source issuer can seal a publication.
"""
from dataclasses import dataclass
from contextlib import contextmanager
from contextvars import ContextVar
from types import MappingProxyType
import asyncio
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.work_board.contracts import (
    ClosedTaskModel, GeneralTaskInput, TaskLimits, TaskProposalGroupV1,
)
from src.work_board.repository import BoardError
from datetime import datetime, timezone


class TelegramDocumentAcquisitionSelection(ClosedTaskModel):
    action: Literal["acquire_one_original_task_document"]
    max_sources: Literal[1]
    source_cap_bytes: Literal[16777216]
    docx_cap_bytes: Literal[10485760]
    formats: list[Literal["pdf", "docx", "xlsx", "csv"]]
    no_learning: Literal[True]

    @field_validator("max_sources", "source_cap_bytes", "docx_cap_bytes", mode="before")
    @classmethod
    def strict_document_integer(cls, value):
        if type(value) is not int:
            raise ValueError("strict document integer required")
        return value

    @field_validator("no_learning", mode="before")
    @classmethod
    def strict_document_no_learning(cls, value):
        if value is not True:
            raise ValueError("explicit no-learning required")
        return value

    @model_validator(mode="after")
    def exact_formats(self):
        if self.formats != ["pdf", "docx", "xlsx", "csv"]:
            raise ValueError("exact ordered document formats required")
        return self


class TelegramCaptureSelection(ClosedTaskModel):
    expected_revision: int = Field(ge=1)
    enabled: bool = False
    goal_id: str = Field(min_length=1, max_length=128)
    goal_revision: int = Field(ge=1)
    requested_output: dict
    limits: TaskLimits = Field(default_factory=lambda: TaskLimits(max_inference_calls=0, max_cost_microusd=0))
    inference_egress_acknowledged: bool = False
    document_acquisition: TelegramDocumentAcquisitionSelection | None = None


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


class TelegramCaptureBindingV2(ClosedTaskModel):
    schema_version: Literal["telegram-task-capture-binding.v2"] = "telegram-task-capture-binding.v2"
    selection: TelegramCaptureBindingV1
    document_acquisition: TelegramDocumentAcquisitionSelection
    selected_document_event_id: str | None = Field(default=None, min_length=1, max_length=256)


def decode_telegram_capture_binding(raw):
    value = json.loads(raw)
    if value.get("schema_version") == "telegram-task-capture-binding.v2":
        return TelegramCaptureBindingV2.model_validate(value)
    return TelegramCaptureBindingV1.model_validate(value)


def telegram_capture_selection(binding):
    return binding.selection if type(binding) is TelegramCaptureBindingV2 else binding


def telegram_capture_binding_digest(binding):
    from src.work_board.general_task import digest
    if type(binding) is TelegramCaptureBindingV2:
        # Lifecycle consumption never changes the immutable original selection.
        return digest(binding.model_dump(mode="json", exclude={"selected_document_event_id"}))
    return digest(binding.model_dump(mode="json"))


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
            if selection.document_acquisition is not None:
                binding = TelegramCaptureBindingV2(selection=binding,
                    document_acquisition=selection.document_acquisition)
            pairing.capture_binding_json = binding.model_dump_json()
            pairing.updated_at = _now()
            await db.commit()
            return {"capture_binding": binding.model_dump(mode="json"),
                "capture_binding_digest": telegram_capture_binding_digest(binding),
                "state_revision": telegram_state_revision(pairing)}


async def reserve_telegram_event_capture(db, pairing, message, receipt, *, event_key, adapter, service,
    document=None, provider_update_id=None, provider_request_digest=None):
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
    from src.extensions.telegram_transport import _aware
    if (not pairing.model_consent_reference or not _aware(pairing.model_consent_expires_at)
        or _aware(pairing.model_consent_expires_at) <= _now()):
        raise BoardError("channel_capture_model_grant_changed", "Original channel model grant is no longer current", status_code=403)
    envelope = decode_telegram_capture_binding(pairing.capture_binding_json)
    binding = telegram_capture_selection(envelope)
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
        "binding_digest": telegram_capture_binding_digest(envelope),
        "reservation": reservation.model_dump(mode="json")}
    if document is not None:
        if (type(envelope) is not TelegramCaptureBindingV2
            or envelope.selected_document_event_id is not None):
            receipt["channel_task_capture"] = {"phase": "blocked", "reason": "channel_document_acquisition_selection_required"}
            return
        from src.work_board.document_channel_ingest import issue_original_document_binding
        acquired = issue_original_document_binding(pairing, envelope, reservation, document,
            provider_update_id=provider_update_id, provider_request_digest=provider_request_digest)
        receipt["channel_task_capture"]["document_acquisition"] = acquired.model_dump(mode="json")
        from sqlalchemy import update
        previous_binding = pairing.capture_binding_json
        consumed_binding = envelope.model_copy(update={"selected_document_event_id": event_key}).model_dump_json()
        consumed = await db.execute(update(TelegramTransportState).where(
            TelegramTransportState.id == pairing.id,
            TelegramTransportState.capture_binding_json == previous_binding).values(
                capture_binding_json=consumed_binding).execution_options(synchronize_session=False))
        if consumed.rowcount != 1:
            raise BoardError("channel_document_selection_changed", "Original one-use document selection changed", status_code=409)
        pairing.capture_binding_json = consumed_binding
        pairing.updated_at = _now()


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


class ChannelCaptureOriginV1(ClosedTaskModel):
    schema_version: Literal["channel-capture-origin.v1"] = "channel-capture-origin.v1"
    task_id: str = Field(min_length=1, max_length=128)
    owner_principal_id: str = Field(min_length=1, max_length=128)
    original_root_id: str = Field(min_length=1, max_length=128)
    goal_id: str = Field(min_length=1, max_length=128)
    goal_revision: int = Field(ge=1)
    conversation_session_id: str = Field(min_length=1, max_length=256)
    idempotency_key: str = Field(min_length=1, max_length=128)
    source_kind: Literal["audio", "telegram"]
    source_id: str = Field(min_length=1, max_length=256)
    canonical_message_id: str = Field(min_length=1, max_length=128)
    reservation_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    group_id: str = Field(min_length=1, max_length=128)
    initial_input_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    group_digest: str = Field(pattern=r"^[a-f0-9]{64}$")


def _origin_record(task, reservation):
    from src.work_board.general_task import digest
    return ChannelCaptureOriginV1(task_id=task.task_id,
        owner_principal_id=reservation.owner_principal_id, original_root_id=reservation.original_root_id,
        goal_id=reservation.task_input.goal_ref, goal_revision=reservation.goal_revision,
        conversation_session_id=reservation.conversation_session_id, idempotency_key=reservation.idempotency_key,
        source_kind=reservation.source_kind, source_id=reservation.source_id,
        canonical_message_id=reservation.canonical_message_id,
        reservation_digest=digest(reservation.model_dump(mode="json")),
        source_digest=reservation.source_digest, request_digest=reservation.request_digest,
        group_id=reservation.proposal_group.group_id,
        initial_input_digest=reservation.proposal_group.initial_input_digest,
        group_digest=digest(reservation.proposal_group.model_dump(mode="json")))


_IDENTITY_SEAL = object()


@dataclass(frozen=True, repr=False)
class _CapturedIdentity:
    workspace_digest: str
    callback_root_digest: str
    origin_key: bytes
    _seal: object
    root: object = None


def stage_captured_identity():
    """Stage fixed workspace and existing signing identity outside writers."""
    import hashlib, hmac
    from config.settings import settings
    from src.workspace import canonical_workspace_root, canonical_workspace_root_identity
    from src.work_board.general_task import digest
    root = canonical_workspace_root_identity(settings.workspace_dir)
    legacy = digest([str(canonical_workspace_root(settings.workspace_dir)), root['device'], root['inode']])
    if current_workspace_digest() != legacy:
        raise BoardError('channel_capture_identity_changed', 'Workspace changed during identity staging', status_code=409)
    return _CapturedIdentity(legacy, digest(root),
        hmac.new(_output_signing_key(), b"seraph.channel-capture-origin.v1", hashlib.sha256).digest(),
        _IDENTITY_SEAL, MappingProxyType(root))


_SOURCE_FRAME = ContextVar('channel_capture_source_frame', default=None)
_SOURCE_FRAME_SEAL = object()
_LIVE_SOURCE_FRAMES = {}


class _SourceFrame:
    def __init__(self, root, identity, seal):
        if seal is not _SOURCE_FRAME_SEAL:
            raise BoardError('channel_capture_identity_required', 'Use the Source identity owner', status_code=403)
        self.root, self.identity = root, identity
        self.creator = asyncio.current_task()
        self.task_binding = None
        self.closed = False


def _checked_source_frame():
    frame = _SOURCE_FRAME.get()
    if (type(frame) is not _SourceFrame or _LIVE_SOURCE_FRAMES.get(id(frame)) is not frame
        or frame.closed or frame.creator is not asyncio.current_task()):
        raise BoardError('channel_capture_identity_required', 'Stage the current Source identity outside SQL', status_code=403)
    return frame


@contextmanager
def staged_captured_source_identity():
    """Lexical verification material; current SQL alone grants Source use."""
    from config.settings import settings
    from src.workspace import canonical_workspace_root_identity
    root = identity = None
    try:
        root = MappingProxyType(canonical_workspace_root_identity(settings.workspace_dir))
    except Exception:
        pass
    try:
        identity = stage_captured_identity()
        if root is None or dict(identity.root) != dict(root):
            identity = None
            root = None
    except Exception:
        # Missing signer does not prevent ordinary Task journal operations.
        pass
    frame = _SourceFrame(root, identity, _SOURCE_FRAME_SEAL)
    _LIVE_SOURCE_FRAMES[id(frame)] = frame
    token = _SOURCE_FRAME.set(frame)
    try:
        yield
    finally:
        frame.closed = True
        _LIVE_SOURCE_FRAMES.pop(id(frame), None)
        import sys
        guard = sys.modules.get('src.workflows.general_task_guard')
        if guard is not None:
            guard.close_source_journals(frame)
        _SOURCE_FRAME.reset(token)


def _staged_source_root_for_sql():
    root = _checked_source_frame().root
    if root is None:
        raise BoardError('channel_capture_identity_required', 'Current workspace identity is unavailable', status_code=409)
    return root


async def check_current_captured_task_source(db, owner, task, *, identity=None):
    """Authenticate exact current capture before any private input or adoption."""
    frame = None
    if identity is None:
        try:
            frame = _checked_source_frame()
            identity = frame.identity
        except BoardError:
            pass
    # Ordinary NULL Tasks neither need nor acquire captured signing material.
    if task.channel_capture_origin_json is None:
        captured = await check_capture_origin(db, owner, task, _classify_only=True)
        if captured is None:
            return None
        raise BoardError('channel_capture_origin_changed', 'Original capture MAC needs operator recovery', status_code=409)
    identity = _checked_identity(identity)
    reservation = await check_capture_origin(db, owner, task,
        _workspace_digest=identity.workspace_digest, _identity=identity)
    if reservation.source_kind == 'telegram':
        from sqlalchemy import select
        from src.db.models import TelegramInboundUpdate
        event = await db.scalar(select(TelegramInboundUpdate).where(
            TelegramInboundUpdate.idempotency_key == reservation.source_id,
            TelegramInboundUpdate.owner_principal_id == owner.principal_id,
            TelegramInboundUpdate.operator_session_id == owner.session_id).execution_options(populate_existing=True))
        stored = json.loads(event.receipt_json).get('channel_task_capture', {}) if event else {}
        if stored.get('task_id') != task.task_id:
            raise BoardError('channel_capture_origin_changed', 'Original captured Task association changed', status_code=409)
        if stored.get('document_acquisition') is not None:
            from src.work_board.document_channel_ingest import check_original_document_sealed
            await check_original_document_sealed(db, owner, event, reservation, expected_task_id=task.task_id)
    if frame is not None:
        binding = (task.task_id, owner.principal_id, owner.session_id,
            task.channel_capture_origin_json, reservation.idempotency_key)
        if frame.task_binding not in (None, binding):
            raise BoardError('channel_capture_identity_required', 'Source frame belongs to another Task', status_code=403)
        frame.task_binding = binding
    return reservation


def _checked_identity(identity):
    if type(identity) is not _CapturedIdentity or identity._seal is not _IDENTITY_SEAL:
        raise BoardError("channel_capture_identity_required", "Stage the original server identity", status_code=403)
    return identity


def _origin_mac(payload, *, identity=None):
    import hashlib, hmac
    from src.work_board.general_task import canonical
    # Existing configured server identity; no separately managed key lifecycle.
    key = (_checked_identity(identity).origin_key if identity is not None else
        hmac.new(_output_signing_key(), b"seraph.channel-capture-origin.v1", hashlib.sha256).digest())
    return hmac.new(key, canonical(payload), hashlib.sha256).hexdigest()


async def publish_capture_origin(db, owner, request, mutation, capture):
    """Set provenance only under the original sealed publication writer."""
    from src.work_board.general_task import canonical, digest
    reservation = await check_capture_publication(db, owner, request, capture)
    origin = _origin_record(mutation.task, reservation).model_dump(mode="json")
    encoded = canonical({"origin": origin, "mac": _origin_mac(origin, identity=capture.identity)}).decode("utf-8")
    if len(encoded.encode("utf-8")) > 4096:
        raise BoardError("channel_capture_origin_invalid", "Capture provenance exceeds its bound", status_code=409)
    if mutation.idempotent_replay:
        if mutation.task.channel_capture_origin_json != encoded:
            raise BoardError("channel_capture_origin_changed", "Original capture provenance needs recovery", status_code=409)
        if capture.document_link is not None:
            from src.work_board.document_channel_ingest import bind_original_document_task
            await bind_original_document_task(db, owner, mutation.task, reservation, capture.document_link)
        return
    if mutation.task.channel_capture_origin_json not in (None, encoded):
        raise BoardError("channel_capture_origin_changed", "Original capture provenance changed", status_code=409)
    mutation.task.channel_capture_origin_json = encoded
    # This immutable server-created event also classifies a removed marker.
    # Its digest is never accepted as source authority or used to backfill it.
    metadata = json.loads(mutation.event.metadata_json)
    metadata["channel_capture_origin_digest"] = digest(encoded)
    mutation.event.metadata_json = canonical(metadata).decode("utf-8")
    await db.flush()
    if capture.document_link is not None:
        from src.work_board.document_channel_ingest import bind_original_document_task
        await bind_original_document_task(db, owner, mutation.task, reservation, capture.document_link)
    await db.flush()


async def check_document_retirement_origin(db, owner, task, reservation, identity):
    """Authenticate immutable capture identity without issuing execution authority.

    The document retirement owner separately checks the current authenticated
    deletion owner and original reciprocal Source/event receipt. Execution group
    or transit expiry neither grants nor prevents this source-only operation.
    Identity must have been staged by that owner before its SQL writer.
    """
    import hmac
    from sqlalchemy import select
    from src.db.models import WorkBoardEvent
    from src.work_board.general_task import digest
    identity = _checked_identity(identity)
    try:
        raw = task.channel_capture_origin_json
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > 4096:
            raise ValueError()
        wrapper = json.loads(raw)
        if set(wrapper) != {"origin", "mac"} or not isinstance(wrapper["mac"], str):
            raise ValueError()
        expected = _origin_record(task, reservation)
        origin = ChannelCaptureOriginV1.model_validate(wrapper["origin"])
        if (origin != expected or origin.owner_principal_id != owner.principal_id
            or origin.original_root_id != owner.session_id
            or task.owner_principal_id != owner.principal_id
            or task.owner_session_id != owner.session_id
            or not hmac.compare_digest(_origin_mac(wrapper["origin"], identity=identity), wrapper["mac"])):
            raise ValueError()
        events = (await db.execute(select(WorkBoardEvent.metadata_json).where(
            WorkBoardEvent.task_id == task.task_id, WorkBoardEvent.kind == "task.created",
            WorkBoardEvent.owner_principal_id == owner.principal_id,
            WorkBoardEvent.owner_session_id == owner.session_id))).scalars().all()
        if len(events) != 1 or json.loads(events[0]).get("channel_capture_origin_digest") != digest(raw):
            raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError):
        raise BoardError("channel_capture_origin_changed", "Original captured source needs operator recovery", status_code=409) from None


async def check_capture_origin(db, owner, task, *, expected_reservation=None,
                               _workspace_digest=None, _classify_only=False, _identity=None):
    """Classify authentic captured Tasks before their private input is parsed.

    Source-owner guards authenticate the original Root/grants before canonical
    Message access. NULL ordinary Tasks retain their existing behavior; legacy
    captured drafts require an unambiguous original reservation, without repair.
    """
    import hmac
    from sqlalchemy import select, func, case
    from src.db.models import AudioIngressJob, TelegramInboundUpdate, WorkBoardEvent
    from src.extensions.telegram_transport import TelegramTransportError
    from src.workflows.job_runtime import DurableJobError
    from src.work_board.general_task import digest
    def denied():
        return BoardError("channel_capture_origin_changed", "Original captured source needs operator recovery", status_code=409)
    raw = task.channel_capture_origin_json
    origin = None
    if raw is not None:
        try:
            if len(raw.encode("utf-8")) > 4096:
                raise ValueError()
            wrapper = json.loads(raw)
            if set(wrapper) != {"origin", "mac"} or not isinstance(wrapper["mac"], str):
                raise ValueError()
            if not hmac.compare_digest(_origin_mac(wrapper["origin"], identity=_identity), wrapper["mac"]):
                raise ValueError()
            origin = ChannelCaptureOriginV1.model_validate(wrapper["origin"])
            if (origin.task_id != task.task_id or origin.owner_principal_id != owner.principal_id
                or origin.original_root_id != owner.session_id or origin.goal_id != task.goal_id
                or origin.goal_revision != task.goal_revision or origin.idempotency_key != task.idempotency_key
                or origin.conversation_session_id != task.origin_session_id):
                raise ValueError()
        except (ValueError, TypeError, KeyError, AttributeError):
            raise denied() from None
    events = (await db.execute(select(WorkBoardEvent.metadata_json).where(
        WorkBoardEvent.task_id == task.task_id, WorkBoardEvent.kind == "task.created",
        WorkBoardEvent.owner_principal_id == owner.principal_id,
        WorkBoardEvent.owner_session_id == owner.session_id))).scalars().all()
    try:
        mirrors = [json.loads(item).get("channel_capture_origin_digest") for item in events]
    except (TypeError, ValueError):
        raise denied() from None
    if any(item is not None for item in mirrors) and (raw is None or mirrors != [digest(raw)]):
        raise denied()
    # Select only source metadata. No Task/private artifact read precedes these
    # source checks; public lineage and caller-shaped prefixes are irrelevant.
    telegram = (await db.execute(select(TelegramInboundUpdate).where(
        TelegramInboundUpdate.owner_principal_id == owner.principal_id,
        TelegramInboundUpdate.operator_session_id == owner.session_id,
        case((func.json_valid(TelegramInboundUpdate.receipt_json),
            func.json_extract(TelegramInboundUpdate.receipt_json,
                "$.channel_task_capture.reservation.idempotency_key")), else_=None) == task.idempotency_key)
        .execution_options(populate_existing=True))).scalars().all()
    audio = (await db.execute(select(AudioIngressJob).where(
        AudioIngressJob.owner_principal_id == owner.principal_id,
        AudioIngressJob.operator_session_id == owner.session_id,
        case((func.json_valid(AudioIngressJob.metadata_json),
            func.json_extract(AudioIngressJob.metadata_json,
                '$."channel_task_capture.v1".idempotency_key')), else_=None) == task.idempotency_key)
        .execution_options(populate_existing=True))).scalars().all()
    if not telegram and not audio and origin is None:
        if expected_reservation is not None:
            raise denied()
        return None
    if len(telegram) + len(audio) != 1:
        raise denied()
    if _classify_only:
        # Classification is metadata-only, never permission to read an input.
        return True
    if telegram and _workspace_digest is None:
        raise denied()
    try:
        if telegram:
            from src.extensions.telegram_transport import default_telegram_transport
            event = telegram[0]
            stored = json.loads(event.receipt_json)["channel_task_capture"]
            ingress = ChannelTaskIngress(pairing_id=stored["pairing_id"], event_id=event.idempotency_key,
                text_or_transcript_ref="message:" + str(event.canonical_message_id), attachment_refs=[],
                reply_context=ChannelReplyContext(capture_binding_digest=stored["binding_digest"],
                    conversation_session_id=event.session_id))
            _, reservation = await check_telegram_capture_source(db, owner, ingress,
                adapter=default_telegram_transport, _workspace_digest=_workspace_digest)
        else:
            from src.workflows.job_runtime import DurableJobRepository
            row = audio[0]
            reservation = ChannelCaptureReservationV1.model_validate(
                json.loads(row.metadata_json)["channel_task_capture.v1"])
            from src.workflows.general_task_accounting import validate_group_owner
            await validate_group_owner(db, reservation.proposal_group)
            await DurableJobRepository().check_confirmed_audio_task_source(db, owner=owner,
                request_id=row.request_id, message_id=reservation.canonical_message_id,
                confirmed_digest=reservation.source_digest)
        expected = _origin_record(task, reservation)
        if (expected.owner_principal_id != owner.principal_id or expected.original_root_id != owner.session_id
            or expected.goal_id != task.goal_id or expected.goal_revision != task.goal_revision
            or expected.conversation_session_id != task.origin_session_id
            or (origin is not None and origin != expected)
            or (expected_reservation is not None and expected_reservation != reservation)):
            raise denied()
        return reservation
    except (ValueError, TypeError, KeyError, AttributeError):
        raise denied() from None
    except (TelegramTransportError, DurableJobError):
        raise denied() from None


@dataclass(frozen=True, repr=False)
class CaptureIntentPublication:
    reservation: ChannelCaptureReservationV1
    source_check: object
    source_scope: object
    _seal: object
    identity: object = None
    document_link: object = None


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
    capture = CaptureIntentPublication(reservation, source_check, source_scope, _CAPTURE_SEAL,
        stage_captured_identity())
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
    _checked_identity(capture.identity)
    if (request.plan is not None or request.accept
        or reservation.owner_principal_id != owner.principal_id
        or reservation.original_root_id != owner.session_id
        or reservation.request_digest != digest(request.model_dump(mode="json"))
        or reservation.goal_revision != request.goal_revision
        or reservation.idempotency_key != request.idempotency_key):
        raise BoardError("channel_capture_request_changed", "Original capture request changed", status_code=409)
    await capture.source_check(db)
    if reservation.source_kind == "telegram":
        from sqlalchemy import select
        from src.db.models import TelegramInboundUpdate
        event = await db.scalar(select(TelegramInboundUpdate).where(
            TelegramInboundUpdate.idempotency_key == reservation.source_id,
            TelegramInboundUpdate.owner_principal_id == owner.principal_id,
            TelegramInboundUpdate.operator_session_id == owner.session_id).execution_options(populate_existing=True))
        stored = json.loads(event.receipt_json).get("channel_task_capture", {}) if event else {}
        if stored.get("document_acquisition") is not None:
            if capture.document_link is None:
                raise BoardError("channel_document_link_required", "Use the original sealed document witness", status_code=409)
            from src.work_board.document_channel_ingest import check_original_document_link
            await check_original_document_link(db, owner, reservation, capture.document_link)
    await validate_group_owner(db, reservation.proposal_group)
    return reservation


async def check_telegram_capture_source(db, owner, ingress, *, adapter, _workspace_digest=None):
    from sqlalchemy import select, func
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
    from src.extensions.telegram_transport import _aware
    if (not pairing.model_consent_reference or not _aware(pairing.model_consent_expires_at)
        or _aware(pairing.model_consent_expires_at) <= _now()):
        raise BoardError("channel_capture_model_grant_changed", "Original channel model grant is no longer current", status_code=403)
    envelope = decode_telegram_capture_binding(pairing.capture_binding_json)
    binding = telegram_capture_selection(envelope)
    if (not binding.enabled or binding.expires_at <= _now()
        or binding.pairing_id != ingress.pairing_id
        or binding.owner_principal_id != owner.principal_id or binding.original_root_id != owner.session_id
        or binding.transit_consent_reference != pairing.transit_consent_reference
        or binding.workspace_identity_digest != (_workspace_digest if _workspace_digest is not None else current_workspace_digest())
        or telegram_capture_binding_digest(envelope) != ingress.reply_context.capture_binding_digest):
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
    if stored.get("document_acquisition") is not None:
        from src.work_board.document_channel_ingest import check_original_document_sealed
        await check_original_document_sealed(db, owner, event, reservation,
            expected_task_id=stored.get("task_id"))
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
    identity = stage_captured_identity()
    staged_workspace = identity.workspace_digest
    _event, reservation = await check_telegram_capture_source(db, owner, ingress, adapter=adapter,
        _workspace_digest=staged_workspace)
    # Capture requests are server-resolved from the immutable reserved input.
    request = GeneralTaskCreate(goal_revision=reservation.goal_revision,
        idempotency_key=reservation.idempotency_key,
        input=reservation.task_input.model_copy(update={"tool_set_digest": None}))
    async def source_check(db):
        _event, current = await check_telegram_capture_source(db, owner, ingress, adapter=adapter,
            _workspace_digest=staged_workspace)
        if current != reservation:
            raise BoardError("channel_capture_reservation_changed", "Original reservation changed", status_code=409)
        if document_link is not None:
            from src.work_board.document_channel_ingest import check_original_document_link
            await check_original_document_link(db, owner, reservation, document_link)
    document_link = None
    if json.loads(_event.receipt_json)["channel_task_capture"].get("document_acquisition") is not None:
        from src.work_board.document_channel_ingest import stage_original_document_link
        document_link = await stage_original_document_link(db, owner, reservation)
    capture = CaptureIntentPublication(reservation, source_check, lambda: adapter._lock, _CAPTURE_SEAL,
        identity, document_link)
    await check_capture_publication(db, owner, request, capture)
    return request, capture


async def check_captured_task_control(db, owner, action, *, adapter, _workspace_digest=None, _identity=None):
    """Resolve a control only from its original linked provider reservation."""
    from sqlalchemy import func, select
    from src.db.models import TelegramInboundUpdate
    from src.extensions.telegram_task_controls import current, task_owned
    if type(action) is not ChannelAction:
        raise BoardError("channel_action_required", "Use the exact closed channel action", status_code=403)
    pairing = await current(db, owner.principal_id, owner.session_id)
    task = await task_owned(db, owner.principal_id, owner.session_id, action.task_id)
    if task.capability_id != "agent.task.v1" or task.task_revision != action.expected_revision:
        raise BoardError("channel_task_revision_changed", "Refresh the original captured Task", status_code=409)
    await check_capture_origin(db, owner, task, _workspace_digest=_workspace_digest, _identity=_identity)
    event = (await db.execute(select(TelegramInboundUpdate).where(
        TelegramInboundUpdate.owner_principal_id == owner.principal_id,
        TelegramInboundUpdate.operator_session_id == owner.session_id,
        func.json_extract(TelegramInboundUpdate.receipt_json, "$.channel_task_capture.task_id") == task.task_id,
    ).execution_options(populate_existing=True))).scalars().one_or_none()
    if event is None:
        raise BoardError("channel_task_source_missing", "Original captured provider event is required", status_code=409)
    import json
    stored = json.loads(event.receipt_json)["channel_task_capture"]
    ingress = ChannelTaskIngress(pairing_id=stored["pairing_id"], event_id=event.idempotency_key,
        text_or_transcript_ref="message:" + str(event.canonical_message_id), attachment_refs=[],
        reply_context=ChannelReplyContext(capture_binding_digest=stored["binding_digest"],
            conversation_session_id=event.session_id))
    _event, reservation = await check_telegram_capture_source(db, owner, ingress, adapter=adapter,
        _workspace_digest=_workspace_digest)
    if (pairing.pairing_id != ingress.pairing_id or task.idempotency_key != reservation.idempotency_key
        or task.goal_id != reservation.task_input.goal_ref or task.goal_revision != reservation.goal_revision
        or task.origin_session_id != reservation.conversation_session_id):
        raise BoardError("channel_task_source_changed", "Original Task source binding changed", status_code=409)
    return task, reservation


_CONTROL_COMMIT_SEAL = object()


@dataclass(frozen=True, repr=False)
class CapturedChannelControlCommit:
    action: str
    task_id: str
    task_revision: int
    attempt_id: str
    board_fence: int
    parent_id: str
    parent_revision: int
    parent_fence: int
    callback_id: str
    callback_digest: str
    reservation_digest: str
    query_id: str
    update_id: int
    request_digest: str
    source_check: object
    consume: object
    _seal: object


async def issue_captured_control_commit(db, *, controls, owner, action, row, query,
                                       update_id, request_digest, identity):
    """Only the actual callback owner may stage an original native intent."""
    from sqlalchemy import select
    from src.db.models import TelegramTaskCallback, WorkBoardAttempt, WorkflowRunState
    from src.extensions.telegram_task_controls import TelegramTaskControls, effect_digest
    from src.work_board.general_task import digest
    if type(controls) is not TelegramTaskControls or action.action not in {"pause", "resume", "cancel"}:
        raise BoardError("channel_control_commit_required", "Use the original callback owner", status_code=403)
    _checked_identity(identity)
    actual = await db.get(TelegramTaskCallback, row.id, populate_existing=True)
    if actual is None or actual is not row or row.effect != action.action or row.status != "pending":
        raise BoardError("channel_control_callback_changed", "Original callback changed", status_code=409)
    task, reservation = await check_captured_task_control(db, owner, action, adapter=controls.adapter,
        _workspace_digest=identity.workspace_digest, _identity=identity)
    attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == task.task_id,
        WorkBoardAttempt.ended_at.is_(None)).execution_options(populate_existing=True))
    parent = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == attempt.workflow_run_id)
        .execution_options(populate_existing=True)) if attempt else None
    if (attempt is None or parent is None or row.attempt_id != attempt.attempt_id
        or row.workflow_run_id != parent.run_identity or row.board_fence != attempt.fencing_token):
        raise BoardError("channel_control_native_changed", "Original native binding changed", status_code=409)
    callback_digest = effect_digest(row)
    reservation_digest = digest(reservation.model_dump(mode="json"))
    async def source_check(check_db):
        current = await check_db.get(TelegramTaskCallback, row.id, populate_existing=True)
        if current is None or effect_digest(current) != callback_digest:
            raise BoardError("channel_control_callback_changed", "Original callback changed", status_code=409)
        await controls._validate(check_db, current, owner=owner.principal_id, session=owner.session_id,
            query=query, update_id=update_id, request_digest=request_digest, captured_identity=identity)
        _task, original = await check_captured_task_control(check_db, owner, action, adapter=controls.adapter,
            _workspace_digest=identity.workspace_digest, _identity=identity)
        if digest(original.model_dump(mode="json")) != reservation_digest:
            raise BoardError("channel_control_source_changed", "Original capture source changed", status_code=409)
    async def consume(check_db, current_task):
        current = await check_db.get(TelegramTaskCallback, row.id, populate_existing=True)
        if current.status != "pending":
            raise BoardError("channel_control_callback_replayed", "Original callback was already consumed", status_code=409)
        await controls._claim(check_db, current, query, update_id, request_digest)
        current.status = "cancel_intent" if action.action == "cancel" else "channel_action_intent"
        current.result_json = json.dumps({"task_id": current_task.task_id,
            "task_revision": current_task.task_revision, "task_status": current_task.status.value,
            "action": action.action, "status": "intent_committed", "no_learning": True}, sort_keys=True)
        check_db.add(current)
    return CapturedChannelControlCommit(action.action, task.task_id, task.task_revision,
        attempt.attempt_id, attempt.fencing_token, parent.run_identity, parent.revision, parent.fencing_token,
        row.id, callback_digest, reservation_digest, query["id"], update_id, request_digest,
        source_check, consume, _CONTROL_COMMIT_SEAL)


async def check_captured_control_commit(db, commit, *, action):
    if (type(commit) is not CapturedChannelControlCommit or commit._seal is not _CONTROL_COMMIT_SEAL
        or commit.action != action):
        raise BoardError("channel_control_commit_required", "Use the original callback owner", status_code=403)
    await commit.source_check(db)


async def consume_captured_control_commit(db, commit, *, action, task, attempt, parent):
    """Called only in the final native writer immediately before paired CAS."""
    if (type(commit) is not CapturedChannelControlCommit or commit._seal is not _CONTROL_COMMIT_SEAL
        or commit.action != action or commit.task_id != task.task_id or commit.task_revision != task.task_revision
        or commit.attempt_id != attempt.attempt_id or commit.board_fence != attempt.fencing_token
        or commit.parent_id != parent.run_identity or commit.parent_revision != parent.revision
        or commit.parent_fence != parent.fencing_token):
        raise BoardError("channel_control_commit_changed", "Original native callback binding changed", status_code=409)
    await commit.source_check(db)
    await commit.consume(db, task)


@dataclass(frozen=True)
class VerifiedChannelOutput:
    """Private physical readback witness; a caller-shaped receipt is not one."""
    task_id: str
    task_revision: int
    attempt_id: str
    parent_job_id: str
    parent_revision: int
    manifest_digest: str
    pairing_id: str
    reference: dict
    original_deadline_at: datetime
    completed_at: datetime
    native_sql_digest: str
    _seal: object
    identity: object = None


_OUTPUT_SEAL = object()


def _terminal_sql_digest(task, attempt, parent, children, accounting):
    from src.work_board.general_task import digest
    return digest([task.task_id, task.task_revision, task.typed_input_digest,
        task.status.value, task.result_refs_json, task.artifact_refs_json,
        attempt.attempt_id, str(attempt.ended_at), attempt.outcome, attempt.fencing_token,
        attempt.workflow_run_id, str(attempt.cancel_requested_at), attempt.lease_owner,
        str(attempt.lease_expires_at), parent.run_identity, parent.revision,
        parent.status, parent.fencing_token, parent.lease_owner, str(parent.lease_expires_at),
        parent.effect_receipts_json, parent.checkpoint_receipts_json, parent.artifact_receipts_json,
        sorted([child.run_identity, child.revision, child.status, child.fencing_token,
            child.lease_owner, str(child.lease_expires_at), child.effect_receipts_json,
            child.checkpoint_receipts_json, child.artifact_receipts_json] for child in children),
        sorted([row.operation_id, row.state, row.bound_microusd,
            row.actual_cost_microusd, row.evidence_json] for row in accounting)])


async def check_channel_output_publication(db, owner, output, *, adapter, staged_workspace):
    """SQL-only current check of a private, previously physical output witness."""
    from sqlalchemy import select, func
    from src.db.models import WorkBoardAttempt, WorkflowRunState, InferenceCostReservation
    from src.workflows.general_task_guard import read_manifest
    from src.work_board.general_task import digest
    if type(output) is not VerifiedChannelOutput or output._seal is not _OUTPUT_SEAL:
        raise BoardError("channel_output_proof_required", "Original physical output readback required", status_code=403)
    task, reservation = await check_captured_task_control(db, owner, ChannelAction(task_id=output.task_id,
        action="inspect", expected_revision=output.task_revision), adapter=adapter,
        _workspace_digest=staged_workspace, _identity=output.identity)
    attempt = await db.get(WorkBoardAttempt, output.attempt_id, populate_existing=True)
    parent = await db.scalar(select(WorkflowRunState).where(
        WorkflowRunState.run_identity == output.parent_job_id).execution_options(populate_existing=True))
    children = list((await db.execute(select(WorkflowRunState).where(
        WorkflowRunState.parent_job_id == output.parent_job_id).execution_options(populate_existing=True))).scalars().all())
    accounting = list((await db.execute(select(InferenceCostReservation).where(
        InferenceCostReservation.owner_id == owner.principal_id,
        InferenceCostReservation.goal_id == task.goal_id,
        InferenceCostReservation.goal_revision == task.goal_revision,
        func.instr(InferenceCostReservation.evidence_json, '"' + reservation.proposal_group.group_id + '"') > 0,
    ).limit(reservation.proposal_group.max_inference_calls + 1).execution_options(populate_existing=True))).scalars().all())
    manifest = read_manifest(parent) if parent else None
    if (attempt is None or parent is None or manifest is None
        or digest(manifest.model_dump(mode="json")) != output.manifest_digest
        or _terminal_sql_digest(task, attempt, parent, children, accounting) != output.native_sql_digest):
        raise BoardError("channel_output_binding_changed", "Original physical output source changed", status_code=409)
    return task


def channel_output_key(output):
    from src.work_board.general_task import digest
    if type(output) is not VerifiedChannelOutput or output._seal is not _OUTPUT_SEAL:
        raise BoardError("channel_output_proof_required", "Original physical output required", status_code=403)
    return "channel-output:" + digest([output.task_id, output.attempt_id, output.parent_job_id,
        output.parent_revision, output.manifest_digest, output.reference["artifact_id"],
        output.reference["content_sha256"]])


async def read_channel_outbox_output(db, row, *, adapter):
    """Read the original source-owned completion token, then physical output."""
    from src.db.models import Message
    from src.work_board.contracts import WorkBoardOwner
    from sqlalchemy import select
    owner = WorkBoardOwner(principal_id=row.owner_principal_id, session_id=row.operator_session_id)
    message = (await db.execute(select(Message.metadata_json, Message.role, Message.session_id,
        Message.owner_principal_id).where(Message.id == row.message_id))).one_or_none()
    try:
        metadata = json.loads(message.metadata_json) if message else {}
        retained = metadata["channel_output.v1"]
        if (set(retained) != {"handle", "relative_path", "task_id", "attempt_id"}
            or message.role != "assistant" or message.session_id != row.session_id
            or message.owner_principal_id != owner.principal_id
            or retained["relative_path"] != "/?channel_output=" + retained["handle"]):
            raise ValueError()
        handle = read_output_handle(retained["handle"], owner)
        if handle.task_id != retained["task_id"] or handle.attempt_id != retained["attempt_id"]:
            raise ValueError()
        output = await read_captured_terminal_output(db, owner, ChannelAction(task_id=handle.task_id,
            action="inspect", expected_revision=handle.task_revision), adapter=adapter)
        if (issue_output_handle(owner, output) != retained["handle"]
            or channel_output_key(output) != row.idempotency_key):
            raise ValueError()
        return owner, output, current_workspace_digest()
    except (ValueError, TypeError, KeyError, AttributeError, OSError):
        raise BoardError("channel_output_outbox_changed", "Original completion source is unavailable", status_code=409) from None


async def maybe_publish_channel_output(task, attempt_id, *, adapter=None):
    """Optional post-commit hook; an ordinary Task is never a channel event."""
    from src.db import engine
    from src.db.models import WorkBoardTask, WorkBoardAttempt
    from src.work_board.contracts import WorkBoardOwner, WorkBoardStatus
    from src.extensions.telegram_transport import default_telegram_transport
    adapter = adapter or default_telegram_transport
    if task.capability_id != "agent.task.v1" or task.status not in {WorkBoardStatus.review, WorkBoardStatus.done}:
        return None
    owner = WorkBoardOwner(principal_id=task.owner_principal_id, session_id=task.owner_session_id)
    async with engine.get_session() as db:
        current = await db.get(WorkBoardTask, task.creation_sequence, populate_existing=True)
        attempt = await db.get(WorkBoardAttempt, attempt_id, populate_existing=True)
        if (current is None or attempt is None or current.task_revision != task.task_revision
            or attempt.task_id != current.task_id or attempt.ended_at is None or attempt.outcome != "verified"):
            return None
        if not await check_capture_origin(db, owner, current, _classify_only=True):
            return None
        # Audio-only capture has no paired Telegram output destination.
        reservation = await check_capture_origin(db, owner, current,
            _workspace_digest=current_workspace_digest())
        if reservation.source_kind != "telegram":
            return None
        output = await read_captured_terminal_output(db, owner, ChannelAction(task_id=current.task_id,
            action="inspect", expected_revision=current.task_revision), adapter=adapter)
        conversation = current.origin_session_id
    notice = ("Seraph output is ready for review in the local Cockpit."
        if task.status is WorkBoardStatus.review else "Seraph Task is done. Its verified output is available in the local Cockpit.")
    return await adapter.enqueue_outbound(notice, owner_principal_id=owner.principal_id,
        operator_session_id=owner.session_id, session_id=conversation,
        idempotency_key=channel_output_key(output), _channel_output=output)


class ChannelOutputHandleV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["channel-output-handle.v1"] = "channel-output-handle.v1"
    owner_principal_id: str = Field(min_length=1, max_length=128)
    original_root_id: str = Field(min_length=1, max_length=128)
    pairing_id: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=128)
    task_revision: int = Field(ge=1)
    attempt_id: str = Field(min_length=1, max_length=128)
    parent_job_id: str = Field(min_length=1, max_length=300)
    parent_revision: int = Field(ge=1)
    artifact_id: str = Field(pattern=r"^art_[a-f0-9]{24}$")
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    manifest_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    expires_at: datetime


def _output_signing_key():
    import hashlib
    from config.settings import settings
    value = str(settings.operator_auth_secret or settings.operator_auth_secret_hash or "").strip()
    if not value:
        raise BoardError("channel_output_signer_unavailable", "Configured server identity is required", status_code=409)
    return hashlib.sha256(("seraph.channel-output.v1:" + value).encode()).digest()


def issue_output_handle(owner, output):
    import base64, hashlib, hmac
    from datetime import timedelta
    if type(output) is not VerifiedChannelOutput or output._seal is not _OUTPUT_SEAL:
        raise BoardError("channel_output_proof_required", "Original physical output readback required", status_code=403)
    expires = min(output.original_deadline_at, output.completed_at + timedelta(seconds=300))
    if expires <= datetime.now(timezone.utc):
        raise BoardError("channel_output_expired", "Original output authority expired", status_code=409)
    handle = ChannelOutputHandleV1(owner_principal_id=owner.principal_id, original_root_id=owner.session_id,
        pairing_id=output.pairing_id, task_id=output.task_id, task_revision=output.task_revision,
        attempt_id=output.attempt_id, parent_job_id=output.parent_job_id, parent_revision=output.parent_revision,
        artifact_id=output.reference["artifact_id"], content_sha256=output.reference["content_sha256"],
        manifest_digest=output.manifest_digest, expires_at=expires)
    encoded = base64.urlsafe_b64encode(handle.model_dump_json().encode()).rstrip(b"=").decode("ascii")
    signature = hmac.new(_output_signing_key(), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return "sco1." + encoded + "." + signature


def read_output_handle(token, owner):
    import base64, hashlib, hmac, re
    from pydantic import ValidationError
    if type(token) is not str or len(token) > 4096:
        raise BoardError("channel_output_handle_invalid", "Use the original signed output link", status_code=403)
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != "sco1" or not re.fullmatch(r"[A-Za-z0-9_-]+", parts[1]) or not re.fullmatch(r"[a-f0-9]{64}", parts[2]):
        raise BoardError("channel_output_handle_invalid", "Use the original signed output link", status_code=403)
    expected = hmac.new(_output_signing_key(), parts[1].encode("ascii"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, parts[2]):
        raise BoardError("channel_output_handle_invalid", "Output link signature is invalid", status_code=403)
    try:
        handle = ChannelOutputHandleV1.model_validate_json(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, ValidationError) as exc:
        raise BoardError("channel_output_handle_invalid", "Output link schema is invalid", status_code=403) from exc
    if (handle.owner_principal_id != owner.principal_id or handle.original_root_id != owner.session_id
        or handle.expires_at.tzinfo is None or handle.expires_at <= datetime.now(timezone.utc)):
        raise BoardError("channel_output_handle_expired", "Original owner or output authority is unavailable", status_code=403)
    return handle


async def resolve_output_handle(db, owner, token, *, adapter):
    handle = read_output_handle(token, owner)
    try:
        output = await read_captured_terminal_output(db, owner, ChannelAction(task_id=handle.task_id,
            action="inspect", expected_revision=handle.task_revision), adapter=adapter)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise BoardError("channel_output_readback_unavailable", "Original physical output is unavailable; inspect current Work", status_code=409) from exc
    if (output.pairing_id != handle.pairing_id or output.attempt_id != handle.attempt_id
        or output.parent_job_id != handle.parent_job_id or output.parent_revision != handle.parent_revision
        or output.manifest_digest != handle.manifest_digest or output.reference["artifact_id"] != handle.artifact_id
        or output.reference["content_sha256"] != handle.content_sha256):
        raise BoardError("channel_output_handle_changed", "Exact original output changed; inspect current Work", status_code=409)
    return {"task_id": output.task_id, "task_revision": output.task_revision,
        "attempt_id": output.attempt_id, "owner_session_id": owner.session_id,
        "workflow_run_id": output.parent_job_id, "parent_workflow_run_id": None,
        "reference": dict(output.reference), "no_learning": True}


async def read_captured_terminal_output(db, owner, action, *, adapter):
    """One exact declared final output from an actually ended original execution."""
    import json
    from sqlalchemy import select, func
    from src.db.models import WorkBoardAttempt, WorkflowRunState, InferenceCostReservation, TelegramTransportState
    from src.work_board.contracts import WorkBoardStatus
    from src.workflows.general_task_guard import read_manifest, child_binding
    from src.workflows.job_runtime import _job_has_unsafe_effects
    from src.work_board.general_task_runtime_artifacts import verify_readonly_native_projection, read_current_native_outputs
    from src.work_board.general_task_native import current_plan
    from src.memory.evidence_working_set import _verified_receipts, _read_file
    from src.workflows.general_task_accounting import entry_for
    from src.work_board.general_task import digest
    identity = stage_captured_identity()
    staged_workspace = identity.workspace_digest
    task, reservation = await check_captured_task_control(db, owner, action, adapter=adapter,
        _workspace_digest=staged_workspace, _identity=identity)
    if task.status not in {WorkBoardStatus.review, WorkBoardStatus.done}:
        raise BoardError("channel_output_not_verified", "Original execution has not completed for review", status_code=409)
    attempts = list((await db.execute(select(WorkBoardAttempt).where(
        WorkBoardAttempt.task_id == task.task_id).order_by(WorkBoardAttempt.started_at.desc(),
            WorkBoardAttempt.attempt_id.desc()).limit(2).execution_options(populate_existing=True))).scalars().all())
    if not attempts or attempts[0].ended_at is None or attempts[0].outcome != "verified" or attempts[0].cancel_requested_at is not None:
        raise BoardError("channel_output_not_verified", "Exact ended verified attempt required", status_code=409)
    attempt = attempts[0]
    parent = (await db.execute(select(WorkflowRunState).where(
        WorkflowRunState.run_identity == attempt.workflow_run_id).execution_options(populate_existing=True))).scalars().one_or_none()
    manifest = read_manifest(parent) if parent else None
    if (parent is None or manifest is None or parent.status != "succeeded"
        or parent.lease_owner or parent.lease_expires_at or attempt.lease_owner or attempt.lease_expires_at
        or parent.owner_kind != "user" or parent.job_kind != "agent.task.v1"
        or manifest.task_revision + 1 != task.task_revision
        or manifest.board_fence != attempt.fencing_token or manifest.job_fence != parent.fencing_token
        or manifest.group_id != reservation.proposal_group.group_id
        or _job_has_unsafe_effects(json.loads(parent.effect_receipts_json))):
        raise BoardError("channel_output_binding_changed", "Original completed native binding required", status_code=409)
    # Shared accounting remains with its original owner. Held contact/cost is
    # never promoted into a successful output merely because a Task ended.
    rows = (await db.execute(select(InferenceCostReservation).where(
        InferenceCostReservation.owner_id == owner.principal_id,
        InferenceCostReservation.goal_id == task.goal_id,
        InferenceCostReservation.goal_revision == task.goal_revision,
        func.instr(InferenceCostReservation.evidence_json, '"' + reservation.proposal_group.group_id + '"') > 0,
    ).limit(reservation.proposal_group.max_inference_calls + 1))).scalars().all()
    if len(rows) > reservation.proposal_group.max_inference_calls:
        raise BoardError("channel_output_accounting_unresolved", "Original group call allowance changed", status_code=409)
    for row in rows:
        entry = entry_for(row)
        if entry and entry["group"]["group_id"] == reservation.proposal_group.group_id:
            if row.state not in {"settled", "released"} or (row.state == "settled" and (
                type(row.actual_cost_microusd) is not int or not 0 <= row.actual_cost_microusd <= row.bound_microusd)):
                raise BoardError("channel_output_accounting_unresolved", "Original inference liability must settle", status_code=409)
    envelope = await verify_readonly_native_projection(db, owner, parent, task, attempt, manifest)
    plan = current_plan(manifest, envelope)
    step_ids = [step.step_id for step in plan.steps]
    if not step_ids or manifest.step_ids != step_ids:
        raise BoardError("channel_output_incomplete", "All original native steps must have verified outputs", status_code=409)
    children = list((await db.execute(select(WorkflowRunState).where(
        WorkflowRunState.parent_job_id == parent.run_identity))).scalars().all())
    if sorted(child.run_identity for child in children) != sorted(manifest.admitted_invocation_ids):
        raise BoardError("channel_output_incomplete", "Exact original admitted child set required", status_code=409)
    for child in children:
        binding = child_binding(child)
        if (child.status != "succeeded" or child.lease_owner or child.lease_expires_at
            or binding.creation_digest != manifest.creation_digest
            or _job_has_unsafe_effects(json.loads(child.effect_receipts_json))):
            raise BoardError("channel_output_child_unresolved", "Original children must close without Unknown effects", status_code=409)
    outputs = await read_current_native_outputs(db, parent, task, attempt, manifest, envelope, step_ids)
    declared = json.loads(task.result_refs_json)
    retained = _verified_receipts(parent)
    if not isinstance(declared, list) or len(declared) != 1:
        raise BoardError("channel_output_ambiguous", "Exactly one declared final artifact required", status_code=409)
    if not isinstance(declared[0], dict) or set(declared[0]) != {"file_path", "content_sha256", "size_bytes"}:
        raise BoardError("channel_output_artifact_changed", "Exact canonical Task artifact projection required", status_code=409)
    checkpoints = [item for item in json.loads(parent.checkpoint_receipts_json)
        if item.get("checkpoint_id") == "general:artifact:" + step_ids[-1]]
    if len(checkpoints) != 1:
        raise BoardError("channel_output_artifact_changed", "Original final assembly checkpoint required", status_code=409)
    checkpoint = checkpoints[0]
    final = checkpoint.get("payload")
    plan_digest = digest(envelope.model_copy(update={"plan": plan}).model_dump(mode="json"))
    if (not isinstance(final, dict) or set(final) != {"schema_version", "producer_ref", "step_id", "plan_digest",
        "producer_fence", "file_path", "content_sha256", "size_bytes", "no_learning"}
        or final["schema_version"] != 1 or final["producer_ref"] != parent.run_identity
        or final["step_id"] != step_ids[-1] or final["plan_digest"] != plan_digest
        or final["producer_fence"] != parent.fencing_token or final["no_learning"] is not True
        or type(final["size_bytes"]) is not int or not 0 < final["size_bytes"] <= 65536
        or checkpoint.get("safe") is not True or checkpoint.get("state_digest") != digest(final)
        or any(declared[0][key] != final[key] for key in declared[0])):
        raise BoardError("channel_output_artifact_changed", "Original final assembly binding required", status_code=409)
    expected_path = f"artifacts/work-board/general-tasks/{digest([parent.run_identity, plan_digest, step_ids[-1]])}-{final['content_sha256']}.json"
    refs = [ref for ref in retained if ref.get("content_sha256") == final["content_sha256"]
        and ref.get("file_path") == final["file_path"] == expected_path
        and ref.get("size_bytes") == final["size_bytes"]]
    if len(refs) != 1 or refs[0].get("artifact_type") != "general_task_step":
        raise BoardError("channel_output_artifact_changed", "Exact native final artifact readback required", status_code=409)
    reference = refs[0]
    physical = json.loads(_read_file(reference["file_path"], reference["content_sha256"]))
    if physical != {"step_id": step_ids[-1], "output": outputs[step_ids[-1]]}:
        raise BoardError("channel_output_artifact_changed", "Physical final output differs from its native receipt", status_code=409)
    # A read itself grants no delivery. Final publication/delivery must recheck
    # this original source and the same physical tuple under its own writer.
    task, reservation = await check_captured_task_control(db, owner, action, adapter=adapter,
        _workspace_digest=staged_workspace, _identity=identity)
    pairing = await db.get(TelegramTransportState, "telegram", populate_existing=True)
    return VerifiedChannelOutput(task.task_id, task.task_revision, attempt.attempt_id,
        parent.run_identity, parent.revision, digest(manifest.model_dump(mode="json")),
        pairing.pairing_id,
        dict(reference), reservation.proposal_group.original_deadline_at,
        attempt.ended_at.replace(tzinfo=timezone.utc) if attempt.ended_at.tzinfo is None else attempt.ended_at,
        _terminal_sql_digest(task, attempt, parent, children, rows),
        _OUTPUT_SEAL, identity)
