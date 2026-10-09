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


async def actual_publication(accounting_db, monkeypatch, *, goal_capacity=None, language='test_python', node_build=False, ordinary_prior=False, publication_profile=False):
    _, owner = await prepare(accounting_db, monkeypatch)
    workspace, _, factory = accounting_db
    if goal_capacity is not None:
        # This is the ORIGINAL Goal policy, established before the first C1
        # Task/group. No accepted Task, Goal revision or grant is refreshed.
        from src.db.models import Goal
        from src.goals.contracts import GoalAdmissionBudget
        async with factory.accounting_sessions() as db:
            goal = await db.get(Goal, 'goal:fixture')
            goal.admission_budget_json = GoalAdmissionBudget(max_outstanding_jobs=goal_capacity,
                max_runtime_seconds=900).model_dump_json()
    # The persisted selector owner requires a private directory, unlike the
    # broader accounting fixture's disposable workspace.
    workspace.chmod(0o700)
    monkeypatch.setattr(settings, 'repo_sandbox', RepoSandboxSettings(enabled=True,
        executor_kind='local', profile='repo-python-pytest-publication-v1' if publication_profile else 'repo-python-pytest-v1'))
    from src.execution.repo_sandbox import persist_repo_sandbox_settings
    persist_repo_sandbox_settings(settings.repo_sandbox)
    repository = workspace / 'example'
    (repository / 'tests').mkdir(parents=True)
    (repository / 'calculator.py').write_text('def add(a, b):\n    return a - b\n')
    (repository / 'tests/test_calculator.py').write_text('from calculator import add\ndef test_add():\n    assert add(1, 2) == 3\n')
    if language == 'test_node':
        from pathlib import Path
        node = Path('/home/pawel/repos/seraph/.agent-worktrees/986-a1-host/.agent-evidence/986/a1-upstream/node-v24.13.1-linux-x64/bin/node')
        assert node.is_file(), 'Required cached fixed Node24 runtime must exist; no ambient fallback or download'
        monkeypatch.setattr(settings, 'repo_sandbox', RepoSandboxSettings(enabled=True,
            executor_kind='local', profile='repo-node24-npm-v1', node_runtime_path=str(node)))
        persist_repo_sandbox_settings(settings.repo_sandbox)
        (repository / 'calculator.py').unlink()
        (repository / 'tests/test_calculator.py').unlink()
        (repository / 'calculator.js').write_text('exports.add = (a, b) => a - b;\n')
        (repository / 'tests/calculator.test.js').write_text("require('node:assert/strict').equal(require('../calculator.js').add(1, 2), 3);\n")
        (repository / 'package.json').write_text(json.dumps({'name': 'actual-source', 'version': '1.0.0',
            'scripts': {'test': 'node --test tests/calculator.test.js'}}))
        (repository / 'package-lock.json').write_text(json.dumps({'name': 'actual-source', 'lockfileVersion': 3, 'packages': {}}))
        if node_build:
            import shutil
            typescript = Path('/home/pawel/repos/seraph/frontend/node_modules/typescript')
            assert (typescript / 'package.json').is_file(), 'Existing cached TypeScript is required; no installation'
            shutil.copytree(typescript, repository / 'node_modules/typescript')
            version = json.loads((typescript / 'package.json').read_text())['version']
            (repository / '.gitignore').write_text('node_modules/\ndist/\n')
            (repository / 'tsconfig.json').write_text(json.dumps({'compilerOptions': {
                'allowJs': True, 'outDir': 'dist'}, 'include': ['calculator.js']}))
            package = json.loads((repository / 'package.json').read_text())
            package['scripts']['build'] = 'tsc --project tsconfig.json'
            (repository / 'package.json').write_text(json.dumps(package))
            (repository / 'package-lock.json').write_text(json.dumps({'name': 'actual-source',
                'lockfileVersion': 3, 'packages': {'node_modules/typescript': {'version': version}}}))
    git(repository, 'init', '--template=', '--initial-branch=develop')
    git(repository, 'add', '.')
    git(repository, 'commit', '-m', 'actual source')
    work = RepoWorkInput.model_validate(selection(repository_ref='example', language_profile=language,
        requested_checks=['build', 'test'] if node_build else ['test'],
        allowed_paths=(['calculator.js', 'tests/calculator.test.js'] if language == 'test_node' else ['calculator.py', 'tests/test_calculator.py']),
        base_commit=git(repository, 'rev-parse', 'HEAD').decode().strip()))
    source = RepoRepairService(session_factory=factory)
    assert source.sandbox.config.model_dump(mode='json') == settings.repo_sandbox.model_dump(mode='json')
    class FixedDescriptorView:
        def descriptors(self):
            return [repository_work_descriptor()]
        async def invoke(self, *args, **kwargs):
            raise AssertionError('Task publication must execute no tool')
    registry = FixedDescriptorView()
    if ordinary_prior:
        from tests.test_general_task_contract import Registry
        registry = Registry()
        registry.entries.insert(0, repository_work_descriptor())
    descriptor = registry.descriptors()[0]
    request = GeneralTaskCreate(goal_revision=1, idempotency_key='source-original',
        expected_plan_revision=1,
        input=GeneralTaskInput(goal_ref='goal:fixture', intent='Repair the selected repository',
            requested_output=descriptor.output_schema,
            limits=TaskLimits(max_inference_calls=5, max_cost_microusd=500, wall_seconds=900),
            tool_set_digest=digest([item.model_dump(mode='json') for item in
                sorted(registry.descriptors(), key=lambda item: item.tool_id)])),
        plan=PlanSpec(revision=1, steps=[{'step_id': 'repair', 'tool_id': 'repository_work',
            'input': work.model_dump(mode='json'), 'output_contract': descriptor.output_schema}]))
    if ordinary_prior:
        prior_descriptor = registry.descriptors()[1]
        repair = request.plan.steps[0].model_copy(update={'depends_on': ['prior']})
        request = request.model_copy(update={'plan': PlanSpec(revision=1, steps=[
            {'step_id': 'prior', 'tool_id': prior_descriptor.tool_id,
                'input': {'text': 'actual original ordinary callback'},
                'output_contract': prior_descriptor.output_schema}, repair])})
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


async def actual_native_source(accounting_db, monkeypatch, *, goal_capacity=None, claim_child=True, language='test_python', node_build=False, ordinary_prior=False, publication_profile=False):
    factory, _, owner, service, request = await actual_publication(accounting_db, monkeypatch,
        goal_capacity=goal_capacity, language=language, node_build=node_build, ordinary_prior=ordinary_prior,
        publication_profile=publication_profile)
    return await admit_existing_source_request(factory, owner, service, request,
        claim_child=claim_child, ordinary_prior=ordinary_prior)


async def admit_existing_source_request(factory, owner, service, request, *, claim_child=True, ordinary_prior=False):
    """Admit another distinct original Task through the unchanged real owners."""
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
        assert claimed is not None, {key: getattr(ready.task, key) for key in
            ('status', 'block_kind', 'block_reason', 'scheduled_at', 'task_revision')}
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
    if ordinary_prior:
        from src.work_board.general_task_native import run_native_step
        from src.workflows.general_task_guard import read_manifest
        from src.auth.service import authenticate_session
        prior = envelope.plan.steps[0]
        prior_descriptor = next(item for item in envelope.descriptors if item.tool_id == prior.tool_id)
        prior_binding, _ = await admit_native_step(jobs, spec.identity.job_id,
            owner=parent['lease']['owner'], fence=parent['lease']['fencing_token'],
            step=prior, descriptor=prior_descriptor, inputs=prior.input, service=service)
        principal = (await authenticate_session(owner.session_id, touch=False)).principal
        await run_native_step(service, jobs, prior_binding, child_owner='actual-prior-native-worker', principal=principal)
        async with factory.accounting_sessions() as db:
            original_parent = await jobs._fetch(db, spec.identity.job_id)
            manifest = read_manifest(original_parent)
        resumed = await jobs.resume_general_task_native_parent(spec.identity.job_id,
            owner=parent['lease']['owner'], expected_revision=original_parent.revision,
            expected_manifest_revision=manifest.manifest_revision)
        parent = resumed['job']
    selected_step = envelope.plan.steps[-1]
    selected_descriptor = next(item for item in envelope.descriptors if item.tool_id == selected_step.tool_id)
    binding, child = await admit_native_step(jobs, spec.identity.job_id,
        owner=parent['lease']['owner'], fence=parent['lease']['fencing_token'],
        step=selected_step, descriptor=selected_descriptor,
        inputs=selected_step.input, service=service)
    child_id = binding.invocation_id
    if not claim_child:
        return factory, owner, service, jobs, binding, request
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
        assert binding.input_digest == digest(request.plan.steps[-1].input)
        assert len(list((await db.execute(select(WorkBoardTask))).scalars())) == 1
        assert list((await db.execute(select(InferenceCostReservation))).scalars()) == []
    return factory, owner, service, jobs, binding, request


