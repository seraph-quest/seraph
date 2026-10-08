import { API_URL } from "../config/constants";
import { apiFetch } from "./api";
import { withMailDeadline } from "./mailApi";

export type SyncProvider = "gmail" | "calendar";
export interface SourceItemRef { provider: SyncProvider; opaque_id: string; revision: string; content_digest: string; privacy: "owner_private"; expires_at: string }
export interface ConnectedSource { connection_ref: { id: string; revision: number }; item_refs: SourceItemRef[] }
export interface RelatedSelection { sources: ConnectedSource[]; acknowledged: boolean }
export function connectedTaskInput(selection: RelatedSelection | null): { connected_sources?: ConnectedSource[]; acknowledge_connected_sources?: true } {
  if (!selection?.sources.length) return {};
  if (!selection.acknowledged) throw Error("Acknowledge the exact local related references before creating the task.");
  const sources = connectedSources(selection.sources);
  if (sources.some(group => group.item_refs.some(ref => Date.parse(ref.expires_at) <= Date.now()))) throw Error("Related source references expired; refresh and select current references.");
  return { connected_sources: sources, acknowledge_connected_sources: true };
}
export function normalizeConnectedRequest<T extends { connected_sources?: ConnectedSource[]; acknowledge_connected_sources?: true }>(request: T): T {
  const { connected_sources, acknowledge_connected_sources, ...legacy } = request;
  return { ...legacy, ...connectedTaskInput(connected_sources?.length ? { sources: connected_sources, acknowledged: acknowledge_connected_sources === true } : null) } as T;
}
export interface RelatedSources { classification: "local_related_context_not_model_input"; memory_status: "no_learning"; sources: (ConnectedSource & { coverage: SyncProjection["coverage"]; freshness: SyncProjection["freshness"] })[] }
export function connectedSources(value: unknown): ConnectedSource[] {
  if (!Array.isArray(value) || value.length > 3) throw Error("Related sources exceed the connection limit.");
  const connections = new Set<string>(), refs = new Set<string>();
  return value.map(group => {
    if (!record(group) || !record(group.connection_ref) || !text(group.connection_ref.id) || !Number.isSafeInteger(group.connection_ref.revision)
      || Number(group.connection_ref.revision) < 1 || connections.has(group.connection_ref.id) || !Array.isArray(group.item_refs) || !group.item_refs.length) throw Error("Related source binding is invalid.");
    connections.add(group.connection_ref.id);
    const items = group.item_refs.map(item => {
      if (!record(item) || !["gmail", "calendar"].includes(String(item.provider))) throw Error("Related source provider is invalid.");
      const ref = sourceItem(item, item.provider as SyncProvider), key = `${ref.provider}:${ref.opaque_id}`;
      if (refs.has(key) || refs.size >= 10) throw Error("Related sources must contain at most ten unique items.");
      refs.add(key); return ref;
    });
    if (new Set(items.map(item => item.provider)).size !== 1) throw Error("Related connection providers differ.");
    return { connection_ref: { id: group.connection_ref.id, revision: Number(group.connection_ref.revision) }, item_refs: items };
  });
}
export function relatedSources(value: unknown): RelatedSources {
  if (!record(value) || value.classification !== "local_related_context_not_model_input" || value.memory_status !== "no_learning") throw Error("Local related-source receipt is invalid.");
  const groups = connectedSources(value.sources);
  return { classification: "local_related_context_not_model_input", memory_status: "no_learning", sources: groups.map((group, index) => {
    const raw = (value.sources as Record<string, unknown>[])[index];
    const safe = syncProjection({ ...raw, items: group.item_refs, state: "ready", connection_id: group.connection_ref.id, active_job_id: null, cursor_revision: 0, reservation_state: "available", external_effect_state: "none", unresolved_jobs: [] }, group.item_refs[0].provider, group.connection_ref.id);
    return { ...group, coverage: safe.coverage, freshness: safe.freshness };
  }) };
}
export interface SyncSelection {
  goal_ref: { id: string; revision: number }; connection_ref: { id: string; revision: number };
  source_scope: { provider: SyncProvider; consents: { id: string; revision: number }[]; label_ids: string[]; thread_keys: string[] };
  window: { start: string; end: string }; max_items: number;
}
export interface SyncProjection {
  reservation_state?: "held" | "available";
  external_effect_state?: "none" | "unknown" | "settled";
  unresolved_jobs: { job_id: string; revision: number; status: string; external_effect_state: "unknown"; failure_reason: string | null }[];
  connection_id?: string; state?: string; status?: string; active_job_id?: string | null; active_job_revision?: number | null;
  job_id?: string; cursor_revision?: number; scope_digest?: string | null; items: SourceItemRef[];
  coverage: { partial?: boolean; more_available?: boolean; pages_read?: number; returned?: number; max_items?: number; window?: { start: string; end: string } };
  freshness: { last_complete_at?: string | null; expires_at?: string }; recovery_action?: string | null;
  cooldown?: { retry_at?: string; retry_count?: number; original_deadline?: string };
  last_error_code?: string | null;
  selection?: SyncSelection | null;
}
const record = (v: unknown): v is Record<string, unknown> => Boolean(v && typeof v === "object" && !Array.isArray(v));
const text = (v: unknown): v is string => typeof v === "string" && v.length > 0 && v.length <= 256 && !/[\u0000-\u001f]/.test(v);
const timestamp = (v: unknown): v is string => text(v) && Number.isFinite(Date.parse(v));
const sha = (v: unknown) => typeof v === "string" && /^(?:sha256:)?[a-f0-9]{64}$/.test(v);
export function sourceItem(value: unknown, provider: SyncProvider): SourceItemRef {
  if (!record(value) || value.provider !== provider || !text(value.opaque_id) || !sha(value.revision) || !sha(value.content_digest)
    || value.privacy !== "owner_private" || !timestamp(value.expires_at)) throw Error("Source item receipt is invalid; refresh the original connection.");
  return { provider, opaque_id: value.opaque_id, revision: value.revision as string, content_digest: value.content_digest as string, privacy: "owner_private", expires_at: value.expires_at };
}
export function syncProjection(value: unknown, provider: SyncProvider, connectionId: string): SyncProjection {
  if (!record(value) || (value.connection_id !== undefined && value.connection_id !== connectionId)
    || !Array.isArray(value.items) || value.items.length > 50 || !record(value.coverage) || !record(value.freshness)
    || (value.state === undefined && (value.memory_status !== "no_learning" || value.status !== "succeeded" || !text(value.job_id) || typeof value.replayed !== "boolean"))
    || (value.state !== undefined && (value.connection_id !== connectionId || !text(value.state) || !(value.active_job_id === null || text(value.active_job_id))
      || !Number.isSafeInteger(value.cursor_revision) || Number(value.cursor_revision) < 0))) throw Error("Sync receipt is unconfirmed; refresh the original connection before another provider read.");
  const coverage = value.coverage, freshness = value.freshness;
  const unresolved: SyncProjection["unresolved_jobs"] = [];
  if (value.state !== undefined) {
    if (!["held", "available"].includes(String(value.reservation_state)) || !["none", "unknown", "settled"].includes(String(value.external_effect_state))
      || !Array.isArray(value.unresolved_jobs) || (value.reservation_state === "held") !== Boolean(value.active_job_id)) throw Error("Physical reservation or external effect state is unconfirmed; refresh sync state.");
    for (const job of value.unresolved_jobs) {
      if (!record(job) || !text(job.job_id) || !Number.isSafeInteger(job.revision) || Number(job.revision) < 1 || !text(job.status)
        || job.external_effect_state !== "unknown" || !(job.failure_reason === null || text(job.failure_reason))) throw Error("Unresolved source history is invalid; refresh sync state.");
      unresolved.push({ job_id: job.job_id, revision: Number(job.revision), status: job.status, external_effect_state: "unknown", failure_reason: job.failure_reason });
    }
  }
  const selection = value.selection;
  const ref = (v: unknown): v is { id: string; revision: number } => record(v) && text(v.id) && Number.isSafeInteger(v.revision) && Number(v.revision) >= 1;
  const ids = (v: unknown, max: number): v is string[] => Array.isArray(v) && v.length <= max && v.every(text) && new Set(v).size === v.length;
  let recovered: SyncSelection | null = null;
  if (selection != null) {
    if (!record(selection) || !ref(selection.goal_ref) || !ref(selection.connection_ref) || selection.connection_ref.id !== connectionId
      || !record(selection.source_scope) || selection.source_scope.provider !== provider || !Array.isArray(selection.source_scope.consents)
      || !selection.source_scope.consents.length || selection.source_scope.consents.length > 3 || !selection.source_scope.consents.every(ref)
      || !ids(selection.source_scope.label_ids, 3) || !ids(selection.source_scope.thread_keys, 10)
      || !record(selection.window) || !timestamp(selection.window.start) || !timestamp(selection.window.end)
      || Date.parse(selection.window.end) <= Date.parse(selection.window.start) || Date.parse(selection.window.end) - Date.parse(selection.window.start) > 7 * 86400000
      || !Number.isSafeInteger(selection.max_items) || Number(selection.max_items) < 1 || Number(selection.max_items) > 50) throw Error("Original sync scope is unconfirmed; restore the original grant.");
    recovered = { goal_ref: selection.goal_ref, connection_ref: selection.connection_ref,
      source_scope: { provider, consents: selection.source_scope.consents, label_ids: selection.source_scope.label_ids, thread_keys: selection.source_scope.thread_keys },
      window: { start: selection.window.start, end: selection.window.end }, max_items: Number(selection.max_items) };
  }
  if ((coverage.partial !== undefined && typeof coverage.partial !== "boolean") || (coverage.more_available !== undefined && typeof coverage.more_available !== "boolean")
    || ["pages_read", "returned", "max_items"].some(k => coverage[k] !== undefined && (!Number.isSafeInteger(coverage[k]) || Number(coverage[k]) < 0))
    || (freshness.last_complete_at != null && !timestamp(freshness.last_complete_at)) || (freshness.expires_at !== undefined && !timestamp(freshness.expires_at))
    || (value.active_job_revision != null && (!Number.isSafeInteger(value.active_job_revision) || Number(value.active_job_revision) < 1))) throw Error("Sync coverage or recovery metadata is invalid.");
  return { connection_id: connectionId, state: text(value.state) ? value.state : undefined, status: text(value.status) ? value.status : undefined,
    reservation_state: value.reservation_state as SyncProjection["reservation_state"], external_effect_state: value.external_effect_state as SyncProjection["external_effect_state"], unresolved_jobs: unresolved,
    active_job_id: text(value.active_job_id) ? value.active_job_id : null, active_job_revision: typeof value.active_job_revision === "number" ? value.active_job_revision : null,
    job_id: text(value.job_id) ? value.job_id : undefined, cursor_revision: Number.isSafeInteger(value.cursor_revision) ? Number(value.cursor_revision) : undefined,
    scope_digest: sha(value.scope_digest) ? value.scope_digest as string : null,
    items: value.items.map(item => sourceItem(item, provider)),
    coverage: { partial: coverage.partial as boolean | undefined, more_available: coverage.more_available as boolean | undefined,
      pages_read: coverage.pages_read as number | undefined, returned: coverage.returned as number | undefined, max_items: coverage.max_items as number | undefined,
      window: record(coverage.window) && timestamp(coverage.window.start) && timestamp(coverage.window.end) ? { start: coverage.window.start, end: coverage.window.end } : undefined },
    freshness: { last_complete_at: freshness.last_complete_at as string | null | undefined, expires_at: freshness.expires_at as string | undefined },
    recovery_action: text(value.recovery_action) ? value.recovery_action : null,
    last_error_code: text(value.last_error_code) ? value.last_error_code : null,
    selection: recovered,
    cooldown: record(value.cooldown) ? { retry_at: timestamp(value.cooldown.retry_at) ? value.cooldown.retry_at : undefined,
      retry_count: Number.isSafeInteger(value.cooldown.retry_count) ? Number(value.cooldown.retry_count) : undefined,
      original_deadline: timestamp(value.cooldown.original_deadline) ? value.cooldown.original_deadline : undefined } : undefined };
}
export const syncPath = (provider: SyncProvider, connectionId: string) => `${provider === "gmail" ? "/api/capabilities/mail" : "/api/calendar"}/connections/${encodeURIComponent(connectionId)}/sync`;
export async function connectionSyncRequest(path: string, body?: unknown): Promise<unknown> {
  return withMailDeadline(async signal => {
    const response = await apiFetch(`${API_URL}${path}`, { signal, ...(body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }) });
    const value: unknown = await response.json();
    if (!response.ok) {
      const detail = record(value) && record(value.detail) ? value.detail : {};
      throw Error(`${text(detail.code) ? detail.code : `source_sync_blocked_${response.status}`}${text(detail.recovery_action) ? ` · recovery: ${detail.recovery_action}` : ""}. Refresh current connection and consent; inspect the existing sync before starting new work.`);
    }
    return value;
  });
}
export function privateSyncItem(value: unknown, expected: SourceItemRef): { ref: SourceItemRef; content: Record<string, unknown> } {
  if (!record(value) || value.memory_status !== "no_learning" || !record(value.item) || !record(value.item.content)) throw Error("Private readback is unconfirmed.");
  const ref = sourceItem(value.item.ref, expected.provider);
  if (ref.opaque_id !== expected.opaque_id || ref.revision !== expected.revision || ref.content_digest !== expected.content_digest || ref.expires_at !== expected.expires_at) throw Error("Private source revision changed; refresh before reading again.");
  // Only source-owner fields enter private rendering; no raw IDs/credentials/cursors.
  const content: Record<string, unknown> = {};
  for (const key of ["status", "subject", "preview", "read_status", "received_at", "body", "summary", "start", "end", "location", "description", "attendees"]) {
    const field = value.item.content[key];
    if (typeof field === "string" && field.length <= 262144 || field === null || Array.isArray(field) && field.length <= 100 && field.every(x => typeof x === "string" && x.length <= 1024)) content[key] = field;
  }
  return { ref, content };
}
