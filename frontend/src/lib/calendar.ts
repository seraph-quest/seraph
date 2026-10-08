import { API_URL } from "../config/constants";
import { apiFetch } from "./api";
import { normalizeConnectedRequest, relatedSources } from "./connectionSync";
import type {
  CalendarApiErrorDetail,
  CalendarCadence,
  CalendarConnectionMetadata,
  CalendarConnectionMutationRequest,
  CalendarEffectiveRoute,
  CalendarEventListResponse,
  CalendarEventOption,
  CalendarExecutionProjection,
  CalendarInputArtifactMetadata,
  CalendarLatestOccurrence,
  CalendarOption,
  CalendarPrepResponse,
  CalendarReadReceipt,
  CalendarResultPreview,
  CalendarVerifyResponse,
  CreateCalendarConnectionRequest,
  CreateCalendarPrepRequest,
  CreateCalendarReadConsentRequest,
  CreateCalendarScheduleRequest,
  GovernedScheduleBinding,
  WorkBoardTask,
  CalendarConsentMetadata,
} from "../types";

const CALENDAR_SERVICE = "calendar_readonly" as const;
const PREP_CAPABILITY = "calendar.meeting-prep.v1" as const;
const DIGEST_PATTERN = /^(?:sha256:)?[0-9a-f]{64}$/i;
const CONTROL_PATTERN = /[\u0000-\u001f\u007f]/;

export class CalendarApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly recovery: string | null;

  constructor(status: number, code: string, message: string, recovery: string | null = null) {
    super(message);
    this.name = "CalendarApiError";
    this.status = status;
    this.code = code;
    this.recovery = recovery;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function fail(message: string): never {
  throw new CalendarApiError(200, "receipt_invalid", message);
}

function exactKeys(value: Record<string, unknown>, required: readonly string[], label: string): void {
  const actual = Object.keys(value).sort();
  const expected = [...required].sort();
  if (actual.length !== expected.length || actual.some((key, index) => key !== expected[index])) {
    fail(`The ${label} receipt has an unexpected shape.`);
  }
}

function requiredString(value: unknown, field: string, max = 1024): string {
  if (typeof value !== "string" || !value.trim() || value.length > max || CONTROL_PATTERN.test(value)) {
    fail(`The calendar receipt has an invalid ${field}.`);
  }
  return value;
}

function optionalString(value: unknown, field: string, max: number): string | null {
  if (value === null) return null;
  return requiredString(value, field, max);
}

function positiveInteger(value: unknown, field: string, max = Number.MAX_SAFE_INTEGER): number {
  if (!Number.isSafeInteger(value) || Number(value) < 1 || Number(value) > max) {
    fail(`The calendar receipt has an invalid ${field}.`);
  }
  return Number(value);
}

function boundedInteger(value: unknown, field: string, min: number, max: number): number {
  if (!Number.isSafeInteger(value) || Number(value) < min || Number(value) > max) {
    fail(`The calendar receipt has an invalid ${field}.`);
  }
  return Number(value);
}

function booleanValue(value: unknown, field: string): boolean {
  if (typeof value !== "boolean") fail(`The calendar receipt has an invalid ${field}.`);
  return value;
}

function digest(value: unknown, field: string): string {
  const result = requiredString(value, field, 71);
  if (!DIGEST_PATTERN.test(result)) fail(`The calendar receipt has an invalid ${field}.`);
  return result;
}

function timestamp(value: unknown, field: string): string {
  const result = requiredString(value, field, 64);
  if (!Number.isFinite(Date.parse(result)) || !/(?:Z|\+00:00)$/i.test(result)) {
    fail(`The calendar receipt has an invalid ${field}.`);
  }
  return result;
}

function nullableTimestamp(value: unknown, field: string): string | null {
  return value === null ? null : timestamp(value, field);
}

type CalendarEventTime = { kind: "date" | "datetime"; value: string; epoch: number };

function eventTime(value: unknown, field: string): CalendarEventTime {
  const result = requiredString(value, field, 64);
  if (/^\d{4}-\d{2}-\d{2}$/.test(result)) {
    const epoch = Date.parse(`${result}T00:00:00Z`);
    if (!Number.isFinite(epoch) || new Date(epoch).toISOString().slice(0, 10) !== result) {
      fail(`The calendar receipt has an invalid ${field}.`);
    }
    return { kind: "date", value: result, epoch };
  }
  // Calendar event dateTime values are normalized to UTC by the backend. Keep
  // the timezone explicit and reject naive or offset-local timestamps rather
  // than silently changing the provider's event boundary.
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$/.test(result)) {
    fail(`The calendar receipt has an invalid ${field}.`);
  }
  const epoch = Date.parse(result);
  if (!Number.isFinite(epoch)) fail(`The calendar receipt has an invalid ${field}.`);
  return { kind: "datetime", value: result, epoch };
}

