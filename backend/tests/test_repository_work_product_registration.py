"""Stock catalog and original Python lifecycle; no fixture registry authority."""
import socket
import subprocess
from pathlib import Path

import pytest

from config.settings import RepoSandboxSettings, settings
# Managed create_app registers router models before its database initialization.
from src.api import router as _managed_router
from src.execution.repo_sandbox import persist_repo_sandbox_settings
from src.native_tools.task_adapters import ToolRegistry, verify_task_tool_capacity
from src.observer.manager import context_manager
from src.security.trust_contract import AuthorityGrant, PrincipalType, TrustPrincipal
from src.work_board.dispatcher import WorkBoardDispatcher
from src.work_board.general_task import current_task_service, digest
from src.workflows.job_runtime import durable_job_repository
from tests.test_inference_accounting import accounting_db
from tests.test_repo_work_contracts import selection


async def canonical_selector_update(**changes):
    """The actual existing settings writer, under its original short fence."""
    from src.api.settings import RepoSandboxSettingsRequest, _persist_repo_sandbox_selector_update
    from src.model_fabric.effective_policy import configuration_mutation_lock
    async with configuration_mutation_lock:
        _persist_repo_sandbox_selector_update(RepoSandboxSettingsRequest(**changes))


async def canonical_rows(dispatcher):
    from sqlalchemy import select
    from sqlmodel import SQLModel
    async with dispatcher.session_provider() as db:
        return {table.name: tuple(tuple(row) for row in (await db.execute(select(table))).all())
            for table in SQLModel.metadata.sorted_tables}


def owned_runtime(source):
    return (source, source.sandbox, source.jobs, source.session_factory,
        source._iterative_lanes, source._iterative_model_callbacks,
        source._iterative_process_callbacks, source._iterative_process_jobs)


def metadata_projection(service, monkeypatch, engine):
    """Deny effect/physical probes only while enumerating the existing catalog."""
    from src.execution.repo_sandbox import LocalRepoRepairExecutor, RootlessDockerRepoSandbox
    from src.execution.repo_node import NodeRepoRepairExecutor
    from src.workflows.repo_repair import RepoRepairService
    from src.workflows import repo_repair_source
    from sqlalchemy import event
    def deny(*args, **kwargs):
        raise AssertionError('runtime catalog must not perform physical/provider/SQL work')
    # Deny SQL without replacing the original provider identity being checked.
    with monkeypatch.context() as metadata:
        metadata.setattr(Path, 'open', deny)
        metadata.setattr(Path, 'read_text', deny)
        metadata.setattr(Path, 'stat', deny)
        metadata.setattr(subprocess, 'Popen', deny)
        metadata.setattr(subprocess, 'run', deny)
        metadata.setattr(RootlessDockerRepoSandbox, 'preflight', deny)
        metadata.setattr(LocalRepoRepairExecutor, 'preflight', deny)
        metadata.setattr(LocalRepoRepairExecutor, 'iterative_preflight', deny)
        metadata.setattr(NodeRepoRepairExecutor, 'preflight', deny)
        metadata.setattr(RepoRepairService, '_workspace', deny)
        metadata.setattr(repo_repair_source, '_assert_task_publication_configuration', deny)
        metadata.setattr('src.workflows.repo_repair.FallbackLiteLLMModel', deny)
        source = service.repository_source_service
        if source is not None:
            metadata.setattr(source, 'model_factory', deny)
        event.listen(engine.sync_engine, 'before_cursor_execute', deny)
        try:
            return ({item.tool_id for item in service.registry.descriptors()}, service.registry.blocked_tools())
        finally:
            event.remove(engine.sync_engine, 'before_cursor_execute', deny)


@pytest.fixture(autouse=True)
def isolated_selection(monkeypatch):
    monkeypatch.setattr(settings, 'repo_sandbox', RepoSandboxSettings())
    context = context_manager.get_context()
    monkeypatch.setattr(context, 'tool_policy_mode', 'full')
    monkeypatch.setattr(context, 'mcp_policy_mode', 'disabled')
    def deny(*args, **kwargs):
        raise AssertionError('registration must not contact any socket')
    monkeypatch.setattr(socket.socket, 'connect', deny)


def dispatcher_for(accounting_db):
    return WorkBoardDispatcher(jobs=durable_job_repository,
        session_provider=accounting_db[2].accounting_sessions)


def repository_descriptor(registry):
    return next(item for item in registry.descriptors() if item.tool_id == 'repository_work')


