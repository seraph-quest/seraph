"""#1020 PR1: reviewed presets and unattended guardian turns on the Cordis loop.

The reviewed Cordis child is real, the guardian owner is cut over with the
production fenced transition, and every model call is a scripted local
completion at the caller's own completion boundary: sockets and DNS are
denied and no provider is contacted.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import json
import subprocess
from types import SimpleNamespace

import httpx
import pytest

from tests.cordis_agent_loop import deny_sockets
from tests.cordis_research import NODE, PACKAGE, needs_cordis, research_host
from tests.test_inference_accounting import accounting_db  # noqa: F401
from src.agent.presets import PRESETS, PresetError, preset_digest, require_call, require_unattended
from src.runtime_plugins.guardian_turns import (GUARDIAN_DOMAIN, PRESETS_DOMAIN, GuardianBlocked, guardian_completion,
                                                guardian_runtime_status)
from src.workflows.job_runtime import durable_job_repository

GUARDIAN_PRESETS = [name for name, value in PRESETS.items() if value["kind"] == "unattended"]


def _response(content: str):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


async def cutover(host, domain: str, *, to: str = "cordis"):
    """Production fenced owner transition: ready -> draining -> blocked(epoch+1) -> ready(boot proof)."""
    from src.db.engine import get_session
    from src.runtime_plugins.ownership import (CompositionDependency, composition_writer, inventory,
                                               legacy_composition_digest, transition_owner)
    from src.workspace.lifecycle import lifecycle_fence_marker
    async with get_session() as db:
        row = (await inventory(db))[domain]
        current = CompositionDependency(domain, row.owner_kind, row.epoch, row.composition_digest)
        state = row.state
    digest = host.reviewed.plugin_digests[domain] if to == "cordis" else legacy_composition_digest(domain)
    epoch = current.epoch + 1
    target = CompositionDependency(domain, to, epoch, digest)
    with lifecycle_fence_marker():
        for expected, expected_state, owner, at, next_state, proof in (
                (current, state, current.owner_kind, current.epoch, "draining", None),
                (current, "draining", to, epoch, "blocked", None),
                (target, "blocked", to, epoch, "ready", host)):
            if next_state == "draining" and expected_state != "ready":
                continue
            async with get_session() as db:
                async with composition_writer(db):
                    await transition_owner(db, runtime_domain=domain, expected=expected, expected_state=expected_state,
                                           owner_kind=owner, epoch=at,
                                           composition_digest=expected.composition_digest if next_state == "draining"
                                           else digest, state=next_state, boot_host=proof)
    return epoch


@asynccontextmanager
async def guardian_host(*, cordis: bool = True):
    async with research_host(durable_job_repository) as host:
        if cordis:
            await cutover(host, GUARDIAN_DOMAIN)
        yield host


@asynccontextmanager
async def service_principal(job_id: str = "strategist_tick"):
    """The scheduler wrapper's identity: a service principal with model inference only."""
    from uuid import uuid4
    from src.approval.runtime import reset_runtime_context, set_runtime_context
    from src.security.trust_contract import AuthorityGrant, PrincipalType, TrustPrincipal
    principal = TrustPrincipal(principal_id=f"service:scheduler:{job_id}", principal_type=PrincipalType.SERVICE,
                               grants=(AuthorityGrant.MODEL_INFERENCE,), job_id=f"scheduler:{job_id}:{uuid4().hex}")
    tokens = set_runtime_context(None, "high_risk", trust_principal=principal)
    try:
        yield principal
    finally:
        reset_runtime_context(tokens)


async def _jobs_of_kind(kind: str) -> list[dict]:
    from sqlalchemy import select
    from src.db.engine import get_session
    from src.db.models import WorkflowRunState
    async with get_session() as db:
        ids = [row.run_identity for row in (await db.execute(
            select(WorkflowRunState).where(WorkflowRunState.job_kind == kind))).scalars()]
    return [await durable_job_repository.get_job(job_id) for job_id in ids]


# ---------------------------------------------------------------- presets

def test_python_and_reviewed_cordis_presets_are_identical():
    """Each step names its preset digest; both tables must produce the same digests."""
    if not NODE or not (PACKAGE / "dist/src/plugins/agent_presets/index.js").is_file():
        pytest.skip("reviewed Cordis package not built")
    script = ("import('" + str(PACKAGE / "dist/src/plugins/agent_presets/index.js") + "').then(m => "
              "process.stdout.write(JSON.stringify(Object.fromEntries(Object.entries(m.PRESETS)"
              ".map(([id, value]) => [id, [value, m.presetDigest(value)]])))))")
    output = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, check=True,
                            timeout=30, env={"LANG": "C.UTF-8"}).stdout
    reviewed = json.loads(output)
    assert set(reviewed) == set(PRESETS)
    for preset_id, (value, digest) in reviewed.items():
        assert value == dict(PRESETS[preset_id]), preset_id
        assert digest == preset_digest(preset_id), preset_id


def test_unattended_presets_hold_no_tools_and_never_widen_a_call():
    assert GUARDIAN_PRESETS and all(require_unattended(name)["tools"] == "none" for name in GUARDIAN_PRESETS)
    for interactive in ("chat.agent", "chat.onboarding", "chat.direct"):
        with pytest.raises(PresetError, match="unattended_preset_tools_denied"):
            require_unattended(interactive)
    require_call("guardian.strategist", runtime_path="strategist_agent", temperature=0.2, max_tokens=512)
    for changed in ({"runtime_path": "chat_agent"}, {"temperature": 0.9}, {"max_tokens": 4096}):
        with pytest.raises(PresetError, match="agent_preset_changed"):
            require_call("guardian.strategist", **{"runtime_path": "strategist_agent", "temperature": 0.2,
                                                   "max_tokens": 512, **changed})


# ---------------------------------------------------------------- inventory upgrade

