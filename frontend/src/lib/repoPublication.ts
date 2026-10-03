import { API_URL } from "../config/constants";
import { apiFetch } from "./api";
import { githubCapacityClosure } from "./githubReadback";
import type { GitHubCapacityClosure } from "./githubReadback";

export const PUBLICATION_CAPABILITY = "engineering.repo-publication.v1";
export const PUBLICATION_PERMISSIONS = ["local_host_execution", "github_git_objects_write", "github_new_branch_write", "github_ready_pr_write"];

export interface PublicationReceipt {
  job_id: string; revision: number; status: string; reason_code: string | null;
  approval_id: string | null; approval_status: string; approval_expires_at: string | null;
  preview_digest: string; learning: "no_learning";
  preview: {
    job_id: string; owner_principal_id: string; owner_session_id: string;
    repository: string; base_branch: string; base_commit: string; base_tree: string;
    branch_name: string; commit_message: string; title: string; body: string;
    connection_revision: number; local_posture_digest: string;
    required_permissions: string[]; local_posture: { isolation_claim: string; profile: string };
    repair_binding: { repair_job_id: string; patch_artifact_id: string; patch_sha256: string; tested_input_digest: string; repair_executor: string; repair_executor_posture_digest: string };
    tested_input: { test_args: string[]; environment_unchanged: boolean; environment: { available: boolean; runtime_binding: string }; base_files: unknown[]; tested_files: unknown[] };
  };
  github_capacity_closure?: GitHubCapacityClosure | null;
  artifacts: Array<{ file_path: string }>; effects: unknown[];
}

export function validatePublicationDiscovery(value: unknown, owner: string, root: string, repair: string) {
  if (!record(value) || value.repair_job_id !== repair || value.owner_principal_id !== owner
    || value.owner_session_id !== root || value.limit !== 20 || !Array.isArray(value.jobs) || value.jobs.length > 20
    || !(value.next_offset === null || (Number.isSafeInteger(value.next_offset) && Number(value.next_offset) > 0 && Number(value.next_offset) <= 2000))) {
    throw new Error("Publication discovery is incomplete; refresh the original repair.");
  }
  return { jobs: value.jobs.map(job => validatePublication(job, owner, root, repair)),
    nextOffset: value.next_offset as number | null, scanLimitReached: value.scan_limit_reached === true };
}

function record(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}
function text(value: unknown): value is string { return typeof value === "string" && value.length > 0 && value.length <= 20000 && !value.includes("\0"); }
function sha(value: unknown): value is string { return typeof value === "string" && /^[0-9a-f]{64}$/.test(value); }

export function validatePublication(value: unknown, owner: string, root: string, repair: string): PublicationReceipt {
  if (!record(value) || value.capability_id !== PUBLICATION_CAPABILITY || !text(value.job_id) || !Number.isInteger(value.revision) || Number(value.revision) <= 0 || !["accepted", "queued", "running", "awaiting_approval", "succeeded", "cancelled", "unknown_external_effect", "blocked", "failed"].includes(String(value.status)) || !sha(value.preview_digest) || value.learning !== "no_learning" || !record(value.preview)) throw new Error("Publication receipt is incomplete; refresh before acting.");
  const p = value.preview;
  if (p.job_id !== value.job_id || p.owner_principal_id !== owner || p.owner_session_id !== root || !record(p.repair_binding) || p.repair_binding.repair_job_id !== repair || !sha(p.repair_binding.patch_sha256) || !sha(p.repair_binding.tested_input_digest) || !record(p.tested_input) || p.tested_input.environment_unchanged !== true || !record(p.tested_input.environment) || p.tested_input.environment.available !== true || !Array.isArray(p.tested_input.test_args) || !p.tested_input.test_args.every(text) || !Array.isArray(p.tested_input.base_files) || !Array.isArray(p.tested_input.tested_files) || !record(p.local_posture) || p.local_posture.isolation_claim !== "none" || !sha(p.local_posture_digest) || !Number.isInteger(p.connection_revision) || !Array.isArray(p.required_permissions) || p.required_permissions.join("\0") !== PUBLICATION_PERMISSIONS.join("\0") || ![p.repository, p.base_branch, p.base_commit, p.base_tree, p.branch_name, p.title, p.body, p.commit_message].every(text) || !Array.isArray(value.artifacts) || !Array.isArray(value.effects) || !text(value.approval_status) || !(value.approval_id === null || text(value.approval_id)) || !(value.approval_expires_at === null || (text(value.approval_expires_at) && Number.isFinite(Date.parse(value.approval_expires_at))))) throw new Error("Publication authority or tested-input receipt is invalid; refresh before acting.");
  const closure = githubCapacityClosure(value.github_capacity_closure);
  if (closure && closure.native_kind !== PUBLICATION_CAPABILITY) throw new Error("Publication closure kind mismatch");
  return value as unknown as PublicationReceipt;
}

export async function publicationRequest(path: string, init: RequestInit, signal?: AbortSignal): Promise<unknown> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  const abort = () => controller.abort();
  signal?.addEventListener("abort", abort, { once: true });
  try {
    const response = await apiFetch(`${API_URL}/api/capabilities/github/repo-publication${path}`, { ...init, signal: controller.signal });
    const value = await response.json();
    if (!response.ok) throw new Error(record(value) && record(value.detail) && text(value.detail.code) ? value.detail.code : "Publication request failed.");
    return value;
  } finally { clearTimeout(timer); signal?.removeEventListener("abort", abort); }
}

export function publicationKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = new Uint8Array(16);
  if (typeof crypto !== "undefined" && typeof crypto.getRandomValues === "function") crypto.getRandomValues(bytes);
  else for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
  bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128;
  const h = [...bytes].map((n) => n.toString(16).padStart(2, "0")).join("");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}
