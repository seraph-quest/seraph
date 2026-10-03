import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RepoRepairInspector } from "./RepoRepairInspector";
import nodePendingApi from "./__fixtures__/node-repair-pending-api.json";
// Actual authenticated recovery GETs; only host binary paths are normalized.
// Original DTOs and the exact-field correlation stay in private test evidence.
import nodeCleanupApi from "./__fixtures__/node-repair-cleanup-api.json";

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

  const cleanupProps = {
    jobId: nodeCleanupApi.held.job_id,
    ownerPrincipalId: nodeCleanupApi.held.owner_principal_id,
    ownerSessionId: nodeCleanupApi.held.operator_session_id,
    taskOwnerPrincipalId: nodeCleanupApi.held.owner_principal_id,
    taskOwnerSessionId: nodeCleanupApi.held.operator_session_id,
  };

  it("recovers physical cleanup explicitly and preserves Unknown through a full remount using actual API projections", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValueOnce(response(nodeCleanupApi.held))
      .mockResolvedValueOnce(response({ status: "unknown_external_effect", physical_capacity_released: true }))
      .mockResolvedValueOnce(response(nodeCleanupApi.released))
      .mockResolvedValueOnce(response(nodeCleanupApi.released));
    const mounted = render(<RepoRepairInspector {...cleanupProps} />);
    expect(await screen.findByText(/Physical capacity held/)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Recover process cleanup" }));
    expect(await screen.findByText(/Physical capacity released · durable cleanup/)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenNthCalledWith(2,
      expect.stringContaining(`/api/workflows/repo-change/${cleanupProps.jobId}/recover`), expect.objectContaining({ method: "POST" }));
    mounted.unmount();
    render(<RepoRepairInspector {...cleanupProps} />);
    expect(await screen.findByText(/Physical capacity released · durable cleanup/)).toBeInTheDocument();
    expect(screen.getByText(/Task, effect and cost liabilities remain Unknown/)).toBeInTheDocument();
    expect(screen.getByText(/unknown external effect · durable revision/)).toBeInTheDocument();
    expect(screen.getByText(/No verified readback receipt/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Recover process cleanup|Resume approved repair/ })).not.toBeInTheDocument();
    const writes = fetchMock.mock.calls.filter(([, options]) => options?.method === "POST");
    expect(writes).toHaveLength(1);
    expect(writes[0][0]).not.toContain("/resume");
  });

  it("refreshes durable GET after a lost recovery response without automatic replay", async () => {
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValueOnce(response(nodeCleanupApi.held))
      .mockRejectedValueOnce(new Error("response lost"))
      .mockResolvedValueOnce(response(nodeCleanupApi.held));
    const mounted = render(<RepoRepairInspector {...cleanupProps} />);
    fireEvent.click(await screen.findByRole("button", { name: "Recover process cleanup" }));
    expect(await screen.findByText(/Recovery response uncertain or rejected: response lost/)).toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(1);
    mounted.unmount();
    fetchMock.mockResolvedValueOnce(response(nodeCleanupApi.held));
    render(<RepoRepairInspector {...cleanupProps} />);
    expect(await screen.findByRole("button", { name: "Recover process cleanup" })).toBeEnabled();
    expect(fetchMock.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(1);
  });

  it("uses durable cleanup readback when the POST response was lost after settlement", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(response(nodeCleanupApi.held))
      .mockRejectedValueOnce(new Error("response lost"))
      .mockResolvedValueOnce(response(nodeCleanupApi.released));
    render(<RepoRepairInspector {...cleanupProps} />);
    fireEvent.click(await screen.findByRole("button", { name: "Recover process cleanup" }));
    expect(await screen.findByText(/Physical capacity released · durable cleanup/)).toBeInTheDocument();
    expect(screen.getByText(/Task, effect and cost liabilities remain Unknown/)).toBeInTheDocument();
  });

  it("does not trust a positive POST when durable cleanup remains held", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(response(nodeCleanupApi.held))
      .mockResolvedValueOnce(response({ status: "unknown_external_effect", physical_capacity_released: true }))
      .mockResolvedValueOnce(response(nodeCleanupApi.held));
    render(<RepoRepairInspector {...cleanupProps} />);
    fireEvent.click(await screen.findByRole("button", { name: "Recover process cleanup" }));
    expect(await screen.findByText(/Process cleanup has no durable verified release/)).toBeInTheDocument();
    expect(screen.queryByText(/Physical capacity released/)).not.toBeInTheDocument();
  });

  it("clears private cleanup data when recovery authority is expired or rejected", async () => {
    vi.mocked(fetch).mockResolvedValueOnce(response(nodeCleanupApi.held))
      .mockResolvedValueOnce(response({ detail: { code: "operator_session_expired" } }, false, 403))
      .mockResolvedValueOnce(response(nodeCleanupApi.held));
    render(<RepoRepairInspector {...cleanupProps} />);
    fireEvent.click(await screen.findByRole("button", { name: "Recover process cleanup" }));
    expect(await screen.findByText(/operator session is no longer authorized for this repair/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Recover process cleanup" })).not.toBeInTheDocument();
    expect(screen.queryByText(/Physical capacity held|Physical capacity released/)).not.toBeInTheDocument();
  });

  it("clears recovery data and ignores a late action response after owner rotation", async () => {
    let resolveAction!: (value: Response) => void;
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockResolvedValueOnce(response(nodeCleanupApi.held))
      .mockImplementationOnce(() => new Promise<Response>((resolve) => { resolveAction = resolve; }));
    const mounted = render(<RepoRepairInspector {...cleanupProps} />);
    fireEvent.click(await screen.findByRole("button", { name: "Recover process cleanup" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    mounted.rerender(<RepoRepairInspector {...cleanupProps} ownerSessionId="new-owner-root" />);
    await act(async () => resolveAction(response({ status: "unknown_external_effect", physical_capacity_released: true })));
    expect(screen.queryByText(/Physical capacity released|Physical capacity held/)).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it.each([
    { job_id: "wrong-job" }, { attempt_id: "wrong-attempt" }, { authority_digest: "f".repeat(64) },
    { fencing_token: 0 }, { fencing_token: "1" }, { process_cleanup_readback_sha256: "invalid" },
    { readback_scope: "artifact_verified" }, { cleanup_receipt_verified: false }, { supervisor_token: "private" },
  ])("rejects malformed or mismatched cleanup receipt metadata %j", async (invalid) => {
    const payload = structuredClone(nodeCleanupApi.released);
    Object.assign(payload.execution.process_cleanup, invalid);
    vi.mocked(fetch).mockResolvedValueOnce(response(payload));
    render(<RepoRepairInspector {...cleanupProps} />);
    expect(await screen.findByText(/process cleanup receipt is malformed/i)).toBeInTheDocument();
    expect(screen.queryByText(/Physical capacity released/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Recover process cleanup" })).not.toBeInTheDocument();
  });

  it("renders exact script and argv from the authenticated TypeScript API receipt", async () => {
    vi.mocked(fetch).mockResolvedValue(response(nodePendingApi));
    render(<RepoRepairInspector jobId={nodePendingApi.job_id} ownerPrincipalId={nodePendingApi.owner_principal_id} ownerSessionId={nodePendingApi.operator_session_id} taskOwnerPrincipalId={nodePendingApi.owner_principal_id} taskOwnerSessionId={nodePendingApi.operator_session_id} />);
    expect(await screen.findByLabelText("Reviewed Node execution inputs")).toBeInTheDocument();
    expect(screen.getByText("tsc --project tsconfig.json")).toBeInTheDocument();
    expect(screen.getByText(/Direct argv: .*node_modules\/typescript\/lib\/tsc.js --project tsconfig.json/)).toBeInTheDocument();
    expect(screen.getByText("node --test tests/app.test.js")).toBeInTheDocument();
    expect(screen.getByText(/CPU, memory and PID ceilings unenforced/)).toBeInTheDocument();
    expect(screen.getByText(/npm and pre\/post hooks are not executed/)).toBeInTheDocument();
  });

  it("renders recorded Node preflight blocked without claiming readiness", async () => {
    vi.mocked(fetch).mockResolvedValue(response({...nodePendingApi,preparation_ready:false,execution_ready:false,preflight:{status:"blocked",evidence_basis:"recorded_job_preflight"}}));
    render(<RepoRepairInspector jobId={nodePendingApi.job_id} ownerPrincipalId={nodePendingApi.owner_principal_id} ownerSessionId={nodePendingApi.operator_session_id} taskOwnerPrincipalId={nodePendingApi.owner_principal_id} taskOwnerSessionId={nodePendingApi.operator_session_id} />);
    expect(await screen.findByText("Recorded job preflight: blocked")).toBeInTheDocument();
    expect(screen.getByText("Preparation: blocked · execution: blocked")).toBeInTheDocument();
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

  it("shows the local host posture and exact approval boundary", async () => {
    const onOpenApprovals = vi.fn();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(projection({
      status: "awaiting_approval",
      source_packet: null,
      executor_kind: "local",
      executor_profile: "local:repo-python-pytest-v1",
      executor_posture: {
        kind: "local",
        profile: "repo-python-pytest-v1",
        isolation_claim: "none",
        network_isolation: "not_verified",
        resource_enforcement: "admission_and_wall_timeout_only",
        host_access: "explicit_job_approval_required",
        image_digest: null,
        limits_digest: "a".repeat(64),
      },
      executor_posture_digest: "9cc8184ce2062898d42e10984571c18ccc2d5892db5269a4c10aec27d00ba522",
      required_permissions: ["local_host_execution"],
      local_host_execution_required: true,
      preparation_ready: true,
      execution_ready: false,
      egress: { consent_id: "consent-1", revision: 1, runtime_path: "strategist_agent", effective_profile_id: "openrouter/repair", effective_upstream: "openrouter", maximum_input_bytes: 65536, maximum_output_tokens: 4096, expires_at: "2030-01-01T00:00:00Z", state: "active" },
      proposal: { proposal_id: "proposal-1", status: "awaiting_approval", revision: 2, base_snapshot_digest: "a".repeat(64), source_digest: "b".repeat(64), model_profile_id: "openrouter/repair", patch_sha256: "c".repeat(64), approval_id: "approval-1", expires_at: "2030-01-01T00:00:00Z", safe_metadata: {} },
      approval: { approval_id: "approval-1", status: "pending", tool_name: "engineering.repo-repair.v1", action: "repo_repair.resolve", expires_at: "2030-01-01T00:00:00Z" },
      recovery_action: "review_repo_repair_proposal",
    }))));

    render(<RepoRepairInspector {...inspectorProps} jobId="job-1" onOpenApprovals={onOpenApprovals} />);
    expect(await screen.findByText(/no isolation guarantee/i)).toBeInTheDocument();
    expect(screen.getByText(/awaiting exact host approval/i)).toBeInTheDocument();
    const approveButton = screen.getByRole("button", { name: "Approve local tests on this host" });
    expect(approveButton).toBeInTheDocument();
    fireEvent.click(approveButton);
    expect(onOpenApprovals).toHaveBeenCalledOnce();
  });

  it.each([
    ["local profile", {
      executor_kind: "local",
      executor_profile: undefined,
    }],
    ["local posture", {
      executor_kind: "local",
      executor_posture: undefined,
    }],
    ["local digest", {
      executor_kind: "local",
      executor_posture_digest: null,
    }],
    ["local readiness", {
      executor_kind: "local",
      preparation_ready: undefined,
    }],
    ["rootless posture", {
      executor_kind: "docker_rootless",
      executor_profile: "docker_rootless:repo-python-pytest-v1",
      executor_posture: undefined,
      executor_posture_digest: "b".repeat(64),
      required_permissions: [],
      local_host_execution_required: false,
      preparation_ready: true,
      execution_ready: true,
    }],
    ["rootful readiness", {
      executor_kind: "docker_rootful",
      executor_profile: "docker_rootful:repo-python-pytest-v1",
      executor_posture: {
        kind: "docker_rootful",
        profile: "repo-python-pytest-v1",
        isolation_claim: "rootful_container",
        network_isolation: "none",
        resource_enforcement: "verified_fixed_limits",
        image_digest: null,
        limits_digest: "a".repeat(64),
        local_host_execution_required: false,
      },
      executor_posture_digest: "b".repeat(64),
      required_permissions: [],
      local_host_execution_required: false,
      preparation_ready: undefined,
      execution_ready: true,
    }],
  ])("keeps incomplete explicit %s metadata from becoming an execution receipt", async (_label, metadata) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(projection(metadata))));
    render(<RepoRepairInspector {...inspectorProps} jobId="job-1" />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/repair status response/i);
    expect(alert).not.toHaveTextContent(/No isolation guarantee/i);
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
