"""#1019 production cutover: the default Cordis owner for chat, live operator
rollback and re-cutover, prerequisites, the owner lease, digest refresh and the
legacy-turn drain gate. Real SQLite, real fenced owner transitions and, where a
ready host is needed, the real reviewed child."""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from tests.cordis_research import needs_cordis, research_host, seed_composition_inventory
from tests.test_inference_accounting import accounting_db  # noqa: F401
from tests.test_research_native_vertical import real_auth  # noqa: F401
from src.runtime_plugins.agent_turns import AGENT_LOOP_DOMAIN, chat_runtime_status
from src.runtime_plugins.bridge import CordisHost
from src.runtime_plugins.cutover import (CUTOVER_EVENT, OPERATOR_EVENT, LegacyTurnGate, legacy_turns,
                                         run_boot_cutover, transition_domain)
from src.runtime_plugins.guardian_turns import GUARDIAN_DOMAIN
from src.runtime_plugins.ownership import PRESETS_DOMAIN, CompositionBindingError
from src.workflows.job_runtime import DurableJobRepository

pytestmark = [needs_cordis, pytest.mark.asyncio]


# #1020 adds the presets (cut over first; agent-loop's prerequisite) and guardian domains.
MANIFEST = [PRESETS_DOMAIN, AGENT_LOOP_DOMAIN, GUARDIAN_DOMAIN, "seraph.delegation.v1", "seraph.board-workers.v1"]


def _by_domain(receipts):
    return {receipt["runtime_domain"]: receipt for receipt in receipts}


async def _events(event_type):
    from src.db.engine import get_session
    from src.db.models import AuditEvent
    async with get_session() as db:
        return [json.loads(row) for row in (await db.execute(select(AuditEvent.details_json).where(
            AuditEvent.event_type == event_type))).scalars()]


async def test_fresh_workspace_cuts_chat_over_once_and_a_missing_runtime_blocks_with_its_fix(accounting_db, real_auth):  # noqa: F811
    await seed_composition_inventory()
    # Cordis not built or not running: the domain is cut over but blocked with the host's fix.
    stopped = CordisHost()
    receipts = _by_domain(await run_boot_cutover(stopped))
    assert list(receipts) == MANIFEST
    assert all(item["action"] == "cut_over" and item["state"] == "blocked" for item in receipts.values())
    status = await chat_runtime_status(stopped)
    assert status["loop"] == "blocked" and status["owner_kind"] == "cordis"
    assert status["reason"].startswith("cordis_") and status["recovery"]
    jobs = DurableJobRepository()
    async with research_host(jobs) as host:
        # A later boot with a ready runtime completes the same cutover, never a second one.
        completed = _by_domain(await run_boot_cutover(host))
        assert list(completed) == MANIFEST
        assert all(item["action"] == "completed" and item["state"] == "ready" for item in completed.values())
        status = await chat_runtime_status(host)
        assert (status["loop"], status["owner_kind"]) == ("cordis.agent-loop.v1", "cordis")
        assert await run_boot_cutover(host) == []
        assert len(await _events(CUTOVER_EVENT)) == len(MANIFEST)


async def test_rollback_is_sticky_and_re_cutover_takes_a_higher_epoch(accounting_db, real_auth):  # noqa: F811
    jobs = DurableJobRepository()
    async with research_host(jobs) as host:
        await run_boot_cutover(host)
        cut = (await chat_runtime_status(host))["owner_epoch"]
        rolled = await transition_domain(AGENT_LOOP_DOMAIN, "legacy", host=host, actor="operator:test",
                                         expected_epoch=cut)
        assert (rolled["owner_kind"], rolled["state"]) == ("legacy", "ready") and rolled["epoch"] == cut + 1
        assert (await chat_runtime_status(host))["loop"] == "legacy.smolagents"
        # Boot never re-cuts a domain an operator moved.
        assert await run_boot_cutover(host) == []
        assert (await chat_runtime_status(host))["loop"] == "legacy.smolagents"
        again = await transition_domain(AGENT_LOOP_DOMAIN, "cordis", host=host, actor="operator:test",
                                        expected_epoch=cut + 1)
        assert (again["owner_kind"], again["state"], again["epoch"]) == ("cordis", "ready", cut + 2)
        assert (await chat_runtime_status(host))["loop"] == "cordis.agent-loop.v1"
        with pytest.raises(CompositionBindingError) as stale:
            await transition_domain(AGENT_LOOP_DOMAIN, "legacy", host=host, actor="operator:test", expected_epoch=cut)
        assert stale.value.reason_code == "composition_owner_changed"
        assert [event["target"] for event in await _events(OPERATOR_EVENT)] == ["legacy", "cordis"]


