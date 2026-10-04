"""One private selected-context invocation on the canonical native job row.

Pair/state/Vault/crypto/file staging precedes pure SQL writers. No text is put
in native input, checkpoints, audit, generic evidence or model projections.
"""
from __future__ import annotations

import asyncio
import anyio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import time
import threading
import uuid

from sqlalchemy import select, text, func, update, or_

from src.auth.service import authenticate_principal, AuthFailure, AuthenticatedOperator
from src.auth.ownership import _current_root
from src.db.models import WorkflowRunState, WorkBoardTask, Goal, Secret, ApprovalRequest
from src.extensions.state import held_extension_state_lock, load_extension_state_payload, save_extension_state_payload, set_node_adapter_pairing_entry, state_path
from src.extensions.paired_edge import current_pairing, resolve_pairing_secret
from src.vault.repository import vault_repository, secret_identity, secret_binding_digest
from src.approval.repository import approval_repository, approval_decision_digest, fingerprint_tool_call
from src.goals.repository import deserialize_admission_budget
from src.workflows.job_runtime import durable_job_repository, DurableJobIdentity, DurableJobSpec, _serialize, _assert_canonical_goal_fence
from src.workflows import selected_context_files as files
from src.workflows.selected_context_contract import *

# Physical settlement only; authority remains the canonical native row. A
# cancelled await never cancels a filesystem thread or proves file absence.
_publications = {}


def _publish_and_verify(ref, ciphertext, plaintext, aborted):
    try:
        if aborted.is_set():
            return
        files.publish(ref, ciphertext)
        if not aborted.is_set() and files.read(ref) != plaintext:
            deny("selected_context_readback_changed")
    finally:
        if aborted.is_set():
            files.discard(ref)


def publication_settled(ident, checkpoint):
    pin = checkpoint.get("physical_publication")
    if pin is None or pin.get("settled") is True:
        return True
    operation = _publications.get(ident)
    return operation is not None and operation[0] == pin.get("id") and operation[1].done()


def session():
    from src.db.engine import get_session
    return get_session()


@asynccontextmanager
async def writer():
    async with session() as db:
        if db.get_bind().dialect.name == "sqlite":
            await db.execute(text("BEGIN IMMEDIATE"))
        yield db


def now():
    return datetime.now(timezone.utc)


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def state(run):
    try:
        value = json.loads(run.checkpoint_context_json or "{}")
    except (ValueError, TypeError):
        deny("selected_context_checkpoint_invalid")
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        deny("selected_context_checkpoint_invalid")
    return value


