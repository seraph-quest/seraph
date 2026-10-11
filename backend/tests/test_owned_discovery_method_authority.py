"""Owner boundary regressions; genuine wrapper journey is tested separately."""
import asyncio
import json
from types import SimpleNamespace, MappingProxyType

import pytest
from sqlalchemy import event

from src.guardian.goal_discovery import _OwnedDiscoverySession
from src.guardian.goal_programmes import (GoalProgrammeService, GoalProgrammeError,
    original_discovery_authority_numeric_schedule)
from src.memory import task_methods as methods
from src.work_board.contracts import WorkBoardOwner, TaskStrategyBinding
from src.work_board.repository import BoardError
from src.work_board.research_parent import DISCOVERY_SERVICE, DISCOVERY_CAPABILITY
from tests.test_task_lessons import failed_local_task, no_inference
from tests.test_programme_prospective_live_selection import (
    genuine_live_selection, private_outputs, SelectionObserved)
from tests.test_discovery_scheduler_invocation import registered_discovery
from tests.test_auth_session_composition_privacy import sql_state


def discovery_owner():
    return WorkBoardOwner(principal_id=DISCOVERY_SERVICE, session_id="")


def discovery_grant():
    # This caller-constructed metadata deliberately cannot confer authority.
    return SimpleNamespace(goal_id="goal", programme_id="programme", grant_revision=1,
        capability_id=DISCOVERY_CAPABILITY)


def test_pointer_lexical_preflight_accepts_maximum_original_closed_shape():
    # 128 astral scalars occupy 1,536 ASCII source bytes when escaped. This is
    # the original Id limit, including the longest permitted string encoding.
    value = "\U0001f600" * 128
    pointer = {"owner": {"identity_id": value, "issuer_principal_id": value,
        "issuer_root_id": value}, "scope": {"goal_id": value,
        "goal_revision": 2**63 - 1, "family": "research"}, "version": value,
        "digest": "a" * 64, "proposal_id": value}
    raw = json.dumps(pointer, ensure_ascii=True)
    methods._bounded_pointer_binding(raw)
    assert methods.ActiveMethodBinding.model_validate_json(raw).version == value
    methods._bounded_pointer_binding(" \nnull\t")


@pytest.mark.parametrize("raw", [
    "[" + ",".join("0" for _ in range(4096)) + "]",
    '{"owner":' + "[" * 1024 + "0" + "]" * 1024 + "}",
    '{"owner":{},"owner":{}}',
    '{"unexpected":"' + "x" * 65536 + '"}',
    'null null',
])
def test_pointer_lexical_preflight_rejects_amplifying_or_open_json_before_full_parse(monkeypatch, raw):
    original = json.loads
    parsed = []
    def tiny_key_only(value, *args, **kwargs):
        parsed.append(len(value))
        assert len(value) <= 386
        return original(value, *args, **kwargs)
    monkeypatch.setattr(methods.json, "loads", tiny_key_only)
    with pytest.raises(BoardError) as denied:
        methods._bounded_pointer_binding(raw)
    assert denied.value.code == "method_pointer_invalid"
    assert all(width <= 386 for width in parsed)


