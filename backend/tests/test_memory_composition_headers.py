"""Actual SQL preflight evidence; no fixture conveys native execution authority."""
from dataclasses import replace
import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel
from src.db.models import Memory, Secret, Goal, WorkBoardTask, WorkBoardEvent
from src.memory.header_bounds import HeaderReadBudget, HeaderBoundsError, SECRET, WRS_BY_RUN
from src.memory.composition_headers import (certify_composition_superset,
    validate_composition_certificate, locate_exact_rows, locate_tombstones, snapshot_reads)
from src.workspace.accounting_witness import _composition_row, composition_row_digest


@pytest_asyncio.fixture
async def model_db(tmp_path):
    engine=create_async_engine("sqlite+aiosqlite:///"+str(tmp_path/"model-schema.sqlite3"))
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    factory=sessionmaker(engine,class_=AsyncSession,expire_on_commit=False)
    async with factory() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        yield db
        await db.rollback()
    await engine.dispose()


@pytest.mark.asyncio
async def test_full_model_superset_secret_real_ids_event_and_aliases(model_db):
    db=model_db
    goal=Goal(title="actual constructor goal")
    task=WorkBoardTask(owner_principal_id="owner",owner_session_id="session",goal_id=goal.id,idempotency_key="event-header")
    a,b=Secret(key="first",encrypted_value="cipher1"),Secret(key="second",encrypted_value="cipher2")
    db.add_all((goal,task,a,b));await db.flush()
    e=WorkBoardEvent(task_id=task.task_id,owner_principal_id="owner",owner_session_id="session",actor_principal_id="owner",actor_session_id="session",kind="actual")
    db.add(e);await db.flush()
    assert type(e.event_id) is int
    budget=HeaderReadBudget()
    secrets=await budget.certify_all(db,SECRET)
    assert set(secrets.row_ids)=={a.id,b.id}
    cert=await certify_composition_superset(db,budget)
    await validate_composition_certificate(db,cert)
    assert ("work_board_events",e.event_id) in cert.rows
    assert len(budget.physical_references)==5
    conn=await db.connection()
    def read(c):
        with snapshot_reads(cert):
            row=_composition_row(c,"work_board_events",e.event_id)
            return row,composition_row_digest("work_board_events",str(e.event_id),row)
    row,digest=await conn.run_sync(read)
    assert row["event_id"]==e.event_id and len(digest)==64
    with pytest.raises(HeaderBoundsError,match="header_certificate_unavailable"):
        await validate_composition_certificate(db,replace(cert))
    await db.execute(text("UPDATE work_board_events SET kind='changed' WHERE event_id=:id"),{"id":e.event_id})
    with pytest.raises(HeaderBoundsError,match="header_certificate_stale"):
        await validate_composition_certificate(db,cert)


@pytest.mark.asyncio
async def test_absence_and_tombstone_locator_are_metadata_only(model_db):
    db=model_db;budget=HeaderReadBudget()
    assert await locate_exact_rows(db,WRS_BY_RUN,"not-existing",budget)==()
    assert await locate_tombstones(db,"not-existing",budget)==()
    assert budget.remaining<1_048_576 and not budget.physical_references


@pytest.mark.asyncio
@pytest.mark.parametrize("change",("generated","unknown-index","unknown-object","objects-overflow"))
async def test_schema_drift_denies_before_private_body(model_db,change):
    db=model_db
    if change=="generated":await db.execute(text("ALTER TABLE memories ADD COLUMN hidden_content TEXT GENERATED ALWAYS AS (content) VIRTUAL"))
    elif change=="unknown-index":await db.execute(text("CREATE INDEX unreviewed_memory_index ON memories(content)"))
    elif change=="unknown-object":await db.execute(text("CREATE VIEW unreviewed_view AS SELECT content FROM memories"))
    else:
        for i in range(1340):await db.execute(text(f"CREATE INDEX excess_{i} ON memories(id)"))
    statements=[];conn=await db.connection()
    def observe(_c,_cursor,statement,_params,_context,_many):statements.append(statement)
    event.listen(conn.sync_connection,"before_cursor_execute",observe)
    try:
        with pytest.raises(HeaderBoundsError):await certify_composition_superset(db,HeaderReadBudget())
    finally:event.remove(conn.sync_connection,"before_cursor_execute",observe)
    assert not any(s.startswith("SELECT memories.") or s.startswith('SELECT "id","content"') for s in statements)
    if change=="generated":assert any("pragma_table_xinfo" in s for s in statements)


