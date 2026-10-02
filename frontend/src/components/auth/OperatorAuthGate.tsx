import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import { AUTH_REQUIRED_EVENT } from "../../lib/operatorAuthEvents";

type AuthView = "checking" | "login" | "setup" | "unavailable" | "authenticated";

export interface OperatorSession {
  authenticated: true;
  principal_id: string;
  session_id: string;
  idle_expires_at: string;
  absolute_expires_at: string;
  ownership_continuity: "stable" | "legacy_rebind_required";
  ownership_recovery_action: "review_and_recreate_work_in_current_scope" | null;
}

type SessionRead =
  | { kind: "authenticated"; session: OperatorSession }
  | { kind: "denied"; code: string | null }
  | { kind: "setup"; code: string | null }
  | { kind: "unavailable"; code: string | null };

type ReconciliationResult = "same_root" | "different_root" | "denied" | "setup" | "unavailable" | "stale";

const RECONCILIATION_MAX_READS = 2;
const RECONCILIATION_DELAY_MS = 250;
const RECONCILIATION_DEADLINE_MS = 5_000;

function isOperatorSession(payload: unknown): payload is OperatorSession {
  if (!payload || typeof payload !== "object") return false;
  const value = payload as Record<string, unknown>;
  return (
    value.authenticated === true &&
    typeof value.principal_id === "string" &&
    typeof value.session_id === "string" &&
    typeof value.idle_expires_at === "string" &&
    typeof value.absolute_expires_at === "string" &&
    (value.ownership_continuity === "stable" || value.ownership_continuity === "legacy_rebind_required") &&
    (value.ownership_recovery_action === null || value.ownership_recovery_action === "review_and_recreate_work_in_current_scope")
  );
}

function sameOwnerRoot(current: OperatorSession, candidate: OperatorSession): boolean {
  if (current.principal_id !== candidate.principal_id) return false;
  if (current.session_id !== candidate.session_id) return false;
  const currentExpiry = Date.parse(current.absolute_expires_at);
  const candidateExpiry = Date.parse(candidate.absolute_expires_at);
  return Number.isFinite(currentExpiry) && currentExpiry === candidateExpiry;
}

async function requestWithDeadline(
  input: RequestInfo | URL,
  init: RequestInit & { authRequired?: boolean },
  timeoutMs: number,
): Promise<{ response: Response; payload: unknown }> {
  const controller = new AbortController();
  let timeoutId: number | undefined;
  let rejectDeadline: ((reason?: unknown) => void) | undefined;
  const deadline = new Promise<never>((_, reject) => {
    rejectDeadline = reject;
    timeoutId = window.setTimeout(() => {
      controller.abort();
      reject(new Error("The operator request exceeded its deadline."));
    }, Math.max(timeoutMs, 1));
  });
  try {
    const response = await Promise.race([
      apiFetch(input, { ...init, signal: controller.signal }),
      deadline,
    ]);
    const payload = await Promise.race([readPayload(response), deadline]);
    return { response, payload };
  } finally {
    if (timeoutId !== undefined) window.clearTimeout(timeoutId);
    // If the request completed before the deadline, settle the losing promise
    // so it cannot retain a pending rejection after the timer is cleared.
    rejectDeadline?.();
  }
}

async function readOperatorSession(timeoutMs = RECONCILIATION_DEADLINE_MS): Promise<SessionRead> {
  try {
    // Reconciliation reads must never dispatch AUTH_REQUIRED_EVENT.  The
    // caller owns the bounded recovery decision after inspecting this result.
    const { response, payload } = await requestWithDeadline(`${API_URL}/api/auth/session`, {
      authRequired: false,
    }, timeoutMs);
    if (response.ok && isOperatorSession(payload)) {
      return { kind: "authenticated", session: payload };
    }
    const code = errorCode(payload);
    if (response.status === 503 && code === "auth_not_configured") {
      return { kind: "setup", code };
    }
    if (response.status === 401) {
      return { kind: "denied", code };
    }
    return { kind: "unavailable", code };
  } catch {
    return { kind: "unavailable", code: null };
  }
}

