"""Authentic original Source cap/cutoff boundaries; no managed-runtime claim."""
import asyncio
import json
from datetime import datetime

import pytest
from sqlalchemy import select

from src.db.models import InferenceCostReservation, WorkBoardEvent
from src.execution import repo_original_producer as producer
from src.workflows import repo_repair_source as source
from src.workflows import repo_repair_source_recovery as recovery
from src.workflows import repo_repair_stop as stop
from src.workflows.job_runtime import _as_utc, _utc_now
from tests import test_repo_source_recovered_finalizer as fixtures
from tests.test_repo_source_public_recovery import _authenticated_original_api
from tests.repository_admission_lifecycle import repository_admission_signer
from tests.test_general_task_planner import accounting_db, forbid_external_inference


def _original_limits(monkeypatch, *, iterations, seconds):
    original = fixtures._actual_source_callback_journey
    async def construct(*args, **kwargs):
        assert 'work_limits' not in kwargs
        return await original(*args, **kwargs, work_limits={
            'max_iterations': iterations, 'max_total_seconds': seconds,
            'max_cost_usd': 0.0010019})
    monkeypatch.setattr(fixtures, '_actual_source_callback_journey', construct)


def _check_actual_command_closure(flow, status):
    with producer.stage_original_producer_completion(flow['registration']) as physical:
        result = producer.original_producer_completion_result(physical)
        manifest = result['manifest']
        assert manifest['status'] == status
        assert manifest['process_cleanup']['cleanup_proven'] is True
        assert manifest['process_cleanup']['oracle'] == 'linux_subreaper_waitpid_echild'
        assert manifest['commands']
        for command in manifest['commands']:
            assert command['stdout_eof'] is True and command['stderr_eof'] is True
            assert command['waited'] is True
        transport = manifest['supervisor_transport']
        assert transport['transport_kind'] == 'original_producer_durable_v2'
        for field in ('command_output_drained', 'command_descriptors_closed', 'original_children_waited', 'no_spawn'):
            assert transport[field] is True


async def _assert_terminal(jobs, job_id, *, reason, original_deadline, maximum_iterations):
    async with jobs._session() as db:
        root = await jobs._fetch(db, job_id)
        assert root.status == 'failed'
        original, work, *_ = source.read_repository_original(root)
        assert original['original_deadline_at'] == original_deadline
        assert work.limits.max_iterations == maximum_iterations
        assert jobs._repo_repair_reservation_state(root)['status'] == 'released'
        intent = source._repository_record(root, 'repository:stop-intent:v1')
        terminal = source._repository_record(root, 'repository:terminal:v1')
        assert intent['stop_reason'] == reason
        assert terminal['closure']['stop_reason'] == reason
        if reason == 'deadline_exhausted':
            assert terminal['closure']['limit_evidence']['cause'] == reason
        for identity in (original['native_binding']['parent_job_id'], original['native_binding']['invocation_id']):
            assert (await jobs._fetch(db, identity)).status == 'cancelled'
        costs = list((await db.scalars(select(InferenceCostReservation).where(
            InferenceCostReservation.job_id == job_id))).all())
        assert len(costs) == 1 and costs[0].state == 'settled'
        assert costs[0].actual_cost_microusd == 0
        events = list((await db.scalars(select(WorkBoardEvent).where(
            WorkBoardEvent.task_id == original['repository_task_id'],
            WorkBoardEvent.kind == 'attempt.repository_stopped'))).all())
        assert len(events) == 1
        assert json.loads(events[0].metadata_json)['no_learning'] is True