@pytest.mark.asyncio
async def test_introduced_domains_are_seeded_once_as_legacy_and_original_gaps_stay_blocked(accounting_db):  # noqa: F811
    from sqlalchemy import delete, select, text
    from src.db.engine import get_session
    from src.db.models import AuditEvent, RuntimeCompositionState
    from src.runtime_plugins.ownership import DOMAIN_INTRODUCTIONS, introduce_domains_sync, inventory
    from tests.cordis_research import seed_composition_inventory
    await seed_composition_inventory()
    async with get_session() as db:
        # A #1019-era inventory: every original row, none of the #1020 domains,
        # and one original domain lost.
        await db.execute(delete(RuntimeCompositionState).where(RuntimeCompositionState.runtime_domain.in_(
            [*DOMAIN_INTRODUCTIONS, "seraph.memory.v1"])))
        await db.commit()
    assert (await guardian_runtime_status())["reason"] == "composition_dependency_missing"
    async with get_session() as db:
        added = await db.run_sync(lambda session: introduce_domains_sync(session.connection()))
        await db.commit()
        again = await db.run_sync(lambda session: introduce_domains_sync(session.connection()))
        await db.commit()
        rows = await inventory(db)
        audits = (await db.execute(select(AuditEvent).where(
            AuditEvent.event_type == "runtime_composition_domain_introduced"))).scalars().all()
    assert sorted(added) == sorted(DOMAIN_INTRODUCTIONS) and again == []
    for domain in DOMAIN_INTRODUCTIONS:
        assert (rows[domain].owner_kind, rows[domain].epoch, rows[domain].state) == ("legacy", 1, "ready")
    assert "seraph.memory.v1" not in rows  # a missing original domain is never repaired
    assert len(audits) == len(DOMAIN_INTRODUCTIONS)
    assert (await guardian_runtime_status())["loop"] == "legacy.guardian"
    async with get_session() as db:
        await db.execute(text("DELETE FROM runtime_composition_states"))
        await db.commit()
        assert await db.run_sync(lambda session: introduce_domains_sync(session.connection())) == []


# ---------------------------------------------------------------- guardian turns

@needs_cordis
@pytest.mark.asyncio
async def test_legacy_guardian_owner_runs_todays_call_with_no_turn(accounting_db, monkeypatch):  # noqa: F811
    deny_sockets(monkeypatch)
    calls = []

    async def call():
        calls.append(1)
        return _response("legacy")
    async with guardian_host(cordis=False):
        async with service_principal("daily_briefing"):
            response = await guardian_completion("guardian.daily_briefing", call, timeout_seconds=30,
                                                 runtime_path="daily_briefing", temperature=0.6, max_tokens=512)
    assert response.choices[0].message.content == "legacy" and calls == [1]
    assert await _jobs_of_kind("cordis_guardian_turn_v1") == []


@needs_cordis
@pytest.mark.asyncio
async def test_cordis_guardian_turn_settles_once_and_adds_lineage_only(accounting_db, monkeypatch):  # noqa: F811
    deny_sockets(monkeypatch)
    from src.approval.runtime import get_current_trust_principal
    seen = []

    async def call():
        # The model call keeps today's principal: the turn never re-owns inference.
        seen.append(get_current_trust_principal().principal_id)
        return _response("Morning briefing")
    async with guardian_host() as host:
        assert (await guardian_runtime_status(host))["loop"] == "cordis.guardian.v1"
        async with service_principal("daily_briefing"):
            response = await guardian_completion("guardian.daily_briefing", call, timeout_seconds=30,
                                                 runtime_path="daily_briefing", temperature=0.6, max_tokens=512,
                                                 host=host)
    assert response.choices[0].message.content == "Morning briefing"
    assert seen == ["service:scheduler:daily_briefing"]
    (turn,) = await _jobs_of_kind("cordis_guardian_turn_v1")
    (step,) = await _jobs_of_kind("cordis_guardian_step_v1")
    assert turn["status"] == "succeeded" and step["status"] == "succeeded"
    from src.db.engine import get_session
    async with get_session() as db:
        run = await durable_job_repository._fetch(db, turn["job_id"])
        assert (run.owner_kind, run.owner_principal_id, run.service_id) == ("service", "service:guardian",
                                                                           "service:guardian")
        assert json.loads(run.declared_authority_json)["inference_principal_id"] == "service:scheduler:daily_briefing"
    async with get_session() as db:
        step_run = await durable_job_repository._fetch(db, step["job_id"])
        from src.runtime_plugins.dispatch import read_context
        assert read_context(step_run)["operations"] == [{"op": "decision", "ref": "settlement"}]
    from src.db.models import ApprovalRequest
    from sqlalchemy import select
    from src.db.engine import get_session
    async with get_session() as db:
        assert (await db.execute(select(ApprovalRequest))).scalars().all() == []  # no approval can arise


@needs_cordis
@pytest.mark.asyncio
async def test_blocked_guardian_owner_makes_no_call_and_never_falls_back(accounting_db, monkeypatch):  # noqa: F811
    deny_sockets(monkeypatch)
    calls = []

    async def call():
        calls.append(1)
        return _response("must not run")
    async with guardian_host() as host:
        await host.stop()
        status = await guardian_runtime_status(host)
        assert status["loop"] == "blocked" and status["reason"].startswith("cordis_")
        with pytest.raises(GuardianBlocked) as blocked:
            await guardian_completion("guardian.evening_review", call, timeout_seconds=30, runtime_path="evening_review",
                                      temperature=0.6, max_tokens=512, host=host)
        assert blocked.value.recovery
        await host.start()
    assert calls == [] and await _jobs_of_kind("cordis_guardian_turn_v1") == []


@needs_cordis
@pytest.mark.asyncio
async def test_epoch_change_during_the_call_fences_adoption(accounting_db, monkeypatch):  # noqa: F811
    deny_sockets(monkeypatch)
    async with guardian_host() as host:
        async def call():
            # The owner row moves on while the model call runs: its output is never used.
            from sqlalchemy import update
            from src.db.engine import get_session
            from src.db.models import RuntimeCompositionState
            async with get_session() as db:
                await db.execute(update(RuntimeCompositionState).where(
                    RuntimeCompositionState.runtime_domain == GUARDIAN_DOMAIN).values(
                        epoch=RuntimeCompositionState.epoch + 5))
                await db.commit()
            return _response("stale")
        async with service_principal("activity_digest"):
            with pytest.raises(GuardianBlocked) as blocked:
                await guardian_completion("guardian.activity_digest", call, timeout_seconds=30,
                                          runtime_path="activity_digest", temperature=0.6, max_tokens=768, host=host)
    assert blocked.value.reason_code in {"guardian_turn_epoch_changed", "composition_dependency_changed"}
    (turn,) = await _jobs_of_kind("cordis_guardian_turn_v1")
    assert turn["status"] != "succeeded"


