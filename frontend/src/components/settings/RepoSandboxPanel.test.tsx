import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
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

const localPayload = {
  ...payload,
  executor_kind: "local",
  executor_profile: "local:repo-python-pytest-v1",
  executor_posture: {
    kind: "local",
    profile: "repo-python-pytest-v1",
    isolation_claim: "none",
    network_isolation: "not_verified",
    resource_enforcement: "admission_and_wall_timeout_only",
    image_digest: null,
    limits_digest: "a".repeat(64),
    local_host_execution_required: true,
  },
  executor_posture_digest: "b".repeat(64),
  local_host_approval_required: true,
  preparation_ready: true,
  execution_ready: false,
  legacy_repo_change_preflight: { ok: false, status: "blocked", reason: "rootless_prerequisite_missing" },
  enabled: true,
  preflight: { ok: true, status: "ready", reason: "local_staging_available" },
  status: "ready",
};

const managedLiveLocalPayload = {
  ...localPayload,
  executor_posture: {
    kind: "local",
    profile: "repo-python-pytest-v1",
    isolation_claim: "none",
    network_isolation: "not_verified",
    resource_enforcement: "admission_and_wall_timeout_only",
    host_access: "explicit_job_approval_required",
    limits_digest: "8e9bdc23d4ac6ba1b0cb8c214bc1fad4af24cf2b6ff61c11957fe2de1cdff4fb",
    worker_source_sha256: "b4274b18c1ed0e67c31c3300e996fda6769bb40c787576022e8ee924923472b6",
    interpreter_sha256: "0dc3a692fa85fcdb7f1a5877d2adf179809ac417a07ffde2373c832863800a15",
    pytest_executable_sha256: "0dc3a692fa85fcdb7f1a5877d2adf179809ac417a07ffde2373c832863800a15",
    pytest_package_sha256: "7be7a1e2218dc59a19d1ad131e4abe21172a295087efc72898938248782e8766",
  },
  executor_posture_digest: "9cc8184ce2062898d42e10984571c18ccc2d5892db5269a4c10aec27d00ba522",
  preflight: { ok: true, status: "ready", reason: "local_staging_available" },
  status: "blocked",
};

const dockerPayload = {
  ...payload,
  executor_kind: "docker_rootless",
  executor_profile: "docker_rootless:repo-python-pytest-v1",
  executor_posture: {
    kind: "docker_rootless",
    profile: "repo-python-pytest-v1",
    isolation_claim: "rootless_container",
    network_isolation: "none",
    resource_enforcement: "verified_fixed_limits",
    image_digest: "registry.example/seraph-worker@sha256:" + "c".repeat(64),
    limits_digest: "a".repeat(64),
    local_host_execution_required: false,
  },
  executor_posture_digest: "b".repeat(64),
  local_host_approval_required: false,
  preparation_ready: true,
  execution_ready: true,
  preflight: { ok: true, status: "ready", reason: "docker_ready" },
  status: "ready",
};

