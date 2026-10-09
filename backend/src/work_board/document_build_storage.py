"""Owner-private document builds on the existing charged input artifact row."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
import hashlib
import hmac
import json
import os
import re
import uuid
from typing import Literal

from pydantic import Field, field_validator
from sqlalchemy import select

from src.db.models import WorkBoardInputArtifact, WorkBoardTask, WorkBoardAttempt, WorkflowRunState
from src.work_board import document_pairs as sources
from src.work_board.contracts import (ClosedTaskModel, DocumentTaskBinding, WorkBoardOwner,
    DocumentBuildReview as BuildReview, DocumentBuildReviewBinding as BuildReviewBinding)
from src.work_board.input_artifacts import _begin_immediate, _metadata_digest, _private_input_file_metadata
from src.work_board.pipelines import now, utc, root_binding
from src.work_board.repository import BoardError
from src.work_board.document_build_contracts import DocumentBuildSpec

CAPABILITY = sources.BUILD_CAPABILITY
CHARGE = sources.BUILD_CHARGE
SPEC_LIMIT = 65536
SELECTION_LIMIT = 16384
OUTPUT_LIMIT = 4 * 1024 * 1024
MANIFEST_LIMIT = 16384
SLOTS = {"spec": SPEC_LIMIT, "selection": SELECTION_LIMIT, "editable": OUTPUT_LIMIT,
    "pdf": OUTPUT_LIMIT, "output-manifest": MANIFEST_LIMIT}
_OUTPUT_SEAL = object()
_PENDING_SEAL = object()
PROFILE_DIGEST = sources.sha256(sources.canonical({"profile": "document-build-renderer.v1",
    "address_space": 512*1024*1024, "cpu_seconds": 10, "fd_limit": 64,
    "wall_seconds": 30, "reap_seconds": 5, "spec_bytes": SPEC_LIMIT,
    "selection_bytes": SELECTION_LIMIT, "editable_bytes": OUTPUT_LIMIT, "pdf_bytes": OUTPUT_LIMIT}))


class BuildCreate(ClosedTaskModel):
    goal_id: str = Field(min_length=1, max_length=128)
    goal_revision: int = Field(ge=1)
    spec: DocumentBuildSpec
    source: DocumentTaskBinding | None = None
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")


class BuildSourceSelection(ClosedTaskModel):
    expected_revision: int = Field(ge=1)
    citation_refs: list[str] = Field(min_length=1, max_length=16)
    acknowledge_local_use: Literal[True]

    _exact_refs = field_validator("citation_refs")(DocumentTaskBinding.exact_unique_refs.__func__)
    _exact_ack = field_validator("acknowledge_local_use", mode="before")(DocumentTaskBinding.literal_local_ack.__func__)


async def citations(db, owner, operator, identifier, *, limit=100, offset=0):
    """Inspect adopted bounded evidence; never extract again to discover refs."""
    from src.work_board.documents import DocumentEvidence, OUTPUT_LIMIT as SOURCE_OUTPUT_LIMIT, current_source_root
    from src.work_board.document_preparation import resolve
    row, value = await sources.owned(db, owner, identifier, capability=sources.SOURCE_CAPABILITY)
    await sources.authority(db, owner, row, value, dict(root_binding()))
    await current_source_root(db, owner, operator)
    if not value.get("evidence") or value.get("live_writer") or value.get("phase") != "sealed":
        raise BoardError("document_build_source_unread", "Read and positively close the selected source first", status_code=409)
    raw = sources.read_private(sources.source_path(row, value, "evidence"), value["evidence"], maximum=SOURCE_OUTPUT_LIMIT)
    evidence = DocumentEvidence.model_validate_json(raw)
    leaves = [leaf for section in evidence.sections for leaf in (section.table_cells or [section])]
    if not leaves:
        raise BoardError("document_build_source_empty", "The adopted source has no selectable literal leaves", status_code=409)
    binding = DocumentTaskBinding(artifact_ref="document-source:" + identifier, source_revision=row.revision,
        metadata_digest=row.metadata_digest, citation_refs=[leaves[0].source_ref],
        selection_digest=_digest([leaves[0].source_ref]), acknowledge_local_use=True)
    await resolve(db, owner, binding, goal_id=row.goal_id, operator=operator)
    page = leaves[offset:offset+limit]
    return {"artifact_ref": binding.artifact_ref, "source_revision": row.revision,
        "goal_id": row.goal_id, "goal_revision": row.goal_revision,
        "citations": [leaf.model_dump(mode="json", exclude={"table_cells"}) for leaf in page],
        "next_offset": offset+limit if len(leaves) > offset+limit else None,
        "no_learning": True, "provider_contacts": 0}


async def select_source(db, owner, operator, identifier, request):
    from src.work_board.document_preparation import resolve
    row, _value = await sources.owned(db, owner, identifier,
        revision=request.expected_revision, capability=sources.SOURCE_CAPABILITY)
    binding = DocumentTaskBinding(artifact_ref="document-source:" + identifier,
        source_revision=row.revision, metadata_digest=row.metadata_digest,
        citation_refs=request.citation_refs, selection_digest=_digest(request.citation_refs),
        acknowledge_local_use=request.acknowledge_local_use)
    _row, selection = await resolve(db, owner, binding, goal_id=row.goal_id, operator=operator)
    return {"source": binding.model_dump(mode="json"), "selection": selection, "goal_id": row.goal_id,
        "goal_revision": row.goal_revision, "no_learning": True, "provider_contacts": 0}


class BuildPrepare(ClosedTaskModel):
    expected_revision: int = Field(ge=1)
    review: BuildReview
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")


class BuildRetire(ClosedTaskModel):
    expected_revision: int = Field(ge=1)
    expected_task_revision: int | None = Field(default=None, ge=1)
    attempt_id: str | None = Field(default=None, min_length=1, max_length=128)
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")


def _digest(value):
    return sources.sha256(value if isinstance(value, bytes) else sources.canonical(value))


def metadata(row):
    value = sources.metadata(row)
    if (row.capability_id != CAPABILITY or row.capability_version != "1"
        or value.get("generation") != 1 or value.get("phase") not in
        {"reserved", "staged", "bound", "rendering", "reaping", "unknown", "completed", "degraded", "cleanup_tombstone", "deleted"}
        or type(value.get("sources")) is not dict or set(value["sources"]) - set(SLOTS)
        or not re.fullmatch(r"[a-f0-9]{64}", str(value.get("spec_digest", "")))
        or not re.fullmatch(r"[a-f0-9]{64}", str(value.get("selection_digest", "")))
        or type(value.get("spec_revision")) is not int or value["spec_revision"] < 1
        or row.document_reserved_bytes not in {CHARGE, 0}
        or (row.document_reserved_bytes == 0 and value["phase"] != "deleted")):
        raise BoardError("document_build_metadata_invalid", "Inspect the original private build", status_code=409)
    return value


def persist(row, value):
    raw = sources.canonical(value).decode()
    if len(raw.encode()) > 8192:
        raise BoardError("document_build_metadata_full", "The bounded private build metadata is full", status_code=409)
    row.document_metadata_json = raw
    row.revision += 1
    row.metadata_digest = _metadata_digest(row)


async def owned(db, owner, identifier, *, revision=None):
    row, _value = await sources.owned(db, owner, identifier, revision=revision, capability=CAPABILITY)
    return row, metadata(row)


async def cleanup_authority(db, owner, row, value, operator):
    """The actual authenticated original owner may reduce retained state only."""
    from src.auth.service import AuthenticatedOperator, AuthFailure
    from src.work_board.documents import current_source_root
    if (not isinstance(operator, AuthenticatedOperator) or not operator._token_hash
        or operator.session_id != owner.session_id or operator.principal.principal_id != owner.principal_id
        or row.owner_session_id != owner.session_id or row.owner_principal_id != owner.principal_id
        or value["root"] != dict(root_binding()) or row.metadata_digest != _metadata_digest(row)):
        raise BoardError("document_build_cleanup_owner_required", "Sign in under the original build owner and workspace", status_code=409)
    try:
        root = await current_source_root(db, owner, operator)
    except AuthFailure:
        raise BoardError("document_build_cleanup_owner_required", "Sign in under the current original build Root", status_code=409) from None
    if (root.replaced_by_id is not None or value["root_authority"] != _digest({"id": root.id,
        "principal": root.principal_id, "token_hash": root.token_hash,
        "absolute": utc(root.absolute_expires_at).isoformat()})):
        raise BoardError("document_build_cleanup_owner_required", "The original build Root is no longer current", status_code=409)
    return root


async def publish_supervision(db, row, value, producer):
    """Store only the retained native invocation's privately sealed closure."""
    from src.work_board.document_build_native import validate_supervision_publication
    receipt = await validate_supervision_publication(db, row, value, producer)
    if value.get("supervision") is not None:
        if value["supervision"] != receipt:
            raise BoardError("document_build_supervision_changed", "The original supervisor closure changed", status_code=409)
        return
    value["supervision"] = receipt
    persist(row, value)