@needs_cordis
@pytest.mark.asyncio
async def test_strategist_decision_is_fenced_by_its_scheduler_tick(accounting_db, monkeypatch):  # noqa: F811
    deny_sockets(monkeypatch)
    from datetime import datetime, timedelta, timezone
    from src.agent.strategist import run_strategist_decision_completion
    from src.workflows.job_runtime import DurableJobIdentity, DurableJobSpec
    requests = []

    async def scripted(**kwargs):
        requests.append({key: kwargs[key] for key in ("temperature", "max_tokens", "runtime_path", "timeout")})
        return _response('{"should_intervene": false, "content": "", "intervention_type": "nudge", '
                         '"urgency": 0, "reasoning": "fine"}')
    monkeypatch.setattr("src.agent.strategist.completion_with_fallback", scripted)

    async def tick(occurrence):
        spec = DurableJobSpec(identity=DurableJobIdentity(
            job_id=occurrence, owner_kind="service", owner_principal_id="service:scheduler:strategist_tick",
            job_kind="strategist_tick", capability_version="1", idempotency_scope="scheduler-occurrence",
            idempotency_key=occurrence), inputs={"trigger": "interval"}, declared_authority={
                "principal": "service:scheduler:strategist_tick", "owner_kind": "service",
                "service_id": "service:strategist"}, service_id="service:strategist",
            deadline_at=datetime.now(timezone.utc) + timedelta(seconds=120), max_attempts=1)
        job = await durable_job_repository.admit_job(spec)
        await durable_job_repository.queue_job(occurrence)
        claimed = await durable_job_repository.claim_job(occurrence, owner="strategist-runner", lease_seconds=90)
        return int(claimed["lease"]["fencing_token"]), job

    async with guardian_host() as host:
        monkeypatch.setattr("src.runtime_plugins.bridge.cordis_host", host)
        async with service_principal():
            fence, _ = await tick("strategist_tick:1")
            raw = await run_strategist_decision_completion(
                context_block="Time: morning", durable_job_id="strategist_tick:1",
                admission_repository=durable_job_repository, durable_lease_owner="strategist-runner",
                durable_fencing_token=fence)
            assert json.loads(raw)["reasoning"] == "fine"
            # Same call as legacy, exactly.
            assert requests == [{"temperature": 0.2, "max_tokens": 512, "runtime_path": "strategist_agent",
                                 "timeout": 60}]
            # A tick whose lease moved on (another attempt) can never adopt a decision.
            fence2, _ = await tick("strategist_tick:2")
            with pytest.raises(GuardianBlocked, match="guardian_parent_inactive"):
                await run_strategist_decision_completion(
                    context_block="Time: noon", durable_job_id="strategist_tick:2",
                    admission_repository=durable_job_repository, durable_lease_owner="strategist-runner",
                    durable_fencing_token=fence2 + 1)
    assert len(requests) == 1  # the fenced tick made no model call


@needs_cordis
@pytest.mark.asyncio
async def test_a_blocked_background_job_is_persisted_announced_once_and_cleared(accounting_db, monkeypatch, tmp_path):  # noqa: F811
    deny_sockets(monkeypatch)
    from config.settings import settings
    from src.scheduler import background_inference
    from src.scheduler.guardian_runtime import report_guardian_blocked, report_guardian_succeeded
    monkeypatch.setattr(settings, "workspace_dir", str(tmp_path))
    announced, events = [], []

    async def deliver(message, **_kwargs):
        announced.append(message.content)

    async def scheduler_event(**kwargs):
        events.append(kwargs)
    monkeypatch.setattr("src.observer.delivery.deliver_or_queue", deliver)
    monkeypatch.setattr("src.audit.runtime.log_scheduler_job_event", scheduler_event)
    blocked = GuardianBlocked("cordis_host_unavailable")
    for _ in range(3):
        await report_guardian_blocked("daily_briefing", blocked)
    assert len(announced) == 1 and "Daily briefing is blocked" in announced[0]
    assert len(events) == 1 and events[0]["outcome"] == "blocked"  # Activity at most hourly
    # The block survives a restart (the in-memory status is reloaded from disk).
    background_inference._status_loaded_from = None
    row = next(job for job in background_inference.background_inference_status()["jobs"]
               if job["job_id"] == "daily_briefing")
    assert row["blocked"] is True and row["last_skip"]["reason"] == "cordis_host_unavailable"
    assert row["last_skip"]["recovery"] == blocked.recovery
    report_guardian_succeeded("daily_briefing")
    row = next(job for job in background_inference.background_inference_status()["jobs"]
               if job["job_id"] == "daily_briefing")
    assert row["blocked"] is False
    await report_guardian_blocked("daily_briefing", blocked)
    assert len(announced) == 2  # a new transition into blocked is announced again


