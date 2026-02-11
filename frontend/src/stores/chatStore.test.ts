import { describe, it, expect, beforeEach, vi } from "vitest";
import { useChatStore } from "./chatStore";

// Mock localStorage
const localStorageMock = (() => {
  let store: Record<string, string> = {};
  return {
    getItem: vi.fn((key: string) => store[key] ?? null),
    setItem: vi.fn((key: string, value: string) => { store[key] = value; }),
    removeItem: vi.fn((key: string) => { delete store[key]; }),
    clear: vi.fn(() => { store = {}; }),
  };
})();
Object.defineProperty(globalThis, "localStorage", { value: localStorageMock });

// Mock fetch
const mockFetch = vi.fn();
globalThis.fetch = mockFetch;

function resetStore() {
  useChatStore.setState({
    messages: [],
    sessionId: null,
    sessions: [],
    connectionStatus: "disconnected",
    isAgentBusy: false,
    agentVisual: { animationState: "idle", positionX: 50, facing: "right", speechText: null },
    ambientState: "idle",
    chatPanelOpen: true,
    chatMaximized: false,
    questPanelOpen: false,
    settingsPanelOpen: false,
    onboardingCompleted: null,
    toolRegistry: [],
  });
}

describe("chatStore sync actions", () => {
  beforeEach(() => {
    resetStore();
    vi.clearAllMocks();
    localStorageMock.clear();
  });

  it("addMessage appends to messages", () => {
    const msg = { id: "1", role: "user" as const, content: "hi", timestamp: Date.now() };
    useChatStore.getState().addMessage(msg);
    expect(useChatStore.getState().messages).toHaveLength(1);
    expect(useChatStore.getState().messages[0].content).toBe("hi");
  });

  it("setSessionId writes to localStorage", () => {
    useChatStore.getState().setSessionId("abc123");
    expect(useChatStore.getState().sessionId).toBe("abc123");
    expect(localStorageMock.setItem).toHaveBeenCalledWith("seraph_last_session_id", "abc123");
  });

  it("setAgentVisual merges partial state", () => {
    useChatStore.getState().setAgentVisual({ animationState: "walking", positionX: 30 });
    const visual = useChatStore.getState().agentVisual;
    expect(visual.animationState).toBe("walking");
    expect(visual.positionX).toBe(30);
    expect(visual.facing).toBe("right"); // preserved
  });

  it("resetAgentVisual restores defaults", () => {
    useChatStore.getState().setAgentVisual({ animationState: "walking", positionX: 10 });
    useChatStore.getState().resetAgentVisual();
    const visual = useChatStore.getState().agentVisual;
    expect(visual.animationState).toBe("idle");
    expect(visual.positionX).toBe(50);
  });

  it("setChatPanelOpen closes quest panel", () => {
    useChatStore.setState({ questPanelOpen: true });
    useChatStore.getState().setChatPanelOpen(true);
    expect(useChatStore.getState().chatPanelOpen).toBe(true);
    expect(useChatStore.getState().questPanelOpen).toBe(false);
  });

  it("setQuestPanelOpen closes chat panel", () => {
    useChatStore.setState({ chatPanelOpen: true });
    useChatStore.getState().setQuestPanelOpen(true);
    expect(useChatStore.getState().questPanelOpen).toBe(true);
    expect(useChatStore.getState().chatPanelOpen).toBe(false);
  });

  it("toggleChatMaximized toggles state", () => {
    expect(useChatStore.getState().chatMaximized).toBe(false);
    useChatStore.getState().toggleChatMaximized();
    expect(useChatStore.getState().chatMaximized).toBe(true);
    useChatStore.getState().toggleChatMaximized();
    expect(useChatStore.getState().chatMaximized).toBe(false);
  });

  it("newSession clears state and localStorage", () => {
    useChatStore.setState({ sessionId: "old", messages: [{ id: "1", role: "user", content: "hi", timestamp: 0 }] });
    useChatStore.getState().newSession();
    expect(useChatStore.getState().sessionId).toBeNull();
    expect(useChatStore.getState().messages).toHaveLength(0);
    expect(localStorageMock.removeItem).toHaveBeenCalledWith("seraph_last_session_id");
  });
});

describe("chatStore async actions", () => {
  beforeEach(() => {
    resetStore();
    vi.clearAllMocks();
    localStorageMock.clear();
  });

  it("fetchProfile sets onboardingCompleted", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ onboarding_completed: true }),
    });
    await useChatStore.getState().fetchProfile();
    expect(useChatStore.getState().onboardingCompleted).toBe(true);
  });

  it("skipOnboarding sets completed to true", async () => {
    mockFetch.mockResolvedValueOnce({ ok: true });
    await useChatStore.getState().skipOnboarding();
    expect(useChatStore.getState().onboardingCompleted).toBe(true);
  });

  it("restartOnboarding resets state", async () => {
    useChatStore.setState({ onboardingCompleted: true, sessionId: "s1" });
    mockFetch.mockResolvedValueOnce({ ok: true });
    await useChatStore.getState().restartOnboarding();
    expect(useChatStore.getState().onboardingCompleted).toBe(false);
    expect(useChatStore.getState().sessionId).toBeNull();
  });

  it("loadSessions populates sessions list", async () => {
    const sessions = [{ id: "s1", title: "Chat 1" }, { id: "s2", title: "Chat 2" }];
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => sessions,
    });
    await useChatStore.getState().loadSessions();
    expect(useChatStore.getState().sessions).toHaveLength(2);
  });

  it("switchSession loads messages and sets sessionId", async () => {
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => [
        { id: "m1", role: "user", content: "hello", created_at: "2024-01-01T00:00:00Z" },
        { id: "m2", role: "assistant", content: "hi", created_at: "2024-01-01T00:00:01Z" },
      ],
    });
    await useChatStore.getState().switchSession("s1");
    expect(useChatStore.getState().sessionId).toBe("s1");
    expect(useChatStore.getState().messages).toHaveLength(2);
    expect(useChatStore.getState().messages[1].role).toBe("agent"); // mapped from assistant
  });

  it("deleteSession removes from list and clears if current", async () => {
    useChatStore.setState({
      sessionId: "s1",
      sessions: [{ id: "s1", title: "A", created_at: "", updated_at: "", last_message: null, last_message_role: null }],
    });
    mockFetch.mockResolvedValueOnce({ ok: true });
    await useChatStore.getState().deleteSession("s1");
    expect(useChatStore.getState().sessions).toHaveLength(0);
    expect(useChatStore.getState().sessionId).toBeNull();
  });

  it("renameSession updates session title in list", async () => {
    useChatStore.setState({
      sessions: [{ id: "s1", title: "Old", created_at: "", updated_at: "", last_message: null, last_message_role: null }],
    });
    mockFetch.mockResolvedValueOnce({ ok: true });
    await useChatStore.getState().renameSession("s1", "New Title");
    expect(useChatStore.getState().sessions[0].title).toBe("New Title");
  });

  it("generateSessionTitle updates title from API response", async () => {
    useChatStore.setState({
      sessions: [{ id: "s1", title: "Old", created_at: "", updated_at: "", last_message: null, last_message_role: null }],
    });
    mockFetch.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ title: "Generated Title" }),
    });
    await useChatStore.getState().generateSessionTitle("s1");
    expect(useChatStore.getState().sessions[0].title).toBe("Generated Title");
  });
});
