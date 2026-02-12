import { create } from "zustand";
import { API_URL } from "../config/constants";
import type {
  ChatMessage,
  ConnectionStatus,
  AgentVisualState,
  AmbientState,
  SessionInfo,
  ToolMeta,
} from "../types";

interface ChatStore {
  messages: ChatMessage[];
  sessionId: string | null;
  sessions: SessionInfo[];
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
  magicEffectPoolSize: number;
  debugWalkability: boolean;

  addMessage: (message: ChatMessage) => void;
  setMessages: (messages: ChatMessage[]) => void;
  setSessionId: (id: string) => void;
  setSessions: (sessions: SessionInfo[]) => void;
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
  setMagicEffectPoolSize: (size: number) => void;
  setDebugWalkability: (on: boolean) => void;
  fetchToolRegistry: () => Promise<void>;
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

const LAST_SESSION_KEY = "seraph_last_session_id";
const MAX_MESSAGES = 500;

const defaultVisual: AgentVisualState = {
  animationState: "idle",
  positionX: 50,
  facing: "right",
  speechText: null,
};

export const useChatStore = create<ChatStore>((set, get) => ({
  messages: [],
  sessionId: localStorage.getItem(LAST_SESSION_KEY),
  sessions: [],
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
  magicEffectPoolSize: 0,
  debugWalkability: false,

  addMessage: (message) =>
    set((state) => {
      const updated = [...state.messages, message];
      return { messages: updated.length > MAX_MESSAGES ? updated.slice(-MAX_MESSAGES) : updated };
    }),

  setMessages: (messages) =>
    set({ messages: messages.length > MAX_MESSAGES ? messages.slice(-MAX_MESSAGES) : messages }),

  setSessionId: (id) => {
    localStorage.setItem(LAST_SESSION_KEY, id);
    set({ sessionId: id });
  },

  setSessions: (sessions) => set({ sessions }),

  setConnectionStatus: (status) => set({ connectionStatus: status }),

  setAgentBusy: (busy) => set({ isAgentBusy: busy }),

  setAgentVisual: (visual) =>
    set((state) => ({
      agentVisual: { ...state.agentVisual, ...visual },
    })),

  resetAgentVisual: () => set({ agentVisual: { ...defaultVisual } }),

  setAmbientState: (state) => set({ ambientState: state }),

  setAmbientTooltip: (tooltip) => set({ ambientTooltip: tooltip }),

  setChatPanelOpen: (open) =>
    set({ chatPanelOpen: open, questPanelOpen: open ? false : get().questPanelOpen }),

  toggleChatMaximized: () => set((state) => ({ chatMaximized: !state.chatMaximized })),

  setQuestPanelOpen: (open) =>
    set({ questPanelOpen: open, chatPanelOpen: open ? false : get().chatPanelOpen }),

  setSettingsPanelOpen: (open) => set({ settingsPanelOpen: open }),

  setOnboardingCompleted: (completed) => set({ onboardingCompleted: completed }),

  setToolRegistry: (tools) => set({ toolRegistry: tools }),

  setMagicEffectPoolSize: (size) => set({ magicEffectPoolSize: size }),

  setDebugWalkability: (on) => set({ debugWalkability: on }),

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
        localStorage.removeItem(LAST_SESSION_KEY);
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
        localStorage.setItem(LAST_SESSION_KEY, sessionId);
        set({ sessionId, messages: chatMessages });
      }
    } catch (err) {
      console.error("Failed to switch session:", err);
    }
  },

  newSession: () => {
    localStorage.removeItem(LAST_SESSION_KEY);
    set({ sessionId: null, messages: [] });
  },

  deleteSession: async (sessionId: string) => {
    try {
      await fetch(`${API_URL}/api/sessions/${sessionId}`, { method: "DELETE" });
      const { sessions, sessionId: currentId } = get();
      set({ sessions: sessions.filter((s) => s.id !== sessionId) });
      if (currentId === sessionId) {
        localStorage.removeItem(LAST_SESSION_KEY);
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