@needs_cordis
@pytest.mark.asyncio
async def test_existing_chat_turn_ids_survive_the_presets_upgrade(accounting_db, real_auth, monkeypatch):  # noqa: F811
    """A #1019 database without the new rows keeps its turns; new turns bind the presets owner."""
    deny_sockets(monkeypatch)
    from sqlalchemy import delete
    from src.db.engine import get_session
    from src.db.models import RuntimeCompositionState
    from src.runtime_plugins.agent_turns import CordisTurn, TurnBlocked, chat_runtime_status
    from src.runtime_plugins.ownership import DOMAIN_INTRODUCTIONS, RuntimeCompositionBinding, introduce_domains_sync
    from tests.cordis_agent_loop import cutover_agent_loop
    from tests.test_cordis_agent_loop import _session
    async with research_host(durable_job_repository) as host:
        await cutover_agent_loop(host)
        operator = await _session()
        before, _ = await CordisTurn.admit(operator=operator, conversation_session_id="chat-1", message_id="m-old",
                                           profile="agent", timeout_seconds=60, host=host, jobs=durable_job_repository)
        async with get_session() as db:
            await db.execute(delete(RuntimeCompositionState).where(
                RuntimeCompositionState.runtime_domain.in_(list(DOMAIN_INTRODUCTIONS))))
            await db.commit()
        status = await chat_runtime_status(host)
        assert status["loop"] == "blocked" and status["reason"] == "composition_dependency_missing"
        with pytest.raises(TurnBlocked):
            await CordisTurn.admit(operator=operator, conversation_session_id="chat-1", message_id="m-gap",
                                   profile="agent", timeout_seconds=60, host=host, jobs=durable_job_repository)
        async with get_session() as db:
            await db.run_sync(lambda session: introduce_domains_sync(session.connection()))
            await db.commit()
        assert (await chat_runtime_status(host))["loop"] == "cordis.agent-loop.v1"
        after, _ = await CordisTurn.admit(operator=operator, conversation_session_id="chat-1", message_id="m-new",
                                          profile="agent", timeout_seconds=60, host=host, jobs=durable_job_repository)
        old_job = await durable_job_repository.get_job(before.job_id)
        assert old_job["job_id"] == before.job_id  # unchanged identity and row
        async with get_session() as db:
            run = await durable_job_repository._fetch(db, after.job_id)
            binding = RuntimeCompositionBinding.from_json(run.composition_binding_json)
        presets = [item for item in binding.dependency_vector if item.runtime_domain == PRESETS_DOMAIN]
        assert [(item.owner_kind, item.epoch) for item in presets] == [("legacy", 1)]
        assert after.preset_id == "chat.agent" and after.preset_digest == preset_digest("chat.agent")
        # A turn admitted by #1019 bound no presets owner: it is still read, validated and
        # recovered exactly as written, and no new admission can produce that shape.
        legacy_shape = RuntimeCompositionBinding(
            binding.runtime_domain, binding.origin_method, binding.native_branch, binding.allowed_child_methods,
            tuple(item for item in binding.dependency_vector if item.runtime_domain != PRESETS_DOMAIN),
            binding.host_package_digest, binding.host_composition_digest, binding.goal_bound)
        assert legacy_shape.presets_bound is False and binding.presets_bound is True
        from src.db.models import WorkflowRunState
        from sqlalchemy import update
        async with get_session() as db:
            await db.execute(update(WorkflowRunState).where(WorkflowRunState.run_identity == before.job_id)
                             .values(composition_binding_json=legacy_shape.to_json()))
            await db.commit()
        from src.runtime_plugins.ownership import CompositionBindingError, validate_invocation
        async with get_session() as db:
            run = await durable_job_repository._fetch(db, before.job_id)
            read = RuntimeCompositionBinding.from_json(run.composition_binding_json)
            await validate_invocation(db, read)
        assert read == legacy_shape
        from datetime import datetime, timedelta, timezone
        recovered = await durable_job_repository.recover_stale_job(
            before.job_id, now=datetime.now(timezone.utc) + timedelta(hours=1))
        assert recovered["status"] in {"failed", "blocked", "unknown_external_effect"}
        # The pre-presets shape is the chat step's alone: a guardian step without presets is invalid.
        from src.runtime_plugins.ownership import closure_dependencies, method_closure
        guardian_vector = tuple(item for item in legacy_shape.dependency_vector
                                if item.runtime_domain in closure_dependencies("guardian.startTurn", "guardian"))
        with pytest.raises(CompositionBindingError):
            RuntimeCompositionBinding(GUARDIAN_DOMAIN, "guardian.startTurn", "guardian",
                                      method_closure("guardian.startTurn", "guardian"), guardian_vector)


from tests.test_research_native_vertical import real_auth  # noqa: E402,F401


def _report_patches(job: str):
    """Today's inputs for one scheduled report, from its own test module's fixtures."""
    from unittest.mock import AsyncMock, MagicMock, patch
    from src.observer.context import CurrentContext
    context = MagicMock()
    if job == "daily_briefing":
        from src.scheduler.jobs.daily_briefing import run_daily_briefing
        from tests.test_daily_briefing import _hybrid_memories
        context.refresh = AsyncMock(return_value=CurrentContext(time_of_day="morning", day_of_week="Monday",
                                                                is_working_hours=True,
                                                                active_goals_summary="3 active goals"))
        return run_daily_briefing, (
            patch("src.observer.manager.context_manager", context),
            patch("src.memory.soul.read_soul", return_value="# Soul\nName: Hero"),
            patch("src.scheduler.jobs.daily_briefing.retrieve_hybrid_memory",
                  new=AsyncMock(return_value=_hybrid_memories(("User likes mornings",)))))
    from src.scheduler.jobs.evening_review import run_evening_review
    context.refresh = AsyncMock(return_value=CurrentContext(time_of_day="evening", day_of_week="Monday",
                                                            is_working_hours=False,
                                                            active_goals_summary="2 active goals"))
    return run_evening_review, (
        patch("src.observer.manager.context_manager", context),
        patch("src.memory.soul.read_soul", return_value="# Soul\nName: Hero"),
        patch("src.scheduler.jobs.evening_review._count_messages_today", AsyncMock(return_value=(15, False))),
        patch("src.scheduler.jobs.evening_review._get_completed_goals_today",
              AsyncMock(return_value=(["Exercise"], False))))


@needs_cordis
@pytest.mark.asyncio
@pytest.mark.parametrize("job", ["daily_briefing", "evening_review"])
async def test_scheduled_report_parity_on_the_legacy_and_cordis_guardian_owners(  # noqa: F811
        accounting_db, monkeypatch, mocked_canonical_inference_context, job):
    """One scripted completion, two owners: the same provider request and the same delivered report."""
    deny_sockets(monkeypatch)
    from contextlib import ExitStack
    from unittest.mock import AsyncMock, patch
    from tests.test_daily_briefing import _mock_litellm_response
    runs = {}
    async with guardian_host(cordis=False) as host:
        monkeypatch.setattr("src.runtime_plugins.bridge.cordis_host", host)
        for owner in ("legacy", "cordis"):
            if owner == "cordis":
                await cutover(host, GUARDIAN_DOMAIN)
            requests, delivered = [], AsyncMock()

            def completion(**kwargs):
                requests.append({key: kwargs.get(key) for key in ("messages", "temperature", "max_tokens")})
                return _mock_litellm_response(f"Hello Hero, here is your {job}.")
            run, patches = _report_patches(job)
            with ExitStack() as stack:
                for item in (*patches, patch("litellm.completion", side_effect=completion),
                             patch("src.observer.delivery.deliver_or_queue", delivered)):
                    stack.enter_context(item)
                async with service_principal(job):
                    await run()
            delivered.assert_called_once()
            message = delivered.call_args[0][0]
            runs[owner] = (requests, message.content, message.intervention_type, delivered.call_args[1])
    assert runs["legacy"] == runs["cordis"]
    assert len(runs["cordis"][0]) == 1
    turns = await _jobs_of_kind("cordis_guardian_turn_v1")
    assert [turn["status"] for turn in turns] == ["succeeded"]  # only the Cordis-owned run made a turn


