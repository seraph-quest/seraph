"""Actual old SQLite migration, indexed reads and canonical writer invariants."""
from tests.general_task_method_lifecycle import native_admission_lifecycle
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest
from sqlalchemy import MetaData, Table, event, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlmodel import SQLModel, select

from src.db.models import InferenceCostReservation as Row, InferenceAccountingOwner
from src.work_board.general_task import digest
from src.workflows.inference_accounting import InferenceAccountingError, _operation_payload, _ledger_digest
from src.workflows.inference_group_lookup import (classify_group_lookup, group_reservation_rows,
    assert_group_lookup, INDEX_NAME)
from tests.test_general_task_specialist_accounting import group as original_group, entry as specialist_entry
from tests.test_inference_accounting import accounting_db, setup_configuration, request
from tests.test_general_task_persistence import task_runtime
from tests.test_work_board_m6_provider_free_journey import isolated_runtime


def reservation(group, ordinal=1, *, owner=None, evidence=None):
    identifier = f"planning-specialist:{ordinal:04}"
    entry = specialist_entry(group)
    entry.update(call_ordinal=ordinal, original_operation_id=identifier,
        original_job_id=f"inference:{ordinal:04}")
    return Row(operation_id=identifier, deployment_id="fixture", job_id=entry["original_job_id"],
        owner_id=owner or group.owner_principal_id, goal_id=group.goal_id, goal_revision=group.goal_revision,
        payload_digest="a" * 64, policy_digest="b" * 64, runtime_path="general_task_planner",
        profile_id="fixture", period_id="2026-10", settings_revision=1, ceiling_microusd=1000,
        bound_microusd=100, sequence=ordinal, priority=1, deadline_at=group.original_deadline_at.replace(tzinfo=None),
        job_fencing_token=1, evidence_json=evidence or json.dumps([{"kind":"reservation"}, entry]))


@pytest.mark.parametrize("later_corruption", [False, True])
def test_accounting_continuity_validates_all_original_evidence_before_lookup_drift(later_corruption):
    from src.workflows.job_runtime import DurableJobRepository
    group = original_group()
    rows = [reservation(group, ordinal) for ordinal in (1, 2)]
    for row in rows:
        row.group_lookup_key = classify_group_lookup(row)
    # The first row's evidence is valid, but its derived projection has drifted.
    # It must not mask a later row's original evidence failure.
    rows[0].group_lookup_key = "none"
    if later_corruption:
        entries = json.loads(rows[1].evidence_json)
        entries[1]["original_job_id"] = "foreign-original-job"
        rows[1].evidence_json = json.dumps(entries)
    before = [row.model_dump(mode="json") for row in rows]
    expected = "general_task_group_evidence_invalid" if later_corruption else "general_task_group_lookup_invalid"
    with pytest.raises(InferenceAccountingError) as denied:
        DurableJobRepository()._assert_accounting_continuity(None, None, rows)
    assert str(denied.value) == expected
    assert [row.model_dump(mode="json") for row in rows] == before
    assert all(row.contact_started_at is None and row.bound_microusd == 100 for row in rows)


