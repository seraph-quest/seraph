import { homeSections, type HomeContinuation } from "./homeContinuation";
export const sourceAt = "2026-10-09T10:00:00Z";
const base = { source_at: sourceAt, ownership_access: "current" as const };
export function homeFixture(): HomeContinuation {
  const snapshot = Object.fromEntries(homeSections.map(key => [key, { items: [], state: "empty", source_as_of: null }])) as unknown as HomeContinuation;
  snapshot.as_of = sourceAt;
  snapshot.active_goals = { state: "ready", source_as_of: sourceAt, items: [{ ...base, kind: "active_goal", goal_id: "goal-1", goal_revision: 1, status: "active", sort_order: 0, due_at: null, target: { kind: "goal", goal_id: "goal-1", goal_revision: 1 } }] };
  snapshot.programme_status = { state: "ready", source_as_of: sourceAt, items: [{ ...base, kind: "programme", goal_id: "goal-1", goal_revision: 1, programme_id: "a".repeat(32), grant_revision: 1, state: "active", reason_code: null, expires_at: "2026-10-10T10:00:00Z", next_digest_at: "2026-10-10T06:00:00Z", target: { kind: "programme", goal_id: "goal-1", goal_revision: 1, programme_id: "a".repeat(32) } }] };
  snapshot.task_next_actions = { state: "ready", source_as_of: sourceAt, items: [{ ...base, kind: "task_next_action", task_id: "task-1", task_revision: 1, goal_id: "goal-1", goal_revision: 1, status: "todo", priority: 50, scheduled_at: null, action: "inspect_task", method: { status: "admitted", method_id: "proposal-1", version: "version-1", digest: "b".repeat(64), admitted_at: sourceAt, lifecycle: "rolled_back_metadata", reason_code: "method_rolled_back", target: { kind: "method", proposal_id: "proposal-1", version: "version-1", digest: "b".repeat(64) } }, target: { kind: "task", task_id: "task-1", task_revision: 1 } }] };
  snapshot.prepared_outputs = { state: "ready", source_as_of: sourceAt, items: [{ ...base, kind: "prepared_output", task_id: "task-result", task_revision: 4, attempt_id: "attempt-1", output_state: "unknown", method: { status: "unknown", method_id: null, version: null, digest: null, admitted_at: null, lifecycle: "unknown", reason_code: "method_projection_missing", target: null }, target: { kind: "output", task_id: "task-result", task_revision: 4, attempt_id: "attempt-1" } }] };
  snapshot.approvals = { state: "ready", source_as_of: sourceAt, items: [{ ...base, kind: "approval", approval_id: "approval-1", status: "pending", expires_at: null, target: { kind: "approval", approval_id: "approval-1" } }] };
  snapshot.blocked_items = { state: "ready", source_as_of: sourceAt, items: [{ ...base, kind: "blocked_task", task_id: "task-blocked", task_revision: 2, goal_id: "goal-1", goal_revision: 1, reason_code: "unknown_effect", target: { kind: "task", task_id: "task-blocked", task_revision: 2 } }] };
  return snapshot;
}
