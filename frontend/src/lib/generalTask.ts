import { API_URL } from "../config/constants";
import { apiFetch } from "./api";
import type { WorkBoardTask } from "../types";

export const GENERAL_TASK_CAPABILITY = "agent.task.v1";
export interface GeneralTaskLimits {
  max_steps: number; max_inference_calls: number; wall_seconds: number;
  depth: 0; max_outstanding_children: number; max_cost_microusd: number;
}
export interface DocumentTaskBinding {
  artifact_ref: string; source_revision: number; metadata_digest: string;
  citation_refs: string[]; selection_digest: string; acknowledge_local_use: true;
}
export interface GeneralTaskInput {
  goal_ref: string; intent: string; evidence_refs: string[];
  requested_output: Record<string, unknown>; limits: GeneralTaskLimits;
  tool_set_digest?: string; inference_egress_acknowledged: boolean;
  document_source?: DocumentTaskBinding;
}
export interface TaskPlan {
  schema_version: 1; revision: number;
  steps: { step_id: string; tool_id: string; input: Record<string, unknown>;
    depends_on: string[]; output_contract: Record<string, unknown> }[];
}
export interface GeneralToolDescriptor {
  tool_id: string; version: string; input_schema: Record<string, unknown>;
  output_schema: Record<string, unknown>; effects: string[]; permissions: string[];
  deadline: number; verifier: string; policy_digest: string;
}
export interface GeneralTaskPlanRead {
  task_id: string; task_revision: number; accepted: boolean;
  task_input: GeneralTaskInput; plan: TaskPlan | null; descriptors: GeneralToolDescriptor[]; proposal_error?: string;
  strategy: { status: string; reason: string | null }; no_learning: true;
  approval_pause?: GeneralTaskApprovalPause | null;
  native_execution?: GeneralTaskNativeExecution;
}
export interface GeneralTaskNativeExecution {
  phase: "native_ready" | "native_wait" | "assembly" | "operator_paused" | "approval_wait" | "cancelled" | "unknown_recovery" | "complete";
  plan_revision: number; manifest_revision: number; original_deadline_at: string; native_deadline_at: string;
  steps: { step_id: string; status: string; contact_state: string; invocation_id: string; plan_revision: number;
    artifact_refs: { artifact_id: string; digest: string; schema_version: string }[] }[];
  admitted_invocation_ids: string[]; remaining_steps: string[];
  partial_output_refs: { artifact_id: string; digest: string; schema_version: string }[]; no_learning: true;
}
export interface GeneralTaskApprovalPause {
  approval_id: string; approval_status: "pending" | "approved" | "expired" | "denied" | "revoked" | "consumed" | "unavailable";
  step_id: string; tool_id: string; workflow_run_id: string; attempt_id: string;
  fencing_token: number; workflow_revision: number; original_deadline_at: string;
  can_resume: boolean; reason: string | null;
}
export function canResumeGeneralTask(read: GeneralTaskPlanRead, task: WorkBoardTask): boolean {
  const pause = read.approval_pause, attempt = task.latest_attempt;
  return Boolean(read.accepted && read.plan && pause?.can_resume && pause.approval_status === "approved"
    && read.task_revision === task.task_revision && task.status === "blocked"
    && task.recovery_action === "approve_existing_run" && attempt && !attempt.ended_at
    && read.plan.steps.some(step => step.step_id === pause.step_id && step.tool_id === pause.tool_id)
    && pause.attempt_id === attempt.attempt_id && pause.workflow_run_id === attempt.workflow_run_id
    && pause.fencing_token === attempt.fencing_token && Date.parse(pause.original_deadline_at) > Date.now());
}
export interface GeneralTaskCreateRequest {
  goal_revision: number; idempotency_key: string; input: GeneralTaskInput;
}
export class GeneralTaskError extends Error {
  constructor(message: string, public status: number) { super(message); }
}
export async function generalTaskRequest(path: string, body?: unknown, signal?: AbortSignal): Promise<unknown> {
  const response = await apiFetch(`${API_URL}/api/work-board${path}`, {
    signal, ...(body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
  });
  if (!response.ok) {
    if ([401, 403].includes(response.status)) throw new GeneralTaskError("Current operator authority is unavailable. Sign in with the original task owner or recover ownership through Work, then refresh.", response.status);
    throw new GeneralTaskError(response.status === 409
      ? "The task, goal, plan or tool revision changed. Refresh the current plan and review again."
      : `Task service blocked (${response.status}). Inspect current consent, budget and capability settings, then refresh Work.`, response.status);
  }
  return response.json();
}
const record = (v: unknown): v is Record<string, unknown> => Boolean(v && typeof v === "object" && !Array.isArray(v));
const artifactReference = (v: unknown): boolean => record(v) && typeof v.artifact_id === "string"
  && v.artifact_id.length > 0 && v.artifact_id.length <= 128 && typeof v.digest === "string"
  && /^[a-f0-9]{64}$/.test(v.digest) && typeof v.schema_version === "string";
function validDocumentTaskBinding(value: unknown): value is DocumentTaskBinding {
  return record(value) && typeof value.artifact_ref === "string" && /^document-source:[0-9a-f-]{36}$/.test(value.artifact_ref)
    && typeof value.source_revision === "number" && Number.isSafeInteger(value.source_revision) && value.source_revision >= 1
    && typeof value.metadata_digest === "string" && /^[a-f0-9]{64}$/.test(value.metadata_digest)
    && Array.isArray(value.citation_refs) && value.citation_refs.length > 0 && value.citation_refs.length <= 16
    && value.citation_refs.every((ref) => typeof ref === "string" && ref.length > 0 && ref.length <= 512)
    && new Set(value.citation_refs).size === value.citation_refs.length
    && typeof value.selection_digest === "string" && /^[a-f0-9]{64}$/.test(value.selection_digest)
    && value.acknowledge_local_use === true;
}
export function validateGeneralTaskPlan(value: unknown, task: WorkBoardTask): GeneralTaskPlanRead {
  if (!record(value) || value.task_id !== task.task_id || value.task_revision !== task.task_revision
    || typeof value.accepted !== "boolean" || value.no_learning !== true || !record(value.task_input)
    || value.task_input.goal_ref !== task.goal_id || typeof value.task_input.intent !== "string"
    || (value.task_input.document_source !== undefined && !validDocumentTaskBinding(value.task_input.document_source))
    || !record(value.task_input.limits) || !Array.isArray(value.descriptors) || !record(value.strategy)
    || (value.plan === null ? (value.accepted !== false || typeof value.proposal_error !== "string")
      : (!record(value.plan) || value.plan.schema_version !== 1 || !Number.isSafeInteger(value.plan.revision) || !Array.isArray(value.plan.steps) || !value.plan.steps.length || !value.descriptors.length))) {
    throw new Error("Plan readback did not match the current task revision. Refresh Work before reviewing.");
  }
  const descriptors = value.descriptors;
  const native = value.native_execution;
  if (native != null && (!record(native) || !["native_ready", "native_wait", "assembly", "operator_paused", "approval_wait", "cancelled", "unknown_recovery", "complete"].includes(String(native.phase))
    || !Number.isSafeInteger(native.plan_revision) || Number(native.plan_revision) < 1 || Number(native.plan_revision) > 16
    || !Number.isSafeInteger(native.manifest_revision) || Number(native.manifest_revision) < 1
    || native.no_learning !== true || typeof native.original_deadline_at !== "string" || typeof native.native_deadline_at !== "string"
    || !Number.isFinite(Date.parse(native.original_deadline_at)) || !Number.isFinite(Date.parse(native.native_deadline_at))
    || Date.parse(native.native_deadline_at) > Date.parse(native.original_deadline_at)
    || !Array.isArray(native.steps) || native.steps.length > 16
    || native.steps.some(step => !record(step) || typeof step.step_id !== "string" || typeof step.invocation_id !== "string"
      || !["admitted", "running", "awaiting_approval", "verified", "failed", "blocked", "cancelled", "unknown"].includes(String(step.status))
      || !["not_contacted", "contact_started", "contact_denied", "unknown", "settled"].includes(String(step.contact_state))
      || !Number.isSafeInteger(step.plan_revision) || Number(step.plan_revision) < 1 || Number(step.plan_revision) > 16
      || !Array.isArray(step.artifact_refs) || step.artifact_refs.length > 16 || !step.artifact_refs.every(artifactReference))
    || !Array.isArray(native.admitted_invocation_ids) || native.admitted_invocation_ids.length > 16
    || !native.admitted_invocation_ids.every(id => typeof id === "string")
    || !Array.isArray(native.remaining_steps) || native.remaining_steps.length > 16 || !native.remaining_steps.every(id => typeof id === "string")
    || !Array.isArray(native.partial_output_refs) || native.partial_output_refs.length > 16 || !native.partial_output_refs.every(artifactReference))) {
    throw new Error("Native task receipts are incomplete. Refresh Work before continuing.");
  }
  const pause = value.approval_pause;
  if (pause != null && (!record(pause) || !["pending", "approved", "expired", "denied", "revoked", "consumed", "unavailable"].includes(String(pause.approval_status))
    || ["approval_id", "step_id", "tool_id", "workflow_run_id", "attempt_id"].some(k => typeof pause[k] !== "string" || !pause[k])
    || !Number.isSafeInteger(pause.fencing_token) || Number(pause.fencing_token) < 1
    || !Number.isSafeInteger(pause.workflow_revision) || Number(pause.workflow_revision) < 1
    || typeof pause.original_deadline_at !== "string" || !Number.isFinite(Date.parse(pause.original_deadline_at))
    || typeof pause.can_resume !== "boolean" || !(pause.reason === null || typeof pause.reason === "string"))) {
    throw new Error("Approval pause receipt is incomplete. Refresh Work before continuing.");
  }
  if (descriptors.some(d => !record(d) || typeof d.tool_id !== "string" || typeof d.version !== "string"
    || !record(d.input_schema) || !record(d.output_schema) || !Array.isArray(d.effects) || !Array.isArray(d.permissions)
    || !d.effects.every(x => typeof x === "string") || !d.permissions.every(x => typeof x === "string")
    || typeof d.verifier !== "string" || typeof d.deadline !== "number")) throw new Error("Registered tool metadata is incomplete; refresh before acceptance.");
  if (record(value.plan) && (value.plan.steps as unknown[]).some(s => !record(s) || typeof s.step_id !== "string" || typeof s.tool_id !== "string"
    || !record(s.input) || !record(s.output_contract) || !Array.isArray(s.depends_on)
    || !s.depends_on.every(x => typeof x === "string") || !descriptors.some(d => record(d) && d.tool_id === s.tool_id))) {
    throw new Error("Plan steps do not match the registered tool descriptors.");
  }
  return value as unknown as GeneralTaskPlanRead;
}
export async function createGeneralTask(request: GeneralTaskCreateRequest, principal: string, session: string): Promise<WorkBoardTask> {
  const value = await generalTaskRequest("/general-tasks", request);
  if (!record(value) || !record(value.task) || typeof value.task.task_id !== "string"
    || value.task.capability_id !== GENERAL_TASK_CAPABILITY || value.task.owner_principal_id !== principal
    || value.task.owner_session_id !== session || value.task.goal_id !== request.input.goal_ref
    || value.task.goal_revision !== request.goal_revision || value.task.status !== "triage"
    || value.task.requires_review !== true || value.task.idempotency_key !== request.idempotency_key) {
    throw new Error("Task receipt is unconfirmed. Inspect Work or retry the exact request before starting another task.");
  }
  return value.task as unknown as WorkBoardTask;
}
