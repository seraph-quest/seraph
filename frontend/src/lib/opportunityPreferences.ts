import { API_URL } from "../config/constants";
import { apiFetch } from "./api";
import type { OpportunityFeedbackRequest, OpportunityFeedbackSummary, OpportunityPreferenceProposal,
  OpportunityPreferenceScope, OpportunityRecommendationReceipt, OpportunityRecommendationRequest } from "../types";

const record = (value: unknown): value is Record<string, unknown> => Boolean(value && typeof value === "object" && !Array.isArray(value));
const id = (value: unknown): value is string => typeof value === "string" && /^[a-zA-Z0-9:_-]{1,128}$/.test(value);
const integer = (value: unknown, minimum = 1): value is number => typeof value === "number" && Number.isSafeInteger(value) && value >= minimum;
const sha = (value: unknown): value is string => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
const uuid = (value: unknown): value is string => typeof value === "string" && /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(value);
const time = (value: unknown): value is string => typeof value === "string" && /(?:Z|\+00:00)$/.test(value) && Number.isFinite(Date.parse(value));
const nullable = (value: unknown, check: (value: unknown) => boolean) => value === null || check(value);
const closed = (value: Record<string, unknown>, keys: string) => Object.keys(value).every((key) => keys.split(",").includes(key));

export function normalizeOpportunityFeedback(value: unknown): OpportunityFeedbackSummary | null {
  if (!record(value) || !integer(value.feedback_revision, 0) || !integer(value.event_count, 0) || value.event_count > 100
    || !nullable(value.intervention_id, id) || !nullable(value.feedback_at, time) || !nullable(value.feedback_event_id, uuid)
    || !nullable(value.feedback_history_digest, sha) || ![null, "helpful", "not_helpful"].includes(value.feedback_type as string | null)
    || value.memory_status !== "no_learning" || !nullable(value.reason_code, (v) => typeof v === "string")
    || (value.feedback_revision === 0 && (value.feedback_type !== null || value.feedback_event_id !== null || value.event_count !== 0))
    || (value.feedback_revision > 0 && (!id(value.intervention_id) || !time(value.feedback_at) || !uuid(value.feedback_event_id)
      || !sha(value.feedback_history_digest) || value.feedback_type === null || value.event_count < 1))) return null;
  return value as unknown as OpportunityFeedbackSummary;
}

export function normalizeOpportunityRecommendation(value: unknown): OpportunityRecommendationReceipt | null {
  if (!record(value) || !id(value.opportunity_id) || !integer(value.opportunity_revision) || !integer(value.feedback_revision, 0)
    || !id(value.task_id) || !integer(value.task_revision) || !nullable(value.attempt_id, id) || !nullable(value.job_id, id)
    || !nullable(value.proposal_id, id) || !nullable(value.bundle_digest, sha) || !sha(value.population_digest)
    || typeof value.reason_code !== "string" || typeof value.idempotent_replay !== "boolean" || value.memory_status !== "no_learning"
    || !["queued", "running", "proposed", "no_learning", "blocked", "cancel_requested", "cancelled", "unknown"].includes(String(value.status))
    || (["queued", "running"].includes(String(value.status)) && (value.proposal_id !== null || value.bundle_digest !== null))
    || (["proposed", "no_learning"].includes(String(value.status)) && (!id(value.attempt_id) || !id(value.job_id) || !sha(value.bundle_digest)))
    || (value.status === "proposed" && !id(value.proposal_id)) || (value.status === "no_learning" && value.proposal_id !== null)) return null;
  return value as unknown as OpportunityRecommendationReceipt;
}

function normalizeScope(value: unknown): OpportunityPreferenceScope | null {
  if (!record(value) || !closed(value, "schema_version,owner_principal_id,owner_session_id,goal_id,goal_revision,action,blueprint_id,watch_id,watch_revision,source_context_digest,generation_cutoff_at,window_days,population_count,feedback_event_count,population_members,population_digest,bundle_digest")
    || value.schema_version !== "guardian_opportunity_preference.v1" || !id(value.owner_principal_id) || !id(value.owner_session_id)
    || !id(value.goal_id) || !integer(value.goal_revision) || !time(value.generation_cutoff_at) || value.window_days !== 30
    || !sha(value.source_context_digest) || !sha(value.population_digest) || !sha(value.bundle_digest)
    || !integer(value.population_count, 2) || value.population_count > 100 || !integer(value.feedback_event_count, value.population_count)
    || value.feedback_event_count > 100 || !Array.isArray(value.population_members) || value.population_members.length !== value.population_count
    || !value.population_members.every((member) => record(member)
      && closed(member, "opportunity_id,intervention_id,feedback_revision,feedback_event_id,feedback_at,feedback_binding_digest")
      && id(member.opportunity_id) && id(member.intervention_id) && integer(member.feedback_revision) && uuid(member.feedback_event_id)
      && time(member.feedback_at) && sha(member.feedback_binding_digest))
    || new Set(value.population_members.map((member) => member.opportunity_id)).size !== value.population_count
    || (value.action === "prefer_blueprint" ? (! ["public-browser-check", "public-evidence-report"].includes(String(value.blueprint_id))
      || value.watch_id !== null || value.watch_revision !== null)
      : value.action !== "suppress_watch" || value.blueprint_id !== null || !id(value.watch_id) || !integer(value.watch_revision))) return null;
  return value as unknown as OpportunityPreferenceScope;
}

