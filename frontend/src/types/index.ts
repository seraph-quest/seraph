export type MessageRole = "user" | "agent" | "step" | "status" | "error" | "proactive" | "approval" | "clarification";

export interface ChatMessage {
  id: string;
  role: MessageRole;
  content: string;
  timestamp: number;
  sessionId?: string | null;
  interventionId?: string;
  stepNumber?: number;
  toolUsed?: string;
  urgency?: number;
  interventionType?: string;
  approvalId?: string;
  riskLevel?: string;
  approvalStatus?: "pending" | "approved" | "denied" | "consumed";
  /** Server-owned approval boundary for trusted local repository execution. */
  localHostExecutionRequired?: boolean;
  requiredPermissions?: string[];
  clarificationQuestion?: string;
  clarificationReason?: string;
  clarificationOptions?: string[];
}

export interface WSMessage {
  type: "message" | "resume_message" | "ping" | "skip_onboarding";
  message: string;
  session_id: string | null;
}

export interface WSResponse {
  type: "status" | "step" | "delta" | "final" | "error" | "pong" | "proactive" | "ambient" | "approval_required" | "clarification_required";
  content: string;
  session_id: string;
  step: number | null;
  seq: number | null;
  intervention_id?: string;
  approval_id?: string;
  tool_name?: string;
  risk_level?: string;
  local_host_execution_required?: boolean;
  required_permissions?: string[];
  question?: string;
  reason?: string;
  options?: string[];
  urgency?: number;
  intervention_type?: string;
  reasoning?: string;
  state?: string;
  tooltip?: string;
}

export type ConnectionStatus = "connecting" | "connected" | "disconnected" | "error";

export type AgentAnimationState =
  | "idle"
  | "thinking"
  | "walking"
  | "wandering"
  | "casting"
  | "speaking";

export type FacingDirection = "left" | "right";

export interface AgentVisualState {
  animationState: AgentAnimationState;
  positionX: number; // percentage 0-100
  facing: FacingDirection;
  speechText: string | null;
}

export type AmbientState = "idle" | "has_insight" | "goal_behind" | "on_track" | "waiting";

export interface SessionInfo {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  last_message: string | null;
  last_message_role: string | null;
}

export type SessionContinuityState = "live" | "restored" | "new_activity";

export type GoalCriterionVerifier =
  | "artifact_readback"
  | "external_readback"
  | "operator_attestation";

export interface GoalSuccessCriterion {
  criterion_id: string;
  description: string;
  verifier_kind: GoalCriterionVerifier | null;
  target: string | Record<string, unknown>;
  evidence_refs: string[];
}

export interface GoalAdmissionBudget {
  reviewed_grant: boolean;
  grant_id?: string | null;
  max_outstanding_jobs: number;
  max_attempts: number;
  max_runtime_seconds: number;
  notifications_per_day: number;
  period_started_at?: string | null;
  period_expires_at?: string | null;
  quiet_hours_start?: number | null;
  quiet_hours_end?: number | null;
  timezone: string;
}

export type GuardianInboxState = "pending" | "snoozed" | "accepted" | "dismissed" | "expired" | GuardianOpportunityStatus;

export type GuardianInboxAction = "accept_followup" | "snooze" | "dismiss";

/** Safe evidence metadata exposed by the guardian inbox. Source content stays behind artifact access. */
export interface GuardianInboxEvidenceRef {
  artifact_id?: string | null;
  artifact_type?: string | null;
  file_path?: string | null;
  content_sha256?: string | null;
  sha256?: string | null;
  kind?: string | null;
  status?: string | null;
  verification?: string | null;
  last_verified_at?: string | null;
  workflow_run_id?: string | null;
  owner_session_id?: string | null;
  target_path?: string | null;
  artifact_url?: string | null;
  label?: string | null;
}

/** Authorized durable source-job/readback receipt shown in item details. */
export interface GuardianInboxReadback {
  target_path?: string | null;
  readback_id?: string | null;
  verified_at?: string | null;
  digest?: string | null;
  status?: string | null;
}

/** Bounded, redacted, owner-checked detail preview for a verified artifact. */
export interface GuardianInboxEvidencePreview {
  source_id?: string | null;
  line_count?: number | null;
  artifact_id?: string | null;
  artifact_type?: string | null;
  file_path?: string | null;
  sha256?: string | null;
  owner_session_id?: string | null;
  workflow_run_id?: string | null;
  text?: string | null;
  trust?: string | null;
}

export interface GuardianInboxJob {
  id?: string | null;
  status?: string | null;
  attempt_count?: number | null;
  max_attempts?: number | null;
  readback_id?: string | null;
  verified_at?: string | null;
  digest?: string | null;
  readback_status?: string | null;
  readbacks?: GuardianInboxReadback[];
}

export type GuardianInboxReasonState = "provided" | "unavailable" | "not_provided";

export interface GuardianInboxActionHistoryEntry {
  receipt_id: string;
  action: GuardianInboxAction | "unavailable";
  created_at: string | null;
  expected_revision: number | null;
  result_revision: number | null;
  task_id: string | null;
  outcome: GuardianInboxState | "unavailable";
  reason_state: GuardianInboxReasonState;
  safe_reason: string | null;
}

/**
 * Detail-only, owner-checked Mail selection metadata.  This deliberately
 * carries no subject, preview, body, provider identity, or credentials.
 */
export interface GuardianInboxMailOrigin {
  /** Exact source watch that produced this accepted candidate. */
  watch_id: string;
  message_binding_id: string;
  message_revision: string;
  status: string;
  private: true;
}

/** Operator-safe projection of a durable, owner-scoped guardian intervention. */
export interface GuardianInboxItem {
  feedback_summary?: OpportunityFeedbackSummary | null;
  plan_offer?: OpportunityPlanOffer | null;
  plan_preview?: OpportunityPlanPreview | null;
  opportunity_id?: string | null;
  opportunity_revision?: number | null;
  opportunity_status?: GuardianOpportunityStatus | null;
  assessment?: GuardianOpportunityAssessment | null;
  reason_code?: string | null;
  delivery_status?: string | null;
  cancel_requested?: boolean;
  cancel_allowed?: boolean;
  quiescent?: boolean;
  id: string;
  revision: number;
  state: GuardianInboxState;
  /** True when the server sent an unknown state; actions are then disabled. */
  degraded?: boolean;
  source_kind: string;
  source_id: string;
  title: string;
  summary: string;
  why_now: string;
  goal_id: string;
  goal_revision: number;
  watch_id: string;
  plan_revision: number;
  task_id?: string | null;
  expires_at: string;
  snoozed_until?: string | null;
  evidence_refs: GuardianInboxEvidenceRef[];
  evidence_previews?: GuardianInboxEvidencePreview[];
  job?: GuardianInboxJob | null;
  allowed_actions: GuardianInboxAction[];
  evidence_status?: string | null;
  source_status?: string | null;
  source_freshness?: string | null;
  verification_status?: string | null;
  memory_status?: string | null;
  policy_reason?: string | null;
  recovery_action?: string | null;
  evidence_url?: string | null;
  task_url?: string | null;
  watch_url?: string | null;
  mail?: GuardianInboxMailOrigin | null;
  action_history?: GuardianInboxActionHistoryEntry[];
  action_history_truncated?: boolean;
}

