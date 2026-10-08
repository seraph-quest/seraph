import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { GoalForm } from "./GoalForm";
import { GoalProgrammePanel } from "./GoalProgrammePanel";
import { useQuestStore } from "../../stores/questStore";
import type { GoalInfo } from "../../types";
import type { GoalProgramme } from "./goalProgrammeApi";

const goal: GoalInfo = { id: "goal-1", parent_id: null, path: "goal-1", level: "weekly", title: "Private title",
  description: "Private medical notes", status: "active", domain: "growth", start_date: null, due_date: null, sort_order: 0, revision: 4 };
const programme: GoalProgramme = { schema_version: "GoalProgramme.v1", id: "programme-1", goal_id: goal.id,
  goal_revision: 4, grant_revision: 3, public_brief: "Public release updates", brief_digest: "a".repeat(64),
  expires_at: "2026-10-15T12:00:00Z", confirmed_at: "2026-10-08T12:00:00Z", capability_ids: ["goal.public-discovery.v1"],
  budget: { max_inference_microusd: 1000, max_outstanding_runs: 1 }, cadence: "daily", notification_limits: { per_day: 0 },
  state: "active", reason_code: null, recovery: null, artifact_prefix: "goal-programmes/goal-1/3/",
  issuer_root_id: "root-old", route_epoch: 2, route_digest: "b".repeat(64), review_digest: "c".repeat(64) };
