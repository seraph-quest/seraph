"""Stock catalog and original Python lifecycle; no fixture registry authority."""
import socket

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
