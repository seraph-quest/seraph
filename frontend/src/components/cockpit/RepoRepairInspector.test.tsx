import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RepoRepairInspector } from "./RepoRepairInspector";

function response(payload: unknown, ok = true, status = ok ? 200 : 409) {
  return { ok, status, json: async () => payload } as unknown as Response;
}

const packet = {
  packet_id: "packet-1",
  state: "ready",
  repository_ref: "repo",
  base_snapshot_sha256: "a".repeat(64),
  source_manifest_sha256: "b".repeat(64),
  artifact_sha256: "c".repeat(64),
  revision: 1,
};

function projection(overrides: Record<string, unknown> = {}) {
  return {
    job_id: "job-1",
    status: "paused",
    owner_principal_id: "operator:single",
    operator_session_id: "session-1",
    task_id: "task-1",
    attempt_id: "attempt-1",
    workflow_run_id: "job-1",
    goal_id: "goal-1",
    goal_revision: 1,
    revision: 4,
    authority_digest: "d".repeat(64),
    input_digest: "e".repeat(64),
    run_fingerprint: "f".repeat(64),
    capability_id: "engineering.repo-repair.v1",
    capability_version: "1",
    limits: { max_cpu_seconds: 120, max_memory_bytes: 512 * 1024 * 1024, max_pids: 64, max_wall_seconds: 180 },
    preflight: { ok: false, status: "blocked", reason: "resource_controller_unavailable:cpu" },
    source_packet: packet,
    egress: null,
    proposal: null,
    execution: { artifacts: [], readback: null, memory_status: "no_learning", provider_contacted: false },
    approval: null,
    approval_id: null,
    memory_status: "no_learning",
    recovery_action: "review_code_egress",
    operator_visible: true,
    ...overrides,
  };
}

