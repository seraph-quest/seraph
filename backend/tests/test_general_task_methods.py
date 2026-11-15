"""Actual canonical review -> native registered-tool consumer, no inference."""
import json
import pytest
import pytest_asyncio
from tests.general_task_method_lifecycle import AdmissionSignerLifetime
from sqlalchemy import select
from src.memory import task_methods as methods
from src.memory.task_lessons import create_task_lesson, LessonScope
from src.work_board.contracts import GeneralTaskCreate, GeneralTaskInput, PlanSpec, WorkBoardOwner
from src.work_board.general_task import GeneralTaskService, digest
from src.native_tools.registry import ToolRegistry
from src.work_board.dispatcher import WorkBoardDispatcher
from src.db.models import WorkBoardTask, WorkflowRunState, MemoryTombstone
from src.work_board.repository import BoardError
from tests.test_task_lessons import failed_local_task, no_inference
from tests.test_task_methods import review

pytestmark = pytest.mark.parametrize('async_db', ['file'], indirect=True)


@pytest_asyncio.fixture
async def method_admission_lifecycle():
    from src.work_board.historical_method import historical_method_service
    lifetime = AdmissionSignerLifetime(historical_method_service)
    try:
        yield lifetime
    finally:
        await lifetime.close()


async def setup_method(async_db, monkeypatch, tmp_path, *, correction=None):
    operator, source = await failed_local_task(async_db, monkeypatch, tmp_path)
    from src.auth.ownership import enroll
    await enroll(operator)
    source = source.model_copy(update={'scope': LessonScope(goal_id='goal', goal_revision=1, family='general'),
        **({'correction': correction} if correction else {})})
    proposed = await create_task_lesson(operator, source)
    await methods.review_method(operator, review(await methods.inspect_method(operator, proposed['proposal_id']), 'accept', 'actual-native-method'))
    current = methods.CurrentMethod()
    await current.start()
    monkeypatch.setattr(methods, 'current_method', current)
    owner = WorkBoardOwner(principal_id=operator.principal.principal_id, session_id=operator.session_id)
    registry = ToolRegistry()
    registry.start()
    service = GeneralTaskService(registry, strategy_resolver=methods.TaskMethodStrategyResolver(current))
    service.start()
    dispatcher = WorkBoardDispatcher(session_provider=async_db, general_tasks=service)
    return operator, current, owner, registry, service, dispatcher


def read_request(registry, key, path='selected.txt'):
    descriptors = registry.descriptors()
    descriptor = next(item for item in descriptors if item.tool_id == 'read_file')
    return GeneralTaskCreate(goal_revision=1, idempotency_key=key, expected_plan_revision=1, accept=True,
        input=GeneralTaskInput(goal_ref='goal', intent='Read the selected existing local source',
            requested_output=descriptor.output_schema, tool_set_digest=digest([item.model_dump(mode='json') for item in descriptors])),
        plan=PlanSpec(revision=1, steps=[{'step_id': 'read', 'tool_id': 'read_file', 'input': {'file_path': path}, 'output_contract': descriptor.output_schema}]))


