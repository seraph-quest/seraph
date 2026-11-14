import { describe, expect, it } from "vitest";

import {
  chatFabricRouteDegraded,
  classifyObserverContext,
  daemonConfiguredOff,
  deriveChatRuntimeIdentity,
  deriveConnectionItem,
  deriveContextItem,
  deriveDesktopItem,
  deriveVersionItem,
  summarizeShellStatus,
  type ShellRuntimeStatus,
} from "./cockpitShellStatus";

// Shape of /api/runtime/status on develop @ 0850178d (2026-11-14, local
// no-login): the top-level provider/model are a stale configured default
// (OpenRouter/Grok) while chat runs on the NEAR TEE text slot.
const nearRuntime = {
  build_id: "SERAPH_PRIME_v2026.10.7",
  provider: "openrouter",
  model: "x-ai/grok-4.1-fast",
  model_label: "grok-4.1-fast",
  effective_runtime: {
    runtime_path: "chat_agent",
    active_profile: "near_tee.text",
    provider_label: "near_tee",
    model: "deepseek-ai/DeepSeek-V4.1-Flash-1017",
    model_label: "DeepSeek-V4.1-Flash-1017",
    route_label: "NEAR TEE text slot",
    inference_ready: true,
    inference_readiness: { status: "ready", reasons: [] },
  },
  model_fabric: {
    runtime_paths: {
      chat_agent: {
        selected: null,
        attempted: null,
        succeeded: null,
        attempt_count: 0,
        last_outcome: null,
        fallback_used: null,
        degradation_codes: [],
        persistence: "unknown",
      },
    },
  },
} as ShellRuntimeStatus & { provider: string; model: string };

describe("deriveChatRuntimeIdentity", () => {
  it("names the effective chat route, never the stale top-level default", () => {
    const identity = deriveChatRuntimeIdentity(nearRuntime, "runtime_status");
    expect(identity.label).toBe("NEAR TEE text slot · DeepSeek-V4.1-Flash-1017");
    expect(identity.model).toBe("DeepSeek-V4.1-Flash-1017");
    expect(identity.tone).toBe("ok");
    expect(identity.label).not.toMatch(/grok|openrouter/i);
  });

  it("does not treat an unused route with unknown persistence as degraded", () => {
    expect(chatFabricRouteDegraded(nearRuntime.model_fabric?.runtime_paths?.chat_agent)).toBe(false);
    expect(chatFabricRouteDegraded({ attempt_count: 2, persistence: "unknown", last_outcome: "succeeded" })).toBe(true);
    expect(chatFabricRouteDegraded({ attempt_count: 1, persistence: "persisted", last_outcome: "provider_error" })).toBe(true);
  });

  it("marks a retained receipt as not confirmed instead of current", () => {
    const identity = deriveChatRuntimeIdentity(nearRuntime, "retained");
    expect(identity.tone).toBe("unknown");
    expect(identity.reason).toMatch(/Last confirmed value/);
  });

  it("keeps a retained blocked route visibly blocked while unconfirmed", () => {
    const identity = deriveChatRuntimeIdentity({
      ...nearRuntime,
      effective_runtime: { ...nearRuntime.effective_runtime, inference_ready: false, inference_readiness: { status: "blocked", reasons: ["credential_missing"] } },
    }, "retained");
    expect(identity.tone).toBe("unknown");
    expect(identity.reason).toMatch(/^Last confirmed value/);
    expect(identity.reason).toContain("Chat inference is blocked: credential missing.");
  });

  it("explains a blocked route with its reasons", () => {
    const identity = deriveChatRuntimeIdentity({
      ...nearRuntime,
      effective_runtime: {
        ...nearRuntime.effective_runtime,
        inference_ready: false,
        inference_readiness: { status: "blocked", reasons: ["credential_missing", "proof_missing:health"] },
      },
    }, "runtime_status");
    expect(identity.tone).toBe("error");
    expect(identity.reason).toBe("Chat inference is blocked: credential missing, proof missing health.");
  });

  it("says why it cannot name a route rather than falling back to the default", () => {
    const identity = deriveChatRuntimeIdentity({ build_id: "x" }, "runtime_status");
    expect(identity.tone).toBe("unknown");
    expect(identity.label).toBe("Route not reported");
    expect(identity.reason).toMatch(/configured default is not shown/);
  });
});

describe("deriveVersionItem", () => {
  it("reports one version when UI and backend agree", () => {
    const item = deriveVersionItem("SERAPH_PRIME_v2026.10.7", nearRuntime, "runtime_status");
    expect(item).toMatchObject({ tone: "ok", value: "SERAPH_PRIME_v2026.10.7" });
  });

  it("never shows a retained earlier build as the backend version", () => {
    const item = deriveVersionItem("SERAPH_PRIME_v2026.10.7", { ...nearRuntime, build_id: "SERAPH_PRIME_v2026.4.11" }, "retained");
    expect(item.value).toBe("SERAPH_PRIME_v2026.10.7");
    expect(item.tone).toBe("unknown");
    expect(item.value).not.toContain("2026.4.11");
  });

  it("flags a real mismatch with what to do", () => {
    const item = deriveVersionItem("SERAPH_PRIME_v2026.10.7", { ...nearRuntime, build_id: "SERAPH_PRIME_v2026.11.0" }, "runtime_status");
    expect(item.tone).toBe("warning");
    expect(item.reason).toMatch(/Reload the page/);
  });
});

