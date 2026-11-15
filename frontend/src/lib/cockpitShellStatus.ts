// Compact, honest shell status for the cockpit header (#1172).
//
// Every item names its source endpoint, and every state that is not "ready"
// carries a reason and, where one exists, the action that resolves it. The
// derivations are pure so the header, the status popover and tests share one
// truth.

import type { ConnectionStatus } from "../types";
import { isSuccessfulModelFabricOutcome } from "./modelFabric";
import type { SettingsSectionId } from "./settingsNavigation";

export type ShellStatusTone = "ok" | "info" | "warning" | "error" | "unknown";

export type ShellStatusAction =
  | { kind: "settings"; label: string; section: SettingsSectionId }
  | { kind: "refresh"; label: string }
  | { kind: "load-presence"; label: string };

export interface ShellStatusItem {
  id: "connection" | "chat_model" | "context" | "desktop" | "policy" | "interruptions" | "version";
  label: string;
  value: string;
  tone: ShellStatusTone;
  /** Why the item is in this state. Required for every tone except "ok". */
  reason: string | null;
  /** Where the value came from, so the operator can verify it. */
  source: string;
  action: ShellStatusAction | null;
}

/** Subset of `/api/runtime/status` the shell reads. Top-level provider/model are
 * deliberately absent: they describe a configured default, not the chat path. */
export interface ShellRuntimeStatus {
  build_id?: string | null;
  version?: string | null;
  effective_runtime?: {
    runtime_path?: string;
    active_profile?: string;
    provider_label?: string;
    model?: string;
    model_label?: string;
    route_label?: string;
    inference_ready?: boolean;
    inference_readiness?: { status?: string; reasons?: string[] };
  };
  model_fabric?: {
    status?: string;
    runtime_paths?: Record<string, ShellFabricRoute | undefined>;
    workloads?: Record<string, ShellFabricRoute | undefined>;
  } | null;
}

export interface ShellFabricReceipt {
  profile_id: string;
  model: string;
  outcome?: string;
}

export interface ShellFabricRoute {
  succeeded?: ShellFabricReceipt | null;
  attempted?: ShellFabricReceipt | null;
  attempt_count?: number;
  last_outcome?: string | null;
  persistence?: string | null;
  fallback_used?: boolean | null;
  degradation_codes?: string[];
}

export type RuntimeReceiptSourceKind = "runtime_status" | "operator_posture" | "retained";

function humanize(value: string): string {
  return value.replace(/[_:]+/g, " ").replace(/\s+/g, " ").trim();
}

/**
 * A chat route counts as degraded only on evidence from an actual attempt.
 * `persistence: "unknown"` with zero attempts is the normal state of a route
 * that has not been used since the backend started and is not a fault.
 */
export function chatFabricRouteDegraded(route: ShellFabricRoute | null | undefined): boolean {
  if (!route) return false;
  const attempts = typeof route.attempt_count === "number" ? route.attempt_count : 0;
  if (route.last_outcome != null && !isSuccessfulModelFabricOutcome(route.last_outcome)) return true;
  if (route.persistence == null || route.persistence === "persisted") return false;
  if (route.persistence === "unknown") return attempts > 0;
  return true;
}

export interface ChatRuntimeIdentity {
  route: string | null;
  model: string | null;
  /** Human label: "NEAR TEE text slot · DeepSeek-V4.1-Flash-1017". */
  label: string;
  tone: ShellStatusTone;
  reason: string | null;
  stale: boolean;
  /** Last governed chat receipt, when the model fabric recorded one. */
  lastReceipt: string | null;
}

/**
 * The model chat actually uses comes from `effective_runtime` (runtime path
 * `chat_agent`). The top-level `provider`/`model` fields are configured
 * defaults and may name a retired provider, so they are never shown as the
 * chat model.
 */