@pytest.mark.parametrize('profile', ['repo-python-pytest-v1',
    'repo-python-pytest-publication-v1', 'repo-node24-npm-v1'])
async def test_stock_lifecycle_restores_persisted_selector_and_native_owner(accounting_db, monkeypatch, profile):
    workspace, _, factory = accounting_db
    workspace.chmod(0o700)
    selected = RepoSandboxSettings(enabled=True, executor_kind='local', profile=profile)
    persist_repo_sandbox_settings(selected)
    dispatcher = dispatcher_for(accounting_db)
    for _ in range(2):
        # Simulate restart with disabled environment defaults, not a registry override.
        monkeypatch.setattr(settings, 'repo_sandbox', RepoSandboxSettings())
        with current_task_service(dispatcher=dispatcher) as service:
            assert type(service.registry) is ToolRegistry
            assert dispatcher.general_tasks is service and service.started
            assert settings.repo_sandbox == selected
            source = service.repository_source_service
            assert source.jobs is dispatcher.jobs
            assert source.session_factory is dispatcher.session_provider
            assert source.sandbox.config == selected
            assert service.repository_work_adapter.__self__ is source
            assert service.repository_work_adapter.__func__ is source.native_iteration_adapter.__func__
            descriptor = repository_descriptor(service.registry)
            witness = service.registry.compile_capacity(descriptor)
            assert verify_task_tool_capacity(witness,
                descriptor_digest=digest(descriptor.model_dump(mode='json')))[0] is True
            # The stock classifier retains the approval-capable reservation;
            # it does not grant the no-approval/native capacity discount.
            assert witness.approval_possible is True
            assert not [item for item in service.registry.blocked_tools()
                if item['tool_id'] == 'repository_work']
            detached = descriptor.model_copy(deep=True)
            detached.input_schema['properties']['intent']['maxLength'] = 1
            assert repository_descriptor(service.registry).input_schema != detached.input_schema
            with pytest.raises(PermissionError, match='capacity contract changed'):
                service.registry.compile_capacity(detached)
            assert service.registry.compile_capacity(descriptor.model_copy(deep=True)).approval_possible
        assert dispatcher.general_tasks is None
        assert not service.started and not service.registry.started


@pytest.mark.parametrize('state,reason', [
    ('disabled', 'repository_executor_disabled'),
    ('malformed', 'repository_executor_disabled'),
    ('untrusted', 'repository_executor_disabled'),
    ('rootless', 'repository_executor_unsupported'),
])
async def test_stock_lifecycle_fail_closed_persisted_selection(accounting_db, monkeypatch, state, reason):
    workspace, _, _ = accounting_db
    workspace.chmod(0o700)
    selected = RepoSandboxSettings(enabled=state != 'disabled',
        executor_kind='docker_rootless' if state == 'rootless' else 'local')
    persist_repo_sandbox_settings(selected)
    path = workspace / 'artifacts/repo-sandbox/settings.json'
    if state == 'malformed':
        path.write_text('{invalid JSON')
    if state == 'untrusted':
        path.chmod(0o644)
    monkeypatch.setattr(settings, 'repo_sandbox', RepoSandboxSettings(enabled=True))
    dispatcher = dispatcher_for(accounting_db)
    with current_task_service(dispatcher=dispatcher) as service:
        assert 'repository_work' not in {item.tool_id for item in service.registry.descriptors()}
        assert service.repository_source_service is None and service.repository_work_adapter is None
        assert {'tool_id': 'repository_work', 'reason': reason} in service.registry.blocked_tools()
        if state in {'disabled', 'malformed', 'untrusted'}:
            assert settings.repo_sandbox.enabled is False
    assert dispatcher.general_tasks is None


@pytest.mark.parametrize('mode', ['safe', 'balanced'])
async def test_stock_policy_excludes_repository_without_source(accounting_db, monkeypatch, mode):
    workspace, _, _ = accounting_db
    workspace.chmod(0o700)
    persist_repo_sandbox_settings(RepoSandboxSettings(enabled=True))
    monkeypatch.setattr(context_manager.get_context(), 'tool_policy_mode', mode)
    with current_task_service(dispatcher=dispatcher_for(accounting_db)) as service:
        assert 'repository_work' not in {item.tool_id for item in service.registry.descriptors()}
        assert service.repository_source_service is None
        assert {'tool_id': 'repository_work', 'reason': 'tool_policy_denied'} in service.registry.blocked_tools()


