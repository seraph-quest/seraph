import { afterEach, describe, expect, it, vi } from "vitest";
import { authClient, AuthConfigurationError, authenticatedFetch, installAuthenticatedFetch, setUnauthorizedHandler } from "./authClient";

afterEach(() => {
  vi.restoreAllMocks();
  setUnauthorizedHandler(null);
});

describe("cookie-only authentication client", () => {
  it("sends only the passphrase and includes browser credentials", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ authenticated: true, principal_id: "operator" }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);

    await authClient.login("correct horse battery staple");

    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/api/auth/login"), expect.objectContaining({ credentials: "include" }));
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(JSON.parse(String(init.body))).toEqual({ password: "correct horse battery staple" });
    expect(String(init.body)).not.toContain("username");
  });

  it("locks centrally when an authenticated request receives 401", async () => {
    const lock = vi.fn();
    setUnauthorizedHandler(lock);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 401 })));

    await authenticatedFetch("/api/goals");

    expect(lock).toHaveBeenCalledOnce();
  });

  it("installs credentials and 401 handling for existing fetch call sites", async () => {
    const native = vi.fn().mockResolvedValue(new Response(null, { status: 401 }));
    vi.stubGlobal("fetch", native);
    const lock = vi.fn();
    setUnauthorizedHandler(lock);
    const restore = installAuthenticatedFetch();

    await fetch("/api/runtime/status", { method: "POST" });

    expect(native).toHaveBeenCalledWith("/api/runtime/status", expect.objectContaining({ method: "POST", credentials: "include" }));
    expect(lock).toHaveBeenCalledOnce();
    restore();
  });

  it("distinguishes an unconfigured backend from invalid credentials", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(
      JSON.stringify({ detail: { code: "auth_not_configured" } }),
      { status: 503, headers: { "Content-Type": "application/json" } },
    )));

    await expect(authClient.session()).rejects.toBeInstanceOf(AuthConfigurationError);
  });
});