export function deriveChatRuntimeIdentity(
  status: ShellRuntimeStatus | null | undefined,
  source: RuntimeReceiptSourceKind | null | undefined,
): ChatRuntimeIdentity {
  if (!status) {
    return {
      route: null,
      model: null,
      label: "Not reported yet",
      tone: "unknown",
      reason: "Runtime status has not loaded in this browser session yet.",
      stale: false,
      lastReceipt: null,
    };
  }
  const effective = status.effective_runtime;
  const fabricRoute = status.model_fabric?.runtime_paths?.chat_agent ?? status.model_fabric?.workloads?.interactive;
  const receipt = fabricRoute?.succeeded ?? fabricRoute?.attempted ?? null;
  const lastReceipt = receipt
    ? `${receipt.profile_id} · ${receipt.model}${fabricRoute?.succeeded ? "" : receipt.outcome ? ` · ${humanize(receipt.outcome)}` : ""}${fabricRoute?.fallback_used ? " · fallback" : ""}`
    : null;
  if (!effective && receipt) {
    // Older runtime payloads without effective_runtime: a governed chat
    // receipt is still evidence of the path actually used.
    const degraded = chatFabricRouteDegraded(fabricRoute);
    const attemptedOnly = !fabricRoute?.succeeded;
    return {
      route: receipt.profile_id,
      model: receipt.model,
      label: `${receipt.profile_id} · ${receipt.model}`,
      tone: source === "retained" ? "unknown" : degraded ? "warning" : "ok",
      reason: [
        "The backend did not report an effective chat route; this is the last governed chat receipt.",
        attemptedOnly ? `Last attempt ${humanize(receipt.outcome ?? "unknown")}; no successful receipt yet.` : null,
        fabricRoute?.fallback_used ? "A fallback route was used." : null,
        degraded && fabricRoute?.degradation_codes?.length ? `Degradation: ${fabricRoute.degradation_codes.map(humanize).join(", ")}.` : null,
      ].filter(Boolean).join(" "),
      stale: source === "retained",
      lastReceipt,
    };
  }
  if (!effective) {
    return {
      route: null,
      model: null,
      label: "Route not reported",
      tone: "unknown",
      reason: "The backend did not report an effective chat route. The configured default is not shown because it may not be the path chat uses.",
      stale: source === "retained",
      lastReceipt: null,
    };
  }
  const route = effective.route_label?.trim() || effective.provider_label?.trim() || null;
  const model = effective.model_label?.trim() || effective.model?.trim() || null;
  const label = [route, model].filter(Boolean).join(" · ") || "Route not reported";
  const stale = source === "retained";
  const readiness = effective.inference_readiness;
  const reasons = readiness?.reasons?.filter(Boolean) ?? [];
  const blocked = effective.inference_ready === false || readiness?.status === "configuration_required" || readiness?.status === "blocked";
  const reasonList = reasons.length ? `: ${reasons.map(humanize).join(", ")}` : "";
  let tone: ShellStatusTone = "ok";
  const notes: string[] = [];
  if (blocked) {
    tone = "error";
    const status = readiness?.status && readiness.status !== "blocked" ? ` (readiness: ${humanize(readiness.status)})` : "";
    notes.push(`Chat inference is blocked${status}${reasonList}.`);
  } else if (readiness?.status === "degraded") {
    tone = "warning";
    notes.push(`Chat inference is degraded${reasonList}.`);
  }
  if (chatFabricRouteDegraded(fabricRoute)) {
    if (tone === "ok") tone = "warning";
    const codes = fabricRoute?.degradation_codes?.filter(Boolean) ?? [];
    notes.push(`The last chat attempt did not complete cleanly${fabricRoute?.last_outcome ? ` (${humanize(fabricRoute.last_outcome)})` : ""}${codes.length ? `: ${codes.map(humanize).join(", ")}` : ""}.`);
  }
  if (status.model_fabric?.status === "degraded") {
    if (tone === "ok") tone = "warning";
    notes.push("The model fabric reports a degraded state.");
  }
  if (stale) {
    // A retained receipt is never presented as current truth; keep what it
    // last said so a blocked route stays visibly blocked.
    return {
      route,
      model,
      label,
      tone: "unknown",
      reason: ["Last confirmed value; the latest runtime status refresh has not answered.", ...notes].join(" "),
      stale,
      lastReceipt,
    };
  }
  return { route, model, label, tone, reason: notes.length ? notes.join(" ") : null, stale, lastReceipt };
}

