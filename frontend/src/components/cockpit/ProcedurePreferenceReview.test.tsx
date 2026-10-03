import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { procedurePreferences, type ProcedurePreferenceReview as Review, type ProcedurePreferenceScope } from "../../lib/procedurePreferences";
import { ProcedurePreferenceReview } from "./ProcedurePreferenceReview";

const scope: ProcedurePreferenceScope = { routineId: "routine", version: 1, routineRevision: 3, goalId: "goal", goalRevision: 1 };
const manual = "Only matching manual invocations are counted. Governed scheduled invocations are excluded.";
const quality = "This deterministic preference is not a measured quality improvement.";
const outcomes = [1, 2].map((number) => ({ task_id: `manual-${number}`, task_revision: 5, status: "done", attempt_id: `attempt-${number}`,
  attempt_fence: 1, feedback: "helpful" as const, feedback_event_id: number, verified: true }));
const review: Review = { proposal_id: "proposal", owner_principal_id: "owner", owner_session_id: "root", revision: 1,
  status: "proposed", preview_text: "Suggest reviewed version 1, selection only.", preview_text_digest: "a".repeat(64),
  bundle_digest: "b".repeat(64), included_count: 2, outcomes, manual_disclosure: manual, quality_disclosure: quality };
const props = { ownerPrincipalId: "owner", ownerSessionId: "root", scope };
const acknowledgment = "I understand this changes future suggestions only and grants no execution authority.";

beforeEach(() => {
  sessionStorage.clear(); vi.restoreAllMocks();
  vi.spyOn(procedurePreferences, "outcomes").mockResolvedValue({ included_count: 2, outcomes, manual_disclosure: manual, quality_disclosure: quality });
  vi.spyOn(procedurePreferences, "selection").mockResolvedValue({ status: "none", reason_code: "no_adopted_procedure_preference" });
  vi.spyOn(procedurePreferences, "recommend").mockResolvedValue({ job_id: "job", job_status: "succeeded", status: "proposed", reason_code: "reviewed_outcomes_support_preference", proposal_id: "proposal", included_count: 2, outcomes, manual_disclosure: manual, quality_disclosure: quality });
  vi.spyOn(procedurePreferences, "inspectJob").mockResolvedValue({ job_id: "job", job_status: "succeeded", status: "proposed", reason_code: "reviewed_outcomes_support_preference", proposal_id: "proposal", included_count: 2, outcomes, manual_disclosure: manual, quality_disclosure: quality });
  vi.spyOn(procedurePreferences, "review").mockResolvedValue(review);
  vi.spyOn(procedurePreferences, "act").mockResolvedValue({ ...review, status: "accepted", revision: 2 });
  vi.spyOn(procedurePreferences, "feedback").mockResolvedValue({ event_id: 3 });
  vi.spyOn(procedurePreferences, "findJob").mockResolvedValue({ found: false, job: null });
  vi.spyOn(procedurePreferences, "cancel").mockResolvedValue({ job_id: "job", job_status: "cancelled", status: "cancelled",
    reason_code: "cancelled", proposal_id: null, included_count: 0, outcomes: [], manual_disclosure: manual, quality_disclosure: quality });
});

async function preview() {
  await screen.findByText("Included manual invocations: 2");
  await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Preview outcome recommendation" })); });
  await screen.findByText(review.preview_text);
}

