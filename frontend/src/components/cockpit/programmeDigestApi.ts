import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";

export interface FollowThroughIntent {
  finding_id: string;
  desired_outcome: string;
  task_proposal_id: string | null;
  due_at: string | null;
  status: string;
}
export interface PreparedOutput { artifact_id: string; digest: string; schema_version: 1 }
export interface ProgrammeFinding {
  id: string; goal_id: string; programme_id: string; job_id: string; text: string; task_id: string | null;
  citations: { source_id: string; first_line: number; last_line: number; span_sha256: string }[];
  prepared_outputs: PreparedOutput[]; follow_through: FollowThroughIntent | null;
  actionable: boolean; recovery: string | null; source_freshness: "current" | "stale" | "blocked";
}
export interface ProgrammeStatus {
  goal_id: string; id: string; grant_revision: number; state: string; reason_code: string | null;
  last_run: string | null; sources_checked: number; output: string | null; next_run: string | null;
  current_run_status: string | null; current_admitted_at: string | null;
  next_digest_at?: string | null;
  remaining_allowance_microusd: number | null; recovery: string | null;
}
export interface ProgrammeDigestSnapshot {
  digests: { id: string; created_at: string; digest: {
    local_date: string; timezone: string;
    programme_ids: string[]; finding_ids: string[]; prepared_outputs: PreparedOutput[]; blocked_reasons: string[];
  }; findings: ProgrammeFinding[] }[];
  programmes: ProgrammeStatus[];
  notifications: { enabled: boolean; deadline_categories: string[]; digest_slots_remaining: number;
    deadline_slots_remaining: number; quiet_hours_active: boolean; delivery_debt: boolean };
}

export async function programmeDigestRequest<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await apiFetch(`${API_URL}/api/guardian/inbox/${path}`, {
    signal, ...(body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload?.detail?.code ?? "Programme readback unavailable. Refresh before acting.");
  return payload as T;
}

export function isProgrammeDigestSnapshot(value: unknown): value is ProgrammeDigestSnapshot {
  if (!value || typeof value !== "object") return false;
  const snapshot = value as ProgrammeDigestSnapshot;
  const nullableText = (text: unknown) => text === null || typeof text === "string";
  const timestamp = (text: unknown) => text === null || (typeof text === "string" && Number.isFinite(Date.parse(text)));
  const outputs = (rows: PreparedOutput[]) => Array.isArray(rows) && rows.every((row) => row && typeof row.artifact_id === "string" && typeof row.digest === "string" && row.schema_version === 1);
  return Array.isArray(snapshot.programmes) && snapshot.programmes.every((p) => p && typeof p.id === "string" && typeof p.goal_id === "string"
    && Number.isSafeInteger(p.grant_revision) && p.grant_revision > 0
    && ["active", "blocked", "paused", "revoked", "review_due"].includes(p.state) && Number.isSafeInteger(p.sources_checked) && p.sources_checked >= 0
    && timestamp(p.last_run) && timestamp(p.next_run) && nullableText(p.output) && nullableText(p.reason_code) && nullableText(p.recovery)
    && (p.current_run_status === null || ["accepted", "queued", "running", "awaiting_approval", "paused", "blocked", "unknown_external_effect", "cost_liability", "failed", "degraded", "succeeded", "cancelled"].includes(p.current_run_status))
    && timestamp(p.current_admitted_at) && ((p.current_run_status === null) === (p.current_admitted_at === null))
    && (p.next_digest_at === undefined || timestamp(p.next_digest_at))
    && (p.remaining_allowance_microusd === null || (Number.isSafeInteger(p.remaining_allowance_microusd) && p.remaining_allowance_microusd >= 0)))
    && Array.isArray(snapshot.digests) && snapshot.digests.every((entry) => entry && typeof entry.id === "string"
      && entry.digest && typeof entry.digest.local_date === "string" && typeof entry.digest.timezone === "string"
      && Array.isArray(entry.digest.blocked_reasons) && entry.digest.blocked_reasons.every((reason) => typeof reason === "string")
      && outputs(entry.digest.prepared_outputs) && Array.isArray(entry.digest.finding_ids) && Array.isArray(entry.digest.programme_ids)
      && Array.isArray(entry.findings) && entry.findings.every((f) => f && typeof f.id === "string" && entry.digest.finding_ids.includes(f.id)
        && typeof f.text === "string" && typeof f.actionable === "boolean" && nullableText(f.task_id) && nullableText(f.recovery)
        && ["current", "stale", "blocked"].includes(f.source_freshness) && entry.digest.programme_ids.includes(f.programme_id)
        && outputs(f.prepared_outputs) && Array.isArray(f.citations)
        && f.citations.every((c) => c && typeof c.source_id === "string" && Number.isSafeInteger(c.first_line) && Number.isSafeInteger(c.last_line))
        && (f.follow_through === null || (f.follow_through?.finding_id === f.id && typeof f.follow_through.desired_outcome === "string"
          && ["pending", "prepared", "deferred", "dismissed", "completed", "blocked"].includes(f.follow_through.status)
          && nullableText(f.follow_through.task_proposal_id) && timestamp(f.follow_through.due_at)))))
    && typeof snapshot.notifications?.enabled === "boolean" && Array.isArray(snapshot.notifications.deadline_categories)
    && Number.isSafeInteger(snapshot.notifications.digest_slots_remaining) && Number.isSafeInteger(snapshot.notifications.deadline_slots_remaining)
    && snapshot.notifications.deadline_categories.every((category) => typeof category === "string")
    && snapshot.notifications.digest_slots_remaining >= 0 && snapshot.notifications.digest_slots_remaining <= 1
    && snapshot.notifications.deadline_slots_remaining >= 0 && snapshot.notifications.deadline_slots_remaining <= 1
    && typeof snapshot.notifications.quiet_hours_active === "boolean" && typeof snapshot.notifications.delivery_debt === "boolean";
}