@pytest.mark.asyncio
async def test_public_committed_final_failed_iteration_uses_original_cap_stop(
        accounting_db, monkeypatch, repository_admission_signer):
    _original_limits(monkeypatch, iterations=1, seconds=900)
    async with _authenticated_original_api(accounting_db, monkeypatch, 'test_python', failed=True) as (flow, _, client):
        _check_actual_command_closure(flow, 'failed')
        jobs, job_id, owner = flow['jobs'], flow['kwargs']['job_id'], flow['kwargs']['owner']
        async with jobs._session() as db:
            root = await jobs._fetch(db, job_id)
            revision = root.revision
            original_deadline = source.read_repository_original(root)[0]['original_deadline_at']
            assert source._repository_record(root, 'repository:stop-intent:v1') is None
        async with recovery.stage_original_repository_completion_publication(flow['service'], jobs,
                job_id=job_id, owner=owner, iteration_index=1, expected_job_revision=revision) as witness:
            assert recovery.repository_completion_outcome(witness)['status'] == 'failed'
        url = '/api/workflows/repo-repair/' + job_id
        current = await client.get(url)
        assert current.status_code == 200, current.text
        assert current.json()['status'] == 'running'
        assert current.json()['source_recovery']['original_result'] == 'failed'
        assert current.json()['source_recovery']['public_actions'] == 'reconcile_original_cleanup'
        response = await client.post(url + '/source-recovery', json={
            'expected_job_revision': current.json()['revision'], 'action': 'reconcile_original_cleanup'})
        assert response.status_code == 200, response.text
        final = await client.get(url)
        assert final.status_code == 200, final.text
        assert final.json()['source_recovery']['state'] == 'original_stop_committed'
        assert final.json()['source_recovery']['physical_hold'] is False
        assert final.json()['source_recovery']['public_actions'] == 'unavailable'
        await _assert_terminal(jobs, job_id, reason='iterations_exhausted',
            original_deadline=original_deadline, maximum_iterations=1)


@pytest.mark.asyncio
async def test_public_original_deadline_crosses_after_genuine_cleanup_before_stop(
        accounting_db, monkeypatch, repository_admission_signer):
    _original_limits(monkeypatch, iterations=3, seconds=30)
    async with _authenticated_original_api(accounting_db, monkeypatch, 'test_python') as (flow, _, client):
        _check_actual_command_closure(flow, 'succeeded')
        jobs, job_id = flow['jobs'], flow['kwargs']['job_id']
        async with jobs._session() as db:
            root = await jobs._fetch(db, job_id)
            original_deadline = source.read_repository_original(root)[0]['original_deadline_at']
            cutoff = _as_utc(datetime.fromisoformat(original_deadline))
            assert _utc_now() < cutoff, 'Authentic producer must finish before its original cutoff'
        original_limit_owner = stop.repository_automatic_limit_reason
        observed = []
        async def after_real_cutoff(*args, **kwargs):
            # Entered only AFTER genuine original completion writer committed.
            async with jobs._session() as db:
                current = await jobs._fetch(db, job_id)
                identity = flow['registration']['iteration_id']
                assert source._repository_record(current, 'repository:cleanup:' + identity) is not None
                assert source._repository_record(current, 'repository:readback:' + identity) is not None
            assert _utc_now() < cutoff, 'Original cleanup must commit before cutoff'
            remaining = (cutoff - _utc_now()).total_seconds()
            assert remaining <= 30
            if remaining > 0:
                await asyncio.sleep(remaining + 0.02)
            assert _utc_now() >= cutoff
            reason = await original_limit_owner(*args, **kwargs)
            observed.append(reason)
            return reason
        monkeypatch.setattr(stop, 'repository_automatic_limit_reason', after_real_cutoff)
        url = '/api/workflows/repo-repair/' + job_id
        current = await client.get(url)
        assert current.status_code == 200, current.text
        assert _utc_now() < cutoff, 'Original public consumer must start before cutoff'
        response = await client.post(url + '/source-recovery', json={
            'expected_job_revision': current.json()['revision'], 'action': 'reconcile_original_cleanup'})
        assert response.status_code == 200, response.text
        assert observed == ['deadline_exhausted']
        final = await client.get(url)
        assert final.status_code == 200, final.text
        assert final.json()['source_recovery']['original_result'] == 'succeeded'
        assert final.json()['source_recovery']['physical_hold'] is False
        assert final.json()['source_recovery']['public_actions'] == 'unavailable'
        await _assert_terminal(jobs, job_id, reason='deadline_exhausted',
            original_deadline=original_deadline, maximum_iterations=3)
