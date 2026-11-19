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

# ADR-035: pins historical OpenRouter broker, ledger and settlement mechanics that still settle retained liabilities
pytestmark = pytest.mark.openrouter_legacy


async def _charge(cost):
    async def provider():
        return {"usage": {"cost": cost}} if cost is not None else {"usage": {"prompt_tokens": 1}}
    return provider


async def _ledger(root, *, operations=(("op-a", "0.000020"), ("op-c", "0.000030"), ("op-b", "0.000150")), fillers=0):
    """Settled calls; op-b (last) charges more than the legacy stored 100 µUSD bound.

    ``fillers`` 1 µUSD calls come first, so with 128 of them the named rows
    fall outside the 128 operations a status snapshot lists (and re-checks).
    """
    setup_configuration()
    repository = DurableJobRepository()
    await repository.ensure_inference_ledger()
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
        assert totals["schema_version"] == codec.LEDGER_TOTALS_VERSION == 2 and "overruns" not in totals
        # The displayed figures: settled spend per month and unpriced requests.
        period = rng.choice(["2026-08", "2026-09", "2026-10"])
        spent = sum(row["actual_cost_microusd"] or 0 for row in rows.values()
            if row["state"] == "settled" and row["period_id"] == period)
        assert totals["settled_by_period"].get(period, 0) == spent
        for state in ("reserved", "contact_started", "unknown"):
            assert totals["open_rows_by_state"][state] == sum(row["state"] == state for row in rows.values())


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
    assert json.loads(account["ledger_totals_json"])["schema_version"] == 2
    snapshot = await repository.inference_accounting_snapshot()
    assert snapshot["status"] == "ready" and snapshot["reason_code"] is None
    assert snapshot["spent_microusd"] == 200 and snapshot["operation_count"] == 3


def _assert_immediately_blocked(snapshot_value, admission):
    """The external witness is ahead of the database: never resealed (#1284)."""
    assert snapshot_value["status"] == "unavailable" and snapshot_value["reason_code"] == "accounting_continuity_unavailable"
    assert "accounting_continuity_unavailable" in admission


def _history_gaps(root):
    history = json.loads(_sql(root, "SELECT settings_history_json FROM inference_accounting_owners")[0][0])
    return [entry for entry in history if entry.get("kind") == "history_gap"]


def _assert_sealed_from_content(root):
    """The reseal left a ledger whose digests, totals and witness match its rows."""
    connection = sqlite3.connect(root / "seraph.db")
    connection.row_factory = sqlite3.Row
    try:
        raw = [dict(row) for row in connection.execute("SELECT * FROM inference_cost_reservations")]
        account = dict(connection.execute("SELECT * FROM inference_accounting_owners").fetchone())
    finally:
        connection.close()
    assert all(row["ledger_row_digest"] == codec.row_digest(row) for row in raw)
    assert codec.ledger_digest(account, raw) == account["ledger_digest"]
    assert json.loads(account["ledger_totals_json"]) == codec.ledger_totals(raw)
    assert read_lifecycle_receipt(ProductionWorkspace(host_root=root))["inference_accounting"] == codec.witness(account)
    return raw


def _assert_resealed_and_proceeds(root, snapshot_value, admission):
    """ADR-037 (#1284): a mismatch is display-only; it reseals, records a gap and proceeds."""
    assert snapshot_value["status"] == "ready" and snapshot_value["reason_code"] is None, (snapshot_value["reason_code"], admission)
    assert snapshot_value["history_verified"] is False and snapshot_value["history_gap_since"]
    assert admission == "admitted"
    assert _history_gaps(root) and all(gap["reason"] for gap in _history_gaps(root))
    return _assert_sealed_from_content(root)


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["delete_row", "insert_row", "row_digest_column", "account_totals",
                                    "consistent_row_and_digest", "account_ceiling"])
