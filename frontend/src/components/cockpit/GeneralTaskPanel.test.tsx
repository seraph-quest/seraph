import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { apiFetch } from "../../lib/api";
import { GeneralTaskPanel } from "./GeneralTaskPanel";
import type { GoalInfo, WorkBoardTask } from "../../types";

vi.mock("../../lib/api", () => ({ apiFetch: vi.fn() }));
const owner = { ownerPrincipalId: "operator-one", ownerSessionId: "session-one" };
const goal = { id: "goal-one", title: "Owned Goal", revision: 3, status: "active", owner_session_id: owner.ownerSessionId } as GoalInfo;
const task = { task_id: "task-one", capability_id: "agent.task.v1", task_revision: 2, status: "triage", goal_id: goal.id, goal_revision: 3, owner_principal_id: owner.ownerPrincipalId, owner_session_id: owner.ownerSessionId, requires_review: true } as WorkBoardTask;
const plan = { task_id: task.task_id, task_revision: 2, accepted: false, no_learning: true,
  task_input: { goal_ref: goal.id, intent: "Prepare a local note", limits: { max_steps: 16, max_inference_calls: 12, wall_seconds: 900, depth: 0, max_outstanding_children: 2, max_cost_microusd: 0 } },
  plan: { schema_version: 1, revision: 1, steps: [{ step_id: "note", tool_id: "local.note", input: { text: "<script>untrusted</script>" }, depends_on: [], output_contract: { type: "object" } }] },
  descriptors: [{ tool_id: "local.note", version: "1", input_schema: { type: "object" }, output_schema: { type: "object" }, effects: ["local_artifact"], permissions: ["artifact_write"], deadline: 30, verifier: "artifact_readback" }], strategy: { status: "none", reason: "baseline" } };
const documentBinding = { artifact_ref: "document-source:11111111-1111-4111-8111-111111111111", source_revision: 7, metadata_digest: "11".repeat(32), citation_refs: ["pdf#page=1"], selection_digest: "22".repeat(32), acknowledge_local_use: true as const };
const documentPlan = { ...plan, accepted: true, task_input: { ...plan.task_input, limits: { max_steps: 1, max_inference_calls: 0, wall_seconds: 60, depth: 0, max_outstanding_children: 0, max_cost_microusd: 0 }, document_source: documentBinding },
  plan: { schema_version: 1, revision: 1, steps: [{ step_id: "prepare", tool_id: "document_prepare", input: { selection_digest: documentBinding.selection_digest }, depends_on: [], output_contract: { type: "object" } }] },
  descriptors: [{ tool_id: "document_prepare", version: "1", input_schema: { type: "object" }, output_schema: { type: "object" }, effects: ["owner_private_read", "local_compute"], permissions: ["capability_execute", "document_local_use"], deadline: 30, verifier: "document_selected_private_readback.v1" }] };
function response(value: unknown, status = 200) { return new Response(JSON.stringify(value), { status }); }
beforeEach(() => { vi.mocked(apiFetch).mockReset(); });

it("submits ordinary intent without a forged tool plan and opens its persisted inert Work task", async () => {
  const created = vi.fn();
  vi.mocked(apiFetch).mockImplementation(async (_url, init) => {
    const body = JSON.parse(String(init?.body));
    return response({ task: { ...task, idempotency_key: body.idempotency_key } });
  });
  render(<GeneralTaskPanel {...owner} goals={[goal]} onCreated={created} />);
  fireEvent.change(screen.getByLabelText("Task Goal"), { target: { value: goal.id } });
  fireEvent.change(screen.getByLabelText("What should Seraph do?"), { target: { value: "Prepare a local note" } });
  fireEvent.click(screen.getByRole("button", { name: "Prepare task plan" }));
  await waitFor(() => expect(created).toHaveBeenCalled());
  const body = JSON.parse(String(vi.mocked(apiFetch).mock.calls[0][1]?.body));
  expect(body.plan).toBeUndefined(); expect(body.accept).toBeUndefined();
  expect(body.input.intent).toBe("Prepare a local note"); expect(body.input.tool_set_digest).toBeUndefined();
  expect(body.input.limits.max_cost_microusd).toBe(0);
});

