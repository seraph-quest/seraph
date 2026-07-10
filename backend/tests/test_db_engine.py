from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from src.db.engine import (
    _configure_sqlite_connection,
    _ensure_legacy_columns,
    _ensure_search_indexes,
)
from src.db.models import (
    ModelCapabilityProofRecord,
    ModelRouteAttemptReceiptRecord,
    ModelRouteReceiptRecord,
)


async def test_ensure_legacy_columns_backfills_kind_from_category(tmp_path):
    db_path = tmp_path / "legacy-memory.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    event.listen(engine.sync_engine, "connect", _configure_sqlite_connection)

    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                """
                CREATE TABLE memories (
                    id VARCHAR PRIMARY KEY,
                    content VARCHAR,
                    category VARCHAR,
                    source_session_id VARCHAR,
                    embedding_id VARCHAR,
                    created_at DATETIME
                )
                """
            )
            await conn.exec_driver_sql(
                """
                INSERT INTO memories (id, content, category, source_session_id, embedding_id, created_at)
                VALUES ('goal-1', 'Ship batch A', 'goal', 's1', NULL, '2026-03-25T00:00:00Z')
                """
            )

            await _ensure_legacy_columns(conn)

            row = (
                await conn.exec_driver_sql(
                    "SELECT kind FROM memories WHERE id = 'goal-1'"
                )
            ).fetchone()
            assert row is not None
            assert row[0] == "goal"
    finally:
        await engine.dispose()


async def test_ensure_legacy_columns_adds_guardian_intervention_active_project(tmp_path):
    db_path = tmp_path / "legacy-guardian-interventions.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    event.listen(engine.sync_engine, "connect", _configure_sqlite_connection)

    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                """
                CREATE TABLE guardian_interventions (
                    id VARCHAR PRIMARY KEY,
                    session_id VARCHAR,
                    message_type VARCHAR,
                    intervention_type VARCHAR,
                    urgency INTEGER,
                    content_excerpt VARCHAR,
                    latest_outcome VARCHAR,
                    created_at DATETIME,
                    updated_at DATETIME
                )
                """
            )

            await _ensure_legacy_columns(conn)

            columns = {
                row[1]
                for row in (
                    await conn.exec_driver_sql("PRAGMA table_info(guardian_interventions)")
                ).fetchall()
            }
            indexes = {
                row[1]
                for row in (
                    await conn.exec_driver_sql("PRAGMA index_list(guardian_interventions)")
                ).fetchall()
            }

            assert "active_project" in columns
            assert "ix_guardian_interventions_active_project" in indexes
    finally:
        await engine.dispose()


async def test_ensure_legacy_columns_upgrades_early_model_fabric_tables_idempotently(
    tmp_path,
):
    db_path = tmp_path / "legacy-model-fabric.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    event.listen(engine.sync_engine, "connect", _configure_sqlite_connection)

    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                """
                CREATE TABLE model_capability_proofs (
                    id VARCHAR PRIMARY KEY, proof_hash VARCHAR, profile_schema_version VARCHAR,
                    profile_contract_hash VARCHAR, profile_id VARCHAR, model VARCHAR,
                    endpoint VARCHAR, endpoint_digest VARCHAR, endpoint_class VARCHAR,
                    adapter VARCHAR, capability VARCHAR, canary_version VARCHAR,
                    outcome VARCHAR, checked_at FLOAT, expires_at FLOAT,
                    proven_value_json VARCHAR, created_at DATETIME
                )
                """
            )
            await conn.exec_driver_sql(
                """
                CREATE TABLE model_route_receipts (
                    id VARCHAR PRIMARY KEY, receipt_id VARCHAR, receipt_hash VARCHAR,
                    request_id VARCHAR, route_decision_id VARCHAR, workload VARCHAR,
                    outcome VARCHAR, actual_profile_id VARCHAR, actual_model VARCHAR,
                    actual_adapter VARCHAR, destination_class VARCHAR, egress_class VARCHAR,
                    trust_decision_id VARCHAR, started_at DATETIME, finished_at DATETIME,
                    latency_ms INTEGER, created_at DATETIME
                )
                """
            )
            await conn.exec_driver_sql(
                """
                CREATE TABLE model_route_attempt_receipts (
                    id VARCHAR PRIMARY KEY, route_receipt_id VARCHAR, attempt_id VARCHAR,
                    attempt_index INTEGER, profile_id VARCHAR, model VARCHAR, endpoint VARCHAR,
                    endpoint_digest VARCHAR, adapter VARCHAR, destination_class VARCHAR,
                    egress_class VARCHAR, trust_decision_id VARCHAR,
                    capability_proof_hashes_json VARCHAR, outcome VARCHAR, error_code VARCHAR,
                    started_at DATETIME, finished_at DATETIME, latency_ms INTEGER,
                    created_at DATETIME
                )
                """
            )
            await conn.exec_driver_sql(
                """
                INSERT INTO model_capability_proofs VALUES (
                    'proof-row', 'proof-hash', 'seraph.model-fabric.v1', 'contract-hash',
                    'legacy-profile', 'legacy-model', 'http://127.0.0.1:8000/v1',
                    'endpoint-hash', 'loopback', 'openai_compatible_chat', 'text',
                    'canary-v1', 'passed', 10.0, 20.0, '"verified"',
                    '2026-07-01T00:00:00Z'
                )
                """
            )
            await conn.exec_driver_sql(
                """
                INSERT INTO model_route_receipts VALUES (
                    'route-row', 'legacy-receipt', 'route-hash', 'request-1', 'decision-1',
                    'capability_probe', 'succeeded', 'legacy-profile', 'legacy-model',
                    'openai_compatible_chat', 'loopback', 'local_only', 'trust-1',
                    '2026-07-01T00:00:00Z', '2026-07-01T00:00:01Z', 1000,
                    '2026-07-01T00:00:01Z'
                )
                """
            )
            await conn.exec_driver_sql(
                """
                INSERT INTO model_route_attempt_receipts VALUES (
                    'attempt-row', 'legacy-receipt', 'attempt-1', 0, 'legacy-profile',
                    'legacy-model', 'http://127.0.0.1:8000/v1', 'endpoint-hash',
                    'openai_compatible_chat', 'loopback', 'local_only', 'trust-1', '[]',
                    'succeeded', NULL, '2026-07-01T00:00:00Z',
                    '2026-07-01T00:00:01Z', 1000, '2026-07-01T00:00:01Z'
                )
                """
            )

            await _ensure_legacy_columns(conn)
            await _ensure_legacy_columns(conn)

        factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as session:
            proof = await session.get(ModelCapabilityProofRecord, "proof-row")
            route = await session.get(ModelRouteReceiptRecord, "route-row")
            attempt = await session.get(ModelRouteAttemptReceiptRecord, "attempt-row")
            assert proof is not None and proof.receipt_id == "legacy-unbound"
            assert proof.receipt_hash == "0" * 64
            assert route is not None and route.runtime_path == "legacy_unknown"
            assert route.degradation_codes_json == "[]"
            assert attempt is not None and attempt.cost_kind == "unknown"
            proof.receipt_hash = "a" * 64
            route.usage_total_tokens = 7
            attempt.degradation_code = "legacy_checked"
            await session.commit()

        async with engine.connect() as conn:
            preserved = (
                await conn.exec_driver_sql(
                    "SELECT profile_id, receipt_hash FROM model_capability_proofs WHERE id='proof-row'"
                )
            ).one()
            route_update = (
                await conn.exec_driver_sql(
                    "SELECT usage_total_tokens FROM model_route_receipts WHERE id='route-row'"
                )
            ).one()
            attempt_update = (
                await conn.exec_driver_sql(
                    "SELECT degradation_code FROM model_route_attempt_receipts WHERE id='attempt-row'"
                )
            ).one()
            assert preserved == ("legacy-profile", "a" * 64)
            assert route_update == (7,)
            assert attempt_update == ("legacy_checked",)
    finally:
        await engine.dispose()


