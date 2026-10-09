"""Immutable proposal identity on the existing task/accounting owners."""
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
import json

from src.work_board.contracts import TaskProposalGroupV1, TaskProposalProvenanceV1
from src.work_board.repository import BoardError

_PUBLICATION_SEAL = object()


@dataclass(frozen=True)
class ProposalPublicationWitness:
    owner_principal_id: str
    original_root_id: str
    goal_id: str
    goal_revision: int
    envelope_bytes: bytes
    seal: object


async def seal_proposal_publication(db, owner, envelope, *, goal_revision):
    from src.work_board.general_task import canonical
    witness = ProposalPublicationWitness(owner.principal_id, owner.session_id,
        envelope.task_input.goal_ref, goal_revision, canonical(envelope.model_dump(mode="json")), _PUBLICATION_SEAL)
    await recheck_proposal_publication(db, owner, witness)
    return witness


async def recheck_proposal_publication(db, owner, witness):
    from src.workflows.inference_group_lookup import group_reservation_rows
    from src.workflows.inference_accounting import InferenceAccountingError
    from src.work_board.contracts import GeneralTaskEnvelope
    from src.work_board.general_task import digest
    from src.workflows.general_task_accounting import validate_group_owner
    if (not isinstance(witness, ProposalPublicationWitness) or witness.seal is not _PUBLICATION_SEAL
        or witness.owner_principal_id != owner.principal_id or witness.original_root_id != owner.session_id):
        raise BoardError("general_task_publication_witness_invalid", "Use the current server proposal publication", status_code=409)
    envelope = GeneralTaskEnvelope.model_validate_json(witness.envelope_bytes)
    group = envelope.proposal_group
    if (group is None or group.owner_principal_id != owner.principal_id
        or group.owner_session_id != owner.session_id or group.goal_id != witness.goal_id
        or group.goal_revision != witness.goal_revision
        or group.limits_digest != digest(envelope.task_input.limits.model_dump(mode="json"))):
        raise BoardError("general_task_publication_binding_changed", "Original proposal binding changed", status_code=409)
    await validate_group_owner(db, group)
    from src.workflows.specialist_evidence import validate_handoff_publication
    await validate_handoff_publication(db, owner, envelope)
    try:
        operations = await group_reservation_rows(db, owner_id=owner.principal_id,
            group_id=group.group_id, group_digest=digest(group.model_dump(mode="json")),
            original_root_id=owner.session_id,
            original_deadline_at=group.original_deadline_at, group=group)
    except InferenceAccountingError as exc:
        raise BoardError("general_task_provenance_changed", "Original proposal accounting binding changed", status_code=409) from exc
    if envelope.proposal_provenance is not None:
        original = next((row for row in operations if row.operation_id == envelope.proposal_provenance.initial_operation_id), None)
        if original is None or proposal_provenance(original.model_dump(mode="json"), group) != envelope.proposal_provenance:
            raise BoardError("general_task_provenance_changed", "Original proposal accounting binding changed", status_code=409)
        if original.state in {"unknown", "contact_started"}:
            raise BoardError("general_task_group_unknown", "Reconcile original planning liability before publication", status_code=409)
    elif any(any(item.get("kind") == "general_task_group_reservation.v1" and item.get("role") == "initial_proposal"
        and item.get("group", {}).get("group_id") == group.group_id
        for item in json.loads(row.evidence_json)) for row in operations):
        raise BoardError("general_task_provenance_missing", "A charged proposal cannot become an uncharged manual plan", status_code=409)
    return envelope


def publication_scan_input(witness, raw):
    from src.work_board.general_task import canonical
    from src.work_board.contracts import GeneralTaskEnvelope
    if (not isinstance(witness, ProposalPublicationWitness) or witness.seal is not _PUBLICATION_SEAL
        or canonical(GeneralTaskEnvelope.model_validate(raw).model_dump(mode="json")) != witness.envelope_bytes):
        raise BoardError("general_task_publication_witness_invalid", "Exact server proposal publication required", status_code=409)
    return {key: value for key, value in raw.items() if key not in {"proposal_group", "proposal_provenance", "specialist_handoff"}}