export function deriveVersionItem(
  uiBuildId: string,
  status: ShellRuntimeStatus | null | undefined,
  source: RuntimeReceiptSourceKind | null | undefined,
): ShellStatusItem {
  const backendBuild = source === "retained" ? null : status?.build_id?.trim() || null;
  if (!backendBuild) {
    return {
      id: "version",
      label: "Version",
      value: uiBuildId,
      tone: "unknown",
      reason: "This is the browser bundle's build. The backend build has not been confirmed in this session.",
      source: "frontend bundle · /api/runtime/status",
      action: { kind: "refresh", label: "Check again" },
    };
  }
  if (backendBuild !== uiBuildId) {
    return {
      id: "version",
      label: "Version",
      value: `UI ${uiBuildId} · backend ${backendBuild}`,
      tone: "warning",
      reason: "The browser bundle and the backend report different builds. Reload the page after an update; if it persists, restart Seraph with manage.sh.",
      source: "frontend bundle · /api/runtime/status",
      action: null,
    };
  }
  return {
    id: "version",
    label: "Version",
    value: backendBuild,
    tone: "ok",
    reason: null,
    source: "/api/runtime/status",
    action: null,
  };
}

export interface ShellDaemonPresence {
  connected?: boolean;
  daemon_state?: string | null;
  last_error_kind?: string | null;
  status_reason?: string | null;
  recovery_hint?: string | null;
}

/** True when the native desktop daemon is off because configuration says so. */
export function daemonConfiguredOff(daemon: ShellDaemonPresence | null | undefined): boolean {
  return daemon?.last_error_kind === "configured_off" || daemon?.daemon_state === "disabled";
}

export function deriveDesktopItem(
  daemon: ShellDaemonPresence | null | undefined,
  presenceLoad: "unavailable" | "confirmed" | "stale",
  checkFailed = false,
): ShellStatusItem {
  if ((presenceLoad === "unavailable" || !daemon) && checkFailed) {
    return {
      id: "desktop",
      label: "Desktop presence",
      value: "Unavailable",
      tone: "unknown",
      reason: "The continuity check did not return confirmed metadata, so desktop presence and the deferred queue are unknown.",
      source: "/api/observer/continuity",
      action: { kind: "load-presence", label: "Retry" },
    };
  }
  if (presenceLoad === "unavailable" || !daemon) {
    return {
      id: "desktop",
      label: "Desktop presence",
      value: "Not checked yet",
      tone: "unknown",
      reason: "Desktop continuity has not been loaded in this session.",
      source: "/api/observer/continuity",
      action: { kind: "load-presence", label: "Check now" },
    };
  }
  const staleSuffix = presenceLoad === "stale" ? " (last confirmed)" : "";
  if (daemon.connected) {
    return {
      id: "desktop",
      label: "Desktop presence",
      value: `Linked${staleSuffix}`,
      tone: presenceLoad === "stale" ? "unknown" : "ok",
      reason: presenceLoad === "stale" ? "The latest refresh failed; showing the last confirmed state." : null,
      source: "/api/observer/continuity",
      action: presenceLoad === "stale" ? { kind: "load-presence", label: "Retry" } : null,
    };
  }
  if (daemonConfiguredOff(daemon)) {
    return {
      id: "desktop",
      label: "Desktop presence",
      value: `Off by configuration${staleSuffix}`,
      tone: "info",
      reason: [
        daemon.status_reason || "The native desktop daemon is turned off for this environment.",
        "Chat, work and approvals are unaffected; desktop notifications and screen context are unavailable.",
        daemon.recovery_hint ? `To enable it: ${daemon.recovery_hint}` : null,
      ].filter(Boolean).join(" "),
      source: "/api/observer/continuity",
      action: { kind: "settings", label: "Open Settings → Native", section: "native" },
    };
  }
  return {
    id: "desktop",
    label: "Desktop presence",
    value: `Not linked${staleSuffix}`,
    tone: "warning",
    reason: [
      daemon.status_reason || "The native desktop daemon is enabled but has not posted recently.",
      daemon.recovery_hint ?? null,
    ].filter(Boolean).join(" "),
    source: "/api/observer/continuity",
    action: { kind: "settings", label: "Open Settings → Native", section: "native" },
  };
}

