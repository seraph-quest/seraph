import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from config.settings import settings
from src.db import init_db, close_db
from src.memory.soul import ensure_soul_exists
from src.scheduler.engine import init_scheduler, shutdown_scheduler
from src.skills.manager import skill_manager
from src.tools.mcp_manager import mcp_manager

limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"])


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
    mcp_config = os.path.join(settings.workspace_dir, "mcp-servers.json")
    mcp_manager.load_config(mcp_config)
    skills_dir = os.path.join(settings.workspace_dir, "skills")
    os.makedirs(skills_dir, exist_ok=True)
    skill_manager.init(skills_dir)
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

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

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
