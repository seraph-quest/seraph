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
    goalLoop: null,
    goalLoopGoalId: null,
    goalLoopLoading: false,
    goalLoopError: null,
    goalLoopAction: null,
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

  it.each([500, 422, 404])("retains last-known loop evidence while HTTP %s retrieval fails", async (status) => {
    const previous = {
      goal: { id: "g1", title: "Ship", status: "active", revision: 4 },
      criterion: null,
      receipts: [{ receipt_type: "outcome", execution_status: "completed", verification: "passed", usefulness: "useful", learning: "applied" }],
      strategy_deltas: [],
    };
    useQuestStore.setState({ goalLoop: previous, goalLoopGoalId: "g1" });
    mockFetch.mockResolvedValueOnce({
      ok: false,
      status,
      json: async () => ({ detail: { code: "loop_unavailable", reason: `HTTP ${status}` } }),
    });

    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    try {
      await useQuestStore.getState().loadGoalLoop("g1");
    } finally {
      errorSpy.mockRestore();
    }

    expect(useQuestStore.getState().goalLoop).toEqual(previous);
    expect(useQuestStore.getState().goalLoopError).toMatchObject({ status });
    expect(useQuestStore.getState().goalLoopLoading).toBe(false);
  });

  it("rejects receipt fields with unsafe runtime types", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({
        goal: { id: "g1", title: "Malformed receipt", status: "active", revision: 1 },
        criterion: null,
        receipts: [{
          receipt_type: "outcome",
          execution_status: { status: "failed" },
          verification: "unknown",
          usefulness: "unknown",
          learning: "no_learning",
          reason: { detail: "unsafe" },
          artifact_ref: { path: "unsafe" },
          audit_event_id: { id: "unsafe" },
          created_at: { timestamp: "unsafe" },
        }],
        strategy_deltas: [],
      }),
    });
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    try {
      await useQuestStore.getState().loadGoalLoop("g1");
    } finally {
      errorSpy.mockRestore();
    }

    expect(useQuestStore.getState().goalLoop).toBeNull();
    expect(useQuestStore.getState().goalLoopError).toMatchObject({ status: 502 });
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

  it("ignores an out-of-order loop response from a previously selected goal", async () => {
    let resolveFirst!: (value: unknown) => void;
    let resolveSecond!: (value: unknown) => void;
    const firstResponse = new Promise((resolve) => { resolveFirst = resolve; });
    const secondResponse = new Promise((resolve) => { resolveSecond = resolve; });
    const goalOne = {
      goal: { id: "g1", title: "First", status: "active", revision: 1 },
      criterion: null,
      receipts: [],
      strategy_deltas: [],
    };
    const goalTwo = {
      goal: { id: "g2", title: "Second", status: "active", revision: 2 },
      criterion: null,
      receipts: [],
      strategy_deltas: [],
    };
    mockFetch.mockImplementation((url: string) => ({
      ok: true,
      status: 200,
      json: () => url.includes("/g1/") ? firstResponse : secondResponse,
    }));

    const firstLoad = useQuestStore.getState().loadGoalLoop("g1");
    const secondLoad = useQuestStore.getState().loadGoalLoop("g2");
    resolveSecond(goalTwo);
    await secondLoad;
    resolveFirst(goalOne);
    await firstLoad;

    expect(useQuestStore.getState().goalLoopGoalId).toBe("g2");
    expect(useQuestStore.getState().goalLoop).toEqual(goalTwo);
    expect(useQuestStore.getState().goalLoopError).toBeNull();
    expect(useQuestStore.getState().goalLoopLoading).toBe(false);
  });

  it("rejects a loop payload whose goal identity does not match the request", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({
        goal: { id: "other-goal", title: "Wrong", status: "active", revision: 1 },
        criterion: null,
        receipts: [],
        strategy_deltas: [],
      }),
    });
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    try {
      await useQuestStore.getState().loadGoalLoop("g1");
    } finally {
      errorSpy.mockRestore();
    }

    expect(useQuestStore.getState().goalLoop).toBeNull();
    expect(useQuestStore.getState().goalLoopError).toMatchObject({ status: 502 });
  });

  it("rejects malformed criterion metadata before it reaches the panel", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({
        goal: { id: "g1", title: "Malformed", status: "active", revision: 1 },
        criterion: {
          criterion_id: "artifact",
          description: "Read an artifact",
          verifier_kind: "artifact_readback",
          target: { file_path: "artifact.md" },
          evidence_refs: null,
        },
        receipts: [],
        strategy_deltas: [],
      }),
    });
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    try {
      await useQuestStore.getState().loadGoalLoop("g1");
    } finally {
      errorSpy.mockRestore();
    }

    expect(useQuestStore.getState().goalLoop).toBeNull();
    expect(useQuestStore.getState().goalLoopError).toMatchObject({ status: 502 });
  });

  it("refreshes the goal tree before rereading a revision-changing correction", async () => {
    const refreshedTree = [{ id: "g1", title: "Updated", revision: 5 }];
    const refreshedLoop = {
      goal: { id: "g1", title: "Updated", status: "active", revision: 5 },
      criterion: null,
      receipts: [],
      strategy_deltas: [],
    };
    mockFetch.mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ status: "applied" }) });
    mockFetch.mockResolvedValueOnce({ ok: true, status: 200, json: async () => refreshedTree });
    mockFetch.mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ domains: {}, active_count: 1, completed_count: 0, total_count: 1 }) });
    mockFetch.mockResolvedValueOnce({ ok: true, status: 200, json: async () => refreshedLoop });

    await useQuestStore.getState().applyStrategyCorrection("g1", {
      correction_id: "correction-1",
      expected_revision: 4,
      query: "updated query",
      reason: "Operator correction",
    });

    expect(mockFetch.mock.calls.map(([url]) => String(url))).toEqual([
      expect.stringContaining("/api/goals/g1/strategy-corrections"),
      expect.stringContaining("/api/goals/tree"),
      expect.stringContaining("/api/goals/dashboard"),
      expect.stringContaining("/api/goals/g1/loop"),
    ]);
    expect(useQuestStore.getState().goalTree).toEqual(refreshedTree);
    expect(useQuestStore.getState().goalLoop?.goal.revision).toBe(5);
  });

  it("refreshes the goal tree before rereading a revision-changing rollback", async () => {
    const refreshedTree = [{ id: "g1", title: "Restored", revision: 6 }];
    const refreshedLoop = {
      goal: { id: "g1", title: "Restored", status: "active", revision: 6 },
      criterion: null,
      receipts: [],
      strategy_deltas: [],
    };
    mockFetch.mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ status: "rolled_back" }) });
    mockFetch.mockResolvedValueOnce({ ok: true, status: 200, json: async () => refreshedTree });
    mockFetch.mockResolvedValueOnce({ ok: true, status: 200, json: async () => ({ domains: {}, active_count: 1, completed_count: 0, total_count: 1 }) });
    mockFetch.mockResolvedValueOnce({ ok: true, status: 200, json: async () => refreshedLoop });

    await useQuestStore.getState().rollbackStrategyCorrection("g1", "delta-1", {
      expected_revision: 5,
      reason: "Operator rollback",
    });

    expect(mockFetch.mock.calls.map(([url]) => String(url))).toEqual([
      expect.stringContaining("/api/goals/g1/strategy-corrections/delta-1/rollback"),
      expect.stringContaining("/api/goals/tree"),
      expect.stringContaining("/api/goals/dashboard"),
      expect.stringContaining("/api/goals/g1/loop"),
    ]);
    expect(useQuestStore.getState().goalTree).toEqual(refreshedTree);
    expect(useQuestStore.getState().goalLoop?.goal.revision).toBe(6);
  });
});
