"""Canonical SQLite inventory and workspace round-trip coverage for M1."""

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine as create_sync_engine
from sqlalchemy.ext.asyncio import create_async_engine

from config.settings import settings
from src.db import engine as db_engine
from src.db.engine import OPERATOR_REQUIRED_TABLES
from src.db.models import Goal, SQLModel
from src.workspace import (
    WorkspaceConfig,
    WorkspaceDatabaseObjectSpec,
    WorkspaceIdentity,
    WorkspacePathSpec,
    WorkspaceRootKind,
    WorkspaceStateClass,
    WorkspaceStateRegistry,
    backup_workspace,
    restore_workspace,
)


def test_work_board_tables_are_registered_in_canonical_metadata():
    expected = {
        "work_board_tasks",
        "work_board_attempts",
        "work_board_links",
        "work_board_comments",
        "work_board_events",
    }
    assert expected.issubset(SQLModel.metadata.tables)
    assert expected.issubset(set(OPERATOR_REQUIRED_TABLES))


def _board_workspace(tmp_path: Path) -> tuple[Path, WorkspaceStateRegistry]:
    root = tmp_path / "board-workspace"
    root.mkdir()
    (root / ".seraph-synthetic-workspace").write_bytes(b"seraph-synthetic-workspace-v1\n")
    (root / "soul.md").write_text("board test\n", encoding="utf-8")
    (root / "artifacts").mkdir()
    (root / "extensions").mkdir()
    (root / ".vault-key").write_text("vault-test-secret\n", encoding="utf-8")
    (root / "derived").mkdir()
    (root / "cache").mkdir()
    (root / "tmp").mkdir()
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
            """
        )
    config = WorkspaceConfig(
        identity=WorkspaceIdentity("synthetic-board", root, WorkspaceRootKind.SYNTHETIC_FIXTURE),
        declared_paths=(
            WorkspacePathSpec("seraph.db", WorkspaceStateClass.CANONICAL),
            WorkspacePathSpec("soul.md", WorkspaceStateClass.CANONICAL),
            WorkspacePathSpec("artifacts", WorkspaceStateClass.CANONICAL),
            WorkspacePathSpec("extensions", WorkspaceStateClass.CANONICAL),
            WorkspacePathSpec(".vault-key", WorkspaceStateClass.SECRET_RECOVERY),
            WorkspacePathSpec("derived", WorkspaceStateClass.DERIVED),
            WorkspacePathSpec("cache", WorkspaceStateClass.CACHE),
            WorkspacePathSpec("tmp", WorkspaceStateClass.DISPOSABLE),
            WorkspacePathSpec(".seraph-synthetic-workspace", WorkspaceStateClass.DISPOSABLE),
        ),
        expected_database_objects=(
            WorkspaceDatabaseObjectSpec("work_board_tasks", "table", WorkspaceStateClass.CANONICAL),
            WorkspaceDatabaseObjectSpec("work_board_attempts", "table", WorkspaceStateClass.CANONICAL),
            WorkspaceDatabaseObjectSpec("work_board_links", "table", WorkspaceStateClass.CANONICAL),
            WorkspaceDatabaseObjectSpec("work_board_comments", "table", WorkspaceStateClass.CANONICAL),
            WorkspaceDatabaseObjectSpec("work_board_events", "table", WorkspaceStateClass.CANONICAL),
        ),
    )
    return root, WorkspaceStateRegistry(config)


def test_backup_restore_retains_work_board_task_and_event_records(tmp_path):
    root, registry = _board_workspace(tmp_path)
    inventory = registry.build_manifest()["database"]["tables"]
    assert {
        "work_board_tasks",
        "work_board_attempts",
        "work_board_links",
        "work_board_comments",
        "work_board_events",
    }.issubset({table["name"] for table in inventory})
    archive = Path(backup_workspace(root, registry=registry)["archive_path"])
    with sqlite3.connect(root / "seraph.db") as connection:
        connection.execute("DELETE FROM work_board_events")
        connection.execute("DELETE FROM work_board_comments")
        connection.execute("DELETE FROM work_board_links")
        connection.execute("DELETE FROM work_board_attempts")
        connection.execute("DELETE FROM work_board_tasks")
    restore_workspace(
        root,
        archive,
        registry=registry,
        confirm=True,
        restore_id="restore-board-records",
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
    assert task == ("task-roundtrip", "Persisted board task", "triage")
    assert event == ("task-roundtrip", "task.created")
    assert attempt == ("attempt-roundtrip", "task-roundtrip", "executor.test")
    assert link == ("link-roundtrip", "task-parent", "task-roundtrip")
    assert comment == ("comment-roundtrip", "task-roundtrip", "Persisted comment")


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
    assert {
        "work_board_tasks",
        "work_board_attempts",
        "work_board_links",
        "work_board_comments",
        "work_board_events",
    }.issubset(tables)
    assert existing == ("legacy-goal", "Existing goal")