async def test_a_transition_never_switches_under_a_live_legacy_turn(accounting_db, real_auth):  # noqa: F811
    jobs = DurableJobRepository()
    async with research_host(jobs) as host:
        legacy_turns.enter(AGENT_LOOP_DOMAIN)
        try:
            receipts = _by_domain(await run_boot_cutover(host))
        finally:
            legacy_turns.leave(AGENT_LOOP_DOMAIN)
        assert receipts[AGENT_LOOP_DOMAIN] == {"runtime_domain": AGENT_LOOP_DOMAIN, "action": "deferred",
                            "reason": "composition_not_quiescent"}
        # The boot step deferred the domain while the turn was live; nothing switched.
        assert (await chat_runtime_status(host))["loop"] == "legacy.smolagents"
        legacy_turns.enter(AGENT_LOOP_DOMAIN)
        with pytest.raises(CompositionBindingError) as refused:
            await transition_domain(AGENT_LOOP_DOMAIN, "cordis", host=host, actor="operator:test")
        assert refused.value.reason_code == "composition_not_quiescent"
        legacy_turns.leave(AGENT_LOOP_DOMAIN)
        # While a transition runs, no new legacy turn starts.
        with legacy_turns.quiesced(AGENT_LOOP_DOMAIN):
            with pytest.raises(CompositionBindingError) as closed:
                legacy_turns.enter(AGENT_LOOP_DOMAIN)
            assert closed.value.reason_code == "composition_transition_in_progress"
        moved = await transition_domain(AGENT_LOOP_DOMAIN, "cordis", host=host, actor="operator:test")
        assert moved["owner_kind"] == "cordis" and moved["state"] == "ready"


async def test_operator_api_requires_auth_confirmation_and_a_manifest_domain(accounting_db, real_auth, monkeypatch):  # noqa: F811
    from src.api import auth, runtime_composition
    from src.auth.middleware import OperatorAuthMiddleware
    from src.runtime_plugins import bridge
    jobs = DurableJobRepository()
    async with research_host(jobs) as host:
        monkeypatch.setattr(bridge, "cordis_host", host)
        app = FastAPI()
        app.add_middleware(OperatorAuthMiddleware)
        app.include_router(auth.router, prefix="/api/auth")
        app.include_router(runtime_composition.router, prefix="/api")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                     headers={"origin": "http://localhost:3001"}) as client:
            path = "/api/runtime/composition/domains/" + AGENT_LOOP_DOMAIN + "/owner"
            body = {"target": "cordis", "expected_epoch": 1, "confirm_domain": AGENT_LOOP_DOMAIN}
            assert (await client.post(path, json=body)).status_code == 401
            assert (await client.post("/api/auth/login", json={"password": "research-vertical-private-secret"})).status_code == 200
            no_origin = await client.post(path, json=body, headers={"origin": ""})
            assert no_origin.status_code == 403
            assert (await client.post(path, json={**body, "confirm_domain": "yes"})).status_code == 422
            unknown = await client.post("/api/runtime/composition/domains/seraph.memory.v1/owner",
                                        json={**body, "confirm_domain": "seraph.memory.v1"})
            assert unknown.status_code == 404
            stale = await client.post(path, json={**body, "expected_epoch": 7})
            assert stale.status_code == 409 and stale.json()["detail"]["code"] == "composition_owner_changed"
            # Chat needs its presets on Cordis first (#1020).
            early = await client.post(path, json=body)
            assert early.status_code == 409 and early.json()["detail"]["code"] == "composition_prerequisite_not_cordis"
            presets = await client.post("/api/runtime/composition/domains/" + PRESETS_DOMAIN + "/owner", json={
                "target": "cordis", "expected_epoch": 1, "confirm_domain": PRESETS_DOMAIN})
            assert presets.status_code == 200, presets.text
            moved = await client.post(path, json=body)
            assert moved.status_code == 200, moved.text
            domains = _by_domain(moved.json()["domains"])
            assert list(domains) == MANIFEST
            assert domains[AGENT_LOOP_DOMAIN] == {"runtime_domain": AGENT_LOOP_DOMAIN,
                                                  "prerequisites": [PRESETS_DOMAIN], "owner_kind": "cordis",
                                                  "epoch": 2, "state": "ready"}
            listed = await client.get("/api/runtime/composition/domains")
            assert _by_domain(listed.json()["domains"])[AGENT_LOOP_DOMAIN]["owner_kind"] == "cordis"
            # A Root whose ownership continuity is under recovery may read owners but not move them.
            from dataclasses import replace
            stable = runtime_composition._operator
            monkeypatch.setattr(runtime_composition, "_operator",
                                lambda request: replace(stable(request), ownership_continuity="rotated"))
            recovering = await client.post(path, json={**body, "target": "legacy", "expected_epoch": 2})
            assert recovering.status_code == 403
            assert recovering.json()["detail"]["code"] == "operator_ownership_recovery_required"
            assert _by_domain((await client.get("/api/runtime/composition/domains")).json()["domains"])[
                AGENT_LOOP_DOMAIN]["epoch"] == 2


