import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { apiFetch } from "../../lib/api";
import type { GoalInfo, WorkBoardTask } from "../../types";
import wire from "../../test/fixtures/procedure-v3-wire.json";
import { ReusableProcedureSave } from "./ReusableProcedureSave";
import { TaskMethodReview } from "./TaskMethodReview";

vi.mock("../../lib/api", () => ({ apiFetch: vi.fn() }));
// Literal responses captured from the completed physical four-step native
// search/read/write/MCP journey. No UI-authored candidate or approval receipt.
const task = wire.source_task as unknown as WorkBoardTask;
const goal = wire.goal as unknown as GoalInfo;
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
beforeEach(() => vi.mocked(apiFetch).mockReset());
function mount() {
  return render(<ReusableProcedureSave task={task} ownerPrincipalId={task.owner_principal_id} ownerSessionId={task.owner_session_id} goals={[goal]} onCreated={vi.fn()} />);
}
it("shows only actual original producer offers after explicit source inspection", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response(wire.source));
  mount();
  expect(apiFetch).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Inspect reusable source" }));
  await screen.findByText("Eligible completed source");
  expect(screen.getAllByRole("textbox")).toHaveLength(wire.source.parameter_offers.length);
  expect(screen.getByLabelText("Reusable source receipts")).toHaveTextContent(wire.source.source_receipt.manifest_digest);
  expect(screen.getByRole("button", { name: "Save immutable method proposal" })).toBeDisabled();
  expect(screen.getAllByRole("textbox").every(input => (input as HTMLInputElement).value === "")).toBe(true);
});
it("fails closed on changed original attempt and does not offer save", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response({ ...wire.source, source_attempt: "changed-attempt" }));
  mount(); fireEvent.click(screen.getByRole("button", { name: "Inspect reusable source" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Original Task attempt or Goal changed");
  expect(screen.queryByRole("button", { name: "Save immutable method proposal" })).toBeNull();
});
it("denies saving from recovered read-only ownership", () => {
  render(<ReusableProcedureSave task={{ ...task, ownership_access: "recovered_read_only" }} ownerPrincipalId={task.owner_principal_id} ownerSessionId={task.owner_session_id} goals={[goal]} onCreated={vi.fn()} />);
  expect(screen.getByRole("button", { name: "Inspect reusable source" })).toBeDisabled();
  expect(apiFetch).not.toHaveBeenCalled();
});
it("uses actual save, adoption and signed current wire through fresh invocation and selects its new Task", async () => {
  const onCreated = vi.fn().mockResolvedValue(undefined);
  vi.mocked(apiFetch)
    .mockResolvedValueOnce(response(wire.source))
    .mockResolvedValueOnce(response(wire.save))
    .mockResolvedValueOnce(response(wire.pre_adopt))
    .mockResolvedValueOnce(response(wire.adopt))
    .mockResolvedValueOnce(response(wire.post_adopt))
    .mockResolvedValueOnce(response(wire.invoke));
  render(<ReusableProcedureSave task={task} ownerPrincipalId={task.owner_principal_id} ownerSessionId={task.owner_session_id} goals={[goal]} onCreated={onCreated} />);
  fireEvent.click(screen.getByRole("button", { name: "Inspect reusable source" }));
  await screen.findByText("Eligible completed source");
  for (const offer of wire.source.parameter_offers) {
    const parameter = wire.post_adopt.parameters.find(p => p.step_id === offer.step_id && p.input_pointer === offer.input_pointer)!;
    fireEvent.change(screen.getByLabelText(new RegExp(`Parameter name for ${offer.step_id}${offer.input_pointer}`)), { target: { value: parameter.name } });
  }
  fireEvent.click(screen.getByLabelText("Save this exact complete source and parameter selection as an immutable private proposal."));
  fireEvent.click(screen.getByRole("button", { name: "Save immutable method proposal" }));
  await screen.findByText(new RegExp(`Proposal ${wire.save.proposal_id} is inert`));
  const saveBody = JSON.parse(String(vi.mocked(apiFetch).mock.calls[1][1]?.body));
  expect(saveBody.source_attempt).toBe(wire.source.source_attempt);
  expect(saveBody.parameter_selections).toHaveLength(wire.post_adopt.parameters.length);
  expect(Object.keys(saveBody).sort()).toEqual(["expected_revision", "idempotency_key", "parameter_selections", "source_attempt"]);
  fireEvent.click(screen.getByRole("button", { name: "Inspect canonical method and scope" }));
  await screen.findByLabelText("Canonical proposed method");
  expect(screen.queryByRole("button", { name: "Create Task from current method" })).toBeNull();
  fireEvent.click(screen.getByLabelText("I reviewed this exact method and verified source evidence."));
  fireEvent.click(screen.getByRole("button", { name: "Adopt reviewed method" }));
  await screen.findByRole("button", { name: "Create Task from current method" });
  expect(screen.getByText(new RegExp(`Exact version ${wire.post_adopt.version}`))).toHaveTextContent(String(wire.post_adopt.pointer_revision));
  for (const [name, value] of Object.entries(wire.invoke_request.parameters)) {
    const input = screen.getByLabelText(`Fresh parameter ${name}`);
    expect((input as HTMLInputElement).value).toBe("");
    fireEvent.change(input, { target: { value: String(value) } });
  }
  fireEvent.change(screen.getByLabelText("Invocation Goal"), { target: { value: goal.id } });
  fireEvent.click(screen.getByLabelText("Create a fresh bounded Task with these exact values and current pin."));
  fireEvent.click(screen.getByRole("button", { name: "Create Task from current method" }));
  await screen.findByRole("button", { name: "Create Task from current method" });
  await vi.waitFor(() => expect(onCreated).toHaveBeenCalledWith(wire.invoke.task_id));
  const invokedBody = JSON.parse(String(vi.mocked(apiFetch).mock.calls[5][1]?.body));
  expect(invokedBody).toMatchObject({ version: wire.post_adopt.version, digest: wire.post_adopt.digest,
    expected_pointer_revision: wire.post_adopt.pointer_revision, parameters: wire.invoke_request.parameters,
    goal_id: goal.id, goal_revision: goal.revision, inference_egress_acknowledged: false });
  expect(wire.invoke.task_id).not.toBe(task.task_id);
  expect(Object.keys(invokedBody).sort()).toEqual(["digest", "expected_pointer_revision", "goal_id", "goal_revision", "idempotency_key", "inference_egress_acknowledged", "limits", "parameters", "version"]);
});
it.each([["method_invocation_pointer_changed", 409], ["method_invocation_noncurrent_version", 409], ["method_current_owner_required", 403], ["method_invocation_binding_missing", 409], ["idempotency_conflict", 409]] as const)("shows %s and requires explicit fresh inspection before another invocation", async (code, status) => {
  const onCreated = vi.fn();
  vi.mocked(apiFetch).mockResolvedValueOnce(response(wire.post_adopt)).mockResolvedValueOnce(response({ detail: { code } }, status));
  render(<TaskMethodReview task={task} proposalId={wire.save.proposal_id} owned goals={[goal]} onCreated={onCreated} />);
  fireEvent.click(screen.getByRole("button", { name: "Inspect canonical method and scope" }));
  await screen.findByRole("button", { name: "Create Task from current method" });
  for (const [name, value] of Object.entries(wire.invoke_request.parameters)) fireEvent.change(screen.getByLabelText(`Fresh parameter ${name}`), { target: { value: String(value) } });
  fireEvent.change(screen.getByLabelText("Invocation Goal"), { target: { value: goal.id } });
  fireEvent.click(screen.getByLabelText("Create a fresh bounded Task with these exact values and current pin."));
  fireEvent.click(screen.getByRole("button", { name: "Create Task from current method" }));
  expect(await screen.findByText(new RegExp(`^${code}:`))).toHaveTextContent("Inspect again");
  expect(screen.getByRole("button", { name: "Create Task from current method" })).toBeDisabled();
  expect(onCreated).not.toHaveBeenCalled();
  expect(apiFetch).toHaveBeenCalledTimes(2);
});
it("reads actual family disable, exact prior activation and canonical tombstone after explicit deletion confirmation", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response(wire.post_rollback))
    .mockResolvedValueOnce(response(wire.disable)).mockResolvedValueOnce(response(wire.post_disable))
    .mockResolvedValueOnce(response(wire.activate)).mockResolvedValueOnce(response(wire.post_activate))
    .mockResolvedValueOnce(response(wire.delete)).mockResolvedValueOnce(response(wire.post_delete));
  render(<TaskMethodReview task={task} proposalId={wire.save.proposal_id} owned goals={[goal]} onCreated={vi.fn()} />);
  fireEvent.click(screen.getByRole("button", { name: "Inspect canonical method and scope" }));
  await screen.findByRole("button", { name: "Create Task from current method" });
  fireEvent.change(screen.getByLabelText("Review reason"), { target: { value: "Explicit family disable" } });
  fireEvent.click(screen.getByRole("button", { name: "Disable general-task method selection" }));
  await screen.findByText("Future tasks use configured baseline.");
  expect(screen.queryByRole("button", { name: "Create Task from current method" })).toBeNull();
  expect(JSON.parse(String(vi.mocked(apiFetch).mock.calls[1][1]?.body))).toMatchObject({ action: "disable", scope_digest: wire.post_rollback.scope_digest });
  fireEvent.change(screen.getByLabelText("Review reason"), { target: { value: "Explicit prior activation" } });
  fireEvent.click(screen.getByRole("button", { name: "Activate exact reviewed prior method" }));
  await screen.findByRole("button", { name: "Create Task from current method" });
  expect(JSON.parse(String(vi.mocked(apiFetch).mock.calls[3][1]?.body))).toMatchObject({ action: "activate", scope_digest: wire.post_disable.scope_digest });
  fireEvent.change(screen.getByLabelText("Review reason"), { target: { value: "Explicit canonical deletion" } });
  expect(screen.getByRole("button", { name: "Delete canonical method version" })).toBeDisabled();
  fireEvent.click(screen.getByLabelText("Tombstone this exact canonical method version. Its next execution boundary will stop; source Tasks, artifacts and audit remain."));
  fireEvent.click(screen.getByRole("button", { name: "Delete canonical method version" }));
  await screen.findByRole("region", { name: "Deleted canonical method" });
  expect(screen.getByText(new RegExp(`tombstone ${wire.post_delete.tombstone.id}`))).toBeInTheDocument();
  expect(screen.queryByLabelText("Canonical proposed method")).toBeNull();
  expect(screen.queryByRole("button", { name: "Create Task from current method" })).toBeNull();
  expect(screen.queryByRole("button", { name: "Adopt reviewed method" })).toBeNull();
  expect(JSON.parse(String(vi.mocked(apiFetch).mock.calls[5][1]?.body))).toMatchObject({ action: "delete", scope_digest: wire.post_activate.scope_digest });
  expect(apiFetch).toHaveBeenCalledTimes(7);
});
it("retains the unresolved whole-request publication key across explicit same-pin reinspection", async () => {
  const onCreated = vi.fn().mockResolvedValue(undefined);
  vi.mocked(apiFetch).mockResolvedValueOnce(response(wire.post_adopt)).mockRejectedValueOnce(Error("response lost"))
    .mockResolvedValueOnce(response(wire.post_adopt)).mockResolvedValueOnce(response(wire.invoke));
  render(<TaskMethodReview task={task} proposalId={wire.save.proposal_id} owned goals={[goal]} onCreated={onCreated} />);
  async function fillAndInvoke() {
    for (const [name, value] of Object.entries(wire.invoke_request.parameters)) fireEvent.change(screen.getByLabelText(`Fresh parameter ${name}`), { target: { value: String(value) } });
    fireEvent.change(screen.getByLabelText("Invocation Goal"), { target: { value: goal.id } });
    fireEvent.click(screen.getByLabelText("Create a fresh bounded Task with these exact values and current pin."));
    fireEvent.click(screen.getByRole("button", { name: "Create Task from current method" }));
  }
  fireEvent.click(screen.getByRole("button", { name: "Inspect canonical method and scope" }));
  await screen.findByRole("button", { name: "Create Task from current method" });
  await fillAndInvoke();
  await screen.findByText("response lost");
  fireEvent.click(screen.getByRole("button", { name: "Inspect canonical method and scope" }));
  await screen.findByRole("button", { name: "Create Task from current method" });
  await fillAndInvoke();
  await vi.waitFor(() => expect(onCreated).toHaveBeenCalledWith(wire.invoke.task_id));
  expect(JSON.parse(String(vi.mocked(apiFetch).mock.calls[1][1]?.body))).toEqual(JSON.parse(String(vi.mocked(apiFetch).mock.calls[3][1]?.body)));
});
it("rolls back B to its exact previous A pin and opens A's actual source without activating from history", async () => {
  const onCreated = vi.fn().mockResolvedValue(undefined);
  vi.mocked(apiFetch).mockResolvedValueOnce(response(wire.pre_rollback))
    .mockResolvedValueOnce(response(wire.rollback)).mockResolvedValueOnce(response(wire.post_rollback_b));
  render(<TaskMethodReview task={wire.rollback_source_task as unknown as WorkBoardTask} proposalId={wire.pre_rollback.proposal_id} owned goals={[goal]} onCreated={onCreated} />);
  fireEvent.click(screen.getByRole("button", { name: "Inspect canonical method and scope" }));
  await screen.findByLabelText("Canonical proposed method");
  fireEvent.change(screen.getByLabelText("Review reason"), { target: { value: "Restore exact signed A" } });
  fireEvent.click(screen.getByRole("button", { name: "Restore exact previous method or baseline" }));
  await screen.findByText(new RegExp(`Active version ${wire.post_rollback.active_binding.version}`));
  expect(JSON.parse(String(vi.mocked(apiFetch).mock.calls[1][1]?.body))).toMatchObject({
    proposal_id: wire.pre_rollback.proposal_id, action: "rollback", scope_digest: wire.pre_rollback.scope_digest,
    artifact_digest: wire.pre_rollback.artifact_digest, expected_revision: wire.pre_rollback.expected_revision,
  });
  expect(screen.queryByRole("button", { name: "Create Task from current method" })).toBeNull();
  expect(screen.getByRole("button", { name: "Adopt reviewed method" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: `Open source Task for method ${wire.save.proposal_id}` }));
  expect(onCreated).toHaveBeenCalledWith(wire.source_task.task_id);
  expect(apiFetch).toHaveBeenCalledTimes(3);
});
