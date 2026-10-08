"""Real SQLite/file-lock regressions; only existing HTTP transports are intercepted."""
import asyncio
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
import multiprocessing
from pathlib import Path
import sqlite3

import pytest
from sqlalchemy import event, select

from src.db.models import Goal, WorkBoardAttempt, WorkflowRunState
from src.work_board import near_text_native as native
from src.work_board.repository import BoardError
from tests.test_inference_accounting import accounting_db
from tests.test_research_native_vertical import real_auth
from tests.test_near_accounting_work_journey import actual_billing_journey, drive, state
from tests.test_near_text_work_journey import create_actual_near_task


@pytest.fixture
def scopes(monkeypatch):
    active = []
    original = native.native_policy_scope
    @contextmanager
    def observed(*args, **kwargs):
        with original(*args, **kwargs) as scope:
            active.append(scope)
            try:
                yield scope
            finally:
                assert active.pop() is scope
    monkeypatch.setattr(native, 'native_policy_scope', observed)
    return active


def _lock_child(connection, database, lock_path, writer, hold=False):
    import fcntl
    import os
    database_connection = None
    descriptor = None
    try:
        if writer:
            database_connection = sqlite3.connect(database, timeout=5)
            database_connection.execute('BEGIN IMMEDIATE')
        connection.send('ready')
        if not connection.poll(10) or connection.recv() != 'probe':
            raise RuntimeError('bounded fixture barrier unavailable')
        descriptor = os.open(lock_path, os.O_RDWR | os.O_NOFOLLOW)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            connection.send('busy')
        else:
            connection.send('acquired')
        if hold:
            if not connection.poll(10) or connection.recv() != 'release':
                raise RuntimeError('bounded fixture release unavailable')
        if database_connection is not None:
            database_connection.commit()
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if database_connection is not None:
            database_connection.close()
        connection.close()


@asynccontextmanager
async def _child(database, lock_path, *, writer=False, hold=False):
    context = multiprocessing.get_context('spawn')
    parent, child = context.Pipe()
    process = context.Process(target=_lock_child, args=(child, str(database), str(lock_path), writer, hold))
    process.start()
    child.close()
    try:
        assert await asyncio.wait_for(asyncio.to_thread(parent.recv), 10) == 'ready'
        yield parent
    finally:
        if hold and process.is_alive():
            parent.send('release')
        parent.close()
        await asyncio.to_thread(process.join, 5)
        if process.is_alive():
            process.terminate()
            await asyncio.to_thread(process.join, 5)
        if process.is_alive():
            process.kill()
            await asyncio.to_thread(process.join, 5)
        assert not process.is_alive()
        assert process.exitcode == 0
        process.close()


async def _probe(database, lock_path):
    async with _child(database, lock_path) as pipe:
        pipe.send('probe')
        return await asyncio.wait_for(asyncio.to_thread(pipe.recv), 5)


def _sync_probe(database, lock_path):
    context = multiprocessing.get_context('spawn')
    parent, child = context.Pipe()
    process = context.Process(target=_lock_child, args=(child, str(database), str(lock_path), False))
    process.start()
    child.close()
    try:
        assert parent.poll(10) and parent.recv() == 'ready'
        parent.send('probe')
        assert parent.poll(5)
        return parent.recv()
    finally:
        parent.close()
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join(5)
        if process.is_alive():
            process.kill()
            process.join(5)
        assert not process.is_alive() and process.exitcode == 0
        process.close()


async def _drive_until_marker(dispatcher, marker):
    for _ in range(4):
        await dispatcher.run_pass()
        if marker:
            return
    assert marker, 'target negative fixture did not execute within four normal passes'


async def _assert_original_binding(factory, task_id, bindings, expected):
    assert len(bindings) == expected and all(binding == bindings[0] for binding in bindings)
    original_task, original_attempt, original_job = bindings[0]
    assert original_task == task_id
    assert original_job == 'near-text:' + native.digest([task_id, original_attempt])[:40]
    async with factory.accounting_sessions() as db:
        attempts = list((await db.scalars(select(WorkBoardAttempt).where(WorkBoardAttempt.task_id == task_id))).all())
        assert len(attempts) == 1 and attempts[0].attempt_id == original_attempt


def _paths(engine):
    from config.settings import settings
    from src.workspace.production import ProductionWorkspace
    workspace = ProductionWorkspace(host_root=Path(settings.workspace_dir))
    return Path(engine.url.database), workspace.lifecycle_directory / 'accounting.lock'