@pytest.mark.asyncio
async def test_populated_old_migration_rerun_preserves_all_financial_bytes_and_seeks(tmp_path, monkeypatch):
    from src.db import engine as db_owner
    from config.settings import settings
    group = original_group()
    rows = [reservation(group)]
    entry = specialist_entry(group)
    reordered = reservation(group, 2)
    own_entry = json.loads(reordered.evidence_json)[1]
    reordered.evidence_json = json.dumps([own_entry, {"kind":"reservation"}]).replace("general_task_group", "general_task_\\u0067roup")
    rows.append(reordered)
    for ordinal, evidence in enumerate([
        '[{"kind":"reservation"}]',
        '[{"kind":"research_group_reservation.v1","group_id":"research"}]',
        json.dumps([entry, entry]),
        '[{"kind":"general_task_group_reservation.v2"}]',
        '[{"kind":"reservation","k\\u0069nd":"reservation"}]',
        '{"kind":"reservation"}', '[null]', '[',
    ], 3):
        rows.append(reservation(group, ordinal, evidence=evidence))
    for ordinal in range(20, 280):
        rows.append(reservation(group, ordinal, owner="unrelated-owner", evidence='[{"kind":"reservation"}]'))
    root = tmp_path / "workspace"
    root.mkdir()
    engine = create_async_engine(f"sqlite+aiosqlite:///{root / 'seraph.db'}")
    old_metadata = MetaData()
    old_table = Table(Row.__tablename__, old_metadata,
        *(column._copy() for column in Row.__table__.columns if column.name != "group_lookup_key"))
    old_columns = list(old_table.columns.keys())
    account = InferenceAccountingOwner(deployment_id="fixture", ceiling_microusd=1000)
    async with engine.begin() as conn:
        await conn.run_sync(old_metadata.create_all)
        for row in rows:
            await conn.execute(old_table.insert().values(**{name:getattr(row,name) for name in old_columns}))
        # Compare the actual persisted old SQLite representation: SQLite has
        # already normalized the writer's UTC datetimes to naive values.
        old_rows = [Row(**dict(row)) for row in (await conn.execute(old_table.select().order_by(old_table.c.operation_id))).mappings()]
        before_digest = _ledger_digest(account, old_rows)
        before_payload = json.dumps([_operation_payload(row) for row in old_rows], sort_keys=True)
        before = (await conn.execute(text("SELECT " + ",".join(old_columns) + " FROM inference_cost_reservations ORDER BY operation_id"))).all()
    monkeypatch.setattr(db_owner, "engine", engine)
    monkeypatch.setattr(db_owner, "_db_path", str(root / "seraph.db"))
    monkeypatch.setattr(settings, "workspace_dir", str(root))
    await db_owner.init_db()
    await db_owner.init_db()
    async with AsyncSession(engine) as db:
        migrated = list((await db.execute(select(Row).order_by(Row.operation_id))).scalars())
        expected = ["g:" + group.group_id, "g:" + group.group_id, "none", "none"] + ["invalid"] * 6
        assert [row.group_lookup_key for row in migrated[:10]] == expected
        assert all(row.group_lookup_key == "none" for row in migrated[10:])
        assert _ledger_digest(account, migrated) == before_digest
        assert json.dumps([_operation_payload(row) for row in migrated], sort_keys=True) == before_payload
        after = (await db.execute(text("SELECT " + ",".join(old_columns) + " FROM inference_cost_reservations ORDER BY operation_id"))).all()
        assert after == before
        assert all("group_lookup_key" not in row.model_dump(mode="json") for row in migrated)
        for key in (None, "invalid", "g:" + group.group_id):
            predicate = "group_lookup_key IS NULL" if key is None else "group_lookup_key = :key"
            plan = (await db.execute(text("EXPLAIN QUERY PLAN SELECT * FROM inference_cost_reservations "
                "WHERE owner_id = :owner AND " + predicate + " LIMIT 13"),
                {"owner":group.owner_principal_id, "key":key})).all()
            assert any("SEARCH" in row[3] and INDEX_NAME in row[3] for row in plan), plan
    await engine.dispose()


@pytest.mark.asyncio
async def test_failed_serialized_migration_rolls_back_column_and_financial_rows(tmp_path, monkeypatch):
    from src.db import engine as db_owner
    from src.workflows import inference_group_lookup as lookup
    from config.settings import settings
    group = original_group()
    root = tmp_path / "workspace"
    root.mkdir()
    engine = create_async_engine(f"sqlite+aiosqlite:///{root / 'seraph.db'}")
    metadata = MetaData()
    old = Table(Row.__tablename__, metadata,
        *(column._copy() for column in Row.__table__.columns if column.name != "group_lookup_key"))
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
        for ordinal in (1, 2):
            row = reservation(group, ordinal)
            await conn.execute(old.insert().values(**{name:getattr(row,name) for name in old.columns.keys()}))
        before = (await conn.execute(old.select().order_by(old.c.operation_id))).all()
    monkeypatch.setattr(db_owner, "engine", engine)
    monkeypatch.setattr(db_owner, "_db_path", str(root / 'seraph.db'))
    monkeypatch.setattr(settings, "workspace_dir", str(root))
    original = lookup.classify_group_lookup
    calls = []
    def fail_second(row):
        calls.append(row.operation_id)
        if len(calls) == 2:
            raise RuntimeError("injected classification interruption")
        return original(row)
    monkeypatch.setattr(lookup, "classify_group_lookup", fail_second)
    with pytest.raises(RuntimeError, match="classification interruption"):
        await db_owner.init_db()
    async with engine.begin() as conn:
        assert "group_lookup_key" not in {row[1] for row in (await conn.exec_driver_sql("PRAGMA table_info(inference_cost_reservations)")).all()}
        assert (await conn.execute(old.select().order_by(old.c.operation_id))).all() == before
    monkeypatch.setattr(lookup, "classify_group_lookup", original)
    await db_owner.init_db()
    async with AsyncSession(engine) as db:
        assert all(row.group_lookup_key == "g:" + group.group_id for row in (await db.execute(select(Row))).scalars())
    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("blocker", [None, "invalid", "overflow", "ordinal", "foreign_group", "deadline", "unknown_version"])
