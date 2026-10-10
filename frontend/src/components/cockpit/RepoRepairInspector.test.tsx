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

  it("renders the recorded successful preflight with the actual ready status", async () => {
    // Actual managed publication-profile API preflight uses status=ready,
    // ok=true; status alone cannot establish successful recorded proof.
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(projection({
      executor_kind: "local",
      executor_profile: "local:repo-python-pytest-publication-v1",
      executor_posture: {
        kind: "local", profile: "repo-python-pytest-publication-v1",
        isolation_claim: "none", network_isolation: "not_verified",
        resource_enforcement: "admission_and_wall_timeout_only",
        host_access: "explicit_job_approval_required", image_digest: null,
        limits_digest: "a".repeat(64), runtime_proof_available: true,
        publication_runtime_proof_sha256: "b".repeat(64),
      },
      executor_posture_digest: "c".repeat(64),
      local_host_execution_required: true,
      required_permissions: ["local_host_execution"],
      preflight: { ok: true, status: "ready", reason: "local_staging_available" },
      preparation_ready: true,
      execution_ready: false,
    }))));
    render(<RepoRepairInspector {...inspectorProps} jobId="job-1" />);
    expect(await screen.findByText("Preflight: verified")).toBeInTheDocument();
    expect(screen.queryByText(/Preflight: blocked or unknown/)).not.toBeInTheDocument();
  });

  it("does not treat the ready status alone as verified preflight", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(projection({
      preflight: { ok: false, status: "ready", reason: "receipt_not_verified" },
      preparation_ready: false,
    }))));
    render(<RepoRepairInspector {...inspectorProps} jobId="job-1" />);
    expect(await screen.findByText("Preflight: blocked or unknown · receipt_not_verified")).toBeInTheDocument();
    expect(screen.queryByText("Preflight: verified")).not.toBeInTheDocument();
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

// Closed Source-specific metadata; these negative fixtures grant no runtime authority.
function repositorySourceStatus() {
  return {
    job_id: 'repository:source-test', status: 'running', revision: 6,
    repository_review: {native_child_id:'general-tool:source-test',repository_job_id:'repository:source-test',iteration_index:1,
      iteration_id:'a'.repeat(64),preparation_digest:'b'.repeat(64),contact_state:'not_started',source_preview_path:'/api/workflows/repo-repair/repository:source-test/source-preview'},
    source_recovery:null,patch_proposal:null,approval:null,iterations:[],iteration_states:[],recovery_action:'review_code_egress',provider_contacted:false,no_learning:true,operator_visible:true,
  };
}

describe('RepoRepairInspector original Source recovery readback', () => {
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); window.sessionStorage.clear(); });
  const recovery = { state: 'held_unknown', reason: 'original_completion_unproven', physical_hold: true,
    original_result: null, public_actions: 'unavailable' };
  it.each(['pending_original_producer', 'held_unknown', 'held_partial', 'continuation_ready',
    'original_cleanup_committed', 'original_stop_committed', 'physical_cleanup_only'])('renders owner state %s without enabling recovery', async (state) => {
    const fetch = vi.fn().mockResolvedValue(response({ ...repositorySourceStatus(), source_recovery: {
      ...recovery, state, physical_hold: state === 'physical_cleanup_only' ? false : true,
      original_result: state === 'held_partial' ? 'held_partial' : null,
    } }));
    vi.stubGlobal('fetch', fetch);
    render(<RepoRepairInspector {...inspectorProps} jobId='repository:source-test' />);
    expect(await screen.findByText(`Original recovery: ${state.replace(/_/g, ' ')}`)).toBeInTheDocument();
    expect(screen.getByText(`Original physical hold: ${state === 'physical_cleanup_only' ? 'released' : 'held'}`)).toBeInTheDocument();
    expect(screen.getByText(`Original result: ${state === 'held_partial' ? 'held partial' : 'unavailable'}`)).toBeInTheDocument();
    expect(screen.getByText(/Public Source recovery actions are unavailable/)).toBeInTheDocument();
    expect(screen.getByText(/no learning/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /recover|reconcile|settle|Try original cleanup/i })).not.toBeInTheDocument();
    if (state !== 'continuation_ready') expectNoRepositoryEffects();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh repair status' }));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
    expect(fetch.mock.calls.every(call => !call[1]?.method || call[1].method === 'GET')).toBe(true);
  });

  it('keeps unknown physical hold distinct from released capacity', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({ ...repositorySourceStatus(), source_recovery: { ...recovery, physical_hold: null } })));
    render(<RepoRepairInspector {...inspectorProps} jobId='repository:source-test' />);
    expect(await screen.findByText('Original physical hold: unknown')).toBeInTheDocument();
    expect(screen.queryByText('Original physical hold: released')).not.toBeInTheDocument();
  });

  it.each(['succeeded', 'failed'])('renders original %s without inferring it from cleanup', async (original_result) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({ ...repositorySourceStatus(), source_recovery: {
      ...recovery, state: 'original_cleanup_committed', physical_hold: false, original_result,
    } })));
    render(<RepoRepairInspector {...inspectorProps} jobId='repository:source-test' />);
    expect(await screen.findByText(`Original result: ${original_result}`)).toBeInTheDocument();
    expectNoRepositoryEffects();
  });

  it('clears held recovery readback after a foreign-job refresh', async () => {
    const fetch = vi.fn().mockResolvedValueOnce(response({ ...repositorySourceStatus(), source_recovery: recovery }))
      .mockResolvedValueOnce(response({ ...repositorySourceStatus(), job_id: 'repository:foreign', source_recovery: recovery }));
    vi.stubGlobal('fetch', fetch);
    render(<RepoRepairInspector {...inspectorProps} jobId='repository:source-test' />);
    expect(await screen.findByText('Original physical hold: held')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh repair status' }));
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.queryByLabelText('Original Source recovery readback')).not.toBeInTheDocument();
    expectNoRepositoryEffects();
  });

  it.each([
    ['unknown state', { ...recovery, state: 'recovered' }],
    ['extra private data', { ...recovery, private_path: '/private/key' }],
    ['unsafe reason', { ...recovery, reason: '/private/key' }],
    ['unbounded reason', { ...recovery, reason: 'a'.repeat(129) }],
    ['missing result', { state: recovery.state, reason: recovery.reason, physical_hold: true, public_actions: 'unavailable' }],
    ['untyped hold', { ...recovery, physical_hold: 'false' }],
    ['invented result', { ...recovery, original_result: 'verified' }],
    ['array result', { ...recovery, original_result: ['succeeded'] }],
    ['caller enablement', { ...recovery, public_actions: 'available' }],
    ['premature cleanup action', { ...recovery, public_actions: 'reconcile_original_cleanup' }],
  ])('rejects %s without retaining controls', async (_label, source_recovery) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({ ...repositorySourceStatus(), source_recovery })));
    render(<RepoRepairInspector {...inspectorProps} jobId='repository:source-test' />);
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expectNoRepositoryEffects();
    expect(screen.queryByLabelText('Original Source recovery readback')).not.toBeInTheDocument();
  });

  it('rejects a stale backend missing the required current projection field', async () => {
    const payload: Record<string, unknown> = repositorySourceStatus();
    delete payload.source_recovery;
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response(payload)));
    render(<RepoRepairInspector {...inspectorProps} jobId='repository:source-test' />);
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expectNoRepositoryEffects();
  });
});

