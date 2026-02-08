import { useEffect, useRef, useCallback } from "react";
import { WS_URL, WS_RECONNECT_DELAY_MS, WS_PING_INTERVAL_MS } from "../config/constants";
import { useChatStore } from "../stores/chatStore";
import { detectToolFromStep } from "../lib/toolParser";
import { useAgentAnimation } from "./useAgentAnimation";
import type { WSResponse, ChatMessage } from "../types";

function makeId(): string {
  return Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
}

export function useWebSocket() {
  const wsRef = useRef<WebSocket | null>(null);
  const pingRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const reconnectRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const {
    addMessage,
    setSessionId,
    setConnectionStatus,
    setAgentBusy,
  } = useChatStore();

  const { onToolDetected, onFinalAnswer, onThinking } = useAgentAnimation();

  // Store animation callbacks in refs so `connect` doesn't depend on them
  const onToolDetectedRef = useRef(onToolDetected);
  const onFinalAnswerRef = useRef(onFinalAnswer);
  onToolDetectedRef.current = onToolDetected;
  onFinalAnswerRef.current = onFinalAnswer;

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    setConnectionStatus("connecting");
    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;

    ws.onopen = () => {
      setConnectionStatus("connected");
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
          if (!current) setSessionId(data.session_id);
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
          };
          addMessage(agentMsg);
        } else if (data.type === "error") {
          setAgentBusy(false);

          const errorMsg: ChatMessage = {
            id: makeId(),
            role: "error",
            content: data.content,
            timestamp: Date.now(),
          };
          addMessage(errorMsg);
        }
      } catch {
        // ignore malformed messages
      }
    };

    ws.onclose = () => {
      setConnectionStatus("disconnected");
      if (pingRef.current) clearInterval(pingRef.current);
      reconnectRef.current = setTimeout(connect, WS_RECONNECT_DELAY_MS);
    };

    ws.onerror = () => {
      setConnectionStatus("error");
      ws.close();
    };
  }, [addMessage, setSessionId, setConnectionStatus, setAgentBusy]);

  const sendMessage = useCallback(
    (message: string) => {
      if (!wsRef.current || wsRef.current.readyState !== WebSocket.OPEN) return;

      setAgentBusy(true);
      onThinking();

      const userMsg: ChatMessage = {
        id: makeId(),
        role: "user",
        content: message,
        timestamp: Date.now(),
      };
      addMessage(userMsg);

      const sessionId = useChatStore.getState().sessionId;
      wsRef.current.send(
        JSON.stringify({
          type: "message",
          message,
          session_id: sessionId,
        })
      );
    },
    [addMessage, setAgentBusy, onThinking]
  );

  useEffect(() => {
    connect();
    return () => {
      if (pingRef.current) clearInterval(pingRef.current);
      if (reconnectRef.current) clearTimeout(reconnectRef.current);
      wsRef.current?.close();
    };
  }, [connect]);

  return { sendMessage };
}
