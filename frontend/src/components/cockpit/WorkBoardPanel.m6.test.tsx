import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  WorkBoardAttempt,
  WorkBoardEventPage,
  WorkBoardTask,
  WorkBoardTaskDetail,
  WorkBoardTaskPage,
} from "../../types";
import { WorkBoardPanel } from "./WorkBoardPanel";

function response(payload: unknown, ok = true, status = ok ? 200 : 500) {
  return { ok, status, json: async () => payload };
}

function attempt(taskId: string, workflowRunId: string): WorkBoardAttempt {
  return {
    attempt_id: `${taskId}-attempt`,
    task_id: taskId,
    workflow_run_id: workflowRunId,
    task_revision_at_claim: 4,
    lease_owner: null,
    cancel_requested_at: null,
    lease_expires_at: null,
    heartbeat_at: "2026-09-25T10:00:02Z",
    fencing_token: 2,
    executor_id: "executor-local",
    started_at: "2026-09-25T10:00:00Z",
    ended_at: "2026-09-25T10:00:02Z",
    outcome: "succeeded",
    receipt_refs: [{ workflow_run_id: workflowRunId, status: "succeeded", verified: true, readback_status: "verified", verification_status: "passed" }],
    readback_status: "verified",
    verification_status: "passed",
    created_at: "2026-09-25T10:00:00Z",
    updated_at: "2026-09-25T10:00:02Z",
  };
}

function task(taskId: string, capabilityId: string, title: string, overrides: Partial<WorkBoardTask> = {}): WorkBoardTask {
  const taskAttempt = attempt(taskId, `${taskId}-workflow`);
  return {
    task_id: taskId,
    creation_sequence: taskId === "source-task" ? 1 : 2,
    owner_principal_id: "operator:one",
    owner_session_id: "operator-session-1",
    origin_session_id: "operator-session-1",
    origin_thread_id: null,
    goal_id: "goal-1",
    goal_revision: 4,
    title,
    body: "Private source body must not enter the procedure preview.",
    capability_id: capabilityId,
    typed_input_ref: "workspace-json:inputs/verified.json",
    typed_input_digest: "a".repeat(64),
    executor_id: "executor-local",
    assignee_id: "operator:one",
    priority: 50,
    idempotency_scope: "task",
    idempotency_key: `${taskId}-key`,
    scheduled_at: null,
    status: "done",
    block_kind: null,
    block_reason: null,
    block_source_status: null,
    cancel_requested_at: null,
    requires_review: false,
    reviewer_id: null,
    review_expires_at: null,
    dependency_count: taskId === "action-task" ? 1 : 0,
    completed_dependency_count: taskId === "action-task" ? 1 : 0,
    dispatch_rank: null,
    recovery_action: null,
    readback_status: "verified",
    verification_status: "passed",
    task_revision: 4,
    result_refs: [],
    artifact_refs: [],
    latest_attempt: taskAttempt,
    created_at: "2026-09-25T10:00:00Z",
    updated_at: "2026-09-25T10:00:02Z",
    completed_at: "2026-09-25T10:00:02Z",
    archived_at: null,
    ...overrides,
  };
}

function detail(taskValue: WorkBoardTask, parents: string[] = []): WorkBoardTaskDetail {
  return {
    task: taskValue,
    attempts: taskValue.latest_attempt ? [taskValue.latest_attempt] : [],
    parents,
    children: [],
    comments: [],
    events: [],
    revision: taskValue.task_revision,
  };
}

function page(tasks: WorkBoardTask[]): WorkBoardTaskPage {
  return { tasks, next_after: null, last_event_id: 9 };
}

function events(): WorkBoardEventPage {
  return { events: [], last_event_id: 9, gap: false };
}

function routine(state = "prepared", revision = 1, currentVersion: number | null = null, packageStatus = "not_reviewed", packageDigest?: string) {
  const installedDigest = state === "prepared" ? null : "d".repeat(64);
  return {
    id: "routine-1",
    owner_principal_id: "operator:one",
    state,
    revision,
    current_version: currentVersion,
    name: "Verified follow-through procedure",
    versions: [
      {
        id: "routine-version-1",
        routine_id: "routine-1",
        version: 1,
        workflow_sha256: "e".repeat(64),
        runbook_sha256: "f".repeat(64),
        installed_package_digest: installedDigest,
        source_provenance: {
          source_task_id: "source-task",
          action_task_id: "action-task",
          source_attempt_id: "source-task-attempt",
          action_attempt_id: "action-task-attempt",
        },
        source_repository: "owner/repository",
        source_action: "create_issue",
        source_issue_number: null,
        created_at: "2026-09-25T10:01:00Z",
        installed_at: installedDigest ? "2026-09-25T10:02:00Z" : null,
      },
    ],
    package: { status: packageStatus, digest: packageDigest ?? installedDigest, review_id: packageStatus === "active" ? "review-1" : null },
  };
}