@pytest.mark.asyncio
async def test_genuine_completed_native_source_review_next_task_and_future_rollback(async_db, monkeypatch, tmp_path, no_inference, method_admission_lifecycle):
    """No seeded Task, Attempt, run, StepState, receipt or source authority."""
    from config.settings import settings
    from src.auth.service import create_session
    from src.auth.ownership import enroll
    from src.db.models import Goal, Session, WorkBoardAttempt, WorkflowStepState
    from src.memory.task_lessons import LessonRequest, inspect_task_lesson
    workspace = tmp_path/'workspace'
    workspace.mkdir(mode=0o700)
    monkeypatch.setattr(settings, 'workspace_dir', str(workspace))
    monkeypatch.setattr(settings, 'operator_auth_secret', 'isolated-real-native-method')
    monkeypatch.setattr('src.memory.m5.get_session', async_db)
    _, operator = await create_session()
    await enroll(operator)
    async with async_db() as db:
        db.add(Session(id=operator.session_id))
        db.add(Goal(id='goal', title='Read the selected local source', revision=1, status='active',
            owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id))
        await db.commit()
    await method_admission_lifecycle.start()
    current = methods.CurrentMethod()
    await current.start()
    monkeypatch.setattr(methods, 'current_method', current)
    owner = WorkBoardOwner(principal_id=operator.principal.principal_id, session_id=operator.session_id)
    registry = ToolRegistry(); registry.start()
    service = GeneralTaskService(registry, strategy_resolver=methods.TaskMethodStrategyResolver(current)); service.start()
    dispatcher = WorkBoardDispatcher(session_provider=async_db, general_tasks=service)
    try:
        assert (await current.resolve(owner, 'goal', 'work.general-task.v1')).status == 'none'
        (workspace/'selected.txt').write_text('Genuine original native method source')
        async with async_db() as db:
            original = await service.create(db, owner, read_request(registry, 'genuine-baseline-source'))
        result = await dispatcher.run_pass()
        assert result['completed'] == 1, result
        async with async_db() as db:
            task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == original.task.task_id))
            attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == task.task_id))
            run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == attempt.workflow_run_id))
            assert task.status.value == 'review' and run.status == 'succeeded'
            assert list((await db.execute(select(WorkflowStepState).where(WorkflowStepState.run_identity == run.run_identity))).scalars()) == []
            source = LessonRequest(task_id=task.task_id, attempt_id=attempt.attempt_id,
                correction='Check source existence before reading.', source_refs=[run.run_identity, attempt.attempt_id],
                scope=LessonScope(goal_id='goal', goal_revision=1, family='general'), expected_revision=task.task_revision)
        # The ordinary named operator review owns Review -> Done, never the
        # lesson fixture or native executor.
        from src.work_board.review import complete_review
        async with async_db() as db:
            completed = await complete_review(db, owner, source.task_id,
                expected_revision=source.expected_revision, attempt_id=source.attempt_id)
            assert completed.task.status.value == 'done'
            source = source.model_copy(update={'expected_revision': completed.task.task_revision})
            await db.commit()
        from src.memory.task_lessons import eligible_lesson_source
        eligible = await eligible_lesson_source(operator, source.task_id)
        assert eligible['eligible'] is True and eligible['attempt_id'] == source.attempt_id, eligible
        source = source.model_copy(update={'source_refs': eligible['source_refs'],
            'expected_revision': eligible['expected_revision']})
        proposed = await create_task_lesson(operator, source)
        literal = await inspect_task_lesson(operator, proposed['proposal_id'])
        assert literal['old_method']['registered_tool_ids'] == ['read_file']
        assert literal['new_method']['steps'] == [{'kind': 'guard', 'check': 'source_exists'},
            {'kind': 'registered_tool', 'tool_id': 'read_file'}]
        await methods.review_method(operator, review(await methods.inspect_method(operator, proposed['proposal_id']), 'accept', 'genuine-native-review'))
        pin = await current.resolve(owner, 'goal', 'work.general-task.v1')
        assert pin.status == 'active' and pin.method_id == proposed['proposal_id']
        (workspace/'selected.txt').write_text('NEXT genuine reviewed method physical output')
        async with async_db() as db:
            selected = await service.create(db, owner, read_request(registry, 'genuine-next-native'))
        original_claim = dispatcher.jobs.claim_job
        rolled_back = []
        async def claim_and_rollback(job_id, **kwargs):
            claimed = await original_claim(job_id, **kwargs)
            if claimed['job_kind'] == 'agent.task.v1' and not rolled_back:
                await methods.review_method(operator, review(await methods.inspect_method(operator, pin.method_id), 'rollback', 'genuine-future-baseline'))
                rolled_back.append(job_id)
            return claimed
        monkeypatch.setattr(dispatcher.jobs, 'claim_job', claim_and_rollback)
        result = await dispatcher.run_pass()
        assert result['completed'] == 1 and len(rolled_back) == 1, result
        async with async_db() as db:
            projection = await service.plan(db, owner, selected.task.task_id)
            assert projection['strategy']['version'] == pin.version and projection['strategy']['digest'] == pin.digest
            runs = list((await db.execute(select(WorkflowRunState).where(WorkflowRunState.job_kind == 'agent.task.v1'))).scalars())
            assert len(runs) == 2 and all(run.status == 'succeeded' for run in runs)
            selected_attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == selected.task.task_id))
            selected_run = next(run for run in runs if run.run_identity == selected_attempt.workflow_run_id)
            refs = json.loads(selected_run.artifact_receipts_json)
            physical = [json.loads((workspace/ref['file_path']).read_text()) for ref in refs]
            assert any('NEXT genuine reviewed method physical output' in json.dumps(value) for value in physical)
        assert len(list(workspace.glob('artifacts/work-board/general-tasks/*.json'))) >= 2
        assert (await current.resolve(owner, 'goal', 'work.general-task.v1')).status == 'none'
        async with async_db() as db:
            future = await service.create(db, owner, read_request(registry, 'genuine-future-baseline'))
            assert (await service.plan(db, owner, future.task.task_id))['strategy']['status'] == 'none'
    finally:
        await current.stop(); service.stop(); registry.stop()