describe('RepoRepairInspector current Source metadata boundary',()=>{
  beforeEach(()=>{vi.stubGlobal('fetch',vi.fn());window.sessionStorage.clear();});
  afterEach(()=>{vi.unstubAllGlobals();vi.restoreAllMocks();window.sessionStorage.clear();});
  it.each([
    ['foreign job',{job_id:'repository:foreign'}],
    ['private body',{private_source:'source must not reach metadata'}],
    ['provider contact string',{provider_contacted:'false'}],
    ['invisible source',{operator_visible:false}],
    ['missing no learning',{no_learning:false}],
    ['unpaired iteration metadata',{iterations:[{index:1,input_tree_digest:'a'.repeat(64),patch_digest:'b'.repeat(64),command_refs:['repository:execution:a'],result_artifacts:['repository:readback:a']}]}],
    ['foreign preview route',{repository_review:{...repositorySourceStatus().repository_review,source_preview_path:'https://foreign.invalid/private'}}],
    ['renewed iteration',{repository_review:{...repositorySourceStatus().repository_review,iteration_index:4}}],
    ['null stop',{repository_stop:null}],
    ['empty stop',{repository_stop:{}}],
    ['stop alias',{stop:{reason:'cost_exhausted',pending:true,limit_evidence:null,limit_evidence_digest:null}}],
    ['automatic stop without evidence',{repository_stop:{reason:'cost_exhausted',pending:true,limit_evidence:null,limit_evidence_digest:null}}],
    ['unprepared without stop',{repository_review:{...repositorySourceStatus().repository_review,contact_state:'not_prepared',iteration_index:null,iteration_id:null,preparation_digest:null,source_preview_path:null}}],
    ['mixed unprepared review',{repository_review:{...repositorySourceStatus().repository_review,contact_state:'not_prepared'}}],
  ])('rejects %s before any private read',async(_label,change)=>{
    const fetch=vi.fn().mockResolvedValue(response({...repositorySourceStatus(),...change}));vi.stubGlobal('fetch',fetch);
    render(<RepoRepairInspector {...inspectorProps} jobId='repository:source-test'/>);
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.queryByText('Inspect exact source and diagnostics')).not.toBeInTheDocument();
    expect(fetch).toHaveBeenCalledOnce();
  });
  it('clears current Source metadata during operator rotation before late replies',async()=>{
    const fetch=vi.fn().mockResolvedValueOnce(response(repositorySourceStatus())).mockImplementationOnce(()=>new Promise(()=>undefined));vi.stubGlobal('fetch',fetch);
    const view=render(<RepoRepairInspector {...inspectorProps} jobId='repository:source-test'/>);
    expect(await screen.findByText('Inspect exact source and diagnostics')).toBeInTheDocument();
    view.rerender(<RepoRepairInspector {...inspectorProps} ownerSessionId='new-session' taskOwnerSessionId='new-session' jobId='repository:source-test'/>);
    expect(screen.queryByText('Inspect exact source and diagnostics')).not.toBeInTheDocument();
  });
  it.each([
    ['malformed stop',{...repositorySourceStatus(),repository_stop:null}],
    ['copied native binding',{...repositorySourceStatus(),repository_review:{...repositorySourceStatus().repository_review,native_child_id:'general-tool:foreign'}}],
    ['older revision',{...repositorySourceStatus(),revision:5}],
  ])('clears retained Source controls after %s refresh',async(_label,payload)=>{
    const fetch=vi.fn().mockResolvedValueOnce(response(repositorySourceStatus())).mockResolvedValueOnce(response(payload));vi.stubGlobal('fetch',fetch);
    render(<RepoRepairInspector {...inspectorProps} jobId='repository:source-test'/>);
    expect(await screen.findByText('Inspect exact source and diagnostics')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button',{name:'Refresh repair status'}));
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.queryByText('Inspect exact source and diagnostics')).not.toBeInTheDocument();
    expect(screen.queryByText('Allow this iteration\'s source and diagnostics')).not.toBeInTheDocument();
    expect(screen.queryByText('Execute this approved patch')).not.toBeInTheDocument();
    expect(fetch).toHaveBeenCalledTimes(2);
  });
});

