import { API_URL } from "../config/constants";
import { apiFetch } from "./api";
import type {
  WorkBoardRoutinePackageApproval,
  WorkBoardRoutinePackagePreview,
  WorkBoardRoutineRead,
  WorkBoardRoutineVersion,
  WorkBoardTask,
} from "../types";

export type ProcedureV2TemplateId =
  | "public-browser-check"
  | "watch-and-public-browser"
  | "selected-meeting-prep";

export type ProcedureV2ParameterKind =
  | "goal_id"
  | "goal_revision"
  | "source_watch_id"
  | "watch_revision"
  | "schema_version"
  | "consent_id"
  | "event_binding_id"
  | "event_binding_revision"
  | "consent_revision"
  | "connection_revision"
  | "event_revision"
  | "calendar_list_revision"
  | "purpose";

export interface ProcedureV2ParameterSpec {
  name: string;
  kind: ProcedureV2ParameterKind | string;
  required: boolean;
}

export interface ProcedureV2PlanStep {
  step_id: string;
  capability_id: string;
  capability_version: string;
  typed_input_ref: string;
  typed_input_digest: string;
}

export interface ProcedureV2PlanLimits {
  max_steps: 2;
  max_total_seconds: 300;
}

export interface ProcedureV2Plan {
  schema_version: 2;
  template_id: ProcedureV2TemplateId;
  steps: ProcedureV2PlanStep[];
  parameters: ProcedureV2ParameterSpec[];
  verifier: "leaf_readbacks";
  limits: ProcedureV2PlanLimits;
}

export interface ProcedureV2SourceTaskRef {
  task_id: string;
  expected_revision: number;
}

export interface ProcedureV2SourceRef {
  task_id: string;
  task_revision: number;
  attempt_id: string | null;
  job_id: string | null;
  artifact_ids_and_hashes: Array<{ artifact_id: string; sha256: string }>;
  capability_id: string;
  capability_version: string;
  goal_id: string;
  goal_revision: number;
}

export interface ProcedureV2BindingMetadata {
  binding_id: string | null;
  state: string;
  revision: number | null;
  preview_digest: string | null;
  preview_expires_at: string | null;
  install_job_id: string | null;
  approval_id: string | null;
  /** Server-owned install approval state. Schema-v2 responses must include it. */
  install_approval_status?: string | null;
  install_approval_expires_at?: string | null;
  install_recovery_action?: string | null;
}

export interface ProcedureV2Limits {
  max_steps: number;
  max_total_seconds: number;
}

export interface ProcedureV2Preview {
  status: "preview";
  template_id: ProcedureV2TemplateId;
  preview_digest: string;
  expires_at: string;
  plan: ProcedureV2Plan;
  source_refs: ProcedureV2SourceRef[];
  parameter_schema: ProcedureV2ParameterSpec[];
  permissions: string[];
  limits: ProcedureV2Limits;
  version_diff: Record<string, unknown> | null;
}

export interface ProcedureV2Prepared {
  status: "prepared" | "blocked";
  binding_id: string;
  routine_id?: string;
  version_id?: string;
  version?: number;
  schema_version: 2;
  template_id: ProcedureV2TemplateId;
  revision: number;
  request_digest: string;
  preview_digest: string;
  preview_expires_at: string;
  plan?: ProcedureV2Plan;
  source_refs?: ProcedureV2SourceRef[];
  package_state?: string;
  routine_state?: string;
  /** The server-created install job and its exact approval receipt. */
  install_job_id?: string | null;
  approval_id?: string | null;
  install_approval_status?: string | null;
  install_approval_expires_at?: string | null;
  install_recovery_action?: string | null;
  audit_receipt_id?: string | null;
  recovery_action?: string | null;
}

export interface ProcedureV2PrepareRequest {
  template_id: ProcedureV2TemplateId;
  source_tasks: ProcedureV2SourceTaskRef[];
  name: string;
  idempotency_key: string;
  preview_digest: string;
}

export interface ProcedureV2RoutineVersion extends Omit<WorkBoardRoutineVersion, "source_provenance"> {
  source_provenance: Record<string, unknown>;
  schema_version?: number;
  template_id?: ProcedureV2TemplateId;
  binding_id?: string | null;
  plan_digest?: string | null;
  source_refs?: ProcedureV2SourceRef[];
  parameter_schema?: ProcedureV2ParameterSpec[];
  procedure_binding?: ProcedureV2BindingMetadata | null;
}

export interface ProcedureV2Routine extends Omit<WorkBoardRoutineRead, "versions"> {
  schema_version?: number;
  template_id?: ProcedureV2TemplateId;
  binding_id?: string | null;
  plan_digest?: string | null;
  source_refs?: ProcedureV2SourceRef[];
  parameter_schema?: ProcedureV2ParameterSpec[];
  versions: ProcedureV2RoutineVersion[];
}

export interface ProcedureV2PublicParameters {
  goal_id: string;
  expected_goal_revision: number;
}

export interface ProcedureV2WatchParameters {
  goal_id: string;
  expected_goal_revision: number;
  source_watch_id: string;
  expected_watch_revision: number;
}

export interface ProcedureV2MeetingParameters {
  goal_id: string;
  schema_version: 1;
  consent_id: string;
  event_binding_id: string;
  expected_event_binding_revision: number;
  expected_consent_revision: number;
  expected_connection_revision: number;
  event_revision: string;
  calendar_list_revision: string;
  goal_revision: number;
  purpose: string;
}

export type ProcedureV2InvokeParameters = ProcedureV2PublicParameters | ProcedureV2WatchParameters | ProcedureV2MeetingParameters;

export interface ProcedureV2InvokeRequest {
  version: number;
  expected_routine_revision: number;
  goal_id: string;
  expected_goal_revision: number;
  parameters: ProcedureV2InvokeParameters;
  invocation_uuid: string;
}

export interface ProcedureV2InvokeReceipt {
  status: "accepted" | "succeeded" | "degraded" | "blocked" | "unknown" | string;
  routine_id: string;
  version: number;
  schema_version: 2;
  template_id: ProcedureV2TemplateId;
  invocation_uuid: string;
  scope: string;
  goal_id: string;
  goal_revision: number;
  task_id: string;
  attempt_id: string | null;
  job_id: string | null;
  input_artifact_id: string;
  input_digest: string;
  plan_digest: string;
  revision: number;
  audit_receipt_id?: string | null;
  recovery_action?: string | null;
}

export type ProcedureV2CadenceKind = "hourly" | "6h" | "daily";

export interface ProcedureV2Cadence {
  kind: ProcedureV2CadenceKind;
  timezone: string;
  daily_hour: number | null;
  daily_minute: number | null;
}

export interface ProcedureV2ScheduleRequest {
  version: number;
  expected_routine_revision: number;
  goal_id: string;
  expected_goal_revision: number;
  parameters: ProcedureV2InvokeParameters;
  cadence: ProcedureV2Cadence;
  expires_at: string;
  idempotency_key: string;
}

