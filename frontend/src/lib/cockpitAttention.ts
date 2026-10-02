import type { CockpitHomeSnapshot, HomeResourceState } from "./cockpitHome";

export interface AttentionOwner { principalId: string; sessionId: string }
export interface AttentionItem {
  id: string;
  kind: "task" | "inbox";
  taskId?: string;
  inboxId?: string;
  approvalId?: string;
  title: string;
  reason: string;
  updatedAt: string | null;
  goalId: string | null;
  threadId: string | null;
  recoveryAction: string | null;
  readOnly: boolean;
  metadataConfirmed: boolean;
  priority: number;
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
function text(value: unknown): string | null { return typeof value === "string" && value.length > 0 ? value : null; }

// Every supplied alias must agree. A legacy, partial or conflicting binding cannot authorize a task approval.
function exactAlias(contexts: Record<string, unknown>[], keys: string[], expected: unknown): boolean {
  const supplied = contexts.flatMap((context) => keys.filter((key) => context[key] !== undefined && context[key] !== null).map((key) => context[key]));
  return supplied.length > 0 && supplied.every((value) => value === expected);
}

export function taskApprovalMatches(task: Record<string, unknown>, approval: Record<string, unknown>, owner: AttentionOwner): boolean {
  const attempt = record(task.latest_attempt);
  const context = record(approval.approval_context);
  const scope = record(approval.approval_scope);
  const contexts = [context, record(context.authority), scope, record(scope.authority)];
  return task.ownership_access !== "recovered_read_only"
    && task.owner_principal_id === owner.principalId && task.owner_session_id === owner.sessionId
    && approval.owner_principal_id === owner.principalId && approval.operator_session_id === owner.sessionId
    && approval.status === "pending" && Boolean(text(approval.id)) && Boolean(text(attempt.workflow_run_id))
    && exactAlias(contexts, ["workflow_run_id", "workflow_run_identity", "run_identity", "durable_job_id", "job_id"], attempt.workflow_run_id)
    && exactAlias(contexts, ["goal_id", "durable_goal_id"], task.goal_id)
    && exactAlias(contexts, ["goal_revision", "durable_goal_revision"], task.goal_revision);
}

export function buildCockpitAttention(
  snapshot: CockpitHomeSnapshot,
  resources: Partial<Record<"work" | "approvals" | "inbox", HomeResourceState>>,
  owner: AttentionOwner | null,
): AttentionItem[] {
  const entries = new Map<string, AttentionItem>();
  for (const task of snapshot.work?.tasks.slice(0, 20) ?? []) {
    const id = text(task.task_id);
    const readOnly = task.ownership_access === "recovered_read_only";
    if (!id || !owner || (!readOnly && (task.owner_principal_id !== owner.principalId || task.owner_session_id !== owner.sessionId))) continue;
    const approval = readOnly ? undefined : snapshot.approvals.slice(0, 10).find((candidate) => taskApprovalMatches(task, candidate, owner));
    const unknown = task.block_kind === "unknown_effect" || task.block_kind === "cost_liability";
    const stale = task.readback_status === "stale" || task.verification_status === "stale" || task.block_kind === "review_expired";
    if (!approval && !unknown && !stale && !["blocked", "failed"].includes(String(task.status))) continue;
    const metadataConfirmed = resources.work === "ready" && (!approval || resources.approvals === "ready");
    entries.set(`task:${id}`, {
      id: `task:${id}`, kind: "task", taskId: id, approvalId: approval ? text(approval.id) ?? undefined : undefined,
      title: text(task.title) ?? "Untitled task",
      reason: readOnly ? "Recovered history · read only" : approval ? "Current attempt needs your approval" : unknown ? "Effect or cost is unknown · reconcile before retrying" : text(task.block_reason) ?? (stale ? "Verification or review is stale" : `Task is ${String(task.status)}`),
      updatedAt: text(task.updated_at), goalId: text(task.goal_id), threadId: text(task.origin_thread_id),
      recoveryAction: readOnly || !metadataConfirmed ? null : text(task.recovery_action), readOnly, metadataConfirmed,
      priority: readOnly ? 5 : approval ? 0 : unknown ? 1 : task.status === "blocked" ? 2 : task.status === "failed" ? 3 : 4,
    });
  }
  for (const item of snapshot.inbox?.items.slice(0, 20) ?? []) {
    if (!["pending", "snoozed"].includes(item.state)) continue;
    const taskKey = item.task_id ? `task:${item.task_id}` : null;
    if (taskKey && entries.has(taskKey)) continue;
    entries.set(`inbox:${item.id}`, {
      id: `inbox:${item.id}`, kind: "inbox", inboxId: item.id, title: item.title, reason: item.why_now || `Inbox item is ${item.state}`,
      updatedAt: null, goalId: item.goal_id, threadId: null, recoveryAction: null, readOnly: false,
      metadataConfirmed: resources.inbox === "ready", priority: 4,
    });
  }
  return [...entries.values()].sort((a, b) => a.priority - b.priority || (a.updatedAt ?? "").localeCompare(b.updatedAt ?? "") || a.id.localeCompare(b.id));
}
