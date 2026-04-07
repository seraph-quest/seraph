import { describe, expect, it } from "vitest";

import { deriveSeraphPresenceState } from "./seraphPresence";

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
});