async def test_indexed_inventory_blocks_ambiguity_and_keeps_unknown_released(tmp_path, blocker):
    group = original_group()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'rows.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    statements = []
    event.listen(engine.sync_engine, "before_cursor_execute",
        lambda conn, cursor, statement, parameters, context, many: statements.append(statement))
    async with AsyncSession(engine, expire_on_commit=False) as db:
        count = 13 if blocker == "overflow" else 2
        rows = [reservation(group, ordinal) for ordinal in range(1, count + 1)]
        for row in rows:
            row.group_lookup_key = classify_group_lookup(row)
        rows[0].state = "unknown"
        rows[1].state = "released"
        for ordinal in range(30, 150):
            unrelated = reservation(group, ordinal, evidence='[{"kind":"reservation"}]')
            unrelated.group_lookup_key = "none"
            db.add(unrelated)
        if blocker in (None, "invalid"):
            rows[1].group_lookup_key = blocker
        elif blocker in ("ordinal", "foreign_group", "unknown_version"):
            entries = json.loads(rows[1].evidence_json)
            if blocker == "ordinal":
                entries[1]["call_ordinal"] = 1
            elif blocker == "foreign_group":
                entries[1]["group"]["group_id"] = "f" * 64
                entries[1]["group_digest"] = digest(entries[1]["group"])
            else:
                entries[1]["kind"] = "general_task_group_reservation.v2"
            rows[1].evidence_json = json.dumps(entries)
        elif blocker == "deadline":
            rows[1].deadline_at += timedelta(seconds=1)
        db.add_all(rows)
        await db.commit()
        statements.clear()
        with pytest.raises(InferenceAccountingError, match="lookup_invalid"):
            await group_reservation_rows(db, owner_id=group.owner_principal_id, group_id=group.group_id,
                group_digest=digest(group.model_dump(mode="json")), original_root_id=group.owner_session_id,
                original_deadline_at=group.original_deadline_at)
        selects = [statement for statement in statements if statement.startswith("SELECT")]
        assert len(selects) <= 3 and all("LIMIT" in statement and "group_lookup_key" in statement for statement in selects)
        # Repair only this disposable negative fixture; unknown/released remain selected.
        if blocker != "overflow":
            fixed = reservation(group, 2)
            rows[1].evidence_json, rows[1].deadline_at = fixed.evidence_json, fixed.deadline_at
            rows[1].group_lookup_key = "g:" + group.group_id
            await db.commit()
            selected = await group_reservation_rows(db, owner_id=group.owner_principal_id, group_id=group.group_id,
                group_digest=digest(group.model_dump(mode="json")), original_root_id=group.owner_session_id,
                original_deadline_at=group.original_deadline_at)
            assert [row.state for row in selected] == ["unknown", "released"]
    await engine.dispose()