export function normalizeOpportunityPreference(value: unknown): OpportunityPreferenceProposal | null {
  if (!record(value)) return null;
  const scope = normalizeScope(value.scope);
  if (!scope || value.schema_version !== "opportunity_recommendation.v1" || !id(value.proposal_id)
    || !["proposed", "accepted", "rejected", "blocked", "expired", "rolled_back", "no_learning"].includes(String(value.status))
    || !["proposed", "accepted", "rejected", "blocked", "expired", "rolled_back", "no_learning"].includes(String(value.canonical_status))
    || typeof value.rollback_available !== "boolean"
    || !id(value.owner_principal_id) || !id(value.owner_session_id) || !id(value.source_task_id) || !integer(value.source_task_revision)
    || !id(value.source_attempt_id) || !integer(value.source_attempt_fence) || !id(value.workflow_run_id) || !id(value.goal_id) || !integer(value.goal_revision)
    || typeof value.preview_text !== "string" || new TextEncoder().encode(value.preview_text).length > 65536 || !sha(value.preview_text_digest)
    || !nullable(value.accepted_memory_id, id) || !integer(value.revision) || typeof value.reason_code !== "string" || !nullable(value.expires_at, time)
    || scope.owner_principal_id !== value.owner_principal_id || scope.owner_session_id !== value.owner_session_id
    || scope.goal_id !== value.goal_id || scope.goal_revision !== value.goal_revision || value.bundle_digest !== scope.bundle_digest
    || value.evidence_population !== "current_explicit_opportunity_feedback_only" || value.quality_evidence !== "unmeasured"
    || !["no_learning", "accepted"].includes(String(value.memory_status)) || value.included_count !== scope.population_count || value.feedback_event_count !== scope.feedback_event_count
    || typeof value.quality_disclosure !== "string" || !Array.isArray(value.registered_capabilities) || value.registered_capabilities.length !== 0
    || !Array.isArray(value.allowed_decision_effects) || value.allowed_decision_effects.length !== 0) return null;
  return { ...value, scope } as unknown as OpportunityPreferenceProposal;
}

async function request(path: string, body?: unknown, signal?: AbortSignal): Promise<unknown> {
  const response = await apiFetch(`${API_URL}/api/${path}`, { signal,
    ...(body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }) });
  const value: unknown = await response.json();
  if (!response.ok) {
    const detail = record(value) && record(value.detail) ? value.detail : null;
    throw new Error(typeof detail?.code === "string" ? detail.code : `Opportunity review unavailable (${response.status}).`);
  }
  return value;
}
export async function postOpportunityFeedback(opportunityId: string, body: OpportunityFeedbackRequest, signal?: AbortSignal): Promise<void> {
  const value = await request(`guardian/opportunities/${encodeURIComponent(opportunityId)}/feedback`, body, signal);
  if (!record(value) || value.opportunity_id !== opportunityId || !id(value.intervention_id) || !integer(value.feedback_revision)
    || value.feedback_revision !== body.expected_feedback_revision + 1 || value.feedback_type !== body.feedback_type
    || !uuid(value.feedback_event_id) || !time(value.feedback_at) || !sha(value.feedback_event_digest) || !sha(value.feedback_history_digest)
    || !sha(value.outcome_binding_digest) || typeof value.idempotent_replay !== "boolean" || value.memory_status !== "no_learning") throw new Error("Invalid feedback receipt. Refresh the current history.");
}
export async function opportunityRecommendation(opportunityId: string, body: OpportunityRecommendationRequest, inspect = false, signal?: AbortSignal): Promise<OpportunityRecommendationReceipt> {
  const value = normalizeOpportunityRecommendation(await request(`guardian/opportunities/${encodeURIComponent(opportunityId)}/recommendation${inspect ? `?idempotency_key=${encodeURIComponent(body.idempotency_key)}` : ""}`, inspect ? undefined : body, signal));
  if (!value || value.opportunity_id !== opportunityId || value.feedback_revision !== body.expected_feedback_revision) throw new Error("Invalid recommendation receipt. Actions remain unavailable.");
  return value;
}
export async function inspectOpportunityPreference(proposalId: string, signal?: AbortSignal): Promise<OpportunityPreferenceProposal> {
  const value = normalizeOpportunityPreference(await request(`memory/opportunity-preferences/${encodeURIComponent(proposalId)}`, undefined, signal));
  if (!value || value.proposal_id !== proposalId) throw new Error("Invalid opportunity preference preview. Actions remain unavailable.");
  return value;
}
export async function actOnOpportunityPreference(proposal: OpportunityPreferenceProposal, action: "accept" | "reject" | "rollback", mutationUuid: string, reason: string, signal?: AbortSignal): Promise<void> {
  await request(`memory/opportunity-preferences/${encodeURIComponent(proposal.proposal_id)}/actions`, { action,
    expected_revision: proposal.revision, expected_preview_text_digest: proposal.preview_text_digest,
    expected_bundle_digest: proposal.scope.bundle_digest, acknowledged_opportunity_preference_only: true, mutation_uuid: mutationUuid, reason }, signal);
}
