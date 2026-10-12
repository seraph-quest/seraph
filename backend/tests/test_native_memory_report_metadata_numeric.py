"""Original metadata observations; no synthetic Source or successful ledger."""
import asyncio
from copy import copy

import pytest

from src.memory import composition_headers
from src.memory.header_bounds import HeaderBoundsError
from src.runtime_plugins import memory_producer
from tests.test_native_memory_producer import actual_memory


@pytest.mark.parametrize("construction", [False, True])
def test_unissued_metadata_schedule_denies_before_db(construction):
    class Poison:
        @property
        def db(self):
            raise AssertionError("unissued metadata schedule accessed DB")

    with pytest.raises(HeaderBoundsError):
        memory_producer.original_native_memory_report_metadata_numeric_schedule(
            Poison(), None, certificate=None, facts=None, retention_widths=None,
            _construction_only=construction)


def test_metadata_constructor_mode_is_literal_bool():
    with pytest.raises(HeaderBoundsError, match="memory_report_metadata_constructor_mode_changed"):
        memory_producer.original_native_memory_report_metadata_numeric_schedule(
            None, None, certificate=None, facts=None, retention_widths=None,
            _construction_only=1)


@pytest.mark.parametrize("label", [
    None, True, False, -1, 2**63 - 1, "ascii", "a\n\"\\é😀",
    ("native-memory-report-trace", "entry-pair", 3),
    ("composition-binding-parser", None, ("manifest", 1, "domain-values")),
])
def test_metadata_label_loop_preserves_original_codec(label):
    assert memory_producer._native_memory_report_metadata_label_numeric_upper(label) == (
        composition_headers._common_trace_label_width(label))


@pytest.mark.asyncio
async def test_genuine_metadata_schedule_reuses_ordered_original_pairs(actual_memory, monkeypatch):
    client, _operator, _jobs, _host, _calls, task, attempt, _output = actual_memory
    from src.workspace import accounting_witness

    observed = []

    async def inspect(db, run, operation, prepared, *, existing_references, before_row, certificate):
        guard = db.info["composition_guard"]
        facts = guard._native_memory_source_capacity_facts[3]
        widths = guard._native_memory_retention_numeric_facts[3]
        kwargs = dict(certificate=certificate, facts=facts, retention_widths=widths)
        helper = memory_producer.original_native_memory_report_metadata_numeric_schedule
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
        lineage = memory_producer._native_memory_report_metadata_numeric_lineage(
            operation, prepared, **kwargs)
        trace = lineage[8]
        with monkeypatch.context() as isolated:
            isolated.delattr(lineage[0], "_report_metadata_numeric_trace")
            with pytest.raises(HeaderBoundsError):
                helper(operation, prepared, **kwargs, _construction_only=True)
        from src.security import site_policy
        with monkeypatch.context() as isolated:
            isolated.setattr(site_policy.settings, "browser_site_allowlist",
                site_policy.settings.browser_site_allowlist + ",changed.example")
            with pytest.raises(HeaderBoundsError, match="memory_report_metadata_policy_changed"):
                helper(operation, prepared, **kwargs, _construction_only=True)
        assert frame.remaining == before
        capacity = helper(operation, prepared, **kwargs, _construction_only=True)
        assert type(capacity) is int and capacity > 0 and frame.remaining == before
        frame.debit(capacity, appearance=("test-report-metadata", "constructor"))
        vector = helper(operation, prepared, **kwargs)
        assert len(vector) == 13 + 3 * len(trace)
        for index, entry in enumerate(trace):
            offset = 10 + 3 * index
            pair_upper = 3 + composition_headers._common_trace_label_width(entry[0]) + 20
            assert vector[offset] == (("native-memory-report-trace", "entry-pair", index), pair_upper)
            assert vector[offset + 1] == (("native-memory-report-trace", "entry-list-slot", index), pair_upper + 1)
            assert vector[offset + 2] is entry
        # Independent encoded-container census catches missing output pairs,
        # labels or either of the distinct list/tuple constructors.
        fresh = vector[:10] + tuple(vector[10 + 3 * index + side]
            for index in range(len(trace)) for side in (0, 1)) + vector[-3:]
        container_upper = 2 + len(vector) - 1 + sum(
            3 + composition_headers._common_trace_label_width(label) + 20
            for label, _ in vector)
        fresh_upper = sum(3 + 2 * composition_headers._common_trace_label_width(label) + 20
            for label, _ in fresh)
        assert capacity == fresh_upper + 2 * container_upper
        # Four original candidate reads retain their repeated parser fees.
        assert sum(label == ("native-memory-report-parser", "journal-parser") for label, _ in trace) >= 4
        # Source proof is outside the observed metadata lexical region.
        assert not any(label == ("native-memory-report-stage", "proof-map") for label, _ in trace)
        observed.append(vector)
        raise HeaderBoundsError("test_report_metadata_schedule_observed")

    monkeypatch.setattr(accounting_witness, "_native_memory_effect_numeric_schedule", inspect)
    response = await client.post("/api/memory/task-proposals/native", json={
        "task_id": task.task_id, "expected_task_revision": task.task_revision,
        "attempt_id": attempt.attempt_id, "idempotency_key": "metadata-schedule-observer"})
    assert response.status_code == 409 and len(observed) == 1
