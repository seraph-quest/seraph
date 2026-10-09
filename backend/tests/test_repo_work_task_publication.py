"""Genuine authenticated SQLite Task publication, without tool execution.

The registered descriptor data comes from the fixed bundled factory; production
registration remains inactive. Explicit reviewed plans incur no planning call.
"""
import json
import asyncio

import pytest
from sqlalchemy import select

from config.settings import settings, RepoSandboxSettings
from src.db.models import WorkBoardTask, WorkflowRunState, OperatorSession, InferenceCostReservation
from src.native_tools.task_adapters import repository_work_descriptor
from src.work_board.contracts import (GeneralTaskCreate, GeneralTaskInput,
    GeneralTaskPlanUpdate, PlanSpec, TaskLimits)
from src.work_board.general_task import GeneralTaskService, digest
from src.work_board.repository import BoardError
from src.workflows.repo_repair import RepoRepairService, RepoWorkInput
from tests.test_general_task_planner import accounting_db, prepare, forbid_external_inference
from tests.test_repo_work_source import git
from tests.test_repo_work_contracts import selection


async def actual_publication(accounting_db, monkeypatch):
    _, owner = await prepare(accounting_db, monkeypatch)
    workspace, _, factory = accounting_db
    # The persisted selector owner requires a private directory, unlike the
    # broader accounting fixture's disposable workspace.
    workspace.chmod(0o700)
    monkeypatch.setattr(settings, 'repo_sandbox', RepoSandboxSettings(enabled=True,
        executor_kind='local', profile='repo-python-pytest-v1'))
    from src.execution.repo_sandbox import persist_repo_sandbox_settings
    persist_repo_sandbox_settings(settings.repo_sandbox)
    repository = workspace / 'example'
    (repository / 'tests').mkdir(parents=True)
    (repository / 'calculator.py').write_text('def add(a, b):\n    return a - b\n')
    (repository / 'tests/test_calculator.py').write_text('from calculator import add\ndef test_add():\n    assert add(1, 2) == 3\n')
    git(repository, 'init', '--template=', '--initial-branch=develop')
    git(repository, 'add', '.')
    git(repository, 'commit', '-m', 'actual source')
    work = RepoWorkInput.model_validate(selection(repository_ref='example',
        base_commit=git(repository, 'rev-parse', 'HEAD').decode().strip()))
    source = RepoRepairService()
    assert source.sandbox.config.model_dump(mode='json') == settings.repo_sandbox.model_dump(mode='json')
    class FixedDescriptorView:
        def descriptors(self):
            return [repository_work_descriptor()]
        async def invoke(self, *args, **kwargs):
            raise AssertionError('Task publication must execute no tool')
    registry = FixedDescriptorView()
    descriptor = registry.descriptors()[0]
    request = GeneralTaskCreate(goal_revision=1, idempotency_key='source-original',
        expected_plan_revision=1,
        input=GeneralTaskInput(goal_ref='goal:fixture', intent='Repair the selected repository',
            requested_output=descriptor.output_schema,
            limits=TaskLimits(max_inference_calls=5, max_cost_microusd=500, wall_seconds=900),
            tool_set_digest=digest([descriptor.model_dump(mode='json')])),
        plan=PlanSpec(revision=1, steps=[{'step_id': 'repair', 'tool_id': 'repository_work',
            'input': work.model_dump(mode='json'), 'output_contract': descriptor.output_schema}]))
    service = GeneralTaskService(registry, repository_source_service=source)
    service.start()
    return factory, workspace, owner, service, request


@pytest.mark.asyncio
async def test_actual_original_task_artifact_and_scoped_replay(accounting_db, monkeypatch):
    factory, workspace, owner, service, request = await actual_publication(accounting_db, monkeypatch)
    async with factory() as db:
        mutation = await service.create(db, owner, request)
        task_id, artifact_digest = mutation.task.task_id, mutation.task.typed_input_digest
    async with factory() as db:
        replay = await service.create(db, owner, request)
        assert replay.idempotent_replay and replay.task.task_id == task_id
        assert replay.task.typed_input_digest == artifact_digest
        public = await service.plan(db, owner, task_id)
        assert 'repository_source' not in public
        assert len(list((await db.execute(select(WorkBoardTask))).scalars())) == 1
        assert list((await db.execute(select(WorkflowRunState))).scalars()) == []
        assert list((await db.execute(select(InferenceCostReservation))).scalars()) == []
        from src.work_board.dispatcher import _parse_typed_input
        stored = _parse_typed_input(replay.task)
        assert stored['repository_source']['original_root_id'] == owner.session_id
        assert stored['repository_source']['original_input_digest'] == digest(request.plan.steps[0].input)
        update = GeneralTaskPlanUpdate(expected_revision=replay.task.task_revision,
            expected_plan_revision=1, idempotency_key='reject-source-edit',
            plan=request.plan.model_copy(update={'revision': 2}))
        with pytest.raises(BoardError) as denied:
            await service.update_plan(db, owner, task_id, update)
        assert denied.value.code == 'repository_source_plan_immutable'
        assert replay.task.typed_input_digest == artifact_digest


