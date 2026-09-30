import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import sqlite3
import threading

import pytest
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from sqlmodel import SQLModel

from config.settings import settings
import src.db.engine as db_engine
from src.db.engine import (
    _configure_sqlite_connection,
    _ensure_legacy_columns,
    _ensure_guardian_inbox_columns,
    _ensure_m5_columns,
    _ensure_search_indexes,
    engine as production_engine,
)
from src.db.models import (
    GuardianDecisionPacket,
    GuardianInboxAction,
    ModelCapabilityProofRecord,
    ModelRouteAttemptReceiptRecord,
    ModelRouteReceiptRecord,
    OperatorSession,
)


async def test_guardian_inbox_pending_index_shape_is_stable_for_fresh_and_legacy_databases(tmp_path):
    expected_columns = ["inbox_pending", "updated_at", "id"]

    fresh_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fresh-guardian-packet.db'}")
    event.listen(fresh_engine.sync_engine, "connect", _configure_sqlite_connection)
    try:
        async with fresh_engine.begin() as conn:
            await conn.run_sync(
                lambda sync: SQLModel.metadata.create_all(
                    sync,
                    tables=[GuardianDecisionPacket.__table__],
                )
            )
            fresh_columns = [
                row[2]
                for row in (
                    await conn.exec_driver_sql(
                        "PRAGMA index_info(ix_guardian_decision_packets_inbox_pending_updated)"
                    )
                ).fetchall()
            ]
        assert fresh_columns == expected_columns
    finally:
        await fresh_engine.dispose()

    legacy_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy-guardian-packet.db'}")
    event.listen(legacy_engine.sync_engine, "connect", _configure_sqlite_connection)
    try:
        async with legacy_engine.begin() as conn:
            await conn.exec_driver_sql(
                "CREATE TABLE guardian_decision_packets (id VARCHAR PRIMARY KEY, updated_at DATETIME)"
            )
            await _ensure_legacy_columns(conn)
            await _ensure_legacy_columns(conn)
            await conn.exec_driver_sql(
                "DROP INDEX ix_guardian_decision_packets_inbox_pending_updated"
            )
            # A workspace may already have the additive column from an older
            # startup but have missed the bounded-repair index.  Re-running
            # migration must restore the exact composite shape.
            await _ensure_legacy_columns(conn)
            legacy_columns = [
                row[2]
                for row in (
                    await conn.exec_driver_sql(
                        "PRAGMA index_info(ix_guardian_decision_packets_inbox_pending_updated)"
                    )
                ).fetchall()
            ]
            inbox_pending_column = [
                row[1]
                for row in (
                    await conn.exec_driver_sql(
                        "PRAGMA table_info(guardian_decision_packets)"
                    )
                ).fetchall()
            ]
        assert legacy_columns == expected_columns
        assert "inbox_pending" in inbox_pending_column
    finally:
        await legacy_engine.dispose()


async def test_session_factory_override_is_task_local(monkeypatch):
    class MarkerSession:
        def __init__(self, marker: str):
            self.info = {"marker": marker}

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def commit(self):
            return None

        async def rollback(self):
            return None

    monkeypatch.setattr(
        db_engine,
        "async_session_factory",
        lambda: MarkerSession("canonical"),
    )
    release_canonical_reader = asyncio.Event()

    async def read_marker() -> str:
        async with db_engine.get_session() as session:
            return str(session.info["marker"])

    async def unrelated_application_request() -> str:
        await release_canonical_reader.wait()
        return await read_marker()

    # This task captures the canonical context before the eval override is set.
    unrelated_request = asyncio.create_task(unrelated_application_request())
    with db_engine.override_session_factory(lambda: MarkerSession("eval")):
        release_canonical_reader.set()
        eval_value = await read_marker()
        canonical_value = await unrelated_request

    assert eval_value == "eval"
    assert canonical_value == "canonical"