def _cipher_limit(slot):
    return 4 * ((57 + 16 * (SLOTS[slot] // 16 + 1) + 2) // 3)


def max_pending_inventory():
    """Exact maximum future native pending inventory for preclaim headroom."""
    return {slot: {"generation": 1, "file": f".g1-{slot}.fernet.{'f'*32}.pending",
        "nonce": "f"*32, "cipher_sha256": "f"*64, "cipher_size": _cipher_limit(slot),
        "owner_binding": "f"*64, "mac": "f"*64} for slot in ("editable", "pdf", "output-manifest")}


def _pending_binding(row, value):
    return _digest({"build_id": row.artifact_id, "principal": row.owner_principal_id,
        "session": row.owner_session_id, "root": value["root"], "root_authority": value["root_authority"],
        "generation": 1, "spec_digest": value["spec_digest"], "selection_digest": value["selection_digest"],
        "task_id": row.bound_task_id, "capacity": value.get("renderer_binding")})


def _pending_mac(receipt):
    from src.memory.repository import _effect_mac_key
    from src.extensions.capability_execution import CapabilityJournalError
    try:
        key = _effect_mac_key()
    except CapabilityJournalError:
        raise BoardError("document_build_pending_key_unavailable", "Restore the original private publication key", status_code=409) from None
    return hmac.new(key, b"seraph-document-build-pending-v1\0" + sources.canonical(receipt), hashlib.sha256).hexdigest()


def _validate_pending(row, value, slot, receipt):
    if (slot not in SLOTS or type(receipt) is not dict
        or set(receipt) != {"generation", "file", "nonce", "cipher_sha256", "cipher_size", "owner_binding", "mac"}
        or receipt["generation"] != 1 or not re.fullmatch(r"[a-f0-9]{32}", str(receipt["nonce"]))
        or receipt["file"] != f".g1-{slot}.fernet.{receipt['nonce']}.pending"
        or type(receipt["cipher_size"]) is not int or not 1 <= receipt["cipher_size"] <= _cipher_limit(slot)
        or not re.fullmatch(r"[a-f0-9]{64}", str(receipt["cipher_sha256"]))
        or receipt["owner_binding"] != _pending_binding(row, value)
        or not re.fullmatch(r"[a-f0-9]{64}", str(receipt["mac"]))
        or not hmac.compare_digest(receipt["mac"], _pending_mac({key: item for key, item in receipt.items() if key != "mac"}))):
        raise BoardError("document_build_pending_changed", "The original private pending publication changed", status_code=409)
    return receipt


@dataclass(frozen=True)
class PrivateBuildPublication:
    artifact_id: str
    revision: int
    metadata_digest: str
    pending_json: str
    ciphertexts: tuple[tuple[str, bytes], ...] = field(repr=False)
    _seal: object = field(default=None, repr=False, compare=False)
    _issued_id: int = field(default=0, repr=False, compare=False)


def _issue_private_packet(packet):
    object.__setattr__(packet, "_issued_id", id(packet))
    return packet


def _prepare_private_publication(row, value, contents):
    from src.vault.crypto import _get_fernet
    pending, ciphertexts = {}, []
    for slot, raw in contents:
        if slot not in SLOTS or slot in pending or not isinstance(raw, bytes) or not 0 < len(raw) <= SLOTS[slot]:
            raise BoardError("document_build_publication_bound", "Use the fixed bounded original build slots", status_code=409)
        nonce = uuid.uuid4().hex
        cipher = _get_fernet().encrypt(raw)
        receipt = {"generation": 1, "file": f".g1-{slot}.fernet.{nonce}.pending", "nonce": nonce,
            "cipher_sha256": _digest(cipher), "cipher_size": len(cipher), "owner_binding": _pending_binding(row, value)}
        pending[slot] = {**receipt, "mac": _pending_mac(receipt)}
        ciphertexts.append((slot, cipher))
    return _issue_private_packet(PrivateBuildPublication(row.artifact_id, row.revision, row.metadata_digest,
        sources.canonical(pending).decode(), tuple(ciphertexts), _PENDING_SEAL))


def _publication_current(row, value, staged):
    if (type(staged) is not PrivateBuildPublication or staged._seal is not _PENDING_SEAL
        or staged._issued_id != id(staged)
        or staged.artifact_id != row.artifact_id or staged.revision != row.revision
        or staged.metadata_digest != row.metadata_digest or row.metadata_digest != _metadata_digest(row)):
        raise BoardError("document_build_publication_changed", "The exact original private publication changed", status_code=409)
    pending = json.loads(staged.pending_json)
    if set(pending) != {slot for slot, _cipher in staged.ciphertexts}:
        raise BoardError("document_build_publication_changed", "The exact fixed slot inventory changed", status_code=409)
    for slot, receipt in pending.items():
        _validate_pending(row, value, slot, receipt)
    return pending


def _reserve_private_publication(row, value, staged):
    pending = _publication_current(row, value, staged)
    if value.get("pending") or set(pending) & set(value["sources"]):
        raise BoardError("document_build_publication_pending", "Inspect the retained original publication first", status_code=409)
    value["pending"] = pending
    persist(row, value)
    return _issue_private_packet(replace(staged, revision=row.revision, metadata_digest=row.metadata_digest))


def _publish_private_publication(row, value, staged):
    pending = _publication_current(row, value, staged)
    if value.get("pending") != pending:
        raise BoardError("document_build_publication_changed", "The original pending reservation changed", status_code=409)
    receipts = {}
    for slot, cipher in staged.ciphertexts:
        receipt = pending[slot]
        if len(cipher) != receipt["cipher_size"] or _digest(cipher) != receipt["cipher_sha256"]:
            raise BoardError("document_build_publication_changed", "The source-owned ciphertext changed", status_code=409)
        parent, leaf = sources._open_input_artifact_parent(sources.source_path(row, value, slot), create=True)
        descriptor = -1
        try:
            descriptor = os.open(receipt["file"], os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600, dir_fd=parent)
            view = memoryview(cipher)
            while view:
                count = os.write(descriptor, view)
                if count <= 0: raise OSError("private pending write stalled")
                view = view[count:]
            os.fsync(descriptor); os.close(descriptor); descriptor = -1
            os.link(receipt["file"], leaf, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            os.unlink(receipt["file"], dir_fd=parent); os.fsync(parent)
        finally:
            if descriptor >= 0: os.close(descriptor)
            os.close(parent)
        receipts[slot] = {"cipher_size": receipt["cipher_size"], "cipher_sha256": receipt["cipher_sha256"]}
        sources.read_private(sources.source_path(row, value, slot), receipts[slot], maximum=SLOTS[slot])
    return receipts


def projection(row, value=None):
    value = metadata(row) if value is None else value
    return {"build_id": row.artifact_id, "build_ref": "document-build:" + row.artifact_id,
        "revision": row.revision, "state": value["phase"], "goal_id": row.goal_id,
        "goal_revision": row.goal_revision, "spec_digest": value["spec_digest"],
        "selection_digest": value["selection_digest"], "task_id": row.bound_task_id,
        "original_deadline": value["original_deadline"], "reason_code": value.get("reason"),
        "quota_reserved_bytes": row.document_reserved_bytes, "no_learning": True, "provider_contacts": 0}


def build_binding(row, value):
    return {"build_ref": "document-build:" + row.artifact_id, "build_revision": value["spec_revision"],
        "spec_digest": value["spec_digest"], "selection_digest": value["selection_digest"],
        "source_binding": value.get("source_binding"), "original_deadline": value["original_deadline"]}


async def authority(db, owner, row, value, *, operator=None, metadata_only=False):
    from src.work_board.documents import current_source_root
    if value["phase"] in {"reserved", "cleanup_tombstone", "deleted", "unknown"}:
        raise BoardError("document_build_unavailable", "Inspect the original build and cleanup state", status_code=409)
    if row.metadata_digest != _metadata_digest(row):
        raise BoardError("document_build_metadata_changed", "The original build metadata changed", status_code=409)
    await sources.authority(db, owner, row, value, dict(root_binding()))
    root = await current_source_root(db, owner, operator)
    if (root.replaced_by_id is not None or not root.token_hash
        or value["root_authority"] != _digest({"id": root.id, "principal": root.principal_id,
            "token_hash": root.token_hash, "absolute": utc(root.absolute_expires_at).isoformat()})
        or now() >= utc(datetime.fromisoformat(value["original_deadline"]))):
        raise BoardError("document_build_root_changed", "The original authenticated build authority expired or changed", status_code=409)
    source = value.get("source_binding")
    if source is not None:
        from src.work_board.document_preparation import resolve
        await resolve(db, owner, DocumentTaskBinding.model_validate(source), goal_id=row.goal_id,
            operator=operator, metadata_only=metadata_only)
    return root


def spec_read(row, value):
    from src.work_board.document_build_contracts import DocumentBuildSpec
    raw = sources.read_private(sources.source_path(row, value, "spec"), value["sources"]["spec"], maximum=SPEC_LIMIT)
    if _digest(raw) != value["spec_digest"]:
        raise BoardError("document_build_spec_changed", "The immutable private specification changed", status_code=409)
    return DocumentBuildSpec.model_validate_json(raw)


def selection_read(row, value):
    if "selection" not in value["sources"]:
        return []
    raw = sources.read_private(sources.source_path(row, value, "selection"), value["sources"]["selection"], maximum=SELECTION_LIMIT)
    selected = json.loads(raw)
    if _digest(selected) != value["selection_digest"]:
        raise BoardError("document_build_selection_changed", "The original private selection changed", status_code=409)
    return selected


async def create(db, owner, operator, request):
    from src.work_board.document_build_contracts import DocumentBuildSpec
    from src.work_board.documents import current_source_root
    spec = DocumentBuildSpec.model_validate(request.spec)
    raw = sources.canonical(spec.model_dump(mode="json"))
    if len(raw) > SPEC_LIMIT:
        raise BoardError("document_build_spec_too_large", "The complete specification exceeds 64 KiB", status_code=422)
    root = await current_source_root(db, owner, operator)
    if root.replaced_by_id is not None or not root.token_hash:
        raise BoardError("document_build_root_changed", "Use the original authenticated Root", status_code=409)
    selected = []
    if request.source is not None:
        from src.work_board.document_preparation import resolve
        _source_row, selected = await resolve(db, owner, request.source, goal_id=request.goal_id, operator=operator)
    refs = {item["source_ref"] for item in selected}
    if any(citation.source_ref not in refs for citation in spec.citations):
        raise BoardError("document_build_citation_unselected", "Select each exact source citation before building", status_code=422)
    selection_raw = sources.canonical(selected)
    if len(selection_raw) > SELECTION_LIMIT:
        raise BoardError("document_build_selection_too_large", "Select less private material", status_code=422)
    identifier = str(uuid.uuid5(uuid.NAMESPACE_URL,
        f"seraph:document-build:{owner.principal_id}:{owner.session_id}:{request.goal_id}:{request.goal_revision}:{request.idempotency_key}"))
    immutable = {"build_ref": "document-build:" + identifier, "spec_digest": _digest(raw),
        "selection_digest": _digest(selected), "source_binding": request.source.model_dump(mode="json") if request.source else None}
    payload = sources.canonical({"schema_version": 1, "capability_id": CAPABILITY, "input": immutable})
    await _begin_immediate(db)
    existing = await db.get(WorkBoardInputArtifact, identifier, populate_existing=True)
    if existing is not None:
        if (existing.owner_principal_id != owner.principal_id or existing.owner_session_id != owner.session_id
            or existing.payload_sha256 != _digest(payload)):
            raise BoardError("document_build_idempotency_conflict", "This key is bound to another specification", status_code=409)
        return projection(existing)
    stamp = now()
    row = WorkBoardInputArtifact(artifact_id=identifier, owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id, goal_id=request.goal_id, goal_revision=request.goal_revision,
        capability_id=CAPABILITY, capability_version="1", idempotency_key=request.idempotency_key,
        payload_sha256=_digest(payload), typed_input_ref="document-build:" + identifier, size_bytes=len(payload),
        document_reserved_bytes=CHARGE, expires_at=stamp+timedelta(hours=24))
    value = {"schema": "document-build.v1", "root": dict(root_binding()), "input": immutable,
        "phase": "reserved", "generation": 1, "live_writer": None, "sources": {},
        "spec_revision": 1, "spec_digest": immutable["spec_digest"],
        "selection_digest": immutable["selection_digest"], "source_binding": immutable["source_binding"],
        "formats": ["xlsx" if spec.kind == "table_workbook" else "docx", "pdf"],
        "root_authority": _digest({"id": root.id, "principal": root.principal_id,
            "token_hash": root.token_hash, "absolute": utc(root.absolute_expires_at).isoformat()})}
    goal, budget = await sources.authority(db, owner, row, value, value["root"])
    row.expires_at = min(utc(row.expires_at), utc(root.idle_expires_at), utc(root.absolute_expires_at),
        *[utc(t) for t in (goal.due_date, budget.period_expires_at) if t is not None])
    value["original_deadline"] = utc(row.expires_at).isoformat()
    await sources.check_quota(db, owner, CHARGE)
    row.document_metadata_json = sources.canonical(value).decode()
    db.add(row); await db.flush()
    row.metadata_digest = _metadata_digest(row)
    staged = _prepare_private_publication(row, value, (("spec", raw),) + (("selection", selection_raw),) if selected else (("spec", raw),))
    staged = _reserve_private_publication(row, value, staged)
    await db.commit()
    # Exact durable ciphertext inventory and charge precede any physical write.
    receipts = _publish_private_publication(row, value, staged)
    await _begin_immediate(db)
    fresh, current = await owned(db, owner, identifier, revision=staged.revision)
    if current != value or current.get("pending") != _publication_current(fresh, current, staged):
        raise BoardError("document_build_publication_changed", "Inspect the retained original build", status_code=409)
    current.pop("pending")
    current.update({"sources": receipts, "phase": "staged"})
    persist(fresh, current)
    await authority(db, owner, fresh, current, operator=operator, metadata_only=True)
    await db.commit()
    return projection(fresh)


def _review_binding(row, value, descriptor, *, root, expires_at, task=None):
    return {"schema": "document-build-review.v1", "owner_principal_id": row.owner_principal_id,
        "owner_session_id": row.owner_session_id, "root_authority": value["root_authority"],
        "root_token_digest": _digest(root.token_hash.encode()), "goal_id": row.goal_id,
        "goal_revision": row.goal_revision, "build_id": row.artifact_id, "build_revision": row.revision,
        "generation": 1, "spec_digest": value["spec_digest"], "selection_digest": value["selection_digest"],
        "source_binding_digest": _digest(value.get("source_binding")),
        "descriptor_digest": _digest(descriptor.model_dump(mode="json")), "policy_digest": descriptor.policy_digest,
        "renderer_profile": "document-build-renderer.v1", "renderer_profile_digest": PROFILE_DIGEST,
        "limits_digest": _digest(SLOTS), "formats": value["formats"], "original_deadline": value["original_deadline"],
        "task_id": task.task_id if task else None, "task_revision": task.task_revision if task else None,
        "plan_revision": 1 if task else None, "expires_at": expires_at}


def _review_mac(binding):
    from src.memory.repository import _effect_mac_key
    from src.extensions.capability_execution import CapabilityJournalError
    try:
        key = _effect_mac_key()
    except CapabilityJournalError:
        raise BoardError("document_build_review_key_unavailable", "Restore the original review signing key", status_code=409) from None
    return hmac.new(key, b"seraph-document-build-review-v1\0" + sources.canonical(binding), hashlib.sha256).hexdigest()


async def verify_review(db, owner, row, value, review, *, task=None, descriptor=None):
    if descriptor is None:
        from src.work_board.document_build_native import descriptor as build_descriptor
        descriptor = build_descriptor()
    root = await authority(db, owner, row, value, metadata_only=True)
    review = BuildReview.model_validate(review) if not isinstance(review, BuildReview) else review
    supplied = review.binding.model_dump(mode="json")
    try:
        expiry = utc(datetime.fromisoformat(supplied["expires_at"]))
        if expiry <= now() or expiry > min(now()+timedelta(minutes=5), utc(row.expires_at)):
            raise ValueError("review expiry")
        expected = _review_binding(row, value, descriptor, root=root, expires_at=supplied["expires_at"], task=task)
        if supplied != expected or not hmac.compare_digest(review.mac, _review_mac(expected)):
            raise ValueError("review binding")
    except (KeyError, TypeError, ValueError):
        raise BoardError("document_build_review_changed", "Reload the exact private build review before accepting", status_code=409) from None
    return review.model_dump(mode="json")


async def preview(db, owner, operator, identifier, *, descriptor):
    row, value = await owned(db, owner, identifier)
    root = await authority(db, owner, row, value, operator=operator)
    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == row.bound_task_id)) if row.bound_task_id else None
    spec = spec_read(row, value)
    selected = selection_read(row, value)
    expiry = min(now()+timedelta(minutes=5), utc(row.expires_at))
    if task is not None:
        from src.work_board.dispatcher import _parse_typed_input
        from src.work_board.contracts import GeneralTaskEnvelope
        envelope = GeneralTaskEnvelope.model_validate(_parse_typed_input(task))
        if envelope.proposal_group:
            expiry = min(expiry, envelope.proposal_group.original_deadline_at)
    if expiry <= now():
        raise BoardError("document_build_review_expired", "The original task review window expired", status_code=409)
    binding = _review_binding(row, value, descriptor, root=root, expires_at=expiry.isoformat(), task=task)
    return {**projection(row, value), "spec": spec.model_dump(mode="json"), "selection": selected,
        "review": {"binding": binding, "mac": _review_mac(binding)},
        "limits": {"spec_bytes": SPEC_LIMIT, "editable_bytes": OUTPUT_LIMIT, "pdf_bytes": OUTPUT_LIMIT},
        "formats": ["xlsx" if spec.kind == "table_workbook" else "docx", "pdf"]}


async def prepare(db, owner, operator, service, identifier, request):
    from src.work_board.contracts import GeneralTaskCreate, GeneralTaskInput, TaskLimits, PlanSpec, PlanStep
    row, value = await owned(db, owner, identifier)
    prepare_digest = _digest(request.model_dump(mode="json"))
    if row.bound_task_id:
        await authority(db, owner, row, value, operator=operator, metadata_only=True)
        if value.get("prepare_request_digest") != prepare_digest:
            raise BoardError("document_build_already_prepared", "Inspect the original Work task", status_code=409)
        from src.db.models import WorkBoardEvent
        from src.work_board.repository import BoardMutation
        task = await service.repository.get_task(db, owner, row.bound_task_id)
        event = await db.scalar(select(WorkBoardEvent).where(WorkBoardEvent.task_id == task.task_id)
            .order_by(WorkBoardEvent.event_id.desc()).limit(1))
        if event is None:
            raise BoardError("document_build_task_unverified", "The original task publication needs recovery", status_code=409)
        return BoardMutation(task, event, idempotent_replay=True)
    if row.revision != request.expected_revision:
        raise BoardError("document_build_revision_conflict", "Reload the exact current build", status_code=409)
    descriptors, tool_digest = service.snapshot()
    descriptor = next((item for item in descriptors if item.tool_id == "document_build"), None)
    if descriptor is None:
        raise BoardError("document_build_unavailable", "Restore the fixed local document build tool", status_code=503)
    await verify_review(db, owner, row, value, request.review, descriptor=descriptor)
    await authority(db, owner, row, value, operator=operator)
    mutation = await service.create(db, owner, GeneralTaskCreate(goal_revision=row.goal_revision,
        idempotency_key="document-build:" + identifier, expected_plan_revision=1,
        input=GeneralTaskInput(goal_ref=row.goal_id, intent="Build the reviewed local document specification",
            requested_output=descriptor.output_schema, document_build=build_binding(row, value),
            tool_set_digest=tool_digest, limits=TaskLimits(max_steps=1, max_inference_calls=0,
                max_cost_microusd=0, max_outstanding_children=0, wall_seconds=60)),
        plan=PlanSpec(revision=1, steps=[PlanStep(step_id="build", tool_id="document_build",
            input={"build_ref": "document-build:" + identifier, "spec_digest": value["spec_digest"]},
            output_contract=descriptor.output_schema)])))
    await db.flush()
    # Bind current build in the same task publication transaction, before commit.
    fresh, current = await owned(db, owner, identifier, revision=request.expected_revision)
    await authority(db, owner, fresh, current, operator=operator, metadata_only=True)
    fresh.bound_task_id = mutation.task.task_id
    fresh.bound_task_revision = mutation.task.task_revision
    current.update({"phase": "bound", "prepare_request_digest": prepare_digest})
    persist(fresh, current)
    await db.flush()
    return mutation


@dataclass(frozen=True)
class StagedBuildOutput:
    artifact_id: str
    revision: int
    original_metadata_digest: str
    slots_json: str
    output_json: str
    manifest_json: str
    publication: PrivateBuildPublication = field(repr=False)
    _seal: object = field(default=None, repr=False, compare=False)
    _issued_id: int = field(default=0, repr=False, compare=False)


def prepare_publications(row, value, rendered, *, task_id, attempt_id, job_id, fence, profile_digest, producer):
    """Private fixed immutable publication outside the native SQLite writer."""
    from src.work_board.document_build_contracts import DocumentOutput
    from src.work_board.document_build_native import validate_rendered_supervision
    validate_rendered_supervision(producer, rendered, row=row, value=value)
    editable = rendered.editable
    pdf = rendered.pdf
    if not isinstance(editable, bytes) or not 0 < len(editable) <= OUTPUT_LIMIT or (pdf is not None and (not isinstance(pdf, bytes) or not 0 < len(pdf) <= OUTPUT_LIMIT)):
        raise BoardError("document_build_output_bound", "Renderer output exceeds the fixed bounds", status_code=409)
    allowed_media = {"application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
    if rendered.editable_media not in allowed_media:
        raise BoardError("document_build_media_changed", "The fixed renderer media changed", status_code=409)
    specification = spec_read(row, value)
    expected_media = ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if specification.kind == "table_workbook" else "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    if (rendered.editable_media != expected_media or rendered.editable_extension != value["formats"][0]
        or rendered.source_refs != [citation.source_ref for citation in specification.citations]):
        raise BoardError("document_build_render_binding_changed", "The fixed renderer output does not match the reviewed specification", status_code=409)
    def artifact(slot, raw, media):
        return {"artifact_ref": f"document-build:{row.artifact_id}:{slot}", "sha256": _digest(raw),
            "size_bytes": len(raw), "media_type": media}
    output = DocumentOutput(editable_artifact=artifact("editable", editable, rendered.editable_media),
        pdf_artifact=artifact("pdf", pdf, "application/pdf") if pdf else None,
        source_refs=rendered.source_refs, warnings=rendered.warnings)
    manifest = {"schema": "document-build-output.v1", "build_id": row.artifact_id,
        "spec_digest": value["spec_digest"], "selection_digest": value["selection_digest"],
        "task_id": task_id, "attempt_id": attempt_id, "job_id": job_id, "fence": fence,
        "profile_digest": profile_digest, "output": output.model_dump(mode="json")}
    raw_manifest = sources.canonical(manifest)
    if len(raw_manifest) > MANIFEST_LIMIT:
        raise BoardError("document_build_manifest_bound", "The original output manifest exceeds its bound", status_code=409)
    publication = _prepare_private_publication(row, value, tuple((slot, raw) for slot, raw in
        (("editable", editable), ("pdf", pdf), ("output-manifest", raw_manifest)) if raw is not None))
    pending = json.loads(publication.pending_json)
    slots = {slot: {"cipher_size": receipt["cipher_size"], "cipher_sha256": receipt["cipher_sha256"]}
        for slot, receipt in pending.items()}
    return _issue_private_packet(StagedBuildOutput(row.artifact_id, row.revision, row.metadata_digest,
        sources.canonical(slots).decode(), sources.canonical(output.model_dump(mode="json")).decode(),
        raw_manifest.decode(), publication, _OUTPUT_SEAL))


async def reserve_publications(db, row, value, staged):
    if type(staged) is not StagedBuildOutput or staged._seal is not _OUTPUT_SEAL or staged._issued_id != id(staged):
        raise BoardError("document_build_publication_changed", "The original output producer is required", status_code=409)
    publication = _reserve_private_publication(row, value, staged.publication)
    await db.flush()
    return _issue_private_packet(replace(staged, revision=row.revision, original_metadata_digest=row.metadata_digest, publication=publication))


def publish_publications(row, value, staged):
    if type(staged) is not StagedBuildOutput or staged._seal is not _OUTPUT_SEAL or staged._issued_id != id(staged):
        raise BoardError("document_build_publication_changed", "The original output producer is required", status_code=409)
    receipts = _publish_private_publication(row, value, staged.publication)
    if receipts != json.loads(staged.slots_json):
        raise BoardError("document_build_publication_changed", "The exact reserved ciphertext changed", status_code=409)


def adopt_outputs(row, value, staged, *, native_binding, reap):
    """Called only by original native owner inside its current final writer."""
    if (type(staged) is not StagedBuildOutput or staged._seal is not _OUTPUT_SEAL
        or staged._issued_id != id(staged)
        or staged.artifact_id != row.artifact_id or staged.revision != row.revision
        or staged.original_metadata_digest != row.metadata_digest or row.metadata_digest != _metadata_digest(row)
        or not reap or reap.get("wait_reaped") is not True or not reap.get("witness_sha256")
        or value.get("renderer_binding") != native_binding or not value.get("live_writer")
        or value["live_writer"].get("token") != native_binding.get("nonce")):
        raise BoardError("document_build_adoption_changed", "The original native renderer and positive reap are required", status_code=409)
    pending = _publication_current(row, value, staged.publication)
    if value.get("pending") != pending:
        raise BoardError("document_build_adoption_changed", "The source-owned reserved output inventory changed", status_code=409)
    for slot, receipt in pending.items():
        sources.read_private(sources.source_path(row, value, slot), receipt, maximum=SLOTS[slot])
    output = json.loads(staged.output_json)
    value.pop("pending")
    value["sources"].update(json.loads(staged.slots_json))
    value.update({"output": output, "output_manifest_digest": _digest(staged.manifest_json.encode()),
        "reap": reap, "live_writer": None, "phase": "completed" if output["pdf_artifact"] else "degraded", "reason": None})
    persist(row, value)
    return output


async def _native_output_proof(db, row, value, *, terminal=False):
    if not row.bound_task_id or not value.get("output") or not value.get("reap") or value.get("live_writer"):
        raise BoardError("document_build_output_unverified", "The original task has no positively closed output", status_code=409)
    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == row.bound_task_id)
        .execution_options(populate_existing=True))
    binding = value.get("renderer_binding", {})
    attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == binding.get("attempt_id")))
    child = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == binding.get("job_id")))
    if (task is None or task.owner_principal_id != row.owner_principal_id or task.owner_session_id != row.owner_session_id
        or task.goal_id != row.goal_id or task.goal_revision != row.goal_revision
        or attempt is None or attempt.task_id != task.task_id or child is None
        or child.parent_job_id != attempt.workflow_run_id or child.status not in {"succeeded", "degraded"}
        or child.fencing_token != binding.get("child_fence") or child.attempt_count != 1):
        raise BoardError("document_build_output_unverified", "The original successful native task readback is required", status_code=409)
    from src.work_board.document_build_native import validate_output_readback
    await validate_output_readback(db, task, attempt, child, row, value)
    if terminal and (str(getattr(task.status, "value", task.status)) not in {"done", "archived", "cancelled"}
        or attempt.ended_at is None):
        raise BoardError("document_build_task_not_terminal", "Finish the original task before retiring its files", status_code=409)
    return task, attempt, child


