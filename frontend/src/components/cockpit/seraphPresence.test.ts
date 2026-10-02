import { describe, expect, it } from "vitest";

import {
  deriveSeraphPresenceMetadataState,
  deriveSeraphPresenceState,
  type SeraphPresenceLoadState,
} from "./seraphPresence";

describe("deriveSeraphPresenceMetadataState", () => {
  it.each([
    ["a missing 200 payload", "loaded", false, "unavailable"],
    ["an initial 503 or timeout", "stale", false, "unavailable"],
    ["a confirmed payload", "loaded", true, "confirmed"],
    ["a failed refresh after confirmation", "stale", true, "stale"],
  ])("classifies %s without inventing readiness", (_label, loadState, hasConfirmedPayload, expected) => {
    expect(deriveSeraphPresenceMetadataState(loadState as SeraphPresenceLoadState, hasConfirmedPayload)).toBe(expected);
  });
});

describe("deriveSeraphPresenceState", () => {
  it("uses warning state when approvals are pending", () => {
    const descriptor = deriveSeraphPresenceState({
      connectionStatus: "connected",
      animationState: "idle",
      isAgentBusy: false,
      pendingApprovalCount: 2,
      pendingNotificationCount: 0,
      queuedInsightCount: 0,
      degradedRouteCount: 0,
      degradedSourceAdapterCount: 0,
      attentionImportedFamilyCount: 0,
      attentionPresenceSurfaceCount: 0,
      actionableThreadCount: 0,
      continuityHealth: "ready",
      recommendedFocus: null,
      recentTraceRole: null,
      recentTraceTool: null,
      latestResponseRole: null,
      ambientState: "idle",
      dataQuality: "good",
      recentInterventionCount: 0,
      operatorStatus: null,
    });

    expect(descriptor.state).toBe("approval_wait");
    expect(descriptor.tone).toBe("warning");
  });

  it("prefers tool-use when a step is actively running", () => {
    const descriptor = deriveSeraphPresenceState({
      connectionStatus: "connected",
      animationState: "casting",
      isAgentBusy: true,
      pendingApprovalCount: 0,
      pendingNotificationCount: 0,
      queuedInsightCount: 0,
      degradedRouteCount: 0,
      degradedSourceAdapterCount: 0,
      attentionImportedFamilyCount: 0,
      attentionPresenceSurfaceCount: 0,
      actionableThreadCount: 0,
      continuityHealth: "ready",
      recommendedFocus: null,
      recentTraceRole: "step",
      recentTraceTool: "write_file",
      latestResponseRole: null,
      ambientState: "idle",
      dataQuality: "good",
      recentInterventionCount: 0,
      operatorStatus: "running",
    });

    expect(descriptor.state).toBe("tool_use");
    expect(descriptor.tone).toBe("active");
  });

  it("treats degraded memory/runtime quality as faulted", () => {
    const descriptor = deriveSeraphPresenceState({
      connectionStatus: "connected",
      animationState: "idle",
      isAgentBusy: false,
      pendingApprovalCount: 0,
      pendingNotificationCount: 0,
      queuedInsightCount: 0,
      degradedRouteCount: 0,
      degradedSourceAdapterCount: 0,
      attentionImportedFamilyCount: 0,
      attentionPresenceSurfaceCount: 0,
      actionableThreadCount: 0,
      continuityHealth: "ready",
      recommendedFocus: null,
      recentTraceRole: null,
      recentTraceTool: null,
      latestResponseRole: null,
      ambientState: "idle",
      dataQuality: "memory degraded",
      recentInterventionCount: 0,
      operatorStatus: null,
    });

    expect(descriptor.state).toBe("error");
    expect(descriptor.tone).toBe("error");
  });

  it("treats degraded continuity reach as a fault even without data-quality errors", () => {
    const descriptor = deriveSeraphPresenceState({
      connectionStatus: "connected",
      animationState: "idle",
      isAgentBusy: false,
      pendingApprovalCount: 0,
      pendingNotificationCount: 1,
      queuedInsightCount: 0,
      degradedRouteCount: 2,
      degradedSourceAdapterCount: 1,
      attentionImportedFamilyCount: 1,
      attentionPresenceSurfaceCount: 1,
      actionableThreadCount: 1,
      continuityHealth: "degraded",
      recommendedFocus: "Live delivery",
      recentTraceRole: null,
      recentTraceTool: null,
      latestResponseRole: null,
      ambientState: "idle",
      dataQuality: "good",
      recentInterventionCount: 0,
      operatorStatus: null,
    });

    expect(descriptor.state).toBe("error");
    expect(descriptor.detail).toContain("Cross-surface reach");
  });

  it("falls back to idle when linked and quiet", () => {
    const descriptor = deriveSeraphPresenceState({
      connectionStatus: "connected",
      animationState: "idle",
      isAgentBusy: false,
      pendingApprovalCount: 0,
      pendingNotificationCount: 0,
      queuedInsightCount: 0,
      degradedRouteCount: 0,
      degradedSourceAdapterCount: 0,
      attentionImportedFamilyCount: 0,
      attentionPresenceSurfaceCount: 0,
      actionableThreadCount: 0,
      continuityHealth: "ready",
      recommendedFocus: null,
      recentTraceRole: null,
      recentTraceTool: null,
      latestResponseRole: null,
      ambientState: "idle",
      dataQuality: "good",
      recentInterventionCount: 0,
      operatorStatus: null,
    });

    expect(descriptor.state).toBe("idle");
    expect(descriptor.tone).toBe("neutral");
  });

  it("does not claim healthy readiness when continuity metadata is unavailable", () => {
    const descriptor = deriveSeraphPresenceState({
      metadataState: "unavailable",
      connectionStatus: "connected",
      animationState: "idle",
      isAgentBusy: false,
      pendingApprovalCount: 0,
      pendingNotificationCount: 0,
      queuedInsightCount: 0,
      degradedRouteCount: 0,
      degradedSourceAdapterCount: 0,
      attentionImportedFamilyCount: 0,
      attentionPresenceSurfaceCount: 0,
      actionableThreadCount: 0,
      continuityHealth: null,
      recommendedFocus: null,
      recentTraceRole: null,
      recentTraceTool: null,
      latestResponseRole: null,
      ambientState: "idle",
      dataQuality: null,
      recentInterventionCount: 0,
      operatorStatus: null,
    });

    expect(descriptor.label).toBe("Unknown");
    expect(descriptor.detail).toContain("metadata is unavailable");
    expect(descriptor.tone).toBe("muted");
  });

  it("treats degraded typed adapters as proactive follow-through work when continuity is otherwise healthy", () => {
    const descriptor = deriveSeraphPresenceState({
      connectionStatus: "connected",
      animationState: "idle",
      isAgentBusy: false,
      pendingApprovalCount: 0,
      pendingNotificationCount: 0,
      queuedInsightCount: 0,
      degradedRouteCount: 0,
      degradedSourceAdapterCount: 1,
      attentionImportedFamilyCount: 1,
      attentionPresenceSurfaceCount: 1,
      actionableThreadCount: 0,
      continuityHealth: "attention",
      recommendedFocus: "github-managed",
      recentTraceRole: null,
      recentTraceTool: null,
      latestResponseRole: null,
      ambientState: "idle",
      dataQuality: "good",
      recentInterventionCount: 0,
      operatorStatus: null,
    });

    expect(descriptor.state).toBe("proactive");
    expect(descriptor.detail).toContain("github-managed");
  });
});
