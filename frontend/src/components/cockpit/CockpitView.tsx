import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";

import { appEventBus } from "../../lib/appEventBus";
import { API_URL } from "../../config/constants";
import { useChatStore } from "../../stores/chatStore";
import { useQuestStore } from "../../stores/questStore";
import { useCockpitLayoutStore } from "../../stores/cockpitLayoutStore";
import { usePanelLayoutStore } from "../../stores/panelLayoutStore";
import type { ChatMessage, GoalInfo } from "../../types";
import { buildWorkflowDraft, type WorkflowInfo } from "../settings/workflowDraft";
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
import { COCKPIT_LAYOUTS, getCockpitLayout } from "./layouts";
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
  intervention_type: string;
  content_excerpt: string;
  policy_action: string;
  policy_reason: string;
  delivery_decision?: string | null;
  latest_outcome: string;
  transport?: string | null;
  notification_id?: string | null;
  feedback_type?: string | null;
  updated_at: string;
  continuity_surface: string;
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
    thread_id?: string | null;
    thread_label?: string | null;
    created_at: string;
  }>;
  queued_insight_count: number;
  recent_interventions: GuardianContinuityIntervention[];
}

interface SkillInfo {
  name: string;
  enabled: boolean;
  description?: string;
  file_path?: string;
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
  entityType: "tool" | "skill" | "mcp" | "starter_pack" | "workflow_definition" | "operator_timeline_item";
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
  entityType: "workflow_definition" | "skill" | "mcp";
  name: string;
  summary: string;
  availability: string;
  meta: string;
  entity: OperatorEntity;
}

type LoggedOperatorError = Error & { operatorLogged?: boolean };

