import { useEffect, useRef, useState } from "react";
import { useOptionalOperatorAuth } from "../components/auth/OperatorAuthGate";
import { API_URL } from "../config/constants";
import { apiFetch } from "../lib/api";
import { appEventBus } from "../lib/appEventBus";
import { useQuestStore } from "../stores/questStore";
import type { GoalInfo } from "../types";

export interface AttentionGoalFocus { principalId: string; sessionId: string; goalId: string; goalRevision?: number; programmeId?: string | null }
function findGoal(goals: GoalInfo[], id: string): GoalInfo | null {
  for (const goal of goals) { if (goal.id === id) return goal; const child = findGoal(goal.children ?? [], id); if (child) return child; }
  return null;
}

/** The QuestPanel remains mounted while closed, so open -> focus has no event replay or poll. */
export function useAttentionGoalFocus(onSelect: (goalId: string | null) => void, onResetFilters: () => void, onProgramme?: (id: string | null) => void) {
  const auth = useOptionalOperatorAuth();
  const session = auth?.session;
  const sessionRef = useRef(session); sessionRef.current = session;
  const scope = session ? `${session.principal_id}:${session.session_id}` : null;
  const scopeRef = useRef(scope); scopeRef.current = scope;
  const callbacks = useRef({ onSelect, onResetFilters, onProgramme }); callbacks.current = { onSelect, onResetFilters, onProgramme };
  const generation = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  useEffect(() => {
    generation.current += 1; controller.current?.abort();
    callbacks.current.onSelect(null); callbacks.current.onProgramme?.(null); setStatus(null);
    const focus = async (event: AttentionGoalFocus) => {
      const expectedScope = `${event.principalId}:${event.sessionId}`;
      const currentSession = sessionRef.current;
      if (!currentSession || expectedScope !== scopeRef.current || !(Date.parse(currentSession.absolute_expires_at) > Date.now()) || !(Date.parse(currentSession.idle_expires_at) > Date.now())) return;
      const version = ++generation.current;
      controller.current?.abort(); const active = new AbortController(); controller.current = active;
      callbacks.current.onSelect(null); setStatus("Confirming the originating goal in this operator scope…");
      try {
        const response = await apiFetch(`${API_URL}/api/goals/tree`, { signal: active.signal });
        if (!response.ok) throw new Error("Originating goal read is unavailable. Refresh Goals before continuing.");
        const tree: unknown = await response.json();
        if (version !== generation.current || active.signal.aborted || scopeRef.current !== expectedScope) return;
        if (!(Date.parse(sessionRef.current?.absolute_expires_at ?? "") > Date.now()) || !(Date.parse(sessionRef.current?.idle_expires_at ?? "") > Date.now())) return;
        if (!Array.isArray(tree)) throw new Error("The owner-scoped goal tree is unavailable.");
        const goal = findGoal(tree, event.goalId);
        if (!goal) throw new Error("The originating goal is not present in this operator scope. No other goal was selected.");
        useQuestStore.setState({ goalTree: tree });
        if (event.goalRevision !== undefined && goal.revision !== event.goalRevision) throw new Error("The exact Goal revision changed since Home. Refresh Home before continuing.");
        callbacks.current.onResetFilters(); callbacks.current.onSelect(goal.id); callbacks.current.onProgramme?.(event.programmeId ?? null);
        setStatus(goal.ownership_access === "recovered_read_only" ? "Originating goal · recovered history, read only." : "Originating goal confirmed in the current operator scope.");
      } catch (error) {
        if (version === generation.current && !active.signal.aborted && scopeRef.current === expectedScope) setStatus(error instanceof Error ? error.message : "Originating goal unavailable.");
      }
    };
    appEventBus.on<AttentionGoalFocus>("attention:inspect-goal", focus);
    return () => { generation.current += 1; controller.current?.abort(); appEventBus.off("attention:inspect-goal", focus); };
  }, [scope]);
  return status;
}
