import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { WorkBoardAttempt, WorkBoardTask } from "../../types";
import { WorkBoardMemoryReview, type TaskMemoryProposal } from "./WorkBoardMemoryReview";

function response(payload: unknown, ok = true, status = ok ? 200 : 500) {
  return { ok, status, json: async () => payload };
}

function attempt(overrides: Partial<WorkBoardAttempt> = {}): WorkBoardAttempt {
  return {
    attempt_id: "attempt-1",
    task_id: "task-1",
    workflow_run_id: "run-1",
    task_revision_at_claim: 3,
    lease_owner: "board-worker:one",
    cancel_requested_at: null,
    lease_expires_at: null,
    heartbeat_at: "2026-09-25T06:00:00Z",
    fencing_token: 2,
    executor_id: "seraph-work-board:work.github-followthrough.v1",
    started_at: "2026-09-25T05:58:00Z",
    ended_at: "2026-09-25T05:59:00Z",
    outcome: "succeeded",
    receipt_refs: [],
    readback_status: "verified",
    verification_status: "passed",
    created_at: "2026-09-25T05:58:00Z",
    updated_at: "2026-09-25T05:59:00Z",
    ...overrides,
  };
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
    goal_revision: 4,
    title: "Verified action",
    body: "The task completed with a verified result.",
    capability_id: "work.github-followthrough.v1",
    typed_input_ref: "workspace-json:inputs/action.json",
    typed_input_digest: "a".repeat(64),
    executor_id: "seraph-work-board:work.github-followthrough.v1",
    assignee_id: "operator:one",
    priority: 50,
    idempotency_scope: "task",
    idempotency_key: "task-1-key",
    scheduled_at: null,
    status: "done",
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
    readback_status: "verified",
    verification_status: "passed",
    task_revision: 6,
    result_refs: [],
    artifact_refs: [],
    latest_attempt: attempt(),
    created_at: "2026-09-25T05:00:00Z",
    updated_at: "2026-09-25T05:59:00Z",
    completed_at: "2026-09-25T05:59:00Z",
    archived_at: null,
    ...overrides,
  };
}

const proposal: TaskMemoryProposal = {
  proposal_id: "proposal-1",
  source_task_id: "task-1",
  attempt_id: "attempt-1",
  workflow_run_id: "run-1",
  status: "proposed",
  proposed_text: "A verified outcome from the selected registered capability.",
  proposed_text_digest: "b".repeat(64),
  memory_kind: "fact",
  scope: {
    goal_id: "goal-1",
    source_context_digest: "c".repeat(64),
    preferred_capability_id: "work.github-followthrough.v1",
  },
  confidence: 0.5,
  preferred_capability_id: "work.github-followthrough.v1",
  registered_capabilities: [
    { capability_id: "work.github-followthrough.v1", version: "1" },
    { capability_id: "guardian.research-watch.v1", version: "1" },
  ],
  evidence_refs: ["artifact:art-1", "readback:readback-1"],
  reason_code: "verified_source",
  recovery_action: "none",
  accepted_memory_id: null,
  revision: 1,
  created_at: "2026-09-25T06:00:00Z",
  expires_at: "2026-09-25T06:15:00Z",
};

const changedReceipt = {
  receipt_id: "decision-1",
  receipt_stage: "later_comparison",
  source_task_id: "task-source",
  source_attempt_id: "attempt-source",
  later_task_id: "task-later",
  later_attempt_id: null,
  goal_id: "goal-1",
  source_context_digest: "c".repeat(64),
  before_input_digest: "d".repeat(64),
  after_input_digest: "e".repeat(64),
  before_action_id: "guardian.research-watch.v1",
  after_action_id: "work.github-followthrough.v1",
  accepted_memory_id: "memory-1",
  retrieval_evidence_ids: ["proposal-1"],
  decision_status: "changed",
  reason: "accepted_memory_selected",
  created_at: "2026-09-25T06:01:00Z",
} as const;

