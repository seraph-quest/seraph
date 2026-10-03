import { API_URL } from "../config/constants";
import { apiFetch } from "./api";

export const MOLTBOOK_PATH = "/api/capabilities/moltbook";
export interface MoltbookConnection {
  configured: boolean; id?: string; revision?: number; mode: string; account_name?: string;
  account_id?: string; active_job_id?: string | null; setup_request_key?: string;
  consent?: { actions?: string[]; expires_at?: string; goal_id?: string; goal_revision?: number; session?: string };
  credential_is_consent: false; no_learning: true;
}
export interface MoltbookJob {
  job_id: string; status: string; revision: number; deadline_at: string;
  attempt_count: number; no_learning: true; checkpoints: { checkpoint_id: string; payload?: Record<string, unknown> }[];
}
export interface MoltbookPending { method: "PUT" | "POST"; path: string; body: Record<string, unknown> }
const bytes = (raw: string) => new TextEncoder().encode(raw).length;
const record = (value: unknown): value is Record<string, unknown> => !!value && typeof value === "object" && !Array.isArray(value);
const id = (value: unknown): value is string => typeof value === "string" && /^[A-Za-z0-9_.:-]{1,128}$/.test(value);
const operationPath = /^\/jobs\/moltbook:[a-f0-9]{40}\/execute$/;
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
  } else if (value.method === "POST" && value.path === "/reads") {
    if (keys !== "expected_revision,fields,goal_id,goal_revision,operation,request_key"
        || !id(value.body.goal_id) || !id(value.body.request_key) || !record(value.body.fields)
        || !["inspect", "feed", "post", "comments", "community"].includes(String(value.body.operation))
        || !Number.isSafeInteger(value.body.goal_revision) || !Number.isSafeInteger(value.body.expected_revision)) throw Error("Retained read request corrupt");
  } else if (!(value.method === "POST" && operationPath.test(value.path) && keys === "")) {
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
  if (pending.path === "/reads" && (!id(value.job_id) || !record(value.idempotency) || value.idempotency.key !== pending.body.request_key)) throw Error("Original read admission receipt mismatch");
  if (operationPath.test(pending.path) && pending.path !== `/jobs/${value.job_id}/execute`) throw Error("Original job receipt mismatch");
  clearMoltbookPending(key);
  return value;
}