function safeErrorDetail(payload: unknown): CalendarApiErrorDetail {
  if (!isRecord(payload) || !isRecord(payload.detail)) {
    return { code: "calendar_request_failed", message: "The calendar request failed.", recovery_action: null };
  }
  const detail = payload.detail;
  return {
    code: typeof detail.code === "string" && detail.code.length <= 128 ? detail.code : "calendar_request_failed",
    message: typeof detail.message === "string" && detail.message.trim()
      ? detail.message.slice(0, 500)
      : "The calendar request failed.",
    recovery_action: detail.recovery_action === null || detail.recovery_action === undefined
      ? null
      : typeof detail.recovery_action === "string" && detail.recovery_action.length <= 128
        ? detail.recovery_action
        : null,
  };
}

async function calendarRequest<T>(path: string, init: RequestInit, validate: (value: unknown) => T): Promise<T> {
  const response = await apiFetch(`${API_URL}${path}`, init);
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = safeErrorDetail(payload);
    throw new CalendarApiError(response.status, detail.code, detail.message, detail.recovery_action);
  }
  try {
    return validate(payload);
  } catch (error) {
    if (error instanceof CalendarApiError) throw error;
    throw new CalendarApiError(200, "receipt_invalid", "The calendar response could not be validated.");
  }
}

function connection(value: unknown): CalendarConnectionMetadata {
  if (!isRecord(value)) fail("The connection receipt was not an object.");
  exactKeys(value, ["connection_id", "service", "label", "credential_fingerprint", "state", "revision", "created_at", "updated_at"], "connection");
  if (value.service !== CALENDAR_SERVICE) fail("The connection receipt has an unexpected service.");
  if (!(["preparing", "active", "revoked", "expired", "blocked", "blocked_cleanup"] as const).includes(value.state as never)) {
    fail("The connection receipt has an invalid state.");
  }
  return {
    connection_id: requiredString(value.connection_id, "connection ID"),
    service: CALENDAR_SERVICE,
    label: requiredString(value.label, "label", 200),
    credential_fingerprint: requiredString(value.credential_fingerprint, "credential fingerprint", 128),
    state: value.state as CalendarConnectionMetadata["state"],
    revision: positiveInteger(value.revision, "revision"),
    created_at: timestamp(value.created_at, "created_at"),
    updated_at: timestamp(value.updated_at, "updated_at"),
  };
}

function calendarOption(value: unknown): CalendarOption {
  if (!isRecord(value)) fail("The calendar option was not an object.");
  exactKeys(value, ["calendar_id", "summary"], "calendar option");
  return {
    calendar_id: requiredString(value.calendar_id, "calendar ID", 1024),
    summary: requiredString(value.summary, "calendar summary", 500),
  };
}

function verifyResponse(value: unknown): CalendarVerifyResponse {
  if (!isRecord(value)) fail("The calendar verification response was not an object.");
  exactKeys(value, ["connection", "calendars", "calendar_list_revision", "pages_read", "truncated", "provider_status"], "calendar verification");
  if (!Array.isArray(value.calendars) || value.calendars.length > 50) fail("The calendar verification list is invalid.");
  if (value.pages_read !== 1 || value.provider_status !== "verified") fail("The calendar verification receipt is invalid.");
  return {
    connection: connection(value.connection),
    calendars: value.calendars.map(calendarOption),
    calendar_list_revision: digest(value.calendar_list_revision, "calendar list revision"),
    pages_read: 1,
    truncated: booleanValue(value.truncated, "truncated"),
    provider_status: "verified",
  };
}

