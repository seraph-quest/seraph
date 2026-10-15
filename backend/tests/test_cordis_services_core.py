"""#1007 acceptance: Cordis services over canonical Seraph owners.

Real SQLite file, real DurableJobRepository and (where available) the real
reviewed Cordis child over owned pipes. All external transport is denied:
every socket connect/DNS lookup in this process raises, and the child gets no
PATH/HOME/credentials and speaks only over stdio pipes.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import threading

import pytest
import pytest_asyncio
from sqlalchemy import delete, event, func, select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlmodel import SQLModel

from config.settings import settings
from src.db.engine import get_session, override_session_factory
from src.db.models import AuditEvent, Goal, Memory, OperatorSession, RuntimeCompositionState, WorkflowRunState
from src.runtime_plugins.contracts import METHOD_DOMAINS, validate_result
from src.runtime_plugins.dispatch import (
    BLOCKED_METHODS, SUPPORTED_METHODS, CalledServiceInvocation, NativeServiceBlocked, NativeServiceDispatcher,
    OriginalServiceInvocation)
from src.runtime_plugins.ownership import (
    DOMAINS, CompositionBindingError, CompositionDependency, bind_invocation, composition_writer,
    legacy_composition_digest, seed_legacy_inventory_sync, transition_owner, validate_invocation)
from src.runtime_plugins.read_admission import native_capability_inventory
from src.runtime_plugins.service_jobs import (
    admit_service_job, claim_service_job, execute_native_read, open_invocation, verified_task_call)
from src.workflows.job_runtime import DurableJobLeaseError, DurableJobRepository
from src.workspace.lifecycle import lifecycle_fence_marker

NODE = os.environ.get("SERAPH_CORDIS_TEST_NODE")
PACKAGE = Path(__file__).resolve().parents[2] / "runtime" / "cordis"
needs_host = pytest.mark.skipif(
    not NODE or not Path(NODE).is_file() or not (PACKAGE / "node_modules/cordis").is_dir()
    or not (PACKAGE / "dist/build-manifest.json").is_file(),
    reason="reviewed Cordis test node/package not built (set SERAPH_CORDIS_TEST_NODE)")


def _deny_external_transport(monkeypatch):
    def denied(*_args, **_kwargs):
        raise RuntimeError("external transport denied in #1007 acceptance tests")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


def _engine(path: Path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")

    @event.listens_for(engine.sync_engine, "connect")
    def configure(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()
    return engine


@pytest_asyncio.fixture
async def service_db(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(settings, "workspace_dir", str(root))
    monkeypatch.setattr(settings, "operator_auth_allow_unauthenticated_tests", False)
    monkeypatch.setattr(settings, "operator_auth_secret", "isolated-1007-core")
    monkeypatch.setattr(settings, "operator_auth_secret_hash", "")
    _deny_external_transport(monkeypatch)
    db_path = root / "seraph.db"
    engine = _engine(db_path)
    async with engine.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)
        assert await connection.run_sync(seed_legacy_inventory_sync) == len(DOMAINS)
        # A non-empty inventory is never reseeded or repaired.
        assert await connection.run_sync(seed_legacy_inventory_sync) == 0
    factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    with override_session_factory(factory):
        yield db_path
    await engine.dispose()


async def _operator():
    from src.auth.service import create_session
    _, operator = await create_session()
    return operator


async def _goal(operator, *, goal_id="goal-1007", description="private goal plan"):
    async with get_session() as db:
        db.add(Goal(id=goal_id, title="Ship #1007", description=description, revision=1, status="active",
                    owner_principal_id=operator.principal.principal_id, owner_session_id=operator.session_id))
    return goal_id


async def _row(domain):
    async with get_session() as db:
        row = await db.get(RuntimeCompositionState, domain, populate_existing=True)
        return CompositionDependency(row.runtime_domain, row.owner_kind, row.epoch, row.composition_digest), row.state


# ------------------------------------------------------------------ ownership

@pytest.mark.asyncio
async def test_fixed_inventory_binding_and_missing_dependency_denies_admission(service_db):
    async with get_session() as db:
        rows = (await db.execute(select(RuntimeCompositionState))).scalars().all()
        assert {row.runtime_domain for row in rows} == set(DOMAINS)
        assert all(row.owner_kind == "legacy" and row.epoch == 1 and row.state == "ready" and
                   row.composition_digest == legacy_composition_digest(row.runtime_domain) for row in rows)
        binding = await bind_invocation(db, method="tasks.admit", native_branch="workflow", goal_bound=True)
        assert [item.runtime_domain for item in binding.dependency_vector] == [
            "seraph.authority.v1", "seraph.goals.v1", "seraph.tasks.v1"]
        assert binding.allowed_child_methods == ("authority.resolve", "goals.read", "tasks.admit",
                                                 "tasks.cancel", "tasks.checkpoint", "tasks.inspect")
        await db.execute(delete(RuntimeCompositionState).where(RuntimeCompositionState.runtime_domain == "seraph.goals.v1"))
        with pytest.raises(CompositionBindingError) as missing:
            await validate_invocation(db, binding)
        assert missing.value.reason_code == "composition_dependency_missing"
        with pytest.raises(CompositionBindingError):
            await bind_invocation(db, method="tasks.admit", native_branch="workflow", goal_bound=True)
        await db.rollback()


@pytest.mark.asyncio
async def test_cutover_rules_epochs_only_increase_and_require_fence(service_db):
    domain = "seraph.memory.v1"
    legacy, _ = await _row(domain)
    cordis = "c" * 64
    async with get_session() as db:
        with pytest.raises(CompositionBindingError) as unfenced:
            async with composition_writer(db):
                pass
        assert unfenced.value.reason_code == "composition_maintenance_fence_required"
    with lifecycle_fence_marker():
        async with get_session() as db:
            async with composition_writer(db):
                # Owner change at the same epoch and a direct ready->blocked jump are refused.
                for kwargs in ({"owner_kind": "cordis", "epoch": 1, "composition_digest": cordis, "state": "blocked",
                                "expected_state": "ready"},
                               {"owner_kind": "cordis", "epoch": 2, "composition_digest": cordis, "state": "blocked",
                                "expected_state": "ready"},
                               {"owner_kind": "legacy", "epoch": 1, "composition_digest": legacy.composition_digest,
                                "state": "ready", "expected_state": "ready"}):
                    with pytest.raises(CompositionBindingError):
                        await transition_owner(db, runtime_domain=domain, expected=legacy, **kwargs)
                await transition_owner(db, runtime_domain=domain, expected=legacy, expected_state="ready",
                                       owner_kind="legacy", epoch=1, composition_digest=legacy.composition_digest,
                                       state="draining")
                await transition_owner(db, runtime_domain=domain, expected=legacy, expected_state="draining",
                                       owner_kind="cordis", epoch=2, composition_digest=cordis, state="blocked")
                target = CompositionDependency(domain, "cordis", 2, cordis)
                # Ready needs a boot proof: the admitting host's verified
                # plugin digest for exactly this domain.
                for proof in (None, _boot_host({domain: "d" * 64}), _boot_host({domain: cordis}, admitting=False),
                              _boot_host({domain: cordis}, plugin_state="blocked")):
                    with pytest.raises(CompositionBindingError):
                        await transition_owner(db, runtime_domain=domain, expected=target, expected_state="blocked",
                                               owner_kind="cordis", epoch=2, composition_digest=cordis,
                                               state="ready", boot_host=proof)
                await transition_owner(db, runtime_domain=domain, expected=target, expected_state="blocked",
                                       owner_kind="cordis", epoch=2, composition_digest=cordis, state="ready",
                                       boot_host=_boot_host({domain: cordis}))
                with pytest.raises(CompositionBindingError):  # epochs never decrease
                    await transition_owner(db, runtime_domain=domain, expected=target, expected_state="ready",
                                           owner_kind="legacy", epoch=1, composition_digest=legacy.composition_digest,
                                           state="blocked")
                # Rollback is a new, higher epoch.
                await transition_owner(db, runtime_domain=domain, expected=target, expected_state="ready",
                                       owner_kind="cordis", epoch=2, composition_digest=cordis, state="draining")
                await transition_owner(db, runtime_domain=domain, expected=target, expected_state="draining",
                                       owner_kind="legacy", epoch=3, composition_digest=legacy.composition_digest,
                                       state="blocked")
    current, state = await _row(domain)
    assert (current.owner_kind, current.epoch, state) == ("legacy", 3, "blocked")
    async with get_session() as db:
        proofs = [json.loads(event.details_json) for event in (await db.execute(
            select(AuditEvent).where(AuditEvent.event_type == "runtime_composition_recovery"))).scalars()]
    assert [proof["phase"] for proof in proofs].count("awaiting_boot") == 2
    assert all(proof["runtime_domain"] == domain for proof in proofs)


def _boot_host(digests, *, admitting=True, plugin_state="ready"):
    """Reviewed-host stand-in for the pure ownership rule (no child process)."""
    from types import SimpleNamespace
    from src.runtime_plugins.bridge import CordisHost
    from src.runtime_plugins.composition import ReviewedComposition
    host = CordisHost()
    host.reviewed = ReviewedComposition(Path("/reviewed"), Path("/reviewed/node"), "v24.13.1", {}, "b" * 64,
                                        "c" * 64, dict(digests))
    host.state, host._cleanup_state = ("ready" if admitting else "blocked"), "pending"
    host.process = SimpleNamespace(returncode=None)
    host._plugins = [{"id": domain, "state": plugin_state, "reason": None, "digest": digest}
                     for domain, digest in digests.items()]
    return host


def _race(db_path: Path, digest: str, barrier: threading.Barrier, outcomes: list):
    async def run():
        engine = _engine(db_path)
        factory = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        try:
            with override_session_factory(factory), lifecycle_fence_marker():
                async with get_session() as db:
                    barrier.wait(timeout=10)
                    async with composition_writer(db):
                        expected = CompositionDependency("seraph.tasks.v1", "legacy", 1,
                                                         legacy_composition_digest("seraph.tasks.v1"))
                        await transition_owner(db, runtime_domain="seraph.tasks.v1", expected=expected,
                                               expected_state="draining", owner_kind="cordis", epoch=2,
                                               composition_digest=digest, state="blocked")
            outcomes.append(("won", digest))
        except CompositionBindingError as exc:
            outcomes.append(("lost", exc.reason_code))
        finally:
            await engine.dispose()
    asyncio.run(run())


@pytest.mark.asyncio
async def test_racing_cutovers_on_two_connections_yield_exactly_one_owner(service_db):
    with lifecycle_fence_marker():
        async with get_session() as db:
            async with composition_writer(db):
                expected = CompositionDependency("seraph.tasks.v1", "legacy", 1,
                                                 legacy_composition_digest("seraph.tasks.v1"))
                await transition_owner(db, runtime_domain="seraph.tasks.v1", expected=expected,
                                       expected_state="ready", owner_kind="legacy", epoch=1,
                                       composition_digest=expected.composition_digest, state="draining")
    for _round in range(3):
        barrier, outcomes = threading.Barrier(2), []
        threads = [threading.Thread(target=_race, args=(service_db, digest, barrier, outcomes))
                   for digest in ("a" * 64, "b" * 64)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        winners = [item for item in outcomes if item[0] == "won"]
        if _round == 0:
            assert len(outcomes) == 2 and len(winners) == 1, outcomes
            assert [item for item in outcomes if item[0] == "lost"] == [("lost", "composition_owner_changed")]
            winner_digest = winners[0][1]
        else:  # Later racers against the stale expectation all lose.
            assert winners == [] and len(outcomes) == 2
    current, state = await _row("seraph.tasks.v1")
    assert (current.owner_kind, current.epoch, current.composition_digest, state) == ("cordis", 2, winner_digest, "blocked")
    async with get_session() as db:
        count = await db.scalar(select(func.count()).select_from(AuditEvent).where(
            AuditEvent.event_type == "runtime_composition_recovery"))
    assert count == 2  # draining + the single winning epoch change


# ------------------------------------------------------------------ admission

@pytest.mark.asyncio
async def test_idempotent_admission_reuses_one_job_even_when_concurrent(service_db):
    operator = await _operator()
    candidate = {"schema_version": 1, "checkpoint_refs": ["step-1"]}
    kwargs = dict(operator=operator, origin_method="tasks.admit", native_branch="workflow", candidate=candidate,
                  idempotency_key="admit-once", reviewed_composition=None)
    (first, first_replayed), (second, second_replayed) = await asyncio.gather(
        admit_service_job(**kwargs), admit_service_job(**kwargs))
    third, third_replayed = await admit_service_job(**kwargs)
    assert first["job_id"] == second["job_id"] == third["job_id"]
    assert sorted([first_replayed, second_replayed]) == [False, True] and third_replayed is True
    async with get_session() as db:
        rows = (await db.execute(select(WorkflowRunState).where(
            WorkflowRunState.idempotency_key == "admit-once"))).scalars().all()
    assert len(rows) == 1 and rows[0].composition_binding_json is not None


@pytest.mark.asyncio
async def test_repository_cas_repeats_composition_fence(service_db):
    """Even a caller bypassing the dispatcher cannot checkpoint after an owner change."""
    operator = await _operator()
    jobs = DurableJobRepository()
    job, _ = await admit_service_job(operator=operator, origin_method="tasks.admit", native_branch="workflow",
                                     candidate={"schema_version": 1, "checkpoint_refs": []},
                                     idempotency_key="cas-fence", reviewed_composition=None, jobs=jobs)
    from types import SimpleNamespace
    owner, fence = await claim_service_job(job, host=SimpleNamespace(boot_nonce="e" * 64), jobs=jobs)
    await jobs.record_checkpoint(job["job_id"], checkpoint_id="before", state={"ok": True}, owner=owner,
                                 fencing_token=fence)
    async with get_session() as db:
        await db.execute(update(RuntimeCompositionState).where(
            RuntimeCompositionState.runtime_domain == "seraph.tasks.v1").values(epoch=2))
    with pytest.raises(DurableJobLeaseError):
        await jobs.record_checkpoint(job["job_id"], checkpoint_id="after", state={"ok": True}, owner=owner,
                                     fencing_token=fence)
    stored = await jobs.get_job(job["job_id"])
    assert [item["checkpoint_id"] for item in stored["checkpoints"]] == ["before"]


@pytest.mark.asyncio
async def test_blocked_methods_are_typed_and_visible_never_silent(service_db):
    assert SUPPORTED_METHODS | set(BLOCKED_METHODS) == set(METHOD_DOMAINS) and len(METHOD_DOMAINS) == 34
    assert BLOCKED_METHODS["research.buildPlan"] == "native_research_plan_not_bound"
    dispatcher = NativeServiceDispatcher()
    for method, reason in BLOCKED_METHODS.items():
        async with get_session() as db:
            binding = await bind_invocation(db, method=method, native_branch="base")
        witness = {"invocation_ref": "job-x", "package_digest": "a" * 64, "host_composition_digest": "b" * 64,
                   "original_deadline_at": 2_000_000_000_000}
        scope = OriginalServiceInvocation(witness, binding, "c" * 64)
        called = CalledServiceInvocation(scope, method, binding.epoch_for(method), 1_900_000_000_000, "r-1", "c" * 64)
        frame = {"method": method, "payload": _payload(method), "invocation_ref": "job-x",
                 "composition_epoch": binding.epoch_for(method), "boot_nonce": "c" * 64,
                 "package_digest": "a" * 64, "composition_digest": "b" * 64, "deadline_at": 1_900_000_000_000}
        result = await dispatcher.dispatch(frame, called_scope=called)
        assert result == {"status": "blocked", "reason_code": reason, "memory_status": "no_learning"}
        validate_result(method, result)
        # A forged frame identity is refused before the method inventory.
        forged = await dispatcher.dispatch({**frame, "invocation_ref": "job-y"}, called_scope=called)
        assert forged["reason_code"] == "native_original_frame_changed"


def _payload(method):
    values = {
        "authority.resolve": {}, "goals.read": {}, "tasks.admit": {"request_ref": "r"}, "tasks.inspect": {"job_ref": "r"},
        "tasks.cancel": {"job_ref": "r", "expected_revision": 1}, "tasks.checkpoint": {"checkpoint_ref": "r"},
        "tasks.settle": {"outcome_ref": "r"}, "capabilities.list": {"cursor": None, "limit": 1},
        "capabilities.describe": {"capability_id": "r"}, "capabilities.invoke": {"request_ref": "r"},
        "inference.request": {"request_ref": "r"}, "memory.retrieve": {"query_ref": "r", "limit": 1},
        "memory.propose": {"request_ref": "r"}, "memory.applyReviewed": {"review_ref": "r"},
        "memory.forget": {"request_ref": "r"}, "artifacts.read": {"artifact_ref": "r", "max_bytes": 1},
        "artifacts.stage": {"request_ref": "r", "content": None, "content_digest": None}, "artifacts.adopt": {"request_ref": "r"}, "audit.append": {"event_ref": "r"},
        "research.buildPlan": {"task_ref": "r"}, "research.executeAccepted": {"plan_ref": "r"},
        "conversation.accept": {"turn_ref": "r"}, "conversation.append": {"message_ref": "r"},
        "conversation.read": {"conversation_ref": "r", "limit": 1, "before_message_ref": None},
        "conversation.cancel": {"turn_ref": "r", "expected_revision": 1}, "scheduler.register": {"request_ref": "r"},
        "scheduler.disable": {"schedule_ref": "r", "expected_revision": 1}, "scheduler.dispatchDue": {"limit": 1},
        "connections.inspect": {"connection_ref": "r"}, "connections.invokeBoundAdapter": {"operation_ref": "r"},
        "agent-loop.startTurn": {"turn_ref": "r"}, "agent-loop.cancelTurn": {"turn_ref": "r", "expected_revision": 1},
        "agent-loop.inspectTurn": {"turn_ref": "r"},
        "source-extraction.extract": {"artifact_ref": "r", "acquisition_receipt_ref": "r", "source_slot": 0,
                                      "first_line": 1, "last_line": 1},
    }
    return values[method]


# ------------------------------------------------------------------ real host

@pytest_asyncio.fixture
async def live_host(service_db):
    from src.runtime_plugins.bridge import CordisHost
    jobs = DurableJobRepository()
    dispatcher = NativeServiceDispatcher(jobs=jobs)
    calls = []
    original = dispatcher.dispatch

    async def observe(frame, **kwargs):
        calls.append(json.loads(json.dumps(frame)))
        return await original(frame, **kwargs)
    dispatcher.dispatch = observe
    host = CordisHost(node_path=Path(NODE), service_dispatch=dispatcher)
    try:
        assert await host.start(), host.snapshot()
        yield host, jobs, calls
    finally:
        await host.stop()
        assert host.snapshot()["cleanup"]["process_reaped"] is True


async def _task_scope(host, jobs, operator, *, key="task-1", goal_id=None):
    candidate = {"schema_version": 1, "checkpoint_refs": ["step-1", "step-2"]}
    job, replayed = await admit_service_job(
        operator=operator, origin_method="tasks.admit", native_branch="workflow",
        candidate=candidate, idempotency_key=key,
        reviewed_composition=host.reviewed, goal_id=goal_id, goal_revision=1 if goal_id else None, jobs=jobs)
    owner, fence = await claim_service_job(job, host=host, jobs=jobs)
    scope = await open_invocation(job_id=job["job_id"], owner=owner, fence=fence, host=host,
                                  admission_replayed=replayed, candidate=candidate, jobs=jobs)
    return job, scope


@needs_host
@pytest.mark.asyncio
async def test_live_task_services_forward_through_real_child(live_host):
    host, jobs, calls = live_host
    operator = await _operator()
    goal_id = await _goal(operator)
    job, scope = await _task_scope(host, jobs, operator, goal_id=goal_id)
    resolved = await host.request_service("authority.resolve", {}, original_scope=scope)
    assert resolved["status"] == "succeeded" and resolved["value"]["mode"] == "operator-root"
    goal = await host.request_service("goals.read", {}, original_scope=scope)
    assert goal["value"] == {"goal_ref": goal_id, "revision": 1, "title": "Ship #1007",
                             "description": "private goal plan"}
    admitted = await verified_task_call(host, jobs, scope, "tasks.admit", {"request_ref": job["job_id"]})
    assert admitted["value"]["job_ref"] == job["job_id"] and admitted["value"]["replayed"] is False
    checkpoint = await verified_task_call(host, jobs, scope, "tasks.checkpoint", {"checkpoint_ref": "step-1"})
    assert checkpoint["status"] == "succeeded", checkpoint
    replay = await host.request_service("tasks.checkpoint", {"checkpoint_ref": "step-1"}, original_scope=scope)
    assert replay["reason_code"] == "native_receipt_replay_denied"
    unbound = await host.request_service("tasks.checkpoint", {"checkpoint_ref": "plugin-chosen"}, original_scope=scope)
    assert unbound["reason_code"] == "native_checkpoint_reference_not_bound"
    inspected = await verified_task_call(host, jobs, scope, "tasks.inspect", {"job_ref": job["job_id"]})
    assert inspected["value"]["state"] == "running"
    # The child forwarded exactly the same method each time; no other crossing.
    assert [call["method"] for call in calls] == ["authority.resolve", "goals.read", "tasks.admit",
                                                  "tasks.checkpoint", "tasks.checkpoint", "tasks.checkpoint",
                                                  "tasks.inspect"]
    revision = (await jobs.get_job(job["job_id"]))["revision"]
    cancelled = await verified_task_call(host, jobs, scope, "tasks.cancel",
                                         {"job_ref": job["job_id"], "expected_revision": revision})
    assert cancelled["value"]["state"] == "cancelled"
    after = await host.request_service("tasks.inspect", {"job_ref": job["job_id"]}, original_scope=scope)
    assert after["reason_code"] == "native_original_attempt_changed"
    with pytest.raises(Exception):  # Methods outside the frozen closure never leave Python.
        await host.request_service("memory.retrieve", {"query_ref": "x", "limit": 1}, original_scope=scope)
    snapshot = host.snapshot()["native_services"]
    assert set(snapshot["supported_methods"]) == SUPPORTED_METHODS
    assert set(snapshot["blocked_methods"]) == set(BLOCKED_METHODS)


@needs_host
@pytest.mark.asyncio
async def test_idempotent_admission_replay_is_reported_through_the_child(live_host):
    host, jobs, _ = live_host
    operator = await _operator()
    kwargs = dict(operator=operator, origin_method="tasks.admit", native_branch="workflow",
                  candidate={"schema_version": 1, "checkpoint_refs": []}, idempotency_key="replay-1",
                  reviewed_composition=host.reviewed, jobs=jobs)
    first, _ = await admit_service_job(**kwargs)
    second, replayed = await admit_service_job(**kwargs)
    assert replayed and second["job_id"] == first["job_id"]
    owner, fence = await claim_service_job(second, host=host, jobs=jobs)
    scope = await open_invocation(job_id=second["job_id"], owner=owner, fence=fence, host=host,
                                  admission_replayed=replayed, candidate=kwargs["candidate"], jobs=jobs)
    result = await host.request_service("tasks.admit", {"request_ref": first["job_id"]}, original_scope=scope)
    assert result["value"] == {"job_ref": first["job_id"], "revision": result["value"]["revision"],
                               "state": "running", "replayed": True}
    with pytest.raises(NativeServiceBlocked):  # A claimed attempt is never re-claimed.
        await claim_service_job(await jobs.get_job(first["job_id"]), host=host, jobs=jobs)
    async with get_session() as db:
        assert await db.scalar(select(func.count()).select_from(WorkflowRunState).where(
            WorkflowRunState.idempotency_key == "replay-1")) == 1


async def _expect_denied(host, scope, job_id, reason=None):
    """Private bytes (goals.read) and effects (tasks.checkpoint) are both denied."""
    before = await DurableJobRepository().get_job(job_id)
    goal = await host.request_service("goals.read", {}, original_scope=scope)
    effect = await host.request_service("tasks.checkpoint", {"checkpoint_ref": "step-2"}, original_scope=scope)
    authority = await host.request_service("authority.resolve", {}, original_scope=scope)
    for result in (goal, effect, authority):
        assert result["status"] == "blocked", result
        assert result["memory_status"] == "no_learning"
        if reason is not None:
            assert result["reason_code"] == reason, result
    assert "value" not in goal and "private goal plan" not in json.dumps(goal)
    after = await DurableJobRepository().get_job(job_id)
    assert after["checkpoints"] == before["checkpoints"] and after["revision"] == before["revision"]


@needs_host
@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["revoked_root", "expired_root", "stale_goal", "inactive_goal", "stale_lease",
                                  "wrong_lease_owner", "stale_epoch", "draining_owner", "cordis_owner_swap",
                                  "missing_dependency", "changed_grants"])
async def test_wrong_revoked_stale_or_missing_owners_deny_effects_and_private_bytes(live_host, case):
    host, jobs, _ = live_host
    operator = await _operator()
    goal_id = await _goal(operator)
    job, scope = await _task_scope(host, jobs, operator, key="deny-" + case, goal_id=goal_id)
    assert (await host.request_service("goals.read", {}, original_scope=scope))["status"] == "succeeded"
    now = datetime.now(timezone.utc)
    reason = None
    async with get_session() as db:
        if case == "revoked_root":
            await db.execute(update(OperatorSession).where(OperatorSession.id == operator.session_id).values(revoked_at=now))
            reason = "native_original_root_inactive"
        elif case == "expired_root":
            await db.execute(update(OperatorSession).where(OperatorSession.id == operator.session_id)
                             .values(idle_expires_at=now - timedelta(seconds=1)))
            reason = "native_original_root_inactive"
        elif case == "stale_goal":
            await db.execute(update(Goal).where(Goal.id == goal_id).values(revision=2))
            reason = "native_original_job_authority_changed"
        elif case == "inactive_goal":
            await db.execute(update(Goal).where(Goal.id == goal_id).values(status="completed"))
            reason = "native_original_job_authority_changed"
        elif case == "stale_lease":
            await db.execute(update(WorkflowRunState).where(WorkflowRunState.run_identity == job["job_id"])
                             .values(lease_expires_at=now - timedelta(seconds=1)))
            reason = "native_original_attempt_changed"
        elif case == "wrong_lease_owner":
            await db.execute(update(WorkflowRunState).where(WorkflowRunState.run_identity == job["job_id"])
                             .values(lease_owner="cordis-host:intruder", fencing_token=WorkflowRunState.fencing_token + 1))
            reason = "native_original_attempt_changed"
        elif case == "stale_epoch":
            await db.execute(update(RuntimeCompositionState).where(
                RuntimeCompositionState.runtime_domain == "seraph.goals.v1").values(epoch=2))
            reason = "composition_dependency_changed"
        elif case == "draining_owner":
            await db.execute(update(RuntimeCompositionState).where(
                RuntimeCompositionState.runtime_domain == "seraph.tasks.v1").values(state="draining"))
            reason = "composition_dependency_changed"
        elif case == "cordis_owner_swap":
            await db.execute(update(RuntimeCompositionState).where(
                RuntimeCompositionState.runtime_domain == "seraph.authority.v1").values(owner_kind="cordis"))
            reason = "composition_dependency_changed"
        elif case == "missing_dependency":
            await db.execute(delete(RuntimeCompositionState).where(
                RuntimeCompositionState.runtime_domain == "seraph.authority.v1"))
            reason = "composition_dependency_missing"
        elif case == "changed_grants":
            await db.execute(update(WorkflowRunState).where(WorkflowRunState.run_identity == job["job_id"])
                             .values(declared_authority_json=json.dumps({"principal": "x"})))
            reason = "native_original_policy_changed"
    await _expect_denied(host, scope, job["job_id"], reason)


@needs_host
@pytest.mark.asyncio
async def test_stale_epoch_after_quiesced_cutover_never_revives_old_authority(live_host):
    host, jobs, _ = live_host
    operator = await _operator()
    job, scope = await _task_scope(host, jobs, operator, key="quiesce")
    expected = CompositionDependency("seraph.tasks.v1", "legacy", 1, legacy_composition_digest("seraph.tasks.v1"))
    with lifecycle_fence_marker():
        async with get_session() as db:
            async with composition_writer(db):
                await transition_owner(db, runtime_domain="seraph.tasks.v1", expected=expected, expected_state="ready",
                                       owner_kind="legacy", epoch=1, composition_digest=expected.composition_digest,
                                       state="draining")
        # A live leased attempt bound to the old epoch blocks the epoch change.
        async with get_session() as db:
            with pytest.raises(CompositionBindingError) as busy:
                async with composition_writer(db):
                    await transition_owner(db, runtime_domain="seraph.tasks.v1", expected=expected,
                                           expected_state="draining", owner_kind="legacy", epoch=2,
                                           composition_digest=expected.composition_digest, state="blocked")
            assert busy.value.reason_code == "composition_not_quiescent"
    # Draining already stops admission and calls for the bound attempt.
    blocked = await host.request_service("tasks.inspect", {"job_ref": job["job_id"]}, original_scope=scope)
    assert blocked["reason_code"] == "composition_dependency_changed"
    with pytest.raises(Exception):
        await admit_service_job(operator=operator, origin_method="tasks.admit", native_branch="workflow",
                                candidate={"schema_version": 1, "checkpoint_refs": []}, idempotency_key="during-drain",
                                reviewed_composition=host.reviewed, jobs=jobs)


@needs_host
@pytest.mark.asyncio
async def test_read_job_artifact_readback_journey_records_explicit_no_learning(live_host):
    host, jobs, calls = live_host
    operator = await _operator()
    async with get_session() as db:
        db.add(Memory(id="owner-memory", content="raw content never crosses", summary="cafe summary 1007",
                      source_session_id=operator.session_id))
        db.add(Memory(id="foreign-memory", content="foreign", summary="cafe foreign", source_session_id="other-root"))
    _, digest = native_capability_inventory()
    cases = [
        ({"schema_version": 1, "method": "capabilities.list", "cursor": None, "limit": 2,
          "native_inventory_digest": digest}, "list-1"),
        ({"schema_version": 1, "method": "memory.retrieve", "query": "cafe", "limit": 5, "status": "active"}, "mem-1"),
    ]
    for candidate, key in cases:
        start = len(calls)
        value = await execute_native_read(operator=operator, candidate=candidate, idempotency_key=key,
                                          host=host, jobs=jobs)
        method = candidate["method"]
        assert [call["method"] for call in calls[start:]] == [method, "artifacts.stage", "artifacts.adopt",
                                                              "artifacts.read", "audit.append"]
        assert value["memory_status"] == "no_learning" and value["result"]["memory_status"] == "no_learning"
        job = value["job"]
        assert job["status"] == "succeeded" and "no_learning" in job["result"]["summary"]
        readbacks = [item for item in job["effects"] if item["receipt_kind"] == "readback"]
        assert len(readbacks) == 1 and readbacks[0]["details"] == {"no_learning": True, "verified": True}
        assert [item["checkpoint_id"] for item in job["checkpoints"]][1:] == ["cordis-read:result", "cordis-read:adopt"]
        async with get_session() as db:
            from src.runtime_plugins.dispatch import read_context
            sealed = read_context((await db.execute(select(WorkflowRunState).where(
                WorkflowRunState.run_identity == job["job_id"]))).scalar_one())
        # Returned values are Python's sealed result and Python's own readback.
        assert value["result"] == sealed["result"]["payload"]
        assert value["completion"]["result_digest"] == sealed["result"]["result_digest"]
        content = value["artifact_readback"]["value"]["content"]
        assert json.loads(content) == sealed["result"]["payload"]
        physical = Path(settings.workspace_dir) / job["artifacts"][0]["file_path"]
        assert physical.read_bytes() == content.encode()
        assert physical.stat().st_mode & 0o777 == 0o600
        assert hashlib.sha256(content.encode()).hexdigest() == value["artifact_readback"]["value"]["digest"]
        async with get_session() as db:
            event = await db.get(AuditEvent, value["completion"]["audit_event_ref"])
        assert event.event_type == "runtime_service_observed" and json.loads(event.details_json)["no_learning"] is True
        if method == "memory.retrieve":
            assert value["result"]["value"]["records"] == [{
                "record_ref": "owner-memory", "text": "cafe summary 1007",
                "text_digest": hashlib.sha256(b"cafe summary 1007").hexdigest()}]
            # The private query never crossed to the plugin; only a job reference did.
            assert calls[start]["payload"] == {"query_ref": job["job_id"], "limit": 5}
            assert "cafe" not in json.dumps(job)
        # An exact retry reuses the one job and never re-executes.
        retry = await execute_native_read(operator=operator, candidate=candidate, idempotency_key=key,
                                          host=host, jobs=jobs)
        assert retry["replayed"] is True and retry["job"]["job_id"] == job["job_id"]
        assert len(calls) == start + 5
    async with get_session() as db:
        memory = await db.get(Memory, "owner-memory")
        assert memory.summary == "cafe summary 1007"  # read is not learning


@needs_host
@pytest.mark.asyncio
async def test_revoked_root_mid_journey_denies_artifact_effects_and_blocks_job(live_host, monkeypatch):
    host, jobs, calls = live_host
    operator = await _operator()
    from src.runtime_plugins import service_artifacts
    original = service_artifacts.dispatch_artifact_operation

    async def revoke_then_stage(dispatcher, frame, payload, scope):
        if frame["method"] == "artifacts.stage":
            async with get_session() as db:
                await db.execute(update(OperatorSession).where(OperatorSession.id == operator.session_id)
                                 .values(revoked_at=datetime.now(timezone.utc)))
        return await original(dispatcher, frame, payload, scope)
    monkeypatch.setattr(service_artifacts, "dispatch_artifact_operation", revoke_then_stage)
    _, digest = native_capability_inventory()
    with pytest.raises(NativeServiceBlocked) as denied:
        await execute_native_read(operator=operator, idempotency_key="revoked-mid", host=host, jobs=jobs,
                                  candidate={"schema_version": 1, "method": "capabilities.describe",
                                             "capability_id": "native_tool:read_file", "native_inventory_digest": digest})
    assert denied.value.reason_code == "native_original_root_inactive"
    async with get_session() as db:
        run = (await db.execute(select(WorkflowRunState).where(
            WorkflowRunState.idempotency_key == "revoked-mid"))).scalar_one()
    assert run.artifact_receipts_json in (None, "[]") and json.loads(run.effect_receipts_json or "[]") == []
    assert not list((Path(settings.workspace_dir) / "artifacts").rglob("*.json")) if (
        Path(settings.workspace_dir) / "artifacts").exists() else True


# ------------------------------------------------------------------ goal-programme authority

def _programme_run(*, issuer_root="root-issuer"):
    from types import SimpleNamespace
    from src.work_board.research_parent import DISCOVERY_SERVICE
    binding = {"goal_id": "goal-p", "goal_revision": 3, "programme_id": "programme-1", "grant_revision": 1,
               "owner_identity_id": "identity-1", "issuer_root_id": issuer_root,
               "issuer_principal_id": "operator:root:issuer-principal", "brief_digest": "a" * 64,
               "capability_id": "guardian.goal-discovery.v1", "route_epoch": 1, "route_digest": "b" * 64,
               "expires_at": "2099-01-01T00:00:00+00:00", "cost_ceiling_microusd": 500}
    authority = {"authority_type": "goal_programme_discovery_v1", "principal": DISCOVERY_SERVICE,
                 "owner_kind": "service", "service_id": DISCOVERY_SERVICE,
                 "capability_id": "guardian.goal-discovery.v1", "capability_version": "1",
                 "goal_owner_principal_id": binding["issuer_principal_id"], "goal_owner_session_id": issuer_root,
                 "programme_binding": binding,
                 "plan_ref": {"artifact_id": "plan-1", "digest": "c" * 64, "schema_version": 1},
                 "occurrence_day": "2026-10-15", "original_job_id": "goal-discovery:job-1",
                 "budget_microusd": 100, "no_learning": True}
    return SimpleNamespace(declared_authority_json=json.dumps(authority), owner_kind="service",
                           owner_principal_id=DISCOVERY_SERVICE, service_id=DISCOVERY_SERVICE, session_id=None,
                           goal_id="goal-p", goal_revision=3, run_identity="goal-discovery:job-1")


@pytest.mark.asyncio
async def test_goal_programme_authority_is_resolved_from_current_programme_not_issuer_root(service_db, monkeypatch):
    from src.guardian import goal_programmes
    calls = []

    async def validate(*, db, binding, policy):
        calls.append(binding.programme_id)
        if binding.programme_id == "revoked":
            raise goal_programmes.GoalProgrammeError("programme_revoked")
        return object()
    monkeypatch.setattr(goal_programmes.goal_programme_service, "validate_current_binding", validate)
    monkeypatch.setattr(goal_programmes, "stage_programme_policy", lambda: object())
    dispatcher = NativeServiceDispatcher()
    async with get_session() as db:
        # The issuing Root row does not exist at all (expired/logged out): still valid.
        assert await dispatcher.current_authority(db, _programme_run()) == "goal-programme"
        revoked = _programme_run()
        authority = json.loads(revoked.declared_authority_json)
        authority["programme_binding"]["programme_id"] = "revoked"
        revoked.declared_authority_json = json.dumps(authority)
        with pytest.raises(NativeServiceBlocked) as denied:
            await dispatcher.current_authority(db, revoked)
        assert denied.value.reason_code == "native_programme_authority_changed"
        stale = _programme_run()
        stale.goal_revision = 4  # job no longer bound to the programme's goal revision
        with pytest.raises(NativeServiceBlocked) as lineage:
            await dispatcher.current_authority(db, stale)
        assert lineage.value.reason_code == "native_programme_lineage_changed"
    assert calls == ["programme-1", "revoked"]


# ------------------------------------------------------------------ seams #1008 builds on

def _top_level(body):
    parts, depth, current = [], 0, ""
    for char in body:
        depth += char in "([{"
        depth -= char in ")]}"
        if char == "," and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += char
    return [part for part in [*parts, current.strip()] if part]


def _ts_fields(body, named):
    """Top-level field names of a TS object literal, expanding ``...spread``."""
    fields = set()
    for part in _top_level(body):
        if part.startswith("..."):
            fields |= named[part[3:]]
        else:
            fields.add(part.split(":", 1)[0].strip())
    return fields


def _ts_schemas():
    """Parse the closed TS inventory: method -> (input fields, output fields)."""
    import re
    source = (PACKAGE / "src/contracts/methods.ts").read_text()
    named = {"artifact": _ts_fields(re.search(r"const artifact = \{(.*?)\};", source).group(1), {}),
             "capability": _ts_fields(re.search(r"const capability = object\(\{(.*?)\}\);", source).group(1), {})}
    block = source[source.index("const outputs = {") + len("const outputs = {"):source.index("};\nexport const serviceSchemas")]
    outputs = {}
    for part in _top_level(block):
        name, _, value = part.partition(":")
        name, value = name.strip(), value.strip()
        if not value:  # shorthand reference to a named schema
            outputs[name] = named[name]
        elif value.startswith("object({"):
            outputs[name] = _ts_fields(value[len("object({"):-len("})")], named)
        else:
            outputs[name] = named[value[len("object("):-1]]
    entries = {}
    for method, fields, output in re.findall(
            r"'([\w-]+\.\w+)': \{input: object\(\{(.*?)\}\), output: outputs\.(\w+)\}", source):
        entries[method] = (_ts_fields(fields, named), outputs[output])
    return entries


def test_ts_and_python_method_inventories_stay_in_lockstep():
    from src.runtime_plugins.contracts import REQUESTS, RESULTS
    entries = _ts_schemas()
    assert set(entries) == set(REQUESTS) == set(RESULTS) == set(METHOD_DOMAINS)
    for method in ("research.executeAccepted", "source-extraction.extract", "research.buildPlan"):
        assert method in entries
    for method, (inputs, outputs) in entries.items():
        assert inputs == set(REQUESTS[method]), method
        assert outputs == set(RESULTS[method]), method


@pytest.mark.asyncio
async def test_callbacks_are_gated_by_the_reviewed_closure_not_method_equality(service_db):
    dispatcher = NativeServiceDispatcher()
    async with get_session() as db:
        binding = await bind_invocation(db, method="capabilities.list", native_branch="artifact")
    witness = {"invocation_ref": "job-x", "package_digest": "a" * 64, "host_composition_digest": "b" * 64,
               "original_deadline_at": 2_000_000_000_000, "checkpoint_id": "missing"}
    scope = OriginalServiceInvocation(witness, binding, "c" * 64)
    parent_epoch = binding.epoch_for("capabilities.list")
    called = CalledServiceInvocation(scope, "capabilities.list", parent_epoch, 1_900_000_000_000, "r-1", "c" * 64)

    def frame(method):
        return {"method": method, "payload": _payload(method), "invocation_ref": "job-x",
                "composition_epoch": parent_epoch, "boot_nonce": "c" * 64, "package_digest": "a" * 64,
                "composition_digest": "b" * 64, "deadline_at": 1_900_000_000_000}
    outside = await dispatcher.dispatch(frame("goals.read"), called_scope=called)
    assert outside["reason_code"] == "native_method_not_in_closure"
    # A different in-closure method passes correlation and reaches the owner's
    # current-state checks (which then deny this fixture's missing job).
    inside = await dispatcher.dispatch(frame("artifacts.stage"), called_scope=called)
    assert inside["status"] == "blocked"
    assert inside["reason_code"] not in {"native_method_not_in_closure", "native_original_frame_changed"}


def test_python_profile_accepts_optional_providers_and_rejects_unknown_config():
    from copy import deepcopy
    from src.runtime_plugins.composition import PACKAGE_ROOT, validate_profile
    from src.runtime_plugins.protocol import ProtocolError, decode_json
    base = decode_json((PACKAGE_ROOT / "profile.json").read_bytes())
    optional = deepcopy(base)
    optional["plugins"] = [dict(spec, required=False) if spec["id"] == "seraph.memory.v1" else spec
                           for spec in optional["plugins"] if spec["id"] != "seraph.research.v1"]
    assert validate_profile(optional) is optional
    for change in ("config", "order", "host_optional", "unknown", "duplicate"):
        broken = deepcopy(base)
        if change == "config":
            broken["plugins"][2]["config"] = {"path": "/tmp"}
        elif change == "order":
            broken["plugins"][1], broken["plugins"][2] = broken["plugins"][2], broken["plugins"][1]
        elif change == "host_optional":
            broken["plugins"][0]["required"] = False
        elif change == "unknown":
            broken["plugins"].append({"id": "seraph.unreviewed.v1", "required": False, "dependencies": [], "config": {}})
        else:
            broken["plugins"].append(deepcopy(broken["plugins"][1]))
        with pytest.raises(ProtocolError):
            validate_profile(broken)


@needs_host
@pytest.mark.asyncio
async def test_child_reports_build_derived_plugin_digests_and_mismatch_fences(live_host):
    host, _, _ = live_host
    status = await host.refresh_status()
    reported = {item["id"]: item["digest"] for item in status["plugins"]}
    assert reported == host.reviewed.plugin_digests and len(set(reported.values())) == len(reported)
    from dataclasses import replace
    host.reviewed = replace(host.reviewed, plugin_digests={**host.reviewed.plugin_digests,
                                                           "seraph.tasks.v1": "0" * 64})
    fenced = await host.refresh_status()
    assert fenced["state"] != "ready" or fenced["readiness"]["state"] != "verified"



# ------------------------------------------------------------------ child answers are never authority

@needs_host
@pytest.mark.asyncio
@pytest.mark.parametrize("tampered", ["memory.retrieve", "artifacts.read", "audit.append"])
async def test_child_fabricated_parent_results_never_settle_the_job(live_host, tampered):
    """Regression for the critic probe: a child that performs the real callback
    but answers the parent with different schema-valid values is rejected."""
    host, jobs, _ = live_host
    operator = await _operator()
    async with get_session() as db:
        db.add(Memory(id="m1", content="raw", summary="real summary", source_session_id=operator.session_id))
    real = host.request_service

    async def tampering(method, payload, *, original_scope):
        result = await real(method, payload, original_scope=original_scope)
        if method != tampered:
            return result
        if method == "memory.retrieve":
            value = {"records": [{"record_ref": "fake", "text": "FABRICATED",
                                  "text_digest": hashlib.sha256(b"FABRICATED").hexdigest()}]}
        elif method == "artifacts.read":
            value = {**result["value"], "content": "{}", "size_bytes": 2,
                     "digest": hashlib.sha256(b"{}").hexdigest()}
        else:
            value = {**result["value"], "revision": result["value"]["revision"] + 7}
        return {**result, "value": value}
    host.request_service = tampering
    with pytest.raises(NativeServiceBlocked) as denied:
        await execute_native_read(operator=operator, idempotency_key="tamper-" + tampered, host=host, jobs=jobs,
                                  candidate={"schema_version": 1, "method": "memory.retrieve", "query": "real",
                                             "limit": 5, "status": "active"})
    assert denied.value.reason_code == "native_child_result_mismatch"
    async with get_session() as db:
        run = (await db.execute(select(WorkflowRunState).where(
            WorkflowRunState.idempotency_key == "tamper-" + tampered))).scalar_one()
    assert run.status == "blocked" and run.failure_reason == "native_child_result_mismatch"
    assert "FABRICATED" not in (run.result_summary or "") and run.result_digest is None


@needs_host
@pytest.mark.asyncio
async def test_child_fabricated_task_receipt_is_rejected_against_the_durable_row(live_host):
    host, jobs, _ = live_host
    operator = await _operator()
    job, scope = await _task_scope(host, jobs, operator, key="task-tamper")
    real = host.request_service

    async def tampering(method, payload, *, original_scope):
        result = await real(method, payload, original_scope=original_scope)
        return {**result, "value": {**result["value"], "receipt_ref": "cordis-task:never-recorded"}}
    host.request_service = tampering
    with pytest.raises(NativeServiceBlocked) as denied:
        await verified_task_call(host, jobs, scope, "tasks.checkpoint", {"checkpoint_ref": "step-1"})
    assert denied.value.reason_code == "native_child_result_mismatch"


@needs_host
@pytest.mark.asyncio
async def test_interrupted_stage_stays_visibly_unresolved_and_is_never_reexecuted(live_host, monkeypatch):
    from src.work_board import input_artifacts
    host, jobs, calls = live_host
    operator = await _operator()

    def lost_disk(path, payload):
        raise OSError("disk unavailable")
    monkeypatch.setattr(input_artifacts, "_write_payload", lost_disk)
    _, digest = native_capability_inventory()
    candidate = {"schema_version": 1, "method": "capabilities.describe", "capability_id": "native_tool:read_file",
                 "native_inventory_digest": digest}
    with pytest.raises(NativeServiceBlocked) as denied:
        await execute_native_read(operator=operator, idempotency_key="interrupted", host=host, jobs=jobs,
                                  candidate=candidate)
    assert denied.value.reason_code == "native_artifact_owner_operation_unproven"
    async with get_session() as db:
        run = (await db.execute(select(WorkflowRunState).where(
            WorkflowRunState.idempotency_key == "interrupted"))).scalar_one()
    effects = json.loads(run.effect_receipts_json)
    assert [(item["effect_type"], item["status"]) for item in effects] == [("cordis_read_artifact", "intent")]
    from src.runtime_plugins.dispatch import read_context
    assert [op["state"] for op in read_context(run)["operations"]] == ["intent"]
    calls_before = len(calls)
    monkeypatch.undo()
    retry = await execute_native_read(operator=operator, idempotency_key="interrupted", host=host, jobs=jobs,
                                      candidate=candidate)
    assert retry["replayed"] is True and retry["job"]["job_id"] == run.run_identity
    assert retry["job"]["status"] != "succeeded" and len(calls) == calls_before


@pytest.mark.asyncio
async def test_epoch_change_visibly_blocks_unstarted_jobs_bound_to_the_old_owner(service_db):
    operator = await _operator()
    accepted, _ = await admit_service_job(operator=operator, origin_method="tasks.admit", native_branch="workflow",
                                          candidate={"schema_version": 1, "checkpoint_refs": []},
                                          idempotency_key="unstarted", reviewed_composition=None)
    expected = CompositionDependency("seraph.tasks.v1", "legacy", 1, legacy_composition_digest("seraph.tasks.v1"))
    with lifecycle_fence_marker():
        for target_state, epoch, prior_state in (("draining", 1, "ready"), ("blocked", 2, "draining")):
            async with get_session() as db:
                async with composition_writer(db):
                    receipt = await transition_owner(db, runtime_domain="seraph.tasks.v1", expected=expected,
                                                     expected_state=prior_state, owner_kind="legacy", epoch=epoch,
                                                     composition_digest=expected.composition_digest,
                                                     state=target_state)
    job = await DurableJobRepository().get_job(accepted["job_id"])
    assert job["status"] == "blocked" and job["failure_reason"] == "composition_owner_changed"
    async with get_session() as db:
        proof = json.loads((await db.get(AuditEvent, receipt)).details_json)
    assert proof["blocked_unstarted_jobs"] == [accepted["job_id"]]
    # Rollback to ready at the new epoch never revives the blocked job.
    rolled = CompositionDependency("seraph.tasks.v1", "legacy", 2, expected.composition_digest)
    with lifecycle_fence_marker():
        async with get_session() as db:
            async with composition_writer(db):
                await transition_owner(db, runtime_domain="seraph.tasks.v1", expected=rolled, expected_state="blocked",
                                       owner_kind="legacy", epoch=2, composition_digest=expected.composition_digest,
                                       state="ready")
    assert (await DurableJobRepository().get_job(accepted["job_id"]))["status"] == "blocked"



@needs_host
@pytest.mark.asyncio
async def test_child_reused_checkpoint_receipt_or_false_admit_state_blocks_the_job(live_host):
    """Critic probe 2: answers must equal Python's row-derived value exactly."""
    host, jobs, _ = live_host
    operator = await _operator()
    for lie in ("checkpoint_reuse", "admit_state"):
        job, scope = await _task_scope(host, jobs, operator, key="lie-" + lie)
        ok = await verified_task_call(host, jobs, scope, "tasks.checkpoint", {"checkpoint_ref": "step-1"})
        receipt = next(item for item in (await jobs.get_job(job["job_id"]))["checkpoints"]
                       if item["checkpoint_id"] == "cordis-task:step-1")
        assert ok["value"] == {"receipt_ref": "cordis-task:step-1", "revision": receipt["payload"]["revision"]}
        real = host.request_service

        async def lying(method, payload, *, original_scope):
            current = await jobs.get_job(job["job_id"])
            if lie == "checkpoint_reuse":  # answers step-2 without making the callback
                return {"status": "succeeded", "memory_status": "no_learning",
                        "value": {"receipt_ref": "cordis-task:step-1", "revision": current["revision"]}}
            return {"status": "succeeded", "memory_status": "no_learning",
                    "value": {"job_ref": job["job_id"], "revision": current["revision"], "state": "succeeded",
                              "replayed": False}}
        host.request_service = lying
        try:
            method, payload = (("tasks.checkpoint", {"checkpoint_ref": "step-2"}) if lie == "checkpoint_reuse"
                               else ("tasks.admit", {"request_ref": job["job_id"]}))
            with pytest.raises(NativeServiceBlocked) as denied:
                await verified_task_call(host, jobs, scope, method, payload)
        finally:
            host.request_service = real
        assert denied.value.reason_code == "native_child_result_mismatch"
        after = await jobs.get_job(job["job_id"])
        assert after["status"] == "blocked" and after["failure_reason"] == "native_child_result_mismatch"
        assert "cordis-task:step-2" not in {item["checkpoint_id"] for item in after["checkpoints"]}


