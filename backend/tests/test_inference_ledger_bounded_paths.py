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


# --- #1098 review P3: the programme budget reads sealed row evidence --------

def _programme(job_id, programme_id="programme-1"):
    from types import SimpleNamespace
    from src.work_board.research_parent import DISCOVERY_KIND, DISCOVERY_SERVICE, discovery_authority
    binding = {"goal_id": "goal-1", "goal_revision": 1, "programme_id": programme_id, "grant_revision": 1,
        "owner_identity_id": "identity-1", "issuer_root_id": "root-1", "issuer_principal_id": "user:issuer",
        "brief_digest": "b" * 64, "capability_id": "guardian.goal-discovery.v1", "route_epoch": 1,
        "route_digest": "r" * 64, "expires_at": "2026-12-01T00:00:00Z", "cost_ceiling_microusd": 100}
    authority = {"authority_type": "goal_programme_discovery_v1", "principal": DISCOVERY_SERVICE,
        "owner_kind": "service", "service_id": DISCOVERY_SERVICE, "capability_id": "guardian.goal-discovery.v1",
        "capability_version": "1", "goal_owner_principal_id": "user:issuer", "goal_owner_session_id": "root-1",
        "programme_binding": binding, "plan_ref": {"artifact_id": "plan-1", "digest": "c" * 64, "schema_version": 1},
        "occurrence_day": "2026-10-16", "original_job_id": job_id, "budget_microusd": 100, "no_learning": True}
    run = SimpleNamespace(run_identity=job_id, job_kind=DISCOVERY_KIND, declared_authority_json=json.dumps(authority))
    sealed = discovery_authority(run.declared_authority_json).programme_binding.model_dump(mode="json")
    row = SimpleNamespace(operation_id=f"remote:{job_id}:0", job_id=job_id, owner_id=DISCOVERY_SERVICE,
        goal_id="goal-1", goal_revision=1, state="reserved", bound_microusd=60, actual_cost_microusd=None,
        evidence_json=json.dumps([{"kind": "reservation"}, {"kind": "goal_programme_binding",
            "binding": sealed, "native_job_id": job_id}]))
    return run, row


class _Runs:
    def __init__(self, runs):
        self.runs = runs

    async def execute(self, _query):
        from types import SimpleNamespace
        return SimpleNamespace(scalars=lambda: list(self.runs))


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", [None, "job_kind", "programme_id"])
async def test_programme_budget_counts_a_prior_row_whatever_its_run_row_says(monkeypatch, tamper):
    """An edit to the prior run (unsealed) must not drop its cost from the budget."""
    from src.workflows import research_accounting, research_guard

    async def no_authority_check(*_args, **_kwargs):
        return None
    monkeypatch.setattr(research_guard, "assert_discovery_authority", no_authority_check)
    prior_run, prior_row = _programme("goal-discovery:" + "1" * 32)
    current, _row = _programme("goal-discovery:" + "2" * 32)
    if tamper == "job_kind":
        prior_run.job_kind = "goal_discovery_planning_v1"
    elif tamper == "programme_id":
        prior_run.declared_authority_json = _programme(prior_run.run_identity, "programme-elsewhere")[0].declared_authority_json
    db = _Runs([prior_run, current])
    if tamper is None:
        with pytest.raises(InferenceAccountingError, match="programme_generation_cost_budget_exhausted"):
            await research_accounting.discovery_generation_budget(None, db, current, [prior_row], new_bound=60)
        return
    with pytest.raises(InferenceAccountingError, match="programme_cost_original_lineage_changed"):
        await research_accounting.discovery_generation_budget(None, db, current, [prior_row], new_bound=60)


# --- #1095 review P3s: open-set swaps, readback tamper and concurrency ------