function consent(value: unknown): CalendarConsentMetadata {
  if (!isRecord(value)) fail("The consent receipt was not an object.");
  exactKeys(value, ["consent_id", "connection_id", "connection_revision", "goal_id", "goal_revision", "allowed_fields", "window_minutes", "max_events", "allow_remote_model", "expires_at", "state", "revision", "consent_digest", "created_at", "updated_at", ...(value.sync_metadata_limit === undefined ? [] : ["sync_metadata_limit"])], "consent");
  const allowedFields = value.allowed_fields;
  if (!Array.isArray(allowedFields) || allowedFields.length === 0 || allowedFields.some((field) => !["summary", "start", "end", "location", "description", "attendees"].includes(String(field)))) {
    fail("The consent allowed fields are invalid.");
  }
  if (!( ["active", "revoked", "expired", "consumed"] as const).includes(value.state as never)) fail("The consent state is invalid.");
  return {
    consent_id: requiredString(value.consent_id, "consent ID"),
    connection_id: requiredString(value.connection_id, "connection ID"),
    connection_revision: positiveInteger(value.connection_revision, "connection revision"),
    goal_id: requiredString(value.goal_id, "goal ID"),
    goal_revision: positiveInteger(value.goal_revision, "goal revision"),
    allowed_fields: allowedFields as CalendarConsentMetadata["allowed_fields"],
    window_minutes: boundedInteger(value.window_minutes, "window", 5, Number(value.sync_metadata_limit) > 0 ? 10080 : 1440),
    sync_metadata_limit: value.sync_metadata_limit === undefined ? 0 : boundedInteger(value.sync_metadata_limit, "sync metadata limit", 0, 50),
    max_events: boundedInteger(value.max_events, "max events", 1, 50),
    allow_remote_model: booleanValue(value.allow_remote_model, "remote model choice"),
    expires_at: timestamp(value.expires_at, "expiry"),
    state: value.state as CalendarConsentMetadata["state"],
    revision: positiveInteger(value.revision, "revision"),
    consent_digest: digest(value.consent_digest, "consent digest"),
    created_at: timestamp(value.created_at, "created_at"),
    updated_at: timestamp(value.updated_at, "updated_at"),
  };
}

function event(value: unknown): CalendarEventOption {
  if (!isRecord(value)) fail("The calendar event was not an object.");
  exactKeys(value, ["event_binding_id", "event_binding_revision", "event_key", "event_revision", "calendar_list_revision", "summary", "start", "end", "location", "description", "attendees"], "calendar event");
  if (value.attendees !== null && (!Array.isArray(value.attendees) || value.attendees.length > 50)) fail("The event attendees are invalid.");
  const attendees = value.attendees === null ? null : value.attendees.map((item) => requiredString(item, "attendee", 200));
  const start = eventTime(value.start, "event start");
  const end = eventTime(value.end, "event end");
  if (start.kind !== end.kind || end.epoch <= start.epoch) {
    fail("The calendar receipt has an invalid event interval.");
  }
  return {
    event_binding_id: requiredString(value.event_binding_id, "event binding ID"),
    event_binding_revision: positiveInteger(value.event_binding_revision, "event binding revision"),
    event_key: digest(value.event_key, "event key"),
    event_revision: digest(value.event_revision, "event revision"),
    calendar_list_revision: digest(value.calendar_list_revision, "calendar list revision"),
    summary: requiredString(value.summary, "event summary", 1200),
    start: start.value,
    end: end.value,
    location: optionalString(value.location, "event location", 1200),
    description: optionalString(value.description, "event description", 4000),
    attendees,
  };
}

function eventsResponse(value: unknown): CalendarEventListResponse {
  if (!isRecord(value)) fail("The event response was not an object.");
  exactKeys(value, ["events", "consent_id", "consent_revision", "connection_revision", "calendar_list_revision", "fetched_at", "pages_read", "truncated"], "calendar events");
  if (!Array.isArray(value.events) || value.events.length > 50) fail("The event response list is invalid.");
  const items = value.events.map(event);
  if (new Set(items.map((item) => item.event_binding_id)).size !== items.length) fail("The event response contains duplicate bindings.");
  const pages = boundedInteger(value.pages_read, "pages read", 1, 3);
  return {
    events: items,
    consent_id: requiredString(value.consent_id, "consent ID"),
    consent_revision: positiveInteger(value.consent_revision, "consent revision"),
    connection_revision: positiveInteger(value.connection_revision, "connection revision"),
    calendar_list_revision: digest(value.calendar_list_revision, "calendar list revision"),
    fetched_at: timestamp(value.fetched_at, "fetched_at"),
    pages_read: pages,
    truncated: booleanValue(value.truncated, "truncated"),
  };
}