interface CatalogItemInfo {
  name: string;
  type: "skill" | "mcp_server";
  description: string;
  category?: string;
  bundled?: boolean;
  installed: boolean;
  missing_tools?: string[];
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

interface OperatorFeedEntry {
  id: string;
  summary: string;
  status: "info" | "success" | "failed";
  createdAt: string;
}

interface OperatorTimelineEntry {
  id: string;
  kind: "workflow_run" | "approval" | "notification" | "queued_insight" | "intervention" | "audit" | "routing";
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
  metadata?: Record<string, unknown>;
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
  if (message.role === "proactive") return message.interventionType ?? "proactive";
  if (message.role === "step") return message.toolUsed ?? "step";
  return message.role;
}

function formatContinuityLabel(value: string | null | undefined): string {
  return (value || "unknown").replace(/_/g, " ");
}

function formatOperatorMode(value: string): string {
  return value.replace(/_/g, " ");
}

function formatFeedStatus(value: OperatorFeedEntry["status"]): string {
  if (value === "success") return "ok";
  if (value === "failed") return "failed";
  return "info";
}

function formatCapabilityAction(action: Record<string, unknown>): string {
  const type = typeof action.type === "string" ? action.type : "action";
  const name = typeof action.name === "string" ? action.name : null;
  const mode = typeof action.mode === "string" ? action.mode : null;
  const status = typeof action.status === "string" ? action.status : null;
  const detail = typeof action.detail === "string" ? action.detail : null;
  const target = name ?? mode ?? detail ?? "";
  return `${type.replace(/_/g, " ")}${target ? ` · ${target}` : ""}${status ? ` · ${status}` : ""}`;
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

function buildWorkflowDefinitionEntity(workflow: WorkflowInfo): OperatorEntity {
  return {
    entityType: "workflow_definition",
    name: workflow.name,
    meta: `${workflow.risk_level} risk · ${workflow.availability ?? (workflow.is_available === false ? "blocked" : "ready")}`,
    summary: workflow.description,
    details: {
      file_path: workflow.file_path,
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
      has_headers: server.has_headers ?? false,
      recommended_actions: server.recommended_actions ?? [],
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
        : "Current runtime blockers: none detected from the latest cockpit snapshot.",
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
      "After editing, reload skills and verify the skill remains available in the cockpit capability surface.",
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
  if (workflow.runFingerprint) details.push(`fingerprint ${shortIdentifier(workflow.runFingerprint)}`);
  if (workflow.resumeCheckpointLabel) details.push(`checkpoint ${workflow.resumeCheckpointLabel}`);
  if (workflow.resumeFromStep) details.push(`resume ${workflow.resumeFromStep}`);
  if (workflow.threadContinueMessage) details.push("thread continue ready");
  if (workflow.approvalRecoveryMessage) details.push("approval recovery ready");
  return details;
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

function timelineStatusLabel(value: OperatorTimelineEntry): string {
  return `${value.kind.replace(/_/g, " ")} · ${value.status.replace(/_/g, " ")}`;
}

function supportsArtifactRoundtrip(workflow: WorkflowInfo): boolean {
  return Object.prototype.hasOwnProperty.call(workflow.inputs, "file_path");
}

const COCKPIT_GLOBAL_HINTS = [
  "Shift+1/2/3 switch layouts",
  "Shift+K or Ctrl+K opens the capability palette",
  "Drag headers to move panes",
  "Reset view repacks the current workspace",
];

const COCKPIT_WINDOW_HINTS = {
  sessions: "Thread control, saved conversations, and continuity markers for the active thread.",
  goals: "Keep current priorities visible here; open the full goals overlay for planning.",
  outputs: "Recent workspace artifacts produced in the current audit window.",
  approvals: "Pending approvals block workflow and tool execution until you inspect or approve them.",
  guardianState: "The live synthesis Seraph is using for timing, confidence, and next actions.",
  operatorTimeline: "A thread-aware feed of approvals, interventions, notifications, and surfaced failures.",
  interventions: "Recent proactive nudges, delivery outcomes, and feedback signal.",
  workflowTimeline: "Inspect runs, branch from failures, and resume repaired steps.",
  audit: "Durable tool, memory, workflow, and integration events for the current window.",
  trace: "In-flight routing, tool, and error activity while work is happening.",
  inspector: "Select a run, approval, intervention, or event to inspect details and recovery actions.",
  presence: "A runtime-driven sentinel glyph showing whether Seraph is idle, thinking, using tools, waiting on approval, or faulted.",
  conversation: "Latest replies, pending drafts, and quick thread context for the current session.",
  desktopShell: "Native continuity, queued notifications, and browser-closed follow-up state.",
  operatorTerminal: "Run packs, workflows, macros, and repair actions from one dense control surface.",
} as const;

function CockpitWorkspaceWindow({
  panelId,
  title,
  meta,
  hint,
  showHint,
  minWidth,
  minHeight,
  children,
}: {
  panelId: string;
  title: string;
  meta: string;
  hint?: string | null;
  showHint?: boolean;
  minWidth: number;
  minHeight: number;
  children: ReactNode;
}) {
  const { panelRef, dragHandleProps, resizeHandleProps, style, isFront, bringToFront } = useDragResize(panelId, {
    minWidth,
    minHeight,
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
        <div>
          <div className="cockpit-window-title">{title}</div>
          <div className="cockpit-window-meta">{meta}</div>
        </div>
        <div className="cockpit-window-grip">drag / resize</div>
      </div>
      {showHint && hint ? <div className="cockpit-window-hint">{hint}</div> : null}
      <div className="cockpit-window-body">{children}</div>
    </section>
  );
}

export function CockpitView({ onSend, onSkipOnboarding }: CockpitViewProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [composer, setComposer] = useState("");
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
  const [workflows, setWorkflows] = useState<WorkflowInfo[]>([]);
  const [workflowRuns, setWorkflowRuns] = useState<WorkflowRunRecord[]>([]);
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [mcpServers, setMcpServers] = useState<McpServerInfo[]>([]);
  const [tools, setTools] = useState<ToolInfo[]>([]);
  const [starterPacks, setStarterPacks] = useState<StarterPackInfo[]>([]);
  const [catalogItems, setCatalogItems] = useState<CatalogItemInfo[]>([]);
  const [capabilityRecommendations, setCapabilityRecommendations] = useState<CapabilityRecommendation[]>([]);
  const [runbooks, setRunbooks] = useState<RunbookInfo[]>([]);
  const [savedRunbooks, setSavedRunbooks] = useState<RunbookInfo[]>(() => readRunbookMacros());
  const [operatorTimeline, setOperatorTimeline] = useState<OperatorTimelineEntry[]>([]);
  const [toolPolicyMode, setToolPolicyMode] = useState<ToolPolicyMode | "unknown">("unknown");
  const [mcpPolicyMode, setMcpPolicyMode] = useState<McpPolicyMode | "unknown">("unknown");
  const [approvalMode, setApprovalMode] = useState<ApprovalMode | "unknown">("unknown");
  const [operatorStatus, setOperatorStatus] = useState<string | null>(null);
  const [operatorFeed, setOperatorFeed] = useState<OperatorFeedEntry[]>([]);
  const [doctorPlans, setDoctorPlans] = useState<DoctorPlanRecord[]>([]);
  const [studioOpen, setStudioOpen] = useState(false);
  const [studioSelectedId, setStudioSelectedId] = useState<string | null>(null);
  const [studioDraft, setStudioDraft] = useState("");
  const [studioStatus, setStudioStatus] = useState<string | null>(null);
  const [studioBusy, setStudioBusy] = useState<string | null>(null);
  const [studioPreflight, setStudioPreflight] = useState<CapabilityPreflightResponse | null>(null);
  const [studioWorkflowDiagnostics, setStudioWorkflowDiagnostics] = useState<WorkflowDiagnosticsPayload | null>(null);
  const [studioDraftValidation, setStudioDraftValidation] = useState<Record<string, unknown> | null>(null);
  const [studioMcpTestResult, setStudioMcpTestResult] = useState<Record<string, unknown> | null>(null);
  const [studioMcpUrl, setStudioMcpUrl] = useState("");
  const [studioMcpDescription, setStudioMcpDescription] = useState("");
  const studioSelectionRef = useRef<string | null>(null);
  const studioLoadRequestRef = useRef(0);
  const studioValidationRequestRef = useRef(0);
  const studioSaveRequestRef = useRef(0);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [paletteQuery, setPaletteQuery] = useState("");
  const activeLayoutId = useCockpitLayoutStore((s) => s.activeLayoutId);
  const inspectorVisible = useCockpitLayoutStore((s) => s.inspectorVisible);
  const setLayout = useCockpitLayoutStore((s) => s.setLayout);
  const applyCockpitLayout = usePanelLayoutStore((s) => s.applyCockpitLayout);
  const saveCockpitLayout = usePanelLayoutStore((s) => s.saveCockpitLayout);
  const resetCockpitLayout = usePanelLayoutStore((s) => s.resetCockpitLayout);

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
    resetCockpitLayout(activeLayoutId, inspectorVisible);
  }, [activeLayoutId, inspectorVisible, resetCockpitLayout]);

  const handleSaveWorkspace = useCallback(() => {
    saveCockpitLayout(activeLayoutId);
    setOperatorStatus(`Saved ${getCockpitLayout(activeLayoutId).label} workspace`);
  }, [activeLayoutId, saveCockpitLayout]);

  const handleSelectLayout = useCallback(
    (layoutId: (typeof COCKPIT_LAYOUTS)[keyof typeof COCKPIT_LAYOUTS]["id"]) => {
      setLayout(layoutId);
      applyCockpitLayout(layoutId, inspectorVisible);
    },
    [applyCockpitLayout, inspectorVisible, setLayout],
  );

  useEffect(() => {
    void restoreLastSession();
    refreshGoals();
  }, [refreshGoals, restoreLastSession]);

  useEffect(() => {
    writeRunbookMacros(savedRunbooks);
  }, [savedRunbooks]);

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
      observerResult,
      auditResult,
      approvalsResult,
      continuityResult,
      capabilitiesResult,
      operatorTimelineResult,
      workflowRunsResult,
      toolModeResult,
      mcpModeResult,
      approvalModeResult,
    ] = await Promise.all([
      fetchJson(`${API_URL}/api/observer/state`),
      fetchJson(`${API_URL}/api/audit/events?limit=12`),
      fetchJson(`${API_URL}/api/approvals/pending?limit=8`),
      fetchJson(`${API_URL}/api/observer/continuity`),
      fetchJson(`${API_URL}/api/capabilities/overview`),
      fetchJson(`${API_URL}/api/operator/timeline?limit=12${sessionId ? `&session_id=${encodeURIComponent(sessionId)}` : ""}`),
      fetchJson(`${API_URL}/api/workflows/runs?limit=8${sessionId ? `&session_id=${encodeURIComponent(sessionId)}` : ""}`),
      fetchJson(`${API_URL}/api/settings/tool-policy-mode`),
      fetchJson(`${API_URL}/api/settings/mcp-policy-mode`),
      fetchJson(`${API_URL}/api/settings/approval-mode`),
    ]);

    if (isCancelled()) return;

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
    if (operatorTimelineResult.ok && operatorTimelineResult.payload && typeof operatorTimelineResult.payload === "object") {
      const items = (operatorTimelineResult.payload as { items?: unknown }).items;
      setOperatorTimeline(Array.isArray(items) ? items : []);
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

  const activeSession = sessions.find((item) => item.id === sessionId) ?? null;
  const activeLayout = getCockpitLayout(activeLayoutId);
  const activeSessionLabel = activeSession?.title ?? "fresh thread";
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
  const topGoals = collectGoalTitles(goalTree, 5);
  const readyStarterPacks = useMemo(
    () => starterPacks.filter((pack) => pack.availability === "ready"),
    [starterPacks],
  );
  const connectionLabel = connectionStatus === "connected" ? "live" : connectionStatus;
  const submitDisabled = isAgentBusy || !composer.trim();
  const operatorRunbooks = useMemo(
    () => runbooks,
    [runbooks],
  );
  const recentOperatorTimeline = useMemo(
    () => operatorTimeline.slice(0, 16),
    [operatorTimeline],
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
  const studioEntries = useMemo<ExtensionStudioEntry[]>(
    () => [
      ...workflows.map((workflow) => ({
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
      })),
      ...mcpServers.map((server) => ({
        id: `mcp:${server.name}`,
        entityType: "mcp" as const,
        name: server.name,
        summary: server.description || server.url || "MCP server",
        availability: server.availability ?? server.status ?? "unknown",
        meta: `${server.status ?? "unknown"} · ${server.tool_count ?? 0} tools`,
        entity: buildMcpEntity(server),
      })),
    ],
    [mcpServers, skills, workflows],
  );
  const selectedStudioEntry = useMemo(
    () => studioEntries.find((entry) => entry.id === studioSelectedId) ?? studioEntries[0] ?? null,
    [studioEntries, studioSelectedId],
  );
  useEffect(() => {
    studioSelectionRef.current = selectedStudioEntry?.id ?? null;
  }, [selectedStudioEntry?.id]);
  const studioRecommendedActions = useMemo(
    () => readActionList(selectedStudioEntry?.entity.details.recommended_actions),
    [selectedStudioEntry],
  );

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
    const loadStudioSource = async () => {
      try {
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
  const recentOperatorFeed = useMemo(
    () => {
      const timelineFeed: OperatorFeedEntry[] = recentOperatorTimeline.map((entry) => ({
        id: `timeline:${entry.id}`,
        summary: `${entry.title}: ${entry.summary}`,
        status:
          entry.status === "failed"
            ? "failed"
            : entry.status === "selected" || entry.status === "delivered" || entry.status === "queued"
              ? "success"
              : "info",
        createdAt: entry.updated_at || entry.created_at,
      }));
      return [...operatorFeed, ...timelineFeed]
        .sort((left, right) => new Date(right.createdAt).getTime() - new Date(left.createdAt).getTime())
        .slice(0, 14);
    },
    [operatorFeed, recentOperatorTimeline],
  );
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
      if (item.installed) return;
      items.push({
        id: `catalog:${item.type}:${item.name}`,
        kind: `install ${item.type}`,
        label: item.name,
        detail: `${item.description}${item.missing_tools?.length ? ` · missing tools ${item.missing_tools.join(", ")}` : ""}`,
        action: item.recommended_actions?.[0] ?? { type: "install_catalog_item", label: "Install", name: item.name },
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

  function appendOperatorFeed(summary: string, status: OperatorFeedEntry["status"]) {
    const entry: OperatorFeedEntry = {
      id: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
      summary,
      status,
      createdAt: new Date().toISOString(),
    };
    setOperatorFeed((current) => [entry, ...current].slice(0, 18));
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
    const entryId = entry.id;
    const fileName = managedFileName(entry.entity.details.file_path);
    const requestId = ++studioSaveRequestRef.current;
    setStudioBusy("save");
    setStudioStatus(`Saving ${entry.name}...`);
    try {
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

  function queueArtifactWorkflowDraft(workflow: WorkflowInfo, artifactPath: string) {
    queueComposerDraft(buildWorkflowDraft(workflow, artifactPath));
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
      await runCapabilityActions(result.manual_actions, `${label} bootstrap`);
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
      const response = await fetch(`${API_URL}/api/catalog/install/${item.name}`, {
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
    const safeTypes = new Set<CapabilityAction["type"]>([
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
    const allowedActions = actions.filter((action) => safeTypes.has(action.type));
    if (allowedActions.length === 0) {
      setOperatorStatus(`No safe repair actions available for ${label}`);
      appendOperatorFeed(`No safe repair actions available for ${label}`, "failed");
      return;
    }
    setOperatorStatus(`Repairing ${label}...`);
    for (const action of allowedActions) {
      await runCapabilityAction(action);
    }
    setOperatorStatus(`${label} repair sequence applied`);
    appendOperatorFeed(`${label} repair sequence applied`, "success");
  }

  async function runCapabilityAction(action: CapabilityAction | null | undefined) {
    if (!action) return;
    switch (action.type) {
      case "toggle_skill": {
        const skill = skills.find((item) => item.name === action.name);
        if (skill) await toggleSkill(skill);
        return;
      }
      case "toggle_workflow": {
        const workflow = workflows.find((item) => item.name === action.name);
        if (workflow) await toggleWorkflow(workflow, Boolean(action.enabled));
        return;
      }
      case "toggle_mcp_server": {
        const server = mcpServers.find((item) => item.name === action.name);
        if (server) await toggleMcpServer(server);
        return;
      }
      case "test_mcp_server": {
        const server = mcpServers.find((item) => item.name === action.name);
        if (server) await testMcpServer(server);
        return;
      }
      case "test_native_notification":
        await sendTestNativeNotification();
        return;
      case "set_tool_policy":
        if (action.mode === "safe" || action.mode === "balanced" || action.mode === "full") {
          await updateToolPolicy(action.mode);
        }
        return;
      case "set_mcp_policy":
        if (action.mode === "disabled" || action.mode === "approval" || action.mode === "full") {
          await updateMcpPolicy(action.mode);
        }
        return;
      case "install_catalog_item": {
        const item = catalogItems.find((entry) => entry.name === action.name);
        if (item) await installCatalogItem(item);
        return;
      }
      case "activate_starter_pack": {
        const pack = starterPacks.find((entry) => entry.name === action.name);
        if (pack) await activateStarterPack(pack);
        return;
      }
      case "draft_workflow": {
        const workflow = workflows.find((entry) => entry.name === action.name);
        if (workflow) queueComposerDraft(buildWorkflowDraft(workflow));
        return;
      }
      case "open_settings":
        setSettingsPanelOpen(true);
        return;
      default:
        return;
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

  async function toggleMcpServer(server: McpServerInfo) {
    setOperatorStatus(`${server.enabled ? "Disabling" : "Enabling"} ${server.name}...`);
    try {
      const response = await fetch(`${API_URL}/api/mcp/servers/${server.name}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: !server.enabled }),
      });
      if (!response.ok) {
        setOperatorStatus(`Failed to update ${server.name}`);
        appendOperatorFeed(`Failed to update MCP ${server.name}`, "failed");
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
      const response = await fetch(`${API_URL}/api/mcp/servers/${server.name}/test`, { method: "POST" });
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

    if (selectedInspector.kind === "approval") {
      const approval = selectedInspector.approval;
      title = approval.tool_name;
      meta = `${approval.risk_level} approval`;
      body = approval.summary;
      details = {
        approval_id: approval.id,
        session_id: approval.session_id ?? "n/a",
        thread: approval.thread_label ?? approval.thread_id ?? approval.session_id ?? "n/a",
        status: approval.status,
        resolution: approvalState[approval.id] ?? "pending",
        resume_message: approval.resume_message ?? "n/a",
      };
    } else if (selectedInspector.kind === "workflow") {
      const workflow = selectedInspector.workflow;
      const approval = approvalForWorkflow(workflow);
      const linkedInterventions = interventionsForWorkflow(workflow);
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
        risk_level: workflow.riskLevel ?? "unknown",
        execution_boundaries: workflow.executionBoundaries ?? [],
        accepts_secret_refs: workflow.acceptsSecretRefs ?? false,
        step_tools: workflow.stepTools,
        step_records: workflow.stepRecords ?? [],
        continued_error_steps: workflow.continuedErrorSteps,
        artifact_paths: workflow.artifactPaths,
        pending_approval: approval ? approval.id : "none",
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
        {selectedInspector.kind === "workflow" && (
          <div className="cockpit-feedback-row">
            {selectedInspector.workflow.replayAllowed !== false ? (
              <>
                <button
                  className="cockpit-feedback-button"
                  onClick={() =>
                    queueComposerDraft(
                      selectedInspector.workflow.replayDraft
                        ?? buildWorkflowReplayDraft(selectedInspector.workflow),
                    )
                  }
                >
                  {selectedInspector.workflow.executionBoundaries?.length
                    ? "Draft Boundary-Aware Rerun"
                    : "Draft Rerun"}
                </button>
                {selectedInspector.workflow.retryFromStepDraft && (
                  <button
                    className="cockpit-feedback-button"
                    onClick={() =>
                      queueComposerDraft(
                        selectedInspector.workflow.retryFromStepDraft
                        ?? buildWorkflowReplayDraft(selectedInspector.workflow),
                      )
                    }
                  >
                    Retry From Step
                  </button>
                )}
                {studioEntryForWorkflowRun(selectedInspector.workflow) && (
                  <button
                    className="cockpit-feedback-button"
                    onClick={() => openExtensionStudio(studioEntryForWorkflowRun(selectedInspector.workflow))}
                  >
                    Open Studio
                  </button>
                )}
              </>
            ) : (
              <span className="cockpit-feedback-status">
                Replay blocked: {replayBlockCopy(selectedInspector.workflow.replayBlockReason)}
              </span>
            )}
            {selectedInspector.workflow.threadId && (
              <button
                className="cockpit-feedback-button"
                onClick={() => void openThread(selectedInspector.workflow.threadId)}
              >
                Open Thread
              </button>
            )}
            {selectedInspector.workflow.artifactPaths[0] && (
              <button
                className="cockpit-feedback-button"
                onClick={() =>
                  queueComposerDraft(
                    `Use the workspace file "${selectedInspector.workflow.artifactPaths[0]}" as context for the next action.`,
                  )
                }
                >
                  Use Output
                </button>
              )}
              {selectedInspector.workflow.artifactPaths[0]
                && artifactRoundtripWorkflows.slice(0, 2).map((workflow) => (
                  <button
                    key={`${selectedInspector.workflow.id}:${workflow.name}`}
                    className="cockpit-feedback-button"
                    onClick={() =>
                      queueArtifactWorkflowDraft(
                        workflow,
                        selectedInspector.workflow.artifactPaths[0]!,
                      )
                    }
                  >
                    Run {workflow.name}
                  </button>
                ))}
            {selectedInspector.workflow.approvalRecoveryMessage && (
              <span className="cockpit-feedback-status">
                {selectedInspector.workflow.approvalRecoveryMessage}
              </span>
            )}
          </div>
        )}
        {selectedInspector.kind === "workflow" && workflowResumeDetails(selectedInspector.workflow).length > 0 && (
          <div className="cockpit-chip-row">
            {workflowResumeDetails(selectedInspector.workflow).map((detail) => (
              <span key={`${selectedInspector.workflow.id}:${detail}`} className="cockpit-chip">
                {detail}
              </span>
            ))}
          </div>
        )}
        {selectedInspector.kind === "workflow" && selectedInspector.workflow.timeline?.length && (
          <div className="cockpit-inspector-stack">
            {selectedInspector.workflow.timeline.map((entry) => (
              <div key={`${selectedInspector.workflow.id}:${entry.kind}:${entry.at}`} className="cockpit-inspector-stack-row">
                <div className="cockpit-key">{entry.kind.replace(/_/g, " ")}</div>
                <div className="cockpit-value">
                  {entry.summary}
                  {entry.stepId ? ` · ${entry.stepId}` : ""}
                  {entry.durationMs ? ` · ${entry.durationMs}ms` : ""}
                </div>
              </div>
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
            {artifactRoundtripWorkflows.slice(0, 2).map((workflow) => (
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
          <div className="cockpit-eyebrow cockpit-brandmark">Seraph</div>
          <div className="cockpit-toolbar-hint">Backtick (`) focuses the command bar</div>
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
              Goals overlay
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
        {activeLayout.sections.rail && (
          <>
            <CockpitWorkspaceWindow
              panelId="sessions_pane"
              title="Sessions"
              meta={activeSession ? activeSession.title : "fresh thread"}
              hint={COCKPIT_WINDOW_HINTS.sessions}
              showHint={cockpitHintsEnabled}
              minWidth={260}
              minHeight={180}
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

            <CockpitWorkspaceWindow
              panelId="goals_pane"
              title="Goals"
              meta={loadingGoals ? "refreshing" : `${dashboard?.active_count ?? 0} active`}
              hint={COCKPIT_WINDOW_HINTS.goals}
              showHint={cockpitHintsEnabled}
              minWidth={280}
              minHeight={220}
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
                  <div className="cockpit-empty">Goal dashboard unavailable.</div>
                )}
                <div className="cockpit-sublist">
                  {topGoals.map((goal) => (
                    <div key={goal} className="cockpit-sublist-item">
                      {goal}
                    </div>
                  ))}
                  {topGoals.length === 0 && <div className="cockpit-empty">No active goals yet.</div>}
                </div>
              </section>
            </CockpitWorkspaceWindow>

            <CockpitWorkspaceWindow
              panelId="outputs_pane"
              title="Recent outputs"
              meta={`${artifacts.length} files`}
              hint={COCKPIT_WINDOW_HINTS.outputs}
              showHint={cockpitHintsEnabled}
              minWidth={280}
              minHeight={180}
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

            <CockpitWorkspaceWindow
              panelId="approvals_pane"
              title="Pending approvals"
              meta={`${pendingApprovals.length} waiting`}
              hint={COCKPIT_WINDOW_HINTS.approvals}
              showHint={cockpitHintsEnabled}
              minWidth={300}
              minHeight={220}
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
          </>
        )}

        {(latestResponse || isAgentBusy) && (
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

        {activeLayout.sections.guardianState && (
          <CockpitWorkspaceWindow
            panelId="guardian_state_pane"
            title="Guardian state"
            meta={`${observerState?.time_of_day ?? "pending"} · ${observerState?.day_of_week ?? "today"}`}
            hint={COCKPIT_WINDOW_HINTS.guardianState}
            showHint={cockpitHintsEnabled}
            minWidth={420}
            minHeight={260}
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

        {activeLayout.sections.timeline && (
          <CockpitWorkspaceWindow
            panelId="operator_timeline_pane"
            title="Operator timeline"
            meta={`${recentOperatorTimeline.length} live`}
            hint={COCKPIT_WINDOW_HINTS.operatorTimeline}
            showHint={cockpitHintsEnabled}
            minWidth={360}
            minHeight={220}
          >
            <section className="cockpit-panel cockpit-panel--embedded">
              <div className="cockpit-list">
                {recentOperatorTimeline.map((item) => (
                  <div key={item.id} className="cockpit-row">
                    <button
                      className="cockpit-row-button"
                      onClick={() =>
                        setSelectedInspector({
                          kind: "operator",
                          entity: {
                            entityType: "operator_timeline_item",
                            name: item.title,
                            meta: timelineStatusLabel(item),
                            summary: item.summary,
                            details: {
                              source: item.source,
                              thread: item.thread_label ?? item.thread_id ?? "ambient",
                              status: item.status,
                              continue_message: item.continue_message ?? "",
                              replay_allowed: item.replay_allowed ?? false,
                              replay_block_reason: item.replay_block_reason ?? "none",
                              metadata: item.metadata ?? {},
                            },
                          },
                        })
                      }
                    >
                      <div className="cockpit-row-header">
                        <span className="cockpit-role">{item.title}</span>
                        <span className="cockpit-row-age">{formatAge(item.updated_at)}</span>
                      </div>
                      <div className="cockpit-row-body">{item.summary}</div>
                      <div className="cockpit-row-meta">
                        {timelineStatusLabel(item)}
                        {item.thread_label ? ` · ${item.thread_label}` : ""}
                      </div>
                      {item.kind === "routing" && item.metadata && (
                        <>
                          <div className="cockpit-row-meta">
                            model {String(item.metadata.selected_model ?? "unknown")}
                            {item.metadata.selected_source ? ` · ${String(item.metadata.selected_source)}` : ""}
                            {item.metadata.reroute_cause ? ` · ${String(item.metadata.reroute_cause)}` : ""}
                            {item.metadata.max_budget_class ? ` · budget ${String(item.metadata.max_budget_class)}` : ""}
                            {item.metadata.required_task_class ? ` · task ${String(item.metadata.required_task_class)}` : ""}
                          </div>
                          <div className="cockpit-row-meta">
                            {Array.isArray(item.metadata.required_policy_intents) && item.metadata.required_policy_intents.length
                              ? `intents ${item.metadata.required_policy_intents.join(", ")}`
                              : "no required intents"}
                            {item.metadata.max_cost_tier ? ` · cost ${String(item.metadata.max_cost_tier)}` : ""}
                            {item.metadata.max_latency_tier ? ` · latency ${String(item.metadata.max_latency_tier)}` : ""}
                            {typeof item.metadata.rejected_target_count === "number"
                              ? ` · rejected ${String(item.metadata.rejected_target_count)}`
                              : ""}
                          </div>
                        </>
                      )}
                    </button>
                    <div className="cockpit-feedback-row">
                      {item.continue_message && (
                        <button
                          className="cockpit-feedback-button"
                          onClick={() => void queueThreadDraft(item.continue_message ?? "", item.thread_id)}
                        >
                          Continue
                        </button>
                      )}
                      {item.thread_id && (
                        <button
                          className="cockpit-feedback-button"
                          onClick={() => void openThread(item.thread_id)}
                        >
                          Open Thread
                        </button>
                      )}
                      {item.replay_draft && item.replay_allowed !== false && (
                        <button
                          className="cockpit-feedback-button"
                          onClick={() => queueComposerDraft(item.replay_draft ?? "")}
                        >
                          Replay
                        </button>
                      )}
                      {item.recommended_actions?.length ? (
                        <button
                          className="cockpit-feedback-button"
                          onClick={() => void runCapabilityActions(item.recommended_actions ?? [], item.title)}
                        >
                          Repair
                        </button>
                      ) : null}
                    </div>
                  </div>
                ))}
                {recentOperatorTimeline.length === 0 && (
                  <div className="cockpit-empty">No threaded operator timeline entries yet.</div>
                )}
              </div>
            </section>
          </CockpitWorkspaceWindow>
        )}

        {activeLayout.sections.workflows && (
          <CockpitWorkspaceWindow
            panelId="workflows_pane"
            title="Workflow timeline"
            meta={`${workflowRunsWithArtifacts.length} recent`}
            hint={COCKPIT_WINDOW_HINTS.workflowTimeline}
            showHint={cockpitHintsEnabled}
            minWidth={380}
            minHeight={220}
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
                        onClick={() => setSelectedInspector({ kind: "workflow", workflow })}
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
                        {studioEntryForWorkflowRun(workflow) ? (
                          <button
                            className="cockpit-feedback-button"
                            onClick={() => openExtensionStudio(studioEntryForWorkflowRun(workflow))}
                          >
                            Studio
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

        {activeLayout.sections.interventions && (
          <CockpitWorkspaceWindow
            panelId="interventions_pane"
            title="Interventions"
            meta={`${recentInterventions.length} recent`}
            hint={COCKPIT_WINDOW_HINTS.interventions}
            showHint={cockpitHintsEnabled}
            minWidth={380}
            minHeight={220}
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

        {activeLayout.sections.audit && (
          <CockpitWorkspaceWindow
            panelId="audit_pane"
            title="Audit surface"
            meta={`${auditEvents.length} events`}
            hint={COCKPIT_WINDOW_HINTS.audit}
            showHint={cockpitHintsEnabled}
            minWidth={340}
            minHeight={220}
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

        {activeLayout.sections.trace && (
          <CockpitWorkspaceWindow
            panelId="trace_pane"
            title="Live trace"
            meta={isAgentBusy ? "agent active" : "idle"}
            hint={COCKPIT_WINDOW_HINTS.trace}
            showHint={cockpitHintsEnabled}
            minWidth={320}
            minHeight={180}
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

        {activeLayout.sections.inspector && inspectorVisible && (
          <CockpitWorkspaceWindow
            panelId="inspector_pane"
            title="Operations inspector"
            meta={selectedInspector ? selectedInspector.kind : "nothing selected"}
            hint={COCKPIT_WINDOW_HINTS.inspector}
            showHint={cockpitHintsEnabled}
            minWidth={480}
            minHeight={240}
          >
            <section className="cockpit-panel cockpit-panel--embedded">
              <div className="cockpit-feed">{renderInspector()}</div>
            </section>
          </CockpitWorkspaceWindow>
        )}

        {activeLayout.sections.conversation && (
          <>
            <CockpitWorkspaceWindow
              panelId="presence_pane"
              title="Seraph presence"
              meta={connectionStatus === "connected" ? "runtime linked" : connectionLabel}
              hint={COCKPIT_WINDOW_HINTS.presence}
              showHint={cockpitHintsEnabled}
              minWidth={368}
              minHeight={256}
            >
              <SeraphPresencePane snapshot={seraphPresenceSnapshot} />
            </CockpitWorkspaceWindow>

            <CockpitWorkspaceWindow
              panelId="conversation_pane"
              title="Conversation"
              meta={activeSession?.title ?? "fresh thread · saved after first reply"}
              hint={COCKPIT_WINDOW_HINTS.conversation}
              showHint={cockpitHintsEnabled}
              minWidth={360}
              minHeight={260}
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

            <CockpitWorkspaceWindow
              panelId="desktop_shell_pane"
              title="Desktop shell"
              meta={`${daemonPresence?.connected ? "linked" : "offline"} · ${desktopNotifications.length} alerts`}
              hint={COCKPIT_WINDOW_HINTS.desktopShell}
              showHint={cockpitHintsEnabled}
              minWidth={340}
              minHeight={220}
            >
              <section className="cockpit-panel cockpit-panel--embedded">
                <div className="cockpit-sublist">
                  <div className="cockpit-sublist-item">
                    capture {daemonPresence?.capture_mode ?? "unknown"} · bundle {queuedInsights.length} · recent {recentInterventions.length}
                  </div>
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
                        {item.thread_label
                          ?? (item.thread_id ? sessionTitleById[item.thread_id] ?? `thread ${item.thread_id.slice(0, 6)}` : "ambient queue")}
                      </div>
                      <div className="cockpit-feedback-row">
                        <button
                          className="cockpit-feedback-button"
                          onClick={() =>
                            void queueThreadDraft(
                              `Follow up on this deferred guardian item: ${item.content_excerpt}`,
                              item.thread_id,
                            )
                          }
                        >
                          Draft Follow-up
                        </button>
                        {item.thread_id && (
                          <button
                            className="cockpit-feedback-button"
                            onClick={() => void openThread(item.thread_id)}
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
                      </div>
                      <div className="cockpit-feedback-row">
                        <button
                          className="cockpit-feedback-button"
                          onClick={() =>
                            void queueThreadDraft(
                              `Continue from this guardian intervention: ${item.content_excerpt}`,
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

            <CockpitWorkspaceWindow
              panelId="operator_surface_pane"
              title="Operator terminal"
              meta={`tool ${toolPolicyMode} · mcp ${mcpPolicyMode}`}
              hint={COCKPIT_WINDOW_HINTS.operatorTerminal}
              showHint={cockpitHintsEnabled}
              minWidth={360}
              minHeight={260}
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
                      <span className="cockpit-key">live logs</span>
                      <span className="cockpit-operator-link">{recentOperatorFeed.length} recent</span>
                    </div>
                    {recentOperatorFeed.map((entry) => (
                      <div key={entry.id} className="cockpit-operator-row">
                        <div className="cockpit-operator-details">
                          <div className="cockpit-value">{entry.summary}</div>
                          <div className="cockpit-operator-note">
                            {formatFeedStatus(entry.status)} · {formatAge(entry.createdAt)}
                          </div>
                        </div>
                      </div>
                    ))}
                    {recentOperatorFeed.length === 0 && (
                      <div className="cockpit-empty">No operator actions recorded yet.</div>
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
                        {catalogItems.filter((item) => !item.installed).length} missing
                      </span>
                    </div>
                    {catalogItems.filter((item) => !item.installed).map((item) => (
                      <div key={`${item.type}:${item.name}`} className="cockpit-operator-row cockpit-operator-row--entry">
                        <button
                          type="button"
                          className="cockpit-operator-details cockpit-operator-details--button"
                          onClick={() =>
                            setSelectedInspector({
                              kind: "operator",
                              entity: {
                                entityType: item.type === "skill" ? "skill" : "mcp",
                                name: item.name,
                                meta: `install ${item.type.replace("_", " ")}`,
                                summary: item.description,
                                details: {
                                  category: item.category ?? "",
                                  bundled: item.bundled ?? false,
                                  missing_tools: item.missing_tools ?? [],
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
                            install
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
          </>
        )}
      </div>

      {studioOpen && selectedStudioEntry && (
        <div className="cockpit-overlay-backdrop" onClick={() => setStudioOpen(false)}>
          <section
            className="cockpit-palette cockpit-studio"
            onClick={(event) => event.stopPropagation()}
            aria-label="Extension studio"
          >
            <div className="cockpit-window-header">
              <div>
                <div className="cockpit-window-title">Extension studio</div>
                <div className="cockpit-window-meta">
                  validate, repair, and author workflows, skills, and MCP config from the cockpit
                </div>
              </div>
              <div className="cockpit-window-grip">
                {selectedStudioEntry.entityType.replace(/_/g, " ")} · {selectedStudioEntry.availability}
              </div>
            </div>
            <div className="cockpit-studio-shell">
              <aside className="cockpit-studio-sidebar">
                <div className="cockpit-operator-row">
                  <span className="cockpit-key">extensions</span>
                  <span className="cockpit-operator-link">{studioEntries.length} loaded</span>
                </div>
                <div className="cockpit-list cockpit-list--palette">
                  {studioEntries.map((entry) => (
                    <button
                      key={entry.id}
                      type="button"
                      className={`cockpit-sublist-button ${selectedStudioEntry.id === entry.id ? "active" : ""}`}
                      onClick={() => setStudioSelectedId(entry.id)}
                    >
                      <span>{entry.name}</span>
                      <span className="cockpit-row-age">{entry.entityType === "workflow_definition" ? "workflow" : entry.entityType}</span>
                    </button>
                  ))}
                </div>
              </aside>
              <div className="cockpit-studio-main">
                <div className="cockpit-inspector">
                  <div className="cockpit-inspector-title">{selectedStudioEntry.name}</div>
                  <div className="cockpit-inspector-meta">{selectedStudioEntry.meta}</div>
                  <div className="cockpit-inspector-body">{selectedStudioEntry.summary}</div>
                  <div className="cockpit-chip-row">
                    <span className="cockpit-chip">{selectedStudioEntry.availability}</span>
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
                      disabled={studioBusy === "validation"}
                    >
                      {selectedStudioEntry.entityType === "mcp" ? "Validate config" : "Refresh validation"}
                    </button>
                    <button
                      className="cockpit-feedback-button"
                      onClick={() => void saveStudioDraft()}
                      disabled={studioBusy === "save"}
                    >
                      {selectedStudioEntry.entityType === "mcp" ? "Save config" : "Save draft"}
                    </button>
                    {selectedStudioEntry.entityType !== "mcp" ? (
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
                          : selectedStudioEntry.entityType === "skill"
                            ? "Use validation to check draft syntax and current runtime blockers."
                            : "Run validation to capture a current plan."}
                      </div>
                    </div>
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
                      />
                      <label className="cockpit-key" htmlFor="studio-mcp-description">description</label>
                      <input
                        id="studio-mcp-description"
                        className="cockpit-input"
                        value={studioMcpDescription}
                        onChange={(event) => setStudioMcpDescription(event.target.value)}
                        placeholder="What this MCP server provides"
                      />
                    </div>
                  ) : (
                    <div className="cockpit-studio-form">
                      <label className="cockpit-key" htmlFor="studio-authoring-draft">authoring draft</label>
                      <textarea
                        id="studio-authoring-draft"
                        className="cockpit-input cockpit-studio-textarea"
                        value={studioDraft}
                        onChange={(event) => setStudioDraft(event.target.value)}
                      />
                    </div>
                  )}
                </div>
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
              <div>
                <div className="cockpit-window-title">Capability palette</div>
                <div className="cockpit-window-meta">
                  keyboard-first launcher for capabilities, installs, repairs, and runbooks
                </div>
              </div>
              <div className="cockpit-window-grip">Shift+K / Ctrl+K</div>
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
          <span>command bar</span>
          <span className="cockpit-composer-meta-center">
            {activeLayout.label} workspace · 16px grid snap · {activeSessionLabel}
          </span>
          <span>{isAgentBusy ? "Seraph is responding" : `thread ${activeSessionLabel}`}</span>
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
