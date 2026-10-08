"""Fixed cancellation and pre-contact reserved capacity; original rows only."""
import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from src.db.models import WorkBoardAttempt, WorkBoardTask, WorkflowRunState
from src.work_board.contracts import WorkBoardOwner, GeneralTaskToolClosureV1
from src.work_board.general_task_native import admit_native_step
from src.workflows.general_task_guard import read_manifest, _current, read_general_task_native_cancel
from src.workflows.job_runtime import DurableJobLeaseError, DurableJobTransitionError, _utc_now
from tests.test_general_task_native_guard import running_task
from tests.test_general_task_persistence import task_runtime
from tests.test_work_board_m6_provider_free_journey import isolated_runtime, OWNER, SESSION


@pytest.mark.asyncio
@pytest.mark.parametrize('expired', [False, True])
async def test_zero_claim_cancel_fences_without_invented_closure_or_clock(task_runtime, monkeypatch, expired):
    sessions, dispatcher, service, envelope, original = await running_task(task_runtime)
    jobs = dispatcher.jobs
    step = envelope.plan.steps[0]
    descriptor = next(item for item in service.registry.descriptors() if item.tool_id == step.tool_id)
    binding, _ = await admit_native_step(jobs, original['job']['job_id'],
        owner=original['job']['lease']['owner'], fence=original['job']['lease']['fencing_token'],
        step=step, descriptor=descriptor, inputs=step.input)
    original_child = await jobs.get_job(binding.invocation_id)
    async with sessions() as db:
        parent = await jobs._fetch(db, binding.parent_job_id)
        manifest = read_manifest(parent)
        task_revision = manifest.task_revision
        attempt = await db.get(WorkBoardAttempt, binding.attempt_id)
        original_attempt = (attempt.started_at, attempt.heartbeat_at, attempt.parent_handoff_context_json)
        if expired:
            from src.workflows import job_runtime
            monkeypatch.setattr(job_runtime, '_utc_now', lambda: binding.native_deadline_at + timedelta(seconds=1))
            with pytest.raises(DurableJobLeaseError):
                await _current(jobs, db, binding.parent_job_id)
    cancelled = await jobs.cancel_general_task_native_parent(binding.parent_job_id,
        operator_owner=WorkBoardOwner(principal_id=OWNER, session_id=SESSION),
        expected_task_revision=task_revision)
    assert cancelled['cancellation']['state'] == 'fully_cancelled'
    assert cancelled['event'].kind == 'attempt.cancel_requested'
    assert cancelled['attempt'].attempt_id == binding.attempt_id and cancelled['attempt'].ended_at
    assert cancelled['attempt'].cancel_requested_at
    assert (cancelled['attempt'].started_at, cancelled['attempt'].heartbeat_at,
        cancelled['attempt'].parent_handoff_context_json) == original_attempt
    child = await jobs.get_job(binding.invocation_id)
    assert child['attempt_count'] == 0 and child['status'] == 'cancelled'
    assert child['deadline_at'] == original_child['deadline_at']
    assert not child['effects']
    assert not any(item.get('payload', {}).get('schema_version') == 'general_task.tool_closure.v1'
        for item in cancelled['job']['checkpoints'])
    rejected_claim = await jobs.claim_job(binding.invocation_id, owner='late-claim')
    assert rejected_claim['status'] == 'cancelled' and rejected_claim['attempt_count'] == 0
    assert rejected_claim['lease']['owner'] is None
    assert (await jobs.get_job(binding.invocation_id))['attempt_count'] == 0
    async with sessions() as db:
        parent = await jobs._fetch(db, binding.parent_job_id)
        task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == binding.task_id))
        attempt = await db.get(WorkBoardAttempt, binding.attempt_id)
        assert read_general_task_native_cancel(parent, task, attempt)['state'] == 'fully_cancelled'


