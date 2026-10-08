import { API_URL } from "../config/constants";
import { apiFetch } from "./api";
import type {
  WorkBoardInputArtifactCreateRequest,
  WorkBoardInputArtifactResponse,
  WorkBoardTask,
  WorkBoardTaskCreateRequest,
} from "../types";

export interface BrowserTaskErrorDetail {
  code?: string;
  message?: string;
  recovery?: string;
}

export class BrowserTaskApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly recovery: string | null;

  constructor(status: number, code: string, message: string, recovery: string | null = null) {
    super(message);
    this.name = "BrowserTaskApiError";
    this.status = status;
    this.code = code;
    this.recovery = recovery;
  }
}

const BROWSER_CAPABILITY_ID = "browser.public-task.v1" as const;
const SHA256_PATTERN = /^[0-9a-f]{64}$/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function requiredString(value: unknown, field: string): string {
  if (typeof value !== "string" || !value.trim()) {
    throw new BrowserTaskApiError(200, "receipt_invalid", `The browser receipt is missing ${field}.`);
  }
  return value;
}

function requiredPositiveInteger(value: unknown, field: string): number {
  if (!Number.isSafeInteger(value) || Number(value) < 1) {
    throw new BrowserTaskApiError(200, "receipt_invalid", `The browser receipt has an invalid ${field}.`);
  }
  return Number(value);
}

function requiredDigest(value: unknown, field: string): string {
  const digest = requiredString(value, field);
  if (!SHA256_PATTERN.test(digest)) {
    throw new BrowserTaskApiError(200, "receipt_invalid", `The browser receipt has an invalid ${field}.`);
  }
  return digest;
}

function requiredTimestamp(value: unknown, field: string): string {
  const timestamp = requiredString(value, field);
  if (!Number.isFinite(Date.parse(timestamp))) {
    throw new BrowserTaskApiError(200, "receipt_invalid", `The browser receipt has an invalid ${field}.`);
  }
  return timestamp;
}

/** Validate the fields that bind a 2xx artifact receipt to the requested capability. */
export function validateBrowserInputArtifactResponse(payload: unknown): WorkBoardInputArtifactResponse {
  if (!isRecord(payload)) {
    throw new BrowserTaskApiError(200, "receipt_invalid", "The input-artifact receipt was not an object.");
  }
  const capabilityId = requiredString(payload.capability_id, "capability");
  if (capabilityId !== BROWSER_CAPABILITY_ID) {
    throw new BrowserTaskApiError(200, "receipt_invalid", "The input-artifact receipt has an unexpected capability.");
  }
  return {
    ...(payload as unknown as WorkBoardInputArtifactResponse),
    artifact_id: requiredString(payload.artifact_id, "artifact ID"),
    typed_input_ref: requiredString(payload.typed_input_ref, "typed input reference"),
    typed_input_digest: requiredDigest(payload.typed_input_digest, "typed input digest"),
    capability_id: BROWSER_CAPABILITY_ID,
    goal_id: requiredString(payload.goal_id, "goal ID"),
    goal_revision: requiredPositiveInteger(payload.goal_revision, "goal revision"),
    expires_at: requiredTimestamp(payload.expires_at, "expiry").trim(),
  };
}

/** Validate the task identity and artifact binding before reporting creation to the operator. */
export function validateBrowserTaskResponse(
  payload: unknown,
): { task: WorkBoardTask; idempotent_replay: boolean } {
  if (!isRecord(payload) || !isRecord(payload.task)) {
    throw new BrowserTaskApiError(200, "receipt_invalid", "The task receipt did not include a task object.");
  }
  const task = payload.task;
  const capabilityId = requiredString(task.capability_id, "task capability");
  if (capabilityId !== BROWSER_CAPABILITY_ID) {
    throw new BrowserTaskApiError(200, "receipt_invalid", "The task receipt has an unexpected capability.");
  }
  requiredString(task.task_id, "task ID");
  requiredString(task.title, "task title");
  requiredString(task.goal_id, "task goal ID");
  requiredPositiveInteger(task.goal_revision, "task goal revision");
  requiredString(task.input_artifact_id, "input artifact ID");
  requiredString(task.owner_principal_id, "task owner");
  requiredString(task.owner_session_id, "task owner session");
  if (typeof payload.idempotent_replay !== "boolean") {
    throw new BrowserTaskApiError(200, "receipt_invalid", "The task receipt is missing its replay status.");
  }
  return { task: task as unknown as WorkBoardTask, idempotent_replay: payload.idempotent_replay };
}

function safeErrorDetail(payload: unknown): BrowserTaskErrorDetail {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return {};
  const detail = (payload as { detail?: unknown }).detail;
  if (typeof detail === "string") return { message: detail };
  if (!detail || typeof detail !== "object" || Array.isArray(detail)) return {};
  const value = detail as BrowserTaskErrorDetail;
  return {
    code: typeof value.code === "string" ? value.code : undefined,
    message: typeof value.message === "string" ? value.message : undefined,
    recovery: typeof value.recovery === "string" ? value.recovery : undefined,
  };
}

export async function browserTaskRequest<T>(path: string, init: RequestInit): Promise<T> {
  const response = await apiFetch(`${API_URL}/api/work-board${path}`, init);
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = safeErrorDetail(payload);
    const code = detail.code || `http_${response.status}`;
    const message = detail.message?.trim() || `The browser task request failed (${code}).`;
    throw new BrowserTaskApiError(
      response.status,
      code,
      message.slice(0, 500),
      detail.recovery?.slice(0, 500) ?? null,
    );
  }
  return payload as T;
}

export function createBrowserInputArtifact(
  request: WorkBoardInputArtifactCreateRequest,
  signal?: AbortSignal,
): Promise<WorkBoardInputArtifactResponse> {
  return browserTaskRequest<unknown>("/input-artifacts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
    signal,
  }).then(validateBrowserInputArtifactResponse);
}

export function createBrowserWorkBoardTask(
  request: WorkBoardTaskCreateRequest,
  signal?: AbortSignal,
): Promise<{ task: WorkBoardTask; idempotent_replay: boolean }> {
  return browserTaskRequest<unknown>("/tasks", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
    signal,
  }).then(validateBrowserTaskResponse);
}
