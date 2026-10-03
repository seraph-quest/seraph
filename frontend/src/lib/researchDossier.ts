import { API_URL } from "../config/constants";
import { apiFetch } from "./api";
import type { WorkBoardTask } from "../types";

export const RESEARCH_CAPABILITY = "work.research-dossier.v1";
export type ResearchSource = { kind: "public_https_text"; url: string; first_line: number; last_line: number }
  | { kind: "completed_board_artifact"; producer_task_ref: string; producer_attempt_ref: string; source_sha256: string; first_line: number; last_line: number };
export interface ResearchInput {
  schema_version: 1; question: string;
  perspectives: { instruction: string; source_slots: number[] }[];
  sources: ResearchSource[]; source_egress_acknowledged: true; no_learning: true;
}
interface ArtifactRequest { schema_version: 1; capability_id: typeof RESEARCH_CAPABILITY; goal_id: string; goal_revision: number; input: ResearchInput; idempotency_key: string }
interface TaskRequest { title: string; goal_id: string; goal_revision: number; status: "todo"; capability_id: typeof RESEARCH_CAPABILITY; idempotency_key: string }
export type ResearchPending = { kind: "create"; artifact: ArtifactRequest; task: TaskRequest; artifact_id: string | null }
  | { kind: "control"; task_id: string; action: "recover" | "cancel"; body: { expected_revision: number; idempotency_key: string } };
export interface ResearchState {
  task_id: string; task_revision: number; attempt_id: string; parent_id: string; status: string; phase: string | null;
  deadline_at: string; creation_digest: string | null; recoverable: boolean; cancel_available: boolean; report_available: boolean;
  children: { job_id: string; status: string; reason: string | null; attempt_count: number; lease_present: boolean }[];
  costs: { operation_id: string; job_id: string; state: string; bound_microusd: number; actual_cost_microusd: number | null; contact_started: boolean; reason: string | null }[];
  no_learning: true; semantic_truth_verified: false; recovery_limit: string;
}
const bytes = (v: string) => new TextEncoder().encode(v).length;
const record = (v: unknown): v is Record<string, unknown> => Boolean(v) && typeof v === "object" && !Array.isArray(v);
const exact = (v: Record<string, unknown>, names: string[]) => Object.keys(v).sort().join() === names.sort().join();
const id = (v: unknown): v is string => typeof v === "string" && /^[a-zA-Z0-9:-]{1,128}$/.test(v);
const positive = (v: unknown): v is number => typeof v === "number" && Number.isSafeInteger(v) && v > 0;
const sha = (v: unknown): v is string => typeof v === "string" && /^[a-f0-9]{64}$/.test(v);
const bounded = (v: unknown, max: number): v is string => typeof v === "string" && bytes(v) > 0 && bytes(v) <= max;

export function validateResearchInput(value: unknown): ResearchInput {
  if (!record(value) || !exact(value, ["schema_version", "question", "perspectives", "sources", "source_egress_acknowledged", "no_learning"])
    || value.schema_version !== 1 || value.no_learning !== true || value.source_egress_acknowledged !== true
    || !bounded(value.question, 2048) || !Array.isArray(value.sources) || value.sources.length < 1 || value.sources.length > 4
    || !Array.isArray(value.perspectives) || value.perspectives.length < 1 || value.perspectives.length > 2) throw new Error("Research input exceeds the finite reviewed schema.");
  const identities = new Set<string>();
  for (const source of value.sources) {
    if (!record(source) || !positive(source.first_line) || !positive(source.last_line) || source.last_line < source.first_line || source.last_line > 65536) throw new Error("Choose a finite source line span.");
    if (source.kind === "public_https_text") {
      if (!exact(source, ["kind", "url", "first_line", "last_line"]) || !bounded(source.url, 2048)) throw new Error("Public source input is incomplete.");
      const url = new URL(source.url);
      if (url.protocol !== "https:" || url.username || url.password || url.hash || (url.port && url.port !== "443") || /\\|%(?:2e|2f|5c)/i.test(url.pathname)) throw new Error("Sources require exact public HTTPS without credentials, redirects or ambiguous paths.");
      identities.add(source.url);
    } else if (source.kind === "completed_board_artifact") {
      if (!exact(source, ["kind", "producer_task_ref", "producer_attempt_ref", "source_sha256", "first_line", "last_line"])
        || !id(source.producer_task_ref) || !id(source.producer_attempt_ref) || !sha(source.source_sha256)) throw new Error("Completed source requires its exact task, attempt and digest.");
      identities.add(JSON.stringify([source.producer_task_ref, source.producer_attempt_ref, source.source_sha256]));
    } else throw new Error("Only explicit public text or verified completed Board evidence is permitted.");
  }
  if (identities.size !== value.sources.length) throw new Error("Declare a shared source once.");
  const used = new Set<number>();
  for (const perspective of value.perspectives) {
    if (!record(perspective) || !exact(perspective, ["instruction", "source_slots"]) || !bounded(perspective.instruction, 1024)
      || !Array.isArray(perspective.source_slots) || perspective.source_slots.length < 1 || perspective.source_slots.length > 2
      || new Set(perspective.source_slots).size !== perspective.source_slots.length) throw new Error("Each perspective needs an instruction and at most two source slots.");
    for (const slot of perspective.source_slots) {
      if (!Number.isSafeInteger(slot) || slot < 0 || slot >= value.sources.length) throw new Error("Perspective source slots must identify the declared sources.");
      used.add(slot);
    }
  }
  if (used.size !== value.sources.length) throw new Error("Assign every source to a perspective.");
  return value as unknown as ResearchInput;
}

