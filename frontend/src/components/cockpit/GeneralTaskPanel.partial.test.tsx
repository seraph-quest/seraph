import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { apiFetch } from "../../lib/api";
import { GeneralTaskPanel } from "./GeneralTaskPanel";
import { WorkBoardPanel } from "./WorkBoardPanel";
import { validateGeneralTaskPlan } from "../../lib/generalTask";
import type { WorkBoardTask } from "../../types";

vi.mock("../../lib/api", () => ({ apiFetch: vi.fn() }));
const owner = { ownerPrincipalId: "operator-one", ownerSessionId: "session-one" };
const task: WorkBoardTask = { creation_sequence: 1, origin_session_id: owner.ownerSessionId, origin_thread_id: null,
    goal_revision: 1, title: "Stopped specialist work", body: "", typed_input_ref: "general-input:parent", typed_input_digest: "d".repeat(64),
    executor_id: "agent.task.v1", assignee_id: owner.ownerPrincipalId, priority: 50, idempotency_scope: "task", idempotency_key: "original-task",
    scheduled_at: null, block_kind: "unknown_effect", block_source_status: "running", cancel_requested_at: "2026-10-09T00:11:00Z",
    requires_review: true, reviewer_id: owner.ownerPrincipalId, dependency_count: 0, completed_dependency_count: 0,
    dispatch_rank: null, dispatch_wait_reason: null, recovery_action: null, readback_status: "unknown", verification_status: "reconciliation_required",
    result_refs: [], artifact_refs: [], created_at: "2026-10-09T00:00:00Z", updated_at: "2026-10-09T00:11:00Z", completed_at: null, archived_at: null, task_id: "parent-task", capability_id: "agent.task.v1", task_revision: 8, status: "blocked", goal_id: "goal",
  owner_principal_id: owner.ownerPrincipalId, owner_session_id: owner.ownerSessionId, block_reason: "general_task_cancel_pending",
  latest_attempt: { attempt_id: "original-attempt", task_id: "parent-task", workflow_run_id: "original-parent-job", fencing_token: 1, ended_at: null,
    task_revision_at_claim: 1, lease_owner: "original-owner", cancel_requested_at: "2026-10-09T00:11:00Z", lease_expires_at: "2026-10-09T00:10:00Z",
    heartbeat_at: "2026-10-09T00:09:00Z", executor_id: "agent.task.v1", started_at: "2026-10-09T00:00:00Z", outcome: null,
    receipt_refs: [], readback_status: "unknown", verification_status: "reconciliation_required", created_at: "2026-10-09T00:00:00Z", updated_at: "2026-10-09T00:11:00Z" } };
const output = { child_task_id: "specialist-task", child_job_id: "specialist-job", delegation_invocation_id: "general-tool:original",
  artifact_id: "verified-child-artifact", content_sha256: "a".repeat(64), size_bytes: 123 };
const options = { eligible: true, attempt_id: "original-attempt", workflow_run_id: "original-parent-job", expected_manifest_revision: 9,
  expected_plan_revision: 1, selected_steps: [{ step_id: "delegate", outputs: [output] }], no_learning: true };
const native = { phase: "unknown_recovery", plan_revision: 1, manifest_revision: 9, original_deadline_at: "2026-10-09T00:20:00Z",
  native_deadline_at: "2026-10-09T00:10:00Z", cancellation: { state: "pending", child_ids: ["unknown-sibling-job"], callback_closed: false, effect_debt: true, reason: "unknown" },
  steps: [{ step_id: "delegate", status: "verified", contact_state: "settled", invocation_id: output.delegation_invocation_id,
    plan_revision: 1, artifact_refs: [] }], admitted_invocation_ids: [output.delegation_invocation_id], remaining_steps: ["unknown-sibling"],
  partial_output_refs: [], partial_review_options: options, no_learning: true };
const plan = { task_id: task.task_id, task_revision: task.task_revision, accepted: true, no_learning: true,
  task_input: { goal_ref: "goal", intent: "Bounded specialist work", limits: { max_steps: 4, max_inference_calls: 12, wall_seconds: 900, depth: 0, max_outstanding_children: 2, max_cost_microusd: 10 } },
  plan: { schema_version: 1, revision: 1, steps: [{ step_id: "delegate", tool_id: "specialist_delegate", input: {}, depends_on: [], output_contract: { type: "object" } }] },
  descriptors: [{ tool_id: "specialist_delegate", version: "1", input_schema: { type: "object" }, output_schema: { type: "object" }, effects: ["delegation"], permissions: ["capability_execute"], deadline: 30, verifier: "readback" }],
  strategy: { status: "none", reason: "baseline" }, native_execution: native };
