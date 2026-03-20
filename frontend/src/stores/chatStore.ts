import { create } from "zustand";
import { API_URL } from "../config/constants";
import type {
  ChatMessage,
  ConnectionStatus,
  AgentVisualState,
  AmbientState,
  SessionInfo,
  SessionContinuityState,
  ToolMeta,
} from "../types";

interface ChatStore {
  messages: ChatMessage[];
  sessionId: string | null;
  sessions: SessionInfo[];
  sessionContinuity: Record<string, SessionContinuityState>;
  connectionStatus: ConnectionStatus;
  isAgentBusy: boolean;
  agentVisual: AgentVisualState;
  ambientState: AmbientState;
  ambientTooltip: string;
  chatPanelOpen: boolean;
  chatMaximized: boolean;
  questPanelOpen: boolean;
  settingsPanelOpen: boolean;
  onboardingCompleted: boolean | null;
  toolRegistry: ToolMeta[];

  addMessage: (message: ChatMessage) => void;
  setMessages: (messages: ChatMessage[]) => void;
  setSessionId: (id: string) => void;
  setSessions: (sessions: SessionInfo[]) => void;
  markSessionContinuity: (sessionId: string, state: SessionContinuityState) => void;
  clearSessionContinuity: (sessionId: string) => void;
  setConnectionStatus: (status: ConnectionStatus) => void;
  setAgentBusy: (busy: boolean) => void;
  setAgentVisual: (visual: Partial<AgentVisualState>) => void;
  resetAgentVisual: () => void;
  setAmbientState: (state: AmbientState) => void;
  setAmbientTooltip: (tooltip: string) => void;
  setChatPanelOpen: (open: boolean) => void;
  toggleChatMaximized: () => void;
  setQuestPanelOpen: (open: boolean) => void;
  setSettingsPanelOpen: (open: boolean) => void;
  setOnboardingCompleted: (completed: boolean) => void;
  setToolRegistry: (tools: ToolMeta[]) => void;
  fetchToolRegistry: () => Promise<void>;
  fetchProfile: () => Promise<void>;
  skipOnboarding: () => Promise<void>;
  restartOnboarding: () => Promise<void>;
  loadSessions: () => Promise<void>;
  restoreLastSession: () => Promise<void>;
  switchSession: (sessionId: string, continuityState?: SessionContinuityState) => Promise<void>;
  newSession: () => void;
  deleteSession: (sessionId: string) => Promise<void>;
  renameSession: (sessionId: string, title: string) => Promise<void>;
  generateSessionTitle: (sessionId: string) => Promise<void>;
}

const LAST_SESSION_KEY = "seraph_last_session_id";
const MAX_MESSAGES = 500;
let restoreLastSessionPromise: Promise<void> | null = null;

