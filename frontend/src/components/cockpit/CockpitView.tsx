import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";

import { appEventBus } from "../../lib/appEventBus";
import { API_URL } from "../../config/constants";
import { SERAPH_BUILD_ID } from "../../config/release";
import { useChatStore } from "../../stores/chatStore";
import { useQuestStore } from "../../stores/questStore";
import { useCockpitLayoutStore } from "../../stores/cockpitLayoutStore";
import { PANEL_MIN_SIZES, usePanelLayoutStore } from "../../stores/panelLayoutStore";
import type { ChatMessage, GoalInfo } from "../../types";
import {
  buildWorkflowDraft,
  workflowAcceptsArtifact,
  workflowArtifactInputs,
  type WorkflowInfo,
} from "../settings/workflowDraft";
import { useDragResize } from "../../hooks/useDragResize";
import { ResizeHandles } from "../ResizeHandles";
import {
  collectArtifacts,
  formatInspectorValue,
  type ArtifactRecord,
  type CockpitAuditEvent,
  type WorkflowRunRecord,
  type WorkflowStepRecord,
  type WorkflowTimelineEntry,
} from "./inspector";
import {
  COCKPIT_PANES,
  COCKPIT_LAYOUTS,
  CORE_PANE_IDS,
  getCockpitLayout,
  getDefaultPaneVisibility,
  type CockpitPaneId,
} from "./layouts";
import { SeraphPresencePane } from "./SeraphPresencePane";

interface CockpitViewProps {
  onSend: (message: string) => boolean | void | Promise<boolean | void>;
  onSkipOnboarding?: () => void;
}

interface ObserverState {
  time_of_day?: string;
  day_of_week?: string;
  is_working_hours?: boolean;
  user_state?: string;
  interruption_mode?: string;
  attention_budget_remaining?: number;
  active_window?: string | null;
  screen_context?: string | null;
  active_goals_summary?: string;
  data_quality?: string;
  upcoming_events?: Array<{ summary?: string; start?: string }>;
}

interface RuntimeStatus {
  version: string;
  build_id: string;
  provider: string;
  model: string;
  model_label: string;
  api_base?: string;
  timezone?: string;
  llm_logging_enabled?: boolean;
}

interface PendingApproval {
  id: string;
  session_id?: string | null;
  thread_id?: string | null;
  thread_label?: string | null;
  tool_name: string;
  risk_level: string;
  status: string;
  summary: string;
  created_at: string;
  resume_message?: string | null;
  extension_id?: string | null;
  extension_display_name?: string | null;
  extension_action?: string | null;
  package_path?: string | null;
  lifecycle_boundaries?: string[] | null;
  permissions?: Record<string, unknown> | null;
}

interface DaemonPresenceState {
  connected: boolean;
  pending_notification_count: number;
  capture_mode: string;
  last_native_notification_outcome?: string | null;
}

interface GuardianContinuityIntervention {
  id: string;
  session_id?: string | null;
  thread_id?: string | null;
  thread_label?: string | null;
  thread_source?: string | null;
  intervention_type: string;
  content_excerpt: string;
  policy_action: string;
  policy_reason: string;
  delivery_decision?: string | null;
  latest_outcome: string;
  transport?: string | null;
  notification_id?: string | null;
  feedback_type?: string | null;
  continuation_mode?: string | null;
  resume_message?: string | null;
  updated_at: string;
  continuity_surface: string;
}

interface ObserverReachRouteStatus {
  route: string;
  label: string;
  status: string;
  summary: string;
  selected_transport?: string | null;
  selected_mode?: string | null;
  repair_hint?: string | null;
}

interface ObserverContinuitySnapshot {
  daemon: DaemonPresenceState;
  notifications: Array<{
    id: string;
    intervention_id: string | null;
    title: string;
    body: string;
    intervention_type: string | null;
    urgency: number | null;
    created_at: string;
    surface?: string | null;
    session_id?: string | null;
    thread_id?: string | null;
    thread_label?: string | null;
    thread_source?: string | null;
    continuation_mode?: string | null;
    resume_message?: string | null;
  }>;
  queued_insights: Array<{
    id: string;
    intervention_id: string | null;
    content_excerpt: string;
    intervention_type: string;
    urgency: number;
    reasoning: string;
    session_id?: string | null;
    thread_id?: string | null;
    thread_label?: string | null;
    thread_source?: string | null;
    continuation_mode?: string | null;
    resume_message?: string | null;
    created_at: string;
  }>;
  queued_insight_count: number;
  recent_interventions: GuardianContinuityIntervention[];
  reach?: {
    route_statuses?: ObserverReachRouteStatus[];
  };
}

interface SkillInfo {
  name: string;
  enabled: boolean;
  description?: string;
  file_path?: string;
  source?: string;
  extension_id?: string | null;
  requires_tools?: string[];
  user_invocable?: boolean;
  availability?: "ready" | "blocked" | "disabled";
  missing_tools?: string[];
  recommended_actions?: CapabilityAction[];
}

interface McpServerInfo {
  name: string;
  enabled: boolean;
  url?: string;
  description?: string;
  headers?: Record<string, string> | null;
  connected?: boolean;
  tool_count?: number;
  status?: "disconnected" | "connected" | "auth_required" | "error";
  status_message?: string | null;
  has_headers?: boolean;
  auth_hint?: string;
  source?: string;
  extension_id?: string | null;
  extension_reference?: string | null;
  extension_display_name?: string | null;
  availability?: "ready" | "blocked" | "disabled";
  blocked_reason?: string | null;
  recommended_actions?: CapabilityAction[];
}

interface ToolInfo {
  name: string;
  description?: string;
  risk_level?: string;
  execution_boundaries?: string[];
  accepts_secret_refs?: boolean;
  availability?: "ready" | "blocked";
  blocked_reason?: string | null;
  recommended_actions?: CapabilityAction[];
}

interface OperatorEntity {
  entityType:
    | "tool"
    | "skill"
    | "mcp"
    | "starter_pack"
    | "workflow_definition"
    | "extension_manifest"
    | "activity_item";
  name: string;
  meta: string;
  summary: string;
  details: Record<string, unknown>;
}

interface StarterPackInfo {
  name: string;
  label: string;
  description: string;
  sample_prompt?: string;
  skills: string[];
  workflows: string[];
  ready_skills: string[];
  ready_workflows: string[];
  blocked_skills: Array<{ name: string; availability: string; missing_tools?: string[] }>;
  blocked_workflows: Array<{
    name: string;
    availability: string;
    missing_tools?: string[];
    missing_skills?: string[];
  }>;
  availability: "ready" | "partial" | "blocked";
  recommended_actions?: CapabilityAction[];
}

interface CapabilityAction {
  type:
    | "toggle_skill"
    | "toggle_workflow"
    | "enable_extension"
    | "toggle_mcp_server"
    | "test_mcp_server"
    | "test_native_notification"
    | "set_tool_policy"
    | "set_mcp_policy"
    | "install_catalog_item"
    | "activate_starter_pack"
    | "draft_workflow"
    | "open_settings";
  label: string;
  name?: string;
  mode?: string;
  enabled?: boolean;
  target?: string;
  status?: string;
  detail?: string;
  [key: string]: unknown;
}

interface CapabilityBootstrapResponse {
  target_type: string;
  name: string;
  label: string;
  status: string;
  ready: boolean;
  availability: string;
  blocking_reasons: string[];
  applied_actions: Array<Record<string, unknown>>;
  manual_actions: CapabilityAction[];
  command?: string | null;
  parameter_schema?: Record<string, unknown>;
  risk_level?: string | null;
  execution_boundaries?: string[];
  overview?: CapabilityOverview;
}

interface CapabilityPreflightResponse {
  target_type: string;
  name: string;
  label: string;
  description: string;
  availability: string;
  blocking_reasons: string[];
  recommended_actions: CapabilityAction[];
  autorepair_actions: CapabilityAction[];
  command?: string | null;
  parameter_schema?: Record<string, unknown>;
  risk_level?: string | null;
  execution_boundaries?: string[];
  can_autorepair: boolean;
  ready: boolean;
}

interface WorkflowDiagnosticsPayload {
  loaded_count: number;
  error_count: number;
  workflows: Array<Record<string, unknown>>;
  load_errors: Array<{ file_path: string; message: string }>;
}

interface DoctorPlanRecord {
  id: string;
  label: string;
  source: string;
  createdAt: string;
  availability: string;
  blockingReasons: string[];
  autorepairActions: CapabilityAction[];
  recommendedActions: CapabilityAction[];
  manualActions: CapabilityAction[];
  appliedActions: string[];
  command?: string | null;
  riskLevel?: string | null;
  executionBoundaries?: string[];
}

interface ExtensionStudioEntry {
  id: string;
  entityType: "workflow_definition" | "skill" | "mcp" | "extension_manifest";
  name: string;
  summary: string;
  availability: string;
  meta: string;
  entity: OperatorEntity;
  extensionId?: string | null;
  packageReference?: string | null;
  packageDisplayName?: string | null;
  packageVersion?: string | null;
  packageLocation?: string | null;
  packageTrust?: string | null;
  studioFormat?: string | null;
  saveSupported?: boolean;
  validationSupported?: boolean;
}

interface ExtensionStudioFileInfo {
  key: string;
  role: "manifest" | "contribution";
  reference: string;
  resolved_path?: string | null;
  label: string;
  display_type: string;
  contribution_type?: string | null;
  format: string;
  editable: boolean;
  save_supported: boolean;
  validation_supported: boolean;
  loaded: boolean;
  name?: string | null;
  status?: string | null;
  source?: string | null;
}

interface ExtensionIssueInfo {
  code: string;
  severity: string;
  message: string;
  contribution_type?: string | null;
  reference?: string | null;
  suggested_fix?: string | null;
}

interface ExtensionLoadErrorInfo {
  source: string;
  message: string;
  phase: string;
  details: Array<Record<string, unknown>>;
}

interface ExtensionToggleTargetInfo {
  type: string;
  name: string;
}

interface ExtensionPermissionSummary {
  status: string;
  ok: boolean;
  required: Record<string, unknown>;
  missing: Record<string, unknown>;
  risk_level: string;
}

interface ExtensionApprovalProfile {
  requires_runtime_approval: boolean;
  runtime_behavior: string;
  requires_lifecycle_approval: boolean;
  lifecycle_boundaries: string[];
  risk_level: string;
}

interface ExtensionContributionPermissionProfile {
  status: string;
  requires_network: boolean;
  missing_network: boolean;
  requires_approval: boolean;
  approval_behavior: string;
  missing_tools: string[];
  missing_execution_boundaries: string[];
}

interface ExtensionContributionHealth {
  state: string;
  summary?: string | null;
  ready?: boolean;
  enabled?: boolean;
  configured?: boolean;
  connected?: boolean;
  error?: string | null;
}

interface ExtensionContributionInfo {
  type: string;
  reference: string;
  resolved_path?: string | null;
  name?: string | null;
  description?: string | null;
  status?: string | null;
  loaded: boolean;
  enabled?: boolean | null;
  configured?: boolean | null;
  default_enabled?: boolean | null;
  availability?: string | null;
  source?: string | null;
  platform?: string | null;
  provider_kind?: string | null;
  trigger_type?: string | null;
  schedule?: string | null;
  endpoint?: string | null;
  topic?: string | null;
  adapter_kind?: string | null;
  transport?: string | null;
  source_type?: string | null;
  runtime_profile?: string | null;
  surface_kind?: string | null;
  preferred_panel?: string | null;
  output_surface?: string | null;
  effective_output_surface?: string | null;
  health?: ExtensionContributionHealth | null;
  permission_profile?: ExtensionContributionPermissionProfile | null;
  config_fields: Array<Record<string, unknown>>;
  config_keys: string[];
  capabilities: string[];
  delivery_modes: string[];
  requires_network: boolean;
  requires_daemon: boolean;
  approval_behavior?: string | null;
  requires_approval?: boolean;
}

interface ExtensionConnectorSummary {
  total: number;
  ready: number;
  states: Record<string, number>;
}

interface ExtensionPackageInfo {
  id: string;
  display_name: string;
  version?: string | null;
  kind: string;
  trust: string;
  source: string;
  location: string;
  status: string;
  summary?: string | null;
  description?: string | null;
  compatibility?: { seraph: string } | null;
  publisher?: { name: string; homepage?: string | null; support?: string | null } | null;
  issues: ExtensionIssueInfo[];
  load_errors: ExtensionLoadErrorInfo[];
  toggle_targets: ExtensionToggleTargetInfo[];
  toggleable_contribution_types: string[];
  passive_contribution_types: string[];
  enable_supported: boolean;
  disable_supported: boolean;
  removable: boolean;
  enabled_scope: string;
  configurable: boolean;
  metadata_supported: boolean;
  config_scope: string;
  enabled?: boolean | null;
  config: Record<string, unknown>;
  permission_summary?: ExtensionPermissionSummary | null;
  approval_profile?: ExtensionApprovalProfile | null;
  connector_summary?: ExtensionConnectorSummary | null;
  contributions: ExtensionContributionInfo[];
  studio_files: ExtensionStudioFileInfo[];
}

interface ExtensionLifecyclePlan {
  mode: string;
  recommended_action: "install" | "update" | "none";
  install_allowed: boolean;
  update_supported: boolean;
  current_location?: string | null;
  current_version?: string | null;
  current_source?: string | null;
  candidate_version?: string | null;
  version_relation?: string | null;
  package_changed: boolean;
}

interface ExtensionPathPreview {
  path: string;
  extension_id: string;
  display_name: string;
  version?: string | null;
  ok: boolean;
  results: Array<{ issues?: unknown[] }>;
  load_errors?: Array<Record<string, unknown>>;
  lifecycle_plan?: ExtensionLifecyclePlan | null;
}

interface ExtensionScaffoldResponse {
  status: string;
  path: string;
  created_files: string[];
  preview: ExtensionPathPreview;
}

interface ExtensionLifecycleApprovalDetail {
  type: "approval_required";
  approval_id: string;
  tool_name: string;
  risk_level: string;
  message: string;
}

type LoggedOperatorError = Error & { operatorLogged?: boolean };

interface CatalogItemInfo {
  name: string;
  catalog_id?: string;
  type: "skill" | "mcp_server" | "extension_pack";
  description: string;
  category?: string;
  bundled?: boolean;
  installed: boolean;
  missing_tools?: string[];
  contribution_types?: string[];
  trust?: string;
  version?: string | null;
  installed_version?: string | null;
  update_available?: boolean;
  status?: string;
  doctor_ok?: boolean;
  issues?: unknown[];
  load_errors?: unknown[];
  recommended_actions?: CapabilityAction[];
}

interface CapabilityRecommendation {
  id: string;
  label: string;
  description: string;
  action?: CapabilityAction | null;
}

interface RunbookInfo {
  id: string;
  name?: string;
  label: string;
  description: string;
  source: "starter_pack" | "workflow";
  command: string;
  availability?: "ready" | "partial" | "blocked" | "disabled";
  blocking_reasons?: string[];
  recommended_actions?: CapabilityAction[];
  parameter_schema?: Record<string, unknown>;
  risk_level?: string;
  execution_boundaries?: string[];
  action?: CapabilityAction | null;
}

interface CapabilityOverview {
  tool_policy_mode: ToolPolicyMode;
  mcp_policy_mode: McpPolicyMode;
  approval_mode: ApprovalMode;
  summary: {
    native_tools_ready: number;
    native_tools_total: number;
    skills_ready: number;
    skills_total: number;
    workflows_ready: number;
    workflows_total: number;
    starter_packs_ready: number;
    starter_packs_total: number;
    mcp_servers_ready: number;
    mcp_servers_total: number;
  };
  native_tools: ToolInfo[];
  skills: SkillInfo[];
  workflows: WorkflowInfo[];
  mcp_servers: McpServerInfo[];
  starter_packs: StarterPackInfo[];
  catalog_items: CatalogItemInfo[];
  recommendations: CapabilityRecommendation[];
  runbooks: RunbookInfo[];
}

interface ActivityLedgerSummary {
  window_hours: number;
  started_at: string;
  total_items: number;
  visible_items?: number;
  visible_groups?: number;
  is_partial?: boolean;
  partial_sources?: string[];
  pending_approvals: number;
  failure_count: number;
  llm_call_count: number;
  llm_cost_usd: number;
  input_tokens: number;
  output_tokens: number;
  user_triggered_llm_calls: number;
  autonomous_llm_calls: number;
  llm_cost_by_runtime_path?: Array<{
    key: string;
    calls: number;
    cost_usd: number;
    input_tokens: number;
    output_tokens: number;
  }>;
  llm_cost_by_capability_family?: Array<{
    key: string;
    calls: number;
    cost_usd: number;
    input_tokens: number;
    output_tokens: number;
  }>;
  categories: Record<string, number>;
}

type ActivityLedgerCategory = "llm" | "workflow" | "approval" | "guardian" | "agent" | "system";
type ActivityLedgerFilter = "all" | ActivityLedgerCategory;

interface ActivityLedgerEntry {
  id: string;
  kind: string;
  category: ActivityLedgerCategory;
  group_key?: string | null;
  title: string;
  summary: string;
  status: string;
  created_at: string;
  updated_at: string;
  thread_id?: string | null;
  thread_label?: string | null;
  continue_message?: string | null;
  replay_draft?: string | null;
  replay_allowed?: boolean;
  replay_block_reason?: string | null;
  recommended_actions?: CapabilityAction[];
  source: string;
  model?: string | null;
  provider?: string | null;
  prompt_tokens?: number | null;
  completion_tokens?: number | null;
  cost_usd?: number | null;
  duration_ms?: number | null;
  metadata?: Record<string, unknown>;
}

interface ActivityLedgerGroupChild {
  key: string;
  icon: string;
  label: string;
  summary: string;
  meta: string;
  status: string;
  item?: ActivityLedgerEntry;
}

interface ActivityLedgerGroup {
  key: string;
  lead: ActivityLedgerEntry;
  icon: string;
  title: string;
  summary: string;
  detail?: string | null;
  meta: string;
  footer: string | null;
  updatedAt: string;
  children: ActivityLedgerGroupChild[];
}

interface ImportedCapabilityFamilySummary {
  type: string;
  label: string;
  total: number;
  installed: number;
  ready: number;
  attention: number;
  approval: number;
  packages: string[];
  entries: Array<{
    packageId: string;
    packageLabel: string;
    contribution: ExtensionContributionInfo;
  }>;
}

interface ExtensionGovernanceSummary {
  packageId: string;
  label: string;
  riskLevel: string;
  status: string;
  detail: string;
  packageInfo: ExtensionPackageInfo;
}

interface OperatorTriageEntry {
  id: string;
  kind: "approval" | "workflow" | "queued" | "reach";
  label: string;
  detail: string;
  meta: string;
  priority: number;
  threadId?: string | null;
  continueMessage?: string | null;
  approval?: PendingApproval;
  workflow?: WorkflowRunRecord;
  route?: ObserverReachRouteStatus;
}

interface OperatorEvidenceEntry {
  id: string;
  kind: "approval" | "artifact" | "trace";
  label: string;
  detail: string;
  meta: string;
  sortKey: number;
  threadId?: string | null;
  continueMessage?: string | null;
  approval?: PendingApproval;
  artifact?: ArtifactRecord;
  workflow?: WorkflowRunRecord;
  trace?: ChatMessage;
  audit?: CockpitAuditEvent | null;
}

type ToolPolicyMode = "safe" | "balanced" | "full";
type McpPolicyMode = "disabled" | "approval" | "full";
type ApprovalMode = "off" | "high_risk";

type InspectorSelection =
  | { kind: "approval"; approval: PendingApproval }
  | { kind: "workflow"; workflow: WorkflowRunRecord }
  | { kind: "intervention"; intervention: GuardianContinuityIntervention }
  | { kind: "trace"; message: ChatMessage }
  | { kind: "audit"; event: CockpitAuditEvent }
  | { kind: "operator"; entity: OperatorEntity }
  | { kind: "artifact"; artifact: ArtifactRecord };

function formatAge(value: number | string): string {
  const timestamp = typeof value === "number" ? value : new Date(value).getTime();
  const deltaSeconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
  if (deltaSeconds < 60) return `${deltaSeconds}s`;
  const deltaMinutes = Math.floor(deltaSeconds / 60);
  if (deltaMinutes < 60) return `${deltaMinutes}m`;
  const deltaHours = Math.floor(deltaMinutes / 60);
  return `${deltaHours}h`;
}

function labelForRole(message: ChatMessage): string {
  if (message.role === "approval") return "approval";
  if (message.role === "clarification") return "clarification";
  if (message.role === "proactive") return message.interventionType ?? "proactive";
  if (message.role === "step") return message.toolUsed ?? "step";
  return message.role;
}

function isEditableTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  return Boolean(
    target.isContentEditable
    || target.closest("input, textarea, select, [contenteditable=\"true\"]"),
  );
}

function formatContinuityLabel(value: string | null | undefined): string {
  return (value || "unknown").replace(/_/g, " ");
}

function formatOperatorMode(value: string): string {
  return value.replace(/_/g, " ");
}

function formatCapabilityAction(action: Record<string, unknown>): string {
  const type = typeof action.type === "string" ? action.type : "action";
  const name = typeof action.name === "string" ? action.name : null;
  const mode = typeof action.mode === "string" ? action.mode : null;
  const status = typeof action.status === "string" ? action.status : null;
  const detail = typeof action.detail === "string" ? action.detail : null;
  const target = (typeof action.target === "string" ? action.target : null) ?? name ?? mode ?? detail ?? "";
  return `${type.replace(/_/g, " ")}${target ? ` · ${target}` : ""}${status ? ` · ${status}` : ""}`;
}

const SUPPORTED_CAPABILITY_ACTION_TYPES = new Set<CapabilityAction["type"]>([
  "enable_extension",
  "toggle_skill",
  "toggle_workflow",
  "toggle_mcp_server",
  "test_mcp_server",
  "test_native_notification",
  "set_tool_policy",
  "set_mcp_policy",
  "install_catalog_item",
  "activate_starter_pack",
  "open_settings",
]);

const LOW_RISK_BATCH_CAPABILITY_ACTION_TYPES = new Set<CapabilityAction["type"]>([
  "toggle_skill",
  "toggle_workflow",
  "test_mcp_server",
  "test_native_notification",
  "open_settings",
]);

function isLowRiskBatchCapabilityAction(action: CapabilityAction): boolean {
  return LOW_RISK_BATCH_CAPABILITY_ACTION_TYPES.has(action.type);
}

const IMPORTED_CAPABILITY_FAMILY_DEFS = [
  { type: "toolset_presets", label: "toolsets" },
  { type: "context_packs", label: "context packs" },
  { type: "browser_providers", label: "browser providers" },
  { type: "automation_triggers", label: "automation triggers" },
  { type: "messaging_connectors", label: "messaging" },
  { type: "speech_profiles", label: "speech" },
  { type: "node_adapters", label: "node adapters" },
  { type: "canvas_outputs", label: "canvas outputs" },
  { type: "workflow_runtimes", label: "workflow runtimes" },
  { type: "channel_adapters", label: "channel adapters" },
  { type: "observer_definitions", label: "observer sources" },
] as const;

function activitySpendBucketLabel(value: string): string {
  return value.replace(/[_-]+/g, " ");
}

function summarizeMissingPermissions(summary: ExtensionPermissionSummary | null | undefined): string[] {
  if (!summary) return [];
  const parts: string[] = [];
  if (summary.missing.network === true) {
    parts.push("network");
  }
  if (Array.isArray(summary.missing.tools) && summary.missing.tools.length) {
    parts.push(`${summary.missing.tools.length} tool${summary.missing.tools.length === 1 ? "" : "s"}`);
  }
  if (
    Array.isArray(summary.missing.execution_boundaries)
    && summary.missing.execution_boundaries.length
  ) {
    parts.push(`${summary.missing.execution_boundaries.length} ${summary.missing.execution_boundaries.length === 1 ? "boundary" : "boundaries"}`);
  }
  return parts;
}

function isContributionActive(contribution: ExtensionContributionInfo): boolean {
  const status = (contribution.status ?? contribution.health?.state ?? "").trim().toLowerCase();
  if (contribution.loaded === false) return false;
  if (contribution.enabled === false) return false;
  if (contribution.health?.enabled === false) return false;
  if (contribution.configured === false || contribution.health?.configured === false) return false;
  return ![
    "planned",
    "requires_config",
    "invalid",
    "invalid_config",
    "overridden",
    "disabled",
    "unloaded",
  ].includes(status);
}

function readActionList(value: unknown): CapabilityAction[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    if (!item || typeof item !== "object" || Array.isArray(item)) return [];
    const record = item as Record<string, unknown>;
    if (typeof record.type !== "string" || typeof record.label !== "string") return [];
    return [{
      ...record,
      type: record.type,
      label: record.label,
    } as CapabilityAction];
  });
}

function shortIdentifier(value: string | null | undefined, size = 8): string | null {
  if (!value) return null;
  return value.length > size ? value.slice(0, size) : value;
}

function managedFileName(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  if (!trimmed) return null;
  const segments = trimmed.split(/[\\/]/).filter(Boolean);
  const candidate = segments[segments.length - 1];
  return candidate && candidate.trim() ? candidate.trim() : null;
}

function normalizeExtensionStudioFile(value: Record<string, unknown>): ExtensionStudioFileInfo | null {
  if (typeof value.key !== "string" || typeof value.reference !== "string" || typeof value.label !== "string") {
    return null;
  }
  const role = value.role === "manifest" ? "manifest" : "contribution";
  return {
    key: value.key,
    role,
    reference: value.reference,
    resolved_path: typeof value.resolved_path === "string" ? value.resolved_path : null,
    label: value.label,
    display_type: typeof value.display_type === "string" ? value.display_type : "file",
    contribution_type: typeof value.contribution_type === "string" ? value.contribution_type : null,
    format: typeof value.format === "string" ? value.format : "text",
    editable: Boolean(value.editable),
    save_supported: Boolean(value.save_supported),
    validation_supported: Boolean(value.validation_supported),
    loaded: value.loaded !== false,
    name: typeof value.name === "string" ? value.name : null,
    status: typeof value.status === "string" ? value.status : null,
    source: typeof value.source === "string" ? value.source : null,
  };
}

function normalizeExtensionIssue(value: Record<string, unknown>): ExtensionIssueInfo | null {
  if (
    typeof value.code !== "string"
    || typeof value.severity !== "string"
    || typeof value.message !== "string"
  ) {
    return null;
  }
  return {
    code: value.code,
    severity: value.severity,
    message: value.message,
    contribution_type: typeof value.contribution_type === "string" ? value.contribution_type : null,
    reference: typeof value.reference === "string" ? value.reference : null,
    suggested_fix: typeof value.suggested_fix === "string" ? value.suggested_fix : null,
  };
}

function normalizeExtensionLoadError(value: Record<string, unknown>): ExtensionLoadErrorInfo | null {
  if (
    typeof value.source !== "string"
    || typeof value.message !== "string"
    || typeof value.phase !== "string"
  ) {
    return null;
  }
  return {
    source: value.source,
    message: value.message,
    phase: value.phase,
    details: Array.isArray(value.details)
      ? value.details.flatMap((entry) => (
        entry && typeof entry === "object" && !Array.isArray(entry)
          ? [entry as Record<string, unknown>]
          : []
      ))
      : [],
  };
}

function normalizeExtensionToggleTarget(value: Record<string, unknown>): ExtensionToggleTargetInfo | null {
  if (typeof value.type !== "string" || typeof value.name !== "string") {
    return null;
  }
  return {
    type: value.type,
    name: value.name,
  };
}

function normalizeExtensionPermissionSummary(value: unknown): ExtensionPermissionSummary | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const record = value as Record<string, unknown>;
  return {
    status: typeof record.status === "string" ? record.status : "unknown",
    ok: Boolean(record.ok),
    required: record.required && typeof record.required === "object" && !Array.isArray(record.required)
      ? record.required as Record<string, unknown>
      : {},
    missing: record.missing && typeof record.missing === "object" && !Array.isArray(record.missing)
      ? record.missing as Record<string, unknown>
      : {},
    risk_level: typeof record.risk_level === "string" ? record.risk_level : "low",
  };
}

function normalizeExtensionApprovalProfile(value: unknown): ExtensionApprovalProfile | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const record = value as Record<string, unknown>;
  return {
    requires_runtime_approval: Boolean(record.requires_runtime_approval),
    runtime_behavior: typeof record.runtime_behavior === "string" ? record.runtime_behavior : "never",
    requires_lifecycle_approval: Boolean(record.requires_lifecycle_approval),
    lifecycle_boundaries: Array.isArray(record.lifecycle_boundaries)
      ? record.lifecycle_boundaries.filter((entry): entry is string => typeof entry === "string")
      : [],
    risk_level: typeof record.risk_level === "string" ? record.risk_level : "low",
  };
}

function normalizeContributionPermissionProfile(value: unknown): ExtensionContributionPermissionProfile | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const record = value as Record<string, unknown>;
  return {
    status: typeof record.status === "string" ? record.status : "unknown",
    requires_network: Boolean(record.requires_network),
    missing_network: Boolean(record.missing_network),
    requires_approval: Boolean(record.requires_approval),
    approval_behavior: typeof record.approval_behavior === "string" ? record.approval_behavior : "never",
    missing_tools: Array.isArray(record.missing_tools)
      ? record.missing_tools.filter((entry): entry is string => typeof entry === "string")
      : [],
    missing_execution_boundaries: Array.isArray(record.missing_execution_boundaries)
      ? record.missing_execution_boundaries.filter((entry): entry is string => typeof entry === "string")
      : [],
  };
}

function normalizeContributionHealth(value: unknown): ExtensionContributionHealth | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const record = value as Record<string, unknown>;
  return {
    state: typeof record.state === "string" ? record.state : "unknown",
    summary: typeof record.summary === "string" ? record.summary : null,
    ready: typeof record.ready === "boolean" ? record.ready : undefined,
    enabled: typeof record.enabled === "boolean" ? record.enabled : undefined,
    configured: typeof record.configured === "boolean" ? record.configured : undefined,
    connected: typeof record.connected === "boolean" ? record.connected : undefined,
    error: typeof record.error === "string" ? record.error : null,
  };
}

function normalizeExtensionConnectorSummary(value: unknown): ExtensionConnectorSummary | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const record = value as Record<string, unknown>;
  return {
    total: typeof record.total === "number" ? record.total : 0,
    ready: typeof record.ready === "number" ? record.ready : 0,
    states: record.states && typeof record.states === "object" && !Array.isArray(record.states)
      ? Object.fromEntries(
        Object.entries(record.states as Record<string, unknown>).flatMap(([key, entry]) => (
          typeof entry === "number" ? [[key, entry]] : []
        )),
      )
      : {},
  };
}

function normalizeExtensionContribution(value: Record<string, unknown>): ExtensionContributionInfo | null {
  if (typeof value.type !== "string" || typeof value.reference !== "string") {
    return null;
  }
  const readString = (field: string) => (typeof value[field] === "string" ? value[field] as string : null);
  const readStringList = (field: string) => (
    Array.isArray(value[field]) ? value[field].filter((entry): entry is string => typeof entry === "string") : []
  );
  return {
    type: value.type,
    reference: value.reference,
    resolved_path: readString("resolved_path"),
    name: readString("name"),
    description: readString("description"),
    status: readString("status"),
    loaded: typeof value.loaded === "boolean" ? value.loaded : true,
    enabled: typeof value.enabled === "boolean" ? value.enabled : null,
    configured: typeof value.configured === "boolean" ? value.configured : null,
    default_enabled: typeof value.default_enabled === "boolean" ? value.default_enabled : null,
    availability: readString("availability"),
    source: readString("source"),
    platform: readString("platform"),
    provider_kind: readString("provider_kind"),
    trigger_type: readString("trigger_type"),
    schedule: readString("schedule"),
    endpoint: readString("endpoint"),
    topic: readString("topic"),
    adapter_kind: readString("adapter_kind"),
    transport: readString("transport"),
    source_type: readString("source_type"),
    runtime_profile: readString("runtime_profile"),
    surface_kind: readString("surface_kind"),
    preferred_panel: readString("preferred_panel"),
    output_surface: readString("output_surface"),
    effective_output_surface: readString("effective_output_surface"),
    health: normalizeContributionHealth(value.health),
    permission_profile: normalizeContributionPermissionProfile(value.permission_profile),
    config_fields: Array.isArray(value.config_fields)
      ? value.config_fields.flatMap((entry) => (
        entry && typeof entry === "object" && !Array.isArray(entry) ? [entry as Record<string, unknown>] : []
      ))
      : [],
    config_keys: readStringList("config_keys"),
    capabilities: readStringList("capabilities"),
    delivery_modes: readStringList("delivery_modes"),
    requires_network: Boolean(value.requires_network),
    requires_daemon: Boolean(value.requires_daemon),
    approval_behavior: readString("approval_behavior"),
    requires_approval: typeof value.requires_approval === "boolean" ? value.requires_approval : undefined,
  };
}

function normalizeExtensionPackage(value: Record<string, unknown>): ExtensionPackageInfo | null {
  if (
    typeof value.id !== "string"
    || typeof value.display_name !== "string"
    || typeof value.kind !== "string"
    || typeof value.trust !== "string"
    || typeof value.source !== "string"
    || typeof value.location !== "string"
    || typeof value.status !== "string"
  ) {
    return null;
  }
  const studioFiles = Array.isArray(value.studio_files)
    ? value.studio_files.flatMap((entry) => (
      entry && typeof entry === "object" && !Array.isArray(entry)
        ? [normalizeExtensionStudioFile(entry as Record<string, unknown>)].filter(Boolean) as ExtensionStudioFileInfo[]
        : []
    ))
    : [];
  const issues = Array.isArray(value.issues)
    ? value.issues.flatMap((entry) => (
      entry && typeof entry === "object" && !Array.isArray(entry)
        ? [normalizeExtensionIssue(entry as Record<string, unknown>)].filter(Boolean) as ExtensionIssueInfo[]
        : []
    ))
    : [];
  const loadErrors = Array.isArray(value.load_errors)
    ? value.load_errors.flatMap((entry) => (
      entry && typeof entry === "object" && !Array.isArray(entry)
        ? [normalizeExtensionLoadError(entry as Record<string, unknown>)].filter(Boolean) as ExtensionLoadErrorInfo[]
        : []
    ))
    : [];
  const toggleTargets = Array.isArray(value.toggle_targets)
    ? value.toggle_targets.flatMap((entry) => (
      entry && typeof entry === "object" && !Array.isArray(entry)
        ? [normalizeExtensionToggleTarget(entry as Record<string, unknown>)].filter(Boolean) as ExtensionToggleTargetInfo[]
        : []
    ))
    : [];
  const contributions = Array.isArray(value.contributions)
    ? value.contributions.flatMap((entry) => (
      entry && typeof entry === "object" && !Array.isArray(entry)
        ? [normalizeExtensionContribution(entry as Record<string, unknown>)].filter(Boolean) as ExtensionContributionInfo[]
        : []
    ))
    : [];
  return {
    id: value.id,
    display_name: value.display_name,
    version: typeof value.version === "string" ? value.version : null,
    kind: value.kind,
    trust: value.trust,
    source: value.source,
    location: value.location,
    status: value.status,
    summary: typeof value.summary === "string" ? value.summary : null,
    description: typeof value.description === "string" ? value.description : null,
    compatibility:
      value.compatibility && typeof value.compatibility === "object" && !Array.isArray(value.compatibility)
        ? { seraph: String((value.compatibility as Record<string, unknown>).seraph ?? "") }
        : null,
    publisher:
      value.publisher && typeof value.publisher === "object" && !Array.isArray(value.publisher)
        ? {
          name: String((value.publisher as Record<string, unknown>).name ?? ""),
          homepage:
            typeof (value.publisher as Record<string, unknown>).homepage === "string"
              ? String((value.publisher as Record<string, unknown>).homepage)
              : null,
          support:
            typeof (value.publisher as Record<string, unknown>).support === "string"
              ? String((value.publisher as Record<string, unknown>).support)
              : null,
        }
        : null,
    issues,
    load_errors: loadErrors,
    toggle_targets: toggleTargets,
    toggleable_contribution_types: Array.isArray(value.toggleable_contribution_types)
      ? value.toggleable_contribution_types.filter((entry): entry is string => typeof entry === "string")
      : [],
    passive_contribution_types: Array.isArray(value.passive_contribution_types)
      ? value.passive_contribution_types.filter((entry): entry is string => typeof entry === "string")
      : [],
    enable_supported: Boolean(value.enable_supported),
    disable_supported: Boolean(value.disable_supported),
    removable: Boolean(value.removable),
    enabled_scope: typeof value.enabled_scope === "string" ? value.enabled_scope : "none",
    configurable: Boolean(value.configurable),
    metadata_supported: Boolean(value.metadata_supported),
    config_scope: typeof value.config_scope === "string" ? value.config_scope : "none",
    enabled:
      typeof value.enabled === "boolean"
        ? value.enabled
        : value.enabled === null
          ? null
          : undefined,
    config:
      value.config && typeof value.config === "object" && !Array.isArray(value.config)
        ? value.config as Record<string, unknown>
        : {},
    permission_summary: normalizeExtensionPermissionSummary(value.permission_summary),
    approval_profile: normalizeExtensionApprovalProfile(value.approval_profile),
    connector_summary: normalizeExtensionConnectorSummary(value.connector_summary),
    contributions,
    studio_files: studioFiles,
  };
}

function normalizeExtensionPackagesPayload(payload: unknown): ExtensionPackageInfo[] {
  if (
    !payload
    || typeof payload !== "object"
    || !Array.isArray((payload as { extensions?: unknown }).extensions)
  ) {
    return [];
  }
  return ((payload as { extensions?: unknown }).extensions as unknown[]).flatMap((entry) => (
    entry && typeof entry === "object" && !Array.isArray(entry)
      ? [normalizeExtensionPackage(entry as Record<string, unknown>)].filter(Boolean) as ExtensionPackageInfo[]
      : []
  ));
}

function normalizeExtensionLifecycleApprovalDetail(payload: unknown): ExtensionLifecycleApprovalDetail | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    return null;
  }
  const detail = (payload as { detail?: unknown }).detail;
  if (!detail || typeof detail !== "object" || Array.isArray(detail)) {
    return null;
  }
  const detailRecord = detail as Record<string, unknown>;
  const type = typeof detailRecord.type === "string" ? detailRecord.type : "";
  if (type !== "approval_required") {
    return null;
  }
  const approvalId = typeof detailRecord.approval_id === "string" ? detailRecord.approval_id : "";
  const toolName = typeof detailRecord.tool_name === "string" ? detailRecord.tool_name : "extension_lifecycle";
  const riskLevel = typeof detailRecord.risk_level === "string" ? detailRecord.risk_level : "high";
  const message = typeof detailRecord.message === "string" ? detailRecord.message : "Approval required before retrying the extension action.";
  return {
    type: "approval_required",
    approval_id: approvalId,
    tool_name: toolName,
    risk_level: riskLevel,
    message,
  };
}

function buildWorkflowDefinitionEntity(workflow: WorkflowInfo): OperatorEntity {
  return {
    entityType: "workflow_definition",
    name: workflow.name,
    meta: `${workflow.risk_level} risk · ${workflow.availability ?? (workflow.is_available === false ? "blocked" : "ready")}`,
    summary: workflow.description,
    details: {
      file_path: workflow.file_path,
      source: workflow.source,
      extension_id: workflow.extension_id ?? null,
      tool_name: workflow.tool_name,
      enabled: workflow.enabled,
      user_invocable: workflow.user_invocable,
      inputs: workflow.inputs,
      requires_tools: workflow.requires_tools,
      requires_skills: workflow.requires_skills,
      missing_tools: workflow.missing_tools ?? [],
      missing_skills: workflow.missing_skills ?? [],
      execution_boundaries: workflow.execution_boundaries,
      approval_behavior: workflow.approval_behavior,
      risk_level: workflow.risk_level,
      availability: workflow.availability ?? (workflow.is_available === false ? "blocked" : "ready"),
      recommended_actions: workflow.recommended_actions ?? [],
    },
  };
}

function buildSkillEntity(skill: SkillInfo): OperatorEntity {
  return {
    entityType: "skill",
    name: skill.name,
    meta: skill.availability ?? (skill.enabled ? "ready" : "disabled"),
    summary: skill.description ?? "Skill capability",
    details: {
      enabled: skill.enabled,
      user_invocable: skill.user_invocable ?? false,
      file_path: skill.file_path ?? "",
      source: skill.source,
      extension_id: skill.extension_id ?? null,
      requires_tools: skill.requires_tools ?? [],
      missing_tools: skill.missing_tools ?? [],
      recommended_actions: skill.recommended_actions ?? [],
      availability: skill.availability ?? (skill.enabled ? "ready" : "disabled"),
    },
  };
}

function buildMcpEntity(server: McpServerInfo): OperatorEntity {
  return {
    entityType: "mcp",
    name: server.name,
    meta: server.status ?? "unknown",
    summary: server.description || server.url || "MCP server",
    details: {
      availability: server.availability ?? "unknown",
      blocked_reason: server.blocked_reason ?? "none",
      tool_count: server.tool_count ?? 0,
      url: server.url ?? "",
      description: server.description ?? "",
      headers: server.headers ?? null,
      auth_hint: server.auth_hint ?? "",
      status_message: server.status_message ?? "",
      enabled: server.enabled,
      source: server.source ?? "manual",
      extension_id: server.extension_id ?? null,
      extension_reference: server.extension_reference ?? null,
      extension_display_name: server.extension_display_name ?? null,
      has_headers: server.has_headers ?? false,
      recommended_actions: server.recommended_actions ?? [],
    },
  };
}

function buildExtensionManifestEntity(extension: ExtensionPackageInfo): OperatorEntity {
  return {
    entityType: "extension_manifest",
    name: extension.display_name,
    meta: `${extension.location} · ${extension.trust} · ${extension.version ?? "unknown version"}`,
    summary: extension.summary || extension.description || "Extension package manifest",
    details: {
      extension_id: extension.id,
      kind: extension.kind,
      trust: extension.trust,
      location: extension.location,
      status: extension.status,
      version: extension.version ?? "",
      compatibility: extension.compatibility ?? null,
      publisher: extension.publisher ?? null,
      file_path: "manifest.yaml",
      studio_reference: "manifest.yaml",
      save_supported: extension.location === "workspace",
      validation_supported: true,
      issues: extension.issues,
      load_errors: extension.load_errors,
      toggle_targets: extension.toggle_targets,
      toggleable_contribution_types: extension.toggleable_contribution_types,
      passive_contribution_types: extension.passive_contribution_types,
      enable_supported: extension.enable_supported,
      disable_supported: extension.disable_supported,
      removable: extension.removable,
      enabled_scope: extension.enabled_scope,
      configurable: extension.configurable,
      metadata_supported: extension.metadata_supported,
      config_scope: extension.config_scope,
      enabled: extension.enabled ?? null,
      config: extension.config,
      permission_summary: extension.permission_summary ?? null,
      approval_profile: extension.approval_profile ?? null,
      connector_summary: extension.connector_summary ?? null,
      contributions: extension.contributions,
    },
  };
}

function buildExtensionStudioDraft(entity: OperatorEntity): string {
  const filePath = typeof entity.details.file_path === "string" ? entity.details.file_path : "";
  const requiresTools = Array.isArray(entity.details.requires_tools)
    ? entity.details.requires_tools.filter((item): item is string => typeof item === "string")
    : [];
  const missingTools = Array.isArray(entity.details.missing_tools)
    ? entity.details.missing_tools.filter((item): item is string => typeof item === "string")
    : [];
  const missingSkills = Array.isArray(entity.details.missing_skills)
    ? entity.details.missing_skills.filter((item): item is string => typeof item === "string")
    : [];

  if (entity.entityType === "workflow_definition") {
    return [
      filePath
        ? `Open "${filePath}" and update workflow "${entity.name}".`
        : `Update workflow "${entity.name}".`,
      "Validation focus: keep requires.tools aligned with every step tool, preserve input schema clarity, and keep execution boundaries/risk truthful.",
      missingTools.length || missingSkills.length
        ? `Current blockers: ${[
          missingTools.length ? `tools ${missingTools.join(", ")}` : "",
          missingSkills.length ? `skills ${missingSkills.join(", ")}` : "",
        ].filter(Boolean).join(" · ")}.`
        : "Current runtime blockers: none detected from the latest workspace snapshot.",
      "After editing, reload workflows and review diagnostics plus capability preflight before rerunning.",
    ].join("\n");
  }

  if (entity.entityType === "skill") {
    return [
      filePath
        ? `Open "${filePath}" and update skill "${entity.name}".`
        : `Update skill "${entity.name}".`,
      requiresTools.length
        ? `Keep tool assumptions aligned with runtime availability: ${requiresTools.join(", ")}.`
        : "This skill has no declared tool requirements today.",
      "After editing, reload skills and verify the skill remains available in the live capability surface.",
    ].join("\n");
  }

  const url = typeof entity.details.url === "string" ? entity.details.url : "";
  const description = typeof entity.details.description === "string" ? entity.details.description : "";
  return [
    `Review MCP server "${entity.name}" configuration.`,
    url ? `Current URL: ${url}` : "No URL configured.",
    description ? `Description: ${description}` : "No description configured.",
    "Validate auth hints, headers, and connectivity before enabling or rerunning dependent workflows.",
  ].join("\n");
}

function workflowResumeDetails(workflow: WorkflowRunRecord): string[] {
  const details: string[] = [];
  if (workflow.runIdentity) details.push(`run ${shortIdentifier(workflow.runIdentity)}`);
  if (workflow.branchKind) details.push(workflow.branchKind.replace(/_/g, " "));
  if (typeof workflow.branchDepth === "number" && workflow.branchDepth > 0) details.push(`depth ${workflow.branchDepth}`);
  if (workflow.parentRunIdentity) details.push(`parent ${shortIdentifier(workflow.parentRunIdentity)}`);
  if (
    workflow.rootRunIdentity
    && workflow.rootRunIdentity !== workflow.runIdentity
  ) {
    details.push(`root ${shortIdentifier(workflow.rootRunIdentity)}`);
  }
  if (workflow.runFingerprint) details.push(`fingerprint ${shortIdentifier(workflow.runFingerprint)}`);
  if (workflow.resumeCheckpointLabel) details.push(`checkpoint ${workflow.resumeCheckpointLabel}`);
  if (workflow.resumeFromStep) details.push(`resume ${workflow.resumeFromStep}`);
  if (workflow.checkpointContextAvailable === true) details.push("checkpoint state ready");
  if (workflow.checkpointContextAvailable === false && workflow.continuedErrorSteps.length > 0) {
    details.push("checkpoint state missing");
  }
  if (workflow.threadContinueMessage) details.push("thread continue ready");
  if (workflow.approvalRecoveryMessage) details.push("approval recovery ready");
  return details;
}

function workflowStepSummary(step: WorkflowStepRecord): string {
  const parts = [
    `${step.id} ${step.status}`,
    step.durationMs ? `${step.durationMs}ms` : null,
    step.resultSummary,
    step.errorSummary,
    step.recoveryHint,
  ].filter((part): part is string => typeof part === "string" && part.trim().length > 0);
  return parts.join(" · ");
}

function workflowUpdatedAtMs(workflow: WorkflowRunRecord): number {
  const timestamp = Date.parse(workflow.updatedAt);
  return Number.isNaN(timestamp) ? 0 : timestamp;
}

function workflowFamilyRootIdentity(workflow: WorkflowRunRecord): string {
  return workflow.rootRunIdentity ?? workflow.runIdentity ?? workflow.id;
}

function compareWorkflowRunsNewestFirst(a: WorkflowRunRecord, b: WorkflowRunRecord): number {
  return workflowUpdatedAtMs(b) - workflowUpdatedAtMs(a);
}

interface WorkflowFamilyArtifactOutput {
  key: string;
  filePath: string;
  createdAt: string;
  sourceWorkflow: WorkflowRunRecord;
  sourceLabel: string;
}

function workflowCheckpointActions(
  workflow: WorkflowRunRecord,
): Array<{ stepId: string; draft: string; label: string }> {
  if (!Array.isArray(workflow.checkpointCandidates)) {
    return [];
  }
  return workflow.checkpointCandidates.reduce<Array<{ stepId: string; draft: string; label: string }>>((actions, candidate) => {
    if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) return actions;
    const record = candidate as Record<string, unknown>;
    const stepId = typeof record.step_id === "string" ? record.step_id : "";
    const draft = typeof record.resume_draft === "string" ? record.resume_draft : "";
    if (!stepId || !draft) return actions;
    const kind = typeof record.kind === "string" ? record.kind : "branch_from_checkpoint";
    actions.push({
      stepId,
      draft,
      label: kind === "retry_failed_step" ? `Retry ${stepId}` : `Branch ${stepId}`,
    });
    return actions;
  }, []);
}

const RUNBOOK_MACROS_KEY = "seraph_operator_runbook_macros";

function readRunbookMacros(): RunbookInfo[] {
  if (typeof localStorage === "undefined" || typeof localStorage.getItem !== "function") {
    return [];
  }
  try {
    const raw = localStorage.getItem(RUNBOOK_MACROS_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((item): item is RunbookInfo => {
      return !!item && typeof item === "object"
        && typeof (item as RunbookInfo).id === "string"
        && typeof (item as RunbookInfo).label === "string"
        && typeof (item as RunbookInfo).command === "string";
    });
  } catch {
    return [];
  }
}

function writeRunbookMacros(items: RunbookInfo[]) {
  if (typeof localStorage === "undefined" || typeof localStorage.setItem !== "function") {
    return;
  }
  try {
    localStorage.setItem(RUNBOOK_MACROS_KEY, JSON.stringify(items));
  } catch {
    // ignore storage failures
  }
}

function normalizeWorkflowRun(value: Record<string, unknown>): WorkflowRunRecord {
  const normalizedStepRecords: WorkflowStepRecord[] | undefined = Array.isArray(value.step_records)
    ? value.step_records.reduce<WorkflowStepRecord[]>((records, entry) => {
      if (!entry || typeof entry !== "object" || Array.isArray(entry)) return records;
      const record = entry as Record<string, unknown>;
      if (
        typeof record.id !== "string"
        || typeof record.index !== "number"
        || typeof record.tool !== "string"
        || typeof record.status !== "string"
      ) {
        return records;
      }
      records.push({
        id: record.id,
        index: record.index,
        tool: record.tool,
        status: record.status,
        argumentKeys: Array.isArray(record.argument_keys)
          ? record.argument_keys.filter((item): item is string => typeof item === "string")
          : [],
        artifactPaths: Array.isArray(record.artifact_paths)
          ? record.artifact_paths.filter((item): item is string => typeof item === "string")
          : [],
        resultSummary: typeof record.result_summary === "string" ? record.result_summary : null,
        errorKind: typeof record.error_kind === "string" ? record.error_kind : null,
        errorSummary: typeof record.error_summary === "string" ? record.error_summary : null,
        startedAt: typeof record.started_at === "string" ? record.started_at : null,
        completedAt: typeof record.completed_at === "string" ? record.completed_at : null,
        durationMs: typeof record.duration_ms === "number" ? record.duration_ms : null,
        recoveryActions: Array.isArray(record.recovery_actions)
          ? record.recovery_actions.filter(
              (item): item is Record<string, unknown> => !!item && typeof item === "object" && !Array.isArray(item),
            )
          : undefined,
        recoveryHint: typeof record.recovery_hint === "string" ? record.recovery_hint : null,
        isRecoverable: typeof record.is_recoverable === "boolean" ? record.is_recoverable : undefined,
      });
      return records;
    }, [])
    : undefined;

  const normalizedTimeline: WorkflowTimelineEntry[] | undefined = Array.isArray(value.timeline)
    ? value.timeline.reduce<WorkflowTimelineEntry[]>((entries, entry) => {
      if (!entry || typeof entry !== "object" || Array.isArray(entry)) return entries;
      const record = entry as Record<string, unknown>;
      if (typeof record.kind !== "string" || typeof record.at !== "string" || typeof record.summary !== "string") {
        return entries;
      }
      entries.push({
        kind: record.kind,
        at: record.at,
        summary: record.summary,
        stepId: typeof record.step_id === "string" ? record.step_id : null,
        stepTool: typeof record.step_tool === "string" ? record.step_tool : null,
        resultSummary: typeof record.result_summary === "string" ? record.result_summary : null,
        errorKind: typeof record.error_kind === "string" ? record.error_kind : null,
        errorSummary: typeof record.error_summary === "string" ? record.error_summary : null,
        durationMs: typeof record.duration_ms === "number" ? record.duration_ms : null,
      });
      return entries;
    }, [])
    : undefined;

  return {
    id: String(value.id ?? ""),
    toolName: String(value.tool_name ?? ""),
    workflowName: String(value.workflow_name ?? value.tool_name ?? ""),
    sessionId: typeof value.session_id === "string" ? value.session_id : null,
    status: (value.status as WorkflowRunRecord["status"]) ?? "running",
    startedAt: String(value.started_at ?? value.updated_at ?? ""),
    updatedAt: String(value.updated_at ?? value.started_at ?? ""),
    summary: String(value.summary ?? ""),
    stepTools: Array.isArray(value.step_tools) ? value.step_tools.filter((item): item is string => typeof item === "string") : [],
    stepRecords: normalizedStepRecords,
    artifactPaths: Array.isArray(value.artifact_paths) ? value.artifact_paths.filter((item): item is string => typeof item === "string") : [],
    continuedErrorSteps: Array.isArray(value.continued_error_steps)
      ? value.continued_error_steps.filter((item): item is string => typeof item === "string")
      : [],
    arguments: value.arguments && typeof value.arguments === "object" && !Array.isArray(value.arguments)
      ? (value.arguments as Record<string, unknown>)
      : undefined,
    artifacts: [],
    riskLevel: typeof value.risk_level === "string" ? value.risk_level : undefined,
    executionBoundaries: Array.isArray(value.execution_boundaries)
      ? value.execution_boundaries.filter((item): item is string => typeof item === "string")
      : undefined,
    acceptsSecretRefs: typeof value.accepts_secret_refs === "boolean" ? value.accepts_secret_refs : undefined,
    pendingApprovalCount: typeof value.pending_approval_count === "number" ? value.pending_approval_count : undefined,
    pendingApprovalIds: Array.isArray(value.pending_approval_ids)
      ? value.pending_approval_ids.filter((item): item is string => typeof item === "string")
      : undefined,
    pendingApprovals: Array.isArray(value.pending_approvals)
      ? value.pending_approvals.reduce<NonNullable<WorkflowRunRecord["pendingApprovals"]>>((entries, entry) => {
          if (!entry || typeof entry !== "object" || Array.isArray(entry)) return entries;
          const record = entry as Record<string, unknown>;
          if (
            typeof record.id !== "string"
            || typeof record.summary !== "string"
            || typeof record.created_at !== "string"
          ) {
            return entries;
          }
          entries.push({
            id: record.id,
            summary: record.summary,
            riskLevel: typeof record.risk_level === "string" ? record.risk_level : undefined,
            createdAt: record.created_at,
            threadId: typeof record.thread_id === "string" ? record.thread_id : null,
            threadLabel: typeof record.thread_label === "string" ? record.thread_label : null,
            resumeMessage: typeof record.resume_message === "string" ? record.resume_message : null,
          });
          return entries;
        }, [])
      : undefined,
    threadId: typeof value.thread_id === "string" ? value.thread_id : null,
    threadLabel: typeof value.thread_label === "string" ? value.thread_label : null,
    threadSource: typeof value.thread_source === "string" ? value.thread_source : null,
    replayAllowed: typeof value.replay_allowed === "boolean" ? value.replay_allowed : undefined,
    replayBlockReason: typeof value.replay_block_reason === "string" ? value.replay_block_reason : null,
    replayDraft: typeof value.replay_draft === "string" ? value.replay_draft : null,
    replayInputs:
      value.replay_inputs && typeof value.replay_inputs === "object" && !Array.isArray(value.replay_inputs)
        ? (value.replay_inputs as Record<string, unknown>)
        : undefined,
    parameterSchema:
      value.parameter_schema && typeof value.parameter_schema === "object" && !Array.isArray(value.parameter_schema)
        ? (value.parameter_schema as Record<string, unknown>)
        : undefined,
    replayRecommendedActions: Array.isArray(value.replay_recommended_actions)
      ? value.replay_recommended_actions.filter(
          (item): item is Record<string, unknown> => !!item && typeof item === "object" && !Array.isArray(item),
        )
      : undefined,
    availability: typeof value.availability === "string" ? value.availability : null,
    resumeFromStep: typeof value.resume_from_step === "string" ? value.resume_from_step : null,
    resumeCheckpointLabel:
      typeof value.resume_checkpoint_label === "string" ? value.resume_checkpoint_label : null,
    threadContinueMessage:
      typeof value.thread_continue_message === "string" ? value.thread_continue_message : null,
    approvalRecoveryMessage:
      typeof value.approval_recovery_message === "string"
        ? value.approval_recovery_message
        : null,
    runFingerprint: typeof value.run_fingerprint === "string" ? value.run_fingerprint : null,
    runIdentity: typeof value.run_identity === "string" ? value.run_identity : null,
    parentRunIdentity:
      typeof value.parent_run_identity === "string" ? value.parent_run_identity : null,
    rootRunIdentity:
      typeof value.root_run_identity === "string" ? value.root_run_identity : null,
    branchKind: typeof value.branch_kind === "string" ? value.branch_kind : null,
    branchDepth: typeof value.branch_depth === "number" ? value.branch_depth : null,
    isBranchRun: typeof value.is_branch_run === "boolean" ? value.is_branch_run : undefined,
    retryFromStepDraft:
      typeof value.retry_from_step_draft === "string" ? value.retry_from_step_draft : null,
    checkpointContextAvailable:
      typeof value.checkpoint_context_available === "boolean"
        ? value.checkpoint_context_available
        : undefined,
    checkpointCandidates: Array.isArray(value.checkpoint_candidates)
      ? value.checkpoint_candidates.filter(
          (item): item is Record<string, unknown> => !!item && typeof item === "object" && !Array.isArray(item),
        )
      : undefined,
    resumePlan:
      value.resume_plan && typeof value.resume_plan === "object" && !Array.isArray(value.resume_plan)
        ? (value.resume_plan as Record<string, unknown>)
        : null,
    timeline: normalizedTimeline,
  };
}

function collectGoalTitles(goals: GoalInfo[], limit: number): string[] {
  const titles: string[] = [];

  const visit = (items: GoalInfo[]) => {
    for (const item of items) {
      if (titles.length >= limit) return;
      titles.push(item.title);
      if (item.children?.length) visit(item.children);
    }
  };

  visit(goals);
  return titles;
}

function buildWorkflowReplayDraft(workflow: WorkflowRunRecord): string {
  if (workflow.retryFromStepDraft) {
    return workflow.retryFromStepDraft;
  }
  if (workflow.replayDraft) {
    return workflow.replayDraft;
  }
  const inputs = workflow.arguments
    ? Object.entries(workflow.arguments).map(([name, value]) => `${name}=${JSON.stringify(value)}`)
    : [];
  const base = inputs.length
    ? `Run workflow "${workflow.workflowName}" with ${inputs.join(", ")}.`
    : `Run workflow "${workflow.workflowName}".`;
  const warnings: string[] = [];
  if (workflow.executionBoundaries?.length) {
    warnings.push(`Execution boundaries: ${workflow.executionBoundaries.join(", ")}.`);
  }
  if (workflow.riskLevel) {
    warnings.push(`Risk level: ${workflow.riskLevel}.`);
  }
  if (workflow.pendingApprovalCount) {
    warnings.push(`This workflow currently has ${workflow.pendingApprovalCount} pending approval(s).`);
  }
  if (workflow.acceptsSecretRefs) {
    warnings.push("This workflow can cross secret-reference injection boundaries.");
  }
  if (workflow.checkpointContextAvailable === false && workflow.continuedErrorSteps.length > 0) {
    warnings.push("Checkpoint state was not persisted for the failed step, so only a full rerun is available.");
  }
  return [base, ...warnings].join("\n");
}

function replayBlockCopy(reason: string | null | undefined): string {
  switch (reason) {
    case "pending_approval":
      return "pending approval";
    case "workflow_unavailable":
      return "workflow unavailable";
    case "workflow_disabled":
      return "workflow disabled";
    case "secret_ref_surface":
      return "secret-ref surface";
    case "secret_bearing_boundary":
      return "secret-bearing boundary";
    case "high_risk_requires_manual_reentry":
      return "high-risk replay";
    default:
      return "manual review required";
  }
}

function activityStatusLabel(value: ActivityLedgerEntry): string {
  return `${value.kind.replace(/_/g, " ")} · ${value.status.replace(/_/g, " ")}`;
}

function activityCategoryLabel(value: ActivityLedgerFilter): string {
  return value === "all" ? "All" : value.replace(/_/g, " ");
}

function formatUsd(value: number | null | undefined): string | null {
  if (typeof value !== "number" || Number.isNaN(value) || value <= 0) return null;
  if (value >= 1) return `$${value.toFixed(2)}`;
  if (value >= 0.01) return `$${value.toFixed(3)}`;
  return `$${value.toFixed(4)}`;
}

function _modelLabelForRow(model: string): string {
  return model.replace(/^openrouter\//, "").replace(/^anthropic\//, "").replace(/[_/-]+/g, " ");
}

function formatDuration(valueMs: number | null | undefined): string | null {
  if (typeof valueMs !== "number" || Number.isNaN(valueMs) || valueMs <= 0) return null;
  if (valueMs >= 60_000) return `${(valueMs / 60_000).toFixed(1)}m`;
  if (valueMs >= 1000) return `${(valueMs / 1000).toFixed(1)}s`;
  return `${Math.round(valueMs)}ms`;
}

function activityEmoji(value: ActivityLedgerEntry): string {
  if (value.kind === "llm_call") return "🤖";
  if (value.kind === "workflow_run") return "⚙️";
  if (value.kind === "approval") return "⏳";
  if (value.kind === "extension") return "🧩";
  if (value.kind === "notification" || value.kind === "queued_insight" || value.kind === "intervention") return "📣";
  if (value.kind === "routing") return "🧭";
  if (value.kind === "tool_call" || value.kind === "tool_result" || value.kind === "tool_failed") return "🔧";
  if (value.kind === "agent_run") return "✨";
  if (value.kind === "scheduler_job") return "🗓️";
  if (value.kind === "background_task") {
    const title = value.title.toLowerCase();
    if (title.includes("memory")) return "🧠";
    if (title.includes("skill")) return "📚";
    return "💤";
  }
  if (value.kind === "delivery") return "📨";
  if (value.kind === "integration") return "🔌";
  if (value.status === "failed" || value.status === "timed_out") return "⛔";
  return "•";
}

function activityGroupKey(value: ActivityLedgerEntry): string {
  if (value.group_key) return value.group_key;
  const requestId = typeof value.metadata?.request_id === "string" ? value.metadata.request_id : null;
  if (requestId) return `request:${requestId}`;
  if (value.kind === "workflow_run") return `workflow:${value.id}`;
  if (value.kind === "approval") return `approval:${value.id}`;
  if (value.kind === "notification" || value.kind === "queued_insight" || value.kind === "intervention") {
    return `guardian:${value.id}`;
  }
  return `${value.kind}:${value.thread_id ?? "ambient"}:${value.updated_at}`;
}

function activityLeadPriority(value: ActivityLedgerEntry): number {
  switch (value.kind) {
    case "approval":
      return 0;
    case "workflow_run":
      return 1;
    case "extension":
      return 2;
    case "intervention":
    case "notification":
    case "queued_insight":
      return 3;
    case "agent_run":
      return 4;
    case "llm_call":
      return 5;
    case "routing":
      return 6;
    case "tool_call":
    case "tool_result":
    case "tool_failed":
      return 7;
    default:
      return 8;
  }
}

function chooseActivityLead(items: ActivityLedgerEntry[]): ActivityLedgerEntry {
  return [...items].sort((left, right) => {
    const priorityDelta = activityLeadPriority(left) - activityLeadPriority(right);
    if (priorityDelta !== 0) return priorityDelta;
    return new Date(right.updated_at).getTime() - new Date(left.updated_at).getTime();
  })[0];
}

function activityRowMeta(value: ActivityLedgerEntry): string {
  if (value.kind === "extension") {
    const parts: string[] = [activityStatusLabel(value)];
    if (typeof value.metadata?.action === "string" && value.metadata.action.trim()) {
      parts.push(value.metadata.action.replace(/_/g, " "));
    }
    if (typeof value.metadata?.location === "string" && value.metadata.location.trim()) {
      parts.push(value.metadata.location);
    }
    if (typeof value.metadata?.kind === "string" && value.metadata.kind.trim()) {
      parts.push(value.metadata.kind);
    }
    if (typeof value.metadata?.version === "string" && value.metadata.version.trim()) {
      parts.push(value.metadata.version);
    }
    return parts.join(" · ");
  }
  const parts: string[] = [activityStatusLabel(value)];
  if (value.thread_label) parts.push(value.thread_label);
  if (typeof value.metadata?.capability_family === "string" && value.metadata.capability_family.trim()) {
    parts.push(activitySpendBucketLabel(String(value.metadata.capability_family)));
  }
  if (typeof value.metadata?.runtime_path === "string" && value.metadata.runtime_path.trim()) {
    parts.push(String(value.metadata.runtime_path));
  }
  if (value.model) parts.push(_modelLabelForRow(value.model));
  if (value.provider) parts.push(value.provider);
  const duration = formatDuration(value.duration_ms);
  if (duration) parts.push(duration);
  const usd = formatUsd(value.cost_usd);
  if (usd) parts.push(usd);
  if (typeof value.prompt_tokens === "number" && typeof value.completion_tokens === "number") {
    const total = value.prompt_tokens + value.completion_tokens;
    if (total > 0) parts.push(`${total} tok`);
  }
  return parts.join(" · ");
}

function activityRoutingSummary(value: ActivityLedgerEntry): string {
  if (value.kind !== "routing") return value.summary;
  return [
    `model ${String(value.metadata?.selected_model ?? value.model ?? "unknown")}`,
    typeof value.metadata?.selected_source === "string" ? String(value.metadata.selected_source) : null,
    typeof value.metadata?.reroute_cause === "string" ? String(value.metadata.reroute_cause) : null,
    value.metadata?.max_budget_class ? `budget ${String(value.metadata.max_budget_class)}` : null,
    value.metadata?.required_task_class ? `task ${String(value.metadata.required_task_class)}` : null,
  ].filter(Boolean).join(" · ");
}

function activityLeadDetail(value: ActivityLedgerEntry): string | null {
  if (value.kind === "routing") return activityRoutingSummary(value);
  if (value.kind === "llm_call") {
    return [
      typeof value.metadata?.runtime_path === "string" ? `runtime ${String(value.metadata.runtime_path)}` : null,
      typeof value.metadata?.selected_source === "string" ? `target ${String(value.metadata.selected_source)}` : null,
      typeof value.metadata?.max_budget_class === "string" ? `budget ${String(value.metadata.max_budget_class)}` : null,
      typeof value.metadata?.required_task_class === "string" ? `task ${String(value.metadata.required_task_class)}` : null,
    ].filter(Boolean).join(" · ") || null;
  }
  if (value.kind === "extension" && typeof value.metadata?.error === "string" && value.metadata.error.trim()) {
    return value.metadata.error;
  }
  return null;
}

function activityChildMeta(value: ActivityLedgerEntry): string {
  if (value.kind === "llm_call") {
    const baseMeta = activityRowMeta(value);
    const llmMeta = [
      Array.isArray(value.metadata?.required_policy_intents) && value.metadata.required_policy_intents.length
        ? `intents ${value.metadata.required_policy_intents.join(", ")}`
        : null,
      typeof value.metadata?.max_cost_tier === "string" ? `cost ${String(value.metadata.max_cost_tier)}` : null,
      typeof value.metadata?.max_latency_tier === "string" ? `latency ${String(value.metadata.max_latency_tier)}` : null,
    ].filter(Boolean).join(" · ");
    return [baseMeta, llmMeta].filter(Boolean).join(" · ");
  }
  if (value.kind !== "routing") return activityRowMeta(value);
  return [
    Array.isArray(value.metadata?.required_policy_intents) && value.metadata.required_policy_intents.length
      ? `intents ${value.metadata.required_policy_intents.join(", ")}`
      : null,
    value.metadata?.max_cost_tier ? `cost ${String(value.metadata.max_cost_tier)}` : null,
    value.metadata?.max_latency_tier ? `latency ${String(value.metadata.max_latency_tier)}` : null,
    typeof value.metadata?.rejected_target_count === "number"
      && value.metadata.rejected_target_count > 0
      ? `rejected ${String(value.metadata.rejected_target_count)}`
      : null,
  ].filter(Boolean).join(" · ");
}

function activityLeadMeta(value: ActivityLedgerEntry): string {
  if (value.kind === "routing") return activityChildMeta(value);
  return activityRowMeta(value);
}

function activityInspectorEntity(value: ActivityLedgerEntry): OperatorEntity {
  return {
    entityType: "activity_item",
    name: value.title,
    meta: activityStatusLabel(value),
    summary: value.summary,
    details: {
      category: value.category,
      source: value.source,
      thread: value.thread_label ?? value.thread_id ?? "ambient",
      status: value.status,
      continue_message: value.continue_message ?? "",
      replay_allowed: value.replay_allowed ?? false,
      replay_block_reason: value.replay_block_reason ?? "none",
      model: value.model ?? "n/a",
      provider: value.provider ?? "n/a",
      prompt_tokens: value.prompt_tokens ?? 0,
      completion_tokens: value.completion_tokens ?? 0,
      cost_usd: value.cost_usd ?? 0,
      duration_ms: value.duration_ms ?? 0,
      metadata: value.metadata ?? {},
      recommended_actions: value.recommended_actions ?? [],
    },
  };
}

function workflowStepChildren(value: ActivityLedgerEntry): ActivityLedgerGroupChild[] {
  const stepRecords = Array.isArray(value.metadata?.step_records)
    ? value.metadata.step_records as Array<Record<string, unknown>>
    : [];
  return stepRecords.flatMap((record, index) => {
    const tool = typeof record.tool === "string" ? record.tool : "step";
    const status = typeof record.status === "string" ? record.status : "recorded";
    const duration = typeof record.duration_ms === "number" ? formatDuration(record.duration_ms) : null;
    const summary = typeof record.summary === "string"
      ? record.summary
      : typeof record.result_summary === "string"
        ? record.result_summary
        : typeof record.error_summary === "string"
          ? record.error_summary
          : "No step summary recorded.";
    return [{
      key: `${value.id}:step:${typeof record.id === "string" ? record.id : index}`,
      icon: "🔧",
      label: tool.replace(/_/g, " "),
      summary,
      meta: [status.replace(/_/g, " "), duration].filter(Boolean).join(" · "),
      status,
    }];
  });
}

function activityGroupFooter(items: ActivityLedgerEntry[], lead: ActivityLedgerEntry): string | null {
  const toolCount = items.filter((item) => ["tool_call", "tool_result", "tool_failed"].includes(item.kind)).length;
  const llmCount = items.filter((item) => item.kind === "llm_call").length;
  const approvalCount = items.filter((item) => item.kind === "approval").length;
  const failureCount = items.filter((item) => item.status === "failed" || item.status === "timed_out").length;
  const workflowStepCount = Array.isArray(lead.metadata?.step_records) ? lead.metadata.step_records.length : 0;
  const totalDurationMs = items.reduce((sum, item) => sum + (item.duration_ms ?? 0), 0)
    + (Array.isArray(lead.metadata?.step_records)
      ? (lead.metadata.step_records as Array<Record<string, unknown>>).reduce(
        (sum, record) => sum + (typeof record.duration_ms === "number" ? record.duration_ms : 0),
        0,
      )
      : 0);

  const parts: string[] = [];
  if (workflowStepCount > 0) {
    parts.push(`${workflowStepCount} step${workflowStepCount === 1 ? "" : "s"} recorded`);
  } else if (toolCount > 0) {
    parts.push(`${toolCount} tool${toolCount === 1 ? "" : "s"}`);
  }
  if (llmCount > 0) parts.push(`${llmCount} model call${llmCount === 1 ? "" : "s"}`);
  if (approvalCount > 0) parts.push(`${approvalCount} pending`);
  if (parts.length === 0) return null;
  const duration = formatDuration(totalDurationMs);
  const prefix = failureCount > 0 ? "⛔" : "⚡";
  return `${prefix} ${parts.join(" · ")}${duration ? ` in ${duration} total` : ""}`;
}

function buildActivityLedgerGroups(items: ActivityLedgerEntry[]): ActivityLedgerGroup[] {
  const groups = new Map<string, ActivityLedgerEntry[]>();
  items.forEach((item) => {
    const key = activityGroupKey(item);
    const existing = groups.get(key);
    if (existing) {
      existing.push(item);
    } else {
      groups.set(key, [item]);
    }
  });

  return [...groups.entries()]
    .map(([key, groupItems]) => {
      const lead = chooseActivityLead(groupItems);
      const groupChildren = groupItems
        .filter((item) => item.id !== lead.id)
        .sort((left, right) => new Date(left.created_at).getTime() - new Date(right.created_at).getTime())
        .map((item) => ({
          key: item.id,
          icon: activityEmoji(item),
          label: item.title,
          summary: activityRoutingSummary(item),
          meta: activityChildMeta(item),
          status: item.status,
          item,
        }));
      const stepChildren = lead.kind === "workflow_run" ? workflowStepChildren(lead) : [];
      const children = [...stepChildren, ...groupChildren];
      return {
        key,
        lead,
        icon: activityEmoji(lead),
        title: lead.title,
        summary: lead.summary,
        detail: activityLeadDetail(lead),
        meta: activityLeadMeta(lead),
        footer: activityGroupFooter(groupItems, lead),
        updatedAt: groupItems.reduce(
          (latest, item) => (new Date(item.updated_at).getTime() > new Date(latest).getTime() ? item.updated_at : latest),
          lead.updated_at,
        ),
        children,
      };
    })
    .sort((left, right) => new Date(right.updatedAt).getTime() - new Date(left.updatedAt).getTime());
}

function activityEntryHasRowAction(value: ActivityLedgerEntry): boolean {
  return Boolean(
    value.continue_message
      || (value.replay_draft && value.replay_allowed !== false)
      || (value.recommended_actions && value.recommended_actions.length > 0),
  );
}

function activityGroupActionTarget(group: ActivityLedgerGroup): ActivityLedgerEntry {
  if (activityEntryHasRowAction(group.lead)) return group.lead;
  for (const child of group.children) {
    if (child.item && activityEntryHasRowAction(child.item)) return child.item;
  }
  return group.lead;
}

function canOpenLedgerThread(
  threadId: string | null | undefined,
  activeSessionId: string | null,
  knownSessionIds: Set<string>,
): boolean {
  if (!threadId) return false;
  if (activeSessionId === threadId) return false;
  return knownSessionIds.has(threadId);
}

function deriveActivitySummary(items: ActivityLedgerEntry[]): ActivityLedgerSummary {
  const llmItems = items.filter((item) => item.kind === "llm_call");
  const normalizedBucketKey = (value: unknown, fallback: string) => {
    if (typeof value !== "string") return fallback;
    const normalized = value.trim();
    return normalized || fallback;
  };
  const bucketBy = (field: "runtime_path" | "capability_family", fallback: string) => {
    const buckets = new Map<string, { key: string; calls: number; cost_usd: number; input_tokens: number; output_tokens: number }>();
    llmItems.forEach((item) => {
      const key = normalizedBucketKey(item.metadata?.[field], fallback);
      const current = buckets.get(key) ?? { key, calls: 0, cost_usd: 0, input_tokens: 0, output_tokens: 0 };
      current.calls += 1;
      current.cost_usd += item.cost_usd ?? 0;
      current.input_tokens += item.prompt_tokens ?? 0;
      current.output_tokens += item.completion_tokens ?? 0;
      buckets.set(key, current);
    });
    return Array.from(buckets.values()).sort((left, right) => right.cost_usd - left.cost_usd || right.calls - left.calls);
  };
  return {
    window_hours: 24,
    started_at: new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString(),
    total_items: items.length,
    visible_items: items.length,
    is_partial: false,
    partial_sources: [],
    pending_approvals: items.filter((item) => item.kind === "approval").length,
    failure_count: items.filter((item) => ["failed", "timed_out"].includes(item.status)).length,
    llm_call_count: llmItems.length,
    llm_cost_usd: llmItems.reduce((sum, item) => sum + (item.cost_usd ?? 0), 0),
    input_tokens: llmItems.reduce((sum, item) => sum + (item.prompt_tokens ?? 0), 0),
    output_tokens: llmItems.reduce((sum, item) => sum + (item.completion_tokens ?? 0), 0),
    user_triggered_llm_calls: llmItems.filter((item) => ["rest_chat", "websocket_chat"].includes(item.source)).length,
    autonomous_llm_calls: llmItems.filter((item) => !["rest_chat", "websocket_chat"].includes(item.source)).length,
    llm_cost_by_runtime_path: bucketBy("runtime_path", "unattributed"),
    llm_cost_by_capability_family: bucketBy("capability_family", "unattributed"),
    categories: {
      llm: items.filter((item) => item.category === "llm").length,
      workflow: items.filter((item) => item.category === "workflow").length,
      approval: items.filter((item) => item.category === "approval").length,
      guardian: items.filter((item) => item.category === "guardian").length,
      agent: items.filter((item) => item.category === "agent").length,
      system: items.filter((item) => item.category === "system").length,
    },
  };
}

function normalizeActivityLedgerEntry(raw: Record<string, unknown>): ActivityLedgerEntry {
  const kind = typeof raw.kind === "string" ? raw.kind : "system";
  const category = (typeof raw.category === "string" ? raw.category : (
    kind === "llm_call" || kind === "routing"
      ? "llm"
      : kind === "workflow_run"
        ? "workflow"
        : kind === "approval"
          ? "approval"
          : ["notification", "queued_insight", "intervention"].includes(kind)
            ? "guardian"
            : ["agent_run", "tool_call", "tool_result", "tool_failed"].includes(kind)
              ? "agent"
              : "system"
  )) as ActivityLedgerCategory;

  return {
    id: typeof raw.id === "string" ? raw.id : crypto.randomUUID(),
    kind,
    category,
    group_key: typeof raw.group_key === "string" ? raw.group_key : null,
    title: typeof raw.title === "string" ? raw.title : kind.replace(/_/g, " "),
    summary: typeof raw.summary === "string" ? raw.summary : "",
    status: typeof raw.status === "string" ? raw.status : "unknown",
    created_at: typeof raw.created_at === "string" ? raw.created_at : new Date().toISOString(),
    updated_at: typeof raw.updated_at === "string" ? raw.updated_at : (typeof raw.created_at === "string" ? raw.created_at : new Date().toISOString()),
    thread_id: typeof raw.thread_id === "string" ? raw.thread_id : null,
    thread_label: typeof raw.thread_label === "string" ? raw.thread_label : null,
    continue_message: typeof raw.continue_message === "string" ? raw.continue_message : null,
    replay_draft: typeof raw.replay_draft === "string" ? raw.replay_draft : null,
    replay_allowed: typeof raw.replay_allowed === "boolean" ? raw.replay_allowed : false,
    replay_block_reason: typeof raw.replay_block_reason === "string" ? raw.replay_block_reason : null,
    recommended_actions: readActionList(raw.recommended_actions),
    source: typeof raw.source === "string" ? raw.source : "activity",
    model: typeof raw.model === "string" ? raw.model : null,
    provider: typeof raw.provider === "string" ? raw.provider : null,
    prompt_tokens: typeof raw.prompt_tokens === "number" ? raw.prompt_tokens : null,
    completion_tokens: typeof raw.completion_tokens === "number" ? raw.completion_tokens : null,
    cost_usd: typeof raw.cost_usd === "number" ? raw.cost_usd : null,
    duration_ms: typeof raw.duration_ms === "number" ? raw.duration_ms : null,
    metadata: (raw.metadata && typeof raw.metadata === "object" && !Array.isArray(raw.metadata))
      ? raw.metadata as Record<string, unknown>
      : {},
  };
}

function supportsArtifactRoundtrip(workflow: WorkflowInfo): boolean {
  return workflowArtifactInputs(workflow).length > 0;
}

const COCKPIT_GLOBAL_HINTS = [
  "Shift+1/2/3 switch layouts",
  "Shift+K or Ctrl+K opens the capability palette",
  "Drag headers to move panes",
  "Reset view repacks the current workspace",
];

const COCKPIT_WINDOW_HINTS = {
  sessions: "Thread control, saved conversations, and continuity markers for the active thread.",
  goals: "Keep current priorities visible here; Seraph tracks them as structured goals for planning and review.",
  outputs: "Recent workspace artifacts produced in the current audit window.",
  approvals: "Pending approvals block workflow and tool execution until you inspect or approve them.",
  guardianState: "The live synthesis Seraph is using for timing, confidence, and next actions.",
  operatorTimeline: "Browse what Seraph did, why it did it, and what spent budget across user, guardian, workflow, and system activity.",
  interventions: "Recent proactive nudges, delivery outcomes, and feedback signal.",
  workflowTimeline: "Inspect runs, branch from failures, and resume repaired steps.",
  audit: "Durable tool, memory, workflow, and integration events for the current window.",
  trace: "In-flight routing, tool, and error activity while work is happening.",
  inspector: "Select a run, approval, intervention, or event to inspect details and recovery actions.",
  presence: "Live guardian state mirror.",
  conversation: "Latest replies, pending drafts, and quick thread context for the current session.",
  desktopShell: "Native continuity, queued notifications, and browser-closed follow-up state.",
  operatorTerminal: "Run packs, workflows, macros, and repair actions from one dense control surface.",
} as const;

const ACTIVITY_LEDGER_FILTERS: ActivityLedgerFilter[] = [
  "all",
  "llm",
  "workflow",
  "approval",
  "guardian",
  "agent",
  "system",
];

function CockpitWorkspaceWindow({
  panelId,
  title,
  meta,
  hint,
  showHint,
  minWidth,
  minHeight,
  onClose,
  children,
}: {
  panelId: CockpitPaneId;
  title: string;
  meta: string;
  hint?: string | null;
  showHint?: boolean;
  minWidth?: number;
  minHeight?: number;
  onClose?: () => void;
  children: ReactNode | ((state: { isFront: boolean }) => ReactNode);
}) {
  const resolvedMinWidth = PANEL_MIN_SIZES[panelId]?.width ?? minWidth ?? 240;
  const resolvedMinHeight = PANEL_MIN_SIZES[panelId]?.height ?? minHeight ?? 160;
  const { panelRef, dragHandleProps, resizeHandleProps, style, isFront, bringToFront } = useDragResize(panelId, {
    minWidth: resolvedMinWidth,
    minHeight: resolvedMinHeight,
  });

  return (
    <section
      ref={panelRef}
      className={`cockpit-window ${isFront ? "cockpit-window--active" : ""}`}
      style={style}
      onPointerDown={bringToFront}
    >
      <ResizeHandles resizeHandleProps={resizeHandleProps} />
      <div className="cockpit-window-header" {...dragHandleProps}>
        <div className="cockpit-window-header-main">
          <div className="cockpit-window-title">{title}</div>
          <div className="cockpit-window-meta">{meta}</div>
        </div>
        <div className="cockpit-window-controls">
          {onClose ? (
            <button
              type="button"
              className="cockpit-window-control cockpit-window-control--close"
              title={`Hide ${title}`}
              aria-label={`Hide ${title}`}
              onPointerDown={(event) => event.stopPropagation()}
              onClick={(event) => {
                event.stopPropagation();
                onClose();
              }}
            >
              x
            </button>
          ) : null}
        </div>
      </div>
      {showHint && hint ? <div className="cockpit-window-hint">{hint}</div> : null}
      <div className="cockpit-window-body">
        {typeof children === "function" ? children({ isFront }) : children}
      </div>
    </section>
  );
}

export function CockpitView({ onSend, onSkipOnboarding }: CockpitViewProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [composer, setComposer] = useState("");
  const [runtimeStatus, setRuntimeStatus] = useState<RuntimeStatus | null>(null);
  const [observerState, setObserverState] = useState<ObserverState | null>(null);
  const [auditEvents, setAuditEvents] = useState<CockpitAuditEvent[]>([]);
  const [pendingApprovals, setPendingApprovals] = useState<PendingApproval[]>([]);
  const [feedbackState, setFeedbackState] = useState<Record<string, string>>({});
  const [approvalState, setApprovalState] = useState<Record<string, string>>({});
  const [selectedInspector, setSelectedInspector] = useState<InspectorSelection | null>(null);
  const [daemonPresence, setDaemonPresence] = useState<DaemonPresenceState | null>(null);
  const [desktopNotifications, setDesktopNotifications] = useState<ObserverContinuitySnapshot["notifications"]>([]);
  const [queuedInsights, setQueuedInsights] = useState<ObserverContinuitySnapshot["queued_insights"]>([]);
  const [queuedBundleCount, setQueuedBundleCount] = useState(0);
  const [recentInterventions, setRecentInterventions] = useState<GuardianContinuityIntervention[]>([]);
  const [desktopRouteStatuses, setDesktopRouteStatuses] = useState<ObserverReachRouteStatus[]>([]);
  const [workflows, setWorkflows] = useState<WorkflowInfo[]>([]);
  const [workflowRuns, setWorkflowRuns] = useState<WorkflowRunRecord[]>([]);
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [mcpServers, setMcpServers] = useState<McpServerInfo[]>([]);
  const [tools, setTools] = useState<ToolInfo[]>([]);
  const [starterPacks, setStarterPacks] = useState<StarterPackInfo[]>([]);
  const [catalogItems, setCatalogItems] = useState<CatalogItemInfo[]>([]);
  const [capabilityRecommendations, setCapabilityRecommendations] = useState<CapabilityRecommendation[]>([]);
  const [runbooks, setRunbooks] = useState<RunbookInfo[]>([]);
  const [extensionPackages, setExtensionPackages] = useState<ExtensionPackageInfo[]>([]);
  const [savedRunbooks, setSavedRunbooks] = useState<RunbookInfo[]>(() => readRunbookMacros());
  const [activityLedger, setActivityLedger] = useState<ActivityLedgerEntry[]>([]);
  const [activitySummary, setActivitySummary] = useState<ActivityLedgerSummary | null>(null);
  const [activityFilter, setActivityFilter] = useState<ActivityLedgerFilter>("all");
  const activityLedgerScopeRef = useRef<string>("");
  const [toolPolicyMode, setToolPolicyMode] = useState<ToolPolicyMode | "unknown">("unknown");
  const [mcpPolicyMode, setMcpPolicyMode] = useState<McpPolicyMode | "unknown">("unknown");
  const [approvalMode, setApprovalMode] = useState<ApprovalMode | "unknown">("unknown");
  const [operatorStatus, setOperatorStatus] = useState<string | null>(null);
  const [doctorPlans, setDoctorPlans] = useState<DoctorPlanRecord[]>([]);
  const [studioOpen, setStudioOpen] = useState(false);
  const [studioSelectedId, setStudioSelectedId] = useState<string | null>(null);
  const [studioDraft, setStudioDraft] = useState("");
  const [studioStatus, setStudioStatus] = useState<string | null>(null);
  const [studioPackageStatus, setStudioPackageStatus] = useState<string | null>(null);
  const [studioPackagePreview, setStudioPackagePreview] = useState<ExtensionPathPreview | null>(null);
  const [studioBusy, setStudioBusy] = useState<string | null>(null);
  const [studioPreflight, setStudioPreflight] = useState<CapabilityPreflightResponse | null>(null);
  const [studioWorkflowDiagnostics, setStudioWorkflowDiagnostics] = useState<WorkflowDiagnosticsPayload | null>(null);
  const [studioDraftValidation, setStudioDraftValidation] = useState<Record<string, unknown> | null>(null);
  const [studioMcpTestResult, setStudioMcpTestResult] = useState<Record<string, unknown> | null>(null);
  const [studioMcpUrl, setStudioMcpUrl] = useState("");
  const [studioMcpDescription, setStudioMcpDescription] = useState("");
  const [studioExtensionPath, setStudioExtensionPath] = useState("");
  const [studioScaffoldName, setStudioScaffoldName] = useState("");
  const [studioScaffoldDisplayName, setStudioScaffoldDisplayName] = useState("");
  const [studioExtensionConfigDraft, setStudioExtensionConfigDraft] = useState("{}");
  const [studioExtensionConfigDirty, setStudioExtensionConfigDirty] = useState(false);
  const [pendingLifecycleApprovalId, setPendingLifecycleApprovalId] = useState<string | null>(null);
  const studioExtensionConfigSelectionRef = useRef<string | null>(null);
  const studioSelectionRef = useRef<string | null>(null);
  const studioLoadRequestRef = useRef(0);
  const studioValidationRequestRef = useRef(0);
  const studioSaveRequestRef = useRef(0);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [paletteQuery, setPaletteQuery] = useState("");
  const [windowsMenuOpen, setWindowsMenuOpen] = useState(false);
  const windowsMenuRef = useRef<HTMLDivElement | null>(null);
  const activeLayoutId = useCockpitLayoutStore((s) => s.activeLayoutId);
  const paneVisibility = useCockpitLayoutStore((s) => s.paneVisibility);
  const savedPaneVisibility = useCockpitLayoutStore((s) => s.savedPaneVisibility);
  const setLayout = useCockpitLayoutStore((s) => s.setLayout);
  const setPaneVisible = useCockpitLayoutStore((s) => s.setPaneVisible);
  const togglePaneVisible = useCockpitLayoutStore((s) => s.togglePaneVisible);
  const savePaneVisibility = useCockpitLayoutStore((s) => s.savePaneVisibility);
  const showAllPanes = useCockpitLayoutStore((s) => s.showAllPanes);
  const hideNonCorePanes = useCockpitLayoutStore((s) => s.hideNonCorePanes);
  const applyCockpitLayout = usePanelLayoutStore((s) => s.applyCockpitLayout);
  const saveCockpitLayout = usePanelLayoutStore((s) => s.saveCockpitLayout);
  const resetCockpitLayout = usePanelLayoutStore((s) => s.resetCockpitLayout);
  const bringToFront = usePanelLayoutStore((s) => s.bringToFront);
  const syncCockpitPaneStack = usePanelLayoutStore((s) => s.syncCockpitPaneStack);

  const messages = useChatStore((s) => s.messages);
  const sessions = useChatStore((s) => s.sessions);
  const sessionId = useChatStore((s) => s.sessionId);
  const sessionContinuity = useChatStore((s) => s.sessionContinuity);
  const connectionStatus = useChatStore((s) => s.connectionStatus);
  const isAgentBusy = useChatStore((s) => s.isAgentBusy);
  const ambientState = useChatStore((s) => s.ambientState);
  const ambientTooltip = useChatStore((s) => s.ambientTooltip);
  const cockpitHintsEnabled = useChatStore((s) => s.cockpitHintsEnabled);
  const agentVisual = useChatStore((s) => s.agentVisual);
  const onboardingCompleted = useChatStore((s) => s.onboardingCompleted);
  const restoreLastSession = useChatStore((s) => s.restoreLastSession);
  const switchSession = useChatStore((s) => s.switchSession);
  const newSession = useChatStore((s) => s.newSession);
  const clearSessionContinuity = useChatStore((s) => s.clearSessionContinuity);
  const setQuestPanelOpen = useChatStore((s) => s.setQuestPanelOpen);
  const setSettingsPanelOpen = useChatStore((s) => s.setSettingsPanelOpen);

  const dashboard = useQuestStore((s) => s.dashboard);
  const goalTree = useQuestStore((s) => s.goalTree);
  const loadingGoals = useQuestStore((s) => s.loading);
  const refreshGoals = useQuestStore((s) => s.refresh);

  const handleResetWorkspace = useCallback(() => {
    resetCockpitLayout(activeLayoutId, paneVisibility);
    setWindowsMenuOpen(false);
  }, [activeLayoutId, paneVisibility, resetCockpitLayout]);

  const handleSaveWorkspace = useCallback(() => {
    savePaneVisibility(activeLayoutId);
    saveCockpitLayout(activeLayoutId);
    setOperatorStatus(`Saved ${getCockpitLayout(activeLayoutId).label} workspace`);
    setWindowsMenuOpen(false);
  }, [activeLayoutId, saveCockpitLayout, savePaneVisibility]);

  const handleSelectLayout = useCallback(
    (layoutId: (typeof COCKPIT_LAYOUTS)[keyof typeof COCKPIT_LAYOUTS]["id"]) => {
      const nextVisibility = savedPaneVisibility[layoutId] ?? getDefaultPaneVisibility(layoutId);
      setLayout(layoutId);
      applyCockpitLayout(layoutId, nextVisibility);
      setWindowsMenuOpen(false);
    },
    [applyCockpitLayout, savedPaneVisibility, setLayout],
  );

  const focusPane = useCallback(
    (paneId: CockpitPaneId) => {
      if (!paneVisibility[paneId]) {
        setPaneVisible(paneId, true);
        window.setTimeout(() => bringToFront(paneId), 0);
      } else {
        bringToFront(paneId);
      }
      setWindowsMenuOpen(false);
    },
    [bringToFront, paneVisibility, setPaneVisible],
  );

  const closeWindowPane = useCallback(
    (paneId: CockpitPaneId) => {
      setPaneVisible(paneId, false);
    },
    [setPaneVisible],
  );

  const panesByGroup = useMemo(() => {
    const grouped = new Map<string, typeof COCKPIT_PANES>();
    for (const pane of COCKPIT_PANES) {
      const current = grouped.get(pane.group) ?? [];
      current.push(pane);
      grouped.set(pane.group, current);
    }
    return Array.from(grouped.entries());
  }, []);

  const visiblePaneCount = useMemo(
    () => COCKPIT_PANES.filter((pane) => paneVisibility[pane.id]).length,
    [paneVisibility],
  );

  useEffect(() => {
    void restoreLastSession();
    refreshGoals();
  }, [refreshGoals, restoreLastSession]);

  useEffect(() => {
    writeRunbookMacros(savedRunbooks);
  }, [savedRunbooks]);

  useEffect(() => {
    if (!pendingLifecycleApprovalId) return;
    const approval = pendingApprovals.find((item) => item.id === pendingLifecycleApprovalId);
    if (!approval) return;
    focusPane("approvals_pane");
    setSelectedInspector({ kind: "approval", approval });
    setPendingLifecycleApprovalId(null);
  }, [focusPane, pendingApprovals, pendingLifecycleApprovalId]);

  const refreshCockpit = useCallback(async (isCancelled: () => boolean = () => false) => {
    const fetchJson = async (url: string) => {
      try {
        const response = await fetch(url);
        if (isCancelled() || !response.ok) {
          return { ok: false, payload: null };
        }
        const payload = await response.json().catch(() => null);
        if (isCancelled()) {
          return { ok: false, payload: null };
        }
        return { ok: true, payload };
      } catch {
        return { ok: false, payload: null };
      }
    };

    const [
      runtimeStatusResult,
      observerResult,
      auditResult,
      approvalsResult,
      continuityResult,
      capabilitiesResult,
      extensionsResult,
      activityLedgerResult,
      workflowRunsResult,
      toolModeResult,
      mcpModeResult,
      approvalModeResult,
    ] = await Promise.all([
      fetchJson(`${API_URL}/api/runtime/status`),
      fetchJson(`${API_URL}/api/observer/state`),
      fetchJson(`${API_URL}/api/audit/events?limit=12`),
      fetchJson(`${API_URL}/api/approvals/pending?limit=8`),
      fetchJson(`${API_URL}/api/observer/continuity`),
      fetchJson(`${API_URL}/api/capabilities/overview`),
      fetchJson(`${API_URL}/api/extensions`),
      fetchJson(`${API_URL}/api/activity/ledger?limit=40${sessionId ? `&session_id=${encodeURIComponent(sessionId)}` : ""}`),
      fetchJson(`${API_URL}/api/workflows/runs?limit=8${sessionId ? `&session_id=${encodeURIComponent(sessionId)}` : ""}`),
      fetchJson(`${API_URL}/api/settings/tool-policy-mode`),
      fetchJson(`${API_URL}/api/settings/mcp-policy-mode`),
      fetchJson(`${API_URL}/api/settings/approval-mode`),
    ]);

    if (isCancelled()) return;

    if (runtimeStatusResult.ok && runtimeStatusResult.payload && typeof runtimeStatusResult.payload === "object") {
      setRuntimeStatus(runtimeStatusResult.payload as RuntimeStatus);
    }
    if (observerResult.ok) {
      setObserverState((observerResult.payload as ObserverState | null) ?? {});
    }
    if (auditResult.ok) {
      setAuditEvents(Array.isArray(auditResult.payload) ? auditResult.payload : []);
    }
    if (approvalsResult.ok) {
      setPendingApprovals(Array.isArray(approvalsResult.payload) ? approvalsResult.payload : []);
    }
    if (continuityResult.ok && continuityResult.payload) {
      const continuityPayload = continuityResult.payload as ObserverContinuitySnapshot;
      setDaemonPresence(continuityPayload.daemon);
      setDesktopNotifications(continuityPayload.notifications ?? []);
      setQueuedInsights(continuityPayload.queued_insights ?? []);
      setQueuedBundleCount(continuityPayload.queued_insight_count ?? 0);
      setRecentInterventions(continuityPayload.recent_interventions ?? []);
      setDesktopRouteStatuses(continuityPayload.reach?.route_statuses ?? []);
    }
    if (capabilitiesResult.ok && capabilitiesResult.payload) {
      const capabilityPayload = capabilitiesResult.payload as CapabilityOverview;
      setWorkflows(Array.isArray(capabilityPayload.workflows) ? capabilityPayload.workflows : []);
      setSkills(Array.isArray(capabilityPayload.skills) ? capabilityPayload.skills : []);
      setMcpServers(Array.isArray(capabilityPayload.mcp_servers) ? capabilityPayload.mcp_servers : []);
      setTools(Array.isArray(capabilityPayload.native_tools) ? capabilityPayload.native_tools : []);
      setStarterPacks(Array.isArray(capabilityPayload.starter_packs) ? capabilityPayload.starter_packs : []);
      setCatalogItems(Array.isArray(capabilityPayload.catalog_items) ? capabilityPayload.catalog_items : []);
      setCapabilityRecommendations(
        Array.isArray(capabilityPayload.recommendations) ? capabilityPayload.recommendations : [],
      );
      setRunbooks(Array.isArray(capabilityPayload.runbooks) ? capabilityPayload.runbooks : []);
    }
    if (extensionsResult.ok) {
      setExtensionPackages(normalizeExtensionPackagesPayload(extensionsResult.payload));
    } else {
      setExtensionPackages([]);
    }
    const activityLedgerScope = sessionId ?? "__all__";
    if (
      activityLedgerResult.ok
      && activityLedgerResult.payload
      && typeof activityLedgerResult.payload === "object"
      && Array.isArray((activityLedgerResult.payload as { items?: unknown }).items)
    ) {
      const payload = activityLedgerResult.payload as { items?: unknown; summary?: unknown };
      const items = Array.isArray(payload.items)
        ? payload.items.flatMap((item) => (item && typeof item === "object" && !Array.isArray(item)
          ? [normalizeActivityLedgerEntry(item as Record<string, unknown>)]
          : []))
        : [];
      const derivedSummary = deriveActivitySummary(items);
      setActivityLedger(
        items,
      );
      setActivitySummary(
        payload.summary && typeof payload.summary === "object"
          ? ({ ...derivedSummary, ...(payload.summary as Partial<ActivityLedgerSummary>) } as ActivityLedgerSummary)
          : derivedSummary,
      );
      activityLedgerScopeRef.current = activityLedgerScope;
    } else if (activityLedgerScopeRef.current !== activityLedgerScope) {
      setActivityLedger([]);
      setActivitySummary(deriveActivitySummary([]));
      activityLedgerScopeRef.current = activityLedgerScope;
    }
    if (workflowRunsResult.ok && workflowRunsResult.payload && typeof workflowRunsResult.payload === "object") {
      const runs = (workflowRunsResult.payload as { runs?: unknown }).runs;
      setWorkflowRuns(
        Array.isArray(runs)
          ? runs.map((run: Record<string, unknown>) => normalizeWorkflowRun(run))
          : [],
      );
    }
    if (toolModeResult.ok && toolModeResult.payload && typeof toolModeResult.payload === "object") {
      setToolPolicyMode(((toolModeResult.payload as { mode?: string }).mode ?? "unknown") as ToolPolicyMode | "unknown");
    }
    if (mcpModeResult.ok && mcpModeResult.payload && typeof mcpModeResult.payload === "object") {
      setMcpPolicyMode(((mcpModeResult.payload as { mode?: string }).mode ?? "unknown") as McpPolicyMode | "unknown");
    }
    if (approvalModeResult.ok && approvalModeResult.payload && typeof approvalModeResult.payload === "object") {
      setApprovalMode(((approvalModeResult.payload as { mode?: string }).mode ?? "unknown") as ApprovalMode | "unknown");
    }
  }, [sessionId]);

  useEffect(() => {
    let cancelled = false;

    const refresh = async () => {
      try {
        await refreshCockpit(() => cancelled);
      } catch {}
    };

    void refresh();
    const interval = window.setInterval(() => {
      void refresh();
    }, 12_000);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, [refreshCockpit]);

  useEffect(() => {
    const focusComposer = () => inputRef.current?.focus();
    window.addEventListener("seraph-cockpit-focus-composer", focusComposer as EventListener);
    return () => {
      window.removeEventListener("seraph-cockpit-focus-composer", focusComposer as EventListener);
    };
  }, []);

  useEffect(() => {
    const handleCompose = (event: Event) => {
      const message = (event as CustomEvent<{ message?: string }>).detail?.message?.trim();
      if (!message) return;
      setComposer(message);
      inputRef.current?.focus();
    };

    window.addEventListener("seraph-cockpit-compose", handleCompose as EventListener);
    return () => {
      window.removeEventListener("seraph-cockpit-compose", handleCompose as EventListener);
    };
  }, []);

  useEffect(() => {
    const openPalette = () => setPaletteOpen(true);
    window.addEventListener("seraph-cockpit-open-palette", openPalette as EventListener);
    return () => {
      window.removeEventListener("seraph-cockpit-open-palette", openPalette as EventListener);
    };
  }, []);

  useEffect(() => {
    if (!paletteOpen) return;
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setPaletteOpen(false);
      }
    };
    window.addEventListener("keydown", handleEscape);
    return () => window.removeEventListener("keydown", handleEscape);
  }, [paletteOpen]);

  useEffect(() => {
    if (!windowsMenuOpen) return;
    const handlePointerDown = (event: PointerEvent) => {
      if (!windowsMenuRef.current?.contains(event.target as Node)) {
        setWindowsMenuOpen(false);
      }
    };
    window.addEventListener("pointerdown", handlePointerDown);
    return () => window.removeEventListener("pointerdown", handlePointerDown);
  }, [windowsMenuOpen]);

  useEffect(() => {
    if (!studioOpen) return;
    let cancelled = false;
    void refreshCockpit(() => cancelled);
    return () => {
      cancelled = true;
    };
  }, [refreshCockpit, studioOpen]);

  useEffect(() => {
    syncCockpitPaneStack(paneVisibility);
  }, [paneVisibility, syncCockpitPaneStack]);

  const activeSession = sessions.find((item) => item.id === sessionId) ?? null;
  const activeLayout = getCockpitLayout(activeLayoutId);
  const visibleSections = useMemo(
    () => ({
      rail:
        paneVisibility.sessions_pane
        || paneVisibility.goals_pane
        || paneVisibility.outputs_pane
        || paneVisibility.approvals_pane,
      guardianState: paneVisibility.guardian_state_pane,
      timeline: paneVisibility.operator_timeline_pane,
      workflows: paneVisibility.workflows_pane,
      interventions: paneVisibility.interventions_pane,
      audit: paneVisibility.audit_pane,
      trace: paneVisibility.trace_pane,
      inspector: paneVisibility.inspector_pane,
      conversation:
        paneVisibility.presence_pane
        || paneVisibility.conversation_pane
        || paneVisibility.desktop_shell_pane
        || paneVisibility.operator_surface_pane,
    }),
    [paneVisibility],
  );
  const recentConversation = messages.slice(-18);
  const latestResponse = useMemo(
    () =>
      [...messages]
        .reverse()
        .find((message) =>
          message.role === "agent"
          || message.role === "error"
          || message.role === "approval"
          || message.role === "proactive",
        ) ?? null,
    [messages],
  );
  const artifacts = useMemo(() => collectArtifacts(auditEvents), [auditEvents]);
  const workflowRunsWithArtifacts = useMemo(() => {
    const artifactMap = new Map(
      artifacts.map((artifact) => [`${artifact.sessionId ?? "global"}:${artifact.filePath}`, artifact]),
    );
    return workflowRuns.map((run) => ({
      ...run,
      artifacts: run.artifactPaths
        .map((filePath) => artifactMap.get(`${run.sessionId ?? "global"}:${filePath}`))
        .filter((artifact): artifact is ArtifactRecord => artifact != null),
    }));
  }, [artifacts, workflowRuns]);
  const workflowRunById = useMemo(
    () => new Map(workflowRunsWithArtifacts.map((workflow) => [workflow.id, workflow])),
    [workflowRunsWithArtifacts],
  );
  const workflowRunByIdentity = useMemo(() => {
    const entries = workflowRunsWithArtifacts
      .filter((workflow) => typeof workflow.runIdentity === "string" && workflow.runIdentity.trim().length > 0)
      .map((workflow) => [workflow.runIdentity as string, workflow] as const);
    return new Map(entries);
  }, [workflowRunsWithArtifacts]);
  const workflowChildrenByParentIdentity = useMemo(() => {
    const grouped = new Map<string, WorkflowRunRecord[]>();
    workflowRunsWithArtifacts.forEach((workflow) => {
      if (!workflow.parentRunIdentity) return;
      const existing = grouped.get(workflow.parentRunIdentity) ?? [];
      existing.push(workflow);
      grouped.set(workflow.parentRunIdentity, existing);
    });
    grouped.forEach((entries, key) => {
      grouped.set(key, [...entries].sort(compareWorkflowRunsNewestFirst));
    });
    return grouped;
  }, [workflowRunsWithArtifacts]);
  const workflowFamilyByRootIdentity = useMemo(() => {
    const grouped = new Map<string, WorkflowRunRecord[]>();
    workflowRunsWithArtifacts.forEach((workflow) => {
      const rootIdentity = workflowFamilyRootIdentity(workflow);
      const existing = grouped.get(rootIdentity) ?? [];
      existing.push(workflow);
      grouped.set(rootIdentity, existing);
    });
    grouped.forEach((entries, key) => {
      grouped.set(key, [...entries].sort(compareWorkflowRunsNewestFirst));
    });
    return grouped;
  }, [workflowRunsWithArtifacts]);
  const artifactRoundtripWorkflows = useMemo(
    () =>
      workflows.filter(
        (workflow) =>
          workflow.user_invocable
          && workflow.enabled
          && workflow.is_available !== false
          && supportsArtifactRoundtrip(workflow),
      ),
    [workflows],
  );
  const workflowDefinitionByName = useMemo(
    () => new Map(workflows.map((workflow) => [workflow.name, workflow])),
    [workflows],
  );
  function compatibleArtifactWorkflows(
    artifactPath: string,
    producedArtifactTypes?: string[],
    excludeWorkflowNames: string[] = [],
  ): WorkflowInfo[] {
    return artifactRoundtripWorkflows.filter((workflow) =>
      !excludeWorkflowNames.includes(workflow.name)
      && !excludeWorkflowNames.includes(workflow.tool_name)
      &&
      workflowAcceptsArtifact(workflow, artifactPath, producedArtifactTypes),
    );
  }
  function resolveWorkflowRun(workflow: WorkflowRunRecord): WorkflowRunRecord {
    if (workflow.runIdentity) {
      return workflowRunByIdentity.get(workflow.runIdentity) ?? workflowRunById.get(workflow.id) ?? workflow;
    }
    return workflowRunById.get(workflow.id) ?? workflow;
  }
  function workflowChildRuns(workflow: WorkflowRunRecord): WorkflowRunRecord[] {
    if (!workflow.runIdentity) return [];
    return workflowChildrenByParentIdentity.get(workflow.runIdentity) ?? [];
  }
  function workflowFamilyRuns(workflow: WorkflowRunRecord): WorkflowRunRecord[] {
    return workflowFamilyByRootIdentity.get(workflowFamilyRootIdentity(workflow)) ?? [workflow];
  }
  function workflowPeerRuns(workflow: WorkflowRunRecord): WorkflowRunRecord[] {
    if (!workflow.parentRunIdentity) return [];
    return (workflowChildrenByParentIdentity.get(workflow.parentRunIdentity) ?? [])
      .filter((entry) => entry.runIdentity !== workflow.runIdentity);
  }
  function workflowAncestorRuns(workflow: WorkflowRunRecord): WorkflowRunRecord[] {
    const ancestors: WorkflowRunRecord[] = [];
    const seen = new Set<string>();
    let cursor = workflow.parentRunIdentity;
    while (cursor && !seen.has(cursor)) {
      seen.add(cursor);
      const parent = workflowRunByIdentity.get(cursor);
      if (!parent) break;
      ancestors.push(parent);
      cursor = parent.parentRunIdentity ?? null;
    }
    return ancestors;
  }
  function workflowLatestBranchRun(workflow: WorkflowRunRecord): WorkflowRunRecord | null {
    const childRuns = workflowChildRuns(workflow);
    if (childRuns.length > 0) return childRuns[0] ?? null;
    if (!workflow.runIdentity) return null;
    const ancestorIds = new Set(workflowAncestorRuns(workflow).map((entry) => entry.runIdentity).filter(Boolean));
    const familyRuns = workflowFamilyRuns(workflow);
    return familyRuns.find((entry) => (
      entry.runIdentity !== workflow.runIdentity
      && !!entry.parentRunIdentity
      && !ancestorIds.has(entry.runIdentity)
    )) ?? null;
  }
  function workflowSupervisionLabel(workflow: WorkflowRunRecord): string {
    const childRuns = workflowChildRuns(workflow);
    if ((workflow.pendingApprovalCount ?? 0) > 0) return "approval gate";
    if (workflow.status === "running") return "live run";
    if (workflow.status === "awaiting_approval") return "awaiting approval";
    if ((workflow.status === "degraded" || workflow.status === "failed") && workflow.checkpointContextAvailable === true) {
      return "recovery ready";
    }
    if ((workflow.status === "degraded" || workflow.status === "failed") && workflow.continuedErrorSteps.length > 0) {
      return "rerun only";
    }
    if (childRuns.length > 0) return "branched";
    if (workflow.status === "succeeded") return "completed";
    return workflow.status.replace(/_/g, " ");
  }
  function workflowSupervisionSummary(workflow: WorkflowRunRecord): string[] {
    const summary = [workflowSupervisionLabel(workflow)];
    const childRuns = workflowChildRuns(workflow);
    const peerRuns = workflowPeerRuns(workflow);
    const familyRuns = workflowFamilyRuns(workflow);
    if (childRuns.length > 0) {
      summary.push(`${childRuns.length} child ${childRuns.length === 1 ? "branch" : "branches"}`);
    }
    if (peerRuns.length > 0) {
      summary.push(`${peerRuns.length} peer ${peerRuns.length === 1 ? "branch" : "branches"}`);
    }
    if (familyRuns.length > 1) {
      summary.push(`family ${familyRuns.length}`);
    }
    return summary;
  }
  function workflowCanContinue(workflow: WorkflowRunRecord): boolean {
    const approval = approvalForWorkflow(workflow);
    const continueTarget = approval?.thread_id ?? approval?.session_id ?? workflow.threadId ?? workflow.sessionId;
    if ((approval?.resume_message ?? workflow.threadContinueMessage) && continueTarget) {
      return true;
    }
    return workflowCheckpointActions(workflow).length > 0 || !!workflow.retryFromStepDraft;
  }
  function workflowBestContinuationRun(workflow: WorkflowRunRecord): WorkflowRunRecord | null {
    const resolved = resolveWorkflowRun(workflow);
    const familyRuns = workflowFamilyRuns(resolved);
    const ancestorIds = new Set(
      workflowAncestorRuns(resolved)
        .map((entry) => entry.runIdentity)
        .filter((entry): entry is string => typeof entry === "string" && entry.length > 0),
    );
    const candidates = [
      resolved,
      ...familyRuns.filter((entry) => (
        entry.runIdentity !== resolved.runIdentity
        && (!entry.runIdentity || !ancestorIds.has(entry.runIdentity))
      )),
    ];
    return candidates.find((entry) => (
      workflowCanContinue(entry)
      || entry.pendingApprovalCount
    )) ?? null;
  }
  function workflowFailureLineage(workflow: WorkflowRunRecord): WorkflowRunRecord[] {
    const familyRuns = workflowFamilyRuns(workflow);
    return familyRuns.filter((entry) => (
      entry.status === "failed"
      || entry.status === "degraded"
      || entry.continuedErrorSteps.length > 0
    ));
  }
  function workflowComparisonSummary(base: WorkflowRunRecord, target: WorkflowRunRecord): string[] {
    const summary: string[] = [];
    const baseUpdated = workflowUpdatedAtMs(base);
    const targetUpdated = workflowUpdatedAtMs(target);
    if (targetUpdated > baseUpdated) {
      summary.push("newer than current");
    } else if (targetUpdated < baseUpdated) {
      summary.push("older than current");
    }
    if (target.status !== base.status) {
      summary.push(`${target.status} vs ${base.status}`);
    }
    if ((target.artifactPaths.length || target.artifacts.length) > 0) {
      summary.push(`${Math.max(target.artifactPaths.length, target.artifacts.length)} outputs`);
    }
    if (target.resumeCheckpointLabel && target.resumeCheckpointLabel !== base.resumeCheckpointLabel) {
      summary.push(`checkpoint ${target.resumeCheckpointLabel}`);
    }
    return summary;
  }
  function workflowFamilyArtifactOutputs(workflow: WorkflowRunRecord): WorkflowFamilyArtifactOutput[] {
    const seen = new Set<string>();
    const outputs: WorkflowFamilyArtifactOutput[] = [];
    workflowFamilyRuns(workflow).forEach((entry) => {
      if (entry.runIdentity === workflow.runIdentity || entry.id === workflow.id) {
        return;
      }
      entry.artifacts.forEach((artifact) => {
        const key = `${artifact.filePath}:${entry.runIdentity ?? entry.id}`;
        if (seen.has(key)) return;
        seen.add(key);
        outputs.push({
          key,
          filePath: artifact.filePath,
          createdAt: artifact.createdAt,
          sourceWorkflow: entry,
          sourceLabel: entry.summary,
        });
      });
      entry.artifactPaths.forEach((filePath, index) => {
        const key = `${filePath}:${entry.runIdentity ?? entry.id}`;
        if (seen.has(key)) return;
        seen.add(key);
        outputs.push({
          key,
          filePath,
          createdAt: entry.updatedAt,
          sourceWorkflow: entry,
          sourceLabel: index === 0 ? entry.summary : `${entry.summary} output ${index + 1}`,
        });
      });
    });
    return outputs
      .sort((a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt))
      .slice(0, 4);
  }
  function workflowBranchOriginSummary(workflow: WorkflowRunRecord): string[] {
    const summary: string[] = [];
    const parent = workflow.parentRunIdentity
      ? workflowRunByIdentity.get(workflow.parentRunIdentity)
      : null;
    const familyRuns = workflowFamilyRuns(workflow);
    if (parent) {
      summary.push(`from ${parent.workflowName}`);
    } else if (familyRuns.length > 1) {
      summary.push("root branch");
    }
    if (workflow.resumeCheckpointLabel) {
      summary.push(`checkpoint ${workflow.resumeCheckpointLabel}`);
    } else if (workflow.branchKind) {
      summary.push(workflow.branchKind.replace(/_/g, " "));
    }
    if (typeof workflow.branchDepth === "number") {
      summary.push(`depth ${workflow.branchDepth}`);
    }
    return summary;
  }
  function workflowBranchDebugSummary(workflow: WorkflowRunRecord): string[] {
    const summary = workflowBranchOriginSummary(workflow);
    const bestContinuation = workflowBestContinuationRun(workflow);
    if (bestContinuation) {
      summary.push(
        bestContinuation.runIdentity === workflow.runIdentity
          ? "continue here"
          : `continue ${bestContinuation.workflowName}`,
      );
      summary.push(workflowSupervisionLabel(bestContinuation));
    }
    const latestFailure = workflowFailureLineage(workflow)[0];
    if (latestFailure) {
      summary.push(`latest failure ${latestFailure.summary}`);
    }
    return summary;
  }
  function inspectWorkflowRun(workflow: WorkflowRunRecord | null | undefined) {
    if (!workflow) return;
    setSelectedInspector({ kind: "workflow", workflow: resolveWorkflowRun(workflow) });
  }
  function continueWorkflowRun(workflow: WorkflowRunRecord | null | undefined) {
    if (!workflow) return;
    const resolved = resolveWorkflowRun(workflow);
    const approval = approvalForWorkflow(resolved);
    const continueMessage = approval?.resume_message ?? resolved.threadContinueMessage;
    const threadId = approval?.thread_id ?? approval?.session_id ?? resolved.threadId ?? resolved.sessionId;
    if (continueMessage && threadId) {
      void queueThreadDraft(continueMessage, threadId);
      return;
    }
    const checkpointAction = workflowCheckpointActions(resolved)[0];
    if (checkpointAction?.draft) {
      queueComposerDraft(checkpointAction.draft);
      return;
    }
    if (resolved.retryFromStepDraft) {
      queueComposerDraft(resolved.retryFromStepDraft);
      return;
    }
    if (resolved.replayAllowed !== false) {
      queueComposerDraft(resolved.replayDraft ?? buildWorkflowReplayDraft(resolved));
    }
  }
  function buildWorkflowRedirectDraft(workflow: WorkflowRunRecord): string {
    const resolved = resolveWorkflowRun(workflow);
    const parts = [
      `Redirect workflow "${resolved.workflowName}" from its current state.`,
      `Current status: ${resolved.status}.`,
      `Current summary: ${resolved.summary}.`,
    ];
    if (resolved.threadContinueMessage) {
      parts.push("Keep the current thread continuity instead of starting a fresh run.");
    }
    if (resolved.artifactPaths[0]) {
      parts.push(`Consider the latest artifact at "${resolved.artifactPaths[0]}".`);
    }
    return parts.join(" ");
  }
  const availableWorkflows = useMemo(
    () => workflows.filter((workflow) => workflow.is_available !== false),
    [workflows],
  );
  const blockedWorkflows = useMemo(
    () => workflows.filter((workflow) => workflow.is_available === false),
    [workflows],
  );
  const readySkills = useMemo(
    () => skills.filter((skill) => skill.availability === "ready"),
    [skills],
  );
  const readyMcpServers = useMemo(
    () => mcpServers.filter((server) => server.availability === "ready"),
    [mcpServers],
  );
  const mcpTools = useMemo(
    () => tools.filter((tool) => tool.name.startsWith("mcp_") || tool.execution_boundaries?.includes("external_mcp")),
    [tools],
  );
  const highRiskTools = useMemo(
    () => tools.filter((tool) => tool.risk_level === "high"),
    [tools],
  );
  const blockedTools = useMemo(
    () => tools.filter((tool) => tool.availability === "blocked"),
    [tools],
  );
  const invocableWorkflows = useMemo(
    () => workflows.filter((workflow) => workflow.user_invocable),
    [workflows],
  );
  const approvalWorkflows = useMemo(
    () => availableWorkflows.filter((workflow) => workflow.user_invocable && workflow.requires_approval),
    [availableWorkflows],
  );
  const recentTrace = messages
    .filter((message) => message.role === "step" || message.role === "error")
    .slice(-8)
    .reverse();
  const sessionTitleById = useMemo(
    () => Object.fromEntries(sessions.map((item) => [item.id, item.title])),
    [sessions],
  );
  const knownSessionIds = useMemo(
    () => new Set(sessions.map((item) => item.id)),
    [sessions],
  );
  const topGoals = collectGoalTitles(goalTree, 5);
  const readyStarterPacks = useMemo(
    () => starterPacks.filter((pack) => pack.availability === "ready"),
    [starterPacks],
  );
  const connectionLabel = connectionStatus === "connected" ? "live" : connectionStatus;
  const runtimeProviderLabel = (runtimeStatus?.provider ?? "unknown").replace(/[_.-]+/g, " ").toUpperCase();
  const runtimeModelLabel = (runtimeStatus?.model_label ?? runtimeStatus?.model ?? "unknown")
    .replace(/^openrouter\//, "")
    .replace(/^anthropic\//, "")
    .replace(/[_/-]+/g, " ")
    .toUpperCase();
  const runtimeBuildLabel = runtimeStatus?.build_id ?? SERAPH_BUILD_ID;
  const workspaceTelemetryLeft = `${runtimeProviderLabel} · ${runtimeModelLabel}`;
  const workspaceTelemetryCenter = `${activeLayout.label.toUpperCase()} WORKSPACE · 16PX GRID SNAP · ${runtimeBuildLabel}`;
  const workspaceTelemetryRight = `${connectionLabel.toUpperCase()} LINK · ${toolPolicyMode.toUpperCase()} TOOLS · ${approvalMode.toUpperCase()} APPROVAL`;
  const submitDisabled = isAgentBusy || !composer.trim();
  const operatorRunbooks = useMemo(
    () => runbooks,
    [runbooks],
  );
  const allActivityGroups = useMemo(
    () => buildActivityLedgerGroups(activityLedger),
    [activityLedger],
  );
  const visibleActivityGroups = useMemo(
    () => (activityFilter === "all"
      ? allActivityGroups
      : allActivityGroups.filter(
        (group) =>
          group.lead.category === activityFilter
          || group.children.some((child) => child.item?.category === activityFilter),
      )),
    [activityFilter, allActivityGroups],
  );
  const seraphPresenceSnapshot = useMemo(
    () => ({
      connectionStatus,
      animationState: agentVisual.animationState,
      isAgentBusy,
      pendingApprovalCount: pendingApprovals.length,
      recentTraceRole: recentTrace[0]?.role ?? null,
      recentTraceTool: recentTrace.find((message) => message.toolUsed)?.toolUsed ?? null,
      latestResponseRole: latestResponse?.role ?? null,
      ambientState,
      dataQuality: observerState?.data_quality ?? null,
      recentInterventionCount: recentInterventions.length,
      operatorStatus,
    }),
    [
      agentVisual.animationState,
      ambientState,
      connectionStatus,
      isAgentBusy,
      latestResponse?.role,
      observerState?.data_quality,
      operatorStatus,
      pendingApprovals.length,
      recentInterventions.length,
      recentTrace,
    ],
  );
  const operatorMacros = useMemo(
    () => savedRunbooks,
    [savedRunbooks],
  );
  const extensionPackagesById = useMemo(
    () => new Map(extensionPackages.map((item) => [item.id, item])),
    [extensionPackages],
  );
  const importedCapabilityFamilies = useMemo<ImportedCapabilityFamilySummary[]>(
    () => IMPORTED_CAPABILITY_FAMILY_DEFS.map((definition) => {
      const entries = extensionPackages.flatMap((extensionPackage) => (
        extensionPackage.contributions
          .filter((contribution) => contribution.type === definition.type)
          .map((contribution) => ({
            packageId: extensionPackage.id,
            packageLabel: extensionPackage.display_name,
            contribution,
          }))
      ));
      const packages = Array.from(new Set(entries.map((entry) => entry.packageLabel)));
      const activeEntries = entries.filter((entry) => isContributionActive(entry.contribution));
      const ready = activeEntries.filter((entry) => (
        entry.contribution.health?.ready
        || ["ready", "active"].includes(entry.contribution.status ?? "")
      )).length;
      const attention = entries.filter((entry) => {
        const status = entry.contribution.status ?? entry.contribution.health?.state ?? "";
        return [
          "degraded",
          "invalid",
          "invalid_config",
          "requires_config",
          "planned",
          "overridden",
        ].includes(status)
          || entry.contribution.permission_profile?.status === "missing_permissions";
      }).length;
      const approval = entries.filter((entry) => (
        entry.contribution.permission_profile?.requires_approval
        || entry.contribution.approval_behavior === "always"
      )).length;
      return {
        type: definition.type,
        label: definition.label,
        total: activeEntries.length,
        installed: entries.length,
        ready,
        attention,
        approval,
        packages,
        entries,
      };
    }).filter((entry) => entry.installed > 0),
    [extensionPackages],
  );
  const extensionGovernanceQueue = useMemo<ExtensionGovernanceSummary[]>(
    () => extensionPackages
      .flatMap((extensionPackage) => {
        const details: string[] = [];
        const missingPermissions = summarizeMissingPermissions(extensionPackage.permission_summary);
        if (missingPermissions.length > 0) {
          details.push(`missing ${missingPermissions.join(", ")}`);
        }
        if (extensionPackage.approval_profile?.requires_lifecycle_approval) {
          const boundaries = extensionPackage.approval_profile.lifecycle_boundaries.length
            ? extensionPackage.approval_profile.lifecycle_boundaries.join(", ")
            : extensionPackage.approval_profile.runtime_behavior;
          details.push(`lifecycle approval ${boundaries}`);
        }
        if (extensionPackage.approval_profile?.requires_runtime_approval) {
          details.push(`runtime approval ${extensionPackage.approval_profile.runtime_behavior}`);
        }
        if (extensionPackage.connector_summary?.states) {
          const degradedStates = Object.entries(extensionPackage.connector_summary.states)
            .filter(([state, count]) => state !== "ready" && count > 0)
            .map(([state, count]) => `${count} ${state}`);
          if (degradedStates.length > 0) {
            details.push(`connectors ${degradedStates.join(", ")}`);
          }
        }
        if (extensionPackage.status === "degraded" && extensionPackage.issues[0]?.message) {
          details.push(extensionPackage.issues[0].message);
        }
        if (details.length === 0) return [];
        return [{
          packageId: extensionPackage.id,
          label: extensionPackage.display_name,
          riskLevel: extensionPackage.permission_summary?.risk_level ?? extensionPackage.approval_profile?.risk_level ?? "low",
          status: extensionPackage.status,
          detail: details.join(" · "),
          packageInfo: extensionPackage,
        }];
      })
      .sort((left, right) => {
        const severity = (value: ExtensionGovernanceSummary) => {
          if (value.status === "degraded") return 0;
          if (value.riskLevel === "high") return 1;
          if (value.detail.includes("missing")) return 2;
          return 3;
        };
        return severity(left) - severity(right) || left.label.localeCompare(right.label);
      }),
    [extensionPackages],
  );
  const operatorTriageEntries = useMemo<OperatorTriageEntry[]>(() => {
    const entries: OperatorTriageEntry[] = [];

    pendingApprovals.forEach((approval) => {
      const threadLabel = approval.thread_label
        ?? (approval.thread_id ? sessionTitleById[approval.thread_id] : null)
        ?? (approval.thread_id ? `thread ${approval.thread_id.slice(0, 6)}` : null);
      entries.push({
        id: `approval:${approval.id}`,
        kind: "approval",
        label: `approval: ${approval.tool_name}`,
        detail: `awaiting approval · ${approval.summary}`,
        meta: [approval.risk_level, threadLabel, formatAge(approval.created_at)].filter(Boolean).join(" · "),
        priority: 100,
        threadId: approval.thread_id ?? approval.session_id ?? null,
        continueMessage: approval.resume_message ?? null,
        approval,
      });
    });

    workflowRunsWithArtifacts.forEach((workflow) => {
      const approval = approvalForWorkflow(workflow);
      const latestBranch = workflowLatestBranchRun(workflow);
      const needsAttention = (
        !!approval
        || workflow.status === "running"
        || workflow.status === "awaiting_approval"
        || workflow.status === "failed"
        || workflow.status === "degraded"
        || workflow.replayAllowed === false
        || workflow.checkpointContextAvailable === true
        || latestBranch !== null
      );
      if (!needsAttention) return;

      let priority = 78;
      if (approval) priority = 96;
      else if (workflow.status === "running") priority = 92;
      else if (workflow.status === "awaiting_approval") priority = 90;
      else if (workflow.status === "failed" || workflow.status === "degraded") priority = 88;
      else if (workflow.replayAllowed === false) priority = 82;

      const threadLabel = workflow.threadLabel
        ?? (workflow.threadId ? sessionTitleById[workflow.threadId] : null)
        ?? (workflow.threadId ? `thread ${workflow.threadId.slice(0, 6)}` : null);
      entries.push({
        id: `workflow:${workflow.id}`,
        kind: "workflow",
        label: `workflow ${formatContinuityLabel(workflow.status)}: ${workflow.workflowName}`,
        detail: `${formatContinuityLabel(workflow.status)} · ${workflow.summary}`,
        meta: [
          workflowSupervisionLabel(workflow),
          approval ? "approval waiting" : null,
          latestBranch ? `latest branch ${latestBranch.workflowName}` : null,
          threadLabel,
          formatAge(workflow.updatedAt),
        ].filter(Boolean).join(" · "),
        priority,
        threadId: approval?.thread_id ?? approval?.session_id ?? workflow.threadId ?? workflow.sessionId ?? null,
        continueMessage: approval?.resume_message ?? workflow.threadContinueMessage ?? null,
        workflow,
      });
    });

    queuedInsights.forEach((item) => {
      const threadLabel = item.thread_label
        ?? (item.thread_id ? sessionTitleById[item.thread_id] : null)
        ?? (item.thread_id ? `thread ${item.thread_id.slice(0, 6)}` : "ambient queue");
      entries.push({
        id: `queued:${item.id}`,
        kind: "queued",
        label: `queued: ${item.intervention_type}`,
        detail: `queued follow-up · ${item.content_excerpt}`,
        meta: [
          item.continuation_mode ? formatContinuityLabel(item.continuation_mode) : "queued",
          threadLabel,
          formatAge(item.created_at),
        ].filter(Boolean).join(" · "),
        priority: 84,
        threadId: item.thread_id ?? item.session_id ?? null,
        continueMessage: item.resume_message ?? `Follow up on this deferred guardian item: ${item.content_excerpt}`,
      });
    });

    desktopRouteStatuses
      .filter((route) => route.status !== "ready")
      .forEach((route) => {
        entries.push({
          id: `reach:${route.route}`,
          kind: "reach",
          label: `reach: ${route.label}`,
          detail: `${formatContinuityLabel(route.status)} · ${route.summary}`,
          meta: [
            formatContinuityLabel(route.status),
            route.selected_transport ? `via ${formatContinuityLabel(route.selected_transport)}` : null,
            route.repair_hint,
          ].filter(Boolean).join(" · "),
          priority: route.status === "unavailable" ? 86 : 72,
          route,
        });
      });

    return entries
      .sort((left, right) => right.priority - left.priority || left.label.localeCompare(right.label))
      .slice(0, 8);
  }, [
    desktopRouteStatuses,
    pendingApprovals,
    queuedInsights,
    sessionTitleById,
    workflowRunsWithArtifacts,
  ]);
  const operatorEvidenceEntries = useMemo<OperatorEvidenceEntry[]>(() => {
    const entries: OperatorEvidenceEntry[] = [];
    const latestArtifactEvidence = workflowRunsWithArtifacts
      .flatMap((workflow) => workflow.artifacts.map((artifact) => ({ workflow, artifact })))
      .sort((left, right) => (
        new Date(right.artifact.createdAt).getTime() - new Date(left.artifact.createdAt).getTime()
        || compareWorkflowRunsNewestFirst(left.workflow, right.workflow)
      ))[0] ?? null;
    if (latestArtifactEvidence) {
      const { workflow: workflowWithArtifact, artifact } = latestArtifactEvidence;
      const threadLabel = workflowWithArtifact.threadLabel
        ?? (workflowWithArtifact.threadId ? sessionTitleById[workflowWithArtifact.threadId] : null)
        ?? (workflowWithArtifact.threadId ? `thread ${workflowWithArtifact.threadId.slice(0, 6)}` : null);
      entries.push({
        id: `artifact:${artifact.id}`,
        kind: "artifact",
        label: `artifact: ${artifact.filePath}`,
        detail: `${workflowWithArtifact.workflowName} · ${formatContinuityLabel(workflowWithArtifact.status)} · ${artifact.summary}`,
        meta: [artifact.source, threadLabel, formatAge(artifact.createdAt)].filter(Boolean).join(" · "),
        sortKey: new Date(artifact.createdAt).getTime(),
        threadId: workflowWithArtifact.threadId ?? workflowWithArtifact.sessionId ?? artifact.sessionId ?? null,
        artifact,
        workflow: workflowWithArtifact,
      });
    }

    const latestTrace = recentTrace[0];
    if (latestTrace) {
      const relatedAudit = auditEvents.find((event) => event.tool_name === latestTrace.toolUsed) ?? null;
      entries.push({
        id: `trace:${latestTrace.id}`,
        kind: "trace",
        label: `trace: ${latestTrace.toolUsed ?? labelForRole(latestTrace)}`,
        detail: latestTrace.content,
        meta: [
          latestTrace.stepNumber != null ? `step ${latestTrace.stepNumber}` : null,
          relatedAudit?.summary ?? null,
          formatAge(latestTrace.timestamp),
        ].filter(Boolean).join(" · "),
        sortKey: latestTrace.timestamp,
        trace: latestTrace,
        audit: relatedAudit,
      });
    }

    const approval = pendingApprovals[0] ?? null;
    if (approval) {
      const threadLabel = approval.thread_label
        ?? (approval.thread_id ? sessionTitleById[approval.thread_id] : null)
        ?? (approval.thread_id ? `thread ${approval.thread_id.slice(0, 6)}` : null);
      entries.push({
        id: `approval-context:${approval.id}`,
        kind: "approval",
        label: `approval context: ${approval.tool_name}`,
        detail: `approval context · ${approval.summary}`,
        meta: [
          approval.risk_level,
          approval.extension_action ? `extension ${approval.extension_action}` : null,
          threadLabel,
          formatAge(approval.created_at),
        ].filter(Boolean).join(" · "),
        sortKey: new Date(approval.created_at).getTime(),
        threadId: approval.thread_id ?? approval.session_id ?? null,
        continueMessage: approval.resume_message ?? null,
        approval,
      });
    }

    return entries
      .sort((left, right) => right.sortKey - left.sortKey || left.label.localeCompare(right.label))
      .slice(0, 3);
  }, [
    auditEvents,
    pendingApprovals,
    recentTrace,
    sessionTitleById,
    workflowRunsWithArtifacts,
  ]);
  const primaryTriageEntry = operatorTriageEntries[0] ?? null;
  const primaryApprovalTriageEntry = operatorTriageEntries.find((entry) => entry.approval) ?? null;
  const primaryWorkflowTriageEntry = operatorTriageEntries.find((entry) => entry.workflow) ?? null;
  const primaryEvidenceEntry = operatorEvidenceEntries[0] ?? null;
  function inspectOperatorTriageEntry(entry: OperatorTriageEntry | null | undefined) {
    if (!entry) return;
    if (entry.approval) {
      setSelectedInspector({ kind: "approval", approval: entry.approval });
      return;
    }
    if (entry.workflow) {
      inspectWorkflowRun(entry.workflow);
      return;
    }
    focusPane("desktop_shell_pane");
  }
  function continueOperatorTriageEntry(entry: OperatorTriageEntry | null | undefined) {
    if (!entry) return;
    if (entry.continueMessage) {
      void queueThreadDraft(entry.continueMessage, entry.threadId ?? undefined);
      return;
    }
    if (entry.workflow) {
      continueWorkflowRun(entry.workflow);
    }
  }
  function approveOperatorTriageEntry(entry: OperatorTriageEntry | null | undefined) {
    if (!entry?.approval) return;
    void handleApprovalDecision(entry.approval, "approve");
  }
  function openOperatorTriageThread(entry: OperatorTriageEntry | null | undefined) {
    if (!entry?.threadId || !canOpenLedgerThread(entry.threadId, sessionId, knownSessionIds)) return;
    void openThread(entry.threadId);
  }
  function redirectOperatorWorkflowEntry(entry: OperatorTriageEntry | null | undefined) {
    if (!entry?.workflow) return;
    queueComposerDraft(buildWorkflowRedirectDraft(entry.workflow));
  }
  function inspectOperatorEvidenceEntry(entry: OperatorEvidenceEntry | null | undefined) {
    if (!entry) return;
    if (entry.approval) {
      setSelectedInspector({ kind: "approval", approval: entry.approval });
      return;
    }
    if (entry.artifact) {
      setSelectedInspector({ kind: "artifact", artifact: entry.artifact });
      return;
    }
    if (entry.trace) {
      setSelectedInspector({ kind: "trace", message: entry.trace });
    }
  }
  function draftOperatorEvidenceEntry(entry: OperatorEvidenceEntry | null | undefined) {
    if (!entry) return;
    if (entry.approval?.resume_message) {
      void queueThreadDraft(entry.approval.resume_message, entry.threadId ?? undefined);
      return;
    }
    if (entry.artifact) {
      const workflow = compatibleArtifactWorkflows(
        entry.artifact.filePath,
        undefined,
        entry.workflow ? [entry.workflow.workflowName, entry.workflow.toolName] : [],
      )[0];
      if (workflow) {
        queueArtifactWorkflowDraft(workflow, entry.artifact.filePath);
        return;
      }
      queueComposerDraft(`Use the workspace file "${entry.artifact.filePath}" as context for the next action.`);
    }
  }
  useEffect(() => {
    const handleOperatorShortcut = (event: KeyboardEvent) => {
      const key = event.key.toLowerCase();
      if ((event.ctrlKey || event.metaKey) && key === "k") {
        event.preventDefault();
        setPaletteOpen(true);
        return;
      }
      if (paletteOpen || studioOpen || windowsMenuOpen || isEditableTarget(event.target)) return;
      if (!event.shiftKey || event.altKey || event.ctrlKey || event.metaKey) return;
      if (key === "k") {
        event.preventDefault();
        setPaletteOpen(true);
      } else if (key === "i") {
        event.preventDefault();
        inspectOperatorTriageEntry(primaryTriageEntry);
      } else if (key === "a") {
        event.preventDefault();
        approveOperatorTriageEntry(primaryApprovalTriageEntry);
      } else if (key === "c") {
        event.preventDefault();
        continueOperatorTriageEntry(primaryTriageEntry);
      } else if (key === "o") {
        event.preventDefault();
        openOperatorTriageThread(primaryTriageEntry);
      } else if (key === "r") {
        event.preventDefault();
        redirectOperatorWorkflowEntry(primaryWorkflowTriageEntry);
      } else if (key === "e") {
        event.preventDefault();
        inspectOperatorEvidenceEntry(primaryEvidenceEntry);
      } else if (key === "w") {
        event.preventDefault();
        inspectPrimaryWorkflowEntry();
      } else if (key === "u") {
        event.preventDefault();
        queueWorkflowOutputContext(primaryWorkflowShortcutTarget());
      } else if (key === "p") {
        event.preventDefault();
        queueWorkflowFamilyPlan(primaryWorkflowShortcutTarget());
      }
    };
    window.addEventListener("keydown", handleOperatorShortcut);
    return () => window.removeEventListener("keydown", handleOperatorShortcut);
  }, [
    approveOperatorTriageEntry,
    continueOperatorTriageEntry,
    inspectOperatorEvidenceEntry,
    inspectOperatorTriageEntry,
    openOperatorTriageThread,
    paletteOpen,
    primaryApprovalTriageEntry,
    primaryEvidenceEntry,
    primaryTriageEntry,
    primaryWorkflowTriageEntry,
    inspectPrimaryWorkflowEntry,
    queueWorkflowFamilyPlan,
    primaryWorkflowShortcutTarget,
    queueWorkflowOutputContext,
    redirectOperatorWorkflowEntry,
    studioOpen,
    workflowRunsWithArtifacts,
    windowsMenuOpen,
  ]);
  const activitySpendByCapabilityFamily = useMemo(
    () => (activitySummary?.llm_cost_by_capability_family ?? []).slice(0, 3),
    [activitySummary],
  );
  const activitySpendByRuntimePath = useMemo(
    () => (activitySummary?.llm_cost_by_runtime_path ?? []).slice(0, 3),
    [activitySummary],
  );

  const matchPackageFile = useCallback(
    (
      extensionId: string | null | undefined,
      displayType: "skill" | "workflow" | "mcp_server",
      name: string,
      filePath: string | null | undefined,
    ): ExtensionStudioFileInfo | null => {
      if (!extensionId) return null;
      const extensionPackage = extensionPackagesById.get(extensionId);
      if (!extensionPackage) return null;
      return extensionPackage.studio_files.find((entry) => {
        if (entry.role !== "contribution" || entry.display_type !== displayType) return false;
        if (entry.name === name) return true;
        return typeof entry.resolved_path === "string" && !!filePath && entry.resolved_path === filePath;
      }) ?? null;
    },
    [extensionPackagesById],
  );

  const studioEntries = useMemo<ExtensionStudioEntry[]>(
    () => [
      ...workflows.map((workflow) => ({
        ...(() => {
          const packageFile = matchPackageFile(workflow.extension_id, "workflow", workflow.name, workflow.file_path);
          const extensionPackage = workflow.extension_id ? extensionPackagesById.get(workflow.extension_id) ?? null : null;
          return {
            extensionId: workflow.extension_id ?? null,
            packageReference: packageFile?.reference ?? null,
            packageDisplayName: extensionPackage?.display_name ?? null,
            packageVersion: extensionPackage?.version ?? null,
            packageLocation: extensionPackage?.location ?? null,
            packageTrust: extensionPackage?.trust ?? null,
            studioFormat: packageFile?.format ?? null,
            saveSupported: workflow.extension_id ? (packageFile?.save_supported ?? false) : true,
            validationSupported: workflow.extension_id ? (packageFile?.validation_supported ?? false) : true,
          };
        })(),
        id: `workflow:${workflow.name}`,
        entityType: "workflow_definition" as const,
        name: workflow.name,
        summary: workflow.description,
        availability: workflow.availability ?? (workflow.is_available === false ? "blocked" : "ready"),
        meta: `${workflow.risk_level} risk · ${workflow.step_count} steps`,
        entity: buildWorkflowDefinitionEntity(workflow),
      })),
      ...skills.map((skill) => ({
        id: `skill:${skill.name}`,
        entityType: "skill" as const,
        name: skill.name,
        summary: skill.description ?? "Skill capability",
        availability: skill.availability ?? (skill.enabled ? "ready" : "disabled"),
        meta: skill.user_invocable ? "invocable skill" : "support skill",
        entity: buildSkillEntity(skill),
        ...(() => {
          const packageFile = matchPackageFile(skill.extension_id, "skill", skill.name, skill.file_path ?? null);
          const extensionPackage = skill.extension_id ? extensionPackagesById.get(skill.extension_id) ?? null : null;
          return {
            extensionId: skill.extension_id ?? null,
            packageReference: packageFile?.reference ?? null,
            packageDisplayName: extensionPackage?.display_name ?? null,
            packageVersion: extensionPackage?.version ?? null,
            packageLocation: extensionPackage?.location ?? null,
            packageTrust: extensionPackage?.trust ?? null,
            studioFormat: packageFile?.format ?? null,
            saveSupported: skill.extension_id ? (packageFile?.save_supported ?? false) : true,
            validationSupported: skill.extension_id ? (packageFile?.validation_supported ?? false) : true,
          };
        })(),
      })),
      ...mcpServers.map((server) => ({
        ...(() => {
          const extensionPackage = server.extension_id ? extensionPackagesById.get(server.extension_id) ?? null : null;
          const packagedServer = server.source === "extension" && !!server.extension_id;
          return {
            extensionId: server.extension_id ?? null,
            packageReference: server.extension_reference ?? null,
            packageDisplayName: extensionPackage?.display_name ?? server.extension_display_name ?? null,
            packageVersion: extensionPackage?.version ?? null,
            packageLocation: extensionPackage?.location ?? null,
            packageTrust: extensionPackage?.trust ?? null,
            studioFormat: null,
            saveSupported: packagedServer ? false : true,
            validationSupported: packagedServer ? false : true,
          };
        })(),
        id: `mcp:${server.name}`,
        entityType: "mcp" as const,
        name: server.name,
        summary: server.description || server.url || "MCP server",
        availability: server.availability ?? server.status ?? "unknown",
        meta: `${server.status ?? "unknown"} · ${server.tool_count ?? 0} tools`,
        entity: buildMcpEntity(server),
      })),
      ...extensionPackages.map((extensionPackage) => {
        const manifestFile = extensionPackage.studio_files.find((entry) => entry.role === "manifest") ?? null;
        return {
          id: `extension:${extensionPackage.id}`,
          entityType: "extension_manifest" as const,
          name: extensionPackage.display_name,
          summary: extensionPackage.summary || extensionPackage.description || "Extension package manifest",
          availability: extensionPackage.status,
          meta: `${extensionPackage.location} · ${extensionPackage.trust} · ${extensionPackage.version ?? "unknown version"}`,
          entity: buildExtensionManifestEntity(extensionPackage),
          extensionId: extensionPackage.id,
          packageReference: manifestFile?.reference ?? null,
          packageDisplayName: extensionPackage.display_name,
          packageVersion: extensionPackage.version ?? null,
          packageLocation: extensionPackage.location,
          packageTrust: extensionPackage.trust,
          studioFormat: manifestFile?.format ?? "yaml",
          saveSupported: manifestFile?.save_supported ?? false,
          validationSupported: manifestFile?.validation_supported ?? false,
        };
      }),
    ],
    [extensionPackages, extensionPackagesById, matchPackageFile, mcpServers, skills, workflows],
  );
  const selectedStudioEntry = useMemo(
    () => studioEntries.find((entry) => entry.id === studioSelectedId) ?? studioEntries[0] ?? null,
    [studioEntries, studioSelectedId],
  );
  const selectedExtensionPackage = useMemo(
    () => (
      selectedStudioEntry?.extensionId
        ? extensionPackagesById.get(selectedStudioEntry.extensionId) ?? null
        : null
    ),
    [extensionPackagesById, selectedStudioEntry?.extensionId],
  );
  const studioMcpReadOnly = selectedStudioEntry?.entityType === "mcp" && !!selectedStudioEntry.extensionId;
  const selectedExtensionToggleAction = useMemo(() => {
    if (!selectedExtensionPackage) return null;
    const currentlyEnabled = selectedExtensionPackage.enabled !== false;
    const toggleSupported = currentlyEnabled
      ? selectedExtensionPackage.disable_supported
      : selectedExtensionPackage.enable_supported;
    if (!toggleSupported) return null;
    const scopeLabel =
      selectedExtensionPackage.enabled_scope === "toggleable_contributions"
        ? "contributions"
        : "extension";
    return {
      nextEnabled: !currentlyEnabled,
      label: `${currentlyEnabled ? "Disable" : "Enable"} ${scopeLabel}`,
    };
  }, [selectedExtensionPackage]);
  useEffect(() => {
    studioSelectionRef.current = selectedStudioEntry?.id ?? null;
  }, [selectedStudioEntry?.id]);
  useEffect(() => {
    if (selectedStudioEntry?.entityType === "extension_manifest") {
      const nextSelection = selectedExtensionPackage?.id ?? null;
      const nextConfig =
        selectedExtensionPackage?.config && Object.keys(selectedExtensionPackage.config).length > 0
          ? selectedExtensionPackage.config
          : {};
      const nextDraft = JSON.stringify(nextConfig, null, 2);
      const selectionChanged = studioExtensionConfigSelectionRef.current !== nextSelection;
      studioExtensionConfigSelectionRef.current = nextSelection;
      if (selectionChanged || !studioExtensionConfigDirty) {
        setStudioExtensionConfigDraft(nextDraft);
        setStudioExtensionConfigDirty(false);
      }
      return;
    }
    studioExtensionConfigSelectionRef.current = null;
    setStudioExtensionConfigDirty(false);
    setStudioExtensionConfigDraft("{}");
  }, [selectedExtensionPackage?.config, selectedExtensionPackage?.id, selectedStudioEntry?.entityType, studioExtensionConfigDirty]);
  const studioRecommendedActions = useMemo(
    () => readActionList(selectedStudioEntry?.entity.details.recommended_actions),
    [selectedStudioEntry],
  );
  const studioPackagePreviewIssueCount = useMemo(() => {
    if (!studioPackagePreview || !Array.isArray(studioPackagePreview.results)) return 0;
    return studioPackagePreview.results.reduce((count, result) => {
      const issues = Array.isArray(result?.issues) ? result.issues.length : 0;
      return count + issues;
    }, 0);
  }, [studioPackagePreview]);
  const studioPackagePreviewHasLoadErrors = useMemo(
    () => Array.isArray(studioPackagePreview?.load_errors) && studioPackagePreview.load_errors.length > 0,
    [studioPackagePreview],
  );
  const studioPackagePreviewActionable = useMemo(
    () => Boolean(studioPackagePreview?.ok)
      && !studioPackagePreviewHasLoadErrors
      && studioPackagePreviewIssueCount === 0,
    [studioPackagePreview, studioPackagePreviewHasLoadErrors, studioPackagePreviewIssueCount],
  );
  const studioPackageAction = useMemo(() => {
    const lifecyclePlan = studioPackagePreview?.lifecycle_plan;
    const recommendedAction = lifecyclePlan?.recommended_action ?? "install";
    if (recommendedAction === "update") {
      return {
        key: "update",
        label: "Update package",
        disabled: !studioPackagePreviewActionable,
      };
    }
    if (recommendedAction === "none") {
      return {
        key: "none",
        label: "Up to date",
        disabled: true,
      };
    }
    return {
      key: "install",
      label: "Install package",
      disabled: !studioPackagePreviewActionable,
    };
  }, [studioPackagePreview, studioPackagePreviewActionable]);
  const studioSidebarSections = useMemo(() => {
    const grouped = new Map<string, ExtensionStudioEntry[]>();
    extensionPackages.forEach((extensionPackage) => {
      grouped.set(extensionPackage.id, []);
    });
    const standalone: ExtensionStudioEntry[] = [];
    studioEntries.forEach((entry) => {
      if (entry.extensionId && grouped.has(entry.extensionId)) {
        grouped.get(entry.extensionId)?.push(entry);
      } else {
        standalone.push(entry);
      }
    });
    const sortEntries = (entries: ExtensionStudioEntry[]) => entries.slice().sort((left, right) => {
      if (left.entityType === "extension_manifest" && right.entityType !== "extension_manifest") return -1;
      if (right.entityType === "extension_manifest" && left.entityType !== "extension_manifest") return 1;
      return left.name.localeCompare(right.name);
    });
    return {
      packages: extensionPackages
        .map((extensionPackage) => ({
          extension: extensionPackage,
          entries: sortEntries(grouped.get(extensionPackage.id) ?? []),
        }))
        .filter((group) => group.entries.length > 0),
      standalone: sortEntries(standalone),
    };
  }, [extensionPackages, studioEntries]);

  useEffect(() => {
    if (!studioOpen) return;
    if (!studioSelectedId && studioEntries[0]) {
      setStudioSelectedId(studioEntries[0].id);
      return;
    }
    if (studioSelectedId && !studioEntries.some((entry) => entry.id === studioSelectedId)) {
      setStudioSelectedId(studioEntries[0]?.id ?? null);
    }
  }, [studioEntries, studioOpen, studioSelectedId]);
  const studioWorkflowErrors = useMemo(() => {
    if (!studioWorkflowDiagnostics || !selectedStudioEntry) return [];
    const filePath = typeof selectedStudioEntry.entity.details.file_path === "string"
      ? selectedStudioEntry.entity.details.file_path
      : null;
    const errors = Array.isArray(studioWorkflowDiagnostics.load_errors)
      ? studioWorkflowDiagnostics.load_errors
      : [];
    if (!filePath) return errors;
    return errors.filter((entry) => entry.file_path === filePath || entry.message.includes(selectedStudioEntry.name));
  }, [selectedStudioEntry, studioWorkflowDiagnostics]);
  useEffect(() => {
    if (!studioOpen || !selectedStudioEntry) return;
    const entryId = selectedStudioEntry.id;
    const requestId = ++studioLoadRequestRef.current;
    let cancelled = false;
    const fallbackDraft = buildExtensionStudioDraft(selectedStudioEntry.entity);
    setStudioDraft(fallbackDraft);
    setStudioStatus(null);
    setStudioPreflight(null);
    setStudioWorkflowDiagnostics(null);
    setStudioDraftValidation(null);
    setStudioMcpTestResult(null);
    setStudioMcpUrl(typeof selectedStudioEntry.entity.details.url === "string" ? selectedStudioEntry.entity.details.url : "");
    setStudioMcpDescription(
      typeof selectedStudioEntry.entity.details.description === "string"
        ? selectedStudioEntry.entity.details.description
        : "",
    );
    if (selectedStudioEntry.entityType === "mcp" && selectedStudioEntry.extensionId) {
      setStudioStatus("Packaged MCP definitions are read-only here; use connector test/toggle controls or update the package itself.");
    }
    const loadStudioSource = async () => {
      try {
        if (selectedStudioEntry.extensionId && selectedStudioEntry.packageReference && selectedStudioEntry.entityType !== "mcp") {
          const response = await fetch(
            `${API_URL}/api/extensions/${encodeURIComponent(selectedStudioEntry.extensionId)}/source?reference=${encodeURIComponent(selectedStudioEntry.packageReference)}`,
          );
          const payload = await response.json().catch(() => null);
          if (
            !cancelled
            && requestId === studioLoadRequestRef.current
            && studioSelectionRef.current === entryId
            && response.ok
            && typeof payload?.content === "string"
          ) {
            setStudioDraft(payload.content);
            setStudioDraftValidation(
              payload.validation && typeof payload.validation === "object"
                ? payload.validation as Record<string, unknown>
                : null,
            );
            if (selectedStudioEntry.entityType === "extension_manifest") {
              setStudioStatus(`${selectedStudioEntry.packageDisplayName ?? selectedStudioEntry.name} manifest loaded`);
            }
          }
          return;
        }
        if (selectedStudioEntry.extensionId && selectedStudioEntry.entityType !== "mcp") {
          if (!cancelled && requestId === studioLoadRequestRef.current && studioSelectionRef.current === entryId) {
            setStudioDraftValidation(null);
            setStudioStatus(`Reload extension package metadata before editing ${selectedStudioEntry.name}`);
          }
          return;
        }
        if (selectedStudioEntry.entityType === "workflow_definition") {
          const response = await fetch(`${API_URL}/api/workflows/${encodeURIComponent(selectedStudioEntry.name)}/source`);
          const payload = await response.json().catch(() => null);
          if (
            !cancelled
            && requestId === studioLoadRequestRef.current
            && studioSelectionRef.current === entryId
            && response.ok
            && typeof payload?.content === "string"
          ) {
            setStudioDraft(payload.content);
            setStudioDraftValidation(payload && typeof payload === "object" ? payload : null);
          }
          return;
        }
        if (selectedStudioEntry.entityType === "skill") {
          const response = await fetch(`${API_URL}/api/skills/${encodeURIComponent(selectedStudioEntry.name)}/source`);
          const payload = await response.json().catch(() => null);
          if (
            !cancelled
            && requestId === studioLoadRequestRef.current
            && studioSelectionRef.current === entryId
            && response.ok
            && typeof payload?.content === "string"
          ) {
            setStudioDraft(payload.content);
            setStudioDraftValidation(payload && typeof payload === "object" ? payload : null);
          }
        }
      } catch {
        if (!cancelled && requestId === studioLoadRequestRef.current && studioSelectionRef.current === entryId) {
          setStudioStatus(`Using generated draft for ${selectedStudioEntry.name}`);
        }
      }
    };
    void loadStudioSource();
    return () => {
      cancelled = true;
    };
  }, [selectedStudioEntry?.id, studioOpen]);
  const paletteItems = useMemo(() => {
    const query = paletteQuery.trim().toLowerCase();
    const items: Array<{
      id: string;
      kind: string;
      label: string;
      detail: string;
      action?: CapabilityAction | null;
      draft?: string | null;
      entity?: OperatorEntity | null;
    }> = [];

    capabilityRecommendations.forEach((item) => {
      items.push({
        id: `recommendation:${item.id}`,
        kind: "recommendation",
        label: item.label,
        detail: item.description,
        action: item.action ?? null,
      });
    });
    operatorRunbooks.forEach((item) => {
      items.push({
        id: `runbook:${item.id}`,
        kind: "runbook",
        label: item.label,
        detail: `${item.availability ?? "unknown"} · ${item.description}${item.blocking_reasons?.length ? ` · ${item.blocking_reasons.join(", ")}` : ""}`,
        action: item.action ?? null,
        draft: item.command,
      });
    });
    operatorMacros.forEach((item) => {
      items.push({
        id: `macro:${item.id}`,
        kind: "macro",
        label: item.label,
        detail: `saved runbook · ${item.availability ?? "unknown"} · ${item.description}`,
        action: item.action ?? null,
        draft: item.command,
      });
    });
    starterPacks.forEach((pack) => {
      items.push({
        id: `starter-pack:${pack.name}`,
        kind: "starter pack",
        label: pack.label,
        detail: `${pack.availability} · ${pack.description}${pack.blocked_skills[0] ? ` · blocked skill ${pack.blocked_skills[0].name}` : pack.blocked_workflows[0] ? ` · blocked workflow ${pack.blocked_workflows[0].name}` : ""}`,
        action: { type: "activate_starter_pack", label: "Activate pack", name: pack.name },
      });
    });
    workflows.forEach((workflow) => {
      items.push({
        id: `workflow:${workflow.name}`,
        kind: "workflow",
        label: workflow.name,
        detail: `${workflow.is_available === false ? "blocked" : "ready"} · ${workflow.description}${workflow.missing_tools?.length ? ` · tools ${workflow.missing_tools.join(", ")}` : ""}${workflow.missing_skills?.length ? ` · skills ${workflow.missing_skills.join(", ")}` : ""}`,
        action: workflow.is_available === false
          ? workflow.missing_skills?.[0]
            ? { type: "open_settings", label: "Open settings", target: "workflows" }
            : null
          : { type: "draft_workflow", label: "Draft workflow", name: workflow.name },
      });
    });
    skills.forEach((skill) => {
      items.push({
        id: `skill:${skill.name}`,
        kind: "skill",
        label: skill.name,
        detail: `${skill.availability ?? (skill.enabled ? "ready" : "disabled")} · ${skill.description ?? ""}`.trim(),
        action: skill.recommended_actions?.[0] ?? null,
      });
    });
    mcpServers.forEach((server) => {
      items.push({
        id: `mcp:${server.name}`,
        kind: "mcp",
        label: server.name,
        detail: `${server.availability ?? "unknown"} · ${server.status ?? "unknown"}`,
        action: server.recommended_actions?.[0] ?? { type: "test_mcp_server", label: "Test connection", name: server.name },
      });
    });
    tools.forEach((tool) => {
      items.push({
        id: `tool:${tool.name}`,
        kind: "tool",
        label: tool.name,
        detail: `${tool.availability ?? "unknown"} · ${tool.description ?? ""}`.trim(),
        action: tool.recommended_actions?.[0] ?? null,
      });
    });
    catalogItems.forEach((item) => {
      if (item.installed && !item.update_available) return;
      if (item.type === "extension_pack" && item.status && item.status !== "ready") return;
      const itemKind = item.type === "extension_pack"
        ? (item.installed && item.update_available ? "update pack" : "install pack")
        : `install ${item.type}`;
      const detailParts = [item.description];
      if (item.missing_tools?.length) detailParts.push(`missing tools ${item.missing_tools.join(", ")}`);
      if (item.type === "extension_pack" && item.contribution_types?.length) {
        detailParts.push(item.contribution_types.join(", "));
      }
      items.push({
        id: `catalog:${item.type}:${item.name}`,
        kind: itemKind,
        label: item.name,
        detail: detailParts.filter(Boolean).join(" · "),
        action: item.recommended_actions?.[0] ?? { type: "install_catalog_item", label: item.installed && item.update_available ? "Update" : "Install", name: item.catalog_id ?? item.name },
      });
    });

    if (!query) return items.slice(0, 40);
    return items.filter((item) => `${item.kind} ${item.label} ${item.detail}`.toLowerCase().includes(query)).slice(0, 40);
  }, [
    capabilityRecommendations,
    operatorRunbooks,
    starterPacks,
    workflows,
    skills,
    mcpServers,
    tools,
    catalogItems,
    operatorMacros,
    paletteQuery,
  ]);

  function approvalForWorkflow(workflow: WorkflowRunRecord): PendingApproval | null {
    if (workflow.pendingApprovalIds?.length) {
      const byId = pendingApprovals.find((approval) => workflow.pendingApprovalIds?.includes(approval.id));
      if (byId) return byId;
    }
    const fromSidebar = pendingApprovals.find((approval) =>
      approval.tool_name === workflow.toolName
      && approval.session_id === workflow.sessionId,
    );
    if (fromSidebar) return fromSidebar;

    const attached = workflow.pendingApprovals?.[0];
    if (!attached) return null;
    return {
      id: attached.id,
      session_id: workflow.sessionId ?? null,
      thread_id: attached.threadId ?? workflow.threadId ?? workflow.sessionId ?? null,
      thread_label: attached.threadLabel ?? workflow.threadLabel ?? null,
      tool_name: workflow.toolName,
      risk_level: attached.riskLevel ?? workflow.riskLevel ?? "unknown",
      status: "pending",
      summary: attached.summary,
      created_at: attached.createdAt,
      resume_message: attached.resumeMessage ?? workflow.approvalRecoveryMessage ?? null,
    };
  }

  function interventionsForWorkflow(workflow: WorkflowRunRecord): GuardianContinuityIntervention[] {
    if (!workflow.sessionId || workflow.sessionId !== sessionId) return [];
    return recentInterventions.filter((intervention) => intervention.session_id === workflow.sessionId);
  }

  function studioEntryForWorkflowRun(workflow: WorkflowRunRecord): ExtensionStudioEntry | null {
    return studioEntries.find(
      (entry) => entry.entityType === "workflow_definition" && entry.name === workflow.workflowName,
    ) ?? null;
  }

  function appendOperatorFeed(summary: string, status: "info" | "success" | "failed") {
    setOperatorStatus(
      status === "failed"
        ? `Action failed: ${summary}`
        : status === "success"
          ? summary
          : `Action recorded: ${summary}`,
    );
  }

  async function handleExtensionLifecycleFailure(
    payload: unknown,
    fallbackMessage: string,
    setStatus: (value: string) => void,
  ): Promise<boolean> {
    const approvalDetail = normalizeExtensionLifecycleApprovalDetail(payload);
    if (approvalDetail) {
      setPendingLifecycleApprovalId(approvalDetail.approval_id || null);
      setStatus(`${approvalDetail.message} Review Pending approvals, then retry.`);
      focusPane("approvals_pane");
      await refreshCockpit();
      appendOperatorFeed(
        `${approvalDetail.tool_name} requires ${approvalDetail.risk_level} approval`,
        "info",
      );
      return true;
    }

    const detail = payload && typeof payload === "object" && !Array.isArray(payload)
      ? (payload as { detail?: unknown }).detail
      : null;
    setStatus(typeof detail === "string" ? detail : fallbackMessage);
    return false;
  }

  function rememberDoctorPlan(plan: Omit<DoctorPlanRecord, "id" | "createdAt">) {
    const next: DoctorPlanRecord = {
      ...plan,
      id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
      createdAt: new Date().toISOString(),
    };
    setDoctorPlans((current) => [next, ...current].slice(0, 6));
  }

  function openExtensionStudio(entry: ExtensionStudioEntry | null) {
    if (!entry) return;
    setStudioSelectedId(entry.id);
    setStudioOpen(true);
  }

  function openExtensionStudioForEntity(entity: OperatorEntity | null) {
    if (!entity) return;
    const match = studioEntries.find(
      (entry) => entry.entityType === entity.entityType && entry.name === entity.name,
    );
    if (match) {
      openExtensionStudio(match);
      return;
    }
    if (entity.entityType === "workflow_definition" || entity.entityType === "skill" || entity.entityType === "mcp") {
      setStudioSelectedId(`${entity.entityType === "workflow_definition" ? "workflow" : entity.entityType}:${entity.name}`);
      setStudioOpen(true);
    }
  }

  async function refreshStudioValidation() {
    if (!selectedStudioEntry) return;
    const entry = selectedStudioEntry;
    const entryId = entry.id;
    const fileName = managedFileName(entry.entity.details.file_path);
    const requestId = ++studioValidationRequestRef.current;
    setStudioBusy("validation");
    setStudioStatus(`Validating ${entry.name}...`);
    try {
      if (entry.entityType === "extension_manifest" && entry.extensionId && entry.packageReference) {
        const response = await fetch(
          `${API_URL}/api/extensions/${encodeURIComponent(entry.extensionId)}/source?reference=${encodeURIComponent(entry.packageReference)}`,
        );
        const payload = await response.json().catch(() => null);
        if (requestId !== studioValidationRequestRef.current || studioSelectionRef.current !== entryId) return;
        setStudioDraftValidation(
          payload?.validation && typeof payload.validation === "object"
            ? payload.validation as Record<string, unknown>
            : null,
        );
        if (!response.ok) {
          setStudioStatus(typeof payload?.detail === "string" ? payload.detail : `Failed to validate ${entry.name}`);
        } else {
          setStudioStatus(`${entry.packageDisplayName ?? entry.name} manifest is valid`);
        }
        return;
      }

      if (entry.entityType === "workflow_definition") {
        const [validationResponse, preflightResponse, diagnosticsResponse] = await Promise.all([
          fetch(`${API_URL}/api/workflows/validate`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ content: studioDraft, file_name: fileName }),
          }),
          fetch(
            `${API_URL}/api/capabilities/preflight?target_type=workflow&name=${encodeURIComponent(entry.name)}`,
          ),
          fetch(`${API_URL}/api/workflows/diagnostics`),
        ]);

        const validationPayload = await validationResponse.json().catch(() => null);
        const preflightPayload = preflightResponse.ok
          ? await preflightResponse.json() as CapabilityPreflightResponse
          : null;
        const diagnosticsPayload = diagnosticsResponse.ok
          ? await diagnosticsResponse.json() as WorkflowDiagnosticsPayload
          : null;
        if (requestId !== studioValidationRequestRef.current || studioSelectionRef.current !== entryId) return;
        setStudioDraftValidation(validationPayload && typeof validationPayload === "object" ? validationPayload : null);
        setStudioPreflight(preflightPayload);
        setStudioWorkflowDiagnostics(diagnosticsPayload);
        setStudioStatus(
          validationPayload?.valid === false
            ? `${entry.name} has draft validation errors`
            : preflightPayload?.ready
            ? `${entry.name} is runtime-ready`
            : `${entry.name} still has runtime blockers`,
        );
        return;
      }

      if (entry.entityType === "skill") {
        const response = await fetch(`${API_URL}/api/skills/validate`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ content: studioDraft, file_name: fileName }),
        });
        const payload = await response.json().catch(() => null);
        if (requestId !== studioValidationRequestRef.current || studioSelectionRef.current !== entryId) return;
        setStudioDraftValidation(payload && typeof payload === "object" ? payload : null);
        if (!response.ok) {
          setStudioStatus(typeof payload?.detail === "string" ? payload.detail : `Failed to validate ${entry.name}`);
        } else if (payload?.valid === false) {
          setStudioStatus(`${entry.name} has draft validation errors`);
        } else if (Array.isArray(payload?.missing_tools) && payload.missing_tools.length > 0) {
          setStudioStatus(`${entry.name} saves cleanly but still needs runtime tools`);
        } else {
          setStudioStatus(`${entry.name} is valid and runtime-ready`);
        }
        return;
      }

      if (entry.entityType === "mcp") {
        const response = await fetch(`${API_URL}/api/mcp/servers/validate`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            name: entry.name,
            url: studioMcpUrl.trim(),
            description: studioMcpDescription.trim(),
            enabled: Boolean(entry.entity.details.enabled ?? true),
            headers:
              entry.entity.details.headers && typeof entry.entity.details.headers === "object"
                ? entry.entity.details.headers
                : null,
            auth_hint:
              typeof entry.entity.details.auth_hint === "string"
                ? entry.entity.details.auth_hint
                : "",
          }),
        });
        const payload = await response.json().catch(() => null);
        if (requestId !== studioValidationRequestRef.current || studioSelectionRef.current !== entryId) return;
        setStudioMcpTestResult(payload && typeof payload === "object" ? payload : null);
        if (!response.ok) {
          setStudioStatus(
            typeof payload?.detail === "string" ? payload.detail : `${entry.name} test failed`,
          );
        } else if (payload?.valid === false) {
          const issues = Array.isArray(payload?.issues)
            ? payload.issues.filter((item: unknown): item is string => typeof item === "string")
            : [];
          setStudioStatus(
            issues.length > 0
              ? issues[0]
              : `${entry.name} config has validation issues`,
          );
        } else {
          const warnings = Array.isArray(payload?.warnings)
            ? payload.warnings.filter((item: unknown): item is string => typeof item === "string")
            : [];
          setStudioStatus(
            payload?.status === "auth_required"
              ? `${entry.name} config is valid but still needs auth`
              : warnings.length > 0
                ? warnings[0]
                : `${entry.name} config is ready to test`,
          );
        }
        return;
      }
    } catch {
      if (requestId === studioValidationRequestRef.current && studioSelectionRef.current === entryId) {
        setStudioStatus(`Failed to validate ${entry.name}`);
      }
    } finally {
      if (requestId === studioValidationRequestRef.current && studioSelectionRef.current === entryId) {
        setStudioBusy(null);
      }
    }
  }

  async function saveStudioDraft() {
    if (!selectedStudioEntry || selectedStudioEntry.entityType === "mcp") {
      await saveStudioMcpConfig();
      return;
    }
    const entry = selectedStudioEntry;
    if (entry.extensionId && !entry.packageReference) {
      setStudioStatus(`Reload extension packages before saving ${entry.name}`);
      return;
    }
    const entryId = entry.id;
    const fileName = managedFileName(entry.entity.details.file_path);
    const requestId = ++studioSaveRequestRef.current;
    setStudioBusy("save");
    setStudioStatus(`Saving ${entry.name}...`);
    try {
      if (entry.extensionId && entry.packageReference) {
        const response = await fetch(`${API_URL}/api/extensions/${encodeURIComponent(entry.extensionId)}/source`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            reference: entry.packageReference,
            content: studioDraft,
          }),
        });
        const payload = await response.json().catch(() => null);
        if (!response.ok) {
          if (requestId === studioSaveRequestRef.current && studioSelectionRef.current === entryId) {
            setStudioStatus(
              typeof payload?.detail === "string"
                ? payload.detail
                : `Failed to save ${entry.name}`,
            );
          }
          return;
        }
        await refreshCockpit();
        if (requestId !== studioSaveRequestRef.current || studioSelectionRef.current !== entryId) return;
        setStudioDraftValidation(
          payload?.validation && typeof payload.validation === "object"
            ? payload.validation as Record<string, unknown>
            : null,
        );
        setStudioStatus(
          entry.entityType === "extension_manifest"
            ? `${entry.packageDisplayName ?? entry.name} manifest saved`
            : `${entry.name} saved`,
        );
        appendOperatorFeed(
          entry.entityType === "extension_manifest"
            ? `${entry.packageDisplayName ?? entry.name} manifest saved`
            : `${entry.name} saved`,
          "success",
        );
        return;
      }

      const endpoint = entry.entityType === "workflow_definition"
        ? `${API_URL}/api/workflows/save`
        : `${API_URL}/api/skills/save`;
      const response = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          content: studioDraft,
          file_name: fileName,
        }),
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        const detail = payload?.detail;
        if (requestId === studioSaveRequestRef.current && studioSelectionRef.current === entryId) {
          setStudioStatus(
            typeof detail === "string"
              ? detail
              : `Failed to save ${entry.name}`,
          );
          if (detail && typeof detail === "object") {
            setStudioDraftValidation(detail as Record<string, unknown>);
          }
        }
        return;
      }
      await refreshCockpit();
      if (requestId !== studioSaveRequestRef.current || studioSelectionRef.current !== entryId) return;
      await refreshStudioValidation();
      if (requestId !== studioSaveRequestRef.current || studioSelectionRef.current !== entryId) return;
      setStudioStatus(`${entry.name} saved`);
      appendOperatorFeed(`${entry.name} saved`, "success");
    } catch {
      if (requestId === studioSaveRequestRef.current && studioSelectionRef.current === entryId) {
        setStudioStatus(`Failed to save ${entry.name}`);
      }
    } finally {
      if (requestId === studioSaveRequestRef.current && studioSelectionRef.current === entryId) {
        setStudioBusy(null);
      }
    }
  }

  async function saveStudioMcpConfig() {
    if (!selectedStudioEntry || selectedStudioEntry.entityType !== "mcp") return;
    const entry = selectedStudioEntry;
    const entryId = entry.id;
    const requestId = ++studioSaveRequestRef.current;
    setStudioBusy("save");
    setStudioStatus(`Saving ${entry.name}...`);
    try {
      const response = await fetch(`${API_URL}/api/mcp/servers/${entry.name}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url: studioMcpUrl.trim(),
          description: studioMcpDescription.trim(),
        }),
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        if (requestId === studioSaveRequestRef.current && studioSelectionRef.current === entryId) {
          setStudioStatus(
            typeof payload?.detail === "string" ? payload.detail : `Failed to save ${entry.name}`,
          );
        }
        return;
      }
      await refreshCockpit();
      if (requestId !== studioSaveRequestRef.current || studioSelectionRef.current !== entryId) return;
      setStudioStatus(`${entry.name} config saved`);
      appendOperatorFeed(`${entry.name} config saved`, "success");
    } catch {
      if (requestId === studioSaveRequestRef.current && studioSelectionRef.current === entryId) {
        setStudioStatus(`Failed to save ${entry.name}`);
      }
    } finally {
      if (requestId === studioSaveRequestRef.current && studioSelectionRef.current === entryId) {
        setStudioBusy(null);
      }
    }
  }

  async function validateStudioExtensionPath() {
    const path = studioExtensionPath.trim();
    if (!path) {
      setStudioPackageStatus("Enter a local extension package path first");
      return;
    }
    setStudioBusy("extension-validate");
    setStudioPackagePreview(null);
    setStudioPackageStatus(`Validating ${path}...`);
    try {
      const response = await fetch(`${API_URL}/api/extensions/validate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path }),
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        setStudioPackageStatus(
          typeof payload?.detail === "string"
            ? payload.detail
            : `Failed to validate ${path}`,
        );
        return;
      }
      const preview = payload as ExtensionPathPreview;
      setStudioPackagePreview(preview);
      const issueCount = Array.isArray(payload?.results)
        ? payload.results.reduce((count: number, result: unknown) => {
          if (!result || typeof result !== "object" || Array.isArray(result)) return count;
          const issues = Array.isArray((result as { issues?: unknown }).issues)
            ? ((result as { issues?: unknown }).issues as unknown[]).length
            : 0;
          return count + issues;
        }, 0)
        : 0;
      const loadErrorCount = Array.isArray(payload?.load_errors) ? payload.load_errors.length : 0;
      const lifecyclePlan = preview.lifecycle_plan;
      const packageLabel = payload?.display_name ?? payload?.extension_id ?? path;
      const currentVersion = lifecyclePlan?.current_version;
      const candidateVersion = lifecyclePlan?.candidate_version ?? payload?.version;
      const location = lifecyclePlan?.current_location;
      const validationFailureSummary = [
        issueCount > 0 ? `${issueCount} doctor issue${issueCount === 1 ? "" : "s"}` : null,
        loadErrorCount > 0 ? `${loadErrorCount} load error${loadErrorCount === 1 ? "" : "s"}` : null,
      ].filter(Boolean).join(" and ");
      setStudioPackageStatus(
        issueCount > 0 || loadErrorCount > 0 || payload?.ok === false
          ? `${packageLabel} failed validation${validationFailureSummary ? ` with ${validationFailureSummary}` : ""}`
          : lifecyclePlan?.recommended_action === "update"
            ? `${packageLabel} can update ${currentVersion ?? "installed package"} -> ${candidateVersion ?? "candidate"}`
            : lifecyclePlan?.mode === "workspace_override"
              ? `${packageLabel} will install as a workspace override for the ${location ?? "bundled"} package`
              : lifecyclePlan?.recommended_action === "none"
                ? `${packageLabel} is already up to date`
                : `${packageLabel} is valid and installable`,
      );
    } catch {
      setStudioPackagePreview(null);
      setStudioPackageStatus(`Failed to validate ${path}`);
    } finally {
      setStudioBusy(null);
    }
  }

  async function scaffoldStudioSkillPack() {
    const packageName = studioScaffoldName.trim();
    const displayName = studioScaffoldDisplayName.trim() || packageName;
    if (!packageName) {
      setStudioPackageStatus("Enter a package slug before scaffolding");
      return;
    }
    setStudioBusy("extension-scaffold");
    setStudioPackagePreview(null);
    setStudioPackageStatus(`Scaffolding ${displayName}...`);
    try {
      const response = await fetch(`${API_URL}/api/extensions/scaffold`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          package_name: packageName,
          display_name: displayName,
          kind: "capability-pack",
          contributions: ["skills"],
        }),
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        setStudioPackageStatus(
          typeof payload?.detail === "string"
            ? payload.detail
            : `Failed to scaffold ${displayName}`,
        );
        return;
      }
      const scaffold = payload as ExtensionScaffoldResponse;
      setStudioExtensionPath(scaffold.path);
      setStudioPackagePreview(scaffold.preview);
      const scaffoldLabel = scaffold.preview.display_name ?? displayName;
      const invalidScaffold = scaffold.status !== "scaffolded" || scaffold.preview.ok === false;
      if (invalidScaffold) {
        const issueCount = Array.isArray(scaffold.preview.results)
          ? scaffold.preview.results.reduce((count, result) => count + (Array.isArray(result.issues) ? result.issues.length : 0), 0)
          : 0;
        setStudioPackageStatus(
          `${scaffoldLabel} scaffolded but needs fixes${issueCount > 0 ? ` (${issueCount} issue${issueCount === 1 ? "" : "s"})` : ""}`,
        );
        appendOperatorFeed(
          `Scaffolded extension package needs fixes: ${scaffoldLabel}`,
          "info",
        );
        return;
      }
      setStudioPackageStatus(
        `${scaffoldLabel} scaffolded with ${scaffold.created_files.length} file${scaffold.created_files.length === 1 ? "" : "s"}`,
      );
      appendOperatorFeed(`Scaffolded extension package: ${scaffoldLabel}`, "success");
    } catch {
      setStudioPackageStatus(`Failed to scaffold ${displayName}`);
    } finally {
      setStudioBusy(null);
    }
  }

  async function installStudioExtensionPath() {
    const path = studioExtensionPath.trim();
    if (!path) {
      setStudioPackageStatus("Enter a local extension package path first");
      return;
    }
    setStudioBusy("extension-install");
    setStudioPackageStatus(`Installing ${path}...`);
    try {
      const response = await fetch(`${API_URL}/api/extensions/install`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path }),
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        await handleExtensionLifecycleFailure(payload, `Failed to install ${path}`, setStudioPackageStatus);
        return;
      }
      await refreshCockpit();
      setStudioPackagePreview(null);
      const extensionId = typeof payload?.extension?.id === "string" ? payload.extension.id : null;
      if (extensionId) {
        setStudioSelectedId(`extension:${extensionId}`);
      }
      setStudioPackageStatus(
        `${payload?.extension?.display_name ?? extensionId ?? path} installed`,
      );
      appendOperatorFeed(
        `Installed extension package: ${payload?.extension?.display_name ?? extensionId ?? path}`,
        "success",
      );
    } catch {
      setStudioPackageStatus(`Failed to install ${path}`);
    } finally {
      setStudioBusy(null);
    }
  }

  async function updateStudioExtensionPath() {
    const path = studioExtensionPath.trim();
    if (!path) {
      setStudioPackageStatus("Enter a local extension package path first");
      return;
    }
    setStudioBusy("extension-update");
    setStudioPackageStatus(`Updating ${path}...`);
    try {
      const response = await fetch(`${API_URL}/api/extensions/update`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path }),
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        await handleExtensionLifecycleFailure(payload, `Failed to update ${path}`, setStudioPackageStatus);
        return;
      }
      await refreshCockpit();
      setStudioPackagePreview(null);
      const extensionId = typeof payload?.extension?.id === "string" ? payload.extension.id : null;
      if (extensionId) {
        setStudioSelectedId(`extension:${extensionId}`);
      }
      setStudioPackageStatus(
        `${payload?.extension?.display_name ?? extensionId ?? path} updated`,
      );
      appendOperatorFeed(
        `Updated extension package: ${payload?.extension?.display_name ?? extensionId ?? path}`,
        "success",
      );
    } catch {
      setStudioPackageStatus(`Failed to update ${path}`);
    } finally {
      setStudioBusy(null);
    }
  }

  async function setSelectedExtensionEnabled(enabled: boolean) {
    const extensionId = selectedExtensionPackage?.id;
    if (!extensionId) return;
    setStudioBusy(enabled ? "extension-enable" : "extension-disable");
    setStudioStatus(`${enabled ? "Enabling" : "Disabling"} ${selectedExtensionPackage.display_name}...`);
    try {
      const response = await fetch(
        `${API_URL}/api/extensions/${encodeURIComponent(extensionId)}/${enabled ? "enable" : "disable"}`,
        { method: "POST" },
      );
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        await handleExtensionLifecycleFailure(
          payload,
          `Failed to ${enabled ? "enable" : "disable"} ${selectedExtensionPackage.display_name}`,
          setStudioStatus,
        );
        return;
      }
      await refreshCockpit();
      setStudioStatus(
        `${selectedExtensionPackage.display_name} ${enabled ? "enabled" : "disabled"}`,
      );
      appendOperatorFeed(
        `${selectedExtensionPackage.display_name} ${enabled ? "enabled" : "disabled"}`,
        "success",
      );
    } catch {
      setStudioStatus(`Failed to ${enabled ? "enable" : "disable"} ${selectedExtensionPackage.display_name}`);
    } finally {
      setStudioBusy(null);
    }
  }

  async function saveSelectedExtensionMetadata() {
    const extensionId = selectedExtensionPackage?.id;
    if (!extensionId) return;
    let configPayload: Record<string, unknown>;
    try {
      const parsed = JSON.parse(studioExtensionConfigDraft);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        setStudioStatus("Extension metadata must be a JSON object");
        return;
      }
      configPayload = parsed as Record<string, unknown>;
    } catch {
      setStudioStatus("Extension metadata must be valid JSON");
      return;
    }
    setStudioBusy("extension-configure");
    setStudioStatus(`Saving metadata for ${selectedExtensionPackage.display_name}...`);
    try {
      const response = await fetch(`${API_URL}/api/extensions/${encodeURIComponent(extensionId)}/configure`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ config: configPayload }),
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        await handleExtensionLifecycleFailure(
          payload,
          `Failed to save metadata for ${selectedExtensionPackage.display_name}`,
          setStudioStatus,
        );
        return;
      }
      setStudioExtensionConfigDirty(false);
      await refreshCockpit();
      setStudioStatus(`${selectedExtensionPackage.display_name} metadata saved`);
      appendOperatorFeed(`${selectedExtensionPackage.display_name} metadata saved`, "success");
    } catch {
      setStudioStatus(`Failed to save metadata for ${selectedExtensionPackage.display_name}`);
    } finally {
      setStudioBusy(null);
    }
  }

  async function removeSelectedExtension() {
    const extensionId = selectedExtensionPackage?.id;
    if (!extensionId) return;
    setStudioBusy("extension-remove");
    setStudioStatus(`Removing ${selectedExtensionPackage.display_name}...`);
    try {
      const response = await fetch(`${API_URL}/api/extensions/${encodeURIComponent(extensionId)}`, {
        method: "DELETE",
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        await handleExtensionLifecycleFailure(
          payload,
          `Failed to remove ${selectedExtensionPackage.display_name}`,
          setStudioStatus,
        );
        return;
      }
      await refreshCockpit();
      setStudioSelectedId(null);
      setStudioStatus(`${selectedExtensionPackage.display_name} removed`);
      appendOperatorFeed(`${selectedExtensionPackage.display_name} removed`, "success");
    } catch {
      setStudioStatus(`Failed to remove ${selectedExtensionPackage.display_name}`);
    } finally {
      setStudioBusy(null);
    }
  }

  function saveRunbookMacro(runbook: RunbookInfo) {
    setSavedRunbooks((current) => {
      if (current.some((item) => item.id === runbook.id)) return current;
      const next = [runbook, ...current].slice(0, 8);
      return next;
    });
    setOperatorStatus(`${runbook.label} saved to macros`);
    appendOperatorFeed(`Saved runbook macro: ${runbook.label}`, "success");
  }

  function removeRunbookMacro(runbookId: string) {
    const existing = savedRunbooks.find((item) => item.id === runbookId);
    setSavedRunbooks((current) => current.filter((item) => item.id !== runbookId));
    if (existing) {
      setOperatorStatus(`${existing.label} removed from macros`);
      appendOperatorFeed(`Removed runbook macro: ${existing.label}`, "info");
    }
  }

  async function sendFeedback(interventionId: string, feedbackType: "helpful" | "not_helpful") {
    setFeedbackState((current) => ({ ...current, [interventionId]: "saving" }));

    try {
      const response = await fetch(`${API_URL}/api/observer/interventions/${interventionId}/feedback`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ feedback_type: feedbackType }),
      });
      const payload = await response.json();
      setFeedbackState((current) => ({
        ...current,
        [interventionId]: payload.recorded ? feedbackType : "failed",
      }));
    } catch {
      setFeedbackState((current) => ({ ...current, [interventionId]: "failed" }));
    }
  }

  async function handleApprovalDecision(approval: PendingApproval, decision: "approve" | "deny") {
    if (approvalState[approval.id] === "saving") return;
    setApprovalState((current) => ({ ...current, [approval.id]: "saving" }));

    try {
      const response = await fetch(`${API_URL}/api/approvals/${approval.id}/${decision}`, {
        method: "POST",
      });
      if (!response.ok) {
        setApprovalState((current) => ({ ...current, [approval.id]: "failed" }));
        return;
      }

      const payload = await response.json();
      const nextStatus = payload?.status ?? (decision === "approve" ? "approved" : "denied");
      setApprovalState((current) => ({ ...current, [approval.id]: nextStatus }));
      setPendingApprovals((current) => current.filter((item) => item.id !== approval.id));

      if (decision === "approve" && payload?.resume_message) {
        if (payload.session_id) {
          await openThread(payload.session_id);
        }
        appEventBus.emit("approval-resume", {
          sessionId: payload.session_id ?? approval.session_id ?? null,
          message: payload.resume_message,
        });
      }
    } catch {
      setApprovalState((current) => ({ ...current, [approval.id]: "failed" }));
    }
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const message = composer.trim();
    if (!message || submitDisabled) return;
    const sent = await onSend(message);
    if (sent !== false) {
      setComposer("");
    }
  }

  function queueComposerDraft(message: string) {
    setComposer(message);
    inputRef.current?.focus();
  }

  async function dismissDesktopNotification(notificationId: string) {
    try {
      const response = await fetch(`${API_URL}/api/observer/notifications/${notificationId}/dismiss`, {
        method: "POST",
      });
      if (!response.ok) return;
      await refreshCockpit();
    } catch {
      // ignore
    }
  }

  async function dismissAllDesktopNotifications() {
    try {
      const response = await fetch(`${API_URL}/api/observer/notifications/dismiss-all`, {
        method: "POST",
      });
      if (!response.ok) return;
      await refreshCockpit();
    } catch {
      // ignore
    }
  }

  function queueArtifactWorkflowDraft(
    workflow: WorkflowInfo,
    artifactPath: string,
    producedArtifactTypes?: string[],
  ) {
    queueComposerDraft(buildWorkflowDraft(workflow, artifactPath, producedArtifactTypes));
  }

  async function reloadOperatorSurface(path: "skills" | "workflows") {
    setOperatorStatus(`Reloading ${path}...`);
    try {
      const response = await fetch(`${API_URL}/api/${path}/reload`, { method: "POST" });
      if (!response.ok) {
        setOperatorStatus(`Failed to reload ${path}`);
        appendOperatorFeed(`Failed to reload ${path}`, "failed");
        return;
      }
      await refreshCockpit();
      setOperatorStatus(`${path} reloaded`);
      appendOperatorFeed(`${path} reloaded`, "success");
    } catch {
      setOperatorStatus(`Failed to reload ${path}`);
      appendOperatorFeed(`Failed to reload ${path}`, "failed");
    }
  }

  async function activateStarterPack(pack: StarterPackInfo) {
    setOperatorStatus(`Bootstrapping ${pack.label}...`);
    try {
      const payload = await bootstrapCapability("starter_pack", pack.name, pack.label);
      if (payload.ready) {
        const command = payload.command || pack.sample_prompt;
        if (command) {
          queueComposerDraft(command);
          appendOperatorFeed(`${pack.label} drafted`, "success");
          setOperatorStatus(`${pack.label} drafted to command bar`);
        } else {
          setOperatorStatus(`${pack.label} is ready`);
        }
      }
    } catch {
      // bootstrapCapability already reports failures into the operator surface
    }
  }

  async function openThread(sessionIdToOpen: string | null | undefined) {
    if (!sessionIdToOpen) return false;
    if (useChatStore.getState().sessionId === sessionIdToOpen) {
      return true;
    }
    await switchSession(sessionIdToOpen, "restored");
    const opened = useChatStore.getState().sessionId === sessionIdToOpen;
    if (!opened) {
      setOperatorStatus("Unable to open that thread.");
    }
    return opened;
  }

  async function queueThreadDraft(message: string, sessionIdToOpen?: string | null) {
    if (sessionIdToOpen) {
      const opened = await openThread(sessionIdToOpen);
      if (!opened) return;
    }
    queueComposerDraft(message);
  }

  async function preflightCapability(targetType: "runbook" | "workflow" | "starter_pack", name: string) {
    const response = await fetch(
      `${API_URL}/api/capabilities/preflight?target_type=${encodeURIComponent(targetType)}&name=${encodeURIComponent(name)}`,
    );
    const payload = await response.json().catch(() => null);
    if (!response.ok) {
      throw new Error(payload?.detail || "Capability preflight failed");
    }
    return payload as CapabilityPreflightResponse;
  }

  async function bootstrapCapability(
    targetType: "runbook" | "workflow" | "starter_pack",
    name: string,
    label: string,
    preflight?: CapabilityPreflightResponse | null,
  ) {
    const response = await fetch(`${API_URL}/api/capabilities/bootstrap`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target_type: targetType, name }),
    });
    const payload = await response.json().catch(() => null);
    if (!response.ok || !payload) {
      const detail = payload?.detail || `Failed to bootstrap ${label}`;
      setOperatorStatus(detail);
      appendOperatorFeed(detail, "failed");
      const error = new Error(detail) as LoggedOperatorError;
      error.operatorLogged = true;
      throw error;
    }
    const result = payload as CapabilityBootstrapResponse;
    await refreshCockpit();
    rememberDoctorPlan({
      label,
      source: targetType,
      availability: result.availability,
      blockingReasons: result.blocking_reasons ?? [],
      autorepairActions: preflight?.autorepair_actions ?? [],
      recommendedActions: preflight?.recommended_actions ?? [],
      manualActions: result.manual_actions ?? [],
      appliedActions: (result.applied_actions ?? []).map((action) => formatCapabilityAction(action)),
      command: result.command ?? preflight?.command ?? null,
      riskLevel: result.risk_level ?? preflight?.risk_level ?? null,
      executionBoundaries: result.execution_boundaries ?? preflight?.execution_boundaries ?? [],
    });
    if (result.applied_actions?.length) {
      appendOperatorFeed(
        `${label}: ${result.applied_actions.map((action) => formatCapabilityAction(action)).join(" · ")}`,
        result.ready ? "success" : "info",
      );
    }
    if (!result.ready && result.manual_actions?.length) {
      appendOperatorFeed(
        `${label}: ${result.manual_actions.map((action) => formatCapabilityAction(action)).join(" · ")}`,
        "info",
      );
    }
    if (result.ready) {
      setOperatorStatus(`${label} ready`);
    } else {
      setOperatorStatus(
        result.blocking_reasons?.[0]
          ? `${label} still blocked: ${result.blocking_reasons[0]}`
          : `${label} still blocked`,
      );
    }
    return result;
  }

  async function executeRunbook(runbook: RunbookInfo) {
    setOperatorStatus(`Preflighting ${runbook.label}...`);
    try {
      const preflight = await preflightCapability("runbook", runbook.id);
      if (!preflight.ready) {
        const bootstrap = await bootstrapCapability("runbook", runbook.id, runbook.label, preflight);
        if (bootstrap.ready && bootstrap.command) {
          queueComposerDraft(bootstrap.command);
          appendOperatorFeed(`${runbook.label} drafted`, "success");
        }
        return;
      }
      if (preflight.command) {
        queueComposerDraft(preflight.command);
        setOperatorStatus(`${runbook.label} drafted to command bar`);
        appendOperatorFeed(`${runbook.label} drafted`, "success");
      } else if (runbook.action) {
        await runCapabilityAction(runbook.action);
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : "Capability preflight failed";
      if (!(error instanceof Error && (error as LoggedOperatorError).operatorLogged)) {
        setOperatorStatus(message);
        appendOperatorFeed(message, "failed");
      }
    }
  }

  async function repairWorkflowReplay(workflow: WorkflowRunRecord) {
    const actions: CapabilityAction[] = Array.isArray(workflow.replayRecommendedActions)
      ? workflow.replayRecommendedActions.flatMap((item) => {
          if (
            !item
            || typeof item !== "object"
            || typeof item.type !== "string"
            || typeof item.label !== "string"
          ) {
            return [];
          }
          return [item as unknown as CapabilityAction];
        })
      : [];
    if (!actions.length) {
      setOperatorStatus(`No replay repair actions available for ${workflow.workflowName}`);
      appendOperatorFeed(`No replay repair actions available for ${workflow.workflowName}`, "failed");
      return;
    }
    await runCapabilityActions(actions, `${workflow.workflowName} replay`);
  }

  function failedWorkflowStep(workflow: WorkflowRunRecord): WorkflowStepRecord | null {
    return (
      workflow.stepRecords?.find((step) =>
        step.status !== "succeeded" || workflow.continuedErrorSteps.includes(step.id),
      ) ?? null
    );
  }

  function workflowStepFocusRecords(workflow: WorkflowRunRecord): WorkflowStepRecord[] {
    const records = workflow.stepRecords ?? [];
    return [...records]
      .sort((left, right) => {
        const leftPriority =
          left.status !== "succeeded" || workflow.continuedErrorSteps.includes(left.id)
            ? 0
            : left.isRecoverable || (left.recoveryActions?.length ?? 0) > 0
              ? 1
              : left.artifactPaths.length > 0
                ? 2
                : 3;
        const rightPriority =
          right.status !== "succeeded" || workflow.continuedErrorSteps.includes(right.id)
            ? 0
            : right.isRecoverable || (right.recoveryActions?.length ?? 0) > 0
              ? 1
              : right.artifactPaths.length > 0
                ? 2
                : 3;
        if (leftPriority !== rightPriority) return leftPriority - rightPriority;
        return right.index - left.index;
      })
      .slice(0, 3);
  }

  function queueWorkflowStepContext(workflow: WorkflowRunRecord, step: WorkflowStepRecord) {
    const outputPath = step.artifactPaths[0];
    const parts = [
      `Review workflow "${workflow.workflowName}" step "${step.id}" (${step.tool}).`,
      outputPath ? `Latest step output: "${outputPath}".` : null,
      step.errorSummary ? `Current failure: ${step.errorSummary}.` : null,
      step.resultSummary ? `Latest result: ${step.resultSummary}.` : null,
      step.recoveryHint ? `Recovery hint: ${step.recoveryHint}.` : null,
    ].filter((part): part is string => typeof part === "string" && part.trim().length > 0);
    queueComposerDraft(parts.join(" "));
  }

  function queueWorkflowOutputContext(workflow: WorkflowRunRecord | null | undefined) {
    if (!workflow) return;
    const resolved = resolveWorkflowRun(workflow);
    const outputPath = resolved.artifacts[0]?.filePath ?? resolved.artifactPaths[0];
    if (!outputPath) return;
    queueComposerDraft(`Use the workspace file "${outputPath}" as context for the next action.`);
  }

  function workflowPrimaryOutputPath(workflow: WorkflowRunRecord | null | undefined): string | null {
    if (!workflow) return null;
    const resolved = resolveWorkflowRun(workflow);
    return resolved.artifacts[0]?.filePath ?? resolved.artifactPaths[0] ?? null;
  }

  function queueWorkflowOutputComparison(
    currentWorkflow: WorkflowRunRecord | null | undefined,
    relatedWorkflow: WorkflowRunRecord | null | undefined,
  ) {
    const currentOutputPath = workflowPrimaryOutputPath(currentWorkflow);
    const relatedOutputPath = workflowPrimaryOutputPath(relatedWorkflow);
    if (!currentOutputPath || !relatedOutputPath) return;
    queueComposerDraft(
      `Compare the workspace files "${currentOutputPath}" and "${relatedOutputPath}". `
      + "Summarize the key differences, what changed between these workflow outputs, and whether the related branch improved the result.",
    );
  }

  function queueWorkflowFamilyPlan(workflow: WorkflowRunRecord | null | undefined) {
    if (!workflow) return;
    const resolved = resolveWorkflowRun(workflow);
    const currentOutputPath = workflowPrimaryOutputPath(resolved);
    const bestContinuation = workflowBestContinuationRun(resolved);
    const latestFailure = workflowFailureLineage(resolved)[0] ?? null;
    const familyOutputs = workflowFamilyArtifactOutputs(resolved).slice(0, 3);
    if (!currentOutputPath && !bestContinuation && !latestFailure && familyOutputs.length === 0) return;
    const parts = [
      `Review workflow family state for "${resolved.workflowName}".`,
      currentOutputPath ? `Current output: "${currentOutputPath}".` : null,
      bestContinuation
        ? [
            `Best continuation: "${bestContinuation.summary}"`,
            workflowPrimaryOutputPath(bestContinuation)
              ? `latest output "${workflowPrimaryOutputPath(bestContinuation)}"`
              : null,
          ].filter((part): part is string => typeof part === "string" && part.length > 0).join(" with ")
        : null,
      latestFailure ? `Latest family failure: "${latestFailure.summary}".` : null,
      familyOutputs.length > 0
        ? `Related reusable outputs: ${familyOutputs.map((output) => `"${output.filePath}"`).join(", ")}.`
        : null,
      "Recommend the best next step, whether to continue a branch, compare outputs, or reuse one of the related outputs.",
    ].filter((part): part is string => typeof part === "string" && part.trim().length > 0);
    queueComposerDraft(parts.join(" "));
  }

  function primaryWorkflowShortcutTarget(): WorkflowRunRecord | null {
    const candidates = [
      primaryWorkflowTriageEntry?.workflow ?? null,
      ...workflowRunsWithArtifacts,
    ]
      .filter((workflow): workflow is WorkflowRunRecord => workflow != null)
      .map((workflow) => resolveWorkflowRun(workflow));
    return (
      candidates.find((workflow) => workflowStepFocusRecords(workflow).length > 0)
      ?? candidates.find((workflow) => (workflow.artifacts[0]?.filePath ?? workflow.artifactPaths[0]) != null)
      ?? candidates[0]
      ?? null
    );
  }

  function inspectPrimaryWorkflowEntry() {
    inspectWorkflowRun(primaryWorkflowShortcutTarget());
  }

  async function toggleWorkflow(workflow: WorkflowInfo, enabled: boolean) {
    setOperatorStatus(`${enabled ? "Enabling" : "Disabling"} ${workflow.name}...`);
    try {
      const response = await fetch(`${API_URL}/api/workflows/${workflow.name}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      });
      if (!response.ok) {
        setOperatorStatus(`Failed to update ${workflow.name}`);
        appendOperatorFeed(`Failed to update workflow ${workflow.name}`, "failed");
        return;
      }
      await refreshCockpit();
      setOperatorStatus(`${workflow.name} ${enabled ? "enabled" : "disabled"}`);
      appendOperatorFeed(`${workflow.name} ${enabled ? "enabled" : "disabled"}`, "success");
    } catch {
      setOperatorStatus(`Failed to update ${workflow.name}`);
      appendOperatorFeed(`Failed to update workflow ${workflow.name}`, "failed");
    }
  }

  async function installCatalogItem(item: CatalogItemInfo) {
    setOperatorStatus(`Installing ${item.name}...`);
    rememberDoctorPlan({
      label: item.name,
      source: `catalog ${item.type}`,
      availability: item.installed ? "installed" : "missing",
      blockingReasons: item.missing_tools?.length ? [`missing tools: ${item.missing_tools.join(", ")}`] : [],
      autorepairActions: [],
      recommendedActions: item.recommended_actions ?? [],
      manualActions: item.recommended_actions ?? [],
      appliedActions: [],
      command: null,
      riskLevel: null,
      executionBoundaries: [],
    });
    try {
      const identifier = item.catalog_id ?? item.name;
      const response = await fetch(`${API_URL}/api/catalog/install/${encodeURIComponent(identifier)}`, {
        method: "POST",
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        setOperatorStatus(payload?.detail || `Failed to install ${item.name}`);
        appendOperatorFeed(`Failed to install ${item.name}`, "failed");
        return;
      }
      await refreshCockpit();
      setOperatorStatus(`${item.name} installed`);
      appendOperatorFeed(`${item.name} installed`, "success");
    } catch {
      setOperatorStatus(`Failed to install ${item.name}`);
      appendOperatorFeed(`Failed to install ${item.name}`, "failed");
    }
  }

  async function sendTestNativeNotification() {
    setOperatorStatus("Sending native notification test...");
    try {
      const response = await fetch(`${API_URL}/api/observer/notifications/test`, {
        method: "POST",
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        setOperatorStatus(payload?.detail || "Failed to send native test notification");
        appendOperatorFeed("Failed to send native test notification", "failed");
        return;
      }
      await refreshCockpit();
      setOperatorStatus(`Queued native test: ${payload?.title ?? "Seraph test notification"}`);
      appendOperatorFeed("Queued native test notification", "success");
    } catch {
      setOperatorStatus("Failed to send native test notification");
      appendOperatorFeed("Failed to send native test notification", "failed");
    }
  }

  async function runCapabilityActions(
    actions: CapabilityAction[],
    label: string,
  ) {
    const allowedActions = actions.filter((action) => SUPPORTED_CAPABILITY_ACTION_TYPES.has(action.type));
    if (allowedActions.length === 0) {
      setOperatorStatus(`No safe repair actions available for ${label}`);
      appendOperatorFeed(`No safe repair actions available for ${label}`, "failed");
      return;
    }
    const requiresStepByStepExecution = (
      allowedActions.length > 1
      && allowedActions.some((action) => !isLowRiskBatchCapabilityAction(action))
    );
    if (requiresStepByStepExecution) {
      const actionSummary = allowedActions.map((action) => formatCapabilityAction(action)).join(" · ");
      setOperatorStatus(`${label} requires step-by-step execution`);
      appendOperatorFeed(
        `${label} requires step-by-step execution: ${actionSummary}`,
        "info",
      );
      return;
    }
    setOperatorStatus(`Repairing ${label}...`);
    let completed = true;
    for (const action of allowedActions) {
      const result = await runCapabilityAction(action);
      completed = completed && result;
    }
    if (!completed) {
      return;
    }
    setOperatorStatus(`${label} repair sequence applied`);
    appendOperatorFeed(`${label} repair sequence applied`, "success");
  }

  async function runCapabilityAction(action: CapabilityAction | null | undefined): Promise<boolean> {
    if (!action) return false;
    switch (action.type) {
      case "enable_extension":
        if (action.name) return enableExtensionPackage(action.name, typeof action.target === "string" ? action.target : undefined);
        return false;
      case "toggle_skill": {
        const skill = skills.find((item) => item.name === action.name);
        if (skill) await toggleSkill(skill);
        return true;
      }
      case "toggle_workflow": {
        const workflow = workflows.find((item) => item.name === action.name);
        if (workflow) await toggleWorkflow(workflow, Boolean(action.enabled));
        return true;
      }
      case "toggle_mcp_server": {
        const server = mcpServers.find((item) => item.name === action.name);
        if (server) await toggleMcpServer(server);
        return true;
      }
      case "test_mcp_server": {
        const server = mcpServers.find((item) => item.name === action.name);
        if (server) await testMcpServer(server);
        return true;
      }
      case "test_native_notification":
        await sendTestNativeNotification();
        return true;
      case "set_tool_policy":
        if (action.mode === "safe" || action.mode === "balanced" || action.mode === "full") {
          await updateToolPolicy(action.mode);
        }
        return true;
      case "set_mcp_policy":
        if (action.mode === "disabled" || action.mode === "approval" || action.mode === "full") {
          await updateMcpPolicy(action.mode);
        }
        return true;
      case "install_catalog_item": {
        const item = catalogItems.find((entry) => entry.name === action.name || entry.catalog_id === action.name);
        if (item) await installCatalogItem(item);
        return true;
      }
      case "activate_starter_pack": {
        const pack = starterPacks.find((entry) => entry.name === action.name);
        if (pack) await activateStarterPack(pack);
        return true;
      }
      case "draft_workflow": {
        const workflow = workflows.find((entry) => entry.name === action.name);
        if (workflow) queueComposerDraft(buildWorkflowDraft(workflow));
        return true;
      }
      case "open_settings":
        setSettingsPanelOpen(true);
        return true;
      default:
        return false;
    }
  }

  async function updateToolPolicy(mode: ToolPolicyMode) {
    if (toolPolicyMode === mode) return;
    setOperatorStatus(`Setting tool policy to ${formatOperatorMode(mode)}...`);
    try {
      const response = await fetch(`${API_URL}/api/settings/tool-policy-mode`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode }),
      });
      if (!response.ok) {
        setOperatorStatus("Failed to update tool policy");
        appendOperatorFeed("Failed to update tool policy", "failed");
        return;
      }
      await refreshCockpit();
      setOperatorStatus(`Tool policy set to ${formatOperatorMode(mode)}`);
      appendOperatorFeed(`Tool policy set to ${formatOperatorMode(mode)}`, "success");
    } catch {
      setOperatorStatus("Failed to update tool policy");
      appendOperatorFeed("Failed to update tool policy", "failed");
    }
  }

  async function updateMcpPolicy(mode: McpPolicyMode) {
    if (mcpPolicyMode === mode) return;
    setOperatorStatus(`Setting MCP policy to ${formatOperatorMode(mode)}...`);
    try {
      const response = await fetch(`${API_URL}/api/settings/mcp-policy-mode`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode }),
      });
      if (!response.ok) {
        setOperatorStatus("Failed to update MCP policy");
        appendOperatorFeed("Failed to update MCP policy", "failed");
        return;
      }
      await refreshCockpit();
      setOperatorStatus(`MCP policy set to ${formatOperatorMode(mode)}`);
      appendOperatorFeed(`MCP policy set to ${formatOperatorMode(mode)}`, "success");
    } catch {
      setOperatorStatus("Failed to update MCP policy");
      appendOperatorFeed("Failed to update MCP policy", "failed");
    }
  }

  async function updateApprovalPolicy(mode: ApprovalMode) {
    if (approvalMode === mode) return;
    setOperatorStatus(`Setting approval mode to ${formatOperatorMode(mode)}...`);
    try {
      const response = await fetch(`${API_URL}/api/settings/approval-mode`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ mode }),
      });
      if (!response.ok) {
        setOperatorStatus("Failed to update approval mode");
        appendOperatorFeed("Failed to update approval mode", "failed");
        return;
      }
      await refreshCockpit();
      setOperatorStatus(`Approval mode set to ${formatOperatorMode(mode)}`);
      appendOperatorFeed(`Approval mode set to ${formatOperatorMode(mode)}`, "success");
    } catch {
      setOperatorStatus("Failed to update approval mode");
      appendOperatorFeed("Failed to update approval mode", "failed");
    }
  }

  async function toggleSkill(skill: SkillInfo) {
    setOperatorStatus(`${skill.enabled ? "Disabling" : "Enabling"} ${skill.name}...`);
    try {
      const response = await fetch(`${API_URL}/api/skills/${skill.name}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: !skill.enabled }),
      });
      if (!response.ok) {
        setOperatorStatus(`Failed to update ${skill.name}`);
        appendOperatorFeed(`Failed to update skill ${skill.name}`, "failed");
        return;
      }
      await refreshCockpit();
      setOperatorStatus(`${skill.name} ${skill.enabled ? "disabled" : "enabled"}`);
      appendOperatorFeed(`${skill.name} ${skill.enabled ? "disabled" : "enabled"}`, "success");
    } catch {
      setOperatorStatus(`Failed to update ${skill.name}`);
      appendOperatorFeed(`Failed to update skill ${skill.name}`, "failed");
    }
  }

  async function enableExtensionPackage(extensionId: string, displayName?: string): Promise<boolean> {
    const extensionPackage = extensionPackages.find((item) => item.id === extensionId);
    const label = displayName ?? extensionPackage?.display_name ?? extensionId;
    setOperatorStatus(`Enabling ${label}...`);
    try {
      const response = await fetch(`${API_URL}/api/extensions/${encodeURIComponent(extensionId)}/enable`, {
        method: "POST",
      });
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        const approvalHandled = await handleExtensionLifecycleFailure(
          payload,
          `Failed to enable ${label}`,
          setOperatorStatus,
        );
        if (!approvalHandled) {
          appendOperatorFeed(`Failed to enable extension ${label}`, "failed");
        }
        return false;
      }
      await refreshCockpit();
      setOperatorStatus(`${label} enabled`);
      appendOperatorFeed(`${label} enabled`, "success");
      return true;
    } catch {
      setOperatorStatus(`Failed to enable ${label}`);
      appendOperatorFeed(`Failed to enable extension ${label}`, "failed");
      return false;
    }
  }

  async function toggleMcpServer(server: McpServerInfo) {
    setOperatorStatus(`${server.enabled ? "Disabling" : "Enabling"} ${server.name}...`);
    try {
      const packagedServer = server.source === "extension" && !!server.extension_id && !!server.extension_reference;
      const response = await fetch(
        packagedServer
          ? `${API_URL}/api/extensions/${encodeURIComponent(server.extension_id ?? "")}/connectors/enabled`
          : `${API_URL}/api/mcp/servers/${server.name}`,
        packagedServer
          ? {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ reference: server.extension_reference, enabled: !server.enabled }),
          }
          : {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ enabled: !server.enabled }),
          },
      );
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        const approvalHandled = await handleExtensionLifecycleFailure(
          payload,
          `Failed to update ${server.name}`,
          setOperatorStatus,
        );
        if (!approvalHandled) {
          appendOperatorFeed(`Failed to update MCP ${server.name}`, "failed");
        }
        return;
      }
      await refreshCockpit();
      setOperatorStatus(`${server.name} ${server.enabled ? "disabled" : "enabled"}`);
      appendOperatorFeed(`${server.name} ${server.enabled ? "disabled" : "enabled"}`, "success");
    } catch {
      setOperatorStatus(`Failed to update ${server.name}`);
      appendOperatorFeed(`Failed to update MCP ${server.name}`, "failed");
    }
  }

  async function testMcpServer(server: McpServerInfo) {
    setOperatorStatus(`Testing ${server.name}...`);
    try {
      const packagedServer = server.source === "extension" && !!server.extension_id && !!server.extension_reference;
      const response = await fetch(
        packagedServer
          ? `${API_URL}/api/extensions/${encodeURIComponent(server.extension_id ?? "")}/connectors/test`
          : `${API_URL}/api/mcp/servers/${server.name}/test`,
        packagedServer
          ? {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ reference: server.extension_reference }),
          }
          : { method: "POST" },
      );
      const payload = await response.json();
      if (!response.ok) {
        setOperatorStatus(payload.detail || `${server.name} test failed`);
        appendOperatorFeed(`${server.name} test failed`, "failed");
        return;
      }
      if (payload.status === "ok") {
        setOperatorStatus(`${server.name}: OK — ${payload.tool_count} tools`);
        appendOperatorFeed(`${server.name}: OK — ${payload.tool_count} tools`, "success");
      } else {
        setOperatorStatus(payload.message || `${server.name}: ${payload.status}`);
        appendOperatorFeed(`${server.name}: ${payload.status}`, "info");
      }
      await refreshCockpit();
    } catch {
      setOperatorStatus(`${server.name}: connection failed`);
      appendOperatorFeed(`${server.name}: connection failed`, "failed");
    }
  }

  function renderInspector() {
    if (!selectedInspector) {
      return (
        <div className="cockpit-empty">
          Select a workflow run, approval, intervention, trace row, audit event, or recent output to inspect it here.
        </div>
      );
    }

    let title = "";
    let meta = "";
    let body = "";
    let details: Record<string, unknown> = {};
    const selectedWorkflow = selectedInspector?.kind === "workflow"
      ? resolveWorkflowRun(selectedInspector.workflow)
      : null;
    const selectedWorkflowApproval = selectedWorkflow ? approvalForWorkflow(selectedWorkflow) : null;
    const selectedWorkflowLatestBranch = selectedWorkflow ? workflowLatestBranchRun(selectedWorkflow) : null;
    const selectedWorkflowBestContinuation = selectedWorkflow ? workflowBestContinuationRun(selectedWorkflow) : null;
    const selectedWorkflowCheckpointActions = selectedWorkflow ? workflowCheckpointActions(selectedWorkflow) : [];
    const selectedWorkflowName = selectedWorkflow?.workflowName ?? "workflow";
    const selectedWorkflowCheckpointDraftByStep = new Map(
      selectedWorkflowCheckpointActions.map((action) => [action.stepId, action.draft]),
    );

    if (selectedInspector.kind === "approval") {
      const approval = selectedInspector.approval;
      title = approval.tool_name;
      meta = `${approval.risk_level} approval`;
      body = `approval request · ${approval.summary}`;
      details = {
        approval_id: approval.id,
        session_id: approval.session_id ?? "n/a",
        thread: approval.thread_label ?? approval.thread_id ?? approval.session_id ?? "n/a",
        status: approval.status,
        resolution: approvalState[approval.id] ?? "pending",
        resume_message: approval.resume_message ?? "n/a",
        extension_id: approval.extension_id ?? "n/a",
        extension_display_name: approval.extension_display_name ?? "n/a",
        extension_action: approval.extension_action ?? "n/a",
        package_path: approval.package_path ?? "n/a",
        lifecycle_boundaries: approval.lifecycle_boundaries ?? [],
        permissions: approval.permissions ?? {},
      };
    } else if (selectedInspector.kind === "workflow") {
      const workflow = selectedWorkflow!;
      const linkedInterventions = interventionsForWorkflow(workflow);
      const childRuns = workflowChildRuns(workflow);
      const peerRuns = workflowPeerRuns(workflow);
      const familyRuns = workflowFamilyRuns(workflow);
      title = workflow.workflowName;
      meta = `${workflow.status} · ${workflow.artifacts.length} artifacts`;
      body = workflow.summary;
      details = {
        tool_name: workflow.toolName,
        session_id: workflow.sessionId ?? "n/a",
        thread: workflow.threadLabel ?? workflow.threadId ?? "ambient",
        status: workflow.status,
        run_fingerprint: workflow.runFingerprint ?? "n/a",
        run_identity: workflow.runIdentity ?? "n/a",
        parent_run_identity: workflow.parentRunIdentity ?? "none",
        root_run_identity: workflow.rootRunIdentity ?? "n/a",
        branch_kind: workflow.branchKind ?? "none",
        branch_depth: workflow.branchDepth ?? 0,
        supervision_state: workflowSupervisionLabel(workflow),
        branch_child_count: childRuns.length,
        branch_peer_count: peerRuns.length,
        branch_family_size: familyRuns.length,
        risk_level: workflow.riskLevel ?? "unknown",
        execution_boundaries: workflow.executionBoundaries ?? [],
        accepts_secret_refs: workflow.acceptsSecretRefs ?? false,
        step_tools: workflow.stepTools,
        step_records: workflow.stepRecords ?? [],
        continued_error_steps: workflow.continuedErrorSteps,
        checkpoint_context_available: workflow.checkpointContextAvailable ?? false,
        artifact_paths: workflow.artifactPaths,
        pending_approval: selectedWorkflowApproval ? selectedWorkflowApproval.id : "none",
        pending_approval_count: workflow.pendingApprovalCount ?? 0,
        pending_approvals: workflow.pendingApprovals?.map((item) => item.summary).join(" | ") || "none",
        replay_allowed: workflow.replayAllowed ?? false,
        replay_block_reason: workflow.replayBlockReason ?? "none",
        availability: workflow.availability ?? "unknown",
        replay_inputs: workflow.replayInputs ?? {},
        replay_recommended_actions: workflow.replayRecommendedActions ?? [],
        resume_from_step: workflow.resumeFromStep ?? "n/a",
        resume_checkpoint_label: workflow.resumeCheckpointLabel ?? "n/a",
        retry_from_step_draft: workflow.retryFromStepDraft ?? "n/a",
        timeline: workflow.timeline ?? [],
        linked_interventions: linkedInterventions.length,
      };
    } else if (selectedInspector.kind === "intervention") {
      const intervention = selectedInspector.intervention;
      title = intervention.intervention_type;
      meta = `${formatContinuityLabel(intervention.continuity_surface)} · ${formatAge(intervention.updated_at)}`;
      body = intervention.content_excerpt;
      details = {
        intervention_id: intervention.id,
        feedback: feedbackState[intervention.id] ?? intervention.feedback_type ?? "unrated",
        policy_action: intervention.policy_action,
        policy_reason: intervention.policy_reason,
        delivery_decision: intervention.delivery_decision ?? "n/a",
        latest_outcome: intervention.latest_outcome,
        continuity_surface: intervention.continuity_surface,
        transport: intervention.transport ?? "n/a",
      };
    } else if (selectedInspector.kind === "trace") {
      const message = selectedInspector.message;
      const relatedAudit = auditEvents.find((event) => event.tool_name === message.toolUsed);
      title = message.toolUsed ?? "trace step";
      meta = `step ${message.stepNumber ?? "?"}`;
      body = message.content;
      details = {
        tool: message.toolUsed ?? "n/a",
        related_audit: relatedAudit?.summary ?? "none",
        risk_level: relatedAudit?.risk_level ?? "n/a",
      };
    } else if (selectedInspector.kind === "audit") {
      const event = selectedInspector.event;
      title = event.tool_name ?? event.event_type;
      meta = `${event.event_type} · ${event.risk_level}`;
      body = event.summary;
      details = event.details ?? {};
    } else if (selectedInspector.kind === "operator") {
      const entity = selectedInspector.entity;
      title = entity.name;
      meta = entity.meta;
      body = entity.summary;
      details = entity.details;
    } else {
      const artifact = selectedInspector.artifact;
      title = artifact.filePath;
      meta = artifact.source;
      body = artifact.summary;
      details = {
        file_path: artifact.filePath,
        session_id: artifact.sessionId ?? "n/a",
        created_at: artifact.createdAt,
      };
    }

    return (
      <div className="cockpit-inspector">
        <div className="cockpit-inspector-title">{title}</div>
        <div className="cockpit-inspector-meta">{meta}</div>
        <div className="cockpit-inspector-body">{body}</div>
        {selectedInspector.kind === "workflow" && selectedWorkflow && (
          <div className="cockpit-feedback-row">
            {selectedWorkflow.replayAllowed !== false ? (
              <>
                <button
                  className="cockpit-feedback-button"
                  onClick={() =>
                    queueComposerDraft(
                      selectedWorkflow.replayDraft
                        ?? buildWorkflowReplayDraft(selectedWorkflow),
                    )
                  }
                >
                  {selectedWorkflow.executionBoundaries?.length
                    ? "Draft Boundary-Aware Rerun"
                    : "Draft Rerun"}
                </button>
                {(() => {
                  const checkpointActions = selectedWorkflowCheckpointActions.slice(0, 2);
                  if (checkpointActions.length > 0) {
                    return checkpointActions.map((action) => (
                      <button
                        key={`${selectedWorkflow.id}:${action.stepId}`}
                        className="cockpit-feedback-button"
                        onClick={() => queueComposerDraft(action.draft)}
                      >
                        {action.label}
                      </button>
                    ));
                  }
                  if (!selectedWorkflow.retryFromStepDraft) {
                    return null;
                  }
                  return (
                    <button
                      className="cockpit-feedback-button"
                      onClick={() =>
                        queueComposerDraft(
                          selectedWorkflow.retryFromStepDraft
                          ?? buildWorkflowReplayDraft(selectedWorkflow),
                        )
                      }
                    >
                      Retry From Step
                    </button>
                  );
                })()}
                {studioEntryForWorkflowRun(selectedWorkflow) && (
                  <button
                    className="cockpit-feedback-button"
                    onClick={() => openExtensionStudio(studioEntryForWorkflowRun(selectedWorkflow))}
                  >
                    Open Studio
                  </button>
                )}
                {selectedWorkflowLatestBranch && (
                  <>
                    <button
                      className="cockpit-feedback-button"
                      onClick={() => inspectWorkflowRun(selectedWorkflowLatestBranch)}
                    >
                      Open Latest Branch
                    </button>
                    <button
                      className="cockpit-feedback-button"
                      onClick={() => continueWorkflowRun(selectedWorkflowLatestBranch)}
                    >
                      Continue Latest Branch
                    </button>
                  </>
                )}
              </>
            ) : (
              <span className="cockpit-feedback-status">
                Replay blocked: {replayBlockCopy(selectedWorkflow.replayBlockReason)}
              </span>
            )}
            {selectedWorkflowApproval && (
              <>
                <button
                  className="cockpit-feedback-button"
                  onClick={() => void handleApprovalDecision(selectedWorkflowApproval, "approve")}
                >
                  Approve
                </button>
                <button
                  className="cockpit-feedback-button"
                  onClick={() => void handleApprovalDecision(selectedWorkflowApproval, "deny")}
                >
                  Deny
                </button>
              </>
            )}
            {selectedWorkflow.threadId && (
              <button
                className="cockpit-feedback-button"
                onClick={() => void openThread(selectedWorkflow.threadId)}
              >
                Open Thread
              </button>
            )}
            {selectedWorkflow.artifactPaths[0] && (
              <button
                className="cockpit-feedback-button"
                onClick={() =>
                  queueComposerDraft(
                    `Use the workspace file "${selectedWorkflow.artifactPaths[0]}" as context for the next action.`,
                  )
                }
              >
                Use Output
              </button>
            )}
              {selectedWorkflow.artifactPaths[0]
                && compatibleArtifactWorkflows(
                  selectedWorkflow.artifactPaths[0]!,
                  workflowDefinitionByName.get(selectedWorkflow.workflowName)?.output_surface_artifact_types,
                  [selectedWorkflow.workflowName, selectedWorkflow.toolName],
                ).slice(0, 2).map((workflow) => (
                  <button
                    key={`${selectedWorkflow.id}:${workflow.name}`}
                    className="cockpit-feedback-button"
                    onClick={() =>
                      queueArtifactWorkflowDraft(
                        workflow,
                        selectedWorkflow.artifactPaths[0]!,
                        workflowDefinitionByName.get(selectedWorkflow.workflowName)?.output_surface_artifact_types,
                      )
                    }
                  >
                    Run {workflow.name}
                  </button>
                ))}
            {selectedWorkflow.approvalRecoveryMessage && (
              <span className="cockpit-feedback-status">
                {selectedWorkflow.approvalRecoveryMessage}
              </span>
            )}
            {selectedWorkflow.continuedErrorSteps.length > 0
              && selectedWorkflow.checkpointContextAvailable === false && (
              <span className="cockpit-feedback-status">
                Checkpoint state was not persisted for this failure. Only a full rerun is currently safe.
              </span>
            )}
            {selectedWorkflow.parentRunIdentity && (
              <button
                className="cockpit-feedback-button"
                onClick={() => inspectWorkflowRun(workflowRunByIdentity.get(selectedWorkflow.parentRunIdentity ?? ""))}
              >
                Open Parent
              </button>
            )}
            {workflowPeerRuns(selectedWorkflow)[0] && (
              <button
                className="cockpit-feedback-button"
                onClick={() => inspectWorkflowRun(workflowPeerRuns(selectedWorkflow)[0])}
              >
                Open Peer Branch
              </button>
            )}
          </div>
        )}
        {selectedInspector.kind === "workflow" && selectedWorkflow && workflowResumeDetails(selectedWorkflow).length > 0 && (
          <div className="cockpit-chip-row">
            {workflowResumeDetails(selectedWorkflow).map((detail) => (
              <span key={`${selectedWorkflow.id}:${detail}`} className="cockpit-chip">
                {detail}
              </span>
            ))}
          </div>
        )}
        {selectedInspector.kind === "workflow" && selectedWorkflow && (
          <div className="cockpit-chip-row">
            {workflowSupervisionSummary(selectedWorkflow).map((detail) => (
              <span key={`${selectedWorkflow.id}:supervision:${detail}`} className="cockpit-chip">
                {detail}
              </span>
            ))}
          </div>
        )}
        {selectedInspector.kind === "workflow" && selectedWorkflow && workflowBranchDebugSummary(selectedWorkflow).length > 0 && (
          <div className="cockpit-chip-row">
            {workflowBranchDebugSummary(selectedWorkflow).map((detail) => (
              <span key={`${selectedWorkflow.id}:branch-debug:${detail}`} className="cockpit-chip">
                {detail}
              </span>
            ))}
          </div>
        )}
        {selectedInspector.kind === "workflow" && selectedWorkflow && selectedWorkflowApproval && (
          <div className="cockpit-inspector-stack">
            <div className="cockpit-inspector-stack-row">
              <div className="cockpit-key">pending approval</div>
              <div className="cockpit-value">
                approval context · {selectedWorkflowApproval.summary}
                {selectedWorkflowApproval.thread_label
                  ? ` · ${selectedWorkflowApproval.thread_label}`
                  : selectedWorkflowApproval.thread_id
                    ? ` · thread ${selectedWorkflowApproval.thread_id.slice(0, 6)}`
                    : ""}
                {` · ${selectedWorkflowApproval.risk_level} risk`}
              </div>
              {selectedWorkflowApproval.resume_message && (
                <button
                  className="cockpit-feedback-button"
                  aria-label={`Continue approval context for ${selectedWorkflowName}`}
                  onClick={() =>
                    void queueThreadDraft(
                      selectedWorkflowApproval.resume_message ?? "",
                      selectedWorkflowApproval.thread_id ?? selectedWorkflowApproval.session_id,
                    )
                  }
                >
                  Continue
                </button>
              )}
              {(() => {
                const threadTarget = selectedWorkflowApproval.thread_id ?? selectedWorkflowApproval.session_id;
                if (!threadTarget) return null;
                return (
                  <button
                    className="cockpit-feedback-button"
                    aria-label={`Open approval thread for ${selectedWorkflowName}`}
                    onClick={() => void openThread(threadTarget)}
                  >
                    Open Thread
                  </button>
                );
              })()}
              <button
                className="cockpit-feedback-button"
                aria-label={`Approve approval context for ${selectedWorkflowName}`}
                onClick={() => void handleApprovalDecision(selectedWorkflowApproval, "approve")}
              >
                Approve
              </button>
              <button
                className="cockpit-feedback-button"
                aria-label={`Deny approval context for ${selectedWorkflowName}`}
                onClick={() => void handleApprovalDecision(selectedWorkflowApproval, "deny")}
              >
                Deny
              </button>
            </div>
          </div>
        )}
        {selectedInspector.kind === "workflow" && selectedWorkflow && selectedWorkflow.artifacts.length > 0 && (
          <div className="cockpit-inspector-stack">
            {selectedWorkflow.artifacts.slice(0, 3).map((artifact) => {
              const compatible = compatibleArtifactWorkflows(
                artifact.filePath,
                workflowDefinitionByName.get(selectedWorkflow.workflowName)?.output_surface_artifact_types,
                [selectedWorkflow.workflowName, selectedWorkflow.toolName],
              ).slice(0, 1);
              return (
                <div key={`${selectedWorkflow.id}:artifact:${artifact.id}`} className="cockpit-inspector-stack-row">
                  <div className="cockpit-key">artifact output</div>
                  <div className="cockpit-value">
                    artifact output · {artifact.filePath} · {artifact.source} · {formatAge(artifact.createdAt)}
                  </div>
                  <button
                    className="cockpit-feedback-button"
                    aria-label={`Inspect artifact output ${artifact.filePath}`}
                    onClick={() => setSelectedInspector({ kind: "artifact", artifact })}
                  >
                    Inspect
                  </button>
                  <button
                    className="cockpit-feedback-button"
                    aria-label={`Use artifact output ${artifact.filePath}`}
                    onClick={() =>
                      queueComposerDraft(
                        `Use the workspace file "${artifact.filePath}" as context for the next action.`,
                      )
                    }
                  >
                    Use
                  </button>
                  {compatible.map((workflow) => (
                    <button
                      key={`${selectedWorkflow.id}:${artifact.id}:${workflow.name}`}
                      className="cockpit-feedback-button"
                      aria-label={`Run ${workflow.name} from artifact output ${artifact.filePath}`}
                      onClick={() => queueArtifactWorkflowDraft(workflow, artifact.filePath)}
                    >
                      Run {workflow.name}
                    </button>
                  ))}
                </div>
              );
            })}
            {selectedWorkflow.artifacts.length > 3 && (
              <div className="cockpit-inspector-stack-row">
                <div className="cockpit-key">artifact output</div>
                <div className="cockpit-value">
                  {selectedWorkflow.artifacts.length - 3} more artifact outputs remain available in Recent outputs.
                </div>
              </div>
            )}
          </div>
        )}
        {selectedInspector.kind === "workflow" && selectedWorkflow && workflowStepFocusRecords(selectedWorkflow).length > 0 && (
          <div className="cockpit-inspector-stack">
            {workflowStepFocusRecords(selectedWorkflow).map((step) => {
              const checkpointDraft = selectedWorkflowCheckpointDraftByStep.get(step.id) ?? null;
              const outputPath = step.artifactPaths[0] ?? null;
              const compatible = outputPath
                ? compatibleArtifactWorkflows(
                    outputPath,
                    workflowDefinitionByName.get(selectedWorkflow.workflowName)?.output_surface_artifact_types,
                    [selectedWorkflow.workflowName, selectedWorkflow.toolName],
                  ).slice(0, 1)
                : [];
              return (
                <div key={`${selectedWorkflow.id}:step-focus:${step.id}`} className="cockpit-inspector-stack-row">
                  <div className="cockpit-key">{`step ${step.index + 1}`}</div>
                  <div className="cockpit-value">
                    {step.tool} · {workflowStepSummary(step)}
                  </div>
                  <button
                    className="cockpit-feedback-button"
                    aria-label={`Use step context ${step.id} for ${selectedWorkflowName}`}
                    onClick={() => queueWorkflowStepContext(selectedWorkflow, step)}
                  >
                    Use Context
                  </button>
                  {checkpointDraft && (
                    <button
                      className="cockpit-feedback-button"
                      aria-label={`Draft retry for step ${step.id} in ${selectedWorkflowName}`}
                      onClick={() => queueComposerDraft(checkpointDraft)}
                    >
                      Draft Retry
                    </button>
                  )}
                  {step.recoveryActions?.length ? (
                    <button
                      className="cockpit-feedback-button"
                      aria-label={`Repair step ${step.id} in ${selectedWorkflowName}`}
                      onClick={() => void runCapabilityActions(readActionList(step.recoveryActions), `${selectedWorkflow.workflowName} ${step.id}`)}
                    >
                      Repair
                    </button>
                  ) : null}
                  {outputPath && (
                    <button
                      className="cockpit-feedback-button"
                      aria-label={`Use step output ${outputPath}`}
                      onClick={() => queueComposerDraft(`Use the workspace file "${outputPath}" as context for the next action.`)}
                    >
                      Use Output
                    </button>
                  )}
                  {outputPath && compatible.map((workflow) => (
                    <button
                      key={`${selectedWorkflow.id}:${step.id}:${workflow.name}`}
                      className="cockpit-feedback-button"
                      aria-label={`Run ${workflow.name} from step output ${outputPath}`}
                      onClick={() => queueArtifactWorkflowDraft(workflow, outputPath)}
                    >
                      Run {workflow.name}
                    </button>
                  ))}
                </div>
              );
            })}
          </div>
        )}
        {selectedInspector.kind === "workflow" && selectedWorkflow && (() => {
          const ancestors = workflowAncestorRuns(selectedWorkflow);
          const childRuns = workflowChildRuns(selectedWorkflow);
          const peerRuns = workflowPeerRuns(selectedWorkflow);
          const branchOriginSummary = workflowBranchOriginSummary(selectedWorkflow);
          const failureLineage = workflowFailureLineage(selectedWorkflow).slice(0, 3);
          const familyOutputs = workflowFamilyArtifactOutputs(selectedWorkflow);
          const selectedWorkflowOutputPath = workflowPrimaryOutputPath(selectedWorkflow);
          if (
            ancestors.length === 0
            && childRuns.length === 0
            && peerRuns.length === 0
            && branchOriginSummary.length === 0
            && !selectedWorkflowBestContinuation
            && failureLineage.length === 0
            && familyOutputs.length === 0
          ) {
            return null;
          }
          return (
            <div className="cockpit-inspector-stack">
              {branchOriginSummary.length > 0 && (
                <div className="cockpit-inspector-stack-row">
                  <div className="cockpit-key">branch origin</div>
                  <div className="cockpit-value">{branchOriginSummary.join(" · ")}</div>
                  {selectedWorkflow.parentRunIdentity && (
                    <button
                      className="cockpit-feedback-button"
                      onClick={() => inspectWorkflowRun(workflowRunByIdentity.get(selectedWorkflow.parentRunIdentity ?? ""))}
                    >
                      Open Parent
                    </button>
                  )}
                </div>
              )}
              {selectedWorkflowBestContinuation && (
                <div className="cockpit-inspector-stack-row">
                  <div className="cockpit-key">best continuation</div>
                  <div className="cockpit-value">
                    {selectedWorkflowBestContinuation.workflowName}
                    {" · "}
                    {selectedWorkflowBestContinuation.summary}
                    {" · "}
                    {workflowSupervisionLabel(selectedWorkflowBestContinuation)}
                    {" · "}
                    {formatAge(selectedWorkflowBestContinuation.updatedAt)}
                  </div>
                  <button
                    className="cockpit-feedback-button"
                    aria-label={`Open best continuation for ${selectedWorkflowName}`}
                    onClick={() => inspectWorkflowRun(selectedWorkflowBestContinuation)}
                  >
                    Open
                  </button>
                  <button
                    className="cockpit-feedback-button"
                    aria-label={`Continue best continuation for ${selectedWorkflowName}`}
                    onClick={() => continueWorkflowRun(selectedWorkflowBestContinuation)}
                  >
                    Continue
                  </button>
                  {(selectedWorkflowBestContinuation.artifacts[0]?.filePath ?? selectedWorkflowBestContinuation.artifactPaths[0]) && (
                    <button
                      className="cockpit-feedback-button"
                      aria-label={`Use latest output from best continuation for ${selectedWorkflowName}`}
                      onClick={() => queueWorkflowOutputContext(selectedWorkflowBestContinuation)}
                    >
                      Use Output
                    </button>
                  )}
                  {selectedWorkflowOutputPath && workflowPrimaryOutputPath(selectedWorkflowBestContinuation) && (
                    <button
                      className="cockpit-feedback-button"
                      aria-label={`Compare best continuation output for ${selectedWorkflowName}`}
                      onClick={() => queueWorkflowOutputComparison(selectedWorkflow, selectedWorkflowBestContinuation)}
                    >
                      Compare
                    </button>
                  )}
                </div>
              )}
              {(selectedWorkflowOutputPath || selectedWorkflowBestContinuation || failureLineage.length > 0 || familyOutputs.length > 0) && (
                <div className="cockpit-inspector-stack-row">
                  <div className="cockpit-key">next step</div>
                  <div className="cockpit-value">Bundle current workflow-family state into one continuation-planning draft.</div>
                  <button
                    className="cockpit-feedback-button"
                    aria-label={`Draft next step from workflow family for ${selectedWorkflowName}`}
                    onClick={() => queueWorkflowFamilyPlan(selectedWorkflow)}
                  >
                    Draft Next Step
                  </button>
                </div>
              )}
              {failureLineage.map((entry, index) => (
                <div key={`${selectedWorkflow.id}:failure-lineage:${entry.runIdentity ?? entry.id}`} className="cockpit-inspector-stack-row">
                  <div className="cockpit-key">{index === 0 ? "failure lineage" : "failure branch"}</div>
                  <div className="cockpit-value">
                    {entry.workflowName} · {entry.summary} · {workflowSupervisionLabel(entry)} · {formatAge(entry.updatedAt)}
                  </div>
                  <button
                    className="cockpit-feedback-button"
                    aria-label={`Open failure lineage branch ${entry.workflowName}`}
                    onClick={() => inspectWorkflowRun(entry)}
                  >
                    Open
                  </button>
                </div>
              ))}
              {familyOutputs.map((output) => (
                <div key={`${selectedWorkflow.id}:family-output:${output.key}`} className="cockpit-inspector-stack-row">
                  <div className="cockpit-key">family output</div>
                  <div className="cockpit-value">
                    {output.filePath}
                    {" · "}
                    {output.sourceWorkflow.workflowName}
                    {" · "}
                    {output.sourceLabel}
                    {" · "}
                    {formatAge(output.createdAt)}
                  </div>
                  <button
                    className="cockpit-feedback-button"
                    aria-label={`Open workflow for family output ${output.filePath} from ${shortIdentifier(output.sourceWorkflow.runIdentity ?? output.sourceWorkflow.id)}`}
                    onClick={() => inspectWorkflowRun(output.sourceWorkflow)}
                  >
                    Open Run
                  </button>
                  <button
                    className="cockpit-feedback-button"
                    aria-label={`Use family output ${output.filePath} from ${shortIdentifier(output.sourceWorkflow.runIdentity ?? output.sourceWorkflow.id)}`}
                    onClick={() => queueComposerDraft(`Use the workspace file "${output.filePath}" as context for the next action.`)}
                  >
                    Use Output
                  </button>
                  {selectedWorkflowOutputPath && (
                    <button
                      className="cockpit-feedback-button"
                      aria-label={`Compare family output ${output.filePath} from ${shortIdentifier(output.sourceWorkflow.runIdentity ?? output.sourceWorkflow.id)}`}
                      onClick={() => queueWorkflowOutputComparison(selectedWorkflow, output.sourceWorkflow)}
                    >
                      Compare
                    </button>
                  )}
                </div>
              ))}
              {ancestors.map((entry, index) => (
                <div key={`${selectedWorkflow.id}:ancestor:${entry.runIdentity ?? entry.id}`} className="cockpit-inspector-stack-row">
                  <div className="cockpit-key">{index === 0 ? "parent run" : "ancestor run"}</div>
                  <div className="cockpit-value">
                    {[
                      entry.workflowName,
                      entry.status,
                      workflowSupervisionLabel(entry),
                      ...workflowComparisonSummary(selectedWorkflow, entry),
                      formatAge(entry.updatedAt),
                    ].join(" · ")}
                  </div>
                  <button
                    className="cockpit-feedback-button"
                    onClick={() => inspectWorkflowRun(entry)}
                  >
                    Open
                  </button>
                  {selectedWorkflowOutputPath && workflowPrimaryOutputPath(entry) && (
                    <button
                      className="cockpit-feedback-button"
                      aria-label={`Compare ancestor output ${entry.artifactPaths[0] ?? entry.artifacts[0]?.filePath}`}
                      onClick={() => queueWorkflowOutputComparison(selectedWorkflow, entry)}
                    >
                      Compare
                    </button>
                  )}
                </div>
              ))}
              {childRuns.map((entry) => (
                <div key={`${selectedWorkflow.id}:child:${entry.runIdentity ?? entry.id}`} className="cockpit-inspector-stack-row">
                  <div className="cockpit-key">child branch</div>
                  <div className="cockpit-value">
                    {[
                      entry.workflowName,
                      entry.status,
                      workflowSupervisionLabel(entry),
                      entry.resumeCheckpointLabel ?? entry.branchKind ?? "branch",
                      ...workflowComparisonSummary(selectedWorkflow, entry),
                      formatAge(entry.updatedAt),
                    ].join(" · ")}
                  </div>
                  <button
                    className="cockpit-feedback-button"
                    onClick={() => inspectWorkflowRun(entry)}
                  >
                    Open
                  </button>
                  <button
                    className="cockpit-feedback-button"
                    onClick={() => continueWorkflowRun(entry)}
                  >
                    Continue
                  </button>
                  {(entry.artifacts[0]?.filePath ?? entry.artifactPaths[0]) && (
                    <button
                      className="cockpit-feedback-button"
                      aria-label={`Use child branch output ${entry.artifactPaths[0] ?? entry.artifacts[0]?.filePath}`}
                      onClick={() => queueWorkflowOutputContext(entry)}
                    >
                      Use Output
                    </button>
                  )}
                  {selectedWorkflowOutputPath && workflowPrimaryOutputPath(entry) && (
                    <button
                      className="cockpit-feedback-button"
                      aria-label={`Compare child branch output ${entry.artifactPaths[0] ?? entry.artifacts[0]?.filePath}`}
                      onClick={() => queueWorkflowOutputComparison(selectedWorkflow, entry)}
                    >
                      Compare
                    </button>
                  )}
                </div>
              ))}
              {peerRuns.map((entry) => (
                <div key={`${selectedWorkflow.id}:peer:${entry.runIdentity ?? entry.id}`} className="cockpit-inspector-stack-row">
                  <div className="cockpit-key">peer branch</div>
                  <div className="cockpit-value">
                    {[
                      entry.workflowName,
                      entry.status,
                      workflowSupervisionLabel(entry),
                      entry.resumeCheckpointLabel ?? entry.branchKind ?? "branch",
                      ...workflowComparisonSummary(selectedWorkflow, entry),
                      formatAge(entry.updatedAt),
                    ].join(" · ")}
                  </div>
                  <button
                    className="cockpit-feedback-button"
                    onClick={() => inspectWorkflowRun(entry)}
                  >
                    Open
                  </button>
                  {(entry.artifacts[0]?.filePath ?? entry.artifactPaths[0]) && (
                    <button
                      className="cockpit-feedback-button"
                      aria-label={`Use peer branch output ${entry.artifactPaths[0] ?? entry.artifacts[0]?.filePath}`}
                      onClick={() => queueWorkflowOutputContext(entry)}
                    >
                      Use Output
                    </button>
                  )}
                  {selectedWorkflowOutputPath && workflowPrimaryOutputPath(entry) && (
                    <button
                      className="cockpit-feedback-button"
                      aria-label={`Compare peer branch output ${entry.artifactPaths[0] ?? entry.artifacts[0]?.filePath}`}
                      onClick={() => queueWorkflowOutputComparison(selectedWorkflow, entry)}
                    >
                      Compare
                    </button>
                  )}
                </div>
              ))}
            </div>
          );
        })()}
        {selectedInspector.kind === "workflow" && selectedWorkflow?.timeline?.length && (
          <div className="cockpit-inspector-stack">
            {selectedWorkflow.timeline.map((entry) => (
              (() => {
                const checkpointStepId = entry.stepId ?? null;
                const checkpointDraft = checkpointStepId
                  ? selectedWorkflowCheckpointDraftByStep.get(checkpointStepId) ?? null
                  : null;
                return (
                  <div key={`${selectedWorkflow.id}:${entry.kind}:${entry.at}`} className="cockpit-inspector-stack-row">
                    <div className="cockpit-key">{entry.kind.replace(/_/g, " ")}</div>
                    <div className="cockpit-value">
                      {entry.summary}
                      {entry.stepId ? ` · ${entry.stepId}` : ""}
                      {entry.durationMs ? ` · ${entry.durationMs}ms` : ""}
                    </div>
                    {checkpointDraft && (
                      <button
                        className="cockpit-feedback-button"
                        aria-label={`Draft retry from ${checkpointStepId} for ${selectedWorkflowName}`}
                        onClick={() => queueComposerDraft(checkpointDraft)}
                      >
                        Draft Retry
                      </button>
                    )}
                  </div>
                );
              })()
            ))}
          </div>
        )}
        {selectedInspector.kind === "operator" && (
          <div className="cockpit-feedback-row">
            {(selectedInspector.entity.entityType === "workflow_definition"
              || selectedInspector.entity.entityType === "skill"
              || selectedInspector.entity.entityType === "mcp") && (
              <button
                className="cockpit-feedback-button"
                onClick={() => openExtensionStudioForEntity(selectedInspector.entity)}
              >
                Open Studio
              </button>
            )}
            {typeof selectedInspector.entity.details.file_path === "string" && (
              <>
                <button
                  className="cockpit-feedback-button"
                  onClick={() =>
                    queueComposerDraft(
                      `Use the definition at "${String(selectedInspector.entity.details.file_path)}" as context for the next editing step.`,
                    )
                  }
                >
                  Use Path
                </button>
                <button
                  className="cockpit-feedback-button"
                  onClick={() =>
                    queueComposerDraft(
                      `Open "${String(selectedInspector.entity.details.file_path)}" and update the ${selectedInspector.entity.entityType.replace("_", " ")} "${selectedInspector.entity.name}" based on the current blocker state.`,
                    )
                  }
                >
                  Draft Authoring
                </button>
              </>
            )}
            {Array.isArray(selectedInspector.entity.details.recommended_actions)
              && selectedInspector.entity.details.recommended_actions.length > 0 && (
              <button
                className="cockpit-feedback-button"
                onClick={() =>
                  void runCapabilityActions(
                    selectedInspector.entity.details.recommended_actions as CapabilityAction[],
                    selectedInspector.entity.name,
                  )
                }
              >
                Repair
              </button>
            )}
            {selectedInspector.entity.entityType === "workflow_definition" && (
              <button
                className="cockpit-feedback-button"
                onClick={() => void refreshStudioValidation()}
                disabled={studioBusy === "validation"}
              >
                Validate
              </button>
            )}
            {selectedInspector.entity.entityType === "mcp"
              && Boolean(selectedInspector.entity.details.auth_hint || selectedInspector.entity.details.status_message) && (
              <button
                className="cockpit-feedback-button"
                onClick={() => setSettingsPanelOpen(true)}
              >
                Open Settings
              </button>
            )}
          </div>
        )}
        {selectedInspector.kind === "artifact" && (
          <div className="cockpit-feedback-row">
            <button
              className="cockpit-feedback-button"
              onClick={() =>
                queueComposerDraft(
                  `Use the workspace file "${selectedInspector.artifact.filePath}" as context for the next action.`,
                )
              }
              >
                Use In Command Bar
              </button>
            {compatibleArtifactWorkflows(selectedInspector.artifact.filePath).slice(0, 2).map((workflow) => (
              <button
                key={`${selectedInspector.artifact.id}:${workflow.name}`}
                className="cockpit-feedback-button"
                onClick={() =>
                  queueArtifactWorkflowDraft(workflow, selectedInspector.artifact.filePath)
                }
              >
                Run {workflow.name}
              </button>
            ))}
          </div>
        )}
        <div className="cockpit-inspector-details">
          {Object.entries(details).map(([key, value]) => (
            <div key={key} className="cockpit-inspector-detail">
              <div className="cockpit-key">{key.replace(/_/g, " ")}</div>
              <pre className="cockpit-inspector-value">{formatInspectorValue(value)}</pre>
            </div>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="cockpit-shell">
      <header className="cockpit-topbar">
        <div className="cockpit-brand">
          <div className="cockpit-eyebrow cockpit-brandmark">SERAPH</div>
          <div className="cockpit-toolbar-hint">{SERAPH_BUILD_ID}</div>
        </div>

        <div className="cockpit-topbar-right">
          <div className="cockpit-pill-row">
            <span className={`cockpit-pill cockpit-pill--${connectionStatus}`}>{connectionLabel}</span>
            <span className="cockpit-pill">{ambientState.replace("_", " ")}</span>
            <span className="cockpit-pill">
              desktop {daemonPresence?.connected ? "live" : "offline"}
            </span>
            {daemonPresence && (
              <button
                type="button"
                className="cockpit-pill"
                onClick={() => setSettingsPanelOpen(true)}
                title="Open settings to inspect or dismiss pending desktop notifications"
              >
                native {daemonPresence.pending_notification_count} queued
              </button>
            )}
            <button
              type="button"
              className="cockpit-pill"
              onClick={() => setSettingsPanelOpen(true)}
              title="Open settings to inspect deferred bundle items and recent guardian continuity"
            >
              bundle {queuedBundleCount} queued
            </button>
            <span className="cockpit-pill">
              budget {observerState?.attention_budget_remaining ?? "?"}
            </span>
            <span className="cockpit-pill">
              {(observerState?.data_quality ?? ambientTooltip) || "state pending"}
            </span>
          </div>

          <div className="cockpit-action-row">
            {onboardingCompleted === false && onSkipOnboarding && (
              <button className="cockpit-action cockpit-action--ghost" onClick={onSkipOnboarding}>
                Skip intro
              </button>
            )}
            <button
              className="cockpit-action cockpit-action--ghost"
              onClick={() => newSession()}
              title="Start a blank conversation thread. Earlier sessions stay in the Sessions pane."
            >
              Start fresh
            </button>
            <button
              className="cockpit-action cockpit-action--ghost"
              onClick={() => setQuestPanelOpen(true)}
            >
              Priorities overlay
            </button>
            <button
              className="cockpit-action cockpit-action--ghost"
              onClick={() => setPaletteOpen(true)}
              title="Open the capability command palette"
            >
              Capability palette
            </button>
            <button
              className="cockpit-action cockpit-action--ghost"
              onClick={() => {
                setStudioSelectedId(studioEntries[0]?.id ?? null);
                setStudioOpen(true);
              }}
              title="Open the extension studio for workflows, skills, and MCP configuration"
            >
              Extension studio
            </button>
            <button
              className="cockpit-action cockpit-action--ghost"
              onClick={() => setSettingsPanelOpen(true)}
            >
              Settings
            </button>
          </div>
        </div>

        <div className="cockpit-topbar-bottom">
          <div className="cockpit-menu-anchor" ref={windowsMenuRef}>
            <button
              className={`cockpit-action cockpit-action--ghost ${windowsMenuOpen ? "cockpit-action--active" : ""}`}
              onClick={() => setWindowsMenuOpen((current) => !current)}
              title="Show or hide workspace windows"
              aria-label="Windows"
            >
              Windows {visiblePaneCount}/{COCKPIT_PANES.length}
            </button>
            {windowsMenuOpen && (
              <div className="cockpit-windows-menu cockpit-windows-menu--brand cockpit-window-launcher-drawer retro-scrollbar">
                <div className="cockpit-window-launcher-header">
                  <div className="cockpit-window-launcher-title">Windows</div>
                  <div className="cockpit-window-launcher-meta">{visiblePaneCount}/{COCKPIT_PANES.length} visible</div>
                </div>
                <div className="cockpit-windows-menu-toolbar">
                  <button className="cockpit-windows-menu-action" onClick={() => showAllPanes()}>
                    Show all
                  </button>
                  <button className="cockpit-windows-menu-action" onClick={() => hideNonCorePanes()}>
                    Hide non-core
                  </button>
                  <button className="cockpit-windows-menu-action" onClick={handleResetWorkspace}>
                    Reset workspace
                  </button>
                </div>
                {panesByGroup.map(([group, panes]) => (
                  <div key={group} className="cockpit-windows-menu-group">
                    <div className="cockpit-windows-menu-group-title">{group}</div>
                    {panes.map((pane) => {
                      const isCorePane = CORE_PANE_IDS.includes(pane.id);
                      return (
                        <div key={pane.id} className="cockpit-windows-menu-row">
                          <button
                            className={`cockpit-windows-menu-toggle ${paneVisibility[pane.id] ? "active" : ""}`}
                            onClick={() => togglePaneVisible(pane.id)}
                          >
                            <span className="cockpit-windows-menu-check">{paneVisibility[pane.id] ? "■" : "□"}</span>
                            <span>{pane.label}</span>
                            {isCorePane ? <span className="cockpit-windows-menu-core">core</span> : null}
                          </button>
                          <button
                            className="cockpit-windows-menu-focus"
                            onClick={() => focusPane(pane.id)}
                          >
                            Focus
                          </button>
                        </div>
                      );
                    })}
                  </div>
                ))}
              </div>
            )}
          </div>

          <div className="cockpit-layout-row">
            {Object.values(COCKPIT_LAYOUTS).map((layout) => (
              <button
                key={layout.id}
                className={`cockpit-action cockpit-action--ghost ${
                  activeLayoutId === layout.id ? "cockpit-action--active" : ""
                }`}
                onClick={() => handleSelectLayout(layout.id)}
                title={layout.description}
              >
                {layout.label}
              </button>
            ))}
            <button className="cockpit-action cockpit-action--ghost" onClick={handleResetWorkspace}>
              Reset view
            </button>
            <button className="cockpit-action cockpit-action--ghost" onClick={handleSaveWorkspace}>
              Save view
            </button>
          </div>
        </div>
      </header>

      {cockpitHintsEnabled && (
        <section className="cockpit-hint-strip" aria-label="Workspace hints">
          {COCKPIT_GLOBAL_HINTS.map((hint) => (
            <span key={hint} className="cockpit-hint-chip">
              {hint}
            </span>
          ))}
        </section>
      )}

      <div className="cockpit-workspace">
        {visibleSections.rail && (
          <>
            {paneVisibility.sessions_pane && (
              <CockpitWorkspaceWindow
                panelId="sessions_pane"
                title="Sessions"
                meta={activeSession ? activeSession.title : "fresh thread"}
                hint={COCKPIT_WINDOW_HINTS.sessions}
                showHint={cockpitHintsEnabled}
                minWidth={260}
                minHeight={180}
                onClose={() => closeWindowPane("sessions_pane")}
              >
                <section className="cockpit-panel cockpit-panel--embedded">
                  <div className="cockpit-session-helper">
                    <div className="cockpit-key">thread control</div>
                    <div className="cockpit-session-helper-row">
                      <div className="cockpit-session-helper-text">
                        Start fresh opens a blank thread and keeps earlier sessions in the list. Seraph names it after the first completed reply.
                      </div>
                      <button
                        type="button"
                        className="cockpit-feedback-button"
                        onClick={() => newSession()}
                      >
                        Start fresh
                      </button>
                    </div>
                  </div>
                  <div className="cockpit-list">
                    {sessions.slice(0, 8).map((session) => (
                      <button
                        key={session.id}
                        className={`cockpit-session ${session.id === sessionId ? "active" : ""}`}
                        onClick={() => {
                          clearSessionContinuity(session.id);
                          switchSession(session.id, "live");
                        }}
                      >
                        <span className="cockpit-session-title">
                          {session.title}
                          {sessionContinuity[session.id] && (
                            <span className="cockpit-session-badge">
                              {sessionContinuity[session.id] === "new_activity"
                                ? "new activity"
                                : sessionContinuity[session.id]}
                            </span>
                          )}
                        </span>
                        <span className="cockpit-session-meta">{formatAge(session.updated_at)}</span>
                      </button>
                    ))}
                    {sessions.length === 0 && (
                      <div className="cockpit-empty">No saved sessions yet.</div>
                    )}
                  </div>
                </section>
              </CockpitWorkspaceWindow>
            )}

            {paneVisibility.goals_pane && (
              <CockpitWorkspaceWindow
                panelId="goals_pane"
                title="Priorities"
                meta={loadingGoals ? "refreshing" : `${dashboard?.active_count ?? 0} active`}
                hint={COCKPIT_WINDOW_HINTS.goals}
                showHint={cockpitHintsEnabled}
                minWidth={280}
                minHeight={220}
                onClose={() => closeWindowPane("goals_pane")}
              >
                <section className="cockpit-panel cockpit-panel--embedded">
                  {dashboard ? (
                    <div className="cockpit-domain-stack">
                      {Object.entries(dashboard.domains).map(([domain, stat]) => (
                        <div key={domain} className="cockpit-domain-row">
                          <div className="cockpit-domain-label">{domain.replace("_", " ")}</div>
                          <div className="cockpit-domain-bar">
                            <div
                              className="cockpit-domain-fill"
                              style={{ width: `${stat.progress}%` }}
                            />
                          </div>
                          <div className="cockpit-domain-value">{stat.progress}%</div>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className="cockpit-empty">Priority board unavailable.</div>
                  )}
                  <div className="cockpit-sublist">
                    {topGoals.map((goal) => (
                      <div key={goal} className="cockpit-sublist-item">
                        {goal}
                      </div>
                    ))}
                    {topGoals.length === 0 && <div className="cockpit-empty">No active priorities yet.</div>}
                  </div>
                </section>
              </CockpitWorkspaceWindow>
            )}

            {paneVisibility.outputs_pane && (
              <CockpitWorkspaceWindow
                panelId="outputs_pane"
                title="Recent outputs"
                meta={`${artifacts.length} files`}
                hint={COCKPIT_WINDOW_HINTS.outputs}
                showHint={cockpitHintsEnabled}
                minWidth={280}
                minHeight={180}
                onClose={() => closeWindowPane("outputs_pane")}
              >
                <section className="cockpit-panel cockpit-panel--embedded">
                  <div className="cockpit-sublist">
                    {artifacts.map((artifact) => (
                      <button
                        key={artifact.id}
                        className={`cockpit-sublist-button ${
                          selectedInspector?.kind === "artifact" && selectedInspector.artifact.id === artifact.id
                            ? "active"
                            : ""
                        }`}
                        onClick={() => setSelectedInspector({ kind: "artifact", artifact })}
                      >
                        <span>{artifact.filePath}</span>
                        <span className="cockpit-row-age">{formatAge(artifact.createdAt)}</span>
                      </button>
                    ))}
                    {artifacts.length === 0 && (
                      <div className="cockpit-empty">No recent file outputs in the current audit window.</div>
                    )}
                  </div>
                </section>
              </CockpitWorkspaceWindow>
            )}

            {paneVisibility.approvals_pane && (
              <CockpitWorkspaceWindow
                panelId="approvals_pane"
                title="Pending approvals"
                meta={`${pendingApprovals.length} waiting`}
                hint={COCKPIT_WINDOW_HINTS.approvals}
                showHint={cockpitHintsEnabled}
                minWidth={300}
                minHeight={220}
                onClose={() => closeWindowPane("approvals_pane")}
              >
                <section className="cockpit-panel cockpit-panel--embedded">
                  <div className="cockpit-list">
                    {pendingApprovals.map((approval) => (
                      <div key={approval.id} className="cockpit-row">
                      <button
                        className={`cockpit-row-button ${
                          selectedInspector?.kind === "approval" && selectedInspector.approval.id === approval.id
                            ? "active"
                            : ""
                        }`}
                        onClick={() => setSelectedInspector({ kind: "approval", approval })}
                      >
                        <div className="cockpit-row-header">
                          <span className="cockpit-role">{approval.tool_name}</span>
                          <span className="cockpit-row-age">{formatAge(approval.created_at)}</span>
                        </div>
                        <div className="cockpit-row-body">{approval.summary}</div>
                        <div className="cockpit-row-meta">
                          {approval.risk_level} risk
                          {approval.thread_label
                            ? ` · ${approval.thread_label}`
                            : approval.thread_id
                              ? ` · thread ${approval.thread_id.slice(0, 6)}`
                              : ""}
                        </div>
                      </button>
                      <div className="cockpit-feedback-row">
                        {approval.resume_message && (
                          <button
                            className="cockpit-feedback-button"
                            onClick={() =>
                              void queueThreadDraft(
                                approval.resume_message ?? "",
                                approval.thread_id ?? approval.session_id,
                              )
                            }
                          >
                            Continue
                          </button>
                        )}
                        {(approval.thread_id || approval.session_id) && (
                          <button
                            className="cockpit-feedback-button"
                            onClick={() => void openThread(approval.thread_id ?? approval.session_id)}
                          >
                            Open Thread
                          </button>
                        )}
                        <button
                          className="cockpit-feedback-button"
                          onClick={() => void handleApprovalDecision(approval, "approve")}
                        >
                          Approve
                        </button>
                        <button
                          className="cockpit-feedback-button"
                          onClick={() => void handleApprovalDecision(approval, "deny")}
                        >
                          Deny
                        </button>
                        <span className="cockpit-feedback-status">
                          {approvalState[approval.id] ?? "pending"}
                        </span>
                      </div>
                      </div>
                    ))}
                    {pendingApprovals.length === 0 && (
                      <div className="cockpit-empty">No pending approvals.</div>
                    )}
                  </div>
                </section>
              </CockpitWorkspaceWindow>
            )}
          </>
        )}

        {paneVisibility.response_pane && (latestResponse || isAgentBusy) && (
          <CockpitWorkspaceWindow
            panelId="response_pane"
            title="Latest response"
            meta={
              latestResponse
                ? `${labelForRole(latestResponse)} · ${formatAge(latestResponse.timestamp)}`
                : "awaiting first reply"
            }
            hint="The newest assistant output stays pinned here while you keep the rest of the workspace visible."
            showHint={cockpitHintsEnabled}
            minWidth={420}
            minHeight={160}
            onClose={() => closeWindowPane("response_pane")}
          >
            <section className="cockpit-panel cockpit-panel--embedded cockpit-panel--response">
              {isAgentBusy && (
                <div className="cockpit-pending-response" aria-live="polite">
                  <div className="cockpit-row-header">
                    <span className="cockpit-role">status</span>
                    <span className="cockpit-row-meta">agent responding</span>
                  </div>
                  <div className="cockpit-pending-copy">
                    Seraph is responding
                    <span className="cockpit-thinking-inline" aria-hidden="true">
                      <span className="thinking-dot">.</span>
                      <span className="thinking-dot">.</span>
                      <span className="thinking-dot">.</span>
                    </span>
                  </div>
                </div>
              )}
              {latestResponse ? (
                <div className="cockpit-response-body">{latestResponse.content}</div>
              ) : (
                <div className="cockpit-empty cockpit-response-empty">
                  No response yet. Send a message below to begin this thread.
                </div>
              )}
            </section>
          </CockpitWorkspaceWindow>
        )}

        {visibleSections.guardianState && (
          <CockpitWorkspaceWindow
            panelId="guardian_state_pane"
            title="Guardian state"
            meta={`${observerState?.time_of_day ?? "pending"} · ${observerState?.day_of_week ?? "today"}`}
            hint={COCKPIT_WINDOW_HINTS.guardianState}
            showHint={cockpitHintsEnabled}
            minWidth={420}
            minHeight={260}
            onClose={() => closeWindowPane("guardian_state_pane")}
          >
            <section className="cockpit-panel cockpit-panel--embedded">
              <div className="cockpit-state-grid">
                <div>
                  <div className="cockpit-key">user state</div>
                  <div className="cockpit-value">{observerState?.user_state ?? "unknown"}</div>
                </div>
                <div>
                  <div className="cockpit-key">interrupt mode</div>
                  <div className="cockpit-value">{observerState?.interruption_mode ?? "unknown"}</div>
                </div>
                <div>
                  <div className="cockpit-key">active window</div>
                  <div className="cockpit-value">{observerState?.active_window ?? "not observed"}</div>
                </div>
                <div>
                  <div className="cockpit-key">work hours</div>
                  <div className="cockpit-value">
                    {observerState?.is_working_hours ? "within window" : "outside window"}
                  </div>
                </div>
              </div>
              <div className="cockpit-context-block">
                <div className="cockpit-key">screen context</div>
                <div className="cockpit-value cockpit-value--multiline">
                  {observerState?.screen_context ?? "No screen context ingested yet."}
                </div>
              </div>
              <div className="cockpit-context-block">
                <div className="cockpit-key">active goals</div>
                <div className="cockpit-value cockpit-value--multiline">
                  {observerState?.active_goals_summary ?? "Goal summary unavailable."}
                </div>
              </div>
              <div className="cockpit-context-block">
                <div className="cockpit-key">upcoming events</div>
                <div className="cockpit-value cockpit-value--multiline">
                  {observerState?.upcoming_events?.length
                    ? observerState.upcoming_events
                      .slice(0, 3)
                      .map((event) => event.summary || "Untitled event")
                      .join(" • ")
                    : "No upcoming events loaded."}
                </div>
              </div>
            </section>
          </CockpitWorkspaceWindow>
        )}

        {visibleSections.timeline && (
          <CockpitWorkspaceWindow
            panelId="operator_timeline_pane"
            title="Activity ledger"
            meta={`${visibleActivityGroups.length} groups · ${activitySummary?.llm_call_count ?? 0} llm`}
            hint={COCKPIT_WINDOW_HINTS.operatorTimeline}
            showHint={cockpitHintsEnabled}
            minWidth={360}
            minHeight={220}
            onClose={() => closeWindowPane("operator_timeline_pane")}
          >
            <section className="cockpit-panel cockpit-panel--embedded">
              <div className="cockpit-ledger-toolbar">
                <div className="cockpit-ledger-summary">
                  <span className="cockpit-ledger-badge">
                    spend {formatUsd(activitySummary?.llm_cost_usd ?? 0) ?? "$0.0000"}
                  </span>
                  <span className="cockpit-ledger-badge">
                    {activitySummary?.user_triggered_llm_calls ?? 0} user llm
                  </span>
                  <span className="cockpit-ledger-badge">
                    {activitySummary?.autonomous_llm_calls ?? 0} auto llm
                  </span>
                  <span className="cockpit-ledger-badge">
                    {activitySummary?.failure_count ?? 0} failures
                  </span>
                  {activitySummary?.is_partial ? (
                    <span className="cockpit-ledger-badge">
                      partial {activitySummary.partial_sources?.join(", ") || "window"}
                    </span>
                  ) : null}
                </div>
                {activitySpendByCapabilityFamily.length > 0 ? (
                  <div className="cockpit-ledger-summary">
                    {activitySpendByCapabilityFamily.map((bucket) => (
                      <span key={`family:${bucket.key}`} className="cockpit-ledger-badge">
                        {activitySpendBucketLabel(bucket.key)} {formatUsd(bucket.cost_usd) ?? "$0.0000"} · {bucket.calls}x
                      </span>
                    ))}
                  </div>
                ) : null}
                {activitySpendByRuntimePath.length > 0 ? (
                  <div className="cockpit-ledger-summary">
                    {activitySpendByRuntimePath.map((bucket) => (
                      <span key={`runtime:${bucket.key}`} className="cockpit-ledger-badge">
                        {bucket.key} {formatUsd(bucket.cost_usd) ?? "$0.0000"}
                      </span>
                    ))}
                  </div>
                ) : null}
                <div className="cockpit-ledger-filter-row">
                  {ACTIVITY_LEDGER_FILTERS.map((filter) => (
                    <button
                      key={filter}
                      className={`cockpit-ledger-filter ${activityFilter === filter ? "active" : ""}`}
                      onClick={() => setActivityFilter(filter)}
                    >
                      {activityCategoryLabel(filter)}
                    </button>
                  ))}
                </div>
              </div>
              <div className="cockpit-list">
                {visibleActivityGroups.map((group) => {
                  const actionTarget = activityGroupActionTarget(group);
                  return (
                    <div key={group.key} className="cockpit-row cockpit-ledger-group">
                      <button
                        className="cockpit-row-button cockpit-ledger-parent"
                        onClick={() =>
                          setSelectedInspector({
                            kind: "operator",
                            entity: activityInspectorEntity(group.lead),
                          })
                        }
                      >
                        <div className="cockpit-ledger-line cockpit-ledger-line--primary">
                          <span className="cockpit-ledger-icon">{group.icon}</span>
                          <span className="cockpit-role">{group.title}</span>
                          <span className="cockpit-row-age">{formatAge(group.updatedAt)}</span>
                        </div>
                        <div className="cockpit-ledger-line cockpit-ledger-line--summary">{group.summary}</div>
                        {group.detail ? (
                          <div className="cockpit-ledger-line cockpit-ledger-line--summary">{group.detail}</div>
                        ) : null}
                        <div className="cockpit-row-meta">{group.meta}</div>
                      </button>
                      {group.children.length > 0 && (
                        <div className="cockpit-ledger-children">
                          {group.children.map((child) => {
                            const childItem = child.item;
                            return childItem ? (
                              <button
                                key={child.key}
                                className="cockpit-ledger-child"
                                onClick={() =>
                                  setSelectedInspector({
                                    kind: "operator",
                                    entity: activityInspectorEntity(childItem),
                                  })
                                }
                              >
                                <span className="cockpit-ledger-icon">{child.icon}</span>
                                <span className="cockpit-ledger-child-label">{child.label}</span>
                                <span className="cockpit-ledger-child-summary">{child.summary}</span>
                                <span className="cockpit-ledger-child-meta">{child.meta}</span>
                              </button>
                            ) : (
                              <div key={child.key} className="cockpit-ledger-child cockpit-ledger-child--static">
                                <span className="cockpit-ledger-icon">{child.icon}</span>
                                <span className="cockpit-ledger-child-label">{child.label}</span>
                                <span className="cockpit-ledger-child-summary">{child.summary}</span>
                                <span className="cockpit-ledger-child-meta">{child.meta}</span>
                              </div>
                            );
                          })}
                          {group.footer ? (
                            <div className="cockpit-ledger-footer">{group.footer}</div>
                          ) : null}
                        </div>
                      )}
                      <div className="cockpit-feedback-row">
                        {actionTarget.continue_message && (
                          <button
                            className="cockpit-feedback-button"
                            onClick={() => void queueThreadDraft(actionTarget.continue_message ?? "", actionTarget.thread_id)}
                          >
                            Continue
                          </button>
                        )}
                        {canOpenLedgerThread(actionTarget.thread_id, sessionId, knownSessionIds) && (
                          <button
                            className="cockpit-feedback-button"
                            onClick={() => void openThread(actionTarget.thread_id)}
                          >
                            Open Thread
                          </button>
                        )}
                        {actionTarget.replay_draft && actionTarget.replay_allowed !== false && (
                          <button
                            className="cockpit-feedback-button"
                            onClick={() => queueComposerDraft(actionTarget.replay_draft ?? "")}
                          >
                            Replay
                          </button>
                        )}
                        {actionTarget.recommended_actions?.length ? (
                          <button
                            className="cockpit-feedback-button"
                            onClick={() => void runCapabilityActions(actionTarget.recommended_actions ?? [], actionTarget.title)}
                          >
                            Repair
                          </button>
                        ) : null}
                      </div>
                    </div>
                  );
                })}
                {visibleActivityGroups.length === 0 && (
                  <div className="cockpit-empty">No recent activity ledger entries for this filter.</div>
                )}
              </div>
            </section>
          </CockpitWorkspaceWindow>
        )}

        {visibleSections.workflows && (
          <CockpitWorkspaceWindow
            panelId="workflows_pane"
            title="Workflow timeline"
            meta={`${workflowRunsWithArtifacts.length} recent`}
            hint={COCKPIT_WINDOW_HINTS.workflowTimeline}
            showHint={cockpitHintsEnabled}
            minWidth={380}
            minHeight={220}
            onClose={() => closeWindowPane("workflows_pane")}
          >
            <section className="cockpit-panel cockpit-panel--embedded">
              <div className="cockpit-list">
                {workflowRunsWithArtifacts.map((workflow) => {
                  const approval = approvalForWorkflow(workflow);
                  const linkedInterventions = interventionsForWorkflow(workflow);
                  const failedStep = failedWorkflowStep(workflow);
                  return (
                    <div key={workflow.id} className="cockpit-row">
                      <button
                        className={`cockpit-row-button ${
                          selectedInspector?.kind === "workflow" && selectedInspector.workflow.id === workflow.id
                            ? "active"
                            : ""
                        }`}
                        onClick={() => inspectWorkflowRun(workflow)}
                      >
                        <div className="cockpit-row-header">
                          <span className="cockpit-role">{workflow.workflowName}</span>
                          <span className="cockpit-row-age">{formatAge(workflow.updatedAt)}</span>
                        </div>
                        <div className="cockpit-row-body">{workflow.summary}</div>
                        <div className="cockpit-row-meta">
                          {workflow.status} · {workflow.artifactPaths.length} artifacts ·{" "}
                          {workflow.riskLevel ?? "unknown"} risk ·{" "}
                          {approval ? "approval waiting" : "no approval"} · {linkedInterventions.length} interventions
                          {(workflow.threadLabel ?? (workflow.threadId ? sessionTitleById[workflow.threadId] : null))
                            ? ` · ${workflow.threadLabel ?? sessionTitleById[workflow.threadId ?? ""]}`
                            : ""}
                          {workflow.availability ? ` · ${workflow.availability}` : ""}
                          {workflow.replayAllowed === false ? ` · replay blocked` : ""}
                          {` · supervision ${workflowSupervisionLabel(workflow)}`}
                          {workflowChildRuns(workflow).length > 0 ? ` · ${workflowChildRuns(workflow).length} branches` : ""}
                        </div>
                        {workflow.stepRecords?.length ? (
                          <div className="cockpit-row-meta">
                            {workflow.stepRecords
                              .slice(0, 3)
                              .map((step) => `${step.id} ${step.status}${step.durationMs ? ` · ${step.durationMs}ms` : ""}${step.resultSummary ? ` · ${step.resultSummary}` : ""}${step.errorSummary ? ` · ${step.errorSummary}` : ""}`)
                              .join(" · ")}
                          </div>
                        ) : null}
                        {workflow.timeline?.length ? (
                          <div className="cockpit-row-meta">
                            {workflow.timeline.map((entry) => `${entry.kind.replace(/_/g, " ")}: ${entry.summary}`).join(" · ")}
                          </div>
                        ) : null}
                        {workflowResumeDetails(workflow).length > 0 ? (
                          <div className="cockpit-row-meta">
                            {workflowResumeDetails(workflow).join(" · ")}
                          </div>
                        ) : null}
                        {workflowStepFocusRecords(workflow).length > 0 ? (
                          <div className="cockpit-row-meta">
                            {workflowStepFocusRecords(workflow)
                              .map((step) => `focus ${workflowStepSummary(step)}`)
                              .join(" · ")}
                          </div>
                        ) : null}
                        {workflowSupervisionSummary(workflow).length > 0 ? (
                          <div className="cockpit-row-meta">
                            {workflowSupervisionSummary(workflow).join(" · ")}
                          </div>
                        ) : null}
                        {workflowBranchDebugSummary(workflow).length > 0 ? (
                          <div className="cockpit-row-meta">
                            {workflowBranchDebugSummary(workflow).join(" · ")}
                          </div>
                        ) : null}
                      </button>
                      <div className="cockpit-feedback-row">
                        {(approval?.resume_message || workflow.threadContinueMessage) && (
                          <button
                            className="cockpit-feedback-button"
                            onClick={() =>
                              void queueThreadDraft(
                                approval?.resume_message ?? workflow.threadContinueMessage ?? "",
                                approval?.thread_id ?? approval?.session_id ?? workflow.threadId ?? workflow.sessionId,
                              )
                            }
                          >
                            Continue
                          </button>
                        )}
                        {approval && (
                          <>
                            <button
                              className="cockpit-feedback-button"
                              onClick={() => void handleApprovalDecision(approval, "approve")}
                            >
                              Approve
                            </button>
                            <button
                              className="cockpit-feedback-button"
                              onClick={() => void handleApprovalDecision(approval, "deny")}
                            >
                              Deny
                            </button>
                          </>
                        )}
                        {(workflow.threadId || workflow.sessionId) && (
                          <button
                            className="cockpit-feedback-button"
                            onClick={() => void openThread(workflow.threadId ?? workflow.sessionId)}
                          >
                            Open Thread
                          </button>
                        )}
                        {workflow.replayAllowed !== false ? (
                          <>
                            <button
                              className="cockpit-feedback-button"
                              onClick={() => queueComposerDraft(workflow.replayDraft ?? buildWorkflowReplayDraft(workflow))}
                            >
                              Draft rerun
                            </button>
                            {workflow.retryFromStepDraft && (
                              <button
                                className="cockpit-feedback-button"
                                onClick={() => queueComposerDraft(workflow.retryFromStepDraft ?? buildWorkflowReplayDraft(workflow))}
                              >
                                Retry step
                              </button>
                            )}
                          </>
                        ) : (
                          <>
                            <span className="cockpit-feedback-status">
                              Replay blocked: {replayBlockCopy(workflow.replayBlockReason)}
                            </span>
                            {workflow.replayRecommendedActions?.length ? (
                              <button
                                className="cockpit-feedback-button"
                                onClick={() => void repairWorkflowReplay(workflow)}
                              >
                                Repair replay
                              </button>
                            ) : null}
                          </>
                        )}
                        {failedStep?.recoveryActions?.length ? (
                          <button
                            className="cockpit-feedback-button"
                            onClick={() => void runCapabilityActions(
                              readActionList(failedStep.recoveryActions),
                              `${workflow.workflowName} ${failedStep.id}`,
                            )}
                          >
                            Repair step
                          </button>
                        ) : null}
                        {failedStep ? (
                          <button
                            className="cockpit-feedback-button"
                            onClick={() => queueWorkflowStepContext(workflow, failedStep)}
                          >
                            Use failure context
                          </button>
                        ) : null}
                        {(workflow.artifacts[0]?.filePath ?? workflow.artifactPaths[0]) ? (
                          <button
                            className="cockpit-feedback-button"
                            onClick={() => queueWorkflowOutputContext(workflow)}
                          >
                            Use latest output
                          </button>
                        ) : null}
                        {studioEntryForWorkflowRun(workflow) ? (
                          <button
                            className="cockpit-feedback-button"
                            onClick={() => openExtensionStudio(studioEntryForWorkflowRun(workflow))}
                          >
                            Studio
                          </button>
                        ) : null}
                        {workflowLatestBranchRun(workflow) ? (
                          <button
                            className="cockpit-feedback-button"
                            onClick={() => inspectWorkflowRun(workflowLatestBranchRun(workflow))}
                          >
                            Open Latest Branch
                          </button>
                        ) : null}
                      </div>
                    </div>
                  );
                })}
                {workflowRunsWithArtifacts.length === 0 && (
                  <div className="cockpit-empty">No recent workflow executions in the current audit window.</div>
                )}
              </div>
            </section>
          </CockpitWorkspaceWindow>
        )}

        {visibleSections.interventions && (
          <CockpitWorkspaceWindow
            panelId="interventions_pane"
            title="Interventions"
            meta={`${recentInterventions.length} recent`}
            hint={COCKPIT_WINDOW_HINTS.interventions}
            showHint={cockpitHintsEnabled}
            minWidth={380}
            minHeight={220}
            onClose={() => closeWindowPane("interventions_pane")}
          >
            <section className="cockpit-panel cockpit-panel--embedded">
              <div className="cockpit-list">
                {recentInterventions.map((message) => (
                  <div key={message.id} className="cockpit-row">
                    <button
                      className={`cockpit-row-button ${
                        selectedInspector?.kind === "intervention" && selectedInspector.intervention.id === message.id
                          ? "active"
                          : ""
                      }`}
                      onClick={() => setSelectedInspector({ kind: "intervention", intervention: message })}
                    >
                      <div className="cockpit-row-header">
                        <span className="cockpit-role">{message.intervention_type}</span>
                        <span className="cockpit-row-age">{formatAge(message.updated_at)}</span>
                      </div>
                      <div className="cockpit-row-body">{message.content_excerpt}</div>
                      <div className="cockpit-row-meta">
                        {formatContinuityLabel(message.continuity_surface)} · {formatContinuityLabel(message.latest_outcome)}
                        {message.thread_label
                          ? ` · ${message.thread_label}`
                          : message.thread_id
                            ? ` · thread ${message.thread_id.slice(0, 6)}`
                            : ""}
                      </div>
                    </button>
                    {message.id && (
                      <div className="cockpit-feedback-row">
                        {(message.thread_id || message.session_id) && (
                          <button
                            className="cockpit-feedback-button"
                            onClick={() => void openThread(message.thread_id ?? message.session_id)}
                          >
                            Open Thread
                          </button>
                        )}
                        <button
                          className="cockpit-feedback-button"
                          onClick={() => sendFeedback(message.id, "helpful")}
                        >
                          Helpful
                        </button>
                        <button
                          className="cockpit-feedback-button"
                          onClick={() => sendFeedback(message.id, "not_helpful")}
                        >
                          Not helpful
                        </button>
                        <span className="cockpit-feedback-status">
                          {feedbackState[message.id] ?? message.feedback_type ?? "unrated"}
                        </span>
                      </div>
                    )}
                  </div>
                ))}
                {recentInterventions.length === 0 && (
                  <div className="cockpit-empty">No proactive interventions yet.</div>
                )}
              </div>
            </section>
          </CockpitWorkspaceWindow>
        )}

        {visibleSections.audit && (
          <CockpitWorkspaceWindow
            panelId="audit_pane"
            title="Audit surface"
            meta={`${auditEvents.length} events`}
            hint={COCKPIT_WINDOW_HINTS.audit}
            showHint={cockpitHintsEnabled}
            minWidth={340}
            minHeight={220}
            onClose={() => closeWindowPane("audit_pane")}
          >
            <section className="cockpit-panel cockpit-panel--embedded">
              <div className="cockpit-list">
                {auditEvents.map((event) => (
                  <button
                    key={event.id}
                    className={`cockpit-row-button ${
                      selectedInspector?.kind === "audit" && selectedInspector.event.id === event.id
                        ? "active"
                        : ""
                    }`}
                    onClick={() => setSelectedInspector({ kind: "audit", event })}
                  >
                    <div className="cockpit-row-header">
                      <span className="cockpit-role">{event.tool_name ?? event.event_type}</span>
                      <span className="cockpit-row-age">{formatAge(event.created_at)}</span>
                    </div>
                    <div className="cockpit-row-body">{event.summary}</div>
                    <div className="cockpit-row-meta">
                      {event.event_type} · {event.risk_level} · {event.policy_mode}
                    </div>
                  </button>
                ))}
                {auditEvents.length === 0 && (
                  <div className="cockpit-empty">No audit events available.</div>
                )}
              </div>
            </section>
          </CockpitWorkspaceWindow>
        )}

        {visibleSections.trace && (
          <CockpitWorkspaceWindow
            panelId="trace_pane"
            title="Live trace"
            meta={isAgentBusy ? "agent active" : "idle"}
            hint={COCKPIT_WINDOW_HINTS.trace}
            showHint={cockpitHintsEnabled}
            minWidth={320}
            minHeight={180}
            onClose={() => closeWindowPane("trace_pane")}
          >
            <section className="cockpit-panel cockpit-panel--embedded">
              <div className="cockpit-list">
                {recentTrace.map((message) => (
                  <button
                    key={message.id}
                    className={`cockpit-row-button ${
                      selectedInspector?.kind === "trace" && selectedInspector.message.id === message.id
                        ? "active"
                        : ""
                    }`}
                    onClick={() => setSelectedInspector({ kind: "trace", message })}
                  >
                    <div className="cockpit-row-header">
                      <span className="cockpit-role">{labelForRole(message)}</span>
                      <span className="cockpit-row-age">{formatAge(message.timestamp)}</span>
                    </div>
                    <div className="cockpit-row-body">{message.content}</div>
                  </button>
                ))}
                {recentTrace.length === 0 && (
                  <div className="cockpit-empty">No live tool or error trace yet.</div>
                )}
              </div>
            </section>
          </CockpitWorkspaceWindow>
        )}

        {visibleSections.inspector && (
          <CockpitWorkspaceWindow
            panelId="inspector_pane"
            title="Operations inspector"
            meta={selectedInspector ? selectedInspector.kind : "nothing selected"}
            hint={COCKPIT_WINDOW_HINTS.inspector}
            showHint={cockpitHintsEnabled}
            minWidth={480}
            minHeight={240}
            onClose={() => closeWindowPane("inspector_pane")}
          >
            <section className="cockpit-panel cockpit-panel--embedded">
              <div className="cockpit-feed">{renderInspector()}</div>
            </section>
          </CockpitWorkspaceWindow>
        )}

        {visibleSections.conversation && (
          <>
            {paneVisibility.presence_pane && (
              <CockpitWorkspaceWindow
                panelId="presence_pane"
                title="Seraph presence"
                meta={connectionStatus === "connected" ? "runtime linked" : connectionLabel}
                hint={COCKPIT_WINDOW_HINTS.presence}
                showHint={false}
                minWidth={368}
                minHeight={256}
                onClose={() => closeWindowPane("presence_pane")}
              >
                {({ isFront }) => <SeraphPresencePane snapshot={seraphPresenceSnapshot} isSelected={isFront} />}
              </CockpitWorkspaceWindow>
            )}

            {paneVisibility.conversation_pane && (
              <CockpitWorkspaceWindow
                panelId="conversation_pane"
                title="Conversation"
                meta={activeSession?.title ?? "fresh thread · saved after first reply"}
                hint={COCKPIT_WINDOW_HINTS.conversation}
                showHint={cockpitHintsEnabled}
                minWidth={360}
                minHeight={260}
                onClose={() => closeWindowPane("conversation_pane")}
              >
              <section className="cockpit-panel cockpit-panel--embedded cockpit-chat-panel">
                <div className="cockpit-feed">
                  {recentConversation.map((message) => (
                    <div key={message.id} className={`cockpit-message cockpit-message--${message.role}`}>
                      <div className="cockpit-row-header">
                        <span className="cockpit-role">{labelForRole(message)}</span>
                        <span className="cockpit-row-age">{formatAge(message.timestamp)}</span>
                      </div>
                      <div className="cockpit-message-body">{message.content}</div>
                    </div>
                  ))}
                  {isAgentBusy && (
                    <div className="cockpit-message cockpit-message--pending" aria-live="polite">
                      <div className="cockpit-row-header">
                        <span className="cockpit-role">status</span>
                        <span className="cockpit-row-age">working</span>
                      </div>
                      <div className="cockpit-message-body cockpit-pending-copy">
                        Seraph is responding
                        <span className="cockpit-thinking-inline" aria-hidden="true">
                          <span className="thinking-dot">.</span>
                          <span className="thinking-dot">.</span>
                          <span className="thinking-dot">.</span>
                        </span>
                      </div>
                    </div>
                  )}
                  {recentConversation.length === 0 && (
                    <div className="cockpit-empty">
                      Fresh thread. Send a message below to start a new saved session.
                    </div>
                  )}
                </div>
              </section>
              </CockpitWorkspaceWindow>
            )}

            {paneVisibility.desktop_shell_pane && (
              <CockpitWorkspaceWindow
                panelId="desktop_shell_pane"
                title="Desktop shell"
                meta={`${daemonPresence?.connected ? "linked" : "offline"} · ${desktopNotifications.length} alerts`}
                hint={COCKPIT_WINDOW_HINTS.desktopShell}
                showHint={cockpitHintsEnabled}
                minWidth={340}
                minHeight={220}
                onClose={() => closeWindowPane("desktop_shell_pane")}
              >
              <section className="cockpit-panel cockpit-panel--embedded">
                <div className="cockpit-sublist">
                  <div className="cockpit-sublist-item">
                    capture {daemonPresence?.capture_mode ?? "unknown"} · bundle {queuedInsights.length} · recent {recentInterventions.length}
                  </div>
                  {desktopRouteStatuses.map((route) => (
                    <div key={route.route} className="cockpit-sublist-item">
                      {route.label}: {formatContinuityLabel(route.status)}
                      {route.selected_transport ? ` via ${formatContinuityLabel(route.selected_transport)}` : ""}
                      {route.repair_hint ? ` · ${route.repair_hint}` : ""}
                    </div>
                  ))}
                </div>
                <div className="cockpit-list">
                  {desktopNotifications.slice(0, 3).map((notification) => (
                    <div key={notification.id} className="cockpit-row">
                      <div className="cockpit-row-header">
                        <span className="cockpit-role">{notification.title}</span>
                        <span className="cockpit-row-age">{formatAge(notification.created_at)}</span>
                      </div>
                      <div className="cockpit-row-body">{notification.body}</div>
                      <div className="cockpit-row-meta">
                        {notification.surface ?? "notification"}
                        {notification.continuation_mode ? ` · ${notification.continuation_mode.replace(/_/g, " ")}` : ""}
                        {(notification.thread_label ?? (notification.thread_id ? sessionTitleById[notification.thread_id] : null))
                          ? ` · ${notification.thread_label ?? sessionTitleById[notification.thread_id ?? ""]}`
                          : notification.thread_id
                            ? ` · thread ${notification.thread_id.slice(0, 6)}`
                            : ""}
                      </div>
                      <div className="cockpit-feedback-row">
                        <button
                          className="cockpit-feedback-button"
                          onClick={() =>
                            void queueThreadDraft(
                              notification.resume_message
                                || `Follow up on this desktop alert: ${notification.body}`,
                              notification.thread_id ?? notification.session_id,
                            )
                          }
                        >
                          Continue
                        </button>
                        {(notification.thread_id || notification.session_id) && (
                          <button
                            className="cockpit-feedback-button"
                            onClick={() => void openThread(notification.thread_id ?? notification.session_id)}
                          >
                            Open Thread
                          </button>
                        )}
                        <button
                          className="cockpit-feedback-button"
                          onClick={() => void dismissDesktopNotification(notification.id)}
                        >
                          Dismiss
                        </button>
                      </div>
                    </div>
                  ))}
                  {desktopNotifications.length > 1 && (
                    <button className="cockpit-feedback-button" onClick={() => void dismissAllDesktopNotifications()}>
                      Dismiss All Alerts
                    </button>
                  )}
                  {queuedInsights.slice(0, 2).map((item) => (
                    <div key={item.id} className="cockpit-row">
                      <div className="cockpit-row-header">
                        <span className="cockpit-role">{item.intervention_type}</span>
                        <span className="cockpit-row-age">{formatAge(item.created_at)}</span>
                      </div>
                      <div className="cockpit-row-body">{item.content_excerpt}</div>
                      <div className="cockpit-row-meta">
                        {item.continuation_mode ? `${formatContinuityLabel(item.continuation_mode)} · ` : ""}
                        {item.thread_label
                          ?? (item.thread_id ? sessionTitleById[item.thread_id] ?? `thread ${item.thread_id.slice(0, 6)}` : "ambient queue")}
                      </div>
                      <div className="cockpit-feedback-row">
                        <button
                          className="cockpit-feedback-button"
                          onClick={() =>
                            void queueThreadDraft(
                              item.resume_message ?? `Follow up on this deferred guardian item: ${item.content_excerpt}`,
                              item.thread_id ?? item.session_id,
                            )
                          }
                        >
                          Draft Follow-up
                        </button>
                        {(item.thread_id || item.session_id) && (
                          <button
                            className="cockpit-feedback-button"
                            onClick={() => void openThread(item.thread_id ?? item.session_id)}
                          >
                            Open Thread
                          </button>
                        )}
                      </div>
                    </div>
                  ))}
                  {recentInterventions.slice(0, 2).map((item) => (
                    <div key={`desktop-${item.id}`} className="cockpit-row">
                      <div className="cockpit-row-header">
                        <span className="cockpit-role">{item.intervention_type}</span>
                        <span className="cockpit-row-age">{formatAge(item.updated_at)}</span>
                      </div>
                      <div className="cockpit-row-body">{item.content_excerpt}</div>
                      <div className="cockpit-row-meta">
                        {formatContinuityLabel(item.continuity_surface)} · {formatContinuityLabel(item.latest_outcome)}
                        {item.continuation_mode ? ` · ${formatContinuityLabel(item.continuation_mode)}` : ""}
                      </div>
                      <div className="cockpit-feedback-row">
                        <button
                          className="cockpit-feedback-button"
                          onClick={() =>
                            void queueThreadDraft(
                              item.resume_message ?? `Continue from this guardian intervention: ${item.content_excerpt}`,
                              item.thread_id ?? item.session_id,
                            )
                          }
                        >
                          Continue
                        </button>
                        {(item.thread_id || item.session_id) && (
                          <button
                            className="cockpit-feedback-button"
                            onClick={() => void openThread(item.thread_id ?? item.session_id)}
                          >
                            Open Thread
                          </button>
                        )}
                        <button
                          className="cockpit-feedback-button"
                          onClick={() => sendFeedback(item.id, "helpful")}
                        >
                          Helpful
                        </button>
                      </div>
                    </div>
                  ))}
                  {desktopNotifications.length === 0 && queuedInsights.length === 0 && recentInterventions.length === 0 && (
                    <div className="cockpit-empty">No desktop continuity items yet.</div>
                  )}
                </div>
              </section>
              </CockpitWorkspaceWindow>
            )}

            {paneVisibility.operator_surface_pane && (
              <CockpitWorkspaceWindow
                panelId="operator_surface_pane"
                title="Operator terminal"
                meta={`tool ${toolPolicyMode} · mcp ${mcpPolicyMode}`}
                hint={COCKPIT_WINDOW_HINTS.operatorTerminal}
                showHint={cockpitHintsEnabled}
                minWidth={360}
                minHeight={260}
                onClose={() => closeWindowPane("operator_surface_pane")}
              >
                <section className="cockpit-panel cockpit-panel--embedded">
                <div className="cockpit-state-grid">
                  <div>
                    <div className="cockpit-key">approval</div>
                    <div className="cockpit-value">{approvalMode}</div>
                  </div>
                  <div>
                    <div className="cockpit-key">visible tools</div>
                    <div className="cockpit-value">
                      {tools.length - blockedTools.length}/{tools.length} ready · {highRiskTools.length} high risk
                    </div>
                  </div>
                  <div>
                    <div className="cockpit-key">skills</div>
                    <div className="cockpit-value">
                      {readySkills.length}/{skills.length} ready
                    </div>
                  </div>
                  <div>
                    <div className="cockpit-key">mcp</div>
                    <div className="cockpit-value">
                      {readyMcpServers.length}/{mcpServers.length} ready · {mcpTools.length} tools
                    </div>
                  </div>
                  <div>
                    <div className="cockpit-key">workflows</div>
                    <div className="cockpit-value">
                      {availableWorkflows.length}/{workflows.length} available
                    </div>
                  </div>
                  <div>
                    <div className="cockpit-key">starter packs</div>
                    <div className="cockpit-value">
                      {readyStarterPacks.length}/{starterPacks.length} ready
                    </div>
                  </div>
                  <div>
                    <div className="cockpit-key">extensions</div>
                    <div className="cockpit-value">
                      {extensionPackages.length} loaded · {extensionGovernanceQueue.length} governed
                    </div>
                  </div>
                  <div>
                    <div className="cockpit-key">imported reach</div>
                    <div className="cockpit-value">
                      {importedCapabilityFamilies.length} families · {importedCapabilityFamilies.reduce((sum, item) => sum + item.total, 0)} active / {importedCapabilityFamilies.reduce((sum, item) => sum + item.installed, 0)} installed
                    </div>
                  </div>
                </div>
                <div className="cockpit-sublist">
                  <div className="cockpit-operator-section">
                    <div className="cockpit-operator-row">
                      <span className="cockpit-key">quick actions</span>
                      <div className="cockpit-operator-actions">
                        <button
                          type="button"
                          className="cockpit-operator-button"
                          onClick={() => {
                            setStudioSelectedId(studioEntries[0]?.id ?? null);
                            setStudioOpen(true);
                          }}
                        >
                          studio
                        </button>
                        <button
                          type="button"
                          className="cockpit-operator-button"
                          onClick={() => setPaletteOpen(true)}
                        >
                          palette
                        </button>
                        <button
                          type="button"
                          className="cockpit-operator-button"
                          onClick={() => void sendTestNativeNotification()}
                        >
                          test native
                        </button>
                      </div>
                    </div>
                    {operatorStatus && (
                      <div className="cockpit-sublist-item">{operatorStatus}</div>
                    )}
                    {!operatorStatus && (
                      <div className="cockpit-sublist-item">
                        Search commands, install missing capabilities, and run starter packs from one place.
                      </div>
                    )}
                    <div className="cockpit-sublist-item">
                      Shift+I inspect top triage · Shift+A approve top approval · Shift+C continue · Shift+O open thread · Shift+R redirect workflow · Shift+E inspect latest evidence · Shift+W inspect top workflow · Shift+U use latest output · Shift+P draft next step
                    </div>
                  </div>

                  <section className="cockpit-operator-section" aria-label="Active triage">
                    <div className="cockpit-operator-row">
                      <span className="cockpit-key">active triage</span>
                      <span className="cockpit-operator-link">{operatorTriageEntries.length} requiring action</span>
                    </div>
                    {operatorTriageEntries.map((entry) => {
                      const latestBranch = entry.workflow ? workflowLatestBranchRun(entry.workflow) : null;
                      return (
                      <div key={entry.id} className="cockpit-operator-row cockpit-operator-row--entry">
                        <button
                          type="button"
                          className="cockpit-operator-details cockpit-operator-details--button"
                          aria-label={`Inspect ${entry.label}`}
                          onClick={() => inspectOperatorTriageEntry(entry)}
                        >
                          <div className="cockpit-value">{entry.label}</div>
                          <div className="cockpit-operator-note">{entry.detail}</div>
                          <div className="cockpit-operator-note">{entry.meta}</div>
                        </button>
                        <div className="cockpit-operator-actions">
                          {entry.continueMessage && (
                            <button
                              type="button"
                              className="cockpit-operator-button"
                              aria-label={`Continue ${entry.label}`}
                              onClick={() => continueOperatorTriageEntry(entry)}
                            >
                              continue
                            </button>
                          )}
                          {entry.approval && (
                            <>
                              <button
                                type="button"
                                className="cockpit-operator-button"
                                aria-label={`Approve ${entry.label}`}
                                onClick={() => approveOperatorTriageEntry(entry)}
                              >
                                approve
                              </button>
                              <button
                                type="button"
                                className="cockpit-operator-button"
                                aria-label={`Deny ${entry.label}`}
                                onClick={() => void handleApprovalDecision(entry.approval!, "deny")}
                              >
                                deny
                              </button>
                            </>
                          )}
                          {entry.workflow && latestBranch && (
                            <button
                              type="button"
                              className="cockpit-operator-button"
                              aria-label={`Inspect latest branch for ${entry.label}`}
                              onClick={() => inspectWorkflowRun(latestBranch)}
                            >
                              latest branch
                            </button>
                          )}
                          {entry.threadId && canOpenLedgerThread(entry.threadId, sessionId, knownSessionIds) && (
                            <button
                              type="button"
                              className="cockpit-operator-button"
                              aria-label={`Open thread for ${entry.label}`}
                              onClick={() => openOperatorTriageThread(entry)}
                            >
                              open thread
                            </button>
                          )}
                          {entry.route && (
                            <button
                              type="button"
                              className="cockpit-operator-button"
                              aria-label={`Open desktop shell for ${entry.label}`}
                              onClick={() => inspectOperatorTriageEntry(entry)}
                            >
                              desktop shell
                            </button>
                          )}
                        </div>
                      </div>
                      );
                    })}
                    {operatorTriageEntries.length === 0 && (
                      <div className="cockpit-empty">No active workflows, approvals, queued guardian items, or reach failures need action.</div>
                    )}
                  </section>

                  <section className="cockpit-operator-section" aria-label="Evidence shortcuts">
                    <div className="cockpit-operator-row">
                      <span className="cockpit-key">evidence shortcuts</span>
                      <span className="cockpit-operator-link">{operatorEvidenceEntries.length} surfaced</span>
                    </div>
                    {operatorEvidenceEntries.map((entry) => (
                      <div key={entry.id} className="cockpit-operator-row cockpit-operator-row--entry">
                        <button
                          type="button"
                          className="cockpit-operator-details cockpit-operator-details--button"
                          aria-label={`Inspect ${entry.label}`}
                          onClick={() => inspectOperatorEvidenceEntry(entry)}
                        >
                          <div className="cockpit-value">{entry.label}</div>
                          <div className="cockpit-operator-note">{entry.detail}</div>
                          <div className="cockpit-operator-note">{entry.meta}</div>
                        </button>
                        <div className="cockpit-operator-actions">
                          {(entry.artifact || entry.approval?.resume_message) && (
                            <button
                              type="button"
                              className="cockpit-operator-button"
                              aria-label={`Draft next step for ${entry.label}`}
                              onClick={() => draftOperatorEvidenceEntry(entry)}
                            >
                              draft
                            </button>
                          )}
                          {entry.approval && (
                            <button
                              type="button"
                              className="cockpit-operator-button"
                              aria-label={`Approve ${entry.label}`}
                              onClick={() => void handleApprovalDecision(entry.approval!, "approve")}
                            >
                              approve
                            </button>
                          )}
                          {entry.threadId && canOpenLedgerThread(entry.threadId, sessionId, knownSessionIds) && (
                            <button
                              type="button"
                              className="cockpit-operator-button"
                              aria-label={`Open thread for ${entry.label}`}
                              onClick={() => void openThread(entry.threadId)}
                            >
                              open thread
                            </button>
                          )}
                          {entry.trace?.toolUsed && entry.audit && (
                            <button
                              type="button"
                              className="cockpit-operator-button"
                              aria-label={`Inspect audit for ${entry.label}`}
                              onClick={() => setSelectedInspector({ kind: "audit", event: entry.audit! })}
                            >
                              audit
                            </button>
                          )}
                        </div>
                      </div>
                    ))}
                    {operatorEvidenceEntries.length === 0 && (
                      <div className="cockpit-empty">No artifact, trace, or approval evidence needs surfacing yet.</div>
                    )}
                  </section>

                  <div className="cockpit-operator-section">
                    <div className="cockpit-operator-row">
                      <span className="cockpit-key">imported capability reach</span>
                      <span className="cockpit-operator-link">{importedCapabilityFamilies.length} families</span>
                    </div>
                    {importedCapabilityFamilies.map((family) => (
                      <div key={family.type} className="cockpit-operator-row cockpit-operator-row--entry">
                        <button
                          type="button"
                          className="cockpit-operator-details cockpit-operator-details--button"
                          onClick={() =>
                            setSelectedInspector({
                              kind: "operator",
                              entity: {
                                entityType: "extension_manifest",
                                name: family.label,
                                meta: `${family.total} entries · ${family.packages.length} packages`,
                                summary: family.entries[0]?.contribution.description ?? `${family.label} imported through the extension platform.`,
                                details: {
                                  family: family.type,
                                  ready: family.ready,
                                  attention: family.attention,
                                  approval: family.approval,
                                  packages: family.packages,
                                  entries: family.entries.map((entry) => ({
                                    package_id: entry.packageId,
                                    package_label: entry.packageLabel,
                                    type: entry.contribution.type,
                                    name: entry.contribution.name,
                                    status: entry.contribution.status,
                                    health: entry.contribution.health,
                                    permission_profile: entry.contribution.permission_profile,
                                  })),
                                },
                              },
                            })
                          }
                        >
                          <div className="cockpit-value">{family.label}</div>
                          <div className="cockpit-operator-note">
                            {family.total} active
                            {family.ready ? ` · ${family.ready} ready` : ""}
                            {family.installed > family.total ? ` · ${family.installed - family.total} inactive` : ""}
                            {family.attention ? ` · ${family.attention} attention` : ""}
                            {family.approval ? ` · ${family.approval} approval` : ""}
                            {family.packages.length ? ` · ${family.packages.join(", ")}` : ""}
                          </div>
                        </button>
                      </div>
                    ))}
                    {importedCapabilityFamilies.length === 0 && (
                      <div className="cockpit-empty">No packaged reach or imported capability families are active yet.</div>
                    )}
                  </div>

                  <div className="cockpit-operator-section">
                    <div className="cockpit-operator-row">
                      <span className="cockpit-key">extension boundaries</span>
                      <span className="cockpit-operator-link">{extensionGovernanceQueue.length} requiring attention</span>
                    </div>
                    {extensionGovernanceQueue.map((item) => (
                      <div key={item.packageId} className="cockpit-operator-row cockpit-operator-row--entry">
                        <button
                          type="button"
                          className="cockpit-operator-details cockpit-operator-details--button"
                          onClick={() =>
                            setSelectedInspector({
                              kind: "operator",
                              entity: buildExtensionManifestEntity(item.packageInfo),
                            })
                          }
                        >
                          <div className="cockpit-value">{item.label}</div>
                          <div className="cockpit-operator-note">
                            {item.riskLevel} risk · {item.detail}
                          </div>
                        </button>
                        <div className="cockpit-operator-actions">
                          <button
                            type="button"
                            className="cockpit-operator-button"
                            onClick={() => {
                              setStudioSelectedId(`extension:${item.packageId}`);
                              setStudioOpen(true);
                            }}
                          >
                            inspect
                          </button>
                        </div>
                      </div>
                    ))}
                    {extensionGovernanceQueue.length === 0 && (
                      <div className="cockpit-empty">No extension approval, permission, or connector issues in the current workspace.</div>
                    )}
                  </div>

                  <div className="cockpit-operator-section">
                    <div className="cockpit-operator-row">
                      <span className="cockpit-key">doctor plans</span>
                      <span className="cockpit-operator-link">{doctorPlans.length} recent</span>
                    </div>
                    {doctorPlans.map((plan) => (
                      <div key={plan.id} className="cockpit-operator-row cockpit-operator-row--entry">
                        <button
                          type="button"
                          className="cockpit-operator-details cockpit-operator-details--button"
                          onClick={() =>
                            setSelectedInspector({
                              kind: "operator",
                              entity: {
                                entityType: "workflow_definition",
                                name: plan.label,
                                meta: `${plan.source} · ${plan.availability}`,
                                summary: plan.blockingReasons[0] ?? "Capability plan snapshot",
                                details: {
                                  blocking_reasons: plan.blockingReasons,
                                  autorepair_actions: plan.autorepairActions,
                                  recommended_actions: plan.recommendedActions,
                                  manual_actions: plan.manualActions,
                                  applied_actions: plan.appliedActions,
                                  command: plan.command ?? "",
                                  risk_level: plan.riskLevel ?? "unknown",
                                  execution_boundaries: plan.executionBoundaries ?? [],
                                },
                              },
                            })
                          }
                        >
                          <div className="cockpit-value">{plan.label}</div>
                          <div className="cockpit-operator-note">
                            {plan.source} · {plan.availability}
                            {plan.blockingReasons[0] ? ` · ${plan.blockingReasons[0]}` : ""}
                            {plan.manualActions.length ? ` · ${plan.manualActions.length} manual` : ""}
                          </div>
                        </button>
                        <div className="cockpit-operator-actions">
                          {plan.command ? (
                            <button
                              type="button"
                              className="cockpit-operator-button"
                              onClick={() => queueComposerDraft(plan.command ?? "")}
                            >
                              draft
                            </button>
                          ) : null}
                          {plan.manualActions.length ? (
                            <button
                              type="button"
                              className="cockpit-operator-button"
                              onClick={() => void runCapabilityActions(plan.manualActions, plan.label)}
                            >
                              run manual
                            </button>
                          ) : null}
                          {plan.autorepairActions.length ? (
                            <button
                              type="button"
                              className="cockpit-operator-button"
                              onClick={() => void runCapabilityActions(plan.autorepairActions, plan.label)}
                            >
                              run safe
                            </button>
                          ) : null}
                        </div>
                      </div>
                    ))}
                    {doctorPlans.length === 0 && (
                      <div className="cockpit-empty">No bootstrap or doctor plans captured yet.</div>
                    )}
                  </div>

                  <div className="cockpit-operator-section">
                    <div className="cockpit-operator-row">
                      <span className="cockpit-key">recommendations</span>
                      <span className="cockpit-operator-link">{capabilityRecommendations.length} queued</span>
                    </div>
                    {capabilityRecommendations.map((item) => (
                      <div key={item.id} className="cockpit-operator-row">
                        <button
                          type="button"
                          className="cockpit-operator-details cockpit-operator-details--button"
                          onClick={() =>
                            setSelectedInspector({
                              kind: "operator",
                              entity: {
                                entityType: "starter_pack",
                                name: item.label,
                                meta: "recommended next action",
                                summary: item.description,
                                details: { action: item.action ?? null },
                              },
                            })
                          }
                        >
                          <div className="cockpit-value">{item.label}</div>
                          <div className="cockpit-operator-note">{item.description}</div>
                        </button>
                        {item.action && (
                          <div className="cockpit-operator-actions">
                            <button
                              type="button"
                              className="cockpit-operator-button"
                              onClick={() => void runCapabilityAction(item.action)}
                            >
                              {item.action.label}
                            </button>
                          </div>
                        )}
                      </div>
                    ))}
                    {capabilityRecommendations.length === 0 && (
                      <div className="cockpit-empty">No queued recommendations.</div>
                    )}
                  </div>

                  <div className="cockpit-operator-section">
                    <div className="cockpit-key">policy state</div>
                    <div className="cockpit-operator-row">
                      <span className="cockpit-operator-label">tools</span>
                      <div className="cockpit-operator-actions">
                        {(["safe", "balanced", "full"] as const).map((mode) => (
                          <button
                            key={mode}
                            type="button"
                            aria-label={`Set tool policy to ${mode}`}
                            aria-pressed={toolPolicyMode === mode}
                            className={`cockpit-operator-button ${toolPolicyMode === mode ? "cockpit-operator-button--active" : ""}`}
                            onClick={() => void updateToolPolicy(mode)}
                          >
                            {mode}
                          </button>
                        ))}
                      </div>
                    </div>
                    <div className="cockpit-operator-row">
                      <span className="cockpit-operator-label">mcp</span>
                      <div className="cockpit-operator-actions">
                        {([
                          { value: "disabled", label: "off" },
                          { value: "approval", label: "ask" },
                          { value: "full", label: "full" },
                        ] as const).map((mode) => (
                          <button
                            key={mode.value}
                            type="button"
                            aria-label={`Set MCP policy to ${mode.value}`}
                            aria-pressed={mcpPolicyMode === mode.value}
                            className={`cockpit-operator-button ${mcpPolicyMode === mode.value ? "cockpit-operator-button--active" : ""}`}
                            onClick={() => void updateMcpPolicy(mode.value)}
                          >
                            {mode.label}
                          </button>
                        ))}
                      </div>
                    </div>
                    <div className="cockpit-operator-row">
                      <span className="cockpit-operator-label">approval</span>
                      <div className="cockpit-operator-actions">
                        {([
                          { value: "high_risk", label: "high risk" },
                          { value: "off", label: "off" },
                        ] as const).map((mode) => (
                          <button
                            key={mode.value}
                            type="button"
                            aria-label={`Set approval mode to ${mode.value}`}
                            aria-pressed={approvalMode === mode.value}
                            className={`cockpit-operator-button ${approvalMode === mode.value ? "cockpit-operator-button--active" : ""}`}
                            onClick={() => void updateApprovalPolicy(mode.value)}
                          >
                            {mode.label}
                          </button>
                        ))}
                      </div>
                    </div>
                  </div>

                  <div className="cockpit-operator-section">
                    <div className="cockpit-operator-row">
                      <span className="cockpit-key">runbooks</span>
                      <span className="cockpit-operator-link">
                        {operatorRunbooks.filter((item) => item.availability === "ready").length}/{operatorRunbooks.length} ready
                      </span>
                    </div>
                    {operatorRunbooks.map((runbook) => (
                      <div key={runbook.id} className="cockpit-operator-row cockpit-operator-row--entry">
                        <button
                          type="button"
                          className="cockpit-operator-details cockpit-operator-details--button"
                          onClick={() =>
                            setSelectedInspector({
                              kind: "operator",
                              entity: {
                                entityType: "workflow_definition",
                                name: runbook.label,
                                meta: `${runbook.source.replace("_", " ")} · ${runbook.availability ?? "unknown"}`,
                                summary: runbook.description,
                                details: {
                                  command: runbook.command,
                                  blocking_reasons: runbook.blocking_reasons ?? [],
                                  risk_level: runbook.risk_level ?? "unknown",
                                  execution_boundaries: runbook.execution_boundaries ?? [],
                                  parameter_schema: runbook.parameter_schema ?? {},
                                  recommended_actions: runbook.recommended_actions ?? [],
                                },
                              },
                            })
                          }
                        >
                          <div className="cockpit-value">{runbook.label}</div>
                          <div className="cockpit-operator-note">
                            {runbook.availability ?? "unknown"} · {runbook.description}
                          </div>
                        </button>
                        <div className="cockpit-operator-actions">
                          <button
                            type="button"
                            className="cockpit-operator-button"
                            onClick={() => void executeRunbook(runbook)}
                          >
                            {runbook.availability === "ready" ? "draft" : "repair"}
                          </button>
                          <button
                            type="button"
                            className="cockpit-operator-button"
                            onClick={() => saveRunbookMacro(runbook)}
                          >
                            save macro
                          </button>
                          {runbook.action && (
                            <button
                              type="button"
                              className="cockpit-operator-button"
                              onClick={() => void runCapabilityAction(runbook.action)}
                            >
                              {runbook.action.label}
                            </button>
                          )}
                        </div>
                      </div>
                    ))}
                    {operatorRunbooks.length === 0 && (
                      <div className="cockpit-empty">No runbooks published.</div>
                    )}
                  </div>

                  <div className="cockpit-operator-section">
                    <div className="cockpit-operator-row">
                      <span className="cockpit-key">saved macros</span>
                      <span className="cockpit-operator-link">{operatorMacros.length} saved</span>
                    </div>
                    {operatorMacros.map((runbook) => (
                      <div key={`macro:${runbook.id}`} className="cockpit-operator-row cockpit-operator-row--entry">
                        <button
                          type="button"
                          className="cockpit-operator-details cockpit-operator-details--button"
                          onClick={() => void executeRunbook(runbook)}
                        >
                          <div className="cockpit-value">{runbook.label}</div>
                          <div className="cockpit-operator-note">{runbook.description}</div>
                        </button>
                        <div className="cockpit-operator-actions">
                          <button
                            type="button"
                            className="cockpit-operator-button"
                            onClick={() => void executeRunbook(runbook)}
                          >
                            {runbook.availability === "ready" ? "draft" : "repair"}
                          </button>
                          <button
                            type="button"
                            className="cockpit-operator-button"
                            onClick={() => removeRunbookMacro(runbook.id)}
                          >
                            remove
                          </button>
                        </div>
                      </div>
                    ))}
                    {operatorMacros.length === 0 && (
                      <div className="cockpit-empty">No saved macros yet.</div>
                    )}
                  </div>

                  <div className="cockpit-operator-section">
                    <div className="cockpit-operator-row">
                      <span className="cockpit-key">starter packs</span>
                      <span className="cockpit-operator-link">{readyStarterPacks.length}/{starterPacks.length} ready</span>
                    </div>
                    {starterPacks.map((pack) => (
                      <div key={pack.name} className="cockpit-operator-row cockpit-operator-row--entry">
                        <button
                          type="button"
                          className="cockpit-operator-details cockpit-operator-details--button"
                          onClick={() =>
                            setSelectedInspector({
                              kind: "operator",
                              entity: {
                                entityType: "starter_pack",
                                name: pack.label,
                                meta: `${pack.availability} · ${pack.ready_skills.length + pack.ready_workflows.length} ready items`,
                                summary: pack.description,
                                details: {
                                  skills: pack.skills,
                                  workflows: pack.workflows,
                                  ready_skills: pack.ready_skills,
                                  ready_workflows: pack.ready_workflows,
                                  blocked_skills: pack.blocked_skills,
                                  blocked_workflows: pack.blocked_workflows,
                                  sample_prompt: pack.sample_prompt ?? "",
                                },
                              },
                            })
                          }
                        >
                          <div className="cockpit-value">{pack.label}</div>
                          <div className="cockpit-operator-note">
                            {pack.availability === "ready"
                              ? "ready now"
                              : pack.availability === "partial"
                                ? "partially blocked"
                                : "blocked"}
                            {pack.blocked_skills[0]
                              ? ` · skill ${pack.blocked_skills[0].name}`
                              : pack.blocked_workflows[0]
                                ? ` · workflow ${pack.blocked_workflows[0].name}`
                                : ""}
                          </div>
                        </button>
                        <div className="cockpit-operator-actions">
                          <button
                            type="button"
                            className="cockpit-operator-button"
                            onClick={() => void activateStarterPack(pack)}
                          >
                            activate
                          </button>
                          {pack.recommended_actions?.length ? (
                            <button
                              type="button"
                              className="cockpit-operator-button"
                              onClick={() => void runCapabilityActions(pack.recommended_actions ?? [], pack.label)}
                            >
                              repair
                            </button>
                          ) : null}
                          {pack.sample_prompt && (
                            <button
                              type="button"
                              className="cockpit-operator-button"
                              onClick={() => queueComposerDraft(pack.sample_prompt ?? "")}
                            >
                              draft
                            </button>
                          )}
                        </div>
                      </div>
                    ))}
                    {starterPacks.length === 0 && (
                      <div className="cockpit-empty">No starter packs published.</div>
                    )}
                  </div>

                  <div className="cockpit-operator-section">
                    <div className="cockpit-operator-row">
                      <span className="cockpit-key">installable now</span>
                      <span className="cockpit-operator-link">
                        {
                          catalogItems.filter((item) =>
                            (!item.installed || item.update_available)
                            && (item.type !== "extension_pack" || !item.status || item.status === "ready"),
                          ).length
                        } missing
                      </span>
                    </div>
                    {catalogItems.filter((item) =>
                      (!item.installed || item.update_available)
                      && (item.type !== "extension_pack" || !item.status || item.status === "ready"),
                    ).map((item) => (
                      <div key={item.catalog_id ?? `${item.type}:${item.name}`} className="cockpit-operator-row cockpit-operator-row--entry">
                        <button
                          type="button"
                          className="cockpit-operator-details cockpit-operator-details--button"
                          onClick={() =>
                            setSelectedInspector({
                              kind: "operator",
                              entity: {
                                entityType:
                                  item.type === "skill"
                                    ? "skill"
                                    : item.type === "mcp_server"
                                      ? "mcp"
                                      : "extension_manifest",
                                name: item.name,
                                meta: `${item.installed && item.update_available ? "update" : "install"} ${item.type.replace("_", " ")}`,
                                summary: item.description,
                                details: {
                                  catalog_id: item.catalog_id ?? item.name,
                                  category: item.category ?? "",
                                  bundled: item.bundled ?? false,
                                  missing_tools: item.missing_tools ?? [],
                                  contribution_types: item.contribution_types ?? [],
                                  trust: item.trust ?? "",
                                  version: item.version ?? "",
                                  installed_version: item.installed_version ?? "",
                                  update_available: item.update_available ?? false,
                                  status: item.status ?? "ready",
                                  doctor_ok: item.doctor_ok ?? true,
                                  issues: item.issues ?? [],
                                  load_errors: item.load_errors ?? [],
                                },
                              },
                            })
                          }
                        >
                          <div className="cockpit-value">{item.name}</div>
                          <div className="cockpit-operator-note">{item.description}</div>
                        </button>
                        <div className="cockpit-operator-actions">
                          <button
                            type="button"
                            className="cockpit-operator-button"
                            onClick={() => void installCatalogItem(item)}
                          >
                            {item.installed && item.update_available ? "update" : "install"}
                          </button>
                          {item.recommended_actions?.length ? (
                            <button
                              type="button"
                              className="cockpit-operator-button"
                              onClick={() => void runCapabilityActions(item.recommended_actions ?? [], item.name)}
                            >
                              repair
                            </button>
                          ) : null}
                        </div>
                      </div>
                    ))}
                  </div>

                  <div className="cockpit-operator-section">
                    <div className="cockpit-operator-row">
                      <span className="cockpit-key">tool inventory</span>
                      <span className="cockpit-operator-link">{blockedTools.length} blocked</span>
                    </div>
                    {tools.map((tool) => (
                      <div key={tool.name} className="cockpit-operator-row">
                        <button
                          type="button"
                          className="cockpit-operator-details cockpit-operator-details--button"
                          onClick={() =>
                            setSelectedInspector({
                              kind: "operator",
                              entity: {
                                entityType: "tool",
                                name: tool.name,
                                meta: `${tool.risk_level ?? "unknown"} risk`,
                                summary: tool.description ?? "Native tool capability",
                                details: {
                                  availability: tool.availability ?? "unknown",
                                  blocked_reason: tool.blocked_reason ?? "none",
                                  execution_boundaries: tool.execution_boundaries ?? [],
                                  accepts_secret_refs: tool.accepts_secret_refs ?? false,
                                },
                              },
                            })
                          }
                        >
                          <div className="cockpit-value">{tool.name}</div>
                          <div className="cockpit-operator-note">
                            {tool.availability === "ready" ? "ready" : tool.blocked_reason ?? "blocked"}
                            {tool.execution_boundaries?.length
                              ? ` · ${tool.execution_boundaries.join(", ")}`
                              : ""}
                          </div>
                        </button>
                      </div>
                    ))}
                  </div>

                  <div className="cockpit-operator-section">
                    <div className="cockpit-operator-row">
                      <span className="cockpit-key">mcp servers</span>
                      <button
                        type="button"
                        className="cockpit-operator-link"
                        onClick={() => setSettingsPanelOpen(true)}
                      >
                        full settings
                      </button>
                    </div>
                    {mcpServers.map((server) => (
                      <div key={server.name} className="cockpit-operator-row cockpit-operator-row--entry">
                        <button
                          type="button"
                          className="cockpit-operator-details cockpit-operator-details--button"
                          onClick={() => setSelectedInspector({ kind: "operator", entity: buildMcpEntity(server) })}
                        >
                          <div className="cockpit-value">{server.name}</div>
                          <div className="cockpit-operator-note">
                            {server.status === "connected"
                              ? `${server.tool_count ?? 0} tools live`
                              : server.status === "auth_required"
                                ? "auth required"
                                : server.status === "error"
                                  ? server.status_message || "error"
                                  : server.enabled
                                    ? "disconnected"
                                    : "disabled"}
                          </div>
                        </button>
                        <div className="cockpit-operator-actions">
                          <button
                            type="button"
                            aria-label={`Test ${server.name}`}
                            className="cockpit-operator-button"
                            onClick={() => void testMcpServer(server)}
                          >
                            test
                          </button>
                          <button
                            type="button"
                            aria-label={`${server.enabled ? "Turn off" : "Turn on"} ${server.name}`}
                            className={`cockpit-operator-button ${server.enabled ? "cockpit-operator-button--active" : ""}`}
                            onClick={() => void toggleMcpServer(server)}
                          >
                            {server.enabled ? "on" : "off"}
                          </button>
                          {server.recommended_actions?.length ? (
                            <button
                              type="button"
                              className="cockpit-operator-button"
                              onClick={() => void runCapabilityActions(server.recommended_actions ?? [], server.name)}
                            >
                              repair
                            </button>
                          ) : null}
                          <button
                            type="button"
                            className="cockpit-operator-button"
                            onClick={() => openExtensionStudio(
                              studioEntries.find((entry) => entry.id === `mcp:${server.name}`) ?? null,
                            )}
                          >
                            studio
                          </button>
                          {(server.status === "auth_required" || server.has_headers) && (
                            <button
                              type="button"
                              aria-label={`Open settings for ${server.name}`}
                              className="cockpit-operator-button"
                              onClick={() => setSettingsPanelOpen(true)}
                            >
                              setup
                            </button>
                          )}
                        </div>
                      </div>
                    ))}
                    {mcpServers.length === 0 && (
                      <div className="cockpit-empty">No MCP servers configured.</div>
                    )}
                  </div>

                  <div className="cockpit-operator-section">
                    <div className="cockpit-operator-row">
                      <span className="cockpit-key">skills</span>
                      <button
                        type="button"
                        className="cockpit-operator-link"
                        onClick={() => void reloadOperatorSurface("skills")}
                      >
                        reload
                      </button>
                    </div>
                    {skills.map((skill) => (
                      <div key={skill.name} className="cockpit-operator-row cockpit-operator-row--entry">
                        <button
                          type="button"
                          className="cockpit-operator-details cockpit-operator-details--button"
                          onClick={() => setSelectedInspector({ kind: "operator", entity: buildSkillEntity(skill) })}
                        >
                          <div className="cockpit-value">{skill.name}</div>
                          <div className="cockpit-operator-note">
                            {skill.availability ?? (skill.enabled ? "ready" : "disabled")}
                            {skill.missing_tools?.length ? ` · missing ${skill.missing_tools.join(", ")}` : ""}
                            {!skill.missing_tools?.length && skill.requires_tools?.length
                              ? ` · ${skill.requires_tools.join(", ")}`
                              : ""}
                          </div>
                        </button>
                        <div className="cockpit-operator-actions">
                          <button
                            type="button"
                            aria-label={`${skill.enabled ? "Turn off" : "Turn on"} ${skill.name}`}
                            className={`cockpit-operator-button ${skill.enabled ? "cockpit-operator-button--active" : ""}`}
                            onClick={() => void toggleSkill(skill)}
                          >
                            {skill.enabled ? "on" : "off"}
                          </button>
                          {skill.recommended_actions?.length ? (
                            <button
                              type="button"
                              className="cockpit-operator-button"
                              onClick={() => void runCapabilityActions(skill.recommended_actions ?? [], skill.name)}
                            >
                              repair
                            </button>
                          ) : null}
                          <button
                            type="button"
                            className="cockpit-operator-button"
                            onClick={() => openExtensionStudio(
                              studioEntries.find((entry) => entry.id === `skill:${skill.name}`) ?? null,
                            )}
                          >
                            studio
                          </button>
                        </div>
                      </div>
                    ))}
                    {skills.length === 0 && (
                      <div className="cockpit-empty">No skills loaded.</div>
                    )}
                  </div>

                  <div className="cockpit-operator-section">
                    <div className="cockpit-operator-row">
                      <span className="cockpit-key">workflow availability</span>
                      <button
                        type="button"
                        className="cockpit-operator-link"
                        onClick={() => void reloadOperatorSurface("workflows")}
                      >
                        reload
                      </button>
                    </div>
                    <div className="cockpit-sublist-item">
                      invocable {availableWorkflows.filter((workflow) => workflow.user_invocable).length}/{invocableWorkflows.length} available
                    </div>
                    <div className="cockpit-sublist-item">
                      approval {approvalWorkflows.length} · blocked {blockedWorkflows.length}
                    </div>
                    {blockedWorkflows.map((workflow) => (
                      <div key={workflow.name} className="cockpit-operator-row cockpit-operator-row--entry">
                        <button
                          type="button"
                          className="cockpit-operator-details cockpit-operator-details--button"
                          onClick={() => setSelectedInspector({ kind: "operator", entity: buildWorkflowDefinitionEntity(workflow) })}
                        >
                          <div className="cockpit-value">blocked {workflow.name}</div>
                          <div className="cockpit-operator-note">
                            {workflow.missing_tools?.length ? `tools ${workflow.missing_tools.join(", ")}` : ""}
                            {workflow.missing_tools?.length && workflow.missing_skills?.length ? " · " : ""}
                            {workflow.missing_skills?.length ? `skills ${workflow.missing_skills.join(", ")}` : ""}
                          </div>
                        </button>
                        <div className="cockpit-operator-actions">
                          {workflow.recommended_actions?.length ? (
                            <button
                              type="button"
                              className="cockpit-operator-button"
                              onClick={() => void runCapabilityActions(readActionList(workflow.recommended_actions), workflow.name)}
                            >
                              repair
                            </button>
                          ) : null}
                          <button
                            type="button"
                            className="cockpit-operator-button"
                            onClick={() => openExtensionStudio(
                              studioEntries.find((entry) => entry.id === `workflow:${workflow.name}`) ?? null,
                            )}
                          >
                            studio
                          </button>
                          <button
                            type="button"
                            className="cockpit-operator-button"
                            onClick={async () => {
                              try {
                                const preflight = await preflightCapability("workflow", workflow.name);
                                if (!preflight.ready) {
                                  const bootstrap = await bootstrapCapability("workflow", workflow.name, workflow.name, preflight);
                                  if (bootstrap.ready && bootstrap.command) {
                                    queueComposerDraft(bootstrap.command);
                                  }
                                  return;
                                }
                                queueComposerDraft(preflight.command ?? buildWorkflowDraft(workflow));
                              } catch {
                                queueComposerDraft(buildWorkflowDraft(workflow));
                              }
                            }}
                          >
                            draft
                          </button>
                        </div>
                      </div>
                    ))}
                    {workflows.length === 0 && (
                      <div className="cockpit-empty">No workflows available.</div>
                    )}
                  </div>
                  <div className="cockpit-feedback-row">
                    <button
                      className="cockpit-feedback-button"
                      onClick={() => setSettingsPanelOpen(true)}
                    >
                      Open Settings
                    </button>
                    <button
                      className="cockpit-feedback-button"
                      onClick={() => setPaletteOpen(true)}
                    >
                      Capability Palette
                    </button>
                  </div>
                  {workflows.length === 0 && skills.length === 0 && mcpServers.length === 0 && tools.length === 0 && (
                    <div className="cockpit-empty">Operator surface unavailable.</div>
                  )}
                </div>
              </section>
              </CockpitWorkspaceWindow>
            )}
          </>
        )}
      </div>

      {studioOpen && (
        <div className="cockpit-overlay-backdrop" onClick={() => setStudioOpen(false)}>
          <section
            className="cockpit-palette cockpit-studio"
            onClick={(event) => event.stopPropagation()}
            aria-label="Extension studio"
          >
            <div className="cockpit-window-header">
              <div className="cockpit-window-header-main">
                <div className="cockpit-window-title">Extension studio</div>
                <div className="cockpit-window-meta">
                  validate, repair, and author extension packages, workflows, skills, and MCP config from one workspace
                </div>
              </div>
              <div className="cockpit-window-controls">
                <div className="cockpit-window-meta">
                  {selectedStudioEntry
                    ? `${selectedStudioEntry.entityType.replace(/_/g, " ")} · ${selectedStudioEntry.availability}`
                    : "package lifecycle · install / validate"}
                </div>
                <button
                  type="button"
                  className="cockpit-window-control cockpit-window-control--close"
                  title="Close extension studio"
                  aria-label="Close extension studio"
                  onClick={() => setStudioOpen(false)}
                >
                  x
                </button>
              </div>
            </div>
            <div className="cockpit-studio-shell">
              <aside className="cockpit-studio-sidebar">
                <div className="cockpit-operator-row">
                  <span className="cockpit-key">extensions</span>
                  <span className="cockpit-operator-link">{studioEntries.length} loaded</span>
                </div>
                <div className="cockpit-studio-sidebar-group">
                  <div className="cockpit-operator-row">
                    <span className="cockpit-key">new skill pack</span>
                    <span className="cockpit-row-age">workspace scaffold</span>
                  </div>
                  <input
                    className="cockpit-input"
                    aria-label="New extension package name"
                    value={studioScaffoldName}
                    onChange={(event) => setStudioScaffoldName(event.target.value)}
                    placeholder="research-pack"
                  />
                  <input
                    className="cockpit-input"
                    aria-label="New extension display name"
                    value={studioScaffoldDisplayName}
                    onChange={(event) => setStudioScaffoldDisplayName(event.target.value)}
                    placeholder="Research Pack"
                  />
                  <div className="cockpit-feedback-row">
                    <button
                      className="cockpit-feedback-button"
                      onClick={() => void scaffoldStudioSkillPack()}
                      disabled={
                        !studioScaffoldName.trim()
                        || studioBusy === "extension-scaffold"
                        || studioBusy === "extension-install"
                        || studioBusy === "extension-validate"
                        || studioBusy === "extension-update"
                      }
                    >
                      Scaffold skill pack
                    </button>
                  </div>
                </div>
                <div className="cockpit-studio-sidebar-group">
                  <div className="cockpit-operator-row">
                    <span className="cockpit-key">package path</span>
                    <span className="cockpit-row-age">install / validate</span>
                  </div>
                  <input
                    className="cockpit-input"
                    aria-label="Extension package path"
                    value={studioExtensionPath}
                    onChange={(event) => {
                      setStudioExtensionPath(event.target.value);
                      setStudioPackagePreview(null);
                      setStudioPackageStatus(null);
                    }}
                    placeholder="/path/to/extension-pack"
                  />
                  <div className="cockpit-feedback-row">
                    <button
                      className="cockpit-feedback-button"
                      onClick={() => void validateStudioExtensionPath()}
                      disabled={
                        studioBusy === "extension-validate"
                        || studioBusy === "extension-install"
                        || studioBusy === "extension-update"
                      }
                    >
                      Validate path
                    </button>
                    <button
                      className="cockpit-feedback-button"
                      onClick={() => void (
                        studioPackageAction.key === "update"
                          ? updateStudioExtensionPath()
                          : installStudioExtensionPath()
                      )}
                      disabled={
                        studioPackageAction.disabled
                        || studioBusy === "extension-install"
                        || studioBusy === "extension-validate"
                        || studioBusy === "extension-update"
                      }
                    >
                      {studioPackageAction.label}
                    </button>
                  </div>
                  {studioPackageStatus ? (
                    <div className="cockpit-sublist-item">{studioPackageStatus}</div>
                  ) : null}
                </div>
                {studioSidebarSections.packages.map((group) => (
                  <div key={group.extension.id} className="cockpit-studio-sidebar-group">
                    <div className="cockpit-operator-row">
                      <span className="cockpit-key">{group.extension.display_name}</span>
                      <span className="cockpit-row-age">{group.extension.location}</span>
                    </div>
                    <div className="cockpit-list cockpit-list--palette">
                      {group.entries.map((entry) => (
                        <button
                          key={entry.id}
                          type="button"
                          className={`cockpit-sublist-button ${selectedStudioEntry?.id === entry.id ? "active" : ""}`}
                          onClick={() => setStudioSelectedId(entry.id)}
                        >
                          <span>{entry.entityType === "extension_manifest" ? "manifest.yaml" : entry.name}</span>
                          <span className="cockpit-row-age">
                            {entry.entityType === "workflow_definition"
                              ? "workflow"
                              : entry.entityType === "extension_manifest"
                                ? "manifest"
                                : entry.entityType}
                          </span>
                        </button>
                      ))}
                    </div>
                  </div>
                ))}
                {studioSidebarSections.standalone.length ? (
                  <div className="cockpit-studio-sidebar-group">
                    <div className="cockpit-operator-row">
                      <span className="cockpit-key">standalone</span>
                      <span className="cockpit-row-age">{studioSidebarSections.standalone.length}</span>
                    </div>
                    <div className="cockpit-list cockpit-list--palette">
                      {studioSidebarSections.standalone.map((entry) => (
                        <button
                          key={entry.id}
                          type="button"
                          className={`cockpit-sublist-button ${selectedStudioEntry?.id === entry.id ? "active" : ""}`}
                          onClick={() => setStudioSelectedId(entry.id)}
                        >
                          <span>{entry.name}</span>
                          <span className="cockpit-row-age">{entry.entityType === "workflow_definition" ? "workflow" : entry.entityType}</span>
                        </button>
                      ))}
                    </div>
                  </div>
                ) : null}
              </aside>
              <div className="cockpit-studio-main">
                {selectedStudioEntry ? (
                <div className="cockpit-inspector">
                  <div className="cockpit-inspector-title">{selectedStudioEntry.name}</div>
                  <div className="cockpit-inspector-meta">{selectedStudioEntry.meta}</div>
                  <div className="cockpit-inspector-body">{selectedStudioEntry.summary}</div>
                  <div className="cockpit-chip-row">
                    <span className="cockpit-chip">{selectedStudioEntry.availability}</span>
                    {selectedStudioEntry.packageDisplayName ? (
                      <span className="cockpit-chip">
                        {selectedStudioEntry.packageDisplayName}
                        {selectedStudioEntry.packageVersion ? ` · ${selectedStudioEntry.packageVersion}` : ""}
                      </span>
                    ) : null}
                    {selectedStudioEntry.packageLocation ? (
                      <span className="cockpit-chip">
                        {selectedStudioEntry.packageLocation}
                        {selectedStudioEntry.packageTrust ? ` · ${selectedStudioEntry.packageTrust}` : ""}
                      </span>
                    ) : null}
                    {typeof selectedStudioEntry.entity.details.file_path === "string" && selectedStudioEntry.entity.details.file_path
                      ? <span className="cockpit-chip">{String(selectedStudioEntry.entity.details.file_path)}</span>
                      : null}
                    {typeof selectedStudioEntry.entity.details.url === "string" && selectedStudioEntry.entity.details.url
                      ? <span className="cockpit-chip">{String(selectedStudioEntry.entity.details.url)}</span>
                      : null}
                  </div>
                  <div className="cockpit-feedback-row">
                    <button
                      className="cockpit-feedback-button"
                      onClick={() => void refreshStudioValidation()}
                      disabled={studioBusy === "validation" || selectedStudioEntry.validationSupported === false}
                    >
                      {selectedStudioEntry.entityType === "mcp"
                        ? "Validate config"
                        : selectedStudioEntry.entityType === "extension_manifest"
                          ? "Validate manifest"
                          : "Refresh validation"}
                    </button>
                    <button
                      className="cockpit-feedback-button"
                      onClick={() => void saveStudioDraft()}
                      disabled={studioBusy === "save" || selectedStudioEntry.saveSupported === false}
                    >
                      {selectedStudioEntry.entityType === "mcp"
                        ? "Save config"
                        : selectedStudioEntry.entityType === "extension_manifest"
                          ? "Save manifest"
                          : "Save draft"}
                    </button>
                    {selectedStudioEntry.entityType !== "mcp" && selectedStudioEntry.entityType !== "extension_manifest" ? (
                      <button
                        className="cockpit-feedback-button"
                        onClick={() => queueComposerDraft(studioDraft)}
                      >
                        Queue authoring draft
                      </button>
                    ) : null}
                    {studioRecommendedActions.length ? (
                      <button
                        className="cockpit-feedback-button"
                        onClick={() => void runCapabilityActions(studioRecommendedActions, selectedStudioEntry.name)}
                      >
                        Run repairs
                      </button>
                    ) : null}
                    {selectedStudioEntry.entityType === "extension_manifest" && selectedExtensionToggleAction ? (
                      <button
                        className="cockpit-feedback-button"
                        onClick={() => void setSelectedExtensionEnabled(selectedExtensionToggleAction.nextEnabled)}
                        disabled={studioBusy === "extension-enable" || studioBusy === "extension-disable"}
                      >
                        {selectedExtensionToggleAction.label}
                      </button>
                    ) : null}
                    {selectedStudioEntry.entityType === "extension_manifest" && selectedExtensionPackage?.metadata_supported ? (
                      <button
                        className="cockpit-feedback-button"
                        onClick={() => void saveSelectedExtensionMetadata()}
                        disabled={studioBusy === "extension-configure"}
                      >
                        Save metadata
                      </button>
                    ) : null}
                    {selectedStudioEntry.entityType === "extension_manifest" && selectedExtensionPackage?.removable ? (
                      <button
                        className="cockpit-feedback-button"
                        onClick={() => void removeSelectedExtension()}
                        disabled={studioBusy === "extension-remove"}
                      >
                        Remove package
                      </button>
                    ) : null}
                    <button
                      className="cockpit-feedback-button"
                      onClick={() => setStudioOpen(false)}
                    >
                      Close
                    </button>
                  </div>
                  {studioStatus && (
                    <div className="cockpit-sublist-item">{studioStatus}</div>
                  )}
                  <div className="cockpit-inspector-stack">
                    <div className="cockpit-inspector-stack-row">
                      <div className="cockpit-key">doctor plan</div>
                      <div className="cockpit-value">
                        {studioPreflight
                          ? `${studioPreflight.ready ? "ready" : "blocked"} · ${studioPreflight.blocking_reasons.join(" · ") || "no blockers"}`
                          : selectedStudioEntry.entityType === "extension_manifest"
                            ? "Manage install state, metadata, and manifest integrity from one package surface."
                            : selectedStudioEntry.entityType === "skill"
                              ? "Use validation to check draft syntax and current runtime blockers."
                            : "Run validation to capture a current plan."}
                      </div>
                    </div>
                    {selectedStudioEntry.entityType === "extension_manifest" && selectedExtensionPackage ? (
                      <div className="cockpit-inspector-stack-row">
                        <div className="cockpit-key">package state</div>
                        <div className="cockpit-value">
                          {selectedExtensionPackage.status}
                          {selectedExtensionPackage.enabled_scope !== "none"
                            ? ` · ${selectedExtensionPackage.enabled === false ? "disabled" : "enabled"}`
                            : ""}
                          {selectedExtensionPackage.toggleable_contribution_types.length
                            ? ` · toggles ${selectedExtensionPackage.toggleable_contribution_types.join(", ")}`
                            : ""}
                          {selectedExtensionPackage.passive_contribution_types.length
                            ? ` · passive ${selectedExtensionPackage.passive_contribution_types.join(", ")}`
                            : ""}
                        </div>
                      </div>
                    ) : null}
                    {selectedStudioEntry.entityType === "extension_manifest" && selectedExtensionPackage?.issues.length ? (
                      <div className="cockpit-inspector-stack-row">
                        <div className="cockpit-key">doctor issues</div>
                        <div className="cockpit-value">
                          {selectedExtensionPackage.issues.map((issue) => issue.message).join(" · ")}
                        </div>
                      </div>
                    ) : null}
                    {selectedStudioEntry.entityType === "extension_manifest" && selectedExtensionPackage?.load_errors.length ? (
                      <div className="cockpit-inspector-stack-row">
                        <div className="cockpit-key">load errors</div>
                        <div className="cockpit-value">
                          {selectedExtensionPackage.load_errors.map((error) => error.message).join(" · ")}
                        </div>
                      </div>
                    ) : null}
                    {selectedStudioEntry.entityType === "extension_manifest" && selectedExtensionPackage?.toggle_targets.length ? (
                      <div className="cockpit-inspector-stack-row">
                        <div className="cockpit-key">toggle targets</div>
                        <div className="cockpit-value">
                          {selectedExtensionPackage.toggle_targets
                            .map((target) => `${target.type.replace(/_/g, " ")} ${target.name}`)
                            .join(" · ")}
                        </div>
                      </div>
                    ) : null}
                    {selectedStudioEntry.entityType === "extension_manifest" && selectedExtensionPackage?.metadata_supported ? (
                      <div className="cockpit-inspector-stack-row">
                        <div className="cockpit-key">metadata scope</div>
                        <div className="cockpit-value">
                          {selectedExtensionPackage.config_scope.replace(/_/g, " ")}
                          {selectedExtensionPackage.publisher?.name
                            ? ` · publisher ${selectedExtensionPackage.publisher.name}`
                            : ""}
                        </div>
                      </div>
                    ) : null}
                    {studioDraftValidation ? (
                      <div className="cockpit-inspector-stack-row">
                        <div className="cockpit-key">draft status</div>
                        <div className="cockpit-value">
                          {"valid" in studioDraftValidation
                            ? studioDraftValidation.valid === false
                              ? "invalid"
                              : "valid"
                            : "captured"}
                          {Array.isArray(studioDraftValidation.missing_tools) && studioDraftValidation.missing_tools.length
                            ? ` · tools ${studioDraftValidation.missing_tools.join(", ")}`
                            : ""}
                          {Array.isArray(studioDraftValidation.missing_skills) && studioDraftValidation.missing_skills.length
                            ? ` · skills ${studioDraftValidation.missing_skills.join(", ")}`
                            : ""}
                        </div>
                      </div>
                    ) : null}
                    {studioDraftValidation && Array.isArray(studioDraftValidation.errors) && studioDraftValidation.errors.length > 0 ? (
                      <div className="cockpit-inspector-stack-row">
                        <div className="cockpit-key">draft errors</div>
                        <div className="cockpit-value">
                          {studioDraftValidation.errors.map((entry) => {
                            if (!entry || typeof entry !== "object" || Array.isArray(entry)) return "";
                            const record = entry as Record<string, unknown>;
                            return typeof record.message === "string" ? record.message : JSON.stringify(record);
                          }).filter(Boolean).join(" · ")}
                        </div>
                      </div>
                    ) : null}
                    {studioPreflight?.autorepair_actions?.length ? (
                      <div className="cockpit-inspector-stack-row">
                        <div className="cockpit-key">safe actions</div>
                        <div className="cockpit-value">
                          {studioPreflight.autorepair_actions.map((action) => formatCapabilityAction(action)).join(" · ")}
                        </div>
                      </div>
                    ) : null}
                    {studioPreflight?.recommended_actions?.length ? (
                      <div className="cockpit-inspector-stack-row">
                        <div className="cockpit-key">manual actions</div>
                        <div className="cockpit-value">
                          {studioPreflight.recommended_actions.map((action) => formatCapabilityAction(action)).join(" · ")}
                        </div>
                      </div>
                    ) : null}
                    {studioWorkflowErrors.length > 0 ? (
                      <div className="cockpit-inspector-stack-row">
                        <div className="cockpit-key">load errors</div>
                        <div className="cockpit-value">
                          {studioWorkflowErrors.map((entry) => entry.message).join(" · ")}
                        </div>
                      </div>
                    ) : null}
                    {studioMcpTestResult ? (
                      <div className="cockpit-inspector-stack-row">
                        <div className="cockpit-key">mcp test</div>
                        <div className="cockpit-value">
                          {typeof studioMcpTestResult.status === "string" ? studioMcpTestResult.status : "result"}
                          {typeof studioMcpTestResult.tool_count === "number"
                            ? ` · ${studioMcpTestResult.tool_count} tools`
                            : ""}
                          {typeof studioMcpTestResult.message === "string"
                            ? ` · ${studioMcpTestResult.message}`
                            : ""}
                        </div>
                      </div>
                    ) : null}
                  </div>
                  {selectedStudioEntry.entityType === "mcp" ? (
                    <div className="cockpit-studio-form">
                      <label className="cockpit-key" htmlFor="studio-mcp-url">mcp url</label>
                      <input
                        id="studio-mcp-url"
                        className="cockpit-input"
                        value={studioMcpUrl}
                        onChange={(event) => setStudioMcpUrl(event.target.value)}
                        placeholder="http://host.docker.internal:9001/mcp"
                        disabled={studioMcpReadOnly}
                      />
                      <label className="cockpit-key" htmlFor="studio-mcp-description">description</label>
                      <input
                        id="studio-mcp-description"
                        className="cockpit-input"
                        value={studioMcpDescription}
                        onChange={(event) => setStudioMcpDescription(event.target.value)}
                        placeholder="What this MCP server provides"
                        disabled={studioMcpReadOnly}
                      />
                    </div>
                  ) : (
                    <div className="cockpit-studio-form">
                      <label className="cockpit-key" htmlFor="studio-authoring-draft">
                        {selectedStudioEntry.entityType === "extension_manifest" ? "manifest draft" : "authoring draft"}
                      </label>
                      <textarea
                        id="studio-authoring-draft"
                        className="cockpit-input cockpit-studio-textarea"
                        value={studioDraft}
                        onChange={(event) => setStudioDraft(event.target.value)}
                      />
                      {selectedStudioEntry.entityType === "extension_manifest" && selectedExtensionPackage?.metadata_supported ? (
                        <>
                          <label className="cockpit-key" htmlFor="studio-extension-config">package metadata</label>
                          <textarea
                            id="studio-extension-config"
                            className="cockpit-input cockpit-studio-textarea cockpit-studio-textarea--compact"
                            value={studioExtensionConfigDraft}
                            onChange={(event) => {
                              setStudioExtensionConfigDraft(event.target.value);
                              setStudioExtensionConfigDirty(true);
                            }}
                          />
                        </>
                      ) : null}
                    </div>
                  )}
                </div>
                ) : (
                  <div className="cockpit-inspector">
                    <div className="cockpit-inspector-title">Extension packages</div>
                    <div className="cockpit-inspector-meta">install, validate, and inspect lifecycle state</div>
                    <div className="cockpit-inspector-body">
                      Enter a local package path to validate or install a new extension. Installed packages appear in the left sidebar with manifest-backed lifecycle controls.
                    </div>
                    <div className="cockpit-inspector-stack">
                      <div className="cockpit-inspector-stack-row">
                        <div className="cockpit-key">loaded packages</div>
                        <div className="cockpit-value">{extensionPackages.length}</div>
                      </div>
                      <div className="cockpit-inspector-stack-row">
                        <div className="cockpit-key">workspace packages</div>
                        <div className="cockpit-value">
                          {extensionPackages.filter((item) => item.location === "workspace").length}
                        </div>
                      </div>
                      <div className="cockpit-inspector-stack-row">
                        <div className="cockpit-key">doctor state</div>
                        <div className="cockpit-value">
                          {extensionPackages.filter((item) => item.status === "degraded").length} degraded
                        </div>
                      </div>
                    </div>
                  </div>
                )}
              </div>
            </div>
          </section>
        </div>
      )}

      {paletteOpen && (
        <div className="cockpit-overlay-backdrop" onClick={() => setPaletteOpen(false)}>
          <section
            className="cockpit-palette"
            onClick={(event) => event.stopPropagation()}
            aria-label="Capability palette"
          >
            <div className="cockpit-window-header">
              <div className="cockpit-window-header-main">
                <div className="cockpit-window-title">Capability palette</div>
                <div className="cockpit-window-meta">
                  keyboard-first launcher for capabilities, installs, repairs, and runbooks
                </div>
              </div>
              <div className="cockpit-window-controls">
                <div className="cockpit-window-meta">Shift+K / Ctrl+K</div>
                <button
                  type="button"
                  className="cockpit-window-control cockpit-window-control--close"
                  title="Close capability palette"
                  aria-label="Close capability palette"
                  onClick={() => setPaletteOpen(false)}
                >
                  x
                </button>
              </div>
            </div>
            <div className="cockpit-palette-body">
              <input
                type="text"
                value={paletteQuery}
                onChange={(event) => setPaletteQuery(event.target.value)}
                className="cockpit-input"
                placeholder="Search commands, workflows, starter packs, installs, repairs..."
                autoFocus
              />
              <div className="cockpit-list cockpit-list--palette">
                {paletteItems.map((item) => (
                  <div key={item.id} className="cockpit-row">
                    <div className="cockpit-row-header">
                      <span className="cockpit-role">{item.label}</span>
                      <span className="cockpit-row-age">{item.kind}</span>
                    </div>
                    <div className="cockpit-row-body">{item.detail}</div>
                    <div className="cockpit-feedback-row">
                      {item.draft && (
                        <button
                          className="cockpit-feedback-button"
                          onClick={() => {
                            const runbook = operatorRunbooks.find((entry) => `runbook:${entry.id}` === item.id)
                              ?? operatorMacros.find((entry) => `macro:${entry.id}` === item.id);
                            if (runbook) {
                              void executeRunbook(runbook);
                            } else {
                              queueComposerDraft(item.draft!);
                            }
                            setPaletteOpen(false);
                          }}
                        >
                          Draft
                        </button>
                      )}
                      {item.action && (
                        <button
                          className="cockpit-feedback-button"
                          onClick={() => void runCapabilityAction(item.action)}
                        >
                          {item.action.label}
                        </button>
                      )}
                    </div>
                  </div>
                ))}
                {paletteItems.length === 0 && (
                  <div className="cockpit-empty">No matching capabilities or repair actions.</div>
                )}
              </div>
            </div>
          </section>
        </div>
      )}

      <form className="cockpit-composer" onSubmit={handleSubmit}>
        <div className="cockpit-composer-meta">
          <span>{workspaceTelemetryLeft}</span>
          <span className="cockpit-composer-meta-center">
            {workspaceTelemetryCenter}
          </span>
          <span>{isAgentBusy ? "SERAPH RESPONDING" : workspaceTelemetryRight}</span>
        </div>
        <div className="cockpit-composer-row">
          <input
            ref={inputRef}
            type="text"
            value={composer}
            onChange={(event) => setComposer(event.target.value)}
            placeholder={
              connectionStatus === "connected"
                ? "Ask Seraph, redirect a workflow, or steer the guardian."
                : "WebSocket offline. Message will fall back to direct chat."
            }
            className="cockpit-input"
            disabled={isAgentBusy}
          />
          <button type="submit" className="cockpit-send" disabled={submitDisabled}>
            {isAgentBusy ? (
              <>
                <span className="cockpit-send-spinner" aria-hidden="true">
                  <span className="thinking-dot">.</span>
                  <span className="thinking-dot">.</span>
                  <span className="thinking-dot">.</span>
                </span>
                <span>Working</span>
              </>
            ) : (
              "Send"
            )}
          </button>
        </div>
      </form>
    </div>
  );
}
