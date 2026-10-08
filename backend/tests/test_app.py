import pytest
from unittest.mock import AsyncMock, patch

from config.settings import settings
from src.runtime_plugins.bridge import CordisHost
from src.runtime_plugins.composition import CompositionBlocked
from tests.test_browser_interactions_v2 import local_form
from src.app import (
    _active_chat_runtime_status,
    _augment_inference_readiness,
    _effective_runtime_route_status,
    _safe_runtime_endpoint,
)


_DEFERRED_VLM_PROBE = {
    "checked": False,
    "reachable": False,
    "reason": "deferred_fast_metadata",
    "health": {"checked": False, "ok": False, "status_code": None, "error": ""},
    "backend_health": {"checked": False, "ok": False, "status_code": None, "error": ""},
    "queue_status": {"checked": False, "ok": False, "status_code": None, "error": ""},
    "chat_proxy": {"checked": False, "ok": False, "status_code": None, "error": ""},
}


@pytest.mark.parametrize(
    "unsafe_endpoint",
    [
        "https://user:password@models.example/v1",
        "https://models.example/v1?token=secret",
        "https://models.example/v1#secret-fragment",
        "http://models.example:not-a-port/v1",
        "http://[malformed/v1",
    ],
)
def test_runtime_endpoint_sanitizer_blanks_unsafe_values(unsafe_endpoint):
    assert _safe_runtime_endpoint(unsafe_endpoint) == ""


def test_runtime_endpoint_sanitizer_preserves_safe_absolute_value():
    assert _safe_runtime_endpoint("HTTP://[::1]:8000/v1") == "http://[::1]:8000/v1"


def test_active_runtime_ignores_legacy_local_preference_and_uses_openrouter():
    with (
        patch.object(settings, "default_model", "openrouter/x-ai/grok-4.1-fast"),
        patch.object(settings, "local_model", "openai/unsloth/gemma-4-26B-A4B-it-qat-GGUF"),
        patch.object(settings, "local_llm_api_base", "http://127.0.0.1:8000/v1"),
        patch.object(settings, "local_llm_api_key", "not-needed"),
        patch.object(settings, "openrouter_provider_only", False),
        patch.object(settings, "runtime_profile_preferences", "chat_agent=local-gemma-chat-thinking"),
        patch.object(settings, "runtime_model_overrides", ""),
    ):
        runtime = _active_chat_runtime_status()

    assert runtime["model"] == "x-ai/grok-4.1-fast"
    assert runtime["active_profile"] == "openrouter"


def test_effective_runtime_distinguishes_direct_gpu_text_from_wrapper_chat():
    base_runtime = {
        "provider": "local-gemma",
        "model": "gemma",
        "model_label": "gemma",
        "active_profile": "local-gemma-chat-thinking",
    }
    vlm = {
        "mode": "gpu-server",
        "configured": True,
        "chat_api_base": "http://192.168.1.26:8001/v1",
        "base_url": "http://192.168.1.26:8001",
        "backend_url": "http://192.168.1.26:8000/v1",
    }

    direct = _effective_runtime_route_status(
        {**base_runtime, "api_base": "http://192.168.1.26:8000/v1"},
        vlm,
    )
    wrapper = _effective_runtime_route_status(
        {**base_runtime, "api_base": "http://192.168.1.26:8001/v1"},
        vlm,
    )
    mac_local = _effective_runtime_route_status(
        {**base_runtime, "api_base": "http://127.0.0.1:8000/v1"},
        vlm,
    )
    unrelated_remote = _effective_runtime_route_status(
        {**base_runtime, "api_base": "https://api.example.com/v1"},
        vlm,
    )

    assert direct["route_label"] == "GPU text"
    assert direct["provider_label"] == "local-gemma/gpu-text"
    assert wrapper["route_label"] == "GPU wrapper chat"
    assert wrapper["provider_label"] == "local-gemma/gpu-wrapper-chat"
    assert mac_local["route_label"] == "local Gemma"
    assert mac_local["provider_label"] == "local-gemma"
    assert unrelated_remote["route_label"] == "local Gemma"
    assert unrelated_remote["provider_label"] == "local-gemma"


