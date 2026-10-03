import { API_URL } from "../config/constants";
import { apiFetch } from "./api";

export const MOLTBOOK_PATH = "/api/capabilities/moltbook";
export interface MoltbookConnection {
  configured: boolean; id?: string; revision?: number; mode: string; account_name?: string;
  account_id?: string; active_job_id?: string | null; setup_request_key?: string;
  cooldown_until?: string | null;
  consent?: { actions?: string[]; expires_at?: string; goal_id?: string; goal_revision?: number; session?: string; request_key?: string; request?: Record<string, unknown> };
  credential_is_consent: false; no_learning: true;
}
export interface MoltbookJob {
  job_id: string; status: string; revision: number; deadline_at: string;
  attempt_count: number; no_learning: true; checkpoints: { checkpoint_id: string; payload?: Record<string, unknown> }[];
  lease: { fencing_token: number }; artifacts?: { content_sha256: string }[];
  declared_authority?: { operation?: string }; draft?: { operation: string; fields: Record<string, string>; review: Record<string, unknown> };
  approval?: { id: string; status: string; scope_digest: string }; manual_answer?: string;
  approvals?: { id: string; status: string; scope_digest: string }[];
  admission_request?: Record<string, unknown>;
}
export interface MoltbookPending { method: "PUT" | "POST"; path: string; body: Record<string, unknown> }
const bytes = (raw: string) => new TextEncoder().encode(raw).length;
const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);
const id = (value: unknown): value is string => typeof value === "string" && /^[A-Za-z0-9_.:-]{1,128}$/.test(value);
const operationPath = /^\/jobs\/moltbook:[a-f0-9]{40}\/(execute|approval|answer|cancel|recover)$/;
export function moltbookStorageKey(principal: string, session: string) {
  if (!id(principal) || !id(session)) throw Error("Current owner and login required");
  return `seraph.moltbook.v1:${encodeURIComponent(principal)}:${encodeURIComponent(session)}`;
}
function validate(value: unknown): MoltbookPending {
  if (!record(value) || Object.keys(value).sort().join() !== "body,method,path" || !record(value.body)
      || typeof value.path !== "string" || !["PUT", "POST"].includes(String(value.method))) throw Error("Retained Moltbook request corrupt");
  const keys = Object.keys(value.body).sort().join();
  if (value.method === "PUT" && value.path === "/connection") {
    if (keys !== "expected_revision,request_key,vault_key" || !id(value.body.request_key)
        || typeof value.body.vault_key !== "string" || value.body.vault_key.length > 256
        || !(value.body.expected_revision === null || Number.isSafeInteger(value.body.expected_revision))) throw Error("Retained connection import corrupt");
  } else if (value.method === "POST" && value.path === "/connection/consent") {
    if (keys !== "actions,duration_seconds,expected_revision,goal_id,goal_revision,no_redistribution,personal_noncommercial,request_key"
        || !id(value.body.request_key) || !id(value.body.goal_id) || !Number.isSafeInteger(value.body.goal_revision)
        || !Number.isSafeInteger(value.body.expected_revision) || !Number.isSafeInteger(value.body.duration_seconds)
        || !Array.isArray(value.body.actions) || value.body.actions.length > 7
        || !value.body.actions.every(action => ["inspect", "feed", "post", "comments", "community", "create_post", "create_comment"].includes(action))
        || value.body.personal_noncommercial !== true || value.body.no_redistribution !== true) throw Error("Retained finite consent corrupt");
  } else if (value.method === "POST" && (value.path === "/reads" || value.path === "/writes")) {
    const write = value.path === "/writes";
    const expected = write ? "community_digest,community_job_id,expected_revision,fields,goal_id,goal_revision,introductions_allowed,operation,public_only,request_key"
      : "expected_revision,fields,goal_id,goal_revision,operation,request_key";
    if (keys !== expected
        || !id(value.body.goal_id) || !id(value.body.request_key) || !record(value.body.fields)
        || !(write ? ["create_post", "create_comment"] : ["inspect", "feed", "post", "comments", "community"]).includes(String(value.body.operation))
        || !Number.isSafeInteger(value.body.goal_revision) || !Number.isSafeInteger(value.body.expected_revision)) throw Error("Retained read request corrupt");
    if (write && (typeof value.body.community_job_id !== "string" || !/^moltbook:[a-f0-9]{40}$/.test(value.body.community_job_id) || typeof value.body.community_digest !== "string"
        || !/^[a-f0-9]{64}$/.test(value.body.community_digest) || value.body.introductions_allowed !== true || value.body.public_only !== true)) throw Error("Retained public target review corrupt");
  } else if (value.method === "POST" && operationPath.test(value.path)) {
    const action = value.path.split("/").pop();
    if (action === "approval") {
      if (keys !== "approval_id,decision" || !id(value.body.approval_id) || !["approved", "denied"].includes(String(value.body.decision))) throw Error("Retained approval corrupt");
    } else if (action === "answer") {
      if (keys !== "answer,request_key" || !id(value.body.request_key) || typeof value.body.answer !== "string"
          || !/^-?(?:0|[1-9][0-9]{0,26})\.[0-9]{2}$/.test(value.body.answer)) throw Error("Retained manual answer corrupt");
    } else if (action === "cancel") {
      if (keys !== "expected_revision,fencing_token,request_key" || !id(value.body.request_key)
          || !Number.isSafeInteger(value.body.expected_revision) || !Number.isSafeInteger(value.body.fencing_token)) throw Error("Retained cancel corrupt");
    } else if (action === "execute") {
      if (keys !== "expected_phase,fencing_token,request_key" || !id(value.body.request_key)
          || !Number.isSafeInteger(value.body.fencing_token)
          || !["unattempted", "awaiting_create_approval", "awaiting_verify_approval"].includes(String(value.body.expected_phase))) throw Error("Retained exact execution phase corrupt");
    } else if (keys !== "") throw Error("Retained original-job action corrupt");
  } else {
    throw Error("Retained Moltbook path is outside the fixed controls");
  }
  if (bytes(JSON.stringify(value)) > 16384) throw Error("Retained Moltbook request exceeds 16 KiB");
  return value as unknown as MoltbookPending;
}
export function readMoltbookPending(key: string): MoltbookPending | null {
  const raw = sessionStorage.getItem(key);
  if (raw === null) return null;
  if (bytes(raw) > 16384) throw Error("Retained Moltbook request exceeds 16 KiB");
  return validate(JSON.parse(raw));
}
export function retainMoltbookPending(key: string, pending: MoltbookPending) {
  validate(pending);
  const raw = JSON.stringify(pending);
  sessionStorage.setItem(key, raw);
  if (sessionStorage.getItem(key) !== raw) throw Error("Exact request retention failed; no request sent");
  readMoltbookPending(key);
}
export function clearMoltbookPending(key: string) {
  sessionStorage.removeItem(key);
  if (sessionStorage.getItem(key) !== null) throw Error("Confirmed request could not be cleared");
}
export async function moltbookRequest(path: string, init: RequestInit = {}, signal?: AbortSignal): Promise<unknown> {
  const response = await apiFetch(API_URL + MOLTBOOK_PATH + path, { ...init, signal });
  if (!response.ok) {
    const value = await response.json().catch(() => null);
    throw Error(record(value) && record(value.detail) && typeof value.detail.code === "string"
      ? value.detail.code : `Moltbook result unconfirmed (${response.status})`);
  }
  return response.json();
}
export async function submitMoltbook(key: string, pending: MoltbookPending, signal?: AbortSignal) {
  retainMoltbookPending(key, pending);
  const value = await moltbookRequest(pending.path, { method: pending.method,
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(pending.body) }, signal);
  if (!record(value)) throw Error("Canonical Moltbook receipt unavailable");
  if (pending.path === "/connection" && (value.setup_request_key !== pending.body.request_key || value.configured !== true)) throw Error("Connection import receipt mismatch");
  if (pending.path === "/connection/consent" && (!record(value.consent) || value.consent.request_key !== pending.body.request_key)) throw Error("Original finite consent receipt mismatch");
  if (["/reads", "/writes"].includes(pending.path) && (!id(value.job_id) || !record(value.idempotency) || value.idempotency.key !== pending.body.request_key)) throw Error("Original admission receipt mismatch");
  if (operationPath.test(pending.path)) {
    const action = pending.path.split("/").pop();
    if (action === "approval") {
      if (value.approval_id !== pending.body.approval_id || value.status !== pending.body.decision) throw Error("Exact approval receipt mismatch");
    } else if (pending.path !== `/jobs/${value.job_id}/${action}`) throw Error("Original job receipt mismatch");
  }
  clearMoltbookPending(key);
  return value;
}