export function researchStorageKey(principal: string, session: string, scope: string): string {
  if (!id(principal) || !id(session) || !id(scope)) throw new Error("The current owner and session are required.");
  return `seraph.research.v1:${encodeURIComponent(principal)}:${encodeURIComponent(session)}:${encodeURIComponent(scope)}`;
}

function validatePending(value: unknown): ResearchPending {
  if (!record(value)) throw new Error("Retained research request is corrupt.");
  if (value.kind === "control") {
    if (!exact(value, ["kind", "task_id", "action", "body"]) || !id(value.task_id) || !["recover", "cancel"].includes(String(value.action))
      || !record(value.body) || !exact(value.body, ["expected_revision", "idempotency_key"]) || !positive(value.body.expected_revision) || !id(value.body.idempotency_key)) throw new Error("Retained research control is corrupt.");
  } else if (value.kind === "create") {
    if (!exact(value, ["kind", "artifact", "task", "artifact_id"]) || (value.artifact_id !== null && !id(value.artifact_id))
      || !record(value.artifact) || !exact(value.artifact, ["schema_version", "capability_id", "goal_id", "goal_revision", "input", "idempotency_key"])
      || value.artifact.schema_version !== 1 || value.artifact.capability_id !== RESEARCH_CAPABILITY || !id(value.artifact.goal_id)
      || !positive(value.artifact.goal_revision) || !id(value.artifact.idempotency_key)
      || !record(value.task) || !exact(value.task, ["title", "goal_id", "goal_revision", "status", "capability_id", "idempotency_key"])
      || !bounded(value.task.title, 100) || value.task.goal_id !== value.artifact.goal_id || value.task.goal_revision !== value.artifact.goal_revision
      || value.task.status !== "todo" || value.task.capability_id !== RESEARCH_CAPABILITY || !id(value.task.idempotency_key)) throw new Error("Retained research creation is corrupt.");
    validateResearchInput(value.artifact.input);
  } else throw new Error("Retained research request has an unknown action.");
  return value as unknown as ResearchPending;
}

export function readResearchPending(key: string): ResearchPending | null {
  const raw = window.sessionStorage.getItem(key);
  if (raw === null) return null;
  if (bytes(raw) > 16384) throw new Error("Retained research request exceeds 16 KiB.");
  const pending = validatePending(JSON.parse(raw));
  const parts = key.split(":");
  const scope = decodeURIComponent(parts[parts.length-1] ?? "");
  if (scope !== (pending.kind === "create" ? "create" : pending.task_id)) throw new Error("Retained research request belongs to another task scope.");
  return pending;
}

export function retainResearchPending(key: string, pending: ResearchPending): void {
  validatePending(pending);
  const parts = key.split(":");
  const scope = decodeURIComponent(parts[parts.length-1] ?? "");
  if (scope !== (pending.kind === "create" ? "create" : pending.task_id)) throw new Error("Research request belongs to another task scope.");
  const raw = JSON.stringify(pending);
  if (bytes(raw) > 16384) throw new Error("The full retained request exceeds 16 KiB.");
  window.sessionStorage.setItem(key, raw);
  if (window.sessionStorage.getItem(key) !== raw) throw new Error("Research request retention failed; no mutation was sent.");
  readResearchPending(key);
}

export function newResearchCreation(goal: string, revision: number, title: string, input: ResearchInput): ResearchPending {
  return validatePending({ kind: "create", artifact_id: null,
    artifact: { schema_version: 1, capability_id: RESEARCH_CAPABILITY, goal_id: goal, goal_revision: revision, input: validateResearchInput(input), idempotency_key: "research-input:"+crypto.randomUUID() },
    task: { title, goal_id: goal, goal_revision: revision, status: "todo", capability_id: RESEARCH_CAPABILITY, idempotency_key: "research-task:"+crypto.randomUUID() } });
}

async function jsonRequest(path: string, body?: unknown, signal?: AbortSignal): Promise<unknown> {
  const response = await apiFetch(`${API_URL}${path}`, { signal, ...(body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {}) });
  if (!response.ok) throw new Error(`Research request requires readback (${response.status}). Retain and retry this exact request.`);
  return response.json();
}