@pytest.mark.asyncio
async def test_actual_next_native_task_uses_guard_pin_and_rollback_after_admission(async_db, monkeypatch, tmp_path, no_inference, method_admission_lifecycle):
    operator, current, owner, registry, service, dispatcher = await setup_method(async_db, monkeypatch, tmp_path)
    await method_admission_lifecycle.start()
    workspace = tmp_path/'workspace'
    (workspace/'selected.txt').write_text('Original physical selected content')
    async with async_db() as db:
        created = await service.create(db, owner, read_request(registry, 'native-selected'))
    pinned = await current.resolve(owner, 'goal', 'work.general-task.v1')
    original_claim = dispatcher.jobs.claim_job
    rolled_back = []
    async def claim_then_rollback(job_id, **kwargs):
        result = await original_claim(job_id, **kwargs)
        if result['job_kind'] == 'agent.task.v1' and not rolled_back:
            await methods.review_method(operator, review(await methods.inspect_method(operator, pinned.method_id), 'rollback', 'future-only-baseline'))
            rolled_back.append(job_id)
        return result
    monkeypatch.setattr(dispatcher.jobs, 'claim_job', claim_then_rollback)
    result = await dispatcher.run_pass()
    assert result['completed'] == 1, result
    assert len(rolled_back) == 1
    async with async_db() as db:
        task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == created.task.task_id))
        assert task.status.value == 'review'  # original explicit operator review remains
        projection = await service.plan(db, owner, task.task_id)
        assert projection['strategy']['version'] == pinned.version
        assert projection['strategy']['digest'] == pinned.digest
        parents = list((await db.execute(select(WorkflowRunState).where(WorkflowRunState.job_kind == 'agent.task.v1'))).scalars())
        assert len(parents) == 1 and parents[0].status == 'succeeded'
        assert 'Original physical selected content' in ''.join(path.read_text() for path in workspace.glob('artifacts/work-board/general-tasks/*.json'))
    assert (await current.resolve(owner, 'goal', 'work.general-task.v1')).status == 'none'
    async with async_db() as db:
        next_task = await service.create(db, owner, read_request(registry, 'after-rollback'))
        next_plan = await service.plan(db, owner, next_task.task.task_id)
        assert next_plan['strategy']['status'] == 'none'
    await current.stop(); service.stop(); registry.stop()


@pytest.mark.asyncio
async def test_actual_verified_readback_guard_consumes_original_native_artifacts(async_db, monkeypatch, tmp_path, no_inference, method_admission_lifecycle):
    _operator, current, owner, registry, service, dispatcher = await setup_method(async_db, monkeypatch, tmp_path,
        correction='Verify readback before completion.')
    await method_admission_lifecycle.start()
    (tmp_path/'workspace'/'selected.txt').write_text('Verified original file bytes')
    async with async_db() as db:
        await service.create(db, owner, read_request(registry, 'verified-readback-method'))
    result = await dispatcher.run_pass()
    assert result['completed'] == 1, result
    async with async_db() as db:
        parent = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.job_kind == 'agent.task.v1'))).scalar_one()
        assert parent.status == 'succeeded'
        effects = json.loads(parent.effect_receipts_json)
        assert any(effect.get('effect_type') == 'general_tool_call' and effect.get('status') == 'succeeded' for effect in effects)
        assert 'Verified original file bytes' in ''.join(path.read_text() for path in (tmp_path/'workspace').glob('artifacts/work-board/general-tasks/*.json'))
    await current.stop(); service.stop(); registry.stop()


