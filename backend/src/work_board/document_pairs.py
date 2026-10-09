"""ADR017 pair reservation and private source I/O on the existing input row.

All SQL writers use staged filesystem/Root facts. Unknown writers retain their
generation and quota; elapsed time never proves a descriptor was closed.
"""
from __future__ import annotations
from datetime import timedelta
import asyncio
import fcntl
import hashlib
import json
import os
import re
import sys
import time
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
SOURCE_CAPABILITY = "document.read.v1"
SOURCE_PREFIX = "artifacts/work-board/document-sources"
BUILD_CAPABILITY = "document.build.v1"
BUILD_PREFIX = "artifacts/work-board/document-builds"
BUILD_CHARGE = 24 * 1024 * 1024


async def probe_upload_profile():
    """One bounded local kernel-contract probe owned by the document lifecycle."""
    root = dict(root_binding())
    path = canonical_workspace_root(settings.workspace_dir)/SOURCE_PREFIX/".upload-profile.lock"
    parent = fd = -1
    process = None
    positively_waited = False
    deadline = time.monotonic()+2
    try:
        parent, leaf = _open_input_artifact_parent(path, create=True)
        try: fd = os.open(leaf, os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600, dir_fd=parent)
        except FileExistsError: fd = os.open(leaf, os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK, dir_fd=parent)
        facts = os.fstat(fd)
        if not _private_input_file_metadata(facts) or facts.st_size != 0:
            raise OSError("profile lock metadata changed")
        fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
        # This child uses only local descriptors and stdlib kernel calls. It has
        # an empty environment, no source content and no provider transport.
        code = """import fcntl,os,signal,sys
signal.alarm(2)
fd=os.open(sys.argv[2],os.O_RDWR|os.O_NOFOLLOW,dir_fd=int(sys.argv[1]))
facts=os.fstat(fd)
if (facts.st_dev,facts.st_ino)!=(int(sys.argv[3]),int(sys.argv[4])): sys.exit(3)
try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
except BlockingIOError: print('excluded',flush=True)
else: sys.exit(4)
if sys.stdin.buffer.read(1)!=b'x': sys.exit(5)
fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
print('acquired',flush=True)
os.close(fd)
"""
        async with asyncio.timeout(max(.001, deadline-time.monotonic()-.5)):
            process = await asyncio.create_subprocess_exec(sys.executable, "-I", "-c", code,
                str(parent), leaf, str(facts.st_dev), str(facts.st_ino), env={}, close_fds=True,
                pass_fds=(parent,), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
            if await process.stdout.readline() != b"excluded\n": raise OSError("cross-process exclusion unavailable")
            fcntl.flock(fd, fcntl.LOCK_UN)
            process.stdin.write(b"x"); await process.stdin.drain(); process.stdin.close()
            if await process.stdout.readline() != b"acquired\n": raise OSError("cross-process lock release unavailable")
            if await process.wait() != 0: raise OSError("profile child failed")
            positively_waited = True
        fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
        named = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        if named.st_dev != facts.st_dev or named.st_ino != facts.st_ino: raise OSError("profile name changed")
        os.unlink(leaf, dir_fd=parent); os.fsync(parent)
        directory = os.fstat(parent)
        return {"root": root, "directory_device": directory.st_dev, "directory_inode": directory.st_ino,
            "boot_nonce": uuid.uuid4().hex, "process_id": os.getpid(), "cross_process": True,
            "probe_inode": facts.st_ino, "positive_wait": True, "active": True}
    finally:
        try:
            if process is not None and not positively_waited:
                if process.returncode is None: process.kill()
                await asyncio.wait_for(process.wait(), max(.001, deadline-time.monotonic()))
        finally:
            if fd >= 0: os.close(fd)
            if parent >= 0: os.close(parent)


def validate_upload_profile(profile):
    if (not isinstance(profile, dict) or profile.get("active") is not True or profile.get("process_id") != os.getpid()
        or profile.get("cross_process") is not True or profile.get("positive_wait") is not True
        or not re.fullmatch(r"[0-9a-f]{32}", str(profile.get("boot_nonce", "")))
        or profile.get("root") != dict(root_binding())):
        raise BoardError("document_upload_profile_unproved", "Restart the managed document service to prove this host's private upload lock profile", status_code=503)
    path = canonical_workspace_root(settings.workspace_dir)/SOURCE_PREFIX/".upload-profile.lock"
    try:
        parent, _leaf = _open_input_artifact_parent(path, create=False)
        try: facts = os.fstat(parent)
        finally: os.close(parent)
        if facts.st_dev != profile["directory_device"] or facts.st_ino != profile["directory_inode"]:
            raise OSError("profile directory changed")
    except (OSError, KeyError):
        raise BoardError("document_upload_profile_unproved", "The original private filesystem profile changed; restart the document service", status_code=503) from None


def metadata(row):
    try:
        value = json.loads(row.document_metadata_json)
        expected = {CAPABILITY: "document-pair.v1", SOURCE_CAPABILITY: "document-source.v1",
            BUILD_CAPABILITY: "document-build.v1"}.get(row.capability_id)
        if expected is None or value["schema"] != expected or len(row.document_metadata_json) > 8192:
            raise ValueError()
        return value
    except (TypeError, ValueError, KeyError):
        raise BoardError("document_pair_metadata_invalid", "The private pair needs reconciliation", status_code=409) from None


def active_generation(row):
    value = metadata(row)
    if row.capability_id == BUILD_CAPABILITY:
        if value["phase"] == "deleted":
            return False
        return (value["phase"] not in {"completed", "degraded"} or bool(value.get("live_writer"))
            or (value.get("reap") or {}).get("wait_reaped") is not True or not value.get("output"))
    return value["phase"] != "sealed"


async def check_quota(db, owner, charge):
    rows = list((await db.scalars(select(WorkBoardInputArtifact).where(
        WorkBoardInputArtifact.document_reserved_bytes > 0))).all())
    own = [row for row in rows if row.owner_principal_id == owner.principal_id]
    # Validate every charged family before summing; unknown charge is never free.
    charges = {CAPABILITY: CHARGE, SOURCE_CAPABILITY: 32*1024*1024, BUILD_CAPABILITY: BUILD_CHARGE}
    if any(row.document_reserved_bytes != charges.get(row.capability_id) for row in rows):
        raise BoardError("document_pair_quota_invalid", "Reconcile the unknown private document charge", status_code=409)
    active = [row for row in rows if active_generation(row)]
    if (sum(row.document_reserved_bytes for row in rows)+charge > 256*1024*1024
        or sum(row.document_reserved_bytes for row in own)+charge > 64*1024*1024
        or len(active) >= 16
        or sum(row.owner_principal_id == owner.principal_id for row in active) >= 2):
        raise BoardError("document_pair_quota_full", "Reconcile retained private documents before reserving more", status_code=409)


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


async def owned(db, owner, identifier, *, revision=None, capability=CAPABILITY):
    row = await db.scalar(select(WorkBoardInputArtifact).where(
        WorkBoardInputArtifact.artifact_id == identifier,
        WorkBoardInputArtifact.owner_principal_id == owner.principal_id,
        WorkBoardInputArtifact.owner_session_id == owner.session_id,
        WorkBoardInputArtifact.capability_id == capability).execution_options(populate_existing=True))
    if row is None: raise BoardError("document_pair_not_found", "The private pair is unavailable", status_code=404)
    if revision is not None and row.revision != revision:
        raise BoardError("document_pair_revision_conflict", "Read the current private pair before retry", status_code=409)
    return row, metadata(row)


async def authority(db, owner, row, value, staged_root, *, ingest=False):
    if value["root"] != staged_root:
        raise BoardError("document_pair_root_changed", "The original workspace changed", status_code=409)
    # Long-lived readers must not adopt against a cached pre-execution Goal.
    from src.db.models import Goal
    await db.get(Goal, row.goal_id, populate_existing=True)
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
    general = hasattr(request, "format")
    capability = SOURCE_CAPABILITY if general else CAPABILITY
    charge = 32 * 1024 * 1024 if general else CHARGE
    if not general and request.csv.size_bytes > 1024 * 1024:
        raise BoardError("document_csv_size_exceeded", "CSV must be at most 1 MiB", status_code=422)
    staged_root = dict(root_binding())
    family = "document-source" if general else "document-pair"
    identifier = str(uuid.uuid5(uuid.NAMESPACE_URL, f"seraph:{family}:{owner.principal_id}:{owner.session_id}:{request.goal_id}:{request.goal_revision}:{request.idempotency_key}"))
    inputs = ({"schema_version": 1, "artifact_ref": "document-source:" + identifier,
        "format": request.format, "source": request.source.model_dump(), "no_learning": True} if general else
        DocumentCompareInput(schema_version=1, operation=request.operation,
            pair_ref="document-pair:" + identifier, pdf=request.pdf, csv=request.csv, no_learning=True).model_dump())
    payload = canonical({"schema_version": 1, "capability_id": capability, "input": inputs})
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
        capability_id=capability, capability_version="1", idempotency_key=request.idempotency_key,
        payload_sha256=digest, typed_input_ref=f"workspace-json:{INPUT_ARTIFACT_ROOT}/{identifier}-{digest}.json",
        size_bytes=len(payload), state="pending", created_at=stamp, expires_at=stamp+timedelta(hours=24),
        document_reserved_bytes=charge)
    value = {"schema": family + ".v1", "root": staged_root, "input": inputs,
        "phase": "reserved", "generation": 1, "live_writer": None, "sources": {},
        "ingest_deadline": (stamp+timedelta(seconds=300)).isoformat()}
    goal, budget = await authority(db, owner, row, value, staged_root)
    deadlines = [row.expires_at, stamp+timedelta(seconds=300)] + [utc(v) for v in (goal.due_date, budget.period_expires_at) if v is not None]
    value["ingest_deadline"] = min(deadlines).isoformat()
    row.expires_at = min([row.expires_at] + [utc(v) for v in (goal.due_date, budget.period_expires_at) if v is not None])
    await check_quota(db, owner, charge)
    row.document_metadata_json = canonical(value).decode(); db.add(row)
    await db.flush(); await db.commit()
    return projection(row)