async def test_repository_generic_entrypoints_deny_before_metadata_or_callback(accounting_db, monkeypatch):
    workspace, _, _ = accounting_db
    workspace.chmod(0o700)
    persist_repo_sandbox_settings(RepoSandboxSettings(enabled=True))
    with current_task_service(dispatcher=dispatcher_for(accounting_db)) as service:
        registry = service.registry
        descriptor = repository_descriptor(registry)
        principal = TrustPrincipal(principal_id='test:registration', principal_type=PrincipalType.OPERATOR,
            grants=(AuthorityGrant.CAPABILITY_EXECUTE,), session_id='original-session', job_id='original-job')
        calls = []
        def unexpected(*args, **kwargs):
            calls.append(True)
            raise AssertionError('generic repository callback or metadata reached')
        monkeypatch.setattr('src.tools.approval._tool_approval_context', unexpected)
        monkeypatch.setattr(registry, '_invoke_with_closure', unexpected)
        for _ in range(2):
            with pytest.raises(PermissionError, match='repository native Source owner required'):
                registry.approval_context(descriptor.model_copy(deep=True), selection(), job_id='original-job')
            with pytest.raises(PermissionError, match='repository native Source owner required'):
                registry.begin_invocation(descriptor.model_copy(deep=True), selection(), principal=principal,
                    job_id='original-job', fencing_token=1)
        assert calls == []
        changed = descriptor.model_copy(update={'policy_digest': '0' * 64})
        with pytest.raises(PermissionError, match='contract changed'):
            registry.begin_invocation(changed, selection(), principal=principal, job_id='original-job', fencing_token=1)
        monkeypatch.setattr(context_manager.get_context(), 'tool_policy_mode', 'safe')
        with pytest.raises(PermissionError, match='contract changed'):
            registry.approval_context(descriptor, selection(), job_id='original-job')
        assert calls == []


async def test_stock_lifecycle_failure_cleans_actual_dispatcher(accounting_db):
    workspace, _, _ = accounting_db
    workspace.chmod(0o700)
    persist_repo_sandbox_settings(RepoSandboxSettings(enabled=True))
    dispatcher = dispatcher_for(accounting_db)
    with pytest.raises(RuntimeError, match='later startup failed'):
        with current_task_service(dispatcher=dispatcher) as service:
            assert service.repository_source_service is not None
            raise RuntimeError('later startup failed')
    assert dispatcher.general_tasks is None
    assert not service.started and not service.registry.started


async def test_stock_duplicate_mcp_identity_remains_fail_closed(accounting_db, monkeypatch):
    workspace, _, _ = accounting_db
    workspace.chmod(0o700)
    persist_repo_sandbox_settings(RepoSandboxSettings(enabled=True))
    with current_task_service(dispatcher=dispatcher_for(accounting_db)) as service:
        descriptor = repository_descriptor(service.registry)
        class DuplicateCatalog:
            def task_tool_entries(self, extension_registry, mode):
                return [(descriptor.model_copy(deep=True), None)]
        monkeypatch.setattr(context_manager.get_context(), 'mcp_policy_mode', 'full')
        monkeypatch.setattr(service.registry, 'mcp_runtime', DuplicateCatalog())
        monkeypatch.setattr(service.registry, 'extension_registry', object())
        with pytest.raises(ValueError, match='duplicate task tool identity'):
            service.registry.descriptors()


async def test_stock_default_disabled_catalog_has_no_source(accounting_db):
    with current_task_service(dispatcher=dispatcher_for(accounting_db)) as service:
        assert service.repository_source_service is None
        assert service.repository_work_adapter is None
        assert {'tool_id': 'repository_work', 'reason': 'repository_executor_disabled'} in service.registry.blocked_tools()