def _selected(phase, args, kwargs):
    return phase != 'terminal' or (len(args) > 1 and args[1] == 'succeeded')


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['admit', 'queue', 'claim', 'terminal'])
async def test_sqlite_writer_can_take_policy_lock_while_near_waits(actual_billing_journey, monkeypatch, scopes, phase):
    client, factory, jobs, dispatcher, controls, engine = actual_billing_journey
    task_id, _ = await create_actual_near_task(client, factory)
    database, lock_path = _paths(engine)
    method = {'admit': 'admit_job', 'queue': 'queue_job', 'claim': 'claim_job', 'terminal': 'transition_job'}[phase]
    original = getattr(jobs, method)
    observed = []

    async def contender(*args, **kwargs):
        if not _selected(phase, args, kwargs):
            return await original(*args, **kwargs)
        attempted = asyncio.Event()
        loop = asyncio.get_running_loop()
        def beginning(_connection, _cursor, statement, _parameters, _context, _executemany):
            if statement.strip().upper() == 'BEGIN IMMEDIATE':
                loop.call_soon_threadsafe(attempted.set)
        async with _child(database, lock_path, writer=True) as pipe:
            event.listen(engine.sync_engine, 'before_cursor_execute', beginning)
            async def coordinate():
                await asyncio.wait_for(attempted.wait(), 3)
                pipe.send('probe')
                observed.append(await asyncio.wait_for(asyncio.to_thread(pipe.recv), 3))
                assert observed == ['acquired']
            pending = asyncio.create_task(coordinate())
            try:
                result = await original(*args, **kwargs)
                await pending
                return result
            finally:
                event.remove(engine.sync_engine, 'before_cursor_execute', beginning)
                if not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)

    monkeypatch.setattr(jobs, method, contender)
    await asyncio.wait_for(drive(dispatcher), 35)
    assert observed == ['acquired']
    task, run, row = await state(factory, task_id)
    assert run.status == 'succeeded' and row.state == 'settled'
    assert controls['order'] == ['near'] and controls['peak'] == 1
    assert await _probe(database, lock_path) == 'acquired'


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['admit', 'queue', 'claim', 'terminal'])
async def test_policy_lock_survives_real_session_commit(actual_billing_journey, monkeypatch, scopes, phase):
    from src.workflows import job_runtime
    client, factory, _jobs, dispatcher, _controls, engine = actual_billing_journey
    task_id, _ = await create_actual_near_task(client, factory)
    database, lock_path = _paths(engine)
    original_sessions = factory.accounting_sessions
    observed = []

    @asynccontextmanager
    async def sessions():
        selected = False
        async with original_sessions() as db:
            yield db
            scope = scopes[-1] if scopes else None
            selected = scope is not None and scope.entered and scope.phase == phase and scope.db is db
            if selected:
                observed.append(('before_commit', await _probe(database, lock_path)))
        if selected:
            observed.append(('after_commit', await _probe(database, lock_path)))

    monkeypatch.setattr(job_runtime, 'get_session', sessions)
    await asyncio.wait_for(drive(dispatcher), 35)
    assert observed == [('before_commit', 'busy'), ('after_commit', 'busy')]
    assert (await state(factory, task_id))[1].status == 'succeeded'
    assert await _probe(database, lock_path) == 'acquired'


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['admit', 'queue', 'claim', 'terminal'])
@pytest.mark.parametrize('failure', ['guard', 'cancel', 'commit'])
async def test_error_cancellation_and_real_commit_failure_release_policy_lock(actual_billing_journey, monkeypatch, scopes, phase, failure):
    from src.workflows import job_runtime
    client, factory, _jobs, dispatcher, controls, engine = actual_billing_journey
    task_id, _ = await create_actual_near_task(client, factory)
    database, lock_path = _paths(engine)
    original_sessions = factory.accounting_sessions
    original_guard = native.recheck_provider_contact
    failures = []
    bindings = []
    rolled_back = []

    async def guard(*args, **kwargs):
        result = await original_guard(*args, **kwargs)
        scope = scopes[-1] if scopes else None
        if failure == 'guard' and scope is not None and scope.entered and scope.phase == phase:
            bindings.append((scope.witness.task_id, scope.witness.attempt_id, scope.witness.job_id))
            failures.append('guard')
            raise BoardError('near_policy_changed', 'fixture authority race after original check')
        return result

    @asynccontextmanager
    async def sessions():
        async with original_sessions() as db:
            def rollback_finished(_session):
                scope = scopes[-1] if scopes else None
                if scope is not None and scope.entered and scope.phase == phase and scope.db is db:
                    rolled_back.append(_sync_probe(database, lock_path))
            event.listen(db.sync_session, 'after_rollback', rollback_finished)
            yield db
            scope = scopes[-1] if scopes else None
            if scope is None or not scope.entered or scope.phase != phase or scope.db is not db:
                return
            if failure == 'cancel':
                bindings.append((scope.witness.task_id, scope.witness.attempt_id, scope.witness.job_id))
                failures.append('cancel')
                asyncio.current_task().cancel()
                await asyncio.sleep(0)
            elif failure == 'commit':
                def fail_commit(_session):
                    bindings.append((scope.witness.task_id, scope.witness.attempt_id, scope.witness.job_id))
                    failures.append('commit')
                    raise BoardError('near_policy_changed', 'fixture failure during original commit')
                event.listen(db.sync_session, 'before_commit', fail_commit, once=True)

    monkeypatch.setattr(native, 'recheck_provider_contact', guard)
    monkeypatch.setattr(job_runtime, 'get_session', sessions)
    if failure == 'cancel':
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(_drive_until_marker(dispatcher, failures), 35)
    else:
        await asyncio.wait_for(_drive_until_marker(dispatcher, failures), 35)
    expected = 2 if phase == 'admit' and failure in {'guard', 'commit'} else 1
    assert failures == [failure] * expected
    assert rolled_back == ['busy'] * expected
    await _assert_original_binding(factory, task_id, bindings, expected)
    assert await _probe(database, lock_path) == 'acquired'
    async with factory.accounting_sessions() as db:
        runs = list((await db.scalars(select(WorkflowRunState))).all())
        assert all(run.status != 'succeeded' for run in runs)
        if phase != 'terminal':
            assert all(not run.artifact_receipts_json or run.artifact_receipts_json == '[]' for run in runs)
    output = await client.get(f'/api/work-board/tasks/{task_id}/near-text/output')
    assert output.status_code in {401, 403, 409}
    if phase in {'admit', 'queue', 'claim'}:
        assert controls['calls'] == []