async def outputs(db, owner, operator, identifier):
    row, value = await owned(db, owner, identifier)
    await authority(db, owner, row, value, operator=operator)
    await _native_output_proof(db, row, value)
    raw = sources.read_private(sources.source_path(row, value, "output-manifest"), value["sources"]["output-manifest"], maximum=MANIFEST_LIMIT)
    if _digest(raw) != value.get("output_manifest_digest") or json.loads(raw)["output"] != value["output"]:
        raise BoardError("document_build_manifest_changed", "The original physical output manifest changed", status_code=409)
    return {**projection(row, value), "output": value["output"]}


async def output_read(db, owner, operator, identifier, slot):
    if slot not in {"editable", "pdf"}:
        raise BoardError("document_build_slot_invalid", "Select an original editable or PDF output", status_code=404)
    await outputs(db, owner, operator, identifier)
    row, value = await owned(db, owner, identifier)
    artifact = value["output"].get(slot + "_artifact")
    if artifact is None:
        raise BoardError("document_build_pdf_unavailable", "The task retained editable output and a missing-PDF warning", status_code=409)
    raw = sources.read_private(sources.source_path(row, value, slot), value["sources"][slot], maximum=OUTPUT_LIMIT)
    if len(raw) != artifact["size_bytes"] or _digest(raw) != artifact["sha256"]:
        raise BoardError("document_build_output_changed", "The original physical output changed", status_code=409)
    extension = "pdf" if slot == "pdf" else ("xlsx" if artifact["media_type"].endswith("spreadsheetml.sheet") else "docx")
    return raw, artifact["media_type"], f"document-{identifier}.{extension}"