// Unmodified actual R199 and R209 authenticated Source/Task GET capture bytes.
// Provenance: 1009-STOP-METADATA-HANDOFF-R202.md; raw archive inventory84d977.
// Full owner/Task/401/403 receipts are retained; no normalized or invented successes.
// R209 Unknown Pending: actual-auth-stop-unknown_pending.json, SHA 6bc22abf011c28a27d61018d7282ad13333d9d351a5d1055b10e43f3020b9086.
const repositorySourceCaptureBytes = [
  { state: "pending", sha256: "797e347376229b3feec8e5bef2aa0c86363e9d3a02ee22eb0aa6dd2dfab20f0f", raw: "{\"captures\": [{\"method\": \"GET\", \"path\": \"/api/workflows/repo-repair/repository:a1be74699f85a3f3e7ea42029c2d98a74815b47eff5024c64431ec905d4f7d27\", \"response\": {\"detail\": {\"code\": \"authentication_required\"}}, \"status\": 401}, {\"method\": \"GET\", \"path\": \"/api/workflows/repo-repair/repository:a1be74699f85a3f3e7ea42029c2d98a74815b47eff5024c64431ec905d4f7d27\", \"response\": {\"approval\": null, \"iteration_states\": [], \"iterations\": [], \"job_id\": \"repository:a1be74699f85a3f3e7ea42029c2d98a74815b47eff5024c64431ec905d4f7d27\", \"no_learning\": true, \"operator_visible\": true, \"patch_proposal\": null, \"provider_contacted\": false, \"recovery_action\": \"repository_stop_pending\", \"repository_review\": {\"contact_state\": \"not_prepared\", \"iteration_id\": null, \"iteration_index\": null, \"native_child_id\": \"general-tool:38bc39173a336a25b0a798770c983fa810ecbf05da5c53d7\", \"preparation_digest\": null, \"repository_job_id\": \"repository:a1be74699f85a3f3e7ea42029c2d98a74815b47eff5024c64431ec905d4f7d27\", \"source_preview_path\": null}, \"repository_stop\": {\"limit_evidence\": {\"cause\": \"cost_exhausted\", \"goal_cutoff_at\": \"2026-10-09T11:53:52.015635Z\", \"group_calls\": 0, \"group_liability_microusd\": 0, \"original_deadline_at\": \"2026-10-09T11:43:52.156623Z\", \"original_group_max_calls\": 5, \"original_group_max_cost_microusd\": 500, \"original_limits_digest\": \"0fdfe67e2fd2c329acc94d843ee6bf997abb4d710a0d7936af44b013fc19dcd2\", \"original_root_max_cost_microusd\": 0, \"original_server_bound_microusd\": 100, \"root_liability_microusd\": 0, \"schema_version\": \"repository.native_limit_evidence.v1\"}, \"limit_evidence_digest\": \"4c8a66c5f83088bc2d81fde46633ef033b2559af028d222e7b4ad9c7414a01f5\", \"pending\": true, \"reason\": \"cost_exhausted\"}, \"revision\": 4, \"status\": \"running\"}, \"status\": 200}, {\"method\": \"GET\", \"path\": \"/api/work-board/tasks/57b2a3e196384e58936c4e8679386b6c\", \"response\": {\"attempts\": [{\"attempt_id\": \"ab83b504254a42d39d759a835adacd81\", \"cancel_requested_at\": null, \"created_at\": \"2026-10-09T11:38:53.347470Z\", \"ended_at\": null, \"executor_id\": \"seraph-work-board:agent.task.v1\", \"fencing_token\": 1, \"heartbeat_at\": \"2026-10-09T11:38:53.823032Z\", \"lease_expires_at\": null, \"lease_owner\": null, \"outcome\": null, \"readback_status\": \"pending\", \"receipt_refs\": [], \"started_at\": \"2026-10-09T11:38:52.156623Z\", \"task_id\": \"57b2a3e196384e58936c4e8679386b6c\", \"task_revision_at_claim\": 2, \"updated_at\": \"2026-10-09T11:38:53.823032Z\", \"verification_status\": \"pending\", \"workflow_run_id\": \"work-board:57b2a3e196384e58936c4e8679386b6c:ab83b504254a42d39d759a835adacd81\"}], \"children\": [], \"comments\": [], \"events\": [{\"created_at\": \"2026-10-09T11:38:52.129741Z\", \"event_id\": 1, \"kind\": \"task.created\", \"metadata\": {\"status\": \"todo\", \"task_revision\": 1}, \"task_id\": \"57b2a3e196384e58936c4e8679386b6c\"}, {\"created_at\": \"2026-10-09T11:38:52.155074Z\", \"event_id\": 2, \"kind\": \"task.ready\", \"metadata\": {\"status\": \"ready\", \"task_revision\": 2}, \"task_id\": \"57b2a3e196384e58936c4e8679386b6c\"}, {\"created_at\": \"2026-10-09T11:38:53.353360Z\", \"event_id\": 3, \"kind\": \"task.claimed\", \"metadata\": {\"attempt_id\": \"ab83b504254a42d39d759a835adacd81\", \"status\": \"running\", \"task_revision\": 3}, \"task_id\": \"57b2a3e196384e58936c4e8679386b6c\"}, {\"created_at\": \"2026-10-09T11:38:53.383356Z\", \"event_id\": 4, \"kind\": \"attempt.linked\", \"metadata\": {\"attempt_id\": \"ab83b504254a42d39d759a835adacd81\", \"task_revision\": 4, \"workflow_run_id\": \"work-board:57b2a3e196384e58936c4e8679386b6c:ab83b504254a42d39d759a835adacd81\"}, \"task_id\": \"57b2a3e196384e58936c4e8679386b6c\"}, {\"created_at\": \"2026-10-09T11:38:54.342642Z\", \"event_id\": 9, \"kind\": \"attempt.repository_stop_requested\", \"metadata\": {\"attempt_id\": \"ab83b504254a42d39d759a835adacd81\"}, \"task_id\": \"57b2a3e196384e58936c4e8679386b6c\"}], \"parent_handoffs\": [], \"parents\": [], \"revision\": 5, \"task\": {\"archived_at\": null, \"artifact_refs\": [], \"assignee_id\": null, \"block_kind\": \"needs_input\", \"block_reason\": \"general_task_native_wait\", \"block_source_status\": \"running\", \"body\": \"General registered-tool task\", \"cancel_requested_at\": null, \"capability_id\": \"agent.task.v1\", \"completed_at\": null, \"completed_dependency_count\": 0, \"created_at\": \"2026-10-09T11:38:52.125250Z\", \"creation_sequence\": 1, \"dependency_count\": 0, \"dispatch_rank\": null, \"dispatch_wait_reason\": null, \"executor_id\": \"seraph-work-board:agent.task.v1\", \"goal_id\": \"goal:fixture\", \"goal_revision\": 1, \"idempotency_key\": \"source-original\", \"idempotency_scope\": \"general-task\", \"input_artifact_id\": \"c79ea9e0-a78e-5607-85dc-41ae9382c59c\", \"latest_attempt\": {\"attempt_id\": \"ab83b504254a42d39d759a835adacd81\", \"cancel_requested_at\": null, \"created_at\": \"2026-10-09T11:38:53.347470Z\", \"ended_at\": null, \"executor_id\": \"seraph-work-board:agent.task.v1\", \"fencing_token\": 1, \"heartbeat_at\": \"2026-10-09T11:38:53.823032Z\", \"lease_expires_at\": null, \"lease_owner\": null, \"outcome\": null, \"readback_status\": \"pending\", \"receipt_refs\": [], \"started_at\": \"2026-10-09T11:38:52.156623Z\", \"task_id\": \"57b2a3e196384e58936c4e8679386b6c\", \"task_revision_at_claim\": 2, \"updated_at\": \"2026-10-09T11:38:53.823032Z\", \"verification_status\": \"pending\", \"workflow_run_id\": \"work-board:57b2a3e196384e58936c4e8679386b6c:ab83b504254a42d39d759a835adacd81\"}, \"origin_session_id\": \"d9ee68d0a1c74fb39da483e0e7bfacab\", \"origin_thread_id\": null, \"owner_principal_id\": \"operator:root:c688d29179ce4d62b979bbbb5c8a8a3b\", \"owner_session_id\": \"d9ee68d0a1c74fb39da483e0e7bfacab\", \"pipeline_operation_id\": null, \"pipeline_slot\": null, \"priority\": 50, \"readback_status\": \"pending\", \"recovery_action\": \"approve_existing_run\", \"repository_review\": {\"contact_state\": \"not_prepared\", \"iteration_id\": null, \"iteration_index\": null, \"native_child_id\": \"general-tool:38bc39173a336a25b0a798770c983fa810ecbf05da5c53d7\", \"preparation_digest\": null, \"repository_job_id\": \"repository:a1be74699f85a3f3e7ea42029c2d98a74815b47eff5024c64431ec905d4f7d27\", \"source_preview_path\": null}, \"requires_review\": true, \"result_refs\": [], \"review_expires_at\": null, \"reviewer_id\": \"operator:root:c688d29179ce4d62b979bbbb5c8a8a3b\", \"scheduled_at\": null, \"status\": \"blocked\", \"task_id\": \"57b2a3e196384e58936c4e8679386b6c\", \"task_revision\": 5, \"title\": \"Repair the selected repository\", \"typed_input_digest\": \"6dcbbd706840cea16d070f90ef60ab2f4d676e8b6265bef09fe0c1e36097f7f9\", \"typed_input_ref\": \"workspace-json:artifacts/work-board/inputs/c79ea9e0-a78e-5607-85dc-41ae9382c59c-6dcbbd706840cea16d070f90ef60ab2f4d676e8b6265bef09fe0c1e36097f7f9.json\", \"updated_at\": \"2026-10-09T11:38:53.823032Z\", \"verification_status\": \"pending\"}}, \"status\": 200}, {\"method\": \"GET\", \"path\": \"/api/workflows/repo-repair/repository:a1be74699f85a3f3e7ea42029c2d98a74815b47eff5024c64431ec905d4f7d27\", \"response\": {\"detail\": {\"code\": \"repo_repair_owner_mismatch\"}}, \"status\": 403}], \"owner\": {\"principal_id\": \"operator:root:c688d29179ce4d62b979bbbb5c8a8a3b\", \"session_id\": \"d9ee68d0a1c74fb39da483e0e7bfacab\"}}" },
  { state: "terminal", sha256: "12f059be07c6dd1b2d242d2653d008ab42598fb726512f9b919b115925583fb1", raw: "{\"captures\": [{\"method\": \"GET\", \"path\": \"/api/workflows/repo-repair/repository:7f193adfd34899ddb55c9e2c0ce9d7dde2d9ade3ba763cd5d135dafd4a11bbf6\", \"response\": {\"detail\": {\"code\": \"authentication_required\"}}, \"status\": 401}, {\"method\": \"GET\", \"path\": \"/api/workflows/repo-repair/repository:7f193adfd34899ddb55c9e2c0ce9d7dde2d9ade3ba763cd5d135dafd4a11bbf6\", \"response\": {\"approval\": null, \"iteration_states\": [], \"iterations\": [], \"job_id\": \"repository:7f193adfd34899ddb55c9e2c0ce9d7dde2d9ade3ba763cd5d135dafd4a11bbf6\", \"no_learning\": true, \"operator_visible\": true, \"patch_proposal\": null, \"provider_contacted\": false, \"recovery_action\": \"original_cost_exhausted\", \"repository_review\": {\"contact_state\": \"not_prepared\", \"iteration_id\": null, \"iteration_index\": null, \"native_child_id\": \"general-tool:eb8f2d65e37c6821e693e7d9574d86d996fc37c5a57365e7\", \"preparation_digest\": null, \"repository_job_id\": \"repository:7f193adfd34899ddb55c9e2c0ce9d7dde2d9ade3ba763cd5d135dafd4a11bbf6\", \"source_preview_path\": null}, \"repository_stop\": {\"limit_evidence\": {\"cause\": \"cost_exhausted\", \"goal_cutoff_at\": \"2026-10-09T11:53:57.079427Z\", \"group_calls\": 0, \"group_liability_microusd\": 0, \"original_deadline_at\": \"2026-10-09T11:43:57.193959Z\", \"original_group_max_calls\": 5, \"original_group_max_cost_microusd\": 500, \"original_limits_digest\": \"612a0a068c8950ebb95de7170555c9d08580cea9e219cc82f8e11ae4fc4ba54a\", \"original_root_max_cost_microusd\": 0, \"original_server_bound_microusd\": 100, \"root_liability_microusd\": 0, \"schema_version\": \"repository.native_limit_evidence.v1\"}, \"limit_evidence_digest\": \"5ee9666106a2ed1faa8f7771000c8b9af8bbf11aba2e2bea0cda8654d4e06976\", \"pending\": false, \"reason\": \"cost_exhausted\"}, \"revision\": 5, \"status\": \"failed\"}, \"status\": 200}, {\"method\": \"GET\", \"path\": \"/api/work-board/tasks/2061228f2c604aa7b74fa1d433104b31\", \"response\": {\"attempts\": [{\"attempt_id\": \"af247160c2a2435691d9a309728e7183\", \"cancel_requested_at\": \"2026-10-09T11:38:57.824609Z\", \"created_at\": \"2026-10-09T11:38:57.207926Z\", \"ended_at\": \"2026-10-09T11:38:57.824609Z\", \"executor_id\": \"seraph-work-board:agent.task.v1\", \"fencing_token\": 2, \"heartbeat_at\": \"2026-10-09T11:38:57.343685Z\", \"lease_expires_at\": null, \"lease_owner\": null, \"outcome\": \"cancelled\", \"readback_status\": \"not_applicable\", \"receipt_refs\": [], \"started_at\": \"2026-10-09T11:38:57.193959Z\", \"task_id\": \"2061228f2c604aa7b74fa1d433104b31\", \"task_revision_at_claim\": 2, \"updated_at\": \"2026-10-09T11:38:57.824609Z\", \"verification_status\": \"cancelled\", \"workflow_run_id\": \"work-board:2061228f2c604aa7b74fa1d433104b31:af247160c2a2435691d9a309728e7183\"}], \"children\": [], \"comments\": [], \"events\": [{\"created_at\": \"2026-10-09T11:38:57.172025Z\", \"event_id\": 1, \"kind\": \"task.created\", \"metadata\": {\"status\": \"todo\", \"task_revision\": 1}, \"task_id\": \"2061228f2c604aa7b74fa1d433104b31\"}, {\"created_at\": \"2026-10-09T11:38:57.192653Z\", \"event_id\": 2, \"kind\": \"task.ready\", \"metadata\": {\"status\": \"ready\", \"task_revision\": 2}, \"task_id\": \"2061228f2c604aa7b74fa1d433104b31\"}, {\"created_at\": \"2026-10-09T11:38:57.212197Z\", \"event_id\": 3, \"kind\": \"task.claimed\", \"metadata\": {\"attempt_id\": \"af247160c2a2435691d9a309728e7183\", \"status\": \"running\", \"task_revision\": 3}, \"task_id\": \"2061228f2c604aa7b74fa1d433104b31\"}, {\"created_at\": \"2026-10-09T11:38:57.235646Z\", \"event_id\": 4, \"kind\": \"attempt.linked\", \"metadata\": {\"attempt_id\": \"af247160c2a2435691d9a309728e7183\", \"task_revision\": 4, \"workflow_run_id\": \"work-board:2061228f2c604aa7b74fa1d433104b31:af247160c2a2435691d9a309728e7183\"}, \"task_id\": \"2061228f2c604aa7b74fa1d433104b31\"}, {\"created_at\": \"2026-10-09T11:38:57.744661Z\", \"event_id\": 9, \"kind\": \"attempt.repository_stop_requested\", \"metadata\": {\"attempt_id\": \"af247160c2a2435691d9a309728e7183\"}, \"task_id\": \"2061228f2c604aa7b74fa1d433104b31\"}, {\"created_at\": \"2026-10-09T11:38:57.858646Z\", \"event_id\": 12, \"kind\": \"attempt.cancel_requested\", \"metadata\": {\"attempt_id\": \"af247160c2a2435691d9a309728e7183\", \"workflow_run_id\": \"work-board:2061228f2c604aa7b74fa1d433104b31:af247160c2a2435691d9a309728e7183\"}, \"task_id\": \"2061228f2c604aa7b74fa1d433104b31\"}], \"parent_handoffs\": [], \"parents\": [], \"revision\": 6, \"task\": {\"archived_at\": null, \"artifact_refs\": [], \"assignee_id\": null, \"block_kind\": \"needs_input\", \"block_reason\": \"general_task_native_cancel_fully_cancelled\", \"block_source_status\": \"running\", \"body\": \"General registered-tool task\", \"cancel_requested_at\": \"2026-10-09T11:38:57.824609Z\", \"capability_id\": \"agent.task.v1\", \"completed_at\": null, \"completed_dependency_count\": 0, \"created_at\": \"2026-10-09T11:38:57.167657Z\", \"creation_sequence\": 1, \"dependency_count\": 0, \"dispatch_rank\": null, \"dispatch_wait_reason\": null, \"executor_id\": \"seraph-work-board:agent.task.v1\", \"goal_id\": \"goal:fixture\", \"goal_revision\": 1, \"idempotency_key\": \"source-original\", \"idempotency_scope\": \"general-task\", \"input_artifact_id\": \"f9d2f87c-4caa-5c7f-9174-5c069029a326\", \"latest_attempt\": {\"attempt_id\": \"af247160c2a2435691d9a309728e7183\", \"cancel_requested_at\": \"2026-10-09T11:38:57.824609Z\", \"created_at\": \"2026-10-09T11:38:57.207926Z\", \"ended_at\": \"2026-10-09T11:38:57.824609Z\", \"executor_id\": \"seraph-work-board:agent.task.v1\", \"fencing_token\": 2, \"heartbeat_at\": \"2026-10-09T11:38:57.343685Z\", \"lease_expires_at\": null, \"lease_owner\": null, \"outcome\": \"cancelled\", \"readback_status\": \"not_applicable\", \"receipt_refs\": [], \"started_at\": \"2026-10-09T11:38:57.193959Z\", \"task_id\": \"2061228f2c604aa7b74fa1d433104b31\", \"task_revision_at_claim\": 2, \"updated_at\": \"2026-10-09T11:38:57.824609Z\", \"verification_status\": \"cancelled\", \"workflow_run_id\": \"work-board:2061228f2c604aa7b74fa1d433104b31:af247160c2a2435691d9a309728e7183\"}, \"origin_session_id\": \"50e56812c5ee4ef3b59f4fd5663e418b\", \"origin_thread_id\": null, \"owner_principal_id\": \"operator:root:5c673f23c1524c5cb92b0bb595f6f35e\", \"owner_session_id\": \"50e56812c5ee4ef3b59f4fd5663e418b\", \"pipeline_operation_id\": null, \"pipeline_slot\": null, \"priority\": 50, \"readback_status\": \"not_applicable\", \"recovery_action\": \"approve_existing_run\", \"repository_review\": {\"contact_state\": \"not_prepared\", \"iteration_id\": null, \"iteration_index\": null, \"native_child_id\": \"general-tool:eb8f2d65e37c6821e693e7d9574d86d996fc37c5a57365e7\", \"preparation_digest\": null, \"repository_job_id\": \"repository:7f193adfd34899ddb55c9e2c0ce9d7dde2d9ade3ba763cd5d135dafd4a11bbf6\", \"source_preview_path\": null}, \"requires_review\": true, \"result_refs\": [], \"review_expires_at\": null, \"reviewer_id\": \"operator:root:5c673f23c1524c5cb92b0bb595f6f35e\", \"scheduled_at\": null, \"status\": \"blocked\", \"task_id\": \"2061228f2c604aa7b74fa1d433104b31\", \"task_revision\": 6, \"title\": \"Repair the selected repository\", \"typed_input_digest\": \"47d13fcb8cf96fb730769e44c553afe1f9e2c595d3714571df83c08f15cdca7d\", \"typed_input_ref\": \"workspace-json:artifacts/work-board/inputs/f9d2f87c-4caa-5c7f-9174-5c069029a326-47d13fcb8cf96fb730769e44c553afe1f9e2c595d3714571df83c08f15cdca7d.json\", \"updated_at\": \"2026-10-09T11:38:57.824609Z\", \"verification_status\": \"cancelled\"}}, \"status\": 200}, {\"method\": \"GET\", \"path\": \"/api/workflows/repo-repair/repository:7f193adfd34899ddb55c9e2c0ce9d7dde2d9ade3ba763cd5d135dafd4a11bbf6\", \"response\": {\"detail\": {\"code\": \"repo_repair_owner_mismatch\"}}, \"status\": 403}], \"owner\": {\"principal_id\": \"operator:root:5c673f23c1524c5cb92b0bb595f6f35e\", \"session_id\": \"50e56812c5ee4ef3b59f4fd5663e418b\"}}" },
  { state: "prepared", sha256: "e03f6ef5c0a24a075eb45b1e6bf804212492ef031352d33e0e37f9e4f64a43a6", raw: "{\"captures\": [{\"method\": \"GET\", \"path\": \"/api/workflows/repo-repair/repository:105f447ba9fc14d1ff355228492f0ad1c6e1b6c2fcd1af2677e9437801c9f8d8\", \"response\": {\"detail\": {\"code\": \"authentication_required\"}}, \"status\": 401}, {\"method\": \"GET\", \"path\": \"/api/workflows/repo-repair/repository:105f447ba9fc14d1ff355228492f0ad1c6e1b6c2fcd1af2677e9437801c9f8d8\", \"response\": {\"approval\": null, \"iteration_states\": [], \"iterations\": [], \"job_id\": \"repository:105f447ba9fc14d1ff355228492f0ad1c6e1b6c2fcd1af2677e9437801c9f8d8\", \"no_learning\": true, \"operator_visible\": true, \"patch_proposal\": null, \"provider_contacted\": false, \"recovery_action\": \"review_code_egress\", \"repository_review\": {\"contact_state\": \"not_started\", \"iteration_id\": \"a94f6d1186c6b0d3382227b447023313871beff69a63ba6e9958b94226938be1\", \"iteration_index\": 1, \"native_child_id\": \"general-tool:83cf72e6da4c6ff1cca8bb38f4d28b18514233d78d4e5442\", \"preparation_digest\": \"7b991f921d6adf967414ba6ffc0f2365b9d95a71768ed203817d6fd08486e55f\", \"repository_job_id\": \"repository:105f447ba9fc14d1ff355228492f0ad1c6e1b6c2fcd1af2677e9437801c9f8d8\", \"source_preview_path\": \"/api/workflows/repo-repair/repository:105f447ba9fc14d1ff355228492f0ad1c6e1b6c2fcd1af2677e9437801c9f8d8/source-preview\"}, \"revision\": 6, \"status\": \"running\"}, \"status\": 200}, {\"method\": \"GET\", \"path\": \"/api/work-board/tasks/75b776e5570349cb98435eacfe6320b7\", \"response\": {\"attempts\": [{\"attempt_id\": \"acc70d52be80472abe1229456df30552\", \"cancel_requested_at\": null, \"created_at\": \"2026-10-09T11:39:00.011566Z\", \"ended_at\": null, \"executor_id\": \"seraph-work-board:agent.task.v1\", \"fencing_token\": 1, \"heartbeat_at\": \"2026-10-09T11:39:00.204272Z\", \"lease_expires_at\": null, \"lease_owner\": null, \"outcome\": null, \"readback_status\": \"pending\", \"receipt_refs\": [], \"started_at\": \"2026-10-09T11:38:59.993127Z\", \"task_id\": \"75b776e5570349cb98435eacfe6320b7\", \"task_revision_at_claim\": 2, \"updated_at\": \"2026-10-09T11:39:00.204272Z\", \"verification_status\": \"pending\", \"workflow_run_id\": \"work-board:75b776e5570349cb98435eacfe6320b7:acc70d52be80472abe1229456df30552\"}], \"children\": [], \"comments\": [], \"events\": [{\"created_at\": \"2026-10-09T11:38:59.965723Z\", \"event_id\": 1, \"kind\": \"task.created\", \"metadata\": {\"status\": \"todo\", \"task_revision\": 1}, \"task_id\": \"75b776e5570349cb98435eacfe6320b7\"}, {\"created_at\": \"2026-10-09T11:38:59.991516Z\", \"event_id\": 2, \"kind\": \"task.ready\", \"metadata\": {\"status\": \"ready\", \"task_revision\": 2}, \"task_id\": \"75b776e5570349cb98435eacfe6320b7\"}, {\"created_at\": \"2026-10-09T11:39:00.019653Z\", \"event_id\": 3, \"kind\": \"task.claimed\", \"metadata\": {\"attempt_id\": \"acc70d52be80472abe1229456df30552\", \"status\": \"running\", \"task_revision\": 3}, \"task_id\": \"75b776e5570349cb98435eacfe6320b7\"}, {\"created_at\": \"2026-10-09T11:39:00.049099Z\", \"event_id\": 4, \"kind\": \"attempt.linked\", \"metadata\": {\"attempt_id\": \"acc70d52be80472abe1229456df30552\", \"task_revision\": 4, \"workflow_run_id\": \"work-board:75b776e5570349cb98435eacfe6320b7:acc70d52be80472abe1229456df30552\"}, \"task_id\": \"75b776e5570349cb98435eacfe6320b7\"}], \"parent_handoffs\": [], \"parents\": [], \"revision\": 5, \"task\": {\"archived_at\": null, \"artifact_refs\": [], \"assignee_id\": null, \"block_kind\": \"needs_input\", \"block_reason\": \"general_task_native_wait\", \"block_source_status\": \"running\", \"body\": \"General registered-tool task\", \"cancel_requested_at\": null, \"capability_id\": \"agent.task.v1\", \"completed_at\": null, \"completed_dependency_count\": 0, \"created_at\": \"2026-10-09T11:38:59.961957Z\", \"creation_sequence\": 1, \"dependency_count\": 0, \"dispatch_rank\": null, \"dispatch_wait_reason\": null, \"executor_id\": \"seraph-work-board:agent.task.v1\", \"goal_id\": \"goal:fixture\", \"goal_revision\": 1, \"idempotency_key\": \"source-original\", \"idempotency_scope\": \"general-task\", \"input_artifact_id\": \"c264a2d5-6248-56da-85ce-0717d07b9225\", \"latest_attempt\": {\"attempt_id\": \"acc70d52be80472abe1229456df30552\", \"cancel_requested_at\": null, \"created_at\": \"2026-10-09T11:39:00.011566Z\", \"ended_at\": null, \"executor_id\": \"seraph-work-board:agent.task.v1\", \"fencing_token\": 1, \"heartbeat_at\": \"2026-10-09T11:39:00.204272Z\", \"lease_expires_at\": null, \"lease_owner\": null, \"outcome\": null, \"readback_status\": \"pending\", \"receipt_refs\": [], \"started_at\": \"2026-10-09T11:38:59.993127Z\", \"task_id\": \"75b776e5570349cb98435eacfe6320b7\", \"task_revision_at_claim\": 2, \"updated_at\": \"2026-10-09T11:39:00.204272Z\", \"verification_status\": \"pending\", \"workflow_run_id\": \"work-board:75b776e5570349cb98435eacfe6320b7:acc70d52be80472abe1229456df30552\"}, \"origin_session_id\": \"5742c38f91bf4081ad42c73b6fe1551a\", \"origin_thread_id\": null, \"owner_principal_id\": \"operator:root:dcfe0d0be99a4f24a8351dcd19e0b649\", \"owner_session_id\": \"5742c38f91bf4081ad42c73b6fe1551a\", \"pipeline_operation_id\": null, \"pipeline_slot\": null, \"priority\": 50, \"readback_status\": \"pending\", \"recovery_action\": \"approve_existing_run\", \"repository_review\": {\"contact_state\": \"not_started\", \"iteration_id\": \"a94f6d1186c6b0d3382227b447023313871beff69a63ba6e9958b94226938be1\", \"iteration_index\": 1, \"native_child_id\": \"general-tool:83cf72e6da4c6ff1cca8bb38f4d28b18514233d78d4e5442\", \"preparation_digest\": \"7b991f921d6adf967414ba6ffc0f2365b9d95a71768ed203817d6fd08486e55f\", \"repository_job_id\": \"repository:105f447ba9fc14d1ff355228492f0ad1c6e1b6c2fcd1af2677e9437801c9f8d8\", \"source_preview_path\": \"/api/workflows/repo-repair/repository:105f447ba9fc14d1ff355228492f0ad1c6e1b6c2fcd1af2677e9437801c9f8d8/source-preview\"}, \"requires_review\": true, \"result_refs\": [], \"review_expires_at\": null, \"reviewer_id\": \"operator:root:dcfe0d0be99a4f24a8351dcd19e0b649\", \"scheduled_at\": null, \"status\": \"blocked\", \"task_id\": \"75b776e5570349cb98435eacfe6320b7\", \"task_revision\": 5, \"title\": \"Repair the selected repository\", \"typed_input_digest\": \"1f785127d9c1d6c4ea5504a134e0194bf9ff306ee3087540fb887ce979a4b261\", \"typed_input_ref\": \"workspace-json:artifacts/work-board/inputs/c264a2d5-6248-56da-85ce-0717d07b9225-1f785127d9c1d6c4ea5504a134e0194bf9ff306ee3087540fb887ce979a4b261.json\", \"updated_at\": \"2026-10-09T11:39:00.204272Z\", \"verification_status\": \"pending\"}}, \"status\": 200}, {\"method\": \"GET\", \"path\": \"/api/workflows/repo-repair/repository:105f447ba9fc14d1ff355228492f0ad1c6e1b6c2fcd1af2677e9437801c9f8d8\", \"response\": {\"detail\": {\"code\": \"repo_repair_owner_mismatch\"}}, \"status\": 403}], \"owner\": {\"principal_id\": \"operator:root:dcfe0d0be99a4f24a8351dcd19e0b649\", \"session_id\": \"5742c38f91bf4081ad42c73b6fe1551a\"}}" },
  { state: "unknown_pending", sha256: "6bc22abf011c28a27d61018d7282ad13333d9d351a5d1055b10e43f3020b9086", raw: "{\"captures\": [{\"method\": \"GET\", \"path\": \"/api/workflows/repo-repair/repository:2f501b2bd93025ad2e1ee6b5985ab4ae47505e4992936bc4083c6e81ef5c706b\", \"response\": {\"detail\": {\"code\": \"authentication_required\"}}, \"status\": 401}, {\"method\": \"GET\", \"path\": \"/api/workflows/repo-repair/repository:2f501b2bd93025ad2e1ee6b5985ab4ae47505e4992936bc4083c6e81ef5c706b\", \"response\": {\"approval\": null, \"iteration_states\": [], \"iterations\": [], \"job_id\": \"repository:2f501b2bd93025ad2e1ee6b5985ab4ae47505e4992936bc4083c6e81ef5c706b\", \"no_learning\": true, \"operator_visible\": true, \"patch_proposal\": null, \"provider_contacted\": false, \"recovery_action\": \"repository_stop_pending\", \"repository_review\": {\"contact_state\": \"not_started\", \"iteration_id\": \"9096b922453cb5f835888887e62f96a6953fc6b0b7e5067f4daf9d5222fd0d56\", \"iteration_index\": 1, \"native_child_id\": \"general-tool:1f3f586fe34ef76d8e4a74bb3caa775eda843f8f861dd8e3\", \"preparation_digest\": \"9f13e8b313b4fd747f4aec3ea143737bb3a1f0cdc7295d496f0109edc4f757a3\", \"repository_job_id\": \"repository:2f501b2bd93025ad2e1ee6b5985ab4ae47505e4992936bc4083c6e81ef5c706b\", \"source_preview_path\": \"/api/workflows/repo-repair/repository:2f501b2bd93025ad2e1ee6b5985ab4ae47505e4992936bc4083c6e81ef5c706b/source-preview\"}, \"repository_stop\": {\"limit_evidence\": null, \"limit_evidence_digest\": null, \"pending\": true, \"reason\": \"operator_cancelled\"}, \"revision\": 8, \"status\": \"unknown_external_effect\"}, \"status\": 200}, {\"method\": \"GET\", \"path\": \"/api/work-board/tasks/9957f99759e94c7b995064036ff2601f\", \"response\": {\"attempts\": [{\"attempt_id\": \"f6c14b6efc344cb0a6f9905594ae1d97\", \"cancel_requested_at\": null, \"created_at\": \"2026-10-09T12:59:08.476730Z\", \"ended_at\": null, \"executor_id\": \"seraph-work-board:agent.task.v1\", \"fencing_token\": 1, \"heartbeat_at\": \"2026-10-09T12:59:08.589924Z\", \"lease_expires_at\": null, \"lease_owner\": null, \"outcome\": null, \"readback_status\": \"pending\", \"receipt_refs\": [], \"started_at\": \"2026-10-09T12:59:08.465965Z\", \"task_id\": \"9957f99759e94c7b995064036ff2601f\", \"task_revision_at_claim\": 2, \"updated_at\": \"2026-10-09T12:59:08.589924Z\", \"verification_status\": \"pending\", \"workflow_run_id\": \"work-board:9957f99759e94c7b995064036ff2601f:f6c14b6efc344cb0a6f9905594ae1d97\"}], \"children\": [], \"comments\": [], \"events\": [{\"created_at\": \"2026-10-09T12:59:08.450010Z\", \"event_id\": 1, \"kind\": \"task.created\", \"metadata\": {\"status\": \"todo\", \"task_revision\": 1}, \"task_id\": \"9957f99759e94c7b995064036ff2601f\"}, {\"created_at\": \"2026-10-09T12:59:08.465104Z\", \"event_id\": 2, \"kind\": \"task.ready\", \"metadata\": {\"status\": \"ready\", \"task_revision\": 2}, \"task_id\": \"9957f99759e94c7b995064036ff2601f\"}, {\"created_at\": \"2026-10-09T12:59:08.480348Z\", \"event_id\": 3, \"kind\": \"task.claimed\", \"metadata\": {\"attempt_id\": \"f6c14b6efc344cb0a6f9905594ae1d97\", \"status\": \"running\", \"task_revision\": 3}, \"task_id\": \"9957f99759e94c7b995064036ff2601f\"}, {\"created_at\": \"2026-10-09T12:59:08.499383Z\", \"event_id\": 4, \"kind\": \"attempt.linked\", \"metadata\": {\"attempt_id\": \"f6c14b6efc344cb0a6f9905594ae1d97\", \"task_revision\": 4, \"workflow_run_id\": \"work-board:9957f99759e94c7b995064036ff2601f:f6c14b6efc344cb0a6f9905594ae1d97\"}, \"task_id\": \"9957f99759e94c7b995064036ff2601f\"}, {\"created_at\": \"2026-10-09T12:59:09.637871Z\", \"event_id\": 9, \"kind\": \"attempt.repository_stop_requested\", \"metadata\": {\"attempt_id\": \"f6c14b6efc344cb0a6f9905594ae1d97\"}, \"task_id\": \"9957f99759e94c7b995064036ff2601f\"}], \"parent_handoffs\": [], \"parents\": [], \"revision\": 5, \"task\": {\"archived_at\": null, \"artifact_refs\": [], \"assignee_id\": null, \"block_kind\": \"needs_input\", \"block_reason\": \"general_task_native_wait\", \"block_source_status\": \"running\", \"body\": \"General registered-tool task\", \"cancel_requested_at\": null, \"capability_id\": \"agent.task.v1\", \"completed_at\": null, \"completed_dependency_count\": 0, \"created_at\": \"2026-10-09T12:59:08.447358Z\", \"creation_sequence\": 1, \"dependency_count\": 0, \"dispatch_rank\": null, \"dispatch_wait_reason\": null, \"executor_id\": \"seraph-work-board:agent.task.v1\", \"goal_id\": \"goal:fixture\", \"goal_revision\": 1, \"idempotency_key\": \"source-original\", \"idempotency_scope\": \"general-task\", \"input_artifact_id\": \"88cf2051-8949-5a35-9ddd-b8d722fc0943\", \"latest_attempt\": {\"attempt_id\": \"f6c14b6efc344cb0a6f9905594ae1d97\", \"cancel_requested_at\": null, \"created_at\": \"2026-10-09T12:59:08.476730Z\", \"ended_at\": null, \"executor_id\": \"seraph-work-board:agent.task.v1\", \"fencing_token\": 1, \"heartbeat_at\": \"2026-10-09T12:59:08.589924Z\", \"lease_expires_at\": null, \"lease_owner\": null, \"outcome\": null, \"readback_status\": \"pending\", \"receipt_refs\": [], \"started_at\": \"2026-10-09T12:59:08.465965Z\", \"task_id\": \"9957f99759e94c7b995064036ff2601f\", \"task_revision_at_claim\": 2, \"updated_at\": \"2026-10-09T12:59:08.589924Z\", \"verification_status\": \"pending\", \"workflow_run_id\": \"work-board:9957f99759e94c7b995064036ff2601f:f6c14b6efc344cb0a6f9905594ae1d97\"}, \"origin_session_id\": \"0dd94e1ed5b94dd89296c6b1dd746808\", \"origin_thread_id\": null, \"owner_principal_id\": \"operator:root:0fce5f53a2d54f9288d819f9804078b8\", \"owner_session_id\": \"0dd94e1ed5b94dd89296c6b1dd746808\", \"pipeline_operation_id\": null, \"pipeline_slot\": null, \"priority\": 50, \"readback_status\": \"pending\", \"recovery_action\": \"approve_existing_run\", \"repository_review\": {\"contact_state\": \"not_started\", \"iteration_id\": \"9096b922453cb5f835888887e62f96a6953fc6b0b7e5067f4daf9d5222fd0d56\", \"iteration_index\": 1, \"native_child_id\": \"general-tool:1f3f586fe34ef76d8e4a74bb3caa775eda843f8f861dd8e3\", \"preparation_digest\": \"9f13e8b313b4fd747f4aec3ea143737bb3a1f0cdc7295d496f0109edc4f757a3\", \"repository_job_id\": \"repository:2f501b2bd93025ad2e1ee6b5985ab4ae47505e4992936bc4083c6e81ef5c706b\", \"source_preview_path\": \"/api/workflows/repo-repair/repository:2f501b2bd93025ad2e1ee6b5985ab4ae47505e4992936bc4083c6e81ef5c706b/source-preview\"}, \"requires_review\": true, \"result_refs\": [], \"review_expires_at\": null, \"reviewer_id\": \"operator:root:0fce5f53a2d54f9288d819f9804078b8\", \"scheduled_at\": null, \"status\": \"blocked\", \"task_id\": \"9957f99759e94c7b995064036ff2601f\", \"task_revision\": 5, \"title\": \"Repair the selected repository\", \"typed_input_digest\": \"c0daa8d34be470b9e8d6744ecc30337aa189908d47ce98987c7858d3e35ff72a\", \"typed_input_ref\": \"workspace-json:artifacts/work-board/inputs/88cf2051-8949-5a35-9ddd-b8d722fc0943-c0daa8d34be470b9e8d6744ecc30337aa189908d47ce98987c7858d3e35ff72a.json\", \"updated_at\": \"2026-10-09T12:59:08.589924Z\", \"verification_status\": \"pending\"}}, \"status\": 200}, {\"method\": \"GET\", \"path\": \"/api/workflows/repo-repair/repository:2f501b2bd93025ad2e1ee6b5985ab4ae47505e4992936bc4083c6e81ef5c706b\", \"response\": {\"detail\": {\"code\": \"repo_repair_owner_mismatch\"}}, \"status\": 403}], \"owner\": {\"principal_id\": \"operator:root:0fce5f53a2d54f9288d819f9804078b8\", \"session_id\": \"0dd94e1ed5b94dd89296c6b1dd746808\"}}" },
] as const;

