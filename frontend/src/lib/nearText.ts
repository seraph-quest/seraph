import { API_URL } from "../config/constants";
import { apiFetch } from "./api";
import { NEAR_TEXT_API_BASE, NEAR_TEXT_MODEL, NEAR_TEXT_PROFILE, normalizeModelFabricSettings } from "./modelFabric";
import type { NearTextSetupStatus } from "./modelFabric";
import type { GoalInfo, WorkBoardTask, WorkBoardTaskCreateRequest } from "../types";

export const NEAR_TEXT_CAPABILITY = "inference.near-text.v1";
interface NearTextInputArtifactCreateRequest {
  schema_version: 1; capability_id: typeof NEAR_TEXT_CAPABILITY;
  goal_id: string; goal_revision: number; idempotency_key: string;
  input: { schema_version: "seraph.near.text.input.v1"; question: string; max_output_tokens: number };
}
export interface NearTextReceipt {
  schema_version: "seraph.near.text.receipt.v1";
  task_id: string; attempt_id: string; job_id: string; request_id: string; operation_id: string;
  provider: "near"; profile_id: typeof NEAR_TEXT_PROFILE; model_id: typeof NEAR_TEXT_MODEL; api_base: typeof NEAR_TEXT_API_BASE;
  tls_transport: true; tee_verified: false; e2ee: false;
  input_digest: string; output_digest: string; policy_digest: string; billing_response_digest: string; provider_request_id: string;
  cost_source: "near_billing_costs"; cost_nano_usd: number; cost_microusd: number;
  cost_state: "settled"; cost_reference: string; memory_status: "no_learning";
}
export interface NearTextOutput {
  schema_version: "seraph.near.text.output.v1";
  task_id: string; attempt_id: string; job_id: string; text: string; receipt: NearTextReceipt; no_learning: true;
}
const record = (value: unknown): value is Record<string, unknown> => Boolean(value && typeof value === "object" && !Array.isArray(value));
const id = (value: unknown): value is string => typeof value === "string" && /^[A-Za-z0-9_.:-]{1,256}$/.test(value);
const sha = (value: unknown): value is string => typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
const bytes = (value: string) => new TextEncoder().encode(value).byteLength;
const amount = (value: unknown, max: number): value is number => typeof value === "number" && Number.isSafeInteger(value) && value >= 0 && value <= max;

export function validateNearQuestion(question: string, maxOutputTokens: string | number, configuredCap: number) {
  if (!question.trim() || bytes(question) > 8192) throw new Error("Enter a question of 1–8192 UTF-8 bytes.");
  const tokens = Number(maxOutputTokens);
  if (!String(maxOutputTokens).trim() || !Number.isSafeInteger(tokens) || tokens < 1 || tokens > 1024 || tokens > configuredCap) throw new Error("Answer tokens must be a whole number within the current configured maximum.");
  return { schema_version: "seraph.near.text.input.v1" as const, question, max_output_tokens: tokens };
}
export function nearTextGoalEligible(goal: GoalInfo, ownerSessionId: string | null | undefined) {
  return Boolean(ownerSessionId && goal.owner_session_id === ownerSessionId && goal.status === "active"
    && goal.ownership_access !== "recovered_read_only" && Number.isSafeInteger(goal.revision) && (goal.revision ?? 0) >= 1);
}
async function jsonRequest(path: string, body?: unknown, signal?: AbortSignal) {
  const response = await apiFetch(API_URL + path, { signal, ...(body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  }) });
  if (!response.ok) throw new Error("The request was not confirmed (HTTP " + response.status + "). Inspect Work and the current Goal approval before creating another request.");
  const text = await response.text();
  if (bytes(text) > 256 * 1024) throw new Error("The response exceeded its safe read limit.");
  return JSON.parse(text) as unknown;
}
export async function readNearTextSetup(signal?: AbortSignal): Promise<NearTextSetupStatus | null> {
  const settings = normalizeModelFabricSettings(await jsonRequest("/api/settings/model-fabric", undefined, signal));
  if (!settings || settings.near_text_metadata_unavailable) throw new Error("Current NEAR settings are unavailable. Configure or refresh settings before submitting.");
  return settings.near_text ?? null;
}
export async function createNearTextTask({ goal, question, maxOutputTokens, configuredCap, ownerPrincipalId, ownerSessionId, signal }: {
  goal: GoalInfo; question: string; maxOutputTokens: string | number; configuredCap: number;
  ownerPrincipalId: string; ownerSessionId: string; signal?: AbortSignal;
}): Promise<WorkBoardTask> {
  if (!nearTextGoalEligible(goal, ownerSessionId) || !goal.revision || !ownerPrincipalId) throw new Error("Choose an active current owned Goal with finite approval for this request.");
  const input = validateNearQuestion(question, maxOutputTokens, configuredCap);
  const artifactRequest: NearTextInputArtifactCreateRequest = {
    schema_version: 1, capability_id: NEAR_TEXT_CAPABILITY, goal_id: goal.id, goal_revision: goal.revision,
    idempotency_key: crypto.randomUUID(), input,
  };
  const artifact = await jsonRequest("/api/work-board/input-artifacts", artifactRequest, signal);
  if (!record(artifact) || !id(artifact.artifact_id) || artifact.capability_id !== NEAR_TEXT_CAPABILITY
    || artifact.goal_id !== goal.id || artifact.goal_revision !== goal.revision || !sha(artifact.typed_input_digest)) throw new Error("Private input receipt could not be confirmed. Inspect Work before submitting another request.");
  const taskRequest: WorkBoardTaskCreateRequest = {
    title: "NEAR text question", capability_id: NEAR_TEXT_CAPABILITY, goal_id: goal.id,
    goal_revision: goal.revision, status: "todo", input_artifact_id: artifact.artifact_id,
    idempotency_key: crypto.randomUUID(),
  };
  const result = await jsonRequest("/api/work-board/tasks", taskRequest, signal);
  if (!record(result) || !record(result.task) || !id(result.task.task_id) || result.task.capability_id !== NEAR_TEXT_CAPABILITY
    || result.task.title !== "NEAR text question" || result.task.body !== ""
    || result.task.input_artifact_id !== artifact.artifact_id || result.task.goal_id !== goal.id
    || result.task.goal_revision !== goal.revision || result.task.owner_principal_id !== ownerPrincipalId
    || result.task.owner_session_id !== ownerSessionId) throw new Error("Task receipt could not be confirmed. Inspect Work before submitting another request.");
  return result.task as unknown as WorkBoardTask;
}

