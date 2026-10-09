"""Original Telegram document acquisition into the existing private source owner.

No caller mapping grants acquisition. The original event issuer records a closed
binding; a privately identity-sealed witness resolves and validates that event.
All filesystem, Vault and HTTP work stays outside canonical SQL writers.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
import re
import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from src.work_board import document_pairs as sources
from src.work_board.contracts import WorkBoardOwner
from src.work_board.repository import BoardError, WorkBoardRepository
from src.work_board.input_artifacts import (
    _begin_immediate, _metadata_digest, _open_input_artifact_parent,
    _private_input_file_metadata, INPUT_ARTIFACT_ROOT,
)
from src.work_board.pipelines import now, utc, root_binding

SOURCE_LIMIT = 16 * 1024 * 1024
DOCX_LIMIT = 10 * 1024 * 1024
CHARGE = 32 * 1024 * 1024
_WITNESS_SEAL = object()
_PUBLICATION_SEAL = object()
_READBACK_SEAL = object()
_LEASE_SEAL = object()
_OBSERVED_SEAL = object()


class OriginalDocumentBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema: Literal["telegram-document-ingest.v1"] = "telegram-document-ingest.v1"
    owner_principal_id: str = Field(min_length=1, max_length=128)
    original_root_id: str = Field(min_length=1, max_length=128)
    pairing_id: str = Field(min_length=1, max_length=128)
    pairing_revision: int = Field(ge=1, le=2**63-1)
    pairing_authority_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    selection_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    event_id: str = Field(min_length=1, max_length=256)
    provider_update_id: int = Field(ge=1, le=2**63-1)
    provider_request_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    canonical_message_id: str = Field(min_length=1, max_length=128)
    conversation_session_id: str = Field(min_length=1, max_length=256)
    message_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    reservation_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    creation_request_key: str = Field(min_length=1, max_length=128)
    proposal_group_id: str = Field(min_length=1, max_length=128)
    proposal_group_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    initial_input_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    goal_id: str = Field(min_length=1, max_length=128)
    goal_revision: int = Field(ge=1, le=2**63-1)
    provider_file_id: str = Field(min_length=1, max_length=512)
    format: Literal["pdf", "docx", "xlsx", "csv"]
    declared_size: int = Field(ge=1, le=SOURCE_LIMIT)
    original_cutoff: str = Field(min_length=1, max_length=64)
    no_learning: Literal[True] = True

    @field_validator("owner_principal_id", "original_root_id", "pairing_id", "event_id",
        "canonical_message_id", "conversation_session_id", "creation_request_key",
        "proposal_group_id", "goal_id", "provider_file_id")
    @classmethod
    def finite_ascii(cls, value):
        if not value.isascii() or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError("bounded printable ASCII identity required")
        return value


def _pair_authority(pair):
    return sources.sha256(sources.canonical([pair.pairing_id, pair.owner_principal_id,
        pair.operator_session_id, pair.operator_id, pair.chat_id, pair.token_secret_ref,
        pair.token_fingerprint, pair.transit_consent_reference,
        str(pair.transit_consent_expires_at), str(pair.pairing_expires_at)]))


def normalize_document(message):
    """Provider document metadata is untrusted and never claims byte verification."""
    document = message.get("document")
    if document is None:
        return None
    if type(document) is not dict or message.get("voice") or message.get("audio"):
        raise BoardError("channel_document_metadata_invalid", "Select one supported task document", status_code=422)
    file_id, size = document.get("file_id"), document.get("file_size")
    filename = document.get("file_name")
    media = document.get("mime_type")
    formats = {"application/pdf": "pdf", "text/csv": "csv",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx"}
    extension = filename.rsplit(".", 1)[-1].lower() if type(filename) is str and "." in filename else None
    selected = formats.get(media)
    if selected is None or extension != selected:
        raise BoardError("channel_document_format_unsupported", "Select a PDF, DOCX, XLSX or CSV with matching format hints", status_code=422)
    if (type(file_id) is not str or not 1 <= len(file_id) <= 512 or not file_id.isascii()
        or any(ord(c) < 32 or ord(c) == 127 for c in file_id)
        or type(size) is not int or not 1 <= size <= (DOCX_LIMIT if selected == "docx" else SOURCE_LIMIT)):
        raise BoardError("channel_document_size_or_identity_invalid", "A bounded advertised size and downloadable identity are required", status_code=422)
    return {"file_id": file_id, "size_bytes": size, "format": selected}


def issue_original_document_binding(pair, envelope, reservation, document, *, provider_update_id, provider_request_digest):
    """Only the original provider event writer calls this issuance seam."""
    from src.extensions.telegram_transport import telegram_state_revision
    from src.work_board.channel_capture import telegram_capture_binding_digest
    group = reservation.proposal_group
    return OriginalDocumentBinding(owner_principal_id=reservation.owner_principal_id,
        original_root_id=reservation.original_root_id, pairing_id=pair.pairing_id,
        pairing_revision=telegram_state_revision(pair), pairing_authority_digest=_pair_authority(pair),
        selection_digest=telegram_capture_binding_digest(envelope), event_id=reservation.source_id,
        provider_update_id=provider_update_id, provider_request_digest=provider_request_digest,
        canonical_message_id=reservation.canonical_message_id,
        conversation_session_id=reservation.conversation_session_id,
        message_digest=reservation.source_digest,
        reservation_digest=sources.sha256(sources.canonical(reservation.model_dump(mode="json"))),
        creation_request_key=reservation.idempotency_key, proposal_group_id=group.group_id,
        proposal_group_digest=sources.sha256(sources.canonical(group.model_dump(mode="json"))),
        initial_input_digest=group.initial_input_digest,
        goal_id=reservation.task_input.goal_ref, goal_revision=reservation.goal_revision,
        provider_file_id=document["file_id"], format=document["format"], declared_size=document["size_bytes"],
        original_cutoff=min(envelope.selection.expires_at, group.original_deadline_at).isoformat())


@dataclass(frozen=True)
class TelegramDocumentAcquisitionWitness:
    binding: OriginalDocumentBinding
    workspace_json: str
    _seal: object = field(default=None, repr=False, compare=False)
    _issued_id: int = field(default=0, repr=False, compare=False)


def _issued(value):
    object.__setattr__(value, "_issued_id", id(value))
    return value


def _checked(witness):
    if (type(witness) is not TelegramDocumentAcquisitionWitness or witness._seal is not _WITNESS_SEAL
        or witness._issued_id != id(witness)):
        raise BoardError("channel_document_original_source_required", "Use the genuine selected provider event", status_code=403)
    return witness.binding


async def resolve_original_document(db, owner, event_id):
    from src.db.models import TelegramInboundUpdate
    event = await db.scalar(select(TelegramInboundUpdate).where(
        TelegramInboundUpdate.idempotency_key == event_id,
        TelegramInboundUpdate.owner_principal_id == owner.principal_id,
        TelegramInboundUpdate.operator_session_id == owner.session_id).execution_options(populate_existing=True))
    try:
        binding = OriginalDocumentBinding.model_validate(json.loads(event.receipt_json)["channel_task_capture"]["document_acquisition"])
        if (binding.event_id != event.idempotency_key or binding.owner_principal_id != owner.principal_id
            or binding.original_root_id != owner.session_id):
            raise ValueError()
    except (AttributeError, KeyError, ValueError, TypeError):
        raise BoardError("channel_document_original_source_required", "The original selected document event is unavailable", status_code=409) from None
    witness = _issued(TelegramDocumentAcquisitionWitness(binding,
        sources.canonical(dict(root_binding())).decode(), _WITNESS_SEAL))
    await validate_original_document(db, witness)
    return witness


async def validate_original_document(db, witness):
    """SQL-only fresh authority checks; workspace facts were staged by the owner."""
    from src.db.models import TelegramInboundUpdate, TelegramTransportState, Message, OperatorSession
    from src.work_board.channel_capture import (
        decode_telegram_capture_binding, TelegramCaptureBindingV2, telegram_capture_binding_digest,
        ChannelCaptureReservationV1,
    )
    from src.workflows.general_task_accounting import validate_group_owner
    binding = _checked(witness)
    owner = WorkBoardOwner(principal_id=binding.owner_principal_id, session_id=binding.original_root_id)
    root = await db.get(OperatorSession, binding.original_root_id, populate_existing=True)
    stamp = now()
    if (root is None or root.principal_id != binding.owner_principal_id or root.is_bearer_tombstone
        or root.revoked_at is not None or root.replaced_by_id is not None or stamp >= utc(root.idle_expires_at)
        or stamp >= utc(root.absolute_expires_at) or stamp >= utc(datetime.fromisoformat(binding.original_cutoff))):
        raise BoardError("channel_document_root_or_deadline_changed", "Original document authority expired or changed", status_code=409)
    pair = await db.scalar(select(TelegramTransportState).where(
        TelegramTransportState.pairing_id == binding.pairing_id).execution_options(populate_existing=True))
    if (pair is None or pair.pairing_state != "active" or pair.revoked_at is not None
        or _pair_authority(pair) != binding.pairing_authority_digest
        or not pair.transit_consent_reference or pair.transit_consent_expires_at is None
        or utc(pair.transit_consent_expires_at) <= stamp
        or pair.pairing_expires_at is not None and utc(pair.pairing_expires_at) <= stamp):
        raise BoardError("channel_document_pairing_changed", "Original document pairing or transit authority changed", status_code=409)
    try:
        envelope = decode_telegram_capture_binding(pair.capture_binding_json)
        if (type(envelope) is not TelegramCaptureBindingV2 or not envelope.selection.enabled
            or envelope.selected_document_event_id != binding.event_id
            or telegram_capture_binding_digest(envelope) != binding.selection_digest):
            raise ValueError()
        event = await db.scalar(select(TelegramInboundUpdate).where(
            TelegramInboundUpdate.idempotency_key == binding.event_id).execution_options(populate_existing=True))
        stored = json.loads(event.receipt_json)["channel_task_capture"]
        reservation = ChannelCaptureReservationV1.model_validate(stored["reservation"])
        if (OriginalDocumentBinding.model_validate(stored["document_acquisition"]) != binding
            or sources.sha256(sources.canonical(reservation.model_dump(mode="json"))) != binding.reservation_digest
            or event.status != "accepted" or event.owner_principal_id != owner.principal_id
            or event.operator_session_id != owner.session_id or event.update_id != binding.provider_update_id
            or event.request_digest != binding.provider_request_digest or event.operator_id != pair.operator_id
            or event.chat_id != pair.chat_id or event.canonical_message_id != binding.canonical_message_id
            or event.content_digest != binding.message_digest or event.session_id != binding.conversation_session_id):
            raise ValueError()
        message = await db.get(Message, binding.canonical_message_id, populate_existing=True)
        message_metadata = json.loads(message.metadata_json) if message else {}
        if (message is None or message.role != "user" or message.session_id != binding.conversation_session_id
            or sources.sha256(message.content.encode()) != binding.message_digest
            or message.content != reservation.task_input.intent
            or message_metadata.get("telegram", {}).get("document") != {"file_id": binding.provider_file_id,
                "size_bytes": binding.declared_size, "format": binding.format}
            or message_metadata.get("telegram", {}).get("request_digest") != binding.provider_request_digest):
            raise ValueError()
    except (ValueError, KeyError, AttributeError, TypeError):
        raise BoardError("channel_document_event_changed", "Original selection, event or request changed", status_code=409) from None
    from src.workflows.job_runtime import DurableJobTransitionError
    try:
        await validate_group_owner(db, reservation.proposal_group)
    except DurableJobTransitionError:
        raise BoardError("channel_document_group_changed", "Original document group or Goal authority changed", status_code=409) from None
    await WorkBoardRepository._validate_goal(db, owner, goal_id=binding.goal_id, goal_revision=binding.goal_revision)
    return pair, event


def validate_file_path(value):
    if (type(value) is not str or not value.isascii() or not 1 <= len(value) <= 512):
        raise BoardError("document_path_rejected", "Unsupported Telegram document path", status_code=409)
    segments = value.split("/")
    if not 1 <= len(segments) <= 16 or any(not 1 <= len(segment) <= 128
        or segment in {".", ".."} or re.fullmatch(r"[A-Za-z0-9_.-]+", segment) is None for segment in segments):
        raise BoardError("document_path_rejected", "Unsupported Telegram document path", status_code=409)
    return value


def _persist(row, value):
    raw = sources.canonical(value)
    if len(raw) > 8192:
        raise BoardError("channel_document_metadata_bound", "The original acquisition binding exceeds private metadata capacity", status_code=409)
    row.document_metadata_json = raw.decode()


def validate_channel_metadata(row, value):
    channel = value.get("channel_ingest")
    required = {"schema", "binding", "binding_digest", "pending_payload_sha256", "contact", "observed", "publication", "task_id"}
    try:
        if (type(channel) is not dict or not required <= set(channel)
            or set(channel) - required - {"physical", "cleanup", "task_origin_digest"} or channel["schema"] != "telegram-document-ingest.v1"
            or channel["contact"] not in {"not_started", "started", "closed", "unknown"}
            or re.fullmatch(r"[a-f0-9]{64}", str(channel["pending_payload_sha256"])) is None):
            raise ValueError()
        binding = OriginalDocumentBinding.model_validate(channel["binding"])
        binding_digest = sources.sha256(sources.canonical(binding.model_dump(mode="json")))
        if (channel["binding_digest"] != binding_digest
            or row.artifact_id != str(uuid.uuid5(uuid.NAMESPACE_URL, "seraph:telegram-document:" + binding_digest))
            or row.owner_principal_id != binding.owner_principal_id or row.owner_session_id != binding.original_root_id
            or row.goal_id != binding.goal_id or row.goal_revision != binding.goal_revision or value["generation"] != 1):
            raise ValueError()
    except (KeyError, ValueError, TypeError):
        raise BoardError("channel_document_source_changed", "Original document acquisition metadata changed", status_code=409) from None


def _source_matches_witness(row, value, witness):
    binding = _checked(witness)
    validate_channel_metadata(row, value)
    if value["channel_ingest"]["binding"] != binding.model_dump(mode="json"):
        raise BoardError("channel_document_source_changed", "Original pending source/event binding changed", status_code=409)


def _pending_binding(value):
    return sources.sha256(sources.canonical(value["channel_ingest"]["binding"]))


async def reserve_channel_source(db, owner, witness):
    from src.db.models import WorkBoardInputArtifact
    binding = _checked(witness)
    if (owner.principal_id, owner.session_id) != (binding.owner_principal_id, binding.original_root_id):
        raise BoardError("channel_document_owner_changed", "Use the original document owner", status_code=403)
    staged_root = dict(root_binding())
    if sources.canonical(staged_root).decode() != witness.workspace_json:
        raise BoardError("channel_document_workspace_changed", "Use the original private workspace", status_code=409)
    binding_digest = sources.sha256(sources.canonical(binding.model_dump(mode="json")))
    identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, "seraph:telegram-document:" + binding_digest))
    inputs = {"schema": "telegram-document-ingest.v1", "artifact_ref": "document-source:" + identifier,
        "format": binding.format, "declared_size": binding.declared_size,
        "binding_digest": binding_digest, "no_learning": True}
    stub = sources.canonical(inputs)
    await _begin_immediate(db)
    await validate_original_document(db, witness)
    existing = await db.get(WorkBoardInputArtifact, identifier, populate_existing=True)
    if existing is not None:
        _source_matches_witness(existing, sources.metadata(existing), witness)
        if sources.metadata(existing).get("channel_ingest", {}).get("binding_digest") != binding_digest:
            raise BoardError("channel_document_source_changed", "Original document reservation changed", status_code=409)
        await db.commit()
        return existing, sources.metadata(existing)
    stamp = now()
    deadline = min(stamp + timedelta(seconds=300), utc(datetime.fromisoformat(binding.original_cutoff)))
    row = WorkBoardInputArtifact(artifact_id=identifier, owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id, goal_id=binding.goal_id, goal_revision=binding.goal_revision,
        capability_id=sources.SOURCE_CAPABILITY, capability_version="1", idempotency_key="channel-document:" + binding_digest,
        payload_sha256=sources.sha256(stub), typed_input_ref="workspace-json:" + INPUT_ARTIFACT_ROOT + "/" + identifier + "-" + sources.sha256(stub) + ".json",
        size_bytes=len(stub), state="pending", created_at=stamp, expires_at=stamp + timedelta(hours=24),
        document_reserved_bytes=CHARGE)
    value = {"schema": "document-source.v1", "root": staged_root, "input": inputs,
        "phase": "reserved", "generation": 1, "live_writer": None, "sources": {},
        "ingest_deadline": deadline.isoformat(), "channel_ingest": {
            "schema": "telegram-document-ingest.v1", "binding": binding.model_dump(mode="json"),
            "binding_digest": binding_digest, "pending_payload_sha256": row.payload_sha256,
            "contact": "not_started", "observed": None, "publication": None, "task_id": None}}
    goal, budget = await sources.authority(db, owner, row, value, staged_root)
    for cutoff in (goal.due_date, budget.period_expires_at):
        if cutoff is not None:
            deadline = min(deadline, utc(cutoff))
            row.expires_at = min(row.expires_at, utc(cutoff))
    value["ingest_deadline"] = deadline.isoformat()
    # Reserve exact finite future encoding with real workspace identity, before
    # provider contact. Long roots cannot silently overflow later metadata.
    future = json.loads(sources.canonical(value))
    future.update({"phase": "cleanup_required", "reason": "channel_document_unknown_cleanup_required",
        "live_writer": {"token": "f"*32, "slot": "source"},
        "upload_binding": {"file": "g1-source.upload-lock", "device": 2**63-1, "inode": 2**63-1,
            "binding": _lease_binding(row, value, "f"*32)}})
    future["channel_ingest"].update({"contact": "unknown", "observed": {"size_bytes": SOURCE_LIMIT, "sha256": "f"*64},
        "publication": _max_inventory(row), "task_id": "f"*128, "task_origin_digest": "f"*64,
        "physical": {slot: {"device": 2**63-1, "inode": 2**63-1, "size": _cipher_limit(SOURCE_LIMIT), "sha256": "f"*64}
            for slot in ("source", "payload")},
        "cleanup": {"positive_exclusive_lock": True, "binding_digest": "f"*64}})
    future["sources"] = {"source": {"cipher_size": _cipher_limit(SOURCE_LIMIT), "cipher_sha256": "f"*64,
        "device": 2**63-1, "inode": 2**63-1}}
    future["input"] = {"schema_version": 1, "artifact_ref": inputs["artifact_ref"], "format": binding.format,
        "source": {"size_bytes": SOURCE_LIMIT, "sha256": "f"*64}, "no_learning": True}
    if len(sources.canonical(future)) > 8192:
        raise BoardError("channel_document_metadata_bound", "Original acquisition metadata has insufficient publication headroom", status_code=409)
    await sources.check_quota(db, owner, CHARGE)
    _persist(row, value)
    db.add(row)
    await db.flush()
    await db.commit()
    return row, value


def _lease_binding(row, value, token):
    return {"schema": "telegram-document-upload-lease.v1", "artifact_id": row.artifact_id,
        "owner_principal_id": row.owner_principal_id, "owner_session_id": row.owner_session_id,
        "generation": 1, "slot": "source", "nonce": token,
        "pending_binding_digest": _pending_binding(value), "root_digest": sources.sha256(sources.canonical(value["root"]))}


def stage_channel_lease(row, value, token, profile):
    sources.validate_upload_profile(profile)
    parent, _leaf = _open_input_artifact_parent(sources.source_path(row, value, "source"), create=True)
    descriptor = -1
    name = "g1-source.upload-lock"
    try:
        if os.fstat(parent).st_dev != profile["directory_device"]:
            raise OSError("document lease filesystem differs")
        try:
            descriptor = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            created = True
        except FileExistsError:
            descriptor = os.open(name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            created = False
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not created:
            facts = os.fstat(descriptor)
            if value.get("live_writer") or not _private_input_file_metadata(facts) or not 0 < facts.st_size <= 2048:
                raise ValueError("original precommit lease unavailable")
            old = json.loads(os.read(descriptor, 2049))
            expected = _lease_binding(row, value, token)
            if set(old) != set(expected) or {**old, "nonce": token} != expected or re.fullmatch(r"[a-f0-9]{32}", str(old.get("nonce"))) is None:
                raise ValueError("original precommit lease binding changed")
            named = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if named.st_dev != facts.st_dev or named.st_ino != facts.st_ino:
                raise ValueError("original precommit lease name changed")
            os.lseek(descriptor, 0, os.SEEK_SET); os.ftruncate(descriptor, 0)
        raw = sources.canonical(_lease_binding(row, value, token))
        if os.write(descriptor, raw) != len(raw):
            raise OSError("document lease incomplete")
        os.fsync(descriptor); os.fsync(parent)
        facts = os.fstat(descriptor)
        if not _private_input_file_metadata(facts):
            raise OSError("document lease metadata changed")
        probe = os.open(name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            try: fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: pass
            else: raise OSError("document lease exclusion unavailable")
        finally: os.close(probe)
        return descriptor, {"file": name, "device": facts.st_dev, "inode": facts.st_ino,
            "binding": _lease_binding(row, value, token)}
    except BaseException:
        if descriptor >= 0: os.close(descriptor)
        raise
    finally:
        os.close(parent)


async def acquire_channel_source(db, owner, row, value, witness, *, upload_profile):
    from src.db.models import WorkBoardInputArtifact
    if value["channel_ingest"]["contact"] != "not_started" or value.get("live_writer") or value["phase"] != "reserved":
        raise BoardError("channel_document_acquisition_not_retryable", "Reconcile or delete the original source; acquisition cannot repeat", status_code=409)
    token = uuid.uuid4().hex
    try:
        descriptor, lease = stage_channel_lease(row, value, token, upload_profile)
    except (OSError, ValueError, TypeError, KeyError):
        raise BoardError("document_upload_lock_unavailable", "The exact original private lease is unavailable", status_code=409) from None
    try:
        await _begin_immediate(db)
        fresh, current = await sources.owned(db, owner, row.artifact_id, revision=row.revision, capability=sources.SOURCE_CAPABILITY)
        _source_matches_witness(fresh, current, witness)
        if current != value:
            raise BoardError("channel_document_source_changed", "The pending source changed", status_code=409)
        await validate_original_document(db, witness)
        await sources.authority(db, owner, fresh, current, json.loads(witness.workspace_json), ingest=True)
        rows = list((await db.scalars(select(WorkBoardInputArtifact).where(WorkBoardInputArtifact.document_reserved_bytes > 0))).all())
        active = [item for item in rows if sources.metadata(item).get("live_writer")]
        if len(active) >= 2 or any(item.owner_principal_id == owner.principal_id for item in active):
            raise BoardError("document_upload_writer_busy", "An original document writer retains capacity", status_code=409)
        current["upload_binding"] = lease
        current["live_writer"] = {"token": token, "slot": "source"}
        current["phase"] = "uploading"
        current["channel_ingest"]["contact"] = "started"
        _persist(fresh, current); fresh.revision += 1
        await db.commit()
        return fresh, current, _issued(_ChannelUploadLease(fresh.artifact_id, fresh.revision,
            _pending_binding(current), sources.canonical(lease).decode(), descriptor, _LEASE_SEAL))
    except BaseException:
        os.close(descriptor)
        raise


@dataclass(frozen=True)
class _ChannelUploadLease:
    artifact_id: str
    revision: int
    binding_digest: str
    binding_json: str
    descriptor: int = field(repr=False)
    _seal: object = field(default=None, repr=False, compare=False)
    _issued_id: int = field(default=0, repr=False, compare=False)


def _checked_lease(row, value, lease, *, physical=False):
    if (type(lease) is not _ChannelUploadLease or lease._seal is not _LEASE_SEAL
        or lease._issued_id != id(lease) or lease.artifact_id != row.artifact_id
        or lease.binding_digest != _pending_binding(value)
        or json.loads(lease.binding_json) != value["upload_binding"]):
        raise BoardError("channel_document_original_lease_required", "The actual original held acquisition is required", status_code=409)
    if physical:
        facts = os.fstat(lease.descriptor)
        original = value["upload_binding"]
        if (not _private_input_file_metadata(facts) or (facts.st_dev, facts.st_ino) != (original["device"], original["inode"])):
            raise BoardError("channel_document_original_lease_required", "Original acquisition inode changed", status_code=409)


@dataclass(frozen=True)
class _ObservedSource:
    lease: _ChannelUploadLease = field(repr=False)
    raw: bytes = field(repr=False)
    sha256: str
    _seal: object = field(default=None, repr=False, compare=False)
    _issued_id: int = field(default=0, repr=False, compare=False)


def original_channel_lease(row, value):
    lease = value["upload_binding"]
    writer = value.get("live_writer") or {"token": lease["binding"]["nonce"]}
    expected = _lease_binding(row, value, writer["token"])
    if lease["binding"] != expected or lease["file"] != "g1-source.upload-lock":
        raise ValueError("original document lease binding changed")
    parent, _leaf = _open_input_artifact_parent(sources.source_path(row, value, "source"), create=False)
    descriptor = -1
    try:
        descriptor = os.open(lease["file"], os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        facts = os.fstat(descriptor)
        if (not _private_input_file_metadata(facts) or facts.st_dev != lease["device"]
            or facts.st_ino != lease["inode"] or facts.st_size != len(sources.canonical(expected))):
            raise ValueError("original document lease inode changed")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if os.read(descriptor, 2049) != sources.canonical(expected):
            raise ValueError("original document lease content changed")
        named = os.stat(lease["file"], dir_fd=parent, follow_symlinks=False)
        if named.st_dev != facts.st_dev or named.st_ino != facts.st_ino:
            raise ValueError("original document lease name changed")
        return descriptor
    except BaseException:
        if descriptor >= 0: os.close(descriptor)
        raise
    finally: os.close(parent)


def _cipher_limit(size):
    return 4 * ((57 + 16 * (size // 16 + 1) + 2) // 3)


def _max_inventory(row):
    return {"source": {"temporary": ".g1-source.fernet." + "f"*32 + ".pending",
        "final": "g1-source.fernet", "size": _cipher_limit(SOURCE_LIMIT), "sha256": "f"*64},
        "payload": {"temporary": "." + row.artifact_id + "-" + "f"*64 + ".json." + "f"*32 + ".pending",
            "final": row.artifact_id + "-" + "f"*64 + ".json", "size": 1024, "sha256": "f"*64}}


@dataclass(frozen=True)
class _SourcePublication:
    artifact_id: str
    revision: int
    binding_digest: str
    metadata_json: str
    inventory_json: str
    strict_input_json: str
    ciphertext: bytes = field(repr=False)
    payload: bytes = field(repr=False)
    observed: _ObservedSource = field(repr=False)
    _seal: object = field(default=None, repr=False, compare=False)
    _issued_id: int = field(default=0, repr=False, compare=False)


def prepare_source_publication(row, value, observed):
    from src.vault.crypto import _get_fernet
    if (type(observed) is not _ObservedSource or observed._seal is not _OBSERVED_SEAL
        or observed._issued_id != id(observed)):
        raise BoardError("channel_document_observed_source_required", "Actual bounded original EOF and awaited closure are required", status_code=409)
    _checked_lease(row, value, observed.lease, physical=True)
    raw = observed.raw
    binding = OriginalDocumentBinding.model_validate(value["channel_ingest"]["binding"])
    if type(raw) is not bytes or len(raw) != binding.declared_size:
        raise BoardError("channel_document_size_mismatch", "Actual source size differs from its original advertised size", status_code=409)
    cipher = _get_fernet().encrypt(raw)
    inputs = {"schema_version": 1, "artifact_ref": "document-source:" + row.artifact_id,
        "format": binding.format, "source": {"size_bytes": len(raw), "sha256": sources.sha256(raw)}, "no_learning": True}
    payload = sources.canonical({"schema_version": 1, "capability_id": sources.SOURCE_CAPABILITY, "input": inputs})
    inventory = _max_inventory(row)
    for slot, data in (("source", cipher), ("payload", payload)):
        inventory[slot]["size"] = len(data); inventory[slot]["sha256"] = sources.sha256(data)
        nonce = uuid.uuid4().hex
        if slot == "source":
            inventory[slot]["temporary"] = ".g1-source.fernet." + nonce + ".pending"
        else:
            inventory[slot]["final"] = row.artifact_id + "-" + sources.sha256(payload) + ".json"
            inventory[slot]["temporary"] = "." + inventory[slot]["final"] + "." + nonce + ".pending"
    return _issued(_SourcePublication(row.artifact_id, row.revision, _pending_binding(value),
        row.document_metadata_json, sources.canonical(inventory).decode(), sources.canonical(inputs).decode(),
        cipher, payload, observed, _PUBLICATION_SEAL))


def _publication_current(row, value, packet):
    if (type(packet) is not _SourcePublication or packet._seal is not _PUBLICATION_SEAL
        or packet._issued_id != id(packet) or packet.artifact_id != row.artifact_id
        or packet.revision != row.revision or packet.metadata_json != row.document_metadata_json
        or packet.binding_digest != _pending_binding(value)):
        raise BoardError("channel_document_publication_changed", "Actual original private publication is required", status_code=409)
    _checked_lease(row, value, packet.observed.lease)
    if (packet.observed._seal is not _OBSERVED_SEAL or packet.observed._issued_id != id(packet.observed)
        or json.loads(packet.strict_input_json)["source"]["sha256"] != packet.observed.sha256):
        raise BoardError("channel_document_observed_source_required", "Actual original closed byte producer changed", status_code=409)


async def reserve_source_publication(db, owner, row, value, witness, packet):
    _publication_current(row, value, packet)
    await _begin_immediate(db)
    fresh, current = await sources.owned(db, owner, row.artifact_id, revision=row.revision, capability=sources.SOURCE_CAPABILITY)
    _source_matches_witness(fresh, current, witness)
    _publication_current(fresh, current, packet)
    await validate_original_document(db, witness)
    await sources.authority(db, owner, fresh, current, json.loads(witness.workspace_json), ingest=True)
    if current["channel_ingest"]["publication"] is not None:
        raise BoardError("channel_document_publication_changed", "Original source inventory is already reserved", status_code=409)
    current["channel_ingest"]["publication"] = json.loads(packet.inventory_json)
    current["channel_ingest"]["observed"] = json.loads(packet.strict_input_json)["source"]
    current["channel_ingest"]["contact"] = "closed"
    _persist(fresh, current); fresh.revision += 1
    await db.commit()
    from dataclasses import replace
    return fresh, current, _issued(replace(packet, revision=fresh.revision, metadata_json=fresh.document_metadata_json))


def _inventory_path(row, value, slot, inventory):
    if slot == "source":
        return sources.source_path(row, value, "source")
    from config.settings import settings
    from src.workspace import canonical_workspace_root
    return canonical_workspace_root(settings.workspace_dir) / INPUT_ARTIFACT_ROOT / inventory["payload"]["final"]


def _write_inventory(path, raw, receipt):
    if path.name != receipt["final"] or len(raw) != receipt["size"] or sources.sha256(raw) != receipt["sha256"]:
        raise ValueError("private inventory content changed")
    parent, leaf = _open_input_artifact_parent(path, create=True)
    descriptor = -1
    try:
        descriptor = os.open(receipt["temporary"], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        view = memoryview(raw)
        while view:
            count = os.write(descriptor, view)
            if count <= 0: raise OSError("private source write stalled")
            view = view[count:]
        os.fsync(descriptor); os.close(descriptor); descriptor = -1
        os.link(receipt["temporary"], leaf, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        os.unlink(receipt["temporary"], dir_fd=parent); os.fsync(parent)
        return _read_inventory(path, receipt)
    finally:
        if descriptor >= 0: os.close(descriptor)
        os.close(parent)


def _read_inventory(path, receipt):
    parent, leaf = _open_input_artifact_parent(path, create=False)
    descriptor = -1
    try:
        descriptor = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        facts = os.fstat(descriptor)
        if not _private_input_file_metadata(facts) or facts.st_size != receipt["size"]:
            raise ValueError("private source metadata changed")
        digest = hashlib.sha256(); remaining = facts.st_size
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk: raise ValueError("private source truncated")
            digest.update(chunk); remaining -= len(chunk)
        if digest.hexdigest() != receipt["sha256"]:
            raise ValueError("private source digest changed")
        named = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        if any(getattr(named, key) != getattr(facts, key) for key in ("st_dev", "st_ino", "st_uid", "st_mode", "st_size", "st_nlink")):
            raise ValueError("private source name changed")
        return {"device": facts.st_dev, "inode": facts.st_ino, "size": facts.st_size, "sha256": receipt["sha256"]}
    finally:
        if descriptor >= 0: os.close(descriptor)
        os.close(parent)


@dataclass(frozen=True)
class _SourceReadback:
    publication: _SourcePublication = field(repr=False)
    physical_json: str
    _seal: object = field(default=None, repr=False, compare=False)
    _issued_id: int = field(default=0, repr=False, compare=False)


def publish_source(row, value, packet):
    _publication_current(row, value, packet)
    inventory = json.loads(packet.inventory_json)
    if value["channel_ingest"]["publication"] != inventory:
        raise BoardError("channel_document_publication_changed", "Original pending inventory changed", status_code=409)
    physical = {}
    for slot, raw in (("source", packet.ciphertext), ("payload", packet.payload)):
        physical[slot] = _write_inventory(_inventory_path(row, value, slot, inventory), raw, inventory[slot])
    descriptor = json.loads(packet.strict_input_json)["source"]
    readback = sources.read_private(sources.source_path(row, value, "source"),
        {"cipher_size": len(packet.ciphertext), "cipher_sha256": sources.sha256(packet.ciphertext)}, maximum=descriptor["size_bytes"])
    if len(readback) != descriptor["size_bytes"] or sources.sha256(readback) != descriptor["sha256"]:
        raise BoardError("channel_document_readback_failed", "Original encrypted source readback failed", status_code=409)
    return _issued(_SourceReadback(packet, sources.canonical(physical).decode(), _READBACK_SEAL))


async def seal_channel_source(db, owner, row, value, witness, packet, readback):
    if (type(readback) is not _SourceReadback or readback._seal is not _READBACK_SEAL
        or readback._issued_id != id(readback) or readback.publication is not packet):
        raise BoardError("channel_document_readback_required", "The actual original physical readback is required", status_code=409)
    await _begin_immediate(db)
    fresh, current = await sources.owned(db, owner, row.artifact_id, revision=row.revision, capability=sources.SOURCE_CAPABILITY)
    _source_matches_witness(fresh, current, witness)
    _publication_current(fresh, current, packet)
    _pair, event = await validate_original_document(db, witness)
    await sources.authority(db, owner, fresh, current, json.loads(witness.workspace_json), ingest=True)
    if (fresh.metadata_digest is not None or current["phase"] != "uploading"
        or current["channel_ingest"]["contact"] != "closed" or not current.get("live_writer")
        or current["channel_ingest"]["publication"] != json.loads(packet.inventory_json)):
        raise BoardError("channel_document_first_seal_required", "Only the original pending source may first seal", status_code=409)
    current["input"] = json.loads(packet.strict_input_json)
    inventory = json.loads(packet.inventory_json)
    current["sources"]["source"] = {"cipher_size": inventory["source"]["size"], "cipher_sha256": inventory["source"]["sha256"]}
    current["channel_ingest"]["physical"] = json.loads(readback.physical_json)
    current["sources"]["source"].update({key: current["channel_ingest"]["physical"]["source"][key] for key in ("device", "inode")})
    current["phase"] = "sealed"; current["live_writer"] = None
    fresh.payload_sha256 = sources.sha256(packet.payload); fresh.size_bytes = len(packet.payload)
    fresh.typed_input_ref = "workspace-json:" + INPUT_ARTIFACT_ROOT + "/" + inventory["payload"]["final"]
    _persist(fresh, current); fresh.revision += 1
    fresh.metadata_digest = _metadata_digest(fresh)
    receipt = json.loads(event.receipt_json)
    receipt["channel_task_capture"]["document_source"] = {"artifact_id": fresh.artifact_id,
        "revision": fresh.revision, "source_digest": current["input"]["source"]["sha256"], "metadata_digest": fresh.metadata_digest}
    event.receipt_json = sources.canonical(receipt).decode()
    await db.commit()
    return fresh, current


async def acquire_original_document(adapter, owner, event_id):
    """One admitted original provider acquisition; started/Unknown never retries."""
    from src.db import engine
    from src.vault.repository import vault_repository
    from src.extensions.telegram_transport import _token_fingerprint
    # The document service's current profile is supplied by its owning lifecycle,
    # not constructed from caller facts or a probe-shaped mapping.
    service = getattr(adapter, "document_service", None)
    if service is None:
        raise BoardError("document_service_inactive", "Restore the managed document service before acquiring a selected document", status_code=503)
    descriptor = -1
    identifier = None
    try:
        async with engine.get_session() as db:
            witness = await resolve_original_document(db, owner, event_id)
            row, value = await reserve_channel_source(db, owner, witness)
            identifier = row.artifact_id
            if value["phase"] == "sealed":
                return sources.projection(row)
            row, value, lease = await acquire_channel_source(db, owner, row, value, witness,
                upload_profile=service._upload_profile)
            descriptor = lease.descriptor
            pair, _event = await validate_original_document(db, witness)
            token_ref, fingerprint = pair.token_secret_ref, pair.token_fingerprint
            await db.commit()
        token = await vault_repository.get(token_ref)
        if (type(token) is not str or not token or not token.isascii()
            or re.fullmatch(r"[A-Za-z0-9_:-]{1,4096}", token) is None or _token_fingerprint(token) != fingerprint):
            raise BoardError("channel_document_token_changed", "The original private Telegram token is unavailable", status_code=409)
        async def before_bytes():
            staged_root = dict(root_binding())
            if sources.canonical(staged_root).decode() != witness.workspace_json:
                raise BoardError("channel_document_workspace_changed", "Original workspace changed", status_code=409)
            async with engine.get_session() as db:
                await validate_original_document(db, witness)
                fresh, current = await sources.owned(db, owner, identifier, capability=sources.SOURCE_CAPABILITY)
                if current != value or fresh.revision != row.revision:
                    raise BoardError("channel_document_source_changed", "Original admitted source changed", status_code=409)
                await sources.authority(db, owner, fresh, current, staged_root, ingest=True)
        await before_bytes()
        data = bytearray(); digest = hashlib.sha256()
        remaining = (utc(datetime.fromisoformat(value["ingest_deadline"])) - now()).total_seconds()
        async with asyncio.timeout(max(0, remaining)):
            async with adapter.document_http.acquire(token, witness.binding.provider_file_id,
                timeout=max(.001, remaining), before_bytes=before_bytes) as stream:
                async for chunk in stream:
                    if (type(chunk) is not bytes or not 0 < len(chunk) <= 65536
                        or len(data) + len(chunk) > witness.binding.declared_size):
                        raise BoardError("channel_document_size_mismatch", "Actual document chunks exceed the original bound", status_code=409)
                    digest.update(chunk); data.extend(chunk)
                if len(data) != witness.binding.declared_size:
                    raise BoardError("channel_document_size_mismatch", "Actual document EOF differs from the advertised size", status_code=409)
            # Both EOF and the original HTTP context's awaited close precede any
            # encryption/publication. No plaintext file is ever created.
            raw = bytes(data)
            if sources.sha256(raw) != digest.hexdigest():
                raise BoardError("channel_document_digest_mismatch", "Actual streamed digest changed", status_code=409)
            observed = _issued(_ObservedSource(lease, raw, digest.hexdigest(), _OBSERVED_SEAL))
            packet = prepare_source_publication(row, value, observed)
            async with engine.get_session() as db:
                row, value, packet = await reserve_source_publication(db, owner, row, value, witness, packet)
            readback = publish_source(row, value, packet)
            async with engine.get_session() as db:
                row, value = await seal_channel_source(db, owner, row, value, witness, packet, readback)
        return sources.projection(row)
    except BaseException:
        if identifier is not None and descriptor >= 0:
            from src.work_board.documents import shield_positive_cleanup
            async def retain_unknown():
                async with engine.get_session() as db:
                    await _begin_immediate(db)
                    fresh, current = await sources.owned(db, owner, identifier, capability=sources.SOURCE_CAPABILITY)
                    if current.get("live_writer") and current["phase"] != "sealed":
                        current["channel_ingest"]["contact"] = "unknown"
                        current["phase"] = "cleanup_required"
                        current["reason"] = "channel_document_unknown_cleanup_required"
                        _persist(fresh, current); fresh.revision += 1
                        await db.commit()
            await shield_positive_cleanup(retain_unknown())
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


async def reconcile_channel_source(db, owner, identifier, revision):
    row, value = await sources.owned(db, owner, identifier, revision=revision, capability=sources.SOURCE_CAPABILITY)
    if value["root"] != dict(root_binding()) or not value.get("channel_ingest") or not value.get("live_writer"):
        raise BoardError("document_original_upload_required", "Use the original retained document writer", status_code=409)
    from src.work_board.documents import current_source_root
    await current_source_root(db, owner, None)
    try:
        descriptor = original_channel_lease(row, value)
    except (OSError, ValueError, KeyError, TypeError):
        raise BoardError("document_upload_quiescence_unknown", "Exact original upload closure is unproved; retain capacity", status_code=409) from None
    try:
        await _begin_immediate(db)
        fresh, current = await sources.owned(db, owner, identifier, revision=revision, capability=sources.SOURCE_CAPABILITY)
        if current != value:
            raise BoardError("document_upload_fence_changed", "Original document cleanup binding changed", status_code=409)
        current["live_writer"] = None; current["phase"] = "cleanup_required"
        current["channel_ingest"]["cleanup"] = {"positive_exclusive_lock": True,
            "binding_digest": sources.sha256(sources.canonical(current["upload_binding"]))}
        _persist(fresh, current); fresh.revision += 1
        await db.commit()
        return sources.projection(fresh)
    finally: os.close(descriptor)


def cleanup_channel_files(row, value):
    """Only source-issued exact inventory; partial/foreign/both-link stays charged."""
    inventory = value["channel_ingest"]["publication"] or {}
    source_parent, _leaf = _open_input_artifact_parent(sources.source_path(row, value, "source"), create=False)
    try:
        expected = {"g1-source.upload-lock"}
        if inventory:
            expected.update(inventory["source"][key] for key in ("final", "temporary"))
        if set(os.listdir(source_parent)) - expected:
            raise OSError("foreign source inventory retained")
    finally: os.close(source_parent)
    descriptor = original_channel_lease(row, value) if value.get("upload_binding") else -1
    try:
        for slot, receipt in inventory.items():
            path = _inventory_path(row, value, slot, inventory)
            parent, _leaf = _open_input_artifact_parent(path, create=False)
            try:
                present = [name for name in (receipt["final"], receipt["temporary"])
                    if name in os.listdir(parent)]
                if len(present) != 1:
                    raise OSError("original publication absence or both links unproved")
                name = present[0]
                facts = _read_inventory(path.with_name(name), receipt)
                original_physical = value["channel_ingest"].get("physical", {}).get(slot)
                if original_physical is not None and facts != original_physical:
                    raise OSError("original publication physical identity changed")
                current = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (facts["device"], facts["inode"]):
                    raise OSError("original publication inode changed")
                os.unlink(name, dir_fd=parent); os.fsync(parent)
                if any(name in os.listdir(parent) for name in (receipt["final"], receipt["temporary"])):
                    raise OSError("original publication absence unproved")
            finally: os.close(parent)
        parent, _leaf = _open_input_artifact_parent(sources.source_path(row, value, "source"), create=False)
        try:
            if descriptor >= 0:
                facts = os.fstat(descriptor)
                named = os.stat("g1-source.upload-lock", dir_fd=parent, follow_symlinks=False)
                if (facts.st_dev, facts.st_ino) != (named.st_dev, named.st_ino):
                    raise OSError("original lease replaced")
                os.unlink("g1-source.upload-lock", dir_fd=parent); os.fsync(parent)
            if os.listdir(parent): raise OSError("original source cleanup incomplete")
        finally: os.close(parent)
    finally:
        if descriptor >= 0: os.close(descriptor)


async def delete_channel_source(db, owner, identifier, revision):
    from src.work_board.documents import current_source_root
    staged_root = dict(root_binding())
    await current_source_root(db, owner, None)
    await _begin_immediate(db)
    row, value = await sources.owned(db, owner, identifier, revision=revision, capability=sources.SOURCE_CAPABILITY)
    if value["root"] != staged_root or value["channel_ingest"].get("task_id") or row.bound_task_id:
        raise BoardError("document_bound_pair_retained", "Original Task owns the document association", status_code=409)
    if value.get("live_writer"):
        raise BoardError("document_upload_quiescence_unknown", "Reconcile the exact original writer before deleting", status_code=409)
    if value["phase"] == "deleted":
        await db.commit(); return sources.projection(row)
    value["phase"] = "cleanup_tombstone"
    _persist(row, value); row.metadata_digest = None; row.revision += 1
    tombstone_revision = row.revision
    await db.commit()
    try:
        cleanup_channel_files(row, value)
    except (OSError, ValueError, KeyError):
        raise BoardError("document_pair_cleanup_required", "Exact original private files remain unverified; retain quota", status_code=409) from None
    await _begin_immediate(db)
    fresh, current = await sources.owned(db, owner, identifier, revision=tombstone_revision, capability=sources.SOURCE_CAPABILITY)
    if current != value:
        raise BoardError("document_pair_revision_conflict", "Original cleanup tombstone changed", status_code=409)
    current["phase"] = "deleted"; current["sources"] = {}
    _persist(fresh, current); fresh.state = "deleted"; fresh.document_reserved_bytes = 0; fresh.revision += 1
    await db.commit()
    return sources.projection(fresh)


async def check_original_document_sealed(db, owner, event, reservation, *, expected_task_id=None):
    """Pure SQL reciprocal provenance, never a parser/read or renewal grant."""
    from src.db.models import WorkBoardInputArtifact, WorkBoardTask
    from src.work_board.channel_capture import _origin_record
    try:
        stored = json.loads(event.receipt_json)["channel_task_capture"]
        acquisition = OriginalDocumentBinding.model_validate(stored["document_acquisition"])
        ref = stored["document_source"]
        binding_digest = sources.sha256(sources.canonical(acquisition.model_dump(mode="json")))
        if (acquisition.reservation_digest != sources.sha256(sources.canonical(reservation.model_dump(mode="json")))
            or acquisition.event_id != event.idempotency_key or event.owner_principal_id != owner.principal_id
            or event.operator_session_id != owner.session_id or event.status != "accepted"
            or event.canonical_message_id != acquisition.canonical_message_id
            or event.request_digest != acquisition.provider_request_digest or event.content_digest != acquisition.message_digest):
            raise ValueError()
        row = await db.get(WorkBoardInputArtifact, ref["artifact_id"], populate_existing=True)
        value = sources.metadata(row)
        if (row.capability_id != sources.SOURCE_CAPABILITY or row.owner_principal_id != owner.principal_id
            or row.owner_session_id != owner.session_id or row.goal_id != reservation.task_input.goal_ref
            or row.goal_revision != reservation.goal_revision or value["phase"] != "sealed"
            or row.metadata_digest is None or row.metadata_digest != _metadata_digest(row)
            or row.revision != ref["revision"] or row.metadata_digest != ref["metadata_digest"]
            or value["input"]["source"]["sha256"] != ref["source_digest"]
            or value["channel_ingest"]["binding_digest"] != binding_digest
            or value["channel_ingest"]["binding"] != acquisition.model_dump(mode="json")):
            raise ValueError()
        task_id = value["channel_ingest"]["task_id"]
        if task_id is None:
            if expected_task_id is not None or ref.get("task_id") is not None or stored.get("task_id") is not None:
                raise ValueError()
        else:
            if expected_task_id != task_id or ref.get("task_id") != task_id or stored.get("task_id") != task_id:
                raise ValueError()
            task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id).execution_options(populate_existing=True))
            if (task is None or task.owner_principal_id != owner.principal_id or task.owner_session_id != owner.session_id
                or task.goal_id != row.goal_id or task.goal_revision != row.goal_revision
                or task.idempotency_key != reservation.idempotency_key
                or not task.channel_capture_origin_json
                or sources.sha256(task.channel_capture_origin_json.encode()) != value["channel_ingest"]["task_origin_digest"]
                or json.loads(task.channel_capture_origin_json)["origin"] != _origin_record(task, reservation).model_dump(mode="json")):
                raise ValueError()
    except (ValueError, KeyError, AttributeError, TypeError):
        raise BoardError("channel_document_source_unsealed", "Exact original sealed document and reciprocal Task provenance are required", status_code=409) from None
    return row, value


async def check_channel_source_association(db, owner, row, value):
    from src.db.models import TelegramInboundUpdate
    from src.work_board.channel_capture import ChannelCaptureReservationV1
    event = await db.scalar(select(TelegramInboundUpdate).where(
        TelegramInboundUpdate.idempotency_key == value["channel_ingest"]["binding"]["event_id"])
        .execution_options(populate_existing=True))
    try:
        stored = json.loads(event.receipt_json)["channel_task_capture"]
        reservation = ChannelCaptureReservationV1.model_validate(stored["reservation"])
    except (ValueError, KeyError, AttributeError, TypeError):
        raise BoardError("channel_document_event_changed", "Original reciprocal source event is unavailable", status_code=409) from None
    checked, _current = await check_original_document_sealed(db, owner, event, reservation,
        expected_task_id=value["channel_ingest"]["task_id"])
    if checked.artifact_id != row.artifact_id:
        raise BoardError("channel_document_source_changed", "Original source/event association changed", status_code=409)


@dataclass(frozen=True)
class _DocumentLinkWitness:
    acquisition: TelegramDocumentAcquisitionWitness = field(repr=False)
    artifact_id: str
    revision: int
    metadata_digest: str
    payload_digest: str
    source_digest: str
    inventory_json: str
    physical_json: str
    original_task_id: str | None
    _seal: object = field(default=None, repr=False, compare=False)
    _issued_id: int = field(default=0, repr=False, compare=False)


_LINK_SEAL = object()


async def stage_original_document_link(db, owner, reservation):
    witness = await resolve_original_document(db, owner, reservation.source_id)
    _pair, event = await validate_original_document(db, witness)
    stored = json.loads(event.receipt_json)["channel_task_capture"]
    row, value = await check_original_document_sealed(db, owner, event, reservation,
        expected_task_id=stored.get("task_id"))
    inventory = value["channel_ingest"]["publication"]
    physical = {slot: _read_inventory(_inventory_path(row, value, slot, inventory), receipt)
        for slot, receipt in inventory.items()}
    raw = sources.read_private(sources.source_path(row, value, "source"), value["sources"]["source"], maximum=SOURCE_LIMIT)
    descriptor = value["input"]["source"]
    if len(raw) != descriptor["size_bytes"] or sources.sha256(raw) != descriptor["sha256"]:
        raise BoardError("channel_document_readback_failed", "Original source physical readback changed", status_code=409)
    return _issued(_DocumentLinkWitness(witness, row.artifact_id, row.revision, row.metadata_digest,
        row.payload_sha256, descriptor["sha256"], sources.canonical(inventory).decode(),
        sources.canonical(physical).decode(), value["channel_ingest"]["task_id"], _LINK_SEAL))


async def check_original_document_link(db, owner, reservation, link):
    if (type(link) is not _DocumentLinkWitness or link._seal is not _LINK_SEAL or link._issued_id != id(link)):
        raise BoardError("channel_document_readback_required", "The actual original private link readback is required", status_code=409)
    _pair, event = await validate_original_document(db, link.acquisition)
    row, value = await check_original_document_sealed(db, owner, event, reservation,
        expected_task_id=link.original_task_id)
    if (row.artifact_id != link.artifact_id or row.revision != link.revision
        or row.metadata_digest != link.metadata_digest or row.payload_sha256 != link.payload_digest
        or value["input"]["source"]["sha256"] != link.source_digest
        or value["channel_ingest"]["publication"] != json.loads(link.inventory_json)
        or value["channel_ingest"]["physical"] != json.loads(link.physical_json)):
        raise BoardError("channel_document_source_changed", "Original physical source/link revision changed", status_code=409)
    await sources.authority(db, owner, row, value, json.loads(link.acquisition.workspace_json), ingest=True)
    return row, value, event


async def bind_original_document_task(db, owner, task, reservation, link):
    """Inside the existing Task/origin writer; no filesystem or Vault work."""
    from sqlalchemy import update
    from src.db.models import WorkBoardInputArtifact, TelegramInboundUpdate
    row, value, event = await check_original_document_link(db, owner, reservation, link)
    if (task.owner_principal_id != owner.principal_id or task.owner_session_id != owner.session_id
        or task.goal_id != reservation.task_input.goal_ref or task.goal_revision != reservation.goal_revision
        or task.idempotency_key != reservation.idempotency_key or task.origin_session_id != reservation.conversation_session_id
        or not task.channel_capture_origin_json):
        raise BoardError("channel_document_task_changed", "The exact original captured Task is required", status_code=409)
    if link.original_task_id is not None:
        if link.original_task_id != task.task_id:
            raise BoardError("channel_document_task_changed", "Document already belongs to its original Task", status_code=409)
        return
    original_metadata = row.document_metadata_json
    original_revision, original_digest = row.revision, row.metadata_digest
    original_receipt = event.receipt_json
    value["channel_ingest"]["task_id"] = task.task_id
    value["channel_ingest"]["task_origin_digest"] = sources.sha256(task.channel_capture_origin_json.encode())
    _persist(row, value); row.revision += 1
    row.metadata_digest = _metadata_digest(row)
    # Flush only through exact existing-row CAS. Task/origin/event publication
    # remains owned by the caller's one canonical transaction.
    from sqlalchemy.orm.attributes import set_committed_value
    next_metadata, next_revision, next_digest = row.document_metadata_json, row.revision, row.metadata_digest
    set_committed_value(row, "document_metadata_json", original_metadata)
    set_committed_value(row, "revision", original_revision)
    set_committed_value(row, "metadata_digest", original_digest)
    changed = await db.execute(update(WorkBoardInputArtifact).where(
        WorkBoardInputArtifact.artifact_id == row.artifact_id,
        WorkBoardInputArtifact.revision == original_revision,
        WorkBoardInputArtifact.metadata_digest == original_digest,
        WorkBoardInputArtifact.document_metadata_json == original_metadata).values(
            document_metadata_json=next_metadata, revision=next_revision, metadata_digest=next_digest)
        .execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise BoardError("channel_document_source_changed", "Original source link CAS failed", status_code=409)
    set_committed_value(row, "document_metadata_json", next_metadata)
    set_committed_value(row, "revision", next_revision)
    set_committed_value(row, "metadata_digest", next_digest)
    receipt = json.loads(original_receipt)
    receipt["channel_task_capture"]["task_id"] = task.task_id
    receipt["channel_task_capture"]["document_source"] = _source_receipt(row, value)
    next_receipt = sources.canonical(receipt).decode()
    changed = await db.execute(update(TelegramInboundUpdate).where(
        TelegramInboundUpdate.id == event.id, TelegramInboundUpdate.receipt_json == original_receipt).values(receipt_json=next_receipt)
        .execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise BoardError("channel_document_event_changed", "Original source/event link CAS failed", status_code=409)
    set_committed_value(event, "receipt_json", next_receipt)


def _source_receipt(row, value):
    result = {"artifact_id": row.artifact_id, "revision": row.revision,
        "source_digest": value["input"]["source"]["sha256"], "metadata_digest": row.metadata_digest}
    if value["channel_ingest"]["task_id"] is not None:
        result["task_id"] = value["channel_ingest"]["task_id"]
    return result


async def sync_channel_source_receipt(db, row, value, *, previous_revision, previous_metadata_digest):
    """Legitimate source-owner revision updates carry their reciprocal receipt.

    Replay never calls this helper; it cannot repair a previously drifted source.
    """
    if not value.get("channel_ingest"):
        return
    from src.db.models import TelegramInboundUpdate
    event = await db.scalar(select(TelegramInboundUpdate).where(
        TelegramInboundUpdate.idempotency_key == value["channel_ingest"]["binding"]["event_id"])
        .execution_options(populate_existing=True))
    if event is None:
        raise BoardError("channel_document_event_changed", "Original source event is missing", status_code=409)
    receipt = json.loads(event.receipt_json)
    original = receipt["channel_task_capture"].get("document_source")
    if (original is None or original["artifact_id"] != row.artifact_id
        or original["source_digest"] != value["input"]["source"]["sha256"]
        or original["revision"] != previous_revision or original["metadata_digest"] != previous_metadata_digest
        or original.get("task_id") != value["channel_ingest"]["task_id"]):
        raise BoardError("channel_document_event_changed", "Original reciprocal source receipt changed", status_code=409)
    receipt["channel_task_capture"]["document_source"] = _source_receipt(row, value)
    event.receipt_json = sources.canonical(receipt).decode()
