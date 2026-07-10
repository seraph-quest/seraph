import os
from contextlib import asynccontextmanager
from urllib.parse import urlparse, urlsplit, urlunsplit

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from config.settings import settings
from src.db import init_db, close_db
from src.extensions.registry import default_manifest_roots_for_workspace
from src.llm_logger import init_llm_logging
from src.llm_runtime import effective_runtime_model_id, provider_profile_statuses, provider_profiles, resolve_runtime_profile
from src.memory.soul import ensure_soul_exists
from src.operators.local_codex import ExternalAgentRuntimeRemovedError, reject_legacy_external_agent_model
from src.runbooks.manager import runbook_manager
from src.scheduler.engine import init_scheduler, shutdown_scheduler, sync_scheduled_jobs
from src.skills.manager import skill_manager
from src.starter_packs.manager import starter_pack_manager
from src.tools.mcp_manager import mcp_manager
from src.utils.background import drain_tracked_tasks
from src.vlm_runtime import deferred_vlm_live_probe, effective_vlm_status
from src.workflows.manager import workflow_manager

limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"])
_LOCAL_DEV_ORIGIN_REGEX = r"https?://(localhost|127\.0\.0\.1)(:\d+)?$"


def _safe_runtime_endpoint(value: object) -> str:
    """Return a credential-free absolute HTTP(S) endpoint or blank unsafe legacy input."""
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError:
        return ""
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return ""
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port is not None else host
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path, "", ""))