it("retains one uncertain create request for explicit exact retry, without auto replay", async () => {
  vi.mocked(apiFetch).mockRejectedValueOnce(new Error("connection unavailable")).mockImplementation(async (_url, init) => {
    const body = JSON.parse(String(init?.body)); return response({ task: { ...task, owner_session_id: "retry-session", idempotency_key: body.idempotency_key } });
  });
  render(<GeneralTaskPanel {...owner} ownerSessionId="retry-session" goals={[{ ...goal, owner_session_id: "retry-session" }]} />);
  fireEvent.change(screen.getByLabelText("Task Goal"), { target: { value: goal.id } });
  fireEvent.change(screen.getByLabelText("What should Seraph do?"), { target: { value: "Prepare note" } });
  fireEvent.click(screen.getByRole("button", { name: "Prepare task plan" }));
  await screen.findByRole("alert"); expect(apiFetch).toHaveBeenCalledTimes(1);
  expect(screen.getByLabelText("What should Seraph do?")).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Retry exact task request" }));
  await waitFor(() => expect(apiFetch).toHaveBeenCalledTimes(2));
  expect(vi.mocked(apiFetch).mock.calls[0][1]?.body).toBe(vi.mocked(apiFetch).mock.calls[1][1]?.body);
});

it("shows descriptor effects and exact limits, then accepts only the reviewed revision", async () => {
  const changed = vi.fn(); vi.mocked(apiFetch).mockImplementation(async () => response(plan));
  render(<GeneralTaskPanel {...owner} task={task} onChanged={changed} />);
  await screen.findByText("note · local.note v1");
  expect(screen.queryByRole("button", { name: "Open authenticated private preparation" })).toBeNull();
  expect(screen.getByText(/Effects: local_artifact/)).toHaveTextContent("artifact_write");
  expect(screen.getByRole("button", { name: "Accept reviewed task plan" })).toBeDisabled();
  fireEvent.click(screen.getByText("I reviewed this exact plan, effects, permissions and limits."));
  fireEvent.click(screen.getByRole("button", { name: "Accept reviewed task plan" }));
  await waitFor(() => expect(changed).toHaveBeenCalledTimes(1));
  expect(JSON.parse(String(vi.mocked(apiFetch).mock.calls[1][1]?.body))).toEqual({ action: "promote", expected_revision: 2 });
});

it("keeps the authenticated document view closed across reload until an explicit canonical task read", async () => {
  vi.mocked(apiFetch).mockImplementation(async () => response(documentPlan));
  const mounted = render(<GeneralTaskPanel {...owner} task={task} />);
  const open = await screen.findByRole("button", { name: "Open authenticated private preparation" });
  expect(apiFetch).toHaveBeenCalledTimes(1);
  expect(vi.mocked(apiFetch).mock.calls.some(([url]) => String(url).includes("/api/documents/preparations/"))).toBe(false);
  mounted.unmount();
  render(<GeneralTaskPanel {...owner} task={task} />);
  await screen.findByRole("button", { name: "Open authenticated private preparation" });
  expect(apiFetch).toHaveBeenCalledTimes(2);
  expect(open).toBeDefined();
});