function inputArtifact(value: unknown): CalendarInputArtifactMetadata {
  if (!isRecord(value)) fail("The preparation artifact receipt was not an object.");
  exactKeys(value, ["artifact_id", "typed_input_ref", "typed_input_digest", "capability_id", "goal_id", "goal_revision", "expires_at"], "preparation artifact");
  if (value.capability_id !== PREP_CAPABILITY) fail("The preparation artifact has an unexpected capability.");
  const typedInputRef = requiredString(value.typed_input_ref, "typed input reference", 512);
  if (!typedInputRef.startsWith("workspace-json:")) fail("The preparation artifact reference is not a workspace receipt.");
  return {
    artifact_id: requiredString(value.artifact_id, "artifact ID"),
    typed_input_ref: typedInputRef,
    typed_input_digest: digest(value.typed_input_digest, "typed input digest"),
    capability_id: PREP_CAPABILITY,
    goal_id: requiredString(value.goal_id, "artifact goal ID"),
    goal_revision: positiveInteger(value.goal_revision, "artifact goal revision"),
    expires_at: timestamp(value.expires_at, "artifact expiry"),
  };
}

function task(value: unknown): WorkBoardTask {
  if (!isRecord(value)) fail("The preparation task receipt was not an object.");
  if (value.capability_id !== PREP_CAPABILITY) fail("The preparation task has an unexpected capability.");
  requiredString(value.task_id, "task ID");
  requiredString(value.title, "task title", 200);
  requiredString(value.goal_id, "task goal ID");
  positiveInteger(value.goal_revision, "task goal revision");
  requiredString(value.input_artifact_id, "task artifact ID");
  return value as unknown as WorkBoardTask;
}

function prepResponse(value: unknown): CalendarPrepResponse {
  if (!isRecord(value)) fail("The preparation response was not an object.");
  exactKeys(value, ["input_artifact", "task", "idempotent_replay"], "preparation");
  const artifact = inputArtifact(value.input_artifact);
  const createdTask = task(value.task);
  if (createdTask.input_artifact_id !== artifact.artifact_id
    || createdTask.goal_id !== artifact.goal_id
    || createdTask.goal_revision !== artifact.goal_revision) {
    fail("The preparation receipt does not bind its artifact and task to the same goal.");
  }
  return {
    input_artifact: artifact,
    task: createdTask,
    idempotent_replay: booleanValue(value.idempotent_replay, "idempotent replay"),
  };
}

function occurrence(value: unknown): CalendarLatestOccurrence | null {
  if (value === null) return null;
  if (!isRecord(value)) fail("The schedule occurrence is invalid.");
  exactKeys(value, ["occurrence_id", "binding_revision", "slot_utc", "state", "task_id", "job_id", "failure_code", "recovery_action", "updated_at"], "schedule occurrence");
  if (!( ["reserved", "running", "coalesced", "succeeded", "blocked", "cancelled", "unknown"] as const).includes(value.state as never)) fail("The schedule occurrence state is invalid.");
  return {
    occurrence_id: requiredString(value.occurrence_id, "occurrence ID"),
    binding_revision: positiveInteger(value.binding_revision, "binding revision"),
    slot_utc: timestamp(value.slot_utc, "slot"),
    state: value.state as CalendarLatestOccurrence["state"],
    task_id: value.task_id === null ? null : requiredString(value.task_id, "occurrence task ID"),
    job_id: value.job_id === null ? null : requiredString(value.job_id, "occurrence job ID"),
    failure_code: value.failure_code === null ? null : requiredString(value.failure_code, "failure code", 128),
    recovery_action: value.recovery_action === null ? null : requiredString(value.recovery_action, "recovery action", 128),
    updated_at: timestamp(value.updated_at, "occurrence updated_at"),
  };
}

