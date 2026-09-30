import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SeraphPresencePane } from "./SeraphPresencePane";

describe("SeraphPresencePane", () => {
  it("does not claim the queue is clear when presence surfaces need attention", () => {
    render(
      <SeraphPresencePane
        snapshot={{
          connectionStatus: "connected",
          animationState: "idle",
          isAgentBusy: false,
          pendingApprovalCount: 0,
          pendingNotificationCount: 0,
          queuedInsightCount: 0,
          degradedRouteCount: 0,
          degradedSourceAdapterCount: 0,
          attentionImportedFamilyCount: 0,
          attentionPresenceSurfaceCount: 1,
          actionableThreadCount: 0,
          continuityHealth: "attention",
          recommendedFocus: "Telegram relay",
          recentTraceRole: null,
          recentTraceTool: null,
          latestResponseRole: null,
          ambientState: "idle",
          dataQuality: "good",
          recentInterventionCount: 0,
          operatorStatus: null,
        }}
      />,
    );

    expect(screen.getByText("Queue")).toBeInTheDocument();
    expect(screen.getByText("01")).toBeInTheDocument();
    expect(screen.getByText("1 presence surface need attention")).toBeInTheDocument();
    expect(screen.queryByText(/^clear$/i)).not.toBeInTheDocument();
  });

  it("shows unknown continuity metadata instead of defaulting to healthy zeroes", () => {
    render(
      <SeraphPresencePane
        snapshot={{
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
        }}
      />,
    );

    expect(screen.getAllByText("UNKNOWN")).toHaveLength(3);
    expect(screen.getByText("Presence continuity metadata is unavailable. Load it to confirm queue and reach state.")).toBeInTheDocument();
    expect(screen.getByText("follow-through unknown · alerts unknown · bundled unknown")).toBeInTheDocument();
    expect(screen.getByText("reach unknown · load presence continuity to confirm")).toBeInTheDocument();
    expect(screen.queryByText("GOOD")).not.toBeInTheDocument();
    expect(screen.queryByText("CLEAR")).not.toBeInTheDocument();
    expect(screen.queryByText("READY")).not.toBeInTheDocument();
  });

  it("keeps confirmed values visible with an explicit stale marker", () => {
    render(
      <SeraphPresencePane
        snapshot={{
          metadataState: "stale",
          connectionStatus: "connected",
          animationState: "idle",
          isAgentBusy: false,
          pendingApprovalCount: 0,
          pendingNotificationCount: 0,
          queuedInsightCount: 3,
          degradedRouteCount: 0,
          degradedSourceAdapterCount: 0,
          attentionImportedFamilyCount: 0,
          attentionPresenceSurfaceCount: 0,
          actionableThreadCount: 2,
          continuityHealth: "ready",
          recommendedFocus: null,
          recentTraceRole: null,
          recentTraceTool: null,
          latestResponseRole: null,
          ambientState: "idle",
          dataQuality: "good",
          recentInterventionCount: 0,
          operatorStatus: null,
        }}
      />,
    );

    expect(screen.getByText("ADVISORY · STALE")).toBeInTheDocument();
    expect(screen.getByText("last confirmed · good")).toBeInTheDocument();
    expect(screen.getByText("last confirmed · 2 cross-surface threads waiting")).toBeInTheDocument();
    expect(screen.getByText("last confirmed · follow-through 2 · alerts 0 · bundled 3")).toBeInTheDocument();
    expect(screen.getByText("last confirmed · reach ready")).toBeInTheDocument();
  });
});
