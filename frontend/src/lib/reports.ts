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
  github_activity_status: GitHubEvidenceStatus | null;
  github_activity_item_count: number;
}

// GitHub evidence in a report (#1264). Links come only from these items, and
// only for a GitHub commit, pull request or issue URL.
export type GitHubEvidenceStatus =
  | "not_connected"
  | "complete"
  | "partial"
  | "failed"
  | "blocked"
  | "revoked"
  | "stopped"
  | "not_collected"
  | "running"
  | "unavailable";

export interface GitHubEvidenceItem {
  ref: string;
  repository: string;
  kind: string;
  number: number | null;
  short_sha: string | null;
  title: string;
  url: string | null;
  occurred_at: string | null;
  same_title_count: number;
}

export interface GitHubEvidence {
  source: "archive" | "episode_metadata";
  status: GitHubEvidenceStatus;
  message: string | null;
  item_count: number;
  items_available: boolean;
  identity_login: string | null;
  model_context: "included" | "not_allowed" | "not_sent" | null;
  counts: Array<[string, number]>;
  repositories: string[];
  limitations: Array<{ code: string; repository?: string }>;
  items: GitHubEvidenceItem[];
}

export interface GoalEvidenceRow {
  ref: string;
  title: string;
  level: string;
  domain: string;
  parent_ref: string | null;
  github: string[];
  screen: boolean;
  rolled_up_from: string[];
}

export interface GoalEvidence {
  status: "mapped" | "unparsed" | "not_requested" | "blocked";
  goals: GoalEvidenceRow[];
  goals_without_evidence: string[];
  github_without_goal: string[];
  rejected: { unknown_evidence: number; unknown_goals: number; urls_removed: number };
}

export const GITHUB_GRANT_SENTENCE = "Uses the operator's local gh login (full gh scope; Seraph only issues read GETs).";

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
  github_activity: GitHubEvidence | null;
  goal_evidence: GoalEvidence | null;
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
const GITHUB_STATUSES: readonly GitHubEvidenceStatus[] = [
  "not_connected",
  "complete",
  "partial",
  "failed",
  "blocked",
  "revoked",
  "stopped",
  "not_collected",
  "running",
  "unavailable",
];
const GITHUB_KINDS = ["commit", "pr_opened", "pr_merged", "pr_closed", "issue_opened", "issue_closed"];
export const GITHUB_URL = /^https:\/\/github\.com\/[\w.-]+\/[\w.-]+\/(commit\/[0-9a-f]{40}|pull\/\d+|issues\/\d+)(#[\w-]+)?$/;
const REPOSITORY = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;
const G_REF = /^G\d{1,4}$/;
const A_REF = /^A\d{1,4}$/;
const githubStatus = (value: unknown): GitHubEvidenceStatus | null =>
  typeof value === "string" && (GITHUB_STATUSES as readonly string[]).includes(value) ? (value as GitHubEvidenceStatus) : null;
const boundedText = (value: unknown, max: number): string | null => (typeof value === "string" && value.length <= max ? value : null);
const refList = (value: unknown, pattern: RegExp): string[] =>
  Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && pattern.test(item)).slice(0, 200) : [];

function normalizeGitHubItem(value: unknown): GitHubEvidenceItem | null {
  const row = recordOf(value);
  if (
    !row ||
    typeof row.ref !== "string" ||
    !G_REF.test(row.ref) ||
    typeof row.repository !== "string" ||
    !REPOSITORY.test(row.repository) ||
    typeof row.kind !== "string" ||
    !GITHUB_KINDS.includes(row.kind)
  ) {
    return null;
  }
  return {
    ref: row.ref,
    repository: row.repository,
    kind: row.kind,
    number: count(row.number) && row.number > 0 ? row.number : null,
    short_sha: typeof row.short_sha === "string" && /^[0-9a-f]{7}$/.test(row.short_sha) ? row.short_sha : null,
    title: boundedText(row.title, 300) ?? "",
    // The same check as the server: anything but a GitHub commit/PR/issue URL is not a link.
    url: typeof row.url === "string" && GITHUB_URL.test(row.url) ? row.url : null,
    occurred_at: boundedText(row.occurred_at, 64),
    same_title_count: count(row.same_title_count) && row.same_title_count > 0 ? row.same_title_count : 1,
  };
}