@pytest.mark.asyncio
async def test_actual_source_task_original_native_child_binding(accounting_db, monkeypatch):
    await actual_native_source(accounting_db, monkeypatch)


@pytest.mark.asyncio
async def test_actual_precontact_owner_prepares_original_child_once(accounting_db, monkeypatch):
    from src.auth.service import authenticate_session
    from src.workflows.repo_repair_source import (prepare_repository_native_source,
        repository_review_projection, repository_source_preview, read_repository_original)
    from src.db.models import WorkBoardAttempt
    factory, owner, service, jobs, binding, _request = await actual_native_source(
        accounting_db, monkeypatch, goal_capacity=2, claim_child=False)
    operator = await authenticate_session(owner.session_id, touch=False)
    before = await jobs.get_job(binding.invocation_id)
    assert before['attempt_count'] == 0
    prepared = await prepare_repository_native_source(service, jobs, binding,
        child_owner='actual-source-preparation-worker', principal=operator.principal)
    assert prepared['awaiting_repository_consent'] is True
    child = await jobs.get_job(binding.invocation_id)
    assert child['attempt_count'] == 1 and child['lease']['fencing_token'] == 1
    async with factory() as db:
        task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == binding.task_id))
        attempt = await db.get(WorkBoardAttempt, binding.attempt_id)
        review = await repository_review_projection(db, task=task, attempt=attempt, owner=owner)
        assert review['native_child_id'] == binding.invocation_id
        root = await jobs._fetch(db, review['repository_job_id'])
        original = read_repository_original(root)[0]
        assert original['native_binding'] == binding.model_dump(mode='json')
        assert len(list((await db.execute(select(WorkBoardTask))).scalars())) == 2
        assert list((await db.execute(select(InferenceCostReservation))).scalars()) == []
    preview = await repository_source_preview(service.repository_source_service, jobs,
        job_id=review['repository_job_id'], owner=owner)
    assert preview['provider_contacted'] is False
    assert preview['egress']['diagnostics']['stdout'] == ''
    assert preview['egress']['diagnostics']['stderr'] == ''
    assert preview['egress']['combined_input_bytes'] <= 65536
    replay = await prepare_repository_native_source(service, jobs, binding,
        child_owner='actual-source-preparation-worker', principal=operator.principal)
    assert replay == prepared
    after = await jobs.get_job(binding.invocation_id)
    assert after['lease'] == child['lease'] and after['attempt_count'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['test_python', 'test_node'])
@pytest.mark.parametrize('entry', ['source', 'parent_task', 'repository_task'])
async def test_actual_source_stop_before_contact_atomic_original(accounting_db, monkeypatch, language, entry):
    from src.auth.service import authenticate_session
    from src.workflows.repo_repair_source import prepare_repository_native_source
    from src.workflows.repo_repair_stop import stop_repository_root
    factory, owner, service, jobs, binding, _ = await actual_native_source(
        accounting_db, monkeypatch, goal_capacity=2, claim_child=False, language=language)
    operator = await authenticate_session(owner.session_id, touch=False)
    prepared = await prepare_repository_native_source(service, jobs, binding,
        child_owner='actual-stop-worker', principal=operator.principal)
    root_id = prepared['repository_job_id']
    actual_cancel = jobs.cancel_general_task_native_parent
    async def sql_only_cancel(*args, **kwargs):
        from dataclasses import replace
        from src.workflows.job_runtime import DurableJobLeaseError
        copied = replace(kwargs['repository_stop_witness'])
        with pytest.raises(DurableJobLeaseError, match='source-owned'):
            await actual_cancel(*args, **{**kwargs, 'repository_stop_witness': copied})
        def forbidden(*_args, **_kwargs):
            raise AssertionError('Original stop writer must use staged Source, not private IO/projection')
        with monkeypatch.context() as writer_boundary:
            writer_boundary.setattr(service.repository_source_service, '_read_private_artifact', forbidden)
            writer_boundary.setattr('src.work_board.pipelines.root_binding', forbidden)
            writer_boundary.setattr('src.work_board.repository.WorkBoardRepository._project_attempt', forbidden)
            return await actual_cancel(*args, **kwargs)
    monkeypatch.setattr(jobs, 'cancel_general_task_native_parent', sql_only_cancel)
    if entry == 'source':
        stopped = await stop_repository_root(service.repository_source_service, jobs,
            job_id=root_id, owner=owner, general_task_service=service)
        assert stopped['pending'] is False
    else:
        from src.work_board.dispatcher import WorkBoardDispatcher
        from src.workflows.repo_repair_source import read_repository_original
        async with factory() as db:
            original_scope = read_repository_original(await jobs._fetch(db, root_id))[0]
            task_id = binding.task_id if entry == 'parent_task' else original_scope['repository_task_id']
            task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == task_id))
        dispatcher = WorkBoardDispatcher(session_provider=factory.accounting_sessions, general_tasks=service)
        dispatcher.jobs = jobs
        projected = await dispatcher.cancel_task(owner, task_id, expected_revision=task.task_revision)
        assert projected.task.task_id == task_id and projected.task.status.value == 'blocked'
        assert projected.attempt.ended_at is not None
    root = await jobs.get_job(root_id)
    child = await jobs.get_job(binding.invocation_id)
    parent = await jobs.get_job(binding.parent_job_id)
    assert root['status'] == child['status'] == parent['status'] == 'cancelled'
    assert child['attempt_count'] == 1
    assert root_id not in service.repository_source_service._iterative_lanes
    async with factory() as db:
        released = jobs._repo_repair_reservation_state(await jobs._fetch(db, root_id))
        assert released['status'] == 'released' and released['outcome_status'] == 'cancelled'
        assert list((await db.execute(select(InferenceCostReservation))).scalars()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['test_python', 'test_node'])
