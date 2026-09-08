import { describe, it, expect, beforeEach, vi } from "vitest";
import { GoalUpdateError, useQuestStore } from "./questStore";

const mockFetch = vi.fn();
globalThis.fetch = mockFetch;

function resetStore() {
  useQuestStore.setState({
    goals: [],
    goalTree: [],
    dashboard: null,
    loading: false,
  });
}

describe("questStore", () => {
  beforeEach(() => {
    resetStore();
    vi.clearAllMocks();
  });

  it("loadGoals populates goals list", async () => {
    const goals = [{ id: "g1", title: "A" }, { id: "g2", title: "B" }];
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => goals });
    await useQuestStore.getState().loadGoals();
    expect(useQuestStore.getState().goals).toHaveLength(2);
  });

  it("loadGoals passes filters as query params", async () => {
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => [] });
    await useQuestStore.getState().loadGoals({ level: "daily", domain: "health" });
    const url = mockFetch.mock.calls[0][0] as string;
    expect(url).toContain("level=daily");
    expect(url).toContain("domain=health");
  });

  it("loadTree populates goalTree", async () => {
    const tree = [{ id: "g1", title: "Vision", children: [] }];
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => tree });
    await useQuestStore.getState().loadTree();
    expect(useQuestStore.getState().goalTree).toHaveLength(1);
  });

  it("loadDashboard populates dashboard", async () => {
    const dashboard = { domains: {}, active_count: 0, completed_count: 0, total_count: 0 };
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => dashboard });
    await useQuestStore.getState().loadDashboard();
    expect(useQuestStore.getState().dashboard).toEqual(dashboard);
  });

  it("createGoal calls API and refreshes", async () => {
    // Create
    mockFetch.mockResolvedValueOnce({ ok: true });
    // refresh -> loadTree + loadDashboard
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => [] });
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => ({ domains: {}, active_count: 0, completed_count: 0, total_count: 0 }) });
    await useQuestStore.getState().createGoal({ title: "New" });
    expect(mockFetch).toHaveBeenCalledTimes(3);
  });

  it("updateGoal calls API and refreshes", async () => {
    mockFetch.mockResolvedValueOnce({ ok: true });
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => [] });
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => ({ domains: {}, active_count: 0, completed_count: 0, total_count: 0 }) });
    await useQuestStore.getState().updateGoal("g1", { status: "completed" });
    expect(mockFetch).toHaveBeenCalledTimes(3);
  });

  it("updateGoal sends all editable fields", async () => {
    mockFetch.mockResolvedValueOnce({ ok: true });
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => [] });
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => ({ domains: {}, active_count: 0, completed_count: 0, total_count: 0 }) });
    await useQuestStore.getState().updateGoal("g1", {
      title: "Updated",
      description: "New desc",
      level: "monthly",
      domain: "health",
      due_date: "2025-06-01",
    });
    const [url, opts] = mockFetch.mock.calls[0];
    expect(url).toContain("/api/goals/g1");
    expect(opts.method).toBe("PATCH");
    const body = JSON.parse(opts.body);
    expect(body.title).toBe("Updated");
    expect(body.description).toBe("New desc");
    expect(body.level).toBe("monthly");
    expect(body.domain).toBe("health");
    expect(body.due_date).toBe("2025-06-01");
  });

  it("updateGoal can clear due_date with null", async () => {
    mockFetch.mockResolvedValueOnce({ ok: true });
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => [] });
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => ({ domains: {}, active_count: 0, completed_count: 0, total_count: 0 }) });
    await useQuestStore.getState().updateGoal("g1", { due_date: null });
    const body = JSON.parse(mockFetch.mock.calls[0][1].body);
    expect(body.due_date).toBeNull();
  });

  it("updateGoal sends the server revision for stale-edit protection", async () => {
    mockFetch.mockResolvedValueOnce({ ok: true });
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => [] });
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => ({ domains: {}, active_count: 0, completed_count: 0, total_count: 0 }) });
    await useQuestStore.getState().updateGoal("g1", { title: "Draft", expected_revision: 4 });
    const body = JSON.parse(mockFetch.mock.calls[0][1].body);
    expect(body.expected_revision).toBe(4);
  });

  it("fails closed on a stale revision and preserves structured recovery metadata", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 409,
      json: async () => ({
        detail: {
          code: "stale_goal_revision",
          current_revision: 7,
          recovery: "Refresh the goal and resubmit against the current revision.",
        },
      }),
    });

    await expect(
      useQuestStore.getState().updateGoal("g1", { title: "Draft", expected_revision: 4 }),
    ).rejects.toMatchObject({
      code: "stale_goal_revision",
      goalId: "g1",
      currentRevision: 7,
    } satisfies Partial<GoalUpdateError>);
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  it("deleteGoal calls API and refreshes", async () => {
    mockFetch.mockResolvedValueOnce({ ok: true });
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => [] });
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => ({ domains: {}, active_count: 0, completed_count: 0, total_count: 0 }) });
    await useQuestStore.getState().deleteGoal("g1");
    expect(mockFetch).toHaveBeenCalledTimes(3);
  });

  it("refresh sets loading flag", async () => {
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => [] });
    mockFetch.mockResolvedValueOnce({ ok: true, json: async () => ({ domains: {}, active_count: 0, completed_count: 0, total_count: 0 }) });
    await useQuestStore.getState().refresh();
    expect(useQuestStore.getState().loading).toBe(false);
  });

  it("loads a typed goal loop without proposing or executing a candidate", async () => {
    const payload = {
      goal: { id: "g1", title: "Ship", status: "active", revision: 4 },
      criterion: {
        criterion_id: "artifact",
        description: "A verified artifact exists",
        verifier_kind: "artifact_readback",
        target: { file_path: "artifacts/ship.md" },
        evidence_refs: ["artifact:ship"],
      },
      receipts: [{ receipt_type: "outcome", execution_status: "blocked", verification: "unknown", usefulness: "unknown", learning: "no_learning" }],
      strategy_deltas: [],
    };
    mockFetch.mockResolvedValueOnce({ ok: true, status: 200, json: async () => payload });

    await useQuestStore.getState().loadGoalLoop("g1");

    expect(mockFetch).toHaveBeenCalledWith(expect.stringContaining("/api/goals/g1/loop"));
    expect(mockFetch.mock.calls.some(([url]) => String(url).includes("/candidates"))).toBe(false);
    expect(useQuestStore.getState().goalLoop).toEqual(payload);
  });

  it("reports unauthorized loop inspection while retaining last-known payload", async () => {
    const previous = {
      goal: { id: "g1", title: "Ship", status: "active", revision: 4 },
      criterion: null,
      receipts: [],
      strategy_deltas: [],
    };
    useQuestStore.setState({ goalLoop: previous, goalLoopGoalId: "g1" });
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status: 401,
      json: async () => ({ detail: { code: "authentication_required" } }),
    });

    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    try {
      await useQuestStore.getState().loadGoalLoop("g1");
    } finally {
      errorSpy.mockRestore();
    }

    expect(useQuestStore.getState().goalLoop).toEqual(previous);
    expect(useQuestStore.getState().goalLoopError).toMatchObject({
      status: 401,
      code: "authentication_required",
    });
  });

  it("runs the bounded snapshot endpoint and reloads loop evidence", async () => {
    const response = {
      status: "blocked",
      execution_status: "blocked",
      verification: "unknown",
      learning: "no_learning",
    };
    const payload = {
      goal: { id: "g1", title: "Ship", status: "active", revision: 4 },
      criterion: null,
      receipts: [],
      strategy_deltas: [],
    };
    mockFetch.mockResolvedValueOnce({ ok: true, status: 200, json: async () => response });
    mockFetch.mockResolvedValueOnce({ ok: true, status: 200, json: async () => payload });

    await useQuestStore.getState().runGoalSnapshot("g1", {
      expected_revision: 4,
      evidence_refs: ["artifact:ship"],
    });

    const [url, options] = mockFetch.mock.calls[0];
    expect(url).toContain("/api/goals/g1/snapshot");
    expect(options.method).toBe("POST");
    expect(JSON.parse(options.body)).toMatchObject({ expected_revision: 4 });
    expect(mockFetch.mock.calls.some(([candidateUrl]) => String(candidateUrl).includes("/candidates"))).toBe(false);
    expect(useQuestStore.getState().goalLoop).toEqual(payload);
  });
});