async def cas(db, run, values):
    result = await db.execute(update(WorkflowRunState).where(
        WorkflowRunState.id == run.id, WorkflowRunState.revision == run.revision,
        WorkflowRunState.run_identity == run.run_identity).values(**values,
        revision=run.revision + 1, updated_at=now()).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        deny("selected_context_revision_changed")


def pair_digest(entry):
    # Finite target publication must not change the underlying generation pin.
    return digest({key: value for key, value in entry.items() if key != "selected_context_target"})


@dataclass(frozen=True)
class PairProof:
    operator: AuthenticatedOperator
    locator: PairLocator
    entry: dict
    target: Target | None
    vault_identity: dict
    vault_binding_digest: str
    state_file_digest: str
    state_revision: int


@dataclass(frozen=True)
class AdmissionProof:
    pair: PairProof
    metadata: Metadata


@dataclass(frozen=True)
class OwnerProof:
    operator: AuthenticatedOperator


@asynccontextmanager
async def stage_pair(locator, *, credential=None, operator=None, require_target=True):
    """Hold the shared config generation across outside-writer staging/CAS."""
    with held_extension_state_lock(shared=True):
        payload = load_extension_state_payload()
        entry, lifecycle = current_pairing(payload, extension_id=locator.extension_id,
            reference=locator.reference, name=locator.name)
        if entry.get("credential_owner_scope_version") != "owner-v1":
            deny("selected_context_owned_pair_rotation_required")
        if lifecycle.lifecycle.value != "paired" or lifecycle.device_id != locator.device_id or lifecycle.pairing_id != locator.pairing_id or (lifecycle.expires_at is not None and utc(lifecycle.expires_at) <= now()):
            deny("selected_context_pair_changed")
        owner = entry.get("owner_principal_id")
        if not isinstance(owner, str) or not owner:
            deny("selected_context_owner_unavailable", 403)
        if operator is not None and operator.principal.principal_id != owner:
            deny("selected_context_pair_unavailable", 404)
        snapshot = await vault_repository.snapshot(entry.get("credential_vault_key", ""), owner_principal_id=owner)
        if snapshot is None:
            deny("selected_context_pair_credential_unavailable", 403)
        try:
            verified = await resolve_pairing_secret(entry, lifecycle,
                extension_id=locator.extension_id, reference=locator.reference,
                presented_credential=credential if credential is not None else snapshot.value)
            if verified.identity != snapshot.identity or verified.binding_digest != snapshot.binding_digest:
                deny("selected_context_pair_credential_changed")
            async with session() as db:
                current = await authenticate_principal(owner, db=db)
                if operator is not None:
                    await _current_root(db, operator)
                    if current.session_id != operator.session_id:
                        deny("selected_context_original_root_changed")
        except (ValueError, AuthFailure):
            deny("selected_context_pair_authentication_denied", 403)
        target = None
        if require_target:
            try:
                target = Target.model_validate(entry.get("selected_context_target"))
            except ValueError:
                deny("selected_context_target_permission_required")
            if target.owner_principal_id != owner or target.original_root_session_id != current.session_id or target.pair_generation != lifecycle.generation or target.pair_digest != pair_digest(entry) or target.vault_binding_digest != snapshot.binding_digest:
                deny("selected_context_target_changed")
        from pathlib import Path
        file_digest = hashlib.sha256(Path(state_path()).read_bytes()).hexdigest()
        yield PairProof(operator or current, locator, dict(entry), target, snapshot.identity,
            snapshot.binding_digest, file_digest, int(payload.get("revision", 0))), snapshot.value


async def assert_root(db, proof):
    operator = proof.operator
    if operator._token_hash is not None:
        await _current_root(db, operator)
    current = await authenticate_principal(operator.principal.principal_id, db=db)
    if current.session_id != operator.session_id or current.ownership_continuity != "stable":
        deny("selected_context_original_root_changed", 403)
    return current


async def assert_secret(db, proof):
    row = await db.get(Secret, proof.vault_identity["id"], populate_existing=True)
    if row is None or row.revoked_at is not None or row.owner_principal_id != proof.operator.principal.principal_id or secret_identity(row) != proof.vault_identity or secret_binding_digest(row) != proof.vault_binding_digest:
        deny("selected_context_pair_credential_changed", 403)


async def assert_target(db, proof, target=None):
    await assert_root(db, proof)
    await assert_secret(db, proof)
    target = target or proof.target
    if target is None or target != proof.target or target.expires_at <= int(time.time()):
        deny("selected_context_target_expired")
    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == target.task_id).execution_options(populate_existing=True))
    if task is None or (task.owner_principal_id, task.owner_session_id) != (target.owner_principal_id, target.original_root_session_id) or task.task_revision != target.task_revision or (task.goal_id, task.goal_revision) != (target.goal_id, target.goal_revision) or task.status in {"archived", "cancelled", "done", "running"}:
        deny("selected_context_task_changed")
    goal = await _assert_canonical_goal_fence(db, goal_id=target.goal_id, goal_revision=target.goal_revision,
        owner_kind="user", owner_principal_id=target.owner_principal_id, session_id=target.original_root_session_id)
    grant = deserialize_admission_budget(goal)
    if grant is None or grant.reviewed_grant is not True or not utc(grant.period_started_at) <= now() < utc(grant.period_expires_at):
        deny("selected_context_finite_goal_required")
    return task, goal, grant