describe("RepoRepairInspector", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
    window.sessionStorage.clear();
  });

  afterEach(() => {
    vi.useRealTimers();
    window.sessionStorage.clear();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("keeps source private until explicit inspection and submits exact consent", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock
      .mockResolvedValueOnce(response(projection()))
      .mockResolvedValueOnce(response({
        job_id: "job-1",
        status: "paused",
        recovery_action: "review_code_egress",
        source_packet: { ...packet, selected_files: [{ path: "src/app.py", size_bytes: 12, sha256: "1".repeat(64), text: "VALUE = 1" }], omissions: [] },
        egress: { runtime_path: "strategist_agent", effective_profile_id: "openrouter/repair", effective_upstream: "openrouter", maximum_input_bytes: 65536, maximum_output_tokens: 4096, expires_at: null },
        provider_contacted: false,
        operator_visible: true,
      }))
      .mockResolvedValueOnce(response({ ...projection(), status: "running", egress: { consent_id: "consent-1", revision: 1, runtime_path: "strategist_agent", effective_profile_id: "openrouter/repair", effective_upstream: "openrouter", maximum_input_bytes: 65536, maximum_output_tokens: 4096, expires_at: "2030-01-01T00:00:00Z", state: "active" } }));

    render(<RepoRepairInspector jobId="job-1" />);
    expect(await screen.findByText(/Private source stays local/)).toBeInTheDocument();
    expect(screen.queryByText("VALUE = 1")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Inspect selected source" }));
    expect(await screen.findByText("VALUE = 1")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Allow exact source packet" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/api/workflows/repo-repair/job-1/code-egress-consent"),
      expect.objectContaining({ method: "POST", body: expect.stringContaining('"acknowledged_selected_source":true') }),
    ));
    expect(await screen.findByText(/same durable root/)).toBeInTheDocument();
  });

  it("shows exact approval navigation and readback recovery without exposing content", async () => {
    const onOpenApprovals = vi.fn();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(projection({
      status: "awaiting_approval",
      source_packet: null,
      egress: { consent_id: "consent-1", revision: 1, runtime_path: "strategist_agent", effective_profile_id: "openrouter/repair", effective_upstream: "openrouter", maximum_input_bytes: 65536, maximum_output_tokens: 4096, expires_at: "2030-01-01T00:00:00Z", state: "active" },
      proposal: { proposal_id: "proposal-1", status: "awaiting_approval", revision: 2, base_snapshot_digest: "a".repeat(64), source_digest: "b".repeat(64), model_profile_id: "openrouter/repair", patch_sha256: "c".repeat(64), approval_id: "approval-1", expires_at: "2030-01-01T00:00:00Z", safe_metadata: {} },
      approval: { approval_id: "approval-1", status: "pending", tool_name: "engineering.repo-repair.v1", action: "repo_repair.resolve", expires_at: "2030-01-01T00:00:00Z" },
      recovery_action: "review_repo_repair_proposal",
    }))));

    render(<RepoRepairInspector jobId="job-1" onOpenApprovals={onOpenApprovals} />);
    expect(await screen.findByText(/Patch digest/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Review exact approval" }));
    expect(onOpenApprovals).toHaveBeenCalledTimes(1);
    expect(screen.getByText(/No verified readback receipt/)).toBeInTheDocument();
  });

  it("surfaces a deadline when a private preview fetch ignores abort", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.mocked(fetch);
    fetchMock
      .mockResolvedValueOnce(response(projection()))
      .mockImplementationOnce(() => new Promise<Response>(() => undefined));
    render(<RepoRepairInspector jobId="job-1" />);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByText(/Private source stays local/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Inspect selected source" }));

    await act(async () => {
      vi.advanceTimersByTime(15_000);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(screen.getByRole("alert")).toHaveTextContent(/timed out or was cancelled/i);
    expect(screen.getByRole("button", { name: "Inspect selected source" })).toBeEnabled();
  });

  it("surfaces a deadline when the private preview body parser ignores abort", async () => {
    vi.useFakeTimers();
    const fetchMock = vi.mocked(fetch);
    fetchMock
      .mockResolvedValueOnce(response(projection()))
      .mockResolvedValueOnce({
        ok: true,
        json: () => new Promise<unknown>(() => undefined),
      } as Response);
    render(<RepoRepairInspector jobId="job-1" />);
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    expect(screen.getByText(/Private source stays local/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Inspect selected source" }));

    await act(async () => {
      vi.advanceTimersByTime(15_000);
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(screen.getByRole("alert")).toHaveTextContent(/timed out or was cancelled/i);
    expect(screen.getByRole("button", { name: "Inspect selected source" })).toBeEnabled();
  });

  it("ignores a delayed private preview after the operator switches jobs", async () => {
    const fetchMock = vi.mocked(fetch);
    let resolvePreview: ((value: Response) => void) | undefined;
    fetchMock
      .mockResolvedValueOnce(response(projection()))
      .mockImplementationOnce(() => new Promise((resolve) => { resolvePreview = resolve; }))
      .mockResolvedValueOnce(response(projection({
        job_id: "job-2",
        workflow_run_id: "job-2",
        operator_session_id: "session-2",
        authority_digest: "9".repeat(64),
      })));

    const view = render(<RepoRepairInspector jobId="job-1" />);
    expect(await screen.findByText(/Private source stays local/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Inspect selected source" }));
    view.rerender(<RepoRepairInspector jobId="job-2" />);
    expect(await screen.findByText("job-2")).toBeInTheDocument();
    resolvePreview?.(response({
      job_id: "job-1",
      status: "paused",
      recovery_action: "review_code_egress",
      source_packet: { ...packet, selected_files: [{ path: "src/app.py", size_bytes: 12, sha256: "1".repeat(64), text: "STALE PRIVATE SOURCE" }], omissions: [] },
      egress: { runtime_path: "strategist_agent", effective_profile_id: "openrouter/repair", effective_upstream: "openrouter", maximum_input_bytes: 65536, maximum_output_tokens: 4096, expires_at: null },
      provider_contacted: false,
      operator_visible: true,
    }));
    await waitFor(() => expect(screen.queryByText("STALE PRIVATE SOURCE")).not.toBeInTheDocument());
  });

  it("reuses the exact consent key after an unknown response and remount", async () => {
    const fetchMock = vi.mocked(fetch);
    const source = {
      job_id: "job-1",
      status: "paused",
      recovery_action: "review_code_egress",
      source_packet: { ...packet, selected_files: [{ path: "src/app.py", size_bytes: 12, sha256: "1".repeat(64), text: "VALUE = 1" }], omissions: [] },
      egress: { runtime_path: "strategist_agent", effective_profile_id: "openrouter/repair", effective_upstream: "openrouter", maximum_input_bytes: 65536, maximum_output_tokens: 4096, expires_at: null },
      provider_contacted: false,
      operator_visible: true,
    };
    fetchMock
      .mockResolvedValueOnce(response(projection()))
      .mockResolvedValueOnce(response(source))
      .mockResolvedValueOnce(response({ detail: { code: "timeout" } }, false, 503));
    const first = render(<RepoRepairInspector jobId="job-1" />);
    await screen.findByText(/Private source stays local/);
    fireEvent.click(screen.getByRole("button", { name: "Inspect selected source" }));
    await screen.findByText("VALUE = 1");
    fireEvent.click(screen.getByRole("button", { name: "Allow exact source packet" }));
    await screen.findByRole("alert");
    first.unmount();

    fetchMock
      .mockResolvedValueOnce(response(projection()))
      .mockResolvedValueOnce(response(source))
      .mockResolvedValueOnce(response({ job_id: "job-1", status: "running" }))
      .mockResolvedValueOnce(response(projection({ status: "running", egress: { consent_id: "consent-1", revision: 1, runtime_path: "strategist_agent", effective_profile_id: "openrouter/repair", effective_upstream: "openrouter", maximum_input_bytes: 65536, maximum_output_tokens: 4096, expires_at: "2030-01-01T00:00:00Z", state: "active" } })));
    render(<RepoRepairInspector jobId="job-1" />);
    await screen.findByText(/Private source stays local/);
    fireEvent.click(screen.getByRole("button", { name: "Inspect selected source" }));
    await screen.findByText("VALUE = 1");
    fireEvent.click(screen.getByRole("button", { name: "Allow exact source packet" }));
    await waitFor(() => {
      const consentCalls = fetchMock.mock.calls.filter(([url, init]) => String(url).includes("code-egress-consent") && init?.method === "POST");
      expect(consentCalls).toHaveLength(2);
      const firstBody = JSON.parse(String(consentCalls[0][1]?.body));
      const secondBody = JSON.parse(String(consentCalls[1][1]?.body));
      expect(secondBody).toEqual(firstBody);
    });
  });
});
