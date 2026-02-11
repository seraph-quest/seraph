import json
from contextlib import asynccontextmanager, ExitStack
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel
from starlette.testclient import TestClient

# Ensure models are registered in SQLModel.metadata before create_all
import src.db.models  # noqa: F401


def _make_sync_client_with_db():
    """Create a sync TestClient with an in-memory DB patched in.

    Patches init_db (so the lifespan creates tables on the test engine),
    close_db (no-op), and get_session everywhere (so queries use the test DB).

    Returns (client, cleanup_list). The TestClient is already entered as a
    context manager so the lifespan has run. Call p.stop() on each item in
    cleanup_list when done.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def _get_session():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def _test_init_db():
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)

    async def _test_close_db():
        await engine.dispose()

    targets = [
        "src.db.engine.get_session",
        "src.agent.session.get_session",
        "src.goals.repository.get_session",
        "src.api.profile.get_db",
    ]
    patches = [patch(t, _get_session) for t in targets]
    patches.append(patch("src.app.init_db", _test_init_db))
    patches.append(patch("src.app.close_db", _test_close_db))
    patches.append(patch("src.app.init_scheduler", return_value=None))
    patches.append(patch("src.app.shutdown_scheduler"))
    for p in patches:
        p.start()

    from src.app import create_app
    app = create_app()

    # Enter TestClient as context manager so the lifespan runs (init_db, etc.)
    stack = ExitStack()
    client = stack.enter_context(TestClient(app))

    # Return stack in patches list so cleanup exits the context manager too
    return client, patches, stack


class TestWebSocket:
    def test_websocket_ping(self):
        client, patches, stack = _make_sync_client_with_db()
        try:
            with client.websocket_connect("/ws/chat") as ws:
                # The server sends a proactive welcome on connect; drain it
                _ = ws.receive_text()
                ws.send_text(json.dumps({"type": "ping"}))
                resp = json.loads(ws.receive_text())
                assert resp["type"] == "pong"
        finally:
            stack.close()
            for p in patches:
                p.stop()

    def test_websocket_invalid_json(self):
        client, patches, stack = _make_sync_client_with_db()
        try:
            with client.websocket_connect("/ws/chat") as ws:
                # Drain the welcome message
                _ = ws.receive_text()

                ws.send_text("not json")
                resp = json.loads(ws.receive_text())
                assert resp["type"] == "error"
        finally:
            stack.close()
            for p in patches:
                p.stop()

    def test_websocket_skip_onboarding(self):
        client, patches, stack = _make_sync_client_with_db()
        try:
            with client.websocket_connect("/ws/chat") as ws:
                _ = ws.receive_text()
                ws.send_text(json.dumps({"type": "skip_onboarding"}))
                resp = json.loads(ws.receive_text())
                assert resp["type"] == "final"
                assert "skipped" in resp["content"].lower()
        finally:
            stack.close()
            for p in patches:
                p.stop()