describe("WorkBoardMemoryReview", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("creates a source-bound review and shows the before/after decision receipt", async () => {
    let savedProposals = [proposal];
    let savedReceipts: typeof changedReceipt[] = [];
    const actionBodies: Record<string, unknown>[] = [];
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/memory/task-proposals?") && init?.method !== "POST") {
        return response({ proposals: savedProposals });
      }
      if (url.includes("/api/memory/task-decisions?")) return response({ receipts: savedReceipts });
      if (url.endsWith("/api/memory/task-proposals") && init?.method === "POST") {
        return response({ proposal_id: "proposal-1", status: "proposed", evidence_refs: proposal.evidence_refs, revision: 1 });
      }
      if (url.endsWith("/api/memory/task-decision-capabilities")) {
        return response({ capabilities: [] });
      }
      if (url.endsWith("/actions") && init?.method === "POST") {
        const body = JSON.parse(String(init.body)) as Record<string, unknown>;
        actionBodies.push(body);
        if (body.action === "rollback") {
          savedProposals = [{ ...proposal, status: "rolled_back", accepted_memory_id: "memory-1", revision: 3 }];
          return response({ status: "rolled_back", accepted_memory_id: "memory-1" });
        }
        savedProposals = [{ ...proposal, status: "accepted", accepted_memory_id: "memory-1", revision: 2 }];
        savedReceipts = [changedReceipt];
        return response({ status: "accepted", accepted_memory_id: "memory-1" });
      }
      return response({}, false, 404);
    });

    const currentTask = task();
    render(<WorkBoardMemoryReview task={currentTask} ownerPrincipalId="operator:one" ownerSessionId="operator-session-1" />);

    await screen.findByRole("article", { name: "Memory proposal proposed" });
    fireEvent.click(screen.getByRole("button", { name: "Review learning" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([url, init]) => (
      String(url).endsWith("/api/memory/task-proposals")
        && (init as RequestInit | undefined)?.method === "POST"
    ))).toBe(true));
    const proposalCall = fetchMock.mock.calls.find(([url, init]) => String(url).endsWith("/api/memory/task-proposals") && (init as RequestInit | undefined)?.method === "POST");
    expect(JSON.parse((proposalCall?.[1] as RequestInit).body as string)).toMatchObject({
      task_id: "task-1",
      attempt_id: "attempt-1",
      expected_task_revision: 6,
    });

    await screen.findByRole("article", { name: "Memory proposal proposed" });
    expect(screen.getByText("work.github-followthrough.v1", { exact: false })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText(/Future comparable decision/), {
      target: { value: "require_operator_confirmation" },
    });
    fireEvent.change(screen.getByLabelText("Preferred registered capability"), {
      target: { value: "guardian.research-watch.v1" },
    });
    fireEvent.change(screen.getByLabelText("Existing canonical memory ID to supersede (optional)"), {
      target: { value: "memory-old" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Edit and accept" }));

    expect(await screen.findByText("Accepted canonical memory memory-1")).toBeInTheDocument();
    expect(actionBodies[0]).toMatchObject({
      action: "edit_accept",
      expected_revision: 1,
      edited_text: proposal.proposed_text,
      decision_effect: "require_operator_confirmation",
      preferred_capability_id: "guardian.research-watch.v1",
      corrects_memory_id: "memory-old",
      expected_preview_text_digest: proposal.proposed_text_digest,
      expected_task_revision: 6,
      expected_goal_revision: 4,
    });
    expect(screen.getByText(/Before: guardian\.research-watch\.v1 → After: work\.github-followthrough\.v1/)).toBeInTheDocument();
    expect(screen.getByText(/Input digests sha256:dddddddddddd → sha256:eeeeeeeeeeee/)).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Undo reason"), { target: { value: "The source was no longer relevant." } });
    fireEvent.click(screen.getByRole("button", { name: "Undo memory change" }));
    await screen.findByRole("article", { name: "Memory proposal rolled_back" });
    expect(actionBodies[1]).toMatchObject({ action: "rollback", reason: "The source was no longer relevant." });
  });

  it("keeps a stale proposal recovery error visible after refreshing the proposal", async () => {
    let proposalListCalls = 0;
    let actionCalls = 0;
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/memory/task-proposals?") && init?.method !== "POST") {
        proposalListCalls += 1;
        return response({ proposals: [proposal] });
      }
      if (url.includes("/api/memory/task-decisions?")) return response({ receipts: [] });
      if (url.endsWith("/actions") && init?.method === "POST") {
        actionCalls += 1;
        return response(
          { detail: { code: "stale_proposal_revision", message: "The proposal revision is stale." } },
          false,
          409,
        );
      }
      return response({}, false, 404);
    });

    render(<WorkBoardMemoryReview task={task()} ownerPrincipalId="operator:one" ownerSessionId="operator-session-1" />);

    await screen.findByRole("article", { name: "Memory proposal proposed" });
    fireEvent.click(screen.getByRole("button", { name: "Accept proposal" }));

    await waitFor(() => expect(actionCalls).toBe(1));
    await waitFor(() => expect(proposalListCalls).toBeGreaterThan(1));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The proposal revision is stale. Refresh the proposal, review the current revision, and try the action again.",
    );
  });

  it("shows accepted-memory signing recovery when the server key is unavailable", async () => {
    let currentProposal: TaskMemoryProposal = proposal;
    let actionCalls = 0;
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/memory/task-proposals?") && init?.method !== "POST") {
        return response({ proposals: [currentProposal] });
      }
      if (url.includes("/api/memory/task-decisions?")) return response({ receipts: [] });
      if (url.endsWith("/actions") && init?.method === "POST") {
        actionCalls += 1;
        currentProposal = {
          ...proposal,
          status: "blocked",
          reason_code: "accepted_memory_binding_unverifiable",
          recovery_action: "verify_source_and_reaccept",
          revision: 2,
        };
        return response(
          {
            detail: {
              code: "accepted_binding_unavailable",
              proposal: currentProposal,
            },
          },
          false,
          503,
        );
      }
      return response({}, false, 404);
    });

    render(<WorkBoardMemoryReview task={task()} ownerPrincipalId="operator:one" ownerSessionId="operator-session-1" />);

    await screen.findByRole("article", { name: "Memory proposal proposed" });
    fireEvent.click(screen.getByRole("button", { name: "Accept proposal" }));

    await waitFor(() => expect(actionCalls).toBe(1));
    expect(await screen.findByRole("article", { name: "Memory proposal blocked" })).toBeInTheDocument();
    expect(screen.getByText(/Recovery: verify source and reaccept/)).toBeInTheDocument();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Memory could not be signed. The proposal is blocked until its source is reverified and accepted again.",
    );
  });

  it("reverifies a quarantined source before requiring fresh operator acceptance", async () => {
    let currentProposal: TaskMemoryProposal = {
      ...proposal,
      status: "blocked",
      reason_code: "accepted_memory_binding_unverifiable",
      recovery_action: "verify_source_and_reaccept",
      accepted_memory_id: "memory-old",
      revision: 2,
    };
    const actionBodies: Record<string, unknown>[] = [];
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/memory/task-proposals?") && init?.method !== "POST") {
        return response({ proposals: [currentProposal] });
      }
      if (url.includes("/api/memory/task-decisions?")) {
        return response({ receipts: [{
          ...changedReceipt,
          decision_status: "blocked",
          before_action_id: "",
          after_action_id: "",
          before_input_digest: "",
          after_input_digest: "",
          reason: "receipt_signature_mismatch",
          integrity_status: "signature_mismatch",
        }] });
      }
      if (url.endsWith("/actions") && init?.method === "POST") {
        const body = JSON.parse(String(init.body)) as Record<string, unknown>;
        actionBodies.push(body);
        currentProposal = {
          ...proposal,
          status: "proposed",
          reason_code: "verified_source_reverified",
          recovery_action: "none",
          accepted_memory_id: null,
          corrects_memory_id: "memory-old",
          revision: 3,
        };
        return response(currentProposal);
      }
      return response({}, false, 404);
    });

    render(<WorkBoardMemoryReview task={task()} ownerPrincipalId="operator:one" ownerSessionId="operator-session-1" />);

    await screen.findByRole("article", { name: "Memory proposal blocked" });
    expect(screen.getByText(/Receipt evidence is quarantined \(signature mismatch\)/)).toBeInTheDocument();
    expect(screen.getByText(/Before: No comparable action → After: No comparable action/)).toBeInTheDocument();
    expect(screen.queryByText(/Before: guardian\.research-watch\.v1 → After: work\.github-followthrough\.v1/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Verify source and review again" }));

    expect(await screen.findByRole("article", { name: "Memory proposal proposed" })).toBeInTheDocument();
    expect(screen.getByText(/If accepted, this review will supersede prior canonical memory memory-old/)).toBeInTheDocument();
    expect(actionBodies[0]).toMatchObject({
      action: "recover",
      expected_revision: 2,
      expected_preview_text_digest: proposal.proposed_text_digest,
      expected_task_revision: 6,
      expected_goal_revision: 4,
    });
  });

  it("offers the advertised recovery for a missing source baseline", async () => {
    let currentProposal: TaskMemoryProposal = {
      ...proposal,
      status: "blocked",
      reason_code: "source_baseline_missing",
      recovery_action: "verify_source_and_reaccept",
      revision: 4,
    };
    const actionBodies: Record<string, unknown>[] = [];
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/memory/task-proposals?") && init?.method !== "POST") {
        return response({ proposals: [currentProposal] });
      }
      if (url.includes("/api/memory/task-decisions?")) return response({ receipts: [] });
      if (url.endsWith("/actions") && init?.method === "POST") {
        const body = JSON.parse(String(init.body)) as Record<string, unknown>;
        actionBodies.push(body);
        currentProposal = {
          ...proposal,
          status: "proposed",
          reason_code: "verified_source_reverified",
          recovery_action: "none",
          accepted_memory_id: null,
          revision: 5,
        };
        return response(currentProposal);
      }
      return response({}, false, 404);
    });

    render(<WorkBoardMemoryReview task={task()} ownerPrincipalId="operator:one" ownerSessionId="operator-session-1" />);

    await screen.findByRole("article", { name: "Memory proposal blocked" });
    expect(screen.getByText(/Recovery: verify source and reaccept/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Verify source and review again" }));

    expect(await screen.findByRole("article", { name: "Memory proposal proposed" })).toBeInTheDocument();
    expect(actionBodies[0]).toMatchObject({
      action: "recover",
      expected_revision: 4,
      expected_task_revision: 6,
      expected_goal_revision: 4,
    });
  });

  it("executes the restored proposal recovery when preview text is unavailable", async () => {
    let currentProposal: TaskMemoryProposal = {
      ...proposal,
      status: "blocked",
      proposed_text: null,
      proposed_text_digest: null,
      preview_text: null,
      preview_text_digest: null,
      reason_code: "proposal_preview_not_restored",
      recovery_action: "request_verified_proposal_again",
      revision: 7,
    };
    const actionBodies: Record<string, unknown>[] = [];
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/memory/task-proposals?") && init?.method !== "POST") {
        return response({ proposals: [currentProposal] });
      }
      if (url.includes("/api/memory/task-decisions?")) return response({ receipts: [] });
      if (url.endsWith("/actions") && init?.method === "POST") {
        actionBodies.push(JSON.parse(String(init.body)) as Record<string, unknown>);
        currentProposal = {
          ...proposal,
          status: "proposed",
          reason_code: "verified_source_reverified",
          recovery_action: "none",
          revision: 8,
        };
        return response(currentProposal);
      }
      return response({}, false, 404);
    });

    render(<WorkBoardMemoryReview task={task()} ownerPrincipalId="operator:one" ownerSessionId="operator-session-1" />);

    await screen.findByRole("article", { name: "Memory proposal blocked" });
    expect(screen.getByText(/Recovery: request verified proposal again/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Verify source and review again" }));

    expect(await screen.findByRole("article", { name: "Memory proposal proposed" })).toBeInTheDocument();
    expect(actionBodies[0]).toMatchObject({
      action: "recover",
      expected_revision: 7,
      expected_preview_text_digest: null,
      expected_task_revision: 6,
      expected_goal_revision: 4,
    });
  });

  it("keeps unverified tasks from requesting learning and records explicit no-learning receipts", async () => {
    const noLearning = { ...proposal, status: "no_learning", proposed_text: null, proposed_text_digest: null, reason_code: "unknown_effect" };
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/memory/task-proposals?")) return response({ proposals: [noLearning] });
      if (url.includes("/api/memory/task-decisions?")) return response({ receipts: [] });
      return response({}, false, 404);
    });

    render(<WorkBoardMemoryReview
      task={task({ status: "blocked", block_kind: "unknown_effect", latest_attempt: attempt({ outcome: "unknown_external_effect", readback_status: "unknown", verification_status: "reconciliation_required" }) })}
      ownerPrincipalId="operator:one"
      ownerSessionId="operator-session-1"
    />);

    expect(await screen.findByText(/no learning/i)).toBeInTheDocument();
    expect(screen.getByText(/Reason unknown effect/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Review learning" })).toBeDisabled();
  });

  it("compares ordered typed candidates through the proposal-only goal decision route", async () => {
    const comparison = {
      decision: {
        decision_status: "changed",
        reason: "accepted_memory_selected",
        before_input_digest: "d".repeat(64),
        after_input_digest: "e".repeat(64),
        before_selected_capability_id: "workflow.goal-snapshot-to-file",
        after_selected_capability_id: "guardian.research-watch.v1",
        evidence_ids: ["readback:verified-source"],
        receipt_id: "decision-1",
      },
      selected: { capability_id: "guardian.research-watch.v1", action: "act", reason: "accepted_memory_selected" },
    } as const;
    let requestBody: Record<string, unknown> | null = null;
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/memory/task-proposals?")) return response({ proposals: [] });
      if (url.includes("/api/memory/task-decisions?")) return response({ receipts: [] });
      if (url.endsWith("/api/memory/task-decision-capabilities")) {
        return response({ capabilities: [{ capability_id: "guardian.research-watch.v1", version: "1", input_schema: { type: "object", properties: { watch_id: { type: "string" } } } }] });
      }
      if (url.endsWith("/api/goals/goal-1/candidate-set") && init?.method === "POST") {
        requestBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        return response(comparison);
      }
      return response({}, false, 404);
    });

    render(<WorkBoardMemoryReview
      task={task({ status: "todo", latest_attempt: null })}
      ownerPrincipalId="operator:one"
      ownerSessionId="operator-session-1"
    />);

    fireEvent.change(await screen.findByLabelText("Ordered typed candidate JSON"), {
      target: { value: JSON.stringify([
        { capability_id: "workflow.goal-snapshot-to-file", capability_version: "1", inputs: { file_path: "reports/m5.json" } },
        { capability_id: "guardian.research-watch.v1", capability_version: "1", inputs: { watch_id: "watch:m5", expected_plan_revision: 1 } },
      ]) },
    });
    fireEvent.click(screen.getByRole("button", { name: "Compare candidates" }));
    expect(await screen.findByText(/changed · accepted_memory_selected/)).toBeInTheDocument();
    expect(requestBody).toMatchObject({
      task_id: "task-1",
      expected_task_revision: 6,
      expected_goal_revision: 4,
      candidates: [
        { capability_id: "workflow.goal-snapshot-to-file", capability_version: "1" },
        { capability_id: "guardian.research-watch.v1", capability_version: "1" },
      ],
    });
    expect(await screen.findByText(/No capability was dispatched/)).toBeInTheDocument();
  });

  it("does not read or mutate memory for another authenticated owner session", () => {
    render(<WorkBoardMemoryReview task={task()} ownerPrincipalId="operator:one" ownerSessionId="another-session" />);

    expect(screen.getByText(/available only to this task’s authenticated owner session/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Review learning" })).not.toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
