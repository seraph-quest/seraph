import { API_URL } from "../config/constants";
import { apiFetch } from "./api";
import { CalendarApiError } from "./calendar";

export type RescheduleRole = "calendar_reschedule_read" | "calendar_reschedule_write";
export interface RescheduleProfile {
  connection_id: string; service: RescheduleRole; label: string; revision: number;
  state: "preparing" | "active" | "revoked" | "blocked_cleanup";
  scope_status: string; declared_scopes: string[]; verified_setup_job_id: string | null;
  provider_contact: false; setup_is_write_permission: false;
}
export interface ExactEventTime { dateTime: string; timeZone: string }
export interface RescheduleConsent {
  consent_id: string; revision: number; state: "active" | "revoked"; expires_at: string;
  goal_id: string; goal_revision: number; event_binding_id: string; event_binding_revision: number;
  read_connection_id: string; read_connection_revision: number; write_connection_id: string; write_connection_revision: number;
  provider_contact: false;
}
export interface ReschedulePreview {
  approval_id: string; approval_status: string; decision_digest: string;
  expires_at: number; request_digest: string; source_digest: string;
  account_email: string; calendar_id: string; event_id: string; title: string;
  old_start: ExactEventTime; old_end: ExactEventTime; new_start: ExactEventTime; new_end: ExactEventTime;
  old_start_utc: string; old_end_utc: string; new_start_utc: string; new_end_utc: string;
  source_etag: string; marker_key: string; marker_value: string; protected_digest: string;
  notification_policy: "sendUpdates=none; provider reminders may still produce messages";
}
export interface RescheduleJob {
  job_id: string; request_uuid: string; kind: "calendar_reschedule_v1" | "calendar_reschedule_identity_v1" | "calendar_reschedule_observation_v1";
  status: string; revision: number; goal_id: string; goal_revision: number; deadline_at: string;
  source_task_id: string | null; original_job_id: string | null; outcome: string | null;
  contact_may_have_occurred: boolean; contacts_spent: number; transport_quiescent: boolean;
  cancel_requested: boolean; cancel_request_uuid: string | null; failure_reason: string | null;
  private_read_available: boolean; private_read_reason: string | null;
  no_learning: true; model_used: false; effective_route: "google_calendar_https";
  preview?: ReschedulePreview;
  observations?: { auxiliary_job_id: string; outcome: "verified_reschedule_observation" | "unknown_observation"; no_learning: true }[];
}

