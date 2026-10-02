import { API_URL } from "../config/constants";
import { apiFetch } from "./api";

export interface SetupProgress {
  starter: "local_snapshot" | "public_watch";
  step: "choose" | "preview" | "goal_saved" | "watch_saved" | "task_saved" | "admitted" | "result_opened";
  journey_id: string;
  title: string;
  source: string;
  goal_id?: string;
  goal_revision?: number;
  watch_id?: string;
  plan_revision?: number;
  input_artifact_id?: string;
  task_id?: string;
  watch_task_id?: string;
}

export interface SetupTask {
  task_id: string;
  task_revision: number;
  status: string;
  recovery_action?: string | null;
  block_reason?: string | null;
  readback_status?: string;
  verification_status?: string;
}

export interface SetupResult {
  progress: SetupProgress;
  content: string;
  file_path: string;
  content_sha256: string;
  readback_id: string;
  memory_status: string;
  observation?: {
    status: string;
    sources: Array<{ target: string }>;
    baselines: Array<{ sha256: string }>;
    readback_id: string;
    memory_status: string;
    schedule_state: string;
  } | null;
}

export async function setupRequest<T>(path: string, method = "GET", body?: unknown): Promise<T> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), 10_000);
  try {
    const response = await apiFetch(`${API_URL}/api${path}`, {
      method, signal: controller.signal,
      ...(body === undefined ? {} : { headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }),
    });
    const payload = await response.json();
    if (!response.ok) {
      const detail = payload?.detail;
      throw new Error(typeof detail === "string" ? detail : detail?.recovery ?? detail?.reason ?? detail?.code ?? `Request failed (${response.status}).`);
    }
    return payload as T;
  } finally {
    window.clearTimeout(timer);
  }
}

export function validSetupSource(source: string): boolean {
  try {
    const url = new URL(source);
    return url.protocol === "https:" && !url.username && !url.password && !url.search && !url.hash;
  } catch { return false; }
}

export function setupRecovery(reason: string): string {
  if (/source|https|policy|permission/.test(reason)) return "Use an accessible public HTTPS text page and review its source permission in Connections. Your draft is retained.";
  if (/budget|grant|consent|proactive|expired/.test(reason)) return "Open Goals to review this goal’s finite consent and budget, then refresh the task. Your draft is retained.";
  if (/provider|model|openrouter/.test(reason)) return "Open Settings → OpenRouter setup for any model-dependent work. Both starter paths use no model; inspect the task’s effective route before retrying.";
  if (/unknown|reconcil|cost_liability/.test(reason)) return "Open Work and reconcile this exact attempt. Do not replay an uncertain outcome.";
  return "Refresh this task or open Work to inspect its recovery step. Your draft and existing workspace are retained.";
}
