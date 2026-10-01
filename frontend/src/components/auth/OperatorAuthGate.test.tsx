import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AUTH_REQUIRED_EVENT } from "../../lib/operatorAuthEvents";
import { OperatorAuthGate, useOperatorAuth } from "./OperatorAuthGate";

function response(payload: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  } as Response;
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((nextResolve) => { resolve = nextResolve; });
  return { promise, resolve };
}

function hangingBodyResponse() {
  return {
    ok: true,
    status: 200,
    json: () => new Promise<unknown>(() => {}),
  } as Response;
}

const authenticatedSession = {
  authenticated: true,
  principal_id: "operator:single",
  session_id: "stable-owner-root",
  idle_expires_at: "2099-01-01T00:00:00Z",
  absolute_expires_at: "2099-01-02T00:00:00Z",
  ownership_continuity: "stable",
  ownership_recovery_action: null,
};

function ProtectedProbe({ onRefreshPromise }: { onRefreshPromise?: (promise: Promise<boolean>) => void } = {}) {
  const { session, refreshSession, logout } = useOperatorAuth();
  const [mountToken] = useState(() => Math.random().toString(36));
  const refresh = () => {
    const promise = refreshSession();
    onRefreshPromise?.(promise);
    void promise;
  };
  return (
  <div>
      <div data-testid="principal">{session.principal_id}</div>
      <div data-testid="session-id">{session.session_id}</div>
      <div data-testid="mount-token">{mountToken}</div>
      <button type="button" onClick={refresh}>refresh session</button>
      <button type="button" onClick={() => void logout()}>sign out</button>
    </div>
  );
}