@pytest.mark.asyncio
async def test_settings_put_waits_for_actual_source_publication_commit(accounting_db, monkeypatch):
    factory, _, owner, service, request = await actual_publication(accounting_db, monkeypatch)
    from src.api import settings as settings_api
    from starlette.requests import Request
    from src.execution.repo_sandbox import load_persisted_repo_sandbox_settings
    reached, release = asyncio.Event(), asyncio.Event()
    original = service.repository.create_task
    async def pause_after_actual_writer(*args, **kwargs):
        mutation = await original(*args, **kwargs)
        reached.set()
        await release.wait()
        return mutation
    monkeypatch.setattr(service.repository, 'create_task', pause_after_actual_writer)
    async def readiness():
        return {'enabled': settings.repo_sandbox.enabled}
    monkeypatch.setattr(settings_api, 'get_repo_sandbox_settings', readiness)
    async def publish():
        async with factory() as db:
            return await service.create(db, owner, request)
    publication = asyncio.create_task(publish())
    await asyncio.wait_for(reached.wait(), 5)
    put = asyncio.create_task(settings_api.set_repo_sandbox_settings(
        settings_api.RepoSandboxSettingsRequest(enabled=False),
        Request({'type': 'http', 'client': ('127.0.0.1', 1234)})))
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not put.done()
        assert settings.repo_sandbox.enabled
        assert load_persisted_repo_sandbox_settings()[0].enabled
        release.set()
        mutation = await asyncio.wait_for(publication, 5)
        assert (await asyncio.wait_for(put, 5)) == {'enabled': False}
        async with factory() as db:
            task = await db.scalar(select(WorkBoardTask).where(
                WorkBoardTask.task_id == mutation.task.task_id))
            assert task is not None
        assert not load_persisted_repo_sandbox_settings()[0].enabled
    finally:
        release.set()
        await asyncio.gather(publication, put, return_exceptions=True)


@pytest.mark.asyncio
async def test_settings_failed_persist_preserves_original_selectors(accounting_db, monkeypatch):
    await actual_publication(accounting_db, monkeypatch)
    from src.api import settings as settings_api
    from starlette.requests import Request
    from fastapi import HTTPException
    from src.execution.repo_sandbox import load_persisted_repo_sandbox_settings
    original = settings.repo_sandbox.model_dump(mode='json')
    def failed_write(candidate):
        raise OSError('disposable injected persistence failure')
    monkeypatch.setattr(settings_api, '_persist_repo_sandbox_settings', failed_write)
    with pytest.raises(HTTPException) as denied:
        await settings_api.set_repo_sandbox_settings(
            settings_api.RepoSandboxSettingsRequest(enabled=False),
            Request({'type': 'http', 'client': ('127.0.0.1', 1234)}))
    assert denied.value.status_code == 503
    assert settings.repo_sandbox.model_dump(mode='json') == original
    assert load_persisted_repo_sandbox_settings()[0].model_dump(mode='json') == original