async def test_ensure_m5_columns_adds_m5_candidate_digests(tmp_path):
    db_path = tmp_path / "legacy-m5-receipts.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    event.listen(engine.sync_engine, "connect", _configure_sqlite_connection)

    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                "CREATE TABLE memory_proposals (proposal_id VARCHAR PRIMARY KEY)"
            )
            await conn.exec_driver_sql(
                "CREATE TABLE work_board_decision_receipts (receipt_id VARCHAR PRIMARY KEY)"
            )
            await _ensure_m5_columns(conn)
            await _ensure_m5_columns(conn)
            receipt_columns = {
                row[1]
                for row in (
                    await conn.exec_driver_sql(
                        "PRAGMA table_info(work_board_decision_receipts)"
                    )
                ).fetchall()
            }
            proposal_columns = {
                row[1]
                for row in (
                    await conn.exec_driver_sql("PRAGMA table_info(memory_proposals)")
                ).fetchall()
            }
        assert "candidate_set_digest" in proposal_columns
        assert "acceptance_binding_digest" in proposal_columns
        assert "artifact_ref" in proposal_columns
        assert "artifact_digest" in proposal_columns
        assert "rollback_reason" in proposal_columns
        assert "candidate_set_digest" in receipt_columns
        assert "receipt_integrity_mac" in receipt_columns
    finally:
        await engine.dispose()


async def test_ensure_guardian_inbox_columns_adds_nullable_safe_reason(tmp_path):
    db_path = tmp_path / "legacy-guardian-inbox.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    event.listen(engine.sync_engine, "connect", _configure_sqlite_connection)

    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                """
                CREATE TABLE guardian_inbox_actions (
                    id VARCHAR PRIMARY KEY,
                    owner_principal_id VARCHAR NOT NULL,
                    owner_session_id VARCHAR NOT NULL,
                    item_id VARCHAR NOT NULL,
                    idempotency_key VARCHAR NOT NULL,
                    payload_digest VARCHAR NOT NULL,
                    action VARCHAR NOT NULL,
                    prior_revision INTEGER NOT NULL,
                    result_revision INTEGER NOT NULL,
                    task_id VARCHAR,
                    safe_result_json VARCHAR NOT NULL,
                    created_at DATETIME
                )
                """
            )
            await _ensure_guardian_inbox_columns(conn)
            await _ensure_guardian_inbox_columns(conn)
            columns = {
                row[1]: row[3]
                for row in (
                    await conn.exec_driver_sql(
                        "PRAGMA table_info(guardian_inbox_actions)"
                    )
                ).fetchall()
            }
        assert columns["safe_reason"] == 0
    finally:
        await engine.dispose()


