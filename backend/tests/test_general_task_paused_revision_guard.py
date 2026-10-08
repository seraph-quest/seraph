"""Original paused attempt revision: no execution lease or authority renewal."""
from datetime import datetime, timezone
import json

import pytest
from sqlalchemy import select, update

from src.db.models import Goal, OperatorSession, WorkBoardAttempt, WorkBoardTask, WorkflowRunState
from src.work_board.contracts import PlanRevisionRequest, WorkBoardOwner
from src.work_board.general_task_native import current_plan
from src.work_board.repository import BoardError
from src.workflows.general_task_guard import read_manifest
from src.workflows.job_runtime import DurableJobLeaseError, DurableJobTransitionError
from tests.test_general_task_native_guard import running_task
from tests.test_general_task_persistence import task_runtime
from tests.test_work_board_m6_provider_free_journey import isolated_runtime, OWNER, SESSION


async def paused_task(task_runtime):
    sessions, dispatcher, service, envelope, current = await running_task(task_runtime)
    manifest = current['manifest']
    paused = await dispatcher.jobs.pause_general_task_native_parent(current['job']['job_id'],
        operator_owner=WorkBoardOwner(principal_id=OWNER, session_id=SESSION),
        expected_task_revision=manifest['task_revision'], expected_revision=current['job']['revision'],
        expected_manifest_revision=manifest['manifest_revision'])
    return sessions, dispatcher.jobs, service, envelope, paused


def revision_request(envelope, paused, *, key='paused-edit'):
    step = envelope.plan.steps[0].model_copy(update={'input': {'text': 'revised while paused'}})
    return PlanRevisionRequest(expected_revision=paused['manifest']['task_revision'],
        replacements=[step], reason='Revise only work not admitted', idempotency_key=key)


@pytest.mark.asyncio
async def test_paused_overlay_keeps_original_input_attempt_fences_clocks_and_no_leases(task_runtime):
    sessions, jobs, service, envelope, paused = await paused_task(task_runtime)
    previous = paused['manifest']
    async with sessions() as db:
        original_attempt = await db.scalar(select(WorkBoardAttempt).where(
            WorkBoardAttempt.attempt_id == previous['attempt_id']))
        attempt_state = original_attempt.model_dump()
    result = await jobs.revise_general_task_operator_paused_parent(paused['job']['job_id'],
        operator_owner=WorkBoardOwner(principal_id=OWNER, session_id=SESSION),
        request=revision_request(envelope, paused), service=service)
    current = result['manifest']
    assert current['phase'] == 'operator_paused' and current['plan_revision'] == 2
    for field in ('original_root_id', 'owner_principal_id', 'attempt_id', 'run_id',
        'original_envelope_artifact_id', 'original_envelope_digest', 'original_input_digest',
        'selected_grant_digest', 'group_id', 'group_digest', 'original_limits_digest',
        'creation_digest', 'original_deadline_at', 'native_deadline_at', 'job_fence', 'board_fence',
        'admitted_invocation_ids', 'step_ids', 'step_receipt_artifact_ids', 'step_receipt_digests'):
        assert current[field] == previous[field]
    assert result['job']['status'] == 'paused'
    assert {key: value for key, value in result['job']['lease'].items() if key != 'revision'} == {
        key: value for key, value in paused['job']['lease'].items() if key != 'revision'}
    assert result['job']['attempt_count'] == paused['job']['attempt_count']
    assert result['job']['deadline_at'] == paused['job']['deadline_at']
    assert result['job']['revision'] == paused['job']['revision'] + 1
    async with sessions() as db:
        parent = await jobs._fetch(db, paused['job']['job_id'])
        assert current_plan(read_manifest(parent), envelope).steps[0].input == {'text': 'revised while paused'}
        attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == previous['attempt_id']))
        assert attempt.model_dump() == attempt_state
    repeated = revision_request(envelope, result)
    replay = await jobs.revise_general_task_operator_paused_parent(paused['job']['job_id'],
        operator_owner=WorkBoardOwner(principal_id=OWNER, session_id=SESSION), request=repeated, service=service)
    assert replay['idempotent_replay'] and replay['job']['revision'] == result['job']['revision']
    with pytest.raises(BoardError, match='different task data'):
        await jobs.revise_general_task_operator_paused_parent(paused['job']['job_id'],
            operator_owner=WorkBoardOwner(principal_id=OWNER, session_id=SESSION),
            request=repeated.model_copy(update={'reason': 'Conflicting same key'}), service=service)