def source_path(row, value, slot):
    if row.capability_id == BUILD_CAPABILITY:
        if slot not in {"spec", "selection", "editable", "pdf", "output-manifest"}:
            raise ValueError("fixed build slot required")
        return canonical_workspace_root(settings.workspace_dir) / BUILD_PREFIX / row.artifact_id / f"g{value['generation']}-{slot}.fernet"
    general = row.capability_id == SOURCE_CAPABILITY
    if slot not in ({"source", "evidence"} if general else {"pdf", "csv"}): raise ValueError("fixed document slot required")
    return canonical_workspace_root(settings.workspace_dir) / (SOURCE_PREFIX if general else PREFIX) / row.artifact_id / f"g{value['generation']}-{slot}.fernet"


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
        fd = os.open(leaf, os.O_RDONLY|os.O_NONBLOCK|getattr(os,"O_NOFOLLOW",0), dir_fd=parent)
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


def upload_lease(row, value, slot, token, *, profile=None):
    """Own the same private inode before publishing canonical writer ownership."""
    path = source_path(row, value, slot)
    parent, _leaf = _open_input_artifact_parent(path, create=True)
    name = f"g{value['generation']}-{slot}.upload-lock"
    binding = {"schema": "document-upload-lease.v1", "artifact_id": row.artifact_id,
        "owner_principal_id": row.owner_principal_id, "owner_session_id": row.owner_session_id,
        "generation": value["generation"], "slot": slot, "nonce": token,
        "source_digest": value["input"][slot]["sha256"], "root_digest": sha256(canonical(value["root"]))}
    fd = -1
    try:
        if row.capability_id == SOURCE_CAPABILITY and os.fstat(parent).st_dev != profile["directory_device"]:
            raise OSError("upload filesystem differs from proved profile")
        created = True
        try:
            fd = os.open(name, os.O_RDWR|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600, dir_fd=parent)
        except FileExistsError:
            created = False
            fd = os.open(name, os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK, dir_fd=parent)
        fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
        if not created:
            facts = os.fstat(fd)
            if value.get("live_writer") or not _private_input_file_metadata(facts) or not 0 < facts.st_size <= 2048:
                raise OSError("orphan lease reuse unproven")
            previous = json.loads(os.read(fd, 2049))
            if (set(previous) != set(binding) or not re.fullmatch(r"[0-9a-f]{32}", str(previous.get("nonce", "")))
                or {**previous, "nonce": token} != binding):
                raise OSError("orphan lease binding changed")
            named = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if named.st_dev != facts.st_dev or named.st_ino != facts.st_ino:
                raise OSError("orphan lease name changed")
            os.lseek(fd, 0, os.SEEK_SET); os.ftruncate(fd, 0)
        raw = canonical(binding)
        if os.write(fd, raw) != len(raw):
            raise OSError("upload lease write incomplete")
        os.fsync(fd); os.fsync(parent)
        probe = os.open(name, os.O_RDWR|os.O_NOFOLLOW, dir_fd=parent)
        try:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                raise OSError("filesystem upload exclusion unavailable")
        finally:
            os.close(probe)
        facts = os.fstat(fd)
        if not _private_input_file_metadata(facts):
            raise OSError("upload lease metadata invalid")
        return fd, {"file": name, "device": facts.st_dev, "inode": facts.st_ino, "binding": binding}
    except BaseException:
        if fd >= 0: os.close(fd)
        raise
    finally:
        os.close(parent)


