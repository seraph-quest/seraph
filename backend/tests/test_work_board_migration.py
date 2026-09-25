"""Canonical SQLite inventory and workspace round-trip coverage for M1."""

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy.dialects import sqlite
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.schema import CreateTable

from config.settings import settings
from src.db import engine as db_engine
from src.db.engine import OPERATOR_REQUIRED_TABLES
from src.db.models import (
    Goal,
    SQLModel,
    WorkBoardAttempt,
    WorkBoardHandoff,
    WorkBoardLink,
    WorkBoardProposal,
    WorkBoardTask,
)
from src.workspace import (
    backup_workspace,
    canonical_workspace_registry,
    production_workspace_inventory,
    restore_workspace,
)
from src.workspace.lifecycle import lifecycle_fence_marker


def test_work_board_tables_are_registered_in_canonical_metadata():
    expected = {
        "work_board_tasks",
        "work_board_attempts",
        "work_board_links",
        "work_board_comments",
        "work_board_events",
        "work_board_review_intents",
        "work_board_handoffs",
        "work_board_proposals",
    }
    assert expected.issubset(SQLModel.metadata.tables)
    assert expected.issubset(set(OPERATOR_REQUIRED_TABLES))


def test_work_board_task_sqlite_ddl_uses_autoincrement_sequence():
    ddl = str(CreateTable(SQLModel.metadata.tables["work_board_tasks"]).compile(dialect=sqlite.dialect()))
    normalized = " ".join(ddl.upper().split())
    assert "CREATION_SEQUENCE INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT" in normalized


