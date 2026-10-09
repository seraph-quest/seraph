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


async def actual_publication(accounting_db, monkeypatch, *, goal_capacity=None):
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
    source = RepoRepairService(session_factory=factory)
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


async def actual_native_source(accounting_db, monkeypatch, *, goal_capacity=None):
    factory, _, owner, service, request = await actual_publication(accounting_db, monkeypatch,
        goal_capacity=goal_capacity)
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
    binding, child = await admit_native_step(jobs, spec.identity.job_id,
        owner=parent['lease']['owner'], fence=parent['lease']['fencing_token'],
        step=envelope.plan.steps[0], descriptor=envelope.descriptors[0],
        inputs=envelope.plan.steps[0].input, service=service)
    child_id = binding.invocation_id
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
    return factory, owner, service, jobs, binding, request


@pytest.mark.asyncio
async def test_actual_source_task_original_native_child_binding(accounting_db, monkeypatch):
    await actual_native_source(accounting_db, monkeypatch)


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
        assert len(journal) == 1 and journal[0]['checkpoint_id'] == 'repository:original:v1'
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
