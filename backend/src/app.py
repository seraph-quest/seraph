import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from config.settings import settings
from src.db import init_db, close_db
from src.llm_logger import init_llm_logging
from src.memory.soul import ensure_soul_exists
from src.scheduler.engine import init_scheduler, shutdown_scheduler
from src.skills.manager import skill_manager
from src.tools.mcp_manager import mcp_manager

limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"])


def _seed_default_skills(defaults_dir: str, skills_dir: str) -> None:
    """Copy bundled default skills to workspace if they don't already exist."""
    import shutil
    bundled_skills = os.path.join(defaults_dir, "skills")
    if not os.path.isdir(bundled_skills):
        return
    for filename in os.listdir(bundled_skills):
        if not filename.endswith(".md"):
            continue
        dst = os.path.join(skills_dir, filename)
        if not os.path.exists(dst):
            shutil.copy2(os.path.join(bundled_skills, filename), dst)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    ensure_soul_exists()
    init_llm_logging()
    # Load persisted settings before scheduler starts
    try:
        from src.api.profile import get_or_create_profile
        from src.observer.manager import context_manager
        profile = await get_or_create_profile()
        if profile.interruption_mode:
            context_manager.update_interruption_mode(profile.interruption_mode)
        if profile.capture_mode:
            context_manager.update_capture_mode(profile.capture_mode)
    except Exception:
        import logging
        logging.getLogger(__name__).warning("Failed to load persisted settings", exc_info=True)
    init_scheduler()
    try:
        from src.observer.manager import context_manager
        await context_manager.refresh()
    except Exception:
        import logging
        logging.getLogger(__name__).warning("Initial context refresh failed", exc_info=True)
    defaults_dir = os.path.join(os.path.dirname(__file__), "defaults")
    mcp_config = os.path.join(settings.workspace_dir, "mcp-servers.json")
    if not os.path.exists(mcp_config):
        default_config = os.path.join(defaults_dir, "mcp-servers.default.json")
        if os.path.isfile(default_config):
            import shutil
            os.makedirs(os.path.dirname(mcp_config), exist_ok=True)
            shutil.copy2(default_config, mcp_config)
    stdio_proxy_config = os.path.join(settings.workspace_dir, "stdio-proxies.json")
    if not os.path.exists(stdio_proxy_config):
        default_proxy_config = os.path.join(defaults_dir, "stdio-proxies.default.json")
        if os.path.isfile(default_proxy_config):
            import shutil
            os.makedirs(os.path.dirname(stdio_proxy_config), exist_ok=True)
            shutil.copy2(default_proxy_config, stdio_proxy_config)
    mcp_manager.load_config(mcp_config)
    skills_dir = os.path.join(settings.workspace_dir, "skills")
    os.makedirs(skills_dir, exist_ok=True)
    _seed_default_skills(defaults_dir, skills_dir)
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