def _board_workspace(tmp_path: Path):
    """Build a canonical production-shaped workspace for lifecycle proof."""
    root = tmp_path / "board-workspace"
    root.mkdir()
    registry = canonical_workspace_registry(root)
    directory_paths = {
        "artifacts",
        "extensions",
        "skills",
        "workflows",
        "runbooks",
        "plans",
        "reports",
        "notes",
        "local-runtime-profile-receipts",
        "lance",
        ".seraph-extension-snapshots",
        "cache",
        "tmp",
    }
    for spec in registry.config.declared_paths:
        path = root / spec.logical_path
        if spec.logical_path in directory_paths:
            path.mkdir(parents=True, exist_ok=True)
        elif spec.logical_path == ".vault-key":
            path.write_text("vault-test-secret\n", encoding="utf-8")
        elif spec.logical_path == "seraph.db":
            continue
        elif spec.logical_path == ".seraph-workspace-maintenance.lock":
            path.write_bytes(b"")
        elif spec.logical_path == "soul.md":
            path.write_text("board test\n", encoding="utf-8")
        elif spec.logical_path != "google_calendar_token.json":
            path.write_text("{}\n", encoding="utf-8")
    with sqlite3.connect(root / "seraph.db") as connection:
        connection.executescript(
            """
            CREATE TABLE work_board_tasks (
                creation_sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE work_board_attempts (
                attempt_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                executor_id TEXT NOT NULL
            );
            CREATE TABLE work_board_links (
                link_id TEXT PRIMARY KEY,
                owner_principal_id TEXT NOT NULL,
                owner_session_id TEXT NOT NULL,
                parent_task_id TEXT NOT NULL,
                child_task_id TEXT NOT NULL
            );
            CREATE TABLE work_board_comments (
                comment_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                owner_principal_id TEXT NOT NULL,
                owner_session_id TEXT NOT NULL,
                author_principal_id TEXT NOT NULL,
                author_session_id TEXT NOT NULL,
                body TEXT NOT NULL
            );
            CREATE TABLE work_board_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            );
            CREATE TABLE work_board_review_intents (
                intent_id TEXT PRIMARY KEY,
                owner_principal_id TEXT NOT NULL,
                owner_session_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                attempt_id TEXT NOT NULL,
                workflow_run_id TEXT NOT NULL,
                fencing_token INTEGER NOT NULL,
                task_revision INTEGER NOT NULL,
                request_digest TEXT NOT NULL,
                evidence_refs_json TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE work_board_handoffs (
                handoff_id TEXT PRIMARY KEY,
                schema_version TEXT NOT NULL,
                owner_principal_id TEXT NOT NULL,
                owner_session_id TEXT NOT NULL,
                parent_task_id TEXT NOT NULL,
                child_task_id TEXT NOT NULL,
                link_id TEXT NOT NULL,
                source_attempt_id TEXT NOT NULL,
                workflow_run_id TEXT NOT NULL,
                source_task_revision INTEGER NOT NULL,
                summary TEXT NOT NULL,
                artifact_refs_json TEXT NOT NULL,
                result_refs_json TEXT NOT NULL,
                verification_json TEXT NOT NULL,
                risks_json TEXT NOT NULL
            );
            CREATE TABLE work_board_proposals (
                proposal_id TEXT PRIMARY KEY,
                owner_principal_id TEXT NOT NULL,
                owner_session_id TEXT NOT NULL,
                parent_task_id TEXT NOT NULL,
                parent_revision INTEGER NOT NULL,
                goal_revision INTEGER NOT NULL,
                kind TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                request_digest TEXT NOT NULL,
                capability_id TEXT NOT NULL,
                capability_version TEXT NOT NULL,
                authority_digest TEXT NOT NULL,
                grant_revision INTEGER NOT NULL,
                input_digest TEXT NOT NULL,
                route_id TEXT NOT NULL,
                admission_job_id TEXT NOT NULL,
                effect_id_digest TEXT NOT NULL,
                provider_contact_started INTEGER NOT NULL,
                provider_contact_state TEXT NOT NULL,
                status TEXT NOT NULL,
                proposal_json TEXT NOT NULL
            );
            INSERT INTO work_board_tasks(task_id, title, status)
                VALUES ('task-roundtrip', 'Persisted board task', 'triage');
            INSERT INTO work_board_attempts(attempt_id, task_id, executor_id)
                VALUES ('attempt-roundtrip', 'task-roundtrip', 'executor.test');
            INSERT INTO work_board_links(
                link_id, owner_principal_id, owner_session_id, parent_task_id, child_task_id
            ) VALUES (
                'link-roundtrip', 'operator:test', 'session:test', 'task-parent', 'task-roundtrip'
            );
            INSERT INTO work_board_comments(
                comment_id, task_id, owner_principal_id, owner_session_id,
                author_principal_id, author_session_id, body
            ) VALUES (
                'comment-roundtrip', 'task-roundtrip', 'operator:test', 'session:test',
                'operator:test', 'session:test', 'Persisted comment'
            );
            INSERT INTO work_board_events(task_id, kind, metadata_json)
                VALUES ('task-roundtrip', 'task.created', '{}');
            INSERT INTO work_board_review_intents(
                intent_id, owner_principal_id, owner_session_id, task_id,
                attempt_id, workflow_run_id, fencing_token, task_revision,
                request_digest, evidence_refs_json, status
            ) VALUES (
                'intent-roundtrip', 'operator:test', 'session:test',
                'task-roundtrip', 'attempt-roundtrip', 'run-roundtrip',
                17, 4, 'request-digest-roundtrip', '["readback-roundtrip"]',
                'projected'
            );
            INSERT INTO work_board_handoffs(
                handoff_id, schema_version, owner_principal_id, owner_session_id,
                parent_task_id, child_task_id, link_id, source_attempt_id,
                workflow_run_id, source_task_revision, summary,
                artifact_refs_json, result_refs_json, verification_json, risks_json
            ) VALUES (
                'handoff-roundtrip', 'work_board_handoff.v1', 'operator:test',
                'session:test', 'task-parent', 'task-roundtrip',
                'link-roundtrip', 'attempt-roundtrip', 'run-roundtrip', 4,
                '{"kind":"verified_task_handoff"}',
                '[{"artifact_id":"artifact-roundtrip"}]',
                '[{"readback_id":"readback-roundtrip"}]',
                '{"status":"verified","workflow_run_id":"run-roundtrip","readback_id":"readback-roundtrip","verified_at":"2026-09-25T00:00:00+00:00","content_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}',
                '["verification_scope_bounded"]'
            );
            INSERT INTO work_board_proposals(
                proposal_id, owner_principal_id, owner_session_id, parent_task_id,
                parent_revision, goal_revision, kind, idempotency_key,
                request_digest, capability_id, capability_version,
                authority_digest, grant_revision, input_digest, route_id,
                admission_job_id, effect_id_digest, provider_contact_started,
                provider_contact_state, status, proposal_json
            ) VALUES (
                'proposal-roundtrip', 'operator:test', 'session:test',
                'task-roundtrip', 4, 2, 'specify', 'proposal-key-roundtrip',
                'request-digest-proposal', 'strategist_agent', '1',
                'authority-digest-roundtrip', 9, 'input-digest-roundtrip',
                'strategist_agent', 'job-roundtrip', 'effect-digest-roundtrip',
                0, 'not_started', 'blocked', '{"proposed_tasks":[]}'
            );
            """
        )
    return root, registry


