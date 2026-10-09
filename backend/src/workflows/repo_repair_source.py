"""Private source bindings for the fixed repository iteration owners.

These objects are deliberately not request models. A serialized projection is
evidence, and cannot recreate an execution or accounting witness.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
import hashlib
import json
import re
from typing import Any
from contextlib import asynccontextmanager
from contextvars import ContextVar

from src.work_board.contracts import (TaskProposalGroupV1,
    GeneralTaskNativeChildBindingV1, RepositoryTaskSourceBinding)

_SEAL = object()
_SHA = re.compile(r"^[0-9a-f]{64}$")
_task_publication = ContextVar("repository_task_publication", default=None)


def _source_digest(value):
    from src.security.trust_contract import canonical_digest
    return canonical_digest(value)


def _iterative_executor_preflight(service, compiled=None):
    from src.execution.repo_node import NodeRepoRepairExecutor
    from src.execution.repo_sandbox import LocalRepoRepairExecutor
    from src.workflows.job_runtime import DurableJobLeaseError
    if type(service.sandbox) is NodeRepoRepairExecutor:
        if compiled is None:
            raise DurableJobLeaseError("original compiled Node selection required")
        return service.sandbox.preflight({"repository_ref": str(service._workspace() / compiled.repository_path),
            "test_args": tuple(compiled.test_args), "allowed_paths": compiled.allowed_paths})
    if type(service.sandbox) is LocalRepoRepairExecutor:
        return service.sandbox.iterative_preflight()
    raise DurableJobLeaseError("fixed local iterative executor required")


def repository_checkpoint_inventory(run, work):
    """Reserve the full fixed identity set before any repository effect.

    Private bodies/logs are artifacts; the canonical journal holds bounded
    digest metadata. Keeping the inventory on the original root is not a
    second capacity ledger.
    """
    original, *_ = read_repository_original(run)
    ids = ["repository:original:v1", "repository:inventory:v1", "repository:terminal:v1"]
    for index in range(1, work.limits.max_iterations + 1):
        identity = iteration_identity(run.run_identity, original["repository_attempt_id"],
            _source_digest(original["original_input"]), index)
        ids.extend("repository:" + role + ":" + identity for role in (
            "prepared", "callback-start", "request", "response", "proposal", "patch",
            "approval", "execution", "cleanup", "readback", "accounting", "iteration"))
    ids.extend(("repo-repair-source-intent:" + run.run_identity,
                "repo-repair-source:" + run.run_identity,
                "repo-repair-execution-reservation"))
    if len(ids) != len(set(ids)) or len(ids) > 50:
        from src.workflows.job_runtime import DurableJobTransitionError
        raise DurableJobTransitionError("repository fixed checkpoint capacity exceeded")
    return ids


def _repository_record(run, identity):
    from src.workflows.general_task_guard import _history
    from src.workflows.job_runtime import DurableJobLeaseError, _digest
    records = [item for item in _history(run) if item.get("checkpoint_id") == identity]
    if not records:
        return None
    if (len(records) != 1 or records[0].get("safe") is not True
            or not isinstance(records[0].get("payload"), dict)
            or records[0].get("state_digest") != _digest(records[0]["payload"])):
        raise DurableJobLeaseError("protected repository record changed")
    return records[0]["payload"]


def _append_repository_record(run, identity, payload, *, inventory):
    from src.workflows.general_task_guard import _history
    from src.workflows.job_runtime import DurableJobTransitionError, _canonical, _digest, _utc_now
    if identity not in inventory:
        raise DurableJobTransitionError("unreserved repository checkpoint identity")
    existing = _repository_record(run, identity)
    if existing is not None:
        if existing != payload:
            raise DurableJobTransitionError("immutable repository checkpoint changed")
        return False
    history = _history(run)
    if len(history) >= 50 or len(set(inventory) | {item["checkpoint_id"] for item in history}) > 50:
        raise DurableJobTransitionError("repository checkpoint history is full; never evict")
    if len(_canonical(payload).encode()) > 16384:
        raise DurableJobTransitionError("repository metadata checkpoint exceeds its reserved bound")
    history.append({"checkpoint_id": identity, "safe": True, "payload": payload,
        "state_digest": _digest(payload), "created_at": _utc_now().isoformat()})
    run.checkpoint_receipts_json = _canonical(history)
    return True


async def repository_source_root(db, *, job_id, owner):
    """Absence is legacy; malformed source evidence is never a fallback."""
    from sqlalchemy import select
    from src.db.models import WorkflowRunState
    from src.workflows.general_task_guard import _history
    from src.workflows.job_runtime import DurableJobLeaseError
    run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id))
    if run is None:
        raise DurableJobLeaseError("repository root unavailable")
    if (run.owner_principal_id, run.operator_session_id) != (owner.principal_id, owner.session_id):
        raise DurableJobLeaseError("original repository owner changed")
    if not any(item.get("checkpoint_id") == "repository:original:v1" for item in _history(run)):
        return False
    read_repository_original(run)
    return True


async def prepare_repository_native_source(general_task_service, jobs, binding, *, child_owner, principal):
    """Prepare the one actual original root, before invoking any adapter."""
    from math import ceil
    from sqlalchemy import select
    from src.db.models import WorkflowRunState, WorkBoardAttempt, Goal
    from src.work_board.contracts import WorkBoardOwner, WorkBoardInputArtifactCreate, WorkBoardTaskCreate
    from src.work_board.input_artifacts import prepare_input_artifact
    from src.work_board.general_task_native import publish_positive_claim
    from src.workflows.general_task_guard import assert_general_task_child_current
    from src.workflows.job_runtime import (DurableJobIdentity, DurableJobSpec, _binding,
        _digest, _utc_now, _as_utc, DurableJobLeaseError)
    from src.workflows.repo_repair import RepoRepairService, RepoWorkInput
    from src.goals.repository import deserialize_admission_budget
    source = general_task_service.repository_source_service
    if type(source) is not RepoRepairService or not general_task_service.started:
        raise DurableJobLeaseError("actual current repository source service required")
    if (not principal or not principal.authenticated or principal.revoked
            or principal.principal_id != binding.owner_principal_id
            or principal.operator_session_id != binding.original_root_id):
        raise DurableJobLeaseError("original authenticated repository operator required")
    source.jobs = jobs
    key = _binding(owner_principal_id=binding.owner_principal_id, goal_id=binding.goal_id,
        goal_revision=binding.goal_revision, idempotency_scope="original-repository-child",
        dedupe_key=binding.invocation_id)
    async with jobs._session() as db:
        mapped = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.idempotency_binding == key))
        if mapped is not None:
            original, work, *_ = read_repository_original(mapped)
            if original["native_binding"] != binding.model_dump(mode="json"):
                raise DurableJobLeaseError("permanent repository mapping binding changed")
            await assert_general_task_child_current(db, await jobs._fetch(db, binding.invocation_id))
            root_id = mapped.run_identity
            return await _prepared_repository_projection(db, mapped, owner=WorkBoardOwner(
                principal_id=binding.owner_principal_id, session_id=binding.original_root_id))
    pending = await jobs.get_job(binding.invocation_id)
    if (pending is None or pending["status"] not in {"accepted", "queued"}
            or pending["attempt_count"] or pending["lease"]["fencing_token"]
            or pending["lease"]["owner"] or pending["effects"]):
        raise DurableJobLeaseError("original unclaimed repository child required")
    cutoff = min(binding.original_deadline_at, binding.native_deadline_at)
    remaining = (cutoff - _utc_now()).total_seconds()
    if remaining <= 0:
        raise DurableJobLeaseError("original repository cutoff expired")
    if pending["status"] == "accepted":
        await jobs.queue_job(binding.invocation_id)
    child = await jobs.claim_job(binding.invocation_id, owner=child_owner, lease_seconds=max(1, ceil(remaining)))
    await publish_positive_claim(jobs, binding, child_owner=child_owner,
        child_fence=child["lease"]["fencing_token"])
    from src.work_board.general_task_runtime_artifacts import read_current_native_tool_input
    async with jobs._session() as db:
        current_child = await jobs._fetch(db, binding.invocation_id)
        private = await read_current_native_tool_input(db, current_child)
        work = RepoWorkInput.model_validate(private.inputs)
        goal = await db.get(Goal, binding.goal_id)
        budget = deserialize_admission_budget(goal)
        capacity = budget.max_outstanding_jobs if budget else 1
    owner = WorkBoardOwner(principal_id=binding.owner_principal_id, session_id=binding.original_root_id)
    repository = general_task_service.repository
    async with source.session_factory() as db:
        artifact = await prepare_input_artifact(db, owner, WorkBoardInputArtifactCreate(schema_version=1,
            capability_id="engineering.repo-repair.v1", goal_id=binding.goal_id, goal_revision=binding.goal_revision,
            input=work.model_dump(mode="json"), idempotency_key="repository-source:" + binding.invocation_id))
        created = await repository.create_task(db, owner, WorkBoardTaskCreate(
            title="Repair " + work.repository_ref[:100], goal_id=binding.goal_id, goal_revision=binding.goal_revision,
            capability_id="engineering.repo-repair.v1", input_artifact_id=artifact.artifact_id,
            idempotency_scope="original-repository-child", idempotency_key=binding.invocation_id,
            status="todo", requires_review=False))
        await db.commit()
    async with source.session_factory() as db:
        ready = await repository.promote_task_ready(db, created.task.task_id,
            expected_revision=created.task.task_revision, actor_principal_id=owner.principal_id,
            actor_session_id=owner.session_id)
        await db.commit()
    async with source.session_factory() as db:
        claimed = await repository.claim_ready_task(db, created.task.task_id,
            expected_revision=ready.task.task_revision, lease_owner=child_owner, lease_seconds=max(1, ceil(remaining)))
        await db.commit()
    async with source.session_factory() as db:
        check, scope = await prepare_repository_original_admission(source, db,
            native_invocation_id=binding.invocation_id, repository_task_id=created.task.task_id,
            repository_attempt_id=claimed.attempt.attempt_id)
    inputs = {"schema_version": 1, "capability_id": "engineering.repo-repair.v1",
        "input": work.model_dump(mode="json")}
    root_id = "repository:" + _source_digest([binding.invocation_id, "original-root"])
    deadline = min(cutoff, _as_utc(claimed.attempt.lease_expires_at),
        _utc_now() + timedelta(seconds=work.limits.max_total_seconds))
    spec = DurableJobSpec(identity=DurableJobIdentity(root_id, "user", owner.principal_id,
        "engineering.repo-repair.v1", "1", "original-repository-child", binding.invocation_id),
        inputs=inputs, session_id=owner.session_id, operator_session_id=owner.session_id,
        goal_id=binding.goal_id, goal_revision=binding.goal_revision, deadline_at=deadline,
        resource_claims=("repo-repair-execution",), max_attempts=1, max_outstanding_jobs=capacity,
        budget_microusd=work.limits.max_cost_microusd, run_fingerprint=_digest(inputs),
        declared_authority={"principal": owner.principal_id, "owner_kind": "user", "session_id": owner.session_id,
            "capability_id": "engineering.repo-repair.v1", "budget_microusd": work.limits.max_cost_microusd})
    async with scope():
        admitted = await jobs.admit_job(spec, admission_authority_check=check)
    async with source.session_factory() as db:
        await repository.link_attempt_workflow_run(db, created.task.task_id, claimed.attempt.attempt_id,
            workflow_run_id=root_id, expected_revision=claimed.task.task_revision,
            board_fence=claimed.attempt.fencing_token, lease_owner=claimed.attempt.lease_owner,
            workflow_projection=admitted, expected_identity={
                "job_id": root_id, "owner_kind": "user", "owner_principal_id": owner.principal_id,
                "service_id": spec.service_id, "operator_session_id": owner.session_id, "session_id": owner.session_id,
                "goal_id": binding.goal_id, "goal_revision": binding.goal_revision,
                "job_kind": spec.identity.job_kind, "capability_version": "1", "input_digest": _digest(inputs),
                "authority_digest": _digest(spec.declared_authority), "run_fingerprint": spec.run_fingerprint,
                "idempotency_scope": spec.identity.idempotency_scope, "idempotency_key": binding.invocation_id})
        await db.commit()
    await jobs.queue_job(root_id)
    await jobs.claim_job(root_id, owner=child_owner, lease_seconds=max(1, ceil((deadline - _utc_now()).total_seconds())))
    return await prepare_repository_iteration(source, jobs, job_id=root_id, owner=owner, iteration_index=1)


async def _repository_precontact(service, jobs, *, job_id, owner):
    """Stage physical evidence before a caller enters its SQL writer."""
    from sqlalchemy import select
    from src.db.models import WorkBoardTask, WorkBoardAttempt, Goal, OperatorSession, WorkBoardInputArtifact
    from src.workflows.repo_repair import RepoRepairService
    from src.workflows.general_task_guard import assert_general_task_child_current, read_manifest
    from src.work_board.general_task_runtime_artifacts import verify_general_task_manifest
    from src.workflows.job_runtime import DurableJobLeaseError, _as_utc, _utc_now, _canonical
    from src.workflows.general_task_accounting import validate_group_owner
    if type(service) is not RepoRepairService:
        raise DurableJobLeaseError("actual current repository source owner required")
    async with jobs._session() as db:
        run = await jobs._fetch(db, job_id)
        original, work, compiled, group, binding, source = read_repository_original(run)
        if (run.owner_principal_id, run.operator_session_id) != (owner.principal_id, owner.session_id):
            raise DurableJobLeaseError("original repository owner changed")
        child = await jobs._fetch(db, binding.invocation_id)
        await assert_general_task_child_current(db, child)
        parent = await jobs._fetch(db, binding.parent_job_id)
        task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == binding.task_id))
        attempt = await db.get(WorkBoardAttempt, binding.attempt_id)
        envelope = await verify_general_task_manifest(db, parent, task, attempt, read_manifest(parent))
        if envelope.repository_source != source or envelope.proposal_group != group:
            raise DurableJobLeaseError("original scoped Task source changed")
        authority = await service._resolve_canonical_authority(db, owner=owner,
            work_board_task_id=original["repository_task_id"], work_board_attempt_id=original["repository_attempt_id"],
            workflow_run_id=job_id, goal_id=run.goal_id, goal_revision=run.goal_revision,
            lease_owner=run.lease_owner, fencing_token=run.fencing_token)
        if (run.status != "running" or child.status != "running"
                or run.failure_reason is not None or child.failure_reason is not None
                or _as_utc(run.deadline_at) <= _utc_now()
                or _as_utc(child.lease_expires_at) > _as_utc(run.deadline_at) and
                   _as_utc(child.lease_expires_at) > binding.native_deadline_at):
            raise DurableJobLeaseError("original running repository claims required")
        await validate_group_owner(db, group)
        originals = [run, child, parent, task, attempt, authority.task, authority.attempt,
            await db.get(Goal, group.goal_id), await db.get(OperatorSession, group.owner_session_id),
            await db.get(WorkBoardInputArtifact, authority.task.input_artifact_id)]
        from sqlalchemy import inspect as inspect_mapper
        rows = []
        for row in originals:
            keys = tuple(getattr(row, column.key) for column in inspect_mapper(type(row)).primary_key)
            rows.append((type(row), keys[0] if len(keys) == 1 else keys,
                _canonical(row.model_dump(mode="json"))))
        source_facts = json.loads(service._read_private_artifact(source.source_artifact_ref,
            expected_digest=source.source_artifact_digest))
        service.recheck_task_source_snapshot(work, source_facts)
        return {"run": run, "child": child, "parent": parent, "original": original,
            "work": work, "compiled": compiled, "group": group, "binding": binding,
            "source": source, "authority": authority, "rows": rows}


async def _recheck_repository_sql(db, context):
    """Current original SQL rows only; never reopen an artifact in the writer."""
    from src.workflows.job_runtime import DurableJobLeaseError, _canonical, _as_utc, _utc_now
    from src.workflows.general_task_accounting import validate_group_owner
    for model, key, expected in context["rows"]:
        row = await db.get(model, key, populate_existing=True)
        if row is None or _canonical(row.model_dump(mode="json")) != expected:
            raise DurableJobLeaseError("original repository preparation authority changed")
    await validate_group_owner(db, context["group"])
    for row in (context["run"], context["child"], context["authority"].attempt):
        if (_as_utc(row.lease_expires_at) is None or _as_utc(row.lease_expires_at) <= _utc_now()
                or getattr(row, "cancel_requested_at", None) is not None):
            raise DurableJobLeaseError("original repository lease or cancellation changed")
    if _as_utc(context["run"].deadline_at) <= _utc_now():
        raise DurableJobLeaseError("original repository cutoff expired")


async def _repository_remaining(db, context, *, allow_exhausted_readback=False):
    from sqlalchemy import select
    from src.db.models import InferenceCostReservation
    from src.workflows.general_task_accounting import entry_for, reservation_liability
    from src.workflows.inference_accounting import InferenceAccountingError
    rows = list((await db.execute(select(InferenceCostReservation))).scalars())
    members = []
    root_members = []
    group = context["group"]
    for row in rows:
        entry = entry_for(row)
        if entry and entry["group"]["group_id"] == group.group_id:
            if entry["group"] != group.model_dump(mode="json"):
                raise InferenceAccountingError("general_task_group_conflict")
            members.append(row)
        if row.job_id == context["run"].run_identity:
            if not entry or entry["role"] != "repository_iteration" or entry["group"] != group.model_dump(mode="json"):
                raise InferenceAccountingError("repository_original_member_invalid")
            root_members.append(row)
    if any(row.state == "unknown" for row in members + root_members):
        raise InferenceAccountingError("repository_original_unknown_liability")
    calls = group.max_inference_calls - len(members)
    cost = min(group.max_cost_microusd - sum(map(reservation_liability, members)),
        context["work"].limits.max_cost_microusd - sum(map(reservation_liability, root_members)))
    if calls < 0 or cost < 0 or (not allow_exhausted_readback and (calls == 0 or cost == 0)):
        raise InferenceAccountingError("repository_original_budget_exhausted")
    return {"remaining_inference_calls": calls, "remaining_cost_microusd": cost}


async def prepare_repository_iteration(service, jobs, *, job_id, owner, iteration_index):
    from src.model_fabric.effective_policy import configuration_mutation_lock
    from src.workflows.repo_repair import RepoRepairModelOutput, _canonical_bytes
    from src.workflows.job_runtime import _as_utc, _digest, _canonical, DurableJobLeaseError
    from src.work_board.repository import _begin_sqlite_immediate
    from src.llm_runtime import build_model_kwargs
    from config.settings import settings
    async with configuration_mutation_lock:
        config = _assert_task_publication_configuration(service)
        context = await _repository_precontact(service, jobs, job_id=job_id, owner=owner)
        run, original, work = context["run"], context["original"], context["work"]
        if type(iteration_index) is not int or not 1 <= iteration_index <= work.limits.max_iterations:
            raise DurableJobLeaseError("original repository iteration bound exceeded")
        iteration = iteration_identity(job_id, original["repository_attempt_id"],
            _source_digest(original["original_input"]), iteration_index)
        if _repository_record(run, "repository:callback-start:" + iteration) is not None:
            raise DurableJobLeaseError("original callback has already started; never prepare replay")
        existing = _repository_record(run, "repository:prepared:" + iteration)
        if existing is not None:
            return await _prepared_repository_projection(None, run, owner=owner)
        prior_manifest = None
        prior_diagnostics = None
        if iteration_index != 1:
            prior_id = iteration_identity(job_id, original["repository_attempt_id"],
                _source_digest(original["original_input"]), iteration_index - 1)
            prior_cleanup = _repository_record(run, "repository:cleanup:" + prior_id)
            prior_readback = _repository_record(run, "repository:readback:" + prior_id)
            if (prior_cleanup is None or prior_readback is None
                    or prior_cleanup.get("cleanup_proven") is not True
                    or prior_cleanup.get("iteration_id") != prior_id
                    or prior_cleanup.get("status") != "failed" or prior_readback.get("status") != "failed"):
                raise DurableJobLeaseError("positive failed prior iteration cleanup/readback required")
            prior_manifest = json.loads(service._read_private_artifact(prior_readback["artifact_ref"],
                expected_digest=prior_readback["artifact_digest"]))
            prior_diagnostics = json.loads(service._read_private_artifact(prior_readback["diagnostics_artifact_ref"],
                expected_digest=prior_readback["diagnostics_artifact_digest"]))
            if (_source_digest(prior_manifest) != prior_readback["manifest_digest"]
                    or prior_manifest.get("status") != "failed"
                    or prior_diagnostics.get("iteration_id") != prior_id
                    or prior_manifest.get("diff_sha256") != prior_diagnostics.get("cumulative_diff_sha256")
                    or hashlib.sha256(prior_diagnostics["cumulative_diff"].encode()).hexdigest()
                        != prior_manifest.get("diff_sha256")):
                raise DurableJobLeaseError("actual prior tested cumulative diff readback changed")
        if job_id not in service._iterative_lanes:
            from src.work_board.dispatcher import _reserve_repo_repair_execution_capacity
            service._iterative_lanes[job_id] = await _reserve_repo_repair_execution_capacity(
                jobs=jobs, job_id=job_id, attempt_id=original["repository_attempt_id"],
                workspace_root=str(service._workspace()), owner=run.lease_owner,
                fencing_token=run.fencing_token, authority_digest=run.authority_digest,
                execution_deadline_at=original["original_deadline_at"], expected_revision=run.revision)
        # The actual packet publisher records intent, private artifact, row,
        # and verified readback on this same original root.
        packet = await service.inspect_and_prepare(context["compiled"], owner=owner,
            work_board_task_id=original["repository_task_id"], work_board_attempt_id=original["repository_attempt_id"],
            workflow_run_id=job_id, goal_id=run.goal_id, goal_revision=run.goal_revision, input_digest=run.input_digest)
        context = await _repository_precontact(service, jobs, job_id=job_id, owner=owner)
        run = context["run"]
        import asyncio
        preflight = await asyncio.to_thread(_iterative_executor_preflight, service, context["compiled"])
        if not preflight.ok or not preflight.posture_digest:
            raise DurableJobLeaseError("fixed iterative process supervision is blocked")
        packet_payload = json.loads(service._read_private_artifact(packet.artifact_ref,
            expected_digest=packet.artifact_sha256))
        input_tree_digest = packet_payload["base_snapshot_sha256"]
        stdout = stderr = ""
        if prior_manifest is not None:
            diff = prior_diagnostics["cumulative_diff"]
            if await service._scan_secrets(diff) != diff:
                raise DurableJobLeaseError("fresh source scan blocks prior cumulative patch secrets")
            packet_payload = {**packet_payload,
                "source_bytes_provenance": "original_acknowledged_frozen_base",
                "prior_tested_iteration": {"iteration_id": prior_diagnostics["iteration_id"],
                    "cumulative_diff": diff, "cumulative_diff_sha256": prior_manifest["diff_sha256"],
                    "diff_provenance": "actual_supervised_checkout_cumulative_diff_against_original_base",
                    "tested_tree_digest": prior_manifest["after_digest"],
                    "tested_file_hash_metadata": (prior_manifest["tested_file_hash_metadata"]
                        if work.language_profile == "test_node" else prior_manifest["publication_test_input"]["tested_files"]),
                    "hashes_are_file_bodies": False}}
            input_tree_digest = prior_manifest["after_digest"]
            stdout, stderr = prior_diagnostics["stdout"], prior_diagnostics["stderr"]
        candidate = await service.prepare_iteration_egress(iteration_id=iteration,
            source_payload=packet_payload, stdout=stdout, stderr=stderr)
        kwargs = build_model_kwargs(temperature=0.2, max_tokens=4096, runtime_path="strategist_agent")
        model = service.model_factory(**kwargs)
        response_format = {"type": "json_schema", "json_schema": {
            "name": "seraph_repo_repair_proposal", "strict": True,
            "schema": RepoRepairModelOutput.model_json_schema()}}
        finalized = service.finalize_iteration_egress(candidate, model=model, response_format=response_format)
        prefix = "artifacts/repo-repair/model/iteration-" + iteration
        body_ref, body_digest = service._write_private_artifact(prefix + "-request.json", _canonical(finalized["request_body"]).encode())
        egress_ref, egress_digest = service._write_private_artifact(prefix + "-egress.json", _canonical_bytes(finalized["envelope"]))
        child, authority = context["child"], context["authority"]
        record = {"schema": "seraph.repository_precontact_preparation.v1",
            "iteration_index": iteration_index, "iteration_id": iteration, "input_tree_digest": input_tree_digest,
            "original_checkpoint_digest": _digest(original), "native_binding_digest": _source_digest(original["native_binding"]),
            "native_child_owner": child.lease_owner, "native_child_fence": child.fencing_token,
            "native_child_lease_expires_at": _as_utc(child.lease_expires_at).isoformat(),
            "repository_task_id": original["repository_task_id"], "repository_attempt_id": original["repository_attempt_id"],
            "repository_owner": run.lease_owner, "repository_fence": run.fencing_token,
            "repository_lease_expires_at": _as_utc(run.lease_expires_at).isoformat(),
            "repository_attempt_owner": authority.attempt.lease_owner, "repository_attempt_fence": authority.attempt.fencing_token,
            "repository_attempt_lease_expires_at": _as_utc(authority.attempt.lease_expires_at).isoformat(),
            "source_packet_id": packet.packet_id, "source_packet_artifact_digest": packet.artifact_sha256,
            "source_manifest_digest": packet.source_manifest_sha256,
            "original_source_binding_digest": context["source"].binding_digest,
            "original_group_digest": _source_digest(context["group"].model_dump(mode="json")),
            "executor_profile_digest": _source_digest(config.model_dump(mode="json")),
            "executor_posture_digest": preflight.posture_digest,
            "canonical_selector_digest": _source_digest(settings.repo_sandbox.model_dump(mode="json")),
            "request_body_artifact_ref": body_ref, "request_body_digest": finalized["serialized_request_sha256"],
            "request_route_digest": finalized["request_route_digest"],
            "egress_envelope_artifact_ref": egress_ref, "egress_envelope_digest": egress_digest,
            "diagnostics_digest": finalized["diagnostics_sha256"], "redaction_version": finalized["redaction_version"],
            "combined_input_bytes": finalized["combined_input_bytes"], "original_deadline_at": original["original_deadline_at"]}
        record["preparation_digest"] = _source_digest(record)
        async with jobs._session() as db:
            await _begin_sqlite_immediate(db)
            await _recheck_repository_sql(db, context)
            await _repository_remaining(db, context)
            current = await jobs._fetch(db, job_id)
            _append_repository_record(current, "repository:prepared:" + iteration, record,
                inventory=repository_checkpoint_inventory(current, work))
            current.revision += 1
            await db.commit()
        async with jobs._session() as db:
            return await _prepared_repository_projection(db, await jobs._fetch(db, job_id), owner=owner)


async def _prepared_repository_projection(db, run, *, owner):
    from src.workflows.job_runtime import DurableJobLeaseError
    original, work, *_ = read_repository_original(run)
    if (run.owner_principal_id, run.operator_session_id) != (owner.principal_id, owner.session_id):
        raise DurableJobLeaseError("original repository projection owner changed")
    records = []
    for index in range(1, work.limits.max_iterations + 1):
        iteration = iteration_identity(run.run_identity, original["repository_attempt_id"],
            _source_digest(original["original_input"]), index)
        prepared = _repository_record(run, "repository:prepared:" + iteration)
        if prepared is not None:
            records.append((prepared, _repository_record(run, "repository:callback-start:" + iteration)))
    if not records:
        raise DurableJobLeaseError("original repository preparation unavailable")
    prepared, started = records[-1]
    if (prepared.get("schema") != "seraph.repository_precontact_preparation.v1"
            or prepared.get("original_checkpoint_digest") != _source_digest(original)
            or prepared.get("preparation_digest") != _source_digest({key: value for key, value in prepared.items()
                if key != "preparation_digest"})):
        raise DurableJobLeaseError("original prepared repository identity changed")
    return {"awaiting_repository_consent": started is None, "native_execution": True,
        "native_child_id": original["native_binding"]["invocation_id"], "repository_job_id": run.run_identity,
        "iteration_index": prepared["iteration_index"], "iteration_id": prepared["iteration_id"],
        "preparation_digest": prepared["preparation_digest"], "contact_state": "not_started" if started is None else "started",
        "source_preview_path": "/api/workflows/repo-repair/" + run.run_identity + "/source-preview",
        "verified": False, "no_learning": True}


async def repository_review_projection(db, *, task, attempt, owner):
    from sqlalchemy import select
    from src.db.models import WorkflowRunState
    from src.workflows.general_task_guard import child_binding, read_manifest
    from src.workflows.job_runtime import _binding, DurableJobLeaseError
    if task.capability_id != "agent.task.v1" or attempt is None or not attempt.workflow_run_id:
        return None
    if (task.owner_principal_id, task.owner_session_id) != (owner.principal_id, owner.session_id):
        raise DurableJobLeaseError("original repository discovery owner changed")
    parent = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == attempt.workflow_run_id))
    manifest = read_manifest(parent) if parent is not None else None
    if manifest is None:
        return None
    for identity in manifest.admitted_invocation_ids:
        child = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == identity))
        if child is None or json.loads(child.arguments_json).get("tool_id") != "repository_work":
            continue
        binding = child_binding(child)
        key = _binding(owner_principal_id=binding.owner_principal_id, goal_id=binding.goal_id,
            goal_revision=binding.goal_revision, idempotency_scope="original-repository-child", dedupe_key=identity)
        run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.idempotency_binding == key))
        if run is None:
            return None
        original, *_ = read_repository_original(run)
        if original["native_binding"] != binding.model_dump(mode="json") or binding.task_id != task.task_id:
            raise DurableJobLeaseError("original repository discovery binding changed")
        projection = await _prepared_repository_projection(db, run, owner=owner)
        return {name: projection[name] for name in ("native_child_id", "repository_job_id",
            "iteration_index", "iteration_id", "preparation_digest", "contact_state", "source_preview_path")}
    return None


async def repository_source_preview(service, jobs, *, job_id, owner):
    from src.model_fabric.effective_policy import configuration_mutation_lock
    from src.workflows.job_runtime import DurableJobLeaseError
    from src.workflows.repo_repair import _digest_bytes, _canonical_bytes
    async with configuration_mutation_lock:
        config = _assert_task_publication_configuration(service)
        context = await _repository_precontact(service, jobs, job_id=job_id, owner=owner)
        projection = await _prepared_repository_projection(None, context["run"], owner=owner)
        prepared = _repository_record(context["run"], "repository:prepared:" + projection["iteration_id"])
        body_bytes = service._read_private_artifact(prepared["request_body_artifact_ref"],
            expected_digest=prepared["request_body_digest"])
        body = json.loads(body_bytes)
        if (_source_digest(body) != prepared["request_body_digest"]
                or len(_canonical_bytes(body)) != prepared["combined_input_bytes"]
                or prepared["executor_profile_digest"] != _source_digest(config.model_dump(mode="json"))):
            raise DurableJobLeaseError("original finalized repository preparation changed")
        envelope_bytes = service._read_private_artifact(prepared["egress_envelope_artifact_ref"],
            expected_digest=prepared["egress_envelope_digest"])
        envelope = json.loads(envelope_bytes)
        if _digest_bytes(_canonical_bytes(envelope["diagnostics"])) != prepared["diagnostics_digest"]:
            raise DurableJobLeaseError("original repository diagnostics changed")
        async with jobs._session() as db:
            remaining = await _repository_remaining(db, context)
        from src.llm_runtime import build_model_kwargs
        kwargs = build_model_kwargs(temperature=0.2, max_tokens=4096, runtime_path="strategist_agent")
        return {"job_id": job_id, "status": context["run"].status, "revision": context["run"].revision,
            "recovery_action": "review_code_egress" if projection["contact_state"] == "not_started" else "refresh_repair_status",
            "repository_review": {key: value for key, value in projection.items() if key not in
                {"awaiting_repository_consent", "native_execution", "verified", "no_learning"}},
            "source_packet": {"packet_id": prepared["source_packet_id"], "state": "verified",
                "repository_ref": context["work"].repository_ref,
                "source_manifest_sha256": prepared["source_manifest_digest"],
                "artifact_sha256": prepared["source_packet_artifact_digest"],
                "selected_files": envelope["source_packet"]["files"], "omissions": []},
            "egress": {"runtime_path": "strategist_agent", "effective_profile_id": kwargs.get("runtime_profile"),
                "effective_upstream": "openrouter", "maximum_input_bytes": 65536, "maximum_output_tokens": 4096,
                "request_body": body, "request_body_digest": prepared["request_body_digest"],
                "request_route_digest": prepared["request_route_digest"],
                "egress_envelope_digest": prepared["egress_envelope_digest"],
                "diagnostics_digest": prepared["diagnostics_digest"], "diagnostics": envelope["diagnostics"],
                "redaction_version": prepared["redaction_version"], "combined_input_bytes": prepared["combined_input_bytes"],
                "original_deadline_at": prepared["original_deadline_at"], **remaining},
            "provider_contacted": False, "operator_visible": True}


@dataclass(frozen=True, slots=True)
class _RepositoryFirstStartTicket:
    source: Any
    jobs: Any
    binding: GeneralTaskNativeChildBindingV1
    job_id: str
    child_owner: str
    child_fence: int
    preparation_digest: str
    consent_id: str
    iteration_id: str
    used: bool = field(default=False, repr=False)
    _seal: object = field(default=None, repr=False, compare=False)


async def consume_repository_first_start(ticket, *, service, jobs, binding, child_owner):
    from src.workflows.job_runtime import DurableJobLeaseError, _as_utc, _utc_now
    if (type(ticket) is not _RepositoryFirstStartTicket or ticket._seal is not _SEAL or ticket.used
            or ticket.source is not service.repository_source_service or ticket.jobs is not jobs
            or ticket.binding != binding or ticket.child_owner != child_owner):
        raise DurableJobLeaseError("actual single-use original repository first start required")
    async with jobs._session() as db:
        child = await jobs._fetch(db, binding.invocation_id)
        run = await jobs._fetch(db, ticket.job_id)
        original, *_ = read_repository_original(run)
        intent = _repository_record(run, "repository:callback-start:" + ticket.iteration_id)
        if (intent is None or intent["preparation_digest"] != ticket.preparation_digest
                or intent["consent_id"] != ticket.consent_id or intent["native_child_owner"] != child_owner
                or original["native_binding"] != binding.model_dump(mode="json")
                or child.status != "running" or child.attempt_count != 1
                or child.lease_owner != child_owner or child.fencing_token != ticket.child_fence
                or _as_utc(child.lease_expires_at) <= _utc_now()
                or _as_utc(run.deadline_at) <= _utc_now()):
            raise DurableJobLeaseError("original repository first-start binding changed")
    object.__setattr__(ticket, "used", True)
    return await jobs.get_job(binding.invocation_id)


async def grant_repository_iteration_consent(service, jobs, *, job_id, owner, request,
        general_task_service, principal):
    from src.api.workflows import RepoRepairEgressConsentRequest
    from src.work_board.general_task import GeneralTaskService
    from src.workflows.repo_repair import RepoRepairService, RepoRepairError, _canonical_bytes
    from src.model_fabric.effective_policy import configuration_mutation_lock
    from src.work_board.repository import _begin_sqlite_immediate
    from src.workflows.job_runtime import _as_utc, _utc_now, _digest, _canonical, DurableJobLeaseError
    from src.db.models import RepoRepairEgressConsent
    from sqlalchemy import select
    from src.llm_runtime import build_model_kwargs
    if (type(general_task_service) is not GeneralTaskService or not general_task_service.started
            or type(request) is not RepoRepairEgressConsentRequest or not request.is_repository_iteration_variant
            or request.acknowledged_selected_source is not True or request.acknowledged_diagnostics is not True
            or not principal.authenticated or principal.revoked
            or principal.principal_id != owner.principal_id or principal.operator_session_id != owner.session_id):
        raise RepoRepairError("repository_iteration_consent_authority_invalid", "Current original source owner and exact acknowledgments required")
    source = general_task_service.repository_source_service
    if (type(source) is not RepoRepairService or type(service) is not RepoRepairService
            or source._workspace() != service._workspace() or source.sandbox.config != service.sandbox.config):
        raise RepoRepairError("repository_source_owner_changed", "Restore the actual original source service")
    source.jobs = jobs
    async with configuration_mutation_lock:
        config = _assert_task_publication_configuration(source)
        context = await _repository_precontact(source, jobs, job_id=job_id, owner=owner)
        run = context["run"]
        projection = await _prepared_repository_projection(None, run, owner=owner)
        prepared = _repository_record(run, "repository:prepared:" + request.expected_iteration_id)
        if prepared is None:
            raise RepoRepairError("repository_iteration_preparation_changed", "Refresh the exact original preparation")
        comparisons = {
            "iteration_index": request.expected_iteration_index, "iteration_id": request.expected_iteration_id,
            "preparation_digest": request.expected_preparation_digest, "request_body_digest": request.expected_request_body_digest,
            "request_route_digest": request.expected_request_route_digest, "egress_envelope_digest": request.expected_egress_envelope_digest,
            "diagnostics_digest": request.expected_diagnostics_digest, "redaction_version": request.expected_redaction_version,
            "source_packet_artifact_digest": request.source_packet_digest,
            "source_manifest_digest": request.expected_source_manifest_digest}
        if any(prepared.get(key) != value for key, value in comparisons.items()):
            raise RepoRepairError("repository_iteration_consent_binding_changed", "Review the current exact preparation")
        started = _repository_record(run, "repository:callback-start:" + request.expected_iteration_id)
        if started is not None:
            if started.get("request_key") != request.idempotency_key:
                raise RepoRepairError("repository_callback_already_started", "The original callback cannot start again")
            return {"job_id": job_id, "status": run.status, "consent_id": started["consent_id"],
                "idempotent_replay": True, "repository_review": projection, "operator_visible": True}
        if run.revision != request.expected_job_revision:
            raise RepoRepairError("repair_job_revision_stale", "Refresh the source preview")
        body = json.loads(source._read_private_artifact(prepared["request_body_artifact_ref"],
            expected_digest=prepared["request_body_digest"]))
        envelope = json.loads(source._read_private_artifact(prepared["egress_envelope_artifact_ref"],
            expected_digest=prepared["egress_envelope_digest"]))
        candidate = await source.prepare_iteration_egress(iteration_id=prepared["iteration_id"],
            source_payload=envelope["source_packet"], stdout=envelope["diagnostics"]["stdout"], stderr=envelope["diagnostics"]["stderr"])
        kwargs = build_model_kwargs(temperature=0.2, max_tokens=4096, runtime_path="strategist_agent")
        model = source.model_factory(**kwargs)
        finalized = source.finalize_iteration_egress(candidate, model=model, response_format=body["response_format"])
        import asyncio
        preflight = await asyncio.to_thread(_iterative_executor_preflight, source, context["compiled"])
        if (finalized["serialized_request_sha256"] != prepared["request_body_digest"]
                or finalized["request_route_digest"] != prepared["request_route_digest"]
                or finalized["redaction_version"] != prepared["redaction_version"]
                or len(_canonical_bytes(body)) != prepared["combined_input_bytes"]
                or str(kwargs.get("runtime_profile") or "") != request.expected_profile_id
                or _source_digest(config.model_dump(mode="json")) != prepared["executor_profile_digest"]
                or not preflight.ok or preflight.posture_digest != prepared["executor_posture_digest"]):
            raise RepoRepairError("repository_finalized_request_changed", "Source, diagnostics, route or finalized model request changed")
        adapter = getattr(general_task_service, "repository_work_adapter", None)
        if (getattr(adapter, "__self__", None) is not source
                or getattr(adapter, "__func__", None) is not RepoRepairService.native_iteration_adapter):
            raise RepoRepairError("repository_native_adapter_unavailable", "The fixed original source adapter is unavailable")
        expiry = min(_as_utc(run.deadline_at), context["group"].original_deadline_at,
            _as_utc(run.lease_expires_at), _as_utc(context["child"].lease_expires_at),
            _as_utc(context["authority"].attempt.lease_expires_at), _utc_now() + timedelta(minutes=30))
        digest_payload = {"preparation": prepared, "owner": owner.model_dump(mode="json"),
            "profile": request.expected_profile_id, "expires_at": expiry.isoformat(), "request_key": request.idempotency_key}
        async with jobs._session() as db:
            await _begin_sqlite_immediate(db)
            await _recheck_repository_sql(db, context)
            await _repository_remaining(db, context)
            current = await jobs._fetch(db, job_id)
            existing = await db.scalar(select(RepoRepairEgressConsent).where(
                RepoRepairEgressConsent.owner_principal_id == owner.principal_id,
                RepoRepairEgressConsent.owner_session_id == owner.session_id,
                RepoRepairEgressConsent.request_key == request.idempotency_key))
            if existing is not None:
                raise RepoRepairError("egress_consent_idempotency_conflict", "A consent key cannot change or restart its original callback")
            consent = RepoRepairEgressConsent(owner_principal_id=owner.principal_id, owner_session_id=owner.session_id,
                work_board_task_id=prepared["repository_task_id"], work_board_attempt_id=prepared["repository_attempt_id"],
                workflow_run_id=job_id, source_packet_id=prepared["source_packet_id"],
                source_digest=prepared["source_manifest_digest"], source_manifest_digest=prepared["source_manifest_digest"],
                goal_id=run.goal_id, goal_revision=run.goal_revision, input_digest=run.input_digest,
                effective_profile_id=request.expected_profile_id, effective_upstream="openrouter", runtime_path="strategist_agent",
                iteration_id=prepared["iteration_id"], egress_envelope_schema="seraph.repo_iteration_egress.v1",
                egress_envelope_artifact_ref=prepared["egress_envelope_artifact_ref"], egress_envelope_sha256=prepared["egress_envelope_digest"],
                diagnostics_sha256=prepared["diagnostics_digest"], redaction_version=prepared["redaction_version"],
                serialized_request_sha256=prepared["request_body_digest"], combined_input_bytes=prepared["combined_input_bytes"],
                diagnostics_acknowledged=True, maximum_input_bytes=65536, maximum_output_tokens=4096, expires_at=expiry,
                consent_digest=_source_digest(digest_payload), request_key=request.idempotency_key,
                request_digest=_source_digest(request.model_dump(mode="json")))
            db.add(consent)
            await db.flush()
            intent = {"schema": "seraph.repository_callback_start.v1", "iteration_id": prepared["iteration_id"],
                "preparation_digest": prepared["preparation_digest"], "consent_id": consent.id,
                "consent_digest": consent.consent_digest, "native_child_owner": context["child"].lease_owner,
                "native_child_fence": context["child"].fencing_token,
                "native_child_lease_expires_at": _as_utc(context["child"].lease_expires_at).isoformat(),
                "request_key": request.idempotency_key, "request_digest": consent.request_digest}
            _append_repository_record(current, "repository:callback-start:" + prepared["iteration_id"], intent,
                inventory=repository_checkpoint_inventory(current, context["work"]))
            current.revision += 1
            await db.commit()
        ticket = _RepositoryFirstStartTicket(source=source, jobs=jobs, binding=context["binding"], job_id=job_id,
            child_owner=context["child"].lease_owner, child_fence=context["child"].fencing_token,
            preparation_digest=prepared["preparation_digest"], consent_id=consent.id,
            iteration_id=prepared["iteration_id"], _seal=_SEAL)
    from src.work_board.general_task_native import run_native_step
    try:
        outcome, _artifact, _reference = await run_native_step(general_task_service, jobs, ticket.binding,
            child_owner=ticket.child_owner, principal=principal, repository_first_start=ticket)
    except BaseException:
        # A committed invocation intent is never replayed. Preserve the
        # physical lane and real callback handle until their owners prove
        # closure; a disconnected HTTP caller is not such proof.
        import asyncio
        async def quarantine_original():
            current = await jobs.get_job(job_id)
            if current and current["status"] == "running":
                await jobs.transition_job(job_id, "unknown_external_effect",
                    owner=context["run"].lease_owner,
                    fencing_token=context["run"].fencing_token, expected_revision=current["revision"],
                    reason="repository_callback_closure_unproven",
                    result={"no_learning": True, "operator_action": "reconcile_original_callback",
                        "iteration_id": ticket.iteration_id})
        quarantine = asyncio.create_task(quarantine_original())
        source._iterative_model_callbacks["quarantine:" + ticket.iteration_id] = quarantine
        await asyncio.shield(quarantine)
        raise
    return {"job_id": job_id, "consent_id": consent.id, "repository_outcome": outcome, "operator_visible": True}


async def run_repository_iteration(service, *, jobs, binding, descriptor, inputs, step,
        child_owner, principal, fencing_token, approved_resume, original_deadline, phase_timeout,
        repository_first_start=None):
    """Actual fixed callback: one original, consented, accounted model phase."""
    import asyncio
    from dataclasses import replace
    from src.workflows.repo_repair import (RepoRepairError, _model_response_content, _parse_model_json,
        _digest_bytes, _canonical_bytes, _repair_test_args, REPO_REPAIR_APPROVAL_TOOL, REPO_REPAIR_APPROVAL_ACTION)
    from src.workflows.job_runtime import _as_utc, _utc_now, _digest, DurableJobLeaseError
    from src.work_board.contracts import WorkBoardOwner
    from src.work_board.repository import _begin_sqlite_immediate
    from src.db.models import InferenceCostReservation, RepoRepairProposal
    from src.model_fabric.accounting import bind_repository_iteration_accounting
    from src.model_fabric import bind_remote_inference_receipt
    from src.model_fabric.caller_context import build_canonical_inference_context
    from src.approval.runtime import set_runtime_context, reset_runtime_context, get_current_approval_mode
    from src.llm_runtime import build_model_kwargs
    if (approved_resume or type(repository_first_start) is not _RepositoryFirstStartTicket
            or repository_first_start._seal is not _SEAL or not repository_first_start.used
            or repository_first_start.source is not service or repository_first_start.binding != binding
            or repository_first_start.child_owner != child_owner or repository_first_start.child_fence != fencing_token):
        raise DurableJobLeaseError("actual original consumed repository start required")
    ticket = repository_first_start
    owner = WorkBoardOwner(principal_id=binding.owner_principal_id, session_id=binding.original_root_id)
    context = await _repository_precontact(service, jobs, job_id=ticket.job_id, owner=owner)
    run = context["run"]
    prepared = _repository_record(run, "repository:prepared:" + ticket.iteration_id)
    if prepared["preparation_digest"] != ticket.preparation_digest:
        raise DurableJobLeaseError("original callback preparation changed")
    body = json.loads(service._read_private_artifact(prepared["request_body_artifact_ref"],
        expected_digest=prepared["request_body_digest"]))
    witness_payload = {"group": context["group"].model_dump(mode="json"),
        "repository_job_id": ticket.job_id, "repository_attempt_id": prepared["repository_attempt_id"],
        "repository_fence": prepared["repository_fence"], "parent_task_id": binding.task_id,
        "parent_attempt_id": binding.attempt_id, "native_invocation_id": binding.invocation_id,
        "iteration_index": prepared["iteration_index"], "iteration_id": ticket.iteration_id,
        "operation_id": "remote:repo-work:" + ticket.iteration_id,
        "original_deadline_at": context["original"]["original_deadline_at"],
        "original_max_cost_microusd": context["original"]["original_max_cost_microusd"],
        "source_checkpoint_digest": _digest(context["original"])}
    async with jobs._session() as db:
        await _begin_sqlite_immediate(db)
        await _recheck_repository_sql(db, context)
        await _repository_remaining(db, context)
        current = await jobs._fetch(db, ticket.job_id)
        _append_repository_record(current, "repository:iteration:" + ticket.iteration_id,
            {"phase": "model_ready", "accounting_binding": witness_payload,
             "payload_digest": prepared["request_body_digest"]},
            inventory=repository_checkpoint_inventory(current, context["work"]))
        _append_repository_record(current, "repository:request:" + ticket.iteration_id,
            {"operation_id": witness_payload["operation_id"], "iteration_id": ticket.iteration_id,
             "request_body_digest": prepared["request_body_digest"],
             "request_route_digest": prepared["request_route_digest"],
             "request_artifact_ref": prepared["request_body_artifact_ref"],
             "consent_id": ticket.consent_id, "preparation_digest": ticket.preparation_digest},
            inventory=repository_checkpoint_inventory(current, context["work"]))
        current.revision += 1
        await db.commit()
    async with jobs._session() as db:
        canonical = await stage_repository_canonical_source(service, db,
            repository_job_id=ticket.job_id, native_invocation_id=binding.invocation_id, consent_id=ticket.consent_id)
    witness = RepositoryIterationAccountingWitness(**{**witness_payload, "group": context["group"],
        "original_deadline_at": _as_utc(context["run"].deadline_at)},
        _request_body_digest=prepared["request_body_digest"], _request_route_digest=prepared["request_route_digest"],
        _canonical_source=canonical, _seal=_SEAL)
    model = service.model_factory(**build_model_kwargs(temperature=0.2, max_tokens=4096,
        runtime_path="strategist_agent"))
    scoped_principal = replace(principal, job_id=ticket.job_id)
    inference_context = build_canonical_inference_context("strategist_agent", payload=body["messages"],
        output_tokens=4096, timeout_seconds=min(120.0, phase_timeout), principal=scoped_principal,
        session_id=scoped_principal.session_id, job_id=ticket.job_id,
        transformation_digest=_source_digest({"preparation": prepared["preparation_digest"], "consent": ticket.consent_id}),
        redaction_applied=True)
    tokens = set_runtime_context(scoped_principal.session_id, get_current_approval_mode(), trust_principal=scoped_principal)
    try:
        with bind_remote_inference_receipt(repository=jobs, job_id=ticket.job_id,
                owner=run.lease_owner, fencing_token=run.fencing_token, repository_iteration_witness=witness), \
                bind_repository_iteration_accounting(witness):
            callback = asyncio.create_task(asyncio.to_thread(model.generate, body["messages"],
                response_format=body["response_format"], request_context=inference_context))
            service._iterative_model_callbacks[ticket.iteration_id] = callback
            response = await asyncio.shield(callback)
    finally:
        reset_runtime_context(tokens)
    if not callback.done() or callback.cancelled() or callback.exception() is not None:
        raise DurableJobLeaseError("actual model callback return is unproven")
    content = _model_response_content(response)
    if await service._scan_secrets(content) != content:
        raise RepoRepairError("model_output_secret_detected", "Model output matched protected secret material")
    output = _parse_model_json(content)
    envelope = json.loads(service._read_private_artifact(prepared["egress_envelope_artifact_ref"],
        expected_digest=prepared["egress_envelope_digest"]))
    if (output.base_snapshot_sha256 != envelope["source_packet"]["base_snapshot_sha256"]
            or not set(output.allowed_paths).issubset(context["work"].allowed_paths)
            or _repair_test_args(tuple(output.test_args), output.allowed_paths) !=
                _repair_test_args(tuple(context["compiled"].test_args), context["compiled"].allowed_paths)):
        raise RepoRepairError("model_patch_authority_changed", "Model proposal changed the original base, paths or checks")
    patch = (output.patch_unified_diff.rstrip("\n") + "\n").encode()
    from src.execution.repo_sandbox import _patch_paths_from_diff
    _patch_paths_from_diff(patch, context["work"].allowed_paths)
    patch_digest = _digest_bytes(patch)
    response_ref, response_digest = service._write_private_artifact(
        "artifacts/repo-repair/model/iteration-" + ticket.iteration_id + "-response.json",
        _canonical_bytes({"content": content}))
    patch_ref, _ = service._write_private_artifact(
        "artifacts/repo-repair/patch/iteration-" + ticket.iteration_id + ".diff", patch)
    async with jobs._session() as db:
        cost = await db.get(InferenceCostReservation, witness.operation_id)
        if (cost is None or cost.state != "settled" or cost.contact_started_at is None
                or cost.payload_digest != prepared["request_body_digest"]
                or type(cost.actual_cost_microusd) is not int or cost.actual_cost_microusd < 0):
            raise DurableJobLeaseError("actual original model response accounting is not settled")
    approval_id = "repository-approval:" + ticket.iteration_id
    from src.workflows.repo_repair import (_sandbox_authority_payload,
        _proposal_authority_payload, _authority_digest, _repair_approval_fingerprint)
    posture = await asyncio.to_thread(_iterative_executor_preflight, service, context["compiled"])
    if not posture.ok or posture.posture_digest != prepared["executor_posture_digest"]:
        raise DurableJobLeaseError("fixed iterative executor is blocked")
    proposal = RepoRepairProposal(proposal_id="repository-proposal:" + ticket.iteration_id,
        operation_key="repository-proposal:" + ticket.iteration_id,
        owner_principal_id=owner.principal_id, owner_session_id=owner.session_id,
        work_board_task_id=prepared["repository_task_id"],
        work_board_attempt_id=prepared["repository_attempt_id"], workflow_run_id=ticket.job_id,
        goal_id=run.goal_id, goal_revision=run.goal_revision,
        repository_ref=context["work"].repository_ref,
        base_snapshot_digest=output.base_snapshot_sha256, source_packet_id=prepared["source_packet_id"],
        source_digest=prepared["source_manifest_digest"], model_runtime_path="strategist_agent",
        model_profile_id=cost.profile_id, model_request_digest=prepared["request_body_digest"],
        model_output_digest=response_digest, model_response_artifact_id=response_ref.removeprefix("workspace-json:"),
        model_response_artifact_sha256=response_digest, patch_artifact_id=patch_ref.removeprefix("workspace-json:"),
        patch_sha256=patch_digest, allowed_paths_json=json.dumps(sorted(output.allowed_paths)),
        test_args_json=json.dumps(list(_repair_test_args(tuple(output.test_args), output.allowed_paths))),
        request_digest=_source_digest({"original": context["original"], "iteration": ticket.iteration_id,
            "request": prepared["request_body_digest"], "response": response_digest, "patch": patch_digest}),
        approval_id=approval_id, status="awaiting_approval", expires_at=_as_utc(run.deadline_at),
        safe_metadata_json=json.dumps({"summary": output.summary, "expected_outcome": output.expected_outcome,
            "memory_status": "no_learning", "iteration_id": ticket.iteration_id,
            "source_checkpoint_digest": _digest(context["original"]),
            "sandbox": _sandbox_authority_payload(service.sandbox, posture)}))
    proposal.authority_digest = _authority_digest(_proposal_authority_payload(proposal))
    approval_fingerprint = _repair_approval_fingerprint(proposal, _as_utc(run.deadline_at))
    proposal.approval_fingerprint = approval_fingerprint
    from src.approval.repository import approval_repository
    approval = await approval_repository.get_or_create_pending(session_id=owner.session_id,
        tool_name=REPO_REPAIR_APPROVAL_TOOL, risk_level="high", request_id=approval_id,
        fingerprint=approval_fingerprint, summary="Review the exact bounded repository patch",
        details={"action": REPO_REPAIR_APPROVAL_ACTION, "approval_owner_principal_id": owner.principal_id,
            "approval_owner_operator_session_id": owner.session_id, "approval_execution_owner_principal_id": owner.principal_id,
            "approval_execution_session_id": owner.session_id, "approval_operator_principal_id": owner.principal_id,
            "approval_conversation_id": owner.session_id, "durable_job_id": ticket.job_id,
            "iteration_id": ticket.iteration_id, "proposal_id": proposal.proposal_id,
            "proposal_revision": proposal.revision, "authority_digest": proposal.authority_digest,
            **_sandbox_authority_payload(service.sandbox, posture),
            "patch_sha256": patch_digest,
            "expires_at": _as_utc(run.deadline_at).timestamp(), "memory_status": "no_learning"})
    async with jobs._session() as db:
        await _begin_sqlite_immediate(db)
        current = await jobs._fetch(db, ticket.job_id)
        inventory = repository_checkpoint_inventory(current, context["work"])
        if await db.get(RepoRepairProposal, proposal.proposal_id) is not None:
            raise DurableJobLeaseError("original iteration proposal is single use")
        db.add(proposal)
        await db.flush()
        closed = {"schema": "repository.model_response.v1", "operation_id": witness.operation_id,
            "request_body_digest": prepared["request_body_digest"], "response_artifact_ref": response_ref,
            "response_artifact_digest": response_digest, "callback_returned": True,
            "accounting_digest": _source_digest(cost.model_dump(mode="json")), "returned_at": _utc_now().isoformat()}
        closed["callback_quiescence_digest"] = _source_digest(closed)
        _append_repository_record(current, "repository:response:" + ticket.iteration_id, closed, inventory=inventory)
        _append_repository_record(current, "repository:accounting:" + ticket.iteration_id,
            {"operation_id": cost.operation_id, "accounting_digest": closed["accounting_digest"],
             "state": cost.state, "contact_started_at": _as_utc(cost.contact_started_at).isoformat(),
             "actual_cost_microusd": cost.actual_cost_microusd, "bound_microusd": cost.bound_microusd,
             "original_group_digest": prepared["original_group_digest"]}, inventory=inventory)
        _append_repository_record(current, "repository:patch:" + ticket.iteration_id,
            {"patch_artifact_ref": patch_ref, "patch_sha256": patch_digest,
             "allowed_paths": output.allowed_paths, "test_args": output.test_args}, inventory=inventory)
        _append_repository_record(current, "repository:approval:" + ticket.iteration_id,
            {"approval_id": approval.id, "fingerprint": approval_fingerprint,
             "expires_at": _as_utc(run.deadline_at).isoformat(), "patch_sha256": patch_digest}, inventory=inventory)
        current.revision += 1
        await db.commit()
    async with jobs._session() as db:
        canonical = await stage_repository_canonical_source(service, db,
            repository_job_id=ticket.job_id, native_invocation_id=binding.invocation_id, consent_id=ticket.consent_id)
    from src.workflows.general_task_guard import issue_repository_child_wait_witness
    wait = issue_repository_child_wait_witness(native_binding=binding, source_binding=canonical,
        repository_job_id=ticket.job_id, repository_attempt_id=prepared["repository_attempt_id"],
        repository_fence=prepared["repository_fence"], iteration_index=prepared["iteration_index"],
        iteration_id=ticket.iteration_id, source_checkpoint_digest=_digest(context["original"]),
        request_body_digest=prepared["request_body_digest"], response_readback_digest=response_digest,
        callback_quiescence_digest=closed["callback_quiescence_digest"])
    return {"wait_witness": wait}


async def certify_repository_callback_return(ticket, result, *, service, jobs):
    """Called by the fixed native owner only after its actual adapter await."""
    from src.workflows.job_runtime import DurableJobLeaseError, _utc_now, _digest
    from src.work_board.repository import _begin_sqlite_immediate
    from src.workflows.general_task_guard import (RepositoryChildWaitWitness,
        _assert_repository_child_wait_witness_shape, issue_repository_child_wait_witness)
    if (type(ticket) is not _RepositoryFirstStartTicket or ticket._seal is not _SEAL or not ticket.used
            or ticket.source is not service.repository_source_service or ticket.jobs is not jobs
            or not isinstance(result, dict) or set(result) != {"wait_witness"}):
        raise DurableJobLeaseError("actual original adapter return required")
    provisional = result["wait_witness"]
    _assert_repository_child_wait_witness_shape(provisional)
    callback = ticket.source._iterative_model_callbacks.get(ticket.iteration_id)
    if callback is None or not callback.done() or callback.cancelled() or callback.exception() is not None:
        raise DurableJobLeaseError("actual model callback is not quiescent")
    async with jobs._session() as db:
        canonical = await stage_repository_canonical_source(ticket.source, db,
            repository_job_id=ticket.job_id, native_invocation_id=ticket.binding.invocation_id,
            consent_id=ticket.consent_id)
    from src.workflows.job_runtime import _canonical
    canonical_ref, canonical_digest = ticket.source._write_private_artifact(
        "artifacts/repo-repair/model/iteration-" + ticket.iteration_id + "-canonical-source.json",
        _canonical(canonical.projection()).encode())
    readback = ticket.source._read_private_artifact(canonical_ref, expected_digest=canonical_digest)
    if readback != _canonical(canonical.projection()).encode():
        raise DurableJobLeaseError("literal original canonical snapshot readback changed")
    async with jobs._session() as db:
        await _begin_sqlite_immediate(db)
        run = await jobs._fetch(db, ticket.job_id)
        original, work, *_ = read_repository_original(run)
        response = _repository_record(run, "repository:response:" + ticket.iteration_id)
        if (response is None or response["response_artifact_digest"] != provisional.response_readback_digest
                or provisional.native_binding != ticket.binding):
            raise DurableJobLeaseError("original adapter response readback changed")
        closure = {"schema": "repository.adapter_return.v1", "iteration_id": ticket.iteration_id,
            "native_invocation_id": ticket.binding.invocation_id, "child_fence": ticket.child_fence,
            "preparation_digest": ticket.preparation_digest, "response_readback_digest": provisional.response_readback_digest,
            "provider_callback_digest": response["callback_quiescence_digest"],
            "canonical_source_artifact_ref": canonical_ref,
            "canonical_source_artifact_digest": canonical_digest,
            "original_parent_revision": canonical.parent_revision,
            "original_child_revision": canonical.child_revision,
            "parent_static_digest": _source_digest({k: v for k, v in json.loads(canonical.parent_row_json).items()
                if k not in {"revision", "updated_at", "checkpoint_receipts_json"}}),
            "child_static_digest": _source_digest({k: v for k, v in json.loads(canonical.child_row_json).items()
                if k not in {"revision", "status", "failure_reason", "updated_at", "heartbeat_at"}}),
            "returned_at": _utc_now().isoformat()}
        _append_repository_record(run, "repository:proposal:" + ticket.iteration_id, closure,
            inventory=repository_checkpoint_inventory(run, work))
        run.revision += 1
        await db.commit()
    async with jobs._session() as db:
        canonical = await stage_repository_canonical_source(ticket.source, db,
            repository_job_id=ticket.job_id, native_invocation_id=ticket.binding.invocation_id,
            consent_id=ticket.consent_id)
    return {"wait_witness": issue_repository_child_wait_witness(native_binding=ticket.binding,
        source_binding=canonical, repository_job_id=ticket.job_id,
        repository_attempt_id=provisional.repository_attempt_id, repository_fence=provisional.repository_fence,
        iteration_index=provisional.iteration_index, iteration_id=ticket.iteration_id,
        source_checkpoint_digest=_digest(original), request_body_digest=provisional.request_body_digest,
        response_readback_digest=provisional.response_readback_digest, callback_quiescence_digest=_source_digest(closure))}


async def validate_repository_child_wait_witness(db, witness, *, parent, task,
        attempt, child, manifest, staged_artifact=None, receipt=None, phase):
    """Check the actual adapter return and settled accounting in the C1 writer.

    Physical artifacts were staged by the source owner before this writer.
    Neither a provider response alone nor its provisional witness qualifies.
    """
    from sqlalchemy import select
    from src.db.models import WorkflowRunState, InferenceCostReservation, ApprovalRequest
    from src.workflows.job_runtime import DurableJobLeaseError, _digest, _as_utc, _utc_now
    from src.workflows.general_task_guard import _assert_repository_child_wait_witness_shape
    _assert_repository_child_wait_witness_shape(witness)
    source = witness._source_binding
    assert_repository_canonical_source(source)
    run = await db.scalar(select(WorkflowRunState).where(
        WorkflowRunState.run_identity == witness.repository_job_id))
    if phase != "wait" or run is None:
        raise DurableJobLeaseError("original repository contacted wait required")
    original, work, compiled, group, binding, task_source = read_repository_original(run)
    prepared = _repository_record(run, "repository:prepared:" + witness.iteration_id)
    response = _repository_record(run, "repository:response:" + witness.iteration_id)
    closure = _repository_record(run, "repository:proposal:" + witness.iteration_id)
    approval_binding = _repository_record(run, "repository:approval:" + witness.iteration_id)
    iteration = _repository_record(run, "repository:iteration:" + witness.iteration_id)
    if (any(record is None for record in (prepared, response, closure, approval_binding, iteration))
            or binding != witness.native_binding or binding.invocation_id != child.run_identity
            or binding.parent_job_id != parent.run_identity or binding.task_id != task.task_id
            or binding.attempt_id != attempt.attempt_id
            or _digest(original) != witness.source_checkpoint_digest
            or original["repository_attempt_id"] != witness.repository_attempt_id
            or run.fencing_token != witness.repository_fence
            or prepared["iteration_index"] != witness.iteration_index
            or prepared["request_body_digest"] != witness.request_body_digest
            or response["response_artifact_digest"] != witness.response_readback_digest
            or closure.get("schema") != "repository.adapter_return.v1"
            or closure.get("native_invocation_id") != child.run_identity
            or closure.get("child_fence") != child.fencing_token
            or closure.get("preparation_digest") != prepared["preparation_digest"]
            or closure.get("response_readback_digest") != witness.response_readback_digest
            or closure.get("provider_callback_digest") != response["callback_quiescence_digest"]
            or _source_digest(closure) != witness.callback_quiescence_digest):
        raise DurableJobLeaseError("actual original adapter callback closure changed")
    rows = list((await db.scalars(select(InferenceCostReservation))).all())
    cost = next((row for row in rows if row.operation_id == response["operation_id"]), None)
    if (cost is None or cost.state != "settled" or cost.contact_started_at is None
            or _source_digest(cost.model_dump(mode="json")) != response["accounting_digest"]):
        raise DurableJobLeaseError("original contacted response accounting changed")
    accounting = RepositoryIterationAccountingWitness(
        **{**iteration["accounting_binding"], "group": group,
           "original_deadline_at": _as_utc(run.deadline_at)},
        _canonical_source=source, _request_body_digest=witness.request_body_digest,
        _request_route_digest=prepared["request_route_digest"], _seal=_SEAL)
    await validate_repository_iteration_accounting(db, accounting, run, rows=rows,
        operation_id=cost.operation_id, payload_digest=cost.payload_digest,
        bound_microusd=cost.bound_microusd, deadline_at=cost.deadline_at, already_reserved=True)
    approval = await db.get(ApprovalRequest, approval_binding["approval_id"])
    if (approval is None or approval.status not in {"pending", "approved", "consumed"}
            or approval.fingerprint != approval_binding["fingerprint"]
            or approval.session_id != run.operator_session_id
            or _as_utc(run.deadline_at) <= _utc_now()):
        raise DurableJobLeaseError("exact original pending patch approval required")
    return {"iteration_id": witness.iteration_id, "callback_returned": True,
        "accounting_settled": True, "no_learning": True}


async def recover_repository_wait_witness(service, jobs, *, job_id, owner, iteration_index, _resumed=False):
    """Reissue the original contacted wait after its exact durable successor.

    This reads the source owner's immutable private snapshot and canonical
    journal. A serialized public witness cannot enter this issuer.
    """
    from dataclasses import replace
    from types import SimpleNamespace
    from src.model_fabric.effective_policy import configuration_mutation_lock
    from src.workflows.job_runtime import DurableJobLeaseError, _as_utc, _utc_now, _digest
    from src.workflows.general_task_guard import (read_manifest, _history,
        issue_repository_child_wait_witness, repository_child_wait_checkpoint_id,
        _repository_checkpoint_payload, assert_general_task_child_phase_current)
    from src.work_board.contracts import GENERAL_TASK_MANIFEST_KEY
    from src.work_board.general_task_runtime_artifacts import verify_general_task_manifest
    from src.db.models import WorkBoardTask, WorkBoardAttempt
    from sqlalchemy import select
    async with configuration_mutation_lock:
        _assert_task_publication_configuration(service)
        async with jobs._session() as db:
            run = await jobs._fetch(db, job_id)
            original, work, compiled, group, binding, task_source = read_repository_original(run)
            if (run.owner_principal_id, run.operator_session_id) != (owner.principal_id, owner.session_id):
                raise DurableJobLeaseError("original repository recovery owner changed")
            if (type(iteration_index) is not int or not 1 <= iteration_index <= work.limits.max_iterations
                    or run.status != "running" or _as_utc(run.deadline_at) <= _utc_now()):
                raise DurableJobLeaseError("original repository recovery cutoff or state changed")
            identity = iteration_identity(job_id, original["repository_attempt_id"],
                _source_digest(original["original_input"]), iteration_index)
            closure = _repository_record(run, "repository:proposal:" + identity)
            prepared = _repository_record(run, "repository:prepared:" + identity)
            response = _repository_record(run, "repository:response:" + identity)
            if any(record is None for record in (closure, prepared, response)):
                raise DurableJobLeaseError("actual original callback return is unavailable")
            child = await jobs._fetch(db, binding.invocation_id)
            parent = await jobs._fetch(db, binding.parent_job_id)
            expected_status, expected_reason = ("running", None) if _resumed else ("paused", "repository_child_wait")
            revision_delta = 2 if _resumed else 1
            if (child.status != expected_status or child.failure_reason != expected_reason
                    or parent.status != "paused" or parent.failure_reason != "general_task_native_wait"):
                raise DurableJobLeaseError("original contacted-wait recovery phase required")
            if (child.revision != closure["original_child_revision"] + revision_delta
                    or parent.revision != closure["original_parent_revision"] + 1
                    or _source_digest({k: v for k, v in parent.model_dump(mode="json").items()
                        if k not in {"revision", "updated_at", "checkpoint_receipts_json"}}) != closure["parent_static_digest"]
                    or _source_digest({k: v for k, v in child.model_dump(mode="json").items()
                        if k not in {"revision", "status", "failure_reason", "updated_at", "heartbeat_at"}}) != closure["child_static_digest"]):
                raise DurableJobLeaseError("original contacted-wait metadata changed before private read")
            # Existing current native authority/Root/Goal checks precede any
            # read of the private recovery snapshot.
            await assert_general_task_child_phase_current(db, child)
            snapshot = json.loads(service._read_private_artifact(closure["canonical_source_artifact_ref"],
                expected_digest=closure["canonical_source_artifact_digest"]))
            canonical = _CanonicalRepositorySource(**snapshot, _seal=_SEAL)
            assert_repository_canonical_source(canonical)
            task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == binding.task_id))
            attempt = await db.get(WorkBoardAttempt, binding.attempt_id)
            envelope = await verify_general_task_manifest(db, parent, task, attempt, read_manifest(parent))
            if envelope.repository_source != task_source or envelope.proposal_group != group:
                raise DurableJobLeaseError("original inspected Task recovery binding changed")
            source_facts = json.loads(service._read_private_artifact(task_source.source_artifact_ref,
                expected_digest=task_source.source_artifact_digest))
            if service.recheck_task_source_snapshot(work, source_facts) != compiled:
                raise DurableJobLeaseError("original physical repository source changed")
            old_child, old_parent = json.loads(canonical.child_row_json), json.loads(canonical.parent_row_json)
            current_child, current_parent = child.model_dump(mode="json"), parent.model_dump(mode="json")
            child_changes = {"revision", "status", "failure_reason", "updated_at", "heartbeat_at"}
            parent_changes = {"revision", "updated_at", "checkpoint_receipts_json"}
            if (child.status != expected_status or child.failure_reason != expected_reason
                    or child.revision != old_child["revision"] + revision_delta
                    or parent.revision != old_parent["revision"] + 1
                    or {k: v for k, v in current_child.items() if k not in child_changes} !=
                        {k: v for k, v in old_child.items() if k not in child_changes}
                    or {k: v for k, v in current_parent.items() if k not in parent_changes} !=
                        {k: v for k, v in old_parent.items() if k not in parent_changes}
                    or _as_utc(child.lease_expires_at) <= _utc_now()):
                raise DurableJobLeaseError("only the exact original contacted-wait successor may recover")
            witness = issue_repository_child_wait_witness(native_binding=binding, source_binding=canonical,
                repository_job_id=job_id, repository_attempt_id=original["repository_attempt_id"],
                repository_fence=run.fencing_token, iteration_index=iteration_index, iteration_id=identity,
                source_checkpoint_digest=_digest(original), request_body_digest=prepared["request_body_digest"],
                response_readback_digest=response["response_artifact_digest"],
                callback_quiescence_digest=_source_digest(closure))
            wait_id = repository_child_wait_checkpoint_id(binding, identity)
            expected_wait = _repository_checkpoint_payload(witness, phase="contacted_wait", checkpoint_id=wait_id)
            wait_record = _repository_record(parent, wait_id)
            if wait_record != expected_wait:
                raise DurableJobLeaseError("original canonical contacted wait changed")
            old_history = json.loads(canonical.parent_checkpoint_json)
            current_history = _history(parent)
            if ([item for item in current_history if item["checkpoint_id"] not in {wait_id, GENERAL_TASK_MANIFEST_KEY}] !=
                    [item for item in old_history if item["checkpoint_id"] != GENERAL_TASK_MANIFEST_KEY]):
                raise DurableJobLeaseError("original contacted-wait retained history changed")
            previous = read_manifest(SimpleNamespace(checkpoint_receipts_json=canonical.parent_checkpoint_json))
            expected_manifest = previous.model_copy(update={"manifest_revision": previous.manifest_revision + 1,
                "required_checkpoint_ids": sorted({item["checkpoint_id"] for item in current_history
                    if item["checkpoint_id"].startswith(("general:", "repository:"))})})
            if read_manifest(parent) != expected_manifest:
                raise DurableJobLeaseError("original contacted-wait manifest successor changed")
            def row_json(row):
                return json.dumps(row.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
            recovered = replace(canonical, _contacted_wait_parent_json=row_json(parent),
                _contacted_wait_child_json=row_json(child))
            return replace(witness, _source_binding=recovered)


async def execute_repository_iteration(service, jobs, *, job_id, owner, request, principal):
    """Consume an exact manual patch approval and run the fixed CPU owner."""
    import asyncio
    from sqlalchemy import update
    from src.db.models import RepoRepairProposal, ApprovalRequest
    from src.auth.service import authenticate_principal
    from src.api.workflows import RepoRepairResumeRequest
    from src.model_fabric.effective_policy import configuration_mutation_lock
    from src.workflows.job_runtime import DurableJobLeaseError, _as_utc, _utc_now, _canonical
    from src.work_board.repository import _begin_sqlite_immediate
    from src.workflows.repo_repair import (_repair_approval_fingerprint, _proposal_sandbox_authority,
        _proposal_authority_payload, _authority_digest, REPO_REPAIR_APPROVAL_TOOL, REPO_REPAIR_APPROVAL_ACTION)
    from src.execution.repo_sandbox import RepoSandboxJob, assert_repo_iteration_cleanup_witness
    if type(request) is not RepoRepairResumeRequest:
        raise DurableJobLeaseError("closed repository patch resume request required")
    actual = await authenticate_principal(principal.principal_id)
    if actual.session_id != owner.session_id or principal.principal_id != owner.principal_id:
        raise DurableJobLeaseError("original authenticated repository patch owner required")
    async with jobs._session() as db:
        run = await jobs._fetch(db, job_id)
        original, work, compiled, group, binding, task_source = read_repository_original(run)
        proposal = await db.get(RepoRepairProposal, request.proposal_id)
        approval = await db.get(ApprovalRequest, request.approval_id)
        if (proposal is None or approval is None or proposal.workflow_run_id != job_id
                or proposal.owner_principal_id != owner.principal_id or proposal.owner_session_id != owner.session_id
                or proposal.approval_id != approval.id or proposal.revision != request.expected_proposal_revision
                or run.revision != request.expected_job_revision or proposal.status != "awaiting_approval"
                or approval.status != "approved" or approval.owner_principal_id != owner.principal_id
                or approval.operator_session_id != owner.session_id or approval.session_id != owner.session_id
                or approval.tool_name != REPO_REPAIR_APPROVAL_TOOL or approval.action != REPO_REPAIR_APPROVAL_ACTION
                or approval.expires_at is None or _as_utc(approval.expires_at) <= _utc_now()
                or _as_utc(proposal.expires_at) != _as_utc(run.deadline_at)
                or proposal.authority_digest != _authority_digest(_proposal_authority_payload(proposal))
                or approval.fingerprint != _repair_approval_fingerprint(proposal, _as_utc(approval.expires_at))):
            raise DurableJobLeaseError("exact current original manual patch approval required")
        identity = json.loads(proposal.safe_metadata_json)["iteration_id"]
        prepared = _repository_record(run, "repository:prepared:" + identity)
        if prepared is None or _repository_record(run, "repository:execution:" + identity) is not None:
            raise DurableJobLeaseError("repository process invocation is single use")
        child = await jobs._fetch(db, binding.invocation_id)
        needs_wake = child.status == "paused"
    if needs_wake:
        recovered = await recover_repository_wait_witness(service, jobs, job_id=job_id,
            owner=owner, iteration_index=prepared["iteration_index"])
        parent = await jobs.get_job(binding.parent_job_id)
        child_projection = await jobs.get_job(binding.invocation_id)
        await jobs.resume_repository_child_wait(binding.invocation_id,
            owner=child_projection["lease"]["owner"], expected_parent_revision=parent["revision"],
            expected_child_revision=child_projection["revision"], producer_witness=recovered)
    async with configuration_mutation_lock:
        _assert_task_publication_configuration(service)
        context = await _repository_precontact(service, jobs, job_id=job_id, owner=owner)
        if job_id not in service._iterative_lanes:
            from src.work_board.dispatcher import _reserve_repo_repair_execution_capacity
            service._iterative_lanes[job_id] = await _reserve_repo_repair_execution_capacity(
                jobs=jobs, job_id=job_id, attempt_id=original["repository_attempt_id"],
                workspace_root=str(service._workspace()), owner=context["run"].lease_owner,
                fencing_token=context["run"].fencing_token, authority_digest=context["run"].authority_digest,
                execution_deadline_at=original["original_deadline_at"], expected_revision=context["run"].revision)
            context = await _repository_precontact(service, jobs, job_id=job_id, owner=owner)
        posture = await asyncio.to_thread(_iterative_executor_preflight, service, context["compiled"])
        sandbox = _proposal_sandbox_authority(proposal)
        details = json.loads(approval.details_json)
        if (not posture.ok or posture.posture_digest != sandbox["executor_posture_digest"]
                or sandbox["executor_posture_digest"] != prepared["executor_posture_digest"]
                or sandbox.get("required_permissions") != ["local_host_execution"]
                or details.get("required_permissions") != ["local_host_execution"]
                or details.get("executor_kind") != "local" or details.get("proposal_id") != proposal.proposal_id
                or details.get("patch_sha256") != proposal.patch_sha256):
            raise DurableJobLeaseError("exact approved local host execution posture required")
        patch = service._read_private_artifact("workspace-json:" + proposal.patch_artifact_id,
            expected_digest=proposal.patch_sha256)
        cutoff = min(_as_utc(context["run"].deadline_at), _as_utc(context["child"].lease_expires_at),
            _as_utc(context["authority"].attempt.lease_expires_at))
        remaining = int((cutoff - _utc_now()).total_seconds())
        if remaining < 1:
            raise DurableJobLeaseError("original repository process cutoff expired")
        process_binding = RepoIterationProcessBinding(repository_job_id=job_id,
            repository_attempt_id=original["repository_attempt_id"], repository_fence=context["run"].fencing_token,
            iteration_index=prepared["iteration_index"], iteration_id=identity,
            original_deadline_at=_as_utc(context["run"].deadline_at),
            authority_digest=proposal.authority_digest, base_digest=proposal.base_snapshot_digest, _seal=_SEAL)
        job = RepoSandboxJob(job_id=job_id, repository_root=str(service._workspace() / compiled.repository_path),
            patch_bytes=patch, allowed_paths=tuple(json.loads(proposal.allowed_paths_json)),
            test_args=tuple(json.loads(proposal.test_args_json)), authority_digest=proposal.authority_digest,
            base_digest=proposal.base_snapshot_digest, deadline_seconds=min(180, remaining),
            limits_digest=sandbox["sandbox_limits_digest"], execution_deadline_at=cutoff.isoformat(),
            attempt_id=original["repository_attempt_id"], fencing_token=context["run"].fencing_token,
            expected_posture_digest=posture.posture_digest,
            expected_worker_source_sha256=posture.posture.get("worker_source_sha256", ""),
            expected_interpreter_sha256=posture.posture.get("interpreter_sha256", ""),
            expected_pytest_executable_sha256=posture.posture.get("pytest_executable_sha256", ""),
            expected_pytest_package_sha256=posture.posture.get("pytest_package_sha256", ""), iteration_binding=process_binding)
        async with jobs._session() as db:
            await _begin_sqlite_immediate(db)
            await _recheck_repository_sql(db, context)
            live = await db.get(RepoRepairProposal, proposal.proposal_id, populate_existing=True)
            if live is None or live.model_dump(mode="json") != proposal.model_dump(mode="json"):
                raise DurableJobLeaseError("original patch proposal changed before dispatch")
            changed = await db.execute(update(ApprovalRequest).where(
                ApprovalRequest.id == approval.id, ApprovalRequest.status == "approved",
                ApprovalRequest.fingerprint == approval.fingerprint, ApprovalRequest.owner_principal_id == owner.principal_id,
                ApprovalRequest.operator_session_id == owner.session_id, ApprovalRequest.expires_at > _utc_now()
                ).values(status="consumed", resolved_at=_utc_now()))
            if changed.rowcount != 1:
                raise DurableJobLeaseError("original patch approval consume CAS changed")
            current = await jobs._fetch(db, job_id)
            _append_repository_record(current, "repository:execution:" + identity,
                {"schema": "repository.process_intent.v1", "iteration_id": identity, "proposal_id": proposal.proposal_id,
                 "approval_id": approval.id, "approval_fingerprint": approval.fingerprint, "patch_sha256": proposal.patch_sha256,
                 "process_binding": process_binding.projection(), "request_key": request.idempotency_key},
                inventory=repository_checkpoint_inventory(current, work))
            current.revision += 1
            live.status = "execution_started"
            live.revision += 1
            await db.commit()
        context = await _repository_precontact(service, jobs, job_id=job_id, owner=owner)
        loop = asyncio.get_running_loop()
        async def dispatch_sql_guard():
            async with jobs._session() as db:
                await _begin_sqlite_immediate(db)
                await _recheck_repository_sql(db, context)
                await db.commit()
        def before_dispatch():
            future = asyncio.run_coroutine_threadsafe(dispatch_sql_guard(), loop)
            future.result(timeout=min(10, max(1, remaining)))
        task = asyncio.create_task(asyncio.to_thread(service.sandbox.execute_job, job, before_dispatch=before_dispatch))
        service._iterative_process_callbacks[identity] = task
        try:
            result = await asyncio.shield(task)
            assert_repo_iteration_cleanup_witness(result.get("iteration_cleanup_witness"), job)
        except BaseException:
            # The durable intent and owned transport survive HTTP cancellation.
            # An absent cleanup witness never releases the original root lane.
            service._iterative_lanes[job_id].quarantine(job_id)
            async def quarantine_original():
                current = await jobs.get_job(job_id)
                if current and current["status"] == "running":
                    await jobs.transition_job(job_id, "unknown_external_effect",
                        owner=context["run"].lease_owner, fencing_token=context["run"].fencing_token,
                        expected_revision=current["revision"], reason="repository_process_closure_unproven",
                        result={"no_learning": True, "operator_action": "reconcile_original_process",
                            "iteration_id": identity})
            quarantine = asyncio.create_task(quarantine_original())
            service._iterative_process_callbacks["quarantine:" + identity] = quarantine
            await asyncio.shield(quarantine)
            raise
    cleanup = result.get("iteration_cleanup_witness")
    assert_repo_iteration_cleanup_witness(cleanup, job)
    if not task.done() or task.cancelled() or task.exception() is not None:
        raise DurableJobLeaseError("actual original process owner closure is unproven")
    projection = cleanup.projection()
    prefix = "artifacts/repo-repair/model/iteration-" + identity
    cleanup_ref, cleanup_digest = service._write_private_artifact(prefix + "-cleanup.json", _canonical(projection).encode())
    manifest_ref, manifest_digest = service._write_private_artifact(prefix + "-readback.json", result["outputs"]["readback.json"])
    if service._read_private_artifact(manifest_ref, expected_digest=manifest_digest) != result["outputs"]["readback.json"]:
        raise DurableJobLeaseError("literal original process readback changed")
    # These bytes come from the actual supervised command, rather than a
    # model summary. The next preparation rescans and redacts them afresh.
    outputs = result["outputs"]
    diagnostics = {"iteration_id": identity,
        "stdout": outputs["pytest.stdout"].decode("utf-8", errors="replace"),
        "stderr": outputs["pytest.stderr"].decode("utf-8", errors="replace"),
        "stdout_raw_sha256": hashlib.sha256(outputs["pytest.stdout"]).hexdigest(),
        "stderr_raw_sha256": hashlib.sha256(outputs["pytest.stderr"]).hexdigest(),
        "cumulative_diff": outputs["diff.patch"].decode("utf-8", errors="strict"),
        "cumulative_diff_sha256": hashlib.sha256(outputs["diff.patch"]).hexdigest()}
    diagnostics_ref, diagnostics_digest = service._write_private_artifact(prefix + "-diagnostics.json",
        _canonical(diagnostics).encode())
    if service._read_private_artifact(diagnostics_ref, expected_digest=diagnostics_digest) != _canonical(diagnostics).encode():
        raise DurableJobLeaseError("literal original command diagnostics readback changed")
    async with jobs._session() as db:
        await _begin_sqlite_immediate(db)
        await _recheck_repository_sql(db, context)
        current = await jobs._fetch(db, job_id)
        inventory = repository_checkpoint_inventory(current, work)
        _append_repository_record(current, "repository:cleanup:" + identity,
            {"artifact_ref": cleanup_ref, "artifact_digest": cleanup_digest, "cleanup_proven": True,
             "iteration_id": identity, "status": result["status"]}, inventory=inventory)
        _append_repository_record(current, "repository:readback:" + identity,
            {"artifact_ref": manifest_ref, "artifact_digest": manifest_digest, "status": result["status"],
             "manifest_digest": _source_digest(result["manifest"]), "patch_sha256": proposal.patch_sha256,
             "diagnostics_artifact_ref": diagnostics_ref, "diagnostics_artifact_digest": diagnostics_digest}, inventory=inventory)
        current.revision += 1
        live = await db.get(RepoRepairProposal, proposal.proposal_id)
        live.status = "execution_verified"
        live.last_receipt_id = "repository:readback:" + identity
        live.revision += 1
        await db.commit()
    outcome = {"job_id": job_id, "iteration_id": identity, "status": result["status"], "cleanup_proven": True,
        "manifest_artifact_ref": manifest_ref, "manifest_artifact_digest": manifest_digest, "no_learning": True}
    if result["status"] == "succeeded":
        outcome["original_child_final"] = await finalize_repository_iteration(service, jobs,
            job_id=job_id, owner=owner, iteration_index=prepared["iteration_index"],
            actual_cleanup=cleanup, actual_job=job)
    elif result["status"] == "failed" and prepared["iteration_index"] < work.limits.max_iterations:
        outcome["repository_review"] = await prepare_repository_iteration(service, jobs,
            job_id=job_id, owner=owner, iteration_index=prepared["iteration_index"] + 1)
    elif result["status"] == "failed":
        outcome["recovery_action"] = "original_iterations_exhausted"
    return outcome


async def finalize_repository_iteration(service, jobs, *, job_id, owner, iteration_index,
        actual_cleanup, actual_job):
    """Adopt only the actual supervised final readback into the original child."""
    from dataclasses import replace
    from sqlalchemy import select
    from src.db.models import InferenceCostReservation
    from src.workflows.general_task_guard import issue_repository_child_final_witness
    from src.workflows.repo_repair import RepoIteration, RepoWorkVerifiedResult, _repair_test_args
    from src.work_board.contracts import GeneralTaskArtifactRef, GeneralTaskStepReceiptV1
    from src.work_board.general_task import write_step_artifact
    from src.work_board.general_task_runtime_artifacts import stage_task_artifact
    from src.workflows.job_runtime import _canonical, _as_utc, DurableJobLeaseError
    from src.model_fabric.effective_policy import configuration_mutation_lock
    from src.execution.repo_sandbox import assert_repo_iteration_cleanup_witness
    assert_repo_iteration_cleanup_witness(actual_cleanup, actual_job)
    wait = await recover_repository_wait_witness(service, jobs, job_id=job_id, owner=owner,
        iteration_index=iteration_index, _resumed=True)
    async with configuration_mutation_lock:
        context = await _repository_precontact(service, jobs, job_id=job_id, owner=owner)
        run, original, work = context["run"], context["original"], context["work"]
        iterations, identities = [], []
        for index in range(1, iteration_index + 1):
            identity = iteration_identity(job_id, original["repository_attempt_id"],
                _source_digest(original["original_input"]), index)
            identities.append(identity)
            prepared = _repository_record(run, "repository:prepared:" + identity)
            executed = _repository_record(run, "repository:execution:" + identity)
            cleanup = _repository_record(run, "repository:cleanup:" + identity)
            readback = _repository_record(run, "repository:readback:" + identity)
            if (any(item is None for item in (prepared, executed, cleanup, readback))
                    or cleanup.get("cleanup_proven") is not True
                    or readback["status"] != ("succeeded" if index == iteration_index else "failed")):
                raise DurableJobLeaseError("complete actual original iteration chain required")
            iterations.append(RepoIteration(index=index, input_tree_digest=prepared["input_tree_digest"],
                patch_digest=executed["patch_sha256"], command_refs=["repository:execution:" + identity],
                result_artifacts=["repository:cleanup:" + identity, "repository:readback:" + identity]))
        identity = identities[-1]
        readback = _repository_record(run, "repository:readback:" + identity)
        cleanup_record = _repository_record(run, "repository:cleanup:" + identity)
        execution = _repository_record(run, "repository:execution:" + identity)
        patch = _repository_record(run, "repository:patch:" + identity)
        response = _repository_record(run, "repository:response:" + identity)
        manifest = json.loads(service._read_private_artifact(readback["artifact_ref"],
            expected_digest=readback["artifact_digest"]))
        cleanup_bytes = service._read_private_artifact(cleanup_record["artifact_ref"],
            expected_digest=cleanup_record["artifact_digest"])
        check_args = list(_repair_test_args(tuple(context["compiled"].test_args), context["compiled"].allowed_paths))
        check_exits = {"test_args": check_args, "exit_code": manifest.get("exit_code")}
        if work.language_profile == "test_node":
            commands = manifest.get("commands", [])
            expected_scripts = [entry["script"] for entry in manifest["execution_plan"]["commands"]]
            checks_passed = ([entry.get("script") for entry in commands] == expected_scripts
                and all(entry.get("exit_code") == 0 and entry.get("timed_out") is False
                    and entry.get("cancelled") is False and entry.get("leftover_descendant") is False
                    and entry.get("stdout_truncated") is False
                    and entry.get("cleanup", {}).get("cleanup_proven") is True for entry in commands)
                and list(manifest["execution_plan"]["selection"]) == check_args)
            check_exits = {"test_args": check_args, "commands": commands}
        else:
            checks_passed = (manifest.get("exit_code") == 0 and manifest.get("timed_out") is False
                and manifest.get("stdout_truncated") is False and manifest.get("stderr_truncated") is False
                and manifest.get("test_args") == check_args)
        if (json.loads(cleanup_bytes) != actual_cleanup.projection() or not checks_passed
                or _source_digest(manifest) != readback["manifest_digest"]):
            raise DurableJobLeaseError("all requested original checks and actual cleanup must pass")
        output = RepoWorkVerifiedResult(iterations=iterations, patch_artifact_ref=patch["patch_artifact_ref"],
            final_readback_ref=readback["artifact_ref"]).model_dump(mode="json")
        binding = context["binding"]
        child_owner, child_fence = context["child"].lease_owner, context["child"].fencing_token
        async def output_authority(db, child):
            # The existing artifact writer publishes three known journals.
            # All ownership, status, lease, input and authority columns stay
            # exactly pinned while those journals advance.
            mutable = {"revision", "updated_at", "heartbeat_at", "checkpoint_receipts_json",
                "artifact_receipts_json", "readback_receipts_json"}
            rows = []
            for model, key, expected in context["rows"]:
                original_row = json.loads(expected)
                if original_row.get("run_identity") == binding.invocation_id:
                    actual_row = child.model_dump(mode="json")
                    if ({k: v for k, v in original_row.items() if k not in mutable} !=
                            {k: v for k, v in actual_row.items() if k not in mutable}):
                        raise DurableJobLeaseError("original final output child authority changed")
                    expected = _canonical(actual_row)
                rows.append((model, key, expected))
            await _recheck_repository_sql(db, {**context, "rows": rows})
        artifact, verified = await write_step_artifact(jobs, job_id=binding.invocation_id,
            owner=child_owner, fence=child_fence, plan_digest=binding.plan_digest,
            step_id=binding.step_id, output=output, authority_check=output_authority)
        output_context = await _repository_precontact(service, jobs, job_id=job_id, owner=owner)
        async def readback_authority(db, child):
            await _recheck_repository_sql(db, output_context)
        from src.workflows.job_runtime import _utc_now
        await jobs.record_readback(binding.invocation_id, effect_type="general_tool_call", status="succeeded",
            target_path=artifact["file_path"], content_sha256=artifact["content_sha256"],
            readback_id="repository-child-output:" + identity, verified_at=_utc_now().isoformat(),
            details={"step_id": binding.step_id, "tool_id": "repository_work", "verified": True,
                "output_exists": True, "file_path": artifact["file_path"], "no_learning": True,
                "actual_process_cleanup_digest": cleanup_record["artifact_digest"]},
            owner=child_owner, fencing_token=child_fence, readback_authority_check=readback_authority)
        child = await jobs.get_job(binding.invocation_id)
        matches = [item for item in child["artifacts"] if item["file_path"] == artifact["file_path"]
            and item["content_sha256"] == artifact["content_sha256"]]
        if len(matches) != 1 or verified != output:
            raise DurableJobLeaseError("literal original child output readback required")
        reference = GeneralTaskArtifactRef(artifact_id=matches[0]["artifact_id"],
            digest=artifact["content_sha256"], schema_version="GeneralTaskOutput.v1")
        async with jobs._session() as db:
            canonical = await stage_repository_canonical_source(service, db, repository_job_id=job_id,
                native_invocation_id=binding.invocation_id, consent_id=wait._source_binding.consent_id)
            costs = list((await db.scalars(select(InferenceCostReservation))).all())
            await _repository_remaining(db, await _repository_precontact(service, jobs, job_id=job_id, owner=owner),
                allow_exhausted_readback=True)
        evidence = {"final_patch_digest": execution["patch_sha256"],
            "final_manifest_digest": readback["manifest_digest"], "final_readback_digest": readback["artifact_digest"],
            "final_command_receipt_digest": _source_digest(execution), "final_cleanup_digest": cleanup_record["artifact_digest"],
            "final_accounting_digest": _source_digest([row.model_dump(mode="json") for row in costs
                if row.job_id == job_id]), "final_artifact_id": reference.artifact_id,
            "final_artifact_digest": reference.digest, "requested_check_exits_digest": _source_digest(check_exits),
            "all_iteration_ids_digest": _source_digest(identities)}
        final_source = replace(wait._source_binding, _final_source=canonical,
            _final_evidence_json=_canonical(evidence))
        final = issue_repository_child_final_witness(wait_witness=wait, source_binding=final_source, **evidence)
        staged = stage_task_artifact(parent_job_id=binding.parent_job_id, creation_digest=binding.creation_digest,
            payload=GeneralTaskStepReceiptV1(step_id=binding.step_id, plan_revision=binding.plan_revision,
                invocation_id=binding.invocation_id, input_digest=binding.input_digest, contact_state="settled", status="verified",
                descriptor_digest=binding.descriptor_digest, selected_grant_digest=binding.selected_grant_digest,
                task_id=binding.task_id, attempt_id=binding.attempt_id, child_job_id=binding.invocation_id,
                child_attempt_count=1, child_fence=child_fence, parent_creation_digest=binding.creation_digest,
                phase_digest=binding.phase_digest, artifact_refs=[reference],
                effect_receipt_digest=_source_digest(child["effects"]),
                cleanup_receipt_digest=evidence["final_cleanup_digest"]))
        parent = await jobs.get_job(binding.parent_job_id)
        published = await jobs.publish_general_task_step_receipt(binding.parent_job_id, staged_artifact=staged,
            child_id=binding.invocation_id, owner=child_owner, fencing_token=child_fence,
            expected_parent_revision=parent["revision"], repository_final_witness=final)
        # The original root keeps its physical capacity until both canonical
        # child adoption and the root's actual readback are durably terminal.
        manifest_bytes = service._read_private_artifact(readback["artifact_ref"],
            expected_digest=readback["artifact_digest"])
        root_owner, root_fence = run.lease_owner, run.fencing_token
        manifest_path = readback["artifact_ref"].removeprefix("workspace-json:")
        await jobs.record_artifact(job_id, file_path=manifest_path, artifact_type="repo_repair_manifest",
            content=manifest_bytes, owner=root_owner, fencing_token=root_fence)
        from src.workflows.job_runtime import _utc_now
        root_readback = await jobs.record_readback(job_id, target_path=manifest_path, status="succeeded",
            effect_type="repository_iteration_verified", content_sha256=readback["artifact_digest"],
            readback_id="repository-final:" + identity, verified_at=_utc_now().isoformat(),
            details={"verified": True, "output_exists": True, "no_learning": True,
                "iteration_id": identity, "original_child_id": binding.invocation_id},
            owner=root_owner, fencing_token=root_fence)
        from src.work_board.repository import _begin_sqlite_immediate
        async with jobs._session() as db:
            await _begin_sqlite_immediate(db)
            current = await jobs._fetch(db, job_id)
            closed_child = await jobs._fetch(db, binding.invocation_id)
            if (closed_child.status != "succeeded" or closed_child.result_digest != reference.digest
                    or closed_child.fencing_token != child_fence or closed_child.lease_owner
                    or current.status != "running" or current.fencing_token != root_fence):
                raise DurableJobLeaseError("actual original final adoption must precede root closure")
            _append_repository_record(current, "repository:terminal:v1",
                {"schema": "repository.final_verified.v1", "iteration_id": identity,
                    "original_child_id": binding.invocation_id, "final_witness_digest": _source_digest(final.projection()),
                    "manifest_artifact_digest": readback["artifact_digest"], "no_learning": True},
                inventory=repository_checkpoint_inventory(current, work))
            current.revision += 1
            await db.commit()
        current = await jobs.get_job(job_id)
        terminal = await jobs.transition_job(job_id, "succeeded", owner=root_owner, fencing_token=root_fence,
            expected_revision=current["revision"], result={"verified": True, "no_learning": True,
                "iteration_count": iteration_index, "manifest_artifact_ref": readback["artifact_ref"],
                "patch_artifact_ref": patch["patch_artifact_ref"], "original_child_id": binding.invocation_id})
        from src.work_board.repository import WorkBoardRepository
        from src.work_board.contracts import WorkBoardStatus
        proof = next(item for item in terminal["effects"]
            if item.get("readback_id") == "repository-final:" + identity)
        async with jobs._session() as db:
            authority = context["authority"]
            board = await WorkBoardRepository().project_attempt(db, original["repository_task_id"],
                original["repository_attempt_id"], expected_revision=authority.task.task_revision,
                board_fence=authority.attempt.fencing_token, lease_owner=authority.attempt.lease_owner,
                status=WorkBoardStatus.done, outcome="repository_cumulative_repair_verified",
                result_refs=[{"job_id": job_id, "status": "succeeded", "no_learning": True}],
                verified_readback={"source": "workflow_run", "status": "succeeded", "verified": True,
                    "workflow_run_id": job_id, "readback_id": proof["readback_id"],
                    "content_sha256": proof["content_sha256"], "verified_at": proof["verified_at"]})
            await db.commit()
        lane = service._iterative_lanes.pop(job_id, None)
        if lane is None:
            raise DurableJobLeaseError("original physical root capacity owner disappeared")
        lane.release()
        published["repository_root"] = terminal
        published["repository_task_status"] = board.task.status.value
        return published


async def validate_repository_child_final_witness(db, witness, *, parent, task, attempt,
        child, manifest, staged_artifact=None, receipt=None, phase):
    """SQL-only adoption check for the actual original process owner."""
    from sqlalchemy import select
    from src.db.models import WorkflowRunState, InferenceCostReservation
    from src.workflows.job_runtime import DurableJobLeaseError, _as_utc
    from src.workflows.general_task_guard import _assert_repository_child_final_witness_shape
    _assert_repository_child_final_witness_shape(witness)
    source = witness._source_binding
    current_source = source._final_source
    assert_repository_canonical_source(current_source)
    if phase != "final" or source._final_evidence_json is None:
        raise DurableJobLeaseError("actual original final source owner required")
    evidence = json.loads(source._final_evidence_json)
    if any(getattr(witness, key) != value for key, value in evidence.items()):
        raise DurableJobLeaseError("original final source evidence changed")
    run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == witness.wait_witness.repository_job_id))
    original, work, compiled, group, binding, task_source = read_repository_original(run)
    identity = witness.wait_witness.iteration_id
    readback = _repository_record(run, "repository:readback:" + identity)
    cleanup = _repository_record(run, "repository:cleanup:" + identity)
    execution = _repository_record(run, "repository:execution:" + identity)
    response = _repository_record(run, "repository:response:" + identity)
    iteration = _repository_record(run, "repository:iteration:" + identity)
    rows = list((await db.scalars(select(InferenceCostReservation))).all())
    cost = next((row for row in rows if row.operation_id == response["operation_id"]), None)
    if (binding != witness.wait_witness.native_binding or run.fencing_token != witness.wait_witness.repository_fence
            or readback.get("status") != "succeeded" or cleanup.get("cleanup_proven") is not True
            or readback["artifact_digest"] != witness.final_readback_digest
            or readback["manifest_digest"] != witness.final_manifest_digest
            or cleanup["artifact_digest"] != witness.final_cleanup_digest
            or _source_digest(execution) != witness.final_command_receipt_digest
            or execution["patch_sha256"] != witness.final_patch_digest
            or _source_digest([row.model_dump(mode="json") for row in rows if row.job_id == run.run_identity])
                != witness.final_accounting_digest or cost is None or cost.state != "settled"
            or cost.contact_started_at is None):
        raise DurableJobLeaseError("actual original final readback accounting or cleanup changed")
    accounting = RepositoryIterationAccountingWitness(**{**iteration["accounting_binding"], "group": group,
        "original_deadline_at": _as_utc(run.deadline_at)}, _canonical_source=current_source,
        _request_body_digest=cost.payload_digest,
        _request_route_digest=_repository_record(run, "repository:prepared:" + identity)["request_route_digest"], _seal=_SEAL)
    await validate_repository_iteration_accounting(db, accounting, run, rows=rows, operation_id=cost.operation_id,
        payload_digest=cost.payload_digest, bound_microusd=cost.bound_microusd, deadline_at=cost.deadline_at, already_reserved=True)
    return {"verified": True, "no_learning": True}


def _assert_task_publication_configuration(service, *, staged_config=None):
    from config.settings import settings
    from src.security.trust_contract import canonical_digest
    from src.workspace import canonical_workspace_root
    from src.work_board.repository import BoardError
    if staged_config is None:
        from src.execution.repo_sandbox import load_persisted_repo_sandbox_settings
        staged_config, error = load_persisted_repo_sandbox_settings()
        if error is not None:
            raise BoardError("repository_source_current_configuration_changed",
                "Restore the canonical private repository selector", status_code=409)
    if (canonical_digest(staged_config.model_dump(mode="json")) !=
            canonical_digest(service.sandbox.config.model_dump(mode="json"))
            or service._workspace() != canonical_workspace_root(settings.workspace_dir)):
        raise BoardError("repository_source_current_configuration_changed",
            "Use the current canonical repository profile and workspace", status_code=409)
    return staged_config


def prepare_repository_task_publication(service, envelope, *, owner, goal_revision, replay=False):
    """Inspect the fixed source before publication; never accept caller metadata.

    The scope stages physical checks before the SQLite writer. Its callback
    validates only the original SQL authority and captured configuration.
    """
    import asyncio
    from src.workflows.repo_repair import RepoRepairService, RepoWorkInput
    from src.work_board.repository import BoardError
    from src.security.trust_contract import canonical_digest
    if replay and envelope.repository_source is None:
        raise BoardError("repository_source_unscoped_replay", "Old Tasks cannot acquire repository source authority on replay", status_code=409)
    if type(service) is not RepoRepairService or envelope.plan is None:
        raise BoardError("repository_source_owner_required", "Current fixed repository source owner required", status_code=409)
    _assert_task_publication_configuration(service)
    steps = [step for step in envelope.plan.steps if step.tool_id == "repository_work"]
    if len(steps) != 1 or envelope.proposal_group is None:
        raise BoardError("repository_source_binding_invalid", "One original grouped repository step required", status_code=409)
    from src.native_tools.task_adapters import repository_work_descriptor
    fixed_descriptor = repository_work_descriptor()
    selected_descriptors = [item for item in envelope.descriptors if item.tool_id == "repository_work"]
    if selected_descriptors != [fixed_descriptor]:
        raise BoardError("repository_source_descriptor_changed", "Exact current fixed repository descriptor required", status_code=409)
    try:
        work = RepoWorkInput.model_validate(steps[0].input)
    except ValueError as exc:
        raise BoardError("repository_source_input_invalid", "Review the exact closed repository input", status_code=422) from exc
    group = envelope.proposal_group
    if (group.owner_principal_id != owner.principal_id or group.owner_session_id != owner.session_id
            or group.goal_id != envelope.task_input.goal_ref or group.goal_revision != goal_revision):
        raise BoardError("repository_source_binding_invalid", "Original owner and Goal required", status_code=409)
    if replay:
        binding = envelope.repository_source
        if binding is None:
            raise BoardError("repository_source_unscoped_replay", "Old Tasks cannot acquire repository source authority on replay", status_code=409)
    else:
        if envelope.repository_source is not None:
            raise BoardError("repository_source_caller_metadata", "Source metadata must be minted during original publication", status_code=409)
        binding = service.stage_task_source(work, owner=owner,
            goal_id=group.goal_id, goal_revision=goal_revision)
    projection = binding.model_dump(mode="json", exclude={"schema_version", "binding_digest"})
    if (binding.binding_digest != canonical_digest(projection)
            or binding.original_input_digest != canonical_digest(work.model_dump(mode="json"))
            or binding.owner_principal_id != owner.principal_id
            or binding.original_root_id != owner.session_id
            or binding.goal_id != group.goal_id or binding.goal_revision != goal_revision):
        raise BoardError("repository_source_binding_invalid", "Original source binding changed", status_code=409)
    witness = object()

    @asynccontextmanager
    async def scope():
        from src.model_fabric.effective_policy import configuration_mutation_lock
        from config.settings import settings
        async with configuration_mutation_lock:
            staged_config = _assert_task_publication_configuration(service)
            if repository_work_descriptor() != fixed_descriptor:
                raise BoardError("repository_source_descriptor_changed", "Original fixed descriptor policy changed", status_code=409)
            # Physical work precedes the publication writer, including replay.
            facts = json.loads(service._read_private_artifact(binding.source_artifact_ref,
                expected_digest=binding.source_artifact_digest))
            compiled = service.recheck_task_source_snapshot(work, facts)
            if (facts.get("original_input") != work.model_dump(mode="json")
                    or facts.get("compiled_input") != compiled.model_dump(mode="json")
                    or canonical_digest(service.sandbox.config.model_dump(mode="json")) != binding.executor_profile_digest
                    or facts.get("snapshot_manifest", {}).get("digest") != binding.snapshot_digest
                    or facts.get("owner_principal_id") != owner.principal_id
                    or facts.get("original_root_id") != owner.session_id
                    or facts.get("goal_id") != group.goal_id or facts.get("goal_revision") != goal_revision):
                raise BoardError("repository_source_changed", "Original physical source or execution profile changed", status_code=409)
            token = _task_publication.set((asyncio.current_task(), witness, staged_config,
                canonical_digest(settings.repo_sandbox.model_dump(mode="json"))))
            try:
                yield
            finally:
                _task_publication.reset(token)

    async def check(db):
        from src.workflows.general_task_accounting import validate_group_owner
        from config.settings import settings
        held = _task_publication.get()
        if held is None or held[:2] != (asyncio.current_task(), witness):
            raise BoardError("repository_source_scope_required", "Original source publication scope required", status_code=409)
        _assert_task_publication_configuration(service, staged_config=held[2])
        if canonical_digest(settings.repo_sandbox.model_dump(mode="json")) != held[3]:
            raise BoardError("repository_source_current_configuration_changed", "Original canonical settings changed", status_code=409)
        if repository_work_descriptor() != fixed_descriptor:
            raise BoardError("repository_source_descriptor_changed", "Original fixed descriptor policy changed", status_code=409)
        if canonical_digest(service.sandbox.config.model_dump(mode="json")) != binding.executor_profile_digest:
            raise BoardError("repository_source_changed", "Original execution profile changed", status_code=409)
        await validate_group_owner(db, group)
        _assert_task_publication_configuration(service, staged_config=held[2])
        if canonical_digest(settings.repo_sandbox.model_dump(mode="json")) != held[3]:
            raise BoardError("repository_source_current_configuration_changed", "Original canonical settings changed", status_code=409)

    return binding, check, scope


def read_repository_original(run):
    """Only the protected canonical journal supplies original compilation."""
    from src.workflows.general_task_guard import _history
    from src.workflows.job_runtime import _digest, DurableJobLeaseError
    from src.workflows.repo_repair import RepoWorkInput, RepoRepairInput
    records = [record for record in _history(run)
               if record.get("checkpoint_id") == "repository:original:v1"]
    if len(records) != 1:
        raise DurableJobLeaseError("original fixed repository source checkpoint required")
    record = records[0]
    payload = record.get("payload")
    if (record.get("safe") is not True or not isinstance(payload, dict)
            or record.get("state_digest") != _digest(payload)):
        raise DurableJobLeaseError("original fixed repository source checkpoint changed")
    required = {"schema_version", "repository_job_id", "repository_task_id", "repository_attempt_id",
        "repository_input_artifact_digest", "original_input", "compiled_input", "original_deadline_at",
        "original_max_cost_microusd", "group", "native_binding", "source_binding"}
    if set(payload) != required or payload["schema_version"] != "repository.original.v1":
        raise DurableJobLeaseError("original fixed repository source shape changed")
    try:
        work = RepoWorkInput.model_validate(payload["original_input"])
        compiled = RepoRepairInput.model_validate(payload["compiled_input"])
        group = TaskProposalGroupV1.model_validate(payload["group"])
        binding = GeneralTaskNativeChildBindingV1.model_validate(payload["native_binding"])
        source = RepositoryTaskSourceBinding.model_validate(payload["source_binding"])
        cutoff = TaskProposalGroupV1.utc_timestamp(payload["original_deadline_at"])
        from src.workflows.inference_accounting import _utc
        from src.security.trust_contract import canonical_digest
        if (payload["repository_job_id"] != run.run_identity
                or payload["repository_input_artifact_digest"] != run.input_digest
                or type(payload["original_max_cost_microusd"]) is not int
                or payload["original_max_cost_microusd"] != work.limits.max_cost_microusd
                or canonical_digest(work.model_dump(mode="json")) != source.original_input_digest
                or compiled.repository_path != work.repository_ref
                or compiled.allowed_paths != work.allowed_paths
                or cutoff > group.original_deadline_at or cutoff > binding.original_deadline_at
                or cutoff > binding.native_deadline_at
                or cutoff > _utc(run.started_at) + timedelta(seconds=work.limits.max_total_seconds)
                or _utc(run.deadline_at) != cutoff
                or group.owner_principal_id != binding.owner_principal_id
                or group.owner_session_id != binding.original_root_id
                or group.goal_id != binding.goal_id or group.goal_revision != binding.goal_revision
                or source.owner_principal_id != run.owner_principal_id
                or source.original_root_id != run.operator_session_id
                or source.goal_id != run.goal_id or source.goal_revision != run.goal_revision):
            raise ValueError("original source/cutoff changed")
    except (ValueError, TypeError, KeyError) as exc:
        raise DurableJobLeaseError("original fixed repository source binding changed") from exc
    return payload, work, compiled, group, binding, source


async def prepare_repository_original_admission(service, db, *, native_invocation_id,
        repository_task_id, repository_attempt_id):
    """Stage the actual original child before the existing admission writer.

    This is a private new-root producer, not immutable admission replay. The
    caller must retain its returned scope through admit_job's actual commit.
    No public projection, supplied group, or already-admitted root qualifies.
    """
    import asyncio
    from sqlalchemy import select
    from src.db.models import WorkflowRunState, WorkBoardTask, WorkBoardAttempt, WorkBoardInputArtifact, Goal
    from src.workflows.repo_repair import RepoRepairService
    from src.workflows.general_task_guard import child_binding, read_manifest, assert_general_task_child_current
    from src.work_board.general_task_runtime_artifacts import verify_general_task_manifest
    from src.workflows.job_runtime import DurableJobLeaseError, _utc_now, _as_utc, _canonical, _digest
    from src.security.trust_contract import canonical_digest
    from src.native_tools.task_adapters import repository_work_descriptor
    from src.work_board.general_task import digest as native_digest
    if type(service) is not RepoRepairService:
        raise DurableJobLeaseError("actual original repository source owner required")
    child = await db.scalar(select(WorkflowRunState).where(
        WorkflowRunState.run_identity == native_invocation_id))
    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == repository_task_id))
    attempt = await db.get(WorkBoardAttempt, repository_attempt_id)
    if (child is None or task is None or attempt is None
            or attempt.task_id != task.task_id or attempt.workflow_run_id is not None
            or task.capability_id != "engineering.repo-repair.v1"
            or task.status != "running" or not attempt.lease_owner
            or attempt.lease_expires_at is None or _as_utc(attempt.lease_expires_at) <= _utc_now()
            or attempt.fencing_token < 1
            or attempt.ended_at is not None or attempt.cancel_requested_at is not None):
        raise DurableJobLeaseError("new original repository Task attempt required")
    # Reject existing-root attempts before any private physical source read.
    binding = child_binding(child)
    from src.workflows.job_runtime import _binding
    permanent_binding = _binding(owner_principal_id=binding.owner_principal_id,
        goal_id=binding.goal_id, goal_revision=binding.goal_revision,
        idempotency_scope="original-repository-child", dedupe_key=binding.invocation_id)
    existing_mapping = await db.scalar(select(WorkflowRunState).where(
        WorkflowRunState.idempotency_binding == permanent_binding))
    if existing_mapping is not None:
        # This new-only producer cannot turn immutable generic admission replay
        # into current Source authority. A separate exact scoped recovery owns
        # the existing root; no private source artifact has been read here.
        raise DurableJobLeaseError("original native invocation already has its permanent repository mapping")
    await assert_general_task_child_current(db, child)
    parent = await db.scalar(select(WorkflowRunState).where(
        WorkflowRunState.run_identity == binding.parent_job_id))
    parent_task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == binding.task_id))
    parent_attempt = await db.get(WorkBoardAttempt, binding.attempt_id)
    goal = await db.get(Goal, binding.goal_id)
    manifest = read_manifest(parent) if parent is not None else None
    if parent_task is None or parent_attempt is None or manifest is None or goal is None:
        raise DurableJobLeaseError("original C1 source Task required")
    envelope = await verify_general_task_manifest(db, parent, parent_task, parent_attempt, manifest)
    source = envelope.repository_source
    group = envelope.proposal_group
    input_artifact = await db.get(WorkBoardInputArtifact, task.input_artifact_id)
    if (source is None or group is None or input_artifact is None
            or json.loads(child.arguments_json).get("tool_id") != "repository_work"
            or source.original_root_id != binding.original_root_id
            or (task.owner_principal_id, task.owner_session_id, task.goal_id, task.goal_revision) !=
                (binding.owner_principal_id, binding.original_root_id, binding.goal_id, binding.goal_revision)
            or input_artifact.bound_task_id != task.task_id
            or input_artifact.payload_sha256 != task.typed_input_digest
            or input_artifact.typed_input_ref != task.typed_input_ref
            or input_artifact.capability_id != task.capability_id or input_artifact.capability_version != "1"
            or input_artifact.owner_principal_id != task.owner_principal_id
            or input_artifact.owner_session_id != task.owner_session_id
            or input_artifact.goal_id != task.goal_id or input_artifact.goal_revision != task.goal_revision
            or input_artifact.state != "bound" or _as_utc(input_artifact.expires_at) <= _utc_now()
            or input_artifact.bound_task_revision > task.task_revision):
        raise DurableJobLeaseError("original source and repository Task artifact binding required")
    work = service._read_bound_work_input(input_artifact)
    if (canonical_digest(work.model_dump(mode="json")) != source.original_input_digest
            or native_digest(work.model_dump(mode="json")) != binding.input_digest):
        raise DurableJobLeaseError("original seven-field repository input changed")
    fixed_descriptor = repository_work_descriptor()
    if native_digest(fixed_descriptor.model_dump(mode="json")) != binding.descriptor_digest:
        raise DurableJobLeaseError("original fixed repository descriptor changed")
    originals = ((WorkflowRunState, child.id, child), (WorkflowRunState, parent.id, parent),
        (WorkBoardTask, parent_task.creation_sequence, parent_task),
        (WorkBoardAttempt, parent_attempt.attempt_id, parent_attempt),
        (WorkBoardTask, task.creation_sequence, task), (WorkBoardAttempt, attempt.attempt_id, attempt),
        (WorkBoardInputArtifact, input_artifact.artifact_id, input_artifact), (Goal, goal.id, goal))
    rows = tuple((model, key, _canonical(row.model_dump(mode="json"))) for model, key, row in originals)
    witness, entered = object(), ContextVar("repository_original_admission", default=None)
    compiled = None
    issued = False

    @asynccontextmanager
    async def scope():
        nonlocal compiled, issued
        from src.model_fabric.effective_policy import configuration_mutation_lock
        from config.settings import settings
        async with configuration_mutation_lock:
            issued = False
            # A preparation may have preceded another source admission. Recheck
            # its permanent mapping before reopening any private source bytes.
            # This read is outside the financial/admission writer; the exact
            # same predicate is checked again inside the new-row transaction.
            async with service.session_factory() as mapping_db:
                existing_mapping = await mapping_db.scalar(select(WorkflowRunState).where(
                    WorkflowRunState.idempotency_binding == permanent_binding))
                if existing_mapping is not None:
                    raise DurableJobLeaseError("original native invocation already has its permanent repository mapping")
            staged_config = _assert_task_publication_configuration(service)
            facts = json.loads(service._read_private_artifact(source.source_artifact_ref,
                expected_digest=source.source_artifact_digest))
            compiled = service.recheck_task_source_snapshot(work, facts)
            if (facts.get("original_input") != work.model_dump(mode="json")
                    or canonical_digest(staged_config.model_dump(mode="json")) != source.executor_profile_digest
                    or repository_work_descriptor() != fixed_descriptor):
                raise DurableJobLeaseError("original inspected repository source changed")
            token = entered.set((asyncio.current_task(), witness, staged_config,
                canonical_digest(settings.repo_sandbox.model_dump(mode="json"))))
            try:
                yield
                if not issued:
                    raise DurableJobLeaseError("repository admission replay requires its original scoped recovery")
            finally:
                entered.reset(token)

    async def check(current_db, run):
        nonlocal issued
        from src.workflows.general_task_accounting import validate_group_owner
        from config.settings import settings
        held = entered.get()
        if held is None or held[:2] != (asyncio.current_task(), witness) or compiled is None:
            raise DurableJobLeaseError("original repository source admission scope required")
        _assert_task_publication_configuration(service, staged_config=held[2])
        if (canonical_digest(settings.repo_sandbox.model_dump(mode="json")) != held[3]
                or repository_work_descriptor() != fixed_descriptor):
            raise DurableJobLeaseError("original repository configuration changed")
        for model, key, original in rows:
            current = await current_db.get(model, key, populate_existing=True)
            if current is None or _canonical(current.model_dump(mode="json")) != original:
                raise DurableJobLeaseError("original repository admission source row changed")
        await validate_group_owner(current_db, group)
        from sqlalchemy import func
        from src.workflows.job_runtime import _canonical_goal_max_outstanding, DURABLE_JOB_TERMINAL_STATUSES, DURABLE_JOB_RECORD_SCHEMA_VERSION
        goal_capacity = _canonical_goal_max_outstanding(goal) or 1
        outstanding = await current_db.scalar(select(func.count(WorkflowRunState.run_identity)).where(
            WorkflowRunState.goal_id == binding.goal_id,
            WorkflowRunState.parent_job_id.is_(None), WorkflowRunState.parent_run_identity.is_(None),
            WorkflowRunState.status.not_in(tuple(DURABLE_JOB_TERMINAL_STATUSES)),
            WorkflowRunState.record_schema_version >= DURABLE_JOB_RECORD_SCHEMA_VERSION))
        if int(outstanding or 0) >= goal_capacity:
            raise DurableJobLeaseError("original Goal outstanding capacity cannot admit repository root")
        # Permanent single use is the existing indexed canonical idempotency
        # binding, including terminal/Unknown rows, not an all-history scan.
        existing_mapping = await current_db.scalar(select(WorkflowRunState).where(
            WorkflowRunState.idempotency_binding == permanent_binding))
        if existing_mapping is not None:
            raise DurableJobLeaseError("original C1 invocation already owns its repository root")
        now = _utc_now()
        cutoff = _as_utc(run.deadline_at)
        if (child.status != "running" or child.attempt_count != 1 or child.fencing_token < 1
                or not child.lease_owner or _as_utc(child.lease_expires_at) <= now
                or _as_utc(attempt.lease_expires_at) <= now
                or run.job_kind != "engineering.repo-repair.v1" or run.capability_version != "1"
                or run.root_run_identity != run.run_identity or run.parent_job_id
                or run.idempotency_scope != "original-repository-child"
                or run.idempotency_key != binding.invocation_id
                or run.idempotency_binding != permanent_binding
                or run.input_digest != input_artifact.payload_sha256
                or (run.owner_principal_id, run.operator_session_id, run.goal_id, run.goal_revision) !=
                    (binding.owner_principal_id, binding.original_root_id, binding.goal_id, binding.goal_revision)
                or cutoff is None or not now < cutoff <= min(group.original_deadline_at,
                    binding.original_deadline_at, binding.native_deadline_at,
                    _as_utc(child.lease_expires_at), _as_utc(attempt.lease_expires_at),
                    now + timedelta(seconds=work.limits.max_total_seconds))
                or json.loads(run.checkpoint_receipts_json or "[]")):
            raise DurableJobLeaseError("new original repository root bounds changed")
        payload = {"schema_version": "repository.original.v1", "repository_job_id": run.run_identity,
            "repository_task_id": task.task_id, "repository_attempt_id": attempt.attempt_id,
            "repository_input_artifact_digest": input_artifact.payload_sha256,
            "original_input": work.model_dump(mode="json"), "compiled_input": compiled.model_dump(mode="json"),
            "original_deadline_at": cutoff.isoformat(), "original_max_cost_microusd": work.limits.max_cost_microusd,
            "group": group.model_dump(mode="json"), "native_binding": binding.model_dump(mode="json"),
            "source_binding": source.model_dump(mode="json")}
        run.checkpoint_receipts_json = _canonical([{"checkpoint_id": "repository:original:v1",
            "state_digest": _digest(payload), "safe": True, "payload": payload,
            "created_at": now.isoformat()}])
        inventory = repository_checkpoint_inventory(run, work)
        _append_repository_record(run, "repository:inventory:v1",
            {"schema": "repository.checkpoint_inventory.v1", "identities": inventory,
             "max_records": 50, "max_metadata_bytes_per_record": 16384}, inventory=inventory)
        issued = True

    return check, scope


def iteration_identity(repository_job_id: str, repository_attempt_id: str,
                       original_input_digest: str, index: int) -> str:
    if (not repository_job_id or not repository_attempt_id
            or not _SHA.fullmatch(original_input_digest) or type(index) is not int
            or not 1 <= index <= 3):
        raise ValueError("original repository binding and bounded index required")
    payload = ["seraph.repository_iteration.v1", repository_job_id,
               repository_attempt_id, original_input_digest, index]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class RepoIterationProcessBinding:
    repository_job_id: str
    repository_attempt_id: str
    repository_fence: int
    iteration_index: int
    iteration_id: str
    original_deadline_at: datetime
    authority_digest: str
    base_digest: str
    _seal: object = field(default=None, repr=False, compare=False)

    def projection(self) -> dict[str, Any]:
        return {name: (value.isoformat() if isinstance(value, datetime) else value)
                for name in self.__dataclass_fields__ if not name.startswith("_")
                for value in [getattr(self, name)]}


def assert_repo_iteration_process_binding(binding, job, *, allow_expired_for_cleanup=False) -> None:
    from src.execution.repo_sandbox import RepoSandboxError
    if (type(binding) is not RepoIterationProcessBinding or binding._seal is not _SEAL
            or type(binding.iteration_index) is not int or not 1 <= binding.iteration_index <= 3
            or not _SHA.fullmatch(binding.iteration_id)
            or binding.repository_job_id != job.job_id
            or binding.repository_attempt_id != job.attempt_id
            or type(binding.repository_fence) is not int or binding.repository_fence < 1
            or binding.repository_fence != job.fencing_token
            or binding.authority_digest != job.authority_digest
            or binding.base_digest != job.base_digest
            or binding.original_deadline_at.tzinfo is None
            or (not allow_expired_for_cleanup and binding.original_deadline_at <= datetime.now(timezone.utc))
            or not job.execution_deadline_at):
        raise RepoSandboxError("original source-issued iteration binding required")
    try:
        command_deadline = datetime.fromisoformat(job.execution_deadline_at.replace("Z", "+00:00"))
    except (ValueError, TypeError) as exc:
        raise RepoSandboxError("fixed iteration command deadline required") from exc
    if command_deadline.tzinfo is None or command_deadline > binding.original_deadline_at:
        raise RepoSandboxError("command cannot extend the original repository cutoff")


@dataclass(frozen=True, slots=True)
class _CanonicalRepositorySource:
    """Immutable physical-source staging projected onto exact SQL fences."""
    parent_revision: int
    parent_checkpoint_json: str
    parent_authority_json: str
    parent_envelope_json: str
    task_revision: int
    parent_attempt_fence: int
    repository_attempt_fence: int
    child_revision: int
    child_fence: int
    child_authority_json: str
    child_arguments_json: str
    native_binding_json: str
    parent_row_json: str
    task_row_json: str
    parent_attempt_row_json: str
    repository_attempt_row_json: str
    child_row_json: str
    repository_task_row_json: str
    input_artifact_row_json: str
    consent_row_json: str
    consent_id: str
    _contacted_wait_parent_json: str | None = field(default=None, repr=False, compare=False)
    _contacted_wait_child_json: str | None = field(default=None, repr=False, compare=False)
    _final_source: Any = field(default=None, repr=False, compare=False)
    _final_evidence_json: str | None = field(default=None, repr=False, compare=False)
    _seal: object = field(default=None, repr=False, compare=False)

    def projection(self) -> dict[str, Any]:
        assert_repository_canonical_source(self)
        return {name: getattr(self, name) for name in self.__dataclass_fields__
                if not name.startswith("_")}


def assert_repository_canonical_source(source) -> None:
    """Only the actual source owner can stamp a staged SQL/physical binding."""
    from src.workflows.job_runtime import DurableJobLeaseError
    if (type(source) is not _CanonicalRepositorySource or source._seal is not _SEAL
            or any(not getattr(source, name) for name in (
                "parent_row_json", "task_row_json", "parent_attempt_row_json",
                "parent_envelope_json",
                "repository_attempt_row_json", "child_row_json", "repository_task_row_json",
                "input_artifact_row_json", "consent_row_json", "consent_id", "native_binding_json"))):
        raise DurableJobLeaseError("original producer-sealed repository canonical source required")


async def stage_repository_canonical_source(service, db, *, repository_job_id,
        native_invocation_id, consent_id):
    """Read actual physical artifacts before either canonical writer.

    This cannot bootstrap a root: the protected original handoff must already
    exist, with the same seven-field input and source-issued Task artifact.
    """
    from src.db.models import (WorkflowRunState, WorkBoardTask, WorkBoardAttempt,
        WorkBoardInputArtifact, RepoRepairEgressConsent)
    from src.workflows.repo_repair import RepoRepairService
    from src.work_board.contracts import WorkBoardOwner
    from src.workflows.general_task_guard import (child_binding, read_manifest,
        assert_general_task_child_current)
    from src.work_board.general_task_runtime_artifacts import verify_general_task_manifest
    from src.workflows.job_runtime import DurableJobLeaseError
    if type(service) is not RepoRepairService:
        raise DurableJobLeaseError("actual fixed repository source owner required")
    from sqlalchemy import select
    run = await db.scalar(select(WorkflowRunState).where(
        WorkflowRunState.run_identity == repository_job_id).execution_options(populate_existing=True))
    child = await db.scalar(select(WorkflowRunState).where(
        WorkflowRunState.run_identity == native_invocation_id).execution_options(populate_existing=True))
    if run is None or child is None:
        raise DurableJobLeaseError("original repository and native child required")
    original, work, compiled, group, binding, task_source = read_repository_original(run)
    if child_binding(child) != binding:
        raise DurableJobLeaseError("original repository native child changed")
    await assert_general_task_child_current(db, child)
    parent = await db.scalar(select(WorkflowRunState).where(
        WorkflowRunState.run_identity == binding.parent_job_id).execution_options(populate_existing=True))
    task = await db.scalar(select(WorkBoardTask).where(
        WorkBoardTask.task_id == binding.task_id).execution_options(populate_existing=True))
    attempt = await db.get(WorkBoardAttempt, binding.attempt_id, populate_existing=True)
    if parent is None or task is None or attempt is None:
        raise DurableJobLeaseError("original C1 Task source required")
    manifest = read_manifest(parent)
    if manifest is None:
        raise DurableJobLeaseError("original C1 manifest required")
    envelope = await verify_general_task_manifest(db, parent, task, attempt, manifest)
    if envelope.repository_source != task_source or envelope.proposal_group != group:
        raise DurableJobLeaseError("original scoped repository Task artifact required")
    repo_authority = await service._resolve_canonical_authority(db,
        owner=WorkBoardOwner(principal_id=run.owner_principal_id, session_id=run.operator_session_id),
        work_board_task_id=original["repository_task_id"],
        work_board_attempt_id=original["repository_attempt_id"], workflow_run_id=run.run_identity,
        goal_id=run.goal_id, goal_revision=run.goal_revision,
        lease_owner=run.lease_owner, fencing_token=run.fencing_token, consent_id=consent_id)
    if repo_authority.original_work_input != work or repo_authority.input != compiled:
        raise DurableJobLeaseError("original seven-field input or compilation changed")
    input_artifact = await db.get(WorkBoardInputArtifact, repo_authority.task.input_artifact_id)
    consent = await db.get(RepoRepairEgressConsent, consent_id, populate_existing=True)
    if (input_artifact is None or consent is None or consent.workflow_run_id != run.run_identity
            or not consent.iteration_id or consent.diagnostics_acknowledged is not True
            or consent.egress_envelope_schema != "seraph.repo_iteration_egress.v1"
            or not consent.diagnostics_sha256 or not consent.redaction_version
            or not consent.serialized_request_sha256
            or type(consent.combined_input_bytes) is not int
            or not 0 < consent.combined_input_bytes <= min(65536, consent.maximum_input_bytes)
            or consent.owner_principal_id != run.owner_principal_id
            or consent.owner_session_id != run.operator_session_id):
        raise DurableJobLeaseError("exact fresh repository diagnostics consent required")
    egress = json.loads(service._read_private_artifact(consent.egress_envelope_artifact_ref,
        expected_digest=consent.egress_envelope_sha256))
    from src.workflows.repo_repair import _digest_bytes, _canonical_bytes
    if (egress.get("schema") != consent.egress_envelope_schema
            or egress.get("iteration_id") != consent.iteration_id
            or _digest_bytes(_canonical_bytes(egress.get("diagnostics"))) != consent.diagnostics_sha256
            or egress.get("diagnostics", {}).get("redaction_version") != consent.redaction_version):
        raise DurableJobLeaseError("exact fresh repository diagnostics envelope changed")
    def row_json(row):
        return json.dumps(row.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    source = _CanonicalRepositorySource(
        parent_revision=parent.revision, parent_checkpoint_json=parent.checkpoint_receipts_json,
        parent_authority_json=parent.declared_authority_json, task_revision=task.task_revision,
        parent_envelope_json=json.dumps(envelope.model_dump(mode="json"), sort_keys=True, separators=(",", ":")),
        parent_attempt_fence=attempt.fencing_token,
        repository_attempt_fence=repo_authority.attempt.fencing_token,
        child_revision=child.revision, child_fence=child.fencing_token,
        child_authority_json=child.declared_authority_json, child_arguments_json=child.arguments_json,
        native_binding_json=json.dumps(binding.model_dump(mode="json"), sort_keys=True, separators=(",", ":")),
        parent_row_json=row_json(parent), task_row_json=row_json(task),
        parent_attempt_row_json=row_json(attempt), repository_attempt_row_json=row_json(repo_authority.attempt),
        child_row_json=row_json(child), repository_task_row_json=row_json(repo_authority.task),
        input_artifact_row_json=row_json(input_artifact), consent_row_json=row_json(consent),
        consent_id=consent.id, _seal=_SEAL)
    assert_repository_canonical_source(source)
    return source


@dataclass(frozen=True, slots=True)
class RepositoryIterationAccountingWitness:
    group: TaskProposalGroupV1
    repository_job_id: str
    repository_attempt_id: str
    repository_fence: int
    parent_task_id: str
    parent_attempt_id: str
    native_invocation_id: str
    iteration_index: int
    iteration_id: str
    operation_id: str
    original_deadline_at: datetime
    original_max_cost_microusd: int
    source_checkpoint_digest: str
    _request_body_digest: str | None = field(default=None, repr=False, compare=False)
    _request_route_digest: str | None = field(default=None, repr=False, compare=False)
    _canonical_source: _CanonicalRepositorySource | None = field(default=None, repr=False, compare=False)
    _seal: object = field(default=None, repr=False, compare=False)

    def projection(self) -> dict[str, Any]:
        result = {name: getattr(self, name) for name in self.__dataclass_fields__
                  if not name.startswith("_")}
        result["group"] = self.group.model_dump(mode="json")
        result["original_deadline_at"] = self.original_deadline_at.isoformat()
        return result


def assert_repository_iteration_witness(witness) -> None:
    from src.workflows.inference_accounting import InferenceAccountingError
    if (type(witness) is not RepositoryIterationAccountingWitness or witness._seal is not _SEAL
            or type(witness.group) is not TaskProposalGroupV1
            or type(witness.iteration_index) is not int or not 1 <= witness.iteration_index <= 3
            or not _SHA.fullmatch(witness.iteration_id)
            or not _SHA.fullmatch(witness.source_checkpoint_digest)
            or witness.operation_id != "remote:repo-work:" + witness.iteration_id
            or type(witness.repository_fence) is not int or witness.repository_fence < 1
            or type(witness.original_max_cost_microusd) is not int
            or witness.original_max_cost_microusd < 0
            or witness.original_deadline_at.tzinfo is None
            or witness.original_deadline_at > witness.group.original_deadline_at
            or any(not value for value in (witness.repository_job_id, witness.repository_attempt_id,
                witness.parent_task_id, witness.parent_attempt_id, witness.native_invocation_id))):
        raise InferenceAccountingError("repository_iteration_source_witness_required")


def repository_transport_route_digest(target: dict, runtime_path: str) -> str:
    from src.security.trust_contract import canonical_digest
    return canonical_digest({"runtime_path": runtime_path, "source": target.get("source"),
        "endpoint": str(target.get("api_base") or "").rstrip("/"),
        "profile": target.get("profile"), "model_id": target.get("model_id"),
        "options": target.get("options")})


def verify_repository_iteration_transport_body(body, *, target, runtime_path) -> None:
    """Only a genuine source context can require the fixed iteration body."""
    from src.model_fabric.accounting import current_repository_iteration_accounting_witness
    from src.security.trust_contract import canonical_digest
    from src.workflows.inference_accounting import InferenceAccountingError
    witness = current_repository_iteration_accounting_witness()
    if witness is None:
        return
    assert_repository_iteration_witness(witness)
    if (runtime_path != "strategist_agent" or target.get("source") != "primary"
            or str(target.get("api_base") or "").rstrip("/") != "https://openrouter.ai/api/v1"
            or witness._request_body_digest is None or witness._request_route_digest is None
            or canonical_digest(body) != witness._request_body_digest
            or repository_transport_route_digest(target, runtime_path) != witness._request_route_digest):
        raise InferenceAccountingError("repository_iteration_final_transport_changed")


async def validate_repository_iteration_accounting(db, witness, run, *, rows,
        operation_id, payload_digest, bound_microusd, deadline_at,
        already_reserved=False) -> dict[str, Any]:
    """Recheck private source and both original owners in the accounting writer."""
    from sqlalchemy import select
    from src.db.models import (WorkflowRunState, WorkBoardAttempt, WorkBoardTask,
        WorkBoardInputArtifact, RepoRepairEgressConsent)
    from src.workflows.general_task_guard import _history, child_binding, read_manifest
    from src.workflows.inference_accounting import InferenceAccountingError, _utc
    assert_repository_iteration_witness(witness)
    source = witness._canonical_source
    if (type(source) is not _CanonicalRepositorySource or source._seal is not _SEAL
            or any(not getattr(source, name) for name in (
                "parent_row_json", "task_row_json", "parent_attempt_row_json",
                "repository_attempt_row_json", "child_row_json", "repository_task_row_json",
                "input_artifact_row_json", "consent_row_json", "consent_id"))):
        raise InferenceAccountingError("repository_iteration_staged_source_required")
    now = datetime.now(timezone.utc)
    def blocked(code="repository_iteration_binding_changed"):
        raise InferenceAccountingError(code)
    contacted_wait = bool(source._contacted_wait_parent_json and source._contacted_wait_child_json)
    if contacted_wait and not already_reserved:
        blocked("repository_contacted_wait_cannot_reserve")
    if (run.run_identity != witness.repository_job_id or run.status != "running"
            or run.fencing_token != witness.repository_fence
            or not run.lease_owner or run.lease_expires_at is None
            or _utc(run.lease_expires_at) <= now or run.attempt_count != 1
            or operation_id != witness.operation_id or not _SHA.fullmatch(payload_digest)
            or type(bound_microusd) is not int or bound_microusd < 0
            or _utc(deadline_at) > witness.original_deadline_at
            or _utc(run.deadline_at) > witness.original_deadline_at
            or witness.original_deadline_at <= now):
        blocked()
    repo_attempt = await db.scalar(select(WorkBoardAttempt).where(
        WorkBoardAttempt.attempt_id == witness.repository_attempt_id))
    parent_attempt = await db.scalar(select(WorkBoardAttempt).where(
        WorkBoardAttempt.attempt_id == witness.parent_attempt_id))
    task = await db.scalar(select(WorkBoardTask).where(
        WorkBoardTask.task_id == witness.parent_task_id))
    child = await db.scalar(select(WorkflowRunState).where(
        WorkflowRunState.run_identity == witness.native_invocation_id))
    if (repo_attempt is None or repo_attempt.workflow_run_id != run.run_identity
            or parent_attempt is None or task is None or child is None
            or parent_attempt.task_id != task.task_id):
        blocked()
    repo_task = await db.scalar(select(WorkBoardTask).where(
        WorkBoardTask.task_id == repo_attempt.task_id))
    input_artifact = await db.get(WorkBoardInputArtifact, repo_task.input_artifact_id) if repo_task else None
    consent = await db.get(RepoRepairEgressConsent, source.consent_id)
    def row_json(row):
        return json.dumps(row.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    parent = await db.scalar(select(WorkflowRunState).where(
        WorkflowRunState.run_identity == parent_attempt.workflow_run_id))
    if (parent is None or repo_task is None or input_artifact is None or consent is None
            or parent.status != "paused"
            or row_json(parent) != (source._contacted_wait_parent_json or source.parent_row_json)
            or row_json(task) != source.task_row_json
            or row_json(parent_attempt) != source.parent_attempt_row_json
            or row_json(repo_attempt) != source.repository_attempt_row_json
            or row_json(child) != (source._contacted_wait_child_json or source.child_row_json)
            or row_json(repo_task) != source.repository_task_row_json
            or row_json(input_artifact) != source.input_artifact_row_json
            or row_json(consent) != source.consent_row_json
            or repo_attempt.ended_at is not None or repo_attempt.cancel_requested_at is not None
            or parent_attempt.ended_at is not None or parent_attempt.cancel_requested_at is not None
            or parent.revision != (json.loads(source._contacted_wait_parent_json)["revision"]
                if contacted_wait else source.parent_revision)
            or parent.checkpoint_receipts_json != (json.loads(source._contacted_wait_parent_json)["checkpoint_receipts_json"]
                if contacted_wait else source.parent_checkpoint_json)
            or parent.declared_authority_json != source.parent_authority_json
            or task.task_revision != source.task_revision
            or parent_attempt.fencing_token != source.parent_attempt_fence
            or repo_attempt.fencing_token != source.repository_attempt_fence
            or child.revision != (source.child_revision + 1 if contacted_wait else source.child_revision)
            or child.fencing_token != source.child_fence
            or child.declared_authority_json != source.child_authority_json
            or child.arguments_json != source.child_arguments_json
            or child.status != ("paused" if contacted_wait else "running") or child.attempt_count != 1
            or not child.lease_owner or child.lease_expires_at is None
            or _utc(child.lease_expires_at) <= now or _utc(child.deadline_at) <= now):
        blocked()
    binding = child_binding(child)
    manifest = read_manifest(parent)
    if (manifest is None or manifest.phase != "native_wait"
            or binding.task_id != task.task_id or binding.attempt_id != parent_attempt.attempt_id
            or binding.parent_job_id != parent.run_identity
            or binding.invocation_id not in manifest.admitted_invocation_ids
            or json.loads(source.native_binding_json) != binding.model_dump(mode="json")):
        blocked()
    from src.work_board.general_task import digest
    if (manifest.group_id != witness.group.group_id
            or manifest.group_digest != digest(witness.group.model_dump(mode="json"))):
        blocked("repository_iteration_original_group_changed")
    history = _history(run)
    originals = [item for item in history if item.get("checkpoint_id") == "repository:original:v1"]
    iterations = [item for item in history if item.get("checkpoint_id") ==
                  "repository:iteration:" + witness.iteration_id]
    if len(originals) != 1 or len(iterations) != 1:
        blocked("repository_iteration_source_checkpoint_missing")
    original, iteration = originals[0], iterations[0]
    original_payload, iteration_payload = original.get("payload"), iteration.get("payload")
    from src.workflows.job_runtime import _digest
    if (original.get("safe") is not True or iteration.get("safe") is not True
            or not isinstance(original_payload, dict) or not isinstance(iteration_payload, dict)
            or original.get("state_digest") != _digest(original_payload)
            or original.get("state_digest") != witness.source_checkpoint_digest
            or iteration.get("state_digest") != _digest(iteration_payload)
            or original_payload.get("native_binding") != binding.model_dump(mode="json")
            or original_payload.get("group") != witness.group.model_dump(mode="json")
            or original_payload.get("original_max_cost_microusd") != witness.original_max_cost_microusd
            or original_payload.get("original_deadline_at") != witness.original_deadline_at.isoformat()
            or iteration_payload.get("accounting_binding") != witness.projection()
            or iteration_payload.get("payload_digest") != payload_digest
            or iteration_payload.get("phase") != "model_ready"):
        blocked()
    members = [row for row in rows if row.job_id == witness.repository_job_id]
    existing = [row for row in members if row.operation_id == operation_id]
    if already_reserved != bool(existing) or len(existing) > 1:
        blocked("repository_iteration_reservation_identity_changed")
    if contacted_wait and (existing[0].state != "settled" or existing[0].contact_started_at is None):
        blocked("repository_contacted_wait_requires_settled_response")
    from src.workflows.general_task_accounting import reservation_liability, entry_for
    liability = 0
    for row in members:
        entry = entry_for(row)
        member_binding = entry.get("repository_binding") if entry else None
        if (not entry or entry.get("role") != "repository_iteration"
                or not isinstance(member_binding, dict)
                or member_binding.get("source_checkpoint_digest") != witness.source_checkpoint_digest
                or member_binding.get("repository_job_id") != witness.repository_job_id
                or member_binding.get("group") != witness.group.model_dump(mode="json")):
            blocked("repository_iteration_reservation_source_changed")
        if row.state == "unknown":
            blocked("repository_iteration_unknown")
        liability += reservation_liability(row)
    if liability + (0 if already_reserved else bound_microusd) > witness.original_max_cost_microusd:
        blocked("repository_iteration_original_cost_limit")
    return witness.projection()
