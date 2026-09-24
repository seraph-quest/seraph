import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  WorkBoardEvent,
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

function boardTask(overrides: Partial<WorkBoardTask> = {}): WorkBoardTask {
  return {
    task_id: "task-1",
    creation_sequence: 1,
    owner_principal_id: "operator:one",
    owner_session_id: "operator-session-1",
    origin_session_id: "operator-session-1",
    origin_thread_id: null,
    goal_id: "goal-1",
    goal_revision: 3,
    title: "Recoverable task",
    body: "A bounded operator task",
    capability_id: "goal-snapshot-to-file",
    typed_input_ref: "workspace-json:inputs/task.json",
    typed_input_digest: "a".repeat(64),
    executor_id: "executor-local",
    assignee_id: "operator:one",
    priority: 50,
    idempotency_scope: "task",
    idempotency_key: "task-1-key",
    scheduled_at: null,
    status: "blocked",
    block_kind: "operator",
    block_reason: "The operator must resolve the recovered specification.",
    block_source_status: "todo",
    cancel_requested_at: null,
    requires_review: false,
    reviewer_id: null,
    dependency_count: 0,
    completed_dependency_count: 0,
    dispatch_rank: null,
    recovery_action: "unblock",
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

function detail(task: WorkBoardTask): WorkBoardTaskDetail {
  return {
    task,
    attempts: [],
    parents: [],
    children: [],
    comments: [],
    events: [],
    revision: task.task_revision,
  };
}

function page(task: WorkBoardTask, lastEventId: number): WorkBoardTaskPage {
  return { tasks: [task], next_after: null, last_event_id: lastEventId };
}

function emptyEvents(lastEventId: number): WorkBoardEventPage {
  return { events: [], last_event_id: lastEventId, gap: false };
}

function taskEvent(eventId: number): WorkBoardEvent {
  return {
    event_id: eventId,
    task_id: "task-1",
    kind: "task.unblock",
    metadata: { status: "todo", task_revision: 4 },
    created_at: "2026-09-23T10:00:01Z",
  };
}

class IntegrationBoardSocket {
  static instances: IntegrationBoardSocket[] = [];

  readonly url: string;
  readyState = 0;
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(url: string) {
    this.url = url;
    IntegrationBoardSocket.instances.push(this);
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

describe("WorkBoardPanel integration", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    IntegrationBoardSocket.instances = [];
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("WebSocket", IntegrationBoardSocket);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("projects an action receipt through the event cursor and recovers a blocked task", async () => {
    const blocked = boardTask();
    const recovered = boardTask({
      status: "todo",
      block_kind: null,
      block_reason: null,
      block_source_status: null,
      recovery_action: null,
      task_revision: 4,
      updated_at: "2026-09-23T10:00:01Z",
    });
    let currentTask = blocked;
    let snapshotCalls = 0;
    const actionBodies: Array<Record<string, unknown>> = [];

    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/work-board/tasks?") && !url.match(/\/tasks\/[^?]+/)) {
        snapshotCalls += 1;
        return Promise.resolve(response(page(currentTask, currentTask === blocked ? 10 : 11)));
      }
      if (url.includes("/api/work-board/events?after=10")) {
        return Promise.resolve(response(emptyEvents(10)));
      }
      if (url.includes("/api/work-board/events?after=11")) {
        return Promise.resolve(response({ events: [taskEvent(12)], last_event_id: 12, gap: false }));
      }
      if (url.includes("/api/work-board/events?after=12")) {
        return Promise.resolve(response(emptyEvents(12)));
      }
      if (url.endsWith("/api/goals/tree")) return Promise.resolve(response([]));
      if (url.includes("/api/work-board/goals/goal-1/execution-limits")) {
        return Promise.resolve(response({
          goal_id: "goal-1",
          goal_revision: 3,
          effective_max_runtime_seconds: 300,
          default_max_runtime_seconds: 300,
          hard_max_runtime_seconds: 900,
          attempt_limit: 2,
          limit_source: "default",
        }));
      }
      if (url.endsWith("/api/work-board/tasks/task-1/actions") && init?.method === "POST") {
        const body = JSON.parse(String(init.body ?? "{}")) as Record<string, unknown>;
        actionBodies.push(body);
        expect(body).toEqual({
          action: "unblock",
          expected_revision: 3,
          resolution: "The specification was checked by the operator.",
        });
        currentTask = recovered;
        return Promise.resolve(response({
          task_id: "task-1",
          status: "todo",
          revision: 4,
          attempt_id: null,
          reason_code: null,
          recovery_action: null,
          event_id: 11,
          task: recovered,
        }));
      }
      if (url.endsWith("/api/work-board/tasks/task-1")) return Promise.resolve(response(detail(currentTask)));
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    await waitFor(() => expect(IntegrationBoardSocket.instances).toHaveLength(1));
    act(() => IntegrationBoardSocket.instances[0]?.open());

    expect(await screen.findByText("Recoverable task")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Open task Recoverable task" }));
    const resolution = await screen.findByLabelText("Resolution");
    fireEvent.change(resolution, { target: { value: "The specification was checked by the operator." } });
    fireEvent.click(screen.getByRole("button", { name: "Unblock after rechecking authority" }));

    await waitFor(() => expect(actionBodies).toHaveLength(1));
    await waitFor(() => expect(snapshotCalls).toBeGreaterThanOrEqual(2));
    await waitFor(() => expect(screen.getByText("Current state: Todo")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Unblock after rechecking authority" })).not.toBeInTheDocument();

    await waitFor(() => expect(IntegrationBoardSocket.instances).toHaveLength(2));
    act(() => IntegrationBoardSocket.instances[1]?.open());
    act(() => IntegrationBoardSocket.instances[1]?.send(taskEvent(12)));
    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => String(input).includes("/api/work-board/events?after=11"))).toBe(true));
    expect(screen.getByText("Current state: Todo")).toBeInTheDocument();
  });
});