def test_backup_restore_retains_work_board_task_and_event_records(tmp_path):
    root, registry = _board_workspace(tmp_path)
    inventory_receipt = production_workspace_inventory(root)
    inventory = inventory_receipt["manifest"]["database"]["tables"]
    assert {
        "work_board_tasks",
        "work_board_attempts",
        "work_board_links",
        "work_board_comments",
        "work_board_events",
    }.issubset({table["name"] for table in inventory})
    assert registry.classify_path("seraph.db").value == "canonical"
    with lifecycle_fence_marker():
        archive = Path(backup_workspace(root, registry=registry)["archive_path"])
    with sqlite3.connect(root / "seraph.db") as connection:
        connection.execute("DELETE FROM work_board_events")
        connection.execute("DELETE FROM work_board_comments")
        connection.execute("DELETE FROM work_board_links")
        connection.execute("DELETE FROM work_board_attempts")
        connection.execute("DELETE FROM work_board_tasks")
        connection.execute("DELETE FROM work_board_review_intents")
        connection.execute("DELETE FROM work_board_handoffs")
        connection.execute("DELETE FROM work_board_proposals")
    with lifecycle_fence_marker():
        restore_workspace(
            root,
            archive,
            registry=registry,
            confirm=True,
            restore_id="restore-board-records",
            reconcile_restore=lambda **_kwargs: {
                "status": "ready",
                "derived_rebuild": {
                    "status": "clean_targets_recreated",
                    "rebuilt_directories": [],
                    "stored_derived_files": 0,
                },
                "authority_invalidation": {
                    "status": "applied",
                    "tables_present": [],
                    "operator_sessions_invalidated": 0,
                    "workflow_authority_rows_blocked": 0,
                },
                "token_invalidation": {
                    "status": "applied",
                    "optional_credentials_invalidated": [],
                },
                "secret_values_included": False,
            },
        )
    with sqlite3.connect(root / "seraph.db") as connection:
        task = connection.execute(
            "SELECT task_id, title, status FROM work_board_tasks"
        ).fetchone()
        event = connection.execute(
            "SELECT task_id, kind FROM work_board_events"
        ).fetchone()
        attempt = connection.execute(
            "SELECT attempt_id, task_id, executor_id FROM work_board_attempts"
        ).fetchone()
        link = connection.execute(
            "SELECT link_id, parent_task_id, child_task_id FROM work_board_links"
        ).fetchone()
        comment = connection.execute(
            "SELECT comment_id, task_id, body FROM work_board_comments"
        ).fetchone()
        review_intent = connection.execute(
            "SELECT owner_principal_id, owner_session_id, workflow_run_id, fencing_token, request_digest FROM work_board_review_intents"
        ).fetchone()
        handoff = connection.execute(
            "SELECT owner_principal_id, owner_session_id, workflow_run_id, source_attempt_id, verification_json FROM work_board_handoffs"
        ).fetchone()
        proposal = connection.execute(
            "SELECT owner_principal_id, owner_session_id, admission_job_id, effect_id_digest, authority_digest, input_digest FROM work_board_proposals"
        ).fetchone()
    assert task == ("task-roundtrip", "Persisted board task", "triage")
    assert event == ("task-roundtrip", "task.created")
    assert attempt == ("attempt-roundtrip", "task-roundtrip", "executor.test")
    assert link == ("link-roundtrip", "task-parent", "task-roundtrip")
    assert comment == ("comment-roundtrip", "task-roundtrip", "Persisted comment")
    assert review_intent == (
        "operator:test",
        "session:test",
        "run-roundtrip",
        17,
        "request-digest-roundtrip",
    )
    assert handoff == (
        "operator:test",
        "session:test",
        "run-roundtrip",
        "attempt-roundtrip",
        '{"status":"verified","workflow_run_id":"run-roundtrip","readback_id":"readback-roundtrip","verified_at":"2026-09-25T00:00:00+00:00","content_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}',
    )
    assert proposal == (
        "operator:test",
        "session:test",
        "job-roundtrip",
        "effect-digest-roundtrip",
        "authority-digest-roundtrip",
        "input-digest-roundtrip",
    )