export interface ProcedureV2ScheduleReceipt {
  status: "scheduled" | "paused" | "revoked" | "blocked" | "unknown" | string;
  scheduled_job_id: string;
  binding_id: string;
  revision: number;
  action_type: "guardian.run_procedure.v2";
  routine_id: string;
  version: number;
  template_id: ProcedureV2TemplateId;
  goal_id: string;
  goal_revision: number;
  schedule_idempotency_key: string;
  input_digest: string;
  next_run: string | null;
  expires_at: string;
  state: string;
  pause_route: string;
  recovery_action?: string | null;
  audit_receipt_id?: string | null;
}

export interface ProcedureV2ScheduleControlRequest {
  action: "pause" | "resume";
  expected_binding_revision: number;
  idempotency_key: string;
}

export interface ProcedureV2RevokeRequest {
  expected_binding_revision: number;
  idempotency_key: string;
  reason: string;
}

export interface ProcedureV2ErrorDetail {
  code: string;
  message: string;
  recovery_action: string | null;
  retryable: boolean;
  binding_id: string | null;
  audit_receipt_id: string | null;
}

export class ProcedureV2ApiError extends Error {
  readonly status: number;
  readonly detail: ProcedureV2ErrorDetail;

  constructor(status: number, detail: ProcedureV2ErrorDetail) {
    super(detail.message);
    this.name = "ProcedureV2ApiError";
    this.status = status;
    this.detail = detail;
  }

  get code(): string { return this.detail.code; }
  get recoveryAction(): string | null { return this.detail.recovery_action; }
  get retryable(): boolean { return this.detail.retryable; }
}

export interface ProcedureV2SourceTask {
  task_id: string;
  title: string;
  status: string;
  capability_id: string | null;
  /** Returned by the authenticated board projection; absent means unverified. */
  owner_principal_id?: string | null;
  owner_session_id?: string | null;
  goal_id: string;
  goal_revision: number;
  task_revision: number;
  readback_status: string;
  verification_status: string;
}

/** Owner/session-scoped source-watch metadata used for a fresh invocation. */
export interface ProcedureV2SourceWatch {
  id: string;
  /** Returned by the authenticated watch projection; absent means unverified. */
  owner_principal_id?: string | null;
  owner_session_id?: string | null;
  goal_id: string;
  goal_revision: number;
  plan_revision: number;
  state: string;
  last_status: string | null;
}

const TEMPLATE_IDS = new Set<ProcedureV2TemplateId>([
  "public-browser-check",
  "watch-and-public-browser",
  "selected-meeting-prep",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function stringValue(value: unknown, field: string): string {
  if (typeof value !== "string" || !value.trim()) throw new Error(`Procedure response has invalid ${field}.`);
  return value;
}

function optionalOwnerValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function abortError(): Error {
  if (typeof DOMException === "function") return new DOMException("The governed procedure request was aborted.", "AbortError");
  const error = new Error("The governed procedure request was aborted.");
  error.name = "AbortError";
  return error;
}

function nullableStringValue(value: unknown, field: string): string | null {
  if (value === null || value === undefined) return null;
  return stringValue(value, field);
}

function integerValue(value: unknown, field: string, minimum = 1): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < minimum) throw new Error(`Procedure response has invalid ${field}.`);
  return value;
}

function templateValue(value: unknown): ProcedureV2TemplateId {
  if (typeof value !== "string" || !TEMPLATE_IDS.has(value as ProcedureV2TemplateId)) throw new Error("Procedure response has an unknown template.");
  return value as ProcedureV2TemplateId;
}

const FIXED_PLAN_CONTRACT: Record<ProcedureV2TemplateId, {
  steps: Array<{ step_id: string; capability_id: string; capability_version: string }>;
  parameters: Array<{ name: string; kind: string; required: boolean }>;
  permissions: string[];
}> = {
  "public-browser-check": {
    steps: [{ step_id: "public_browser_check", capability_id: "browser.public-task.v1", capability_version: "1" }],
    parameters: [
      { name: "goal_id", kind: "goal_id", required: true },
      { name: "expected_goal_revision", kind: "goal_revision", required: true },
    ],
    permissions: ["browser.public-task.v1"],
  },
  "watch-and-public-browser": {
    steps: [
      { step_id: "source_watch", capability_id: "guardian.research-watch.v1", capability_version: "1" },
      { step_id: "public_browser_check", capability_id: "browser.public-task.v1", capability_version: "1" },
    ],
    parameters: [
      { name: "goal_id", kind: "goal_id", required: true },
      { name: "expected_goal_revision", kind: "goal_revision", required: true },
      { name: "source_watch_id", kind: "source_watch_id", required: true },
      { name: "expected_watch_revision", kind: "watch_revision", required: true },
    ],
    permissions: ["guardian.research-watch.v1", "browser.public-task.v1"],
  },
  "selected-meeting-prep": {
    steps: [{ step_id: "selected_meeting_prep", capability_id: "calendar.meeting-prep.v1", capability_version: "1" }],
    parameters: [
      { name: "schema_version", kind: "schema_version", required: true },
      { name: "consent_id", kind: "consent_id", required: true },
      { name: "event_binding_id", kind: "event_binding_id", required: true },
      { name: "expected_event_binding_revision", kind: "event_binding_revision", required: true },
      { name: "expected_consent_revision", kind: "consent_revision", required: true },
      { name: "expected_connection_revision", kind: "connection_revision", required: true },
      { name: "event_revision", kind: "event_revision", required: true },
      { name: "calendar_list_revision", kind: "calendar_list_revision", required: true },
      { name: "goal_id", kind: "goal_id", required: true },
      { name: "goal_revision", kind: "goal_revision", required: true },
      { name: "purpose", kind: "purpose", required: true },
    ],
    permissions: ["calendar.meeting-prep.v1"],
  },
};

function isPositiveInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 1;
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

/**
 * Infer and validate the exact typed parameter envelope from the registered
 * template. This deliberately uses the request body rather than the active
 * selector in the component so a remounted reconciliation cannot be paired
 * with a stale local template choice.
 */
function validateTypedProcedureParameters(value: unknown): ProcedureV2TemplateId {
  if (!isRecord(value)) throw new Error("The procedure request has no typed parameter envelope.");
  const keys = Object.keys(value);
  for (const [templateId, contract] of Object.entries(FIXED_PLAN_CONTRACT) as Array<[ProcedureV2TemplateId, (typeof FIXED_PLAN_CONTRACT)[ProcedureV2TemplateId]]>) {
    const expectedKeys = contract.parameters.map((parameter) => parameter.name);
    if (keys.length !== expectedKeys.length || keys.some((key, index) => key !== expectedKeys[index])) continue;
    for (const parameter of contract.parameters) {
      const parameterValue = value[parameter.name];
      if (parameter.kind === "schema_version") {
        if (parameterValue !== 1) throw new Error("The procedure request has an invalid schema version.");
      } else if (new Set([
        "goal_revision",
        "watch_revision",
        "event_binding_revision",
        "consent_revision",
        "connection_revision",
      ]).has(parameter.kind)) {
        if (!isPositiveInteger(parameterValue)) throw new Error(`The procedure request has an invalid ${parameter.name}.`);
      } else if (parameter.kind === "purpose") {
        if (!isNonEmptyString(parameterValue) || parameterValue.length > 500) throw new Error("The procedure request has an invalid purpose.");
      } else if (!isNonEmptyString(parameterValue)) {
        throw new Error(`The procedure request has an invalid ${parameter.name}.`);
      }
    }
    return templateId;
  }
  throw new Error("The procedure request does not match a registered typed template.");
}

