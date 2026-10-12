"""ONE conflict forecast borrows genuine original REPORT numeric lineage."""
import asyncio

import pytest

from src.memory import composition_headers, header_bounds
from src.memory.header_bounds import HeaderBoundsError
from src.workflows import job_runtime
from tests.test_native_memory_producer import actual_memory


@pytest.mark.parametrize("construction", [False, True])
def test_unissued_conflict_forecast_denies_before_selected_rows(construction):
    class Poison:
        @property
        def db(self):
            raise AssertionError("unissued Source reached selected rows")
    with pytest.raises(HeaderBoundsError):
        job_runtime.original_native_memory_admission_conflict_numeric_schedule(
            Poison(), object(), certificate=object(), facts=object(), retention_widths=object(),
            _construction_only=construction)


@pytest.mark.asyncio
async def test_genuine_conflict_planning_uses_stage_selected_report_row_without_builder(actual_memory, monkeypatch):
    client, _operator, _jobs, _host, _calls, task, attempt, _output = actual_memory
    from src.workflows import inference_accounting
    helper = job_runtime.original_native_memory_admission_conflict_numeric_schedule
    reached = []
    original = inference_accounting._original_native_binding_numeric_entries

    def forbidden(*args, **kwargs):
        raise AssertionError("planning scalar allocated a binding forecast")

    async def observe(db, run, operation, prepared, *, existing_references, before_row, certificate):
        guard = db.info["composition_guard"]
        # REPORT rows are deliberately outside the generic Memory dependency
        # roster; the original REPORT8 owner supplies only numerical lineage.
        assert not any(descriptor.table in ("work_board_tasks", "work_board_attempts")
            for descriptor, key, raw, encoded in guard._native_memory_existing_source_rows[2])
        facts = guard._native_memory_source_capacity_facts[3]
        widths = guard._native_memory_retention_numeric_facts[3]
        kwargs = dict(certificate=certificate, facts=facts, retention_widths=widths,
            _construction_only=True)
        before = certificate.budget.remaining
        monkeypatch.setattr(inference_accounting, "_original_native_binding_numeric_entries", forbidden)
        with pytest.raises(HeaderBoundsError):
            helper(operation, prepared, **{**kwargs, "facts": dict(facts)})
        with pytest.raises(HeaderBoundsError):
            helper(operation, prepared, **{**kwargs, "retention_widths": dict(widths)})
        token = composition_headers._CURRENT.set(None)
        try:
            with pytest.raises(HeaderBoundsError):
                helper(operation, prepared, **kwargs)
        finally:
            composition_headers._CURRENT.reset(token)
        async def child():
            with pytest.raises(HeaderBoundsError):
                helper(operation, prepared, **kwargs)
        await asyncio.create_task(child())
        capacity = helper(operation, prepared, **kwargs)
        assert type(capacity) is int and capacity > 0
        assert certificate.budget.remaining == before
        monkeypatch.setattr(inference_accounting, "_original_native_binding_numeric_entries", original)
        certificate.budget.debit(capacity, appearance=("test-report-conflict", "planning"))
        entries = helper(operation, prepared, **{**kwargs, "_construction_only": False})
        # Exact occurrence accounting retains both payload traversals plus
        # the independent fingerprint traversal; it does not parse a binding
        # merely because its already-paid constructor supplies manifest terms.
        assert len(entries) == 135
        assert sum(label[1] == "binding-payload-map" for label, _ in entries) == 3
        assert sum(label[1] == "binding-wire-map" for label, _ in entries) == 1
        assert not any("binding-parser" in label[1] for label, _ in entries)
        reached.append(capacity)
        raise HeaderBoundsError("test_admission_conflict_forecast_observed")

    monkeypatch.setattr(header_bounds, "_reserve_native_memory_effect_ledger", observe)
    response = await client.post("/api/memory/task-proposals/native", json={
        "task_id": task.task_id, "expected_task_revision": task.task_revision,
        "attempt_id": attempt.attempt_id, "idempotency_key": "admission-conflict-forecast",
    })
    assert response.status_code == 409 and len(reached) == 1