function capturedRepositoryState(raw: string) {
  const capture = JSON.parse(raw) as {
    owner: { principal_id: string; session_id: string };
    captures: { method: string; path: string; status: number; response: Record<string, unknown> }[];
  };
  const sourceCall = capture.captures.find((call) => call.status === 200 && call.path.startsWith('/api/workflows/repo-repair/'))!;
  const taskCall = capture.captures.find((call) => call.status === 200 && call.path.startsWith('/api/work-board/tasks/'))!;
  // Preserve the literal R199 capture and its hash. This derived parser input
  // adds the current owner's explicit not-applicable field; it is not a fresh
  // current API receipt or proof of Source recovery execution.
  const source = { ...sourceCall.response, source_recovery: null } as Record<string, unknown>;
  const task = taskCall.response.task as Record<string, unknown>;
  const review = task.repository_review as Record<string, unknown>;
  return { capture, source, sourceCall, task, review, props: {
    jobId: String(review.repository_job_id),
    ownerPrincipalId: capture.owner.principal_id,
    ownerSessionId: capture.owner.session_id,
    taskOwnerPrincipalId: String(task.owner_principal_id),
    taskOwnerSessionId: String(task.owner_session_id),
  } };
}

function expectNoRepositoryEffects() {
  expect(screen.queryByRole('button', { name: 'Inspect exact source and diagnostics' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: "Allow this iteration's source and diagnostics" })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Execute this approved patch' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Review exact patch approval' })).not.toBeInTheDocument();
}

