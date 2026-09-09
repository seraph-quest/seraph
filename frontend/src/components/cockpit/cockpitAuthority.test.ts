import { describe, expect, it } from "vitest";

import {
  displayApprovalOwnerMetadata,
  goalWorkflowBindingState,
  isApprovalAuthorityReady,
  isApprovalActionableStatus,
  selectApprovalForWorkflow,
} from "./cockpitAuthority";

const auth = {
  status: "authenticated" as const,
  principalId: "operator:single",
  sessionId: "browser-session-1",
};

const approval = {
  id: "approval-1",
  tool_name: "filesystem:workspace",
  status: "pending",
  session_id: "conversation-1",
  approval_conversation_id: "conversation-1",
  approval_owner_principal_id: "operator:single",
  approval_owner_operator_session_id: "browser-session-1",
  approval_owner_expires_at: "2099-01-01T00:00:00Z",
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
    expect(isApprovalAuthorityReady(
      { ...approval, approval_owner_expires_at: undefined },
      auth,
      "ready",
    )).toBe(false);
  });

  it("locks rows while approval data is stale and when its supplied expiry has passed", () => {
    expect(isApprovalAuthorityReady(approval, auth, "stale")).toBe(false);
    expect(isApprovalAuthorityReady(
      { ...approval, approval_owner_expires_at: undefined, expires_at: "2020-01-01T00:00:00Z" },
      auth,
      "ready",
      Date.parse("2026-01-01T00:00:00Z"),
    )).toBe(false);
    expect(isApprovalAuthorityReady(
      { ...approval, approval_owner_expires_at: undefined, expires_at: "not-a-date" },
      auth,
      "ready",
    )).toBe(false);
  });

  it("requires an explicit backend pending status before enabling a decision", () => {
    expect(isApprovalActionableStatus("pending")).toBe(true);
    expect(isApprovalActionableStatus("awaiting_approval")).toBe(true);
    expect(isApprovalActionableStatus("")).toBe(false);
    expect(isApprovalActionableStatus(undefined)).toBe(false);
    expect(isApprovalActionableStatus(42)).toBe(false);
    expect(isApprovalAuthorityReady({ ...approval, status: "" }, auth, "ready")).toBe(false);
    expect(isApprovalAuthorityReady({ ...approval, status: "unknown" }, auth, "ready")).toBe(false);
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

  it("fails closed when goal and workflow lineage is missing, ambiguous, or stale", () => {
    expect(goalWorkflowBindingState({
      activeGoalCount: 1,
      goalId: "goal-1",
      goalRevision: 4,
      workflowGoalId: "goal-1",
      workflowGoalRevision: 4,
    })).toBe("matched");
    expect(goalWorkflowBindingState({
      activeGoalCount: 1,
      goalId: "goal-1",
      goalRevision: 4,
      workflowGoalId: "goal-2",
      workflowGoalRevision: 4,
    })).toBe("ambiguous");
    expect(goalWorkflowBindingState({
      activeGoalCount: 1,
      goalId: "goal-1",
      goalRevision: 4,
      workflowGoalId: "goal-1",
      workflowGoalRevision: 3,
    })).toBe("stale");
    expect(goalWorkflowBindingState({
      activeGoalCount: 2,
      goalId: "goal-1",
      goalRevision: 4,
      workflowGoalId: "goal-1",
      workflowGoalRevision: 4,
    })).toBe("ambiguous");
    expect(goalWorkflowBindingState({
      activeGoalCount: 1,
      goalId: "goal-1",
      goalRevision: 4,
    })).toBe("unlinked");
  });
});
