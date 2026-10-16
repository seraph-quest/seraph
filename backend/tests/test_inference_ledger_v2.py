"""#1086: bounded ledger verification keeps every tamper and rollback guarantee."""

from __future__ import annotations

import json
import random
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from tests.test_inference_accounting import accounting_db, request, setup_configuration  # noqa: F401
from src.model_fabric.remote_inference_admission import RemoteInferenceAdmissionBroker
from src.workflows import inference_ledger
from src.workflows.inference_accounting import InferenceAccountingError
from src.workflows.job_runtime import DurableJobRepository
from src.workspace import accounting_witness as codec
from src.workspace.production import (
    ProductionWorkspace, lifecycle_receipt_path, read_accounting_checkpoint, read_lifecycle_receipt, write_lifecycle_receipt,
)


async def _charge(cost):
    async def provider():
        return {"usage": {"cost": cost}} if cost is not None else {"usage": {"prompt_tokens": 1}}
    return provider


async def _ledger(root, *, ceiling=1000, operations=(("op-a", "0.000020"), ("op-c", "0.000030"), ("op-b", "0.000150")), fillers=0):
    """Settled calls; by default op-b (last) overran its 100 µUSD bound (the overrun latch).

    ``fillers`` 1 µUSD calls come first, so with 128 of them the named rows
    fall outside the 128 operations a status snapshot lists (and re-checks).
    """
    setup_configuration(ceiling=ceiling, bound=100)
    repository = DurableJobRepository()
    await repository.configure_inference_accounting(ceiling)
    broker = RemoteInferenceAdmissionBroker(durable_accounting=True)
    for index in range(fillers):
        await broker.execute(request(f"fill-{index:03d}", owner=f"service:fill-{index:03d}"), await _charge("0.000001"))
    for operation_id, cost in operations:
        await broker.execute(request(operation_id, owner=f"service:{operation_id}"), await _charge(cost))
    return repository, broker


def _sql(root, statement, *params):
    connection = sqlite3.connect(root / "seraph.db", isolation_level=None)
    try:
        return connection.execute(statement, params).fetchall()
    finally:
        connection.close()


async def _admission(broker, operation_id="op-next"):
    try:
        await broker.execute(request(operation_id, owner=f"service:{operation_id}"), await _charge("0.000001"))
    except Exception as exc:  # the broker wraps accounting denials
        return str(exc)
    return "admitted"


# --- pure codec properties -------------------------------------------------

def _random_row(rng, index):
    state = rng.choice(["reserved", "contact_started", "unknown", "settled", "released"])
    bound = rng.randint(1, 200)
    return {"operation_id": f"op-{index}", "owner_id": rng.choice(["a", "b", "c"]), "state": state,
        "bound_microusd": bound, "actual_cost_microusd": rng.randint(0, 300) if state == "settled" else None,
        "period_id": rng.choice(["2026-08", "2026-09", "2026-10"]), "sequence": index, "revision": 1,
        "job_id": f"job-{index}", "runtime_path": "chat_agent"}


@pytest.mark.parametrize("seed", range(6))
def test_incremental_totals_equal_recomputed_totals_under_random_writes(seed):
    rng = random.Random(seed)
    rows, totals = {}, codec.empty_ledger_totals()
    for step in range(400):
        if rows and rng.random() < 0.6:
            key = rng.choice(sorted(rows))
            pre = rows[key]
            post = {**_random_row(rng, int(key[3:])), "operation_id": key, "revision": pre["revision"] + 1}
            codec.apply_ledger_contribution(totals, pre, -1)
        else:
            post = _random_row(rng, len(rows) + step * 1000)
        codec.apply_ledger_contribution(totals, post, 1)
        rows[post["operation_id"]] = post
        assert totals == codec.ledger_totals(rows.values())
        period = rng.choice(["2026-08", "2026-09", "2026-10"])
        expected = sum(row["bound_microusd"] if row["state"] in {"reserved", "contact_started", "unknown"}
            else (row["actual_cost_microusd"] or 0) if row["state"] == "settled" and row["period_id"] >= period else 0
            for row in rows.values())
        assert codec.held_microusd(totals, period) == expected