async def test_actual_source_terminal_release_admits_distinct_successor(accounting_db, monkeypatch, language):
    from src.auth.service import authenticate_session
    from src.workflows.repo_repair_source import prepare_repository_native_source, read_repository_original, _repository_record
    from src.workflows.repo_repair_stop import stop_repository_root
    factory, owner, service, jobs, binding, request = await actual_native_source(
        accounting_db, monkeypatch, goal_capacity=2, claim_child=False, language=language)
    operator = await authenticate_session(owner.session_id, touch=False)
    first = await prepare_repository_native_source(service, jobs, binding,
        child_owner='actual-first-root-worker', principal=operator.principal)
    root_id = first['repository_job_id']
    async with factory() as db:
        original = await jobs._fetch(db, root_id)
        inventory = _repository_record(original, 'repository:inventory:v1')['identities']
        assert len(inventory) == 44 and 'repo-repair-execution-release' in inventory
        first_scope = read_repository_original(original)[0]
    stopped = await stop_repository_root(service.repository_source_service, jobs,
        job_id=root_id, owner=owner, general_task_service=service)
    assert stopped['pending'] is False
    new_request = request.model_copy(update={'idempotency_key': 'explicit-distinct-successor'})
    _, _, same_service, successor_jobs, next_binding, _ = await admit_existing_source_request(
        factory, owner, service, new_request, claim_child=False)
    assert same_service is service and next_binding.parent_job_id != binding.parent_job_id
    next_root = await prepare_repository_native_source(service, successor_jobs, next_binding,
        child_owner='actual-successor-root-worker', principal=operator.principal)
    assert next_root['repository_job_id'] != root_id
    async with factory() as db:
        prior = await successor_jobs._fetch(db, root_id)
        current = await successor_jobs._fetch(db, next_root['repository_job_id'])
        assert successor_jobs._repo_repair_reservation_state(prior)['status'] == 'released'
        assert successor_jobs._repo_repair_reservation_state(current)['status'] == 'held'
        assert read_repository_original(prior)[0] == first_scope
    assert next_root['repository_job_id'] in service.repository_source_service._iterative_lanes


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['second_cas', 'event'])
async def test_actual_source_stop_writer_failure_retains_original(accounting_db, monkeypatch, failure):
    from src.auth.service import authenticate_session
    from src.workflows.repo_repair_source import prepare_repository_native_source
    from src.workflows import repo_repair_stop as stop
    from src.workflows.job_runtime import DurableJobLeaseError
    from src.work_board.repository import WorkBoardRepository
    from src.db.models import WorkBoardAttempt
    factory, owner, service, jobs, binding, _ = await actual_native_source(
        accounting_db, monkeypatch, goal_capacity=2, claim_child=False)
    operator = await authenticate_session(owner.session_id, touch=False)
    prepared = await prepare_repository_native_source(service, jobs, binding,
        child_owner='actual-stop-rollback-worker', principal=operator.principal)
    root_id = prepared['repository_job_id']
    original = {identity: await jobs.get_job(identity) for identity in (root_id, binding.invocation_id, binding.parent_job_id)}
    if failure == 'second_cas':
        actual_cas = stop._cas_repository_stop_board_row
        async def lose_second(db, row, values):
            if isinstance(row, WorkBoardAttempt):
                raise DurableJobLeaseError('injected actual second CAS loss')
            return await actual_cas(db, row, values)
        monkeypatch.setattr(stop, '_cas_repository_stop_board_row', lose_second)
    else:
        actual_event = WorkBoardRepository._event
        async def fail_event(*args, **kwargs):
            if kwargs['kind'] == 'attempt.repository_stopped':
                raise DurableJobLeaseError('injected actual terminal event failure')
            return await actual_event(*args, **kwargs)
        monkeypatch.setattr(WorkBoardRepository, '_event', fail_event)
    result = await stop.stop_repository_root(service.repository_source_service, jobs,
        job_id=root_id, owner=owner, general_task_service=service)
    assert result['pending'] is True
    for identity, before in original.items():
        after = await jobs.get_job(identity)
        assert after['status'] == before['status']
        if identity == root_id:
            assert after['revision'] == before['revision'] + 1  # the retained stop intent
            assert {key: value for key, value in after['lease'].items() if key != 'revision'} == {
                key: value for key, value in before['lease'].items() if key != 'revision'}
        else:
            assert after['lease'] == before['lease']
    assert root_id in service.repository_source_service._iterative_lanes
    async with factory() as db:
        repo_task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id ==
            stop._source().read_repository_original(await jobs._fetch(db, root_id))[0]['repository_task_id']))
        assert repo_task.status.value == 'running'
        assert list((await db.execute(select(InferenceCostReservation))).scalars()) == []
    # The actual retained stop snapshot must reject unrelated row drift before
    # reopening any private artifact. A fresh service cannot reseal that drift.
    async with factory() as db:
        parent_task = await db.scalar(select(WorkBoardTask).where(
            WorkBoardTask.task_id == binding.task_id))
        previous_title = parent_task.title
        parent_task.title = previous_title + ' changed after stop intent'
        await db.commit()
    def forbidden_stop_read(*args, **kwargs):
        raise AssertionError('Retained stop scope drift must deny before private read')
    with monkeypatch.context() as boundary:
        boundary.setattr(service.repository_source_service, '_read_private_artifact', forbidden_stop_read)
        with pytest.raises(DurableJobLeaseError, match='snapshot changed before private read'):
            await stop._context(service.repository_source_service, jobs, job_id=root_id, owner=owner)
    async with factory() as db:
        parent_task = await db.scalar(select(WorkBoardTask).where(
            WorkBoardTask.task_id == binding.task_id))
        parent_task.title = previous_title
        await db.commit()
        run = await jobs._fetch(db, root_id)
        retained = stop._source()._repository_record(run, stop.STOP_ID)
    from pathlib import Path
    snapshot_ref = retained['snapshot_artifact_ref']
    assert snapshot_ref.startswith('workspace-json:artifacts/repo-repair/stop-')
    (Path(settings.workspace_dir) / snapshot_ref.removeprefix('workspace-json:')).unlink()
    with pytest.raises((OSError, ValueError, DurableJobLeaseError)):
        await stop._context(service.repository_source_service, jobs, job_id=root_id, owner=owner)
    assert root_id in service.repository_source_service._iterative_lanes
    assert (await jobs.get_job(root_id))['status'] == original[root_id]['status']


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['test_python', 'test_node'])
@pytest.mark.parametrize('drift', ['unchanged', 'unknown_task_field'])
async def test_actual_source_stop_restart_rebinds_only_original_physical_owner(accounting_db, monkeypatch, language, drift):
    from src.auth.service import authenticate_session
    from src.workflows.repo_repair_source import prepare_repository_native_source
    from src.workflows.repo_repair_stop import stop_repository_root
    from src.workflows.job_runtime import DurableJobLeaseError
    from src.work_board.repository import WorkBoardRepository
    factory, owner, service, jobs, binding, _ = await actual_native_source(
        accounting_db, monkeypatch, goal_capacity=2, claim_child=False, language=language)
    operator = await authenticate_session(owner.session_id, touch=False)
    prepared = await prepare_repository_native_source(service, jobs, binding,
        child_owner='actual-restart-original-worker', principal=operator.principal)
    root_id = prepared['repository_job_id']
    original_lane = service.repository_source_service._iterative_lanes[root_id]
    actual_event = WorkBoardRepository._event
    async def reject_terminal(*args, **kwargs):
        if kwargs['kind'] == 'attempt.repository_stopped':
            raise DurableJobLeaseError('actual restart fixture retains original stop transaction')
        return await actual_event(*args, **kwargs)
    with monkeypatch.context() as failure:
        failure.setattr(WorkBoardRepository, '_event', reject_terminal)
        pending = await stop_repository_root(service.repository_source_service, jobs,
            job_id=root_id, owner=owner, general_task_service=service)
        assert pending['pending'] is True
    assert original_lane.quarantined and original_lane._descriptor is not None
    before = await jobs.get_job(root_id)
    fresh = RepoRepairService(session_factory=factory)
    fresh.jobs = jobs
    assert fresh._iterative_lanes == {}
    service.repository_source_service = fresh
    if drift == 'unknown_task_field':
        async with factory() as db:
            task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == binding.task_id))
            task.title += ' changed outside immutable stop snapshot'
            await db.commit()
        def deny_private(*args, **kwargs):
            raise AssertionError('Changed original scope must block restart before private read')
        monkeypatch.setattr(fresh, '_read_private_artifact', deny_private)
        with pytest.raises(DurableJobLeaseError, match='snapshot changed before private read'):
            await stop_repository_root(fresh, jobs, job_id=root_id, owner=owner, general_task_service=service)
        assert (await jobs.get_job(root_id)) == before
        assert original_lane._descriptor is not None and original_lane.quarantined
        assert fresh._iterative_lanes == {}
    else:
        finished = await stop_repository_root(fresh, jobs, job_id=root_id, owner=owner, general_task_service=service)
        assert finished['pending'] is False
        assert original_lane._descriptor is None and root_id not in fresh._iterative_lanes
        async with factory() as db:
            run = await jobs._fetch(db, root_id)
            assert jobs._repo_repair_reservation_state(run)['status'] == 'released'
            assert run.status == 'cancelled' and run.fencing_token == before['lease']['fencing_token']
            assert list((await db.execute(select(InferenceCostReservation))).scalars()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize('ordinary_closure', ['positive', 'missing'])
async def test_actual_source_stop_preserves_prior_ordinary_closure_requirement(accounting_db, monkeypatch, ordinary_closure):
    from src.auth.service import authenticate_session
    from src.workflows.repo_repair_source import prepare_repository_native_source
    from src.workflows.repo_repair_stop import stop_repository_root
    from src.workflows.general_task_guard import child_binding, cleanup_checkpoint_id
    factory, owner, service, jobs, binding, _ = await actual_native_source(
        accounting_db, monkeypatch, goal_capacity=2, claim_child=False, ordinary_prior=True)
    operator = await authenticate_session(owner.session_id, touch=False)
    prepared = await prepare_repository_native_source(service, jobs, binding,
        child_owner='actual-source-after-ordinary', principal=operator.principal)
    root_id = prepared['repository_job_id']
    async with factory() as db:
        children = list((await db.execute(select(WorkflowRunState).where(
            WorkflowRunState.parent_job_id == binding.parent_job_id))).scalars())
        ordinary = next(row for row in children if row.run_identity != binding.invocation_id)
        assert json.loads(ordinary.arguments_json)['tool_id'] == 'fixture.read'
        assert ordinary.status == 'succeeded' and ordinary.attempt_count == 1
        parent = await jobs._fetch(db, binding.parent_job_id)
        cleanup_id = cleanup_checkpoint_id(child_binding(ordinary), ordinary.fencing_token)
        history = json.loads(parent.checkpoint_receipts_json)
        assert any(row['checkpoint_id'] == cleanup_id for row in history)
        if ordinary_closure == 'missing':
            # Negative corruption of a genuinely executed prior sibling; no
            # invented parallel grant or fabricated successful Source proof.
            parent.checkpoint_receipts_json = json.dumps([
                row for row in history if row['checkpoint_id'] != cleanup_id])
            await db.commit()
    result = await stop_repository_root(service.repository_source_service, jobs,
        job_id=root_id, owner=owner, general_task_service=service)
    assert result['pending'] is (ordinary_closure == 'missing')
    root = await jobs.get_job(root_id)
    parent = await jobs.get_job(binding.parent_job_id)
    if ordinary_closure == 'positive':
        assert root['status'] == parent['status'] == 'cancelled'
        assert root_id not in service.repository_source_service._iterative_lanes
    else:
        assert root['status'] == 'running' and parent['status'] == 'paused'
        assert root_id in service.repository_source_service._iterative_lanes
    assert (await jobs.get_job(ordinary.run_identity))['status'] == 'succeeded'


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['test_python', 'test_node'])
async def test_actual_source_unknown_model_cost_retains_original_capacity(accounting_db, monkeypatch, language):
    await _actual_source_callback_journey(accounting_db, monkeypatch, False, language,
        unknown_model_cost=True)


