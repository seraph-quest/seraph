"""Explicit original completion certs and closed trace-negative custody."""
from collections import Counter
from types import MappingProxyType
import pytest
from src.memory import header_bounds
from src.memory.header_bounds import HeaderBoundsError, WRS_BY_RUN, GOAL
from src.runtime_plugins import memory_producer
from src.workflows.job_runtime import (
    _native_memory_completion_certificate_labels,
    original_native_memory_completion_certificate_numeric_schedule,
)
from tests.test_native_memory_producer import actual_memory


@pytest.mark.parametrize("goal_id,count",[(None,21),("original-goal",36)])
def test_completion_cert_roster_preserves_unique_locator_and_optional_goal(goal_id,count):
    labels=tuple(_native_memory_completion_certificate_labels("original-job",goal_id))
    assert len(labels)==count
    counts=Counter(labels)
    for owner in ("locator-indexes","locator-parts"):
        for stage in ("delivery","row-copy","encoded-copy"):
            assert counts[("metadata-prequery",owner,stage)]==1
    assert counts[("exact-header",WRS_BY_RUN.table,WRS_BY_RUN.key,("original-job",))]==1
    if goal_id is None:
        assert not any(GOAL.table in label for label in labels)
    else:
        assert counts[("exact-header",GOAL.table,GOAL.key,(goal_id,))]==1


@pytest.mark.asyncio
async def test_unregistered_completion_cert_owner_stops_before_private_source():
    class Unissued:
        @property
        def source(self):
            raise AssertionError("foreign Source read")
    with pytest.raises(HeaderBoundsError,match="memory_dispatch_validation_numeric_owner_changed"):
        await original_native_memory_completion_certificate_numeric_schedule(Unissued(),object(),
            certificate=object(),facts=MappingProxyType({}),retention_widths=MappingProxyType({}))


@pytest.mark.asyncio
async def test_genuine_completion_cert_trace_projects_original_future_header_and_closed_dispatch_roster(actual_memory,monkeypatch):
    client,_operator,_jobs,_host,_calls,task,attempt,_output=actual_memory
    factory=header_bounds._reserve_native_memory_effect_ledger
    validator=memory_producer._validate_memory_dispatch_source
    observations=[]
    async def observe(db,run,op,prepared,*,existing_references,before_row,certificate):
        guard=db.info["composition_guard"]
        facts=guard._native_memory_source_capacity_facts[3]
        widths=guard._native_memory_retention_numeric_facts[3]
        entries=await original_native_memory_completion_certificate_numeric_schedule(op,prepared,
            certificate=certificate,facts=facts,retention_widths=widths)
        assert len(entries)==21+(15 if run.goal_id is not None else 0)
        assert dict(entries)[("exact-header",WRS_BY_RUN.table,WRS_BY_RUN.key,(run.run_identity,))]>=widths["own_upper"]
        assert guard._native_memory_completion_certificate_numeric[-1] is entries
        # The real original validator still runs; an added unexpected debit is
        # rejected rather than accepted as a new appearance or source grant.
        async def unexpected(db,run,scope):
            source=await validator(db,run,scope)
            source.admission.header_budget.debit(1,appearance=("unexpected-original-reader",))
            return source
        monkeypatch.setattr(memory_producer,"_validate_memory_dispatch_source",unexpected)
        try:
            with pytest.raises(HeaderBoundsError,match="memory_dispatch_read_numeric_(roster|owner)_changed"):
                await memory_producer.original_native_memory_dispatch_validation_read_numeric_schedule(
                    op,prepared,certificate=certificate,facts=facts,retention_widths=widths)
        finally:
            monkeypatch.setattr(memory_producer,"_validate_memory_dispatch_source",validator)
        assert getattr(guard,"_native_memory_dispatch_read_numeric",None) is None
        assert op._effect is None
        observations.append(1)
        return await factory(db,run,op,prepared,existing_references=existing_references,
            before_row=before_row,certificate=certificate)
    monkeypatch.setattr(header_bounds,"_reserve_native_memory_effect_ledger",observe)
    response=await client.post("/api/memory/task-proposals/native",json={
        "task_id":task.task_id,"expected_task_revision":task.task_revision,
        "attempt_id":attempt.attempt_id,"idempotency_key":"completion-explicit-certificate-trace"})
    assert response.status_code in (200,409)
    assert observations==[1]
