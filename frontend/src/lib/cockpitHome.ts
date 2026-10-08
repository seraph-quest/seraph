import { API_URL } from "../config/constants";
import { apiFetch } from "./api";
import { fetchGuardianInbox } from "./guardianInbox";
import type { GuardianInboxItem } from "../types";

export type HomeResourceState = "idle" | "loading" | "refreshing" | "ready" | "empty" | "stale" | "degraded" | "forbidden" | "offline";

export interface CockpitHomeSnapshot {
  goals: { active_count: number; completed_count: number; total_count: number; domains: Record<string, unknown> } | null;
  work: { tasks: Array<Record<string, unknown>>; next_after: string | null; last_event_id?: number | null } | null;
  approvals: Array<Record<string, unknown>>;
  inbox: { items: GuardianInboxItem[]; next_cursor: string | null; last_confirmed_at: string | null } | null;
  continuity: Record<string, unknown> | null;
  runtime: Record<string, unknown> | null;
  last_confirmed_at: string | null;
}

export interface CockpitHomeLoadResult {
  snapshot: CockpitHomeSnapshot;
  resources: Record<"goals" | "work" | "approvals" | "inbox" | "continuity" | "runtime", HomeResourceState>;
  error: string | null;
}

const emptySnapshot: CockpitHomeSnapshot = {
  goals: null,
  work: null,
  approvals: [],
  inbox: null,
  continuity: null,
  runtime: null,
  last_confirmed_at: null,
};

async function readJson(path: string, signal: AbortSignal): Promise<unknown> {
  const response = await apiFetch(`${API_URL}${path}`, { signal });
  if (signal.aborted) throw new DOMException("Home request was cancelled.", "AbortError");
  const body = await response.json().catch(() => null);
  if (signal.aborted) throw new DOMException("Home request was cancelled.", "AbortError");
  if (!response.ok) {
    const detail = body && typeof body === "object" && body !== null && "detail" in body
      ? (body as { detail?: unknown }).detail
      : null;
    const code = detail && typeof detail === "object" && detail !== null && "code" in detail
      ? String((detail as { code?: unknown }).code)
      : `HTTP ${response.status}`;
    const error = new Error(code);
    (error as Error & { status?: number }).status = response.status;
    throw error;
  }
  return body;
}

function object(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function array(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value) ? value.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === "object" && !Array.isArray(item))) : [];
}

function resourceState(error: unknown): HomeResourceState {
  const status = error && typeof error === "object" && "status" in error ? Number((error as { status?: unknown }).status) : 0;
  if (status === 401 || status === 403) return "forbidden";
  if (status === 0) return "offline";
  return "degraded";
}

export async function fetchCockpitHomeSnapshot(
  signal: AbortSignal,
  previous: CockpitHomeSnapshot = emptySnapshot,
): Promise<CockpitHomeLoadResult> {
  const resources = {
    goals: "loading",
    work: "loading",
    approvals: "loading",
    inbox: "loading",
    continuity: "loading",
    runtime: "loading",
  } as CockpitHomeLoadResult["resources"];
  const settled = await Promise.allSettled([
    readJson("/api/goals/dashboard", signal),
    readJson("/api/work-board/tasks?limit=20", signal),
    readJson("/api/approvals/pending?limit=10", signal),
    fetchGuardianInbox({ limit: 20, signal }),
    readJson("/api/observer/continuity", signal),
    readJson("/api/runtime/status", signal),
  ]);
  const next: CockpitHomeSnapshot = { ...previous };
  const errors: string[] = [];
  const assign = <T>(index: number, key: keyof CockpitHomeSnapshot, transform: (value: T) => unknown) => {
    const result = settled[index];
    if (result.status === "fulfilled") {
      (next as unknown as Record<string, unknown>)[key] = transform(result.value as T);
      resources[key as keyof typeof resources] = "ready";
    } else {
      resources[key as keyof typeof resources] = resourceState(result.reason);
      errors.push(`${key}: ${result.reason instanceof Error ? result.reason.message : "unavailable"}`);
    }
  };
  assign(0, "goals", (value) => {
    const data = object(value);
    return {
      active_count: typeof data.active_count === "number" ? data.active_count : 0,
      completed_count: typeof data.completed_count === "number" ? data.completed_count : 0,
      total_count: typeof data.total_count === "number" ? data.total_count : 0,
      domains: object(data.domains),
    };
  });
  assign(1, "work", (value) => {
    const data = object(value);
    return {
      tasks: array(data.tasks),
      next_after: typeof data.next_after === "string" ? data.next_after : null,
      last_event_id: typeof data.last_event_id === "number" ? data.last_event_id : null,
    };
  });
  assign(2, "approvals", (value) => {
    const data = object(value);
    return Array.isArray(data.approvals) ? array(data.approvals) : array(value);
  });
  assign(3, "inbox", (value) => value);
  assign(4, "continuity", (value) => object(value));
  assign(5, "runtime", (value) => object(value));
  // A failed refresh must never look like a fresh confirmation. A partial
  // result remains visibly degraded and keeps the prior confirmation time.
  if (errors.length === 0) next.last_confirmed_at = new Date().toISOString();
  return { snapshot: next, resources, error: errors.length ? errors.join(" · ") : null };
}

export const emptyCockpitHomeSnapshot = emptySnapshot;
