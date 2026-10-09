"""Real disposable SQLite native-child phase fences; no tool/provider contact."""
from tests.general_task_method_lifecycle import native_admission_lifecycle
import json
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from src.db.models import WorkBoardAttempt, WorkBoardTask, WorkflowRunState
from src.work_board.contracts import (
    GeneralTaskCurrentManifestV1, GeneralTaskNativeChildBindingV1,
    GeneralTaskToolInputV1, GeneralTaskStepReceiptV1, WorkBoardOwner, GeneralTaskResume,
)
from src.work_board.general_task import GeneralTaskService, digest
from src.work_board.general_task_runtime_artifacts import (
    compile_creation_digest, compile_phase_digest, selected_grant_digest, stage_task_artifact, initial_native_manifest,
)
from src.work_board.dispatcher import WorkBoardDispatcher
from src.work_board.pipelines import root_binding


@pytest.mark.asyncio
async def test_positive_parent_journal_rejects_copy_foreign_scope_and_metadata_drift(task_runtime):
    from src.workflows import general_task_guard as guard
    from src.work_board.repository import BoardError
    from src.db.models import WorkBoardInputArtifact
    sessions, dispatcher, service, envelope, original = await running_task(task_runtime)
    parent_id = original['job']['job_id']
    async with sessions() as db:
        await guard.stage_positive_parent(dispatcher.jobs, db, parent_id)
        await guard._current(dispatcher.jobs, db, parent_id)
        key = (id(db), parent_id)
        journal = guard._STAGED_NATIVE_JOURNALS[key]
        guard._STAGED_NATIVE_JOURNALS[key] = replace(journal)
        with pytest.raises(DurableJobLeaseError, match='staged positive envelope'):
            await guard._current(dispatcher.jobs, db, parent_id)
        guard._STAGED_NATIVE_JOURNALS[key] = journal
        async def foreign():
            with pytest.raises(BoardError, match='Stage the current Source identity'):
                await guard._current(dispatcher.jobs, db, parent_id)
        await asyncio.create_task(foreign())
        task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == journal.envelope_pins[0]))
        artifact = await db.get(WorkBoardInputArtifact, task.input_artifact_id)
        artifact.revision += 1
        await db.flush()
        with pytest.raises(DurableJobLeaseError, match='immutable metadata changed|staged positive envelope'):
            await guard._current(dispatcher.jobs, db, parent_id)
        await db.rollback()
    assert key not in guard._STAGED_NATIVE_JOURNALS
    assert id(journal) not in guard._LIVE_NATIVE_JOURNALS
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
async def test_admitted_zero_claim_positive_and_missing_receipt_never_contacts(task_runtime, native_admission_lifecycle):
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
async def test_original_native_claim_denies_canonical_drift(task_runtime, change, native_admission_lifecycle):
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
async def test_missing_claim_receipt_blocks_journal_and_private_artifact_mutation(task_runtime, native_admission_lifecycle):
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
async def test_paired_operator_pause_fences_old_child_and_generic_resume(task_runtime, native_admission_lifecycle):
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
async def test_native_original_cutoff_is_narrower_and_not_renewed(task_runtime, native_admission_lifecycle):
    sessions, dispatcher, service, envelope, original = await running_task(task_runtime)
    manifest = GeneralTaskCurrentManifestV1.model_validate(original['manifest'])
    assert manifest.native_deadline_at < manifest.original_deadline_at
    with pytest.raises(DurableJobTransitionError, match='paired manifest resume'):
        await dispatcher.jobs.queue_job(manifest.run_id)
    parent = await dispatcher.jobs.get_job(manifest.run_id)
    from src.workflows.job_runtime import _as_utc
    assert _as_utc(datetime.fromisoformat(parent['deadline_at'])) == manifest.native_deadline_at


