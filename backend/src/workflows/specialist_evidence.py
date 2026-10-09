"""Resolve explicit specialist inputs from the original private copied handoff."""
from __future__ import annotations

import json
from pydantic import Field
from src.work_board.contracts import ClosedTaskModel
from src.work_board.repository import BoardError


class SpecialistEvidencePointer(ClosedTaskModel):
    from_evidence: str = Field(min_length=1, max_length=512)
    pointer: str = Field(default="", max_length=512, pattern=r"^(?:/(?:[^~/]|~[01])*)*$")


def has_evidence_pointer(value):
    if isinstance(value, dict):
        return "from_evidence" in value or any(has_evidence_pointer(item) for item in value.values())
    if isinstance(value, list):
        return any(has_evidence_pointer(item) for item in value)
    return False


def validate_evidence_pointers(value, references):
    if isinstance(value, dict):
        if "from_evidence" in value:
            pointer = SpecialistEvidencePointer.model_validate(value)
            if pointer.from_evidence not in references:
                raise ValueError("evidence pointer exceeds explicit specialist selection")
            return
        for item in value.values():
            validate_evidence_pointers(item, references)
    elif isinstance(value, list):
        for item in value:
            validate_evidence_pointers(item, references)


async def read_specialist_handoff(db, context):
    """Live execution read: current owner, original metadata, copied bytes only."""
    from src.workflows.specialist_delegation import current_delegation
    from src.workflows.delegation_contracts import _producer_tokens, _vault_state
    from src.work_board.contracts import WorkBoardOwner
    from src.work_board.general_task_runtime_artifacts import read_native_artifact_reference
    from src.work_board.general_task import digest
    current = await current_delegation(db, context.callback.run_identity)
    reference = current.reservation.handoff_ref
    copied = read_native_artifact_reference(reference,
        parent_job_id=current.parent.run_identity, creation_digest=current.manifest.creation_digest)
    owner = WorkBoardOwner(principal_id=current.task.owner_principal_id,
        session_id=current.task.owner_session_id)
    selected = [entry.reference for entry in copied.entries]
    original = {item["reference"]: item for item in current.envelope.evidence}
    if (copied.invocation_id != current.callback.run_identity
        or copied.request_digest != current.reservation.delegation_request_digest
        or copied.child_task_id != current.reservation.child_task_id
        or copied.owner_principal_id != owner.principal_id or copied.original_root_id != owner.session_id
        or copied.group_digest != digest(current.envelope.proposal_group.model_dump(mode="json"))
        or selected != current.request.evidence_refs
        or any(entry.model_dump(exclude={"content"}) != original.get(entry.reference) for entry in copied.entries)
        or tuple(copied.producer_tokens) != await _producer_tokens(db, owner, selected)
        or copied.vault_state_digest != await _vault_state(db)
        or not any(item.get("artifact_id") == reference.artifact_id
            and item.get("content_sha256") == reference.digest
            and item.get("artifact_type") == "specialist_evidence_handoff"
            for item in json.loads(current.callback.artifact_receipts_json or "[]"))):
        raise BoardError("specialist_handoff_changed", "Original copied evidence binding changed", status_code=409)
    return copied


async def validate_handoff_publication(db, owner, envelope):
    """Metadata-only private envelope gate on the existing publication writer."""
    if envelope.specialist_handoff is None:
        return
    from sqlalchemy import select
    from src.db.models import WorkflowRunState
    from src.workflows.specialist_delegation import read_reservation, current_delegation
    candidates = (await db.execute(select(WorkflowRunState).where(
        WorkflowRunState.owner_principal_id == owner.principal_id,
        WorkflowRunState.operator_session_id == owner.session_id,
        WorkflowRunState.job_kind == "general_task_native_tool_v1"))).scalars().all()
    matched = [run for run in candidates if (reservation := read_reservation(run)) is not None
        and reservation.handoff_ref == envelope.specialist_handoff]
    if len(matched) != 1:
        raise BoardError("specialist_handoff_denied", "Unique original copied handoff required", status_code=409)
    context = await current_delegation(db, matched[0].run_identity)
    if (envelope.proposal_group != context.envelope.proposal_group
        or envelope.proposal_provenance != context.envelope.proposal_provenance
        or envelope.task_input.intent != context.request.instruction
        or envelope.task_input.evidence_refs != context.request.evidence_refs
        or envelope.task_input.limits != context.envelope.task_input.limits
        or envelope.plan is None or len(envelope.plan.steps) > context.request.limits.max_steps
        or any(step.tool_id not in context.request.allowed_tool_ids for step in envelope.plan.steps)):
        raise BoardError("specialist_handoff_denied", "Original narrowed specialist envelope required", status_code=409)
    return context


async def resolve_specialist_evidence(db, task, envelope, value):
    if not has_evidence_pointer(value):
        return value
    from src.workflows.specialist_delegation import specialist_for_task
    context = await specialist_for_task(db, task)
    if context is None or envelope.specialist_handoff != context.reservation.handoff_ref:
        raise BoardError("specialist_handoff_denied", "Original specialist handoff required", status_code=409)
    validate_evidence_pointers(value, context.request.evidence_refs)
    copied = await read_specialist_handoff(db, context)
    contents = {entry.reference: json.loads(entry.content) for entry in copied.entries}
    def resolve(item):
        if isinstance(item, dict):
            if "from_evidence" in item:
                pointer = SpecialistEvidencePointer.model_validate(item)
                selected = contents[pointer.from_evidence]
                for segment in pointer.pointer[1:].split("/") if pointer.pointer else ():
                    segment = segment.replace("~1", "/").replace("~0", "~")
                    if isinstance(selected, list):
                        if not segment.isdigit() or (segment != "0" and segment.startswith("0")):
                            raise ValueError("invalid copied evidence array pointer")
                        selected = selected[int(segment)]
                    elif isinstance(selected, dict):
                        selected = selected[segment]
                    else:
                        raise ValueError("copied evidence pointer requires a JSON value")
                return json.loads(json.dumps(selected, ensure_ascii=False, allow_nan=False))
            return {key: resolve(child) for key, child in item.items()}
        if isinstance(item, list):
            return [resolve(child) for child in item]
        return item
    return resolve(value)
