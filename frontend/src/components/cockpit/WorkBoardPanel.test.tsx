import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { WorkBoardPanel } from "./WorkBoardPanel";

const task = {
  task_id: "task-1",
  creation_sequence: 1,
  goal_id: "goal-1",
  goal_revision: 3,
  title: "Ship snapshot",
  body: "Create one bounded artifact",
  capability_id: null,
  typed_input_ref: null,
  typed_input_digest: null,
  executor_id: null,
  assignee_id: "operator-1",
  priority: 75,
  status: "triage" as const,
  requires_review: false,
  reviewer_id: null,
  revision: 1,
  created_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
  dependency_count: 0,
  completed_dependency_count: 0,
  latest_attempt: null,
};

const detail = {
  ...task,
  attempts: [],
  parents: [],
  children: [],
  comments: [],
  events: [],
  artifact_refs: [],
  result_refs: [],
};

const executionLimits = {
  goal_id: "goal-1",
  goal_revision: 3,
  effective_max_runtime_seconds: 300,
  default_max_runtime_seconds: 300,
  hard_max_runtime_seconds: 900,
  attempt_limit: 2,
  limit_source: "goal_budget",
};

function response(payload: unknown, ok = true, status = ok ? 200 : 500) {
  return { ok, status, json: async () => payload };
}

class MockBoardSocket {
  static instances: MockBoardSocket[] = [];
  url: string;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;

  constructor(url: string) {
    this.url = url;
    MockBoardSocket.instances.push(this);
    queueMicrotask(() => this.onopen?.());
  }

  close() {
    this.onclose?.();
  }
}

