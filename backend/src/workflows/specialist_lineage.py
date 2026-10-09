"""Display-only reciprocal events from the actual original child publication."""
from __future__ import annotations
import json
from uuid import UUID, uuid5
from pydantic import Field, model_validator
from sqlalchemy import select
from src.db.models import WorkBoardEvent, WorkBoardInputArtifact
from src.work_board.contracts import ClosedTaskModel, TaskIdentity, TaskDigest
from src.work_board.repository import BoardError

_NAMESPACE = UUID("aa3e0a88-c899-5074-831b-58ae04b20bce")
KINDS = {"task.specialist_published":"parent", "task.specialist_origin":"child"}


class SpecialistLineage(ClosedTaskModel):
    parent_task_id: TaskIdentity
    parent_attempt_id: TaskIdentity
    step_id: TaskIdentity
    child_task_id: TaskIdentity
    child_attempt_id: TaskIdentity
    child_job_id: str = Field(pattern=r"^work-board:[A-Za-z0-9_.:-]{1,128}$",max_length=128)
    delegation_invocation_id: str = Field(pattern=r"^general-tool:[0-9a-f]{48}$",max_length=128)
    reservation_digest: TaskDigest

    @model_validator(mode="after")
    def exact_reserved_ids(self):
        if (self.parent_task_id == self.child_task_id or self.parent_attempt_id == self.child_attempt_id
            or self.child_job_id != "work-board:" + self.child_task_id + ":" + self.child_attempt_id):
            raise ValueError("exact distinct specialist publication identities required")
        return self


def lineage_event_key(kind, lineage):
    from src.work_board.general_task import digest
    return str(uuid5(_NAMESPACE,digest([kind,lineage.model_dump(mode="json")])))


def safe_lineage_event(event, metadata):
    """Serializer validation only; this never grants task or execution access."""
    from src.work_board.general_task import digest
    try:
        lineage = SpecialistLineage.model_validate(metadata)
        leg = KINDS[event.kind]
        if (event.task_id != (lineage.parent_task_id if leg == "parent" else lineage.child_task_id)
            or event.mutation_request_digest != digest(lineage.model_dump(mode="json"))
            or event.mutation_idempotency_key != lineage_event_key(event.kind,lineage)):
            return {}
        return lineage.model_dump(mode="json")
    except (ValueError,TypeError,KeyError):
        return {}


async def publish_lineage_events(db, owner, task, publication, *, repository):
    """Same Board writer, actual creation seal first; no dependency edges."""
    from src.workflows.specialist_delegation import current_delegation, verify_publication_source
    from src.workflows.specialist_lifecycle import read_fact,CREATION_KEY,SpecialistChildCreationV1,_task_binding
    from src.work_board.general_task import digest
    from src.work_board.input_artifacts import _metadata_digest
    verify_publication_source(publication)
    context = await current_delegation(db,publication.invocation_id)
    creation = read_fact(context.callback,CREATION_KEY,SpecialistChildCreationV1)
    artifact = await db.get(WorkBoardInputArtifact,task.input_artifact_id,populate_existing=True)
    if (creation is None or creation.reservation_digest != publication.reservation_digest
        or creation.child_task_id != task.task_id or creation.child_task_binding_digest != _task_binding(task)
        or creation.child_input_digest != publication.envelope_digest
        or artifact is None or artifact.bound_task_id != task.task_id
        or artifact.metadata_digest != _metadata_digest(artifact)
        or (task.owner_principal_id,task.owner_session_id) != (owner.principal_id,owner.session_id)
        or task.task_id != context.reservation.child_task_id):
        raise BoardError("specialist_lineage_source_changed","Actual original specialist publication required",status_code=409)
    lineage = SpecialistLineage(parent_task_id=context.task.task_id,parent_attempt_id=context.attempt.attempt_id,
        step_id=context.request.step_id,child_task_id=task.task_id,
        child_attempt_id=context.reservation.child_attempt_id,child_job_id=context.reservation.child_job_id,
        delegation_invocation_id=context.callback.run_identity,reservation_digest=creation.reservation_digest)
    payload = lineage.model_dump(mode="json")
    request_digest = digest(payload)
    for kind,leg in KINDS.items():
        event_task = context.task if leg == "parent" else task
        key = lineage_event_key(kind,lineage)
        existing = await db.scalar(select(WorkBoardEvent).where(
            WorkBoardEvent.owner_principal_id == owner.principal_id,
            WorkBoardEvent.owner_session_id == owner.session_id,
            WorkBoardEvent.mutation_idempotency_key == key))
        if existing is not None:
            if (existing.task_id != event_task.task_id or existing.kind != kind
                or existing.mutation_request_digest != request_digest
                or json.loads(existing.metadata_json or "{}") != payload):
                raise BoardError("specialist_lineage_collision","Original specialist display receipt changed",status_code=409)
            continue
        event = await repository._event(db,event_task,owner,kind=kind,metadata=payload)
        event.mutation_idempotency_key = key
        event.mutation_request_digest = request_digest
        db.add(event)
        await db.flush()