@needs_host
@pytest.mark.asyncio
async def test_boot_proof_requires_the_domain_provider_running_in_the_live_child(live_host):
    host, _, _ = live_host
    domain = "seraph.memory.v1"
    digest = host.reviewed.plugin_digests[domain]
    legacy = CompositionDependency(domain, "legacy", 1, legacy_composition_digest(domain))
    target = CompositionDependency(domain, "cordis", 2, digest)
    with lifecycle_fence_marker():
        async with get_session() as db:
            async with composition_writer(db):
                await transition_owner(db, runtime_domain=domain, expected=legacy, expected_state="ready",
                                       owner_kind="legacy", epoch=1, composition_digest=legacy.composition_digest,
                                       state="draining")
                await transition_owner(db, runtime_domain=domain, expected=legacy, expected_state="draining",
                                       owner_kind="cordis", epoch=2, composition_digest=digest, state="blocked")
        saved = host._plugins
        host._plugins = [dict(item, state="blocked") if item["id"] == domain else item for item in saved]
        try:
            async with get_session() as db:
                with pytest.raises(CompositionBindingError):
                    async with composition_writer(db):
                        await transition_owner(db, runtime_domain=domain, expected=target, expected_state="blocked",
                                               owner_kind="cordis", epoch=2, composition_digest=digest,
                                               state="ready", boot_host=host)
        finally:
            host._plugins = saved
        async with get_session() as db:
            async with composition_writer(db):
                await transition_owner(db, runtime_domain=domain, expected=target, expected_state="blocked",
                                       owner_kind="cordis", epoch=2, composition_digest=digest, state="ready",
                                       boot_host=host)
    assert await _row(domain) == (target, "ready")


