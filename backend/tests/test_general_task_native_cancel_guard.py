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
from tests.general_task_method_lifecycle import native_admission_lifecycle
from tests.test_work_board_m6_provider_free_journey import isolated_runtime, OWNER, SESSION


@pytest.mark.asyncio
@pytest.mark.parametrize('expired', [False, True])
async def test_zero_claim_cancel_fences_without_invented_closure_or_clock(task_runtime, monkeypatch, expired, native_admission_lifecycle):
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
async def test_capacity_rejects_child_before_claim_or_callback(task_runtime, capacity, native_admission_lifecycle):
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
async def test_full_reserved_count_cannot_disable_cancel(task_runtime, native_admission_lifecycle):
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
async def test_cancel_denies_foreign_or_changed_original_scope_without_fences(task_runtime, drift, native_admission_lifecycle):
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
async def test_late_child_journal_change_rolls_back_entire_paired_cancel(task_runtime, monkeypatch, native_admission_lifecycle):
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


@pytest.mark.asyncio
@pytest.mark.parametrize('evidence', ['verified', 'verified_original_root', 'missing_output',
    'tampered_output', 'missing_readback', 'foreign_artifact', 'foreign_intent',
    'missing_source_witness', 'foreign_source_witness'])
async def test_original_readback_before_closure_cancel_is_observational(task_runtime, monkeypatch, evidence, native_admission_lifecycle):
    """Real filesystem callback/readback; cancellation wins closure publication."""
    import asyncio
    from config.settings import settings
    from src.auth.service import authenticate_session
    from src.native_tools.registry import ToolRegistry
    from src.tools.filesystem_tool import read_file
    from src.work_board.contracts import GeneralTaskCreate, GeneralTaskInput, PlanSpec
    from src.work_board.general_task import digest
    from src.work_board.general_task_native import run_native_step
    from src.work_board.repository import BoardError
    from src.workflows.job_runtime import DurableJobError

    workspace = task_runtime[1]
    (workspace / 'original-readback.txt').write_text('Original physical callback bytes')
    physical_reads = []
    original_read = read_file.forward
    def counted_read(file_path):
        physical_reads.append(file_path)
        return original_read(file_path)
    monkeypatch.setattr(read_file, 'forward', counted_read)
    registry = ToolRegistry(); registry.start()
    descriptors = registry.descriptors()
    descriptor = next(item for item in descriptors if item.tool_id == 'read_file')
    creation = GeneralTaskCreate(goal_revision=1, idempotency_key='original-readback-cancel',
        expected_plan_revision=1, input=GeneralTaskInput(goal_ref='goal-1', intent='Read original physical file',
            requested_output=descriptor.output_schema, tool_set_digest=digest([
                item.model_dump(mode='json') for item in descriptors])),
        plan=PlanSpec(revision=1, steps=[{'step_id': 'original', 'tool_id': descriptor.tool_id,
            'input': {'file_path': 'original-readback.txt'}, 'output_contract': descriptor.output_schema}]))
    entered, release = asyncio.Event(), asyncio.Event()
    worker = None
    try:
        sessions, dispatcher, service, envelope, current = await running_task(task_runtime,
            creation_request=creation, registry_override=registry)
        jobs = dispatcher.jobs
        binding, _ = await admit_native_step(jobs, current['job']['job_id'],
            owner=current['job']['lease']['owner'], fence=current['job']['lease']['fencing_token'],
            step=envelope.plan.steps[0], descriptor=descriptor, inputs=envelope.plan.steps[0].input, service=service)
        original_publish = jobs.publish_general_task_tool_closure
        async def after_actual_readback(child_id, **proof):
            entered.set()
            await release.wait()
            return await original_publish(child_id, **proof)
        monkeypatch.setattr(jobs, 'publish_general_task_tool_closure', after_actual_readback)
        operator = await authenticate_session(binding.original_root_id, touch=False)
        worker = asyncio.create_task(run_native_step(service, jobs, binding,
            child_owner='original-readback-native', principal=operator.principal))
        await asyncio.wait_for(entered.wait(), 10)
        handle = service._native_invocations[binding.invocation_id]
        assert handle.closed and handle.witness.outcome == 'returned'
        assert physical_reads == ['original-readback.txt']
        before_child = await jobs.get_job(binding.invocation_id)
        output_record = next(item for item in before_child['artifacts'] if item['artifact_type'] == 'general_task_step')
        output_path = workspace / output_record['file_path']
        assert output_path.read_bytes()
        assert any(item.get('effect_type') == 'general_tool_call' and item.get('status') == 'succeeded'
            and item.get('receipt_kind') == 'readback' for item in before_child['effects'])
        assert not any(item.get('payload', {}).get('schema_version') == 'general_task.tool_closure.v1'
            for item in (await jobs.get_job(binding.parent_job_id))['checkpoints'])
        if evidence == 'missing_output':
            output_path.unlink()
        elif evidence == 'tampered_output':
            output_path.write_bytes(b'{"step_id":"original","output":{"foreign":true}}')
        elif evidence in {'missing_readback', 'foreign_artifact', 'foreign_intent'}:
            async with sessions() as db:
                row = await jobs._fetch(db, binding.invocation_id)
                if evidence == 'foreign_artifact':
                    artifacts = json.loads(row.artifact_receipts_json)
                    next(item for item in artifacts if item.get('artifact_type') == 'general_task_step')['artifact_id'] = 'foreign-artifact'
                    row.artifact_receipts_json = json.dumps(artifacts)
                else:
                    effects = json.loads(row.effect_receipts_json)
                    if evidence == 'missing_readback':
                        effects = [item for item in effects if item.get('effect_type') != 'general_tool_call']
                    else:
                        next(item for item in effects if item.get('effect_type') == 'general_tool_call')['details']['original_intent_digest'] = '0' * 64
                    row.effect_receipts_json = json.dumps(effects)
                db.add(row)
        elif evidence == 'missing_source_witness':
            service._native_output_root_witnesses.pop(binding.invocation_id)
        elif evidence == 'foreign_source_witness':
            from dataclasses import replace
            witness = service._native_output_root_witnesses[binding.invocation_id]
            service._native_output_root_witnesses[binding.invocation_id] = replace(witness, root_path=str(workspace / 'foreign'))
        async with sessions() as db:
            manifest = read_manifest(await jobs._fetch(db, binding.parent_job_id))
        cancelled = await jobs.cancel_general_task_native_parent(binding.parent_job_id,
            operator_owner=WorkBoardOwner(principal_id=operator.principal.principal_id,
                session_id=operator.principal.operator_session_id), expected_task_revision=manifest.task_revision)
        assert cancelled['cancellation']['state'] == 'pending'
        fenced_child = await jobs.get_job(binding.invocation_id)
        fenced_parent = await jobs.get_job(binding.parent_job_id)
        if evidence == 'verified_original_root':
            moved = workspace / 'current-root-moved'; moved.mkdir()
            monkeypatch.setattr(settings, 'workspace_dir', str(moved))
        release.set()
        with pytest.raises((DurableJobError, BoardError)):
            await worker
        after_child = await jobs.get_job(binding.invocation_id)
        after_parent = await jobs.get_job(binding.parent_job_id)
        assert after_child == fenced_child  # no child success, lease/history/fence rewrite
        assert after_parent['attempt_count'] == fenced_parent['attempt_count']
        assert after_parent['deadline_at'] == fenced_parent['deadline_at']
        assert after_parent['lease']['fencing_token'] == fenced_parent['lease']['fencing_token']
        assert after_parent['artifacts'] == fenced_parent['artifacts']
        async with sessions() as db:
            parent = await jobs._fetch(db, binding.parent_job_id)
            task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == binding.task_id))
            attempt = await db.get(WorkBoardAttempt, binding.attempt_id)
            projection = read_general_task_native_cancel(parent, task, attempt)
            assert projection['state'] == ('fully_cancelled' if evidence.startswith('verified') else 'callback_closed_outcome_debt')
            assert projection['callback_closed']
            final_manifest = read_manifest(parent)
            for field in ('step_ids', 'step_receipt_artifact_ids', 'step_receipt_digests', 'step_receipt_schemas'):
                assert getattr(final_manifest, field) == getattr(manifest, field)
            assert bool(attempt.ended_at) == evidence.startswith('verified')
        assert physical_reads == ['original-readback.txt']
        with pytest.raises(DurableJobLeaseError, match='original general task native claim is exhausted'):
            await jobs.claim_job(binding.invocation_id, owner='forbidden-replay')
        assert await jobs.get_job(binding.invocation_id) == after_child
    finally:
        release.set()
        if worker is not None and not worker.done():
            try:
                await worker
            except (DurableJobError, BoardError):
                pass
        registry.stop()