function waitFor(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms));
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
    <form onSubmit={submit} className="cockpit-auth-card cockpit-auth-login w-full max-w-sm p-5">
      <div className="cockpit-auth-eyebrow">Seraph operator access</div>
      <h1 className="cockpit-auth-title mt-3">Sign in to the cockpit</h1>
      <p className="cockpit-auth-copy mt-2">
        The backend keeps the operator credential and session cookie server-side. Your password is sent only to the configured local backend.
      </p>
      <label className="cockpit-auth-label mt-5 block" htmlFor="operator-password">
        Operator password
      </label>
      <input
        id="operator-password"
        autoFocus
        autoComplete="current-password"
        type="password"
        value={password}
        onChange={(event) => setPassword(event.target.value)}
        className="cockpit-auth-input mt-1 w-full px-2 py-2 text-sm"
      />
      {error && <div role="alert" className="cockpit-auth-error mt-2 text-xs">{error}</div>}
      <button
        type="submit"
        disabled={submitting || !password.trim()}
        className="cockpit-auth-button mt-4 px-3 py-2 text-[10px] uppercase disabled:cursor-not-allowed disabled:opacity-40"
      >
        {submitting ? "Signing in..." : "Sign in"}
      </button>
    </form>
  );
}

function SetupRequiredView({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="cockpit-auth-card cockpit-auth-card--warning w-full max-w-xl p-5">
      <div className="cockpit-auth-eyebrow">Operator setup required</div>
      <h1 className="cockpit-auth-title mt-3">Seraph is locked until a server credential is configured</h1>
      <p className="cockpit-auth-copy mt-2">
        Authentication is intentionally fail-closed. Generate a PBKDF2 hash on the backend host, store it as
        <code className="cockpit-auth-code mx-1">OPERATOR_AUTH_SECRET_HASH</code>
        in the backend secret environment, then restart the managed backend.
      </p>
      <pre className="cockpit-auth-pre mt-4 overflow-x-auto p-3 text-[10px] leading-5">
        cd backend{"\n"}uv run python -c 'from src.auth.service import encode_secret; print(encode_secret("REPLACE_ME"))'
      </pre>
      <p className="cockpit-auth-note mt-3 text-[10px] leading-4">
        Keep the raw password in the deployment secret store only. Provider key setup is optional and does not unlock authentication or issue an inference request.
      </p>
      <button
        type="button"
        onClick={onRetry}
        className="cockpit-auth-button cockpit-auth-button--muted mt-4 px-3 py-2 text-[10px] uppercase"
      >
        Check setup again
      </button>
    </div>
  );
}