# ---------------------------------------------------------------- boot cutover order

@needs_cordis
@pytest.mark.asyncio
async def test_boot_cuts_presets_over_first_and_introduces_new_domains_into_a_1019_inventory(  # noqa: F811
        accounting_db, real_auth):
    """A #1019 install (agent-loop already Cordis, no #1020 rows) gains both rows at boot, presets first."""
    from sqlalchemy import delete, select
    from src.db.engine import get_session
    from src.db.models import AuditEvent, RuntimeCompositionState
    from src.runtime_plugins.agent_turns import AGENT_LOOP_DOMAIN, chat_runtime_status
    from src.runtime_plugins.cutover import cutover_manifest, run_boot_cutover
    from src.runtime_plugins.ownership import DOMAIN_INTRODUCTIONS, inventory
    from tests.cordis_agent_loop import cutover_agent_loop
    manifest = cutover_manifest()
    assert [entry.domain for entry in manifest] == [PRESETS_DOMAIN, AGENT_LOOP_DOMAIN, GUARDIAN_DOMAIN,
                                                     "seraph.delegation.v1"]
    # Every turn binds the presets, so every later domain waits for them.
    assert all(PRESETS_DOMAIN in entry.prerequisites for entry in manifest[1:])
    async with research_host(durable_job_repository) as host:
        loop_epoch = await cutover_agent_loop(host)
        async with get_session() as db:
            await db.execute(delete(RuntimeCompositionState).where(
                RuntimeCompositionState.runtime_domain.in_(list(DOMAIN_INTRODUCTIONS))))
            await db.commit()
        receipts = await run_boot_cutover(host)
        assert [item["runtime_domain"] for item in receipts] == [PRESETS_DOMAIN, AGENT_LOOP_DOMAIN, GUARDIAN_DOMAIN,
                                                                 "seraph.delegation.v1"]
        async with get_session() as db:
            rows = await inventory(db)
            introduced = (await db.execute(select(AuditEvent.details_json).where(
                AuditEvent.event_type == "runtime_composition_domain_introduced"))).scalars().all()
        assert sorted(json.loads(item)["runtime_domain"] for item in introduced) == sorted(DOMAIN_INTRODUCTIONS)
        for domain in DOMAIN_INTRODUCTIONS:
            # Introduced as legacy@1, then cut over at epoch 2 on this boot's proof.
            assert (rows[domain].owner_kind, rows[domain].epoch, rows[domain].state) == ("cordis", 2, "ready")
        assert rows[AGENT_LOOP_DOMAIN].epoch == loop_epoch  # the cut-over chat owner is untouched
        assert (await chat_runtime_status(host))["loop"] == "cordis.agent-loop.v1"
        assert (await guardian_runtime_status(host))["loop"] == "cordis.guardian.v1"
        assert await run_boot_cutover(host) == []


# ---------------------------------------------------------------- blocked and fenced turns

@needs_cordis
@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["blocked", "missing"])
async def test_a_presets_owner_that_is_not_ready_blocks_every_guardian_call(accounting_db, monkeypatch, change):  # noqa: F811
    deny_sockets(monkeypatch)
    from sqlalchemy import delete, update
    from src.db.engine import get_session
    from src.db.models import RuntimeCompositionState
    calls = []

    async def call():
        calls.append(1)
        return _response("must not run")
    async with guardian_host() as host:
        async with get_session() as db:
            where = RuntimeCompositionState.runtime_domain == PRESETS_DOMAIN
            await db.execute(update(RuntimeCompositionState).where(where).values(state="blocked")
                             if change == "blocked" else delete(RuntimeCompositionState).where(where))
            await db.commit()
        status = await guardian_runtime_status(host)
        expected = "agent_presets_owner_blocked" if change == "blocked" else "composition_dependency_missing"
        assert (status["loop"], status["reason"]) == ("blocked", expected) and status["recovery"]
        async with service_principal("daily_briefing"):
            with pytest.raises(GuardianBlocked) as blocked:
                await guardian_completion("guardian.daily_briefing", call, timeout_seconds=30,
                                          runtime_path="daily_briefing", temperature=0.6, max_tokens=512, host=host)
        assert blocked.value.reason_code == expected
    assert calls == [] and await _jobs_of_kind("cordis_guardian_turn_v1") == []


@needs_cordis
@pytest.mark.asyncio
async def test_a_restart_mid_turn_never_adopts_the_output(accounting_db, monkeypatch):  # noqa: F811
    """The process restarted while the call ran: stale-lease recovery owns the turn, nothing is used."""
    deny_sockets(monkeypatch)
    from datetime import datetime, timedelta, timezone
    async with guardian_host() as host:
        async def call():
            (turn,) = await _jobs_of_kind("cordis_guardian_turn_v1")
            await durable_job_repository.recover_stale_job(turn["job_id"],
                                                           now=datetime.now(timezone.utc) + timedelta(hours=1))
            return _response("late")
        async with service_principal("weekly_activity_review"):
            with pytest.raises(GuardianBlocked) as blocked:
                await guardian_completion("guardian.weekly_activity_review", call, timeout_seconds=30,
                                          runtime_path="weekly_activity_review", temperature=0.6, max_tokens=1024,
                                          host=host)
    assert blocked.value.reason_code == "guardian_turn_attempt_changed"
    (turn,) = await _jobs_of_kind("cordis_guardian_turn_v1")
    assert turn["status"] != "succeeded"
    assert await _jobs_of_kind("cordis_guardian_step_v1") == []  # no step ran for a recovered turn


