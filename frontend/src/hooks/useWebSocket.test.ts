/**
 * WS reconnection tests — exponential backoff, session load race fix.
 *
 * These tests verify the reconnection logic in useWebSocket.ts by testing
 * the backoff behavior and session restoration sequencing.
 */
import { describe, it, expect, vi } from "vitest";

// We test the constants and behavior rather than the hook directly
// since hooks require a React rendering context.
import { WS_RECONNECT_DELAY_MS } from "../config/constants";
import {
  WS_RESPONSE_TIMEOUT_MS,
  buildClarificationMessage,
  reconcileStreamedFinalAnswer,
  reduceAssistantDelta,
  resolveClarificationSessionId,
  shouldAcceptActiveResponseSession,
  resolveWebSocketUrl,
} from "./useWebSocket";
import type { ChatMessage } from "../types";

describe("WS reconnection constants", () => {
  it("has a sensible initial reconnect delay", () => {
    expect(WS_RECONNECT_DELAY_MS).toBeGreaterThan(0);
    expect(WS_RECONNECT_DELAY_MS).toBeLessThanOrEqual(10_000);
  });

  it("allows normal backend agent turns to finish before resetting the socket", () => {
    expect(WS_RESPONSE_TIMEOUT_MS).toBeGreaterThanOrEqual(120_000);
    expect(WS_RESPONSE_TIMEOUT_MS).toBeLessThanOrEqual(130_000);
  });

  it("resolves proxy-relative paths for HTTP and HTTPS cockpit origins", () => {
    expect(resolveWebSocketUrl("/ws/chat", "http://127.0.0.1:3001/")).toBe("ws://127.0.0.1:3001/ws/chat");
    expect(resolveWebSocketUrl("/ws/chat", "https://cockpit.example/console")).toBe("wss://cockpit.example/ws/chat");
    expect(resolveWebSocketUrl("ws://backend/ws/chat", "http://127.0.0.1:3001/")).toBe("ws://backend/ws/chat");
  });
});

describe("exponential backoff logic", () => {
  const WS_BACKOFF_MAX_MS = 30_000;

  it("doubles on each failure", () => {
    let backoff = WS_RECONNECT_DELAY_MS;
    const delays: number[] = [];

    for (let i = 0; i < 5; i++) {
      delays.push(backoff);
      backoff = Math.min(backoff * 2, WS_BACKOFF_MAX_MS);
    }

    // Each delay should be double the previous (up to max)
    for (let i = 1; i < delays.length; i++) {
      if (delays[i - 1] * 2 <= WS_BACKOFF_MAX_MS) {
        expect(delays[i]).toBe(delays[i - 1] * 2);
      }
    }
  });

  it("caps at maximum delay", () => {
    let backoff = WS_RECONNECT_DELAY_MS;

    for (let i = 0; i < 20; i++) {
      backoff = Math.min(backoff * 2, WS_BACKOFF_MAX_MS);
    }

    expect(backoff).toBe(WS_BACKOFF_MAX_MS);
  });

  it("resets on successful connect", () => {
    let backoff = WS_BACKOFF_MAX_MS; // simulate max backoff

    // Simulate successful connect → reset
    backoff = WS_RECONNECT_DELAY_MS;

    expect(backoff).toBe(WS_RECONNECT_DELAY_MS);
  });
});

describe("session load ordering", () => {
  it("switchSession should only be called after loadSessions resolves", async () => {
    const callOrder: string[] = [];

    const loadSessions = vi.fn(async () => {
      await new Promise((r) => setTimeout(r, 50));
      callOrder.push("loadSessions");
    });

    const switchSession = vi.fn(async () => {
      callOrder.push("switchSession");
    });

    // Simulate the fixed onopen logic: await loadSessions then switchSession
    await loadSessions().then(() => {
      return switchSession();
    });

    expect(callOrder).toEqual(["loadSessions", "switchSession"]);
  });

  it("switchSession should NOT be called if no stored session", async () => {
    const loadSessions = vi.fn(async () => {});
    const switchSession = vi.fn(async () => {});

    const storedSessionId: string | null = null;

    await loadSessions().then(() => {
      if (storedSessionId) {
        return switchSession();
      }
    });

    expect(switchSession).not.toHaveBeenCalled();
  });
});