function cadence(value: unknown): CalendarCadence {
  if (!isRecord(value)) fail("The schedule cadence is invalid.");
  exactKeys(value, ["kind", "timezone", "daily_hour", "daily_minute"], "schedule cadence");
  if (!( ["5min", "hourly", "6h", "daily"] as const).includes(value.kind as never)) fail("The schedule cadence kind is invalid.");
  const daily = value.kind === "daily";
  if (daily && (typeof value.daily_hour !== "number" || typeof value.daily_minute !== "number")) fail("Daily cadence requires a time.");
  if (!daily && value.daily_hour !== null || !daily && value.daily_minute !== null) fail("Non-daily cadence cannot include a daily time.");
  if (daily) {
    boundedInteger(value.daily_hour, "daily hour", 0, 23);
    boundedInteger(value.daily_minute, "daily minute", 0, 59);
  }
  return {
    kind: value.kind as CalendarCadence["kind"],
    timezone: requiredString(value.timezone, "timezone", 128),
    daily_hour: value.daily_hour as number | null,
    daily_minute: value.daily_minute as number | null,
  };
}

function binding(value: unknown): GovernedScheduleBinding {
  if (!isRecord(value)) fail("The schedule binding was not an object.");
  exactKeys(value, ["binding_id", "scheduled_job_id", "capability_id", "action_type", "goal_id", "goal_revision", "input_artifact_id", "input_digest", "consent_kind", "consent_id", "consent_revision", "consent_digest", "cadence", "binding_revision", "expires_at", "state", "last_slot_utc", "created_at", "updated_at", "latest_occurrence"], "schedule binding");
  if (!( ["active", "paused", "revoked", "expired", "blocked"] as const).includes(value.state as never)) fail("The schedule state is invalid.");
  return {
    binding_id: requiredString(value.binding_id, "binding ID"),
    scheduled_job_id: requiredString(value.scheduled_job_id, "scheduled job ID"),
    capability_id: requiredString(value.capability_id, "schedule capability"),
    action_type: requiredString(value.action_type, "schedule action", 128),
    goal_id: requiredString(value.goal_id, "schedule goal ID"),
    goal_revision: positiveInteger(value.goal_revision, "schedule goal revision"),
    input_artifact_id: requiredString(value.input_artifact_id, "schedule artifact ID"),
    input_digest: digest(value.input_digest, "schedule input digest"),
    consent_kind: requiredString(value.consent_kind, "consent kind", 128),
    consent_id: requiredString(value.consent_id, "schedule consent ID"),
    consent_revision: positiveInteger(value.consent_revision, "schedule consent revision"),
    consent_digest: digest(value.consent_digest, "schedule consent digest"),
    cadence: cadence(value.cadence),
    binding_revision: positiveInteger(value.binding_revision, "binding revision"),
    expires_at: timestamp(value.expires_at, "schedule expiry"),
    state: value.state as GovernedScheduleBinding["state"],
    last_slot_utc: value.last_slot_utc === null ? null : timestamp(value.last_slot_utc, "last slot"),
    created_at: timestamp(value.created_at, "schedule created_at"),
    updated_at: timestamp(value.updated_at, "schedule updated_at"),
    latest_occurrence: occurrence(value.latest_occurrence),
  };
}

function envelope<T>(value: unknown, key: string, validate: (input: unknown) => T): T {
  if (!isRecord(value)) fail("The calendar response was not an object.");
  exactKeys(value, [key], "calendar response");
  return validate(value[key]);
}

function id(value: string): string {
  return encodeURIComponent(value);
}

export function validateCalendarConnectionResponse(payload: unknown): CalendarConnectionMetadata {
  return envelope(payload, "connection", connection);
}

export function validateCalendarConnectionsResponse(payload: unknown): CalendarConnectionMetadata[] {
  if (!isRecord(payload)) fail("The connection list response was not an object.");
  exactKeys(payload, ["connections"], "connection list");
  if (!Array.isArray(payload.connections)) fail("The connection list is invalid.");
  return payload.connections.map(connection);
}

export function validateCalendarVerifyResponse(payload: unknown): CalendarVerifyResponse {
  return verifyResponse(payload);
}

