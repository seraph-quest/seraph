import os
from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

os.environ.setdefault("OPENROUTER_API_KEY", "test-key")
os.environ.setdefault("WORKSPACE_DIR", "/tmp/seraph-test")

from src.app import create_app

# Every place get_session is imported — use the local attribute name.
_PATCH_TARGETS = [
    "src.db.engine.get_session",
    "src.agent.session.get_session",
    "src.goals.repository.get_session",
    "src.api.profile.get_db",  # aliased: `import get_session as get_db`
    "src.api.settings.get_db",  # aliased: `import get_session as get_db`
    "src.scheduler.jobs.memory_consolidation.get_session",
    "src.observer.insight_queue.get_session",
    "src.vault.repository.get_session",
]


# ── In-memory async DB fixture ──────────────────────────

@pytest_asyncio.fixture
async def async_db():
    """Provide an in-memory SQLite engine with all tables created.

    Patches ``get_session`` in every module that imports it so the test
    database is used transparently.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    @asynccontextmanager
    async def _get_session():
        async with factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    patches = []
    for target in _PATCH_TARGETS:
        p = patch(target, _get_session)
        p.start()
        patches.append(p)

    yield _get_session

    for p in patches:
        p.stop()
    await engine.dispose()


# ── App / HTTP client fixtures ──────────────────────────

@pytest.fixture
def app():
    return create_app()


@pytest_asyncio.fixture
async def client(app, async_db):
    """Async HTTP test client backed by the in-memory DB."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def mock_agent():
    agent = MagicMock()
    agent.run.return_value = "Mocked agent response"
    return agent
