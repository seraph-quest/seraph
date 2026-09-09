/**
 * Shared, fail-closed checks for approval controls.
 *
 * The browser authentication session is separate from the conversation or
 * workflow session attached to an approval.  A conversation id alone cannot
 * prove that the current operator owns an effectful approval.
 */

export type ApprovalAuthorityRecord = {
  status?: unknown;
  workflow_id?: unknown;
  goal_id?: unknown;
  goal_revision?: unknown;
  approval_owner_principal_id?: unknown;
  approval_owner_operator_session_id?: unknown;
  approval_conversation_id?: unknown;
  session_id?: unknown;
  thread_id?: unknown;
  approval_owner_source?: unknown;
  approval_source?: unknown;
  approval_owner_expires_at?: unknown;
  approval_expires_at?: unknown;
  decision_expires_at?: unknown;
  expires_at?: unknown;
};

export type OperatorAuthBinding = {
  status: "loading" | "authenticated" | "unauthorized" | "degraded" | string;
  principalId: string | null;
  sessionId: string | null;
};

export type ApprovalLoadState = "loading" | "ready" | "stale";

const ACTIONABLE_APPROVAL_STATUSES = new Set([
  "pending",
  "awaiting_approval",
  "awaiting-approval",
  "approval_required",
]);

/** Approval rows must explicitly identify a backend pending state. */
export function isApprovalActionableStatus(status: unknown): boolean {
  const normalized = text(status).toLowerCase();
  return ACTIONABLE_APPROVAL_STATUSES.has(normalized);
}

function text(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function displayText(value: unknown): string {
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return text(value);
}

function expiryMilliseconds(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value < 1_000_000_000_000 ? value * 1000 : value;
  }
  const candidate = text(value);
  if (!candidate) return null;
  const parsed = Date.parse(candidate);
  return Number.isFinite(parsed) ? parsed : null;
}

function approvalExpiry(approval: ApprovalAuthorityRecord): unknown {
  return approval.approval_owner_expires_at
    ?? approval.approval_expires_at
    ?? approval.decision_expires_at
    ?? approval.expires_at;
}

/**
 * Return true only when the API supplied a fresh approval row with an
 * authenticated browser binding that exactly matches its owner metadata.
 * Missing owner/session metadata and malformed supplied expiry values fail
 * closed.  A missing expiry remains unavailable because the browser cannot
 * prove that the owner binding is still fresh.
 */
export function isApprovalAuthorityReady(
  approval: ApprovalAuthorityRecord | null | undefined,
  auth: OperatorAuthBinding,
  approvalLoadState: ApprovalLoadState,
  now = Date.now(),
): boolean {
  if (!approval || approvalLoadState !== "ready" || auth.status !== "authenticated") return false;

  if (!isApprovalActionableStatus(approval.status)) return false;

  const ownerPrincipal = text(approval.approval_owner_principal_id);
  const ownerSession = text(approval.approval_owner_operator_session_id);
  const currentPrincipal = text(auth.principalId);
  const currentSession = text(auth.sessionId);
  if (!ownerPrincipal || !ownerSession || !currentPrincipal || !currentSession) return false;
  if (ownerPrincipal !== currentPrincipal || ownerSession !== currentSession) return false;

  const conversation = text(approval.approval_conversation_id);
  const executionSession = text(approval.session_id) || text(approval.thread_id);
  if (conversation && (!executionSession || conversation !== executionSession)) return false;

  const suppliedExpiry = approvalExpiry(approval);
  if (suppliedExpiry === undefined || suppliedExpiry === null) return false;
  if (!expiryMilliseconds(suppliedExpiry)) return false;
  const expiry = expiryMilliseconds(suppliedExpiry);
  return expiry !== null && expiry > now;
}

/** Keep identifiers useful for inspection without echoing full authority values. */
export function redactIdentifier(value: unknown): string {
  const candidate = text(value);
  if (!candidate) return "unavailable";
  if (candidate.length <= 6) return "••••";
  return `…${candidate.slice(-6)}`;
}

