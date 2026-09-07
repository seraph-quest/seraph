import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { GoalTree } from "./GoalTree";
import { GoalUpdateError, useQuestStore } from "../../stores/questStore";

describe("GoalTree", () => {
  beforeEach(() => {
    useQuestStore.setState({
      updateGoal: vi.fn().mockResolvedValue(undefined),
      deleteGoal: vi.fn().mockResolvedValue(undefined),
    });
  });

  it("shows the bounded success criterion and evidence count", () => {
    render(
      <GoalTree
        depth={0}
        goals={[{
          id: "g1",
          parent_id: null,
          path: "/",
          level: "weekly",
          title: "Ship guardian slice",
          description: null,
          status: "active",
          domain: "productivity",
          start_date: null,
          due_date: null,
          sort_order: 0,
          revision: 3,
          success_criterion: {
            criterion_id: "artifact",
            description: "A verified artifact exists",
            verifier_kind: "artifact_readback",
            target: "workspace/receipt.json",
            evidence_refs: ["artifact:receipt"],
          },
        }]}
      />,
    );

    const criterion = screen.getByTestId("goal-criterion-g1");
    expect(criterion).toHaveTextContent("A verified artifact exists");
    expect(criterion).toHaveTextContent("1 evidence ref");
  });

  it("shows a stale revision error instead of leaving a rejected status update unhandled", async () => {
    const updateGoal = vi.fn().mockRejectedValue(
      new GoalUpdateError("g1", "Refresh the goal.", { code: "stale_goal_revision", currentRevision: 2 }),
    );
    useQuestStore.setState({ updateGoal });
    render(
      <GoalTree
        depth={0}
        goals={[{
          id: "g1",
          parent_id: null,
          path: "/",
          level: "weekly",
          title: "Ship guardian slice",
          description: null,
          status: "active",
          domain: "productivity",
          start_date: null,
          due_date: null,
          sort_order: 0,
          revision: 1,
        }]}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "[ ]" }));
    expect(await screen.findByRole("status")).toHaveTextContent("changed elsewhere");
    expect(updateGoal).toHaveBeenCalledWith("g1", { status: "completed", expected_revision: 1 });
  });
});
