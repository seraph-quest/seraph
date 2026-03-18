import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from "react";

import { EventBus } from "../../game/EventBus";
import { API_URL } from "../../config/constants";
import { useChatStore } from "../../stores/chatStore";
import { useQuestStore } from "../../stores/questStore";
import { useCockpitLayoutStore } from "../../stores/cockpitLayoutStore";
import type { ChatMessage, GoalInfo } from "../../types";
import { buildWorkflowDraft, type WorkflowInfo } from "../settings/workflowDraft";
import {
  collectArtifacts,
  collectWorkflowRuns,
  formatInspectorValue,
  type ArtifactRecord,
  type CockpitAuditEvent,
  type WorkflowRunRecord,
} from "./inspector";
import { COCKPIT_LAYOUTS, getCockpitLayout } from "./layouts";

interface CockpitViewProps {
  onSend: (message: string) => void;
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
  tool_name: string;
  risk_level: string;
  status: string;
  summary: string;
  created_at: string;
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
  }>;
  queued_insights: Array<{
    id: string;
    intervention_id: string | null;
    content_excerpt: string;
    intervention_type: string;
    urgency: number;
    reasoning: string;
    created_at: string;
  }>;
  queued_insight_count: number;
  recent_interventions: GuardianContinuityIntervention[];
}

type InspectorSelection =
  | { kind: "approval"; approval: PendingApproval }
  | { kind: "workflow"; workflow: WorkflowRunRecord }
  | { kind: "intervention"; intervention: GuardianContinuityIntervention }
  | { kind: "trace"; message: ChatMessage }
  | { kind: "audit"; event: CockpitAuditEvent }
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
  const inputs = workflow.arguments
    ? Object.entries(workflow.arguments).map(([name, value]) => `${name}=${JSON.stringify(value)}`)
    : [];
  return inputs.length
    ? `Run workflow "${workflow.workflowName}" with ${inputs.join(", ")}.`
    : `Run workflow "${workflow.workflowName}".`;
}

