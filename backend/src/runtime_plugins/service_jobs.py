"""Python-owned admission, claim and original invocation for Cordis service calls.

The authenticated operator's live Root, the canonical job ledger and the
current composition inventory are the only authority sources. One idempotent
admission produces one durable job; an exact retry reuses it and never renews
a claimed attempt. The original invocation witness is a protected checkpoint
written under the job's own live lease before any crossing to the child.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from types import MappingProxyType

from sqlalchemy import select

from .dispatch import (CONTEXT_TAG, READ_JOB_KIND, TASK_JOB_KIND, WITNESS_PREFIX, NativeServiceBlocked,
                       NativeServiceDispatcher, OriginalServiceInvocation, _canonical, _checked_witness, _digest,
                       _ms, _stored_witness, _writer, cas_run, checkpoint_receipt)
from .ownership import RuntimeCompositionBinding, bind_invocation
from .read_admission import validate_read_candidate, wire_inputs

SERVICE_TIMEOUT = timedelta(seconds=30)
_KEY = re.compile(r"[\x21-\x7e]{1,128}\Z")


def _grants(principal) -> list[str]:
    return sorted(str(getattr(grant, "value", grant)) for grant in principal.grants)


def _jobs(jobs):
    if jobs is not None:
        return jobs
    from src.workflows.job_runtime import durable_job_repository
    return durable_job_repository


async def admit_service_job(*, operator, origin_method: str, native_branch: str, candidate: dict,
                            idempotency_key: str, reviewed_composition, goal_id: str | None = None,
                            goal_revision: int | None = None, jobs=None) -> tuple[dict, bool]:
    """Admit one Cordis-bound job; returns (job, replayed)."""
    from src.auth.ownership import _current_root
    from src.auth.service import authenticate_principal
    from src.db.engine import get_session
    from src.db.models import WorkflowRunState
    from src.workflows.job_runtime import DurableJobIdempotencyConflict, DurableJobIdentity, DurableJobSpec
    jobs = _jobs(jobs)
    if type(idempotency_key) is not str or not _KEY.fullmatch(idempotency_key):
        raise NativeServiceBlocked("native_idempotency_key_invalid")
    if native_branch == "artifact":
        candidate = validate_read_candidate(candidate)
        job_kind = READ_JOB_KIND
        if candidate["method"] != origin_method:
            raise NativeServiceBlocked("native_read_candidate_changed")
    elif native_branch == "workflow" and origin_method == "tasks.admit":
        if (type(candidate) is not dict or set(candidate) != {"schema_version", "checkpoint_refs"}
                or candidate["schema_version"] != 1 or type(candidate["checkpoint_refs"]) is not list
                or len(candidate["checkpoint_refs"]) > 8
                or any(type(item) is not str or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", item)
                       for item in candidate["checkpoint_refs"])):
            raise NativeServiceBlocked("native_task_candidate_invalid")
        job_kind = TASK_JOB_KIND
    else:
        raise NativeServiceBlocked("native_branch_unsupported")
    job_id = "cordis-svc:" + _digest({"root": operator.session_id, "kind": job_kind, "key": idempotency_key})
    for attempt in range(2):
        async with get_session() as db:
            await _current_root(db, operator)
            current = await authenticate_principal(operator.principal.principal_id, db=db)
            if current.session_id != operator.session_id or _grants(current.principal) != _grants(operator.principal):
                raise NativeServiceBlocked("native_original_policy_changed")
            binding = await bind_invocation(db, method=origin_method, native_branch=native_branch,
                                            goal_bound=goal_id is not None, reviewed_composition=reviewed_composition)
            prior = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id))
            deadline = datetime.now(timezone.utc) + SERVICE_TIMEOUT
            if prior is not None:
                # An exact retry carries the original cutoff; it never renews it.
                deadline = prior.deadline_at.replace(tzinfo=prior.deadline_at.tzinfo or timezone.utc)
        authority = {"principal": operator.principal.principal_id, "owner_kind": "user",
                     "session_id": operator.session_id, "grants": _grants(current.principal)}
        spec = DurableJobSpec(
            identity=DurableJobIdentity(job_id=job_id, owner_kind="user",
                                        owner_principal_id=operator.principal.principal_id, job_kind=job_kind,
                                        capability_version="1", idempotency_scope="cordis-service",
                                        idempotency_key=idempotency_key),
            inputs=candidate, session_id=operator.session_id, operator_session_id=operator.session_id,
            goal_id=goal_id, goal_revision=goal_revision, declared_authority=authority, deadline_at=deadline,
            max_attempts=1, composition_binding=binding,
            run_fingerprint=_digest({"candidate": candidate, "root": operator.session_id, "origin": origin_method,
                                     "branch": native_branch, "deadline": deadline.isoformat()}))
        try:
            job = await jobs.admit_job(spec)
        except DurableJobIdempotencyConflict:
            # A concurrent first admission won with its own cutoff; reread it
            # once and replay exactly. A genuine immutable conflict persists.
            if attempt or prior is not None:
                raise
            continue
        return job, (job.get("receipt") or {}).get("status") == "deduped"
    raise NativeServiceBlocked("native_admission_conflict")


async def claim_service_job(job: dict, *, host, jobs=None) -> tuple[str, int]:
    """Queue and claim the accepted original attempt; never a retry or renewal."""
    jobs = _jobs(jobs)
    if job["status"] != "accepted":
        raise NativeServiceBlocked("native_original_attempt_not_claimable")
    queued = await jobs.transition_job(job["job_id"], "queued", expected_revision=job["revision"])
    owner = "cordis-host:" + host.boot_nonce[:24]
    claimed = await jobs.claim_job(job["job_id"], owner=owner, lease_seconds=int(SERVICE_TIMEOUT.total_seconds()),
                                   expected_revision=queued["revision"])
    return owner, int(claimed["lease"]["fencing_token"])


async def open_invocation(*, job_id: str, owner: str, fence: int, host, admission_replayed: bool,
                          candidate: dict, jobs=None) -> OriginalServiceInvocation:
    """Write the protected original witness under the live lease, then capture it."""
    from .bridge import CordisHost
    jobs = _jobs(jobs)
    if type(host) is not CordisHost or not host.admitting or host.reviewed is None:
        raise NativeServiceBlocked("native_original_host_unavailable")
    claim_ref = hashlib.sha256(f"{job_id}:{owner}:{fence}".encode()).hexdigest()[:48]
    async with jobs._session() as db:
        await _writer(db)
        run = await jobs._fetch(db, job_id)
        binding = RuntimeCompositionBinding.from_json(run.composition_binding_json)
        if (run.status != "running" or run.lease_owner != owner or run.fencing_token != fence
                or binding.host_package_digest != host.reviewed.package_digest
                or binding.host_composition_digest != host.reviewed.composition_digest):
            raise NativeServiceBlocked("native_original_host_changed")
        witness = _checked_witness({
            "schema_version": 1, "invocation_ref": run.run_identity, "claim_ref": claim_ref,
            "checkpoint_id": WITNESS_PREFIX + claim_ref, "origin_method": binding.origin_method,
            "native_branch": binding.native_branch, "allowed_child_methods": list(binding.allowed_child_methods),
            "binding_digest": binding.binding_digest, "attempt_count": run.attempt_count, "lease_owner": owner,
            "fence": fence, "input_digest": run.input_digest, "authority_digest": run.authority_digest,
            "run_fingerprint": run.run_fingerprint, "original_deadline_at": _ms(run.deadline_at),
            "package_digest": host.reviewed.package_digest,
            "host_composition_digest": host.reviewed.composition_digest,
            "host_boot_nonce": host.boot_nonce, "admission_replayed": admission_replayed})
        scope = OriginalServiceInvocation(MappingProxyType(witness), binding, host.boot_nonce)
        # The witness must be written by a still-current original owner.
        await NativeServiceDispatcher(jobs=jobs).current_authority(db, run)
        from .ownership import validate_invocation
        await validate_invocation(db, binding)
        receipts = json.loads(run.checkpoint_receipts_json or "[]")
        if any(item.get("checkpoint_id", "").startswith(WITNESS_PREFIX) for item in receipts if isinstance(item, dict)):
            raise NativeServiceBlocked("native_original_invocation_replay_denied")
        receipts.append(checkpoint_receipt(witness["checkpoint_id"], witness, fence))
        from src.workflows.job_runtime import _digest as job_digest
        if job_digest(candidate) != run.input_digest:
            raise NativeServiceBlocked("native_candidate_changed")
        context = {"schema_version": 1, "context_tag": CONTEXT_TAG, "candidate": candidate,
                   "candidate_digest": run.input_digest,
                   "claim_ref": claim_ref, "result": None, "operations": []}
        await cas_run(db, run, checkpoint_receipts_json=_canonical(receipts),
                      checkpoint_context_json=_canonical(context))
        await db.commit()
    async with jobs._session() as db:
        if _stored_witness(await jobs._fetch(db, job_id), witness["checkpoint_id"]) != witness:
            raise NativeServiceBlocked("native_original_invocation_changed")
    return scope


async def verified_task_call(host, jobs, scope, method: str, payload: dict) -> dict:
    """Forward a task method and accept the child's answer only if the durable
    job row independently shows the same receipt/revision/state."""
    jobs = _jobs(jobs)
    result = await host.request_service(method, payload, original_scope=scope)
    if result["status"] != "succeeded":
        return result
    value = result["value"]
    job = await jobs.get_job(scope.witness["invocation_ref"])
    artifacts = [item["artifact_id"] for item in job["artifacts"] if item.get("artifact_id")]
    receipts = {item["checkpoint_id"] for item in job["checkpoints"]}
    if method == "tasks.admit":
        ok = (value["job_ref"] == job["job_id"] and value["replayed"] is scope.witness["admission_replayed"]
              and value["revision"] <= job["revision"])
    elif method == "tasks.inspect":
        ok = value == {"job_ref": job["job_id"], "revision": job["revision"], "state": job["status"],
                       "artifact_refs": artifacts}
    elif method == "tasks.checkpoint":
        ok = value["receipt_ref"] in receipts and value["revision"] <= job["revision"]
    elif method == "tasks.cancel":
        ok = value == {"job_ref": job["job_id"], "revision": job["revision"], "state": job["status"]} \
            and job["status"] == "cancelled"
    else:
        raise NativeServiceBlocked("native_task_method_not_verifiable")
    if not ok:
        raise NativeServiceBlocked("native_child_result_mismatch")
    return result


async def _block(jobs, scope, reason: str) -> None:
    from src.workflows.job_runtime import DurableJobError
    try:
        await jobs.transition_job(scope.witness["invocation_ref"], "blocked", owner=scope.witness["lease_owner"],
                                  fencing_token=scope.witness["fence"], reason=reason)
    except DurableJobError:
        # Lost original authority leaves canonical recovery state; never
        # renew it merely to report a block.
        pass


async def execute_native_read(*, operator, candidate: dict, idempotency_key: str, host=None, jobs=None) -> dict:
    """Temporary-state read -> job -> artifact -> readback, with explicit no_learning."""
    from .bridge import HostBlocked, cordis_host
    from .service_artifacts import assert_artifact_completion, assert_child_echo, next_wire_inputs
    host = host or cordis_host
    jobs = _jobs(jobs)
    if not host.admitting or host.reviewed is None:
        raise NativeServiceBlocked("native_read_host_unavailable")
    method = validate_read_candidate(candidate)["method"]
    job, replayed = await admit_service_job(operator=operator, origin_method=method, native_branch="artifact",
                                            candidate=candidate, idempotency_key=idempotency_key,
                                            reviewed_composition=host.reviewed, jobs=jobs)
    if job["status"] != "accepted":
        # An exact retry reuses original truth; it never re-executes.
        return {"job": job, "replayed": True, "memory_status": "no_learning"}
    owner, fence = await claim_service_job(job, host=host, jobs=jobs)
    scope = await open_invocation(job_id=job["job_id"], owner=owner, fence=fence, host=host,
                                  admission_replayed=replayed, candidate=candidate, jobs=jobs)
    try:
        # The child's answers are never authority: each must equal the result
        # Python itself sealed in the job's private context for that call.
        result = await host.request_service(method, wire_inputs(candidate, job["job_id"]), original_scope=scope)
        if result["status"] != "succeeded":
            raise NativeServiceBlocked(result["reason_code"])
        async with jobs._session() as db:
            assert_child_echo(await jobs._fetch(db, job["job_id"]), method, result)
        for _ in range(4):
            async with jobs._session() as db:
                artifact_method, inputs = next_wire_inputs(await jobs._fetch(db, job["job_id"]))
            observed = await host.request_service(artifact_method, inputs, original_scope=scope)
            if observed["status"] != "succeeded":
                raise NativeServiceBlocked(observed["reason_code"])
            async with jobs._session() as db:
                assert_child_echo(await jobs._fetch(db, job["job_id"]), artifact_method, observed)
        async with jobs._session() as db:
            from .dispatch import read_context
            sealed_digest = read_context(await jobs._fetch(db, job["job_id"]))["result"]["result_digest"]
        dispatcher = NativeServiceDispatcher(jobs=jobs)
        proof: dict = {}

        async def completion(db, _run):
            run, _, _ = await dispatcher.current(db, scope, method)
            proof.update(await assert_artifact_completion(db, run))

        completed = await jobs.transition_job(
            job["job_id"], "succeeded", owner=owner, fencing_token=fence,
            result={"no_learning": True, "memory_status": "no_learning", "method": method,
                    "sealed_result_digest": sealed_digest},
            result_summary="Native service read verified with explicit no_learning",
            terminal_authority_check=completion)
        # Return only Python-owned values: the sealed read and Python's own readback.
        return {"job": completed, "result": proof["result"], "artifact_readback": proof["artifact_readback"],
                "completion": proof, "replayed": False, "memory_status": "no_learning"}
    except (NativeServiceBlocked, HostBlocked) as exc:
        reason = getattr(exc, "reason_code", None) or "native_read_execution_blocked"
        await _block(jobs, scope, reason)
        raise NativeServiceBlocked(reason) from exc
