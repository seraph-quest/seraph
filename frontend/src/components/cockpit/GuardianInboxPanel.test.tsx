import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { StrictMode, useState } from "react";
import { createRef } from "react";

import { GuardianInboxPanel, type GuardianInboxPanelHandle } from "./GuardianInboxPanel";
import type { GuardianInboxItem } from "../../types";

// Digest API bindings have their own finite-response suite. Keep legacy inbox
// request-order fixtures scoped to their original endpoint.
vi.mock("./programmeDigestApi", async (importOriginal) => ({
  ...await importOriginal<typeof import("./programmeDigestApi")>(),
  programmeDigestRequest: vi.fn().mockResolvedValue({ digests: [], programmes: [], notifications: {
    enabled: false, deadline_categories: [], digest_slots_remaining: 1, deadline_slots_remaining: 1,
    quiet_hours_active: false, delivery_debt: false,
  } }),
}));

function ControlledInspectorInbox() {
  const [selected, setSelected] = useState<GuardianInboxItem | null>(null);
  return <>
    <GuardianInboxPanel pollIntervalMs={0} selectedItemId={selected?.id ?? null}
      onSelectItem={setSelected} onRefreshSelectedItem={(id, next) => setSelected((current) => current?.id === id ? next : current)} />
    <output data-testid="controlled-selection">{selected?.title ?? "closed"}</output>
    <button onClick={() => setSelected(null)}>Close test inspector</button>
  </>;
}

function response(payload: unknown, ok = true, status = ok ? 200 : 409) {
  return { ok, status, json: async () => payload };
}

const item = {
  id: "inbox-1",
  revision: 3,
  state: "pending",
  source_kind: "source_packet",
  source_id: "packet-1",
  title: "Watched source changed",
  summary: "A verified local dossier is ready for your review",
  why_now: "Two material source keys changed.",
  goal_id: "goal-1",
  goal_revision: 4,
  watch_id: "watch-1",
  plan_revision: 2,
  task_id: null,
  expires_at: "2026-10-07T12:00:00Z",
  evidence_refs: [{ artifact_id: "artifact-dossier", content_sha256: "abc123" }],
  evidence_status: "verified",
  source_status: "succeeded",
  source_freshness: "current",
  verification_status: "passed",
  memory_status: "no_learning",
  policy_reason: null,
  allowed_actions: ["accept_followup", "snooze", "dismiss"],
};

const assessment = { schema_version: "seraph.opportunity.assessment.v1", relevance: 3, confidence: "medium",
  summary: "<script>open https://model.invalid/path</script>", reason: "The selected release removes an endpoint.",
  citations: [{ source_id: "release-notes", start_line: 2, end_line: 2, span_sha256: "a".repeat(64) }],
  suggested_blueprint: "public-evidence-report", abstain_reason: null };
function opportunity(overrides = {}) {
  return { ...item, id: "opportunity-1", source_kind: "guardian_opportunity", source_id: "opportunity-1",
    title: "Public evidence opportunity", summary: assessment.summary, opportunity_id: "opportunity-1",
    opportunity_revision: 3, opportunity_status: "proposed", assessment,
    reason_code: null, delivery_status: "not_requested", cancel_allowed: false,
    cancel_requested: false, quiescent: false, ...overrides };
}

