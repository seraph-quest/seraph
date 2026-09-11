/**
 * Shared, fail-closed checks for approval controls.
 *
 * The browser authentication session is separate from the conversation or
 * workflow session attached to an approval.  A conversation id alone cannot
 * prove that the current operator owns an effectful approval.
 */

export type ApprovalAuthorityRecord = {
  workflow_id?: unknown;
  goal_id?: unknown;
  criterion_id?: unknown;
  status?: unknown;
  approval_owner_principal_id?: unknown;
  approval_owner_operator_session_id?: unknown;
  owner_principal_id?: unknown;
  operator_session_id?: unknown;
  approval_conversation_id?: unknown;
  conversation_id?: unknown;
  session_id?: unknown;
  thread_id?: unknown;
  approval_owner_source?: unknown;
  approval_source?: unknown;
  approval_owner_expires_at?: unknown;
  approval_expires_at?: unknown;
  decision_expires_at?: unknown;
  expires_at?: unknown;
  approval_scope?: unknown;
  approval_context?: unknown;
  goal_revision?: unknown;
  plan_revision?: unknown;
  candidate_id?: unknown;
};

export type OperatorAuthBinding = {
  status: "loading" | "authenticated" | "unauthorized" | "degraded" | string;
  principalId: string | null;
  sessionId: string | null;
};

export type ApprovalLoadState = "loading" | "ready" | "stale";

export type GoalWorkflowBindingState = "matched" | "ambiguous" | "unlinked" | "stale";

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

function approvalScope(approval: ApprovalAuthorityRecord): Record<string, unknown> | null {
  const candidate = approval.approval_scope ?? approval.approval_context;
  return candidate && typeof candidate === "object" && !Array.isArray(candidate)
    ? candidate as Record<string, unknown>
    : null;
}

/**
 * Bind a displayed workflow to the current goal before exposing any
 * consequential control. Missing identity is intentionally distinguishable
 * from a mismatch so the cockpit can show partial metadata without guessing.
 */
export function goalWorkflowBindingState({
  activeGoalCount,
  goalId,
  goalRevision,
  criterionId,
  planRevision,
  candidateId,
  workflowGoalId,
  workflowGoalRevision,
  workflowCriterionId,
  workflowPlanRevision,
  workflowCandidateId,
}: {
  activeGoalCount: number;
  goalId?: string | null;
  goalRevision?: number | null;
  criterionId?: string | null;
  planRevision?: number | null;
  candidateId?: string | null;
  workflowGoalId?: string | null;
  workflowGoalRevision?: number | null;
  workflowCriterionId?: string | null;
  workflowPlanRevision?: number | null;
  workflowCandidateId?: string | null;
}): GoalWorkflowBindingState {
  if (activeGoalCount > 1) return "ambiguous";
  if (!goalId || !workflowGoalId) return "unlinked";
  if (goalId !== workflowGoalId) return "ambiguous";
  if (
    goalRevision == null
    || workflowGoalRevision == null
    || !criterionId
    || !workflowCriterionId
    || planRevision == null
    || workflowPlanRevision == null
  ) return "unlinked";
  if (goalRevision !== workflowGoalRevision) return "stale";
  if (criterionId !== workflowCriterionId) return "stale";
  if (planRevision !== workflowPlanRevision) return "stale";
  const expectedCandidateId = text(workflowCandidateId);
  const suppliedCandidateId = text(candidateId);
  if (expectedCandidateId !== suppliedCandidateId) return "stale";
  return "matched";
}

/**
 * Keep opaque approval target references out of rendered text while retaining
 * a stable operator-inspection handle. This is a display fingerprint, not an
 * authority decision; the raw scope remains in the backend-bound record.
 */
export function digestOpaqueReference(value: unknown): string {
  const candidate = text(value);
  if (!candidate) return "unavailable";
  let first = 0x811c9dc5;
  let second = 0x9e3779b9;
  for (let index = 0; index < candidate.length; index += 1) {
    const code = candidate.charCodeAt(index);
    first = Math.imul(first ^ code, 0x01000193);
    second = Math.imul(second ^ (code + index), 0x01000193);
  }
  return `digest:${(first >>> 0).toString(16).padStart(8, "0")}${(second >>> 0).toString(16).padStart(8, "0")}`;
}

/** Replace an approval target reference in operator-facing copy with a stable digest. */
export function redactApprovalText(value: unknown, scope: unknown): string {
  const candidate = text(value);
  if (!candidate) return "";
  const record = scope && typeof scope === "object" && !Array.isArray(scope)
    ? scope as Record<string, unknown>
    : null;
  const target = record?.target && typeof record.target === "object" && !Array.isArray(record.target)
    ? record.target as Record<string, unknown>
    : null;
  const reference = text(target?.reference);
  return reference ? candidate.split(reference).join(digestOpaqueReference(reference)) : candidate;
}

/** Format only non-sensitive target metadata for the UI. */
export function displayApprovalScopeTarget(scope: unknown): string[] {
  const record = scope && typeof scope === "object" && !Array.isArray(scope)
    ? scope as Record<string, unknown>
    : null;
  const target = record?.target && typeof record.target === "object" && !Array.isArray(record.target)
    ? record.target as Record<string, unknown>
    : null;
  if (!target) return [];
  return [
    typeof target.type === "string" && target.type.trim() ? `target ${target.type.trim()}` : null,
    Object.prototype.hasOwnProperty.call(target, "reference")
      ? `target reference ${digestOpaqueReference(target.reference)}`
      : null,
  ].filter((value): value is string => Boolean(value));
}

