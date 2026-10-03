import { API_URL } from "../config/constants";
import { apiFetch } from "./api";

const MAIL_BASE = "/api/capabilities/mail";
const GMAIL_SCOPE = "https://www.googleapis.com/auth/gmail.readonly";
const CONTROL_PATTERN = /[\u0000-\u001f\u007f]/;
const DIGEST_PATTERN = /^(?:sha256:)?[0-9a-f]{8,128}$/i;
const SAFE_ID_PATTERN = /^[A-Za-z0-9_.:@+,-]{1,256}$/;
const BODY_FIELDS = ["subject", "plainbody", "replyintent"] as const;

export type MailBodyField = (typeof BODY_FIELDS)[number];
export type MailConnectionState = "preparing" | "active" | "revoked" | "expired" | "blocked" | "blocked_cleanup";
export type MailConsentState = "active" | "revoked" | "expired";
export type MailWatchCadenceKind = "hourly" | "6h";
export interface MailWatchCadence {
  kind: MailWatchCadenceKind;
  timezone: string;
  daily_hour: null;
  daily_minute: null;
}
export type MailWatchBindingState = "active" | "paused" | "revoked" | "expired" | "blocked";

export interface MailConnectionMetadata {
  connection_id: string;
  service: "gmail_readonly";
  label: string;
  revision: number;
  state: MailConnectionState;
  scope_status: string;
  declared_scopes: [typeof GMAIL_SCOPE];
  provider_scopes_verified: boolean;
  verified_setup_job_id: string | null;
}

export interface MailLabelMetadata {
  label_id: string;
  name: string;
  type: string;
  connection_id: string;
  connection_revision: number;
  revision: number;
  state: "active" | "revoked";
}

export interface MailConsentMetadata {
  consent_id: string;
  connection_id: string;
  connection_revision: number;
  goal_id: string;
  goal_revision: number;
  label_ids: string[];
  window_days: 7;
  max_messages: number;
  source_read_allowed: boolean;
  source_revision: number;
  model_egress_allowed: boolean;
  model_revision: number;
  allowed_body_fields: MailBodyField[];
  expires_at: string;
  state: MailConsentState;
  revision: number;
}

export interface MailMessageMetadata {
  source_binding_id: string;
  message_key: string;
  thread_key: string;
  message_revision: string;
  received_at: string | null;
  subject: string;
  preview: string;
  read_status: string;
  fetched_at: string;
}

export interface MailScanCoverage {
  list_page_complete: boolean;
  more_available: boolean;
  returned: number;
  max_messages: number;
  window_days: number;
}

export interface MailMessageScanResponse {
  connection_id: string;
  connection_revision: number;
  consent_id: string;
  source_consent_revision: number;
  messages: MailMessageMetadata[];
  coverage: MailScanCoverage;
  provider_contact: true;
  control_job_id: string;
}

export interface MailMessageReadResponse {
  source_binding_id: string;
  message_key: string;
  thread_key: string;
  message_revision: string;
  subject: string;
  plain_text: string;
  truncated: boolean;
  read_status: string;
  received_at: string | null;
  fetched_at: string;
  provenance: {
    connection_id: string;
    connection_revision: number;
    consent_id: string;
    source_consent_revision: number;
    memory_status: "no_learning";
    egress: "local_only";
  };
  provider_contact: true;
  control_job_id: string;
}

export interface MailReplyTaskReceipt {
  status: "accepted" | "replayed" | "blocked" | "unknown";
  task_id: string;
  attempt_id: string | null;
  job_id: string | null;
  input_artifact_id: string;
  input_digest: string;
  message_key: string | null;
  message_revision: string | null;
  goal_id: string;
  goal_revision: number;
  source_status: string;
  effective_route: unknown | null;
  recovery_action: string | null;
  memory_status: "no_learning";
}

export interface MailDraftResponse {
  status: "pending" | "blocked" | "verified";
  task_id: string;
  recovery_action?: string;
  memory_status: "no_learning";
  draft?: {
    subject: string;
    plainbody: string;
    caveats: string[];
  };
  message_revision?: string;
  sent?: false;
  saved_to_provider?: false;
}

export type MailReplyRecoveryStatus = "not_found" | "verified" | "unknown" | "blocked" | "running" | "pending";

export interface MailReplyRecovery {
  status: MailReplyRecoveryStatus;
  idempotency_scope: "mail-reply-draft";
  idempotency_key: string;
  task_id: string | null;
  attempt_id: string | null;
  job_id: string | null;
  input_artifact_id: string | null;
  input_digest: string | null;
  request_digest: string | null;
  goal_id: string | null;
  goal_revision: number | null;
  memory_status: "no_learning";
  recovery_action: string;
}

export interface MailWatchRecovery {
  status: "not_found" | "replayed";
  idempotency_scope: "mail-watch";
  idempotency_key: string;
  watch: MailWatchMetadata | null;
  watch_id: string | null;
  input_artifact_id: string | null;
  input_digest: string | null;
  request_digest: string | null;
  goal_id: string | null;
  goal_revision: number | null;
  recovery_action: string | null;
  memory_status: "no_learning";
}

export type MailConnectionRecoveryStatus = "not_found" | "replayed" | "pending" | "blocked" | "unknown";

export interface MailConnectionRecovery {
  status: MailConnectionRecoveryStatus;
  idempotency_scope: "mail-connection-setup";
  idempotency_key: string;
  connection_id: string | null;
  request_digest: string | null;
  connection: MailConnectionMetadata | null;
  memory_status: "no_learning";
  recovery_action: string | null;
}

export interface MailWatchOccurrence {
  occurrence_id: string;
  state: string;
  slot_utc: string;
  failure_code: string | null;
  recovery_action: string | null;
}

export interface MailWatchMetadata {
  watch_id: string;
  scheduled_job_id: string;
  capability_id: "gmail.scan_metadata.v1";
  connection_id: string | null;
  connection_revision: number | null;
  mail_consent_id: string | null;
  source_consent_revision: number | null;
  goal_id: string;
  goal_revision: number;
  label_ids: string[];
  cadence: MailWatchCadence;
  binding_revision: number;
  expires_at: string;
  state: MailWatchBindingState;
  watch_state: "baseline_complete" | "coverage_blocked" | "not_started" | "active" | "unknown";
  baseline_complete: boolean;
  last_observed_at: string | null;
  last_completed_occurrence_id: string | null;
  skipped_coverage_reason: string | null;
  list_page_complete: boolean;
  latest_occurrence: MailWatchOccurrence | null;
}

