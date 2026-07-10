import { create } from "zustand";
import { authClient, AuthConfigurationError, AuthRequiredError, setUnauthorizedHandler, type AuthSession } from "./authClient";
import { useChatStore } from "../stores/chatStore";

export type AuthStatus = "bootstrapping" | "setup_required" | "locked" | "authenticated";

type AuthStore = {
  status: AuthStatus;
  operatorName: string | null;
  revision: number;
  error: string | null;
  bootstrap: () => Promise<void>;
  login: (password: string) => Promise<boolean>;
  refresh: () => Promise<boolean>;
  logout: () => Promise<void>;
  lock: () => void;
};

function nameFromSession(session: AuthSession): string | null {
  return session.operator?.display_name ?? session.operator?.username ?? session.username ?? session.principal_id ?? null;
}

function clearBrowserContinuity(): void {
  useChatStore.getState().clearAuthenticatedContinuity();
}

export const useAuthStore = create<AuthStore>((set, get) => ({
  status: "bootstrapping",
  operatorName: null,
  revision: 0,
  error: null,

  bootstrap: async () => {
    try {
      const session = await authClient.session();
      if (!session.authenticated) throw new AuthRequiredError();
      set((state) => ({
        status: "authenticated",
        operatorName: nameFromSession(session),
        revision: state.revision + 1,
        error: null,
      }));
    } catch (error) {
      if (error instanceof AuthConfigurationError) {
        clearBrowserContinuity();
        set({ status: "setup_required", operatorName: null, error: null });
        return;
      }
      if (!(error instanceof AuthRequiredError)) console.warn("Auth bootstrap failed", error);
      get().lock();
    }
  },

  login: async (password) => {
    if (get().status === "setup_required") return false;
    set({ error: null });
    try {
      const session = await authClient.login(password);
      if (!session.authenticated) throw new AuthRequiredError();
      set((state) => ({
        status: "authenticated",
        operatorName: nameFromSession(session),
        revision: state.revision + 1,
        error: null,
      }));
      return true;
    } catch (error) {
      set({ status: "locked", error: error instanceof AuthRequiredError ? "Invalid credentials" : (error as Error).message });
      return false;
    }
  },

  refresh: async () => {
    try {
      const session = await authClient.refresh();
      if (!session.authenticated) throw new AuthRequiredError();
      set((state) => ({ operatorName: nameFromSession(session), revision: state.revision + 1, error: null }));
      return true;
    } catch {
      get().lock();
      return false;
    }
  },

  logout: async () => {
    try { await authClient.logout(); } finally { get().lock(); }
  },

  lock: () => {
    clearBrowserContinuity();
    set({ status: "locked", operatorName: null, error: null });
  },
}));

setUnauthorizedHandler(() => useAuthStore.getState().lock());