def original_upload_lease(row, value):
    """Exclusive acquisition proves quiescence only under the exact lease protocol."""
    lease = value["upload_binding"]
    writer = value["live_writer"]
    expected = {"schema": "document-upload-lease.v1", "artifact_id": row.artifact_id,
        "owner_principal_id": row.owner_principal_id, "owner_session_id": row.owner_session_id,
        "generation": value["generation"], "slot": writer["slot"], "nonce": writer["token"],
        "source_digest": value["input"][writer["slot"]]["sha256"], "root_digest": sha256(canonical(value["root"]))}
    name = f"g{value['generation']}-{writer['slot']}.upload-lock"
    if lease["binding"] != expected or lease["file"] != name or not re.fullmatch(r"g[12]-(source|pdf|csv)\.upload-lock", name):
        raise ValueError("upload lease binding changed")
    parent, _leaf = _open_input_artifact_parent(source_path(row, value, writer["slot"]), create=False)
    fd = -1
    try:
        fd = os.open(name, os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK, dir_fd=parent)
        facts = os.fstat(fd)
        if (not _private_input_file_metadata(facts) or facts.st_size != len(canonical(expected))
            or facts.st_dev != lease["device"] or facts.st_ino != lease["inode"]):
            raise ValueError("upload lease inode changed")
        fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
        if os.read(fd, 2049) != canonical(expected):
            raise ValueError("upload lease content changed")
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if current.st_dev != facts.st_dev or current.st_ino != facts.st_ino:
            raise ValueError("upload lease name changed")
        return fd
    except BaseException:
        if fd >= 0: os.close(fd)
        raise
    finally:
        os.close(parent)