function BackendUnavailableView({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="cockpit-auth-card cockpit-auth-card--danger w-full max-w-xl p-5">
      <div className="cockpit-auth-eyebrow">Backend unavailable</div>
      <h1 className="cockpit-auth-title mt-3">The operator session could not be checked</h1>
      <p className="cockpit-auth-copy mt-2">
        Start the managed local backend, then retry. No provider or GPU request is made by this check.
      </p>
      <button
        type="button"
        onClick={onRetry}
        className="cockpit-auth-button cockpit-auth-button--muted mt-4 px-3 py-2 text-[10px] uppercase"
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
  const sessionRef = useRef<OperatorSession | null>(null);
  const reconciliationRef = useRef<Promise<ReconciliationResult> | null>(null);
  const refreshRef = useRef<Promise<boolean> | null>(null);
  const authGenerationRef = useRef(0);
  const loginGenerationRef = useRef<number | null>(null);
  const logoutFenceRef = useRef(false);

  const beginGeneration = useCallback((clearMountedOwner: boolean): number => {
    authGenerationRef.current += 1;
    reconciliationRef.current = null;
    refreshRef.current = null;
    loginGenerationRef.current = null;
    if (clearMountedOwner) {
      sessionRef.current = null;
      setSession(null);
    }
    return authGenerationRef.current;
  }, []);

  const publishSession = useCallback((candidate: OperatorSession, generation: number): boolean => {
    if (
      logoutFenceRef.current
      || generation !== authGenerationRef.current
      || (loginGenerationRef.current !== null && loginGenerationRef.current !== generation)
    ) {
      return false;
    }
    const current = sessionRef.current;
    if (current && !sameOwnerRoot(current, candidate)) {
      beginGeneration(true);
      setLoginError("A different operator session is active. Sign in again to continue in this scope.");
      setView("login");
      return false;
    }
    sessionRef.current = candidate;
    setSession(candidate);
    setLoginError(null);
    setView("authenticated");
    return true;
  }, [beginGeneration]);

  const reconcileSession = useCallback((baseline: OperatorSession | null) => {
    if (reconciliationRef.current) return reconciliationRef.current;
    if (logoutFenceRef.current || loginGenerationRef.current !== null) return Promise.resolve("stale" as const);

    const generation = authGenerationRef.current;

    const run = (async () => {
      const deadline = Date.now() + RECONCILIATION_DEADLINE_MS;
      for (let attempt = 0; attempt < RECONCILIATION_MAX_READS; attempt += 1) {
        if (generation !== authGenerationRef.current || loginGenerationRef.current !== null) return "stale" as const;
        if (attempt > 0) {
          const remainingBeforeDelay = deadline - Date.now();
          if (remainingBeforeDelay <= 0) break;
          await waitFor(Math.min(RECONCILIATION_DELAY_MS, remainingBeforeDelay));
        }
        const remaining = deadline - Date.now();
        if (remaining <= 0) break;
        const result = await readOperatorSession(remaining);
        if (generation !== authGenerationRef.current || logoutFenceRef.current || loginGenerationRef.current !== null) return "stale" as const;
        if (result.kind === "authenticated") {
          if (baseline && !sameOwnerRoot(baseline, result.session)) {
            beginGeneration(true);
            setLoginError("A different operator session is active. Sign in again to continue in this scope.");
            setView("login");
            return "different_root" as const;
          }
          return publishSession(result.session, generation) ? "same_root" as const : "stale" as const;
        }
        if (result.kind === "setup") {
          if (attempt === 0) {
            beginGeneration(true);
            setLoginError(null);
            setView("setup");
            return "setup" as const;
          }
        } else if (result.kind === "denied" && attempt === RECONCILIATION_MAX_READS - 1) {
          if (generation !== authGenerationRef.current || loginGenerationRef.current !== null) return "stale" as const;
          beginGeneration(true);
          setLoginError("Your operator session is no longer valid. Sign in again.");
          setView("login");
          return "denied" as const;
        }
        if (result.kind === "setup" && attempt === RECONCILIATION_MAX_READS - 1) {
          if (generation !== authGenerationRef.current || loginGenerationRef.current !== null) return "stale" as const;
          beginGeneration(true);
          sessionRef.current = null;
          setSession(null);
          setLoginError(null);
          setView("setup");
          return "setup" as const;
        }
      }
      if (generation !== authGenerationRef.current || loginGenerationRef.current !== null) return "stale" as const;
      beginGeneration(true);
      sessionRef.current = null;
      setSession(null);
      setLoginError(null);
      setView("unavailable");
      return "unavailable" as const;
    })();
    reconciliationRef.current = run;
    void run.finally(() => {
      if (reconciliationRef.current === run) reconciliationRef.current = null;
    });
    return run;
  }, [beginGeneration, publishSession]);

  const inspectSession = useCallback(async () => {
    logoutFenceRef.current = false;
    const generation = beginGeneration(true);
    setLoginError(null);
    setView("checking");
    const result = await readOperatorSession(RECONCILIATION_DEADLINE_MS);
    if (generation !== authGenerationRef.current) return;
    if (result.kind === "authenticated") {
      publishSession(result.session, generation);
      return;
    }
    if (generation !== authGenerationRef.current) return;
    setView(result.kind === "setup" ? "setup" : result.kind === "denied" ? "login" : "unavailable");
  }, [beginGeneration, publishSession]);

  useEffect(() => {
    void inspectSession();
  }, [inspectSession]);

  useEffect(() => {
    const handleAuthRequired = () => {
      void reconcileSession(sessionRef.current);
    };
    window.addEventListener(AUTH_REQUIRED_EVENT, handleAuthRequired);
    return () => window.removeEventListener(AUTH_REQUIRED_EVENT, handleAuthRequired);
  }, [reconcileSession]);

  const login = useCallback(async (password: string) => {
    logoutFenceRef.current = false;
    const generation = beginGeneration(true);
    loginGenerationRef.current = generation;
    setLoginError(null);
    try {
      const { response, payload } = await requestWithDeadline(`${API_URL}/api/auth/login`, {
        method: "POST",
        authRequired: false,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
      }, RECONCILIATION_DEADLINE_MS);
      if (generation !== authGenerationRef.current) return;
      if (response.ok && payload && typeof payload === "object" && (payload as Record<string, unknown>).authenticated === true) {
        const sessionResult = await readOperatorSession(RECONCILIATION_DEADLINE_MS);
        if (generation !== authGenerationRef.current) return;
        if (sessionResult.kind === "authenticated" && loginGenerationRef.current === generation) {
          loginGenerationRef.current = null;
          publishSession(sessionResult.session, generation);
          return;
        }
      }
      if (generation !== authGenerationRef.current) return;
      const code = errorCode(payload);
      if (response.status === 503 && code === "auth_not_configured") {
        setView("setup");
      } else {
        setLoginError(
          code === "login_rate_limited"
            ? "Too many attempts. Wait a minute and try again."
            : response.ok
              ? "The server session could not be established. Retry."
              : "Invalid operator credentials.",
        );
        setView("login");
      }
    } catch {
      if (generation !== authGenerationRef.current) return;
      setLoginError("The backend could not be reached. Retry when it is running.");
      setView("login");
    } finally {
      if (loginGenerationRef.current === generation) loginGenerationRef.current = null;
    }
  }, [beginGeneration, publishSession]);

  const refreshSession = useCallback(async () => {
    if (logoutFenceRef.current) return false;
    if (refreshRef.current) return refreshRef.current;
    const baseline = sessionRef.current;
    const generation = authGenerationRef.current;
    const run = (async () => {
      try {
        const { response, payload } = await requestWithDeadline(`${API_URL}/api/auth/refresh`, {
          method: "POST",
          authRequired: false,
        }, RECONCILIATION_DEADLINE_MS);
        if (logoutFenceRef.current || generation !== authGenerationRef.current) {
          const latest = sessionRef.current;
          if (!logoutFenceRef.current && latest && loginGenerationRef.current === null) void reconcileSession(latest);
          return false;
        }
        if (response.ok && isOperatorSession(payload)) {
          const refreshed = payload;
          if (baseline && !sameOwnerRoot(baseline, refreshed)) {
            beginGeneration(true);
            setLoginError("A different operator session is active. Sign in again to continue in this scope.");
            setView("login");
            return false;
          }
          return publishSession(refreshed, generation);
        }
        if (logoutFenceRef.current || generation !== authGenerationRef.current) return false;
        return (await reconcileSession(baseline)) === "same_root";
      } catch {
        if (logoutFenceRef.current || generation !== authGenerationRef.current) {
          const latest = sessionRef.current;
          if (!logoutFenceRef.current && latest && loginGenerationRef.current === null) void reconcileSession(latest);
          return false;
        }
        return (await reconcileSession(baseline)) === "same_root";
      }
    })();
    refreshRef.current = run;
    void run.finally(() => {
      if (refreshRef.current === run) refreshRef.current = null;
    });
    return run;
  }, [beginGeneration, publishSession, reconcileSession]);

  const logout = useCallback(async () => {
    logoutFenceRef.current = true;
    const generation = beginGeneration(true);
    setLoginError(null);
    setView("login");
    let logoutError: string | null = null;
    try {
      const { response } = await requestWithDeadline(`${API_URL}/api/auth/logout`, {
        method: "POST",
        authRequired: false,
      }, RECONCILIATION_DEADLINE_MS);
      if (response.status !== 204) {
        logoutError = "Sign-out could not be confirmed by the backend; local session cleared.";
      }
    } catch {
      logoutError = "Sign-out could not be confirmed by the backend; local session cleared.";
    } finally {
      if (generation === authGenerationRef.current) {
        sessionRef.current = null;
        setSession(null);
        setLoginError(logoutError);
        setView("login");
      }
    }
  }, [beginGeneration]);

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
    return <div className="cockpit-auth-shell cockpit-auth-loading p-8 text-xs uppercase">Checking operator session...</div>;
  }
  if (view === "setup") {
    return <div className="cockpit-auth-shell p-8 flex items-center justify-center"><SetupRequiredView onRetry={() => void inspectSession()} /></div>;
  }
  if (view === "unavailable") {
    return <div className="cockpit-auth-shell p-8 flex items-center justify-center"><BackendUnavailableView onRetry={() => void inspectSession()} /></div>;
  }
  if (!session || !contextValue) {
    return <div className="cockpit-auth-shell p-8 flex items-center justify-center"><LoginForm error={loginError} onLogin={login} /></div>;
  }
  return (
    <OperatorAuthContext.Provider value={contextValue}>
      {session.ownership_continuity === "legacy_rebind_required" && (
        <div
          role="status"
          aria-live="polite"
          className="cockpit-auth-recovery-notice border-b border-amber-400/30 bg-amber-400/10 px-4 py-2 text-xs"
        >
          Older work remains stored but is blocked in its previous operator scope. Seraph did not restore historical grants or approvals;
          create fresh work in this scope and review it normally.
        </div>
      )}
      {children}
    </OperatorAuthContext.Provider>
  );
}
