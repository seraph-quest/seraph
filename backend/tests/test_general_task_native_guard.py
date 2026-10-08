"""Real disposable SQLite native-child phase fences; no tool/provider contact."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from src.db.models import WorkBoardAttempt, WorkBoardTask, WorkflowRunState
from src.work_board.contracts import (
    GeneralTaskCurrentManifestV1, GeneralTaskNativeChildBindingV1,
    GeneralTaskToolInputV1, GeneralTaskStepReceiptV1, WorkBoardOwner,
)
from src.work_board.general_task import GeneralTaskService, digest
from src.work_board.general_task_runtime_artifacts import (
    compile_creation_digest, compile_phase_digest, selected_grant_digest, stage_task_artifact, initial_native_manifest,
)
from src.work_board.dispatcher import WorkBoardDispatcher
from src.work_board.pipelines import root_binding
from src.workflows.general_task_guard import assert_general_task_child_current, read_manifest
from src.workflows.job_runtime import (
    DurableJobIdentity, DurableJobSpec, DurableJobLeaseError,
    DurableJobTransitionError, _bounded_checkpoint_receipts, _digest,
)
from tests.test_general_task_contract import Registry, request
from tests.test_general_task_persistence import task_runtime
from tests.test_work_board_m6_provider_free_journey import isolated_runtime, OWNER, SESSION, _goal


async def running_task(task_runtime, *, creation_request=None, registry_override=None):
    sessions, _workspace = task_runtime
    registry = registry_override or Registry()
    service = GeneralTaskService(registry)
    service.start()
    dispatcher = WorkBoardDispatcher(session_provider=sessions, general_tasks=service)
    jobs = dispatcher.jobs
    async with sessions() as db:
        db.add(_goal("goal-1", "Native phase guard"))
    async with sessions() as db:
        created = await service.create(db, WorkBoardOwner(principal_id=OWNER, session_id=SESSION),
            (creation_request or request(registry)).model_copy(update={"accept": True}))
    async with sessions() as db:
        ready = await service.repository.promote_task_ready(db, created.task.task_id,
            expected_revision=created.task.task_revision,
            actor_principal_id=dispatcher.runner_id, actor_session_id=dispatcher.runner_session)
    async with sessions() as db:
        claimed = await service.repository.claim_ready_task(db, created.task.task_id,
            expected_revision=ready.task.task_revision, lease_owner=dispatcher.runner_id)
    task, attempt = claimed.task, claimed.attempt
    spec, inputs, *_rest = dispatcher._build_spec(task, attempt)
    admitted = await jobs.admit_job(spec)
    async with sessions() as db:
        linked = await service.repository.link_attempt_workflow_run(db, task.task_id, attempt.attempt_id,
            workflow_run_id=spec.identity.job_id, expected_revision=task.task_revision,
            board_fence=attempt.fencing_token, lease_owner=attempt.lease_owner,
            workflow_projection=admitted,
            expected_identity={"job_id": spec.identity.job_id, "owner_kind": spec.identity.owner_kind,
                "owner_principal_id": spec.identity.owner_principal_id, "service_id": spec.service_id,
                "operator_session_id": spec.operator_session_id, "session_id": spec.session_id,
                "goal_id": spec.goal_id, "goal_revision": spec.goal_revision,
                "job_kind": spec.identity.job_kind, "capability_version": spec.identity.capability_version,
                "input_digest": _digest(spec.inputs), "authority_digest": _digest(spec.declared_authority),
                "run_fingerprint": spec.run_fingerprint, "idempotency_scope": spec.identity.idempotency_scope,
                "idempotency_key": spec.identity.idempotency_key})
    task, attempt = linked.task, linked.attempt
    queued = await jobs.queue_job(spec.identity.job_id)
    parent_projection = await jobs.claim_job(spec.identity.job_id,
        owner=dispatcher.runner_id + ":" + attempt.attempt_id)
    from src.work_board.contracts import GeneralTaskEnvelope
    envelope = GeneralTaskEnvelope.model_validate(inputs)
    group = envelope.proposal_group
    async with sessions() as db:
        parent = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == spec.identity.job_id))
        manifest = initial_native_manifest(parent, task, attempt, envelope)
    result = await jobs.replace_general_task_manifest(parent.run_identity, manifest=manifest,
        owner=parent_projection["lease"]["owner"], fencing_token=1, expected_revision=parent_projection["revision"])
    return sessions, dispatcher, service, envelope, result


async def admitted_child(task_runtime, *, tamper_input=False):
    sessions, dispatcher, service, envelope, original = await running_task(task_runtime)
    jobs = dispatcher.jobs
    previous = GeneralTaskCurrentManifestV1.model_validate(original["manifest"])
    step = envelope.plan.steps[0]
    invocation = "native-child-1"
    next_manifest = previous.model_copy(update={"manifest_revision": 2, "phase_revision": 2,
        "phase": "native_wait", "task_revision": previous.task_revision + 1,
        "admitted_invocation_ids": [invocation]})
    next_manifest = next_manifest.model_copy(update={"phase_digest": compile_phase_digest(next_manifest)})
    descriptor_sha = digest(envelope.descriptors[0].model_dump(mode="json"))
    binding = GeneralTaskNativeChildBindingV1(parent_job_id=previous.run_id, task_id=previous.task_id,
        attempt_id=previous.attempt_id, original_root_id=SESSION, owner_principal_id=OWNER,
        goal_id="goal-1", goal_revision=1, original_deadline_at=previous.original_deadline_at,
        native_deadline_at=previous.native_deadline_at,
        original_envelope_digest=previous.original_envelope_digest, creation_digest=previous.creation_digest,
        parent_authority_digest=original["job"]["authority_digest"], creation_job_fence=1,
        creation_board_fence=1, plan_revision=1, plan_digest=previous.current_plan_digest,
        step_id=step.step_id, invocation_id=invocation, input_digest=digest(step.input),
        descriptor_digest=descriptor_sha, selected_grant_digest=previous.selected_grant_digest,
        phase_revision=2, phase_digest=next_manifest.phase_digest, live_root_digest=digest(root_binding()))
    staged = stage_task_artifact(parent_job_id=previous.run_id, creation_digest=previous.creation_digest,
        payload=GeneralTaskToolInputV1(parent_job_id=previous.run_id, creation_digest=previous.creation_digest,
            invocation_id=invocation, tool_id=step.tool_id, descriptor_digest=descriptor_sha,
            input_digest=binding.input_digest, inputs=step.input))
    spec = DurableJobSpec(identity=DurableJobIdentity(invocation, "user", OWNER,
        "general_task_native_tool_v1", "1", "general-native-tool", invocation),
        session_id=SESSION, operator_session_id=SESSION, parent_job_id=previous.run_id,
        parent_fencing_token=1, goal_id="goal-1", goal_revision=1, plan_revision=1,
        declared_authority={"principal": OWNER, "owner_kind": "user", "session_id": SESSION,
            "capability_id": "agent.native-tool-step.v1", "general_task_child_binding": binding.model_dump(mode="json")},
        inputs={"step_id": step.step_id, "tool_id": step.tool_id, "tool_input_digest": binding.input_digest,
            "descriptor_digest": descriptor_sha, "typed_input_ref": "general-task-input:" + staged.reference.artifact_id,
            "typed_input_digest": staged.reference.digest}, deadline_at=previous.native_deadline_at)
    if tamper_input:
        spec.inputs['unapproved_private_body'] = 'PRIVATE_STAGING_INJECTION_CANARY'
    child = await jobs.admit_general_task_tool_child(spec, manifest=next_manifest,
        owner=original["job"]["lease"]["owner"], fencing_token=1,
        expected_revision=original["job"]["revision"], staged_input=staged)
    return sessions, jobs, binding, child


@pytest.mark.asyncio
async def test_admitted_zero_claim_positive_and_missing_receipt_never_contacts(task_runtime):
    sessions, jobs, binding, admitted = await admitted_child(task_runtime)
    assert admitted["attempt_count"] == 0 and admitted["lease"]["fencing_token"] == 0
    parent = await jobs.get_job(binding.parent_job_id)
    assert parent["status"] == "paused" and parent["lease"]["owner"] is None
    await jobs.queue_job(binding.invocation_id)
    claimed = await jobs.claim_job(binding.invocation_id, owner="native-worker")
    assert claimed["attempt_count"] == 1 and claimed["lease"]["fencing_token"] == 1
    async with sessions() as db:
        child = await jobs._fetch(db, binding.invocation_id)
        with pytest.raises(DurableJobLeaseError, match="positive original native claim"):
            await assert_general_task_child_current(db, child)
    receipt = GeneralTaskStepReceiptV1(step_id=binding.step_id, plan_revision=1,
        invocation_id=binding.invocation_id, input_digest=binding.input_digest, contact_state="not_contacted",
        status="running", descriptor_digest=binding.descriptor_digest,
        selected_grant_digest=binding.selected_grant_digest, task_id=binding.task_id,
        attempt_id=binding.attempt_id, child_job_id=binding.invocation_id, child_attempt_count=1, child_fence=1,
        parent_creation_digest=binding.creation_digest, phase_digest=binding.phase_digest)
    staged = stage_task_artifact(parent_job_id=binding.parent_job_id, creation_digest=binding.creation_digest, payload=receipt)
    await jobs.publish_general_task_step_receipt(binding.parent_job_id, staged_artifact=staged,
        child_id=binding.invocation_id, owner="native-worker", fencing_token=1, expected_parent_revision=parent["revision"])
    async with sessions() as db:
        await assert_general_task_child_current(db, await jobs._fetch(db, binding.invocation_id))


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['deadline', 'authority', 'phase', 'root', 'cancel'])
async def test_original_native_claim_denies_canonical_drift(task_runtime, change):
    from src.db.models import OperatorSession
    from sqlalchemy import update
    sessions, jobs, binding, admitted = await admitted_child(task_runtime)
    await jobs.queue_job(binding.invocation_id)
    async with sessions() as db:
        if change == 'root':
            await db.execute(update(OperatorSession).where(OperatorSession.id == SESSION).values(revoked_at=datetime.now(timezone.utc)))
        elif change == 'cancel':
            await db.execute(update(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == binding.attempt_id).values(cancel_requested_at=datetime.now(timezone.utc)))
        else:
            values = {'deadline': {'deadline_at': binding.native_deadline_at + timedelta(seconds=1)},
                      'authority': {'authority_digest': 'f'*64},
                      'phase': {'failure_reason': 'operator_changed_phase'}}[change]
            await db.execute(update(WorkflowRunState).where(WorkflowRunState.run_identity == binding.parent_job_id).values(**values))
    with pytest.raises((DurableJobLeaseError, DurableJobTransitionError)):
        await jobs.claim_job(binding.invocation_id, owner='native-worker')
    child = await jobs.get_job(binding.invocation_id)
    assert child['attempt_count'] == 0 and child['lease']['fencing_token'] == 0
    parent = await jobs.get_job(binding.parent_job_id)
    assert read_manifest(type('Row', (), {'checkpoint_receipts_json': json.dumps(parent['checkpoints'])})()).admitted_invocation_ids == [binding.invocation_id]


def test_protected_manifest_corruption_never_becomes_empty_history():
    from types import SimpleNamespace
    with pytest.raises(DurableJobTransitionError):
        read_manifest(SimpleNamespace(checkpoint_receipts_json=json.dumps([
            {'checkpoint_id': 'general-task:current-manifest:v1', 'safe': True, 'payload': {}}])))


@pytest.mark.asyncio
async def test_missing_claim_receipt_blocks_journal_and_private_artifact_mutation(task_runtime):
    sessions, jobs, binding, admitted = await admitted_child(task_runtime)
    await jobs.queue_job(binding.invocation_id)
    await jobs.claim_job(binding.invocation_id, owner='native-worker')
    with pytest.raises(DurableJobLeaseError, match='positive original native claim'):
        await jobs.record_effect(binding.invocation_id, effect_type='tool-contact', status='intent',
            owner='native-worker', fencing_token=1)
    with pytest.raises(DurableJobLeaseError, match='positive original native claim'):
        await jobs.record_artifact(binding.invocation_id, file_path='artifacts/blocked-private.txt',
            content='PRIVATE_ADOPTION_CANARY', owner='native-worker', fencing_token=1)
    with pytest.raises(DurableJobLeaseError, match='positive original native claim'):
        await jobs.record_checkpoint(binding.invocation_id, checkpoint_id='general:unproved',
            state={'private_input': 'PRIVATE_CHECKPOINT_CANARY'}, owner='native-worker', fencing_token=1)
    with pytest.raises(DurableJobLeaseError, match='positive original native claim'):
        await jobs.transition_job(binding.invocation_id, 'succeeded',
            owner='native-worker', fencing_token=1, result={'verified': True})
    child = await jobs.get_job(binding.invocation_id)
    assert child['effects'] == []
    assert all(item['artifact_type'] != 'workspace_file' for item in child['artifacts'])
    assert child['checkpoints'] == [] and child['status'] == 'running'
    with pytest.raises(DurableJobLeaseError, match='claim is exhausted'):
        await jobs.claim_job(binding.invocation_id, owner='native-worker', continue_existing_attempt=True)


@pytest.mark.asyncio
async def test_paired_operator_pause_fences_old_child_and_generic_resume(task_runtime):
    sessions, jobs, binding, admitted = await admitted_child(task_runtime)
    await jobs.queue_job(binding.invocation_id)
    parent = await jobs.get_job(binding.parent_job_id)
    manifest = read_manifest(type('Row', (), {'checkpoint_receipts_json': json.dumps(parent['checkpoints'])})())
    with pytest.raises(DurableJobLeaseError, match='close under native_wait'):
        await jobs.pause_general_task_native_parent(binding.parent_job_id,
            operator_owner=WorkBoardOwner(principal_id=OWNER, session_id=SESSION),
            expected_task_revision=manifest.task_revision, expected_revision=parent['revision'],
            expected_manifest_revision=manifest.manifest_revision)
    cancelled = await jobs.cancel_job(binding.invocation_id)
    assert cancelled['status'] == 'cancelled' and cancelled['attempt_count'] == 0
    paused = await jobs.pause_general_task_native_parent(binding.parent_job_id,
        operator_owner=WorkBoardOwner(principal_id=OWNER, session_id=SESSION),
        expected_task_revision=manifest.task_revision, expected_revision=parent['revision'],
        expected_manifest_revision=manifest.manifest_revision)
    assert paused['manifest']['phase'] == 'operator_paused'
    assert paused['manifest']['job_fence'] == manifest.job_fence + 1
    assert paused['manifest']['board_fence'] == manifest.board_fence + 1
    historical = await jobs.claim_job(binding.invocation_id, owner='native-worker')
    assert historical['status'] == 'cancelled' and historical['attempt_count'] == 0
    with pytest.raises(DurableJobTransitionError, match='paired manifest resume'):
        await jobs.queue_job(binding.parent_job_id)
    async with sessions() as db:
        task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == manifest.task_id))
        attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == manifest.attempt_id))
        assert task.block_reason == 'general_task_operator_paused'
        assert task.task_revision == paused['manifest']['task_revision']
        assert attempt.lease_owner is None
        assert attempt.fencing_token == paused['manifest']['board_fence']


@pytest.mark.asyncio
async def test_native_original_cutoff_is_narrower_and_not_renewed(task_runtime):
    sessions, dispatcher, service, envelope, original = await running_task(task_runtime)
    manifest = GeneralTaskCurrentManifestV1.model_validate(original['manifest'])
    assert manifest.native_deadline_at < manifest.original_deadline_at
    with pytest.raises(DurableJobTransitionError, match='paired manifest resume'):
        await dispatcher.jobs.queue_job(manifest.run_id)
    parent = await dispatcher.jobs.get_job(manifest.run_id)
    from src.workflows.job_runtime import _as_utc
    assert _as_utc(datetime.fromisoformat(parent['deadline_at'])) == manifest.native_deadline_at


@pytest.mark.asyncio
async def test_no_child_operator_resume_keeps_same_attempt_and_original_cutoff(task_runtime):
    sessions, dispatcher, service, envelope, original = await running_task(task_runtime)
    jobs = dispatcher.jobs
    previous = GeneralTaskCurrentManifestV1.model_validate(original['manifest'])
    paused = await jobs.pause_general_task_native_parent(previous.run_id,
        operator_owner=WorkBoardOwner(principal_id=OWNER, session_id=SESSION),
        expected_task_revision=previous.task_revision, expected_revision=original['job']['revision'],
        expected_manifest_revision=previous.manifest_revision)
    with pytest.raises(DurableJobLeaseError):
        await jobs.resume_general_task_native_parent(previous.run_id, owner=dispatcher.runner_id,
            expected_revision=original['job']['revision'], expected_manifest_revision=previous.manifest_revision)
    resumed = await jobs.resume_general_task_native_parent(previous.run_id, owner=dispatcher.runner_id,
        expected_revision=paused['job']['revision'], expected_manifest_revision=paused['manifest']['manifest_revision'])
    assert resumed['manifest']['phase'] == 'assembly'
    assert resumed['manifest']['attempt_id'] == previous.attempt_id
    assert resumed['job']['attempt_count'] == original['job']['attempt_count'] == 1
    current = GeneralTaskCurrentManifestV1.model_validate(resumed['manifest'])
    assert current.native_deadline_at == previous.native_deadline_at
    assert current.original_deadline_at == previous.original_deadline_at


@pytest.mark.asyncio
async def test_protected_checkpoint_capacity_and_missing_proof_fail_closed(task_runtime):
    from src.workflows.job_runtime import _digest
    sessions, dispatcher, service, envelope, original = await running_task(task_runtime)
    manifest = GeneralTaskCurrentManifestV1.model_validate(original['manifest'])
    proof_ids = ['general:proof-' + str(i) for i in range(49)]
    manifest = manifest.model_copy(update={'required_checkpoint_ids': proof_ids})
    special = {'checkpoint_id': 'general-task:current-manifest:v1', 'safe': True,
               'payload': manifest.model_dump(mode='json'), 'state_digest': _digest(manifest.model_dump(mode='json'))}
    history = [{'checkpoint_id': key, 'safe': True} for key in proof_ids] + [special]
    retained = _bounded_checkpoint_receipts(history + [{'checkpoint_id': 'ordinary-later'}], limit=50)
    assert {x['checkpoint_id'] for x in retained} == set(proof_ids) | {special['checkpoint_id']}
    with pytest.raises(DurableJobTransitionError, match='capacity'):
        _bounded_checkpoint_receipts(history, limit=49)
    with pytest.raises(DurableJobTransitionError, match='proof is missing'):
        _bounded_checkpoint_receipts(history[1:], limit=50)


@pytest.mark.asyncio
async def test_rejected_input_admission_keeps_parent_board_and_manifest_atomic(task_runtime):
    sessions, _workspace = task_runtime
    with pytest.raises(DurableJobLeaseError, match='private native tool input binding'):
        await admitted_child(task_runtime, tamper_input=True)
    async with sessions() as db:
        rows = list((await db.execute(select(WorkflowRunState))).scalars().all())
        assert len(rows) == 1
        parent = rows[0]
        manifest = read_manifest(parent)
        task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == manifest.task_id))
        attempt = await db.scalar(select(WorkBoardAttempt).where(WorkBoardAttempt.attempt_id == manifest.attempt_id))
        assert parent.status == 'running' and parent.lease_owner
        assert manifest.phase == 'native_ready' and manifest.admitted_invocation_ids == []
        assert manifest.manifest_revision == 1
        assert task.status.value == 'running' and task.task_revision == manifest.task_revision
        assert attempt.lease_owner and attempt.fencing_token == manifest.board_fence
        assert 'PRIVATE_STAGING_INJECTION_CANARY' not in parent.arguments_json
        assert 'PRIVATE_STAGING_INJECTION_CANARY' not in parent.checkpoint_receipts_json


@pytest.mark.asyncio
async def test_parent_publication_rechecks_original_authority_before_private_write(task_runtime):
    from sqlalchemy import update
    from src.work_board.repository import BoardError
    sessions, dispatcher, service, envelope, original = await running_task(task_runtime)
    manifest = GeneralTaskCurrentManifestV1.model_validate(original['manifest'])
    async with sessions() as db:
        await db.execute(update(WorkflowRunState).where(WorkflowRunState.run_identity == manifest.run_id)
            .values(authority_digest='f'*64))
    with pytest.raises((DurableJobLeaseError, BoardError)):
        await dispatcher.jobs.record_artifact(manifest.run_id, file_path='artifacts/forbidden-assembly.txt',
            content='PRIVATE_ASSEMBLY_CANARY', owner=original['job']['lease']['owner'], fencing_token=manifest.job_fence)
    parent = await dispatcher.jobs.get_job(manifest.run_id)
    assert all(item['artifact_type'] != 'workspace_file' for item in parent['artifacts'])
    assert 'PRIVATE_ASSEMBLY_CANARY' not in json.dumps(parent['checkpoints'])


@pytest.mark.asyncio
@pytest.mark.parametrize('contact_intent', [False, True])
async def test_claimed_child_cancel_without_original_callback_receipt_keeps_native_wait(task_runtime, contact_intent):
    from src.work_board.general_task_native import publish_positive_claim
    sessions, jobs, binding, admitted = await admitted_child(task_runtime)
    await jobs.queue_job(binding.invocation_id)
    claimed = await jobs.claim_job(binding.invocation_id, owner='native-worker')
    fence = claimed['lease']['fencing_token']
    await publish_positive_claim(jobs, binding, child_owner='native-worker', child_fence=fence)
    if contact_intent:
        await jobs.record_effect(binding.invocation_id, effect_type='general_tool_call',
            effect_id='general:' + binding.step_id + ':' + str(fence), status='intent',
            owner='native-worker', fencing_token=fence)
    original = await jobs.get_job(binding.parent_job_id)
    manifest = read_manifest(type('Row', (), {'checkpoint_receipts_json': json.dumps(original['checkpoints'])})())
    cancelled = await jobs.cancel_job(binding.invocation_id, owner='native-worker', fencing_token=fence)
    assert cancelled['attempt_count'] == 1
    assert cancelled['status'] == ('unknown_external_effect' if contact_intent else 'cancelled')
    with pytest.raises(DurableJobLeaseError, match='callback closure proof|close under native_wait'):
        await jobs.pause_general_task_native_parent(binding.parent_job_id,
            operator_owner=WorkBoardOwner(principal_id=OWNER, session_id=SESSION),
            expected_task_revision=manifest.task_revision, expected_revision=original['revision'],
            expected_manifest_revision=manifest.manifest_revision)
    parent = await jobs.get_job(binding.parent_job_id)
    current = read_manifest(type('Row', (), {'checkpoint_receipts_json': json.dumps(parent['checkpoints'])})())
    assert current == manifest
    assert parent['revision'] == original['revision']
    assert current.phase == 'native_wait'
    assert current.admitted_invocation_ids == [binding.invocation_id]


@pytest.mark.asyncio
@pytest.mark.parametrize('tamper_output', [False, True])
async def test_verified_terminal_receipt_never_reopens_contact_and_requires_physical_output(task_runtime, monkeypatch, tamper_output):
    from dataclasses import replace
    from src.auth.service import authenticate_session
    from src.work_board.general_task_native import admit_native_step, run_native_step
    from src.work_board.repository import BoardError
    from src.workspace import canonical_workspace_root
    from config.settings import settings
    sessions, dispatcher, service, envelope, original = await running_task(task_runtime)
    jobs = dispatcher.jobs
    binding, _admitted = await admit_native_step(jobs, original['job']['job_id'],
        owner=original['job']['lease']['owner'], fence=original['job']['lease']['fencing_token'],
        step=envelope.plan.steps[0], descriptor=envelope.descriptors[0], inputs=envelope.plan.steps[0].input)
    operator = await authenticate_session(binding.original_root_id, touch=False)
    principal = replace(operator.principal, session_id=binding.original_root_id,
        operator_session_id=binding.original_root_id, job_id=binding.invocation_id)
    real_transition = jobs.transition_job
    terminal = {}
    async def defer_terminal(job_id, status, **kwargs):
        if job_id == binding.invocation_id and status == 'succeeded':
            terminal.update(kwargs)
            return await jobs.get_job(job_id)
        return await real_transition(job_id, status, **kwargs)
    monkeypatch.setattr(jobs, 'transition_job', defer_terminal)
    output, artifact, _reference = await run_native_step(service, jobs, binding,
        child_owner='native-verified-owner', principal=principal)
    assert output == {'text': 'hello'} and len(service.registry.calls) == 1
    with pytest.raises(DurableJobLeaseError, match='positive child claim changed'):
        await jobs.record_effect(binding.invocation_id, effect_type='general_tool_call', status='intent',
            owner=terminal['owner'], fencing_token=terminal['fencing_token'])
    if tamper_output:
        (canonical_workspace_root(settings.workspace_dir) / artifact['file_path']).write_bytes(b'PRIVATE_CHANGED_OUTPUT')
        with pytest.raises((DurableJobLeaseError, BoardError)):
            await real_transition(binding.invocation_id, 'succeeded', **terminal)
        assert (await jobs.get_job(binding.invocation_id))['status'] == 'running'
    else:
        result = await real_transition(binding.invocation_id, 'succeeded', **terminal)
        assert result['status'] == 'succeeded' and result['attempt_count'] == 1