@pytest.mark.asyncio
async def test_lookalike_continuation_denied_before_policy_or_database(monkeypatch):
    reads = []
    def forbidden(*args, **kwargs):
        reads.append((args, kwargs))
        raise AssertionError("unowned authority must deny before a read")
    monkeypatch.setattr("src.guardian.goal_programmes._policy_binding", forbidden)
    monkeypatch.setattr(methods.database, "get_session", forbidden)
    fake = SimpleNamespace(db=object(), header_budget=object(),
        validate=lambda **kwargs: forbidden())
    programme = GoalProgrammeService()
    await programme.start()
    with pytest.raises(GoalProgrammeError, match="programme_original_selection_unavailable"):
        await programme.assert_authority(goal_id="goal", programme_id="programme",
            grant_revision=1, capability_id=DISCOVERY_CAPABILITY, discovery_session=fake)
    with pytest.raises(GoalProgrammeError, match="programme_original_selection_unavailable"):
        await programme.validate_current_binding(db=fake.db, binding=discovery_grant(),
            policy=object(), discovery_session=fake)
    with pytest.raises(GoalProgrammeError, match="programme_original_selection_unavailable"):
        await programme.prepay_discovery_binding_copy(fake)
    with pytest.raises(GoalProgrammeError, match="programme_original_selection_unavailable"):
        await original_discovery_authority_numeric_schedule(fake, constructor=MappingProxyType({}))
    with pytest.raises(BoardError):
        await methods.original_discovery_strategy_numeric_schedule(fake,
            constructor=MappingProxyType({}), strategy=TaskStrategyBinding(status="none", reason="baseline"))
    current = methods.CurrentMethod()
    current._started = True  # Negative owner-boundary unit test, no issued guard.
    denied = await current.resolve(discovery_owner(), "goal", DISCOVERY_CAPABILITY,
        discovery_grant(), discovery_session=fake)
    assert denied.status == "blocked" and denied.reason == "method_programme_service_mismatch"
    with pytest.raises(BoardError) as denied_pin:
        await current.validate_pinned(discovery_owner(), "goal", TaskStrategyBinding(status="none", reason="baseline"),
            discovery_grant(), db=fake.db, discovery_session=fake)
    assert denied_pin.value.code == "method_programme_service_mismatch"
    assert reads == []


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["inactive", "wrong_task", "copied"])
async def test_original_type_stale_or_detached_continuation_denied_before_reads(monkeypatch, mutation):
    reads = []
    def forbidden(*args, **kwargs):
        reads.append((args, kwargs))
        raise AssertionError("stale continuation must not read")
    monkeypatch.setattr("src.guardian.goal_programmes._policy_binding", forbidden)
    monkeypatch.setattr(methods.database, "get_session", forbidden)
    # These are rejected negative objects, never a fixture for genuine admission.
    stale = object.__new__(_OwnedDiscoverySession)
    stale.active = mutation != "inactive"
    stale.service = SimpleNamespace(_owned_discovery_session=stale)
    stale.task = object() if mutation == "wrong_task" else asyncio.current_task()
    if mutation == "copied":
        stale.service._owned_discovery_session = object()
    programme = GoalProgrammeService()
    await programme.start()
    with pytest.raises(RuntimeError, match="goal_discovery_session_unavailable"):
        await programme.assert_authority(goal_id="goal", programme_id="programme",
            grant_revision=1, capability_id=DISCOVERY_CAPABILITY, discovery_session=stale)
    with pytest.raises(RuntimeError, match="goal_discovery_session_unavailable"):
        await programme.validate_current_binding(db=object(), binding=discovery_grant(),
            policy=object(), discovery_session=stale)
    with pytest.raises(RuntimeError, match="goal_discovery_session_unavailable"):
        await programme.prepay_discovery_binding_copy(stale)
    current = methods.CurrentMethod()
    current._started = True
    with pytest.raises(RuntimeError, match="goal_discovery_session_unavailable"):
        await current.resolve(discovery_owner(), "goal", DISCOVERY_CAPABILITY,
            discovery_grant(), discovery_session=stale)
    assert reads == []


@pytest.mark.asyncio
@pytest.mark.parametrize("async_db", ["file"], indirect=True)
async def test_ordinary_canonical_owner_keeps_baseline_and_admitted_pin(async_db, monkeypatch, tmp_path, no_inference):
    operator, _ = await failed_local_task(async_db, monkeypatch, tmp_path)
    owner = WorkBoardOwner(principal_id=operator.principal.principal_id, session_id=operator.session_id)
    current = methods.CurrentMethod()
    await current.start()
    try:
        baseline = await current.resolve(owner, "goal", "work.general-task.v1")
        assert baseline.status == "none" and baseline.reason == "baseline"
        # Existing admitted baseline remains pin validation, not new selection.
        monkeypatch.setattr(methods, "_pointer", lambda *args: pytest.fail("admitted baseline must not select a pointer"))
        assert await current.validate_pinned(owner, "goal", baseline) == baseline
    finally:
        await current.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("pointer_exhaustion", [False, True])
