import { useCallback, useEffect, useState } from "react";

import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import { SourceWatchForm, type SourceWatchFormGoal } from "../cockpit/SourceWatchForm";
import type { GoalInfo } from "../../types";

function flattenGoals(goals: GoalInfo[]): SourceWatchFormGoal[] {
  const result: SourceWatchFormGoal[] = [];
  const visit = (goal: GoalInfo) => {
    result.push({
      id: goal.id,
      title: goal.title,
      revision: goal.revision,
      proactive_enabled: goal.proactive_enabled,
      admission_budget: goal.admission_budget,
    });
    goal.children?.forEach(visit);
  };
  goals.forEach(visit);
  return result;
}

export function SourceWatchPanel() {
  const [goals, setGoals] = useState<SourceWatchFormGoal[]>([]);
  const [status, setStatus] = useState<string | null>(null);

  const loadGoals = useCallback(async () => {
    try {
      const response = await apiFetch(`${API_URL}/api/goals/tree`);
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        setStatus("Goal labels unavailable; refresh before configuring a watch.");
        return;
      }
      const tree = Array.isArray(payload)
        ? payload as GoalInfo[]
        : payload && typeof payload === "object" && Array.isArray((payload as { goals?: unknown }).goals)
          ? (payload as { goals: GoalInfo[] }).goals
          : [];
      setGoals(flattenGoals(tree));
      setStatus(null);
    } catch {
      setStatus("Goal labels unavailable; refresh before configuring a watch.");
    }
  }, []);

  useEffect(() => {
    void loadGoals();
  }, [loadGoals]);

  return (
    <div className="px-1">
      <div className="text-[10px] uppercase tracking-wider text-retro-border font-bold mb-1">
        Guardian source watches
      </div>
      <div className="text-[9px] text-retro-text/40 mb-2">
        Choose a loaded goal and bounded cadence. Existing 15-minute watches stay visible as legacy schedules; new watches use the operator timezone.
      </div>
      {status ? <div className="text-[9px] text-retro-highlight mb-1" role="status">{status}</div> : null}
      {goals.length > 0 ? (
        <SourceWatchForm goal={null} goalOptions={goals} />
      ) : (
        <div className="text-[9px] text-retro-text/30">No goal revisions are available for a source watch.</div>
      )}
      <button
        type="button"
        onClick={() => void loadGoals()}
        className="mt-1 text-[9px] text-retro-highlight hover:text-retro-text uppercase tracking-wider"
      >
        Refresh goal labels
      </button>
    </div>
  );
}