function response(value: unknown, status = 200) { return new Response(JSON.stringify(value), { status }); }
function overlay(key: string) { return { state: "partial_review_pending_debt", decision_digest: "b".repeat(64), idempotency_key: key,
  result_ref: { artifact_id: "partial-result", digest: "c".repeat(64), schema_version: "SpecialistPartialResult.v1" }, selected_step_ids: ["delegate"],
  selected_outputs: [{ step_id: "delegate", ...output }], unresolved_job_ids: ["unknown-sibling-job"], unresolved_effect_count: 2,
  current_unresolved_job_ids: ["unknown-sibling-job"], current_unresolved_effect_count: 2, current_unresolved_cost_count: 1,
  current_cancellation_state: "pending", no_learning: true }; }
function receipt(body: { partial_decision: { idempotency_key: string } }) {
  return { task, attempt: task.latest_attempt, partial_review: overlay(body.partial_decision.idempotency_key), idempotent_replay: false };
}
async function selectAndInspect(inspect = true) {
  fireEvent.click(await screen.findByLabelText("Select partial step delegate"));
  if (inspect) fireEvent.click(screen.getByRole("button", { name: `Inspect partial artifact ${output.artifact_id}` }));
}
beforeEach(() => { vi.mocked(apiFetch).mockReset(); vi.mocked(apiFetch).mockResolvedValue(response(plan)); });
afterEach(() => { vi.unstubAllGlobals(); });

it("passes the existing Work Board artifact inspector through the actual selected task panel", async () => {
  const boardTask = task;
  const inspect = vi.fn();
  const boardResponse = async (input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith(`/api/work-board/tasks/${task.task_id}/plan`)) return response(plan);
    if (url.includes("/api/work-board/tasks?")) return response({ tasks: [boardTask], next_after: null, last_event_id: 1 });
    if (url.endsWith(`/api/work-board/tasks/${task.task_id}`)) return response({ task: boardTask, events: [], attempts: [task.latest_attempt], parents: [], children: [], comments: [], revision: 8 });
    if (url.includes("/api/work-board/events")) return response({ events: [], last_event_id: 1, gap: false });
    if (url.endsWith("/api/goals/tree")) return response([]);
    return response({});
  };
  vi.stubGlobal("fetch", vi.fn(boardResponse));
  vi.mocked(apiFetch).mockImplementation(boardResponse);
  vi.stubGlobal("WebSocket", class { close() {} });
  render(<WorkBoardPanel {...owner} onInspectArtifact={inspect} />);
  fireEvent.click(await screen.findByRole("button", { name: "Open task Stopped specialist work" }));
  fireEvent.click(await screen.findByRole("button", { name: `Inspect partial artifact ${output.artifact_id}` }));
  expect(inspect).toHaveBeenCalledWith({ reference: { artifact_id: output.artifact_id, content_sha256: output.content_sha256 },
    ownerSessionId: owner.ownerSessionId, workflowRunId: output.child_job_id, parentWorkflowRunId: output.delegation_invocation_id });
});

it("routes the selected genuine-shaped child reference to the existing inspector before acknowledgment", async () => {
  const inspect = vi.fn(); render(<GeneralTaskPanel {...owner} task={task} onInspectArtifact={inspect} />);
  await selectAndInspect(false);
  expect(screen.getByLabelText("Acknowledge unresolved partial debt")).toBeDisabled();
  expect(screen.getByRole("button", { name: "Accept selected partial results" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: `Inspect partial artifact ${output.artifact_id}` }));
  expect(inspect).toHaveBeenCalledWith({ reference: { artifact_id: output.artifact_id, content_sha256: output.content_sha256 }, ownerSessionId: owner.ownerSessionId,
    workflowRunId: output.child_job_id, parentWorkflowRunId: output.delegation_invocation_id });
  expect(screen.getByLabelText("Acknowledge unresolved partial debt")).toBeEnabled();
  expect(apiFetch).toHaveBeenCalledTimes(1);
});