async def _actual_source_callback_journey(accounting_db, monkeypatch, three_iterations, language,
        node_readback_drift=None, node_build=False, stop_at=None, publication_profile=False,
        unknown_model_cost=False):
    import httpx
    from src.auth.service import authenticate_session
    from src.api.workflows import RepoRepairEgressConsentRequest
    from src.workflows.repo_repair_source import (prepare_repository_native_source,
        repository_source_preview, grant_repository_iteration_consent)
    factory, owner, service, jobs, binding, creation_request = await actual_native_source(
        accounting_db, monkeypatch, goal_capacity=2, claim_child=False, language=language, node_build=node_build,
        publication_profile=publication_profile)
    operator = await authenticate_session(owner.session_id, touch=False)
    prepared = await prepare_repository_native_source(service, jobs, binding,
        child_owner='actual-consent-callback-worker', principal=operator.principal)
    root_id = prepared['repository_job_id']
    preview = await repository_source_preview(service.repository_source_service, jobs,
        job_id=root_id, owner=owner)
    review, egress, packet = preview['repository_review'], preview['egress'], preview['source_packet']
    request = RepoRepairEgressConsentRequest(expected_job_revision=preview['revision'],
        source_packet_digest=packet['artifact_sha256'],
        expected_source_manifest_digest=packet['source_manifest_sha256'],
        expected_profile_id=egress['effective_profile_id'], acknowledged_selected_source=True,
        idempotency_key='actual-first-contact', expected_iteration_index=review['iteration_index'],
        expected_iteration_id=review['iteration_id'], expected_preparation_digest=review['preparation_digest'],
        expected_request_body_digest=egress['request_body_digest'],
        expected_request_route_digest=egress['request_route_digest'],
        expected_egress_envelope_digest=egress['egress_envelope_digest'],
        expected_diagnostics_digest=egress['diagnostics_digest'],
        expected_redaction_version=egress['redaction_version'], acknowledged_diagnostics=True)
    messages = egress['request_body']['messages']
    source = json.loads(messages[1]['content'])
    output = {'summary': 'Correct addition',
        'base_snapshot_sha256': source['source_packet']['base_snapshot_sha256'],
        'patch_unified_diff': '--- a/calculator.py\n+++ b/calculator.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n',
        'allowed_paths': ['calculator.py', 'tests/test_calculator.py'],
        'test_args': ['pytest', '-q', 'tests/test_calculator.py'],
        'expected_outcome': 'The addition check passes'}
    if three_iterations:
        output['patch_unified_diff'] = output['patch_unified_diff'].replace('+    return a + b', '+    return a - b + 0')
    if language == 'test_node':
        output.update(allowed_paths=['calculator.js', 'tests/calculator.test.js'], test_args=['npm', 'test'],
            patch_unified_diff='--- a/calculator.js\n+++ b/calculator.js\n@@ -1 +1 @@\n-exports.add = (a, b) => a - b;\n+exports.add = (a, b) => ' + ('a - b + 0' if three_iterations else 'a + b') + ';\n')
        if node_build:
            output['test_args'] = ['npm', 'run', 'build', 'test']
    if stop_at == 'active_process':
        output['patch_unified_diff'] = (
            '--- a/calculator.js\n+++ b/calculator.js\n@@ -1 +1 @@\n-exports.add = (a, b) => a - b;\n'
            '+exports.add = (a, b) => { while (true) {} };\n' if language == 'test_node' else
            '--- a/calculator.py\n+++ b/calculator.py\n@@ -1,2 +1,3 @@\n def add(a, b):\n-    return a - b\n'
            '+    while True:\n+        pass\n')
    contacted = []
    def final_http(request):
        assert str(request.url) == 'https://openrouter.ai/api/v1/chat/completions'
        assert json.loads(request.content) == egress['request_body']
        contacted.append(request)
        usage = {'prompt_tokens': 1, 'completion_tokens': 1}
        if not unknown_model_cost:
            usage['cost'] = '0'
        return httpx.Response(200, json={'id': 'scripted-source-final-transport',
            'usage': usage,
            'choices': [{'message': {'role': 'assistant', 'content': json.dumps(output)}}]})
    real_client = httpx.Client
    def owned_client(*args, **kwargs):
        return real_client(*args, **kwargs, transport=httpx.MockTransport(final_http))
    monkeypatch.setattr(httpx, 'Client', owned_client)
    before_child = await jobs.get_job(binding.invocation_id)
    from src.workflows.repo_repair import RepoRepairError
    with pytest.raises(RepoRepairError, match='preparation'):
        await grant_repository_iteration_consent(service.repository_source_service, jobs,
            job_id=root_id, owner=owner,
            request=request.model_copy(update={'expected_request_body_digest': 'f' * 64}),
            general_task_service=service, principal=operator.principal)
    assert contacted == []
    async with factory() as db:
        assert list((await db.scalars(select(InferenceCostReservation))).all()) == []
    assert (await jobs.get_job(binding.invocation_id))['lease'] == before_child['lease']
    if unknown_model_cost:
        with pytest.raises(Exception):
            await grant_repository_iteration_consent(service.repository_source_service, jobs,
                job_id=root_id, owner=owner, request=request, general_task_service=service,
                principal=operator.principal)
        assert len(contacted) == 1
        async with factory() as db:
            rows = list((await db.scalars(select(InferenceCostReservation))).all())
            assert len(rows) == 1 and rows[0].state == 'unknown'
            assert rows[0].contact_started_at is not None
            assert rows[0].actual_cost_microusd is None
            from src.workflows.general_task_accounting import reservation_liability
            assert rows[0].bound_microusd > 0
            assert reservation_liability(rows[0]) == rows[0].bound_microusd
            run = await jobs._fetch(db, root_id)
            held = jobs._repo_repair_reservation_state(run)
            assert held['status'] == 'held'
        assert (await jobs.get_job(root_id))['status'] == 'unknown_external_effect'
        from src.workflows.job_runtime import DurableJobLeaseError
        with pytest.raises((RepoRepairError, DurableJobLeaseError)):
            await grant_repository_iteration_consent(service.repository_source_service, jobs,
                job_id=root_id, owner=owner, request=request, general_task_service=service,
                principal=operator.principal)
        from src.workflows.repo_repair_stop import stop_repository_root
        stopped = await stop_repository_root(service.repository_source_service, jobs,
            job_id=root_id, owner=owner, general_task_service=service)
        assert stopped['pending'] is True
        assert root_id in service.repository_source_service._iterative_lanes
        async with factory() as db:
            run = await jobs._fetch(db, root_id)
            assert jobs._repo_repair_reservation_state(run)['status'] == 'held'
        assert len(contacted) == 1
        return
    result = await grant_repository_iteration_consent(service.repository_source_service, jobs,
        job_id=root_id, owner=owner, request=request, general_task_service=service,
        principal=operator.principal)
    assert result['repository_outcome']['awaiting_repository_wait'] is True
    assert len(contacted) == 1
    after_child = await jobs.get_job(binding.invocation_id)
    assert after_child['status'] == 'paused'
    for key in ('owner', 'fencing_token', 'expires_at'):
        assert after_child['lease'][key] == before_child['lease'][key]
    assert after_child['lease']['revision'] == before_child['lease']['revision'] + 1
    assert after_child['attempt_count'] == 1
    async with factory() as db:
        rows = list((await db.scalars(select(InferenceCostReservation))).all())
        assert len(rows) == 1 and rows[0].state == 'settled'
        assert rows[0].contact_started_at is not None and rows[0].actual_cost_microusd == 0
        assert rows[0].job_id == root_id
    if stop_at == 'contacted_wait':
        from src.workflows.repo_repair_stop import stop_repository_root
        stopped = await stop_repository_root(service.repository_source_service, jobs,
            job_id=root_id, owner=owner, general_task_service=service)
        assert stopped['pending'] is False
        assert len(contacted) == 1
        assert (await jobs.get_job(root_id))['status'] == 'cancelled'
        assert (await jobs.get_job(binding.invocation_id))['status'] == 'cancelled'
        assert (await jobs.get_job(binding.parent_job_id))['status'] == 'cancelled'
        assert root_id not in service.repository_source_service._iterative_lanes
        assert service.repository_source_service._iterative_process_callbacks == {}
        return
    from src.workflows.repo_repair_source import recover_repository_wait_witness
    restarted_source = RepoRepairService(session_factory=factory, jobs=jobs)
    from src.workflows.job_runtime import DurableJobLeaseError
    async with factory() as db:
        native_child = await jobs._fetch(db, binding.invocation_id)
        original_priority = native_child.priority
        native_child.priority += 1
        await db.commit()
    def forbidden_private_read(*args, **kwargs):
        raise AssertionError('Changed unknown child fields must deny before private snapshot read')
    with monkeypatch.context() as boundary:
        boundary.setattr(restarted_source, '_read_private_artifact', forbidden_private_read)
        with pytest.raises(DurableJobLeaseError, match='before private read'):
            await recover_repository_wait_witness(restarted_source, jobs,
                job_id=root_id, owner=owner, iteration_index=1)
    async with factory() as db:
        native_child = await jobs._fetch(db, binding.invocation_id)
        native_child.priority = original_priority
        await db.commit()
    from datetime import datetime, timezone
    async with factory() as db:
        session = await db.get(OperatorSession, owner.session_id)
        session.revoked_at = datetime.now(timezone.utc)
        await db.commit()
    with monkeypatch.context() as boundary:
        boundary.setattr(restarted_source, '_read_private_artifact', forbidden_private_read)
        with pytest.raises((DurableJobLeaseError, BoardError)):
            await recover_repository_wait_witness(restarted_source, jobs,
                job_id=root_id, owner=owner, iteration_index=1)
    async with factory() as db:
        session = await db.get(OperatorSession, owner.session_id)
        session.revoked_at = None
        await db.commit()
        from src.workflows.repo_repair_source import _repository_record
        original_run = await jobs._fetch(db, root_id)
        closure = _repository_record(original_run, 'repository:proposal:' + review['iteration_id'])
    snapshot = accounting_db[0] / closure['canonical_source_artifact_ref'].removeprefix('workspace-json:')
    snapshot_bytes = snapshot.read_bytes()
    snapshot.unlink()
    with pytest.raises(RepoRepairError):
        await recover_repository_wait_witness(restarted_source, jobs,
            job_id=root_id, owner=owner, iteration_index=1)
    snapshot.write_bytes(snapshot_bytes)
    snapshot.chmod(0o600)
    recovered = await recover_repository_wait_witness(restarted_source, jobs,
        job_id=root_id, owner=owner, iteration_index=1)
    parent = await jobs.get_job(binding.parent_job_id)
    with pytest.raises(DurableJobLeaseError):
        await jobs.resume_repository_child_wait(binding.invocation_id,
            owner=after_child['lease']['owner'], expected_parent_revision=parent['revision'],
            expected_child_revision=after_child['revision'], producer_witness=recovered.projection())
    resumed = await jobs.resume_repository_child_wait(binding.invocation_id,
        owner=after_child['lease']['owner'], expected_parent_revision=parent['revision'],
        expected_child_revision=after_child['revision'], producer_witness=recovered)
    assert resumed['child']['status'] == 'running'
    for key in ('owner', 'fencing_token', 'expires_at'):
        assert resumed['child']['lease'][key] == before_child['lease'][key]
    assert resumed['child']['attempt_count'] == 1
    assert len(contacted) == 1
    from src.api.workflows import RepoRepairResumeRequest
    from src.approval.repository import approval_repository, approval_decision_digest
    from src.workflows.repo_repair_source import execute_repository_iteration
    from src.db.models import RepoRepairProposal
    async with factory() as db:
        proposal = await db.get(RepoRepairProposal, 'repository-proposal:' + review['iteration_id'])
    current = await jobs.get_job(root_id)
    execution_request = RepoRepairResumeRequest(approval_id=proposal.approval_id,
        proposal_id=proposal.proposal_id, expected_proposal_revision=proposal.revision,
        expected_job_revision=current['revision'], idempotency_key='actual-approved-iteration')
    with pytest.raises(DurableJobLeaseError, match='manual patch approval'):
        await execute_repository_iteration(restarted_source, jobs, job_id=root_id,
            owner=owner, request=execution_request, principal=operator.principal)
    approval = await approval_repository.get(proposal.approval_id)
    approved = await approval_repository.resolve_exact(approval.id, 'approved',
        expected_digest=approval_decision_digest(approval), owner_principal_id=owner.principal_id,
        operator_session_id=owner.session_id)
    assert approved.status == 'approved'
    if node_readback_drift is not None:
        sandbox = service.repository_source_service.sandbox
        original_read = sandbox._read_private_output
        def changed_result(directory, name):
            raw = original_read(directory, name)
            if name == 'supervisor-result.json':
                result = json.loads(raw)
                if node_readback_drift == 'missing':
                    result.pop('tested_file_hash_metadata')
                elif node_readback_drift == 'tampered':
                    result['tested_file_hash_metadata'][0]['sha256'] = 'f' * 64
                elif node_readback_drift == 'body_hash':
                    result['diff_sha256'] = 'f' * 64
                else:
                    result['token'] = '0' * 64
                return json.dumps(result).encode()
            return raw
        monkeypatch.setattr(sandbox, '_read_private_output', changed_result)
        from src.execution.repo_sandbox import RepoSandboxError
        with pytest.raises(RepoSandboxError):
            await execute_repository_iteration(service.repository_source_service, jobs, job_id=root_id,
                owner=owner, request=execution_request, principal=operator.principal)
        assert (await jobs.get_job(root_id))['status'] == 'unknown_external_effect'
        assert (await jobs.get_job(binding.invocation_id))['status'] == 'running'
        assert root_id in service.repository_source_service._iterative_lanes
        assert len(contacted) == 1
        return
    if stop_at == 'active_process':
        import asyncio
        from src.workflows.repo_repair_stop import stop_repository_root
        source_owner = service.repository_source_service
        executing = asyncio.create_task(execute_repository_iteration(source_owner, jobs, job_id=root_id,
            owner=owner, request=execution_request, principal=operator.principal))
        for _ in range(1000):
            marker = source_owner.sandbox._read_job_marker(root_id)
            if marker and type(marker.get('pid')) is int and marker.get('pid_start_identity'):
                from pathlib import Path
                children = Path('/proc/' + str(marker['pid']) + '/task/' + str(marker['pid']) + '/children')
                if children.exists() and children.read_text().strip():
                    # Observe a real supervisor-owned child, beyond the parent
                    # Popen/handshake window. Startup ambiguity remains Unknown.
                    await asyncio.sleep(0.3)
                    break
            if executing.done():
                await executing
                raise AssertionError('Actual process did not reach owned supervisor')
            await asyncio.sleep(0.01)
        else:
            raise AssertionError('Actual original supervisor was never observed')
        stopped = await stop_repository_root(source_owner, jobs, job_id=root_id,
            owner=owner, general_task_service=service)
        actual = await asyncio.wait_for(executing, timeout=20)
        assert actual['cleanup_proven'] is True and actual['recovery_action'] == 'repository_stopped'
        assert (await jobs.get_job(root_id))['status'] == 'cancelled'
        assert (await jobs.get_job(binding.invocation_id))['status'] == 'cancelled'
        assert root_id not in source_owner._iterative_lanes
        assert len(contacted) == 1
        return
    executed = await execute_repository_iteration(service.repository_source_service, jobs, job_id=root_id,
        owner=owner, request=execution_request, principal=operator.principal)
    assert executed['status'] == ('failed' if three_iterations else 'succeeded') and executed['cleanup_proven'] is True
    if stop_at == 'failed_process':
        from src.workflows.repo_repair_stop import stop_repository_root
        stopped = await stop_repository_root(service.repository_source_service, jobs,
            job_id=root_id, owner=owner, general_task_service=service)
        assert stopped['pending'] is False
        assert len(contacted) == 1
        assert (await jobs.get_job(root_id))['status'] == 'cancelled'
        assert (await jobs.get_job(binding.invocation_id))['status'] == 'cancelled'
        assert (await jobs.get_job(binding.parent_job_id))['status'] == 'cancelled'
        assert root_id not in service.repository_source_service._iterative_lanes
        return
    if not three_iterations:
        assert executed['original_child_final']['child']['status'] == 'succeeded'
        assert (await jobs.get_job(root_id))['status'] == 'succeeded'
        assert root_id not in service.repository_source_service._iterative_lanes
        final_receipt = executed['original_child_final']['receipt']
        assert final_receipt['status'] == 'verified' and final_receipt['contact_state'] == 'settled'
        assert final_receipt['child_attempt_count'] == 1 and final_receipt['child_fence'] == before_child['lease']['fencing_token']
    original_path, original_bytes = (('calculator.js', 'exports.add = (a, b) => a - b;\n') if language == 'test_node'
        else ('calculator.py', 'def add(a, b):\n    return a - b\n'))
    assert (accounting_db[0] / 'example' / original_path).read_text() == original_bytes
    assert len(contacted) == 1
    if three_iterations:
        from src.workflows.repo_repair_source import prepare_repository_iteration
        for index in (2, 3):
            original_child = await jobs.get_job(binding.invocation_id)
            await prepare_repository_iteration(service.repository_source_service, jobs,
                job_id=root_id, owner=owner, iteration_index=index)
            preview = await repository_source_preview(service.repository_source_service, jobs,
                job_id=root_id, owner=owner)
            review, egress, packet = preview['repository_review'], preview['egress'], preview['source_packet']
            source_envelope = json.loads(egress['request_body']['messages'][1]['content'])
            prior = source_envelope['source_packet']['prior_tested_iteration']
            assert prior['hashes_are_file_bodies'] is False
            if node_build:
                assert [item['path'] for item in prior['tested_file_hash_metadata']] == [
                    'calculator.js', 'tests/calculator.test.js']
                assert prior['tested_file_hash_metadata_scope'] == {
                    'kind': 'original_acknowledged_source_paths_present_in_tested_tree',
                    'selected_source_paths': ['calculator.js', 'tests/calculator.test.js'],
                    'missing_selected_source_paths': [],
                    'full_metadata_retained_in_physical_readback': True}
                async with factory() as db:
                    root = await jobs._fetch(db, root_id)
                    from src.workflows.repo_repair_source import _repository_record
                    previous_readback = _repository_record(root, 'repository:readback:' + prior['iteration_id'])
                full_manifest = json.loads(service.repository_source_service._read_private_artifact(
                    previous_readback['artifact_ref'], expected_digest=previous_readback['artifact_digest']))
                assert full_manifest['after_digest'] == prior['tested_tree_digest']
                assert len(full_manifest['tested_file_hash_metadata']) > len(prior['tested_file_hash_metadata'])
                assert any(item['path'].startswith('node_modules/typescript/')
                    for item in full_manifest['tested_file_hash_metadata'])
            assert ('+exports.add = (a, b) => a - b' if language == 'test_node' else '+    return a - b') in prior['cumulative_diff']
            assert ('ERR_ASSERTION' if language == 'test_node' else 'FAILED') in egress['diagnostics']['stdout']
            next_expression = 'a - b + 1' if index == 2 else 'a - b + 2' if stop_at == 'iterations_exhausted' else 'a + b'
            output['patch_unified_diff'] = ('--- a/calculator.py\n+++ b/calculator.py\n@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return ' + next_expression + '\n')
            if language == 'test_node':
                output['patch_unified_diff'] = ('--- a/calculator.js\n+++ b/calculator.js\n@@ -1 +1 @@\n-exports.add = (a, b) => a - b;\n+exports.add = (a, b) => ' + next_expression + ';\n')
            request = request.model_copy(update={'expected_job_revision': preview['revision'],
                'source_packet_digest': packet['artifact_sha256'],
                'expected_source_manifest_digest': packet['source_manifest_sha256'],
                'expected_iteration_index': index, 'expected_iteration_id': review['iteration_id'],
                'expected_preparation_digest': review['preparation_digest'],
                'expected_request_body_digest': egress['request_body_digest'],
                'expected_request_route_digest': egress['request_route_digest'],
                'expected_egress_envelope_digest': egress['egress_envelope_digest'],
                'expected_diagnostics_digest': egress['diagnostics_digest'],
                'expected_redaction_version': egress['redaction_version'],
                'idempotency_key': 'actual-contact-' + str(index)})
            result = await grant_repository_iteration_consent(service.repository_source_service, jobs,
                job_id=root_id, owner=owner, request=request, general_task_service=service,
                principal=operator.principal)
            assert result['repository_outcome']['awaiting_repository_wait'] is True
            assert len(contacted) == index
            async with factory() as db:
                proposal = await db.get(RepoRepairProposal, 'repository-proposal:' + review['iteration_id'])
            approval = await approval_repository.get(proposal.approval_id)
            await approval_repository.resolve_exact(approval.id, 'approved',
                expected_digest=approval_decision_digest(approval), owner_principal_id=owner.principal_id,
                operator_session_id=owner.session_id)
            execution_request = execution_request.model_copy(update={
                'proposal_id': proposal.proposal_id, 'approval_id': proposal.approval_id,
                'expected_proposal_revision': proposal.revision,
                'expected_job_revision': (await jobs.get_job(root_id))['revision'],
                'idempotency_key': 'actual-process-' + str(index)})
            executed = await execute_repository_iteration(service.repository_source_service, jobs,
                job_id=root_id, owner=owner, request=execution_request, principal=operator.principal)
            assert executed['status'] == ('failed' if index == 2 or stop_at == 'iterations_exhausted' else 'succeeded')
            current_child = await jobs.get_job(binding.invocation_id)
            assert current_child['attempt_count'] == 1
            if index == 3 and stop_at == 'iterations_exhausted':
                assert executed['stop_pending'] is False
                assert executed['recovery_action'] == 'original_iterations_exhausted'
                assert (await jobs.get_job(root_id))['status'] == 'failed'
                assert current_child['status'] == 'cancelled'
                assert (await jobs.get_job(binding.parent_job_id))['status'] == 'cancelled'
                assert root_id not in service.repository_source_service._iterative_lanes
                assert len(contacted) == 3
                async with factory() as db:
                    rows = list((await db.scalars(select(InferenceCostReservation))).all())
                    assert len(rows) == 3 and all(row.state == 'settled' for row in rows)
                return
            if index == 2:
                for key in ('owner', 'fencing_token', 'expires_at'):
                    assert current_child['lease'][key] == original_child['lease'][key]
            else:
                assert executed['original_child_final']['child']['status'] == 'succeeded'
                assert current_child['lease']['fencing_token'] == original_child['lease']['fencing_token']
                assert current_child['lease']['owner'] is None and current_child['lease']['expires_at'] is None
        async with factory() as db:
            costs = list((await db.scalars(select(InferenceCostReservation))).all())
            assert len(costs) == 3 and all(row.state == 'settled' for row in costs)
            assert len({row.operation_id for row in costs}) == 3
        assert (accounting_db[0] / 'example' / original_path).read_text() == original_bytes
        assert (await jobs.get_job(root_id))['status'] == 'succeeded'
        assert root_id not in service.repository_source_service._iterative_lanes
    from src.workflows.repo_repair_source import repository_operator_projection
    with monkeypatch.context() as metadata_only:
        def forbid_private_metadata_read(*args, **kwargs):
            raise AssertionError('operator iteration metadata must not open private artifacts')
        metadata_only.setattr(service.repository_source_service, '_read_private_artifact', forbid_private_metadata_read)
        projected = await repository_operator_projection(service.repository_source_service, jobs,
            job_id=root_id, owner=owner)
    assert len(projected['iterations']) == (3 if three_iterations else 1)
    for item in projected['iterations']:
        assert set(item) == {'index', 'input_tree_digest', 'patch_digest', 'command_refs', 'result_artifacts'}
        assert len(item['command_refs']) == 1 and len(item['result_artifacts']) == 2
    for state in projected['iteration_states']:
        assert state['command_results_status'] == 'recorded'
        assert state['command_results'] == ([{'check': 'build', 'status': 'succeeded', 'exit_code': 0}]
            if node_build else []) + [{'check': 'test',
            'status': 'succeeded' if state['status'] == 'succeeded' else 'failed',
            'exit_code': 0 if state['status'] == 'succeeded' else 1}]
    from src.workflows.general_task_guard import read_manifest
    async with factory() as db:
        parent = await jobs._fetch(db, binding.parent_job_id)
        manifest = read_manifest(parent)
    resumed_parent = await jobs.resume_general_task_native_parent(binding.parent_job_id,
        owner='actual-original-parent-assembly', expected_revision=parent.revision,
        expected_manifest_revision=manifest.manifest_revision)
    assert resumed_parent['manifest']['phase'] == 'assembly'
    from src.work_board.dispatcher import WorkBoardDispatcher
    parent_runtime = resumed_parent['job']
    parent_outcome = await service.execute(jobs, job_id=binding.parent_job_id,
        owner=parent_runtime['lease']['owner'], fence=parent_runtime['lease']['fencing_token'],
        envelope=None, principal=operator.principal)
    assert parent_outcome['verified'] is True and parent_outcome['step_count'] == 1
    dispatcher = WorkBoardDispatcher(session_provider=factory.accounting_sessions, general_tasks=service)
    dispatcher.jobs = jobs
    await dispatcher._settle_parent(binding.parent_job_id, parent_runtime['lease']['owner'],
        parent_runtime['lease']['fencing_token'], parent_outcome)
    parent_terminal = await jobs.get_job(binding.parent_job_id)
    assert parent_terminal['status'] == 'succeeded'
    from src.work_board.contracts import WorkBoardStatus
    from src.db.models import WorkBoardAttempt
    async with factory() as db:
        parent_task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == binding.task_id))
        parent_attempt = await db.get(WorkBoardAttempt, binding.attempt_id)
    parent_proof = dispatcher._workflow_readback(parent_terminal, binding.parent_job_id)
    assert parent_proof is not None
    projected = await dispatcher._project(parent_task, parent_attempt,
        board_revision=parent_task.task_revision, status=WorkBoardStatus.done, outcome='verified',
        proof=parent_proof, result_refs=parent_outcome.get('result_refs'),
        artifact_refs=parent_outcome.get('artifact_refs'), lease_owner=parent_attempt.lease_owner)
    assert projected.task.status is WorkBoardStatus.done and projected.attempt.ended_at is not None
    assert len(contacted) == (3 if three_iterations else 1)
    async with factory() as db:
        current_proposal = await db.get(RepoRepairProposal, proposal.proposal_id)
        actual_proposal = current_proposal.model_dump(mode='json')
    return {'factory': factory, 'workspace': service.repository_source_service._workspace(),
        'repository': service.repository_source_service._workspace() / 'example', 'owner': owner,
        'service': service, 'jobs': jobs, 'root_id': root_id, 'proposal': actual_proposal,
        'creation_request': creation_request}


