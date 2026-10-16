"""#1095: the rare accounting writers and readers verify a bounded ledger slice."""

from __future__ import annotations

import json

import pytest

from tests.test_inference_accounting import accounting_db, request  # noqa: F401
from tests.test_inference_ledger_v2 import _admission, _ledger, _sql
from src.workflows import inference_ledger
from src.workflows.inference_accounting import InferenceAccountingError


def _forbid_full_load(monkeypatch, repository):
    """Fail if the whole ledger is loaded while a full pass is not due."""
    real = repository._accounting_rows
    calls = []

    async def guarded(db):
        calls.append(True)
        return await real(db)

    monkeypatch.setattr(repository, "_accounting_rows", guarded)
    return calls


@pytest.mark.asyncio
async def test_startup_recovery_verifies_off_lock_then_sweeps_without_a_full_load(accounting_db, monkeypatch):
    root, _engine, _factory = accounting_db
    repository, broker = await _ledger(root, operations=(("op-a", "0.000020"), ("op-c", "0.000030")))
    await broker._prepare_accounting(request("op-open", owner="service:op-open"))
    inference_ledger.reset_process_state()  # a fresh process owes a full pass (R7)
    calls = _forbid_full_load(monkeypatch, repository)
    assert await repository.recover_inference_accounting() == []
    # The background verifier ran on a worker thread; the sweep itself only
    # verified the witness, the digest root and the open rows.
    assert calls == []
    assert await _admission(broker) == "admitted"
    assert calls == []


@pytest.mark.asyncio
async def test_startup_recovery_still_latches_on_an_untouched_row_edit(accounting_db, monkeypatch):
    root, _engine, _factory = accounting_db
    repository, broker = await _ledger(root, operations=(("op-a", "0.000020"), ("op-c", "0.000030")))
    monkeypatch.setattr(inference_ledger, "SHADOW_FULL_LEDGER", False)  # the oracle assumes no tampering
    _sql(root, "UPDATE inference_cost_reservations SET actual_cost_microusd = 0 WHERE operation_id = 'op-a'")
    inference_ledger.reset_process_state()
    with pytest.raises(InferenceAccountingError, match="accounting_continuity_unavailable"):
        await repository.recover_inference_accounting()
    assert "accounting_continuity_unavailable" in await _admission(broker)


@pytest.mark.asyncio
@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("value", ["released", "settled", "contact_started"])
async def test_recovery_refuses_an_open_row_hidden_or_moved_by_a_state_edit(accounting_db, monkeypatch, scoped, value):
    """``state`` is a content column: hiding an open row must not skip its recovery."""
    root, _engine, _factory = accounting_db
    repository, broker = await _ledger(root, operations=(("op-a", "0.000020"),))
    monkeypatch.setattr(inference_ledger, "SHADOW_FULL_LEDGER", False)
    await broker._prepare_accounting(request("op-open", owner="service:op-open"))
    job_id = _sql(root, "SELECT job_id FROM inference_cost_reservations WHERE operation_id = 'op-open'")[0][0]
    _sql(root, "UPDATE inference_cost_reservations SET state = ? WHERE operation_id = 'op-open'", value)
    with pytest.raises(InferenceAccountingError, match="accounting_continuity_unavailable"):
        await repository.recover_inference_accounting(job_id=job_id if scoped else None)


@pytest.mark.asyncio
async def test_settings_save_and_reserve_review_use_the_sealed_totals(accounting_db, monkeypatch):
    root, _engine, _factory = accounting_db
    repository, broker = await _ledger(root)  # op-b overran its 100 µUSD bound
    calls = _forbid_full_load(monkeypatch, repository)
    assert "provider_charge_exceeded_reservation" in await _admission(broker, "op-latched")
    await repository.configure_inference_accounting(2000)
    await repository.configure_inference_accounting(2000, reserve_review_microusd=200)
    assert calls == []
    history = json.loads(_sql(root, "SELECT settings_history_json FROM inference_accounting_owners")[0][0])
    review = [entry for entry in history if entry.get("kind") == "request_reserve_review"]
    assert [item["operation_id"] for item in review[-1]["operations"]] == ["op-b"]


@pytest.mark.asyncio
async def test_settings_save_refuses_a_tampered_touched_account(accounting_db, monkeypatch):
    root, _engine, _factory = accounting_db
    repository, _broker = await _ledger(root, operations=(("op-a", "0.000020"),))
    monkeypatch.setattr(inference_ledger, "SHADOW_FULL_LEDGER", False)
    _sql(root, "UPDATE inference_accounting_owners SET ceiling_microusd = 999999")
    with pytest.raises(InferenceAccountingError, match="accounting_continuity_unavailable"):
        await repository.configure_inference_accounting(2000)
