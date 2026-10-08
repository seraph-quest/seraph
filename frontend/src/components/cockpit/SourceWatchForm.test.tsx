import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SourceWatchForm } from "./SourceWatchForm";

function response(payload: unknown, ok = true) {
  return { ok, json: async () => payload };
}

describe("SourceWatchForm", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockResolvedValue(response([]));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("labels manual-only and paused schedules as disabled", async () => {
    fetchMock.mockResolvedValue(response([
      { id: "manual", goal_id: "goal-1", goal_revision: 1, plan_revision: 1, state: "active", write_mode: "approval_each_run", schedule: { cron: "0 8 * * *", timezone: "UTC", enabled: false, configured_enabled: false } },
      { id: "paused", goal_id: "goal-1", goal_revision: 1, plan_revision: 1, state: "paused", write_mode: "approval_each_run", schedule: { cron: "0 8 * * *", timezone: "UTC", enabled: false, configured_enabled: true } },
    ]));
    render(<SourceWatchForm goal={{ id: "goal-1", title: "Goal", revision: 1 }} />);
    expect(await screen.findByText(/manual only · scheduler disabled/)).toBeInTheDocument();
    expect(screen.getByText(/schedule paused · scheduler disabled/)).toBeInTheDocument();
  });

  it("loads the owner watches and creates a bounded HTTPS watch for the active goal", async () => {
    fetchMock
      .mockResolvedValueOnce(response([]))
      .mockResolvedValueOnce(response({ id: "watch-1", goal_id: "goal-1" }))
      .mockResolvedValueOnce(response([{ id: "watch-1", goal_id: "goal-1", goal_revision: 2, plan_revision: 1, state: "active", write_mode: "approval_each_run", sources: [{ target: "https://example.org/updates.txt" }], baselines: [] }]));

    render(<SourceWatchForm goal={{ id: "goal-1", title: "Ship the guardian slice", revision: 2 }} />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/api/capabilities/source-watches"),
      expect.objectContaining({ credentials: "include" }),
    ));

    fireEvent.change(screen.getByLabelText("Guardian source"), { target: { value: "https://example.org/updates.txt" } });
    fireEvent.click(screen.getByRole("button", { name: "Add watch" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/api/capabilities/source-watches"),
      expect.objectContaining({ method: "POST" }),
    ));
    const [, init] = fetchMock.mock.calls[1] as [RequestInfo, RequestInit];
    expect(JSON.parse(String(init.body))).toMatchObject({
      goal_id: "goal-1",
      expected_goal_revision: 2,
      write_mode: "approval_each_run",
      schedule: { cron: "0 * * * *", timezone: expect.any(String) },
    });
  });

  it("selects a loaded goal and persists a daily cadence in the operator timezone", async () => {
    fetchMock
      .mockResolvedValueOnce(response([]))
      .mockResolvedValueOnce(response({ id: "watch-daily", goal_id: "goal-2" }))
      .mockResolvedValueOnce(response([]));

    render(
      <SourceWatchForm
        goal={null}
        goalOptions={[
          { id: "goal-1", title: "Old goal", revision: 2 },
          { id: "goal-2", title: "Loaded goal", revision: 7 },
        ]}
      />,
    );
    fireEvent.change(screen.getByRole("combobox", { name: "Guardian goal" }), { target: { value: "goal-2" } });
    fireEvent.change(screen.getByRole("combobox", { name: "Guardian cadence" }), { target: { value: "daily" } });
    fireEvent.change(screen.getByRole("combobox", { name: "Guardian daily hour" }), { target: { value: "6" } });
    fireEvent.change(screen.getByLabelText("Guardian source"), { target: { value: "notes/plan.md" } });
    fireEvent.click(screen.getByRole("button", { name: "Add watch" }));

    await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => (init as RequestInit | undefined)?.method === "POST")).toBe(true));
    const postCall = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "POST");
    expect(JSON.parse(String((postCall?.[1] as RequestInit).body))).toMatchObject({
      goal_id: "goal-2",
      expected_goal_revision: 7,
      schedule: { cron: "0 6 * * *", timezone: expect.any(String) },
    });
  });

  it("uses the selected goal's reviewed reference without asking for a raw grant ID", async () => {
    fetchMock
      .mockResolvedValueOnce(response([]))
      .mockResolvedValueOnce(response({ id: "watch-standing", goal_id: "goal-reviewed" }))
      .mockResolvedValueOnce(response([]));

    render(
      <SourceWatchForm
        goal={{
          id: "goal-reviewed",
          title: "Reviewed goal",
          revision: 5,
          proactive_enabled: true,
          admission_budget: {
            reviewed_grant: true,
            grant_id: "budget-review:existing",
            max_outstanding_jobs: 1,
            max_attempts: 1,
            max_runtime_seconds: 300,
            notifications_per_day: 0,
            quiet_hours_start: 22,
            quiet_hours_end: 8,
            timezone: "Europe/Warsaw",
          },
        }}
      />,
    );
    fireEvent.change(screen.getByLabelText("Guardian write mode"), { target: { value: "standing_reviewed" } });
    expect(screen.getByLabelText("Guardian reviewed grant reference")).toHaveValue("budget-review:existing");
    expect(screen.getByLabelText("Guardian reviewed grant reference")).toHaveAttribute("readonly");
    fireEvent.change(screen.getByLabelText("Guardian source"), { target: { value: "notes/plan.md" } });
    fireEvent.click(screen.getByRole("button", { name: "Add watch" }));

    await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => (init as RequestInit | undefined)?.method === "POST")).toBe(true));
    const postCall = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "POST");
    expect(JSON.parse(String((postCall?.[1] as RequestInit).body))).toMatchObject({
      write_mode: "standing_reviewed",
      reviewed_grant_id: "budget-review:existing",
    });
  });

  it("shows blocked operator state when the backend refuses a watch", async () => {
    fetchMock
      .mockResolvedValueOnce(response([]))
      .mockResolvedValueOnce(response({ detail: { code: "goal_budget_missing_reviewed_grant" } }, false));

    render(<SourceWatchForm goal={{ id: "goal-1", title: "Ship the guardian slice", revision: 2 }} />);
    fireEvent.change(screen.getByLabelText("Guardian source"), { target: { value: "notes/plan.md" } });
    fireEvent.click(screen.getByRole("button", { name: "Add watch" }));
    expect(await screen.findByRole("status")).toHaveTextContent("goal_budget_missing_reviewed_grant");
  });

  it("approves and executes the exact packet digest from the cockpit receipt", async () => {
    const watch = {
      id: "watch-approval",
      goal_id: "goal-1",
      goal_revision: 2,
      plan_revision: 3,
      state: "active",
      write_mode: "approval_each_run",
      active_job_id: "source-watch:watch-approval:occurrence-1",
      active_job_fence: 4,
      latest_packet: {
        id: "packet-1",
        run_identity: "source-watch:watch-approval:occurrence-1",
        status: "awaiting_approval",
        approval_id: "approval-1",
        approval_revision: 8,
        packet_digest: "packet-digest-1",
        verification_status: "pending",
      },
    };
    fetchMock
      .mockResolvedValueOnce(response([watch]))
      .mockResolvedValueOnce(response({ status: "approved" }))
      .mockResolvedValueOnce(response({ status: "succeeded" }))
      .mockResolvedValueOnce(response([]));

    render(<SourceWatchForm goal={{ id: "goal-1", title: "Ship the guardian slice", revision: 2 }} />);
    fireEvent.click(await screen.findByRole("button", { name: "Approve and execute packet" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
    expect(fetchMock.mock.calls[1]?.[0]).toContain("/api/approvals/approval-1/approve");
    const [, executeInit] = fetchMock.mock.calls[2] as [RequestInfo, RequestInit];
    expect(JSON.parse(String(executeInit.body))).toEqual({
      expected_packet_digest: "packet-digest-1",
      approval_id: "approval-1",
      expected_approval_revision: 8,
    });
    expect(await screen.findByRole("status")).toHaveTextContent("Packet execution: succeeded");
  });

  it("sends the persisted plan revision and fencing token for cancellation", async () => {
    const watch = {
      id: "watch-running",
      goal_id: "goal-1",
      goal_revision: 2,
      plan_revision: 5,
      state: "active",
      write_mode: "approval_each_run",
      active_job_id: "job-running",
      active_job_fence: 9,
      latest_packet: { id: "packet-running", run_identity: "job-running", status: "executing" },
    };
    fetchMock
      .mockResolvedValueOnce(response([watch]))
      .mockResolvedValueOnce(response({ status: "cancelled" }))
      .mockResolvedValueOnce(response([]));

    render(<SourceWatchForm goal={{ id: "goal-1", title: "Ship the guardian slice", revision: 2 }} />);
    fireEvent.click(await screen.findByRole("button", { name: "Cancel occurrence" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(fetchMock.mock.calls[1]?.[0]).toContain("/api/capabilities/source-watches/watch-running/cancel");
    const [, cancelInit] = fetchMock.mock.calls[1] as [RequestInfo, RequestInit];
    expect(JSON.parse(String(cancelInit.body))).toEqual({
      job_id: "job-running",
      expected_plan_revision: 5,
      expected_fencing_token: 9,
    });
    expect(await screen.findByRole("status")).toHaveTextContent("Cancellation: cancelled");
  });

  it("offers bounded recovery for a blocked durable occurrence", async () => {
    const watch = {
      id: "watch-recovery",
      goal_id: "goal-1",
      goal_revision: 2,
      plan_revision: 6,
      state: "blocked",
      write_mode: "approval_each_run",
      active_job_id: "job-recovery",
      active_job_fence: 11,
      last_error_code: "recovery_readback_required",
      latest_packet: {
        id: "packet-recovery",
        run_identity: "job-recovery",
        status: "blocked",
        failure_code: "recovery_readback_required",
      },
    };
    fetchMock
      .mockResolvedValueOnce(response([watch]))
      .mockResolvedValueOnce(response({ status: "succeeded", recovery: "verified_and_reconciled" }))
      .mockResolvedValueOnce(response([]));

    render(<SourceWatchForm goal={{ id: "goal-1", title: "Ship the guardian slice", revision: 2 }} />);
    expect(await screen.findByText(/recovery required/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Recover occurrence" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(fetchMock.mock.calls[1]?.[0]).toContain("/api/capabilities/source-watches/watch-recovery/recover");
    const [, recoveryInit] = fetchMock.mock.calls[1] as [RequestInfo, RequestInit];
    expect(JSON.parse(String(recoveryInit.body))).toEqual({
      job_id: "job-recovery",
      expected_plan_revision: 6,
    });
    expect(await screen.findByRole("status")).toHaveTextContent("Recovery: succeeded");
  });

  it("shows hashes/readback and submits a reversible criteria correction", async () => {
    const watch = {
      id: "watch-correction",
      goal_id: "goal-1",
      goal_revision: 2,
      plan_revision: 1,
      state: "active",
      write_mode: "approval_each_run",
      baselines: [{ source_key: "primary", state: "ready", sha256: "baseline-hash" }],
      latest_packet: {
        id: "packet-correction",
        status: "succeeded",
        verification_status: "passed",
        memory_status: "no_learning",
        dossier_path: "guardian/source-watches/watch-correction/packets/packet-correction.md",
        dossier_artifact_id: "artifact-dossier",
        dossier_sha256: "dossier-hash",
        task_path: "guardian/source-watches/watch-correction/tasks/packet-correction.md",
        task_artifact_id: "artifact-task",
        task_sha256: "task-hash",
      },
    };
    fetchMock
      .mockResolvedValueOnce(response([watch]))
      .mockResolvedValueOnce(response({ status: "succeeded", strategy_delta_id: "delta-1" }))
      .mockResolvedValueOnce(response([]));

    render(<SourceWatchForm goal={{ id: "goal-1", title: "Ship the guardian slice", revision: 2 }} />);
    expect(await screen.findByText(/sha256 dossier-hash/)).toBeInTheDocument();
    expect(screen.getByText(/readback passed/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Correction include terms for watch-correction"), { target: { value: "release, deadline" } });
    fireEvent.change(screen.getByLabelText("Correction reason for watch-correction"), { target: { value: "Track release deadlines" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply correction" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    const [, correctionInit] = fetchMock.mock.calls[1] as [RequestInfo, RequestInit];
    expect(JSON.parse(String(correctionInit.body))).toMatchObject({
      expected_goal_revision: 2,
      expected_plan_revision: 1,
      include_terms: ["release", "deadline"],
      exclude_terms: [],
      reason: "Track release deadlines",
    });
    expect(await screen.findByRole("status")).toHaveTextContent("Correction: succeeded");
  });

  it("uses the dedicated undo receipt and prior delta identity", async () => {
    const watch = {
      id: "watch-undo",
      goal_id: "goal-1",
      goal_revision: 3,
      plan_revision: 2,
      state: "active",
      write_mode: "approval_each_run",
      latest_packet: {
        id: "packet-undo",
        status: "succeeded",
        verification_status: "passed",
        strategy_delta_id: "delta-prior",
      },
    };
    fetchMock
      .mockResolvedValueOnce(response([watch]))
      .mockResolvedValueOnce(response({ status: "succeeded", correction: "undo" }))
      .mockResolvedValueOnce(response([]));

    render(<SourceWatchForm goal={{ id: "goal-1", title: "Ship the guardian slice", revision: 3 }} />);
    fireEvent.click(await screen.findByRole("button", { name: "Undo last correction" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(fetchMock.mock.calls[1]?.[0]).toContain("/api/capabilities/source-watches/watch-undo/corrections/undo");
    const [, undoInit] = fetchMock.mock.calls[1] as [RequestInfo, RequestInit];
    expect(JSON.parse(String(undoInit.body))).toEqual({
      expected_goal_revision: 3,
      expected_plan_revision: 2,
      prior_strategy_delta_id: "delta-prior",
      reason: "Operator restored the prior source-watch criteria.",
    });
  });
});