export interface OpportunityFeedbackSummary {
  intervention_id: string | null;
  feedback_revision: number;
  feedback_type: "helpful" | "not_helpful" | null;
  feedback_at: string | null;
  feedback_event_id: string | null;
  feedback_history_digest: string | null;
  event_count: number;
  memory_status: "no_learning";
  reason_code: string | null;
}
export interface OpportunityFeedbackRequest {
  expected_feedback_revision: number;
  feedback_type: "helpful" | "not_helpful";
  reason: string;
  idempotency_key: string;
}
export interface OpportunityRecommendationRequest {
  expected_opportunity_revision: number;
  expected_feedback_revision: number;
  idempotency_key: string;
}
export interface OpportunityRecommendationReceipt {
  opportunity_id: string;
  opportunity_revision: number;
  feedback_revision: number;
  task_id: string;
  task_revision: number;
  attempt_id: string | null;
  job_id: string | null;
  status: "queued" | "running" | "proposed" | "no_learning" | "blocked" | "cancel_requested" | "cancelled" | "unknown";
  proposal_id: string | null;
  bundle_digest: string | null;
  reason_code: string;
  population_digest: string;
  idempotent_replay: boolean;
  memory_status: "no_learning";
}
export interface OpportunityPreferenceScope {
  schema_version: "guardian_opportunity_preference.v1";
  owner_principal_id: string;
  owner_session_id: string;
  goal_id: string;
  goal_revision: number;
  action: "prefer_blueprint" | "suppress_watch";
  blueprint_id: BlueprintId | null;
  watch_id: string | null;
  watch_revision: number | null;
  source_context_digest: string;
  generation_cutoff_at: string;
  window_days: 30;
  population_count: number;
  feedback_event_count: number;
  population_members: { opportunity_id: string; intervention_id: string; feedback_revision: number; feedback_event_id: string;
    feedback_at: string; feedback_binding_digest: string }[];
  population_digest: string;
  bundle_digest: string;
}
export interface OpportunityPreferenceProposal {
  schema_version: "opportunity_recommendation.v1";
  proposal_id: string;
  status: string;
  canonical_status: string;
  rollback_available: boolean;
  owner_principal_id: string;
  owner_session_id: string;
  source_task_id: string;
  source_task_revision: number;
  source_attempt_id: string;
  source_attempt_fence: number;
  workflow_run_id: string;
  goal_id: string;
  goal_revision: number;
  preview_text: string;
  preview_text_digest: string;
  accepted_memory_id: string | null;
  revision: number;
  reason_code: string;
  expires_at: string | null;
  scope: OpportunityPreferenceScope;
  bundle_digest: string;
  evidence_population: "current_explicit_opportunity_feedback_only";
  quality_evidence: "unmeasured";
  memory_status: string;
  included_count: number;
  feedback_event_count: number;
  quality_disclosure: string;
  registered_capabilities: [];
  allowed_decision_effects: [];
}

export interface GuardianInboxPage {
  items: GuardianInboxItem[];
  next_cursor?: string | null;
  last_confirmed_at?: string | null;
}

export interface GuardianInboxActionRequest {
  action: GuardianInboxAction;
  expected_revision: number;
  idempotency_key: string;
  until?: string;
  reason?: string;
}

export interface GuardianInboxActionResponse {
  id: string;
  revision: number;
  state: GuardianInboxState;
  task_id?: string | null;
  receipt_id: string;
  recovery_action?: string | null;
}

export type CanonicalMemoryKind =
  | "fact"
  | "preference"
  | "pattern"
  | "goal"
  | "reflection"
  | "project"
  | "collaborator"
  | "obligation"
  | "routine"
  | "timeline"
  | "commitment"
  | "communication_preference"
  | "procedural";

export type CanonicalMemoryStatus = "active" | "archived" | "superseded";

export interface CanonicalMemoryProvenance {
  source_type?: string | null;
  source_id?: string | null;
  source_session_id?: string | null;
  verified?: boolean | null;
  [key: string]: unknown;
}

export interface CanonicalMemoryLink {
  kind: string;
  label?: string | null;
  href?: string | null;
  id?: string | null;
}

export interface CanonicalMemoryRecord {
  ownership_access?: "recovered_read_only";
  execution_block_reason?: string;
  id: string;
  kind: CanonicalMemoryKind;
  status: CanonicalMemoryStatus;
  summary: string | null;
  confidence: number | null;
  created_at: string;
  updated_at: string;
  last_confirmed_at: string | null;
  source_session_id: string;
  safe_provenance: CanonicalMemoryProvenance;
  links: CanonicalMemoryLink[];
  content?: string | null;
  current_source?: string | null;
  conflict?: Record<string, unknown> | null;
  tombstone?: Record<string, unknown> | null;
  audit_links?: CanonicalMemoryLink[];
  privacy_boundary?: string | null;
  redaction_state?: string | null;
  sources?: Array<Record<string, unknown>>;
  source_state?: Record<string, unknown> | null;
  conflict_state?: Record<string, unknown> | null;
  tombstone_state?: string | null;
}

export interface CanonicalMemoryPage {
  records: CanonicalMemoryRecord[];
  next_cursor: string | null;
  last_confirmed_at: string | null;
}

export interface GoalInfo {
  owner_session_id?: string | null;
  guardian_policy_revision?: number;
  guardian_policy?: GuardianGoalPolicy | null;
  guardian_assessment_state?: "disabled" | "enabled" | "goal_review_required";
  ownership_access?: "recovered_read_only";
  execution_block_reason?: string;
  id: string;
  parent_id: string | null;
  path: string;
  level: string;
  title: string;
  description: string | null;
  status: string;
  domain: string;
  start_date: string | null;
  due_date: string | null;
  sort_order: number;
  children?: GoalInfo[];
  progress?: number;
  /** Server revision for optimistic stale-edit protection. */
  revision?: number;
  /** Null when no bounded verification criterion is configured. */
  success_criterion?: GoalSuccessCriterion | null;
  proactive_enabled?: boolean;
  admission_budget?: GoalAdmissionBudget | null;
}

export type GuardianOpportunityStatus = "queued" | "assessing" | "proposed" | "silent" | "blocked" | "unknown" | "planned" | "dismissed" | "expired" | "cancelled";