export function nearTextOutputEligible(task: WorkBoardTask) {
  return task.capability_id === NEAR_TEXT_CAPABILITY && ["review", "done"].includes(task.status)
    && task.ownership_access !== "recovered_read_only" && !task.block_kind && !task.block_reason
    && task.readback_status === "verified" && task.verification_status === "passed"
    && Boolean(task.latest_attempt?.attempt_id && task.latest_attempt.workflow_run_id
      && task.latest_attempt.outcome === "verified" && task.latest_attempt.readback_status === "verified");
}
export async function readNearTextOutput(task: WorkBoardTask, signal?: AbortSignal): Promise<NearTextOutput> {
  if (!nearTextOutputEligible(task)) throw new Error("A settled charge and current successful local readback are required before showing an answer.");
  const value = await jsonRequest("/api/work-board/tasks/" + encodeURIComponent(task.task_id) + "/near-text/output", undefined, signal);
  if (!record(value) || value.schema_version !== "seraph.near.text.output.v1" || value.task_id !== task.task_id
    || value.attempt_id !== task.latest_attempt?.attempt_id || !id(value.job_id) || value.job_id !== task.latest_attempt?.workflow_run_id || value.no_learning !== true
    || typeof value.text !== "string" || bytes(value.text) < 1 || bytes(value.text) > 64 * 1024 || !record(value.receipt)) throw new Error("Answer readback did not match the current task and attempt.");
  const r = value.receipt;
  const fields = ["schema_version", "task_id", "attempt_id", "job_id", "request_id", "operation_id", "provider", "profile_id", "model_id", "api_base", "tls_transport", "tee_verified", "e2ee", "input_digest", "output_digest", "policy_digest", "billing_response_digest", "provider_request_id", "cost_source", "cost_nano_usd", "cost_microusd", "cost_state", "cost_reference", "memory_status"];
  if (Object.keys(r).some(key => !fields.includes(key))) throw new Error("Answer receipt contains unexpected metadata.");
  if (r.schema_version !== "seraph.near.text.receipt.v1" || r.task_id !== value.task_id || r.attempt_id !== value.attempt_id
    || r.job_id !== value.job_id || !id(r.request_id) || !id(r.operation_id) || r.provider !== "near"
    || r.profile_id !== NEAR_TEXT_PROFILE || r.model_id !== NEAR_TEXT_MODEL || r.api_base !== NEAR_TEXT_API_BASE
    || r.tls_transport !== true || r.tee_verified !== false || r.e2ee !== false
    || !sha(r.input_digest) || r.input_digest !== task.typed_input_digest || !sha(r.output_digest) || !sha(r.policy_digest) || !sha(r.billing_response_digest)
    || typeof r.provider_request_id !== "string" || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(r.provider_request_id)
    || r.cost_source !== "near_billing_costs" || r.cost_state !== "settled"
    || !amount(r.cost_nano_usd, 1_000_000_000_000) || !amount(r.cost_microusd, 1_000_000_000)
    || Math.ceil(r.cost_nano_usd / 1000) !== r.cost_microusd || !id(r.cost_reference)
    || r.memory_status !== "no_learning") throw new Error("Answer receipt lacks a matching settled charge or current local readback.");
  return value as unknown as NearTextOutput;
}
