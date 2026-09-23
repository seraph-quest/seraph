import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent } from "react";

import { API_URL, WS_URL } from "../../config/constants";
import { resolveWebSocketUrl } from "../../hooks/useWebSocket";
import { apiFetch } from "../../lib/api";
import type {
  GoalInfo,
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
  reconcile_admission_binding: "Reconcile the pending job admission",
  reconcile_external_effect: "Reconcile the external effect before retrying",
};

const TASK_LIMIT = 100;
const EVENT_LIMIT = 100;
const MAX_SYNC_PAGES = 20;
const RECONNECT_DELAY_MS = 3_000;

export interface WorkBoardPanelProps {
  onOpenApprovals?: () => void;
  onInspectWorkflowRun?: (workflowRunId: string) => void;
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
  const payload = await response.json().catch(() => null);
  if (!response.ok) throw responseError(payload, response.status);
  return payload as T;
}

async function apiRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await apiFetch(`${API_URL}${path}`, init);
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

function receiptTitle(reference: WorkBoardReceiptReference): string {
  return reference.artifact_type || reference.effect_type || reference.status || "Execution receipt";
}

function safeReferenceLabel(reference: WorkBoardReceiptReference): string {
  return reference.artifact_id
    || reference.workflow_run_id
    || reference.job_id
    || reference.effect_id
    || reference.file_path
    || reference.target_path
    || reference.reason_code
    || "Safe reference";
}

