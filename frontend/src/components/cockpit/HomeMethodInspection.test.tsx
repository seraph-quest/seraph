import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { apiFetch } from "../../lib/api";
import { HomeMethodInspection } from "./HomeMethodInspection";
vi.mock("../../lib/api", () => ({ apiFetch: vi.fn() }));
const target = { kind: "method" as const, proposal_id: "proposal", version: "version", digest: "d".repeat(64) };
const owner = { principalId: "operator", sessionId: "root" };
const task = { task_id: "source-task", task_revision: 3, goal_id: "source-goal", goal_revision: 2, owner_session_id: "root", owner_principal_id: "operator", latest_attempt: { attempt_id: "source-attempt" } };
const preview = { task_id: "source-task", attempt_id: "source-attempt", source_refs: ["artifact:verified"], observed: { status: "completed", readback_digest: "c".repeat(64) }, proposal_id: "proposal", expected_revision: 4, artifact_digest: "a".repeat(64), scope_digest: "b".repeat(64), scope: { goal_id: "source-goal", goal_revision: 2, family: "general" }, old_method: null, new_method: { schema_version: "TaskMethod.v1" }, active_binding: null, quality_evidence: "unmeasured", adoption_requires_current_owner: false, configured_baseline: true };
const response = (v: unknown, status = 200) => new Response(JSON.stringify(v), { status });
beforeEach(() => vi.mocked(apiFetch).mockReset());
it("opens the exact original proposal and genuine producer Task before exposing existing rollback controls", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response(preview)).mockResolvedValueOnce(response({ task }));
  render(<HomeMethodInspection target={target} owner={owner} />);
  await screen.findByLabelText("Canonical proposed method");
  expect(apiFetch).toHaveBeenCalledTimes(2); expect(vi.mocked(apiFetch).mock.calls[1][0]).toContain("/tasks/source-task");
  fireEvent.change(screen.getByLabelText("Review reason"), { target: { value: "Return future selection to baseline" } });
  expect(screen.getByRole("button", { name: "Rollback to baseline" })).toBeEnabled();
});
it("retains recovered history as inspection only even if a stale owner preview claims current controls", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response(preview)).mockResolvedValueOnce(response({ task: { ...task, ownership_access: "recovered_read_only" } }));
  render(<HomeMethodInspection target={target} owner={owner} />);
  await screen.findByLabelText("Canonical proposed method");
  fireEvent.change(screen.getByLabelText("Review reason"), { target: { value: "reason" } });
  for (const name of ["Adopt reviewed method", "Reject method", "Rollback to baseline"]) expect(screen.getByRole("button", { name })).toBeDisabled();
});
it("does not substitute another source Task or expose rollback after a changed Goal binding", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response(preview)).mockResolvedValueOnce(response({ task: { ...task, goal_revision: 9 } }));
  render(<HomeMethodInspection target={target} owner={owner} />); await screen.findByText(/Task and Goal do not match/);
  expect(screen.queryByRole("button", { name: "Rollback to baseline" })).not.toBeInTheDocument();
});
it("fails closed when canonical original method is unavailable without reading private Task context", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response({ detail: { code: "owner_revoked" } }, 403));
  render(<HomeMethodInspection target={target} owner={owner} />); await screen.findByText(/exact original method is unavailable/);
  expect(apiFetch).toHaveBeenCalledTimes(1); expect(screen.queryByRole("button", { name: "Rollback to baseline" })).not.toBeInTheDocument();
});