@pytest.mark.asyncio
@pytest.mark.parametrize('three_iterations', [False, True])
@pytest.mark.parametrize('language', ['test_python', 'test_node'])
async def test_actual_source_consent_callback_and_settled_c1_wait(accounting_db, monkeypatch, three_iterations, language):
    await _actual_source_callback_journey(accounting_db, monkeypatch, three_iterations, language)


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['test_python', 'test_node'])
async def test_actual_source_success_release_admits_explicit_successor(accounting_db, monkeypatch, language):
    from src.auth.service import authenticate_session
    from src.workflows.repo_repair_source import prepare_repository_native_source, read_repository_original
    finished = await _actual_source_callback_journey(accounting_db, monkeypatch, True, language)
    factory, jobs, service, owner = (finished[key] for key in ('factory', 'jobs', 'service', 'owner'))
    async with factory() as db:
        original = await jobs._fetch(db, finished['root_id'])
        original_scope = read_repository_original(original)[0]
        assert original.status == 'succeeded'
        assert jobs._repo_repair_reservation_state(original)['status'] == 'released'
    request = finished['creation_request'].model_copy(update={'idempotency_key': 'explicit-success-successor'})
    _, _, same_service, successor_jobs, binding, _ = await admit_existing_source_request(
        factory, owner, service, request, claim_child=False)
    assert same_service is service
    operator = await authenticate_session(owner.session_id, touch=False)
    successor = await prepare_repository_native_source(service, successor_jobs, binding,
        child_owner='actual-after-success-worker', principal=operator.principal)
    assert successor['repository_job_id'] != finished['root_id']
    async with factory() as db:
        prior = await successor_jobs._fetch(db, finished['root_id'])
        current = await successor_jobs._fetch(db, successor['repository_job_id'])
        assert read_repository_original(prior)[0] == original_scope
        assert successor_jobs._repo_repair_reservation_state(prior)['status'] == 'released'
        assert successor_jobs._repo_repair_reservation_state(current)['status'] == 'held'
    assert successor['repository_job_id'] in service.repository_source_service._iterative_lanes


