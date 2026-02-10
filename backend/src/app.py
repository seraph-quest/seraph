from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config.settings import settings
from src.db import init_db, close_db
from src.memory.soul import ensure_soul_exists
from src.scheduler.engine import init_scheduler, shutdown_scheduler
from src.tools.mcp_manager import mcp_manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    ensure_soul_exists()
    # Load persisted interruption mode before scheduler starts
    try:
        from src.api.profile import get_or_create_profile
        from src.observer.manager import context_manager
        profile = await get_or_create_profile()
        if profile.interruption_mode:
            context_manager.update_interruption_mode(profile.interruption_mode)
    except Exception:
        import logging
        logging.getLogger(__name__).warning("Failed to load persisted interruption mode", exc_info=True)
    init_scheduler()
    try:
        from src.observer.manager import context_manager
        await context_manager.refresh()
    except Exception:
        import logging
        logging.getLogger(__name__).warning("Initial context refresh failed", exc_info=True)
    if settings.things_mcp_url:
        mcp_manager.connect("things", settings.things_mcp_url)
    if settings.github_mcp_url:
        mcp_manager.connect("github", settings.github_mcp_url)
    yield
    shutdown_scheduler()
    mcp_manager.disconnect_all()
    await close_db()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Seraph AI Assistant",
        version="0.1.0",
        debug=settings.debug,
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    from src.api.router import api_router

    app.include_router(api_router)

    return app
