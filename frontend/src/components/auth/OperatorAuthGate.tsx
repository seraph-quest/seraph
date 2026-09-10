import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import { AUTH_REQUIRED_EVENT } from "../../lib/operatorAuthEvents";

type AuthView = "checking" | "login" | "setup" | "unavailable" | "authenticated";

export interface OperatorSession {
  authenticated: true;
  principal_id: string;
  idle_expires_at: string;
  absolute_expires_at: string;
}

interface OperatorAuthContextValue {
  session: OperatorSession;
  refreshSession: () => Promise<boolean>;
  logout: () => Promise<void>;
}

const OperatorAuthContext = createContext<OperatorAuthContextValue | null>(null);

function errorCode(payload: unknown): string | null {
  if (!payload || typeof payload !== "object") return null;
  const detail = (payload as Record<string, unknown>).detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") {
    const code = (detail as Record<string, unknown>).code;
    return typeof code === "string" ? code : null;
  }
  return null;
}

async function readPayload(response: Response): Promise<unknown> {
  return response.json().catch(() => null);
}

function LoginForm({
  error,
  onLogin,
}: {
  error: string | null;
  onLogin: (password: string) => Promise<void>;
}) {
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!password.trim() || submitting) return;
    setSubmitting(true);
    try {
      await onLogin(password);
      setPassword("");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form onSubmit={submit} className="w-full max-w-sm border border-retro-text/20 bg-retro-bg/80 p-5 shadow-xl">
      <div className="text-retro-highlight text-xs uppercase tracking-[0.3em]">Seraph operator access</div>
      <h1 className="mt-3 text-xl font-semibold text-retro-text">Sign in to the cockpit</h1>
      <p className="mt-2 text-xs leading-5 text-retro-text/60">
        The backend keeps the operator credential and session cookie server-side. Your password is sent only to the configured local backend.
      </p>
      <label className="mt-5 block text-[10px] uppercase tracking-wider text-retro-text/50" htmlFor="operator-password">
        Operator password
      </label>
      <input
        id="operator-password"
        autoFocus
        autoComplete="current-password"
        type="password"
        value={password}
        onChange={(event) => setPassword(event.target.value)}
        className="mt-1 w-full border border-retro-text/20 bg-transparent px-2 py-2 text-sm text-retro-text outline-none focus:border-retro-highlight"
      />
      {error && <div role="alert" className="mt-2 text-xs text-red-400">{error}</div>}
      <button
        type="submit"
        disabled={submitting || !password.trim()}
        className="mt-4 border border-retro-highlight px-3 py-2 text-[10px] uppercase tracking-wider text-retro-highlight hover:bg-retro-highlight/10 disabled:cursor-not-allowed disabled:opacity-40"
      >
        {submitting ? "Signing in..." : "Sign in"}
      </button>
    </form>
  );
}

function SetupRequiredView({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="w-full max-w-xl border border-yellow-400/40 bg-retro-bg/80 p-5 shadow-xl">
      <div className="text-yellow-400 text-xs uppercase tracking-[0.3em]">Operator setup required</div>
      <h1 className="mt-3 text-xl font-semibold text-retro-text">Seraph is locked until a server credential is configured</h1>
      <p className="mt-2 text-xs leading-5 text-retro-text/60">
        Authentication is intentionally fail-closed. Generate a PBKDF2 hash on the backend host, store it as
        <code className="mx-1 text-yellow-300">OPERATOR_AUTH_SECRET_HASH</code>
        in the backend secret environment, then restart the managed backend.
      </p>
      <pre className="mt-4 overflow-x-auto border border-retro-text/10 bg-black/20 p-3 text-[10px] leading-5 text-retro-text/70">
        cd backend{"\n"}uv run python -c 'from src.auth.service import encode_secret; print(encode_secret("REPLACE_ME"))'
      </pre>
      <p className="mt-3 text-[10px] leading-4 text-retro-text/45">
        Keep the raw password in the deployment secret store only. Provider key setup is optional and does not unlock authentication or issue an inference request.
      </p>
      <button
        type="button"
        onClick={onRetry}
        className="mt-4 border border-retro-text/20 px-3 py-2 text-[10px] uppercase tracking-wider text-retro-text/70 hover:text-retro-text"
      >
        Check setup again
      </button>
    </div>
  );
}

function BackendUnavailableView({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="w-full max-w-xl border border-red-400/40 bg-retro-bg/80 p-5 shadow-xl">
      <div className="text-red-400 text-xs uppercase tracking-[0.3em]">Backend unavailable</div>
      <h1 className="mt-3 text-xl font-semibold text-retro-text">The operator session could not be checked</h1>
      <p className="mt-2 text-xs leading-5 text-retro-text/60">
        Start the managed local backend, then retry. No provider or GPU request is made by this check.
      </p>
      <button
        type="button"
        onClick={onRetry}
        className="mt-4 border border-retro-text/20 px-3 py-2 text-[10px] uppercase tracking-wider text-retro-text/70 hover:text-retro-text"
      >
        Retry connection
      </button>
    </div>
  );
}

