import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RepoSandboxPanel } from "./RepoSandboxPanel";

const payload = {
  enabled: false,
  docker_socket: "",
  worker_image_digest: "",
  profile: "repo-python-pytest-v1",
  limits: { max_cpu_seconds: 120, max_memory_bytes: 512 * 1024 * 1024, max_pids: 64, max_wall_seconds: 180 },
  limits_digest: "a".repeat(64),
  limits_editable: false,
  preflight: { ok: false, status: "blocked", reason: "resource_controller_unavailable:cpu" },
  status: "blocked",
  configuration_error: null,
  operator_visible: true,
};

function response(value: unknown, ok = true) {
  return { ok, json: async () => value } as unknown as Response;
}

describe("RepoSandboxPanel", () => {
  beforeEach(() => vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(payload))));
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("shows fixed limits and truthful blocked preflight", async () => {
    render(<RepoSandboxPanel />);
    expect(await screen.findByText(/Effective status: blocked/)).toBeInTheDocument();
    expect(screen.getByText(/resource_controller_unavailable:cpu/)).toBeInTheDocument();
    expect(screen.getByText(/Limits are fixed and non-editable/)).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Profile" })).toHaveAttribute("readonly");
  });

  it("persists only typed selectors and never offers limit editing", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock
      .mockResolvedValueOnce(response(payload))
      .mockResolvedValueOnce(response(payload));
    render(<RepoSandboxPanel />);
    await screen.findByText(/Effective status: blocked/);
    fireEvent.click(screen.getByRole("button", { name: "save selectors" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/api/settings/repo-sandbox"),
      expect.objectContaining({ method: "PUT", body: expect.stringContaining('"profile":"repo-python-pytest-v1"') }),
    ));
    expect(screen.queryByRole("spinbutton")).not.toBeInTheDocument();
  });

  it("ignores a late refresh response after the panel remounts", async () => {
    const fetchMock = vi.mocked(fetch);
    let resolveLate: ((value: Response) => void) | undefined;
    fetchMock
      .mockResolvedValueOnce(response(payload))
      .mockImplementationOnce(() => new Promise((resolve) => { resolveLate = resolve; }));
    const first = render(<RepoSandboxPanel />);
    await screen.findByText(/Effective status: blocked/);
    fireEvent.click(screen.getByRole("button", { name: "refresh status" }));
    first.unmount();

    fetchMock.mockResolvedValueOnce(response({ ...payload, status: "fresh-state" }));
    render(<RepoSandboxPanel />);
    expect(await screen.findByText(/Effective status: fresh-state/)).toBeInTheDocument();
    resolveLate?.(response({ ...payload, status: "stale-state" }));
    await waitFor(() => {
      expect(screen.getByText(/Effective status: fresh-state/)).toBeInTheDocument();
      expect(screen.queryByText(/Effective status: stale-state/)).not.toBeInTheDocument();
    });
  });

  it("surfaces a deadline when the settings fetch ignores abort", async () => {
    vi.useFakeTimers();
    vi.mocked(fetch).mockImplementation(() => new Promise<Response>(() => undefined));
    render(<RepoSandboxPanel />);

    await act(async () => {
      vi.advanceTimersByTime(15_000);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(screen.getByText("The repository sandbox request timed out or was cancelled.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "refresh status" })).toBeEnabled();
  });

  it("surfaces a deadline when the settings body parser ignores abort", async () => {
    vi.useFakeTimers();
    vi.mocked(fetch).mockResolvedValue({
      ok: true,
      json: () => new Promise<unknown>(() => undefined),
    } as Response);
    render(<RepoSandboxPanel />);

    await act(async () => {
      vi.advanceTimersByTime(15_000);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(screen.getByText("The repository sandbox request timed out or was cancelled.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "refresh status" })).toBeEnabled();
  });
});
