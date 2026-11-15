import { API_URL } from "../config/constants";
import { apiFetch } from "./api";
import type { WorkBoardTask } from "../types";

export const SOURCE_PREFERENCES = ["primary", "official", "peer_reviewed", "dated", "independent"] as const;
export const EVIDENCE_FIELDS = ["url", "title", "date", "excerpt", "claim", "limitation"] as const;
export interface ResearchStrategy {
  schema_version: "ResearchStrategy.v1"; query_templates: string[];
  source_preferences: (typeof SOURCE_PREFERENCES)[number][];
  required_evidence_fields: (typeof EVIDENCE_FIELDS)[number][];
  draft_sections: string[]; stop_conditions: string[];
}
export interface LessonScope { goal_id: string; goal_revision: number; family: "general" | "research" | "software" | "knowledge" }
export type Observation = { status: "completed"; readback_digest: string } | { status: "failed"; failure_reason_digest: string };
export interface ResearchSource {
  task_id: string; expected_revision: number; attempt_id: string; source_refs: string[]; scope: LessonScope;
  eligible: true; supported_candidate_kind: "research_strategy"; source_current: true; observed: Observation;
  reason_code: string;
}
export interface ResearchMethodRequest {
  task_id: string; attempt_id: string; source_refs: string[]; scope: LessonScope; expected_revision: number; strategy: ResearchStrategy;
}
export const object = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);
export const identifier = (v: unknown): v is string => typeof v === "string" && /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(v);
const sha = (v: unknown) => typeof v === "string" && /^[a-f0-9]{64}$/.test(v);
export function validSourceRefs(v: unknown): v is string[] {
  return Array.isArray(v) && v.length > 0 && v.length <= 16 && new Set(v).size === v.length
    && v.every(r => typeof r === "string" && r.length <= 255 && /^[A-Za-z0-9][A-Za-z0-9._:/-]*$/.test(r));
}
export function validObservation(v: unknown): v is Observation {
  return object(v) && Object.keys(v).length === 2 && (v.status === "completed" && sha(v.readback_digest) || v.status === "failed" && sha(v.failure_reason_digest));
}
export function validateResearchStrategy(v: unknown): asserts v is ResearchStrategy {
  const texts = (a: unknown, max: number, min = 0) => Array.isArray(a) && a.length >= min && a.length <= max && a.every(t => typeof t === "string" && t.trim().length > 0 && [...t].length <= 1000);
  const enums = (a: unknown, allowed: readonly string[]) => Array.isArray(a) && a.length <= allowed.length && a.every(s => typeof s === "string" && allowed.includes(s));
  if (!object(v) || Object.keys(v).sort().join(",") !== "draft_sections,query_templates,required_evidence_fields,schema_version,source_preferences,stop_conditions"
    || v.schema_version !== "ResearchStrategy.v1" || !texts(v.query_templates, 3) || !enums(v.source_preferences, SOURCE_PREFERENCES)
    || !enums(v.required_evidence_fields, EVIDENCE_FIELDS) || !texts(v.draft_sections, 16) || !texts(v.stop_conditions, 8, 1)) {
    throw Error("Use at most 3 queries, 5 source preferences, 6 evidence fields, 16 draft sections and 1–8 stop conditions. Each text field must contain 1–1000 characters.");
  }
}
export function validateProvenance(value: unknown, task: WorkBoardTask, attemptId?: string, refs?: string[]): void {
  if (!object(value) || value.task_id !== task.task_id || !identifier(value.attempt_id) || !validSourceRefs(value.source_refs) || !validObservation(value.observed)
    || (attemptId !== undefined && value.attempt_id !== attemptId) || (task.latest_attempt?.attempt_id && value.attempt_id !== task.latest_attempt.attempt_id)
    || (refs !== undefined && JSON.stringify(value.source_refs) !== JSON.stringify(refs))) throw Error("Source Task, Attempt or verified references changed. Refresh the exact Work card before review.");
}
export function validateResearchSource(value: unknown, task: WorkBoardTask): asserts value is ResearchSource {
  validateProvenance(value, task);
  if (!object(value) || value.expected_revision !== task.task_revision || value.eligible !== true || value.source_current !== true || value.supported_candidate_kind !== "research_strategy"
    || task.capability_id !== "work.research-dossier.v1" || value.observed && (value.observed as Observation).status !== "completed"
    || !object(value.scope) || value.scope.family !== "research" || value.scope.goal_id !== task.goal_id || value.scope.goal_revision !== task.goal_revision) throw Error("A current verified completed research dossier is required. Inspect the exact source again.");
}
export class ResearchMethodError extends Error { constructor(message: string, public status: number) { super(message); } }
export async function researchMethodRequest(path: string, body?: ResearchMethodRequest, signal?: AbortSignal): Promise<unknown> {
  const r = await apiFetch(`${API_URL}/api/memory${path}`, { signal, ...(body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : {}) });
  if (!r.ok) throw new ResearchMethodError(r.status === 409 ? "Research source or task revision changed. Inspect the current Work evidence before preparing again." : `Research method preparation blocked (${r.status}). Correct bounded fields or restore current original ownership and source verification.`, r.status);
  return r.json();
}
