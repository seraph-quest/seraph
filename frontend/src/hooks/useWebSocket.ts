import { useEffect, useRef, useCallback } from "react";
import { API_URL, WS_URL, WS_RECONNECT_DELAY_MS, WS_PING_INTERVAL_MS } from "../config/constants";
import { useChatStore } from "../stores/chatStore";
import { detectToolFromStep } from "../lib/toolParser";
import { useAgentAnimation } from "./useAgentAnimation";
import { appEventBus } from "../lib/appEventBus";
import type { WSResponse, ChatMessage } from "../types";

function makeId(): string {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
}

const WS_BACKOFF_MAX_MS = 30_000;

type ClarificationPayload = {
  content?: string;
  message?: string;
  question?: string;
  reason?: string;
  options?: string[];
};

export function resolveClarificationSessionId(
  detail: Record<string, unknown> | null | undefined,
  fallbackSessionId: string | null,
): string | null {
  return typeof detail?.session_id === "string" ? detail.session_id : fallbackSessionId;
}

export function buildClarificationMessage(
  detail: ClarificationPayload,
  sessionId: string | null,
): ChatMessage {
  return {
    id: makeId(),
    role: "clarification",
    content: detail.message ?? detail.content ?? "I need one more detail before I can continue.",
    timestamp: Date.now(),
    sessionId,
    clarificationQuestion: detail.question,
    clarificationReason: detail.reason,
    clarificationOptions: Array.isArray(detail.options) ? detail.options : undefined,
  };
}

