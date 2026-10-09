import { API_URL } from "../config/constants";
import { apiFetch } from "./api";

export const homeSections = ["active_goals", "programme_status", "task_next_actions", "prepared_outputs", "approvals", "blocked_items"] as const;
export type HomeSectionKey = typeof homeSections[number];
export type HomeTarget =
  | { kind: "inbox"; inbox_id: string; inbox_revision: number }
  | { kind: "goal"; goal_id: string; goal_revision: number }
  | { kind: "programme"; goal_id: string; goal_revision: number; programme_id: string }
  | { kind: "task"; task_id: string; task_revision: number }
  | { kind: "output"; task_id: string; task_revision: number; attempt_id: string }
  | { kind: "approval"; approval_id: string }
  | { kind: "method"; proposal_id: string; version: string; digest: string };
export interface HistoricalMethod {
  status: "admitted" | "baseline" | "unknown";
  method_id: string | null; version: string | null; digest: string | null; admitted_at: string | null;
  lifecycle: "active_metadata" | "rolled_back_metadata" | "suppressed_metadata" | "unavailable" | "unknown";
  reason_code: typeof methodReasons[number] | null; target: Extract<HomeTarget, { kind: "method" }> | null;
}
interface SourceRow { ownership_access: "current" | "recovered_read_only"; source_at: string }
export type HomeItem = SourceRow & (
  | { kind: "active_goal"; goal_id: string; goal_revision: number; status: "active"; sort_order: number; due_at: string | null; title: string | null; target: Extract<HomeTarget, {kind: "goal"}> }
  | { kind: "inbox_decision"; inbox_id: string; inbox_revision: number; source_kind: "source_packet" | "mail_notice" | "guardian_opportunity"; state: "pending" | "snoozed"; title: string; source_availability: "present" | "unavailable"; goal_id: string; goal_revision: number; snoozed_until: string | null; expires_at: string; ownership_access: "current"; target: Extract<HomeTarget, {kind: "inbox"}> }
  | { kind: "programme"; goal_id: string; goal_revision: number; programme_id: string; grant_revision: number; state: "active" | "blocked" | "paused" | "revoked" | "review_due"; reason_code: typeof reasons[number] | null; expires_at: string; next_digest_at: string | null; target: Extract<HomeTarget, {kind: "programme"}> }
  | { kind: "task_next_action"; task_id: string; task_revision: number; goal_id: string; goal_revision: number; status: "triage" | "todo" | "ready" | "running"; priority: number; scheduled_at: string | null; action: "inspect_task" | "review_plan" | "review_result" | "recover_task"; method: HistoricalMethod | null; target: Extract<HomeTarget, {kind: "task"}> }
  | { kind: "prepared_output"; task_id: string; task_revision: number; attempt_id: string; output_state: "prepared" | "blocked" | "unknown"; method: HistoricalMethod | null; target: Extract<HomeTarget, {kind: "output"}> }
  | { kind: "approval"; approval_id: string; status: "pending"; expires_at: string | null; target: Extract<HomeTarget, {kind: "approval"}> }
  | { kind: "blocked_task"; task_id: string; task_revision: number; goal_id: string; goal_revision: number; reason_code: typeof reasons[number]; target: Extract<HomeTarget, {kind: "task"}> }
  | { kind: "blocked_approval"; approval_id: string; reason_code: "approval_expired"; target: Extract<HomeTarget, {kind: "approval"}> }
  | { kind: "blocked_programme"; goal_id: string; goal_revision: number; programme_id: string; grant_revision: number; reason_code: typeof reasons[number]; target: Extract<HomeTarget, {kind: "programme"}> }
);
export interface HomeSection { items: HomeItem[]; state: "ready" | "empty" | "degraded" | "blocked"; source_as_of: string | null }
export type HomeContinuation = Record<HomeSectionKey, HomeSection> & { as_of: string };
export const reasons = ["unknown_effect", "cost_liability", "needs_input", "approval_expired", "general_task_operator_paused", "attempt_limit", "owner_mismatch", "goal_not_active", "goal_revision_changed", "policy_blocked", "source_changed", "recovery_unknown", "programme_blocked", "programme_paused", "programme_revoked", "programme_review_due", "unsupported_source"] as const;
export const methodReasons = ["method_projection_missing", "method_projection_invalid", "method_key_unavailable", "method_canonical_unavailable", "method_suppressed", "method_rolled_back"] as const;
const object = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
const id = (v: unknown) => typeof v === "string" && v.length > 0 && new TextEncoder().encode(v).length <= 512 && new TextDecoder().decode(new TextEncoder().encode(v)) === v;
const integer = (v: unknown) => typeof v === "number" && Number.isInteger(v);
const time = (v: unknown) => typeof v === "string" && /(?:Z|\+00:00)$/.test(v) && Number.isFinite(Date.parse(v));
const sha = (v: unknown) => typeof v === "string" && /^[a-f0-9]{64}$/.test(v);
const nullable = (v: unknown, check: (v: unknown) => boolean) => v === null || check(v);
const oneOf = (v: unknown, values: readonly string[]) => typeof v === "string" && values.includes(v);
function exact(v: Record<string, unknown>, keys: string[]) { return Object.keys(v).length === keys.length && keys.every(k => Object.prototype.hasOwnProperty.call(v, k)); }
const targetKeys: Record<HomeTarget["kind"], string[]> = { inbox: ["inbox_id", "inbox_revision"], goal: ["goal_id", "goal_revision"], programme: ["goal_id", "goal_revision", "programme_id"], task: ["task_id", "task_revision"], output: ["task_id", "task_revision", "attempt_id"], approval: ["approval_id"], method: ["proposal_id", "version", "digest"] };
function target(v: unknown): v is HomeTarget {
  if (!object(v) || typeof v.kind !== "string" || !Object.prototype.hasOwnProperty.call(targetKeys, v.kind)) return false;
  return exact(v, ["kind", ...targetKeys[v.kind as HomeTarget["kind"]]]) && Object.entries(v).every(([k, value]) => k === "kind" || k === "digest" ? k === "kind" || sha(value) : k.endsWith("revision") ? integer(value) && Number(value) >= 1 : id(value));
}
function method(v: unknown): boolean {
  if (v === null) return true;
  if (!object(v) || !exact(v, ["status", "method_id", "version", "digest", "admitted_at", "lifecycle", "reason_code", "target"]) || !oneOf(v.status, ["admitted", "baseline", "unknown"]) || !oneOf(v.lifecycle, ["active_metadata", "rolled_back_metadata", "suppressed_metadata", "unavailable", "unknown"]) || !nullable(v.reason_code, x => oneOf(x, methodReasons))) return false;
  if (v.status === "baseline") return [v.method_id, v.version, v.digest, v.target].every(x => x === null) && nullable(v.admitted_at, time);
  if (v.status === "unknown") return [v.method_id, v.version, v.digest, v.admitted_at, v.target].every(x => x === null);
  if (["unavailable", "unknown", "suppressed_metadata"].includes(String(v.lifecycle)) && v.target !== null) return false;
  return id(v.method_id) && id(v.version) && sha(v.digest) && time(v.admitted_at) && (v.target === null || target(v.target) && v.target.kind === "method" && v.target.proposal_id === v.method_id && v.target.version === v.version && v.target.digest === v.digest);
}
const rowKeys = {
  active_goal: ["goal_id", "goal_revision", "status", "sort_order", "due_at", "title"],
  inbox_decision: ["inbox_id", "inbox_revision", "source_kind", "state", "title", "source_availability", "goal_id", "goal_revision", "snoozed_until", "expires_at"],
  programme: ["goal_id", "goal_revision", "programme_id", "grant_revision", "state", "reason_code", "expires_at", "next_digest_at"],
  task_next_action: ["task_id", "task_revision", "goal_id", "goal_revision", "status", "priority", "scheduled_at", "action", "method"],
  prepared_output: ["task_id", "task_revision", "attempt_id", "output_state", "method"],
  approval: ["approval_id", "status", "expires_at"],
  blocked_task: ["task_id", "task_revision", "goal_id", "goal_revision", "reason_code"],
  blocked_approval: ["approval_id", "reason_code"],
  blocked_programme: ["goal_id", "goal_revision", "programme_id", "grant_revision", "reason_code"],
};
const sectionKinds: Record<HomeSectionKey, string[]> = { active_goals: ["active_goal"], programme_status: ["programme"], task_next_actions: ["task_next_action", "inbox_decision"], prepared_outputs: ["prepared_output"], approvals: ["approval"], blocked_items: ["blocked_task", "blocked_approval", "blocked_programme"] };
function row(v: unknown, section: HomeSectionKey): v is HomeItem {
  if (!object(v) || !oneOf(v.kind, sectionKinds[section]) || !exact(v, ["kind", "ownership_access", "source_at", "target", ...rowKeys[v.kind as keyof typeof rowKeys]]) || !oneOf(v.ownership_access, ["current", "recovered_read_only"]) || !time(v.source_at) || !target(v.target)) return false;
  const expected = v.kind === "inbox_decision" ? "inbox" : v.kind === "active_goal" ? "goal" : v.kind === "prepared_output" ? "output" : String(v.kind).includes("programme") ? "programme" : String(v.kind).includes("approval") ? "approval" : "task";
  if (v.target.kind !== expected || !Object.entries(v.target).every(([k, value]) => k === "kind" || v[k] === value)) return false;
  if (v.kind === "inbox_decision" && (v.ownership_access !== "current" || v.title !== ({source_packet: "Watched source changed", mail_notice: "New message in watched mailbox", guardian_opportunity: "Public evidence opportunity"} as Record<string, string>)[String(v.source_kind)] || !time(v.expires_at))) return false;
  return Object.entries(v).every(([k, value]) => {
    if (k === "programme_id") return typeof value === "string" && /^[a-f0-9]{32}$/.test(value);
    if (k === "task_id" || k === "attempt_id") return typeof value === "string" && /^[A-Za-z0-9_.:/-]{1,512}$/.test(value);
    if (k.endsWith("_id")) return id(value);
    if (k.endsWith("revision")) return integer(value) && Number(value) >= 1;
    if (["due_at", "expires_at", "scheduled_at", "next_digest_at", "snoozed_until"].includes(k)) return nullable(value, time);
    if (k === "sort_order") return integer(value);
    if (k === "priority") return integer(value) && Number(value) >= 0 && Number(value) <= 100;
    if (k === "method") return method(value);
    if (k === "reason_code") return nullable(value, x => oneOf(x, reasons)) && (!String(v.kind).startsWith("blocked_") || value !== null) && (v.kind !== "blocked_approval" || value === "approval_expired");
    if (k === "source_kind") return oneOf(value, ["source_packet", "mail_notice", "guardian_opportunity"]);
    if (k === "source_availability") return oneOf(value, ["present", "unavailable"]);
    if (k === "title" && v.kind === "active_goal") return nullable(value, x => typeof x === "string" && x.trim().length > 0 && [...x].length <= 256 && new TextEncoder().encode(x).length <= 512 && !/[\x00-\x1f\x7f]/.test(x));
    if (k === "state") return oneOf(value, v.kind === "inbox_decision" ? ["pending", "snoozed"] : ["active", "blocked", "paused", "revoked", "review_due"]);
    if (k === "output_state") return oneOf(value, ["prepared", "blocked", "unknown"]);
    if (k === "action") return oneOf(value, ["inspect_task", "review_plan", "review_result", "recover_task"]);
    if (k === "status") return oneOf(value, v.kind === "approval" ? ["pending"] : v.kind === "active_goal" ? ["active"] : ["triage", "todo", "ready", "running"]);
    return true;
  });
}
export function decodeHomeContinuation(value: unknown): HomeContinuation {
  if (!object(value) || !exact(value, [...homeSections, "as_of"]) || !time(value.as_of)) throw new Error("Home metadata has an unsupported schema.");
  let count = 0;
  for (const key of homeSections) {
    const section = value[key];
    if (!object(section) || !exact(section, ["items", "state", "source_as_of"]) || !oneOf(section.state, ["ready", "empty", "degraded", "blocked"]) || !nullable(section.source_as_of, time) || !Array.isArray(section.items) || section.items.length > 20 || !section.items.every(item => row(item, key)) || section.state === "empty" && section.items.length > 0) throw new Error(`Home ${key} metadata is unavailable.`);
    count += section.items.length;
  }
  if (count > 20) throw new Error("Home exceeds its bounded page size.");
  return value as unknown as HomeContinuation;
}
export class HomeContinuationError extends Error {
  constructor(readonly code: string, readonly status: number) { super(code); }
}
export async function fetchHomeContinuation(signal: AbortSignal, cursor: string | null = null) {
  const query = new URLSearchParams({ limit: "20" });
  if (cursor) query.set("cursor", cursor);
  const response = await apiFetch(`${API_URL}/api/operator/continuation?${query}`, { signal });
  const body: unknown = await response.json();
  if (signal.aborted) throw new DOMException("Cancelled", "AbortError");
  if (!response.ok) {
    const detail = object(body) && object(body.detail) ? body.detail : body;
    throw new HomeContinuationError(object(detail) && typeof detail.code === "string" ? detail.code : `HTTP ${response.status}`, response.status);
  }
  const nextCursor = response.headers.get("X-Continuation-Cursor");
  if (nextCursor !== null && (nextCursor.length > 1024 || !/^[A-Za-z0-9_-]+={0,2}$/.test(nextCursor))) throw new Error("Home continuation cursor is unavailable.");
  return { snapshot: decodeHomeContinuation(body), nextCursor };
}
