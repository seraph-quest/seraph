import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { apiFetch } from "../../lib/api";
import type { WorkBoardTask } from "../../types";
import { TaskLessonReview } from "./TaskLessonReview";
vi.mock("../../lib/api", () => ({ apiFetch: vi.fn() }));
const owner = { ownerPrincipalId: "operator", ownerSessionId: "session" };
const task = { task_id: "task", task_revision: 3, goal_id: "goal", goal_revision: 2, owner_principal_id: "operator", owner_session_id: "session", status: "done" } as WorkBoardTask;
const scope = { goal_id: "goal", goal_revision: 2, family: "general" };
const source = { task_id: "task", expected_revision: 3, attempt_id: "attempt", source_refs: ["artifact:verified"], scope, eligible: true, reason_code: "verified_ordinary_task", automatic_policy: { enabled: false, policy_revision: null, daily_cap: 2, inference_egress: "not_permitted", adoption: "requires_separate_review" } };
const oldMethod = { schema_version: "TaskMethod.v1", family: "general", steps: [{ kind: "registered_tool", tool_id: "write_note" }], registered_tool_ids: ["write_note"], input_parameters: {}, output_contract: { artifact_type: "task_result", required_fields: ["status", "source_refs"] } };
const lesson = { schema_version: "task_method_proposal.v1", proposal_id: "proposal", task_id: "task", attempt_id: "attempt", revision: 1, status: "proposed", result: "candidate_inert", reason_code: "explicit_correction", scope, behavior_changed: false, source_current: true, correction: "Check source existence", old_method: oldMethod, new_method: { ...oldMethod, steps: [{ kind: "guard", check: "source_exists" }, ...oldMethod.steps] } };
const response = (v: unknown, status = 200) => new Response(JSON.stringify(v), { status });
beforeEach(() => { vi.mocked(apiFetch).mockReset(); });
async function inspect() { fireEvent.click(screen.getByRole("button", { name: "Inspect lesson sources" })); await screen.findByText("Verified sources available"); }
it("uses authoritative source refs and shows exact private old/new candidate without adoption", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response(source)).mockResolvedValueOnce(response({ proposal_id: "proposal" })).mockResolvedValueOnce(response(lesson));
  render(<TaskLessonReview {...owner} task={task} />); await inspect();
  fireEvent.change(screen.getByLabelText("Private task correction"), { target: { value: "Check source existence" } });
  fireEvent.click(screen.getByRole("button", { name: "Prepare private lesson candidate" }));
  await screen.findByRole("region", { name: "Exact private lesson change" });
  expect(JSON.parse(String(vi.mocked(apiFetch).mock.calls[1][1]?.body))).toEqual({ task_id: "task", attempt_id: "attempt", expected_revision: 3, correction: "Check source existence", source_refs: ["artifact:verified"], scope });
  expect(screen.getByLabelText("Old task method")).not.toHaveTextContent("source_exists");
  expect(screen.getByLabelText("Proposed task method")).toHaveTextContent("source_exists");
  expect(screen.queryByRole("button", { name: /adopt|accept/i })).toBeNull();
});
it("retains an unsupported correction as literal private no-change", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response(source)).mockResolvedValueOnce(response({ proposal_id: "proposal" })).mockResolvedValueOnce(response({ ...lesson, result: "no_change", reason_code: "unsupported_correction_no_change", new_method: null, correction: "<script>never execute</script>" }));
  render(<TaskLessonReview {...owner} task={task} />); await inspect();
  fireEvent.change(screen.getByLabelText("Private task correction"), { target: { value: "<script>never execute</script>" } });
  fireEvent.click(screen.getByRole("button", { name: "Prepare private lesson candidate" }));
  expect(await screen.findByText(/No change · unsupported_correction/)).toBeInTheDocument();
  expect(screen.getByText("Correction: <script>never execute</script>").querySelector("script")).toBeNull();
});
it("fails closed on changed task revision and unverified sources", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response({ ...source, expected_revision: 2 })).mockResolvedValueOnce(response({ ...source, eligible: false, source_refs: [], reason_code: "lesson_run_unverified" }));
  render(<TaskLessonReview {...owner} task={task} />);
  fireEvent.click(screen.getByRole("button", { name: "Inspect lesson sources" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("exact Work card");
  expect(screen.queryByLabelText("Private task correction")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Inspect lesson sources" }));
  expect(await screen.findByText(/Blocked: lesson_run_unverified/)).toBeInTheDocument();
});
it("requires explicit scoped automatic consent and supports revocation", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response(source)).mockResolvedValueOnce(response({ ...source.automatic_policy, enabled: true })).mockResolvedValueOnce(response(source.automatic_policy));
  render(<TaskLessonReview {...owner} task={task} />); await inspect();
  expect(screen.getByRole("button", { name: "Enable automatic task lesson proposals" })).toBeDisabled();
  fireEvent.click(screen.getByText(/Allow bounded private lesson proposals/));
  fireEvent.click(screen.getByRole("button", { name: "Enable automatic task lesson proposals" }));
  await screen.findByText(/Automatic proposals: enabled/);
  expect(JSON.parse(String(vi.mocked(apiFetch).mock.calls[1][1]?.body))).toEqual({ enabled: true, expected_revision: 3, mutation_uuid: expect.any(String) });
  fireEvent.click(screen.getByRole("button", { name: "Disable automatic task lesson proposals" }));
  await screen.findByText(/Automatic proposals: disabled/);
});
it("does not expose private correction or late lesson evidence after owner change", async () => {
  let resolve!: (v: Response) => void; vi.mocked(apiFetch).mockImplementation(() => new Promise(r => { resolve = r; }));
  const mounted = render(<TaskLessonReview {...owner} task={task} />);
  fireEvent.click(screen.getByRole("button", { name: "Inspect lesson sources" }));
  mounted.rerender(<TaskLessonReview {...owner} ownerSessionId="other" task={task} />);
  resolve(response(source)); await waitFor(() => expect(screen.getByRole("button", { name: "Inspect lesson sources" })).toBeDisabled());
  expect(screen.queryByText("artifact:verified")).toBeNull();
});
it("shows the bound automatic no-change/error outcome without claiming adoption", async () => {
  vi.mocked(apiFetch).mockResolvedValue(response({ ...source, automatic_outcome: { status: "blocked", result: "no_change", reason_code: "automatic_lesson_unavailable", error_type: "OSError", task_revision: 3, attempt_id: "attempt", workflow_run_id: "run", behavior_changed: false, provider_contacts: 0, outcome_binding: "a".repeat(64) } }));
  render(<TaskLessonReview {...owner} task={task} />); await inspect();
  const outcome = screen.getByRole("region", { name: "Automatic lesson outcome" });
  expect(outcome).toHaveTextContent("blocked · no change · automatic_lesson_unavailable");
  expect(outcome).toHaveTextContent("behavior unchanged · no provider contact");
  expect(outcome).toHaveTextContent("OSError");
  expect(screen.queryByRole("button", { name: /adopt|accept/i })).toBeNull();
});
it("rejects automatic outcomes from a different task revision or attempt", async () => {
  vi.mocked(apiFetch).mockResolvedValue(response({ ...source, automatic_outcome: { status: "blocked", result: "no_change", reason_code: "automatic_lesson_unavailable", task_revision: 2, attempt_id: "foreign", behavior_changed: false, provider_contacts: 0, outcome_binding: "a".repeat(64) } }));
  render(<TaskLessonReview {...owner} task={task} />); fireEvent.click(screen.getByRole("button", { name: "Inspect lesson sources" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("not bound");
  expect(screen.queryByRole("region", { name: "Automatic lesson outcome" })).toBeNull();
});
