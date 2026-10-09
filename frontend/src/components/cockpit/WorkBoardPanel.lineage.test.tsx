import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { WorkBoardAttempt, WorkBoardEvent, WorkBoardTask, WorkBoardTaskDetail } from "../../types";
import { WorkBoardPanel } from "./WorkBoardPanel";

const owner = { ownerPrincipalId: "operator:one", ownerSessionId: "operator-session-1" };
const tuple = { parent_task_id: "parent-task", parent_attempt_id: "parent-attempt", step_id: "delegate",
  child_task_id: "a".repeat(32), child_attempt_id: "b".repeat(32),
  child_job_id: `work-board:${"a".repeat(32)}:${"b".repeat(32)}`,
  delegation_invocation_id: `general-tool:${"c".repeat(48)}`, reservation_digest: "d".repeat(64) };

function task(taskId: string, title: string): WorkBoardTask {
  return { task_id: taskId, creation_sequence: 1, owner_principal_id: owner.ownerPrincipalId,
    owner_session_id: owner.ownerSessionId, origin_session_id: owner.ownerSessionId, origin_thread_id: null,
    goal_id: "goal-1", goal_revision: 1, title, body: "", capability_id: "goal-snapshot-to-file",
    typed_input_ref: "workspace-json:inputs/task.json", typed_input_digest: "a".repeat(64),
    executor_id: "executor-local", assignee_id: owner.ownerPrincipalId, priority: 50,
    idempotency_scope: "task", idempotency_key: taskId, scheduled_at: null, status: "todo",
    block_kind: null, block_reason: null, block_source_status: null, cancel_requested_at: null,
    requires_review: false, reviewer_id: null, dependency_count: 0, completed_dependency_count: 0,
    dispatch_rank: null, dispatch_wait_reason: null, recovery_action: null,
    readback_status: "not_started", verification_status: "not_started", task_revision: 1,
    result_refs: [], artifact_refs: [], latest_attempt: null, created_at: "2026-10-09T10:00:00Z",
    updated_at: "2026-10-09T10:00:00Z", completed_at: null, archived_at: null };
}
function attempt(taskId: string, attemptId: string, jobId: string): WorkBoardAttempt {
  return { attempt_id: attemptId, task_id: taskId, workflow_run_id: jobId, task_revision_at_claim: 1,
    lease_owner: "original-owner", cancel_requested_at: null, lease_expires_at: "2026-10-09T11:00:00Z",
    heartbeat_at: "2026-10-09T10:00:00Z", fencing_token: 1, executor_id: "executor-local",
    started_at: "2026-10-09T10:00:00Z", ended_at: null, outcome: null, receipt_refs: [],
    readback_status: "not_started", verification_status: "not_started", created_at: "2026-10-09T10:00:00Z",
    updated_at: "2026-10-09T10:00:00Z" };
}
function event(kind: string, taskId: string, metadata: Record<string, unknown> = { ...tuple }): WorkBoardEvent {
  return { event_id: kind === "task.specialist_published" ? 1 : 2, task_id: taskId,
    kind, metadata, created_at: "2026-10-09T10:00:00Z" };
}
function detail(value: WorkBoardTask, events: WorkBoardEvent[], attempts: WorkBoardAttempt[] = []): WorkBoardTaskDetail {
  return { task: value, events, attempts, parents: [], children: [], comments: [], revision: 1 };
}
function setup({ leg = "parent", change, targetError = false }: {
  leg?: "parent" | "child";
  change?: (parent: WorkBoardTaskDetail, child: WorkBoardTaskDetail) => void;
  targetError?: boolean;
} = {}) {
  const parent = detail(task(tuple.parent_task_id, "Delegating task"),
    [event("task.specialist_published", tuple.parent_task_id)],
    [attempt(tuple.parent_task_id, tuple.parent_attempt_id, "work-board:parent-task:parent-attempt")]);
  const child = detail(task(tuple.child_task_id, "Specialist task"), [event("task.specialist_origin", tuple.child_task_id)]);
  change?.(parent, child);
  const selected = leg === "parent" ? parent : child;
  const target = leg === "parent" ? child : parent;
  const fetch = vi.fn(async (input: RequestInfo | URL, _init?: RequestInit) => {
    const url = String(input);
    let payload: unknown = {};
    if (url.includes("/api/work-board/tasks?")) payload = { tasks: [selected.task], next_after: null, last_event_id: 7 };
    else if (url.includes("/api/work-board/events")) payload = { events: [], last_event_id: 7, gap: false };
    else if (url.endsWith("/api/goals/tree")) payload = [];
    else if (url.endsWith(`/api/work-board/tasks/${selected.task.task_id}`)) payload = selected;
    else if (url.endsWith(`/api/work-board/tasks/${target.task.task_id}`)) {
      if (targetError) return { ok: false, status: 404, json: async () => ({ detail: "Task not found" }) };
      payload = target;
    }
    return { ok: true, status: 200, json: async () => payload };
  });
  vi.stubGlobal("fetch", fetch);
  vi.stubGlobal("WebSocket", class { close() {} });
  render(<WorkBoardPanel {...owner} />);
  return { parent, child, selected, target, fetch };
}
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

