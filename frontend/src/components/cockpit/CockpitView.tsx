import { useEffect, useRef, useState, type FormEvent } from "react";
import { API_URL } from "../../config/constants";
import { useChatStore } from "../../stores/chatStore";
import { useQuestStore } from "../../stores/questStore";
import type { ChatMessage, GoalInfo } from "../../types";

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

interface AuditEvent {
  id: string;
  event_type: string;
  tool_name: string | null;
  summary: string;
  created_at: string;
  risk_level: string;
  policy_mode: string;
}

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

export function CockpitView({ onSend, onSkipOnboarding }: CockpitViewProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [composer, setComposer] = useState("");
  const [observerState, setObserverState] = useState<ObserverState | null>(null);
  const [auditEvents, setAuditEvents] = useState<AuditEvent[]>([]);
  const [feedbackState, setFeedbackState] = useState<Record<string, string>>({});

  const messages = useChatStore((s) => s.messages);
  const sessions = useChatStore((s) => s.sessions);
  const sessionId = useChatStore((s) => s.sessionId);
  const connectionStatus = useChatStore((s) => s.connectionStatus);
  const isAgentBusy = useChatStore((s) => s.isAgentBusy);
  const ambientState = useChatStore((s) => s.ambientState);
  const ambientTooltip = useChatStore((s) => s.ambientTooltip);
  const onboardingCompleted = useChatStore((s) => s.onboardingCompleted);
  const toolRegistry = useChatStore((s) => s.toolRegistry);
  const loadSessions = useChatStore((s) => s.loadSessions);
  const switchSession = useChatStore((s) => s.switchSession);
  const newSession = useChatStore((s) => s.newSession);
  const fetchToolRegistry = useChatStore((s) => s.fetchToolRegistry);
  const setQuestPanelOpen = useChatStore((s) => s.setQuestPanelOpen);
  const setSettingsPanelOpen = useChatStore((s) => s.setSettingsPanelOpen);
  const setInterfaceMode = useChatStore((s) => s.setInterfaceMode);

  const dashboard = useQuestStore((s) => s.dashboard);
  const goalTree = useQuestStore((s) => s.goalTree);
  const loadingGoals = useQuestStore((s) => s.loading);
  const refreshGoals = useQuestStore((s) => s.refresh);

  useEffect(() => {
    loadSessions();
    fetchToolRegistry();
    refreshGoals();
  }, [loadSessions, fetchToolRegistry, refreshGoals]);

  useEffect(() => {
    let cancelled = false;

    const refreshCockpit = async () => {
      try {
        const [observerResponse, auditResponse] = await Promise.all([
          fetch(`${API_URL}/api/observer/state`),
          fetch(`${API_URL}/api/audit/events?limit=10`),
        ]);

        if (!cancelled && observerResponse.ok) {
          const observerPayload = await observerResponse.json();
          setObserverState(observerPayload);
        }

        if (!cancelled && auditResponse.ok) {
          const auditPayload = await auditResponse.json();
          setAuditEvents(Array.isArray(auditPayload) ? auditPayload : []);
        }
      } catch {
        if (!cancelled) {
          setAuditEvents([]);
        }
      }
    };

    refreshCockpit();
    const interval = window.setInterval(refreshCockpit, 12_000);
    return () => {
      cancelled = true;
      window.clearInterval(interval);
    };
  }, []);

  useEffect(() => {
    const focusComposer = () => inputRef.current?.focus();
    window.addEventListener("seraph-cockpit-focus-composer", focusComposer as EventListener);
    return () => {
      window.removeEventListener("seraph-cockpit-focus-composer", focusComposer as EventListener);
    };
  }, []);

  const activeSession = sessions.find((item) => item.id === sessionId) ?? null;
  const recentConversation = messages.slice(-18);
  const recentTrace = messages
    .filter((message) => message.role === "step" || message.role === "error")
    .slice(-8)
    .reverse();
  const recentInterventions = messages
    .filter((message) => message.role === "proactive" || message.role === "approval")
    .slice(-6)
    .reverse();
  const workflowTools = toolRegistry
    .filter((tool) => tool.name.startsWith("workflow_"))
    .slice(0, 4);
  const topGoals = collectGoalTitles(goalTree, 5);
  const connectionLabel = connectionStatus === "connected" ? "live" : connectionStatus;
  const submitDisabled = connectionStatus !== "connected" || isAgentBusy || !composer.trim();

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

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const message = composer.trim();
    if (!message || submitDisabled) return;
    onSend(message);
    setComposer("");
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
        </div>
      </header>

      <div className="cockpit-grid">
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
              <div className="cockpit-card-title">Capabilities</div>
              <div className="cockpit-card-meta">{toolRegistry.length} tools</div>
            </div>
            <div className="cockpit-metric-grid">
              <div className="cockpit-metric">
                <div className="cockpit-metric-value">{workflowTools.length}</div>
                <div className="cockpit-metric-label">workflows</div>
              </div>
              <div className="cockpit-metric">
                <div className="cockpit-metric-value">{recentInterventions.length}</div>
                <div className="cockpit-metric-label">interventions</div>
              </div>
              <div className="cockpit-metric">
                <div className="cockpit-metric-value">{auditEvents.length}</div>
                <div className="cockpit-metric-label">recent audits</div>
              </div>
            </div>
            <div className="cockpit-sublist">
              {workflowTools.map((tool) => (
                <div key={tool.name} className="cockpit-sublist-item">
                  {tool.name.replace("workflow_", "").replace(/_/g, " ")}
                </div>
              ))}
              {workflowTools.length === 0 && (
                <div className="cockpit-empty">No workflow tools loaded.</div>
              )}
            </div>
          </section>
        </aside>

        <main className="cockpit-center">
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

          <section className="cockpit-panel">
            <div className="cockpit-card-header">
              <div className="cockpit-card-title">Interventions</div>
              <div className="cockpit-card-meta">
                {recentInterventions.length} recent
              </div>
            </div>
            <div className="cockpit-list">
              {recentInterventions.map((message) => (
                <div key={message.id} className="cockpit-row">
                  <div className="cockpit-row-header">
                    <span className="cockpit-role">{labelForRole(message)}</span>
                    <span className="cockpit-row-age">{formatAge(message.timestamp)}</span>
                  </div>
                  <div className="cockpit-row-body">{message.content}</div>
                  {message.interventionId && (
                    <div className="cockpit-feedback-row">
                      <button
                        className="cockpit-feedback-button"
                        onClick={() => sendFeedback(message.interventionId!, "helpful")}
                      >
                        Helpful
                      </button>
                      <button
                        className="cockpit-feedback-button"
                        onClick={() => sendFeedback(message.interventionId!, "not_helpful")}
                      >
                        Not helpful
                      </button>
                      <span className="cockpit-feedback-status">
                        {feedbackState[message.interventionId] ?? "unrated"}
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

          <section className="cockpit-panel">
            <div className="cockpit-card-header">
              <div className="cockpit-card-title">Audit surface</div>
              <div className="cockpit-card-meta">{auditEvents.length} events</div>
            </div>
            <div className="cockpit-list">
              {auditEvents.map((event) => (
                <div key={event.id} className="cockpit-row">
                  <div className="cockpit-row-header">
                    <span className="cockpit-role">{event.tool_name ?? event.event_type}</span>
                    <span className="cockpit-row-age">{formatAge(event.created_at)}</span>
                  </div>
                  <div className="cockpit-row-body">{event.summary}</div>
                  <div className="cockpit-row-meta">
                    {event.event_type} · {event.risk_level} · {event.policy_mode}
                  </div>
                </div>
              ))}
              {auditEvents.length === 0 && (
                <div className="cockpit-empty">No audit events available.</div>
              )}
            </div>
          </section>

          <section className="cockpit-panel">
            <div className="cockpit-card-header">
              <div className="cockpit-card-title">Live trace</div>
              <div className="cockpit-card-meta">
                {isAgentBusy ? "agent active" : "idle"}
              </div>
            </div>
            <div className="cockpit-list">
              {recentTrace.map((message) => (
                <div key={message.id} className="cockpit-row">
                  <div className="cockpit-row-header">
                    <span className="cockpit-role">{labelForRole(message)}</span>
                    <span className="cockpit-row-age">{formatAge(message.timestamp)}</span>
                  </div>
                  <div className="cockpit-row-body">{message.content}</div>
                </div>
              ))}
              {recentTrace.length === 0 && (
                <div className="cockpit-empty">No live tool or error trace yet.</div>
              )}
            </div>
          </section>
        </main>

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
        </aside>
      </div>

      <form className="cockpit-composer" onSubmit={handleSubmit}>
        <div className="cockpit-composer-meta">
          <span>command bar</span>
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