it("opens only the canonical task private view and rejects a blocked or stale readback", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response(documentPlan)).mockResolvedValueOnce(response({ detail: { code: "document_preparation_not_completed" } }, 409));
  render(<GeneralTaskPanel {...owner} task={task} />);
  fireEvent.click(await screen.findByRole("button", { name: "Open authenticated private preparation" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("not complete");
  expect(String(vi.mocked(apiFetch).mock.calls[1][0])).toContain("/api/documents/preparations/task-one");
  expect(vi.mocked(apiFetch).mock.calls[1][1]?.body).toBeUndefined();
  expect(screen.queryByRole("region", { name: "Authenticated private cited preparation" })).toBeNull();
});

it("surfaces stale source authority as a blocked private readback", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response(documentPlan)).mockResolvedValueOnce(response({ detail: { code: "document_preparation_source_changed" } }, 409));
  render(<GeneralTaskPanel {...owner} task={task} />);
  fireEvent.click(await screen.findByRole("button", { name: "Open authenticated private preparation" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("document_preparation_source_changed");
  expect(screen.queryByRole("region", { name: "Authenticated private cited preparation" })).toBeNull();
});

it("rejects an unknown provider-contact readback instead of rendering private text", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response(documentPlan)).mockResolvedValueOnce(response({ task_id: task.task_id, status: "succeeded", sections: [{ source_ref: "pdf#page=1", text: "must stay hidden" }], no_learning: true, provider_contacts: 1 }));
  render(<GeneralTaskPanel {...owner} task={task} />);
  fireEvent.click(await screen.findByRole("button", { name: "Open authenticated private preparation" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("provider contact");
  expect(screen.queryByText("must stay hidden")).toBeNull();
});

it("renders the authenticated private cited view only after a successful explicit readback", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response(documentPlan)).mockResolvedValueOnce(response({ task_id: task.task_id, status: "succeeded", sections: [{ source_ref: "pdf#page=1", text: "owner private paragraph" }], no_learning: true, provider_contacts: 0 }));
  render(<GeneralTaskPanel {...owner} task={task} />);
  fireEvent.click(await screen.findByRole("button", { name: "Open authenticated private preparation" }));
  const view = await screen.findByRole("region", { name: "Authenticated private cited preparation" });
  expect(view).toHaveTextContent("owner private paragraph");
  expect(view).toHaveTextContent("Cached value unavailable; freshness unknown. Formula remains inert.");
  expect(String(vi.mocked(apiFetch).mock.calls[1][0])).toContain(encodeURIComponent(task.task_id));
});

it("rejects stale readbacks and requires refresh after acceptance conflict", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response({ ...plan, task_revision: 1 })).mockResolvedValueOnce(response(plan)).mockResolvedValueOnce(response({}, 409));
  render(<GeneralTaskPanel {...owner} task={task} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("did not match");
  expect(screen.queryByRole("button", { name: "Accept reviewed task plan" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Refresh current task plan" }));
  await screen.findByText("note · local.note v1");
  fireEvent.click(screen.getByText("I reviewed this exact plan, effects, permissions and limits."));
  fireEvent.click(screen.getByRole("button", { name: "Accept reviewed task plan" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("review again");
  expect(screen.queryByRole("button", { name: "Accept reviewed task plan" })).toBeNull();
});

it("does not read or accept a foreign task and clears a late previous-owner plan", async () => {
  let resolve!: (value: Response) => void;
  vi.mocked(apiFetch).mockImplementation(() => new Promise(r => { resolve = r; }));
  const mounted = render(<GeneralTaskPanel {...owner} task={task} />);
  mounted.rerender(<GeneralTaskPanel {...owner} ownerSessionId="other-session" task={task} />);
  resolve(response(plan)); await waitFor(() => expect(screen.getByText(/current task ownership/)).toBeInTheDocument());
  expect(screen.queryByText("note · local.note v1")).toBeNull(); expect(apiFetch).toHaveBeenCalledTimes(1);
});

it("saves an edited inert plan against exact task and plan revisions without accepting it", async () => {
  const changed = vi.fn();
  vi.mocked(apiFetch).mockResolvedValueOnce(response(plan)).mockResolvedValueOnce(response({ task: { ...task, task_revision: 3 } }));
  render(<GeneralTaskPanel {...owner} task={task} onChanged={changed} />);
  await screen.findByText("note · local.note v1");
  fireEvent.change(screen.getByLabelText("Typed plan steps"), { target: { value: JSON.stringify([{ ...plan.plan.steps[0], input: { text: "Revised note" } }]) } });
  expect(screen.getByRole("button", { name: "Accept reviewed task plan" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Save inert plan revision" }));
  await waitFor(() => expect(changed).toHaveBeenCalledTimes(1));
  const body = JSON.parse(String(vi.mocked(apiFetch).mock.calls[1][1]?.body));
  expect(body.expected_revision).toBe(2); expect(body.expected_plan_revision).toBe(1); expect(body.plan.revision).toBe(2);
  expect(body.plan.steps[0].input.text).toBe("Revised note"); expect(body.action).toBeUndefined();
});

it("keeps the exact uncertain edit and clears the stale plan after a revision conflict", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response(plan)).mockRejectedValueOnce(Error("Connection lost")).mockResolvedValueOnce(response({}, 409));
  render(<GeneralTaskPanel {...owner} task={task} />);
  await screen.findByText("note · local.note v1");
  fireEvent.change(screen.getByLabelText("Typed plan steps"), { target: { value: JSON.stringify([{ ...plan.plan.steps[0], input: { text: "Revised note" } }]) } });
  fireEvent.click(screen.getByRole("button", { name: "Save inert plan revision" }));
  await screen.findByRole("alert");
  expect(screen.getByLabelText("Typed plan steps")).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Reconcile exact plan edit" }));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("review again"));
  expect(vi.mocked(apiFetch).mock.calls[1][1]?.body).toBe(vi.mocked(apiFetch).mock.calls[2][1]?.body);
  expect(screen.queryByRole("button", { name: "Accept reviewed task plan" })).toBeNull();
  expect(screen.getByRole("button", { name: "Refresh current task plan" })).toBeEnabled();
});
it("retains a blocked proposal on the same card and saves its first valid inert plan", async () => {
  const changed = vi.fn();
  vi.mocked(apiFetch).mockResolvedValueOnce(response({ ...plan, plan: null, descriptors: [], proposal_error: "general_task_plan_invalid" }))
    .mockResolvedValueOnce(response({ tools: plan.descriptors })).mockResolvedValueOnce(response({ task: { ...task, task_revision: 3 } }));
  render(<GeneralTaskPanel {...owner} task={task} onChanged={changed} />);
  await screen.findByText(/Proposal blocked: general_task_plan_invalid/);
  expect(screen.getByRole("button", { name: "Accept reviewed task plan" })).toBeDisabled();
  expect(screen.getByLabelText("Typed plan steps")).toHaveValue("[]");
  fireEvent.change(screen.getByLabelText("Typed plan steps"), { target: { value: JSON.stringify(plan.plan.steps) } });
  fireEvent.click(screen.getByRole("button", { name: "Save inert plan revision" }));
  await waitFor(() => expect(changed).toHaveBeenCalledTimes(1));
  const body = JSON.parse(String(vi.mocked(apiFetch).mock.calls[2][1]?.body));
  expect(body.expected_plan_revision).toBe(0); expect(body.plan.revision).toBe(1);
});

const pausedTask = { ...task, status: "blocked", recovery_action: "approve_existing_run", latest_attempt: {
  attempt_id: "attempt-one", workflow_run_id: "run-one", fencing_token: 4, ended_at: null,
} } as WorkBoardTask;
const pause = { approval_id: "approval-one", approval_status: "approved", step_id: "note", tool_id: "local.note",
  workflow_run_id: "run-one", attempt_id: "attempt-one", fencing_token: 4, workflow_revision: 9,
  original_deadline_at: new Date(Date.now() + 600000).toISOString(), can_resume: true, reason: null };
const pausedPlan = { ...plan, accepted: true, approval_pause: pause };
it("continues only by explicit action with the exact approved existing run receipt", async () => {
  const changed = vi.fn();
  vi.mocked(apiFetch).mockResolvedValueOnce(response(pausedPlan)).mockResolvedValueOnce(response({ task: { ...pausedTask, latest_attempt: { ...pausedTask.latest_attempt, fencing_token: 5 } } }));
  render(<GeneralTaskPanel {...owner} task={pausedTask} onChanged={changed} />);
  const button = await screen.findByRole("button", { name: "Continue approved task run" });
  expect(apiFetch).toHaveBeenCalledTimes(1);
  fireEvent.click(button);
  await waitFor(() => expect(changed).toHaveBeenCalledTimes(1));
  expect(String(vi.mocked(apiFetch).mock.calls[1][0])).toContain("/tasks/task-one/plan/resume");
  expect(JSON.parse(String(vi.mocked(apiFetch).mock.calls[1][1]?.body))).toEqual({ expected_revision: 2,
    expected_plan_revision: 1, workflow_run_id: "run-one", attempt_id: "attempt-one", fencing_token: 4,
    workflow_revision: 9, approval_id: "approval-one" });
});
it.each(["pending", "denied", "expired", "revoked", "consumed", "unavailable"])("never continues a %s approval after reload", async approval_status => {
  vi.mocked(apiFetch).mockResolvedValue(response({ ...pausedPlan, approval_pause: { ...pause, approval_status } }));
  render(<GeneralTaskPanel {...owner} task={pausedTask} />);
  expect(await screen.findByRole("button", { name: "Continue approved task run" })).toBeDisabled();
  expect(apiFetch).toHaveBeenCalledTimes(1);
});
it.each([
  { original_deadline_at: "2000-01-01T00:00:00Z" }, { attempt_id: "another-attempt" },
  { workflow_run_id: "another-run" }, { fencing_token: 5 }, { can_resume: false },
  { step_id: "another-step" },
])("blocks stale or unbound approval readback %j", async change => {
  vi.mocked(apiFetch).mockResolvedValue(response({ ...pausedPlan, approval_pause: { ...pause, ...change } }));
  render(<GeneralTaskPanel {...owner} task={pausedTask} />);
  expect(await screen.findByRole("button", { name: "Continue approved task run" })).toBeDisabled();
});
it("clears continuation after conflict or uncertain response and never replays it", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response(pausedPlan)).mockRejectedValueOnce(Error("Connection lost"))
    .mockResolvedValueOnce(response({ ...pausedPlan, approval_pause: { ...pause, approval_status: "consumed", can_resume: false } }));
  render(<GeneralTaskPanel {...owner} task={pausedTask} />);
  fireEvent.click(await screen.findByRole("button", { name: "Continue approved task run" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("never automatically replayed");
  expect(screen.queryByRole("button", { name: "Continue approved task run" })).toBeNull();
  expect(apiFetch).toHaveBeenCalledTimes(2);
  fireEvent.click(screen.getByRole("button", { name: "Refresh current task plan" }));
  expect(await screen.findByRole("button", { name: "Continue approved task run" })).toBeDisabled();
  expect(vi.mocked(apiFetch).mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
});
it("requires fresh review after a revoked grant response", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response(pausedPlan)).mockResolvedValueOnce(response({}, 409));
  render(<GeneralTaskPanel {...owner} task={pausedTask} />);
  fireEvent.click(await screen.findByRole("button", { name: "Continue approved task run" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("review again");
  expect(screen.queryByRole("button", { name: "Continue approved task run" })).toBeNull();
});
it("does not expose continuation for an unknown-effect task even with an inconsistent approved pause", async () => {
  vi.mocked(apiFetch).mockResolvedValue(response(pausedPlan));
  render(<GeneralTaskPanel {...owner} task={{ ...pausedTask, recovery_action: "reconcile_external_effect" }} />);
  expect(await screen.findByRole("button", { name: "Continue approved task run" })).toBeDisabled();
});
