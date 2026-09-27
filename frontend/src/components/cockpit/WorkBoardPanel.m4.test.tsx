import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  WorkBoardAttempt,
  WorkBoardEventPage,
  WorkBoardProposal,
  WorkBoardTask,
  WorkBoardTaskDetail,
  WorkBoardTaskPage,
} from "../../types";
import { WorkBoardPanel } from "./WorkBoardPanel";

function response(payload: unknown, ok = true, status = ok ? 200 : 500) {
  return { ok, status, json: async () => payload };
}

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
    title: "Reviewable task",
    body: "A bounded task",
    capability_id: "goal-snapshot-to-file",
    typed_input_ref: "workspace-json:inputs/task.json",
    typed_input_digest: "a".repeat(64),
    executor_id: "executor-local",
    assignee_id: "operator:one",
    priority: 50,
    idempotency_scope: "task",
    idempotency_key: "task-1-key",
    scheduled_at: null,
    status: "review",
    block_kind: null,
    block_reason: null,
    block_source_status: null,
    cancel_requested_at: null,
    requires_review: true,
    reviewer_id: "operator:one",
    review_expires_at: "2099-01-02T00:00:00Z",
    dependency_count: 0,
    completed_dependency_count: 0,
    dispatch_rank: null,
    recovery_action: null,
    readback_status: "verified",
    verification_status: "passed",
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

function attempt(overrides: Partial<WorkBoardAttempt> = {}): WorkBoardAttempt {
  return {
    attempt_id: "attempt-1",
    task_id: "task-1",
    workflow_run_id: "workflow-run-1",
    task_revision_at_claim: 3,
    lease_owner: null,
    cancel_requested_at: null,
    lease_expires_at: null,
    heartbeat_at: null,
    fencing_token: 4,
    executor_id: "executor-local",
    started_at: "2026-09-23T10:00:00Z",
    ended_at: "2026-09-23T10:00:05Z",
    outcome: "succeeded",
    receipt_refs: [
      {
        artifact_id: "artifact-1",
        workflow_run_id: "workflow-run-1",
        status: "succeeded",
        verified: true,
        content_sha256: "b".repeat(64),
      },
    ],
    readback_status: "verified",
    verification_status: "passed",
    created_at: "2026-09-23T10:00:00Z",
    updated_at: "2026-09-23T10:00:05Z",
    ...overrides,
  };
}

function detail(taskValue: WorkBoardTask, overrides: Partial<WorkBoardTaskDetail> = {}): WorkBoardTaskDetail {
  return {
    task: taskValue,
    attempts: taskValue.latest_attempt ? [taskValue.latest_attempt] : [],
    parents: [],
    children: [],
    comments: [],
    events: [],
    revision: taskValue.task_revision,
    ...overrides,
  };
}

function proposal(overrides: Partial<WorkBoardProposal> = {}): WorkBoardProposal {
  return {
    kind: "decompose",
    proposal_id: "proposal-1",
    proposal_revision: 1,
    parent_task_id: "task-1",
    parent_revision: 3,
    proposal_digest: "c".repeat(64),
    expires_at: "2099-01-03T00:00:00Z",
    proposed_tasks: [
      {
        task_id: "proposal-child-1",
        title: "Proposed bounded action",
        body: "Safe proposed body",
        capability_id: "goal-snapshot-to-file",
        capability_version: "1",
        executor_id: "executor-local",
        authority: "Owner: authenticated owner/session; goal goal-1 revision 1. Capability: workflow.goal-snapshot-to-file@1; executor seraph-work-board:workflow.goal-snapshot-to-file is derived from the server registry. Capability-specific authority requirements: Operator capability-execute session; active owner-bound goal at the exact revision; configured success criterion, verifier, and evidence; enabled governed workflow tool; scoped artifact write followed by independent readback. This capability does not use remote inference. Current provider-free preflight: READY; dispatch will recheck before claim. Limits: at most 2 task attempts; 300s effective goal/job runtime (default 300s, hard cap 900s). Accepting creates Todo only and grants no authority or external-effect approval. Every dispatch rechecks current owner session, goal revision, grants, policy, approval, idempotency, and required independent readback.",
        typed_input_ref: "workspace-json:inputs/proposal-child.json",
        typed_input_digest: "d".repeat(64),
        dependencies: [],
        cost_estimate: "0 local units",
      },
    ],
    proposed_links: [{ parent_task_id: "task-1", child_task_id: "proposal-child-1" }],
    estimated_cost: "0 local units",
    blocked_reason: null,
    status: "proposed",
    capability_id: "work-board-proposal",
    capability_version: "1",
    ...overrides,
  };
}