async def reconcile_upload(db, owner, identifier, revision, *, capability=SOURCE_CAPABILITY):
    row, value = await owned(db, owner, identifier, revision=revision, capability=capability)
    if value["root"] != dict(root_binding()):
        raise BoardError("document_pair_root_changed", "Cleanup uses the original workspace", status_code=409)
    if not value.get("live_writer") or value["live_writer"].get("slot") not in {"source", "pdf", "csv"}:
        raise BoardError("document_original_upload_required", "There is no original upload writer to reconcile", status_code=409)
    try:
        lease_fd = original_upload_lease(row, value)
    except (OSError, ValueError, KeyError, TypeError):
        raise BoardError("document_upload_quiescence_unknown", "The exact original upload lease is held or unavailable; retain capacity", status_code=409) from None
    try:
        await _begin_immediate(db)
        fresh, current = await owned(db, owner, identifier, revision=revision, capability=capability)
        if current != value:
            raise BoardError("document_upload_fence_changed", "The original upload binding changed", status_code=409)
        current["live_writer"] = None
        current["phase"] = "cleanup_required"
        current["reason"] = "document_original_upload_closed_cleanup_required"
        current["upload_cleanup"] = {"binding_digest": sha256(canonical(value["upload_binding"])), "positive_exclusive_lock": True}
        fresh.document_metadata_json = canonical(current).decode(); fresh.revision += 1
        await db.commit()
        return projection(fresh)
    finally:
        os.close(lease_fd)


