"""ADR017 pair reservation and private source I/O on the existing input row.

All SQL writers use staged filesystem/Root facts. Unknown writers retain their
generation and quota; elapsed time never proves a descriptor was closed.
"""
from __future__ import annotations
from datetime import timedelta
import hashlib
import json
import os
import uuid
from sqlalchemy import func, select
from config.settings import settings
from src.db.models import WorkBoardInputArtifact
from src.work_board.contracts import WorkBoardOwner
from src.work_board.document_compare_contracts import DocumentCompareInput
from src.work_board.document_compare_parser import CAPABILITY, canonical, sha256
from src.work_board.input_artifacts import (_begin_immediate, _metadata, _metadata_digest,
    _open_input_artifact_parent, _private_input_file_metadata, _write_payload, _payload_path,
    _safe_file_bytes, INPUT_ARTIFACT_ROOT)
from src.work_board.repository import BoardError, WorkBoardRepository
from src.work_board.pipelines import now, utc, root_binding
from src.workspace import canonical_workspace_root

PREFIX = "artifacts/work-board/document-pairs"
CHARGE = 16 * 1024 * 1024


def metadata(row):
    try:
        value = json.loads(row.document_metadata_json)
        if value["schema"] != "document-pair.v1" or len(row.document_metadata_json) > 8192:
            raise ValueError()
        return value
    except (TypeError, ValueError, KeyError):
        raise BoardError("document_pair_metadata_invalid", "The private pair needs reconciliation", status_code=409) from None


def projection(row):
    value = metadata(row)
    return {"artifact_id": row.artifact_id, "revision": row.revision, "state": row.state,
        "pair_state": value["phase"], "generation": value["generation"],
        "uploaded": sorted(value["sources"]), "ingest_deadline": value["ingest_deadline"],
        "typed_input_ref": row.typed_input_ref if row.metadata_digest else None,
        "typed_input_digest": row.payload_sha256 if row.metadata_digest else None,
        "goal_id": row.goal_id, "goal_revision": row.goal_revision,
        "no_learning": True, "reason_code": value.get("reason"),
        "quota_reserved_bytes": row.document_reserved_bytes}


async def owned(db, owner, identifier, *, revision=None):
    row = await db.scalar(select(WorkBoardInputArtifact).where(
        WorkBoardInputArtifact.artifact_id == identifier,
        WorkBoardInputArtifact.owner_principal_id == owner.principal_id,
        WorkBoardInputArtifact.owner_session_id == owner.session_id,
        WorkBoardInputArtifact.capability_id == CAPABILITY).execution_options(populate_existing=True))
    if row is None: raise BoardError("document_pair_not_found", "The private pair is unavailable", status_code=404)
    if revision is not None and row.revision != revision:
        raise BoardError("document_pair_revision_conflict", "Read the current private pair before retry", status_code=409)
    return row, metadata(row)


async def authority(db, owner, row, value, staged_root, *, ingest=False):
    if value["root"] != staged_root:
        raise BoardError("document_pair_root_changed", "The original workspace changed", status_code=409)
    goal = await WorkBoardRepository._validate_goal(db, owner, goal_id=row.goal_id, goal_revision=row.goal_revision)
    from src.goals.repository import deserialize_admission_budget
    budget = deserialize_admission_budget(goal)
    if str(getattr(goal.status, "value", goal.status)) != "active" or budget is None:
        raise BoardError("document_pair_goal_not_active", "Activate the bounded Goal before selecting documents", status_code=409)
    stamp = now()
    for deadline in (row.expires_at, goal.due_date, budget.period_expires_at):
        if deadline is not None and utc(deadline) <= stamp:
            raise BoardError("document_pair_authority_expired", "The original document authority expired", status_code=409)
    if ingest and utc(__import__('datetime').datetime.fromisoformat(value['ingest_deadline'])) <= stamp:
        raise BoardError("document_pair_ingest_expired", "The original upload window expired; reconcile cleanup", status_code=409)
    return goal, budget