@pytest.mark.asyncio
async def test_no_child_operator_resume_keeps_same_attempt_and_original_cutoff(task_runtime, native_admission_lifecycle):
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
async def test_protected_checkpoint_capacity_and_missing_proof_fail_closed(task_runtime, native_admission_lifecycle):
    from src.workflows.job_runtime import _digest
    sessions, dispatcher, service, envelope, original = await running_task(task_runtime)
    manifest = GeneralTaskCurrentManifestV1.model_validate(original['manifest'])
    proof_ids = ['general:proof-' + str(i) for i in range(49)]
    manifest = manifest.model_copy(update={'required_checkpoint_ids': proof_ids})
    special = {'checkpoint_id': 'general-task:current-manifest:v1', 'safe': True,
               'payload': manifest.model_dump(mode='json'), 'state_digest': _digest(manifest.model_dump(mode='json'))}
    history = [{'checkpoint_id': key, 'safe': True} for key in proof_ids] + [special]
    from copy import deepcopy
    original_history = deepcopy(history)
    with pytest.raises(DurableJobTransitionError, match='capacity'):
        _bounded_checkpoint_receipts(history + [{'checkpoint_id': 'ordinary-later'}], limit=50)
    assert history == original_history
    retained = _bounded_checkpoint_receipts(history, limit=50)
    assert {x['checkpoint_id'] for x in retained} == set(proof_ids) | {special['checkpoint_id']}
    with pytest.raises(DurableJobTransitionError, match='capacity'):
        _bounded_checkpoint_receipts(history, limit=49)
    with pytest.raises(DurableJobTransitionError, match='proof is missing'):
        _bounded_checkpoint_receipts(history[1:], limit=50)


@pytest.mark.asyncio
async def test_rejected_input_admission_keeps_parent_board_and_manifest_atomic(task_runtime, native_admission_lifecycle):
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
async def test_parent_publication_rechecks_original_authority_before_private_write(task_runtime, native_admission_lifecycle):
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
async def test_claimed_child_cancel_without_original_callback_receipt_keeps_native_wait(task_runtime, contact_intent, native_admission_lifecycle):
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
    with pytest.raises(DurableJobLeaseError, match='protected transition evidence|close under native_wait'):
        await jobs.pause_general_task_native_parent(binding.parent_job_id,
            operator_owner=WorkBoardOwner(principal_id=OWNER, session_id=SESSION),
            expected_task_revision=manifest.task_revision, expected_revision=original['revision'],
            expected_manifest_revision=manifest.manifest_revision)
    with pytest.raises(DurableJobLeaseError):
        await jobs.resume_general_task_native_parent(binding.parent_job_id,
            owner='native-worker', expected_revision=original['revision'],
            expected_manifest_revision=manifest.manifest_revision)
    parent = await jobs.get_job(binding.parent_job_id)
    current = read_manifest(type('Row', (), {'checkpoint_receipts_json': json.dumps(parent['checkpoints'])})())
    assert current == manifest
    assert parent['revision'] == original['revision']
    assert current.phase == 'native_wait'
    assert current.admitted_invocation_ids == [binding.invocation_id]


@pytest.mark.asyncio
async def test_generic_checkpoint_and_caller_closure_cannot_forge_native_transition(task_runtime, native_admission_lifecycle):
    from src.work_board.general_task_native import publish_positive_claim
    from src.work_board.contracts import GeneralTaskToolClosureV1
    sessions, jobs, binding, _ = await admitted_child(task_runtime)
    await jobs.queue_job(binding.invocation_id)
    await jobs.claim_job(binding.invocation_id, owner='native-worker')
    await publish_positive_claim(jobs, binding, child_owner='native-worker', child_fence=1)
    parent = await jobs.get_job(binding.parent_job_id)
    child = await jobs.get_job(binding.invocation_id)
    for identity in ('general:approval:forged', 'general:cleanup:forged'):
        with pytest.raises(DurableJobTransitionError, match='fixed writer'):
            await jobs.record_checkpoint(binding.invocation_id, checkpoint_id=identity,
                state={}, checkpoint_payload={}, owner='native-worker', fencing_token=1)
    forged = GeneralTaskToolClosureV1(original_binding_digest=digest(binding.model_dump(mode='json')),
        invocation_id=binding.invocation_id, child_fence=1, descriptor_digest=binding.descriptor_digest,
        input_digest=binding.input_digest, outcome='returned', output_digest='a' * 64)
    with pytest.raises(PermissionError, match='original native callback closure witness'):
        await jobs.publish_general_task_tool_closure(binding.invocation_id, owner='native-worker',
            fencing_token=1, expected_parent_revision=parent['revision'], producer_witness=forged)
    assert (await jobs.get_job(binding.parent_job_id))['checkpoints'] == parent['checkpoints']
    assert (await jobs.get_job(binding.invocation_id))['revision'] == child['revision']