async def test_nested_stock_owner_denies_before_selector_restore(accounting_db, monkeypatch):
    from src.execution import repo_sandbox
    workspace, _, _ = accounting_db
    workspace.chmod(0o700)
    selected = RepoSandboxSettings(enabled=True, executor_kind='local')
    persist_repo_sandbox_settings(selected)
    dispatcher = dispatcher_for(accounting_db)
    with current_task_service(dispatcher=dispatcher) as service:
        registry, source = service.registry, service.repository_source_service
        process_selector = settings.repo_sandbox
        original_descriptors = [item.model_dump(mode='json') for item in registry.descriptors()]
        # Original persisted settings owner changes disk only; the active
        # lifecycle's current process selector must not be replaced by a rival.
        persist_repo_sandbox_settings(RepoSandboxSettings(enabled=False))
        original_loader = repo_sandbox.load_persisted_repo_sandbox_settings
        loads = []
        def observe_loader():
            loads.append(True)
            return original_loader()
        monkeypatch.setattr(repo_sandbox, 'load_persisted_repo_sandbox_settings', observe_loader)
        with pytest.raises(RuntimeError, match='general task lifecycle already owned'):
            with current_task_service(dispatcher=dispatcher):
                raise AssertionError('second lifecycle must never acquire ownership')
        assert loads == []
        assert settings.repo_sandbox is process_selector and settings.repo_sandbox == selected
        assert dispatcher.general_tasks is service and service.started
        assert service.registry is registry and registry.started
        assert registry.communication_dispatcher is dispatcher
        assert service.repository_source_service is source
        assert source.jobs is dispatcher.jobs and source.session_factory is dispatcher.session_provider
        assert source.sandbox.config == selected
        assert service.repository_work_adapter.__self__ is source
        assert [item.model_dump(mode='json') for item in registry.descriptors()] == original_descriptors
    assert dispatcher.general_tasks is None
    assert not service.started and not registry.started
    assert registry.communication_dispatcher is None


async def test_live_enable_after_disabled_startup_keeps_absent_source_blocked(accounting_db, monkeypatch):
    workspace, engine, _ = accounting_db
    workspace.chmod(0o700)
    persist_repo_sandbox_settings(RepoSandboxSettings())
    dispatcher = dispatcher_for(accounting_db)
    with current_task_service(dispatcher=dispatcher) as service:
        assert service.repository_source_service is None
        before = await canonical_rows(dispatcher)
        await canonical_selector_update(enabled=True)
        tools, blocked = metadata_projection(service, monkeypatch, engine)
        assert 'repository_work' not in tools
        assert {'tool_id': 'repository_work', 'reason': 'repository_source_unavailable'} in blocked
        assert service.repository_source_service is None and service.repository_work_adapter is None
        assert dispatcher.general_tasks is service and service.started
        await canonical_selector_update(enabled=False)
        tools, blocked = metadata_projection(service, monkeypatch, engine)
        assert 'repository_work' not in tools
        assert {'tool_id': 'repository_work', 'reason': 'repository_executor_disabled'} in blocked
        assert await canonical_rows(dispatcher) == before


@pytest.mark.parametrize('profile', ['repo-python-pytest-v1', 'repo-node24-npm-v1'])
async def test_live_disable_restore_and_profile_change_preserve_original_owner(accounting_db, monkeypatch, profile):
    workspace, engine, _ = accounting_db
    workspace.chmod(0o700)
    selected = RepoSandboxSettings(enabled=True, profile=profile)
    persist_repo_sandbox_settings(selected)
    dispatcher = dispatcher_for(accounting_db)
    with current_task_service(dispatcher=dispatcher) as service:
        source = service.repository_source_service
        original = owned_runtime(source)
        original_adapter = service.repository_work_adapter
        maps = tuple(dict(item) for item in original[4:])
        before = await canonical_rows(dispatcher)
        tools, _ = metadata_projection(service, monkeypatch, engine)
        assert 'repository_work' in tools
        await canonical_selector_update(enabled=False)
        tools, blocked = metadata_projection(service, monkeypatch, engine)
        assert 'repository_work' not in tools
        assert {'tool_id': 'repository_work', 'reason': 'repository_executor_disabled'} in blocked
        await canonical_selector_update(enabled=True)
        tools, _ = metadata_projection(service, monkeypatch, engine)
        assert 'repository_work' in tools
        changed_profile = 'repo-node24-npm-v1' if profile == 'repo-python-pytest-v1' else 'repo-python-pytest-v1'
        await canonical_selector_update(profile=changed_profile)
        tools, blocked = metadata_projection(service, monkeypatch, engine)
        assert 'repository_work' not in tools
        assert {'tool_id': 'repository_work', 'reason': 'repository_configuration_changed'} in blocked
        await canonical_selector_update(profile=profile, node_runtime_path='/original-owner-stale-node')
        tools, blocked = metadata_projection(service, monkeypatch, engine)
        assert 'repository_work' not in tools
        assert {'tool_id': 'repository_work', 'reason': 'repository_configuration_changed'} in blocked
        await canonical_selector_update(node_runtime_path=selected.node_runtime_path)
        tools, _ = metadata_projection(service, monkeypatch, engine)
        assert 'repository_work' in tools
        assert all(actual is expected for actual, expected in zip(owned_runtime(source), original))
        assert service.repository_work_adapter is original_adapter
        assert tuple(dict(item) for item in original[4:]) == maps
        assert await canonical_rows(dispatcher) == before


