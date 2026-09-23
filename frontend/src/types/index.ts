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

/** Canonical task states exposed by the authenticated operator work board. */
export type WorkBoardTaskStatus =
  | "triage"
  | "todo"
  | "ready"
  | "running"
  | "blocked"
  | "review"
  | "done"
  | "archived";

export type WorkBoardBlockKind =
  | "dependency"
  | "needs_input"
  | "capability"
  | "transient"
  | "cancelled"
  | "review_expired"
  | "unknown_effect"
  | "operator";

/**
 * Operator-safe receipt/artifact projection returned by the work-board API.
 *
 * This is deliberately a closed set. The backend strips workflow payloads and
 * secrets before storing these references, and the UI must not render unknown
 * object fields as arbitrary JSON or prose.
 */
export interface WorkBoardSafeReference {
  artifact_id?: string | null;
  artifact_type?: string | null;
  file_path?: string | null;
  content_sha256?: string | null;
  size_bytes?: number | null;
  exists?: boolean | null;
  effect_id?: string | null;
  effect_type?: string | null;
  status?: string | null;
  verified?: boolean | null;
  target_digest?: string | null;
  target_path?: string | null;
  job_id?: string | null;
  workflow_run_id?: string | null;
  recovery_action?: string | null;
  reason_code?: string | null;
  error_code?: string | null;
  child_job_id?: string | null;
}

export type WorkBoardReference = string | WorkBoardSafeReference;

export interface WorkBoardTask {
  task_id: string;
  creation_sequence?: number;
  owner_principal_id?: string;
  owner_session_id?: string;
  origin_thread_id?: string | null;
  goal_id: string;
  goal_revision: number;
  title: string;
  body?: string | null;
  capability_id?: string | null;
  typed_input_ref?: string | null;
  typed_input_digest?: string | null;
  executor_id?: string | null;
  assignee_id?: string | null;
  priority: number;
  idempotency_scope?: string | null;
  idempotency_key?: string | null;
  scheduled_at?: string | null;
  status: WorkBoardTaskStatus;
  block_kind?: WorkBoardBlockKind | null;
  block_reason?: string | null;
  block_source_status?: WorkBoardTaskStatus | null;
  requires_review: boolean;
  reviewer_id?: string | null;
  /** Detail responses expose this as `revision`; task list payloads may use `task_revision`. */
  revision?: number;
  task_revision?: number;
  created_at: string;
  updated_at: string;
  completed_at?: string | null;
  archived_at?: string | null;
  result_refs?: WorkBoardReference[];
  artifact_refs?: WorkBoardReference[];
  dependency_count?: number;
  completed_dependency_count?: number;
  latest_attempt?: WorkBoardAttempt | null;
}

export interface WorkBoardAttempt {
  attempt_id: string;
  task_id?: string;
  workflow_run_id?: string | null;
  task_revision_at_claim: number;
  lease_owner?: string | null;
  lease_expires_at?: string | null;
  lease_heartbeat_at?: string | null;
  heartbeat_at?: string | null;
  fence?: number | null;
  fencing_token?: number | null;
  executor_id?: string | null;
  started_at?: string | null;
  ended_at?: string | null;
  outcome?: string | null;
  receipt_refs?: WorkBoardReference[];
  artifact_refs?: WorkBoardReference[];
  readback_status?: string | null;
}

export interface WorkBoardLink {
  parent_task_id: string;
  child_task_id: string;
  created_at?: string;
}

export interface WorkBoardComment {
  comment_id: string;
  task_id?: string;
  author_principal_id?: string;
  body: string;
  created_at: string;
}

export interface WorkBoardEvent {
  event_id: number;
  task_id: string;
  actor?: string | null;
  kind: string;
  created_at: string;
  metadata?: Record<string, unknown>;
}

export interface WorkBoardTaskDetail extends WorkBoardTask {
  attempts: WorkBoardAttempt[];
  parents: WorkBoardLink[];
  children: WorkBoardLink[];
  comments: WorkBoardComment[];
  events: WorkBoardEvent[];
  recovery_action?: string | null;
}

export interface WorkBoardTaskListResponse {
  tasks: WorkBoardTask[];
  next_after?: number | string | null;
  last_event_id?: number | null;
  gap?: boolean;
}

export interface WorkBoardEventListResponse {
  events: WorkBoardEvent[];
  last_event_id?: number | null;
  gap?: boolean;
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
