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