describe("Work Board informational specialist lineage", () => {
  it.each(["parent", "child"] as const)("opens the authenticated reciprocal task from the %s leg", async (leg) => {
    const { selected, target, fetch } = setup({ leg });
    fireEvent.click(await screen.findByRole("button", { name: `Open task ${selected.task.title}` }));
    expect(await screen.findByText("Specialist task published · Attempt reserved · Job reserved")).toBeInTheDocument();
    const button = await screen.findByRole("button", { name: leg === "parent" ? "Open specialist" : "Open delegating task" });
    const timeline = button.closest("section")!;
    expect(within(timeline).queryByRole("button", { name: "Remove" })).not.toBeInTheDocument();
    expect(selected.parents).toEqual([]); expect(selected.children).toEqual([]);
    expect(selected.task.dependency_count).toBe(0);
    fireEvent.click(button);
    expect(await screen.findByRole("region", { name: `Task details for ${target.task.title}` })).toBeInTheDocument();
    expect(fetch.mock.calls.filter(([url]) => String(url).endsWith(`/tasks/${target.task.task_id}`)).length).toBeGreaterThanOrEqual(2);
    expect(fetch.mock.calls.every(([, init]) => !init?.method || init.method === "GET")).toBe(true);
  });

  it("labels actual matching claim and job admission separately from reservation", async () => {
    const { selected } = setup({ change: (_, child) => {
      child.attempts = [attempt(tuple.child_task_id, tuple.child_attempt_id, tuple.child_job_id)];
    } });
    fireEvent.click(await screen.findByRole("button", { name: `Open task ${selected.task.title}` }));
    expect(await screen.findByText("Specialist task published · Attempt claimed · Job admitted")).toBeInTheDocument();
  });

  it("keeps the job reserved until the actual claim links its job", async () => {
    const { selected } = setup({ change: (_, child) => {
      const claimed = attempt(tuple.child_task_id, tuple.child_attempt_id, tuple.child_job_id);
      claimed.workflow_run_id = null;
      child.attempts = [claimed];
    } });
    fireEvent.click(await screen.findByRole("button", { name: `Open task ${selected.task.title}` }));
    expect(await screen.findByText("Specialist task published · Attempt claimed · Job reserved")).toBeInTheDocument();
  });

  it.each(["extra", "missing", "digest", "task_leg", "job_tuple", "kind", "foreign", "reciprocal", "parent_attempt", "child_attempt", "child_job", "lookup_missing"])(
    "keeps %s association data generic without clickable lineage", async (change) => {
      const { selected, fetch, target } = setup({ targetError: change === "lookup_missing", change: (parent, child) => {
        const current = parent.events[0];
        if (change === "extra") current.metadata.status = "do-not-display-private-path";
        if (change === "missing") delete current.metadata.step_id;
        if (change === "digest") current.metadata.reservation_digest = "D".repeat(64);
        if (change === "task_leg") current.task_id = tuple.child_task_id;
        if (change === "job_tuple") current.metadata.child_job_id = "work-board:another-task:another-attempt";
        if (change === "kind") current.kind = "task.specialist_completed";
        if (change === "foreign") child.task.owner_session_id = "foreign-root";
        if (change === "reciprocal") child.events[0].metadata.reservation_digest = "e".repeat(64);
        if (change === "parent_attempt") parent.attempts[0].task_id = tuple.child_task_id;
        if (change === "child_attempt") child.attempts = [attempt(tuple.parent_task_id, tuple.child_attempt_id, tuple.child_job_id)];
        if (change === "child_job") child.attempts = [attempt(tuple.child_task_id, tuple.child_attempt_id, "work-board:other:other")];
      } });
      fireEvent.click(await screen.findByRole("button", { name: `Open task ${selected.task.title}` }));
      await screen.findByText("Safe task event timeline");
      if (["foreign", "reciprocal", "parent_attempt", "child_attempt", "child_job", "lookup_missing"].includes(change)) {
        await waitFor(() => expect(fetch.mock.calls.some(([url]) => String(url).endsWith(`/tasks/${target.task.task_id}`))).toBe(true));
      }
      if (change === "lookup_missing") await screen.findByText(/Linked task unavailable:/);
      await act(async () => {});
      expect(screen.queryByRole("button", { name: "Open specialist" })).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: "Open delegating task" })).not.toBeInTheDocument();
      expect(screen.queryByText("do-not-display-private-path")).not.toBeInTheDocument();
    });
});
