import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  createReadConsent,
  listCalendarConnections,
  listCalendarEvents,
  verifyCalendarConnection,
} from "../../lib/calendar";
import type {
  CalendarConnectionMetadata,
  CalendarEventListResponse,
  CalendarVerifyResponse,
  GoalInfo,
  WorkBoardTask,
  WorkBoardRoutinePackageApproval,
  WorkBoardRoutinePackagePreview,
} from "../../types";
import {
  isPublicSchedulableProcedure,
  correlateInvocationReceipt,
  correlateScheduleReceipt,
  newProcedureRequestKey,
  procedureTemplateLabel,
  procedureV2Api,
  ProcedureV2ApiError,
  sourceTaskFromWorkBoard,
  type ProcedureV2CadenceKind,
  type ProcedureV2InvokeParameters,
  type ProcedureV2InvokeRequest,
  type ProcedureV2InvokeReceipt,
  type ProcedureV2PrepareRequest,
  type ProcedureV2Preview,
  type ProcedureV2Prepared,
  type ProcedureV2Routine,
  type ProcedureV2ScheduleReceipt,
  type ProcedureV2ScheduleRequest,
  type ProcedureV2SourceTask,
  type ProcedureV2SourceWatch,
  type ProcedureV2TemplateId,
} from "../../lib/procedureV2Api";

export interface ProcedureV2ReviewProps {
  active?: boolean;
  ownerPrincipalId?: string | null;
  ownerSessionId?: string | null;
  selectedSourceTask?: WorkBoardTask | null;
  goals?: GoalInfo[];
  pendingApprovals?: ProcedureApprovalSummary[];
  onOpenTask?: (taskId: string) => void;
  onOpenApprovals?: (approvalId: string) => void;
}

export interface ProcedureApprovalSummary {
  id: string;
  status: string;
  tool_name: string;
  summary: string;
  expires_at?: string | number | null;
}

interface PendingPrepareProcedureAction {
  schema_version: 1;
  kind: "prepare";
  request: ProcedureV2PrepareRequest;
}

interface PendingInvokeProcedureAction {
  schema_version: 1;
  kind: "invoke";
  routineId: string;
  /** Server-owned version identity captured before the first POST. */
  versionId?: string | null;
  request: ProcedureV2InvokeRequest;
}

interface PendingScheduleProcedureAction {
  schema_version: 1;
  kind: "schedule";
  routineId: string;
  request: ProcedureV2ScheduleRequest;
}

type PendingProcedureAction = PendingPrepareProcedureAction | PendingInvokeProcedureAction | PendingScheduleProcedureAction;

interface PreparedProcedure {
  schema_version: 1;
  routineId: string;
  bindingId: string;
  revision: number;
  version: number;
  /** Optional for old sessionStorage receipts; never inferred across owners. */
  versionId?: string | null;
  installJobId: string | null;
  approvalId: string | null;
  installApprovalStatus?: string | null;
  installApprovalExpiresAt?: string | null;
  installRecoveryAction?: string | null;
  /** Same owner/session recovery gate for a package mutation with an unknown outcome. */
  packageActivationRecovery?: PackageActivationRecovery | null;
  /** Same owner/session recovery gate for every routine lifecycle mutation. */
  lifecycleRecovery?: LifecycleRecovery | null;
}

interface PackageActivationRecovery {
  schema_version: 1;
  routineId: string;
  versionId: string;
  version: number;
  digest: string;
  expectedRevision: number;
  approvalId: string;
}

type LifecycleAction = "install" | "activate" | "pause" | "revoke" | "rollback";

interface LifecycleRecovery {
  schema_version: 1;
  action: LifecycleAction;
  ownerPrincipalId: string;
  ownerSessionId: string;
  routineId: string;
  versionId: string;
  version: number;
  requestRevision: number;
  expectedState: "installed" | "active" | "paused" | "revoked";
  approvalId: string | null;
  installJobId: string | null;
  targetVersionId: string | null;
  targetVersion: number | null;
  packageDigest: string | null;
}

interface OwnerRequestScope {
  key: string | null;
  generation: number;
}

const PENDING_STORAGE_PREFIX = "seraph.procedure-v2.pending";
const PREPARED_STORAGE_PREFIX = "seraph.procedure-v2.prepared";
const DEFAULT_MEETING_PURPOSE = "bounded preparation request";

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function pendingStorageKey(principalId: string | null | undefined, sessionId: string | null | undefined): string | null {
  if (!principalId || !sessionId) return null;
  return `${PENDING_STORAGE_PREFIX}:${encodeURIComponent(principalId)}:${encodeURIComponent(sessionId)}`;
}

function preparedStorageKey(principalId: string | null | undefined, sessionId: string | null | undefined): string | null {
  if (!principalId || !sessionId) return null;
  return `${PREPARED_STORAGE_PREFIX}:${encodeURIComponent(principalId)}:${encodeURIComponent(sessionId)}`;
}

function readLifecycleRecovery(
  value: unknown,
  ownerPrincipalId?: string | null,
  ownerSessionId?: string | null,
): LifecycleRecovery | null | undefined {
  if (value === undefined) return undefined;
  if (value === null) return null;
  if (!isRecord(value)
    || value.schema_version !== 1
    || (value.action !== "install" && value.action !== "activate" && value.action !== "pause" && value.action !== "revoke" && value.action !== "rollback")
    || typeof value.ownerPrincipalId !== "string" || !value.ownerPrincipalId
    || typeof value.ownerSessionId !== "string" || !value.ownerSessionId
    || typeof value.routineId !== "string" || !value.routineId
    || typeof value.versionId !== "string" || !value.versionId
    || typeof value.version !== "number" || !Number.isSafeInteger(value.version) || value.version < 1
    || typeof value.requestRevision !== "number" || !Number.isSafeInteger(value.requestRevision) || value.requestRevision < 1
    || (value.expectedState !== "installed" && value.expectedState !== "active" && value.expectedState !== "paused" && value.expectedState !== "revoked")
    || (value.approvalId !== null && (typeof value.approvalId !== "string" || !value.approvalId))
    || (value.installJobId !== null && (typeof value.installJobId !== "string" || !value.installJobId))
    || (value.targetVersionId !== null && (typeof value.targetVersionId !== "string" || !value.targetVersionId))
    || (value.targetVersion !== null && (typeof value.targetVersion !== "number" || !Number.isSafeInteger(value.targetVersion) || value.targetVersion < 1))
    || (value.packageDigest !== null && (typeof value.packageDigest !== "string" || !value.packageDigest))) {
    throw new Error("The retained procedure lifecycle recovery receipt is invalid.");
  }
  if ((ownerPrincipalId && value.ownerPrincipalId !== ownerPrincipalId)
    || (ownerSessionId && value.ownerSessionId !== ownerSessionId)) {
    throw new Error("The retained procedure lifecycle recovery receipt belongs to another operator session.");
  }
  if ((value.action === "install" && (value.expectedState !== "installed" || !value.approvalId || value.targetVersionId !== null || value.targetVersion !== null))
    || (value.action === "activate" && (value.expectedState !== "active" || value.approvalId !== null || value.targetVersionId !== null || value.targetVersion !== null))
    || (value.action === "pause" && (value.expectedState !== "paused" || value.approvalId !== null || value.targetVersionId !== null || value.targetVersion !== null))
    || (value.action === "revoke" && (value.expectedState !== "revoked" || value.approvalId !== null || value.targetVersionId !== null || value.targetVersion !== null))
    || (value.action === "rollback" && (value.expectedState === "revoked" || value.approvalId !== null))) {
    throw new Error("The retained procedure lifecycle recovery receipt has an invalid action state binding.");
  }
  if (value.action === "install" && (!value.installJobId || !value.packageDigest)) {
    throw new Error("The retained procedure install recovery receipt is missing its exact job or package digest.");
  }
  if (value.action !== "install" && value.installJobId !== null) {
    throw new Error("The retained procedure lifecycle recovery receipt has an unexpected install job.");
  }
  if ((value.targetVersionId === null) !== (value.targetVersion === null)) {
    throw new Error("The retained procedure lifecycle recovery receipt has an incomplete target version.");
  }
  if (value.action === "rollback" && (!value.targetVersionId || value.targetVersion === null
    || value.targetVersionId !== value.versionId || value.targetVersion !== value.version)) {
    throw new Error("The retained rollback recovery receipt is missing its exact target version.");
  }
  return {
    schema_version: 1,
    action: value.action,
    ownerPrincipalId: value.ownerPrincipalId,
    ownerSessionId: value.ownerSessionId,
    routineId: value.routineId,
    versionId: value.versionId,
    version: value.version,
    requestRevision: value.requestRevision,
    expectedState: value.expectedState,
    approvalId: value.approvalId,
    installJobId: value.installJobId,
    targetVersionId: value.targetVersionId,
    targetVersion: value.targetVersion,
    packageDigest: value.packageDigest,
  };
}

function readPackageActivationRecovery(value: unknown): PackageActivationRecovery | null | undefined {
  if (value === undefined) return undefined;
  if (value === null) return null;
  if (!isRecord(value)
    || value.schema_version !== 1
    || typeof value.routineId !== "string" || !value.routineId
    || typeof value.versionId !== "string" || !value.versionId
    || typeof value.version !== "number" || !Number.isSafeInteger(value.version) || value.version < 1
    || typeof value.digest !== "string" || !value.digest
    || typeof value.expectedRevision !== "number" || !Number.isSafeInteger(value.expectedRevision) || value.expectedRevision < 1
    || typeof value.approvalId !== "string" || !value.approvalId) {
    throw new Error("The retained package activation recovery receipt is invalid.");
  }
  return {
    schema_version: 1,
    routineId: value.routineId,
    versionId: value.versionId,
    version: value.version,
    digest: value.digest,
    expectedRevision: value.expectedRevision,
    approvalId: value.approvalId,
  };
}

function readPrepared(key: string | null, ownerPrincipalId?: string | null, ownerSessionId?: string | null): PreparedProcedure | null {
  if (!key || typeof window === "undefined") return null;
  try {
    const raw = window.sessionStorage.getItem(key);
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<PreparedProcedure>;
    if (value.schema_version !== 1
      || typeof value.routineId !== "string" || !value.routineId
      || typeof value.bindingId !== "string" || !value.bindingId
      || typeof value.revision !== "number" || !Number.isSafeInteger(value.revision) || value.revision < 1
      || typeof value.version !== "number" || !Number.isSafeInteger(value.version) || value.version < 1
      || (value.versionId !== undefined && value.versionId !== null && (typeof value.versionId !== "string" || !value.versionId))
      || (value.installJobId !== null && (typeof value.installJobId !== "string" || !value.installJobId))
      || (value.approvalId !== null && (typeof value.approvalId !== "string" || !value.approvalId))
      || (value.installApprovalStatus !== undefined && value.installApprovalStatus !== null && typeof value.installApprovalStatus !== "string")
      || (value.installApprovalExpiresAt !== undefined && value.installApprovalExpiresAt !== null && typeof value.installApprovalExpiresAt !== "string")
      || (value.installRecoveryAction !== undefined && value.installRecoveryAction !== null && typeof value.installRecoveryAction !== "string")) return null;
    const packageActivationRecovery = readPackageActivationRecovery(value.packageActivationRecovery);
    const lifecycleRecovery = readLifecycleRecovery(value.lifecycleRecovery, ownerPrincipalId, ownerSessionId);
    return {
      schema_version: 1,
      routineId: value.routineId,
      bindingId: value.bindingId,
      revision: value.revision,
      version: value.version,
      versionId: value.versionId,
      installJobId: value.installJobId,
      approvalId: value.approvalId,
      installApprovalStatus: value.installApprovalStatus,
      installApprovalExpiresAt: value.installApprovalExpiresAt,
      installRecoveryAction: value.installRecoveryAction,
      packageActivationRecovery,
      lifecycleRecovery: lifecycleRecovery ?? null,
    };
  } catch {
    return null;
  }
}

function persistPrepared(key: string | null, value: PreparedProcedure): string | null {
  if (!key || typeof window === "undefined") return "The authenticated session is unavailable, so the exact prepared approval cannot be retained across remounts.";
  try {
    const encoded = JSON.stringify(value);
    if (encoded.length > 32 * 1024) return "The prepared approval receipt is too large to retain safely.";
    window.sessionStorage.setItem(key, encoded);
    if (window.sessionStorage.getItem(key) !== encoded) return "The prepared approval receipt could not be read back from session storage.";
    return null;
  } catch { return "The prepared approval receipt could not be retained in this session."; }
}

function clearPrepared(key: string | null): void {
  if (!key || typeof window === "undefined") return;
  try { window.sessionStorage.removeItem(key); } catch { /* retain no user-facing failure for cleanup */ }
}

function readPending(key: string | null): PendingProcedureAction | null {
  if (!key || typeof window === "undefined") return null;
  try {
    const raw = window.sessionStorage.getItem(key);
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<PendingProcedureAction>;
    if (value.schema_version !== 1 || !value.request || typeof value.request !== "object") return null;
    if (value.kind === "prepare") return value as PendingPrepareProcedureAction;
    if ((value.kind !== "invoke" && value.kind !== "schedule") || typeof value.routineId !== "string" || !value.routineId) return null;
    if (value.kind === "invoke"
      && value.versionId !== undefined
      && value.versionId !== null
      && (typeof value.versionId !== "string" || !value.versionId)) return null;
    return value as PendingInvokeProcedureAction | PendingScheduleProcedureAction;
  } catch {
    return null;
  }
}

function persistPending(key: string | null, value: PendingProcedureAction): string | null {
  if (!key || typeof window === "undefined") return "The authenticated session is unavailable, so the exact request cannot be retained.";
  try {
    const encoded = JSON.stringify(value);
    if (encoded.length > 128 * 1024) return "The exact request is too large to retain safely. No mutation was sent.";
    window.sessionStorage.setItem(key, encoded);
    if (window.sessionStorage.getItem(key) !== encoded) return "The exact request could not be read back from session storage. No mutation was sent.";
    return null;
  } catch {
    return "The exact request could not be retained in session storage. No mutation was sent.";
  }
}

function clearPending(key: string | null): void {
  if (!key || typeof window === "undefined") return;
  try { window.sessionStorage.removeItem(key); } catch { /* keep the durable server receipt as the authority */ }
}

function formatTime(value: string | null | undefined): string {
  if (!value) return "unknown";
  const timestamp = new Date(value).getTime();
  return Number.isFinite(timestamp) ? new Date(timestamp).toLocaleString() : value;
}

function localDateTimeToIso(value: string): string {
  const timestamp = new Date(value).getTime();
  return Number.isFinite(timestamp) ? new Date(timestamp).toISOString() : value;
}