async def bind_target(operator, task_id, body):
    if not body.acknowledge_local_selected_text:
        deny("selected_context_explicit_permission_required", 422)
    async with stage_pair(body.pair, operator=operator, require_target=False) as (proof, _credential):
        async with writer() as db:
            await assert_root(db, proof)
            await assert_secret(db, proof)
            task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id).execution_options(populate_existing=True))
            if task is None or (task.owner_principal_id, task.owner_session_id) != (operator.principal.principal_id, operator.session_id) or task.task_revision != body.expected_task_revision or (task.goal_id, task.goal_revision) != (body.goal_id, body.goal_revision) or task.status in {"archived", "done", "cancelled", "running"}:
                deny("selected_context_task_changed")
            goal = await _assert_canonical_goal_fence(db, goal_id=body.goal_id, goal_revision=body.goal_revision,
                owner_kind="user", owner_principal_id=operator.principal.principal_id, session_id=operator.session_id)
            grant = deserialize_admission_budget(goal)
            if grant is None or grant.reviewed_grant is not True or not utc(grant.period_started_at) <= now() < utc(grant.period_expires_at):
                deny("selected_context_finite_goal_required")
            expiry = int(min(time.time() + 120, operator.idle_expires_at.timestamp(), operator.absolute_expires_at.timestamp(), utc(grant.period_expires_at).timestamp()))
            if proof.entry.get("expires_at"):
                pair_expiry = datetime.fromisoformat(proof.entry["expires_at"].replace("Z", "+00:00"))
                expiry = min(expiry, int(utc(pair_expiry).timestamp()))
        previous = proof.entry.get("selected_context_target", {})
        target = Target(schema_version=1, target_revision=int(previous.get("target_revision", 0)) + 1,
            owner_principal_id=operator.principal.principal_id, original_root_session_id=operator.session_id,
            task_id=task_id, task_revision=body.expected_task_revision, goal_id=body.goal_id,
            goal_revision=body.goal_revision, pair_generation=int(proof.entry.get("generation", 0)),
            pair_digest=pair_digest(proof.entry), vault_binding_digest=proof.vault_binding_digest, expires_at=expiry)
    # Never upgrade a held shared lock. Publication takes the existing exclusive
    # CAS nonblockingly after all staging and pure authority validation finish.
    payload = load_extension_state_payload()
    entry, lifecycle = current_pairing(payload, extension_id=body.pair.extension_id, reference=body.pair.reference, name=body.pair.name)
    if int(payload.get("revision", 0)) != body.expected_state_revision or body.expected_state_revision != proof.state_revision or pair_digest(entry) != target.pair_digest:
        deny("selected_context_pair_state_changed")
    entry = dict(entry)
    entry["selected_context_target"] = target.model_dump()
    set_node_adapter_pairing_entry(payload, extension_id=body.pair.extension_id, reference=body.pair.reference, name=body.pair.name, pairing=entry)
    revision = save_extension_state_payload(payload, expected_revision=body.expected_state_revision)
    return {"target": target.model_dump(), "state_revision": revision, "execution_authority": False, "provider_contact": False}


