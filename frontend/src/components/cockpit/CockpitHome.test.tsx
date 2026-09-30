import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CockpitHome } from "./CockpitHome";

function response(payload: unknown, ok = true, status = ok ? 200 : 503) {
  return { ok, status, json: async () => payload };
}

describe("CockpitHome", () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  it("loads only the bounded Home projections and renders operator links", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/goals/dashboard")) return Promise.resolve(response({ active_count: 2, completed_count: 1, total_count: 3, domains: {} }));
      if (url.includes("/api/work-board/tasks")) return Promise.resolve(response({ tasks: [
        { id: "task-1", title: "Review packet", status: "triage" },
        { id: "task-2", title: "Blocked task", status: "blocked" },
        { id: "task-3", title: "Running task", status: "running" },
        { id: "task-4", title: "Review task", status: "review" },
      ], next_after: null }));
      if (url.includes("/api/approvals/pending")) return Promise.resolve(response({ approvals: [{ id: "approval-1" }] }));
      if (url.includes("/api/guardian/inbox?")) return Promise.resolve(response({ items: [{ id: "candidate-1", revision: 1, state: "pending", title: "Review candidate", summary: "summary", why_now: "now", goal_id: "goal-1", goal_revision: 1, watch_id: "watch-1", plan_revision: 1, source_kind: "source_packet", source_id: "source-1", expires_at: "2030-01-01T00:00:00Z", evidence_refs: [], allowed_actions: [] }], next_cursor: null }));
      if (url.includes("/api/observer/continuity")) return Promise.resolve(response({ continuity_health: "ready" }));
      if (url.includes("/api/runtime/status")) return Promise.resolve(response({ effective_runtime: { summary_label: "OpenRouter · governed" }, status: "ready" }));
      return Promise.resolve(response({}));
    });
    render(<CockpitHome onOpenSection={vi.fn()} goalSummary={{ title: "Ship operator cockpit", status: "active", criterion: "A verified task receipt exists" }} />);
    expect(await screen.findByText("OpenRouter · governed")).toBeInTheDocument();
    expect(screen.getByText("Review candidate · pending")).toBeInTheDocument();
    expect(screen.getByText(/A verified task receipt exists/)).toBeInTheDocument();
    expect(screen.getByText("1 / 1")).toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([input]) => String(input).includes("/api/operator/")).length).toBe(0);
    expect(fetchMock.mock.calls.some(([, init]) => (init as RequestInit | undefined)?.method === "POST")).toBe(false);
  });

  it("keeps the Home surface usable when one projection is degraded", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/runtime/status")) return Promise.resolve(response({ status: "blocked" }, false));
      if (url.includes("/api/guardian/inbox?")) return Promise.resolve(response({ items: [
        { id: "accepted-1", revision: 1, state: "accepted", title: "Accepted", summary: "", why_now: "", goal_id: "goal-1", goal_revision: 1, watch_id: "watch-1", plan_revision: 1, source_kind: "source_packet", source_id: "source-1", expires_at: "2030-01-01T00:00:00Z", evidence_refs: [], allowed_actions: [] },
      ], next_cursor: null }));
      if (url.includes("/api/approvals/pending")) return Promise.resolve(response([]));
      if (url.includes("/api/work-board/tasks")) return Promise.resolve(response({ tasks: [], next_after: null }));
      if (url.includes("/api/goals/dashboard")) return Promise.resolve(response({ active_count: 0, total_count: 0, completed_count: 0, domains: {} }));
      if (url.includes("/api/observer/continuity")) return Promise.resolve(response({}));
      return Promise.resolve(response({}));
    });
    render(<CockpitHome onOpenSection={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Some Home sections are unavailable"));
    expect(screen.getByText("unavailable")).toBeInTheDocument();
  });

  it("does not present failed initial work, inbox, or approval reads as zero", async () => {
    fetchMock.mockResolvedValue(response({ detail: { code: "home_unavailable" } }, false, 503));
    render(<CockpitHome onOpenSection={vi.fn()} />);

    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Some Home sections are unavailable"));
    const workMetric = screen.getByText(/running \/ queued work on this page/).closest("button");
    const inboxMetric = screen.getByText(/pending inbox items on this page/).closest("button");
    const approvalsMetric = screen.getByText(/pending approvals on this page/).closest("button");
    expect(workMetric).not.toBeNull();
    expect(inboxMetric).not.toBeNull();
    expect(approvalsMetric).not.toBeNull();
    expect(within(workMetric as HTMLElement).getByText("—")).toBeInTheDocument();
    expect(within(inboxMetric as HTMLElement).getByText("—")).toBeInTheDocument();
    expect(within(approvalsMetric as HTMLElement).getByText("—")).toBeInTheDocument();
    expect(screen.getByText("Inbox data unavailable.")).toBeInTheDocument();
    expect(screen.getByText("Work data unavailable.")).toBeInTheDocument();
    expect(screen.getByText("last confirmed · unavailable")).toBeInTheDocument();
  });

  it("retains confirmed counts and labels them last confirmed after a partial refresh", async () => {
    let phase = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (phase > 0 && (url.includes("/api/work-board/tasks") || url.includes("/api/approvals/pending") || url.includes("/api/guardian/inbox?"))) {
        return Promise.resolve(response({ detail: { code: "projection_unavailable" } }, false, 503));
      }
      if (url.includes("/api/goals/dashboard")) return Promise.resolve(response({ active_count: 1, completed_count: 0, total_count: 1, domains: {} }));
      if (url.includes("/api/work-board/tasks")) return Promise.resolve(response({ tasks: [
        { id: "task-running", title: "Running", status: "running" },
        { id: "task-ready", title: "Ready", status: "ready" },
      ], next_after: null }));
      if (url.includes("/api/approvals/pending")) return Promise.resolve(response({ approvals: [{ id: "approval-1" }] }));
      if (url.includes("/api/guardian/inbox?")) return Promise.resolve(response({ items: [{ id: "candidate-1", revision: 1, state: "pending", title: "Review candidate", summary: "summary", why_now: "now", goal_id: "goal-1", goal_revision: 1, watch_id: "watch-1", plan_revision: 1, source_kind: "source_packet", source_id: "source-1", expires_at: "2030-01-01T00:00:00Z", evidence_refs: [], allowed_actions: [] }], next_cursor: null }));
      if (url.includes("/api/observer/continuity")) return Promise.resolve(response({ continuity_health: "ready" }));
      if (url.includes("/api/runtime/status")) return Promise.resolve(response({ effective_runtime: { summary_label: "OpenRouter · governed" }, status: "ready" }));
      return Promise.resolve(response({}));
    });
    render(<CockpitHome onOpenSection={vi.fn()} />);
    expect(await screen.findByText("OpenRouter · governed")).toBeInTheDocument();
    phase = 1;
    fireEvent.click(screen.getByRole("button", { name: "Refresh Home" }));

    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Showing last confirmed Home data"));
    const workMetric = screen.getByText(/running \/ queued work on this page/).closest("button");
    expect(within(workMetric as HTMLElement).getByText("1 / 1")).toBeInTheDocument();
    expect(screen.getByText(/running \/ queued work on this page · last confirmed/)).toBeInTheDocument();
    expect(screen.getByText(/pending inbox items on this page · last confirmed/)).toBeInTheDocument();
    expect(screen.getByText(/pending approvals on this page · last confirmed/)).toBeInTheDocument();
    expect(screen.getByText("Review candidate · pending")).toBeInTheDocument();
  });

  it("resets the retry backoff before a manual Home refresh", async () => {
    const timeoutSpy = vi.spyOn(window, "setTimeout");
    fetchMock.mockResolvedValue(response({}, false, 503));
    render(<CockpitHome onOpenSection={vi.fn()} />);

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(6));
    await waitFor(() => expect(timeoutSpy.mock.calls.some(([, delay]) => delay === 60_000)).toBe(true));
    fireEvent.click(screen.getByRole("button", { name: "Refresh Home" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(12));

    const retryDelays = timeoutSpy.mock.calls
      .map(([, delay]) => delay)
      .filter((delay): delay is number => typeof delay === "number");
    expect(retryDelays.filter((delay) => delay === 60_000).length).toBeGreaterThanOrEqual(2);
    expect(retryDelays).not.toContain(120_000);
    timeoutSpy.mockRestore();
  });
});