const base = "/api/capabilities/calendar/reschedule";
const digestPattern = /^[a-f0-9]{64}$/;
function invalid(): never { throw new CalendarApiError(200, "calendar_reschedule_receipt_invalid", "The original Calendar receipt is unconfirmed."); }
function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) invalid();
  return value as Record<string, unknown>;
}
function string(value: unknown, maximum = 256, empty = false): value is string {
  return typeof value === "string" && (empty || value.length > 0) && new TextEncoder().encode(value).length <= maximum && !/[\u0000-\u0008\u000b-\u001f\u007f]/.test(value);
}
function int(value: unknown, min = 1): value is number { return Number.isSafeInteger(value) && Number(value) >= min; }
function stamp(value: unknown): value is string { return string(value, 128) && Number.isFinite(Date.parse(value)) && /(?:Z|\+00:00)$/.test(value); }
function nullable(value: unknown, maximum = 256): boolean { return value === null || string(value, maximum); }
export function rescheduleScopes(role: RescheduleRole): string[] {
  return ["https://www.googleapis.com/auth/calendar.events.owned" + (role === "calendar_reschedule_read" ? ".readonly" : ""), "https://www.googleapis.com/auth/calendar.calendarlist.readonly", "openid", "email"];
}
export function rescheduleProfile(value: unknown): RescheduleProfile {
  const v = record(value);
  if (!string(v.connection_id) || !["calendar_reschedule_read", "calendar_reschedule_write"].includes(String(v.service)) || !string(v.label, 200) || !int(v.revision)
    || !["preparing", "active", "revoked", "blocked_cleanup"].includes(String(v.state)) || !string(v.scope_status) || !nullable(v.verified_setup_job_id)
    || v.provider_contact !== false || v.setup_is_write_permission !== false || !Array.isArray(v.declared_scopes)
    || JSON.stringify([...v.declared_scopes].sort()) !== JSON.stringify(rescheduleScopes(v.service as RescheduleRole).sort())) invalid();
  return v as unknown as RescheduleProfile;
}
export function rescheduleConsent(value: unknown): RescheduleConsent {
  const v = record(value);
  if (!string(v.consent_id) || !int(v.revision) || !["active", "revoked"].includes(String(v.state)) || !stamp(v.expires_at) || v.provider_contact !== false
    || !string(v.goal_id) || !int(v.goal_revision) || !string(v.event_binding_id) || !int(v.event_binding_revision)
    || !string(v.read_connection_id) || !int(v.read_connection_revision) || !string(v.write_connection_id) || !int(v.write_connection_revision)) invalid();
  return v as unknown as RescheduleConsent;
}
function exactTime(value: unknown): ExactEventTime {
  const v = record(value);
  if (Object.keys(v).length !== 2 || !string(v.timeZone, 128) || !string(v.dateTime, 128)
    || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:Z|[+-]\d{2}:\d{2})$/.test(v.dateTime)
    || v.dateTime.endsWith("-00:00") || !Number.isFinite(Date.parse(v.dateTime))) invalid();
  // The server's ZoneInfo roundtrip proves DST/offset validity. Never repair
  // the literal offset here using browser-local date conversion.
  return v as unknown as ExactEventTime;
}
export function rescheduleJob(value: unknown): RescheduleJob {
  const v = record(value);
  if (!string(v.job_id) || !string(v.request_uuid) || !["calendar_reschedule_v1", "calendar_reschedule_identity_v1", "calendar_reschedule_observation_v1"].includes(String(v.kind))
    || !string(v.status) || !int(v.revision) || !string(v.goal_id) || !int(v.goal_revision) || !stamp(v.deadline_at)
    || !nullable(v.source_task_id) || !nullable(v.original_job_id) || !nullable(v.outcome) || !nullable(v.failure_reason)
    || !nullable(v.cancel_request_uuid) || !nullable(v.private_read_reason) || !int(v.contacts_spent, 0) || Number(v.contacts_spent) > 13
    || [v.contact_may_have_occurred, v.transport_quiescent, v.cancel_requested, v.private_read_available].some(x => typeof x !== "boolean")
    || v.no_learning !== true || v.model_used !== false || v.effective_route !== "google_calendar_https") invalid();
  if (v.preview !== undefined) {
    if (v.private_read_available !== true) invalid();
    const p = record(v.preview);
    if (!string(p.approval_id) || !string(p.approval_status) || !digestPattern.test(String(p.decision_digest)) || !Number.isFinite(p.expires_at)
      || !digestPattern.test(String(p.request_digest)) || !digestPattern.test(String(p.source_digest)) || !digestPattern.test(String(p.protected_digest))
      || !string(p.account_email, 254) || !string(p.calendar_id, 1024) || !string(p.event_id, 512) || !string(p.title, 1024, true) || !string(p.source_etag, 512)
      || !/^seraphReschedule_[a-f0-9]{24}$/.test(String(p.marker_key)) || !digestPattern.test(String(p.marker_value))
      || p.notification_policy !== "sendUpdates=none; provider reminders may still produce messages"
      || ![p.old_start_utc, p.old_end_utc, p.new_start_utc, p.new_end_utc].every(stamp)) invalid();
    [p.old_start, p.old_end, p.new_start, p.new_end].forEach(exactTime);
  }
  if (v.observations !== undefined && (!Array.isArray(v.observations) || v.observations.length > 16 || v.observations.some(item => {
    const o = record(item); return !string(o.auxiliary_job_id) || !["verified_reschedule_observation", "unknown_observation"].includes(String(o.outcome)) || o.no_learning !== true;
  }))) invalid();
  return v as unknown as RescheduleJob;
}
async function request<T>(path: string, validate: (value: unknown) => T, body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await apiFetch(API_URL + base + path, body === undefined ? { method: "GET", signal } : { method: "POST", signal, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const value = await response.json().catch(() => null);
  if (!response.ok) {
    const outer = value && typeof value === "object" ? value as Record<string, unknown> : {};
    const detail = outer.detail && typeof outer.detail === "object" ? outer.detail as Record<string, unknown> : {};
    throw new CalendarApiError(response.status, string(detail.code, 128) ? detail.code : "calendar_reschedule_request_failed", string(detail.message, 500) ? detail.message : "The Calendar request is unconfirmed.", string(detail.recovery_action, 128) ? detail.recovery_action : null);
  }
  return validate(value);
}
export function listRescheduleProfiles(signal?: AbortSignal) {
  return request("/profiles", value => { const v = record(value); if (v.provider_contact !== false || !Array.isArray(v.profiles) || v.profiles.length > 32) invalid(); return v.profiles.map(rescheduleProfile); }, undefined, signal);
}
export function importRescheduleProfile(body: unknown, signal?: AbortSignal) { return request("/profiles", value => rescheduleProfile(record(value).profile), body, signal); }
export function recoverRescheduleProfile(uuid: string, signal?: AbortSignal) { return request("/profiles/recovery/" + encodeURIComponent(uuid), value => { const v = record(value); if (v.provider_contact !== false) invalid(); return v.profile === null ? null : rescheduleProfile(v.profile); }, undefined, signal); }
export function revokeRescheduleProfile(id: string, body: unknown, signal?: AbortSignal) { return request("/profiles/" + encodeURIComponent(id) + "/revoke", value => rescheduleProfile(record(value).profile), body, signal); }
export function verifyReschedulePair(body: unknown, signal?: AbortSignal) { return request("/profiles/verify-pair", rescheduleJob, body, signal); }
export function createRescheduleConsent(body: unknown, signal?: AbortSignal) { return request("/consents", value => rescheduleConsent(record(value).consent), body, signal); }
export function revokeRescheduleConsent(id: string, body: unknown, signal?: AbortSignal) { return request("/consents/" + encodeURIComponent(id) + "/revoke", value => rescheduleConsent(record(value).consent), body, signal); }
export function createRescheduleTask(body: unknown, signal?: AbortSignal) { return request("/tasks", value => { const v = record(value), task = record(v.task); if (!string(task.task_id) || !int(task.task_revision)) invalid(); return task as { task_id: string; task_revision: number }; }, body, signal); }
export function previewReschedule(body: unknown, signal?: AbortSignal) { return request("/operations/preview", rescheduleJob, body, signal); }
export function readReschedule(id: string, signal?: AbortSignal) { return request("/operations/" + encodeURIComponent(id), rescheduleJob, undefined, signal); }
export function inspectPrivateReschedule(id: string, signal?: AbortSignal) { return request("/operations/" + encodeURIComponent(id) + "/private", rescheduleJob, undefined, signal); }
export function actReschedule(id: string, action: "decision" | "execute" | "cancel" | "observe", body: unknown, signal?: AbortSignal) { return request("/operations/" + encodeURIComponent(id) + "/" + action, rescheduleJob, body, signal); }
export function recoverRescheduleOperation(kind: RescheduleJob["kind"], uuid: string, signal?: AbortSignal) { return request("/operations/recovery/" + encodeURIComponent(kind) + "/" + encodeURIComponent(uuid), value => { const v = record(value); if (v.provider_contact !== false) invalid(); return v.job === null ? null : rescheduleJob(v.job); }, undefined, signal); }