async def acquire_upload(db, owner, identifier, revision, slot, *, capability=CAPABILITY, upload_profile=None):
    # Lifecycle proof is checked before acquiring the canonical SQL writer.
    if capability == SOURCE_CAPABILITY: validate_upload_profile(upload_profile)
    staged_root=dict(root_binding()); token=uuid.uuid4().hex
    await _begin_immediate(db)
    row,value=await owned(db,owner,identifier,revision=revision,capability=capability)
    await authority(db,owner,row,value,staged_root,ingest=True)
    if slot not in ({"source"} if capability == SOURCE_CAPABILITY else {"pdf","csv"}) or value["phase"] not in {"reserved","uploading"} or slot in value["sources"]:
        raise BoardError("document_pair_slot_unavailable", "This source slot cannot be overwritten", status_code=409)
    rows=list((await db.scalars(select(WorkBoardInputArtifact).where(WorkBoardInputArtifact.document_reserved_bytes>0))).all())
    active=[r for r in rows if metadata(r).get("live_writer")]
    if value.get("live_writer") or len(active)>=2 or any(r.owner_principal_id==owner.principal_id for r in active):
        raise BoardError("document_upload_writer_busy", "A private upload writer is already held", status_code=409)
    try:
        lease_fd, lease = upload_lease(row, value, slot, token, profile=upload_profile)
    except (OSError, ValueError, KeyError, TypeError):
        raise BoardError("document_upload_lock_unavailable", "Private filesystem exclusion is unavailable; upload remains blocked", status_code=503) from None
    try:
        value["phase"]="uploading"; value["live_writer"]={"token":token,"slot":slot}
        value["upload_binding"] = lease
        row.document_metadata_json=canonical(value).decode(); row.revision+=1
        await db.commit()
        return row,value,token,lease_fd
    except BaseException:
        os.close(lease_fd)
        raise