function requestGoalRevision(parameters: ProcedureV2InvokeParameters, templateId: ProcedureV2TemplateId): number {
  const revision = templateId === "selected-meeting-prep"
    ? (parameters as ProcedureV2MeetingParameters).goal_revision
    : (parameters as ProcedureV2PublicParameters).expected_goal_revision;
  if (!isPositiveInteger(revision)) throw new Error("The procedure request has no valid goal revision.");
  return revision;
}

function validateParameter(value: unknown): ProcedureV2ParameterSpec {
  if (!isRecord(value)) throw new Error("Procedure response has an invalid parameter schema.");
  if (typeof value.required !== "boolean") throw new Error("Procedure response has an invalid parameter requirement.");
  return {
    name: stringValue(value.name, "parameter name"),
    kind: stringValue(value.kind, "parameter kind"),
    required: value.required,
  };
}

function validatePlan(value: unknown): ProcedureV2Plan {
  if (!isRecord(value) || value.schema_version !== 2 || !TEMPLATE_IDS.has(value.template_id as ProcedureV2TemplateId)
    || value.verifier !== "leaf_readbacks" || !Array.isArray(value.steps) || !Array.isArray(value.parameters)
    || !isRecord(value.limits) || value.limits.max_steps !== 2 || value.limits.max_total_seconds !== 300
    || value.steps.length < 1 || value.steps.length > 2) {
    throw new Error("Procedure response has an invalid fixed v2 plan.");
  }
  const templateId = templateValue(value.template_id);
  const contract = FIXED_PLAN_CONTRACT[templateId];
  if (value.steps.length !== contract.steps.length || value.parameters.length !== contract.parameters.length) {
    throw new Error("Procedure response does not match the registered fixed template.");
  }
  const steps = value.steps.map((step) => {
    if (!isRecord(step)) throw new Error("Procedure response has an invalid plan step.");
    return {
      step_id: stringValue(step.step_id, "step id"),
      capability_id: stringValue(step.capability_id, "capability id"),
      capability_version: stringValue(step.capability_version, "capability version"),
      typed_input_ref: stringValue(step.typed_input_ref, "typed input ref"),
      typed_input_digest: stringValue(step.typed_input_digest, "typed input digest"),
    };
  });
  steps.forEach((step, index) => {
    const expected = contract.steps[index];
    if (step.step_id !== expected.step_id || step.capability_id !== expected.capability_id || step.capability_version !== expected.capability_version
      || !/^[A-Za-z0-9_.:/-]{1,512}$/.test(step.typed_input_ref)
      || step.typed_input_ref.split("/").some((part) => part === "" || part === "." || part === "..")
      || !/^[0-9a-f]{64}$/i.test(step.typed_input_digest)) {
      throw new Error("Procedure response does not match the registered fixed step contract.");
    }
  });
  const parameters = value.parameters.map(validateParameter);
  parameters.forEach((parameter, index) => {
    const expected = contract.parameters[index];
    if (parameter.name !== expected.name || parameter.kind !== expected.kind || parameter.required !== expected.required) {
      throw new Error("Procedure response does not match the registered fixed parameter contract.");
    }
  });
  return {
    schema_version: 2,
    template_id: templateId,
    steps,
    parameters,
    verifier: "leaf_readbacks",
    limits: { max_steps: 2, max_total_seconds: 300 },
  };
}

function validateSourceRef(value: unknown): ProcedureV2SourceRef {
  if (!isRecord(value) || !Array.isArray(value.artifact_ids_and_hashes)) throw new Error("Procedure response has an invalid source proof.");
  return {
    task_id: stringValue(value.task_id, "source task id"),
    task_revision: integerValue(value.task_revision, "source task revision"),
    attempt_id: nullableStringValue(value.attempt_id, "source attempt id"),
    job_id: nullableStringValue(value.job_id, "source job id"),
    artifact_ids_and_hashes: value.artifact_ids_and_hashes.map((item) => {
      if (!isRecord(item)) throw new Error("Procedure response has an invalid source artifact proof.");
      const sha256 = stringValue(item.sha256, "source artifact digest");
      if (!/^[0-9a-f]{64}$/i.test(sha256)) throw new Error("Procedure response has an invalid source artifact digest.");
      return { artifact_id: stringValue(item.artifact_id, "source artifact id"), sha256 };
    }),
    capability_id: stringValue(value.capability_id, "source capability id"),
    capability_version: stringValue(value.capability_version, "source capability version"),
    goal_id: stringValue(value.goal_id, "source goal id"),
    goal_revision: integerValue(value.goal_revision, "source goal revision"),
  };
}

function validatePreview(value: unknown): ProcedureV2Preview {
  if (!isRecord(value) || value.status !== "preview" || !Array.isArray(value.source_refs)
    || !Array.isArray(value.permissions) || !isRecord(value.limits)) throw new Error("Procedure response has an invalid v2 preview.");
  if (value.limits.max_steps !== 2 || value.limits.max_total_seconds !== 300) throw new Error("Procedure preview exceeds the fixed v2 limits.");
  const templateId = templateValue(value.template_id);
  const plan = validatePlan(value.plan);
  if (plan.template_id !== templateId) throw new Error("Procedure preview plan does not match its template.");
  const contract = FIXED_PLAN_CONTRACT[templateId];
  if (value.source_refs.length !== contract.steps.length || !Array.isArray(value.parameter_schema)) {
    throw new Error("Procedure preview does not contain the exact registered source and parameter schema.");
  }
  const parameterSchema = value.parameter_schema.map(validateParameter);
  if (parameterSchema.length !== plan.parameters.length || parameterSchema.some((parameter, index) => {
    const expected = plan.parameters[index];
    return parameter.name !== expected.name || parameter.kind !== expected.kind || parameter.required !== expected.required;
  })) {
    throw new Error("Procedure preview parameter schema does not match its fixed plan.");
  }
  const sourceRefs = value.source_refs.map(validateSourceRef);
  if (sourceRefs.some((source, index) => source.capability_id !== contract.steps[index].capability_id || source.capability_version !== contract.steps[index].capability_version)) {
    throw new Error("Procedure preview source proof does not match its fixed plan.");
  }
  const permissions = value.permissions.map((permission) => stringValue(permission, "permission"));
  if (permissions.length !== contract.permissions.length || permissions.some((permission, index) => permission !== contract.permissions[index])) {
    throw new Error("Procedure preview permissions do not match its fixed template.");
  }
  return {
    status: "preview",
    template_id: templateId,
    preview_digest: stringValue(value.preview_digest, "preview digest"),
    expires_at: stringValue(value.expires_at, "preview expiry"),
    plan,
    source_refs: sourceRefs,
    parameter_schema: parameterSchema,
    permissions,
    limits: { max_steps: 2, max_total_seconds: 300 },
    version_diff: value.version_diff === null || value.version_diff === undefined ? null : (isRecord(value.version_diff) ? value.version_diff : null),
  };
}