async def test_real_registered_wrapper_current_authority_and_method_owner_share_session(
        genuine_live_selection, registered_discovery, monkeypatch, pointer_exhaustion):
    """Positive original-owner reads, ending before staging/insertion effects.

    This uses the real canonical grants, original native host/full guard and
    actual registered wrapper. No guard, certificate or owned session is faked.
    Root must separately release this test's native fixture before execution.
    """
    from src.guardian.goal_programmes import goal_programme_service
    from src.goals.contracts import GoalProgrammeAuthorityBinding
    case = genuine_live_selection
    _, _, wrapper = registered_discovery
    original = goal_programme_service.assert_authority
    before_db, before_files = sql_state(case), private_outputs(case)
    observed = []
    current = methods.current_method
    assert current._started

    async def actual_owner(**kwargs):
        owned = kwargs["discovery_session"]
        assert type(owned) is _OwnedDiscoverySession
        assert owned.validate() is owned
        assert owned.task is asyncio.current_task()
        db, frame = owned.db, owned.header_budget
        resolver = owned.service.strategy_resolver
        assert type(resolver) is methods.TaskMethodStrategyResolver
        assert resolver.current is current and current._started
        programme = await original(**kwargs)
        await goal_programme_service.prepay_discovery_binding_copy(owned)
        binding = GoalProgrammeAuthorityBinding.from_programme(programme, DISCOVERY_CAPABILITY)
        # Only prohibit opening an owner session after the authentic wrapper
        # issued this continuation. Both real owners must use the existing one.
        with monkeypatch.context() as local:
            local.setattr(goal_programme_service, "assert_authority", original)
            local.setattr(methods.database, "get_session",
                lambda *args, **kw: pytest.fail("owned method opened a second session"))
            queries = []
            def observe_sql(connection, cursor, statement, parameters, context, executemany):
                sql = " ".join(statement.lower().split())
                if (" from memory_proposals " in sql and "memory_proposals.schema_version =" in sql
                        and "memory_proposals.accepted_memory_id is not null" in sql):
                    queries.append("history")
                elif (sql.startswith("select case when typeof(id)")
                        and "from task_method_active where owner_identity_id=" in sql):
                    queries.append("pointer-scope")
                elif sql.startswith("select _rowid_ from task_method_active where goal_id="):
                    queries.append("foreign-absence")
            event.listen(case.engine.sync_engine, "before_cursor_execute", observe_sql)
            try:
                selected = await resolver.resolve(discovery_owner(), case.goal_id,
                    DISCOVERY_CAPABILITY, binding, discovery_session=owned)
                assert selected.status == "none" and selected.reason == "baseline"
                assert queries == ["history", "pointer-scope", "foreign-absence"]
                assert await resolver.validate_pinned(discovery_owner(), case.goal_id, selected,
                    binding, db=db, discovery_session=owned) == selected
                assert queries == ["history", "pointer-scope", "foreign-absence"]
            finally:
                event.remove(case.engine.sync_engine, "before_cursor_execute", observe_sql)
            # Authentic possession still cannot substitute caller numerical
            # bounds for the original constructor issued later by admission.
            remaining = frame.remaining
            with pytest.raises(GoalProgrammeError, match="programme_numeric_trace_changed"):
                await original_discovery_authority_numeric_schedule(owned,
                    constructor=MappingProxyType({"job_id": "forged"}))
            with pytest.raises(BoardError) as rejected_schedule:
                await methods.original_discovery_strategy_numeric_schedule(owned,
                    constructor=MappingProxyType({"job_id": "forged"}), strategy=selected)
            assert rejected_schedule.value.code == "method_original_session_unavailable"
            assert frame.remaining == remaining
            if pointer_exhaustion:
                from src.memory.header_bounds import HeaderBoundsError
                certificate = methods._owned_method_selection(owned).common33
                token = methods._DISCOVERY_READ.set((owned, certificate))
                try:
                    # Spend the actual issued frame without refund/reset. The
                    # malicious data object supplies no guard or certificate.
                    frame.debit(frame.remaining, appearance=("test-original-frame-exhaustion",))
                    local.setattr(methods, "_bounded_pointer_binding",
                        lambda *args: pytest.fail("exhausted frame parsed pointer JSON"))
                    with pytest.raises(HeaderBoundsError, match="canonical_bound_not_certified"):
                        methods._prepay_owned_pointer(SimpleNamespace(id="malicious-pointer-data",
                            binding_json="null", previous_binding_json="null",
                            owner_identity_id="identity", goal_id=case.goal_id, family="research"))
                    assert frame.remaining == 0
                finally:
                    methods._DISCOVERY_READ.reset(token)
        assert owned.db is db and owned.header_budget is frame
        observed.append((programme.id, selected.status))
        raise SelectionObserved("original owners read; no staging or insertion")

    monkeypatch.setattr(goal_programme_service, "assert_authority", actual_owner)
    await wrapper()
    assert observed == [(case.programme["id"], "none")]
    assert sql_state(case) == before_db and private_outputs(case) == before_files
    assert case.contacts == [] and current._started