@pytest.mark.parametrize('drift', ['source', 'jobs', 'session', 'adapter', 'service', 'dispatcher', 'dispatcher_reference',
    'executor', 'workspace', 'executor_workspace'])
async def test_stock_catalog_blocks_original_owner_drift_without_effects(accounting_db, monkeypatch, drift):
    workspace, engine, _ = accounting_db
    workspace.chmod(0o700)
    persist_repo_sandbox_settings(RepoSandboxSettings(enabled=True))
    dispatcher = dispatcher_for(accounting_db)
    with current_task_service(dispatcher=dispatcher) as service:
        source = service.repository_source_service
        original = owned_runtime(source)
        before = await canonical_rows(dispatcher)
        target, attribute = {
            'source': (service, 'repository_source_service'), 'jobs': (source, 'jobs'),
            'session': (source, 'session_factory'), 'adapter': (service, 'repository_work_adapter'),
            'service': (service.registry, 'delegation_service'), 'dispatcher': (dispatcher, 'general_tasks'),
            'dispatcher_reference': (service.registry, 'communication_dispatcher'),
            'executor': (source, 'sandbox'), 'workspace': (source, 'workspace_dir'),
            'executor_workspace': (source.sandbox, 'workspace_dir'),
        }[drift]
        with monkeypatch.context() as corrupt:
            corrupt.setattr(target, attribute, object() if drift == 'dispatcher_reference' else
                '/foreign-workspace' if drift in {'workspace', 'executor_workspace'} else None)
            tools, blocked = metadata_projection(service, monkeypatch, engine)
            assert 'repository_work' not in tools
            reason = ('repository_configuration_changed' if drift in {'executor', 'workspace', 'executor_workspace'}
                else 'repository_source_unavailable')
            assert {'tool_id': 'repository_work', 'reason': reason} in blocked
            # Existing policy and disabled-selector reasons retain precedence
            # even when an original owner/configuration negative also exists.
            corrupt.setattr(context_manager.get_context(), 'tool_policy_mode', 'safe')
            _, blocked = metadata_projection(service, monkeypatch, engine)
            assert {'tool_id': 'repository_work', 'reason': 'tool_policy_denied'} in blocked
        assert all(actual is expected for actual, expected in zip(owned_runtime(source), original))
        assert await canonical_rows(dispatcher) == before


async def test_custom_registry_lifecycle_remains_descriptor_owned(accounting_db):
    class CustomRegistry:
        def __init__(self):
            self.started = False
            self.descriptor_reads = 0
        def start(self):
            self.started = True
        def stop(self):
            self.started = False
        def descriptors(self):
            self.descriptor_reads += 1
            return []
    registry = CustomRegistry()
    dispatcher = dispatcher_for(accounting_db)
    with current_task_service(registry=registry, dispatcher=dispatcher) as service:
        assert service.registry is registry and service.started
        assert service.repository_source_service is None and service.repository_work_adapter is None
        assert registry.descriptor_reads == 1
        assert registry.communication_dispatcher is dispatcher
    assert dispatcher.general_tasks is None and not service.started and not registry.started
    assert registry.communication_dispatcher is None


async def test_stock_owner_trailing_slash_workspace_remains_available_without_probes(accounting_db, monkeypatch):
    workspace, engine, _ = accounting_db
    workspace.chmod(0o700)
    configured_workspace = str(workspace) + '/'
    monkeypatch.setattr(settings, 'workspace_dir', configured_workspace)
    persist_repo_sandbox_settings(RepoSandboxSettings(enabled=True))
    dispatcher = dispatcher_for(accounting_db)
    with current_task_service(dispatcher=dispatcher) as service:
        source = service.repository_source_service
        original = owned_runtime(source)
        before = await canonical_rows(dispatcher)
        assert source.workspace_dir == settings.workspace_dir == configured_workspace
        assert source.sandbox.workspace_dir == workspace
        tools, blocked = metadata_projection(service, monkeypatch, engine)
        assert 'repository_work' in tools
        assert not [item for item in blocked if item['tool_id'] == 'repository_work']
        descriptor = repository_descriptor(service.registry)
        witness = service.registry.compile_capacity(descriptor)
        assert verify_task_tool_capacity(witness,
            descriptor_digest=digest(descriptor.model_dump(mode='json')))[0] is True
        assert all(actual is expected for actual, expected in zip(owned_runtime(source), original))
        assert await canonical_rows(dispatcher) == before