async def test_structural_tampering_reseals_records_a_history_gap_and_proceeds(accounting_db, monkeypatch, tamper):
    monkeypatch.setattr(inference_ledger, "SHADOW_FULL_LEDGER", False)  # the oracle assumes no tampering
    root, _engine, _factory = accounting_db
    repository, broker = await _ledger(root)
    if tamper == "delete_row":
        _sql(root, "DELETE FROM inference_cost_reservations WHERE operation_id = 'op-a'")
    elif tamper == "insert_row":
        # A copy of op-a under a new id, by column name (the stored row digest
        # is copied too, so it no longer matches the row's content).
        connection = sqlite3.connect(root / "seraph.db")
        connection.row_factory = sqlite3.Row
        row = {**dict(connection.execute("SELECT * FROM inference_cost_reservations WHERE operation_id = 'op-a'").fetchone()),
               "operation_id": "op-forged"}
        connection.execute(f"INSERT INTO inference_cost_reservations ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
                           tuple(row.values()))
        connection.commit()
        connection.close()
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
    assert not _history_gaps(root)
    snapshot = await repository.inference_accounting_snapshot()
    rows = _assert_resealed_and_proceeds(root, snapshot, await _admission(broker))
    # The view is rebuilt from the rows present, nothing more.
    ids = sorted(row["operation_id"] for row in rows)
    expected = {"delete_row": ["op-b", "op-c", "op-next"], "insert_row": ["op-a", "op-b", "op-c", "op-forged", "op-next"]}
    assert ids == expected.get(tamper, ["op-a", "op-b", "op-c", "op-next"])
    # The resealed ledger verifies cleanly from then on.
    monkeypatch.setattr("config.settings.settings.workspace_dir", str(root))
    assert (await repository.verify_inference_ledger_in_background())["status"] == "verified"


@pytest.mark.asyncio
async def test_witness_rollback_reseals_but_database_swap_behind_the_witness_blocks(accounting_db, monkeypatch):
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
    # A witness behind the database is spend history only: reseal and proceed.
    _assert_resealed_and_proceeds(root, await repository.inference_accounting_snapshot(), await _admission(broker, "op-c"))
    assert path.read_bytes() != current_bytes
    # A database behind the witness may be missing a started contact: it
    # stays blocked until maintenance (at-most-once), and nothing is resealed.
    await engine.dispose()
    witness_bytes = path.read_bytes()
    shutil.copy2(root.parent / "older.db", root / "seraph.db")
    _assert_immediately_blocked(await repository.inference_accounting_snapshot(), await _admission(broker, "op-d"))
    assert path.read_bytes() == witness_bytes


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["lower_settled_actual", "settled_to_released", "older_period",
                                    "large_charge_actual_lowered", "legacy_bound_raised"])
async def test_content_edits_to_untouched_rows_never_change_the_view_until_full_verification_reseals(accounting_db, monkeypatch, tamper):
    """R1: editing history cannot change what is shown until full verification
    detects it; then the ledger is resealed from content with a recorded gap (#1284)."""
    root, _engine, _factory = accounting_db
    large = tamper in {"large_charge_actual_lowered", "legacy_bound_raised"}
    # 128 fillers keep the tampered row out of the snapshot's re-checked list,
    # so only the sealed totals stand between the edit and the view.
    operations = (("op-b", "0.000150"),) if large else (("op-c", "0.000090"),)
    repository, broker = await _ledger(root, operations=operations, fillers=128)
    monkeypatch.setattr(inference_ledger, "SHADOW_FULL_LEDGER", False)  # the oracle assumes no tampering
    assert await _admission(broker, "op-before") == "admitted"
    before = await repository.inference_accounting_snapshot()
    edits = {
        "lower_settled_actual": "UPDATE inference_cost_reservations SET actual_cost_microusd = 0 WHERE operation_id = 'op-c'",
        "settled_to_released": "UPDATE inference_cost_reservations SET state = 'released' WHERE operation_id = 'op-c'",
        "older_period": "UPDATE inference_cost_reservations SET period_id = '2000-01' WHERE operation_id = 'op-c'",
        "large_charge_actual_lowered": "UPDATE inference_cost_reservations SET actual_cost_microusd = 10 WHERE operation_id = 'op-b'",
        "legacy_bound_raised": "UPDATE inference_cost_reservations SET bound_microusd = 1000 WHERE operation_id = 'op-b'",
    }
    _sql(root, edits[tamper])
    # Status reads only the sealed totals: the shown figures are exactly as
    # before the edit.
    during = await repository.inference_accounting_snapshot()
    for key in ("status", "spent_microusd", "unpriced_requests", "operation_count", "reason_code"):
        assert during[key] == before[key], key
    assert await _admission(broker, "op-during") == "admitted"
    # Full verification (background verifier, then the in-lock re-check)
    # detects the edit and reseals instead of latching.
    monkeypatch.setattr("config.settings.settings.workspace_dir", str(root))
    result = await repository.verify_inference_ledger_in_background()
    assert result["status"] == "resealed", result
    assert [gap["reason"] for gap in _history_gaps(root)] == [result["reason"]]
    _assert_resealed_and_proceeds(root, await repository.inference_accounting_snapshot(), await _admission(broker, "op-after"))
    assert (await repository.verify_inference_ledger_in_background())["status"] == "verified"


