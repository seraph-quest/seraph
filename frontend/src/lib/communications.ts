import { generalTaskRequest } from "./generalTask";
import type { CalendarMeetingPrepInput, WorkBoardTask } from "../types";
import type { createMailReplyTask } from "./mailApi";

export type CommunicationReplyInput = Omit<Parameters<typeof createMailReplyTask>[0], "idempotency_key">;
export interface CommunicationSelection {
  reply_inputs: CommunicationReplyInput[]; meeting_inputs: CalendarMeetingPrepInput[];
  reschedule_inputs: CommunicationRescheduleInput[]; acknowledge_private_review: true;
}
export interface CommunicationCreate {
  goal_id: string; goal_revision: number; selection: CommunicationSelection;
  max_cost_microusd: number; wall_seconds: number; idempotency_key: string;
}
export interface ExactCommunicationApproval { operation_id: string; exact_preview_digest: string; approval_id: string; expires_at: number }
export async function createCommunicationTask(body: CommunicationCreate, ownerPrincipalId: string, ownerSessionId: string, signal: AbortSignal): Promise<WorkBoardTask> {
  const v = await generalTaskRequest("/general-tasks/communications", body, signal);
  if (!record(v) || !record(v.task) || v.task.capability_id !== "agent.task.v1" || v.task.goal_id !== body.goal_id
    || v.task.goal_revision !== body.goal_revision || v.task.owner_principal_id !== ownerPrincipalId || v.task.owner_session_id !== ownerSessionId
    || v.task.idempotency_key !== body.idempotency_key || !text(v.task.task_id, 128) || !integer(v.task.task_revision)) {
    throw Error("Communication task admission is unconfirmed. Keep the exact original request for explicit reconciliation.");
  }
  return v.task as unknown as WorkBoardTask;
}