@pytest.mark.asyncio
async def test_init_db_additively_creates_board_tables_and_preserves_existing_rows(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "startup-workspace"
    root.mkdir()
    database_path = root / "seraph.db"
    sync_engine = create_sync_engine(f"sqlite:///{database_path}")
    SQLModel.metadata.create_all(sync_engine, tables=[Goal.__table__])
    with sync_engine.begin() as connection:
        connection.execute(
            Goal.__table__.insert().values(
                id="legacy-goal",
                path="/",
                level="daily",
                title="Existing goal",
                status="active",
                domain="productivity",
                revision=1,
                proactive_enabled=False,
            )
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
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        existing = connection.execute(
            "SELECT id, title FROM goals WHERE id = 'legacy-goal'"
        ).fetchone()
        task_ddl = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'work_board_tasks'"
        ).fetchone()[0]
    assert {
        "work_board_tasks",
        "work_board_attempts",
        "work_board_links",
        "work_board_comments",
        "work_board_events",
    }.issubset(tables)
    assert existing == ("legacy-goal", "Existing goal")
    assert "AUTOINCREMENT" in task_ddl.upper()


@pytest.mark.asyncio
async def test_work_board_attempt_migration_adds_restart_safe_handoff_binding(tmp_path):
    database_path = tmp_path / "work-board-attempt-upgrade.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE work_board_attempts (attempt_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, cancel_requested_at DATETIME)"
        )
        connection.execute(
            "INSERT INTO work_board_attempts(attempt_id, task_id) VALUES ('legacy-attempt', 'legacy-task')"
        )
    async_engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )
    try:
        async with async_engine.begin() as connection:
            await db_engine._ensure_work_board_columns(connection)
    finally:
        await async_engine.dispose()
    with sqlite3.connect(database_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(work_board_attempts)")}
        attempt = connection.execute(
            "SELECT parent_handoff_context_json, parent_handoff_digest FROM work_board_attempts WHERE attempt_id = 'legacy-attempt'"
        ).fetchone()
    assert {"parent_handoff_context_json", "parent_handoff_digest"}.issubset(columns)
    assert attempt == ("[]", None)


@pytest.mark.asyncio
async def test_proposal_idempotency_migration_scopes_index_to_parent_revision(tmp_path):
    database_path = tmp_path / "proposal-index-upgrade.db"
    sync_engine = create_sync_engine(f"sqlite:///{database_path}")
    SQLModel.metadata.create_all(
        sync_engine,
        tables=[
            WorkBoardTask.__table__,
            WorkBoardAttempt.__table__,
            WorkBoardProposal.__table__,
            WorkBoardHandoff.__table__,
        ],
    )
    with sync_engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX ux_work_board_proposals_idempotency")
        connection.exec_driver_sql(
            """
            CREATE UNIQUE INDEX ux_work_board_proposals_idempotency
            ON work_board_proposals (
                owner_principal_id,
                owner_session_id,
                parent_task_id,
                kind,
                idempotency_key
            )
            """
        )
    sync_engine.dispose()

    async_engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )
    try:
        async with async_engine.begin() as connection:
            await db_engine._ensure_work_board_indexes(connection)
    finally:
        await async_engine.dispose()

    with sqlite3.connect(database_path) as connection:
        columns = [
            row[2]
            for row in connection.execute(
                "PRAGMA index_info(ux_work_board_proposals_idempotency)"
            )
        ]

    assert columns == [
        "owner_principal_id",
        "owner_session_id",
        "parent_task_id",
        "parent_revision",
        "kind",
        "idempotency_key",
    ]


