import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  WorkBoardAttempt,
  WorkBoardEventPage,
  WorkBoardTask,
  WorkBoardTaskDetail,
  WorkBoardTaskPage,
} from "../../types";
import { WorkBoardPanel } from "./WorkBoardPanel";

function response(payload: unknown, ok = true, status = ok ? 200 : 500) {
  return {
    ok,
    status,
    json: async () => payload,
  };
}

const EMPTY_ATTEMPTS: WorkBoardAttempt[] = [];

function task(overrides: Partial<WorkBoardTask> = {}): WorkBoardTask {
  return {
    task_id: "task-1",
    creation_sequence: 1,
    owner_principal_id: "operator:one",
    owner_session_id: "operator-session-1",
    origin_session_id: "operator-session-1",
    origin_thread_id: null,
    goal_id: "goal-1",
    goal_revision: 3,
    title: "Bounded task",
    body: "A safe task description",
    capability_id: "goal-snapshot-to-file",
    typed_input_ref: "workspace-json:inputs/task.json",
    typed_input_digest: "a".repeat(64),
    executor_id: "executor-local",
    assignee_id: "operator:one",
    priority: 50,
    idempotency_scope: "task",
    idempotency_key: "task-1-key",
    scheduled_at: null,
    status: "todo",
    block_kind: null,
    block_reason: null,
    block_source_status: null,
    cancel_requested_at: null,
    requires_review: false,
    reviewer_id: null,
    dependency_count: 0,
    completed_dependency_count: 0,
    dispatch_rank: null,
    recovery_action: null,
    readback_status: "not_started",
    verification_status: "not_started",
    task_revision: 3,
    result_refs: [],
    artifact_refs: [],
    latest_attempt: null,
    created_at: "2026-09-23T10:00:00Z",
    updated_at: "2026-09-23T10:00:00Z",
    completed_at: null,
    archived_at: null,
    ...overrides,
  };
}

function detail(taskValue: WorkBoardTask, overrides: Partial<WorkBoardTaskDetail> = {}): WorkBoardTaskDetail {
  return {
    task: taskValue,
    attempts: EMPTY_ATTEMPTS,
    parents: [],
    children: [],
    comments: [],
    events: [],
    revision: taskValue.task_revision,
    ...overrides,
  };
}

function page(tasks: WorkBoardTask[], lastEventId = 7): WorkBoardTaskPage {
  return { tasks, next_after: null, last_event_id: lastEventId };
}

function events(lastEventId = 7): WorkBoardEventPage {
  return { events: [], last_event_id: lastEventId, gap: false };
}

function boardEvent(eventId: number): WorkBoardEventPage["events"][number] {
  return {
    event_id: eventId,
    task_id: "task-1",
    kind: "task_updated",
    metadata: { status: "ready" },
    created_at: "2026-09-23T10:00:01Z",
  };
}

function endedAttempt(overrides: Partial<WorkBoardAttempt> = {}): WorkBoardAttempt {
  return {
    attempt_id: "attempt-1",
    task_id: "task-1",
    workflow_run_id: "workflow-run-1",
    task_revision_at_claim: 3,
    lease_owner: null,
    cancel_requested_at: null,
    lease_expires_at: null,
    heartbeat_at: null,
    fencing_token: 1,
    executor_id: "executor-local",
    started_at: "2026-09-23T10:00:00Z",
    ended_at: "2026-09-23T10:00:01Z",
    outcome: "transient",
    receipt_refs: [{ status: "no_external_effect", reason_code: "no_external_effect" }],
    readback_status: "not_applicable",
    verification_status: "failed",
    created_at: "2026-09-23T10:00:00Z",
    updated_at: "2026-09-23T10:00:01Z",
    ...overrides,
  };
}

function limits(goalRevision = 3) {
  return {
    goal_id: "goal-1",
    goal_revision: goalRevision,
    effective_max_runtime_seconds: 300,
    default_max_runtime_seconds: 300,
    hard_max_runtime_seconds: 900,
    attempt_limit: 2,
    limit_source: "default" as const,
  };
}

function taskResponse(fetchMock: ReturnType<typeof vi.fn>, currentTask: WorkBoardTask, eventCursor = 7) {
  fetchMock.mockImplementation((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/api/work-board/tasks?") && !url.match(/\/tasks\/[^?]+/)) {
      return Promise.resolve(response(page([currentTask], eventCursor)));
    }
    if (url.includes("/api/work-board/events")) return Promise.resolve(response(events(eventCursor)));
    if (url.endsWith("/api/goals/tree")) return Promise.resolve(response([]));
    if (url.includes("/api/work-board/goals/goal-1/execution-limits")) {
      return Promise.resolve(response(limits(currentTask.goal_revision)));
    }
    if (url.includes(`/api/work-board/tasks/${currentTask.task_id}`)) {
      return Promise.resolve(response(detail(currentTask)));
    }
    return Promise.resolve(response({}));
  });
}