@pytest.mark.asyncio
async def test_real_canonical_writer_contact_unknown_settlement_and_snapshot_excludes_projection(accounting_db):
    from src.workflows.job_runtime import DurableJobRepository
    from src.model_fabric.remote_inference_admission import RemoteInferenceAdmissionBroker
    root, engine, factory = accounting_db
    setup_configuration()
    owner = DurableJobRepository()
    await owner.configure_inference_accounting(1000)
    async def scripted_transport():
        return {"id":"gen-private-lookup", "usage":{"prompt_tokens":1}}
    await RemoteInferenceAdmissionBroker(durable_accounting=True).execute(request("projection-unknown"), scripted_transport)
    snapshot = await owner.inference_accounting_snapshot()
    operation = snapshot["operations"][0]
    assert operation["state"] == "unknown" and "group_lookup_key" not in operation
    async with factory() as db:
        row = await db.get(Row, operation["operation_id"])
        assert row.group_lookup_key == "none"
        assert_group_lookup(row)
        assert len(json.loads(row.evidence_json)) >= 3
    await owner.settle_inference_cost(operation["operation_id"], job_id=operation["job_id"],
        expected_revision=operation["revision"], actual_cost_microusd=1, evidence_digest="d" * 64,
        operator_id="operator:fixture", idempotency_key="projection-settle")
    await owner.settle_inference_cost(operation["operation_id"], job_id=operation["job_id"],
        expected_revision=operation["revision"], actual_cost_microusd=1, evidence_digest="d" * 64,
        operator_id="operator:fixture", idempotency_key="projection-settle")
    async with factory() as db:
        row = await db.get(Row, operation["operation_id"])
        assert row.state == "settled" and row.group_lookup_key == "none"
        assert_group_lookup(row)
    assert "group_lookup_key" not in json.dumps(await owner.inference_accounting_snapshot())


@pytest.mark.asyncio
@pytest.mark.parametrize("divergence", [False, True])
async def test_actual_group_writer_and_append_keep_projection_or_rollback_before_contact(task_runtime, monkeypatch, divergence, native_admission_lifecycle):
    from tests.test_general_task_specialist_planning import specialist_fixture
    from src.workflows.job_runtime import DurableJobRepository
    from src.workspace.production import ProductionWorkspace, lifecycle_receipt_path
    from config.settings import settings
    from pathlib import Path
    sessions, dispatcher, planner, transport, owner, group, binding, inputs, descriptors, provenance = await specialist_fixture(task_runtime, monkeypatch)
    before = await dispatcher.jobs.inference_accounting_snapshot()
    witness_path = lifecycle_receipt_path(ProductionWorkspace(host_root=Path(settings.workspace_dir)))
    before_witness = witness_path.read_bytes()
    persist = DurableJobRepository._persist_accounting_witness
    if divergence:
        from src.work_board.repository import BoardError
        async def changed_evidence(self, db, workspace, account, rows):
            # Exercise a divergence inside the canonical transaction, after
            # the writer derived its projection and before financial adoption.
            for row in rows:
                if row.group_lookup_key == "g:" + group.group_id:
                    evidence = json.loads(row.evidence_json)
                    evidence.append({"kind":"general_task_group_reservation.v999"})
                    row.evidence_json = json.dumps(evidence)
            return await persist(self, db, workspace, account, rows)
        monkeypatch.setattr(DurableJobRepository, "_persist_accounting_witness", changed_evidence)
        with pytest.raises(BoardError, match="lookup_invalid") as denied:
            async with sessions() as db:
                await planner.propose_specialist(db, owner, group=group, binding=binding,
                    task_input=inputs, descriptors=descriptors, original_provenance=provenance, request_key="projection")
        assert denied.value.code == "general_task_planning_admission_blocked" and denied.value.status_code == 409
        assert transport["contacts"] == []
        assert await dispatcher.jobs.inference_accounting_snapshot() == before
        assert witness_path.read_bytes() == before_witness
        async with sessions() as db:
            assert list((await db.execute(select(Row))).scalars()) == []
    else:
        async with sessions() as db:
            await planner.propose_specialist(db, owner, group=group, binding=binding,
                task_input=inputs, descriptors=descriptors, original_provenance=provenance, request_key="projection")
        assert len(transport["contacts"]) == 1
        async with sessions() as db:
            rows = await group_reservation_rows(db, owner_id=group.owner_principal_id, group_id=group.group_id,
                group_digest=digest(group.model_dump(mode="json")), original_root_id=group.owner_session_id,
                original_deadline_at=group.original_deadline_at, group=group)
            assert len(rows) == 1 and rows[0].state == "settled"
            assert rows[0].group_lookup_key == "g:" + group.group_id
            assert_group_lookup(rows[0])
            assert any(entry["kind"] == "provider_contact_started" for entry in json.loads(rows[0].evidence_json))


