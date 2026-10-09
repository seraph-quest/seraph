"""Actual canonical specialist association events; never dependency authority."""
from tests.general_task_method_lifecycle import native_admission_lifecycle
import json
import pytest
from sqlalchemy import select,delete,func
from src.db.models import WorkBoardTask,WorkBoardEvent,WorkBoardLink
from tests.test_general_task_persistence import task_runtime
from tests.test_work_board_m6_provider_free_journey import isolated_runtime
from tests.test_specialist_evidence_runtime import copied_evidence_fixture,reserve_evidence_specialist


@pytest.mark.asyncio
async def test_actual_lineage_pair_replay_repair_and_foreign_scope(task_runtime,monkeypatch, native_admission_lifecycle):
    from src.workflows.specialist_delegation import execute_specialist
    from src.workflows.specialist_lineage import KINDS,SpecialistLineage
    from src.work_board.events import _event_payload
    from src.work_board.dispatcher import _parse_typed_input
    from src.work_board.contracts import GeneralTaskEnvelope,GeneralTaskCreate,WorkBoardOwner
    from src.work_board.repository import BoardError
    fixture = await copied_evidence_fixture(task_runtime,monkeypatch)
    sessions,workspace,owner,dispatcher,service,envelope,original,reference,plan,planner,transport = fixture
    context,principal = await reserve_evidence_specialist(fixture)
    result = await execute_specialist(dispatcher.jobs,service=service,invocation_id=context.callback.run_identity,
        fencing_token=context.callback.fencing_token,principal=principal)
    async with sessions() as db:
        child = await service.repository.get_task(db,owner,result["child_id"])
        private = GeneralTaskEnvelope.model_validate(_parse_typed_input(child))
        rows = list((await db.execute(select(WorkBoardEvent).where(WorkBoardEvent.kind.in_(KINDS)))).scalars())
        assert len(rows) == 2
        by_kind = {row.kind:row for row in rows}
        parent = by_kind["task.specialist_published"]
        origin = by_kind["task.specialist_origin"]
        assert parent.task_id == context.task.task_id and origin.task_id == child.task_id
        parent_wire = _event_payload(parent)
        child_wire = _event_payload(origin)
        assert len(parent_wire["metadata"]) == 8
        assert parent_wire["metadata"] == child_wire["metadata"]
        association = SpecialistLineage.model_validate(parent_wire["metadata"])
        assert association.child_task_id == result["child_id"]
        assert association.child_attempt_id == context.reservation.child_attempt_id
        assert association.child_job_id == context.reservation.child_job_id
        assert await db.scalar(select(func.count()).select_from(WorkBoardLink)) == 0
        with pytest.raises(BoardError):
            await service.repository.get_detail(db,WorkBoardOwner(principal_id="foreign",session_id="foreign"),child.task_id)
        child_id = child.task_id
        await db.execute(delete(WorkBoardEvent).where(WorkBoardEvent.event_id == origin.event_id))
    request = GeneralTaskCreate(goal_revision=1,idempotency_key=context.reservation.child_publication_key,
        input=private.task_input,plan=private.plan,expected_plan_revision=private.plan.revision,accept=True)
    async with sessions() as db:
        replay = await service.create(db,owner,request,_specialist_context=context)
        assert replay.idempotent_replay and replay.task.task_id == child_id
    async with sessions() as db:
        replay = await service.create(db,owner,request,_specialist_context=context)
        assert replay.idempotent_replay
        rows = list((await db.execute(select(WorkBoardEvent).where(WorkBoardEvent.kind.in_(KINDS)))).scalars())
        assert len(rows) == 2
        assert all(len(_event_payload(row)["metadata"]) == 8 for row in rows)
    assert len(transport["contacts"]) == 1


def test_fake_or_expanded_lineage_rows_have_no_projected_navigation():
    from src.workflows.specialist_lineage import SpecialistLineage,lineage_event_key
    from src.work_board.general_task import digest
    from src.work_board.events import _event_payload
    data = dict(parent_task_id="parent",parent_attempt_id="parent-attempt",step_id="step",
        child_task_id="child",child_attempt_id="child-attempt",child_job_id="work-board:child:child-attempt",
        delegation_invocation_id="general-tool:"+"a"*48,reservation_digest="b"*64)
    lineage = SpecialistLineage.model_validate(data)
    event = WorkBoardEvent(task_id="parent",owner_principal_id="owner",owner_session_id="root",
        actor_principal_id="owner",kind="task.specialist_published",metadata_json=json.dumps(data),
        mutation_idempotency_key=lineage_event_key("task.specialist_published",lineage),mutation_request_digest=digest(data))
    assert len(_event_payload(event)["metadata"]) == 8
    event.metadata_json = json.dumps({**data,"child_task_id":"unrelated"})
    assert _event_payload(event)["metadata"] == {}
    event.metadata_json = json.dumps({**data,"instruction":"private"})
    assert _event_payload(event)["metadata"] == {}
    event.metadata_json = json.dumps(data)
    event.task_id = "unrelated"
    assert _event_payload(event)["metadata"] == {}