@pytest.mark.asyncio
@pytest.mark.parametrize('capacity', ['count', 'bytes', 'witness'])
async def test_capacity_rejects_child_before_claim_or_callback(task_runtime, capacity):
    sessions, dispatcher, service, envelope, original = await running_task(task_runtime)
    jobs = dispatcher.jobs
    parent_id = original['job']['job_id']
    owner, fence = original['job']['lease']['owner'], original['job']['lease']['fencing_token']
    if capacity == 'count':
        for index in range(44):
            await jobs.record_checkpoint(parent_id, checkpoint_id='general:owned-existing:' + str(index),
                state={'retained': True}, checkpoint_payload={'retained': True}, owner=owner, fencing_token=fence)
    elif capacity == 'bytes':
        # Existing actual entries consume their full canonical bytes; not a small placeholder.
        async with sessions() as db:
            parent = await jobs._fetch(db, parent_id)
            history = json.loads(parent.checkpoint_receipts_json)
            history.append({'checkpoint_id': 'ordinary-existing-large', 'payload': {'value': 'x' * (4 * 1024 * 1024)}, 'safe': True})
            parent.checkpoint_receipts_json = json.dumps(history)
            db.add(parent)
    else:
        # Retained generic keys may be large even when their payloads are tiny.
        # Below 50 keys/4 MiB, their future cancellation metadata still cannot
        # fit the closed 64-KiB witness. Reject before creating any child.
        async with sessions() as db:
            parent = await jobs._fetch(db, parent_id)
            history = json.loads(parent.checkpoint_receipts_json)
            history.extend({'checkpoint_id': 'general:retained:' + str(index) + '\U0001f512' * 256,
                'payload': {'retained': True}, 'safe': True} for index in range(35))
            parent.checkpoint_receipts_json = json.dumps(history)
            db.add(parent)
    before = await jobs.get_job(parent_id)
    step = envelope.plan.steps[0]
    descriptor = next(item for item in service.registry.descriptors() if item.tool_id == step.tool_id)
    expected = 'future cancellation witness capacity' if capacity == 'witness' else 'capacity'
    with pytest.raises(DurableJobTransitionError, match=expected):
        await admit_native_step(jobs, parent_id, owner=owner, fence=fence,
            step=step, descriptor=descriptor, inputs=step.input)
    assert await jobs.get_job(parent_id) == before
    async with sessions() as db:
        assert not list((await db.execute(select(WorkflowRunState).where(
            WorkflowRunState.parent_job_id == parent_id))).scalars())


@pytest.mark.asyncio
async def test_full_reserved_count_cannot_disable_cancel(task_runtime):
    sessions, dispatcher, service, envelope, original = await running_task(task_runtime)
    jobs = dispatcher.jobs
    parent_id = original['job']['job_id']
    for index in range(43):
        await jobs.record_checkpoint(parent_id, checkpoint_id='general:retained:' + str(index),
            state={'retained': True}, checkpoint_payload={'retained': True},
            owner=original['job']['lease']['owner'], fencing_token=original['job']['lease']['fencing_token'])
    step = envelope.plan.steps[0]
    descriptor = next(item for item in service.registry.descriptors() if item.tool_id == step.tool_id)
    binding, _ = await admit_native_step(jobs, parent_id, owner=original['job']['lease']['owner'],
        fence=original['job']['lease']['fencing_token'], step=step, descriptor=descriptor, inputs=step.input)
    before = await jobs.get_job(parent_id)
    assert len(before['checkpoints']) == 50
    async with sessions() as db:
        manifest = read_manifest(await jobs._fetch(db, parent_id))
    cancelled = await jobs.cancel_general_task_native_parent(parent_id,
        operator_owner=WorkBoardOwner(principal_id=OWNER, session_id=SESSION), expected_task_revision=manifest.task_revision)
    assert cancelled['cancellation']['state'] == 'fully_cancelled'
    assert len(cancelled['job']['checkpoints']) == 50
    assert {item['checkpoint_id'] for item in before['checkpoints']} == {
        item['checkpoint_id'] for item in cancelled['job']['checkpoints']}