function supportsArtifactRoundtrip(workflow: WorkflowInfo): boolean {
  return Object.prototype.hasOwnProperty.call(workflow.inputs, "file_path");
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
  const activeLayoutId = useCockpitLayoutStore((s) => s.activeLayoutId);
  const inspectorVisible = useCockpitLayoutStore((s) => s.inspectorVisible);
  const setLayout = useCockpitLayoutStore((s) => s.setLayout);
  const resetLayout = useCockpitLayoutStore((s) => s.resetLayout);

  const messages = useChatStore((s) => s.messages);
  const sessions = useChatStore((s) => s.sessions);
  const sessionId = useChatStore((s) => s.sessionId);
  const connectionStatus = useChatStore((s) => s.connectionStatus);
  const isAgentBusy = useChatStore((s) => s.isAgentBusy);
  const ambientState = useChatStore((s) => s.ambientState);
  const ambientTooltip = useChatStore((s) => s.ambientTooltip);
  const onboardingCompleted = useChatStore((s) => s.onboardingCompleted);
  const loadSessions = useChatStore((s) => s.loadSessions);
  const switchSession = useChatStore((s) => s.switchSession);
  const newSession = useChatStore((s) => s.newSession);
  const setQuestPanelOpen = useChatStore((s) => s.setQuestPanelOpen);
  const setSettingsPanelOpen = useChatStore((s) => s.setSettingsPanelOpen);
  const setInterfaceMode = useChatStore((s) => s.setInterfaceMode);

  const dashboard = useQuestStore((s) => s.dashboard);
  const goalTree = useQuestStore((s) => s.goalTree);
  const loadingGoals = useQuestStore((s) => s.loading);
  const refreshGoals = useQuestStore((s) => s.refresh);

  useEffect(() => {
    loadSessions();
    refreshGoals();
  }, [loadSessions, refreshGoals]);

  const refreshCockpit = useCallback(async (cancelled = false) => {
    try {
      const [
        observerResponse,
        auditResponse,
        approvalsResponse,
        continuityResponse,
        workflowsResponse,
      ] = await Promise.all([
        fetch(`${API_URL}/api/observer/state`),
        fetch(`${API_URL}/api/audit/events?limit=12`),
        fetch(`${API_URL}/api/approvals/pending?limit=8`),
        fetch(`${API_URL}/api/observer/continuity`),
        fetch(`${API_URL}/api/workflows`),
      ]);

      if (!cancelled && observerResponse.ok) {
        const observerPayload = await observerResponse.json();
        setObserverState(observerPayload);
      }

      if (!cancelled && auditResponse.ok) {
        const auditPayload = await auditResponse.json();
        setAuditEvents(Array.isArray(auditPayload) ? auditPayload : []);
      }

      if (!cancelled && approvalsResponse.ok) {
        const approvalsPayload = await approvalsResponse.json();
        setPendingApprovals(Array.isArray(approvalsPayload) ? approvalsPayload : []);
      }

      if (!cancelled && continuityResponse.ok) {
        const continuityPayload: ObserverContinuitySnapshot = await continuityResponse.json();
        setDaemonPresence(continuityPayload.daemon);
        setDesktopNotifications(continuityPayload.notifications ?? []);
        setQueuedInsights(continuityPayload.queued_insights ?? []);
        setQueuedBundleCount(continuityPayload.queued_insight_count ?? 0);
        setRecentInterventions(continuityPayload.recent_interventions ?? []);
      }
      if (!cancelled && workflowsResponse.ok) {
        const workflowPayload = await workflowsResponse.json();
        setWorkflows(Array.isArray(workflowPayload.workflows) ? workflowPayload.workflows : []);
      }
    } catch {
      if (!cancelled) {
        setAuditEvents([]);
        setPendingApprovals([]);
        setDaemonPresence(null);
        setDesktopNotifications([]);
        setQueuedInsights([]);
        setQueuedBundleCount(0);
        setRecentInterventions([]);
        setWorkflows([]);
      }
    }
  }, []);

  useEffect(() => {
    let cancelled = false;

    const refresh = async () => {
      try {
        await refreshCockpit(cancelled);
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

  const activeSession = sessions.find((item) => item.id === sessionId) ?? null;
  const activeLayout = getCockpitLayout(activeLayoutId);
  const recentConversation = messages.slice(-18);
  const artifacts = useMemo(() => collectArtifacts(auditEvents), [auditEvents]);
  const workflowRuns = useMemo(() => collectWorkflowRuns(auditEvents).slice(0, 6), [auditEvents]);
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
  const recentTrace = messages
    .filter((message) => message.role === "step" || message.role === "error")
    .slice(-8)
    .reverse();
  const topGoals = collectGoalTitles(goalTree, 5);
  const connectionLabel = connectionStatus === "connected" ? "live" : connectionStatus;
  const submitDisabled = connectionStatus !== "connected" || isAgentBusy || !composer.trim();

  function approvalForWorkflow(workflow: WorkflowRunRecord): PendingApproval | null {
    return pendingApprovals.find((approval) => approval.tool_name === workflow.toolName) ?? null;
  }

  function interventionsForWorkflow(workflow: WorkflowRunRecord): GuardianContinuityIntervention[] {
    if (!workflow.sessionId || workflow.sessionId !== sessionId) return [];
    return recentInterventions.filter((intervention) => intervention.session_id === workflow.sessionId);
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
        EventBus.emit("approval-resume", {
          sessionId: payload.session_id ?? approval.session_id ?? null,
          message: payload.resume_message,
        });
      }
    } catch {
      setApprovalState((current) => ({ ...current, [approval.id]: "failed" }));
    }
  }

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const message = composer.trim();
    if (!message || submitDisabled) return;
    onSend(message);
    setComposer("");
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
        status: approval.status,
        resolution: approvalState[approval.id] ?? "pending",
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
        status: workflow.status,
        step_tools: workflow.stepTools,
        continued_error_steps: workflow.continuedErrorSteps,
        artifact_paths: workflow.artifactPaths,
        pending_approval: approval ? approval.id : "none",
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
            <button
              className="cockpit-feedback-button"
              onClick={() => queueComposerDraft(buildWorkflowReplayDraft(selectedInspector.workflow))}
            >
              Draft Rerun
            </button>
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
          <div className="cockpit-eyebrow">Seraph</div>
          <div className="cockpit-title">Guardian Cockpit</div>
          <div className="cockpit-subtitle">
            dense operator surface for state, evidence, interventions, and action
          </div>
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
            <button className="cockpit-action cockpit-action--ghost" onClick={() => newSession()}>
              New session
            </button>
            <button
              className="cockpit-action cockpit-action--ghost"
              onClick={() => setQuestPanelOpen(true)}
            >
              Goals overlay
            </button>
            <button
              className="cockpit-action cockpit-action--ghost"
              onClick={() => setSettingsPanelOpen(true)}
            >
              Settings
            </button>
            <button
              className="cockpit-action cockpit-action--primary"
              onClick={() => setInterfaceMode("village")}
            >
              Legacy village
            </button>
          </div>

          <div className="cockpit-layout-row">
            {Object.values(COCKPIT_LAYOUTS).map((layout) => (
              <button
                key={layout.id}
                className={`cockpit-action cockpit-action--ghost ${
                  activeLayoutId === layout.id ? "cockpit-action--active" : ""
                }`}
                onClick={() => setLayout(layout.id)}
                title={layout.description}
              >
                {layout.label}
              </button>
            ))}
            <button className="cockpit-action cockpit-action--ghost" onClick={() => resetLayout()}>
              Reset view
            </button>
          </div>
        </div>
      </header>

      <div className={`cockpit-grid cockpit-grid--${activeLayoutId}`}>
        {activeLayout.sections.rail && (
        <aside className="cockpit-rail">
          <section className="cockpit-panel">
            <div className="cockpit-card-header">
              <div className="cockpit-card-title">Sessions</div>
              <div className="cockpit-card-meta">
                {activeSession ? activeSession.title : "new session"}
              </div>
            </div>
            <div className="cockpit-list">
              {sessions.slice(0, 8).map((session) => (
                <button
                  key={session.id}
                  className={`cockpit-session ${session.id === sessionId ? "active" : ""}`}
                  onClick={() => switchSession(session.id)}
                >
                  <span className="cockpit-session-title">{session.title}</span>
                  <span className="cockpit-session-meta">{formatAge(session.updated_at)}</span>
                </button>
              ))}
              {sessions.length === 0 && (
                <div className="cockpit-empty">No saved sessions yet.</div>
              )}
            </div>
          </section>

          <section className="cockpit-panel">
            <div className="cockpit-card-header">
              <div className="cockpit-card-title">Goals</div>
              <div className="cockpit-card-meta">
                {loadingGoals ? "refreshing" : `${dashboard?.active_count ?? 0} active`}
              </div>
            </div>
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

          <section className="cockpit-panel">
            <div className="cockpit-card-header">
              <div className="cockpit-card-title">Recent outputs</div>
              <div className="cockpit-card-meta">{artifacts.length} files</div>
            </div>
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

          <section className="cockpit-panel">
            <div className="cockpit-card-header">
              <div className="cockpit-card-title">Pending approvals</div>
              <div className="cockpit-card-meta">{pendingApprovals.length} waiting</div>
            </div>
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
                    <div className="cockpit-row-meta">{approval.risk_level} risk</div>
                  </button>
                  <div className="cockpit-feedback-row">
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
        </aside>
        )}

        <main className={`cockpit-center ${activeLayout.centerSingleColumn ? "cockpit-center--single" : ""}`}>
          {activeLayout.sections.guardianState && (
          <section className="cockpit-panel">
            <div className="cockpit-card-header">
              <div className="cockpit-card-title">Guardian state</div>
              <div className="cockpit-card-meta">
                {observerState?.time_of_day ?? "pending"} · {observerState?.day_of_week ?? "today"}
              </div>
            </div>
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
          )}

          {activeLayout.sections.workflows && (
          <section className="cockpit-panel">
            <div className="cockpit-card-header">
              <div className="cockpit-card-title">Workflow runs</div>
              <div className="cockpit-card-meta">{workflowRuns.length} recent</div>
            </div>
            <div className="cockpit-list">
              {workflowRuns.map((workflow) => {
                const approval = approvalForWorkflow(workflow);
                const linkedInterventions = interventionsForWorkflow(workflow);
                return (
                  <button
                    key={workflow.id}
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
                      {approval ? "approval waiting" : "no approval"} · {linkedInterventions.length} interventions
                    </div>
                  </button>
                );
              })}
              {workflowRuns.length === 0 && (
                <div className="cockpit-empty">No recent workflow executions in the current audit window.</div>
              )}
            </div>
          </section>
          )}

          {activeLayout.sections.interventions && (
          <section className="cockpit-panel">
            <div className="cockpit-card-header">
              <div className="cockpit-card-title">Interventions</div>
              <div className="cockpit-card-meta">{recentInterventions.length} recent</div>
            </div>
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
                    </div>
                  </button>
                  {message.id && (
                    <div className="cockpit-feedback-row">
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
          )}

          {activeLayout.sections.audit && (
          <section className="cockpit-panel">
            <div className="cockpit-card-header">
              <div className="cockpit-card-title">Audit surface</div>
              <div className="cockpit-card-meta">{auditEvents.length} events</div>
            </div>
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
          )}

          {activeLayout.sections.trace && (
          <section className="cockpit-panel">
            <div className="cockpit-card-header">
              <div className="cockpit-card-title">Live trace</div>
              <div className="cockpit-card-meta">{isAgentBusy ? "agent active" : "idle"}</div>
            </div>
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
          )}

          {activeLayout.sections.inspector && inspectorVisible && (
          <section
            className={`cockpit-panel ${activeLayout.centerSingleColumn ? "" : "cockpit-panel--span-2"}`}
          >
            <div className="cockpit-card-header">
              <div className="cockpit-card-title">Operations inspector</div>
              <div className="cockpit-card-meta">
                {selectedInspector ? selectedInspector.kind : "nothing selected"}
              </div>
            </div>
            <div className="cockpit-feed">{renderInspector()}</div>
          </section>
          )}
        </main>

        {activeLayout.sections.conversation && (
        <aside className="cockpit-chatrail">
          <section className="cockpit-panel cockpit-chat-panel">
            <div className="cockpit-card-header">
              <div className="cockpit-card-title">Conversation</div>
              <div className="cockpit-card-meta">
                {activeSession?.title ?? "new session"}
              </div>
            </div>
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
              {recentConversation.length === 0 && (
                <div className="cockpit-empty">No conversation yet. Use the command bar below.</div>
              )}
            </div>
          </section>

          <section className="cockpit-panel">
            <div className="cockpit-card-header">
              <div className="cockpit-card-title">Desktop shell</div>
              <div className="cockpit-card-meta">
                {daemonPresence?.connected ? "linked" : "offline"} · {desktopNotifications.length} alerts
              </div>
            </div>
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
                  <div className="cockpit-feedback-row">
                    <button
                      className="cockpit-feedback-button"
                      onClick={() => queueComposerDraft(`Follow up on this desktop alert: ${notification.body}`)}
                    >
                      Draft Follow-up
                    </button>
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
                  <div className="cockpit-feedback-row">
                    <button
                      className="cockpit-feedback-button"
                      onClick={() => queueComposerDraft(`Follow up on this deferred guardian item: ${item.content_excerpt}`)}
                    >
                      Draft Follow-up
                    </button>
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
                      onClick={() => queueComposerDraft(`Continue from this guardian intervention: ${item.content_excerpt}`)}
                    >
                      Continue
                    </button>
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
        </aside>
        )}
      </div>

      <form className="cockpit-composer" onSubmit={handleSubmit}>
        <div className="cockpit-composer-meta">
          <span>command bar</span>
          <span>
            {activeLayout.label} · Shift+1/2/3 layouts · Shift+I inspector · Shift+C composer
          </span>
          <span>{isAgentBusy ? "agent busy" : "ready"}</span>
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
                : "Waiting for WebSocket connection..."
            }
            className="cockpit-input"
            disabled={connectionStatus !== "connected" || isAgentBusy}
          />
          <button type="submit" className="cockpit-send" disabled={submitDisabled}>
            Send
          </button>
        </div>
      </form>
    </div>
  );
}