@pytest.mark.asyncio
async def test_handoff_migration_adds_and_only_exactly_backfills_source_attempt(tmp_path):
    database_path = tmp_path / "handoff-source-attempt-upgrade.db"
    sync_engine = create_sync_engine(f"sqlite:///{database_path}")
    SQLModel.metadata.create_all(
        sync_engine,
        tables=[
            WorkBoardTask.__table__,
            WorkBoardAttempt.__table__,
            WorkBoardLink.__table__,
            WorkBoardProposal.__table__,
        ],
    )
    with sync_engine.begin() as connection:
        connection.exec_driver_sql(
            """
            CREATE TABLE work_board_handoffs (
                handoff_id TEXT PRIMARY KEY,
                owner_principal_id TEXT NOT NULL,
                owner_session_id TEXT NOT NULL,
                parent_task_id TEXT NOT NULL,
                child_task_id TEXT NOT NULL,
                workflow_run_id TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                artifact_refs_json TEXT NOT NULL DEFAULT '[]',
                result_refs_json TEXT NOT NULL DEFAULT '[]',
                verification_json TEXT NOT NULL DEFAULT '{}',
                created_at DATETIME
            )
            """
        )
        connection.execute(
            WorkBoardTask.__table__.insert(),
            [
                {
                    "task_id": "handoff-parent",
                    "owner_principal_id": "operator:migration",
                    "owner_session_id": "session:migration",
                    "goal_id": "goal:migration",
                    "idempotency_key": "handoff-parent-key",
                    "status": "done",
                },
                {
                    "task_id": "handoff-child",
                    "owner_principal_id": "operator:migration",
                    "owner_session_id": "session:migration",
                    "goal_id": "goal:migration",
                    "idempotency_key": "handoff-child-key",
                    "status": "todo",
                },
            ],
        )
        connection.execute(
            WorkBoardAttempt.__table__.insert().values(
                attempt_id="attempt-parent-exact",
                task_id="handoff-parent",
                workflow_run_id="run-parent-exact",
                executor_id="executor:test",
            )
        )
        connection.execute(
            WorkBoardLink.__table__.insert().values(
                link_id="link-parent-child",
                owner_principal_id="operator:migration",
                owner_session_id="session:migration",
                parent_task_id="handoff-parent",
                child_task_id="handoff-child",
            )
        )
        connection.exec_driver_sql(
            """
            INSERT INTO work_board_handoffs(
                handoff_id, owner_principal_id, owner_session_id,
                parent_task_id, child_task_id, workflow_run_id
            ) VALUES
                ('handoff-exact', 'operator:migration', 'session:migration',
                 'handoff-parent', 'handoff-child', 'run-parent-exact'),
                ('handoff-unproven', 'operator:migration', 'session:migration',
                 'handoff-parent', 'handoff-child', 'run-not-found')
            """
        )
    sync_engine.dispose()

    async_engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        connect_args={"check_same_thread": False},
    )
    try:
        async with async_engine.begin() as connection:
            await db_engine._ensure_work_board_columns(connection)
            await db_engine._ensure_work_board_indexes(connection)
    finally:
        await async_engine.dispose()

    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]: {"notnull": row[3], "default": row[4]}
            for row in connection.execute("PRAGMA table_info(work_board_handoffs)")
        }
        rows = dict(
            connection.execute(
                "SELECT handoff_id, source_attempt_id FROM work_board_handoffs"
            ).fetchall()
        )
    assert columns["source_attempt_id"]["notnull"] == 1
    assert rows == {
        "handoff-exact": "attempt-parent-exact",
        "handoff-unproven": "",
    }