@pytest.mark.asyncio
@pytest.mark.parametrize("count,content",((129,"bounded"),(1,"x"*200_000)))
async def test_unrelated_history_or_body_upper_overflow_fails_without_body(model_db,count,content):
    db=model_db;db.add_all(Memory(content=content) for _ in range(count));await db.flush()
    statements=[];conn=await db.connection()
    def observe(_c,_cursor,statement,_params,_context,_many):statements.append(statement)
    event.listen(conn.sync_connection,"before_cursor_execute",observe)
    try:
        with pytest.raises(HeaderBoundsError):await certify_composition_superset(db,HeaderReadBudget())
    finally:event.remove(conn.sync_connection,"before_cursor_execute",observe)
    assert not any(s.startswith("SELECT memories.") or s.startswith('SELECT "id","content"') for s in statements)


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_actual_search_initialized_schema_metadata_is_supported(async_db, record_property):
    async with async_db() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        import json
        rows=(await db.execute(text("SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE name LIKE 'session_recall_%' ORDER BY type,name"))).all()
        record_property("fts_schema_json", json.dumps([list(row) for row in rows],sort_keys=True))
        record_property("sqlite_version", await db.scalar(text("SELECT sqlite_version()")))
        assert await db.scalar(text("SELECT 1 FROM sqlite_schema WHERE name='session_recall_fts'"))==1
        certificate=await certify_composition_superset(db,HeaderReadBudget())
        await validate_composition_certificate(db,certificate)
        assert (await db.execute(text("SELECT type,name,tbl_name,sql FROM sqlite_schema WHERE name LIKE 'session_recall_%' ORDER BY type,name"))).all()==rows


@pytest.mark.asyncio
async def test_original_transient_run_binding_equals_actual_sql_after_flush(model_db):
    from src.db.models import WorkflowRunState
    from src.workspace.accounting_witness import _native_memory_transient_sql_row
    from src.memory.header_bounds import WRS_BY_RUN
    run=WorkflowRunState(run_identity="actual-byte-projection",root_run_identity="actual-byte-projection",workflow_name="source-owned")
    before=_native_memory_transient_sql_row(run)
    assert type(before["started_at"]) is str and len(before["id"])==32
    model_db.add(run);await model_db.flush()
    columns=",".join('"'+column+'"' for column in WRS_BY_RUN.columns)
    actual=(await model_db.execute(text(f'SELECT {columns} FROM workflow_run_states WHERE id=:id'),{"id":run.id})).one()
    assert dict(zip(WRS_BY_RUN.columns,actual))==before
    with pytest.raises(HeaderBoundsError,match="memory_transient_row_unavailable"):
        _native_memory_transient_sql_row(run)


@pytest.mark.asyncio
async def test_actual_future_constructor_slot_reconciles_after_flush(model_db):
    from src.db.models import WorkflowRunState
    from src.workspace.accounting_witness import _native_memory_transient_sql_row
    import json
    budget=HeaderReadBudget()
    run=WorkflowRunState(run_identity="real-future-slot",root_run_identity="real-future-slot",workflow_name="byte-evidence")
    raw=_native_memory_transient_sql_row(run)
    size=len(json.dumps(raw,ensure_ascii=True,allow_nan=False).encode())
    budget.reserve_future_row(WRS_BY_RUN,run.run_identity,size)
    assert len(budget.future_references)==1 and not budget.physical_references
    model_db.add(run);await model_db.flush()
    ids=await locate_exact_rows(model_db,WRS_BY_RUN,run.run_identity,budget)
    assert ids==(run.run_identity,)
    await budget.certify(model_db,WRS_BY_RUN,ids)
    assert not budget.future_references and len(budget.physical_references)==1
    assert budget.remaining<1_048_576-size


