"""Authenticated bounded reads through the original native claim and stock host."""
from __future__ import annotations

from .dispatch import NativeServiceBlocked, capture_original_scope
from .read_admission import NativeServiceReadAdmission


async def execute_native_read(*, operator, admission, idempotency_key, owner_recheck):
    from src.db.engine import get_session
    from src.auth.ownership import _current_root
    from src.workflows.job_runtime import durable_job_repository as jobs
    from src.workflows.job_runtime import DurableJobError
    from src.auth.service import AuthFailure
    from src.work_board.repository import BoardError
    from .bridge import cordis_host as host, HostBlocked
    from .ownership import begin_native_writer
    from .read_journal import native_read_spec
    if type(admission) is not NativeServiceReadAdmission or not host.admitting or host.reviewed is None:
        raise NativeServiceBlocked("native_read_host_unavailable")
    reviewed, boot = host.reviewed, host.boot_nonce
    async def check(db, _run):
        await _current_root(db, operator)
        await owner_recheck(db, admission.candidate())
    async with get_session() as db:
        await begin_native_writer(db, owner="durable_jobs")
        await owner_recheck(db, admission.candidate())
        spec = await native_read_spec(db, admission=admission, operator=operator,
            reviewed_composition=reviewed, idempotency_key=idempotency_key)
        job = await jobs._admit_in_session(db, spec, native_read_admission=admission,
            native_read_host=host, native_read_host_boot_nonce=boot, admission_authority_check=check)
    # An exact retry reuses original truth. It never retries a claimed attempt
    # or releases an old private result using a fresh operator request.
    if job["status"] != "accepted":
        return {"job": job, "replayed": True, "memory_status": "no_learning"}
    claim = None
    try:
        await jobs.transition_job(job["job_id"], "queued", expected_revision=job["revision"])
        claim = await jobs.claim_service_job(job["job_id"], host=host,
            owner="native-read:" + boot[:24], lease_seconds=30, claim_authority_check=check)
        scope = capture_original_scope(claim, host)
        method = admission.candidate()["method"]
        result = await host.request_service(method, admission.wire_inputs(job["job_id"]), original_scope=scope)
        if result["status"] != "succeeded":
            raise NativeServiceBlocked(result["reason_code"])
        from .read_journal import ARTIFACT_METHODS, prepare_operation, validate_read_policy
        from .dispatch import NativeServiceDispatcher
        dispatcher = NativeServiceDispatcher(jobs=jobs)
        artifact_result = None
        for artifact_method in ARTIFACT_METHODS:
            async with jobs._session() as db:
                run, _, _ = await dispatcher._current_in_db(db, job["job_id"], artifact_method, scope)
                await validate_read_policy(db, run)
                operation = prepare_operation(db, run, artifact_method)
                await db.flush()
            observed = await host.request_service(artifact_method, operation.wire_inputs(), original_scope=scope)
            if observed["status"] != "succeeded":
                raise NativeServiceBlocked(observed["reason_code"])
            if artifact_method == "artifacts.read":
                artifact_result = observed
        completed = await jobs.complete_native_read(claim, result=result)
        return {"job": completed, "result": result, "artifact_readback": artifact_result,
            "replayed": False, "memory_status": "no_learning"}
    except (NativeServiceBlocked, HostBlocked, DurableJobError, AuthFailure, BoardError) as exc:
        reason = getattr(exc, "reason_code", None) or getattr(exc, "reason", None) or "native_read_execution_blocked"
        if claim is not None:
            witness = claim.checkpoint["payload"]
            try:
                await jobs.transition_job(job["job_id"], "blocked", owner=witness["lease_owner"],
                    fencing_token=witness["fencing_token"], reason=reason)
            except (DurableJobError, BoardError):
                # Lost original authority leaves canonical recovery state;
                # the ingress must not renew it merely to report a block.
                pass
        raise NativeServiceBlocked(reason) from exc


async def native_read_http(**kwargs):
    from fastapi import HTTPException
    from src.workflows.job_runtime import DurableJobError
    from src.auth.service import AuthFailure
    from .protocol import ProtocolError
    from .ownership import CompositionBindingError
    from src.work_board.repository import BoardError
    try:
        return await execute_native_read(**kwargs)
    except (NativeServiceBlocked, DurableJobError, AuthFailure, ProtocolError, CompositionBindingError, BoardError) as exc:
        reason = getattr(exc, "reason_code", None) or "native_read_original_unavailable"
        raise HTTPException(status_code=409, detail={"code": reason}) from exc
