"""Stock lifecycle dispatch reaches original Source preparation before consent."""
import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import select

from config.settings import RepoSandboxSettings, settings
# Match managed startup's model registration before disposable schema creation.
from src.api import router as _managed_router
from src.auth.service import authenticate_session
from src.db.models import Goal, InferenceCostReservation, WorkBoardAttempt, WorkBoardTask, WorkBoardStatus
from src.execution.repo_sandbox import persist_repo_sandbox_settings
from src.goals.contracts import GoalAdmissionBudget
from src.native_tools.registry import ToolRegistry
from src.native_tools.task_adapters import verify_task_tool_capacity
from src.observer.manager import context_manager
from src.work_board.contracts import GeneralTaskCreate, GeneralTaskInput, PlanSpec, TaskLimits
from src.work_board.dispatcher import WorkBoardDispatcher
from src.work_board.general_task import current_task_service, digest
from src.workflows.general_task_guard import child_binding, assert_general_task_child_current, read_manifest
from src.workflows.repo_repair import RepoWorkInput
from src.workflows.repo_repair_source import (
    read_repository_original, repository_review_projection, repository_source_preview,
)
from tests.repository_admission_lifecycle import repository_admission_signer
from tests.test_general_task_planner import accounting_db, prepare, forbid_external_inference
from tests.test_repo_work_contracts import selection
from tests.test_repo_work_source import git


@pytest_asyncio.fixture
async def stock_current_method_lifetime(accounting_db, monkeypatch):
    from src.memory import task_methods as methods
    # Select the genuine owner using the existing method-test lifecycle seam.
    # Its original database wrapper stays on this fixture's canonical factory.
    current = methods.CurrentMethod()
    monkeypatch.setattr(methods, 'current_method', current)
    assert methods.database.get_session is accounting_db[2].accounting_sessions
    try:
        yield current
    finally:
        await current.stop()
        assert current._started is False and current._key is None


