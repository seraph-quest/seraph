import { useEffect, useRef, type FormEvent, type ReactNode, type RefObject } from "react";

import type { ChatMessage } from "../../types";

export interface ConversationActivity {
  label: string;
  detail: string;
  tone: "neutral" | "active" | "warning" | "error" | "success" | "muted";
}

export interface CockpitConversationDockProps {
  open: boolean;
  onToggle: () => void;
  sessionTitle: string;
  sessions: Array<{ id: string; title: string }>;
  currentSessionId: string | null;
  onSwitchSession: (sessionId: string) => void;
  onNewSession: () => void;
  messages: ChatMessage[];
  isAgentBusy: boolean;
  activity: ConversationActivity;
  roleLabel: (message: ChatMessage) => string;
  ageLabel: (timestamp: number) => string;
  /** Opens the approvals surface for an approval request in the thread. */
  onOpenApprovals?: (approvalId?: string) => void;
  /** Onboarding prompt/skip controls while onboarding is active. */
  onboarding?: ReactNode;
  /** Push-to-talk control. */
  voice?: ReactNode;
  composerValue: string;
  onComposerChange: (value: string) => void;
  onSubmit: (event: FormEvent<HTMLFormElement>) => void;
  inputRef: RefObject<HTMLInputElement | null>;
  placeholder: string;
  submitDisabled: boolean;
}

/**
 * Conversation with Seraph as a first-class, always-findable surface (#1172).
 * It replaces the thin bottom composer plus the hidden "Conversation" window:
 * the thread, what Seraph is doing now, voice and the composer live together
 * in a dock that every section keeps visible.
 */
export function CockpitConversationDock(props: CockpitConversationDockProps) {
  const {
    open, onToggle, sessionTitle, sessions, currentSessionId, onSwitchSession, onNewSession,
    messages, isAgentBusy, activity, roleLabel, ageLabel, onOpenApprovals, onboarding, voice,
    composerValue, onComposerChange, onSubmit, inputRef, placeholder, submitDisabled,
  } = props;
  const feedRef = useRef<HTMLDivElement | null>(null);
  const lastMessageId = messages[messages.length - 1]?.id;

  useEffect(() => {
    const feed = feedRef.current;
    if (!feed || !open) return;
    feed.scrollTop = feed.scrollHeight;
  }, [lastMessageId, isAgentBusy, open]);

  if (!open) {
    return (
      <aside className="cockpit-dock cockpit-dock--collapsed" aria-label="Conversation">
        <button
          type="button"
          className="cockpit-dock-reopen"
          onClick={onToggle}
          aria-label="Open conversation"
          title="Open conversation (Shift+C)"
        >
          <span className={`cockpit-status-dot cockpit-status-dot--${activity.tone === "error" ? "error" : activity.tone === "warning" ? "warning" : "ok"}`} aria-hidden="true" />
          <span className="cockpit-dock-reopen-label">Chat</span>
        </button>
      </aside>
    );
  }

  return (
    <aside className="cockpit-dock" aria-label="Conversation" data-testid="cockpit-conversation-dock">
      <header className="cockpit-dock-header">
        <div className="cockpit-dock-title-row">
          <h2 className="cockpit-dock-title">Conversation</h2>
          <div className="cockpit-dock-actions">
            <button type="button" className="cockpit-shell-button cockpit-shell-button--compact" onClick={onNewSession} title="Start a blank thread. Earlier threads stay in the list.">
              New thread
            </button>
            <button
              type="button"
              className="cockpit-shell-button cockpit-shell-button--compact cockpit-shell-button--icon"
              onClick={onToggle}
              aria-label="Collapse conversation"
              title="Collapse conversation"
            >
              <span aria-hidden="true">→</span>
            </button>
          </div>
        </div>
        <label className="cockpit-dock-thread">
          <span className="cockpit-visually-hidden">Thread</span>
          <select
            value={currentSessionId ?? ""}
            onChange={(event) => {
              if (event.target.value) onSwitchSession(event.target.value);
            }}
            aria-label="Thread"
          >
            {currentSessionId === null || !sessions.some((session) => session.id === currentSessionId) ? (
              <option value="">{sessionTitle}</option>
            ) : null}
            {sessions.slice(0, 20).map((session) => (
              <option key={session.id} value={session.id}>{session.title}</option>
            ))}
          </select>
        </label>
        <div className={`cockpit-dock-activity cockpit-dock-activity--${activity.tone}`} role="status" aria-live="polite" data-testid="cockpit-seraph-activity">
          <span className="cockpit-dock-activity-label">Seraph · {activity.label}</span>
          <span className="cockpit-dock-activity-detail">{activity.detail}</span>
        </div>
      </header>

      {onboarding}

      <div className="cockpit-dock-feed retro-scrollbar" ref={feedRef} role="log" aria-label="Messages" tabIndex={0}>
        {messages.map((message) => (
          <article key={message.id} className={`cockpit-dock-message cockpit-dock-message--${message.role}`}>
            <div className="cockpit-dock-message-meta">
              <span className="cockpit-dock-message-role">{message.role === "agent" ? "Seraph" : message.role === "user" ? "You" : roleLabel(message)}</span>
              <span className="cockpit-dock-message-age">{ageLabel(message.timestamp)}</span>
            </div>
            <div className="cockpit-dock-message-body">{message.content}</div>
            {message.role === "approval" && onOpenApprovals ? (
              <button
                type="button"
                className="cockpit-dock-message-action"
                onClick={() => onOpenApprovals(message.approvalId)}
              >
                Review approval
              </button>
            ) : null}
          </article>
        ))}
        {isAgentBusy ? (
          <div className="cockpit-dock-message cockpit-dock-message--pending" aria-live="polite">
            <div className="cockpit-dock-message-body">
              Seraph is responding
              <span className="cockpit-thinking-inline" aria-hidden="true">
                <span className="thinking-dot">.</span>
                <span className="thinking-dot">.</span>
                <span className="thinking-dot">.</span>
              </span>
            </div>
          </div>
        ) : null}
        {messages.length === 0 && !isAgentBusy ? (
          <div className="cockpit-dock-empty">
            <p>Ask Seraph anything: plan work, capture a goal, or check on a task.</p>
            <p>New threads are saved after the first reply.</p>
          </div>
        ) : null}
      </div>

      {voice ? (
        <details className="cockpit-dock-voice">
          <summary>Voice input</summary>
          <div className="cockpit-dock-voice-body">{voice}</div>
        </details>
      ) : null}
      <form className="cockpit-composer" onSubmit={onSubmit}>
        <div className="cockpit-composer-row">
          <label className="cockpit-visually-hidden" htmlFor="cockpit-composer-input">Message Seraph</label>
          <input
            id="cockpit-composer-input"
            ref={inputRef}
            type="text"
            value={composerValue}
            onChange={(event) => onComposerChange(event.target.value)}
            placeholder={placeholder}
            className="cockpit-input"
            disabled={isAgentBusy}
            autoComplete="off"
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
    </aside>
  );
}