async def guard_admission(db, spec, proof):
    """Pure same-writer capture-primary guard called by native admission."""
    metadata = proof.metadata
    ident = job_id(metadata.target.owner_principal_id, metadata.capture_uuid)
    if spec.identity.job_id != ident or spec.identity.job_kind != JOB_KIND or spec.identity.capability_version != VERSION or spec.source_task_id != metadata.target.task_id or spec.inputs != metadata.model_dump():
        deny("selected_context_admission_binding_changed")
    await assert_root(db, proof.pair)
    original = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == ident))
    if original is not None:
        if (original.owner_principal_id, original.operator_session_id, original.job_kind, original.source_task_id) != (metadata.target.owner_principal_id, metadata.target.original_root_session_id, JOB_KIND, metadata.target.task_id) or original.input_digest != digest(spec.inputs):
            deny("selected_context_capture_conflict")
        checkpoint = state(original)
        if checkpoint.get("metadata") != spec.inputs:
            deny("selected_context_capture_conflict")
        return original
    validate_metadata(metadata)
    await assert_target(db, proof.pair, metadata.target)
    outstanding = await db.scalar(select(func.count()).select_from(WorkflowRunState).where(
        WorkflowRunState.owner_principal_id == metadata.target.owner_principal_id,
        WorkflowRunState.job_kind == JOB_KIND, WorkflowRunState.status.in_(("accepted", "queued", "running", "paused"))))
    if outstanding:
        deny("selected_context_owner_outstanding_limit", 429)
    # Nullable scalar quota projection on this SAME canonical job. Reserve at
    # admission, retain through uncertainty and release only verified cleanup.
    # Read at most65 indexed scalars; never scan historical receipt JSON.
    charges = (await db.scalars(select(WorkflowRunState.selected_context_reserved_bytes).where(
        WorkflowRunState.owner_principal_id == metadata.target.owner_principal_id,
        WorkflowRunState.job_kind == JOB_KIND,
        or_(WorkflowRunState.selected_context_reserved_bytes.is_(None), WorkflowRunState.selected_context_reserved_bytes != 0))
        .limit(MAX_RETAINED_CAPTURES + 1))).all()
    if any(type(charge) is not int or not 1 <= charge <= MAX_TEXT_BYTES for charge in charges):
        deny("selected_context_quota_projection_invalid")
    if len(charges) >= MAX_RETAINED_CAPTURES or sum(charges) + metadata.reviewed_byte_count > MAX_RETAINED_BYTES:
        deny("selected_context_owner_retention_limit", 429)


async def run_for_owner(db, operator, ident, task_id=None):
    run = await durable_job_repository._fetch(db, ident)
    if run.job_kind != JOB_KIND or run.capability_version != VERSION or (run.owner_principal_id, run.operator_session_id) != (operator.principal.principal_id, operator.session_id) or (task_id is not None and run.source_task_id != task_id):
        deny("selected_context_capture_unavailable", 404)
    checkpoint = state(run)
    metadata = Metadata.model_validate(checkpoint.get("metadata"))
    declared = json.loads(run.declared_authority_json or "{}")
    if run.source_task_id != metadata.target.task_id or declared.get("source_task_id") != metadata.target.task_id or run.input_digest != digest(metadata.model_dump()) or run.run_identity != job_id(metadata.target.owner_principal_id, metadata.capture_uuid):
        deny("selected_context_locator_changed", 403)
    return run, checkpoint, metadata


async def current_capture(db, proof, run, checkpoint, metadata):
    if checkpoint.get("tombstone") is not None:
        deny("selected_context_capture_discarded", 410)
    if metadata.expires_at <= int(time.time()) or utc(run.deadline_at) <= now():
        deny("selected_context_ticket_expired", 410)
    if checkpoint.get("pair_file_digest") != proof.state_file_digest:
        deny("selected_context_pair_state_changed")
    await assert_target(db, proof, metadata.target)


def metadata_projection(run, checkpoint, metadata):
    pending = checkpoint.get("approval", {})
    return {"job_id": run.run_identity, "kind": JOB_KIND, "capability_id": CAPABILITY_ID,
        "source_task_id": run.source_task_id, "status": run.status, "revision": run.revision,
        "deadline_at": utc(run.deadline_at).isoformat(), "source": metadata.source.model_dump(),
        "capture_uuid": metadata.capture_uuid, "reviewed_utf8_sha256": metadata.reviewed_utf8_sha256,
        "reviewed_byte_count": metadata.reviewed_byte_count, "adapter_build_digest": metadata.adapter_build_digest,
        "approval_id": pending.get("id"), "approval_decision_digest": pending.get("decision_digest"),
        "tombstone": checkpoint.get("tombstone"), "cleanup_state": checkpoint.get("cleanup_state"),
        "no_learning": True, "analysis_eligible": False, "instruction_authority": False,
        "private_read_available": False, "provider_contact": False}