export interface MailScheduleControlReceipt {
  binding_id: string;
  scheduled_job_id: string;
  binding_revision: number;
  state: Exclude<MailWatchBindingState, "blocked">;
}

export interface CreateMailConnectionRequest {
  schema_version: 1;
  service: "gmail_readonly";
  label: string;
  client_id: string;
  client_secret?: string;
  refresh_token: string;
  declared_scopes: [typeof GMAIL_SCOPE];
  idempotency_key: string;
}

export interface MailWatchCreateRequest {
  schema_version: 1;
  connection_id: string;
  expected_connection_revision: number;
  mail_consent_id: string;
  expected_source_consent_revision: number;
  goal_id: string;
  expected_goal_revision: number;
  label_ids: string[];
  cadence: MailWatchCadence;
  expires_at: string;
  max_messages: number;
  idempotency_key: string;
}

export class MailApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly recovery: string | null;

  constructor(status: number, code: string, message: string, recovery: string | null = null) {
    super(message);
    this.name = "MailApiError";
    this.status = status;
    this.code = code;
    this.recovery = recovery;
  }
}

const MAIL_REQUEST_TIMEOUT_MS = 15_000;

/**
 * Bound the whole operation, including response parsing. AbortController is
 * cooperative, so the deadline also settles the caller when a fetch adapter
 * or Response.json implementation ignores the abort signal.
 */
export function withMailDeadline<T>(
  operation: (signal: AbortSignal) => Promise<T>,
  parentSignal?: AbortSignal,
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const controller = new AbortController();
    let settled = false;
    let timeout: ReturnType<typeof setTimeout> | null = null;

    const cleanup = () => {
      if (timeout !== null) clearTimeout(timeout);
      parentSignal?.removeEventListener("abort", onAbort);
    };
    const finishResolve = (value: T) => {
      if (settled) return;
      settled = true;
      cleanup();
      resolve(value);
    };
    const finishReject = (error: unknown) => {
      if (settled) return;
      settled = true;
      cleanup();
      reject(error);
    };
    const onAbort = () => {
      controller.abort();
      finishReject(new MailApiError(0, "mail_transport_unavailable", "The Mail operation has no confirmed outcome. Keep the exact request for reconciliation.", "reconcile_existing_request"));
    };

    if (parentSignal?.aborted) {
      onAbort();
      return;
    }
    parentSignal?.addEventListener("abort", onAbort, { once: true });
    timeout = setTimeout(onAbort, MAIL_REQUEST_TIMEOUT_MS);
    void Promise.resolve()
      .then(() => operation(controller.signal))
      .then(finishResolve, finishReject);
  });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function fail(message: string): never {
  throw new MailApiError(200, "receipt_invalid", message);
}

function exactKeys(value: Record<string, unknown>, required: readonly string[], label: string): void {
  const actual = Object.keys(value).sort();
  const expected = [...required].sort();
  if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) {
    fail(`The ${label} receipt has an unexpected shape.`);
  }
}

function safeString(value: unknown, field: string, max = 1024): string {
  if (typeof value !== "string" || !value.trim() || value.length > max || CONTROL_PATTERN.test(value)) {
    fail(`The Mail receipt has an invalid ${field}.`);
  }
  return value;
}

function nullableString(value: unknown, field: string, max = 1024): string | null {
  return value === null ? null : safeString(value, field, max);
}

function nullableOpaqueId(value: unknown, field: string): string | null {
  return value === null ? null : opaqueId(value, field);
}

function nullableDigest(value: unknown, field: string): string | null {
  return value === null ? null : digest(value, field);
}

function nullablePositiveInteger(value: unknown, field: string): number | null {
  return value === null ? null : positiveInteger(value, field);
}

function positiveInteger(value: unknown, field: string, max = Number.MAX_SAFE_INTEGER): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < 1 || value > max) {
    fail(`The Mail receipt has an invalid ${field}.`);
  }
  return value;
}

function boundedInteger(value: unknown, field: string, min: number, max: number): number {
  if (typeof value !== "number" || !Number.isSafeInteger(value) || value < min || value > max) {
    fail(`The Mail receipt has an invalid ${field}.`);
  }
  return value;
}

function booleanValue(value: unknown, field: string): boolean {
  if (typeof value !== "boolean") fail(`The Mail receipt has an invalid ${field}.`);
  return value;
}

function timestamp(value: unknown, field: string): string {
  const result = safeString(value, field, 64);
  if (!Number.isFinite(Date.parse(result)) || !/(?:Z|\+00:00)$/i.test(result)) {
    fail(`The Mail receipt has an invalid ${field}.`);
  }
  return result;
}

function digest(value: unknown, field: string): string {
  const result = safeString(value, field, 128);
  if (!DIGEST_PATTERN.test(result)) fail(`The Mail receipt has an invalid ${field}.`);
  return result;
}

function opaqueId(value: unknown, field: string): string {
  const result = safeString(value, field, 256);
  if (!SAFE_ID_PATTERN.test(result)) fail(`The Mail receipt has an invalid ${field}.`);
  return result;
}

function listOfStrings(value: unknown, field: string, max: number, opaque = false): string[] {
  if (!Array.isArray(value) || value.length > max) fail(`The Mail receipt has an invalid ${field}.`);
  const result = value.map((item) => opaque ? opaqueId(item, field) : safeString(item, field, 256));
  if (new Set(result).size !== result.length) fail(`The Mail receipt has duplicate ${field}.`);
  return result;
}