async def reserve(db, owner, request):
    if request.csv.size_bytes > 1024 * 1024:
        raise BoardError("document_csv_size_exceeded", "CSV must be at most 1 MiB", status_code=422)
    staged_root = dict(root_binding())
    identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, f"seraph:document-pair:{owner.principal_id}:{owner.session_id}:{request.goal_id}:{request.goal_revision}:{request.idempotency_key}"))
    inputs = DocumentCompareInput(schema_version=1, operation=request.operation,
        pair_ref="document-pair:" + identifier, pdf=request.pdf, csv=request.csv, no_learning=True).model_dump()
    payload = canonical({"schema_version": 1, "capability_id": CAPABILITY, "input": inputs})
    digest = sha256(payload); stamp = now()
    await _begin_immediate(db)
    existing = await db.get(WorkBoardInputArtifact, identifier, populate_existing=True)
    if existing is not None:
        if (existing.owner_principal_id != owner.principal_id or existing.owner_session_id != owner.session_id
            or existing.payload_sha256 != digest):
            raise BoardError("document_pair_idempotency_conflict", "This key is already bound to another private pair", status_code=409)
        return projection(existing)
    row = WorkBoardInputArtifact(artifact_id=identifier, owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id, goal_id=request.goal_id, goal_revision=request.goal_revision,
        capability_id=CAPABILITY, capability_version="1", idempotency_key=request.idempotency_key,
        payload_sha256=digest, typed_input_ref=f"workspace-json:{INPUT_ARTIFACT_ROOT}/{identifier}-{digest}.json",
        size_bytes=len(payload), state="pending", created_at=stamp, expires_at=stamp+timedelta(hours=24),
        document_reserved_bytes=CHARGE)
    value = {"schema": "document-pair.v1", "root": staged_root, "input": inputs,
        "phase": "reserved", "generation": 1, "live_writer": None, "sources": {},
        "ingest_deadline": (stamp+timedelta(seconds=300)).isoformat()}
    goal, budget = await authority(db, owner, row, value, staged_root)
    deadlines = [row.expires_at, stamp+timedelta(seconds=300)] + [utc(v) for v in (goal.due_date, budget.period_expires_at) if v is not None]
    value["ingest_deadline"] = min(deadlines).isoformat()
    row.expires_at = min([row.expires_at] + [utc(v) for v in (goal.due_date, budget.period_expires_at) if v is not None])
    rows = list((await db.scalars(select(WorkBoardInputArtifact).where(WorkBoardInputArtifact.document_reserved_bytes > 0))).all())
    own = [r for r in rows if r.owner_principal_id == owner.principal_id]
    if (sum(r.document_reserved_bytes for r in rows)+CHARGE > 256*1024*1024
        or sum(r.document_reserved_bytes for r in own)+CHARGE > 64*1024*1024
        or sum(metadata(r)["phase"] != "sealed" for r in rows) >= 16
        or sum(metadata(r)["phase"] != "sealed" for r in own) >= 2):
        raise BoardError("document_pair_quota_full", "Reconcile retained private pairs before reserving more", status_code=409)
    row.document_metadata_json = canonical(value).decode(); db.add(row)
    await db.flush(); await db.commit()
    return projection(row)


def source_path(row, value, slot):
    if slot not in {"pdf", "csv"}: raise ValueError("fixed document slot required")
    return canonical_workspace_root(settings.workspace_dir) / PREFIX / row.artifact_id / f"g{value['generation']}-{slot}.fernet"