# ------------------------------------------------------------------ #1066 transient storage contention

def _hold_writer_lock(db_path):
    import sqlite3
    holder = sqlite3.connect(db_path, isolation_level=None, timeout=0)
    holder.execute("BEGIN IMMEDIATE")
    return holder


@needs_host
@pytest.mark.asyncio
async def test_locked_database_in_one_callback_fails_only_that_call(live_host, service_db):
    """A real SQLite writer lock held by another connection during one callback
    yields a typed per-call block; the boot keeps admitting and later calls work."""
    host, jobs, calls = live_host
    operator = await _operator()
    goal_id = await _goal(operator)
    job, scope = await _task_scope(host, jobs, operator, key="locked", goal_id=goal_id)
    dispatcher = host.service_dispatch
    observed = dispatcher.dispatch
    lock_for = {"tasks.checkpoint"}

    async def contended(frame, **kwargs):
        if frame["method"] not in lock_for:
            return await observed(frame, **kwargs)
        lock_for.discard(frame["method"])
        holder = _hold_writer_lock(service_db)
        try:
            return await observed(frame, **kwargs)
        finally:
            holder.rollback()
            holder.close()
    dispatcher.dispatch = contended
    boot = host.boot_nonce
    before = await jobs.get_job(job["job_id"])
    busy = await host.request_service("tasks.checkpoint", {"checkpoint_ref": "step-1"}, original_scope=scope)
    assert busy == {"status": "blocked", "reason_code": "native_owner_storage_busy", "memory_status": "no_learning"}
    # No partial effect, and the same boot keeps admitting.
    after = await jobs.get_job(job["job_id"])
    assert after["checkpoints"] == before["checkpoints"] and after["revision"] == before["revision"]
    assert host.admitting and host.boot_nonce == boot
    status = host.snapshot()["native_services"]
    assert status["transient_failures"] == 1 and status["last_transient_reason"] == "native_owner_storage_busy"
    # The same invocation's later calls succeed once contention clears.
    retried = await verified_task_call(host, jobs, scope, "tasks.checkpoint", {"checkpoint_ref": "step-1"})
    assert retried["status"] == "succeeded"
    assert (await host.request_service("goals.read", {}, original_scope=scope))["status"] == "succeeded"
    assert (await host.refresh_status())["readiness"]["state"] == "verified"