async def test_init_db_upgrades_legacy_guardian_inbox_action_table(tmp_path, monkeypatch):
    root = tmp_path / "legacy-inbox-startup"
    root.mkdir()
    database_path = root / "seraph.db"
    sync_engine = create_sync_engine(f"sqlite:///{database_path}")
    with sync_engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE guardian_inbox_actions (
                id VARCHAR PRIMARY KEY,
                owner_principal_id VARCHAR NOT NULL,
                owner_session_id VARCHAR NOT NULL,
                item_id VARCHAR NOT NULL,
                idempotency_key VARCHAR NOT NULL,
                payload_digest VARCHAR NOT NULL,
                action VARCHAR NOT NULL,
                prior_revision INTEGER NOT NULL,
                result_revision INTEGER NOT NULL,
                task_id VARCHAR,
                safe_result_json VARCHAR NOT NULL,
                created_at DATETIME
            )
            """
        )
        connection.exec_driver_sql(
            """
            INSERT INTO guardian_inbox_actions (
                id, owner_principal_id, owner_session_id, item_id,
                idempotency_key, payload_digest, action, prior_revision,
                result_revision, task_id, safe_result_json, created_at
            ) VALUES (
                'legacy-action', 'operator:legacy', 'legacy-session',
                'legacy-item', 'legacy-key', 'a', 'dismiss', 1, 2, NULL,
                '{"state":"dismissed"}', '2026-09-30T00:00:00Z'
            )
            """
        )
    sync_engine.dispose()

    async_engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(settings, "workspace_dir", str(root))
    monkeypatch.setattr(db_engine, "engine", async_engine)
    monkeypatch.setattr(db_engine, "_db_path", str(database_path))

    async def _noop(_connection):
        return None

    monkeypatch.setattr(db_engine, "_ensure_legacy_columns", _noop)
    monkeypatch.setattr(db_engine, "_ensure_telegram_transport_columns", _noop)
    monkeypatch.setattr(db_engine, "_ensure_memory_indexes", _noop)
    monkeypatch.setattr(db_engine, "_ensure_search_indexes", _noop)

    try:
        await db_engine.init_db()
    finally:
        await async_engine.dispose()

    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]
            for row in connection.execute(
                f"PRAGMA table_info({GuardianInboxAction.__tablename__})"
            )
        }
        legacy_row = connection.execute(
            "SELECT id, safe_reason FROM guardian_inbox_actions WHERE id = 'legacy-action'"
        ).fetchone()
    assert "safe_reason" in columns
    assert legacy_row == ("legacy-action", None)


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


async def test_ensure_legacy_columns_adds_session_owner_principal_id(tmp_path):
    db_path = tmp_path / "legacy-sessions.db"
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
                INSERT INTO sessions (id, title, created_at, updated_at)
                VALUES ('legacy-session', 'Legacy', '2026-03-25T00:00:00Z', '2026-03-25T00:00:00Z')
                """
            )

            await _ensure_legacy_columns(conn)
            await _ensure_legacy_columns(conn)

            columns = {
                row[1]
                for row in (
                    await conn.exec_driver_sql("PRAGMA table_info(sessions)")
                ).fetchall()
            }
            indexes = {
                row[1]
                for row in (
                    await conn.exec_driver_sql("PRAGMA index_list(sessions)")
                ).fetchall()
            }
            owner = (
                await conn.exec_driver_sql(
                    "SELECT owner_principal_id FROM sessions WHERE id = 'legacy-session'"
                )
            ).one()

            assert "owner_principal_id" in columns
            assert "ix_sessions_owner_principal_id" in indexes
            assert owner == (None,)
    finally:
        await engine.dispose()


