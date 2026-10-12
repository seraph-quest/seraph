"""Completion cannot reconstruct live Source or capacity from a claim-shaped value.

These are pre-writer negatives, not a fixture for genuine captured retention.
Full original-positive/copy/task cases require the complete owner composer.
"""
from contextlib import asynccontextmanager
from dataclasses import replace
from types import MappingProxyType

import pytest

from src.memory.header_bounds import HeaderReadBudget
from src.runtime_plugins.dispatch import NativeServiceBlocked
from src.runtime_plugins.memory_producer import _memory_completion_capacity_source
from src.workflows.job_runtime import DurableJobRepository, NativeServiceClaim


@pytest.mark.asyncio
@pytest.mark.parametrize("copied", [False, True])
async def test_unissued_typed_claim_and_raw_frame_cannot_open_completion_writer(monkeypatch, copied):
    # A typed value does not create the original private issuer registry entry.
    # No Source, retention, numeric vector or permission is fabricated here.
    claim = NativeServiceClaim(MappingProxyType({"job_id": "unissued-completion"}),
        MappingProxyType({}), None, "0" * 64, object())
    if copied:
        claim = replace(claim)
    budget = HeaderReadBudget()
    remaining = budget.remaining
    calls = []
    @asynccontextmanager
    async def unavailable_writer(*args, **kwargs):
        calls.append("writer")
        raise AssertionError("completion reached a writer without original Source")
        yield
    repository = DurableJobRepository()
    monkeypatch.setattr(repository, "_writer_session", unavailable_writer)
    with pytest.raises(NativeServiceBlocked, match="native_memory_actual_claim_source_unavailable"):
        await repository.complete_native_memory(claim, result={}, header_budget=budget)
    assert calls == []
    assert budget.remaining == remaining


@pytest.mark.asyncio
async def test_missing_original_claim_denies_before_source_lookup_or_payment():
    budget = HeaderReadBudget()
    remaining = budget.remaining
    with pytest.raises(NativeServiceBlocked, match="native_memory_actual_claim_source_required"):
        _memory_completion_capacity_source(object(), header_budget=budget)
    assert budget.remaining == remaining