@pytest.mark.asyncio
@pytest.mark.parametrize('drift', ['capability', 'source_ref', 'input_owner'])
async def test_wrong_native_capability_denies_before_private_input_read(task_runtime, monkeypatch, drift, native_admission_lifecycle):
    from src.work_board.general_task_native import publish_positive_claim
    from src.work_board import general_task_runtime_artifacts as artifacts
    sessions, jobs, binding, _ = await admitted_child(task_runtime)
    await jobs.queue_job(binding.invocation_id)
    await jobs.claim_job(binding.invocation_id, owner='native-worker')
    await publish_positive_claim(jobs, binding, child_owner='native-worker', child_fence=1)
    async with sessions() as db:
        if drift == 'capability':
            row = await jobs._fetch(db, binding.invocation_id)
            authority = json.loads(row.declared_authority_json)
            authority['capability_id'] = 'document.read.v1'
            row.declared_authority_json = json.dumps(authority, sort_keys=True)
            row.authority_digest = _digest(authority)
        else:
            task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == binding.task_id))
            if drift == 'source_ref':
                row = task
                row.typed_input_ref = 'workspace-json:foreign-source.json'
            else:
                from src.db.models import WorkBoardInputArtifact
                row = await db.get(WorkBoardInputArtifact, task.input_artifact_id)
                row.owner_principal_id = 'foreign-owner'
        db.add(row)
    rejection = 'native child binding' if drift == 'capability' else 'original native phase'
    reads = []
    def forbidden_private_read(*args, **kwargs):
        reads.append('private-read')
        raise AssertionError('WRONG_CAP_PRIVATE_INPUT_CANARY')
    monkeypatch.setattr(artifacts, 'read_native_artifact_reference', forbidden_private_read)
    async with sessions() as db:
        row = await jobs._fetch(db, binding.invocation_id)
        with pytest.raises(DurableJobLeaseError, match=rejection):
            await artifacts.read_current_native_tool_input(db, row)
        with pytest.raises(DurableJobLeaseError, match=rejection):
            await assert_general_task_child_current(db, row)
    assert reads == []
    before = await jobs.get_job(binding.invocation_id)
    with pytest.raises(DurableJobLeaseError, match=rejection):
        await jobs.heartbeat_job(binding.invocation_id, owner='native-worker', fencing_token=1)
    with pytest.raises(DurableJobLeaseError, match=rejection):
        await jobs.record_effect(binding.invocation_id, effect_type='tool-contact', status='intent',
            owner='native-worker', fencing_token=1)
    with pytest.raises(DurableJobLeaseError, match=rejection):
        await jobs.record_artifact(binding.invocation_id, file_path='artifacts/wrong-capability.txt',
            content='WRONG_CAP_PRIVATE_ARTIFACT_CANARY', owner='native-worker', fencing_token=1)
    with pytest.raises(DurableJobLeaseError, match=rejection):
        await jobs.record_checkpoint(binding.invocation_id, checkpoint_id='wrong-capability-receipt',
            state={'verified': True}, owner='native-worker', fencing_token=1)
    with pytest.raises(DurableJobLeaseError, match=rejection):
        await jobs.transition_job(binding.invocation_id, 'succeeded',
            owner='native-worker', fencing_token=1, result={'verified': True})
    after = await jobs.get_job(binding.invocation_id)
    assert after['revision'] == before['revision']
    assert after['effects'] == before['effects']
    assert after['artifacts'] == before['artifacts']
    assert after['checkpoints'] == before['checkpoints']
    assert after['status'] == 'running'
    assert not (task_runtime[1] / 'artifacts/wrong-capability.txt').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('drift', ['missing', 'malformed', 'foreign', 'stale_fence', 'expired_approval', 'descriptor_before_resume'])