function WorkBoardPanel({ onOpenApprovals, onInspectWorkflowRun }: WorkBoardPanelProps) {
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
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busyAction, setBusyAction] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  const [createBusy, setCreateBusy] = useState(false);
  const [createLimit, setCreateLimit] = useState<WorkBoardExecutionLimits | null>(null);
  const [createLimitError, setCreateLimitError] = useState<string | null>(null);
  const [createLimitAcknowledged, setCreateLimitAcknowledged] = useState(false);
  const [createDraft, setCreateDraft] = useState<CreateDraft>({
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
  });
  const [editMode, setEditMode] = useState(false);
  const [editDraft, setEditDraft] = useState<EditDraft | null>(null);
  const [detailLimit, setDetailLimit] = useState<WorkBoardExecutionLimits | null>(null);
  const [detailLimitError, setDetailLimitError] = useState<string | null>(null);
  const [detailLimitAcknowledged, setDetailLimitAcknowledged] = useState(false);
  const [blockReason, setBlockReason] = useState("");
  const [blockConfirmed, setBlockConfirmed] = useState(false);
  const [unblockResolution, setUnblockResolution] = useState("");
  const [commentDraft, setCommentDraft] = useState("");
  const [parentTaskIdDraft, setParentTaskIdDraft] = useState("");
  const [childTaskIdDraft, setChildTaskIdDraft] = useState("");

  const tasksRef = useRef(tasks);
  const eventCursorRef = useRef<number | null>(null);
  const selectedTaskIdRef = useRef(selectedTaskId);
  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<number | null>(null);
  const stoppedRef = useRef(false);
  const syncingRef = useRef(false);
  const socketGenerationRef = useRef(0);
  const reconnectRef = useRef<(() => Promise<void>) | null>(null);

  useEffect(() => {
    tasksRef.current = tasks;
  }, [tasks]);

  useEffect(() => {
    selectedTaskIdRef.current = selectedTaskId;
  }, [selectedTaskId]);

  const allGoals = useMemo(() => flattenGoals(goals), [goals]);
  const selectedTask = detail?.task ?? tasks.find((task) => task.task_id === selectedTaskId) ?? null;
  const taskById = useMemo(() => new Map(tasks.map((task) => [task.task_id, task])), [tasks]);

  const loadAllTaskPages = useCallback(async (): Promise<BoardSnapshot> => {
    const loaded: WorkBoardTask[] = [];
    let after: number | null = null;
    let firstEventCursor: number | null = null;
    const cursors = new Set<number>();
    for (let pageNumber = 0; pageNumber < MAX_SYNC_PAGES; pageNumber += 1) {
      const query = new URLSearchParams({ limit: String(TASK_LIMIT) });
      if (after !== null) query.set("after", String(after));
      const page = await boardRequest<WorkBoardTaskPage>(`/tasks?${query.toString()}`);
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
  }, []);

  const loadEventDelta = useCallback(async (startingCursor: number): Promise<EventDelta> => {
    let after = startingCursor;
    const events: WorkBoardEvent[] = [];
    const cursors = new Set<number>();
    for (let pageNumber = 0; pageNumber < MAX_SYNC_PAGES; pageNumber += 1) {
      const query = new URLSearchParams({ after: String(after), limit: String(EVENT_LIMIT) });
      const page = await boardRequest<WorkBoardEventPage>(`/events?${query.toString()}`);
      if (!page || !Array.isArray(page.events) || !asSafeInteger(page.last_event_id) || typeof page.gap !== "boolean") {
        throw new WorkBoardSyncError("The event cursor response is invalid. The board is stale until a fresh snapshot succeeds.");
      }
      if (page.gap) throw new WorkBoardSyncError("The event cursor is too old. The board is refreshing from a fresh snapshot.");
      events.push(...page.events);
      if (page.events.length === 0 || page.events.length < EVENT_LIMIT || page.last_event_id === after) {
        return { events, eventCursor: page.last_event_id };
      }
      if (page.last_event_id < after || cursors.has(page.last_event_id)) {
        throw new WorkBoardSyncError("The event stream returned a non-advancing cursor. The board is stale.");
      }
      cursors.add(page.last_event_id);
      // Use the cursor returned by this response exactly; do not derive it.
      after = page.last_event_id;
    }
    throw new WorkBoardSyncError("The event backlog exceeded the bounded catch-up window. The board is refreshing.");
  }, []);

  const fetchTaskDetail = useCallback(async (taskId: string): Promise<WorkBoardTaskDetail> => {
    return boardRequest<WorkBoardTaskDetail>(`/tasks/${encodeURIComponent(taskId)}`);
  }, []);

  const reloadOneTask = useCallback(async (taskId: string) => {
    try {
      const nextDetail = await fetchTaskDetail(taskId);
      setTasks((current) => uniqueTasks([
        ...current.filter((task) => task.task_id !== nextDetail.task.task_id),
        nextDetail.task,
      ]));
      if (selectedTaskIdRef.current === taskId) setDetail(nextDetail);
    } catch (error) {
      if (error instanceof WorkBoardApiError && error.status === 404) {
        void reconnectRef.current?.();
      } else {
        setStale(true);
        setBoardError(errorText(error));
      }
    }
  }, [fetchTaskDetail]);

  const loadSnapshotAndCatchUp = useCallback(async (): Promise<number> => {
    setLoading((current) => current && tasksRef.current.length === 0);
    const snapshot = await loadAllTaskPages();
    setTasks(snapshot.tasks);
    setStale(false);
    setBoardError(null);
    const delta = await loadEventDelta(snapshot.eventCursor);
    eventCursorRef.current = delta.eventCursor;
    const taskIds = Array.from(new Set(delta.events.map((event) => event.task_id).filter(Boolean)));
    if (taskIds.length) {
      const refreshed = await Promise.all(taskIds.map((taskId) => fetchTaskDetail(taskId).catch(() => null)));
      const changed = refreshed.filter((item): item is WorkBoardTaskDetail => item !== null).map((item) => item.task);
      if (changed.length) {
        setTasks((current) => uniqueTasks([
          ...current.filter((task) => !changed.some((item) => item.task_id === task.task_id)),
          ...changed,
        ]));
        const selected = refreshed.find((item) => item?.task.task_id === selectedTaskIdRef.current);
        if (selected) setDetail(selected);
      }
    }
    setLoading(false);
    setStale(false);
    return delta.eventCursor;
  }, [fetchTaskDetail, loadAllTaskPages, loadEventDelta]);

  const refreshSnapshot = useCallback(async () => {
    setBoardError(null);
    setLoading((current) => current && tasksRef.current.length === 0);
    try {
      await loadSnapshotAndCatchUp();
      setAnnouncement("Work board refreshed from the authenticated server snapshot.");
    } catch (error) {
      setStale(true);
      setLoading(false);
      setBoardError(errorText(error));
    }
  }, [loadSnapshotAndCatchUp]);

  const refreshSelectedTask = useCallback(async () => {
    if (!selectedTaskId) return;
    setDetailLoading(true);
    setDetailError(null);
    try {
      const nextDetail = await fetchTaskDetail(selectedTaskId);
      setDetail(nextDetail);
      setTasks((current) => uniqueTasks([
        ...current.filter((task) => task.task_id !== selectedTaskId),
        nextDetail.task,
      ]));
    } catch (error) {
      setDetailError(errorText(error));
      if (error instanceof WorkBoardApiError && error.status === 409) void refreshSnapshot();
    } finally {
      setDetailLoading(false);
    }
  }, [fetchTaskDetail, refreshSnapshot, selectedTaskId]);

  const openSocketAt = useCallback((cursor: number) => {
    if (stoppedRef.current) return;
    socketRef.current?.close(1000, "reconnecting");
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
      if (generation === socketGenerationRef.current) setConnectionState("connected");
    };
    socket.onmessage = (message) => {
      if (generation !== socketGenerationRef.current) return;
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
      const currentCursor = eventCursorRef.current ?? cursor;
      if (eventPayload.event_id <= currentCursor) return;
      eventCursorRef.current = eventPayload.event_id;
      void reloadOneTask(eventPayload.task_id);
    };
    socket.onclose = (event) => {
      if (generation !== socketGenerationRef.current || stoppedRef.current) return;
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
      if (generation === socketGenerationRef.current) setConnectionState("disconnected");
    };
  }, [reloadOneTask]);

  const reconnectFromSnapshot = useCallback(async () => {
    if (stoppedRef.current || syncingRef.current) return;
    syncingRef.current = true;
    if (reconnectTimerRef.current !== null) {
      window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
    }
    setConnectionState("connecting");
    try {
      const cursor = await loadSnapshotAndCatchUp();
      if (!stoppedRef.current) openSocketAt(cursor);
    } catch (error) {
      if (!stoppedRef.current) {
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
    } finally {
      syncingRef.current = false;
    }
  }, [loadSnapshotAndCatchUp, openSocketAt]);

  useEffect(() => {
    reconnectRef.current = reconnectFromSnapshot;
  }, [reconnectFromSnapshot]);

  useEffect(() => {
    stoppedRef.current = false;
    void reconnectFromSnapshot();
    void apiRequest<GoalInfo[]>("/api/goals/tree")
      .then((payload) => {
        if (!Array.isArray(payload)) throw new Error("Goals response was not a list");
        setGoals(payload);
        setGoalError(null);
      })
      .catch((error) => setGoalError(errorText(error)));
    return () => {
      stoppedRef.current = true;
      socketGenerationRef.current += 1;
      if (reconnectTimerRef.current !== null) window.clearTimeout(reconnectTimerRef.current);
      reconnectTimerRef.current = null;
      socketRef.current?.close(1000, "component_unmounted");
      socketRef.current = null;
    };
  }, [reconnectFromSnapshot]);

  useEffect(() => {
    if (!selectedTaskId) {
      setDetail(null);
      setDetailError(null);
      setEditMode(false);
      setEditDraft(null);
      setDetailLimit(null);
      return;
    }
    setDetailLoading(true);
    setDetailError(null);
    void fetchTaskDetail(selectedTaskId)
      .then((nextDetail) => {
        setDetail(nextDetail);
        setTasks((current) => uniqueTasks([
          ...current.filter((task) => task.task_id !== selectedTaskId),
          nextDetail.task,
        ]));
      })
      .catch((error) => setDetailError(errorText(error)))
      .finally(() => setDetailLoading(false));
  }, [fetchTaskDetail, selectedTaskId]);

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
    void boardRequest<WorkBoardExecutionLimits>(
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
  }, [createDraft.goalId, createDraft.goalRevision, createOpen]);

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
    void boardRequest<WorkBoardExecutionLimits>(
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
  }, [selectedTask?.goal_id, selectedTask?.goal_revision, selectedTask?.task_id]);

  const assigneeOptions = useMemo(() => {
    return Array.from(new Set(tasks.map((task) => task.assignee_id).filter((value): value is string => Boolean(value)))).sort();
  }, [tasks]);

  const visibleTasks = useMemo(() => {
    const query = searchText.trim().toLocaleLowerCase();
    return tasks.filter((task) => {
      if (task.status === "archived" && !showArchived) return false;
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
    setActionError(null);
    setSelectedTaskId(taskId);
    setEditMode(false);
    setDetail(null);
  }, []);

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
    if (createDraft.status === "todo" && (!specComplete || !createLimit || !createLimitAcknowledged)) {
      setCreateError("A Todo task needs a complete typed capability specification and acknowledgment of its current runtime limit.");
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
    setCreateBusy(true);
    try {
      const response = await boardRequest<{ task: WorkBoardTask; idempotent_replay: boolean }>("/tasks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      createIdempotencyRef.current = makeIdempotencyKey();
      setCreateOpen(false);
      setCreateDraft({
        title: "", body: "", goalId: "", goalRevision: "", status: "triage", capabilityId: "",
        typedInputRef: "", typedInputDigest: "", executorId: "", assigneeId: "", priority: "50",
        scheduledAt: "", requiresReview: false, reviewerId: "",
      });
      await refreshSnapshot();
      openTask(response.task.task_id);
      setAnnouncement(response.idempotent_replay ? "The matching task already exists." : "Task created in the canonical work board.");
    } catch (error) {
      setCreateError(inputErrorMessage(error));
      if (error instanceof WorkBoardApiError && error.status === 409) await refreshSnapshot();
    } finally {
      setCreateBusy(false);
    }
  };

  const createIdempotencyRef = useRef(makeIdempotencyKey());

  const setCreateField = <K extends keyof CreateDraft>(key: K, value: CreateDraft[K]) => {
    setCreateDraft((current) => ({ ...current, [key]: value }));
  };

  const openEditMode = () => {
    if (!selectedTask) return;
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
    const body: WorkBoardTaskPatchRequest = {
      expected_revision: selectedTask.task_revision,
      title: editDraft.title.trim(),
      body: editDraft.body.trim(),
      priority,
      capability_id: editDraft.capabilityId.trim() || null,
      typed_input_ref: ref || null,
      typed_input_digest: digest.toLowerCase() || null,
      executor_id: editDraft.executorId.trim() || null,
      assignee_id: editDraft.assigneeId.trim() || null,
      scheduled_at: editDraft.scheduledAt ? new Date(editDraft.scheduledAt).toISOString() : null,
    };
    try {
      await boardRequest<{ task: WorkBoardTask }>(`/tasks/${encodeURIComponent(selectedTask.task_id)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      await refreshSnapshot();
      await refreshSelectedTask();
      setEditMode(false);
      setAnnouncement("Task fields saved with the current revision.");
    } catch (error) {
      setActionError(inputErrorMessage(error));
      if (error instanceof WorkBoardApiError && error.status === 409) {
        setStale(true);
        await refreshSnapshot();
        await refreshSelectedTask();
      }
    } finally {
      setBusyAction(false);
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
      await boardRequest<{ task: WorkBoardTask } | { task: WorkBoardTask; attempt: unknown }>(
        `/tasks/${encodeURIComponent(selectedTask.task_id)}/actions`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) },
      );
      await refreshSnapshot();
      await refreshSelectedTask();
      setBlockReason("");
      setBlockConfirmed(false);
      setUnblockResolution("");
      setAnnouncement(`${STATUS_LABELS[selectedTask.status]} task action ${action} requested.`);
    } catch (error) {
      setActionError(inputErrorMessage(error));
      if (error instanceof WorkBoardApiError && error.status === 409) {
        setStale(true);
        await refreshSnapshot();
        await refreshSelectedTask();
      }
    } finally {
      setBusyAction(false);
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
      await boardRequest<{ comment: WorkBoardComment }>(`/tasks/${encodeURIComponent(selectedTask.task_id)}/comments`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      setCommentDraft("");
      await refreshSelectedTask();
      await refreshSnapshot();
    } catch (error) {
      setActionError(inputErrorMessage(error));
      if (error instanceof WorkBoardApiError && error.status === 409) {
        setStale(true);
        await refreshSnapshot();
        await refreshSelectedTask();
      }
    } finally {
      setBusyAction(false);
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
      await boardRequest(`/links`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      setParentTaskIdDraft("");
      setChildTaskIdDraft("");
      await refreshSelectedTask();
      await refreshSnapshot();
    } catch (error) {
      setActionError(inputErrorMessage(error));
      if (error instanceof WorkBoardApiError && error.status === 409) {
        setStale(true);
        await refreshSnapshot();
        await refreshSelectedTask();
      }
    } finally {
      setBusyAction(false);
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
      await boardRequest(`/links`, {
        method: "DELETE", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      await refreshSelectedTask();
      await refreshSnapshot();
    } catch (error) {
      setActionError(inputErrorMessage(error));
      if (error instanceof WorkBoardApiError && error.status === 409) {
        setStale(true);
        await refreshSnapshot();
        await refreshSelectedTask();
      }
    } finally {
      setBusyAction(false);
    }
  };

  const dragTaskIdRef = useRef<string | null>(null);
  const dropTask = (status: WorkBoardStatus) => async (event: React.DragEvent<HTMLElement>) => {
    event.preventDefault();
    const taskId = event.dataTransfer.getData("text/plain") || dragTaskIdRef.current;
    dragTaskIdRef.current = null;
    const task = tasks.find((item) => item.task_id === taskId);
    if (!task) return;
    if (task.status === "triage" && status === "todo") {
      setAnnouncement("Open task details, complete the typed specification, and acknowledge the current limit before promoting to Todo.");
      openTask(task.task_id);
      return;
    }
    // Ready/Running/Review/Done are server-owned; no card drag can force them.
    setAnnouncement(`The backend does not allow moving ${STATUS_LABELS[task.status]} directly to ${STATUS_LABELS[status]}.`);
  };

  const canManuallyBlock = (task: WorkBoardTask) => {
    return ["triage", "todo", "ready", "review"].includes(task.status)
      && !isActiveAttempt(task)
      && task.status !== "running";
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

  const selectGoal = (goalId: string) => {
    const goal = allGoals.find((item) => item.id === goalId);
    setCreateField("goalId", goalId);
    setCreateField("goalRevision", goal?.revision ? String(goal.revision) : "");
  };

  const statusOptions: Array<"all" | WorkBoardStatus> = ["all", ...BOARD_COLUMNS, "archived"];
  const activeCount = tasks.filter((task) => task.status === "running").length;

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
          <button type="button" className="cockpit-feedback-button" onClick={() => setCreateOpen(true)}>
            Create task
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
      {goalError && <div className="mt-2 text-xs text-amber-300" role="status">Goal metadata unavailable: {goalError}</div>}
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
                          draggable
                          onDragStart={(event) => {
                            dragTaskIdRef.current = task.task_id;
                            event.dataTransfer.setData("text/plain", task.task_id);
                            event.dataTransfer.effectAllowed = "move";
                          }}
                          className="rounded border border-white/10 bg-slate-950/60 p-3 text-xs"
                        >
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

      {showArchived && (
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

      {selectedTask && (
        <div className="fixed inset-0 z-[80] flex justify-end bg-black/55" onMouseDown={(event) => { if (event.target === event.currentTarget) setSelectedTaskId(null); }}>
          <aside role="dialog" aria-modal="false" aria-labelledby="work-board-detail-title" className="h-full w-full max-w-2xl overflow-y-auto border-l border-white/15 bg-slate-950 p-4 shadow-2xl">
            <div className="sticky top-0 z-10 -mx-4 -mt-4 mb-4 flex items-center justify-between border-b border-white/10 bg-slate-950/95 px-4 py-3 backdrop-blur">
              <div>
                <div className="text-[10px] uppercase tracking-wide opacity-70">{STATUS_LABELS[selectedTask.status]} · revision {selectedTask.task_revision}</div>
                <h2 id="work-board-detail-title" className="text-lg font-semibold">{selectedTask.title}</h2>
              </div>
              <button type="button" className="cockpit-feedback-button" aria-label="Close task details" onClick={() => setSelectedTaskId(null)}>Close</button>
            </div>

            {(detailLoading || stale) && <div className="mb-3 text-xs text-amber-200" role="status">{detailLoading ? "Refreshing task detail…" : "Showing the last confirmed task detail."}</div>}
            {detailError && <div className="mb-3 rounded border border-red-500/40 p-2 text-sm" role="alert">{detailError}<button type="button" className="ml-2 underline" onClick={() => void refreshSelectedTask()}>Refresh detail</button></div>}
            {actionError && <div className="mb-3 rounded border border-amber-500/40 p-2 text-sm" role="alert">{actionError}</div>}

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
                    <button type="button" className="cockpit-feedback-button" onClick={openEditMode} disabled={busyAction}>Edit bounded fields</button>
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

              <section className="rounded border border-white/10 p-3">
                <div className="font-semibold">Actions and recovery</div>
                <div className="mt-1">{selectedTask.status === "blocked" ? `Blocked: ${selectedTask.block_reason || "No safe reason was supplied."}` : `Current state: ${STATUS_LABELS[selectedTask.status]}`}</div>
                {selectedTask.recovery_action && <div className="mt-1">Server recovery action: {RECOVERY_LABELS[selectedTask.recovery_action]}</div>}
                <div className="mt-2 flex flex-wrap gap-2">
                  {selectedTask.status === "triage" && (
                    <button type="button" className="cockpit-feedback-button" disabled={!canPromote || busyAction} onClick={() => void performAction("promote")} title={!canPromote ? "Complete the typed specification and acknowledge the current server limit first." : undefined}>Promote to Todo</button>
                  )}
                  {selectedTask.status === "blocked" && selectedTask.recovery_action === "unblock" && (
                    <form className="flex min-w-full flex-col gap-2" onSubmit={(event) => { event.preventDefault(); const resolution = unblockResolution.trim(); if (!resolution || resolution.length > 1000) { setActionError("Enter a resolution between 1 and 1000 characters."); return; } void performAction("unblock", { resolution }); }}>
                      <label>Resolution<textarea className="cockpit-input mt-1 w-full" maxLength={1000} rows={2} value={unblockResolution} onChange={(event) => setUnblockResolution(event.currentTarget.value)} /></label>
                      <button type="submit" className="cockpit-feedback-button self-start" disabled={busyAction || !unblockResolution.trim() || unblockResolution.trim().length > 1000}>Unblock after rechecking authority</button>
                    </form>
                  )}
                  {selectedTask.status === "blocked" && selectedTask.recovery_action === "retry" && !isActiveAttempt(selectedTask) && (
                    <button type="button" className="cockpit-feedback-button" disabled={busyAction} onClick={() => void performAction("retry", {}, true)}>Retry (new attempt)</button>
                  )}
                  {selectedTask.status === "blocked" && selectedTask.recovery_action !== "retry" && selectedTask.recovery_action !== "unblock" && selectedTask.recovery_action && (
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
                    <form className="flex min-w-full flex-col gap-2" onSubmit={(event) => { event.preventDefault(); if (!blockConfirmed) { setActionError("Confirm the operator block before submitting."); return; } const reason = blockReason.trim(); if (!reason || reason.length > 1000) { setActionError("Enter a block reason between 1 and 1000 characters."); return; } void performAction("block", { block_kind: "operator", reason }); }}>
                      <label>Manual block reason<textarea className="cockpit-input mt-1 w-full" maxLength={1000} rows={2} value={blockReason} onChange={(event) => setBlockReason(event.currentTarget.value)} /></label>
                      <label className="flex items-center gap-2"><input type="checkbox" checked={blockConfirmed} onChange={(event) => setBlockConfirmed(event.currentTarget.checked)} />Confirm this operator block</label>
                      <button type="submit" className="cockpit-feedback-button self-start" disabled={busyAction || !blockConfirmed || !blockReason.trim() || blockReason.trim().length > 1000}>Block task</button>
                    </form>
                  )}
                  {selectedTask.status === "review" && <div className="w-full rounded bg-slate-900 p-2">Awaiting {selectedTask.reviewer_id ?? "the named reviewer"}. Evidence: {READBACK_LABELS[selectedTask.readback_status]} readback · {VERIFICATION_LABELS[selectedTask.verification_status]} verification. Reviewer verdict controls appear only when authorized by the server.</div>}
                </div>
                {selectedTask.status === "blocked" && (selectedTask.block_kind === "unknown_effect" || selectedTask.block_kind === "cost_liability" || selectedTask.recovery_action === "reconcile_external_effect") && (
                  <div className="mt-2 rounded border border-amber-500/40 p-2" role="status">External effect or cost is unresolved. Reconcile independent readback before any new attempt; retry is unavailable.</div>
                )}
                {isActiveAttempt(selectedTask) && <div className="mt-2 text-[10px] opacity-75">Attempt is active; manual block and retry are disabled until the durable run is reconciled.</div>}
              </section>

              <section className="rounded border border-white/10 p-3">
                <div className="font-semibold">Dependencies</div>
                <div className="mt-1">Parent progress {selectedTask.completed_dependency_count}/{selectedTask.dependency_count}</div>
                <div className="mt-2 grid gap-1">
                  <div className="text-[10px] uppercase opacity-70">Parents</div>
                  {detail?.parents.map((parentId) => {
                    const parent = taskById.get(parentId);
                    return <div key={parentId} className="flex items-center justify-between gap-2"><button type="button" className="break-all text-left underline" onClick={() => openTask(parentId)}>{parent?.title ?? parentId}</button><span>{parent ? STATUS_LABELS[parent.status] : "Not in current snapshot"}</span><button type="button" className="underline" disabled={busyAction || selectedTask.status === "running"} onClick={() => void deleteLink(parentId, selectedTask.task_id, selectedTask.task_revision)}>Remove</button></div>;
                  })}
                  <form className="flex gap-2" onSubmit={(event) => { event.preventDefault(); if (parentTaskIdDraft.trim()) void createLink(parentTaskIdDraft, selectedTask.task_id, selectedTask.task_revision); }}>
                    <label className="sr-only" htmlFor="work-board-parent-id">Parent task ID</label><input id="work-board-parent-id" aria-label="Parent task ID" className="cockpit-input min-w-0 flex-1" maxLength={128} value={parentTaskIdDraft} onChange={(event) => setParentTaskIdDraft(event.currentTarget.value)} placeholder="Parent task ID" />
                    <button type="submit" className="cockpit-feedback-button" disabled={busyAction || !parentTaskIdDraft.trim() || selectedTask.status === "running"}>Add parent</button>
                  </form>
                  <div className="mt-2 text-[10px] uppercase opacity-70">Children</div>
                  {detail?.children.map((childId) => {
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
                  {detail?.attempts.map((attempt) => (
                    <div key={attempt.attempt_id} className="rounded bg-black/20 p-2">
                      <div>Attempt {attempt.attempt_id} · {attempt.ended_at ? attempt.outcome ?? "ended" : "active"} · fence {attempt.fencing_token}</div>
                      <div>Started {safeDateTime(attempt.started_at)} · ended {safeDateTime(attempt.ended_at)} · executor {attempt.executor_id ?? "Unassigned"}</div>
                      <div>Readback {READBACK_LABELS[attempt.readback_status]} · verification {VERIFICATION_LABELS[attempt.verification_status]}</div>
                      {attempt.workflow_run_id && <div className="mt-1 break-all">Workflow run {attempt.workflow_run_id}{onInspectWorkflowRun && <button type="button" className="ml-2 underline" onClick={() => onInspectWorkflowRun(attempt.workflow_run_id!)}>Open workflow evidence</button>}</div>}
                      {[...attempt.receipt_refs].map((receipt, index) => (
                        <div key={`${attempt.attempt_id}:receipt:${index}`} className="mt-1 border-t border-white/10 pt-1">
                          <div>{receiptTitle(receipt)} · {safeReferenceLabel(receipt)}</div>
                          <div>{receipt.status ?? receipt.outcome ?? "Receipt"}{receipt.verified === true ? " · verified" : ""}{receipt.readback_status ? ` · readback ${READBACK_LABELS[receipt.readback_status]}` : ""}</div>
                          {receipt.content_sha256 && <div className="break-all font-mono text-[10px]">SHA-256 {receipt.content_sha256}</div>}
                          {receipt.file_path && <div className="break-all text-[10px]">Artifact path {receipt.file_path}</div>}
                          {receipt.workflow_run_id && onInspectWorkflowRun && <button type="button" className="underline" onClick={() => onInspectWorkflowRun(receipt.workflow_run_id!)}>Inspect existing workflow record</button>}
                        </div>
                      ))}
                    </div>
                  ))}
                  {[...selectedTask.result_refs, ...selectedTask.artifact_refs].map((reference, index) => (
                    <div key={`task-ref:${index}`} className="rounded bg-black/20 p-2">
                      <div>{receiptTitle(reference)} · {safeReferenceLabel(reference)}</div>
                      <div>{reference.status ?? reference.outcome ?? "Reference"}{reference.verified === true ? " · verified" : ""}</div>
                      {reference.content_sha256 && <div className="break-all font-mono text-[10px]">SHA-256 {reference.content_sha256}</div>}
                      {reference.file_path && <div className="break-all text-[10px]">Artifact path {reference.file_path}</div>}
                      {reference.workflow_run_id && onInspectWorkflowRun && <button type="button" className="underline" onClick={() => onInspectWorkflowRun(reference.workflow_run_id!)}>Open workflow evidence</button>}
                    </div>
                  ))}
                  {(!detail?.attempts.length && !selectedTask.result_refs.length && !selectedTask.artifact_refs.length) && <div className="cockpit-empty">No attempts or output references yet.</div>}
                </div>
              </section>

              <section className="rounded border border-white/10 p-3">
                <div className="font-semibold">Comments</div>
                <div className="mt-2 grid gap-2">
                  {detail?.comments.map((comment) => <article key={comment.comment_id} className="rounded bg-black/20 p-2"><div className="text-[10px] opacity-70">{comment.author_principal_id} · {safeDateTime(comment.created_at)}</div><div className="mt-1 whitespace-pre-wrap break-words">{comment.body}</div></article>)}
                  {(!detail?.comments.length) && <div className="cockpit-empty">No comments yet.</div>}
                </div>
                <form className="mt-2 grid gap-2" onSubmit={(event) => void addComment(event)}>
                  <label>Comment<textarea className="cockpit-input mt-1 w-full" maxLength={2000} rows={2} value={commentDraft} onChange={(event) => setCommentDraft(event.currentTarget.value)} /></label>
                  <button type="submit" className="cockpit-feedback-button justify-self-start" disabled={busyAction || !commentDraft.trim() || commentDraft.trim().length > 2000}>Add comment</button>
                </form>
              </section>

              <section className="rounded border border-white/10 p-3">
                <div className="font-semibold">Safe task event timeline</div>
                <div className="mt-2 grid gap-2">
                  {detail?.events.map((event) => <div key={event.event_id} className="border-l border-white/20 pl-2"><div>{event.kind.replace(/[_.]/g, " ")} · {safeDateTime(event.created_at)}</div><div className="text-[10px] opacity-70">{eventSummary(event)}</div></div>)}
                  {(!detail?.events.length) && <div className="cockpit-empty">No task events yet.</div>}
                </div>
              </section>
            </div>
          </aside>
        </div>
      )}

      {createOpen && (
        <div className="fixed inset-0 z-[90] flex items-center justify-center bg-black/65 p-4" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !createBusy) setCreateOpen(false); }}>
          <form role="dialog" aria-modal="true" aria-labelledby="work-board-create-title" className="max-h-[90vh] w-full max-w-2xl overflow-y-auto rounded border border-white/15 bg-slate-950 p-4 shadow-2xl" onSubmit={(event) => void createTask(event)}>
            <div className="flex items-center justify-between gap-2"><h2 id="work-board-create-title" className="text-lg font-semibold">Create a goal-linked task</h2><button type="button" className="cockpit-feedback-button" onClick={() => setCreateOpen(false)} disabled={createBusy}>Close</button></div>
            <p className="mt-1 text-xs opacity-70">A free-text idea starts in Triage. Todo requires a typed capability input and current runtime-limit acknowledgment. The dispatcher alone promotes eligible work to Ready.</p>
            {createError && <div className="mt-2 rounded border border-amber-500/40 p-2 text-sm" role="alert">{createError}</div>}
            <div className="mt-3 grid gap-3 sm:grid-cols-2">
              <label className="sm:col-span-2">Title<input className="cockpit-input mt-1 w-full" autoFocus maxLength={200} required value={createDraft.title} onChange={(event) => setCreateField("title", event.currentTarget.value)} /></label>
              <label className="sm:col-span-2">Bounded task description<textarea className="cockpit-input mt-1 w-full" maxLength={4000} rows={3} value={createDraft.body} onChange={(event) => setCreateField("body", event.currentTarget.value)} /></label>
              <label>Goal<select className="cockpit-input mt-1 w-full" required value={createDraft.goalId} onChange={(event) => selectGoal(event.currentTarget.value)}><option value="">Choose a goal</option>{allGoals.map((goal) => <option key={goal.id} value={goal.id}>{goal.title} · {goal.id}</option>)}</select></label>
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
            </div>
            <div className="mt-3 flex flex-wrap justify-end gap-2"><button type="button" className="cockpit-feedback-button" onClick={() => setCreateOpen(false)} disabled={createBusy}>Cancel</button><button type="submit" className="cockpit-feedback-button" disabled={createBusy || !createDraft.goalId || !createDraft.goalRevision || (createDraft.status === "todo" && (!createLimit || !createLimitAcknowledged || !createDraft.capabilityId.trim() || !createDraft.typedInputRef.trim() || !hasValidDigest(createDraft.typedInputDigest)))}>{createBusy ? "Creating…" : createDraft.status === "triage" ? "Create in Triage" : "Create specified Todo"}</button></div>
          </form>
        </div>
      )}
    </section>
  );
}

export { WorkBoardPanel, buildBoardSocketUrl, eventSummary };
