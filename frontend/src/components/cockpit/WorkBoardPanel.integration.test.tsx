import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { WorkBoardPanel } from "./WorkBoardPanel";

function response(payload: unknown, ok = true, status = ok ? 200 : 503) {
  return { ok, status, json: async () => payload };
}

class IntegrationBoardSocket {
  static instances: IntegrationBoardSocket[] = [];
  url: string;
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  onclose: (() => void) | null = null;

  constructor(url: string) {
    this.url = url;
    IntegrationBoardSocket.instances.push(this);
    queueMicrotask(() => this.onopen?.());
  }

  close() {
    this.onclose?.();
  }
}

const limits = {
  goal_id: "goal-integration",
  goal_revision: 1,
  effective_max_runtime_seconds: 300,
  default_max_runtime_seconds: 300,
  hard_max_runtime_seconds: 900,
  attempt_limit: 2,
  limit_source: "goal_budget",
};

describe("WorkBoardPanel managed API/event integration", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("WebSocket", IntegrationBoardSocket);
    IntegrationBoardSocket.instances = [];
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("projects unknown-effect recovery evidence and reconnects after a fresh snapshot", async () => {
    let detailVersion = 1;
    let listCalls = 0;
    const blockedTask = {
      task_id: "integration-task",
      creation_sequence: 1,
      goal_id: "goal-integration",
      goal_revision: 1,
      title: "Reconcile external write",
      body: "Read back the durable effect before recovery.",
      capability_id: "github.issue.write",
      typed_input_ref: "artifact://integration-input",
      typed_input_digest: "a".repeat(64),
      executor_id: "executor-1",
      assignee_id: "operator-1",
      priority: 80,
      status: "blocked" as const,
      block_kind: "unknown_effect",
      block_reason: "effect status cannot be proven",
      recovery_action: "reconcile_external_effect",
      requires_review: true,
      reviewer_id: "reviewer-1",
      task_revision: 3,
      revision: 3,
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      latest_attempt: {
        attempt_id: "integration-attempt",
        task_id: "integration-task",
        workflow_run_id: "integration-run",
        task_revision_at_claim: 3,
        outcome: "unknown_effect",
        receipt_refs: ["receipt:integration"],
        readback_status: "unknown",
      },
    };

    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/work-board/goals/goal-integration/execution-limits")) {
        return Promise.resolve(response(limits));
      }
      if (url.includes("/api/work-board/tasks/integration-task")) {
        return Promise.resolve(response({
          task: {
            ...blockedTask,
            title: detailVersion === 1 ? blockedTask.title : "Recovery evidence updated",
            block_reason: detailVersion === 1 ? blockedTask.block_reason : "operator readback is still required",
          },
          attempts: [blockedTask.latest_attempt],
          parents: [],
          children: [],
          comments: [],
          events: [{
            event_id: detailVersion === 1 ? 7 : 8,
            task_id: blockedTask.task_id,
            kind: detailVersion === 1 ? "attempt_unknown_effect" : "recovery_reviewed",
            created_at: new Date().toISOString(),
          }],
          revision: detailVersion === 1 ? 3 : 4,
        }));
      }
      if (url.includes("/api/work-board/tasks?")) {
        listCalls += 1;
        return Promise.resolve(response({
          tasks: [blockedTask],
          next_after: null,
          last_event_id: listCalls > 2 ? 9 : 7,
        }));
      }
      return Promise.resolve(response({}));
    });

    render(<WorkBoardPanel />);
    const card = await screen.findByRole("button", { name: /Open task integration-task/i });
    fireEvent.click(card);
    expect(await screen.findByRole("heading", { name: "Reconcile external write" })).toBeInTheDocument();
    expect((screen.getAllByText(/unknown_effect: effect status cannot be proven/)).length).toBeGreaterThan(0);
    expect(screen.getByText("receipt:integration")).toBeInTheDocument();
    expect(screen.getByText("readback unknown")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Unblock" })).not.toBeInTheDocument();
    expect(screen.getByText("recovery: reconcile_external_effect")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry with fresh attempt" })).not.toBeInTheDocument();

    await waitFor(() => expect(IntegrationBoardSocket.instances.length).toBeGreaterThan(0));
    detailVersion = 2;
    await act(async () => {
      IntegrationBoardSocket.instances[0].onmessage?.({
        data: JSON.stringify({ event_id: 8, type: "task.updated", task_id: blockedTask.task_id }),
      });
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(await screen.findByRole("heading", { name: "Recovery evidence updated" })).toBeInTheDocument();
    expect(screen.getByText("recovery_reviewed")).toBeInTheDocument();

    vi.useFakeTimers();
    act(() => IntegrationBoardSocket.instances[0].onclose?.());
    await act(async () => {
      vi.advanceTimersByTime(1500);
      for (let index = 0; index < 8; index += 1) await Promise.resolve();
    });
    expect(listCalls).toBeGreaterThan(2);
    expect(IntegrationBoardSocket.instances.some((socket) => socket.url.includes("after=9"))).toBe(true);
  });
});
