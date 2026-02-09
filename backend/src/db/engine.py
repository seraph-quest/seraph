import os
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

from config.settings import settings

_db_path = os.path.join(settings.workspace_dir, "seraph.db")
_db_url = f"sqlite+aiosqlite:///{_db_path}"

engine = create_async_engine(
    _db_url,
    echo=settings.debug,
    connect_args={"check_same_thread": False},
)

async_session_factory = sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


async def init_db() -> None:
    """Create all tables on startup."""
    os.makedirs(os.path.dirname(_db_path), exist_ok=True)
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)


async def close_db() -> None:
    """Dispose of the engine on shutdown."""
    await engine.dispose()


@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async DB session."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
