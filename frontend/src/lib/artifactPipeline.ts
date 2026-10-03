import { API_URL } from "../config/constants";
import { apiFetch } from "./api";

export interface ArtifactPipeline {
  operation_id: string; revision: number; parent_revision: number; digest: string;
  status: "proposed" | "accepted"; plan_version: number; deadline_at: string | null;
  steps: { slot: string; capability_id: string; task_id: string; task_revision: number; status: string; block_reason: string | null }[];
  source_scope: { start_url: string; allowed_hosts: string[]; approved_url_prefixes: string[] };
  pending_revision: Record<string, unknown> | null;
  reused_output: Record<string, unknown> | null;
  authority_frozen?: Record<string, unknown> | null;
  no_learning: true;
}

export interface PipelinePending { path: string; body: Record<string, unknown> }
export interface PipelineStorage { schema_version: 1; operation_id: string | null; pending: PipelinePending | null }
const MAX_BYTES = 16 * 1024;
const identifier = /^[a-zA-Z0-9:-]{1,128}$/;
const safeId = (value: unknown): value is string => typeof value === "string" && identifier.test(value);
const positive = (v: unknown): v is number => typeof v === "number" && Number.isSafeInteger(v) && v > 0;
const record = (v: unknown): v is Record<string, unknown> => Boolean(v) && typeof v === "object" && !Array.isArray(v);

export function pipelineStorageKey(principal: string, session: string, task: string): string {
  return `seraph.artifact-pipeline.v1:${encodeURIComponent(principal)}:${encodeURIComponent(session)}:${encodeURIComponent(task)}`;
}

function validatePending(value: unknown, task: string, operation: string | null): value is PipelinePending {
  if (!record(value) || Object.keys(value).sort().join() !== "body,path" || typeof value.path !== "string" || !record(value.body)) return false;
  const keys = Object.keys(value.body).sort().join();
  if (!positive(value.body.expected_revision)) return false;
  if (value.path === `/api/work-board/tasks/${task}/pipeline-preview`) {
    return keys === "expected_revision,idempotency_key,source_input_artifact_id" && safeId(value.body.source_input_artifact_id) && safeId(value.body.idempotency_key);
  }
  if (!operation) return false;
  const prefix = `/api/work-board/pipelines/${operation}/`;
  if (value.path === `${prefix}accept`) return keys === "expected_digest,expected_parent_revision,expected_revision" && positive(value.body.expected_parent_revision) && /^[a-f0-9]{64}$/.test(String(value.body.expected_digest));
  if (value.path === `${prefix}advance` || value.path === `${prefix}quiesce`) return keys === "expected_revision";
  if (value.path === `${prefix}revision`) return keys === "expected_revision,idempotency_key,source_input_artifact_id" && safeId(value.body.source_input_artifact_id) && safeId(value.body.idempotency_key);
  if (value.path === `${prefix}reuse-preview`) return keys === "expected_parent_revision,expected_revision,idempotency_key" && positive(value.body.expected_parent_revision) && safeId(value.body.idempotency_key);
  return false;
}

export function readPipelineStorage(key: string, task: string): PipelineStorage {
  const raw = window.sessionStorage.getItem(key);
  if (raw === null) return { schema_version: 1, operation_id: null, pending: null };
  if (new TextEncoder().encode(raw).length > MAX_BYTES) throw new Error("Retained pipeline request exceeds its allowance.");
  const value: unknown = JSON.parse(raw);
  if (!record(value) || Object.keys(value).sort().join() !== "operation_id,pending,schema_version" || value.schema_version !== 1
    || (value.operation_id !== null && (typeof value.operation_id !== "string" || !identifier.test(value.operation_id)))
    || (value.pending !== null && !validatePending(value.pending, task, value.operation_id as string | null))) throw new Error("Retained pipeline request is corrupt. No mutation can be submitted.");
  return value as unknown as PipelineStorage;
}

export function writePipelineStorage(key: string, task: string, value: PipelineStorage): void {
  const raw = JSON.stringify(value);
  if (new TextEncoder().encode(raw).length > MAX_BYTES) throw new Error("Pipeline request exceeds its allowance.");
  window.sessionStorage.setItem(key, raw);
  if (window.sessionStorage.getItem(key) !== raw) throw new Error("Pipeline request retention could not be verified.");
  readPipelineStorage(key, task);
}

export function validatePipeline(value: unknown): ArtifactPipeline {
  if (!record(value) || typeof value.operation_id !== "string" || !identifier.test(value.operation_id)
    || !positive(value.revision) || !positive(value.parent_revision) || !positive(value.plan_version)
    || typeof value.digest !== "string" || !/^[a-f0-9]{64}$/.test(value.digest)
    || !["proposed", "accepted"].includes(String(value.status)) || value.no_learning !== true
    || !Array.isArray(value.steps) || value.steps.length < 1 || value.steps.length > 3
    || !record(value.source_scope) || typeof value.source_scope.start_url !== "string"
    || !Array.isArray(value.source_scope.allowed_hosts) || !value.source_scope.allowed_hosts.every((host) => typeof host === "string")
    || !Array.isArray(value.source_scope.approved_url_prefixes) || !value.source_scope.approved_url_prefixes.every((path) => typeof path === "string")
    || (value.status === "accepted" && (value.steps.length !== 3 || typeof value.deadline_at !== "string" || !Number.isFinite(Date.parse(value.deadline_at))))) throw new Error("Pipeline readback is incomplete.");
  const capabilities = ["browser.public-task.v1", "work.evidence-dossier.v1", "work.local-evidence-report.v1"];
  for (const [index, step] of value.steps.entries()) if (!record(step) || step.capability_id !== capabilities[index]
    || typeof step.task_id !== "string" || !identifier.test(step.task_id) || !positive(step.task_revision) || typeof step.status !== "string") throw new Error("Pipeline steps differ from the fixed reviewed chain.");
  return value as unknown as ArtifactPipeline;
}

export async function pipelineRequest(path: string, pending?: PipelinePending, signal?: AbortSignal): Promise<ArtifactPipeline> {
  const response = await apiFetch(`${API_URL}${path}`, { signal, ...(pending ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(pending.body) } : {}) });
  if (!response.ok) throw new Error(`Pipeline readback unavailable (${response.status}); retain and retry the exact request after refreshing.`);
  return validatePipeline(await response.json());
}

export async function pipelineReport(operation: string, signal?: AbortSignal): Promise<string> {
  const response = await apiFetch(`${API_URL}/api/work-board/pipelines/${operation}/report`, { signal });
  if (!response.ok || !response.headers.get("Content-Type")?.startsWith("text/plain")) throw new Error("The independently verified plain-text report is unavailable.");
  const text = await response.text();
  if (new TextEncoder().encode(text).length > 65536) throw new Error("Report exceeds its finite allowance.");
  return text;
}
