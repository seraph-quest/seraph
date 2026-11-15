import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { apiFetch } from "../../lib/api";
import type { WorkBoardTask } from "../../types";
import { TaskMethodReview } from "./TaskMethodReview";
vi.mock("../../lib/api", () => ({ apiFetch: vi.fn() }));
const task = { task_id: "task", task_revision: 3, goal_id: "goal", goal_revision: 2, owner_session_id: "session" } as WorkBoardTask;
const data = { task_id: "task", attempt_id: "attempt", source_refs: ["artifact:verified"], observed: { status: "completed", readback_digest: "c".repeat(64) }, proposal_id: "proposal", expected_revision: 4, artifact_digest: "a".repeat(64), scope_digest: "b".repeat(64),
  scope: { goal_id: "goal", goal_revision: 2, family: "general" }, old_method: null,
  new_method: { schema_version: "TaskMethod.v1", family: "general", steps: [{ kind: "registered_tool", tool_id: "read_file" }] },
  active_binding: null, quality_evidence: "unmeasured", adoption_requires_current_owner: false, configured_baseline: false };
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
beforeEach(() => { vi.mocked(apiFetch).mockReset(); });
async function inspect() {
  fireEvent.click(screen.getByRole("button", { name: "Inspect canonical method and scope" }));
  await screen.findByLabelText("Canonical proposed method");
}
it("requires literal inspection then posts the exact seven-field scope review", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response(data)).mockResolvedValueOnce(response({ status: "accepted" }));
  render(<TaskMethodReview task={task} proposalId="proposal" owned />);
  expect(apiFetch).not.toHaveBeenCalled();
  expect(screen.queryByRole("button", { name: "Adopt reviewed method" })).toBeNull();
  await inspect();
  expect(screen.getByText(/Quality is unmeasured/)).toBeInTheDocument();
  fireEvent.click(screen.getByLabelText("I reviewed this exact method and verified source evidence."));
  fireEvent.click(screen.getByRole("button", { name: "Adopt reviewed method" }));
  await screen.findByText(/Review recorded/);
  expect(JSON.parse(String(vi.mocked(apiFetch).mock.calls[1][1]?.body))).toEqual({ proposal_id: "proposal", expected_revision: 4,
    artifact_digest: data.artifact_digest, scope_digest: data.scope_digest, action: "accept", reason: "", idempotency_key: expect.any(String) });
  expect(screen.queryByLabelText("Canonical proposed method")).toBeNull();
});
it("requires a reason and explicitly rolls back future tasks to baseline", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response(data)).mockResolvedValueOnce(response({ status: "rolled_back" }));
  render(<TaskMethodReview task={task} proposalId="proposal" owned />); await inspect();
  expect(screen.getByRole("button", { name: "Rollback to baseline" })).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Review reason"), { target: { value: "Use baseline for future tasks" } });
  fireEvent.click(screen.getByRole("button", { name: "Rollback to baseline" }));
  await screen.findByText(/Review recorded/);
  expect(JSON.parse(String(vi.mocked(apiFetch).mock.calls[1][1]?.body)).action).toBe("rollback");
});
it("blocks changed Goal scope and unmeasured evidence mismatch", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response({ ...data, scope: { ...data.scope, goal_revision: 9 } }));
  render(<TaskMethodReview task={task} proposalId="proposal" owned />);
  fireEvent.click(screen.getByRole("button", { name: "Inspect canonical method and scope" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("does not match");
  expect(screen.queryByRole("button", { name: "Adopt reviewed method" })).toBeNull();
});
it.each([
  ["missing references", { source_refs: [] }],
  ["different task", { task_id: "other-task" }],
  ["different attempt", { attempt_id: "other-attempt" }],
  ["different references", { source_refs: ["artifact:other"] }],
  ["invalid observed receipt", { observed: { status: "completed", readback_digest: "invalid" } }],
])("blocks canonical inspection with %s", async (_name, changed) => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response({ ...data, ...changed }));
  render(<TaskMethodReview task={task} proposalId="proposal" owned attemptId="attempt" sourceRefs={["artifact:verified"]} />);
  fireEvent.click(screen.getByRole("button", { name: "Inspect canonical method and scope" }));
  await screen.findByRole("alert");
  expect(screen.queryByRole("button", { name: "Adopt reviewed method" })).toBeNull();
  expect(screen.queryByRole("region", { name: "Canonical method source provenance" })).toBeNull();
  expect(apiFetch).toHaveBeenCalledTimes(1);
});
it("denies recovered review and clears late private data on owner change", async () => {
  let resolve!: (value: Response) => void;
  vi.mocked(apiFetch).mockImplementation(() => new Promise(r => { resolve = r; }));
  const mounted = render(<TaskMethodReview task={task} proposalId="proposal" owned />);
  fireEvent.click(screen.getByRole("button", { name: "Inspect canonical method and scope" }));
  mounted.rerender(<TaskMethodReview task={{ ...task, owner_session_id: "other" }} proposalId="proposal" owned={false} />);
  resolve(response(data));
  await waitFor(() => expect(screen.getByRole("button", { name: "Inspect canonical method and scope" })).toBeDisabled());
  expect(screen.queryByLabelText("Canonical proposed method")).toBeNull();
});
it("disables adoption for server inspection-only ownership", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response({ ...data, adoption_requires_current_owner: true }));
  render(<TaskMethodReview task={task} proposalId="proposal" owned />); await inspect();
  expect(screen.getByRole("button", { name: "Adopt reviewed method" })).toBeDisabled();
});
it("drops reviewed stale CAS after a rejected action without auto-retry", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response(data)).mockResolvedValueOnce(response({ detail: "changed" }, 409));
  render(<TaskMethodReview task={task} proposalId="proposal" owned />); await inspect();
  fireEvent.click(screen.getByRole("button", { name: "Reject method" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Inspect again");
  expect(apiFetch).toHaveBeenCalledTimes(2);
});