function routinePackagePreview(digest = "d".repeat(64)) {
  return {
    routine_id: "routine-1",
    version: 1,
    pack_id: "seraph.routine.routine1.v1",
    digest,
    installed_package_digest: digest,
    review_id: null,
    status: "not_reviewed",
    manifest: {
      display_name: "Seraph guardian procedure v1",
      summary: "A reviewed local procedure definition.",
      version: "1.0.1",
      authority: { tools: [], filesystem: [], network: false, secrets: [], approval: "always" },
      resources: { max_runtime_seconds: 600, max_artifact_bytes: 10485760, max_inference_cost_microusd: 0, inference_priority: "approved_operator" },
      data_policy: { classes: ["public"], egress: [] },
    },
    runbook: {
      title: "Seraph verified guardian procedure",
      summary: "Fixed, reviewed procedure definition.",
      procedure: {
        capability_id: "guardian-routine.v1",
        steps: [
          { id: "guardian_watch_run", capability: "guardian_watch_run", tool: "guardian_watch_run" },
          { id: "github_followthrough", capability: "github_followthrough", tool: "github_followthrough" },
        ],
      },
      bindings: {
        workflow_sha256: "e".repeat(64),
        legacy_runbook_sha256: "f".repeat(64),
        source_provenance_sha256: "a".repeat(64),
      },
    },
  };
}

function activeGoal(revision = 7) {
  return {
    id: "goal-1",
    parent_id: null,
    path: "goal-1",
    level: "root",
    title: "Approved second goal",
    description: null,
    status: "active",
    domain: "operations",
    start_date: null,
    due_date: null,
    sort_order: 1,
    revision,
    children: [],
  };
}

function sourceWatch(overrides: Partial<{
  id: string;
  goal_id: string;
  goal_revision: number;
  plan_revision: number;
  state: string;
  last_status: string | null;
}> = {}) {
  return {
    id: "watch-2",
    goal_id: "goal-1",
    goal_revision: 7,
    plan_revision: 4,
    state: "active",
    last_status: "succeeded",
    ...overrides,
  };
}

class TestBoardSocket {
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(_url: string) {}

  close() {
    this.onclose?.();
  }
}

function installTransport(
  fetchMock: ReturnType<typeof vi.fn>,
  state: { source: WorkBoardTask; action: WorkBoardTask },
  handler?: (url: string, init?: RequestInit) => ReturnType<typeof response> | null,
) {
  fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const handled = handler?.(url, init);
    if (handled) return Promise.resolve(handled);
    if (url.includes("/api/work-board/tasks?") && !url.match(/\/tasks\/[^?]+/)) return Promise.resolve(response(page([state.source, state.action])));
    if (url.includes("/api/work-board/events")) return Promise.resolve(response(events()));
    if (url.endsWith("/api/goals/tree")) return Promise.resolve(response([]));
    if (url.includes("/api/work-board/goals/goal-1/execution-limits")) {
      return Promise.resolve(response({ goal_id: "goal-1", goal_revision: 4, effective_max_runtime_seconds: 300, default_max_runtime_seconds: 300, hard_max_runtime_seconds: 900, attempt_limit: 2, limit_source: "default" }));
    }
    if (url.endsWith("/api/work-board/tasks/action-task")) return Promise.resolve(response(detail(state.action, [state.source.task_id])));
    if (url.endsWith("/api/work-board/tasks/source-task")) return Promise.resolve(response(detail(state.source)));
    if (url.includes("/api/work-board/tasks/action-task/proposals")) return Promise.resolve(response({ proposals: [] }));
    return Promise.resolve(response({}));
  });
}

const owner = { ownerPrincipalId: "operator:one", ownerSessionId: "operator-session-1" };

