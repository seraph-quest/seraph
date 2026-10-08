"""Exact old/new Session DDL compatibility; no task-link retention grant."""
import sqlite3

import pytest

from src.workspace.accounting_witness import validate_retained_table_schema, _composition_row
from src.workspace.production import ProductionWorkspaceReconciliationError
from tests.test_runtime_composition_ownership import composition_db, bound_spec
from src.workflows.job_runtime import DurableJobRepository
from src.workspace.production import maintenance_fence, read_lifecycle_receipt, read_accounting_checkpoint


def session_database(column="continuity_task_id VARCHAR REFERENCES work_board_tasks(task_id)", index="CREATE INDEX ix_sessions_continuity_task_id ON sessions (continuity_task_id)"):
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE work_board_tasks(task_id VARCHAR PRIMARY KEY)")
    addition = ", " + column if column else ""
    db.execute("CREATE TABLE sessions(id VARCHAR PRIMARY KEY, owner_principal_id VARCHAR, title VARCHAR, created_at DATETIME, updated_at DATETIME" + addition + ")")
    if index:
        db.execute(index)
    return db


@pytest.mark.parametrize("old", [False, True])
def test_exact_old_and_nullable_session_ddl_remain_supported(old):
    with session_database("" if old else "continuity_task_id VARCHAR REFERENCES work_board_tasks(task_id)", "" if old else "CREATE INDEX ix_sessions_continuity_task_id ON sessions (continuity_task_id)") as db:
        validate_retained_table_schema(db, "sessions")
        db.execute("INSERT INTO sessions(id) VALUES ('selected')")
        assert set(_composition_row(db, "sessions", "selected")) == {"id", "owner_principal_id", "title", "created_at", "updated_at"}


@pytest.mark.parametrize("column", [
    "continuity_task_id TEXT REFERENCES work_board_tasks(task_id)",
    "continuity_task_id VARCHAR NOT NULL REFERENCES work_board_tasks(task_id)",
    "continuity_task_id VARCHAR DEFAULT NULL REFERENCES work_board_tasks(task_id)",
    "continuity_task_id VARCHAR REFERENCES work_board_tasks(task_id) ON DELETE CASCADE",
    "continuity_task_id VARCHAR REFERENCES work_board_tasks(task_id) ON UPDATE CASCADE",
    "continuity_task_id VARCHAR REFERENCES sessions(id)",
    "continuity_task_id VARCHAR",
    "continuity_task_id VARCHAR REFERENCES work_board_tasks(task_id), unknown_column VARCHAR",
    "continuity_task_id VARCHAR, FOREIGN KEY(continuity_task_id,id) REFERENCES work_board_tasks(task_id,task_id)",
])
@pytest.mark.parametrize("error", ["composition_projection_schema_changed", "composition_restore_schema_unavailable"])
def test_source_and_destination_reject_incompatible_column(column, error):
    with session_database(column) as db:
        with pytest.raises(ProductionWorkspaceReconciliationError, match=error):
            validate_retained_table_schema(db, "sessions", error=error)


@pytest.mark.parametrize("index", [
    "",
    "CREATE INDEX ix_sessions_continuity_task_id ON sessions (title)",
    "CREATE UNIQUE INDEX ix_sessions_continuity_task_id ON sessions (continuity_task_id)",
    "CREATE INDEX ix_sessions_continuity_task_id ON sessions (continuity_task_id) WHERE continuity_task_id IS NOT NULL",
    "CREATE INDEX ix_sessions_continuity_task_id ON sessions (continuity_task_id, title)",
    "CREATE INDEX ix_sessions_continuity_task_id ON sessions (lower(continuity_task_id))",
    "CREATE INDEX ix_sessions_continuity_task_id ON sessions (continuity_task_id DESC)",
    "CREATE INDEX ix_sessions_continuity_task_id ON sessions (continuity_task_id COLLATE NOCASE)",
    "CREATE INDEX ix_sessions_continuity_task_id ON work_board_tasks (task_id)",
])
@pytest.mark.parametrize("error", ["composition_projection_schema_changed", "composition_restore_schema_unavailable"])
def test_source_and_destination_reject_missing_or_malformed_actual_index(index, error):
    with session_database(index=index) as db:
        with pytest.raises(ProductionWorkspaceReconciliationError, match=error):
            validate_retained_table_schema(db, "sessions", error=error)


def test_historical_ddl_cannot_hide_malformed_named_index():
    with session_database("", "CREATE INDEX ix_sessions_continuity_task_id ON sessions(title)") as db:
        with pytest.raises(ProductionWorkspaceReconciliationError, match="composition_projection_schema_changed"):
            validate_retained_table_schema(db, "sessions")


