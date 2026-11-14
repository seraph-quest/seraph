"""Shared NEAR TEE test configuration (#1156); no provider contact.

Writes the canonical model-fabric configuration directly (the same validated
write path the settings API uses) so a test can put a route behind a usable
NEAR TEE text slot without the settings HTTP flow. No credential value is
stored here: only a non-secret fingerprint, so any dispatch attempt still
fails closed at credential capture before transport.
"""
from __future__ import annotations

from src.model_fabric.configuration import (
    InferenceLaneConfig,
    ModelFabricConfiguration,
    NearTeeSetup,
    NearTeeSlot,
    write_model_fabric_configuration,
)

NEAR_TEE_TEXT_MODEL = "Qwen/Qwen3.8-27B"


_FINGERPRINT = "0123456789ab"


def write_near_tee_text_configuration(*, egress_revision: int = 2, ceiling_microusd: int = 1_000_000,
        request_cost_bound_microusd: int = 20_000, credential_fingerprint: str | None = _FINGERPRINT,
        egress_revoked: bool = False, timeout_seconds: float = 30.0) -> ModelFabricConfiguration:
    """Persist a ready, consented, enabled NEAR TEE text slot behind the shared lane."""
    configuration = ModelFabricConfiguration(
        status="ready",
        egress_revision=egress_revision,
        egress_revoked=egress_revoked,
        inference_lane=InferenceLaneConfig(spend_ceiling_microusd=ceiling_microusd),
        near_tee=NearTeeSetup(credential_fingerprint=credential_fingerprint, slots={"text": NearTeeSlot(
            model_id=NEAR_TEE_TEXT_MODEL, max_input_tokens=8192, max_output_tokens=1024,
            request_cost_bound_microusd=request_cost_bound_microusd, enabled=True,
            timeout_seconds=timeout_seconds, purpose_consent_revision=egress_revision)}),
    )
    write_model_fabric_configuration(configuration)
    return configuration


def assume_assessment_route_available(monkeypatch) -> None:
    """Precondition for guardian tick/recovery mechanics tests (#1156).

    Those tests pin how the tick starts, recovers and fences assessments
    given a usable assessment route, independent of provider setup. With
    #1159 the route is the NEAR TEE text slot (its journey is pinned in
    test_near_tee_vertical_journeys); without one, candidates are held queued
    (test_vertical_no_provider_journeys). No provider transport is enabled by
    this: model calls stay mocked/forbidden.
    """
    from src.guardian import opportunity_runtime
    monkeypatch.setattr(opportunity_runtime, "assessment_route_block", lambda: None)


# ── Ready NEAR TEE text slot for vertical journeys (#1159) ─────────────

NEAR_TEE_VERTICAL_KEY = "private-intercepted-near-tee-vertical-key"
NEAR_TEE_TEXT_PROFILE_ID = "near_tee.text"


