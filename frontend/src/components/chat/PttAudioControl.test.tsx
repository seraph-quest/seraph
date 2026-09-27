import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { PttAudioControl } from "./PttAudioControl";

describe("PttAudioControl", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("keeps consent controls available before a conversation exists", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ reference: "capture-1", state: "active" }),
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<PttAudioControl sessionId={null} />);

    const captureConsent = screen.getByRole("checkbox", { name: "Allow microphone capture" });
    expect(captureConsent).toBeEnabled();
    expect(screen.getByRole("button", { name: "Hold to talk" })).toBeDisabled();

    fireEvent.click(captureConsent);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "/api/audio/ptt/consent",
      expect.objectContaining({ method: "POST" }),
    ));
    expect(screen.getByText("Choose or start a conversation before recording.")).toBeInTheDocument();
  });

  it("explains the LAN HTTP microphone limitation instead of failing silently", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ reference: "capture-1", state: "active" }),
    });
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("MediaRecorder", class {
      static isTypeSupported() {
        return true;
      }
    });
    Object.defineProperty(window, "isSecureContext", { configurable: true, value: false });

    render(<PttAudioControl sessionId="session-1" />);
    const captureConsent = screen.getByRole("checkbox", { name: "Allow microphone capture" });
    fireEvent.click(captureConsent);
    await waitFor(() => expect(captureConsent).toBeChecked());

    fireEvent.pointerDown(screen.getByRole("button", { name: "Hold to talk" }));
    expect(await screen.findByText(/requires HTTPS or localhost/)).toBeInTheDocument();
  });
});
