/**
 * Shared, fail-closed checks for approval controls.
 *
 * The browser authentication session is separate from the conversation or
 * workflow session attached to an approval.  A conversation id alone cannot
 * prove that the current operator owns an effectful approval.
 */

export type ApprovalAuthorityRecord = {
  status?: unknown;
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
  approval_scope?: unknown;
  approval_context?: unknown;
  goal_revision?: unknown;
  plan_revision?: unknown;
};

export type OperatorAuthBinding = {
  status: "loading" | "authenticated" | "unauthorized" | "degraded" | string;
  principalId: string | null;
  sessionId: string | null;
};

export type ApprovalLoadState = "loading" | "ready" | "stale";

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
  if (status && !["pending", "awaiting_approval", "approval_required"].includes(status)) return false;

  const ownerPrincipal = text(approval.approval_owner_principal_id);
  const ownerSession = text(approval.approval_owner_operator_session_id);
  const currentPrincipal = text(auth.principalId);
  const currentSession = text(auth.sessionId);
  if (!ownerPrincipal || !ownerSession || !currentPrincipal || !currentSession) return false;
  if (ownerPrincipal !== currentPrincipal || ownerSession !== currentSession) return false;

  const conversation = text(approval.approval_conversation_id);
  const executionSession = text(approval.session_id) || text(approval.thread_id);
  if (conversation && (!executionSession || conversation !== executionSession)) return false;

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
  toolName?: unknown;
  sessionId?: unknown;
  pendingApprovalIds?: readonly string[] | null;
  pendingApprovals?: readonly ApprovalCandidate[] | null;
};

/** Select only an approval explicitly bound to the inspected workflow. */
export function selectApprovalForWorkflow<T extends ApprovalCandidate>(
  pending: readonly T[],
  workflow: WorkflowApprovalBinding | null | undefined,
): T | null {
  if (!workflow) return null;

  const pendingIds = workflow.pendingApprovalIds?.filter((id) => typeof id === "string" && id.trim()) ?? [];
  if (pendingIds.length > 0) {
    const byId = pending.find((approval) => pendingIds.includes(approval.id));
    if (byId) return byId;
    const attachedById = workflow.pendingApprovals?.find((approval) => pendingIds.includes(approval.id));
    if (attachedById) return attachedById as T;
    return null;
  }

  const toolName = text(workflow.toolName);
  const sessionId = text(workflow.sessionId);
  if (toolName && sessionId) {
    const byContext = pending.find((approval) => (
      text(approval.tool_name) === toolName
      && (text(approval.session_id) || text(approval.thread_id)) === sessionId
    ));
    if (byContext) return byContext;
  }

  const attached = workflow.pendingApprovals?.[0];
  return attached ? attached as T : null;
}