it("submits only the exact original decision and keeps accepted results pending debt", async () => {
  vi.mocked(apiFetch).mockImplementation(async (_url, init) => init?.method === "POST" ? response(receipt(JSON.parse(String(init.body)))) : response(plan));
  render(<GeneralTaskPanel {...owner} task={task} onInspectArtifact={vi.fn()} />);
  await selectAndInspect(); fireEvent.click(screen.getByLabelText("Acknowledge unresolved partial debt"));
  fireEvent.click(screen.getByRole("button", { name: "Accept selected partial results" }));
  expect(await screen.findByLabelText("Accepted partial results pending debt")).toHaveTextContent("unresolved effects: 2");
  expect(screen.getByLabelText("Accepted partial results pending debt")).toHaveTextContent("not full task success");
  expect(screen.getByLabelText("Accepted partial results pending debt")).toHaveTextContent("accounting liabilities");
  const [url, init] = vi.mocked(apiFetch).mock.calls[1];
  expect(String(url)).toContain(`/tasks/${task.task_id}/actions`);
  const body = JSON.parse(String(init?.body));
  expect(body).toEqual({ action: "accept_partial_results", expected_revision: 8, partial_decision: {
    idempotency_key: expect.stringMatching(/^[a-f0-9-]{36}$/), attempt_id: options.attempt_id, workflow_run_id: options.workflow_run_id,
    expected_manifest_revision: 9, expected_plan_revision: 1, selected_step_ids: ["delegate"], acknowledge_unresolved: true } });
  expect(screen.queryByRole("button", { name: "Accept selected partial results" })).toBeNull();
  expect(screen.getByRole("button", { name: "Resume paused task work" })).toBeDisabled();
});

it("blocks selection acknowledgment when the inspector is unavailable", async () => {
  render(<GeneralTaskPanel {...owner} task={task} />); await selectAndInspect(false);
  expect(screen.getByRole("button", { name: `Inspect partial artifact ${output.artifact_id}` })).toBeDisabled();
  expect(screen.getByLabelText("Acknowledge unresolved partial debt")).toBeDisabled();
});

it("retains one uncertain POST for explicit exact reconciliation and never automatically retries", async () => {
  const bodies: unknown[] = [];
  vi.mocked(apiFetch).mockImplementation(async (_url, init) => {
    if (init?.method !== "POST") return response(plan);
    const body = JSON.parse(String(init.body)); bodies.push(body);
    if (bodies.length === 1) throw Error("response lost");
    return response({ ...receipt(body), idempotent_replay: true });
  });
  render(<GeneralTaskPanel {...owner} task={task} onInspectArtifact={vi.fn()} />);
  await selectAndInspect(); fireEvent.click(screen.getByLabelText("Acknowledge unresolved partial debt"));
  fireEvent.click(screen.getByRole("button", { name: "Accept selected partial results" }));
  await screen.findByRole("alert"); expect(bodies).toHaveLength(1);
  expect(screen.getByLabelText("Select partial step delegate")).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Reconcile exact partial decision" }));
  await screen.findByLabelText("Accepted partial results pending debt");
  expect(bodies).toHaveLength(2); expect(bodies[1]).toEqual(bodies[0]);
});