export interface CommunicationSourceRef {
  source_id: string; capability_id: "work.mail-reply-draft.v1" | "calendar.meeting-prep.v1";
  source_revision: string; source_input_digest: string; task_id: string; attempt_id: string;
  job_id: string; artifact_path: string; artifact_digest: string; readback_id: string;
}
export interface CommunicationReply { source_ref: CommunicationSourceRef; subject: string; body: string; caveats: string[] }
export interface CommunicationBrief {
  schema_version: 1; event_key: string; event_revision: string; summary: string;
  agenda: string[]; questions: string[]; risks: string[]; preparation_steps: string[];
}
export interface CommunicationRescheduleInput {
  schema_version: 1; consent_id: string; expected_consent_revision: number;
  event_binding_id: string; expected_event_binding_revision: number; goal_id: string; goal_revision: number;
  new_start: { dateTime: string; timeZone: string }; new_end: { dateTime: string; timeZone: string };
}
export interface CommunicationPlan {
  source_refs: CommunicationSourceRef[]; reply_drafts: CommunicationReply[];
  meeting_preparations: { source_ref: CommunicationSourceRef; brief: CommunicationBrief }[];
  reschedule_proposals: { source_ref: CommunicationSourceRef; input: CommunicationRescheduleInput }[];
  unresolved_questions: { source_id: string; reason: string; job_id: string | null;
    recovery: "review_source" | "inspect_original" | "review_capacity" | "review_calendar_occupancy" }[];
}
export interface CommunicationPlanRead { task_id: string; plan: CommunicationPlan; no_learning: true }
export interface CommunicationCleanupRequest { expected_task_revision: number }
export interface CommunicationCleanupResult { status: "cleanup_verified" | "cleanup_unresolved"; absent: boolean; no_learning: true }
export interface CommunicationActionBundle {
  selected_actions: { kind: "reply" | "reschedule"; source_input_digest: string; operation_id: string }[];
  exact_preview_digests: string[]; approval_ids: string[];
}
const record = (v: unknown): v is Record<string, unknown> => Boolean(v && typeof v === "object" && !Array.isArray(v));
const text = (v: unknown, max = 256): v is string => typeof v === "string" && v.length > 0 && v.length <= max && !/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(v);
const digest = (v: unknown): v is string => typeof v === "string" && /^[a-f0-9]{64}$/.test(v);
const integer = (v: unknown) => Number.isSafeInteger(v) && Number(v) >= 1;
const keys = (v: unknown, names: string[]): v is Record<string, unknown> => record(v) && Object.keys(v).length === names.length && names.every(n => Object.prototype.hasOwnProperty.call(v, n));
const list = (v: unknown, max: number, check: (entry: unknown) => boolean): boolean => Array.isArray(v) && v.length <= max && v.every(check);
const sourceKeys = ["source_id", "capability_id", "source_revision", "source_input_digest", "task_id", "attempt_id", "job_id", "artifact_path", "artifact_digest", "readback_id"];
function source(v: unknown): v is CommunicationSourceRef {
  return keys(v, sourceKeys) && text(v.source_id) && ["work.mail-reply-draft.v1", "calendar.meeting-prep.v1"].includes(String(v.capability_id))
    && text(v.source_revision, 128) && digest(v.source_input_digest) && text(v.task_id, 128) && text(v.attempt_id, 128)
    && text(v.job_id) && text(v.artifact_path, 512) && digest(v.artifact_digest) && text(v.readback_id);
}
function brief(v: unknown): boolean {
  return keys(v, ["schema_version", "event_key", "event_revision", "summary", "agenda", "questions", "risks", "preparation_steps"])
    && v.schema_version === 1 && text(v.event_key) && text(v.event_revision, 128) && text(v.summary, 1200)
    && [v.agenda, v.questions, v.risks, v.preparation_steps].every(items => list(items, 8, entry => text(entry, 400)));
}
function time(v: unknown): boolean { return keys(v, ["dateTime", "timeZone"]) && text(v.dateTime, 128) && text(v.timeZone, 128); }
function reschedule(v: unknown): boolean {
  return keys(v, ["schema_version", "consent_id", "expected_consent_revision", "event_binding_id", "expected_event_binding_revision", "goal_id", "goal_revision", "new_start", "new_end"])
    && v.schema_version === 1 && text(v.consent_id) && integer(v.expected_consent_revision) && text(v.event_binding_id)
    && integer(v.expected_event_binding_revision) && text(v.goal_id) && integer(v.goal_revision) && time(v.new_start) && time(v.new_end);
}
export function parseCommunicationPlan(value: unknown, taskId: string): CommunicationPlanRead {
  const invalid = () => { throw Error("The private communication plan is incomplete or changed. Refresh the original task and review its sources."); };
  if (!keys(value, ["task_id", "plan", "no_learning"]) || value.task_id !== taskId || value.no_learning !== true
    || !keys(value.plan, ["source_refs", "reply_drafts", "meeting_preparations", "reschedule_proposals", "unresolved_questions"]) || new TextEncoder().encode(JSON.stringify(value.plan)).length > 65536) return invalid();
  const p = value.plan;
  if (!list(p.source_refs, 10, source)) return invalid();
  const refs = p.source_refs as CommunicationSourceRef[];
  if (new Set(refs.map(r => r.source_input_digest)).size !== refs.length) return invalid();
  const exactRef = (v: unknown, capability: string) => source(v) && v.capability_id === capability
    && refs.some(r => sourceKeys.every(k => r[k as keyof CommunicationSourceRef] === v[k as keyof CommunicationSourceRef]));
  if (!list(p.reply_drafts, 5, v => keys(v, ["source_ref", "subject", "body", "caveats"]) && exactRef(v.source_ref, "work.mail-reply-draft.v1") && text(v.subject, 200) && text(v.body, 4000) && list(v.caveats, 5, item => text(item, 300)))
    || !list(p.meeting_preparations, 5, v => keys(v, ["source_ref", "brief"]) && exactRef(v.source_ref, "calendar.meeting-prep.v1") && brief(v.brief))
    || !list(p.reschedule_proposals, 3, v => keys(v, ["source_ref", "input"]) && exactRef(v.source_ref, "calendar.meeting-prep.v1") && reschedule(v.input))
    || !list(p.unresolved_questions, 16, v => keys(v, ["source_id", "reason", "job_id", "recovery"]) && text(v.source_id) && typeof v.reason === "string" && /^[a-z][a-z0-9_]{0,127}$/.test(v.reason)
      && (v.job_id === null || text(v.job_id)) && ["review_source", "inspect_original", "review_capacity", "review_calendar_occupancy"].includes(String(v.recovery)))) return invalid();
  return value as unknown as CommunicationPlanRead;
}
export async function readCommunicationPlan(taskId: string, signal: AbortSignal): Promise<CommunicationPlanRead> {
  return parseCommunicationPlan(await generalTaskRequest(`/tasks/${encodeURIComponent(taskId)}/communications`, undefined, signal), taskId);
}
export function parseCommunicationCleanup(value: unknown): CommunicationCleanupResult {
  if (!keys(value, ["status", "absent", "no_learning"]) || value.no_learning !== true
    || !((value.status === "cleanup_verified" && value.absent === true)
      || (value.status === "cleanup_unresolved" && value.absent === false))) {
    throw Error("Aggregate private plan cleanup is unconfirmed. Reconcile only the original cleanup request; absence has not been verified.");
  }
  return value as unknown as CommunicationCleanupResult;
}
export async function cleanupCommunicationPlan(taskId: string, body: CommunicationCleanupRequest, signal: AbortSignal): Promise<CommunicationCleanupResult> {
  if (!text(taskId, 128) || !integer(body.expected_task_revision)) throw Error("Cleanup requires the exact original task revision.");
  // The response has no echoed task/owner fields. Authority remains at the
  // authenticated task-scoped endpoint; the panel fences the request context.
  return parseCommunicationCleanup(await generalTaskRequest(`/tasks/${encodeURIComponent(taskId)}/communications/cleanup`, body, signal));
}
export async function reviewCommunicationActions(taskId: string, bundle: CommunicationActionBundle, signal: AbortSignal): Promise<void> {
  const response = await generalTaskRequest(`/tasks/${encodeURIComponent(taskId)}/communications/selection`, bundle, signal);
  if (!keys(response, ["task_id", "bundle", "no_learning"]) || response.task_id !== taskId || response.no_learning !== true || JSON.stringify(response.bundle) !== JSON.stringify(bundle)) {
    throw Error("Selected action validation is unconfirmed. Inspect each original native operation.");
  }
}