function errorMessage(error: unknown): string {
  if (error instanceof ProcedureV2ApiError) {
    const recovery = error.recoveryAction ? ` Recovery: ${error.recoveryAction}.` : "";
    return `${error.message}${recovery}`;
  }
  return error instanceof Error ? error.message : "The governed procedure request failed.";
}

// These codes are emitted after the server has rejected the request during a
// typed authority/validation check, before a task, schedule, or procedure
// mutation can be committed. Every other failure keeps the exact request for
// reconciliation because a response may have been lost after an effect.
const DEFINITIVE_PRE_EFFECT_CODES = new Set([
  "procedure_source_task_not_found",
  "procedure_source_task_revision_stale",
  "procedure_source_task_not_verified",
  "procedure_source_attempt_not_verified",
  "procedure_source_job_missing",
  "procedure_source_job_binding_invalid",
  "procedure_source_proof_missing",
  "procedure_source_input_binding_invalid",
  "procedure_source_capability_unsupported",
  "procedure_source_artifact_missing",
  "procedure_template_unknown",
  "procedure_source_count_invalid",
  "procedure_source_capability_mismatch",
  "procedure_source_capability_unavailable",
  "procedure_source_goal_lineage_mismatch",
  "procedure_source_no_material_change",
  "procedure_source_input_binding_missing",
  "procedure_preview_expired",
  "procedure_preview_digest_mismatch",
  "procedure_routine_not_found",
  "procedure_version_not_found",
  "procedure_version_schema_invalid",
  "procedure_version_proof_invalid",
  "procedure_version_not_active",
  "procedure_package_not_current",
  "procedure_plan_digest_changed",
  "procedure_source_proof_changed",
  "procedure_parameters_invalid",
  "procedure_goal_binding_mismatch",
  "procedure_watch_authority_stale",
  "procedure_invocation_id_invalid",
  "procedure_goal_authority_stale",
  "procedure_plan_invalid",
  "procedure_goal_budget_missing",
  "procedure_goal_budget_invalid",
  "procedure_goal_budget_missing_reviewed_grant",
  "procedure_goal_proactivity_disabled",
  "procedure_goal_budget_finite_expiry_required",
  "procedure_goal_budget_period_not_started",
  "procedure_goal_budget_period_expired",
  "procedure_invocation_conflict",
  "procedure_template_not_schedulable",
  "procedure_schedule_expiry_invalid",
  "procedure_schedule_expiry_exceeds_goal_budget",
  "procedure_schedule_cadence_invalid",
  "procedure_schedule_conflict",
]);

function isDefinitivePreEffectFailure(error: unknown): error is ProcedureV2ApiError {
  return error instanceof ProcedureV2ApiError
    && !error.retryable
    && DEFINITIVE_PRE_EFFECT_CODES.has(error.code);
}

function isConfirmedInvocationStatus(status: string): boolean {
  return status === "accepted" || status === "succeeded";
}

function isConfirmedScheduleStatus(status: string): boolean {
  return status === "scheduled";
}

function isCurrentInstallApproval(prepared: PreparedProcedure | null): boolean {
  if (!prepared?.approvalId || prepared.installApprovalStatus !== "approved" || !prepared.installApprovalExpiresAt) return false;
  const expiry = new Date(prepared.installApprovalExpiresAt).getTime();
  return Number.isFinite(expiry) && expiry > Date.now();
}

function installApprovalNeedsFreshRebind(
  prepared: PreparedProcedure | null,
  status: string,
  current: boolean,
): boolean {
  if (!prepared?.approvalId) return true;
  // Pending and consumed receipts have a live operator-facing recovery path:
  // review the pending approval or reconcile the already-consumed receipt.
  if (status === "pending" || status === "consumed") return false;
  return !current;
}

function preparedFromRoutine(routine: ProcedureV2Routine, current: PreparedProcedure | null, requestedVersion?: number): PreparedProcedure | null {
  const versionNumber = requestedVersion ?? current?.version ?? routine.current_version ?? routine.versions[0]?.version;
  const version = current?.versionId
    ? routine.versions.find((candidate) => candidate.id === current.versionId && candidate.version === versionNumber)
    : routine.versions.find((candidate) => candidate.version === versionNumber)
      ?? routine.versions.find((candidate) => candidate.version === routine.current_version)
      ?? routine.versions[0];
  const binding = version?.procedure_binding;
  if (!version?.template_id || !binding?.binding_id || !binding.revision) return null;
  return {
    schema_version: 1,
    routineId: routine.id,
    bindingId: binding.binding_id,
    revision: binding.revision,
    version: version.version,
    versionId: version.id,
    installJobId: binding.install_job_id,
    approvalId: binding.approval_id,
    installApprovalStatus: binding.install_approval_status,
    installApprovalExpiresAt: binding.install_approval_expires_at,
    installRecoveryAction: binding.install_recovery_action,
    packageActivationRecovery: current?.routineId === routine.id && current.versionId === version.id
      ? current.packageActivationRecovery
      : null,
    lifecycleRecovery: current?.routineId === routine.id && current.versionId === version.id
      ? current.lifecycleRecovery
      : null,
  };
}

function sourceEligibility(task: ProcedureV2SourceTask, templateId: ProcedureV2TemplateId): boolean {
  if (task.status !== "done" || task.readback_status !== "verified" || task.verification_status !== "passed") return false;
  if (templateId === "public-browser-check") return task.capability_id === "browser.public-task.v1";
  if (templateId === "watch-and-public-browser") return task.capability_id === "guardian.research-watch.v1" || task.capability_id === "browser.public-task.v1";
  return task.capability_id === "calendar.meeting-prep.v1";
}

type OwnerScopedSource = Pick<WorkBoardTask, "owner_principal_id" | "owner_session_id">
  | Pick<ProcedureV2SourceTask, "owner_principal_id" | "owner_session_id">
  | Pick<ProcedureV2SourceWatch, "owner_principal_id" | "owner_session_id">;

function sourceBelongsToOwner(task: OwnerScopedSource, principalId: string | null | undefined, sessionId: string | null | undefined): boolean {
  // A missing owner component is an unverified selection.  Treating it as a
  // wildcard lets a task with incomplete metadata cross the owner boundary.
  return Boolean(principalId && sessionId)
    && task.owner_principal_id === principalId
    && task.owner_session_id === sessionId;
}

function routineBelongsToOwner(routine: ProcedureV2Routine, requestedRoutineId: string, principalId: string | null | undefined): boolean {
  return Boolean(principalId)
    && routine.id === requestedRoutineId
    && routine.owner_principal_id === principalId;
}

function sourceLabel(task: ProcedureV2SourceTask): string {
  return `${task.title} · ${task.capability_id ?? "capability unavailable"} · ${task.task_id.slice(0, 16)} · revision ${task.task_revision}`;
}

function isRoutineActive(routine: ProcedureV2Routine | null, packagePreview: WorkBoardRoutinePackagePreview | null, versionId?: string | null): boolean {
  if (!routine || routine.state !== "active" || routine.package?.status !== "active") return false;
  const currentVersion = routine.current_version ?? routine.versions[0]?.version;
  const version = routine.versions.find((candidate) => candidate.version === currentVersion);
  return Boolean(version?.installed_package_digest
    && (!versionId || version.id === versionId)
    && routine.package.digest === version.installed_package_digest)
    && (!packagePreview || (packagePreview.status === "active" && packagePreview.digest === packagePreview.installed_package_digest));
}

function routinePackageMatchesVersion(routine: ProcedureV2Routine | null, versionNumber: number, versionId?: string | null): boolean {
  if (!routine || routine.package?.status !== "active") return false;
  const version = routine.versions.find((candidate) => candidate.version === versionNumber);
  return Boolean(version?.installed_package_digest
    && (!versionId || version.id === versionId)
    && routine.package.digest === version.installed_package_digest);
}

function lifecycleReadbackBelongsToOwner(
  routine: ProcedureV2Routine,
  routineId: string,
  principalId: string | null | undefined,
  sessionId: string | null | undefined,
): boolean {
  if (!routineBelongsToOwner(routine, routineId, principalId)) return false;
  // The current routine DTO is owner-principal scoped.  If a future additive
  // response includes the operator session, accept it only when it matches the
  // session that issued this request; never treat a returned session as a
  // substitute for the authenticated request scope.
  const returnedSessionId = (routine as ProcedureV2Routine & { owner_session_id?: string | null }).owner_session_id;
  return returnedSessionId === undefined || returnedSessionId === sessionId;
}

function lifecycleMutationReadbackIsCurrent(
  routine: ProcedureV2Routine,
  recovery: LifecycleRecovery,
  principalId: string | null | undefined,
  sessionId: string | null | undefined,
): boolean {
  if (recovery.ownerPrincipalId !== principalId || recovery.ownerSessionId !== sessionId) return false;
  if (!lifecycleReadbackBelongsToOwner(routine, recovery.routineId, principalId, sessionId)
    || routine.revision <= recovery.requestRevision
    || routine.current_version !== recovery.version
    || routine.state !== recovery.expectedState) return false;
  const version = routine.versions.find((candidate) => candidate.id === recovery.versionId && candidate.version === recovery.version);
  if (!version) return false;
  if (recovery.action === "install") {
    const binding = version.procedure_binding;
    return Boolean(recovery.approvalId
      && recovery.installJobId
      && recovery.packageDigest
      && binding
      && binding.approval_id === recovery.approvalId
      && binding.install_job_id === recovery.installJobId
      && version.installed_package_digest === recovery.packageDigest);
  }
  if (recovery.action === "activate" || recovery.action === "rollback") {
    return routinePackageMatchesVersion(routine, recovery.version, recovery.versionId)
      && (!recovery.packageDigest || routine.package?.digest === recovery.packageDigest);
  }
  return true;
}

function packageActivationReadbackIsCurrent(
  routine: ProcedureV2Routine,
  context: PackageActivationRecovery,
  principalId: string | null | undefined,
  sessionId: string | null | undefined,
): boolean {
  const result = lifecycleReadbackBelongsToOwner(routine, context.routineId, principalId, sessionId)
    && routine.current_version === context.version
    // Package activation changes the external package pointer; the routine
    // row itself is not the lifecycle activation mutation. Keep the exact
    // revision captured for the guarded package request.
    && routine.revision === context.expectedRevision
    && routine.package?.status === "active"
    && routine.package.digest === context.digest
    && routinePackageMatchesVersion(routine, context.version, context.versionId);
  return result;
}

const DEFINITIVE_LIFECYCLE_PRE_EFFECT_CODES = new Set([
  "routine_not_found",
  "routine_owner_session_mismatch",
  "routine_revision_stale",
  "routine_revision_or_version_invalid",
  "routine_revoked_terminal",
  "routine_version_not_found",
  "routine_version_not_installed",
  "routine_version_not_current",
  "routine_install_job_missing",
  "routine_install_not_awaiting_approval",
  "routine_install_revision_stale",
  "package_review_required",
]);

function isDefinitiveLifecyclePreEffectFailure(error: unknown): error is ProcedureV2ApiError {
  return error instanceof ProcedureV2ApiError
    && !error.retryable
    && error.status < 500
    && DEFINITIVE_LIFECYCLE_PRE_EFFECT_CODES.has(error.code);
}

function revisionForGoal(goal: GoalInfo | undefined): number | null {
  return goal && typeof goal.revision === "number" && Number.isSafeInteger(goal.revision) && goal.revision >= 1 ? goal.revision : null;
}

