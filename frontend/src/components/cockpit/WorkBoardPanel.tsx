import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type DragEvent,
  type FormEvent,
} from "react";

import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import { resolveWebSocketUrl } from "../../hooks/useWebSocket";
import type {
  WorkBoardAttempt,
  WorkBoardComment,
  WorkBoardEvent,
  WorkBoardLink,
  WorkBoardReference,
  WorkBoardSafeReference,
  WorkBoardTask,
  WorkBoardTaskDetail,
  WorkBoardTaskListResponse,
  WorkBoardTaskStatus,
} from "../../types";

export const WORK_BOARD_STATUSES: WorkBoardTaskStatus[] = [
  "triage",
  "todo",
  "ready",
  "running",
  "blocked",
  "review",
  "done",
];

const STATUS_LABELS: Record<WorkBoardTaskStatus, string> = {
  triage: "Triage",
  todo: "Todo",
  ready: "Ready",
  running: "Running",
  blocked: "Blocked",
  review: "Review",
  done: "Done",
  archived: "Archived",
};

/** Client-side transition affordances. The API remains authoritative. */
const DRAG_TARGETS: Partial<Record<WorkBoardTaskStatus, WorkBoardTaskStatus[]>> = {
  triage: ["todo"],
  done: ["archived"],
};

const DEFAULT_PRIORITY = 50;
const MAX_TITLE_LENGTH = 200;
const MAX_BODY_LENGTH = 4000;

interface WorkBoardPanelProps {
  onStatus?: (message: string) => void;
}

interface CreateDraft {
  title: string;
  body: string;
  goalId: string;
  goalRevision: string;
  capabilityId: string;
  typedInputRef: string;
  typedInputDigest: string;
  executorId: string;
  assigneeId: string;
  reviewerId: string;
  priority: string;
  idempotencyScope: string;
  idempotencyKey: string;
  scheduledAt: string;
  parentTaskId: string;
  requiresReview: boolean;
}

interface EditDraft {
  title: string;
  body: string;
  capabilityId: string;
  typedInputRef: string;
  typedInputDigest: string;
  executorId: string;
  assigneeId: string;
  priority: string;
  scheduledAt: string;
}

interface LinkDraft {
  parentTaskId: string;
  childTaskId: string;
  expectedChildRevision: string;
}

interface ExecutionLimits {
  goalId: string;
  goalRevision: number;
  effectiveMaxRuntimeSeconds: number;
  defaultMaxRuntimeSeconds: number;
  hardMaxRuntimeSeconds: number;
  attemptLimit: number;
  limitSource: string;
}

interface WorkBoardReadbackAttempt extends WorkBoardAttempt {
  readback_refs?: WorkBoardReference[];
  readback_reason?: string | null;
  verification_status?: string | null;
}

interface WorkBoardReadbackTaskDetail extends WorkBoardTaskDetail {
  readback_status?: string | null;
  readback_refs?: WorkBoardReference[];
  readback_reason?: string | null;
  verification_status?: string | null;
  attempts: WorkBoardReadbackAttempt[];
}

type BoardSocketState = "connecting" | "connected" | "reconnecting" | "disconnected";

type WorkBoardAction = "promote" | "unblock" | "retry" | "cancel" | "archive";

class WorkBoardApiError extends Error {
  status: number;
  detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "WorkBoardApiError";
    this.status = status;
    this.detail = detail;
  }
}

function makeIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `board-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function initialCreateDraft(): CreateDraft {
  return {
    title: "",
    body: "",
    goalId: "",
    goalRevision: "",
    capabilityId: "",
    typedInputRef: "",
    typedInputDigest: "",
    executorId: "",
    assigneeId: "",
    reviewerId: "",
    priority: String(DEFAULT_PRIORITY),
    idempotencyScope: "operator-work-board",
    idempotencyKey: makeIdempotencyKey(),
    scheduledAt: "",
    parentTaskId: "",
    requiresReview: false,
  };
}

function editDraftFor(task: WorkBoardTask): EditDraft {
  return {
    title: task.title,
    body: task.body ?? "",
    capabilityId: task.capability_id ?? "",
    typedInputRef: task.typed_input_ref ?? "",
    typedInputDigest: task.typed_input_digest ?? "",
    executorId: task.executor_id ?? "",
    assigneeId: task.assignee_id ?? "",
    priority: String(task.priority),
    scheduledAt: task.scheduled_at ?? "",
  };
}

function normalizeDependencyLinks(
  value: unknown,
  taskId: string | undefined,
  direction: "parent" | "child",
): WorkBoardLink[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((link) => {
    if (typeof link === "string" && taskId) {
      return direction === "parent"
        ? [{ parent_task_id: link, child_task_id: taskId }]
        : [{ parent_task_id: taskId, child_task_id: link }];
    }
    if (link && typeof link === "object" && !Array.isArray(link)) return [link as WorkBoardLink];
    return [];
  });
}

function detailFromPayload(payload: unknown): WorkBoardReadbackTaskDetail | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
  const record = payload as Record<string, unknown>;
  const task = record.task ?? payload;
  if (!task || typeof task !== "object" || Array.isArray(task)) return null;
  const taskRecord = task as Record<string, unknown>;
  const taskId = typeof taskRecord.task_id === "string" ? taskRecord.task_id : undefined;
  const detail = {
    ...taskRecord,
    revision: typeof record.revision === "number"
      ? record.revision
      : typeof taskRecord.revision === "number" ? taskRecord.revision : undefined,
    attempts: normalizeAttempts(
      Array.isArray(record.attempts) ? record.attempts : taskRecord.attempts,
    ),
    parents: normalizeDependencyLinks(
      Array.isArray(record.parents) ? record.parents : taskRecord.parents,
      taskId,
      "parent",
    ),
    children: normalizeDependencyLinks(
      Array.isArray(record.children) ? record.children : taskRecord.children,
      taskId,
      "child",
    ),
    comments: Array.isArray(record.comments)
      ? record.comments
      : Array.isArray(taskRecord.comments) ? taskRecord.comments : [],
    events: Array.isArray(record.events)
      ? record.events
      : Array.isArray(taskRecord.events) ? taskRecord.events : [],
    readback_status: typeof record.readback_status === "string"
      ? record.readback_status
      : typeof taskRecord.readback_status === "string" ? taskRecord.readback_status : null,
    readback_refs: Array.isArray(record.readback_refs)
      ? record.readback_refs
      : Array.isArray(taskRecord.readback_refs) ? taskRecord.readback_refs : [],
    readback_reason: typeof record.readback_reason === "string"
      ? record.readback_reason
      : typeof taskRecord.readback_reason === "string" ? taskRecord.readback_reason : null,
    verification_status: typeof record.verification_status === "string"
      ? record.verification_status
      : typeof taskRecord.verification_status === "string" ? taskRecord.verification_status : null,
    recovery_action: typeof record.recovery_action === "string"
      ? record.recovery_action
      : typeof taskRecord.recovery_action === "string" ? taskRecord.recovery_action : null,
  } as WorkBoardReadbackTaskDetail;
  return detail;
}

function executionLimitsFromPayload(
  payload: unknown,
  expectedGoalId: string,
  expectedGoalRevision: number,
): ExecutionLimits | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
  const record = payload as Record<string, unknown>;
  const goalId = typeof record.goal_id === "string" ? record.goal_id : "";
  const goalRevision = typeof record.goal_revision === "number" ? record.goal_revision : 0;
  const effective = record.effective_max_runtime_seconds;
  const defaultLimit = record.default_max_runtime_seconds;
  const hardLimit = record.hard_max_runtime_seconds;
  const attemptLimit = record.attempt_limit;
  const source = typeof record.limit_source === "string" ? record.limit_source.trim() : "";
  if (
    goalId !== expectedGoalId
    || goalRevision !== expectedGoalRevision
    || typeof effective !== "number"
    || typeof defaultLimit !== "number"
    || typeof hardLimit !== "number"
    || typeof attemptLimit !== "number"
    || !source
    || effective < 1
    || defaultLimit < 1
    || hardLimit < effective
    || attemptLimit < 1
  ) return null;
  return {
    goalId,
    goalRevision,
    effectiveMaxRuntimeSeconds: effective,
    defaultMaxRuntimeSeconds: defaultLimit,
    hardMaxRuntimeSeconds: hardLimit,
    attemptLimit,
    limitSource: source,
  };
}

function normalizeAttempts(value: unknown): WorkBoardReadbackAttempt[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((attempt) => {
    if (!attempt || typeof attempt !== "object" || Array.isArray(attempt)) return [];
    const record = attempt as Record<string, unknown>;
    if (typeof record.attempt_id !== "string" || typeof record.task_revision_at_claim !== "number") {
      return [];
    }
    return [record as unknown as WorkBoardReadbackAttempt];
  });
}

function tasksFromPayload(payload: unknown): {
  tasks: WorkBoardTask[];
  nextAfter: string | null;
  lastEventId: number | null;
  gap: boolean;
} {
  if (Array.isArray(payload)) return { tasks: payload as WorkBoardTask[], nextAfter: null, lastEventId: null, gap: false };
  if (!payload || typeof payload !== "object") return { tasks: [], nextAfter: null, lastEventId: null, gap: false };
  const record = payload as Partial<WorkBoardTaskListResponse> & Record<string, unknown>;
  const tasks = Array.isArray(record.tasks) ? record.tasks as WorkBoardTask[] : [];
  const nextAfter = typeof record.next_after === "number" || typeof record.next_after === "string"
    ? String(record.next_after)
    : null;
  const lastEventId = typeof record.last_event_id === "number" ? record.last_event_id : null;
  return { tasks, nextAfter, lastEventId, gap: record.gap === true };
}

function eventCursorFromPayload(payload: unknown): number | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
  const value = (payload as Record<string, unknown>).last_event_id;
  return typeof value === "number" ? value : null;
}

function errorDetail(payload: unknown, fallback: string): string {
  if (typeof payload === "string" && payload.trim()) return payload;
  if (payload && typeof payload === "object" && !Array.isArray(payload)) {
    const record = payload as Record<string, unknown>;
    if (typeof record.detail === "string" && record.detail.trim()) return record.detail;
    if (record.detail && typeof record.detail === "object") {
      const detail = record.detail as Record<string, unknown>;
      if (typeof detail.reason === "string" && detail.reason.trim()) return detail.reason;
      if (typeof detail.message === "string" && detail.message.trim()) return detail.message;
      if (typeof detail.error === "string" && detail.error.trim()) return detail.error;
    }
    if (typeof record.reason === "string" && record.reason.trim()) return record.reason;
    if (typeof record.error === "string" && record.error.trim()) return record.error;
  }
  return fallback;
}

function formatAge(value?: string | null): string {
  if (!value) return "age unknown";
  const timestamp = new Date(value).getTime();
  if (!Number.isFinite(timestamp)) return "age unknown";
  const seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h`;
  return `${Math.floor(hours / 24)}d`;
}

