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


from tests.test_research_native_vertical import real_auth  # noqa: E402,F401
