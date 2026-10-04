import { API_URL } from "../config/constants";
import { apiFetch } from "./api";

export interface ProcedureOutcome {
  task_id: string;
  task_revision: number;
  status: string;
  attempt_id: string | null;
  attempt_fence?: number | null;
  feedback: "helpful" | "harmful" | null;
  feedback_event_id: number | null;
  feedback_allowed?: boolean;
  feedback_current?: boolean;
  feedback_history_label?: "helpful" | "harmful" | null;
  feedback_history_count?: number;
  verified?: boolean;
  reason_code?: string;
}
export interface ProcedurePreferenceReview {
  proposal_id: string;
  owner_principal_id: string;
  owner_session_id: string;
  revision: number;
  status: string;
  preview_text: string;
  preview_text_digest: string;
  bundle_digest: string;
  included_count: number;
  outcomes: ProcedureOutcome[];
  manual_disclosure: string;
  quality_disclosure: string;
}
export interface ProcedureOutcomeList {
  included_count: number;
  outcomes: ProcedureOutcome[];
  manual_disclosure: string;
  quality_disclosure: string;
}
export interface ProcedureRecommendation extends ProcedureOutcomeList {
  job_id: string;
  job_status: string;
  status: string;
  reason_code: string;
  proposal_id: string | null;
  job_revision?: number;
  fencing_token?: number;
}
export interface RecommendationCancelRequest extends RecommendationRequest {
  expected_job_revision: number;
  expected_fencing_token: number;
}
export interface ProcedurePreferenceSelection {
  status: string;
  reason_code: string;
  suggested_version?: number;
  suggested_version_id?: string;
  review?: ProcedurePreferenceReview;
}
export interface ProcedurePreferenceScope {
  routineId: string;
  version: number;
  routineRevision: number;
  goalId: string;
  goalRevision: number;
}
export interface ProcedurePreferenceAction {
  action: "accept" | "reject" | "rollback";
  expected_revision: number;
  expected_preview_text_digest: string;
  expected_bundle_digest: string;
  acknowledged_selection_only: true;
  mutation_uuid: string;
  reason: string;
}
export interface RecommendationRequest {
  version: number;
  expected_routine_revision: number;
  goal_id: string;
  expected_goal_revision: number;
  request_uuid: string;
}

async function request<T>(path: string, body?: object): Promise<T> {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 120_000);
  try {
    const response = await apiFetch(`${API_URL}${path}`, body ? {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal: controller.signal,
    } : { signal: controller.signal });
    const raw = await response.text();
    if (raw.length > 128 * 1024) throw new Error("Procedure response exceeds its finite display bound.");
    const value = JSON.parse(raw);
    if (!response.ok) throw new Error(value?.detail?.message ?? value?.detail?.code ?? "Procedure review is unavailable.");
    return value as T;
  } finally { clearTimeout(timeout); }
}
const routinePath = (scope: ProcedurePreferenceScope) => `/api/capabilities/routines/${encodeURIComponent(scope.routineId)}`;
const query = (scope: ProcedurePreferenceScope) => new URLSearchParams({
  version: String(scope.version), expected_routine_revision: String(scope.routineRevision),
  goal_id: scope.goalId, expected_goal_revision: String(scope.goalRevision),
}).toString();

export const procedurePreferences = {
  outcomes: (scope: ProcedurePreferenceScope) => request<ProcedureOutcomeList>(`${routinePath(scope)}/outcomes?${query(scope)}`),
  selection: (scope: ProcedurePreferenceScope) => request<ProcedurePreferenceSelection>(`${routinePath(scope)}/preference?${query(scope)}`),
  recommend: (scope: ProcedurePreferenceScope, body: RecommendationRequest) => request<ProcedureRecommendation>(`${routinePath(scope)}/recommendations`, body),
  inspectJob: (scope: ProcedurePreferenceScope, jobId: string) => request<ProcedureRecommendation>(`${routinePath(scope)}/recommendations/${encodeURIComponent(jobId)}`),
  findJob: (scope: ProcedurePreferenceScope, body: RecommendationRequest) => request<{ found: boolean; job: ProcedureRecommendation | null }>(`${routinePath(scope)}/recommendations?${query(scope)}&request_uuid=${encodeURIComponent(body.request_uuid)}`),
  cancel: (scope: ProcedurePreferenceScope, jobId: string, body: RecommendationCancelRequest) => request<ProcedureRecommendation>(`${routinePath(scope)}/recommendations/${encodeURIComponent(jobId)}/cancel`, body),
  review: (proposalId: string) => request<ProcedurePreferenceReview>(`/api/memory/procedure-preferences/${encodeURIComponent(proposalId)}`),
  act: (proposalId: string, body: ProcedurePreferenceAction) => request<ProcedurePreferenceReview>(`/api/memory/procedure-preferences/${encodeURIComponent(proposalId)}/actions`, body),
  feedback: (scope: ProcedurePreferenceScope, outcome: ProcedureOutcome, label: "helpful" | "harmful", reason: string, mutationUuid: string) => request<{ event_id: number }>(`${routinePath(scope)}/outcomes/${encodeURIComponent(outcome.task_id)}/feedback`, {
    version: scope.version, expected_routine_revision: scope.routineRevision, goal_id: scope.goalId,
    expected_goal_revision: scope.goalRevision, expected_task_revision: outcome.task_revision,
    expected_attempt_id: outcome.attempt_id, expected_attempt_fence: outcome.attempt_fence ?? null,
    label, supersedes_event_id: outcome.feedback_event_id, reason, mutation_uuid: mutationUuid,
  }),
};
