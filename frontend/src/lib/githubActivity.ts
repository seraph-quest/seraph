// GitHub activity source (#1264): strict normalisers for the settings panel.
// The backend owns consent and every gh start; this module only projects state.

export const GITHUB_ACTIVITY_GRANT_SENTENCE =
  "Uses the operator's local gh login (full gh scope; Seraph only issues read GETs).";

const GITHUB_URL = /^https:\/\/github\.com\/[\w.-]+\/[\w.-]+\/(commit\/[0-9a-f]{40}|pull\/\d+|issues\/\d+)$/;

export interface GitHubActivityScope {
  discover_from_activity: boolean;
  repositories: string[];
  owners: string[];
  exclude_repositories: string[];
  kinds: string[];
}

export interface GitHubActivitySource {
  id: string | null;
  state: string;
  revision: number;
  stopped_reason: string | null;
  identity_login: string | null;
  gh_entry_path: string | null;
  scope: GitHubActivityScope | null;
  model_context_allowed: boolean;
}

export interface GitHubActivityRun {
  id: string;
  status: string;
  reason_code: string | null;
  message: string;
  report_date: string;
  call_count: number;
  finished_at: string | null;
}

// Another ownership identity's source (#1270): neutral, with no login, gh path or scope.
export interface GitHubActivityOtherSource {
  id: string;
  state: string;
  revision: number;
  stoppable: boolean;
  message: string;
}

export interface GitHubActivityStatus {
  source: GitHubActivitySource;
  gh: { available: boolean; reason_code: string | null; entry_path: string | null };
  grant_sentence: string;
  endpoints: string[];
  message: string;
  last_run: GitHubActivityRun | null;
  // The source belongs to the operator identity; an unenrolled login has none.
  ownership_required: boolean;
  other_identity_sources: GitHubActivityOtherSource[];
}

export interface GhLoginCheck {
  gh_version: string;
  gh_entry_path: string;
  identity_id: number;
  identity_login: string;
}

export interface GitHubActivityItem {
  id: string;
  repository: string;
  kind: string;
  number: number | null;
  title: string;
  url: string | null;
  occurred_at: string;
}

function record(value: unknown, what: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`Invalid ${what}`);
  return value as Record<string, unknown>;
}

const text = (value: unknown) => (typeof value === "string" ? value : null);
const strings = (value: unknown) => (Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : []);

function scope(value: unknown): GitHubActivityScope | null {
  if (value === undefined || value === null) return null;
  const row = record(value, "GitHub activity scope");
  return { discover_from_activity: row.discover_from_activity === true, repositories: strings(row.repositories),
    owners: strings(row.owners), exclude_repositories: strings(row.exclude_repositories), kinds: strings(row.kinds) };
}

function otherSources(value: unknown): GitHubActivityOtherSource[] {
  if (!Array.isArray(value)) return [];
  return value.map(item => record(item, "GitHub activity source of another identity"))
    .filter(item => typeof item.id === "string" && typeof item.state === "string"
      && typeof item.revision === "number" && Number.isSafeInteger(item.revision))
    .map(item => ({ id: item.id as string, state: item.state as string, revision: item.revision as number,
      stoppable: item.state === "active" && strings(item.controls).includes("stop"), message: text(item.message) ?? "" }));
}

function run(value: unknown): GitHubActivityRun | null {
  if (value === undefined || value === null) return null;
  const row = record(value, "GitHub activity run");
  if (typeof row.id !== "string" || typeof row.status !== "string" || typeof row.message !== "string") {
    throw new Error("Invalid GitHub activity run");
  }
  return { id: row.id, status: row.status, reason_code: text(row.reason_code), message: row.message,
    report_date: text(row.report_date) ?? "", call_count: typeof row.call_count === "number" ? row.call_count : 0,
    finished_at: text(row.finished_at) };
}

export function githubActivityStatus(value: unknown): GitHubActivityStatus {
  const row = record(value, "GitHub activity status");
  const source = record(row.source, "GitHub activity source");
  const gh = record(row.gh, "GitHub activity gh presence");
  if (typeof source.state !== "string" || typeof source.revision !== "number" || !Number.isSafeInteger(source.revision)) {
    throw new Error("Invalid GitHub activity source");
  }
  return {
    source: { id: text(source.id), state: source.state, revision: source.revision,
      stopped_reason: text(source.stopped_reason), identity_login: text(source.identity_login),
      gh_entry_path: text(source.gh_entry_path), scope: scope(source.scope),
      model_context_allowed: source.model_context_allowed === true },
    gh: { available: gh.available === true, reason_code: text(gh.reason_code), entry_path: text(gh.entry_path) },
    // The fixed sentence is shown whatever the server sends, so it cannot be dropped.
    grant_sentence: GITHUB_ACTIVITY_GRANT_SENTENCE,
    endpoints: strings(row.endpoints).filter(item => item.startsWith("GET /")),
    message: text(row.message) ?? "",
    last_run: run(row.last_run),
    ownership_required: row.ownership_required === true,
    other_identity_sources: otherSources(row.other_identity_sources),
  };
}

export function ghLoginCheck(value: unknown): GhLoginCheck {
  const row = record(value, "gh login check");
  if (typeof row.gh_version !== "string" || typeof row.gh_entry_path !== "string" || typeof row.identity_login !== "string"
    || typeof row.identity_id !== "number" || !Number.isSafeInteger(row.identity_id) || row.identity_id < 1) {
    throw new Error("Invalid gh login check");
  }
  return { gh_version: row.gh_version, gh_entry_path: row.gh_entry_path, identity_id: row.identity_id,
    identity_login: row.identity_login };
}

export function githubActivityItems(value: unknown): { message: string; items: GitHubActivityItem[] } {
  const row = record(value, "GitHub activity items");
  const items = Array.isArray(row.items) ? row.items : [];
  return {
    message: text(row.message) ?? "",
    items: items.map(item => record(item, "GitHub activity item")).filter(item => typeof item.id === "string"
      && typeof item.repository === "string" && typeof item.kind === "string" && typeof item.occurred_at === "string")
      .map(item => ({ id: item.id as string, repository: item.repository as string, kind: item.kind as string,
        number: typeof item.number === "number" ? item.number : null, title: text(item.title) ?? "",
        // Only a GitHub URL of a known shape is ever rendered as a link.
        url: typeof item.url === "string" && GITHUB_URL.test(item.url) ? item.url : null,
        occurred_at: item.occurred_at as string })),
  };
}

export function parseRepositoryList(value: string): string[] {
  return value.split(/[\s,]+/).map(item => item.trim()).filter(Boolean);
}

// The backend's one repository rule (github_cli.valid_repository), mirrored for early feedback.
const REPOSITORY = /^[A-Za-z0-9_.-]{1,100}\/[A-Za-z0-9_.-]{1,100}$/;
export function validRepository(value: string): boolean {
  if (!REPOSITORY.test(value) || value.includes("..")) return false;
  const [owner, name] = value.split("/");
  return !(owner.startsWith("-") || owner.startsWith(".") || name.startsWith("-") || name === ".");
}

const LOGIN = /^[A-Za-z0-9_-]{1,39}$/;
export function validOwner(value: string): boolean {
  return LOGIN.test(value) && !value.startsWith("-") && !value.startsWith("_");
}