def test_overlapping_quiesced_blocks_keep_the_gate_closed_until_the_last_one_ends():
    gate = LegacyTurnGate()
    first, second = gate.quiesced(AGENT_LOOP_DOMAIN), gate.quiesced(AGENT_LOOP_DOMAIN)
    first.__enter__()
    second.__enter__()
    # The first transition ends while the second still runs: the gate stays closed.
    first.__exit__(None, None, None)
    with pytest.raises(CompositionBindingError) as closed:
        gate.enter(AGENT_LOOP_DOMAIN)
    assert closed.value.reason_code == "composition_transition_in_progress"
    second.__exit__(None, None, None)
    gate.enter(AGENT_LOOP_DOMAIN)
    gate.leave(AGENT_LOOP_DOMAIN)
    # Nested blocks reopen only after the outer one exits.
    with gate.quiesced(AGENT_LOOP_DOMAIN):
        with gate.quiesced(AGENT_LOOP_DOMAIN):
            pass
        with pytest.raises(CompositionBindingError):
            gate.enter(AGENT_LOOP_DOMAIN)
    assert gate.closed == {}


async def test_production_transitions_require_the_runtime_owner_lease(accounting_db, real_auth, monkeypatch, tmp_path):  # noqa: F811
    from config.settings import settings
    from src.workspace import production
    from tests.test_workspace_production import _workspace
    jobs = DurableJobRepository()
    async with research_host(jobs) as host:
        monkeypatch.setattr(settings, "deployment_environment", "production")
        assert not production.runtime_workspace_owner_held()
        # Without the lease a production process moves nothing, at boot or live.
        assert await run_boot_cutover(host) == [{"runtime_domain": domain, "action": "deferred",
                                                 "reason": "composition_owner_lease_missing"} for domain in MANIFEST]
        with pytest.raises(CompositionBindingError) as missing:
            await transition_domain(AGENT_LOOP_DOMAIN, "cordis", host=host, actor="operator:test")
        assert missing.value.reason_code == "composition_owner_lease_missing"
        assert (await chat_runtime_status(host))["loop"] == "legacy.smolagents"
        # The lifespan's lease (the real lock) admits boot and live transitions alike.
        with production.runtime_workspace_owner(_workspace(tmp_path)):
            assert production.runtime_workspace_owner_held()
            cut = _by_domain(await run_boot_cutover(host))[AGENT_LOOP_DOMAIN]
            assert (cut["action"], cut["state"]) == ("cut_over", "ready")
            rolled = await transition_domain(AGENT_LOOP_DOMAIN, "legacy", host=host, actor="operator:test",
                                             expected_epoch=cut["epoch"])
            assert rolled["owner_kind"] == "legacy"
        assert not production.runtime_workspace_owner_held()


