"""Observe genuine digest calls; never manufacture Source or future bodies."""
import asyncio
from copy import copy

import pytest

from src.memory import composition_headers
from src.memory.header_bounds import HeaderBoundsError
from src.runtime_plugins import memory_producer
from tests.test_native_memory_producer import actual_memory


@pytest.mark.parametrize("construction", [True, False])
def test_stage_schedule_unissued_denies_before_db(construction):
    class Poison:
        @property
        def db(self):
            raise AssertionError("unissued stage schedule reached DB")

    with pytest.raises(HeaderBoundsError):
        memory_producer.original_native_memory_report_stage_revalidation_numeric_schedule(
            Poison(), None, certificate=None, facts=None, retention_widths=None,
            _construction_only=construction)


def test_stage_schedule_requires_literal_bool():
    with pytest.raises(HeaderBoundsError, match="memory_report_stage_constructor_mode_changed"):
        memory_producer.original_native_memory_report_stage_revalidation_numeric_schedule(
            None, None, certificate=None, facts=None, retention_widths=None,
            _construction_only=1)


@pytest.mark.asyncio
async def test_genuine_stage_schedule_covers_both_original_digest_occurrences(actual_memory, monkeypatch):
    client, _operator, _jobs, _host, _calls, task, attempt, _output = actual_memory
    from src.workspace import accounting_witness

    source_calls = []
    stage_calls = []
    original_pay = memory_producer._memory_report_stage_pay

    def instrument(original, observed):
        def call(*args, **kwargs):
            payments = []

            def pay(budget, stage, upper):
                payments.append((("native-memory-report-stage", stage), upper))
                return original_pay(budget, stage, upper)

            monkeypatch.setattr(memory_producer, "_memory_report_stage_pay", pay)
            try:
                return original(*args, **kwargs)
            finally:
                monkeypatch.setattr(memory_producer, "_memory_report_stage_pay", original_pay)
                observed.append(tuple(payments))
        return call

    monkeypatch.setattr(memory_producer, "_memory_report_source_digest",
        instrument(memory_producer._memory_report_source_digest, source_calls))
    monkeypatch.setattr(memory_producer, "_checked_memory_report_input_stage",
        instrument(memory_producer._checked_memory_report_input_stage, stage_calls))
    seen = []

    async def inspect(db, run, operation, prepared, *, existing_references, before_row, certificate):
        guard = db.info["composition_guard"]
        kwargs = dict(certificate=certificate, facts=guard._native_memory_source_capacity_facts[3],
            retention_widths=guard._native_memory_retention_numeric_facts[3])
        helper = memory_producer.original_native_memory_report_stage_revalidation_numeric_schedule
        frame = certificate.budget
        before = frame.remaining
        with pytest.raises(HeaderBoundsError):
            helper(copy(operation), prepared, **kwargs, _construction_only=True)
        for replacement in ({"facts": dict(kwargs["facts"])},
                {"retention_widths": dict(kwargs["retention_widths"])}):
            with pytest.raises(HeaderBoundsError):
                helper(operation, prepared, **{**kwargs, **replacement}, _construction_only=True)

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
        frame.debit(capacity, appearance=("test-stage-schedule", "constructor"))
        source_count, stage_count = len(source_calls), len(stage_calls)
        vector = helper(operation, prepared, **kwargs)
        assert (len(source_calls), len(stage_calls)) == (source_count, stage_count)
        assert len(vector) == 17 and source_calls and stage_calls
        actual = source_calls[-1] + stage_calls[-1]
        assert len(source_calls[-1]) == 11 and len(stage_calls[-1]) == 6
        for (label, upper), (actual_label, actual_upper) in zip(vector, actual, strict=True):
            assert label == actual_label and upper >= actual_upper
        assert sum(label[1] == "input-hash-return" for label, _ in vector) == 2
        assert sum(label[1] == "source-digest-fields" for label, _ in vector) == 1
        labels = sum(composition_headers._common_trace_label_width(label) for label, _ in vector)
        pairs = 17 * 23 + labels
        assert capacity == labels + 2 * pairs + 18
        seen.append(vector)
        raise HeaderBoundsError("test_stage_revalidation_schedule_observed")

    monkeypatch.setattr(accounting_witness, "_native_memory_effect_numeric_schedule", inspect)
    response = await client.post("/api/memory/task-proposals/native", json={
        "task_id": task.task_id, "expected_task_revision": task.task_revision,
        "attempt_id": attempt.attempt_id, "idempotency_key": "stage-revalidation-schedule-observer"})
    assert response.status_code == 409 and len(seen) == 1