export type ObserverContextKind = "unknown" | "good" | "not_refreshed" | "unexplained" | "fault";

export interface ObserverContextClass {
  kind: ObserverContextKind;
  /** Sources that raised or have no runner. */
  erroredSources: string[];
  /** Sources that reported themselves degraded. */
  degradedSources: string[];
  noActiveSources: boolean;
}

/**
 * Classify `/api/observer/state.data_quality` with its `data_quality_reason`
 * (#1172). Only an operator scope that has not been refreshed yet is an
 * expected state; a failing, degraded or missing source is a fault. A
 * backend that reports no reason cannot be called expected.
 */
export function classifyObserverContext(
  dataQuality: string | null | undefined,
  reason: string | null | undefined,
): ObserverContextClass {
  const reasons = (reason ?? "").split(",").map((entry) => entry.trim()).filter(Boolean);
  const erroredSources = reasons.filter((entry) => entry.startsWith("source_error:")).map((entry) => entry.slice("source_error:".length));
  const degradedSources = reasons.filter((entry) => entry.startsWith("source_degraded:")).map((entry) => entry.slice("source_degraded:".length));
  const noActiveSources = reasons.includes("no_active_sources");
  const base = { erroredSources, degradedSources, noActiveSources };
  if (!dataQuality) return { kind: "unknown", ...base };
  if (dataQuality === "good") return { kind: "good", ...base };
  const quality = dataQuality.toLowerCase();
  if (quality === "stale" || quality.includes("failed") || quality.includes("outage")
    || erroredSources.length || degradedSources.length || noActiveSources) {
    return { kind: "fault", ...base };
  }
  if (reasons.length && reasons.every((entry) => entry === "unscoped_fallback")) return { kind: "not_refreshed", ...base };
  return { kind: "unexplained", ...base };
}

export function describeObserverFault(context: ObserverContextClass): string {
  const parts = [
    context.erroredSources.length ? `failed: ${context.erroredSources.join(", ")}` : null,
    context.degradedSources.length ? `reported degraded: ${context.degradedSources.join(", ")}` : null,
    context.noActiveSources ? "no observer source is enabled" : null,
  ].filter(Boolean);
  return parts.length ? `Observer sources ${parts.join("; ")}.` : "No observer source refreshed successfully.";
}

export function deriveContextItem(
  dataQuality: string | null | undefined,
  observerLoaded: boolean,
  dataQualityReason: string | null = null,
): ShellStatusItem {
  const source = "/api/observer/state · data_quality";
  if (!observerLoaded || !dataQuality) {
    return {
      id: "context",
      label: "Context signals",
      value: "Not reported yet",
      tone: "unknown",
      reason: "Observer state has not loaded in this session yet.",
      source: "/api/observer/state",
      action: { kind: "refresh", label: "Check again" },
    };
  }
  const context = classifyObserverContext(dataQuality, dataQualityReason);
  if (context.kind === "good") {
    return { id: "context", label: "Context signals", value: "Complete", tone: "ok", reason: null, source: "/api/observer/state", action: null };
  }
  if (context.kind === "not_refreshed") {
    return {
      id: "context",
      label: "Context signals",
      value: "Not refreshed yet for this session",
      tone: "info",
      reason: "The observer has not refreshed its sources for this operator session yet, so proactive timing uses less context until it does. Chat and work are unaffected.",
      source,
      action: { kind: "refresh", label: "Check again" },
    };
  }
  if (context.kind === "fault") {
    return {
      id: "context",
      label: "Context signals",
      value: dataQuality === "stale" ? "Unavailable" : "Degraded",
      tone: "warning",
      reason: `${describeObserverFault(context)} Proactive timing and guidance have less context until the sources recover; chat and work are unaffected. Observer sources are managed by the Core Observer Sources package (Extension studio); check the backend log for the failing source.`,
      source: `${source} · data_quality_reason`,
      action: { kind: "refresh", label: "Check again" },
    };
  }
  return {
    id: "context",
    label: "Context signals",
    value: "Partial · reason not reported",
    tone: "warning",
    reason: "Some observer sources did not refresh and this backend does not report why, so this may be a fault. Chat and work are unaffected; proactive timing uses less context.",
    source,
    action: { kind: "refresh", label: "Check again" },
  };
}

