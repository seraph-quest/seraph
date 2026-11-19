"""File-backed accounting database and lifecycle plumbing (#1284).

Moved unchanged from ``tests/test_inference_accounting.py`` so the files that
import it keep one owner; it sets up no spending limit.
"""

from contextlib import asynccontextmanager
import asyncio

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

from config.settings import settings
from src.utils.background import drain_tracked_tasks, join_tracked_threads
from src.workspace.production import ProductionWorkspace
from tests.support.db import install_database


@pytest_asyncio.fixture
async def accounting_db(tmp_path, monkeypatch):
    # Register both canonical SQLModel owners before materializing this file DB.
    # Reviewed-method selection can otherwise register its table during a later
    # test, after create_all, leaving full-metadata readback without that table.
    from src.db import models as _models  # noqa: F401
    from src.db import task_method_models as _task_method_models  # noqa: F401

    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(settings, "workspace_dir", str(root))
    monkeypatch.setenv("SERAPH_WORKSPACE_LIFECYCLE_PATH", str(tmp_path / "deployment-lifecycle"))
    from src.workspace.production import prepare_lifecycle_directory
    prepare_lifecycle_directory(ProductionWorkspace(host_root=root))
    # Partial (#1222): still its own engine, not create_database_engine, so no
    # foreign keys, WAL or busy-timeout pragmas. Several fixtures seeded on it
    # violate foreign keys, which production pragmas enforce. Tracked as a
    # follow-up to #1222 in #1241.
    engine = create_async_engine(f"sqlite+aiosqlite:///{root / 'seraph.db'}", connect_args={"timeout": 5})
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
    @asynccontextmanager
    async def sessions():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise
    # The single database seam (#1222): every get_session caller uses this file.
    install_database(monkeypatch, engine)
    factory.accounting_sessions = sessions
    try:
        yield root, engine, factory
    finally:
        # As the `db` fixture: tracked background work writes to this engine,
        # so it finishes before the engine is disposed (#1223).
        try:
            await drain_tracked_tasks(timeout_seconds=5.0)
            await asyncio.to_thread(join_tracked_threads, timeout_seconds=5.0)
        finally:
            await engine.dispose()