def test_selected_session_link_is_unsupported_without_clearing_it():
    with session_database() as db:
        db.execute("INSERT INTO sessions(id, continuity_task_id) VALUES ('selected', 'task')")
        validate_retained_table_schema(db, "sessions")
        with pytest.raises(ProductionWorkspaceReconciliationError, match="composition_session_continuity_unsupported"):
            _composition_row(db, "sessions", "selected")
        assert db.execute("SELECT continuity_task_id FROM sessions").fetchone()[0] == "task"


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["source", "destination"])
@pytest.mark.parametrize("damage", ["missing_index", "wrong_index", "nonnull"])
async def test_restore_rejects_actual_schema_or_selected_link_before_copy(composition_db, tmp_path, side, damage):
    from src.workspace.accounting_continuity import retain_inference_accounting
    root, _, _, workspace = composition_db
    await DurableJobRepository().admit_job(await bound_spec(job_id="ddl-retained"))
    target = tmp_path / "retained"
    target.mkdir()
    with sqlite3.connect(root / "seraph.db") as source, sqlite3.connect(target / "seraph.db") as destination:
        source.backup(destination)
    damaged = root if side == "source" else target
    with sqlite3.connect(damaged / "seraph.db") as db:
        if damage == "nonnull":
            assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] > 0
            db.execute("UPDATE sessions SET continuity_task_id='unsupported-task'")
        else:
            db.execute("DROP INDEX ix_sessions_continuity_task_id")
            if damage == "wrong_index":
                db.execute("CREATE INDEX ix_sessions_continuity_task_id ON sessions(title)")
    with sqlite3.connect(target / "seraph.db") as db:
        before = tuple(db.iterdump())
    receipt, checkpoint = read_lifecycle_receipt(workspace), read_accounting_checkpoint(workspace)
    with maintenance_fence(workspace), pytest.raises(ProductionWorkspaceReconciliationError):
        retain_inference_accounting(active=root, target=target, database_path="seraph.db")
    with sqlite3.connect(target / "seraph.db") as db:
        assert tuple(db.iterdump()) == before
    assert read_lifecycle_receipt(workspace) == receipt
    assert read_accounting_checkpoint(workspace) == checkpoint


@pytest.mark.asyncio
@pytest.mark.parametrize("source_old,destination_old", [(True, True), (True, False), (False, True), (False, False)])
async def test_actual_old_new_null_only_restore_preserves_frozen_job_binding(composition_db, tmp_path, source_old, destination_old):
    from src.workspace.accounting_continuity import retain_inference_accounting
    root, _, _, workspace = composition_db
    await DurableJobRepository().admit_job(await bound_spec(job_id="ddl-positive"))
    target = tmp_path / "retained"
    target.mkdir()
    with sqlite3.connect(root / "seraph.db") as source, sqlite3.connect(target / "seraph.db") as destination:
        source.backup(destination)
        original_binding = source.execute("SELECT composition_binding_json FROM workflow_run_states WHERE run_identity='ddl-positive'").fetchone()[0]
    for directory, old in ((root, source_old), (target, destination_old)):
        if old:
            with sqlite3.connect(directory / "seraph.db") as db:
                db.execute("DROP INDEX ix_sessions_continuity_task_id")
                db.execute("CREATE TABLE sessions_old(id VARCHAR PRIMARY KEY, owner_principal_id VARCHAR, title VARCHAR, created_at DATETIME, updated_at DATETIME)")
                db.execute("INSERT INTO sessions_old SELECT id,owner_principal_id,title,created_at,updated_at FROM sessions")
                db.execute("DROP TABLE sessions")
                db.execute("ALTER TABLE sessions_old RENAME TO sessions")
    with maintenance_fence(workspace):
        result = retain_inference_accounting(active=root, target=target, database_path="seraph.db")
        assert retain_inference_accounting(active=root, target=target, database_path="seraph.db") == result
    with sqlite3.connect(target / "seraph.db") as db:
        validate_retained_table_schema(db, "sessions")
        assert db.execute("SELECT COUNT(*) FROM runtime_composition_states WHERE state='blocked'").fetchone()[0] == 14
        assert db.execute("SELECT COUNT(*) FROM runtime_composition_states WHERE epoch=2").fetchone()[0] == 14
        assert db.execute("SELECT COUNT(*) FROM audit_events WHERE event_type='runtime_composition_recovery'").fetchone()[0] == 14
        assert db.execute("SELECT composition_binding_json FROM workflow_run_states WHERE run_identity='ddl-positive'").fetchone()[0] == original_binding
        if not destination_old:
            assert db.execute("SELECT COUNT(*) FROM sessions WHERE continuity_task_id IS NOT NULL").fetchone()[0] == 0