async def prepare_near_tee_vertical(monkeypatch, *, ceiling_microusd: int = 1_000_000,
        request_cost_bound_microusd: int = 20_000, capabilities=("text", "structured_output"),
        timeout_seconds: float = 30.0, jobs=None, near=None, egress_revision: int = 2):
    """A ready NEAR TEE text slot with every dispatch gate satisfied; no contact.

    - the canonical configuration (consented, enabled, behind the shared lane);
    - the write-only ``near_api_key`` in the vault with a matching fingerprint;
    - a real catalog-check receipt produced by ``run_catalog_check`` over the
      intercepted NEAR fixture (the catalog GET is the only request made);
    - literal capability proofs for ``capabilities`` (+ health, latency_ms)
      bound to the exact slot profile hash (not produced by any model call);
    - the durable accounting ceiling matching the lane.

    Returns the installed intercepting ``NearFixture``: it answers only the
    pinned NEAR host and fails the test on any other destination, including
    OpenRouter. ``near.requests`` records every request.
    """
    import hashlib
    import time
    from datetime import datetime, timezone
    from src.db.engine import get_session
    from src.db.models import ModelRouteReceiptRecord
    from src.model_fabric import candidate_from_profile
    from src.model_fabric.configuration import near_tee_profile_for_slot, read_model_fabric_configuration
    from src.model_fabric.near_tee_catalog import read_catalog_receipts, run_catalog_check, write_catalog_receipts
    from src.model_fabric.proofs import build_model_route_proof
    from src.model_fabric.repository import model_fabric_repository
    from src.vault.repository import vault_repository
    from src.workflows.job_runtime import DurableJobRepository
    from tests.test_near_tee_text_slot import NearFixture

    near = (near or NearFixture()).install(monkeypatch)
    fingerprint = hashlib.sha256(NEAR_TEE_VERTICAL_KEY.encode()).hexdigest()[:12]
    write_near_tee_text_configuration(ceiling_microusd=ceiling_microusd,
        request_cost_bound_microusd=request_cost_bound_microusd, credential_fingerprint=fingerprint,
        timeout_seconds=timeout_seconds, egress_revision=egress_revision)
    await vault_repository.store("near_api_key", NEAR_TEE_VERTICAL_KEY)
    configured = read_model_fabric_configuration()
    receipts = await run_catalog_check(configured)
    assert receipts["text"]["outcome"] == "passed", receipts
    stored = read_catalog_receipts()
    stored.update(receipts)
    write_catalog_receipts(stored)
    profile = near_tee_profile_for_slot("text", configured.near_tee.slots["text"])
    candidate = candidate_from_profile(profile)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with get_session() as db:
        if await db.get(ModelRouteReceiptRecord, "near-tee-literal-fixture") is None:
            db.add(ModelRouteReceiptRecord(receipt_id="near-tee-literal-fixture", receipt_hash="c" * 64,
                request_id="near-tee-literal-fixture", route_decision_id="near-tee-literal-fixture",
                runtime_path="functional_fixture", workload="capability_probe", outcome="succeeded",
                egress_class="cloud_allowed_full", started_at=now, finished_at=now, latency_ms=1))
        await db.commit()
    values = {"health": "healthy", "latency_ms": 1000}
    for capability in (*capabilities, "health", "latency_ms"):
        proof = build_model_route_proof(profile=profile, endpoint_class=candidate.endpoint_class,
            adapter=candidate.adapter, capability=capability, canary_version="near-tee-functional-fixture-v1",
            outcome="passed", checked_at=time.time() - 1, expires_at=time.time() + 600,
            probe_receipt_id="near-tee-literal-fixture", probe_receipt_hash="c" * 64,
            proven_value=values.get(capability, "verified"))
        assert (await model_fabric_repository.persist_capability_proof(proof)).persisted
    await (jobs or DurableJobRepository()).configure_inference_accounting(ceiling_microusd)
    # Only the catalog read happened; no inference.
    assert [request.method for request in near.requests] == ["GET"]
    return near


def near_inference_requests(near):
    """The intercepted NEAR inference/billing requests (catalog reads excluded)."""
    return [request for request in near.requests if request.method == "POST"]


def near_settled_rows(snapshot, runtime_path: str):
    return [row for row in snapshot["operations"] if row["runtime_path"] == runtime_path]


def assume_repository_repair_route_available(monkeypatch) -> None:
    """Precondition for repository registration/admission mechanics tests (#1159).

    Those tests pin how the stock selector, native owner and child admission
    behave given a usable repair route, independent of provider setup. With
    OpenRouter retired the repair iteration step has no NEAR TEE adapter, so
    it is refused with ``near_tee_route_unsupported`` before planning (pinned
    in test_near_tee_vertical_journeys). No provider transport is enabled by
    this: model calls stay mocked/forbidden.
    """
    from src.model_fabric import near_tee_vertical
    monkeypatch.setattr(near_tee_vertical, "repository_repair_route_block", lambda: None)


def restore_repository_repair_route_block(monkeypatch) -> None:
    """Undo ``assume_repository_repair_route_available`` (the retirement switch)."""
    from src.model_fabric import near_tee_vertical
    monkeypatch.setattr(near_tee_vertical, "repository_repair_route_block", _REAL_REPOSITORY_REPAIR_ROUTE_BLOCK)


from src.model_fabric import near_tee_vertical as _near_tee_vertical  # noqa: E402
_REAL_REPOSITORY_REPAIR_ROUTE_BLOCK = _near_tee_vertical.repository_repair_route_block