@pytest.mark.asyncio
@pytest.mark.parametrize('drift', ['owner', 'revision', 'input_ref', 'root_revoked', 'unclaimed_effect'])
async def test_cancel_denies_foreign_or_changed_original_scope_without_fences(task_runtime, drift):
    from src.db.models import OperatorSession
    from src.workflows.job_runtime import _canonical
    sessions, dispatcher, service, envelope, original = await running_task(task_runtime)
    jobs = dispatcher.jobs
    step = envelope.plan.steps[0]
    descriptor = next(item for item in service.registry.descriptors() if item.tool_id == step.tool_id)
    binding, _ = await admit_native_step(jobs, original['job']['job_id'], owner=original['job']['lease']['owner'],
        fence=original['job']['lease']['fencing_token'], step=step, descriptor=descriptor, inputs=step.input)
    async with sessions() as db:
        parent = await jobs._fetch(db, binding.parent_job_id)
        manifest = read_manifest(parent)
        if drift == 'input_ref':
            task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == binding.task_id))
            task.typed_input_ref = 'workspace-json:foreign.json'; db.add(task)
        elif drift == 'root_revoked':
            root = await db.get(OperatorSession, SESSION); root.revoked_at = _utc_now(); db.add(root)
        elif drift == 'unclaimed_effect':
            child = await jobs._fetch(db, binding.invocation_id)
            child.effect_receipts_json = _canonical([{'status': 'unknown'}]); db.add(child)
    parent_before = await jobs.get_job(binding.parent_job_id)
    child_before = await jobs.get_job(binding.invocation_id)
    with pytest.raises(DurableJobLeaseError):
        await jobs.cancel_general_task_native_parent(binding.parent_job_id,
            operator_owner=WorkBoardOwner(principal_id='foreign' if drift == 'owner' else OWNER, session_id=SESSION),
            expected_task_revision=manifest.task_revision - int(drift == 'revision'))
    assert await jobs.get_job(binding.parent_job_id) == parent_before
    assert await jobs.get_job(binding.invocation_id) == child_before
    async with sessions() as db:
        assert (await db.get(WorkBoardAttempt, binding.attempt_id)).cancel_requested_at is None


@pytest.mark.asyncio
async def test_late_child_journal_change_rolls_back_entire_paired_cancel(task_runtime, monkeypatch):
    from sqlalchemy import update
    from src.workflows import general_task_guard as guard
    sessions, dispatcher, service, envelope, original = await running_task(task_runtime)
    jobs = dispatcher.jobs
    step = envelope.plan.steps[0]
    descriptor = next(item for item in service.registry.descriptors() if item.tool_id == step.tool_id)
    binding, _ = await admit_native_step(jobs, original['job']['job_id'], owner=original['job']['lease']['owner'],
        fence=original['job']['lease']['fencing_token'], step=step, descriptor=descriptor, inputs=step.input)
    async with sessions() as db:
        manifest = read_manifest(await jobs._fetch(db, binding.parent_job_id))
    parent_before = await jobs.get_job(binding.parent_job_id)
    child_before = await jobs.get_job(binding.invocation_id)
    fixed = guard._cancel_cas_board
    async def late_same_writer_change(db, *args, **kwargs):
        await fixed(db, *args, **kwargs)
        await db.execute(update(WorkflowRunState).where(WorkflowRunState.run_identity == binding.invocation_id)
            .values(effect_receipts_json='[{"status":"unknown"}]').execution_options(synchronize_session=False))
    monkeypatch.setattr(guard, '_cancel_cas_board', late_same_writer_change)
    with pytest.raises(DurableJobLeaseError, match='journal CAS'):
        await jobs.cancel_general_task_native_parent(binding.parent_job_id,
            operator_owner=WorkBoardOwner(principal_id=OWNER, session_id=SESSION), expected_task_revision=manifest.task_revision)
    assert await jobs.get_job(binding.parent_job_id) == parent_before
    assert await jobs.get_job(binding.invocation_id) == child_before
    async with sessions() as db:
        attempt = await db.get(WorkBoardAttempt, binding.attempt_id)
        task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == binding.task_id))
        assert attempt.cancel_requested_at is None and attempt.ended_at is None
        assert task.task_revision == manifest.task_revision