@pytest.mark.asyncio
@pytest.mark.parametrize("source_new,target_new,extra_target", [(False,False,False), (False,True,False), (True,False,False), (True,True,False), (True,True,True)])
async def test_actual_retention_old_new_columns_preserves_financial_wire_and_blocks_null_until_startup(accounting_db, source_new, target_new, extra_target):
    from src.workflows.job_runtime import DurableJobRepository
    from src.model_fabric.remote_inference_admission import RemoteInferenceAdmissionBroker
    from src.workspace.accounting_continuity import retain_inference_accounting, verify_accounting_generation
    from src.workspace.production import ProductionWorkspace, maintenance_fence, lifecycle_receipt_path, read_lifecycle_receipt
    from src.db.engine import _ensure_inference_group_lookup
    from src.workspace.accounting_witness import ledger_record
    root, engine, factory = accounting_db
    setup_configuration()
    repository = DurableJobRepository()
    await repository.configure_inference_accounting(1000)
    async def unknown_transport():
        return {"id":"gen-retained", "usage":{"prompt_tokens":1}}
    await RemoteInferenceAdmissionBroker(durable_accounting=True).execute(request("retention-private-projection"), unknown_transport)
    workspace = ProductionWorkspace(host_root=root)
    expected = read_lifecycle_receipt(workspace)["inference_accounting"]
    witness_before = lifecycle_receipt_path(workspace).read_bytes()
    await engine.dispose()
    target = root.parent / "retained-target"
    target.mkdir()
    with sqlite3.connect(root / "seraph.db") as source, sqlite3.connect(target / "seraph.db") as destination:
        source.backup(destination)
    def old_schema(database):
        with sqlite3.connect(database) as db:
            db.execute(f"DROP INDEX {INDEX_NAME}")
            db.execute("ALTER TABLE inference_cost_reservations DROP COLUMN group_lookup_key")
    if not source_new:
        old_schema(root / "seraph.db")
    if not target_new:
        old_schema(target / "seraph.db")
    before_account, before_rows = verify_accounting_generation(root / "seraph.db", expected)
    if extra_target:
        from src.workspace.production import ProductionWorkspaceReconciliationError
        with sqlite3.connect(target / "seraph.db") as db:
            db.execute("ALTER TABLE inference_cost_reservations ADD COLUMN unsupported_extra VARCHAR")
            before = db.execute("SELECT * FROM inference_cost_reservations").fetchall()
        with maintenance_fence(workspace), maintenance_fence(ProductionWorkspace(host_root=target)):
            with pytest.raises(ProductionWorkspaceReconciliationError, match="columns invalid"):
                retain_inference_accounting(active=root, target=target, database_path="seraph.db")
        with sqlite3.connect(target / "seraph.db") as db:
            assert db.execute("SELECT * FROM inference_cost_reservations").fetchall() == before
        assert lifecycle_receipt_path(workspace).read_bytes() == witness_before
        return
    with maintenance_fence(workspace), maintenance_fence(ProductionWorkspace(host_root=target)):
        result = retain_inference_accounting(active=root, target=target, database_path="seraph.db")
    assert result["status"] == "retained_latest"
    after_account, after_rows = verify_accounting_generation(target / "seraph.db", expected)
    assert after_account == before_account and after_rows == before_rows
    assert lifecycle_receipt_path(workspace).read_bytes() == witness_before
    assert all("group_lookup_key" not in ledger_record(row) for row in after_rows)
    target_engine = create_async_engine(f"sqlite+aiosqlite:///{target / 'seraph.db'}")
    if target_new:
        async with AsyncSession(target_engine) as db:
            selected = list((await db.execute(select(Row))).scalars())
            assert len(selected) == 1 and selected[0].group_lookup_key is None
            with pytest.raises(InferenceAccountingError, match="lookup_invalid"):
                await group_reservation_rows(db, owner_id=selected[0].owner_id, group_id="f" * 64,
                    group_digest="e" * 64, original_root_id="unused", original_deadline_at=datetime.now(timezone.utc))
    async with target_engine.begin() as conn:
        await conn.exec_driver_sql("BEGIN IMMEDIATE")
        await _ensure_inference_group_lookup(conn)
    async with AsyncSession(target_engine) as db:
        selected = list((await db.execute(select(Row))).scalars())
        assert len(selected) == 1 and selected[0].group_lookup_key == "none"
        assert_group_lookup(selected[0])
    assert verify_accounting_generation(target / "seraph.db", expected) == (before_account,before_rows)
    await target_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("old_schema", [False, True])