@pytest.mark.asyncio
async def test_attribution_guard_rejects_unsupported_original_tool_output_without_injection(async_db, monkeypatch, tmp_path, no_inference):
    _operator, current, owner, registry, service, _dispatcher = await setup_method(async_db, monkeypatch, tmp_path,
        correction='Preserve source attribution.')
    async with async_db() as db:
        with pytest.raises(BoardError) as failure:
            await service.create(db, owner, read_request(registry, 'unsupported-attribution'))
        assert failure.value.code == 'general_task_method_guard_unsupported'
    assert not list((tmp_path/'workspace').glob('artifacts/work-board/general-tasks/*.json'))
    await current.stop(); service.stop(); registry.stop()


@pytest.mark.asyncio
async def test_exact_tool_sequence_rejects_operator_plan_drift(async_db, monkeypatch, tmp_path, no_inference):
    _operator, current, owner, registry, service, _dispatcher = await setup_method(async_db, monkeypatch, tmp_path)
    request = read_request(registry, 'wrong-sequence')
    request = request.model_copy(update={'plan': request.plan.model_copy(update={'steps': [request.plan.steps[0], request.plan.steps[0].model_copy(update={'step_id': 'read-again'})]})})
    async with async_db() as db:
        with pytest.raises(BoardError) as failure:
            await service.create(db, owner, request)
        assert failure.value.code == 'general_task_method_sequence_mismatch'
    await current.stop(); service.stop(); registry.stop()


@pytest.mark.asyncio
async def test_actual_root_revocation_after_parent_claim_denies_before_callback(async_db, monkeypatch, tmp_path, no_inference, method_admission_lifecycle):
    _operator, current, owner, registry, service, dispatcher = await setup_method(async_db, monkeypatch, tmp_path)
    await method_admission_lifecycle.start()
    (tmp_path/'workspace'/'selected.txt').write_text('Current original source')
    async with async_db() as db:
        await service.create(db, owner, read_request(registry, 'revoked-root-pin'))
    original = dispatcher.jobs.claim_job
    async def claim_then_revoke(job_id, **kwargs):
        result = await original(job_id, **kwargs)
        if result['job_kind'] == 'agent.task.v1':
            from src.auth.service import revoke_session
            await revoke_session(owner.session_id)
        return result
    monkeypatch.setattr(dispatcher.jobs, 'claim_job', claim_then_revoke)
    contacts = []
    invoke = registry.begin_invocation
    def observe(*args, **kwargs):
        contacts.append(args[0].tool_id)
        return invoke(*args, **kwargs)
    monkeypatch.setattr(registry, 'begin_invocation', observe)
    result = await dispatcher.run_pass()
    assert result['completed'] == 0 and contacts == [], result
    await current.stop(); service.stop(); registry.stop()


@pytest.mark.asyncio
async def test_late_tombstone_after_physical_readback_blocks_protected_step_cas(async_db, monkeypatch, tmp_path, no_inference, method_admission_lifecycle):
    _operator, current, owner, registry, service, dispatcher = await setup_method(async_db, monkeypatch, tmp_path)
    await method_admission_lifecycle.start()
    (tmp_path/'workspace'/'selected.txt').write_text('Actual readback before revocation')
    async with async_db() as db:
        await service.create(db, owner, read_request(registry, 'late-tombstone'))
    pinned = await current.resolve(owner, 'goal', 'work.general-task.v1')
    original = dispatcher.jobs.record_readback
    mutations = []
    async def readback_then_tombstone(*args, **kwargs):
        result = await original(*args, **kwargs)
        if kwargs.get('effect_type') == 'general_tool_call' and kwargs.get('effect_id') and not mutations:
            async with async_db() as db:
                db.add(MemoryTombstone(memory_id=pinned.version))
                await db.commit()
            mutations.append(args[0])
        return result
    monkeypatch.setattr(dispatcher.jobs, 'record_readback', readback_then_tombstone)
    result = await dispatcher.run_pass()
    assert result['completed'] == 0 and len(mutations) == 1, result
    child = await dispatcher.jobs.get_job(mutations[0])
    assert any(effect.get('status') == 'succeeded' for effect in child['effects'])
    async with async_db() as db:
        parent = (await db.execute(select(WorkflowRunState).where(WorkflowRunState.job_kind == 'agent.task.v1'))).scalar_one()
        history = json.loads(parent.checkpoint_receipts_json)
        assert not any(item.get('payload', {}).get('schema_version') == 'StepReceipt.v1' for item in history)
        assert parent.status != 'succeeded'
    await current.stop(); service.stop(); registry.stop()