function connection(value: unknown): MailConnectionMetadata {
  if (!isRecord(value)) fail("The connection receipt was not an object.");
  exactKeys(value, ["connection_id", "service", "label", "revision", "state", "scope_status", "declared_scopes", "provider_scopes_verified", "verified_setup_job_id"], "connection");
  if (value.service !== "gmail_readonly") fail("The connection receipt has an unexpected service.");
  if (!(typeof value.state === "string" && ["preparing", "active", "revoked", "expired", "blocked", "blocked_cleanup"].includes(value.state))) fail("The connection receipt has an invalid state.");
  if (!Array.isArray(value.declared_scopes) || value.declared_scopes.length !== 1 || value.declared_scopes[0] !== GMAIL_SCOPE) fail("The connection receipt has an invalid declared scope.");
  return {
    connection_id: opaqueId(value.connection_id, "connection ID"),
    service: "gmail_readonly",
    label: safeString(value.label, "label", 200),
    revision: positiveInteger(value.revision, "revision"),
    state: value.state as MailConnectionState,
    scope_status: safeString(value.scope_status, "scope status", 64),
    declared_scopes: [GMAIL_SCOPE],
    provider_scopes_verified: booleanValue(value.provider_scopes_verified, "provider scope status"),
    verified_setup_job_id: nullableString(value.verified_setup_job_id, "setup job ID"),
  };
}

function label(value: unknown): MailLabelMetadata {
  if (!isRecord(value)) fail("The label receipt was not an object.");
  exactKeys(value, ["label_id", "name", "type", "connection_id", "connection_revision", "revision", "state"], "label");
  if (value.state !== "active" && value.state !== "revoked") fail("The label receipt has an invalid state.");
  return {
    label_id: opaqueId(value.label_id, "label ID"),
    name: safeString(value.name, "label name", 200),
    type: safeString(value.type, "label type", 32),
    connection_id: opaqueId(value.connection_id, "connection ID"),
    connection_revision: positiveInteger(value.connection_revision, "connection revision"),
    revision: positiveInteger(value.revision, "label revision"),
    state: value.state,
  };
}

function consent(value: unknown): MailConsentMetadata {
  if (!isRecord(value)) fail("The consent receipt was not an object.");
  exactKeys(value, ["consent_id", "connection_id", "connection_revision", "goal_id", "goal_revision", "label_ids", "window_days", "max_messages", "source_read_allowed", "source_revision", "model_egress_allowed", "model_revision", "allowed_body_fields", "expires_at", "state", "revision"], "consent");
  const fields = listOfStrings(value.allowed_body_fields, "allowed body fields", BODY_FIELDS.length) as MailBodyField[];
  if (!fields.every((field) => BODY_FIELDS.includes(field))) fail("The consent receipt has an unsupported body field.");
  if (value.state !== "active" && value.state !== "revoked" && value.state !== "expired") fail("The consent receipt has an invalid state.");
  const labels = listOfStrings(value.label_ids, "label IDs", 3, true);
  return {
    consent_id: opaqueId(value.consent_id, "consent ID"),
    connection_id: opaqueId(value.connection_id, "connection ID"),
    connection_revision: positiveInteger(value.connection_revision, "connection revision"),
    goal_id: opaqueId(value.goal_id, "goal ID"),
    goal_revision: positiveInteger(value.goal_revision, "goal revision"),
    label_ids: labels,
    window_days: 7,
    max_messages: boundedInteger(value.max_messages, "message limit", 1, 10),
    source_read_allowed: booleanValue(value.source_read_allowed, "source consent"),
    source_revision: positiveInteger(value.source_revision, "source revision"),
    model_egress_allowed: booleanValue(value.model_egress_allowed, "model consent"),
    model_revision: positiveInteger(value.model_revision, "model revision"),
    allowed_body_fields: fields,
    expires_at: timestamp(value.expires_at, "consent expiry"),
    state: value.state,
    revision: positiveInteger(value.revision, "consent revision"),
  };
}

function errorDetail(payload: unknown): { code: string; message: string; recovery: string | null } {
  if (!isRecord(payload) || !isRecord(payload.detail)) return { code: "mail_request_failed", message: "The Mail request failed.", recovery: null };
  const detail = payload.detail;
  return {
    code: typeof detail.code === "string" && detail.code.length <= 128 ? detail.code : "mail_request_failed",
    message: typeof detail.message === "string" && detail.message.trim() ? detail.message.slice(0, 500) : "The Mail request failed.",
    recovery: detail.recovery_action === null || detail.recovery_action === undefined
      ? null
      : typeof detail.recovery_action === "string" && detail.recovery_action.length <= 128 ? detail.recovery_action : null,
  };
}

export async function mailRequest<T>(path: string, init: RequestInit, validate: (value: unknown) => T): Promise<T> {
  let response: Response;
  let payload: unknown;
  try {
    ({ response, payload } = await withMailDeadline<{ response: Response; payload: unknown }>(async (signal) => {
      const nextInit = { ...init, signal };
      const nextResponse = await apiFetch(`${API_URL}${path}`, nextInit);
      const nextPayload = await nextResponse.json().catch(() => null);
      return { response: nextResponse, payload: nextPayload };
    }, init.signal ?? undefined));
  } catch (error) {
    if (error instanceof MailApiError) throw error;
    throw new MailApiError(0, "mail_transport_unavailable", "The Mail operation has no confirmed outcome. Keep the exact request for reconciliation.", "reconcile_existing_request");
  }
  if (!response.ok) {
    const detail = errorDetail(payload);
    throw new MailApiError(response.status, detail.code, detail.message, detail.recovery);
  }
  try {
    return validate(payload);
  } catch (error) {
    if (error instanceof MailApiError) throw error;
    throw new MailApiError(200, "receipt_invalid", "The Mail response could not be validated.");
  }
}

