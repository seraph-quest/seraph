import { API_URL } from "../config/constants";
import { apiFetch } from "./api";

// Read-only client for stored end-of-day goal reports. These helpers only GET
// already-stored reports; nothing here can start report generation or inference.

export type ReportStatus = "ready" | "blocked";

export interface ReportListItem {
  episode_id: string;
  date: string | null;
  timezone: string | null;
  created_at: string | null;
  observed_at: string | null;
  status: ReportStatus;
  blocked_reason: string | null;
  analysis_provider: string | null;
  active_goal_count: number;
  completed_goal_count: number;
  total_tracked_minutes: number;
}

export interface ReportSummary {
  source: "archive" | "episode_metadata";
  total_observations: number;
  total_tracked_minutes: number;
  switch_count: number | null;
  by_activity: Array<[string, number]>;
  by_project: Array<[string, number]>;
  by_source: Array<[string, number]>;
}

export interface ReportDetail extends ReportListItem {
  body: string;
  summary: ReportSummary;
  artifacts: { report_text_sha256: string | null; report_json_sha256: string | null };
}

const MAX_RESPONSE_BYTES = 512 * 1024;
const MAX_ITEMS = 100;
const MAX_MIX = 25;

const recordOf = (value: unknown): Record<string, unknown> | null =>
  value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : null;
const episodeId = (value: unknown): value is string => typeof value === "string" && /^[A-Za-z0-9_.:-]{1,128}$/.test(value);
const count = (value: unknown): value is number => typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
const optionalText = (value: unknown, max = 256): value is string | null =>
  value === null || (typeof value === "string" && value.length <= max);
const sha = (value: unknown): string | null => (typeof value === "string" && /^[0-9a-f]{64}$/.test(value) ? value : null);

export function normalizeReportListItem(value: unknown): ReportListItem | null {
  const row = recordOf(value);
  if (
    !row ||
    !episodeId(row.episode_id) ||
    (row.status !== "ready" && row.status !== "blocked") ||
    !optionalText(row.date, 32) ||
    !optionalText(row.timezone, 64) ||
    !optionalText(row.created_at, 64) ||
    !optionalText(row.observed_at, 64) ||
    !optionalText(row.blocked_reason) ||
    !optionalText(row.analysis_provider) ||
    !count(row.active_goal_count) ||
    !count(row.completed_goal_count) ||
    !count(row.total_tracked_minutes)
  ) {
    return null;
  }
  // A blocked report must carry its reason and a ready one must not; anything
  // else is an inconsistent projection and is not shown as a report.
  if ((row.status === "blocked") !== (typeof row.blocked_reason === "string")) return null;
  return {
    episode_id: row.episode_id,
    date: row.date as string | null,
    timezone: row.timezone as string | null,
    created_at: row.created_at as string | null,
    observed_at: row.observed_at as string | null,
    status: row.status,
    blocked_reason: row.blocked_reason as string | null,
    analysis_provider: row.analysis_provider as string | null,
    active_goal_count: row.active_goal_count,
    completed_goal_count: row.completed_goal_count,
    total_tracked_minutes: row.total_tracked_minutes,
  };
}

function normalizeMix(value: unknown): Array<[string, number]> | null {
  const row = recordOf(value);
  if (!row) return null;
  const entries = Object.entries(row);
  if (entries.length > MAX_MIX) return null;
  const result: Array<[string, number]> = [];
  for (const [key, seconds] of entries) {
    if (!key || key.length > 120 || !count(seconds)) return null;
    result.push([key, seconds]);
  }
  return result.sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

function normalizeSummary(value: unknown): ReportSummary | null {
  const row = recordOf(value);
  if (!row || (row.source !== "archive" && row.source !== "episode_metadata")) return null;
  const byActivity = normalizeMix(row.by_activity);
  const byProject = normalizeMix(row.by_project);
  const bySource = normalizeMix(row.by_source);
  if (
    !count(row.total_observations) ||
    !count(row.total_tracked_minutes) ||
    !(row.switch_count === null || count(row.switch_count)) ||
    !byActivity ||
    !byProject ||
    !bySource
  ) {
    return null;
  }
  return {
    source: row.source,
    total_observations: row.total_observations,
    total_tracked_minutes: row.total_tracked_minutes,
    switch_count: row.switch_count as number | null,
    by_activity: byActivity,
    by_project: byProject,
    by_source: bySource,
  };
}

export function normalizeReportDetail(value: unknown): ReportDetail | null {
  const item = normalizeReportListItem(value);
  const row = recordOf(value);
  if (!item || !row || typeof row.body !== "string") return null;
  const summary = normalizeSummary(row.summary);
  const artifacts = recordOf(row.artifacts) ?? {};
  if (!summary) return null;
  // Whitelist: never carry anything path-like the server might accidentally return.
  return {
    ...item,
    body: row.body,
    summary,
    artifacts: { report_text_sha256: sha(artifacts.report_text_sha256), report_json_sha256: sha(artifacts.report_json_sha256) },
  };
}

async function readJson(path: string, signal?: AbortSignal): Promise<unknown> {
  const response = await apiFetch(API_URL + path, { signal });
  if (!response.ok) {
    const error = new Error(`Report request failed (HTTP ${response.status}).`) as Error & { status?: number };
    error.status = response.status;
    throw error;
  }
  const text = await response.text();
  if (new TextEncoder().encode(text).byteLength > MAX_RESPONSE_BYTES) throw new Error("The report response exceeded its safe read limit.");
  return JSON.parse(text) as unknown;
}

export async function listEndOfDayReports(limit = 30, signal?: AbortSignal): Promise<ReportListItem[]> {
  const bounded = Math.max(1, Math.min(MAX_ITEMS, Math.trunc(limit)));
  const payload = recordOf(await readJson(`/api/reports/end-of-day?limit=${bounded}`, signal));
  if (!payload || !Array.isArray(payload.items) || payload.items.length > MAX_ITEMS) {
    throw new Error("Stored reports could not be read.");
  }
  const items = payload.items.map(normalizeReportListItem);
  if (items.some((item) => item === null)) throw new Error("Stored reports could not be read.");
  return items as ReportListItem[];
}

export async function readEndOfDayReport(id: string, signal?: AbortSignal): Promise<ReportDetail> {
  if (!episodeId(id)) throw new Error("Unknown report.");
  const detail = normalizeReportDetail(await readJson(`/api/reports/end-of-day/${encodeURIComponent(id)}`, signal));
  if (!detail || detail.episode_id !== id) throw new Error("The stored report could not be read.");
  return detail;
}
