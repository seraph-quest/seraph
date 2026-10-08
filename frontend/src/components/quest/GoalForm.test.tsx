import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GoalForm, GuardianPolicyForm } from "./GoalForm";
import type { GoalInfo } from "../../types";
import { GoalUpdateError, useQuestStore } from "../../stores/questStore";

describe("GoalForm", () => {
  const createGoal = vi.fn<(...args: any[]) => Promise<void>>();
  const updateGoal = vi.fn<(...args: any[]) => Promise<void>>();

  beforeEach(() => {
    createGoal.mockReset().mockResolvedValue(undefined);
    updateGoal.mockReset().mockResolvedValue(undefined);
    useQuestStore.setState({ createGoal, updateGoal });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, status: 200,
      json: async () => ({ goal_id: "goal-1", grant_revision: 0, programmes: [] }) }));
  });

  afterEach(() => { vi.unstubAllGlobals(); });

  it("submits the existing criterion and finite reviewed budget contract", async () => {
    const onClose = vi.fn();
    render(<GoalForm onClose={onClose} />);

    fireEvent.change(screen.getByPlaceholderText("Priority title"), { target: { value: "Ship the report" } });
    fireEvent.click(screen.getByLabelText("Configure success criterion"));
    fireEvent.change(screen.getByLabelText("Criterion ID"), { target: { value: "report-ready" } });
    fireEvent.change(screen.getByLabelText("Criterion description"), { target: { value: "A verified report is readable" } });
    fireEvent.change(screen.getByLabelText("Criterion verifier"), { target: { value: "artifact_readback" } });
    fireEvent.change(screen.getByLabelText("Criterion target"), { target: { value: '{"path":"reports/latest.md"}' } });
    fireEvent.change(screen.getByLabelText("Criterion evidence references"), { target: { value: "artifact:report\nreadback:report" } });
    fireEvent.click(screen.getByLabelText("Enable standing goal observations"));
    fireEvent.click(screen.getByLabelText("I reviewed these limits and the local output scope"));
    expect(screen.getByLabelText("Reviewed grant reference")).toHaveAttribute("readonly");
    expect((screen.getByLabelText("Reviewed grant reference") as HTMLInputElement).value).toMatch(/^budget-review:/);
    fireEvent.change(screen.getByLabelText("Maximum outstanding jobs"), { target: { value: "2" } });
    fireEvent.change(screen.getByLabelText("Maximum attempts"), { target: { value: "3" } });
    fireEvent.change(screen.getByLabelText("Maximum runtime seconds"), { target: { value: "600" } });
    fireEvent.change(screen.getByLabelText("Notifications per day"), { target: { value: "4" } });
    fireEvent.change(screen.getByLabelText("Operator timezone"), { target: { value: "Europe/Warsaw" } });
    fireEvent.click(screen.getByRole("button", { name: "Create" }));

    await waitFor(() => expect(createGoal).toHaveBeenCalledTimes(1));
    expect(createGoal.mock.calls[0][0]).toMatchObject({
      title: "Ship the report",
      proactive_enabled: true,
      success_criterion: {
        criterion_id: "report-ready",
        description: "A verified report is readable",
        verifier_kind: "artifact_readback",
        target: { path: "reports/latest.md" },
        evidence_refs: ["artifact:report", "readback:report"],
      },
      admission_budget: {
        reviewed_grant: true,
        grant_id: expect.stringMatching(/^budget-review:/),
        max_outstanding_jobs: 2,
        max_attempts: 3,
        max_runtime_seconds: 600,
        notifications_per_day: 4,
        timezone: "Europe/Warsaw",
        quiet_hours_start: 22,
        quiet_hours_end: 8,
      },
    });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("keeps the edited draft after a stale revision response", async () => {
    updateGoal.mockRejectedValueOnce(new GoalUpdateError("goal-1", "stale", { code: "stale_goal_revision" }));
    render(
      <GoalForm
        goal={{
          id: "goal-1",
          parent_id: null,
          path: "goal-1",
          level: "weekly",
          title: "Original title",
          description: "Original description",
          status: "active",
          domain: "productivity",
          start_date: null,
          due_date: null,
          sort_order: 0,
          revision: 4,
        }}
        onClose={vi.fn()}
      />,
    );
    fireEvent.change(screen.getByPlaceholderText("Priority title"), { target: { value: "Draft kept locally" } });
    fireEvent.click(screen.getByRole("button", { name: "Update" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("draft is still here");
    expect(screen.getByPlaceholderText("Priority title")).toHaveValue("Draft kept locally");
    expect(updateGoal).toHaveBeenCalledWith("goal-1", expect.objectContaining({ expected_revision: 4 }));
  });

  it("warns ordinary Goal edits invalidate assessment consent and keeps the policy out of its PATCH", async () => {
    const goal = policyGoal();
    goal.guardian_policy = { schema_version: "seraph.guardian.policy.v1", assessment_enabled: true, auto_stage_plan: false,
      confirmed_at: new Date().toISOString(), review_due_at: goal.admission_budget!.period_expires_at!, grant_id: "grant-1",
      original_root_id: "root-1", goal_revision: 4, source_watch_ids: [watchId], max_assessments_per_utc_day: 1,
      max_plan_proposals_per_utc_day: 0, max_notification_per_utc_day: 0, minimum_gap_seconds: 1800 };
    render(<GoalForm goal={goal} onClose={vi.fn()} />);
    expect(screen.getByText(/Editing this priority invalidates its assessment policy/)).toBeInTheDocument();
    fireEvent.change(screen.getByPlaceholderText("Priority title"), { target: { value: "Changed priority" } });
    fireEvent.click(screen.getByRole("button", { name: "Update" }));
    await waitFor(() => expect(updateGoal).toHaveBeenCalledTimes(1));
    expect(updateGoal.mock.calls[0][1]).toMatchObject({ title: "Changed priority", expected_revision: 4 });
    expect(updateGoal.mock.calls[0][1]).not.toHaveProperty("guardian_policy");
    expect(updateGoal.mock.calls[0][1]).not.toHaveProperty("guardian_policy_revision");
  });
});

const watchId = "11111111-1111-4111-8111-111111111111";
function policyGoal(): GoalInfo {
  return { id: "goal-1", parent_id: null, path: "goal-1", level: "weekly", title: "Fresh priority",
    description: null, status: "active", domain: "growth", start_date: null, due_date: null, sort_order: 0,
    revision: 4, owner_session_id: "root-1", guardian_policy_revision: 0, guardian_policy: null,
    guardian_assessment_state: "disabled", proactive_enabled: true,
    admission_budget: { reviewed_grant: true, grant_id: "grant-1", max_outstanding_jobs: 1,
      max_attempts: 1, max_runtime_seconds: 300, notifications_per_day: 0, timezone: "UTC",
      period_expires_at: new Date(Date.now() + 3 * 86400000).toISOString() } };
}
function publicWatch(overrides = {}) {
  return { id: watchId, goal_id: "goal-1", goal_revision: 4, plan_revision: 2, state: "active",
    sources: [{ source_key: "release-notes", kind: "public_https_text", label: "Release notes" }], ...overrides };
}
function savedPolicyGoal(): GoalInfo {
  const goal = policyGoal();
  return { ...goal, guardian_policy_revision: 2, guardian_assessment_state: "enabled", guardian_policy: {
    schema_version: "seraph.guardian.policy.v1", assessment_enabled: true, auto_stage_plan: true,
    confirmed_at: new Date().toISOString(), review_due_at: goal.admission_budget!.period_expires_at!,
    grant_id: "grant-1", original_root_id: "root-1", goal_revision: 4, source_watch_ids: [watchId],
    max_assessments_per_utc_day: 2, max_plan_proposals_per_utc_day: 1, max_notification_per_utc_day: 0,
    minimum_gap_seconds: 1800,
  } };
}
function policyResponse(payload: unknown, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => payload };
}

describe("GuardianPolicyForm", () => {
  const fetchMock = vi.fn();
  beforeEach(() => { fetchMock.mockReset(); vi.stubGlobal("fetch", fetchMock); });
  afterEach(() => { vi.unstubAllGlobals(); });

  it("reopens saved canonical policy controls with separate acknowledgments still unchecked", async () => {
    const goal = savedPolicyGoal();
    fetchMock.mockResolvedValue(policyResponse([publicWatch()]));
    const first = render(<GuardianPolicyForm goal={goal} />);
    await screen.findByLabelText(`Assessment watch ${watchId}`);
    first.unmount();
    render(<GuardianPolicyForm goal={{ ...goal }} />);
    await screen.findByLabelText(`Assessment watch ${watchId}`);
    expect(screen.getByText(/Policy revision 2/)).toBeInTheDocument();
    expect(screen.getByLabelText("Enable bounded public opportunity assessments")).toBeChecked();
    expect(screen.getByLabelText(`Assessment watch ${watchId}`)).toBeChecked();
    expect(screen.getByLabelText("Enable silent non-executable Triage plan staging")).toBeChecked();
    expect(screen.getByLabelText("Advisory proposals per UTC day")).toHaveValue(1);
    expect(screen.getByLabelText("I separately acknowledge advisory staging never accepts or executes a plan")).not.toBeChecked();
    expect(screen.getByLabelText("I separately acknowledge optional notifications and quiet-hour limits")).not.toBeChecked();
    expect(fetchMock.mock.calls.every(([, init]) => !init?.method || init.method === "GET")).toBe(true);
  });

  it("retains a dirty draft on explicit refresh and keeps the current header through older tree props", async () => {
    const goal = policyGoal();
    fetchMock.mockResolvedValueOnce(policyResponse([publicWatch()]));
    const view = render(<GuardianPolicyForm goal={goal} />);
    await screen.findByLabelText(`Assessment watch ${watchId}`);
    fireEvent.click(screen.getByLabelText("Enable bounded public opportunity assessments"));
    fireEvent.change(screen.getByLabelText("Assessments per UTC day"), { target: { value: "4" } });
    const current = savedPolicyGoal();
    fetchMock.mockResolvedValueOnce(policyResponse([current]));
    fetchMock.mockResolvedValueOnce(policyResponse([publicWatch()]));
    fireEvent.click(screen.getByRole("button", { name: "Refresh policy metadata" }));
    await screen.findByText(/Current revisions refreshed/);
    view.rerender(<GuardianPolicyForm goal={{ ...goal }} />);
    expect(screen.getByText(/Policy revision 2/)).toBeInTheDocument();
    expect(screen.getByText(/Current confirmed policy: enabled/)).toBeInTheDocument();
    expect(screen.getByLabelText("Assessments per UTC day")).toHaveValue(4);
    expect(screen.getByLabelText(`Assessment watch ${watchId}`)).not.toBeChecked();
    expect(screen.getByLabelText("Enable silent non-executable Triage plan staging")).not.toBeChecked();
    expect(screen.getByLabelText("I separately acknowledge advisory staging never accepts or executes a plan")).not.toBeChecked();
    expect(fetchMock.mock.calls.every(([, init]) => !init?.method || init.method === "GET")).toBe(true);
    fetchMock.mockImplementationOnce(async (_url, init) => {
      const body = JSON.parse(init.body);
      return policyResponse({ goal_revision: 4, guardian_policy_revision: 3, guardian_policy: body.policy, assessment_state: "enabled" });
    });
    fireEvent.click(screen.getByLabelText(`Assessment watch ${watchId}`));
    fireEvent.click(screen.getByRole("button", { name: "Save assessment policy" }));
    await screen.findByText(/Assessment policy saved/);
    const saves = fetchMock.mock.calls.filter(([, init]) => init?.method === "PUT");
    expect(saves).toHaveLength(1);
    expect(JSON.parse(saves[0][1].body)).toMatchObject({ expected_goal_revision: 4, expected_policy_revision: 2,
      acknowledge_auto_stage_plan: false, acknowledge_notifications: false,
      policy: { max_assessments_per_utc_day: 4, auto_stage_plan: false } });
    view.rerender(<GuardianPolicyForm goal={{ ...goal }} />);
    expect(screen.getByText(/Policy revision 3/)).toBeInTheDocument();
    view.rerender(<GuardianPolicyForm goal={{ ...goal, ownership_access: "recovered_read_only", proactive_enabled: false,
      admission_budget: null, guardian_assessment_state: "goal_review_required" }} />);
    expect(screen.getByText(/Goal review required/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save assessment policy" })).toBeDisabled();
  });

  it("keeps legacy NULL policies off and saves deliberate policy-only consent with exact CAS, UUID and default limits", async () => {
    const goal = policyGoal();
    fetchMock.mockResolvedValueOnce(policyResponse([publicWatch()]));
    fetchMock.mockImplementationOnce(async (_url, init) => {
      const body = JSON.parse(init.body);
      return policyResponse({ goal_revision: 4, guardian_policy_revision: 1, guardian_policy: body.policy, assessment_state: "enabled" });
    });
    render(<GuardianPolicyForm goal={goal} />);
    expect(screen.getByLabelText("Enable bounded public opportunity assessments")).not.toBeChecked();
    expect(screen.getByLabelText("Enable silent non-executable Triage plan staging")).not.toBeChecked();
    expect(screen.getByLabelText("I separately acknowledge advisory staging never accepts or executes a plan")).not.toBeChecked();
    expect(screen.getByLabelText("I separately acknowledge optional notifications and quiet-hour limits")).not.toBeChecked();
    await screen.findByLabelText(`Assessment watch ${watchId}`);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByLabelText("Enable bounded public opportunity assessments"));
    fireEvent.click(screen.getByLabelText(`Assessment watch ${watchId}`));
    fireEvent.click(screen.getByRole("button", { name: "Save assessment policy" }));
    await screen.findByText(/Assessment policy saved/);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    const [url, request] = fetchMock.mock.calls[1];
    expect(url).toContain("/api/goals/goal-1/guardian-policy");
    expect(request).toMatchObject({ method: "PUT", credentials: "include" });
    const body = JSON.parse(request.body);
    expect(body).toMatchObject({ expected_goal_revision: 4, expected_policy_revision: 0,
      acknowledge_auto_stage_plan: false, acknowledge_notifications: false,
      idempotency_key: expect.stringMatching(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/),
      policy: { schema_version: "seraph.guardian.policy.v1", assessment_enabled: true, auto_stage_plan: false,
        grant_id: "grant-1", original_root_id: "root-1", goal_revision: 4, source_watch_ids: [watchId],
        max_assessments_per_utc_day: 1, max_plan_proposals_per_utc_day: 0, max_notification_per_utc_day: 0, minimum_gap_seconds: 1800 } });
    expect(Date.parse(body.policy.review_due_at)).toBeLessThanOrEqual(Date.parse(goal.admission_budget!.period_expires_at!));
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === "POST" || init?.method === "PATCH")).toBe(false);
  });

  it("excludes private, inactive, other Goal and stale watches without auto-rebind", async () => {
    fetchMock.mockResolvedValueOnce(policyResponse([
      publicWatch({ id: "private", sources: [{ source_key: "mail", kind: "mail" }] }),
      publicWatch({ id: "stale", goal_revision: 3 }), publicWatch({ id: "paused", state: "paused" }),
      publicWatch({ id: "other", goal_id: "other-goal" }),
    ]));
    render(<GuardianPolicyForm goal={policyGoal()} />);
    await screen.findByText(/No active public watch/);
    fireEvent.click(screen.getByLabelText("Enable bounded public opportunity assessments"));
    fireEvent.click(screen.getByRole("button", { name: "Save assessment policy" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("one to three active public watches");
    expect(screen.queryByLabelText("Assessment watch stale")).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("requires independent unchecked acknowledgments for advisory staging and notifications", async () => {
    fetchMock.mockResolvedValueOnce(policyResponse([publicWatch()]));
    render(<GuardianPolicyForm goal={policyGoal()} />);
    await screen.findByLabelText(`Assessment watch ${watchId}`);
    expect(screen.getByText("Separately consented staging may silently create a non-executable Triage plan. Saving this permission never accepts or executes a plan.")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Enable bounded public opportunity assessments"));
    fireEvent.click(screen.getByLabelText(`Assessment watch ${watchId}`));
    fireEvent.click(screen.getByLabelText("Enable silent non-executable Triage plan staging"));
    fireEvent.change(screen.getByLabelText("Advisory proposals per UTC day"), { target: { value: "1" } });
    fireEvent.change(screen.getByLabelText("Opportunity notifications per UTC day"), { target: { value: "1" } });
    fireEvent.click(screen.getByRole("button", { name: "Save assessment policy" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Separately acknowledge advisory staging");
    fireEvent.click(screen.getByLabelText("I separately acknowledge advisory staging never accepts or executes a plan"));
    fireEvent.click(screen.getByRole("button", { name: "Save assessment policy" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Separately acknowledge optional notifications");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    fetchMock.mockImplementationOnce(async (_url, init) => {
      const body = JSON.parse(init.body);
      return policyResponse({ goal_revision: 4, guardian_policy_revision: 1, guardian_policy: body.policy, assessment_state: "enabled" });
    });
    fireEvent.click(screen.getByLabelText("I separately acknowledge optional notifications and quiet-hour limits"));
    fireEvent.click(screen.getByRole("button", { name: "Save assessment policy" }));
    await screen.findByText(/Assessment policy saved/);
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toMatchObject({ acknowledge_auto_stage_plan: true, acknowledge_notifications: true,
      policy: { auto_stage_plan: true, max_plan_proposals_per_utc_day: 1, max_notification_per_utc_day: 1 } });
    expect(fetchMock.mock.calls[1][1].method).toBe("PUT");
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("limits selection to three watches and does not dispatch or renew them", async () => {
    const ids = [1, 2, 3, 4].map((value) => `00000000-0000-4000-8000-00000000000${value}`);
    fetchMock.mockResolvedValueOnce(policyResponse(ids.map((id) => publicWatch({ id }))));
    render(<GuardianPolicyForm goal={policyGoal()} />);
    await screen.findByLabelText(`Assessment watch ${ids[0]}`);
    ids.slice(0, 3).forEach((id) => fireEvent.click(screen.getByLabelText(`Assessment watch ${id}`)));
    expect(screen.getByLabelText(`Assessment watch ${ids[3]}`)).toBeDisabled();
    fireEvent.click(screen.getByLabelText(`Assessment watch ${ids[0]}`));
    expect(screen.getByLabelText(`Assessment watch ${ids[3]}`)).toBeEnabled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("retains the draft instead of adopting an incomplete policy save receipt", async () => {
    fetchMock.mockResolvedValueOnce(policyResponse([publicWatch()]));
    fetchMock.mockResolvedValueOnce(policyResponse({ goal_revision: 4, guardian_policy_revision: 1,
      guardian_policy: {}, assessment_state: "enabled" }));
    render(<GuardianPolicyForm goal={policyGoal()} />);
    await screen.findByLabelText(`Assessment watch ${watchId}`);
    fireEvent.click(screen.getByLabelText("Enable bounded public opportunity assessments"));
    fireEvent.click(screen.getByLabelText(`Assessment watch ${watchId}`));
    fireEvent.click(screen.getByRole("button", { name: "Save assessment policy" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("incomplete metadata");
    expect(screen.getByLabelText(`Assessment watch ${watchId}`)).toBeChecked();
    expect(screen.getByRole("button", { name: "Save assessment policy" })).toBeDisabled();
    expect(screen.queryByText(/Assessment policy saved/)).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("retains a stale draft after 409 and requires a successful explicit metadata refresh without resaving", async () => {
    const goal = policyGoal();
    fetchMock.mockResolvedValueOnce(policyResponse([publicWatch()]));
    fetchMock.mockResolvedValueOnce(policyResponse({ detail: { code: "guardian_policy_revision_stale" } }, 409));
    render(<GuardianPolicyForm goal={goal} />);
    await screen.findByLabelText(`Assessment watch ${watchId}`);
    fireEvent.click(screen.getByLabelText("Enable bounded public opportunity assessments"));
    fireEvent.click(screen.getByLabelText(`Assessment watch ${watchId}`));
    fireEvent.click(screen.getByRole("button", { name: "Save assessment policy" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Your draft is retained");
    expect(screen.getByRole("button", { name: "Save assessment policy" })).toBeDisabled();
    expect(screen.getByLabelText(`Assessment watch ${watchId}`)).toBeChecked();
    fetchMock.mockResolvedValueOnce(policyResponse([{ ...goal, revision: 5, guardian_policy_revision: 1, guardian_assessment_state: "goal_review_required" }]));
    fetchMock.mockResolvedValueOnce(policyResponse([publicWatch()]));
    fireEvent.click(screen.getByRole("button", { name: "Refresh policy metadata" }));
    await screen.findByText(/Current revisions refreshed/);
    expect(screen.getByText(/Retained watch selections are stale/)).toBeInTheDocument();
    expect(screen.getByText(/Goal review required/)).toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "PUT")).toHaveLength(1);
  });

  it("keeps last-known watch selections and controls through a partial metadata failure", async () => {
    const goal = policyGoal();
    fetchMock.mockResolvedValueOnce(policyResponse([publicWatch()]));
    render(<GuardianPolicyForm goal={goal} />);
    await screen.findByLabelText(`Assessment watch ${watchId}`);
    fireEvent.click(screen.getByLabelText(`Assessment watch ${watchId}`));
    fetchMock.mockResolvedValueOnce(policyResponse([goal]));
    fetchMock.mockRejectedValueOnce(new Error("watch metadata offline"));
    fireEvent.click(screen.getByRole("button", { name: "Refresh policy metadata" }));
    await screen.findByText(/Metadata refresh is incomplete/);
    expect(screen.getByLabelText(`Assessment watch ${watchId}`)).toBeChecked();
    expect(screen.getByRole("button", { name: "Save assessment policy" })).toBeEnabled();
    expect(screen.getByLabelText("Enable bounded public opportunity assessments")).toBeEnabled();
    fetchMock.mockImplementationOnce(async (_url, init) => {
      const body = JSON.parse(init.body);
      return policyResponse({ goal_revision: 4, guardian_policy_revision: 1, guardian_policy: body.policy, assessment_state: "enabled" });
    });
    fireEvent.click(screen.getByLabelText("Enable bounded public opportunity assessments"));
    fireEvent.click(screen.getByRole("button", { name: "Save assessment policy" }));
    await screen.findByText(/Assessment policy saved/);
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "PUT")).toHaveLength(1);
  });

  it("rejects review dates beyond the finite Goal budget and fractional daily limits before HTTP mutation", async () => {
    fetchMock.mockResolvedValueOnce(policyResponse([publicWatch()]));
    render(<GuardianPolicyForm goal={policyGoal()} />);
    await screen.findByLabelText(`Assessment watch ${watchId}`);
    fireEvent.click(screen.getByLabelText("Enable bounded public opportunity assessments"));
    fireEvent.click(screen.getByLabelText(`Assessment watch ${watchId}`));
    const validDue = (screen.getByLabelText("Assessment review due UTC") as HTMLInputElement).value;
    fireEvent.change(screen.getByLabelText("Assessment review due UTC"), { target: { value: new Date(Date.now() + 8 * 86400000).toISOString().slice(0, 16) } });
    fireEvent.click(screen.getByRole("button", { name: "Save assessment policy" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("within seven days");
    fireEvent.change(screen.getByLabelText("Assessment review due UTC"), { target: { value: validDue } });
    fireEvent.change(screen.getByLabelText("Assessments per UTC day"), { target: { value: "1.5" } });
    fireEvent.click(screen.getByRole("button", { name: "Save assessment policy" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("whole-number daily limits");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