it("drops stale CAS selection and requires refreshed inspection instead of a new automatic decision", async () => {
  vi.mocked(apiFetch).mockImplementation(async (_url, init) => init?.method === "POST" ? response({}, 409) : response(plan));
  render(<GeneralTaskPanel {...owner} task={task} onInspectArtifact={vi.fn()} />);
  await selectAndInspect(); fireEvent.click(screen.getByLabelText("Acknowledge unresolved partial debt"));
  fireEvent.click(screen.getByRole("button", { name: "Accept selected partial results" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("revision changed");
  expect(apiFetch).toHaveBeenCalledTimes(2);
  expect(screen.queryByRole("button", { name: "Reconcile exact partial decision" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Refresh current task plan" }));
  await screen.findByLabelText("Select partial step delegate");
  expect(screen.getByLabelText("Select partial step delegate")).not.toBeChecked();
  expect(screen.getByLabelText("Acknowledge unresolved partial debt")).toBeDisabled();
});

it("fences a late acceptance receipt when the authenticated owner changes", async () => {
  let finish!: (response: Response) => void;
  let posted: { partial_decision: { idempotency_key: string } };
  vi.mocked(apiFetch).mockImplementation(async (_url, init) => {
    if (init?.method !== "POST") return response(plan);
    posted = JSON.parse(String(init.body)); return new Promise(resolve => { finish = resolve; });
  });
  const changed = vi.fn(); const { rerender } = render(<GeneralTaskPanel {...owner} task={task} onInspectArtifact={vi.fn()} onChanged={changed} />);
  await selectAndInspect(); fireEvent.click(screen.getByLabelText("Acknowledge unresolved partial debt"));
  fireEvent.click(screen.getByRole("button", { name: "Accept selected partial results" }));
  await waitFor(() => expect(finish).toBeDefined());
  rerender(<GeneralTaskPanel ownerPrincipalId="foreign" ownerSessionId="foreign-session" task={task} onInspectArtifact={vi.fn()} onChanged={changed} />);
  await act(async () => finish(response(receipt(posted!))));
  expect(screen.queryByLabelText("Accepted partial results pending debt")).toBeNull(); expect(changed).not.toHaveBeenCalled();
  expect(screen.queryByLabelText("Review specialist partial results")).toBeNull();
});

it("renders a historical partial overlay after full cancellation as pending debt without new acceptance", async () => {
  const cancelled: WorkBoardTask = { ...task, block_kind: "cancelled", latest_attempt: { ...task.latest_attempt!, ended_at: "2026-10-09T00:30:00Z" } };
  const partial = { ...overlay("11111111-1111-4111-8111-111111111111"), current_cancellation_state: "fully_cancelled",
    current_unresolved_job_ids: [], current_unresolved_effect_count: 0, current_unresolved_cost_count: 0 };
  vi.mocked(apiFetch).mockResolvedValue(response({ ...plan, native_execution: { ...native, phase: "cancelled",
    cancellation: { ...native.cancellation, state: "fully_cancelled", callback_closed: true, effect_debt: false },
    partial_review_options: { eligible: false, reason: "already_reviewed" }, partial_review: partial } }));
  render(<GeneralTaskPanel {...owner} task={cancelled} onInspectArtifact={vi.fn()} />);
  expect(await screen.findByLabelText("Accepted partial results pending debt")).toHaveTextContent("Current cancellation: fully_cancelled");
  expect(screen.getByLabelText("Accepted partial results pending debt")).toHaveTextContent("pending debt");
  expect(screen.getByLabelText("Accepted partial results pending debt")).toHaveTextContent("current unresolved effects: 0");
  expect(screen.getByLabelText("Accepted partial results pending debt")).toHaveTextContent("1 unresolved jobs and 2 unresolved effects at acceptance");
  expect(screen.getByLabelText("Accepted partial results pending debt")).toHaveTextContent("Zero current counts do not prove full success");
  expect(screen.queryByRole("button", { name: "Accept selected partial results" })).toBeNull();
});

it.each(["full success", "foreign owner", "renewed fence", "changed revision"])("quarantines a %s acceptance response instead of displaying success", async (mutation) => {
  vi.mocked(apiFetch).mockImplementation(async (_url, init) => {
    if (init?.method !== "POST") return response(plan);
    const value = receipt(JSON.parse(String(init.body)));
    if (mutation === "full success") return response({ ...value, task: { ...value.task, status: "done" } });
    if (mutation === "foreign owner") return response({ ...value, task: { ...value.task, owner_session_id: "foreign-session" } });
    if (mutation === "renewed fence") return response({ ...value, attempt: { ...value.attempt, fencing_token: 2 } });
    return response({ ...value, task: { ...value.task, task_revision: 9 } });
  });
  render(<GeneralTaskPanel {...owner} task={task} onInspectArtifact={vi.fn()} />);
  await selectAndInspect(); fireEvent.click(screen.getByLabelText("Acknowledge unresolved partial debt"));
  fireEvent.click(screen.getByRole("button", { name: "Accept selected partial results" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("unconfirmed");
  expect(screen.queryByLabelText("Accepted partial results pending debt")).toBeNull();
  expect(screen.getByRole("button", { name: "Reconcile exact partial decision" })).toBeEnabled();
});

it.each([
  ["foreign attempt", (value: typeof options) => ({ ...value, attempt_id: "foreign-attempt" })],
  ["stale manifest", (value: typeof options) => ({ ...value, expected_manifest_revision: 10 })],
  ["unknown output", (value: typeof options) => ({ ...value, selected_steps: [{ ...value.selected_steps[0], outputs: [{ ...output, delegation_invocation_id: "foreign-callback" }] }] })],
  ["private path", (value: typeof options) => ({ ...value, selected_steps: [{ ...value.selected_steps[0], outputs: [{ ...output, file_path: "private/path" }] }] })],
])("rejects %s partial options before offering inspection or selection", async (_label, change) => {
  const malformed = { ...plan, native_execution: { ...native, partial_review_options: change(options) } };
  expect(() => validateGeneralTaskPlan(malformed, task)).toThrow();
  vi.mocked(apiFetch).mockResolvedValue(response(malformed));
  render(<GeneralTaskPanel {...owner} task={task} onInspectArtifact={vi.fn()} />);
  await screen.findByRole("alert"); expect(screen.queryByLabelText("Review specialist partial results")).toBeNull();
});