def _sanitize_runtime_endpoints(value: object, *, key: str = "") -> object:
    if isinstance(value, dict):
        return {
            item_key: _sanitize_runtime_endpoints(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_runtime_endpoints(item, key=key) for item in value]
    endpoint_key = key in {"api_base", "base_url", "backend_url", "default_api_base"} or key.endswith(
        ("_endpoint", "_api_base", "_base_url", "_backend_url")
    )
    return _safe_runtime_endpoint(value) if endpoint_key else value


def _runtime_provider_label(
    model: str | None = None,
    *,
    profile: str | None = None,
    api_base: str | None = None,
) -> str:
    normalized_profile = (profile or "").strip()
    if normalized_profile.startswith("local-gemma-"):
        return "local-gemma"
    model = (model or settings.default_model).strip()
    api_base = (api_base if api_base is not None else settings.llm_api_base).strip()
    if model.startswith("openrouter/") or "openrouter" in api_base:
        return "openrouter"
    if normalized_profile == "local" or model.startswith("ollama/") or settings.local_model.strip().startswith("ollama/"):
        return "local"
    if "127.0.0.1" in api_base or "localhost" in api_base:
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


def _active_chat_runtime_status() -> dict[str, str]:
    default_model = settings.default_model.strip()
    active_profile = resolve_runtime_profile(runtime_path="chat_agent")
    profile = provider_profiles().get(active_profile)
    effective_model = effective_runtime_model_id(runtime_path="chat_agent", profile=active_profile).strip()
    reject_legacy_external_agent_model(effective_model)
    model = effective_model
    if profile is not None and effective_model == (profile.routing_model or profile.model):
        model = profile.model.strip()
    api_base = _safe_runtime_endpoint(profile.api_base if profile is not None else settings.llm_api_base)
    return {
        "provider": _runtime_provider_label(model, profile=active_profile, api_base=api_base),
        "model": model,
        "model_label": _runtime_model_label(model),
        "api_base": api_base,
        "active_profile": active_profile,
    }


def _effective_runtime_route_status(runtime: dict[str, str], vlm_status: dict[str, object]) -> dict[str, object]:
    provider = runtime.get("provider", "")
    model = runtime.get("model", "")
    model_label = runtime.get("model_label", "")
    profile = runtime.get("active_profile", "")
    if provider == "local-gemma":
        mode = str(vlm_status.get("mode") or "not_configured")
        text_api_base = _safe_runtime_endpoint(runtime.get("api_base", ""))
        wrapper_base_url = _safe_runtime_endpoint(vlm_status.get("base_url", ""))
        advertised_backend_url = _safe_runtime_endpoint(vlm_status.get("backend_url", ""))
        wrapper_chat_api_base = (
            wrapper_base_url
            if wrapper_base_url.rstrip("/").endswith("/v1")
            else f"{wrapper_base_url.rstrip('/')}/v1" if wrapper_base_url else ""
        )
        uses_wrapper_chat = bool(
            text_api_base and wrapper_chat_api_base and text_api_base == wrapper_chat_api_base
        )
        text_hostname = urlparse(text_api_base).hostname if text_api_base else ""
        uses_direct_gpu_text = bool(
            mode == "gpu-server"
            and text_api_base
            and advertised_backend_url
            and text_api_base == advertised_backend_url
            and text_hostname not in {"localhost", "127.0.0.1", "::1"}
            and not uses_wrapper_chat
        )
        if uses_direct_gpu_text:
            route_label = "GPU text"
            provider_label = "local-gemma/gpu-text"
        elif uses_wrapper_chat and mode == "gpu-server":
            route_label = "GPU wrapper chat"
            provider_label = "local-gemma/gpu-wrapper-chat"
        elif uses_wrapper_chat and mode == "mac-wrapper":
            route_label = "Mac wrapper chat"
            provider_label = "local-gemma/mac-wrapper-chat"
        else:
            route_label = "local Gemma"
            provider_label = "local-gemma"
        return {
            "runtime_path": "chat_agent",
            "active_profile": profile,
            "provider": provider,
            "provider_label": provider_label,
            "model": model,
            "model_label": model_label,
            "mode": mode,
            "route_label": route_label,
            "summary_label": f"{route_label} · {model_label or model or 'unknown'}",
            "api_base": runtime.get("api_base", ""),
            "vlm_base_url": str(vlm_status.get("base_url") or ""),
            "vlm_backend_url": str(vlm_status.get("backend_url") or ""),
            "vlm_configured": bool(vlm_status.get("configured")),
            "queue_status_endpoint": str(vlm_status.get("queue_status_endpoint") or ""),
            "health_endpoint": str(vlm_status.get("health_endpoint") or ""),
            "backend_health_endpoint": str(vlm_status.get("backend_health_endpoint") or ""),
        }

    provider_label = provider or "unknown"
    return {
        "runtime_path": "chat_agent",
        "active_profile": profile,
        "provider": provider,
        "provider_label": provider_label,
        "model": model,
        "model_label": model_label,
        "mode": "remote_provider" if provider != "local" else provider,
        "route_label": provider_label,
        "summary_label": f"{provider_label} · {model_label or model or 'unknown'}",
        "api_base": runtime.get("api_base", ""),
        "vlm_configured": bool(vlm_status.get("configured")),
    }

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
    shutdown_error: Exception | None = None
    try:
        await drain_tracked_tasks(timeout_seconds=5.0)
    except Exception as exc:
        shutdown_error = exc
    finally:
        await close_db()
    if shutdown_error is not None:
        raise shutdown_error


def create_app() -> FastAPI:
    app = FastAPI(
        title="Seraph AI Assistant",
        version="2026.4.11",
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
        try:
            runtime = _active_chat_runtime_status()
        except ExternalAgentRuntimeRemovedError as exc:
            raise HTTPException(status_code=410, detail=exc.payload()) from exc
        vlm_status = _sanitize_runtime_endpoints(
            effective_vlm_status(live_probe=deferred_vlm_live_probe())
        )
        default_model = settings.default_model.strip()
        from src.api.model_fabric_settings import model_fabric_runtime_status

        fabric_status = await model_fabric_runtime_status(str(runtime.get("active_profile") or ""))
        return {
            "version": app.version,
            "build_id": f"SERAPH_PRIME_v{app.version}",
            **runtime,
            "effective_runtime": _effective_runtime_route_status(runtime, vlm_status),
            "default_provider": _runtime_provider_label(
                default_model,
                api_base=_safe_runtime_endpoint(settings.llm_api_base),
            ),
            "default_model": default_model,
            "default_model_label": _runtime_model_label(default_model),
            "default_api_base": _safe_runtime_endpoint(settings.llm_api_base),
            "provider_profiles": _sanitize_runtime_endpoints(provider_profile_statuses()),
            "vlm_runtime": vlm_status,
            "model_fabric": fabric_status,
            "timezone": settings.user_timezone,
            "llm_logging_enabled": settings.llm_log_enabled,
        }

    from src.api.router import api_router

    app.include_router(api_router)

    return app
