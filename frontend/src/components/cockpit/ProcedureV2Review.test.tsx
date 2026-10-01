import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { GoalInfo, WorkBoardRoutinePackagePreview, WorkBoardTask } from "../../types";
import * as calendarApi from "../../lib/calendar";
import {
  procedureV2Api,
  ProcedureV2ApiError,
  type ProcedureV2Preview,
  type ProcedureV2Prepared,
  type ProcedureV2InvokeReceipt,
  type ProcedureV2Routine,
  type ProcedureV2ScheduleReceipt,
} from "../../lib/procedureV2Api";
import { ProcedureV2Review } from "./ProcedureV2Review";

const digest = "a".repeat(64);

const goal: GoalInfo = {
  id: "goal-1",
  parent_id: null,
  path: "goal-1",
  level: "root",
  title: "Keep the public service healthy",
  description: null,
  status: "active",
  domain: "operations",
  start_date: null,
  due_date: null,
  sort_order: 0,
  revision: 4,
};

const sourceTask = {
  task_id: "source-task",
  creation_sequence: 1,
  owner_principal_id: "operator:one",
  owner_session_id: "session-1",
  origin_session_id: "session-1",
  origin_thread_id: null,
  goal_id: "goal-1",
  goal_revision: 4,
  title: "Verified public check",
  body: "PRIVATE SOURCE INSTRUCTIONS MUST NOT BE RENDERED",
  capability_id: "browser.public-task.v1",
  typed_input_ref: "workspace-json:private-input",
  typed_input_digest: digest,
  executor_id: null,
  assignee_id: null,
  priority: 50,
  idempotency_scope: "task",
  idempotency_key: "task-key",
  scheduled_at: null,
  status: "done",
  block_kind: null,
  block_reason: null,
  block_source_status: null,
  cancel_requested_at: null,
  requires_review: false,
  reviewer_id: null,
  dependency_count: 0,
  completed_dependency_count: 0,
  dispatch_rank: null,
  dispatch_wait_reason: null,
  recovery_action: null,
  readback_status: "verified",
  verification_status: "passed",
  task_revision: 3,
  result_refs: [],
  artifact_refs: [],
  latest_attempt: null,
  created_at: "2026-10-01T10:00:00Z",
  updated_at: "2026-10-01T10:01:00Z",
  completed_at: "2026-10-01T10:01:00Z",
  archived_at: null,
} as WorkBoardTask;

const existingRoutine = {
  id: "routine-existing",
  owner_principal_id: "operator:one",
  state: "active",
  revision: 5,
  current_version: 2,
  name: "Reusable public check",
  versions: [
    {
      id: "version-existing-1",
      routine_id: "routine-existing",
      version: 1,
      workflow_sha256: digest,
      runbook_sha256: digest,
      installed_package_digest: digest,
      source_provenance: {},
      source_repository: null,
      source_action: null,
      source_issue_number: null,
      created_at: "2026-10-01T12:15:00Z",
      installed_at: "2026-10-01T12:00:00Z",
      schema_version: 2,
      template_id: "public-browser-check",
      procedure_binding: { binding_id: "binding-existing-1", state: "active", revision: 3, preview_digest: digest, preview_expires_at: "2026-10-01T12:15:00Z", install_job_id: "routine-install:routine-existing:v1", approval_id: "approval-existing-1", install_approval_status: "consumed", install_approval_expires_at: "2026-10-02T12:15:00Z" },
    },
    {
      id: "version-existing-2",
      routine_id: "routine-existing",
      version: 2,
      workflow_sha256: digest,
      runbook_sha256: digest,
      installed_package_digest: digest,
      source_provenance: {},
      source_repository: null,
      source_action: null,
      source_issue_number: null,
      created_at: "2026-10-01T12:10:00Z",
      installed_at: "2026-10-01T12:10:00Z",
      schema_version: 2,
      template_id: "public-browser-check",
      procedure_binding: { binding_id: "binding-existing-2", state: "active", revision: 4, preview_digest: digest, preview_expires_at: "2026-10-01T12:15:00Z", install_job_id: "routine-install:routine-existing:v2", approval_id: "approval-existing-2", install_approval_status: "consumed", install_approval_expires_at: "2026-10-02T12:15:00Z" },
    },
  ],
  package: { status: "active", digest, review_id: "review-existing" },
} as ProcedureV2Routine;

const preview: ProcedureV2Preview = {
  status: "preview",
  template_id: "public-browser-check",
  preview_digest: digest,
  expires_at: "2026-10-01T12:15:00Z",
  plan: {
    schema_version: 2,
    template_id: "public-browser-check",
    steps: [{ step_id: "public_browser_check", capability_id: "browser.public-task.v1", capability_version: "1", typed_input_ref: "server-owned-ref", typed_input_digest: digest }],
    parameters: [{ name: "goal_id", kind: "goal_id", required: true }, { name: "expected_goal_revision", kind: "goal_revision", required: true }],
    verifier: "leaf_readbacks",
    limits: { max_steps: 2, max_total_seconds: 300 },
  },
  source_refs: [{ task_id: "source-task", task_revision: 3, attempt_id: "attempt-1", job_id: "job-1", artifact_ids_and_hashes: [{ artifact_id: "artifact-1", sha256: digest }], capability_id: "browser.public-task.v1", capability_version: "1", goal_id: "goal-1", goal_revision: 4 }],
  parameter_schema: [{ name: "goal_id", kind: "goal_id", required: true }, { name: "expected_goal_revision", kind: "goal_revision", required: true }],
  permissions: ["browser.public-task.v1"],
  limits: { max_steps: 2, max_total_seconds: 300 },
  version_diff: null,
};

afterEach(() => {
  vi.restoreAllMocks();
  window.sessionStorage.clear();
});

beforeEach(() => {
  vi.spyOn(procedureV2Api, "listRoutines").mockResolvedValue([]);
});