def approval_binding(row):
    return digest([row.id, row.owner_principal_id, row.operator_session_id,
        row.session_id, row.tool_name, row.fingerprint, row.details_json,
        utc(row.expires_at).isoformat() if row.expires_at is not None else None,
        row.action, row.risk_level])


async def exact_approval(db, run, checkpoint):
    bound = checkpoint.get("approval", {})
    row = await db.get(ApprovalRequest, bound.get("id"), populate_existing=True)
    if row is None or row.owner_principal_id != run.owner_principal_id or row.operator_session_id != run.operator_session_id or row.tool_name != CAPABILITY_ID or row.fingerprint != bound.get("fingerprint") or approval_binding(row) != bound.get("binding_digest") or row.expires_at is None or utc(row.expires_at) <= now():
        deny("selected_context_exact_approval_changed", 403)
    return row


async def prepare(proof, metadata):
    validate_metadata(metadata)
    if metadata.pair != proof.locator or metadata.target != proof.target:
        deny("selected_context_target_changed")
    inputs = metadata.model_dump()
    spec = DurableJobSpec(identity=DurableJobIdentity(job_id=job_id(metadata.target.owner_principal_id, metadata.capture_uuid),
        owner_kind="user", owner_principal_id=metadata.target.owner_principal_id, job_kind=JOB_KIND,
        capability_version=VERSION, idempotency_scope=JOB_KIND, idempotency_key=metadata.capture_uuid),
        inputs=inputs, source_task_id=metadata.target.task_id, session_id=metadata.target.original_root_session_id,
        operator_session_id=metadata.target.original_root_session_id, goal_id=metadata.target.goal_id,
        goal_revision=metadata.target.goal_revision, priority=60, resource_claims=("selected-context-local",),
        declared_authority={"schema_version": 1, "capability_id": CAPABILITY_ID, "capability_version": VERSION,
            "owner_kind": "user", "principal": metadata.target.owner_principal_id,
            "session_id": metadata.target.original_root_session_id, "source_task_id": metadata.target.task_id,
            "goal_id": metadata.target.goal_id, "goal_revision": metadata.target.goal_revision,
            "target_digest": digest(metadata.target.model_dump()), "finite_authority": True,
            "runtime_cap_seconds": 15, "no_learning": True, "analysis_eligible": False,
            "instruction_authority": False, "external_mutation": False, "credential_egress": False},
        deadline_at=datetime.fromtimestamp(metadata.expires_at, timezone.utc), max_attempts=1, max_outstanding_jobs=1)
    admitted = await durable_job_repository.admit_job(spec, selected_context_admission=AdmissionProof(proof, metadata))
    ident = admitted["job_id"]
    async with writer() as db:
        run, checkpoint, original = await run_for_owner(db, proof.operator, ident)
        if run.status != "accepted":
            return metadata_projection(run, checkpoint, original)
        await assert_target(db, proof, metadata.target)
        if checkpoint.get("metadata") != inputs:
            deny("selected_context_admission_phase_changed")
    approval_id = str(uuid.uuid5(uuid.NAMESPACE_URL, ident + ":selected-context-approval"))
    async with session() as db:
        run, checkpoint, _ = await run_for_owner(db, proof.operator, ident)
    scope = {"job_id": ident, "input_digest": run.input_digest, "authority_digest": run.authority_digest,
        "target_digest": digest(metadata.target.model_dump()), "expires_at": metadata.expires_at,
        "reviewed_utf8_sha256": metadata.reviewed_utf8_sha256, "reviewed_byte_count": metadata.reviewed_byte_count}
    fingerprint = fingerprint_tool_call(CAPABILITY_ID, {"scope_digest": digest(scope)})
    details = {"approval_scope": scope, "scope_digest": digest(scope), "session_id": run.operator_session_id,
        "operator_session_id": run.operator_session_id, "owner_principal_id": run.owner_principal_id,
        "approval_operator_principal_id": run.owner_principal_id, "durable_job_id": ident,
        "durable_owner_kind": "user", "durable_owner_principal_id": run.owner_principal_id,
        "durable_authority_digest": run.authority_digest, "durable_goal_id": run.goal_id,
        "durable_goal_revision": run.goal_revision, "durable_capability_version": VERSION,
        "durable_budget_digest": run.budget_digest, "durable_approval_id": approval_id,
        "approval_expires_at": metadata.expires_at, "expires_at": metadata.expires_at, "attachment_refs": []}
    approval = await approval_repository.get_or_create_pending(session_id=run.operator_session_id,
        tool_name=CAPABILITY_ID, risk_level="high", summary="Attach this reviewed private selected-text digest to this exact task; no analysis or learning",
        fingerprint=fingerprint, details=details, request_id=approval_id)
    async with writer() as db:
        run, checkpoint, _ = await run_for_owner(db, proof.operator, ident)
        await current_capture(db, proof, run, checkpoint, metadata)
        if run.status == "paused" and checkpoint.get("approval", {}).get("id") == approval_id:
            return metadata_projection(run, checkpoint, metadata)
        if run.status != "accepted" or approval.id != approval_id:
            deny("selected_context_approval_changed")
        checkpoint["approval"] = {"id": approval_id, "fingerprint": fingerprint,
            "binding_digest": approval_binding(approval), "decision_digest": approval_decision_digest(approval)}
        await cas(db, run, {"status": "paused", "checkpoint_context_json": canonical(checkpoint)})
    return await inspect(proof, ident)


