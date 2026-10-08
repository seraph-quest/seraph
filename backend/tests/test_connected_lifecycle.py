"""Merged optional host shutdown cannot strand native source ownership."""
from unittest.mock import AsyncMock
import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [None, 'cordis_start', 'cordis_stop', 'goal_stop', 'sync_stop', 'replacement', 'real_host', 'real_host_stop'])
async def test_combined_lifecycle_keeps_native_owners_and_exact_pointer_fenced(app, async_db, monkeypatch, tmp_path, failure):
    import src.app as application
    import src.guardian.goal_programmes as programmes
    import src.integrations.connection_sync as sources
    from src.work_board.dispatcher import _dispatcher
    from config.settings import settings
    monkeypatch.setattr(settings, 'workspace_dir', str(tmp_path))
    monkeypatch.setattr(settings, 'deployment_environment', 'test')
    monkeypatch.setattr(application, 'init_db', AsyncMock())
    monkeypatch.setattr(application, 'close_db', AsyncMock())
    monkeypatch.setattr(application, 'init_scheduler', lambda: None)
    monkeypatch.setattr(application, 'shutdown_scheduler', lambda: None)
    monkeypatch.setattr(application, 'sync_scheduled_jobs', AsyncMock())
    monkeypatch.setattr(application, 'init_llm_logging', lambda: None)
    monkeypatch.setattr(application.mcp_manager, 'load_config', lambda *args: None)
    monkeypatch.setattr(application.mcp_manager, 'disconnect_all', lambda: None)
    monkeypatch.setattr('src.model_fabric.configuration.hydrate_openrouter_credential', AsyncMock())
    monkeypatch.setattr('src.observer.manager.context_manager.refresh', AsyncMock())
    monkeypatch.setattr(_dispatcher, 'connection_sync_runtime', None)
    service = programmes.GoalProgrammeService()
    monkeypatch.setattr(programmes, 'goal_programme_service', service)
    events = []
    class OwnedHost:
        async def start(self):
            events.append('cordis:start')
            if failure == 'cordis_start': raise RuntimeError('owned optional start failure')
        async def stop(self):
            events.append('cordis:stop')
            if failure == 'cordis_stop': raise RuntimeError('owned optional stop failure')
    host = OwnedHost()
    process = None
    if failure in {'real_host', 'real_host_stop'}:
        import os
        from pathlib import Path
        from src.runtime_plugins.bridge import CordisHost
        node = os.environ.get('SERAPH_CORDIS_TEST_NODE')
        if not node: pytest.skip('Explicit reviewed Node required for actual combined child proof')
        host = CordisHost(node_path=Path(node))
        original_start, original_stop = host.start, host.stop
        async def start_host():
            await original_start()
            events.append('cordis:start')
        async def stop_host():
            await original_stop()
            events.append('cordis:stop')
            if failure == 'real_host_stop': raise RuntimeError('owned actual host stop failure')
        monkeypatch.setattr(host, 'start', start_host)
        monkeypatch.setattr(host, 'stop', stop_host)
    monkeypatch.setattr(application, 'cordis_host', host)
    goal_start, goal_stop = service.start, service.stop
    async def start_goal():
        await goal_start()
        events.append('goal:start')
    async def stop_goal():
        await goal_stop()
        events.append('goal:stop')
        if failure == 'goal_stop': raise RuntimeError('owned Goal stop failure')
    monkeypatch.setattr(service, 'start', start_goal)
    monkeypatch.setattr(service, 'stop', stop_goal)
    source_start, source_stop = sources.ConnectionSyncService.start, sources.ConnectionSyncService.stop
    async def start_source(self):
        await source_start(self)
        events.append('sync:start')
    async def stop_source(self):
        await source_stop(self)
        events.append('sync:stop')
        if failure == 'sync_stop': raise RuntimeError('owned source stop failure')
    monkeypatch.setattr(sources.ConnectionSyncService, 'start', start_source)
    monkeypatch.setattr(sources.ConnectionSyncService, 'stop', stop_source)
    replacement = object()
    async def lifespan():
        nonlocal process
        async with app.router.lifespan_context(app):
            assert service._running and app.state.connection_sync_runtime.started
            assert _dispatcher.connection_sync_runtime is app.state.connection_sync_runtime
            assert events == ['sync:start', 'goal:start', 'cordis:start']
            if failure == 'replacement': _dispatcher.connection_sync_runtime = replacement
            if failure in {'real_host', 'real_host_stop'}:
                status = await host.refresh_status()
                assert host.admitting and status['state'] == 'ready'
                assert status['readiness']['state'] == 'verified'
                process = host.process
                assert process is not None and process.returncode is None
    if failure in {'cordis_stop', 'goal_stop', 'sync_stop', 'real_host_stop'}:
        with pytest.raises(RuntimeError, match='owned .* stop failure'):
            await lifespan()
    else:
        await lifespan()
    assert events == ['sync:start', 'goal:start', 'cordis:start', 'cordis:stop', 'goal:stop', 'sync:stop']
    assert not service._running and not app.state.connection_sync_runtime.started
    assert _dispatcher.connection_sync_runtime is (replacement if failure == 'replacement' else None)

    if failure in {'real_host', 'real_host_stop'}:
        import os
        assert process is not None and process.returncode == 0
        with pytest.raises(ProcessLookupError): os.kill(process.pid, 0)
        assert host.snapshot()['cleanup']['process_reaped'] is True
        assert host.snapshot()['cleanup']['resources_remaining'] == 0