async def test_ensure_legacy_columns_claims_only_transcript_sessions_for_single_operator(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "legacy-conversations.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    event.listen(engine.sync_engine, "connect", _configure_sqlite_connection)
    monkeypatch.setattr(settings, "operator_auth_secret", "configured-secret")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")

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
                    content VARCHAR
                )
                """
            )
            await conn.exec_driver_sql(
                """
                INSERT INTO sessions (id, title)
                VALUES
                    ('legacy-conversation', 'Legacy cockpit'),
                    ('service-placeholder', 'Job reference'),
                    ('other-owner', 'Other operator')
                """
            )
            await conn.exec_driver_sql(
                """
                INSERT INTO messages (id, session_id, role, content)
                VALUES
                    ('message-1', 'legacy-conversation', 'user', 'Continue our work'),
                    ('message-2', 'service-placeholder', 'step', 'scheduled work'),
                    ('message-3', 'other-owner', 'assistant', 'Already claimed')
                """
            )
            await conn.exec_driver_sql(
                "ALTER TABLE sessions ADD COLUMN owner_principal_id VARCHAR"
            )
            await conn.exec_driver_sql(
                "UPDATE sessions SET owner_principal_id = 'operator:other' "
                "WHERE id = 'other-owner'"
            )

            await _ensure_legacy_columns(conn)
            await _ensure_legacy_columns(conn)

            owners = (
                await conn.exec_driver_sql(
                    "SELECT id, owner_principal_id FROM sessions ORDER BY id"
                )
            ).fetchall()

        assert owners == [
            ("legacy-conversation", "operator:single"),
            ("other-owner", "operator:other"),
            ("service-placeholder", None),
        ]
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


async def test_ensure_legacy_columns_adds_goal_proactive_permission_disabled_by_default(tmp_path):
    db_path = tmp_path / "legacy-goals-proactive.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    event.listen(engine.sync_engine, "connect", _configure_sqlite_connection)

    try:
        async with engine.begin() as conn:
            await conn.exec_driver_sql(
                """
                CREATE TABLE goals (
                    id VARCHAR PRIMARY KEY,
                    title VARCHAR,
                    revision INTEGER,
                    success_criterion_json VARCHAR
                )
                """
            )
            await conn.exec_driver_sql(
                "INSERT INTO goals (id, title, revision) VALUES ('goal-1', 'Legacy', 1)"
            )

            await _ensure_legacy_columns(conn)

            row = (
                await conn.exec_driver_sql(
                    "SELECT proactive_enabled FROM goals WHERE id = 'goal-1'"
                )
            ).one()
            indexes = {
                item[1]
                for item in (
                    await conn.exec_driver_sql("PRAGMA index_list(goals)")
                ).fetchall()
            }
            assert row[0] == 0
            assert "ix_goals_proactive_enabled" in indexes
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


@pytest.mark.asyncio
async def _run_file_backed_sqlite_auth_pool_is_bounded(
    tmp_path,
    monkeypatch,
):
    """The merged #877 pool bound still covers a 40-request auth burst."""
    pool = production_engine.sync_engine.pool
    assert pool._pool.maxsize == 20
    assert pool._max_overflow == 0
    assert pool._timeout == 3
    assert pool._pre_ping is True

    db_path = tmp_path / "operator-auth-pool.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        pool_size=20,
        max_overflow=0,
        pool_timeout=3,
        pool_pre_ping=True,
    )
    event.listen(engine.sync_engine, "connect", _configure_sqlite_connection)

    checked_out = 0
    max_checked_out = 0
    counter_lock = threading.Lock()
    checkout_barrier = asyncio.Barrier(20)

    @event.listens_for(engine.sync_engine, "checkout")
    def _track_checkout(_dbapi_connection, _connection_record, _connection_proxy):
        nonlocal checked_out, max_checked_out
        with counter_lock:
            checked_out += 1
            max_checked_out = max(max_checked_out, checked_out)

    @event.listens_for(engine.sync_engine, "checkin")
    def _track_checkin(_dbapi_connection, _connection_record):
        nonlocal checked_out
        with counter_lock:
            checked_out -= 1

    class BarrierSession(AsyncSession):
        async def get(self, entity, ident, **kwargs):
            await self.connection()
            await asyncio.wait_for(checkout_barrier.wait(), timeout=5)
            return await super().get(entity, ident, **kwargs)

    setup_factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    factory = sessionmaker(engine, class_=BarrierSession, expire_on_commit=False)

    @asynccontextmanager
    async def _get_session():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    monkeypatch.setattr("src.auth.service.get_session", _get_session)
    from src.auth.service import authenticate_session

    try:
        now = datetime.now(timezone.utc)
        async with engine.begin() as connection:
            await connection.run_sync(OperatorSession.__table__.create)
        async with setup_factory() as session:
            session.add(
                OperatorSession(
                    id="auth-pool-session",
                    token_hash="not-used-by-this-test",
                    last_seen_at=now,
                    idle_expires_at=now + timedelta(minutes=5),
                    absolute_expires_at=now + timedelta(hours=1),
                )
            )
            await session.commit()

        results = await asyncio.wait_for(
            asyncio.gather(
                *[
                    authenticate_session("auth-pool-session", touch=False)
                    for _ in range(40)
                ],
                return_exceptions=True,
            ),
            timeout=30,
        )
        assert [result for result in results if isinstance(result, BaseException)] == []
        assert len(results) == 40
        assert max_checked_out == 20
        assert max_checked_out <= 20
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_file_backed_sqlite_auth_pool_is_bounded(tmp_path, monkeypatch):
    await asyncio.wait_for(
        _run_file_backed_sqlite_auth_pool_is_bounded(tmp_path, monkeypatch),
        timeout=60,
    )