async def inspect(proof, ident, *, task_id=None, include_private=False):
    async with writer() as db:
        await assert_root(db, proof)
        run, checkpoint, metadata = await run_for_owner(db, proof.operator, ident, task_id)
        result = metadata_projection(run, checkpoint, metadata)
        approval = await db.get(ApprovalRequest, checkpoint.get("approval", {}).get("id"))
        if approval is not None:
            result["approval_status"] = approval.status
            result["approval_decision_digest"] = approval_decision_digest(approval)
        try:
            await current_capture(db, proof, run, checkpoint, metadata)
            result["private_read_available"] = run.status == "succeeded" and "file" in checkpoint
        except SelectedContextError as exc:
            result["private_read_reason"] = exc.code
        ref = checkpoint.get("file")
    if include_private:
        if not result["private_read_available"] or not ref:
            deny("selected_context_private_read_denied", 403)
        with files.capture_lock(ident, shared=True):
            plaintext = await asyncio.to_thread(files.read, ref)
            async with writer() as db:
                latest, current, latest_meta = await run_for_owner(db, proof.operator, ident, task_id)
                await current_capture(db, proof, latest, current, latest_meta)
                if latest.status != "succeeded" or current.get("file") != ref:
                    deny("selected_context_private_read_changed", 403)
            result["text"] = plaintext
    return result


async def decide(proof, ident, body):
    async with writer() as db:
        run, checkpoint, metadata = await run_for_owner(db, proof.operator, ident)
        await current_capture(db, proof, run, checkpoint, metadata)
        if run.status != "paused":
            deny("selected_context_approval_phase_changed")
        await exact_approval(db, run, checkpoint)
        try:
            await approval_repository.resolve_exact_in_session(db, checkpoint["approval"]["id"],
                body.decision, expected_digest=body.expected_digest)
        except ValueError:
            deny("selected_context_exact_approval_changed", 409)
    return await inspect(proof, ident)


