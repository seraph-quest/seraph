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

export interface GoalInfo {
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
  | "renew_review";

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
  readback_status: WorkBoardReadbackStatus;
  verification_status: WorkBoardVerificationStatus;
  created_at: string;
  updated_at: string;
}

/** Safe task projection returned by the authenticated /api/work-board routes. */
export interface WorkBoardTask {
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
  task: WorkBoardTask;
  attempts: WorkBoardAttempt[];
  parents: string[];
  children: string[];
  comments: WorkBoardComment[];
  events: WorkBoardEvent[];
  parent_handoffs?: WorkBoardSafeParentHandoff[];
  revision: number;
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
  limit_source: "goal_admission_budget" | "default";
}

export interface WorkBoardTaskCreateRequest {
  title: string;
  body?: string;
  goal_id: string;
  goal_revision: number;
  status?: "triage" | "todo";
  capability_id?: string | null;
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
  kind: "specify" | "decompose";
  proposal_id: string;
  proposal_revision: number;
  parent_task_id: string;
  parent_revision: number;
  idempotency_key?: string;
  proposal_digest: string;
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
