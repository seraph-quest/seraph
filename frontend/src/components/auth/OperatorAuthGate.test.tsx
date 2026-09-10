import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
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

const authenticatedSession = {
  authenticated: true,
  principal_id: "operator:single",
  idle_expires_at: "2099-01-01T00:00:00Z",
  absolute_expires_at: "2099-01-02T00:00:00Z",
};

function ProtectedProbe() {
  const { session, refreshSession, logout } = useOperatorAuth();
  return (
    <div>
      <div data-testid="principal">{session.principal_id}</div>
      <button type="button" onClick={() => void refreshSession()}>refresh session</button>
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

  it("returns to login when the authenticated API or WebSocket reports revocation", async () => {
    fetchMock.mockResolvedValue(response(authenticatedSession));

    render(
      <OperatorAuthGate>
        <ProtectedProbe />
      </OperatorAuthGate>,
    );

    expect(await screen.findByTestId("principal")).toHaveTextContent("operator:single");
    await act(async () => {
      window.dispatchEvent(new Event(AUTH_REQUIRED_EVENT));
    });

    expect(await screen.findByLabelText("Operator password")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("no longer valid");
    expect(screen.queryByTestId("principal")).not.toBeInTheDocument();
  });
});
