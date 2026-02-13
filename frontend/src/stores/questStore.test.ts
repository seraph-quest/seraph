import { describe, it, expect, beforeEach, vi } from "vitest";
import { useQuestStore } from "./questStore";

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
});
