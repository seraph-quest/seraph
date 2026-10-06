import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GoalLoopPanel } from "./GoalLoopPanel";
import { GoalLoopReceiptDetails } from "./GoalLoopReceiptDetails";
import { normalizeGoalLoopReceipt, useQuestStore } from "../../stores/questStore";
import type { GoalInfo, GoalLoopPayload, GoalLoopReceipt, GoalStrategyDelta } from "../../types";

const goal: GoalInfo = {
  id: "g1",
  parent_id: null,
  path: "/g1",
  level: "weekly",
  title: "Ship guardian slice",
  description: null,
  status: "active",
  domain: "productivity",
  start_date: null,
  due_date: null,
  sort_order: 0,
  revision: 4,
  success_criterion: {
    criterion_id: "artifact",
    description: "A verified artifact exists",
    verifier_kind: "artifact_readback",
    target: {
      query: "guardian evidence",
      file_path: "artifacts/guardian.md",
      priority: 4,
    },
    evidence_refs: ["artifact:guardian"],
  },
};

const candidateReceipt: GoalLoopReceipt = {
  audit_event_id: "audit-candidate",
  event_type: "goal_loop_candidate",
  receipt_version: "goal_conditioned_loop_v1",
  receipt_type: "candidate",
  proposal_only: true,
  candidate_id: "candidate-1",
  dedupe_key: `gcl:${"d".repeat(32)}`,
  goal_id: "g1",
  goal_revision: 4,
  criterion_id: "artifact",
  action: "act",
  capability_id: "workflow.goal-snapshot-to-file",
  capability_version: "1",
  input_keys: ["evidence_refs", "file_path"],
  input_digest: "a".repeat(64),
  strategy_delta_id: null,
  strategy_delta_provenance: "not_present",
  expected_outcome: "A verified artifact exists",
  expires_at: "2026-09-10T08:00:00Z",
  created_at: "2026-09-09T07:59:00Z",
  reason: "goal snapshot proposed",
  evidence_refs: ["artifact:guardian"],
  content_redacted: true,
};

const outcomeReceipt: GoalLoopReceipt = {
  audit_event_id: "audit-outcome",
  event_type: "goal_loop_outcome",
  receipt_version: "goal_conditioned_loop_v1",
  receipt_type: "outcome",
  outcome_id: "outcome-1",
  candidate_id: "candidate-1",
  dedupe_key: `gcl:${"d".repeat(32)}`,
  decision_input_digest: "b".repeat(64),
  strategy_delta_id: null,
  strategy_delta_provenance: "not_present",
  goal_id: "g1",
  goal_revision: 4,
  execution_status: "succeeded",
  verification: "passed",
  usefulness: "helpful",
  learning: "applied",
  learning_record_id: "learning-1",
  artifact_ref: "artifacts/guardian.md",
  evidence_refs: ["artifact:guardian"],
  capability_id: "workflow.goal-snapshot-to-file",
  reason: "goal snapshot read back",
  created_at: "2026-09-09T08:00:00Z",
  content_redacted: true,
};

const payload: GoalLoopPayload = {
  goal: {
    id: "g1",
    title: goal.title,
    status: "active",
    revision: 4,
  },
  criterion: goal.success_criterion ?? null,
  receipts: [outcomeReceipt],
  strategy_deltas: [],
};

const appliedDelta: GoalStrategyDelta = {
  delta_id: "delta-1",
  goal_id: "g1",
  scope: "goal",
  field_name: "web_brief_target",
  before: { query: "old" },
  after: { query: "new" },
  source_event_id: "event-1",
  author_id: "operator-1",
  evaluator_id: null,
  goal_revision_before: 3,
  goal_revision_after: 4,
  status: "applied",
  rollback_target_id: null,
  reason: "operator correction",
  created_at: "2026-09-09T08:00:00Z",
  updated_at: "2026-09-09T08:00:00Z",
};

function setupStore(overrides: Partial<ReturnType<typeof useQuestStore.getState>> = {}) {
  useQuestStore.setState({
    goalLoop: payload,
    goalLoopGoalId: "g1",
    goalLoopLoading: false,
    goalLoopError: null,
    goalLoopAction: null,
    loadGoalLoop: vi.fn().mockResolvedValue(undefined),
    runGoalSnapshot: vi.fn().mockResolvedValue({ status: "blocked" }),
    applyStrategyCorrection: vi.fn().mockResolvedValue({ status: "applied" }),
    rollbackStrategyCorrection: vi.fn().mockResolvedValue({ status: "rolled_back" }),
    updateGoal: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  });
}