describe("WorkBoardPanel M6 governed procedure controls", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("WebSocket", TestBoardSocket);
    vi.spyOn(window, "confirm").mockReturnValue(true);
  });

  afterEach(() => {
    window.sessionStorage.clear();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("previews and accepts only the selected verified journey with safe receipt fields", async () => {
    const source = task("source-task", "guardian.research-watch.v1", "Verified research");
    const action = task("action-task", "work.github-followthrough.v1", "Verified follow-through");
    const state = { source, action };
    let previewBody: Record<string, unknown> | null = null;
    let acceptBody: Record<string, unknown> | null = null;
    installTransport(fetchMock, state, (url, init) => {
      if (url.endsWith("/api/capabilities/routines/from-board/preview") && init?.method === "POST") {
        previewBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        return response({
          preview_digest: "c".repeat(64),
          source_refs: { source_task_id: "source-task", source_attempt_id: "source-task-attempt", source_watch_job_id: "source-workflow", source_packet_id: "packet-1", action_task_id: "action-task", action_attempt_id: "action-task-attempt", source_m3_job_id: "action-workflow" },
          version_plan: { version: 1, steps: ["guardian_watch_run", "github_followthrough"], workflow: "owner_bound_fixed_guardian_routine" },
          typed_parameters: { goal_id: "goal-1", goal_revision: 4, source_watch_id: "watch-1", source_watch_revision: 2, invocation_uuid: "fresh_uuid_per_invocation" },
          permissions: { capability_id: "guardian-routine.v1", external_mutation: "fresh_operator_grant_required", package_review: "current_active_digest_required", shell_or_arbitrary_connector: false },
          limits: { runtime_seconds: 600, attempts: 1, remote_inference: false },
          verifier: { source: "independent_workflow_readback", required: true, unknown_effect: "blocked" },
          expires_at: "2099-01-01T00:00:00Z",
          safe_summary: "Fixed guardian watch and reviewed GitHub follow-through for goal goal-1",
        });
      }
      if (url.endsWith("/api/capabilities/routines/from-board") && init?.method === "POST") {
        acceptBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        return response({ routine_id: "routine-1", state: "prepared", status: "prepared", revision: 1, version: 1, install_job_id: "routine-install:routine-1:v1", preview_digest: "c".repeat(64), binding_id: "binding-1" });
      }
      if (url.endsWith("/api/capabilities/routines/routine-1")) return response(routine());
      return null;
    });

    render(<WorkBoardPanel {...owner} />);
    fireEvent.click(await screen.findByRole("button", { name: "Open task Verified follow-through" }));
    const previewButton = await screen.findByRole("button", { name: "Preview procedure" });
    await waitFor(() => expect(previewButton).toBeEnabled());
    fireEvent.click(previewButton);

    const preview = await screen.findByRole("region", { name: "Governed procedure preview" });
    expect(preview).toHaveTextContent("Fixed guardian watch and reviewed GitHub follow-through");
    expect(preview).toHaveTextContent("source_watch_id: watch-1");
    expect(preview).toHaveTextContent("source_task_id: source-task");
    expect(preview).not.toHaveTextContent("Private source body must not enter");
    await waitFor(() => expect(previewBody).toMatchObject({
      source_task_id: "source-task",
      action_task_id: "action-task",
      expected_source_revision: 4,
      expected_action_revision: 4,
      name: "Verified follow-through procedure",
    }));

    fireEvent.click(screen.getByRole("button", { name: "Accept and prepare procedure" }));
    await waitFor(() => expect(acceptBody).toMatchObject({
      source_task_id: "source-task",
      action_task_id: "action-task",
      preview_digest: "c".repeat(64),
    }));
    expect(await screen.findByRole("region", { name: "Governed procedure lifecycle" })).toHaveTextContent("prepared");
    expect(screen.getByText(/Installation is waiting for the exact approval receipt/i)).toBeInTheDocument();
  });

  it("surfaces blocked or stale preview recovery without exposing source content", async () => {
    const source = task("source-task", "guardian.research-watch.v1", "Verified research");
    const action = task("action-task", "work.github-followthrough.v1", "Verified follow-through");
    installTransport(fetchMock, { source, action }, (url, init) => {
      if (url.endsWith("/api/capabilities/routines/from-board/preview") && init?.method === "POST") {
        return response({ detail: { code: "board_journey_runs_not_verified", message: "Independent readback is not verified." } }, false, 409);
      }
      return null;
    });

    render(<WorkBoardPanel {...owner} />);
    fireEvent.click(await screen.findByRole("button", { name: "Open task Verified follow-through" }));
    fireEvent.click(await screen.findByRole("button", { name: "Preview procedure" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Evidence is unavailable or changed. Reload the task and review its source permissions.");
    expect(screen.queryByRole("region", { name: "Governed procedure preview" })).not.toBeInTheDocument();
  });

  it("reviews, approves, and activates the exact installed package before enabling invocations", async () => {
    const source = task("source-task", "guardian.research-watch.v1", "Verified research");
    const action = task("action-task", "work.github-followthrough.v1", "Verified follow-through");
    const state = { source, action };
    const lifecycleCalls: Array<{ path: string; body: Record<string, unknown> }> = [];
    const NativeURL = URL;
    class DownloadURL extends NativeURL {}
    Object.defineProperty(DownloadURL, "createObjectURL", { value: vi.fn(() => "blob:procedure-export") });
    Object.defineProperty(DownloadURL, "revokeObjectURL", { value: vi.fn() });
    vi.stubGlobal("URL", DownloadURL);
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
    let current = routine("prepared", 1, null, "not_reviewed");
    installTransport(fetchMock, state, (url, init) => {
      if (url.endsWith("/api/capabilities/routines/from-board/preview")) {
        return response({
          preview_digest: "c".repeat(64),
          source_refs: { source_task_id: "source-task", action_task_id: "action-task" },
          version_plan: { version: 1, steps: ["guardian_watch_run", "github_followthrough"], workflow: "owner_bound_fixed_guardian_routine" },
          typed_parameters: {},
          permissions: { capability_id: "guardian-routine.v1", external_mutation: "fresh_operator_grant_required", package_review: "current_active_digest_required", shell_or_arbitrary_connector: false },
          limits: { runtime_seconds: 600, attempts: 1, remote_inference: false },
          verifier: { source: "independent_workflow_readback", required: true, unknown_effect: "blocked" },
          expires_at: "2099-01-01T00:00:00Z",
          safe_summary: "Safe preview",
        });
      }
      if (url.endsWith("/api/capabilities/routines/from-board")) return response({ routine_id: "routine-1", state: "prepared", status: "prepared", revision: 1, version: 1, install_job_id: "routine-install:routine-1:v1", preview_digest: "c".repeat(64), binding_id: "binding-1", approval_id: "approval-1" });
      if (url.endsWith("/api/capabilities/routines/routine-1") && (!init?.method || init.method === "GET")) return response(current);
      if (url.endsWith("/versions/1/package/preview") && init?.method === "POST") {
        lifecycleCalls.push({ path: url, body: JSON.parse(String(init.body)) as Record<string, unknown> });
        return response(routinePackagePreview());
      }
      if (url.endsWith("/versions/1/package/review") && init?.method === "POST") {
        lifecycleCalls.push({ path: url, body: JSON.parse(String(init.body)) as Record<string, unknown> });
        return response({ digest: "d".repeat(64), review: { review_id: "package-review-1", status: "approved" } });
      }
      if (url.endsWith("/versions/1/package/approvals") && init?.method === "POST") {
        lifecycleCalls.push({ path: url, body: JSON.parse(String(init.body)) as Record<string, unknown> });
        return response({ digest: "d".repeat(64), approval: { approval_id: "package-approval-1", status: "pending", action: "activate", pack_id: "seraph.routine.routine1.v1", version: "1.0.1", digest: "d".repeat(64), goal_id: "goal-1" } });
      }
      if (url.endsWith("/versions/1/package/approvals/package-approval-1/decision") && init?.method === "POST") {
        const body = JSON.parse(String(init.body)) as Record<string, unknown>;
        lifecycleCalls.push({ path: url, body });
        return response({ digest: "d".repeat(64), approval: { approval_id: "package-approval-1", status: body.decision, action: "activate", pack_id: "seraph.routine.routine1.v1", version: "1.0.1", digest: "d".repeat(64), goal_id: "goal-1" } });
      }
      if (url.endsWith("/versions/1/package/activate") && init?.method === "POST") {
        const body = JSON.parse(String(init.body)) as Record<string, unknown>;
        lifecycleCalls.push({ path: url, body });
        current = routine("installed", 2, 1, "active");
        return response({ digest: "d".repeat(64), status: "active" });
      }
      if (url.endsWith("/versions/1/export") && (!init?.method || init.method === "GET")) {
        return response({
          schema_version: 1,
          kind: "seraph.reviewed_procedure.v1",
          pack_id: "seraph.routine.routine1.v1",
          version: 1,
          package_digest: "d".repeat(64),
          manifest: { display_name: "Seraph guardian procedure v1" },
          runbook: { procedure: { capability_id: "guardian-routine.v1" } },
        });
      }
      if (url.includes("/api/capabilities/routines/routine-1/") && init?.method === "POST") {
        const body = JSON.parse(String(init.body)) as Record<string, unknown>;
        lifecycleCalls.push({ path: url, body });
        if (url.endsWith("/install")) current = routine("installed", 2, 1, "not_reviewed");
        if (url.endsWith("/activate")) {
          current = routine("active", 3, 1, "active", "d".repeat(64));
          current.versions.push({ ...current.versions[0], id: "routine-version-2", version: 2, installed_package_digest: "g".repeat(64) });
          current.current_version = 2;
        }
        if (url.endsWith("/rollback")) current = routine("active", 4, 1, "active");
        if (url.endsWith("/revoke")) current = routine("revoked", 5, 1, "active");
        return response(current);
      }
      return null;
    });

    render(<WorkBoardPanel {...owner} onOpenApprovals={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Open task Verified follow-through" }));
    fireEvent.click(await screen.findByRole("button", { name: "Preview procedure" }));
    fireEvent.click(await screen.findByRole("button", { name: "Accept and prepare procedure" }));
    fireEvent.click(await screen.findByRole("button", { name: "Install reviewed procedure" }));

    const enableButton = await screen.findByRole("button", { name: "Enable procedure invocations" });
    expect(enableButton).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Preview installed package" }));
    const packagePreview = await screen.findByRole("region", { name: "Procedure package preview" });
    expect(packagePreview).toHaveTextContent("seraph.routine.routine1.v1");
    expect(packagePreview).toHaveTextContent("network disabled");
    expect(packagePreview).toHaveTextContent("guardian_watch_run → github_followthrough");
    expect(packagePreview).not.toHaveTextContent("Private source body must not enter");

    fireEvent.click(await screen.findByRole("button", { name: "Record local package review" }));
    fireEvent.click(await screen.findByRole("button", { name: "Prepare activation approval" }));
    expect(await screen.findByText(/Activation approval pending/i)).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "Approve exact package activation" }));
    fireEvent.click(await screen.findByRole("button", { name: "Activate reviewed capability package" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Enable procedure invocations" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Enable procedure invocations" }));

    await waitFor(() => expect(lifecycleCalls.map((call) => call.path.split("/").slice(-2).join("/"))).toContain("package/preview"));
    expect(lifecycleCalls.some((call) => call.path.endsWith("/package/review") && call.body.expected_routine_revision === 2)).toBe(true);
    expect(lifecycleCalls.some((call) => call.path.endsWith("/package/approvals") && call.body.expected_routine_revision === 2)).toBe(true);
    expect(lifecycleCalls.some((call) => call.path.endsWith("/package/approvals/package-approval-1/decision") && call.body.decision === "approved")).toBe(true);
    expect(lifecycleCalls.some((call) => call.path.endsWith("/package/activate") && call.body.approval_id === "package-approval-1")).toBe(true);
    expect(lifecycleCalls.some((call) => call.path.endsWith("/activate") && call.body.expected_routine_revision === 2)).toBe(true);

    const rollback = await screen.findByRole("combobox", { name: "Rollback target" });
    fireEvent.change(rollback, { target: { value: "1" } });
    await waitFor(() => expect(lifecycleCalls.some((call) => call.path.endsWith("/rollback") && call.body.target_version === 1)).toBe(true));

    fireEvent.click(await screen.findByRole("button", { name: "Export reviewed procedure" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith("/versions/1/export"))).toBe(true));
    expect(DownloadURL.createObjectURL).toHaveBeenCalledTimes(1);
    expect(DownloadURL.revokeObjectURL).toHaveBeenCalledWith("blob:procedure-export");
    expect(anchorClick).toHaveBeenCalledTimes(1);

    fireEvent.click(await screen.findByRole("button", { name: "Revoke permanently" }));
    expect(await screen.findByText(/Revoked: future invocations are blocked permanently/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Enable procedure invocations" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Invoke governed procedure" })).toBeDisabled();
  });

  it("does not offer an old rollback target when its package digest is not the active readback", async () => {
    const source = task("source-task", "guardian.research-watch.v1", "Verified research");
    const action = task("action-task", "work.github-followthrough.v1", "Verified follow-through");
    const state = { source, action };
    const current = routine("active", 4, 2, "active", "g".repeat(64));
    current.versions.push({
      ...current.versions[0],
      id: "routine-version-2",
      version: 2,
      installed_package_digest: "g".repeat(64),
    });
    installTransport(fetchMock, state, (url, init) => {
      if (url.endsWith("/api/capabilities/routines")) return response({ routines: [current] });
      if (url.endsWith("/api/capabilities/routines/routine-1") && (!init?.method || init.method === "GET")) return response(current);
      return null;
    });

    render(<WorkBoardPanel {...owner} />);
    fireEvent.click(await screen.findByRole("button", { name: "Open task Verified follow-through" }));
    fireEvent.change(await screen.findByRole("combobox", { name: "Existing governed procedure" }), { target: { value: "routine-1" } });

    expect(await screen.findByText(/Rollback is blocked: no installed earlier version matches/i)).toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "Rollback target" })).not.toBeInTheDocument();
  });

  it("loads an owner routine, selects the current goal/watch, and creates a Todo task with the exact fresh-authority invoke payload", async () => {
    const source = task("source-task", "guardian.research-watch.v1", "Verified research");
    const action = task("action-task", "work.github-followthrough.v1", "Verified follow-through");
    const state = { source, action };
    let invokeBody: Record<string, unknown> | null = null;
    installTransport(fetchMock, state, (url, init) => {
      if (url.endsWith("/api/goals/tree")) return response([activeGoal(7)]);
      if (url.endsWith("/api/capabilities/routines") && (!init?.method || init.method === "GET")) {
        return response({ routines: [routine("active", 8, 1, "active")] });
      }
      if (url.endsWith("/api/capabilities/source-watches") && (!init?.method || init.method === "GET")) {
        return response([sourceWatch()]);
      }
      if (url.endsWith("/api/capabilities/routines/routine-1") && (!init?.method || init.method === "GET")) return response(routine("active", 8, 1, "active"));
      if (url.endsWith("/api/capabilities/routines/routine-1/invoke") && init?.method === "POST") {
        invokeBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        return response({ status: "queued", task_id: "invoked-todo-1", task_revision: 1, deduped: false, preview: { routine_id: "routine-1", version: 1, goal_id: "goal-1", goal_revision: 7, source_watch_id: "watch-2", source_watch_revision: 4 } });
      }
      return null;
    });

    render(<WorkBoardPanel {...owner} />);
    fireEvent.click(await screen.findByRole("button", { name: "Open task Verified follow-through" }));
    const routineSelector = await screen.findByRole("combobox", { name: "Existing governed procedure" });
    fireEvent.change(routineSelector, { target: { value: "routine-1" } });

    expect(await screen.findByRole("region", { name: "Governed procedure lifecycle" })).toHaveTextContent("active · revision 8");
    expect(screen.getByRole("combobox", { name: "Invocation goal" })).toHaveValue("goal-1");
    expect(screen.getByRole("combobox", { name: "Approved source watch" })).toHaveValue("watch-2");
    fireEvent.click(screen.getByRole("button", { name: "Invoke governed procedure" }));

    await waitFor(() => expect(invokeBody).toEqual({
      version: 1,
      expected_routine_revision: 8,
      goal_id: "goal-1",
      expected_goal_revision: 7,
      source_watch_id: "watch-2",
      expected_watch_revision: 4,
      invocation_uuid: expect.any(String),
    }));
    expect(await screen.findByText("invoked-todo-1")).toBeInTheDocument();
  });

  it("replays the exact persisted invocation after an ambiguous response and remount", async () => {
    const source = task("source-task", "guardian.research-watch.v1", "Verified research");
    const action = task("action-task", "work.github-followthrough.v1", "Verified follow-through");
    const state = { source, action };
    const invocationBodies: Record<string, unknown>[] = [];
    let firstResponseAmbiguous = true;
    let staleAuthority = false;
    installTransport(fetchMock, state, (url, init) => {
      if (url.endsWith("/api/goals/tree")) return response([activeGoal(staleAuthority ? 8 : 7)]);
      if (url.endsWith("/api/capabilities/routines") && (!init?.method || init.method === "GET")) {
        return response(staleAuthority
          ? { routines: [routine("paused", 9, 1, "not_reviewed", "x".repeat(64))] }
          : { routines: [routine("active", 8, 1, "active")] });
      }
      if (url.endsWith("/api/capabilities/source-watches") && (!init?.method || init.method === "GET")) {
        return response([staleAuthority
          ? sourceWatch({ goal_revision: 8, plan_revision: 5, state: "paused" })
          : sourceWatch()]);
      }
      if (url.endsWith("/api/capabilities/routines/routine-1") && (!init?.method || init.method === "GET")) {
        return response(staleAuthority
          ? routine("paused", 9, 1, "not_reviewed", "x".repeat(64))
          : routine("active", 8, 1, "active"));
      }
      if (url.endsWith("/api/capabilities/routines/routine-1/invoke") && init?.method === "POST") {
        invocationBodies.push(JSON.parse(String(init.body)) as Record<string, unknown>);
        if (firstResponseAmbiguous) {
          firstResponseAmbiguous = false;
          staleAuthority = true;
          throw new Error("connection reset after admission");
        }
        return response({
          status: "queued",
          task_id: "invoked-after-replay",
          task_revision: 1,
          deduped: true,
          preview: { routine_id: "routine-1", version: 1, goal_id: "goal-1", goal_revision: 7, source_watch_id: "watch-2", source_watch_revision: 4 },
        });
      }
      return null;
    });

    const firstMount = render(<WorkBoardPanel {...owner} />);
    fireEvent.click(await screen.findByRole("button", { name: "Open task Verified follow-through" }));
    fireEvent.change(await screen.findByRole("combobox", { name: "Existing governed procedure" }), { target: { value: "routine-1" } });
    fireEvent.click(await screen.findByRole("button", { name: "Invoke governed procedure" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Evidence is unavailable or changed. Reload the task and review its source permissions.");
    expect(invocationBodies).toHaveLength(1);
    const firstRequest = invocationBodies[0];
    expect(screen.getByRole("button", { name: "Retry invocation and reconcile" })).toBeInTheDocument();

    firstMount.unmount();
    render(<WorkBoardPanel {...owner} />);
    fireEvent.click(await screen.findByRole("button", { name: "Open task Verified follow-through" }));
    fireEvent.change(await screen.findByRole("combobox", { name: "Existing governed procedure" }), { target: { value: "routine-1" } });
    expect(await screen.findByText(/exact routine, goal, watch, and revision request is preserved/i)).toBeInTheDocument();
    const retryButton = await screen.findByRole("button", { name: "Retry invocation and reconcile" });
    expect(retryButton).toBeEnabled();
    expect(screen.getByRole("combobox", { name: "Invocation goal" })).toBeDisabled();
    expect(screen.getByRole("combobox", { name: "Approved source watch" })).toBeDisabled();
    fireEvent.click(retryButton);

    await waitFor(() => expect(invocationBodies).toHaveLength(2));
    expect(invocationBodies[1]).toEqual(firstRequest);
    expect(await screen.findByText("invoked-after-replay")).toBeInTheDocument();
    expect(window.sessionStorage.length).toBe(0);
  });

  it("fails closed without posting when the exact invocation cannot be persisted", async () => {
    const source = task("source-task", "guardian.research-watch.v1", "Verified research");
    const action = task("action-task", "work.github-followthrough.v1", "Verified follow-through");
    const state = { source, action };
    let invokeCalls = 0;
    installTransport(fetchMock, state, (url, init) => {
      if (url.endsWith("/api/goals/tree")) return response([activeGoal(7)]);
      if (url.endsWith("/api/capabilities/routines") && (!init?.method || init.method === "GET")) return response({ routines: [routine("active", 8, 1, "active")] });
      if (url.endsWith("/api/capabilities/source-watches") && (!init?.method || init.method === "GET")) return response([sourceWatch()]);
      if (url.endsWith("/api/capabilities/routines/routine-1") && (!init?.method || init.method === "GET")) return response(routine("active", 8, 1, "active"));
      if (url.endsWith("/api/capabilities/routines/routine-1/invoke") && init?.method === "POST") {
        invokeCalls += 1;
        return response({ status: "queued", task_id: "should-not-exist" });
      }
      return null;
    });
    const unavailableStorage = {
      getItem: () => null,
      setItem: () => { throw new Error("storage disabled"); },
      removeItem: () => undefined,
      clear: () => undefined,
      key: () => null,
      length: 0,
    } as unknown as Storage;
    vi.spyOn(window, "sessionStorage", "get").mockReturnValue(unavailableStorage);

    render(<WorkBoardPanel {...owner} />);
    fireEvent.click(await screen.findByRole("button", { name: "Open task Verified follow-through" }));
    fireEvent.change(await screen.findByRole("combobox", { name: "Existing governed procedure" }), { target: { value: "routine-1" } });
    const invokeButton = await screen.findByRole("button", { name: "Invoke governed procedure" });
    await waitFor(() => expect(invokeButton).toBeEnabled());
    fireEvent.click(invokeButton);

    expect(await screen.findByText(/exact invocation request could not be persisted/i)).toBeInTheDocument();
    expect(invokeCalls).toBe(0);
  });

  it("keeps paused watches blocked", async () => {
    const source = task("source-task", "guardian.research-watch.v1", "Verified research");
    const action = task("action-task", "work.github-followthrough.v1", "Verified follow-through");
    const state = { source, action };
    installTransport(fetchMock, state, (url, init) => {
      if (url.endsWith("/api/goals/tree")) return response([activeGoal(7)]);
      if (url.endsWith("/api/capabilities/routines")) return response({ routines: [routine("active", 8, 1, "active")] });
      if (url.endsWith("/api/capabilities/source-watches")) return response([sourceWatch({ state: "paused" })]);
      if (url.endsWith("/api/capabilities/routines/routine-1") && (!init?.method || init.method === "GET")) return response(routine("active", 8, 1, "active"));
      return null;
    });

    render(<WorkBoardPanel {...owner} />);
    fireEvent.click(await screen.findByRole("button", { name: "Open task Verified follow-through" }));
    fireEvent.change(await screen.findByRole("combobox", { name: "Existing governed procedure" }), { target: { value: "routine-1" } });
    expect(await screen.findByText(/selected source watch is paused/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Invoke governed procedure" })).toBeDisabled();
  });

  it("surfaces a stale invoke response for recovery", async () => {
    const source = task("source-task", "guardian.research-watch.v1", "Verified research");
    const action = task("action-task", "work.github-followthrough.v1", "Verified follow-through");
    const state = { source, action };
    installTransport(fetchMock, state, (url, init) => {
      if (url.endsWith("/api/goals/tree")) return response([activeGoal(7)]);
      if (url.endsWith("/api/capabilities/routines")) return response({ routines: [routine("active", 8, 1, "active")] });
      if (url.endsWith("/api/capabilities/source-watches")) return response([sourceWatch({ state: "active" })]);
      if (url.endsWith("/api/capabilities/routines/routine-1") && (!init?.method || init.method === "GET")) return response(routine("active", 8, 1, "active"));
      if (url.endsWith("/api/capabilities/routines/routine-1/invoke")) return response({ detail: { code: "source_watch_revision_stale", message: "The selected source watch revision is stale." } }, false, 409);
      return null;
    });

    render(<WorkBoardPanel {...owner} />);
    fireEvent.click(await screen.findByRole("button", { name: "Open task Verified follow-through" }));
    fireEvent.change(await screen.findByRole("combobox", { name: "Existing governed procedure" }), { target: { value: "routine-1" } });
    fireEvent.click(await screen.findByRole("button", { name: "Invoke governed procedure" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Evidence is unavailable or changed. Reload the task and review its source permissions.");

  });

  it("reloads the cockpit and selects a persisted routine without recreating it", async () => {
    const source = task("source-task", "guardian.research-watch.v1", "Verified research");
    const action = task("action-task", "work.github-followthrough.v1", "Verified follow-through");
    const state = { source, action };
    installTransport(fetchMock, state, (url, init) => {
      if (url.endsWith("/api/goals/tree")) return response([activeGoal()]);
      if (url.endsWith("/api/capabilities/routines")) return response({ routines: [routine("active", 9, 1, "active")] });
      if (url.endsWith("/api/capabilities/source-watches")) return response([sourceWatch()]);
      if (url.endsWith("/api/capabilities/routines/routine-1") && (!init?.method || init.method === "GET")) return response(routine("active", 9, 1, "active"));
      return null;
    });

    const firstMount = render(<WorkBoardPanel {...owner} />);
    fireEvent.click(await screen.findByRole("button", { name: "Open task Verified follow-through" }));
    fireEvent.change(await screen.findByRole("combobox", { name: "Existing governed procedure" }), { target: { value: "routine-1" } });
    expect(await screen.findByRole("region", { name: "Governed procedure lifecycle" })).toHaveTextContent("active · revision 9");
    firstMount.unmount();

    render(<WorkBoardPanel {...owner} />);
    fireEvent.click(await screen.findByRole("button", { name: "Open task Verified follow-through" }));
    fireEvent.change(await screen.findByRole("combobox", { name: "Existing governed procedure" }), { target: { value: "routine-1" } });
    expect(await screen.findByRole("region", { name: "Governed procedure lifecycle" })).toHaveTextContent("Routine routine-1 · version 1");
    expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith("/api/capabilities/routines"))).toBe(true);
  });
});
