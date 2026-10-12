"""Original Source pre-facts plan validation custody; offline tests."""
import asyncio
from copy import copy
import pytest
from src.memory import m5, composition_headers
from src.memory.header_bounds import HeaderBoundsError
from tests.test_native_memory_producer import actual_memory


def test_unissued_prefacts_source_denies_before_plan_db():
    class Poison:
        @property
        def info(self):
            raise AssertionError("unissued source reached db")
    with pytest.raises(HeaderBoundsError):
        m5._native_prepared_original_source_frame(object(), Poison(), object())


@pytest.mark.asyncio
async def test_genuine_prefacts_validator_pays_and_rejects_copied_child_frame(actual_memory, monkeypatch):
    client, _operator, _jobs, _host, _calls, task, attempt, _output = actual_memory
    original = m5.validate_native_memory_mutation_plan
    observed = []
    async def observe(plan, *, db, candidate_digest, consumed=False, native_memory_source=None):
        if native_memory_source is not None:
            source = native_memory_source
            before = source.admission.header_budget.remaining
            with pytest.raises(HeaderBoundsError):
                m5._native_prepared_original_source_frame(plan, db, copy(source))
            async def child():
                with pytest.raises(HeaderBoundsError):
                    m5._native_prepared_original_source_frame(plan, db, source)
            await asyncio.create_task(child())
            token = composition_headers._CURRENT.set(None)
            try:
                with pytest.raises(HeaderBoundsError):
                    m5._native_prepared_original_source_frame(plan, db, source)
            finally:
                composition_headers._CURRENT.reset(token)
            assert source.admission.header_budget.remaining == before
            await original(plan, db=db, candidate_digest=candidate_digest,
                consumed=consumed, native_memory_source=source)
            assert source.admission.header_budget.remaining < before
            observed.append(plan)
            raise HeaderBoundsError("test_prefacts_validator_observed")
        return await original(plan, db=db, candidate_digest=candidate_digest, consumed=consumed)
    monkeypatch.setattr(m5, "validate_native_memory_mutation_plan", observe)
    response = await client.post("/api/memory/task-proposals/native", json={
        "task_id": task.task_id, "expected_task_revision": task.task_revision,
        "attempt_id": attempt.attempt_id, "idempotency_key": "prefacts-validator-observer"})
    assert response.status_code == 409 and len(observed) == 1


@pytest.mark.asyncio
async def test_original_apply_calls_existing_validator_without_prefacts_keyword(monkeypatch):
    # Stop at the original validator boundary; no plan permission/effect fixture.
    class StopAtOriginalValidator(Exception):
        pass
    class CandidateOnly:
        _candidate_digest = "original-digest"
    plan, db = CandidateOnly(), object()
    calls = []
    async def validator(actual, **kwargs):
        assert actual is plan
        assert kwargs == {"db": db, "candidate_digest": "original-digest"}
        calls.append(actual)
        raise StopAtOriginalValidator
    monkeypatch.setattr(m5, "validate_native_memory_mutation_plan", validator)
    with pytest.raises(StopAtOriginalValidator):
        await m5._apply_native_memory_mutation_plan(db, plan, method="memory.propose")
    assert calls == [plan]