@pytest.mark.asyncio
async def test_missing_source_guard_blocks_actual_native_task_before_callback(async_db, monkeypatch, tmp_path, no_inference, method_admission_lifecycle):
    _operator, current, owner, registry, service, dispatcher = await setup_method(async_db, monkeypatch, tmp_path)
    await method_admission_lifecycle.start()
    async with async_db() as db:
        created = await service.create(db, owner, read_request(registry, 'missing-source', 'absent.txt'))
    actual = registry.begin_invocation
    contacts = []
    def observe(*args, **kwargs):
        contacts.append(args[0].tool_id)
        return actual(*args, **kwargs)
    monkeypatch.setattr(registry, 'begin_invocation', observe)
    result = await dispatcher.run_pass()
    assert result['completed'] == 0, result
    assert contacts == []
    async with async_db() as db:
        task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == created.task.task_id))
        assert task.status.value != 'done'
        assert not list((await db.execute(select(WorkflowRunState).where(WorkflowRunState.job_kind == 'general_task_native_tool_v1'))).scalars())
    await current.stop(); service.stop(); registry.stop()


@pytest.mark.asyncio
async def test_actual_tombstone_after_parent_claim_denies_before_tool(async_db, monkeypatch, tmp_path, no_inference, method_admission_lifecycle):
    _operator, current, owner, registry, service, dispatcher = await setup_method(async_db, monkeypatch, tmp_path)
    await method_admission_lifecycle.start()
    (tmp_path/'workspace'/'selected.txt').write_text('Original selected source')
    async with async_db() as db:
        await service.create(db, owner, read_request(registry, 'revoked-pin'))
    pinned = await current.resolve(owner, 'goal', 'work.general-task.v1')
    claim = dispatcher.jobs.claim_job
    async def claim_then_tombstone(job_id, **kwargs):
        result = await claim(job_id, **kwargs)
        if result['job_kind'] == 'agent.task.v1':
            async with async_db() as db:
                db.add(MemoryTombstone(memory_id=pinned.version))
                await db.commit()
        return result
    monkeypatch.setattr(dispatcher.jobs, 'claim_job', claim_then_tombstone)
    contacts = []
    actual = registry.begin_invocation
    def observe(*args, **kwargs):
        contacts.append(args[0].tool_id)
        return actual(*args, **kwargs)
    monkeypatch.setattr(registry, 'begin_invocation', observe)
    result = await dispatcher.run_pass()
    assert result['completed'] == 0 and contacts == [], result
    assert (await current.resolve(owner, 'goal', 'work.general-task.v1')).status == 'blocked'
    await current.stop(); service.stop(); registry.stop()


@pytest.mark.asyncio
async def test_triage_promotion_requires_current_reviewed_method(async_db, monkeypatch, tmp_path, no_inference):
    operator, current, owner, registry, service, _dispatcher = await setup_method(async_db, monkeypatch, tmp_path)
    request = read_request(registry, 'inert-reviewed').model_copy(update={'accept': False})
    async with async_db() as db:
        created = await service.create(db, owner, request)
    pinned = await current.resolve(owner, 'goal', 'work.general-task.v1')
    await methods.review_method(operator, review(await methods.inspect_method(operator, pinned.method_id), 'rollback', 'stale-inert-review'))
    async with async_db() as db:
        with pytest.raises(BoardError) as failure:
            await service.validate_acceptance(db, owner, created.task.task_id, created.task.task_revision)
        assert failure.value.code == 'general_task_strategy_changed'
    await current.stop(); service.stop(); registry.stop()