@pytest.mark.asyncio
@pytest.mark.parametrize('drift', ['stale', 'goal', 'root', 'source', 'phase', 'cancel'])
async def test_paused_revision_drift_denies_before_private_staging(task_runtime, monkeypatch, drift):
    sessions, jobs, service, envelope, paused = await paused_task(task_runtime)
    manifest = paused['manifest']
    request = revision_request(envelope, paused)
    async with sessions() as db:
        if drift == 'stale':
            request = request.model_copy(update={'expected_revision': request.expected_revision - 1})
        elif drift == 'goal':
            await db.execute(update(Goal).where(Goal.id == 'goal-1').values(revision=2))
        elif drift == 'root':
            await db.execute(update(OperatorSession).where(OperatorSession.id == SESSION).values(
                revoked_at=datetime.now(timezone.utc)))
        elif drift == 'source':
            await db.execute(update(WorkBoardTask).where(WorkBoardTask.task_id == manifest['task_id']).values(
                typed_input_ref='workspace-json:foreign.json'))
        elif drift == 'phase':
            await db.execute(update(WorkflowRunState).where(WorkflowRunState.run_identity == manifest['run_id']).values(
                failure_reason='general_task_native_wait'))
        elif drift == 'cancel':
            await db.execute(update(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == manifest['attempt_id']).values(
                cancel_requested_at=datetime.now(timezone.utc)))
    staged = []
    def forbidden_stage(*args, **kwargs):
        staged.append(True)
        raise AssertionError('PAUSED_PRIVATE_STAGE_CANARY')
    monkeypatch.setattr('src.work_board.general_task_native.stage_task_artifact', forbidden_stage)
    before = await jobs.get_job(manifest['run_id'])
    with pytest.raises((DurableJobLeaseError, DurableJobTransitionError, BoardError)):
        await jobs.revise_general_task_operator_paused_parent(manifest['run_id'],
            operator_owner=WorkBoardOwner(principal_id=OWNER, session_id=SESSION), request=request, service=service)
    assert staged == []
    after = await jobs.get_job(manifest['run_id'])
    assert after['revision'] == before['revision'] and after['checkpoints'] == before['checkpoints']


@pytest.mark.asyncio
@pytest.mark.parametrize('late_drift', ['journal', 'input_owner'])
async def test_paused_revision_final_exact_cas_rolls_back_task_and_overlay(task_runtime, monkeypatch, late_drift):
    from src.db.models import WorkBoardInputArtifact
    from src.work_board import general_task_native as native
    sessions, jobs, service, envelope, paused = await paused_task(task_runtime)
    manifest = paused['manifest']
    compile_original = native.compile_paused_plan_revision
    async def drift_after_compilation(service, db, parent, task, attempt, envelope, previous, request):
        result = await compile_original(service, db, parent, task, attempt, envelope, previous, request)
        if late_drift == 'journal':
            history = json.loads(parent.checkpoint_receipts_json)
            history.append({'checkpoint_id': 'concurrent-private-journal', 'payload': {}})
            await db.execute(update(WorkflowRunState).where(WorkflowRunState.run_identity == parent.run_identity).values(
                checkpoint_receipts_json=json.dumps(history)).execution_options(synchronize_session=False))
        else:
            await db.execute(update(WorkBoardInputArtifact).where(WorkBoardInputArtifact.artifact_id == task.input_artifact_id).values(
                owner_principal_id='foreign-owner').execution_options(synchronize_session=False))
        return result
    monkeypatch.setattr(native, 'compile_paused_plan_revision', drift_after_compilation)
    before = await jobs.get_job(manifest['run_id'])
    with pytest.raises(DurableJobLeaseError, match='exact original journal CAS'):
        await jobs.revise_general_task_operator_paused_parent(manifest['run_id'],
            operator_owner=WorkBoardOwner(principal_id=OWNER, session_id=SESSION),
            request=revision_request(envelope, paused), service=service)
    after = await jobs.get_job(manifest['run_id'])
    assert after['revision'] == before['revision'] and after['checkpoints'] == before['checkpoints']
    assert after['artifacts'] == before['artifacts'] and after['lease'] == before['lease']
    async with sessions() as db:
        task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == manifest['task_id']))
        assert task.task_revision == manifest['task_revision']
        source = await db.get(WorkBoardInputArtifact, task.input_artifact_id)
        assert source.owner_principal_id == OWNER