@pytest.mark.asyncio
async def test_open_rows_refuse_a_swap_of_states_with_equal_bounds(accounting_db, monkeypatch):
    """Per-state sums survive swapping two equal-bound rows' states; digests do not."""
    root, _engine, _factory = accounting_db
    repository, broker = await _ledger(root, operations=(("op-a", "0.000020"),))
    monkeypatch.setattr(inference_ledger, "SHADOW_FULL_LEDGER", False)
    first = await broker._prepare_accounting(request("op-first", owner="service:op-first"))
    await broker._prepare_accounting(request("op-second", owner="service:op-second"))
    await broker._contact_accounting(first)
    assert _sql(root, "SELECT operation_id, state FROM inference_cost_reservations WHERE operation_id LIKE 'op-%' "
        "AND state != 'settled' ORDER BY operation_id") == [("op-first", "contact_started"), ("op-second", "reserved")]
    _sql(root, "UPDATE inference_cost_reservations SET state = CASE operation_id WHEN 'op-first' THEN 'reserved' "
        "ELSE 'contact_started' END WHERE operation_id IN ('op-first', 'op-second')")
    with pytest.raises(InferenceAccountingError, match="accounting_continuity_unavailable"):
        await repository.recover_inference_accounting(job_id="any-job")


async def _group_costs(repository):
    from src.work_board.research_readback import _verified_group_costs
    async with repository._session() as db:
        return await _verified_group_costs(repository, db, ["child-a"], ["remote:child-a"])


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["ledger_row_digest", "job_id"])
async def test_research_readback_refuses_a_tampered_group_row(accounting_db, monkeypatch, tamper):
    from tests.test_inference_ledger_v2 import _seed_rows
    root, _engine, _factory = accounting_db
    repository, _broker = await _ledger(root, operations=(("op-a", "0.000020"),))
    monkeypatch.setattr(inference_ledger, "SHADOW_FULL_LEDGER", False)
    _seed_rows(root, [{"operation_id": "remote:child-a", "job_id": "child-a"}])
    assert [row.operation_id for row in await _group_costs(repository)] == ["remote:child-a"]
    value = "0" * 64 if tamper == "ledger_row_digest" else "job-elsewhere"
    _sql(root, f"UPDATE inference_cost_reservations SET {tamper} = ? WHERE operation_id = 'remote:child-a'", value)
    with pytest.raises(InferenceAccountingError, match="accounting_continuity_unavailable"):
        await _group_costs(repository)


@pytest.mark.asyncio
async def test_research_readback_is_not_made_busy_by_a_concurrent_accounting_writer(accounting_db, monkeypatch):
    from pathlib import Path
    from config.settings import settings
    from tests.test_inference_ledger_v2 import _seed_rows
    from src.workflows.inference_accounting import _continuity_lock
    root, _engine, _factory = accounting_db
    repository, _broker = await _ledger(root, operations=(("op-a", "0.000020"),))
    monkeypatch.setattr(inference_ledger, "SHADOW_FULL_LEDGER", False)
    _seed_rows(root, [{"operation_id": "remote:child-a", "job_id": "child-a"}])
    # A writer holds the exclusive accounting flock (non-blocking for others).
    with _continuity_lock(Path(settings.workspace_dir).resolve()):
        assert [row.operation_id for row in await _group_costs(repository)] == ["remote:child-a"]


@pytest.mark.asyncio
async def test_research_readback_without_an_accounting_owner_fails_closed(accounting_db, monkeypatch):
    from tests.test_inference_accounting import setup_configuration
    root, _engine, _factory = accounting_db
    setup_configuration(ceiling=1000, bound=100)  # deployment binding, but no accounting owner yet
    repository = __import__("src.workflows.job_runtime", fromlist=["DurableJobRepository"]).DurableJobRepository()
    assert _sql(root, "SELECT count(*) FROM inference_accounting_owners") == [(0,)]
    from pathlib import Path
    from config.settings import settings
    from src.workflows.inference_accounting import _continuity_lock
    with _continuity_lock(Path(settings.workspace_dir).resolve(), exclusive=False):
        pass  # the lock itself is available: the refusal below is the missing owner
    with pytest.raises(InferenceAccountingError, match="accounting_continuity_unavailable"):
        await _group_costs(repository)
