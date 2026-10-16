"""One original native read's finite private artifact and audit operations.

The source is exclusively the invocation's own sealed read result, in its
retained non-private projection: memory text never rests in the job row or the
artifact file, so a later forget leaves no copy to scrub (#1100). Operations
run in fixed order stage -> adopt -> read -> audit.append, each exactly once.
Stage records its physical intent before writing; an interrupted intent stays
visibly unresolved and is never re-executed. Every operation rechecks the
original lease, fence, deadline, composition owners and authority.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json

from sqlalchemy.exc import OperationalError, TimeoutError as PoolTimeoutError

from .contracts import succeeded
from .dispatch import (READ_JOB_KIND, NativeServiceBlocked, _append_receipt, _canonical, _digest, _writer,
                       cas_run, checkpoint_receipt, operation_ref, read_context, sealed_read_digest,
                       sealed_read_result, transient_storage_error)
from .ownership import ARTIFACT_CHILDREN

ARTIFACT_TYPE = "cordis_service_read"
ARTIFACT_PREFIX = "artifacts/runtime-service-read/"
AUDIT_EVENT_TYPE = "runtime_service_observed"
AUDIT_SUMMARY = "Native service read verified"
MAX_ARTIFACT_BYTES = 65536


def artifact_bytes(context: dict) -> bytes:
    content = _canonical(sealed_read_result(context)["projection"]).encode("utf-8")
    if not 1 <= len(content) <= MAX_ARTIFACT_BYTES:
        raise NativeServiceBlocked("native_artifact_source_unsupported")
    return content


def artifact_path(run, content_digest: str) -> str:
    key = hashlib.sha256(run.run_identity.encode("utf-8")).hexdigest()
    return f"{ARTIFACT_PREFIX}{key}-{content_digest}.json"


def _workspace_file(reference: str):
    from config.settings import settings
    from src.workspace import canonical_workspace_root
    return canonical_workspace_root(settings.workspace_dir) / reference


def readback(run, context: dict) -> tuple[str, bytes, str]:
    """Literal private-file readback of the exact sealed source bytes."""
    from src.work_board.input_artifacts import _safe_file_bytes
    from src.work_board.repository import BoardError
    content = artifact_bytes(context)
    digest = hashlib.sha256(content).hexdigest()
    reference = artifact_path(run, digest)
    try:
        actual = _safe_file_bytes(_workspace_file(reference), expected_digest=digest, expected_size=len(content))
    except (BoardError, OSError) as exc:
        raise NativeServiceBlocked("native_artifact_readback_unproven") from exc
    if actual != content:
        raise NativeServiceBlocked("native_artifact_readback_changed")
    return reference, actual, digest


def audit_details(run, context: dict) -> dict:
    ops = context["operations"]
    return {"original_read_method": context["candidate"]["method"],
            "sealed_result_digest": sealed_read_result(context)["result_digest"],
            "artifact_ref": ops[0]["artifact_ref"],
            "content_digest": hashlib.sha256(artifact_bytes(context)).hexdigest(),
            "no_learning": True}


def expected_wire(run, context: dict, method: str) -> dict:
    """The only acceptable plugin request for the next operation."""
    if method == "artifacts.stage":
        # The read journey stages only Python's sealed bytes; the child sends none.
        return {"request_ref": operation_ref(run.run_identity, method), "content": None, "content_digest": None}
    if method == "artifacts.adopt":
        return {"request_ref": operation_ref(run.run_identity, method)}
    if method == "artifacts.read":
        return {"artifact_ref": context["operations"][1]["artifact_ref"], "max_bytes": MAX_ARTIFACT_BYTES, "offset": 0}
    return {"event_ref": operation_ref(run.run_identity, method)}


def next_wire_inputs(run) -> tuple[str, dict]:
    """Python owner helper: the next method and its exact server-derived inputs."""
    context = read_context(run)
    ops = context["operations"]
    if len(ops) >= len(ARTIFACT_CHILDREN) or (ops and ops[-1]["state"] != "settled"):
        raise NativeServiceBlocked("native_artifact_operation_unresolved")
    method = ARTIFACT_CHILDREN[len(ops)]
    return method, expected_wire(run, context, method)


def _artifact_grants(run) -> None:
    grants = set(json.loads(run.declared_authority_json).get("grants", []))
    if not {"capability_execute", "artifact_transfer"} <= grants:
        raise NativeServiceBlocked("native_artifact_grants_required")


async def _current(dispatcher, db, scope, method):
    await _writer(db)
    run, binding, _ = await dispatcher.current(db, scope, method)
    if run.job_kind != READ_JOB_KIND or binding.native_branch != "artifact":
        raise NativeServiceBlocked("native_artifact_branch_required")
    _artifact_grants(run)
    context = read_context(run)
    sealed_read_result(context)
    return run, context


def _effect_receipt(run, *, request_ref, path, digest, status, readback_ref=None):
    receipt = {"effect_id": request_ref, "receipt_kind": "readback" if readback_ref else "effect",
               "effect_type": "cordis_read_artifact", "target_path": path, "target_digest": digest,
               "status": status, "content_sha256": digest,
               "details": {"no_learning": True, "verified": bool(readback_ref)},
               "recorded_at": datetime.now(timezone.utc).isoformat(), "fencing_token": run.fencing_token}
    if readback_ref:
        receipt["readback_id"] = readback_ref
        receipt["verified_at"] = receipt["recorded_at"]
    return receipt


async def dispatch_artifact_operation(dispatcher, frame, payload, scope):
    from src.db.models import AuditEvent
    from src.work_board.input_artifacts import _write_payload
    from src.work_board.repository import BoardError
    method, call_ref = frame["method"], frame["request_id"]
    index = ARTIFACT_CHILDREN.index(method)
    async with dispatcher.jobs._session() as db:
        run, context = await _current(dispatcher, db, scope, method)
        ops = context["operations"]
        if ops and ops[-1]["state"] != "settled":
            raise NativeServiceBlocked("native_artifact_operation_unresolved")
        if len(ops) != index:
            raise NativeServiceBlocked("native_artifact_operation_replay_denied")
        if payload != expected_wire(run, context, method):
            raise NativeServiceBlocked("native_artifact_wire_changed")
        content = artifact_bytes(context)
        digest = hashlib.sha256(content).hexdigest()
        path = artifact_path(run, digest)
        request_ref = operation_ref(run.run_identity, method)
        if method == "artifacts.stage":
            # Physical intent commits before the file effect.
            ops.append({"method": method, "request_ref": request_ref, "state": "intent", "call_ref": call_ref,
                        "artifact_ref": None})
            intent = _effect_receipt(run, request_ref=request_ref, path=path, digest=digest, status="intent")
            await cas_run(db, run, checkpoint_context_json=_canonical(context),
                          effect_receipts_json=_append_receipt(run.effect_receipts_json, intent, key="effect_id"))
            await db.commit()
    if method == "artifacts.stage":
        try:
            _write_payload(_workspace_file(path), content)
        except (OSError, BoardError) as exc:
            # The committed intent stays unresolved; a new call cannot replay it.
            raise NativeServiceBlocked("native_artifact_owner_operation_unproven") from exc
        try:
            async with dispatcher.jobs._session() as db:
                run, context = await _current(dispatcher, db, scope, method)
                ops = context["operations"]
                if (len(ops) != 1 or ops[0]["state"] != "intent" or ops[0]["call_ref"] != call_ref
                        or ops[0]["request_ref"] != request_ref):
                    raise NativeServiceBlocked("native_artifact_operation_changed")
                reference, actual, digest = readback(run, context)
                from src.artifacts.registry import build_artifact_record
                record = build_artifact_record(file_path=reference, artifact_type=ARTIFACT_TYPE, producer=run.job_kind,
                                               run_id=run.run_identity, session_id=run.session_id, content=actual)
                artifact_receipt = {key: record[key] for key in ("artifact_id", "artifact_type", "file_path", "producer",
                                                                 "content_sha256", "size_bytes", "exists")}
                artifact_receipt["recorded_at"] = datetime.now(timezone.utc).isoformat()
                effects = [item for item in json.loads(run.effect_receipts_json or "[]")
                           if not (isinstance(item, dict) and item.get("effect_id") == request_ref)]
                effects.append(_effect_receipt(run, request_ref=request_ref, path=reference, digest=digest,
                                               status="succeeded", readback_ref=request_ref + ":readback"))
                result = succeeded(method, {"artifact_ref": record["artifact_id"], "digest": digest,
                                            "size_bytes": len(actual)})
                # Seal Python's own result; the owner compares the child's echo to it.
                ops[0].update(state="settled", artifact_ref=record["artifact_id"], result_digest=_digest(result))
                await cas_run(db, run, checkpoint_context_json=_canonical(context), effect_receipts_json=_canonical(effects),
                              artifact_receipts_json=_append_receipt(run.artifact_receipts_json, artifact_receipt,
                                                                     key="artifact_id"))
                await db.commit()
                return result
        except (OperationalError, PoolTimeoutError) as exc:
            # The file already exists: storage contention while settling is an
            # unproven physical effect, never harmless busy contention. Other
            # storage faults still fence the boot.
            if not transient_storage_error(exc):
                raise
            raise NativeServiceBlocked("native_artifact_owner_operation_unproven") from exc
    async with dispatcher.jobs._session() as db:
        run, context = await _current(dispatcher, db, scope, method)
        ops = context["operations"]
        if len(ops) != index or (ops and ops[-1]["state"] != "settled"):
            raise NativeServiceBlocked("native_artifact_operation_replay_denied")
        reference, actual, digest = readback(run, context)
        artifact_ref = ops[0]["artifact_ref"]
        if not any(isinstance(item, dict) and item.get("artifact_id") == artifact_ref
                   and item.get("content_sha256") == digest and item.get("file_path") == reference
                   for item in json.loads(run.artifact_receipts_json or "[]")):
            raise NativeServiceBlocked("native_artifact_canonical_receipt_missing")
        values = {}
        if method == "artifacts.adopt":
            value = {"artifact_ref": artifact_ref, "digest": digest, "size_bytes": len(actual),
                     "receipt_ref": request_ref}
            values["checkpoint_receipts_json"] = _append_receipt(run.checkpoint_receipts_json, checkpoint_receipt(
                "cordis-read:adopt", {"artifact_ref": artifact_ref, "content_digest": digest, "no_learning": True},
                run.fencing_token), key="checkpoint_id")
        elif method == "artifacts.read":
            if payload["artifact_ref"] != artifact_ref or len(actual) > payload["max_bytes"]:
                raise NativeServiceBlocked("native_artifact_read_scope_changed")
            value = {"artifact_ref": artifact_ref, "digest": digest, "size_bytes": len(actual),
                     "content": actual.decode("utf-8")}
        else:
            event = AuditEvent(id=request_ref, session_id=run.session_id, actor="runtime",
                               event_type=AUDIT_EVENT_TYPE, summary=AUDIT_SUMMARY,
                               details_json=_canonical(audit_details(run, context)))
            db.add(event)
            await db.flush()
            # The canonical job CAS revision commits the AuditEvent with it.
            value = {"receipt_ref": request_ref, "revision": int(run.revision or 1) + 1}
        result = succeeded(method, value)
        ops.append({"method": method, "request_ref": request_ref, "state": "settled", "call_ref": call_ref,
                    "artifact_ref": artifact_ref, "result_digest": _digest(result)})
        revision = await cas_run(db, run, checkpoint_context_json=_canonical(context), **values)
        if method == "audit.append" and revision != value["revision"]:
            raise NativeServiceBlocked("native_audit_revision_changed")
        await db.commit()
        return result


async def assert_artifact_completion(db, run) -> dict:
    """Completion independently rereads the file and the actual audit row."""
    from src.db.models import AuditEvent
    context = read_context(run)
    ops = context["operations"]
    if [op["method"] for op in ops] != list(ARTIFACT_CHILDREN) or any(op["state"] != "settled" for op in ops):
        raise NativeServiceBlocked("native_artifact_completion_unproven")
    reference, actual, digest = readback(run, context)
    # The sealed artifacts.read result must equal Python's own current readback.
    expected_read = succeeded("artifacts.read", {"artifact_ref": ops[0]["artifact_ref"], "digest": digest,
                                                 "size_bytes": len(actual), "content": actual.decode("utf-8")})
    if ops[2].get("result_digest") != _digest(expected_read):
        raise NativeServiceBlocked("native_artifact_readback_changed")
    event = await db.get(AuditEvent, ops[3]["request_ref"], populate_existing=True)
    if (event is None or event.session_id != run.session_id or event.actor != "runtime"
            or event.event_type != AUDIT_EVENT_TYPE or event.summary != AUDIT_SUMMARY
            or json.loads(event.details_json or "null") != audit_details(run, context)):
        raise NativeServiceBlocked("native_artifact_audit_readback_unproven")
    sealed = sealed_read_result(context)
    if context["candidate"]["method"] == "memory.retrieve":
        from .read_admission import assert_memory_records_current
        await assert_memory_records_current(db, run, sealed["projection"]["value"])
    return {"artifact_ref": ops[0]["artifact_ref"], "file_path": reference, "content_digest": digest,
            "audit_event_ref": event.id, "result_digest": sealed["result_digest"],
            "retained_result": sealed["projection"], "artifact_readback": expected_read}


def assert_child_echo(run, method: str, observed: dict) -> None:
    """A child's answer to the parent call must equal Python's sealed result."""
    context = read_context(run)
    if method in ARTIFACT_CHILDREN:
        ops = context["operations"]
        sealed = ops[-1] if ops else None
        if (sealed is None or sealed["method"] != method or sealed["state"] != "settled"
                or sealed.get("result_digest") != _digest(observed)):
            raise NativeServiceBlocked("native_child_result_mismatch")
    elif context["result"] is None or sealed_read_digest(observed) != sealed_read_result(context)["result_digest"]:
        raise NativeServiceBlocked("native_child_result_mismatch")