describe("ProcedureV2Review", () => {
  it("requires an explicit preview and never renders a private source body", async () => {
    vi.spyOn(procedureV2Api, "listSourceTasks").mockResolvedValue([{
      task_id: sourceTask.task_id,
      title: sourceTask.title,
      status: sourceTask.status,
      capability_id: sourceTask.capability_id,
      goal_id: sourceTask.goal_id,
      goal_revision: sourceTask.goal_revision,
      task_revision: sourceTask.task_revision,
      readback_status: sourceTask.readback_status,
      verification_status: sourceTask.verification_status,
    }]);
    const previewCall = vi.spyOn(procedureV2Api, "previewFromTasks").mockResolvedValue(preview);
    render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-1" selectedSourceTask={sourceTask} goals={[goal]} />);

    expect(screen.queryByText("PRIVATE SOURCE INSTRUCTIONS MUST NOT BE RENDERED")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Procedure name"), { target: { value: "Public status check" } });
    fireEvent.click(screen.getByRole("button", { name: "Preview fixed procedure" }));

    await waitFor(() => expect(previewCall).toHaveBeenCalledWith(expect.objectContaining({
      template_id: "public-browser-check",
      source_tasks: [{ task_id: "source-task", expected_revision: 3 }],
      name: "Public status check",
    })));
    expect(await screen.findByRole("region", { name: "Procedure preview" })).toBeInTheDocument();
    expect(screen.getByText(/Nothing was installed or executed/)).toBeInTheDocument();
  });

  it.each([
    ["principal", { owner_principal_id: "" }],
    ["session", { owner_session_id: "" }],
  ] as const)("fails closed when a selected source is missing its owner-%s binding and sends no preview", async (_component, missingOwner) => {
    vi.spyOn(procedureV2Api, "listSourceTasks").mockResolvedValue([]);
    const previewCall = vi.spyOn(procedureV2Api, "previewFromTasks").mockResolvedValue(preview);
    const incompleteSource = { ...sourceTask, ...missingOwner } as WorkBoardTask;
    render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-1" selectedSourceTask={incompleteSource} goals={[goal]} />);

    await waitFor(() => expect(procedureV2Api.listSourceTasks).toHaveBeenCalled());
    expect(screen.queryByText(/Work Board source selected/)).not.toBeInTheDocument();
    expect(screen.getByLabelText("Verified source task")).toHaveValue("");
    const previewButton = screen.getByRole("button", { name: "Preview fixed procedure" });
    fireEvent.click(previewButton);
    expect(previewCall).not.toHaveBeenCalled();
  });

  it("does not display or submit fetched tasks without the exact current owner pair", async () => {
    vi.spyOn(procedureV2Api, "listSourceTasks").mockResolvedValue([
      {
        task_id: "foreign-task",
        title: "Foreign task",
        status: "done",
        capability_id: "browser.public-task.v1",
        owner_principal_id: "operator:other",
        owner_session_id: "session-other",
        goal_id: "goal-1",
        goal_revision: 4,
        task_revision: 3,
        readback_status: "verified",
        verification_status: "passed",
      },
      {
        task_id: "missing-owner-task",
        title: "Missing owner task",
        status: "done",
        capability_id: "browser.public-task.v1",
        goal_id: "goal-1",
        goal_revision: 4,
        task_revision: 3,
        readback_status: "verified",
        verification_status: "passed",
      },
    ]);
    const previewCall = vi.spyOn(procedureV2Api, "previewFromTasks").mockResolvedValue(preview);
    render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-1" goals={[goal]} />);

    await waitFor(() => expect(procedureV2Api.listSourceTasks).toHaveBeenCalled());
    expect(screen.queryByRole("option", { name: /foreign-task/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /missing-owner-task/ })).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Procedure name"), { target: { value: "Owner check" } });
    fireEvent.click(screen.getByRole("button", { name: "Preview fixed procedure" }));
    expect(previewCall).not.toHaveBeenCalled();
  });

  it("keeps stale or expired server recovery visible", async () => {
    vi.spyOn(procedureV2Api, "listSourceTasks").mockResolvedValue([{
      task_id: sourceTask.task_id,
      title: sourceTask.title,
      status: sourceTask.status,
      capability_id: sourceTask.capability_id,
      goal_id: sourceTask.goal_id,
      goal_revision: sourceTask.goal_revision,
      task_revision: sourceTask.task_revision,
      readback_status: sourceTask.readback_status,
      verification_status: sourceTask.verification_status,
    }]);
    vi.spyOn(procedureV2Api, "previewFromTasks").mockRejectedValue(new ProcedureV2ApiError(409, {
      code: "procedure_source_stale",
      message: "The selected source changed",
      recovery_action: "select_verified_task",
      retryable: false,
      binding_id: null,
      audit_receipt_id: null,
    }));
    render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-1" selectedSourceTask={sourceTask} goals={[goal]} />);
    fireEvent.change(screen.getByLabelText("Procedure name"), { target: { value: "Stale check" } });
    fireEvent.click(screen.getByRole("button", { name: "Preview fixed procedure" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Recovery: select_verified_task");
  });

  it("keeps a blocked preparation recovery visible instead of inventing an approval", async () => {
    vi.spyOn(procedureV2Api, "listSourceTasks").mockResolvedValue([{
      task_id: sourceTask.task_id,
      title: sourceTask.title,
      status: sourceTask.status,
      capability_id: sourceTask.capability_id,
      goal_id: sourceTask.goal_id,
      goal_revision: sourceTask.goal_revision,
      task_revision: sourceTask.task_revision,
      readback_status: sourceTask.readback_status,
      verification_status: sourceTask.verification_status,
    }]);
    vi.spyOn(procedureV2Api, "previewFromTasks").mockResolvedValue(preview);
    vi.spyOn(procedureV2Api, "prepareFromTasks").mockResolvedValue({
      status: "blocked",
      binding_id: "binding-1",
      revision: 1,
      schema_version: 2,
      template_id: "public-browser-check",
      request_digest: digest,
      preview_digest: digest,
      preview_expires_at: preview.expires_at,
      recovery_action: "reconcile_preparation",
    });
    render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-1" selectedSourceTask={sourceTask} goals={[goal]} />);
    fireEvent.change(screen.getByLabelText("Procedure name"), { target: { value: "Blocked check" } });
    fireEvent.click(screen.getByRole("button", { name: "Preview fixed procedure" }));
    await screen.findByRole("region", { name: "Procedure preview" });
    fireEvent.click(screen.getByRole("button", { name: "Prepare this reviewed version" }));
    expect(await screen.findByText("Preparation was blocked by the server. Recovery: reconcile_preparation.")).toBeInTheDocument();
  });

  it("clears a definitive pre-effect preparation rejection and exposes fresh preview recovery", async () => {
    vi.spyOn(procedureV2Api, "listSourceTasks").mockResolvedValue([{
      task_id: sourceTask.task_id,
      title: sourceTask.title,
      status: sourceTask.status,
      capability_id: sourceTask.capability_id,
      goal_id: sourceTask.goal_id,
      goal_revision: sourceTask.goal_revision,
      task_revision: sourceTask.task_revision,
      readback_status: sourceTask.readback_status,
      verification_status: sourceTask.verification_status,
    }]);
    vi.spyOn(procedureV2Api, "previewFromTasks").mockResolvedValue(preview);
    vi.spyOn(procedureV2Api, "prepareFromTasks").mockRejectedValue(new ProcedureV2ApiError(409, {
      code: "procedure_preview_expired",
      message: "The procedure preview has expired",
      recovery_action: "create_fresh_preview",
      retryable: false,
      binding_id: null,
      audit_receipt_id: null,
    }));
    render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-1" selectedSourceTask={sourceTask} goals={[goal]} />);
    fireEvent.change(screen.getByLabelText("Procedure name"), { target: { value: "Expired check" } });
    fireEvent.click(screen.getByRole("button", { name: "Preview fixed procedure" }));
    await screen.findByRole("region", { name: "Procedure preview" });
    fireEvent.click(screen.getByRole("button", { name: "Prepare this reviewed version" }));
    expect(await screen.findByText(/No governed effect was committed\. Start a fresh preview or rebind/)).toBeInTheDocument();
    expect(window.sessionStorage.getItem("seraph.procedure-v2.pending:operator%3Aone:session-1")).toBeNull();
    expect(screen.queryByRole("region", { name: "Procedure preview" })).not.toBeInTheDocument();
  });

  it("keeps a server-pending install approval on its review path", async () => {
    vi.spyOn(procedureV2Api, "listSourceTasks").mockResolvedValue([]);
    const pendingRoutine = {
      id: "routine-pending",
      owner_principal_id: "operator:one",
      state: "prepared",
      revision: 1,
      current_version: 1,
      name: "Pending public check",
      versions: [{
        id: "version-pending",
        routine_id: "routine-pending",
        version: 1,
        workflow_sha256: digest,
        runbook_sha256: digest,
        installed_package_digest: null,
        source_provenance: {},
        source_repository: null,
        source_action: null,
        source_issue_number: null,
        created_at: preview.expires_at,
        installed_at: null,
        schema_version: 2,
        template_id: "public-browser-check",
        procedure_binding: {
          binding_id: "binding-pending",
          state: "prepared",
          revision: 1,
          preview_digest: digest,
          preview_expires_at: preview.expires_at,
          install_job_id: "routine-install:routine-pending:v1",
          approval_id: "approval-pending",
          install_approval_status: "pending",
          install_approval_expires_at: "2026-10-02T12:15:00Z",
          install_recovery_action: null,
        },
      }],
      package: { status: "not_installed", digest: null, review_id: null },
    } as ProcedureV2Routine;
    vi.spyOn(procedureV2Api, "getRoutine").mockResolvedValue(pendingRoutine);
    window.sessionStorage.setItem("seraph.procedure-v2.prepared:operator%3Aone:session-1", JSON.stringify({
      schema_version: 1,
      routineId: "routine-pending",
      bindingId: "binding-pending",
      revision: 1,
      version: 1,
      installJobId: "routine-install:routine-pending:v1",
      approvalId: "approval-pending",
      installApprovalStatus: "pending",
      installApprovalExpiresAt: "2026-10-02T12:15:00Z",
      installRecoveryAction: null,
    }));
    const onOpenApprovals = vi.fn();

    render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-1" goals={[goal]} onOpenApprovals={onOpenApprovals} />);

    await screen.findByText("binding-pending");
    expect(screen.getByText(/server status pending/)).toBeInTheDocument();
    expect(screen.getByText("This exact install approval is pending operator review. Review it in Pending approvals before installing.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Start fresh preview/rebind" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Install with exact approval" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Review this exact approval in Pending approvals" }));
    expect(onOpenApprovals).toHaveBeenCalledWith("approval-pending");
  });

  it("does not let a pending-list approval override the server approval status", async () => {
    const serverPendingRoutine = {
      ...existingRoutine,
      state: "prepared",
      revision: 6,
      current_version: 1,
      package: { status: "not_installed", digest: null, review_id: null },
      versions: existingRoutine.versions.map((version) => version.version === 1
        ? {
          ...version,
          installed_package_digest: null,
          installed_at: null,
          procedure_binding: {
            ...version.procedure_binding,
            state: "prepared",
            revision: 6,
            approval_id: "approval-server",
            install_approval_status: "pending",
            install_approval_expires_at: "2026-10-02T12:15:00Z",
          },
        }
        : version),
    } as ProcedureV2Routine;
    const lifecycle = vi.spyOn(procedureV2Api, "lifecycle");
    vi.spyOn(procedureV2Api, "listRoutines").mockResolvedValue([serverPendingRoutine]);
    vi.spyOn(procedureV2Api, "getRoutine").mockResolvedValue(serverPendingRoutine);

    render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-1" goals={[goal]} pendingApprovals={[{ id: "approval-server", status: "approved", tool_name: "guardian:routine-install", summary: "Stale list approval" }]} />);
    fireEvent.change(await screen.findByLabelText("Existing reviewed procedure"), { target: { value: serverPendingRoutine.id } });
    expect(await screen.findByText(/pending-list status approved/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Install with exact approval" })).toBeDisabled();
    expect(lifecycle).not.toHaveBeenCalled();
  });

  it("keeps the fresh-procedure gate and retained recovery when preview is unresolved", async () => {
    const installedRoutine = { ...existingRoutine, state: "installed", revision: 7, current_version: 2 } as ProcedureV2Routine;
    const recovery = {
      schema_version: 1,
      routineId: installedRoutine.id,
      versionId: "version-existing-2",
      version: 2,
      digest,
      expectedRevision: 7,
      approvalId: "package-approval-2",
    };
    const previewCall = vi.spyOn(procedureV2Api, "previewFromTasks");
    vi.spyOn(procedureV2Api, "listRoutines").mockResolvedValue([installedRoutine]);
    vi.spyOn(procedureV2Api, "getRoutine").mockResolvedValue(installedRoutine);
    window.sessionStorage.setItem("seraph.procedure-v2.prepared:operator%3Aone:session-1", JSON.stringify({
      schema_version: 1,
      routineId: installedRoutine.id,
      bindingId: "binding-existing-2",
      revision: 4,
      version: 2,
      versionId: "version-existing-2",
      installJobId: "install-2",
      approvalId: "approval-install-2",
      packageActivationRecovery: recovery,
    }));

    render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-1" goals={[goal]} />);
    await screen.findByText(/Package activation is unverified/);
    const previewButton = screen.getByRole("button", { name: "Preview fixed procedure" });
    expect(previewButton).toBeDisabled();
    expect(JSON.parse(window.sessionStorage.getItem("seraph.procedure-v2.prepared:operator%3Aone:session-1") ?? "null")).toMatchObject({ packageActivationRecovery: recovery });
    expect(previewCall).not.toHaveBeenCalled();
  });

  it("retains an unknown preparation body by owner session and retries the exact key after remount", async () => {
    vi.spyOn(procedureV2Api, "listSourceTasks").mockResolvedValue([{
      task_id: sourceTask.task_id,
      title: sourceTask.title,
      status: sourceTask.status,
      capability_id: sourceTask.capability_id,
      goal_id: sourceTask.goal_id,
      goal_revision: sourceTask.goal_revision,
      task_revision: sourceTask.task_revision,
      readback_status: sourceTask.readback_status,
      verification_status: sourceTask.verification_status,
    }]);
    vi.spyOn(procedureV2Api, "previewFromTasks").mockResolvedValue(preview);
    const preparedResponse = {
      status: "prepared" as const,
      binding_id: "binding-1",
      routine_id: "routine-1",
      version_id: "version-1",
      version: 1,
      schema_version: 2 as const,
      template_id: "public-browser-check" as const,
      revision: 1,
      request_digest: digest,
      preview_digest: digest,
      preview_expires_at: preview.expires_at,
      install_job_id: "routine-install:routine-1:v1",
      approval_id: "approval-install-1",
      install_approval_status: "pending",
      install_approval_expires_at: "2026-10-02T12:15:00Z",
    };
    const prepareCall = vi.spyOn(procedureV2Api, "prepareFromTasks")
      .mockRejectedValueOnce(new ProcedureV2ApiError(503, { code: "procedure_preparation_unknown", message: "Preparation outcome is unknown", recovery_action: "reconcile_preparation", retryable: true, binding_id: "binding-1", audit_receipt_id: null }))
      .mockResolvedValueOnce(preparedResponse);
    vi.spyOn(procedureV2Api, "getRoutine").mockResolvedValue({
      id: "routine-1", owner_principal_id: "operator:one", state: "prepared", revision: 1, current_version: 1, name: "Unknown preparation", versions: [{ id: "version-1", routine_id: "routine-1", version: 1, workflow_sha256: digest, runbook_sha256: digest, installed_package_digest: null, source_provenance: {}, source_repository: null, source_action: null, source_issue_number: null, created_at: preview.expires_at, installed_at: null, schema_version: 2, template_id: "public-browser-check", procedure_binding: { binding_id: "binding-1", state: "prepared", revision: 1, preview_digest: digest, preview_expires_at: preview.expires_at, install_job_id: "routine-install:routine-1:v1", approval_id: "approval-install-1", install_approval_status: "pending", install_approval_expires_at: "2026-10-02T12:15:00Z", install_recovery_action: null } }], package: { status: "not_installed", digest: null, review_id: null },
    } as ProcedureV2Routine);

    const first = render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-1" selectedSourceTask={sourceTask} goals={[goal]} />);
    fireEvent.change(screen.getByLabelText("Procedure name"), { target: { value: "Unknown preparation" } });
    fireEvent.click(screen.getByRole("button", { name: "Preview fixed procedure" }));
    await screen.findByRole("region", { name: "Procedure preview" });
    fireEvent.click(screen.getByRole("button", { name: "Prepare this reviewed version" }));
    expect(await screen.findByText(/Preparation outcome is unknown/)).toBeInTheDocument();
    const pendingKey = "seraph.procedure-v2.pending:operator%3Aone:session-1";
    const pendingBody = JSON.parse(window.sessionStorage.getItem(pendingKey) ?? "null");
    expect(pendingBody).toMatchObject({ schema_version: 1, kind: "prepare", request: { template_id: "public-browser-check", preview_digest: digest, idempotency_key: expect.any(String) } });
    const firstRequest = prepareCall.mock.calls[0]?.[0];
    first.unmount();

    const isolated = render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-2" goals={[goal]} />);
    expect(screen.queryByText(/unconfirmed prepare request/)).not.toBeInTheDocument();
    isolated.unmount();
    render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-1" goals={[goal]} />);
    fireEvent.click(await screen.findByRole("button", { name: "Retry exact request" }));
    await screen.findByText("binding-1");
    expect(prepareCall).toHaveBeenCalledTimes(2);
    expect(prepareCall.mock.calls[1]?.[0]).toEqual(firstRequest);
  });

  it("retains a prepared request when routine readback fails, then retries the exact body after remount", async () => {
    vi.spyOn(procedureV2Api, "listSourceTasks").mockResolvedValue([{
      task_id: sourceTask.task_id,
      title: sourceTask.title,
      status: sourceTask.status,
      capability_id: sourceTask.capability_id,
      goal_id: sourceTask.goal_id,
      goal_revision: sourceTask.goal_revision,
      task_revision: sourceTask.task_revision,
      readback_status: sourceTask.readback_status,
      verification_status: sourceTask.verification_status,
    }]);
    vi.spyOn(procedureV2Api, "previewFromTasks").mockResolvedValue(preview);
    const preparedResponse: ProcedureV2Prepared = {
      status: "prepared",
      binding_id: "binding-readback",
      routine_id: "routine-readback",
      version_id: "version-readback",
      version: 1,
      schema_version: 2,
      template_id: "public-browser-check",
      revision: 1,
      request_digest: digest,
      preview_digest: digest,
      preview_expires_at: preview.expires_at,
      install_job_id: "install-readback",
      approval_id: "approval-readback",
      install_approval_status: "pending",
      install_approval_expires_at: "2026-10-02T12:15:00Z",
    };
    const preparedRoutine = {
      id: "routine-readback",
      owner_principal_id: "operator:one",
      state: "prepared",
      revision: 1,
      current_version: 1,
      name: "Readback recovery",
      versions: [{
        id: "version-readback",
        routine_id: "routine-readback",
        version: 1,
        workflow_sha256: digest,
        runbook_sha256: digest,
        installed_package_digest: null,
        source_provenance: {},
        source_repository: null,
        source_action: null,
        source_issue_number: null,
        created_at: preview.expires_at,
        installed_at: null,
        schema_version: 2,
        template_id: "public-browser-check",
        procedure_binding: {
          binding_id: "binding-readback",
          state: "prepared",
          revision: 1,
          preview_digest: digest,
          preview_expires_at: preview.expires_at,
          install_job_id: "install-readback",
          approval_id: "approval-readback",
          install_approval_status: "pending",
          install_approval_expires_at: "2026-10-02T12:15:00Z",
          install_recovery_action: null,
        },
      }],
      package: { status: "not_installed", digest: null, review_id: null },
    } as ProcedureV2Routine;
    const prepareCall = vi.spyOn(procedureV2Api, "prepareFromTasks").mockResolvedValueOnce(preparedResponse).mockResolvedValueOnce(preparedResponse);
    const getRoutine = vi.spyOn(procedureV2Api, "getRoutine")
      .mockRejectedValueOnce(new Error("routine readback unavailable"))
      .mockRejectedValueOnce(new Error("routine readback still unavailable"))
      .mockRejectedValueOnce(new Error("routine readback still unavailable"))
      .mockResolvedValueOnce(preparedRoutine);

    const first = render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-1" selectedSourceTask={sourceTask} goals={[goal]} />);
    fireEvent.change(screen.getByLabelText("Procedure name"), { target: { value: "Readback recovery" } });
    fireEvent.click(screen.getByRole("button", { name: "Preview fixed procedure" }));
    await screen.findByRole("region", { name: "Procedure preview" });
    fireEvent.click(screen.getByRole("button", { name: "Prepare this reviewed version" }));
    expect(await screen.findByText(/routine readback unavailable/)).toBeInTheDocument();
    const pendingKey = "seraph.procedure-v2.pending:operator%3Aone:session-1";
    const firstRequest = prepareCall.mock.calls[0]?.[0];
    expect(firstRequest).toBeDefined();
    expect(JSON.parse(window.sessionStorage.getItem(pendingKey) ?? "null")).toMatchObject({
      schema_version: 1,
      kind: "prepare",
      request: firstRequest,
    });
    first.unmount();

    render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-1" goals={[goal]} />);
    await waitFor(() => expect(screen.getAllByRole("alert").some((node) => node.textContent?.includes("An unconfirmed prepare request"))).toBe(true));
    fireEvent.click(screen.getByRole("button", { name: "Retry exact request" }));
    await screen.findByText("binding-readback");
    expect(prepareCall).toHaveBeenCalledTimes(2);
    expect(prepareCall.mock.calls[1]?.[0]).toEqual(firstRequest);
    expect(getRoutine).toHaveBeenCalledTimes(4);
    expect(window.sessionStorage.getItem(pendingKey)).toBeNull();
    expect(window.sessionStorage.getItem("seraph.procedure-v2.prepared:operator%3Aone:session-1")).not.toBeNull();
  });

  it("does not correlate preparation by numeric version when the server returns a different version id", async () => {
    vi.spyOn(procedureV2Api, "listSourceTasks").mockResolvedValue([{
      task_id: sourceTask.task_id,
      title: sourceTask.title,
      status: sourceTask.status,
      capability_id: sourceTask.capability_id,
      goal_id: sourceTask.goal_id,
      goal_revision: sourceTask.goal_revision,
      task_revision: sourceTask.task_revision,
      readback_status: sourceTask.readback_status,
      verification_status: sourceTask.verification_status,
    }]);
    vi.spyOn(procedureV2Api, "previewFromTasks").mockResolvedValue(preview);
    vi.spyOn(procedureV2Api, "prepareFromTasks").mockResolvedValue({
      status: "prepared", binding_id: "binding-exact", routine_id: "routine-exact", version_id: "version-requested", version: 1,
      schema_version: 2, template_id: "public-browser-check", revision: 1, request_digest: digest, preview_digest: digest,
      preview_expires_at: preview.expires_at, install_job_id: "install-exact", approval_id: "approval-exact", install_approval_status: "pending", install_approval_expires_at: "2026-10-02T12:15:00Z",
    });
    vi.spyOn(procedureV2Api, "getRoutine").mockResolvedValue({
      id: "routine-exact", owner_principal_id: "operator:one", state: "prepared", revision: 1, current_version: 1, name: "Exact identity",
      versions: [{ id: "version-other", routine_id: "routine-exact", version: 1, workflow_sha256: digest, runbook_sha256: digest, installed_package_digest: null, source_provenance: {}, source_repository: null, source_action: null, source_issue_number: null, created_at: preview.expires_at, installed_at: null }],
      package: { status: "not_installed", digest: null, review_id: null },
    } as ProcedureV2Routine);

    render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-1" selectedSourceTask={sourceTask} goals={[goal]} />);
    fireEvent.change(screen.getByLabelText("Procedure name"), { target: { value: "Exact identity" } });
    fireEvent.click(screen.getByRole("button", { name: "Preview fixed procedure" }));
    await screen.findByRole("region", { name: "Procedure preview" });
    fireEvent.click(screen.getByRole("button", { name: "Prepare this reviewed version" }));
    expect(await screen.findByText(/prepared routine readback did not contain the exact server-owned version/i)).toBeInTheDocument();
    expect(window.sessionStorage.getItem("seraph.procedure-v2.pending:operator%3Aone:session-1")).not.toBeNull();
    expect(screen.queryByText("Exact identity · prepared · current v1")).not.toBeInTheDocument();
  });

  it("does not apply a delayed preparation response after the owner session changes", async () => {
    vi.spyOn(procedureV2Api, "listSourceTasks").mockResolvedValue([{
      task_id: sourceTask.task_id,
      title: sourceTask.title,
      status: sourceTask.status,
      capability_id: sourceTask.capability_id,
      goal_id: sourceTask.goal_id,
      goal_revision: sourceTask.goal_revision,
      task_revision: sourceTask.task_revision,
      readback_status: sourceTask.readback_status,
      verification_status: sourceTask.verification_status,
    }]);
    vi.spyOn(procedureV2Api, "previewFromTasks").mockResolvedValue(preview);
    let resolvePrepare: (value: ProcedureV2Prepared) => void = () => undefined;
    const delayedPrepare = new Promise<ProcedureV2Prepared>((resolve) => { resolvePrepare = resolve; });
    vi.spyOn(procedureV2Api, "prepareFromTasks").mockReturnValue(delayedPrepare);
    const view = render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-1" selectedSourceTask={sourceTask} goals={[goal]} />);
    fireEvent.change(screen.getByLabelText("Procedure name"), { target: { value: "Delayed preparation" } });
    fireEvent.click(screen.getByRole("button", { name: "Preview fixed procedure" }));
    await screen.findByRole("region", { name: "Procedure preview" });
    fireEvent.click(screen.getByRole("button", { name: "Prepare this reviewed version" }));
    view.rerender(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-2" selectedSourceTask={sourceTask} goals={[goal]} />);
    await waitFor(() => expect(screen.queryByText(/unconfirmed prepare request/)).not.toBeInTheDocument());
    resolvePrepare({ status: "prepared", binding_id: "binding-1", routine_id: "routine-1", version_id: "version-1", version: 1, schema_version: 2, template_id: "public-browser-check", revision: 1, request_digest: digest, preview_digest: digest, preview_expires_at: preview.expires_at, install_job_id: "install-1", approval_id: "approval-1", install_approval_status: "pending", install_approval_expires_at: "2026-10-02T12:15:00Z" });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText("binding-1")).not.toBeInTheDocument();
  });

  it("does not apply a delayed invocation receipt after the owner session changes", async () => {
    vi.spyOn(procedureV2Api, "listSourceTasks").mockResolvedValue([]);
    const activeRoutine = {
      id: "routine-1", owner_principal_id: "operator:one", state: "active", revision: 4, current_version: 1, name: "Delayed invocation",
      versions: [{ id: "version-1", routine_id: "routine-1", version: 1, workflow_sha256: digest, runbook_sha256: digest, installed_package_digest: digest, source_provenance: {}, source_repository: null, source_action: null, source_issue_number: null, created_at: preview.expires_at, installed_at: preview.expires_at }],
      package: { status: "active", digest, review_id: "review-1" },
    } as ProcedureV2Routine;
    vi.spyOn(procedureV2Api, "getRoutine").mockResolvedValue(activeRoutine);
    let resolveInvoke: (value: ProcedureV2InvokeReceipt) => void = () => undefined;
    const delayedInvoke = new Promise<ProcedureV2InvokeReceipt>((resolve) => { resolveInvoke = resolve; });
    const invokeCall = vi.spyOn(procedureV2Api, "invoke").mockReturnValue(delayedInvoke);
    window.sessionStorage.setItem("seraph.procedure-v2.prepared:operator%3Aone:session-1", JSON.stringify({ schema_version: 1, routineId: "routine-1", bindingId: "binding-1", revision: 1, version: 1, installJobId: "install-1", approvalId: "approval-1" }));
    const view = render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-1" goals={[goal]} />);
    await screen.findByText("routine-1");
    fireEvent.click(screen.getByRole("button", { name: "Invoke for this goal" }));
    await waitFor(() => expect(invokeCall).toHaveBeenCalledTimes(1));
    view.rerender(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-2" goals={[goal]} />);
    await waitFor(() => expect(screen.queryByText("binding-1")).not.toBeInTheDocument());
    const delayedRequest = invokeCall.mock.calls[0]?.[1];
    resolveInvoke({ status: "accepted", routine_id: "routine-1", version: 1, schema_version: 2, template_id: "public-browser-check", invocation_uuid: delayedRequest?.invocation_uuid ?? "invoke-delayed", scope: "procedure-v2:routine-1:version-1", goal_id: delayedRequest?.goal_id ?? "goal-1", goal_revision: delayedRequest?.expected_goal_revision ?? 4, task_id: "task-delayed", attempt_id: null, job_id: null, input_artifact_id: "input-delayed", input_digest: digest, plan_digest: digest, revision: 4, audit_receipt_id: null, recovery_action: null });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText(/Invocation accepted as task task-delayed/)).not.toBeInTheDocument();
  });

  it("does not apply a delayed schedule receipt after the owner session changes", async () => {
    vi.spyOn(procedureV2Api, "listSourceTasks").mockResolvedValue([]);
    const activeRoutine = {
      id: "routine-1", owner_principal_id: "operator:one", state: "active", revision: 4, current_version: 1, name: "Delayed schedule",
      versions: [{ id: "version-1", routine_id: "routine-1", version: 1, workflow_sha256: digest, runbook_sha256: digest, installed_package_digest: digest, source_provenance: {}, source_repository: null, source_action: null, source_issue_number: null, created_at: preview.expires_at, installed_at: preview.expires_at }],
      package: { status: "active", digest, review_id: "review-1" },
    } as ProcedureV2Routine;
    vi.spyOn(procedureV2Api, "getRoutine").mockResolvedValue(activeRoutine);
    let resolveSchedule: (value: ProcedureV2ScheduleReceipt) => void = () => undefined;
    const delayedSchedule = new Promise<ProcedureV2ScheduleReceipt>((resolve) => { resolveSchedule = resolve; });
    const scheduleCall = vi.spyOn(procedureV2Api, "schedule").mockReturnValue(delayedSchedule);
    window.sessionStorage.setItem("seraph.procedure-v2.prepared:operator%3Aone:session-1", JSON.stringify({ schema_version: 1, routineId: "routine-1", bindingId: "binding-1", revision: 1, version: 1, installJobId: "install-1", approvalId: "approval-1" }));
    const view = render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-1" goals={[goal]} />);
    await screen.findByText("routine-1");
    fireEvent.change(screen.getByLabelText("Schedule expiry"), { target: { value: "2026-10-05T09:00" } });
    fireEvent.click(screen.getByRole("button", { name: "Create finite schedule" }));
    await waitFor(() => expect(scheduleCall).toHaveBeenCalledTimes(1));
    view.rerender(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-2" goals={[goal]} />);
    await waitFor(() => expect(screen.queryByText("binding-1")).not.toBeInTheDocument());
    resolveSchedule({ status: "scheduled", scheduled_job_id: "schedule-delayed", binding_id: "schedule-binding-delayed", revision: 1, action_type: "guardian.run_procedure.v2", routine_id: "routine-1", version: 1, template_id: "public-browser-check", goal_id: "goal-1", goal_revision: 4, schedule_idempotency_key: "schedule-delayed", input_digest: digest, next_run: "2026-10-02T09:00:00Z", expires_at: "2026-10-05T09:00:00Z", state: "active", pause_route: "/api/governed-schedules/schedule-binding-delayed", recovery_action: null });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText(/Schedule scheduled: schedule-delayed/)).not.toBeInTheDocument();
  });

  it("retains the exact invocation request when a receipt belongs to another goal", async () => {
    vi.spyOn(procedureV2Api, "listSourceTasks").mockResolvedValue([{
      task_id: sourceTask.task_id,
      title: sourceTask.title,
      status: sourceTask.status,
      capability_id: sourceTask.capability_id,
      goal_id: sourceTask.goal_id,
      goal_revision: sourceTask.goal_revision,
      task_revision: sourceTask.task_revision,
      readback_status: sourceTask.readback_status,
      verification_status: sourceTask.verification_status,
    }]);
    vi.spyOn(procedureV2Api, "previewFromTasks").mockResolvedValue(preview);
    vi.spyOn(procedureV2Api, "prepareFromTasks").mockResolvedValue({
      status: "prepared",
      binding_id: "binding-1",
      routine_id: "routine-1",
      version_id: "version-1",
      version: 1,
      schema_version: 2,
      template_id: "public-browser-check",
      revision: 1,
      request_digest: digest,
      preview_digest: digest,
      preview_expires_at: preview.expires_at,
      install_job_id: "install-1",
      approval_id: "approval-1",
      install_approval_status: "pending",
      install_approval_expires_at: "2026-10-02T12:15:00Z",
    });
    const activeRoutine = {
      id: "routine-1",
      owner_principal_id: "operator:one",
      state: "active",
      revision: 4,
      current_version: 1,
      name: "Correlated invocation",
      versions: [{ id: "version-1", routine_id: "routine-1", version: 1, workflow_sha256: digest, runbook_sha256: digest, installed_package_digest: digest, source_provenance: {}, source_repository: null, source_action: null, source_issue_number: null, created_at: preview.expires_at, installed_at: preview.expires_at }],
      package: { status: "active", digest, review_id: "review-1" },
    } as ProcedureV2Routine;
    vi.spyOn(procedureV2Api, "getRoutine").mockResolvedValue(activeRoutine);
    const invokeCall = vi.spyOn(procedureV2Api, "invoke").mockImplementation(async (_routineId, request) => ({
      status: "accepted",
      routine_id: "routine-1",
      version: request.version,
      schema_version: 2,
      template_id: "public-browser-check",
      invocation_uuid: request.invocation_uuid,
      scope: "procedure-v2:routine-1:version-1",
      goal_id: request.goal_id,
      goal_revision: request.expected_goal_revision + 1,
      task_id: "task-other-goal",
      attempt_id: null,
      job_id: null,
      input_artifact_id: "artifact-other-goal",
      input_digest: digest,
      plan_digest: digest,
      revision: 4,
      audit_receipt_id: null,
      recovery_action: null,
    }));

    render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-1" selectedSourceTask={sourceTask} goals={[goal]} />);
    fireEvent.change(screen.getByLabelText("Procedure name"), { target: { value: "Correlated invocation" } });
    fireEvent.click(screen.getByRole("button", { name: "Preview fixed procedure" }));
    await screen.findByRole("region", { name: "Procedure preview" });
    fireEvent.click(screen.getByRole("button", { name: "Prepare this reviewed version" }));
    await screen.findByText("routine-1");
    fireEvent.click(screen.getByRole("button", { name: "Invoke for this goal" }));
    await screen.findByText(/invocation receipt does not match the exact governed request/);
    expect(invokeCall).toHaveBeenCalledTimes(1);
    expect(window.sessionStorage.getItem("seraph.procedure-v2.pending:operator%3Aone:session-1")).not.toBeNull();
    expect(screen.queryByText(/Invocation accepted as task task-other-goal/)).not.toBeInTheDocument();
  });

  it("selects an owner source watch and submits the server current revision", async () => {
    vi.spyOn(procedureV2Api, "listSourceTasks").mockResolvedValue([]);
    vi.spyOn(procedureV2Api, "listSourceWatches").mockResolvedValue([{
      id: "watch-foreign",
      owner_principal_id: "operator:other",
      owner_session_id: "session-other",
      goal_id: "goal-1",
      goal_revision: 4,
      plan_revision: 8,
      state: "active",
      last_status: "material_change",
    }, {
      id: "watch-missing-owner",
      goal_id: "goal-1",
      goal_revision: 4,
      plan_revision: 9,
      state: "active",
      last_status: "material_change",
    }, {
      id: "watch-1",
      owner_principal_id: "operator:one",
      owner_session_id: "session-1",
      goal_id: "goal-1",
      goal_revision: 4,
      plan_revision: 7,
      state: "active",
      last_status: "material_change",
    }]);
    const activeRoutine = {
      id: "routine-1", owner_principal_id: "operator:one", state: "active", revision: 4, current_version: 1, name: "Watch procedure",
      versions: [{ id: "version-1", routine_id: "routine-1", version: 1, workflow_sha256: digest, runbook_sha256: digest, installed_package_digest: digest, source_provenance: {}, source_repository: null, source_action: null, source_issue_number: null, created_at: preview.expires_at, installed_at: preview.expires_at }],
      package: { status: "active", digest, review_id: "review-1" },
    } as ProcedureV2Routine;
    vi.spyOn(procedureV2Api, "getRoutine").mockResolvedValue(activeRoutine);
    const invokeCall = vi.spyOn(procedureV2Api, "invoke").mockImplementation(async (_routineId, request) => ({ status: "accepted", routine_id: "routine-1", version: 1, schema_version: 2, template_id: "watch-and-public-browser", invocation_uuid: request.invocation_uuid, scope: "procedure-v2:routine-1:version-1", goal_id: request.goal_id, goal_revision: request.expected_goal_revision, task_id: "task-watch", attempt_id: null, job_id: null, input_artifact_id: "input-watch", input_digest: digest, plan_digest: digest, revision: 4, audit_receipt_id: null, recovery_action: null }));
    window.sessionStorage.setItem("seraph.procedure-v2.prepared:operator%3Aone:session-1", JSON.stringify({ schema_version: 1, routineId: "routine-1", bindingId: "binding-1", revision: 1, version: 1, installJobId: "install-1", approvalId: "approval-1" }));
    render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-1" goals={[goal]} />);
    await screen.findByText("routine-1");
    fireEvent.change(screen.getByLabelText("Procedure template"), { target: { value: "watch-and-public-browser" } });
    await screen.findByRole("option", { name: /watch-1.*current watch revision 7/ });
    expect(screen.queryByRole("option", { name: /watch-foreign/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("option", { name: /watch-missing-owner/ })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Expected watch revision")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Source watch"), { target: { value: "watch-1" } });
    fireEvent.click(screen.getByRole("button", { name: "Invoke for this goal" }));
    await waitFor(() => expect(invokeCall).toHaveBeenCalledWith("routine-1", expect.objectContaining({
      expected_goal_revision: 4,
      parameters: { goal_id: "goal-1", expected_goal_revision: 4, source_watch_id: "watch-1", expected_watch_revision: 7 },
    })));
  });

  it("uses the typed calendar connection, consent, and event picker for the exact M5 input", async () => {
    vi.spyOn(procedureV2Api, "listSourceTasks").mockResolvedValue([]);
    const connection = { connection_id: "connection-1", service: "calendar_readonly" as const, label: "Work", credential_fingerprint: digest, state: "active" as const, revision: 2, created_at: preview.expires_at, updated_at: preview.expires_at };
    vi.spyOn(calendarApi, "listCalendarConnections").mockResolvedValue([connection]);
    vi.spyOn(calendarApi, "verifyCalendarConnection").mockResolvedValue({ connection, calendars: [{ calendar_id: "calendar-1", summary: "Work" }], calendar_list_revision: digest, pages_read: 1, truncated: false, provider_status: "verified" });
    vi.spyOn(calendarApi, "createReadConsent").mockResolvedValue({ consent_id: "consent-1", connection_id: "connection-1", connection_revision: 2, goal_id: "goal-1", goal_revision: 4, allowed_fields: ["summary", "start", "end"], window_minutes: 1440, max_events: 20, allow_remote_model: true, expires_at: "2026-10-02T09:00:00Z", state: "active", revision: 1, consent_digest: digest, created_at: preview.expires_at, updated_at: preview.expires_at });
    vi.spyOn(calendarApi, "listCalendarEvents").mockResolvedValue({ events: [{ event_binding_id: "event-binding-1", event_binding_revision: 3, event_key: digest, event_revision: digest, calendar_list_revision: digest, summary: "Launch planning", start: "2026-10-01T12:00:00Z", end: "2026-10-01T13:00:00Z", location: null, description: null, attendees: null }], consent_id: "consent-1", consent_revision: 1, connection_revision: 2, calendar_list_revision: digest, fetched_at: preview.expires_at, pages_read: 1, truncated: false });
    const activeRoutine = {
      id: "routine-1", owner_principal_id: "operator:one", state: "active", revision: 4, current_version: 1, name: "Meeting procedure",
      versions: [{ id: "version-1", routine_id: "routine-1", version: 1, workflow_sha256: digest, runbook_sha256: digest, installed_package_digest: digest, source_provenance: {}, source_repository: null, source_action: null, source_issue_number: null, created_at: preview.expires_at, installed_at: preview.expires_at }],
      package: { status: "active", digest, review_id: "review-1" },
    } as ProcedureV2Routine;
    vi.spyOn(procedureV2Api, "getRoutine").mockResolvedValue(activeRoutine);
    const invokeCall = vi.spyOn(procedureV2Api, "invoke").mockImplementation(async (_routineId, request) => ({ status: "accepted", routine_id: "routine-1", version: 1, schema_version: 2, template_id: "selected-meeting-prep", invocation_uuid: request.invocation_uuid, scope: "procedure-v2:routine-1:version-1", goal_id: request.goal_id, goal_revision: request.expected_goal_revision, task_id: "task-meeting", attempt_id: null, job_id: null, input_artifact_id: "input-meeting", input_digest: digest, plan_digest: digest, revision: 4, audit_receipt_id: null, recovery_action: null }));
    window.sessionStorage.setItem("seraph.procedure-v2.prepared:operator%3Aone:session-1", JSON.stringify({ schema_version: 1, routineId: "routine-1", bindingId: "binding-1", revision: 1, version: 1, installJobId: "install-1", approvalId: "approval-1" }));
    render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-1" goals={[goal]} />);
    await screen.findByText("routine-1");
    fireEvent.change(screen.getByLabelText("Procedure template"), { target: { value: "selected-meeting-prep" } });
    await screen.findByRole("option", { name: /Work · revision 2/ });
    fireEvent.click(screen.getByRole("button", { name: "Verify and list calendars" }));
    await screen.findByRole("option", { name: "Work" });
    fireEvent.click(screen.getByLabelText("Allow governed calendar model"));
    fireEvent.click(screen.getByRole("button", { name: "Create typed consent and load events" }));
    await screen.findByRole("option", { name: /Launch planning/ });
    fireEvent.change(screen.getByLabelText("Returned meeting"), { target: { value: "event-binding-1" } });
    fireEvent.change(screen.getByLabelText("Meeting purpose"), { target: { value: "Prepare the launch" } });
    fireEvent.click(screen.getByRole("button", { name: "Invoke for this goal" }));
    await waitFor(() => expect(invokeCall).toHaveBeenCalledWith("routine-1", expect.objectContaining({
      parameters: {
        schema_version: 1,
        consent_id: "consent-1",
        event_binding_id: "event-binding-1",
        expected_event_binding_revision: 3,
        expected_consent_revision: 1,
        expected_connection_revision: 2,
        event_revision: digest,
        calendar_list_revision: digest,
        goal_id: "goal-1",
        goal_revision: 4,
        purpose: "Prepare the launch",
      },
    })));
    expect(invokeCall.mock.calls[0]?.[1].parameters).not.toHaveProperty("expected_goal_revision");
  });

  it("walks the public procedure through package governance, invocation, and finite schedule controls", async () => {
    let resolveInitialList!: (values: ProcedureV2Routine[]) => void;
    vi.mocked(procedureV2Api.listRoutines).mockReturnValueOnce(new Promise((resolve) => { resolveInitialList = resolve; }));
    vi.spyOn(procedureV2Api, "listSourceTasks").mockResolvedValue([{
      task_id: sourceTask.task_id,
      title: sourceTask.title,
      status: sourceTask.status,
      capability_id: sourceTask.capability_id,
      goal_id: sourceTask.goal_id,
      goal_revision: sourceTask.goal_revision,
      task_revision: sourceTask.task_revision,
      readback_status: sourceTask.readback_status,
      verification_status: sourceTask.verification_status,
    }]);
    vi.spyOn(procedureV2Api, "previewFromTasks").mockResolvedValue(preview);
    vi.spyOn(procedureV2Api, "prepareFromTasks").mockResolvedValue({
      status: "prepared",
      binding_id: "binding-1",
      routine_id: "routine-1",
      version_id: "version-1",
      version: 1,
      schema_version: 2,
      template_id: "public-browser-check",
      revision: 1,
      request_digest: digest,
      preview_digest: digest,
      preview_expires_at: preview.expires_at,
      install_job_id: "routine-install:routine-1:v1",
      approval_id: "approval-install-1",
      install_approval_status: "approved",
      install_approval_expires_at: "2026-10-02T12:15:00Z",
    });
    const preparedRoutine = {
      id: "routine-1",
      owner_principal_id: "operator:one",
      state: "prepared",
      revision: 1,
      current_version: 1,
      name: "Public status check",
      versions: [{ id: "version-1", routine_id: "routine-1", version: 1, workflow_sha256: digest, runbook_sha256: digest, installed_package_digest: null, source_provenance: {}, source_repository: null, source_action: null, source_issue_number: null, created_at: preview.expires_at, installed_at: null }],
      package: { status: "not_installed", digest: null, review_id: null },
    } as ProcedureV2Routine;
    preparedRoutine.versions[0].template_id = "public-browser-check";
    preparedRoutine.versions[0].procedure_binding = {
      binding_id: "binding-1",
      revision: 1,
      install_job_id: "routine-install:routine-1:v1",
      approval_id: "approval-install-1",
      install_approval_status: "approved",
      install_approval_expires_at: "2026-10-02T12:15:00Z",
    } as NonNullable<ProcedureV2Routine["versions"][number]["procedure_binding"]>;
    const installedRoutine = {
      ...preparedRoutine,
      state: "installed",
      revision: 2,
      package: { status: "not_installed", digest: null, review_id: null },
      versions: [{ ...preparedRoutine.versions[0], installed_package_digest: digest, installed_at: preview.expires_at }],
    } as ProcedureV2Routine;
    const packageReadyRoutine = {
      ...preparedRoutine,
      state: "installed",
      // Package activation changes the external package pointer; the routine
      // row revision remains the guarded install revision.
      revision: 2,
      package: { status: "active", digest, review_id: "review-1" },
      versions: [{ ...preparedRoutine.versions[0], installed_package_digest: digest, installed_at: preview.expires_at }],
    } as ProcedureV2Routine;
    const activeRoutine = {
      ...preparedRoutine,
      state: "active",
      revision: 3,
      package: { status: "active", digest, review_id: "review-1" },
      versions: [{ ...preparedRoutine.versions[0], installed_package_digest: digest, installed_at: preview.expires_at }],
    } as ProcedureV2Routine;
    vi.spyOn(procedureV2Api, "getRoutine").mockResolvedValueOnce(preparedRoutine).mockResolvedValueOnce(preparedRoutine).mockResolvedValue(packageReadyRoutine);
    const lifecycle = vi.spyOn(procedureV2Api, "lifecycle").mockImplementation(async (_routineId, action) => action === "activate" ? activeRoutine : installedRoutine);
    vi.spyOn(procedureV2Api, "packagePreview").mockResolvedValue({
      routine_id: "routine-1", version: 1, pack_id: "pack-1", digest, installed_package_digest: digest, review_id: null, status: "reviewed",
      manifest: { display_name: "Public status package", summary: "Fixed browser procedure", version: "1", authority: { tools: ["browser.public-task.v1"], filesystem: [], network: false, secrets: [], approval: "operator" }, resources: { max_runtime_seconds: 300, max_artifact_bytes: 1000, max_inference_cost_microusd: 0, inference_priority: "normal" }, data_policy: { classes: ["public"], egress: ["none"] } },
      runbook: { title: "Public status", summary: "Fixed", procedure: { capability_id: "guardian-routine.v2", steps: [{ id: "public_browser_check", capability: "browser.public-task.v1", tool: "browser" }] }, bindings: { workflow_sha256: digest, legacy_runbook_sha256: digest, source_provenance_sha256: digest } },
    });
    vi.spyOn(procedureV2Api, "packageReview").mockResolvedValue({ digest, review: { review_id: "review-1", status: "approved" } });
    vi.spyOn(procedureV2Api, "preparePackageApproval").mockResolvedValue({ digest, approval: { approval_id: "approval-1", status: "pending", action: "activate", pack_id: "pack-1", version: "1", digest, goal_id: "goal-1" } });
    vi.spyOn(procedureV2Api, "decidePackageApproval").mockResolvedValue({ digest, approval: { approval_id: "approval-1", status: "approved", action: "activate", pack_id: "pack-1", version: "1", digest, goal_id: "goal-1" } });
    vi.spyOn(procedureV2Api, "activatePackage").mockResolvedValue({ digest, status: "active" });
    const invokeCall = vi.spyOn(procedureV2Api, "invoke").mockImplementation(async (_routineId, request) => ({ status: "accepted", routine_id: "routine-1", version: 1, schema_version: 2, template_id: "public-browser-check", invocation_uuid: request.invocation_uuid, scope: "procedure-v2:routine-1:version-1", goal_id: request.goal_id, goal_revision: request.expected_goal_revision, task_id: "task-2", attempt_id: null, job_id: null, input_artifact_id: "input-2", input_digest: digest, plan_digest: digest, revision: 4, audit_receipt_id: "receipt-2", recovery_action: null }));
    const schedule: ProcedureV2ScheduleReceipt = { status: "scheduled", scheduled_job_id: "schedule-1", binding_id: "schedule-binding-1", revision: 1, action_type: "guardian.run_procedure.v2", routine_id: "routine-1", version: 1, template_id: "public-browser-check", goal_id: "goal-1", goal_revision: 4, schedule_idempotency_key: "schedule-1", input_digest: digest, next_run: "2026-10-02T09:00:00Z", expires_at: "2026-10-05T09:00:00Z", state: "active", pause_route: "/api/governed-schedules/schedule-binding-1", recovery_action: null };
    const scheduleCall = vi.spyOn(procedureV2Api, "schedule")
      .mockImplementationOnce(async (_routineId, request) => ({ ...schedule, status: "unknown", state: "unknown", schedule_idempotency_key: request.idempotency_key }))
      .mockImplementationOnce(async (_routineId, request) => ({ ...schedule, status: "blocked", state: "blocked", schedule_idempotency_key: request.idempotency_key }))
      .mockImplementationOnce(async (_routineId, request) => ({ ...schedule, schedule_idempotency_key: request.idempotency_key }));
    const scheduleControl = vi.spyOn(procedureV2Api, "scheduleControl").mockResolvedValue({ ...schedule, status: "paused", state: "paused", revision: 2 });

    let view = render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-1" selectedSourceTask={sourceTask} goals={[goal]} pendingApprovals={[{ id: "approval-install-1", status: "pending", tool_name: "guardian:routine-install", summary: "Install reviewed package" }]} />);
    fireEvent.change(screen.getByLabelText("Procedure name"), { target: { value: "Public status check" } });
    fireEvent.click(screen.getByRole("button", { name: "Preview fixed procedure" }));
    await screen.findByRole("region", { name: "Procedure preview" });
    fireEvent.click(screen.getByRole("button", { name: "Prepare this reviewed version" }));
    await screen.findByText("binding-1");
    expect(screen.getByRole("option", { name: "Public status check · prepared · current v1" })).toHaveValue("routine-1");
    expect(procedureV2Api.listRoutines).toHaveBeenCalledTimes(1);
    await act(async () => { resolveInitialList([]); });
    expect(screen.getByRole("option", { name: "Public status check · prepared · current v1" })).toHaveValue("routine-1");
    view.unmount();
    view = render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-1" selectedSourceTask={sourceTask} goals={[goal]} pendingApprovals={[]} />);
    await screen.findByText("binding-1");
    expect(screen.getByText("approval-install-1")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Preview reviewed package" }));
    await screen.findByRole("region", { name: "Procedure package preview" });
    fireEvent.click(screen.getByRole("button", { name: "Install with exact approval" }));
    await waitFor(() => expect(procedureV2Api.lifecycle).toHaveBeenCalledWith("routine-1", "install", expect.objectContaining({ approval_id: "approval-install-1" })));
    fireEvent.click(screen.getByRole("button", { name: "Record package review" }));
    fireEvent.click(await screen.findByRole("button", { name: "Prepare activation approval" }));
    fireEvent.click(await screen.findByRole("button", { name: "Approve activation" }));
    fireEvent.click(await screen.findByRole("button", { name: "Activate reviewed package" }));
    await waitFor(() => expect(screen.getByText(/The reviewed package is active/)).toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Invoke for this goal" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Activate procedure" }));
    await waitFor(() => expect(lifecycle).toHaveBeenCalledWith("routine-1", "activate", {
      version: 1,
      expected_routine_revision: 2,
    }));
    fireEvent.click(screen.getByRole("button", { name: "Invoke for this goal" }));
    await waitFor(() => expect(invokeCall).toHaveBeenCalledWith("routine-1", expect.objectContaining({ parameters: { goal_id: "goal-1", expected_goal_revision: 4 } })));
    fireEvent.change(screen.getByLabelText("Schedule expiry"), { target: { value: "2026-10-05T09:00" } });
    fireEvent.click(screen.getByRole("button", { name: "Create finite schedule" }));
    await screen.findByText(/Schedule unknown/);
    const firstScheduleRequest = scheduleCall.mock.calls[0]?.[1];
    fireEvent.click(screen.getByRole("button", { name: "Retry exact request" }));
    await waitFor(() => expect(scheduleCall).toHaveBeenCalledTimes(2));
    expect(scheduleCall.mock.calls[1]?.[1]).toEqual(firstScheduleRequest);
    await screen.findByText(/finite schedule outcome (?:is|remains) blocked/);
    expect(window.sessionStorage.getItem("seraph.procedure-v2.pending:operator%3Aone:session-1")).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Retry exact request" }));
    await waitFor(() => expect(scheduleCall).toHaveBeenCalledTimes(3));
    expect(scheduleCall.mock.calls[2]?.[1]).toEqual(firstScheduleRequest);
    await screen.findByText(/Schedule scheduled/);
    fireEvent.click(screen.getByRole("button", { name: "Pause schedule" }));
    await waitFor(() => expect(scheduleControl).toHaveBeenCalledWith("schedule-binding-1", expect.objectContaining({ action: "pause", expected_binding_revision: 1 })));
  });

  it("requires an explicit resume for a paused procedure and reconciles an unverified activation response", async () => {
    const pausedRoutine = { ...existingRoutine, state: "paused" } as ProcedureV2Routine;
    const getRoutine = vi.spyOn(procedureV2Api, "getRoutine").mockResolvedValue(pausedRoutine);
    vi.spyOn(procedureV2Api, "listRoutines").mockResolvedValue([pausedRoutine]);
    const lifecycle = vi.spyOn(procedureV2Api, "lifecycle").mockResolvedValue({
      ...pausedRoutine,
      state: "active",
      current_version: 1,
      revision: pausedRoutine.revision + 1,
    });

    render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-1" goals={[goal]} />);
    const selector = await screen.findByLabelText("Existing reviewed procedure");
    fireEvent.change(selector, { target: { value: pausedRoutine.id } });
    await waitFor(() => expect(getRoutine).toHaveBeenCalledWith(pausedRoutine.id));
    expect(await screen.findByRole("button", { name: "Resume procedure" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Invoke for this goal" })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: "Resume procedure" }));
    await waitFor(() => expect(lifecycle).toHaveBeenCalledWith(pausedRoutine.id, "activate", {
      version: 2,
      expected_routine_revision: pausedRoutine.revision,
    }));
    expect(lifecycle).toHaveBeenCalledTimes(1);
    expect(await screen.findByText(/activation outcome could not be verified/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Resume procedure" })).toBeDisabled();

    getRoutine.mockResolvedValueOnce({ ...pausedRoutine, state: "active", current_version: 2, revision: pausedRoutine.revision + 1 });
    fireEvent.click(screen.getByRole("button", { name: "Refresh authority" }));
    await waitFor(() => expect(getRoutine).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("button", { name: "Resume procedure" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Invoke for this goal" })).not.toBeDisabled();
  });

  it("holds invocation after an unknown pause until an exact paused readback", async () => {
    const activeRoutine = { ...existingRoutine, state: "active" } as ProcedureV2Routine;
    const pausedReadback = { ...activeRoutine, state: "paused", revision: activeRoutine.revision + 1 } as ProcedureV2Routine;
    const getRoutine = vi.spyOn(procedureV2Api, "getRoutine").mockResolvedValue(activeRoutine);
    vi.spyOn(procedureV2Api, "listRoutines").mockResolvedValue([activeRoutine]);
    const lifecycle = vi.spyOn(procedureV2Api, "lifecycle").mockRejectedValue(new ProcedureV2ApiError(503, {
      code: "procedure_outcome_unknown",
      message: "The pause receipt was lost",
      recovery_action: "refresh_procedure",
      retryable: true,
      binding_id: null,
      audit_receipt_id: null,
    }));

    render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-1" goals={[goal]} />);
    fireEvent.change(await screen.findByLabelText("Existing reviewed procedure"), { target: { value: activeRoutine.id } });
    await screen.findByRole("button", { name: "Pause future invocations" });
    expect(screen.getByRole("button", { name: "Invoke for this goal" })).toBeEnabled();

    fireEvent.click(screen.getByRole("button", { name: "Pause future invocations" }));
    await screen.findByText(/pause outcome could not be verified/i);
    expect(lifecycle).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Pause future invocations" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Invoke for this goal" })).toBeDisabled();

    getRoutine.mockResolvedValueOnce(pausedReadback);
    fireEvent.click(screen.getByRole("button", { name: "Refresh authority" }));
    await waitFor(() => expect(getRoutine).toHaveBeenCalledTimes(2));
    expect(await screen.findByRole("button", { name: "Resume procedure" })).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText(/last lifecycle request may have reached/i)).not.toBeInTheDocument());
    expect(lifecycle).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["install", "prepared", "installed", 1, 1],
    ["activate", "installed", "active", 2, 2],
    ["pause", "active", "paused", 2, 2],
    ["revoke", "active", "revoked", 2, 2],
    ["rollback", "active", "active", 1, 2],
  ] as const)("retains an unknown %s across a same-owner remount and reconciles only its exact readback", async (action, beforeState, afterState, targetVersion, beforeCurrentVersion) => {
    const targetVersionId = targetVersion === 1 ? "version-existing-1" : "version-existing-2";
    const beforeVersions = existingRoutine.versions.map((version) => version.version === 1 && action === "install"
      ? {
        ...version,
        installed_package_digest: null,
        installed_at: null,
        procedure_binding: {
          ...version.procedure_binding,
          approval_id: "approval-install-1",
          install_job_id: `install-${targetVersion}`,
          install_approval_status: "approved",
          install_approval_expires_at: "2026-10-02T12:15:00Z",
        },
      }
      : { ...version });
    const beforeRoutine = {
      ...existingRoutine,
      state: beforeState,
      revision: 5,
      current_version: beforeCurrentVersion,
      versions: beforeVersions,
      package: action === "install" ? { status: "not_installed", digest: null, review_id: null } : existingRoutine.package,
    } as ProcedureV2Routine;
    const afterRoutine = {
      ...beforeRoutine,
      state: afterState,
      revision: 6,
      current_version: targetVersion,
      versions: beforeVersions.map((version) => version.id === targetVersionId
        ? { ...version, installed_package_digest: digest, installed_at: "2026-10-01T12:20:00Z" }
        : version),
      package: action === "install" ? { status: "not_installed", digest: null, review_id: null } : existingRoutine.package,
    } as ProcedureV2Routine;
    const getRoutine = vi.spyOn(procedureV2Api, "getRoutine").mockResolvedValue(beforeRoutine);
    vi.spyOn(procedureV2Api, "listRoutines").mockResolvedValue([beforeRoutine]);
    const lifecycle = vi.spyOn(procedureV2Api, "lifecycle");
    const lifecycleRecovery = {
      schema_version: 1,
      action,
      ownerPrincipalId: "operator:one",
      ownerSessionId: "session-1",
      routineId: existingRoutine.id,
      versionId: targetVersionId,
      version: targetVersion,
      requestRevision: 5,
      expectedState: afterState,
      approvalId: action === "install" ? "approval-install-1" : null,
      installJobId: action === "install" ? `install-${targetVersion}` : null,
      targetVersionId: action === "rollback" ? targetVersionId : null,
      targetVersion: action === "rollback" ? targetVersion : null,
      packageDigest: digest,
    };
    window.sessionStorage.setItem("seraph.procedure-v2.prepared:operator%3Aone:session-1", JSON.stringify({
      schema_version: 1,
      routineId: existingRoutine.id,
      bindingId: targetVersionId === "version-existing-1" ? "binding-existing-1" : "binding-existing-2",
      revision: 5,
      version: targetVersion,
      versionId: targetVersionId,
      installJobId: `install-${targetVersion}`,
      approvalId: "approval-install-1",
      lifecycleRecovery,
    }));

    const first = render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-1" goals={[goal]} />);
    await screen.findByText(new RegExp(`Procedure ${action} is unverified`));
    expect(lifecycle).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Refresh authority" })).toBeEnabled();
    first.unmount();

    const second = render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-1" goals={[goal]} />);
    await screen.findByText(new RegExp(`Procedure ${action} is unverified`));
    expect(lifecycle).not.toHaveBeenCalled();
    getRoutine.mockResolvedValueOnce(afterRoutine);
    fireEvent.click(screen.getByRole("button", { name: "Refresh authority" }));
    await waitFor(() => expect(getRoutine).toHaveBeenCalledTimes(3));
    await waitFor(() => expect(screen.queryByText(new RegExp(`Procedure ${action} is unverified`))).not.toBeInTheDocument());
    expect(JSON.parse(window.sessionStorage.getItem("seraph.procedure-v2.prepared:operator%3Aone:session-1") ?? "null")).toMatchObject({ lifecycleRecovery: null });
    expect(lifecycle).not.toHaveBeenCalled();
    second.unmount();
  });

  it("does not clear install recovery when the readback changes its exact binding", async () => {
    const beforeRoutine = {
      ...existingRoutine,
      state: "prepared",
      revision: 5,
      current_version: 1,
      package: { status: "not_installed", digest: null, review_id: null },
      versions: existingRoutine.versions.map((version) => version.version === 1
        ? {
          ...version,
          installed_package_digest: null,
          installed_at: null,
          procedure_binding: {
            ...version.procedure_binding,
            state: "prepared",
            revision: 5,
            install_job_id: "install-exact",
            approval_id: "approval-exact",
            install_approval_status: "approved",
            install_approval_expires_at: "2026-10-02T12:15:00Z",
          },
        }
        : version),
    } as ProcedureV2Routine;
    const forgedReadback = {
      ...beforeRoutine,
      state: "installed",
      revision: 6,
      versions: beforeRoutine.versions.map((version) => version.version === 1
        ? {
          ...version,
          installed_package_digest: digest,
          installed_at: "2026-10-01T12:30:00Z",
          procedure_binding: { ...version.procedure_binding, install_job_id: "install-forged" },
        }
        : version),
    } as ProcedureV2Routine;
    const getRoutine = vi.spyOn(procedureV2Api, "getRoutine").mockResolvedValueOnce(beforeRoutine).mockResolvedValueOnce(forgedReadback);
    vi.spyOn(procedureV2Api, "listRoutines").mockResolvedValue([beforeRoutine]);
    const lifecycle = vi.spyOn(procedureV2Api, "lifecycle");
    const recovery = {
      schema_version: 1,
      action: "install",
      ownerPrincipalId: "operator:one",
      ownerSessionId: "session-1",
      routineId: beforeRoutine.id,
      versionId: "version-existing-1",
      version: 1,
      requestRevision: 5,
      expectedState: "installed",
      approvalId: "approval-exact",
      installJobId: "install-exact",
      targetVersionId: null,
      targetVersion: null,
      packageDigest: digest,
    };
    window.sessionStorage.setItem("seraph.procedure-v2.prepared:operator%3Aone:session-1", JSON.stringify({
      schema_version: 1,
      routineId: beforeRoutine.id,
      bindingId: "binding-existing-1",
      revision: 5,
      version: 1,
      versionId: "version-existing-1",
      installJobId: "install-exact",
      approvalId: "approval-exact",
      lifecycleRecovery: recovery,
    }));

    render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-1" goals={[goal]} />);
    await screen.findByText(/Procedure install is unverified/);
    fireEvent.click(screen.getByRole("button", { name: "Refresh authority" }));
    await screen.findByText(/did not prove the requested procedure install/i);
    expect(getRoutine).toHaveBeenCalledTimes(2);
    expect(lifecycle).not.toHaveBeenCalled();
    expect(JSON.parse(window.sessionStorage.getItem("seraph.procedure-v2.prepared:operator%3Aone:session-1") ?? "null")).toMatchObject({ lifecycleRecovery: recovery });
  });

  it("persists lifecycle recovery before dispatch and keeps it after an unmounted response", async () => {
    const preparedRoutine = {
      ...existingRoutine,
      state: "prepared",
      revision: 5,
      current_version: 1,
      package: { status: "not_installed", digest: null, review_id: null },
      versions: existingRoutine.versions.map((version) => version.version === 1
        ? { ...version, installed_package_digest: null, installed_at: null, procedure_binding: { ...version.procedure_binding, approval_id: "approval-install-1", install_job_id: "routine-install:routine-existing:v1", install_approval_status: "approved", install_approval_expires_at: "2026-10-02T12:15:00Z" } }
        : version),
    } as ProcedureV2Routine;
    vi.spyOn(procedureV2Api, "listRoutines").mockResolvedValue([preparedRoutine]);
    vi.spyOn(procedureV2Api, "getRoutine").mockResolvedValue(preparedRoutine);
    vi.spyOn(procedureV2Api, "packagePreview").mockResolvedValue({
      routine_id: preparedRoutine.id,
      version: 1,
      pack_id: "pack-install",
      digest,
      installed_package_digest: null,
      review_id: null,
      status: "not_reviewed",
      manifest: { display_name: "Install package", summary: "Fixed browser procedure", version: "1", authority: { tools: ["browser.public-task.v1"], filesystem: [], network: false, secrets: [], approval: "operator" }, resources: { max_runtime_seconds: 300, max_artifact_bytes: 1000, max_inference_cost_microusd: 0, inference_priority: "normal" }, data_policy: { classes: ["public"], egress: ["none"] } },
      runbook: { title: "Public status", summary: "Fixed", procedure: { capability_id: "guardian-routine.v2", steps: [{ id: "public_browser_check", capability: "browser.public-task.v1", tool: "browser" }] }, bindings: { workflow_sha256: digest, legacy_runbook_sha256: digest, source_provenance_sha256: digest } },
    });
    let resolveLifecycle!: (routine: ProcedureV2Routine) => void;
    const lifecycle = vi.spyOn(procedureV2Api, "lifecycle").mockReturnValue(new Promise((resolve) => { resolveLifecycle = resolve; }));
    window.sessionStorage.setItem("seraph.procedure-v2.prepared:operator%3Aone:session-1", JSON.stringify({
      schema_version: 1,
      routineId: preparedRoutine.id,
      bindingId: "binding-existing-1",
      revision: 5,
      version: 1,
      versionId: "version-existing-1",
      installJobId: "routine-install:routine-existing:v1",
      approvalId: "approval-install-1",
    }));

    const view = render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-1" goals={[goal]} />);
    await screen.findByText(preparedRoutine.id);
    fireEvent.click(await screen.findByRole("button", { name: "Preview reviewed package" }));
    await screen.findByRole("region", { name: "Procedure package preview" });
    fireEvent.click(await screen.findByRole("button", { name: "Install with exact approval" }));
    await waitFor(() => expect(lifecycle).toHaveBeenCalledTimes(1));
    expect(JSON.parse(window.sessionStorage.getItem("seraph.procedure-v2.prepared:operator%3Aone:session-1") ?? "null")).toMatchObject({
      lifecycleRecovery: {
        schema_version: 1,
        action: "install",
        routineId: preparedRoutine.id,
        versionId: "version-existing-1",
        requestRevision: 5,
        expectedState: "installed",
      },
    });
    view.unmount();
    await act(async () => {
      resolveLifecycle({
        ...preparedRoutine,
        state: "installed",
        revision: 6,
        versions: preparedRoutine.versions.map((version) => version.version === 1 ? { ...version, installed_package_digest: digest, installed_at: "2026-10-01T12:30:00Z" } : version),
      });
    });
    expect(JSON.parse(window.sessionStorage.getItem("seraph.procedure-v2.prepared:operator%3Aone:session-1") ?? "null")).toMatchObject({ lifecycleRecovery: { action: "install" } });
  });

  it.each([
    ["a different version id", (routine: ProcedureV2Routine) => ({
      ...routine,
      versions: routine.versions.map((version) => version.version === 2 ? { ...version, id: "version-forged" } : version),
    })],
    ["the same revision", (routine: ProcedureV2Routine) => ({ ...routine, revision: routine.revision })],
    ["an older revision", (routine: ProcedureV2Routine) => ({ ...routine, revision: routine.revision - 1 })],
  ] as const)("does not accept activation readback with %s", async (_caseName, mutate) => {
    const pausedRoutine = { ...existingRoutine, state: "paused" } as ProcedureV2Routine;
    vi.spyOn(procedureV2Api, "getRoutine").mockResolvedValue(pausedRoutine);
    vi.spyOn(procedureV2Api, "listRoutines").mockResolvedValue([pausedRoutine]);
    const lifecycle = vi.spyOn(procedureV2Api, "lifecycle").mockResolvedValue({
      ...mutate(pausedRoutine),
      state: "active",
      current_version: 2,
    });

    render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-1" goals={[goal]} />);
    fireEvent.change(await screen.findByLabelText("Existing reviewed procedure"), { target: { value: pausedRoutine.id } });
    const resume = await screen.findByRole("button", { name: "Resume procedure" });
    fireEvent.click(resume);
    await waitFor(() => expect(lifecycle).toHaveBeenCalledWith(pausedRoutine.id, "activate", {
      version: 2,
      expected_routine_revision: pausedRoutine.revision,
    }));
    expect(await screen.findByText(/activation outcome could not be verified/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Resume procedure" })).toBeDisabled();
  });

  it("retains exact package activation context across same-owner remount and accepts only a newer active readback", async () => {
    const installedRoutine = {
      ...existingRoutine,
      state: "installed",
      revision: 7,
      current_version: 2,
      package: { status: "active", digest, review_id: "review-package" },
    } as ProcedureV2Routine;
    const activePackageReadback = { ...installedRoutine, revision: 7 } as ProcedureV2Routine;
    const getRoutine = vi.spyOn(procedureV2Api, "getRoutine").mockResolvedValue(installedRoutine);
    vi.spyOn(procedureV2Api, "listRoutines").mockResolvedValue([installedRoutine]);
    const packagePreview = {
      routine_id: installedRoutine.id,
      version: 2,
      pack_id: "pack-2",
      digest,
      installed_package_digest: digest,
      review_id: "review-package",
      status: "reviewed",
      manifest: { display_name: "Public status package", summary: "Fixed browser procedure", version: "2", authority: { tools: ["browser.public-task.v1"], filesystem: [], network: false, secrets: [], approval: "operator" }, resources: { max_runtime_seconds: 300, max_artifact_bytes: 1000, max_inference_cost_microusd: 0, inference_priority: "normal" }, data_policy: { classes: ["public"], egress: ["none"] } },
      runbook: { title: "Public status", summary: "Fixed", procedure: { capability_id: "guardian-routine.v2", steps: [{ id: "public_browser_check", capability: "browser.public-task.v1", tool: "browser" }] }, bindings: { workflow_sha256: digest, legacy_runbook_sha256: digest, source_provenance_sha256: digest } },
    } as WorkBoardRoutinePackagePreview;
    vi.spyOn(procedureV2Api, "packagePreview").mockResolvedValue(packagePreview);
    vi.spyOn(procedureV2Api, "preparePackageApproval").mockResolvedValue({ digest, approval: { approval_id: "package-approval-2", status: "pending", action: "activate", pack_id: "pack-2", version: "2", digest, goal_id: "goal-1" } });
    vi.spyOn(procedureV2Api, "decidePackageApproval").mockResolvedValue({ digest, approval: { approval_id: "package-approval-2", status: "approved", action: "activate", pack_id: "pack-2", version: "2", digest, goal_id: "goal-1" } });
    let rejectActivation!: (cause: unknown) => void;
    const activatePackage = vi.spyOn(procedureV2Api, "activatePackage").mockImplementation(() => new Promise((_, reject) => { rejectActivation = reject; }));
    window.sessionStorage.setItem("seraph.procedure-v2.prepared:operator%3Aone:session-1", JSON.stringify({ schema_version: 1, routineId: installedRoutine.id, bindingId: "binding-existing-2", revision: 4, version: 2, versionId: "version-existing-2", installJobId: "install-2", approvalId: "install-approval-2" }));

    const firstOwnerView = render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-1" goals={[goal]} />);
    await screen.findByText(installedRoutine.id);
    fireEvent.click(await screen.findByRole("button", { name: "Preview reviewed package" }));
    fireEvent.click(await screen.findByRole("button", { name: "Prepare activation approval" }));
    fireEvent.click(await screen.findByRole("button", { name: "Approve activation" }));
    fireEvent.click(await screen.findByRole("button", { name: "Activate reviewed package" }));
    await waitFor(() => expect(activatePackage).toHaveBeenCalledTimes(1));
    expect(JSON.parse(window.sessionStorage.getItem("seraph.procedure-v2.prepared:operator%3Aone:session-1") ?? "null")).toMatchObject({
      packageActivationRecovery: {
        schema_version: 1,
        routineId: installedRoutine.id,
        versionId: "version-existing-2",
        version: 2,
        digest,
        expectedRevision: 7,
        approvalId: "package-approval-2",
      },
    });
    rejectActivation(new ProcedureV2ApiError(503, { code: "procedure_outcome_unknown", message: "The package receipt was lost", recovery_action: "reconcile_existing_effect", retryable: false, binding_id: null, audit_receipt_id: null }));
    await screen.findByText(/package activation outcome could not be verified/i);
    expect(screen.getAllByText(/package-approval-2/).length).toBeGreaterThan(0);
    expect(screen.getByText(/expected revision 7/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Activate reviewed package" })).toBeDisabled();
    expect(JSON.parse(window.sessionStorage.getItem("seraph.procedure-v2.prepared:operator%3Aone:session-1") ?? "null")).toMatchObject({
      packageActivationRecovery: {
        schema_version: 1,
        routineId: installedRoutine.id,
        versionId: "version-existing-2",
        version: 2,
        digest,
        expectedRevision: 7,
        approvalId: "package-approval-2",
      },
    });

    expect(screen.getByRole("button", { name: "Refresh authority" })).toBeEnabled();
    firstOwnerView.unmount();
    const view = render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-1" goals={[goal]} />);
    await screen.findByText(/Package activation is unverified/);
    expect(screen.getByText(/package-approval-2/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Preview reviewed package" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Activate procedure" })).toBeDisabled();

    getRoutine.mockResolvedValueOnce(activePackageReadback);
    fireEvent.click(screen.getByRole("button", { name: "Refresh authority" }));
    await screen.findByText(/reviewed package activation is confirmed/i);
    expect(screen.queryByRole("button", { name: "Activate reviewed package" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Activate procedure" })).toBeEnabled();
    expect(getRoutine).toHaveBeenCalledTimes(3);
    view.unmount();
  });

  it("keeps package activation recovery blocked when readback is inactive", async () => {
    const installedRoutine = { ...existingRoutine, state: "installed", revision: 7, current_version: 2 } as ProcedureV2Routine;
    const inactiveReadback = { ...installedRoutine, package: { status: "not_installed", digest: null, review_id: null } } as ProcedureV2Routine;
    const recovery = {
      schema_version: 1,
      routineId: installedRoutine.id,
      versionId: "version-existing-2",
      version: 2,
      digest,
      expectedRevision: 7,
      approvalId: "package-approval-2",
    };
    const getRoutine = vi.spyOn(procedureV2Api, "getRoutine").mockResolvedValueOnce(installedRoutine).mockResolvedValueOnce(inactiveReadback);
    vi.spyOn(procedureV2Api, "listRoutines").mockResolvedValue([installedRoutine]);
    window.sessionStorage.setItem("seraph.procedure-v2.prepared:operator%3Aone:session-1", JSON.stringify({
      schema_version: 1,
      routineId: installedRoutine.id,
      bindingId: "binding-existing-2",
      revision: 4,
      version: 2,
      versionId: "version-existing-2",
      installJobId: "install-2",
      approvalId: "install-approval-2",
      packageActivationRecovery: recovery,
    }));

    render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-1" goals={[goal]} />);
    await screen.findByText(/Package activation is unverified/);
    fireEvent.click(screen.getByRole("button", { name: "Refresh authority" }));
    await screen.findByText(/did not prove the reviewed package activation/i);
    expect(screen.getByRole("button", { name: "Pause future invocations" })).toBeDisabled();
    expect(JSON.parse(window.sessionStorage.getItem("seraph.procedure-v2.prepared:operator%3Aone:session-1") ?? "null")).toMatchObject({ packageActivationRecovery: recovery });
    expect(getRoutine).toHaveBeenCalledTimes(2);
  });

  it("does not hydrate package activation recovery into another owner session", async () => {
    const installedRoutine = { ...existingRoutine, state: "installed", revision: 7, current_version: 2 } as ProcedureV2Routine;
    const recovery = {
      schema_version: 1,
      routineId: installedRoutine.id,
      versionId: "version-existing-2",
      version: 2,
      digest,
      expectedRevision: 7,
      approvalId: "package-approval-2",
    };
    vi.spyOn(procedureV2Api, "listRoutines").mockResolvedValue([installedRoutine]);
    vi.spyOn(procedureV2Api, "getRoutine").mockResolvedValue(installedRoutine);
    window.sessionStorage.setItem("seraph.procedure-v2.prepared:operator%3Aone:session-1", JSON.stringify({
      schema_version: 1,
      routineId: installedRoutine.id,
      bindingId: "binding-existing-2",
      revision: 4,
      version: 2,
      versionId: "version-existing-2",
      installJobId: "install-2",
      approvalId: "install-approval-2",
      packageActivationRecovery: recovery,
    }));

    render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-2" goals={[goal]} />);
    await screen.findByText(/No existing reviewed v2 procedures are available|Choose an existing v2 procedure/);
    expect(screen.queryByText(/Package activation is unverified/)).not.toBeInTheDocument();
    expect(window.sessionStorage.getItem("seraph.procedure-v2.prepared:operator%3Aone:session-2")).toBeNull();
  });

  it("does not publish a delayed activation readback after the owner session changes", async () => {
    const pausedRoutine = { ...existingRoutine, state: "paused" } as ProcedureV2Routine;
    vi.spyOn(procedureV2Api, "listRoutines").mockResolvedValue([pausedRoutine]);
    vi.spyOn(procedureV2Api, "getRoutine").mockResolvedValue(pausedRoutine);
    let resolveActivation!: (value: ProcedureV2Routine) => void;
    const lifecycle = vi.spyOn(procedureV2Api, "lifecycle").mockReturnValue(new Promise((resolve) => { resolveActivation = resolve; }));

    const view = render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-1" goals={[goal]} />);
    fireEvent.change(await screen.findByLabelText("Existing reviewed procedure"), { target: { value: pausedRoutine.id } });
    fireEvent.click(await screen.findByRole("button", { name: "Resume procedure" }));
    view.rerender(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-2" goals={[goal]} />);
    await act(async () => { resolveActivation({ ...pausedRoutine, state: "active", current_version: 2, revision: pausedRoutine.revision + 1 }); });

    expect(lifecycle).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("Procedure activate completed with a current server revision.")).not.toBeInTheDocument();
  });

  it("retains an unknown invocation request and retries it with its captured version after routine readback failure", async () => {
    vi.spyOn(procedureV2Api, "listSourceTasks").mockResolvedValue([{
      task_id: sourceTask.task_id,
      title: sourceTask.title,
      status: sourceTask.status,
      capability_id: sourceTask.capability_id,
      goal_id: sourceTask.goal_id,
      goal_revision: sourceTask.goal_revision,
      task_revision: sourceTask.task_revision,
      readback_status: sourceTask.readback_status,
      verification_status: sourceTask.verification_status,
    }]);
    vi.spyOn(procedureV2Api, "previewFromTasks").mockResolvedValue(preview);
    vi.spyOn(procedureV2Api, "prepareFromTasks").mockResolvedValue({ status: "prepared", binding_id: "binding-1", routine_id: "routine-1", version_id: "version-1", version: 1, schema_version: 2, template_id: "public-browser-check", revision: 1, request_digest: digest, preview_digest: digest, preview_expires_at: preview.expires_at, install_job_id: "routine-install:routine-1:v1", approval_id: "approval-install-1", install_approval_status: "approved", install_approval_expires_at: "2026-10-02T12:15:00Z" });
    const activeRoutine = {
      id: "routine-1", owner_principal_id: "operator:one", state: "active", revision: 4, current_version: 1, name: "Public status check",
      versions: [{ id: "version-1", routine_id: "routine-1", version: 1, workflow_sha256: digest, runbook_sha256: digest, installed_package_digest: digest, source_provenance: {}, source_repository: null, source_action: null, source_issue_number: null, created_at: preview.expires_at, installed_at: preview.expires_at }],
      package: { status: "active", digest, review_id: "review-1" },
    } as ProcedureV2Routine;
    vi.spyOn(procedureV2Api, "getRoutine")
      .mockResolvedValueOnce(activeRoutine)
      .mockRejectedValueOnce(new Error("routine metadata unavailable during reconciliation"));
    const unknownError = new ProcedureV2ApiError(503, {
      code: "procedure_outcome_unknown",
      message: "The task receipt is not yet settled",
      recovery_action: "reconcile_existing_effect",
      retryable: false,
      binding_id: "binding-1",
      audit_receipt_id: null,
    });
    const invokeCall = vi.spyOn(procedureV2Api, "invoke")
      .mockRejectedValueOnce(unknownError)
      .mockImplementationOnce(async (_routineId, request) => ({ status: "degraded", routine_id: "routine-1", version: 1, schema_version: 2, template_id: "public-browser-check", invocation_uuid: request.invocation_uuid, scope: "procedure-v2:routine-1:version-1", goal_id: request.goal_id, goal_revision: request.expected_goal_revision, task_id: "task-2", attempt_id: null, job_id: null, input_artifact_id: "input-2", input_digest: digest, plan_digest: digest, revision: 4, audit_receipt_id: null, recovery_action: "reconcile_existing_effect" }))
      .mockImplementationOnce(async (_routineId, request) => ({ status: "accepted", routine_id: "routine-1", version: 1, schema_version: 2, template_id: "public-browser-check", invocation_uuid: request.invocation_uuid, scope: "procedure-v2:routine-1:version-1", goal_id: request.goal_id, goal_revision: request.expected_goal_revision, task_id: "task-2", attempt_id: null, job_id: null, input_artifact_id: "input-2", input_digest: digest, plan_digest: digest, revision: 4, audit_receipt_id: "receipt-2", recovery_action: null }));

    const first = render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-1" selectedSourceTask={sourceTask} goals={[goal]} />);
    fireEvent.change(screen.getByLabelText("Procedure name"), { target: { value: "Unknown outcome check" } });
    fireEvent.click(screen.getByRole("button", { name: "Preview fixed procedure" }));
    await screen.findByRole("region", { name: "Procedure preview" });
    fireEvent.click(screen.getByRole("button", { name: "Prepare this reviewed version" }));
    await screen.findByText("routine-1");
    fireEvent.click(screen.getByRole("button", { name: "Invoke for this goal" }));
    await screen.findByText(/Recovery: reconcile_existing_effect/);
    expect(invokeCall).toHaveBeenCalledTimes(1);
    const firstRequest = invokeCall.mock.calls[0]?.[1];
    expect(JSON.parse(window.sessionStorage.getItem("seraph.procedure-v2.pending:operator%3Aone:session-1") ?? "{}")).toMatchObject({
      kind: "invoke",
      routineId: "routine-1",
      versionId: "version-1",
    });
    first.unmount();

    render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-1" goals={[goal]} />);
    expect(await screen.findByText(/An unconfirmed invoke request is retained/)).toBeInTheDocument();
    expect(await screen.findByText(/The retained procedure could not restore its routine/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry exact request" }));
    await waitFor(() => expect(invokeCall).toHaveBeenCalledTimes(2));
    expect(invokeCall.mock.calls[1]?.[1]).toEqual(firstRequest);
    await screen.findByText(/invocation outcome is degraded/);
    expect(window.sessionStorage.getItem("seraph.procedure-v2.pending:operator%3Aone:session-1")).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Retry exact request" }));
    await waitFor(() => expect(invokeCall).toHaveBeenCalledTimes(3));
    expect(invokeCall.mock.calls[2]?.[1]).toEqual(firstRequest);
    expect(window.sessionStorage.getItem("seraph.procedure-v2.pending:operator%3Aone:session-1")).toBeNull();
  });

  it("discovers an existing reviewed routine after a fresh mount and rolls back only the selected version", async () => {
    const listRoutines = vi.spyOn(procedureV2Api, "listRoutines").mockResolvedValue([existingRoutine]);
    const getRoutine = vi.spyOn(procedureV2Api, "getRoutine").mockResolvedValue(existingRoutine);
    const lifecycle = vi.spyOn(procedureV2Api, "lifecycle").mockResolvedValue({ ...existingRoutine, current_version: 1, revision: 6 });
    vi.spyOn(window, "confirm").mockReturnValue(true);

    render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-1" goals={[goal]} />);
    const selector = await screen.findByLabelText("Existing reviewed procedure");
    expect(listRoutines).toHaveBeenCalledTimes(1);
    fireEvent.change(selector, { target: { value: "routine-existing" } });
    await waitFor(() => expect(getRoutine).toHaveBeenCalledWith("routine-existing"));
    expect(await screen.findByText(/Loaded the server-owned procedure Reusable public check/)).toBeInTheDocument();
    expect(screen.getByText("binding-existing-2")).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Existing procedure version"), { target: { value: "1" } });
    await waitFor(() => expect(getRoutine).toHaveBeenCalledTimes(2));
    const rollback = await screen.findByRole("button", { name: "Rollback future invocations to version 1" });
    fireEvent.click(rollback);
    await waitFor(() => expect(lifecycle).toHaveBeenCalledWith("routine-existing", "rollback", {
      target_version: 1,
      expected_routine_revision: 5,
      reason: "Operator requested rollback to version 1.",
    }));
  });

  it.each([
    ["a different routine id", { id: "routine-other" }],
    ["a different owner", { owner_principal_id: "operator:other" }],
  ] as const)("does not select a routine readback with %s", async (_caseName, mismatch) => {
    vi.spyOn(procedureV2Api, "listSourceTasks").mockResolvedValue([]);
    vi.spyOn(procedureV2Api, "listRoutines").mockResolvedValue([existingRoutine]);
    vi.spyOn(procedureV2Api, "getRoutine").mockResolvedValue({ ...existingRoutine, ...mismatch });

    render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-1" goals={[goal]} />);
    const selector = await screen.findByLabelText("Existing reviewed procedure");
    fireEvent.change(selector, { target: { value: "routine-existing" } });

    expect(await screen.findByRole("alert")).toHaveTextContent("not bound to the current operator");
    expect(screen.queryByText(/Loaded the server-owned procedure/)).not.toBeInTheDocument();
  });

  it("clears routine choices on an owner-session change and surfaces metadata failure", async () => {
    const listRoutines = vi.spyOn(procedureV2Api, "listRoutines");
    listRoutines.mockResolvedValueOnce([existingRoutine]).mockRejectedValueOnce(new Error("routine metadata unavailable"));
    const view = render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-1" goals={[goal]} />);
    const selector = await screen.findByLabelText("Existing reviewed procedure");
    expect(screen.getByRole("option", { name: /Reusable public check/ })).toBeInTheDocument();
    view.rerender(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-2" goals={[goal]} />);
    await waitFor(() => expect(screen.queryByRole("option", { name: /Reusable public check/ })).not.toBeInTheDocument());
    expect(await screen.findByText("Existing procedure unavailable: routine metadata unavailable")).toBeInTheDocument();
    expect(selector).toHaveValue("");
  });

  it("blocks switching existing procedures while an unknown request is retained", async () => {
    vi.spyOn(procedureV2Api, "listRoutines").mockResolvedValue([existingRoutine]);
    window.sessionStorage.setItem("seraph.procedure-v2.pending:operator%3Aone:session-1", JSON.stringify({
      schema_version: 1,
      kind: "invoke",
      routineId: "routine-old",
      request: { version: 1, expected_routine_revision: 4, goal_id: "goal-1", expected_goal_revision: 4, parameters: { goal_id: "goal-1", expected_goal_revision: 4 }, invocation_uuid: "invoke-pending" },
    }));
    render(<ProcedureV2Review ownerPrincipalId="operator:one" ownerSessionId="session-1" goals={[goal]} />);
    const selector = await screen.findByLabelText("Existing reviewed procedure");
    expect(selector).toBeDisabled();
    expect(await screen.findByText(/unconfirmed invoke request/)).toBeInTheDocument();
  });
});
