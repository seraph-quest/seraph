import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { GoalLoopPanel } from "./GoalLoopPanel";
import { useQuestStore } from "../../stores/questStore";
import type { GoalInfo, GoalLoopPayload, GoalStrategyDelta } from "../../types";

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

const payload: GoalLoopPayload = {
  goal: {
    id: "g1",
    title: goal.title,
    status: "active",
    revision: 4,
  },
  criterion: goal.success_criterion ?? null,
  receipts: [
    {
      receipt_type: "outcome",
      created_at: "2026-09-09T08:00:00Z",
      execution_status: "completed",
      verification: "passed",
      usefulness: "useful",
      learning: "applied",
      artifact_ref: "artifacts/guardian.md",
      evidence_refs: ["artifact:guardian"],
    },
  ],
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
  beforeEach(() => {
    setupStore();
  });

  it("shows the four outcome axes and uses only the bounded snapshot endpoint", async () => {
    const runGoalSnapshot = vi.fn().mockResolvedValue({ status: "blocked" });
    setupStore({ runGoalSnapshot });
    render(<GoalLoopPanel goal={goal} />);

    expect(screen.getByTestId("goal-loop-panel")).toHaveAttribute("data-state", "active");
    expect(screen.getByTestId("goal-axis-execution")).toHaveTextContent("completed");
    expect(screen.getByTestId("goal-axis-verification")).toHaveTextContent("passed");
    expect(screen.getByTestId("goal-axis-usefulness")).toHaveTextContent("useful");
    expect(screen.getByTestId("goal-axis-learning")).toHaveTextContent("applied");

    fireEvent.click(screen.getByRole("button", { name: "run snapshot" }));
    await waitFor(() => expect(runGoalSnapshot).toHaveBeenCalledWith("g1", expect.objectContaining({
      expected_revision: 4,
      evidence_refs: ["artifact:guardian"],
    })));
    expect(screen.getByTestId("goal-loop-panel")).toHaveAttribute("data-state", "recovered");
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
