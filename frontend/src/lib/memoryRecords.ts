import { API_URL } from "../config/constants";
import { apiFetch } from "./api";
import type {
  CanonicalMemoryKind,
  CanonicalMemoryLink,
  CanonicalMemoryPage,
  CanonicalMemoryProvenance,
  CanonicalMemoryRecord,
  CanonicalMemoryStatus,
} from "../types";

export const MEMORY_KINDS: CanonicalMemoryKind[] = [
  "fact", "preference", "pattern", "goal", "reflection", "project",
  "collaborator", "obligation", "routine", "timeline", "commitment",
  "communication_preference", "procedural",
];
export const MEMORY_STATUSES: CanonicalMemoryStatus[] = ["active", "archived", "superseded"];

export class MemoryRecordsApiError extends Error {
  readonly status: number;
  readonly code: string | null;
  readonly payload: unknown;

  constructor(status: number, message: string, code: string | null = null, payload: unknown = null) {
    super(message);
    this.name = "MemoryRecordsApiError";
    this.status = status;
    this.code = code;
    this.payload = payload;
  }
}

function record(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}

function text(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

function numberValue(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function normalizeLinks(value: unknown): CanonicalMemoryLink[] {
  if (Array.isArray(value)) {
    return value.flatMap((entry): CanonicalMemoryLink[] => {
      if (typeof entry === "string") return [{ kind: "reference", label: "Reference", href: entry }];
      if (!record(entry) || typeof entry.kind !== "string") return [];
      return [{
        kind: entry.kind,
        label: text(entry.label),
        href: text(entry.href),
        id: text(entry.id),
      }];
    });
  }
  // The current backend projection uses a compact kind -> opaque reference
  // map for metadata links. Normalize it into the shared typed link shape;
  // safeMemoryHref still decides which values may become anchors.
  if (record(value)) {
    return Object.entries(value).flatMap(([kind, reference]): CanonicalMemoryLink[] => {
      if (typeof reference !== "string") return [];
      const isPath = reference.startsWith("/");
      return [{
        kind,
        label: kind.replace(/_/g, " "),
        href: isPath ? reference : null,
        id: isPath ? null : reference,
      }];
    });
  }
  return [];
}

function normalizeRecord(value: unknown, detail = false): CanonicalMemoryRecord | null {
  if (!record(value) || typeof value.id !== "string" || !value.id.trim()) return null;
  if (!MEMORY_KINDS.includes(value.kind as CanonicalMemoryKind)) return null;
  if (!MEMORY_STATUSES.includes(value.status as CanonicalMemoryStatus)) return null;
  const owner = text(value.source_session_id);
  if (!owner) return null;
  const provenance = record(value.safe_provenance) ? value.safe_provenance as CanonicalMemoryProvenance : {};
  const tombstoneState = text(value.tombstone_state);
  const conflict = record(value.conflict)
    ? value.conflict
    : record(value.conflict_state) ? value.conflict_state : null;
  return {
    id: value.id,
    ...(value.ownership_access === "recovered_read_only" ? { ownership_access: "recovered_read_only" as const, execution_block_reason: text(value.execution_block_reason) ?? undefined } : {}),
    kind: value.kind as CanonicalMemoryKind,
    status: value.status as CanonicalMemoryStatus,
    summary: text(value.summary),
    confidence: numberValue(value.confidence),
    created_at: text(value.created_at) ?? "",
    updated_at: text(value.updated_at) ?? "",
    last_confirmed_at: text(value.last_confirmed_at),
    source_session_id: owner,
    safe_provenance: provenance,
    links: normalizeLinks(value.links),
    ...(detail ? {
      content: text(value.content),
      current_source: text(value.current_source) ?? text(value.source_state),
      conflict,
      tombstone: record(value.tombstone)
        ? value.tombstone
        : tombstoneState && tombstoneState !== "none" ? { state: tombstoneState } : null,
      audit_links: normalizeLinks(value.audit_links),
      privacy_boundary: text(value.privacy_boundary) ?? text(provenance.privacy_boundary),
      redaction_state: text(value.redaction_state),
      sources: Array.isArray(value.sources)
        ? value.sources.filter((entry): entry is Record<string, unknown> => record(entry))
        : [],
      source_state: record(value.source_state) ? value.source_state : null,
      conflict_state: record(value.conflict_state) ? value.conflict_state : null,
      tombstone_state: tombstoneState,
    } : {}),
  };
}

async function payload(response: Response): Promise<unknown> {
  try { return await response.json(); } catch { return null; }
}

function throwApiError(response: Response, body: unknown, fallback: string): never {
  const detail = record(body) && record(body.detail) ? body.detail : record(body) ? body : null;
  const code = detail && typeof detail.code === "string" ? detail.code : null;
  const message = detail && typeof detail.reason === "string"
    ? detail.reason
    : detail && typeof detail.message === "string" ? detail.message : `${fallback} (HTTP ${response.status}).`;
  throw new MemoryRecordsApiError(response.status, message, code, body);
}

export async function fetchMemoryRecords(options: {
  limit?: number;
  cursor?: string | null;
  query?: string;
  kind?: CanonicalMemoryKind | "";
  status?: CanonicalMemoryStatus | "";
  signal?: AbortSignal;
} = {}): Promise<CanonicalMemoryPage> {
  const params = new URLSearchParams();
  params.set("limit", String(Math.min(50, Math.max(1, Math.floor(options.limit ?? 20)))));
  if (options.cursor) params.set("cursor", options.cursor);
  if (options.query?.trim()) params.set("q", options.query.trim().slice(0, 200));
  if (options.kind) params.set("kind", options.kind);
  if (options.status) params.set("status", options.status);
  const response = await apiFetch(`${API_URL}/api/memory/records?${params.toString()}`, { signal: options.signal });
  const body = await payload(response);
  if (!response.ok) throwApiError(response, body, "Canonical memory records could not be loaded");
  const data = record(body) ? body : {};
  const raw = Array.isArray(data.records) ? data.records : [];
  return {
    records: raw.flatMap((entry) => {
      const normalized = normalizeRecord(entry);
      return normalized ? [normalized] : [];
    }),
    next_cursor: text(data.next_cursor),
    last_confirmed_at: text(data.last_confirmed_at),
  };
}

export async function fetchMemoryRecord(id: string, signal?: AbortSignal): Promise<CanonicalMemoryRecord> {
  const response = await apiFetch(`${API_URL}/api/memory/records/${encodeURIComponent(id)}`, { signal });
  if (signal?.aborted) throw new DOMException("Memory detail request was cancelled.", "AbortError");
  const body = await payload(response);
  if (signal?.aborted) throw new DOMException("Memory detail request was cancelled.", "AbortError");
  if (!response.ok) throwApiError(response, body, "Canonical memory record could not be loaded");
  const data = record(body) && record(body.record) ? body.record : body;
  const normalized = normalizeRecord(data, true);
  if (!normalized) throw new MemoryRecordsApiError(502, "Canonical memory returned an incomplete record.", "invalid_memory_record", body);
  return normalized;
}

export async function postMemoryControl(
  id: string,
  action: "pin" | "forget" | "audit",
  body: { reason: string; privacy_boundary?: string; mode?: "archive" | "redact" },
): Promise<unknown> {
  const response = await apiFetch(`${API_URL}/api/memory/${encodeURIComponent(id)}/${action}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const result = await payload(response);
  if (!response.ok) throwApiError(response, result, `Memory ${action} could not be completed`);
  return result;
}

export async function postMemoryDeleteExport(body: {
  memory_id: string;
  reason: string;
  privacy_boundary?: string;
}): Promise<unknown> {
  const response = await apiFetch(`${API_URL}/api/memory/live-controls/actions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      action: "propagate_delete_export",
      acknowledged: true,
      memory_id: body.memory_id,
      reason: body.reason,
      privacy_boundary: body.privacy_boundary,
    }),
  });
  const result = await payload(response);
  if (!response.ok) throwApiError(response, result, "Memory delete/export could not be completed");
  return result;
}

export async function postMemoryCorrection(body: {
  content: string;
  kind: CanonicalMemoryKind;
  summary?: string;
  corrects_memory_id: string;
  reason: string;
  confidence?: number;
  importance?: number;
  privacy_boundary?: string;
}): Promise<unknown> {
  const response = await apiFetch(`${API_URL}/api/memory/corrections`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const result = await payload(response);
  if (!response.ok) throwApiError(response, result, "Memory correction could not be completed");
  return result;
}

export function safeMemoryHref(value: string | null | undefined): string | null {
  if (!value || value.startsWith("//")) return null;
  try {
    const parsed = new URL(value, window.location.origin);
    if (parsed.origin !== window.location.origin) return null;
    const allowed = ["/api/memory/records/", "/api/memory/audit", "/api/work-board/tasks/", "/cockpit"];
    if (!allowed.some((prefix) => parsed.pathname.startsWith(prefix))) return null;
    return `${parsed.pathname}${parsed.search}${parsed.hash}`;
  } catch {
    return null;
  }
}