export function originalExecution(job: MoltbookJob): MoltbookPending {
  const checkpoint = job.checkpoints.find(value => value.checkpoint_id === "moltbook:state")?.payload;
  return { method: "POST", path: `/jobs/${job.job_id}/execute`, body: { request_key: crypto.randomUUID(),
    expected_phase: checkpoint?.phase ?? "unattempted", fencing_token: job.lease.fencing_token } };
}

export function pendingApplied(job: MoltbookJob, pending: MoltbookPending): boolean {
  if (pending.path === "/writes") return !!job.admission_request && equalMoltbookBody(job.admission_request, pending.body);
  if (!pending.path.startsWith(`/jobs/${job.job_id}/`)) return false;
  const value = job.checkpoints.find(item => item.checkpoint_id === "moltbook:state")?.payload;
  if (!value) return false;
  if (pending.path.endsWith("/execute")) return Array.isArray(value.executions) && value.executions.some(receipt => record(receipt)
    && receipt.request_key === pending.body.request_key && receipt.phase === pending.body.expected_phase && receipt.fencing_token === pending.body.fencing_token);
  if (pending.path.endsWith("/answer")) return value.answer_request_key === pending.body.request_key && job.manual_answer === pending.body.answer;
  if (pending.path.endsWith("/cancel")) return record(value.cancel_request) && value.cancel_request.request_key === pending.body.request_key
    && value.cancel_request.original_revision === pending.body.expected_revision && value.cancel_request.fencing_token === pending.body.fencing_token;
  if (pending.path.endsWith("/approval")) return (job.approvals ?? []).some(receipt => receipt.id === pending.body.approval_id
    && (receipt.status === pending.body.decision || (pending.body.decision === "approved" && receipt.status === "consumed")));
  return pending.path.endsWith("/recover") && record(value.recovery) && value.recovery.no_http_replay === true;
}

export function equalMoltbookBody(a: Record<string, unknown>, b: Record<string, unknown>): boolean {
  const stable = (value: unknown): unknown => Array.isArray(value) ? value.map(stable)
    : record(value) ? Object.fromEntries(Object.keys(value).sort().map(key => [key, stable(value[key])])) : value;
  return JSON.stringify(stable(a)) === JSON.stringify(stable(b));
}