def test_row_digest_normalises_aware_and_naive_utc_datetimes():
    naive = {"operation_id": "x", "created_at": datetime(2026, 10, 16, 12, 0, 0), "updated_at": "2026-10-16 12:00:00.000000"}
    aware = {"operation_id": "x", "created_at": datetime(2026, 10, 16, 14, 0, 0, tzinfo=timezone(timedelta(hours=2))),
        "updated_at": "2026-10-16T12:00:00+00:00"}
    assert codec.row_digest(naive) == codec.row_digest(aware)
    assert codec.row_digest({**naive, "ledger_row_digest": "f" * 64, "group_lookup_key": "none"}) == codec.row_digest(naive)


# --- runtime ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_digest_at_flush_matches_reloaded_raw_rows_and_writes_are_v2(accounting_db):
    root, _engine, _factory = accounting_db
    repository, _broker = await _ledger(root)
    connection = sqlite3.connect(root / "seraph.db")
    connection.row_factory = sqlite3.Row
    try:
        raw = [dict(row) for row in connection.execute("SELECT * FROM inference_cost_reservations")]
        account = dict(connection.execute("SELECT * FROM inference_accounting_owners").fetchone())
    finally:
        connection.close()
    assert len(raw) == 3 and all(row["ledger_row_digest"] == codec.row_digest(row) for row in raw)
    assert codec.is_v2_digest(account["ledger_digest"])
    assert codec.ledger_digest(account, raw) == account["ledger_digest"]
    assert json.loads(account["ledger_totals_json"]) == codec.ledger_totals(raw)
    snapshot = await repository.inference_accounting_snapshot()
    assert snapshot["reason_code"] == "provider_charge_exceeded_reservation"
    assert snapshot["committed_microusd"] == 200 and snapshot["operation_count"] == 3


def _assert_immediately_blocked(snapshot_value, admission):
    assert snapshot_value["status"] == "blocked" and snapshot_value["reason_code"] == "accounting_continuity_unavailable"
    assert "accounting_continuity_unavailable" in admission


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["delete_row", "insert_row", "row_digest_column", "account_totals",
                                    "consistent_row_and_digest", "account_ceiling"])
async def test_structural_tampering_blocks_the_next_operation(accounting_db, monkeypatch, tamper):
    monkeypatch.setattr(inference_ledger, "SHADOW_FULL_LEDGER", False)  # the oracle assumes no tampering
    root, _engine, _factory = accounting_db
    repository, broker = await _ledger(root)
    if tamper == "delete_row":
        _sql(root, "DELETE FROM inference_cost_reservations WHERE operation_id = 'op-a'")
    elif tamper == "insert_row":
        _sql(root, "INSERT INTO inference_cost_reservations SELECT 'op-forged', deployment_id, job_id, owner_id, goal_id, goal_revision, "
             "payload_digest, policy_digest, runtime_path, profile_id, period_id, settings_revision, ceiling_microusd, bound_microusd, "
             "owner_ceiling_microusd, sequence, priority, deadline_at, state, job_fencing_token, contact_started_at, actual_cost_microusd, "
             "provider_operation_id, evidence_json, group_lookup_key, recovery_reason, revision, created_at, updated_at, ledger_row_digest "
             "FROM inference_cost_reservations WHERE operation_id = 'op-a'")
    elif tamper == "row_digest_column":
        _sql(root, "UPDATE inference_cost_reservations SET ledger_row_digest = ? WHERE operation_id = 'op-a'", "0" * 64)
    elif tamper == "account_totals":
        totals = json.loads(_sql(root, "SELECT ledger_totals_json FROM inference_accounting_owners")[0][0])
        totals["settled_by_period"] = {key: 0 for key in totals["settled_by_period"]}
        _sql(root, "UPDATE inference_accounting_owners SET ledger_totals_json = ?", json.dumps(totals, sort_keys=True))
    elif tamper == "consistent_row_and_digest":
        _sql(root, "UPDATE inference_cost_reservations SET actual_cost_microusd = 1 WHERE operation_id = 'op-a'")
        connection = sqlite3.connect(root / "seraph.db")
        connection.row_factory = sqlite3.Row
        row = dict(connection.execute("SELECT * FROM inference_cost_reservations WHERE operation_id = 'op-a'").fetchone())
        connection.execute("UPDATE inference_cost_reservations SET ledger_row_digest = ? WHERE operation_id = 'op-a'", (codec.row_digest(row),))
        connection.commit()
        connection.close()
    else:
        _sql(root, "UPDATE inference_accounting_owners SET ceiling_microusd = 1000000")
    _assert_immediately_blocked(await repository.inference_accounting_snapshot(), await _admission(broker))


