"""Two deterministic CPU leaves behind existing durable Work execution.

Quoted source data is never interpreted as instructions. This module has no
model, HTTP client, credential reader or subprocess dependency.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping

from config.settings import settings
from src.db.models import WorkBoardAttempt, WorkBoardTask
from src.work_board.pipeline_contracts import (
    CPU_KINDS, DOSSIER, REPORT, EvidenceConsumerInput, MAX_OUTPUT_BYTES,
    canonical_bytes, digest,
)
from src.workflows.job_runtime import DurableJobIdentity, DurableJobSpec
from src.workspace import canonical_workspace_root


def job_id(task: WorkBoardTask, attempt: WorkBoardAttempt) -> str:
    return f"evidence-cpu:{task.task_id}:{attempt.attempt_id}"


def spec_for(task: WorkBoardTask, attempt: WorkBoardAttempt, inputs: Mapping[str, Any], *, deadline: datetime) -> DurableJobSpec:
    if task.capability_id not in CPU_KINDS:
        raise ValueError("unregistered evidence CPU capability")
    model = EvidenceConsumerInput.model_validate(dict(inputs))
    handoffs = json.loads(attempt.parent_handoff_context_json)
    if not isinstance(handoffs, list) or len(handoffs) != 1 or digest(handoffs) != attempt.parent_handoff_digest:
        raise ValueError("evidence CPU handoff binding invalid")
    safe_inputs = {"input": model.model_dump(mode="json"), "parent_handoff_context": handoffs, "parent_handoff_digest": attempt.parent_handoff_digest}
    authority = {
        "principal": task.owner_principal_id, "owner_kind": "user",
        "session_id": task.owner_session_id, "goal_id": task.goal_id,
        "goal_revision": task.goal_revision, "capability_id": task.capability_id,
        "capability_version": "1", "finite_authority": True,
        "operation_ref": model.operation_ref, "plan_version": model.plan_version,
        "permissions": ["workspace_read", "workspace_write"],
        "limits": {"max_seconds": 30, "max_output_bytes": MAX_OUTPUT_BYTES, "max_attempts": 2},
        "no_learning": True,
    }
    fingerprint = digest({"task_ref": task.task_id, "attempt_ref": attempt.attempt_id,
                          "inputs": safe_inputs, "authority": authority})
    return DurableJobSpec(
        identity=DurableJobIdentity(job_id=job_id(task, attempt), owner_kind="user",
            owner_principal_id=task.owner_principal_id, job_kind=task.capability_id,
            capability_version="1", idempotency_scope="work-board-attempt",
            idempotency_key=f"{task.task_id}:{attempt.attempt_id}"),
        inputs=safe_inputs, session_id=task.owner_session_id,
        operator_session_id=task.owner_session_id, goal_id=task.goal_id,
        goal_revision=task.goal_revision, plan_revision=model.plan_version,
        priority=task.priority, declared_authority=authority,
        deadline_at=deadline, max_attempts=2, max_outstanding_jobs=1,
        run_fingerprint=fingerprint,
    )


def output_bytes(capability: str, inputs: Mapping[str, Any]) -> bytes:
    model = EvidenceConsumerInput.model_validate(dict(inputs))
    if capability == DOSSIER:
        if model.producer_schema != "browser_public_task_result":
            raise ValueError("dossier requires the fixed browser output schema")
        # Parse only the known structural producer envelope. All source text
        # remains quoted data, including instruction-looking strings.
        producer = json.loads(model.quoted_source_data)
        if (not isinstance(producer, dict)
            or set(producer) != {"schema_version", "capability_id", "task_id", "attempt_id", "final_url", "extracts", "checks", "request_count"}
            or type(producer.get("schema_version")) is not int or producer["schema_version"] != 1
            or producer.get("capability_id") != "browser.public-task.v1"
            or producer.get("task_id") != model.producer_task_ref
            or producer.get("attempt_id") != model.producer_attempt_ref
            or not isinstance(producer.get("final_url"), str)
            or not isinstance(producer.get("extracts"), list)
            or not isinstance(producer.get("checks"), list)
            or type(producer.get("request_count")) is not int or producer["request_count"] < 0):
            raise ValueError("browser output differs from the exact producer schema")
        result = canonical_bytes({"schema": "evidence_dossier.v1", "source_sha256": model.producer_sha256,
            "quoted_public_evidence": producer, "no_learning": True})
    elif capability == REPORT:
        if model.producer_schema != "evidence_dossier.v1":
            raise ValueError("report requires the fixed evidence dossier schema")
        producer = json.loads(model.quoted_source_data)
        if not isinstance(producer, dict) or set(producer) != {"schema", "source_sha256", "quoted_public_evidence", "no_learning"} or producer["schema"] != "evidence_dossier.v1" or producer["no_learning"] is not True or not isinstance(producer["quoted_public_evidence"], dict) or not isinstance(producer["source_sha256"], str) or len(producer["source_sha256"]) != 64:
            raise ValueError("evidence dossier schema invalid")
        result = ("Local evidence report\n\nUntrusted public source data, quoted verbatim.\n"
            f"Dossier SHA-256: {model.producer_sha256}\nMemory: no_learning\n\n"
            + json.dumps(producer["quoted_public_evidence"], ensure_ascii=False, sort_keys=True, indent=2)
            + "\n").encode("utf-8")
    else:
        raise ValueError("unregistered evidence CPU capability")
    if len(result) > MAX_OUTPUT_BYTES:
        raise ValueError("evidence output exceeds finite allowance")
    return result


def read_output(reference: str, expected_digest: str, *, max_bytes: int = MAX_OUTPUT_BYTES) -> bytes:
    from src.work_board.input_artifacts import _safe_file_bytes
    from src.browser.task_runner import read_browser_artifact_bytes
    root = canonical_workspace_root(settings.workspace_dir)
    if reference.startswith("artifacts/work-board/browser/"):
        raw = read_browser_artifact_bytes(reference, workspace_root=root, max_bytes=max_bytes)
        if raw is None or hashlib.sha256(raw).hexdigest() != expected_digest:
            raise ValueError("browser source digest mismatch")
        return raw
    if not reference.startswith("artifacts/work-board/evidence/") or ".." in reference.split("/"):
        raise ValueError("evidence source reference invalid")
    path = root / reference
    # Size follows private no-follow open/read, rather than an untrusted stat.
    import os
    from src.work_board.input_artifacts import _open_input_artifact_parent
    fd, leaf = _open_input_artifact_parent(path, create=False)
    try:
        descriptor = os.open(leaf, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=fd)
        try:
            size = os.fstat(descriptor).st_size
        finally:
            os.close(descriptor)
    finally:
        os.close(fd)
    if size < 1 or size > max_bytes:
        raise ValueError("evidence source size invalid")
    return _safe_file_bytes(path, expected_digest=expected_digest, expected_size=size)


async def execute(task: WorkBoardTask, attempt: WorkBoardAttempt, inputs: Mapping[str, Any], *, jobs: Any, runner: str, deadline: datetime, admission_only: bool, validate_current: Any, validate_terminal: Any) -> Mapping[str, Any]:
    spec = spec_for(task, attempt, inputs, deadline=deadline)
    existing = await jobs.get_job(spec.identity.job_id)
    if existing is None:
        projection = await jobs.admit_job(spec)
    else:
        projection = existing
        # Deadline never renews on recovery. The complete immutable authority
        # and input/fingerprint binding must still match the admitted job.
        from src.workflows.job_runtime import _digest
        if projection.get("input_digest") != _digest(spec.inputs) or projection.get("run_fingerprint") != spec.run_fingerprint or projection.get("declared_authority") != spec.declared_authority:
            raise ValueError("evidence CPU immutable admission drift")
    if admission_only or projection.get("status") == "succeeded":
        return {**dict(projection), "job_id": spec.identity.job_id, "admission_only": admission_only, "status": projection.get("status")}
    await validate_current(task, attempt, inputs)
    if projection.get("status") == "accepted":
        projection = await jobs.queue_job(spec.identity.job_id, expected_revision=projection.get("revision"),
            reason="evidence_cpu_board_linked")
    projection = await jobs.claim_job(spec.identity.job_id, owner=runner, lease_seconds=30,
        expected_revision=projection.get("revision"), expected_fencing_token=projection.get("fencing_token"))
    lease = projection.get("lease") or {}
    fence = int(lease.get("fencing_token") or 0)
    try:
        # This current-source guard and use-admission receipt commit together;
        # a prior advisory validation alone cannot order canonical correction.
        await jobs.record_checkpoint(spec.identity.job_id,
            checkpoint_id='evidence-cpu-source-use', state={'phase': 'source_use_admitted'},
            checkpoint_payload={'phase': 'source_use_admitted', 'no_learning': True},
            owner=runner, fencing_token=fence, expected_revision=projection.get('revision'))
        await validate_current(task, attempt, inputs)
        content = output_bytes(task.capability_id, inputs)
        sha = hashlib.sha256(content).hexdigest()
        suffix = "txt" if task.capability_id == REPORT else "json"
        reference = f"artifacts/work-board/evidence/{task.task_id}-{attempt.attempt_id}-{sha}.{suffix}"
        from src.work_board.input_artifacts import _write_payload
        _write_payload(canonical_workspace_root(settings.workspace_dir) / reference, content)
        actual = read_output(reference, sha)
        if actual != content:
            raise ValueError("evidence output readback mismatch")
        await validate_current(task, attempt, inputs)
        await jobs.record_artifact(spec.identity.job_id, file_path=reference,
            artifact_type="evidence_local_report" if task.capability_id == REPORT else "evidence_dossier",
            content=actual, owner=runner, fencing_token=fence)
        await jobs.record_readback(spec.identity.job_id, effect_type="evidence_cpu_output",
            target_path=reference, target_digest=sha, content_sha256=sha,
            readback_id=f"evidence-readback-{sha[:32]}", verified_at=datetime.now(timezone.utc).isoformat(),
            status="succeeded", details={"verified": True, "no_learning": True},
            owner=runner, fencing_token=fence)
        await validate_current(task, attempt, inputs)
        finished = await jobs.transition_job(spec.identity.job_id, "succeeded", owner=runner,
            fencing_token=fence, result={"status": "succeeded", "no_learning": True, "output_sha256": sha},
            result_summary="Deterministic local evidence output independently read back; no_learning",
            terminal_authority_check=validate_terminal)
        return {**dict(finished), "job_id": spec.identity.job_id, "status": "succeeded", "memory_status": "no_learning", "admission_only": False}
    except BaseException:
        # A terminal authority callback is inside the job's atomic transaction
        # and rolls back on rejection. Recheck in its own guard session so a
        # current Goal/source freeze persists without committing job effects.
        try:
            await validate_current(task, attempt, inputs)
        except Exception:
            pass
        await jobs.transition_job(spec.identity.job_id, "blocked", owner=runner,
            fencing_token=fence, reason="Evidence output needs exact binding/readback recovery")
        raise