async function capturedBytesDigest(raw: string): Promise<string> {
  // Vitest runs in Node; the browser TypeScript project has no Node type package.
  const moduleName = 'node:crypto';
  const crypto = await import(moduleName) as {
    createHash(algorithm: 'sha256'): { update(data: string): { digest(encoding: 'hex'): string } };
  };
  return crypto.createHash('sha256').update(raw).digest('hex');
}

describe('RepoRepairInspector literal authenticated Source captures R199', () => {
  beforeEach(() => { window.sessionStorage.clear(); });
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); window.sessionStorage.clear(); });

  it.each(repositorySourceCaptureBytes)('binds historical $state capture plus current nullable schema to the same owner and Task review', async ({ state, raw, sha256 }) => {
    expect(await capturedBytesDigest(raw)).toBe(sha256);
    const { capture, source, sourceCall, task, review, props } = capturedRepositoryState(raw);
    expect(source.repository_review).toEqual(review);
    expect(Object.keys(review)).toHaveLength(7);
    expect(task.owner_principal_id).toBe(capture.owner.principal_id);
    expect(task.owner_session_id).toBe(capture.owner.session_id);
    expect(sourceCall.path).toBe(`/api/workflows/repo-repair/${props.jobId}`);
    const fetch = vi.fn().mockResolvedValue(response(source));
    const openApprovals = vi.fn();
    vi.stubGlobal('fetch', fetch);
    render(<RepoRepairInspector {...props} onOpenApprovals={openApprovals} />);
    await screen.findByRole('button', { name: 'Refresh repair status' });
    if (state === 'prepared') {
      expect(await screen.findByText('running · iteration 1 of at most 3')).toBeInTheDocument();
      expect(screen.getByRole('button', { name: 'Inspect exact source and diagnostics' })).toBeInTheDocument();
      expect(source).not.toHaveProperty('repository_stop');
      expect(screen.queryByText(/Stop reason:/)).not.toBeInTheDocument();
    } else {
      const stop = source.repository_stop as Record<string, unknown>;
      const evidence = stop.limit_evidence as Record<string, unknown> | null;
      expect(await screen.findByText(`Stop reason: ${stop.reason}`)).toBeInTheDocument();
      if (state === 'unknown_pending') {
        expect(stop.reason).toBe('operator_cancelled');
        expect(evidence).toBeNull();
        expect(stop.limit_evidence_digest).toBeNull();
      } else {
        expect(evidence).not.toBeNull();
        expect(screen.getByText(`Original cutoff: ${evidence!.original_deadline_at}`)).toBeInTheDocument();
        expect(screen.getByText(`Recorded Root cost: ${evidence!.root_liability_microusd} microusd · original limit ${evidence!.original_root_max_cost_microusd} microusd`)).toBeInTheDocument();
        expect(screen.getByText(`Recorded group calls: ${evidence!.group_calls} · original limit ${evidence!.original_group_max_calls}`)).toBeInTheDocument();
      }
      if (review.contact_state === 'not_prepared') {
        expect(screen.queryByText(/iteration .*of at most 3/)).not.toBeInTheDocument();
      }
      if (source.status === 'unknown_external_effect') {
        expect(stop.pending).toBe(true);
        expect(screen.getByText(/unknown external effect/)).toBeInTheDocument();
        expect(screen.getByText('Unknown outcome. Reconcile the original repository execution, reservation and debt. Physical cleanup is pending verification.')).toBeInTheDocument();
      }
      expectNoRepositoryEffects();
      expect(screen.getByText(stop.pending ? 'Original reservation remains held. Physical cleanup is pending verification.' : 'Original repository stop recorded.')).toBeInTheDocument();
      expect(screen.queryByText('Stop pending. Original durable and physical capacities remain retained.')).not.toBeInTheDocument();
      fireEvent.click(screen.getByRole('button', { name: 'Refresh repair status' }));
      await waitFor(() => expect(fetch).toHaveBeenCalledTimes(2));
      expectNoRepositoryEffects();
      expect(openApprovals).not.toHaveBeenCalled();
    }
    expect(fetch.mock.calls.every((call) => !call[1]?.method || call[1].method === 'GET')).toBe(true);
  });

  it.each([401, 403])('renders the actual authenticated capture rejection %s without controls', async (status) => {
    const { capture, props } = capturedRepositoryState(repositorySourceCaptureBytes[0].raw);
    const rejection = capture.captures.find((call) => call.status === status)!;
    const fetch = vi.fn().mockResolvedValue(response(rejection.response, false, status));
    vi.stubGlobal('fetch', fetch);
    render(<RepoRepairInspector {...props} />);
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expectNoRepositoryEffects();
    expect(fetch).toHaveBeenCalledOnce();
  });

  it.each([
    ['missing stop', (source: Record<string, unknown>) => { delete source.repository_stop; }],
    ['null stop', (source: Record<string, unknown>) => { source.repository_stop = null; }],
    ['copied Root', (source: Record<string, unknown>) => { source.job_id = 'repository:foreign'; }],
    ['mixed preparation', (source: Record<string, unknown>) => { (source.repository_review as Record<string, unknown>).iteration_id = 'a'.repeat(64); }],
    ['private extra field', (source: Record<string, unknown>) => { (source.repository_stop as Record<string, unknown>).private_source = 'must not render'; }],
    ['wrong state', (source: Record<string, unknown>) => { source.status = 'failed'; }],
    ['unsupported pending state', (source: Record<string, unknown>) => { source.status = 'paused'; }],
    ['malformed pending state', (source: Record<string, unknown>) => { source.status = 'unknown'; }],
    ['unsafe cost', (source: Record<string, unknown>) => { ((source.repository_stop as Record<string, unknown>).limit_evidence as Record<string, unknown>).root_liability_microusd = Number.MAX_SAFE_INTEGER + 1; }],
    ['foreign cause', (source: Record<string, unknown>) => { ((source.repository_stop as Record<string, unknown>).limit_evidence as Record<string, unknown>).cause = 'goal_limit_exhausted'; }],
    ['malformed cutoff', (source: Record<string, unknown>) => { ((source.repository_stop as Record<string, unknown>).limit_evidence as Record<string, unknown>).original_deadline_at = 'not a timestamp'; }],
    ['missing digest', (source: Record<string, unknown>) => { (source.repository_stop as Record<string, unknown>).limit_evidence_digest = null; }],
  ])('rejects %s in literal Pending metadata', async (_label, mutate) => {
    const { source, props } = capturedRepositoryState(repositorySourceCaptureBytes[0].raw);
    mutate(source);
    const fetch = vi.fn().mockResolvedValue(response(source));
    vi.stubGlobal('fetch', fetch);
    render(<RepoRepairInspector {...props} />);
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expectNoRepositoryEffects();
    expect(screen.queryByText(/Stop reason:/)).not.toBeInTheDocument();
    expect(fetch).toHaveBeenCalledOnce();
  });

  it('clears the actual prepared controls when a foreign captured Root is returned by refresh', async () => {
    const prepared = capturedRepositoryState(repositorySourceCaptureBytes[2].raw);
    const pending = capturedRepositoryState(repositorySourceCaptureBytes[0].raw);
    const fetch = vi.fn().mockResolvedValueOnce(response(prepared.source)).mockResolvedValueOnce(response(pending.source));
    vi.stubGlobal('fetch', fetch);
    render(<RepoRepairInspector {...prepared.props} />);
    expect(await screen.findByRole('button', { name: 'Inspect exact source and diagnostics' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Refresh repair status' }));
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expectNoRepositoryEffects();
    expect(fetch).toHaveBeenCalledTimes(2);
  });
});
