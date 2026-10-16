"""#1019 production cutover: the default Cordis owner for chat, operator rollback
and re-cutover, and the legacy-turn drain gate. Real SQLite, real fenced owner
transitions and, where a ready host is needed, the real reviewed child."""
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
from src.runtime_plugins.cutover import (CUTOVER_EVENT, OPERATOR_EVENT, legacy_turns, pending_intents,
                                         record_operator_intent, run_boot_cutover)
from src.runtime_plugins.ownership import CompositionBindingError
from src.workflows.job_runtime import DurableJobRepository

pytestmark = [needs_cordis, pytest.mark.asyncio]


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
    [receipt] = await run_boot_cutover(stopped)
    assert receipt["action"] == "cut_over" and receipt["state"] == "blocked"
    status = await chat_runtime_status(stopped)
    assert status["loop"] == "blocked" and status["owner_kind"] == "cordis"
    assert status["reason"].startswith("cordis_") and status["recovery"]
    jobs = DurableJobRepository()
    async with research_host(jobs) as host:
        # A later boot with a ready runtime completes the same cutover, never a second one.
        [completed] = await run_boot_cutover(host)
        assert completed["action"] == "completed" and completed["state"] == "ready"
        status = await chat_runtime_status(host)
        assert (status["loop"], status["owner_kind"]) == ("cordis.agent-loop.v1", "cordis")
        assert await run_boot_cutover(host) == []
        assert len(await _events(CUTOVER_EVENT)) == 1


async def test_operator_intents_apply_at_boot_rollback_is_sticky_and_re_cutover_takes_a_higher_epoch(accounting_db, real_auth):  # noqa: F811
    jobs = DurableJobRepository()
    async with research_host(jobs) as host:
        await run_boot_cutover(host)
        cut = (await chat_runtime_status(host))["owner_epoch"]
        # The serving process only records the intent; nothing moves until the next boot.
        await record_operator_intent(AGENT_LOOP_DOMAIN, "legacy", expected_epoch=cut, actor="operator:test")
        assert (await chat_runtime_status(host))["loop"] == "cordis.agent-loop.v1"
        assert (await pending_intents())[AGENT_LOOP_DOMAIN]["target"] == "legacy"
        [applied] = await run_boot_cutover(host)
        assert (applied["action"], applied["owner_kind"], applied["state"]) == ("intent_applied", "legacy", "ready")
        assert applied["epoch"] == cut + 1 and await pending_intents() == {}
        assert (await chat_runtime_status(host))["loop"] == "legacy.smolagents"
        # Boot never re-cuts a domain an operator moved.
        assert await run_boot_cutover(host) == []
        assert (await chat_runtime_status(host))["loop"] == "legacy.smolagents"
        await record_operator_intent(AGENT_LOOP_DOMAIN, "cordis", expected_epoch=cut + 1, actor="operator:test")
        [again] = await run_boot_cutover(host)
        assert (again["owner_kind"], again["state"], again["epoch"]) == ("cordis", "ready", cut + 2)
        assert (await chat_runtime_status(host))["loop"] == "cordis.agent-loop.v1"
        # A stale request is refused now; an intent overtaken before boot is rejected visibly then.
        with pytest.raises(CompositionBindingError) as stale:
            await record_operator_intent(AGENT_LOOP_DOMAIN, "legacy", expected_epoch=cut, actor="operator:test")
        assert stale.value.reason_code == "composition_owner_changed"
        assert [event["outcome"] for event in await _events(OPERATOR_EVENT)] == ["applied", "applied"]


async def test_a_boot_step_never_switches_under_a_live_legacy_turn_and_retries(accounting_db, real_auth):  # noqa: F811
    jobs = DurableJobRepository()
    async with research_host(jobs) as host:
        legacy_turns.enter(AGENT_LOOP_DOMAIN)
        try:
            [deferred] = await run_boot_cutover(host)
        finally:
            legacy_turns.leave(AGENT_LOOP_DOMAIN)
        assert deferred == {"runtime_domain": AGENT_LOOP_DOMAIN, "action": "deferred",
                            "reason": "composition_not_quiescent"}
        # Nothing switched and no marker was written; the next boot retries.
        assert (await chat_runtime_status(host))["loop"] == "legacy.smolagents" and await _events(CUTOVER_EVENT) == []
        with legacy_turns.quiesced(AGENT_LOOP_DOMAIN):
            with pytest.raises(CompositionBindingError) as closed:
                legacy_turns.enter(AGENT_LOOP_DOMAIN)
            assert closed.value.reason_code == "composition_transition_in_progress"
        [cut] = await run_boot_cutover(host)
        assert (cut["action"], cut["state"]) == ("cut_over", "ready")


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
            requested = await client.post(path, json=body)
            assert requested.status_code == 200, requested.text
            assert requested.json()["restart_required"] is True
            assert requested.json()["domains"] == [{"runtime_domain": AGENT_LOOP_DOMAIN, "prerequisites": [],
                                                    "owner_kind": "legacy", "epoch": 1, "state": "ready",
                                                    "pending_target": "cordis"}]
            # The next boot applies it.
            [applied] = await run_boot_cutover(host)
            assert applied["action"] == "intent_applied" and applied["owner_kind"] == "cordis"
            listed = await client.get("/api/runtime/composition/domains")
            assert listed.json()["domains"][0]["owner_kind"] == "cordis"
            assert listed.json()["domains"][0]["pending_target"] is None