export function deriveConnectionItem(status: ConnectionStatus): ShellStatusItem {
  if (status === "connected") {
    return { id: "connection", label: "Live link", value: "Connected", tone: "ok", reason: null, source: "chat WebSocket", action: null };
  }
  if (status === "connecting") {
    return {
      id: "connection",
      label: "Live link",
      value: "Connecting",
      tone: "unknown",
      reason: "Opening the chat WebSocket. Messages sent now use the direct chat fallback.",
      source: "chat WebSocket",
      action: null,
    };
  }
  return {
    id: "connection",
    label: "Live link",
    value: status === "error" ? "Error" : "Offline",
    tone: status === "error" ? "error" : "warning",
    reason: "The chat WebSocket is not connected; it retries automatically. Messages fall back to direct chat, and live updates pause until it reconnects.",
    source: "chat WebSocket",
    action: null,
  };
}

export function derivePolicyItem(tools: string, mcp: string, approvals: string): ShellStatusItem {
  const known = tools !== "unknown" && mcp !== "unknown" && approvals !== "unknown";
  return {
    id: "policy",
    label: "Tools & approvals",
    value: known
      ? `Tools ${humanize(tools)} · MCP ${humanize(mcp)} · Approvals ${humanize(approvals)}`
      : "Not reported yet",
    tone: known ? "ok" : "unknown",
    reason: known ? null : "Policy modes have not loaded in this session yet.",
    source: "/api/settings/{tool,mcp}-policy-mode · /api/settings/approval-mode",
    action: { kind: "settings", label: "Open Settings → Policies", section: "policies" },
  };
}

/**
 * Proactive interruption budget, plus the deferred bundle queue (nudges held
 * back for bundled delivery) when continuity metadata has been confirmed.
 */
export function deriveInterruptionItem(
  remaining: number | null | undefined,
  observerLoaded: boolean,
  queuedBundleCount: number | null = null,
): ShellStatusItem {
  const queued = queuedBundleCount === null
    ? ""
    : ` · ${queuedBundleCount} deferred nudge${queuedBundleCount === 1 ? "" : "s"} queued`;
  if (!observerLoaded || typeof remaining !== "number") {
    return {
      id: "interruptions",
      label: "Interruption budget",
      value: queued ? `Budget not reported${queued}` : "Not reported yet",
      tone: "unknown",
      reason: "Observer state has not loaded in this session yet.",
      source: "/api/observer/state · attention_budget_remaining",
      action: { kind: "refresh", label: "Check again" },
    };
  }
  return {
    id: "interruptions",
    label: "Interruption budget",
    value: `${remaining} proactive nudge${remaining === 1 ? "" : "s"} left${queued}`,
    tone: remaining > 0 ? "ok" : "info",
    reason: remaining > 0 ? null : "Seraph has used its proactive interruption budget; it will hold non-urgent nudges until the budget resets.",
    source: "/api/observer/state · attention_budget_remaining",
    action: { kind: "settings", label: "Open Settings → General", section: "general" },
  };
}

const TONE_RANK: Record<ShellStatusTone, number> = { ok: 0, info: 0, unknown: 1, warning: 2, error: 3 };

export interface ShellStatusSummary {
  tone: ShellStatusTone;
  label: string;
  attentionCount: number;
}

/** Info items (expected configuration) never raise the overall status. */
export function summarizeShellStatus(items: ShellStatusItem[]): ShellStatusSummary {
  const worst = items.reduce<ShellStatusTone>((current, item) => (TONE_RANK[item.tone] > TONE_RANK[current] ? item.tone : current), "ok");
  const attentionCount = items.filter((item) => item.tone === "warning" || item.tone === "error").length;
  const pendingCount = items.filter((item) => item.tone === "unknown").length;
  if (worst === "error" || worst === "warning") {
    return { tone: worst, label: `${attentionCount} need${attentionCount === 1 ? "s" : ""} attention`, attentionCount };
  }
  if (worst === "unknown") {
    return { tone: "unknown", label: `${pendingCount} not confirmed`, attentionCount: 0 };
  }
  return { tone: "ok", label: "All systems ready", attentionCount: 0 };
}