async def test_actual_interrupted_checkpoint_replay_on_old_and_new_schema_keeps_witness_bytes(accounting_db, monkeypatch, old_schema):
    from src.workflows.job_runtime import DurableJobRepository
    from src.model_fabric.remote_inference_admission import RemoteInferenceAdmissionBroker
    from src.workspace.production import (ProductionWorkspace, maintenance_fence,
        read_accounting_checkpoint, lifecycle_receipt_path, read_lifecycle_receipt)
    from src.workspace.accounting_continuity import reconcile_accounting_checkpoint, verify_accounting_generation
    from src.workspace import canonical_workspace_registry
    from src.db.engine import _ensure_inference_group_lookup
    root, engine, factory = accounting_db
    setup_configuration()
    repository = DurableJobRepository()
    await repository.configure_inference_accounting(1000)
    workspace = ProductionWorkspace(host_root=root)
    original_commit = AsyncSession.commit
    interrupted = []
    async def interrupt_contact(db):
        checkpoint = read_accounting_checkpoint(workspace)
        if not interrupted and checkpoint and any(row["state"] == "contact_started" for row in checkpoint["operations"]):
            interrupted.append(True)
            raise RuntimeError("actual interrupted contact writer")
        await original_commit(db)
    monkeypatch.setattr(AsyncSession, "commit", interrupt_contact)
    contacts = []
    async def forbidden_contact():
        contacts.append(True)
    with pytest.raises(Exception):
        await RemoteInferenceAdmissionBroker(durable_accounting=True).execute(request("checkpoint-private"), forbidden_contact)
    assert interrupted and contacts == []
    await engine.dispose()
    witness_bytes = lifecycle_receipt_path(workspace).read_bytes()
    checkpoint_bytes = (workspace.lifecycle_directory / "accounting-checkpoint.json").read_bytes()
    assert b"group_lookup_key" not in checkpoint_bytes
    if old_schema:
        with sqlite3.connect(root / "seraph.db") as db:
            db.execute(f"DROP INDEX {INDEX_NAME}")
            db.execute("ALTER TABLE inference_cost_reservations DROP COLUMN group_lookup_key")
    with maintenance_fence(workspace):
        result = reconcile_accounting_checkpoint(root=root, registry=canonical_workspace_registry(root))
    assert result["status"] == "reconciled"
    expected = read_lifecycle_receipt(workspace)["inference_accounting"]
    before = verify_accounting_generation(root / "seraph.db", expected)
    assert before[1][0]["state"] == "contact_started"
    assert before[1][0]["bound_microusd"] == 100
    if not old_schema:
        async with factory() as db:
            row = await db.get(Row, before[1][0]["operation_id"])
            assert row.group_lookup_key is None
            with pytest.raises(InferenceAccountingError, match="lookup_invalid"):
                assert_group_lookup(row)
    async with engine.begin() as conn:
        await conn.exec_driver_sql("BEGIN IMMEDIATE")
        await _ensure_inference_group_lookup(conn)
    assert verify_accounting_generation(root / "seraph.db", expected) == before
    assert lifecycle_receipt_path(workspace).read_bytes() == witness_bytes
    assert (workspace.lifecycle_directory / "accounting-checkpoint.json").read_bytes() == checkpoint_bytes
    assert contacts == []
    snapshot = await repository.inference_accounting_snapshot()
    assert snapshot["status"] == "ready" and snapshot["unknown_microusd"] == 100
