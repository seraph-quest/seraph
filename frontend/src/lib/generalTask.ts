import { API_URL } from "../config/constants";
import { apiFetch } from "./api";
import type { WorkBoardTask } from "../types";
import { validBuildBinding, type DocumentBuildTaskBinding } from "./documentBuild";

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
  document_build?: DocumentBuildTaskBinding;
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
  strategy: { status: "none" | "active" | "blocked"; reason: string | null; method_id?: string | null;
    version?: string | null; digest?: string | null; typed_data?: Record<string, unknown> | null }; no_learning: true;
  approval_pause?: GeneralTaskApprovalPause | null;
  native_execution?: GeneralTaskNativeExecution;
}
export interface GeneralTaskNativeExecution {
  partial_review_options?: SpecialistPartialOptions;
  partial_review?: SpecialistPartialReview;
  cancellation?: { state: "pending" | "callback_closed_outcome_debt" | "fully_cancelled";
    child_ids: string[]; callback_closed: boolean; effect_debt: boolean; reason: string };
  phase: "native_ready" | "native_wait" | "assembly" | "operator_paused" | "approval_wait" | "cancelled" | "unknown_recovery" | "complete";
  plan_revision: number; manifest_revision: number; original_deadline_at: string; native_deadline_at: string;
  steps: { step_id: string; status: string; contact_state: string; invocation_id: string; plan_revision: number;
    artifact_refs: { artifact_id: string; digest: string; schema_version: string }[] }[];
  admitted_invocation_ids: string[]; remaining_steps: string[];
  partial_output_refs: { artifact_id: string; digest: string; schema_version: string }[]; no_learning: true;
}
export interface SpecialistPartialOutput {
  child_task_id: string; child_job_id: string; delegation_invocation_id: string;
  artifact_id: string; content_sha256: string; size_bytes: number;
}
export type SpecialistPartialOptions = { eligible: false; reason: string } | {
  eligible: boolean; attempt_id: string; workflow_run_id: string;
  expected_manifest_revision: number; expected_plan_revision: number;
  selected_steps: { step_id: string; outputs: SpecialistPartialOutput[] }[]; no_learning: true;
};
export interface SpecialistPartialReview {
  state: "partial_review_pending_debt"; decision_digest: string; idempotency_key: string;
  result_ref: { artifact_id: string; digest: string; schema_version: "SpecialistPartialResult.v1" };
  selected_step_ids: string[]; selected_outputs: (SpecialistPartialOutput & { step_id: string })[];
  unresolved_job_ids: string[]; unresolved_effect_count: number;
  current_unresolved_job_ids: string[]; current_unresolved_effect_count: number; current_unresolved_cost_count: number;
  current_cancellation_state: "pending" | "callback_closed_outcome_debt" | "fully_cancelled"; no_learning: true;
}
export interface SpecialistPartialRequest {
  action: "accept_partial_results"; expected_revision: number;
  partial_decision: { idempotency_key: string; attempt_id: string; workflow_run_id: string;
    expected_manifest_revision: number; expected_plan_revision: number; selected_step_ids: string[]; acknowledge_unresolved: true };
}
export interface GeneralTaskApprovalPause {
  approval_id: string; approval_status: "pending" | "approved" | "expired" | "denied" | "revoked" | "consumed" | "unavailable";
  step_id: string; tool_id: string; workflow_run_id: string; attempt_id: string;
  fencing_token: number; workflow_revision: number; original_deadline_at: string;
  can_resume: boolean; reason: string | null;
  child_job_id?: string; expected_manifest_revision?: number;
}
export function canResumeGeneralTask(read: GeneralTaskPlanRead, task: WorkBoardTask): boolean {
  const pause = read.approval_pause, attempt = task.latest_attempt;
  return Boolean(read.accepted && read.plan && pause?.can_resume && pause.approval_status === "approved"
    && read.task_revision === task.task_revision && task.status === "blocked"
    && task.recovery_action === "approve_existing_run" && attempt && !attempt.ended_at
    && read.plan.steps.some(step => step.step_id === pause.step_id && step.tool_id === pause.tool_id)
    && (!read.native_execution || (read.native_execution.phase === "approval_wait"
      && pause.expected_manifest_revision === read.native_execution.manifest_revision
      && Boolean(pause.child_job_id && read.native_execution.admitted_invocation_ids.includes(pause.child_job_id))
      && read.native_execution.steps.some(step => step.step_id === pause.step_id
        && step.invocation_id === pause.child_job_id && step.status === "awaiting_approval"
        && step.contact_state === "not_contacted")))
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
const closed = (v: Record<string, unknown>, keys: string[]) => Object.keys(v).length === keys.length && keys.every(k => Object.prototype.hasOwnProperty.call(v, k));
const identity = (v: unknown): v is string => typeof v === "string" && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(v);
const sha = (v: unknown) => typeof v === "string" && /^[a-f0-9]{64}$/.test(v);
const uuid = (v: unknown) => typeof v === "string" && /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(v);
const uniqueIds = (v: unknown, max: number, min = 0): v is string[] => Array.isArray(v) && v.length >= min && v.length <= max && v.every(identity) && new Set(v).size === v.length;
const partialBytes = (v: unknown) => new TextEncoder().encode(JSON.stringify(v)).length <= 65536;
const partialOutputKeys = ["child_task_id", "child_job_id", "delegation_invocation_id", "artifact_id", "content_sha256", "size_bytes"];
function validPartialOutput(v: unknown, selected = false): boolean {
  return record(v) && closed(v, selected ? [...partialOutputKeys, "step_id"] : partialOutputKeys)
    && partialOutputKeys.slice(0, 4).every(k => identity(v[k])) && sha(v.content_sha256)
    && Number.isSafeInteger(v.size_bytes) && Number(v.size_bytes) >= 1 && Number(v.size_bytes) <= 65536
    && (!selected || identity(v.step_id));
}
function samePartialChild(outputs: Record<string, unknown>[]): boolean {
  return outputs.every(output => output.child_task_id === outputs[0]?.child_task_id
    && output.child_job_id === outputs[0]?.child_job_id && output.delegation_invocation_id === outputs[0]?.delegation_invocation_id);
}
export function validateSpecialistPartialReview(v: unknown): SpecialistPartialReview {
  if (!record(v) || !closed(v, ["state", "decision_digest", "idempotency_key", "result_ref", "selected_step_ids", "selected_outputs", "unresolved_job_ids", "unresolved_effect_count", "current_unresolved_job_ids", "current_unresolved_effect_count", "current_unresolved_cost_count", "current_cancellation_state", "no_learning"])
    || v.state !== "partial_review_pending_debt" || !sha(v.decision_digest) || !uuid(v.idempotency_key) || v.no_learning !== true
    || !record(v.result_ref) || !closed(v.result_ref, ["artifact_id", "digest", "schema_version"])
    || !identity(v.result_ref.artifact_id) || !sha(v.result_ref.digest) || v.result_ref.schema_version !== "SpecialistPartialResult.v1"
    || !uniqueIds(v.selected_step_ids, 4, 1) || !Array.isArray(v.selected_outputs) || !v.selected_outputs.length || v.selected_outputs.length > 64
    || !v.selected_outputs.every(output => validPartialOutput(output, true) && (v.selected_step_ids as string[]).includes(output.step_id))
    || v.selected_step_ids.some(id => !(v.selected_outputs as Record<string, unknown>[]).some(output => output.step_id === id))
    || v.selected_step_ids.some(id => !samePartialChild((v.selected_outputs as Record<string, unknown>[]).filter(output => output.step_id === id)))
    || new Set(v.selected_outputs.map(output => output.artifact_id)).size !== v.selected_outputs.length
    || !uniqueIds(v.unresolved_job_ids, 84) || !Number.isSafeInteger(v.unresolved_effect_count) || Number(v.unresolved_effect_count) < 0 || Number(v.unresolved_effect_count) > 256
    || !uniqueIds(v.current_unresolved_job_ids, 84) || !Number.isSafeInteger(v.current_unresolved_effect_count) || Number(v.current_unresolved_effect_count) < 0 || Number(v.current_unresolved_effect_count) > 256
    || !Number.isSafeInteger(v.current_unresolved_cost_count) || Number(v.current_unresolved_cost_count) < 0 || Number(v.current_unresolved_cost_count) > 12
    || !["pending", "callback_closed_outcome_debt", "fully_cancelled"].includes(String(v.current_cancellation_state)) || !partialBytes(v)) {
    throw Error("Partial review receipt is incomplete. Refresh the original task; unresolved debt remains pending.");
  }
  return v as unknown as SpecialistPartialReview;
}
function validatePartialOptions(v: unknown, task: WorkBoardTask, native: Record<string, unknown>, plan: Record<string, unknown> | null): void {
  if (record(v) && closed(v, ["eligible", "reason"]) && v.eligible === false && typeof v.reason === "string" && v.reason.length <= 200) return;
  if (!record(v) || !closed(v, ["eligible", "attempt_id", "workflow_run_id", "expected_manifest_revision", "expected_plan_revision", "selected_steps", "no_learning"])
    || typeof v.eligible !== "boolean" || v.no_learning !== true || !identity(v.attempt_id) || !identity(v.workflow_run_id)
    || v.attempt_id !== task.latest_attempt?.attempt_id || v.workflow_run_id !== task.latest_attempt?.workflow_run_id
    || v.expected_manifest_revision !== native.manifest_revision || v.expected_plan_revision !== native.plan_revision
    || !Array.isArray(v.selected_steps) || v.selected_steps.length > 4 || (v.eligible && !v.selected_steps.length)
    || v.selected_steps.some(step => !record(step) || !closed(step, ["step_id", "outputs"]) || !identity(step.step_id)
      || !Array.isArray(plan?.steps) || !plan.steps.some(original => record(original) && original.step_id === step.step_id)
      || !Array.isArray(step.outputs) || !step.outputs.length || step.outputs.length > 64 || !step.outputs.every(output => validPartialOutput(output)) || !samePartialChild(step.outputs))
    || new Set(v.selected_steps.map(step => step.step_id)).size !== v.selected_steps.length
    || v.selected_steps.flatMap(step => step.outputs).length > 64
    || new Set(v.selected_steps.flatMap(step => step.outputs.map((output: SpecialistPartialOutput) => output.artifact_id))).size !== v.selected_steps.flatMap(step => step.outputs).length
    || (v.eligible && (task.status !== "blocked" || task.latest_attempt?.ended_at || native.phase !== "unknown_recovery" || !record(native.cancellation) || !["pending", "callback_closed_outcome_debt"].includes(String(native.cancellation.state))))
    || !partialBytes(v)) throw Error("Partial selection did not match the original stopped task. Refresh before reviewing.");
}
const artifactReference = (v: unknown): boolean => record(v) && typeof v.artifact_id === "string"
  && v.artifact_id.length > 0 && v.artifact_id.length <= 128 && typeof v.digest === "string"
  && /^[a-f0-9]{64}$/.test(v.digest) && typeof v.schema_version === "string";
export function validDocumentTaskBinding(value: unknown): value is DocumentTaskBinding {
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
    || (value.task_input.document_build !== undefined && !validBuildBinding(value.task_input.document_build))
    || !record(value.task_input.limits) || !Array.isArray(value.descriptors) || !record(value.strategy)
    || (value.plan === null ? (value.accepted !== false || typeof value.proposal_error !== "string")
      : (!record(value.plan) || value.plan.schema_version !== 1 || !Number.isSafeInteger(value.plan.revision) || !Array.isArray(value.plan.steps) || !value.plan.steps.length || !value.descriptors.length))) {
    throw new Error("Plan readback did not match the current task revision. Refresh Work before reviewing.");
  }
  const strategy = value.strategy;
  if (!["none", "active", "blocked"].includes(String(strategy.status))
    || (strategy.status === "active" && (typeof strategy.method_id !== "string" || !strategy.method_id
      || typeof strategy.version !== "string" || !strategy.version || typeof strategy.digest !== "string"
      || !/^[a-f0-9]{64}$/.test(strategy.digest) || !record(strategy.typed_data)
      || !["TaskMethod.v1", "ResearchStrategy.v1"].includes(String(strategy.typed_data.schema_version))))
    || (strategy.status === "none" && [strategy.method_id, strategy.version, strategy.digest, strategy.typed_data].some(v => v != null))) {
    throw new Error("Original task method binding is unavailable. Refresh the exact plan.");
  }
  const descriptors = value.descriptors;
  const native = value.native_execution;
  if (native != null && (!record(native) || !["native_ready", "native_wait", "assembly", "operator_paused", "approval_wait", "cancelled", "unknown_recovery", "complete"].includes(String(native.phase))
    || (native.cancellation != null && (!record(native.cancellation)
      || !["pending", "callback_closed_outcome_debt", "fully_cancelled"].includes(String(native.cancellation.state))
      || (native.cancellation.state === "fully_cancelled" ? native.phase !== "cancelled" : native.phase !== "unknown_recovery")
      || !Array.isArray(native.cancellation.child_ids) || native.cancellation.child_ids.length > 16
      || !native.cancellation.child_ids.every(id => typeof id === "string" && id.length > 0 && id.length <= 256)
      || typeof native.cancellation.callback_closed !== "boolean" || typeof native.cancellation.effect_debt !== "boolean"
      || typeof native.cancellation.reason !== "string" || native.cancellation.reason.length > 500
      || (native.cancellation.state === "pending" ? native.cancellation.callback_closed
        : !native.cancellation.callback_closed)
      || (native.cancellation.state === "fully_cancelled" && native.cancellation.effect_debt)
      || (native.cancellation.state === "callback_closed_outcome_debt" && !native.cancellation.effect_debt)))
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
  if (record(native)) {
    const planSteps: unknown[] = record(value.plan) && Array.isArray(value.plan.steps) ? value.plan.steps : [];
    if (native.partial_review_options !== undefined) validatePartialOptions(native.partial_review_options, task, native, record(value.plan) ? value.plan : null);
    if (record(native.partial_review_options) && native.partial_review_options.eligible === true && value.accepted !== true) {
      throw Error("Partial review requires the original admitted task plan.");
    }
    if (native.partial_review !== undefined) {
      const partial = validateSpecialistPartialReview(native.partial_review);
      if (!record(native.cancellation) || partial.current_cancellation_state !== native.cancellation.state
        || !planSteps.length || partial.selected_step_ids.some(id => !planSteps.some(step => record(step) && step.step_id === id))) {
        throw Error("Partial review is not bound to the original stopped task. Refresh Work.");
      }
    }
    const outputs = record(native.partial_review_options) && Array.isArray(native.partial_review_options.selected_steps)
      ? native.partial_review_options.selected_steps.flatMap(step => step.outputs.map((output: SpecialistPartialOutput) => ({ ...output, step_id: step.step_id }))) : [];
    if (record(native.partial_review) && Array.isArray(native.partial_review.selected_outputs)) outputs.push(...native.partial_review.selected_outputs);
    if (outputs.some(output => !Array.isArray(native.steps) || !native.steps.some(step => record(step)
      && step.step_id === output.step_id && step.invocation_id === output.delegation_invocation_id && step.status === "verified" && step.contact_state === "settled"))) {
      throw Error("Partial artifacts do not match verified original specialist steps. Refresh Work.");
    }
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
  if (record(pause) && ((pause.child_job_id !== undefined) !== (pause.expected_manifest_revision !== undefined)
    || (pause.child_job_id !== undefined && (typeof pause.child_job_id !== "string" || !pause.child_job_id
      || pause.child_job_id.length > 256 || !Number.isSafeInteger(pause.expected_manifest_revision)
      || Number(pause.expected_manifest_revision) < 1)))) {
    throw new Error("Native approval binding is incomplete. Refresh Work before continuing.");
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
  if (value.task_input.document_build !== undefined) {
    const binding = value.task_input.document_build as DocumentBuildTaskBinding;
    const steps = record(value.plan) ? value.plan.steps as Record<string, unknown>[] : [];
    const step = steps[0], limits = value.task_input.limits;
    if (steps.length !== 1 || !record(step) || step.step_id !== "build" || step.tool_id !== "document_build"
      || !record(step.input) || Object.keys(step.input).length !== 2 || step.input.build_ref !== binding.build_ref || step.input.spec_digest !== binding.spec_digest
      || !Array.isArray(step.depends_on) || step.depends_on.length !== 0 || value.task_input.intent !== "Build the reviewed local document specification"
      || value.task_input.document_source !== undefined || value.task_input.inference_egress_acknowledged !== false
      || limits.max_steps !== 1 || limits.max_inference_calls !== 0 || limits.max_cost_microusd !== 0 || limits.max_outstanding_children !== 0
      || typeof limits.wall_seconds !== "number" || limits.wall_seconds > 60) {
      throw new Error("Document builds require their original fixed zero-inference local renderer plan. Refresh Work before reviewing.");
    }
  }
  return value as unknown as GeneralTaskPlanRead;
}
export function validateSpecialistPartialResponse(value: unknown, task: WorkBoardTask, request: SpecialistPartialRequest): SpecialistPartialReview {
  const latest = record(value) && record(value.task) && record(value.task.latest_attempt) ? value.task.latest_attempt : null;
  if (!record(value) || !closed(value, ["task", "attempt", "partial_review", "idempotent_replay"]) || !record(value.task) || !record(value.attempt)
    || value.task.task_id !== task.task_id || value.task.owner_principal_id !== task.owner_principal_id || value.task.owner_session_id !== task.owner_session_id
    || value.task.status !== task.status || value.task.task_revision !== request.expected_revision
    || value.attempt.attempt_id !== request.partial_decision.attempt_id || value.attempt.workflow_run_id !== request.partial_decision.workflow_run_id
    || value.attempt.ended_at != null || value.attempt.fencing_token !== task.latest_attempt?.fencing_token
    || latest?.fencing_token !== task.latest_attempt?.fencing_token || latest?.ended_at != null || latest?.attempt_id !== request.partial_decision.attempt_id
    || latest?.workflow_run_id !== request.partial_decision.workflow_run_id || typeof value.idempotent_replay !== "boolean") {
    throw Error("Partial acceptance receipt is unconfirmed. Inspect the original task before another decision; debt remains unresolved.");
  }
  const partial = validateSpecialistPartialReview(value.partial_review);
  if (partial.idempotency_key !== request.partial_decision.idempotency_key
    || JSON.stringify(partial.selected_step_ids) !== JSON.stringify(request.partial_decision.selected_step_ids)) {
    throw Error("Partial acceptance receipt does not match the exact decision. Reconcile the original request.");
  }
  return partial;
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