@pytest.mark.asyncio
async def test_actual_node_source_three_iterations_build_and_test(accounting_db, monkeypatch):
    await _actual_source_callback_journey(accounting_db, monkeypatch, True, 'test_node', node_build=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['test_python', 'test_node'])
async def test_actual_source_stop_contacted_wait_settles_original(accounting_db, monkeypatch, language):
    await _actual_source_callback_journey(accounting_db, monkeypatch, False, language,
        stop_at='contacted_wait')


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['test_python', 'test_node'])
async def test_actual_source_stop_failed_process_full_readback(accounting_db, monkeypatch, language):
    await _actual_source_callback_journey(accounting_db, monkeypatch, True, language,
        stop_at='failed_process')


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['test_python', 'test_node'])
async def test_actual_source_stop_active_supervisor_keeps_original_until_reaped(accounting_db, monkeypatch, language):
    await _actual_source_callback_journey(accounting_db, monkeypatch, False, language,
        stop_at='active_process')


@pytest.mark.asyncio
@pytest.mark.parametrize('language', ['test_python', 'test_node'])
async def test_actual_source_three_failed_iterations_terminal_original(accounting_db, monkeypatch, language):
    await _actual_source_callback_journey(accounting_db, monkeypatch, True, language,
        stop_at='iterations_exhausted')