async def test_resumed_approval_drift_denies_every_native_writer(task_runtime, drift, request, monkeypatch, native_admission_lifecycle):
    from src.auth.service import authenticate_session
    from src.approval.repository import ApprovalRepository
    from src.db.models import ApprovalRequest
    from src.native_tools.registry import ToolRegistry
    from src.work_board.contracts import GeneralTaskCreate, GeneralTaskInput, PlanSpec
    from src.work_board.general_task_native import admit_native_step, run_native_step
    from src.workflows.general_task_guard import approval_checkpoint_id
    from tests.test_general_task_adapters import mcp_registry
    registry = ToolRegistry()
    registry.start()
    request.addfinalizer(registry.stop)
    registry, _manager, tool, _, _ = mcp_registry.__wrapped__(task_runtime[1], registry)
    descriptors = registry.descriptors()
    descriptor = next(item for item in descriptors if item.tool_id == 'mcp:local:repo_read')
    creation = GeneralTaskCreate(goal_revision=1, idempotency_key='resumed-drift', expected_plan_revision=1,
        input=GeneralTaskInput(goal_ref='goal-1', intent='Read one explicitly approved owned MCP source',
            requested_output=descriptor.output_schema,
            tool_set_digest=digest([item.model_dump(mode='json') for item in descriptors])),
        plan=PlanSpec(revision=1, steps=[{'step_id': 'mcp-read', 'tool_id': descriptor.tool_id,
            'input': {'query': 'literal owned repository'}, 'output_contract': descriptor.output_schema}]))
    sessions, dispatcher, service, envelope, original = await running_task(task_runtime,
        creation_request=creation, registry_override=registry)
    jobs = dispatcher.jobs
    binding, _ = await admit_native_step(jobs, original['job']['job_id'],
        owner=original['job']['lease']['owner'], fence=original['job']['lease']['fencing_token'],
        step=envelope.plan.steps[0], descriptor=descriptor, inputs=envelope.plan.steps[0].input)
    operator = await authenticate_session(binding.original_root_id, touch=False)
    waiting, _, _ = await run_native_step(service, jobs, binding,
        child_owner='native-worker', principal=operator.principal)
    assert waiting['awaiting_approval'] and tool.calls == 0
    approved = await ApprovalRepository().resolve(waiting['approval_id'], 'approved')
    assert approved is not None and approved.status == 'approved'
    async with sessions() as db:
        parent = await jobs._fetch(db, binding.parent_job_id)
        manifest = read_manifest(parent)
        parent_revision = parent.revision
        attempt = await db.get(WorkBoardAttempt, binding.attempt_id)
        resume_request = GeneralTaskResume(expected_revision=manifest.task_revision,
            expected_plan_revision=manifest.plan_revision, workflow_run_id=parent.run_identity,
            attempt_id=attempt.attempt_id, fencing_token=attempt.fencing_token,
            workflow_revision=parent_revision, approval_id=waiting['approval_id'],
            child_job_id=binding.invocation_id, expected_manifest_revision=manifest.manifest_revision)
    if drift == 'descriptor_before_resume':
        from src.work_board.repository import BoardError
        # An earlier successful preflight cannot authorize a later changed descriptor.
        async with sessions() as db:
            parent = await jobs._fetch(db, binding.parent_job_id)
            task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == binding.task_id))
            attempt = await db.get(WorkBoardAttempt, binding.attempt_id)
            await service.validate_native_resume(db,
                WorkBoardOwner(principal_id=OWNER, session_id=SESSION), task, attempt,
                parent, manifest, envelope, await jobs._fetch(db, binding.invocation_id), binding, resume_request)
        before_parent = await jobs.get_job(binding.parent_job_id)
        before_child = await jobs.get_job(binding.invocation_id)
        changed = descriptor.model_copy(update={'deadline': descriptor.deadline + 1})
        monkeypatch.setattr(registry, 'descriptors', lambda: [changed if item.tool_id == descriptor.tool_id
            else item for item in descriptors])
        with pytest.raises(BoardError):
            await jobs.resume_general_task_native_approval(binding.invocation_id,
                operator_owner=WorkBoardOwner(principal_id=OWNER, session_id=SESSION),
                expected_task_revision=manifest.task_revision, expected_parent_revision=parent_revision,
                expected_manifest_revision=manifest.manifest_revision, approval_id=waiting['approval_id'],
                service=service, request=resume_request)
        assert await jobs.get_job(binding.parent_job_id) == before_parent
        assert await jobs.get_job(binding.invocation_id) == before_child
        async with sessions() as db:
            task = await db.scalar(select(WorkBoardTask).where(WorkBoardTask.task_id == binding.task_id))
            assert task.task_revision == manifest.task_revision
            assert (await db.get(WorkBoardAttempt, binding.attempt_id)).fencing_token == manifest.board_fence
        assert tool.calls == 0
        return
    resumed = await jobs.resume_general_task_native_approval(binding.invocation_id,
        operator_owner=WorkBoardOwner(principal_id=OWNER, session_id=SESSION),
        expected_task_revision=manifest.task_revision, expected_parent_revision=parent_revision,
        expected_manifest_revision=manifest.manifest_revision, approval_id=waiting['approval_id'],
        service=service, request=resume_request)
    owner, fence = resumed['runtime_owner'], resumed['child']['lease']['fencing_token']
    async with sessions() as db:
        await assert_general_task_child_current(db, await jobs._fetch(db, binding.invocation_id))
        if drift == 'expired_approval':
            approval = await db.get(ApprovalRequest, waiting['approval_id'])
            approval.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.add(approval)
        else:
            parent = await jobs._fetch(db, binding.parent_job_id)
            history = json.loads(parent.checkpoint_receipts_json)
            transition = next(item for item in history if item['checkpoint_id'] == approval_checkpoint_id(binding))
            if drift == 'missing':
                history.remove(transition)
            elif drift == 'malformed':
                transition['payload']['unrecognized_authority'] = True
            elif drift == 'foreign':
                transition['payload']['original_binding']['invocation_id'] = 'foreign-child'
            elif drift == 'stale_fence':
                transition['payload']['current_child_fence'] = fence - 1
            transition['state_digest'] = _digest(transition['payload'])
            parent.checkpoint_receipts_json = json.dumps(history)
            db.add(parent)
    before = await jobs.get_job(binding.invocation_id)
    lease = dict(owner=owner, fencing_token=fence)
    async with sessions() as db:
        with pytest.raises(DurableJobLeaseError):
            await assert_general_task_child_current(db, await jobs._fetch(db, binding.invocation_id))
    for operation in (
        lambda: jobs.heartbeat_job(binding.invocation_id, **lease),
        lambda: jobs.record_effect(binding.invocation_id, effect_type='tool-contact', status='intent', **lease),
        lambda: jobs.record_readback(binding.invocation_id, target_path='artifacts/drift.txt', status='succeeded', **lease),
        lambda: jobs.record_artifact(binding.invocation_id, file_path='artifacts/drift.txt', content='PRIVATE_CANARY', **lease),
        lambda: jobs.record_checkpoint(binding.invocation_id, checkpoint_id='drift-receipt', state={}, **lease),
        lambda: jobs.transition_job(binding.invocation_id, 'succeeded', result={'verified': True}, **lease),
    ):
        with pytest.raises((DurableJobLeaseError, DurableJobTransitionError)):
            await operation()
    after = await jobs.get_job(binding.invocation_id)
    assert after['revision'] == before['revision']
    assert after['effects'] == before['effects'] and after['artifacts'] == before['artifacts']
    assert after['checkpoints'] == before['checkpoints'] and after['status'] == 'running'
    assert tool.calls == 0 and not (task_runtime[1] / 'artifacts/drift.txt').exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('tamper_output', [False, True])