@pytest.mark.asyncio
async def test_listed_row_edits_are_caught_by_the_status_read(accounting_db, monkeypatch):
    """Rows a snapshot returns (open rows first, up to 128) are re-checked on every read."""
    root, _engine, _factory = accounting_db
    repository, broker = await _ledger(root, operations=())
    monkeypatch.setattr(inference_ledger, "SHADOW_FULL_LEDGER", False)
    await broker._prepare_accounting(request("op-open", owner="service:op-open"))
    _sql(root, "UPDATE inference_cost_reservations SET state = 'released' WHERE operation_id = 'op-open'")
    snapshot = await repository.inference_accounting_snapshot()
    rows = _assert_resealed_and_proceeds(root, snapshot, await _admission(broker))
    assert [row["state"] for row in rows if row["operation_id"] == "op-open"] == ["released"]


@pytest.mark.asyncio
async def test_stale_full_verification_runs_inline_at_admission(accounting_db, monkeypatch):
    """R7: past 2× the interval, admission verifies in full (and reseals) before admitting."""
    root, _engine, _factory = accounting_db
    repository, broker = await _ledger(root, operations=(("op-a", "0.000020"),), fillers=128)
    monkeypatch.setattr(inference_ledger, "SHADOW_FULL_LEDGER", False)
    _sql(root, "UPDATE inference_cost_reservations SET actual_cost_microusd = 0 WHERE operation_id = 'op-a'")
    assert await _admission(broker, "op-fresh") == "admitted"  # within the window: fast path
    clock = inference_ledger.time.monotonic() + inference_ledger._STALE_AFTER_SECONDS + 1
    monkeypatch.setattr(inference_ledger.time, "monotonic", lambda: clock)
    assert not _history_gaps(root)
    assert await _admission(broker, "op-stale") == "admitted"
    assert [gap["reason"] for gap in _history_gaps(root)] == ["ledger_digest_mismatch"]
    _assert_sealed_from_content(root)


@pytest.mark.asyncio
async def test_touched_row_is_verified_before_it_is_resealed(accounting_db, monkeypatch):
    monkeypatch.setattr(inference_ledger, "SHADOW_FULL_LEDGER", False)  # the oracle assumes no tampering
    """R2 (#1284): a writer never seals tampered content silently: it reseals
    the whole ledger from content and records the gap before writing."""
    root, _engine, _factory = accounting_db
    repository, broker = await _ledger(root, operations=())
    handle = await broker._prepare_accounting(request("op-open", owner="service:op-open"))
    _sql(root, "UPDATE inference_cost_reservations SET bound_microusd = 1 WHERE operation_id = 'op-open'")
    await broker._contact_accounting(handle)
    assert [gap["reason"] for gap in _history_gaps(root)] == ["ledger_row_digest_mismatch"]
    rows = _assert_sealed_from_content(root)
    assert [(row["state"], row["bound_microusd"]) for row in rows] == [("contact_started", 1)]


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
    assert json.loads(after[2])["schema_version"] == 2
    snapshot = await repository.inference_accounting_snapshot()
    assert snapshot["spent_microusd"] == 200 and snapshot["reason_code"] is None