export function useOperatorAuth(): OperatorAuthContextValue {
  const value = useContext(OperatorAuthContext);
  if (!value) {
    throw new Error("useOperatorAuth must be used inside OperatorAuthGate");
  }
  return value;
}

export function useOptionalOperatorAuth(): OperatorAuthContextValue | null {
  return useContext(OperatorAuthContext);
}

export function OperatorAuthGate({ children }: { children: ReactNode }) {
  const [view, setView] = useState<AuthView>("checking");
  const [session, setSession] = useState<OperatorSession | null>(null);
  const [loginError, setLoginError] = useState<string | null>(null);

  const inspectSession = useCallback(async () => {
    setLoginError(null);
    setView("checking");
    try {
      const response = await apiFetch(`${API_URL}/api/auth/session`);
      const payload = await readPayload(response);
      if (response.ok && payload && typeof payload === "object" && (payload as Record<string, unknown>).authenticated === true) {
        setSession(payload as OperatorSession);
        setView("authenticated");
        return;
      }
      const code = errorCode(payload);
      setSession(null);
      setView(response.status === 503 && code === "auth_not_configured" ? "setup" : response.status === 401 ? "login" : "unavailable");
    } catch {
      setSession(null);
      setView("unavailable");
    }
  }, []);

  useEffect(() => {
    void inspectSession();
  }, [inspectSession]);

  useEffect(() => {
    const handleAuthRequired = () => {
      setSession(null);
      setLoginError("Your operator session is no longer valid. Sign in again.");
      setView("login");
    };
    window.addEventListener(AUTH_REQUIRED_EVENT, handleAuthRequired);
    return () => window.removeEventListener(AUTH_REQUIRED_EVENT, handleAuthRequired);
  }, []);

  const login = useCallback(async (password: string) => {
    setLoginError(null);
    try {
      const response = await apiFetch(`${API_URL}/api/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
      });
      const payload = await readPayload(response);
      if (response.ok && payload && typeof payload === "object" && (payload as Record<string, unknown>).authenticated === true) {
        const sessionResponse = await apiFetch(`${API_URL}/api/auth/session`);
        const sessionPayload = await readPayload(sessionResponse);
        if (sessionResponse.ok && sessionPayload && typeof sessionPayload === "object" && (sessionPayload as Record<string, unknown>).authenticated === true) {
          setSession(sessionPayload as OperatorSession);
          setView("authenticated");
          return;
        }
      }
      const code = errorCode(payload);
      if (response.status === 503 && code === "auth_not_configured") {
        setView("setup");
      } else {
        setLoginError(code === "login_rate_limited" ? "Too many attempts. Wait a minute and try again." : "Invalid operator credentials.");
        setView("login");
      }
    } catch {
      setLoginError("The backend could not be reached. Retry when it is running.");
      setView("login");
    }
  }, []);

  const refreshSession = useCallback(async () => {
    try {
      const response = await apiFetch(`${API_URL}/api/auth/refresh`, { method: "POST" });
      const payload = await readPayload(response);
      if (!response.ok || !payload || typeof payload !== "object" || (payload as Record<string, unknown>).authenticated !== true) {
        setSession(null);
        setLoginError("Your operator session could not be refreshed. Sign in again.");
        setView("login");
        return false;
      }
      setSession(payload as OperatorSession);
      return true;
    } catch {
      setSession(null);
      setLoginError("The backend could not refresh your operator session.");
      setView("login");
      return false;
    }
  }, []);

  const logout = useCallback(async () => {
    try {
      await apiFetch(`${API_URL}/api/auth/logout`, { method: "POST" });
    } finally {
      setSession(null);
      setLoginError(null);
      setView("login");
    }
  }, []);

  useEffect(() => {
    if (!session) return;
    const refreshDelay = Math.max(
      Math.min(new Date(session.idle_expires_at).getTime() - Date.now() - 60_000, 15 * 60_000),
      30_000,
    );
    const timer = window.setTimeout(() => void refreshSession(), refreshDelay);
    return () => window.clearTimeout(timer);
  }, [refreshSession, session]);

  const contextValue = useMemo<OperatorAuthContextValue | null>(
    () => session ? { session, refreshSession, logout } : null,
    [logout, refreshSession, session],
  );

  if (view === "checking") {
    return <div className="min-h-screen bg-retro-bg p-8 text-xs uppercase tracking-wider text-retro-text/50">Checking operator session...</div>;
  }
  if (view === "setup") {
    return <div className="min-h-screen bg-retro-bg p-8 flex items-center justify-center"><SetupRequiredView onRetry={() => void inspectSession()} /></div>;
  }
  if (view === "unavailable") {
    return <div className="min-h-screen bg-retro-bg p-8 flex items-center justify-center"><BackendUnavailableView onRetry={() => void inspectSession()} /></div>;
  }
  if (!session || !contextValue) {
    return <div className="min-h-screen bg-retro-bg p-8 flex items-center justify-center"><LoginForm error={loginError} onLogin={login} /></div>;
  }
  return <OperatorAuthContext.Provider value={contextValue}>{children}</OperatorAuthContext.Provider>;
}