@pytest.mark.asyncio
async def _run_file_backed_sqlite_auth_touch_is_coalesced(
    tmp_path,
    monkeypatch,
):
    """Concurrent authentication coalesces a stale session touch to one write."""
    pool = production_engine.sync_engine.pool
    assert pool._pool.maxsize == 20
    assert pool._max_overflow == 0
    assert pool._timeout == 3
    assert pool._pre_ping is True

    db_path = tmp_path / "operator-auth-contention.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        pool_size=20,
        max_overflow=0,
        pool_timeout=3,
        pool_pre_ping=True,
    )
    event.listen(engine.sync_engine, "connect", _configure_sqlite_connection)

    updates_entered = 0
    initial_auth_reads_started = 0
    barrier_gets_completed = 0
    checked_out = 0
    max_checked_out = 0
    counter_lock = threading.Lock()
    pool_saturation_barrier = asyncio.Barrier(20)
    initial_reads_completed = asyncio.Event()
    task_get_calls: dict[asyncio.Task, int] = {}
    lock_connection = None
    lock_engine = None
    lock_released = False

    @event.listens_for(engine.sync_engine, "checkout")
    def _track_checkout(_dbapi_connection, _connection_record, _connection_proxy):
        nonlocal checked_out, max_checked_out
        with counter_lock:
            checked_out += 1
            max_checked_out = max(max_checked_out, checked_out)

    @event.listens_for(engine.sync_engine, "checkin")
    def _track_checkin(_dbapi_connection, _connection_record):
        nonlocal checked_out
        with counter_lock:
            checked_out -= 1

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _observe_auth_touch(
        _conn, _cursor, statement, _parameters, _context, _executemany
    ):
        nonlocal updates_entered
        if statement.lstrip().upper().startswith("UPDATE OPERATOR_SESSIONS"):
            with counter_lock:
                updates_entered += 1

    setup_factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    class BarrierSession(AsyncSession):
        async def get(self, entity, ident, **kwargs):
            nonlocal initial_auth_reads_started, barrier_gets_completed
            task = asyncio.current_task()
            assert task is not None
            task_get_calls[task] = task_get_calls.get(task, 0) + 1
            if task_get_calls[task] == 1:
                # The first twenty request tasks hold their initial reads
                # together to prove the pool reaches its 20-connection cap.
                # Later initial reads do not join another full barrier: one
                # serialized stale-touch write may already hold a connection.
                with counter_lock:
                    initial_auth_reads_started += 1
                    saturate_pool = initial_auth_reads_started <= 20
                if saturate_pool:
                    await self.connection()
                    await asyncio.wait_for(pool_saturation_barrier.wait(), timeout=5)
                record = await super().get(entity, ident, **kwargs)
                with counter_lock:
                    barrier_gets_completed += 1
                    if barrier_gets_completed == 40:
                        initial_reads_completed.set()
                return record
            return await super().get(entity, ident, **kwargs)

    request_factory = sessionmaker(engine, class_=BarrierSession, expire_on_commit=False)

    @asynccontextmanager
    async def _get_session():
        async with request_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    monkeypatch.setattr("src.auth.service.get_session", _get_session)
    from src.auth.service import authenticate_session

    async def _release_lock():
        nonlocal lock_released
        if lock_released or lock_connection is None:
            return
        await lock_connection.exec_driver_sql("ROLLBACK")
        lock_released = True

    async def _wait_for_update():
        deadline = asyncio.get_running_loop().time() + 6
        while True:
            with counter_lock:
                observed = updates_entered
            if observed >= 1:
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError(f"expected one blocked auth touch update, observed {observed}")
            await asyncio.sleep(0.01)

    try:
        now = datetime.now(timezone.utc) - timedelta(minutes=1)
        async with engine.begin() as connection:
            await connection.run_sync(OperatorSession.__table__.create)
        async with setup_factory() as session:
            session.add(
                OperatorSession(
                    id="auth-contention-session",
                    token_hash="not-used-by-this-test",
                    last_seen_at=now,
                    idle_expires_at=now + timedelta(minutes=5),
                    absolute_expires_at=now + timedelta(hours=1),
                )
            )
            await session.commit()
        # Create the bounded pool before acquiring the separate writer lock.
        # New SQLite connections configure journal mode during connect, which
        # itself may need a write lock and would confound this test.
        warm_connections = []
        for _ in range(20):
            warm_connections.append(await engine.connect())
        for connection in warm_connections:
            await connection.close()
        lock_engine = create_async_engine(
            f"sqlite+aiosqlite:///{db_path}",
            connect_args={"check_same_thread": False},
            poolclass=NullPool,
        )
        event.listen(lock_engine.sync_engine, "connect", _configure_sqlite_connection)
        lock_connection = await lock_engine.connect()
        await lock_connection.exec_driver_sql("BEGIN IMMEDIATE")
        auth_tasks = [
            asyncio.create_task(
                authenticate_session("auth-contention-session"),
                name=f"auth-contention-{index}",
            )
            for index in range(40)
        ]
        await asyncio.wait_for(initial_reads_completed.wait(), timeout=10)
        await _wait_for_update()
        # Give the DBAPI writer a scheduling turn while the independent
        # BEGIN IMMEDIATE connection still owns SQLite's writer reservation.
        await asyncio.sleep(0.05)
        assert all(not task.done() for task in auth_tasks), "a touch completed before the writer lock was released"
        with counter_lock:
            observed_updates = updates_entered
        assert observed_updates == 1, "stale touch writes must remain coalesced under contention"
        assert max_checked_out == 20
        assert max_checked_out <= 20
        await _release_lock()

        results = await asyncio.wait_for(
            asyncio.gather(*auth_tasks, return_exceptions=True),
            timeout=15,
        )
        failures = [result for result in results if isinstance(result, BaseException)]
        assert failures == []
        assert len(results) == 40
        with counter_lock:
            assert updates_entered == 1

        async with setup_factory() as session:
            record = await session.get(OperatorSession, "auth-contention-session")
            assert record is not None
            last_seen_at = record.last_seen_at
            idle_expires_at = record.idle_expires_at
            if last_seen_at.tzinfo is None:
                last_seen_at = last_seen_at.replace(tzinfo=timezone.utc)
            if idle_expires_at.tzinfo is None:
                idle_expires_at = idle_expires_at.replace(tzinfo=timezone.utc)
            assert last_seen_at > now
            assert idle_expires_at > now
            assert record.revoked_at is None
    finally:
        try:
            await asyncio.wait_for(_release_lock(), timeout=5)
        except (Exception, asyncio.CancelledError):
            pass
        if "auth_tasks" in locals():
            for task in auth_tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*auth_tasks, return_exceptions=True)
        if lock_connection is not None:
            await lock_connection.close()
        if lock_engine is not None:
            await lock_engine.dispose()
        await engine.dispose()


@pytest.mark.asyncio
async def test_file_backed_sqlite_auth_touch_is_coalesced(
    tmp_path,
    monkeypatch,
):
    # Bound schema setup, the contention barrier, durable readback, and cleanup.
    await asyncio.wait_for(
        _run_file_backed_sqlite_auth_touch_is_coalesced(
            tmp_path,
            monkeypatch,
        ),
        timeout=60,
    )