@pytest.mark.asyncio
@pytest.mark.parametrize('case', ['missing', 'foreign_job', 'non_near', 'foreign_witness', 'wrong_phase', 'closed', 'reused'])
async def test_queue_scope_rejection_does_not_adopt_or_contact(actual_billing_journey, monkeypatch, case):
    client, factory, jobs, dispatcher, controls, _engine = actual_billing_journey
    task_id, _ = await create_actual_near_task(client, factory)
    original = jobs.queue_job
    checked = []

    async def queued(job_id, **kwargs):
        scope = kwargs['near_text_policy_scope']
        witness = kwargs['near_text_witness']
        before = await jobs.get_job(job_id)
        bad = dict(kwargs)
        target = job_id
        if case == 'missing':
            bad.pop('near_text_policy_scope')
        elif case in {'foreign_job', 'non_near'}:
            target = 'unrelated-native-job'
            async with factory.accounting_sessions() as db:
                run = await db.scalar(select(WorkflowRunState).where(WorkflowRunState.run_identity == job_id))
                assert run is not None
                values = run.model_dump()
                values.pop('id')
                values.update(run_identity=target, idempotency_binding='foreign-fixture-binding')
                if case == 'non_near':
                    values['job_kind'] = 'readonly_research_child'
                clone = WorkflowRunState(**values)
                assert clone.id != run.id and clone.run_identity != run.run_identity
                db.add(clone)
            before_target = await jobs.get_job(target)
            assert before_target is not None
        elif case == 'foreign_witness':
            bad['near_text_witness'] = replace(witness)
        elif case == 'wrong_phase':
            with native.native_policy_scope(witness, 'claim') as wrong:
                bad['near_text_policy_scope'] = wrong
                with pytest.raises(BoardError, match='original active transaction policy scope'):
                    await original(target, **bad)
            checked.append(case)
            assert await jobs.get_job(job_id) == before and controls['calls'] == []
            raise BoardError('near_policy_scope_invalid', 'fixture denied queue')
        elif case == 'closed':
            with native.native_policy_scope(witness, 'queue') as closed:
                pass
            bad['near_text_policy_scope'] = closed
        elif case == 'reused':
            await original(job_id, **kwargs)
            before = await jobs.get_job(job_id)
        with pytest.raises(BoardError) as rejected:
            await original(target, **bad)
        assert rejected.value.code == 'near_policy_scope_invalid'
        assert await jobs.get_job(job_id) == before
        if case in {'foreign_job', 'non_near'}:
            assert await jobs.get_job(target) == before_target
        assert controls['calls'] == []
        checked.append(case)
        raise BoardError('near_policy_scope_invalid', 'fixture denied queue')

    monkeypatch.setattr(jobs, 'queue_job', queued)
    await asyncio.wait_for(_drive_until_marker(dispatcher, checked), 35)
    assert checked == [case] and controls['calls'] == []
    async with factory.accounting_sessions() as db:
        runs = list((await db.scalars(select(WorkflowRunState))).all())
        assert runs and all(run.status != 'succeeded' for run in runs)


