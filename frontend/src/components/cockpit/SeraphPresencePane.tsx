import { useMemo } from "react";

import Seraph, { type SeraphState, type SeraphTelemetryEntry } from "./Seraph";
import {
  deriveSeraphPresenceState,
  type SeraphPresenceSnapshot,
  type SeraphPresenceState,
} from "./seraphPresence";

interface SeraphPresencePaneProps {
  snapshot: SeraphPresenceSnapshot;
  isSelected?: boolean;
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
  const total = snapshot.pendingApprovalCount
    + snapshot.actionableThreadCount
    + snapshot.degradedRouteCount
    + snapshot.degradedSourceAdapterCount
    + snapshot.attentionImportedFamilyCount
    + snapshot.attentionPresenceSurfaceCount;
  return total > 0 ? total.toString().padStart(2, "0") : "CLEAR";
}

function queueHint(snapshot: SeraphPresenceSnapshot): string {
  if (snapshot.pendingApprovalCount > 0) {
    return `${snapshot.pendingApprovalCount} approval waiting`;
  }
  if (snapshot.actionableThreadCount > 0) {
    return `${snapshot.actionableThreadCount} cross-surface thread${snapshot.actionableThreadCount === 1 ? "" : "s"} waiting`;
  }
  const reachHints = [
    snapshot.degradedRouteCount > 0
      ? `${snapshot.degradedRouteCount} route${snapshot.degradedRouteCount === 1 ? "" : "s"} need repair`
      : null,
    snapshot.degradedSourceAdapterCount > 0
      ? `${snapshot.degradedSourceAdapterCount} adapter${snapshot.degradedSourceAdapterCount === 1 ? "" : "s"} degraded`
      : null,
    snapshot.attentionPresenceSurfaceCount > 0
      ? `${snapshot.attentionPresenceSurfaceCount} presence surface${snapshot.attentionPresenceSurfaceCount === 1 ? "" : "s"} need attention`
      : null,
    snapshot.attentionImportedFamilyCount > 0
      ? `${snapshot.attentionImportedFamilyCount} imported famil${snapshot.attentionImportedFamilyCount === 1 ? "y" : "ies"} need attention`
      : null,
  ].filter(Boolean).join(" · ");
  if (reachHints) {
    return reachHints;
  }
  if (snapshot.recentInterventionCount > 0) {
    return `${snapshot.recentInterventionCount} continuity events`;
  }
  return "clear";
}

function reachLabel(snapshot: SeraphPresenceSnapshot): string {
  const issues = snapshot.degradedRouteCount
    + snapshot.degradedSourceAdapterCount
    + snapshot.attentionImportedFamilyCount
    + snapshot.attentionPresenceSurfaceCount;
  if (issues > 0) {
    return `WARN ${issues}`;
  }
  return snapshot.connectionStatus === "connected" ? "READY" : "LINK";
}

export function SeraphPresencePane({ snapshot, isSelected = false }: SeraphPresencePaneProps) {
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
        hint: queueHint(snapshot),
      },
      {
        label: "Reach",
        value: reachLabel(snapshot),
        hint: snapshot.degradedRouteCount > 0
          || snapshot.degradedSourceAdapterCount > 0
          || snapshot.attentionImportedFamilyCount > 0
          || snapshot.attentionPresenceSurfaceCount > 0
          ? [
            snapshot.degradedRouteCount > 0
              ? `${snapshot.degradedRouteCount} route${snapshot.degradedRouteCount === 1 ? "" : "s"} need repair`
              : null,
            snapshot.degradedSourceAdapterCount > 0
              ? `${snapshot.degradedSourceAdapterCount} adapter${snapshot.degradedSourceAdapterCount === 1 ? "" : "s"} degraded`
              : null,
            snapshot.attentionPresenceSurfaceCount > 0
              ? `${snapshot.attentionPresenceSurfaceCount} presence surface${snapshot.attentionPresenceSurfaceCount === 1 ? "" : "s"} need attention`
              : null,
            snapshot.attentionImportedFamilyCount > 0
              ? `${snapshot.attentionImportedFamilyCount} imported famil${snapshot.attentionImportedFamilyCount === 1 ? "y" : "ies"} need attention`
              : null,
          ].filter(Boolean).join(" · ")
          : snapshot.pendingNotificationCount > 0
            ? `${snapshot.pendingNotificationCount} desktop alert${snapshot.pendingNotificationCount === 1 ? "" : "s"} pending`
            : "browser and desktop linked",
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
        dividerColor={isSelected ? "rgba(141,226,255,0.2)" : "rgba(141,226,255,0.12)"}
        edgeColor={isSelected ? "rgba(141,226,255,0.2)" : "rgba(141,226,255,0.12)"}
      />
      <div className="cockpit-sublist">
        <div className="cockpit-sublist-item">
          follow-through {snapshot.actionableThreadCount} · alerts {snapshot.pendingNotificationCount} · bundled {snapshot.queuedInsightCount}
        </div>
        <div className="cockpit-sublist-item">
          reach {snapshot.degradedRouteCount > 0 ? `${snapshot.degradedRouteCount} degraded routes` : "ready"}
          {snapshot.degradedSourceAdapterCount > 0 ? ` · ${snapshot.degradedSourceAdapterCount} adapters degraded` : ""}
          {snapshot.attentionPresenceSurfaceCount > 0 ? ` · ${snapshot.attentionPresenceSurfaceCount} presence attention` : ""}
          {snapshot.attentionImportedFamilyCount > 0 ? ` · ${snapshot.attentionImportedFamilyCount} imported attention` : ""}
          {snapshot.recommendedFocus ? ` · focus ${snapshot.recommendedFocus}` : ""}
        </div>
      </div>
    </section>
  );
}