@needs_host
@pytest.mark.asyncio
async def test_locked_database_during_a_read_journey_blocks_only_that_job(live_host, service_db):
    host, jobs, calls = live_host
    operator = await _operator()
    dispatcher = host.service_dispatch
    observed = dispatcher.dispatch
    locked = {"capabilities.list"}

    async def contended(frame, **kwargs):
        if frame["method"] not in locked:
            return await observed(frame, **kwargs)
        locked.discard(frame["method"])
        holder = _hold_writer_lock(service_db)
        try:
            return await observed(frame, **kwargs)
        finally:
            holder.rollback()
            holder.close()
    dispatcher.dispatch = contended
    _, digest = native_capability_inventory()
    candidate = {"schema_version": 1, "method": "capabilities.list", "cursor": None, "limit": 2,
                 "native_inventory_digest": digest}
    with pytest.raises(NativeServiceBlocked) as busy:
        await execute_native_read(operator=operator, candidate=candidate, idempotency_key="busy-1", host=host, jobs=jobs)
    assert busy.value.reason_code == "native_owner_storage_busy"
    async with get_session() as db:
        run = (await db.execute(select(WorkflowRunState).where(
            WorkflowRunState.idempotency_key == "busy-1"))).scalar_one()
    assert run.status == "blocked" and run.failure_reason == "native_owner_storage_busy"
    assert json.loads(run.effect_receipts_json or "[]") == [] and run.artifact_receipts_json in (None, "[]")
    assert host.admitting
    fresh = await execute_native_read(operator=operator, candidate=candidate, idempotency_key="busy-2",
                                      host=host, jobs=jobs)
    assert fresh["job"]["status"] == "succeeded"


