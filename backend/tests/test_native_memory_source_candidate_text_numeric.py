"""Genuine candidate formatter observations; no constructed future Source."""
import asyncio
from copy import copy

import pytest

from src.memory import composition_headers
from src.memory.header_bounds import HeaderBoundsError
from src.runtime_plugins import memory_producer
from tests.test_native_memory_producer import actual_memory


@pytest.mark.parametrize("construction", [True, False])
def test_candidate_text_schedule_unissued_denies_before_db(construction):
    class Poison:
        @property
        def db(self):
            raise AssertionError("unissued candidate-text schedule reached DB")

    with pytest.raises(HeaderBoundsError):
        memory_producer.original_native_memory_source_candidate_text_numeric_schedule(
            Poison(), None, certificate=None, facts=None, retention_widths=None,
            _construction_only=construction)


def test_candidate_text_schedule_requires_literal_bool():
    with pytest.raises(HeaderBoundsError, match="memory_source_candidate_text_constructor_mode_changed"):
        memory_producer.original_native_memory_source_candidate_text_numeric_schedule(
            None, None, certificate=None, facts=None, retention_widths=None,
            _construction_only=1)


@pytest.mark.asyncio
async def test_genuine_candidate_text_schedule_covers_original_formatter(actual_memory, monkeypatch):
    client, _operator, _jobs, _host, _calls, task, attempt, _output = actual_memory
    from src.workspace import accounting_witness

    actual = []
    original_pay = memory_producer._native_memory_source_candidate_text_pay
    original_stage = memory_producer._memory_report_stage_pay

    def observe_candidate(frame, proof):
        payments = []

        def observe_stage(budget, stage, upper):
            if budget is frame:
                payments.append((("native-memory-report-stage", stage), upper))
            return original_stage(budget, stage, upper)

        monkeypatch.setattr(memory_producer, "_memory_report_stage_pay", observe_stage)
        try:
            return original_pay(frame, proof)
        finally:
            monkeypatch.setattr(memory_producer, "_memory_report_stage_pay", original_stage)
            actual.append(tuple(payments))

    monkeypatch.setattr(memory_producer, "_native_memory_source_candidate_text_pay", observe_candidate)
    observed = []

    async def inspect(db, run, operation, prepared, *, existing_references, before_row, certificate):
        guard = db.info["composition_guard"]
        facts = guard._native_memory_source_capacity_facts[3]
        widths = guard._native_memory_retention_numeric_facts[3]
        kwargs = dict(certificate=certificate, facts=facts, retention_widths=widths)
        helper = memory_producer.original_native_memory_source_candidate_text_numeric_schedule
        frame = certificate.budget
        before = frame.remaining
        for replacement in ({"facts": dict(facts)}, {"retention_widths": dict(widths)}):
            with pytest.raises(HeaderBoundsError):
                helper(operation, prepared, **{**kwargs, **replacement}, _construction_only=True)
        with pytest.raises(HeaderBoundsError):
            helper(copy(operation), prepared, **kwargs, _construction_only=True)

        async def child():
            with pytest.raises(HeaderBoundsError):
                helper(operation, prepared, **kwargs, _construction_only=True)

        await asyncio.create_task(child())
        token = composition_headers._CURRENT.set(None)
        try:
            with pytest.raises(HeaderBoundsError):
                helper(operation, prepared, **kwargs, _construction_only=True)
        finally:
            composition_headers._CURRENT.reset(token)
        assert frame.remaining == before
        capacity = helper(operation, prepared, **kwargs, _construction_only=True)
        assert type(capacity) is int and capacity > 0 and frame.remaining == before
        frame.debit(capacity, appearance=("test-source-candidate-text", "constructor"))
        vector = helper(operation, prepared, **kwargs)
        assert len(vector) == 9 and actual and len(actual[-1]) == 9
        for (label, upper), (original_label, original_upper) in zip(vector, actual[-1], strict=True):
            assert label == original_label and upper >= original_upper
        # This original ASCII fixture catches the invalid width/6 inverse:
        # UTF width is encoded-width minus quotes, not one sixth of that.
        proof = memory_producer._native_memory_source_proof_numeric_facts(operation, prepared, **kwargs)
        capability = proof[2]["capability_id"][0]
        goal = proof[2]["goal_id"][0]
        assert capability > 2 and goal > 2
        ref_utf8 = proof[3][0] - 2 if proof[3] else 64
        literal = len("Verified  completed successfully for goal  at revision ; independent readback  is bound to the source attempt.")
        wrong_format = 6 * (literal + (capability - 2) // 6 + (goal - 2) // 6 + 20 + ref_utf8) + 2
        assert actual[-1][3][1] > wrong_format
        label_values = sum(composition_headers._common_trace_label_width(label) for label, _ in vector)
        pair_values = 9 * 23 + label_values
        assert capacity == label_values + pair_values + 2 * (10 + pair_values)
        observed.append(vector)
        raise HeaderBoundsError("test_source_candidate_text_schedule_observed")

    monkeypatch.setattr(accounting_witness, "_native_memory_effect_numeric_schedule", inspect)
    response = await client.post("/api/memory/task-proposals/native", json={
        "task_id": task.task_id, "expected_task_revision": task.task_revision,
        "attempt_id": attempt.attempt_id, "idempotency_key": "candidate-text-schedule-observer"})
    assert response.status_code == 409 and len(observed) == 1