@needs_cordis
@pytest.mark.asyncio
async def test_a_disabled_toggle_admits_no_turn_and_unreadable_settings_pause_every_job(  # noqa: F811
        accounting_db, monkeypatch):
    deny_sockets(monkeypatch)
    import asyncio
    from src.scheduler import background_inference
    from src.scheduler.engine import _async_job_wrapper
    calls = []

    async def job():
        async def call():
            calls.append(1)
            return _response("must not run")
        await guardian_completion("guardian.daily_briefing", call, timeout_seconds=30, runtime_path="daily_briefing",
                                  temperature=0.6, max_tokens=512)
    async with guardian_host() as host:
        monkeypatch.setattr("src.runtime_plugins.bridge.cordis_host", host)
        background_inference.write_background_inference_settings({"daily_briefing": False})
        await _async_job_wrapper(job, asyncio.get_running_loop(), job_id="daily_briefing",
                                 allow_model_inference=True)()
        background_inference.background_inference_settings_path().write_text("{not json", encoding="utf-8")
        for job_id in ("daily_briefing", "evening_review", "strategist_tick"):
            await _async_job_wrapper(job, asyncio.get_running_loop(), job_id=job_id, allow_model_inference=True)()
        # The same wrapper runs the job, on the Cordis owner, once the toggle is back on.
        background_inference.background_inference_settings_path().unlink()
        await _async_job_wrapper(job, asyncio.get_running_loop(), job_id="daily_briefing",
                                 allow_model_inference=True)()
    assert calls == [1]
    assert len(await _jobs_of_kind("cordis_guardian_turn_v1")) == 1
    assert len(await _jobs_of_kind("cordis_guardian_step_v1")) == 1


# ---------------------------------------------------------------- the strategist tick, both owners

STRATEGIST_SCENARIOS = {
    "intervene": ('{"should_intervene": true, "content": "Time to refocus.", "intervention_type": "advisory", '
                  '"urgency": 3, "reasoning": "Focus drift"}'),
    "no_intervene": ('{"should_intervene": false, "content": "", "intervention_type": "nudge", "urgency": 0, '
                     '"reasoning": "fine"}'),
    "parse_failure": "this is not a decision",
    "timeout": None,
}


async def _strategist_tick(monkeypatch, occurrence: str, content: str | None) -> tuple:
    """Run one real tick with a scripted decision completion; returns what it asked, delivered and recorded."""
    import asyncio
    from src.observer.user_state import DeliveryDecision
    from src.scheduler.jobs import strategist_tick
    requests, delivered = [], []

    async def scripted(**kwargs):
        requests.append({key: kwargs[key] for key in ("temperature", "max_tokens", "runtime_path", "timeout")}
                        | {"roles": [message["role"] for message in kwargs["messages"]]})
        if content is None:
            raise asyncio.TimeoutError()
        return _response(content)

    async def deliver(message, **_kwargs):
        delivered.append(message.content)
        return DeliveryDecision.deliver
    monkeypatch.setattr("src.agent.strategist.completion_with_fallback", scripted)
    monkeypatch.setattr("src.observer.delivery.deliver_or_queue", deliver)
    monkeypatch.setattr(strategist_tick, "_occurrence_identity", lambda now=None: occurrence)
    async with service_principal():
        await strategist_tick.run_strategist_tick()
    tick = await durable_job_repository.get_job(occurrence)
    return requests, delivered, tick["status"], tick.get("failure_reason")


@needs_cordis
@pytest.mark.asyncio
async def test_strategist_tick_is_identical_on_both_owners_and_blocked_without_cordis(accounting_db, monkeypatch):  # noqa: F811
    deny_sockets(monkeypatch)
    from unittest.mock import AsyncMock
    from src.scheduler.jobs import strategist_tick
    from tests.test_strategist_tick import _make_guardian_state
    monkeypatch.setattr(strategist_tick, "build_guardian_state", AsyncMock(return_value=_make_guardian_state()))
    outcomes = {}
    async with guardian_host(cordis=False) as host:
        monkeypatch.setattr("src.runtime_plugins.bridge.cordis_host", host)
        for owner in ("legacy", "cordis"):
            if owner == "cordis":
                await cutover(host, GUARDIAN_DOMAIN)
            for scenario, content in STRATEGIST_SCENARIOS.items():
                outcomes[(owner, scenario)] = await _strategist_tick(
                    monkeypatch, f"strategist_tick:{owner}-{scenario}", STRATEGIST_SCENARIOS[scenario])
        for scenario in STRATEGIST_SCENARIOS:
            assert outcomes[("legacy", scenario)] == outcomes[("cordis", scenario)], scenario
        assert outcomes[("cordis", "intervene")][1] == ["Time to refocus."]
        assert outcomes[("cordis", "no_intervene")][1] == []
        turns = await _jobs_of_kind("cordis_guardian_turn_v1")
        assert len(turns) == len(STRATEGIST_SCENARIOS)  # the Cordis owner adds lineage, nothing else
        # Cordis is the owner but its runtime is down: blocked, no model call, no decision delivered.
        await host.stop()
        requests, delivered, status, reason = await _strategist_tick(
            monkeypatch, "strategist_tick:cordis-down", STRATEGIST_SCENARIOS["intervene"])
        await host.start()
    assert requests == [] and status == "blocked" and reason.startswith("cordis_")
    assert len(delivered) == 1 and "Strategist tick is blocked" in delivered[0]  # the one operator notice


# ---------------------------------------------------------------- memory consolidation: two admissions

