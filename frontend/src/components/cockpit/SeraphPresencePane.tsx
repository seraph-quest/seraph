import { useMemo } from "react";

import Seraph, { type SeraphState, type SeraphTelemetryEntry } from "./Seraph";
import {
  deriveSeraphPresenceState,
  type SeraphPresenceDescriptor,
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
  if (snapshot.metadataState === "unavailable") return "UNKNOWN";
  if (snapshot.connectionStatus === "error") return "FAULT";
  if (snapshot.connectionStatus !== "connected") return "OFFLINE";
  if ((snapshot.dataQuality ?? "").toLowerCase().includes("degraded")) return "DEGRADED";
  if (snapshot.isAgentBusy) return "ACTIVE";
  return "GOOD";
}

function withFreshness(snapshot: SeraphPresenceSnapshot, value: string): string {
  return snapshot.metadataState === "stale" ? `last confirmed · ${value}` : value;
}

function contextHint(snapshot: SeraphPresenceSnapshot): string {
  if (snapshot.metadataState === "unavailable") {
    return "continuity unavailable · load to confirm";
  }
  return withFreshness(snapshot, (snapshot.dataQuality ?? (
    snapshot.connectionStatus === "connected" ? "live link" : "direct fallback"
  )).replace(/_/g, " "));
}

function queueLabel(snapshot: SeraphPresenceSnapshot): string {
  if (snapshot.metadataState === "unavailable") return "UNKNOWN";
  const total = snapshot.pendingApprovalCount
    + snapshot.actionableThreadCount
    + snapshot.degradedRouteCount
    + snapshot.degradedSourceAdapterCount
    + snapshot.attentionImportedFamilyCount
    + snapshot.attentionPresenceSurfaceCount;
  return total > 0 ? total.toString().padStart(2, "0") : "CLEAR";
}

function queueHint(snapshot: SeraphPresenceSnapshot): string {
  if (snapshot.metadataState === "unavailable") {
    return "load presence continuity to confirm queued work";
  }
  if (snapshot.pendingApprovalCount > 0) {
    return withFreshness(snapshot, `${snapshot.pendingApprovalCount} approval waiting`);
  }
  if (snapshot.actionableThreadCount > 0) {
    return withFreshness(snapshot, `${snapshot.actionableThreadCount} cross-surface thread${snapshot.actionableThreadCount === 1 ? "" : "s"} waiting`);
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
    return withFreshness(snapshot, reachHints);
  }
  if (snapshot.recentInterventionCount > 0) {
    return withFreshness(snapshot, `${snapshot.recentInterventionCount} continuity events`);
  }
  return withFreshness(snapshot, "clear");
}

function reachLabel(snapshot: SeraphPresenceSnapshot): string {
  if (snapshot.metadataState === "unavailable") return "UNKNOWN";
  const issues = snapshot.degradedRouteCount
    + snapshot.degradedSourceAdapterCount
    + snapshot.attentionImportedFamilyCount
    + snapshot.attentionPresenceSurfaceCount;
  if (issues > 0) {
    return `WARN ${issues}`;
  }
  return snapshot.connectionStatus === "connected" ? "READY" : "LINK";
}

function reachHint(snapshot: SeraphPresenceSnapshot): string {
  if (snapshot.metadataState === "unavailable") {
    return "load presence continuity to confirm reach";
  }
  const hint = snapshot.degradedRouteCount > 0
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
      : "browser and desktop linked";
  return withFreshness(snapshot, hint);
}

function descriptorForSnapshot(snapshot: SeraphPresenceSnapshot): SeraphPresenceDescriptor {
  const descriptor = deriveSeraphPresenceState(snapshot);
  if (snapshot.metadataState !== "stale") return descriptor;
  return {
    ...descriptor,
    label: `${descriptor.label} · Stale`,
    detail: `Last confirmed: ${descriptor.detail} Refresh presence continuity to confirm current state.`,
    tone: descriptor.tone === "neutral" || descriptor.tone === "success" ? "warning" : descriptor.tone,
  };
}

export function SeraphPresencePane({ snapshot, isSelected = false }: SeraphPresencePaneProps) {
  const descriptor = useMemo(() => descriptorForSnapshot(snapshot), [snapshot]);
  const metadataUnavailable = snapshot.metadataState === "unavailable";
  const metadataStale = snapshot.metadataState === "stale";
  const telemetry = useMemo<SeraphTelemetryEntry[]>(
    () => [
      {
        label: "Context",
        value: contextLabel(snapshot),
        hint: contextHint(snapshot),
      },
      {
        label: "Queue",
        value: queueLabel(snapshot),
        hint: queueHint(snapshot),
      },
      {
        label: "Reach",
        value: reachLabel(snapshot),
        hint: reachHint(snapshot),
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
          {metadataUnavailable
            ? "follow-through unknown · alerts unknown · bundled unknown"
            : `${metadataStale ? "last confirmed · " : ""}follow-through ${snapshot.actionableThreadCount} · alerts ${snapshot.pendingNotificationCount} · bundled ${snapshot.queuedInsightCount}`}
        </div>
        <div className="cockpit-sublist-item">
          {metadataUnavailable
            ? "reach unknown · load presence continuity to confirm"
            : `${metadataStale ? "last confirmed · " : ""}reach ${snapshot.degradedRouteCount > 0 ? `${snapshot.degradedRouteCount} degraded routes` : "ready"}`}
          {!metadataUnavailable && snapshot.degradedSourceAdapterCount > 0 ? ` · ${snapshot.degradedSourceAdapterCount} adapters degraded` : ""}
          {!metadataUnavailable && snapshot.attentionPresenceSurfaceCount > 0 ? ` · ${snapshot.attentionPresenceSurfaceCount} presence attention` : ""}
          {!metadataUnavailable && snapshot.attentionImportedFamilyCount > 0 ? ` · ${snapshot.attentionImportedFamilyCount} imported attention` : ""}
          {!metadataUnavailable && snapshot.recommendedFocus ? ` · focus ${snapshot.recommendedFocus}` : ""}
        </div>
      </div>
    </section>
  );
}
