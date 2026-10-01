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

const inspectorProps = {
  ownerPrincipalId: "operator:single",
  ownerSessionId: "session-1",
  taskOwnerPrincipalId: "operator:single",
  taskOwnerSessionId: "session-1",
} as const;

function sourcePreviewPayload(text = "VALUE = 1", overrides: Record<string, unknown> = {}) {
  return {
    job_id: "job-1",
    status: "paused",
    recovery_action: "review_code_egress",
    source_packet: { ...packet, selected_files: [{ path: "src/app.py", size_bytes: text.length, sha256: "1".repeat(64), text }], omissions: [] },
    egress: { runtime_path: "strategist_agent", effective_profile_id: "openrouter/repair", effective_upstream: "openrouter", maximum_input_bytes: 65536, maximum_output_tokens: 4096, expires_at: null },
    provider_contacted: false,
    operator_visible: true,
    ...overrides,
  };
}

function consentReceipt(overrides: Record<string, unknown> = {}) {
  return {
    job_id: "job-1",
    status: "running",
    consent_id: "consent-1",
    consent_revision: 1,
    expires_at: "2030-01-01T00:00:00Z",
    recovery_action: "dispatcher_will_resume_same_root",
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

  it("does not request a repair projection without an exact current task owner binding", async () => {
    const fetchMock = vi.mocked(fetch);
    render(<RepoRepairInspector jobId="job-1" ownerPrincipalId="operator:single" ownerSessionId="session-1" taskOwnerPrincipalId="operator:other" taskOwnerSessionId="session-9" />);
    expect(await screen.findByText(/Select a repair owned by the current operator session/)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("keeps source private until explicit inspection and submits exact consent", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock
      .mockResolvedValueOnce(response(projection()))
      .mockResolvedValueOnce(response({
        job_id: "job-1",
        status: "paused",
        recovery_action: "review_code_egress",
        source_packet: { ...packet, selected_files: [{ path: "src/app.py", size_bytes: "VALUE = 1".length, sha256: "1".repeat(64), text: "VALUE = 1" }], omissions: [] },
        egress: { runtime_path: "strategist_agent", effective_profile_id: "openrouter/repair", effective_upstream: "openrouter", maximum_input_bytes: 65536, maximum_output_tokens: 4096, expires_at: null },
        provider_contacted: false,
        operator_visible: true,
      }))
      .mockResolvedValueOnce(response({ job_id: "job-1", status: "running", consent_id: "consent-1", consent_revision: 1, expires_at: "2030-01-01T00:00:00Z", recovery_action: "dispatcher_will_resume_same_root", operator_visible: true }))
      .mockResolvedValueOnce(response({ ...projection(), status: "running", egress: { consent_id: "consent-1", revision: 1, runtime_path: "strategist_agent", effective_profile_id: "openrouter/repair", effective_upstream: "openrouter", maximum_input_bytes: 65536, maximum_output_tokens: 4096, expires_at: "2030-01-01T00:00:00Z", state: "active" } }));

    render(<RepoRepairInspector {...inspectorProps} jobId="job-1" />);
    expect(await screen.findByText(/Private source stays local/)).toBeInTheDocument();
    expect(screen.queryByText("VALUE = 1")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Inspect selected source" }));
    expect(await screen.findByText("VALUE = 1")).toBeInTheDocument();
    expect(screen.getByText(/provider contact not recorded\./i)).toBeInTheDocument();
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

    render(<RepoRepairInspector {...inspectorProps} jobId="job-1" onOpenApprovals={onOpenApprovals} />);
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
    render(<RepoRepairInspector {...inspectorProps} jobId="job-1" />);
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
    render(<RepoRepairInspector {...inspectorProps} jobId="job-1" />);
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

    const view = render(<RepoRepairInspector {...inspectorProps} jobId="job-1" />);
    expect(await screen.findByText(/Private source stays local/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Inspect selected source" }));
    view.rerender(<RepoRepairInspector {...inspectorProps} jobId="job-2" taskOwnerSessionId="session-2" ownerSessionId="session-2" />);
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

  it("clears a private preview immediately when the owner binding rotates", async () => {
    const fetchMock = vi.mocked(fetch);
    let resolvePreview: ((value: Response) => void) | undefined;
    fetchMock
      .mockResolvedValueOnce(response(projection()))
      .mockResolvedValueOnce(response(sourcePreviewPayload()))
      .mockImplementationOnce(() => new Promise((resolve) => { resolvePreview = resolve; }))
      .mockResolvedValueOnce(response(projection({ operator_session_id: "session-2" })));
    const view = render(<RepoRepairInspector {...inspectorProps} jobId="job-1" />);
    await screen.findByText(/Private source stays local/);
    fireEvent.click(screen.getByRole("button", { name: "Inspect selected source" }));
    await screen.findByText("VALUE = 1");
    fireEvent.click(screen.getByRole("button", { name: "Inspect selected source" }));
    view.rerender(<RepoRepairInspector {...inspectorProps} jobId="job-1" ownerSessionId="session-2" taskOwnerSessionId="session-2" />);
    expect(screen.queryByText("VALUE = 1")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByText(/Private source stays local/)).toBeInTheDocument());
    resolvePreview?.(response(sourcePreviewPayload("STALE PRIVATE SOURCE")));
    await waitFor(() => expect(screen.queryByText("STALE PRIVATE SOURCE")).not.toBeInTheDocument());
  });

  it("clears private state on an unauthorized refresh instead of retaining the old projection", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock
      .mockResolvedValueOnce(response(projection()))
      .mockResolvedValueOnce(response({ detail: "forbidden" }, false, 403));
    render(<RepoRepairInspector {...inspectorProps} jobId="job-1" />);
    await screen.findByText(/Private source stays local/);
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(screen.queryByText(/Private source stays local/)).not.toBeInTheDocument());
    expect(screen.getByRole("alert")).toHaveTextContent(/no longer authorized/i);
  });

  it("retains the exact consent request and private preview when the receipt is malformed", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock
      .mockResolvedValueOnce(response(projection()))
      .mockResolvedValueOnce(response(sourcePreviewPayload()))
      .mockResolvedValueOnce(response({ job_id: "job-1", operator_visible: true }));
    render(<RepoRepairInspector {...inspectorProps} jobId="job-1" />);
    await screen.findByText(/Private source stays local/);
    fireEvent.click(screen.getByRole("button", { name: "Inspect selected source" }));
    await screen.findByText("VALUE = 1");
    fireEvent.click(screen.getByRole("button", { name: "Allow exact source packet" }));
    await waitFor(() => expect(screen.getByText(/exact request key is retained/i)).toBeInTheDocument());
    expect(screen.getByText("VALUE = 1")).toBeInTheDocument();
  });

  it("retains the exact consent request and private preview for a malformed expiry timestamp", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock
      .mockResolvedValueOnce(response(projection()))
      .mockResolvedValueOnce(response(sourcePreviewPayload()))
      .mockResolvedValueOnce(response(consentReceipt({ expires_at: "not-a-date" })));
    render(<RepoRepairInspector {...inspectorProps} jobId="job-1" />);
    await screen.findByText(/Private source stays local/);
    fireEvent.click(screen.getByRole("button", { name: "Inspect selected source" }));
    await screen.findByText("VALUE = 1");
    fireEvent.click(screen.getByRole("button", { name: "Allow exact source packet" }));
    await waitFor(() => expect(screen.getByText(/malformed or expired/i)).toBeInTheDocument());
    expect(screen.getByText("VALUE = 1")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("rejects a private source preview with a truthy non-boolean visibility flag", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock
      .mockResolvedValueOnce(response(projection()))
      .mockResolvedValueOnce(response({ ...sourcePreviewPayload(), operator_visible: "true" }));
    render(<RepoRepairInspector {...inspectorProps} jobId="job-1" />);
    await screen.findByText(/Private source stays local/);
    fireEvent.click(screen.getByRole("button", { name: "Inspect selected source" }));
    await waitFor(() => expect(screen.getByText(/did not match the current owner-bound packet/i)).toBeInTheDocument());
    expect(screen.queryByText("VALUE = 1")).not.toBeInTheDocument();
  });

  it("rejects a private source preview with a missing egress object", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock
      .mockResolvedValueOnce(response(projection()))
      .mockResolvedValueOnce(response(sourcePreviewPayload("VALUE = 1", { egress: null })));
    render(<RepoRepairInspector {...inspectorProps} jobId="job-1" />);
    await screen.findByText(/Private source stays local/);
    fireEvent.click(screen.getByRole("button", { name: "Inspect selected source" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/did not match the current owner-bound packet/i));
    expect(screen.queryByText("VALUE = 1")).not.toBeInTheDocument();
  });

  it("rejects a private source preview with a null selected file", async () => {
    const fetchMock = vi.mocked(fetch);
    const malformed = sourcePreviewPayload("VALUE = 1");
    fetchMock
      .mockResolvedValueOnce(response(projection()))
      .mockResolvedValueOnce(response({
        ...malformed,
        source_packet: { ...malformed.source_packet, selected_files: [null] },
      }));
    render(<RepoRepairInspector {...inspectorProps} jobId="job-1" />);
    await screen.findByText(/Private source stays local/);
    fireEvent.click(screen.getByRole("button", { name: "Inspect selected source" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/did not match the current owner-bound packet/i));
    expect(screen.queryByText("VALUE = 1")).not.toBeInTheDocument();
  });

  it("rejects a private source preview with an unsafe selected-file size", async () => {
    const fetchMock = vi.mocked(fetch);
    const malformed = sourcePreviewPayload("VALUE = 1");
    fetchMock
      .mockResolvedValueOnce(response(projection()))
      .mockResolvedValueOnce(response({
        ...malformed,
        source_packet: {
          ...malformed.source_packet,
          selected_files: [{ ...malformed.source_packet.selected_files[0], size_bytes: Number.NaN }],
        },
      }));
    render(<RepoRepairInspector {...inspectorProps} jobId="job-1" />);
    await screen.findByText(/Private source stays local/);
    fireEvent.click(screen.getByRole("button", { name: "Inspect selected source" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/did not match the current owner-bound packet/i));
    expect(screen.queryByText("VALUE = 1")).not.toBeInTheDocument();
  });

  it("rejects a private source preview with a non-boolean provider contact flag", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock
      .mockResolvedValueOnce(response(projection()))
      .mockResolvedValueOnce(response(sourcePreviewPayload("VALUE = 1", { provider_contacted: "true" })));
    render(<RepoRepairInspector {...inspectorProps} jobId="job-1" />);
    await screen.findByText(/Private source stays local/);
    fireEvent.click(screen.getByRole("button", { name: "Inspect selected source" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/did not match the current owner-bound packet/i));
    expect(screen.queryByText("VALUE = 1")).not.toBeInTheDocument();
  });

  it("shows recorded provider contact from the validated preview when a proposal exists", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock
      .mockResolvedValueOnce(response(projection({
        status: "awaiting_approval",
        proposal: {
          proposal_id: "proposal-1",
          status: "awaiting_approval",
          revision: 2,
          base_snapshot_digest: "a".repeat(64),
          source_digest: "b".repeat(64),
          model_profile_id: "openrouter/repair",
          patch_sha256: "c".repeat(64),
          approval_id: "approval-1",
          expires_at: "2030-01-01T00:00:00Z",
          safe_metadata: {},
        },
      })))
      .mockResolvedValueOnce(response(sourcePreviewPayload("VALUE = 1", { provider_contacted: true })));
    render(<RepoRepairInspector {...inspectorProps} jobId="job-1" />);
    await screen.findByText(/Private source stays local/);
    fireEvent.click(screen.getByRole("button", { name: "Inspect selected source" }));
    expect(await screen.findByText("VALUE = 1")).toBeInTheDocument();
    expect(screen.getByText(/provider contact recorded\./i)).toBeInTheDocument();
  });

  it("does not clear the preview when consent readback is mismatched", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock
      .mockResolvedValueOnce(response(projection()))
      .mockResolvedValueOnce(response(sourcePreviewPayload()))
      .mockResolvedValueOnce(response(consentReceipt()))
      .mockResolvedValueOnce(response(projection({ status: "running", egress: { consent_id: "other-consent", revision: 1, runtime_path: "strategist_agent", effective_profile_id: "openrouter/repair", effective_upstream: "openrouter", maximum_input_bytes: 65536, maximum_output_tokens: 4096, expires_at: "2030-01-01T00:00:00Z", state: "active" } })));
    render(<RepoRepairInspector {...inspectorProps} jobId="job-1" />);
    await screen.findByText(/Private source stays local/);
    fireEvent.click(screen.getByRole("button", { name: "Inspect selected source" }));
    await screen.findByText("VALUE = 1");
    fireEvent.click(screen.getByRole("button", { name: "Allow exact source packet" }));
    await waitFor(() => expect(screen.getByText(/readback did not match/i)).toBeInTheDocument());
    expect(screen.getByText("VALUE = 1")).toBeInTheDocument();
  });

  it("retains the preview when consent readback fails after the POST", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock
      .mockResolvedValueOnce(response(projection()))
      .mockResolvedValueOnce(response(sourcePreviewPayload()))
      .mockResolvedValueOnce(response(consentReceipt()))
      .mockResolvedValueOnce(response({ detail: "status unavailable" }, false, 503));
    render(<RepoRepairInspector {...inspectorProps} jobId="job-1" />);
    await screen.findByText(/Private source stays local/);
    fireEvent.click(screen.getByRole("button", { name: "Inspect selected source" }));
    await screen.findByText("VALUE = 1");
    fireEvent.click(screen.getByRole("button", { name: "Allow exact source packet" }));
    await waitFor(() => expect(screen.getByText(/readback did not match/i)).toBeInTheDocument());
    expect(screen.getByText("VALUE = 1")).toBeInTheDocument();
  });

  it("reuses the exact consent key after an unknown response and remount", async () => {
    const fetchMock = vi.mocked(fetch);
    const source = {
      job_id: "job-1",
      status: "paused",
      recovery_action: "review_code_egress",
      source_packet: { ...packet, selected_files: [{ path: "src/app.py", size_bytes: "VALUE = 1".length, sha256: "1".repeat(64), text: "VALUE = 1" }], omissions: [] },
      egress: { runtime_path: "strategist_agent", effective_profile_id: "openrouter/repair", effective_upstream: "openrouter", maximum_input_bytes: 65536, maximum_output_tokens: 4096, expires_at: null },
      provider_contacted: false,
      operator_visible: true,
    };
    fetchMock
      .mockResolvedValueOnce(response(projection()))
      .mockResolvedValueOnce(response(source))
      .mockResolvedValueOnce(response({ detail: { code: "timeout" } }, false, 503));
    const first = render(<RepoRepairInspector {...inspectorProps} jobId="job-1" />);
    await screen.findByText(/Private source stays local/);
    fireEvent.click(screen.getByRole("button", { name: "Inspect selected source" }));
    await screen.findByText("VALUE = 1");
    fireEvent.click(screen.getByRole("button", { name: "Allow exact source packet" }));
    await screen.findByRole("alert");
    first.unmount();

    fetchMock
      .mockResolvedValueOnce(response(projection()))
      .mockResolvedValueOnce(response(source))
      .mockResolvedValueOnce(response({ job_id: "job-1", status: "running", consent_id: "consent-1", consent_revision: 1, expires_at: "2030-01-01T00:00:00Z", recovery_action: "dispatcher_will_resume_same_root", operator_visible: true }))
      .mockResolvedValueOnce(response(projection({ status: "running", egress: { consent_id: "consent-1", revision: 1, runtime_path: "strategist_agent", effective_profile_id: "openrouter/repair", effective_upstream: "openrouter", maximum_input_bytes: 65536, maximum_output_tokens: 4096, expires_at: "2030-01-01T00:00:00Z", state: "active" } })));
    render(<RepoRepairInspector {...inspectorProps} jobId="job-1" />);
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