@pytest.mark.asyncio
async def test_tampered_v1_ledger_reseals_as_v2_with_a_history_gap(accounting_db):
    root, engine, _factory = accounting_db
    repository, _broker = await _ledger(root, operations=(("op-a", "0.000020"),))
    await engine.dispose()
    _sql(root, "UPDATE inference_accounting_owners SET ledger_digest = ?", "0" * 64)
    _sql(root, "UPDATE inference_cost_reservations SET ledger_row_digest = NULL")
    await repository.recover_inference_accounting()
    assert codec.is_v2_digest(_sql(root, "SELECT ledger_digest FROM inference_accounting_owners")[0][0])
    assert _history_gaps(root)
    _assert_sealed_from_content(root)


@pytest.mark.asyncio
async def test_null_row_digests_are_fully_verified_and_backfilled(accounting_db, monkeypatch):
    monkeypatch.setattr(inference_ledger, "SHADOW_FULL_LEDGER", False)  # the oracle assumes no tampering
    root, _engine, _factory = accounting_db
    repository, broker = await _ledger(root, operations=(("op-a", "0.000020"), ("op-c", "0.000030")))
    _sql(root, "UPDATE inference_cost_reservations SET ledger_row_digest = NULL WHERE operation_id = 'op-a'")
    # Restored rows without a stored digest: full verification, then backfill.
    assert await _admission(broker) == "admitted"
    assert _sql(root, "SELECT ledger_row_digest FROM inference_cost_reservations WHERE operation_id = 'op-a'")[0][0] is not None
    assert not _history_gaps(root)
    # A NULL on a tampered row is not silently trusted: it is resealed with a gap.
    _sql(root, "UPDATE inference_cost_reservations SET ledger_row_digest = NULL, actual_cost_microusd = 0 WHERE operation_id = 'op-a'")
    assert await _admission(broker, "op-after") == "admitted"
    assert _history_gaps(root)
    _assert_sealed_from_content(root)


# --- review P2: scoped reads cannot be emptied by editing untouched rows ----

def _seed_rows(root, rows):
    """Append rows as a canonical writer would: digests, sealed totals, witness."""
    connection = sqlite3.connect(root / "seraph.db")
    connection.row_factory = sqlite3.Row
    template = dict(connection.execute("SELECT * FROM inference_cost_reservations LIMIT 1").fetchone())
    for overrides in rows:
        row = {**template, **overrides}
        row["ledger_row_digest"] = codec.row_digest(row)
        connection.execute(f"INSERT INTO inference_cost_reservations ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
            tuple(row.values()))
    account = dict(connection.execute("SELECT * FROM inference_accounting_owners").fetchone())
    every = [dict(item) for item in connection.execute("SELECT * FROM inference_cost_reservations")]
    account["ledger_totals_json"] = json.dumps(codec.ledger_totals(every), sort_keys=True, separators=(",", ":"))
    account["revision"] += 1
    account["ledger_digest"] = codec.ledger_digest(account, every)
    connection.execute("UPDATE inference_accounting_owners SET ledger_totals_json=?, revision=?, ledger_digest=?",
        (account["ledger_totals_json"], account["revision"], account["ledger_digest"]))
    connection.commit()
    connection.close()
    workspace = ProductionWorkspace(host_root=root)
    receipt = read_lifecycle_receipt(workspace)
    receipt["inference_accounting"] = codec.witness(account)
    write_lifecycle_receipt(workspace, receipt)


async def _scoped(repository, root, call):
    from config.settings import settings
    from pathlib import Path
    from src.db.models import InferenceAccountingOwner
    from src.workflows.inference_accounting import _continuity_lock
    async with repository._session() as db:
        await repository._accounting_begin(db)
        account = await db.get(InferenceAccountingOwner, "deployment")
        with _continuity_lock(Path(settings.workspace_dir).resolve()) as workspace:
            ledger = await repository._open_ledger(db, workspace, account)
            return await call(db, ledger)


def _group_members(original, ordinals, *, group_id=None):
    """Seedable rows carrying real, valid group reservation evidence."""
    from tests.test_inference_group_lookup import reservation
    members = []
    for ordinal in ordinals:
        row = reservation(original, ordinal)
        members.append({"operation_id": row.operation_id, "job_id": row.job_id, "owner_id": row.owner_id,
            "goal_id": row.goal_id, "goal_revision": row.goal_revision, "evidence_json": row.evidence_json,
            "deadline_at": str(row.deadline_at), "group_lookup_key": "g:" + original.group_id})
    return members


