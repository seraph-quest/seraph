"""Local document preparation using adopted private evidence, never model input."""
from pydantic import Field, field_validator
from typing import Literal
from sqlalchemy import select
from src.work_board.contracts import ClosedTaskModel, DocumentTaskBinding, GeneralTaskEnvelope
from src.work_board.repository import BoardError
from src.work_board import document_pairs as sources
from src.work_board.documents import CAPABILITY, DocumentEvidence, OUTPUT_LIMIT, current_source_root
from src.work_board.general_task import canonical, digest


class PreparationCreate(ClosedTaskModel):
    artifact_ref: str = Field(pattern=r"^document-source:[a-f0-9-]{36}$")
    expected_source_revision: int = Field(ge=1)
    citation_refs: list[str] = Field(min_length=1, max_length=16)
    acknowledge_local_use: Literal[True]
    idempotency_key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")

    @field_validator("citation_refs")
    @classmethod
    def unique_leaf_refs(cls, value):
        return DocumentTaskBinding.exact_unique_refs(value)

    @field_validator("acknowledge_local_use", mode="before")
    @classmethod
    def literal_local_ack(cls, value):
        return DocumentTaskBinding.literal_local_ack(value)


def selected_view(evidence, references):
    """Table containers are not leaves: selecting one cannot reveal other cells."""
    if not references or len(references) > 16 or len(set(references)) != len(references):
        raise BoardError("document_selection_invalid", "Select 1–16 unique citations", status_code=422)
    leaves = {}
    for section in evidence.sections:
        entries = section.table_cells or [section]
        for item in entries:
            if item.source_ref in leaves:
                raise BoardError("document_citation_ambiguous", "Source citations are ambiguous", status_code=409)
            leaves[item.source_ref] = item.model_dump(mode="json", exclude={"table_cells"})
    if any(ref not in leaves for ref in references):
        raise BoardError("document_citation_missing", "Select current leaf citations", status_code=409)
    result = [leaves[ref] for ref in references]
    if len(canonical({"task_id": "x" * 128, "status": "succeeded", "sections": result, "no_learning": True, "provider_contacts": 0})) > 16384:
        raise BoardError("document_preparation_too_large", "Select less material; the complete private view exceeds 16 KiB", status_code=422)
    return result


async def resolve(db, owner, binding, *, goal_id=None, operator=None, metadata_only=False):
    from src.work_board.input_artifacts import _metadata_digest
    row, value = await sources.owned(db, owner, binding.artifact_ref.split(":", 1)[1],
        revision=binding.source_revision, capability=CAPABILITY)
    await sources.authority(db, owner, row, value, dict(sources.root_binding()))
    await current_source_root(db, owner, operator)
    if (row.metadata_digest != binding.metadata_digest or row.metadata_digest != _metadata_digest(row) or value.get("phase") != "sealed"
        or value.get("live_writer") or value.get("reason") == "document_output_cleanup_required"
        or not value.get("evidence") or not value.get("read_request_digest")
        or not value.get("witness_digest") or (goal_id is not None and row.goal_id != goal_id)
        or binding.selection_digest != digest(binding.citation_refs)):
        raise BoardError("document_preparation_source_changed", "Read and select the current adopted document", status_code=409)
    if metadata_only:
        return row, None
    source_bytes = sources.read_private(sources.source_path(row, value, "source"), value["sources"]["source"], maximum=16 * 1024 * 1024)
    if sources.sha256(source_bytes) != value["input"]["source"]["sha256"]:
        raise BoardError("document_preparation_source_changed", "Original encrypted source readback changed", status_code=409)
    raw = sources.read_private(sources.source_path(row, value, "evidence"), value["evidence"], maximum=OUTPUT_LIMIT)
    evidence = DocumentEvidence.model_validate_json(raw)
    if evidence.source_digest != value["input"]["source"]["sha256"]:
        raise BoardError("document_preparation_source_changed", "Private source readback changed", status_code=409)
    return row, selected_view(evidence, binding.citation_refs)


def check_envelope(envelope):
    binding = envelope.task_input.document_source
    if binding is None:
        return
    limits = envelope.task_input.limits
    current_descriptor = descriptor()
    if (envelope.task_input.inference_egress_acknowledged or envelope.task_input.evidence_refs
        or envelope.task_input.intent != "Prepare selected document citations locally"
        or limits.max_inference_calls or limits.max_cost_microusd or limits.max_outstanding_children
        or limits.max_steps != 1 or envelope.plan is None or len(envelope.plan.steps) != 1
        or envelope.plan.steps[0].tool_id != "document_prepare"
        or envelope.plan.steps[0].step_id != "prepare" or envelope.plan.steps[0].depends_on
        or envelope.descriptors != [current_descriptor]
        or envelope.task_input.requested_output != current_descriptor.output_schema
        or envelope.plan.steps[0].output_contract != current_descriptor.output_schema
        or envelope.plan.steps[0].input != {"selection_digest": binding.selection_digest}):
        raise BoardError("document_local_plan_required", "Use the exact local document plan with zero inference", status_code=422)