@needs_cordis
@pytest.mark.asyncio
async def test_a_flush_inside_an_operator_turn_is_owned_by_that_root_and_a_revoked_root_adopts_nothing(  # noqa: F811
        accounting_db, real_auth, monkeypatch):
    deny_sockets(monkeypatch)
    from datetime import datetime, timezone
    from sqlalchemy import update
    from src.auth.service import bind_operator_principal
    from src.db.engine import get_session
    from src.db.models import OperatorSession
    from tests.test_cordis_agent_loop import _session
    async with guardian_host() as host:
        operator = await _session()
        root = bind_operator_principal(operator, "chat-1")

        async def call():
            return _response('{"facts": []}')
        response = await guardian_completion("guardian.memory_consolidation", call, timeout_seconds=30,
                                             runtime_path="session_consolidation", temperature=0.3, max_tokens=1024,
                                             root=root, host=host)
        assert response.choices[0].message.content == '{"facts": []}'
        (turn,) = await _jobs_of_kind("cordis_guardian_turn_v1")
        (step,) = await _jobs_of_kind("cordis_guardian_step_v1")
        async with get_session() as db:
            for job in (turn, step):
                run = await durable_job_repository._fetch(db, job["job_id"])
                assert (run.owner_kind, run.owner_principal_id, run.service_id) == (
                    "user", operator.principal.principal_id, None)
                assert run.operator_session_id == run.session_id == operator.session_id
        assert turn["status"] == step["status"] == "succeeded"

        async def revoking_call():
            # The operator signs out while the consolidation call runs.
            async with get_session() as db:
                await db.execute(update(OperatorSession).where(OperatorSession.id == operator.session_id)
                                 .values(revoked_at=datetime.now(timezone.utc)))
                await db.commit()
            return _response('{"facts": ["late"]}')
        with pytest.raises(GuardianBlocked) as blocked:
            await guardian_completion("guardian.memory_consolidation", revoking_call, timeout_seconds=30,
                                      runtime_path="session_consolidation", temperature=0.3, max_tokens=1024,
                                      root=root, host=host)
        assert blocked.value.reason_code == "native_original_root_inactive"
        # A revoked Root admits nothing more.
        with pytest.raises(GuardianBlocked):
            await guardian_completion("guardian.memory_consolidation", call, timeout_seconds=30,
                                      runtime_path="session_consolidation", temperature=0.3, max_tokens=1024,
                                      root=root, host=host)
        # Revoked after the sequencer decided but before adoption: the settlement CAS refuses it.
        from src.runtime_plugins.guardian_turns import GuardianTurn
        decide = GuardianTurn.decide_final
        second = await _session()

        async def decide_then_revoke(self, digest):
            await decide(self, digest)
            async with get_session() as db:
                await db.execute(update(OperatorSession).where(OperatorSession.id == second.session_id)
                                 .values(revoked_at=datetime.now(timezone.utc)))
                await db.commit()
        monkeypatch.setattr(GuardianTurn, "decide_final", decide_then_revoke)
        with pytest.raises(GuardianBlocked) as late:
            await guardian_completion("guardian.memory_consolidation", call, timeout_seconds=30,
                                      runtime_path="session_consolidation", temperature=0.3, max_tokens=1024,
                                      root=bind_operator_principal(second, "chat-1"), host=host)
        assert late.value.reason_code == "native_original_root_inactive"
    turns = await _jobs_of_kind("cordis_guardian_turn_v1")
    assert sorted(turn["status"] for turn in turns) == ["blocked", "blocked", "succeeded"]


@needs_cordis
@pytest.mark.asyncio
async def test_a_blocked_guardian_domain_skips_every_flush_and_keeps_its_fingerprint_uncached(  # noqa: F811
        accounting_db, monkeypatch, mocked_canonical_inference_context):
    deny_sockets(monkeypatch)
    import asyncio
    from unittest.mock import patch
    from src.agent.session import session_manager
    from src.memory import flush
    from src.scheduler import background_inference
    flush._reset_memory_flush_state()
    # The flush, the profile read and the session history all use this test's database.
    for target in ("src.memory.flush.get_session", "src.profile.service.get_db"):
        monkeypatch.setattr(target, accounting_db[2].accounting_sessions)
    await session_manager.get_or_create("s-flush")
    await session_manager.add_message("s-flush", "user", "My name is Alice and I work at ACME Corp as an engineer.")
    await session_manager.add_message("s-flush", "assistant", "Nice to meet you, Alice! ACME Corp sounds great.")
    async with guardian_host() as host:
        monkeypatch.setattr("src.runtime_plugins.bridge.cordis_host", host)
        await host.stop()
        with patch("litellm.completion") as completion:
            assert await flush.flush_session_memory("s-flush", trigger="post_response") is False
            # The synchronous workflow trigger never raises into its workflow.
            assert await asyncio.to_thread(flush.flush_session_memory_sync, session_id="s-flush",
                                           trigger="workflow_completed") is False
        completion.assert_not_called()
        assert flush._last_flushed_session_fingerprints == {}  # a later catch-up consolidates once ready
        row = next(job for job in background_inference.background_inference_status()["jobs"]
                   if job["job_id"] == "memory_consolidation")
        assert row["blocked"] is True and row["last_skip"]["reason"].startswith("cordis_")
        await host.start()
    assert await _jobs_of_kind("cordis_guardian_turn_v1") == []


# ---------------------------------------------------------------- one broker, one ledger

class _ProviderBoundary(httpx.AsyncBaseTransport, httpx.BaseTransport):
    """httpx transport at the provider boundary: canaries and decisions answered locally, nothing leaves."""

    def __init__(self, calls: list):
        self.calls = calls

    def _answer(self, request) -> bytes:
        assert request.url.host == "openrouter.ai" and request.url.path == "/api/v1/chat/completions"
        body = json.loads(request.content)
        self.calls.append(body)
        if len(body["messages"]) == 1:
            content = '{"ok":true}' if "response_format" in body else "CANARY_OK"
        else:
            content = STRATEGIST_SCENARIOS["no_intervene"]
        return json.dumps(dict(id=f"intercepted-{len(self.calls)}",
                               choices=[dict(message=dict(role="assistant", content=content))],
                               usage=dict(cost="0.000002", prompt_tokens=10, completion_tokens=10))).encode()

    async def handle_async_request(self, request):
        from tests.test_research_native_vertical import ResponseBytes
        return httpx.Response(200, request=request, headers={"content-type": "application/json"},
                              stream=ResponseBytes(self._answer(request)))

    def handle_request(self, request):
        return httpx.Response(200, request=request, headers={"content-type": "application/json"},
                              content=self._answer(request))


async def _claimed_tick(occurrence: str) -> int:
    from datetime import datetime, timedelta, timezone
    from src.workflows.job_runtime import DurableJobIdentity, DurableJobSpec
    spec = DurableJobSpec(identity=DurableJobIdentity(
        job_id=occurrence, owner_kind="service", owner_principal_id="service:scheduler:strategist_tick",
        job_kind="strategist_tick", capability_version="1", idempotency_scope="scheduler-occurrence",
        idempotency_key=occurrence), inputs={"trigger": "interval"}, declared_authority={
            "principal": "service:scheduler:strategist_tick", "owner_kind": "service",
            "service_id": "service:strategist"}, service_id="service:strategist",
        deadline_at=datetime.now(timezone.utc) + timedelta(seconds=120), max_attempts=1)
    await durable_job_repository.admit_job(spec)
    await durable_job_repository.queue_job(occurrence)
    claimed = await durable_job_repository.claim_job(occurrence, owner="strategist-runner", lease_seconds=90)
    return int(claimed["lease"]["fencing_token"])


