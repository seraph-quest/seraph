"""Closed registered-tool task interpreter on the existing board and job owners.

Planning is data only. This service neither creates another queue nor starts
an agent/model loop; proposal acceptance and durable admission are distinct.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import inspect
import json
from typing import Any, Protocol

from jsonschema import Draft202012Validator

from src.work_board.contracts import (
    DependencyPointer, GeneralTaskCreate, GeneralTaskEnvelope, PlanSpec,
    StrategyResolver, TaskStrategyBinding, ToolDescriptor, WorkBoardInputArtifactCreate,
    WorkBoardOwner, WorkBoardTaskCreate,
)
from src.work_board.repository import BoardError, WorkBoardRepository
from src.db.models import WorkBoardStatus

CAPABILITY = "agent.task.v1"
MAX_BYTES = 64 * 1024


def canonical(value: Any) -> bytes:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_BYTES:
        raise BoardError("general_task_payload_limit", "Task data exceeds 64 KiB", status_code=422)
    return encoded


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def validate_schema(schema: dict, value: Any = None, *, check_value: bool = True):
    """Local schemas only: no reference retrieval, executable expressions or secrets."""
    def closed(item, depth=0):
        if depth > 32:
            raise ValueError("schema nesting limit")
        if isinstance(item, dict):
            for key, child in item.items():
                if key in {"$ref", "$dynamicRef", "$recursiveRef"}:
                    raise ValueError("schema references are excluded")
                closed(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                closed(child, depth + 1)
    canonical(schema)
    closed(schema)
    Draft202012Validator.check_schema(schema)
    if check_value:
        Draft202012Validator(schema).validate(value)


def validate_data(value: Any, *, dependencies: set[str], depth: int = 0):
    from src.work_board.dispatcher import _AUTHORITY_INPUT_KEYS
    if depth > 32:
        raise ValueError("task data nesting limit")
    canonical(value)
    if isinstance(value, dict):
        if "$dependency" in value:
            if set(value) != {"$dependency"}:
                raise ValueError("dependency pointers cannot carry sibling fields")
            pointer = DependencyPointer.model_validate(value["$dependency"])
            if pointer.step_id not in dependencies:
                raise ValueError("pointer requires a declared dependency")
            return
        for key, child in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if normalized in _AUTHORITY_INPUT_KEYS or normalized in {
                "password", "secret", "secret_ref", "api_key", "token", "authorization",
                "credential", "credentials", "credential_refs", "expression", "eval", "shell",
            } or str(key).startswith("$"):
                raise ValueError("privilege-bearing or executable task data is excluded")
            validate_data(child, dependencies=dependencies, depth=depth + 1)
    elif isinstance(value, list):
        for child in value:
            validate_data(child, dependencies=dependencies, depth=depth + 1)
    elif isinstance(value, str) and ("${vault:" in value or value.startswith("secret-ref:")):
        raise ValueError("secret references do not belong in planner data")


def has_pointer(value):
    if isinstance(value, dict):
        return "$dependency" in value or any(has_pointer(item) for item in value.values())
    if isinstance(value, list):
        return any(has_pointer(item) for item in value)
    return False


def resolve_input(value, verified_outputs):
    if isinstance(value, dict):
        if "$dependency" in value:
            pointer = DependencyPointer.model_validate(value["$dependency"])
            selected = verified_outputs[pointer.step_id]
            if pointer.pointer:
                for segment in pointer.pointer[1:].split("/"):
                    segment = segment.replace("~1", "/").replace("~0", "~")
                    if isinstance(selected, list):
                        if not segment.isdigit() or (segment != "0" and segment.startswith("0")):
                            raise ValueError("invalid array pointer")
                        selected = selected[int(segment)]
                    elif isinstance(selected, dict):
                        selected = selected[segment]
                    else:
                        raise ValueError("pointer does not name a JSON value")
            # Detach data from its verified artifact representation.
            return json.loads(canonical(selected))
        return {key: resolve_input(item, verified_outputs) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_input(item, verified_outputs) for item in value]
    return value


class DescriptorRegistry(Protocol):
    def descriptors(self) -> list[ToolDescriptor]: ...
    async def invoke(self, descriptor: ToolDescriptor, inputs: dict, *, principal,
                     job_id: str, fencing_token: int) -> Any: ...


class GeneralTaskService:
    def __init__(self, registry: DescriptorRegistry, *, repository=None,
                 strategy_resolver: StrategyResolver | None = None, planner=None):
        self.registry = registry
        self.repository = repository or WorkBoardRepository()
        self.strategy_resolver = strategy_resolver
        self.planner = planner
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def snapshot(self):
        if not self.started:
            raise BoardError("general_task_inactive", "Task service is inactive", status_code=503)
        descriptors = sorted(self.registry.descriptors(), key=lambda item: item.tool_id)
        if len({item.tool_id for item in descriptors}) != len(descriptors):
            raise BoardError("general_task_tool_identity_conflict", "Duplicate tool identity", status_code=409)
        return descriptors, digest([item.model_dump(mode="json") for item in descriptors])

    async def strategy(self, owner, goal_ref):
        binding = TaskStrategyBinding(status="none", reason="baseline")
        if self.strategy_resolver is not None:
            binding = self.strategy_resolver.resolve(owner, goal_ref, CAPABILITY)
            if inspect.isawaitable(binding):
                binding = await binding
            binding = TaskStrategyBinding.model_validate(binding)
        if binding.status == "blocked":
            raise BoardError("general_task_strategy_blocked", binding.reason, status_code=409)
        return binding

    async def evidence(self, db, owner, references):
        """Only current owner-completed board output is execution evidence."""
        from sqlalchemy import select
        from src.db.models import WorkBoardAttempt, WorkflowRunState
        from src.work_board.review import _verified_workflow_readback
        from src.work_board.input_artifacts import _safe_file_bytes
        from src.workspace import canonical_workspace_root
        from config.settings import settings
        result = []
        for reference in references:
            if not reference.startswith("board-output:"):
                raise BoardError("general_task_evidence_unsupported", "Select verified completed Work output", status_code=422)
            producer = await self.repository.get_task(db, owner, reference.removeprefix("board-output:"))
            if producer.status != WorkBoardStatus.done:
                raise BoardError("general_task_evidence_stale", "Evidence producer is not complete", status_code=409)
            attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == producer.task_id)
                .order_by(WorkBoardAttempt.created_at.desc()).limit(1))
            if attempt is None or await _verified_workflow_readback(db, producer, attempt) is None:
                raise BoardError("general_task_evidence_unverified", "Evidence needs verified readback", status_code=409)
            run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == attempt.workflow_run_id))
            if run is None or run.status != "succeeded":
                raise BoardError("general_task_evidence_unverified", "Evidence run has not succeeded", status_code=409)
            artifacts, effects = json.loads(run.artifact_receipts_json), json.loads(run.effect_receipts_json)
            artifact = next((item for item in artifacts if item.get("exists") and any(
                effect.get("receipt_kind") == "readback" and effect.get("status") == "succeeded"
                and effect.get("target_path") == item.get("file_path")
                and effect.get("content_sha256") == item.get("content_sha256") for effect in effects)), None)
            if not artifact:
                raise BoardError("general_task_evidence_unverified", "Evidence artifact lacks readback", status_code=409)
            path = artifact["file_path"]
            if path.startswith(("/", "~")) or ".." in path.split("/"):
                raise BoardError("general_task_evidence_unverified", "Evidence path is invalid", status_code=409)
            size = artifact.get("size_bytes") or artifact.get("byte_count")
            if type(size) is not int or not 0 < size <= MAX_BYTES:
                raise BoardError("general_task_evidence_limit", "Evidence exceeds the finite data allowance", status_code=409)
            raw = _safe_file_bytes(canonical_workspace_root(settings.workspace_dir) / path,
                expected_digest=artifact["content_sha256"], expected_size=size)
            result.append({"reference": reference, "producer_revision": producer.task_revision,
                "producer_attempt_ref": attempt.attempt_id, "file_path": path,
                "content_sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": size})
        return result

    async def validate(self, owner: WorkBoardOwner, request: GeneralTaskCreate):
        descriptors, tool_digest = self.snapshot()
        if request.plan is None:
            raise BoardError("general_task_plan_required", "Generate and review a typed plan first", status_code=422)
        if request.input.tool_set_digest != tool_digest:
            raise BoardError("general_task_tool_set_changed", "Refresh the current tool contract", status_code=409)
        by_id = {item.tool_id: item for item in descriptors}
        selected = {}
        try:
            validate_schema(request.input.requested_output, check_value=False)
            for step in request.plan.steps:
                descriptor = by_id.get(step.tool_id)
                if descriptor is None:
                    raise ValueError("tool unavailable: " + step.tool_id)
                validate_schema(descriptor.input_schema, check_value=False)
                validate_schema(descriptor.output_schema, check_value=False)
                validate_schema(step.output_contract, check_value=False)
                validate_data(step.input, dependencies=set(step.depends_on))
                if not has_pointer(step.input):
                    validate_schema(descriptor.input_schema, step.input)
                selected[step.tool_id] = descriptor
        except Exception as exc:
            raise BoardError("general_task_plan_invalid", "Plan violates a registered tool contract", status_code=422) from exc
        strategy = await self.strategy(owner, request.input.goal_ref)
        return GeneralTaskEnvelope(task_input=request.input, plan=request.plan,
            descriptors=list(selected.values()), strategy=strategy)

    async def create(self, db, owner, request: GeneralTaskCreate):
        from src.work_board.input_artifacts import prepare_input_artifact
        await self.repository._validate_goal(db, owner, goal_id=request.input.goal_ref,
            goal_revision=request.goal_revision)
        await self.strategy(owner, request.input.goal_ref)
        evidence = await self.evidence(db, owner, request.input.evidence_refs)
        if request.plan is None:
            if self.planner is None:
                raise BoardError("general_task_planner_inactive", "Restore the governed task planner", status_code=503)
            descriptors, tool_digest = self.snapshot()
            if request.input.tool_set_digest is not None and request.input.tool_set_digest != tool_digest:
                raise BoardError("general_task_tool_set_changed", "Refresh the current tool contract", status_code=409)
            task_input = request.input.model_copy(update={"tool_set_digest": tool_digest})
            plan = await self.planner.propose(db, owner, task_input, descriptors,
                goal_revision=request.goal_revision, idempotency_key=request.idempotency_key)
            request = GeneralTaskCreate(goal_revision=request.goal_revision,
                idempotency_key=request.idempotency_key, input=task_input, plan=plan,
                expected_plan_revision=plan.revision)
        envelope = await self.validate(owner, request)
        envelope = envelope.model_copy(update={"evidence": evidence})
        artifact = await prepare_input_artifact(db, owner, WorkBoardInputArtifactCreate(
            schema_version=1, capability_id=CAPABILITY, goal_id=request.input.goal_ref,
            goal_revision=request.goal_revision, input=envelope.model_dump(mode="json"),
            idempotency_key="general:" + request.idempotency_key))
        # prepare_input_artifact reserves durably before filesystem I/O; task
        # publication binds that exact artifact under the repository writer CAS.
        return await self.repository.create_task(db, owner, WorkBoardTaskCreate(
            title=request.input.intent[:200], body="General registered-tool task",
            goal_id=request.input.goal_ref, goal_revision=request.goal_revision,
            capability_id=CAPABILITY, input_artifact_id=artifact.artifact_id,
            status=WorkBoardStatus.todo if request.accept else WorkBoardStatus.triage,
            idempotency_scope="general-task", idempotency_key=request.idempotency_key,
            requires_review=True))

    async def plan(self, db, owner, task_id):
        from src.work_board.dispatcher import _parse_typed_input
        task = await self.repository.get_task(db, owner, task_id)
        if task.capability_id != CAPABILITY:
            raise BoardError("general_task_unavailable", "General task unavailable", status_code=404)
        envelope = GeneralTaskEnvelope.model_validate(_parse_typed_input(task))
        return {"task_id": task.task_id, "task_revision": task.task_revision,
            "accepted": task.status != WorkBoardStatus.triage,
            **envelope.model_dump(mode="json"), "no_learning": True}

    async def update_plan(self, db, owner, task_id, request):
        from sqlalchemy import select
        from src.db.models import WorkBoardAttempt
        from src.work_board.dispatcher import _parse_typed_input
        from src.work_board.input_artifacts import (
            prepare_input_artifact, stage_input_artifact, recheck_staged_input,
            resolve_input_artifact_for_task, bind_input_artifact,
            InputRetirementWitness, _metadata_digest, _revoke_input_artifact_locked,
        )
        from src.work_board.repository import _begin_sqlite_immediate, BoardRevisionConflict
        from src.work_board.pipelines import root_binding
        task = await self.repository.get_task(db, owner, task_id)
        if task.capability_id != CAPABILITY or task.status != WorkBoardStatus.triage:
            raise BoardError("general_task_plan_locked", "Only inert Triage plans are editable", status_code=409)
        if task.task_revision != request.expected_revision:
            raise BoardRevisionConflict(task_id, request.expected_revision, task.task_revision)
        prior = GeneralTaskEnvelope.model_validate(_parse_typed_input(task))
        if prior.plan.revision != request.expected_plan_revision:
            raise BoardError("general_task_plan_revision_stale", "Plan changed before editing", status_code=409)
        original = await resolve_input_artifact_for_task(db, owner, artifact_id=task.input_artifact_id,
            goal_id=task.goal_id, goal_revision=task.goal_revision, capability_id=CAPABILITY,
            expected_task_id=task_id)
        retirement = InputRetirementWitness(original.row.artifact_id, original.row.revision,
            _metadata_digest(original.row), task_id, task.task_revision, task.typed_input_ref,
            original.row.payload_sha256, original.row.size_bytes, canonical(root_binding()))
        _, tool_digest = self.snapshot()
        revised_request = GeneralTaskCreate(goal_revision=task.goal_revision,
            idempotency_key=request.idempotency_key,
            input=prior.task_input.model_copy(update={"tool_set_digest": tool_digest}),
            plan=request.plan, expected_plan_revision=request.plan.revision)
        revised = await self.validate(owner, revised_request)
        revised = revised.model_copy(update={"evidence": await self.evidence(db, owner, prior.task_input.evidence_refs)})
        metadata = await prepare_input_artifact(db, owner, WorkBoardInputArtifactCreate(
            schema_version=1, capability_id=CAPABILITY, goal_id=task.goal_id,
            goal_revision=task.goal_revision, input=revised.model_dump(mode="json"),
            idempotency_key="general-edit:" + request.idempotency_key))
        staged = await stage_input_artifact(db, owner, artifact_id=metadata.artifact_id,
            capability_id=CAPABILITY, goal_id=task.goal_id, goal_revision=task.goal_revision)
        await _begin_sqlite_immediate(db)
        current = await self.repository.get_task(db, owner, task_id)
        await db.refresh(current)
        if current.task_revision != request.expected_revision or current.status != WorkBoardStatus.triage:
            raise BoardError("general_task_plan_revision_stale", "Task changed before plan binding", status_code=409)
        if await db.scalar(select(WorkBoardAttempt.attempt_id).where(WorkBoardAttempt.task_id == task_id).limit(1)):
            raise BoardError("general_task_plan_locked", "Attempt history locks the immutable plan", status_code=409)
        await self.repository._validate_goal(db, owner, goal_id=current.goal_id, goal_revision=current.goal_revision)
        self.recheck(revised)
        binding_request = WorkBoardTaskCreate(title=current.title, goal_id=current.goal_id,
            goal_revision=current.goal_revision, capability_id=CAPABILITY,
            input_artifact_id=metadata.artifact_id, idempotency_key=request.idempotency_key)
        resolved = await recheck_staged_input(db, owner, binding_request, witness=staged)
        # Reuse the existing input-retirement CAS. Old bytes remain private,
        # immutable historical evidence and the revoked row cannot execute.
        await _revoke_input_artifact_locked(db, owner, witness=retirement)
        await self.repository._cas_task_update(db, owner, current,
            expected_revision=request.expected_revision,
            values={"input_artifact_id": metadata.artifact_id,
                "typed_input_ref": metadata.typed_input_ref, "typed_input_digest": metadata.typed_input_digest,
                "task_revision": request.expected_revision + 1, "updated_at": datetime.now(timezone.utc)})
        await bind_input_artifact(db, owner, artifact=resolved,
            task_id=task_id, task_revision=current.task_revision)
        await self.repository._event(db, current, owner, kind="task.plan_revised",
            metadata={"plan_revision": revised.plan.revision, "task_revision": current.task_revision})
        return current

    def recheck(self, envelope: GeneralTaskEnvelope):
        current, _ = self.snapshot()
        by_id = {item.tool_id: item for item in current}
        for prior in envelope.descriptors:
            if prior != by_id.get(prior.tool_id):
                raise BoardError("general_task_tool_contract_changed", "Restore or revise the tool contract", status_code=409)

    async def recheck_authority(self, db, owner, envelope):
        self.recheck(envelope)
        binding = await self.strategy(owner, envelope.task_input.goal_ref)
        if binding != envelope.strategy:
            raise BoardError("general_task_strategy_changed", "Review the current task method", status_code=409)
        evidence = await self.evidence(db, owner, envelope.task_input.evidence_refs)
        if evidence != envelope.evidence:
            raise BoardError("general_task_evidence_changed", "Review changed evidence", status_code=409)

    async def execute(self, jobs, *, job_id, owner, fence, envelope, principal):
        """A step intent is durable before invocation; unknown work never retries."""
        outputs = {}
        by_id = {item.tool_id: item for item in envelope.descriptors}
        remaining = list(envelope.plan.steps)
        while remaining:
            step = next(item for item in remaining if set(item.depends_on) <= outputs.keys())
            from src.db.engine import get_session
            async with get_session() as db:
                await self.recheck_authority(db, WorkBoardOwner(
                    principal_id=principal.principal_id, session_id=principal.operator_session_id), envelope)
            inputs = resolve_input(step.input, outputs)
            validate_data(inputs, dependencies=set())
            descriptor = by_id[step.tool_id]
            validate_schema(descriptor.input_schema, inputs)
            projection = await jobs.get_job(job_id)
            checkpoint_id = "general:step:" + step.step_id
            previous = [item for item in projection.get("checkpoints", [])
                        if item.get("checkpoint_id") == checkpoint_id]
            if previous:
                raise BoardError("general_task_unresolved_step", "Existing step intent requires reconciliation", status_code=409)
            await jobs.record_checkpoint(job_id, checkpoint_id=checkpoint_id,
                state={"step_id": step.step_id, "descriptor_digest": digest(descriptor.model_dump(mode="json")),
                       "input_digest": digest(inputs), "phase": "intent"},
                owner=owner, fencing_token=fence)
            await jobs.record_effect(job_id, effect_type="general_tool_call",
                effect_id="general:" + step.step_id + ":intent", status="intent",
                details={"tool_id": descriptor.tool_id, "step_id": step.step_id,
                         "input_digest": digest(inputs), "no_learning": True},
                owner=owner, fencing_token=fence)
            operator_result = await asyncio.wait_for(self.registry.invoke(descriptor, inputs,
                principal=replace(principal, job_id=job_id), job_id=job_id, fencing_token=fence),
                timeout=descriptor.deadline)
            validate_schema(descriptor.output_schema, operator_result)
            validate_schema(step.output_contract, operator_result)
            canonical(operator_result)
            # Private artifact adoption/readback uses the existing bounded writer.
            from src.work_board.research_artifacts import write_verified, read
            artifact = await write_verified(jobs, job_id=job_id, owner=owner, fence=fence,
                creation_digest=digest([envelope.model_dump(mode="json"), step.step_id]), slot=0,
                kind="manifest", content=canonical({"step_id": step.step_id, "output": operator_result}),
                max_bytes=MAX_BYTES)
            actual = json.loads(read(artifact["file_path"], artifact["content_sha256"]))
            outputs[step.step_id] = actual["output"]
            await jobs.record_effect(job_id, effect_type="general_tool_call",
                effect_id="general:" + step.step_id + ":verified", status="succeeded",
                target_path=artifact["file_path"], content_sha256=artifact["content_sha256"],
                details={"step_id": step.step_id, "tool_id": descriptor.tool_id,
                         "verified": True, "no_learning": True},
                owner=owner, fencing_token=fence)
            await jobs.record_checkpoint(job_id, checkpoint_id="general:verified:" + step.step_id,
                state=artifact, checkpoint_payload=artifact, owner=owner, fencing_token=fence)
            remaining.remove(step)
        final = outputs[envelope.plan.steps[-1].step_id]
        validate_schema(envelope.task_input.requested_output, final)
        return {"verified": True, "output_digest": digest(final), "step_count": len(outputs),
            "learning": "no_learning", "no_learning": True,
            "content_sha256": artifact["content_sha256"],
            "readback_id": "general-readback:" + digest([job_id, artifact])[:32],
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "result_refs": [artifact], "artifact_refs": [artifact]}
