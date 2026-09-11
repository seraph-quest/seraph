import { describe, expect, it } from "vitest";

import {
  digestOpaqueReference,
  displayApprovalScopeTarget,
  displayApprovalOwnerMetadata,
  goalWorkflowBindingState,
  isApprovalAuthorityReady,
  selectApprovalForWorkflow,
} from "./cockpitAuthority";

const auth = {
  status: "authenticated" as const,
  principalId: "operator:single",
  sessionId: "browser-session-1",
};

const approval = {
  id: "approval-1",
  workflow_id: "run-1",
  goal_id: "goal-1",
  criterion_id: "criterion-1",
  goal_revision: 1,
  plan_revision: 1,
  tool_name: "filesystem:workspace",
  status: "pending",
  session_id: "conversation-1",
  approval_conversation_id: "conversation-1",
  approval_owner_principal_id: "operator:single",
  approval_owner_operator_session_id: "browser-session-1",
  expires_at: "2099-01-01T00:00:00Z",
  approval_scope: { action: "filesystem_write", target: { reference: "workspace/report.md" } },
};

describe("cockpit approval authority", () => {
  it("requires both the authenticated principal and browser session owner", () => {
    expect(isApprovalAuthorityReady(approval, auth, "ready")).toBe(true);
    expect(isApprovalAuthorityReady(
      approval,
      { ...auth, sessionId: "browser-session-2" },
      "ready",
    )).toBe(false);
    expect(isApprovalAuthorityReady(
      { ...approval, approval_owner_principal_id: undefined },
      auth,
      "ready",
    )).toBe(false);
    expect(isApprovalAuthorityReady(
      { ...approval, approval_owner_operator_session_id: undefined },
      auth,
      "ready",
    )).toBe(false);
  });

  it("treats a missing approval status as non-actionable", () => {
    expect(isApprovalAuthorityReady({ ...approval, status: undefined }, auth, "ready")).toBe(false);
  });

  it("locks rows while approval data is stale and when its supplied expiry has passed", () => {
    expect(isApprovalAuthorityReady(approval, auth, "stale")).toBe(false);
    expect(isApprovalAuthorityReady(
      { ...approval, expires_at: "2020-01-01T00:00:00Z" },
      auth,
      "ready",
      Date.parse("2026-01-01T00:00:00Z"),
    )).toBe(false);
    expect(isApprovalAuthorityReady(
      { ...approval, expires_at: "not-a-date" },
      auth,
      "ready",
    )).toBe(false);
  });

  it("requires a concrete scope and expiry before exposing effect authority", () => {
    expect(isApprovalAuthorityReady(
      { ...approval, approval_scope: undefined },
      auth,
      "ready",
    )).toBe(false);
    expect(isApprovalAuthorityReady(
      { ...approval, expires_at: undefined },
      auth,
      "ready",
    )).toBe(false);
    expect(isApprovalAuthorityReady(
      { ...approval, approval_scope: { action: "write_file" }, expires_at: 4_102_444_800 },
      auth,
      "ready",
      Date.parse("2026-01-01T00:00:00Z"),
    )).toBe(true);
  });

  it("does not select an unrelated pending approval for a workflow", () => {
    expect(selectApprovalForWorkflow(
      [{ ...approval }, { ...approval, id: "approval-2", session_id: "other-conversation" }],
      { toolName: "filesystem:workspace", sessionId: "workflow-conversation", pendingApprovalIds: [] },
    )).toBe(null);
    expect(selectApprovalForWorkflow(
      [{ ...approval, id: "approval-2", session_id: "other-conversation" }],
      { toolName: "filesystem:workspace", sessionId: "workflow-conversation", pendingApprovalIds: ["missing"] },
    )).toBe(null);
  });

  it("prefers an approval explicitly bound to the workflow identity", () => {
    expect(selectApprovalForWorkflow(
      [
        { ...approval, id: "approval-other", workflow_id: "run-other" },
        { ...approval, workflow_id: "run-1" },
      ],
      {
        workflowId: "run-1",
        goalId: "goal-1",
        criterionId: "criterion-1",
        goalRevision: 1,
        planRevision: 1,
        toolName: "filesystem:workspace",
        sessionId: "conversation-1",
      },
    )?.id).toBe("approval-1");
  });

  it("keeps owner metadata inspectable without exposing full identifiers", () => {
    const metadata = displayApprovalOwnerMetadata({
      ...approval,
      approval_owner_source: "operator_auth_session",
      approval_owner_expires_at: "2026-09-09T12:00:00Z",
    });
    expect(metadata.principal).toBe("…single");
    expect(metadata.session).toBe("…sion-1");
    expect(metadata.source).toBe("operator_auth_session");
    expect(metadata.expiry).toBe("2026-09-09T12:00:00Z");
  });

  it("requires a unique matching goal and revision before binding workflow authority", () => {
    expect(goalWorkflowBindingState({
      activeGoalCount: 1,
      goalId: "goal-1",
      goalRevision: 4,
      criterionId: "criterion-1",
      planRevision: 8,
      workflowGoalId: "goal-1",
      workflowGoalRevision: 4,
      workflowCriterionId: "criterion-1",
      workflowPlanRevision: 8,
    })).toBe("matched");
    expect(goalWorkflowBindingState({
      activeGoalCount: 2,
      goalId: "goal-1",
      goalRevision: 4,
      criterionId: "criterion-1",
      planRevision: 8,
      workflowGoalId: "goal-1",
      workflowGoalRevision: 4,
      workflowCriterionId: "criterion-1",
      workflowPlanRevision: 8,
    })).toBe("ambiguous");
    expect(goalWorkflowBindingState({
      activeGoalCount: 1,
      goalId: "goal-1",
      goalRevision: 4,
      criterionId: "criterion-1",
      planRevision: 8,
      workflowGoalId: "goal-1",
      workflowGoalRevision: 3,
      workflowCriterionId: "criterion-1",
      workflowPlanRevision: 8,
    })).toBe("stale");
    expect(goalWorkflowBindingState({ activeGoalCount: 1, goalId: "goal-1" })).toBe("unlinked");
    expect(goalWorkflowBindingState({
      activeGoalCount: 1,
      goalId: "goal-1",
      goalRevision: 4,
      criterionId: "criterion-1",
      planRevision: 8,
      workflowGoalId: "goal-1",
      workflowGoalRevision: 4,
      workflowCriterionId: "criterion-1",
      workflowPlanRevision: 9,
    })).toBe("stale");
  });

  it("fails closed when multiple approvals share the exact workflow identity", () => {
    expect(selectApprovalForWorkflow(
      [
        { ...approval, id: "approval-1" },
        { ...approval, id: "approval-2" },
      ],
      {
        workflowId: "run-1",
        goalId: "goal-1",
        goalRevision: 1,
        criterionId: "criterion-1",
        planRevision: 1,
      },
    )).toBe(null);
  });

  it("requires a declared candidate identity to match the approval", () => {
    expect(selectApprovalForWorkflow(
      [{ ...approval, candidate_id: "candidate-stale" }],
      {
        workflowId: "run-1",
        goalId: "goal-1",
        goalRevision: 1,
        criterionId: "criterion-1",
        planRevision: 1,
        candidateId: "candidate-current",
        sessionId: "conversation-1",
      },
    )).toBe(null);
    expect(selectApprovalForWorkflow(
      [{ ...approval, candidate_id: "candidate-current" }],
      {
        workflowId: "run-1",
        goalId: "goal-1",
        goalRevision: 1,
        criterionId: "criterion-1",
        planRevision: 1,
        candidateId: "candidate-current",
        sessionId: "conversation-1",
      },
    )?.id).toBe("approval-1");
  });

  it("keeps approval target references out of rendered scope labels", () => {
    const labels = displayApprovalScopeTarget({
      target: { type: "workspace", reference: "workspace/private/report.md" },
    });
    expect(labels).toContain("target workspace");
    expect(labels.join(" ")).not.toContain("workspace/private/report.md");
    expect(labels.join(" ")).toContain(digestOpaqueReference("workspace/private/report.md"));
  });
});
