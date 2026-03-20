import { useMemo } from "react";

import Seraph, { type SeraphState, type SeraphTelemetryEntry } from "./Seraph";
import {
  deriveSeraphPresenceState,
  type SeraphPresenceSnapshot,
  type SeraphPresenceState,
} from "./seraphPresence";

interface SeraphPresencePaneProps {
  snapshot: SeraphPresenceSnapshot;
}

function toSeraphState(state: SeraphPresenceState): SeraphState {
  switch (state) {
    case "offline":
    case "error":
    case "approval_wait":
    case "tool_use":
    case "thinking":
    case "idle":
      return state;
    case "responding":
    case "proactive":
      return "idle";
  }
}

function contextLabel(snapshot: SeraphPresenceSnapshot): string {
  if (snapshot.connectionStatus === "error") return "FAULT";
  if (snapshot.connectionStatus !== "connected") return "OFFLINE";
  if ((snapshot.dataQuality ?? "").toLowerCase().includes("degraded")) return "DEGRADED";
  if (snapshot.isAgentBusy) return "ACTIVE";
  return "GOOD";
}

function queueLabel(snapshot: SeraphPresenceSnapshot): string {
  const total = snapshot.pendingApprovalCount + snapshot.recentInterventionCount;
  return total > 0 ? total.toString().padStart(2, "0") : "CLEAR";
}

export function SeraphPresencePane({ snapshot }: SeraphPresencePaneProps) {
  const descriptor = useMemo(() => deriveSeraphPresenceState(snapshot), [snapshot]);
  const telemetry = useMemo<SeraphTelemetryEntry[]>(
    () => [
      {
        label: "Context",
        value: contextLabel(snapshot),
        hint: (snapshot.dataQuality ?? "runtime linked").replace(/_/g, " "),
      },
      {
        label: "Queue",
        value: queueLabel(snapshot),
        hint:
          snapshot.pendingApprovalCount > 0
            ? `${snapshot.pendingApprovalCount} approval waiting`
            : snapshot.recentInterventionCount > 0
              ? `${snapshot.recentInterventionCount} continuity events`
              : "clear",
      },
    ],
    [snapshot],
  );

  return (
    <section className="cockpit-panel cockpit-panel--embedded cockpit-presence-panel" aria-label="Seraph presence">
      <Seraph
        state={toSeraphState(descriptor.state)}
        detail={descriptor.detail}
        telemetry={telemetry}
        statusLabel={descriptor.label.toUpperCase()}
      />
    </section>
  );
}