async def test_transitions_enforce_prerequisites_and_dependents(accounting_db, real_auth, monkeypatch):  # noqa: F811
    from src.runtime_plugins import cutover
    monkeypatch.setattr(cutover, "_MANIFEST", list(cutover._MANIFEST))
    dependent = "seraph.conversation.v1"
    cutover.register_cutover_domain(dependent, prerequisites=(AGENT_LOOP_DOMAIN,))
    jobs = DurableJobRepository()
    async with research_host(jobs) as host:
        # A dependent cannot move to Cordis while its prerequisite is legacy.
        with pytest.raises(CompositionBindingError) as prerequisite:
            await transition_domain(dependent, "cordis", host=host, actor="operator:test")
        assert prerequisite.value.reason_code == "composition_prerequisite_not_cordis"
        await transition_domain(PRESETS_DOMAIN, "cordis", host=host, actor="operator:test")
        await transition_domain(AGENT_LOOP_DOMAIN, "cordis", host=host, actor="operator:test")
        moved = await transition_domain(dependent, "cordis", host=host, actor="operator:test")
        assert moved["owner_kind"] == "cordis"
        # The prerequisite cannot roll back under a Cordis-owned dependent; the dependent first.
        with pytest.raises(CompositionBindingError) as dependent_owned:
            await transition_domain(AGENT_LOOP_DOMAIN, "legacy", host=host, actor="operator:test")
        assert dependent_owned.value.reason_code == "composition_dependent_cordis_owned"
        assert (await chat_runtime_status(host))["owner_kind"] == "cordis"
        await transition_domain(dependent, "legacy", host=host, actor="operator:test")
        rolled = await transition_domain(AGENT_LOOP_DOMAIN, "legacy", host=host, actor="operator:test")
        assert rolled["owner_kind"] == "legacy"


async def test_boot_refreshes_a_stale_cordis_digest_and_status_names_the_drift(accounting_db, real_auth, tmp_path):  # noqa: F811
    from tests.cordis_agent_loop import patched_package
    jobs = DurableJobRepository()
    async with research_host(jobs) as host:
        cut = _by_domain(await run_boot_cutover(host))[AGENT_LOOP_DOMAIN]
        assert (cut["action"], cut["state"]) == ("cut_over", "ready")
    # A rebuilt package (one harmless byte) reports a different reviewed plugin digest.
    variant = patched_package(tmp_path, "return ['settlement'];", "return ['settlement'] ;")
    async with research_host(jobs, package_root=variant) as rebuilt:
        drift = await chat_runtime_status(rebuilt)
        assert (drift["loop"], drift["reason"]) == ("blocked", "agent_loop_composition_drift")
        assert "./manage.sh -e dev local up" in drift["recovery"] and "Docker" in drift["recovery"]
        [refreshed] = [item for item in await run_boot_cutover(rebuilt) if item["runtime_domain"] == AGENT_LOOP_DOMAIN]
        assert (refreshed["action"], refreshed["state"], refreshed["epoch"]) == ("digest_refreshed", "ready",
                                                                                 cut["epoch"] + 1)
        assert (await chat_runtime_status(rebuilt))["loop"] == "cordis.agent-loop.v1"
        assert await run_boot_cutover(rebuilt) == []
        # Still one default cutover marker per domain: a refresh is not a re-cut.
        assert len(await _events(CUTOVER_EVENT)) == len(MANIFEST)


async def test_a_blocked_cordis_owner_recovery_names_local_and_docker_restarts(accounting_db, real_auth):  # noqa: F811
    await seed_composition_inventory()
    stopped = CordisHost()
    await run_boot_cutover(stopped)
    status = await chat_runtime_status(stopped)
    assert status["loop"] == "blocked" and "./manage.sh -e prod up -d --build" in status["recovery"]