export interface GuardianOpportunityAssessment {
  schema_version: "seraph.opportunity.assessment.v1";
  relevance: number;
  confidence: "low" | "medium" | "high";
  summary: string;
  reason: string;
  citations: Array<{ source_id: string; start_line: number; end_line: number; span_sha256: string }>;
  suggested_blueprint: "public-evidence-report" | "public-browser-check" | "none";
  abstain_reason: string | null;
}

export interface GuardianGoalPolicy {
  schema_version: "seraph.guardian.policy.v1";
  assessment_enabled: boolean;
  auto_stage_plan: boolean;
  confirmed_at: string;
  review_due_at: string;
  grant_id: string;
  original_root_id: string;
  goal_revision: number;
  source_watch_ids: string[];
  max_assessments_per_utc_day: number;
  max_plan_proposals_per_utc_day: number;
  max_notification_per_utc_day: number;
  minimum_gap_seconds: number;
}

export interface GuardianPolicyWatch {
  id: string;
  goal_id: string;
  goal_revision: number;
  plan_revision: number;
  state: string;
  sources: Array<{ source_key: string; kind: string; label?: string }>;
}

export type WorkBoardStatus =
  | "triage"
  | "todo"
  | "ready"
  | "running"
  | "blocked"
  | "review"
  | "done"
  | "archived";

export type WorkBoardRecoveryAction =
  | "cancel"
  | "unblock"
  | "retry"
  | "approve_existing_run"
  | "restore_prerequisite"
  | "configure_goal_success_criterion"
  | "reconcile_admission_binding"
  | "reconcile_external_effect"
  | "renew_review"
  | "prepare_routine_publication"
  | "resume_routine_publication";

export type WorkBoardReadbackStatus =
  | "not_started"
  | "pending"
  | "verified"
  | "failed"
  | "unknown"
  | "not_applicable";

export type WorkBoardVerificationStatus =
  | "not_started"
  | "pending"
  | "passed"
  | "failed"
  | "reconciliation_required"
  | "cancelled";

export interface WorkBoardReceiptReference {
  artifact_id?: string;
  /** Browser runner's safe artifact handle before board projection normalization. */
  artifact_ref?: string;
  artifact_sha256?: string;
  artifact_type?: string;
  file_path?: string;
  content_sha256?: string;
  size_bytes?: number;
  exists?: boolean | null;
  effect_id?: string;
  effect_id_digest?: string;
  effect_type?: string;
  readback_id?: string;
  verification_id?: string;
  status?: string;
  verified?: boolean | null;
  target_digest?: string;
  target_path?: string;
  job_id?: string;
  workflow_run_id?: string;
  recovery_action?: string;
  reason_code?: string;
  error_code?: string;
  child_job_id?: string;
  readback_status?: WorkBoardReadbackStatus;
  verification_status?: WorkBoardVerificationStatus;
  outcome?: string;
  receipt_kind?: "effect" | "readback" | string;
  readback_digest?: string;
  checkpoint_id?: string;
  action_index?: number;
  action_count?: number;
  request_count?: number;
  durable_status?: string;
  cleanup_status?: string;
  memory_status?: "no_learning" | string;
  actual_page_url?: string;
  actual_page_url_digest?: string;
}

export interface WorkBoardBrowserExecution {
  capability_id: "browser.public-task.v1";
  job_id: string;
  durable_status: string;
  action_index: number | null;
  action_count: number | null;
  request_count: number | null;
  cleanup_status: "cleanup_verified" | "not_needed" | "cleanup_unknown" | "unknown";
  memory_status: "no_learning" | "unknown";
  readback_id: string | null;
  artifact_id: string | null;
  file_path: string | null;
  content_sha256: string | null;
}

export interface WorkBoardAttempt {
  attempt_id: string;
  task_id: string;
  workflow_run_id: string | null;
  task_revision_at_claim: number;
  lease_owner: string | null;
  cancel_requested_at: string | null;
  lease_expires_at: string | null;
  heartbeat_at: string | null;
  fencing_token: number;
  executor_id: string | null;
  started_at: string;
  ended_at: string | null;
  outcome: string | null;
  receipt_refs: WorkBoardReceiptReference[];
  browser_execution?: WorkBoardBrowserExecution | null;
  calendar_execution?: CalendarExecutionProjection | null;
  readback_status: WorkBoardReadbackStatus;
  verification_status: WorkBoardVerificationStatus;
  created_at: string;
  updated_at: string;
}

/** Safe task projection returned by the authenticated /api/work-board routes. */
export interface WorkBoardTask {
  opportunity_id?: string | null;
  opportunity_revision?: number | null;
  proposal_ref?: OpportunityPlanReference | null;
  plan_preview?: OpportunityPlanPreview | null;
  ownership_access?: "recovered_read_only";
  execution_block_reason?: string;
  task_id: string;
  creation_sequence: number;
  owner_principal_id: string;
  owner_session_id: string;
  origin_session_id: string | null;
  origin_thread_id: string | null;
  goal_id: string;
  goal_revision: number;
  title: string;
  body: string;
  capability_id: string | null;
  input_artifact_id?: string | null;
  pipeline_operation_id?: string | null;
  pipeline_slot?: string | null;
  typed_input_ref: string | null;
  typed_input_digest: string | null;
  executor_id: string | null;
  assignee_id: string | null;
  priority: number;
  idempotency_scope: string;
  idempotency_key: string;
  scheduled_at: string | null;
  status: WorkBoardStatus;
  block_kind: string | null;
  block_reason: string | null;
  block_source_status: WorkBoardStatus | null;
  cancel_requested_at: string | null;
  requires_review: boolean;
  reviewer_id: string | null;
  review_expires_at?: string | null;
  dependency_count: number;
  completed_dependency_count: number;
  dispatch_rank: number | null;
  dispatch_wait_reason?: string | null;
  recovery_action: WorkBoardRecoveryAction | null;
  readback_status: WorkBoardReadbackStatus;
  verification_status: WorkBoardVerificationStatus;
  task_revision: number;
  result_refs: WorkBoardReceiptReference[];
  artifact_refs: WorkBoardReceiptReference[];
  latest_attempt: WorkBoardAttempt | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  archived_at: string | null;
}

export interface WorkBoardComment {
  comment_id: string;
  task_id: string;
  author_principal_id: string;
  author_session_id: string;
  body: string;
  created_at: string;
}

