import { API_URL } from "../config/constants";
import { apiFetch } from "./api";
import type { WorkBoardTask } from "../types";

export const GENERAL_TASK_CAPABILITY = "agent.task.v1";
export interface GeneralTaskLimits {
  max_steps: number; max_inference_calls: number; wall_seconds: number;
  depth: 0; max_outstanding_children: number; max_cost_microusd: number;
}
export interface GeneralTaskInput {
  goal_ref: string; intent: string; evidence_refs: string[];
  requested_output: Record<string, unknown>; limits: GeneralTaskLimits;
  tool_set_digest?: string; inference_egress_acknowledged: boolean;
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
export function validateGeneralTaskPlan(value: unknown, task: WorkBoardTask): GeneralTaskPlanRead {
  if (!record(value) || value.task_id !== task.task_id || value.task_revision !== task.task_revision
    || typeof value.accepted !== "boolean" || value.no_learning !== true || !record(value.task_input)
    || value.task_input.goal_ref !== task.goal_id || typeof value.task_input.intent !== "string"
    || !record(value.task_input.limits) || !Array.isArray(value.descriptors) || !record(value.strategy)
    || (value.plan === null ? (value.accepted !== false || typeof value.proposal_error !== "string")
      : (!record(value.plan) || value.plan.schema_version !== 1 || !Number.isSafeInteger(value.plan.revision) || !Array.isArray(value.plan.steps) || !value.plan.steps.length || !value.descriptors.length))) {
    throw new Error("Plan readback did not match the current task revision. Refresh Work before reviewing.");
  }
  const descriptors = value.descriptors;
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