def test_future_constructor_reservation_denies_exhaustion_without_resource_reset():
    budget=HeaderReadBudget();budget.debit(1_048_576-1)
    with pytest.raises(HeaderBoundsError,match="canonical_bound_not_certified"):
        budget.reserve_future_row(WRS_BY_RUN,"actual-reservation-address",2)
    assert budget.remaining==1 and not budget.future_references


@pytest.mark.asyncio
async def test_actual_full_init_db_fixed_search_and_principal_metadata(tmp_path,monkeypatch,record_property):
    import src.db.engine as original
    import json
    workspace=tmp_path/"ordinary-canonical";workspace.mkdir()
    engine=create_async_engine("sqlite+aiosqlite:///"+str(workspace/"seraph.db"))
    monkeypatch.setattr(original,"engine",engine)
    monkeypatch.setattr(original,"_db_path",str(workspace/"seraph.db"))
    monkeypatch.setattr(original.settings,"workspace_dir",str(workspace))
    try:
        await original.init_db()
        factory=sessionmaker(engine,class_=AsyncSession,expire_on_commit=False)
        async with factory() as db:
            await db.execute(text("BEGIN IMMEDIATE"))
            goal=Goal(title="ordinary initialized goal")
            task=WorkBoardTask(owner_principal_id="ordinary-owner",owner_session_id="ordinary-session",goal_id=goal.id,idempotency_key="ordinary-header")
            memory=Memory(content="ordinary bounded private body")
            db.add_all((goal,task,memory));await db.flush()
            actual_event=WorkBoardEvent(task_id=task.task_id,owner_principal_id="ordinary-owner",owner_session_id="ordinary-session",actor_principal_id="ordinary-owner",actor_session_id="ordinary-session",kind="ordinary-header",metadata_json="{}")
            db.add(actual_event);await db.flush()
            before=(await db.execute(text("SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY rowid"))).all()
            names={r[1] for r in before}
            assert {"operator_principal_required_insert","operator_principal_required_update"}<=names
            assert len([n for n in names if n.startswith("session_recall_")])==15
            record_property("ordinary_schema_json",json.dumps([list(r) for r in before]))
            record_property("sqlite_version",await db.scalar(text("SELECT sqlite_version()")))
            statements=[];connection=await db.connection()
            def observe(_c,_cursor,statement,_params,_context,_many):statements.append(statement)
            event.listen(connection.sync_connection,"before_cursor_execute",observe)
            try:
                certificate=await certify_composition_superset(db,HeaderReadBudget())
                await validate_composition_certificate(db,certificate)
                assert ("memories",memory.id) in certificate.rows
                def read_original_rows(c):
                    with snapshot_reads(certificate):
                        actual=_composition_row(c,"work_board_events",actual_event.event_id)
                        assert actual["event_id"]==actual_event.event_id
                        _composition_row(c,"work_board_tasks",task.task_id)
                await connection.run_sync(read_original_rows)
            finally:event.remove(connection.sync_connection,"before_cursor_execute",observe)
            assert not any('FROM "session_recall_fts' in statement or 'FROM session_recall_fts' in statement for statement in statements)
            after=(await db.execute(text("SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY rowid"))).all()
            assert before==after
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_changed_fixed_fts_trigger_denies_before_private_body(async_db):
    async with async_db() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        await db.execute(text("DROP TRIGGER session_recall_sessions_ad"))
        await db.execute(text("CREATE TRIGGER session_recall_sessions_ad AFTER DELETE ON sessions BEGIN SELECT 1; END"))
        with pytest.raises(HeaderBoundsError,match="header_fts_metadata_changed"):
            await certify_composition_superset(db,HeaderReadBudget())


@pytest.mark.asyncio
async def test_exact_1355_schema_objects_overflow_before_private_bodies(async_db):
    async with async_db() as db:
        await db.execute(text("BEGIN IMMEDIATE"))
        count=await db.scalar(text("SELECT count(*) FROM sqlite_schema"))
        assert 1349<=count<1355
        for index in range(1355-count):
            await db.execute(text(f"CREATE INDEX bounded_fts_overflow_{index} ON memories(id)"))
        assert await db.scalar(text("SELECT count(*) FROM sqlite_schema"))==1355
        with pytest.raises(HeaderBoundsError,match="header_schema_object_bound"):
            await certify_composition_superset(db,HeaderReadBudget())