async def test_verified_terminal_receipt_never_reopens_contact_and_requires_physical_output(task_runtime, monkeypatch, tamper_output, request, native_admission_lifecycle):
    from dataclasses import replace
    from src.auth.service import authenticate_session
    from src.work_board.general_task_native import admit_native_step, run_native_step
    from src.work_board.repository import BoardError
    from src.workspace import canonical_workspace_root
    from config.settings import settings
    from src.native_tools.registry import ToolRegistry
    from src.work_board.contracts import GeneralTaskCreate, GeneralTaskInput, PlanSpec
    from src.tools.filesystem_tool import read_file
    registry = ToolRegistry()
    registry.start()
    request.addfinalizer(registry.stop)
    descriptor = next(item for item in registry.descriptors() if item.tool_id == 'read_file')
    (task_runtime[1] / 'terminal-source.txt').write_text('hello')
    calls = []
    real_read = read_file.forward
    def counted_read(file_path):
        calls.append(file_path)
        return real_read(file_path)
    monkeypatch.setattr(read_file, 'forward', counted_read)
    creation = GeneralTaskCreate(goal_revision=1, idempotency_key='terminal-read', expected_plan_revision=1,
        input=GeneralTaskInput(goal_ref='goal-1', intent='Read one owned local file',
            requested_output=descriptor.output_schema,
            tool_set_digest=digest([item.model_dump(mode='json') for item in registry.descriptors()])),
        plan=PlanSpec(revision=1, steps=[{'step_id': 'terminal-read', 'tool_id': 'read_file',
            'input': {'file_path': 'terminal-source.txt'}, 'output_contract': descriptor.output_schema}]))
    sessions, dispatcher, service, envelope, original = await running_task(task_runtime,
        creation_request=creation, registry_override=registry)
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
    assert output['content'] == 'hello' and calls == ['terminal-source.txt']
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


