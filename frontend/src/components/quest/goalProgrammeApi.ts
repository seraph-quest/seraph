import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";

export interface ProgrammeRequest {
  expected_goal_revision: number;
  expected_grant_revision: number;
  public_brief: string;
  duration_days: number;
  budget: { max_inference_microusd: number; max_outstanding_runs: 1 };
  cadence: "daily";
  notification_limits: { per_day: number };
}
export interface GoalProgramme {
  schema_version: "GoalProgramme.v1";
  id: string;
  goal_id: string;
  goal_revision: number;
  grant_revision: number;
  public_brief: string;
  brief_digest: string;
  expires_at: string;
  confirmed_at: string;
  capability_ids: string[];
  budget: ProgrammeRequest["budget"];
  cadence: "daily";
  notification_limits: ProgrammeRequest["notification_limits"];
  state: "active" | "blocked" | "paused" | "revoked" | "review_due";
  reason_code: string | null;
  recovery: string | null;
  artifact_prefix: string;
  issuer_root_id: string;
  route_epoch: number;
  route_digest: string;
  review_digest: string;
}
export interface ProgrammePreview {
  programme: GoalProgramme;
  review_digest: string;
  public_only: true;
  preview_only: true;
  paused_programme_ids: string[];
}

export interface DiscoveryRun {
  job_id: string;
  programme_id: string;
  goal_revision: number;
  grant_revision: number;
  occurrence_day: string;
  status: string;
  deadline_at: string;
  external_effect_state: "none" | "unknown" | "settled";
  outstanding_held: boolean;
  accounting_liability: boolean;
  denial_cause?: string | null;
  outcome: null | { state: "findings" | "quiet" | "empty"; coverage: string; freshness: "current"; no_learning: true };
  no_learning: true;
  recovery: string | null;
}

export function isDiscoveryRun(value: unknown): value is DiscoveryRun {
  if (!value || typeof value !== "object") return false;
  const item = value as Partial<DiscoveryRun>;
  return typeof item.job_id === "string" && /^goal-discovery:[a-f0-9]{32}$/.test(item.job_id)
    && typeof item.programme_id === "string" && /^[a-f0-9]{32}$/.test(item.programme_id)
    && Number.isSafeInteger(item.goal_revision) && item.goal_revision! > 0
    && Number.isSafeInteger(item.grant_revision) && item.grant_revision! > 0
    && typeof item.occurrence_day === "string" && /^\d{4}-\d{2}-\d{2}$/.test(item.occurrence_day)
    && typeof item.deadline_at === "string" && Number.isFinite(Date.parse(item.deadline_at))
    && ["accepted", "queued", "running", "paused", "blocked", "failed", "succeeded", "degraded", "cancelled", "cost_liability", "unknown_external_effect"].includes(item.status ?? "")
    && ["none", "unknown", "settled"].includes(item.external_effect_state ?? "")
    && typeof item.outstanding_held === "boolean" && typeof item.accounting_liability === "boolean"
    && (item.denial_cause === undefined || item.denial_cause === null || [
      "programme_unclaimed_original_paused", "programme_unclaimed_original_revoked",
      "programme_unclaimed_identity_revoked", "programme_unclaimed_goal_changed",
      "programme_unclaimed_original_expired",
    ].includes(item.denial_cause))
    && item.no_learning === true && (item.recovery === null || typeof item.recovery === "string")
    && (item.outcome === null || (Boolean(item.outcome) && ["findings", "quiet", "empty"].includes(item.outcome!.state)
      && typeof item.outcome!.coverage === "string" && item.outcome!.freshness === "current" && item.outcome!.no_learning === true));
}
export function isGoalProgramme(value: unknown): value is GoalProgramme {
  if (!value || typeof value !== "object") return false;
  const item = value as Partial<GoalProgramme>;
  return item.schema_version === "GoalProgramme.v1"
    && typeof item.id === "string" && Boolean(item.id)
    && typeof item.goal_id === "string"
    && Number.isSafeInteger(item.goal_revision) && item.goal_revision! >= 1
    && Number.isSafeInteger(item.grant_revision) && item.grant_revision! >= 1
    && typeof item.public_brief === "string"
    && typeof item.brief_digest === "string" && /^[a-f0-9]{64}$/.test(item.brief_digest)
    && typeof item.review_digest === "string" && /^[a-f0-9]{64}$/.test(item.review_digest)
    && typeof item.route_digest === "string" && /^[a-f0-9]{64}$/.test(item.route_digest)
    && Number.isSafeInteger(item.route_epoch) && item.route_epoch! >= 1
    && typeof item.artifact_prefix === "string" && Boolean(item.artifact_prefix)
    && typeof item.issuer_root_id === "string"
    && typeof item.expires_at === "string" && Number.isFinite(Date.parse(item.expires_at))
    && typeof item.confirmed_at === "string" && Number.isFinite(Date.parse(item.confirmed_at))
    && Array.isArray(item.capability_ids) && item.capability_ids.length > 0 && item.capability_ids.every((id) => typeof id === "string")
    && item.cadence === "daily" && item.budget?.max_outstanding_runs === 1
    && Number.isSafeInteger(item.budget.max_inference_microusd) && item.budget.max_inference_microusd >= 0
    && Number.isSafeInteger(item.notification_limits?.per_day) && item.notification_limits!.per_day >= 0 && item.notification_limits!.per_day <= 2
    && ["active", "blocked", "paused", "revoked", "review_due"].includes(item.state ?? "")
    && (item.reason_code === null || typeof item.reason_code === "string")
    && (item.recovery === null || typeof item.recovery === "string");
}
export class ProgrammeError extends Error {
  constructor(public code: string) { super(code); }
}
export async function programmeApi<T>(goalId: string, suffix = "", body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await apiFetch(`${API_URL}/api/goals/${encodeURIComponent(goalId)}/programmes${suffix}`, {
    method: body === undefined ? "GET" : "POST", signal,
    ...(body === undefined ? {} : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  });
  const payload = await response.json();
  if (!response.ok) {
    const detail = payload?.detail;
    throw new ProgrammeError(typeof detail === "string" ? detail : detail?.code ?? "programme_request_failed");
  }
  return payload as T;
}
