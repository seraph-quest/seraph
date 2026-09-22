import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SourceWatchForm } from "./SourceWatchForm";

function response(payload: unknown, ok = true) {
  return { ok, json: async () => payload };
}

describe("SourceWatchForm", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockResolvedValue(response([]));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("loads the owner watches and creates a bounded HTTPS watch for the active goal", async () => {
    fetchMock
      .mockResolvedValueOnce(response([]))
      .mockResolvedValueOnce(response({ id: "watch-1", goal_id: "goal-1" }))
      .mockResolvedValueOnce(response([{ id: "watch-1", goal_id: "goal-1", goal_revision: 2, plan_revision: 1, state: "active", write_mode: "approval_each_run", sources: [{ target: "https://example.org/updates.txt" }], baselines: [] }]));

    render(<SourceWatchForm goal={{ id: "goal-1", title: "Ship the guardian slice", revision: 2 }} />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/api/capabilities/source-watches"),
      expect.objectContaining({ credentials: "include" }),
    ));

    fireEvent.change(screen.getByLabelText("Guardian source"), { target: { value: "https://example.org/updates.txt" } });
    fireEvent.click(screen.getByRole("button", { name: "Add watch" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/api/capabilities/source-watches"),
      expect.objectContaining({ method: "POST" }),
    ));
    const [, init] = fetchMock.mock.calls[1] as [RequestInfo, RequestInit];
    expect(JSON.parse(String(init.body))).toMatchObject({
      goal_id: "goal-1",
      expected_goal_revision: 2,
      write_mode: "approval_each_run",
      schedule: { cron: "*/15 * * * *", timezone: "UTC" },
    });
  });

  it("shows blocked operator state when the backend refuses a watch", async () => {
    fetchMock
      .mockResolvedValueOnce(response([]))
      .mockResolvedValueOnce(response({ detail: { code: "goal_budget_missing_reviewed_grant" } }, false));

    render(<SourceWatchForm goal={{ id: "goal-1", title: "Ship the guardian slice", revision: 2 }} />);
    fireEvent.change(screen.getByLabelText("Guardian source"), { target: { value: "notes/plan.md" } });
    fireEvent.click(screen.getByRole("button", { name: "Add watch" }));
    expect(await screen.findByRole("status")).toHaveTextContent("goal_budget_missing_reviewed_grant");
  });
});