function validatePrepared(value: unknown): ProcedureV2Prepared {
  if (!isRecord(value) || (value.status !== "prepared" && value.status !== "blocked")) throw new Error("Procedure response has an invalid prepared v2 binding.");
  const installJobId = nullableStringValue(value.install_job_id, "install job id");
  const approvalId = nullableStringValue(value.approval_id, "approval id");
  const hasInstallApprovalStatus = Object.prototype.hasOwnProperty.call(value, "install_approval_status");
  const hasInstallApprovalExpiry = Object.prototype.hasOwnProperty.call(value, "install_approval_expires_at");
  if (value.status === "prepared" && (!installJobId || !approvalId || typeof value.routine_id !== "string" || !value.routine_id.trim()
    || typeof value.version_id !== "string" || !value.version_id.trim() || typeof value.version !== "number"
    || !hasInstallApprovalStatus || !hasInstallApprovalExpiry || typeof value.install_approval_status !== "string"
    || !value.install_approval_status.trim())) {
    throw new Error("Prepared procedure is missing its exact install approval binding.");
  }
  const routineId = value.status === "prepared" ? stringValue(value.routine_id, "routine id") : undefined;
  const versionId = value.status === "prepared" ? stringValue(value.version_id, "version id") : undefined;
  const version = value.status === "prepared" ? integerValue(value.version, "version") : undefined;
  return {
    status: value.status,
    binding_id: stringValue(value.binding_id, "binding id"),
    routine_id: routineId,
    version_id: versionId,
    version,
    schema_version: 2,
    template_id: templateValue(value.template_id),
    revision: integerValue(value.revision, "binding revision"),
    request_digest: stringValue(value.request_digest, "request digest"),
    preview_digest: stringValue(value.preview_digest, "preview digest"),
    preview_expires_at: stringValue(value.preview_expires_at, "preview expiry"),
    plan: value.plan ? validatePlan(value.plan) : undefined,
    source_refs: Array.isArray(value.source_refs) ? value.source_refs.map(validateSourceRef) : undefined,
    package_state: typeof value.package_state === "string" ? value.package_state : undefined,
    routine_state: typeof value.routine_state === "string" ? value.routine_state : undefined,
    install_job_id: installJobId,
    approval_id: approvalId,
    install_approval_status: nullableStringValue(value.install_approval_status, "install approval status"),
    install_approval_expires_at: nullableStringValue(value.install_approval_expires_at, "install approval expiry"),
    install_recovery_action: nullableStringValue(value.install_recovery_action, "install recovery action"),
    audit_receipt_id: nullableStringValue(value.audit_receipt_id, "audit receipt id"),
    recovery_action: nullableStringValue(value.recovery_action, "recovery action"),
  };
}

function validateInvoke(value: unknown): ProcedureV2InvokeReceipt {
  if (!isRecord(value) || value.schema_version !== 2) throw new Error("Procedure response has an invalid invocation receipt.");
  return {
    status: stringValue(value.status, "invocation status") as ProcedureV2InvokeReceipt["status"],
    routine_id: stringValue(value.routine_id, "routine id"),
    version: integerValue(value.version, "version"),
    schema_version: 2,
    template_id: templateValue(value.template_id),
    invocation_uuid: stringValue(value.invocation_uuid, "invocation id"),
    scope: stringValue(value.scope, "invocation scope"),
    goal_id: stringValue(value.goal_id, "goal id"),
    goal_revision: integerValue(value.goal_revision, "goal revision"),
    task_id: stringValue(value.task_id, "task id"),
    attempt_id: nullableStringValue(value.attempt_id, "attempt id"),
    job_id: nullableStringValue(value.job_id, "job id"),
    input_artifact_id: stringValue(value.input_artifact_id, "input artifact id"),
    input_digest: stringValue(value.input_digest, "input digest"),
    plan_digest: stringValue(value.plan_digest, "plan digest"),
    revision: integerValue(value.revision, "routine revision"),
    audit_receipt_id: nullableStringValue(value.audit_receipt_id, "audit receipt id"),
    recovery_action: nullableStringValue(value.recovery_action, "recovery action"),
  };
}

function validateSchedule(value: unknown): ProcedureV2ScheduleReceipt {
  if (!isRecord(value)) throw new Error("Procedure response has an invalid schedule receipt.");
  if (value.action_type !== "guardian.run_procedure.v2") throw new Error("Procedure schedule has an unexpected action type.");
  return {
    status: stringValue(value.status, "schedule status") as ProcedureV2ScheduleReceipt["status"],
    scheduled_job_id: stringValue(value.scheduled_job_id, "scheduled job id"),
    binding_id: stringValue(value.binding_id, "schedule binding id"),
    revision: integerValue(value.revision, "schedule revision"),
    action_type: "guardian.run_procedure.v2",
    routine_id: stringValue(value.routine_id, "routine id"),
    version: integerValue(value.version, "version"),
    template_id: templateValue(value.template_id),
    goal_id: stringValue(value.goal_id, "goal id"),
    goal_revision: integerValue(value.goal_revision, "goal revision"),
    schedule_idempotency_key: stringValue(value.schedule_idempotency_key, "schedule idempotency key"),
    input_digest: stringValue(value.input_digest, "schedule input digest"),
    next_run: nullableStringValue(value.next_run, "next run"),
    expires_at: stringValue(value.expires_at, "schedule expiry"),
    state: stringValue(value.state, "schedule state"),
    pause_route: stringValue(value.pause_route, "schedule pause route"),
    recovery_action: nullableStringValue(value.recovery_action, "recovery action"),
    audit_receipt_id: nullableStringValue(value.audit_receipt_id, "audit receipt id"),
  };
}

function invalidProcedureResponse(message: string): never {
  throw new ProcedureV2ApiError(502, {
    code: "procedure_response_invalid",
    message,
    recovery_action: "reconcile_existing_effect",
    retryable: true,
    binding_id: null,
    audit_receipt_id: null,
  });
}

function assertRequestIdentity(request: ProcedureV2InvokeRequest | ProcedureV2ScheduleRequest): ProcedureV2TemplateId {
  if (!isPositiveInteger(request.version) || !isPositiveInteger(request.expected_routine_revision)) {
    invalidProcedureResponse("The procedure response cannot be correlated to an invalid request version.");
  }
  if (!isNonEmptyString(request.goal_id) || !isPositiveInteger(request.expected_goal_revision)) {
    invalidProcedureResponse("The procedure response cannot be correlated to an invalid goal binding.");
  }
  const templateId = validateTypedProcedureParameters(request.parameters);
  const nestedGoalId = request.parameters.goal_id;
  const nestedGoalRevision = requestGoalRevision(request.parameters, templateId);
  if (nestedGoalId !== request.goal_id || nestedGoalRevision !== request.expected_goal_revision) {
    invalidProcedureResponse("The procedure request has inconsistent outer and typed goal identity.");
  }
  return templateId;
}

