import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AuthGate } from "./AuthGate";
import { authClient, AuthConfigurationError, AuthRequiredError } from "./authClient";
import { useAuthStore } from "./authStore";

describe("operator lock screen", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    useAuthStore.setState({ status: "bootstrapping", operatorName: null, revision: 0, error: null });
  });

  it("keeps anonymous visitors locked and unlocks after login", async () => {
    vi.spyOn(authClient, "session").mockRejectedValue(new AuthRequiredError());
    vi.spyOn(authClient, "login").mockResolvedValue({ authenticated: true, principal_id: "operator:single" });

    render(<AuthGate><p>PRIVATE COCKPIT</p></AuthGate>);

    expect(await screen.findByRole("heading", { name: "Operator access" })).toBeInTheDocument();
    expect(screen.queryByText("PRIVATE COCKPIT")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Passphrase"), { target: { value: "memory-only-secret" } });
    fireEvent.click(screen.getByRole("button", { name: "UNLOCK" }));
    expect(await screen.findByText("PRIVATE COCKPIT")).toBeInTheDocument();
  });

  it("restores an authenticated cockpit from the cookie session", async () => {
    vi.spyOn(authClient, "session").mockResolvedValue({ authenticated: true, principal_id: "operator:single" });

    render(<AuthGate><p>PRIVATE COCKPIT</p></AuthGate>);

    await waitFor(() => expect(screen.getByText("PRIVATE COCKPIT")).toBeInTheDocument());
    expect(screen.queryByLabelText("Passphrase")).not.toBeInTheDocument();
  });

  it("shows fail-closed setup guidance and can recover on retry", async () => {
    vi.spyOn(authClient, "session")
      .mockRejectedValueOnce(new AuthConfigurationError())
      .mockResolvedValueOnce({ authenticated: true, principal_id: "operator:single" });

    render(<AuthGate><p>PRIVATE COCKPIT</p></AuthGate>);

    expect(await screen.findByRole("heading", { name: "Operator access needs setup" })).toBeInTheDocument();
    expect(screen.queryByLabelText("Passphrase")).not.toBeInTheDocument();
    expect(screen.queryByText("PRIVATE COCKPIT")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "RETRY CONNECTION" }));
    expect(await screen.findByText("PRIVATE COCKPIT")).toBeInTheDocument();
  });
});