@pytest.mark.asyncio
@pytest.mark.parametrize('drift', ['missing', 'tampered', 'body_hash'])
async def test_actual_node_build_source_readback_drift_quarantines_original(accounting_db, monkeypatch, drift):
    await _actual_source_callback_journey(accounting_db, monkeypatch, False, 'test_node',
        node_readback_drift=drift, node_build=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('drift', ['missing', 'tampered', 'copied'])
async def test_actual_node_source_readback_drift_quarantines_original(accounting_db, monkeypatch, drift):
    await _actual_source_callback_journey(accounting_db, monkeypatch, False, 'test_node', node_readback_drift=drift)


@pytest.mark.asyncio
@pytest.mark.parametrize('goal_capacity', [None, 1, 2])
async def test_actual_original_repository_admission_mints_protected_source(accounting_db, monkeypatch, goal_capacity):
    factory, owner, service, jobs, binding, request = await actual_native_source(accounting_db, monkeypatch,
        goal_capacity=goal_capacity)
    from datetime import datetime, timedelta, timezone
    from src.work_board.contracts import WorkBoardInputArtifactCreate, WorkBoardTaskCreate
    from src.work_board.input_artifacts import prepare_input_artifact
    from src.workflows.job_runtime import (DurableJobSpec, DurableJobIdentity, _digest,
        DurableJobAdmissionDenied, DurableJobLeaseError)
    from src.workflows.repo_repair_source import (prepare_repository_original_admission,
        read_repository_original, stage_repository_canonical_source)
    async with factory.accounting_sessions() as db:
        artifact = await prepare_input_artifact(db, owner, WorkBoardInputArtifactCreate(
            schema_version=1, capability_id='engineering.repo-repair.v1', goal_id='goal:fixture', goal_revision=1,
            input=request.plan.steps[0].input, idempotency_key='original-repo-artifact'))
        created = await service.repository.create_task(db, owner, WorkBoardTaskCreate(
            title='Original bounded repair', goal_id='goal:fixture', goal_revision=1,
            capability_id='engineering.repo-repair.v1', input_artifact_id=artifact.artifact_id,
            idempotency_scope='original-repository-child', idempotency_key=binding.invocation_id,
            status='todo', requires_review=False))
    async with factory.accounting_sessions() as db:
        ready = await service.repository.promote_task_ready(db, created.task.task_id,
            expected_revision=created.task.task_revision,
            actor_principal_id='actual-repo-runner', actor_session_id='actual-repo-runner-session')
    async with factory.accounting_sessions() as db:
        claimed = await service.repository.claim_ready_task(db, created.task.task_id,
            expected_revision=ready.task.task_revision, lease_owner='actual-repo-runner', lease_seconds=900)
    async with factory() as db:
        check, scope = await prepare_repository_original_admission(service.repository_source_service, db,
            native_invocation_id=binding.invocation_id, repository_task_id=created.task.task_id,
            repository_attempt_id=claimed.attempt.attempt_id)
        stale_check, stale_scope = await prepare_repository_original_admission(service.repository_source_service, db,
            native_invocation_id=binding.invocation_id, repository_task_id=created.task.task_id,
            repository_attempt_id=claimed.attempt.attempt_id)
    run_id = 'repo-repair-actual-original-source'
    inputs = {'schema_version': 1, 'capability_id': 'engineering.repo-repair.v1',
        'input': request.plan.steps[0].input}
    authority = {'principal': owner.principal_id, 'owner_kind': 'user', 'session_id': owner.session_id,
        'capability_id': 'engineering.repo-repair.v1'}
    cutoff = min(binding.native_deadline_at, binding.original_deadline_at,
        datetime.now(timezone.utc) + timedelta(seconds=800))
    spec = DurableJobSpec(identity=DurableJobIdentity(run_id, 'user', owner.principal_id,
        'engineering.repo-repair.v1', '1', 'original-repository-child', binding.invocation_id),
        inputs=inputs, session_id=owner.session_id, operator_session_id=owner.session_id,
        goal_id='goal:fixture', goal_revision=1, deadline_at=cutoff,
        resource_claims=('repo-repair-execution',), declared_authority=authority,
        max_attempts=1, max_outstanding_jobs=goal_capacity or 1, run_fingerprint=_digest(inputs))
    if goal_capacity != 2:
        with pytest.raises(DurableJobAdmissionDenied, match='goal_budget_outstanding_limit'):
            async with scope():
                await jobs.admit_job(spec, admission_authority_check=check)
        async with factory() as db:
            assert await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == run_id)) is None
            assert list((await db.execute(select(InferenceCostReservation))).scalars()) == []
        return
    with pytest.raises(DurableJobLeaseError, match='source admission scope required'):
        await jobs.admit_job(spec, admission_authority_check=check)
    async with scope():
        admitted = await jobs.admit_job(spec, admission_authority_check=check)
    async with factory() as db:
        run = await jobs._fetch(db, run_id)
        # Admission is a real new row; execution/start and all iteration
        # witnesses are deliberately not asserted by this admission test.
        journal = json.loads(run.checkpoint_receipts_json)
        assert len(journal) == 2 and journal[0]['checkpoint_id'] == 'repository:original:v1'
        inventory = journal[1]['payload']['identities']
        assert journal[1]['checkpoint_id'] == 'repository:inventory:v1'
        assert len(inventory) == len(set(inventory)) <= 50
        assert sum(identity.startswith('repository:prepared:') for identity in inventory) == 3
        assert sum(identity.startswith('repository:callback-start:') for identity in inventory) == 3
        payload = journal[0]['payload']
        assert journal[0]['state_digest'] == _digest(payload)
        assert payload['native_binding'] == binding.model_dump(mode='json')
        assert payload['repository_task_id'] == created.task.task_id
        assert payload['repository_attempt_id'] == claimed.attempt.attempt_id
        assert payload['original_input'] == request.plan.steps[0].input
        assert payload['original_deadline_at'] == cutoff.isoformat()
        assert payload['group']['group_id']
        assert list((await db.execute(select(InferenceCostReservation))).scalars()) == []
    # A permanent mapping denies another new-only producer before physical
    # private reads, even if a stored terminal/corrupt mapping is presented.
    async def forbidden_physical(*args, **kwargs):
        raise AssertionError('mapping replay must not read private source')
    def forbidden_private(*args, **kwargs):
        raise AssertionError('stale source scope must not read private source')
    with monkeypatch.context() as blocked_reads:
        blocked_reads.setattr('src.workflows.general_task_guard.assert_general_task_child_current', forbidden_physical)
        blocked_reads.setattr(service.repository_source_service, '_read_private_artifact', forbidden_private)
        with pytest.raises(DurableJobLeaseError, match='permanent repository mapping'):
            async with stale_scope():
                await jobs.admit_job(spec, admission_authority_check=stale_check)
        for mapping_state in ('accepted', 'cancelled', 'unknown_external_effect'):
            async with factory.accounting_sessions() as db:
                run = await jobs._fetch(db, run_id)
                run.status = mapping_state
            async with factory() as db:
                with pytest.raises(DurableJobLeaseError, match='permanent repository mapping'):
                    await prepare_repository_original_admission(service.repository_source_service, db,
                        native_invocation_id=binding.invocation_id, repository_task_id=created.task.task_id,
                        repository_attempt_id=claimed.attempt.attempt_id)

    async with factory.accounting_sessions() as db:
        run = await jobs._fetch(db, run_id)
        run.status = 'accepted'

    # Actual original link/claim permits control-plane inspection, but a
    # missing owned egress consent cannot mint any accounting/source witness.
    async with factory.accounting_sessions() as db:
        await service.repository.link_attempt_workflow_run(db, created.task.task_id, claimed.attempt.attempt_id,
            workflow_run_id=run_id, expected_revision=claimed.task.task_revision,
            board_fence=claimed.attempt.fencing_token, lease_owner=claimed.attempt.lease_owner,
            workflow_projection=admitted,
            expected_identity={'job_id': run_id, 'owner_kind': spec.identity.owner_kind,
                'owner_principal_id': spec.identity.owner_principal_id, 'service_id': spec.service_id,
                'operator_session_id': spec.operator_session_id, 'session_id': spec.session_id,
                'goal_id': spec.goal_id, 'goal_revision': spec.goal_revision,
                'job_kind': spec.identity.job_kind, 'capability_version': spec.identity.capability_version,
                'input_digest': _digest(spec.inputs), 'authority_digest': _digest(spec.declared_authority),
                'run_fingerprint': spec.run_fingerprint, 'idempotency_scope': spec.identity.idempotency_scope,
                'idempotency_key': spec.identity.idempotency_key})
    await jobs.queue_job(run_id)
    await jobs.claim_job(run_id, owner=claimed.attempt.lease_owner, lease_seconds=900)
    from src.workflows.repo_repair import RepoRepairError
    source_service = service.repository_source_service
    source_service.session_factory = factory
    monkeypatch.setattr('src.workflows.repo_repair.durable_job_repository', jobs)
    async with factory() as db:
        current_run = await jobs._fetch(db, run_id)
        _original, _work, compiled, *_rest = read_repository_original(current_run)
        original_repo_lease = (current_run.lease_owner, current_run.fencing_token, current_run.lease_expires_at)
        current_child = await jobs._fetch(db, binding.invocation_id)
        original_child_lease = (current_child.lease_owner, current_child.fencing_token, current_child.lease_expires_at)
    packet = await source_service.inspect_and_prepare(compiled, owner=owner,
        work_board_task_id=created.task.task_id, work_board_attempt_id=claimed.attempt.attempt_id,
        workflow_run_id=run_id, goal_id=spec.goal_id, goal_revision=spec.goal_revision,
        input_digest=_digest(inputs))
    assert packet.state == 'verified'
    packet_bytes = source_service._read_private_artifact(packet.artifact_ref, expected_digest=packet.artifact_sha256)
    assert json.loads(packet_bytes)['workflow_run_id'] == run_id
    async with factory() as db:
        current_run = await jobs._fetch(db, run_id)
        current_child = await jobs._fetch(db, binding.invocation_id)
        assert (current_run.lease_owner, current_run.fencing_token, current_run.lease_expires_at) == original_repo_lease
        assert (current_child.lease_owner, current_child.fencing_token, current_child.lease_expires_at) == original_child_lease
        assert read_repository_original(current_run)[0]['native_binding'] == binding.model_dump(mode='json')
        with pytest.raises(RepoRepairError) as denied:
            await stage_repository_canonical_source(service.repository_source_service, db,
                repository_job_id=run_id, native_invocation_id=binding.invocation_id,
                consent_id='actual-missing-consent')
        assert denied.value.code == 'egress_consent_authority_invalid'
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
