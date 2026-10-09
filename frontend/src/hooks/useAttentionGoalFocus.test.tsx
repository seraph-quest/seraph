import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useState } from "react";
import { appEventBus } from "../lib/appEventBus";
import { useQuestStore } from "../stores/questStore";
import { useAttentionGoalFocus } from "./useAttentionGoalFocus";

const context = vi.hoisted(() => ({ session: { principal_id: "owner", session_id: "root", absolute_expires_at: "2099-01-01T00:00:00Z", idle_expires_at: "2099-01-01T00:00:00Z" } }));
vi.mock("../components/auth/OperatorAuthGate", () => ({ useOptionalOperatorAuth: () => context }));
function Harness() {
  const [goalId, setGoalId] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [filtered, setFiltered] = useState(true);
  const [programme, setProgramme] = useState<string | null>(null);
  const status = useAttentionGoalFocus(setGoalId, () => setFiltered(false), setProgramme);
  return <><button onClick={() => { setOpen(true); appEventBus.emit("attention:inspect-goal", { principalId: "owner", sessionId: "root", goalId: "goal" }); }}>Open originating goal</button>{open && <><span>{goalId ?? "no selection"}</span><span>{programme ?? "no programme"}</span><span>{filtered ? "filtered" : "reset filters"}</span>{status && <p role="status">{status}</p>}</>}</>;
}
const goal = { id: "goal", title: "Current scoped goal", children: [] };
const fetchMock = vi.fn();
beforeEach(() => { context.session = { principal_id: "owner", session_id: "root", absolute_expires_at: "2099-01-01T00:00:00Z", idle_expires_at: "2099-01-01T00:00:00Z" }; fetchMock.mockReset(); vi.stubGlobal("fetch", fetchMock); useQuestStore.setState({ goalTree: [] }); });
afterEach(() => vi.unstubAllGlobals());

it("focuses exact freshly owned goal when opening an otherwise closed panel, with one bounded read", async () => {
  fetchMock.mockResolvedValue({ ok: true, status: 200, json: async () => [goal] });
  render(<Harness />); expect(fetchMock).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText("Open originating goal"));
  expect(await screen.findByText("goal")).toBeInTheDocument(); expect(screen.getByText("reset filters")).toBeInTheDocument();
  expect(fetchMock).toHaveBeenCalledOnce(); expect(fetchMock.mock.calls[0][0]).toContain("/api/goals/tree");
});
it("does not fall back to a retained foreign or missing goal", async () => {
  useQuestStore.setState({ goalTree: [goal as never] });
  fetchMock.mockResolvedValue({ ok: true, status: 200, json: async () => [{ ...goal, id: "different-goal" }] });
  render(<Harness />); fireEvent.click(screen.getByText("Open originating goal"));
  expect(await screen.findByText(/not present in this operator scope/)).toBeInTheDocument(); expect(screen.getByText("no selection")).toBeInTheDocument();
});
it("ignores old-root events and delayed old-root tree results after a root transition", async () => {
  let finish!: (value: unknown) => void;
  fetchMock.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
  const view = render(<Harness />); fireEvent.click(screen.getByText("Open originating goal"));
  context.session = { ...context.session, session_id: "new-root" };
  view.rerender(<Harness />);
  await act(async () => finish({ ok: true, status: 200, json: async () => [goal] }));
  expect(screen.getByText("no selection")).toBeInTheDocument();
  act(() => appEventBus.emit("attention:inspect-goal", { principalId: "owner", sessionId: "root", goalId: "goal" }));
  expect(fetchMock).toHaveBeenCalledOnce(); expect(useQuestStore.getState().goalTree).toEqual([]);
});
it("keeps history visibly read only and clears selection when root changes", async () => {
  fetchMock.mockResolvedValue({ ok: true, status: 200, json: async () => [{ ...goal, ownership_access: "recovered_read_only" }] });
  const view = render(<Harness />); fireEvent.click(screen.getByText("Open originating goal"));
  expect(await screen.findByText(/recovered history, read only/)).toBeInTheDocument();
  context.session = { ...context.session, principal_id: "other", session_id: "new-root" }; view.rerender(<Harness />);
  await waitFor(() => expect(screen.getByText("no selection")).toBeInTheDocument());
});
it.each(["expired", "invalid"])("rejects %s finite operator scope before reading goal metadata", (kind) => {
  context.session = { ...context.session, absolute_expires_at: kind === "expired" ? "2000-01-01T00:00:00Z" : "invalid" };
  render(<Harness />); fireEvent.click(screen.getByText("Open originating goal")); expect(fetchMock).not.toHaveBeenCalled(); expect(screen.getByText("no selection")).toBeInTheDocument();
});

it("does not select another Goal revision for a Home metadata target", async () => { fetchMock.mockResolvedValue({ ok: true, json: async () => [{ ...goal, revision: 3 }] }); render(<Harness />); fireEvent.click(screen.getByText("Open originating goal")); await screen.findByText("goal"); act(() => appEventBus.emit("attention:inspect-goal", { principalId: "owner", sessionId: "root", goalId: "goal", goalRevision: 2, programmeId: "exact-programme" })); await screen.findByText(/exact Goal revision changed/); expect(screen.getByText("no selection")).toBeInTheDocument(); expect(screen.getByText("no programme")).toBeInTheDocument(); });
it("forwards the exact Home programme only after current owner and Goal revision readback", async () => { fetchMock.mockResolvedValue({ ok: true, json: async () => [{ ...goal, revision: 3 }] }); render(<Harness />); fireEvent.click(screen.getByText("Open originating goal")); await screen.findByText("goal"); act(() => appEventBus.emit("attention:inspect-goal", { principalId: "owner", sessionId: "root", goalId: "goal", goalRevision: 3, programmeId: "exact-programme" })); await screen.findByText("exact-programme"); expect(fetchMock).toHaveBeenCalledTimes(2); });