async def _group_ids(repository, root, group_id):
    rows = await _scoped(repository, root, lambda db, ledger: repository._group_rows(db, ledger, group_id))
    return sorted(row.operation_id for row in rows)


@pytest.mark.asyncio
async def test_hidden_group_member_is_found_again_not_under_counted(accounting_db, monkeypatch):
    from tests.test_general_task_specialist_accounting import group as original_group
    root, _engine, _factory = accounting_db
    repository, _broker = await _ledger(root, operations=(("op-a", "0.000020"),))
    monkeypatch.setattr(inference_ledger, "SHADOW_FULL_LEDGER", False)
    original = original_group()
    _seed_rows(root, _group_members(original, (1, 2, 3)))
    members = await _group_ids(repository, root, original.group_id)
    assert len(members) == 3 and not _history_gaps(root)
    # The lookup key is unhashed: moving a member out of the group's key must
    # not let the group's call cap see two rows. The read reseals (keys
    # rederived from sealed evidence) and still returns every member (#1284).
    _sql(root, "UPDATE inference_cost_reservations SET group_lookup_key = ? WHERE operation_id = ?", "g:" + "b" * 64, members[0])
    assert await _group_ids(repository, root, original.group_id) == members
    assert [gap["reason"] for gap in _history_gaps(root)] == ["ledger_group_count_mismatch"]
    assert _sql(root, "SELECT group_lookup_key FROM inference_cost_reservations WHERE operation_id = ?", members[0])[0][0] == \
        "g:" + original.group_id


@pytest.mark.asyncio
async def test_swapped_group_member_is_not_counted_in_its_place(accounting_db, monkeypatch):
    from tests.test_general_task_specialist_accounting import group as original_group
    root, _engine, _factory = accounting_db
    repository, _broker = await _ledger(root, operations=(("op-a", "0.000020"),))
    monkeypatch.setattr(inference_ledger, "SHADOW_FULL_LEDGER", False)
    from src.work_board.contracts import WorkBoardOwner
    from src.work_board.general_task_proposal import new_group
    from tests.test_general_task_planner import descriptor, task_input
    original = original_group()
    other = new_group(WorkBoardOwner(principal_id="operator:fixture", session_id="original-root"),
        task_input(), [descriptor()], goal_revision=1, request_key="other-intent",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10))
    assert original.group_id != other.group_id
    outsider = _group_members(other, (9,))
    _seed_rows(root, _group_members(original, (1, 2)) + outsider)
    members = await _group_ids(repository, root, original.group_id)
    # Move a member out and a non-member in: the count is unchanged.
    _sql(root, "UPDATE inference_cost_reservations SET group_lookup_key = ? WHERE operation_id = ?", "g:" + other.group_id, members[0])
    _sql(root, "UPDATE inference_cost_reservations SET group_lookup_key = ? WHERE operation_id = ?",
         "g:" + original.group_id, outsider[0]["operation_id"])
    # The read reseals from sealed evidence: the real members, never the outsider.
    assert await _group_ids(repository, root, original.group_id) == members
    assert await _group_ids(repository, root, other.group_id) == [outsider[0]["operation_id"]]
    assert _history_gaps(root)


@pytest.mark.asyncio
async def test_invalid_group_evidence_still_refuses_the_group_read(accounting_db, monkeypatch):
    """Group evidence guards call counts and lineage, not money: a reseal never accepts invalid evidence."""
    from tests.test_general_task_specialist_accounting import group as original_group
    root, _engine, _factory = accounting_db
    repository, _broker = await _ledger(root, operations=(("op-a", "0.000020"),))
    monkeypatch.setattr(inference_ledger, "SHADOW_FULL_LEDGER", False)
    original = original_group()
    _seed_rows(root, _group_members(original, (1, 2)))
    members = await _group_ids(repository, root, original.group_id)
    entries = json.loads(_sql(root, "SELECT evidence_json FROM inference_cost_reservations WHERE operation_id = ?", members[0])[0][0])
    entries[1]["original_job_id"] = "foreign-original-job"
    _sql(root, "UPDATE inference_cost_reservations SET evidence_json = ? WHERE operation_id = ?", json.dumps(entries), members[0])
    witness = read_lifecycle_receipt(ProductionWorkspace(host_root=root))["inference_accounting"]
    with pytest.raises(InferenceAccountingError, match="general_task_group_evidence_invalid"):
        await _group_ids(repository, root, original.group_id)
    # Nothing was resealed over the invalid evidence.
    assert read_lifecycle_receipt(ProductionWorkspace(host_root=root))["inference_accounting"] == witness