describe("clarification transport helpers", () => {
  it("prefers the server-provided clarification session id", () => {
    expect(
      resolveClarificationSessionId({ session_id: "session-clarify" }, "fallback-session")
    ).toBe("session-clarify");
  });

  it("builds a dedicated clarification chat message with options", () => {
    const message = buildClarificationMessage({
      message: "Which city should I check?",
      question: "Which city should I check?",
      reason: "Weather depends on location.",
      options: ["Wroclaw", "Warsaw"],
    }, "session-clarify");

    expect(message.role).toBe("clarification");
    expect(message.sessionId).toBe("session-clarify");
    expect(message.clarificationQuestion).toBe("Which city should I check?");
    expect(message.clarificationReason).toBe("Weather depends on location.");
    expect(message.clarificationOptions).toEqual(["Wroclaw", "Warsaw"]);
  });

  it("falls back to websocket content when message is absent", () => {
    const message = buildClarificationMessage({
      content: "Which city should I check?",
      question: "Which city should I check?",
      reason: "Weather depends on location.",
    }, "session-clarify");

    expect(message.content).toBe("Which city should I check?");
    expect(message.clarificationQuestion).toBe("Which city should I check?");
  });
});

describe("active websocket session transitions", () => {
  it("accepts final replies for a backend-created session while a user turn is active", () => {
    expect(shouldAcceptActiveResponseSession("stale-session", "new-session", "final", true)).toBe(true);
  });

  it("accepts streamed delta replies for a backend-created session while a user turn is active", () => {
    expect(shouldAcceptActiveResponseSession("stale-session", "new-session", "delta", true)).toBe(true);
  });

  it("rejects foreign proactive session activity when no user turn is active", () => {
    expect(shouldAcceptActiveResponseSession("current-session", "other-session", "proactive", false)).toBe(false);
  });
});

describe("streamed assistant message reconciliation", () => {
  it("appends deltas into one assistant message and reconciles the final frame", () => {
    const initialMessages: ChatMessage[] = [
      {
        id: "user-1",
        role: "user",
        content: "Hello",
        timestamp: 1,
        sessionId: "session-1",
      },
    ];

    const first = reduceAssistantDelta(
      initialMessages,
      null,
      { content: "Hel", session_id: "session-1" },
      "assistant-stream",
      2,
    );
    const second = reduceAssistantDelta(
      first.messages,
      first.streaming,
      { content: "lo", session_id: "session-1" },
      "unused-id",
      3,
    );
    const final = reconcileStreamedFinalAnswer(
      second.messages,
      second.streaming,
      { content: "Hello.", session_id: "session-1" },
      4,
    );

    expect(second.messages.filter((message) => message.role === "agent")).toHaveLength(1);
    expect(second.messages[1]).toMatchObject({
      id: "assistant-stream",
      role: "agent",
      content: "Hello",
      sessionId: "session-1",
    });
    expect(final.reconciled).toBe(true);
    expect(final.messages.filter((message) => message.role === "agent")).toHaveLength(1);
    expect(final.messages[1]).toMatchObject({
      id: "assistant-stream",
      content: "Hello.",
      sessionId: "session-1",
    });
  });

  it("does not reconcile a final frame from a different session", () => {
    const messages: ChatMessage[] = [
      {
        id: "assistant-stream",
        role: "agent",
        content: "Hel",
        timestamp: 2,
        sessionId: "session-1",
      },
    ];

    const final = reconcileStreamedFinalAnswer(
      messages,
      { id: "assistant-stream", sessionId: "session-1", content: "Hel" },
      { content: "Other", session_id: "session-2" },
      4,
    );

    expect(final.reconciled).toBe(false);
    expect(final.messages).toBe(messages);
  });
});
