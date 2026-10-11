"""Original registered wrapper provenance; no scheduler-trigger or grant claim."""
import asyncio
import copy

import pytest
import pytest_asyncio

from config.settings import settings
from src.approval import runtime
from src.guardian import goal_discovery as discovery
from src.scheduler import engine
from tests.test_programme_prospective_live_selection import genuine_live_selection


@pytest_asyncio.fixture
async def registered_discovery(genuine_live_selection, monkeypatch):
    # The genuine existing lifecycle fixture authenticates host/jobs/service.
    # Actual init_scheduler registers the actual callback/wrapper. Do not start
    # its timer: direct invocation proves registration, not APScheduler delivery.
    monkeypatch.setattr(settings, "scheduler_enabled", True)
    monkeypatch.setattr(engine, "_scheduler", None)
    monkeypatch.setattr(engine, "_scheduler_loop", None)
    monkeypatch.setattr(engine.AsyncIOScheduler, "start", lambda self: None)
    scheduler = engine.init_scheduler()
    registration = scheduler.get_job("goal_public_discovery")
    assert registration is not None
    assert discovery.goal_discovery_service._tasks == set()
    yield scheduler, registration, registration.func
    assert discovery.goal_discovery_service._tasks == set()
    assert discovery.goal_discovery_service._active_discovery_invocation is None
    assert runtime._current_discovery_invocation.get() is None


@pytest.mark.asyncio
async def test_registered_wrapper_consumes_once_and_closes_exact_lifetime(
        registered_discovery, monkeypatch):
    _scheduler, _registration, wrapper = registered_discovery
    service = discovery.goal_discovery_service
    saved = []
    previous = runtime.get_current_trust_principal()

    async def observe_tick():
        marker = runtime._get_current_discovery_invocation(service, consume=True)
        saved.append(marker)
        assert marker.validate(service=service) is marker
        assert asyncio.current_task() in service._tasks
        principal = runtime.get_current_trust_principal()
        assert principal.grants == () and principal is marker._bindings[6]
        with pytest.raises(RuntimeError, match="goal_discovery_invocation_consumed"):
            runtime._get_current_discovery_invocation(service, consume=True)

    monkeypatch.setattr(service, "tick", observe_tick)
    await wrapper()
    assert runtime.get_current_trust_principal() is previous
    with pytest.raises(RuntimeError, match="goal_discovery_invocation_unavailable"):
        saved[0].validate(service=service)
    # An unwrapped original callback cannot establish candidate ownership.
    with pytest.raises(RuntimeError, match="goal_discovery_invocation_unavailable"):
        await discovery.run_goal_discovery_tick()


@pytest.mark.parametrize("mutation", ["context-set", "context-reset", "principal-set", "principal-reset", "copied-principal"])
@pytest.mark.asyncio
async def test_every_original_principal_mutator_invalidates_even_same_object(
        registered_discovery, monkeypatch, mutation):
    _scheduler, _registration, wrapper = registered_discovery
    service = discovery.goal_discovery_service

    async def observe_tick():
        principal = runtime.get_current_trust_principal()
        # A token created before consuming has already invalidated this pass;
        # reset cases capture a token before the wrapper entry instead below.
        marker = runtime._get_current_discovery_invocation(service, consume=True)
        if mutation == "context-set":
            token = runtime.set_runtime_context(None, "high_risk", trust_principal=principal)
            runtime.reset_runtime_context(token)
        elif mutation in {"principal-set", "copied-principal"}:
            supplied = copy.copy(principal) if mutation == "copied-principal" else principal
            if mutation == "copied-principal":
                assert supplied == principal and supplied is not principal
            token = runtime.set_runtime_trust_principal(supplied)
            runtime.reset_runtime_trust_principal(token)
        elif mutation == "context-reset":
            runtime.reset_runtime_context(outer_token)
        else:
            runtime.reset_runtime_trust_principal(outer_token)
        with pytest.raises(RuntimeError, match="goal_discovery_invocation_unavailable"):
            marker.validate(service=service)
        restore = runtime.set_runtime_trust_principal(principal)
        with pytest.raises(RuntimeError, match="goal_discovery_invocation_unavailable"):
            marker.validate(service=service)
        runtime.reset_runtime_trust_principal(restore)

    previous = runtime.get_current_trust_principal()
    outer_token = (runtime.set_runtime_context(None, "high_risk", trust_principal=previous)
        if mutation == "context-reset" else runtime.set_runtime_trust_principal(previous)
        if mutation == "principal-reset" else None)
    monkeypatch.setattr(service, "tick", observe_tick)
    try:
        await wrapper()
    finally:
        # Reset tokens were consumed by the intended mutation; normalize only
        # after the original wrapper cleanup, without resurrecting its marker.
        if mutation in {"context-reset", "principal-reset"}:
            runtime.set_runtime_trust_principal(previous)


