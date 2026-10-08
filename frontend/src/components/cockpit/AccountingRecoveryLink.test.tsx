import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import type { WorkBoardTask } from "../../types";
import { AccountingRecoveryLink } from "./AccountingRecoveryLink";

const owner = { principalId: "owner", sessionId: "root" };
const task = { task_id: "task", task_revision: 4, goal_id: "goal", goal_revision: 2, latest_attempt: { workflow_run_id: "job" } } as WorkBoardTask;
const control = { action: "settle", method: "POST", endpoint: "/api/settings/model-fabric/accounting/settle", expected_revision: 3 };
const operation = { operation_id: "operation", job_id: "job", owner_id: "owner", goal_id: "goal", goal_revision: 2, revision: 3, controls: [control] };
const fetchMock = vi.fn();
beforeEach(() => { fetchMock.mockReset(); vi.stubGlobal("fetch", fetchMock); });
afterEach(() => vi.unstubAllGlobals());
function receipt(value: unknown, ok = true) { return { ok, status: ok ? 200 : 503, json: async () => value }; }

it("reads one exact job and opens only existing accounting without constructing settlement", async () => {
  const open = vi.fn(); fetchMock.mockResolvedValue(receipt({ operations: [operation] }));
  render(<AccountingRecoveryLink task={task} owner={owner} metadataConfirmed onOpen={open} />);
  fireEvent.click(await screen.findByRole("button", { name: "Open owning cost accounting" }));
  expect(open).toHaveBeenCalledOnce(); expect(fetchMock).toHaveBeenCalledOnce();
  expect(fetchMock.mock.calls[0][0]).toContain("/api/settings/model-fabric/accounting?job_id=job");
  expect(fetchMock.mock.calls[0][1].method).toBeUndefined();
});

it.each([
  { job_id: "other" }, { owner_id: "other" }, { goal_id: "other" }, { goal_revision: 9 },
  { controls: [{ ...control, endpoint: "/api/guessed/settle" }] }, { controls: [{ ...control, expected_revision: 4 }] }, { controls: [] },
])("withholds accounting for a spliced or unadvertised row %j", async (change) => {
  fetchMock.mockResolvedValue(receipt({ operations: [{ ...operation, ...change }] }));
  render(<AccountingRecoveryLink task={task} owner={owner} metadataConfirmed onOpen={vi.fn()} />);
  await screen.findByText(/No settlement control is advertised/);
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
});

it("retains a confirmed receipt on partial metadata but removes its control", async () => {
  fetchMock.mockResolvedValue(receipt({ operations: [operation] }));
  const props = { task, owner, onOpen: vi.fn() };
  const view = render(<AccountingRecoveryLink {...props} metadataConfirmed />);
  await screen.findByRole("button"); view.rerender(<AccountingRecoveryLink {...props} metadataConfirmed={false} />);
  expect(screen.getByText(/owning accounting API advertises/)).toBeInTheDocument();
  expect(screen.queryByRole("button")).not.toBeInTheDocument(); expect(fetchMock).toHaveBeenCalledOnce();
});

it("discards a late previous-root response and keeps failed metadata visibly blocked", async () => {
  let finish!: (value: unknown) => void;
  fetchMock.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; })).mockResolvedValue(receipt({}, false));
  const view = render(<AccountingRecoveryLink task={task} owner={owner} metadataConfirmed onOpen={vi.fn()} />);
  view.rerender(<AccountingRecoveryLink task={task} owner={{ ...owner, sessionId: "new-root" }} metadataConfirmed onOpen={vi.fn()} />);
  finish(receipt({ operations: [operation] }));
  await screen.findByText(/metadata is unavailable or permission is missing/);
  await waitFor(() => expect(screen.queryByRole("button")).not.toBeInTheDocument());
});
