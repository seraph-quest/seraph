"""Fixed research creation CAS on canonical job rows; no separate executor."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from sqlalchemy import select, text, update

from src.db.models import WorkBoardAttempt, WorkBoardStatus, WorkBoardTask, WorkflowRunState
from src.work_board.research_contracts import CHILD_KIND, PARENT_CAPABILITY, PARENT_KIND, ResearchDossierInput


def checkpoint(projection, checkpoint_id):
    records = projection.get("checkpoints", [])
    matching = [item.get("payload") for item in records if item.get("checkpoint_id") == checkpoint_id]
    if len(matching) != 1 or not isinstance(matching[0], dict):
        return None
    return matching[0]


async def create_fixed_children(jobs, *, parent_id, runtime_owner, runtime_fence, task_id, attempt_id,
        board_revision, board_fence, board_owner, inputs):
    """Insert all fixed child intents and creation evidence in ONE writer CAS."""
    from src.workflows.job_runtime import (_append_parent_fence_condition, _assert_canonical_goal_fence,
        _canonical, _digest, _binding, _safe_durable_inputs, _safe_durable_authority, _validate_admission_authority, DurableJobIdentity,
        DurableJobIdempotencyConflict, DurableJobLeaseError, DurableJobSpec)
    from src.work_board.pipelines import root_binding
    from src.work_board.pipeline_contracts import digest
    model = ResearchDossierInput.model_validate(dict(inputs))
    now = datetime.now(timezone.utc)
    async with jobs._session() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        parent = await jobs._fetch(db, parent_id)
        jobs._assert_lease(parent, owner=runtime_owner, fencing_token=runtime_fence)
        from src.workflows.research_guard import assert_research_operator_session
        await assert_research_operator_session(db, parent, now=now)
        task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
        attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == attempt_id))
        if (parent.job_kind != PARENT_KIND or parent.status != "running" or parent.branch_depth != 0
            or parent.parent_job_id or parent.capability_version != "1" or task is None or attempt is None
            or task.capability_id != PARENT_CAPABILITY or task.status != WorkBoardStatus.running
            or task.task_revision != board_revision or attempt.task_id != task_id or attempt.workflow_run_id != parent_id
            or attempt.ended_at or attempt.cancel_requested_at or attempt.fencing_token != board_fence
            or attempt.lease_owner != board_owner or attempt.lease_expires_at is None
            or attempt.lease_expires_at.replace(tzinfo=timezone.utc) <= now
            or parent.owner_principal_id != task.owner_principal_id or parent.session_id != task.owner_session_id):
            raise DurableJobLeaseError("research creation requires the exact live parent and board attempt")
        await _assert_canonical_goal_fence(db, goal_id=parent.goal_id, goal_revision=parent.goal_revision,
            owner_kind=parent.owner_kind, owner_principal_id=parent.owner_principal_id,
            session_id=parent.session_id, authority=parent.declared_authority_json)
        authority = json.loads(parent.declared_authority_json)
        if authority.get("live_root_digest") != digest(root_binding()) or authority.get("typed_input_digest") != task.typed_input_digest:
            raise DurableJobLeaseError("research original root/input binding changed")
        from src.work_board.input_artifacts import INPUT_ARTIFACT_SCHEMA_VERSION, _canonical_json
        import hashlib
        envelope = {"schema_version": INPUT_ARTIFACT_SCHEMA_VERSION, "capability_id": PARENT_CAPABILITY,
            "input": model.model_dump(mode="json", exclude_none=True)}
        if hashlib.sha256(_canonical_json(envelope)).hexdigest() != task.typed_input_digest:
            raise DurableJobLeaseError("research child group must use the exact admitted source input")
        from src.work_board.research_readback import binds, original_admission_current
        if not binds(task, attempt, parent, typed_inputs=model):
            raise DurableJobLeaseError("research original parent identity changed")
        admission = await original_admission_current(db, task, attempt, parent)
        creation = {"schema_version": 2, "board_task_id": task_id, "board_attempt_id": attempt_id,
            "creation_board_fence": board_fence, "creation_job_fence": runtime_fence,
            "parent_input_digest": parent.input_digest, "live_root_digest": authority["live_root_digest"],
            "child_ids": [f"{parent_id}:child:{slot}" for slot in range(len(model.perspectives))],
            "model_policy_digest": authority["model_policy_digest"], "no_learning": True}
        creation.update(parent_authority_digest=parent.authority_digest,
            parent_run_fingerprint=parent.run_fingerprint, research_authority_schema_version=2,
            original_deadline_at=admission["original_deadline_at"], research_admission_digest=_digest(admission))
        creation["creation_digest"] = _digest(creation)
        history = json.loads(parent.checkpoint_receipts_json)
        existing = [item for item in history if item.get("checkpoint_id") == "research:creation"]
        if existing:
            # Creation leases are immutable; a new lease cannot mint new child
            # intent IDs or reinterpret an already admitted operation.
            if len(existing) != 1:
                raise DurableJobIdempotencyConflict("research creation checkpoint is ambiguous")
            prior = existing[0].get("payload")
            if not isinstance(prior, dict) or prior.get("board_attempt_id") != attempt_id or prior.get("parent_input_digest") != parent.input_digest:
                raise DurableJobIdempotencyConflict("research creation checkpoint changed")
            rows = list((await db.execute(select(WorkflowRunState).where(WorkflowRunState.parent_job_id == parent_id))).scalars())
            if sorted(row.run_identity for row in rows) != sorted(prior["child_ids"]):
                raise DurableJobIdempotencyConflict("research child group is incomplete; never top it up")
            for row in rows:
                child_authority = json.loads(row.declared_authority_json)
                if row.job_kind != CHILD_KIND or row.parent_fencing_token != prior["creation_job_fence"] or child_authority.get("parent_creation_digest") != prior["creation_digest"]:
                    raise DurableJobIdempotencyConflict("research child creation identity changed")
            return prior
        if await db.scalar(select(WorkflowRunState.id).where(WorkflowRunState.parent_job_id == parent_id).limit(1)):
            raise DurableJobIdempotencyConflict("research child rows precede their creation checkpoint")
        child_deadline = min(parent.deadline_at.replace(tzinfo=timezone.utc), now + timedelta(seconds=120))
        for slot, child_id in enumerate(creation["child_ids"]):
            child_authority = {**authority, "capability_id": "work.readonly-research-child.v1",
                "parent_creation_digest": creation["creation_digest"], "research_slot": slot,
                "parent_board_task_id": task_id, "parent_board_attempt_id": attempt_id,
                "creation_board_fence": board_fence, "creation_job_fence": runtime_fence}
            child_inputs = {"parent_input_digest": parent.input_digest, "research_slot": slot,
                "parent_creation_digest": creation["creation_digest"], "source_slots": model.perspectives[slot].source_slots,
                "source_manifest_digest": _digest([model.sources[index].model_dump(mode="json") for index in model.perspectives[slot].source_slots]),
                "source_permission_revision": authority["model_policy_revision"],
                "model_policy_revision": authority["model_policy_revision"],
                "slot_allowance_microusd": authority["research_slot_allowance_microusd"],
                "no_learning": True}
            identity = DurableJobIdentity(job_id=child_id, owner_kind="user", owner_principal_id=parent.owner_principal_id,
                job_kind=CHILD_KIND, capability_version="1", idempotency_scope="research-child-slot",
                idempotency_key=f"{parent_id}:{slot}")
            spec = DurableJobSpec(identity=identity, inputs=child_inputs, declared_authority=child_authority,
                parent_job_id=parent_id, parent_fencing_token=runtime_fence,
                session_id=parent.session_id, operator_session_id=parent.operator_session_id,
                goal_id=parent.goal_id, goal_revision=parent.goal_revision, deadline_at=child_deadline,
                priority=parent.priority, max_attempts=1, resource_claims=("remote_inference",),
                run_fingerprint=_digest({"input": child_inputs, "authority": child_authority}))
            _validate_admission_authority(spec)
            input_digest, safe_inputs = _safe_durable_inputs(child_inputs)
            from src.work_board.research_parent import fixed_child_projection
            safe_authority = _safe_durable_authority(child_authority, native_job_kind=CHILD_KIND,
                native_research_projection=fixed_child_projection(parent, child_authority))
            # Use the canonical admission encodings, identity and digests. The
            # fixed group bypasses no root limit: its already-admitted parent
            # owns the sole Goal outstanding slot, as native routine leaves do.
            db.add(WorkflowRunState(run_identity=child_id, root_run_identity=parent.root_run_identity,
                parent_run_identity=parent_id, parent_job_id=parent_id, parent_fencing_token=runtime_fence,
                workflow_name=CHILD_KIND, tool_name=CHILD_KIND, job_kind=CHILD_KIND, capability_version="1",
                owner_kind="user", owner_principal_id=parent.owner_principal_id,
                session_id=parent.session_id, conversation_id=parent.conversation_id,
                operator_session_id=parent.operator_session_id, goal_id=parent.goal_id, goal_revision=parent.goal_revision,
                status="accepted", branch_depth=1, run_fingerprint=spec.run_fingerprint,
                arguments_json=_canonical(safe_inputs), approval_context_json=_canonical(safe_authority),
                input_digest=input_digest, authority_digest=_digest(child_authority), budget_digest=_digest({"budget_microusd": None}),
                idempotency_scope=identity.idempotency_scope, idempotency_key=identity.idempotency_key,
                idempotency_binding=_binding(owner_principal_id=identity.owner_principal_id, goal_id=parent.goal_id,
                    goal_revision=parent.goal_revision, idempotency_scope=identity.idempotency_scope,
                    dedupe_key=identity.idempotency_key), priority=parent.priority,
                resource_claims_json=_canonical(["remote_inference"]), declared_authority_json=_canonical(safe_authority),
                deadline_at=child_deadline.replace(tzinfo=None), max_attempts=1, fencing_token=0, revision=1, attempt_count=0))
        history.insert(0, {"checkpoint_id": "research:creation", "state_digest": _digest(creation),
            "state_keys": sorted(creation), "safe": True, "payload": creation,
            "recorded_at": now.isoformat(), "fencing_token": runtime_fence})
        conditions = [WorkflowRunState.run_identity == parent_id, WorkflowRunState.status == "running",
            WorkflowRunState.revision == parent.revision, WorkflowRunState.fencing_token == runtime_fence,
            WorkflowRunState.lease_owner == runtime_owner, WorkflowRunState.lease_expires_at > now]
        _append_parent_fence_condition(conditions, parent, now=now)
        result = await db.execute(update(WorkflowRunState).where(*conditions).values(
            checkpoint_receipts_json=_canonical(history), revision=parent.revision+1,
            updated_at=now.replace(tzinfo=None)).execution_options(synchronize_session=False))
        if result.rowcount != 1:
            raise DurableJobLeaseError("research creation lost its parent CAS")
        await db.flush()
        return creation


async def adopt_discovery_artifact(jobs, *, job_id, owner, fence, artifact):
    """Exact staged programme output, under the current native artifact CAS."""
    from src.work_board.research_artifacts import DiscoveryStagedArtifact, DISCOVERY_ARTIFACT_LIMITS, discovery_prefix, sha
    from src.work_board.research_parent import discovery_authority, DISCOVERY_KIND
    from src.artifacts.registry import artifact_id_for
    from src.workflows.research_guard import assert_discovery_authority
    from src.workflows.job_runtime import _canonical, _append_goal_fence_condition, _effect_ledger_or_raise, DurableJobLeaseError
    if type(artifact) is not DiscoveryStagedArtifact or artifact.job_id != job_id:
        raise ValueError("exact native discovery artifact proof required")
    if (artifact.kind not in DISCOVERY_ARTIFACT_LIMITS or type(artifact.slot) is not int
            or not 0 <= artifact.slot < 4 or type(artifact.content) is not bytes
            or not 0 < len(artifact.content) <= DISCOVERY_ARTIFACT_LIMITS[artifact.kind]
            or sha(artifact.content) != artifact.reference.digest
            or not artifact.file_path.startswith(discovery_prefix(artifact.programme_id))
            or any(p in {"", ".", ".."} for p in artifact.file_path.split("/"))):
        raise ValueError("discovery staged artifact binding invalid")
    expected_id = artifact_id_for(file_path=artifact.file_path, artifact_type="goal_discovery_" + artifact.kind,
        producer=DISCOVERY_KIND, run_id=job_id, content_sha256=artifact.reference.digest)
    if artifact.reference.artifact_id != expected_id:
        raise ValueError("discovery canonical artifact identity changed")
    now = datetime.now(timezone.utc)
    async with jobs._session() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        run = await jobs._fetch(db, job_id)
        jobs._assert_lease(run, owner=owner, fencing_token=fence)
        await assert_discovery_authority(db, run.declared_authority_json, run=run)
        authority = discovery_authority(run.declared_authority_json)
        if run.status != "running" or run.deadline_at.replace(tzinfo=timezone.utc) <= now or authority.programme_binding.programme_id != artifact.programme_id:
            raise DurableJobLeaseError("discovery output has no current original attempt")
        identifier = f"discovery:artifact:{artifact.kind}:{artifact.slot}"
        payload = {"artifact_ref": artifact.reference.model_dump(mode="json"), "file_path": artifact.file_path,
            "kind": artifact.kind, "slot": artifact.slot, "job_id": job_id,
            "programme_id": artifact.programme_id, "byte_count": len(artifact.content),
            "producer_fence": fence, "no_learning": True}
        from src.workflows.research_guard import current_discovery_witness
        witness = current_discovery_witness()
        declared = next((output for step in witness.plan.steps for output in step.output_slots if output.slot == artifact.kind), None)
        if (declared is not None and len(artifact.content) > min(declared.max_bytes, witness.plan.limits.max_output_bytes)
                or artifact.kind in {"snapshot", "draft"} and len(artifact.content) > witness.plan.limits.max_output_bytes):
            raise ValueError("discovery output exceeds its original declared byte cap")
        if artifact.kind == "manifest":
            from src.workflows.research_sources import compile_discovery_search_derivation
            from src.guardian.research_plan_contracts import SearchManifestV1
            payload["search_derivation"] = compile_discovery_search_derivation(witness.plan, witness.artifacts,
                _effect_ledger_or_raise(run.effect_receipts_json), SearchManifestV1.model_validate_json(artifact.content),
                artifact.reference, job_id=job_id)
        if artifact.kind == "brief":
            from src.workflows.research_sources import discovery_stage_inputs, is_local_unsupported_discovery_brief
            value = json.loads(artifact.content)
            local_negative = is_local_unsupported_discovery_brief(value, witness.artifacts, _effect_ledger_or_raise(run.effect_receipts_json))
            discovery_stage_inputs(witness.plan, witness.artifacts, "plan_queries" if local_negative else "prepare_brief")
        history = json.loads(run.checkpoint_receipts_json)
        existing = [p.get("payload") for p in history if p.get("checkpoint_id") == identifier]
        if existing:
            if len(existing) != 1 or any(existing[0].get(key) != value for key, value in payload.items() if key != "producer_fence"):
                raise DurableJobLeaseError("original discovery artifact cannot be rebound")
            return existing[0]
        history.append({"checkpoint_id": identifier, "payload": payload, "recorded_at": now.isoformat()})
        artifacts = json.loads(run.artifact_receipts_json)
        artifacts.append({"artifact_id": expected_id, "artifact_type": "goal_discovery_" + artifact.kind,
            "file_path": artifact.file_path, "producer": DISCOVERY_KIND,
            "content_sha256": artifact.reference.digest, "size_bytes": len(artifact.content),
            "exists": True, "recorded_at": now.isoformat()})
        readbacks = _effect_ledger_or_raise(run.effect_receipts_json)
        readbacks.append({"effect_id": "discovery-artifact:" + expected_id, "receipt_kind": "readback",
            "readback_id": "discovery-readback-" + expected_id,
            "effect_type": "research_artifact_readback", "target_path": artifact.file_path,
            "target_digest": artifact.reference.digest, "content_sha256": artifact.reference.digest,
            "status": "succeeded", "verified_at": now.isoformat(), "recorded_at": now.isoformat(),
            "fencing_token": fence, "reconciled": True, "reconciliation_status": "resolved",
            "details": {"verified": True, "no_learning": True}})
        conditions = [WorkflowRunState.id == run.id, WorkflowRunState.revision == run.revision,
            WorkflowRunState.status == "running", WorkflowRunState.lease_owner == owner,
            WorkflowRunState.fencing_token == fence, WorkflowRunState.lease_expires_at > now]
        _append_goal_fence_condition(conditions, run)
        result = await db.execute(update(WorkflowRunState).where(*conditions).values(
            checkpoint_receipts_json=_canonical(history), artifact_receipts_json=_canonical(artifacts),
            effect_receipts_json=_canonical(readbacks), revision=run.revision + 1))
        if result.rowcount != 1:
            raise DurableJobLeaseError("discovery original output CAS lost")
        await db.commit()
        return payload