async def upload(proof, body):
    metadata = body.metadata
    validate_metadata(metadata)
    raw = validate_text(body)
    ident = job_id(metadata.target.owner_principal_id, metadata.capture_uuid)
    with files.capture_lock(ident):
        async with writer() as db:
            run, checkpoint, original = await run_for_owner(db, proof.operator, ident)
            await current_capture(db, proof, run, checkpoint, original)
            if metadata != original:
                deny("selected_context_capture_conflict")
            if run.status == "succeeded":
                return metadata_projection(run, checkpoint, original)
            approval = await exact_approval(db, run, checkpoint)
            if run.status != "paused" or approval is None or approval.status != "approved" or approval.owner_principal_id != run.owner_principal_id or approval.operator_session_id != run.operator_session_id:
                deny("selected_context_exact_approval_required", 403)
            approved_digest = approval_decision_digest(approval)
            admitted = _serialize(run)
        queued = await durable_job_repository.queue_job(ident, expected_state="paused", expected_revision=admitted["revision"])
        worker = "selected-context:" + uuid.uuid4().hex
        claimed = await durable_job_repository.claim_job(ident, owner=worker, lease_seconds=15, expected_revision=queued["revision"])
        if claimed["status"] != "running":
            deny("selected_context_native_execution_blocked")
        fence = claimed["lease"]["fencing_token"]
        ref = None
        try:
            async with asyncio.timeout(15):
                ref, ciphertext = await asyncio.to_thread(files.prepare, ident, body.text)
                async with writer() as db:
                    run, checkpoint, original = await run_for_owner(db, proof.operator, ident)
                    await current_capture(db, proof, run, checkpoint, original)
                    durable_job_repository._assert_lease(run, owner=worker, fencing_token=fence)
                    if checkpoint.get("file"):
                        deny("selected_context_upload_already_reserved")
                    checkpoint["file"] = ref
                    operation_id = uuid.uuid4().hex
                    checkpoint["physical_publication"] = {"id": operation_id, "settled": False}
                    await cas(db, run, {"checkpoint_context_json": canonical(checkpoint)})
                aborted = threading.Event()
                operation = asyncio.create_task(asyncio.to_thread(_publish_and_verify, ref, ciphertext, body.text, aborted))
                # Observe errors even after a timed-out request; no late SQL writes.
                operation.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
                _publications[ident] = (operation_id, operation, aborted)
                await asyncio.shield(operation)
                async with writer() as db:
                    run, checkpoint, original = await run_for_owner(db, proof.operator, ident)
                    await current_capture(db, proof, run, checkpoint, original)
                    durable_job_repository._assert_lease(run, owner=worker, fencing_token=fence)
                    approval = await exact_approval(db, run, checkpoint)
                    if approval.status != "approved" or approval_decision_digest(approval) != approved_digest or checkpoint.get("file") != ref:
                        deny("selected_context_exact_approval_changed")
                    approval.status = "consumed"
                    checkpoint["physical_publication"]["settled"] = True
                    await cas(db, run, {"status": "succeeded", "finished_at": now(), "lease_owner": None, "lease_expires_at": None,
                        "checkpoint_context_json": canonical(checkpoint),
                        "result_digest": metadata.reviewed_utf8_sha256, "result_summary": "Verified private D1 selected text; no learning or analysis",
                        "artifact_receipts_json": canonical([{"artifact_id": ident + ":private-text", "producer": JOB_KIND,
                            "artifact_type": "selected_context_private_v1", "file_path": ref["path"],
                            "content_sha256": ref["ciphertext_digest"], "plaintext_bytes": len(raw), "verified": True,
                            "instruction_authority": False, "analysis_eligible": False, "no_learning": True}])})
        except BaseException:
            operation = _publications.get(ident)
            if operation is not None:
                operation[2].set()
            # ASGI cancellation scopes must not interrupt local tombstoning.
            with anyio.CancelScope(shield=True):
                # Revoke visibility FIRST, then settle only capture-owned bytes.
                async with writer() as db:
                    run = await durable_job_repository._fetch(db, ident)
                    checkpoint = state(run)
                    if run.status == "running" and not checkpoint.get("tombstone"):
                        checkpoint["tombstone"] = {"reason": "upload_failed", "at": int(time.time())}
                        checkpoint["cleanup_state"] = "blocked_cleanup"
                        await cas(db, run, {"status": "cancelled", "checkpoint_context_json": canonical(checkpoint), "lease_owner": None, "lease_expires_at": None})
                cleanup = ref is None
                if ref is not None and publication_settled(ident, checkpoint):
                    try:
                        async with asyncio.timeout(5):
                            cleanup = await asyncio.to_thread(files.discard, ref)
                    except (OSError, SelectedContextError, TimeoutError):
                        cleanup = False
                async with writer() as db:
                    run = await durable_job_repository._fetch(db, ident)
                    checkpoint = state(run)
                    if checkpoint.get("tombstone", {}).get("reason") == "upload_failed":
                        checkpoint["cleanup_state"] = "verified_unavailable" if cleanup else "blocked_cleanup"
                        if cleanup and checkpoint.get("physical_publication"):
                            checkpoint["physical_publication"]["settled"] = True
                        await cas(db, run, {"checkpoint_context_json": canonical(checkpoint),
                            "selected_context_reserved_bytes": 0 if cleanup else run.selected_context_reserved_bytes})
                if cleanup:
                    _publications.pop(ident, None)
            raise
        _publications.pop(ident, None)
    return await inspect(proof, ident)


