import { create } from "zustand";
import { API_URL } from "../config/constants";
import type { GoalInfo } from "../types";

interface DomainStat {
  active: number;
  completed: number;
  total: number;
  progress: number;
}

interface Dashboard {
  domains: Record<string, DomainStat>;
  active_count: number;
  completed_count: number;
  total_count: number;
}

interface QuestStore {
  goals: GoalInfo[];
  goalTree: GoalInfo[];
  dashboard: Dashboard | null;
  loading: boolean;

  loadGoals: (filters?: { level?: string; domain?: string; status?: string }) => Promise<void>;
  loadTree: () => Promise<void>;
  loadDashboard: () => Promise<void>;
  createGoal: (goal: Partial<GoalInfo>) => Promise<void>;
  updateGoal: (id: string, updates: { status?: string; title?: string }) => Promise<void>;
  deleteGoal: (id: string) => Promise<void>;
  refresh: () => Promise<void>;
}

export const useQuestStore = create<QuestStore>((set, get) => ({
  goals: [],
  goalTree: [],
  dashboard: null,
  loading: false,

  loadGoals: async (filters) => {
    const params = new URLSearchParams();
    if (filters?.level) params.set("level", filters.level);
    if (filters?.domain) params.set("domain", filters.domain);
    if (filters?.status) params.set("status", filters.status);
    const qs = params.toString();
    try {
      const res = await fetch(`${API_URL}/api/goals${qs ? `?${qs}` : ""}`);
      if (res.ok) set({ goals: await res.json() });
    } catch (err) { console.error("Failed to load goals:", err); }
  },

  loadTree: async () => {
    try {
      const res = await fetch(`${API_URL}/api/goals/tree`);
      if (res.ok) set({ goalTree: await res.json() });
    } catch (err) { console.error("Failed to load goal tree:", err); }
  },

  loadDashboard: async () => {
    try {
      const res = await fetch(`${API_URL}/api/goals/dashboard`);
      if (res.ok) set({ dashboard: await res.json() });
    } catch (err) { console.error("Failed to load dashboard:", err); }
  },

  createGoal: async (goal) => {
    try {
      await fetch(`${API_URL}/api/goals`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(goal),
      });
      await get().refresh();
    } catch (err) { console.error("Failed to create goal:", err); }
  },

  updateGoal: async (id, updates) => {
    try {
      await fetch(`${API_URL}/api/goals/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(updates),
      });
      await get().refresh();
    } catch (err) { console.error("Failed to update goal:", err); }
  },

  deleteGoal: async (id) => {
    try {
      await fetch(`${API_URL}/api/goals/${id}`, { method: "DELETE" });
      await get().refresh();
    } catch (err) { console.error("Failed to delete goal:", err); }
  },

  refresh: async () => {
    set({ loading: true });
    await Promise.all([get().loadTree(), get().loadDashboard()]);
    set({ loading: false });
  },
}));