describe("GoalLoopPanel", () => {
  afterEach(() => { vi.unstubAllGlobals(); });
  beforeEach(() => {
    setupStore();
  });

  it("loads a bounded Goal-scoped history only on explicit inspection and retains it through partial failures", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce({ ok: true, status: 200, json: async () => [] });
    vi.stubGlobal("fetch", fetchMock);
    render(<GoalLoopPanel goal={goal} />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(fetchMock.mock.calls[0][0]).toContain("/api/capabilities/source-watches");
    fetchMock.mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ items: [{
      id: "unknown-1", revision: 2, state: "unknown", source_kind: "guardian_opportunity", source_id: "unknown-1",
      opportunity_id: "unknown-1", opportunity_revision: 2, opportunity_status: "unknown", assessment: null,
      title: "Assessment history", summary: "Assessment contact uncertain", why_now: "outcome_unknown",
      goal_id: "g1", goal_revision: 4, watch_id: "watch-1", plan_revision: 1, expires_at: "2026-10-07T12:00:00Z",
      evidence_refs: [], allowed_actions: [], reason_code: "outcome_unknown",
    }], next_cursor: "opaque-next" }) });
    fireEvent.click(screen.getByRole("button", { name: "Review assessment history" }));
    await screen.findByText("unknown · Assessment contact uncertain");
    expect(fetchMock.mock.calls[1][0]).toContain("/api/guardian/opportunities?goal_id=g1&limit=20");
    expect(screen.getByText(/Outcome Unknown; retained inference liability/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "More assessment history" })).toBeEnabled();
    fetchMock.mockRejectedValueOnce(new Error("History temporarily unavailable"));
    fireEvent.click(screen.getByRole("button", { name: "More assessment history" }));
    await screen.findByText(/Last-known assessment history retained/);
    expect(screen.getByText("unknown · Assessment contact uncertain")).toBeInTheDocument();
    expect(fetchMock.mock.calls[2][0]).toContain("cursor=opaque-next");
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
  });

  it.each([
    { status: "blocked", reason: "source_excerpt_unavailable", policy: "source_stale" },
    { status: "proposed", reason: "goal_review_required", policy: "goal_review_required" },
    { status: "proposed", reason: "source_stale", policy: "source_stale" },
    { status: "silent", reason: "assessment_abstained", policy: null },
  ])("retains $status history reason $reason and the live $policy policy without proposing actions", async ({ status, reason, policy }) => {
    const fetchMock = vi.fn().mockResolvedValueOnce({ ok: true, status: 200, json: async () => [] });
    vi.stubGlobal("fetch", fetchMock);
    render(<GoalLoopPanel goal={goal} />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    // With no stored reason, the API projects the current policy reason for proposed rows.
    fetchMock.mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ items: [{
      id: "recovered-1", revision: 2, state: status, source_kind: "guardian_opportunity", source_id: "recovered-1",
      opportunity_id: "recovered-1", opportunity_revision: 2, opportunity_status: status, assessment: null,
      title: "Assessment history", summary: "Recovered assessment", why_now: reason,
      goal_id: "g1", goal_revision: 4, watch_id: "watch-1", plan_revision: 1,
      expires_at: "2026-10-07T12:00:00Z", evidence_refs: [], evidence_status: "unavailable",
      allowed_actions: [], reason_code: reason, policy_reason: policy,
    }] }) });
    fireEvent.click(screen.getByRole("button", { name: "Review assessment history" }));
    const summary = await screen.findByText(`${status} · Recovered assessment`);
    const history = summary.closest("article")!;
    expect(within(history).getByText(`${reason} · Goal revision 4`)).toBeInTheDocument();
    if (policy && policy !== reason) expect(within(history).getByText(`Current policy: ${policy}`)).toBeInTheDocument();
    else expect(within(history).queryByText(/^Current policy:/)).not.toBeInTheDocument();
    expect(within(history).getByText("No proposed intervention. No learning recorded.")).toBeInTheDocument();
    expect(within(history).queryByRole("button")).not.toBeInTheDocument();
    expect(fetchMock.mock.calls[1][0]).toContain("/api/guardian/opportunities?goal_id=g1&limit=20");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("shows the invalidated Goal policy while other loop metadata is degraded", async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => [] });
    vi.stubGlobal("fetch", fetchMock);
    setupStore({ goalLoopError: { status: 503, code: "metadata_unavailable", message: "Loop metadata unavailable", payload: null } });
    render(<GoalLoopPanel goal={{ ...goal, guardian_assessment_state: "goal_review_required", guardian_policy_revision: 2 }} />);
    expect(screen.getByText(/Goal review required. Review\/rebind watches/)).toBeInTheDocument();
    expect(screen.getByLabelText("Enable bounded public opportunity assessments")).toBeEnabled();
    expect(screen.getByRole("button", { name: "Refresh policy metadata" })).toBeEnabled();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
  });

  it("shows the four outcome axes and uses only the bounded snapshot endpoint", async () => {
    const runGoalSnapshot = vi.fn().mockResolvedValue({ status: "blocked" });
    setupStore({ runGoalSnapshot });
    render(<GoalLoopPanel goal={goal} />);

    expect(screen.getByTestId("goal-loop-panel")).toHaveAttribute("data-state", "active");
    expect(screen.getByTestId("goal-axis-execution")).toHaveTextContent("succeeded");
    expect(screen.getByTestId("goal-axis-verification")).toHaveTextContent("passed");
    expect(screen.getByTestId("goal-axis-usefulness")).toHaveTextContent("helpful");
    expect(screen.getByTestId("goal-axis-learning")).toHaveTextContent("applied");

    fireEvent.click(screen.getByRole("button", { name: "run snapshot" }));
    await waitFor(() => expect(runGoalSnapshot).toHaveBeenCalledWith("g1", expect.objectContaining({
      expected_revision: 4,
      evidence_refs: ["artifact:guardian"],
    })));
    expect(screen.getByTestId("goal-loop-panel")).toHaveAttribute("data-state", "recovered");
  });

  it("exposes separate candidate and outcome contracts through a keyboard disclosure", async () => {
    const user = userEvent.setup();
    expect(normalizeGoalLoopReceipt(candidateReceipt)).not.toBeNull();
    expect(normalizeGoalLoopReceipt(outcomeReceipt)).not.toBeNull();
    const { unmount } = render(<GoalLoopReceiptDetails receipt={candidateReceipt} />);

    const candidateDetails = screen.getByTestId("goal-loop-receipt-details");
    const candidateSummary = screen.getByText("Inspect exact backend receipt fields");
    expect(candidateDetails).not.toHaveAttribute("open");
    candidateSummary.focus();
    expect(document.activeElement).toBe(candidateSummary);
    await user.keyboard("{Enter}");

    expect(candidateDetails).toHaveAttribute("open");
    expect(screen.getByTestId("goal-loop-receipt-audit-event-id")).toHaveTextContent("audit-candidate");
    expect(screen.getByTestId("goal-loop-receipt-proposal-only")).toHaveTextContent("true");
    expect(screen.getByTestId("goal-loop-receipt-action")).toHaveTextContent("act");
    expect(screen.getByTestId("goal-loop-receipt-capability-id")).toHaveTextContent("workflow.goal-snapshot-to-file");
    expect(screen.getByTestId("goal-loop-receipt-input-digest")).toHaveTextContent("a".repeat(64));
    expect(screen.getByTestId("goal-loop-receipt-evidence-refs")).toHaveTextContent("artifact:guardian");
    expect(screen.getByTestId("goal-loop-receipt-expires-at")).toHaveTextContent("2026-09-10T08:00:00Z");

    await user.keyboard(" ");
    expect(candidateDetails).not.toHaveAttribute("open");

    unmount();
    render(<GoalLoopReceiptDetails receipt={outcomeReceipt} />);
    const outcomeDetails = screen.getByTestId("goal-loop-receipt-details");
    const outcomeSummary = screen.getByText("Inspect exact backend receipt fields");
    outcomeSummary.focus();
    await user.keyboard("{Enter}");

    expect(outcomeDetails).toHaveAttribute("open");
    expect(screen.getByTestId("goal-loop-receipt-decision-input-digest")).toHaveTextContent("b".repeat(64));
    expect(screen.getByTestId("goal-loop-receipt-execution-status")).toHaveTextContent("succeeded");
    expect(screen.getByTestId("goal-loop-receipt-usefulness")).toHaveTextContent("helpful");
    expect(screen.getByTestId("goal-loop-receipt-proposal-only")).toHaveTextContent("unknown");
    expect(screen.getByTestId("goal-loop-receipt-action")).toHaveTextContent("unknown");
    expect(screen.getByTestId("goal-loop-receipt-input-digest")).toHaveTextContent("unknown");
    expect(screen.getByTestId("goal-loop-receipt-capability-version")).toHaveTextContent("unknown");
    expect(screen.getByTestId("goal-loop-receipt-expected-outcome")).toHaveTextContent("unknown");
    expect(screen.getByTestId("goal-loop-receipt-expires-at")).toHaveTextContent("unknown");
  });

  it.each([
    ["false", { ...outcomeReceipt, content_redacted: false }],
    ["absent", (({ content_redacted: _redacted, ...receipt }) => receipt)(outcomeReceipt)],
  ] as const)("withholds receipt fields when content_redacted is %s", (_label, receipt) => {
    render(<GoalLoopReceiptDetails receipt={receipt} />);

    fireEvent.click(screen.getByText("Inspect exact backend receipt fields"));

    expect(screen.getByTestId("goal-loop-receipt-withheld")).toBeInTheDocument();
    expect(screen.queryByTestId("goal-loop-receipt-reason")).not.toBeInTheDocument();
    expect(screen.queryByText("goal snapshot read back")).not.toBeInTheDocument();
  });

  it.each([
    ["false", { ...outcomeReceipt, content_redacted: false }],
    ["absent", (({ content_redacted: _redacted, ...receipt }) => receipt)(outcomeReceipt)],
  ] as const)("rejects a %s content_redacted receipt during runtime normalization", (_label, receipt) => {
    expect(normalizeGoalLoopReceipt(receipt)).toBeNull();
  });

  it.each([
    ["false", { ...outcomeReceipt, content_redacted: false }],
    ["absent", (({ content_redacted: _redacted, ...receipt }) => receipt)(outcomeReceipt)],
  ] as const)("fails closed for a %s content_redacted flag before exposing receipt text", (_label, receipt) => {
    const runGoalSnapshot = vi.fn().mockResolvedValue({ status: "blocked" });
    setupStore({
      goalLoop: { ...payload, receipts: [receipt] },
      runGoalSnapshot,
    });
    render(<GoalLoopPanel goal={goal} onEdit={vi.fn()} />);

    expect(screen.getByTestId("goal-loop-panel")).toHaveAttribute("data-state", "partial_metadata");
    expect(screen.queryByText("goal snapshot read back")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "run snapshot" })).toBeDisabled();
    expect(screen.queryByTestId("goal-loop-receipt-details")).not.toBeInTheDocument();
    expect(runGoalSnapshot).not.toHaveBeenCalled();
  });

  it.each([
    ["candidate", { ...candidateReceipt, candidate_id: undefined }],
    ["outcome", { ...outcomeReceipt, outcome_id: undefined }],
  ] as const)("marks an incomplete %s receipt as partial and disables effects", (_label, receipt) => {
    const runGoalSnapshot = vi.fn().mockResolvedValue({ status: "blocked" });
    expect(normalizeGoalLoopReceipt(receipt)).toBeNull();
    setupStore({
      goalLoop: { ...payload, receipts: [receipt] },
      runGoalSnapshot,
    });
    render(<GoalLoopPanel goal={goal} />);

    expect(screen.getByTestId("goal-loop-panel")).toHaveAttribute("data-state", "partial_metadata");
    expect(screen.getByText(/A bounded success criterion or receipt field is missing/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "run snapshot" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "run snapshot" }));
    expect(runGoalSnapshot).not.toHaveBeenCalled();
  });

  it("binds pause, correction, and rollback to their existing bounded controls", async () => {
    const updateGoal = vi.fn().mockResolvedValue(undefined);
    const applyStrategyCorrection = vi.fn().mockResolvedValue({ status: "applied" });
    const rollbackStrategyCorrection = vi.fn().mockResolvedValue({ status: "rolled_back" });
    setupStore({
      updateGoal,
      applyStrategyCorrection,
      rollbackStrategyCorrection,
      goalLoop: {
        ...payload,
        strategy_deltas: [{
          delta_id: "delta-1",
          goal_id: "g1",
          scope: "goal",
          field_name: "web_brief_target",
          before: { query: "old" },
          after: { query: "new" },
          source_event_id: "event-1",
          author_id: "operator-1",
          evaluator_id: null,
          goal_revision_before: 3,
          goal_revision_after: 4,
          status: "applied",
          rollback_target_id: null,
          reason: "operator correction",
          created_at: "2026-09-09T08:00:00Z",
          updated_at: "2026-09-09T08:00:00Z",
        }],
      },
    });
    render(<GoalLoopPanel goal={goal} />);

    fireEvent.click(screen.getByRole("button", { name: "pause" }));
    await waitFor(() => expect(updateGoal).toHaveBeenCalledWith("g1", { status: "paused", expected_revision: 4 }));

    fireEvent.change(screen.getByRole("textbox", { name: "Replacement query" }), { target: { value: "new guardian evidence" } });
    fireEvent.click(screen.getByRole("button", { name: "apply correction" }));
    await waitFor(() => expect(applyStrategyCorrection).toHaveBeenCalledWith("g1", expect.objectContaining({
      expected_revision: 4,
      query: "new guardian evidence",
    })));

    fireEvent.click(screen.getByRole("button", { name: "rollback" }));
    await waitFor(() => expect(rollbackStrategyCorrection).toHaveBeenCalledWith("g1", "delta-1", {
      expected_revision: 4,
      reason: "Operator rolled back the bounded strategy correction.",
    }));
  });

  it("renders stale state and disables every effectful control", () => {
    setupStore({
      goalLoop: { ...payload, goal: { ...payload.goal, revision: 3 } },
    });
    render(<GoalLoopPanel goal={goal} onEdit={vi.fn()} />);

    expect(screen.getByTestId("goal-loop-panel")).toHaveAttribute("data-state", "stale");
    expect(screen.getByRole("button", { name: "run snapshot" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "pause" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "edit" })).toBeDisabled();
  });

  it("renders unauthorized state and retains the last-known evidence", () => {
    setupStore({
      goalLoopError: {
        status: 401,
        code: "authentication_required",
        message: "Authentication is required.",
        payload: null,
      },
    });
    render(<GoalLoopPanel goal={goal} />);

    expect(screen.getByTestId("goal-loop-panel")).toHaveAttribute("data-state", "unauthorized");
    expect(screen.getByText("Authentication is required.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "run snapshot" })).toBeDisabled();
  });

  it("keeps retained evidence read-only while loop data is loading", () => {
    setupStore({ goalLoopLoading: true, goalLoop: { ...payload, strategy_deltas: [appliedDelta] } });
    render(<GoalLoopPanel goal={goal} onEdit={vi.fn()} />);

    expect(screen.getByTestId("goal-loop-panel")).toHaveAttribute("data-state", "loading");
    expect(screen.getByRole("button", { name: "edit" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "pause" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "run snapshot" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "apply correction" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "rollback" })).toBeDisabled();
  });

  it.each([
    ["blocked", "blocked"],
    ["failed", "failed"],
    ["awaiting_approval", "awaiting_approval"],
  ] as const)("renders %s from the latest receipt without claiming success", (state, executionStatus) => {
    setupStore({
      goalLoop: {
        ...payload,
        receipts: [{ ...payload.receipts[0], execution_status: executionStatus }],
      },
    });
    render(<GoalLoopPanel goal={goal} />);
    expect(screen.getByTestId("goal-loop-panel")).toHaveAttribute("data-state", state);
  });

  it("renders degraded state while retaining last-known evidence", () => {
    setupStore({
      goalLoopError: {
        status: 503,
        code: "runtime_unavailable",
        message: "The governed runtime is unavailable.",
        payload: null,
      },
    });
    render(<GoalLoopPanel goal={goal} onEdit={vi.fn()} />);

    expect(screen.getByTestId("goal-loop-panel")).toHaveAttribute("data-state", "degraded");
    expect(screen.getByText("The governed runtime is unavailable.")).toBeInTheDocument();
    expect(screen.getByTestId("goal-axis-verification")).toHaveTextContent("passed");
    expect(screen.getByRole("button", { name: "edit" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "pause" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "run snapshot" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "apply correction" })).toBeDisabled();
  });

  it.each([
    [500, "degraded"],
    [422, "degraded"],
    [404, "failed"],
  ] as const)("fails closed for a retained payload after HTTP %s loop retrieval", (status, state) => {
    setupStore({
      goalLoopError: {
        status,
        code: status === 404 ? "goal_not_found" : "loop_unavailable",
        message: `Loop request failed with HTTP ${status}.`,
        payload: null,
      },
    });
    const onEdit = vi.fn();
    render(<GoalLoopPanel goal={goal} onEdit={onEdit} />);

    expect(screen.getByTestId("goal-loop-panel")).toHaveAttribute("data-state", state);
    expect(screen.getByTestId("goal-loop-read-only")).toHaveTextContent("Last-known evidence retained");
    expect(screen.getByRole("button", { name: "edit" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "pause" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "run snapshot" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "apply correction" })).toBeDisabled();
    expect(onEdit).not.toHaveBeenCalled();
  });

  it("keeps a receipt-level failed outcome retryable when retrieval succeeded", () => {
    setupStore({
      goalLoop: {
        ...payload,
        receipts: [{ ...payload.receipts[0], execution_status: "failed" }],
      },
    });
    render(<GoalLoopPanel goal={goal} onEdit={vi.fn()} />);

    expect(screen.getByTestId("goal-loop-panel")).toHaveAttribute("data-state", "failed");
    expect(screen.getByRole("button", { name: "edit" })).not.toBeDisabled();
    expect(screen.getByRole("button", { name: "pause" })).not.toBeDisabled();
    expect(screen.getByRole("button", { name: "run snapshot" })).not.toBeDisabled();
  });

  it("renders malformed receipt metadata as partial and never renders object values", () => {
    const malformedReceipt = {
      ...payload.receipts[0],
      execution_status: { value: "failed" },
      reason: { detail: "unsafe" },
      artifact_ref: { path: "unsafe" },
      audit_event_id: { id: "unsafe" },
      created_at: { timestamp: "unsafe" },
    } as unknown as GoalLoopPayload["receipts"][number];
    setupStore({
      goalLoop: { ...payload, receipts: [malformedReceipt] },
    });

    render(<GoalLoopPanel goal={goal} onEdit={vi.fn()} />);

    expect(screen.getByTestId("goal-loop-panel")).toHaveAttribute("data-state", "partial_metadata");
    expect(screen.getByTestId("goal-axis-execution")).toHaveTextContent("unknown");
    expect(screen.queryByText("[object Object]")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "edit" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "run snapshot" })).toBeDisabled();
  });

  it("renders malformed criterion metadata as partial and disables effects", () => {
    setupStore({
      goalLoop: {
        ...payload,
        criterion: { ...payload.criterion, evidence_refs: null } as unknown as GoalLoopPayload["criterion"],
      },
    });
    render(<GoalLoopPanel goal={goal} onEdit={vi.fn()} />);

    expect(screen.getByTestId("goal-loop-panel")).toHaveAttribute("data-state", "partial_metadata");
    expect(screen.getByText("No bounded success criterion is configured.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "edit" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "pause" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "run snapshot" })).toBeDisabled();
  });

  it("keeps an approval-pending loop informational and disables every effect", () => {
    setupStore({
      goalLoop: {
        ...payload,
        receipts: [{ ...payload.receipts[0], execution_status: "awaiting_approval" }],
      },
    });
    render(<GoalLoopPanel goal={goal} onEdit={vi.fn()} />);

    expect(screen.getByTestId("goal-loop-panel")).toHaveAttribute("data-state", "awaiting_approval");
    expect(screen.getByRole("button", { name: "edit" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "pause" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "run snapshot" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "apply correction" })).toBeDisabled();
  });

  it("renders empty and partial metadata states explicitly", () => {
    const { rerender } = render(<GoalLoopPanel />);
    expect(screen.getByTestId("goal-loop-panel")).toHaveAttribute("data-state", "empty");
    expect(screen.getByText(/Select a priority/)).toBeInTheDocument();

    act(() => {
      setupStore({
        goalLoop: { ...payload, criterion: null },
      });
    });
    rerender(<GoalLoopPanel goal={goal} />);
    expect(screen.getByTestId("goal-loop-panel")).toHaveAttribute("data-state", "partial_metadata");
    expect(screen.getByText(/No bounded success criterion/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "pause" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "run snapshot" })).toBeDisabled();
  });
});