def stored_scan_input(record, raw):
    """Only canonical owner-bound artifact/task readers use this projection."""
    from src.work_board.contracts import GeneralTaskEnvelope
    envelope = GeneralTaskEnvelope.model_validate(raw)
    group = envelope.proposal_group
    if group is not None and (group.owner_principal_id != record.owner_principal_id
        or group.owner_session_id != record.owner_session_id or group.goal_id != record.goal_id
        or group.goal_revision != record.goal_revision):
        raise BoardError("general_task_publication_binding_changed", "Canonical proposal owner binding changed", status_code=409)
    return {key: value for key, value in raw.items() if key not in {"proposal_group", "proposal_provenance", "specialist_handoff"}}


def group_identity(owner, goal_id, goal_revision, request_key):
    from src.work_board.general_task import digest
    return digest(["general-task-group.v1", owner.principal_id, owner.session_id,
        goal_id, goal_revision, request_key])


def new_group(owner, task_input, descriptors, *, goal_revision, request_key,
              expires_at, now=None):
    from src.work_board.general_task import digest
    issued = now or datetime.now(timezone.utc)
    deadline = min(issued + timedelta(seconds=task_input.limits.wall_seconds), expires_at)
    if deadline <= issued:
        raise BoardError("general_task_deadline", "Original task authority expired", status_code=409)
    return TaskProposalGroupV1(group_id=group_identity(owner, task_input.goal_ref,
        goal_revision, request_key), owner_principal_id=owner.principal_id,
        owner_session_id=owner.session_id, goal_id=task_input.goal_ref,
        goal_revision=goal_revision, creation_request_key=request_key,
        initial_input_digest=digest(task_input.model_dump(mode="json")),
        planning_snapshot_digest=digest([item.model_dump(mode="json") for item in descriptors]),
        intent_egress_ack_digest=digest([task_input.intent, task_input.inference_egress_acknowledged]),
        limits_digest=digest(task_input.limits.model_dump(mode="json")),
        max_inference_calls=task_input.limits.max_inference_calls,
        max_cost_microusd=task_input.limits.max_cost_microusd,
        max_steps=task_input.limits.max_steps, issued_at=issued, original_deadline_at=deadline)


def group_entry(operation):
    entries = [item for item in json.loads(operation["evidence_json"])
        if item.get("kind") == "general_task_group_reservation.v1"]
    if len(entries) != 1:
        raise BoardError("general_task_provenance_missing", "Inspect original inference accounting provenance", status_code=409)
    return entries[0]


def reservation_binding(operation):
    """Exclude mutable settlement/fence fields from immutable identity evidence."""
    from src.work_board.general_task import digest
    fields = ("operation_id", "job_id", "owner_id", "goal_id", "goal_revision",
        "payload_digest", "policy_digest", "runtime_path", "profile_id",
        "deployment_id", "settings_revision", "sequence", "bound_microusd", "deadline_at")
    return digest({**{key: operation[key] for key in fields}, "group_entry": group_entry(operation)})


def proposal_provenance(operation, group):
    from src.work_board.general_task import digest
    entry = group_entry(operation)
    if (entry.get("role") != "initial_proposal" or entry.get("group") != group.model_dump(mode="json")
        or entry.get("group_digest") != digest(group.model_dump(mode="json"))
        or operation["owner_id"] != group.owner_principal_id
        or operation["goal_id"] != group.goal_id or operation["goal_revision"] != group.goal_revision):
        raise BoardError("general_task_provenance_changed", "Original proposal accounting binding changed", status_code=409)
    return TaskProposalProvenanceV1(group_id=group.group_id,
        group_digest=entry["group_digest"], initial_operation_id=operation["operation_id"],
        initial_inference_job_id=operation["job_id"], initial_payload_digest=operation["payload_digest"],
        initial_policy_digest=operation["policy_digest"], deployment_id=operation["deployment_id"],
        settings_revision=operation["settings_revision"], reservation_sequence=operation["sequence"],
        reservation_binding_digest=reservation_binding(operation), original_deadline_at=group.original_deadline_at)
