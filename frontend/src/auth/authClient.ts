import { apiUrl } from "../config/constants";

export type AuthSession = {
  authenticated: boolean;
  principal_id?: string | null;
  username?: string | null;
  operator?: { username?: string | null; display_name?: string | null } | null;
};

export class AuthRequiredError extends Error {
  constructor(public readonly code = "authentication_required") {
    super("Authentication required");
    this.name = "AuthRequiredError";
  }
}

export class AuthConfigurationError extends Error {
  constructor() {
    super("Operator authentication is not configured");
    this.name = "AuthConfigurationError";
  }
}

let unauthorizedHandler: (() => void) | null = null;
let restoreFetch: (() => void) | null = null;

export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler;
}

export function installAuthenticatedFetch(): () => void {
  if (restoreFetch) return restoreFetch;
  const nativeFetch = globalThis.fetch.bind(globalThis);
  globalThis.fetch = async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const response = await nativeFetch(input, { ...init, credentials: "include" });
    if (response.status === 401) unauthorizedHandler?.();
    return response;
  };
  restoreFetch = () => {
    globalThis.fetch = nativeFetch;
    restoreFetch = null;
  };
  return restoreFetch;
}

export async function authenticatedFetch(
  input: RequestInfo | URL,
  init: RequestInit = {},
): Promise<Response> {
  const response = await fetch(input, { ...init, credentials: "include" });
  if (response.status === 401) unauthorizedHandler?.();
  return response;
}

async function authJson(path: string, init?: RequestInit): Promise<AuthSession> {
  const response = await authenticatedFetch(apiUrl(path), init);
  const payload = await response.json().catch(() => null);
  const code = payload?.detail?.code;
  if (response.status === 503 && code === "auth_not_configured") throw new AuthConfigurationError();
  if (response.status === 401) throw new AuthRequiredError(typeof code === "string" ? code : undefined);
  if (!response.ok) {
    throw new Error(payload?.detail?.message ?? payload?.detail ?? "Authentication request failed");
  }
  return payload;
}

export const authClient = {
  session: () => authJson("/api/auth/session"),
  login: (password: string) => authJson("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  }),
  refresh: () => authJson("/api/auth/refresh", { method: "POST" }),
  logout: async () => {
    const response = await authenticatedFetch(apiUrl("/api/auth/logout"), { method: "POST" });
    if (!response.ok && response.status !== 401) throw new Error("Logout failed");
  },
};