@needs_host
@pytest.mark.asyncio
async def test_contention_after_stage_wrote_its_file_is_unproven_not_busy(live_host, service_db, monkeypatch):
    """A lock while settling a written stage is an unresolved effect, never busy."""
    from src.runtime_plugins import service_artifacts
    host, jobs, calls = live_host
    operator = await _operator()
    original_readback = service_artifacts.readback
    state = {"raised": False}

    def locked_once(run, context):
        if not state["raised"]:
            state["raised"] = True
            raise OperationalError("UPDATE workflow_run_state", {}, Exception("database is locked"))
        return original_readback(run, context)
    monkeypatch.setattr(service_artifacts, "readback", locked_once)
    before = host.service_dispatch.transient_failures
    _, digest = native_capability_inventory()
    candidate = {"schema_version": 1, "method": "capabilities.list", "cursor": None, "limit": 2,
                 "native_inventory_digest": digest}
    with pytest.raises(NativeServiceBlocked) as unproven:
        await execute_native_read(operator=operator, candidate=candidate, idempotency_key="stage-lock",
                                  host=host, jobs=jobs)
    assert state["raised"]
    assert unproven.value.reason_code == "native_artifact_owner_operation_unproven"
    assert host.service_dispatch.transient_failures == before
    async with get_session() as db:
        run = (await db.execute(select(WorkflowRunState).where(
            WorkflowRunState.idempotency_key == "stage-lock"))).scalar_one()
    assert run.status == "blocked" and run.failure_reason == "native_artifact_owner_operation_unproven"
    assert [item["status"] for item in json.loads(run.effect_receipts_json)] == ["intent"]
    assert host.admitting


