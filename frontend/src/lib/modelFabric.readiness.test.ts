import { describe, expect, it } from "vitest";
import {
  dollarsToMicrousd,
  microusdToDollars,
  normalizeModelFabricSettings,
  textRouteProofLabel,
  type ModelFabricRuntimeStatus,
} from "./modelFabric";

function runtime(proofs: Array<{ capability: string; status: string; expires_at: string | null }>): ModelFabricRuntimeStatus {
  return {
    proofs: proofs.map((proof) => ({ profile_id: "openrouter.text", outcome: "passed", checked_at: null, ...proof })),
  } as unknown as ModelFabricRuntimeStatus;
}

describe("daily-use model-fabric helpers", () => {
  it.each([
    ["0.05", 50_000],
    ["5", 5_000_000],
    ["12.345678", 12_345_678],
    ["0.000001", 1],
  ])("converts $%s to %i micro-USD and back", (dollars, micro) => {
    expect(dollarsToMicrousd(dollars)).toBe(micro);
    expect(microusdToDollars(micro)).toBe(dollars);
  });

  it.each(["", "-1", "1e3", "0.0000001", "five", "1,000"])("rejects non-dollar input %j", (value) => {
    expect(dollarsToMicrousd(value)).toBeNull();
  });

  it("labels text-route proof expiry for the chat composer", () => {
    const now = Date.parse("2026-10-15T12:00:00Z");
    expect(textRouteProofLabel(runtime([
      { capability: "text", status: "fresh", expires_at: "2026-10-16T11:00:00Z" },
      { capability: "health", status: "fresh", expires_at: "2026-10-15T17:30:00Z" },
    ]), now)).toBe("PROOFS 5H LEFT");
    expect(textRouteProofLabel(runtime([
      { capability: "text", status: "fresh", expires_at: "2026-10-15T12:20:00Z" },
    ]), now)).toBe("PROOFS 20M LEFT");
    expect(textRouteProofLabel(runtime([
      { capability: "text", status: "fresh", expires_at: "2026-10-16T11:00:00Z" },
      { capability: "latency_ms", status: "stale", expires_at: "2026-10-15T11:00:00Z" },
    ]), now)).toBe("PROOF EXPIRED: LATENCY MS");
    expect(textRouteProofLabel(runtime([{ capability: "tool_use", status: "missing", expires_at: null }]), now)).toBe("PROOF MISSING: TOOL USE");
    expect(textRouteProofLabel(runtime([
      { capability: "text", status: "fresh", expires_at: "2026-10-16T11:00:00Z" },
      { capability: "tool_use", status: "failed", expires_at: "2026-10-16T11:00:00Z" },
    ]), now)).toBe("PROOF FAILED: TOOL USE");
    expect(textRouteProofLabel(runtime([]), now)).toBeNull();
    expect(textRouteProofLabel(null, now)).toBeNull();
  });

  it("reads the proof lifetime bounds with a conservative fallback", () => {
    const base = { schema_version: "seraph.model-fabric.settings.v1", status: "ready", profiles: [], workload_policies: [], defaults: {} };
    expect(normalizeModelFabricSettings({ ...base, canary_proof_ttl: { default_seconds: 3600, max_seconds: 86400 } })?.canary_proof_ttl)
      .toEqual({ default_seconds: 3600, max_seconds: 86400 });
    expect(normalizeModelFabricSettings(base)?.canary_proof_ttl).toEqual({ default_seconds: 3600, max_seconds: 3600 });
    expect(normalizeModelFabricSettings({ ...base, canary_proof_ttl: { default_seconds: 9e9, max_seconds: 9e9 } })?.canary_proof_ttl)
      .toEqual({ default_seconds: 3600, max_seconds: 3600 });
  });
});

describe("settings without Seraph-side cost limits (#1284)", () => {
  const nearText = {
    schema_version: "seraph.near.text.v1", enabled: true, profile_id: "near.text", model_id: "z-ai/glm-5.3-flash",
    api_base: "https://cloud-api.near.ai/v1", max_output_tokens: 512, timeout_seconds: 45,
    credential_ref: "vault:near_text_api_key", credential_fingerprint: null, plaintext_egress_consent_revision: null,
    key_present: true, consent_current: true, status: "configured", reason_code: null, tls_transport: true,
    tee_verified: false, e2ee: false, provider_plaintext_disclosure: "NEAR receives the question in plaintext over HTTPS.",
  };
  const textSlot = {
    model_id: "Qwen/Qwen3.8-27B", enabled: true, max_input_tokens: 8192, max_output_tokens: 1024, timeout_seconds: 30,
    catalog_ttl_hours: 24, consent_current: true, status: "ready",
  };
  const settings = (extra: Record<string, unknown>) => normalizeModelFabricSettings({
    schema_version: "seraph.model-fabric.settings.v1", status: "configured", ...extra });

  it("accepts NEAR text, NEAR TEE slots and the lane with no ceiling or request bound", () => {
    const value = settings({ near_text: nearText, near_tee: { tee_verified: false, e2ee: false, key_present: true, status: "ready",
      slots: { text: textSlot } }, inference_lane: { max_queued: 64, max_inflight: 1, max_outstanding_per_owner: 16, max_retries: 2 } });
    expect(value?.near_text).not.toBeNull();
    expect(value?.near_text_metadata_unavailable).toBe(false);
    expect(value?.near_tee?.slots.text?.model_id).toBe("Qwen/Qwen3.8-27B");
    expect(value?.near_tee_metadata_unavailable).toBe(false);
    expect(value?.inference_lane).toEqual({ max_queued: 64, max_inflight: 1, max_outstanding_per_owner: 16, max_retries: 2 });
  });

  it("ignores the limit fields an older backend still sends", () => {
    const value = settings({ near_text: { ...nearText, request_cost_bound_microusd: 1_000, spend_ceiling_microusd: 10 },
      near_tee: { tee_verified: false, e2ee: false, key_present: true, status: "ready",
        slots: { text: { ...textSlot, request_cost_bound_microusd: 20_000 } } },
      inference_lane: { schema_version: "seraph.inference.lane.v1", spend_ceiling_microusd: 1, published: false, max_queued: 64,
        max_inflight: 1, max_outstanding_per_owner: 16, max_retries: 2, request_reserve_review_microusd: 5 } });
    expect(value?.near_text).not.toHaveProperty("request_cost_bound_microusd");
    expect(value?.near_text).not.toHaveProperty("spend_ceiling_microusd");
    expect(value?.near_tee?.slots.text).not.toHaveProperty("request_cost_bound_microusd");
    expect(value?.inference_lane).toEqual({ max_queued: 64, max_inflight: 1, max_outstanding_per_owner: 16, max_retries: 2 });
    expect(value).not.toHaveProperty("inference_lane_reason");
  });

  it("reads the spend history with only display fields", () => {
    const value = settings({ inference_accounting: { status: "ready", period_id: "2026-11", spent_microusd: 3_200_000,
      unpriced_requests: 2, history_verified: true, operation_count: 0, operations_truncated: false, operations: [],
      revision: 4, deployment_id: "dep", ledger_digest: "abc" } });
    expect(value?.inference_accounting).toMatchObject({ status: "ready", period_id: "2026-11", spent_microusd: 3_200_000,
      unpriced_requests: 2, history_verified: true, revision: 4, deployment_id: "dep", ledger_digest: "abc" });
    expect(settings({ inference_accounting: { status: "unavailable", operations: [] } })?.inference_accounting?.spent_microusd).toBeUndefined();
  });
});