@pytest.mark.asyncio
async def test_real_admission_integrity_rollback_releases_before_reselect(actual_billing_journey, monkeypatch, scopes):
    client, factory, jobs, dispatcher, controls, engine = actual_billing_journey
    task_id, _ = await create_actual_near_task(client, factory)
    database, lock_path = _paths(engine)
    injected = []
    released = []

    def duplicate_goal(session, _context, _instances):
        scope = scopes[-1] if scopes else None
        if scope is not None and scope.entered and scope.phase == 'admit' and not injected:
            injected.append(True)
            original_goal = session.get(Goal, scope.witness.goal_id)
            assert original_goal is not None
            session.add(Goal(**original_goal.model_dump()))

    def reselected(_connection, _cursor, statement, _parameters, _context, _executemany):
        scope = scopes[-1] if scopes else None
        if injected and scope is not None and scope.phase == 'admit' and statement.lstrip().upper().startswith('SELECT') and 'workflow_run_states' in statement:
            assert scope.closed
            released.append(_sync_probe(database, lock_path))

    from sqlalchemy.orm import Session
    event.listen(Session, 'before_flush', duplicate_goal)
    event.listen(engine.sync_engine, 'before_cursor_execute', reselected)
    try:
        await asyncio.wait_for(drive(dispatcher), 35)
    finally:
        event.remove(Session, 'before_flush', duplicate_goal)
        event.remove(engine.sync_engine, 'before_cursor_execute', reselected)
    assert injected == [True] and released and set(released) == {'acquired'}
    assert controls['calls'] == []
    async with factory.accounting_sessions() as db:
        assert list((await db.scalars(select(WorkflowRunState))).all()) == []
    assert await _probe(database, lock_path) == 'acquired'


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['admit', 'queue', 'claim', 'terminal'])
async def test_foreign_process_policy_busy_has_no_phase_adoption(actual_billing_journey, monkeypatch, scopes, phase):
    from src.workspace.production import ProductionWorkspaceReconciliationError
    client, factory, jobs, dispatcher, controls, engine = actual_billing_journey
    task_id, _ = await create_actual_near_task(client, factory)
    database, lock_path = _paths(engine)
    method = {'admit': 'admit_job', 'queue': 'queue_job', 'claim': 'claim_job', 'terminal': 'transition_job'}[phase]
    original = getattr(jobs, method)
    denied = []
    bindings = []

    async def busy(*args, **kwargs):
        if not _selected(phase, args, kwargs):
            return await original(*args, **kwargs)
        job_id = args[0].identity.job_id if phase == 'admit' else args[0]
        before = await jobs.get_job(job_id)
        async with _child(database, lock_path, hold=True) as pipe:
            pipe.send('probe')
            assert await asyncio.wait_for(asyncio.to_thread(pipe.recv), 5) == 'acquired'
            with pytest.raises(ProductionWorkspaceReconciliationError, match='accounting continuity busy'):
                await original(*args, **kwargs)
            assert await jobs.get_job(job_id) == before
            scope = scopes[-1]
            assert scope.witness.job_id == job_id
            bindings.append((scope.witness.task_id, scope.witness.attempt_id, scope.witness.job_id))
            denied.append(phase)
        raise BoardError('near_policy_changed', 'fixture confirmed foreign contention')

    monkeypatch.setattr(jobs, method, busy)
    await asyncio.wait_for(_drive_until_marker(dispatcher, denied), 35)
    expected = 2 if phase == 'admit' else 1
    assert denied == [phase] * expected
    await _assert_original_binding(factory, task_id, bindings, expected)
    if phase != 'terminal':
        assert controls['calls'] == []
    assert await _probe(database, lock_path) == 'acquired'
    output = await client.get(f'/api/work-board/tasks/{task_id}/near-text/output')
    assert output.status_code in {401, 403, 409}