/**
 * Correlate a parsed invocation receipt with the exact request and server-owned
 * route/version. Callers must provide the persisted version id for scope; a
 * numeric version alone is not an equivalent identity.
 */
export function correlateInvocationReceipt(
  receipt: ProcedureV2InvokeReceipt,
  request: ProcedureV2InvokeRequest,
  routineId: string,
  versionId: string | null | undefined,
): ProcedureV2InvokeReceipt {
  const templateId = assertRequestIdentity(request);
  if (!isNonEmptyString(routineId) || !isNonEmptyString(versionId)) {
    invalidProcedureResponse("The invocation receipt cannot be correlated without the server-owned routine version.");
  }
  if (receipt.schema_version !== 2
    || receipt.routine_id !== routineId
    || receipt.version !== request.version
    || receipt.template_id !== templateId
    || receipt.invocation_uuid !== request.invocation_uuid
    || receipt.goal_id !== request.goal_id
    || receipt.goal_revision !== request.expected_goal_revision
    || receipt.scope !== `procedure-v2:${routineId}:${versionId}`) {
    invalidProcedureResponse("The invocation receipt does not match the exact governed request.");
  }
  return receipt;
}

/** Correlate a parsed finite schedule receipt with its exact creation request. */
export function correlateScheduleReceipt(
  receipt: ProcedureV2ScheduleReceipt,
  request: ProcedureV2ScheduleRequest,
  routineId: string,
): ProcedureV2ScheduleReceipt {
  const templateId = assertRequestIdentity(request);
  if (!isPublicSchedulableProcedure(templateId)) {
    invalidProcedureResponse("The schedule receipt uses a template that is not schedulable.");
  }
  if (!isNonEmptyString(routineId)
    || receipt.routine_id !== routineId
    || receipt.version !== request.version
    || receipt.template_id !== templateId
    || receipt.goal_id !== request.goal_id
    || receipt.goal_revision !== request.expected_goal_revision
    || receipt.schedule_idempotency_key !== request.idempotency_key
    || receipt.action_type !== "guardian.run_procedure.v2"
    || !isNonEmptyString(receipt.binding_id)
    || !isNonEmptyString(receipt.scheduled_job_id)) {
    invalidProcedureResponse("The schedule receipt does not match the exact governed request.");
  }
  return receipt;
}

function validateRoutine(value: unknown): ProcedureV2Routine {
  if (!isRecord(value) || !Array.isArray(value.versions) || typeof value.id !== "string"
    || typeof value.name !== "string" || typeof value.state !== "string"
    || typeof value.revision !== "number" || !Number.isSafeInteger(value.revision)
    || !isRecord(value.package) || typeof value.package.status !== "string") {
    throw new Error("Procedure response has an invalid routine.");
  }
  const routineId = stringValue(value.id, "routine id");
  const ownerPrincipalId = stringValue(value.owner_principal_id, "routine owner principal id");
  const versions = value.versions.map((candidate) => {
    if (!isRecord(candidate) || typeof candidate.id !== "string" || typeof candidate.routine_id !== "string"
      || typeof candidate.version !== "number" || !Number.isSafeInteger(candidate.version)
      || typeof candidate.workflow_sha256 !== "string" || typeof candidate.runbook_sha256 !== "string"
      || !isRecord(candidate.source_provenance) || typeof candidate.created_at !== "string") {
      throw new Error("Procedure response has an invalid routine version.");
    }
    if (candidate.routine_id !== routineId) {
      throw new Error("Procedure response has a routine version bound to a different routine.");
    }
    return {
      id: candidate.id,
      routine_id: candidate.routine_id,
      version: candidate.version,
      workflow_sha256: candidate.workflow_sha256,
      runbook_sha256: candidate.runbook_sha256,
      installed_package_digest: candidate.installed_package_digest === null || candidate.installed_package_digest === undefined ? null : stringValue(candidate.installed_package_digest, "installed package digest"),
      source_provenance: candidate.source_provenance,
      source_repository: candidate.source_repository === null || candidate.source_repository === undefined ? null : stringValue(candidate.source_repository, "source repository"),
      source_action: candidate.source_action === null || candidate.source_action === undefined ? null : stringValue(candidate.source_action, "source action"),
      source_issue_number: candidate.source_issue_number === null || candidate.source_issue_number === undefined ? null : integerValue(candidate.source_issue_number, "source issue number", 0),
      created_at: candidate.created_at,
      installed_at: candidate.installed_at === null || candidate.installed_at === undefined ? null : stringValue(candidate.installed_at, "installed timestamp"),
      schema_version: typeof candidate.schema_version === "number" ? candidate.schema_version : undefined,
      template_id: candidate.template_id === undefined || candidate.template_id === null ? undefined : templateValue(candidate.template_id),
      binding_id: candidate.binding_id === null || candidate.binding_id === undefined ? null : stringValue(candidate.binding_id, "binding id"),
      plan_digest: candidate.plan_digest === null || candidate.plan_digest === undefined ? null : stringValue(candidate.plan_digest, "plan digest"),
      source_refs: Array.isArray(candidate.source_refs) ? candidate.source_refs.map(validateSourceRef) : undefined,
      parameter_schema: Array.isArray(candidate.parameter_schema) ? candidate.parameter_schema.map(validateParameter) : undefined,
      procedure_binding: candidate.procedure_binding === null || candidate.procedure_binding === undefined
        ? undefined
        : validateBindingMetadata(candidate.procedure_binding),
    } satisfies ProcedureV2RoutineVersion;
  });
  return {
    id: routineId,
    owner_principal_id: ownerPrincipalId,
    state: value.state,
    revision: value.revision,
    current_version: value.current_version === null || value.current_version === undefined ? null : integerValue(value.current_version, "current routine version"),
    name: value.name,
    versions,
    package: {
      status: value.package.status,
      digest: value.package.digest === null || value.package.digest === undefined ? null : stringValue(value.package.digest, "package digest"),
      review_id: value.package.review_id === null || value.package.review_id === undefined ? null : stringValue(value.package.review_id, "package review id"),
      reason: value.package.reason === null || value.package.reason === undefined ? null : stringValue(value.package.reason, "package reason"),
    },
    schema_version: typeof value.schema_version === "number" ? value.schema_version : undefined,
    template_id: value.template_id === undefined || value.template_id === null ? undefined : templateValue(value.template_id),
    binding_id: value.binding_id === null || value.binding_id === undefined ? null : stringValue(value.binding_id, "binding id"),
    plan_digest: value.plan_digest === null || value.plan_digest === undefined ? null : stringValue(value.plan_digest, "plan digest"),
    source_refs: Array.isArray(value.source_refs) ? value.source_refs.map(validateSourceRef) : undefined,
    parameter_schema: Array.isArray(value.parameter_schema) ? value.parameter_schema.map(validateParameter) : undefined,
  };
}