@needs_cordis
@pytest.mark.asyncio
async def test_strategist_inference_lands_in_the_one_broker_and_ledger_on_both_owners(  # noqa: F811
        accounting_db, real_auth, monkeypatch):
    """The real governed path, offline: the same broker, ledger row, owner and tick binding on either loop."""
    deny_sockets(monkeypatch)
    from dataclasses import replace
    from fastapi import FastAPI
    from sqlalchemy import select
    from src.agent.strategist import run_strategist_decision_completion
    from src.api import auth, model_fabric_settings
    from src.auth.middleware import OperatorAuthMiddleware
    from src.db.engine import get_session
    from src.db.models import InferenceCostReservation
    from src.model_fabric import remote_inference_admission
    from src.model_fabric.configuration import write_model_fabric_configuration
    from tests.test_inference_accounting import setup_configuration
    monkeypatch.setattr("src.model_fabric.execution.gpu_admission_broker",
                        remote_inference_admission.remote_inference_admission_broker)
    configured = setup_configuration()
    write_model_fabric_configuration(replace(model_fabric_settings._setup_configuration(
        replace(configured.openrouter_setup, timeout_seconds=30, capabilities=("text", "structured_output")),
        profiles=(), policies=()), egress_revision=configured.egress_revision + 1))
    await durable_job_repository.configure_inference_accounting(1000)
    calls = []
    original_client, original_sync_client = httpx.AsyncClient, httpx.Client

    def clients(**kwargs):
        if kwargs.get("transport") is None:
            kwargs["transport"] = _ProviderBoundary(calls)
        return original_client(**kwargs)

    def sync_clients(**kwargs):
        if kwargs.get("transport") is None:
            kwargs["transport"] = _ProviderBoundary(calls)
        return original_sync_client(**kwargs)
    monkeypatch.setattr(httpx, "AsyncClient", clients)
    monkeypatch.setattr(httpx, "Client", sync_clients)
    app = FastAPI()
    app.add_middleware(OperatorAuthMiddleware)
    app.include_router(auth.router, prefix="/api/auth")
    app.include_router(model_fabric_settings.router, prefix="/api")
    async with original_client(transport=httpx.ASGITransport(app=app), base_url="http://test",
                               headers={"origin": "http://localhost:3001"}) as client:
        assert (await client.post("/api/auth/login", json={"password": "research-vertical-private-secret"})).status_code == 200
        for capability in ("text", "structured_output", "latency_ms", "health"):
            proof = await client.post("/api/settings/model-fabric/canary", json=dict(
                profile_id="openrouter", capability=capability, timeout_seconds=30))
            assert proof.status_code == 200 and proof.json()["outcome"] == "passed", proof.text
    canaries = len(calls)
    ledger = {}
    async with guardian_host(cordis=False) as host:
        monkeypatch.setattr("src.runtime_plugins.bridge.cordis_host", host)
        for owner in ("legacy", "cordis"):
            if owner == "cordis":
                await cutover(host, GUARDIAN_DOMAIN)
            occurrence = f"strategist_tick:ledger-{owner}"
            async with service_principal():
                fence = await _claimed_tick(occurrence)
                raw = await run_strategist_decision_completion(
                    context_block="Time: morning", durable_job_id=occurrence,
                    admission_repository=durable_job_repository, durable_lease_owner="strategist-runner",
                    durable_fencing_token=fence)
            assert json.loads(raw)["reasoning"] == "fine"
            async with get_session() as db:
                rows = (await db.execute(select(InferenceCostReservation).where(
                    InferenceCostReservation.job_id == occurrence))).scalars().all()
            ledger[owner] = [(row.runtime_path, row.owner_id, row.state, row.job_fencing_token == fence,
                              row.actual_cost_microusd) for row in rows]
    assert ledger["legacy"] == ledger["cordis"] == [
        ("strategist_agent", "service:scheduler:strategist_tick", ledger["legacy"][0][2], True, 2)]
    decisions = calls[canaries:]
    assert len(decisions) == 2 and decisions[0] == decisions[1]  # the same provider request on both loops
    (turn,) = await _jobs_of_kind("cordis_guardian_turn_v1")
    assert turn["status"] == "succeeded"


@needs_cordis
@pytest.mark.asyncio
async def test_measured_unattended_turn_overhead_p95_over_100_samples(accounting_db, monkeypatch):  # noqa: F811
    """The added Cordis cost of one guardian call on the real path, against #1019's 250 ms per-step budget:
    the turn admission, the one guardian.startTurn step (job, invocation, decision, verification) and the
    fenced settlement. The model call itself is a local no-op."""
    import os
    import statistics
    import time
    from src.runtime_plugins.guardian_turns import GuardianTurn
    deny_sockets(monkeypatch)
    samples = 110

    def p95(values):
        return statistics.quantiles(values, n=100)[94]
    admissions, steps, settlements = [], [], []
    async with guardian_host() as host:
        async with service_principal("daily_briefing"):
            for _ in range(samples):
                began = time.perf_counter()
                turn = await GuardianTurn.admit(preset_id="guardian.daily_briefing", timeout_seconds=60, host=host,
                                                jobs=durable_job_repository)
                admissions.append(time.perf_counter() - began)
                began = time.perf_counter()
                await turn.decide_final("a" * 64)
                steps.append(time.perf_counter() - began)
                began = time.perf_counter()
                assert await turn.settle("succeeded")
                settlements.append(time.perf_counter() - began)
    print("MEASURED guardian n=%d p50/p95 ms: admission %.1f/%.1f, step %.1f/%.1f, settlement %.1f/%.1f" % (
        samples, statistics.median(admissions) * 1000, p95(admissions) * 1000, statistics.median(steps) * 1000,
        p95(steps) * 1000, statistics.median(settlements) * 1000, p95(settlements) * 1000))
    # Non-gating by default, as #1019: asserted only on a quiet host with SERAPH_CORDIS_LATENCY_BUDGET=1.
    if os.environ.get("SERAPH_CORDIS_LATENCY_BUDGET") == "1":
        assert p95(admissions) <= 0.250 and p95(steps) <= 0.250 and p95(settlements) <= 0.250