describe("explicit procedure preference review", () => {
  it("discovers a retained active native request using GET and cancels only its displayed revision and fence", async () => {
    const body = { version: 1, expected_routine_revision: 3, goal_id: "goal", expected_goal_revision: 1,
      request_uuid: "5c4ad444-aeeb-4fab-802d-b7282bb6b7dd" };
    const key = `seraph.procedure-preference:${JSON.stringify(["owner", "root", "routine", 1, 3, "goal", 1])}`;
    sessionStorage.setItem(key, JSON.stringify({ pending: { kind: "recommend", body }, jobId: null, proposalId: null }));
    const running = { job_id: "job", job_status: "running", status: "running", job_revision: 3, fencing_token: 1,
      reason_code: "running", proposal_id: null, included_count: 0, outcomes: [], manual_disclosure: manual, quality_disclosure: quality };
    vi.mocked(procedurePreferences.findJob).mockResolvedValue({ found: true, job: running });
    vi.mocked(procedurePreferences.inspectJob).mockResolvedValue({ ...running, job_status: "cancelled", status: "cancelled", reason_code: "cancelled" });
    render(<ProcedurePreferenceReview {...props} />);
    const cancel = await screen.findByRole("button", { name: "Cancel owned recommendation" });
    expect(procedurePreferences.findJob).toHaveBeenCalledWith(scope, body);
    expect(procedurePreferences.recommend).not.toHaveBeenCalled(); expect(procedurePreferences.cancel).not.toHaveBeenCalled();
    await act(async () => { fireEvent.click(cancel); });
    expect(procedurePreferences.cancel).toHaveBeenCalledWith(scope, "job", expect.objectContaining({
      expected_job_revision: 3, expected_fencing_token: 1, goal_id: "goal", expected_goal_revision: 1,
    }));
    await screen.findByText(/cancelled · cancelled/);
    expect(screen.queryByRole("button", { name: "Cancel owned recommendation" })).not.toBeInTheDocument();
    expect(procedurePreferences.act).not.toHaveBeenCalled();
  });
  it("discloses the manual-only population and unmeasured quality before adoption and on an adopted suggestion", async () => {
    const selected = vi.fn();
    render(<ProcedurePreferenceReview {...props} onSelectVersion={selected} />);
    await preview();
    expect(screen.getByText(manual)).toBeVisible(); expect(screen.getByText(quality)).toBeVisible();
    expect(screen.getByRole("button", { name: "Adopt reviewed preference" })).toBeDisabled();
    expect(procedurePreferences.act).not.toHaveBeenCalled();
    vi.mocked(procedurePreferences.selection).mockResolvedValue({ status: "suggested", reason_code: "adopted_reviewed_procedure_preference", suggested_version: 1, suggested_version_id: "version", review: { ...review, status: "accepted", revision: 2 } });
    fireEvent.click(screen.getByRole("checkbox", { name: acknowledgment }));
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Adopt reviewed preference" })); });
    const suggestion = await screen.findByLabelText("Adopted Library suggestion");
    expect(suggestion).toHaveTextContent(manual); expect(suggestion).toHaveTextContent(quality);
    expect(suggestion).toHaveTextContent("Included manual invocations: 2"); expect(suggestion).toHaveTextContent("manual-1");
    expect(procedurePreferences.act).toHaveBeenCalledWith("proposal", expect.objectContaining({ expected_bundle_digest: review.bundle_digest, acknowledged_selection_only: true }));
    fireEvent.click(screen.getByRole("button", { name: "Select suggested reviewed version" })); expect(selected).toHaveBeenCalledWith(1);
    expect(procedurePreferences.recommend).toHaveBeenCalledTimes(1);
  });

  it("uses GET-only remount recovery and resets acknowledgment when Root changes", async () => {
    const view = render(<ProcedurePreferenceReview {...props} />); await preview();
    fireEvent.click(screen.getByRole("checkbox", { name: acknowledgment }));
    expect(screen.getByRole("button", { name: "Adopt reviewed preference" })).toBeEnabled();
    view.unmount(); const current = render(<ProcedurePreferenceReview {...props} />);
    await screen.findByText(review.preview_text);
    expect(screen.getByRole("checkbox", { name: acknowledgment })).not.toBeChecked();
    expect(procedurePreferences.recommend).toHaveBeenCalledTimes(1); expect(procedurePreferences.act).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("checkbox", { name: acknowledgment }));
    expect(screen.getByRole("button", { name: "Adopt reviewed preference" })).toBeEnabled();
    current.rerender(<ProcedurePreferenceReview {...props} ownerSessionId="different-root" />);
    expect(screen.queryByText(review.preview_text)).not.toBeInTheDocument();
    await screen.findByText("Included manual invocations: 2");
    expect(procedurePreferences.act).not.toHaveBeenCalled();
  });

  it("retains the exact ambiguous action across reload and sends it only on an explicit retry", async () => {
    vi.mocked(procedurePreferences.act).mockRejectedValueOnce(new Error("response timeout"));
    const view = render(<ProcedurePreferenceReview {...props} />); await preview();
    fireEvent.click(screen.getByRole("checkbox", { name: acknowledgment }));
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Adopt reviewed preference" })); });
    await screen.findByRole("button", { name: "Retry exact review request" });
    const exact = vi.mocked(procedurePreferences.act).mock.calls[0]; view.unmount();
    render(<ProcedurePreferenceReview {...props} />); await screen.findByRole("button", { name: "Retry exact review request" });
    expect(procedurePreferences.act).toHaveBeenCalledTimes(1);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Retry exact review request" })); });
    await waitFor(() => expect(procedurePreferences.act).toHaveBeenCalledTimes(2));
    expect(vi.mocked(procedurePreferences.act).mock.calls[1]).toEqual(exact);
    await waitFor(() => expect(screen.queryByRole("button", { name: "Retry exact review request" })).not.toBeInTheDocument());
  });

  it("requires an explicit reason before appending corrected feedback", async () => {
    render(<ProcedurePreferenceReview {...props} />); await screen.findByText("Included manual invocations: 2");
    expect(screen.getAllByRole("button", { name: "Harmful" })[0]).toBeDisabled();
    fireEvent.change(screen.getByRole("textbox", { name: "Procedure feedback reason" }), { target: { value: "The check missed an operator requirement" } });
    await act(async () => { fireEvent.click(screen.getAllByRole("button", { name: "Harmful" })[0]); });
    expect(procedurePreferences.feedback).toHaveBeenCalledWith(scope, outcomes[0], "harmful", "The check missed an operator requirement", expect.any(String));
    expect(procedurePreferences.act).not.toHaveBeenCalled();
  });
});
