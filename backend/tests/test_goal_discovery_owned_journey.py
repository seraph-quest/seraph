"""Original registered-wrapper admission, paid files and persisted queue.

The genuine fixture issues Auth/Identity/Goal/programme and starts the reviewed
native host. The execution phase after admission is intercepted: this slice
does not contact providers or substitute a fixture for insertion/queue/readback.
"""
import asyncio

import pytest
from sqlalchemy import event

from tests.test_programme_prospective_live_selection import genuine_live_selection


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit", [False, True])
async def test_discovery_lifecycle_stopped_original_strategy_owner_denies_before_bind(monkeypatch, explicit):
    from types import SimpleNamespace
    from src.guardian import goal_discovery as discovery_module
    from src.memory.task_methods import current_method, TaskMethodStrategyResolver

    assert not current_method._started
    service = discovery_module.GoalDiscoveryService()
    previous = (service.jobs, service.strategy_resolver,
        service._lifecycle_dispatcher, service._lifecycle_host)
    dispatcher = SimpleNamespace(goal_discovery=None, jobs=service.jobs, general_tasks=None,
        strategy_resolver=TaskMethodStrategyResolver(current_method) if explicit else None)
    entered = []
    async def forbidden_start():
        entered.append(True)
        pytest.fail("stopped original strategy owner reached discovery start")
    monkeypatch.setattr(discovery_module, "goal_discovery_service", service)
    monkeypatch.setattr(service, "start", forbidden_start)
    with pytest.raises(RuntimeError, match="public discovery strategy lifecycle unavailable"):
        async with discovery_module.current_goal_discovery(dispatcher=dispatcher):
            pytest.fail("stopped original strategy owner bound discovery")
    assert entered == [] and dispatcher.goal_discovery is None
    assert (service.jobs, service.strategy_resolver,
        service._lifecycle_dispatcher, service._lifecycle_host) == previous


