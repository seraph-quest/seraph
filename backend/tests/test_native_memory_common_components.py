"""Original common-cache acquisition separated from Memory Source callbacks."""
import asyncio
import pytest
from src.memory import header_bounds, composition_headers
from src.memory.header_bounds import HeaderBoundsError
from src.workspace import accounting_witness
from tests.test_native_memory_producer import actual_memory


def test_unissued_common_planning_rejects_before_database():
    class Poison:
        @property
        def db(self):
            raise AssertionError("unissued operation reached database")
    with pytest.raises(HeaderBoundsError):
        accounting_witness._native_memory_future_common_numeric_components(
            Poison(), object(), certificate=object(), facts=object(),
            retention_widths=object(), _construction_only=True)


@pytest.mark.asyncio
async def test_genuine_common_components_prepaid_and_exclude_source_callbacks(actual_memory, monkeypatch):
    client, _operator, _jobs, _host, _calls, task, attempt, _output = actual_memory
    helper = accounting_witness._native_memory_future_common_numeric_components
    reached = []

    async def observe(db, run, operation, prepared, *, existing_references, before_row, certificate):
        guard = db.info["composition_guard"]
        facts = guard._native_memory_source_capacity_facts[3]
        widths = guard._native_memory_retention_numeric_facts[3]
        args = dict(certificate=certificate, facts=facts, retention_widths=widths,
            _construction_only=True)
        before = certificate.budget.remaining
        with pytest.raises(HeaderBoundsError):
            helper(operation, prepared, **{**args, "facts": dict(facts)})
        with pytest.raises(HeaderBoundsError):
            helper(operation, prepared, **{**args, "_construction_only": 1})
        token = composition_headers._CURRENT.set(None)
        try:
            with pytest.raises(HeaderBoundsError):
                helper(operation, prepared, **args)
        finally:
            composition_headers._CURRENT.reset(token)
        async def child():
            with pytest.raises(HeaderBoundsError):
                helper(operation, prepared, **args)
        await asyncio.create_task(child())
        capacity = helper(operation, prepared, **args)
        assert type(capacity) is int and capacity > 0
        assert certificate.budget.remaining == before
        certificate.budget.debit(capacity, appearance=("test-common-components", "construction"))
        components = helper(operation, prepared, certificate=certificate, facts=facts,
            retention_widths=widths)
        assert tuple(name for name, entries in components) == ("core", "miss", "hit")
        core, miss, hit = (entries for name, entries in components)
        validation = 9 if certificate.schema_cookies is not None else 3
        assert len(miss) == 2 * validation + 12
        assert len(hit) == validation + 6
        assert all(amount == 47 for label, amount in miss + hit)
        assert all(label[0] == "metadata-prequery" and label[1] in {
            "state-changes", "schema-main", "schema-temp"} for label, amount in miss + hit)
        assert not any(type(label) is tuple and "memory-universe" in label
            for label, amount in core + miss + hit)
        assert not any(type(label) is tuple and "common-certification-trace" in label
            for label, amount in core + miss + hit)
        reached.append(1)
        raise HeaderBoundsError("test_common_components_observed")

    monkeypatch.setattr(header_bounds, "_reserve_native_memory_effect_ledger", observe)
    response = await client.post("/api/memory/task-proposals/native", json={
        "task_id": task.task_id, "expected_task_revision": task.task_revision,
        "attempt_id": attempt.attempt_id, "idempotency_key": "common-components",
    })
    assert response.status_code == 409 and reached == [1]