class TestBoardSocket {
  static instances: TestBoardSocket[] = [];

  readonly url: string;
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(url: string) {
    this.url = url;
    TestBoardSocket.instances.push(this);
  }

  open() {
    this.readyState = 1;
    this.onopen?.();
  }

  close(code = 1000, reason = "") {
    this.readyState = 3;
    this.onclose?.({ code, reason } as CloseEvent);
  }

  send(payload: unknown) {
    this.onmessage?.({ data: JSON.stringify(payload) } as MessageEvent);
  }
}

describe("WorkBoardPanel", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    TestBoardSocket.instances = [];
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("WebSocket", TestBoardSocket);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("carries the server snapshot cursor to the authenticated socket and reconnects from a fresh snapshot", async () => {
    const currentTask = task({ status: "ready", dispatch_rank: 1 });
    taskResponse(fetchMock, currentTask, 42);

    render(<WorkBoardPanel />);
    await waitFor(() => expect(TestBoardSocket.instances).toHaveLength(1));
    const first = TestBoardSocket.instances[0];
    expect(first?.url).toContain("/ws/work-board/events?after=42");
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes("/events?after=42"))).toBe(true);

    vi.useFakeTimers();
    act(() => first?.close(1006, "network reset"));
    await act(async () => {
      vi.advanceTimersByTime(3_000);
      await Promise.resolve();
      await Promise.resolve();
    });
    await vi.waitFor(() => expect(TestBoardSocket.instances).toHaveLength(2), { timeout: 1_000 });

    expect(TestBoardSocket.instances[1]?.url).toContain("/ws/work-board/events?after=42");
    const reconnectCalls = fetchMock.mock.calls
      .map(([input]) => String(input))
      .filter((url) => url.includes("/api/work-board/tasks?") || url.includes("/api/work-board/events?after=42"));
    expect(reconnectCalls.slice(-2)).toEqual([
      expect.stringContaining("/api/work-board/tasks?"),
      expect.stringContaining("/api/work-board/events?after=42"),
    ]);
  });

  it("takes a fresh snapshot when a live event detail cannot be read", async () => {
    const currentTask = task({ status: "ready" });
    let taskListCalls = 0;
    let detailCalls = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/work-board/tasks?") && !url.match(/\/tasks\/[^?]+/)) {
        taskListCalls += 1;
        return Promise.resolve(response(page([currentTask], taskListCalls === 1 ? 42 : 43)));
      }
      if (url.includes("/api/work-board/events?after=42")) return Promise.resolve(response(events(42)));
      if (url.includes("/api/work-board/events?after=43")) return Promise.resolve(response(events(43)));
      if (url.endsWith("/api/goals/tree")) return Promise.resolve(response([]));
      if (url.includes("/api/work-board/tasks/task-1")) {
        detailCalls += 1;
        if (detailCalls === 1) return Promise.resolve(response({ detail: { code: "workspace_unavailable" } }, false, 503));
        return Promise.resolve(response(detail(currentTask)));
      }
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    await waitFor(() => expect(TestBoardSocket.instances).toHaveLength(1));
    act(() => TestBoardSocket.instances[0]?.send(boardEvent(43)));

    await waitFor(() => expect(TestBoardSocket.instances).toHaveLength(2));
    expect(taskListCalls).toBeGreaterThanOrEqual(2);
    expect(TestBoardSocket.instances[1]?.url).toContain("/ws/work-board/events?after=43");
  });

  it("rejects illegal drag requests that would force Running or Done", async () => {
    const triageTask = task({ status: "triage", title: "Rough idea", capability_id: null, typed_input_ref: null, typed_input_digest: null });
    const doneTask = task({ task_id: "task-done", creation_sequence: 2, status: "done", title: "Finished task" });
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/work-board/tasks?") && !url.match(/\/tasks\/[^?]+/)) return Promise.resolve(response(page([triageTask, doneTask])));
      if (url.includes("/api/work-board/events")) return Promise.resolve(response(events()));
      if (url.endsWith("/api/goals/tree")) return Promise.resolve(response([]));
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    await screen.findByText("Rough idea");
    const triageCard = within(screen.getByRole("region", { name: "Triage column" })).getByRole("listitem");
    fireEvent.dragStart(triageCard, { dataTransfer: { setData: vi.fn(), getData: vi.fn(() => "task-1"), effectAllowed: "move" } });
    fireEvent.drop(screen.getByRole("region", { name: "Running column" }), { dataTransfer: { getData: () => "task-1" } });
    fireEvent.drop(screen.getByRole("region", { name: "Done column" }), { dataTransfer: { getData: () => "task-1" } });

    expect(fetchMock.mock.calls.some(([, init]) => (init as RequestInit | undefined)?.method === "POST")).toBe(false);
    expect(screen.getByText(/backend does not allow moving Triage directly to Done/i)).toBeInTheDocument();
  });

  it("refreshes canonical detail and snapshot after a stale revision conflict", async () => {
    const blocked = task({ status: "blocked", title: "Blocked authority", block_reason: "Grant expired", recovery_action: "unblock" });
    const refreshed = task({ ...blocked, task_revision: 4, block_reason: "Grant restored" });
    taskResponse(fetchMock, blocked);
    let actionCalls = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/work-board/tasks/") && init?.method === "POST") {
        actionCalls += 1;
        return Promise.resolve(response({ detail: { code: "revision_conflict", message: "The task revision is stale." } }, false, 409));
      }
      if (url.includes("/api/work-board/tasks?") && !url.match(/\/tasks\/[^?]+/)) return Promise.resolve(response(page([refreshed])));
      if (url.includes("/api/work-board/events")) return Promise.resolve(response(events()));
      if (url.endsWith("/api/goals/tree")) return Promise.resolve(response([]));
      if (url.includes("/api/work-board/goals/goal-1/execution-limits")) return Promise.resolve(response(limits()));
      if (url.includes("/api/work-board/tasks/task-1")) return Promise.resolve(response(detail(refreshed)));
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "Open task Blocked authority" }));
    const resolution = await screen.findByLabelText("Resolution");
    fireEvent.change(resolution, { target: { value: "The grant was restored and rechecked." } });
    fireEvent.click(screen.getByRole("button", { name: "Unblock after rechecking authority" }));

    await waitFor(() => expect(actionCalls).toBe(1));
    await waitFor(() => expect(screen.getByText(/task revision is stale/i)).toBeInTheDocument());
    const taskListCalls = fetchMock.mock.calls.filter(([input]) => String(input).includes("/api/work-board/tasks?"));
    const detailCalls = fetchMock.mock.calls.filter(([input]) => String(input).includes("/api/work-board/tasks/task-1"));
    expect(taskListCalls.length).toBeGreaterThanOrEqual(2);
    expect(detailCalls.length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText(/revision 4/)).toBeInTheDocument();
  });

  it("shows unknown-effect reconciliation and does not offer retry", async () => {
    const unknown = task({
      status: "blocked",
      title: "Unknown external effect",
      block_kind: "unknown_effect",
      block_reason: "The destination outcome cannot be proven.",
      recovery_action: "reconcile_external_effect",
      latest_attempt: null,
    });
    taskResponse(fetchMock, unknown);

    render(<WorkBoardPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "Open task Unknown external effect" }));

    expect(await screen.findByText(/External effect or cost is unresolved/i)).toBeInTheDocument();
    expect(within(screen.getByRole("dialog")).getByText("Blocked: The destination outcome cannot be proven.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Retry/ })).not.toBeInTheDocument();
  });

  it("shows review reviewer and evidence without inventing a verdict action", async () => {
    const review = task({
      status: "review",
      title: "Review verified output",
      requires_review: true,
      reviewer_id: "operator:reviewer",
      readback_status: "verified",
      verification_status: "passed",
    });
    taskResponse(fetchMock, review);

    render(<WorkBoardPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "Open task Review verified output" }));

    expect(await screen.findByText(/Awaiting operator:reviewer/i)).toBeInTheDocument();
    expect(screen.getByText(/Verified readback · Passed verification/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Edit bounded fields" })).toBeDisabled();
    expect(screen.queryByRole("button", { name: /approve|complete review|request changes/i })).not.toBeInTheDocument();
  });

  it("sends only changed fields when editing a Ready task", async () => {
    const ready = task({ status: "ready", dispatch_rank: 1 });
    const renamed = task({ ...ready, title: "Renamed without changing execution authority", task_revision: 4 });
    let patchBody: Record<string, unknown> | null = null;
    let patchAccepted = false;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/work-board/tasks/task-1") && init?.method === "PATCH") {
        patchBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        patchAccepted = true;
        return Promise.resolve(response({ task: renamed }));
      }
      if (url.includes("/api/work-board/tasks?") && !url.match(/\/tasks\/[^?]+/)) return Promise.resolve(response(page([patchAccepted ? renamed : ready], patchAccepted ? 8 : 7)));
      if (url.includes("/api/work-board/events")) return Promise.resolve(response(events(8)));
      if (url.endsWith("/api/goals/tree")) return Promise.resolve(response([]));
      if (url.includes("/api/work-board/goals/goal-1/execution-limits")) return Promise.resolve(response(limits()));
      if (url.includes("/api/work-board/tasks/task-1")) return Promise.resolve(response(detail(patchAccepted ? renamed : ready)));
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "Open task Bounded task" }));
    fireEvent.click(await screen.findByRole("button", { name: "Edit bounded fields" }));
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: renamed.title } });
    fireEvent.click(screen.getByRole("button", { name: "Save with current revision" }));

    await waitFor(() => expect(patchBody).toEqual({ expected_revision: 3, title: renamed.title }));
  });

  it("requires acknowledgment of the current limit before a safe retry", async () => {
    const blocked = task({
      status: "blocked",
      block_kind: "transient",
      block_reason: "The local capability failed before dispatch.",
      recovery_action: "retry",
      latest_attempt: endedAttempt(),
    });
    let actionBody: Record<string, unknown> | null = null;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/work-board/tasks/task-1/actions")) {
        actionBody = JSON.parse(String(init?.body)) as Record<string, unknown>;
        return Promise.resolve(response({ task: task({ status: "ready", task_revision: 4 }) }));
      }
      if (url.includes("/api/work-board/tasks?") && !url.match(/\/tasks\/[^?]+/)) return Promise.resolve(response(page([blocked], 8)));
      if (url.includes("/api/work-board/events")) return Promise.resolve(response(events(8)));
      if (url.endsWith("/api/goals/tree")) return Promise.resolve(response([]));
      if (url.includes("/api/work-board/goals/goal-1/execution-limits")) return Promise.resolve(response(limits()));
      if (url.includes("/api/work-board/tasks/task-1")) return Promise.resolve(response(detail(blocked, { attempts: [endedAttempt()] })));
      return Promise.resolve(response({}));
    });
    vi.spyOn(window, "confirm").mockReturnValue(true);

    render(<WorkBoardPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "Open task Bounded task" }));
    const retry = await screen.findByRole("button", { name: "Retry (new attempt)" });
    expect(retry).toBeDisabled();
    fireEvent.click(await screen.findByLabelText(/I acknowledge the server-derived runtime limit/i));
    await waitFor(() => expect(retry).toBeEnabled());
    fireEvent.click(retry);

    await waitFor(() => expect(actionBody).toEqual({ action: "retry", expected_revision: 3 }));
  });

  it("submits an operator unblock with only the task revision and bounded resolution", async () => {
    const blocked = task({ status: "blocked", title: "Operator recovery", block_kind: "operator", recovery_action: "unblock", block_reason: "Waiting for operator input." });
    taskResponse(fetchMock, blocked);
    let actionBody: unknown = null;
    let currentTask = blocked;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/work-board/tasks/task-1/actions")) {
        actionBody = JSON.parse(String(init?.body));
        currentTask = { ...blocked, status: "todo", task_revision: 4 };
        return Promise.resolve(response({ task: currentTask }));
      }
      if (url.includes("/api/work-board/tasks?") && !url.match(/\/tasks\/[^?]+/)) return Promise.resolve(response(page([currentTask])));
      if (url.includes("/api/work-board/events")) return Promise.resolve(response(events()));
      if (url.endsWith("/api/goals/tree")) return Promise.resolve(response([]));
      if (url.includes("/api/work-board/goals/goal-1/execution-limits")) return Promise.resolve(response(limits()));
      if (url.includes("/api/work-board/tasks/task-1")) return Promise.resolve(response(detail(currentTask)));
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "Open task Operator recovery" }));
    fireEvent.change(await screen.findByLabelText("Resolution"), { target: { value: "The grant was restored and I rechecked the task authority." } });
    fireEvent.click(screen.getByRole("button", { name: "Unblock after rechecking authority" }));

    await waitFor(() => expect(actionBody).toEqual({
      action: "unblock",
      expected_revision: 3,
      resolution: "The grant was restored and I rechecked the task authority.",
    }));
    expect(JSON.stringify(actionBody)).not.toContain("run_id");
    expect(JSON.stringify(actionBody)).not.toContain("workflow_run_id");
  });
});