async def test_sqlite_connection_enables_foreign_keys(tmp_path):
    db_path = tmp_path / "fk-check.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    event.listen(engine.sync_engine, "connect", _configure_sqlite_connection)

    try:
        async with engine.connect() as conn:
            foreign_keys = (await conn.exec_driver_sql("PRAGMA foreign_keys")).fetchone()
            assert foreign_keys is not None
            assert foreign_keys[0] == 1
            busy_timeout = (await conn.exec_driver_sql("PRAGMA busy_timeout")).fetchone()
            assert busy_timeout is not None
            assert busy_timeout[0] == 5000
    finally:
        await engine.dispose()


async def test_ensure_search_indexes_backfills_session_message_and_event_rows(tmp_path):
    db_path = tmp_path / "fts-check.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    event.listen(engine.sync_engine, "connect", _configure_sqlite_connection)

    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                """
                CREATE TABLE sessions (
                    id VARCHAR PRIMARY KEY,
                    title VARCHAR,
                    created_at DATETIME,
                    updated_at DATETIME
                )
                """
            )
            await conn.exec_driver_sql(
                """
                CREATE TABLE messages (
                    id VARCHAR PRIMARY KEY,
                    session_id VARCHAR,
                    role VARCHAR,
                    content VARCHAR,
                    metadata_json VARCHAR,
                    step_number INTEGER,
                    tool_used VARCHAR,
                    created_at DATETIME
                )
                """
            )
            await conn.exec_driver_sql(
                """
                CREATE TABLE memory_episodes (
                    id VARCHAR PRIMARY KEY,
                    session_id VARCHAR,
                    episode_type VARCHAR,
                    summary VARCHAR,
                    content VARCHAR,
                    source_message_id VARCHAR,
                    source_tool_name VARCHAR,
                    source_role VARCHAR,
                    subject_entity_id VARCHAR,
                    project_entity_id VARCHAR,
                    salience FLOAT,
                    confidence FLOAT,
                    metadata_json VARCHAR,
                    observed_at DATETIME,
                    created_at DATETIME
                )
                """
            )
            await conn.exec_driver_sql(
                """
                INSERT INTO sessions (id, title, created_at, updated_at)
                VALUES ('s1', 'Atlas planning', '2026-03-25T00:00:00Z', '2026-03-25T00:00:00Z')
                """
            )
            await conn.exec_driver_sql(
                """
                INSERT INTO messages (id, session_id, role, content, created_at)
                VALUES ('m1', 's1', 'assistant', 'Weather planning for Atlas', '2026-03-25T00:01:00Z')
                """
            )
            await conn.exec_driver_sql(
                """
                INSERT INTO memory_episodes (id, session_id, episode_type, summary, content, observed_at, created_at)
                VALUES ('e1', 's1', 'workflow', 'Workflow failed', 'Upload step failed for Atlas workflow', '2026-03-25T00:02:00Z', '2026-03-25T00:02:00Z')
                """
            )

            await _ensure_search_indexes(conn)

            rows = (
                await conn.exec_driver_sql(
                    """
                    SELECT entry_type, source_label, text
                    FROM session_recall_fts
                    WHERE session_recall_fts MATCH 'Atlas'
                    ORDER BY created_at
                    """
                )
            ).fetchall()

            assert len(rows) == 3
            assert rows[0][0] == "title"
            assert rows[1][0] == "message"
            assert rows[2][0] == "event"
    finally:
        await engine.dispose()
