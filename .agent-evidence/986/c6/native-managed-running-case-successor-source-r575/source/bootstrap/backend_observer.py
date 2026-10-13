'Managed-test observation only; installed once by the trusted R5 bootstrap.\n\nReceivers own fixed private marker/release files. Nothing here grants a product\ncapability, changes a return value, or performs filesystem I/O in a SQL writer.\n'
import asyncio
import contextvars
import hashlib
import os
import time
from datetime import datetime, timezone


class ManagedOriginalDeliveryFailure(RuntimeError):
    'Delivery failed after the original physical executor actually returned.'


def install(context, *, selected, state, emit, released):
    from src.execution import repo_original_producer as native
    from src.execution.repo_sandbox import LocalRepoRepairExecutor
    from src.execution import repo_sandbox as cleanup_owner
    from src.execution.repo_node import NodeRepoRepairExecutor
    from src.workflows import repo_repair_source as source
    from src.workflows import repo_repair_source_recovery as recovery
    from src.workflows.repo_repair import RepoRepairService

    originals = []
    markers = set()
    absent = set()
    written = {}
    failed = set()
    public_unknown = context['reviewed_case']['id'] in {'public-unknown-http', 'public-unknown-inspector'}
    actual_services = {}
    actual_outer_tasks = {}
    returned_results = {}
    registration = contextvars.ContextVar('managed_observed_registration', default=None)

    def replace(owner, name, wrapper):
        original = getattr(owner, name)
        setattr(owner, name, wrapper(original))
        originals.append((owner, name, original, getattr(owner, name)))

    def cutoff(*values):
        parsed = [datetime.fromisoformat(value).astimezone(timezone.utc) for value in values]
        return min(parsed)

    def deadline(end):
        remaining = (end - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 0:
            raise RuntimeError('managed_observer_original_cutoff_expired')
        return time.monotonic() + min(5.0, remaining)

    def marker(point, *, job_id, iteration_id, end, **fields):
        if point in markers:
            raise RuntimeError('managed_observer_cutpoint_repeated')
        until = deadline(end)
        markers.add(point)
        emit(point, {'nonce': context['nonce'], 'backend_pid': os.getpid(),
            'point': point, 'job_id': job_id, 'iteration_id': iteration_id, **fields})
        return until

    def wait_sync(point, until, end):
        while not released(point):
            if time.monotonic() >= until or datetime.now(timezone.utc) >= end:
                raise RuntimeError('managed_observer_release_timeout')
            time.sleep(0.01)

    async def wait_async(point, until, end):
        while not released(point):
            if time.monotonic() >= until or datetime.now(timezone.utc) >= end:
                raise RuntimeError('managed_observer_release_timeout')
            await asyncio.sleep(0.01)

    def observe_issue(original):
        async def wrapped(service, jobs, job, *, owner):
            actual = await original(service, jobs, job, owner=owner)
            if public_unknown:
                actual_services[job.job_id] = service
                actual_outer_tasks[job.job_id] = asyncio.current_task()
            if selected == 'intent_before_popen':
                # Source committed the intent before calling this genuine factory;
                # this factory cannot spawn. A fresh owner context confirms it.
                current = await source._repository_precontact(service, jobs,
                    job_id=job.job_id, owner=owner)
                identity = job.iteration_binding.iteration_id
                intent = source._repository_record(current['run'], 'repository:execution:' + identity)
                if (intent is None or intent['process_binding'] != job.iteration_binding.projection()
                        or source._repository_record(current['run'], 'repository:producer:' + identity) is not None):
                    raise RuntimeError('managed_observer_intent_boundary_changed')
                end = cutoff(job.execution_deadline_at, current['run'].deadline_at.isoformat())
                until = marker(selected, job_id=job.job_id, iteration_id=identity, end=end,
                    intent_digest=source._source_digest(intent))
                await wait_async(selected, until, end)
            return actual
        return wrapped

    def observe_native_issue(original):
        async def wrapped(service, jobs, job, *, register_ready, authorize_command, registration_readback):
            def ready(actual_ready):
                ack = register_ready(actual_ready)
                if selected == 'registration_commit_before_ack':
                    actual = registration_readback()
                    if (type(actual) is not dict or actual['ready'] != actual_ready.projection()
                            or source._source_digest(actual) != ack.registration_digest
                            or actual['ready_digest'] != ack.ready_digest):
                        raise RuntimeError('managed_observer_registration_readback_changed')
                    end = cutoff(actual['execution_deadline_at'], actual['original_deadline_at'])
                    until = marker(selected, job_id=job.job_id,
                        iteration_id=job.iteration_binding.iteration_id, end=end,
                        producer_pid=actual_ready.pid, registration_digest=ack.registration_digest,
                        ready_digest=ack.ready_digest)
                    wait_sync(selected, until, end)
                return ack
            return await original(service, jobs, job, register_ready=ready,
                authorize_command=authorize_command, registration_readback=registration_readback)
        return wrapped

    def observe_registration(original):
        def wrapped(*args, **kwargs):
            actual = original(*args, **kwargs)
            registration.set(actual)
            return actual
        return wrapped

    def observe_absence(original):
        def wrapped(service, relative_path):
            actual = original(service, relative_path)
            if selected == 'cleanup_artifact_fsync_before_append' and actual is None:
                absent.add((id(service), relative_path))
            return actual
        return wrapped

    def observe_write(original):
        def wrapped(service, relative_path, raw):
            actual = original(service, relative_path, raw)
            key = (id(service), relative_path)
            if selected == 'cleanup_artifact_fsync_before_append' and key in absent:
                absent.remove(key)
                written[(id(service), actual[0])] = (actual[1], raw)
            return actual
        return wrapped

    def observe_read(original):
        def wrapped(service, ref, *, expected_digest):
            actual = original(service, ref, expected_digest=expected_digest)
            expected = written.pop((id(service), ref), None)
            if expected is not None:
                digest, raw = expected
                reg = registration.get()
                literal = 'workspace-json:artifacts/repo-repair/model/iteration-' + reg['iteration_id'] + '-cleanup.json'
                if (ref != literal or digest != expected_digest or actual != raw
                        or hashlib.sha256(actual).hexdigest() != expected_digest):
                    raise RuntimeError('managed_observer_cleanup_readback_changed')
                end = cutoff(reg['execution_deadline_at'], reg['original_deadline_at'])
                until = marker(selected, job_id=reg['job_id'], iteration_id=reg['iteration_id'],
                    end=end, artifact_ref=ref, artifact_digest=expected_digest)
                wait_sync(selected, until, end)
            return actual
        return wrapped

    def observe_execute(original):
        def wrapped(executor, job, *args, **kwargs):
            actual = original(executor, job, *args, **kwargs)
            if state == 'unknown_external_effect' and job.job_id not in failed:
                producer = kwargs.get('producer_owner')
                native.assert_original_producer_live_completion(producer, actual)
                if public_unknown:
                    cleanup_owner.assert_repo_iteration_cleanup_witness(actual.get('iteration_cleanup_witness'), job)
                else:
                    source.assert_repo_iteration_cleanup_witness(actual.get('iteration_cleanup_witness'), job)
                # An actual registry lookup validates identity; no copied result
                # is returned, injected, or used to grant recovery authority.
                native.original_producer_result_registration(actual)
                if public_unknown:
                    registered = native.original_producer_result_registration(actual)
                    end = cutoff(registered['execution_deadline_at'], registered['original_deadline_at'])
                    until = marker('public_stop_before_uncertainty', job_id=job.job_id,
                        iteration_id=registered['iteration_id'], end=end)
                    returned_results[job.job_id] = actual
                    wait_sync('public_stop_before_uncertainty', until, end)
                    return actual
                failed.add(job.job_id)
                raise ManagedOriginalDeliveryFailure('managed_original_delivery_failure')
            return actual
        return wrapped


    def observe_outer_cleanup(original):
        def wrapped(witness, job):
            result = original(witness, job)
            if public_unknown and job.job_id in returned_results and job.job_id not in failed:
                try:
                    current_task = asyncio.current_task()
                except RuntimeError:
                    current_task = None
                if current_task is actual_outer_tasks.get(job.job_id) and current_task is not None:
                    actual = returned_results[job.job_id]
                    service = actual_services[job.job_id]
                    identity = job.iteration_binding.iteration_id
                    callback = service._iterative_process_callbacks.get(identity)
                    if (callback is None or not callback.done() or callback.cancelled()
                            or callback.exception() is not None or callback.result() is not actual
                            or actual.get('iteration_cleanup_witness') is not witness):
                        raise RuntimeError('public_unknown_successful_original_callback_missing')
                    failed.add(job.job_id)
                    raise ManagedOriginalDeliveryFailure('managed_original_delivery_failure')
            return result
        return wrapped

    def observe_quarantine(original):
        async def wrapped(service, jobs, **kwargs):
            actual = await original(service, jobs, **kwargs)
            if state == 'unknown_external_effect' and kwargs['job_id'] in failed:
                current = await jobs.get_job(kwargs['job_id'])
                if current['status'] != 'unknown_external_effect':
                    raise RuntimeError('managed_observer_original_quarantine_missing')
                identity = kwargs['result']['iteration_id']
                async with jobs._session() as db:
                    run = await jobs._fetch(db, kwargs['job_id'])
                    if (source._repository_record(run, 'repository:cleanup:' + identity) is not None
                            or source._repository_record(run, 'repository:readback:' + identity) is not None):
                        raise RuntimeError('managed_observer_unknown_already_appended')
                # No pause: Source still delivers its actual failure to HTTP.
                emit('unknown_first_stage', {'nonce': context['nonce'], 'backend_pid': os.getpid(),
                    'point': 'unknown_first_stage', 'job_id': kwargs['job_id'],
                    'iteration_id': identity, 'status': current['status'],
                    'revision': current['revision'], 'cleanup_appended': False, 'readback_appended': False})
            return actual
        return wrapped

    replace(recovery, 'issue_repository_source_producer', observe_issue)
    replace(native, 'issue_original_producer_owner', observe_native_issue)
    replace(recovery, 'read_registered_repository_producer', observe_registration)
    replace(recovery, '_read_original_cleanup_envelope_if_present', observe_absence)
    replace(RepoRepairService, '_write_private_artifact', observe_write)
    replace(RepoRepairService, '_read_private_artifact', observe_read)
    if public_unknown:
        # Source imports this exact original function locally at admission entry.
        # Installing before that entry binds the wrapper only for these cases.
        replace(cleanup_owner, 'assert_repo_iteration_cleanup_witness', observe_outer_cleanup)
    replace(LocalRepoRepairExecutor, 'execute_job', observe_execute)
    replace(NodeRepoRepairExecutor, 'execute_job', observe_execute)
    replace(source, '_quarantine_original_uncertainty', observe_quarantine)

    def restore():
        for owner, name, original, wrapper in reversed(originals):
            if getattr(owner, name) is not wrapper:
                raise RuntimeError('managed_observer_restore_order_changed')
            setattr(owner, name, original)
        originals.clear()
    return restore
