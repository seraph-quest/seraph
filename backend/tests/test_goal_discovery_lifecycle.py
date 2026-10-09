"""Actual current app owner cleanup, including partial native startup."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


@pytest.mark.parametrize("failure", ["partial_start", "late_stop"])
@pytest.mark.asyncio
async def test_actual_app_public_discovery_failure_releases_exact_owner(client, monkeypatch, tmp_path, failure):
    import src.app as app_module
    from config.settings import settings
    from src.runtime_plugins.bridge import CordisHost
    from src.guardian.goal_discovery import goal_discovery_service
    from src.work_board.dispatcher import _dispatcher
    host = CordisHost(node_path=tmp_path / "owned-absent-node")
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
    for target in ("src.model_fabric.configuration.hydrate_openrouter_credential",
            "src.observer.manager.context_manager.refresh", "src.guardian.goal_programmes.goal_programme_service.start",
            "src.guardian.goal_programmes.goal_programme_service.stop"):
        monkeypatch.setattr(target, AsyncMock())
    for target in ("src.workflows.job_runtime.durable_job_repository.recover_stale_jobs",
            "src.workflows.routines.routine_service.recover_pending_installs", "src.guardian.audio_worker.cleanup_audio_ingress_jobs"):
        monkeypatch.setattr(target, AsyncMock(return_value=[]))
    monkeypatch.setattr("src.api.profile.get_or_create_profile", AsyncMock(return_value=SimpleNamespace(
        interruption_mode=None, capture_mode=None, tool_policy_mode=None, mcp_policy_mode=None, approval_mode=None)))
    actual_start, actual_stop = goal_discovery_service.start, goal_discovery_service.stop
    stops, tasks = [], []
    async def start():
        await actual_start()
        tasks.append(_dispatcher.general_tasks)
        assert tasks[-1].started and tasks[-1].registry.started
        if failure == "partial_start":
            raise RuntimeError("owned discovery partial start failure")
    async def stop():
        assert _dispatcher.general_tasks is tasks[-1] and tasks[-1].started
        await actual_stop()
        stops.append(True)
        if failure == "late_stop":
            raise RuntimeError("owned discovery late stop failure")
    monkeypatch.setattr(goal_discovery_service, "start", start)
    monkeypatch.setattr(goal_discovery_service, "stop", stop)
    assert _dispatcher.goal_discovery is None
    with pytest.raises(RuntimeError, match="owned discovery"):
        async with app_module.lifespan(client._transport.app):
            assert failure == "late_stop" and _dispatcher.goal_discovery is goal_discovery_service
    assert stops == [True] and not goal_discovery_service.started
    assert _dispatcher.goal_discovery is None and _dispatcher.general_tasks is None
    assert not tasks[0].started and not tasks[0].registry.started
    from src.guardian.goal_programmes import goal_programme_service
    goal_programme_service.stop.assert_awaited_once()
    app_module.shutdown_scheduler.assert_called_once()