@pytest.mark.asyncio
async def test_stock_repository_work_dispatch_prepares_original_child_with_goal_capacity_two(
    accounting_db, monkeypatch, repository_admission_signer, stock_current_method_lifetime,
):
    jobs, owner = await prepare(accounting_db, monkeypatch)
    workspace, _, factory = accounting_db
    workspace.chmod(0o700)
    context = context_manager.get_context()
    monkeypatch.setattr(context, 'tool_policy_mode', 'full')
    monkeypatch.setattr(context, 'mcp_policy_mode', 'disabled')
    monkeypatch.setattr(settings, 'repo_sandbox', RepoSandboxSettings(
        enabled=True, executor_kind='local', profile='repo-python-pytest-v1'))
    persist_repo_sandbox_settings(settings.repo_sandbox)
    async with factory.accounting_sessions() as db:
        goal = await db.get(Goal, 'goal:fixture')
        assert (goal.owner_principal_id, goal.owner_session_id) == (owner.principal_id, owner.session_id)
        goal.admission_budget_json = GoalAdmissionBudget(
            max_outstanding_jobs=2, max_runtime_seconds=900).model_dump_json()
    repository = workspace / 'example'
    (repository / 'tests').mkdir(parents=True)
    (repository / 'calculator.py').write_text('def add(a, b):\n    return a - b\n')
    (repository / 'tests/test_calculator.py').write_text(
        'from calculator import add\ndef test_add():\n    assert add(1, 2) == 3\n')
    git(repository, 'init', '--template=', '--initial-branch=develop')
    git(repository, 'add', '.')
    git(repository, 'commit', '-m', 'stock admission source')
    work = RepoWorkInput.model_validate(selection(
        repository_ref='example', language_profile='test_python', requested_checks=['test'],
        allowed_paths=['calculator.py', 'tests/test_calculator.py'],
        base_commit=git(repository, 'rev-parse', 'HEAD').decode().strip()))
    # Original app order: current method before historical signer and Task service.
    # prepare() has already established the original private server auth key.
    await stock_current_method_lifetime.start()
    assert stock_current_method_lifetime._started
    assert stock_current_method_lifetime._key is not None
    await repository_admission_signer.start()
    dispatcher = WorkBoardDispatcher(jobs=jobs, session_provider=factory.accounting_sessions)
    operator = await authenticate_session(owner.session_id, touch=False)
    assert operator.principal.principal_id == owner.principal_id
    with current_task_service(dispatcher=dispatcher) as service:
        assert type(service.registry) is ToolRegistry
        assert dispatcher.general_tasks is service
        source = service.repository_source_service
        assert source.jobs is jobs and source.session_factory is dispatcher.session_provider
        assert service.repository_work_adapter.__self__ is source
        descriptor = next(item for item in service.registry.descriptors()
            if item.tool_id == 'repository_work')
        capacity = service.registry.compile_capacity(descriptor)
        assert capacity.approval_possible is True
        assert verify_task_tool_capacity(capacity,
            descriptor_digest=digest(descriptor.model_dump(mode='json')))[0] is True
        request = GeneralTaskCreate(goal_revision=1, idempotency_key='stock-precontact',
            accept=True, expected_plan_revision=1,
            input=GeneralTaskInput(goal_ref='goal:fixture', intent='Repair the selected repository',
                requested_output=descriptor.output_schema,
                limits=TaskLimits(max_inference_calls=5, max_cost_microusd=500, wall_seconds=900),
                tool_set_digest=digest([item.model_dump(mode='json') for item in
                    sorted(service.registry.descriptors(), key=lambda item: item.tool_id)])),
            plan=PlanSpec(revision=1, steps=[{'step_id': 'repair', 'tool_id': 'repository_work',
                'input': work.model_dump(mode='json'), 'output_contract': descriptor.output_schema}]))
        async with factory.accounting_sessions() as db:
            created = await service.create(db, owner, request)
            task_id = created.task.task_id
            assert created.task.owner_principal_id == owner.principal_id
            assert created.task.owner_session_id == owner.session_id
        # One real bounded scheduler pass owns promotion, claim, parent admission,
        # interpreter and original native Source first start; no fixture DML does it.
        receipt = await asyncio.wait_for(dispatcher.run_pass(), timeout=20)
        assert receipt['claimed'] == 1 and receipt['admitted'] == 1, receipt
        # The native owner has prepared Source and holds the parent for consent;
        # run_pass counts that original wait as blocked, not completed.
        assert receipt['blocked'] == 1 and receipt['completed'] == 0, receipt
        async with factory.accounting_sessions() as db:
            task = (await db.execute(select(WorkBoardTask).where(
                WorkBoardTask.task_id == task_id))).scalar_one()
            assert task.status is WorkBoardStatus.blocked
            assert task.block_kind == 'needs_input'
            assert task.block_reason == 'general_task_native_wait'
            attempts = (await db.scalars(select(WorkBoardAttempt).where(
                WorkBoardAttempt.task_id == task_id))).all()
            assert len(attempts) == 1
            attempt = attempts[0]
            assert attempt.fencing_token > 0 and attempt.workflow_run_id
            assert attempt.outcome == 'general_task_native_wait'
            parent = await jobs._fetch(db, attempt.workflow_run_id)
            assert parent.status == 'paused' and parent.failure_reason == 'general_task_native_wait'
            assert read_manifest(parent).phase == 'native_wait'
            review = await repository_review_projection(db, task=task, attempt=attempt, owner=owner)
            child = await jobs._fetch(db, review['native_child_id'])
            binding = child_binding(child)
            await assert_general_task_child_current(db, child)
            assert binding.task_id == task_id and binding.attempt_id == attempt.attempt_id
            assert binding.original_root_id == owner.session_id
            assert binding.input_digest == digest(work.model_dump(mode='json'))
            root = await jobs._fetch(db, review['repository_job_id'])
            assert root.status == 'running' and root.failure_reason is None
            assert child.status == 'running' and child.failure_reason is None
            repository_tasks = (await db.scalars(select(WorkBoardTask).where(
                WorkBoardTask.capability_id == 'engineering.repo-repair.v1'))).all()
            assert len(repository_tasks) == 1
            repository_task = repository_tasks[0]
            assert repository_task.status is WorkBoardStatus.running
            assert repository_task.block_kind is None and repository_task.block_reason is None
            repository_attempts = (await db.scalars(select(WorkBoardAttempt).where(
                WorkBoardAttempt.task_id == repository_task.task_id))).all()
            assert len(repository_attempts) == 1
            repository_attempt = repository_attempts[0]
            assert repository_attempt.outcome == 'pending_admission'
            assert repository_attempt.workflow_run_id == root.run_identity
            assert repository_attempt.fencing_token == root.fencing_token == 1
            original = read_repository_original(root)[0]
            assert original['native_binding'] == binding.model_dump(mode='json')
            assert root.goal_id == 'goal:fixture' and root.goal_revision == 1
            assert root.owner_principal_id == owner.principal_id
            assert root.operator_session_id == owner.session_id
            assert (await db.scalars(select(InferenceCostReservation))).all() == []
            assert len((await db.scalars(select(WorkBoardTask))).all()) == 2
        actual_child = await jobs.get_job(binding.invocation_id)
        assert actual_child['attempt_count'] == 1
        assert actual_child['lease']['fencing_token'] == 1
        preview = await repository_source_preview(source, jobs,
            job_id=review['repository_job_id'], owner=owner)
        assert preview['provider_contacted'] is False
        assert preview['egress']['diagnostics']['stdout'] == ''
        assert preview['egress']['diagnostics']['stderr'] == ''
        assert preview['egress']['combined_input_bytes'] <= 65536
        assert preview['repository_review']['native_child_id'] == binding.invocation_id
    assert dispatcher.general_tasks is None
