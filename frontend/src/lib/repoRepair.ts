import { API_URL } from "../config/constants";
import { apiFetch } from "./api";
import type {
  WorkBoardTask,
  WorkBoardTaskCreateRequest,
} from "../types";

export const REPO_REPAIR_CAPABILITY = "engineering.repo-repair.v1" as const;

const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const REQUEST_TIMEOUT_MS = 15_000;

export interface RepoRepairInput {
  repository_path: string;
  problem_statement: string;
  acceptance_criteria: string[];
  source_paths: string[];
  allowed_paths: string[];
  test_args: string[];
  evidence_refs: string[];
}

export interface RepoRepairInputArtifactCreateRequest {
  schema_version: 1;
  capability_id: typeof REPO_REPAIR_CAPABILITY;
  goal_id: string;
  goal_revision: number;
  input: RepoRepairInput;
  idempotency_key: string;
}

export interface RepoRepairInputArtifactResponse {
  artifact_id: string;
  typed_input_ref: string;
  typed_input_digest: string;
  capability_id: typeof REPO_REPAIR_CAPABILITY;
  goal_id: string;
  goal_revision: number;
  expires_at: string;
}

export interface RepoRepairTaskResponse {
  task: WorkBoardTask;
  idempotent_replay: boolean;
}

export interface RepoRepairErrorDetail {
  code?: string;
  message?: string;
  recovery?: string;
}

export class RepoRepairApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly recovery: string | null;

  constructor(status: number, code: string, message: string, recovery: string | null = null) {
    super(message);
    this.name = "RepoRepairApiError";
    this.status = status;
    this.code = code;
    this.recovery = recovery;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function requiredString(value: unknown, field: string): string {
  if (typeof value !== "string" || !value.trim()) {
    throw new RepoRepairApiError(200, "receipt_invalid", `The repair receipt is missing ${field}.`);
  }
  return value;
}

function requiredPositiveInteger(value: unknown, field: string): number {
  if (!Number.isSafeInteger(value) || Number(value) < 1) {
    throw new RepoRepairApiError(200, "receipt_invalid", `The repair receipt has an invalid ${field}.`);
  }
  return Number(value);
}

function requiredDigest(value: unknown, field: string): string {
  const digest = requiredString(value, field);
  if (!SHA256_PATTERN.test(digest)) {
    throw new RepoRepairApiError(200, "receipt_invalid", `The repair receipt has an invalid ${field}.`);
  }
  return digest;
}

function requiredTimestamp(value: unknown, field: string): string {
  const timestamp = requiredString(value, field);
  if (!Number.isFinite(Date.parse(timestamp))) {
    throw new RepoRepairApiError(200, "receipt_invalid", `The repair receipt has an invalid ${field}.`);
  }
  return timestamp;
}

function safeErrorDetail(payload: unknown): RepoRepairErrorDetail {
  if (!isRecord(payload)) return {};
  const detail = payload.detail;
  if (typeof detail === "string") return { message: detail };
  if (!isRecord(detail)) return {};
  return {
    code: typeof detail.code === "string" ? detail.code : undefined,
    message: typeof detail.message === "string" ? detail.message : undefined,
    recovery: typeof detail.recovery === "string" ? detail.recovery : undefined,
  };
}

/**
 * Request the repair producer boundary with a deadline covering both fetch and
 * response-body parsing. The exact caller signal remains authoritative.
 */
export async function repoRepairRequest<T>(
  path: string,
  init: RequestInit = {},
  parentSignal?: AbortSignal,
): Promise<T> {
  const controller = new AbortController();
  const abortFromParent = () => controller.abort();
  if (parentSignal?.aborted) controller.abort();
  else parentSignal?.addEventListener("abort", abortFromParent, { once: true });

  let timedOut = false;
  let timeout: number | undefined;
  let operation: Promise<T>;
  const operationPromise = (async () => {
    const response = await apiFetch(`${API_URL}/api/work-board${path}`, {
      ...init,
      signal: controller.signal,
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      const detail = safeErrorDetail(payload);
      const code = detail.code || `http_${response.status}`;
      const message = detail.message?.trim() || `The repair request failed (${code}).`;
      throw new RepoRepairApiError(response.status, code, message.slice(0, 500), detail.recovery?.slice(0, 500) ?? null);
    }
    return payload as T;
  })();
  operation = operationPromise;
  const deadline = new Promise<T>((_, reject) => {
    timeout = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
      reject(new RepoRepairApiError(408, "request_timeout", "The repair request exceeded its deadline; its exact request is retained for reconciliation."));
    }, REQUEST_TIMEOUT_MS);
  });
  const cancelled = new Promise<T>((_, reject) => {
    controller.signal.addEventListener("abort", () => {
      if (!timedOut) reject(new DOMException("The repair request was cancelled.", "AbortError"));
    }, { once: true });
  });
  try {
    return await Promise.race([operation, deadline, cancelled]);
  } finally {
    if (timeout !== undefined) window.clearTimeout(timeout);
    parentSignal?.removeEventListener("abort", abortFromParent);
    void operationPromise.catch(() => undefined);
  }
}