@pytest.mark.parametrize("damage", ["lookalike", "rebound-registration-callable", "missing-registration", "replaced-registration"])
@pytest.mark.asyncio
async def test_wrapper_registration_denials_precede_principal_and_callback(
        registered_discovery, monkeypatch, damage):
    scheduler, registration, wrapper = registered_discovery
    calls = []
    monkeypatch.setattr(discovery.goal_discovery_service, "tick", lambda: calls.append("candidate"))
    original_set = engine.set_runtime_context

    def observe_set(*args, **kwargs):
        calls.append("principal")
        return original_set(*args, **kwargs)

    monkeypatch.setattr(engine, "set_runtime_context", observe_set)
    if damage == "lookalike":
        target = engine._async_job_wrapper(discovery.run_goal_discovery_tick,
            asyncio.get_running_loop(), job_id="goal_public_discovery")
        # Even manually registering this reconstructed wrapper cannot mint
        # provenance from the original init_scheduler registration issuer.
        registration.func = target
    elif damage == "rebound-registration-callable":
        # Root rejected the newly proposed FunctionType(private-code/closure)
        # oracle as arbitrary same-process code execution outside the accepted
        # contract. This checks ordinary callable replacement instead.
        async def replacement():
            await wrapper()
        registration.func = replacement
        target = wrapper
    else:
        target = wrapper
        scheduler.remove_job("goal_public_discovery")
        if damage == "replaced-registration":
            scheduler.add_job(wrapper, id="goal_public_discovery")
            assert scheduler.get_job("goal_public_discovery") is not registration
    with pytest.raises(RuntimeError, match="goal_discovery_invocation_unavailable"):
        await target()
    assert calls == []


@pytest.mark.asyncio
async def test_inherited_child_and_reentry_preserve_consumed_outer_owner(
        registered_discovery, monkeypatch):
    _scheduler, _registration, wrapper = registered_discovery
    service = discovery.goal_discovery_service

    async def observe_tick():
        marker = runtime._get_current_discovery_invocation(service, consume=True)
        principal = runtime.get_current_trust_principal()
        owner = asyncio.current_task()
        with pytest.raises(RuntimeError, match="goal_discovery_invocation_unavailable"):
            await wrapper()
        async def child():
            with pytest.raises(RuntimeError, match="goal_discovery_invocation_unavailable"):
                marker.validate(service=service)
            with pytest.raises(RuntimeError, match="goal_discovery_invocation_unavailable"):
                await wrapper()
        await asyncio.create_task(child())
        assert marker.validate(service=service) is marker
        assert runtime.get_current_trust_principal() is principal
        assert service._tasks == {owner}
        with pytest.raises((RuntimeError, AttributeError)):
            copy.copy(marker).validate(service=service)

    monkeypatch.setattr(service, "tick", observe_tick)
    await wrapper()


@pytest.mark.parametrize("exit_kind", ["success", "error", "cancel"])
@pytest.mark.asyncio
async def test_concurrent_second_wrapper_denies_without_mutating_first_and_cleans_exit(
        registered_discovery, monkeypatch, exit_kind):
    _scheduler, _registration, wrapper = registered_discovery
    service = discovery.goal_discovery_service
    entered, release = asyncio.Event(), asyncio.Event()
    saved = []
    principal_sets = []
    original_set = engine.set_runtime_context

    def observe_set(*args, **kwargs):
        principal_sets.append(asyncio.current_task())
        return original_set(*args, **kwargs)

    monkeypatch.setattr(engine, "set_runtime_context", observe_set)
    async def observe_tick():
        marker = runtime._get_current_discovery_invocation(service, consume=True)
        saved.append((marker, runtime.get_current_trust_principal(), asyncio.current_task()))
        entered.set()
        await release.wait()
        marker.validate(service=service)
        if exit_kind == "error":
            raise RuntimeError("observed_callback_failure")

    monkeypatch.setattr(service, "tick", observe_tick)
    first = asyncio.create_task(wrapper())
    try:
        await entered.wait()
        with pytest.raises(RuntimeError, match="goal_discovery_invocation_unavailable"):
            await wrapper()
        assert principal_sets == [first]
        assert service._tasks == {first}
        assert service._active_discovery_invocation is saved[0][0]
        assert saved[0][0]._live and saved[0][0]._consumed
        if exit_kind == "cancel":
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
        else:
            release.set()
            if exit_kind == "error":
                with pytest.raises(RuntimeError, match="observed_callback_failure"):
                    await first
            else:
                await first
        assert service._tasks == set() and not saved[0][0]._live
        if exit_kind == "success":
            await wrapper()
            assert len(saved) == 2 and not saved[1][0]._live
    finally:
        release.set()
        if not first.done():
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
async def test_service_stop_cancels_registered_owner_without_readding_membership(
        registered_discovery, monkeypatch):
    _scheduler, _registration, wrapper = registered_discovery
    service = discovery.goal_discovery_service
    entered = asyncio.Event()
    marker = []

    async def observe_tick():
        marker.append(runtime._get_current_discovery_invocation(service, consume=True))
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(service, "tick", observe_tick)
    task = asyncio.create_task(wrapper())
    try:
        await entered.wait()
        await service.stop()
        assert task.cancelled() and service._tasks == set()
        assert not marker[0]._live and service._active_discovery_invocation is None
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