function safeStorageGet(key: string): string | null {
  if (typeof localStorage === "undefined" || typeof localStorage.getItem !== "function") {
    return null;
  }
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function safeStorageSet(key: string, value: string): void {
  if (typeof localStorage === "undefined" || typeof localStorage.setItem !== "function") {
    return;
  }
  try {
    localStorage.setItem(key, value);
  } catch {
    // Ignore storage failures so the app still works in constrained environments.
  }
}

function safeStorageRemove(key: string): void {
  if (typeof localStorage === "undefined" || typeof localStorage.removeItem !== "function") {
    return;
  }
  try {
    localStorage.removeItem(key);
  } catch {
    // Ignore storage failures so the app still works in constrained environments.
  }
}

const defaultVisual: AgentVisualState = {
  animationState: "idle",
  positionX: 50,
  facing: "right",
  speechText: null,
};

export const useChatStore = create<ChatStore>((set, get) => ({
  messages: [],
  sessionId: safeStorageGet(LAST_SESSION_KEY),
  sessions: [],
  sessionContinuity: {},
  connectionStatus: "disconnected",
  isAgentBusy: false,
  agentVisual: { ...defaultVisual },
  ambientState: "idle",
  ambientTooltip: "",
  chatPanelOpen: true,
  chatMaximized: false,
  questPanelOpen: false,
  settingsPanelOpen: false,
  onboardingCompleted: null,
  toolRegistry: [],

  addMessage: (message) =>
    set((state) => {
      const updated = [...state.messages, message];
      return { messages: updated.length > MAX_MESSAGES ? updated.slice(-MAX_MESSAGES) : updated };
    }),

  setMessages: (messages) =>
    set({ messages: messages.length > MAX_MESSAGES ? messages.slice(-MAX_MESSAGES) : messages }),

  setSessionId: (id) => {
    safeStorageSet(LAST_SESSION_KEY, id);
    set({ sessionId: id });
  },

  setSessions: (sessions) => set({ sessions }),

  markSessionContinuity: (sessionId, state) =>
    set((current) => ({
      sessionContinuity: {
        ...current.sessionContinuity,
        [sessionId]: state,
      },
    })),

  clearSessionContinuity: (sessionId) =>
    set((current) => {
      if (!(sessionId in current.sessionContinuity)) {
        return current;
      }
      const next = { ...current.sessionContinuity };
      delete next[sessionId];
      return { sessionContinuity: next };
    }),

  setConnectionStatus: (status) => set({ connectionStatus: status }),

  setAgentBusy: (busy) => set({ isAgentBusy: busy }),

  setAgentVisual: (visual) =>
    set((state) => ({
      agentVisual: { ...state.agentVisual, ...visual },
    })),

  resetAgentVisual: () => set({ agentVisual: { ...defaultVisual } }),

  setAmbientState: (state) => set({ ambientState: state }),

  setAmbientTooltip: (tooltip) => set({ ambientTooltip: tooltip }),

  setChatPanelOpen: (open) => set({ chatPanelOpen: open }),

  toggleChatMaximized: () => set((state) => ({ chatMaximized: !state.chatMaximized })),

  setQuestPanelOpen: (open) => set({ questPanelOpen: open }),

  setSettingsPanelOpen: (open) => set({ settingsPanelOpen: open }),

  setOnboardingCompleted: (completed) => set({ onboardingCompleted: completed }),

  setToolRegistry: (tools) => set({ toolRegistry: tools }),

  fetchToolRegistry: async () => {
    try {
      const res = await fetch(`${API_URL}/api/tools`);
      if (res.ok) {
        const data = await res.json();
        // API returns array directly (or {tools: [...]})
        const tools = Array.isArray(data) ? data : data.tools ?? [];
        set({
          toolRegistry: tools.map((t: Record<string, unknown>) => ({
            name: t.name as string,
            description: (t.description ?? "") as string,
          })),
        });
      }
    } catch (err) {
      console.error("Failed to fetch tool registry:", err);
    }
  },

  fetchProfile: async () => {
    try {
      const res = await fetch(`${API_URL}/api/user/profile`);
      if (res.ok) {
        const data = await res.json();
        set({ onboardingCompleted: data.onboarding_completed });
      }
    } catch (err) {
      console.error("Failed to fetch profile:", err);
    }
  },

  skipOnboarding: async () => {
    try {
      const res = await fetch(`${API_URL}/api/user/onboarding/skip`, { method: "POST" });
      if (res.ok) {
        set({ onboardingCompleted: true });
      }
    } catch (err) {
      console.error("Failed to skip onboarding:", err);
    }
  },

  restartOnboarding: async () => {
    try {
      const res = await fetch(`${API_URL}/api/user/onboarding/restart`, { method: "POST" });
      if (res.ok) {
        safeStorageRemove(LAST_SESSION_KEY);
        set({ onboardingCompleted: false, sessionId: null, messages: [] });
      }
    } catch (err) {
      console.error("Failed to restart onboarding:", err);
    }
  },

  loadSessions: async () => {
    try {
      const res = await fetch(`${API_URL}/api/sessions`);
      if (res.ok) {
        const sessions = await res.json();
        set((state) => {
          const known = new Set((Array.isArray(sessions) ? sessions : []).map((item: SessionInfo) => item.id));
          const nextContinuity = Object.fromEntries(
            Object.entries(state.sessionContinuity).filter(([id]) => known.has(id)),
          );
          return {
            sessions,
            sessionContinuity: nextContinuity,
          };
        });
      }
    } catch (err) {
      console.error("Failed to load sessions:", err);
    }
  },

  restoreLastSession: async () => {
    if (restoreLastSessionPromise) {
      return restoreLastSessionPromise;
    }

    restoreLastSessionPromise = (async () => {
      await get().loadSessions();

      const storedSessionId = safeStorageGet(LAST_SESSION_KEY) ?? get().sessionId;
      if (!storedSessionId) {
        return;
      }

      const state = get();
      if (state.sessionId === storedSessionId && state.messages.length > 0) {
        return;
      }

      await get().switchSession(storedSessionId, "restored");
    })();

    try {
      await restoreLastSessionPromise;
    } finally {
      restoreLastSessionPromise = null;
    }
  },

  switchSession: async (sessionId: string, continuityState?: SessionContinuityState) => {
    try {
      const res = await fetch(`${API_URL}/api/sessions/${sessionId}/messages`);
      if (res.ok) {
        const msgs = await res.json();
        const chatMessages: ChatMessage[] = msgs.map((m: Record<string, unknown>) => ({
          id: m.id as string,
          role: m.role === "assistant" ? "agent" : m.role as string,
          content: m.content as string,
          timestamp: new Date(m.created_at as string).getTime(),
          sessionId,
          stepNumber: m.step_number as number | undefined,
          toolUsed: m.tool_used as string | undefined,
        }));
        safeStorageSet(LAST_SESSION_KEY, sessionId);
        set((state) => {
          const nextContinuity = { ...state.sessionContinuity };
          if (continuityState) {
            nextContinuity[sessionId] = continuityState;
          } else {
            delete nextContinuity[sessionId];
          }
          return { sessionId, messages: chatMessages, sessionContinuity: nextContinuity };
        });
      } else if (res.status === 404 && get().sessionId === sessionId) {
        safeStorageRemove(LAST_SESSION_KEY);
        set((state) => {
          const nextContinuity = { ...state.sessionContinuity };
          delete nextContinuity[sessionId];
          return { sessionId: null, messages: [], sessionContinuity: nextContinuity };
        });
      }
    } catch (err) {
      console.error("Failed to switch session:", err);
    }
  },

  newSession: () => {
    safeStorageRemove(LAST_SESSION_KEY);
    set({ sessionId: null, messages: [] });
  },

  deleteSession: async (sessionId: string) => {
    try {
      await fetch(`${API_URL}/api/sessions/${sessionId}`, { method: "DELETE" });
      const { sessions, sessionId: currentId } = get();
      set({ sessions: sessions.filter((s) => s.id !== sessionId) });
      if (currentId === sessionId) {
        safeStorageRemove(LAST_SESSION_KEY);
        set({ sessionId: null, messages: [] });
      }
    } catch (err) {
      console.error("Failed to delete session:", err);
    }
  },

  renameSession: async (sessionId: string, title: string) => {
    try {
      const res = await fetch(`${API_URL}/api/sessions/${sessionId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title }),
      });
      if (res.ok) {
        const { sessions } = get();
        set({ sessions: sessions.map((s) => s.id === sessionId ? { ...s, title } : s) });
      }
    } catch (err) {
      console.error("Failed to rename session:", err);
    }
  },

  generateSessionTitle: async (sessionId: string) => {
    try {
      const res = await fetch(`${API_URL}/api/sessions/${sessionId}/generate-title`, {
        method: "POST",
      });
      if (res.ok) {
        const data = await res.json();
        const { sessions } = get();
        set({ sessions: sessions.map((s) => s.id === sessionId ? { ...s, title: data.title } : s) });
      }
    } catch (err) {
      console.error("Failed to generate session title:", err);
    }
  },
}));