function json(method: string, body: unknown, signal?: AbortSignal): RequestInit {
  return { method, signal, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

function envelope<T>(value: unknown, key: string, parse: (input: unknown) => T, label: string): T {
  if (!isRecord(value)) fail(`The ${label} response was not an object.`);
  exactKeys(value, [key], label);
  return parse(value[key]);
}

function connectionsResponse(value: unknown): MailConnectionMetadata[] {
  if (!isRecord(value)) fail("The connection list response was not an object.");
  exactKeys(value, ["connections"], "connection list");
  if (!Array.isArray(value.connections) || value.connections.length > 100) fail("The connection list is invalid.");
  return value.connections.map(connection);
}

function labelsResponse(value: unknown): { connection_id: string; connection_revision: number; labels: MailLabelMetadata[]; provider_contact: false } {
  if (!isRecord(value)) fail("The label response was not an object.");
  exactKeys(value, ["connection_id", "connection_revision", "labels", "provider_contact"], "label response");
  if (value.provider_contact !== false || !Array.isArray(value.labels) || value.labels.length > 200) fail("The cached label response is invalid.");
  return { connection_id: opaqueId(value.connection_id, "connection ID"), connection_revision: positiveInteger(value.connection_revision, "connection revision"), labels: value.labels.map(label), provider_contact: false };
}

function consentsResponse(value: unknown): { consents: MailConsentMetadata[]; provider_contact: false } {
  if (!isRecord(value)) fail("The consent list response was not an object.");
  exactKeys(value, ["consents", "provider_contact"], "consent list");
  if (value.provider_contact !== false || !Array.isArray(value.consents) || value.consents.length > 100) fail("The consent list is invalid.");
  return { consents: value.consents.map(consent), provider_contact: false };
}

function connectionEnvelope(value: unknown): MailConnectionMetadata {
  return envelope(value, "connection", connection, "connection");
}

function consentEnvelope(value: unknown): MailConsentMetadata {
  return envelope(value, "consent", consent, "consent");
}

function operationMetadata(value: unknown): { connection_id: string; connection_revision: number; provider_contact: true; control_job_id: string } {
  if (!isRecord(value)) fail("The Mail operation response was not an object.");
  for (const key of ["connection_id", "connection_revision", "provider_contact", "control_job_id"] as const) {
    if (!(key in value)) fail("The Mail operation response is incomplete.");
  }
  return {
    connection_id: opaqueId(value.connection_id, "connection ID"),
    connection_revision: positiveInteger(value.connection_revision, "connection revision"),
    provider_contact: value.provider_contact === true ? true : fail("The Mail operation did not prove provider contact."),
    control_job_id: opaqueId(value.control_job_id, "control job ID"),
  };
}

function scanResponse(value: unknown): MailMessageScanResponse {
  if (!isRecord(value)) fail("The Mail scan response was not an object.");
  exactKeys(value, ["connection_id", "connection_revision", "consent_id", "source_consent_revision", "messages", "coverage", "provider_contact", "control_job_id"], "Mail scan");
  if (value.provider_contact !== true || !Array.isArray(value.messages) || value.messages.length > 10 || !isRecord(value.coverage)) fail("The Mail scan response is invalid.");
  const coverage = value.coverage;
  exactKeys(coverage, ["list_page_complete", "more_available", "returned", "max_messages", "window_days"], "scan coverage");
  return {
    connection_id: opaqueId(value.connection_id, "connection ID"),
    connection_revision: positiveInteger(value.connection_revision, "connection revision"),
    consent_id: opaqueId(value.consent_id, "consent ID"),
    source_consent_revision: positiveInteger(value.source_consent_revision, "source consent revision"),
    messages: value.messages.map((item) => {
      if (!isRecord(item)) fail("The Mail message metadata is invalid.");
      exactKeys(item, ["source_binding_id", "message_key", "thread_key", "message_revision", "received_at", "subject", "preview", "read_status", "fetched_at"], "message metadata");
      return {
        source_binding_id: opaqueId(item.source_binding_id, "message binding ID"),
        message_key: opaqueId(item.message_key, "message key"),
        thread_key: opaqueId(item.thread_key, "thread key"),
        message_revision: digest(item.message_revision, "message revision"),
        received_at: item.received_at === null ? null : timestamp(item.received_at, "received time"),
        subject: safeString(item.subject, "message subject", 500),
        preview: safeString(item.preview, "message preview", 1000),
        read_status: safeString(item.read_status, "read status", 64),
        fetched_at: timestamp(item.fetched_at, "fetched time"),
      };
    }),
    coverage: {
      list_page_complete: booleanValue(coverage.list_page_complete, "list page completion"),
      more_available: booleanValue(coverage.more_available, "more available"),
      returned: boundedInteger(coverage.returned, "returned count", 0, 10),
      max_messages: boundedInteger(coverage.max_messages, "maximum messages", 1, 10),
      window_days: boundedInteger(coverage.window_days, "window days", 1, 7),
    },
    provider_contact: true,
    control_job_id: opaqueId(value.control_job_id, "control job ID"),
  };
}

function readResponse(value: unknown): MailMessageReadResponse {
  if (!isRecord(value)) fail("The Mail read response was not an object.");
  exactKeys(value, ["source_binding_id", "message_key", "thread_key", "message_revision", "subject", "plain_text", "truncated", "read_status", "received_at", "fetched_at", "provenance", "provider_contact", "control_job_id"], "Mail message read");
  if (!isRecord(value.provenance)) fail("The Mail read provenance is invalid.");
  const provenance = value.provenance;
  exactKeys(provenance, ["connection_id", "connection_revision", "consent_id", "source_consent_revision", "memory_status", "egress"], "Mail read provenance");
  if (provenance.memory_status !== "no_learning" || provenance.egress !== "local_only" || value.provider_contact !== true) fail("The Mail read response has an invalid privacy receipt.");
  return {
    source_binding_id: opaqueId(value.source_binding_id, "message binding ID"),
    message_key: opaqueId(value.message_key, "message key"),
    thread_key: opaqueId(value.thread_key, "thread key"),
    message_revision: digest(value.message_revision, "message revision"),
    subject: safeString(value.subject, "message subject", 500),
    plain_text: safeString(value.plain_text, "message body", 64 * 1024),
    truncated: booleanValue(value.truncated, "truncation status"),
    read_status: safeString(value.read_status, "read status", 64),
    received_at: value.received_at === null ? null : timestamp(value.received_at, "received time"),
    fetched_at: timestamp(value.fetched_at, "fetched time"),
    provenance: {
      connection_id: opaqueId(provenance.connection_id, "connection ID"),
      connection_revision: positiveInteger(provenance.connection_revision, "connection revision"),
      consent_id: opaqueId(provenance.consent_id, "consent ID"),
      source_consent_revision: positiveInteger(provenance.source_consent_revision, "source consent revision"),
      memory_status: "no_learning",
      egress: "local_only",
    },
    provider_contact: true,
    control_job_id: opaqueId(value.control_job_id, "control job ID"),
  };
}

function replyResponse(value: unknown): MailReplyTaskReceipt {
  if (!isRecord(value)) fail("The reply task response was not an object.");
  exactKeys(value, ["status", "task_id", "attempt_id", "job_id", "input_artifact_id", "input_digest", "message_key", "message_revision", "goal_id", "goal_revision", "source_status", "effective_route", "recovery_action", "memory_status"], "reply task");
  if (!["accepted", "replayed", "blocked", "unknown"].includes(String(value.status)) || value.memory_status !== "no_learning") fail("The reply task response is invalid.");
  return {
    status: value.status as MailReplyTaskReceipt["status"],
    task_id: opaqueId(value.task_id, "task ID"),
    attempt_id: nullableString(value.attempt_id, "attempt ID"),
    job_id: nullableString(value.job_id, "job ID"),
    input_artifact_id: opaqueId(value.input_artifact_id, "input artifact ID"),
    input_digest: digest(value.input_digest, "input digest"),
    message_key: nullableString(value.message_key, "message key"),
    message_revision: value.message_revision === null ? null : digest(value.message_revision, "message revision"),
    goal_id: opaqueId(value.goal_id, "goal ID"),
    goal_revision: positiveInteger(value.goal_revision, "goal revision"),
    source_status: safeString(value.source_status, "source status", 64),
    effective_route: value.effective_route === null ? null : value.effective_route,
    recovery_action: nullableString(value.recovery_action, "recovery action", 128),
    memory_status: "no_learning",
  };
}

function draftResponse(value: unknown): MailDraftResponse {
  if (!isRecord(value)) fail("The draft response was not an object.");
  const status = value.status;
  if (status === "pending" || status === "blocked") {
    exactKeys(value, ["status", "task_id", "recovery_action", "memory_status"], "draft status");
    if (value.memory_status !== "no_learning") fail("The draft status has an invalid memory receipt.");
    return { status, task_id: opaqueId(value.task_id, "task ID"), recovery_action: nullableString(value.recovery_action, "recovery action", 128) ?? undefined, memory_status: "no_learning" };
  }
  if (status !== "verified" || !isRecord(value.draft)) fail("The verified draft response is invalid.");
  exactKeys(value, ["status", "task_id", "draft", "message_revision", "memory_status", "sent", "saved_to_provider"], "verified draft");
  const draft = value.draft;
  exactKeys(draft, ["subject", "plainbody", "caveats"], "draft body");
  return {
    status: "verified",
    task_id: opaqueId(value.task_id, "task ID"),
    draft: { subject: safeString(draft.subject, "draft subject", 500), plainbody: safeString(draft.plainbody, "draft body", 64 * 1024), caveats: listOfStrings(draft.caveats, "draft caveats", 16) },
    message_revision: digest(value.message_revision, "message revision"),
    memory_status: "no_learning",
    sent: value.sent === false ? false : fail("A Mail draft cannot be marked sent."),
    saved_to_provider: value.saved_to_provider === false ? false : fail("A Mail draft cannot be marked as provider saved."),
  };
}

function replyRecoveryResponse(value: unknown): MailReplyRecovery {
  if (!isRecord(value)) fail("The Mail reply recovery response was not an object.");
  exactKeys(value, [
    "status", "idempotency_scope", "idempotency_key", "task_id", "attempt_id", "job_id",
    "input_artifact_id", "input_digest", "request_digest", "goal_id", "goal_revision",
    "memory_status", "recovery_action",
  ], "Mail reply recovery");
  const status = value.status;
  if (!["not_found", "verified", "unknown", "blocked", "running", "pending"].includes(String(status))) fail("The Mail reply recovery status is invalid.");
  if (value.idempotency_scope !== "mail-reply-draft" || value.memory_status !== "no_learning") fail("The Mail reply recovery scope is invalid.");
  return {
    status: status as MailReplyRecoveryStatus,
    idempotency_scope: "mail-reply-draft",
    idempotency_key: opaqueId(value.idempotency_key, "reply idempotency key"),
    task_id: nullableOpaqueId(value.task_id, "task ID"),
    attempt_id: nullableOpaqueId(value.attempt_id, "attempt ID"),
    job_id: nullableOpaqueId(value.job_id, "job ID"),
    input_artifact_id: nullableOpaqueId(value.input_artifact_id, "input artifact ID"),
    input_digest: nullableDigest(value.input_digest, "input digest"),
    request_digest: nullableDigest(value.request_digest, "request digest"),
    goal_id: nullableOpaqueId(value.goal_id, "Goal ID"),
    goal_revision: nullablePositiveInteger(value.goal_revision, "Goal revision"),
    memory_status: "no_learning",
    recovery_action: safeString(value.recovery_action, "recovery action", 128),
  };
}

function watchRecoveryResponse(value: unknown): MailWatchRecovery {
  if (!isRecord(value)) fail("The Mail watch recovery response was not an object.");
  exactKeys(value, [
    "status", "idempotency_scope", "idempotency_key", "watch", "watch_id", "input_artifact_id",
    "input_digest", "request_digest", "goal_id", "goal_revision", "recovery_action", "memory_status",
  ], "Mail watch recovery");
  if (value.status !== "not_found" && value.status !== "replayed") fail("The Mail watch recovery status is invalid.");
  if (value.idempotency_scope !== "mail-watch" || value.memory_status !== "no_learning") fail("The Mail watch recovery scope is invalid.");
  const parsedWatch = value.watch === null ? null : watch(value.watch);
  if (value.status === "replayed" && !parsedWatch) fail("The replayed Mail watch recovery has no watch projection.");
  if (parsedWatch && value.watch_id !== parsedWatch.watch_id) fail("The Mail watch recovery identity does not match its projection.");
  return {
    status: value.status,
    idempotency_scope: "mail-watch",
    idempotency_key: opaqueId(value.idempotency_key, "watch idempotency key"),
    watch: parsedWatch,
    watch_id: nullableOpaqueId(value.watch_id, "watch ID"),
    input_artifact_id: nullableOpaqueId(value.input_artifact_id, "input artifact ID"),
    input_digest: nullableDigest(value.input_digest, "input digest"),
    request_digest: nullableDigest(value.request_digest, "request digest"),
    goal_id: nullableOpaqueId(value.goal_id, "Goal ID"),
    goal_revision: nullablePositiveInteger(value.goal_revision, "Goal revision"),
    recovery_action: value.recovery_action === null ? null : safeString(value.recovery_action, "recovery action", 128),
    memory_status: "no_learning",
  };
}

function connectionRecoveryResponse(value: unknown): MailConnectionRecovery {
  if (!isRecord(value)) fail("The Mail connection recovery response was not an object.");
  exactKeys(value, ["status", "idempotency_scope", "idempotency_key", "connection_id", "request_digest", "connection", "memory_status", "recovery_action"], "Mail connection recovery");
  if (!["not_found", "replayed", "pending", "blocked", "unknown"].includes(String(value.status))) fail("The Mail connection recovery status is invalid.");
  if (value.idempotency_scope !== "mail-connection-setup" || value.memory_status !== "no_learning") fail("The Mail connection recovery scope is invalid.");
  const parsedConnection = value.connection === null ? null : connection(value.connection);
  if (parsedConnection && value.connection_id !== parsedConnection.connection_id) fail("The Mail connection recovery identity does not match its projection.");
  return {
    status: value.status as MailConnectionRecoveryStatus,
    idempotency_scope: "mail-connection-setup",
    idempotency_key: opaqueId(value.idempotency_key, "setup idempotency key"),
    connection_id: nullableOpaqueId(value.connection_id, "connection ID"),
    request_digest: nullableDigest(value.request_digest, "request digest"),
    connection: parsedConnection,
    memory_status: "no_learning",
    recovery_action: value.recovery_action === null ? null : safeString(value.recovery_action, "recovery action", 128),
  };
}

function watch(value: unknown): MailWatchMetadata {
  if (!isRecord(value)) fail("The Mail watch response was not an object.");
  exactKeys(value, ["watch_id", "scheduled_job_id", "capability_id", "connection_id", "connection_revision", "mail_consent_id", "source_consent_revision", "goal_id", "goal_revision", "label_ids", "cadence", "binding_revision", "expires_at", "state", "watch_state", "baseline_complete", "last_observed_at", "last_completed_occurrence_id", "skipped_coverage_reason", "list_page_complete", "latest_occurrence"], "Mail watch");
  if (value.capability_id !== "gmail.scan_metadata.v1" || !isRecord(value.cadence)) fail("The Mail watch capability is invalid.");
  const cadence = value.cadence;
  exactKeys(cadence, ["kind", "timezone", "daily_hour", "daily_minute"], "watch cadence");
  if (cadence.kind !== "hourly" && cadence.kind !== "6h" || cadence.daily_hour !== null || cadence.daily_minute !== null) fail("The Mail watch cadence is invalid.");
  const state = value.watch_state;
  if (!["baseline_complete", "coverage_blocked", "not_started", "active", "unknown"].includes(String(state))) fail("The Mail watch state is invalid.");
  if (!["active", "paused", "revoked", "expired", "blocked"].includes(String(value.state))) fail("The Mail binding state is invalid.");
  let latest: MailWatchOccurrence | null = null;
  if (value.latest_occurrence !== null) {
    if (!isRecord(value.latest_occurrence)) fail("The Mail watch occurrence is invalid.");
    exactKeys(value.latest_occurrence, ["occurrence_id", "state", "slot_utc", "failure_code", "recovery_action"], "watch occurrence");
    latest = { occurrence_id: opaqueId(value.latest_occurrence.occurrence_id, "occurrence ID"), state: safeString(value.latest_occurrence.state, "occurrence state", 64), slot_utc: timestamp(value.latest_occurrence.slot_utc, "occurrence slot"), failure_code: nullableString(value.latest_occurrence.failure_code, "failure code", 128), recovery_action: nullableString(value.latest_occurrence.recovery_action, "recovery action", 128) };
  }
  return {
    watch_id: opaqueId(value.watch_id, "watch ID"),
    scheduled_job_id: opaqueId(value.scheduled_job_id, "scheduled job ID"),
    capability_id: "gmail.scan_metadata.v1",
    connection_id: value.connection_id === null ? null : opaqueId(value.connection_id, "connection ID"),
    connection_revision: value.connection_revision === null ? null : positiveInteger(value.connection_revision, "connection revision"),
    mail_consent_id: value.mail_consent_id === null ? null : opaqueId(value.mail_consent_id, "consent ID"),
    source_consent_revision: value.source_consent_revision === null ? null : positiveInteger(value.source_consent_revision, "source consent revision"),
    goal_id: opaqueId(value.goal_id, "goal ID"),
    goal_revision: positiveInteger(value.goal_revision, "goal revision"),
    label_ids: listOfStrings(value.label_ids, "watch label IDs", 3, true),
    cadence: { kind: cadence.kind, timezone: safeString(cadence.timezone, "watch timezone", 128), daily_hour: null, daily_minute: null },
    binding_revision: positiveInteger(value.binding_revision, "binding revision"),
    expires_at: timestamp(value.expires_at, "watch expiry"),
    state: value.state as MailWatchBindingState,
    watch_state: state as MailWatchMetadata["watch_state"],
    baseline_complete: booleanValue(value.baseline_complete, "baseline status"),
    last_observed_at: value.last_observed_at === null ? null : timestamp(value.last_observed_at, "last observed time"),
    last_completed_occurrence_id: value.last_completed_occurrence_id === null ? null : opaqueId(value.last_completed_occurrence_id, "completed occurrence ID"),
    skipped_coverage_reason: nullableString(value.skipped_coverage_reason, "coverage reason", 256),
    list_page_complete: booleanValue(value.list_page_complete, "list page completion"),
    latest_occurrence: latest,
  };
}

function watchEnvelope(value: unknown): MailWatchMetadata {
  if (!isRecord(value)) fail("The Mail watch response was not an object.");
  exactKeys(value, ["watch", "status"], "Mail watch response");
  if (value.status !== "accepted" && value.status !== "replayed") fail("The Mail watch response has an invalid status.");
  return watch(value.watch);
}

function watchProjectionEnvelope(value: unknown): MailWatchMetadata {
  return envelope(value, "watch", watch, "Mail watch");
}

function watchesResponse(value: unknown): MailWatchMetadata[] {
  if (!isRecord(value)) fail("The Mail watch list response was not an object.");
  exactKeys(value, ["watches"], "Mail watch list");
  if (!Array.isArray(value.watches) || value.watches.length > 100) fail("The Mail watch list is invalid.");
  return value.watches.map(watch);
}

function scheduleControlResponse(value: unknown): MailScheduleControlReceipt {
  if (!isRecord(value)) fail("The Mail schedule control response was not an object.");
  exactKeys(value, ["binding"], "Mail schedule control");
  const binding = value.binding;
  if (!isRecord(binding)) fail("The Mail schedule control binding was not an object.");
  exactKeys(binding, [
    "binding_id", "scheduled_job_id", "capability_id", "action_type", "goal_id", "goal_revision",
    "input_artifact_id", "input_digest", "consent_kind", "consent_id", "consent_revision", "consent_digest",
    "cadence", "binding_revision", "expires_at", "state", "last_slot_utc", "created_at", "updated_at", "latest_occurrence",
  ], "Mail schedule control binding");
  if (binding.capability_id !== "gmail.scan_metadata.v1" || binding.action_type !== "gmail.scan_metadata.v1") fail("The Mail schedule control capability is invalid.");
  if (!["active", "paused", "revoked", "expired"].includes(String(binding.state))) fail("The Mail schedule control state is invalid.");
  if (!isRecord(binding.cadence)) fail("The Mail schedule control cadence is invalid.");
  exactKeys(binding.cadence, ["kind", "timezone", "daily_hour", "daily_minute"], "Mail schedule control cadence");
  if ((binding.cadence.kind !== "hourly" && binding.cadence.kind !== "6h") || binding.cadence.daily_hour !== null || binding.cadence.daily_minute !== null) fail("The Mail schedule control cadence is invalid.");
  if (binding.latest_occurrence !== null) {
    if (!isRecord(binding.latest_occurrence)) fail("The Mail schedule control occurrence is invalid.");
    exactKeys(binding.latest_occurrence, ["occurrence_id", "binding_revision", "slot_utc", "state", "task_id", "job_id", "failure_code", "recovery_action", "updated_at"], "Mail schedule control occurrence");
    opaqueId(binding.latest_occurrence.occurrence_id, "occurrence ID");
    positiveInteger(binding.latest_occurrence.binding_revision, "occurrence binding revision");
    timestamp(binding.latest_occurrence.slot_utc, "occurrence slot");
    safeString(binding.latest_occurrence.state, "occurrence state", 64);
    nullableString(binding.latest_occurrence.task_id, "occurrence task ID");
    nullableString(binding.latest_occurrence.job_id, "occurrence job ID");
    nullableString(binding.latest_occurrence.failure_code, "failure code", 128);
    nullableString(binding.latest_occurrence.recovery_action, "recovery action", 128);
    timestamp(binding.latest_occurrence.updated_at, "occurrence updated_at");
  }
  return {
    binding_id: opaqueId(binding.binding_id, "binding ID"),
    scheduled_job_id: opaqueId(binding.scheduled_job_id, "scheduled job ID"),
    binding_revision: positiveInteger(binding.binding_revision, "binding revision"),
    state: binding.state as MailScheduleControlReceipt["state"],
  };
}


function id(value: string, field: string): string {
  if (!SAFE_ID_PATTERN.test(value)) throw new MailApiError(0, "mail_request_invalid", `The ${field} is invalid.`);
  return encodeURIComponent(value);
}

export function makeMailIdempotencyKey(prefix: string): string {
  try {
    return `${prefix}:${crypto.randomUUID()}`;
  } catch {
    return `${prefix}:${Date.now().toString(36)}:${Math.random().toString(36).slice(2)}`;
  }
}

export function listMailConnections(signal?: AbortSignal): Promise<MailConnectionMetadata[]> {
  return mailRequest(`${MAIL_BASE}/connections`, { method: "GET", signal }, connectionsResponse);
}

export function createMailConnection(request: CreateMailConnectionRequest, signal?: AbortSignal): Promise<MailConnectionMetadata> {
  return mailRequest(`${MAIL_BASE}/connections`, json("POST", request, signal), connectionEnvelope);
}

export function getMailConnectionRecovery(idempotencyKey: string, signal?: AbortSignal): Promise<MailConnectionRecovery> {
  return mailRequest(`${MAIL_BASE}/connections/recovery/${id(idempotencyKey, "setup idempotency key")}`, { method: "GET", signal }, connectionRecoveryResponse);
}

export function verifyMailConnection(connectionId: string, request: { expected_revision: number; request_uuid: string }, signal?: AbortSignal): Promise<Record<string, unknown>> {
  return mailRequest(`${MAIL_BASE}/connections/${id(connectionId, "connection ID")}/verify`, json("POST", request, signal), (value) => operationMetadata(value) && value as Record<string, unknown>);
}

export function revokeMailConnection(connectionId: string, request: { expected_revision: number; idempotency_key: string }, signal?: AbortSignal): Promise<MailConnectionMetadata> {
  return mailRequest(`${MAIL_BASE}/connections/${id(connectionId, "connection ID")}`, json("DELETE", request, signal), connectionEnvelope);
}

export function listMailLabels(connectionId: string, signal?: AbortSignal): Promise<{ connection_id: string; connection_revision: number; labels: MailLabelMetadata[]; provider_contact: false }> {
  return mailRequest(`${MAIL_BASE}/labels?connection_id=${id(connectionId, "connection ID")}`, { method: "GET", signal }, labelsResponse);
}

export function refreshMailLabels(request: { connection_id: string; expected_connection_revision: number; acknowledge_account_label_read: true; request_uuid: string }, signal?: AbortSignal): Promise<Record<string, unknown>> {
  return mailRequest(`${MAIL_BASE}/labels/refresh`, json("POST", request, signal), (value) => operationMetadata(value) && value as Record<string, unknown>);
}

export function listMailConsents(connectionId?: string, signal?: AbortSignal): Promise<{ consents: MailConsentMetadata[]; provider_contact: false }> {
  const query = connectionId ? `?connection_id=${id(connectionId, "connection ID")}` : "";
  return mailRequest(`${MAIL_BASE}/read-consents${query}`, { method: "GET", signal }, consentsResponse);
}

export interface CreateMailConsentRequest {
  schema_version: 1;
  connection_id: string;
  expected_connection_revision: number;
  goal_id: string;
  expected_goal_revision: number;
  label_ids: string[];
  expires_at: string;
  max_messages: number;
  allowed_body_fields: MailBodyField[];
  acknowledge_source_read: true;
  idempotency_key: string;
}

export function createMailConsent(request: CreateMailConsentRequest, signal?: AbortSignal): Promise<MailConsentMetadata> {
  return mailRequest(`${MAIL_BASE}/read-consents`, json("POST", request, signal), consentEnvelope);
}

export function setMailModelConsent(consentId: string, request: { expected_revision: number; acknowledged_payload_fields: MailBodyField[]; allow: boolean }, signal?: AbortSignal): Promise<MailConsentMetadata> {
  return mailRequest(`${MAIL_BASE}/read-consents/${id(consentId, "consent ID")}/model-consent`, json("POST", request, signal), consentEnvelope);
}

export function revokeMailConsent(consentId: string, request: { expected_revision: number; idempotency_key: string; reason: string }, signal?: AbortSignal): Promise<MailConsentMetadata> {
  return mailRequest(`${MAIL_BASE}/read-consents/${id(consentId, "consent ID")}/revoke`, json("POST", request, signal), consentEnvelope);
}

export function scanMailMessages(request: { connection_id: string; expected_connection_revision: number; mail_consent_id: string; expected_source_consent_revision: number; label_ids: string[]; received_after: string; max_messages: number; request_uuid: string }, signal?: AbortSignal): Promise<MailMessageScanResponse> {
  return mailRequest(`${MAIL_BASE}/messages/scan`, json("POST", request, signal), scanResponse);
}

export function readMailMessage(messageBindingId: string, request: { connection_id: string; expected_connection_revision: number; mail_consent_id: string; expected_source_consent_revision: number; message_binding_id: string; expected_message_revision: string; acknowledge_selected_body_read: true; request_uuid: string }, signal?: AbortSignal): Promise<MailMessageReadResponse> {
  return mailRequest(`${MAIL_BASE}/messages/${id(messageBindingId, "message binding ID")}/read`, json("POST", request, signal), readResponse);
}

export function createMailReplyTask(request: { schema_version: 1; connection_id: string; expected_connection_revision: number; message_binding_id: string; expected_message_revision: string; mail_consent_id: string; expected_source_consent_revision: number; expected_model_consent_revision: number; goal_id: string; expected_goal_revision: number; reply_intent: string; style: "brief" | "formal"; idempotency_key: string }, signal?: AbortSignal): Promise<MailReplyTaskReceipt> {
  return mailRequest(`${MAIL_BASE}/reply-tasks`, json("POST", request, signal), replyResponse);
}

export function getMailReplyDraft(taskId: string, signal?: AbortSignal): Promise<MailDraftResponse> {
  return mailRequest(`${MAIL_BASE}/reply-tasks/${id(taskId, "task ID")}/draft`, { method: "GET", signal }, draftResponse);
}

export function getMailReplyRecovery(idempotencyKey: string, signal?: AbortSignal): Promise<MailReplyRecovery> {
  return mailRequest(`${MAIL_BASE}/reply-tasks/recovery/${id(idempotencyKey, "reply idempotency key")}`, { method: "GET", signal }, replyRecoveryResponse);
}

export function createMailWatch(request: MailWatchCreateRequest, signal?: AbortSignal): Promise<MailWatchMetadata> {
  return mailRequest(`${MAIL_BASE}/watches`, json("POST", request, signal), watchEnvelope);
}

export function listMailWatches(signal?: AbortSignal): Promise<MailWatchMetadata[]> {
  return mailRequest(`${MAIL_BASE}/watches`, { method: "GET", signal }, watchesResponse);
}

export function getMailWatch(watchId: string, signal?: AbortSignal): Promise<MailWatchMetadata> {
  return mailRequest(`${MAIL_BASE}/watches/${id(watchId, "watch ID")}`, { method: "GET", signal }, watchProjectionEnvelope);
}

export function getMailWatchRecovery(idempotencyKey: string, signal?: AbortSignal): Promise<MailWatchRecovery> {
  return mailRequest(`${MAIL_BASE}/watches/recovery/${id(idempotencyKey, "watch idempotency key")}`, { method: "GET", signal }, watchRecoveryResponse);
}

export type MailWatchControlRequest =
  | { action: "pause" | "resume"; expected_binding_revision: number; idempotency_key: string }
  | { expected_binding_revision: number; idempotency_key: string; reason: string };

export function controlMailWatch(watchId: string, request: Extract<MailWatchControlRequest, { action: "pause" | "resume" }>, signal?: AbortSignal): Promise<MailScheduleControlReceipt> {
  return mailRequest(`/api/governed-schedules/${id(watchId, "watch ID")}`, json("PATCH", request, signal), scheduleControlResponse);
}

export function revokeMailWatch(watchId: string, request: Extract<MailWatchControlRequest, { reason: string }>, signal?: AbortSignal): Promise<MailScheduleControlReceipt> {
  return mailRequest(`/api/governed-schedules/${id(watchId, "watch ID")}/revoke`, json("POST", request, signal), scheduleControlResponse);
}


export function validateMailConnectionResponse(value: unknown): MailConnectionMetadata {
  return connectionEnvelope(value);
}

export function validateMailConsentResponse(value: unknown): MailConsentMetadata {
  return consentEnvelope(value);
}

export function validateMailWatchResponse(value: unknown): MailWatchMetadata {
  return watchEnvelope(value);
}

export function validateMailWatchListResponse(value: unknown): MailWatchMetadata[] {
  return watchesResponse(value);
}

export function validateMailWatchControlResponse(value: unknown): MailScheduleControlReceipt {
  return scheduleControlResponse(value);
}