def original_job_digest(task, attempt, envelope):
    """Match the existing dispatcher envelope, including its immutable handoffs."""
    from src.work_board.dispatcher import WorkBoardDispatcher
    from src.workflows.job_runtime import _digest
    inputs = {"task_id": task.task_id, "attempt_id": attempt.attempt_id,
        "capability_id": task.capability_id, "typed_input_ref": task.typed_input_ref,
        "typed_input_digest": task.typed_input_digest, **envelope.model_dump(mode="json", exclude_none=True)}
    handoffs = WorkBoardDispatcher._attempt_parent_handoffs(attempt)
    if handoffs:
        inputs["parent_handoff_context"] = handoffs
        inputs["parent_handoff_digest"] = attempt.parent_handoff_digest
    return _digest(inputs)


async def invocation(db, principal, job_id, fencing_token):
    from src.db.models import WorkBoardAttempt, WorkBoardTask, WorkflowRunState
    from src.work_board.contracts import WorkBoardOwner
    from src.work_board.dispatcher import _parse_typed_input
    attempt = (await db.scalars(select(WorkBoardAttempt).where(WorkBoardAttempt.workflow_run_id == job_id))).one_or_none()
    run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id).execution_options(populate_existing=True))
    if (attempt is None or attempt.ended_at is not None or attempt.cancel_requested_at is not None
        or run is None or run.status != "running" or run.fencing_token != fencing_token):
        raise BoardError("document_preparation_job_changed", "The original live task and fence are required", status_code=409)
    task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == attempt.task_id).execution_options(populate_existing=True))
    if task is None or task.owner_principal_id != principal.principal_id or task.owner_session_id != principal.session_id:
        raise BoardError("document_preparation_owner_changed", "The original task owner is required", status_code=409)
    owner = WorkBoardOwner(principal_id=principal.principal_id, session_id=principal.session_id)
    envelope = GeneralTaskEnvelope.model_validate(_parse_typed_input(task))
    check_envelope(envelope)
    if (run.job_kind != "agent.task.v1" or run.owner_principal_id != principal.principal_id
        or run.operator_session_id != principal.session_id or run.goal_id != task.goal_id
        or run.goal_revision != task.goal_revision or run.input_digest != original_job_digest(task, attempt, envelope)
        or run.idempotency_scope != "work-board-attempt" or run.idempotency_key != f"{task.task_id}:{attempt.attempt_id}"):
        raise BoardError("document_preparation_job_changed", "The original accepted job input is required", status_code=409)
    if envelope.task_input.document_source is None:
        raise BoardError("document_local_consent_required", "Explicit source use is required", status_code=409)
    from datetime import datetime, timezone
    if (run.deadline_at is None or sources.utc(run.deadline_at) <= datetime.now(timezone.utc)
        or run.lease_expires_at is None or sources.utc(run.lease_expires_at) <= datetime.now(timezone.utc)):
        raise BoardError("document_preparation_deadline_expired", "The original execution window expired", status_code=409)
    await resolve(db, owner, envelope.task_input.document_source, goal_id=task.goal_id)
    return owner, envelope


async def propose(db, owner, operator, service, request):
    from src.work_board.contracts import GeneralTaskCreate, GeneralTaskInput, TaskLimits, PlanSpec, PlanStep
    row, value = await sources.owned(db, owner, request.artifact_ref.split(":", 1)[1],
        revision=request.expected_source_revision, capability=CAPABILITY)
    binding = DocumentTaskBinding(artifact_ref=request.artifact_ref, source_revision=row.revision,
        metadata_digest=row.metadata_digest, citation_refs=request.citation_refs,
        selection_digest=digest(request.citation_refs), acknowledge_local_use=True)
    await resolve(db, owner, binding, goal_id=row.goal_id, operator=operator)
    descriptors, tool_digest = service.snapshot()
    contract = next((item for item in descriptors if item.tool_id == "document_prepare"), None)
    if contract is None:
        raise BoardError("document_preparation_unavailable", "Restore the local document adapter", status_code=503)
    return await service.create(db, owner, GeneralTaskCreate(goal_revision=row.goal_revision,
        idempotency_key=request.idempotency_key, expected_plan_revision=1,
        input=GeneralTaskInput(goal_ref=row.goal_id, intent="Prepare selected document citations locally",
            requested_output=contract.output_schema, document_source=binding, tool_set_digest=tool_digest,
            limits=TaskLimits(max_steps=1, max_inference_calls=0, max_cost_microusd=0, max_outstanding_children=0, wall_seconds=60)),
        plan=PlanSpec(revision=1, steps=[PlanStep(step_id="prepare", tool_id="document_prepare",
            input={"selection_digest": binding.selection_digest}, output_contract=contract.output_schema)])))


