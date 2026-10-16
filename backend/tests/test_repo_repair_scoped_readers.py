"""#1114: repository readers read a bounded, sealed slice of the reservations.

The readers select by the unhashed group key, the owner and the job instead of
loading the whole table, so an edit to those columns could hide a row from
them. Each read must hold exactly the sealed group membership and every read
row must hash to its stored digest; otherwise the reader refuses.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tests.test_inference_accounting import accounting_db, request  # noqa: F401
from tests.test_inference_ledger_v2 import _ledger, _seed_rows, _sql
from src.workflows import inference_ledger
from src.workflows import repo_repair_source as source
from src.workflows import repo_repair_stop as stop
from src.workflows.inference_accounting import InferenceAccountingError
from src.workflows.job_runtime import DurableJobLeaseError

GROUP, OTHER, OWNER, ROOT = "a" * 64, "c" * 64, "operator:owner", "repository-root"


def _evidence(group_id):
    return json.dumps([{"kind": "general_task_group_reservation.v1", "group": {"group_id": group_id}}])


async def _group_ledger(root):
    """Two Root members, one sibling member and one other group's row, sealed."""
    repository, _broker = await _ledger(root, operations=(("op-a", "0.000020"),))
    member = {"evidence_json": _evidence(GROUP), "group_lookup_key": "g:" + GROUP, "owner_id": OWNER}
    _seed_rows(root, [{**member, "operation_id": "root-1", "job_id": ROOT},
                      {**member, "operation_id": "root-2", "job_id": ROOT},
                      {**member, "operation_id": "sibling", "job_id": "sibling-job"},
                      {"operation_id": "outsider", "evidence_json": _evidence(OTHER), "group_lookup_key": "g:" + OTHER,
                       "owner_id": OWNER, "job_id": "outsider-job", "bound_microusd": 1}])
    return repository


def _context():
    return {"group": SimpleNamespace(group_id=GROUP, owner_principal_id=OWNER),
            "run": SimpleNamespace(run_identity=ROOT)}


@pytest.mark.asyncio
async def test_scoped_group_read_returns_exactly_the_sealed_members(accounting_db):
    root, _engine, _factory = accounting_db
    repository = await _group_ledger(root)
    async with repository._session() as db:
        rows = await source._repository_accounting_rows(db, _context())
        assert [row.operation_id for row in rows] == ["root-1", "root-2", "sibling"]
        assert [row.operation_id for row in await source._job_cost_rows(db, ROOT)] == ["root-1", "root-2"]


_HIDE = {
    # unhashed lookup key moved to another group, or cleared off the owner's scope
    "key_moved": ["UPDATE inference_cost_reservations SET group_lookup_key = 'g:' || ? WHERE operation_id = 'sibling'"],
    # content columns on a row the scoped read then never loads
    "owner_moved": ["UPDATE inference_cost_reservations SET owner_id = 'operator:other' WHERE operation_id = 'sibling'"],
    "deleted": ["DELETE FROM inference_cost_reservations WHERE operation_id = 'sibling'"],
    # a member out and a cheap non-member in under the key: same row count
    "swapped": ["UPDATE inference_cost_reservations SET group_lookup_key = 'g:' || ? WHERE operation_id = 'sibling'",
                "UPDATE inference_cost_reservations SET group_lookup_key = 'g:' || ? WHERE operation_id = 'outsider'"],
}


@pytest.mark.asyncio
@pytest.mark.parametrize("hide", sorted(_HIDE))
async def test_member_hidden_from_the_scoped_read_is_refused(accounting_db, monkeypatch, hide):
    root, _engine, _factory = accounting_db
    repository = await _group_ledger(root)
    monkeypatch.setattr(inference_ledger, "SHADOW_FULL_LEDGER", False)  # the oracle assumes no tampering
    for statement, value in zip(_HIDE[hide], (OTHER, GROUP)):
        _sql(root, statement, *([value] if "?" in statement else []))
    async with repository._session() as db:
        with pytest.raises(InferenceAccountingError, match="accounting_continuity_unavailable"):
            await source._repository_remaining(db, _context())
        with pytest.raises(DurableJobLeaseError, match="original stop accounting rows changed"):
            await stop._original_accounting_members(db, _context())
        with pytest.raises(DurableJobLeaseError, match="original repository accounting rows changed"):
            await source._iteration_cost_rows(db, _context()["run"], _context()["group"], None,
                error=lambda: DurableJobLeaseError("original repository accounting rows changed"))