function page(tasks: WorkBoardTask[], lastEventId = 10): WorkBoardTaskPage {
  return { tasks, next_after: null, last_event_id: lastEventId };
}

function events(lastEventId = 10): WorkBoardEventPage {
  return { events: [], last_event_id: lastEventId, gap: false };
}

class TestBoardSocket {
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(_url: string) {}

  close() {
    this.onclose?.();
  }
}

function installBoardTransport(
  fetchMock: ReturnType<typeof vi.fn>,
  state: { task: WorkBoardTask; detail: WorkBoardTaskDetail },
  handler?: (url: string, init?: RequestInit) => ReturnType<typeof response> | null,
) {
  fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const handled = handler?.(url, init);
    if (handled) return Promise.resolve(handled);
    if (url.includes("/api/work-board/tasks?") && !url.match(/\/tasks\/[^?]+/)) return Promise.resolve(response(page([state.task])));
    if (url.includes("/api/work-board/events")) return Promise.resolve(response(events()));
    if (url.endsWith("/api/goals/tree")) return Promise.resolve(response([]));
    if (url.includes("/api/work-board/goals/goal-1/execution-limits")) {
      return Promise.resolve(response({
        goal_id: "goal-1",
        goal_revision: state.task.goal_revision,
        effective_max_runtime_seconds: 300,
        default_max_runtime_seconds: 300,
        hard_max_runtime_seconds: 900,
        attempt_limit: 2,
        limit_source: "default",
      }));
    }
    if (url.endsWith(`/api/work-board/tasks/${state.task.task_id}`)) return Promise.resolve(response(state.detail));
    return Promise.resolve(response({}));
  });
}

