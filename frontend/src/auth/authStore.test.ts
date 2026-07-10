import { beforeEach, describe, expect, it, vi } from "vitest";
import { authClient, AuthConfigurationError } from "./authClient";
import { useAuthStore } from "./authStore";
import { useChatStore } from "../stores/chatStore";

describe("auth store continuity", () => {
  beforeEach(() => {
    localStorage.clear();
    useAuthStore.setState({ status: "locked", operatorName: null, revision: 0, error: null });
    useChatStore.getState().clearAuthenticatedContinuity();
    vi.restoreAllMocks();
  });

  it("unlocks from a valid session without persisting a secret", async () => {
    vi.spyOn(authClient, "login").mockResolvedValue({ authenticated: true, principal_id: "operator" });

    expect(await useAuthStore.getState().login("private-passphrase")).toBe(true);

    expect(useAuthStore.getState()).toMatchObject({ status: "authenticated", operatorName: "operator", revision: 1 });
    expect(JSON.stringify(localStorage)).not.toContain("private-passphrase");
  });

  it("rotates the session revision so the websocket reconnects", async () => {
    useAuthStore.setState({ status: "authenticated", revision: 3 });
    vi.spyOn(authClient, "refresh").mockResolvedValue({ authenticated: true, principal_id: "operator" });

    expect(await useAuthStore.getState().refresh()).toBe(true);
    expect(useAuthStore.getState().revision).toBe(4);
  });

  it("logout locks and removes conversation continuity", async () => {
    localStorage.setItem("seraph_last_session_id", "session-secret-pointer");
    useChatStore.setState({ sessionId: "session-secret-pointer", messages: [{ id: "m1", role: "user", content: "hello", timestamp: 1, sessionId: "session-secret-pointer" }] });
    vi.spyOn(authClient, "logout").mockResolvedValue();

    await useAuthStore.getState().logout();

    expect(useAuthStore.getState().status).toBe("locked");
    expect(useChatStore.getState()).toMatchObject({ sessionId: null, messages: [], connectionStatus: "disconnected" });
    expect(localStorage.getItem("seraph_last_session_id")).toBeNull();
  });

  it("blocks login while server authentication is unconfigured", async () => {
    vi.spyOn(authClient, "session").mockRejectedValue(new AuthConfigurationError());
    const login = vi.spyOn(authClient, "login");

    await useAuthStore.getState().bootstrap();
    expect(useAuthStore.getState().status).toBe("setup_required");
    expect(await useAuthStore.getState().login("must-not-be-sent")).toBe(false);
    expect(login).not.toHaveBeenCalled();
  });
});
