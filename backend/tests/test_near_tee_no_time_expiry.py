"""ADR-035 amendment 2026-11-14 (#1178): no time-based NEAR TEE dispatch guards.

Operator decision: capability checks and the catalog check are a one-time
setup gate. A passing proof or setup receipt stays valid until its binding
changes (model, slot settings / profile hash, credential, configuration
revision), never by age. Background catalog reads are informational: they
update "last confirmed" and raise a Settings warning, and never block.

Every NEAR request is intercepted (``NearFixture``); no live call.
"""
from __future__ import annotations

from dataclasses import replace
import time

import httpx
import pytest

from config.settings import settings
from tests.near_tee_support import near_inference_requests, near_settled_rows
from tests.test_near_tee_text_slot import NearFixture, catalog_record
from tests.test_near_tee_vertical_journeys import (  # noqa: F401
    PLAN,
    _planner_world,
    accounting_db,
    descriptor,
    forbid_external_inference,
    isolated_broker,
    task_input,
)

DAY = 24 * 3600


class FlakyCatalogNear(NearFixture):
    """The NEAR fixture whose public catalog read can fail (HTTP 503)."""

    def __init__(self):
        super().__init__()
        self.catalog_down = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.catalog_down and request.method == "GET" and request.url.raw_path.decode().startswith("/v1/model/"):
            self.requests.append(request)
            return httpx.Response(503, content=b"{}")
        return super().handler(request)


def _age_setup_receipt(seconds: float) -> None:
    """Make the stored setup receipt ``seconds`` old, with a legacy past ``expires_at``."""
    from src.model_fabric.near_tee_catalog import read_catalog_receipts, write_catalog_receipts
    receipts = read_catalog_receipts()
    receipts["text"]["checked_at"] -= seconds
    receipts["text"]["expires_at"] = time.time() - 1  # a pre-#1178 receipt's expiry, long past
    write_catalog_receipts(receipts)


def _text_profile():
    from src.model_fabric.configuration import near_tee_profile_for_slot, read_model_fabric_configuration
    return near_tee_profile_for_slot("text", read_model_fabric_configuration().near_tee.slots["text"])


async def _plan(accounting_db, owner):
    from src.work_board.general_task_planner import GeneralTaskPlanner
    async with accounting_db[2]() as db:
        return await GeneralTaskPlanner().propose(db, owner, task_input(), [descriptor()], 1, "intent")


def test_near_tee_proofs_do_not_expire_by_age_but_other_profiles_still_do():
    from src.model_fabric.contracts import EndpointClass, ModelRouteProof
    from src.model_fabric.proofs import canonical_proof_hash, proof_aged_out, proof_is_fresh

    def proof(profile_id: str, outcome: str = "passed") -> ModelRouteProof:
        values = dict(profile_schema_version="seraph.model-fabric.profile.v1", profile_contract_hash="a" * 64,
            profile_id=profile_id, model="Qwen/Qwen3.8-27B", endpoint="https://cloud-api.near.ai/v1/chat/completions",
            endpoint_class=EndpointClass.REMOTE, adapter="openai_compatible_chat", capability="text",
            canary_version="v1", outcome=outcome, checked_at=100.0, expires_at=200.0, proof_hash="",
            probe_receipt_id="receipt", probe_receipt_hash="b" * 64, proven_value="verified")
        return replace(ModelRouteProof(**values), proof_hash=canonical_proof_hash(ModelRouteProof(**values)))

    near = proof("near_tee.text")
    later = 200.0 + 30 * DAY
    assert proof_is_fresh(near, now=later) is True
    assert proof_aged_out(near, now=later) is False
    # A failed NEAR TEE proof never counts.
    assert proof_is_fresh(proof("near_tee.text", "failed"), now=150.0) is False
    # Other profiles keep their expiry (unchanged behaviour).
    other = proof("openrouter.text")
    assert proof_is_fresh(other, now=150.0) is True
    assert proof_is_fresh(other, now=later) is False and proof_aged_out(other, now=later) is True