describe("WorkBoardPanel M4 review and triage controls", () => {
  const fetchMock = vi.fn();
  const owner = { ownerPrincipalId: "operator:one", ownerSessionId: "operator-session-1" };

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("WebSocket", TestBoardSocket);
    vi.spyOn(window, "confirm").mockReturnValue(true);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  });

  it("opens readback-only attempt evidence in the workflow inspector callback", async () => {
    const latestAttempt = attempt({
      receipt_refs: [{
        readback_id: "attempt-readback-1",
        workflow_run_id: "workflow-run-1",
        status: "succeeded",
        verified: true,
      }],
    });
    const reviewTask = task({ latest_attempt: latestAttempt });
    installBoardTransport(fetchMock, {
      task: reviewTask,
      detail: detail(reviewTask),
    });
    const onInspectArtifact = vi.fn();

    render(<WorkBoardPanel {...owner} onInspectArtifact={onInspectArtifact} />);
    fireEvent.click(await screen.findByRole("button", { name: "Open task Reviewable task" }));
    fireEvent.click(await screen.findByRole("button", { name: "Inspect execution evidence attempt-readback-1" }));

    expect(onInspectArtifact).toHaveBeenCalledWith(expect.objectContaining({
      reference: expect.objectContaining({ readback_id: "attempt-readback-1" }),
      workflowRunId: "workflow-run-1",
      parentWorkflowRunId: "workflow-run-1",
    }));
  });

  it("does not offer Decompose from an incomplete Triage card", async () => {
    const triage = task({
      status: "triage",
      title: "Rough operator idea",
      capability_id: null,
      typed_input_ref: null,
      typed_input_digest: null,
    });
    const state = { task: triage, detail: detail(triage) };
    installBoardTransport(fetchMock, state);

    render(<WorkBoardPanel {...owner} />);
    fireEvent.click(await screen.findByRole("button", { name: "Open task Rough operator idea" }));
    expect(await screen.findByRole("button", { name: "Specify for review" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Decompose for review" })).not.toBeInTheDocument();
  });

  it("shows review verdict only to the exact named owner session and submits bounded changes", async () => {
    const reviewed = task({ latest_attempt: attempt() });
    const state = { task: reviewed, detail: detail(reviewed) };
    let actionBody: Record<string, unknown> | null = null;
    installBoardTransport(fetchMock, state, (url, init) => {
      if (url.endsWith("/actions") && init?.method === "POST") {
        actionBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        return response({ task: state.task });
      }
      return null;
    });

    const first = render(<WorkBoardPanel ownerPrincipalId="operator:other" ownerSessionId="operator-session-1" />);
    fireEvent.click(await screen.findByRole("button", { name: "Open task Reviewable task" }));
    expect(screen.queryByRole("button", { name: "Approve review" })).not.toBeInTheDocument();
    first.unmount();

    render(<WorkBoardPanel {...owner} />);
    fireEvent.click(await screen.findByRole("button", { name: "Open task Reviewable task" }));
    expect(screen.queryByLabelText("Manual block reason")).not.toBeInTheDocument();
    fireEvent.change(await screen.findByLabelText("Required changes"), { target: { value: "Recheck the destination readback." } });
    fireEvent.click(screen.getByRole("button", { name: "Request changes" }));

    await waitFor(() => expect(actionBody).toEqual({
      action: "request_changes",
      expected_revision: 3,
      reason: "Recheck the destination readback.",
    }));
  });

  it("shows the goal verification recovery for blocked GoalSnapshot work", async () => {
    const blocked = task({
      status: "blocked",
      title: "Goal verification prerequisite",
      block_kind: "capability",
      block_reason: "goal_snapshot_criterion_missing",
      recovery_action: "configure_goal_success_criterion",
      requires_review: false,
      reviewer_id: null,
      review_expires_at: null,
      latest_attempt: null,
    });
    const state = { task: blocked, detail: detail(blocked) };
    installBoardTransport(fetchMock, state);

    render(<WorkBoardPanel {...owner} />);
    fireEvent.click(await screen.findByRole("button", { name: "Open task Goal verification prerequisite" }));
    expect((await screen.findAllByText(/Complete the goal's success criterion, verifier, and evidence/)).length).toBeGreaterThan(0);
  });

  it("keeps manual block reasons within the backend 500-character contract", async () => {
    const todo = task({
      status: "todo",
      title: "Manual block target",
      requires_review: false,
      reviewer_id: null,
      review_expires_at: null,
    });
    const state = { task: todo, detail: detail(todo) };
    let actionBody: Record<string, unknown> | null = null;
    installBoardTransport(fetchMock, state, (url, init) => {
      if (url.endsWith("/actions") && init?.method === "POST") {
        actionBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        return response({ task: { ...state.task, status: "blocked", task_revision: 4 } });
      }
      return null;
    });

    render(<WorkBoardPanel {...owner} />);
    fireEvent.click(await screen.findByRole("button", { name: "Open task Manual block target" }));
    const reasonField = screen.getByLabelText("Manual block reason") as HTMLTextAreaElement;
    expect(reasonField.maxLength).toBe(500);
    fireEvent.change(reasonField, { target: { value: "x".repeat(501) } });
    fireEvent.click(screen.getByLabelText("Confirm this operator block"));
    expect(screen.getByRole("button", { name: "Block task" })).toBeDisabled();

    fireEvent.change(reasonField, { target: { value: "x".repeat(500) } });
    expect(screen.getByRole("button", { name: "Block task" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: "Block task" }));

    await waitFor(() => expect(actionBody).toEqual({
      action: "block",
      expected_revision: 3,
      block_kind: "operator",
      source_status: "todo",
      reason: "x".repeat(500),
    }));
  });

  it("offers only explicit reviewer renewal for an expired review", async () => {
    const expired = task({
      status: "blocked",
      block_kind: "review_expired",
      block_reason: "The review window expired.",
      block_source_status: "review",
      recovery_action: "renew_review",
      review_expires_at: null,
      latest_attempt: attempt(),
    });
    const state = { task: expired, detail: detail(expired) };
    let actionBody: Record<string, unknown> | null = null;
    installBoardTransport(fetchMock, state, (url, init) => {
      if (url.endsWith("/actions") && init?.method === "POST") {
        actionBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        return response({ task: state.task });
      }
      return null;
    });

    render(<WorkBoardPanel {...owner} />);
    fireEvent.click(await screen.findByRole("button", { name: "Open task Reviewable task" }));
    expect(await screen.findByText(/Generic unblock and retry cannot restore it/i)).toBeInTheDocument();
    expect(screen.getByText("Server recovery action: Renew the review window")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Renew review window" }));

    await waitFor(() => expect(actionBody).toEqual({
      action: "renew_review",
      expected_revision: 3,
    }));
    expect(screen.queryByRole("button", { name: "Retry \(new attempt\)" })).not.toBeInTheDocument();
  });

  it("shows an exhausted attempt limit without exposing retry", async () => {
    const exhausted = task({
      status: "blocked",
      block_kind: "attempt_limit",
      block_reason: "The board attempt limit has been exhausted.",
      block_source_status: "review",
      recovery_action: null,
      latest_attempt: attempt(),
    });
    installBoardTransport(fetchMock, { task: exhausted, detail: detail(exhausted) });

    render(<WorkBoardPanel {...owner} />);
    fireEvent.click(await screen.findByRole("button", { name: "Open task Reviewable task" }));

    expect(await screen.findByText(/used its two-attempt limit/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Retry/ })).not.toBeInTheDocument();
  });

  it("shows review intent for an ordinary running task before terminal readback", async () => {
    const running = task({
      status: "running",
      requires_review: false,
      reviewer_id: null,
      review_expires_at: null,
      latest_attempt: attempt({
        ended_at: null,
        receipt_refs: [],
        lease_owner: "executor-local",
        lease_expires_at: "2099-01-03T00:00:00Z",
      }),
    });
    const state = { task: running, detail: detail(running) };
    let actionBody: Record<string, unknown> | null = null;
    installBoardTransport(fetchMock, state, (url, init) => {
      if (url.endsWith("/actions") && init?.method === "POST") {
        actionBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        return response({ task: state.task });
      }
      return null;
    });

    render(<WorkBoardPanel {...owner} />);
    fireEvent.click(await screen.findByRole("button", { name: "Open task Reviewable task" }));
    fireEvent.click(await screen.findByRole("button", { name: "Request review" }));

    await waitFor(() => expect(actionBody).toEqual({
      action: "request_review",
      expected_revision: 3,
      attempt_id: "attempt-1",
    }));
  });

  it("previews a governed proposal and refreshes after a stale acceptance", async () => {
    const todo = task({ status: "todo", title: "Task to decompose" });
    const refreshed = { ...todo, task_revision: 4, title: "Task changed on server" };
    const state = { task: todo, detail: detail(todo) };
    let proposalCall = 0;
    let acceptBody: Record<string, unknown> | null = null;
    installBoardTransport(fetchMock, state, (url, init) => {
      if (url.endsWith("/decompose") && init?.method === "POST") {
        proposalCall += 1;
        return response(proposal());
      }
      if (url.endsWith("/proposals/proposal-1/accept") && init?.method === "POST") {
        acceptBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        state.task = refreshed;
        state.detail = detail(refreshed);
        return response({ detail: { code: "stale_proposal_revision", message: "The proposal parent revision is stale." } }, false, 409);
      }
      return null;
    });

    render(<WorkBoardPanel {...owner} />);
    fireEvent.click(await screen.findByRole("button", { name: "Open task Task to decompose" }));
    fireEvent.click(await screen.findByRole("button", { name: "Decompose for review" }));
    expect(await screen.findByRole("region", { name: "Triage proposal preview" })).toHaveTextContent("Proposed bounded action");
    expect(screen.getByText(/authority: Owner: authenticated owner\/session; goal goal-1 revision 1/i)).toBeInTheDocument();
    expect(screen.getByText(/Capability-specific authority requirements: Operator capability-execute session/i)).toBeInTheDocument();
    expect(screen.getByText(/Current provider-free preflight: READY/i)).toBeInTheDocument();
    expect(screen.getByText(/300s effective goal\/job runtime/i)).toBeInTheDocument();
    expect(screen.getByText(/at most 2 task attempts/i)).toBeInTheDocument();
    expect(screen.getByText(/grants no authority or external-effect approval/i)).toBeInTheDocument();
    expect(screen.getByText(/workspace-json:inputs\/proposal-child\.json/i)).toBeInTheDocument();
    expect(screen.getByText(new RegExp(`digest ${"d".repeat(64)}`))).toBeInTheDocument();
    expect(screen.getByText(/Estimated cost: 0 local units/i)).toBeInTheDocument();
    expect(proposalCall).toBe(1);

    fireEvent.click(screen.getByRole("button", { name: "Accept proposal" }));
    await waitFor(() => expect(acceptBody).toEqual({
      expected_proposal_revision: 1,
      expected_parent_revision: 3,
    }));
    expect(await screen.findByText(/proposal parent revision is stale/i)).toBeInTheDocument();
    expect(screen.getByText(/revision 4/)).toBeInTheDocument();
  });

  it("blocks acceptance when a legacy or incomplete proposal has no server authority preview", async () => {
    const todo = task({ status: "todo", title: "Task to decompose" });
    const state = { task: todo, detail: detail(todo) };
    installBoardTransport(fetchMock, state, (url, init) => {
      if (url.endsWith("/decompose") && init?.method === "POST") {
        return response(proposal({
          proposed_tasks: [{
            ...proposal().proposed_tasks[0],
            authority: "server_bound",
          }],
        }));
      }
      return null;
    });

    render(<WorkBoardPanel {...owner} />);
    fireEvent.click(await screen.findByRole("button", { name: "Open task Task to decompose" }));
    fireEvent.click(await screen.findByRole("button", { name: "Decompose for review" }));

    expect(await screen.findByText(/complete server-derived authority preview is missing/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Accept proposal" })).toBeDisabled();
  });

  it("requires an explicit new idempotency key after a no-contact prerequisite block", async () => {
    const todo = task({ status: "todo", title: "Retryable proposal", latest_attempt: null });
    const state = { task: todo, detail: detail(todo) };
    const hydratedKey = "old-request-key";
    let proposalPostBody: Record<string, unknown> | null = null;
    installBoardTransport(fetchMock, state, (url, init) => {
      if (url.endsWith(`/api/work-board/tasks/${todo.task_id}/proposals`) && (!init?.method || init.method === "GET")) {
        return response({ proposals: [proposal({
          kind: "decompose",
          status: "blocked",
          idempotency_key: hydratedKey,
          blocked_reason: "openrouter_route_unavailable",
          recovery_action: "retry_same_binding_after_prerequisite",
        })] });
      }
      if (url.endsWith("/decompose") && init?.method === "POST") {
        proposalPostBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        return response(proposal({
          kind: "decompose",
          status: "proposed",
          idempotency_key: String(proposalPostBody.idempotency_key),
        }));
      }
      return null;
    });

    render(<WorkBoardPanel {...owner} />);
    fireEvent.click(await screen.findByRole("button", { name: "Open task Retryable proposal" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("openrouter_route_unavailable");
    fireEvent.click(screen.getByRole("button", { name: "Retry with new request key" }));

    await waitFor(() => expect(proposalPostBody).not.toBeNull());
    const requestBody = proposalPostBody as unknown as Record<string, unknown>;
    expect(requestBody.idempotency_key).toBeTypeOf("string");
    expect(requestBody.idempotency_key).not.toBe(hydratedKey);
    expect(await screen.findByText(/proposed bounded action/i)).toBeInTheDocument();
  });

  it("keeps unknown external effects in recovery and renders safe parent handoff evidence", async () => {
    const blocked = task({
      status: "blocked",
      title: "Unknown effect",
      requires_review: false,
      reviewer_id: null,
      block_kind: "unknown_effect",
      block_reason: "The destination outcome cannot be proven.",
      recovery_action: "reconcile_external_effect",
      latest_attempt: null,
    });
    const state = {
      task: blocked,
      detail: detail(blocked, {
        parent_handoffs: [{
          handoff_id: "handoff-1",
          parent_task_id: "parent-1",
          child_task_id: "task-1",
          status: "verified",
          summary: "Safe structured parent result",
          artifact_refs: [{ artifact_id: "parent-artifact-1", artifact_type: "verified output", workflow_run_id: "parent-run-1", content_sha256: "c".repeat(64), verified: true }],
          result_refs: [
            { readback_id: "parent-readback-1", status: "verified", verified: true },
            { verification_id: "parent-verification-1", status: "verified", verified: true },
          ],
          verification_receipt: { status: "verified", workflow_run_id: "parent-run-1" },
          source_attempt_id: "attempt-parent-1",
          source_task_revision: 7,
          risks: ["verification_scope_bounded"],
        }],
      }),
    };
    installBoardTransport(fetchMock, state);

    const onInspectArtifact = vi.fn();
    const onInspectWorkflowRun = vi.fn();
    render(<WorkBoardPanel {...owner} onInspectArtifact={onInspectArtifact} onInspectWorkflowRun={onInspectWorkflowRun} />);
    fireEvent.click(await screen.findByRole("button", { name: "Open task Unknown effect" }));
    expect(await screen.findByText(/External effect or cost is unresolved/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Retry/ })).not.toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Safe parent handoffs" })).toHaveTextContent("Safe structured parent result");
    expect(screen.getByRole("region", { name: "Safe parent handoffs" })).toHaveTextContent("source attempt attempt-parent-1");
    expect(within(screen.getByRole("region", { name: "Safe parent handoffs" })).getByText(/do not grant authority/i)).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Inspect parent handoff evidence parent-artifact-1" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Inspect parent handoff evidence parent-artifact-1" }));
    expect(onInspectArtifact).toHaveBeenCalledWith(expect.objectContaining({
      reference: expect.objectContaining({ artifact_id: "parent-artifact-1" }),
      workflowRunId: "parent-run-1",
      parentWorkflowRunId: "parent-run-1",
    }));
    fireEvent.click(await screen.findByRole("button", { name: "Inspect parent handoff evidence parent-readback-1" }));
    expect(onInspectArtifact).toHaveBeenLastCalledWith(expect.objectContaining({
      reference: expect.objectContaining({ readback_id: "parent-readback-1" }),
      workflowRunId: "parent-run-1",
      parentWorkflowRunId: "parent-run-1",
    }));
    fireEvent.click(screen.getByRole("button", { name: "Inspect parent handoff evidence parent-verification-1" }));
    expect(onInspectArtifact).toHaveBeenLastCalledWith(expect.objectContaining({
      reference: expect.objectContaining({ verification_id: "parent-verification-1" }),
      workflowRunId: "parent-run-1",
      parentWorkflowRunId: "parent-run-1",
    }));
    fireEvent.click(screen.getByRole("button", { name: "Open parent workflow evidence" }));
    expect(onInspectWorkflowRun).toHaveBeenCalledWith("parent-run-1", "operator-session-1");
    expect(screen.getByText(/SHA-256 c{64}/)).toBeInTheDocument();
  });

  it("preserves a UTC schedule when a different bounded field is edited", async () => {
    vi.stubEnv("TZ", "Europe/Warsaw");
    const scheduled = task({
      status: "todo",
      title: "Scheduled task",
      scheduled_at: "2026-09-25T12:00:00Z",
    });
    let patchBody: Record<string, unknown> | null = null;
    installBoardTransport(fetchMock, { task: scheduled, detail: detail(scheduled) }, (url, init) => {
      if (url.endsWith(`/api/work-board/tasks/${scheduled.task_id}`) && init?.method === "PATCH") {
        patchBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        return response({ task: { ...scheduled, title: "Renamed task", task_revision: 4 } });
      }
      return null;
    });

    render(<WorkBoardPanel {...owner} />);
    fireEvent.click(await screen.findByRole("button", { name: "Open task Scheduled task" }));
    fireEvent.click(await screen.findByRole("button", { name: "Edit bounded fields" }));
    expect(screen.getByLabelText("Scheduled at")).toHaveValue("2026-09-25T14:00");
    fireEvent.change(screen.getByLabelText("Title"), { target: { value: "Renamed task" } });
    fireEvent.click(screen.getByRole("button", { name: "Save with current revision" }));

    await waitFor(() => expect(patchBody).toEqual({ expected_revision: 3, title: "Renamed task" }));
    expect(patchBody).not.toHaveProperty("scheduled_at");
  });
});
