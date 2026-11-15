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