def _cleanup(row, value):
    """Positive no-follow exact names/inodes/hashes; foreign or missing holds quota."""
    path = sources.source_path(row, value, "spec")
    parent, _leaf = sources._open_input_artifact_parent(path, create=False)
    held = []
    try:
        expected = {f"g1-{slot}.fernet": receipt for slot, receipt in value["sources"].items()}
        listed = set(os.listdir(parent))
        for slot, receipt in value.get("pending", {}).items():
            _validate_pending(row, value, slot, receipt)
            final = f"g1-{slot}.fernet"
            if final in expected:
                raise OSError("pending and adopted slot overlap")
            present = listed & {receipt["file"], final}
            if len(present) != 1:
                raise OSError("pending fragment missing or both links retained")
            expected[present.pop()] = receipt
        witness = value.get("reap")
        if witness and witness.get("witness_name"):
            name = witness["witness_name"]
            if not re.fullmatch(r"[a-f0-9]{32}\.witness\.json", name):
                raise OSError("original witness name changed")
            expected[name] = {"cipher_size": witness["witness_size"], "cipher_sha256": witness["witness_sha256"]}
        if set(os.listdir(parent)) != set(expected):
            raise OSError("foreign or missing private build fragments")
        for leaf, receipt in expected.items():
            fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            held.append((leaf, fd))
            facts = os.fstat(fd)
            if not _private_input_file_metadata(facts) or facts.st_size != receipt["cipher_size"]:
                raise OSError("private build metadata changed")
            digest = hashlib.sha256()
            remaining = facts.st_size
            while remaining:
                chunk = os.read(fd, min(65536, remaining))
                if not chunk: raise OSError("private build truncated")
                digest.update(chunk); remaining -= len(chunk)
            named = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            if (named.st_dev, named.st_ino) != (facts.st_dev, facts.st_ino) or digest.hexdigest() != receipt["cipher_sha256"]:
                raise OSError("private build identity changed")
        # All entries are verified before the first unlink. Partial cleanup remains charged.
        for leaf, fd in held:
            facts = os.fstat(fd); named = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
            if (named.st_dev, named.st_ino) != (facts.st_dev, facts.st_ino): raise OSError("cleanup raced")
            os.unlink(leaf, dir_fd=parent)
        os.fsync(parent)
        if os.listdir(parent): raise OSError("cleanup directory not empty")
    finally:
        for _leaf, fd in held: os.close(fd)
        os.close(parent)