@pytest.mark.asyncio
async def test_stopped_actual_method_owner_denies_before_database(genuine_live_selection, monkeypatch):
    from src.guardian.goal_discovery import goal_discovery_service
    case = genuine_live_selection
    current = methods.current_method
    resolver = goal_discovery_service.strategy_resolver
    assert type(resolver) is methods.TaskMethodStrategyResolver and resolver.current is current
    assert current._started
    before_db, before_files = sql_state(case), private_outputs(case)
    await current.stop()
    try:
        assert not current._started and current._key is None
        with monkeypatch.context() as local:
            local.setattr(methods.database, "get_session",
                lambda *args, **kwargs: pytest.fail("stopped method owner accessed database"))
            denied = await resolver.resolve(discovery_owner(), case.goal_id,
                DISCOVERY_CAPABILITY, discovery_grant())
            assert denied.status == "blocked" and denied.reason == "method_lifecycle_not_started"
            with pytest.raises(BoardError) as denied_pin:
                await resolver.validate_pinned(discovery_owner(), case.goal_id,
                    TaskStrategyBinding(status="none", reason="baseline"), discovery_grant())
            assert denied_pin.value.code == "method_lifecycle_not_started"
        assert sql_state(case) == before_db and private_outputs(case) == before_files
        assert case.contacts == []
    finally:
        # Restore through the original lifecycle API for the fixture's own
        # final unwind; no state field or signer is supplied by this test.
        await current.start()
        assert current._started


@pytest.mark.asyncio
async def test_real_registered_wrapper_authority_schedule_uses_issued_constructor_before_effects(
        genuine_live_selection, registered_discovery, monkeypatch):
    """Measure the real source schedule; the hook issues no reservation/permit.

    The original wrapper prepares its native guard and original admission
    constructor. We stop at the real reservation entry before staging. Root
    must release this native fixture separately from ordinary owner tests.
    """
    from src.workspace.accounting_witness import CompositionSessionGuard
    case = genuine_live_selection
    _, _, wrapper = registered_discovery
    before_db, before_files = sql_state(case), private_outputs(case)
    observed = []

    async def observe_schedule(guard, *, discovery_session, constructor, **kwargs):
        owned = discovery_session.validate()
        assert owned.guard is guard and owned._constructor_bounds is constructor
        assert owned._candidate[0] == constructor["job_id"]
        original_frame, original_db = owned.header_budget, owned.db
        entries = await original_discovery_authority_numeric_schedule(owned, constructor=constructor)
        admissions = tuple(entry for entry in entries if entry[0] == "admission")
        queues = tuple(entry for entry in entries if entry[0] == "queue")
        assert admissions and queues
        assert len(admissions) == 2 * len(queues)
        assert admissions[:len(queues)] == admissions[len(queues):]
        assert all(type(upper) is int and upper >= 0 for _, _, upper in entries)
        assert owned.header_budget is original_frame and owned.db is original_db
        observed.append(constructor["job_id"])
        raise SelectionObserved("real forecast read; no reservation or effects")

    monkeypatch.setattr(CompositionSessionGuard, "_reserve_original_discovery_admission", observe_schedule)
    await wrapper()
    assert len(observed) == 1
    assert sql_state(case) == before_db and private_outputs(case) == before_files
    assert case.contacts == []