describe("OperatorAuthGate", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("shows actionable server-side setup when authentication is not configured", async () => {
    fetchMock.mockResolvedValue(response({ detail: { code: "auth_not_configured" } }, 503));

    render(
      <OperatorAuthGate>
        <ProtectedProbe />
      </OperatorAuthGate>,
    );

    expect(await screen.findByText("Operator setup required")).toBeInTheDocument();
    expect(screen.getByText("OPERATOR_AUTH_SECRET_HASH")).toBeInTheDocument();
    expect(screen.getByText(/encode_secret/)).toBeInTheDocument();
    expect(screen.queryByTestId("principal")).not.toBeInTheDocument();
  });

  it("logs in with credentials, keeps the cookie out of state, and sends credentials on refresh", async () => {
    fetchMock
      .mockResolvedValueOnce(response({ detail: { code: "authentication_required" } }, 401))
      .mockResolvedValueOnce(response({ authenticated: true, principal_id: "operator:single" }))
      .mockResolvedValueOnce(response(authenticatedSession))
      .mockResolvedValueOnce(response({ ...authenticatedSession, idle_expires_at: "2099-01-01T01:00:00Z" }));

    render(
      <OperatorAuthGate>
        <ProtectedProbe />
      </OperatorAuthGate>,
    );

    const password = await screen.findByLabelText("Operator password");
    fireEvent.change(password, { target: { value: "synthetic operator password" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    expect(await screen.findByTestId("principal")).toHaveTextContent("operator:single");
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(fetchMock.mock.calls[1][1]).toMatchObject({
      method: "POST",
      credentials: "include",
    });
    expect(fetchMock.mock.calls[2][1]).toMatchObject({ credentials: "include" });

    fireEvent.click(screen.getByRole("button", { name: "refresh session" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
    expect(fetchMock.mock.calls[3][1]).toMatchObject({
      method: "POST",
      credentials: "include",
    });
    expect(screen.queryByText("synthetic operator password")).not.toBeInTheDocument();
  });

  it("reconciles an auth-required event without unmounting the stable owner", async () => {
    fetchMock.mockResolvedValue(response(authenticatedSession));

    render(
      <OperatorAuthGate>
        <ProtectedProbe />
      </OperatorAuthGate>,
    );

    expect(await screen.findByTestId("principal")).toHaveTextContent("operator:single");
    const mountToken = screen.getByTestId("mount-token").textContent;
    await act(async () => {
      window.dispatchEvent(new Event(AUTH_REQUIRED_EVENT));
    });

    expect(screen.getByTestId("principal")).toHaveTextContent("operator:single");
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId("mount-token")).toHaveTextContent(mountToken ?? "");
    expect(screen.queryByLabelText("Operator password")).not.toBeInTheDocument();
  });

  it("keeps the mounted owner after a refresh CAS loser and winning cookie readback", async () => {
    fetchMock
      .mockResolvedValueOnce(response(authenticatedSession))
      .mockResolvedValueOnce(response({ detail: { code: "session_revoked" } }, 401))
      .mockResolvedValueOnce(response(authenticatedSession));

    render(
      <OperatorAuthGate>
        <ProtectedProbe />
      </OperatorAuthGate>,
    );

    expect(await screen.findByTestId("principal")).toHaveTextContent("operator:single");
    const mountToken = screen.getByTestId("mount-token").textContent;
    fireEvent.click(screen.getByRole("button", { name: "refresh session" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(screen.getByTestId("principal")).toHaveTextContent("operator:single");
    expect(screen.getByTestId("mount-token")).toHaveTextContent(mountToken ?? "");
    expect(screen.queryByLabelText("Operator password")).not.toBeInTheDocument();
  });

  it("treats the first reconciliation denial as tentative but the second as definitive", async () => {
    fetchMock
      .mockResolvedValueOnce(response(authenticatedSession))
      .mockResolvedValueOnce(response({ detail: { code: "session_revoked" } }, 401))
      .mockResolvedValueOnce(response({ detail: { code: "session_revoked" } }, 401))
      .mockResolvedValueOnce(response({ detail: { code: "session_revoked" } }, 401));

    render(
      <OperatorAuthGate>
        <ProtectedProbe />
      </OperatorAuthGate>,
    );

    expect(await screen.findByTestId("principal")).toHaveTextContent("operator:single");
    fireEvent.click(screen.getByRole("button", { name: "refresh session" }));
    expect(await screen.findByLabelText("Operator password")).toBeInTheDocument();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
    expect(screen.queryByTestId("principal")).not.toBeInTheDocument();
  });

  it("does not resurrect the protected owner when a refresh completes after explicit logout", async () => {
    const refresh = deferred<Response>();
    fetchMock
      .mockResolvedValueOnce(response(authenticatedSession))
      .mockReturnValueOnce(refresh.promise)
      .mockResolvedValueOnce(response(null, 204));

    render(
      <OperatorAuthGate>
        <ProtectedProbe />
      </OperatorAuthGate>,
    );

    expect(await screen.findByTestId("principal")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "refresh session" }));
    fireEvent.click(screen.getByRole("button", { name: "sign out" }));
    expect(await screen.findByLabelText("Operator password")).toBeInTheDocument();
    await act(async () => {
      refresh.resolve(response({ ...authenticatedSession, idle_expires_at: "2099-01-01T01:00:00Z" }));
      await refresh.promise;
    });
    expect(screen.queryByTestId("principal")).not.toBeInTheDocument();
  });

  it("ignores a stale setup result after a new login starts", async () => {
    const staleSetupRead = deferred<Response>();
    const newSession = { ...authenticatedSession, session_id: "new-owner-root" };
    fetchMock
      .mockResolvedValueOnce(response(authenticatedSession))
      .mockResolvedValueOnce(response({ detail: { code: "session_revoked" } }, 401))
      .mockReturnValueOnce(staleSetupRead.promise)
      .mockResolvedValueOnce(response(null, 204))
      .mockResolvedValueOnce(response({ authenticated: true, principal_id: "operator:single" }))
      .mockResolvedValueOnce(response(newSession));

    render(
      <OperatorAuthGate>
        <ProtectedProbe />
      </OperatorAuthGate>,
    );

    expect(await screen.findByTestId("session-id")).toHaveTextContent("stable-owner-root");
    fireEvent.click(screen.getByRole("button", { name: "refresh session" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));

    fireEvent.click(screen.getByRole("button", { name: "sign out" }));
    const password = await screen.findByLabelText("Operator password");
    fireEvent.change(password, { target: { value: "new password" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByTestId("session-id")).toHaveTextContent("new-owner-root");

    await act(async () => {
      staleSetupRead.resolve(response({ detail: { code: "auth_not_configured" } }, 503));
      await staleSetupRead.promise;
      await Promise.resolve();
    });
    expect(screen.getByTestId("session-id")).toHaveTextContent("new-owner-root");
    expect(screen.queryByText("Operator setup required")).not.toBeInTheDocument();
  });

  it("keeps global auth events fenced after explicit logout until a new login or inspect", async () => {
    const logoutResponse = deferred<Response>();
    fetchMock
      .mockResolvedValueOnce(response(authenticatedSession))
      .mockReturnValueOnce(logoutResponse.promise);

    render(
      <OperatorAuthGate>
        <ProtectedProbe />
      </OperatorAuthGate>,
    );

    expect(await screen.findByTestId("principal")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "sign out" }));
    expect(await screen.findByLabelText("Operator password")).toBeInTheDocument();

    await act(async () => {
      window.dispatchEvent(new Event(AUTH_REQUIRED_EVENT));
      await Promise.resolve();
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);

    await act(async () => {
      logoutResponse.resolve(response(null, 204));
      await logoutResponse.promise;
      await Promise.resolve();
    });
    await act(async () => {
      window.dispatchEvent(new Event(AUTH_REQUIRED_EVENT));
      await Promise.resolve();
    });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(screen.queryByTestId("principal")).not.toBeInTheDocument();
  });

  it("keeps the login view and reports an unconfirmed logout without claiming revocation", async () => {
    fetchMock
      .mockResolvedValueOnce(response(authenticatedSession))
      .mockResolvedValueOnce(response({ detail: { code: "session_unavailable" } }, 503));

    render(
      <OperatorAuthGate>
        <ProtectedProbe />
      </OperatorAuthGate>,
    );

    expect(await screen.findByTestId("principal")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "sign out" }));
    expect(await screen.findByLabelText("Operator password")).toBeInTheDocument();
    expect(await screen.findByRole("alert")).toHaveTextContent("Sign-out could not be confirmed");
    expect(screen.queryByText(/session is no longer valid/i)).not.toBeInTheDocument();
  });

  it("keeps the new root when a stale refresh finishes after a new login", async () => {
    const refresh = deferred<Response>();
    const newSession = { ...authenticatedSession, session_id: "new-owner-root" };
    fetchMock
      .mockResolvedValueOnce(response(authenticatedSession))
      .mockReturnValueOnce(refresh.promise)
      .mockResolvedValueOnce(response(null, 204))
      .mockResolvedValueOnce(response({ authenticated: true, principal_id: "operator:single" }))
      .mockResolvedValueOnce(response(newSession));

    render(
      <OperatorAuthGate>
        <ProtectedProbe />
      </OperatorAuthGate>,
    );

    expect(await screen.findByTestId("session-id")).toHaveTextContent("stable-owner-root");
    fireEvent.click(screen.getByRole("button", { name: "refresh session" }));
    fireEvent.click(screen.getByRole("button", { name: "sign out" }));
    const password = await screen.findByLabelText("Operator password");
    fireEvent.change(password, { target: { value: "new password" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByTestId("session-id")).toHaveTextContent("new-owner-root");

    // The stale refresh may have changed the browser cookie.  Reconciliation
    // is allowed only against the currently authenticated root and must keep
    // that root mounted.
    fetchMock.mockResolvedValue(response(newSession));
    await act(async () => {
      refresh.resolve(response(authenticatedSession));
      await refresh.promise;
    });
    expect(screen.getByTestId("session-id")).toHaveTextContent("new-owner-root");
  });

  it.each([
    ["a hung refresh request", () => new Promise<Response>(() => {})],
    ["a hung refresh response body", () => Promise.resolve(hangingBodyResponse())],
  ])("bounds %s and enters recovery without a stuck refresh promise", async (_label, refreshResponse) => {
    const refreshPromise = deferred<Promise<boolean>>();
    fetchMock
      .mockResolvedValueOnce(response(authenticatedSession))
      .mockReturnValueOnce(refreshResponse())
      .mockResolvedValue(response({ detail: { code: "session_unavailable" } }, 503));

    render(
      <OperatorAuthGate>
        <ProtectedProbe onRefreshPromise={(promise) => refreshPromise.resolve(promise)} />
      </OperatorAuthGate>,
    );

    expect(await screen.findByTestId("principal")).toBeInTheDocument();
    vi.useFakeTimers();
    fireEvent.click(screen.getByRole("button", { name: "refresh session" }));
    const result = refreshPromise.promise;
    await act(async () => {
      vi.advanceTimersByTime(5_000);
      await Promise.resolve();
    });
    await act(async () => {
      await Promise.resolve();
      vi.advanceTimersByTime(250);
      await Promise.resolve();
    });
    expect(await result).toBe(false);
    expect(screen.getByText("Backend unavailable")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it("clears mounted owner state when reconciliation proves a different root", async () => {
    fetchMock
      .mockResolvedValueOnce(response(authenticatedSession))
      .mockResolvedValueOnce(response({ detail: { code: "session_revoked" } }, 401))
      .mockResolvedValueOnce(response({
        ...authenticatedSession,
        session_id: "foreign-owner-root",
      }));

    render(
      <OperatorAuthGate>
        <ProtectedProbe />
      </OperatorAuthGate>,
    );

    expect(await screen.findByTestId("principal")).toHaveTextContent("operator:single");
    fireEvent.click(screen.getByRole("button", { name: "refresh session" }));
    expect(await screen.findByLabelText("Operator password")).toBeInTheDocument();
    expect(screen.queryByTestId("principal")).not.toBeInTheDocument();
  });

  it("coalesces refresh callers and bounds a delayed reconciliation to two reads", async () => {
    fetchMock
      .mockResolvedValueOnce(response(authenticatedSession))
      .mockResolvedValueOnce(response({ detail: { code: "session_revoked" } }, 401))
      .mockResolvedValueOnce(response({ detail: { code: "session_unavailable" } }, 503))
      .mockResolvedValueOnce(response(authenticatedSession));

    render(
      <OperatorAuthGate>
        <ProtectedProbe />
      </OperatorAuthGate>,
    );

    expect(await screen.findByTestId("principal")).toHaveTextContent("operator:single");
    fireEvent.click(screen.getByRole("button", { name: "refresh session" }));
    fireEvent.click(screen.getByRole("button", { name: "refresh session" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 300));
    });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
    expect(screen.getByTestId("principal")).toHaveTextContent("operator:single");
  });

  it("shows a persistent recovery notice for legacy ownership metadata", async () => {
    fetchMock.mockResolvedValue(response({
      ...authenticatedSession,
      ownership_continuity: "legacy_rebind_required",
      ownership_recovery_action: "review_and_recreate_work_in_current_scope",
    }));

    render(
      <OperatorAuthGate>
        <ProtectedProbe />
      </OperatorAuthGate>,
    );

    expect(await screen.findByRole("status")).toHaveTextContent("Older work remains stored but is blocked");
    expect(screen.getByTestId("principal")).toBeInTheDocument();
  });
});