export function validateCalendarConsentResponse(payload: unknown): CalendarConsentMetadata {
  return envelope(payload, "consent", consent);
}

export function validateCalendarEventsResponse(payload: unknown): CalendarEventListResponse {
  return eventsResponse(payload);
}

export function validateCalendarPrepResponse(payload: unknown): CalendarPrepResponse {
  return prepResponse(payload);
}

export function validateCalendarBindingResponse(payload: unknown): GovernedScheduleBinding {
  return envelope(payload, "binding", binding);
}

export function validateCalendarBindingsResponse(payload: unknown): GovernedScheduleBinding[] {
  if (!isRecord(payload)) fail("The schedule list response was not an object.");
  exactKeys(payload, ["bindings"], "schedule list");
  if (!Array.isArray(payload.bindings) || payload.bindings.length > 100) fail("The schedule list is invalid.");
  return payload.bindings.map(binding);
}

export function validateCalendarExecution(value: unknown): CalendarExecutionProjection | null {
  if (value === null || value === undefined) return null;
  if (!isRecord(value)) return null;
  const required = ["capability_id", "job_id", "durable_status", "connection_id", "connection_revision", "consent_id", "consent_revision", "event_binding_id", "event_key", "event_revision", "calendar_list_revision", "read_1", "read_2", "effective_route", "artifact_id", "file_path", "content_sha256", "readback_id", "verified_at", "memory_status", "failure_code", "recovery_action"] as const;
  if (Object.keys(value).sort().join("|") !== [...required].sort().join("|")) return null;
  if (value.capability_id !== PREP_CAPABILITY) return null;
  const read = (input: unknown): CalendarReadReceipt | null => {
    if (input === null) return null;
    if (!isRecord(input)) return null;
    if (Object.keys(input).sort().join("|") !== ["status", "request_digest", "response_digest", "verified_at"].sort().join("|")) return null;
    if (!( ["succeeded", "blocked", "unknown"] as const).includes(input.status as never)) return null;
    return {
      status: input.status as CalendarReadReceipt["status"],
      request_digest: digest(input.request_digest, "read request digest"),
      response_digest: input.response_digest === null ? null : digest(input.response_digest, "read response digest"),
      verified_at: nullableTimestamp(input.verified_at, "read verification time"),
    };
  };
  const route = (input: unknown): CalendarEffectiveRoute | null => {
    if (input === null) return null;
    if (!isRecord(input)) return null;
    if (Object.keys(input).sort().join("|") !== ["runtime_path", "provider", "model", "upstream_provider", "profile_id", "admission_digest", "status", "cost_microusd"].sort().join("|")) return null;
    if (input.runtime_path !== "strategist_agent" || input.provider !== "openrouter") return null;
    return {
      runtime_path: "strategist_agent",
      provider: "openrouter",
      model: requiredString(input.model, "effective model", 256),
      upstream_provider: requiredString(input.upstream_provider, "upstream provider", 256),
      profile_id: requiredString(input.profile_id, "profile ID", 256),
      admission_digest: digest(input.admission_digest, "admission digest"),
      status: requiredString(input.status, "effective route status", 256),
      cost_microusd: input.cost_microusd === null ? null : boundedInteger(input.cost_microusd, "cost", 0, Number.MAX_SAFE_INTEGER),
    };
  };
  if (value.memory_status !== null && value.memory_status !== "no_learning") return null;
  return {
    capability_id: PREP_CAPABILITY,
    job_id: requiredString(value.job_id, "calendar job ID"),
    durable_status: requiredString(value.durable_status, "durable status", 256),
    connection_id: value.connection_id === null ? null : requiredString(value.connection_id, "connection ID"),
    connection_revision: value.connection_revision === null ? null : positiveInteger(value.connection_revision, "connection revision"),
    consent_id: value.consent_id === null ? null : requiredString(value.consent_id, "consent ID"),
    consent_revision: value.consent_revision === null ? null : positiveInteger(value.consent_revision, "consent revision"),
    event_binding_id: value.event_binding_id === null ? null : requiredString(value.event_binding_id, "event binding ID"),
    event_key: value.event_key === null ? null : digest(value.event_key, "event key"),
    event_revision: value.event_revision === null ? null : digest(value.event_revision, "event revision"),
    calendar_list_revision: value.calendar_list_revision === null ? null : digest(value.calendar_list_revision, "calendar list revision"),
    read_1: read(value.read_1),
    read_2: read(value.read_2),
    effective_route: route(value.effective_route),
    artifact_id: value.artifact_id === null ? null : requiredString(value.artifact_id, "artifact ID"),
    file_path: value.file_path === null ? null : requiredString(value.file_path, "artifact path", 512),
    content_sha256: value.content_sha256 === null ? null : digest(value.content_sha256, "artifact digest"),
    readback_id: value.readback_id === null ? null : requiredString(value.readback_id, "readback ID"),
    verified_at: nullableTimestamp(value.verified_at, "verification time"),
    memory_status: value.memory_status as "no_learning" | null,
    failure_code: value.failure_code === null ? null : requiredString(value.failure_code, "failure code", 128),
    recovery_action: value.recovery_action === null ? null : requiredString(value.recovery_action, "recovery action", 128),
  };
}