def publish_private(path, plaintext):
    """Binary Fernet source, no plaintext filesystem or inline/base64 input."""
    from src.vault.crypto import _get_fernet
    ciphertext = _get_fernet().encrypt(plaintext)
    parent, leaf = _open_input_artifact_parent(path, create=True)
    temporary = f".{leaf}.{uuid.uuid4().hex}.pending"; descriptor = -1
    try:
        descriptor = os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,"O_NOFOLLOW",0), 0o600, dir_fd=parent)
        view = memoryview(ciphertext)
        while view:
            count = os.write(descriptor, view); view = view[count:]
        os.fsync(descriptor); os.close(descriptor); descriptor = -1
        os.link(temporary, leaf, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        os.unlink(temporary, dir_fd=parent); os.fsync(parent)
    finally:
        if descriptor >= 0: os.close(descriptor)
        os.close(parent)
    return {"cipher_sha256": sha256(ciphertext), "cipher_size": len(ciphertext)}


def read_private(path, receipt, *, maximum):
    from src.vault.crypto import _get_fernet
    parent, leaf = _open_input_artifact_parent(path, create=False)
    fd = -1
    try:
        fd = os.open(leaf, os.O_RDONLY|getattr(os,"O_NOFOLLOW",0), dir_fd=parent)
        stat = os.fstat(fd)
        if not _private_input_file_metadata(stat) or stat.st_size != receipt["cipher_size"] or stat.st_size > (maximum+1024)*2:
            raise ValueError("private document ciphertext metadata changed")
        chunks=[]; remaining=stat.st_size
        while remaining:
            chunk=os.read(fd,min(65536,remaining))
            if not chunk: raise ValueError("private document ciphertext truncated")
            chunks.append(chunk); remaining-=len(chunk)
        ciphertext=b"".join(chunks)
    finally:
        if fd >= 0: os.close(fd)
        os.close(parent)
    if sha256(ciphertext) != receipt["cipher_sha256"]: raise ValueError("private document ciphertext changed")
    plaintext=_get_fernet().decrypt(ciphertext)
    if len(plaintext)>maximum: raise ValueError("private document plaintext exceeds bound")
    return plaintext


async def acquire_upload(db, owner, identifier, revision, slot):
    staged_root=dict(root_binding()); token=uuid.uuid4().hex
    await _begin_immediate(db)
    row,value=await owned(db,owner,identifier,revision=revision)
    await authority(db,owner,row,value,staged_root,ingest=True)
    if slot not in {"pdf","csv"} or value["phase"] not in {"reserved","uploading"} or slot in value["sources"]:
        raise BoardError("document_pair_slot_unavailable", "This source slot cannot be overwritten", status_code=409)
    rows=list((await db.scalars(select(WorkBoardInputArtifact).where(WorkBoardInputArtifact.document_reserved_bytes>0))).all())
    active=[r for r in rows if metadata(r).get("live_writer")]
    if value.get("live_writer") or len(active)>=2 or any(r.owner_principal_id==owner.principal_id for r in active):
        raise BoardError("document_upload_writer_busy", "A private upload writer is already held", status_code=409)
    value["phase"]="uploading"; value["live_writer"]={"token":token,"slot":slot}
    row.document_metadata_json=canonical(value).decode(); row.revision+=1
    await db.commit()
    return row,value,token


async def upload(db,owner,identifier,revision,slot,stream):
    row,value,token=await acquire_upload(db,owner,identifier,revision,slot)
    descriptor=value["input"][slot]; data=bytearray(); result=None; reason=None
    try:
        async for chunk in stream:
            if len(data)+len(chunk)>descriptor["size_bytes"]:
                raise ValueError("document_source_size_mismatch")
            data.extend(chunk)
        raw=bytes(data)
        if len(raw)!=descriptor["size_bytes"] or sha256(raw)!=descriptor["sha256"]:
            raise ValueError("document_source_digest_mismatch")
        result=publish_private(source_path(row,value,slot),raw)
        if read_private(source_path(row,value,slot),result,maximum=descriptor["size_bytes"])!=raw:
            raise ValueError("document_source_readback_failed")
    except Exception:
        reason="document_upload_failed_cleanup_required"
    # Reached only after the actual awaited stream and held descriptors close.
    # Cancellation/BaseException skips this writer; unknown remains held.
    staged_root=dict(root_binding()); await _begin_immediate(db)
    fresh,current=await owned(db,owner,identifier)
    if current.get("live_writer")!={"token":token,"slot":slot}:
        raise BoardError("document_upload_fence_changed", "The original writer needs reconciliation",status_code=409)
    current["live_writer"]=None
    if result is not None and reason is None:
        current["sources"][slot]=result
    else:
        current["phase"]="cleanup_required"; current["reason"]=reason
    fresh.document_metadata_json=canonical(current).decode(); fresh.revision+=1
    await db.commit()
    if reason: raise BoardError(reason,"The private upload failed; its generation remains charged",status_code=409)
    return projection(fresh)


async def complete(db,owner,identifier,revision):
    row,value=await owned(db,owner,identifier,revision=revision)
    staged_root=dict(root_binding()); await authority(db,owner,row,value,staged_root,ingest=True)
    if value["phase"]=="sealed": return projection(row)
    if value.get("live_writer") or set(value["sources"])!={"pdf","csv"}:
        raise BoardError("document_pair_incomplete", "Both immutable sources must finish and read back",status_code=409)
    for slot in ("pdf","csv"):
        raw=read_private(source_path(row,value,slot),value["sources"][slot],maximum=value["input"][slot]["size_bytes"])
        if sha256(raw)!=value["input"][slot]["sha256"]: raise BoardError("document_source_changed","The private source changed",status_code=409)
    payload=canonical({"schema_version":1,"capability_id":CAPABILITY,"input":value["input"]})
    _write_payload(_payload_path(row),payload)
    _safe_file_bytes(_payload_path(row),expected_digest=row.payload_sha256,expected_size=row.size_bytes)
    await _begin_immediate(db)
    fresh,current=await owned(db,owner,identifier,revision=revision)
    await authority(db,owner,fresh,current,staged_root,ingest=True)
    if current != value: raise BoardError("document_pair_revision_conflict","The pair changed before sealing",status_code=409)
    current["phase"]="sealed"; fresh.document_metadata_json=canonical(current).decode(); fresh.revision+=1
    fresh.metadata_digest=_metadata_digest(fresh)
    await db.commit(); return projection(fresh)


async def source_pair(db,task,inputs):
    owner=WorkBoardOwner(principal_id=task.owner_principal_id,session_id=task.owner_session_id)
    row,value=await owned(db,owner,task.input_artifact_id)
    await authority(db,owner,row,value,dict(root_binding()))
    if (value["phase"]!="sealed" or row.state!="bound" or row.bound_task_id!=task.task_id
        or row.payload_sha256!=task.typed_input_digest or value["input"]!=dict(inputs)
        or row.metadata_digest!=_metadata_digest(row)):
        raise BoardError("document_pair_binding_changed","The sealed pair is not bound to this exact task",status_code=409)
    result=[]
    for slot in ("pdf","csv"):
        raw=read_private(source_path(row,value,slot),value["sources"][slot],maximum=value["input"][slot]["size_bytes"])
        if sha256(raw)!=value["input"][slot]["sha256"]: raise BoardError("document_source_changed","The private source changed",status_code=409)
        result.append(raw)
    return tuple(result)