@pytest.mark.asyncio
async def test_witness_rollback_and_database_swap_block_the_next_operation(accounting_db, monkeypatch):
    monkeypatch.setattr(inference_ledger, "SHADOW_FULL_LEDGER", False)  # the oracle assumes no tampering
    root, engine, _factory = accounting_db
    repository, broker = await _ledger(root, operations=(("op-a", "0.000020"),))
    await engine.dispose()
    shutil.copy2(root / "seraph.db", root.parent / "older.db")
    workspace = ProductionWorkspace(host_root=root)
    older_receipt = read_lifecycle_receipt(workspace)
    await broker.execute(request("op-b", owner="service:op-b"), await _charge("0.000020"))
    await engine.dispose()
    # The receipt writer refuses regression; write the old bytes directly.
    path = lifecycle_receipt_path(workspace)
    current_bytes = path.read_bytes()
    path.write_text(json.dumps(older_receipt, sort_keys=True, separators=(",", ":")))
    _assert_immediately_blocked(await repository.inference_accounting_snapshot(), await _admission(broker, "op-c"))
    path.write_bytes(current_bytes)
    await engine.dispose()
    shutil.copy2(root.parent / "older.db", root / "seraph.db")
    _assert_immediately_blocked(await repository.inference_accounting_snapshot(), await _admission(broker, "op-d"))


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["lower_settled_actual", "settled_to_released", "older_period",
                                    "overrun_actual_lowered", "overrun_bound_raised"])
async def test_content_edits_to_untouched_rows_never_change_admission_and_fail_full_verification(accounting_db, monkeypatch, tamper):
    """R1: editing history cannot free capacity or clear the overrun latch (ADR-006)."""
    root, _engine, _factory = accounting_db
    overrun = tamper.startswith("overrun")
    # 128 fillers keep the tampered row out of the snapshot's re-checked list,
    # so only the sealed totals stand between the edit and admission.
    operations = (("op-b", "0.000150"),) if overrun else (("op-c", "0.000090"),)
    repository, broker = await _ledger(root, ceiling=300, operations=operations, fillers=128)
    monkeypatch.setattr(inference_ledger, "SHADOW_FULL_LEDGER", False)  # the oracle assumes no tampering
    expected = "provider_charge_exceeded_reservation" if overrun else "deployment_cost_budget_exhausted"
    assert expected in await _admission(broker, "op-before")
    before = await repository.inference_accounting_snapshot()
    edits = {
        "lower_settled_actual": "UPDATE inference_cost_reservations SET actual_cost_microusd = 0 WHERE operation_id = 'op-c'",
        "settled_to_released": "UPDATE inference_cost_reservations SET state = 'released' WHERE operation_id = 'op-c'",
        "older_period": "UPDATE inference_cost_reservations SET period_id = '2000-01' WHERE operation_id = 'op-c'",
        "overrun_actual_lowered": "UPDATE inference_cost_reservations SET actual_cost_microusd = 10 WHERE operation_id = 'op-b'",
        "overrun_bound_raised": "UPDATE inference_cost_reservations SET bound_microusd = 1000 WHERE operation_id = 'op-b'",
    }
    _sql(root, edits[tamper])
    # Status and admission read only the sealed totals: capacity and the
    # overrun latch are exactly as before the edit.
    during = await repository.inference_accounting_snapshot()
    for key in ("status", "committed_microusd", "reserved_microusd", "remaining_microusd", "reason_code", "overrun_max_cost_microusd"):
        assert during[key] == before[key], key
    assert expected in await _admission(broker, "op-during")
    # Full verification (background verifier, then the in-lock re-check)
    # detects the edit and latches accounting for this process.
    monkeypatch.setattr("config.settings.settings.workspace_dir", str(root))
    result = await repository.verify_inference_ledger_in_background()
    assert result["status"] == "latched", result
    _assert_immediately_blocked(await repository.inference_accounting_snapshot(), await _admission(broker, "op-after"))