@pytest.mark.asyncio
@pytest.mark.parametrize("candidate_count", [1, 2])
async def test_registered_wrapper_original_admission_queue_and_files(
    genuine_live_selection, monkeypatch, candidate_count,
):
    from src.guardian.goal_discovery import goal_discovery_service, _OwnedDiscoverySession
    from src.scheduler import engine as scheduler_engine
    from src.guardian import goal_discovery as discovery_module
    from config.settings import settings

    case = genuine_live_selection
    service = goal_discovery_service
    if candidate_count == 2:
        from src.goals.repository import GoalRepository
        from src.goals.contracts import GoalProgrammeRequest, GoalProgrammeAccept
        from src.guardian.goal_programmes import goal_programme_service
        second = await GoalRepository().create("Second original owned discovery Goal",
            owner_principal_id=case.operator.principal.principal_id,
            owner_session_id=case.operator.session_id)
        requested = GoalProgrammeRequest(expected_goal_revision=second.revision,
            public_brief="Track another public release", budget={"max_inference_microusd": 1000})
        preview = await goal_programme_service.preview(operator=case.operator,
            goal_id=second.id, request=requested)
        accepted = await goal_programme_service.accept(operator=case.operator,
            goal_id=second.id, request=GoalProgrammeAccept(**requested.model_dump(),
                review_digest=preview["review_digest"], public_web_acknowledged=True,
                local_artifacts_acknowledged=True, inference_ceiling_acknowledged=True))
        assert accepted["state"] == "active"
    observations = []
    queued = []
    original_admit = service.admit
    original_physical = discovery_module.physical_discovery_inputs
    physical_observations = []
    from src.db import engine as database_engine
    original_exit = database_engine._exit_owned_discovery_writer
    publication_payments = []

    async def observe_original_exit(owned, *, phase, job_id):
        reservation = owned._numeric_reservation()
        result = await original_exit(owned, phase=phase, job_id=job_id)
        publication_payments.append((phase, job_id, tuple(reservation.consumed)))
        return result

    async def observe_physical(jobs, job_id, **kwargs):
        owned = kwargs["discovery_session"]
        owned.validate(jobs=jobs)
        witness = await original_physical(jobs, job_id, **kwargs)
        assert witness.job_id == job_id
        physical_observations.append((owned, job_id, witness))
        return witness

    async def observe_admit(**kwargs):
        owned = kwargs["discovery_session"]
        assert type(owned) is _OwnedDiscoverySession
        owned.validate(jobs=service.jobs)
        observations.append((owned, owned.db, owned.header_budget, asyncio.current_task(),
            owned.header_budget.remaining))
        # Possession of copied db/frame/marker fields cannot issue a continuation.
        copied = object.__new__(_OwnedDiscoverySession)
        copied.__dict__.update(owned.__dict__)
        with pytest.raises(RuntimeError, match="goal_discovery_session_unavailable"):
            copied.validate()
        result = await original_admit(**kwargs)
        assert result["status"] == "queued"
        # The queue's original physical reader delivered the actual files/pin
        # before CAS, while the candidate's paid continuation was live.
        assert any(owner is owned and job_id == result["job_id"]
            for owner, job_id, _ in physical_observations)
        queued.append(result["job_id"])
        return result

    async def observe_execution_boundary(job_id):
        assert job_id in queued
        assert service._owned_discovery_session is None
        assert asyncio.current_task() in service._tasks

    monkeypatch.setattr(service, "admit", observe_admit)
    monkeypatch.setattr(discovery_module, "physical_discovery_inputs", observe_physical)
    monkeypatch.setattr(database_engine, "_exit_owned_discovery_writer", observe_original_exit)
    monkeypatch.setattr(service, "run", observe_execution_boundary)
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    scheduler = scheduler_engine.init_scheduler()
    assert scheduler is not None
    scheduler.pause()
    try:
        registered = scheduler.get_job("goal_public_discovery")
        assert registered is not None
        await registered.func()
        assert len(queued) == candidate_count
        assert len(set(queued)) == candidate_count
        assert len(observations) == candidate_count
        owned, db, frame, task, _ = observations[0]
        for owner, original_db, original_frame, original_task, _ in observations:
            assert owner is owned and original_db is db
            assert original_frame is frame and original_task is task
        assert all(later[4] < earlier[4]
            for earlier, later in zip(observations, observations[1:]))
        assert task is asyncio.current_task()
        assert owned.db is db and owned.header_budget is frame
        assert frame.remaining < 1_048_576
        assert frame.physical_references
        assert len(publication_payments) == 2 * candidate_count
        for phase, job_id, payments in publication_payments:
            paid_phase = "publication-" + phase
            actual = [appearance for consumed_phase, appearance, upper, amount in payments
                if consumed_phase == paid_phase and amount > 0
                and appearance and appearance[0] == "programme-manifest"]
            # Observe the real original publisher's consumed reservations,
            # including separate JSON/UTF8 and effect-digest appearances.
            for label in ("files-sort", "files-json", "files-utf8", "files-update", "result"):
                assert ("programme-manifest", label) in actual
            for index in range(2):
                for label in ("file-dto", "effect-digest-json", "effect-digest-utf8"):
                    assert ("programme-manifest", label, job_id, index) in actual
        assert not owned.active
        assert service._owned_discovery_session is None
        assert not service._tasks
        assert service._active_discovery_invocation is None
        assert case.contacts == []
    finally:
        scheduler_engine.shutdown_scheduler()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["sqlalchemy", "os", "type"])