export function displayApprovalOwnerMetadata(approval: ApprovalAuthorityRecord | null | undefined) {
  return {
    principal: redactIdentifier(approval?.approval_owner_principal_id),
    session: redactIdentifier(approval?.approval_owner_operator_session_id),
    source: text(approval?.approval_owner_source ?? approval?.approval_source) || "unavailable",
    expiry: displayText(
      approval?.approval_owner_expires_at
        ?? approval?.approval_expires_at
        ?? approval?.decision_expires_at
        ?? approval?.expires_at,
    ) || "unavailable",
  };
}

export type ApprovalCandidate = ApprovalAuthorityRecord & {
  id: string;
  tool_name?: unknown;
};

export type WorkflowApprovalBinding = {
  workflowId?: unknown;
  goalId?: unknown;
  goalRevision?: unknown;
  toolName?: unknown;
  sessionId?: unknown;
  pendingApprovalIds?: readonly string[] | null;
  pendingApprovals?: readonly ApprovalCandidate[] | null;
};

export type GoalWorkflowBindingState = "matched" | "ambiguous" | "unlinked" | "stale";

function integer(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= 1 ? value : null;
}

/**
 * Match a workflow to the one current goal before composing outcome controls.
 * Missing identifiers/revisions are metadata gaps, while mismatches are
 * ambiguous or stale and must never be paired by list order.
 */
export function goalWorkflowBindingState(input: {
  activeGoalCount: number;
  goalId?: unknown;
  goalRevision?: unknown;
  workflowGoalId?: unknown;
  workflowGoalRevision?: unknown;
}): GoalWorkflowBindingState {
  if (input.activeGoalCount > 1) return "ambiguous";
  const goalId = text(input.goalId);
  const workflowGoalId = text(input.workflowGoalId);
  if (!goalId || !workflowGoalId) return "unlinked";
  if (goalId !== workflowGoalId) return "ambiguous";
  const goalRevision = integer(input.goalRevision);
  const workflowGoalRevision = integer(input.workflowGoalRevision);
  if (goalRevision === null || workflowGoalRevision === null) return "unlinked";
  return goalRevision === workflowGoalRevision ? "matched" : "stale";
}

/** Select only an approval explicitly bound to the inspected workflow. */
export function selectApprovalForWorkflow<T extends ApprovalCandidate>(
  pending: readonly T[],
  workflow: WorkflowApprovalBinding | null | undefined,
): T | null {
  if (!workflow) return null;

  const pendingIds = workflow.pendingApprovalIds?.filter((id) => typeof id === "string" && id.trim()) ?? [];
  // A workflow may report several pending approvals, but this panel has one
  // consequential decision surface. Refuse to choose by list order or by a
  // conversation/tool match when the backend did not provide one explicit id.
  if (pendingIds.length !== 1) return null;

  const candidate = pending.find((approval) => approval.id === pendingIds[0]);
  if (!candidate) return null;

  const workflowId = text(workflow.workflowId);
  const candidateWorkflowId = text(candidate.workflow_id);
  const goalId = text(workflow.goalId);
  const candidateGoalId = text(candidate.goal_id);
  const goalRevision = integer(workflow.goalRevision);
  const candidateGoalRevision = integer(candidate.goal_revision);
  const sessionId = text(workflow.sessionId);
  const candidateSessionId = text(candidate.session_id) || text(candidate.thread_id);
  if (
    !workflowId
    || !candidateWorkflowId
    || workflowId !== candidateWorkflowId
    || !goalId
    || !candidateGoalId
    || goalId !== candidateGoalId
    || goalRevision === null
    || candidateGoalRevision === null
    || goalRevision !== candidateGoalRevision
    || !sessionId
    || !candidateSessionId
    || sessionId !== candidateSessionId
  ) return null;

  const toolName = text(workflow.toolName);
  if (toolName && text(candidate.tool_name) !== toolName) return null;
  return candidate;
}