describe("WorkBoardPanel", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("WebSocket", MockBoardSocket);
    MockBoardSocket.instances = [];
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/work-board/tasks/task-1") && (!init?.method || init.method === "GET")) {
        return Promise.resolve(response({ task: detail }));
      }
      if (url.includes("/api/work-board/goals/goal-1/execution-limits")) {
        return Promise.resolve(response(executionLimits));
      }
      if (url.includes("/api/work-board/tasks?") || url.endsWith("/api/work-board/tasks")) {
        return Promise.resolve(response({ tasks: [task], last_event_id: 7 }));
      }
      return Promise.resolve(response({ task: detail }));
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("renders the fixed Kanban columns and a canonical task card", async () => {
    render(<WorkBoardPanel />);

    expect(await screen.findByRole("heading", { name: "Triage" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Running" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Done" })).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: /Open task task-1/i })).toBeInTheDocument();
    expect(screen.getByText(/assignee operator-1 · executor unassigned/)).toBeInTheDocument();
  });

  it("appends the next task page while preserving active filters and query", async () => {
    const laterTask = { ...task, task_id: "task-2", title: "Later snapshot" };
    const listRequests: string[] = [];
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/work-board/tasks?")) {
        listRequests.push(url);
        if (url.includes("after=7")) {
          return Promise.resolve(response({ tasks: [laterTask], next_after: null, last_event_id: 8 }));
        }
        return Promise.resolve(response({ tasks: [task], next_after: 7, last_event_id: 7 }));
      }
      return Promise.resolve(response({}));
    });

    fetchMock.mockClear();
    render(<WorkBoardPanel />);
    fireEvent.change(await screen.findByLabelText("Search work board tasks"), { target: { value: "snapshot" } });
    fireEvent.change(screen.getByLabelText("Filter work board tasks by status"), { target: { value: "triage" } });
    fireEvent.change(screen.getByLabelText("Filter work board tasks by assignee"), { target: { value: "assignee-1" } });

    const loadMore = await screen.findByRole("button", { name: "Load more tasks" });
    fireEvent.click(loadMore);
    expect(await screen.findByRole("button", { name: /Open task task-2/i })).toBeInTheDocument();

    const laterRequest = listRequests.find((url) => url.includes("after=7"));
    expect(laterRequest).toContain("status=triage");
    expect(laterRequest).toContain("assignee_id=assignee-1");
    expect(laterRequest).toContain("q=snapshot");
  });

  it("keeps the page-one event cursor when later SQLite pages are loaded", async () => {
    const laterTask = { ...task, task_id: "task-2", title: "Later snapshot" };
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/work-board/tasks?")) {
        if (url.includes("after=7")) {
          return Promise.resolve(response({ tasks: [laterTask], next_after: null, last_event_id: 99 }));
        }
        return Promise.resolve(response({ tasks: [task], next_after: 7, last_event_id: 7 }));
      }
      return Promise.resolve(response({}));
    });

    fetchMock.mockClear();
    render(<WorkBoardPanel />);
    await waitFor(() => expect(MockBoardSocket.instances.length).toBeGreaterThan(0));
    const first = MockBoardSocket.instances[0];
    fireEvent.click(await screen.findByRole("button", { name: "Load more tasks" }));
    expect(await screen.findByRole("button", { name: /Open task task-2/i })).toBeInTheDocument();
    expect(first.url).toContain("after=7");

    vi.useFakeTimers();
    act(() => first.onclose?.());
    await act(async () => {
      vi.advanceTimersByTime(1500);
      for (let index = 0; index < 8; index += 1) await Promise.resolve();
    });

    const resumed = MockBoardSocket.instances[MockBoardSocket.instances.length - 1];
    expect(resumed?.url).toContain("after=7");
    expect(resumed?.url).not.toContain("after=99");
  });

  it("refreshes the selected detail and retains loaded pages after an authenticated event", async () => {
    const laterTask = { ...task, task_id: "task-2", title: "Later snapshot" };
    const listRequests: string[] = [];
    const detailRequests: string[] = [];
    let detailVersion = 1;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/work-board/tasks/task-1") && (!init?.method || init.method === "GET")) {
        detailRequests.push(url);
        return Promise.resolve(response({
          task: { ...task, title: `Ship snapshot v${detailVersion}`, task_revision: detailVersion },
          attempts: [{
            attempt_id: `attempt-${detailVersion}`,
            task_id: "task-1",
            workflow_run_id: `run-${detailVersion}`,
            task_revision_at_claim: detailVersion,
            outcome: detailVersion === 1 ? "running" : "succeeded",
            readback_status: detailVersion === 1 ? "pending" : "passed",
          }],
          parents: [],
          children: [],
          comments: [{
            comment_id: `comment-${detailVersion}`,
            task_id: "task-1",
            body: `detail refresh ${detailVersion}`,
            created_at: new Date().toISOString(),
          }],
          events: [{
            event_id: detailVersion,
            task_id: "task-1",
            kind: detailVersion === 1 ? "attempt_started" : "attempt_verified",
            created_at: new Date().toISOString(),
          }],
          revision: detailVersion,
        }));
      }
      if (url.includes("/api/work-board/tasks?")) {
        listRequests.push(url);
        if (url.includes("after=7")) {
          return Promise.resolve(response({ tasks: [laterTask], next_after: null, last_event_id: 8 }));
        }
        return Promise.resolve(response({ tasks: [task], next_after: 7, last_event_id: 7 }));
      }
      return Promise.resolve(response({}));
    });

    fetchMock.mockClear();
    render(<WorkBoardPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "Load more tasks" }));
    expect(await screen.findByRole("button", { name: /Open task task-2/i })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Open task task-1/i }));
    expect(await screen.findByRole("heading", { name: "Ship snapshot v1" })).toBeInTheDocument();
    await waitFor(() => expect(MockBoardSocket.instances.length).toBeGreaterThan(0));

    detailVersion = 2;
    await act(async () => {
      MockBoardSocket.instances[0].onmessage?.({ data: JSON.stringify({ event_id: 9, type: "task.updated", task_id: "task-1" }) });
      await Promise.resolve();
    });

    expect(await screen.findByRole("heading", { name: "Ship snapshot v2" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Open task task-2/i })).toBeInTheDocument();
    expect(screen.getByText("detail refresh 2")).toBeInTheDocument();
    expect(screen.getByText("attempt_verified")).toBeInTheDocument();
    await waitFor(() => expect(detailRequests.length).toBeGreaterThan(1));
    await waitFor(() => expect(listRequests.filter((url) => url.includes("after=7")).length).toBeGreaterThan(1));
  });

  it("preserves unsaved edit and comment drafts while a live event refreshes server detail", async () => {
    let detailVersion = 1;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/work-board/tasks/task-1") && (!init?.method || init.method === "GET")) {
        return Promise.resolve(response({
          task: {
            ...task,
            title: detailVersion === 1 ? "Ship snapshot" : "Server refreshed title",
            task_revision: detailVersion,
          },
          attempts: [],
          parents: [],
          children: [],
          comments: [{
            comment_id: `server-comment-${detailVersion}`,
            task_id: "task-1",
            body: detailVersion === 1 ? "initial server comment" : "server detail update",
            created_at: new Date().toISOString(),
          }],
          events: [{
            event_id: detailVersion,
            task_id: "task-1",
            kind: detailVersion === 1 ? "task_opened" : "task_updated",
            created_at: new Date().toISOString(),
          }],
          revision: detailVersion,
        }));
      }
      if (url.includes("/api/work-board/tasks?")) {
        return Promise.resolve(response({ tasks: [task], last_event_id: 7 }));
      }
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /Open task task-1/i }));
    expect(await screen.findByRole("heading", { name: "Ship snapshot" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Local title draft" } });
    fireEvent.change(screen.getByLabelText("Add comment"), { target: { value: "Local comment draft" } });

    await waitFor(() => expect(MockBoardSocket.instances.length).toBeGreaterThan(0));
    detailVersion = 2;
    await act(async () => {
      MockBoardSocket.instances[0].onmessage?.({ data: JSON.stringify({ event_id: 8, type: "task.updated", task_id: "task-1" }) });
      await Promise.resolve();
    });

    expect(await screen.findByRole("heading", { name: "Server refreshed title" })).toBeInTheDocument();
    expect(screen.getByLabelText("Title")).toHaveValue("Local title draft");
    expect(screen.getByLabelText("Add comment")).toHaveValue("Local comment draft");
    expect(screen.getByText("server detail update")).toBeInTheDocument();
    expect(screen.getByText("task_updated")).toBeInTheDocument();
  });

  it("resets unsaved edit and comment drafts when explicitly opening another task", async () => {
    const secondTask = {
      ...task,
      task_id: "task-2",
      title: "Review second task",
      body: "Second task body",
      priority: 25,
      assignee_id: "operator-2",
    };
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/work-board/tasks/task-1") && (!init?.method || init.method === "GET")) {
        return Promise.resolve(response({ task, attempts: [], parents: [], children: [], comments: [], events: [], revision: 1 }));
      }
      if (url.includes("/api/work-board/tasks/task-2") && (!init?.method || init.method === "GET")) {
        return Promise.resolve(response({ task: secondTask, attempts: [], parents: [], children: [], comments: [], events: [], revision: 1 }));
      }
      if (url.includes("/api/work-board/tasks?")) return Promise.resolve(response({ tasks: [task, secondTask], last_event_id: 7 }));
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /Open task task-1/i }));
    expect(await screen.findByRole("heading", { name: "Ship snapshot" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Unsaved first title" } });
    fireEvent.change(screen.getByLabelText("Add comment"), { target: { value: "Unsaved first comment" } });

    fireEvent.click(screen.getByRole("button", { name: /Open task task-2/i }));
    expect(await screen.findByRole("heading", { name: "Review second task" })).toBeInTheDocument();
    expect(screen.getByLabelText("Title")).toHaveValue("Review second task");
    expect(screen.getByLabelText("Body")).toHaveValue("Second task body");
    expect(screen.getByLabelText("Add comment")).toHaveValue("");
  });

  it("does not let a late detail response replace the task opened most recently", async () => {
    const secondTask = { ...task, task_id: "task-2", title: "Second task" };
    let resolveFirst: ((value: ReturnType<typeof response>) => void) | undefined;
    let resolveSecond: ((value: ReturnType<typeof response>) => void) | undefined;
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/work-board/tasks/task-1")) {
        return new Promise((resolve) => { resolveFirst = resolve; });
      }
      if (url.includes("/api/work-board/tasks/task-2")) {
        return new Promise((resolve) => { resolveSecond = resolve; });
      }
      if (url.includes("/api/work-board/tasks?")) return Promise.resolve(response({ tasks: [task, secondTask], last_event_id: 7 }));
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    const firstCard = await screen.findByRole("button", { name: /Open task task-1/i });
    const secondCard = screen.getByRole("button", { name: /Open task task-2/i });
    fireEvent.click(firstCard);
    fireEvent.click(secondCard);

    await act(async () => {
      resolveSecond?.(response({ task: secondTask, attempts: [], parents: [], children: [], comments: [], events: [], revision: 1 }));
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(await screen.findByRole("heading", { name: "Second task" })).toBeInTheDocument();

    await act(async () => {
      resolveFirst?.(response({ task, attempts: [], parents: [], children: [], comments: [], events: [], revision: 1 }));
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByRole("heading", { name: "Second task" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Ship snapshot" })).not.toBeInTheDocument();
  });

  it("rebases the edit draft after successful save and comment refreshes", async () => {
    let serverTask = { ...task, title: "Original server title", body: "Original server body", revision: 1, task_revision: 1 };
    let serverComments: Array<{ comment_id: string; task_id: string; body: string; created_at: string }> = [];
    let nextRevision = 1;
    const detailPayload = () => ({
      task: serverTask,
      attempts: [],
      parents: [],
      children: [],
      comments: serverComments,
      events: [{
        event_id: nextRevision,
        task_id: "task-1",
        kind: nextRevision >= 3 ? "comment_added" : "task_saved",
        created_at: new Date().toISOString(),
      }],
      revision: nextRevision,
    });
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/work-board/tasks/task-1") && init?.method === "PATCH") {
        nextRevision = 2;
        serverTask = {
          ...serverTask,
          title: "Canonical saved title",
          body: "Canonical saved body",
          revision: nextRevision,
          task_revision: nextRevision,
        };
        return Promise.resolve(response({ task: serverTask }));
      }
      if (url.endsWith("/comments") && init?.method === "POST") {
        const body = JSON.parse(String(init.body)) as { body: string };
        nextRevision = 3;
        serverTask = {
          ...serverTask,
          title: "Canonical after comment",
          revision: nextRevision,
          task_revision: nextRevision,
        };
        serverComments = [{
          comment_id: "comment-saved",
          task_id: "task-1",
          body: body.body,
          created_at: new Date().toISOString(),
        }];
        return Promise.resolve(response({ comment: serverComments[0] }));
      }
      if (url.includes("/api/work-board/tasks/task-1") && (!init?.method || init.method === "GET")) {
        return Promise.resolve(response(detailPayload()));
      }
      if (url.includes("/api/work-board/tasks?")) return Promise.resolve(response({ tasks: [serverTask], last_event_id: nextRevision }));
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /Open task task-1/i }));
    expect(await screen.findByRole("heading", { name: "Original server title" })).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Submitted title draft" } });
    fireEvent.change(screen.getByLabelText("Body"), { target: { value: "Submitted body draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Save bounded fields" }));

    expect(await screen.findByRole("heading", { name: "Canonical saved title" })).toBeInTheDocument();
    expect(screen.getByLabelText("Title")).toHaveValue("Canonical saved title");
    expect(screen.getByLabelText("Body")).toHaveValue("Canonical saved body");
    expect(screen.getByLabelText("Add comment")).toHaveValue("");

    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Unsaved title before comment" } });
    fireEvent.change(screen.getByLabelText("Add comment"), { target: { value: "Submitted comment" } });
    fireEvent.click(screen.getByRole("button", { name: "Comment" }));

    expect(await screen.findByRole("heading", { name: "Canonical after comment" })).toBeInTheDocument();
    expect(screen.getByLabelText("Title")).toHaveValue("Canonical after comment");
    expect(screen.getByLabelText("Add comment")).toHaveValue("");
    expect(screen.getByText("Submitted comment")).toBeInTheDocument();
  });

  it("renders only known artifact/readback routes and derives verified object evidence", async () => {
    const readbackAttempt = {
      attempt_id: "attempt-readback",
      task_id: "task-1",
      workflow_run_id: "run-readback",
      task_revision_at_claim: 1,
      outcome: "succeeded",
      readback_status: null,
      readback_refs: ["/api/observer/screen-artifacts/obs-1/analysis"],
      receipt_refs: [{
        job_id: "job-object",
        workflow_run_id: "run-object",
        status: "succeeded",
        verified: true,
        artifact_id: "artifact-object",
        file_path: "outputs/report.json",
        secret: "must-not-render",
      }],
    };
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/work-board/tasks/task-1")) {
        return Promise.resolve(response({
          task: {
            ...task,
            artifact_refs: [
              "/api/nodes/edge/artifacts/art-1/content",
              "javascript:alert(1)",
              "workspace/output.json",
            ],
            result_refs: ["/api/observer/screen-artifacts/obs-1/provider-output", "https://example.com/readback"],
          },
          attempts: [readbackAttempt],
          readback_status: null,
          readback_refs: [],
          parents: [],
          children: [],
          comments: [],
          events: [],
          revision: 1,
        }));
      }
      if (url.includes("/api/work-board/tasks?")) return Promise.resolve(response({ tasks: [task], last_event_id: 7 }));
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /Open task task-1/i }));

    expect(await screen.findByRole("link", { name: "/api/nodes/edge/artifacts/art-1/content" })).toHaveAttribute("href", expect.stringContaining("/api/nodes/edge/artifacts/art-1/content"));
    expect(screen.getByRole("link", { name: "/api/observer/screen-artifacts/obs-1/provider-output" })).toHaveAttribute("href", expect.stringContaining("/api/observer/screen-artifacts/obs-1/provider-output"));
    expect(screen.getByRole("link", { name: "https://example.com/readback" })).toHaveAttribute("target", "_blank");
    expect(screen.getByText("javascript:alert(1)")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "javascript:alert(1)" })).not.toBeInTheDocument();
    expect(screen.getByText("workspace/output.json")).toBeInTheDocument();
    expect(screen.getByText(/job=job-object/)).toBeInTheDocument();
    expect(screen.getByText(/artifact=artifact-object/)).toBeInTheDocument();
    expect(screen.getByText(/verified=true/)).toBeInTheDocument();
    expect(screen.queryByText(/must-not-render/)).not.toBeInTheDocument();
    expect(screen.getByText("Readback status: passed")).toBeInTheDocument();
    expect(screen.getByText("readback passed")).toBeInTheDocument();
  });

  it("enters, traps, and exits each dialog with keyboard focus restoration", async () => {
    render(<WorkBoardPanel />);
    const createTrigger = await screen.findByRole("button", { name: "Create task" });
    createTrigger.focus();
    fireEvent.click(createTrigger);
    const createDialog = await screen.findByRole("dialog", { name: "Create bounded task" });
    const createTitle = screen.getByLabelText("Title");
    expect(document.activeElement).toBe(createTitle);

    const createFocusable = Array.from(createDialog.querySelectorAll<HTMLElement>("button, input, textarea, select")).filter((element) => !element.hasAttribute("disabled"));
    const lastCreateFocusable = createFocusable[createFocusable.length - 1];
    lastCreateFocusable?.focus();
    fireEvent.keyDown(createDialog, { key: "Tab" });
    expect(document.activeElement).toBe(createFocusable[0]);
    createFocusable[0].focus();
    fireEvent.keyDown(createDialog, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(lastCreateFocusable);
    fireEvent.keyDown(createDialog, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Create bounded task" })).not.toBeInTheDocument());
    expect(document.activeElement).toBe(createTrigger);

    const card = screen.getByRole("button", { name: /Open task task-1/i });
    card.focus();
    fireEvent.click(card);
    const detailDialog = await screen.findByRole("dialog", { name: "Ship snapshot" });
    expect(document.activeElement).toBe(screen.getByLabelText("Title"));
    fireEvent.keyDown(detailDialog, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Ship snapshot" })).not.toBeInTheDocument());
    expect(document.activeElement).toBe(card);
  });

  it("does not issue an illegal drag transition to Running", async () => {
    render(<WorkBoardPanel />);
    const card = await screen.findByRole("button", { name: /Open task task-1/i });
    const running = screen.getByRole("region", { name: "Running tasks" });

    fireEvent.dragStart(card.closest("article")!);
    fireEvent.dragOver(running);
    fireEvent.drop(running);

    await waitFor(() => expect(screen.getAllByText(/not a legal operator transition/i).length).toBeGreaterThan(0));
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === "POST" && String(init.body).includes("promote"))).toBe(false);
  });

  it("does not drag Todo to Ready or Review to Todo through generic promotion", async () => {
    const todoTask = { ...task, task_id: "todo-task", title: "Todo task", status: "todo" as const };
    const reviewTask = { ...task, task_id: "review-task", title: "Review task", status: "review" as const };
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/work-board/tasks?") || url.endsWith("/api/work-board/tasks")) {
        return Promise.resolve(response({ tasks: [todoTask, reviewTask], last_event_id: 7 }));
      }
      return Promise.resolve(response({ task: todoTask }));
    });

    render(<WorkBoardPanel />);
    const todoCard = await screen.findByRole("button", { name: /Open task todo-task/i });
    const ready = screen.getByRole("region", { name: "Ready tasks" });
    fireEvent.dragStart(todoCard.closest("article")!);
    fireEvent.dragOver(ready);
    fireEvent.drop(ready);
    await waitFor(() => expect(screen.getAllByText(/not a legal operator transition/i).length).toBeGreaterThan(0));

    const reviewCard = await screen.findByRole("button", { name: /Open task review-task/i });
    const todo = screen.getByRole("region", { name: "Todo tasks" });
    fireEvent.dragStart(reviewCard.closest("article")!);
    fireEvent.dragOver(todo);
    fireEvent.drop(todo);
    await waitFor(() => expect(screen.getAllByText(/not a legal operator transition/i).length).toBeGreaterThan(1));

    expect(fetchMock.mock.calls.some(([, init]) => init?.method === "POST" && String(init.body).includes("promote"))).toBe(false);
  });

  it("omits freeform finite limits from task creation", async () => {
    let createBody: Record<string, unknown> | null = null;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/work-board/tasks") && init?.method === "POST") {
        createBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        return Promise.resolve(response({}));
      }
      if (url.includes("/api/work-board/goals/goal-1/execution-limits")) return Promise.resolve(response(executionLimits));
      if (url.includes("/api/work-board/tasks?") || url.endsWith("/api/work-board/tasks")) {
        return Promise.resolve(response({ tasks: [task], last_event_id: 7 }));
      }
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "Create task" }));
    expect(screen.queryByLabelText(/Finite limits/i)).not.toBeInTheDocument();
    expect(screen.getByLabelText("Goal revision")).toHaveAttribute("min", "1");
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Create bounded task" } });
    fireEvent.change(screen.getByLabelText("Goal ID"), { target: { value: "goal-1" } });
    fireEvent.change(screen.getByLabelText("Goal revision"), { target: { value: "3" } });
    expect(await screen.findByText(/Effective runtime limit: 300s/)).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText(/I acknowledge these current goal execution limits/));
    fireEvent.click(screen.getByRole("button", { name: /^Create$/ }));

    await waitFor(() => expect(createBody).toEqual(expect.objectContaining({ title: "Create bounded task" })));
    expect(createBody).not.toHaveProperty("finite_limits");
  });

  it("shows archived tasks immediately when Archived is the selected status filter", async () => {
    const archivedTask = { ...task, task_id: "archived-task", title: "Archived result", status: "archived" as const };
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("status=archived")) return Promise.resolve(response({ tasks: [archivedTask], last_event_id: 7 }));
      if (url.includes("/api/work-board/tasks?")) return Promise.resolve(response({ tasks: [task], last_event_id: 7 }));
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    fireEvent.change(screen.getByLabelText("Filter work board tasks by status"), { target: { value: "archived" } });
    expect(await screen.findByRole("button", { name: /Open task archived-task/i })).toBeInTheDocument();
    expect(screen.getByLabelText("Include archived")).toBeChecked();
  });

  it("captures an optional dependency parent during task creation through the link contract", async () => {
    let createBody: Record<string, unknown> | null = null;
    let linkBody: Record<string, unknown> | null = null;
    const createdTask = { ...task, task_id: "created-task", revision: 4, task_revision: 4 };
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/work-board/tasks") && init?.method === "POST") {
        createBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        return Promise.resolve(response({ task: createdTask }));
      }
      if (url.includes("/api/work-board/goals/goal-1/execution-limits")) return Promise.resolve(response(executionLimits));
      if (url.endsWith("/api/work-board/links") && init?.method === "POST") {
        linkBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        return Promise.resolve(response({ link: linkBody }));
      }
      if (url.includes("/api/work-board/tasks?")) return Promise.resolve(response({ tasks: [task], last_event_id: 7 }));
      if (url.includes("/api/work-board/tasks/created-task")) return Promise.resolve(response({ task: createdTask, attempts: [], parents: ["parent-task"], children: [], comments: [], events: [], revision: 4 }));
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "Create task" }));
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Create with parent" } });
    fireEvent.change(screen.getByLabelText("Goal ID"), { target: { value: "goal-1" } });
    fireEvent.change(screen.getByLabelText("Goal revision"), { target: { value: "3" } });
    fireEvent.change(screen.getByLabelText("Dependency parent task ID"), { target: { value: "parent-task" } });
    expect(await screen.findByText(/Effective runtime limit: 300s/)).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText(/I acknowledge these current goal execution limits/));
    fireEvent.click(screen.getByRole("button", { name: /^Create$/ }));

    await waitFor(() => expect(createBody).toEqual(expect.objectContaining({ title: "Create with parent" })));
    expect(createBody).not.toHaveProperty("parent_task_id");
    expect(linkBody).toEqual({ parent_task_id: "parent-task", child_task_id: "created-task", expected_child_revision: 4 });
  });

  it("blocks creation when the goal execution-limits response is for a stale revision", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/work-board/goals/goal-1/execution-limits")) {
        return Promise.resolve(response({ ...executionLimits, goal_revision: 2 }));
      }
      if (url.includes("/api/work-board/tasks?")) return Promise.resolve(response({ tasks: [task], last_event_id: 7 }));
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    fireEvent.click(await screen.findByRole("button", { name: "Create task" }));
    fireEvent.change(screen.getByLabelText("Goal ID"), { target: { value: "goal-1" } });
    fireEvent.change(screen.getByLabelText("Goal revision"), { target: { value: "3" } });
    expect(await screen.findByText(/could not be verified/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^Create$/ })).toBeDisabled();
    expect(screen.queryByLabelText(/I acknowledge these current goal execution limits/)).not.toBeInTheDocument();
  });

  it("keeps Ready promotion blocked when detail limits are for a stale goal revision", async () => {
    const typedTask = { ...task, typed_input_ref: "artifact://spec", typed_input_digest: "a".repeat(64) };
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/work-board/goals/goal-1/execution-limits")) return Promise.resolve(response({ ...executionLimits, goal_revision: 2 }));
      if (url.includes("/api/work-board/tasks/task-1")) return Promise.resolve(response({ task: typedTask, attempts: [], parents: [], children: [], comments: [], events: [], revision: 1 }));
      if (url.includes("/api/work-board/tasks?")) return Promise.resolve(response({ tasks: [typedTask], last_event_id: 7 }));
      return Promise.resolve(response({}));
    });

    fetchMock.mockClear();
    render(<WorkBoardPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /Open task task-1/i }));
    const moveToTodo = await screen.findByRole("button", { name: "Move to Todo" });
    expect(await screen.findByText(/Ready admission remains blocked/)).toBeInTheDocument();
    expect(moveToTodo).toBeDisabled();
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
  });

  it("shows review evidence and same-card reviewer controls", async () => {
    const reviewTask = {
      ...task,
      status: "review" as const,
      requires_review: true,
      reviewer_id: "reviewer-1",
      executor_id: "worker-1",
      latest_attempt: {
        attempt_id: "attempt-review",
        task_id: "task-1",
        task_revision_at_claim: 1,
        executor_id: "worker-1",
        outcome: "succeeded",
        readback_status: null,
        receipt_refs: [{
          job_id: "job-review",
          workflow_run_id: "run-review",
          status: "succeeded",
          verified: true,
        }],
      },
    };
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/work-board/tasks?")) return Promise.resolve(response({ tasks: [reviewTask], last_event_id: 7 }));
      if (url.includes("/api/work-board/tasks/task-1")) return Promise.resolve(response({ task: reviewTask, attempts: [reviewTask.latest_attempt], parents: [], children: [], comments: [], events: [], revision: 1 }));
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    const card = await screen.findByRole("button", { name: /Open task task-1/i });
    expect(screen.getByText(/reviewer reviewer-1 · evidence passed/)).toBeInTheDocument();
    fireEvent.click(card);
    expect(await screen.findByText("review evidence: passed")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Request changes" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Complete review" })).not.toBeDisabled();
  });

  it("requests review with verified attempt evidence and no client prose", async () => {
    const runningTask = {
      ...task,
      task_id: "running-review-task",
      title: "Running review candidate",
      status: "running" as const,
      executor_id: "worker-1",
      reviewer_id: "reviewer-1",
      revision: 2,
      latest_attempt: {
        attempt_id: "attempt-running-review",
        task_id: "running-review-task",
        workflow_run_id: "run-running-review",
        task_revision_at_claim: 2,
        executor_id: "worker-1",
        outcome: "succeeded",
        readback_status: "passed",
        receipt_refs: [{ artifact_id: "artifact-review", effect_id: "effect-review" }],
      },
    };
    let actionBody: Record<string, unknown> | null = null;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/actions") && init?.method === "POST") {
        actionBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        return Promise.resolve(response({ task: { ...runningTask, status: "review", revision: 3 } }));
      }
      if (url.includes("/api/work-board/goals/goal-1/execution-limits")) return Promise.resolve(response(executionLimits));
      if (url.includes("/api/work-board/tasks/running-review-task")) {
        return Promise.resolve(response({ task: runningTask, attempts: [runningTask.latest_attempt], parents: [], children: [], comments: [], events: [], revision: 2 }));
      }
      if (url.includes("/api/work-board/tasks?")) return Promise.resolve(response({ tasks: [runningTask], last_event_id: 7 }));
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /Open task running-review-task/i }));
    fireEvent.click(await screen.findByRole("button", { name: "Request review" }));

    await waitFor(() => expect(actionBody).toEqual({
      action: "request_review",
      expected_revision: 2,
      attempt_id: "attempt-running-review",
      evidence_refs: ["artifact-review", "effect-review"],
    }));
  });

  it("keeps review request unavailable without named reviewer and verified evidence", async () => {
    const incompleteTask = {
      ...task,
      task_id: "incomplete-review-task",
      title: "Incomplete review candidate",
      status: "running" as const,
      executor_id: "worker-1",
      reviewer_id: null,
      latest_attempt: {
        attempt_id: "attempt-incomplete-review",
        task_id: "incomplete-review-task",
        task_revision_at_claim: 1,
        outcome: "running",
      },
    };
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/work-board/tasks/incomplete-review-task")) {
        return Promise.resolve(response({ task: incompleteTask, attempts: [incompleteTask.latest_attempt], parents: [], children: [], comments: [], events: [], revision: 1 }));
      }
      if (url.includes("/api/work-board/tasks?")) return Promise.resolve(response({ tasks: [incompleteTask], last_event_id: 7 }));
      return Promise.resolve(response({}));
    });

    fetchMock.mockClear();
    render(<WorkBoardPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /Open task incomplete-review-task/i }));
    const requestButton = await screen.findByRole("button", { name: "Request review" });
    expect(requestButton).toBeDisabled();
    expect(screen.getByText("Assign a named reviewer before requesting review.")).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
  });

  it("requests bounded changes with the current review revision", async () => {
    const reviewTask = {
      ...task,
      task_id: "changes-review-task",
      title: "Review requiring changes",
      status: "review" as const,
      requires_review: true,
      reviewer_id: "reviewer-2",
      executor_id: "worker-1",
      revision: 5,
      latest_attempt: {
        attempt_id: "attempt-changes",
        task_id: "changes-review-task",
        workflow_run_id: "run-changes",
        task_revision_at_claim: 5,
        executor_id: "worker-1",
        outcome: "succeeded",
      },
    };
    let actionBody: Record<string, unknown> | null = null;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/actions") && init?.method === "POST") {
        actionBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        return Promise.resolve(response({ task: reviewTask }));
      }
      if (url.includes("/api/work-board/goals/goal-1/execution-limits")) return Promise.resolve(response(executionLimits));
      if (url.includes("/api/work-board/tasks/changes-review-task")) return Promise.resolve(response({ task: reviewTask, attempts: [reviewTask.latest_attempt], parents: [], children: [], comments: [], events: [], revision: 5 }));
      if (url.includes("/api/work-board/tasks?")) return Promise.resolve(response({ tasks: [reviewTask], last_event_id: 7 }));
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /Open task changes-review-task/i }));
    const changes = await screen.findByLabelText("Required changes");
    expect(screen.getByRole("button", { name: "Request changes" })).toBeDisabled();
    fireEvent.change(changes, { target: { value: "Re-run the readback with the approved source." } });
    fireEvent.click(screen.getByRole("button", { name: "Request changes" }));

    await waitFor(() => expect(actionBody).toEqual({
      action: "request_changes",
      expected_revision: 5,
      reason: "Re-run the readback with the approved source.",
    }));
  });

  it("completes review with a separate named reviewer and omits client evidence", async () => {
    const reviewTask = {
      ...task,
      task_id: "complete-review-task",
      title: "Verified review candidate",
      status: "review" as const,
      requires_review: true,
      reviewer_id: "reviewer-2",
      executor_id: "worker-1",
      revision: 6,
      latest_attempt: {
        attempt_id: "attempt-complete",
        task_id: "complete-review-task",
        workflow_run_id: "run-complete",
        task_revision_at_claim: 6,
        executor_id: "worker-1",
        outcome: "succeeded",
        readback_status: "passed",
      },
    };
    let actionBody: Record<string, unknown> | null = null;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/actions") && init?.method === "POST") {
        actionBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        return Promise.resolve(response({ task: { ...reviewTask, status: "done", revision: 7 } }));
      }
      if (url.includes("/api/work-board/goals/goal-1/execution-limits")) return Promise.resolve(response(executionLimits));
      if (url.includes("/api/work-board/tasks/complete-review-task")) return Promise.resolve(response({ task: reviewTask, attempts: [reviewTask.latest_attempt], parents: [], children: [], comments: [], events: [], revision: 6 }));
      if (url.includes("/api/work-board/tasks?")) return Promise.resolve(response({ tasks: [reviewTask], last_event_id: 7 }));
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /Open task complete-review-task/i }));
    await screen.findByRole("dialog", { name: "Verified review candidate" });
    expect(screen.getByText("Linked workflow run: run-complete")).toBeInTheDocument();
    expect(screen.getByText("Readback evidence: passed")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Complete review" }));

    await waitFor(() => expect(actionBody).toEqual({
      action: "complete_review",
      expected_revision: 6,
      attempt_id: "attempt-complete",
    }));
    expect(actionBody).not.toHaveProperty("evidence_refs");
  });

  it("blocks self-review completion when reviewer and worker are the same", async () => {
    const selfReviewTask = {
      ...task,
      task_id: "self-review-task",
      title: "Self review blocked",
      status: "review" as const,
      requires_review: true,
      reviewer_id: "worker-1",
      executor_id: "worker-1",
      latest_attempt: {
        attempt_id: "attempt-self-review",
        task_id: "self-review-task",
        task_revision_at_claim: 1,
        executor_id: "worker-1",
        outcome: "succeeded",
      },
    };
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/work-board/tasks/self-review-task")) return Promise.resolve(response({ task: selfReviewTask, attempts: [selfReviewTask.latest_attempt], parents: [], children: [], comments: [], events: [], revision: 1 }));
      if (url.includes("/api/work-board/tasks?")) return Promise.resolve(response({ tasks: [selfReviewTask], last_event_id: 7 }));
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /Open task self-review-task/i }));
    await screen.findByRole("dialog", { name: "Self review blocked" });
    expect(screen.getByRole("button", { name: "Complete review" })).toBeDisabled();
    expect(screen.getByText(/reviewer must be separate from the worker/i)).toBeInTheDocument();
  });

  it("previews a proposal and sends explicit accept and reject revisions", async () => {
    const proposalTask = { ...task, task_id: "proposal-task", title: "Proposal parent", revision: 4 };
    const proposal = {
      kind: "decompose",
      proposal_id: "proposal-1",
      parent_task_id: "proposal-task",
      parent_revision: 4,
      proposal_revision: 2,
      proposal_digest: "digest-1",
      expires_at: "2026-09-24T12:00:00Z",
      proposed_tasks: [{
        task_id: "child-1",
        title: "Research child",
        dependencies: ["proposal-task"],
        capability_id: "research.read",
        executor_id: "executor-1",
        authority: "goal:read",
        cost_estimate: "0.10",
      }],
      proposed_links: [{ parent_task_id: "proposal-task", child_task_id: "child-1" }],
      estimated_cost: "0.10",
    };
    let acceptBody: Record<string, unknown> | null = null;
    let rejectBody: Record<string, unknown> | null = null;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/decompose") && init?.method === "POST") return Promise.resolve(response(proposal));
      if (url.endsWith("/proposals/proposal-1/accept") && init?.method === "POST") {
        acceptBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        return Promise.resolve(response({ ok: true }));
      }
      if (url.endsWith("/proposals/proposal-1/reject") && init?.method === "POST") {
        rejectBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        return Promise.resolve(response({ ok: true }));
      }
      if (url.includes("/api/work-board/goals/goal-1/execution-limits")) return Promise.resolve(response(executionLimits));
      if (url.includes("/api/work-board/tasks/proposal-task")) return Promise.resolve(response({ task: proposalTask, attempts: [], parents: [], children: [], comments: [], events: [], revision: 4 }));
      if (url.includes("/api/work-board/tasks?")) return Promise.resolve(response({ tasks: [proposalTask], last_event_id: 7 }));
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /Open task proposal-task/i }));
    await screen.findByRole("dialog", { name: "Proposal parent" });
    fireEvent.click(await screen.findByRole("button", { name: "Decompose proposal" }));
    expect(await screen.findByRole("heading", { name: "decompose proposal preview" })).toBeInTheDocument();
    expect(screen.getByText("Research child")).toBeInTheDocument();
    expect(screen.getByText(/capability: research\.read · executor: executor-1/)).toBeInTheDocument();
    expect(screen.getByText(/authority: goal:read · cost: 0\.10/)).toBeInTheDocument();
    expect(screen.getByText(/Proposed links: proposal-task → child-1/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Accept proposal" }));
    await waitFor(() => expect(acceptBody).toEqual({
      expected_proposal_revision: 2,
      expected_parent_revision: 4,
    }));

    fireEvent.click(await screen.findByRole("button", { name: "Decompose proposal" }));
    expect(await screen.findByRole("button", { name: "Reject proposal" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Reject proposal" }));
    await waitFor(() => expect(rejectBody).toEqual({ expected_proposal_revision: 2 }));
  });

  it("keeps proposal actions unavailable when proposal_revision is absent", async () => {
    const proposalTask = { ...task, task_id: "pending-proposal-task", title: "Pending proposal", revision: 1 };
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/specify") && init?.method === "POST") {
        return Promise.resolve(response({
          kind: "specify",
          proposal_id: "proposal-pending",
          parent_task_id: "pending-proposal-task",
          parent_revision: 1,
          proposal_digest: "digest-pending",
          expires_at: "2026-09-24T12:00:00Z",
          proposed_tasks: [],
          proposed_links: [],
          estimated_cost: null,
        }));
      }
      if (url.includes("/api/work-board/tasks/pending-proposal-task")) return Promise.resolve(response({ task: proposalTask, attempts: [], parents: [], children: [], comments: [], events: [], revision: 1 }));
      if (url.includes("/api/work-board/tasks?")) return Promise.resolve(response({ tasks: [proposalTask], last_event_id: 7 }));
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /Open task pending-proposal-task/i }));
    await screen.findByRole("dialog", { name: "Pending proposal" });
    fireEvent.click(await screen.findByRole("button", { name: "Specify proposal" }));
    expect(await screen.findByText(/missing proposal_revision/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Accept proposal" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject proposal" })).toBeDisabled();
  });

  it("renders only safe structured parent handoff fields", async () => {
    const handoffTask = { ...task, task_id: "handoff-task", title: "Handoff child" };
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/work-board/tasks/handoff-task")) {
        return Promise.resolve(response({
          task: handoffTask,
          parent_handoffs: [{
            parent_task_id: "parent-task",
            child_task_id: "handoff-task",
            status: "verified",
            summary: "Safe structured result",
            artifact_refs: [{ artifact_id: "artifact-safe", secret: "do-not-render" }],
            result_refs: [{ workflow_run_id: "run-safe", status: "succeeded", verified: true }],
          }],
          attempts: [],
          parents: [],
          children: [],
          comments: [],
          events: [],
          revision: 1,
        }));
      }
      if (url.includes("/api/work-board/tasks?")) return Promise.resolve(response({ tasks: [handoffTask], last_event_id: 7 }));
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /Open task handoff-task/i }));
    await screen.findByRole("dialog", { name: "Handoff child" });
    expect(await screen.findByText(/Summary: Safe structured result/)).toBeInTheDocument();
    expect(screen.getByText(/artifact=artifact-safe/)).toBeInTheDocument();
    expect(screen.getByText(/run=run-safe/)).toBeInTheDocument();
    expect(screen.queryByText(/do-not-render/)).not.toBeInTheDocument();
  });

  it("renders a server-provided dispatch rank without deriving one in the client", async () => {
    const rankedTask = { ...task, status: "ready" as const, dispatch_rank: 1 };
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/work-board/tasks?")) return Promise.resolve(response({ tasks: [rankedTask], last_event_id: 7 }));
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    expect(await screen.findByText(/rank 1/)).toBeInTheDocument();
  });

  it("offers typed Triage to Todo as a keyboard-operable detail action", async () => {
    const typedTask = {
      ...task,
      typed_input_ref: "artifact://spec",
      typed_input_digest: "a".repeat(64),
    };
    let actionBody: Record<string, unknown> | null = null;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/actions") && init?.method === "POST") {
        actionBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        return Promise.resolve(response({ task: { ...typedTask, status: "todo", revision: 2 } }));
      }
      if (url.includes("/api/work-board/goals/goal-1/execution-limits")) return Promise.resolve(response(executionLimits));
      if (url.includes("/api/work-board/tasks/task-1")) return Promise.resolve(response({ task: typedTask, attempts: [], parents: [], children: [], comments: [], events: [], revision: 1 }));
      if (url.includes("/api/work-board/tasks?")) return Promise.resolve(response({ tasks: [typedTask], last_event_id: 7 }));
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /Open task task-1/i }));
    const moveToTodo = await screen.findByRole("button", { name: "Move to Todo" });
    expect(await screen.findByText(/runtime 300s/)).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText(/I acknowledge the current goal execution limits/));
    moveToTodo.focus();
    await userEvent.setup().keyboard("{Enter}");

    await waitFor(() => expect(actionBody).toEqual(expect.objectContaining({ action: "promote", expected_revision: 1 })));
  });

  it("offers Done to Archived as a keyboard-operable detail action", async () => {
    const doneTask = { ...task, status: "done" as const, revision: 5 };
    let actionBody: Record<string, unknown> | null = null;
    vi.spyOn(window, "confirm").mockReturnValue(true);
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/actions") && init?.method === "POST") {
        actionBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        return Promise.resolve(response({ task: { ...doneTask, status: "archived", revision: 6 } }));
      }
      if (url.includes("/api/work-board/tasks/task-1")) return Promise.resolve(response({ task: doneTask, attempts: [], parents: [], children: [], comments: [], events: [], revision: 5 }));
      if (url.includes("/api/work-board/tasks?")) return Promise.resolve(response({ tasks: [doneTask], last_event_id: 7 }));
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /Open task task-1/i }));
    const archive = await screen.findByRole("button", { name: "Archive" });
    archive.focus();
    await userEvent.setup().keyboard("{Enter}");

    await waitFor(() => expect(actionBody).toEqual(expect.objectContaining({ action: "archive", expected_revision: 5 })));
  });

  it("offers retry only for a server-authorized blocked recovery and sends the current revision", async () => {
    const retryTask = {
      ...task,
      task_id: "retry-task",
      title: "Retry transient work",
      status: "blocked" as const,
      block_kind: "transient" as const,
      block_reason: "temporary capability failure",
      recovery_action: "retry",
      revision: 4,
      latest_attempt: {
        attempt_id: "attempt-retry",
        task_id: "retry-task",
        task_revision_at_claim: 3,
        ended_at: new Date().toISOString(),
        outcome: "failed",
      },
    };
    let actionBody: Record<string, unknown> | null = null;
    let detailCalls = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/actions") && init?.method === "POST") {
        actionBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        return Promise.resolve(response({ task: { ...retryTask, status: "todo", revision: 5 } }));
      }
      if (url.includes("/api/work-board/goals/goal-1/execution-limits")) return Promise.resolve(response(executionLimits));
      if (url.includes("/api/work-board/tasks/retry-task")) {
        detailCalls += 1;
        return Promise.resolve(response({
          task: retryTask,
          attempts: [retryTask.latest_attempt],
          parents: [],
          children: [],
          comments: [],
          events: [],
          revision: 4,
        }));
      }
      if (url.includes("/api/work-board/tasks?")) return Promise.resolve(response({ tasks: [retryTask], last_event_id: 7 }));
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /Open task retry-task/i }));
    const retry = await screen.findByRole("button", { name: "Retry with fresh attempt" });
    expect(retry).not.toBeDisabled();
    fireEvent.click(retry);

    await waitFor(() => expect(actionBody).toEqual({ action: "retry", expected_revision: 4 }));
    expect(actionBody).not.toHaveProperty("job_id");
    await waitFor(() => expect(detailCalls).toBeGreaterThan(1));
  });

  it("offers cancellation only for a server-authorized running recovery and never sends a job ID", async () => {
    const runningTask = {
      ...task,
      task_id: "running-task",
      title: "Running durable work",
      status: "running" as const,
      recovery_action: "cancel",
      revision: 7,
      latest_attempt: {
        attempt_id: "attempt-running",
        task_id: "running-task",
        workflow_run_id: "run-secret",
        task_revision_at_claim: 7,
        outcome: "running",
      },
    };
    let actionBody: Record<string, unknown> | null = null;
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/actions") && init?.method === "POST") {
        actionBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        return Promise.resolve(response({ task: { ...runningTask, status: "blocked", revision: 8 } }));
      }
      if (url.includes("/api/work-board/goals/goal-1/execution-limits")) return Promise.resolve(response(executionLimits));
      if (url.includes("/api/work-board/tasks/running-task")) {
        return Promise.resolve(response({
          task: runningTask,
          attempts: [runningTask.latest_attempt],
          parents: [],
          children: [],
          comments: [],
          events: [],
          revision: 7,
        }));
      }
      if (url.includes("/api/work-board/tasks?")) return Promise.resolve(response({ tasks: [runningTask], last_event_id: 7 }));
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /Open task running-task/i }));
    fireEvent.click(await screen.findByRole("button", { name: "Cancel durable job" }));

    expect(confirmSpy).toHaveBeenCalledWith("Cancel durable execution for task running-task?");
    await waitFor(() => expect(actionBody).toEqual({ action: "cancel", expected_revision: 7 }));
    expect(actionBody).not.toHaveProperty("job_id");
    expect(JSON.stringify(actionBody)).not.toContain("run-secret");
  });

  it("shows a server rejection and refreshes after an authorized recovery action fails", async () => {
    const retryTask = {
      ...task,
      task_id: "stale-retry-task",
      title: "Stale retry",
      status: "blocked" as const,
      block_kind: "transient" as const,
      recovery_action: "retry",
      revision: 6,
    };
    let detailCalls = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/actions") && init?.method === "POST") {
        return Promise.resolve(response({ detail: "stale task revision; refresh before retry" }, false, 409));
      }
      if (url.includes("/api/work-board/goals/goal-1/execution-limits")) return Promise.resolve(response(executionLimits));
      if (url.includes("/api/work-board/tasks/stale-retry-task")) {
        detailCalls += 1;
        return Promise.resolve(response({ task: retryTask, attempts: [], parents: [], children: [], comments: [], events: [], revision: 6 }));
      }
      if (url.includes("/api/work-board/tasks?")) return Promise.resolve(response({ tasks: [retryTask], last_event_id: 7 }));
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /Open task stale-retry-task/i }));
    fireEvent.click(await screen.findByRole("button", { name: "Retry with fresh attempt" }));

    expect((await screen.findAllByText("stale task revision; refresh before retry")).length).toBeGreaterThan(0);
    await waitFor(() => expect(detailCalls).toBeGreaterThan(1));
  });

  it("does not render retry or cancellation without the matching server recovery action", async () => {
    const blockedTask = {
      ...task,
      task_id: "unapproved-blocked-task",
      title: "Blocked without recovery",
      status: "blocked" as const,
      block_kind: "capability" as const,
      recovery_action: null,
    };
    const runningTask = {
      ...task,
      task_id: "unapproved-running-task",
      title: "Running without recovery",
      status: "running" as const,
      recovery_action: null,
    };
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/work-board/goals/goal-1/execution-limits")) return Promise.resolve(response(executionLimits));
      if (url.includes("/api/work-board/tasks/unapproved-blocked-task")) return Promise.resolve(response({ task: blockedTask, attempts: [], parents: [], children: [], comments: [], events: [], revision: 1 }));
      if (url.includes("/api/work-board/tasks/unapproved-running-task")) return Promise.resolve(response({ task: runningTask, attempts: [], parents: [], children: [], comments: [], events: [], revision: 1 }));
      if (url.includes("/api/work-board/tasks?")) return Promise.resolve(response({ tasks: [blockedTask, runningTask], last_event_id: 7 }));
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /Open task unapproved-blocked-task/i }));
    expect(await screen.findByRole("heading", { name: "Blocked without recovery" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry with fresh attempt" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Close task detail" }));

    fireEvent.click(await screen.findByRole("button", { name: /Open task unapproved-running-task/i }));
    expect(await screen.findByRole("heading", { name: "Running without recovery" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel durable job" })).not.toBeInTheDocument();
  });

  it("requires a bounded unblock resolution and sends the exact revision-bound payload", async () => {
    const operatorBlockedTask = {
      ...task,
      task_id: "operator-blocked-task",
      title: "Operator-resolved task",
      status: "blocked" as const,
      block_kind: "operator" as const,
      block_source_status: "todo" as const,
      block_reason: "Waiting for operator action",
      recovery_action: "unblock",
      revision: 3,
    };
    let actionBody: Record<string, unknown> | null = null;
    let detailCalls = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/actions") && init?.method === "POST") {
        actionBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        return Promise.resolve(response({ task: { ...operatorBlockedTask, status: "todo", revision: 4 } }));
      }
      if (url.includes("/api/work-board/goals/goal-1/execution-limits")) return Promise.resolve(response(executionLimits));
      if (url.includes("/api/work-board/tasks/operator-blocked-task")) {
        detailCalls += 1;
        return Promise.resolve(response({
          task: operatorBlockedTask,
          attempts: [],
          parents: [],
          children: [],
          comments: [],
          events: [],
          revision: 3,
        }));
      }
      if (url.includes("/api/work-board/tasks?")) return Promise.resolve(response({ tasks: [operatorBlockedTask], last_event_id: 7 }));
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /Open task operator-blocked-task/i }));
    const resolution = await screen.findByLabelText("Resolution statement");
    const unblock = await screen.findByRole("button", { name: "Unblock" });
    expect(resolution).toHaveAttribute("maxLength", "1000");
    expect(unblock).toBeDisabled();

    fireEvent.change(resolution, { target: { value: "   " } });
    expect(unblock).toBeDisabled();
    expect(actionBody).toBeNull();

    fireEvent.change(resolution, { target: { value: "Operator confirmed the dependency is satisfied." } });
    expect(unblock).not.toBeDisabled();
    fireEvent.click(unblock);

    await waitFor(() => expect(actionBody).toEqual({
      action: "unblock",
      expected_revision: 3,
      resolution: "Operator confirmed the dependency is satisfied.",
    }));
    expect(actionBody).not.toHaveProperty("job_id");
    await waitFor(() => expect(detailCalls).toBeGreaterThan(1));
  });

  it("refreshes after a stale revision response instead of retaining optimistic state", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/work-board/tasks/task-1") && init?.method === "PATCH") {
        return Promise.resolve(response({ detail: "stale revision" }, false, 409));
      }
      if (url.includes("/api/work-board/tasks/task-1")) return Promise.resolve(response({ task: detail }));
      if (url.includes("/api/work-board/tasks")) return Promise.resolve(response({ tasks: [task], last_event_id: 7 }));
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /Open task task-1/i }));
    expect(await screen.findByRole("heading", { name: "Ship snapshot" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Save bounded fields" }));

    expect((await screen.findAllByText("stale revision")).length).toBeGreaterThan(0);
    expect(fetchMock.mock.calls.some(([input, init]) => String(input).includes("/api/work-board/tasks/task-1") && init?.method !== "PATCH")).toBe(true);
  });

  it("ignores an older overlapping filter response and keeps the newest cursor", async () => {
    const oldTask = { ...task, task_id: "old-task", title: "Old filtered result" };
    const newTask = { ...task, task_id: "new-task", title: "Newest filtered result" };
    let releaseOld: ((value: unknown) => void) | undefined;
    const oldResponse = new Promise((resolve) => { releaseOld = resolve; });
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("q=old")) return oldResponse.then(() => response({ tasks: [oldTask], last_event_id: 10 }));
      if (url.includes("q=new")) return Promise.resolve(response({ tasks: [newTask], last_event_id: 20 }));
      if (url.includes("/api/work-board/tasks?")) return Promise.resolve(response({ tasks: [task], last_event_id: 7 }));
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    expect(await screen.findByRole("button", { name: /Open task task-1/i })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Search work board tasks"), { target: { value: "old" } });
    await waitFor(() => expect(releaseOld).toBeDefined());
    fireEvent.change(screen.getByLabelText("Search work board tasks"), { target: { value: "new" } });

    expect(await screen.findByRole("button", { name: /Open task new-task/i })).toBeInTheDocument();
    await act(async () => {
      releaseOld?.(undefined);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(screen.queryByRole("button", { name: /Open task old-task/i })).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("#20");
  });

  it("keeps a detail refresh error visible after a successful mutation", async () => {
    let detailCalls = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/work-board/tasks/task-1") && init?.method === "PATCH") {
        return Promise.resolve(response({ task: { ...task, title: "Saved task", revision: 2 } }));
      }
      if (url.includes("/api/work-board/tasks/task-1") && (!init?.method || init.method === "GET")) {
        detailCalls += 1;
        if (detailCalls > 1) return Promise.resolve(response({ detail: "detail refresh unavailable" }, false, 503));
        return Promise.resolve(response({ task: detail, attempts: [], parents: [], children: [], comments: [], events: [], revision: 1 }));
      }
      if (url.includes("/api/work-board/tasks?")) return Promise.resolve(response({ tasks: [task], last_event_id: 7 }));
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /Open task task-1/i }));
    expect(await screen.findByRole("heading", { name: "Ship snapshot" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Save bounded fields" }));

    expect((await screen.findAllByText("detail refresh unavailable")).length).toBeGreaterThan(0);
    expect(screen.getAllByText("detail refresh unavailable")[0]).toBeVisible();
  });

  it("does not drag or unblock a blocked task without a typed operator recovery", async () => {
    const blockedTask = {
      ...task,
      task_id: "blocked-task",
      title: "Blocked external effect",
      status: "blocked" as const,
      block_kind: "operator",
      block_reason: "effect outcome is unknown",
      latest_attempt: {
        attempt_id: "attempt-unknown",
        task_id: "blocked-task",
        task_revision_at_claim: 1,
        outcome: "unknown_effect",
      },
    };
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/work-board/tasks/blocked-task")) {
        return Promise.resolve(response({ task: blockedTask, attempts: [blockedTask.latest_attempt], parents: [], children: [], comments: [], events: [], revision: 1 }));
      }
      if (url.includes("/api/work-board/tasks?")) return Promise.resolve(response({ tasks: [blockedTask], last_event_id: 7 }));
      return Promise.resolve(response({}));
    });

    fetchMock.mockClear();
    render(<WorkBoardPanel />);
    const card = await screen.findByRole("button", { name: /Open task blocked-task/i });
    const todo = screen.getByRole("region", { name: "Todo tasks" });
    fireEvent.dragStart(card.closest("article")!);
    fireEvent.dragOver(todo);
    fireEvent.drop(todo);
    await waitFor(() => expect(screen.getAllByText(/not a legal operator transition/i).length).toBeGreaterThan(0));

    fireEvent.click(card);
    expect(await screen.findByRole("heading", { name: "Blocked external effect" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Unblock" })).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
  });

  it("sends a bounded manual block reason with the current revision", async () => {
    let actionBody: Record<string, unknown> | null = null;
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/actions") && init?.method === "POST") {
        actionBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        return Promise.resolve(response({ task: { ...task, status: "blocked", revision: 2 } }));
      }
      if (url.includes("/api/work-board/tasks/task-1")) return Promise.resolve(response({ task: detail, attempts: [], parents: [], children: [], comments: [], events: [], revision: 1 }));
      if (url.includes("/api/work-board/tasks?")) return Promise.resolve(response({ tasks: [task], last_event_id: 7 }));
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /Open task task-1/i }));
    expect(await screen.findByRole("heading", { name: "Ship snapshot" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Block reason"), { target: { value: "Waiting for operator approval" } });
    fireEvent.click(screen.getByRole("button", { name: "Block task" }));

    expect(confirmSpy).toHaveBeenCalledWith("Block task task-1 with this operator reason?");
    await waitFor(() => expect(actionBody).toEqual(expect.objectContaining({
      action: "block",
      expected_revision: 1,
      block_kind: "operator",
      reason: "Waiting for operator approval",
    })));
  });

  it("normalizes the M1 detail envelope and renders sibling attempt, comment, and event records", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/work-board/tasks/task-1")) {
        return Promise.resolve(response({
          task: { ...task, task_revision: 2 },
          attempts: [{
            attempt_id: "12345678-abcd",
            task_id: "task-1",
            workflow_run_id: "run-1",
            task_revision_at_claim: 2,
            outcome: "succeeded",
            readback_status: "passed",
          }],
          parents: [],
          children: [],
          comments: [{
            comment_id: "comment-1",
            task_id: "task-1",
            author_principal_id: "operator-1",
            body: "handoff is safe",
            created_at: new Date().toISOString(),
          }],
          events: [{
            event_id: 12,
            task_id: "task-1",
            kind: "attempt_verified",
            created_at: new Date().toISOString(),
            metadata: { outcome: "succeeded" },
          }],
          revision: 2,
        }));
      }
      if (url.includes("/api/work-board/tasks?")) {
        return Promise.resolve(response({ tasks: [task], last_event_id: 7 }));
      }
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /Open task task-1/i }));

    expect(await screen.findByRole("heading", { name: "Attempts and readback" })).toBeInTheDocument();
    expect(screen.getByText("12345678")).toBeInTheDocument();
    expect(screen.getByText("handoff is safe")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Safe event timeline" })).toBeInTheDocument();
    expect(screen.getByText("attempt_verified")).toBeInTheDocument();
  });

  it("normalizes M1 string dependency IDs into removable links", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/work-board/tasks/task-1")) {
        return Promise.resolve(response({
          task: { ...task, task_revision: 4 },
          attempts: [],
          parents: ["parent-task"],
          children: ["child-task"],
          comments: [],
          events: [],
          revision: 4,
        }));
      }
      if (url.includes("/api/work-board/tasks?")) {
        return Promise.resolve(response({ tasks: [task], last_event_id: 7 }));
      }
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    fireEvent.click(await screen.findByRole("button", { name: /Open task task-1/i }));

    expect(await screen.findByText("parent-task → task-1")).toBeInTheDocument();
    expect(screen.getByText("task-1 → child-task")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Remove dependency parent-task to task-1" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Remove dependency task-1 to child-task" })).toBeInTheDocument();
  });

  it("takes a fresh snapshot before reconnecting from the last event cursor", async () => {
    render(<WorkBoardPanel />);
    await waitFor(() => expect(MockBoardSocket.instances.length).toBeGreaterThan(0));
    const first = MockBoardSocket.instances[0];
    vi.useFakeTimers();
    act(() => first.onclose?.());

    await act(async () => {
      vi.advanceTimersByTime(1500);
      for (let index = 0; index < 6; index += 1) await Promise.resolve();
    });

    expect(fetchMock.mock.calls.filter(([input]) => String(input).includes("/api/work-board/tasks?")).length).toBeGreaterThan(1);
    expect(MockBoardSocket.instances.some((socket) => socket.url.includes("after=7"))).toBe(true);
  });

  it("does not open a socket when the initial snapshot fails", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/work-board/tasks?")) return Promise.resolve(response({ detail: "snapshot unavailable" }, false, 503));
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => String(input).includes("/api/work-board/tasks?"))).toBe(true));
    expect(MockBoardSocket.instances).toHaveLength(0);
  });

  it("does not resume a socket when the reconnect snapshot fails", async () => {
    render(<WorkBoardPanel />);
    await waitFor(() => expect(MockBoardSocket.instances.length).toBeGreaterThan(0));
    const first = MockBoardSocket.instances[0];
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/work-board/tasks?")) return Promise.resolve(response({ detail: "snapshot unavailable" }, false, 503));
      return Promise.resolve(response({}));
    });

    vi.useFakeTimers();
    act(() => first.onclose?.());
    await act(async () => {
      vi.advanceTimersByTime(1500);
      for (let index = 0; index < 8; index += 1) await Promise.resolve();
    });

    expect(MockBoardSocket.instances).toHaveLength(1);
  });
});