function isSuccessfulLifecycleReceipt(value: unknown, routineId: string, action: "pause" | "revoke"): boolean {
  return isRecord(value)
    && value.status === (action === "pause" ? "paused" : "revoked")
    && value.routine_id === routineId
    && typeof value.reason === "string";
}

function stringArray(value: unknown, field: string): string[] {
  if (!Array.isArray(value)) throw new Error(`Procedure response has invalid ${field}.`);
  return value.map((item) => stringValue(item, field));
}

function booleanValue(value: unknown, field: string): boolean {
  if (typeof value !== "boolean") throw new Error(`Procedure response has invalid ${field}.`);
  return value;
}

function validatePackagePreview(value: unknown): WorkBoardRoutinePackagePreview {
  if (!isRecord(value) || !isRecord(value.manifest) || !isRecord(value.manifest.authority)
    || !isRecord(value.manifest.resources) || !isRecord(value.manifest.data_policy)
    || !isRecord(value.runbook) || !isRecord(value.runbook.procedure) || !isRecord(value.runbook.bindings)) {
    throw new Error("Procedure response has an invalid package preview.");
  }
  const authority = value.manifest.authority;
  const resources = value.manifest.resources;
  const dataPolicy = value.manifest.data_policy;
  const procedure = value.runbook.procedure;
  const bindings = value.runbook.bindings;
  if (!Array.isArray(procedure.steps) || (procedure.schema_version !== 1 && procedure.schema_version !== 2)) throw new Error("Procedure package has invalid fixed steps.");
  const procedureSchema = procedure.schema_version;
  const procedureCapability = stringValue(procedure.capability_id, "package capability id");
  const procedureTemplate = procedureSchema === 2 ? templateValue(procedure.template_id) : undefined;
  const steps = procedure.steps.map((step) => {
    if (!isRecord(step)) throw new Error("Procedure package has an invalid step.");
    if (procedureSchema === 2) {
      return {
        id: stringValue(step.id, "package step id"),
        capability_id: stringValue(step.capability_id, "package step capability id"),
        capability_version: stringValue(step.capability_version, "package step capability version"),
      };
    }
    return {
      id: stringValue(step.id, "package step id"),
      capability: stringValue(step.capability, "package step capability"),
      tool: stringValue(step.tool, "package step tool"),
    };
  });
  if (procedureSchema === 2) {
    const contract = FIXED_PLAN_CONTRACT[procedureTemplate!];
    if (procedureCapability !== "guardian-routine.v2" || steps.length !== contract.steps.length || steps.some((step, index) => {
      const expected = contract.steps[index];
      return step.id !== expected.step_id || step.capability_id !== expected.capability_id || step.capability_version !== expected.capability_version;
    })) throw new Error("Procedure package does not match its registered v2 step contract.");
  }
  const workflowDigest = stringValue(bindings.workflow_sha256, "package workflow digest");
  const sourceProvenanceDigest = stringValue(bindings.source_provenance_sha256, "package provenance digest");
  const runbookDigest = procedureSchema === 2
    ? stringValue(bindings.runbook_sha256, "package runbook digest")
    : stringValue(bindings.legacy_runbook_sha256, "package runbook digest");
  const planDigest = procedureSchema === 2 ? stringValue(bindings.plan_digest, "package plan digest") : undefined;
  const procedurePlanDigest = procedureSchema === 2 ? stringValue(procedure.plan_digest, "procedure plan digest") : undefined;
  if (procedureSchema === 2 && procedurePlanDigest !== planDigest) throw new Error("Procedure package plan digest does not match its binding.");
  return {
    routine_id: stringValue(value.routine_id, "package routine id"),
    version: integerValue(value.version, "package version"),
    pack_id: stringValue(value.pack_id, "package id"),
    digest: stringValue(value.digest, "package digest"),
    installed_package_digest: nullableStringValue(value.installed_package_digest, "installed package digest"),
    review_id: nullableStringValue(value.review_id, "package review id"),
    status: stringValue(value.status, "package status"),
    manifest: {
      schema_version: typeof value.manifest.schema_version === "number" ? value.manifest.schema_version : undefined,
      display_name: stringValue(value.manifest.display_name, "package display name"),
      summary: stringValue(value.manifest.summary, "package summary"),
      version: stringValue(value.manifest.version, "package manifest version"),
      authority: {
        tools: stringArray(authority.tools, "package tools"),
        filesystem: stringArray(authority.filesystem, "package filesystem"),
        network: booleanValue(authority.network, "package network authority"),
        secrets: stringArray(authority.secrets, "package secrets"),
        approval: stringValue(authority.approval, "package approval"),
      },
      resources: {
        max_runtime_seconds: integerValue(resources.max_runtime_seconds, "package runtime", 0),
        max_artifact_bytes: integerValue(resources.max_artifact_bytes, "package artifact bytes", 0),
        max_inference_cost_microusd: integerValue(resources.max_inference_cost_microusd, "package inference budget", 0),
        inference_priority: stringValue(resources.inference_priority, "package inference priority"),
      },
      data_policy: { classes: stringArray(dataPolicy.classes, "package data classes"), egress: stringArray(dataPolicy.egress, "package data egress") },
    },
    runbook: {
      title: stringValue(value.runbook.title, "package runbook title"),
      summary: stringValue(value.runbook.summary, "package runbook summary"),
      procedure: {
        schema_version: procedureSchema,
        capability_id: procedureCapability,
        template_id: procedureTemplate,
        plan_digest: procedurePlanDigest,
        steps,
      },
      bindings: {
        workflow_sha256: workflowDigest,
        ...(procedureSchema === 2 ? { runbook_sha256: runbookDigest, plan_digest: planDigest } : { legacy_runbook_sha256: runbookDigest }),
        source_provenance_sha256: sourceProvenanceDigest,
      },
    },
  };
}

function validatePackageMutation(value: unknown): { digest: string; status: string } {
  if (!isRecord(value)) throw new Error("Procedure response has an invalid package mutation receipt.");
  return { digest: stringValue(value.digest, "package digest"), status: stringValue(value.status, "package status") };
}

function validatePackageReview(value: unknown): { digest: string; review: { review_id: string; status: string } } {
  if (!isRecord(value) || !isRecord(value.review)) throw new Error("Procedure response has an invalid package review receipt.");
  return { digest: stringValue(value.digest, "package digest"), review: { review_id: stringValue(value.review.review_id, "package review id"), status: stringValue(value.review.status, "package review status") } };
}

