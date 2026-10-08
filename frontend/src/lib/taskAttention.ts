import { API_URL } from "../config/constants";
import { apiFetch } from "./api";
import { taskApprovalMatches, type AttentionOwner } from "./cockpitAttention";
import type { WorkBoardTask, WorkBoardTaskDetail } from "../types";
import { isApprovalAuthorityReady } from "../components/cockpit/cockpitAuthority";

export async function attentionRequest<T>(path: string, signal?: AbortSignal, body?: unknown): Promise<T> {
  const response = await apiFetch(`${API_URL}${path}`, { signal, ...(body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }) });
  const payload = await response.json();
  if (!response.ok) throw new Error(`Recovery readback unavailable (${response.status}). Refresh the exact task; check current permission or expired approval in Connections.`);
  return payload as T;
}

export function sameAttentionTask(expected: WorkBoardTask, actual: WorkBoardTask, owner: AttentionOwner): boolean {
  return actual.ownership_access !== "recovered_read_only" && expected.ownership_access !== "recovered_read_only"
    && actual.owner_principal_id === owner.principalId && actual.owner_session_id === owner.sessionId
    && expected.owner_principal_id === owner.principalId && expected.owner_session_id === owner.sessionId
    && actual.task_id === expected.task_id && actual.task_revision === expected.task_revision
    && actual.goal_id === expected.goal_id && actual.goal_revision === expected.goal_revision
    && Boolean(expected.latest_attempt?.attempt_id) && Boolean(expected.latest_attempt?.workflow_run_id)
    && actual.latest_attempt?.attempt_id === expected.latest_attempt?.attempt_id
    && actual.latest_attempt?.workflow_run_id === expected.latest_attempt?.workflow_run_id;
}

export async function readAttentionTask(task: WorkBoardTask, owner: AttentionOwner, signal?: AbortSignal): Promise<WorkBoardTask> {
  const detail = await attentionRequest<WorkBoardTaskDetail>(`/api/work-board/tasks/${encodeURIComponent(task.task_id)}`, signal);
  if (!detail.task || !sameAttentionTask(task, detail.task, owner)) throw new Error("The task, attempt or owner changed. Refresh task detail before acting.");
  return detail.task;
}

export async function readTaskApproval(task: WorkBoardTask, owner: AttentionOwner, approvalId?: string | null, signal?: AbortSignal): Promise<Record<string, unknown> | null> {
  const suffix = approvalId ? `approval_id=${encodeURIComponent(approvalId)}&limit=1` : "limit=10";
  const payload = await attentionRequest<unknown>(`/api/approvals/pending?${suffix}`, signal);
  const approvals = Array.isArray(payload) ? payload : (payload as { approvals?: unknown })?.approvals;
  if (!Array.isArray(approvals)) throw new Error("Approval metadata is unavailable. Refresh the exact task.");
  const match = approvals.slice(0, approvalId ? 1 : 10).find((candidate) => candidate && typeof candidate === "object" && taskApprovalMatches(task as unknown as Record<string, unknown>, candidate, owner));
  if (!match || (approvalId && match.id !== approvalId)) return null;
  if (!isApprovalAuthorityReady(match, { status: "authenticated", principalId: owner.principalId, sessionId: owner.sessionId }, "ready") || match.conversation_id !== match.session_id) throw new Error("The exact approval has incomplete owner, scope or conversation metadata. Inspect it in Pending approvals; no task decision is available.");
  const expiry = typeof match.expires_at === "string" ? Date.parse(match.expires_at) : NaN;
  if (!Number.isFinite(expiry) || expiry <= Date.now()) throw new Error("The exact approval is expired or its expiry is unavailable. Request fresh review through the task's advertised recovery.");
  return match;
}