@pytest.mark.asyncio
@pytest.mark.parametrize('attachment_failure', ['refused', 'crash'])
async def test_actual_precontact_wait_attachment_failure_rolls_back_paired_state(task_runtime, monkeypatch, request, attachment_failure, native_admission_lifecycle):
    from src.auth.service import authenticate_session
    from src.native_tools.registry import ToolRegistry
    from src.work_board.contracts import GeneralTaskCreate, GeneralTaskInput, PlanSpec
    from src.work_board.general_task_native import admit_native_step, run_native_step
    from src.approval.repository import approval_repository
    from tests.test_general_task_adapters import mcp_registry
    registry = ToolRegistry()
    registry.start()
    request.addfinalizer(registry.stop)
    registry, _manager, tool, _, _ = mcp_registry.__wrapped__(task_runtime[1], registry)
    descriptors = registry.descriptors()
    descriptor = next(item for item in descriptors if item.tool_id == 'mcp:local:repo_read')
    creation = GeneralTaskCreate(goal_revision=1, idempotency_key='wait-attachment-fault', expected_plan_revision=1,
        input=GeneralTaskInput(goal_ref='goal-1', intent='Read using one explicitly approved local tool',
            requested_output=descriptor.output_schema,
            tool_set_digest=digest([item.model_dump(mode='json') for item in descriptors])),
        plan=PlanSpec(revision=1, steps=[{'step_id': 'mcp-read', 'tool_id': descriptor.tool_id,
            'input': {'query': 'literal owned repository'}, 'output_contract': descriptor.output_schema}]))
    sessions, dispatcher, service, envelope, current = await running_task(task_runtime,
        creation_request=creation, registry_override=registry)
    jobs = dispatcher.jobs
    binding, _ = await admit_native_step(jobs, current['job']['job_id'],
        owner=current['job']['lease']['owner'], fence=current['job']['lease']['fencing_token'],
        step=envelope.plan.steps[0], descriptor=descriptor, inputs=envelope.plan.steps[0].input)
    before = await jobs.get_job(binding.parent_job_id)
    async def attachment_fault(*args, **kwargs):
        if attachment_failure == 'crash':
            raise RuntimeError('fixture crash before approval wait attachment')
        return None
    monkeypatch.setattr(approval_repository, 'attach_general_task_native_child_wait_binding_in_session', attachment_fault)
    operator = await authenticate_session(binding.original_root_id, touch=False)
    expected = RuntimeError if attachment_failure == 'crash' else DurableJobLeaseError
    with pytest.raises(expected):
        await run_native_step(service, jobs, binding,
            child_owner='general-task-native:' + binding.invocation_id, principal=operator.principal)
    parent = await jobs.get_job(binding.parent_job_id)
    child = await jobs.get_job(binding.invocation_id)
    manifest = read_manifest(type('Row', (), {'checkpoint_receipts_json': json.dumps(parent['checkpoints'])})())
    assert manifest.phase == 'native_wait' and manifest.admitted_invocation_ids == [binding.invocation_id]
    assert child['status'] == 'running' and child['attempt_count'] == 1
    assert tool.calls == 0
    # Admission reserves finite slots, but a failed attachment may replace none
    # of them with actual approval/closure evidence or change their provenance.
    protected_slots = lambda projection: [item for item in projection['checkpoints']
        if item['checkpoint_id'].startswith(('general:approval:', 'general:cleanup:'))]
    assert protected_slots(parent) == protected_slots(before)
    assert not any(item.get('payload', {}).get('schema_version') in {
        'general_task.native_approval_transition.v1', 'general_task.tool_closure.v1'}
        for item in parent['checkpoints'])
    assert parent['revision'] == before['revision'] + 1  # only original positive claim receipt
    assert len(child['effects']) == 1 and child['effects'][0]['status'] == 'intent'
    with pytest.raises(DurableJobLeaseError):
        await jobs.claim_job(binding.invocation_id, owner='replacement-worker', continue_existing_attempt=True)