@pytest.mark.asyncio
async def test_listed_row_edits_are_caught_by_the_status_read(accounting_db, monkeypatch):
    """Rows a snapshot returns (open rows first, up to 128) are re-checked on every read."""
    root, _engine, _factory = accounting_db
    repository, broker = await _ledger(root, operations=())
    monkeypatch.setattr(inference_ledger, "SHADOW_FULL_LEDGER", False)
    await broker._prepare_accounting(request("op-open", owner="service:op-open"))
    _sql(root, "UPDATE inference_cost_reservations SET state = 'released' WHERE operation_id = 'op-open'")
    _assert_immediately_blocked(await repository.inference_accounting_snapshot(), await _admission(broker))


@pytest.mark.asyncio
async def test_stale_full_verification_runs_inline_at_admission(accounting_db, monkeypatch):
    """R7: past 2× the interval, admission verifies in full before admitting."""
    root, _engine, _factory = accounting_db
    repository, broker = await _ledger(root, ceiling=10_000, operations=(("op-a", "0.000020"),), fillers=128)
    monkeypatch.setattr(inference_ledger, "SHADOW_FULL_LEDGER", False)
    _sql(root, "UPDATE inference_cost_reservations SET actual_cost_microusd = 0 WHERE operation_id = 'op-a'")
    assert await _admission(broker, "op-fresh") == "admitted"  # within the window: fast path
    clock = inference_ledger.time.monotonic() + inference_ledger._STALE_AFTER_SECONDS + 1
    monkeypatch.setattr(inference_ledger.time, "monotonic", lambda: clock)
    assert "accounting_continuity_unavailable" in await _admission(broker, "op-stale")


@pytest.mark.asyncio
async def test_touched_row_is_verified_before_it_is_resealed(accounting_db, monkeypatch):
    monkeypatch.setattr(inference_ledger, "SHADOW_FULL_LEDGER", False)  # the oracle assumes no tampering
    """R2: a writer never seals tampered content into a fresh digest."""
    root, _engine, _factory = accounting_db
    repository, broker = await _ledger(root, operations=())
    handle = await broker._prepare_accounting(request("op-open", owner="service:op-open"))
    _sql(root, "UPDATE inference_cost_reservations SET bound_microusd = 1 WHERE operation_id = 'op-open'")
    with pytest.raises(Exception, match="accounting_continuity_unavailable"):
        await broker._contact_accounting(handle)
    stored = _sql(root, "SELECT ledger_row_digest, bound_microusd FROM inference_cost_reservations")[0]
    assert stored[1] == 1 and stored[0] != codec.row_digest({**dict(zip(("ledger_row_digest", "bound_microusd"), stored))})


@pytest.mark.asyncio
async def test_background_verifier_ignores_the_witness_and_passes_a_consistent_ledger(accounting_db, monkeypatch):
    """R6: writers persist the witness before commit; only DB consistency counts."""
    root, _engine, _factory = accounting_db
    repository, broker = await _ledger(root)
    workspace = ProductionWorkspace(host_root=root)
    receipt = read_lifecycle_receipt(workspace)
    receipt["inference_accounting"] = {**receipt["inference_accounting"], "revision": receipt["inference_accounting"]["revision"] + 1}
    write_lifecycle_receipt(workspace, receipt)
    monkeypatch.setattr("config.settings.settings.workspace_dir", str(root))
    assert (await repository.verify_inference_ledger_in_background())["status"] == "verified"


