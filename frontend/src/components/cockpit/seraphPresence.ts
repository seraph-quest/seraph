import type { AgentAnimationState, ConnectionStatus, MessageRole } from "../../types";

export type SeraphPresenceState =
  | "offline"
  | "error"
  | "approval_wait"
  | "tool_use"
  | "thinking"
  | "responding"
  | "proactive"
  | "idle";

export interface SeraphPresenceSnapshot {
  connectionStatus: ConnectionStatus;
  animationState: AgentAnimationState;
  isAgentBusy: boolean;
  pendingApprovalCount: number;
  pendingNotificationCount: number;
  queuedInsightCount: number;
  degradedRouteCount: number;
  degradedSourceAdapterCount: number;
  attentionImportedFamilyCount: number;
  actionableThreadCount: number;
  continuityHealth?: string | null;
  recommendedFocus?: string | null;
  recentTraceRole?: MessageRole | null;
  recentTraceTool?: string | null;
  latestResponseRole?: MessageRole | null;
  ambientState?: string | null;
  dataQuality?: string | null;
  recentInterventionCount: number;
  operatorStatus?: string | null;
}

export interface SeraphPresenceDescriptor {
  state: SeraphPresenceState;
  label: string;
  detail: string;
  tone: "neutral" | "active" | "warning" | "error" | "success" | "muted";
  cadenceMs: number;
}

function containsAny(value: string | null | undefined, parts: string[]): boolean {
  if (!value) {
    return false;
  }
  const normalized = value.toLowerCase();
  return parts.some((part) => normalized.includes(part));
}

export function deriveSeraphPresenceState(snapshot: SeraphPresenceSnapshot): SeraphPresenceDescriptor {
  if (snapshot.connectionStatus === "error") {
    return {
      state: "error",
      label: "Fault",
      detail: "A transport or runtime error needs operator attention.",
      tone: "error",
      cadenceMs: 520,
    };
  }

  if (snapshot.connectionStatus === "disconnected" || snapshot.connectionStatus === "connecting") {
    return {
      state: "offline",
      label: snapshot.connectionStatus === "connecting" ? "Linking" : "Offline",
      detail: "The live workspace is not fully linked to runtime transport.",
      tone: "muted",
      cadenceMs: 1200,
    };
  }

  if (
    snapshot.recentTraceRole === "error"
    || containsAny(snapshot.operatorStatus, ["error", "failed", "fault"])
    || containsAny(snapshot.dataQuality, ["degraded", "failed", "outage"])
    || snapshot.continuityHealth === "degraded"
  ) {
    return {
      state: "error",
      label: "Degraded",
      detail: snapshot.continuityHealth === "degraded"
        ? "Cross-surface reach, imported capability surfaces, or continuity needs operator repair."
        : "Seraph is running with a degraded seam or recent execution failure.",
      tone: "error",
      cadenceMs: 520,
    };
  }

  if (snapshot.pendingApprovalCount > 0) {
    return {
      state: "approval_wait",
      label: "Approval wait",
      detail: "Execution is paused behind one or more approval boundaries.",
      tone: "warning",
      cadenceMs: 900,
    };
  }

  if (snapshot.animationState === "casting" || snapshot.recentTraceTool || snapshot.recentTraceRole === "step") {
    return {
      state: "tool_use",
      label: "Tool use",
      detail: "A tool or workflow step is active in the current thread.",
      tone: "active",
      cadenceMs: 300,
    };
  }

  if (snapshot.isAgentBusy || snapshot.animationState === "thinking") {
    return {
      state: "thinking",
      label: "Thinking",
      detail: "Seraph is reasoning on the next action or reply.",
      tone: "active",
      cadenceMs: 420,
    };
  }

  if (snapshot.animationState === "speaking" || snapshot.latestResponseRole === "agent") {
    return {
      state: "responding",
      label: "Responding",
      detail: "The current thread just produced an assistant response.",
      tone: "success",
      cadenceMs: 700,
    };
  }

  if (
    snapshot.actionableThreadCount > 0
    || snapshot.pendingNotificationCount > 0
    || snapshot.queuedInsightCount > 0
    || snapshot.degradedSourceAdapterCount > 0
    || snapshot.attentionImportedFamilyCount > 0
    || snapshot.latestResponseRole === "proactive"
    || snapshot.ambientState === "has_insight"
    || snapshot.recentInterventionCount > 0
  ) {
    return {
      state: "proactive",
      label: "Advisory",
      detail: snapshot.recommendedFocus
        ? `Cross-surface follow-up is waiting on ${snapshot.recommendedFocus}.`
        : "Guardian continuity and proactive guidance are active.",
      tone: "success",
      cadenceMs: 760,
    };
  }

  return {
    state: "idle",
    label: "Idle",
    detail: "Linked and ready for a new directive.",
    tone: "neutral",
    cadenceMs: 1100,
  };
}
