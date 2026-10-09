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


async def collect_handoff_publication_rows(db, owner, envelope):
    """Metadata-only private envelope gate on the existing publication writer."""
    if envelope.specialist_handoff is None:
        return
    from sqlalchemy import select
    from src.db.models import WorkflowRunState
    from src.workflows.specialist_delegation import read_reservation, current_delegation
    from src.workflows.inference_group_lookup import group_reservation_rows
    from src.workflows.general_task_accounting import entry_for
    from src.workflows.inference_accounting import InferenceAccountingError
    from src.work_board.general_task import digest
    group = envelope.proposal_group
    if group is None:
        raise BoardError("specialist_handoff_denied", "Original specialist group required", status_code=409)
    try:
        rows = await group_reservation_rows(db, owner_id=owner.principal_id,
            group_id=group.group_id, group_digest=digest(group.model_dump(mode="json")),
            original_root_id=owner.session_id, original_deadline_at=group.original_deadline_at,
            group=group)
    except InferenceAccountingError as exc:
        raise BoardError("specialist_handoff_denied", "Original specialist group changed", status_code=409) from exc
    invocation_ids = set()
    for row in rows:
        entry = entry_for(row)
        if entry["role"] == "specialist":
            invocation_id = entry.get("delegation_invocation_id")
            if not invocation_id:
                raise BoardError("specialist_handoff_denied", "Original specialist invocation required", status_code=409)
            invocation_ids.add(invocation_id)
    candidates = []
    for invocation_id in sorted(invocation_ids):
        run = (await db.execute(select(WorkflowRunState).where(
            WorkflowRunState.run_identity == invocation_id).limit(1))).scalar_one_or_none()
        if (run is None or run.owner_principal_id != owner.principal_id
            or run.operator_session_id != owner.session_id
            or run.job_kind != "general_task_native_tool_v1"):
            raise BoardError("specialist_handoff_denied", "Original specialist invocation changed", status_code=409)
        candidates.append(run)
    matched = [run for run in candidates if (reservation := read_reservation(run)) is not None
        and reservation.handoff_ref == envelope.specialist_handoff]
    if len(matched) != 1:
        raise BoardError("specialist_handoff_denied", "Unique original copied handoff required", status_code=409)
    from src.workflows.job_runtime import _canonical
    return {"callback": matched[0], "rows": tuple((type(row), tuple(getattr(row, column.name) for column in row.__table__.primary_key),
        _canonical(row.model_dump(mode="json"))) for row in [*rows, *candidates])}


async def validate_handoff_publication(db, owner, envelope, *, _repository_stop_context=None):
    selection = await collect_handoff_publication_rows(db, owner, envelope)
    if selection is None:
        return
    from src.workflows.specialist_delegation import current_delegation
    context = await current_delegation(db, selection["callback"].run_identity,
        _repository_stop_context=_repository_stop_context)
    if (envelope.proposal_group != context.envelope.proposal_group
        or envelope.proposal_provenance != context.envelope.proposal_provenance
        or envelope.task_input.intent != context.request.instruction
        or envelope.task_input.evidence_refs != context.request.evidence_refs
        or envelope.task_input.limits != context.envelope.task_input.limits
        or envelope.plan is None or len(envelope.plan.steps) > context.request.limits.max_steps
        or any(step.tool_id not in context.request.allowed_tool_ids for step in envelope.plan.steps)):
        raise BoardError("specialist_handoff_denied", "Original narrowed specialist envelope required", status_code=409)
    return context


async def validate_handoff_publication_staged(db, owner, envelope, physical):
    """Original Stop initializer: complete read-only semantics, no issued proof."""
    selection = await collect_handoff_publication_rows(db, owner, envelope)
    if selection is None:
        if physical is not None:
            raise BoardError("specialist_handoff_changed", "Original optional handoff changed", status_code=409)
        return
    if physical is None:
        raise BoardError("specialist_handoff_changed", "Original optional staged handoff required", status_code=409)
    from src.workflows.specialist_delegation import _current_delegation_data
    context = await _current_delegation_data(db, selection["callback"].run_identity,
        _specialist_physical=physical)
    if (envelope.proposal_group != context.envelope.proposal_group
        or envelope.proposal_provenance != context.envelope.proposal_provenance
        or envelope.task_input.intent != context.request.instruction
        or envelope.task_input.evidence_refs != context.request.evidence_refs
        or envelope.task_input.limits != context.envelope.task_input.limits
        or envelope.plan is None or len(envelope.plan.steps) > context.request.limits.max_steps
        or any(step.tool_id not in context.request.allowed_tool_ids for step in envelope.plan.steps)):
        raise BoardError("specialist_handoff_denied", "Original narrowed specialist envelope required", status_code=409)
    return None


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
