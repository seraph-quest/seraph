import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent, MouseEvent } from "react";
import { createPortal } from "react-dom";

import { API_URL, WS_URL } from "../../config/constants";
import { resolveWebSocketUrl } from "../../hooks/useWebSocket";
import { apiFetch } from "../../lib/api";
import { fetchGuardianInboxItem } from "../../lib/guardianInbox";
import { BrowserTaskForm } from "./BrowserTaskForm";
import type { BrowserTaskSubmissionReceipt, PendingBrowserSubmission } from "./BrowserTaskForm";
import { CalendarPrepForm } from "./CalendarPrepForm";
import type { PendingCalendarSubmission } from "./CalendarPrepForm";
import { RepoRepairForm } from "./RepoRepairForm";
import type { PendingRepoRepairSubmission, RepoRepairSubmissionReceipt } from "./RepoRepairForm";
import { MailPanel } from "./MailPanel";
import { WorkBoardMemoryReview } from "./WorkBoardMemoryReview";
import { TaskApprovalReview } from "./TaskApprovalReview";
import { ArtifactPipelineReview } from "./ArtifactPipelineReview";
import { ResearchDossierPanel } from "./ResearchDossierPanel";
import { JsonFormatterPanel } from "./JsonFormatterPanel";
import { TaskEffectRecovery } from "./TaskEffectRecovery";
import { TaskEvidencePanel } from "./TaskEvidencePanel";
import { SpecificationEvidenceReview, specificationScope, retainSpecificationAcceptance } from "./SpecificationEvidenceReview";
import type { SpecificationReplacement } from "./SpecificationEvidenceReview";
import { RepoRepairInspector } from "./RepoRepairInspector";
import { validateCalendarExecution } from "../../lib/calendar";
import type {
  GoalInfo,
  CalendarPrepResponse,
  GuardianInboxItem,
  WorkBoardAttempt,
  WorkBoardBrowserExecution,
  CalendarExecutionProjection,
  WorkBoardActionRequest,
  WorkBoardComment,
  WorkBoardCommentCreateRequest,
  WorkBoardEvent,
  WorkBoardEventPage,
  WorkBoardExecutionLimits,
  WorkBoardLinkCreateRequest,
  WorkBoardLinkDeleteRequest,
  WorkBoardReadbackStatus,
  WorkBoardReceiptReference,
  WorkBoardRecoveryAction,
  WorkBoardProposal,
  WorkBoardRoutineBinding,
  WorkBoardRoutineInvokeRequest,
  WorkBoardRoutineInvokeReceipt,
  WorkBoardRoutinePackageApproval,
  WorkBoardRoutinePackagePreview,
  WorkBoardRoutineProcedureExport,
  WorkBoardRoutinePublicationPrepareRequest,
  WorkBoardRoutinePublicationResponse,
  WorkBoardRoutinePublicationState,
  WorkBoardRoutinePreview,
  WorkBoardRoutinePreviewRequest,
  WorkBoardRoutineRead,
  WorkBoardSourceWatch,
  WorkBoardStatus,
  WorkBoardTask,
  WorkBoardTaskCreateRequest,
  WorkBoardTaskDetail,
  WorkBoardTaskPage,
  WorkBoardTaskPatchRequest,
  WorkBoardVerificationStatus,
} from "../../types";

const BOARD_COLUMNS: WorkBoardStatus[] = [
  "triage",
  "todo",
  "ready",
  "running",
  "blocked",
  "review",
  "done",
];

const STATUS_LABELS: Record<WorkBoardStatus, string> = {
  triage: "Triage",
  todo: "Todo",
  ready: "Ready",
  running: "Running",
  blocked: "Blocked",
  review: "Review",
  done: "Done",
  archived: "Archived",
};

const READBACK_LABELS: Record<WorkBoardReadbackStatus, string> = {
  not_started: "Not started",
  pending: "Pending",
  verified: "Verified",
  failed: "Failed",
  unknown: "Unknown",
  not_applicable: "Not applicable",
};

const VERIFICATION_LABELS: Record<WorkBoardVerificationStatus, string> = {
  not_started: "Not started",
  pending: "Pending",
  passed: "Passed",
  failed: "Failed",
  reconciliation_required: "Reconciliation required",
  cancelled: "Cancelled",
};

const RECOVERY_LABELS: Record<WorkBoardRecoveryAction, string> = {
  cancel: "Cancel active attempt",
  unblock: "Resolve block",
  retry: "Retry task",
  approve_existing_run: "Open existing approval",
  restore_prerequisite: "Restore a required capability or grant",
  configure_goal_success_criterion: "Complete the goal's success criterion, verifier, and evidence",
  reconcile_admission_binding: "Reconcile the pending job admission",
  reconcile_external_effect: "Reconcile the external effect before retrying",
  renew_review: "Renew the review window",
  prepare_routine_publication: "Prepare publication preview",
  resume_routine_publication: "Resume approved publication",
};

const TASK_LIMIT = 100;
const EVENT_LIMIT = 100;
const MAX_SYNC_PAGES = 20;
const DETAIL_REFRESH_BATCH_SIZE = 8;
const RECONNECT_DELAY_MS = 3_000;
const BOARD_REQUEST_TIMEOUT_MS = 15_000;
const ROUTINE_SOURCE_CAPABILITY = "guardian.research-watch.v1";
const ROUTINE_ACTION_CAPABILITY = "work.github-followthrough.v1";
const GUARDIAN_INBOX_SCOPE = /^guardian-inbox:([A-Za-z0-9_-]{1,128})$/;

export interface WorkBoardPanelProps {
  onOpenApprovals?: () => void;
  onOpenInboxCandidate?: (item: GuardianInboxItem) => void;
  onInspectWorkflowRun?: (workflowRunId: string, ownerSessionId: string | null) => void;
  onInspectArtifact?: (request: WorkBoardArtifactInspectRequest) => void;
  focusTaskId?: string | null;
  onFocusTaskHandled?: (taskId: string) => void;
  ownerPrincipalId?: string | null;
  ownerSessionId?: string | null;
  /** Safe task metadata link for the Library's explicit procedure source picker. */
  onSelectedTaskChange?: (task: WorkBoardTask | null) => void;
  attentionContext?: { taskId: string; approvalId?: string | null; origin: "home" | "inbox"; goalId?: string | null; threadId?: string | null } | null;
  onReturnAttention?: () => void;
  onOpenAttentionGoal?: () => void;
  onOpenAttentionThread?: () => void;
  onOpenAccounting?: () => void;
}

export interface WorkBoardArtifactInspectRequest {
  reference: WorkBoardReceiptReference;
  ownerSessionId: string | null;
  workflowRunId: string | null;
  parentWorkflowRunId: string | null;
}

interface ApiErrorBody {
  detail?: string | { code?: string; message?: string; recovery?: string };
}

class WorkBoardApiError extends Error {
  status: number;
  code: string;
  recovery: string | null;

  constructor(status: number, code: string, message: string, recovery: string | null = null) {
    super(message);
    this.name = "WorkBoardApiError";
    this.status = status;
    this.code = code;
    this.recovery = recovery;
  }
}

class WorkBoardSyncError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "WorkBoardSyncError";
  }
}

interface BoardSnapshot {
  tasks: WorkBoardTask[];
  eventCursor: number;
}

interface EventDelta {
  events: WorkBoardEvent[];
  eventCursor: number;
}

interface CreateDraft {
  title: string;
  body: string;
  goalId: string;
  goalRevision: string;
  status: "triage" | "todo";
  capabilityId: string;
  typedInputRef: string;
  typedInputDigest: string;
  executorId: string;
  assigneeId: string;
  priority: string;
  scheduledAt: string;
  requiresReview: boolean;
  reviewerId: string;
}

interface PendingTaskCreate {
  idempotencyKey: string;
  draft: CreateDraft;
  payload: WorkBoardTaskCreateRequest;
}

const pendingTaskCreates = new Map<string, PendingTaskCreate>();

const MAX_PENDING_BROWSER_SUBMISSIONS = 32;
const pendingBrowserSubmissions = new Map<string, PendingBrowserSubmission>();

const MAX_PENDING_CALENDAR_SUBMISSIONS = 32;
const pendingCalendarSubmissions = new Map<string, PendingCalendarSubmission>();

const MAX_PENDING_REPO_REPAIR_SUBMISSIONS = 32;
const pendingRepoRepairSubmissions = new Map<string, PendingRepoRepairSubmission>();

function rememberPendingBrowserSubmission(scope: string, pending: PendingBrowserSubmission): void {
  pendingBrowserSubmissions.delete(scope);
  pendingBrowserSubmissions.set(scope, pending);
  while (pendingBrowserSubmissions.size > MAX_PENDING_BROWSER_SUBMISSIONS) {
    const oldest = pendingBrowserSubmissions.keys().next().value;
    if (typeof oldest !== "string") break;
    pendingBrowserSubmissions.delete(oldest);
  }
}

function rememberPendingCalendarSubmission(scope: string, pending: PendingCalendarSubmission): void {
  pendingCalendarSubmissions.delete(scope);
  pendingCalendarSubmissions.set(scope, pending);
  while (pendingCalendarSubmissions.size > MAX_PENDING_CALENDAR_SUBMISSIONS) {
    const oldest = pendingCalendarSubmissions.keys().next().value;
    if (typeof oldest !== "string") break;
    pendingCalendarSubmissions.delete(oldest);
  }
}

function rememberPendingRepoRepairSubmission(scope: string, pending: PendingRepoRepairSubmission): void {
  pendingRepoRepairSubmissions.delete(scope);
  pendingRepoRepairSubmissions.set(scope, pending);
  while (pendingRepoRepairSubmissions.size > MAX_PENDING_REPO_REPAIR_SUBMISSIONS) {
    const oldest = pendingRepoRepairSubmissions.keys().next().value;
    if (typeof oldest !== "string") break;
    pendingRepoRepairSubmissions.delete(oldest);
  }
}

interface PendingRoutineInvocation {
  routineId: string;
  request: WorkBoardRoutineInvokeRequest;
}

interface PendingRoutineInvocationStorageRead {
  pending: PendingRoutineInvocation | null;
  error: string | null;
}

interface StoredPendingRoutineInvocation {
  routine_id: string;
  request: WorkBoardRoutineInvokeRequest;
}

const ROUTINE_INVOCATION_STORAGE_PREFIX = "seraph.work-board.routine-invocation.v1";

function routineInvocationStorageKey(
  ownerPrincipalId: string | null | undefined,
  ownerSessionId: string | null | undefined,
): string | null {
  if (!ownerPrincipalId || !ownerSessionId) return null;
  return `${ROUTINE_INVOCATION_STORAGE_PREFIX}:${encodeURIComponent(ownerPrincipalId)}:${encodeURIComponent(ownerSessionId)}`;
}

function routineInvocationStorage(): Storage | null {
  try {
    if (typeof window === "undefined" || typeof window.sessionStorage === "undefined") return null;
    const storage = window.sessionStorage;
    if (typeof storage.getItem !== "function"
      || typeof storage.setItem !== "function"
      || typeof storage.removeItem !== "function") return null;
    return storage;
  } catch {
    return null;
  }
}

function isSafeRoutineStorageString(value: unknown, maxLength = 256): value is string {
  return typeof value === "string"
    && value.length > 0
    && value.length <= maxLength
    && value === value.trim()
    && !/[\u0000-\u001f\u007f]/.test(value);
}

function isSafeRoutineInvocationRequest(value: unknown): value is WorkBoardRoutineInvokeRequest {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const request = value as Partial<WorkBoardRoutineInvokeRequest>;
  return isSafeRoutineStorageString(request.invocation_uuid)
    && isSafeRoutineStorageString(request.goal_id)
    && isSafeRoutineStorageString(request.source_watch_id)
    && typeof request.version === "number"
    && Number.isSafeInteger(request.version)
    && request.version >= 1
    && typeof request.expected_routine_revision === "number"
    && Number.isSafeInteger(request.expected_routine_revision)
    && request.expected_routine_revision >= 1
    && typeof request.expected_goal_revision === "number"
    && Number.isSafeInteger(request.expected_goal_revision)
    && request.expected_goal_revision >= 1
    && typeof request.expected_watch_revision === "number"
    && Number.isSafeInteger(request.expected_watch_revision)
    && request.expected_watch_revision >= 1;
}

function isPendingRoutineInvocation(value: unknown): value is StoredPendingRoutineInvocation {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const pending = value as { routine_id?: unknown; request?: unknown };
  return isSafeRoutineStorageString(pending.routine_id)
    && isSafeRoutineInvocationRequest(pending.request);
}

function readPendingRoutineInvocation(storageKey: string | null): PendingRoutineInvocationStorageRead {
  if (!storageKey) return { pending: null, error: null };
  const storage = routineInvocationStorage();
  if (!storage) {
    return {
      pending: null,
      error: "Browser session storage is unavailable. Invocation is blocked until the exact retry request can be persisted safely.",
    };
  }
  try {
    const raw = storage.getItem(storageKey);
    if (raw === null) return { pending: null, error: null };
    const parsed: unknown = JSON.parse(raw);
    if (!isPendingRoutineInvocation(parsed)) {
      return {
        pending: null,
        error: "The saved routine invocation record is invalid. Do not retry until the operator session storage is restored.",
      };
    }
    return {
      pending: { routineId: parsed.routine_id, request: parsed.request },
      error: null,
    };
  } catch {
    return {
      pending: null,
      error: "The saved routine invocation record could not be read. Invocation is blocked until the exact retry request is available.",
    };
  }
}

function persistPendingRoutineInvocation(storageKey: string | null, pending: PendingRoutineInvocation): string | null {
  if (!storageKey) return "The authenticated owner session is unavailable, so the exact invocation request cannot be persisted.";
  const storage = routineInvocationStorage();
  if (!storage) return "Browser session storage is unavailable. Invocation is blocked until the exact retry request can be persisted safely.";
  const encoded = JSON.stringify({ routine_id: pending.routineId, request: pending.request });
  try {
    storage.setItem(storageKey, encoded);
    if (storage.getItem(storageKey) !== encoded) {
      return "The exact invocation request could not be read back from browser session storage. No invocation was sent.";
    }
    return null;
  } catch {
    return "The exact invocation request could not be persisted in browser session storage. No invocation was sent.";
  }
}

function clearPendingRoutineInvocation(storageKey: string | null): boolean {
  if (!storageKey) return false;
  const storage = routineInvocationStorage();
  if (!storage) return false;
  try {
    storage.removeItem(storageKey);
    return storage.getItem(storageKey) === null;
  } catch {
    return false;
  }
}

function emptyCreateDraft(): CreateDraft {
  return {
    title: "",
    body: "",
    goalId: "",
    goalRevision: "",
    status: "triage",
    capabilityId: "",
    typedInputRef: "",
    typedInputDigest: "",
    executorId: "",
    assigneeId: "",
    priority: "50",
    scheduledAt: "",
    requiresReview: false,
    reviewerId: "",
  };
}

interface EditDraft {
  title: string;
  body: string;
  priority: string;
  capabilityId: string;
  typedInputRef: string;
  typedInputDigest: string;
  executorId: string;
  assigneeId: string;
  scheduledAt: string;
}

function boardApiUrl(path: string): string {
  return `${API_URL}/api/work-board${path}`;
}

function asSafeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function isWorkBoardEvent(value: unknown): value is WorkBoardEvent {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const event = value as Partial<WorkBoardEvent>;
  return asSafeInteger(event.event_id)
    && typeof event.task_id === "string"
    && event.task_id.length > 0
    && typeof event.kind === "string"
    && Boolean(event.metadata)
    && typeof event.metadata === "object"
    && !Array.isArray(event.metadata)
    && typeof event.created_at === "string";
}

function responseError(payload: unknown, status: number): WorkBoardApiError {
  const body = payload && typeof payload === "object" ? payload as ApiErrorBody : {};
  const detail = body.detail;
  const code = typeof detail === "object" && detail ? detail.code : null;
  const message = typeof detail === "object" && detail ? detail.message : detail;
  const recovery = typeof detail === "object" && detail ? detail.recovery : null;
  const statusCode = typeof code === "string" ? code : `http_${status}`;
  const safeMessage = typeof message === "string" && message.trim()
    ? message.trim().slice(0, 500)
    : `The work board request failed (${statusCode}).`;
  return new WorkBoardApiError(
    status,
    statusCode,
    safeMessage,
    typeof recovery === "string" ? recovery.slice(0, 500) : null,
  );
}

async function boardRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await apiFetch(boardApiUrl(path), init);
  if (init?.signal?.aborted) {
    const error = new Error("The work-board request was cancelled.");
    error.name = "AbortError";
    throw error;
  }
  const payload = await response.json().catch(() => null);
  if (!response.ok) throw responseError(payload, response.status);
  return payload as T;
}

async function apiRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await apiFetch(`${API_URL}${path}`, init);
  if (init?.signal?.aborted) {
    const error = new Error("The work-board request was cancelled.");
    error.name = "AbortError";
    throw error;
  }
  const payload = await response.json().catch(() => null);
  if (!response.ok) throw responseError(payload, response.status);
  return payload as T;
}

function buildBoardSocketUrl(cursor: number): string {
  const chatSocketUrl = resolveWebSocketUrl(WS_URL, window.location.href);
  const socketUrl = new URL(chatSocketUrl);
  socketUrl.pathname = "/ws/work-board/events";
  socketUrl.search = "";
  // The server owns cursor meaning. Preserve the returned number as-is.
  socketUrl.searchParams.set("after", String(cursor));
  return socketUrl.toString();
}

function flattenGoals(goals: GoalInfo[]): GoalInfo[] {
  const flattened: GoalInfo[] = [];
  const visit = (items: GoalInfo[]) => {
    for (const item of items) {
      flattened.push(item);
      if (Array.isArray(item.children)) visit(item.children);
    }
  };
  visit(goals);
  return flattened;
}

