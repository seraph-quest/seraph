import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SourceWatchPanel } from "./SourceWatchPanel";

function response(payload: unknown, ok = true) {
  return { ok, json: async () => payload };
}

describe("SourceWatchPanel", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("uses loaded goal labels and revisions instead of asking the operator for IDs", async () => {
    fetchMock
      .mockResolvedValueOnce(response([{
        id: "goal-1",
        title: "Ship the report",
        revision: 6,
        status: "active",
        children: [],
      }]))
      .mockResolvedValueOnce(response([]));

    render(<SourceWatchPanel />);
    expect(await screen.findByRole("combobox", { name: "Guardian goal" })).toHaveValue("goal-1");
    expect(screen.queryByLabelText("Guardian goal id")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Guardian goal revision")).not.toBeInTheDocument();
    expect(screen.getByText(/Ship the report · revision 6/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Guardian source"), { target: { value: "notes/plan.md" } });
    fireEvent.click(screen.getByRole("button", { name: "Add watch" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => (init as RequestInit | undefined)?.method === "POST")).toBe(true));
    const postCall = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "POST");
    expect(JSON.parse(String((postCall?.[1] as RequestInit).body))).toMatchObject({
      goal_id: "goal-1",
      expected_goal_revision: 6,
      schedule: { cron: "0 * * * *", timezone: expect.any(String) },
    });
  });
});