async def discard(proof, ident, body, *, task_id):
    async with writer() as db:
        await assert_root(db, proof)
        run, checkpoint, metadata = await run_for_owner(db, proof.operator, ident, task_id)
        existing = checkpoint.get("tombstone")
        if existing is not None and not isinstance(existing, dict):
            deny("selected_context_checkpoint_invalid")
        if existing is None or existing.get("request_uuid") is None:
            if run.revision != body.expected_revision:
                deny("selected_context_revision_changed")
            checkpoint["tombstone"] = {**(existing or {"reason": "operator_discard", "at": int(time.time())}),
                "request_uuid": body.request_uuid, "request_digest": digest(body.model_dump()),
                "original_expected_revision": body.expected_revision}
            checkpoint["cleanup_state"] = "blocked_cleanup"
            await cas(db, run, {"status": "cancelled", "checkpoint_context_json": canonical(checkpoint),
                "lease_owner": None, "lease_expires_at": None})
        elif existing.get("request_uuid") != body.request_uuid or existing.get("request_digest") != digest(body.model_dump()):
            deny("selected_context_discard_conflict")
        ref = checkpoint.get("file")
    cleanup = False
    try:
        with files.capture_lock(ident):
            async with asyncio.timeout(5):
                cleanup = publication_settled(ident, checkpoint) and (ref is None or await asyncio.to_thread(files.discard, ref))
    except (OSError, SelectedContextError, TimeoutError):
        pass
    async with writer() as db:
        await assert_root(db, proof)
        run, checkpoint, metadata = await run_for_owner(db, proof.operator, ident, task_id)
        if checkpoint.get("tombstone", {}).get("request_uuid") != body.request_uuid:
            deny("selected_context_discard_conflict")
        checkpoint["cleanup_state"] = "verified_unavailable" if cleanup else "blocked_cleanup"
        if cleanup and checkpoint.get("physical_publication"):
            checkpoint["physical_publication"]["settled"] = True
        await cas(db, run, {"checkpoint_context_json": canonical(checkpoint),
            "selected_context_reserved_bytes": 0 if cleanup else run.selected_context_reserved_bytes})
        if cleanup:
            _publications.pop(ident, None)
        result = metadata_projection(run, checkpoint, metadata)
    result["encrypted_audit_bytes_may_remain"] = True
    result["physical_erasure_verified"] = False
    if not cleanup:
        deny("selected_context_cleanup_blocked", 503)
    return result