describe("GuardianInboxPanel", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    window.sessionStorage.clear();
  });

  afterEach(() => {
    window.sessionStorage.clear();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("records an explicit revision-zero feedback vote, then appends a correction with the current CAS", async () => {
    const summary = { intervention_id: "intervention-1", feedback_revision: 0, feedback_type: null as string | null, feedback_at: null as string | null,
      feedback_event_id: null as string | null, feedback_history_digest: null as string | null, event_count: 0, memory_status: "no_learning", reason_code: null };
    const row = opportunity({ feedback_summary: summary, allowed_actions: [], delivery_status: "acknowledged" });
    fetchMock.mockImplementation((url, init) => {
      if (String(url).endsWith("/feedback")) {
        const body = JSON.parse(init.body);
        summary.feedback_revision += 1; summary.event_count += 1; summary.feedback_type = body.feedback_type;
        summary.feedback_at = "2026-10-06T10:00:00+00:00"; summary.feedback_event_id = "11111111-1111-4111-8111-111111111111";
        summary.feedback_history_digest = "a".repeat(64);
        return Promise.resolve(response({ opportunity_id: "opportunity-1", ...summary, feedback_event_digest: "b".repeat(64),
          outcome_binding_digest: "c".repeat(64), idempotent_replay: false }));
      }
      const current = { ...row, feedback_summary: { ...summary } };
      return Promise.resolve(response(String(url).endsWith("/inbox/opportunity-1") ? current : { items: [current] }));
    });
    render(<GuardianInboxPanel currentOwnerPrincipalId="operator:one" currentRootId="root-1" pollIntervalMs={0} />);
    const helpful = await screen.findByRole("button", { name: "Record Helpful" });
    fireEvent.click(screen.getByRole("button", { name: "View evidence and task" }));
    await screen.findByRole("button", { name: "Hide evidence" });
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(0);
    expect(screen.getByText(/No explicit vote/)).toBeInTheDocument();
    fireEvent.click(helpful);
    await screen.findByText(/Current feedback: helpful · revision 1/);
    fireEvent.click(screen.getByRole("button", { name: "Record Not helpful" }));
    await screen.findByText(/Current feedback: not_helpful · revision 2/);
    const posts = fetchMock.mock.calls.filter(([, init]) => init?.method === "POST");
    expect(posts).toHaveLength(2);
    expect(JSON.parse(posts[0][1].body)).toEqual({ expected_feedback_revision: 0, feedback_type: "helpful", reason: "", idempotency_key: expect.stringMatching(/^[a-f0-9-]{36}$/) });
    expect(JSON.parse(posts[1][1].body)).toEqual({ expected_feedback_revision: 1, feedback_type: "not_helpful", reason: "", idempotency_key: expect.stringMatching(/^[a-f0-9-]{36}$/) });
    expect(JSON.parse(posts[0][1].body).idempotency_key).not.toBe(JSON.parse(posts[1][1].body).idempotency_key);
  });

  it.each(["no_learning", "proposed"])("keeps a revision-zero CPU request through response loss and reload, inspecting only the original UUID through terminal %s", async (terminal) => {
    const row = opportunity({ feedback_summary: { intervention_id: "intervention-1", feedback_revision: 0, feedback_type: null,
      feedback_at: null, feedback_event_id: null, feedback_history_digest: null, event_count: 0, memory_status: "no_learning", reason_code: null }, allowed_actions: [] });
    let phase = "queued";
    fetchMock.mockImplementation((url, init) => {
      if (String(url).includes("/recommendation")) {
        if (init?.method === "POST") return Promise.reject(new TypeError("lost response"));
        return Promise.resolve(response({ opportunity_id: "opportunity-1", opportunity_revision: 3, feedback_revision: 0,
          task_id: "cpu-task-1", task_revision: 1, attempt_id: phase === "queued" ? null : "cpu-attempt-1", job_id: phase === "queued" ? null : "cpu-job-1",
          status: phase, proposal_id: phase === "proposed" ? "signed-proposal-1" : null, bundle_digest: phase === "queued" ? null : "b".repeat(64), population_digest: "a".repeat(64),
          reason_code: phase === "queued" ? "opportunity_recommendation_queued" : phase === "proposed" ? "opportunity_preference_proposed" : "opportunity_feedback_insufficient_or_conflicting",
          idempotent_replay: true, memory_status: "no_learning" }));
      }
      return Promise.resolve(response({ items: [row] }));
    });
    const view = render(<GuardianInboxPanel currentOwnerPrincipalId="operator:one" currentRootId="root-1" pollIntervalMs={0} />);
    fireEvent.click(await screen.findByRole("button", { name: "Request opportunity recommendation" }));
    await screen.findByText("lost response");
    const first = fetchMock.mock.calls.find(([, init]) => init?.method === "POST")!;
    const body = JSON.parse(first[1].body);
    expect(body).toEqual({ expected_opportunity_revision: 3, expected_feedback_revision: 0, idempotency_key: expect.stringMatching(/^[a-f0-9-]{36}$/) });
    view.unmount();
    render(<GuardianInboxPanel currentOwnerPrincipalId="operator:one" currentRootId="root-1" pollIntervalMs={0} />);
    fireEvent.click(await screen.findByRole("button", { name: "Refresh existing recommendation" }));
    await screen.findByText(/CPU recommendation queued/);
    expect(screen.getByText(/CPU stage does not adopt memory/)).toBeInTheDocument();
    expect(screen.queryByText(/insufficient evidence/)).not.toBeInTheDocument();
    phase = terminal;
    fireEvent.click(screen.getByRole("button", { name: "Refresh existing recommendation" }));
    await screen.findByText(terminal === "no_learning" ? "Actual CPU result: insufficient evidence. No signed memory proposal."
      : "A signed proposal exists; inspect its current review and adoption state in Work.");
    expect(screen.queryByText(/memory not adopted|proposal awaits/)).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
    const inspections = fetchMock.mock.calls.filter(([url]) => String(url).includes("/recommendation?"));
    expect(inspections).toHaveLength(2);
    expect(inspections.every(([url]) => String(url).endsWith(`idempotency_key=${body.idempotency_key}`))).toBe(true);
    expect(screen.queryByRole("button", { name: /Adopt/ })).not.toBeInTheDocument();
  });

  it("renders cited assessment prose literally and shows only exact normalized evidence spans", async () => {
    const row = opportunity({ allowed_actions: [] });
    fetchMock.mockResolvedValueOnce(response({ items: [row] }));
    fetchMock.mockResolvedValueOnce(response({ ...row, evidence_previews: [{ artifact_id: "artifact-dossier", sha256: "abc123",
      source_id: "release-notes", text: "Uncited heading\nThe endpoint was removed.\nUncited footer", line_count: 3 }] }));
    render(<GuardianInboxPanel pollIntervalMs={0} />);
    expect(await screen.findByText(assessment.summary)).toBeInTheDocument();
    expect(screen.getByText(/Model judgment · relevance 3\/4 · confidence medium/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /model.invalid/ })).not.toBeInTheDocument();
    expect(document.querySelector("script")).toBeNull();
    expect(screen.queryByRole("button", { name: "Accept follow-up" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "View evidence and task" }));
    expect(await screen.findByText("The endpoint was removed.")).toBeInTheDocument();
    expect(screen.queryByText("Uncited heading")).not.toBeInTheDocument();
    expect(screen.getByText(/normalized redacted lines 2–2/)).toHaveTextContent("a".repeat(64));
  });

  it("stages one exact plan only on explicit Generate without accepting or dispatching", async () => {
    const offer = { available_blueprint_ids: ["public-browser-check", "public-evidence-report"], unavailable_reason: null,
      can_generate: true, generation_block_reason: null, proposal_ref: null };
    const ref = { proposal_id: "plan-1", proposal_revision: 1, parent_task_id: "triage-1", parent_revision: 1,
      kind: "opportunity_plan", proposal_digest: null, blueprint_id: null, expires_at: "2030-01-01T00:00:00Z", status: "pending_inference" };
    const row = opportunity({ plan_offer: offer });
    fetchMock.mockResolvedValueOnce(response({ items: [row] }));
    fetchMock.mockResolvedValueOnce(response({ opportunity_id: row.opportunity_id, opportunity_revision: 4, proposal_ref: ref, reason_code: null }));
    fetchMock.mockResolvedValueOnce(response({ items: [{ ...row, opportunity_revision: 4,
      plan_offer: { ...offer, can_generate: false, generation_block_reason: "plan_exists", proposal_ref: ref } }] }));
    const onOpenTask = vi.fn();
    render(<GuardianInboxPanel pollIntervalMs={0} currentOwnerPrincipalId="operator:one" currentRootId="root-1" onOpenTask={onOpenTask} />);
    const button = await screen.findByRole("button", { name: "Generate plan" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    fireEvent.click(button); fireEvent.click(button);
    await screen.findByRole("button", { name: "Review staged read-only plan in Work" });
    const posts = fetchMock.mock.calls.filter(([, init]) => init?.method === "POST");
    expect(posts).toHaveLength(1);
    expect(posts[0][0]).toContain("/api/guardian/opportunities/opportunity-1/plan");
    expect(JSON.parse(posts[0][1].body)).toEqual({ expected_opportunity_revision: 3, expected_goal_revision: 4,
      idempotency_key: expect.stringMatching(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/) });
    fireEvent.click(screen.getByRole("button", { name: "Review staged read-only plan in Work" }));
    expect(onOpenTask).toHaveBeenCalledWith("triage-1", expect.objectContaining({ opportunity_id: "opportunity-1" }));
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it.each(["started", "succeeded", "unknown"])("reload with %s contact only inspects and never resends the retained generation", async (contact) => {
    const row = { ...opportunity(), plan_offer: { available_blueprint_ids: ["public-browser-check"], unavailable_reason: null,
      can_generate: true, generation_block_reason: null, proposal_ref: null } };
    fetchMock.mockResolvedValueOnce(response({ items: [row] }));
    fetchMock.mockRejectedValueOnce(new Error("Lost response"));
    const mounted = render(<GuardianInboxPanel pollIntervalMs={0} currentOwnerPrincipalId="operator:one" currentRootId="root-1" />);
    fireEvent.click(await screen.findByRole("button", { name: "Generate plan" }));
    await screen.findByText("Lost response"); mounted.unmount();
    fetchMock.mockResolvedValueOnce(response({ items: [{ ...row, plan_offer: { ...row.plan_offer, can_generate: false,
      generation_block_reason: "plan_exists", proposal_ref: { proposal_id: "plan-1", kind: "opportunity_plan", proposal_revision: 1,
        parent_task_id: "triage-1", parent_revision: 1, proposal_digest: null, blueprint_id: null,
        expires_at: "2030-01-01T00:00:00Z", status: "pending_inference", provider_contact_state: contact } } }] }));
    render(<GuardianInboxPanel pollIntervalMs={0} currentOwnerPrincipalId="operator:one" currentRootId="root-1" />);
    expect(await screen.findByRole("button", { name: "Retry exact never-contacted plan request" })).toBeDisabled();
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("shows eligible blueprints after the daily cap and sends zero additional requests", async () => {
    fetchMock.mockResolvedValueOnce(response({ items: [opportunity({ plan_offer: {
      available_blueprint_ids: ["public-browser-check", "public-evidence-report"], unavailable_reason: null,
      can_generate: false, generation_block_reason: "daily_plan_cap", proposal_ref: null } })] }));
    render(<GuardianInboxPanel pollIntervalMs={0} currentOwnerPrincipalId="operator:one" currentRootId="root-1" />);
    expect(await screen.findByRole("button", { name: "Generate plan" })).toBeDisabled();
    expect(screen.getByText("Available read-only blueprints: public-browser-check, public-evidence-report")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("only manually replays the original UUID and revisions after canonical never-contacted proof", async () => {
    const offer = { available_blueprint_ids: ["public-browser-check"], unavailable_reason: null, can_generate: true, generation_block_reason: null, proposal_ref: null };
    const row = opportunity({ plan_offer: offer });
    const ref = { proposal_id: "plan-1", kind: "opportunity_plan", proposal_revision: 1, parent_task_id: "triage-1", parent_revision: 1,
      proposal_digest: null, blueprint_id: null, expires_at: "2030-01-01T00:00:00Z", status: "pending_inference",
      provider_contact_state: "not_started", generation_retry_allowed: true };
    const currentRow = { ...row, opportunity_revision: 4, plan_offer: { ...offer, can_generate: false, generation_block_reason: "plan_exists", proposal_ref: ref } };
    fetchMock.mockResolvedValueOnce(response({ items: [row] })); fetchMock.mockRejectedValueOnce(new Error("Lost response"));
    const mounted = render(<GuardianInboxPanel pollIntervalMs={0} currentOwnerPrincipalId="operator:one" currentRootId="root-1" />);
    fireEvent.click(await screen.findByRole("button", { name: "Generate plan" }));
    await screen.findByText("Lost response");
    const originalBody = fetchMock.mock.calls[1][1].body; mounted.unmount();
    fetchMock.mockResolvedValueOnce(response({ items: [currentRow] }));
    fetchMock.mockResolvedValueOnce(response({ opportunity_id: "opportunity-1", opportunity_revision: 4, proposal_ref: ref, reason_code: null }));
    fetchMock.mockResolvedValueOnce(response({ items: [currentRow] }));
    render(<GuardianInboxPanel pollIntervalMs={0} currentOwnerPrincipalId="operator:one" currentRootId="root-1" />);
    const retry = await screen.findByRole("button", { name: "Retry exact never-contacted plan request" });
    await waitFor(() => expect(retry).toBeEnabled());
    expect(fetchMock).toHaveBeenCalledTimes(3);
    fireEvent.click(retry);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(5));
    expect(fetchMock.mock.calls[3][1].body).toBe(originalBody);
    expect(fetchMock.mock.calls[3][0]).toBe(fetchMock.mock.calls[1][0]);
  });

  it("aborts generation and ignores a late response when the authenticated owner or Root changes", async () => {
    const offer = { available_blueprint_ids: ["public-browser-check"], unavailable_reason: null, can_generate: true, generation_block_reason: null, proposal_ref: null };
    const row = opportunity({ plan_offer: offer });
    let resolvePost: (value: ReturnType<typeof response>) => void = () => {};
    fetchMock.mockResolvedValueOnce(response({ items: [row] }));
    fetchMock.mockImplementationOnce(() => new Promise((resolve) => { resolvePost = resolve; }));
    const mounted = render(<GuardianInboxPanel pollIntervalMs={0} currentOwnerPrincipalId="operator:one" currentRootId="root-1" />);
    fireEvent.click(await screen.findByRole("button", { name: "Generate plan" }));
    const signal = fetchMock.mock.calls[1][1].signal as AbortSignal;
    fetchMock.mockResolvedValueOnce(response({ items: [] }));
    mounted.rerender(<GuardianInboxPanel pollIntervalMs={0} currentOwnerPrincipalId="operator:two" currentRootId="root-2" />);
    await waitFor(() => expect(signal.aborted).toBe(true));
    await act(async () => resolvePost(response({ opportunity_id: "opportunity-1", opportunity_revision: 4, proposal_ref: null, reason_code: null })));
    await waitFor(() => expect(screen.queryByTestId("guardian-inbox-row-opportunity-1")).not.toBeInTheDocument());
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
  });

  it("does not submit Generate when exact session retention is unavailable", async () => {
    fetchMock.mockResolvedValueOnce(response({ items: [opportunity({ plan_offer: { available_blueprint_ids: ["public-browser-check"],
      unavailable_reason: null, can_generate: true, generation_block_reason: null, proposal_ref: null } })] }));
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("session storage unavailable"); });
    render(<GuardianInboxPanel pollIntervalMs={0} currentOwnerPrincipalId="operator:one" currentRootId="root-1" />);
    fireEvent.click(await screen.findByRole("button", { name: "Generate plan" }));
    await screen.findByText("session storage unavailable");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("shows silent, blocked, stale, Unknown and cancelled history without inventing actions", async () => {
    fetchMock.mockResolvedValueOnce(response({ items: [
      opportunity({ id: "silent", opportunity_id: "silent", state: "silent", opportunity_status: "silent", allowed_actions: [], reason_code: "low_relevance" }),
      opportunity({ id: "blocked", opportunity_id: "blocked", state: "blocked", opportunity_status: "blocked", assessment: null, allowed_actions: [], reason_code: "source_excerpt_unavailable" }),
      opportunity({ id: "stale", opportunity_id: "stale", allowed_actions: [], reason_code: "goal_review_required" }),
      opportunity({ id: "unknown", opportunity_id: "unknown", state: "unknown", opportunity_status: "unknown", assessment: null, allowed_actions: [], reason_code: "outcome_unknown" }),
      opportunity({ id: "cancelled", opportunity_id: "cancelled", state: "cancelled", opportunity_status: "cancelled", assessment: null, allowed_actions: [], quiescent: true }),
    ] }));
    render(<GuardianInboxPanel pollIntervalMs={0} />);
    await screen.findByTestId("guardian-inbox-row-silent");
    expect(screen.getByText("Silent assessment history; no proposed action.")).toBeInTheDocument();
    expect(screen.getByText(/Goal review required/)).toBeInTheDocument();
    expect(screen.getByText(/Outcome Unknown; retained inference liability/)).toBeInTheDocument();
    expect(screen.getByTestId("guardian-inbox-row-cancelled")).toHaveAttribute("data-state", "cancelled");
    expect(screen.queryByRole("button", { name: "Accept follow-up" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel assessment" })).not.toBeInTheDocument();
    expect(screen.queryByText(/server state is not recognized/)).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it.each([
    { status: "blocked", reason: "source_excerpt_unavailable", policy: "source_stale" },
    { status: "proposed", reason: "goal_review_required", policy: "goal_review_required" },
    { status: "proposed", reason: "source_stale", policy: "source_stale" },
  ])("shows $status recovery reason $reason alongside live policy $policy without restoring evidence or actions", async ({ status, reason, policy }) => {
    // Proposed rows with no stored reason receive the live reason in the API projection.
    const row = opportunity({ state: status, opportunity_status: status,
      assessment: status === "blocked" ? null : assessment,
      reason_code: reason, policy_reason: policy, source_freshness: "stale",
      evidence_status: "unavailable", evidence_refs: [], allowed_actions: [], recovery_action: "review_goal_and_watch" });
    fetchMock.mockResolvedValueOnce(response({ items: [row] }));
    render(<GuardianInboxPanel pollIntervalMs={0} />);
    const article = await screen.findByTestId("guardian-inbox-row-opportunity-1");
    expect(within(article).getByText(`Assessment ${status} · reason ${reason} · delivery not_requested · no learning`)).toBeInTheDocument();
    expect(within(article).getByText(`authority / budget boundary · ${policy}`)).toBeInTheDocument();
    expect(within(article).getByText(/evidence unavailable/)).toBeInTheDocument();
    expect(within(article).getByText("recovery · review_goal_and_watch")).toBeInTheDocument();
    expect(within(article).queryByRole("button", { name: "Accept follow-up" })).not.toBeInTheDocument();
    expect(within(article).queryByRole("button", { name: "Cancel assessment" })).not.toBeInTheDocument();
    expect(within(article).queryByRole("button", { name: "Dismiss" })).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toContain("/api/guardian/inbox");
  });

  it("preserves a silent assessment reason without reporting it as a current authority boundary", async () => {
    const row = opportunity({ state: "silent", opportunity_status: "silent", reason_code: "assessment_abstained",
      policy_reason: null, why_now: "assessment_abstained", allowed_actions: [] });
    fetchMock.mockResolvedValueOnce(response({ items: [row] }));
    render(<GuardianInboxPanel pollIntervalMs={0} />);
    const article = await screen.findByTestId("guardian-inbox-row-opportunity-1");
    expect(within(article).getByText("Assessment silent · reason assessment_abstained · delivery not_requested · no learning")).toBeInTheDocument();
    expect(within(article).getByText("authority / budget boundary · No current boundary reason")).toBeInTheDocument();
    expect(within(article).getByText("Silent assessment history; no proposed action.")).toBeInTheDocument();
    expect(within(article).queryByRole("button", { name: "Accept follow-up" })).not.toBeInTheDocument();
    expect(within(article).queryByRole("button", { name: "Cancel assessment" })).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it.each(["closed", "another row"])("does not overwrite %s selection with an in-flight detail response", async (selection) => {
    const first = opportunity();
    const second = opportunity({ id: "opportunity-2", opportunity_id: "opportunity-2", source_id: "opportunity-2", title: "Another opportunity" });
    let resolveDetail: (value: ReturnType<typeof response>) => void = () => {};
    fetchMock.mockResolvedValueOnce(response({ items: [first, second] }));
    fetchMock.mockImplementationOnce(() => new Promise<ReturnType<typeof response>>((resolve) => { resolveDetail = resolve; }));
    fetchMock.mockResolvedValueOnce(response(second));
    render(<ControlledInspectorInbox />);
    const firstRow = await screen.findByTestId(`guardian-inbox-row-${first.id}`);
    fireEvent.click(within(firstRow).getByRole("button", { name: "View evidence and task" }));
    expect(screen.getByTestId("controlled-selection")).toHaveTextContent(first.title);
    if (selection === "closed") fireEvent.click(screen.getByRole("button", { name: "Close test inspector" }));
    else fireEvent.click(within(screen.getByTestId(`guardian-inbox-row-${second.id}`)).getByRole("button", { name: "View evidence and task" }));
    await act(async () => resolveDetail(response({ ...first, title: "Old detail must not reselect" })));
    expect(screen.getByTestId("controlled-selection")).toHaveTextContent(selection === "closed" ? "closed" : second.title);
  });

  it("clears the controlled selected inspector when a successful replacement list removes its ID", async () => {
    const row = opportunity();
    fetchMock.mockResolvedValueOnce(response({ items: [row] }));
    fetchMock.mockResolvedValueOnce(response(row));
    fetchMock.mockResolvedValueOnce(response({ items: [] }));
    render(<ControlledInspectorInbox />);
    fireEvent.click(await screen.findByRole("button", { name: "View evidence and task" }));
    expect(screen.getByTestId("controlled-selection")).toHaveTextContent(row.title);
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(screen.getByTestId("controlled-selection")).toHaveTextContent("closed"));
  });

  it("requires server cancellation availability and waits for quiescence instead of claiming cancelled", async () => {
    const row = opportunity({ state: "assessing", opportunity_status: "assessing", assessment: null, allowed_actions: [], cancel_allowed: true });
    fetchMock.mockResolvedValueOnce(response({ items: [row] }));
    fetchMock.mockResolvedValueOnce(response({ opportunity_id: row.opportunity_id, revision: 4, status: "assessing",
      reason_code: "cancel_requested", cancel_requested: true, quiescent: false }));
    render(<GuardianInboxPanel pollIntervalMs={0} />);
    fireEvent.click(await screen.findByRole("button", { name: "Cancel assessment" }));
    await screen.findByText(/Cancellation requested · waiting for native quiescence/);
    expect(screen.getByTestId("guardian-inbox-row-opportunity-1")).toHaveAttribute("data-state", "assessing");
    expect(screen.queryByRole("button", { name: "Cancel assessment" })).not.toBeInTheDocument();
    const [url, init] = fetchMock.mock.calls[1];
    expect(url).toContain("/api/guardian/opportunities/opportunity-1/cancel");
    expect(JSON.parse(init.body)).toEqual({ expected_opportunity_revision: 3,
      idempotency_key: expect.stringMatching(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/) });
    expect(fetchMock).toHaveBeenCalledTimes(2);
    fetchMock.mockResolvedValueOnce(response({ items: [{ ...row, state: "cancelled", opportunity_status: "cancelled", opportunity_revision: 5,
      cancel_allowed: false, cancel_requested: true, quiescent: true }] }));
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(screen.getByTestId("guardian-inbox-row-opportunity-1")).toHaveAttribute("data-state", "cancelled"));
  });

  it("retains contacted Unknown liability even after native quiescence is confirmed", async () => {
    fetchMock.mockResolvedValueOnce(response({ items: [opportunity({ state: "unknown", opportunity_status: "unknown", assessment: null,
      allowed_actions: [], cancel_allowed: true })] }));
    fetchMock.mockResolvedValueOnce(response({ opportunity_id: "opportunity-1", revision: 4, status: "unknown",
      reason_code: "outcome_unknown", cancel_requested: true, quiescent: true }));
    render(<GuardianInboxPanel pollIntervalMs={0} />);
    fireEvent.click(await screen.findByRole("button", { name: "Cancel assessment" }));
    await screen.findByText(/Native work is quiescent; Unknown outcome and cost liability remain retained/);
    expect(screen.getByTestId("guardian-inbox-row-opportunity-1")).toHaveAttribute("data-state", "unknown");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("does not automatically retry a stale cancellation or accept malformed model citations", async () => {
    fetchMock.mockResolvedValueOnce(response({ items: [opportunity({ cancel_allowed: true,
      assessment: { ...assessment, citations: [{ ...assessment.citations[0], end_line: 201 }] } })] }));
    fetchMock.mockResolvedValueOnce(response({ detail: { code: "opportunity_revision_stale" } }, false, 409));
    render(<GuardianInboxPanel pollIntervalMs={0} />);
    fireEvent.click(await screen.findByRole("button", { name: "Cancel assessment" }));
    await screen.findByText(/Opportunity changed. Refresh and review/);
    expect(screen.queryByRole("button", { name: "Accept follow-up" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Cancel assessment" })).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("does not let cached cited detail restore actions that the refreshed current projection removed", async () => {
    const row = opportunity();
    fetchMock.mockResolvedValueOnce(response({ items: [row] }));
    fetchMock.mockResolvedValueOnce(response({ ...row, evidence_previews: [{ artifact_id: "artifact-dossier", sha256: "abc123",
      source_id: "release-notes", text: "Header\nAuthorized evidence", line_count: 2 }] }));
    render(<GuardianInboxPanel pollIntervalMs={0} />);
    fireEvent.click(await screen.findByRole("button", { name: "View evidence and task" }));
    await screen.findByText("Authorized evidence");
    expect(screen.getByRole("button", { name: "Accept follow-up" })).toBeInTheDocument();
    fetchMock.mockResolvedValueOnce(response({ items: [{ ...row, allowed_actions: [], reason_code: "source_stale", evidence_status: "unavailable" }] }));
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Accept follow-up" })).not.toBeInTheDocument());
    expect(screen.queryByText("Authorized evidence")).not.toBeInTheDocument();
    expect(screen.getByText(/Exact cited excerpt unavailable/)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("loads a safe item without mutating or invoking a provider on mount", async () => {
    fetchMock.mockResolvedValueOnce(response({ items: [item], next_cursor: null, last_confirmed_at: "2026-09-30T10:00:00Z" }));
    render(<GuardianInboxPanel pollIntervalMs={0} />);

    expect(await screen.findByText("Watched source changed · pending")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ credentials: "include" });
    expect(fetchMock.mock.calls.some(([, init]) => (init as RequestInit | undefined)?.method === "POST")).toBe(false);
  });

  it("shows a busy skeleton before the first inbox page is confirmed", async () => {
    let resolveList: (value: ReturnType<typeof response>) => void = () => {};
    fetchMock.mockImplementationOnce(() => new Promise<ReturnType<typeof response>>((resolve) => { resolveList = resolve; }));
    render(<GuardianInboxPanel pollIntervalMs={0} />);

    expect(screen.getByTestId("guardian-inbox-panel")).toHaveAttribute("aria-busy", "true");
    expect(screen.getByTestId("guardian-inbox-loading")).toBeInTheDocument();
    expect(screen.queryByText("No pending decisions.")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Accept follow-up" })).not.toBeInTheDocument();

    await act(async () => {
      resolveList(response({ items: [] }));
    });
    expect(await screen.findByText("No pending decisions.")).toBeInTheDocument();
  });

  it("offers canonical Goals and Work links for an empty inbox", async () => {
    const onOpenGoals = vi.fn();
    const onOpenWork = vi.fn();
    fetchMock.mockResolvedValueOnce(response({ items: [] }));
    render(<GuardianInboxPanel pollIntervalMs={0} onOpenGoals={onOpenGoals} onOpenWork={onOpenWork} />);

    fireEvent.click(await screen.findByRole("button", { name: "Open Goals" }));
    fireEvent.click(screen.getByRole("button", { name: "Open Work" }));
    expect(onOpenGoals).toHaveBeenCalledOnce();
    expect(onOpenWork).toHaveBeenCalledOnce();
  });

  it("reuses the same action key when a stale action is retried", async () => {
    fetchMock
      .mockResolvedValueOnce(response({ items: [item] }))
      .mockResolvedValueOnce(response({ detail: { code: "stale_inbox_revision", recovery: "Refresh the item." } }, false, 409))
      .mockResolvedValueOnce(response({ items: [item] }))
      .mockResolvedValueOnce(response({
        id: "inbox-1",
        revision: 4,
        state: "accepted",
        task_id: "task-1",
        receipt_id: "receipt-1",
      }));
    render(<GuardianInboxPanel pollIntervalMs={0} />);
    fireEvent.click(await screen.findByRole("button", { name: "Accept follow-up" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("stale_inbox_revision");
    fireEvent.click(screen.getByRole("button", { name: "Accept follow-up" }));

    await waitFor(() => expect(screen.getByText("Accept follow-up recorded.")).toBeInTheDocument());
    const firstBody = JSON.parse(String((fetchMock.mock.calls[1][1] as RequestInit).body));
    const secondBody = JSON.parse(String((fetchMock.mock.calls[3][1] as RequestInit).body));
    expect(firstBody).toEqual({
      action: "accept_followup",
      expected_revision: 3,
      idempotency_key: expect.any(String),
    });
    expect(secondBody).toEqual(firstBody);
  });

  it("retains an unknown accept payload across panel remounts", async () => {
    fetchMock
      .mockResolvedValueOnce(response({ items: [item] }))
      .mockResolvedValueOnce(response({ detail: { message: "temporary action outage" } }, false, 503))
      .mockResolvedValueOnce(response({ items: [item] }))
      .mockResolvedValueOnce(response({ items: [item] }))
      .mockResolvedValueOnce(response({
        id: item.id,
        revision: 4,
        state: "accepted",
        task_id: "task-after-retry",
        receipt_id: "receipt-after-retry",
      }));
    const first = render(<GuardianInboxPanel pollIntervalMs={0} />);
    fireEvent.click(await screen.findByRole("button", { name: "Accept follow-up" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Outcome unknown"));
    const stored = window.sessionStorage.getItem("seraph.guardian.gesture.v1:inbox-1:accept_followup");
    expect(stored).toContain("idempotency_key");
    expect(stored).not.toContain("temporary action outage");
    const firstBody = JSON.parse(String((fetchMock.mock.calls[1][1] as RequestInit).body));

    first.unmount();
    render(<GuardianInboxPanel pollIntervalMs={0} />);
    fireEvent.click(await screen.findByRole("button", { name: "Accept follow-up" }));
    await waitFor(() => expect(screen.getByText("Accept follow-up recorded.")).toBeInTheDocument());
    const secondBody = JSON.parse(String((fetchMock.mock.calls[4][1] as RequestInit).body));
    expect(secondBody).toEqual(firstBody);
  });

  it("requires the original dismiss reason before retrying an unknown outcome", async () => {
    fetchMock
      .mockResolvedValueOnce(response({ items: [item] }))
      .mockResolvedValueOnce(response({ detail: { message: "temporary action outage" } }, false, 503))
      .mockResolvedValueOnce(response({ items: [item] }))
      .mockResolvedValueOnce(response({ items: [item] }));
    const first = render(<GuardianInboxPanel pollIntervalMs={0} />);
    const firstReason = await screen.findByLabelText("Dismiss reason for Watched source changed");
    fireEvent.change(firstReason, { target: { value: "duplicate source packet" } });
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Outcome unknown"));
    const stored = window.sessionStorage.getItem("seraph.guardian.gesture.v1:inbox-1:dismiss");
    expect(stored).toContain("reason_digest");
    expect(stored).not.toContain("duplicate source packet");

    first.unmount();
    render(<GuardianInboxPanel pollIntervalMs={0} />);
    const changedReason = await screen.findByLabelText("Dismiss reason for Watched source changed");
    fireEvent.change(changedReason, { target: { value: "different reason" } });
    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Re-enter the same dismiss reason"));
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it("keeps a failed snooze payload stable and requires refresh before changed input creates a new gesture", async () => {
    fetchMock
      .mockResolvedValueOnce(response({ items: [item] }))
      .mockResolvedValueOnce(response({ detail: { code: "stale_inbox_revision", recovery: "Refresh the inbox." } }, false, 409))
      .mockResolvedValueOnce(response({ items: [{ ...item, revision: 4 }] }))
      .mockResolvedValueOnce(response({ items: [{ ...item, revision: 4 }] }))
      .mockResolvedValueOnce(response({
        id: "inbox-1",
        revision: 5,
        state: "snoozed",
        receipt_id: "receipt-snooze-1",
      }));
    render(<GuardianInboxPanel pollIntervalMs={0} />);
    const snooze = await screen.findByLabelText("Snooze Watched source changed");
    fireEvent.change(snooze, { target: { value: "2030-01-02T03:04" } });
    fireEvent.click(screen.getByRole("button", { name: "Snooze" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("stale_inbox_revision");
    expect(snooze).not.toBeDisabled();

    fireEvent.change(snooze, { target: { value: "2031-02-03T04:05" } });
    fireEvent.click(screen.getByRole("button", { name: "Snooze" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Refresh the inbox before sending a new request");
    expect(fetchMock).toHaveBeenCalledTimes(3);

    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
    await waitFor(() => expect(screen.getByRole("button", { name: "Refresh" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Snooze" }));
    await waitFor(() => expect(screen.getByText("Snooze recorded.")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Accept follow-up" })).not.toBeInTheDocument();

    const firstBody = JSON.parse(String((fetchMock.mock.calls[1][1] as RequestInit).body));
    const secondBody = JSON.parse(String((fetchMock.mock.calls[4][1] as RequestInit).body));
    expect(new Date(firstBody.until).toISOString()).toBe(new Date("2030-01-02T03:04").toISOString());
    expect(new Date(secondBody.until).toISOString()).toBe(new Date("2031-02-03T04:05").toISOString());
    expect(secondBody.idempotency_key).not.toBe(firstBody.idempotency_key);
    expect(secondBody.expected_revision).toBe(4);
  });

  it("clears a definitive snooze rejection so corrected input creates a fresh gesture", async () => {
    fetchMock
      .mockResolvedValueOnce(response({ items: [item] }))
      .mockResolvedValueOnce(response({ detail: { code: "invalid_snooze_until", message: "Snooze time is outside the allowed bounds." } }, false, 422))
      .mockResolvedValueOnce(response({
        id: item.id,
        revision: 4,
        state: "snoozed",
        receipt_id: "receipt-corrected-snooze",
      }));
    render(<GuardianInboxPanel pollIntervalMs={0} />);
    const snooze = await screen.findByLabelText("Snooze Watched source changed");
    fireEvent.change(snooze, { target: { value: "2020-01-01T00:00" } });
    fireEvent.click(screen.getByRole("button", { name: "Snooze" }));

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Snooze time is outside the allowed bounds."));
    expect(window.sessionStorage.getItem("seraph.guardian.gesture.v1:inbox-1:snooze")).toBeNull();

    fireEvent.change(snooze, { target: { value: "2030-01-02T03:04" } });
    fireEvent.click(screen.getByRole("button", { name: "Snooze" }));
    await waitFor(() => expect(screen.getByText("Snooze recorded.")).toBeInTheDocument());

    expect(fetchMock).toHaveBeenCalledTimes(3);
    const rejectedBody = JSON.parse(String((fetchMock.mock.calls[1][1] as RequestInit).body));
    const correctedBody = JSON.parse(String((fetchMock.mock.calls[2][1] as RequestInit).body));
    expect(rejectedBody.expected_revision).toBe(3);
    expect(correctedBody.expected_revision).toBe(3);
    expect(correctedBody.idempotency_key).not.toBe(rejectedBody.idempotency_key);
    expect(new Date(correctedBody.until).toISOString()).toBe(new Date("2030-01-02T03:04").toISOString());
  });

  it("retains the last-known inbox when a refresh fails", async () => {
    fetchMock
      .mockResolvedValueOnce(response({ items: [item] }))
      .mockResolvedValueOnce(response({ detail: { code: "inbox_unavailable", message: "temporary failure" } }, false, 503));
    render(<GuardianInboxPanel pollIntervalMs={0} />);
    expect(await screen.findByText("Watched source changed · pending")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(await screen.findByText(/last-known items/)).toBeInTheDocument();
    expect(screen.getByText("Watched source changed · pending")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Accept follow-up" })).toBeDisabled();
  });

  it("renders an authorized snooze time without inventing a snooze reason", async () => {
    fetchMock.mockResolvedValueOnce(response({
      items: [{ ...item, state: "snoozed", snoozed_until: "2030-01-01T00:00:00Z", allowed_actions: [] }],
    }));
    render(<GuardianInboxPanel pollIntervalMs={0} />);

    expect(await screen.findByText(/Watched source changed · Snoozed until/)).toBeInTheDocument();
    expect(screen.getByText(/reason unavailable/)).toBeInTheDocument();
  });

  it("aborts overlapping list loads and ignores the older response", async () => {
    const pending: Array<{
      resolve: (value: ReturnType<typeof response>) => void;
      signal: AbortSignal | undefined;
    }> = [];
    fetchMock.mockImplementation((_input: RequestInfo | URL, init?: RequestInit) => (
      new Promise<ReturnType<typeof response>>((resolve) => {
        pending.push({ resolve, signal: init?.signal as AbortSignal | undefined });
      })
    ));
    const { rerender } = render(<GuardianInboxPanel pollIntervalMs={0} pageSize={50} />);
    await waitFor(() => expect(pending).toHaveLength(1));

    rerender(<GuardianInboxPanel pollIntervalMs={0} pageSize={49} />);
    await waitFor(() => expect(pending).toHaveLength(2));
    expect(pending[0].signal?.aborted).toBe(true);

    await act(async () => {
      pending[1].resolve(response({ items: [{ ...item, id: "newer", title: "Newer item" }] }));
    });
    expect(await screen.findByText("Newer item · pending")).toBeInTheDocument();

    await act(async () => {
      pending[0].resolve(response({ items: [item] }));
    });
    expect(screen.queryByText("Watched source changed · pending")).not.toBeInTheDocument();
  });

  it("aborts a hidden detail request and ignores its late evidence", async () => {
    let resolveDetail: (value: ReturnType<typeof response>) => void = () => {};
    let detailSignal: AbortSignal | undefined;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/guardian/inbox?") && !url.endsWith("/inbox-1")) {
        return Promise.resolve(response({ items: [item] }));
      }
      if (url.endsWith("/api/guardian/inbox/inbox-1")) {
        detailSignal = init?.signal as AbortSignal | undefined;
        return new Promise<ReturnType<typeof response>>((resolve) => { resolveDetail = resolve; });
      }
      return Promise.resolve(response({}));
    });
    render(<GuardianInboxPanel pollIntervalMs={0} />);
    fireEvent.click(await screen.findByRole("button", { name: "View evidence and task" }));
    expect(await screen.findByText("Refreshing verified evidence details…")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Hide evidence" }));
    expect(detailSignal?.aborted).toBe(true);

    await act(async () => {
      resolveDetail(response({ ...item, job: { id: "late-job", status: "succeeded" } }));
    });
    expect(screen.queryByText(/durable job late-job/)).not.toBeInTheDocument();
  });

  it("opens an accepted task through the existing work-board focus callback", async () => {
    const onOpenTask = vi.fn();
    const accepted = { ...item, state: "accepted", task_id: "task-1", allowed_actions: [] };
    fetchMock
      .mockResolvedValueOnce(response({ items: [accepted] }))
      .mockResolvedValueOnce(response({ ...accepted, links: { board_task: "/api/work-board/tasks/task-1" } }));
    render(<GuardianInboxPanel pollIntervalMs={0} onOpenTask={onOpenTask} />);
    fireEvent.click(await screen.findByRole("button", { name: "View evidence and task" }));
    fireEvent.click(await screen.findByRole("link", { name: /Open accepted task task-1/ }));
    expect(onOpenTask).toHaveBeenCalledWith("task-1", expect.objectContaining({ id: "inbox-1", task_id: "task-1", goal_id: "goal-1" }));
  });

  it("can focus the returned task after an accepted disposition", async () => {
    const onOpenTask = vi.fn();
    fetchMock
      .mockResolvedValueOnce(response({ items: [item] }))
      .mockResolvedValueOnce(response({ id: item.id, revision: 4, state: "accepted", task_id: "task-accepted", receipt_id: "receipt-accepted" }));
    render(<GuardianInboxPanel pollIntervalMs={0} autoFocusAcceptedTask onOpenTask={onOpenTask} />);

    fireEvent.click(await screen.findByRole("button", { name: "Accept follow-up" }));
    await waitFor(() => expect(onOpenTask).toHaveBeenCalledWith("task-accepted", expect.objectContaining({ id: "inbox-1", task_id: "task-accepted", state: "accepted" })));
  });

  it("passes backend-shaped verified artifact metadata to the authorized inspector callback", async () => {
    const onInspectArtifact = vi.fn();
    const backendRef = {
      kind: "dossier",
      artifact_type: "guardian_decision_dossier",
      file_path: "guardian/source-watches/watch-1/packets/packet-1.md",
      artifact_id: "artifact:dossier:packet-1",
      sha256: "a".repeat(64),
      status: "verified",
      verification: "cached_readback",
      last_verified_at: "2026-09-30T10:00:00Z",
    };
    fetchMock
      .mockResolvedValueOnce(response({ items: [{ ...item, evidence_refs: [] }] }))
      .mockResolvedValueOnce(response({
        ...item,
        evidence_refs: [backendRef],
        evidence_previews: [{
          artifact_id: backendRef.artifact_id,
          artifact_type: backendRef.artifact_type,
          file_path: backendRef.file_path,
          sha256: backendRef.sha256,
          owner_session_id: "operator-session-1",
          workflow_run_id: "guardian-job-1",
          text: "bounded redacted dossier preview",
          trust: "untrusted_source_evidence",
        }],
        job: {
          id: "guardian-job-1",
          status: "succeeded",
          attempt_count: 1,
          max_attempts: 1,
          readback_id: "guardian_readback:packet-1",
          verified_at: "2026-09-30T10:00:00Z",
          digest: "b".repeat(64),
          readback_status: "verified",
          readbacks: [
            {
              target_path: "guardian/source-watches/watch-1/packets/packet-1.md",
              readback_id: "guardian_readback:packet-1",
              verified_at: "2026-09-30T10:00:00Z",
              digest: "b".repeat(64),
              status: "succeeded",
            },
            {
              target_path: "guardian/source-watches/watch-1/tasks/packet-1.md",
              readback_id: "guardian_readback:task-1",
              verified_at: "2026-09-30T10:01:00Z",
              digest: "c".repeat(64),
              status: "succeeded",
            },
          ],
        },
        action_history: [{
          receipt_id: "receipt-snooze-history",
          action: "snooze",
          created_at: "2026-09-30T10:02:00Z",
          expected_revision: 2,
          result_revision: 3,
          task_id: null,
          outcome: "snoozed",
          reason_state: "provided",
          safe_reason: "review after the next source cycle",
        }],
        action_history_truncated: false,
      }));
    render(<GuardianInboxPanel pollIntervalMs={0} onInspectArtifact={onInspectArtifact} />);
    fireEvent.click(await screen.findByRole("button", { name: "View evidence and task" }));
    expect(await screen.findByText("durable job guardian-job-1 · succeeded")).toBeInTheDocument();
    expect(screen.getByText(/packets\/packet-1\.md · guardian_readback:packet-1 · succeeded/)).toBeInTheDocument();
    expect(screen.getByText(/tasks\/packet-1\.md · guardian_readback:task-1 · succeeded/)).toBeInTheDocument();
    expect(screen.getByText(/snooze · snoozed · receipt receipt-snooze-history/)).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: /Inspect guardian evidence artifact:dossier:packet-1/ }));

    expect(onInspectArtifact).toHaveBeenCalledWith(expect.objectContaining({
      artifact_id: backendRef.artifact_id,
      file_path: backendRef.file_path,
      content_sha256: backendRef.sha256,
      status: "verified",
      verification: "cached_readback",
    }), expect.objectContaining({
      text: "bounded redacted dossier preview",
      owner_session_id: "operator-session-1",
      trust: "untrusted_source_evidence",
    }));
  });

  it("keeps verified detail through a list poll and reloads after its binding changes", async () => {
    const ownerSessionId = "operator-session-1";
    const firstRef = {
      artifact_id: "artifact-dossier-1",
      file_path: "guardian/source-watches/watch-1/packets/packet-1.md",
      sha256: "a".repeat(64),
      owner_session_id: ownerSessionId,
      status: "verified",
    };
    const secondRef = {
      ...firstRef,
      artifact_id: "artifact-dossier-2",
      sha256: "b".repeat(64),
    };
    const firstItem = { ...item, evidence_refs: [firstRef] };
    const firstDetail = {
      ...firstItem,
      evidence_previews: [{
        artifact_id: firstRef.artifact_id,
        file_path: firstRef.file_path,
        sha256: firstRef.sha256,
        owner_session_id: ownerSessionId,
        text: "first verified preview",
      }],
      job: {
        id: "job-first",
        status: "succeeded",
        attempt_count: 1,
        max_attempts: 1,
        readbacks: [{
          target_path: firstRef.file_path,
          readback_id: "readback-first",
          status: "succeeded",
          digest: firstRef.sha256,
        }],
      },
    };
    const secondItem = { ...firstItem, revision: firstItem.revision + 1, evidence_refs: [secondRef] };
    const secondDetail = {
      ...secondItem,
      evidence_previews: [{
        artifact_id: secondRef.artifact_id,
        file_path: secondRef.file_path,
        sha256: secondRef.sha256,
        owner_session_id: ownerSessionId,
        text: "second verified preview",
      }],
      job: {
        id: "job-second",
        status: "succeeded",
        attempt_count: 2,
        max_attempts: 2,
        readbacks: [{
          target_path: secondRef.file_path,
          readback_id: "readback-second",
          status: "succeeded",
          digest: secondRef.sha256,
        }],
      },
    };
    let listCalls = 0;
    let detailCalls = 0;
    let bindingChanged = false;
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/guardian/inbox?") && !url.includes("/api/guardian/inbox/inbox-1")) {
        listCalls += 1;
        const list = bindingChanged ? secondItem : firstItem;
        return Promise.resolve(response({ items: [list], next_cursor: null }));
      }
      if (url.endsWith("/api/guardian/inbox/inbox-1")) {
        detailCalls += 1;
        return Promise.resolve(response(detailCalls === 1 ? firstDetail : secondDetail));
      }
      return Promise.resolve(response({}));
    });

    render(<GuardianInboxPanel pollIntervalMs={25} />);
    fireEvent.click(await screen.findByRole("button", { name: "View evidence and task" }));
    expect(await screen.findByText("durable job job-first · succeeded")).toBeInTheDocument();
    expect(screen.getByText(/readback-first · succeeded/)).toBeInTheDocument();

    await waitFor(() => expect(listCalls).toBeGreaterThanOrEqual(2), { timeout: 500 });
    expect(screen.getByText("durable job job-first · succeeded")).toBeInTheDocument();
    expect(screen.getByText(/readback-first · succeeded/)).toBeInTheDocument();

    bindingChanged = true;
    await waitFor(() => expect(screen.getByText("durable job job-second · succeeded")).toBeInTheDocument(), { timeout: 750 });
    expect(screen.queryByText("durable job job-first · succeeded")).not.toBeInTheDocument();
    expect(screen.getByText(/readback-second · succeeded/)).toBeInTheDocument();
  });

  it("accepts detail-only Mail origin metadata when the safe list omits it", async () => {
    const onSelectItem = vi.fn();
    const detail = {
      ...item,
      mail: {
        watch_id: "watch-1",
        message_binding_id: "message-binding-1",
        message_revision: "sha256:" + "a".repeat(64),
        status: "present",
        private: true,
      },
    };
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/guardian/inbox?") && !url.endsWith("/inbox/inbox-1")) return Promise.resolve(response({ items: [item], next_cursor: null }));
      if (url.endsWith("/api/guardian/inbox/inbox-1")) return Promise.resolve(response(detail));
      return Promise.resolve(response({}));
    });
    render(<GuardianInboxPanel pollIntervalMs={0} onSelectItem={onSelectItem} />);
    fireEvent.click(await screen.findByRole("button", { name: "View evidence and task" }));
    await waitFor(() => expect(onSelectItem.mock.calls.some(([value]) => value?.mail?.message_binding_id === "message-binding-1")).toBe(true));
    expect(onSelectItem.mock.calls[onSelectItem.mock.calls.length - 1]?.[0]).toMatchObject({ mail: { private: true, message_revision: "sha256:" + "a".repeat(64) } });
  });

  it("degrades missing or unknown server state without exposing actions", async () => {
    fetchMock.mockResolvedValueOnce(response({
      items: [
        { ...item, id: "inbox-unknown", state: "future_state", allowed_actions: ["accept_followup", "dismiss"] },
        { ...item, id: "inbox-missing", state: undefined, allowed_actions: ["accept_followup", "dismiss"] },
      ],
    }));
    render(<GuardianInboxPanel pollIntervalMs={0} />);

    expect((await screen.findAllByText("degraded · server state is not recognized; actions are unavailable")).length).toBe(2);
    expect(screen.queryByRole("button", { name: "Accept follow-up" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Dismiss" })).not.toBeInTheDocument();
  });

  it("accepts deferred results after the StrictMode effect replay", async () => {
    const pending: Array<(value: unknown) => void> = [];
    fetchMock.mockImplementation(() => new Promise((resolve) => pending.push(resolve)));
    render(
      <StrictMode>
        <GuardianInboxPanel pollIntervalMs={0} />
      </StrictMode>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    await act(async () => {
      pending[1](response({ items: [item] }));
    });
    expect(await screen.findByText("Watched source changed · pending")).toBeInTheDocument();
    await act(async () => {
      pending[0](response({ items: [item] }));
    });
  });

  it("keeps inspector actions inside the M1 payload owner", async () => {
    fetchMock
      .mockResolvedValueOnce(response({ items: [item] }))
      .mockResolvedValueOnce(response({ id: item.id, revision: 4, state: "accepted", task_id: "task-1", receipt_id: "receipt-bridge" }));
    const ref = createRef<GuardianInboxPanelHandle>();
    render(<GuardianInboxPanel ref={ref} pollIntervalMs={0} />);
    await screen.findByText("Watched source changed · pending");
    act(() => ref.current?.runAction(item.id, "accept_followup"));
    await waitFor(() => expect(screen.getByText("Accept follow-up recorded.")).toBeInTheDocument());
    const request = JSON.parse(String((fetchMock.mock.calls[1][1] as RequestInit).body));
    expect(request).toEqual({
      action: "accept_followup",
      expected_revision: 3,
      idempotency_key: expect.any(String),
    });
  });
});