@pytest.mark.asyncio
async def test_proofs_and_receipt_older_than_a_day_still_dispatch(accounting_db, monkeypatch):
    jobs, owner, near = await _planner_world(accounting_db, monkeypatch, proof_age_seconds=2 * DAY)
    _age_setup_receipt(2 * DAY)
    near.answer = {"role": "assistant", "content": __import__("json").dumps(PLAN)}
    plan = await _plan(accounting_db, owner)
    assert plan.steps[0].tool_id == "fixture.read"
    assert [r.url.raw_path.decode() for r in near_inference_requests(near)] == [
        "/v1/chat/completions", "/v1/billing/costs"]
    rows = near_settled_rows(await jobs.inference_accounting_snapshot(), "general_task_planner")
    assert len(rows) == 1 and rows[0]["state"] == "settled"


@pytest.mark.asyncio
async def test_old_receipt_and_failing_background_read_still_dispatch(accounting_db, monkeypatch):
    from src.model_fabric.near_tee_catalog import (
        catalog_warning, last_confirmed, read_catalog_observations, refresh_catalog_observations,
    )
    jobs, owner, near = await _planner_world(accounting_db, monkeypatch, near=FlakyCatalogNear(),
        proof_age_seconds=2 * DAY)
    _age_setup_receipt(2 * DAY)
    profile = _text_profile()
    setup_confirmed = last_confirmed("text", profile)
    near.catalog_down = True
    await refresh_catalog_observations()
    observation = read_catalog_observations()["text"]
    assert observation["last_attempt_outcome"] == "read_failed"
    assert observation["last_attempt_error"] == "near_tee_catalog_http_error"
    assert observation["last_read"] is None
    # The failed read changes nothing: last confirmed stays the setup check, no warning.
    assert last_confirmed("text", profile) == setup_confirmed and setup_confirmed[2] == "setup_check"
    assert catalog_warning("text", profile) is None
    near.answer = {"role": "assistant", "content": __import__("json").dumps(PLAN)}
    await _plan(accounting_db, owner)
    assert [r.url.raw_path.decode() for r in near_inference_requests(near)][0] == "/v1/chat/completions"


@pytest.mark.asyncio
async def test_background_read_updates_last_confirmed_and_keeps_it_through_a_failure(accounting_db, monkeypatch):
    from src.model_fabric.near_tee_catalog import (
        read_catalog_receipts, last_confirmed, refresh_catalog_observations, tee_hosting_label,
    )
    _jobs, _owner, near = await _planner_world(accounting_db, monkeypatch, near=FlakyCatalogNear())
    _age_setup_receipt(2 * DAY)
    setup_receipt = read_catalog_receipts()["text"]
    profile = _text_profile()
    await refresh_catalog_observations(now=time.time())
    confirmed = last_confirmed("text", profile)
    assert confirmed[2] == "background_read" and confirmed[0] > setup_receipt["checked_at"]
    assert tee_hosting_label("text", profile) == (
        f"TEE-hosted per NEAR catalog as of {confirmed[1]}; NEAR gateway receives plaintext; "
        "attestation not verified by Seraph; no end-to-end encryption")
    # The setup receipt itself is untouched by background reads.
    assert read_catalog_receipts()["text"] == setup_receipt
    near.catalog_down = True
    await refresh_catalog_observations(now=time.time() + 10)
    assert last_confirmed("text", profile) == confirmed


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"isReady": False}, {"attestationSupported": False}, {"verifiable": False}])
async def test_fresh_read_reporting_disqualifying_change_warns_but_does_not_block(accounting_db, monkeypatch, change):
    from src.model_fabric.near_tee_catalog import catalog_receipt_block_reason, catalog_warning, refresh_catalog_observations
    jobs, owner, near = await _planner_world(accounting_db, monkeypatch)
    profile = _text_profile()
    near.catalog = catalog_record(**change)
    started = time.time()
    await refresh_catalog_observations(now=started)
    warning = catalog_warning("text", profile)
    assert warning is not None and warning["blocks_dispatch"] is False
    assert warning["reason_code"].startswith("near_tee_catalog_not_")
    assert warning["message"].startswith(f"NEAR catalog reports {profile.model} not ")
    assert " since " in warning["message"]
    # A later read with the same report keeps the original "since".
    await refresh_catalog_observations(now=started + 3600)
    assert catalog_warning("text", profile)["since"] == warning["since"]
    # The setup gate is unaffected and the request still reaches NEAR.
    assert catalog_receipt_block_reason("text", profile) is None
    near.answer = {"role": "assistant", "content": __import__("json").dumps(PLAN)}
    await _plan(accounting_db, owner)
    assert [r.url.raw_path.decode() for r in near_inference_requests(near)][0] == "/v1/chat/completions"
    # The model coming back clears the warning.
    near.catalog = catalog_record()
    await refresh_catalog_observations(now=started + 7200)
    assert catalog_warning("text", profile) is None


