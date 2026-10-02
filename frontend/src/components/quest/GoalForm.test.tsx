import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { GoalForm } from "./GoalForm";
import { GoalUpdateError, useQuestStore } from "../../stores/questStore";

describe("GoalForm", () => {
  const createGoal = vi.fn<(...args: any[]) => Promise<void>>();
  const updateGoal = vi.fn<(...args: any[]) => Promise<void>>();

  beforeEach(() => {
    createGoal.mockReset().mockResolvedValue(undefined);
    updateGoal.mockReset().mockResolvedValue(undefined);
    useQuestStore.setState({ createGoal, updateGoal });
  });

  it("submits the existing criterion and finite reviewed budget contract", async () => {
    const onClose = vi.fn();
    render(<GoalForm onClose={onClose} />);

    fireEvent.change(screen.getByPlaceholderText("Priority title"), { target: { value: "Ship the report" } });
    fireEvent.click(screen.getByLabelText("Configure success criterion"));
    fireEvent.change(screen.getByLabelText("Criterion ID"), { target: { value: "report-ready" } });
    fireEvent.change(screen.getByLabelText("Criterion description"), { target: { value: "A verified report is readable" } });
    fireEvent.change(screen.getByLabelText("Criterion verifier"), { target: { value: "artifact_readback" } });
    fireEvent.change(screen.getByLabelText("Criterion target"), { target: { value: '{"path":"reports/latest.md"}' } });
    fireEvent.change(screen.getByLabelText("Criterion evidence references"), { target: { value: "artifact:report\nreadback:report" } });
    fireEvent.click(screen.getByLabelText("Enable standing goal observations"));
    fireEvent.click(screen.getByLabelText("I reviewed these limits and the local output scope"));
    expect(screen.getByLabelText("Reviewed grant reference")).toHaveAttribute("readonly");
    expect((screen.getByLabelText("Reviewed grant reference") as HTMLInputElement).value).toMatch(/^budget-review:/);
    fireEvent.change(screen.getByLabelText("Maximum outstanding jobs"), { target: { value: "2" } });
    fireEvent.change(screen.getByLabelText("Maximum attempts"), { target: { value: "3" } });
    fireEvent.change(screen.getByLabelText("Maximum runtime seconds"), { target: { value: "600" } });
    fireEvent.change(screen.getByLabelText("Notifications per day"), { target: { value: "4" } });
    fireEvent.change(screen.getByLabelText("Operator timezone"), { target: { value: "Europe/Warsaw" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(createGoal).toHaveBeenCalledTimes(1));
    expect(createGoal.mock.calls[0][0]).toMatchObject({
      title: "Ship the report",
      proactive_enabled: true,
      success_criterion: {
        criterion_id: "report-ready",
        description: "A verified report is readable",
        verifier_kind: "artifact_readback",
        target: { path: "reports/latest.md" },
        evidence_refs: ["artifact:report", "readback:report"],
      },
      admission_budget: {
        reviewed_grant: true,
        grant_id: expect.stringMatching(/^budget-review:/),
        max_outstanding_jobs: 2,
        max_attempts: 3,
        max_runtime_seconds: 600,
        notifications_per_day: 4,
        timezone: "Europe/Warsaw",
        quiet_hours_start: 22,
        quiet_hours_end: 8,
      },
    });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("keeps the edited draft after a stale revision response", async () => {
    updateGoal.mockRejectedValueOnce(new GoalUpdateError("goal-1", "stale", { code: "stale_goal_revision" }));
    render(
      <GoalForm
        goal={{
          id: "goal-1",
          parent_id: null,
          path: "goal-1",
          level: "weekly",
          title: "Original title",
          description: "Original description",
          status: "active",
          domain: "productivity",
          start_date: null,
          due_date: null,
          sort_order: 0,
          revision: 4,
        }}
        onClose={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByPlaceholderText("Priority title"), { target: { value: "Draft kept locally" } });
    fireEvent.click(screen.getByRole("button", { name: "Update" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("draft is still here");
    expect(screen.getByPlaceholderText("Priority title")).toHaveValue("Draft kept locally");
    expect(updateGoal).toHaveBeenCalledWith("goal-1", expect.objectContaining({ expected_revision: 4 }));
  });
});