export function useWebSocket() {
  const wsRef = useRef<WebSocket | null>(null);
  const pingRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const reconnectRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const backoffRef = useRef(WS_RECONNECT_DELAY_MS);
  const pendingResumeRef = useRef<{ sessionId: string | null; message: string } | null>(null);

  const {
    addMessage,
    setSessionId,
    setConnectionStatus,
    setAgentBusy,
    setAmbientState,
    setChatPanelOpen,
    markSessionContinuity,
  } = useChatStore();

  const { onToolDetected, onFinalAnswer, onThinking } = useAgentAnimation();

  const onToolDetectedRef = useRef(onToolDetected);
  const onFinalAnswerRef = useRef(onFinalAnswer);
  onToolDetectedRef.current = onToolDetected;
  onFinalAnswerRef.current = onFinalAnswer;

  const buildUserMessage = useCallback((message: string): ChatMessage => ({
    id: makeId(),
    role: "user",
    content: message,
    timestamp: Date.now(),
    sessionId: useChatStore.getState().sessionId,
  }), []);

  const buildErrorMessage = useCallback((message: string): ChatMessage => ({
    id: makeId(),
    role: "error",
    content: message,
    timestamp: Date.now(),
    sessionId: useChatStore.getState().sessionId,
  }), []);

  const sendSocketMessage = useCallback(
    (
      message: string,
      sessionId: string | null,
      echoUser: boolean,
      messageType: "message" | "resume_message" = "message"
    ) => {
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return false;

      try {
        setAgentBusy(true);
        onThinking();

        if (echoUser) {
          addMessage(buildUserMessage(message));
        }

        wsRef.current.send(
          JSON.stringify({
            type: messageType,
            message,
            session_id: sessionId,
          })
        );
        return true;
      } catch {
        setAgentBusy(false);
        addMessage(buildErrorMessage("Message delivery failed."));
        return false;
      }
    },
    [addMessage, buildErrorMessage, buildUserMessage, setAgentBusy, onThinking]
  );

  const sendRestMessage = useCallback(async (message: string, sessionId: string | null) => {
    setAgentBusy(true);
    onThinking();
    addMessage(buildUserMessage(message));

    try {
      const response = await fetch(`${API_URL}/api/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message,
          session_id: sessionId,
        }),
      });

      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        const detail = payload?.detail ?? payload;

        if (response.status === 409 && detail?.type === "approval_required") {
          const approvalMsg: ChatMessage = {
            id: makeId(),
            role: "approval",
            content: detail.message ?? "Approval required before continuing.",
            timestamp: Date.now(),
            sessionId,
            approvalId: detail.approval_id,
            toolUsed: detail.tool_name,
            riskLevel: detail.risk_level,
            approvalStatus: "pending",
          };
          addMessage(approvalMsg);
          return false;
        }

        if (response.status === 409 && detail?.type === "clarification_required") {
          const nextSessionId = resolveClarificationSessionId(detail, sessionId);
          if (nextSessionId) {
            setSessionId(nextSessionId);
          }
          addMessage(buildClarificationMessage(detail, nextSessionId));
          await useChatStore.getState().loadSessions();
          return false;
        }

        const messageText =
          (typeof detail === "string" && detail)
          || detail?.message
          || "Message delivery failed.";
        addMessage(buildErrorMessage(messageText));
        return false;
      }

      const nextSessionId = typeof payload?.session_id === "string" ? payload.session_id : sessionId;
      if (nextSessionId) {
        setSessionId(nextSessionId);
      }

      const responseText = typeof payload?.response === "string" ? payload.response : "";
      onFinalAnswerRef.current(responseText);
      addMessage({
        id: makeId(),
        role: "agent",
        content: responseText,
        timestamp: Date.now(),
        sessionId: nextSessionId,
      });

      await useChatStore.getState().fetchProfile();
      await useChatStore.getState().loadSessions();
      if (nextSessionId) {
        const updated = useChatStore.getState();
        const session = updated.sessions.find((item) => item.id === nextSessionId);
        if (session && session.title === "New Conversation") {
          await updated.generateSessionTitle(nextSessionId);
        }
      }

      return true;
    } catch {
      addMessage(buildErrorMessage("Message delivery failed."));
      return false;
    } finally {
      setAgentBusy(false);
    }
  }, [addMessage, buildErrorMessage, buildUserMessage, onThinking, setAgentBusy, setSessionId]);

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    setConnectionStatus("connecting");
    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;

    ws.onopen = () => {
      setConnectionStatus("connected");
      backoffRef.current = WS_RECONNECT_DELAY_MS; // reset on successful connect
      useChatStore.getState().fetchProfile();
      useChatStore.getState().fetchToolRegistry();

      void useChatStore.getState().restoreLastSession();

      if (pendingResumeRef.current) {
        const pending = pendingResumeRef.current;
        pendingResumeRef.current = null;
        sendSocketMessage(pending.message, pending.sessionId, false, "resume_message");
      }

      pingRef.current = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: "ping" }));
        }
      }, WS_PING_INTERVAL_MS);
    };

    ws.onmessage = (event) => {
      try {
        const data: WSResponse = JSON.parse(event.data);

        if (data.type === "pong") return;

        if (data.session_id) {
          const current = useChatStore.getState().sessionId;
          if (!current) {
            setSessionId(data.session_id);
          } else if (current !== data.session_id) {
            markSessionContinuity(data.session_id, "new_activity");
            void useChatStore.getState().loadSessions();
            return;
          }
        }

        if (data.type === "step") {
          const tool = detectToolFromStep(data.content);
          if (tool) {
            onToolDetectedRef.current(tool, data.content);
          }

          const stepMsg: ChatMessage = {
            id: makeId(),
            role: "step",
            content: data.content,
            timestamp: Date.now(),
            sessionId: data.session_id,
            stepNumber: data.step ?? undefined,
            toolUsed: tool ?? undefined,
          };
          addMessage(stepMsg);
        } else if (data.type === "final") {
          setAgentBusy(false);
          onFinalAnswerRef.current(data.content);

          const agentMsg: ChatMessage = {
            id: makeId(),
            role: "agent",
            content: data.content,
            timestamp: Date.now(),
            sessionId: data.session_id,
          };
          addMessage(agentMsg);

          // Refresh session list and profile after a conversation turn
          useChatStore.getState().fetchProfile();
          useChatStore.getState().loadSessions().then(() => {
            // Auto-name session if still "New Conversation"
            if (data.session_id) {
              const updated = useChatStore.getState();
              const session = updated.sessions.find((s) => s.id === data.session_id);
              if (session && session.title === "New Conversation") {
                updated.generateSessionTitle(data.session_id);
              }
            }
          });
        } else if (data.type === "error") {
          setAgentBusy(false);

          const errorMsg: ChatMessage = {
            id: makeId(),
            role: "error",
            content: data.content,
            timestamp: Date.now(),
            sessionId: data.session_id,
          };
          addMessage(errorMsg);
        } else if (data.type === "approval_required") {
          setAgentBusy(false);

          const approvalMsg: ChatMessage = {
            id: makeId(),
            role: "approval",
            content: data.content,
            timestamp: Date.now(),
            sessionId: data.session_id,
            approvalId: data.approval_id,
            toolUsed: data.tool_name,
            riskLevel: data.risk_level,
            approvalStatus: "pending",
          };
          addMessage(approvalMsg);
        } else if (data.type === "clarification_required") {
          setAgentBusy(false);

          addMessage(buildClarificationMessage(data, data.session_id));
        } else if (data.type === "proactive") {
          // Phase 3: Proactive messages from Seraph
          const proactiveMsg: ChatMessage = {
            id: makeId(),
            role: "proactive",
            content: data.content,
            timestamp: Date.now(),
            sessionId: data.session_id,
            interventionId: data.intervention_id,
            urgency: data.urgency ?? undefined,
            interventionType: data.intervention_type ?? undefined,
          };

          if (data.intervention_type === "alert" || data.intervention_type === "advisory") {
            setChatPanelOpen(true);
            addMessage(proactiveMsg);
          } else if (data.intervention_type === "nudge") {
            addMessage(proactiveMsg);
          } else {
            addMessage(proactiveMsg);
          }
        } else if (data.type === "ambient") {
          if (data.state) {
            setAmbientState(data.state as "idle" | "has_insight" | "goal_behind" | "on_track" | "waiting");
            useChatStore.getState().setAmbientTooltip(data.tooltip ?? "");
          }
        }
      } catch (err) {
        console.warn("Failed to parse WebSocket message:", err);
      }
    };

    ws.onclose = () => {
      setAgentBusy(false);
      setConnectionStatus("disconnected");
      if (pingRef.current) clearInterval(pingRef.current);
      reconnectRef.current = setTimeout(connect, backoffRef.current);
      backoffRef.current = Math.min(backoffRef.current * 2, WS_BACKOFF_MAX_MS);
    };

    ws.onerror = () => {
      setAgentBusy(false);
      setConnectionStatus("error");
      ws.close();
    };
  }, [addMessage, markSessionContinuity, setSessionId, setConnectionStatus, setAgentBusy, setAmbientState, setChatPanelOpen]);

  const skipOnboarding = useCallback(() => {
    if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;
    wsRef.current.send(JSON.stringify({ type: "skip_onboarding" }));
    useChatStore.getState().setOnboardingCompleted(true);
  }, []);

  const sendMessage = useCallback(
    async (message: string) => {
      const sessionId = useChatStore.getState().sessionId;
      if (sendSocketMessage(message, sessionId, true)) {
        return true;
      }

      return sendRestMessage(message, sessionId);
    },
    [sendRestMessage, sendSocketMessage]
  );

  useEffect(() => {
    const handleApprovalResume = (payload: { sessionId?: string | null; message?: string }) => {
      if (!payload?.message) return;
      const fallbackSessionId = useChatStore.getState().sessionId;
      const ok = sendSocketMessage(
        payload.message,
        payload.sessionId ?? fallbackSessionId,
        false,
        "resume_message"
      );
      if (!ok) {
        pendingResumeRef.current = {
          sessionId: payload.sessionId ?? fallbackSessionId,
          message: payload.message,
        };
      }
    };

    appEventBus.on("approval-resume", handleApprovalResume);
    connect();
    return () => {
      appEventBus.off("approval-resume", handleApprovalResume);
      if (pingRef.current) clearInterval(pingRef.current);
      if (reconnectRef.current) clearTimeout(reconnectRef.current);
      wsRef.current?.close();
    };
  }, [connect, sendSocketMessage]);

  return { sendMessage, skipOnboarding };
}