@pytest.mark.asyncio
@pytest.mark.parametrize("column", ["owner_id", "job_id"])
async def test_hidden_discovery_row_reseals_and_reads_current_content(accounting_db, monkeypatch, column):
    from types import SimpleNamespace
    from src.work_board.research_parent import DISCOVERY_KIND, DISCOVERY_SERVICE
    root, _engine, _factory = accounting_db
    repository, _broker = await _ledger(root, operations=(("op-a", "0.000020"),))
    monkeypatch.setattr(inference_ledger, "SHADOW_FULL_LEDGER", False)
    _seed_rows(root, [{"operation_id": f"remote:goal-discovery:x:{index}", "owner_id": DISCOVERY_SERVICE} for index in range(2)])
    run = SimpleNamespace(job_kind=DISCOVERY_KIND)
    found = await _scoped(repository, root, lambda db, ledger: repository._discovery_rows(db, ledger, run))
    assert sorted(row.operation_id for row in found if row.owner_id == DISCOVERY_SERVICE) == [
        "remote:goal-discovery:x:0", "remote:goal-discovery:x:1"]
    value = "service:someone-else" if column == "owner_id" else "job-elsewhere"
    _sql(root, f"UPDATE inference_cost_reservations SET {column} = ? WHERE operation_id = 'remote:goal-discovery:x:0'", value)
    # An owner edit hides the row from the read (sealed count mismatch); a job
    # edit leaves it read with content that no longer matches its digest.
    # Either way the ledger is resealed from content and recorded (#1284).
    found = await _scoped(repository, root, lambda db, ledger: repository._discovery_rows(db, ledger, run))
    owned = sorted(row.operation_id for row in found if row.owner_id == DISCOVERY_SERVICE)
    assert owned == (["remote:goal-discovery:x:1"] if column == "owner_id"
                     else ["remote:goal-discovery:x:0", "remote:goal-discovery:x:1"])
    assert _history_gaps(root)
    _assert_sealed_from_content(root)


@pytest.mark.asyncio
@pytest.mark.parametrize("column, value", [("owner_id", "service:someone-else"), ("state", "released"),
                                           ("runtime_path", "chat_agent")])
async def test_hidden_near_text_reservation_still_counts_as_outstanding(accounting_db, monkeypatch, column, value):
    from src.db.models import InferenceCostReservation
    root, _engine, _factory = accounting_db
    repository, _broker = await _ledger(root, operations=(("op-a", "0.000020"),))
    monkeypatch.setattr(inference_ledger, "SHADOW_FULL_LEDGER", False)
    _seed_rows(root, [{"operation_id": "near-open", "owner_id": "user:near", "runtime_path": "near_text_native",
                       "state": "reserved", "actual_cost_microusd": None}])

    async def outstanding(db, ledger):
        prior = await db.get(InferenceCostReservation, "near-next")
        return repository._near_outstanding_sealed(ledger, prior, "user:near")

    assert await _scoped(repository, root, outstanding) is True
    _sql(root, f"UPDATE inference_cost_reservations SET {column} = ? WHERE operation_id = 'near-open'", value)
    # Content edits to the untouched row change nothing the decision reads.
    assert await _scoped(repository, root, outstanding) is True


def test_v1_ledgers_are_skipped_not_recorded_by_the_background_verifier(tmp_path):
    database = tmp_path / "seraph.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE inference_accounting_owners (deployment_id, revision, ledger_digest)")
    connection.execute("CREATE TABLE inference_cost_reservations (operation_id)")
    connection.execute("INSERT INTO inference_accounting_owners VALUES ('d', 3, ?)", ("0" * 64,))
    connection.commit()
    connection.close()
    assert inference_ledger.verify_offline(database) == ("d", inference_ledger.V1_LEDGER_SKIPPED, 3)