export function validateCalendarResultPreview(value: unknown): CalendarResultPreview | null {
  if (!isRecord(value)) return null;
  const keys = ["schema_version", "capability_id", "artifact_id", "readback_id", "file_path", "content_sha256", "event_key", "event_revision", "summary", "agenda", "questions", "risks", "preparation_steps", ...(value.related_sources === undefined ? [] : ["related_sources"])] as const;
  if (Object.keys(value).sort().join("|") !== [...keys].sort().join("|")) return null;
  if (value.schema_version !== 1 || value.capability_id !== PREP_CAPABILITY) return null;
  const boundedList = (input: unknown): string[] | null => {
    if (!Array.isArray(input) || input.length > 8) return null;
    const items = input.map((item) => typeof item === "string" && item.length <= 400 && !CONTROL_PATTERN.test(item) ? item : null);
    return items.every((item): item is string => item !== null) ? items : null;
  };
  const agenda = boundedList(value.agenda);
  const questions = boundedList(value.questions);
  const risks = boundedList(value.risks);
  const preparationSteps = boundedList(value.preparation_steps);
  if (!agenda || !questions || !risks || !preparationSteps) return null;
  const result = {
    schema_version: 1 as const,
    capability_id: PREP_CAPABILITY,
    ...(value.related_sources === undefined ? {} : { related_sources: relatedSources(value.related_sources) }),
    artifact_id: requiredString(value.artifact_id, "preview artifact ID"),
    readback_id: requiredString(value.readback_id, "preview readback ID"),
    file_path: requiredString(value.file_path, "preview artifact path", 512),
    content_sha256: digest(value.content_sha256, "preview digest"),
    event_key: digest(value.event_key, "preview event key"),
    event_revision: digest(value.event_revision, "preview event revision"),
    summary: requiredString(value.summary, "preview summary", 1200),
    agenda,
    questions,
    risks,
    preparation_steps: preparationSteps,
  };
  if (new TextEncoder().encode(JSON.stringify(result)).byteLength > 64 * 1024) return null;
  return result;
}

export function listCalendarConnections(signal?: AbortSignal): Promise<CalendarConnectionMetadata[]> {
  return calendarRequest("/api/calendar/connections", { method: "GET", signal }, validateCalendarConnectionsResponse);
}

export function createCalendarConnection(request: CreateCalendarConnectionRequest, signal?: AbortSignal): Promise<CalendarConnectionMetadata> {
  return calendarRequest("/api/calendar/connections", { method: "POST", signal, headers: { "Content-Type": "application/json" }, body: JSON.stringify(request) }, validateCalendarConnectionResponse);
}

export function verifyCalendarConnection(connectionId: string, request: CalendarConnectionMutationRequest, signal?: AbortSignal): Promise<CalendarVerifyResponse> {
  return calendarRequest(`/api/calendar/connections/${id(connectionId)}/verify`, { method: "POST", signal, headers: { "Content-Type": "application/json" }, body: JSON.stringify(request) }, validateCalendarVerifyResponse);
}

