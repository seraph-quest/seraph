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

function taskResponse(
  fetchMock: ReturnType<typeof vi.fn>,
  currentTask: WorkBoardTask,
  eventCursor = 7,
  currentDetail = detail(currentTask),
) {
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
      return Promise.resolve(response(currentDetail));
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
    let eventCalls = 0;
    let detailCalls = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/work-board/tasks?") && !url.match(/\/tasks\/[^?]+/)) {
        taskListCalls += 1;
        return Promise.resolve(response(page([currentTask], taskListCalls === 1 ? 42 : 43)));
      }
      if (url.includes("/api/work-board/events?after=42")) {
        eventCalls += 1;
        return Promise.resolve(response(eventCalls === 1
          ? events(42)
          : { events: [boardEvent(43)], last_event_id: 43, gap: false }));
      }
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

  it("replays ordered REST events when WebSocket notifications arrive out of order", async () => {
    const taskA = task({ task_id: "task-a", title: "Task A before" });
    const taskB = task({ task_id: "task-b", creation_sequence: 2, title: "Task B before" });
    const updatedA = { ...taskA, title: "Task A refreshed", task_revision: 4 };
    const updatedB = { ...taskB, title: "Task B refreshed", task_revision: 4 };
    let eventDeltaCalls = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/work-board/tasks?") && !url.match(/\/tasks\/[^?]+/)) return Promise.resolve(response(page([taskA, taskB], 42)));
      if (url.includes("/api/work-board/events?after=42")) {
        eventDeltaCalls += 1;
        return Promise.resolve(response(eventDeltaCalls === 1
          ? events(42)
          : { events: [{ ...boardEvent(43), task_id: "task-a" }, { ...boardEvent(44), task_id: "task-b" }], last_event_id: 44, gap: false }));
      }
      if (url.endsWith("/api/goals/tree")) return Promise.resolve(response([]));
      if (url.endsWith("/api/work-board/tasks/task-a")) return Promise.resolve(response(detail(updatedA)));
      if (url.endsWith("/api/work-board/tasks/task-b")) return Promise.resolve(response(detail(updatedB)));
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    await waitFor(() => expect(TestBoardSocket.instances).toHaveLength(1));
    act(() => {
      TestBoardSocket.instances[0]?.send({ ...boardEvent(44), task_id: "task-b" });
      TestBoardSocket.instances[0]?.send({ ...boardEvent(43), task_id: "task-a" });
    });

    expect(await screen.findByText("Task A refreshed")).toBeInTheDocument();
    expect(await screen.findByText("Task B refreshed")).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith("/api/work-board/tasks/task-a"))).toBe(true);
    expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith("/api/work-board/tasks/task-b"))).toBe(true);
  });

  it("reconnects from a fresh snapshot when the REST event feed trails a WebSocket event", async () => {
    const currentTask = task({ status: "ready" });
    let taskListCalls = 0;
    let event42Calls = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/work-board/tasks?") && !url.match(/\/tasks\/[^?]+/)) {
        taskListCalls += 1;
        return Promise.resolve(response(page([currentTask], taskListCalls === 1 ? 42 : 43)));
      }
      if (url.includes("/api/work-board/events?after=42")) {
        event42Calls += 1;
        return Promise.resolve(response(event42Calls === 1
          ? events(42)
          : { events: [boardEvent(43)], last_event_id: 43, gap: false }));
      }
      if (url.includes("/api/work-board/events?after=43")) return Promise.resolve(response(events(43)));
      if (url.endsWith("/api/goals/tree")) return Promise.resolve(response([]));
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    await waitFor(() => expect(TestBoardSocket.instances).toHaveLength(1));
    act(() => TestBoardSocket.instances[0]?.send(boardEvent(44)));

    await waitFor(() => expect(TestBoardSocket.instances).toHaveLength(2));
    expect(taskListCalls).toBeGreaterThanOrEqual(2);
    expect(TestBoardSocket.instances[1]?.url).toContain("/ws/work-board/events?after=43");
  });

  it("lets a new socket generation reconcile after an old detail request is aborted", async () => {
    const original = task({ status: "ready", title: "Before reconnect" });
    const updated = { ...original, title: "Updated after reconnect", task_revision: 4 };
    let taskListCalls = 0;
    let event42Calls = 0;
    let detailCalls = 0;
    const pendingRequest: { signal: AbortSignal | null } = { signal: null };
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/work-board/tasks?") && !url.match(/\/tasks\/[^?]+/)) {
        taskListCalls += 1;
        return Promise.resolve(response(page([taskListCalls === 1 ? original : updated], taskListCalls === 1 ? 42 : 43)));
      }
      if (url.includes("/api/work-board/events?after=42")) {
        event42Calls += 1;
        return Promise.resolve(response(event42Calls === 1
          ? events(42)
          : { events: [boardEvent(43)], last_event_id: 43, gap: false }));
      }
      if (url.includes("/api/work-board/events?after=43")) {
        if (taskListCalls === 2) return Promise.resolve(response(events(43)));
        return Promise.resolve(response({ events: [boardEvent(44)], last_event_id: 44, gap: false }));
      }
      if (url.endsWith("/api/goals/tree")) return Promise.resolve(response([]));
      if (url.endsWith("/api/work-board/tasks/task-1")) {
        detailCalls += 1;
        if (detailCalls === 1) {
          pendingRequest.signal = init?.signal ?? null;
          return new Promise((_resolve, reject) => {
            pendingRequest.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
          });
        }
        return Promise.resolve(response(detail(updated)));
      }
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    await waitFor(() => expect(TestBoardSocket.instances).toHaveLength(1));
    act(() => TestBoardSocket.instances[0]?.send(boardEvent(43)));
    await waitFor(() => expect(pendingRequest.signal).not.toBeNull());

    vi.useFakeTimers();
    act(() => TestBoardSocket.instances[0]?.close(1006, "network reset"));
    await act(async () => {
      vi.advanceTimersByTime(3_000);
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(TestBoardSocket.instances).toHaveLength(2);
    expect(pendingRequest.signal?.aborted).toBe(true);

    act(() => TestBoardSocket.instances[1]?.send(boardEvent(44)));
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByText("Updated after reconnect")).toBeInTheDocument();
    vi.useRealTimers();
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
    await waitFor(() => expect(screen.getAllByRole("alert").some((alert) => alert.textContent?.includes("directly to Running"))).toBe(true));
    fireEvent.drop(screen.getByRole("region", { name: "Done column" }), { dataTransfer: { getData: () => "task-1" } });

    expect(fetchMock.mock.calls.some(([, init]) => (init as RequestInit | undefined)?.method === "POST")).toBe(false);
    await waitFor(() => expect(screen.getAllByRole("alert").some((alert) => alert.textContent?.includes("directly to Done"))).toBe(true));
    expect(screen.getAllByRole("alert").find((alert) => alert.textContent?.includes("directly to Done"))).toHaveTextContent(/board was refreshed from the server/i);
    await waitFor(() => expect(fetchMock.mock.calls.filter(([input]) => String(input).includes("/api/work-board/tasks?")).length).toBeGreaterThanOrEqual(2));
  });

  it("promotes an eligible Triage card through the backend when dropped in Todo", async () => {
    const triage = task({ status: "triage", title: "Ready to specify" });
    const promoted = { ...triage, status: "todo" as const, task_revision: 4 };
    let taskListCalls = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/work-board/tasks?") && !url.match(/\/tasks\/[^?]+/)) {
        taskListCalls += 1;
        return Promise.resolve(response(page([taskListCalls === 1 ? triage : promoted], taskListCalls === 1 ? 42 : 43)));
      }
      if (url.includes("/api/work-board/events?after=42")) return Promise.resolve(response(events(42)));
      if (url.includes("/api/work-board/events?after=43")) return Promise.resolve(response(events(43)));
      if (url.endsWith("/api/goals/tree")) return Promise.resolve(response([]));
      if (url.includes("/api/work-board/goals/goal-1/execution-limits")) return Promise.resolve(response(limits()));
      if (url.includes("/api/work-board/tasks/task-1/actions") && init?.method === "POST") {
        return Promise.resolve(response({ task: promoted }));
      }
      if (url.endsWith("/api/work-board/tasks/task-1")) return Promise.resolve(response(detail(taskListCalls === 1 ? triage : promoted)));
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    const triageCard = within(await screen.findByRole("region", { name: "Triage column" })).getByRole("listitem");
    fireEvent.click(screen.getByRole("button", { name: "Open task Ready to specify" }));
    fireEvent.click(await screen.findByLabelText(/I acknowledge the server-derived runtime limit/));
    fireEvent.dragStart(triageCard, { dataTransfer: { setData: vi.fn(), getData: vi.fn(() => "task-1"), effectAllowed: "move" } });
    fireEvent.drop(screen.getByRole("region", { name: "Todo column" }), { dataTransfer: { getData: () => "task-1" } });

    await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => {
      const body = JSON.parse(String((init as RequestInit | undefined)?.body ?? "{}")) as Record<string, unknown>;
      return (init as RequestInit | undefined)?.method === "POST" && body.action === "promote" && body.expected_revision === 3;
    })).toBe(true));
    expect(await screen.findByRole("region", { name: "Todo column" })).toHaveTextContent("Ready to specify");
  });

  it("shows archived tasks when the Archived status filter is selected", async () => {
    const archived = task({ status: "archived", title: "Old completed task" });
    taskResponse(fetchMock, archived);

    render(<WorkBoardPanel />);
    fireEvent.change(await screen.findByLabelText("Status filter"), { target: { value: "archived" } });

    expect(await screen.findByRole("region", { name: "Archived tasks" })).toHaveTextContent("Old completed task");
  });

  it("keeps task B selected when task A comment refresh resolves late", async () => {
    const taskA = task({ task_id: "task-a", title: "Task A" });
    const taskB = task({ task_id: "task-b", creation_sequence: 2, title: "Task B" });
    let taskADetailCalls = 0;
    let resolveLateA: ((value: ReturnType<typeof response>) => void) | null = null;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/work-board/tasks?") && !url.match(/\/tasks\/[^?]+/)) return Promise.resolve(response(page([taskA, taskB])));
      if (url.includes("/api/work-board/events")) return Promise.resolve(response(events()));
      if (url.endsWith("/api/goals/tree")) return Promise.resolve(response([]));
      if (url.endsWith("/api/work-board/tasks/task-a/comments") && init?.method === "POST") return Promise.resolve(response({ comment: {} }));
      if (url.endsWith("/api/work-board/tasks/task-a")) {
        taskADetailCalls += 1;
        if (taskADetailCalls === 2) {
          return new Promise<ReturnType<typeof response>>((resolve) => { resolveLateA = resolve; });
        }
        return Promise.resolve(response(detail(taskA)));
      }
      if (url.endsWith("/api/work-board/tasks/task-b")) return Promise.resolve(response(detail(taskB)));
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "Open task Task A" }));
    const comment = await screen.findByLabelText("Comment");
    fireEvent.change(comment, { target: { value: "refresh after posting" } });
    fireEvent.click(screen.getByRole("button", { name: "Add comment" }));
    await waitFor(() => expect(resolveLateA).not.toBeNull());

    fireEvent.click(screen.getByRole("button", { name: "Open task Task B" }));
    const taskDetails = screen.getByRole("region", { name: "Task details for Task B" });
    expect(await within(taskDetails).findByText("Task B")).toBeInTheDocument();
    await act(async () => { resolveLateA?.(response(detail({ ...taskA, title: "Stale Task A response", task_revision: 99 }))); });
    expect(within(taskDetails).getByText("Task B")).toBeInTheDocument();
    expect(screen.queryByText("Stale Task A response")).not.toBeInTheDocument();
  });

  it("aborts a pending dependency link on unmount without refreshing after cleanup", async () => {
    const parent = task({ task_id: "task-parent", title: "Parent task" });
    const child = task({ task_id: "task-child", creation_sequence: 2, title: "Child task" });
    let resolveLink: ((value: ReturnType<typeof response>) => void) | null = null;
    const linkRequest = { signal: null as AbortSignal | null };
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/work-board/tasks?") && !url.match(/\/tasks\/[^?]+/)) return Promise.resolve(response(page([parent, child])));
      if (url.includes("/api/work-board/events")) return Promise.resolve(response(events()));
      if (url.endsWith("/api/goals/tree")) return Promise.resolve(response([]));
      if (url.endsWith("/api/work-board/tasks/task-child")) return Promise.resolve(response(detail(child)));
      if (url.endsWith("/api/work-board/links") && init?.method === "POST") {
        linkRequest.signal = init.signal as AbortSignal;
        return new Promise<ReturnType<typeof response>>((resolve) => { resolveLink = resolve; });
      }
      return Promise.resolve(response({}));
    });

    const { unmount } = render(<WorkBoardPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "Open task Child task" }));
    fireEvent.change(await screen.findByLabelText("Parent task ID"), { target: { value: "task-parent" } });
    fireEvent.click(screen.getByRole("button", { name: "Add parent" }));
    await waitFor(() => expect(resolveLink).not.toBeNull());

    const callsBeforeUnmount = fetchMock.mock.calls.length;
    unmount();
    expect(linkRequest.signal?.aborted).toBe(true);
    await act(async () => { resolveLink?.(response({ link: {} })); });

    expect(fetchMock.mock.calls).toHaveLength(callsBeforeUnmount);
  });

  it("opens task artifact references through the existing artifact inspector callback", async () => {
    const reference = { artifact_id: "artifact:notes/result.md", file_path: "notes/result.md", content_sha256: "b".repeat(64), verified: true };
    const currentTask = task({
      title: "Artifact task",
      artifact_refs: [reference],
      result_refs: [],
    });
    const onInspectArtifact = vi.fn();
    taskResponse(fetchMock, currentTask);
    render(<WorkBoardPanel onInspectArtifact={onInspectArtifact} />);

    fireEvent.click(await screen.findByRole("button", { name: "Open task Artifact task" }));
    fireEvent.click(await screen.findByRole("button", { name: "Inspect artifact notes/result.md" }));

    expect(onInspectArtifact).toHaveBeenCalledWith(reference);
  });

  it("opens attempt receipt artifacts through the existing artifact inspector callback", async () => {
    const reference = { artifact_id: "artifact:attempt-output", file_path: "artifacts/output.md", verified: true };
    const currentTask = task({ title: "Attempt artifact task" });
    taskResponse(fetchMock, currentTask, 7, detail(currentTask, {
      attempts: [endedAttempt({ receipt_refs: [reference] })],
    }));
    const onInspectArtifact = vi.fn();
    render(<WorkBoardPanel onInspectArtifact={onInspectArtifact} />);

    fireEvent.click(await screen.findByRole("button", { name: "Open task Attempt artifact task" }));
    fireEvent.click(await screen.findByRole("button", { name: "Inspect artifact artifacts/output.md" }));

    expect(onInspectArtifact).toHaveBeenCalledWith(reference);
  });

  it("retries an uncertain create with the same payload and idempotency key after remount", async () => {
    const goal = {
      id: "goal-pending-create",
      parent_id: null,
      path: "goal-pending-create",
      level: "daily",
      title: "Pending create goal",
      description: null,
      status: "active",
      domain: "productivity",
      start_date: null,
      due_date: null,
      revision: 1,
    };
    const createdTask = task({ task_id: "task-created-once", title: "Create once", goal_id: goal.id, goal_revision: 1 });
    let postCount = 0;
    let accepted = false;
    let retryPayload: Record<string, unknown> | null = null;
    const firstRequest = { payload: null as Record<string, unknown> | null, signal: null as AbortSignal | null };
    let resolveFirst: ((value: ReturnType<typeof response>) => void) | null = null;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/work-board/tasks?") && !url.match(/\/tasks\/[^?]+/)) return Promise.resolve(response(page(accepted ? [createdTask] : [])));
      if (url.includes("/api/work-board/events")) return Promise.resolve(response(events()));
      if (url.endsWith("/api/goals/tree")) return Promise.resolve(response([goal]));
      if (url.endsWith(`/api/work-board/goals/${goal.id}/execution-limits`)) return Promise.resolve(response(limits(1)));
      if (url.endsWith("/api/work-board/tasks") && init?.method === "POST") {
        postCount += 1;
        const payload = JSON.parse(String(init.body)) as Record<string, unknown>;
        if (postCount === 1) {
          firstRequest.payload = payload;
          firstRequest.signal = init.signal as AbortSignal;
          return new Promise<ReturnType<typeof response>>((resolve) => { resolveFirst = resolve; });
        }
        retryPayload = payload;
        return Promise.resolve(response({ task: createdTask, idempotent_replay: true }));
      }
      if (url.endsWith(`/api/work-board/tasks/${createdTask.task_id}`)) return Promise.resolve(response(detail(createdTask)));
      return Promise.resolve(response({}));
    });

    const props = { ownerPrincipalId: "operator:pending-create", ownerSessionId: "session:pending-create" };
    const firstMount = render(<WorkBoardPanel {...props} />);
    fireEvent.click(await screen.findByRole("button", { name: "Create task" }));
    fireEvent.change(await screen.findByLabelText("Title"), { target: { value: "Create once" } });
    fireEvent.change(screen.getByLabelText("Goal"), { target: { value: goal.id } });
    fireEvent.click(screen.getByRole("button", { name: "Create in Triage" }));
    await waitFor(() => expect(resolveFirst).not.toBeNull());

    firstMount.unmount();
    expect(firstRequest.signal?.aborted).toBe(true);
    accepted = true;
    await act(async () => { resolveFirst?.(response({ task: createdTask, idempotent_replay: false })); });

    render(<WorkBoardPanel {...props} />);
    const dialog = await screen.findByRole("dialog", { name: "Create a goal-linked task" });
    expect(within(dialog).getByLabelText("Title")).toHaveValue("Create once");
    expect(within(dialog).getByLabelText("Title")).toBeDisabled();
    expect(within(dialog).getByRole("status")).toHaveTextContent(/unconfirmed receipt/i);
    fireEvent.click(within(dialog).getByRole("button", { name: "Retry create and reconcile" }));

    await waitFor(() => expect(postCount).toBe(2));
    expect(retryPayload).toEqual(firstRequest.payload);
    expect(await screen.findByRole("region", { name: "Task details for Create once" })).toBeInTheDocument();
  });

  it("keeps task details modeless and restores focus to the opener when closed", async () => {
    const currentTask = task({ title: "Modeless details" });
    taskResponse(fetchMock, currentTask);
    render(<WorkBoardPanel />);

    const opener = await screen.findByRole("button", { name: "Open task Modeless details" });
    fireEvent.click(opener);
    const panel = await screen.findByRole("region", { name: "Task details for Modeless details" });

    expect(panel).not.toHaveAttribute("aria-modal");
    expect(document.activeElement).toBe(panel);
    expect(opener.closest("[inert]")).toBeNull();

    opener.focus();
    expect(document.activeElement).toBe(opener);

    fireEvent.click(within(panel).getByRole("button", { name: "Close task details" }));
    await waitFor(() => expect(screen.queryByRole("region", { name: "Task details for Modeless details" })).not.toBeInTheDocument());
    expect(document.activeElement).toBe(opener);
  });

  it("traps focus in the create dialog, closes on Escape, and restores focus", async () => {
    taskResponse(fetchMock, task());
    render(<WorkBoardPanel />);
    const opener = await screen.findByRole("button", { name: "Create task" });
    fireEvent.click(opener);
    const dialog = await screen.findByRole("dialog", { name: "Create a goal-linked task" });
    const focusables = Array.from(dialog.querySelectorAll<HTMLElement>("button:not([disabled]), input:not([disabled]):not([type=\"hidden\"]), select:not([disabled]), textarea:not([disabled])"));
    expect(document.activeElement).toBe(focusables[0]);
    const background = opener.closest<HTMLElement>(".cockpit-operator-row");
    expect(background?.inert).toBe(true);

    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    expect(first).toBeDefined();
    expect(last).toBeDefined();
    first?.focus();
    fireEvent.keyDown(document, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(last);
    fireEvent.keyDown(document, { key: "Tab" });
    expect(document.activeElement).toBe(first);
    fireEvent.keyDown(document, { key: "Escape" });

    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Create a goal-linked task" })).not.toBeInTheDocument());
    expect(document.activeElement).toBe(opener);
    expect(background?.inert).toBe(false);
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
    expect(within(screen.getByRole("region", { name: "Task details for Unknown external effect" })).getByText("Blocked: The destination outcome cannot be proven.")).toBeInTheDocument();
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
