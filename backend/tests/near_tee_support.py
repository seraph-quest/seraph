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
        egress_revoked: bool = False) -> ModelFabricConfiguration:
    """Persist a ready, consented, enabled NEAR TEE text slot behind the shared lane."""
    configuration = ModelFabricConfiguration(
        status="ready",
        egress_revision=egress_revision,
        egress_revoked=egress_revoked,
        inference_lane=InferenceLaneConfig(spend_ceiling_microusd=ceiling_microusd),
        near_tee=NearTeeSetup(credential_fingerprint=credential_fingerprint, slots={"text": NearTeeSlot(
            model_id=NEAR_TEE_TEXT_MODEL, max_input_tokens=8192, max_output_tokens=1024,
            request_cost_bound_microusd=request_cost_bound_microusd, enabled=True,
            purpose_consent_revision=egress_revision)}),
    )
    write_model_fabric_configuration(configuration)
    return configuration


def assume_assessment_route_available(monkeypatch) -> None:
    """Precondition for guardian tick/recovery mechanics tests (#1156).

    Those tests pin how the tick starts, recovers and fences assessments
    given a usable assessment route. The guardian vertical has no NEAR TEE
    adapter yet (#1159), so in the retired world its candidates are held
    queued; that hold is covered by test_vertical_no_provider_journeys. No
    provider transport is enabled by this: model calls stay mocked/forbidden.
    """
    from src.model_fabric import retirement
    monkeypatch.setattr(retirement, "openrouter_only_route_block", lambda _runtime_path: None)