function validatePackageApproval(value: unknown): { digest: string; approval: WorkBoardRoutinePackageApproval } {
  if (!isRecord(value) || !isRecord(value.approval)) throw new Error("Procedure response has an invalid package approval receipt.");
  const approval = value.approval;
  const status = approval.status;
  if (status !== "pending" && status !== "approved" && status !== "denied" && status !== "expired" && status !== "consumed") throw new Error("Procedure response has an invalid package approval status.");
  return {
    digest: stringValue(value.digest, "package digest"),
    approval: {
      approval_id: stringValue(approval.approval_id, "approval id"),
      status,
      action: stringValue(approval.action, "approval action"),
      pack_id: stringValue(approval.pack_id, "approval package id"),
      version: stringValue(approval.version, "approval version"),
      digest: stringValue(approval.digest, "approval digest"),
      goal_id: stringValue(approval.goal_id, "approval goal id"),
      expires_at: approval.expires_at === null || approval.expires_at === undefined ? null : stringValue(approval.expires_at, "approval expiry"),
    },
  };
}

function validateBindingMetadata(value: unknown): ProcedureV2BindingMetadata {
  if (!isRecord(value)) throw new Error("Procedure response has an invalid procedure binding.");
  if (!Object.prototype.hasOwnProperty.call(value, "install_approval_status")
    || !Object.prototype.hasOwnProperty.call(value, "install_approval_expires_at")
    || typeof value.install_approval_status !== "string"
    || !value.install_approval_status.trim()) {
    throw new Error("Procedure response has incomplete install approval metadata.");
  }
  const revision = value.revision === null || value.revision === undefined ? null : integerValue(value.revision, "procedure binding revision");
  return {
    binding_id: nullableStringValue(value.binding_id, "procedure binding id"),
    state: stringValue(value.state, "procedure binding state"),
    revision,
    preview_digest: nullableStringValue(value.preview_digest, "procedure preview digest"),
    preview_expires_at: nullableStringValue(value.preview_expires_at, "procedure preview expiry"),
    install_job_id: nullableStringValue(value.install_job_id, "procedure install job id"),
    approval_id: nullableStringValue(value.approval_id, "procedure approval id"),
    install_approval_status: nullableStringValue(value.install_approval_status, "procedure install approval status"),
    install_approval_expires_at: nullableStringValue(value.install_approval_expires_at, "procedure install approval expiry"),
    install_recovery_action: nullableStringValue(value.install_recovery_action, "procedure install recovery action"),
  };
}

function parseError(status: number, payload: unknown): ProcedureV2ApiError {
  const detail = isRecord(payload) && isRecord(payload.detail) ? payload.detail : isRecord(payload) ? payload : {};
  const code = typeof detail.code === "string" && detail.code.trim() ? detail.code.trim() : `http_${status}`;
  const message = typeof detail.message === "string" && detail.message.trim()
    ? detail.message.trim().slice(0, 500)
    : "The governed procedure request failed.";
  return new ProcedureV2ApiError(status, {
    code,
    message,
    recovery_action: typeof detail.recovery_action === "string" ? detail.recovery_action.slice(0, 500) : null,
    retryable: detail.retryable === true,
    binding_id: typeof detail.binding_id === "string" ? detail.binding_id : null,
    audit_receipt_id: typeof detail.audit_receipt_id === "string" ? detail.audit_receipt_id : null,
  });
}

async function requestJson<T>(path: string, init: RequestInit = {}, validate: (value: unknown) => T): Promise<T> {
  const timeoutMs = 15_000;
  const callerSignal = init.signal;
  if (callerSignal?.aborted) {
    throw abortError();
  }
  const controller = new AbortController();
  let timeout: ReturnType<typeof setTimeout> | undefined;
  const request = (async () => {
    const response = await apiFetch(`${API_URL}${path}`, { ...init, signal: controller.signal });
    const payload = await response.json().catch((cause) => {
      if (controller.signal.aborted) throw cause;
      return null;
    });
    return { response, payload };
  })();
  let rejectBounded: (reason?: unknown) => void = () => undefined;
  const boundedRequest = new Promise<{ response: Response; payload: unknown }>((resolve, reject) => {
    rejectBounded = reject;
    timeout = setTimeout(() => {
      controller.abort();
      reject(new ProcedureV2ApiError(504, {
        code: "procedure_request_timeout",
        message: "The governed procedure request timed out before its response was fully read.",
        recovery_action: "retry_exact_request",
        retryable: true,
        binding_id: null,
        audit_receipt_id: null,
      }));
    }, timeoutMs);
    // A timed-out fetch/body may ignore AbortController and settle later. The
    // handlers keep that late settlement from becoming an unhandled rejection.
    request.then(resolve, reject);
  });
  const abortFromCaller = () => {
    controller.abort();
    rejectBounded(abortError());
  };
  callerSignal?.addEventListener("abort", abortFromCaller, { once: true });
  try {
    const { response, payload } = await boundedRequest;
    if (!response.ok) throw parseError(response.status, payload);
    try {
      return validate(payload);
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : "The governed procedure response was malformed.";
      throw new ProcedureV2ApiError(response.status, {
        code: "procedure_response_invalid",
        message,
        recovery_action: "refresh_procedure",
        retryable: true,
        binding_id: null,
        audit_receipt_id: null,
      });
    }
  } finally {
    if (timeout !== undefined) clearTimeout(timeout);
    callerSignal?.removeEventListener("abort", abortFromCaller);
  }
}