export interface WorkBoardEvent {
  event_id: number;
  task_id: string;
  kind: string;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface WorkBoardTaskDetail {
  proposal_ref?: OpportunityPlanReference | null;
  plan_preview?: OpportunityPlanPreview | null;
  task: WorkBoardTask;
  attempts: WorkBoardAttempt[];
  parents: string[];
  children: string[];
  comments: WorkBoardComment[];
  events: WorkBoardEvent[];
  parent_handoffs?: WorkBoardSafeParentHandoff[];
  revision: number;
}

export type RepoRepairExecutorKind = "local" | "docker_rootless" | "docker_rootful";

export interface WorkBoardRepoRepairExecutorPosture {
  kind?: RepoRepairExecutorKind;
  profile?: string;
  isolation_claim?: string;
  network_isolation?: string;
  resource_enforcement?: string;
  image_digest?: string | null;
  limits_digest?: string | null;
  host_access?: string;
  local_host_execution_required?: boolean;
  [key: string]: unknown;
}

/** Owner-bound, content-free status projection for engineering.repo-repair.v1. */
export type WorkBoardRepoRepairProcessCleanup = {
  status: "unverified" | "held";
  physical_capacity_released: false;
  cleanup_receipt_verified: false;
  readback_scope: null;
} | {
  status: "released";
  physical_capacity_released: true;
  cleanup_receipt_verified: true;
  readback_scope: "process_cleanup_only";
  job_id: string;
  attempt_id: string;
  fencing_token: number;
  authority_digest: string;
  process_cleanup_readback_sha256: string;
};

export interface WorkBoardRepoRepairProjection {
  job_id: string;
  status: string;
  owner_principal_id: string;
  operator_session_id: string;
  task_id: string | null;
  attempt_id: string | null;
  workflow_run_id: string;
  goal_id: string | null;
  goal_revision: number | null;
  revision: number | null;
  authority_digest: string | null;
  input_digest: string | null;
  run_fingerprint: string | null;
  capability_id: "engineering.repo-repair.v1";
  capability_version: string | null;
  /** Server-selected executor metadata; absent on legacy rootless rows. */
  executor_kind?: RepoRepairExecutorKind;
  executor_profile?: string | null;
  executor_posture?: WorkBoardRepoRepairExecutorPosture | null;
  executor_posture_digest?: string | null;
  required_permissions?: string[];
  local_host_execution_required?: boolean;
  preparation_ready?: boolean;
  execution_ready?: boolean;
  limits: Record<string, number>;
  preflight: Record<string, unknown> | null;
  source_packet: {
    packet_id: string;
    state: string;
    repository_ref: string;
    base_snapshot_sha256: string;
    source_manifest_sha256: string;
    artifact_sha256: string;
    revision: number;
  } | null;
  egress: {
    consent_id: string;
    revision: number;
    runtime_path: string;
    effective_profile_id: string;
    effective_upstream: string;
    maximum_input_bytes: number;
    maximum_output_tokens: number;
    expires_at: string;
    state: string;
  } | null;
  proposal: {
    proposal_id: string;
    status: string;
    revision: number;
    base_snapshot_digest: string;
    source_digest: string;
    model_profile_id: string;
    patch_sha256: string;
    approval_id: string | null;
    expires_at: string;
    safe_metadata: Record<string, unknown>;
  } | null;
  execution: {
    process_cleanup?: WorkBoardRepoRepairProcessCleanup | null;
    artifacts: Array<{
      artifact_id?: string;
      file_path?: string;
      artifact_type?: string;
      content_sha256?: string;
    }>;
    readback: {
      receipt_kind?: string;
      status?: string;
      readback_id?: string;
      target_path?: string;
      content_sha256?: string;
      verified?: boolean;
      verified_at?: string;
    } | null;
    memory_status: string;
    provider_contacted: boolean;
  };
  approval: {
    approval_id: string;
    status: string;
    tool_name: string;
    action: string;
    expires_at: string | null;
  } | null;
  approval_id: string | null;
  memory_status: string;
  recovery_action: string;
  operator_visible: boolean;
}

export interface WorkBoardRepoRepairSourcePreview {
  job_id: string;
  status: string;
  recovery_action: string;
  source_packet: {
    packet_id: string;
    state: string;
    repository_ref: string;
    base_snapshot_sha256: string;
    source_manifest_sha256: string;
    artifact_sha256: string;
    selected_files: Array<{ path: string; size_bytes: number; sha256: string; text: string }>;
    omissions: string[];
    revision: number;
  };
  egress: {
    runtime_path: string;
    effective_profile_id: string | null;
    effective_upstream: string | null;
    maximum_input_bytes: number;
    maximum_output_tokens: number;
    expires_at: string | null;
  };
  provider_contacted: boolean;
  operator_visible: boolean;
}

export interface WorkBoardSafeParentHandoff {
  handoff_id: string;
  parent_task_id: string;
  child_task_id: string;
  status: string;
  summary: string;
  artifact_refs: WorkBoardReceiptReference[];
  result_refs: WorkBoardReceiptReference[];
  verification_receipt: Record<string, unknown>;
  source_attempt_id: string | null;
  source_task_revision: number;
  risks: string[];
}

export interface WorkBoardTaskPage {
  tasks: WorkBoardTask[];
  next_after: number | null;
  last_event_id: number;
}

export interface WorkBoardEventPage {
  events: WorkBoardEvent[];
  last_event_id: number;
  gap: boolean;
}

export interface WorkBoardExecutionLimits {
  goal_id: string;
  goal_revision: number;
  effective_max_runtime_seconds: number;
  default_max_runtime_seconds: number;
  hard_max_runtime_seconds: number;
  attempt_limit: number;
  max_outstanding_jobs?: number;
  limit_source: "goal_admission_budget" | "default";
  browser_task_policy?: BrowserTaskPolicy | null;
}

export interface BrowserTaskPolicyRuleSet {
  rules: string[];
  truncated: boolean;
  known: boolean;
}

export interface BrowserTaskPolicy {
  policy_state: "confirmed" | "unknown";
  policy_source: "configured_site_policy" | null;
  allowlist: BrowserTaskPolicyRuleSet;
  blocklist: BrowserTaskPolicyRuleSet;
  limits: {
    max_runtime_seconds: number;
    hard_max_runtime_seconds: number;
    max_actions: number;
    max_navigations: number;
    max_requests: number;
    max_extract_bytes: number;
    max_browser_contexts: number;
    ready_capacity: number;
    max_attempts: number;
    max_outstanding_jobs: number;
    inference: "none";
  };
}

export interface WorkBoardInputArtifactCreateRequest {
  schema_version: 1;
  capability_id: "browser.public-task.v1";
  goal_id: string;
  goal_revision: number;
  input: Record<string, unknown>;
  idempotency_key: string;
}

export interface WorkBoardInputArtifactResponse {
  artifact_id: string;
  typed_input_ref: string;
  typed_input_digest: string;
  capability_id: "browser.public-task.v1";
  goal_id: string;
  goal_revision: number;
  expires_at: string;
  state?: string | null;
  size_bytes?: number | null;
  bound_task_id?: string | null;
  bound_task_revision?: number | null;
  revision?: number | null;
}

export interface WorkBoardTaskCreateRequest {
  title: string;
  body?: string;
  goal_id: string;
  goal_revision: number;
  status?: "triage" | "todo";
  capability_id?: string | null;
  input_artifact_id?: string | null;
  typed_input_ref?: string | null;
  typed_input_digest?: string | null;
  executor_id?: string | null;
  assignee_id?: string | null;
  priority?: number;
  idempotency_scope?: string;
  idempotency_key: string;
  scheduled_at?: string | null;
  requires_review?: boolean;
  reviewer_id?: string | null;
  origin_thread_id?: string | null;
}

export interface WorkBoardTaskPatchRequest {
  expected_revision: number;
  title?: string;
  body?: string;
  priority?: number;
  capability_id?: string | null;
  typed_input_ref?: string | null;
  typed_input_digest?: string | null;
  executor_id?: string | null;
  assignee_id?: string | null;
  scheduled_at?: string | null;
}

export type WorkBoardAction =
  | "promote" | "block" | "unblock" | "retry" | "cancel" | "archive"
  | "request_review" | "request_changes" | "complete_review" | "renew_review";

export interface WorkBoardActionRequest {
  action: WorkBoardAction;
  expected_revision: number;
  block_kind?: "operator" | "dependency" | "needs_input" | "capability" | "transient" | "cancelled" | "review_expired" | "unknown_effect";
  source_status?: WorkBoardStatus;
  attempt_id?: string;
  evidence_refs?: string[];
  reason?: string;
  resolution?: string;
}

export interface WorkBoardProposalTask {
  task_id?: string;
  title: string;
  body?: string;
  goal_id?: string;
  goal_revision?: number;
  capability_id: string;
  typed_input_ref?: string | null;
  typed_input_digest?: string | null;
  executor_id: string;
  authority: string;
  dependencies?: string[];
  cost_estimate?: string | null;
  capability_version?: string;
}

export interface WorkBoardProposalLink {
  parent_task_id: string;
  child_task_id: string;
}

export interface WorkBoardProposal {
  opportunity_id?: string | null;
  opportunity_revision?: number | null;
  proposal_ref?: OpportunityPlanReference | null;
  plan_preview?: OpportunityPlanPreview | null;
  kind: "specify" | "decompose" | OpportunityPlanKind;
  proposal_id: string;
  proposal_revision: number;
  parent_task_id: string;
  parent_revision: number;
  idempotency_key?: string;
  proposal_digest: string | null;
  expires_at: string;
  proposed_tasks: WorkBoardProposalTask[];
  proposed_links: WorkBoardProposalLink[];
  estimated_cost: string | null;
  blocked_reason?: string | null;
  recovery_action?: string | null;
  status?: string;
  request_digest?: string;
  route_id?: string;
  capability_id?: string;
  capability_version?: string;
  grant_revision?: number;
  input_digest?: string;
  admission_job_id?: string;
  effect_id_digest?: string;
  provider_contact_state?: string;
}

export interface WorkBoardCommentCreateRequest {
  expected_revision: number;
  body: string;
}

export interface WorkBoardLinkCreateRequest {
  parent_task_id: string;
  child_task_id: string;
  expected_child_revision: number;
}

export interface WorkBoardLinkDeleteRequest extends WorkBoardLinkCreateRequest {}

/** Safe preview returned before a verified board journey can become a routine. */
export interface WorkBoardRoutinePreviewRequest {
  source_task_id: string;
  action_task_id: string;
  expected_source_revision: number;
  expected_action_revision: number;
  name: string;
  idempotency_key: string;
}

export interface WorkBoardRoutinePreview {
  preview_digest: string;
  source_refs: Record<string, string | number | boolean | null>;
  version_plan: {
    version: number;
    steps: string[];
    workflow: string;
  };
  typed_parameters: Record<string, string | number | boolean | null>;
  permissions: {
    capability_id: string;
    external_mutation: string;
    package_review: string;
    shell_or_arbitrary_connector: boolean;
  };
  limits: {
    runtime_seconds: number;
    attempts: number;
    remote_inference: boolean;
  };
  verifier: {
    source: string;
    required: boolean;
    unknown_effect: string;
  };
  expires_at: string;
  safe_summary: string;
}

export interface WorkBoardRoutineBinding {
  routine_id: string;
  state: string;
  status: string;
  revision: number;
  version: number;
  install_job_id: string;
  preview_digest: string;
  binding_id: string;
  /** Present when the server can expose the exact approval receipt safely. */
  approval_id?: string | null;
}

export interface WorkBoardRoutineVersion {
  id: string;
  routine_id: string;
  version: number;
  workflow_sha256: string;
  runbook_sha256: string;
  installed_package_digest: string | null;
  source_provenance: Record<string, string | number | boolean | null | string[]>;
  source_repository: string | null;
  source_action: string | null;
  source_issue_number: number | null;
  created_at: string;
  installed_at: string | null;
}

export interface WorkBoardRoutineRead {
  ownership_access?: "recovered_read_only";
  execution_block_reason?: string;
  id: string;
  owner_principal_id: string;
  state: string;
  revision: number;
  current_version: number | null;
  name: string;
  versions: WorkBoardRoutineVersion[];
  package: {
    status: string;
    digest?: string | null;
    review_id?: string | null;
    reason?: string | null;
  };
}

/** Redacted server-generated capability-pack proposal for one routine version. */
export interface WorkBoardRoutinePackagePreview {
  routine_id: string;
  version: number;
  pack_id: string;
  digest: string;
  installed_package_digest: string | null;
  review_id: string | null;
  status: string;
  manifest: {
    schema_version?: number;
    display_name: string;
    summary: string;
    version: string;
    authority: {
      tools: string[];
      filesystem: string[];
      network: boolean;
      secrets: string[];
      approval: string;
    };
    resources: {
      max_runtime_seconds: number;
      max_artifact_bytes: number;
      max_inference_cost_microusd: number;
      inference_priority: string;
    };
    data_policy: { classes: string[]; egress: string[] };
  };
  runbook: {
    title: string;
    summary: string;
    procedure: {
      schema_version?: 1 | 2;
      capability_id: string;
      template_id?: string;
      plan_digest?: string;
      steps: Array<{ id: string; capability_id?: string; capability_version?: string; capability?: string; tool?: string }>;
    };
    bindings: {
      workflow_sha256: string;
      legacy_runbook_sha256?: string;
      runbook_sha256?: string;
      source_provenance_sha256: string;
      plan_digest?: string;
    };
  };
}

/** Redacted JSON snapshot for one installed, digest-verified procedure version. */
export interface WorkBoardRoutineProcedureExport {
  schema_version: 1;
  kind: "seraph.reviewed_procedure.v1";
  pack_id: string;
  version: number;
  package_digest: string;
  manifest: Record<string, unknown>;
  runbook: Record<string, unknown>;
}

export interface WorkBoardRoutinePackageApproval {
  approval_id: string;
  status: "pending" | "approved" | "denied" | "expired" | "consumed";
  action: string;
  pack_id: string;
  version: string;
  digest: string;
  goal_id: string;
  expires_at?: string | null;
}

/** Owner/session-scoped source-watch fields safe for routine invocation selection. */
export interface WorkBoardSourceWatch {
  id: string;
  goal_id: string;
  goal_revision: number;
  plan_revision: number;
  state: string;
  last_status: string | null;
}

export interface WorkBoardRoutineInvokeRequest {
  version: number;
  expected_routine_revision: number;
  goal_id: string;
  expected_goal_revision: number;
  source_watch_id: string;
  expected_watch_revision: number;
  invocation_uuid: string;
}

export interface WorkBoardRoutineInvokeReceipt {
  status: string;
  task_id: string;
  task_revision?: number;
  deduped?: boolean;
  preview?: {
    routine_id?: string;
    routine_revision?: number;
    version?: number;
    goal_id?: string;
    goal_revision?: number;
    source_watch_id?: string;
    source_watch_revision?: number;
    steps?: string[];
  };
}

export interface WorkBoardRoutinePublicationPrepareRequest {
  expected_revision: number;
  title?: string | null;
  body: string;
}

export interface WorkBoardRoutinePublicationState {
  task_id: string;
  task_revision: number;
  attempt_id: string;
  parent_workflow_run_id: string | null;
  routine_id: string;
  routine_version: number;
  routine_revision: number;
  source_watch_id: string;
  source_watch_revision: number;
  parent_status: string | null;
  m3_job_id: string | null;
  m3_status: string | null;
  approval_id: string | null;
  approval_status: string | null;
  preview: {
    repository?: string;
    action?: string;
    issue_number?: number | null;
    title?: string | null;
    body?: string | null;
    body_sha256?: string;
    marker?: string;
    dossier_artifact_id?: string;
    dossier_sha256?: string;
    source_watch_id?: string;
    connection_revision?: number;
  } | null;
  status: string | null;
  recovery_action: "prepare_routine_publication" | "resume_routine_publication";
}

export interface WorkBoardRoutinePublicationResponse {
  task: WorkBoardTask;
  publication: WorkBoardRoutinePublicationState;
  approval_required?: boolean;
  operator_action?: string;
  recovery?: Record<string, unknown>;
  readback_required?: boolean;
}

/** The smaller goal shape returned by the loop inspection endpoint. */
export interface GoalLoopGoal {
  id: string;
  title: string;
  status: string;
  revision: number;
  parent_id?: string | null;
  description?: string | null;
  level?: string;
  domain?: string;
  due_date?: string | null;
  success_criterion?: GoalSuccessCriterion | null;
  proactive_enabled?: boolean;
}

export type GoalLoopReceiptEventType =
  | "goal_loop_candidate"
  | "goal_loop_outcome"
  | "goal_loop_no_learning";

export type GoalLoopReceiptType = "candidate" | "outcome" | "no_learning";

/** Actions emitted by the goal-conditioned candidate contract. */
export type GoalLoopCandidateAction = "act" | "clarify" | "defer" | "silent";

/** Execution statuses emitted by the outcome contract or its approval gate. */
export type GoalLoopExecutionStatus =
  | "succeeded"
  | "failed"
  | "blocked"
  | "awaiting_approval"
  | "pending_approval"
  | "approval_required";

export type GoalLoopVerification = "passed" | "failed" | "unknown";

export type GoalLoopUsefulness = "helpful" | "harmful" | "ignored" | "corrected" | "unknown";

export type GoalLoopLearning = "applied" | "proposed" | "no_learning";

export type GoalLoopStrategyDeltaProvenance = "verified" | "unresolved" | "not_present";

/** Redacted candidate/outcome/no-learning receipt from the governed loop. */
export interface GoalLoopReceipt {
  audit_event_id?: string | number | null;
  event_type?: GoalLoopReceiptEventType;
  created_at?: string | null;
  receipt_version?: "goal_conditioned_loop_v1";
  receipt_type?: GoalLoopReceiptType;
  proposal_only?: boolean;
  candidate_id?: string | null;
  outcome_id?: string | null;
  dedupe_key?: string | null;
  goal_id?: string | null;
  goal_revision?: number | null;
  plan_revision?: number | null;
  criterion_id?: string | null;
  action?: GoalLoopCandidateAction | null;
  capability_id?: string | null;
  capability_version?: string | null;
  input_keys?: string[];
  input_digest?: string | null;
  decision_input_digest?: string | null;
  expected_outcome?: string | null;
  expires_at?: string | null;
  strategy_delta_id?: string | null;
  strategy_delta_provenance?: GoalLoopStrategyDeltaProvenance | null;
  execution_status?: GoalLoopExecutionStatus | null;
  verification?: GoalLoopVerification | null;
  usefulness?: GoalLoopUsefulness | null;
  learning?: GoalLoopLearning | null;
  learning_record_id?: string | null;
  artifact_ref?: string | null;
  evidence_refs?: string[];
  reason?: string | null;
  content_redacted?: boolean;
}

export interface GoalStrategyDelta {
  delta_id: string;
  goal_id: string;
  scope: string;
  field_name: string;
  before: Record<string, unknown>;
  after: Record<string, unknown>;
  source_event_id: string;
  author_id: string;
  evaluator_id?: string | null;
  goal_revision_before: number;
  goal_revision_after?: number | null;
  status: string;
  rollback_target_id?: string | null;
  reason: string;
  created_at: string;
  updated_at: string;
}

export interface GoalLoopPayload {
  goal: GoalLoopGoal;
  criterion: GoalSuccessCriterion | null;
  receipts: GoalLoopReceipt[];
  strategy_deltas: GoalStrategyDelta[];
}

export interface GoalSnapshotInput {
  expected_revision: number;
  file_path?: string;
  evidence_refs?: string[];
  reason?: string;
  expected_outcome?: string;
  cancel_requested?: boolean;
}

export interface GoalStrategyCorrectionInput {
  correction_id: string;
  expected_revision: number;
  query?: string;
  file_path?: string;
  priority?: number;
  reason: string;
}

export interface GoalStrategyRollbackInput {
  expected_revision: number;
  reason: string;
}

export interface GoalLoopActionResponse {
  status?: string;
  goal?: GoalLoopGoal;
  delta?: GoalStrategyDelta;
  audit_receipt?: { status?: string; reason?: string; [key: string]: unknown };
  execution_status?: string;
  verification?: string;
  usefulness?: string;
  learning?: string;
  artifact_ref?: string | null;
  [key: string]: unknown;
}

export interface UserProfileInfo {
  id: string;
  name: string;
  onboarding_completed: boolean;
  preferences_json: string | null;
}

export interface DomainProgress {
  domain: string;
  total: number;
  completed: number;
  percentage: number;
}

export interface ToolMeta {
  name: string;
  description: string;
}

export type CalendarConnectionState =
  | "preparing"
  | "active"
  | "revoked"
  | "expired"
  | "blocked"
  | "blocked_cleanup";

export interface CalendarConnectionMetadata {
  connection_id: string;
  service: "calendar_readonly";
  label: string;
  credential_fingerprint: string;
  state: CalendarConnectionState;
  revision: number;
  created_at: string;
  updated_at: string;
}

export interface CalendarOption {
  calendar_id: string;
  summary: string;
}

export interface CalendarVerifyResponse {
  connection: CalendarConnectionMetadata;
  calendars: CalendarOption[];
  calendar_list_revision: string;
  pages_read: 1;
  truncated: boolean;
  provider_status: "verified";
}

export interface CreateCalendarConnectionRequest {
  schema_version: 1;
  service: "calendar_readonly";
  label: string;
  client_id: string;
  client_secret?: string;
  refresh_token: string;
  idempotency_key: string;
}

export interface CalendarConnectionMutationRequest {
  expected_revision: number;
  idempotency_key: string;
}

export type CalendarAllowedField =
  | "summary"
  | "start"
  | "end"
  | "location"
  | "description"
  | "attendees";

export type CalendarConsentState = "active" | "revoked" | "expired" | "consumed";

export interface CalendarConsentMetadata {
  sync_metadata_limit?: number;
  consent_id: string;
  connection_id: string;
  connection_revision: number;
  goal_id: string;
  goal_revision: number;
  allowed_fields: CalendarAllowedField[];
  window_minutes: number;
  max_events: number;
  allow_remote_model: boolean;
  expires_at: string;
  state: CalendarConsentState;
  revision: number;
  consent_digest: string;
  created_at: string;
  updated_at: string;
}

export interface CreateCalendarReadConsentRequest {
  acknowledge_sync_metadata?: boolean;
  schema_version: 1;
  connection_id: string;
  calendar_id: string;
  goal_id: string;
  goal_revision: number;
  allowed_fields: CalendarAllowedField[];
  window_minutes: number;
  max_events: number;
  allow_remote_model: boolean;
  expires_at: string;
  idempotency_key: string;
}

export interface CalendarEventOption {
  event_binding_id: string;
  event_binding_revision: number;
  event_key: string;
  event_revision: string;
  calendar_list_revision: string;
  summary: string;
  start: string;
  end: string;
  location: string | null;
  description: string | null;
  attendees: string[] | null;
}

export interface CalendarEventListResponse {
  events: CalendarEventOption[];
  consent_id: string;
  consent_revision: number;
  connection_revision: number;
  calendar_list_revision: string;
  fetched_at: string;
  pages_read: number;
  truncated: boolean;
}

export interface CalendarMeetingPrepInput {
  schema_version: 1;
  consent_id: string;
  event_binding_id: string;
  expected_event_binding_revision: number;
  expected_consent_revision: number;
  expected_connection_revision: number;
  event_revision: string;
  calendar_list_revision: string;
  goal_id: string;
  goal_revision: number;
  purpose: string;
}

export interface CreateCalendarPrepRequest {
  schema_version: 1;
  input: CalendarMeetingPrepInput;
  title: string;
  idempotency_key: string;
}

export interface CalendarInputArtifactMetadata {
  artifact_id: string;
  typed_input_ref: string;
  typed_input_digest: string;
  capability_id: "calendar.meeting-prep.v1";
  goal_id: string;
  goal_revision: number;
  expires_at: string;
}

export interface CalendarPrepResponse {
  input_artifact: CalendarInputArtifactMetadata;
  task: WorkBoardTask;
  idempotent_replay: boolean;
}

export type CalendarCadenceKind = "5min" | "hourly" | "6h" | "daily";

export interface CalendarCadence {
  kind: CalendarCadenceKind;
  timezone: string;
  daily_hour: number | null;
  daily_minute: number | null;
}

export interface CreateCalendarScheduleRequest {
  schema_version: 1;
  consent_id: string;
  goal_id: string;
  goal_revision: number;
  calendar_id: string;
  cadence: CalendarCadence;
  expires_at: string;
  idempotency_key: string;
}

export type GovernedScheduleState = "active" | "paused" | "revoked" | "expired" | "blocked";
export type GovernedOccurrenceState =
  | "reserved"
  | "running"
  | "coalesced"
  | "succeeded"
  | "blocked"
  | "cancelled"
  | "unknown";

export interface CalendarLatestOccurrence {
  occurrence_id: string;
  binding_revision: number;
  slot_utc: string;
  state: GovernedOccurrenceState;
  task_id: string | null;
  job_id: string | null;
  failure_code: string | null;
  recovery_action: string | null;
  updated_at: string;
}

export interface GovernedScheduleBinding {
  binding_id: string;
  scheduled_job_id: string;
  capability_id: string;
  action_type: string;
  goal_id: string;
  goal_revision: number;
  input_artifact_id: string;
  input_digest: string;
  consent_kind: string;
  consent_id: string;
  consent_revision: number;
  consent_digest: string;
  cadence: CalendarCadence;
  binding_revision: number;
  expires_at: string;
  state: GovernedScheduleState;
  last_slot_utc: string | null;
  created_at: string;
  updated_at: string;
  latest_occurrence: CalendarLatestOccurrence | null;
}

export interface CalendarReadReceipt {
  status: "succeeded" | "blocked" | "unknown";
  request_digest: string;
  response_digest: string | null;
  verified_at: string | null;
}

export interface CalendarEffectiveRoute {
  runtime_path: "strategist_agent";
  provider: "openrouter";
  model: string;
  upstream_provider: string;
  profile_id: string;
  admission_digest: string;
  status: string;
  cost_microusd: number | null;
}

export interface CalendarExecutionProjection {
  capability_id: "calendar.meeting-prep.v1";
  job_id: string;
  durable_status: string;
  connection_id: string | null;
  connection_revision: number | null;
  consent_id: string | null;
  consent_revision: number | null;
  event_binding_id: string | null;
  event_key: string | null;
  event_revision: string | null;
  calendar_list_revision: string | null;
  read_1: CalendarReadReceipt | null;
  read_2: CalendarReadReceipt | null;
  effective_route: CalendarEffectiveRoute | null;
  artifact_id: string | null;
  file_path: string | null;
  content_sha256: string | null;
  readback_id: string | null;
  verified_at: string | null;
  memory_status: "no_learning" | null;
  failure_code: string | null;
  recovery_action: string | null;
}

export interface CalendarResultPreview {
  schema_version: 1;
  capability_id: "calendar.meeting-prep.v1";
  artifact_id: string;
  readback_id: string;
  file_path: string;
  content_sha256: string;
  event_key: string;
  event_revision: string;
  summary: string;
  agenda: string[];
  questions: string[];
  risks: string[];
  preparation_steps: string[];
}

export interface CalendarApiErrorDetail {
  code: string;
  message: string;
  recovery_action: string | null;
}

// Fixed opportunity plan projections from the governed server.
type OpaqueId = string;
type Sha256 = string;
type UTC = string;
type UUID = string;
export type BlueprintId = 'public-browser-check' | 'public-evidence-report';
export type OpportunityPlanKind = 'opportunity_plan' | 'public-evidence-pipeline.v1';
export type OpportunityPlanStatus =
  | 'pending_inference' | 'proposed' | 'accepted' | 'blocked' | 'rejected' | 'expired';
export type PlanProviderContactState = 'not_started' | 'started' | 'succeeded' | 'unknown';
export type PipelineRecoveryReason =
  | 'input_artifact_cleanup_required' | 'input_artifact_write_failed'
  | 'pipeline_recovery_required' | 'pipeline_materialization_conflict'
  | 'pipeline_output_unverified' | 'pipeline_output_too_large'
  | 'pipeline_handoff_changed' | 'pipeline_input_changed'
  | 'pipeline_root_changed' | 'pipeline_goal_changed'
  | 'pipeline_source_changed' | 'pipeline_source_permission'
  | 'pipeline_review_required' | 'pipeline_expired'
  | 'pipeline_attempts_exhausted' | 'pipeline_task_changed'
  | 'pipeline_producer_changed' | 'pipeline_plan_changed'
  | 'source_stale' | 'goal_review_required' | 'opportunity_expired';

export interface OpportunityPlanRequest {
  expected_opportunity_revision: number; // strict integer >=1
  expected_goal_revision: number; // strict integer >=1
  idempotency_key: UUID;
}
export interface OpportunityPlanReference {
  proposal_id: OpaqueId; // report operation_id is this SAME canonical PK
  kind: OpportunityPlanKind; // pending is opportunity_plan; report finalizes after settlement
  proposal_revision: number; // required once atomic pending row exists
  parent_task_id: OpaqueId; // existing staged Triage source/review card
  parent_revision: number; // required original current review revision
  proposal_digest: Sha256 | null; // null until verified canonical result, never zero/empty fake SHA
  expires_at: UTC; // fixed original review expiry; replay never renews it
  status: OpportunityPlanStatus;
  blueprint_id: BlueprintId | null; // null before valid model result
  provider_contact_state?: PlanProviderContactState;
  generation_retry_allowed?: boolean; // absent=false; exact current NEVER-contacted proof only
  recovery_reason?: PipelineRecoveryReason | null; // report only; safe current inspection
}
export interface OpportunityPlanOffer {
  available_blueprint_ids: BlueprintId[]; // pure current eligible set, <=2; independent daily cap
  unavailable_reason: string | null;
  can_generate: boolean;
  generation_block_reason: string | null;
  proposal_ref: OpportunityPlanReference | null;
}
export interface OpportunityPlanResponse {
  opportunity_id: OpaqueId;
  opportunity_revision: number;
  proposal_ref: OpportunityPlanReference | null;
  reason_code: string | null;
}
export interface PlanCitation {
  source_id: OpaqueId;
  start_line: number; // strict 1..200
  end_line: number; // start<=end<=exact offered LF line count
  span_sha256: Sha256;
}
export interface OpportunityPlanModelResult {
  schema_version: 'seraph.opportunity.plan.v1';
  blueprint_id: BlueprintId; // exact server-offered subset
  title: string; // 1..160 characters
  reason: string; // 1..1000 characters
  citations: PlanCitation[]; // 1..4, same offered IDs/spans; <=16KiB strict JSON
}
export interface BrowserCheck {
  kind: 'url_host' | 'url_path_prefix';
  value: string;
}
export interface FixedBrowserInput {
  schema_version: 1;
  start_url: string;
  allowed_hosts: string[]; // exactly one current server-selected public host
  approved_url_prefixes: string[]; // exactly the current source URL
  actions: [
    { kind: 'navigate'; url: string; expected_checks: BrowserCheck[] },
    { kind: 'extract'; selector: 'body'; max_chars: 8192; expected_checks: BrowserCheck[] }
  ];
  final_expected_checks: BrowserCheck[];
}
export interface OpportunityPlanPreview {
  opportunity_id: OpaqueId;
  opportunity_revision: number;
  blueprint_id: BlueprintId;
  goal_id: OpaqueId;
  goal_revision: number;
  source_id: OpaqueId;
  source_digest: Sha256;
  watch_id: OpaqueId;
  watch_revision: number;
  steps: OpportunityPlanPreviewStep[]; // exact native one or fixed three
  review_expires_at: UTC;
  deadline_at: UTC | null; // report original operation admission deadline after acceptance
  no_learning: true;
  recovery_reason?: PipelineRecoveryReason | null; // report only, no authority
}
export interface OpportunityPlanPreviewStep {
  slot: 'public_source' | 'evidence_dossier' | 'local_report';
  capability_id: 'browser.public-task.v1' | 'work.evidence-dossier.v1' | 'work.local-evidence-report.v1';
  input: FixedBrowserInput | null; // CPU remains null until real verified producer materialization
  input_materialization: 'bound' | 'after_verified_producer';
  output_schema: 'browser_public_task_result' | 'evidence_dossier.v1' | 'text/plain';
  permissions: string[];
  native_approvals: string[]; // current registered descriptions, never approval receipts
  runtime_seconds: number;
  output_bytes: number;
}
export interface BrowserPlanAcceptRequest {
  expected_proposal_revision: number;
  expected_parent_revision: number;
}
export interface ReportPlanAcceptRequest {
  expected_revision: number;
  expected_parent_revision: number;
  expected_digest: Sha256;
}
// Preaccept report UI passes exact matched proposal_ref/plan_preview to the existing
// ArtifactPipelineReview owner; task.pipeline_operation_id remains unbound until accept.
// Only proposed + non-null digest/blueprint + matching kind permits the existing accept.
// Reload/lost response: GET only. Exact manual generation replay needs retained original
// request plus generation_retry_allowed=true; contacted/Unknown never enables resend.
// Existing accepted pipeline replay remains keyed by its original accepted_digest;
// do not add incoming revision/body equality requirements to that ordinary compatibility.