@pytest.mark.asyncio
async def test_v1_ledger_migrates_forward_once_with_an_empty_checkpoint(accounting_db):
    """R5: a v1 ledger is fully v1-verified, then sealed as v2 with no row changes."""
    root, engine, _factory = accounting_db
    repository, broker = await _ledger(root)
    await engine.dispose()
    connection = sqlite3.connect(root / "seraph.db")
    connection.row_factory = sqlite3.Row
    account = dict(connection.execute("SELECT * FROM inference_accounting_owners").fetchone())
    rows = [dict(row) for row in connection.execute("SELECT * FROM inference_cost_reservations")]
    account["ledger_totals_json"] = ""
    v1 = codec._v1_ledger_digest(account, rows)
    connection.execute("UPDATE inference_accounting_owners SET ledger_digest = ?, ledger_totals_json = ''", (v1,))
    connection.execute("UPDATE inference_cost_reservations SET ledger_row_digest = NULL")
    connection.commit()
    connection.close()
    workspace = ProductionWorkspace(host_root=root)
    receipt = read_lifecycle_receipt(workspace)
    # Recreate the receipt a v1 build would hold (same revision, v1 digest).
    path = lifecycle_receipt_path(workspace)
    receipt["inference_accounting"] = {**receipt["inference_accounting"], "ledger_digest": v1}
    path.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    revision = account["revision"]
    assert await repository.recover_inference_accounting() == []
    after = _sql(root, "SELECT revision, ledger_digest, ledger_totals_json FROM inference_accounting_owners")[0]
    assert after[0] == revision + 1 and codec.is_v2_digest(after[1])
    assert read_accounting_checkpoint(workspace)["operations"] == []
    assert all(digest is not None for (digest,) in _sql(root, "SELECT ledger_row_digest FROM inference_cost_reservations"))
    snapshot = await repository.inference_accounting_snapshot()
    assert snapshot["committed_microusd"] == 200 and snapshot["reason_code"] == "provider_charge_exceeded_reservation"


@pytest.mark.asyncio
async def test_tampered_v1_ledger_never_migrates(accounting_db):
    root, engine, _factory = accounting_db
    repository, _broker = await _ledger(root, operations=(("op-a", "0.000020"),))
    await engine.dispose()
    _sql(root, "UPDATE inference_accounting_owners SET ledger_digest = ?", "0" * 64)
    _sql(root, "UPDATE inference_cost_reservations SET ledger_row_digest = NULL")
    with pytest.raises(InferenceAccountingError, match="accounting_continuity_unavailable"):
        await repository.recover_inference_accounting()
    assert not codec.is_v2_digest(_sql(root, "SELECT ledger_digest FROM inference_accounting_owners")[0][0])


@pytest.mark.asyncio
async def test_null_row_digests_are_fully_verified_and_backfilled(accounting_db, monkeypatch):
    monkeypatch.setattr(inference_ledger, "SHADOW_FULL_LEDGER", False)  # the oracle assumes no tampering
    root, _engine, _factory = accounting_db
    repository, broker = await _ledger(root, operations=(("op-a", "0.000020"), ("op-c", "0.000030")))
    _sql(root, "UPDATE inference_cost_reservations SET ledger_row_digest = NULL WHERE operation_id = 'op-a'")
    # Restored rows without a stored digest: full verification, then backfill.
    assert await _admission(broker) == "admitted"
    assert _sql(root, "SELECT ledger_row_digest FROM inference_cost_reservations WHERE operation_id = 'op-a'")[0][0] is not None
    # A NULL on a tampered row is not silently trusted.
    _sql(root, "UPDATE inference_cost_reservations SET ledger_row_digest = NULL, actual_cost_microusd = 0 WHERE operation_id = 'op-a'")
    assert "accounting_continuity_unavailable" in await _admission(broker, "op-after")