@pytest.mark.asyncio
async def test_actual_source_task_original_native_child_binding(accounting_db, monkeypatch):
    factory, _, owner, service, request = await actual_publication(accounting_db, monkeypatch)
    from src.work_board.dispatcher import WorkBoardDispatcher
    from src.work_board.contracts import GeneralTaskEnvelope
    from src.work_board.general_task_native import (
        initialize_interpreter, admit_native_step, publish_positive_claim)
    from src.workflows.general_task_guard import child_binding, assert_general_task_child_current
    from src.workflows.job_runtime import _digest
    dispatcher = WorkBoardDispatcher(session_provider=factory.accounting_sessions, general_tasks=service)
    jobs = dispatcher.jobs
    async with factory.accounting_sessions() as db:
        created = await service.create(db, owner, request.model_copy(update={'accept': True}))
    async with factory.accounting_sessions() as db:
        ready = await service.repository.promote_task_ready(db, created.task.task_id,
            expected_revision=created.task.task_revision,
            actor_principal_id=dispatcher.runner_id, actor_session_id=dispatcher.runner_session)
    async with factory.accounting_sessions() as db:
        claimed = await service.repository.claim_ready_task(db, created.task.task_id,
            expected_revision=ready.task.task_revision, lease_owner=dispatcher.runner_id)
    task, attempt = claimed.task, claimed.attempt
    spec, inputs, *_ = dispatcher._build_spec(task, attempt)
    admitted = await jobs.admit_job(spec)
    async with factory.accounting_sessions() as db:
        linked = await service.repository.link_attempt_workflow_run(db, task.task_id, attempt.attempt_id,
            workflow_run_id=spec.identity.job_id, expected_revision=task.task_revision,
            board_fence=attempt.fencing_token, lease_owner=attempt.lease_owner,
            workflow_projection=admitted,
            expected_identity={'job_id': spec.identity.job_id, 'owner_kind': spec.identity.owner_kind,
                'owner_principal_id': spec.identity.owner_principal_id, 'service_id': spec.service_id,
                'operator_session_id': spec.operator_session_id, 'session_id': spec.session_id,
                'goal_id': spec.goal_id, 'goal_revision': spec.goal_revision,
                'job_kind': spec.identity.job_kind, 'capability_version': spec.identity.capability_version,
                'input_digest': _digest(spec.inputs), 'authority_digest': _digest(spec.declared_authority),
                'run_fingerprint': spec.run_fingerprint, 'idempotency_scope': spec.identity.idempotency_scope,
                'idempotency_key': spec.identity.idempotency_key})
    await jobs.queue_job(spec.identity.job_id)
    parent = await jobs.claim_job(spec.identity.job_id,
        owner=dispatcher.runner_id + ':' + linked.attempt.attempt_id)
    manifest = await initialize_interpreter(jobs, spec.identity.job_id,
        owner=parent['lease']['owner'], fence=parent['lease']['fencing_token'], service=service)
    envelope = GeneralTaskEnvelope.model_validate(inputs)
    assert envelope.repository_source is not None
    child = await admit_native_step(jobs, spec.identity.job_id,
        owner=parent['lease']['owner'], fence=parent['lease']['fencing_token'],
        step=envelope.plan.steps[0], descriptor=envelope.descriptors[0],
        inputs=envelope.plan.steps[0].input, service=service)
    child_id = child['run_identity']
    await jobs.queue_job(child_id)
    claimed_child = await jobs.claim_job(child_id, owner='actual-repository-native-worker', lease_seconds=900)
    async with factory.accounting_sessions() as db:
        actual_child = await jobs._fetch(db, child_id)
        binding = child_binding(actual_child)
    await publish_positive_claim(jobs, binding,
        child_owner=claimed_child['lease']['owner'], child_fence=claimed_child['lease']['fencing_token'])
    async with factory.accounting_sessions() as db:
        actual_child = await jobs._fetch(db, child_id)
        await assert_general_task_child_current(db, actual_child)
        assert binding.task_id == created.task.task_id
        assert binding.attempt_id == linked.attempt.attempt_id
        assert binding.original_root_id == owner.session_id
        assert binding.input_digest == digest(request.plan.steps[0].input)
        assert len(list((await db.execute(select(WorkBoardTask))).scalars())) == 1
        assert list((await db.execute(select(InferenceCostReservation))).scalars()) == []



@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['profile', 'source', 'root'])
async def test_actual_scoped_replay_cannot_regrant_changed_authority(accounting_db, monkeypatch, change):
    factory, workspace, owner, service, request = await actual_publication(accounting_db, monkeypatch)
    async with factory() as db:
        mutation = await service.create(db, owner, request)
        original_id, original_digest = mutation.task.task_id, mutation.task.typed_input_digest
    if change == 'profile':
        monkeypatch.setattr(settings, 'repo_sandbox', settings.repo_sandbox.model_copy(update={'enabled': False}))
        from src.execution.repo_sandbox import persist_repo_sandbox_settings
        persist_repo_sandbox_settings(settings.repo_sandbox)
    elif change == 'source':
        (workspace / 'example/calculator.py').write_text('OPERATOR_CHANGE = True\n')
    else:
        from datetime import datetime, timezone
        async with factory() as db:
            root = await db.get(OperatorSession, owner.session_id)
            root.revoked_at = datetime.now(timezone.utc).replace(tzinfo=None)
            await db.commit()
    async with factory() as db:
        with pytest.raises(Exception):
            await service.create(db, owner, request)
        task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == original_id))
        assert task.typed_input_digest == original_digest
        assert len(list((await db.execute(select(WorkBoardTask))).scalars())) == 1
        assert list((await db.execute(select(WorkflowRunState))).scalars()) == []
        assert list((await db.execute(select(InferenceCostReservation))).scalars()) == []