export async function submitResearchCreation(key: string, pending: ResearchPending, signal?: AbortSignal): Promise<WorkBoardTask> {
  if (pending.kind !== "create") throw new Error("The retained request is not creation.");
  retainResearchPending(key, pending);
  if (!pending.artifact_id) {
    const result = await jsonRequest("/api/work-board/input-artifacts", pending.artifact, signal);
    if (!record(result) || !id(result.artifact_id) || !sha(result.typed_input_digest) || result.capability_id !== RESEARCH_CAPABILITY
      || result.goal_id !== pending.artifact.goal_id || result.goal_revision !== pending.artifact.goal_revision) throw new Error("Research input receipt does not bind the original request.");
    pending = { ...pending, artifact_id: result.artifact_id };
    retainResearchPending(key, pending);
  }
  const result = await jsonRequest("/api/work-board/tasks", { ...pending.task, input_artifact_id: pending.artifact_id }, signal);
  if (!record(result) || !record(result.task) || !id(result.task.task_id) || result.task.capability_id !== RESEARCH_CAPABILITY
    || result.task.input_artifact_id !== pending.artifact_id || result.task.goal_id !== pending.task.goal_id || result.task.goal_revision !== pending.task.goal_revision) throw new Error("Research task receipt does not bind the original request.");
  window.sessionStorage.removeItem(key);
  if (window.sessionStorage.getItem(key) !== null) throw new Error("Confirmed research request could not be cleared.");
  return result.task as unknown as WorkBoardTask;
}

export function validateResearchState(value: unknown, task: string): ResearchState {
  if (!record(value) || value.task_id !== task || !positive(value.task_revision) || !id(value.attempt_id)
    || typeof value.parent_id !== "string" || value.parent_id.length > 256 || value.no_learning !== true || value.semantic_truth_verified !== false
    || typeof value.status !== "string" || typeof value.deadline_at !== "string" || !Number.isFinite(Date.parse(value.deadline_at))
    || !Array.isArray(value.children) || value.children.length < 1 || value.children.length > 2 || !Array.isArray(value.costs) || value.costs.length > 2
    || typeof value.recoverable !== "boolean" || typeof value.cancel_available !== "boolean" || typeof value.report_available !== "boolean"
    || typeof value.recovery_limit !== "string") throw new Error("Research state readback is incomplete.");
  for (const child of value.children) if (!record(child) || !bounded(child.job_id, 256)
    || !bounded(child.status, 64) || (child.reason !== null && !bounded(child.reason, 256))
    || typeof child.lease_present !== "boolean" || typeof child.attempt_count !== "number" || !Number.isSafeInteger(child.attempt_count)
    || child.attempt_count < 0 || child.attempt_count > 1) throw new Error("Research child readback is incomplete.");
  for (const cost of value.costs) if (!record(cost) || !bounded(cost.operation_id, 256) || !bounded(cost.job_id, 256)
    || !bounded(cost.state, 64) || !positive(cost.bound_microusd)
    || (cost.actual_cost_microusd !== null && (typeof cost.actual_cost_microusd !== "number" || !Number.isSafeInteger(cost.actual_cost_microusd) || cost.actual_cost_microusd < 0))
    || typeof cost.contact_started !== "boolean" || (cost.reason !== null && !bounded(cost.reason, 256))) throw new Error("Research cost readback is incomplete.");
  return value as unknown as ResearchState;
}

export async function readResearchState(task: string, signal?: AbortSignal): Promise<ResearchState> {
  if (!id(task)) throw new Error("Invalid research task identity.");
  return validateResearchState(await jsonRequest(`/api/work-board/tasks/${task}/research`, undefined, signal), task);
}

export async function submitResearchControl(key: string, pending: ResearchPending, signal?: AbortSignal): Promise<ResearchState> {
  if (pending.kind !== "control") throw new Error("The retained request is not research control.");
  retainResearchPending(key, pending);
  const result = await jsonRequest(`/api/work-board/tasks/${pending.task_id}/research/${pending.action}`, pending.body, signal);
  if (!record(result)) throw new Error("Research control readback is incomplete.");
  const state = validateResearchState(result.research, pending.task_id);
  const receipt = result[pending.action === "recover" ? "recovery" : "cancellation"];
  if (record(receipt) && receipt.completed === true) window.sessionStorage.removeItem(key);
  return state;
}

export async function readResearchReport(task: string, signal?: AbortSignal): Promise<string> {
  if (!id(task)) throw new Error("Invalid research task identity.");
  const response = await apiFetch(`${API_URL}/api/work-board/tasks/${task}/research-report`, { signal });
  if (!response.ok || !response.headers.get("content-type")?.startsWith("text/plain")) throw new Error("Verified literal research dossier unavailable.");
  const text = await response.text();
  if (bytes(text) > 65536) throw new Error("Research dossier exceeds its finite allowance.");
  return text;
}