export function revokeCalendarConnection(connectionId: string, request: CalendarConnectionMutationRequest, signal?: AbortSignal): Promise<CalendarConnectionMetadata> {
  return calendarRequest(`/api/calendar/connections/${id(connectionId)}`, { method: "DELETE", signal, headers: { "Content-Type": "application/json" }, body: JSON.stringify(request) }, validateCalendarConnectionResponse);
}

export function listCalendars(connectionId: string, signal?: AbortSignal): Promise<CalendarVerifyResponse> {
  return calendarRequest(`/api/calendar/connections/${id(connectionId)}/calendars`, { method: "GET", signal }, validateCalendarVerifyResponse);
}

export function createReadConsent(request: CreateCalendarReadConsentRequest, signal?: AbortSignal): Promise<CalendarConsentMetadata> {
  return calendarRequest("/api/calendar/read-consents", { method: "POST", signal, headers: { "Content-Type": "application/json" }, body: JSON.stringify(request) }, validateCalendarConsentResponse);
}

export function revokeReadConsent(consentId: string, request: CalendarConnectionMutationRequest, signal?: AbortSignal): Promise<CalendarConsentMetadata> {
  return calendarRequest(`/api/calendar/read-consents/${id(consentId)}`, { method: "DELETE", signal, headers: { "Content-Type": "application/json" }, body: JSON.stringify(request) }, validateCalendarConsentResponse);
}

export function listCalendarEvents(connectionId: string, consentId: string, signal?: AbortSignal): Promise<CalendarEventListResponse> {
  const query = new URLSearchParams({ consent_id: consentId });
  return calendarRequest(`/api/calendar/connections/${id(connectionId)}/events?${query.toString()}`, { method: "GET", signal }, validateCalendarEventsResponse);
}

export function createCalendarPrep(request: CreateCalendarPrepRequest, signal?: AbortSignal): Promise<CalendarPrepResponse> {
  return calendarRequest("/api/calendar/prep", { method: "POST", signal, headers: { "Content-Type": "application/json" }, body: JSON.stringify({ ...request, input: normalizeConnectedRequest(request.input) }) }, (payload) => {
    const result = validateCalendarPrepResponse(payload);
    if (result.input_artifact.capability_id !== PREP_CAPABILITY
      || result.input_artifact.goal_id !== request.input.goal_id
      || result.input_artifact.goal_revision !== request.input.goal_revision
      || result.task.capability_id !== PREP_CAPABILITY
      || result.task.goal_id !== request.input.goal_id
      || result.task.goal_revision !== request.input.goal_revision) {
      fail("The preparation receipt does not match the selected goal and capability.");
    }
    return result;
  });
}

export function createGovernedSchedule(request: CreateCalendarScheduleRequest, signal?: AbortSignal): Promise<GovernedScheduleBinding> {
  return calendarRequest("/api/calendar/schedules", { method: "POST", signal, headers: { "Content-Type": "application/json" }, body: JSON.stringify(request) }, validateCalendarBindingResponse);
}

export function listGovernedSchedules(signal?: AbortSignal): Promise<GovernedScheduleBinding[]> {
  return calendarRequest("/api/governed-schedules", { method: "GET", signal }, validateCalendarBindingsResponse);
}

export type GovernedScheduleControlRequest =
  | { action: "pause" | "resume"; expected_binding_revision: number; idempotency_key: string }
  | { expected_binding_revision: number; idempotency_key: string; reason: string };

export function controlGovernedSchedule(bindingId: string, request: Extract<GovernedScheduleControlRequest, { action: "pause" | "resume" }>, signal?: AbortSignal): Promise<GovernedScheduleBinding> {
  return calendarRequest(`/api/governed-schedules/${id(bindingId)}`, { method: "PATCH", signal, headers: { "Content-Type": "application/json" }, body: JSON.stringify(request) }, validateCalendarBindingResponse);
}

export function revokeGovernedSchedule(bindingId: string, request: Extract<GovernedScheduleControlRequest, { reason: string }>, signal?: AbortSignal): Promise<GovernedScheduleBinding> {
  return calendarRequest(`/api/governed-schedules/${id(bindingId)}/revoke`, { method: "POST", signal, headers: { "Content-Type": "application/json" }, body: JSON.stringify(request) }, validateCalendarBindingResponse);
}