describe("expected configuration vs faults", () => {
  const configuredOff = {
    connected: false,
    daemon_state: "disabled",
    last_error_kind: "configured_off",
    status_reason: "Native desktop presence is disabled by DAEMON_ENABLED=false.",
    recovery_hint: "Set DAEMON_ENABLED=true in .env.dev and start it with ./manage.sh -e dev daemon start when native desktop presence is wanted.",
  };

  it("shows a configured-off desktop daemon as expected, with the reason and how to enable it", () => {
    expect(daemonConfiguredOff(configuredOff)).toBe(true);
    const item = deriveDesktopItem(configuredOff, "confirmed");
    expect(item.tone).toBe("info");
    expect(item.value).toBe("Off by configuration");
    expect(item.reason).toContain("DAEMON_ENABLED=false");
    expect(item.reason).toContain("To enable it:");
    expect(item.action).toMatchObject({ kind: "settings", section: "native" });
  });

  it("treats an enabled but silent daemon as needing attention", () => {
    const item = deriveDesktopItem({ connected: false, daemon_state: "enabled", status_reason: "No post in 30s." }, "confirmed");
    expect(item.tone).toBe("warning");
  });

  it("explains unknown desktop presence and offers to check", () => {
    const item = deriveDesktopItem(null, "unavailable");
    expect(item.tone).toBe("unknown");
    expect(item.reason).toBeTruthy();
    expect(item.action).toMatchObject({ kind: "load-presence" });
  });

  it("reports an operator scope that has not refreshed yet as expected (info)", () => {
    const item = deriveContextItem("degraded", true, "unscoped_fallback");
    expect(item.tone).toBe("info");
    expect(item.value).toBe("Not refreshed yet for this session");
    expect(item.reason).toMatch(/Chat and work are unaffected/);
  });

  it.each([
    ["degraded", "source_error:git", /failed: git/],
    ["degraded", "source_degraded:goals", /reported degraded: goals/],
    ["degraded", "source_error:time,source_degraded:goals", /failed: time; reported degraded: goals/],
    ["stale", "source_error:time", /failed: time/],
    ["stale", "no_active_sources", /no observer source is enabled/],
    ["stale", null, /No observer source refreshed successfully/],
  ])("reports observer %s (%s) as a warning that names the source", (quality, reason, pattern) => {
    const item = deriveContextItem(quality, true, reason);
    expect(item.tone).toBe("warning");
    expect(item.reason).toMatch(pattern);
    expect(summarizeShellStatus([deriveConnectionItem("connected"), item])).toMatchObject({ tone: "warning", label: "1 needs attention" });
  });

  it("never calls degraded context without a reason expected (older backend)", () => {
    const item = deriveContextItem("degraded", true, null);
    expect(item.tone).toBe("warning");
    expect(item.value).toBe("Partial · reason not reported");
    expect(item.reason).toMatch(/may be a fault/);
  });

  it("classifies observer context from data_quality and its reason", () => {
    expect(classifyObserverContext("good", null).kind).toBe("good");
    expect(classifyObserverContext(null, null).kind).toBe("unknown");
    expect(classifyObserverContext("degraded", "unscoped_fallback").kind).toBe("not_refreshed");
    expect(classifyObserverContext("degraded", "unscoped_fallback,source_error:git").kind).toBe("fault");
    expect(classifyObserverContext("degraded", "something_new").kind).toBe("unexplained");
    expect(classifyObserverContext("stale", null).kind).toBe("fault");
  });

  it("does not raise the overall status for expected configuration", () => {
    const summary = summarizeShellStatus([
      deriveConnectionItem("connected"),
      deriveDesktopItem(configuredOff, "confirmed"),
      deriveContextItem("degraded", true, "unscoped_fallback"),
    ]);
    expect(summary).toEqual({ tone: "ok", label: "All systems ready", attentionCount: 0 });
  });

  it("raises the overall status for a real problem and counts it", () => {
    const summary = summarizeShellStatus([deriveConnectionItem("disconnected"), deriveContextItem("good", true)]);
    expect(summary.tone).toBe("warning");
    expect(summary.label).toBe("1 needs attention");
  });

  it("gives every non-ready item a reason", () => {
    const items = [
      deriveConnectionItem("connecting"),
      deriveConnectionItem("error"),
      deriveDesktopItem(null, "unavailable"),
      deriveContextItem(null, false),
      deriveContextItem("stale", true),
      deriveContextItem("degraded", true),
      deriveVersionItem("SERAPH_PRIME_v1", null, null),
    ];
    for (const item of items) {
      expect(item.tone).not.toBe("ok");
      expect(item.reason, item.id).toBeTruthy();
    }
  });
});
