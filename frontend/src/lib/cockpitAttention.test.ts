import { describe, expect, it } from "vitest";
import { buildCockpitAttention, taskApprovalMatches } from "./cockpitAttention";
import { emptyCockpitHomeSnapshot } from "./cockpitHome";

const owner = { principalId: "owner", sessionId: "root" };
const task = { task_id: "task", title: "Source review", owner_principal_id: "owner", owner_session_id: "root", goal_id: "goal", goal_revision: 2, latest_attempt: { attempt_id: "attempt", workflow_run_id: "run" }, status: "blocked", block_reason: "Waiting for approval", recovery_action: "approve_existing_run" };
const approval = { id: "approval", status: "pending", owner_principal_id: "owner", operator_session_id: "root", approval_context: { workflow_run_id: "run", goal_id: "goal", goal_revision: 2 } };
const ready = { work: "ready", approvals: "ready", inbox: "ready" } as const;
function snapshot(tasks: Record<string, unknown>[] = [task], approvals: Record<string, unknown>[] = [approval]) { return { ...emptyCockpitHomeSnapshot, work: { tasks, next_after: null }, approvals }; }

describe("bounded attention projection", () => {
  it("prioritizes exact current-root approvals above unknown effects and deduplicates linked inbox items", () => {
    const value = snapshot([{ ...task, task_id: "unknown", latest_attempt: null, block_kind: "unknown_effect" }, task]);
    value.inbox = { items: [{ id: "candidate", task_id: "task", state: "pending" } as never], next_cursor: null, last_confirmed_at: null };
    expect(buildCockpitAttention(value, ready, owner).map((item) => item.id)).toEqual(["task:task", "task:unknown"]);
    expect(buildCockpitAttention(value, ready, owner)[0].approvalId).toBe("approval");
  });
  it.each([
    { ...approval, operator_session_id: "old-root" },
    { ...approval, owner_principal_id: "other" },
    { ...approval, status: "approved" },
    { ...approval, approval_context: { workflow_run_id: "other-run", goal_id: "goal", goal_revision: 2 } },
    { ...approval, approval_context: { workflow_run_id: "run", job_id: "other-run", goal_id: "goal", goal_revision: 2 } },
    { ...approval, approval_context: { workflow_run_id: "run", goal_id: "other-goal", goal_revision: 2 } },
    { ...approval, approval_context: { workflow_run_id: "run", goal_id: "goal", goal_revision: 1 } },
    { ...approval, approval_context: { workflow_run_id: "run" } },
  ])("rejects foreign, expired, legacy and spliced approval bindings", (candidate) => {
    expect(taskApprovalMatches(task, candidate, owner)).toBe(false);
  });
  it("keeps confirmed metadata through partial refresh while withholding recovery controls", () => {
    const [item] = buildCockpitAttention(snapshot(), { ...ready, approvals: "degraded" }, owner);
    expect(item.title).toBe("Source review");
    expect(item.metadataConfirmed).toBe(false);
    expect(item.recoveryAction).toBeNull();
  });
  it("excludes ordinary foreign tasks and makes recovered history nonactionable", () => {
    expect(buildCockpitAttention(snapshot([{ ...task, owner_session_id: "other" }]), ready, owner)).toEqual([]);
    const [item] = buildCockpitAttention(snapshot([{ ...task, owner_session_id: "old", ownership_access: "recovered_read_only" }]), ready, owner);
    expect(item.readOnly).toBe(true);
    expect(item.approvalId).toBeUndefined();
    expect(item.recoveryAction).toBeNull();
  });
  it("bounds untrusted aggregate inputs without detail fanout", () => {
    const tasks = Array.from({ length: 1000 }, (_, index) => ({ ...task, task_id: `task-${index}` }));
    expect(buildCockpitAttention(snapshot(tasks), ready, owner)).toHaveLength(20);
  });
});