async def upload(db,owner,identifier,revision,slot,stream,*,capability=CAPABILITY,upload_profile=None):
    row,value,token,lease_fd=await acquire_upload(db,owner,identifier,revision,slot,capability=capability,upload_profile=upload_profile)
    descriptor=value["input"][slot]; data=bytearray(); result=None; reason=None
    request_task = asyncio.current_task()
    fresh = None
    try:
        remaining=(utc(__import__('datetime').datetime.fromisoformat(value['ingest_deadline']))-now()).total_seconds()
        async with asyncio.timeout(max(0,remaining)):
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
    except BaseException:
        reason="document_upload_failed_cleanup_required"
        raise
    finally:
        # No detached I/O exists: stream consumption is awaited and private I/O
        # is synchronous. Cancellation must also finish closing its iterator.
        try:
            async def settle_upload():
                nonlocal fresh, reason
                async with asyncio.timeout(10):
                    close = getattr(stream, "aclose", None)
                    if close is not None: await close()
                    staged_root = dict(root_binding())
                    await _begin_immediate(db)
                    fresh,current=await owned(db,owner,identifier,capability=capability)
                    if current.get("live_writer")!={"token":token,"slot":slot} or current.get("upload_binding") != value["upload_binding"]:
                        raise BoardError("document_upload_fence_changed", "The original writer needs reconciliation",status_code=409)
                    current["live_writer"]=None
                    if request_task.cancelling():
                        reason = "document_upload_failed_cleanup_required"
                    if result is not None and reason is None:
                        from src.auth.service import AuthFailure
                        try:
                            await authority(db,owner,fresh,current,staged_root,ingest=True)
                            if capability == SOURCE_CAPABILITY:
                                from src.work_board.documents import current_source_root
                                await current_source_root(db, owner, None)
                        except (BoardError, AuthFailure):
                            reason = "document_upload_authority_changed_cleanup_required"
                        else:
                            current["sources"][slot]=result
                    if reason is not None:
                        current["phase"]="cleanup_required"; current["reason"]=reason
                    fresh.document_metadata_json=canonical(current).decode(); fresh.revision+=1
                    await db.commit()
            from src.work_board.documents import shield_positive_cleanup
            await shield_positive_cleanup(settle_upload())
        finally:
            os.close(lease_fd)
    if reason: raise BoardError(reason,"The private upload failed; its generation remains charged",status_code=409)
    return projection(fresh)


async def complete(db,owner,identifier,revision,*,capability=CAPABILITY):
    row,value=await owned(db,owner,identifier,revision=revision,capability=capability)
    staged_root=dict(root_binding()); await authority(db,owner,row,value,staged_root,ingest=True)
    if value["phase"]=="sealed": return projection(row)
    slots = ("source",) if capability == SOURCE_CAPABILITY else ("pdf", "csv")
    if value.get("live_writer") or set(value["sources"])!=set(slots):
        raise BoardError("document_pair_incomplete", "Both immutable sources must finish and read back",status_code=409)
    for slot in slots:
        raw=read_private(source_path(row,value,slot),value["sources"][slot],maximum=value["input"][slot]["size_bytes"])
        if sha256(raw)!=value["input"][slot]["sha256"]: raise BoardError("document_source_changed","The private source changed",status_code=409)
    payload=canonical({"schema_version":1,"capability_id":capability,"input":value["input"]})
    _write_payload(_payload_path(row),payload)
    _safe_file_bytes(_payload_path(row),expected_digest=row.payload_sha256,expected_size=row.size_bytes)
    await _begin_immediate(db)
    fresh,current=await owned(db,owner,identifier,revision=revision,capability=capability)
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