@pytest.mark.asyncio
async def test_model_slot_or_credential_change_invalidates_setup(accounting_db, monkeypatch):
    from src.model_fabric import candidate_from_profile
    from src.model_fabric.configuration import (
        near_tee_credential_bound_since, near_tee_profile_for_slot, read_model_fabric_configuration,
        record_near_tee_credential_binding,
    )
    from src.model_fabric.near_tee_catalog import RECEIPT_STALE, catalog_receipt_block_reason
    from src.model_fabric.repository import model_fabric_repository
    _jobs, _owner, _near = await _planner_world(accounting_db, monkeypatch, proof_age_seconds=2 * DAY)
    configured = read_model_fabric_configuration()
    slot = configured.near_tee.slots["text"]
    profile = near_tee_profile_for_slot("text", slot)

    async def proof_for(target):
        candidate = candidate_from_profile(target)
        return await model_fabric_repository.latest_capability_proof(profile_schema_version=target.schema_version,
            profile_contract_hash=target.contract_hash, profile_id=target.id, model=target.model,
            endpoint=candidate.endpoint, endpoint_class=candidate.endpoint_class, adapter=candidate.adapter,
            capability="text")

    assert await proof_for(profile) is not None  # two days old, still the setup proof
    # A changed model or slot setting is a new profile hash: no proof, stale receipt.
    for changed in (replace(slot, model_id="z-ai/glm-5.3-flash"), replace(slot, max_output_tokens=900)):
        rebound = near_tee_profile_for_slot("text", changed)
        assert rebound.contract_hash != profile.contract_hash
        assert await proof_for(rebound) is None
        assert catalog_receipt_block_reason("text", rebound) == RECEIPT_STALE
    # A configuration revision change needs the catalog check again.
    assert catalog_receipt_block_reason("text", profile, revision=configured.egress_revision + 2) == RECEIPT_STALE
    # A credential change: proofs checked before it no longer count.
    record_near_tee_credential_binding("fedcba987654")
    assert near_tee_credential_bound_since("fedcba987654") is not None
    assert await proof_for(profile) is None
    # The recorded fingerprint differs from the configured one: re-bound now, still no old proof.
    assert near_tee_credential_bound_since() is not None
    assert await proof_for(profile) is None


@pytest.mark.asyncio
async def test_background_refresher_runs_without_blocking_and_stops(accounting_db, monkeypatch):
    import asyncio
    from src.model_fabric.near_tee_catalog import NearTeeCatalogRefresher, read_catalog_observations
    _jobs, _owner, near = await _planner_world(accounting_db, monkeypatch)
    before = len(near.requests)
    # Disabled (the test default): start() does nothing.
    disabled = NearTeeCatalogRefresher(startup_delay=0)
    disabled.start()
    assert disabled._task is None
    monkeypatch.setattr(settings, "near_tee_catalog_refresh_enabled", True)
    refresher = NearTeeCatalogRefresher(startup_delay=0)
    refresher.start()
    try:
        for _ in range(200):
            if read_catalog_observations().get("text", {}).get("last_attempt_outcome") == "read":
                break
            await asyncio.sleep(0.01)
        observation = read_catalog_observations()["text"]
        assert observation["last_attempt_outcome"] == "read" and observation["last_read"]["outcome"] == "passed"
        assert observation["last_read"]["basis"] == "background_read"
    finally:
        await refresher.stop()
    assert refresher._task is None
    reads = near.requests[before:]
    assert reads and all(r.method == "GET" and "authorization" not in r.headers for r in reads)
