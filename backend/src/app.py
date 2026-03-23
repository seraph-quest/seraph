import os
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from config.settings import settings
from src.db import init_db, close_db
from src.extensions.registry import default_manifest_roots_for_workspace
from src.llm_logger import init_llm_logging
from src.memory.soul import ensure_soul_exists
from src.runbooks.manager import runbook_manager
from src.scheduler.engine import init_scheduler, shutdown_scheduler, sync_scheduled_jobs
from src.skills.manager import skill_manager
from src.starter_packs.manager import starter_pack_manager
from src.tools.mcp_manager import mcp_manager
from src.workflows.manager import workflow_manager

limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"])
_LOCAL_DEV_ORIGIN_REGEX = r"https?://(localhost|127\.0\.0\.1)(:\d+)?$"


def _runtime_provider_label() -> str:
    model = settings.default_model.strip()
    api_base = settings.llm_api_base.strip()
    if model.startswith("openrouter/") or "openrouter" in api_base:
        return "openrouter"
    if model.startswith("ollama/") or settings.local_model.strip().startswith("ollama/"):
        return "local"
    if api_base:
        parsed = urlparse(api_base)
        if parsed.netloc:
            return parsed.netloc
    if "/" in model:
        return model.split("/", 1)[0]
    return "unknown"


def _runtime_model_label(model: str) -> str:
    normalized = model.strip()
    if not normalized:
        return "unknown"
    return normalized.split("/")[-1]

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
        if profile.tool_policy_mode:
            context_manager.update_tool_policy_mode(profile.tool_policy_mode)
        if profile.mcp_policy_mode:
            context_manager.update_mcp_policy_mode(profile.mcp_policy_mode)
        if profile.approval_mode:
            context_manager.update_approval_mode(profile.approval_mode)
    except Exception:
        import logging
        logging.getLogger(__name__).warning("Failed to load persisted settings", exc_info=True)
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
    extensions_dir = os.path.join(settings.workspace_dir, "extensions")
    os.makedirs(extensions_dir, exist_ok=True)
    manifest_roots = default_manifest_roots_for_workspace(settings.workspace_dir)
    skills_dir = os.path.join(settings.workspace_dir, "skills")
    os.makedirs(skills_dir, exist_ok=True)
    skill_manager.init(skills_dir, manifest_roots=manifest_roots)
    runbooks_dir = os.path.join(settings.workspace_dir, "runbooks")
    os.makedirs(runbooks_dir, exist_ok=True)
    runbook_manager.init(runbooks_dir, manifest_roots=manifest_roots)
    workflows_dir = os.path.join(settings.workspace_dir, "workflows")
    os.makedirs(workflows_dir, exist_ok=True)
    workflow_manager.init(workflows_dir, manifest_roots=manifest_roots)
    starter_pack_manager.init(
        os.path.join(settings.workspace_dir, "starter-packs.json"),
        manifest_roots=manifest_roots,
    )
    init_scheduler()
    await sync_scheduled_jobs()
    try:
        from src.observer.manager import context_manager
        await context_manager.refresh()
    except Exception:
        import logging
        logging.getLogger(__name__).warning("Initial context refresh failed", exc_info=True)
    yield
    shutdown_scheduler()
    mcp_manager.disconnect_all()
    await close_db()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Seraph AI Assistant",
        version="2026.3.19",
        debug=settings.debug,
        lifespan=lifespan,
    )

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://localhost:5173"],
        allow_origin_regex=_LOCAL_DEV_ORIGIN_REGEX,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/runtime/status")
    async def runtime_status():
        model = settings.default_model.strip()
        return {
            "version": app.version,
            "build_id": f"SERAPH_PRIME_v{app.version}",
            "provider": _runtime_provider_label(),
            "model": model,
            "model_label": _runtime_model_label(model),
            "api_base": settings.llm_api_base.strip(),
            "timezone": settings.user_timezone,
            "llm_logging_enabled": settings.llm_log_enabled,
        }

    from src.api.router import api_router

    app.include_router(api_router)

    return app
