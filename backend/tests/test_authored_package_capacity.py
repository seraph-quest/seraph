"""Real file-SQLite claim races; rows here do not claim parser execution."""
import asyncio
from datetime import datetime,timezone,timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select,text

from tests.test_inference_accounting import accounting_db
from src.db.models import WorkflowRunState
from src.execution.tool_package_profile import canonical
from src.work_board.repository import BoardError
from src.work_board.tool_package_native import claim_authored_capacity,job_id
from src.workflows.job_runtime import _digest


def queued(name,package="local.capacity-test",goal="goal-one",priority=50):
    cap=f"pack.{package}.summarize.v1"
    authority={"principal":"capacity-owner","session_id":"capacity-root","goal_id":goal,"goal_revision":1,
        "capability_id":cap,"board_task_id":name,"board_attempt_id":name+"-attempt",
        "pack":{"pack_id":package,"owner_principal_id":"capacity-owner","session_id":"capacity-root","goal_id":goal,"goal_revision":1,
            "version":"1.0.0","digest":"a"*64,"authority_digest":"b"*64,"dependencies_digest":"c"*64,"review_id":"review-test"}}
    identity=job_id(SimpleNamespace(task_id=name,capability_id=cap),SimpleNamespace(attempt_id=name+"-attempt"))
    return WorkflowRunState(run_identity=identity,root_run_identity=identity,workflow_name="capacity-test-only",
        job_kind="local_authored_json",owner_kind="user",owner_principal_id="capacity-owner",session_id="capacity-root",goal_id=goal,goal_revision=1,
        status="queued",priority=priority,declared_authority_json=canonical(authority).decode(),authority_digest=_digest(authority))


@pytest.mark.asyncio
async def test_two_goal_claim_race_and_distinct_package(accounting_db):
    _,_,factory=accounting_db
    first=queued("first",goal="goal-one");second=queued("second",goal="goal-two")
    other=queued("other",package="local.independent-test",goal="goal-three")
    async with factory.accounting_sessions() as db:
        db.add_all([first,second,other])
    start=asyncio.Event()
    async def claim(identity):
        await start.wait()
        try:
            async with factory.accounting_sessions() as db:
                await db.execute(text("BEGIN IMMEDIATE"))
                row=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==identity))
                await claim_authored_capacity(db,row)
                row.status="running"
            return "claimed"
        except BoardError as exc:return exc.code
    tasks=[asyncio.create_task(claim(item.run_identity)) for item in (first,second)]
    start.set();outcomes=await asyncio.gather(*tasks)
    assert outcomes.count("claimed")==1
    assert set(outcomes)<= {"claimed","authored_package_capacity_held","authored_package_higher_priority_ready"}
    assert await claim(other.run_identity)=="claimed"
    async with factory.accounting_sessions() as db:
        rows=list((await db.scalars(select(WorkflowRunState))).all())
        assert sum(row.status=="running" for row in rows)==2
        # Age/deadline/terminal labels cannot discharge the claim witness.
        owned=next(row for row in rows if row.status=="running" and row.run_identity!=other.run_identity)
        owned.status="cancelled";owned.lease_expires_at=datetime(2000,1,1,tzinfo=timezone.utc)
    waiting=first if outcomes[0]!="claimed" else second
    assert await claim(waiting.run_identity)=="authored_package_capacity_held"


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate_priority,peer_priority,blocked",[(80,100,True),(80,60,False),(60,80,True)])
async def test_higher_numeric_priority_owns_next_claim(accounting_db,candidate_priority,peer_priority,blocked):
    _,_,factory=accounting_db
    candidate=queued("candidate",priority=candidate_priority)
    peer=queued("peer",priority=peer_priority)
    candidate.started_at=peer.started_at=datetime(2026,10,4,tzinfo=timezone.utc)
    async with factory.accounting_sessions() as db:db.add_all([candidate,peer])
    async with factory.accounting_sessions() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        row=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==candidate.run_identity))
        if blocked:
            with pytest.raises(BoardError) as denied:await claim_authored_capacity(db,row)
            assert denied.value.code=="authored_package_higher_priority_ready"
            assert row.checkpoint_receipts_json=="[]"
        else:
            await claim_authored_capacity(db,row)
            assert '"checkpoint_id":"authored-package:capacity"' in row.checkpoint_receipts_json


@pytest.mark.asyncio
@pytest.mark.parametrize("tie",["earlier-start","equal-start-id"])
async def test_equal_priority_orders_earlier_start_then_identity(accounting_db,tie):
    _,_,factory=accounting_db
    first,last=sorted([queued("tie-one",priority=80),queued("tie-two",priority=80)],key=lambda row:row.run_identity)
    first.started_at=last.started_at=datetime(2026,10,4,tzinfo=timezone.utc)
    if tie=="earlier-start":
        # The later identity still wins when it was ready first.
        first.started_at+=timedelta(seconds=1)
        first,last=last,first
    async with factory.accounting_sessions() as db:db.add_all([first,last])
    async with factory.accounting_sessions() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        later=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==last.run_identity))
        with pytest.raises(BoardError) as denied:await claim_authored_capacity(db,later)
        assert denied.value.code=="authored_package_higher_priority_ready"
        assert later.checkpoint_receipts_json=="[]"
        earlier=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==first.run_identity))
        await claim_authored_capacity(db,earlier)
        assert '"checkpoint_id":"authored-package:capacity"' in earlier.checkpoint_receipts_json


@pytest.mark.asyncio
async def test_malformed_binding_fail_closed(accounting_db):
    _,_,factory=accounting_db
    low=queued("low",priority=80);high=queued("high",priority=100)
    high.started_at=low.started_at
    async with factory.accounting_sessions() as db:db.add_all([low,high])
    async with factory.accounting_sessions() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        row=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==low.run_identity))
        with pytest.raises(BoardError) as denied:await claim_authored_capacity(db,row)
        assert denied.value.code=="authored_package_higher_priority_ready"
    async with factory.accounting_sessions() as db:
        row=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==high.run_identity))
        row.declared_authority_json="{}"
    async with factory.accounting_sessions() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        row=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==low.run_identity))
        with pytest.raises(BoardError) as denied:await claim_authored_capacity(db,row)
        assert denied.value.code=="authored_package_capacity_binding_invalid"


@pytest.mark.asyncio
async def test_owner_history_query_is_bounded(accounting_db):
    _,_,factory=accounting_db
    rows=[queued(f"bounded-{index}") for index in range(4097)]
    async with factory.accounting_sessions() as db:db.add_all(rows)
    async with factory.accounting_sessions() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        row=await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity==rows[0].run_identity))
        with pytest.raises(BoardError) as denied:await claim_authored_capacity(db,row)
        assert denied.value.code=="authored_package_capacity_history_full"
