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
});