async def invoke(principal, job_id, fencing_token, inputs):
    from src.db.engine import get_session
    async with get_session() as db:
        _owner, envelope = await invocation(db, principal, job_id, fencing_token)
        binding = envelope.task_input.document_source
        if inputs != {"selection_digest": binding.selection_digest}:
            raise BoardError("document_selection_changed", "The accepted selection changed", status_code=409)
        return {"source_binding": binding.model_dump(mode="json"), "no_learning": True, "provider_contacts": 0}


def descriptor():
    from src.work_board.contracts import ToolDescriptor
    from src.native_tools.registry import get_tool_metadata
    from src.tools.policy import get_task_policy_snapshot
    hash_schema = {"type": "string", "pattern": "^[a-f0-9]{64}$"}
    properties = {"artifact_ref": {"type": "string", "maxLength": 64},
        "source_revision": {"type": "integer", "minimum": 1}, "metadata_digest": hash_schema,
        "citation_refs": {"type": "array", "minItems": 1, "maxItems": 16, "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 512}},
        "selection_digest": hash_schema, "acknowledge_local_use": {"const": True}}
    binding_schema = {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}
    output = {"type": "object", "properties": {"source_binding": binding_schema,
        "no_learning": {"const": True}, "provider_contacts": {"const": 0}},
        "required": ["source_binding", "no_learning", "provider_contacts"], "additionalProperties": False}
    return ToolDescriptor(tool_id="document_prepare", version="1",
        input_schema={"type": "object", "properties": {"selection_digest": {"type": "string", "pattern": "^[a-f0-9]{64}$"}},
            "required": ["selection_digest"], "additionalProperties": False},
        output_schema=output, effects=["owner_private_read", "local_compute"], permissions=["capability_execute", "document_local_use"],
        deadline=30, verifier="document_selected_private_readback.v1", policy_digest=digest({"version": 1, "max_refs": 16,
            "view_bytes": 16384, "local_only": True, "metadata": get_tool_metadata("document_prepare"), "policy": get_task_policy_snapshot()}))


async def private_view(db, owner, operator, service, jobs, task_id):
    from src.db.models import WorkBoardAttempt
    from src.work_board.dispatcher import _parse_typed_input
    task = await service.repository.get_task(db, owner, task_id)
    if task.capability_id != "agent.task.v1":
        raise BoardError("document_preparation_unavailable", "This task is not a local preparation", status_code=404)
    envelope = GeneralTaskEnvelope.model_validate(_parse_typed_input(task))
    check_envelope(envelope)
    binding = envelope.task_input.document_source
    if binding is None:
        raise BoardError("document_preparation_unavailable", "This task has no document selection", status_code=404)
    # Report stale source/Goal/Root precisely even for an incomplete task,
    # without decrypting evidence before successful native readback proof.
    await resolve(db, owner, binding, goal_id=task.goal_id, operator=operator, metadata_only=True)
    attempt = (await db.scalars(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == task_id)
        .order_by(WorkBoardAttempt.created_at.desc()))).first()
    projection = await jobs.get_job(attempt.workflow_run_id) if attempt and attempt.workflow_run_id else None
    if projection is None or projection.get("status") != "succeeded":
        raise BoardError("document_preparation_not_completed", "Review and accept the local plan in Work, then refresh its completed preparation", status_code=409)
    if (projection.get("input_digest") != original_job_digest(task, attempt, envelope)
        or projection.get("goal_id") != task.goal_id or projection.get("goal_revision") != task.goal_revision
        or projection.get("owner", {}).get("principal_id") != owner.principal_id
        or projection.get("operator_session_id") != owner.session_id
        or projection.get("idempotency", {}).get("scope") != "work-board-attempt"
        or projection.get("idempotency", {}).get("key") != f"{task_id}:{attempt.attempt_id}"):
        raise BoardError("document_preparation_readback_changed", "The original accepted task readback is required", status_code=409)
    outputs, artifacts = service.recovered_outputs(projection, envelope)
    output = outputs.get(envelope.plan.steps[0].step_id)
    artifact = artifacts.get(envelope.plan.steps[0].step_id)
    if output != {"source_binding": binding.model_dump(mode="json"), "no_learning": True, "provider_contacts": 0} or artifact is None:
        raise BoardError("document_preparation_readback_changed", "The exact verified local output is required", status_code=409)
    if not any(effect.get("receipt_kind") == "readback" and effect.get("status") == "succeeded"
        and effect.get("effect_type") == "general_tool_call" and effect.get("content_sha256") == artifact["content_sha256"]
        for effect in projection.get("effects", [])):
        raise BoardError("document_preparation_readback_changed", "Native successful readback is required", status_code=409)
    _row, sections = await resolve(db, owner, binding, goal_id=task.goal_id, operator=operator)
    return {"task_id": task_id, "status": "succeeded", "sections": sections, "no_learning": True, "provider_contacts": 0}
