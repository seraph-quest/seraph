import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { API_URL } from "../../config/constants";
import { TelegramPairingPanel } from "./TelegramPairingPanel";

function response(value: unknown, ok = true, status = 200) {
  return { ok, status, json: async () => value } as Response;
}
const unpaired = { configured: false, pairing_state: "unpaired", transport_mode: "injected_recording", live_transport: false };
const paired = {
  configured: true, pairing_state: "active", operator_id: 42, chat_id: 4242, token_fingerprint: "0123456789ab",
  consent: { telegram_transit: { state: "missing", expires_at: null } }, transport_mode: "injected_recording", live_transport: false,
};
afterEach(() => vi.unstubAllGlobals());

describe("TelegramPairingPanel", () => {
  it("pairs with a write-only token, then grants transit consent over the existing API", async () => {
    const sentinel = "123456:synthetic-bot-token";
    let state: Record<string, unknown> = unpaired;
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/telegram/pair")) state = paired;
      if (url.endsWith("/api/telegram/consent")) state = { ...paired, consent: { telegram_transit: { state: "active", expires_at: "2026-10-16T00:00:00Z" } } };
      return response(init?.method === "POST" ? { ok: true } : state);
    });
    vi.stubGlobal("fetch", fetch);
    render(<TelegramPairingPanel />);
    expect(await screen.findByText(/Live Telegram delivery is not shipped yet/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Grant transit consent" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Telegram user ID"), { target: { value: "42" } });
    fireEvent.change(screen.getByLabelText("Telegram chat ID"), { target: { value: "4242" } });
    fireEvent.change(screen.getByLabelText("Telegram bot token (write-only)"), { target: { value: sentinel } });
    fireEvent.click(screen.getByRole("button", { name: "Pair Telegram" }));
    expect(await screen.findByText(/fingerprint 0123456789ab/)).toBeInTheDocument();
    const pairCall = fetch.mock.calls.find(([url]) => String(url).endsWith("/api/telegram/pair"))!;
    expect(pairCall[0]).toBe(`${API_URL}/api/telegram/pair`);
    expect(JSON.parse(String(pairCall[1]?.body))).toEqual({ operator_id: 42, chat_id: 4242, bot_token: sentinel });
    expect(screen.getByLabelText("Telegram bot token (write-only)")).toHaveValue("");
    expect(screen.queryByText(sentinel)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Grant transit consent" }));
    expect(await screen.findByText(/transit consent active until 2026-10-16/)).toBeInTheDocument();
    const consent = fetch.mock.calls.find(([url]) => String(url).endsWith("/api/telegram/consent"))!;
    expect(JSON.parse(String(consent[1]?.body))).toEqual({ boundary: "telegram_transit" });
    await waitFor(() => expect(screen.getByRole("button", { name: "Revoke transit consent" })).toBeEnabled());
  });

  it("rejects non-numeric IDs locally and reports API error codes without losing status", async () => {
    const fetch = vi.fn(async (input: RequestInfo | URL) => String(input).endsWith("/status")
      ? response(paired)
      : response({ detail: { code: "telegram_authority_mismatch" } }, false, 403));
    vi.stubGlobal("fetch", fetch);
    render(<TelegramPairingPanel />);
    await screen.findByText(/fingerprint 0123456789ab/);
    fireEvent.click(screen.getByRole("button", { name: "Re-pair Telegram" }));
    expect(await screen.findByRole("status")).toHaveTextContent("Enter the numeric Telegram user ID and chat ID.");
    fireEvent.click(screen.getByRole("button", { name: "Grant transit consent" }));
    expect(await screen.findByText(/403 · telegram_authority_mismatch/)).toBeInTheDocument();
    expect(screen.getByText(/fingerprint 0123456789ab/)).toBeInTheDocument();
  });
});