def test_openrouter_runtime_receipt_stays_blocked_until_profile_and_proofs_are_routable():
    route = {
        "provider": "openrouter",
        "active_profile": "openrouter",
        "inference_ready": True,
        "inference_readiness": {"reasons": []},
    }
    fabric = {
        "profiles": [{
            "id": "openrouter",
            "model_fabric_eligible": True,
            "routable": False,
            "non_routable_reasons": ["cost_bound_missing"],
        }],
        "proofs": [{"profile_id": "openrouter", "capability": "health", "status": "missing"}],
    }

    receipt = _augment_inference_readiness(route, fabric)

    assert receipt["inference_ready"] is False
    assert receipt["inference_readiness"]["status"] == "configuration_required"
    assert "model_fabric_cost_bound_missing" in receipt["inference_readiness"]["reasons"]
    assert "model_fabric_proof_missing:health" in receipt["inference_readiness"]["reasons"]


@pytest.mark.asyncio
async def test_cors_allows_loopback_dev_origin(client):
    response = await client.options(
        "/api/capabilities/overview",
        headers={
            "Origin": "http://127.0.0.1:3001",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:3001"


@pytest.mark.asyncio
async def test_cors_uses_exact_origins_without_loopback_port_wildcard(client):
    rejected = await client.options(
        "/api/capabilities/overview",
        headers={
            "Origin": "http://localhost:9999",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert rejected.status_code == 400
    assert "access-control-allow-origin" not in rejected.headers

    allowed = await client.options(
        "/api/capabilities/overview",
        headers={
            "Origin": "http://localhost:3001",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "http://localhost:3001"


@pytest.mark.asyncio
async def test_runtime_status_exposes_release_and_model(client):
    with (
        patch.object(settings, "runtime_profile_preferences", ""),
        patch.object(settings, "local_runtime_paths", ""),
    ):
        response = await client.get("/api/runtime/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["version"] == "2026.10.7"
    assert payload["build_id"] == "SERAPH_PRIME_v2026.10.7"
    assert payload["provider"] == "openrouter"
    assert payload["model"] == settings.default_model.removeprefix("openrouter/")
    assert payload["model_label"] == settings.default_model.split("/")[-1]
    assert payload["active_profile"] == "openrouter"
    assert payload["default_provider"] == "openrouter"
    assert payload["default_model"] == settings.default_model
    assert isinstance(payload["provider_profiles"], list)
    assert "local_operators" not in payload
    assert any(item["id"] == "openrouter" for item in payload["provider_profiles"])
    assert all("api_key" not in item for item in payload["provider_profiles"])
    admission = payload["remote_inference_admission"]
    assert admission["verification"] == {
        "contract": "contract_tested",
        "configuration": "configuration_required",
        "provider": "live_unverified",
    }
    assert admission["serial_remote_inference"] is True
    assert admission["max_active"] == 1
    assert admission["capacity"]["max_queued"] == 64


@pytest.mark.asyncio
async def test_runtime_status_rejects_removed_local_codex_when_selected(client):
    with patch.object(settings, "default_model", "codex-local"):
        response = await client.get("/api/runtime/status")

    assert response.status_code == 410
    payload = response.json()
    assert payload["detail"]["code"] == "external_agent_runtime_removed"


@pytest.mark.asyncio
async def test_runtime_status_reports_openrouter_when_local_preference_is_stale(client):
    with (
        patch.object(settings, "default_model", "openrouter/x-ai/grok-4.1-fast"),
        patch.object(settings, "local_model", "openai/unsloth/gemma-4-26B-A4B-it-qat-GGUF"),
        patch.object(settings, "local_llm_api_base", "http://127.0.0.1:8000/v1"),
        patch.object(settings, "local_llm_api_key", "not-needed"),
        patch.object(settings, "openrouter_provider_only", False),
        patch.object(settings, "runtime_profile_preferences", "chat_agent=local-gemma-chat-thinking"),
    ):
        response = await client.get("/api/runtime/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "openrouter"
    assert payload["model"] == "x-ai/grok-4.1-fast"
    assert payload["model_label"] == "grok-4.1-fast"
    assert payload["api_base"] == "https://openrouter.ai/api/v1"
    assert payload["active_profile"] == "openrouter"
    assert payload["default_provider"] == "openrouter"
    assert payload["default_model"] == "openrouter/x-ai/grok-4.1-fast"


@pytest.mark.asyncio
async def test_runtime_status_reports_openrouter_and_historical_vlm_metadata(client):
    with (
        patch.object(settings, "default_model", "openrouter/x-ai/grok-4.1-fast"),
        patch.object(settings, "local_model", "openai/unsloth/gemma-4-26B-A4B-it-qat-GGUF"),
        patch.object(settings, "local_llm_api_base", ""),
        patch.object(settings, "seraph_vlm_mode", "gpu-server"),
        patch.object(settings, "seraph_vlm_base_url", "http://192.168.1.26:8001"),
        patch.object(settings, "seraph_vlm_backend_url", "http://192.168.1.26:8000/v1"),
        patch.object(settings, "seraph_vlm_api_key", "secret-token"),
        patch.object(settings, "openrouter_provider_only", False),
        patch.object(settings, "runtime_profile_preferences", "chat_agent=local-gemma-chat-thinking"),
    ):
        response = await client.get("/api/runtime/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "openrouter"
    assert payload["api_base"] == "https://openrouter.ai/api/v1"
    assert payload["active_profile"] == "openrouter"
    assert payload["effective_runtime"]["provider"] == "openrouter"
    assert payload["effective_runtime"]["active_provider_policy"] == "openrouter_only"
    assert payload["vlm_runtime"]["active"] is False
    assert payload["vlm_runtime"]["disabled_reason"] == "local_vlm_disabled_openrouter_only"
    assert payload["vlm_runtime"]["live_probe"]["checked"] is False
    assert payload["vlm_runtime"]["live_probe"]["reason"] == "deferred_fast_metadata"
    assert "secret-token" not in str(payload)


@pytest.mark.asyncio
async def test_runtime_status_does_not_activate_direct_gpu_text(client):
    with (
        patch.object(settings, "default_model", "openrouter/x-ai/grok-4.1-fast"),
        patch.object(settings, "local_model", "openai/unsloth/gemma-4-26B-A4B-it-qat-GGUF"),
        patch.object(settings, "local_llm_api_base", "http://192.168.1.26:8000/v1"),
        patch.object(settings, "local_llm_api_key", "not-needed"),
        patch.object(settings, "seraph_vlm_mode", "gpu-server"),
        patch.object(settings, "seraph_vlm_base_url", "http://192.168.1.26:8001"),
        patch.object(settings, "seraph_vlm_backend_url", "http://192.168.1.26:8000/v1"),
        patch.object(settings, "runtime_profile_preferences", "chat_agent=local-gemma-chat-thinking"),
        patch.object(settings, "openrouter_provider_only", False),
    ):
        response = await client.get("/api/runtime/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["provider"] == "openrouter"
    assert payload["api_base"] == "https://openrouter.ai/api/v1"
    assert payload["effective_runtime"]["route_label"] == "openrouter"
    assert payload["effective_runtime"]["provider_label"] == "openrouter"
    assert payload["vlm_runtime"]["active"] is False
    assert payload["vlm_runtime"]["disabled_reason"] == "local_vlm_disabled_openrouter_only"
    assert payload["vlm_runtime"]["live_probe"]["checked"] is False
    assert payload["vlm_runtime"]["live_probe"]["reason"] == "deferred_fast_metadata"


@pytest.mark.asyncio
async def test_runtime_status_does_not_wait_for_live_vlm_probe(client):
    with (
        patch.object(settings, "seraph_vlm_mode", "gpu-server"),
        patch.object(settings, "seraph_vlm_base_url", "http://192.168.1.26:8001"),
        patch("src.vlm_runtime.probe_effective_vlm_runtime", side_effect=AssertionError("live probe should not run")),
    ):
        response = await client.get("/api/runtime/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["vlm_runtime"]["live_probe"]["checked"] is False
    assert payload["vlm_runtime"]["live_probe"]["reason"] == "deferred_fast_metadata"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe_endpoint",
    [
        "https://user:password@models.example/v1",
        "https://models.example/v1?token=secret",
        "https://models.example/v1#secret-fragment",
        "http://models.example:not-a-port/v1",
        "http://[malformed/v1",
    ],
)
async def test_runtime_status_uses_openrouter_and_blanks_unsafe_legacy_endpoints(client, unsafe_endpoint):
    with (
        patch.object(settings, "default_model", "openai-compatible/model"),
        patch.object(settings, "llm_api_base", unsafe_endpoint),
        patch.object(settings, "runtime_profile_preferences", ""),
        patch.object(settings, "local_runtime_paths", ""),
    ):
        response = await client.get("/api/runtime/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["api_base"] == "https://openrouter.ai/api/v1"
    assert payload["default_api_base"] == ""
    assert payload["effective_runtime"]["api_base"] == "https://openrouter.ai/api/v1"
    assert all(profile.get("api_base") != unsafe_endpoint for profile in payload["provider_profiles"])
    serialized = str(payload)
    assert "password" not in serialized
    assert "token=secret" not in serialized
    assert "secret-fragment" not in serialized


@pytest.mark.asyncio
async def test_browser_provider_api_is_publicly_exposed(client):
    response = await client.get("/api/browser/providers?owner_session_id=test-auth-bypass")

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_optional_cordis_failure_preserves_health_and_redacted_runtime_status(client, monkeypatch):
    """The actual status binding must not make missing Node a core outage."""
    import httpx

    original_send = httpx.AsyncClient.send

    async def deny_external_transport(session, *args, **kwargs):
        if not isinstance(session._transport, httpx.ASGITransport):
            raise AssertionError("external transport forbidden during implementation check")
        return await original_send(session, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "send", deny_external_transport)
    host = CordisHost()
    with patch("src.runtime_plugins.bridge.reviewed_composition", side_effect=CompositionBlocked("node_unsupported")):
        assert await host.start() is False
    monkeypatch.setattr("src.app.cordis_host", host)
    assert (await client.get("/health")).json() == {"status": "ok"}
    response = await client.get("/api/runtime/status")
    assert response.status_code == 200
    snapshot = response.json()["cordis_runtime"]
    assert snapshot["state"] == "blocked"
    assert snapshot["reason"] == "node_unsupported"
    assert snapshot["runtime_role"] == "lifecycle_host"
    assert snapshot["cleanup"]["state"] == "not_started"
    assert not {"boot_nonce", "pid", "stderr", "env"} & snapshot.keys()


@pytest.mark.asyncio
async def test_runtime_status_awaits_current_cordis_readback_not_cached_ready(client, monkeypatch):
    host = CordisHost()
    cached = host.snapshot()
    actual = {**cached, "state": "ready", "reason": None,
              "readiness": {"state": "verified", "checked_at": 123}}
    refresh = AsyncMock(return_value=actual)
    monkeypatch.setattr(host, "refresh_status", refresh)
    monkeypatch.setattr(host, "snapshot", lambda: (_ for _ in ()).throw(AssertionError("cached API readiness")))
    monkeypatch.setattr("src.app.cordis_host", host)
    response = await client.get("/api/runtime/status")
    assert response.status_code == 200
    assert response.json()["cordis_runtime"] == actual
    refresh.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_optional_cordis_app_lifespan_missing_node_keeps_core_open_and_runs_owned_stop(client, monkeypatch, tmp_path):
    """Execute actual lifespan wiring with unrelated startup owners isolated."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    import src.app as app_module

    host = CordisHost(node_path=tmp_path / "absent-node")
    monkeypatch.setattr(app_module, "cordis_host", host)
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    for name in ("init_db", "close_db", "sync_scheduled_jobs", "drain_tracked_tasks"):
        monkeypatch.setattr(app_module, name, AsyncMock())
    for name in ("ensure_soul_exists", "init_llm_logging", "init_scheduler", "shutdown_scheduler"):
        monkeypatch.setattr(app_module, name, Mock())
    for manager, methods in [(app_module.mcp_manager, ("load_config", "disconnect_all")),
                             (app_module.skill_manager, ("init",)), (app_module.runbook_manager, ("init",)),
                             (app_module.workflow_manager, ("init",)), (app_module.starter_pack_manager, ("init",))]:
        for method in methods:
            monkeypatch.setattr(manager, method, Mock())
    monkeypatch.setattr("src.model_fabric.configuration.hydrate_openrouter_credential", AsyncMock())
    monkeypatch.setattr("src.workflows.job_runtime.durable_job_repository.recover_stale_jobs", AsyncMock(return_value=[]))
    monkeypatch.setattr("src.workflows.routines.routine_service.recover_pending_installs", AsyncMock(return_value=[]))
    monkeypatch.setattr("src.guardian.audio_worker.cleanup_audio_ingress_jobs", AsyncMock(return_value=[]))
    profile = SimpleNamespace(interruption_mode=None, capture_mode=None, tool_policy_mode=None, mcp_policy_mode=None, approval_mode=None)
    monkeypatch.setattr("src.api.profile.get_or_create_profile", AsyncMock(return_value=profile))
    monkeypatch.setattr("src.observer.manager.context_manager.refresh", AsyncMock())
    async with app_module.lifespan(client._transport.app):
        assert host.snapshot()["state"] == "blocked"
        assert host.reason == "node_missing"
        assert (await client.get("/health")).json() == {"status": "ok"}
    assert host.state == "stopped"
    assert host.process is None
    assert host.snapshot()["cleanup"]["resources_remaining"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("host_failure", ["none", "start", "stop", "cancel"])
async def test_actual_cordis_app_lifespan_authenticated_status_and_positive_cleanup(client, monkeypatch, tmp_path, local_form, host_failure):
    """One real stock host crosses actual app startup, authenticated RPC and reap."""
    import os
    import json
    import time
    from pathlib import Path
    from types import SimpleNamespace
    from unittest.mock import Mock
    import src.app as app_module

    from src.browser.sessions import ProfiledInteractionSessions
    from src.browser.interaction_contracts import digest
    from src.browser.task_lane import BrowserTaskLane
    from tests.test_browser_interactions_v2 import FORM
    from contextlib import nullcontext
    import uuid
    request, contacts, denied = local_form
    browser_service = ProfiledInteractionSessions(request=request, source_digest=digest(FORM))
    monkeypatch.setattr("src.browser.sessions.profiled_interaction_sessions", browser_service)
    monkeypatch.setattr("src.api.browser.profiled_interaction_sessions", browser_service)
    selected = os.environ.get("SERAPH_CORDIS_TEST_NODE")
    if not selected:
        pytest.skip("explicit reviewed Node required for native app-lifespan proof")
    host = CordisHost(node_path=Path(selected).resolve())
    monkeypatch.setattr(app_module, "cordis_host", host)
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "isolated-cordis-auth-fixture")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    monkeypatch.setattr(settings, "operator_auth_cookie_secure", False)
    # Isolate unrelated startup owners; Cordis start/status/stop remain actual.
    for name in ("init_db", "close_db", "sync_scheduled_jobs", "drain_tracked_tasks"):
        monkeypatch.setattr(app_module, name, AsyncMock())
    for name in ("ensure_soul_exists", "init_llm_logging", "init_scheduler", "shutdown_scheduler"):
        monkeypatch.setattr(app_module, name, Mock())
    for manager, methods in [(app_module.mcp_manager, ("load_config", "disconnect_all")),
                             (app_module.skill_manager, ("init",)), (app_module.runbook_manager, ("init",)),
                             (app_module.workflow_manager, ("init",)), (app_module.starter_pack_manager, ("init",))]:
        for method in methods:
            monkeypatch.setattr(manager, method, Mock())
    for target in ("src.model_fabric.configuration.hydrate_openrouter_credential",
                   "src.observer.manager.context_manager.refresh",
                   "src.guardian.goal_programmes.goal_programme_service.start",
                   "src.guardian.goal_programmes.goal_programme_service.stop"):
        monkeypatch.setattr(target, AsyncMock())
    for target in ("src.workflows.job_runtime.durable_job_repository.recover_stale_jobs",
                   "src.workflows.routines.routine_service.recover_pending_installs",
                   "src.guardian.audio_worker.cleanup_audio_ingress_jobs"):
        monkeypatch.setattr(target, AsyncMock(return_value=[]))
    profile = SimpleNamespace(interruption_mode=None, capture_mode=None, tool_policy_mode=None, mcp_policy_mode=None, approval_mode=None)
    monkeypatch.setattr("src.api.profile.get_or_create_profile", AsyncMock(return_value=profile))
    assert (await client.get("/api/runtime/status")).status_code == 401
    login = await client.post("/api/auth/login", json={"password":"isolated-cordis-auth-fixture"},
                              headers={"origin":"http://localhost:3001"})
    assert login.status_code == 200
    assert (await client.post("/api/auth/ownership/enroll", headers={"origin":"http://localhost:3001"})).status_code == 200
    goal_response = await client.post("/api/goals", json={"title":"Preview the registered form during managed lifespan"}, headers={"origin":"http://localhost:3001"})
    assert goal_response.status_code == 200, goal_response.text
    goal = goal_response.json()
    from src.work_board.dispatcher import _dispatcher
    observed_stop_owners = []
    actual_host_stop = host.stop
    async def stop_with_late_failure(*args, **kwargs):
        from src.agent.session import session_manager
        assert session_manager._task_continuity is None
        assert client._transport.app.state.task_continuity._started
        assert _dispatcher.general_tasks is not None
        assert _dispatcher.general_tasks.started and _dispatcher.general_tasks.registry.started
        observed_stop_owners.append(_dispatcher.general_tasks)
        assert browser_service.started
        assert bool(browser_service.active) is (host_failure != "cancel")
        await actual_host_stop(*args, **kwargs)
        if host_failure == "stop":
            raise RuntimeError("fixture late Cordis stop failure after positive reap")
    monkeypatch.setattr(host, "stop", stop_with_late_failure)
    actual_host_start = host.start
    startup_resources = []
    async def start_with_late_failure(*args, **kwargs):
        assert _dispatcher.general_tasks is not None
        assert _dispatcher.general_tasks.started and _dispatcher.general_tasks.registry.started
        started = await actual_host_start(*args, **kwargs)
        from src.agent.session import session_manager
        assert client._transport.app.state.task_continuity._started
        assert session_manager._task_continuity is client._transport.app.state.task_continuity
        startup_resources.append((host.process, host.boot_nonce))
        if host_failure == "cancel":
            import asyncio
            asyncio.current_task().cancel()
            await asyncio.sleep(0)
        if host_failure == "start":
            raise RuntimeError("fixture late Cordis startup failure with owned child")
        return started
    monkeypatch.setattr(host, "start", start_with_late_failure)
    async def goal_stop_after_browser():
        assert not browser_service.started and browser_service.active == {}
        from src.agent.session import session_manager
        assert not client._transport.app.state.task_continuity._started
        assert session_manager._task_continuity is None
    monkeypatch.setattr("src.guardian.goal_programmes.goal_programme_service.stop",
        AsyncMock(side_effect=goal_stop_after_browser))
    previous_boot = None
    receipts = []
    for _ in range(2):
        assert _dispatcher.general_tasks is None
        import asyncio
        expected = pytest.raises(asyncio.CancelledError) if host_failure == "cancel" else pytest.raises(RuntimeError, match="late Cordis stop failure") if host_failure == "stop" else nullcontext()
        with expected:
            async with app_module.lifespan(client._transport.app):
                assert _dispatcher.general_tasks is not None
                assert _dispatcher.general_tasks.started and _dispatcher.general_tasks.registry.started
                assert host.admitting
                assert host.boot_nonce is not None and host.boot_nonce != previous_boot
                previous_boot = host.boot_nonce
                process = host.process
                pid = process.pid
                assert host.snapshot()["readiness"]["state"] == "unknown"
                started = int(time.time()*1000)
                response = await client.get("/api/runtime/status")
                assert response.status_code == 200
                actual = response.json()["cordis_runtime"]
                assert actual["state"] == "ready" and actual["reason"] is None
                assert actual["runtime_role"] == "lifecycle_host"
                assert actual["readiness"]["state"] == "verified"
                assert started <= actual["readiness"]["checked_at"] <= int(time.time()*1000)
                assert actual["plugins"] == [{"id":"seraph.host-lifecycle@1.0.0", "state":"ready", "reason":None}]
                assert not {"boot_nonce", "pid", "stderr", "env"} & actual.keys()
                assert host.boot_nonce not in response.text
                assert (await client.get("/health")).json() == {"status":"ok"}
                assert browser_service.started
                prepared = await client.post("/api/capabilities/browser-interactions/jobs", json={
                    "profile_id":"httpbin.forms.v1", "goal_id":goal["id"], "goal_revision":goal["revision"],
                    "request_key":str(uuid.uuid4()), "read_ack":True}, headers={"origin":"http://localhost:3001"})
                assert prepared.status_code == 200, prepared.text
                job_id = prepared.json()["job_id"]
                page = browser_service.active[job_id]["page"]
                assert page.latest is not None and not page.page.is_closed()
        assert _dispatcher.general_tasks is None
        assert not observed_stop_owners[-1].started
        assert not observed_stop_owners[-1].registry.started
        from src.guardian.goal_programmes import goal_programme_service
        assert goal_programme_service.stop.await_count == len(observed_stop_owners)
        assert app_module.shutdown_scheduler.call_count == len(observed_stop_owners)
        if host_failure == "cancel":
            process, boot_nonce = startup_resources[-1]
            try:
                assert not client._transport.app.state.task_continuity._started
                assert not browser_service.started and browser_service.active == {}
                assert host.snapshot()["cleanup"]["process_reaped"] is True
            finally:
                # Keep even the deliberately failing pre-fix receipt leak-free.
                if browser_service.started:
                    await actual_host_stop()
                    await browser_service.stop()
                    from src.guardian.goal_programmes import goal_programme_service
                    await goal_programme_service.stop()
            assert boot_nonce is not None and boot_nonce != previous_boot
            previous_boot = boot_nonce
            assert process.returncode == 0
            with pytest.raises(ProcessLookupError): os.kill(process.pid, 0)
            assert (await client.get("/api/capabilities/browser-interactions/jobs")).json()["jobs"] == []
            lane = BrowserTaskLane(tmp_path).acquire()
            lane.release()
            receipts.append({"host_failure":"cancel", "process_exit_code":process.returncode,
                "process_reaped":True, "browser_started":False, "browser_contexts":0,
                "browser_jobs":0, "shared_lane_available":True, "cancellation_propagated":True,
                "continuity_unbound_and_stopped":True, "task_owner_released":True,
                "task_service_and_registry_stopped":True, "goal_and_scheduler_stopped":True})
            continue
        assert not browser_service.started and browser_service.active == {}
        assert not client._transport.app.state.task_continuity._started
        assert page.page.is_closed() and not page.resources.browser.is_connected()
        cleanup_row = await browser_service.jobs.get_job(job_id)
        assert cleanup_row["status"] == "blocked"
        assert any(c["checkpoint_id"] == "native-physical-resource-cleanup" for c in cleanup_row["checkpoints"])
        lane = BrowserTaskLane(tmp_path).acquire()
        lane.release()
        from src.guardian.goal_programmes import goal_programme_service
        goal_programme_service.stop.assert_awaited()
        cleanup = host.snapshot()["cleanup"]
        assert cleanup == {"state":"clean", "process_reaped":True,
                           "resources_remaining":0, "cordis_disposal":"confirmed"}
        assert process.returncode == 0 and not host.admitting
        with pytest.raises(ProcessLookupError): os.kill(pid, 0)
        receipts.append({"runtime_status":actual, "cleanup":cleanup,
                         "fresh_boot":True, "process_exit_code":process.returncode,
                         "owned_pid_absent":True, "browser_positive_cleanup":True,
                         "browser_durable_status":cleanup_row["status"], "host_failure":host_failure,
                         "continuity_unbound_and_stopped":True,
                         "task_owner_released":True, "task_service_and_registry_stopped":True,
                         "goal_and_scheduler_stopped":True})
    assert contacts == ([] if host_failure == "cancel" else [("GET", "/forms/post"), ("GET", "/forms/post")]) and denied == []
    (tmp_path / "cordis-app-lifecycle-proof.json").write_text(json.dumps({"authenticated":True, "cycles":receipts}, indent=2))