const response = (payload: unknown, status = 200) => ({ ok: status < 400, status, json: async () => payload });
const fetchMock = vi.fn();
let stored: GoalProgramme[];
let previewed: GoalProgramme;
function renderEditor() { return render(<GoalForm goal={goal} onClose={vi.fn()} />); }
async function preview() {
  await waitFor(() => expect(screen.getByRole("button", { name: "Preview finite public programme" })).toBeEnabled());
  fireEvent.change(screen.getByLabelText("Public brief"), { target: { value: programme.public_brief } });
  fireEvent.change(screen.getByLabelText("Inference ceiling (micro USD)"), { target: { value: "1000" } });
  fireEvent.click(screen.getByRole("button", { name: "Preview finite public programme" }));
  await screen.findByLabelText("Exact programme review");
}
function acknowledge() {
  fireEvent.click(screen.getByLabelText("I reviewed this exact public brief and public-web search/read"));
  fireEvent.click(screen.getByLabelText("I reviewed this exact local artifact prefix"));
  fireEvent.click(screen.getByLabelText("I reviewed this finite inference ceiling and expiry"));
}
describe("Goal programme cockpit journey", () => {
  beforeEach(() => {
    stored = []; previewed = programme;
    useQuestStore.setState({ updateGoal: vi.fn().mockResolvedValue(undefined), createGoal: vi.fn().mockResolvedValue(undefined) });
    fetchMock.mockReset().mockImplementation(async (url: string, init: RequestInit) => {
      if (url.endsWith("/preview")) return response({ programme: previewed, review_digest: previewed.review_digest, public_only: true, preview_only: true });
      if (url.endsWith("/accept")) { stored = [previewed]; return response(previewed); }
      if (url.endsWith("/pause") || url.endsWith("/revoke")) {
        stored = stored.map((p) => ({ ...p, state: url.endsWith("/pause") ? "paused" : "revoked" })); return response(stored[0]);
      }
      if (init.method === "GET") return response({ goal_id: goal.id, grant_revision: 2, programmes: stored });
      throw new Error("Unexpected contact");
    });
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => { vi.unstubAllGlobals(); });
  it("shows retained Unknown independently of capacity and never reads a private brief automatically", async () => {
    const base = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (url, init) => url.endsWith("/discovery") ? response({ goal_id: goal.id,
      current_day_only: true, no_learning: true, runs: [{ job_id: `goal-discovery:${"a".repeat(32)}`,
        programme_id: "b".repeat(32), goal_revision: 4, grant_revision: 3, occurrence_day: "2026-10-08",
        status: "running", deadline_at: "2026-10-08T12:05:00Z", external_effect_state: "unknown", outstanding_held: true,
        accounting_liability: true, outcome: null, no_learning: true, recovery: "Original contact unknown; no replay." }] }) : base(url, init));
    renderEditor();
    fireEvent.click(screen.getByRole("button", { name: "Inspect discovery runs" }));
    await screen.findByText(/External effect: unknown/);
    expect(screen.getByText("Original accounting liability is unresolved.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Read selected discovery brief/ })).toBeDisabled();
    expect(fetchMock.mock.calls.some(([url]) => url.endsWith("/brief"))).toBe(false);
  });
  it("reads only an explicitly selected completed cited local brief with escaped text", async () => {
    const base = fetchMock.getMockImplementation()!;
    const job = `goal-discovery:${"a".repeat(32)}`;
    fetchMock.mockImplementation(async (url, init) => {
      if (url.endsWith("/discovery")) return response({ goal_id: goal.id, current_day_only: true, no_learning: true,
        runs: [{ job_id: job, programme_id: "b".repeat(32), goal_revision: 4, grant_revision: 3,
          occurrence_day: "2026-10-08", status: "succeeded", deadline_at: "2026-10-08T12:05:00Z",
          external_effect_state: "settled", outstanding_held: false, accounting_liability: false,
          outcome: { state: "findings", coverage: "partial", freshness: "current", no_learning: true }, no_learning: true, recovery: null }] });
      if (url.endsWith("/brief")) return response({ job_id: job, programme_id: "b".repeat(32), physical_readback: true,
        no_learning: true, brief: { findings: ["<script>private readback text</script>"], citations: [], uncertainties: [],
          prepared_artifact_refs: [], proposed_next_steps: [], coverage: { status: "partial", no_learning: true } } });
      return base(url, init);
    });
    renderEditor();
    fireEvent.click(screen.getByRole("button", { name: "Inspect discovery runs" }));
    const selected = await screen.findByRole("button", { name: /Read selected discovery brief/ });
    expect(fetchMock.mock.calls.some(([url]) => url.endsWith("/brief"))).toBe(false);
    fireEvent.click(selected);
    const readback = await screen.findByLabelText("Discovery brief physical readback");
    expect(readback).toHaveTextContent("<script>private readback text</script>");
    expect(readback.querySelector("script")).toBeNull();
    expect(fetchMock.mock.calls.filter(([url]) => url.endsWith("/brief"))).toHaveLength(1);
  });
  it("configures from the actual GoalForm without private egress and accepts only three explicit acknowledgments", async () => {
    renderEditor();
    expect(screen.getByLabelText("Public brief")).toHaveValue("");
    await preview();
    const review = screen.getByLabelText("Exact programme review");
    expect(review).toHaveTextContent(programme.artifact_prefix);
    expect(review).toHaveTextContent("grant revision 3");
    expect(screen.getByRole("button", { name: "Accept reviewed finite programme" })).toBeDisabled();
    acknowledge();
    fireEvent.click(screen.getByRole("button", { name: "Accept reviewed finite programme" }));
    await screen.findByLabelText("Programme programme-1");
    const previewCall = fetchMock.mock.calls.find(([url]) => url.endsWith("/preview"))!;
    const body = JSON.parse(previewCall[1].body);
    expect(body).toEqual({ expected_goal_revision: 4, expected_grant_revision: 2, public_brief: programme.public_brief,
      duration_days: 7, budget: { max_inference_microusd: 1000, max_outstanding_runs: 1 }, cadence: "daily", notification_limits: { per_day: 0 } });
    const acceptCall = fetchMock.mock.calls.find(([url]) => url.endsWith("/accept"))!;
    expect(JSON.parse(acceptCall[1].body)).toEqual({ ...body, review_digest: programme.review_digest,
      public_web_acknowledged: true, local_artifacts_acknowledged: true, inference_ceiling_acknowledged: true });
    expect(JSON.stringify(fetchMock.mock.calls)).not.toContain(goal.description);
    expect(JSON.stringify(fetchMock.mock.calls)).not.toContain(goal.title);
    expect(previewCall[1].credentials).toBe("include");
  });
  it("invalidates reviewed authority when the public draft changes", async () => {
    renderEditor(); await preview(); acknowledge();
    fireEvent.change(screen.getByLabelText("Public brief"), { target: { value: "Changed public scope" } });
    expect(screen.queryByLabelText("Exact programme review")).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([url]) => url.endsWith("/accept"))).toBe(false);
  });
  it("discloses changed-brief preview pause before action and shows the server-confirmed effect", async () => {
    stored = [{ ...programme, id: "old-programme", state: "active" }];
    const normal = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (url, init) => {
      if (url.endsWith("/preview")) {
        stored = [{ ...stored[0], state: "paused", reason_code: "programme_public_brief_changed" }];
        return response({ programme: previewed, review_digest: previewed.review_digest,
          public_only: true, preview_only: true, paused_programme_ids: ["old-programme"] });
      }
      return normal(url, init);
    });
    renderEditor();
    expect(screen.getByText(/clicking Preview immediately pauses previous programmes/)).toHaveTextContent("Abandoning this review will not resume them");
    await preview();
    expect(screen.getByLabelText("Exact programme review")).toHaveTextContent("Changed-brief preview paused these previous programmes: old-programme");
    await waitFor(() => expect(screen.getByLabelText("Programme old-programme")).toHaveTextContent("paused"));
    expect(fetchMock.mock.calls.some(([url]) => url.endsWith("/accept"))).toBe(false);
  });
  it("blocks programme review while a private Goal draft is unsaved", async () => {
    renderEditor(); await preview(); acknowledge();
    fireEvent.change(screen.getByPlaceholderText("Priority title"), { target: { value: "Private edited title" } });
    expect(screen.queryByLabelText("Exact programme review")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Preview finite public programme" })).toBeDisabled();
    expect(screen.getByText(/Save and reopen your changed priority/)).toBeInTheDocument();
  });
  it("rejects duration overflow and noninteger budget without server contact", async () => {
    renderEditor(); await waitFor(() => expect(screen.getByRole("button", { name: "Preview finite public programme" })).toBeEnabled());
    fireEvent.change(screen.getByLabelText("Public brief"), { target: { value: "Public" } });
    fireEvent.change(screen.getByLabelText("Programme duration (days)"), { target: { value: "8" } });
    fireEvent.click(screen.getByRole("button", { name: "Preview finite public programme" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("1–7 days");
    fireEvent.change(screen.getByLabelText("Programme duration (days)"), { target: { value: "7" } });
    fireEvent.change(screen.getByLabelText("Inference ceiling (micro USD)"), { target: { value: "1.5" } });
    fireEvent.click(screen.getByRole("button", { name: "Preview finite public programme" }));
    expect(fetchMock.mock.calls.every(([, init]) => init.method === "GET")).toBe(true);
  });
  it("shows configured blocked recovery and retained history for all terminal states", async () => {
    stored = ["blocked", "paused", "revoked", "review_due"].map((state, index) => ({ ...programme, id: `p-${state}`, grant_revision: index + 1,
      state: state as GoalProgramme["state"], reason_code: state === "blocked" ? "programme_budget_missing" : null,
      recovery: state === "blocked" ? "Configure the governed route and finite budget, then review a new programme." : "Review a new programme revision." }));
    renderEditor();
    const blocked = await screen.findByLabelText("Programme p-blocked");
    expect(blocked).toHaveTextContent("programme_budget_missing");
    expect(blocked).toHaveTextContent("Configure the governed route and finite budget");
    for (const state of ["blocked", "paused", "revoked", "review_due"]) expect(screen.getByLabelText(`Programme p-${state}`)).toHaveTextContent("History and existing liabilities remain retained");
    expect(within(screen.getByLabelText("Programme p-revoked")).getByRole("button", { name: "Revoke programme 3" })).toBeDisabled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
  it("requires fresh explicit recovery solely for exact old programme revoke and never renews", async () => {
    stored = [programme];
    const normal = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (url, init) => {
      if (url.endsWith("/revoke") && !JSON.parse(init.body).recover_owner_acknowledged) return response({ detail: { code: "programme_owner_recovery_required" } }, 403);
      return normal(url, init);
    });
    renderEditor(); await screen.findByLabelText("Programme programme-1");
    fireEvent.click(screen.getByRole("button", { name: "Revoke programme 3" }));
    await screen.findByText(/A fresh login needs explicit owner recovery/);
    expect(screen.getByRole("button", { name: "Confirm recovered revoke" })).toBeDisabled();
    fireEvent.click(screen.getByLabelText("I acknowledge recovery for this exact old programme control"));
    fireEvent.click(screen.getByRole("button", { name: "Confirm recovered revoke" }));
    await waitFor(() => expect(screen.getByLabelText("Programme programme-1")).toHaveTextContent("revoked"));
    const calls = fetchMock.mock.calls.filter(([url]) => url.endsWith("/revoke"));
    expect(JSON.parse(calls[0][1].body)).toEqual({ expected_grant_revision: 3, recover_owner_acknowledged: false });
    expect(JSON.parse(calls[1][1].body)).toEqual({ expected_grant_revision: 3, recover_owner_acknowledged: true });
    expect(fetchMock.mock.calls.every(([url]) => !url.endsWith("/preview") && !url.endsWith("/accept"))).toBe(true);
  });
  it("discards an in-flight preview after a changed draft", async () => {
    let resolve!: (value: unknown) => void;
    fetchMock.mockImplementation(async (url) => url.endsWith("/preview") ? new Promise((done) => { resolve = done; })
      : response({ goal_id: goal.id, grant_revision: 2, programmes: [] }));
    render(<GoalProgrammePanel goal={goal} />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Preview finite public programme" })).toBeEnabled());
    fireEvent.change(screen.getByLabelText("Public brief"), { target: { value: programme.public_brief } });
    fireEvent.click(screen.getByRole("button", { name: "Preview finite public programme" }));
    fireEvent.change(screen.getByLabelText("Public brief"), { target: { value: "Different scope" } });
    resolve(response({ programme, review_digest: programme.review_digest, public_only: true, preview_only: true }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Preview finite public programme" })).toBeEnabled());
    expect(screen.queryByLabelText("Exact programme review")).not.toBeInTheDocument();
  });
  it("fails closed on mismatched preview binding", async () => {
    previewed = { ...programme, goal_revision: 5 };
    renderEditor();
    await waitFor(() => expect(screen.getByRole("button", { name: "Preview finite public programme" })).toBeEnabled());
    fireEvent.change(screen.getByLabelText("Public brief"), { target: { value: programme.public_brief } });
    fireEvent.click(screen.getByRole("button", { name: "Preview finite public programme" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Programme review binding mismatch");
    expect(screen.queryByLabelText("Exact programme review")).not.toBeInTheDocument();
  });
  it("fails closed on incomplete programme metadata without rendering unsafe controls", async () => {
    fetchMock.mockResolvedValue(response({ goal_id: goal.id, grant_revision: 2, programmes: [{ id: "invalid" }] }));
    renderEditor();
    expect(await screen.findByRole("alert")).toHaveTextContent("Programme metadata unavailable");
    expect(screen.getByRole("button", { name: "Preview finite public programme" })).toBeDisabled();
    expect(screen.queryByLabelText("Programme invalid")).not.toBeInTheDocument();
  });
  it("retains history through metadata failure and does not reuse a rejected acceptance", async () => {
    stored = [programme]; renderEditor(); await preview(); acknowledge();
    fetchMock.mockResolvedValueOnce(response({ detail: { code: "programme_revision_stale" } }, 409));
    fireEvent.click(screen.getByRole("button", { name: "Accept reviewed finite programme" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("programme_revision_stale");
    expect(screen.queryByLabelText("Exact programme review")).not.toBeInTheDocument();
    fetchMock.mockRejectedValueOnce(new Error("metadata unavailable"));
    fireEvent.click(screen.getByRole("button", { name: "Reload programmes" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Retained history remains visible"));
    expect(screen.getByLabelText("Programme programme-1")).toBeInTheDocument();
  });
});