export function normalizeGitHubEvidence(value: unknown): GitHubEvidence | null {
  const row = recordOf(value);
  const status = row ? githubStatus(row.status) : null;
  if (!row || !status || (row.source !== "archive" && row.source !== "episode_metadata")) return null;
  const counts = recordOf(row.counts) ?? {};
  const items = Array.isArray(row.items) ? row.items.slice(0, 200).map(normalizeGitHubItem) : [];
  const limitations = Array.isArray(row.limitations) ? row.limitations : [];
  return {
    source: row.source,
    status,
    message: boundedText(row.message, 1000),
    item_count: count(row.item_count) ? row.item_count : 0,
    items_available: row.items_available === true,
    identity_login: typeof row.identity_login === "string" && /^[A-Za-z0-9-]{1,39}$/.test(row.identity_login) ? row.identity_login : null,
    model_context:
      row.model_context === "included" || row.model_context === "not_allowed" || row.model_context === "not_sent" ? row.model_context : null,
    counts: Object.entries(counts)
      .filter((entry): entry is [string, number] => GITHUB_KINDS.includes(entry[0]) && count(entry[1]))
      .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])),
    repositories: Array.isArray(row.repositories)
      ? row.repositories.filter((repo): repo is string => typeof repo === "string" && REPOSITORY.test(repo)).slice(0, 20)
      : [],
    limitations: limitations
      .map(recordOf)
      .filter((item): item is Record<string, unknown> => !!item && typeof item.code === "string" && /^[a-z0-9_]{1,64}$/.test(item.code))
      .map((item) =>
        typeof item.repository === "string" && REPOSITORY.test(item.repository)
          ? { code: item.code as string, repository: item.repository }
          : { code: item.code as string },
      )
      .slice(0, 50),
    items: items.filter((item): item is GitHubEvidenceItem => item !== null),
  };
}

export function normalizeGoalEvidence(value: unknown): GoalEvidence | null {
  const row = recordOf(value);
  if (!row || !["mapped", "unparsed", "not_requested", "blocked"].includes(String(row.status))) return null;
  const rejected = recordOf(row.rejected) ?? {};
  const goals = (Array.isArray(row.goals) ? row.goals.slice(0, 24) : [])
    .map(recordOf)
    .filter((goal): goal is Record<string, unknown> => !!goal && typeof goal.ref === "string" && A_REF.test(goal.ref))
    .map((goal) => ({
      ref: goal.ref as string,
      title: boundedText(goal.title, 120) ?? "",
      level: boundedText(goal.level, 32) ?? "",
      domain: boundedText(goal.domain, 32) ?? "",
      parent_ref: typeof goal.parent_ref === "string" && A_REF.test(goal.parent_ref) ? goal.parent_ref : null,
      github: refList(goal.github, G_REF),
      screen: goal.screen === true,
      rolled_up_from: refList(goal.rolled_up_from, A_REF),
    }));
  const number = (key: string) => (count(rejected[key]) ? (rejected[key] as number) : 0);
  return {
    status: row.status as GoalEvidence["status"],
    goals,
    goals_without_evidence: refList(row.goals_without_evidence, A_REF),
    github_without_goal: refList(row.github_without_goal, G_REF),
    rejected: { unknown_evidence: number("unknown_evidence"), unknown_goals: number("unknown_goals"), urls_removed: number("urls_removed") },
  };
}

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
    // Older servers and reports carry no GitHub evidence fields.
    github_activity_status: githubStatus(row.github_activity_status),
    github_activity_item_count: count(row.github_activity_item_count) ? row.github_activity_item_count : 0,
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
    // A malformed evidence block is not shown; the report itself still is.
    github_activity: normalizeGitHubEvidence(row.github_activity),
    goal_evidence: normalizeGoalEvidence(row.goal_evidence),
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
