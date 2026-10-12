"""Genuine Source observations; no synthetic successful operation or ledger."""
import asyncio
from copy import copy
import pytest
from src.memory import composition_headers
from src.memory.header_bounds import HeaderBoundsError
from src.runtime_plugins import memory_producer
from tests.test_native_memory_producer import actual_memory

@pytest.mark.parametrize("construction", [False, True])
def test_unissued_proof_schedule_denies_before_db(construction):
    class Poison:
        @property
        def db(self):
            raise AssertionError("unissued proof schedule accessed DB")
    with pytest.raises(HeaderBoundsError):
        memory_producer.original_native_memory_source_proof_numeric_schedule(
            Poison(),None,certificate=None,facts=None,retention_widths=None,
            _construction_only=construction)


def test_proof_schedule_mode_is_literal_bool():
    with pytest.raises(HeaderBoundsError,match="memory_source_proof_constructor_mode_changed"):
        memory_producer.original_native_memory_source_proof_numeric_schedule(
            None,None,certificate=None,facts=None,retention_widths=None,
            _construction_only=1)

@pytest.mark.asyncio
async def test_genuine_proof_schedule_retains_all_original_ordered_appearances(actual_memory, monkeypatch):
    client,_operator,_jobs,_host,_calls,task,attempt,_output=actual_memory
    from src.workspace import accounting_witness
    actual=[]
    original_pay=memory_producer._native_memory_source_proof_pay
    original_stage=memory_producer._memory_report_stage_pay
    def observe_proof(frame, original_task, original_attempt, readback):
        entries=[]
        def observe_stage(budget,stage,upper):
            if budget is frame:entries.append((("native-memory-report-stage",stage),upper))
            return original_stage(budget,stage,upper)
        monkeypatch.setattr(memory_producer,"_memory_report_stage_pay",observe_stage)
        try:return original_pay(frame,original_task,original_attempt,readback)
        finally:
            monkeypatch.setattr(memory_producer,"_memory_report_stage_pay",original_stage)
            actual.append(tuple(entries))
    monkeypatch.setattr(memory_producer,"_native_memory_source_proof_pay",observe_proof)
    seen=[]
    async def inspect(db,run,operation,prepared,*,existing_references,before_row,certificate):
        guard=db.info["composition_guard"]
        facts=guard._native_memory_source_capacity_facts[3]
        widths=guard._native_memory_retention_numeric_facts[3]
        kwargs=dict(certificate=certificate,facts=facts,retention_widths=widths)
        helper=memory_producer.original_native_memory_source_proof_numeric_schedule
        frame=certificate.budget;before=frame.remaining
        for override in ({"facts":dict(facts)},{"retention_widths":dict(widths)}):
            with pytest.raises(HeaderBoundsError):helper(operation,prepared,**{**kwargs,**override},_construction_only=True)
        with pytest.raises(HeaderBoundsError):helper(copy(operation),prepared,**kwargs,_construction_only=True)
        async def child():
            with pytest.raises(HeaderBoundsError):helper(operation,prepared,**kwargs,_construction_only=True)
        await asyncio.create_task(child())
        token=composition_headers._CURRENT.set(None)
        try:
            with pytest.raises(HeaderBoundsError):helper(operation,prepared,**kwargs,_construction_only=True)
        finally:composition_headers._CURRENT.reset(token)
        assert frame.remaining==before
        # Scalar must not construct even the nested child's forecast vector.
        child_helper=memory_producer.original_native_memory_source_readback_digest_numeric_schedule
        def scalar_child(*args,**kw):
            assert kw.get("_construction_only") is True
            return child_helper(*args,**kw)
        monkeypatch.setattr(memory_producer,"original_native_memory_source_readback_digest_numeric_schedule",scalar_child)
        try:capacity=helper(operation,prepared,**kwargs,_construction_only=True)
        finally:monkeypatch.setattr(memory_producer,"original_native_memory_source_readback_digest_numeric_schedule",child_helper)
        assert type(capacity) is int and capacity>0 and frame.remaining==before
        # Pay genuine original constructor first; exhaustion is a real failure.
        frame.debit(capacity,appearance=("test-source-proof","constructor"))
        vector=helper(operation,prepared,**kwargs)
        assert type(vector) is tuple and actual and vector==actual[-1]
        assert sum(label==("native-memory-report-stage","proof-map") for label,_ in vector)==3
        assert sum(label==("native-memory-report-stage","proof-numeric-scalar-tuple") for label,_ in vector)==15
        assert sum(label==("native-memory-report-stage","proof-readback-numeric-field-pair") for label,_ in vector)==11
        seen.append(vector)
        raise HeaderBoundsError("test_source_proof_schedule_observed")
    monkeypatch.setattr(accounting_witness,"_native_memory_effect_numeric_schedule",inspect)
    response=await client.post("/api/memory/task-proposals/native",json={
        "task_id":task.task_id,"expected_task_revision":task.task_revision,
        "attempt_id":attempt.attempt_id,"idempotency_key":"source-proof-schedule-observer"})
    assert response.status_code==409 and len(seen)==1