function isTargetLegal(task: WorkBoardTask, target: WorkBoardTaskStatus): boolean {
  if (task.status === "triage" && target === "todo") {
    return Boolean(task.typed_input_ref?.trim() && task.typed_input_digest?.trim());
  }
  return DRAG_TARGETS[task.status]?.includes(target) ?? false;
}

function taskRevision(task: WorkBoardTask): number {
  return task.revision ?? task.task_revision ?? 0;
}

async function requestBoardJson(path: string, init: RequestInit = {}): Promise<unknown> {
  const response = await apiFetch(`${API_URL}${path}`, init);
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    throw new WorkBoardApiError(response.status, errorDetail(payload, `Work board request failed (${response.status})`));
  }
  return payload;
}

function actionLabel(action: string): string {
  return action.replace(/_/g, " ");
}

function safeReferenceHref(reference: string): string | null {
  const value = reference.trim();
  if (!value || /[\u0000-\u001f\u007f]/.test(value)) return null;
  if (/^https?:\/\//i.test(value)) {
    try {
      const url = new URL(value);
      return url.protocol === "http:" || url.protocol === "https:" ? url.toString() : null;
    } catch {
      return null;
    }
  }
  // These are the existing authenticated artifact/readback routes. Arbitrary
  // workspace paths and unknown API paths remain identifiers until the
  // backend supplies a route-bound reference.
  if (/%2f|%5c|(?:^|\/)\.\.(?:\/|$)/i.test(value)) return null;
  const knownArtifactRoute = /^\/api\/(?:nodes\/edge\/artifacts\/[^/?#]+(?:\/content)?|observer\/screen-artifacts\/[^/?#]+\/(?:image|provider-output|codex-output|analysis))$/i;
  if (knownArtifactRoute.test(value)) {
    return `${API_URL}${value}`;
  }
  return null;
}

function recoveryEligibility(task: WorkBoardTask | WorkBoardReadbackTaskDetail): string | null {
  if (!("attempts" in task) || !Array.isArray(task.attempts)) return null;
  const value = (task as WorkBoardTask & { recovery_action?: unknown }).recovery_action;
  if (value !== "unblock" || task.status !== "blocked" || (task.block_kind as string | null | undefined) !== "operator") return null;
  if (task.attempts.length > 0 || taskRevision(task) < 1) return null;
  return value;
}

const PENDING_EVIDENCE_STATUSES = new Set(["", "pending", "unknown", "in_progress", "running"]);
const SUCCESS_EVIDENCE_STATUSES = new Set(["succeeded", "success", "passed", "verified", "complete", "completed"]);

function evidenceStatusFromReferences(references?: WorkBoardReference[]): string | null {
  if (!references?.length) return null;
  let succeeded = false;
  let terminal: string | null = null;
  for (const reference of references) {
    if (!reference || typeof reference !== "object" || Array.isArray(reference)) continue;
    const status = typeof reference.status === "string" ? reference.status.trim().toLowerCase() : "";
    if (reference.verified === true && (!status || SUCCESS_EVIDENCE_STATUSES.has(status))) return "passed";
    if (SUCCESS_EVIDENCE_STATUSES.has(status)) {
      succeeded = true;
      continue;
    }
    if (status && !PENDING_EVIDENCE_STATUSES.has(status) && !terminal) terminal = status;
  }
  return succeeded ? "succeeded" : terminal;
}

function evidenceStatusFromAttempt(attempt?: WorkBoardReadbackAttempt | null): string | null {
  if (!attempt) return null;
  const explicit = [attempt.readback_status, attempt.verification_status]
    .find((value) => typeof value === "string" && value.trim());
  const normalizedExplicit = typeof explicit === "string" ? explicit.trim().toLowerCase() : "";
  const derived = evidenceStatusFromReferences([
    ...(attempt.receipt_refs ?? []),
    ...(attempt.readback_refs ?? []),
  ]);
  if (explicit && !PENDING_EVIDENCE_STATUSES.has(normalizedExplicit)) return explicit;
  return derived ?? explicit ?? null;
}

function evidenceStatus(task: WorkBoardTask | WorkBoardReadbackTaskDetail): string {
  const detail = task as WorkBoardReadbackTaskDetail;
  const explicit = [detail.readback_status, detail.verification_status]
    .find((value) => typeof value === "string" && value.trim());
  const normalizedExplicit = typeof explicit === "string" ? explicit.trim().toLowerCase() : "";
  const latestAttempt = detail.attempts?.[detail.attempts.length - 1]
    ?? (task.latest_attempt as WorkBoardReadbackAttempt | null | undefined);
  const derived = evidenceStatusFromAttempt(latestAttempt)
    ?? evidenceStatusFromReferences([
      ...(detail.readback_refs ?? []),
      ...(task.result_refs ?? []),
    ]);
  if (explicit && !PENDING_EVIDENCE_STATUSES.has(normalizedExplicit)) return explicit;
  return derived ?? explicit ?? "pending";
}

function serverSelectionRank(task: WorkBoardTask): number | null {
  const record = task as WorkBoardTask & {
    selection_rank?: unknown;
    dispatch_rank?: unknown;
  };
  if (typeof record.selection_rank === "number") return record.selection_rank;
  if (typeof record.dispatch_rank === "number") return record.dispatch_rank;
  return null;
}

const SAFE_REFERENCE_FIELDS: Array<[keyof WorkBoardSafeReference, string]> = [
  ["artifact_id", "artifact"],
  ["artifact_type", "type"],
  ["file_path", "file"],
  ["content_sha256", "sha256"],
  ["size_bytes", "size"],
  ["exists", "exists"],
  ["effect_id", "effect"],
  ["effect_type", "effect_type"],
  ["status", "status"],
  ["verified", "verified"],
  ["target_digest", "target_sha256"],
  ["target_path", "target"],
  ["job_id", "job"],
  ["workflow_run_id", "run"],
  ["recovery_action", "recovery"],
  ["reason_code", "reason"],
  ["error_code", "error"],
  ["child_job_id", "child_job"],
];

function formatSafeReference(reference: WorkBoardReference): string | null {
  if (typeof reference === "string") {
    const value = reference.trim();
    if (!value || value.length > 512 || /[\u0000-\u001f\u007f]/.test(value)) return null;
    return value;
  }
  if (!reference || typeof reference !== "object" || Array.isArray(reference)) return null;

  const parts: string[] = [];
  for (const [field, label] of SAFE_REFERENCE_FIELDS) {
    const value = reference[field];
    if (value === null || value === undefined) continue;
    if (typeof value !== "string" && typeof value !== "number" && typeof value !== "boolean") continue;
    const text = String(value).trim();
    if (!text || text.length > 512 || /[\u0000-\u001f\u007f]/.test(text)) continue;
    parts.push(`${label}=${text}`);
  }
  return parts.length ? parts.join(" · ").slice(0, 1024) : null;
}

function ReferenceList({ label, references }: { label: string; references?: WorkBoardReference[] }) {
  const values = references?.map(formatSafeReference).filter((reference): reference is string => Boolean(reference)) ?? [];
  return (
    <div className="work-board-reference-list">
      <strong>{label}</strong>
      {values.length ? (
        <ul>
          {values.map((reference, index) => {
            const href = safeReferenceHref(reference);
            const external = /^https?:\/\//i.test(reference.trim());
            return (
              <li key={`${reference}:${index}`}>
                {href ? (
                  <a href={href} target={external ? "_blank" : undefined} rel={external ? "noreferrer" : undefined}>
                    {reference}
                  </a>
                ) : <code>{reference}</code>}
                {!href ? <small>identifier; authenticated route pending</small> : null}
              </li>
            );
          })}
        </ul>
      ) : <span>none returned.</span>}
    </div>
  );
}

function taskListParams(
  statusFilter: WorkBoardTaskStatus | "all",
  assigneeFilter: string,
  query: string,
  after?: string | null,
): string {
  const params = new URLSearchParams();
  if (statusFilter !== "all") params.set("status", statusFilter);
  if (assigneeFilter.trim()) params.set("assignee_id", assigneeFilter.trim());
  if (query.trim()) params.set("q", query.trim());
  if (after) params.set("after", after);
  params.set("limit", "100");
  return params.toString();
}

const DIALOG_FOCUSABLE_SELECTOR = [
  "button:not([disabled])",
  "[href]",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

function dialogFocusable(dialog: HTMLElement): HTMLElement[] {
  return Array.from(dialog.querySelectorAll<HTMLElement>(DIALOG_FOCUSABLE_SELECTOR))
    .filter((element) => !element.hidden && element.getAttribute("aria-hidden") !== "true");
}

export function WorkBoardPanel({ onStatus }: WorkBoardPanelProps) {
  const [tasks, setTasks] = useState<WorkBoardTask[]>([]);
  const [selectedTask, setSelectedTask] = useState<WorkBoardReadbackTaskDetail | null>(null);
  const [selectedLoading, setSelectedLoading] = useState(false);
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<WorkBoardTaskStatus | "all">("all");
  const [assigneeFilter, setAssigneeFilter] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [nextAfter, setNextAfter] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [socketState, setSocketState] = useState<BoardSocketState>("connecting");
  const [lastEventId, setLastEventId] = useState<number | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [createDraft, setCreateDraft] = useState<CreateDraft>(() => initialCreateDraft());
  const [createExecutionLimits, setCreateExecutionLimits] = useState<ExecutionLimits | null>(null);
  const [createLimitsLoading, setCreateLimitsLoading] = useState(false);
  const [createLimitsError, setCreateLimitsError] = useState<string | null>(null);
  const [createLimitsAcknowledged, setCreateLimitsAcknowledged] = useState(false);
  const [editDraft, setEditDraft] = useState<EditDraft | null>(null);
  const [commentDraft, setCommentDraft] = useState("");
  const [blockReasonDraft, setBlockReasonDraft] = useState("");
  const [unblockResolutionDraft, setUnblockResolutionDraft] = useState("");
  const [detailExecutionLimits, setDetailExecutionLimits] = useState<ExecutionLimits | null>(null);
  const [detailLimitsLoading, setDetailLimitsLoading] = useState(false);
  const [detailLimitsError, setDetailLimitsError] = useState<string | null>(null);
  const [detailLimitsAcknowledged, setDetailLimitsAcknowledged] = useState(false);
  const [linkDraft, setLinkDraft] = useState<LinkDraft>({
    parentTaskId: "",
    childTaskId: "",
    expectedChildRevision: "",
  });
  const [mutationKey, setMutationKey] = useState<string | null>(null);
  const [draggedTaskId, setDraggedTaskId] = useState<string | null>(null);
  const [dropTarget, setDropTarget] = useState<WorkBoardTaskStatus | null>(null);
  const [showArchived, setShowArchived] = useState(false);
  const cursorRef = useRef<number | null>(null);
  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof window.setTimeout> | null>(null);
  const loadedPageCountRef = useRef(1);
  const boardRequestSequenceRef = useRef(0);
  const boardAbortControllerRef = useRef<AbortController | null>(null);
  const selectedRequestSequenceRef = useRef(0);
  const selectedAbortControllerRef = useRef<AbortController | null>(null);
  const selectedTaskIdRef = useRef<string | null>(null);
  const openTaskRef = useRef<(
    taskId: string,
    clearError?: boolean,
    trigger?: HTMLElement,
    preserveDrafts?: boolean,
  ) => Promise<boolean>>(async () => false);
  const dialogTriggerRef = useRef<HTMLElement | null>(null);
  const createDialogRef = useRef<HTMLDivElement | null>(null);
  const detailDialogRef = useRef<HTMLElement | null>(null);
  const loadBoardRef = useRef<(preservePages?: boolean, clearError?: boolean) => Promise<boolean>>(async () => false);
  const mountedRef = useRef(true);
  const editDraftRef = useRef<EditDraft | null>(null);
  const commentDraftRef = useRef("");

  const announce = useCallback((message: string) => {
    setAnnouncement(message);
    onStatus?.(message);
  }, [onStatus]);

  useEffect(() => {
    editDraftRef.current = editDraft;
    commentDraftRef.current = commentDraft;
  }, [commentDraft, editDraft]);

  const loadBoard = useCallback(async (preservePages = false, clearError = true) => {
    boardAbortControllerRef.current?.abort();
    const controller = new AbortController();
    boardAbortControllerRef.current = controller;
    const requestSequence = ++boardRequestSequenceRef.current;
    const isCurrentRequest = () => mountedRef.current && requestSequence === boardRequestSequenceRef.current;
    setLoading(true);
    setLoadingMore(false);
    try {
      const requestedPages = preservePages ? Math.max(1, loadedPageCountRef.current) : 1;
      const loadedTasks: WorkBoardTask[] = [];
      let after: string | null = null;
      let pageCount = 0;
      let lastPage = { nextAfter: null as string | null, lastEventId: null as number | null, gap: false };
      let snapshotEventId: number | null = null;
      do {
        const payload = await requestBoardJson(`/api/work-board/tasks?${taskListParams(statusFilter, assigneeFilter, query, after)}`, {
          signal: controller.signal,
        });
        if (!isCurrentRequest()) return false;
        const next = tasksFromPayload(payload);
        loadedTasks.push(...next.tasks);
        if (pageCount === 0) snapshotEventId = next.lastEventId;
        lastPage = next;
        pageCount += 1;
        after = next.nextAfter;
      } while (pageCount < requestedPages && after);
      if (!isCurrentRequest()) return false;
      setTasks(loadedTasks);
      setNextAfter(lastPage.nextAfter);
      loadedPageCountRef.current = pageCount;
      if (snapshotEventId != null) {
        // Every page is read from its own SQLite snapshot. Only page one owns
        // the event cursor; advancing from a later page can skip events.
        cursorRef.current = snapshotEventId;
        setLastEventId(snapshotEventId);
      } else if (cursorRef.current == null) {
        // The M1 list response is intentionally task-shaped. Bootstrap the
        // authenticated event cursor before opening the live stream.
        try {
          const eventPayload = await requestBoardJson("/api/work-board/events?after=0&limit=1", {
            signal: controller.signal,
          });
          if (!isCurrentRequest()) return false;
          const eventCursor = eventCursorFromPayload(eventPayload);
          if (eventCursor != null && isCurrentRequest()) {
            cursorRef.current = eventCursor;
            setLastEventId(eventCursor);
          }
        } catch {
          if (!isCurrentRequest()) return false;
          // A missing cursor is safe: the socket starts from zero and a gap
          // frame forces a fresh snapshot before resuming.
        }
      }
      if (clearError) setError(null);
      return true;
    } catch (caught) {
      if (!isCurrentRequest()) return false;
      if (caught instanceof DOMException && caught.name === "AbortError") return false;
      const message = caught instanceof WorkBoardApiError
        ? caught.detail
        : "Work board is unavailable; retry when the authenticated API recovers.";
      setError(message);
      announce(message);
      return false;
    } finally {
      if (isCurrentRequest()) {
        setLoading(false);
        if (boardAbortControllerRef.current === controller) boardAbortControllerRef.current = null;
      }
    }
  }, [announce, assigneeFilter, query, statusFilter]);

  const loadMore = useCallback(async () => {
    if (!nextAfter || loading || loadingMore) return;
    boardAbortControllerRef.current?.abort();
    const controller = new AbortController();
    boardAbortControllerRef.current = controller;
    const requestSequence = ++boardRequestSequenceRef.current;
    const isCurrentRequest = () => mountedRef.current && requestSequence === boardRequestSequenceRef.current;
    setLoadingMore(true);
    try {
      const payload = await requestBoardJson(`/api/work-board/tasks?${taskListParams(statusFilter, assigneeFilter, query, nextAfter)}`, {
        signal: controller.signal,
      });
      if (!isCurrentRequest()) return;
      const next = tasksFromPayload(payload);
      setTasks((current) => [...current, ...next.tasks]);
      setNextAfter(next.nextAfter);
      loadedPageCountRef.current += 1;
      // Keep the cursor captured from page one. Later pages are separate
      // SQLite snapshots and their event IDs are not a safe resume point.
      setError(null);
    } catch (caught) {
      if (!isCurrentRequest()) return;
      if (caught instanceof DOMException && caught.name === "AbortError") return;
      const message = caught instanceof WorkBoardApiError
        ? caught.detail
        : "More work board tasks are unavailable; retry when the authenticated API recovers.";
      setError(message);
      announce(message);
    } finally {
      if (isCurrentRequest()) {
        setLoadingMore(false);
        if (boardAbortControllerRef.current === controller) boardAbortControllerRef.current = null;
      }
    }
  }, [announce, assigneeFilter, loading, loadingMore, nextAfter, query, statusFilter]);

  useEffect(() => {
    loadBoardRef.current = loadBoard;
  }, [loadBoard]);

  const refreshSnapshot = useCallback(async (clearError = true) => {
    return loadBoardRef.current(true, clearError);
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    void loadBoard();
    return () => {
      mountedRef.current = false;
    };
  }, [loadBoard]);

  useEffect(() => {
    let disposed = false;
    let reconnectDelay = 1500;

    const closeSocket = () => {
      socketRef.current?.close();
      socketRef.current = null;
    };

    const scheduleReconnect = () => {
      if (disposed) return;
      reconnectTimerRef.current = window.setTimeout(async () => {
        if (disposed) return;
        const snapshotLoaded = await refreshSnapshot();
        if (!snapshotLoaded || disposed) {
          setSocketState("disconnected");
          reconnectDelay = Math.min(30_000, reconnectDelay * 2);
          scheduleReconnect();
          return;
        }
        reconnectDelay = Math.min(30_000, reconnectDelay * 2);
        connect();
      }, reconnectDelay);
    };

    const connect = () => {
      if (disposed || typeof WebSocket === "undefined") return;
      const after = cursorRef.current == null ? "" : `?after=${encodeURIComponent(String(cursorRef.current))}`;
      setSocketState(reconnectDelay > 1500 ? "reconnecting" : "connecting");
      const socket = new WebSocket(resolveWebSocketUrl(`/ws/work-board/events${after}`));
      socketRef.current = socket;
      socket.onopen = () => {
        if (disposed) return;
        reconnectDelay = 1500;
        setSocketState("connected");
      };
      socket.onmessage = (message) => {
        if (disposed) return;
        const refreshEventState = () => {
          const selectedTaskId = selectedTaskIdRef.current;
          if (selectedTaskId) {
            const editDraftSnapshot = editDraftRef.current;
            const commentDraftSnapshot = commentDraftRef.current;
            void openTaskRef.current(selectedTaskId, false, undefined, true).then(
              (detailLoaded) => {
                if (detailLoaded && selectedTaskIdRef.current === selectedTaskId) {
                  if (editDraftSnapshot) setEditDraft(editDraftSnapshot);
                  setCommentDraft(commentDraftSnapshot);
                }
                void refreshSnapshot(detailLoaded);
              },
              () => void refreshSnapshot(false),
            );
          } else {
            void refreshSnapshot();
          }
        };
        try {
          const payload = JSON.parse(String(message.data)) as Record<string, unknown>;
          const eventId = typeof payload.event_id === "number"
            ? payload.event_id
            : payload.event && typeof payload.event === "object" && typeof (payload.event as Record<string, unknown>).event_id === "number"
              ? (payload.event as Record<string, unknown>).event_id as number
              : null;
          if (eventId != null) {
            cursorRef.current = eventId;
            setLastEventId(eventId);
          }
          if (payload.type === "cursor_gap" || payload.gap === true) {
            announce("Board event history has a gap; refreshed the canonical task snapshot.");
          }
          refreshEventState();
        } catch {
          announce("Received an unreadable board event; refreshed the canonical task snapshot.");
          refreshEventState();
        }
      };
      socket.onerror = () => {
        if (!disposed) setSocketState("reconnecting");
      };
      socket.onclose = () => {
        if (disposed) return;
        setSocketState("reconnecting");
        scheduleReconnect();
      };
    };

    // The first socket is opened only after a snapshot so its cursor is valid.
    void refreshSnapshot().then((snapshotLoaded) => {
      if (snapshotLoaded && !disposed) {
        connect();
      } else if (!disposed) {
        setSocketState("disconnected");
        scheduleReconnect();
      }
    });
    return () => {
      disposed = true;
      if (reconnectTimerRef.current) window.clearTimeout(reconnectTimerRef.current);
      closeSocket();
    };
  }, [announce, refreshSnapshot]);

  const openTask = useCallback(async (
    taskId: string,
    clearError = true,
    trigger?: HTMLElement,
    preserveDrafts = false,
  ) => {
    if (trigger) dialogTriggerRef.current = trigger;
    selectedAbortControllerRef.current?.abort();
    const controller = new AbortController();
    selectedAbortControllerRef.current = controller;
    const requestSequence = ++selectedRequestSequenceRef.current;
    const isCurrentRequest = () => mountedRef.current && requestSequence === selectedRequestSequenceRef.current;
    setSelectedLoading(true);
    try {
      const payload = await requestBoardJson(`/api/work-board/tasks/${encodeURIComponent(taskId)}`, {
        signal: controller.signal,
      });
      if (!isCurrentRequest()) return false;
      const detail = detailFromPayload(payload);
      if (!detail) throw new Error("Task detail response was empty.");
      if (!isCurrentRequest()) return false;
      setSelectedTask(detail);
      if (!preserveDrafts) {
        setEditDraft(editDraftFor(detail));
        setCommentDraft("");
        setBlockReasonDraft("");
        setUnblockResolutionDraft("");
      }
      setLinkDraft({ parentTaskId: detail.task_id, childTaskId: "", expectedChildRevision: "" });
      if (clearError) setError(null);
      return true;
    } catch (caught) {
      if (!isCurrentRequest() || (caught instanceof DOMException && caught.name === "AbortError")) return false;
      const message = caught instanceof WorkBoardApiError
        ? caught.detail
        : "Task detail is unavailable; refresh the board and retry.";
      setError(message);
      announce(message);
      return false;
    } finally {
      if (isCurrentRequest()) {
        setSelectedLoading(false);
        if (selectedAbortControllerRef.current === controller) selectedAbortControllerRef.current = null;
      }
    }
  }, [announce]);

  useEffect(() => {
    selectedTaskIdRef.current = selectedTask?.task_id ?? null;
  }, [selectedTask?.task_id]);

  useEffect(() => {
    openTaskRef.current = openTask;
  }, [openTask]);

  useEffect(() => {
    if (!createOpen) {
      setCreateExecutionLimits(null);
      setCreateLimitsLoading(false);
      setCreateLimitsError(null);
      setCreateLimitsAcknowledged(false);
      return undefined;
    }
    const goalId = createDraft.goalId.trim();
    const goalRevision = Number(createDraft.goalRevision);
    if (!goalId || !Number.isInteger(goalRevision) || goalRevision < 1) {
      setCreateExecutionLimits(null);
      setCreateLimitsLoading(false);
      setCreateLimitsError(null);
      setCreateLimitsAcknowledged(false);
      return undefined;
    }
    const controller = new AbortController();
    setCreateExecutionLimits(null);
    setCreateLimitsAcknowledged(false);
    setCreateLimitsLoading(true);
    setCreateLimitsError(null);
    void requestBoardJson(`/api/work-board/goals/${encodeURIComponent(goalId)}/execution-limits?goal_revision=${goalRevision}`, {
      signal: controller.signal,
    }).then((payload) => {
      if (controller.signal.aborted) return;
      const limits = executionLimitsFromPayload(payload, goalId, goalRevision);
      if (!limits) {
        setCreateLimitsError("The current goal execution limits could not be verified; task creation is blocked until they are read back.");
        return;
      }
      setCreateExecutionLimits(limits);
    }).catch((caught) => {
      if (controller.signal.aborted) return;
      const message = caught instanceof WorkBoardApiError
        ? caught.detail
        : "Goal execution limits are unavailable; task creation is blocked until the current revision can be verified.";
      setCreateLimitsError(message);
    }).finally(() => {
      if (!controller.signal.aborted) setCreateLimitsLoading(false);
    });
    return () => controller.abort();
  }, [createDraft.goalId, createDraft.goalRevision, createOpen]);

  useEffect(() => {
    if (!selectedTask) {
      setDetailExecutionLimits(null);
      setDetailLimitsLoading(false);
      setDetailLimitsError(null);
      setDetailLimitsAcknowledged(false);
      return undefined;
    }
    const goalId = selectedTask.goal_id.trim();
    const goalRevision = selectedTask.goal_revision;
    const controller = new AbortController();
    setDetailExecutionLimits(null);
    setDetailLimitsAcknowledged(false);
    setDetailLimitsLoading(true);
    setDetailLimitsError(null);
    void requestBoardJson(`/api/work-board/goals/${encodeURIComponent(goalId)}/execution-limits?goal_revision=${goalRevision}`, {
      signal: controller.signal,
    }).then((payload) => {
      if (controller.signal.aborted) return;
      const limits = executionLimitsFromPayload(payload, goalId, goalRevision);
      if (!limits) {
        setDetailLimitsError("The current goal execution limits could not be verified; Ready admission remains blocked.");
        return;
      }
      setDetailExecutionLimits(limits);
    }).catch((caught) => {
      if (controller.signal.aborted) return;
      const message = caught instanceof WorkBoardApiError
        ? caught.detail
        : "Goal execution limits are unavailable; Ready admission remains blocked until the goal revision can be verified.";
      setDetailLimitsError(message);
    }).finally(() => {
      if (!controller.signal.aborted) setDetailLimitsLoading(false);
    });
    return () => controller.abort();
  }, [selectedTask?.goal_id, selectedTask?.goal_revision, selectedTask?.task_id]);

  useEffect(() => {
    const dialog = createOpen ? createDialogRef.current : selectedTask ? detailDialogRef.current : null;
    if (!dialog) return undefined;

    const restoreTarget = dialogTriggerRef.current
      ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null);
    const initialFocus = dialog.querySelector<HTMLElement>("[data-dialog-autofocus]")
      ?? dialogFocusable(dialog)[0]
      ?? dialog;
    initialFocus.focus();

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        if (createOpen) setCreateOpen(false);
        else setSelectedTask(null);
        return;
      }
      if (event.key !== "Tab") return;

      const focusable = dialogFocusable(dialog);
      if (!focusable.length) {
        event.preventDefault();
        dialog.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const active = document.activeElement;
      if (event.shiftKey) {
        if (active === first || !dialog.contains(active)) {
          event.preventDefault();
          last.focus();
        }
      } else if (active === last || !dialog.contains(active)) {
        event.preventDefault();
        first.focus();
      }
    };

    dialog.addEventListener("keydown", handleKeyDown);
    return () => {
      dialog.removeEventListener("keydown", handleKeyDown);
      if (restoreTarget && document.contains(restoreTarget)) restoreTarget.focus();
    };
  }, [createOpen, selectedTask?.task_id]);

  const refreshSelectedTask = useCallback(async (taskId: string | null = selectedTask?.task_id ?? null) => {
    if (!taskId) return false;
    const detailLoaded = await openTask(taskId, false);
    const snapshotLoaded = await refreshSnapshot(false);
    return detailLoaded && snapshotLoaded;
  }, [openTask, refreshSnapshot, selectedTask?.task_id]);

  const runAction = useCallback(async (
    task: WorkBoardTask,
    action: WorkBoardAction,
    extra: Record<string, unknown> = {},
  ): Promise<boolean> => {
    const key = `${task.task_id}:${action}`;
    setMutationKey(key);
    try {
      await requestBoardJson(`/api/work-board/tasks/${encodeURIComponent(task.task_id)}/actions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, expected_revision: taskRevision(task), ...extra }),
      });
      announce(`${actionLabel(action)} requested for ${task.task_id}.`);
      if (await refreshSelectedTask(task.task_id)) {
        setError(null);
        return true;
      }
      return false;
    } catch (caught) {
      const message = caught instanceof WorkBoardApiError
        ? caught.detail
        : `Could not ${actionLabel(action)} ${task.task_id}.`;
      setError(message);
      announce(message);
      await refreshSelectedTask(task.task_id).catch(() => {});
      return false;
    } finally {
      setMutationKey(null);
    }
  }, [announce, refreshSelectedTask]);

  const selectedRecoveryAction = selectedTask ? recoveryEligibility(selectedTask) : null;

  const unblockTask = useCallback(async (task: WorkBoardTask) => {
    if (selectedTask?.task_id !== task.task_id || selectedRecoveryAction !== "unblock") return;
    const resolution = unblockResolutionDraft.trim();
    if (!resolution) {
      announce("A bounded resolution statement is required before unblocking this task.");
      return;
    }
    if (resolution.length > 1000) {
      announce("The unblock resolution statement must be 1000 characters or fewer.");
      return;
    }
    if (await runAction(task, "unblock", { resolution })) {
      setUnblockResolutionDraft("");
    }
  }, [announce, runAction, selectedRecoveryAction, selectedTask?.task_id, unblockResolutionDraft]);

  const transitionTask = useCallback(async (task: WorkBoardTask, target: WorkBoardTaskStatus) => {
    const typedRecoveryTransition = target === "todo"
      && task.status === "blocked"
      && selectedTask?.task_id === task.task_id
      && selectedRecoveryAction === "unblock";
    if (!isTargetLegal(task, target) && !typedRecoveryTransition) {
      announce(`${STATUS_LABELS[target]} is not a legal operator transition from ${STATUS_LABELS[task.status]}.`);
      return;
    }
    if (typedRecoveryTransition) {
      await unblockTask(task);
      return;
    }
    if (
      target === "todo"
      && task.status === "triage"
      && (selectedTask?.task_id !== task.task_id || !detailExecutionLimits || !detailLimitsAcknowledged)
    ) {
      announce("Read and acknowledge the current goal execution limits before promoting this task.");
      return;
    }
    if (target === "archived") {
      if (!window.confirm(`Archive task ${task.task_id}?`)) return;
      await runAction(task, "archive");
      return;
    }
    await runAction(task, "promote");
  }, [announce, detailExecutionLimits, detailLimitsAcknowledged, runAction, selectedRecoveryAction, selectedTask?.task_id, unblockTask]);

  const blockTask = useCallback(async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!selectedTask) return;
    const reason = blockReasonDraft.trim();
    if (!reason) {
      announce("A bounded reason is required before blocking a task.");
      return;
    }
    if (!["triage", "todo", "ready", "review"].includes(selectedTask.status)) {
      announce("This task cannot be manually blocked from its current status.");
      return;
    }
    if (!window.confirm(`Block task ${selectedTask.task_id} with this operator reason?`)) return;
    setMutationKey(`${selectedTask.task_id}:block`);
    try {
      await requestBoardJson(`/api/work-board/tasks/${encodeURIComponent(selectedTask.task_id)}/actions`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action: "block",
          expected_revision: taskRevision(selectedTask),
          block_kind: "operator",
          reason,
        }),
      });
      announce(`Task ${selectedTask.task_id} was blocked with an operator reason.`);
      if (await refreshSelectedTask(selectedTask.task_id)) {
        setBlockReasonDraft("");
        setError(null);
      }
    } catch (caught) {
      const message = caught instanceof WorkBoardApiError
        ? caught.detail
        : "Task could not be blocked; the server state was retained.";
      setError(message);
      announce(message);
      await refreshSelectedTask(selectedTask.task_id).catch(() => {});
    } finally {
      setMutationKey(null);
    }
  }, [announce, blockReasonDraft, refreshSelectedTask, selectedTask]);

  const saveTask = useCallback(async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!selectedTask || !editDraft) return;
    setMutationKey(`${selectedTask.task_id}:edit`);
    try {
      const priority = Number(editDraft.priority);
      if (!Number.isInteger(priority) || priority < 0 || priority > 100) {
        throw new Error("Priority must be an integer from 0 to 100.");
      }
      await requestBoardJson(`/api/work-board/tasks/${encodeURIComponent(selectedTask.task_id)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: editDraft.title.trim(),
          body: editDraft.body,
          capability_id: editDraft.capabilityId.trim() || null,
          typed_input_ref: editDraft.typedInputRef.trim() || null,
          typed_input_digest: editDraft.typedInputDigest.trim() || null,
          executor_id: editDraft.executorId.trim() || null,
          assignee_id: editDraft.assigneeId.trim() || null,
          priority,
          scheduled_at: editDraft.scheduledAt || null,
          expected_revision: taskRevision(selectedTask),
        }),
      });
      announce(`Task ${selectedTask.task_id} updated.`);
      if (await refreshSelectedTask(selectedTask.task_id)) setError(null);
    } catch (caught) {
      const message = caught instanceof WorkBoardApiError || caught instanceof Error
        ? caught.message
        : "Task update failed; the server state was retained.";
      setError(message);
      announce(message);
      await refreshSelectedTask(selectedTask.task_id).catch(() => {});
    } finally {
      setMutationKey(null);
    }
  }, [announce, editDraft, refreshSelectedTask, selectedTask]);

  const addComment = useCallback(async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!selectedTask || !commentDraft.trim()) return;
    const body = commentDraft.trim();
    setMutationKey(`${selectedTask.task_id}:comment`);
    try {
      await requestBoardJson(`/api/work-board/tasks/${encodeURIComponent(selectedTask.task_id)}/comments`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ body, expected_revision: taskRevision(selectedTask) }),
      });
      setCommentDraft("");
      announce(`Comment added to ${selectedTask.task_id}.`);
      if (await refreshSelectedTask(selectedTask.task_id)) setError(null);
    } catch (caught) {
      const message = caught instanceof WorkBoardApiError
        ? caught.detail
        : "Comment could not be added; refresh and retry.";
      setError(message);
      announce(message);
      await refreshSelectedTask(selectedTask.task_id).catch(() => {});
    } finally {
      setMutationKey(null);
    }
  }, [announce, commentDraft, refreshSelectedTask, selectedTask]);

  const createTask = useCallback(async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setMutationKey("create");
    try {
      const goalRevision = Number(createDraft.goalRevision);
      const priority = Number(createDraft.priority);
      if (!createDraft.title.trim()) throw new Error("A bounded task title is required.");
      if (!createDraft.goalId.trim() || !Number.isInteger(goalRevision) || goalRevision < 1) {
        throw new Error("A current goal ID and positive goal revision are required.");
      }
      if (!Number.isInteger(priority) || priority < 0 || priority > 100) {
        throw new Error("Priority must be an integer from 0 to 100.");
      }
      if (!createExecutionLimits || !createLimitsAcknowledged) {
        throw new Error(createLimitsError ?? "Read and acknowledge the current goal execution limits before creating this task.");
      }
      const payload = await requestBoardJson("/api/work-board/tasks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: createDraft.title.trim(),
          body: createDraft.body,
          goal_id: createDraft.goalId.trim(),
          goal_revision: goalRevision,
          capability_id: createDraft.capabilityId.trim() || null,
          typed_input_ref: createDraft.typedInputRef.trim() || null,
          typed_input_digest: createDraft.typedInputDigest.trim() || null,
          executor_id: createDraft.executorId.trim() || null,
          assignee_id: createDraft.assigneeId.trim() || null,
          reviewer_id: createDraft.reviewerId.trim() || null,
          priority,
          idempotency_scope: createDraft.idempotencyScope.trim() || "operator-work-board",
          idempotency_key: createDraft.idempotencyKey.trim() || makeIdempotencyKey(),
          scheduled_at: createDraft.scheduledAt || null,
          requires_review: createDraft.requiresReview,
        }),
      });
      const created = detailFromPayload(payload);
      const parentTaskId = createDraft.parentTaskId.trim();
      if (parentTaskId && created?.task_id) {
        await requestBoardJson("/api/work-board/links", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            parent_task_id: parentTaskId,
            child_task_id: created.task_id,
            expected_child_revision: taskRevision(created),
          }),
        });
        announce(`Dependency ${parentTaskId} → ${created.task_id} added.`);
      }
      setCreateOpen(false);
      setCreateDraft(initialCreateDraft());
      announce(`Task ${created?.task_id ?? "created"} is now on the work board.`);
      const snapshotLoaded = await refreshSnapshot();
      const detailLoaded = created?.task_id ? await openTask(created.task_id) : true;
      if (snapshotLoaded && detailLoaded) setError(null);
    } catch (caught) {
      const message = caught instanceof WorkBoardApiError || caught instanceof Error
        ? caught.message
        : "Task could not be created; no board state was changed.";
      setError(message);
      announce(message);
    } finally {
      setMutationKey(null);
    }
  }, [announce, createDraft, createExecutionLimits, createLimitsAcknowledged, createLimitsError, openTask, refreshSnapshot]);

  const addLink = useCallback(async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const parentTaskId = linkDraft.parentTaskId.trim();
    const childTaskId = linkDraft.childTaskId.trim();
    const expectedChildRevision = Number(linkDraft.expectedChildRevision);
    if (!parentTaskId || !childTaskId || !Number.isInteger(expectedChildRevision) || expectedChildRevision < 1) {
      announce("A parent ID, child ID, and current child revision are required to add a dependency.");
      return;
    }
    setMutationKey("link:add");
    try {
      await requestBoardJson("/api/work-board/links", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ parent_task_id: parentTaskId, child_task_id: childTaskId, expected_child_revision: expectedChildRevision }),
      });
      announce(`Dependency ${parentTaskId} → ${childTaskId} added.`);
      setLinkDraft({ parentTaskId: selectedTask?.task_id ?? parentTaskId, childTaskId: "", expectedChildRevision: "" });
      if (await refreshSelectedTask(selectedTask?.task_id ?? null)) setError(null);
    } catch (caught) {
      const message = caught instanceof WorkBoardApiError ? caught.detail : "Dependency could not be added; no link was projected.";
      setError(message);
      announce(message);
      await refreshSelectedTask(selectedTask?.task_id ?? null).catch(() => {});
    } finally {
      setMutationKey(null);
    }
  }, [announce, linkDraft, refreshSelectedTask, selectedTask?.task_id]);

  const removeLink = useCallback(async (link: WorkBoardLink) => {
    setMutationKey(`link:remove:${link.parent_task_id}:${link.child_task_id}`);
    try {
      const childPayload = await requestBoardJson(`/api/work-board/tasks/${encodeURIComponent(link.child_task_id)}`);
      const child = detailFromPayload(childPayload);
      if (!child) throw new Error("The child task revision could not be read.");
      await requestBoardJson("/api/work-board/links", {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          parent_task_id: link.parent_task_id,
          child_task_id: link.child_task_id,
          expected_child_revision: taskRevision(child),
        }),
      });
      announce(`Dependency ${link.parent_task_id} → ${link.child_task_id} removed.`);
      if (await refreshSelectedTask(selectedTask?.task_id ?? null)) setError(null);
    } catch (caught) {
      const message = caught instanceof WorkBoardApiError ? caught.detail : "Dependency could not be removed; refresh and retry.";
      setError(message);
      announce(message);
      await refreshSelectedTask(selectedTask?.task_id ?? null).catch(() => {});
    } finally {
      setMutationKey(null);
    }
  }, [announce, refreshSelectedTask, selectedTask?.task_id]);

  const visibleTasks = useMemo(() => {
    if (showArchived || statusFilter === "archived") return tasks;
    return tasks.filter((task) => task.status !== "archived");
  }, [showArchived, statusFilter, tasks]);

  const columnStatuses = useMemo<WorkBoardTaskStatus[]>(
    () => showArchived || statusFilter === "archived"
      ? [...WORK_BOARD_STATUSES, "archived"]
      : WORK_BOARD_STATUSES,
    [showArchived, statusFilter],
  );

  const tasksByStatus = useMemo(() => {
    const grouped = new Map<WorkBoardTaskStatus, WorkBoardTask[]>();
    columnStatuses.forEach((status) => grouped.set(status, []));
    visibleTasks.forEach((task) => {
      const list = grouped.get(task.status);
      if (list) list.push(task);
    });
    return grouped;
  }, [columnStatuses, visibleTasks]);

  const handleDragStart = useCallback((event: DragEvent<HTMLElement>, task: WorkBoardTask) => {
    setDraggedTaskId(task.task_id);
    if (event.dataTransfer) {
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", task.task_id);
    }
  }, []);

  const handleDrop = useCallback((event: DragEvent<HTMLElement>, target: WorkBoardTaskStatus) => {
    event.preventDefault();
    setDropTarget(null);
    const taskId = event.dataTransfer?.getData("text/plain") || draggedTaskId;
    const task = tasks.find((item) => item.task_id === taskId);
    setDraggedTaskId(null);
    if (task) void transitionTask(task, target);
  }, [draggedTaskId, tasks, transitionTask]);

  const renderAttempt = (attempt: WorkBoardReadbackAttempt) => (
    <li key={attempt.attempt_id} className="work-board-attempt">
      <span>{attempt.attempt_id.slice(0, 8)}</span>
      <span>{attempt.outcome ?? "pending"}</span>
      <span>{attempt.workflow_run_id ? `run ${attempt.workflow_run_id.slice(0, 8)}` : "admission pending"}</span>
      <span>readback {evidenceStatusFromAttempt(attempt) ?? "pending"}</span>
      {attempt.readback_reason ? <span>{attempt.readback_reason}</span> : null}
      {attempt.readback_refs?.length ? <ReferenceList label="Attempt readback references" references={attempt.readback_refs} /> : null}
      {attempt.artifact_refs?.length ? <ReferenceList label="Attempt artifact references" references={attempt.artifact_refs} /> : null}
      {attempt.receipt_refs?.length ? <ReferenceList label="Attempt receipt references" references={attempt.receipt_refs} /> : null}
    </li>
  );

  return (
    <section className="work-board" aria-label="Operator work board">
      <div className="work-board-toolbar">
        <div className="work-board-toolbar-main">
          <label className="work-board-search">
            <span>Search</span>
            <input
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="task title or ID"
              aria-label="Search work board tasks"
            />
          </label>
          <label className="work-board-filter">
            <span>Status</span>
            <select
              value={statusFilter}
              onChange={(event) => setStatusFilter(event.target.value as WorkBoardTaskStatus | "all")}
              aria-label="Filter work board tasks by status"
            >
              <option value="all">All active</option>
              {WORK_BOARD_STATUSES.map((status) => <option key={status} value={status}>{STATUS_LABELS[status]}</option>)}
              <option value="archived">Archived</option>
            </select>
          </label>
          <label className="work-board-filter">
            <span>Assignee</span>
            <input
              value={assigneeFilter}
              onChange={(event) => setAssigneeFilter(event.target.value)}
              placeholder="any assignee"
              aria-label="Filter work board tasks by assignee"
            />
          </label>
        </div>
        <div className="work-board-toolbar-actions">
          <span className={`work-board-socket work-board-socket--${socketState}`} role="status">
            {socketState === "connected" ? "live" : socketState}
            {lastEventId != null ? ` · #${lastEventId}` : ""}
          </span>
          <button type="button" className="cockpit-feedback-button" onClick={() => void refreshSnapshot()} disabled={loading}>
            Refresh
          </button>
          <button
            type="button"
            className="cockpit-feedback-button cockpit-feedback-button--primary"
            onClick={(event) => {
              dialogTriggerRef.current = event.currentTarget;
              setCreateOpen(true);
            }}
          >
            Create task
          </button>
        </div>
      </div>

      <div className="work-board-status-row" aria-live="polite">
        <span>{loading ? "Refreshing canonical task state…" : `${visibleTasks.length} task${visibleTasks.length === 1 ? "" : "s"}`}</span>
        <span>{error ?? announcement}</span>
        <label className="work-board-archive-toggle">
          <input type="checkbox" checked={showArchived || statusFilter === "archived"} disabled={statusFilter === "archived"} onChange={(event) => setShowArchived(event.target.checked)} />
          Include archived
        </label>
      </div>

      <div className="work-board-columns">
        {columnStatuses.map((status) => {
          const columnTasks = tasksByStatus.get(status) ?? [];
          return (
            <section
              key={status}
              className={`work-board-column ${dropTarget === status ? "work-board-column--drop-target" : ""}`}
              aria-label={`${STATUS_LABELS[status]} tasks`}
              onDragOver={(event) => {
                event.preventDefault();
                setDropTarget(status);
              }}
              onDragLeave={() => setDropTarget((current) => current === status ? null : current)}
              onDrop={(event) => handleDrop(event, status)}
            >
              <div className="work-board-column-header">
                <h3>{STATUS_LABELS[status]}</h3>
                <span>{columnTasks.length}</span>
              </div>
              <div className="work-board-column-body">
                {columnTasks.map((task) => (
                  <article
                    key={task.task_id}
                    className={`work-board-card ${draggedTaskId === task.task_id ? "work-board-card--dragging" : ""}`}
                    draggable
                    onDragStart={(event) => handleDragStart(event, task)}
                    onDragEnd={() => {
                      setDraggedTaskId(null);
                      setDropTarget(null);
                    }}
                  >
                    <button
                      type="button"
                      className="work-board-card-button"
                      onClick={(event) => void openTask(task.task_id, true, event.currentTarget)}
                      aria-label={`Open task ${task.task_id}: ${task.title}`}
                    >
                      <span className="work-board-card-topline">
                        <strong>{task.title}</strong>
                        <span className="work-board-card-priority">P{task.priority}</span>
                      </span>
                      <span className="work-board-card-id">{task.task_id}</span>
                      <span className="work-board-card-meta">
                        assignee {task.assignee_id ?? "unassigned"} · executor {task.executor_id ?? "unassigned"} · goal {task.goal_id} r{task.goal_revision}
                      </span>
                      <span className="work-board-card-meta">
                        deps {task.completed_dependency_count ?? 0}/{task.dependency_count ?? 0}
                        {task.latest_attempt ? ` · attempt ${task.latest_attempt.outcome ?? "active"}` : " · no attempt"}
                        {task.status === "ready" && serverSelectionRank(task) != null ? ` · rank ${serverSelectionRank(task)}` : ""}
                        {` · ${formatAge(task.created_at)}`}
                      </span>
                      {task.status === "blocked" && (
                        <span className="work-board-card-state work-board-card-state--blocked">
                          {task.block_kind ?? "blocked"}: {task.block_reason ?? "recovery required"}
                        </span>
                      )}
                      {task.status === "review" && (
                        <span className="work-board-card-state work-board-card-state--review">
                          reviewer {task.reviewer_id ?? "named reviewer pending"} · evidence {evidenceStatus(task)}
                        </span>
                      )}
                      {task.status === "running" && (
                        <span className="work-board-card-state">
                          {task.latest_attempt?.lease_owner ?? "lease active"} · {formatAge(task.latest_attempt?.started_at)}
                        </span>
                      )}
                    </button>
                  </article>
                ))}
                {columnTasks.length === 0 && <div className="work-board-column-empty">Drop a legal transition here.</div>}
              </div>
            </section>
          );
        })}
      </div>

      {nextAfter && (
        <div className="work-board-pagination">
          <button
            type="button"
            className="cockpit-feedback-button"
            onClick={() => void loadMore()}
            disabled={loading || loadingMore}
          >
            {loadingMore ? "Loading more tasks…" : "Load more tasks"}
          </button>
        </div>
      )}

      <div className="work-board-live-region" aria-live="assertive" aria-atomic="true">{announcement}</div>

      {createOpen && (
        <div className="work-board-overlay" role="presentation">
          <div
            ref={createDialogRef}
            className="work-board-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="work-board-create-title"
            tabIndex={-1}
          >
            <div className="work-board-dialog-header">
              <h2 id="work-board-create-title">Create bounded task</h2>
              <button type="button" className="cockpit-window-control" onClick={() => setCreateOpen(false)} aria-label="Close create task dialog">x</button>
            </div>
            <form onSubmit={createTask} className="work-board-form">
              <label>Title<input data-dialog-autofocus required maxLength={MAX_TITLE_LENGTH} value={createDraft.title} onChange={(event) => setCreateDraft((draft) => ({ ...draft, title: event.target.value }))} /></label>
              <label>Body<textarea maxLength={MAX_BODY_LENGTH} value={createDraft.body} onChange={(event) => setCreateDraft((draft) => ({ ...draft, body: event.target.value }))} /></label>
              <div className="work-board-form-grid">
                <label>Goal ID<input required value={createDraft.goalId} onChange={(event) => setCreateDraft((draft) => ({ ...draft, goalId: event.target.value }))} /></label>
                <label>Goal revision<input required type="number" min="1" step="1" value={createDraft.goalRevision} onChange={(event) => setCreateDraft((draft) => ({ ...draft, goalRevision: event.target.value }))} /></label>
                <label>Dependency parent task ID<input value={createDraft.parentTaskId} onChange={(event) => setCreateDraft((draft) => ({ ...draft, parentTaskId: event.target.value }))} placeholder="optional existing task" /></label>
                <label>Capability ID<input value={createDraft.capabilityId} onChange={(event) => setCreateDraft((draft) => ({ ...draft, capabilityId: event.target.value }))} placeholder="triage allowed" /></label>
                <label>Executor ID<input value={createDraft.executorId} onChange={(event) => setCreateDraft((draft) => ({ ...draft, executorId: event.target.value }))} placeholder="unassigned allowed" /></label>
                <label>Reviewer ID<input value={createDraft.reviewerId} onChange={(event) => setCreateDraft((draft) => ({ ...draft, reviewerId: event.target.value }))} placeholder="required for review" /></label>
                <label>Assignee ID<input value={createDraft.assigneeId} onChange={(event) => setCreateDraft((draft) => ({ ...draft, assigneeId: event.target.value }))} placeholder="operator or executor" /></label>
                <label>Typed input ref<input value={createDraft.typedInputRef} onChange={(event) => setCreateDraft((draft) => ({ ...draft, typedInputRef: event.target.value }))} /></label>
                <label>Input SHA-256<input value={createDraft.typedInputDigest} onChange={(event) => setCreateDraft((draft) => ({ ...draft, typedInputDigest: event.target.value }))} /></label>
                <label>Priority<input required type="number" min="0" max="100" step="1" value={createDraft.priority} onChange={(event) => setCreateDraft((draft) => ({ ...draft, priority: event.target.value }))} /></label>
                <label>Scheduled at<input type="datetime-local" value={createDraft.scheduledAt} onChange={(event) => setCreateDraft((draft) => ({ ...draft, scheduledAt: event.target.value }))} /></label>
              </div>
              {createLimitsLoading ? <p className="work-board-form-note">Reading the current goal execution limits…</p> : null}
              {createLimitsError ? <p className="work-board-form-note work-board-form-note--error">{createLimitsError}</p> : null}
              {createExecutionLimits ? (
                <div className="work-board-form-note">
                  <div>Effective runtime limit: {createExecutionLimits.effectiveMaxRuntimeSeconds}s (default {createExecutionLimits.defaultMaxRuntimeSeconds}s, hard cap {createExecutionLimits.hardMaxRuntimeSeconds}s).</div>
                  <div>Attempt limit: {createExecutionLimits.attemptLimit}; source: {createExecutionLimits.limitSource}.</div>
                  <label className="work-board-checkbox"><input type="checkbox" checked={createLimitsAcknowledged} onChange={(event) => setCreateLimitsAcknowledged(event.target.checked)} /> I acknowledge these current goal execution limits.</label>
                </div>
              ) : null}
              <label className="work-board-checkbox"><input type="checkbox" checked={createDraft.requiresReview} onChange={(event) => setCreateDraft((draft) => ({ ...draft, requiresReview: event.target.checked }))} /> Require named review</label>
              <div className="work-board-form-actions">
                <button type="button" className="cockpit-feedback-button" onClick={() => setCreateOpen(false)}>Cancel</button>
                <button type="submit" className="cockpit-feedback-button cockpit-feedback-button--primary" disabled={mutationKey === "create" || createLimitsLoading || !createExecutionLimits || !createLimitsAcknowledged}>Create</button>
              </div>
            </form>
          </div>
        </div>
      )}

      {selectedTask && (
        <div className="work-board-overlay" role="presentation">
          <aside
            ref={detailDialogRef}
            className="work-board-drawer"
            role="dialog"
            aria-modal="true"
            aria-labelledby="work-board-detail-title"
            tabIndex={-1}
          >
            <div className="work-board-dialog-header">
              <div>
                <h2 id="work-board-detail-title">{selectedTask.title}</h2>
                <span className="work-board-card-id">{selectedTask.task_id} · {STATUS_LABELS[selectedTask.status]}</span>
              </div>
              <button type="button" className="cockpit-window-control" onClick={() => setSelectedTask(null)} aria-label="Close task detail">x</button>
            </div>
            {selectedLoading ? <div className="work-board-loading">Refreshing task detail…</div> : null}
            <div className="work-board-detail-scroll">
              <section className="work-board-detail-summary">
                <span>goal {selectedTask.goal_id} revision {selectedTask.goal_revision}</span>
                <span>priority {selectedTask.priority}</span>
                <span>assignee {selectedTask.assignee_id ?? "unassigned"}</span>
                <span>executor {selectedTask.executor_id ?? "unassigned"}</span>
                {detailLimitsLoading ? <span>execution limits: reading…</span> : null}
                {detailLimitsError ? <span className="work-board-card-state work-board-card-state--blocked">{detailLimitsError}</span> : null}
                {detailExecutionLimits ? (
                  <div className="work-board-form-note">
                    runtime {detailExecutionLimits.effectiveMaxRuntimeSeconds}s (default {detailExecutionLimits.defaultMaxRuntimeSeconds}s, hard {detailExecutionLimits.hardMaxRuntimeSeconds}s) · attempts {detailExecutionLimits.attemptLimit} · source {detailExecutionLimits.limitSource}
                  </div>
                ) : null}
                {selectedTask.status === "review" ? <span>review evidence: {evidenceStatus(selectedTask)}</span> : null}
                {selectedTask.block_kind ? <span className="work-board-card-state work-board-card-state--blocked">{selectedTask.block_kind}: {selectedTask.block_reason ?? "recovery required"}</span> : null}
                {selectedTask.recovery_action ? <span>recovery: {selectedTask.recovery_action}</span> : null}
              </section>

              {editDraft && (
                <form className="work-board-form" onSubmit={saveTask}>
                  <label>Title<input data-dialog-autofocus required maxLength={MAX_TITLE_LENGTH} value={editDraft.title} onChange={(event) => setEditDraft((draft) => draft ? { ...draft, title: event.target.value } : draft)} /></label>
                  <label>Body<textarea maxLength={MAX_BODY_LENGTH} value={editDraft.body} onChange={(event) => setEditDraft((draft) => draft ? { ...draft, body: event.target.value } : draft)} /></label>
                  <div className="work-board-form-grid">
                    <label>Capability ID<input value={editDraft.capabilityId} onChange={(event) => setEditDraft((draft) => draft ? { ...draft, capabilityId: event.target.value } : draft)} /></label>
                    <label>Executor ID<input value={editDraft.executorId} onChange={(event) => setEditDraft((draft) => draft ? { ...draft, executorId: event.target.value } : draft)} /></label>
                    <label>Assignee ID<input value={editDraft.assigneeId} onChange={(event) => setEditDraft((draft) => draft ? { ...draft, assigneeId: event.target.value } : draft)} /></label>
                    <label>Typed input ref<input value={editDraft.typedInputRef} onChange={(event) => setEditDraft((draft) => draft ? { ...draft, typedInputRef: event.target.value } : draft)} /></label>
                    <label>Input SHA-256<input value={editDraft.typedInputDigest} onChange={(event) => setEditDraft((draft) => draft ? { ...draft, typedInputDigest: event.target.value } : draft)} /></label>
                    <label>Priority<input required type="number" min="0" max="100" step="1" value={editDraft.priority} onChange={(event) => setEditDraft((draft) => draft ? { ...draft, priority: event.target.value } : draft)} /></label>
                    <label>Scheduled at<input type="datetime-local" value={editDraft.scheduledAt} onChange={(event) => setEditDraft((draft) => draft ? { ...draft, scheduledAt: event.target.value } : draft)} /></label>
                  </div>
                  <button type="submit" className="cockpit-feedback-button cockpit-feedback-button--primary" disabled={mutationKey === `${selectedTask.task_id}:edit`}>Save bounded fields</button>
                </form>
              )}

              {selectedRecoveryAction === "unblock" && (
                <div className="work-board-recovery-form">
                  <label htmlFor="work-board-unblock-resolution">Resolution statement</label>
                  <textarea
                    id="work-board-unblock-resolution"
                    required
                    maxLength={1000}
                    value={unblockResolutionDraft}
                    onChange={(event) => setUnblockResolutionDraft(event.target.value)}
                    placeholder="Describe the bounded operator resolution"
                  />
                  <button
                    type="button"
                    className="cockpit-feedback-button"
                    disabled={mutationKey != null || !unblockResolutionDraft.trim()}
                    onClick={() => void unblockTask(selectedTask)}
                  >
                    Unblock
                  </button>
                </div>
              )}

              <div className="work-board-detail-actions">
                {selectedTask.status === "triage" && isTargetLegal(selectedTask, "todo") && (
                  <button
                    type="button"
                    className="cockpit-feedback-button"
                    disabled={mutationKey != null || !detailExecutionLimits || !detailLimitsAcknowledged}
                    title={!detailExecutionLimits || !detailLimitsAcknowledged ? "Read and acknowledge the current goal execution limits before promotion." : undefined}
                    onClick={() => void transitionTask(selectedTask, "todo")}
                  >
                    Move to Todo
                  </button>
                )}
                {selectedTask.status === "blocked" && selectedTask.recovery_action === "retry" && (
                  <button
                    type="button"
                    className="cockpit-feedback-button"
                    disabled={mutationKey != null}
                    onClick={() => void runAction(selectedTask, "retry")}
                  >
                    Retry with fresh attempt
                  </button>
                )}
                {selectedTask.status === "running" && selectedTask.recovery_action === "cancel" && (
                  <button
                    type="button"
                    className="cockpit-feedback-button"
                    disabled={mutationKey != null}
                    onClick={() => {
                      if (!window.confirm(`Cancel durable execution for task ${selectedTask.task_id}?`)) return;
                      void runAction(selectedTask, "cancel");
                    }}
                  >
                    Cancel durable job
                  </button>
                )}
                {selectedTask.status === "done" && <button type="button" className="cockpit-feedback-button" disabled={mutationKey != null} onClick={() => void transitionTask(selectedTask, "archived")}>Archive</button>}
              </div>

              {detailExecutionLimits ? (
                <label className="work-board-checkbox"><input type="checkbox" checked={detailLimitsAcknowledged} onChange={(event) => setDetailLimitsAcknowledged(event.target.checked)} /> I acknowledge the current goal execution limits for this task.</label>
              ) : null}

              {["triage", "todo", "ready", "review"].includes(selectedTask.status) && (
                <form className="work-board-block-form" onSubmit={blockTask}>
                  <label>Block reason<textarea required maxLength={1000} value={blockReasonDraft} onChange={(event) => setBlockReasonDraft(event.target.value)} placeholder="Bounded operator reason" /></label>
                  <button type="submit" className="cockpit-feedback-button" disabled={mutationKey === `${selectedTask.task_id}:block` || !blockReasonDraft.trim()}>Block task</button>
                </form>
              )}

              <section className="work-board-detail-section">
                <h3>Dependencies</h3>
                <div>Parents: {selectedTask.parents.length ? selectedTask.parents.map((link) => link.parent_task_id).join(", ") : "none"}</div>
                <div>Children: {selectedTask.children.length ? selectedTask.children.map((link) => link.child_task_id).join(", ") : "none"}</div>
                <form className="work-board-link-form" onSubmit={addLink}>
                  <label>Parent task ID<input value={linkDraft.parentTaskId} onChange={(event) => setLinkDraft((draft) => ({ ...draft, parentTaskId: event.target.value }))} /></label>
                  <label>Child task ID<input value={linkDraft.childTaskId} onChange={(event) => setLinkDraft((draft) => ({ ...draft, childTaskId: event.target.value }))} placeholder="task ID" /></label>
                  <label>Child revision<input type="number" min="1" step="1" value={linkDraft.expectedChildRevision} onChange={(event) => setLinkDraft((draft) => ({ ...draft, expectedChildRevision: event.target.value }))} /></label>
                  <button type="submit" className="cockpit-feedback-button" disabled={mutationKey === "link:add"}>Add dependency</button>
                </form>
                {[...selectedTask.parents, ...selectedTask.children].map((link) => (
                  <div key={`${link.parent_task_id}:${link.child_task_id}`} className="work-board-link-row">
                    <span>{link.parent_task_id} → {link.child_task_id}</span>
                    <button type="button" className="cockpit-feedback-button" disabled={mutationKey != null} onClick={() => void removeLink(link)} aria-label={`Remove dependency ${link.parent_task_id} to ${link.child_task_id}`}>Remove</button>
                  </div>
                ))}
              </section>
              <section className="work-board-detail-section">
                <h3>Attempts and readback</h3>
                <div>Readback status: {evidenceStatus(selectedTask)}</div>
                {selectedTask.readback_reason ? <div>{selectedTask.readback_reason}</div> : null}
                <ul className="work-board-attempts">{selectedTask.attempts.length ? selectedTask.attempts.map(renderAttempt) : <li>No attempts yet.</li>}</ul>
              </section>
              <section className="work-board-detail-section">
                <h3>Comments</h3>
                <div className="work-board-comments">
                  {selectedTask.comments.length ? selectedTask.comments.map((comment: WorkBoardComment) => <div className="work-board-comment" key={comment.comment_id}><strong>{comment.author_principal_id ?? "operator"}</strong><span>{comment.body}</span><small>{formatAge(comment.created_at)}</small></div>) : <div>No comments yet.</div>}
                </div>
                <form className="work-board-comment-form" onSubmit={addComment}>
                  <label className="sr-only" htmlFor="work-board-comment">Add comment</label>
                  <textarea id="work-board-comment" value={commentDraft} maxLength={2000} onChange={(event) => setCommentDraft(event.target.value)} placeholder="Bounded handoff or recovery note" />
                  <button type="submit" className="cockpit-feedback-button" disabled={!commentDraft.trim() || mutationKey === `${selectedTask.task_id}:comment`}>Comment</button>
                </form>
              </section>
              <section className="work-board-detail-section">
                <h3>Safe event timeline</h3>
                <ul className="work-board-events">
                  {selectedTask.events.length ? selectedTask.events.map((event: WorkBoardEvent) => <li key={event.event_id}><span>#{event.event_id}</span><strong>{event.kind}</strong><small>{formatAge(event.created_at)}</small></li>) : <li>No events returned.</li>}
                </ul>
              </section>
              <section className="work-board-detail-section">
                <h3>Artifacts and result references</h3>
                <ReferenceList label="Artifact references" references={selectedTask.artifact_refs} />
                <ReferenceList
                  label="Readback/result references"
                  references={[...(selectedTask.readback_refs ?? []), ...(selectedTask.result_refs ?? [])]}
                />
              </section>
            </div>
          </aside>
        </div>
      )}
    </section>
  );
}

export { STATUS_LABELS, DRAG_TARGETS, isTargetLegal, tasksFromPayload };