function formatAge(value: string | null | undefined): string {
  if (!value) return "age unavailable";
  const timestamp = new Date(value).getTime();
  if (!Number.isFinite(timestamp)) return "age unavailable";
  const seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1_000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h`;
  return `${Math.floor(hours / 24)}d`;
}

function toLocalDateTime(value: string | null | undefined): string {
  if (!value) return "";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "";
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}

function normalizedIsoOrNull(value: string | null | undefined): string | null {
  if (!value) return null;
  const timestamp = new Date(value).getTime();
  return Number.isFinite(timestamp) ? new Date(timestamp).toISOString() : value;
}

function eventSummary(event: WorkBoardEvent): string {
  const metadata = event.metadata;
  const parts = [
    metadata.status,
    metadata.outcome,
    metadata.reason_code,
    metadata.recovery_action,
  ].filter((value): value is string => typeof value === "string" && value.length > 0);
  return parts.length ? parts.join(" · ") : event.kind.replace(/[_.]/g, " ");
}

function attemptLabel(task: WorkBoardTask): string {
  const attempt = task.latest_attempt;
  if (!attempt) return "No attempt";
  if (!attempt.ended_at) return attempt.cancel_requested_at ? "Cancellation pending" : "Active attempt";
  return attempt.outcome ? attempt.outcome.replace(/_/g, " ") : "Attempt ended";
}

function isActiveAttempt(task: WorkBoardTask): boolean {
  return Boolean(task.latest_attempt && !task.latest_attempt.ended_at);
}

function safeDateTime(value: string | null | undefined): string {
  if (!value) return "Not scheduled";
  const date = new Date(value);
  return Number.isFinite(date.getTime()) ? date.toLocaleString() : "Schedule unavailable";
}

function errorText(error: unknown): string {
  if (error instanceof WorkBoardApiError) {
    if (error.status === 401 || error.status === 403) return "Your current operator session cannot access this work board.";
    if (error.status === 503 || error.code === "board_storage_unavailable") {
      return error.recovery
        ? `The workspace database is unavailable. ${error.recovery}`
        : "The workspace database is unavailable. Check its readiness and refresh.";
    }
    if (error.status === 409 || /stale|conflict|revision/i.test(error.code)) {
      return "The task or goal changed. The board is refreshing the current state.";
    }
    return error.message;
  }
  if (error instanceof WorkBoardSyncError) return error.message;
  return "The work board could not reach the authenticated backend. The last confirmed state is preserved.";
}

function inputErrorMessage(error: unknown): string {
  if (error instanceof WorkBoardApiError) return error.message;
  return "The request could not be completed. Check the task and try again.";
}

function hasValidDigest(value: string): boolean {
  return /^[0-9a-f]{64}$/i.test(value.trim());
}

function uniqueTasks(tasks: WorkBoardTask[]): WorkBoardTask[] {
  const byId = new Map<string, WorkBoardTask>();
  for (const task of tasks) byId.set(task.task_id, task);
  return Array.from(byId.values()).sort((left, right) => left.creation_sequence - right.creation_sequence);
}

function makeIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return `work-board-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

function normalizeSourceWatches(payload: unknown): WorkBoardSourceWatch[] {
  const values = Array.isArray(payload)
    ? payload
    : payload && typeof payload === "object" && Array.isArray((payload as { watches?: unknown }).watches)
      ? (payload as { watches: unknown[] }).watches
      : [];
  return values.flatMap((value) => {
    if (!value || typeof value !== "object") return [];
    const item = value as Record<string, unknown>;
    const id = typeof item.id === "string" ? item.id : "";
    const goalId = typeof item.goal_id === "string" ? item.goal_id : "";
    const goalRevision = item.goal_revision;
    const planRevision = item.plan_revision;
    if (!id || !goalId
      || typeof goalRevision !== "number" || !Number.isSafeInteger(goalRevision) || goalRevision < 1
      || typeof planRevision !== "number" || !Number.isSafeInteger(planRevision) || planRevision < 1) return [];
    return [{
      id,
      goal_id: goalId,
      goal_revision: goalRevision,
      plan_revision: planRevision,
      state: typeof item.state === "string" ? item.state : "unknown",
      last_status: typeof item.last_status === "string" ? item.last_status : null,
    }];
  });
}

function normalizeRoutineList(payload: unknown): WorkBoardRoutineRead[] {
  const values = payload && typeof payload === "object" && Array.isArray((payload as { routines?: unknown }).routines)
    ? (payload as { routines: unknown[] }).routines
    : Array.isArray(payload) ? payload : [];
  return values.filter((value): value is WorkBoardRoutineRead => {
    if (!value || typeof value !== "object") return false;
    const item = value as Partial<WorkBoardRoutineRead>;
    return typeof item.id === "string"
      && typeof item.name === "string"
      && typeof item.state === "string"
      && typeof item.revision === "number"
      && Number.isSafeInteger(item.revision)
      && Array.isArray(item.versions)
      && Boolean(item.package && typeof item.package === "object");
  });
}

function receiptTitle(reference: WorkBoardReceiptReference): string {
  return reference.artifact_type || reference.effect_type || reference.status || "Execution receipt";
}

function safeReferenceLabel(reference: WorkBoardReceiptReference): string {
  return reference.artifact_id
    || reference.artifact_ref
    || reference.workflow_run_id
    || reference.job_id
    || reference.readback_id
    || reference.verification_id
    || reference.effect_id_digest
    || reference.file_path
    || reference.target_path
    || reference.reason_code
    || "Safe reference";
}

function browserArtifactPath(value: string | undefined): string | undefined {
  const candidate = value?.trim();
  if (!candidate || candidate.length > 512 || candidate.startsWith("/") || candidate.includes("\\") || candidate.includes("\u0000")) {
    return undefined;
  }
  const segments = candidate.split("/");
  if (segments.some((segment) => segment === "" || segment === "." || segment === "..")) return undefined;
  return candidate;
}

/**
 * BrowserRunner receipts use the capability's artifact_ref/artifact_sha256
 * names. The existing WorkBoard inspector deliberately accepts only its
 * owner-bound file_path/content_sha256 projection, so adapt those safe server
 * fields into the established request shape without manufacturing a URL or
 * weakening the workflow/session checks in CockpitView.
 */
function browserEvidenceReference(reference: WorkBoardReceiptReference): WorkBoardReceiptReference {
  const artifactPath = browserArtifactPath(reference.artifact_ref);
  const artifactDigest = typeof reference.artifact_sha256 === "string"
    && /^[a-f0-9]{64}$/i.test(reference.artifact_sha256.trim())
    ? reference.artifact_sha256.trim().toLowerCase()
    : undefined;
  return {
    ...reference,
    file_path: reference.file_path ?? artifactPath,
    content_sha256: reference.content_sha256 ?? artifactDigest,
  };
}

function safeBrowserExecution(value: WorkBoardBrowserExecution | null | undefined): WorkBoardBrowserExecution | null {
  if (!value || value.capability_id !== "browser.public-task.v1") return null;
  if (typeof value.job_id !== "string" || !value.job_id.trim() || typeof value.durable_status !== "string") return null;
  return value;
}

function browserExecutionForAttempt(task: WorkBoardTask, attempt: WorkBoardAttempt): WorkBoardBrowserExecution | null {
  const latest = task.latest_attempt;
  // The authenticated task-detail DTO carries the verified browser projection
  // on latest_attempt. The historical attempts list remains metadata-only;
  // prefer the detail projection for the same attempt and never replace a
  // server-provided null with an older list receipt.
  if (latest?.attempt_id === attempt.attempt_id
    && Object.prototype.hasOwnProperty.call(latest, "browser_execution")) {
    return safeBrowserExecution(latest.browser_execution);
  }
  return safeBrowserExecution(attempt.browser_execution);
}

function browserExecutionCount(value: number | null, minimum: number, maximum: number): string {
  return typeof value === "number" && Number.isInteger(value) && value >= minimum && value <= maximum
    ? String(value)
    : "unavailable";
}

function browserExecutionAction(value: WorkBoardBrowserExecution): string {
  const count = browserExecutionCount(value.action_count, 1, 8);
  if (typeof value.action_index !== "number" || !Number.isInteger(value.action_index) || value.action_index < -1 || value.action_index > 7) {
    return `action unavailable/${count}`;
  }
  return value.action_index < 0 ? `action not started/${count}` : `action ${value.action_index + 1}/${count}`;
}

function browserExecutionReference(value: WorkBoardBrowserExecution): WorkBoardReceiptReference | null {
  if (!value.file_path || !value.content_sha256 || !/^[a-f0-9]{64}$/i.test(value.content_sha256)) return null;
  const reference: WorkBoardReceiptReference = {
    file_path: value.file_path,
    content_sha256: value.content_sha256.toLowerCase(),
    job_id: value.job_id,
    verified: true,
  };
  if (value.artifact_id) reference.artifact_id = value.artifact_id;
  if (value.readback_id) reference.readback_id = value.readback_id;
  return reference;
}

function safeCalendarExecution(value: unknown): CalendarExecutionProjection | null {
  try {
    return validateCalendarExecution(value);
  } catch {
    return null;
  }
}

function calendarExecutionForAttempt(task: WorkBoardTask, attempt: WorkBoardAttempt): CalendarExecutionProjection | null {
  const latest = task.latest_attempt;
  if (latest?.attempt_id === attempt.attempt_id && Object.prototype.hasOwnProperty.call(latest, "calendar_execution")) {
    return safeCalendarExecution(latest.calendar_execution);
  }
  return safeCalendarExecution(attempt.calendar_execution);
}

function calendarExecutionReference(value: CalendarExecutionProjection): WorkBoardReceiptReference | null {
  if (!value.artifact_id || !value.file_path || !value.content_sha256 || !value.readback_id || !value.verified_at || !/^(?:sha256:)?[a-f0-9]{64}$/i.test(value.content_sha256)) return null;
  return {
    artifact_id: value.artifact_id,
    file_path: value.file_path,
    content_sha256: value.content_sha256.replace(/^sha256:/i, "").toLowerCase(),
    readback_id: value.readback_id,
    job_id: value.job_id,
    verified: true,
    artifact_type: "calendar_meeting_prep_result",
  };
}

function proposalStatusLabel(proposal: WorkBoardProposal): string {
  return (proposal.status ?? "unknown").replace(/_/g, " ");
}

function hasServerAuthorityPreview(authority: unknown): authority is string {
  if (typeof authority !== "string") return false;
  const currentPreflight = authority.includes("Current provider-free preflight: READY;")
    || authority.includes("Current provider-free preflight: PENDING input binding at acceptance; dispatch remains unavailable.")
    || /Current provider-free preflight: BLOCKED code=[a-z0-9_]+;/.test(authority);
  return authority.includes("Owner: authenticated owner/session; goal ")
    && authority.includes("Capability-specific authority requirements: ")
    && currentPreflight
    && /\b[1-9]\d{0,2}s effective goal\/job runtime/.test(authority)
    && authority.includes("Accepting creates Todo only and grants no authority or external-effect approval")
    && authority.includes("required independent readback");
}

function referenceWorkflowRunId(
  reference: WorkBoardReceiptReference,
  fallback: string | null,
): string | null {
  // Registered board adapters return the child durable job in ``job_id``
  // while ``workflow_run_id`` remains the immutable parent attempt link.
  // Follow the child when it is present so the inspector can enforce the
  // child session/parent lineage checks against its own projection.
  return reference.child_job_id ?? reference.job_id ?? reference.workflow_run_id ?? fallback;
}

function WorkBoardPanel({
  onOpenApprovals,
  onOpenInboxCandidate,
  onInspectWorkflowRun,
  onInspectArtifact,
  focusTaskId,
  onFocusTaskHandled,
  ownerPrincipalId,
  ownerSessionId,
  onSelectedTaskChange,
  attentionContext,
  onReturnAttention,
  onOpenAttentionGoal,
  onOpenAttentionThread,
  onOpenAccounting,
}: WorkBoardPanelProps) {
  const pendingCreateScope = ownerPrincipalId && ownerSessionId
    ? `${ownerPrincipalId}\u0000${ownerSessionId}`
    : null;
  const pendingCreateAtMount = pendingCreateScope ? pendingTaskCreates.get(pendingCreateScope) ?? null : null;
  const pendingBrowserAtMount = pendingCreateScope ? pendingBrowserSubmissions.get(pendingCreateScope) ?? null : null;
  const pendingCalendarAtMount = pendingCreateScope ? pendingCalendarSubmissions.get(pendingCreateScope) ?? null : null;
  const pendingRepoRepairAtMount = pendingCreateScope ? pendingRepoRepairSubmissions.get(pendingCreateScope) ?? null : null;
  const previousBrowserScopeRef = useRef<string | null>(pendingCreateScope);
  useEffect(() => {
    const previousScope = previousBrowserScopeRef.current;
    if (previousScope && previousScope !== pendingCreateScope) {
      pendingBrowserSubmissions.delete(previousScope);
      pendingCalendarSubmissions.delete(previousScope);
      pendingRepoRepairSubmissions.delete(previousScope);
    }
    previousBrowserScopeRef.current = pendingCreateScope;
  }, [pendingCreateScope]);
  const createIdempotencyRef = useRef(pendingCreateAtMount?.idempotencyKey ?? makeIdempotencyKey());
  const [pendingCreate, setPendingCreate] = useState<PendingTaskCreate | null>(pendingCreateAtMount);
  const [tasks, setTasks] = useState<WorkBoardTask[]>([]);
  const [goals, setGoals] = useState<GoalInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [stale, setStale] = useState(false);
  const [connectionState, setConnectionState] = useState<"connecting" | "connected" | "disconnected" | "denied">("connecting");
  const [boardError, setBoardError] = useState<string | null>(null);
  const [goalError, setGoalError] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const [searchText, setSearchText] = useState("");
  const [statusFilter, setStatusFilter] = useState<"all" | WorkBoardStatus>("all");
  const [assigneeFilter, setAssigneeFilter] = useState("all");
  const [showArchived, setShowArchived] = useState(false);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
  const [detail, setDetail] = useState<WorkBoardTaskDetail | null>(null);
  const [inboxOrigin, setInboxOrigin] = useState<{ requestKey: string; item: GuardianInboxItem } | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [moveFeedback, setMoveFeedback] = useState<string | null>(null);
  const [busyAction, setBusyAction] = useState(false);
  const [createOpen, setCreateOpen] = useState(Boolean(pendingCreateAtMount));
  const [browserTaskOpen, setBrowserTaskOpen] = useState(Boolean(pendingBrowserAtMount));
  const [researchOpen, setResearchOpen] = useState(false);
  const [formatterOpen, setFormatterOpen] = useState(false);
  const [browserTaskReceipt, setBrowserTaskReceipt] = useState<BrowserTaskSubmissionReceipt | null>(null);
  const [calendarPrepOpen, setCalendarPrepOpen] = useState(Boolean(pendingCalendarAtMount));
  const [calendarPrepReceipt, setCalendarPrepReceipt] = useState<CalendarPrepResponse | null>(null);
  const [repoRepairOpen, setRepoRepairOpen] = useState(Boolean(pendingRepoRepairAtMount));
  const [repoRepairReceipt, setRepoRepairReceipt] = useState<RepoRepairSubmissionReceipt | null>(null);
  const [createError, setCreateError] = useState<string | null>(pendingCreateAtMount
    ? "A previous create did not return a receipt. Retry the same request to reconcile it before editing or starting another task."
    : null);
  const [createBusy, setCreateBusy] = useState(false);
  const [createLimit, setCreateLimit] = useState<WorkBoardExecutionLimits | null>(null);
  const [createLimitError, setCreateLimitError] = useState<string | null>(null);
  const [createLimitAcknowledged, setCreateLimitAcknowledged] = useState(false);
  const [createDraft, setCreateDraft] = useState<CreateDraft>(() => pendingCreateAtMount?.draft ?? emptyCreateDraft());
  const [editMode, setEditMode] = useState(false);
  const [editDraft, setEditDraft] = useState<EditDraft | null>(null);
  const [detailLimit, setDetailLimit] = useState<WorkBoardExecutionLimits | null>(null);
  const [detailLimitError, setDetailLimitError] = useState<string | null>(null);
  const [detailLimitAcknowledged, setDetailLimitAcknowledged] = useState(false);
  const [blockReason, setBlockReason] = useState("");
  const [blockConfirmed, setBlockConfirmed] = useState(false);
  const [unblockResolution, setUnblockResolution] = useState("");
  const [reviewChangesReason, setReviewChangesReason] = useState("");
  const [proposal, setProposal] = useState<WorkBoardProposal | null>(null);
  const [proposalEvidence, setProposalEvidence] = useState<{ scope: string; replacement: SpecificationReplacement | null } | null>(null);
  const updateProposalEvidence = useCallback((scope: string, replacement: SpecificationReplacement | null) => {
    setProposalEvidence({ scope, replacement });
  }, []);
  const [proposalBusy, setProposalBusy] = useState(false);
  const [proposalError, setProposalError] = useState<string | null>(null);
  const [routineSourceTaskId, setRoutineSourceTaskId] = useState("");
  const [routineActionTaskId, setRoutineActionTaskId] = useState("");
  const [routineName, setRoutineName] = useState("");
  const [routinePreview, setRoutinePreview] = useState<WorkBoardRoutinePreview | null>(null);
  const [routineBinding, setRoutineBinding] = useState<WorkBoardRoutineBinding | null>(null);
  const [routine, setRoutine] = useState<WorkBoardRoutineRead | null>(null);
  const [routinePackagePreview, setRoutinePackagePreview] = useState<WorkBoardRoutinePackagePreview | null>(null);
  const [routinePackageApproval, setRoutinePackageApproval] = useState<WorkBoardRoutinePackageApproval | null>(null);
  const [routineRecords, setRoutineRecords] = useState<WorkBoardRoutineRead[]>([]);
  const [routineListError, setRoutineListError] = useState<string | null>(null);
  const [selectedRoutineId, setSelectedRoutineId] = useState("");
  const [routineApprovalId, setRoutineApprovalId] = useState("");
  const [routineBusy, setRoutineBusy] = useState(false);
  const [routineError, setRoutineError] = useState<string | null>(null);
  const [sourceWatches, setSourceWatches] = useState<WorkBoardSourceWatch[]>([]);
  const [sourceWatchError, setSourceWatchError] = useState<string | null>(null);
  const [routineInvocationGoalId, setRoutineInvocationGoalId] = useState("");
  const [routineInvocationWatchId, setRoutineInvocationWatchId] = useState("");
  const [routineInvokeReceipt, setRoutineInvokeReceipt] = useState<WorkBoardRoutineInvokeReceipt | null>(null);
  const [routineInvokeBusy, setRoutineInvokeBusy] = useState(false);
  const [routinePublication, setRoutinePublication] = useState<WorkBoardRoutinePublicationState | null>(null);
  const [routinePublicationTitle, setRoutinePublicationTitle] = useState("");
  const [routinePublicationBody, setRoutinePublicationBody] = useState("");
  const [routinePublicationBusy, setRoutinePublicationBusy] = useState(false);
  const [routinePublicationError, setRoutinePublicationError] = useState<string | null>(null);
  const routineInvocationStorageKeyValue = useMemo(
    () => routineInvocationStorageKey(ownerPrincipalId, ownerSessionId),
    [ownerPrincipalId, ownerSessionId],
  );
  const pendingRoutineInvocationAtMount = useMemo(
    () => readPendingRoutineInvocation(routineInvocationStorageKeyValue),
    [routineInvocationStorageKeyValue],
  );
  const [pendingRoutineInvocation, setPendingRoutineInvocation] = useState<PendingRoutineInvocation | null>(
    pendingRoutineInvocationAtMount.pending,
  );
  const [pendingRoutineStorageKey, setPendingRoutineStorageKey] = useState<string | null>(
    pendingRoutineInvocationAtMount.pending ? routineInvocationStorageKeyValue : null,
  );
  const [routinePersistenceError, setRoutinePersistenceError] = useState<string | null>(
    pendingRoutineInvocationAtMount.error,
  );
  const [commentDraft, setCommentDraft] = useState("");
  const [parentTaskIdDraft, setParentTaskIdDraft] = useState("");
  const [childTaskIdDraft, setChildTaskIdDraft] = useState("");

  const tasksRef = useRef(tasks);
  const eventCursorRef = useRef<number | null>(null);
  const selectedTaskIdRef = useRef(selectedTaskId);
  const socketRef = useRef<WebSocket | null>(null);
  const socketEventControllerRef = useRef<AbortController | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  const stoppedRef = useRef(false);
  const boardGenerationRef = useRef(0);
  const syncingRef = useRef(false);
  const syncingGenerationRef = useRef(0);
  const socketGenerationRef = useRef(0);
  const reconnectRef = useRef<(() => Promise<boolean>) | null>(null);
  const eventReconcileQueueRef = useRef<Promise<void>>(Promise.resolve());
  const taskDetailRequestVersionRef = useRef(new Map<string, number>());
  const inboxOriginControllerRef = useRef<AbortController | null>(null);
  const inboxOriginRequestKeyRef = useRef<string | null>(null);
  const requestControllersRef = useRef(new Set<AbortController>());
  const createDialogRef = useRef<HTMLFormElement | null>(null);
  const createOpenerRef = useRef<HTMLElement | null>(null);
  const proposalKeysRef = useRef(new Map<string, string>());
  const routineRequestKeyRef = useRef(makeIdempotencyKey());
  const proposalSelectionVersionRef = useRef(0);
  const createBusyRef = useRef(createBusy);
  const taskDetailPanelRef = useRef<HTMLElement | null>(null);
  const taskDetailOpenerRef = useRef<HTMLElement | null>(null);
  createBusyRef.current = createBusy;

  const isCurrentBoardGeneration = useCallback((generation: number): boolean => (
    !stoppedRef.current && generation === boardGenerationRef.current
  ), []);

  const requestBoard = useCallback(<T,>(path: string, init?: RequestInit): Promise<T> => {
    const controller = new AbortController();
    const upstreamSignal = init?.signal;
    const abortFromUpstream = () => controller.abort();
    if (upstreamSignal?.aborted) controller.abort();
    else upstreamSignal?.addEventListener("abort", abortFromUpstream, { once: true });
    requestControllersRef.current.add(controller);
    const method = (init?.method ?? "GET").toUpperCase();
    const hasTimeout = method === "GET" || method === "HEAD";
    let timedOut = false;
    const timeout = hasTimeout
      ? window.setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, BOARD_REQUEST_TIMEOUT_MS)
      : null;
    return boardRequest<T>(path, { ...init, signal: controller.signal })
      .catch((error) => {
        if (timedOut) throw new WorkBoardSyncError("The work-board request timed out. The last confirmed state is preserved while the board reconnects.");
        throw error;
      })
      .finally(() => {
        if (timeout !== null) window.clearTimeout(timeout);
        upstreamSignal?.removeEventListener("abort", abortFromUpstream);
        requestControllersRef.current.delete(controller);
      });
  }, []);

  const requestApi = useCallback(<T,>(path: string, init?: RequestInit): Promise<T> => {
    const controller = new AbortController();
    requestControllersRef.current.add(controller);
    return apiRequest<T>(path, { ...init, signal: controller.signal })
      .finally(() => requestControllersRef.current.delete(controller));
  }, []);

  useEffect(() => {
    tasksRef.current = tasks;
  }, [tasks]);

  useEffect(() => {
    selectedTaskIdRef.current = selectedTaskId;
  }, [selectedTaskId]);

  useEffect(() => {
    const restored = readPendingRoutineInvocation(routineInvocationStorageKeyValue);
    setPendingRoutineInvocation(restored.pending);
    setPendingRoutineStorageKey(restored.pending ? routineInvocationStorageKeyValue : null);
    setRoutinePersistenceError(restored.error);
  }, [routineInvocationStorageKeyValue]);

  const allGoals = useMemo(() => flattenGoals(goals), [goals]);
  const selectedDetail = selectedTaskId && detail?.task.task_id === selectedTaskId ? detail : null;
  const selectedTask = selectedDetail?.task ?? tasks.find((task) => task.task_id === selectedTaskId) ?? null;
  const selectedInboxScope = selectedTask?.idempotency_scope ?? null;
  const selectedInboxScopeMatch = selectedInboxScope?.match(GUARDIAN_INBOX_SCOPE) ?? null;
  const selectedInboxOriginKey = selectedTask && selectedInboxScopeMatch
    ? `${selectedTask.task_id}\u0000${selectedInboxScope}`
    : null;
  const selectedInboxOrigin = selectedInboxOriginKey && inboxOrigin?.requestKey === selectedInboxOriginKey
    ? inboxOrigin.item
    : null;
  const taskById = useMemo(() => new Map(tasks.map((task) => [task.task_id, task])), [tasks]);
  const verifiedRoutineSourceTasks = useMemo(
    () => tasks.filter((task) => task.status === "done" && task.capability_id === ROUTINE_SOURCE_CAPABILITY),
    [tasks],
  );
  const verifiedRoutineActionTasks = useMemo(
    () => tasks.filter((task) => task.status === "done" && task.capability_id === ROUTINE_ACTION_CAPABILITY),
    [tasks],
  );

  const loadAllTaskPages = useCallback(async (): Promise<BoardSnapshot> => {
    const loaded: WorkBoardTask[] = [];
    let after: number | null = null;
    let firstEventCursor: number | null = null;
    const cursors = new Set<number>();
    for (let pageNumber = 0; pageNumber < MAX_SYNC_PAGES; pageNumber += 1) {
      const query = new URLSearchParams({ limit: String(TASK_LIMIT) });
      if (after !== null) query.set("after", String(after));
      const page = await requestBoard<WorkBoardTaskPage>(`/tasks?${query.toString()}`);
      if (!page || !Array.isArray(page.tasks) || !asSafeInteger(page.last_event_id)) {
        throw new WorkBoardSyncError("The board snapshot returned an invalid cursor or task list. The last confirmed state is preserved.");
      }
      if (firstEventCursor === null) firstEventCursor = page.last_event_id;
      loaded.push(...page.tasks);
      if (page.next_after === null || page.next_after === undefined) {
        return { tasks: uniqueTasks(loaded), eventCursor: firstEventCursor };
      }
      if (!asSafeInteger(page.next_after) || cursors.has(page.next_after)) {
        throw new WorkBoardSyncError("The board returned an invalid page cursor. The last confirmed state is preserved.");
      }
      cursors.add(page.next_after);
      // Pass the server-issued cursor through unchanged on the next request.
      after = page.next_after;
    }
    throw new WorkBoardSyncError("The board has more pages than this bounded snapshot can load. Refresh to try again.");
  }, [requestBoard]);

  const loadEventDelta = useCallback(async (startingCursor: number, signal?: AbortSignal): Promise<EventDelta> => {
    let after = startingCursor;
    const events: WorkBoardEvent[] = [];
    const cursors = new Set<number>();
    for (let pageNumber = 0; pageNumber < MAX_SYNC_PAGES; pageNumber += 1) {
      const query = new URLSearchParams({ after: String(after), limit: String(EVENT_LIMIT) });
      const page = await requestBoard<WorkBoardEventPage>(`/events?${query.toString()}`, { signal });
      if (!page || !Array.isArray(page.events) || !asSafeInteger(page.last_event_id) || typeof page.gap !== "boolean") {
        throw new WorkBoardSyncError("The event cursor response is invalid. The board is stale until a fresh snapshot succeeds.");
      }
      if (page.gap) throw new WorkBoardSyncError("The event cursor is too old. The board is refreshing from a fresh snapshot.");
      let previousEventId = after;
      for (const event of page.events) {
        if (!isWorkBoardEvent(event) || event.event_id <= previousEventId) {
          throw new WorkBoardSyncError("The event stream contains malformed or out-of-order data. The board is taking a fresh snapshot.");
        }
        previousEventId = event.event_id;
      }
      if (page.last_event_id < previousEventId || page.last_event_id < after) {
        throw new WorkBoardSyncError("The event cursor moved backwards. The board is taking a fresh snapshot.");
      }
      events.push(...page.events);
      if (page.events.length === 0 || page.events.length < EVENT_LIMIT || page.last_event_id === after) {
        return { events, eventCursor: page.last_event_id };
      }
      if (page.last_event_id <= after || cursors.has(page.last_event_id)) {
        throw new WorkBoardSyncError("The event stream returned a non-advancing cursor. The board is stale.");
      }
      cursors.add(page.last_event_id);
      // Use the cursor returned by this response exactly; do not derive it.
      after = page.last_event_id;
    }
    throw new WorkBoardSyncError("The event backlog exceeded the bounded catch-up window. The board is refreshing.");
  }, [requestBoard]);

  const fetchTaskDetail = useCallback(async (taskId: string, signal?: AbortSignal): Promise<WorkBoardTaskDetail> => {
    return requestBoard<WorkBoardTaskDetail>(`/tasks/${encodeURIComponent(taskId)}`, { signal });
  }, [requestBoard]);

  const readTaskDetail = useCallback(async (taskId: string, signal?: AbortSignal): Promise<WorkBoardTaskDetail | null> => {
    const requestVersion = (taskDetailRequestVersionRef.current.get(taskId) ?? 0) + 1;
    taskDetailRequestVersionRef.current.set(taskId, requestVersion);
    const nextDetail = await fetchTaskDetail(taskId, signal);
    return taskDetailRequestVersionRef.current.get(taskId) === requestVersion ? nextDetail : null;
  }, [fetchTaskDetail]);

  const reloadEventTasks = useCallback(async (
    generation: number,
    targetEventId: number,
    eventTaskId?: string,
  ): Promise<boolean> => {
    const startingCursor = eventCursorRef.current;
    const controller = socketEventControllerRef.current;
    if (startingCursor === null || !controller) return false;
    try {
      // A late notification can arrive after a newer event has advanced the
      // cursor.  Refresh its task directly instead of treating the lower ID
      // as already reconciled; the REST page may have been observed before
      // that event became visible to this owner.
      if (eventTaskId && targetEventId <= startingCursor) {
        const refreshed = await readTaskDetail(eventTaskId, controller.signal);
        if (generation !== socketGenerationRef.current || controller.signal.aborted || !refreshed) return false;
        setTasks((current) => uniqueTasks([
          ...current.filter((task) => task.task_id !== refreshed.task.task_id),
          refreshed.task,
        ]));
        if (refreshed.task.task_id === selectedTaskIdRef.current) setDetail(refreshed);
        return true;
      }
      const delta = await loadEventDelta(startingCursor, controller.signal);
      if (generation !== socketGenerationRef.current || controller.signal.aborted) return false;
      if (delta.eventCursor < targetEventId || !delta.events.some((event) => event.event_id === targetEventId)) return false;
      const taskIds = Array.from(new Set(delta.events.map((event) => event.task_id)));
      const refreshed: WorkBoardTaskDetail[] = [];
      for (let index = 0; index < taskIds.length; index += DETAIL_REFRESH_BATCH_SIZE) {
        const batch = taskIds.slice(index, index + DETAIL_REFRESH_BATCH_SIZE);
        const results = await Promise.all(batch.map((taskId) => readTaskDetail(taskId, controller.signal)));
        if (results.some((item) => item === null)) return false;
        refreshed.push(...results as WorkBoardTaskDetail[]);
      }
      if (generation !== socketGenerationRef.current || controller.signal.aborted) return false;
      if (refreshed.length) {
        const changed = refreshed.map((item) => item.task);
        setTasks((current) => uniqueTasks([
          ...current.filter((task) => !changed.some((item) => item.task_id === task.task_id)),
          ...changed,
        ]));
        const selected = refreshed.find((item) => item.task.task_id === selectedTaskIdRef.current);
        if (selected) setDetail(selected);
      }
      eventCursorRef.current = delta.eventCursor;
      return true;
    } catch (error) {
      if (generation === socketGenerationRef.current) {
        setStale(true);
        setBoardError(errorText(error));
      }
      return false;
    }
  }, [loadEventDelta, readTaskDetail]);

  const loadSnapshotAndCatchUp = useCallback(async (
    generation = boardGenerationRef.current,
  ): Promise<number> => {
    const snapshot = await loadAllTaskPages();
    if (!isCurrentBoardGeneration(generation)) return snapshot.eventCursor;
    setTasks(snapshot.tasks);
    const delta = await loadEventDelta(snapshot.eventCursor);
    if (!isCurrentBoardGeneration(generation)) return delta.eventCursor;
    const taskIds = Array.from(new Set([
      ...delta.events.map((event) => event.task_id),
      ...(selectedTaskIdRef.current ? [selectedTaskIdRef.current] : []),
    ].filter(Boolean)));
    if (taskIds.length) {
      const refreshed: WorkBoardTaskDetail[] = [];
      for (let index = 0; index < taskIds.length; index += DETAIL_REFRESH_BATCH_SIZE) {
        const batch = taskIds.slice(index, index + DETAIL_REFRESH_BATCH_SIZE);
        const results = await Promise.all(batch.map((taskId) => readTaskDetail(taskId)));
        if (!isCurrentBoardGeneration(generation)) return delta.eventCursor;
        if (results.some((item) => item === null)) {
          throw new WorkBoardSyncError("A task changed during event catch-up. The board is taking another fresh snapshot.");
        }
        refreshed.push(...results as WorkBoardTaskDetail[]);
      }
      const changed = refreshed.map((item) => item.task);
      if (changed.length) {
        setTasks((current) => uniqueTasks([
          ...current.filter((task) => !changed.some((item) => item.task_id === task.task_id)),
          ...changed,
        ]));
        const selected = refreshed.find((item) => item.task.task_id === selectedTaskIdRef.current);
        if (selected) setDetail(selected);
      }
    }
    if (!isCurrentBoardGeneration(generation)) return delta.eventCursor;
    eventCursorRef.current = delta.eventCursor;
    setLoading(false);
    setStale(false);
    setBoardError(null);
    return delta.eventCursor;
  }, [isCurrentBoardGeneration, loadAllTaskPages, loadEventDelta, readTaskDetail]);

  const refreshSnapshot = useCallback(async (): Promise<boolean> => {
    if (stoppedRef.current) return false;
    setBoardError(null);
    const synchronized = await reconnectRef.current?.();
    if (synchronized && !stoppedRef.current) {
      setAnnouncement("Work board refreshed from the authenticated server snapshot.");
    }
    return Boolean(synchronized && !stoppedRef.current);
  }, []);

  const refreshSelectedTask = useCallback(async () => {
    if (stoppedRef.current || !selectedTaskId) return;
    const requestedTaskId = selectedTaskId;
    setDetailLoading(true);
    setDetailError(null);
    try {
      const nextDetail = await readTaskDetail(requestedTaskId);
      if (stoppedRef.current || !nextDetail || selectedTaskIdRef.current !== requestedTaskId) return;
      setDetail(nextDetail);
      setTasks((current) => uniqueTasks([
        ...current.filter((task) => task.task_id !== requestedTaskId),
        nextDetail.task,
      ]));
    } catch (error) {
      if (stoppedRef.current) return;
      if (selectedTaskIdRef.current === requestedTaskId) setDetailError(errorText(error));
      if (error instanceof WorkBoardApiError && error.status === 409) void refreshSnapshot();
    } finally {
      if (!stoppedRef.current && selectedTaskIdRef.current === requestedTaskId) setDetailLoading(false);
    }
  }, [readTaskDetail, refreshSnapshot, selectedTaskId]);

  const openSocketAt = useCallback((cursor: number, boardGeneration = boardGenerationRef.current) => {
    if (!isCurrentBoardGeneration(boardGeneration)) return;
    socketEventControllerRef.current?.abort();
    const eventController = new AbortController();
    socketEventControllerRef.current = eventController;
    eventReconcileQueueRef.current = Promise.resolve();
    const generation = socketGenerationRef.current + 1;
    socketGenerationRef.current = generation;
    setConnectionState("connecting");
    let socket: WebSocket;
    try {
      socket = new WebSocket(buildBoardSocketUrl(cursor));
    } catch {
      setConnectionState("disconnected");
      if (reconnectTimerRef.current === null) {
        reconnectTimerRef.current = window.setTimeout(() => {
          reconnectTimerRef.current = null;
          void reconnectRef.current?.();
        }, RECONNECT_DELAY_MS);
      }
      return;
    }
    socketRef.current = socket;
    socket.onopen = () => {
      if (isCurrentBoardGeneration(boardGeneration) && generation === socketGenerationRef.current) setConnectionState("connected");
    };
    socket.onmessage = (message) => {
      if (!isCurrentBoardGeneration(boardGeneration) || generation !== socketGenerationRef.current) return;
      let payload: unknown;
      try {
        payload = JSON.parse(String(message.data));
      } catch {
        setStale(true);
        setBoardError("A malformed live event was received. The board is refreshing from a new snapshot.");
        void reconnectRef.current?.();
        return;
      }
      if (!payload || typeof payload !== "object") {
        setStale(true);
        setBoardError("A malformed live event was received. The board is refreshing from a new snapshot.");
        void reconnectRef.current?.();
        return;
      }
      const eventPayload = payload as Partial<WorkBoardEvent> & { type?: string; last_event_id?: number };
      if (eventPayload.type === "cursor_gap") {
        setStale(true);
        setBoardError("The live event queue reported a cursor gap. The board is taking a fresh snapshot.");
        socket.close(4000, "cursor_gap");
        void reconnectRef.current?.();
        return;
      }
      if (!asSafeInteger(eventPayload.event_id) || typeof eventPayload.task_id !== "string") {
        setStale(true);
        setBoardError("A live event had an invalid cursor. The board is taking a fresh snapshot.");
        socket.close(4000, "invalid_event");
        void reconnectRef.current?.();
        return;
      }
      if (!isWorkBoardEvent(eventPayload)) {
        setStale(true);
        setBoardError("A live event had invalid task metadata. The board is taking a fresh snapshot.");
        socket.close(4000, "invalid_event");
        void reconnectRef.current?.();
        return;
      }
      const eventId = eventPayload.event_id;
      eventReconcileQueueRef.current = eventReconcileQueueRef.current.then(async () => {
        if (generation !== socketGenerationRef.current) return;
        const reconciled = await reloadEventTasks(generation, eventId, eventPayload.task_id);
        if (!reconciled) {
          if (isCurrentBoardGeneration(boardGeneration) && generation === socketGenerationRef.current) void reconnectRef.current?.();
        }
      }).catch(() => {
        if (isCurrentBoardGeneration(boardGeneration) && generation === socketGenerationRef.current) void reconnectRef.current?.();
      });
    };
    socket.onclose = (event) => {
      if (!isCurrentBoardGeneration(boardGeneration) || generation !== socketGenerationRef.current) return;
      if (event.code === 4401) {
        setConnectionState("denied");
        setStale(true);
        setBoardError("The operator session is no longer valid. Sign in again to reconnect the board.");
        void apiFetch(`${API_URL}/api/auth/session`);
        return;
      }
      if (event.code === 4000) return;
      setConnectionState("disconnected");
      setStale(true);
      if (reconnectTimerRef.current === null) {
        reconnectTimerRef.current = window.setTimeout(() => {
          reconnectTimerRef.current = null;
          void reconnectRef.current?.();
        }, RECONNECT_DELAY_MS);
      }
    };
    socket.onerror = () => {
      if (isCurrentBoardGeneration(boardGeneration) && generation === socketGenerationRef.current) setConnectionState("disconnected");
    };
  }, [isCurrentBoardGeneration, reloadEventTasks]);

  const reconnectFromSnapshot = useCallback(async (
    boardGeneration = boardGenerationRef.current,
  ): Promise<boolean> => {
    if (!isCurrentBoardGeneration(boardGeneration) || syncingRef.current) return false;
    syncingRef.current = true;
    const syncGeneration = syncingGenerationRef.current + 1;
    syncingGenerationRef.current = syncGeneration;
    // Invalidate pending socket handlers before reading a new snapshot. The
    // HTTP snapshot and catch-up then become the only state authority.
    socketGenerationRef.current += 1;
    socketEventControllerRef.current?.abort();
    socketEventControllerRef.current = null;
    eventReconcileQueueRef.current = Promise.resolve();
    const previousSocket = socketRef.current;
    socketRef.current = null;
    previousSocket?.close(4000, "snapshot_sync");
    if (reconnectTimerRef.current !== null) {
      window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    setConnectionState("connecting");
    setStale(true);
    setBoardError(null);
    setLoading(tasksRef.current.length === 0);
    try {
      const cursor = await loadSnapshotAndCatchUp(boardGeneration);
      if (isCurrentBoardGeneration(boardGeneration)) openSocketAt(cursor, boardGeneration);
      return isCurrentBoardGeneration(boardGeneration);
    } catch (error) {
      if (isCurrentBoardGeneration(boardGeneration)) {
        setStale(true);
        setLoading(false);
        setConnectionState("disconnected");
        setBoardError(errorText(error));
        if (reconnectTimerRef.current === null) {
          reconnectTimerRef.current = window.setTimeout(() => {
            reconnectTimerRef.current = null;
            void reconnectRef.current?.();
          }, RECONNECT_DELAY_MS);
        }
      }
      return false;
    } finally {
      if (syncingGenerationRef.current === syncGeneration) syncingRef.current = false;
    }
  }, [isCurrentBoardGeneration, loadSnapshotAndCatchUp, openSocketAt]);

  useEffect(() => {
    reconnectRef.current = reconnectFromSnapshot;
  }, [reconnectFromSnapshot]);

  useEffect(() => {
    const boardGeneration = boardGenerationRef.current + 1;
    boardGenerationRef.current = boardGeneration;
    stoppedRef.current = false;
    void reconnectFromSnapshot(boardGeneration);
    void requestApi<GoalInfo[]>("/api/goals/tree")
      .then((payload) => {
        if (!isCurrentBoardGeneration(boardGeneration)) return;
        if (!Array.isArray(payload)) throw new Error("Goals response was not a list");
        setGoals(payload);
        setGoalError(null);
      })
      .catch((error) => {
        if (isCurrentBoardGeneration(boardGeneration) && !(error instanceof Error && error.name === "AbortError")) setGoalError(errorText(error));
      });
    return () => {
      stoppedRef.current = true;
      syncingRef.current = false;
      syncingGenerationRef.current += 1;
      boardGenerationRef.current += 1;
      socketGenerationRef.current += 1;
      socketEventControllerRef.current?.abort();
      socketEventControllerRef.current = null;
      eventReconcileQueueRef.current = Promise.resolve();
      requestControllersRef.current.forEach((controller) => controller.abort());
      requestControllersRef.current.clear();
      inboxOriginControllerRef.current?.abort();
      inboxOriginControllerRef.current = null;
      if (reconnectTimerRef.current !== null) window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
      socketRef.current?.close(1000, "component_unmounted");
      socketRef.current = null;
    };
  }, [isCurrentBoardGeneration, reconnectFromSnapshot, requestApi]);

  useEffect(() => {
    if (!selectedTaskId) {
      setDetail(null);
      setDetailError(null);
      setEditMode(false);
      setEditDraft(null);
      setDetailLimit(null);
      return;
    }
    let active = true;
    setDetailLoading(true);
    setDetailError(null);
    void readTaskDetail(selectedTaskId)
      .then((nextDetail) => {
        if (!active || !nextDetail) return;
        setDetail(nextDetail);
        setTasks((current) => uniqueTasks([
          ...current.filter((task) => task.task_id !== selectedTaskId),
          nextDetail.task,
        ]));
      })
      .catch((error) => { if (active) setDetailError(errorText(error)); })
      .finally(() => { if (active) setDetailLoading(false); });
    return () => { active = false; };
  }, [readTaskDetail, selectedTaskId]);

  useEffect(() => {
    const taskId = selectedTask?.task_id ?? null;
    const scope = selectedTask?.idempotency_scope ?? null;
    const scopeMatch = scope?.match(GUARDIAN_INBOX_SCOPE) ?? null;
    const requestKey = taskId && scopeMatch ? `${taskId}\u0000${scope}` : null;
    const previousRequestKey = inboxOriginRequestKeyRef.current;

    if (previousRequestKey !== requestKey) {
      inboxOriginControllerRef.current?.abort();
      inboxOriginControllerRef.current = null;
      inboxOriginRequestKeyRef.current = requestKey;
      setInboxOrigin(null);
    }
    if (!taskId || !scopeMatch || !requestKey || previousRequestKey === requestKey) return;

    const controller = new AbortController();
    inboxOriginControllerRef.current = controller;
    void fetchGuardianInboxItem(scopeMatch[1], controller.signal)
      .then((item) => {
        if (stoppedRef.current || controller.signal.aborted
          || inboxOriginRequestKeyRef.current !== requestKey
          || selectedTaskIdRef.current !== taskId) return;
        if (item.task_id !== taskId || item.state !== "accepted") return;
        setInboxOrigin({ requestKey, item });
      })
      .catch(() => {
        // A missing, mismatched, or unavailable candidate is not an origin
        // receipt. Keep the board usable without manufacturing provenance.
      })
      .finally(() => {
        if (inboxOriginRequestKeyRef.current === requestKey) {
          inboxOriginControllerRef.current = null;
        }
      });
  }, [selectedTask?.idempotency_scope, selectedTask?.task_id]);

  useEffect(() => {
    setRoutineRecords([]);
    setSourceWatches([]);
    setRoutineListError(null);
    setSourceWatchError(null);
    if (!ownerPrincipalId || !ownerSessionId) return;
    let active = true;
    void requestApi<unknown>("/api/capabilities/routines")
      .then((payload) => {
        if (!active || stoppedRef.current) return;
        setRoutineRecords(normalizeRoutineList(payload));
      })
      .catch((error) => {
        if (active && !stoppedRef.current && !(error instanceof Error && error.name === "AbortError")) {
          setRoutineListError(inputErrorMessage(error));
        }
      });
    void requestApi<unknown>("/api/capabilities/source-watches")
      .then((payload) => {
        if (!active || stoppedRef.current) return;
        setSourceWatches(normalizeSourceWatches(payload));
      })
      .catch((error) => {
        if (active && !stoppedRef.current && !(error instanceof Error && error.name === "AbortError")) {
          setSourceWatchError(inputErrorMessage(error));
        }
      });
    return () => { active = false; };
  }, [ownerPrincipalId, ownerSessionId, requestApi]);

  useEffect(() => {
    const selectionVersion = proposalSelectionVersionRef.current + 1;
    proposalSelectionVersionRef.current = selectionVersion;
    setProposal(null);
    setProposalError(null);
    setProposalBusy(false);
    if (!selectedTaskId) {
      return;
    }
    const taskId = selectedTaskId;
    let active = true;
    void requestBoard<{ proposals: WorkBoardProposal[] }>(
      `/tasks/${encodeURIComponent(taskId)}/proposals`,
    ).then((payload) => {
      if (!active || stoppedRef.current
        || proposalSelectionVersionRef.current !== selectionVersion
        || selectedTaskIdRef.current !== taskId) return;
      const proposals = Array.isArray(payload?.proposals) ? payload.proposals : [];
      const nextProposal = proposals.find((item) => item.status === "proposed" || item.status === "pending_inference")
        ?? proposals[0]
        ?? null;
      if (nextProposal?.idempotency_key) {
        const scope = `${nextProposal.parent_task_id}:${nextProposal.parent_revision}:${nextProposal.kind}`;
        proposalKeysRef.current.set(scope, nextProposal.idempotency_key);
      }
      setProposal(nextProposal);
      setProposalError(nextProposal?.blocked_reason ?? null);
    }).catch((error) => {
      if (active && !stoppedRef.current
        && proposalSelectionVersionRef.current === selectionVersion
        && selectedTaskIdRef.current === taskId
        && !(error instanceof Error && error.name === "AbortError")) {
        setProposalError(errorText(error));
      }
    });
    return () => { active = false; };
  }, [requestBoard, selectedTaskId]);

  useEffect(() => {
    if (selectedTaskId) {
      taskDetailPanelRef.current?.focus();
      return;
    }
    const opener = taskDetailOpenerRef.current;
    if (opener?.isConnected) opener.focus();
    taskDetailOpenerRef.current = null;
  }, [selectedTaskId]);

  useEffect(() => {
    if (!createOpen || !createDraft.goalId || !createDraft.goalRevision) {
      setCreateLimit(null);
      setCreateLimitError(null);
      return;
    }
    const goalRevision = Number(createDraft.goalRevision);
    if (!Number.isSafeInteger(goalRevision) || goalRevision < 1) {
      setCreateLimit(null);
      setCreateLimitError("Select a goal with a current revision before specifying execution.");
      return;
    }
    let active = true;
    setCreateLimit(null);
    setCreateLimitError(null);
    setCreateLimitAcknowledged(false);
    void requestBoard<WorkBoardExecutionLimits>(
      `/goals/${encodeURIComponent(createDraft.goalId)}/execution-limits?goal_revision=${goalRevision}`,
    ).then((limits) => {
      if (!active) return;
      if (limits.goal_revision !== goalRevision || limits.goal_id !== createDraft.goalId) {
        setCreateLimitError("The goal revision changed. Refresh goals before creating a specified task.");
        return;
      }
      setCreateLimit(limits);
    }).catch((error) => {
      if (active) setCreateLimitError(errorText(error));
    });
    return () => { active = false; };
  }, [createDraft.goalId, createDraft.goalRevision, createOpen, requestBoard]);

  useEffect(() => {
    if (!createOpen) return;
    const dialog = createDialogRef.current;
    if (!dialog) return;

    const previousInertStates: Array<{ element: HTMLElement; inert: boolean }> = [];
    let current: HTMLElement | null = dialog;
    while (current && current !== document.body) {
      const parent: HTMLElement | null = current.parentElement;
      if (!parent) break;
      for (const sibling of Array.from(parent.children)) {
        if (sibling === current || !(sibling instanceof HTMLElement)) continue;
        previousInertStates.push({ element: sibling, inert: Boolean(sibling.inert) });
        sibling.inert = true;
      }
      current = parent;
    }

    const focusableSelector = [
      "a[href]",
      "button:not([disabled])",
      "input:not([disabled]):not([type=\"hidden\"])",
      "select:not([disabled])",
      "textarea:not([disabled])",
      "[tabindex]:not([tabindex=\"-1\"])",
    ].join(",");
    const focusables = () => Array.from(dialog.querySelectorAll<HTMLElement>(focusableSelector))
      .filter((element) => !element.hidden && element.getAttribute("aria-hidden") !== "true");
    const focusInitial = () => (dialog.querySelector<HTMLElement>("[autofocus]") ?? focusables()[0] ?? dialog).focus();
    focusInitial();

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeCreateDialog();
        return;
      }
      if (event.key !== "Tab") return;
      const items = focusables();
      const first = items[0];
      const last = items[items.length - 1];
      if (!first || !last) {
        event.preventDefault();
        dialog.focus();
      } else if (event.shiftKey && (document.activeElement === first || !dialog.contains(document.activeElement))) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || !dialog.contains(document.activeElement))) {
        event.preventDefault();
        first.focus();
      }
    };
    const onFocusIn = (event: FocusEvent) => {
      if (event.target instanceof Node && !dialog.contains(event.target)) focusInitial();
    };
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("focusin", onFocusIn);

    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("focusin", onFocusIn);
      for (const { element, inert } of previousInertStates) element.inert = inert;
      const opener = createOpenerRef.current;
      if (opener?.isConnected) opener.focus();
      createOpenerRef.current = null;
    };
  }, [createOpen]);

  useEffect(() => {
    if (!selectedTask) {
      setDetailLimit(null);
      setDetailLimitError(null);
      setDetailLimitAcknowledged(false);
      return;
    }
    let active = true;
    setDetailLimit(null);
    setDetailLimitError(null);
    setDetailLimitAcknowledged(false);
    void requestBoard<WorkBoardExecutionLimits>(
      `/goals/${encodeURIComponent(selectedTask.goal_id)}/execution-limits?goal_revision=${selectedTask.goal_revision}`,
    ).then((limits) => {
      if (!active) return;
      if (limits.goal_revision !== selectedTask.goal_revision || limits.goal_id !== selectedTask.goal_id) {
        setDetailLimitError("The task goal revision is stale. Recheck the goal before promoting or retrying this task.");
        return;
      }
      setDetailLimit(limits);
    }).catch((error) => {
      if (active) setDetailLimitError(errorText(error));
    });
    return () => { active = false; };
  }, [selectedTask?.goal_id, selectedTask?.goal_revision, selectedTask?.task_id, requestBoard]);

  const assigneeOptions = useMemo(() => {
    return Array.from(new Set(tasks.map((task) => task.assignee_id).filter((value): value is string => Boolean(value)))).sort();
  }, [tasks]);

  const visibleTasks = useMemo(() => {
    const query = searchText.trim().toLocaleLowerCase();
    return tasks.filter((task) => {
      if (task.status === "archived" && !showArchived && statusFilter !== "archived") return false;
      if (statusFilter !== "all" && task.status !== statusFilter) return false;
      if (assigneeFilter !== "all" && task.assignee_id !== assigneeFilter) return false;
      if (!query) return true;
      return [task.task_id, task.title, task.body, task.goal_id, task.executor_id, task.assignee_id]
        .some((value) => (value ?? "").toLocaleLowerCase().includes(query));
    });
  }, [assigneeFilter, searchText, showArchived, statusFilter, tasks]);

  const tasksByStatus = useMemo(() => {
    const grouped = new Map<WorkBoardStatus, WorkBoardTask[]>();
    for (const status of BOARD_COLUMNS) grouped.set(status, []);
    for (const task of visibleTasks) {
      if (task.status !== "archived") grouped.get(task.status)?.push(task);
    }
    return grouped;
  }, [visibleTasks]);

  const archivedTasks = useMemo(() => visibleTasks.filter((task) => task.status === "archived"), [visibleTasks]);

  const openTask = useCallback((taskId: string) => {
    if (selectedTaskIdRef.current === null && document.activeElement instanceof HTMLElement) {
      taskDetailOpenerRef.current = createOpenerRef.current?.isConnected
        ? createOpenerRef.current
        : document.activeElement;
    }
    selectedTaskIdRef.current = taskId;
    setActionError(null);
    setMoveFeedback(null);
    setBlockReason("");
    setBlockConfirmed(false);
    setUnblockResolution("");
    setReviewChangesReason("");
    setProposal(null);
    setProposalError(null);
    setRoutineSourceTaskId("");
    setRoutineActionTaskId("");
    setRoutineName("");
    setRoutinePreview(null);
    setRoutineBinding(null);
    setRoutine(null);
    setRoutinePackagePreview(null);
    setRoutinePackageApproval(null);
    setSelectedRoutineId("");
    setRoutineApprovalId("");
    setRoutineError(null);
    setRoutineInvocationGoalId("");
    setRoutineInvocationWatchId("");
    setRoutineInvokeReceipt(null);
    setSourceWatchError(null);
    const openedTask = tasks.find((task) => task.task_id === taskId);
    onSelectedTaskChange?.(openedTask ?? null);
    setRoutineName(openedTask?.status === "done" ? `${openedTask.title} procedure` : "");
    routineRequestKeyRef.current = makeIdempotencyKey();
    setCommentDraft("");
    setSelectedTaskId(taskId);
    setEditMode(false);
    setDetail(null);
  }, [onSelectedTaskChange, tasks]);

  useEffect(() => {
    if (!focusTaskId) return;
    openTask(focusTaskId);
    onFocusTaskHandled?.(focusTaskId);
  }, [focusTaskId, onFocusTaskHandled, openTask]);

  const closeTask = useCallback(() => {
    selectedTaskIdRef.current = null;
    setSelectedTaskId(null);
    onSelectedTaskChange?.(null);
    inboxOriginControllerRef.current?.abort();
    inboxOriginControllerRef.current = null;
    inboxOriginRequestKeyRef.current = null;
    setInboxOrigin(null);
  }, [onSelectedTaskChange]);

  const openCreateDialog = (event: MouseEvent<HTMLButtonElement>) => {
    createOpenerRef.current = event.currentTarget;
    setCreateOpen(true);
  };

  const closeCreateDialog = () => {
    if (!createBusyRef.current && !pendingCreate) setCreateOpen(false);
  };

  const createTask = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setCreateError(null);
    const goalRevision = Number(createDraft.goalRevision);
    const priority = Number(createDraft.priority);
    const specComplete = Boolean(
      createDraft.capabilityId.trim()
      && createDraft.typedInputRef.trim()
      && hasValidDigest(createDraft.typedInputDigest),
    );
    if (!createDraft.goalId || !Number.isSafeInteger(goalRevision) || goalRevision < 1) {
      setCreateError("Choose a goal and current revision before creating the task.");
      return;
    }
    if (!createDraft.title.trim() || createDraft.title.trim().length > 200) {
      setCreateError("Enter a task title between 1 and 200 characters.");
      return;
    }
    if (!Number.isInteger(priority) || priority < 0 || priority > 100) {
      setCreateError("Priority must be between 0 and 100.");
      return;
    }
    if (!pendingCreate && createDraft.status === "todo" && (!specComplete || !createLimit || !createLimitAcknowledged)) {
      setCreateError("A Todo task needs a complete typed capability specification and acknowledgment of its current runtime limit.");
      return;
    }
    if (Boolean(createDraft.typedInputRef.trim()) !== Boolean(createDraft.typedInputDigest.trim())
      || (createDraft.typedInputDigest.trim() && !hasValidDigest(createDraft.typedInputDigest))) {
      setCreateError("Typed input reference and SHA-256 digest must be supplied together.");
      return;
    }
    if (createDraft.requiresReview && !createDraft.reviewerId.trim()) {
      setCreateError("Choose the named reviewer when review is required.");
      return;
    }
    const body: WorkBoardTaskCreateRequest = {
      title: createDraft.title.trim(),
      body: createDraft.body.trim(),
      goal_id: createDraft.goalId,
      goal_revision: goalRevision,
      status: createDraft.status,
      capability_id: createDraft.capabilityId.trim() || null,
      typed_input_ref: createDraft.typedInputRef.trim() || null,
      typed_input_digest: createDraft.typedInputDigest.trim().toLowerCase() || null,
      executor_id: createDraft.executorId.trim() || null,
      assignee_id: createDraft.assigneeId.trim() || null,
      priority,
      idempotency_scope: "task",
      idempotency_key: createIdempotencyRef.current,
      scheduled_at: createDraft.scheduledAt ? new Date(createDraft.scheduledAt).toISOString() : null,
      requires_review: createDraft.requiresReview,
      reviewer_id: createDraft.requiresReview ? createDraft.reviewerId.trim() : null,
    };
    const pending = pendingCreate ?? {
      idempotencyKey: createIdempotencyRef.current,
      draft: { ...createDraft },
      payload: body,
    };
    if (!pendingCreate) {
      if (pendingCreateScope) pendingTaskCreates.set(pendingCreateScope, pending);
      setPendingCreate(pending);
    }
    setCreateBusy(true);
    try {
      const response = await requestBoard<{ task: WorkBoardTask; idempotent_replay: boolean }>("/tasks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(pending.payload),
      });
      if (pendingCreateScope && pendingTaskCreates.get(pendingCreateScope)?.idempotencyKey === pending.idempotencyKey) {
        pendingTaskCreates.delete(pendingCreateScope);
      }
      createIdempotencyRef.current = makeIdempotencyKey();
      if (stoppedRef.current) return;
      setPendingCreate(null);
      setCreateOpen(false);
      setCreateDraft(emptyCreateDraft());
      await refreshSnapshot();
      if (stoppedRef.current) return;
      openTask(response.task.task_id);
      setAnnouncement(response.idempotent_replay ? "The matching task already exists." : "Task created in the canonical work board.");
    } catch (error) {
      const definitiveRejection = error instanceof WorkBoardApiError
        && [400, 401, 403, 404, 422].includes(error.status)
        && error.code !== "idempotency_conflict";
      if (definitiveRejection) {
        if (pendingCreateScope && pendingTaskCreates.get(pendingCreateScope)?.idempotencyKey === pending.idempotencyKey) {
          pendingTaskCreates.delete(pendingCreateScope);
        }
        if (!stoppedRef.current) setPendingCreate(null);
        createIdempotencyRef.current = makeIdempotencyKey();
      }
      if (!stoppedRef.current) {
        setCreateError(definitiveRejection
          ? inputErrorMessage(error)
          : "The create receipt was not confirmed. Retry the unchanged request to reconcile it with the same idempotency key.");
        if (error instanceof WorkBoardApiError && error.status === 409) await refreshSnapshot();
      }
    } finally {
      if (!stoppedRef.current) setCreateBusy(false);
    }
  };

  const setCreateField = <K extends keyof CreateDraft>(key: K, value: CreateDraft[K]) => {
    setCreateDraft((current) => ({ ...current, [key]: value }));
  };

  const openEditMode = () => {
    if (!selectedTask || ["running", "review", "done", "archived"].includes(selectedTask.status)) return;
    setEditDraft({
      title: selectedTask.title,
      body: selectedTask.body,
      priority: String(selectedTask.priority),
      capabilityId: selectedTask.capability_id ?? "",
      typedInputRef: selectedTask.typed_input_ref ?? "",
      typedInputDigest: selectedTask.typed_input_digest ?? "",
      executorId: selectedTask.executor_id ?? "",
      assigneeId: selectedTask.assignee_id ?? "",
      scheduledAt: toLocalDateTime(selectedTask.scheduled_at),
    });
    setActionError(null);
    setEditMode(true);
  };

  const saveTaskEdits = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!selectedTask || !editDraft) return;
    setBusyAction(true);
    setActionError(null);
    const priority = Number(editDraft.priority);
    if (!Number.isInteger(priority) || priority < 0 || priority > 100) {
      setActionError("Priority must be between 0 and 100.");
      setBusyAction(false);
      return;
    }
    const ref = editDraft.typedInputRef.trim();
    const digest = editDraft.typedInputDigest.trim();
    if (Boolean(ref) !== Boolean(digest) || (digest && !hasValidDigest(digest))) {
      setActionError("Typed input reference and SHA-256 digest must be supplied together.");
      setBusyAction(false);
      return;
    }
    const body: WorkBoardTaskPatchRequest = { expected_revision: selectedTask.task_revision };
    const title = editDraft.title.trim();
    const taskBody = editDraft.body.trim();
    const capabilityId = editDraft.capabilityId.trim() || null;
    const executorId = editDraft.executorId.trim() || null;
    const assigneeId = editDraft.assigneeId.trim() || null;
    const scheduledAt = editDraft.scheduledAt ? new Date(editDraft.scheduledAt).toISOString() : null;
    const typedInputChanged = ref !== (selectedTask.typed_input_ref ?? "")
      || digest.toLowerCase() !== (selectedTask.typed_input_digest ?? "");
    if (title !== selectedTask.title) body.title = title;
    if (taskBody !== selectedTask.body) body.body = taskBody;
    if (priority !== selectedTask.priority) body.priority = priority;
    if (capabilityId !== selectedTask.capability_id) body.capability_id = capabilityId;
    if (typedInputChanged) {
      body.typed_input_ref = ref || null;
      body.typed_input_digest = digest.toLowerCase() || null;
    }
    if (executorId !== selectedTask.executor_id) body.executor_id = executorId;
    if (assigneeId !== selectedTask.assignee_id) body.assignee_id = assigneeId;
    if (scheduledAt !== normalizedIsoOrNull(selectedTask.scheduled_at)) body.scheduled_at = scheduledAt;
    if (Object.keys(body).length === 1) {
      setActionError("There are no task changes to save.");
      setBusyAction(false);
      return;
    }
    try {
      await requestBoard<{ task: WorkBoardTask }>(`/tasks/${encodeURIComponent(selectedTask.task_id)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (stoppedRef.current) return;
      await refreshSnapshot();
      if (stoppedRef.current) return;
      await refreshSelectedTask();
      if (stoppedRef.current) return;
      setEditMode(false);
      setAnnouncement("Task fields saved with the current revision.");
    } catch (error) {
      if (stoppedRef.current) return;
      setActionError(inputErrorMessage(error));
      if (error instanceof WorkBoardApiError && error.status === 409) {
        setStale(true);
        await refreshSnapshot();
        if (stoppedRef.current) return;
        await refreshSelectedTask();
      }
    } finally {
      if (!stoppedRef.current) setBusyAction(false);
    }
  };

  const performAction = async (
    action: WorkBoardActionRequest["action"],
    extra: Partial<WorkBoardActionRequest> = {},
    requireConfirmation = false,
  ) => {
    if (!selectedTask) return;
    if (requireConfirmation && !window.confirm(`Confirm ${action} for ${selectedTask.title}?`)) return;
    const body: WorkBoardActionRequest = {
      action,
      expected_revision: selectedTask.task_revision,
      ...extra,
    };
    setBusyAction(true);
    setActionError(null);
    try {
      await requestBoard<{ task: WorkBoardTask } | { task: WorkBoardTask; attempt: unknown }>(
        `/tasks/${encodeURIComponent(selectedTask.task_id)}/actions`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) },
      );
      if (stoppedRef.current) return;
      await refreshSnapshot();
      if (stoppedRef.current) return;
      await refreshSelectedTask();
      if (stoppedRef.current) return;
      setBlockReason("");
      setBlockConfirmed(false);
      setUnblockResolution("");
      setAnnouncement(`${STATUS_LABELS[selectedTask.status]} task action ${action} requested.`);
    } catch (error) {
      if (stoppedRef.current) return;
      setActionError(inputErrorMessage(error));
      if (error instanceof WorkBoardApiError && error.status === 409) {
        setStale(true);
        await refreshSnapshot();
        if (stoppedRef.current) return;
        await refreshSelectedTask();
      } else if (action === "promote") {
        await refreshSnapshot();
        if (stoppedRef.current) return;
        await refreshSelectedTask();
      }
    } finally {
      if (!stoppedRef.current) setBusyAction(false);
    }
  };

  const currentAttempt = selectedTask?.latest_attempt
    ?? selectedDetail?.attempts[0]
    ?? null;
  const currentCalendarExecution = selectedTask && currentAttempt
    ? calendarExecutionForAttempt(selectedTask, currentAttempt)
    : null;
  const currentOwnerSession = Boolean(
    selectedTask
    && ownerPrincipalId
    && ownerSessionId
    && selectedTask.owner_principal_id === ownerPrincipalId
    && selectedTask.owner_session_id === ownerSessionId,
  );
  const namedReviewer = Boolean(
    selectedTask
    && currentOwnerSession
    && ownerPrincipalId
    && selectedTask.reviewer_id === ownerPrincipalId,
  );
  const defaultRoutineActionTaskId = selectedTask?.capability_id === ROUTINE_ACTION_CAPABILITY
    ? selectedTask.task_id
    : selectedDetail?.children.find((taskId) => verifiedRoutineActionTasks.some((task) => task.task_id === taskId))
      ?? verifiedRoutineActionTasks[0]?.task_id
      ?? "";
  const defaultRoutineSourceTaskId = selectedTask?.capability_id === ROUTINE_SOURCE_CAPABILITY
    ? selectedTask.task_id
    : selectedDetail?.parents.find((taskId) => verifiedRoutineSourceTasks.some((task) => task.task_id === taskId))
      ?? verifiedRoutineSourceTasks[0]?.task_id
      ?? "";
  const selectedRoutineActionTaskId = routineActionTaskId || defaultRoutineActionTaskId;
  const selectedRoutineSourceTaskId = routineSourceTaskId || defaultRoutineSourceTaskId;
  const routineSourceTask = taskById.get(selectedRoutineSourceTaskId) ?? null;
  const routineActionTask = taskById.get(selectedRoutineActionTaskId) ?? null;
  const routineJourneyReady = Boolean(
    currentOwnerSession
    && routineSourceTask
    && routineActionTask
    && routineSourceTask.task_id !== routineActionTask.task_id
    && routineSourceTask.status === "done"
    && routineActionTask.status === "done"
    && routineSourceTask.capability_id === ROUTINE_SOURCE_CAPABILITY
    && routineActionTask.capability_id === ROUTINE_ACTION_CAPABILITY,
  );
  const requestReview = () => {
    if (!selectedTask || !currentAttempt) return;
    if (!currentAttempt.attempt_id || !currentAttempt.workflow_run_id) {
      setActionError("Review requires the active fenced attempt with its linked durable workflow run.");
      return;
    }
    void performAction("request_review", {
      attempt_id: currentAttempt.attempt_id,
    });
  };

  const requestChanges = () => {
    const reason = reviewChangesReason.trim();
    if (!reason || reason.length > 500) {
      setActionError("Enter requested changes between 1 and 500 characters.");
      return;
    }
    void performAction("request_changes", { reason });
  };

  const resetRoutineRequest = () => {
    routineRequestKeyRef.current = makeIdempotencyKey();
    setRoutinePreview(null);
    setRoutineBinding(null);
    setRoutine(null);
    setRoutinePackagePreview(null);
    setRoutinePackageApproval(null);
    setSelectedRoutineId("");
    setRoutineApprovalId("");
    setRoutineError(null);
    setRoutineInvokeReceipt(null);
  };

  const readRoutine = async (routineId: string): Promise<WorkBoardRoutineRead | null> => {
    try {
      const nextRoutine = await requestApi<WorkBoardRoutineRead>(
        `/api/capabilities/routines/${encodeURIComponent(routineId)}`,
      );
      if (stoppedRef.current) return null;
      setRoutine(nextRoutine);
      setRoutineRecords((current) => {
        const existing = current.some((item) => item.id === nextRoutine.id);
        return existing
          ? current.map((item) => item.id === nextRoutine.id ? nextRoutine : item)
          : [nextRoutine, ...current];
      });
      return nextRoutine;
    } catch (error) {
      if (!stoppedRef.current) setRoutineError(inputErrorMessage(error));
      return null;
    }
  };

  const readRoutinePublication = async (taskId: string, expectedRevision?: number): Promise<WorkBoardRoutinePublicationState | null> => {
    try {
      const response = await requestBoard<WorkBoardRoutinePublicationResponse>(
        `/tasks/${encodeURIComponent(taskId)}/routine-publication`,
      );
      if (stoppedRef.current) return null;
      if (typeof expectedRevision === "number" && response.publication.task_revision !== expectedRevision) {
        setRoutinePublicationError("The routine publication state changed while this card was open. Refresh the card before continuing.");
        return null;
      }
      setRoutinePublication(response.publication);
      if (response.publication.preview?.title !== undefined) setRoutinePublicationTitle(response.publication.preview.title ?? "");
      if (response.publication.preview?.body !== undefined) setRoutinePublicationBody(response.publication.preview.body ?? "");
      return response.publication;
    } catch (error) {
      if (!stoppedRef.current) {
        if (error instanceof WorkBoardApiError && error.status === 404) {
          setRoutinePublication(null);
          setRoutinePublicationError(null);
        } else {
          setRoutinePublicationError(inputErrorMessage(error));
        }
      }
      return null;
    }
  };

  const prepareRoutinePublication = async () => {
    if (!selectedTask || selectedTask.capability_id !== "guardian-routine.v1") {
      setRoutinePublicationError("Select the governed routine invocation card before preparing publication.");
      return;
    }
    const body = routinePublicationBody.trim();
    const title = routinePublicationTitle.trim();
    if (!body || body.length > 32_000 || title.length > 160) {
      setRoutinePublicationError("Publication body must contain 1–32,000 characters and the title at most 160 characters.");
      return;
    }
    setRoutinePublicationBusy(true);
    setRoutinePublicationError(null);
    const request: WorkBoardRoutinePublicationPrepareRequest = {
      expected_revision: selectedTask.task_revision,
      title: title || null,
      body,
    };
    try {
      const response = await requestBoard<WorkBoardRoutinePublicationResponse>(
        `/tasks/${encodeURIComponent(selectedTask.task_id)}/routine-publication/prepare`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(request),
        },
      );
      if (stoppedRef.current) return;
      setRoutinePublication(response.publication);
      setRoutinePublicationTitle(response.publication.preview?.title ?? title);
      setRoutinePublicationBody(response.publication.preview?.body ?? body);
      setAnnouncement("The exact publication preview is prepared. Inspect it, then approve it in Pending approvals.");
      await refreshSelectedTask();
      await refreshSnapshot();
    } catch (error) {
      if (!stoppedRef.current) setRoutinePublicationError(inputErrorMessage(error));
    } finally {
      if (!stoppedRef.current) setRoutinePublicationBusy(false);
    }
  };

  const resumeRoutinePublication = async () => {
    if (!selectedTask || !routinePublication) return;
    if (routinePublication.approval_status !== "approved") {
      setRoutinePublicationError("Approve the exact publication preview in Pending approvals before resuming this card.");
      onOpenApprovals?.();
      return;
    }
    setRoutinePublicationBusy(true);
    setRoutinePublicationError(null);
    try {
      const response = await requestBoard<WorkBoardRoutinePublicationResponse>(
        `/tasks/${encodeURIComponent(selectedTask.task_id)}/routine-publication/recover`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ expected_revision: selectedTask.task_revision }),
        },
      );
      if (stoppedRef.current) return;
      setRoutinePublication(response.publication);
      setAnnouncement("The approved publication was resumed. The board will show Done only after independent readback.");
      await refreshSelectedTask();
      await refreshSnapshot();
    } catch (error) {
      if (!stoppedRef.current) setRoutinePublicationError(inputErrorMessage(error));
    } finally {
      if (!stoppedRef.current) setRoutinePublicationBusy(false);
    }
  };

  useEffect(() => {
    if (
      selectedTask
      && selectedTask.capability_id === "guardian-routine.v1"
      && currentOwnerSession
    ) {
      void readRoutinePublication(selectedTask.task_id, selectedTask.task_revision);
    } else {
      setRoutinePublication(null);
      setRoutinePublicationError(null);
    }
    // The selected card and its revision are the only board-owned inputs.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedTask?.task_id, selectedTask?.task_revision, currentOwnerSession]);

  const selectExistingRoutine = async (routineId: string) => {
    setSelectedRoutineId(routineId);
    setRoutineError(null);
    setRoutineInvokeReceipt(null);
    setRoutinePreview(null);
    setRoutineBinding(null);
    setRoutinePackagePreview(null);
    setRoutinePackageApproval(null);
    routineRequestKeyRef.current = makeIdempotencyKey();
    if (!routineId) {
      setRoutine(null);
      return;
    }
    const cached = routineRecords.find((item) => item.id === routineId) ?? null;
    setRoutine(cached);
    if (cached?.name) setRoutineName(cached.name);
    await readRoutine(routineId);
  };

  const routineRequest = (): WorkBoardRoutinePreviewRequest | null => {
    const source = taskById.get(selectedRoutineSourceTaskId);
    const action = taskById.get(selectedRoutineActionTaskId);
    const name = routineName.trim();
    if (!currentOwnerSession) {
      setRoutineError("The verified journey can only be reused by its authenticated owner session.");
      return null;
    }
    if (!source || !action || !routineJourneyReady) {
      setRoutineError("Choose one Done research task and its linked Done follow-through task. The server will recheck the link and receipts.");
      return null;
    }
    if (!name || name.length > 80) {
      setRoutineError("Enter a procedure name between 1 and 80 characters.");
      return null;
    }
    return {
      source_task_id: source.task_id,
      action_task_id: action.task_id,
      expected_source_revision: source.task_revision,
      expected_action_revision: action.task_revision,
      name,
      idempotency_key: routineRequestKeyRef.current,
    };
  };

  const previewRoutine = async () => {
    const request = routineRequest();
    if (!request) return;
    setRoutineBusy(true);
    setRoutineError(null);
    try {
      const nextPreview = await requestApi<WorkBoardRoutinePreview>(
        "/api/capabilities/routines/from-board/preview",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(request),
        },
      );
      if (stoppedRef.current) return;
      setRoutinePreview(nextPreview);
      setRoutineBinding(null);
      setRoutine(null);
      setAnnouncement("The verified journey returned a reviewable procedure preview.");
    } catch (error) {
      if (!stoppedRef.current) setRoutineError(inputErrorMessage(error));
    } finally {
      if (!stoppedRef.current) setRoutineBusy(false);
    }
  };

  const acceptRoutine = async () => {
    if (!routinePreview) return;
    const request = routineRequest();
    if (!request) return;
    setRoutineBusy(true);
    setRoutineError(null);
    try {
      const nextBinding = await requestApi<WorkBoardRoutineBinding>(
        "/api/capabilities/routines/from-board",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ...request, preview_digest: routinePreview.preview_digest }),
        },
      );
      if (stoppedRef.current) return;
      setRoutineBinding(nextBinding);
      if (nextBinding.approval_id) setRoutineApprovalId(nextBinding.approval_id);
      if (nextBinding.routine_id) await readRoutine(nextBinding.routine_id);
      if (stoppedRef.current) return;
      setAnnouncement("The procedure was accepted and prepared. Installation still requires its current approval receipt.");
    } catch (error) {
      if (!stoppedRef.current) setRoutineError(inputErrorMessage(error));
    } finally {
      if (!stoppedRef.current) setRoutineBusy(false);
    }
  };

  const routinePackageContext = () => {
    if (!currentOwnerSession) {
      setRoutineError("Package review and activation require the current authenticated owner session.");
      return null;
    }
    const routineId = routine?.id;
    const version = routine?.current_version ?? routine?.versions[0]?.version;
    if (!routineId || !routine || !version) {
      setRoutineError("Install a procedure version before reviewing its capability package.");
      return null;
    }
    return {
      routineId,
      version,
      expectedRevision: routine.revision,
      basePath: `/api/capabilities/routines/${encodeURIComponent(routineId)}/versions/${version}/package`,
    };
  };

  const previewRoutinePackage = async () => {
    const context = routinePackageContext();
    if (!context) return;
    setRoutineBusy(true);
    setRoutineError(null);
    setRoutinePackageApproval(null);
    try {
      const preview = await requestApi<WorkBoardRoutinePackagePreview>(`${context.basePath}/preview`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ expected_routine_revision: context.expectedRevision }),
      });
      if (stoppedRef.current) return;
      setRoutinePackagePreview(preview);
      setAnnouncement("The server returned the exact local procedure package for review. Its approval grants no invocation authority.");
    } catch (error) {
      if (!stoppedRef.current) {
        setRoutineError(inputErrorMessage(error));
        if (error instanceof WorkBoardApiError && error.status === 409) {
          setRoutinePackagePreview(null);
          setRoutinePackageApproval(null);
          void readRoutine(context.routineId);
        }
      }
    } finally {
      if (!stoppedRef.current) setRoutineBusy(false);
    }
  };

  const exportRoutineProcedure = async () => {
    const context = routinePackageContext();
    if (!context) return;
    setRoutineBusy(true);
    setRoutineError(null);
    try {
      const exported = await requestApi<WorkBoardRoutineProcedureExport>(
        `/api/capabilities/routines/${encodeURIComponent(context.routineId)}/versions/${context.version}/export`,
      );
      if (stoppedRef.current) return;
      const blob = new Blob([JSON.stringify(exported, null, 2)], { type: "application/json" });
      const objectUrl = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = objectUrl;
      link.download = `${exported.pack_id}-v${exported.version}.json`;
      link.click();
      URL.revokeObjectURL(objectUrl);
      setAnnouncement(`Exported reviewed procedure ${exported.pack_id}, version ${exported.version}.`);
    } catch (error) {
      if (!stoppedRef.current) setRoutineError(inputErrorMessage(error));
    } finally {
      if (!stoppedRef.current) setRoutineBusy(false);
    }
  };

  const reviewRoutinePackage = async () => {
    const context = routinePackageContext();
    if (!context || !routinePackagePreview) return;
    if (routinePackagePreview.digest !== routinePackagePreview.installed_package_digest) {
      setRoutineError("The package preview does not match the installed digest. Refresh the routine before reviewing it.");
      return;
    }
    setRoutineBusy(true);
    setRoutineError(null);
    setRoutinePackageApproval(null);
    try {
      const result = await requestApi<{
        digest: string;
        review: { review_id: string; status: string };
      }>(`${context.basePath}/review`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ expected_routine_revision: context.expectedRevision }),
      });
      if (stoppedRef.current) return;
      if (result.digest !== routinePackagePreview.digest || !result.review?.review_id || result.review.status !== "approved") {
        setRoutineError("The returned package review did not match the preview digest. Activation remains blocked.");
        return;
      }
      setRoutinePackagePreview((current) => current ? { ...current, review_id: result.review.review_id, status: "reviewed" } : current);
      setAnnouncement("The exact installed procedure package has a local review receipt. Prepare its separate activation approval next.");
    } catch (error) {
      if (!stoppedRef.current) {
        setRoutineError(inputErrorMessage(error));
        if (error instanceof WorkBoardApiError && error.status === 409) {
          setRoutinePackagePreview(null);
          void readRoutine(context.routineId);
        }
      }
    } finally {
      if (!stoppedRef.current) setRoutineBusy(false);
    }
  };

  const prepareRoutinePackageApproval = async () => {
    const context = routinePackageContext();
    if (!context || !routinePackagePreview?.review_id) return;
    setRoutineBusy(true);
    setRoutineError(null);
    try {
      const result = await requestApi<{ digest: string; approval: WorkBoardRoutinePackageApproval }>(
        `${context.basePath}/approvals`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ expected_routine_revision: context.expectedRevision }),
        },
      );
      if (stoppedRef.current) return;
      if (result.digest !== routinePackagePreview.digest
        || result.approval?.digest !== routinePackagePreview.digest
        || result.approval?.action !== "activate"
        || result.approval?.status !== "pending") {
        setRoutineError("The activation approval is not bound to this reviewed package digest. Activation remains blocked.");
        return;
      }
      setRoutinePackageApproval(result.approval);
      setAnnouncement("A short-lived activation approval is bound to this exact package digest and source goal.");
    } catch (error) {
      if (!stoppedRef.current) {
        setRoutineError(inputErrorMessage(error));
        if (error instanceof WorkBoardApiError && error.status === 409) void readRoutine(context.routineId);
      }
    } finally {
      if (!stoppedRef.current) setRoutineBusy(false);
    }
  };

  const decideRoutinePackageApproval = async (decision: "approved" | "denied") => {
    const context = routinePackageContext();
    const approval = routinePackageApproval;
    if (!context || !approval || approval.status !== "pending") return;
    setRoutineBusy(true);
    setRoutineError(null);
    try {
      const result = await requestApi<{ digest: string; approval: WorkBoardRoutinePackageApproval }>(
        `${context.basePath}/approvals/${encodeURIComponent(approval.approval_id)}/decision`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ expected_routine_revision: context.expectedRevision, decision }),
        },
      );
      if (stoppedRef.current) return;
      if (result.digest !== routinePackagePreview?.digest || result.approval?.approval_id !== approval.approval_id
        || result.approval.status !== decision) {
        setRoutineError("The activation decision receipt did not match the pending approval. Refresh before continuing.");
        return;
      }
      setRoutinePackageApproval(result.approval);
      setAnnouncement(decision === "approved"
        ? "Activation is approved for the exact reviewed package digest. Activate it when ready."
        : "Package activation was denied. The procedure remains unavailable for invocation.");
    } catch (error) {
      if (!stoppedRef.current) {
        setRoutineError(inputErrorMessage(error));
        if (error instanceof WorkBoardApiError && error.status === 409) {
          setRoutinePackageApproval(null);
          void readRoutine(context.routineId);
        }
      }
    } finally {
      if (!stoppedRef.current) setRoutineBusy(false);
    }
  };

  const activateRoutinePackage = async () => {
    const context = routinePackageContext();
    const approval = routinePackageApproval;
    if (!context || !approval || approval.status !== "approved" || !routinePackagePreview?.review_id) return;
    setRoutineBusy(true);
    setRoutineError(null);
    try {
      const result = await requestApi<{ digest: string; status: string }>(`${context.basePath}/activate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          expected_routine_revision: context.expectedRevision,
          approval_id: approval.approval_id,
        }),
      });
      if (stoppedRef.current) return;
      if (result.digest !== routinePackagePreview.digest || result.status !== "active") {
        setRoutineError("Package activation could not be verified against the reviewed digest. Procedure invocation remains blocked.");
        return;
      }
      setRoutinePackagePreview((current) => current ? { ...current, status: "active" } : current);
      setRoutinePackageApproval({ ...approval, status: "consumed" });
      setAnnouncement("The reviewed package is active. Enable the procedure separately before requesting invocations.");
      await readRoutine(context.routineId);
    } catch (error) {
      if (!stoppedRef.current) {
        setRoutineError(inputErrorMessage(error));
        if (error instanceof WorkBoardApiError && error.status === 409) {
          setRoutinePackageApproval(null);
          void readRoutine(context.routineId);
        }
      }
    } finally {
      if (!stoppedRef.current) setRoutineBusy(false);
    }
  };

  const runRoutineLifecycleAction = async (
    action: "install" | "activate" | "pause" | "revoke" | "rollback",
    targetVersion?: number,
  ) => {
    const routineId = routine?.id ?? routineBinding?.routine_id;
    if (!routineId) {
      setRoutineError("Accept a verified procedure preview before using its lifecycle controls.");
      return;
    }
    if (routine?.state === "revoked") {
      setRoutineError("This procedure is revoked permanently. Create a new reviewed procedure from a fresh verified journey.");
      return;
    }
    if (action === "install" && !routineApprovalId.trim()) {
      setRoutineError("Installation is blocked until the exact approval receipt is supplied from Pending approvals.");
      onOpenApprovals?.();
      return;
    }
    if (action === "rollback" && !targetVersion) {
      setRoutineError("Choose an installed earlier version before requesting rollback.");
      return;
    }
    if (action === "revoke" && !window.confirm("Revoke this procedure permanently?")) return;
    setRoutineBusy(true);
    setRoutineError(null);
    const version = routinePreview?.version_plan.version
      ?? routine?.current_version
      ?? routine?.versions[0]?.version
      ?? 1;
    const expectedRoutineRevision = routine?.revision ?? routineBinding?.revision ?? 1;
    const body = action === "install"
      ? { version, expected_routine_revision: expectedRoutineRevision, approval_id: routineApprovalId.trim() }
      : action === "activate"
        ? { version, expected_routine_revision: expectedRoutineRevision }
        : action === "rollback"
          ? { target_version: targetVersion, expected_routine_revision: expectedRoutineRevision, reason: "Operator requested rollback after reviewing the procedure receipt." }
          : { expected_routine_revision: expectedRoutineRevision, reason: action === "revoke" ? "Operator revoked the reviewed procedure." : "Operator paused the reviewed procedure." };
    try {
      const result = await requestApi<WorkBoardRoutineRead>(
        `/api/capabilities/routines/${encodeURIComponent(routineId)}/${action}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
      );
      if (stoppedRef.current) return;
      setRoutine(result);
      setRoutineBinding((current) => current ? { ...current, revision: result.revision, state: result.state, status: result.state } : current);
      setAnnouncement(`Procedure ${action} completed with the server's current revision.`);
    } catch (error) {
      if (!stoppedRef.current) {
        setRoutineError(inputErrorMessage(error));
        if (error instanceof WorkBoardApiError && error.status === 409) void readRoutine(routineId);
      }
    } finally {
      if (!stoppedRef.current) setRoutineBusy(false);
    }
  };

  const proposalKey = (
    kind: WorkBoardProposal["kind"],
    task: WorkBoardTask,
    forceNew = false,
  ): string => {
    const scope = `${task.task_id}:${task.task_revision}:${kind}`;
    if (forceNew) proposalKeysRef.current.delete(scope);
    const existing = proposalKeysRef.current.get(scope);
    if (existing) return existing;
    const key = makeIdempotencyKey();
    proposalKeysRef.current.set(scope, key);
    return key;
  };

  const requestProposal = async (kind: WorkBoardProposal["kind"], forceNew = false) => {
    if (!selectedTask || !["triage", "todo"].includes(selectedTask.status)) return;
    const taskId = selectedTask.task_id;
    // Invalidate an in-flight hydration GET before issuing the operator's
    // explicit POST.  The POST response is the newest preview for this card;
    // a slower GET must not overwrite it with an older persisted row.
    const selectionVersion = proposalSelectionVersionRef.current + 1;
    proposalSelectionVersionRef.current = selectionVersion;
    setProposalBusy(true);
    setProposalError(null);
    setProposal(null);
    try {
      const nextProposal = await requestBoard<WorkBoardProposal>(
        `/tasks/${encodeURIComponent(taskId)}/${kind}`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            expected_revision: selectedTask.task_revision,
            idempotency_key: proposalKey(kind, selectedTask, forceNew),
          }),
        },
      );
      if (stoppedRef.current
        || proposalSelectionVersionRef.current !== selectionVersion
        || selectedTaskIdRef.current !== taskId) return;
      setProposal(nextProposal);
      if (nextProposal.status === "blocked") {
        setProposalError(nextProposal.blocked_reason ?? "The governed proposal route is blocked. Follow the recovery guidance before retrying.");
      }
      setAnnouncement(`${kind === "specify" ? "Specify" : "Decompose"} returned a reviewable proposal.`);
    } catch (error) {
      if (stoppedRef.current
        || proposalSelectionVersionRef.current !== selectionVersion
        || selectedTaskIdRef.current !== taskId) return;
      setProposalError(inputErrorMessage(error));
      if (error instanceof WorkBoardApiError && error.status === 409) {
        setStale(true);
        await refreshSnapshot();
        if (stoppedRef.current
          || proposalSelectionVersionRef.current !== selectionVersion
          || selectedTaskIdRef.current !== taskId) return;
        await refreshSelectedTask();
      }
    } finally {
      if (!stoppedRef.current
        && proposalSelectionVersionRef.current === selectionVersion
        && selectedTaskIdRef.current === taskId) setProposalBusy(false);
    }
  };

  const decideProposal = async (decision: "accept" | "reject") => {
    if (!proposal) return;
    const taskId = selectedTaskIdRef.current;
    const selectionVersion = proposalSelectionVersionRef.current;
    const proposalId = proposal.proposal_id;
    setProposalBusy(true);
    setProposalError(null);
    const path = `/proposals/${encodeURIComponent(proposal.proposal_id)}/${decision}`;
    const body = decision === "accept"
      ? {
        expected_proposal_revision: proposal.proposal_revision,
        expected_parent_revision: proposal.parent_revision,
        ...(selectedTask && proposal.kind === "specify" && proposalEvidence?.scope === specificationScope(selectedTask, proposal, ownerSessionId)
          && proposalEvidence.replacement ? { execution_replacement: proposalEvidence.replacement } : {}),
      }
      : { expected_proposal_revision: proposal.proposal_revision };
    try {
      const retainedBody = decision === "accept" && selectedTask && proposal.kind === "specify"
        ? retainSpecificationAcceptance(specificationScope(selectedTask, proposal, ownerSessionId), body as import('./SpecificationEvidenceReview').SpecificationAcceptance)
        : body;
      const receipt = await requestBoard<{
        proposal_id: string;
        status: string;
        proposal_revision: number;
        task_ids?: string[];
      }>(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(retainedBody),
      });
      if (stoppedRef.current
        || proposalSelectionVersionRef.current !== selectionVersion
        || selectedTaskIdRef.current !== taskId) return;
      setProposal((current) => current
        ? { ...current, status: receipt.status, proposal_revision: receipt.proposal_revision }
        : current);
      setAnnouncement(decision === "accept" ? "The reviewed proposal was accepted into the canonical board." : "The proposal was rejected.");
      await refreshSnapshot();
      if (stoppedRef.current
        || proposalSelectionVersionRef.current !== selectionVersion
        || selectedTaskIdRef.current !== taskId) return;
      await refreshSelectedTask();
    } catch (error) {
      if (stoppedRef.current
        || proposalSelectionVersionRef.current !== selectionVersion
        || selectedTaskIdRef.current !== taskId) return;
      setProposalError(inputErrorMessage(error));
      if (error instanceof WorkBoardApiError && error.status === 409) {
        setStale(true);
        await refreshSnapshot();
        if (stoppedRef.current
          || proposalSelectionVersionRef.current !== selectionVersion
          || selectedTaskIdRef.current !== taskId) return;
        await refreshSelectedTask();
      }
    } finally {
      if (!stoppedRef.current
        && proposalSelectionVersionRef.current === selectionVersion
        && selectedTaskIdRef.current === taskId
        && proposal?.proposal_id === proposalId) setProposalBusy(false);
    }
  };

  const addComment = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!selectedTask || !commentDraft.trim()) return;
    const body: WorkBoardCommentCreateRequest = {
      expected_revision: selectedTask.task_revision,
      body: commentDraft.trim(),
    };
    setBusyAction(true);
    setActionError(null);
    try {
      await requestBoard<{ comment: WorkBoardComment }>(`/tasks/${encodeURIComponent(selectedTask.task_id)}/comments`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      if (stoppedRef.current) return;
      setCommentDraft("");
      await refreshSelectedTask();
      if (stoppedRef.current) return;
      await refreshSnapshot();
    } catch (error) {
      if (stoppedRef.current) return;
      setActionError(inputErrorMessage(error));
      if (error instanceof WorkBoardApiError && error.status === 409) {
        setStale(true);
        await refreshSnapshot();
        if (stoppedRef.current) return;
        await refreshSelectedTask();
      }
    } finally {
      if (!stoppedRef.current) setBusyAction(false);
    }
  };

  const createLink = async (parentTaskId: string, childTaskId: string, childRevision: number) => {
    const body: WorkBoardLinkCreateRequest = {
      parent_task_id: parentTaskId.trim(),
      child_task_id: childTaskId.trim(),
      expected_child_revision: childRevision,
    };
    setBusyAction(true);
    setActionError(null);
    try {
      await requestBoard(`/links`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      if (stoppedRef.current) return;
      setParentTaskIdDraft("");
      setChildTaskIdDraft("");
      await refreshSelectedTask();
      if (stoppedRef.current) return;
      await refreshSnapshot();
    } catch (error) {
      if (stoppedRef.current) return;
      setActionError(inputErrorMessage(error));
      if (error instanceof WorkBoardApiError && error.status === 409) {
        setStale(true);
        await refreshSnapshot();
        if (stoppedRef.current) return;
        await refreshSelectedTask();
      }
    } finally {
      if (!stoppedRef.current) setBusyAction(false);
    }
  };

  const deleteLink = async (parentTaskId: string, childTaskId: string, childRevision: number) => {
    const body: WorkBoardLinkDeleteRequest = {
      parent_task_id: parentTaskId,
      child_task_id: childTaskId,
      expected_child_revision: childRevision,
    };
    setBusyAction(true);
    setActionError(null);
    try {
      await requestBoard(`/links`, {
        method: "DELETE", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      if (stoppedRef.current) return;
      await refreshSelectedTask();
      if (stoppedRef.current) return;
      await refreshSnapshot();
    } catch (error) {
      if (stoppedRef.current) return;
      setActionError(inputErrorMessage(error));
      if (error instanceof WorkBoardApiError && error.status === 409) {
        setStale(true);
        await refreshSnapshot();
        if (stoppedRef.current) return;
        await refreshSelectedTask();
      }
    } finally {
      if (!stoppedRef.current) setBusyAction(false);
    }
  };

  const canPromote = Boolean(
    selectedTask
    && selectedTask.status === "triage"
    && selectedTask.capability_id
    && selectedTask.typed_input_ref
    && selectedTask.typed_input_digest
    && hasValidDigest(selectedTask.typed_input_digest)
    && detailLimit
    && detailLimit.goal_revision === selectedTask.goal_revision
    && detailLimitAcknowledged,
  );

  const dragTaskIdRef = useRef<string | null>(null);
  const dropTask = (status: WorkBoardStatus) => async (event: React.DragEvent<HTMLElement>) => {
    event.preventDefault();
    const taskId = event.dataTransfer.getData("text/plain") || dragTaskIdRef.current;
    dragTaskIdRef.current = null;
    const task = tasks.find((item) => item.task_id === taskId);
    if (!task) return;
    if (task.status === "triage" && status === "todo") {
      if (selectedTask?.task_id === task.task_id && canPromote) {
        setMoveFeedback(null);
        await performAction("promote");
        return;
      }
      const message = "Open task details, complete the typed specification, and acknowledge the current limit before promoting to Todo.";
      setMoveFeedback(message);
      setAnnouncement(message);
      openTask(task.task_id);
      return;
    }
    // Ready/Running/Review/Done are server-owned; no card drag can force them.
    const rejectedMove = `The backend does not allow moving ${STATUS_LABELS[task.status]} directly to ${STATUS_LABELS[status]}.`;
    setMoveFeedback(rejectedMove);
    const refreshed = await refreshSnapshot();
    if (stoppedRef.current) return;
    const message = `${rejectedMove} ${refreshed ? "The board was refreshed from the server." : "The refresh failed; the last confirmed state remains visible."}`;
    setMoveFeedback(message);
    setAnnouncement(message);
  };

  const canManuallyBlock = (task: WorkBoardTask) => {
    return ["triage", "todo", "ready"].includes(task.status)
      && !isActiveAttempt(task)
      && task.status !== "running";
  };

  const canRetry = Boolean(
    selectedTask
    && selectedTask.status === "blocked"
    && ["retry", "restore_prerequisite"].includes(selectedTask.recovery_action ?? "")
    && !isActiveAttempt(selectedTask)
    && detailLimit
    && detailLimit.goal_revision === selectedTask.goal_revision
    && detailLimitAcknowledged,
  );

  const selectGoal = (goalId: string) => {
    const goal = allGoals.find((item) => item.id === goalId);
    setCreateField("goalId", goalId);
    setCreateField("goalRevision", goal?.revision ? String(goal.revision) : "");
  };

  const statusOptions: Array<"all" | WorkBoardStatus> = ["all", ...BOARD_COLUMNS, "archived"];
  const activeCount = tasks.filter((task) => task.status === "running").length;
  const proposalAuthorityComplete = Boolean(
    proposal
    && proposal.proposed_tasks.length > 0
    && proposal.proposed_tasks.every((task) => hasServerAuthorityPreview(task.authority)),
  );
  const routineVersion = routinePreview?.version_plan.version
    ?? routine?.current_version
    ?? routine?.versions[0]?.version
    ?? 1;
  const routineVersionRecord = routine?.versions.find((version) => version.version === routineVersion) ?? null;
  const routinePackageDigest = routineVersionRecord?.installed_package_digest ?? null;
  const routinePackageReviewed = Boolean(
    routine?.package?.status === "active"
    && routinePackageDigest
    && routine?.package?.digest === routinePackageDigest,
  );
  const routineRollbackVersions = routine?.versions.filter((version) => (
    version.version !== routine.current_version
    && Boolean(version.installed_package_digest)
    && routine.package?.status === "active"
    && Boolean(routine.package.digest)
    && version.installed_package_digest === routine.package.digest
  )) ?? [];
  const routineRollbackCandidates = routine?.versions.filter((version) => (
    version.version !== routine.current_version && Boolean(version.installed_package_digest)
  )) ?? [];
  const routineRollbackBlocked = Boolean(
    routine
    && routine.state !== "revoked"
    && routineRollbackCandidates.length > 0
    && routineRollbackVersions.length === 0,
  );
  const activeRoutineGoals = useMemo(
    () => allGoals.filter((goal) => goal.status === "active"),
    [allGoals],
  );
  const defaultRoutineInvocationGoalId = activeRoutineGoals.some((goal) => goal.id === selectedTask?.goal_id)
    ? selectedTask?.goal_id ?? ""
    : activeRoutineGoals[0]?.id ?? "";
  const selectedRoutineInvocationGoalId = pendingRoutineInvocation
    && pendingRoutineInvocation.routineId === routine?.id
    ? pendingRoutineInvocation.request.goal_id
    : activeRoutineGoals.some((goal) => goal.id === routineInvocationGoalId)
      ? routineInvocationGoalId
      : defaultRoutineInvocationGoalId;
  const selectedRoutineInvocationGoal = activeRoutineGoals.find((goal) => goal.id === selectedRoutineInvocationGoalId) ?? null;
  const routineWatchCandidates = useMemo(
    () => sourceWatches.filter((watch) => watch.goal_id === selectedRoutineInvocationGoalId),
    [selectedRoutineInvocationGoalId, sourceWatches],
  );
  const selectedRoutineWatchId = pendingRoutineInvocation
    && pendingRoutineInvocation.routineId === routine?.id
    ? pendingRoutineInvocation.request.source_watch_id
    : routineWatchCandidates.some((watch) => watch.id === routineInvocationWatchId)
      ? routineInvocationWatchId
      : routineWatchCandidates.find((watch) => watch.state === "active")?.id
          ?? routineWatchCandidates[0]?.id
          ?? "";
  const selectedRoutineWatch = routineWatchCandidates.find((watch) => watch.id === selectedRoutineWatchId) ?? null;
  const pendingRoutineMatches = !pendingRoutineInvocation || pendingRoutineInvocation.routineId === routine?.id;
  const pendingRoutineReplayReady = Boolean(
    currentOwnerSession
    && pendingRoutineInvocation
    && pendingRoutineStorageKey === routineInvocationStorageKeyValue
    && routine
    && pendingRoutineInvocation.routineId === routine.id
  );
  const routineInvocationReady = pendingRoutineReplayReady || Boolean(
    currentOwnerSession
    && routine?.state === "active"
    && routinePackageReviewed
    && pendingRoutineMatches
    && (!routinePersistenceError || Boolean(pendingRoutineInvocation))
    && selectedRoutineInvocationGoal
    && typeof selectedRoutineInvocationGoal.revision === "number"
    && selectedRoutineWatch?.state === "active",
  );

  const invokeRoutine = async () => {
    const pending = pendingRoutineInvocation;
    const routineId = pending?.routineId ?? routine?.id ?? routineBinding?.routine_id;
    if (!routineId || !routine) {
      setRoutineError("Select or accept a governed procedure before invoking it.");
      return;
    }
    if (pending && pending.routineId !== routine.id) {
      setRoutineError("A different routine has an unconfirmed invocation. Select that routine before retrying; no new request was created.");
      return;
    }
    if (!currentOwnerSession) {
      setRoutineError("Invocation is available only to the authenticated owner session of the selected card.");
      return;
    }
    if (pending && pendingRoutineStorageKey !== routineInvocationStorageKeyValue) {
      setRoutineError("The saved invocation belongs to a different owner session. Refresh the authenticated session before retrying; no new request was created.");
      return;
    }
    let request: WorkBoardRoutineInvokeRequest;
    if (pending) {
      request = pending.request;
    } else {
      if (routine.state === "revoked") {
        setRoutineError("This procedure is revoked permanently. Create a new reviewed procedure from a fresh verified journey.");
        return;
      }
      if (routine.state !== "active" || !routinePackageReviewed) {
        setRoutineError("Invocation is blocked until this procedure is active and its installed package review is current.");
        return;
      }
      const selectedGoal = selectedRoutineInvocationGoal;
      const expectedGoalRevision = selectedGoal?.revision;
      if (!selectedGoal || selectedGoal.status !== "active"
        || typeof expectedGoalRevision !== "number" || !Number.isSafeInteger(expectedGoalRevision) || expectedGoalRevision < 1) {
        setRoutineError("Choose an active goal with a current revision before invoking this procedure.");
        return;
      }
      const selectedWatch = selectedRoutineWatch;
      if (!selectedWatch || selectedWatch.state !== "active") {
        setRoutineError("Invocation is blocked until an owner-visible active source watch is selected for the current goal.");
        return;
      }
      request = {
        version: routineVersion,
        expected_routine_revision: routine.revision,
        goal_id: selectedGoal.id,
        expected_goal_revision: expectedGoalRevision,
        source_watch_id: selectedWatch.id,
        expected_watch_revision: selectedWatch.plan_revision,
        invocation_uuid: makeIdempotencyKey(),
      };
    }
    const pendingRequest = pending ?? { routineId: routine.id, request };
    if (!pending) {
      const persistenceError = persistPendingRoutineInvocation(
        routineInvocationStorageKeyValue,
        pendingRequest,
      );
      if (persistenceError) {
        setRoutinePersistenceError(persistenceError);
        setRoutineError(persistenceError);
        return;
      }
      setRoutinePersistenceError(null);
      setPendingRoutineInvocation(pendingRequest);
      setPendingRoutineStorageKey(routineInvocationStorageKeyValue);
    }
    setRoutineInvokeBusy(true);
    setRoutineError(null);
    try {
      const receipt = await requestApi<WorkBoardRoutineInvokeReceipt>(
        `/api/capabilities/routines/${encodeURIComponent(routineId)}/invoke`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(request),
        },
      );
      if (stoppedRef.current) return;
      if (!receipt || typeof receipt.task_id !== "string" || !receipt.task_id) {
        throw new Error("The routine invocation did not return a canonical Todo task receipt.");
      }
      const cleared = clearPendingRoutineInvocation(routineInvocationStorageKeyValue);
      if (cleared) {
        setPendingRoutineInvocation(null);
        setPendingRoutineStorageKey(null);
        setRoutinePersistenceError(null);
      } else {
        setRoutinePersistenceError("The Todo task receipt was confirmed, but the exact retry record could not be cleared. Keep this panel open and retry only if the receipt is not visible after refresh.");
      }
      setRoutineInvokeReceipt(receipt);
      setAnnouncement(`The governed procedure created Todo task ${receipt.task_id}. The board is refreshing for dispatcher execution.`);
      await refreshSnapshot();
    } catch (error) {
      if (!stoppedRef.current) {
        const definitiveRejection = error instanceof WorkBoardApiError
          && error.status >= 400
          && error.status < 500;
        if (definitiveRejection) {
          const cleared = clearPendingRoutineInvocation(routineInvocationStorageKeyValue);
          if (cleared) {
            setPendingRoutineInvocation(null);
            setPendingRoutineStorageKey(null);
            setRoutinePersistenceError(null);
          } else {
            setRoutinePersistenceError("The server rejected this invocation, but the exact retry record could not be cleared. Keep the record until browser session storage is available again.");
          }
          setRoutineError(cleared
            ? inputErrorMessage(error)
            : `${inputErrorMessage(error)} The exact retry record is being preserved because it could not be cleared safely.`);
        } else {
          setRoutineError("The invocation receipt was not confirmed. Retry the unchanged request to reconcile it; no new invocation key will be generated.");
        }
        if (error instanceof WorkBoardApiError && error.status === 409) void readRoutine(routineId);
      }
    } finally {
      if (!stoppedRef.current) setRoutineInvokeBusy(false);
    }
  };

  const setBrowserPending = (pending: PendingBrowserSubmission | null) => {
    if (!pendingCreateScope) return;
    if (pending) {
      rememberPendingBrowserSubmission(pendingCreateScope, pending);
      return;
    }
    pendingBrowserSubmissions.delete(pendingCreateScope);
  };

  const closeBrowserTask = () => {
    if (pendingCreateScope && pendingBrowserSubmissions.has(pendingCreateScope)) {
      setAnnouncement("The public browser task outcome is unconfirmed. Keep the form open and retry the exact request before closing it.");
      return;
    }
    setBrowserTaskOpen(false);
  };

  const setCalendarPending = (pending: PendingCalendarSubmission | null) => {
    if (!pendingCreateScope) return;
    if (pending) {
      rememberPendingCalendarSubmission(pendingCreateScope, pending);
      return;
    }
    pendingCalendarSubmissions.delete(pendingCreateScope);
  };

  const closeCalendarPrep = () => {
    if (pendingCreateScope && pendingCalendarSubmissions.has(pendingCreateScope)) {
      setAnnouncement("The calendar preparation outcome is unconfirmed. Keep the form open and retry the exact request before closing it.");
      return;
    }
    setCalendarPrepOpen(false);
  };

  const setRepoRepairPending = (pending: PendingRepoRepairSubmission | null) => {
    if (!pendingCreateScope) return;
    if (pending) {
      rememberPendingRepoRepairSubmission(pendingCreateScope, pending);
      return;
    }
    pendingRepoRepairSubmissions.delete(pendingCreateScope);
  };

  const closeRepoRepair = () => {
    if (pendingCreateScope && pendingRepoRepairSubmissions.has(pendingCreateScope)) {
      setAnnouncement("The repository repair outcome is unconfirmed. Keep this form open and retry the exact request before closing it.");
      return;
    }
    setRepoRepairOpen(false);
  };

  return (
    <section className="cockpit-panel cockpit-panel--embedded min-w-0" aria-label="Work board">
      <div className="cockpit-operator-row flex-wrap">
        <div>
          <div className="cockpit-key">operator execution board</div>
          <div className="cockpit-operator-link" role="status" aria-live="polite">
            {connectionState === "connected" ? "Live events connected" : connectionState === "connecting" ? "Synchronizing snapshot and events" : connectionState === "denied" ? "Operator session required" : "Disconnected · last confirmed snapshot shown"}
            {stale ? " · stale" : ""} · {activeCount}/2 running
          </div>
        </div>
        <div className="cockpit-operator-actions flex-wrap">
          <button type="button" className="cockpit-feedback-button" onClick={openCreateDialog}>
            Create task
          </button>
          <button type="button" className="cockpit-feedback-button" onClick={() => { setBrowserTaskReceipt(null); setBrowserTaskOpen(true); }}>
            Public browser task
          </button>
          <button type="button" className="cockpit-feedback-button" onClick={() => setResearchOpen(true)}>
            Research dossier
          </button>
          <button type="button" className="cockpit-feedback-button" onClick={() => setFormatterOpen(true)}>Isolated JSON formatter</button>
          <button type="button" className="cockpit-feedback-button" onClick={() => { setCalendarPrepReceipt(null); setCalendarPrepOpen(true); }}>
            Calendar meeting prep
          </button>
          <button type="button" className="cockpit-feedback-button" onClick={() => { setRepoRepairReceipt(null); setRepoRepairOpen(true); }}>
            Repository repair
          </button>
          <button type="button" className="cockpit-feedback-button" onClick={() => void refreshSnapshot()} disabled={loading}>
            {loading ? "Refreshing…" : "Refresh board"}
          </button>
        </div>
      </div>

      <div className="mt-3 grid gap-2 md:grid-cols-[minmax(12rem,1fr)_12rem_12rem_auto]">
        <label className="text-xs">
          Search tasks
          <input
            type="search"
            aria-label="Search tasks"
            className="cockpit-input mt-1 w-full"
            value={searchText}
            onChange={(event) => setSearchText(event.currentTarget.value.slice(0, 200))}
            maxLength={200}
          />
        </label>
        <label className="text-xs">
          Status filter
          <select aria-label="Status filter" className="cockpit-input mt-1 w-full" value={statusFilter} onChange={(event) => setStatusFilter(event.currentTarget.value as "all" | WorkBoardStatus)}>
            {statusOptions.map((status) => <option key={status} value={status}>{status === "all" ? "All statuses" : STATUS_LABELS[status]}</option>)}
          </select>
        </label>
        <label className="text-xs">
          Assignee filter
          <select aria-label="Assignee filter" className="cockpit-input mt-1 w-full" value={assigneeFilter} onChange={(event) => setAssigneeFilter(event.currentTarget.value)}>
            <option value="all">All assignees</option>
            {assigneeOptions.map((assignee) => <option key={assignee} value={assignee}>{assignee}</option>)}
          </select>
        </label>
        <label className="flex items-end gap-2 pb-2 text-xs">
          <input type="checkbox" checked={showArchived} onChange={(event) => setShowArchived(event.currentTarget.checked)} />
          Include archived
        </label>
      </div>

      {stale && (
        <div className="mt-3 rounded border border-amber-500/50 bg-amber-950/20 p-3 text-sm" role="alert">
          <div>{boardError ?? "The board is showing the last server-confirmed snapshot."}</div>
          <button type="button" className="cockpit-feedback-button mt-2" onClick={() => void refreshSnapshot()}>
            Refresh canonical snapshot
          </button>
        </div>
      )}
      {!stale && boardError && (
        <div className="mt-3 rounded border border-red-500/50 bg-red-950/20 p-3 text-sm" role="alert">
          {boardError}
        </div>
      )}
      {moveFeedback && <div className="mt-3 rounded border border-amber-500/40 p-2 text-sm" role="alert">{moveFeedback}</div>}
      {goalError && <div className="mt-2 text-xs text-amber-300" role="status">Goal metadata unavailable: {goalError}</div>}
      {browserTaskReceipt && (
        <div className="mt-2 rounded border border-emerald-500/40 bg-emerald-950/20 p-2 text-sm" role="status">
          Public browser task input artifact verified: <span className="font-mono break-all">{browserTaskReceipt.artifactId}</span> · {browserTaskReceipt.actionCount} action{browserTaskReceipt.actionCount === 1 ? "" : "s"} · SHA-256 <span className="font-mono break-all">{browserTaskReceipt.digest}</span>. The selected task is open below for durable progress and recovery.
        </div>
      )}
      {calendarPrepReceipt && (
        <div className="mt-2 rounded border border-emerald-500/40 bg-emerald-950/20 p-2 text-sm" role="status">
          Calendar preparation task input artifact verified: <span className="font-mono break-all">{calendarPrepReceipt.input_artifact.artifact_id}</span> · SHA-256 <span className="font-mono break-all">{calendarPrepReceipt.input_artifact.typed_input_digest}</span>. The selected task is open below for durable progress and recovery.
        </div>
      )}
      {repoRepairReceipt && (
        <div className="mt-2 rounded border border-emerald-500/40 bg-emerald-950/20 p-2 text-sm" role="status">
          Repository repair input artifact verified: <span className="font-mono break-all">{repoRepairReceipt.artifactId}</span> · SHA-256 <span className="font-mono break-all">{repoRepairReceipt.digest}</span>. The selected task is open below for durable progress and recovery.
        </div>
      )}
      <div className="sr-only" aria-live="polite">{announcement}</div>

      {loading && tasks.length === 0 ? (
        <div className="cockpit-empty mt-4" role="status">Loading authenticated work board…</div>
      ) : (
        <div className="mt-4 overflow-x-auto pb-2" aria-label="Work board columns">
          <div className="grid min-w-max grid-flow-col auto-cols-[minmax(14rem,18rem)] gap-3">
            {BOARD_COLUMNS.map((status) => {
              const columnTasks = tasksByStatus.get(status) ?? [];
              return (
                <section
                  key={status}
                  aria-label={`${STATUS_LABELS[status]} column`}
                  className="min-h-40 rounded border border-white/10 bg-black/10 p-2"
                  onDragOver={(event) => event.preventDefault()}
                  onDrop={dropTask(status)}
                >
                  <div className="mb-2 flex items-center justify-between border-b border-white/10 pb-2">
                    <h3 className="text-xs font-semibold uppercase tracking-wide">{STATUS_LABELS[status]}</h3>
                    <span className="text-[10px] opacity-70">{columnTasks.length}</span>
                  </div>
                  <div className="grid gap-2" role="list" aria-label={`${STATUS_LABELS[status]} tasks`}>
                    {columnTasks.map((task) => {
                      const active = isActiveAttempt(task);
                      const latest = task.latest_attempt;
                      return (
                        <article
                          key={task.task_id}
                          role="listitem"
                          draggable={task.ownership_access !== "recovered_read_only"}
                          onDragStart={(event) => {
                            if (task.ownership_access === "recovered_read_only") { event.preventDefault(); return; }
                            dragTaskIdRef.current = task.task_id;
                            event.dataTransfer.setData("text/plain", task.task_id);
                            event.dataTransfer.effectAllowed = "move";
                          }}
                          className="rounded border border-white/10 bg-slate-950/60 p-3 text-xs"
                        >
                          {task.ownership_access === "recovered_read_only" && <div className="mb-2 text-amber-200">Recovered original · read only</div>}
                          <button type="button" className="w-full text-left" onClick={() => openTask(task.task_id)} aria-label={`Open task ${task.title}`}>
                            <div className="flex items-start justify-between gap-2">
                              <span className="break-all font-mono text-[10px] opacity-70">{task.task_id}</span>
                              <span className="shrink-0">P{task.priority}</span>
                            </div>
                            <div className="mt-1 font-medium">{task.title}</div>
                            <div className="mt-2 space-y-1 text-[10px] opacity-80">
                              <div>Executor: {task.executor_id ?? "Unassigned"} · Assignee: {task.assignee_id ?? "Unassigned"}</div>
                              <div>Goal: {task.goal_id} · Dependencies: {task.completed_dependency_count}/{task.dependency_count}</div>
                              <div>Latest attempt: {attemptLabel(task)} · Age: {formatAge(task.created_at)}</div>
                              {status === "ready" && task.dispatch_rank !== null && <div>Server dispatch rank: #{task.dispatch_rank}</div>}
                              {task.dispatch_wait_reason && <div className="text-amber-200">Dispatch waiting: {task.dispatch_wait_reason === "browser_cleanup_required" ? "browser cleanup recovery is required" : task.dispatch_wait_reason}</div>}
                              {status === "running" && <div>Lease: {active ? safeDateTime(latest?.lease_expires_at) : "not active"} · started {formatAge(latest?.started_at)}</div>}
                              {status === "blocked" && <div className="text-amber-200">Blocked: {task.block_reason || "The server did not provide a safe reason."}</div>}
                              {status === "review" && <div>Reviewer: {task.reviewer_id ?? "Not named"} · Readback: {READBACK_LABELS[task.readback_status]} · Verification: {VERIFICATION_LABELS[task.verification_status]}</div>}
                            </div>
                          </button>
                          {task.status === "running" && task.recovery_action === "cancel" && (
                            <button type="button" className="cockpit-feedback-button mt-2" onClick={() => openTask(task.task_id)}>
                              Open cancellation controls
                            </button>
                          )}
                          {task.status === "blocked" && task.recovery_action && (
                            <div className="mt-2 text-[10px] opacity-80">Recovery: {RECOVERY_LABELS[task.recovery_action]}</div>
                          )}
                        </article>
                      );
                    })}
                    {columnTasks.length === 0 && <div className="cockpit-empty py-3 text-[10px]">No {STATUS_LABELS[status].toLowerCase()} tasks.</div>}
                  </div>
                </section>
              );
            })}
          </div>
        </div>
      )}

      {(showArchived || statusFilter === "archived") && (
        <section className="mt-4 rounded border border-white/10 p-3" aria-label="Archived tasks">
          <h3 className="text-xs font-semibold uppercase tracking-wide">Archived tasks · {archivedTasks.length}</h3>
          <div className="mt-2 grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
            {archivedTasks.map((task) => (
              <button key={task.task_id} type="button" className="cockpit-row-button text-left" onClick={() => openTask(task.task_id)}>
                <span className="font-mono text-[10px]">{task.task_id}</span> · {task.title}
              </button>
            ))}
            {archivedTasks.length === 0 && <div className="cockpit-empty">No archived tasks match the current filters.</div>}
          </div>
        </section>
      )}

      {selectedTask && createPortal(
        <aside hidden={createOpen || browserTaskOpen || researchOpen || formatterOpen || calendarPrepOpen || repoRepairOpen} ref={taskDetailPanelRef} role="region" aria-label={`Task details for ${selectedTask.title}`} tabIndex={-1} className="fixed inset-y-0 right-0 z-[190] h-full w-full max-w-2xl overflow-y-auto border-l border-white/15 bg-slate-950 p-4 text-slate-100 shadow-2xl">
            <div className="sticky top-0 z-10 -mx-4 -mt-4 mb-4 flex flex-wrap items-center justify-between gap-2 border-b border-white/10 bg-slate-950/95 px-4 py-3 backdrop-blur">
              <div className="min-w-0 flex-1 break-words">
                <div className="text-[10px] uppercase tracking-wide opacity-70">{STATUS_LABELS[selectedTask.status]} · revision {selectedTask.task_revision}</div>
                <h2 id="work-board-detail-title" className="text-lg font-semibold">{selectedTask.title}</h2>
              </div>
              <div className="flex flex-wrap justify-end gap-2">
                {attentionContext?.taskId === selectedTask.task_id && onReturnAttention && <button type="button" className="cockpit-feedback-button" onClick={() => { closeTask(); onReturnAttention(); }}>Return to {attentionContext.origin === "home" ? "Home attention" : "Inbox decision"}</button>}
                <button type="button" className="cockpit-feedback-button" aria-label="Close task details" onClick={closeTask}>Close</button>
              </div>
            </div>
            {attentionContext?.taskId === selectedTask.task_id && <div className="mb-3 flex flex-wrap gap-2" aria-label="Originating context">
              {attentionContext.goalId && onOpenAttentionGoal && <button type="button" className="cockpit-feedback-button" onClick={onOpenAttentionGoal}>Open originating goal</button>}
              {attentionContext.threadId && onOpenAttentionThread && <button type="button" className="cockpit-feedback-button" onClick={onOpenAttentionThread}>Open originating thread</button>}
            </div>}
            {selectedTask.ownership_access === "recovered_read_only" && <div role="status" className="mb-3 text-amber-200">Recovered original · read only. Previous approvals, jobs and permissions stay blocked. Create fresh reviewed intent through operator ownership recovery.</div>}
            <fieldset disabled={selectedTask.ownership_access === "recovered_read_only"}>
            {(detailLoading || stale) && <div className="mb-3 text-xs text-amber-200" role="status">{detailLoading ? "Refreshing task detail…" : "Showing the last confirmed task detail."}</div>}
            {detailError && <div className="mb-3 rounded border border-red-500/40 p-2 text-sm" role="alert">{detailError}<button type="button" className="ml-2 underline" onClick={() => void refreshSelectedTask()}>Refresh detail</button></div>}
            {actionError && <div className="mb-3 rounded border border-amber-500/40 p-2 text-sm" role="alert">{actionError}</div>}
            {selectedTask.dispatch_wait_reason && <div className="mb-3 rounded border border-amber-500/40 bg-amber-950/20 p-2 text-sm" role="status">Dispatch waiting: {selectedTask.dispatch_wait_reason === "browser_cleanup_required" ? "browser cleanup recovery is required before this task can run." : selectedTask.dispatch_wait_reason}</div>}
            {selectedInboxOrigin && (
              <section className="mb-3 rounded border border-cyan-400/30 bg-cyan-950/10 p-3 text-xs" aria-label="Inbox origin">
                <div className="font-semibold">Created from Inbox candidate</div>
                <div className="mt-1 break-all">Candidate {selectedInboxOrigin.id} · accepted</div>
                {onOpenInboxCandidate && (
                  <button
                    type="button"
                    className="cockpit-feedback-button mt-2"
                    onClick={() => onOpenInboxCandidate(selectedInboxOrigin)}
                  >
                    Review Inbox decision
                  </button>
                )}
              </section>
            )}

            <div className="grid gap-3 text-xs">
              <section className="rounded border border-white/10 p-3">
                <div className="font-semibold">Goal and authority</div>
                <div className="mt-1 break-all">Goal {selectedTask.goal_id} · revision {selectedTask.goal_revision}</div>
                <div>Owner {selectedTask.owner_principal_id} · session {selectedTask.owner_session_id}</div>
                <div>Capability {selectedTask.capability_id ?? "Not specified"} · executor {selectedTask.executor_id ?? "Unassigned"}</div>
                <div>Input reference {selectedTask.typed_input_ref ?? "Not specified"}</div>
                <div className="break-all">Input digest {selectedTask.typed_input_digest ?? "Not specified"}</div>
                <div>Runtime limit {detailLimit ? `${detailLimit.effective_max_runtime_seconds}s (${detailLimit.limit_source}; hard cap ${detailLimit.hard_max_runtime_seconds}s)` : detailLimitError ?? "Checking current goal limit…"}</div>
                {detailLimitError && <div className="mt-1 text-amber-200">{detailLimitError}</div>}
                {detailLimit && (
                  <label className="mt-2 flex items-start gap-2">
                    <input type="checkbox" checked={detailLimitAcknowledged} onChange={(event) => setDetailLimitAcknowledged(event.currentTarget.checked)} />
                    <span>I acknowledge the server-derived runtime limit of {detailLimit.effective_max_runtime_seconds} seconds. It cannot be raised here.</span>
                  </label>
                )}
              </section>

              {!editMode ? (
                <section className="rounded border border-white/10 p-3">
                  <div className="flex items-center justify-between gap-2">
                    <div className="font-semibold">Task specification</div>
                    <button
                      type="button"
                      className="cockpit-feedback-button"
                      onClick={openEditMode}
                      disabled={busyAction || ["running", "review", "done", "archived"].includes(selectedTask.status)}
                      title={["running", "review", "done", "archived"].includes(selectedTask.status) ? "This task state does not allow specification edits." : undefined}
                    >Edit bounded fields</button>
                  </div>
                  <p className="mt-2 whitespace-pre-wrap break-words">{selectedTask.body || "No task description."}</p>
                  <div className="mt-2">Priority {selectedTask.priority} · Assignee {selectedTask.assignee_id ?? "Unassigned"} · Scheduled {safeDateTime(selectedTask.scheduled_at)}</div>
                  <div>Review {selectedTask.requires_review ? `required · ${selectedTask.reviewer_id ?? "reviewer not named"}` : "not required"}</div>
                </section>
              ) : editDraft && (
                <form className="grid gap-2 rounded border border-white/10 p-3" onSubmit={(event) => void saveTaskEdits(event)}>
                  <div className="font-semibold">Edit bounded task fields</div>
                  <label>Title<input className="cockpit-input mt-1 w-full" maxLength={200} required value={editDraft.title} onChange={(event) => setEditDraft({ ...editDraft, title: event.currentTarget.value })} /></label>
                  <label>Description<textarea className="cockpit-input mt-1 w-full" maxLength={4000} rows={3} value={editDraft.body} onChange={(event) => setEditDraft({ ...editDraft, body: event.currentTarget.value })} /></label>
                  <label>Priority 0–100<input className="cockpit-input mt-1 w-full" type="number" min={0} max={100} value={editDraft.priority} onChange={(event) => setEditDraft({ ...editDraft, priority: event.currentTarget.value })} /></label>
                  <label>Capability ID<input className="cockpit-input mt-1 w-full" maxLength={128} value={editDraft.capabilityId} onChange={(event) => setEditDraft({ ...editDraft, capabilityId: event.currentTarget.value })} /></label>
                  <label>Typed input reference<input className="cockpit-input mt-1 w-full" maxLength={512} value={editDraft.typedInputRef} onChange={(event) => setEditDraft({ ...editDraft, typedInputRef: event.currentTarget.value })} /></label>
                  <label>Typed input SHA-256<input className="cockpit-input mt-1 w-full font-mono" maxLength={64} value={editDraft.typedInputDigest} onChange={(event) => setEditDraft({ ...editDraft, typedInputDigest: event.currentTarget.value })} /></label>
                  <label>Executor ID<input className="cockpit-input mt-1 w-full" maxLength={128} value={editDraft.executorId} onChange={(event) => setEditDraft({ ...editDraft, executorId: event.currentTarget.value })} /></label>
                  <label>Assignee ID<input className="cockpit-input mt-1 w-full" maxLength={128} value={editDraft.assigneeId} onChange={(event) => setEditDraft({ ...editDraft, assigneeId: event.currentTarget.value })} /></label>
                  <label>Scheduled at<input className="cockpit-input mt-1 w-full" type="datetime-local" value={editDraft.scheduledAt} onChange={(event) => setEditDraft({ ...editDraft, scheduledAt: event.currentTarget.value })} /></label>
                  <div className="flex gap-2"><button type="submit" className="cockpit-feedback-button" disabled={busyAction}>{busyAction ? "Saving…" : "Save with current revision"}</button><button type="button" className="cockpit-feedback-button" onClick={() => setEditMode(false)}>Cancel edit</button></div>
                </form>
              )}

              {selectedTask.capability_id === "calendar.meeting-prep.v1" && (
                <section className="rounded border border-cyan-400/30 bg-cyan-950/10 p-3" aria-label="Calendar meeting preparation execution">
                  <div className="font-semibold">Calendar preparation execution</div>
                  {!currentCalendarExecution ? (
                    <div className="mt-1 text-amber-200">Execution receipt unavailable. The board will not infer provider, model, artifact, or readback success.</div>
                  ) : (
                    <div className="mt-2 grid gap-1">
                      <div>Durable job <span className="font-mono break-all">{currentCalendarExecution.job_id}</span> · {currentCalendarExecution.durable_status}</div>
                      <div>Read 1: {currentCalendarExecution.read_1?.status ?? "unavailable"} · Read 2: {currentCalendarExecution.read_2?.status ?? "unavailable"}</div>
                      <div>Effective route: {currentCalendarExecution.effective_route ? `${currentCalendarExecution.effective_route.runtime_path} · ${currentCalendarExecution.effective_route.provider} · ${currentCalendarExecution.effective_route.model}` : "not confirmed"}</div>
                      <div>Memory: {currentCalendarExecution.memory_status ?? "unavailable"} · verified {safeDateTime(currentCalendarExecution.verified_at)}</div>
                      {currentCalendarExecution.failure_code && <div className="text-amber-200">Failure: {currentCalendarExecution.failure_code}{currentCalendarExecution.recovery_action ? ` · recovery ${currentCalendarExecution.recovery_action}` : ""}</div>}
                      {(() => {
                        const reference = calendarExecutionReference(currentCalendarExecution);
                        return reference && onInspectArtifact ? <button type="button" className="cockpit-feedback-button mt-2 justify-self-start" onClick={() => onInspectArtifact({ reference, ownerSessionId: selectedTask.owner_session_id, workflowRunId: currentCalendarExecution.job_id, parentWorkflowRunId: currentAttempt?.workflow_run_id ?? null })}>Inspect verified calendar artifact</button> : <div className="text-amber-200">Verified artifact/readback is unavailable.</div>;
                      })()}
                    </div>
                  )}
                </section>
              )}

              {selectedTask.capability_id === "engineering.repo-repair.v1"
                && currentAttempt?.workflow_run_id
                && (
                  <RepoRepairInspector
                    jobId={currentAttempt.workflow_run_id}
                    onOpenApprovals={onOpenApprovals}
                    ownerPrincipalId={ownerPrincipalId}
                    ownerSessionId={ownerSessionId}
                    taskOwnerPrincipalId={selectedTask.owner_principal_id}
                    taskOwnerSessionId={selectedTask.owner_session_id}
                  />
                )}

              {selectedTask.capability_id === "work.mail-reply-draft.v1" && (
                <MailPanel
                  taskId={selectedTask.task_id}
                  ownerPrincipalId={ownerPrincipalId}
                  ownerSessionId={ownerSessionId}
                />
              )}

              {selectedTask.capability_id !== "work.mail-reply-draft.v1" && selectedInboxOrigin?.mail && (
                <MailPanel
                  taskId={selectedTask.task_id}
                  ownerPrincipalId={ownerPrincipalId}
                  ownerSessionId={ownerSessionId}
                  mailOrigin={selectedInboxOrigin.mail}
                  goalId={selectedInboxOrigin.goal_id}
                  goalRevision={selectedInboxOrigin.goal_revision}
                />
              )}

              <section className="rounded border border-white/10 p-3">
                <div className="font-semibold">Actions and recovery</div>
                <div className="mt-1">{selectedTask.status === "blocked" ? `Blocked: ${selectedTask.block_reason || "No safe reason was supplied."}` : `Current state: ${STATUS_LABELS[selectedTask.status]}`}</div>
                {selectedTask.recovery_action && <div className="mt-1">Server recovery action: {RECOVERY_LABELS[selectedTask.recovery_action]}</div>}
                {selectedTask.ownership_access !== "recovered_read_only" && ownerPrincipalId && ownerSessionId && (selectedTask.recovery_action === "approve_existing_run" || (attentionContext?.taskId === selectedTask.task_id && attentionContext.approvalId)) && <TaskApprovalReview
                  task={selectedTask} owner={{ principalId: ownerPrincipalId, sessionId: ownerSessionId }} approvalId={attentionContext?.taskId === selectedTask.task_id ? attentionContext.approvalId : null}
                  metadataConfirmed={Boolean(selectedDetail) && !detailLoading && !stale && !detailError} onRefresh={refreshSelectedTask}
                />}
                {selectedTask.ownership_access !== "recovered_read_only" && ownerPrincipalId && ownerSessionId && selectedTask.recovery_action === "reconcile_external_effect" && <TaskEffectRecovery task={selectedTask} owner={{ principalId: ownerPrincipalId, sessionId: ownerSessionId }} metadataConfirmed={Boolean(selectedDetail) && !detailLoading && !stale && !detailError} onRefresh={refreshSelectedTask} onOpenAccounting={onOpenAccounting} />}
                {selectedTask.capability_id === "guardian-routine.v1" && routinePublication && (
                  <section className="mt-3 rounded border border-amber-500/40 bg-amber-950/10 p-3" aria-label="Routine publication recovery">
                    <div className="font-semibold">Governed publication recovery</div>
                    <div className="mt-1 text-[11px] opacity-80">
                      Parent {routinePublication.parent_workflow_run_id ?? "unavailable"} · attempt {routinePublication.attempt_id} · M3 {routinePublication.m3_status ?? "not prepared"}
                    </div>
                    {!routinePublication.m3_job_id ? (
                      <>
                        <div className="mt-2">Inspect and edit the bounded publication text before preparing the exact M3 approval.</div>
                        <label className="mt-2 block">Publication title (optional)
                          <input className="cockpit-input mt-1 w-full" maxLength={160} value={routinePublicationTitle} onChange={(event) => setRoutinePublicationTitle(event.currentTarget.value)} disabled={routinePublicationBusy} />
                        </label>
                        <label className="mt-2 block">Publication body
                          <textarea className="cockpit-input mt-1 w-full" maxLength={32000} rows={5} value={routinePublicationBody} onChange={(event) => setRoutinePublicationBody(event.currentTarget.value)} disabled={routinePublicationBusy} />
                        </label>
                        <button type="button" className="cockpit-feedback-button mt-2" disabled={routinePublicationBusy || !routinePublicationBody.trim()} onClick={() => void prepareRoutinePublication()}>
                          {routinePublicationBusy ? "Preparing preview…" : "Prepare exact publication preview"}
                        </button>
                      </>
                    ) : (
                      <>
                        <div className="mt-2">The M3 preview is immutable and must be approved separately.</div>
                        {routinePublication.preview?.title !== undefined && <div className="mt-2"><span className="font-semibold">Title:</span> {routinePublication.preview.title || "(none)"}</div>}
                        {routinePublication.preview?.body !== undefined && <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded bg-black/20 p-2 text-[11px]">{routinePublication.preview.body}</pre>}
                        <div className="mt-2 font-mono text-[11px]">Approval {routinePublication.approval_id ?? "unavailable"} · {routinePublication.approval_status ?? "status unavailable"}</div>
                        <div className="mt-2 flex flex-wrap gap-2">
                          {onOpenApprovals && <button type="button" className="cockpit-feedback-button" onClick={onOpenApprovals}>Open Pending approvals</button>}
                          <button type="button" className="cockpit-feedback-button" disabled={routinePublicationBusy} onClick={() => void readRoutinePublication(selectedTask.task_id, selectedTask.task_revision)}>Refresh approval state</button>
                          <button type="button" className="cockpit-feedback-button" disabled={routinePublicationBusy || routinePublication.approval_status !== "approved"} onClick={() => void resumeRoutinePublication()} title={routinePublication.approval_status !== "approved" ? "Approve the exact M3 preview first." : undefined}>Resume approved publication</button>
                        </div>
                      </>
                    )}
                    {routinePublicationError && <div className="mt-2 rounded border border-red-500/40 p-2" role="alert">{routinePublicationError}</div>}
                  </section>
                )}
                {selectedTask.status === "review" && selectedTask.review_expires_at && (
                  <div className="mt-1" role="status">
                    Review deadline: {safeDateTime(selectedTask.review_expires_at)}
                    {new Date(selectedTask.review_expires_at).getTime() <= Date.now() ? " · expired; renewal is required" : ""}
                  </div>
                )}
                <div className="mt-2 flex flex-wrap gap-2">
                  {selectedTask.status === "triage" && (
                    <button type="button" className="cockpit-feedback-button" disabled={!canPromote || busyAction} onClick={() => void performAction("promote")} title={!canPromote ? "Complete the typed specification and acknowledge the current server limit first." : undefined}>Promote to Todo</button>
                  )}
                  {["triage", "todo"].includes(selectedTask.status) && currentOwnerSession && (
                    <button type="button" className="cockpit-feedback-button" disabled={busyAction || proposalBusy} onClick={() => void requestProposal("specify")}>Specify for review</button>
                  )}
                  {selectedTask.status === "todo" && currentOwnerSession && (
                    <button type="button" className="cockpit-feedback-button" disabled={busyAction || proposalBusy} onClick={() => void requestProposal("decompose")}>Decompose for review</button>
                  )}
                  {selectedTask.status === "running" && currentOwnerSession && (
                    <button
                      type="button"
                      className="cockpit-feedback-button"
                      disabled={busyAction || !currentAttempt?.attempt_id || !currentAttempt.workflow_run_id}
                      title={!currentAttempt?.attempt_id || !currentAttempt.workflow_run_id ? "Wait for the active fenced attempt with its linked durable workflow run." : undefined}
                      onClick={requestReview}
                    >Request review</button>
                  )}
                  {selectedTask.status === "blocked" && selectedTask.recovery_action === "unblock" && (
                    <form className="flex min-w-full flex-col gap-2" onSubmit={(event) => { event.preventDefault(); const resolution = unblockResolution.trim(); if (!resolution || resolution.length > 1000) { setActionError("Enter a resolution between 1 and 1000 characters."); return; } void performAction("unblock", { resolution }); }}>
                      <label>Resolution<textarea className="cockpit-input mt-1 w-full" maxLength={1000} rows={2} value={unblockResolution} onChange={(event) => setUnblockResolution(event.currentTarget.value)} /></label>
                      <button type="submit" className="cockpit-feedback-button self-start" disabled={busyAction || !unblockResolution.trim() || unblockResolution.trim().length > 1000}>Unblock after rechecking authority</button>
                    </form>
                  )}
                  {selectedTask.status === "blocked" && ["retry", "restore_prerequisite"].includes(selectedTask.recovery_action ?? "") && !isActiveAttempt(selectedTask) && (
                    <button
                      type="button"
                      className="cockpit-feedback-button"
                      disabled={busyAction || !canRetry}
                      title={!canRetry ? "Acknowledge the current server-derived runtime limit before retrying." : undefined}
                      onClick={() => void performAction("retry", {}, true)}
                    >{selectedTask.recovery_action === "restore_prerequisite" ? "Retry after rechecking prerequisites (new attempt)" : "Retry (new attempt)"}</button>
                  )}
                  {selectedTask.status === "blocked" && ["retry", "restore_prerequisite"].includes(selectedTask.recovery_action ?? "") && !canRetry && (
                    <div className="w-full text-amber-200" role="status">
                      Retry stays disabled until the current goal revision limit is loaded and acknowledged. {detailLimitError ?? "Check the current runtime limit above."}
                    </div>
                  )}
                  {selectedTask.status === "blocked" && selectedTask.recovery_action !== "retry" && selectedTask.recovery_action !== "restore_prerequisite" && selectedTask.recovery_action !== "unblock" && selectedTask.recovery_action && (
                    <div className="w-full rounded bg-amber-950/20 p-2" role="status">
                      {selectedTask.recovery_action === "approve_existing_run" ? (
                        <><span>Continue in the existing authenticated approval surface.</span>{onOpenApprovals && <button type="button" className="ml-2 underline" onClick={onOpenApprovals}>Open approvals</button>}</>
                      ) : <span>{RECOVERY_LABELS[selectedTask.recovery_action]}. Resolve and recheck the server-side prerequisite; this board will not guess a recovery route.</span>}
                    </div>
                  )}
                  {selectedTask.status === "running" && selectedTask.recovery_action === "cancel" && (
                    <button type="button" className="cockpit-feedback-button" disabled={busyAction || Boolean(selectedTask.cancel_requested_at)} onClick={() => void performAction("cancel", {}, true)}>{selectedTask.cancel_requested_at ? "Cancellation pending" : "Request durable cancellation"}</button>
                  )}
                  {selectedTask.status === "done" && (
                    <button type="button" className="cockpit-feedback-button" disabled={busyAction} onClick={() => void performAction("archive", {}, true)}>Archive completed task</button>
                  )}
                  {canManuallyBlock(selectedTask) && (
                    <form className="flex min-w-full flex-col gap-2" onSubmit={(event) => { event.preventDefault(); if (!blockConfirmed) { setActionError("Confirm the operator block before submitting."); return; } const reason = blockReason.trim(); if (!reason || reason.length > 500) { setActionError("Enter a block reason between 1 and 500 characters."); return; } void performAction("block", { block_kind: "operator", source_status: selectedTask.status, reason }); }}>
                      <label>Manual block reason<textarea className="cockpit-input mt-1 w-full" maxLength={500} rows={2} value={blockReason} onChange={(event) => setBlockReason(event.currentTarget.value)} /></label>
                      <label className="flex items-center gap-2"><input type="checkbox" checked={blockConfirmed} onChange={(event) => setBlockConfirmed(event.currentTarget.checked)} />Confirm this operator block</label>
                      <button type="submit" className="cockpit-feedback-button self-start" disabled={busyAction || !blockConfirmed || !blockReason.trim() || blockReason.trim().length > 500}>Block task</button>
                    </form>
                  )}
                  {selectedTask.status === "review" && (
                    <div className="w-full rounded bg-slate-900 p-2">
                      <div>Awaiting {selectedTask.reviewer_id ?? "the named reviewer"}. Evidence: {READBACK_LABELS[selectedTask.readback_status]} readback · {VERIFICATION_LABELS[selectedTask.verification_status]} verification.</div>
                      {namedReviewer ? (
                        <div className="mt-2 grid gap-2">
                          <div className="text-[10px] opacity-80">You are the authenticated named reviewer for this task. Approval requires the current revision and the same verified attempt.</div>
                          <div className="flex flex-wrap gap-2">
                            <button type="button" className="cockpit-feedback-button" disabled={busyAction || !currentAttempt?.attempt_id} onClick={() => currentAttempt?.attempt_id && void performAction("complete_review", { attempt_id: currentAttempt.attempt_id }, true)}>Approve review</button>
                            <button type="button" className="cockpit-feedback-button" disabled={busyAction} onClick={requestChanges}>Request changes</button>
                          </div>
                          <label>Required changes<textarea className="cockpit-input mt-1 w-full" maxLength={500} rows={2} value={reviewChangesReason} onChange={(event) => setReviewChangesReason(event.currentTarget.value)} /></label>
                        </div>
                      ) : <div className="mt-1 text-[10px] opacity-75">Reviewer verdict controls appear only for the exact authenticated owner session named by the server.</div>}
                    </div>
                  )}
                  {selectedTask.status === "blocked" && selectedTask.block_kind === "review_expired" && (
                    <div className="w-full rounded bg-amber-950/20 p-2" role="status">
                      <div>This review expired. Generic unblock and retry cannot restore it; the named reviewer must renew the same verified attempt.</div>
                      {namedReviewer && <button type="button" className="cockpit-feedback-button mt-2" disabled={busyAction} onClick={() => void performAction("renew_review", {}, true)}>Renew review window</button>}
                    </div>
                  )}
                  {selectedTask.status === "blocked" && selectedTask.block_kind === "attempt_limit" && (
                    <div className="w-full rounded bg-amber-950/20 p-2" role="status">
                      <div>This task has used its two-attempt limit. Retry is unavailable; create a new linked task for further work.</div>
                    </div>
                  )}
                </div>
                {proposalError && <div className="mt-2 rounded border border-amber-500/40 p-2" role="alert">{proposalError}</div>}
                {proposal && (
                  <section className="mt-3 rounded border border-white/10 bg-black/20 p-3" aria-label="Triage proposal preview">
                    <div className="flex items-center justify-between gap-2">
                      <div className="font-semibold">{proposal.kind === "specify" ? "Specify" : "Decompose"} proposal preview</div>
                      <span className="text-[10px] uppercase opacity-70">{proposalStatusLabel(proposal)} · revision {proposal.proposal_revision}</span>
                    </div>
                    {proposal.blocked_reason && <div className="mt-2 rounded border border-amber-500/40 p-2" role="status">
                      Blocked: {proposal.blocked_reason}. {proposal.recovery_action === "reconcile_external_effect"
                        ? "Reconcile the durable provider receipt before any retry; this page will not replay a contacted request."
                        : proposal.recovery_action === "reconcile_admission_binding"
                          ? "The bounded durable admission needs operator reconciliation; this idempotency key cannot be retried. Resolve the receipt or create a new explicitly accepted request after recovery."
                          : "Resolve the prerequisite, then retry the unchanged request only when its durable receipt proves that provider contact never started; a changed binding requires a new request key."}
                    </div>}
                    {proposal.recovery_action === "retry_same_binding_after_prerequisite"
                      && selectedTask
                      && ["triage", "todo"].includes(selectedTask.status)
                      && currentOwnerSession
                      && (
                        <button
                          type="button"
                          className="cockpit-feedback-button mt-2"
                          disabled={proposalBusy}
                          onClick={() => void requestProposal(proposal.kind, true)}
                        >Retry with new request key</button>
                      )}
                    <div className="mt-2">Estimated cost: {proposal.estimated_cost ?? "Not provided"}</div>
                    <div className="mt-1 break-all">Proposal capability: {proposal.capability_id ?? "Governed proposal route"}{proposal.capability_version ? ` · version ${proposal.capability_version}` : ""}</div>
                    <div className="mt-1">Parent revision: {proposal.parent_revision} · expires {safeDateTime(proposal.expires_at)}</div>
                    <div className="mt-2 grid gap-2">
                      {proposal.proposed_tasks.map((proposedTask, index) => (
                        <article key={proposedTask.task_id ?? `${proposal.proposal_id}:task:${index}`} className="rounded border border-white/10 p-2">
                          <div className="font-medium">{proposedTask.title}</div>
                          {proposedTask.body && <div className="mt-1 whitespace-pre-wrap break-words">{proposedTask.body}</div>}
                          <div className="mt-1">Capability: {proposedTask.capability_id} · version {proposedTask.capability_version ?? "current"}</div>
                          <div className="break-all">Typed input: {proposedTask.typed_input_ref ?? "Not provided"} · digest {proposedTask.typed_input_digest ?? "Not provided"}</div>
                          <div>Executor: {proposedTask.executor_id} · authority: {hasServerAuthorityPreview(proposedTask.authority) ? proposedTask.authority : "unavailable; this preview cannot be accepted"}</div>
                          {proposedTask.dependencies?.length ? <div>Dependencies: {proposedTask.dependencies.join(", ")}</div> : <div>Dependencies: none proposed</div>}
                          {proposedTask.cost_estimate && <div>Task cost estimate: {proposedTask.cost_estimate}</div>}
                        </article>
                      ))}
                      {proposal.proposed_tasks.length === 0 && <div className="cockpit-empty">No executable task proposal was returned.</div>}
                    </div>
                    {proposal.proposed_links.length > 0 && <div className="mt-2">Proposed dependencies: {proposal.proposed_links.map((link) => `${link.parent_task_id} → ${link.child_task_id}`).join(" · ")}</div>}
                    {proposal.status === "proposed" && !proposalAuthorityComplete && (
                      <div className="mt-2 text-amber-200" role="status">A complete server-derived authority preview is missing. Request a fresh proposal before accepting this one.</div>
                    )}
                    {proposal.status === "proposed" && (
                      <>
                      {proposal.kind === "specify" && <SpecificationEvidenceReview
                        key={specificationScope(selectedTask, proposal, ownerSessionId)} task={selectedTask}
                        proposal={proposal} ownerSessionId={ownerSessionId} onReplacement={updateProposalEvidence} />}
                      <div className="mt-3 flex flex-wrap gap-2">
                        <button type="button" className="cockpit-feedback-button" disabled={proposalBusy || !proposalAuthorityComplete} onClick={() => void decideProposal("accept")}>Accept proposal</button>
                        <button type="button" className="cockpit-feedback-button" disabled={proposalBusy} onClick={() => void decideProposal("reject")}>Reject proposal</button>
                      </div>
                      </>
                    )}
                    {proposal.status === "pending_inference" && <div className="mt-2 text-amber-200" role="status">The governed proposal request is pending. Refresh or retry the same request only after its durable receipt is reconciled.</div>}
                  </section>
                )}
                {selectedTask.status === "blocked" && (selectedTask.block_kind === "unknown_effect" || selectedTask.block_kind === "cost_liability" || selectedTask.recovery_action === "reconcile_external_effect") && (
                  <div className="mt-2 rounded border border-amber-500/40 p-2" role="status">External effect or cost is unresolved. Reconcile independent readback before any new attempt; retry is unavailable.</div>
                )}
                {isActiveAttempt(selectedTask) && <div className="mt-2 text-[10px] opacity-75">Attempt is active; manual block and retry are disabled until the durable run is reconciled.</div>}
              </section>

              <section className="rounded border border-white/10 p-3" aria-label="Governed procedure from verified journey">
                <div className="font-semibold">Reuse this verified journey as a governed procedure</div>
                <p className="mt-1 text-[10px] opacity-75">Only the fixed research-watch → GitHub follow-through journey can be reused here. The server rechecks ownership, Done state, dependency linkage, workflow readback, memory outcome, and current authority. Source bodies, approvals, credentials, and private content are never included in this preview.</p>
                {!currentOwnerSession && <div className="mt-2 rounded border border-amber-500/40 p-2" role="status">This control is available only to the authenticated owner session of the selected card.</div>}
                {currentOwnerSession && routineRecords.length > 0 && (
                  <div className="mt-2 rounded border border-white/10 bg-black/20 p-2">
                    <label>Existing governed procedure
                      <select
                        aria-label="Existing governed procedure"
                        className="cockpit-input mt-1 w-full"
                        value={selectedRoutineId}
                        disabled={routineBusy || routineInvokeBusy}
                        onChange={(event) => { void selectExistingRoutine(event.currentTarget.value); }}
                      >
                        <option value="">Create from a verified journey</option>
                        {routineRecords.map((record) => (
                          <option key={record.id} value={record.id} disabled={record.ownership_access === "recovered_read_only"}>
                            {record.name}{record.ownership_access === "recovered_read_only" ? " · recovered read only" : ""} · {record.state} · revision {record.revision}
                          </option>
                        ))}
                      </select>
                    </label>
                    <div className="mt-1 text-[10px] opacity-75">Saved procedures are loaded from the authenticated owner/session workspace. Selection shows only the routine name, state, revision, and version metadata.</div>
                  </div>
                )}
                {routineListError && <div className="mt-2 rounded border border-amber-500/40 p-2" role="status">Saved procedures could not be loaded: {routineListError}</div>}
                {currentOwnerSession && (verifiedRoutineSourceTasks.length === 0 || verifiedRoutineActionTasks.length === 0) && (
                  <div className="mt-2 rounded border border-amber-500/40 p-2" role="status">Blocked: a verified Done research task and a verified Done GitHub follow-through task are required before a procedure can be previewed.</div>
                )}
                <div className="mt-2 grid gap-2 sm:grid-cols-2">
                  <label>Verified research task
                    <select
                      aria-label="Verified research task"
                      className="cockpit-input mt-1 w-full"
                      value={selectedRoutineSourceTaskId}
                      onChange={(event) => { setRoutineSourceTaskId(event.currentTarget.value); resetRoutineRequest(); }}
                      disabled={routineBusy || verifiedRoutineSourceTasks.length === 0}
                    >
                      <option value="">Choose a Done research task</option>
                      {verifiedRoutineSourceTasks.map((task) => <option key={task.task_id} value={task.task_id}>{task.title} · {task.task_id}</option>)}
                    </select>
                  </label>
                  <label>Verified follow-through task
                    <select
                      aria-label="Verified follow-through task"
                      className="cockpit-input mt-1 w-full"
                      value={selectedRoutineActionTaskId}
                      onChange={(event) => { setRoutineActionTaskId(event.currentTarget.value); resetRoutineRequest(); }}
                      disabled={routineBusy || verifiedRoutineActionTasks.length === 0}
                    >
                      <option value="">Choose a Done follow-through task</option>
                      {verifiedRoutineActionTasks.map((task) => <option key={task.task_id} value={task.task_id}>{task.title} · {task.task_id}</option>)}
                    </select>
                  </label>
                  <label className="sm:col-span-2">Procedure name
                    <input
                      aria-label="Procedure name"
                      className="cockpit-input mt-1 w-full"
                      maxLength={80}
                      value={routineName}
                      onChange={(event) => { setRoutineName(event.currentTarget.value); resetRoutineRequest(); }}
                      disabled={routineBusy}
                    />
                  </label>
                </div>
                {!routineJourneyReady && currentOwnerSession && verifiedRoutineSourceTasks.length > 0 && verifiedRoutineActionTasks.length > 0 && (
                  <div className="mt-2 text-amber-200" role="status">The selected pair is not a verified linked journey in the current snapshot. Choose the linked Done tasks; the backend remains authoritative.</div>
                )}
                <div className="mt-2 flex flex-wrap gap-2">
                  <button
                    type="button"
                    className="cockpit-feedback-button"
                    disabled={routineBusy || !routineJourneyReady || !routineName.trim()}
                    onClick={() => void previewRoutine()}
                  >{routineBusy && !routinePreview ? "Preparing preview…" : "Preview procedure"}</button>
                  {routinePreview && (
                    <button type="button" className="cockpit-feedback-button" disabled={routineBusy} onClick={() => void acceptRoutine()}>
                      {routineBusy ? "Accepting…" : "Accept and prepare procedure"}
                    </button>
                  )}
                </div>
                {routineError && <div className="mt-2 rounded border border-amber-500/40 p-2" role="alert">{routineError}</div>}
                {routinePreview && (
                  <div className="mt-3 rounded border border-white/10 bg-black/20 p-3" role="region" aria-label="Governed procedure preview">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="font-semibold">Reviewable procedure proposal</div>
                      <span className="font-mono text-[10px]">preview {routinePreview.preview_digest}</span>
                    </div>
                    <div className="mt-2">{routinePreview.safe_summary}</div>
                    <div className="mt-2 grid gap-1 text-[10px]">
                      <div>Version {routinePreview.version_plan.version} · workflow {routinePreview.version_plan.workflow}</div>
                      <div>Steps: {routinePreview.version_plan.steps.join(" → ")}</div>
                      <div>Runtime limit: {routinePreview.limits.runtime_seconds}s · attempts: {routinePreview.limits.attempts} · remote inference: {routinePreview.limits.remote_inference ? "enabled" : "disabled"}</div>
                      <div>External mutation: {routinePreview.permissions.external_mutation} · package review: {routinePreview.permissions.package_review}</div>
                      <div>Verifier: {routinePreview.verifier.source} · unknown effect: {routinePreview.verifier.unknown_effect}</div>
                      <div>Expires: {safeDateTime(routinePreview.expires_at)}</div>
                    </div>
                    <div className="mt-2 rounded bg-black/20 p-2">
                      <div className="font-semibold text-[10px]">Typed invocation parameters</div>
                      <div className="mt-1 grid gap-1 break-all font-mono text-[10px]">
                        {Object.entries(routinePreview.typed_parameters).map(([key, value]) => <div key={key}>{key}: {value === null ? "null" : String(value)}</div>)}
                        {Object.keys(routinePreview.typed_parameters).length === 0 && <div>None returned</div>}
                      </div>
                    </div>
                    <div className="mt-2 rounded bg-black/20 p-2">
                      <div className="font-semibold text-[10px]">Safe source receipts</div>
                      <div className="mt-1 grid gap-1 break-all font-mono text-[10px]">
                        {(["source_task_id", "source_attempt_id", "source_watch_job_id", "source_packet_id", "action_task_id", "action_attempt_id", "source_m3_job_id"] as const).map((key) => (
                          <div key={key}>{key}: {routinePreview.source_refs[key] ?? "unavailable"}</div>
                        ))}
                      </div>
                    </div>
                  </div>
                )}
                {(routineBinding || routine) && (
                  <div className="mt-3 rounded border border-white/10 bg-black/20 p-3" role="region" aria-label="Governed procedure lifecycle">
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="font-semibold">Procedure lifecycle</div>
                      <span>{routine?.state ?? routineBinding?.state} · revision {routine?.revision ?? routineBinding?.revision}</span>
                    </div>
                    <div className="mt-1 break-all text-[10px]">Routine {routine?.id ?? routineBinding?.routine_id} · version {routineVersion}{routineBinding?.install_job_id ? ` · install job ${routineBinding.install_job_id}` : ""}</div>
                    {routine && <div className="mt-1">Package review: {routine.package?.status ?? "unavailable"}{routine.package?.reason ? ` · ${routine.package.reason}` : ""}</div>}
                    {(routine?.state === "revoked" || routineBinding?.state === "revoked") && <div className="mt-2 rounded border border-red-500/40 p-2" role="status">Revoked: future invocations are blocked permanently. A revoked procedure cannot be reactivated or revived by rollback.</div>}
                    {routine && routine.state !== "revoked" && routine.package?.status !== "active" && (
                      <div className="mt-2 rounded border border-amber-500/40 p-2" role="status">Blocked: this capability package is not active for the current owner session. The procedure cannot be enabled or invoked until its exact package review and activation approval are current.</div>
                    )}
                    {routine && routine.state !== "prepared" && routine.state !== "revoked" && (
                      <section className="mt-3 rounded border border-white/10 bg-black/20 p-3" aria-label="Procedure package governance">
                        <div className="font-semibold">Review the installed procedure package</div>
                        <p className="mt-1 text-[10px] opacity-75">This immutable v2 package records the reviewed procedure definition for its source goal. It grants no tools, filesystem access, network access, or secrets. Package activation does not authorize later work; each invocation receives fresh goal revision, grants, approvals, budget, task, and readback.</p>
                        <div className="mt-2 flex flex-wrap gap-2">
                          {routine?.current_version && (
                            <button type="button" className="cockpit-feedback-button" disabled={routineBusy} onClick={() => void exportRoutineProcedure()}>
                              {routineBusy ? "Exporting procedure…" : "Export reviewed procedure"}
                            </button>
                          )}
                          <button type="button" className="cockpit-feedback-button" disabled={routineBusy} onClick={() => void previewRoutinePackage()}>
                            {routineBusy && !routinePackagePreview ? "Loading package preview…" : "Preview installed package"}
                          </button>
                          {routinePackagePreview
                            && routinePackagePreview.digest === routinePackagePreview.installed_package_digest
                            && !routinePackagePreview.review_id
                            && routinePackagePreview.status !== "active"
                            && (
                              <button type="button" className="cockpit-feedback-button" disabled={routineBusy} onClick={() => void reviewRoutinePackage()}>
                                {routineBusy ? "Recording review…" : "Record local package review"}
                              </button>
                            )}
                          {routinePackagePreview?.review_id
                            && routinePackagePreview.status !== "active"
                            && (!routinePackageApproval || ["denied", "expired"].includes(routinePackageApproval.status))
                            && (
                              <button type="button" className="cockpit-feedback-button" disabled={routineBusy} onClick={() => void prepareRoutinePackageApproval()}>
                                {routineBusy ? "Preparing approval…" : "Prepare activation approval"}
                              </button>
                            )}
                          {routinePackageApproval?.status === "pending" && (
                            <>
                              <button type="button" className="cockpit-feedback-button" disabled={routineBusy} onClick={() => void decideRoutinePackageApproval("approved")}>Approve exact package activation</button>
                              <button type="button" className="cockpit-feedback-button" disabled={routineBusy} onClick={() => void decideRoutinePackageApproval("denied")}>Deny package activation</button>
                            </>
                          )}
                          {routinePackageApproval?.status === "approved" && (
                            <button type="button" className="cockpit-feedback-button" disabled={routineBusy} onClick={() => void activateRoutinePackage()}>
                              {routineBusy ? "Activating package…" : "Activate reviewed capability package"}
                            </button>
                          )}
                        </div>
                        {routinePackagePreview && (
                          <div className="mt-3 rounded border border-white/10 p-3" role="region" aria-label="Procedure package preview">
                            <div className="font-semibold">{routinePackagePreview.manifest.display_name}</div>
                            <div className="mt-1">{routinePackagePreview.manifest.summary}</div>
                            <div className="mt-2 grid gap-1 break-all font-mono text-[10px]">
                              <div>Package ID: {routinePackagePreview.pack_id} · version {routinePackagePreview.manifest.version}</div>
                              <div>Digest: {routinePackagePreview.digest}</div>
                              <div>Capability: {routinePackagePreview.runbook.procedure.capability_id}</div>
                              <div>Fixed steps: {routinePackagePreview.runbook.procedure.steps.map((step) => step.id).join(" → ")}</div>
                              <div>Workflow SHA-256: {routinePackagePreview.runbook.bindings.workflow_sha256}</div>
                              <div>Authority: {routinePackagePreview.manifest.authority.tools.length} tools · {routinePackagePreview.manifest.authority.filesystem.length} filesystem paths · network {routinePackagePreview.manifest.authority.network ? "enabled" : "disabled"} · {routinePackagePreview.manifest.authority.secrets.length} secrets</div>
                              <div>Limits: {routinePackagePreview.manifest.resources.max_runtime_seconds}s · {routinePackagePreview.manifest.resources.max_artifact_bytes} artifact bytes · ${routinePackagePreview.manifest.resources.max_inference_cost_microusd / 1_000_000} inference budget</div>
                              <div>Package state: {routinePackagePreview.status}{routinePackagePreview.review_id ? ` · review ${routinePackagePreview.review_id}` : ""}</div>
                            </div>
                            {routinePackageApproval && (
                              <div className="mt-2 rounded bg-black/20 p-2" role="status">
                                Activation approval {routinePackageApproval.status}: <span className="font-mono">{routinePackageApproval.approval_id}</span> · digest {routinePackageApproval.digest}
                              </div>
                            )}
                            {routinePackagePreview.digest !== routinePackagePreview.installed_package_digest && (
                              <div className="mt-2 rounded border border-amber-500/40 p-2" role="alert">The package preview differs from the installed digest. Review and activation controls remain blocked.</div>
                            )}
                            {routinePackageApproval?.status === "denied" && (
                              <div className="mt-2 rounded border border-amber-500/40 p-2" role="status">Activation was denied. The procedure remains unavailable; a new exact approval is required after review.</div>
                            )}
                          </div>
                        )}
                      </section>
                    )}
                    {!routineApprovalId.trim() && routine?.state === "prepared" && (
                      <div className="mt-2 grid gap-2 rounded border border-amber-500/40 p-2">
                        <div role="status">Installation is waiting for the exact approval receipt created for this procedure.</div>
                        <label>Install approval ID from Pending approvals
                          <input aria-label="Install approval ID from Pending approvals" className="cockpit-input mt-1 w-full" maxLength={256} value={routineApprovalId} onChange={(event) => setRoutineApprovalId(event.currentTarget.value)} />
                        </label>
                        {onOpenApprovals && <button type="button" className="cockpit-feedback-button justify-self-start" onClick={onOpenApprovals}>Open Pending approvals</button>}
                      </div>
                    )}
                    <div className="mt-2 flex flex-wrap gap-2">
                      {routine?.state === "prepared" && (
                        <button type="button" className="cockpit-feedback-button" disabled={routineBusy || !routineApprovalId.trim()} onClick={() => void runRoutineLifecycleAction("install")}>Install reviewed procedure</button>
                      )}
                      {routine?.state === "installed" && (
                        <button type="button" className="cockpit-feedback-button" disabled={routineBusy || !routinePackageReviewed} onClick={() => void runRoutineLifecycleAction("activate")} title={!routinePackageReviewed ? "The current package must first be reviewed, separately approved, and activated." : undefined}>Enable procedure invocations</button>
                      )}
                      {routine && ["installed", "active", "paused"].includes(routine.state) && (
                        <button type="button" className="cockpit-feedback-button" disabled={routineBusy || routine.state === "revoked"} onClick={() => void runRoutineLifecycleAction("pause")}>Pause future invocations</button>
                      )}
                      {routine && routine.state !== "revoked" && (
                        <button type="button" className="cockpit-feedback-button" disabled={routineBusy} onClick={() => void runRoutineLifecycleAction("revoke")}>Revoke permanently</button>
                      )}
                    </div>
                    {routineRollbackVersions.length > 0 && routine && routine.state !== "revoked" && (
                      <div className="mt-2 flex flex-wrap items-end gap-2">
                        <label>Rollback target
                          <select aria-label="Rollback target" className="cockpit-input mt-1 block" defaultValue="" disabled={routineBusy} onChange={(event) => { const target = Number(event.currentTarget.value); if (target) void runRoutineLifecycleAction("rollback", target); }}>
                            <option value="">Choose installed version</option>
                            {routineRollbackVersions.map((version) => <option key={version.version} value={version.version}>Version {version.version}</option>)}
                          </select>
                        </label>
                        <span className="text-[10px] opacity-75">Rollback changes future invocations after the server verifies the current package receipt.</span>
                      </div>
                    )}
                    {routineRollbackBlocked && routine && routine.state !== "revoked" && (
                      <div className="mt-2 rounded border border-amber-500/40 p-2" role="status">
                        Rollback is blocked: no installed earlier version matches the current active package readback. Refresh the package review before choosing a rollback target.
                      </div>
                    )}
                    {routine && (
                      <div className="mt-3 rounded border border-white/10 bg-black/20 p-3" role="region" aria-label="Governed procedure invocation">
                        <div className="font-semibold">Invoke for a fresh approved goal and input</div>
                        <p className="mt-1 text-[10px] opacity-75">Invocation submits the current routine, goal, and source-watch revisions. The server creates one canonical Todo task; the normal dispatcher owns execution and approvals.</p>
                        {pendingRoutineInvocation && <div className="mt-2 rounded border border-amber-500/40 p-2" role="status">
                          An invocation has an unconfirmed receipt. The exact routine, goal, watch, and revision request is preserved under invocation ID <span className="font-mono">{pendingRoutineInvocation.request.invocation_uuid}</span>. Retry reconciliation submits the same request and cannot create a new invocation key.
                        </div>}
                        {routinePersistenceError && routinePersistenceError !== routineError && <div className="mt-2 rounded border border-amber-500/40 p-2" role="alert">{routinePersistenceError}</div>}
                        {activeRoutineGoals.length === 0 && <div className="mt-2 rounded border border-amber-500/40 p-2" role="status">Blocked: no active owner goal with a current revision is available.</div>}
                        {activeRoutineGoals.length > 0 && (
                          <div className="mt-2 grid gap-2 sm:grid-cols-2">
                            <label>Invocation goal
                              <select
                                aria-label="Invocation goal"
                                className="cockpit-input mt-1 w-full"
                                value={selectedRoutineInvocationGoalId}
                                disabled={routineInvokeBusy || routine.state === "revoked" || Boolean(pendingRoutineInvocation)}
                                onChange={(event) => { setRoutineInvocationGoalId(event.currentTarget.value); setRoutineInvocationWatchId(""); setRoutineInvokeReceipt(null); setRoutineError(null); }}
                              >
                                <option value="">Choose an active goal</option>
                                {activeRoutineGoals.map((goal) => <option key={goal.id} value={goal.id} disabled={goal.ownership_access === "recovered_read_only"}>{goal.title} · revision {goal.revision ?? "unavailable"}</option>)}
                              </select>
                            </label>
                            <label>Approved source watch
                              <select
                                aria-label="Approved source watch"
                                className="cockpit-input mt-1 w-full"
                                value={selectedRoutineWatchId}
                                disabled={routineInvokeBusy || routine.state === "revoked" || routineWatchCandidates.length === 0 || Boolean(pendingRoutineInvocation)}
                                onChange={(event) => { setRoutineInvocationWatchId(event.currentTarget.value); setRoutineInvokeReceipt(null); setRoutineError(null); }}
                              >
                                <option value="">Choose an owner-visible watch</option>
                                {routineWatchCandidates.map((watch) => <option key={watch.id} value={watch.id}>{watch.id} · {watch.state} · goal rev {watch.goal_revision} · plan rev {watch.plan_revision}</option>)}
                              </select>
                            </label>
                          </div>
                        )}
                        {sourceWatchError && <div className="mt-2 rounded border border-amber-500/40 p-2" role="status">Source-watch selection is unavailable: {sourceWatchError}</div>}
                        {selectedRoutineWatch && <div className="mt-2 text-[10px]">Watch {selectedRoutineWatch.id} · goal revision {selectedRoutineWatch.goal_revision} · plan revision {selectedRoutineWatch.plan_revision} · status {selectedRoutineWatch.state}{selectedRoutineWatch.last_status ? ` · ${selectedRoutineWatch.last_status}` : ""}</div>}
                        {routine.state !== "active" && routine.state !== "revoked" && <div className="mt-2 rounded border border-amber-500/40 p-2" role="status">Blocked: this procedure is {routine.state}. Activate it before requesting a fresh invocation.</div>}
                        {routine.state === "active" && !routinePackageReviewed && <div className="mt-2 rounded border border-amber-500/40 p-2" role="status">Blocked: the installed package review is stale or unavailable. Refresh the review before invoking.</div>}
                        {routine.state === "active" && routineWatchCandidates.length === 0 && <div className="mt-2 rounded border border-amber-500/40 p-2" role="status">Blocked: no owner-visible source watch matches the selected active goal.</div>}
                        {selectedRoutineWatch && selectedRoutineWatch.state !== "active" && <div className="mt-2 rounded border border-amber-500/40 p-2" role="status">Blocked: the selected source watch is {selectedRoutineWatch.state}. Select an active approved watch or refresh its state.</div>}
                        <button type="button" className="cockpit-feedback-button mt-2" disabled={routineInvokeBusy || !routineInvocationReady} onClick={() => void invokeRoutine()}>
                          {routineInvokeBusy ? "Reconciling Todo task…" : pendingRoutineInvocation ? "Retry invocation and reconcile" : "Invoke governed procedure"}
                        </button>
                        {routineInvokeReceipt && <div className="mt-2 rounded border border-emerald-500/40 p-2" role="status">Invocation queued: Todo task <span className="font-mono">{routineInvokeReceipt.task_id}</span>{typeof routineInvokeReceipt.task_revision === "number" ? ` · revision ${routineInvokeReceipt.task_revision}` : ""}. The board was refreshed; dispatcher controls execution.</div>}
                      </div>
                    )}
                  </div>
                )}
              </section>

              <section className="rounded border border-white/10 p-3">
                <div className="font-semibold">Dependencies</div>
                <div className="mt-1">Parent progress {selectedTask.completed_dependency_count}/{selectedTask.dependency_count}</div>
                <div className="mt-2 grid gap-1">
                  <div className="text-[10px] uppercase opacity-70">Parents</div>
                  {selectedDetail?.parents.map((parentId) => {
                    const parent = taskById.get(parentId);
                    return <div key={parentId} className="flex items-center justify-between gap-2"><button type="button" className="break-all text-left underline" onClick={() => openTask(parentId)}>{parent?.title ?? parentId}</button><span>{parent ? STATUS_LABELS[parent.status] : "Not in current snapshot"}</span><button type="button" className="underline" disabled={busyAction || selectedTask.status === "running"} onClick={() => void deleteLink(parentId, selectedTask.task_id, selectedTask.task_revision)}>Remove</button></div>;
                  })}
                  <form className="flex gap-2" onSubmit={(event) => { event.preventDefault(); if (parentTaskIdDraft.trim()) void createLink(parentTaskIdDraft, selectedTask.task_id, selectedTask.task_revision); }}>
                    <label className="sr-only" htmlFor="work-board-parent-id">Parent task ID</label><input id="work-board-parent-id" aria-label="Parent task ID" className="cockpit-input min-w-0 flex-1" maxLength={128} value={parentTaskIdDraft} onChange={(event) => setParentTaskIdDraft(event.currentTarget.value)} placeholder="Parent task ID" />
                    <button type="submit" className="cockpit-feedback-button" disabled={busyAction || !parentTaskIdDraft.trim() || selectedTask.status === "running"}>Add parent</button>
                  </form>
                  <div className="mt-2 text-[10px] uppercase opacity-70">Children</div>
                  {selectedDetail?.children.map((childId) => {
                    const child = taskById.get(childId);
                    return <div key={childId} className="flex items-center justify-between gap-2"><button type="button" className="break-all text-left underline" onClick={() => openTask(childId)}>{child?.title ?? childId}</button><span>{child ? STATUS_LABELS[child.status] : "Not in current snapshot"}</span><button type="button" className="underline" disabled={busyAction} onClick={() => void deleteLink(selectedTask.task_id, childId, child?.task_revision ?? selectedTask.task_revision)}>Remove</button></div>;
                  })}
                  <form className="flex gap-2" onSubmit={(event) => { event.preventDefault(); const childId = childTaskIdDraft.trim(); const child = taskById.get(childId); if (child) void createLink(selectedTask.task_id, childId, child.task_revision); else setActionError("Refresh the board and select a child task from the current snapshot before linking it."); }}>
                    <label className="sr-only" htmlFor="work-board-child-id">Child task ID</label><input id="work-board-child-id" aria-label="Child task ID" className="cockpit-input min-w-0 flex-1" maxLength={128} value={childTaskIdDraft} onChange={(event) => setChildTaskIdDraft(event.currentTarget.value)} placeholder="Child task ID" />
                    <button type="submit" className="cockpit-feedback-button" disabled={busyAction || !childTaskIdDraft.trim()}>Add child</button>
                  </form>
                </div>
              </section>

              <section className="rounded border border-white/10 p-3">
                <div className="font-semibold">Attempts, readback, and artifacts</div>
                {selectedTask.latest_attempt && <div className="mt-1">Latest attempt: {attemptLabel(selectedTask)} · readback {READBACK_LABELS[selectedTask.latest_attempt.readback_status]} · verification {VERIFICATION_LABELS[selectedTask.latest_attempt.verification_status]}</div>}
                <div className="mt-2 grid gap-2">
                  {selectedDetail?.attempts.map((attempt) => (
                    <div key={attempt.attempt_id} className="rounded bg-black/20 p-2">
                      <div>Attempt {attempt.attempt_id} · {attempt.ended_at ? attempt.outcome ?? "ended" : "active"} · fence {attempt.fencing_token}</div>
                      <div>Started {safeDateTime(attempt.started_at)} · ended {safeDateTime(attempt.ended_at)} · executor {attempt.executor_id ?? "Unassigned"}</div>
                      <div>Readback {READBACK_LABELS[attempt.readback_status]} · verification {VERIFICATION_LABELS[attempt.verification_status]}</div>
                      {attempt.workflow_run_id && <div className="mt-1 break-all">Workflow run {attempt.workflow_run_id}{onInspectWorkflowRun && <button type="button" className="ml-2 underline" onClick={() => onInspectWorkflowRun(attempt.workflow_run_id!, selectedTask.owner_session_id)}>Open workflow evidence</button>}</div>}
                      {selectedTask.capability_id === "browser.public-task.v1" && (() => {
                        const execution = browserExecutionForAttempt(selectedTask, attempt);
                        if (!execution) {
                          return <div className="mt-1 rounded border border-amber-500/30 p-2 text-[10px]" aria-label="Browser durable execution receipt">Durable browser execution receipt unavailable; progress, cleanup, and readback remain unknown until a current owner-bound receipt is returned.</div>;
                        }
                        const executionReference = browserExecutionReference(execution);
                        return (
                          <div className="mt-1 rounded border border-cyan-500/30 p-2 text-[10px]" aria-label="Browser durable execution receipt">
                            <div>Browser durable execution · job <span className="font-mono break-all">{execution.job_id}</span> · status {execution.durable_status || "unknown"}</div>
                            <div>Browser progress · {browserExecutionAction(execution)} · {browserExecutionCount(execution.request_count, 0, 32)} requests</div>
                            <div>Cleanup {execution.cleanup_status || "unknown"} · Memory {execution.memory_status || "unknown"}</div>
                            {execution.readback_id && <div className="break-all">Readback {execution.readback_id}</div>}
                            {execution.file_path && <div className="break-all">Artifact path {execution.file_path}</div>}
                            {executionReference && onInspectArtifact && <button type="button" className="mt-1 underline" aria-label={`Inspect execution evidence ${execution.file_path}`} onClick={() => onInspectArtifact({ reference: executionReference, ownerSessionId: selectedTask.owner_session_id, workflowRunId: execution.job_id, parentWorkflowRunId: attempt.workflow_run_id })}>Inspect browser artifact</button>}
                          </div>
                        );
                      })()}
                      {[...attempt.receipt_refs].map((receipt, index) => {
                        const inspectReference = browserEvidenceReference(receipt);
                        const inspectLabel = inspectReference.file_path
                          ?? inspectReference.target_path
                          ?? inspectReference.artifact_id
                          ?? inspectReference.artifact_ref
                          ?? inspectReference.effect_id_digest
                          ?? inspectReference.readback_id
                          ?? inspectReference.verification_id
                          ?? "available evidence";
                        const inspectable = Boolean(
                          inspectReference.file_path
                          || inspectReference.artifact_id
                          || inspectReference.target_path
                          || inspectReference.effect_id_digest
                          || inspectReference.readback_id
                          || inspectReference.verification_id,
                        );
                        return (
                          <div key={`${attempt.attempt_id}:receipt:${index}`} className="mt-1 border-t border-white/10 pt-1">
                            <div>{receiptTitle(receipt)} · {safeReferenceLabel(inspectReference)}</div>
                            <div>{receipt.status ?? receipt.outcome ?? "Receipt"}{receipt.verified === true ? " · verified" : ""}{receipt.readback_status ? ` · readback ${READBACK_LABELS[receipt.readback_status]}` : ""}</div>
                            {(receipt.checkpoint_id || typeof receipt.action_index === "number" || typeof receipt.action_count === "number" || typeof receipt.request_count === "number") && (
                              <div className="mt-1 text-[10px]" aria-label="Browser execution progress">
                                Browser progress
                                {receipt.checkpoint_id ? ` · checkpoint ${receipt.checkpoint_id}` : ""}
                                {typeof receipt.action_index === "number" ? ` · action ${receipt.action_index + 1}${typeof receipt.action_count === "number" ? `/${receipt.action_count}` : ""}` : typeof receipt.action_count === "number" ? ` · ${receipt.action_count} actions` : ""}
                                {typeof receipt.request_count === "number" ? ` · ${receipt.request_count} requests` : ""}
                              </div>
                            )}
                            {receipt.durable_status && <div className="text-[10px]">Durable status {receipt.durable_status}</div>}
                            {receipt.artifact_ref && <div className="break-all text-[10px]">Browser artifact receipt {receipt.artifact_ref}</div>}
                            {inspectReference.content_sha256 && <div className="break-all font-mono text-[10px]">SHA-256 {inspectReference.content_sha256}</div>}
                            {receipt.file_path && <div className="break-all text-[10px]">Artifact path {receipt.file_path}</div>}
                            {receipt.cleanup_status && <div className="text-[10px]">Cleanup {receipt.cleanup_status}</div>}
                            {receipt.memory_status && <div className="text-[10px]">Memory {receipt.memory_status}</div>}
                            {inspectable && onInspectArtifact && <button type="button" className="mt-1 underline" aria-label={`Inspect execution evidence ${inspectLabel}`} onClick={() => onInspectArtifact({ reference: inspectReference, ownerSessionId: selectedTask.owner_session_id, workflowRunId: referenceWorkflowRunId(receipt, attempt.workflow_run_id), parentWorkflowRunId: attempt.workflow_run_id })}>{receipt.target_path || receipt.effect_id_digest || receipt.readback_id || receipt.verification_id ? "Inspect readback evidence" : "Inspect artifact"}</button>}
                            {receipt.workflow_run_id && onInspectWorkflowRun && <button type="button" className="underline" onClick={() => onInspectWorkflowRun(receipt.workflow_run_id!, selectedTask.owner_session_id)}>Inspect existing workflow record</button>}
                          </div>
                        );
                      })}
                    </div>
                  ))}
                  {[...selectedTask.result_refs, ...selectedTask.artifact_refs].map((reference, index) => {
                    const inspectReference = browserEvidenceReference(reference);
                    const inspectLabel = inspectReference.file_path
                      ?? inspectReference.target_path
                      ?? inspectReference.artifact_id
                      ?? inspectReference.artifact_ref
                      ?? inspectReference.effect_id_digest
                      ?? inspectReference.readback_id
                      ?? inspectReference.verification_id
                      ?? "available evidence";
                    const inspectable = Boolean(
                      inspectReference.file_path
                      || inspectReference.artifact_id
                      || inspectReference.target_path
                      || inspectReference.effect_id_digest
                      || inspectReference.readback_id
                      || inspectReference.verification_id,
                    );
                    return (
                      <div key={`task-ref:${index}`} className="rounded bg-black/20 p-2">
                        <div>{receiptTitle(reference)} · {safeReferenceLabel(inspectReference)}</div>
                        <div>{reference.status ?? reference.outcome ?? "Reference"}{reference.verified === true ? " · verified" : ""}</div>
                        {(reference.checkpoint_id || typeof reference.action_index === "number" || typeof reference.action_count === "number" || typeof reference.request_count === "number") && (
                          <div className="mt-1 text-[10px]" aria-label="Browser execution progress">
                            Browser progress
                            {reference.checkpoint_id ? ` · checkpoint ${reference.checkpoint_id}` : ""}
                            {typeof reference.action_index === "number" ? ` · action ${reference.action_index + 1}${typeof reference.action_count === "number" ? `/${reference.action_count}` : ""}` : typeof reference.action_count === "number" ? ` · ${reference.action_count} actions` : ""}
                            {typeof reference.request_count === "number" ? ` · ${reference.request_count} requests` : ""}
                          </div>
                        )}
                        {reference.durable_status && <div className="text-[10px]">Durable status {reference.durable_status}</div>}
                        {reference.artifact_ref && <div className="break-all text-[10px]">Browser artifact receipt {reference.artifact_ref}</div>}
                        {inspectReference.content_sha256 && <div className="break-all font-mono text-[10px]">SHA-256 {inspectReference.content_sha256}</div>}
                        {reference.file_path && <div className="break-all text-[10px]">Artifact path {reference.file_path}</div>}
                        {reference.cleanup_status && <div className="text-[10px]">Cleanup {reference.cleanup_status}</div>}
                        {reference.memory_status && <div className="text-[10px]">Memory {reference.memory_status}</div>}
                        {inspectable && onInspectArtifact && <button type="button" className="mt-1 underline" aria-label={`Inspect execution evidence ${inspectLabel}`} onClick={() => onInspectArtifact({ reference: inspectReference, ownerSessionId: selectedTask.owner_session_id, workflowRunId: referenceWorkflowRunId(reference, selectedTask.latest_attempt?.workflow_run_id ?? null), parentWorkflowRunId: selectedTask.latest_attempt?.workflow_run_id ?? null })}>{reference.target_path || reference.effect_id_digest || reference.readback_id || reference.verification_id ? "Inspect readback evidence" : "Inspect artifact"}</button>}
                        {reference.workflow_run_id && onInspectWorkflowRun && <button type="button" className="underline" onClick={() => onInspectWorkflowRun(reference.workflow_run_id!, selectedTask.owner_session_id)}>Open workflow evidence</button>}
                      </div>
                    );
                  })}
                  {(!selectedDetail?.attempts.length && !selectedTask.result_refs.length && !selectedTask.artifact_refs.length) && <div className="cockpit-empty">No attempts or output references yet.</div>}
                </div>
              </section>

              <WorkBoardMemoryReview
                task={selectedTask}
                ownerPrincipalId={ownerPrincipalId}
                ownerSessionId={ownerSessionId}
              />
              {selectedTask.capability_id === "work.research-dossier.v1" && <ResearchDossierPanel
                key={`research-inspector:${ownerPrincipalId}:${ownerSessionId}:${selectedTask.task_id}`}
                task={selectedTask} ownerPrincipalId={ownerPrincipalId} ownerSessionId={ownerSessionId}
                onChanged={async () => { await refreshSnapshot(); }} />}
              {selectedTask.capability_id === "work.json-format.v1" && <JsonFormatterPanel
                key={`formatter-inspector:${ownerPrincipalId}:${ownerSessionId}:${selectedTask.task_id}`}
                task={selectedTask} ownerPrincipalId={ownerPrincipalId} ownerSessionId={ownerSessionId}
                onChanged={async () => { await refreshSnapshot(); }} />}

              {ownerPrincipalId && ownerSessionId && <ArtifactPipelineReview key={`${ownerPrincipalId}:${ownerSessionId}:${selectedTask.task_id}`} task={selectedTask}
                ownerPrincipalId={ownerPrincipalId} ownerSessionId={ownerSessionId}
                metadataConfirmed={Boolean(selectedDetail && !detailLoading && !stale && !detailError)}
                onRefresh={refreshSelectedTask} onOpenTask={openTask} />}
              <TaskEvidencePanel task={selectedTask} ownerSessionId={ownerSessionId} />

              {selectedDetail?.parent_handoffs && selectedDetail.parent_handoffs.length > 0 && (
                <section className="rounded border border-white/10 p-3" aria-label="Safe parent handoffs">
                  <div className="font-semibold">Safe parent handoffs</div>
                  <div className="mt-1 text-[10px] opacity-75">Verified parent results are evidence and input only. They do not grant authority, change this task capability, or bypass approval.</div>
                  <div className="mt-2 grid gap-2">
                    {selectedDetail.parent_handoffs.map((handoff) => (
                      <article key={handoff.handoff_id || `${handoff.parent_task_id}:${handoff.source_task_revision}`} className="rounded bg-black/20 p-2">
                        {(() => {
                          const handoffWorkflowRunId = typeof handoff.verification_receipt.workflow_run_id === "string"
                            ? handoff.verification_receipt.workflow_run_id
                            : null;
                          const evidenceRefs = [...handoff.artifact_refs, ...handoff.result_refs];
                          return (
                            <>
                        <div>Parent {handoff.parent_task_id} · {handoff.status} · source attempt {handoff.source_attempt_id ?? "unavailable"} · source revision {handoff.source_task_revision}</div>
                        <div className="mt-1 whitespace-pre-wrap break-words">{handoff.summary || "No safe summary supplied."}</div>
                        <div className="mt-1">Verification: {String(handoff.verification_receipt.status ?? "unknown")}{handoff.risks.length ? ` · risks: ${handoff.risks.join(", ")}` : ""}</div>
                        {handoffWorkflowRunId && onInspectWorkflowRun && <button type="button" className="mt-1 underline" onClick={() => onInspectWorkflowRun(handoffWorkflowRunId, selectedTask.owner_session_id)}>Open parent workflow evidence</button>}
                        {evidenceRefs.length > 0 && (
                          <div className="mt-1 grid gap-1" aria-label={`Evidence references from parent ${handoff.parent_task_id}`}>
                            {evidenceRefs.map((reference, index) => {
                              const referenceId = reference.file_path ?? reference.target_path ?? reference.artifact_id ?? reference.effect_id_digest ?? reference.readback_id ?? reference.verification_id ?? reference.job_id ?? `reference-${index + 1}`;
                              const inspectable = Boolean(reference.file_path || reference.artifact_id || reference.target_path || reference.effect_id_digest || reference.readback_id || reference.verification_id);
                              return (
                                <div key={`${handoff.handoff_id}:evidence:${index}`} className="border-t border-white/10 pt-1">
                                  <div>{receiptTitle(reference)} · {safeReferenceLabel(reference)}{reference.verified === true ? " · verified" : ""}{reference.readback_status ? ` · readback ${READBACK_LABELS[reference.readback_status]}` : ""}</div>
                                  {reference.content_sha256 && <div className="break-all font-mono text-[10px]">SHA-256 {reference.content_sha256}</div>}
                                  {inspectable && onInspectArtifact && <button type="button" className="mt-1 underline" aria-label={`Inspect parent handoff evidence ${referenceId}`} onClick={() => onInspectArtifact({ reference, ownerSessionId: selectedTask.owner_session_id, workflowRunId: referenceWorkflowRunId(reference, handoffWorkflowRunId), parentWorkflowRunId: handoffWorkflowRunId })}>Inspect parent handoff evidence</button>}
                                </div>
                              );
                            })}
                          </div>
                        )}
                            </>
                          );
                        })()}
                      </article>
                    ))}
                  </div>
                </section>
              )}

              <section className="rounded border border-white/10 p-3">
                <div className="font-semibold">Comments</div>
                <div className="mt-2 grid gap-2">
                  {selectedDetail?.comments.map((comment) => <article key={comment.comment_id} className="rounded bg-black/20 p-2"><div className="text-[10px] opacity-70">{comment.author_principal_id} · {safeDateTime(comment.created_at)}</div><div className="mt-1 whitespace-pre-wrap break-words">{comment.body}</div></article>)}
                  {(!selectedDetail?.comments.length) && <div className="cockpit-empty">No comments yet.</div>}
                </div>
                <form className="mt-2 grid gap-2" onSubmit={(event) => void addComment(event)}>
                  <label>Comment<textarea className="cockpit-input mt-1 w-full" maxLength={2000} rows={2} value={commentDraft} onChange={(event) => setCommentDraft(event.currentTarget.value)} /></label>
                  <button type="submit" className="cockpit-feedback-button justify-self-start" disabled={busyAction || !commentDraft.trim() || commentDraft.trim().length > 2000}>Add comment</button>
                </form>
              </section>

              <section className="rounded border border-white/10 p-3">
                <div className="font-semibold">Safe task event timeline</div>
                <div className="mt-2 grid gap-2">
                  {selectedDetail?.events.map((event) => <div key={event.event_id} className="border-l border-white/20 pl-2"><div>{event.kind.replace(/[_.]/g, " ")} · {safeDateTime(event.created_at)}</div><div className="text-[10px] opacity-70">{eventSummary(event)}</div></div>)}
                  {(!selectedDetail?.events.length) && <div className="cockpit-empty">No task events yet.</div>}
                </div>
              </section>
            </div>
            </fieldset>
        </aside>,
        document.querySelector(".cockpit-shell") ?? document.body,
      )}

      {createPortal(<div className="relative z-[200]">
      {createOpen && (
        <div className="fixed inset-0 z-[90] flex items-center justify-center bg-black/65 p-4" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) closeCreateDialog(); }}>
          <form ref={createDialogRef} role="dialog" aria-modal="true" aria-labelledby="work-board-create-title" tabIndex={-1} className="max-h-[90vh] w-full max-w-2xl overflow-y-auto rounded border border-white/15 bg-slate-950 p-4 text-slate-100 shadow-2xl" onSubmit={(event) => void createTask(event)}>
            <div className="flex items-center justify-between gap-2"><h2 id="work-board-create-title" className="text-lg font-semibold">Create a goal-linked task</h2><button type="button" className="cockpit-feedback-button" onClick={closeCreateDialog} disabled={createBusy || Boolean(pendingCreate)}>Close</button></div>
            <p className="mt-1 text-xs opacity-70">A free-text idea starts in Triage. Todo requires a typed capability input and current runtime-limit acknowledgment. The dispatcher alone promotes eligible work to Ready.</p>
            {pendingCreate && <p className="mt-2 rounded border border-amber-500/40 p-2 text-sm" role="status">This exact task request has an unconfirmed receipt. Its fields and idempotency key are held until the server confirms the existing task or accepts the same request.</p>}
            {createError && <div className="mt-2 rounded border border-amber-500/40 p-2 text-sm" role="alert">{createError}</div>}
            <fieldset disabled={Boolean(pendingCreate)} className="mt-3 grid gap-3 sm:grid-cols-2">
              <label className="sm:col-span-2">Title<input className="cockpit-input mt-1 w-full" autoFocus={!pendingCreate} maxLength={200} required value={createDraft.title} onChange={(event) => setCreateField("title", event.currentTarget.value)} /></label>
              <label className="sm:col-span-2">Bounded task description<textarea className="cockpit-input mt-1 w-full" maxLength={4000} rows={3} value={createDraft.body} onChange={(event) => setCreateField("body", event.currentTarget.value)} /></label>
              <label>Goal<select className="cockpit-input mt-1 w-full" required value={createDraft.goalId} onChange={(event) => selectGoal(event.currentTarget.value)}><option value="">Choose a goal</option>{allGoals.map((goal) => <option key={goal.id} value={goal.id} disabled={goal.ownership_access === "recovered_read_only"}>{goal.title} · {goal.id}</option>)}</select></label>
              <label>Goal revision<input className="cockpit-input mt-1 w-full" type="number" min={1} readOnly value={createDraft.goalRevision} aria-readonly="true" /></label>
              <label>Initial status<select className="cockpit-input mt-1 w-full" value={createDraft.status} onChange={(event) => setCreateField("status", event.currentTarget.value as CreateDraft["status"])}><option value="triage">Triage · rough idea</option><option value="todo">Todo · specified</option></select></label>
              <label>Priority 0–100<input className="cockpit-input mt-1 w-full" type="number" min={0} max={100} value={createDraft.priority} onChange={(event) => setCreateField("priority", event.currentTarget.value)} /></label>
              <label>Capability ID<input className="cockpit-input mt-1 w-full" maxLength={128} value={createDraft.capabilityId} onChange={(event) => setCreateField("capabilityId", event.currentTarget.value)} /></label>
              <label>Executor ID (optional)<input className="cockpit-input mt-1 w-full" maxLength={128} value={createDraft.executorId} onChange={(event) => setCreateField("executorId", event.currentTarget.value)} /></label>
              <label>Typed input reference<input className="cockpit-input mt-1 w-full" maxLength={512} value={createDraft.typedInputRef} onChange={(event) => setCreateField("typedInputRef", event.currentTarget.value)} /></label>
              <label>Typed input SHA-256<input className="cockpit-input mt-1 w-full font-mono" maxLength={64} value={createDraft.typedInputDigest} onChange={(event) => setCreateField("typedInputDigest", event.currentTarget.value)} /></label>
              <label>Assignee ID (optional)<input className="cockpit-input mt-1 w-full" maxLength={128} value={createDraft.assigneeId} onChange={(event) => setCreateField("assigneeId", event.currentTarget.value)} /></label>
              <label>Scheduled at (optional)<input className="cockpit-input mt-1 w-full" type="datetime-local" value={createDraft.scheduledAt} onChange={(event) => setCreateField("scheduledAt", event.currentTarget.value)} /></label>
              <label className="flex items-center gap-2"><input type="checkbox" checked={createDraft.requiresReview} onChange={(event) => setCreateField("requiresReview", event.currentTarget.checked)} />Require review</label>
              {createDraft.requiresReview && <label>Named reviewer ID<input className="cockpit-input mt-1 w-full" maxLength={128} required value={createDraft.reviewerId} onChange={(event) => setCreateField("reviewerId", event.currentTarget.value)} /></label>}
              <div className="sm:col-span-2 rounded border border-white/10 p-2">
                <div className="font-semibold">Effective runtime limit</div>
                {!createDraft.goalId ? <div className="text-xs opacity-70">Choose a goal to read its current server-derived execution limit.</div> : createLimit ? <><div>{createLimit.effective_max_runtime_seconds} seconds · source {createLimit.limit_source} · hard ceiling {createLimit.hard_max_runtime_seconds} seconds · {createLimit.attempt_limit} attempts</div><label className="mt-2 flex items-start gap-2"><input type="checkbox" checked={createLimitAcknowledged} onChange={(event) => setCreateLimitAcknowledged(event.currentTarget.checked)} /><span>I acknowledge this current limit. It cannot be increased from the board.</span></label></> : <div className="text-xs text-amber-200">{createLimitError ?? "Checking the current goal revision and limit…"}</div>}
              </div>
            </fieldset>
            <div className="mt-3 flex flex-wrap justify-end gap-2"><button type="button" className="cockpit-feedback-button" onClick={closeCreateDialog} disabled={createBusy || Boolean(pendingCreate)}>Cancel</button><button type="submit" className="cockpit-feedback-button" disabled={createBusy || (!pendingCreate && (!createDraft.goalId || !createDraft.goalRevision || (createDraft.status === "todo" && (!createLimit || !createLimitAcknowledged || !createDraft.capabilityId.trim() || !createDraft.typedInputRef.trim() || !hasValidDigest(createDraft.typedInputDigest)))))}>{createBusy ? "Creating…" : pendingCreate ? "Retry create and reconcile" : createDraft.status === "triage" ? "Create in Triage" : "Create specified Todo"}</button></div>
          </form>
        </div>
      )}
      {formatterOpen && <JsonFormatterPanel key={`${ownerPrincipalId}:${ownerSessionId}:formatter-create`}
        goals={allGoals} ownerPrincipalId={ownerPrincipalId} ownerSessionId={ownerSessionId}
        onClose={() => setFormatterOpen(false)} onCreated={async (task) => {
          setFormatterOpen(false);await refreshSnapshot();if (!stoppedRef.current) openTask(task.task_id);
        }} />}
      {researchOpen && <ResearchDossierPanel key={`${ownerPrincipalId}:${ownerSessionId}:research-create`}
        goals={allGoals} ownerPrincipalId={ownerPrincipalId} ownerSessionId={ownerSessionId}
        onClose={() => setResearchOpen(false)} onCreated={async (task) => {
          setResearchOpen(false);
          await refreshSnapshot();
          if (!stoppedRef.current) openTask(task.task_id);
        }} />}
      {browserTaskOpen && (
        <BrowserTaskForm
          key={pendingCreateScope ?? "anonymous"}
          goals={allGoals}
          initialPending={pendingBrowserAtMount}
          onPendingChange={setBrowserPending}
          ownerPrincipalId={ownerPrincipalId}
          ownerSessionId={ownerSessionId}
          onClose={closeBrowserTask}
          onCreated={async (task, receipt) => {
            if (pendingCreateScope) pendingBrowserSubmissions.delete(pendingCreateScope);
            setBrowserTaskReceipt(receipt);
            setBrowserTaskOpen(false);
            await refreshSnapshot();
            if (stoppedRef.current) return;
            openTask(task.task_id);
            setAnnouncement(`Public browser task ${task.title} was created with input artifact ${receipt.artifactId}, ${receipt.actionCount} actions, digest ${receipt.digest}.`);
          }}
        />
      )}
      {calendarPrepOpen && (
        <CalendarPrepForm
          key={pendingCreateScope ?? "anonymous"}
          goals={allGoals}
          initialPending={pendingCalendarAtMount}
          onPendingChange={setCalendarPending}
          onClose={closeCalendarPrep}
          onOpenSettings={() => setAnnouncement("Open Settings → Calendar to configure an active read-only connection.")}
          onCreated={async (task, receipt) => {
            if (pendingCreateScope) pendingCalendarSubmissions.delete(pendingCreateScope);
            setCalendarPrepReceipt(receipt);
            setCalendarPrepOpen(false);
            await refreshSnapshot();
            if (stoppedRef.current) return;
            openTask(task.task_id);
            setAnnouncement(`Calendar meeting preparation task ${task.title} was created with artifact ${receipt.input_artifact.artifact_id}.`);
          }}
        />
      )}
      {repoRepairOpen && (
        <RepoRepairForm
          key={pendingCreateScope ?? "anonymous"}
          goals={allGoals}
          initialPending={pendingRepoRepairAtMount}
          onPendingChange={setRepoRepairPending}
          ownerPrincipalId={ownerPrincipalId}
          ownerSessionId={ownerSessionId}
          onClose={closeRepoRepair}
          onCreated={async (task, receipt) => {
            if (pendingCreateScope) pendingRepoRepairSubmissions.delete(pendingCreateScope);
            setRepoRepairReceipt(receipt);
            setRepoRepairOpen(false);
            await refreshSnapshot();
            if (stoppedRef.current) return;
            openTask(task.task_id);
            setAnnouncement(`Repository repair task ${task.title} was created with input artifact ${receipt.artifactId}.`);
          }}
        />
      )}
      </div>, document.querySelector(".cockpit-shell") ?? document.body)}
    </section>
  );
}

export { WorkBoardPanel, buildBoardSocketUrl, eventSummary };