def cleanup_generation(row,value):
    """Remove a positively closed, unbound generation through held handles."""
    general = row.capability_id == SOURCE_CAPABILITY
    path=source_path(row,value,"source" if general else "pdf")
    try:parent,_leaf=_open_input_artifact_parent(path,create=False)
    except FileNotFoundError:parent=-1
    if parent>=0:
        held_leases = []
        try:
            names=os.listdir(parent)
            if len(names)>8:raise OSError("document cleanup fragment bound")
            slots = "source|evidence" if general else "pdf|csv"
            expected=re.compile(rf"^g{value['generation']}-({slots})\.fernet$")
            fragment=re.compile(rf"^\.g{value['generation']}-({slots})\.fernet\.[0-9a-f]{{32}}\.pending$")
            witness = re.compile(r"^[0-9a-f]{32}\.witness\.json$") if general else None
            checked=[]
            for name in names:
                lease = re.compile(rf"^g{value['generation']}-({slots})\.upload-lock$")
                if not expected.fullmatch(name) and not fragment.fullmatch(name) and not lease.fullmatch(name) and not (witness and witness.fullmatch(name)):raise OSError("document cleanup foreign generation")
                fd=os.open(name,os.O_RDONLY|getattr(os,"O_NOFOLLOW",0),dir_fd=parent)
                try:
                    if lease.fullmatch(name):
                        fcntl.flock(fd, fcntl.LOCK_EX|fcntl.LOCK_NB)
                        held_leases.append(fd)
                    stat=os.fstat(fd)
                    if not _private_input_file_metadata(stat) or stat.st_size>(34 if general else 6)*1024*1024:
                        raise OSError("document cleanup file metadata")
                    checked.append((name,stat))
                finally:
                    if fd not in held_leases: os.close(fd)
            # Final name identity checks precede unlink, after actual writers
            # are positively closed and the canonical tombstone is durable.
            for name,original in checked:
                current=os.stat(name,dir_fd=parent,follow_symlinks=False)
                if any(getattr(current,k)!=getattr(original,k) for k in ("st_dev","st_ino","st_uid","st_mode","st_size","st_nlink")):
                    raise OSError("document cleanup name replaced")
                os.unlink(name,dir_fd=parent)
            os.fsync(parent)
            if os.listdir(parent):raise OSError("document cleanup directory not empty")
        finally:
            for descriptor in held_leases: os.close(descriptor)
            os.close(parent)
    from src.work_board.input_artifacts import _cleanup_private_input_file, _InputArtifactCleanupUnverified
    try:
        _cleanup_private_input_file(_payload_path(row),expected_digest=row.payload_sha256,expected_size=row.size_bytes)
    except _InputArtifactCleanupUnverified as exc:
        if exc.reason!="cleanup_target_missing":raise


async def reset_unbound(db,owner,identifier,revision,*,retry,capability=CAPABILITY):
    staged_root=dict(root_binding());await _begin_immediate(db)
    row,value=await owned(db,owner,identifier,revision=revision,capability=capability)
    if value["root"]!=staged_root:raise BoardError("document_pair_root_changed","Cleanup must use the exact original workspace",status_code=409)
    if row.bound_task_id:raise BoardError("document_bound_pair_retained","The task owns this immutable pair; cancel or recover its original attempt",status_code=409)
    if value.get("live_writer"):
        raise BoardError("document_upload_quiescence_unknown","The original writer has no positive quiescence receipt; capacity remains held",status_code=409)
    if retry:
        await authority(db,owner,row,value,staged_root,ingest=True)
        if value["generation"]>=2:raise BoardError("document_upload_retry_limit","The two original upload generations are exhausted",status_code=409)
    value["phase"]="cleanup_tombstone";value["reason"]="document_pair_cleanup_pending"
    row.document_metadata_json=canonical(value).decode();row.metadata_digest=None;row.revision+=1
    tombstone_revision=row.revision;await db.commit()
    try:cleanup_generation(row,value)
    except OSError as exc:
        raise BoardError("document_pair_cleanup_required","The exact private generation still requires cleanup; quota is held",status_code=409) from exc
    await _begin_immediate(db)
    fresh,current=await owned(db,owner,identifier,revision=tombstone_revision,capability=capability)
    if current!=value:raise BoardError("document_pair_revision_conflict","The cleanup tombstone changed",status_code=409)
    if retry:
        await authority(db,owner,fresh,current,staged_root,ingest=True)
        current.update({"generation":current["generation"]+1,"phase":"reserved","sources":{},"live_writer":None,"reason":None})
    else:
        current.update({"phase":"deleted","sources":{},"reason":None});fresh.state="deleted";fresh.document_reserved_bytes=0
    fresh.document_metadata_json=canonical(current).decode();fresh.revision+=1
    await db.commit();return projection(fresh)