export function validateRepoRepairInputArtifactResponse(payload: unknown): RepoRepairInputArtifactResponse {
  if (!isRecord(payload)) {
    throw new RepoRepairApiError(200, "receipt_invalid", "The input-artifact receipt was not an object.");
  }
  const capabilityId = requiredString(payload.capability_id, "capability");
  if (capabilityId !== REPO_REPAIR_CAPABILITY) {
    throw new RepoRepairApiError(200, "receipt_invalid", "The input-artifact receipt has an unexpected capability.");
  }
  return {
    artifact_id: requiredString(payload.artifact_id, "artifact ID"),
    typed_input_ref: requiredString(payload.typed_input_ref, "typed input reference"),
    typed_input_digest: requiredDigest(payload.typed_input_digest, "typed input digest"),
    capability_id: REPO_REPAIR_CAPABILITY,
    goal_id: requiredString(payload.goal_id, "goal ID"),
    goal_revision: requiredPositiveInteger(payload.goal_revision, "goal revision"),
    expires_at: requiredTimestamp(payload.expires_at, "expiry"),
  };
}

export function validateRepoRepairTaskResponse(
  payload: unknown,
  expected: {
    goalId: string;
    goalRevision: number;
    artifactId: string;
    artifactDigest: string;
    ownerPrincipalId: string;
    ownerSessionId: string;
  },
): RepoRepairTaskResponse {
  if (!isRecord(payload) || !isRecord(payload.task)) {
    throw new RepoRepairApiError(200, "receipt_invalid", "The task receipt did not include a task object.");
  }
  const task = payload.task;
  if (requiredString(task.capability_id, "task capability") !== REPO_REPAIR_CAPABILITY
    || requiredString(task.task_id, "task ID") === ""
    || requiredString(task.input_artifact_id, "input artifact ID") !== expected.artifactId
    || requiredDigest(task.typed_input_digest, "task input digest") !== expected.artifactDigest
    || requiredString(task.owner_principal_id, "task owner") !== expected.ownerPrincipalId
    || requiredString(task.owner_session_id, "task owner session") !== expected.ownerSessionId
    || requiredString(task.goal_id, "task goal ID") !== expected.goalId
    || requiredPositiveInteger(task.goal_revision, "task goal revision") !== expected.goalRevision
    || task.status !== "todo") {
    throw new RepoRepairApiError(200, "receipt_invalid", "The task receipt does not match the requested goal, artifact, capability, status, or owner.");
  }
  if (typeof payload.idempotent_replay !== "boolean") {
    throw new RepoRepairApiError(200, "receipt_invalid", "The task receipt is missing its replay status.");
  }
  return { task: task as unknown as WorkBoardTask, idempotent_replay: payload.idempotent_replay };
}

export function createRepoRepairInputArtifact(
  request: RepoRepairInputArtifactCreateRequest,
  signal?: AbortSignal,
): Promise<RepoRepairInputArtifactResponse> {
  return repoRepairRequest<unknown>("/input-artifacts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  }, signal).then(validateRepoRepairInputArtifactResponse);
}

export function createRepoRepairTask(
  request: WorkBoardTaskCreateRequest,
  expected: Parameters<typeof validateRepoRepairTaskResponse>[1],
  signal?: AbortSignal,
): Promise<RepoRepairTaskResponse> {
  return repoRepairRequest<unknown>("/tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  }, signal).then((payload) => validateRepoRepairTaskResponse(payload, expected));
}
