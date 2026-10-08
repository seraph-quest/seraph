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
  expect(screen.getByText(/Effects: local_artifact/)).toHaveTextContent("artifact_write");
  expect(screen.getByRole("button", { name: "Accept reviewed task plan" })).toBeDisabled();
  fireEvent.click(screen.getByText("I reviewed this exact plan, effects, permissions and limits."));
  fireEvent.click(screen.getByRole("button", { name: "Accept reviewed task plan" }));
  await waitFor(() => expect(changed).toHaveBeenCalledTimes(1));
  expect(JSON.parse(String(vi.mocked(apiFetch).mock.calls[1][1]?.body))).toEqual({ action: "promote", expected_revision: 2 });
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
