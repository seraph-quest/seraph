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


def write_near_tee_text_configuration(*, egress_revision: int = 2, ceiling_microusd: int = 1_000_000,
        request_cost_bound_microusd: int = 20_000) -> ModelFabricConfiguration:
    """Persist a ready, consented, enabled NEAR TEE text slot behind the shared lane."""
    configuration = ModelFabricConfiguration(
        status="ready",
        egress_revision=egress_revision,
        inference_lane=InferenceLaneConfig(spend_ceiling_microusd=ceiling_microusd),
        near_tee=NearTeeSetup(credential_fingerprint="0123456789ab", slots={"text": NearTeeSlot(
            model_id=NEAR_TEE_TEXT_MODEL, max_input_tokens=8192, max_output_tokens=1024,
            request_cost_bound_microusd=request_cost_bound_microusd, enabled=True,
            purpose_consent_revision=egress_revision)}),
    )
    write_model_fabric_configuration(configuration)
    return configuration