@pytest.mark.parametrize("failure_site", ["census", "admission", "session_exit"])
async def test_owned_pass_ordinary_failures_redact_and_unwind(
    genuine_live_selection, monkeypatch, caplog, failure_kind, failure_site,
):
    from contextlib import asynccontextmanager
    from sqlalchemy.exc import SQLAlchemyError
    from config.settings import settings
    from src.db import engine as database_engine
    from src.guardian import goal_discovery as discovery_module
    from src.scheduler import engine as scheduler_engine

    service = discovery_module.goal_discovery_service
    private_sentinel = "private-discovery-failure-sentinel"
    failure_type = {"sqlalchemy": SQLAlchemyError, "os": OSError, "type": TypeError}[failure_kind]
    observed, reached, effects = [], [], []
    original_session = database_engine.get_session
    staged_root = genuine_live_selection.workspace.host_root / "goal-programmes"
    before = {path.relative_to(staged_root): path.read_bytes()
        for path in staged_root.rglob("*") if path.is_file()}

    def observe_owner():
        owned = service._owned_discovery_session
        owned.validate(jobs=service.jobs)
        if not observed:
            observed.append(owned)

    def fail_census(goal):
        observe_owner()
        reached.append("census")
        if failure_site == "session_exit":
            # Exercise the genuine session owner's exception unwind first.
            raise ValueError("original-census-interruption")
        raise failure_type(private_sentinel)

    async def fail_admission(**kwargs):
        observe_owner()
        reached.append("admission")
        raise failure_type(private_sentinel)

    @asynccontextmanager
    async def fail_session_exit(*args, **kwargs):
        if kwargs.get("discovery_service") is not service:
            async with original_session(*args, **kwargs) as db:
                yield db
            return
        try:
            async with original_session(*args, **kwargs) as db:
                yield db
        finally:
            # The real owner has completed its rollback/cleanup. Its exit
            # failure must stay inside the owned pass's redaction boundary.
            reached.append("session_exit")
            raise failure_type(private_sentinel)

    async def forbidden_effect(*args, **kwargs):
        effects.append(True)
        pytest.fail("failed owned pass reached execution or queue")

    if failure_site in {"census", "session_exit"}:
        monkeypatch.setattr(discovery_module, "_load", fail_census)
    else:
        monkeypatch.setattr(service, "admit", fail_admission)
    if failure_site == "session_exit":
        monkeypatch.setattr(database_engine, "get_session", fail_session_exit)
    monkeypatch.setattr(service, "_queue", forbidden_effect)
    monkeypatch.setattr(service, "run", forbidden_effect)
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    scheduler = scheduler_engine.init_scheduler()
    scheduler.pause()
    caplog.clear()
    try:
        await scheduler.get_job("goal_public_discovery").func()
        assert reached == (["census", "session_exit"] if failure_site == "session_exit" else [failure_site])
        blocked = [record for record in caplog.records
            if record.name == "src.guardian.goal_discovery"]
        assert len(blocked) == 1
        assert blocked[0].getMessage() == "Goal discovery admission blocked"
        assert blocked[0].exc_info is None and blocked[0].stack_info is None
        assert private_sentinel not in caplog.text
        assert not any(record.exc_info or record.stack_info for record in caplog.records)
        assert len(observed) == 1 and not observed[0].active
        assert not observed[0].db.in_transaction()
        assert service._owned_discovery_session is None
        assert service._active_discovery_invocation is None and not service._tasks
        assert effects == [] and genuine_live_selection.contacts == []
        assert not [job for job in await service.jobs.list_jobs(limit=100)
            if job["job_kind"] == "goal_public_discovery_v1"]
        assert {path.relative_to(staged_root): path.read_bytes()
            for path in staged_root.rglob("*") if path.is_file()} == before
    finally:
        scheduler_engine.shutdown_scheduler()


@pytest.mark.asyncio
async def test_owned_pass_cancellation_propagates_after_real_unwind(
    genuine_live_selection, monkeypatch, caplog,
):
    from config.settings import settings
    from src.guardian.goal_discovery import goal_discovery_service
    from src.scheduler import engine as scheduler_engine

    service = goal_discovery_service
    observed, effects = [], []
    async def cancel_admission(**kwargs):
        owned = kwargs["discovery_session"]
        owned.validate(jobs=service.jobs)
        observed.append(owned)
        raise asyncio.CancelledError()

    async def forbidden_effect(*args, **kwargs):
        effects.append(True)
        pytest.fail("cancelled owned pass reached execution or queue")

    monkeypatch.setattr(service, "admit", cancel_admission)
    monkeypatch.setattr(service, "_queue", forbidden_effect)
    monkeypatch.setattr(service, "run", forbidden_effect)
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    scheduler = scheduler_engine.init_scheduler()
    scheduler.pause()
    caplog.clear()
    try:
        with pytest.raises(asyncio.CancelledError):
            await scheduler.get_job("goal_public_discovery").func()
        assert len(observed) == 1 and not observed[0].active
        assert not observed[0].db.in_transaction()
        assert service._owned_discovery_session is None
        assert service._active_discovery_invocation is None and not service._tasks
        assert effects == [] and genuine_live_selection.contacts == []
        assert not any(record.name == "src.guardian.goal_discovery"
            and record.getMessage() == "Goal discovery admission blocked" for record in caplog.records)
        assert not any(record.exc_info or record.stack_info for record in caplog.records)
    finally:
        scheduler_engine.shutdown_scheduler()