@pytest.mark.asyncio
async def test_persistent_contention_while_blocking_leaves_the_job_for_stale_recovery():
    """Marking a job blocked under persistent contention never escapes as a raw error."""
    from types import SimpleNamespace
    from src.runtime_plugins.service_jobs import _block

    class LockedJobs:
        async def transition_job(self, *args, **kwargs):
            raise OperationalError("UPDATE workflow_run_state", {}, Exception("database is locked"))

    class BrokenJobs:
        async def transition_job(self, *args, **kwargs):
            raise OperationalError("UPDATE workflow_run_state", {}, Exception("disk I/O error"))
    scope = SimpleNamespace(witness={"invocation_ref": "job-1", "lease_owner": "owner", "fence": 1})
    await _block(LockedJobs(), scope, "native_child_result_mismatch")
    with pytest.raises(OperationalError):
        await _block(BrokenJobs(), scope, "native_child_result_mismatch")


@pytest.mark.asyncio
async def test_non_transient_storage_faults_still_escape_to_the_host_fence(service_db):
    """Only lock/busy contention is per-call; other storage faults are not masked."""
    from sqlalchemy.exc import OperationalError, TimeoutError as PoolTimeoutError
    from src.runtime_plugins.dispatch import transient_storage_error
    assert transient_storage_error(OperationalError("BEGIN IMMEDIATE", {}, Exception("database is locked")))
    assert transient_storage_error(PoolTimeoutError("QueuePool limit reached"))
    assert not transient_storage_error(OperationalError("SELECT", {}, Exception("no such table: goals")))
    assert not transient_storage_error(OperationalError("SELECT", {}, Exception("disk I/O error")))
