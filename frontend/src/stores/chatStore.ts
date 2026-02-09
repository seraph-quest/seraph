import { create } from "zustand";
import { API_URL } from "../config/constants";
import type {
  ChatMessage,
  ConnectionStatus,
  AgentVisualState,
  AmbientState,
  SessionInfo,
} from "../types";

interface ChatStore {
  messages: ChatMessage[];
  sessionId: string | null;
  sessions: SessionInfo[];
  connectionStatus: ConnectionStatus;
  isAgentBusy: boolean;
  agentVisual: AgentVisualState;
  ambientState: AmbientState;
  chatPanelOpen: boolean;
  questPanelOpen: boolean;
  settingsOpen: boolean;
  onboardingCompleted: boolean | null;

  addMessage: (message: ChatMessage) => void;
  setMessages: (messages: ChatMessage[]) => void;
  setSessionId: (id: string) => void;
  setSessions: (sessions: SessionInfo[]) => void;
  setConnectionStatus: (status: ConnectionStatus) => void;
  setAgentBusy: (busy: boolean) => void;
  setAgentVisual: (visual: Partial<AgentVisualState>) => void;
  resetAgentVisual: () => void;
  setAmbientState: (state: AmbientState) => void;
  setChatPanelOpen: (open: boolean) => void;
  setQuestPanelOpen: (open: boolean) => void;
  setSettingsOpen: (open: boolean) => void;
  setOnboardingCompleted: (completed: boolean) => void;
  fetchProfile: () => Promise<void>;
  skipOnboarding: () => Promise<void>;
  restartOnboarding: () => Promise<void>;
  loadSessions: () => Promise<void>;
  switchSession: (sessionId: string) => Promise<void>;
  newSession: () => void;
  deleteSession: (sessionId: string) => Promise<void>;
  renameSession: (sessionId: string, title: string) => Promise<void>;
  generateSessionTitle: (sessionId: string) => Promise<void>;
}

const defaultVisual: AgentVisualState = {
  animationState: "idle",
  positionX: 50,
  facing: "right",
  speechText: null,
};

export const useChatStore = create<ChatStore>((set, get) => ({
  messages: [],
  sessionId: null,
  sessions: [],
  connectionStatus: "disconnected",
  isAgentBusy: false,
  agentVisual: { ...defaultVisual },
  ambientState: "idle",
  chatPanelOpen: true,
  questPanelOpen: false,
  settingsOpen: false,
  onboardingCompleted: null,

  addMessage: (message) =>
    set((state) => ({ messages: [...state.messages, message] })),

  setMessages: (messages) => set({ messages }),

  setSessionId: (id) => set({ sessionId: id }),

  setSessions: (sessions) => set({ sessions }),

  setConnectionStatus: (status) => set({ connectionStatus: status }),

  setAgentBusy: (busy) => set({ isAgentBusy: busy }),

  setAgentVisual: (visual) =>
    set((state) => ({
      agentVisual: { ...state.agentVisual, ...visual },
    })),

  resetAgentVisual: () => set({ agentVisual: { ...defaultVisual } }),

  setAmbientState: (state) => set({ ambientState: state }),

  setChatPanelOpen: (open) =>
    set({ chatPanelOpen: open, questPanelOpen: open ? false : get().questPanelOpen }),

  setQuestPanelOpen: (open) =>
    set({ questPanelOpen: open, chatPanelOpen: open ? false : get().chatPanelOpen }),

  setSettingsOpen: (open) => set({ settingsOpen: open }),

  setOnboardingCompleted: (completed) => set({ onboardingCompleted: completed }),

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
        set({ sessions });
      }
    } catch (err) {
      console.error("Failed to load sessions:", err);
    }
  },

  switchSession: async (sessionId: string) => {
    try {
      const res = await fetch(`${API_URL}/api/sessions/${sessionId}/messages`);
      if (res.ok) {
        const msgs = await res.json();
        const chatMessages: ChatMessage[] = msgs.map((m: Record<string, unknown>) => ({
          id: m.id as string,
          role: m.role === "assistant" ? "agent" : m.role as string,
          content: m.content as string,
          timestamp: new Date(m.created_at as string).getTime(),
          stepNumber: m.step_number as number | undefined,
          toolUsed: m.tool_used as string | undefined,
        }));
        set({ sessionId, messages: chatMessages });
      }
    } catch (err) {
      console.error("Failed to switch session:", err);
    }
  },

  newSession: () => {
    set({ sessionId: null, messages: [] });
  },

  deleteSession: async (sessionId: string) => {
    try {
      await fetch(`${API_URL}/api/sessions/${sessionId}`, { method: "DELETE" });
      const { sessions, sessionId: currentId } = get();
      set({ sessions: sessions.filter((s) => s.id !== sessionId) });
      if (currentId === sessionId) {
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