@pytest.mark.asyncio
async def test_unwrapped_tick_denies_before_any_database_read(
    genuine_live_selection,
):
    from src.guardian.goal_discovery import run_goal_discovery_tick

    statements = []
    def before(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = genuine_live_selection.engine.sync_engine
    event.listen(engine, "before_cursor_execute", before)
    try:
        with pytest.raises(RuntimeError, match="goal_discovery_invocation_unavailable"):
            await run_goal_discovery_tick()
        assert statements == []
        assert genuine_live_selection.contacts == []
    finally:
        event.remove(engine, "before_cursor_execute", before)


@pytest.mark.asyncio
async def test_exhausted_original_candidate_frame_stops_before_staging(
    genuine_live_selection, monkeypatch, caplog,
):
    from config.settings import settings
    from src.guardian.goal_discovery import goal_discovery_service
    from src.memory.header_bounds import HeaderBoundsError
    from src.scheduler import engine as scheduler_engine
    from src.workflows.job_runtime import DurableJobRepository

    service = goal_discovery_service
    original_admit = service.admit
    seen = []
    staged_root = genuine_live_selection.workspace.host_root / "goal-programmes"
    before = {path.relative_to(staged_root): path.read_bytes()
        for path in staged_root.rglob("*") if path.is_file()}

    async def exhaust(**kwargs):
        owned = kwargs["discovery_session"]
        owned.validate(jobs=service.jobs)
        seen.append(owned)
        # Exercise the original debit API; no reset, refund or counterfeit frame.
        owned.header_budget.debit(owned.header_budget.remaining)
        with pytest.raises(HeaderBoundsError):
            await original_admit(**kwargs)
        raise HeaderBoundsError("canonical_bound_not_certified")

    monkeypatch.setattr(service, "admit", exhaust)
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    scheduler = scheduler_engine.init_scheduler()
    scheduler.pause()
    try:
        await scheduler.get_job("goal_public_discovery").func()
        assert len(seen) == 1 and seen[0].header_budget.remaining == 0
        assert not seen[0].active
        after = {path.relative_to(staged_root): path.read_bytes()
            for path in staged_root.rglob("*") if path.is_file()}
        assert after == before
        assert not [job for job in await DurableJobRepository().list_jobs(limit=100)
            if job["job_kind"] == "goal_public_discovery_v1"]
        blocked = [record for record in caplog.records
            if record.name == "src.guardian.goal_discovery"
            and record.getMessage() == "Goal discovery admission blocked"]
        assert len(blocked) == 1
        assert service._owned_discovery_session is None
        assert service._active_discovery_invocation is None and not service._tasks
        assert genuine_live_selection.contacts == []
    finally:
        scheduler_engine.shutdown_scheduler()


@pytest.mark.asyncio
async def test_original_run_failure_preserves_registered_wrapper_task_until_finally(
    genuine_live_selection, monkeypatch,
):
    from config.settings import settings
    from src.approval.runtime import _get_current_discovery_invocation
    from src.guardian.goal_discovery import goal_discovery_service
    from src.scheduler import engine as scheduler_engine
    from src.workflows.job_runtime import DurableJobNotFound

    service = goal_discovery_service
    seen = []
    async def original_run_boundary():
        marker = _get_current_discovery_invocation(service)
        task = asyncio.current_task()
        assert task in service._tasks
        # A genuine original run fails its original missing-row physical reader;
        # its cleanup must preserve the outer registered wrapper membership.
        with pytest.raises(DurableJobNotFound):
            await service.run("goal-discovery:" + "0" * 32)
        assert task in service._tasks
        marker.validate(service=service)
        seen.append(task)

    monkeypatch.setattr(service, "tick", original_run_boundary)
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    scheduler = scheduler_engine.init_scheduler()
    scheduler.pause()
    try:
        await scheduler.get_job("goal_public_discovery").func()
        assert seen == [asyncio.current_task()]
        assert not service._tasks and service._active_discovery_invocation is None
        assert genuine_live_selection.contacts == []
    finally:
        scheduler_engine.shutdown_scheduler()


@pytest.mark.asyncio
async def test_existing_original_accepted_job_queues_without_restaging_or_repin(
    genuine_live_selection, monkeypatch,
):
    from config.settings import settings
    from src.guardian import goal_discovery as discovery_module
    from src.guardian.goal_discovery import goal_discovery_service
    from src.scheduler import engine as scheduler_engine
    from src.db import engine as database_engine

    class StopAfterOriginalAdmission(BaseException):
        """Test interruption after the actual A publish/commit, before B."""

    service = goal_discovery_service
    original_queue = service._queue
    admitted, completed, handles = [], [], []
    original_exit = database_engine._exit_owned_discovery_writer

    async def observe_exit(owned, *, phase, job_id):
        await original_exit(owned, phase=phase, job_id=job_id)
        completed.append((phase, job_id))

    async def stop_before_b(job_id, *, discovery_session=None):
        discovery_session.validate(jobs=service.jobs)
        assert discovery_session._candidate_phase == "inserted"
        assert completed == [("admission", job_id)]
        admitted.append(job_id)
        raise StopAfterOriginalAdmission()

    async def excluded_execution(job_id):
        assert job_id == admitted[0]
        assert service._owned_discovery_session is None

    def no_staging(*args, **kwargs):
        raise AssertionError("accepted original must not stage another artifact")

    async def no_current_selection(*args, **kwargs):
        raise AssertionError("accepted original must validate its pin, not resolve again")

    original_admit = service.admit
    async def observe_existing(**kwargs):
        owned = kwargs["discovery_session"]
        before_spend = owned.header_budget.remaining
        result = await original_admit(**kwargs)
        assert result["job_id"] == admitted[0] and result["status"] == "queued"
        assert owned._candidate is None and owned._candidate_phase is None
        assert owned.header_budget.remaining < before_spend
        handles.append(owned)
        return result

    monkeypatch.setattr(database_engine, "_exit_owned_discovery_writer", observe_exit)
    monkeypatch.setattr(service, "_queue", stop_before_b)
    monkeypatch.setattr(service, "run", excluded_execution)
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    scheduler = scheduler_engine.init_scheduler()
    scheduler.pause()
    try:
        registered = scheduler.get_job("goal_public_discovery")
        with pytest.raises(StopAfterOriginalAdmission):
            await registered.func()
        assert len(admitted) == 1
        before = await service.jobs.get_job(admitted[0])
        assert before["status"] == "accepted"
        staged_root = genuine_live_selection.workspace.host_root / "goal-programmes"
        files_before = {path.relative_to(staged_root): path.read_bytes()
            for path in staged_root.rglob("*") if path.is_file()}
        assert files_before
        completed.clear()
        monkeypatch.setattr(service, "_queue", original_queue)
        monkeypatch.setattr(service, "_strategy", no_current_selection)
        monkeypatch.setattr(service, "admit", observe_existing)
        monkeypatch.setattr(discovery_module, "stage_discovery_artifact", no_staging)
        await registered.func()
        after = await service.jobs.get_job(admitted[0])
        assert after["status"] == "queued"
        assert completed == [("queue", admitted[0])]
        for field in ("job_id", "declared_authority", "input_digest", "authority_digest",
                "budget_digest", "run_fingerprint", "idempotency", "artifacts", "effects", "checkpoints"):
            assert after[field] == before[field]
        assert {path.relative_to(staged_root): path.read_bytes()
            for path in staged_root.rglob("*") if path.is_file()} == files_before
        assert len(handles) == 1 and not handles[0].active
        assert service._owned_discovery_session is None
        assert service._active_discovery_invocation is None and not service._tasks
        assert genuine_live_selection.contacts == []
    finally:
        scheduler_engine.shutdown_scheduler()


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_status", ["queued", "cancelled"])
async def test_existing_queued_or_terminal_job_ends_only_paid_original_read(
    genuine_live_selection, monkeypatch, existing_status,
):
    from config.settings import settings
    from src.guardian import goal_discovery as discovery_module
    from src.guardian.goal_discovery import goal_discovery_service
    from src.scheduler import engine as scheduler_engine
    from src.db import engine as database_engine
    from src.workflows.research_guard import discovery_writer_scope
    from src.workflows.research_sources import physical_discovery_inputs
    from src.workspace.accounting_witness import CompositionSessionGuard

    service = goal_discovery_service
    original_admit = service.admit
    original_read_finish = CompositionSessionGuard._finish_owned_discovery_read
    original_exit = database_engine._exit_owned_discovery_writer
    observed, read_completions, writer_exits = [], [], []
    phase = "prepare"

    async def observe_admit(**kwargs):
        owned = kwargs["discovery_session"]
        result = await original_admit(**kwargs)
        observed.append((phase, result, owned))
        assert owned._candidate is None and owned._candidate_phase is None
        return result

    async def observe_read_finish(guard, discovery_session, job_id):
        assert phase == "read"
        discovery_session.validate(db=guard.db)
        assert discovery_session._candidate_phase == "reader"
        spent_before = discovery_session.header_budget.remaining
        await original_read_finish(guard, discovery_session, job_id)
        assert discovery_session._candidate is None
        assert discovery_session._candidate_phase is None
        assert discovery_session.header_budget.remaining < spent_before
        read_completions.append((job_id, discovery_session))

    async def observe_exit(owned, *, phase, job_id):
        await original_exit(owned, phase=phase, job_id=job_id)
        writer_exits.append((phase, job_id))

    async def excluded_execution(job_id):
        assert service._owned_discovery_session is None

    def no_staging(*args, **kwargs):
        raise AssertionError("persisted original read must not stage artifacts")

    async def no_queue(*args, **kwargs):
        raise AssertionError("queued/terminal original must not enter another writer")

    monkeypatch.setattr(service, "admit", observe_admit)
    monkeypatch.setattr(service, "run", excluded_execution)
    monkeypatch.setattr(database_engine, "_exit_owned_discovery_writer", observe_exit)
    monkeypatch.setattr(CompositionSessionGuard, "_finish_owned_discovery_read", observe_read_finish)
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    scheduler = scheduler_engine.init_scheduler()
    scheduler.pause()
    try:
        registered = scheduler.get_job("goal_public_discovery")
        await registered.func()
        assert len(observed) == 1 and observed[0][1]["status"] == "queued"
        job_id = observed[0][1]["job_id"]
        assert writer_exits == [("admission", job_id), ("queue", job_id)]
        if existing_status == "cancelled":
            witness = await physical_discovery_inputs(service.jobs, job_id)
            async with discovery_writer_scope(witness=witness):
                cancelled = await service.jobs.cancel_job(job_id)
            assert cancelled["status"] == "cancelled"
        before = await service.jobs.get_job(job_id)
        assert before["status"] == existing_status
        staged_root = genuine_live_selection.workspace.host_root / "goal-programmes"
        files_before = {path.relative_to(staged_root): path.read_bytes()
            for path in staged_root.rglob("*") if path.is_file()}
        phase = "read"
        writer_exits.clear()
        monkeypatch.setattr(service, "_queue", no_queue)
        monkeypatch.setattr(discovery_module, "stage_discovery_artifact", no_staging)
        await registered.func()
        assert len(observed) == 2 and observed[-1][1] == before
        assert len(read_completions) == 1 and read_completions[0][0] == job_id
        assert read_completions[0][1] is observed[-1][2]
        assert writer_exits == []
        after = await service.jobs.get_job(job_id)
        assert after == before
        assert {path.relative_to(staged_root): path.read_bytes()
            for path in staged_root.rglob("*") if path.is_file()} == files_before
        assert not observed[-1][2].active
        assert service._owned_discovery_session is None
        assert service._active_discovery_invocation is None and not service._tasks
        assert genuine_live_selection.contacts == []
    finally:
        scheduler_engine.shutdown_scheduler()