/**
 * Return true only when the API supplied a fresh approval row with an
 * authenticated browser binding that exactly matches its owner metadata.
 * Missing owner/session, scope, or expiry values fail closed. Bound-revision
 * metadata stays visible to the operator; a row without it remains
 * inspectable but cannot claim a revision-specific authorization.
 * The backend approval repository gives pending rows a finite decision
 * window; legacy rows without one cannot be rendered as actionable authority.
 */
export function isApprovalAuthorityReady(
  approval: ApprovalAuthorityRecord | null | undefined,
  auth: OperatorAuthBinding,
  approvalLoadState: ApprovalLoadState,
  now = Date.now(),
): boolean {
  if (!approval || approvalLoadState !== "ready" || auth.status !== "authenticated") return false;

  const status = text(approval.status).toLowerCase();
  if (!status || !["pending", "awaiting_approval", "approval_required"].includes(status)) return false;

  const ownerPrincipal = text(approval.approval_owner_principal_id ?? approval.owner_principal_id);
  const ownerSession = text(approval.approval_owner_operator_session_id ?? approval.operator_session_id);
  const currentPrincipal = text(auth.principalId);
  const currentSession = text(auth.sessionId);
  if (!ownerPrincipal || !ownerSession || !currentPrincipal || !currentSession) return false;
  if (ownerPrincipal !== currentPrincipal || ownerSession !== currentSession) return false;

  const conversation = text(approval.approval_conversation_id ?? approval.conversation_id);
  const executionSession = text(approval.session_id) || text(approval.thread_id);
  if (!conversation || !executionSession) return false;

  if (!approvalScope(approval) || Object.keys(approvalScope(approval) ?? {}).length === 0) return false;
  const suppliedExpiry = approvalExpiry(approval);
  if (suppliedExpiry === undefined || suppliedExpiry === null) return false;
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
    principal: redactIdentifier(approval?.approval_owner_principal_id ?? approval?.owner_principal_id),
    session: redactIdentifier(approval?.approval_owner_operator_session_id ?? approval?.operator_session_id),
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
  criterionId?: unknown;
  planRevision?: unknown;
  candidateId?: unknown;
  toolName?: unknown;
  sessionId?: unknown;
  conversationId?: unknown;
  ownerPrincipalId?: unknown;
  operatorSessionId?: unknown;
  pendingApprovalIds?: readonly string[] | null;
  pendingApprovals?: readonly ApprovalCandidate[] | null;
};

function approvalMatchesWorkflow(
  approval: ApprovalCandidate,
  workflow: WorkflowApprovalBinding,
): boolean {
  const workflowId = text(workflow.workflowId);
  if (!workflowId || text(approval.workflow_id) !== workflowId) return false;

  const workflowToolName = text(workflow.toolName);
  if (workflowToolName && text(approval.tool_name) !== workflowToolName) return false;
  const workflowSessionId = text(workflow.sessionId);
  if (!workflowSessionId) return false;
  if (
    text(approval.session_id) !== workflowSessionId
    || text(approval.approval_conversation_id ?? approval.conversation_id)
      !== (text(workflow.conversationId) || workflowSessionId)
  ) return false;

  const workflowOwnerPrincipalId = text(workflow.ownerPrincipalId);
  const approvalOwnerPrincipalId = text(
    approval.approval_owner_principal_id ?? approval.owner_principal_id,
  );
  if (!workflowOwnerPrincipalId || !approvalOwnerPrincipalId || workflowOwnerPrincipalId !== approvalOwnerPrincipalId) {
    return false;
  }

  const workflowOperatorSessionId = text(workflow.operatorSessionId);
  const approvalOperatorSessionId = text(
    approval.approval_owner_operator_session_id ?? approval.operator_session_id,
  );
  if (!workflowOperatorSessionId || !approvalOperatorSessionId || workflowOperatorSessionId !== approvalOperatorSessionId) {
    return false;
  }

  const workflowGoalId = text(workflow.goalId);
  const workflowCriterionId = text(workflow.criterionId);
  const workflowGoalRevision = workflow.goalRevision;
  if (!workflowGoalId || !workflowCriterionId || !Number.isInteger(workflowGoalRevision)) return false;
  if (text(approval.goal_id) !== workflowGoalId) return false;
  if (text(approval.criterion_id) !== workflowCriterionId) return false;
  if (!Number.isInteger(approval.goal_revision) || approval.goal_revision !== workflowGoalRevision) return false;
  if (!Number.isInteger(workflow.planRevision) || !Number.isInteger(approval.plan_revision)) return false;
  if (approval.plan_revision !== workflow.planRevision) return false;
  const workflowCandidateId = text(workflow.candidateId);
  if (text(approval.candidate_id) !== workflowCandidateId) return false;
  return true;
}

/** Select only an approval explicitly bound to the inspected workflow identity. */
export function selectApprovalForWorkflow<T extends ApprovalCandidate>(
  pending: readonly T[],
  workflow: WorkflowApprovalBinding | null | undefined,
): T | null {
  if (!workflow) return null;

  const pendingIds = workflow.pendingApprovalIds?.filter((id) => typeof id === "string" && id.trim()) ?? [];
  const candidates = [
    ...pending,
    ...(workflow.pendingApprovals ?? []),
  ].filter((approval) => (
    (pendingIds.length === 0 || pendingIds.includes(approval.id))
    && approvalMatchesWorkflow(approval, workflow)
  ));
  const uniqueCandidates = [...new Map(candidates.map((approval) => [approval.id, approval])).values()];
  return uniqueCandidates.length === 1 ? uniqueCandidates[0] as T : null;
}
