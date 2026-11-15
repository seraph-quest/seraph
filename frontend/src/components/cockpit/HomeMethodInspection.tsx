import { useEffect, useState } from "react";
import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import type { AttentionOwner } from "../../lib/cockpitAttention";
import type { HomeTarget } from "../../lib/homeContinuation";
import { identifier, object, validSourceRefs, validateProvenance } from "../../lib/researchMethods";
import type { WorkBoardTask } from "../../types";
import { TaskMethodReview } from "./TaskMethodReview";

export function HomeMethodInspection({ target, owner }: { target: Extract<HomeTarget, {kind: "method"}>; owner: AttentionOwner }) {
  const [source, setSource] = useState<{ task: WorkBoardTask; attemptId: string; refs: string[]; preview: unknown } | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    setSource(null); setError(null);
    async function read() {
      const response = await apiFetch(`${API_URL}/api/memory/task-methods/${encodeURIComponent(target.proposal_id)}`, { signal: controller.signal });
      if (!response.ok) throw new Error("The exact original method is unavailable in this operator scope. No other method was selected.");
      const candidate: unknown = await response.json();
      if (!object(candidate) || candidate.proposal_id !== target.proposal_id || !identifier(candidate.task_id) || !identifier(candidate.attempt_id) || !validSourceRefs(candidate.source_refs)) throw new Error("Original method source binding is unavailable.");
      const taskResponse = await apiFetch(`${API_URL}/api/work-board/tasks/${encodeURIComponent(candidate.task_id)}`, { signal: controller.signal });
      if (!taskResponse.ok) throw new Error("Original method source Task is unavailable. Restore its owning read scope before review.");
      const detail: unknown = await taskResponse.json();
      if (!object(detail) || !object(detail.task) || detail.task.task_id !== candidate.task_id || !object(candidate.scope)
        || detail.task.goal_id !== candidate.scope.goal_id || detail.task.goal_revision !== candidate.scope.goal_revision
        || typeof detail.task.owner_session_id !== "string" || typeof detail.task.owner_principal_id !== "string" || !Number.isSafeInteger(detail.task.task_revision)) throw new Error("Original method source Task and Goal do not match.");
      const task = detail.task as unknown as WorkBoardTask;
      validateProvenance(candidate, task, candidate.attempt_id, candidate.source_refs);
      if (!controller.signal.aborted) setSource({ task, attemptId: candidate.attempt_id, refs: candidate.source_refs, preview: candidate });
    }
    void read().catch(cause => { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : "Method inspection unavailable."); });
    return () => controller.abort();
  }, [target.proposal_id, target.version, target.digest, owner.principalId, owner.sessionId]);
  return <section aria-label="Original admitted method inspector"><h3>Original admitted method</h3><p>Historical proposal {target.proposal_id} · version {target.version} · digest {target.digest}. Current controls are checked by the canonical method owner.</p>
    {error ? <p role="status">{error}</p> : source ? <TaskMethodReview task={source.task} proposalId={target.proposal_id} attemptId={source.attemptId} sourceRefs={source.refs} initialPreview={source.preview}
      owned={source.task.ownership_access !== "recovered_read_only" && source.task.owner_principal_id === owner.principalId && source.task.owner_session_id === owner.sessionId} /> : <p role="status">Reading the exact canonical method and source Task…</p>}
  </section>;
}