@pytest.mark.asyncio
@pytest.mark.parametrize("edit", ["state = 'released'", "bound_microusd = 1", "actual_cost_microusd = 0",
                                  "job_id = 'elsewhere'"])
async def test_content_edit_to_a_read_row_is_refused(accounting_db, monkeypatch, edit):
    """A Root row moved off the job is still read as a group member and refused."""
    root, _engine, _factory = accounting_db
    repository = await _group_ledger(root)
    monkeypatch.setattr(inference_ledger, "SHADOW_FULL_LEDGER", False)
    _sql(root, f"UPDATE inference_cost_reservations SET {edit} WHERE operation_id = 'root-2'")
    async with repository._session() as db:
        with pytest.raises(InferenceAccountingError, match="accounting_continuity_unavailable"):
            await source._repository_accounting_rows(db, _context())
        with pytest.raises(DurableJobLeaseError, match="original stop accounting rows changed"):
            await stop._original_accounting_members(db, _context())
        if edit != "job_id = 'elsewhere'":
            with pytest.raises(InferenceAccountingError, match="accounting_continuity_unavailable"):
                await source._job_cost_rows(db, ROOT)


@pytest.mark.asyncio
async def test_contacted_row_outside_the_scope_is_digest_checked(accounting_db, monkeypatch):
    root, _engine, _factory = accounting_db
    repository = await _group_ledger(root)
    monkeypatch.setattr(inference_ledger, "SHADOW_FULL_LEDGER", False)
    _sql(root, "UPDATE inference_cost_reservations SET bound_microusd = 2 WHERE operation_id = 'outsider'")
    from src.db.models import InferenceCostReservation
    async with repository._session() as db:
        cost = await db.get(InferenceCostReservation, "outsider")
        with pytest.raises(DurableJobLeaseError, match="original repository accounting rows changed"):
            await source._iteration_cost_rows(db, _context()["run"], _context()["group"], cost,
                error=lambda: DurableJobLeaseError("original repository accounting rows changed"))


@pytest.mark.asyncio
@pytest.mark.parametrize("totals", ["", "{}", "unsealed_member"])
async def test_missing_or_stale_sealed_totals_are_refused(accounting_db, monkeypatch, totals):
    root, _engine, _factory = accounting_db
    repository = await _group_ledger(root)
    monkeypatch.setattr(inference_ledger, "SHADOW_FULL_LEDGER", False)
    if totals == "unsealed_member":
        sealed = json.loads(_sql(root, "SELECT ledger_totals_json FROM inference_accounting_owners")[0][0])
        sealed["rows_by_group"]["g:" + GROUP] += 1
        totals = json.dumps(sealed)
    _sql(root, "UPDATE inference_accounting_owners SET ledger_totals_json = ?", totals)
    async with repository._session() as db:
        with pytest.raises(InferenceAccountingError, match="accounting_continuity_unavailable"):
            await source._repository_accounting_rows(db, _context())


@pytest.mark.asyncio
async def test_rows_this_session_changed_are_left_to_its_writer(accounting_db, monkeypatch):
    """An accounting writer's own unsealed change is not a tamper."""
    from src.db.models import InferenceCostReservation
    root, _engine, _factory = accounting_db
    repository = await _group_ledger(root)
    monkeypatch.setattr(inference_ledger, "SHADOW_FULL_LEDGER", False)
    async with repository._session() as db:
        with db.no_autoflush:
            row = await db.get(InferenceCostReservation, "root-1")
            row.state = "released"
            rows = await source._repository_accounting_rows(db, _context())
        assert [item.operation_id for item in rows] == ["root-1", "root-2", "sibling"]
        await db.rollback()