const rootfulPayload = {
  ...dockerPayload,
  executor_kind: "docker_rootful",
  executor_profile: "docker_rootful:repo-python-pytest-v1",
  executor_posture: {
    ...dockerPayload.executor_posture,
    kind: "docker_rootful",
    isolation_claim: "rootful_container",
  },
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

  it("keeps backend controls usable when React replays queued input updates", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValue(response(managedLiveLocalPayload));
    render(<StrictMode><RepoSandboxPanel /></StrictMode>);
    await screen.findByText(/Technical preflight: verified/);
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.change(screen.getByRole("combobox", { name: "Repository execution backend" }), { target: { value: "docker_rootful" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Docker socket" }), { target: { value: "unix:///tmp/test-docker.sock" } });
    fireEvent.change(screen.getByRole("textbox", { name: "Pinned worker image digest" }), { target: { value: "worker@sha256:" + "c".repeat(64) } });
    fireEvent.click(screen.getByRole("button", { name: "save selectors" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/api/settings/repo-sandbox"),
      expect.objectContaining({ method: "PUT" }),
    ));
    const saveRequest = fetchMock.mock.calls.find(([, options]) => options?.method === "PUT")?.[1];
    expect(JSON.parse(saveRequest?.body as string)).toEqual({ enabled: false, executor_kind: "docker_rootful", docker_socket: "unix:///tmp/test-docker.sock", worker_image_digest: "worker@sha256:" + "c".repeat(64), profile: "repo-python-pytest-v1" });
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

  it("selects the local backend and shows the host permission boundary without claiming isolation", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock
      .mockResolvedValueOnce(response(payload))
      .mockResolvedValueOnce(response(localPayload));
    render(<RepoSandboxPanel />);
    await screen.findByText(/Effective status: blocked/);
    fireEvent.change(screen.getByRole("combobox", { name: "Repository execution backend" }), { target: { value: "local" } });
    expect(screen.getByText(/Selected local posture is not effective until the server returns a complete receipt/)).toBeInTheDocument();
    expect(screen.getByText(/Effective status: unknown/)).toBeInTheDocument();
    expect(screen.queryByText(/No isolation guarantee/)).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "Docker socket" })).not.toBeInTheDocument();
    expect(screen.queryByText(/Configured ceilings:/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "save selectors" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/api/settings/repo-sandbox"),
      expect.objectContaining({ method: "PUT", body: expect.stringContaining('"executor_kind":"local"') }),
    ));
    expect(await screen.findByText(/awaiting exact per-job host approval/i)).toBeInTheDocument();
  });

  it("accepts the managed local API receipt whose posture uses canonical host access metadata", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(response(managedLiveLocalPayload));
    render(<RepoSandboxPanel />);
    expect(await screen.findByText(/Effective status: blocked/)).toBeInTheDocument();
    expect(screen.getByText(/Posture: isolation none · network not_verified/)).toBeInTheDocument();
    expect(screen.getByText(/No isolation guarantee/)).toBeInTheDocument();
    expect(screen.queryByText(/metadata was malformed/i)).not.toBeInTheDocument();
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

  it("retains the last known controls when a refresh returns malformed metadata", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock
      .mockResolvedValueOnce(response(payload))
      .mockResolvedValueOnce(response({ status: "blocked", limits: { max_cpu_seconds: "unknown" } }));
    render(<RepoSandboxPanel />);
    await screen.findByText(/Effective status: blocked/);
    fireEvent.click(screen.getByRole("button", { name: "refresh status" }));
    await waitFor(() => expect(screen.getByText(/last known controls are retained/i)).toBeInTheDocument());
    expect(screen.getByRole("textbox", { name: "Docker socket" })).toHaveValue("");
    expect(screen.getByText(/Posture: isolation unknown · network unknown/)).toBeInTheDocument();
    expect(screen.queryByText(/Effective status: ready/)).not.toBeInTheDocument();
    expect(screen.getByText(/Effective status: unknown/)).toBeInTheDocument();
  });

  it("rejects contradictory posture metadata while retaining the last known controls", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock
      .mockResolvedValueOnce(response(localPayload))
      .mockResolvedValueOnce(response({
        ...localPayload,
        executor_posture: { ...localPayload.executor_posture, isolation_claim: "full_host_isolation" },
        executor_posture_digest: "not-a-digest",
      }));
    render(<RepoSandboxPanel />);
    await screen.findByText(/Effective status: ready/);
    fireEvent.click(screen.getByRole("button", { name: "refresh status" }));
    await waitFor(() => expect(screen.getByText(/last known controls are retained/i)).toBeInTheDocument());
    expect(screen.getByText(/Posture: isolation unknown · network unknown/)).toBeInTheDocument();
    expect(screen.queryByText(/No isolation guarantee/)).not.toBeInTheDocument();
    expect(screen.getByText(/Effective status: unknown/)).toBeInTheDocument();
  });

  it("clears a previously ready receipt when a refresh fails", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock
      .mockResolvedValueOnce(response(localPayload))
      .mockResolvedValueOnce(response({ detail: "settings unavailable" }, false));
    render(<RepoSandboxPanel />);
    await screen.findByText(/Effective status: ready/);
    fireEvent.click(screen.getByRole("button", { name: "refresh status" }));
    await waitFor(() => expect(screen.getByText(/metadata is unavailable/i)).toBeInTheDocument());
    expect(screen.queryByText(/Effective status: ready/)).not.toBeInTheDocument();
    expect(screen.getByText(/Effective status: unknown/)).toBeInTheDocument();
    expect(screen.getByText(/Posture: isolation unknown · network unknown/)).toBeInTheDocument();
  });

  it("retains the last known controls when a save returns malformed metadata", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock
      .mockResolvedValueOnce(response(payload))
      .mockResolvedValueOnce(response({ enabled: true, profile: "repo-python-pytest-v1" }));
    render(<RepoSandboxPanel />);
    await screen.findByText(/Effective status: blocked/);
    fireEvent.click(screen.getByRole("button", { name: "save selectors" }));
    await waitFor(() => expect(screen.getByText(/last known controls are retained/i)).toBeInTheDocument());
    expect(screen.getByText(/Posture: isolation unknown · network unknown/)).toBeInTheDocument();
    expect(screen.queryByText(/Effective status: ready/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Repository sandbox settings saved/)).not.toBeInTheDocument();
  });

  it.each([
    ["local profile", { ...localPayload, executor_profile: undefined }],
    ["local posture digest", { ...localPayload, executor_posture_digest: null }],
    ["rootless posture", { ...dockerPayload, executor_posture: undefined }],
    ["rootful readiness", { ...dockerPayload, executor_kind: "docker_rootful", executor_profile: "docker_rootful:repo-python-pytest-v1", executor_posture: { ...dockerPayload.executor_posture, kind: "docker_rootful", isolation_claim: "rootful_container" }, preparation_ready: undefined }],
  ])("keeps explicit %s metadata unknown when its receipt is incomplete", async (_label, malformed) => {
    vi.mocked(fetch).mockResolvedValueOnce(response(malformed));
    render(<RepoSandboxPanel />);
    expect(await screen.findByText(/Effective status: unknown/)).toBeInTheDocument();
    expect(screen.getByText(/Posture: isolation unknown · network unknown/)).toBeInTheDocument();
    expect(screen.queryByText(/Preparation: ready/)).not.toBeInTheDocument();
  });

  it.each([
    ["local missing status", { ...localPayload, preflight: { ok: true } }],
    ["local malformed status", { ...localPayload, preflight: { ok: true, status: "future_status" } }],
    ["local contradictory status", { ...localPayload, preflight: { ok: false, status: "ready" } }],
    ["rootless missing status", { ...dockerPayload, preflight: { ok: true } }],
    ["rootless malformed status", { ...dockerPayload, preflight: { ok: true, status: "future_status" } }],
    ["rootless contradictory status", { ...dockerPayload, preflight: { ok: false, status: "ready" } }],
    ["rootful missing status", { ...rootfulPayload, preflight: { ok: true } }],
    ["rootful malformed status", { ...rootfulPayload, preflight: { ok: true, status: "future_status" } }],
    ["rootful contradictory status", { ...rootfulPayload, preflight: { ok: false, status: "ready" } }],
  ])("rejects explicit %s preflight metadata", async (_label, malformed) => {
    vi.mocked(fetch).mockResolvedValueOnce(response(malformed));
    render(<RepoSandboxPanel />);
    expect(await screen.findByText(/Effective status: unknown/)).toBeInTheDocument();
    expect(screen.getByText(/Posture: isolation unknown · network unknown/)).toBeInTheDocument();
    expect(screen.queryByText(/Technical preflight: verified/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Preparation: ready/)).not.toBeInTheDocument();
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