function jsonRequest(body: unknown): RequestInit {
  return { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

function routinePath(routineId: string): string {
  return `/api/capabilities/routines/${encodeURIComponent(routineId)}`;
}

export const procedureV2Api = {
  listSourceTasks(signal?: AbortSignal): Promise<ProcedureV2SourceTask[]> {
    return requestJson(`/api/work-board/tasks?limit=100`, { signal }, (payload) => {
      const values = isRecord(payload) && Array.isArray(payload.tasks) ? payload.tasks : Array.isArray(payload) ? payload : [];
      return values.flatMap((value): ProcedureV2SourceTask[] => {
        if (!isRecord(value) || typeof value.task_id !== "string" || typeof value.title !== "string"
          || typeof value.status !== "string" || typeof value.goal_id !== "string"
          || typeof value.goal_revision !== "number" || typeof value.task_revision !== "number") return [];
        return [{
          task_id: value.task_id,
          title: value.title,
          status: value.status,
          capability_id: typeof value.capability_id === "string" ? value.capability_id : null,
          owner_principal_id: optionalOwnerValue(value.owner_principal_id),
          owner_session_id: optionalOwnerValue(value.owner_session_id),
          goal_id: value.goal_id,
          goal_revision: value.goal_revision,
          task_revision: value.task_revision,
          readback_status: typeof value.readback_status === "string" ? value.readback_status : "unknown",
          verification_status: typeof value.verification_status === "string" ? value.verification_status : "unknown",
        }];
      });
    });
  },

  listSourceWatches(signal?: AbortSignal): Promise<ProcedureV2SourceWatch[]> {
    return requestJson("/api/capabilities/source-watches", { signal }, (payload) => {
      const values = Array.isArray(payload)
        ? payload
        : isRecord(payload) && Array.isArray(payload.watches) ? payload.watches : [];
      return values.flatMap((value): ProcedureV2SourceWatch[] => {
        if (!isRecord(value) || typeof value.id !== "string" || typeof value.goal_id !== "string"
          || typeof value.goal_revision !== "number" || !Number.isSafeInteger(value.goal_revision) || value.goal_revision < 1
          || typeof value.plan_revision !== "number" || !Number.isSafeInteger(value.plan_revision) || value.plan_revision < 1) return [];
        return [{
          id: value.id,
          owner_principal_id: optionalOwnerValue(value.owner_principal_id),
          owner_session_id: optionalOwnerValue(value.owner_session_id),
          goal_id: value.goal_id,
          goal_revision: value.goal_revision,
          plan_revision: value.plan_revision,
          state: typeof value.state === "string" ? value.state : "unknown",
          last_status: typeof value.last_status === "string" ? value.last_status : null,
        }];
      });
    });
  },

  previewFromTasks(request: {
    template_id: ProcedureV2TemplateId;
    source_tasks: ProcedureV2SourceTaskRef[];
    name: string;
    idempotency_key: string;
  }): Promise<ProcedureV2Preview> {
    return requestJson("/api/capabilities/routines/from-tasks/preview", jsonRequest(request), validatePreview);
  },

  prepareFromTasks(request: ProcedureV2PrepareRequest): Promise<ProcedureV2Prepared> {
    return requestJson("/api/capabilities/routines/from-tasks", jsonRequest(request), validatePrepared);
  },

  listRoutines(): Promise<ProcedureV2Routine[]> {
    return requestJson("/api/capabilities/routines", {}, (payload) => {
      const values = isRecord(payload) && Array.isArray(payload.routines) ? payload.routines : Array.isArray(payload) ? payload : [];
      return values.map(validateRoutine);
    });
  },

  getRoutine(routineId: string): Promise<ProcedureV2Routine> {
    return requestJson(routinePath(routineId), {}, validateRoutine);
  },

  packagePreview(routineId: string, version: number, expectedRoutineRevision: number): Promise<WorkBoardRoutinePackagePreview> {
    return requestJson(`${routinePath(routineId)}/versions/${version}/package/preview`, jsonRequest({ expected_routine_revision: expectedRoutineRevision }), validatePackagePreview);
  },

  packageReview(routineId: string, version: number, expectedRoutineRevision: number): Promise<{ digest: string; review: { review_id: string; status: string } }> {
    return requestJson(`${routinePath(routineId)}/versions/${version}/package/review`, jsonRequest({ expected_routine_revision: expectedRoutineRevision }), validatePackageReview);
  },

  preparePackageApproval(routineId: string, version: number, expectedRoutineRevision: number): Promise<{ digest: string; approval: WorkBoardRoutinePackageApproval }> {
    return requestJson(`${routinePath(routineId)}/versions/${version}/package/approvals`, jsonRequest({ expected_routine_revision: expectedRoutineRevision }), validatePackageApproval);
  },

  decidePackageApproval(routineId: string, version: number, approvalId: string, expectedRoutineRevision: number, decision: "approved" | "denied"): Promise<{ digest: string; approval: WorkBoardRoutinePackageApproval }> {
    return requestJson(`${routinePath(routineId)}/versions/${version}/package/approvals/${encodeURIComponent(approvalId)}/decision`, jsonRequest({ expected_routine_revision: expectedRoutineRevision, decision }), validatePackageApproval);
  },

  activatePackage(routineId: string, version: number, expectedRoutineRevision: number, approvalId: string): Promise<{ digest: string; status: string }> {
    return requestJson(`${routinePath(routineId)}/versions/${version}/package/activate`, jsonRequest({ expected_routine_revision: expectedRoutineRevision, approval_id: approvalId }), validatePackageMutation);
  },

  async lifecycle(routineId: string, action: "install" | "activate" | "pause" | "revoke" | "rollback", body: Record<string, unknown>): Promise<ProcedureV2Routine> {
    const payload = await requestJson(`${routinePath(routineId)}/${action}`, jsonRequest(body), (value) => value);
    try {
      return validateRoutine(payload);
    } catch (cause) {
      // The shipped pause/revoke route returns a small committed mutation
      // receipt, while install/activate/rollback return a routine projection.
      // Read back the exact owner-scoped routine after a positively identified
      // pause/revoke receipt; never repeat the mutation or accept the receipt
      // itself as the lifecycle state.
      if (action === "pause" || action === "revoke") {
        if (isSuccessfulLifecycleReceipt(payload, routineId, action)) return requestJson(routinePath(routineId), {}, validateRoutine);
      }
      const message = cause instanceof Error ? cause.message : "The governed procedure response was malformed.";
      throw new ProcedureV2ApiError(502, {
        code: "procedure_response_invalid",
        message,
        recovery_action: "refresh_procedure",
        retryable: true,
        binding_id: null,
        audit_receipt_id: null,
      });
    }
  },

  invoke(routineId: string, request: ProcedureV2InvokeRequest): Promise<ProcedureV2InvokeReceipt> {
    return requestJson(`${routinePath(routineId)}/invoke-v2`, jsonRequest(request), validateInvoke);
  },

  schedule(routineId: string, request: ProcedureV2ScheduleRequest): Promise<ProcedureV2ScheduleReceipt> {
    return requestJson(`${routinePath(routineId)}/schedule-v2`, jsonRequest(request), validateSchedule);
  },

  scheduleControl(bindingId: string, request: ProcedureV2ScheduleControlRequest): Promise<ProcedureV2ScheduleReceipt> {
    return requestJson(`/api/governed-schedules/${encodeURIComponent(bindingId)}`, { ...jsonRequest(request), method: "PATCH" }, validateSchedule);
  },

  revokeSchedule(bindingId: string, request: ProcedureV2RevokeRequest): Promise<ProcedureV2ScheduleReceipt> {
    return requestJson(`/api/governed-schedules/${encodeURIComponent(bindingId)}/revoke`, jsonRequest(request), validateSchedule);
  },
};

export function procedureTemplateLabel(templateId: ProcedureV2TemplateId): string {
  switch (templateId) {
    case "public-browser-check": return "Public browser check";
    case "watch-and-public-browser": return "Research watch → public browser";
    case "selected-meeting-prep": return "Selected meeting preparation";
  }
}

export function newProcedureRequestKey(prefix = "procedure-v2"): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

export function isPublicSchedulableProcedure(templateId: ProcedureV2TemplateId): boolean {
  return templateId === "public-browser-check" || templateId === "watch-and-public-browser";
}

export function sourceTaskFromWorkBoard(task: WorkBoardTask): ProcedureV2SourceTask {
  return {
    task_id: task.task_id,
    title: task.title,
    status: task.status,
    capability_id: task.capability_id,
    owner_principal_id: task.owner_principal_id,
    owner_session_id: task.owner_session_id,
    goal_id: task.goal_id,
    goal_revision: task.goal_revision,
    task_revision: task.task_revision,
    readback_status: task.readback_status,
    verification_status: task.verification_status,
  };
}
