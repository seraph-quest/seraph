"""Existing SQLite schema stays additive, idempotent and indexed."""
import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from src.db.engine import _ensure_legacy_columns


@pytest.mark.asyncio
async def test_existing_native_table_selected_context_columns_and_query_plans(tmp_path):
    engine=create_async_engine(f"sqlite+aiosqlite:///{tmp_path/'legacy.db'}")
    try:
        async with engine.begin() as db:
            await db.exec_driver_sql("CREATE TABLE workflow_run_states (id VARCHAR PRIMARY KEY, status VARCHAR, run_fingerprint VARCHAR, metadata_json VARCHAR)")
            await db.exec_driver_sql("INSERT INTO workflow_run_states VALUES ('legacy','completed','original-digest','{}')")
            await _ensure_legacy_columns(db)
            first=(await db.exec_driver_sql("SELECT status,source_task_id,selected_context_reserved_bytes,metadata_json FROM workflow_run_states WHERE id='legacy'")).one()
            await _ensure_legacy_columns(db)
            second=(await db.exec_driver_sql("SELECT status,source_task_id,selected_context_reserved_bytes,metadata_json FROM workflow_run_states WHERE id='legacy'")).one()
            assert first==second and first[1:3]==(None,None)
            task_plan=(await db.exec_driver_sql("EXPLAIN QUERY PLAN SELECT id FROM workflow_run_states WHERE job_kind='selected_context_v1' AND owner_principal_id='owner' AND operator_session_id='root' AND source_task_id='task' LIMIT 33")).all()
            quota_plan=(await db.exec_driver_sql("EXPLAIN QUERY PLAN SELECT selected_context_reserved_bytes FROM workflow_run_states WHERE job_kind='selected_context_v1' AND owner_principal_id='owner' AND selected_context_reserved_bytes > 0 LIMIT 65")).all()
            assert any("ix_workflow_run_states_source_task" in row[3] for row in task_plan)
            assert any("ix_workflow_run_states_selected_context_quota" in row[3] for row in quota_plan)
    finally:
        await engine.dispose()