async def retire(db, owner, operator, identifier, request, *, jobs=None):
    await _begin_immediate(db)
    row, value = await owned(db, owner, identifier)
    await cleanup_authority(db, owner, row, value, operator)
    if value["phase"] == "deleted":
        if value.get("retirement_key") != request.idempotency_key:
            raise BoardError("document_build_retire_key_changed", "Use the original retirement request", status_code=409)
        return projection(row)
    if row.revision != request.expected_revision:
        raise BoardError("document_build_revision_conflict", "Reload the current original build before retirement", status_code=409)
    if value.get("live_writer"):
        if jobs is None:
            raise BoardError("document_build_cleanup_unknown", "The original writer has no positive closure", status_code=409)
        original_digest = row.metadata_digest
        await db.rollback()
        from src.work_board.document_build_native import reconcile_reap
        result = await reconcile_reap(jobs, owner, identifier, operator,
            expected_revision=request.expected_revision, expected_metadata_digest=original_digest)
        if result.get("cleanup_proven") is not True:
            raise BoardError("document_build_cleanup_unknown", "The original writer has no positive closure", status_code=409)
        await _begin_immediate(db)
        row, value = await owned(db, owner, identifier, revision=result["build_revision"])
        await cleanup_authority(db, owner, row, value, operator)
        if row.metadata_digest != result["metadata_digest"] or value.get("live_writer"):
            raise BoardError("document_build_cleanup_unknown", "The exact original cleanup changed", status_code=409)
    retirement_mode = "unbound"
    if row.bound_task_id:
        if value.get("output") is None:
            if value.get("output_manifest_digest") or set(value["sources"]) & {"editable", "pdf", "output-manifest"}:
                raise BoardError("document_build_outputless_changed", "The original build has adopted output facts", status_code=409)
            from src.work_board.document_build_native import validate_outputless_retirement
            task, attempt = await validate_outputless_retirement(db, owner, row, value)
            retirement_mode = "terminal_outputless"
        else:
            task, attempt, _child = await _native_output_proof(db, row, value, terminal=True)
            retirement_mode = "adopted_output"
        if request.expected_task_revision != task.task_revision or request.attempt_id != attempt.attempt_id:
            raise BoardError("document_build_retire_binding_changed", "Reload the original terminal task and attempt", status_code=409)
    elif value["phase"] not in {"staged", "reserved", "cleanup_tombstone"}:
        raise BoardError("document_build_cleanup_unknown", "Inspect the original build closure", status_code=409)
    if value.get("retirement_key") and value["retirement_key"] != request.idempotency_key:
        raise BoardError("document_build_retire_key_changed", "Use the original retirement request", status_code=409)
    value.update({"phase": "cleanup_tombstone", "reason": "document_build_cleanup_pending",
        "retirement_key": request.idempotency_key, "retirement_mode": retirement_mode})
    persist(row, value); revision = row.revision
    await db.commit()
    try:
        _cleanup(row, value)
    except (OSError, KeyError, ValueError):
        raise BoardError("document_build_cleanup_required", "Original files still need exact cleanup; quota remains held", status_code=409) from None
    await _begin_immediate(db)
    fresh, current = await owned(db, owner, identifier, revision=revision)
    await cleanup_authority(db, owner, fresh, current, operator)
    if current != value or current["root"] != dict(root_binding()) or current.get("live_writer"):
        raise BoardError("document_build_retire_changed", "The original retirement changed; quota remains held", status_code=409)
    current.update({"phase": "deleted", "reason": None})
    fresh.state = "deleted"; fresh.document_reserved_bytes = 0
    persist(fresh, current); await db.commit()
    return projection(fresh)