export function ProcedureV2Review({
  active = true,
  ownerPrincipalId,
  ownerSessionId,
  selectedSourceTask = null,
  goals = [],
  pendingApprovals = [],
  onOpenTask,
  onOpenApprovals,
}: ProcedureV2ReviewProps) {
  const [templateId, setTemplateId] = useState<ProcedureV2TemplateId>("public-browser-check");
  const [tasks, setTasks] = useState<ProcedureV2SourceTask[]>([]);
  const [taskLoadError, setTaskLoadError] = useState<string | null>(null);
  const [sourceTaskId, setSourceTaskId] = useState("");
  const [secondSourceTaskId, setSecondSourceTaskId] = useState("");
  const [existingRoutines, setExistingRoutines] = useState<ProcedureV2Routine[]>([]);
  const routineListGenerationRef = useRef(0);
  const [existingRoutineId, setExistingRoutineId] = useState("");
  const [existingVersionNumber, setExistingVersionNumber] = useState<number | null>(null);
  const [existingRoutineLoading, setExistingRoutineLoading] = useState(false);
  const [existingRoutineError, setExistingRoutineError] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [previewRequestKey, setPreviewRequestKey] = useState(() => newProcedureRequestKey("procedure-preview"));
  const [preview, setPreview] = useState<ProcedureV2Preview | null>(null);
  const preparedKey = useMemo(() => preparedStorageKey(ownerPrincipalId, ownerSessionId), [ownerPrincipalId, ownerSessionId]);
  const [prepared, setPrepared] = useState<PreparedProcedure | null>(() => readPrepared(preparedKey, ownerPrincipalId, ownerSessionId));
  const preparedKeyRef = useRef(preparedKey);
  const ownerGenerationRef = useRef(0);
  const mountedRef = useRef(false);
  const preparedResetKeyRef = useRef(preparedKey);
  // Advance the owner generation during render so a promise settling between
  // render and effects cannot publish old-owner metadata into the new view.
  if (preparedKeyRef.current !== preparedKey) {
    preparedKeyRef.current = preparedKey;
    ownerGenerationRef.current += 1;
  }
  const captureOwnerScope = (): OwnerRequestScope => ({ key: preparedKey, generation: ownerGenerationRef.current });
  const ownerRequestIsCurrent = (scope: OwnerRequestScope): boolean => mountedRef.current
    && preparedKeyRef.current === scope.key
    && ownerGenerationRef.current === scope.generation;
  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);
  const [routine, setRoutine] = useState<ProcedureV2Routine | null>(null);
  const [packagePreview, setPackagePreview] = useState<WorkBoardRoutinePackagePreview | null>(null);
  const [packageApproval, setPackageApproval] = useState<WorkBoardRoutinePackageApproval | null>(null);
  const [goalId, setGoalId] = useState("");
  const [watchId, setWatchId] = useState("");
  const [sourceWatches, setSourceWatches] = useState<ProcedureV2SourceWatch[]>([]);
  const [sourceWatchError, setSourceWatchError] = useState<string | null>(null);
  const [calendarConnections, setCalendarConnections] = useState<CalendarConnectionMetadata[]>([]);
  const [calendarConnectionId, setCalendarConnectionId] = useState("");
  const [calendarVerification, setCalendarVerification] = useState<CalendarVerifyResponse | null>(null);
  const [calendarId, setCalendarId] = useState("");
  const [calendarConsent, setCalendarConsent] = useState<{ consentId: string; revision: number; connectionRevision: number; goalId: string } | null>(null);
  const [calendarEvents, setCalendarEvents] = useState<CalendarEventListResponse | null>(null);
  const [calendarEventId, setCalendarEventId] = useState("");
  const [calendarLoading, setCalendarLoading] = useState(false);
  const [calendarError, setCalendarError] = useState<string | null>(null);
  const [allowCalendarModel, setAllowCalendarModel] = useState(false);
  const [meetingPurpose, setMeetingPurpose] = useState(DEFAULT_MEETING_PURPOSE);
  const [cadenceKind, setCadenceKind] = useState<ProcedureV2CadenceKind>("daily");
  const [cadenceTimezone, setCadenceTimezone] = useState(() => Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC");
  const [cadenceHour, setCadenceHour] = useState("9");
  const [cadenceMinute, setCadenceMinute] = useState("0");
  const [scheduleExpiry, setScheduleExpiry] = useState("");
  const [schedule, setSchedule] = useState<ProcedureV2ScheduleReceipt | null>(null);
  const [invokeReceipt, setInvokeReceipt] = useState<ProcedureV2InvokeReceipt | null>(null);
  const [pending, setPending] = useState<PendingProcedureAction | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [lifecycleReconcileRequired, setLifecycleReconcileRequired] = useState(() => Boolean(readPrepared(preparedKey, ownerPrincipalId, ownerSessionId)?.lifecycleRecovery));
  const [lifecycleActivationRecovery, setLifecycleActivationRecovery] = useState<LifecycleRecovery | null>(() => readPrepared(preparedKey, ownerPrincipalId, ownerSessionId)?.lifecycleRecovery ?? null);
  const [packageActivationRecovery, setPackageActivationRecovery] = useState<PackageActivationRecovery | null>(() => readPrepared(preparedKey, ownerPrincipalId, ownerSessionId)?.packageActivationRecovery ?? null);

  const retainPackageActivationRecovery = (recovery: PackageActivationRecovery | null, basePrepared: PreparedProcedure | null = prepared): string | null => {
    setPackageActivationRecovery(recovery);
    if (!basePrepared) return "The prepared procedure is unavailable, so the package outcome cannot be retained safely.";
    const nextPrepared = { ...basePrepared, packageActivationRecovery: recovery };
    const retentionError = persistPrepared(preparedKey, nextPrepared);
    setPrepared(nextPrepared);
    if (retentionError) setNotice(`${retentionError} Keep this owner session open while reconciling the package outcome.`);
    return retentionError;
  };

  const retainLifecycleRecovery = (recovery: LifecycleRecovery | null, basePrepared: PreparedProcedure | null = prepared): string | null => {
    setLifecycleActivationRecovery(recovery);
    setLifecycleReconcileRequired(Boolean(recovery));
    if (!basePrepared) return "The prepared procedure is unavailable, so the lifecycle outcome cannot be retained safely.";
    const nextPrepared = { ...basePrepared, lifecycleRecovery: recovery };
    const retentionError = persistPrepared(preparedKey, nextPrepared);
    setPrepared(nextPrepared);
    if (retentionError) setNotice(`${retentionError} Keep this owner session open while reconciling the lifecycle outcome.`);
    return retentionError;
  };

  const pendingKey = useMemo(() => pendingStorageKey(ownerPrincipalId, ownerSessionId), [ownerPrincipalId, ownerSessionId]);
  const activeGoals = useMemo(() => goals.filter((goal) => goal.status.toLowerCase() === "active"), [goals]);
  const selectedGoal = activeGoals.find((goal) => goal.id === goalId) ?? activeGoals[0];
  const selectedSource = tasks.find((task) => task.task_id === sourceTaskId) ?? null;
  const selectedSecondSource = tasks.find((task) => task.task_id === secondSourceTaskId) ?? null;
  const selectedWatch = sourceWatches.find((watch) => watch.id === watchId) ?? null;
  const eligibleWatches = useMemo(
    () => sourceWatches.filter((watch) => sourceBelongsToOwner(watch, ownerPrincipalId, ownerSessionId)
      && watch.goal_id === selectedGoal?.id && watch.state === "active"),
    [ownerPrincipalId, ownerSessionId, selectedGoal?.id, sourceWatches],
  );
  const selectedCalendarEvent = calendarEvents?.events.find((event) => event.event_binding_id === calendarEventId) ?? null;
  const existingRoutineOptions = useMemo(
    () => existingRoutines.filter((candidate) => candidate.owner_principal_id === ownerPrincipalId
      && candidate.versions.some((version) => version.template_id && version.procedure_binding?.binding_id)),
    [existingRoutines, ownerPrincipalId],
  );
  const eligibleTasks = useMemo(() => tasks.filter((task) => sourceEligibility(task, templateId)), [tasks, templateId]);
  const browserTasks = useMemo(() => tasks.filter((task) => sourceEligibility(task, "public-browser-check")), [tasks]);
  const watchTasks = useMemo(() => tasks.filter((task) => sourceEligibility(task, "watch-and-public-browser")), [tasks]);
  const meetingTasks = useMemo(() => tasks.filter((task) => sourceEligibility(task, "selected-meeting-prep")), [tasks]);
  const currentVersion = existingRoutineId && existingVersionNumber
    ? existingVersionNumber
    : routine?.current_version ?? routine?.versions[0]?.version ?? prepared?.version ?? 1;
  const routineCurrentVersion = routine?.current_version ?? routine?.versions[0]?.version ?? null;
  const routineActive = isRoutineActive(routine, packagePreview, prepared?.versionId)
    && !lifecycleReconcileRequired
    && !packageActivationRecovery
    && (!existingRoutineId || !routineCurrentVersion || currentVersion === routineCurrentVersion);
  const unresolvedRecovery = Boolean(lifecycleReconcileRequired || packageActivationRecovery);

  const loadTasks = useCallback(async () => {
    const requestOwnerScope = captureOwnerScope();
    setTaskLoadError(null);
    try {
      const values = await procedureV2Api.listSourceTasks();
      if (!ownerRequestIsCurrent(requestOwnerScope)) return;
      setTasks(values.filter((task) => sourceBelongsToOwner(task, ownerPrincipalId, ownerSessionId)));
      if (selectedSourceTask && sourceBelongsToOwner(selectedSourceTask, ownerPrincipalId, ownerSessionId)) {
        const selected = sourceTaskFromWorkBoard(selectedSourceTask);
        setTasks((current) => current.some((task) => task.task_id === selected.task_id) ? current : [selected, ...current]);
        setSourceTaskId(selected.task_id);
      }
    } catch (cause) {
      if (ownerRequestIsCurrent(requestOwnerScope)) setTaskLoadError(errorMessage(cause));
    }
  }, [ownerPrincipalId, ownerSessionId, preparedKey, selectedSourceTask]);

  const loadExistingRoutine = async (routineId: string, requestedVersion?: number) => {
    if (pending) {
      setExistingRoutineError("Reconcile the retained exact request before selecting another procedure.");
      return;
    }
    if (packageActivationRecovery) {
      setExistingRoutineError("Refresh authority to reconcile the retained package activation before selecting another procedure.");
      return;
    }
    const requestOwnerScope = captureOwnerScope();
    setExistingRoutineId(routineId);
    setExistingRoutineLoading(true);
    setExistingRoutineError(null);
    try {
      const nextRoutine = await procedureV2Api.getRoutine(routineId);
      if (!ownerRequestIsCurrent(requestOwnerScope)) return;
      if (!routineBelongsToOwner(nextRoutine, routineId, ownerPrincipalId)) {
        throw new Error("The returned routine is not bound to the current operator.");
      }
      const candidates = nextRoutine.versions
        .filter((version) => version.template_id && version.procedure_binding?.binding_id && version.procedure_binding.revision)
        .sort((left, right) => right.version - left.version);
      const selectedVersion = candidates.find((version) => version.version === requestedVersion)
        ?? candidates.find((version) => version.version === nextRoutine.current_version)
        ?? candidates[0];
      const binding = selectedVersion?.procedure_binding;
      const bindingId = binding?.binding_id;
      const bindingRevision = binding?.revision;
      if (!selectedVersion?.template_id || !bindingId || !bindingRevision) {
        throw new Error("The selected routine has no server-owned v2 binding metadata.");
      }
      const preparedProcedure: PreparedProcedure = {
        schema_version: 1,
        routineId: nextRoutine.id,
        bindingId,
        revision: bindingRevision,
        version: selectedVersion.version,
        versionId: selectedVersion.id,
        installJobId: binding.install_job_id,
        approvalId: binding.approval_id,
        installApprovalStatus: binding.install_approval_status,
        installApprovalExpiresAt: binding.install_approval_expires_at,
        installRecoveryAction: binding.install_recovery_action,
        packageActivationRecovery: prepared?.routineId === nextRoutine.id && prepared.versionId === selectedVersion.id
          ? prepared.packageActivationRecovery
          : null,
        lifecycleRecovery: prepared?.routineId === nextRoutine.id && prepared.versionId === selectedVersion.id
          ? prepared.lifecycleRecovery
          : null,
      };
      setRoutine(nextRoutine);
      setPrepared(preparedProcedure);
      setExistingVersionNumber(selectedVersion.version);
      setTemplateId(selectedVersion.template_id);
      setName(nextRoutine.name);
      setPreview(null);
      setPackagePreview(null);
      setPackageApproval(null);
      setSchedule(null);
      setInvokeReceipt(null);
      const retentionError = persistPrepared(requestOwnerScope.key, preparedProcedure);
      setLifecycleActivationRecovery(preparedProcedure.lifecycleRecovery ?? null);
      setLifecycleReconcileRequired(Boolean(preparedProcedure.lifecycleRecovery));
      setPackageActivationRecovery(preparedProcedure.packageActivationRecovery ?? null);
      setNotice(retentionError
        ? `${retentionError} Keep this owner session open while using the loaded procedure.`
        : `Loaded the server-owned procedure ${nextRoutine.name}, version ${selectedVersion.version}, binding ${binding.binding_id}. No mutation was sent.`);
    } catch (cause) {
      if (ownerRequestIsCurrent(requestOwnerScope)) setExistingRoutineError(errorMessage(cause));
    } finally {
      if (ownerRequestIsCurrent(requestOwnerScope)) setExistingRoutineLoading(false);
    }
  };

  useEffect(() => {
    if (!active) return;
    void loadTasks();
  }, [active, loadTasks]);

  useEffect(() => {
    if (!active || !ownerPrincipalId || !ownerSessionId) return;
    let cancelled = false;
    const requestOwnerScope = captureOwnerScope();
    const listGeneration = ++routineListGenerationRef.current;
    setExistingRoutines([]);
    setExistingRoutineLoading(true);
    setExistingRoutineError(null);
    void procedureV2Api.listRoutines()
      .then((values) => { if (!cancelled && listGeneration === routineListGenerationRef.current && ownerRequestIsCurrent(requestOwnerScope)) setExistingRoutines(values); })
      .catch((cause) => { if (!cancelled && listGeneration === routineListGenerationRef.current && ownerRequestIsCurrent(requestOwnerScope)) setExistingRoutineError(errorMessage(cause)); })
      .finally(() => { if (!cancelled && listGeneration === routineListGenerationRef.current && ownerRequestIsCurrent(requestOwnerScope)) setExistingRoutineLoading(false); });
    return () => { cancelled = true; };
  }, [active, ownerPrincipalId, ownerSessionId]);

  useEffect(() => {
    const selected = selectedSourceTask && sourceBelongsToOwner(selectedSourceTask, ownerPrincipalId, ownerSessionId)
      ? sourceTaskFromWorkBoard(selectedSourceTask)
      : null;
    if (selected) {
      setTasks((current) => current.some((task) => task.task_id === selected.task_id) ? current : [selected, ...current]);
      setSourceTaskId(selected.task_id);
    }
  }, [ownerPrincipalId, ownerSessionId, selectedSourceTask]);

  useEffect(() => {
    const restored = readPending(pendingKey);
    setPending(restored);
  }, [pendingKey]);

  useEffect(() => {
    if (preparedResetKeyRef.current === preparedKey) return;
    preparedResetKeyRef.current = preparedKey;
    const restoredPrepared = readPrepared(preparedKey, ownerPrincipalId, ownerSessionId);
    setPrepared(restoredPrepared);
    setTasks([]);
    setTaskLoadError(null);
    setSourceTaskId("");
    setSecondSourceTaskId("");
    setSourceWatches([]);
    setSourceWatchError(null);
    setWatchId("");
    setCalendarConnections([]);
    setCalendarConnectionId("");
    setCalendarVerification(null);
    setCalendarId("");
    setCalendarConsent(null);
    setCalendarEvents(null);
    setCalendarEventId("");
    setCalendarLoading(false);
    setCalendarError(null);
    setAllowCalendarModel(false);
    setMeetingPurpose(DEFAULT_MEETING_PURPOSE);
    setGoalId("");
    setTemplateId("public-browser-check");
    setName("");
    setPreview(null);
    setPreviewRequestKey(newProcedureRequestKey("procedure-preview"));
    setPending(readPending(pendingKey));
    setRoutine(null);
    setPackagePreview(null);
    setPackageApproval(null);
    setSchedule(null);
    setInvokeReceipt(null);
    setExistingRoutineId("");
    setExistingVersionNumber(null);
    setExistingRoutineLoading(false);
    setExistingRoutines([]);
    setExistingRoutineError(null);
    setCadenceKind("daily");
    setCadenceTimezone(Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC");
    setCadenceHour("9");
    setCadenceMinute("0");
    setScheduleExpiry("");
    setBusy(false);
    setError(null);
    setNotice(null);
    setLifecycleReconcileRequired(false);
    setLifecycleActivationRecovery(restoredPrepared?.lifecycleRecovery ?? null);
    setPackageActivationRecovery(restoredPrepared?.packageActivationRecovery ?? null);
    setLifecycleReconcileRequired(Boolean(restoredPrepared?.lifecycleRecovery));
  }, [ownerPrincipalId, ownerSessionId, pendingKey, preparedKey]);

  useEffect(() => {
    const pendingRoutineId = pending && "routineId" in pending ? pending.routineId : null;
    const routineId = prepared?.routineId ?? pendingRoutineId;
    if (!routineId || !active || routine) return;
    let cancelled = false;
    const requestOwnerScope = captureOwnerScope();
    setBusy(true);
    void procedureV2Api.getRoutine(routineId)
      .then((nextRoutine) => {
        if (cancelled || !ownerRequestIsCurrent(requestOwnerScope)) return;
        if (!routineBelongsToOwner(nextRoutine, routineId, ownerPrincipalId)) {
          throw new Error("The returned routine is not bound to the current operator.");
        }
        const version = nextRoutine.current_version ?? nextRoutine.versions[0]?.version ?? 1;
        setRoutine(nextRoutine);
        const refreshedPrepared = preparedFromRoutine(nextRoutine, prepared, version);
        if (refreshedPrepared) {
          setPrepared(refreshedPrepared);
          setLifecycleActivationRecovery(refreshedPrepared.lifecycleRecovery ?? null);
          setLifecycleReconcileRequired(Boolean(refreshedPrepared.lifecycleRecovery));
          setPackageActivationRecovery(refreshedPrepared.packageActivationRecovery ?? null);
          persistPrepared(requestOwnerScope.key, refreshedPrepared);
        } else {
          setPrepared((current) => current ?? {
            schema_version: 1,
            routineId,
            bindingId: nextRoutine.binding_id ?? "",
            revision: nextRoutine.revision,
            version,
            installJobId: "",
            approvalId: "",
            installApprovalStatus: null,
            installApprovalExpiresAt: null,
            installRecoveryAction: null,
          });
        }
      })
      .catch((cause) => {
        if (!cancelled && ownerRequestIsCurrent(requestOwnerScope)) setError(`The retained procedure could not restore its routine. ${errorMessage(cause)}`);
      })
      .finally(() => {
        if (!cancelled && ownerRequestIsCurrent(requestOwnerScope)) setBusy(false);
      });
    return () => { cancelled = true; };
  }, [active, ownerPrincipalId, ownerSessionId, pending, prepared, routine]);

  useEffect(() => {
    if (!goalId && selectedGoal) setGoalId(selectedGoal.id);
  }, [goalId, selectedGoal]);

  useEffect(() => {
    if (calendarConsent && selectedGoal?.id !== calendarConsent.goalId) {
      setCalendarConsent(null);
      setCalendarEvents(null);
      setCalendarEventId("");
    }
  }, [calendarConsent, selectedGoal?.id]);

  useEffect(() => {
    if (templateId !== "watch-and-public-browser") return;
    if (selectedWatch && !eligibleWatches.some((watch) => watch.id === selectedWatch.id)) {
      setWatchId(eligibleWatches[0]?.id ?? "");
    } else if (!selectedWatch && eligibleWatches.length > 0) {
      setWatchId(eligibleWatches[0].id);
    }
  }, [eligibleWatches, selectedWatch, templateId]);

  useEffect(() => {
    if (!active || templateId !== "watch-and-public-browser" || !ownerPrincipalId || !ownerSessionId) return;
    let cancelled = false;
    const requestOwnerScope = captureOwnerScope();
    setSourceWatchError(null);
    void procedureV2Api.listSourceWatches()
      .then((values) => {
        if (!cancelled && ownerRequestIsCurrent(requestOwnerScope)) {
          setSourceWatches(values.filter((watch) => sourceBelongsToOwner(watch, ownerPrincipalId, ownerSessionId)));
        }
      })
      .catch((cause) => { if (!cancelled && ownerRequestIsCurrent(requestOwnerScope)) setSourceWatchError(errorMessage(cause)); });
    return () => { cancelled = true; };
  }, [active, ownerPrincipalId, ownerSessionId, templateId]);

  useEffect(() => {
    if (!active || templateId !== "selected-meeting-prep" || !ownerPrincipalId || !ownerSessionId) return;
    let cancelled = false;
    const requestOwnerScope = captureOwnerScope();
    setCalendarError(null);
    setCalendarLoading(true);
    void listCalendarConnections()
      .then((values) => {
        if (cancelled || !ownerRequestIsCurrent(requestOwnerScope)) return;
        setCalendarConnections(values);
        setCalendarConnectionId((current) => current || values.find((value) => value.state === "active")?.connection_id || "");
      })
      .catch((cause) => { if (!cancelled && ownerRequestIsCurrent(requestOwnerScope)) setCalendarError(errorMessage(cause)); })
      .finally(() => { if (!cancelled && ownerRequestIsCurrent(requestOwnerScope)) setCalendarLoading(false); });
    return () => { cancelled = true; };
  }, [active, ownerPrincipalId, ownerSessionId, templateId]);

  useEffect(() => {
    if (templateId !== "watch-and-public-browser") setSecondSourceTaskId("");
    if (templateId === "watch-and-public-browser" && sourceTaskId && secondSourceTaskId === sourceTaskId) setSecondSourceTaskId("");
    setPreview(null);
    setPreviewRequestKey(newProcedureRequestKey("procedure-preview"));
    setError(null);
  }, [templateId, sourceTaskId, secondSourceTaskId]);

  const resetPrepared = () => {
    if (!pending) clearPrepared(preparedKey);
    setPrepared(null);
    setRoutine(null);
    setPackagePreview(null);
    setPackageApproval(null);
    setSchedule(null);
    setInvokeReceipt(null as never);
    setLifecycleReconcileRequired(false);
    setLifecycleActivationRecovery(null);
    setPackageActivationRecovery(null);
  };

  const handlePendingFailure = (kind: PendingProcedureAction["kind"], cause: unknown, requestOwnerScope: OwnerRequestScope, pendingStorageKey: string | null) => {
    if (!ownerRequestIsCurrent(requestOwnerScope)) return;
    if (!isDefinitivePreEffectFailure(cause)) {
      setError(errorMessage(cause));
      return;
    }
    clearPending(pendingStorageKey);
    setPending(null);
    if (kind === "prepare") {
      setPreview(null);
      setPreviewRequestKey(newProcedureRequestKey("procedure-preview"));
    }
    setError(`${errorMessage(cause)} No governed effect was committed. Start a fresh preview or rebind before retrying.`);
  };

  const selectedCalendarConnection = calendarConnections.find((connection) => connection.connection_id === calendarConnectionId) ?? null;

  const chooseCalendarConnection = (connectionId: string) => {
    setCalendarConnectionId(connectionId);
    setCalendarVerification(null);
    setCalendarId("");
    setCalendarConsent(null);
    setCalendarEvents(null);
    setCalendarEventId("");
    setCalendarError(null);
  };

  const verifyCalendar = async () => {
    if (!selectedCalendarConnection || selectedCalendarConnection.state !== "active") {
      setCalendarError("Choose an active calendar connection first.");
      return;
    }
    const requestOwnerScope = captureOwnerScope();
    setCalendarLoading(true);
    setCalendarError(null);
    try {
      const result = await verifyCalendarConnection(selectedCalendarConnection.connection_id, {
        expected_revision: selectedCalendarConnection.revision,
        idempotency_key: newProcedureRequestKey("procedure-calendar-verify"),
      });
      if (!ownerRequestIsCurrent(requestOwnerScope)) return;
      setCalendarVerification(result);
      setCalendarId(result.calendars[0]?.calendar_id ?? "");
      setCalendarConsent(null);
      setCalendarEvents(null);
      setCalendarEventId("");
    } catch (cause) {
      if (ownerRequestIsCurrent(requestOwnerScope)) setCalendarError(errorMessage(cause));
    } finally {
      if (ownerRequestIsCurrent(requestOwnerScope)) setCalendarLoading(false);
    }
  };

  const createCalendarConsentAndLoadEvents = async () => {
    const goalRevision = revisionForGoal(selectedGoal);
    if (!selectedCalendarConnection || !calendarVerification || !calendarId || !selectedGoal || !goalRevision) {
      setCalendarError("Verify the connection, choose a returned calendar, and choose an active goal first.");
      return;
    }
    if (!allowCalendarModel) {
      setCalendarError("Explicitly allow the governed model in this calendar read consent before loading events.");
      return;
    }
    const requestOwnerScope = captureOwnerScope();
    setCalendarLoading(true);
    setCalendarError(null);
    try {
      const consent = await createReadConsent({
        schema_version: 1,
        connection_id: selectedCalendarConnection.connection_id,
        calendar_id: calendarId,
        goal_id: selectedGoal.id,
        goal_revision: goalRevision,
        allowed_fields: ["summary", "start", "end"],
        window_minutes: 1440,
        max_events: 20,
        allow_remote_model: true,
        expires_at: new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString(),
        idempotency_key: newProcedureRequestKey("procedure-calendar-consent"),
      });
      const eventResponse = await listCalendarEvents(selectedCalendarConnection.connection_id, consent.consent_id);
      if (!ownerRequestIsCurrent(requestOwnerScope)) return;
      if (eventResponse.consent_id !== consent.consent_id || eventResponse.consent_revision !== consent.revision || eventResponse.connection_revision !== consent.connection_revision) {
        throw new Error("The returned event list does not match the current calendar consent. Refresh the consent before selecting an event.");
      }
      setCalendarConsent({ consentId: consent.consent_id, revision: consent.revision, connectionRevision: consent.connection_revision, goalId: selectedGoal.id });
      setCalendarEvents(eventResponse);
      setCalendarEventId("");
    } catch (cause) {
      if (ownerRequestIsCurrent(requestOwnerScope)) setCalendarError(errorMessage(cause));
    } finally {
      if (ownerRequestIsCurrent(requestOwnerScope)) setCalendarLoading(false);
    }
  };

  const sourceRefsForRequest = (): Array<{ task_id: string; expected_revision: number }> | null => {
    if (!selectedSource || !sourceBelongsToOwner(selectedSource, ownerPrincipalId, ownerSessionId) || !sourceEligibility(selectedSource, templateId)) {
      setError("Choose a Done task with a verified readback for this fixed template.");
      return null;
    }
    if (templateId !== "watch-and-public-browser") return [{ task_id: selectedSource.task_id, expected_revision: selectedSource.task_revision }];
    if (!selectedSecondSource || !sourceBelongsToOwner(selectedSecondSource, ownerPrincipalId, ownerSessionId)
      || selectedSecondSource.task_id === selectedSource.task_id || !sourceEligibility(selectedSecondSource, templateId)) {
      setError("Choose two distinct verified tasks: the research watch first and the public browser result second.");
      return null;
    }
    const ordered = selectedSource.capability_id === "guardian.research-watch.v1"
      ? [selectedSource, selectedSecondSource]
      : selectedSecondSource.capability_id === "guardian.research-watch.v1"
        ? [selectedSecondSource, selectedSource]
        : null;
    if (!ordered) {
      setError("The watch-and-browser template requires one research watch and one public browser result.");
      return null;
    }
    return ordered.map((task) => ({ task_id: task.task_id, expected_revision: task.task_revision }));
  };

  const previewProcedure = async () => {
    if (unresolvedRecovery) {
      setError("Refresh authority to reconcile the retained procedure outcome before preparing another procedure.");
      return;
    }
    if (pending) {
      setError("An exact governed request is awaiting reconciliation. Retry it before preparing another procedure.");
      return;
    }
    const source_tasks = sourceRefsForRequest();
    const trimmedName = name.trim();
    if (!source_tasks || !trimmedName || trimmedName.length > 80) {
      if (!trimmedName) setError("Give the reviewed procedure a name.");
      else if (trimmedName.length > 80) setError("Procedure names are limited to 80 characters.");
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    const requestOwnerScope = captureOwnerScope();
    resetPrepared();
    try {
      const next = await procedureV2Api.previewFromTasks({ template_id: templateId, source_tasks, name: trimmedName, idempotency_key: previewRequestKey });
      if (!ownerRequestIsCurrent(requestOwnerScope)) return;
      setPreview(next);
      setNotice(`Preview expires ${formatTime(next.expires_at)}. Nothing was installed or executed.`);
    } catch (cause) {
      if (ownerRequestIsCurrent(requestOwnerScope)) setError(errorMessage(cause));
    } finally {
      if (ownerRequestIsCurrent(requestOwnerScope)) setBusy(false);
    }
  };

  const acceptPreparedResponse = async (next: ProcedureV2Prepared, requestOwnerScope: OwnerRequestScope, pendingStorageKey: string | null) => {
    if (!ownerRequestIsCurrent(requestOwnerScope)) return;
    if (next.status === "blocked") {
      clearPending(pendingStorageKey);
      setPending(null);
      setError(`Preparation was blocked by the server.${next.recovery_action ? ` Recovery: ${next.recovery_action}.` : ""}`);
      return;
    }
    if (!next.routine_id || !next.install_job_id || !next.approval_id || !next.version || !next.version_id
      || typeof next.install_approval_status !== "string" || !next.install_approval_status
      || typeof next.install_approval_expires_at !== "string" || !next.install_approval_expires_at) {
      throw new Error("The server did not return the exact install job approval for this prepared procedure.");
    }
    const preparedProcedure: PreparedProcedure = {
      schema_version: 1,
      routineId: next.routine_id,
      bindingId: next.binding_id,
      revision: next.revision,
      version: next.version,
      versionId: next.version_id,
      installJobId: next.install_job_id,
      approvalId: next.approval_id,
      installApprovalStatus: next.install_approval_status,
      installApprovalExpiresAt: next.install_approval_expires_at,
      installRecoveryAction: next.install_recovery_action,
      packageActivationRecovery: null,
      lifecycleRecovery: null,
    };
    const retentionError = persistPrepared(requestOwnerScope.key, preparedProcedure);
    if (retentionError) throw new Error(retentionError);
    const nextRoutine = await procedureV2Api.getRoutine(next.routine_id);
    if (!ownerRequestIsCurrent(requestOwnerScope)) return;
    if (!routineBelongsToOwner(nextRoutine, next.routine_id, ownerPrincipalId)) {
      throw new Error("The prepared routine readback is not bound to the current operator.");
    }
    const readbackVersion = nextRoutine.versions.find((candidate) => candidate.version === preparedProcedure.version);
    if (nextRoutine.id !== preparedProcedure.routineId
      || !readbackVersion
      || readbackVersion.routine_id !== preparedProcedure.routineId
      || readbackVersion.id !== preparedProcedure.versionId
      || readbackVersion.version !== preparedProcedure.version) {
      throw new Error("The prepared routine readback did not contain the exact server-owned version. The request remains retained for reconciliation.");
    }
    setPrepared(preparedProcedure);
    setRoutine(nextRoutine);
    // An older list snapshot must not erase this exact mutation readback.
    routineListGenerationRef.current += 1;
    setExistingRoutineLoading(false);
    setExistingRoutineError(null);
    setExistingRoutines((current) => [nextRoutine, ...current.filter((candidate) => candidate.id !== nextRoutine.id)]);
    // Keep the exact request until both the receipt and its server-owned
    // routine binding are durable and readable. A lost GET therefore leaves a
    // safe same-key reconciliation path instead of silently losing recovery.
    clearPending(pendingStorageKey);
    setPending(null);
    setNotice("Prepared. Package review, approval, installation, and activation are still required.");
  };

  const prepareProcedure = async () => {
    if (!preview) return;
    if (unresolvedRecovery) {
      setError("Refresh authority to reconcile the retained procedure outcome before preparing another procedure.");
      return;
    }
    const source_tasks = sourceRefsForRequest();
    if (!source_tasks) return;
    setBusy(true);
    setError(null);
    const request: ProcedureV2PrepareRequest = {
        template_id: templateId,
        source_tasks,
        name: name.trim(),
        idempotency_key: previewRequestKey,
        preview_digest: preview.preview_digest,
      };
    const pendingAction: PendingPrepareProcedureAction = { schema_version: 1, kind: "prepare", request };
    const pendingError = persistPending(pendingKey, pendingAction);
    if (pendingError) { setError(pendingError); setBusy(false); return; }
    setPending(pendingAction);
    const requestOwnerScope = captureOwnerScope();
    try {
      const next = await procedureV2Api.prepareFromTasks(request);
      await acceptPreparedResponse(next, requestOwnerScope, pendingKey);
    } catch (cause) {
      handlePendingFailure("prepare", cause, requestOwnerScope, pendingKey);
    } finally {
      if (ownerRequestIsCurrent(requestOwnerScope)) setBusy(false);
    }
  };

  const refreshRoutine = async () => {
    if (!prepared?.routineId) return;
    const requestOwnerScope = captureOwnerScope();
    const expectedPrepared = prepared;
    const expectedPackageRecovery = packageActivationRecovery;
    const expectedLifecycleRecovery = lifecycleActivationRecovery;
    try {
      const nextRoutine = await procedureV2Api.getRoutine(expectedPrepared.routineId);
      if (!ownerRequestIsCurrent(requestOwnerScope)) return;
      if (!routineBelongsToOwner(nextRoutine, expectedPrepared.routineId, ownerPrincipalId)) {
        throw new Error("The returned routine is not bound to the current operator.");
      }
      const refreshedPrepared = preparedFromRoutine(nextRoutine, expectedPrepared, expectedPrepared.version);
      if (!refreshedPrepared || (expectedPrepared.versionId && refreshedPrepared.versionId !== expectedPrepared.versionId)) {
        throw new Error("Refresh authority did not return the exact prepared routine version. No lifecycle outcome was accepted.");
      }
      if (expectedPackageRecovery && !packageActivationReadbackIsCurrent(nextRoutine, expectedPackageRecovery, ownerPrincipalId, ownerSessionId)) {
        setRoutine(nextRoutine);
        setPrepared(refreshedPrepared);
        const retentionError = persistPrepared(requestOwnerScope.key, refreshedPrepared);
        if (retentionError) setNotice(`${retentionError} Keep this owner session open while using the refreshed approval.`);
        setError("Refresh authority did not prove the reviewed package activation for the exact approval, digest, version, and expected revision. No repeat activation was sent.");
        return;
      }
      if (expectedLifecycleRecovery && !lifecycleMutationReadbackIsCurrent(nextRoutine, expectedLifecycleRecovery, ownerPrincipalId, ownerSessionId)) {
        setRoutine(nextRoutine);
        setPrepared(refreshedPrepared);
        const retentionError = persistPrepared(requestOwnerScope.key, refreshedPrepared);
        if (retentionError) setNotice(`${retentionError} Keep this owner session open while using the refreshed approval.`);
        setError(`Refresh authority did not prove the requested procedure ${expectedLifecycleRecovery.action} at a newer server revision. No repeat lifecycle mutation was sent.`);
        return;
      }
      setRoutine(nextRoutine);
      const reconciledPrepared = {
        ...refreshedPrepared,
        packageActivationRecovery: expectedPackageRecovery ? null : refreshedPrepared.packageActivationRecovery,
        lifecycleRecovery: expectedLifecycleRecovery ? null : refreshedPrepared.lifecycleRecovery,
      };
      setPrepared(reconciledPrepared);
      const retentionError = persistPrepared(requestOwnerScope.key, reconciledPrepared);
      if (retentionError) setNotice(`${retentionError} Keep this owner session open while using the refreshed approval.`);
      if (expectedPackageRecovery) {
        setPackagePreview((current) => current && current.digest === expectedPackageRecovery.digest
          ? { ...current, status: "active", installed_package_digest: expectedPackageRecovery.digest }
          : current);
        setPackageApproval((current) => current?.approval_id === expectedPackageRecovery.approvalId
          ? { ...current, status: "consumed" }
          : current);
        setPackageActivationRecovery(null);
        setNotice("The reviewed package activation is confirmed by the current owner-scoped routine readback.");
      }
      if (expectedLifecycleRecovery) {
        setLifecycleReconcileRequired(false);
        setLifecycleActivationRecovery(null);
      } else if (!expectedPackageRecovery) {
        setLifecycleReconcileRequired(false);
      }
    } catch (cause) {
      if (ownerRequestIsCurrent(requestOwnerScope)) setError(errorMessage(cause));
    }
  };

  const packageContext = () => {
    if (!prepared || !routine) {
      setError("Prepare a fixed procedure before reviewing its package.");
      return null;
    }
    const selectedVersion = routine.versions.find((candidate) => candidate.version === currentVersion);
    if (!prepared.versionId || !selectedVersion || selectedVersion.id !== prepared.versionId || selectedVersion.version !== prepared.version) {
      setError("The selected procedure version identity is unavailable or stale. Refresh authority before sending a package mutation.");
      return null;
    }
    const reviewedPackagePreview = packagePreview
      && packagePreview.routine_id === prepared.routineId
      && packagePreview.version === selectedVersion.version
      ? packagePreview
      : null;
    return {
      routineId: prepared.routineId,
      versionId: selectedVersion.id,
      version: selectedVersion.version,
      revision: routine.revision,
      packageDigest: selectedVersion.installed_package_digest ?? reviewedPackagePreview?.digest ?? null,
    };
  };

  const previewPackage = async () => {
    if (packageActivationRecovery || lifecycleReconcileRequired) {
      setError(packageActivationRecovery
        ? "The previous package activation outcome is unverified. Refresh authority before sending another package request."
        : "The previous procedure lifecycle outcome is unverified. Refresh authority before sending another package request.");
      return;
    }
    const context = packageContext();
    if (!context) return;
    setBusy(true);
    setError(null);
    const requestOwnerScope = captureOwnerScope();
    try {
      const nextPackage = await procedureV2Api.packagePreview(context.routineId, context.version, context.revision);
      if (!ownerRequestIsCurrent(requestOwnerScope)) return;
      setPackagePreview(nextPackage);
      setPackageApproval(null);
    } catch (cause) { if (ownerRequestIsCurrent(requestOwnerScope)) setError(errorMessage(cause)); }
    finally { if (ownerRequestIsCurrent(requestOwnerScope)) setBusy(false); }
  };

  const reviewPackage = async () => {
    if (packageActivationRecovery || lifecycleReconcileRequired) {
      setError(packageActivationRecovery
        ? "The previous package activation outcome is unverified. Refresh authority before sending another package request."
        : "The previous procedure lifecycle outcome is unverified. Refresh authority before sending another package request.");
      return;
    }
    const context = packageContext();
    if (!context || !packagePreview || packagePreview.digest !== packagePreview.installed_package_digest) {
      setError("The package preview must match the installed digest before review.");
      return;
    }
    setBusy(true);
    setError(null);
    const requestOwnerScope = captureOwnerScope();
    try {
      const result = await procedureV2Api.packageReview(context.routineId, context.version, context.revision);
      if (!ownerRequestIsCurrent(requestOwnerScope)) return;
      if (result.digest !== packagePreview.digest || result.review.status !== "approved") throw new Error("The package review receipt did not match the preview digest.");
      setPackagePreview({ ...packagePreview, review_id: result.review.review_id, status: "reviewed" });
      setNotice("Package review recorded. Prepare the separate activation approval.");
    } catch (cause) { if (ownerRequestIsCurrent(requestOwnerScope)) setError(errorMessage(cause)); }
    finally { if (ownerRequestIsCurrent(requestOwnerScope)) setBusy(false); }
  };

  const prepareApproval = async () => {
    if (packageActivationRecovery || lifecycleReconcileRequired) {
      setError(packageActivationRecovery
        ? "The previous package activation outcome is unverified. Refresh authority before sending another package request."
        : "The previous procedure lifecycle outcome is unverified. Refresh authority before sending another package request.");
      return;
    }
    const context = packageContext();
    if (!context || !packagePreview?.review_id) return;
    setBusy(true);
    setError(null);
    const requestOwnerScope = captureOwnerScope();
    try {
      const result = await procedureV2Api.preparePackageApproval(context.routineId, context.version, context.revision);
      if (!ownerRequestIsCurrent(requestOwnerScope)) return;
      if (result.digest !== packagePreview.digest || result.approval.status !== "pending") throw new Error("The activation approval is not bound to the reviewed package.");
      setPackageApproval(result.approval);
    } catch (cause) { if (ownerRequestIsCurrent(requestOwnerScope)) setError(errorMessage(cause)); }
    finally { if (ownerRequestIsCurrent(requestOwnerScope)) setBusy(false); }
  };

  const decideApproval = async (decision: "approved" | "denied") => {
    if (packageActivationRecovery || lifecycleReconcileRequired) {
      setError(packageActivationRecovery
        ? "The previous package activation outcome is unverified. Refresh authority before sending another package request."
        : "The previous procedure lifecycle outcome is unverified. Refresh authority before sending another package request.");
      return;
    }
    const context = packageContext();
    if (!context || !packageApproval) return;
    setBusy(true);
    setError(null);
    const requestOwnerScope = captureOwnerScope();
    try {
      const result = await procedureV2Api.decidePackageApproval(context.routineId, context.version, packageApproval.approval_id, context.revision, decision);
      if (!ownerRequestIsCurrent(requestOwnerScope)) return;
      if (result.approval.approval_id !== packageApproval.approval_id || result.approval.status !== decision) throw new Error("The activation decision receipt did not match the pending approval.");
      setPackageApproval(result.approval);
    } catch (cause) { if (ownerRequestIsCurrent(requestOwnerScope)) setError(errorMessage(cause)); }
    finally { if (ownerRequestIsCurrent(requestOwnerScope)) setBusy(false); }
  };

  const activatePackage = async () => {
    if (packageActivationRecovery || lifecycleReconcileRequired) {
      setError(packageActivationRecovery
        ? "The previous package activation outcome is unverified. Refresh authority before sending another package request."
        : "The previous procedure lifecycle outcome is unverified. Refresh authority before sending another package request.");
      return;
    }
    const context = packageContext();
    if (!context || !packagePreview?.review_id || packageApproval?.status !== "approved") return;
    if (packagePreview.routine_id !== context.routineId || packagePreview.version !== context.version
      || !packagePreview.digest
      || packageApproval.digest !== packagePreview.digest
      || packageApproval.pack_id !== packagePreview.pack_id
      || packageApproval.version !== String(context.version)) {
      setError("Package activation is blocked until the reviewed approval, exact version, and package digest match the current routine readback.");
      return;
    }
    const recoveryContext: PackageActivationRecovery = {
      schema_version: 1,
      routineId: context.routineId,
      versionId: context.versionId,
      version: context.version,
      digest: packagePreview.digest,
      expectedRevision: context.revision,
      approvalId: packageApproval.approval_id,
    };
    setBusy(true);
    setError(null);
    const requestOwnerScope = captureOwnerScope();
    const retentionError = retainPackageActivationRecovery(recoveryContext);
    if (retentionError) {
      setError(`${retentionError} No package activation request was sent.`);
      if (ownerRequestIsCurrent(requestOwnerScope)) setBusy(false);
      return;
    }
    try {
      const result = await procedureV2Api.activatePackage(context.routineId, context.version, context.revision, packageApproval.approval_id);
      if (!ownerRequestIsCurrent(requestOwnerScope)) return;
      if (result.digest !== recoveryContext.digest || result.status !== "active") throw new Error("Package activation returned an unverified receipt. Refresh authority before retrying.");
      const nextRoutine = await procedureV2Api.getRoutine(context.routineId);
      if (!ownerRequestIsCurrent(requestOwnerScope)) return;
      if (!packageActivationReadbackIsCurrent(nextRoutine, recoveryContext, ownerPrincipalId, ownerSessionId)) {
        throw new Error("Package activation was accepted, but the current routine readback did not prove the exact package at a newer revision.");
      }
      const refreshedPrepared = preparedFromRoutine(nextRoutine, prepared, context.version);
      if (refreshedPrepared && refreshedPrepared.versionId !== recoveryContext.versionId) {
        throw new Error("Package activation readback returned a different prepared version. Refresh authority before retrying.");
      }
      setRoutine(nextRoutine);
      if (refreshedPrepared) {
        const reconciledPrepared = { ...refreshedPrepared, packageActivationRecovery: null };
        setPrepared(reconciledPrepared);
        const retentionError = persistPrepared(requestOwnerScope.key, reconciledPrepared);
        if (retentionError) setNotice(`${retentionError} Keep this owner session open while using the refreshed approval.`);
      }
      setPackagePreview({ ...packagePreview, status: "active", installed_package_digest: recoveryContext.digest });
      setPackageApproval({ ...packageApproval, status: "consumed" });
      setPackageActivationRecovery(null);
      setNotice("The reviewed package is active. Activate the procedure explicitly before invoking it.");
    } catch (cause) {
      if (ownerRequestIsCurrent(requestOwnerScope)) {
        retainPackageActivationRecovery(recoveryContext);
        setError("The package activation outcome could not be verified. Refresh authority before sending another activation request.");
      }
    }
    finally { if (ownerRequestIsCurrent(requestOwnerScope)) setBusy(false); }
  };

  const runLifecycle = async (action: LifecycleAction) => {
    if (packageActivationRecovery) {
      setError("The package activation outcome is unverified. Refresh authority before sending another lifecycle mutation.");
      return;
    }
    const context = packageContext();
    if (!context) return;
    if (!ownerPrincipalId || !ownerSessionId) {
      setError("The authenticated owner session is unavailable. No lifecycle mutation was sent.");
      return;
    }
    if (lifecycleReconcileRequired) {
      setError("The last procedure lifecycle mutation was not confirmed. Refresh authority and read back the current procedure before sending another lifecycle request.");
      return;
    }
    if (action === "install" && !isCurrentInstallApproval(prepared)) {
      const recovery = prepared?.installRecoveryAction ? ` Recovery: ${prepared.installRecoveryAction}.` : " Refresh the procedure to obtain a current approval.";
      setError(`Installation is blocked until the server confirms a current approved receipt.${recovery}`);
      return;
    }
    if (action === "install" && !context.packageDigest) {
      setError("Preview the reviewed package before installing so the exact package digest can be retained for recovery.");
      return;
    }
    if (action === "revoke" && !window.confirm("Revoke this procedure permanently?")) return;
    if (action === "rollback" && (!existingRoutineId || !existingVersionNumber || existingVersionNumber === routineCurrentVersion)) {
      setError("Choose an installed earlier version before requesting rollback.");
      return;
    }
    if (action === "rollback" && !window.confirm(`Rollback future invocations to version ${currentVersion}?`)) return;
    if (action === "activate" && (!routine || (routine.state !== "installed" && routine.state !== "paused") || !routinePackageMatchesVersion(routine, context.version, context.versionId))) {
      setError("Activation is blocked until the requested version is installed, active, and matches the current server package digest. Refresh authority before retrying.");
      return;
    }
    const rollbackState: LifecycleRecovery["expectedState"] | null = action === "rollback"
      ? routine?.state === "installed" || routine?.state === "active" || routine?.state === "paused" ? routine.state : null
      : null;
    if (action === "rollback" && !rollbackState) {
      setError("Rollback is blocked until the current routine state is read back as installed, active, or paused.");
      return;
    }
    const lifecycleRecovery: LifecycleRecovery = {
      schema_version: 1,
      action,
      ownerPrincipalId,
      ownerSessionId,
      routineId: context.routineId,
      versionId: context.versionId,
      version: context.version,
      requestRevision: context.revision,
      expectedState: action === "install" ? "installed" : action === "activate" ? "active" : action === "pause" ? "paused" : action === "revoke" ? "revoked" : rollbackState!,
      approvalId: action === "install" ? prepared?.approvalId ?? null : null,
      installJobId: action === "install" ? prepared?.installJobId ?? null : null,
      targetVersionId: action === "rollback" ? context.versionId : null,
      targetVersion: action === "rollback" ? context.version : null,
      packageDigest: context.packageDigest,
    };
    setBusy(true);
    setError(null);
    const requestOwnerScope = captureOwnerScope();
    const retentionError = retainLifecycleRecovery(lifecycleRecovery);
    if (retentionError) {
      setError(`${retentionError} No lifecycle mutation was sent.`);
      if (ownerRequestIsCurrent(requestOwnerScope)) setBusy(false);
      return;
    }
    try {
      const approvalId = prepared?.approvalId;
      const body = action === "install"
        ? { version: context.version, expected_routine_revision: context.revision, approval_id: approvalId! }
        : action === "activate"
          ? { version: context.version, expected_routine_revision: context.revision }
        : action === "rollback"
          ? { target_version: context.version, expected_routine_revision: context.revision, reason: `Operator requested rollback to version ${context.version}.` }
          : { expected_routine_revision: context.revision, reason: `Operator requested ${action}.` };
      const nextRoutine = await procedureV2Api.lifecycle(context.routineId, action, body);
      if (!ownerRequestIsCurrent(requestOwnerScope)) return;
      if (!lifecycleReadbackBelongsToOwner(nextRoutine, context.routineId, ownerPrincipalId, ownerSessionId)) {
        throw new Error("The lifecycle response is not bound to the current operator.");
      }
      if (!lifecycleMutationReadbackIsCurrent(nextRoutine, lifecycleRecovery, ownerPrincipalId, ownerSessionId)) {
        throw new Error(`The ${action} response did not prove the requested procedure state at a newer server revision.`);
      }
      const refreshedPrepared = preparedFromRoutine(nextRoutine, prepared, context.version);
      if (!refreshedPrepared || refreshedPrepared.versionId !== context.versionId) {
        throw new Error(`The ${action} response did not include the exact server-owned procedure version.`);
      }
      const reconciledPrepared = { ...refreshedPrepared, lifecycleRecovery: null };
      const clearError = persistPrepared(requestOwnerScope.key, reconciledPrepared);
      setRoutine(nextRoutine);
      if (clearError) {
        setPrepared({ ...reconciledPrepared, lifecycleRecovery });
        setLifecycleReconcileRequired(true);
        setLifecycleActivationRecovery(lifecycleRecovery);
        setError(`${clearError} The ${action} readback was verified, but local recovery could not be cleared. Refresh authority before another mutation.`);
        return;
      }
      setPrepared(reconciledPrepared);
      setLifecycleReconcileRequired(false);
      setLifecycleActivationRecovery(null);
      setNotice(`Procedure ${action} completed with a current server revision.`);
    } catch (cause) {
      if (ownerRequestIsCurrent(requestOwnerScope)) {
        if (isDefinitiveLifecyclePreEffectFailure(cause)) {
          retainLifecycleRecovery(null);
          setError(`${errorMessage(cause)} No governed lifecycle effect was committed. Refresh authority before retrying.`);
        } else {
          retainLifecycleRecovery(lifecycleRecovery);
          const label = action === "activate" ? "activation" : action;
          setError(`The procedure ${label} outcome could not be verified. Refresh authority before sending another lifecycle mutation.`);
        }
      }
    }
    finally { if (ownerRequestIsCurrent(requestOwnerScope)) setBusy(false); }
  };

  const buildParameters = (): ProcedureV2InvokeParameters | null => {
    const goalRevision = revisionForGoal(selectedGoal);
    if (!selectedGoal || !goalRevision) {
      setError("Choose an active goal with a current server revision.");
      return null;
    }
    if (templateId === "watch-and-public-browser") {
      if (!selectedWatch || !sourceBelongsToOwner(selectedWatch, ownerPrincipalId, ownerSessionId)
        || selectedWatch.goal_id !== selectedGoal.id || selectedWatch.state !== "active") {
        setError("Choose an active owner-visible source watch for the selected goal.");
        return null;
      }
      return { goal_id: selectedGoal.id, expected_goal_revision: goalRevision, source_watch_id: selectedWatch.id, expected_watch_revision: selectedWatch.plan_revision };
    }
    if (templateId === "selected-meeting-prep") {
      const purpose = meetingPurpose.trim();
      if (!calendarConsent || !calendarEvents || !selectedCalendarEvent || !purpose || purpose.length > 500) {
        setError("Choose a current typed calendar consent and event, then enter a purpose of at most 500 characters.");
        return null;
      }
      return {
        goal_id: selectedGoal.id,
        schema_version: 1,
        consent_id: calendarConsent.consentId,
        event_binding_id: selectedCalendarEvent.event_binding_id,
        expected_event_binding_revision: selectedCalendarEvent.event_binding_revision,
        expected_consent_revision: calendarConsent.revision,
        expected_connection_revision: calendarConsent.connectionRevision,
        event_revision: selectedCalendarEvent.event_revision,
        calendar_list_revision: selectedCalendarEvent.calendar_list_revision,
        goal_revision: goalRevision,
        purpose,
      };
    }
    return { goal_id: selectedGoal.id, expected_goal_revision: goalRevision };
  };

  const sendInvocationRequest = async (
    routineId: string,
    request: ProcedureV2InvokeRequest,
    versionId: string | null | undefined,
    requestOwnerScope: OwnerRequestScope,
    requestStorageKey: string | null,
  ) => {
    setBusy(true);
    setError(null);
    try {
      const receipt = correlateInvocationReceipt(
        await procedureV2Api.invoke(routineId, request),
        request,
        routineId,
        versionId,
      );
      if (!ownerRequestIsCurrent(requestOwnerScope)) return;
      setInvokeReceipt(receipt);
      if (isConfirmedInvocationStatus(receipt.status)) {
        clearPending(requestStorageKey);
        setPending(null);
        setNotice(`Invocation accepted as task ${receipt.task_id}. Read back the leaf job and artifact on the Work Board.`);
      } else {
        // A blocked, degraded, or unknown receipt is not proof that the
        // intended operator outcome was accepted. Keep the exact key/body for
        // reconciliation and make the recovery state explicit.
        setError(`The invocation outcome is ${receipt.status}, not confirmed; the exact request remains retained for reconciliation. Do not create a new request.`);
      }
    } catch (cause) {
      handlePendingFailure("invoke", cause, requestOwnerScope, requestStorageKey);
    } finally {
      if (ownerRequestIsCurrent(requestOwnerScope)) setBusy(false);
    }
  };

  const invoke = async () => {
    if (!prepared || !routine || !routineActive) {
      setError("Invocation is blocked until the procedure package is installed, reviewed, approved, and active.");
      return;
    }
    const request = (() => {
      const parameters = buildParameters();
      if (!parameters) return null;
      return {
        version: currentVersion,
        expected_routine_revision: routine.revision,
        goal_id: parameters.goal_id,
        expected_goal_revision: revisionForGoal(selectedGoal)!,
        parameters,
        invocation_uuid: newProcedureRequestKey("procedure-invoke"),
      } satisfies ProcedureV2InvokeRequest;
    })();
    if (!request) return;
    const versionId = routine.versions.find((candidate) => candidate.version === request.version)?.id
      ?? (prepared.routineId === routine.id && prepared.version === request.version ? prepared.versionId : null);
    if (!versionId) {
      setError("Invocation is blocked until the exact server-owned routine version can be read back.");
      return;
    }
    const pendingAction: PendingProcedureAction = {
      schema_version: 1,
      kind: "invoke",
      routineId: prepared.routineId,
      versionId,
      request,
    };
    const requestOwnerScope = captureOwnerScope();
    const pendingError = persistPending(pendingKey, pendingAction);
    if (pendingError) { setError(pendingError); return; }
    setPending(pendingAction);
    await sendInvocationRequest(prepared.routineId, request, versionId, requestOwnerScope, pendingKey);
  };

  const scheduleProcedure = async () => {
    if (!prepared || !routine || !routineActive || !isPublicSchedulableProcedure(templateId)) {
      setError("Only an active reviewed public browser procedure can receive a finite schedule.");
      return;
    }
    const parameters = buildParameters();
    if (!parameters) return;
    const expiry = localDateTimeToIso(scheduleExpiry);
    if (!scheduleExpiry || !Number.isFinite(new Date(expiry).getTime())) {
      setError("Choose a finite schedule expiry.");
      return;
    }
    const hour = Number(cadenceHour);
    const minute = Number(cadenceMinute);
    const request: ProcedureV2ScheduleRequest = {
      version: currentVersion,
      expected_routine_revision: routine.revision,
      goal_id: parameters.goal_id,
      expected_goal_revision: revisionForGoal(selectedGoal)!,
      parameters,
      cadence: { kind: cadenceKind, timezone: cadenceTimezone.trim() || "UTC", daily_hour: cadenceKind === "daily" ? hour : null, daily_minute: cadenceKind === "daily" ? minute : null },
      expires_at: expiry,
      idempotency_key: newProcedureRequestKey("procedure-schedule"),
    };
    const pendingAction: PendingProcedureAction = { schema_version: 1, kind: "schedule", routineId: prepared.routineId, request };
    const requestOwnerScope = captureOwnerScope();
    const pendingError = persistPending(pendingKey, pendingAction);
    if (pendingError) { setError(pendingError); return; }
    setPending(pendingAction);
    setBusy(true);
    setError(null);
    try {
      const receipt = correlateScheduleReceipt(
        await procedureV2Api.schedule(prepared.routineId, request),
        request,
        prepared.routineId,
      );
      if (!ownerRequestIsCurrent(requestOwnerScope)) return;
      setSchedule(receipt);
      if (isConfirmedScheduleStatus(receipt.status)) {
        clearPending(pendingKey);
        setPending(null);
        setNotice(`Finite schedule ${receipt.scheduled_job_id} accepted; it expires ${formatTime(receipt.expires_at)}.`);
      } else {
        setError(`The finite schedule outcome is ${receipt.status}, not confirmed; the exact request remains retained for reconciliation. Do not create a new schedule.`);
      }
    } catch (cause) { handlePendingFailure("schedule", cause, requestOwnerScope, pendingKey); }
    finally { if (ownerRequestIsCurrent(requestOwnerScope)) setBusy(false); }
  };

  const retryPending = () => {
    if (!pending) return;
    if (pending.kind === "prepare") {
      const requestOwnerScope = captureOwnerScope();
      setBusy(true);
      setError(null);
      void procedureV2Api.prepareFromTasks(pending.request)
        .then((next) => acceptPreparedResponse(next, requestOwnerScope, pendingKey))
        .catch((cause) => { handlePendingFailure("prepare", cause, requestOwnerScope, pendingKey); })
        .finally(() => { if (ownerRequestIsCurrent(requestOwnerScope)) setBusy(false); });
    } else if (pending.kind === "invoke") {
      // Reconciliation uses the retained server-bound routine id and exact
      // body. It intentionally does not require a fresh local routine/package
      // snapshot, which may be stale after a remount or delayed refresh.
      const requestOwnerScope = captureOwnerScope();
      const exactPreparedVersionId = prepared
        && prepared.routineId === pending.routineId
        && prepared.version === pending.request.version
        ? prepared.versionId
        : null;
      const exactRoutineVersionId = routine
        && routine.id === pending.routineId
        ? routine.versions.find((candidate) => candidate.version === pending.request.version)?.id
        : null;
      // Old pending entries predate versionId.  A same-owner prepared receipt
      // or exact routine readback may supply the identity; numeric version
      // alone is never accepted as a correlation scope.
      const versionId = pending.versionId ?? exactPreparedVersionId ?? exactRoutineVersionId ?? null;
      void sendInvocationRequest(pending.routineId, pending.request, versionId, requestOwnerScope, pendingKey);
    }
    else {
      const requestOwnerScope = captureOwnerScope();
      setBusy(true);
      setError(null);
      void procedureV2Api.schedule(pending.routineId, pending.request)
        .then((rawReceipt) => {
          const receipt = correlateScheduleReceipt(rawReceipt, pending.request, pending.routineId);
          if (!ownerRequestIsCurrent(requestOwnerScope)) return;
          setSchedule(receipt);
          if (isConfirmedScheduleStatus(receipt.status)) {
            clearPending(pendingKey);
            setPending(null);
            setNotice(`Schedule reconciliation returned ${receipt.scheduled_job_id}.`);
          } else {
            setError(`The finite schedule outcome remains ${receipt.status}; the exact request remains retained for reconciliation. Do not create a new schedule.`);
          }
        })
        .catch((cause) => { handlePendingFailure("schedule", cause, requestOwnerScope, pendingKey); })
        .finally(() => { if (ownerRequestIsCurrent(requestOwnerScope)) setBusy(false); });
    }
  };

  const controlSchedule = async (action: "pause" | "resume" | "revoke") => {
    if (!schedule) return;
    setBusy(true);
    setError(null);
    const requestOwnerScope = captureOwnerScope();
    try {
      const next = action === "revoke"
        ? await procedureV2Api.revokeSchedule(schedule.binding_id, { expected_binding_revision: schedule.revision, idempotency_key: newProcedureRequestKey("schedule-revoke"), reason: "Operator revoked the finite procedure schedule." })
        : await procedureV2Api.scheduleControl(schedule.binding_id, { action, expected_binding_revision: schedule.revision, idempotency_key: newProcedureRequestKey(`schedule-${action}`) });
      if (ownerRequestIsCurrent(requestOwnerScope)) setSchedule(next);
    } catch (cause) { if (ownerRequestIsCurrent(requestOwnerScope)) setError(errorMessage(cause)); }
    finally { if (ownerRequestIsCurrent(requestOwnerScope)) setBusy(false); }
  };

  const startFreshProcedureRebind = () => {
    if (unresolvedRecovery) {
      setError("Refresh authority to reconcile the retained procedure outcome before starting a fresh preview.");
      return;
    }
    if (pending) {
      setError("Reconcile the retained exact request before starting a fresh rebind.");
      return;
    }
    resetPrepared();
    setPreview(null);
    setPreviewRequestKey(newProcedureRequestKey("procedure-preview"));
    setError(null);
    setNotice("Choose a verified source and create a fresh preview before preparing another procedure.");
  };

  const sourceOptions = templateId === "selected-meeting-prep" ? meetingTasks : templateId === "watch-and-public-browser" ? watchTasks : browserTasks;
  const secondOptions = templateId === "watch-and-public-browser" ? watchTasks : [];
  const exactApproval = pendingApprovals.find((approval) => approval.id === prepared?.approvalId);
  const installApprovalStatus = prepared?.installApprovalStatus ?? "missing";
  const installApprovalCurrent = isCurrentInstallApproval(prepared);
  const installApprovalNeedsFresh = installApprovalNeedsFreshRebind(prepared, installApprovalStatus, installApprovalCurrent);

  if (!active) return null;

  return (
    <section className="cockpit-panel cockpit-panel--embedded mb-4" aria-label="Reviewed procedures" data-testid="procedure-v2-review">
      <div className="cockpit-section-header">
        <div>
          <div className="cockpit-eyebrow">PROCEDURES</div>
          <h2>Reuse verified work safely</h2>
          <p>Save a fixed, readback-verified journey, review its package, then invoke it for a fresh goal or schedule a finite public check.</p>
        </div>
      </div>
      {!ownerPrincipalId || !ownerSessionId ? <div className="rounded border border-amber-500/40 p-3" role="status">Sign in to prepare an owner-bound procedure.</div> : null}
      {taskLoadError ? <div className="mt-2 rounded border border-amber-500/40 p-3" role="alert">Source tasks unavailable: {taskLoadError}</div> : null}
      {ownerPrincipalId && ownerSessionId ? <div className="mt-3 grid gap-2 rounded border border-white/10 p-3">
        <div className="font-semibold">Existing reviewed procedures</div>
        <p className="text-xs opacity-75">Load an owner/session-bound routine and its server metadata before invoking it. Selection performs read-only GETs; no install, activation, or execution is automatic.</p>
        <label>Existing reviewed procedure
          <select aria-label="Existing reviewed procedure" className="cockpit-input mt-1 w-full" value={existingRoutineId} disabled={busy || existingRoutineLoading || Boolean(pending) || Boolean(packageActivationRecovery) || Boolean(lifecycleReconcileRequired)} onChange={(event) => { const id = event.currentTarget.value; if (id) void loadExistingRoutine(id); else { setExistingRoutineId(""); setExistingVersionNumber(null); } }}>
            <option value="">Choose an existing v2 procedure</option>
            {existingRoutineOptions.map((candidate) => <option key={candidate.id} value={candidate.id}>{candidate.name} · {candidate.state} · {candidate.current_version ? `current v${candidate.current_version}` : "prepared"}</option>)}
          </select>
        </label>
        {existingRoutineId && routine && existingVersionNumber !== null ? <label>Procedure version
          <select aria-label="Existing procedure version" className="cockpit-input mt-1 w-full" value={String(existingVersionNumber)} disabled={busy || existingRoutineLoading || Boolean(pending) || Boolean(packageActivationRecovery) || Boolean(lifecycleReconcileRequired)} onChange={(event) => void loadExistingRoutine(existingRoutineId, Number(event.currentTarget.value))}>
            {routine.versions.filter((version) => version.template_id && version.procedure_binding?.binding_id).sort((left, right) => right.version - left.version).map((version) => <option key={version.version} value={version.version}>v{version.version}{version.version === routine.current_version ? " · current" : " · available earlier version"} · {version.template_id}</option>)}
          </select>
        </label> : null}
        {existingRoutineLoading ? <div className="text-xs opacity-75" role="status">Loading owner-bound procedure metadata…</div> : null}
        {existingRoutineError ? <div className="rounded border border-amber-500/40 p-2 text-xs" role="alert">Existing procedure unavailable: {existingRoutineError}</div> : null}
        {!existingRoutineLoading && !existingRoutineError && existingRoutineOptions.length === 0 ? <div className="text-xs opacity-75">No existing reviewed v2 procedures are available for this owner session.</div> : null}
      </div> : null}
      {pending && (!prepared || !routine) ? <div className="mt-2 rounded border border-amber-500/40 p-3 text-sm" role="alert">An unconfirmed {pending.kind} request is retained{pending && "routineId" in pending ? <> for routine <span className="font-mono">{pending.routineId}</span></> : " before the server preparation receipt was confirmed"}. Restore the owner-bound state before retrying the exact request. <button type="button" className="underline" disabled={busy} onClick={retryPending}>Retry exact request</button></div> : null}
      <div className="mt-3 grid gap-3 rounded border border-white/10 p-3">
        <div className="font-semibold">1. Select a verified source and preview</div>
        {selectedSourceTask && sourceBelongsToOwner(selectedSourceTask, ownerPrincipalId, ownerSessionId) ? <div className="rounded border border-sky-500/30 p-2 text-xs" role="status">Work Board source selected: <span className="font-mono">{selectedSourceTask.task_id}</span> · {selectedSourceTask.title}. Preview still requires an explicit action. <button type="button" className="underline" onClick={() => onOpenTask?.(selectedSourceTask.task_id)}>Open task</button></div> : null}
        <div className="grid gap-2 sm:grid-cols-2">
          <label>Fixed template
            <select aria-label="Procedure template" className="cockpit-input mt-1 w-full" value={templateId} disabled={busy} onChange={(event) => setTemplateId(event.currentTarget.value as ProcedureV2TemplateId)}>
              <option value="public-browser-check">Public browser check</option>
              <option value="watch-and-public-browser">Research watch → public browser</option>
              <option value="selected-meeting-prep">Selected meeting preparation</option>
            </select>
          </label>
          <label>Procedure name
            <input aria-label="Procedure name" className="cockpit-input mt-1 w-full" maxLength={80} value={name} disabled={busy} onChange={(event) => setName(event.currentTarget.value)} placeholder="e.g. Check the public status page" />
          </label>
        </div>
        <label>{templateId === "watch-and-public-browser" ? "First source (watch or browser; server enforces order)" : "Verified source task"}
          <select aria-label="Verified source task" className="cockpit-input mt-1 w-full" value={sourceTaskId} disabled={busy} onChange={(event) => setSourceTaskId(event.currentTarget.value)}>
            <option value="">Choose a Done task with verified readback</option>
            {sourceOptions.map((task) => <option key={task.task_id} value={task.task_id}>{sourceLabel(task)}</option>)}
          </select>
        </label>
        {templateId === "watch-and-public-browser" ? <label>Second source (the other fixed leaf)
          <select aria-label="Second source task" className="cockpit-input mt-1 w-full" value={secondSourceTaskId} disabled={busy} onChange={(event) => setSecondSourceTaskId(event.currentTarget.value)}>
            <option value="">Choose the paired verified task</option>
            {secondOptions.filter((task) => task.task_id !== sourceTaskId).map((task) => <option key={task.task_id} value={task.task_id}>{sourceLabel(task)}</option>)}
          </select>
        </label> : null}
        {eligibleTasks.length === 0 ? <div className="text-xs opacity-75">No eligible verified task is currently visible for this template. Complete the leaf and its independent readback first.</div> : null}
        <button type="button" className="cockpit-feedback-button justify-self-start" disabled={busy || Boolean(pending) || unresolvedRecovery || !ownerPrincipalId || !ownerSessionId} onClick={() => void previewProcedure()}>{busy && !preview ? "Preparing preview…" : "Preview fixed procedure"}</button>
        {preview ? <div className="rounded border border-white/10 bg-black/20 p-3 text-xs" role="region" aria-label="Procedure preview">
          <div className="font-semibold">{procedureTemplateLabel(preview.template_id)} · preview</div>
          <div className="mt-1 break-all font-mono">Digest: {preview.preview_digest}</div>
          <div>Expires: {formatTime(preview.expires_at)} · permissions: {preview.permissions.join(", ") || "none returned"}</div>
          <div>Fixed limits: {preview.plan.limits.max_steps} steps · {preview.plan.limits.max_total_seconds}s · verifier {preview.plan.verifier}</div>
          <div className="mt-2">Steps: {preview.plan.steps.map((step) => `${step.step_id} (${step.capability_id})`).join(" → ")}</div>
          <div>Source proof: {preview.source_refs.map((source) => `${source.task_id} rev ${source.task_revision}`).join(" · ") || "unavailable"}</div>
          <div className="mt-2 flex flex-wrap gap-2"><button type="button" className="cockpit-feedback-button" disabled={busy || Boolean(pending) || unresolvedRecovery} onClick={() => void prepareProcedure()}>Prepare this reviewed version</button><button type="button" className="cockpit-feedback-button" disabled={busy || unresolvedRecovery} onClick={() => { setPreview(null); setPreviewRequestKey(newProcedureRequestKey("procedure-preview")); }}>Discard preview</button></div>
        </div> : null}
      </div>

      {prepared && routine ? <div className="mt-3 grid gap-3 rounded border border-white/10 p-3">
        <div className="font-semibold">2. Review, install, and activate the exact package</div>
        <div className="text-xs">Binding <span className="font-mono">{prepared.bindingId}</span> · routine <span className="font-mono">{prepared.routineId}</span> · revision {routine.revision} · state {routine.state} · package {routine.package?.status ?? "unknown"}</div>
        {routine.state === "revoked" ? <div className="rounded border border-red-500/40 p-2" role="alert">This procedure is revoked permanently. Prepare a new version from a fresh verified source.</div> : null}
        <div className="flex flex-wrap gap-2">
          <button type="button" className="cockpit-feedback-button" disabled={busy || routine.state === "revoked" || Boolean(packageActivationRecovery) || lifecycleReconcileRequired} onClick={() => void previewPackage()}>Preview reviewed package</button>
          {routine.state === "prepared" ? <button type="button" className="cockpit-feedback-button" disabled={busy || !installApprovalCurrent || lifecycleReconcileRequired || Boolean(packageActivationRecovery)} onClick={() => void runLifecycle("install")}>Install with exact approval</button> : null}
          {(routine.state === "installed" || routine.state === "paused") && routinePackageMatchesVersion(routine, currentVersion, prepared?.versionId) ? <button type="button" className="cockpit-feedback-button" disabled={busy || lifecycleReconcileRequired || Boolean(packageActivationRecovery)} onClick={() => void runLifecycle("activate")}>{routine.state === "paused" ? "Resume procedure" : "Activate procedure"}</button> : null}
          {routine.state !== "revoked" ? <button type="button" className="cockpit-feedback-button" disabled={busy || lifecycleReconcileRequired || Boolean(packageActivationRecovery)} onClick={() => void runLifecycle("pause")}>Pause future invocations</button> : null}
          {routine.state !== "revoked" ? <button type="button" className="cockpit-feedback-button" disabled={busy || lifecycleReconcileRequired || Boolean(packageActivationRecovery)} onClick={() => void runLifecycle("revoke")}>Revoke permanently</button> : null}
          {existingRoutineId && routine.state !== "revoked" && existingVersionNumber !== null && routineCurrentVersion !== null && existingVersionNumber !== routineCurrentVersion ? <button type="button" className="cockpit-feedback-button" disabled={busy || lifecycleReconcileRequired || Boolean(packageActivationRecovery)} onClick={() => void runLifecycle("rollback")}>Rollback future invocations to version {existingVersionNumber}</button> : null}
          <button type="button" className="cockpit-feedback-button" disabled={busy} onClick={() => void refreshRoutine()}>Refresh authority</button>
        </div>
        {lifecycleReconcileRequired ? <div className="rounded border border-amber-500/40 p-2 text-xs" role="alert">The last lifecycle request may have reached the server, but its readback was unavailable. Refresh authority before trying another lifecycle action; the client will not send an automatic duplicate.</div> : null}
        {lifecycleActivationRecovery ? <div className="rounded border border-amber-500/40 p-2 text-xs" role="alert">Procedure {lifecycleActivationRecovery.action} is unverified for version <span className="font-mono">{lifecycleActivationRecovery.versionId}</span> at request revision {lifecycleActivationRecovery.requestRevision}. Refresh authority before trying another lifecycle action.</div> : null}
        {packageActivationRecovery ? <div className="rounded border border-amber-500/40 p-2 text-xs" role="alert">Package activation is unverified. Approval <span className="font-mono">{packageActivationRecovery.approvalId}</span>, version <span className="font-mono">{packageActivationRecovery.versionId}</span>, digest <span className="font-mono">{packageActivationRecovery.digest}</span>, and expected revision {packageActivationRecovery.expectedRevision} are retained. Refresh authority before any repeat mutation.</div> : null}
        {(routine.state === "installed" || routine.state === "paused") && routine.package?.status === "active" && !routinePackageMatchesVersion(routine, currentVersion, prepared?.versionId) ? <div className="rounded border border-amber-500/40 p-2 text-xs" role="alert">Activation is blocked because the current routine package digest does not match the requested installed version. Refresh authority before retrying.</div> : null}
        {routine.state === "prepared" ? <div className="grid gap-2 rounded border border-amber-500/40 p-2 text-xs">
          <div>Installation is bound to the server-created job and approval returned with this preparation. The global Pending approvals list is only a review surface; this card never accepts an arbitrary approval ID.</div>
          <div role="status">Install job <span className="font-mono">{prepared.installJobId}</span> · exact approval <span className="font-mono">{prepared.approvalId}</span> · server status {installApprovalStatus} {prepared.installApprovalExpiresAt ? `· expires ${formatTime(prepared.installApprovalExpiresAt)}` : ""}{exactApproval ? ` · pending-list status ${exactApproval.status}` : " · retained for this owner session; it may no longer appear in the global pending list"}</div>
          {exactApproval ? <div className="text-xs opacity-80">{exactApproval.tool_name}: {exactApproval.summary}</div> : null}
          {installApprovalStatus === "pending" ? <div className="rounded border border-amber-500/40 p-2" role="status">This exact install approval is pending operator review. Review it in Pending approvals before installing.</div> : null}
          {installApprovalStatus === "consumed" ? <div className="rounded border border-sky-500/40 p-2" role="status">This exact install approval was already consumed. Refresh authority to reconcile the install receipt before taking another action.</div> : null}
          {installApprovalNeedsFresh ? <div className="rounded border border-amber-500/40 p-2" role="alert">The server approval is {installApprovalStatus === "approved" ? "no longer current" : installApprovalStatus}. {prepared.installRecoveryAction ? `Recovery: ${prepared.installRecoveryAction}.` : "Start a fresh preview/rebind before installing."}</div> : null}
          {installApprovalNeedsFresh ? <button type="button" className="cockpit-feedback-button justify-self-start" disabled={busy || Boolean(pending)} onClick={startFreshProcedureRebind}>Start fresh preview/rebind</button> : null}
          {onOpenApprovals && prepared.approvalId ? <button type="button" className="cockpit-feedback-button justify-self-start" onClick={() => onOpenApprovals(prepared.approvalId!)}>Review this exact approval in Pending approvals</button> : null}
        </div> : null}
        {packagePreview ? <div className="rounded border border-white/10 bg-black/20 p-3 text-xs" role="region" aria-label="Procedure package preview">
          <div className="font-semibold">{packagePreview.manifest.display_name}</div>
          <div>{packagePreview.manifest.summary}</div>
          <div className="mt-1 break-all font-mono">Digest: {packagePreview.digest} · installed: {packagePreview.installed_package_digest ?? "none"}</div>
          <div>Authority: {packagePreview.manifest.authority.tools.length} tool(s), {packagePreview.manifest.authority.filesystem.length} filesystem path(s), network {packagePreview.manifest.authority.network ? "enabled" : "disabled"}, {packagePreview.manifest.authority.secrets.length} secret(s)</div>
          <div>Package status: {packagePreview.status}{packagePreview.review_id ? ` · review ${packagePreview.review_id}` : ""}</div>
          <div className="mt-2 flex flex-wrap gap-2">
            {packagePreview.digest === packagePreview.installed_package_digest && !packagePreview.review_id ? <button type="button" className="cockpit-feedback-button" disabled={busy || Boolean(packageActivationRecovery) || lifecycleReconcileRequired} onClick={() => void reviewPackage()}>Record package review</button> : null}
            {packagePreview.review_id && !packageApproval ? <button type="button" className="cockpit-feedback-button" disabled={busy || Boolean(packageActivationRecovery) || lifecycleReconcileRequired} onClick={() => void prepareApproval()}>Prepare activation approval</button> : null}
            {packageApproval?.status === "pending" ? <><button type="button" className="cockpit-feedback-button" disabled={busy || Boolean(packageActivationRecovery) || lifecycleReconcileRequired} onClick={() => void decideApproval("approved")}>Approve activation</button><button type="button" className="cockpit-feedback-button" disabled={busy || Boolean(packageActivationRecovery) || lifecycleReconcileRequired} onClick={() => void decideApproval("denied")}>Deny activation</button></> : null}
            {packageApproval?.status === "approved" ? <button type="button" className="cockpit-feedback-button" disabled={busy || Boolean(packageActivationRecovery) || lifecycleReconcileRequired} onClick={() => void activatePackage()}>Activate reviewed package</button> : null}
          </div>
          {packageApproval ? <div className="mt-2 rounded bg-black/20 p-2" role="status">Activation approval {packageApproval.status}: <span className="font-mono">{packageApproval.approval_id}</span></div> : null}
          {routine.state === "installed" && packagePreview.status === "active" ? <div className="mt-2 rounded border border-amber-500/40 p-2" role="status">The package is active, but the procedure is still installed. Activate the procedure above before invoking or scheduling it.</div> : null}
          {packagePreview.digest !== packagePreview.installed_package_digest ? <div className="mt-2 rounded border border-amber-500/40 p-2" role="alert">Package digest drifted. Refresh the routine before reviewing or activating.</div> : null}
        </div> : null}
      </div> : null}

      {prepared && routine ? <div className="mt-3 grid gap-3 rounded border border-white/10 p-3">
        <div className="font-semibold">3. Invoke or schedule with fresh authority</div>
        {!routineActive ? <div className="rounded border border-amber-500/40 p-2 text-xs" role="status">Invocation is blocked until the exact installed package has a current review, approval, and activation receipt.</div> : null}
        <div className="grid gap-2 sm:grid-cols-2">
          <label>Fresh goal
            <select aria-label="Procedure goal" className="cockpit-input mt-1 w-full" value={goalId} disabled={busy || !routineActive} onChange={(event) => setGoalId(event.currentTarget.value)}>
              <option value="">Choose an active goal</option>
              {activeGoals.map((goal) => <option key={goal.id} value={goal.id}>{goal.title} · revision {goal.revision ?? "unavailable"}</option>)}
            </select>
          </label>
          {templateId === "watch-and-public-browser" ? <label>Owner-visible source watch
            <select aria-label="Source watch" className="cockpit-input mt-1 w-full" value={watchId} disabled={busy || !routineActive || eligibleWatches.length === 0} onChange={(event) => setWatchId(event.currentTarget.value)}>
              <option value="">Choose an active source watch</option>
              {eligibleWatches.map((watch) => <option key={watch.id} value={watch.id}>{watch.id.slice(0, 16)} · goal revision {watch.goal_revision} · current watch revision {watch.plan_revision}</option>)}
            </select>
            <span className="mt-1 block text-[10px] opacity-70">The current owner-scoped revision is read from the server; it cannot be typed manually.</span>
            {sourceWatchError ? <span className="mt-1 block text-xs text-amber-200" role="alert">Source watches unavailable: {sourceWatchError}</span> : null}
          </label> : null}
        </div>
        {templateId === "selected-meeting-prep" ? <div className="grid gap-2 rounded border border-white/10 p-2 sm:grid-cols-2">
          <div className="sm:col-span-2 text-xs font-semibold">Choose a typed M5 calendar event</div>
          <label>Calendar connection
            <select aria-label="Calendar connection" className="cockpit-input mt-1 w-full" value={calendarConnectionId} disabled={busy || !routineActive || calendarLoading} onChange={(event) => chooseCalendarConnection(event.currentTarget.value)}>
              <option value="">Choose an active connection</option>
              {calendarConnections.filter((connection) => connection.state === "active").map((connection) => <option key={connection.connection_id} value={connection.connection_id}>{connection.label} · revision {connection.revision}</option>)}
            </select>
          </label>
          <button type="button" className="cockpit-feedback-button self-end" disabled={busy || !routineActive || calendarLoading || !selectedCalendarConnection} onClick={() => void verifyCalendar()}>{calendarLoading ? "Loading calendar metadata…" : "Verify and list calendars"}</button>
          {calendarVerification ? <label>Returned calendar
            <select aria-label="Returned calendar" className="cockpit-input mt-1 w-full" value={calendarId} disabled={busy || !routineActive || calendarLoading} onChange={(event) => { setCalendarId(event.currentTarget.value); setCalendarConsent(null); setCalendarEvents(null); setCalendarEventId(""); }}>
              <option value="">Choose a returned calendar</option>
              {calendarVerification.calendars.map((calendar) => <option key={calendar.calendar_id} value={calendar.calendar_id}>{calendar.summary}</option>)}
            </select>
            <span className="mt-1 block text-[10px] opacity-70">Returned calendar list revision {calendarVerification.calendar_list_revision}</span>
          </label> : null}
          {calendarVerification ? <div className="self-end text-xs">The event read uses only summary, start, and end. Provider IDs stay inside the typed calendar adapter.</div> : null}
          <label className="sm:col-span-2 flex items-start gap-2 text-xs"><input aria-label="Allow governed calendar model" type="checkbox" checked={allowCalendarModel} disabled={busy || !routineActive || calendarLoading} onChange={(event) => setAllowCalendarModel(event.currentTarget.checked)} /><span>Allow the governed model for this bounded calendar read (required to create the consent).</span></label>
          <button type="button" className="cockpit-feedback-button justify-self-start" disabled={busy || !routineActive || calendarLoading || !calendarId || !allowCalendarModel} onClick={() => void createCalendarConsentAndLoadEvents()}>Create typed consent and load events</button>
          {calendarEvents ? <label className="sm:col-span-2">Returned meeting
            <select aria-label="Returned meeting" className="cockpit-input mt-1 w-full" value={calendarEventId} disabled={busy || !routineActive || calendarLoading} onChange={(event) => setCalendarEventId(event.currentTarget.value)}>
              <option value="">Choose a returned event</option>
              {calendarEvents.events.map((event) => <option key={event.event_binding_id} value={event.event_binding_id}>{event.summary} · {formatTime(event.start)}</option>)}
            </select>
            <span className="mt-1 block text-[10px] opacity-70">Consent revision {calendarEvents.consent_revision} · connection revision {calendarEvents.connection_revision} · list revision {calendarEvents.calendar_list_revision}</span>
          </label> : null}
          <label className="sm:col-span-2">Meeting purpose
            <textarea aria-label="Meeting purpose" className="cockpit-input mt-1 w-full" maxLength={500} rows={2} value={meetingPurpose} disabled={busy || !routineActive} onChange={(event) => setMeetingPurpose(event.currentTarget.value)} />
            <span className="mt-1 block text-[10px] opacity-70">{meetingPurpose.length}/500 characters</span>
          </label>
          {calendarError ? <div className="sm:col-span-2 rounded border border-amber-500/40 p-2 text-xs" role="alert">Calendar metadata unavailable: {calendarError}</div> : null}
        </div> : null}
        {pending ? <div className="rounded border border-amber-500/40 p-2 text-xs" role="alert">The last {pending.kind} has no confirmed receipt. Its exact key and body are retained for reconciliation; do not create a new request. <button type="button" className="underline" disabled={busy} onClick={retryPending}>Retry exact request</button></div> : null}
        {invokeReceipt ? <div className={`rounded border p-2 text-xs ${isConfirmedInvocationStatus(invokeReceipt.status) ? "border-emerald-500/40" : "border-amber-500/40"}`} role="status">Invocation {invokeReceipt.status}: task <span className="font-mono">{invokeReceipt.task_id}</span> · attempt {invokeReceipt.attempt_id ?? "pending"} · job {invokeReceipt.job_id ?? "pending"} · input artifact <span className="font-mono">{invokeReceipt.input_artifact_id}</span>. {isConfirmedInvocationStatus(invokeReceipt.status) ? null : "The exact request remains retained for reconciliation. "}<button type="button" className="underline" onClick={() => onOpenTask?.(invokeReceipt.task_id)}>Open Work Board</button></div> : null}
        {schedule ? <div className={`rounded border p-2 text-xs ${isConfirmedScheduleStatus(schedule.status) ? "border-white/10" : "border-amber-500/40"}`} role="status">Schedule {schedule.status}: <span className="font-mono">{schedule.scheduled_job_id}</span> · next {formatTime(schedule.next_run)} · expires {formatTime(schedule.expires_at)} · revision {schedule.revision} {isConfirmedScheduleStatus(schedule.status) ? null : "· Exact request remains retained for reconciliation."}
          <div className="mt-2 flex flex-wrap gap-2"><button type="button" className="cockpit-feedback-button" disabled={busy || schedule.state === "revoked"} onClick={() => void controlSchedule(schedule.state === "paused" ? "resume" : "pause")}>{schedule.state === "paused" ? "Resume schedule" : "Pause schedule"}</button><button type="button" className="cockpit-feedback-button" disabled={busy || schedule.state === "revoked"} onClick={() => void controlSchedule("revoke")}>Revoke schedule</button></div>
        </div> : null}
        <div className="flex flex-wrap gap-2">
          <button type="button" className="cockpit-feedback-button" disabled={busy || !routineActive || Boolean(pending)} onClick={() => void invoke()}>Invoke for this goal</button>
          {isPublicSchedulableProcedure(templateId) ? <button type="button" className="cockpit-feedback-button" disabled={busy || !routineActive || Boolean(pending)} onClick={() => void scheduleProcedure()}>Create finite schedule</button> : null}
        </div>
        {isPublicSchedulableProcedure(templateId) ? <div className="grid gap-2 rounded border border-white/10 p-2 sm:grid-cols-2">
          <label>Cadence
            <select aria-label="Schedule cadence" className="cockpit-input mt-1 w-full" value={cadenceKind} disabled={busy || !routineActive} onChange={(event) => setCadenceKind(event.currentTarget.value as ProcedureV2CadenceKind)}><option value="hourly">Hourly</option><option value="6h">Every 6 hours</option><option value="daily">Daily</option></select>
          </label>
          <label>Timezone
            <input aria-label="Schedule timezone" className="cockpit-input mt-1 w-full" value={cadenceTimezone} disabled={busy || !routineActive} onChange={(event) => setCadenceTimezone(event.currentTarget.value)} />
          </label>
          {cadenceKind === "daily" ? <><label>Daily hour (0–23)<input aria-label="Daily hour" className="cockpit-input mt-1 w-full" inputMode="numeric" value={cadenceHour} disabled={busy || !routineActive} onChange={(event) => setCadenceHour(event.currentTarget.value)} /></label><label>Daily minute (0–59)<input aria-label="Daily minute" className="cockpit-input mt-1 w-full" inputMode="numeric" value={cadenceMinute} disabled={busy || !routineActive} onChange={(event) => setCadenceMinute(event.currentTarget.value)} /></label></> : null}
          <label className="sm:col-span-2">Schedule expiry (maximum seven days and current goal budget)
            <input aria-label="Schedule expiry" type="datetime-local" className="cockpit-input mt-1 w-full" value={scheduleExpiry} disabled={busy || !routineActive} onChange={(event) => setScheduleExpiry(event.currentTarget.value)} />
          </label>
        </div> : null}
      </div> : null}
      {error ? <div className="mt-3 rounded border border-red-500/40 p-3 text-sm" role="alert">{error}</div> : null}
      {notice ? <div className="mt-3 rounded border border-emerald-500/40 p-3 text-sm" role="status">{notice}</div> : null}
    </section>
  );
}
