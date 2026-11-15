import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { apiFetch } from "../../lib/api";
import { ConnectionSyncPanel, RelatedSourcesReview } from "./ConnectionSyncPanel";
import { connectedTaskInput, connectedSources, normalizeConnectedRequest, privateSyncItem, relatedSources, syncProjection } from "../../lib/connectionSync";
vi.mock("../../lib/api", () => ({ apiFetch: vi.fn() }));
const future = new Date(Date.now() + 86400000).toISOString();
const sha = "a".repeat(64), item = { provider: "gmail" as const, opaque_id: sha, revision: sha, content_digest: sha, privacy: "owner_private" as const, expires_at: future };
const props = { provider: "gmail" as const, ownerPrincipalId: "owner", ownerSessionId: "session", connectionId: "connection", connectionRevision: 2, connectionState: "active", labelIds: ["label-opaque"],
  consent: { id: "grant", revision: 3, goalId: "goal", goalRevision: 4, state: "active", expiresAt: future, metadataLimit: 50, privateLimit: 10 } };
const state = { connection_id: "connection", state: "ready", active_job_id: null, active_job_revision: null, cursor_revision: 1, scope_digest: sha,
  reservation_state: "available", external_effect_state: "none", unresolved_jobs: [],
  items: [item], coverage: { pages_read: 1, returned: 1, max_items: 50, partial: true, more_available: true }, freshness: { last_complete_at: new Date().toISOString(), expires_at: future }, recovery_action: null };
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
beforeEach(() => { vi.mocked(apiFetch).mockReset(); });
it("selects exact local task refs only by separate ack and clears them on Goal or revision change", async () => {
  const change = vi.fn(), selection = { goal_ref: { id: "goal", revision: 4 }, connection_ref: { id: "connection", revision: 2 }, source_scope: { provider: "gmail", consents: [{ id: "grant", revision: 3 }], label_ids: ["label-opaque"], thread_keys: [] }, window: { start: new Date().toISOString(), end: future }, max_items: 50 };
  vi.mocked(apiFetch).mockImplementation(async () => response({ ...state, selection }));
  const mounted = render(<ConnectionSyncPanel {...props} relatedGoal={{ id: "goal", revision: 4 }} onRelatedChange={change} />);
  const useRef = await screen.findByLabelText(/Use local reference/); expect(useRef).not.toBeChecked();
  fireEvent.click(useRef);
  await waitFor(() => expect(change.mock.lastCall?.[0]?.acknowledged).toBe(false));
  expect(() => connectedTaskInput(change.mock.lastCall?.[0])).toThrow(/Acknowledge/);
  fireEvent.click(screen.getByLabelText(/I acknowledge these exact references/));
  await waitFor(() => expect(change.mock.lastCall?.[0]?.acknowledged).toBe(true));
  expect(connectedTaskInput(change.mock.lastCall?.[0])).toEqual({ connected_sources: [{ connection_ref: { id: "connection", revision: 2 }, item_refs: [item] }], acknowledge_connected_sources: true });
  expect(apiFetch).toHaveBeenCalledTimes(1);
  mounted.rerender(<ConnectionSyncPanel {...props} relatedGoal={{ id: "goal", revision: 5 }} onRelatedChange={change} />);
  await waitFor(() => expect(change.mock.lastCall?.[0]).toBeNull());
  expect(await screen.findByLabelText(/Use local reference/)).not.toBeChecked();
});
it("bounds local refs, rejects duplicates and expiry, and omits empty legacy fields", () => {
  const group = { connection_ref: { id: "connection", revision: 2 }, item_refs: [item] };
  expect(() => connectedSources([{ ...group, item_refs: [item, item] }])).toThrow(/unique/);
  expect(() => connectedSources([{ ...group, item_refs: Array.from({ length: 11 }, (_, n) => ({ ...item, opaque_id: String(n) })) }])).toThrow(/ten unique/);
  expect(() => connectedSources(Array.from({ length: 4 }, (_, n) => ({ ...group, connection_ref: { id: String(n), revision: 1 } })))).toThrow(/limit/);
  expect(() => connectedTaskInput({ sources: [{ ...group, item_refs: [{ ...item, expires_at: "2020-01-01T00:00:00Z" }] }], acknowledged: true })).toThrow(/expired/);
  expect(normalizeConnectedRequest({ connected_sources: [], acknowledge_connected_sources: true })).toEqual({});
});
it("opens task related cache only by explicit ack and fences late private readback on owner change", async () => {
  const related = relatedSources({ classification: "local_related_context_not_model_input", memory_status: "no_learning", sources: [{ connection_ref: { id: "connection", revision: 2 }, item_refs: [item], coverage: state.coverage, freshness: state.freshness }] });
  let finish!: (v: Response) => void;
  vi.mocked(apiFetch).mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  const mounted = render(<RelatedSourcesReview related={related} ownerPrincipalId="owner" ownerSessionId="session" />);
  expect(screen.getByLabelText("Task local related references")).toHaveTextContent("model did not use");
  expect(apiFetch).not.toHaveBeenCalled();
  fireEvent.click(screen.getByLabelText(/I acknowledge opening/)); fireEvent.click(screen.getByRole("button", { name: /Open related private item/ }));
  await waitFor(() => expect(apiFetch).toHaveBeenCalledTimes(1));
  expect(String(vi.mocked(apiFetch).mock.calls[0][0])).toContain(`/sync/items/${sha}`);
  mounted.rerender(<RelatedSourcesReview related={related} ownerPrincipalId="owner" ownerSessionId="new-session" />);
  finish(response({ item: { ref: item, content: { body: "late private body" } }, memory_status: "no_learning" }));
  await waitFor(() => expect(screen.queryByLabelText("Private related cache readback")).not.toBeInTheDocument());
  expect(document.body.textContent).not.toContain("late private body");
});
it("renders literal related private cache readback only after the exact explicit local read", async () => {
  const related = relatedSources({ classification: "local_related_context_not_model_input", memory_status: "no_learning", sources: [{ connection_ref: { id: "connection", revision: 2 }, item_refs: [item], coverage: state.coverage, freshness: state.freshness, content: "must not become a task result" }] });
  expect(JSON.stringify(related)).not.toContain("must not become");
  vi.mocked(apiFetch).mockResolvedValueOnce(response({ item: { ref: item, content: { body: "<script>literal private context</script>", refresh_token: "not rendered" } }, memory_status: "no_learning" }));
  render(<RelatedSourcesReview related={related} ownerPrincipalId="owner" ownerSessionId="session" />);
  expect(apiFetch).not.toHaveBeenCalled();
  fireEvent.click(screen.getByLabelText(/I acknowledge opening/)); fireEvent.click(screen.getByRole("button", { name: /Open related private item/ }));
  expect(await screen.findByLabelText("Private related cache readback")).toHaveTextContent("<script>literal private context</script>");
  expect(document.querySelector("script")).toBeNull(); expect(document.body.textContent).not.toContain("not rendered");
  expect(JSON.parse(String(vi.mocked(apiFetch).mock.calls[0][1]?.body))).toEqual({ acknowledge_private_read: true });
  expect(apiFetch).toHaveBeenCalledTimes(1);
});
it("loads redacted scope coverage without contacting the provider and syncs only exact acknowledged grants", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response(state)).mockResolvedValueOnce(response({ ...state, state: undefined, job_id: "job", status: "succeeded", replayed: false, memory_status: "no_learning" })).mockResolvedValueOnce(response(state));
  render(<ConnectionSyncPanel {...props} />);
  await screen.findByText(/Coverage: partial/);
  expect(apiFetch).toHaveBeenCalledTimes(1); expect(vi.mocked(apiFetch).mock.calls[0][1]?.method).toBeUndefined();
  fireEvent.click(screen.getByText(/I acknowledge this exact window/));
  fireEvent.click(screen.getByRole("button", { name: "Sync selected context" }));
  await waitFor(() => expect(apiFetch).toHaveBeenCalledTimes(3));
  const body = JSON.parse(String(vi.mocked(apiFetch).mock.calls[1][1]?.body));
  expect(body.input.goal_ref).toEqual({ id: "goal", revision: 4 }); expect(body.input.connection_ref).toEqual({ id: "connection", revision: 2 });
  expect(body.input.source_scope).toEqual({ provider: "gmail", consents: [{ id: "grant", revision: 3 }], label_ids: ["label-opaque"], thread_keys: [], selected_private_items: [], acknowledge_private_read: false, reset_cursor: false });
  expect(body.input.max_items).toBe(50); expect(body.request_uuid).toBeTruthy();
});
it("private read is explicit, exact revision-bound and literal with no generic secret fields", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response(state)).mockResolvedValueOnce(response({ item: { ref: item, content: { subject: "<script>private</script>", body: "private body", refresh_token: "secret", provider_id: "raw" } }, memory_status: "no_learning" }));
  render(<ConnectionSyncPanel {...props} />); await screen.findByText(/Coverage: partial/);
  const button = screen.getByRole("button", { name: `Read private item ${sha}` }); expect(button).toBeDisabled();
  fireEvent.click(screen.getByText(/I acknowledge reading one exact/)); fireEvent.click(button);
  expect(await screen.findByLabelText("Private synchronized source")).toHaveTextContent("<script>private</script>");
  expect(document.body.textContent).not.toContain("secret"); expect(document.querySelector("script")).toBeNull();
  expect(JSON.parse(String(vi.mocked(apiFetch).mock.calls[1][1]?.body))).toEqual({ acknowledge_private_read: true });
});
it("releases exact physical capacity while retaining running unknown contact history without replay", async () => {
  const history = [{ job_id: "old-job", revision: 7, status: "running", external_effect_state: "unknown", failure_reason: null }];
  const unknown = { ...state, state: "blocked", reservation_state: "held", external_effect_state: "unknown", unresolved_jobs: history, active_job_id: "old-job", active_job_revision: 7, recovery_action: "release_physical_slot" };
  vi.mocked(apiFetch).mockResolvedValueOnce(response(unknown)).mockResolvedValueOnce(response({ job_id: "old-job", status: "running", physical_slot_released: true, provider_contacts: 0, memory_status: "no_learning" })).mockResolvedValueOnce(response({ ...state, unresolved_jobs: history }));
  render(<ConnectionSyncPanel {...props} />); await screen.findByText(/Existing sync old-job/);
  expect(screen.getByRole("button", { name: "Sync selected context" })).toBeDisabled();
  fireEvent.click(screen.getByText(/I acknowledge physical slot release/)); fireEvent.click(screen.getByRole("button", { name: "Release physical sync slot" }));
  await waitFor(() => expect(apiFetch).toHaveBeenCalledTimes(3));
  expect(String(vi.mocked(apiFetch).mock.calls[1][0])).toContain("/sync/old-job/reconcile");
  expect(JSON.parse(String(vi.mocked(apiFetch).mock.calls[1][1]?.body))).toEqual({ expected_job_revision: 7, expected_cursor_revision: 1, acknowledge_physical_slot_release: true });
  expect(screen.getByLabelText("Unresolved sync contact")).toHaveTextContent("stored status: running · external effect: unknown");
  expect(screen.getByText(/Physical reservation: available/)).toHaveTextContent("active external effect: none");
  expect(screen.queryByText(/Existing sync old-job/)).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Sync selected context" })).toBeDisabled();
  expect(screen.queryByLabelText("Private synchronized source")).not.toBeInTheDocument();
});
it("preserves usable state but marks failed refresh stale and blocks private read/new contacts", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response(state)).mockResolvedValueOnce(response({ detail: { code: "connection_sync_scope_missing" } }, 403));
  render(<ConnectionSyncPanel {...props} />); await screen.findByText(/Coverage: partial/);
  fireEvent.click(screen.getByRole("button", { name: /Refresh sync state/ }));
  expect(await screen.findByRole("alert")).toHaveTextContent("connection_sync_scope_missing");
  expect(screen.getByText(/Stale, unconfirmed state/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Sync selected context" })).toBeDisabled();
});
it("requires a new explicit decision after uncertain sync and never auto retries", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response(state)).mockRejectedValueOnce(Error("lost response"));
  render(<ConnectionSyncPanel {...props} />); await screen.findByText(/Coverage: partial/);
  fireEvent.click(screen.getByText(/I acknowledge this exact window/)); fireEvent.click(screen.getByRole("button", { name: "Sync selected context" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("no request was automatically replayed");
  expect(apiFetch).toHaveBeenCalledTimes(2); expect(screen.getByRole("button", { name: "Sync selected context" })).toBeDisabled();
});
it("expired or legacy grants cannot start sync", async () => {
  vi.mocked(apiFetch).mockResolvedValue(response(state));
  render(<ConnectionSyncPanel {...props} consent={{ ...props.consent, metadataLimit: 0 }} />);
  await screen.findByText(/Coverage: partial/); expect(screen.getByRole("button", { name: "Sync selected context" })).toBeDisabled();
});
it.each(["expired", "revoked"])("blocks a %s source grant while keeping refresh usable", async stateName => {
  vi.mocked(apiFetch).mockResolvedValue(response(state));
  render(<ConnectionSyncPanel {...props} consent={{ ...props.consent, state: stateName }} />);
  await screen.findByText(/Coverage: partial/);
  expect(screen.getByRole("button", { name: "Sync selected context" })).toBeDisabled();
  expect(screen.getByRole("button", { name: /Refresh sync state/ })).toBeEnabled();
});
it("shows a bounded cooldown without admitting another provider read", async () => {
  vi.mocked(apiFetch).mockResolvedValue(response({ ...state, state: "waiting", reservation_state: "held", active_job_id: "rate-job", active_job_revision: 9,
    cooldown: { retry_at: future, retry_count: 1, original_deadline: future }, last_error_code: "connection_sync_rate_limited" }));
  render(<ConnectionSyncPanel {...props} />);
  await screen.findByText(/Rate limit: bounded retry/);
  expect(screen.getByText(/Last source error: connection_sync_rate_limited/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Sync selected context" })).toBeDisabled();
  expect(apiFetch).toHaveBeenCalledTimes(1);
});
it("fences late private content when owner or scope changes", async () => {
  let resolve!: (v: Response) => void;
  vi.mocked(apiFetch).mockResolvedValueOnce(response(state)).mockImplementationOnce(() => new Promise(r => { resolve = r; })).mockResolvedValue(response(state));
  const mounted = render(<ConnectionSyncPanel {...props} />); await screen.findByText(/Coverage: partial/);
  fireEvent.click(screen.getByText(/I acknowledge reading one exact/)); fireEvent.click(screen.getByRole("button", { name: `Read private item ${sha}` }));
  await waitFor(() => expect(typeof resolve).toBe("function"));
  mounted.rerender(<ConnectionSyncPanel {...props} ownerSessionId="new-owner" />);
  resolve(response({ item: { ref: item, content: { body: "late private body" } }, memory_status: "no_learning" }));
  await waitFor(() => expect(screen.queryByLabelText("Private synchronized source")).toBeNull()); expect(document.body.textContent).not.toContain("late private body");
});
it("rejects wrong connection and stale private revisions", () => {
  expect(() => syncProjection({ ...state, connection_id: "foreign" }, "gmail", "connection")).toThrow();
  expect(() => privateSyncItem({ item: { ref: { ...item, revision: "b".repeat(64) }, content: {} }, memory_status: "no_learning" }, item)).toThrow(/revision changed/);
});
it.each(["none", "settled"])("keeps the %s ledger state distinct from unknown", async external_effect_state => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response({ ...state, external_effect_state }));
  render(<ConnectionSyncPanel {...props} />);
  expect(await screen.findByText(/Physical reservation: available/)).toHaveTextContent(`active external effect: ${external_effect_state}`);
  expect(screen.queryByLabelText("Unresolved sync contact")).not.toBeInTheDocument();
});
it("allows explicit physical cleanup with a revoked grant but blocks new work and preserves callback-active uncertainty", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response({ ...state, state: "revoked", reservation_state: "held", external_effect_state: "unknown", active_job_id: "old-job", active_job_revision: 7, recovery_action: "release_physical_slot",
    unresolved_jobs: [{ job_id: "old-job", revision: 7, status: "running", external_effect_state: "unknown", failure_reason: null }] }))
    .mockResolvedValueOnce(response({ detail: { code: "connection_sync_callback_active" } }, 409));
  render(<ConnectionSyncPanel {...props} consent={{ ...props.consent, state: "revoked" }} />);
  fireEvent.click(await screen.findByText(/I acknowledge physical slot release/));
  fireEvent.click(screen.getByRole("button", { name: "Release physical sync slot" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("connection_sync_callback_active");
  expect(screen.getByLabelText("Unresolved sync contact")).toHaveTextContent("stored status: running");
  expect(screen.getByRole("button", { name: "Sync selected context" })).toBeDisabled();
  expect(apiFetch).toHaveBeenCalledTimes(2);
});
it("fails closed on missing ledger metadata and malformed unresolved history", () => {
  expect(() => syncProjection({ ...state, reservation_state: undefined }, "gmail", "connection")).toThrow(/reservation/);
  expect(() => syncProjection({ ...state, unresolved_jobs: [{ job_id: "old", revision: 1, status: "running", external_effect_state: "settled", failure_reason: null }] }, "gmail", "connection")).toThrow(/history/);
});
it("requires existing explicit operator enrollment without making an enrollment or provider call", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response({ detail: { code: "source_sync_operator_continuity_required", recovery_action: "enroll_operator_ownership" } }, 403));
  render(<ConnectionSyncPanel {...props} />);
  expect(await screen.findByText(/Open “Operator ownership and recovery”/)).toBeInTheDocument();
  expect(apiFetch).toHaveBeenCalledTimes(1);
});
it("recovers original calendar scope from authoritative status after reload without restoring acknowledgements", async () => {
  const selection = { goal_ref: { id: "original-goal", revision: 6 }, connection_ref: { id: "connection", revision: 2 },
    source_scope: { provider: "calendar", consents: [{ id: "original-grant", revision: 8 }], label_ids: [], thread_keys: [], selected_private_items: [], acknowledge_private_read: false, reset_cursor: false },
    window: { start: new Date().toISOString(), end: future }, max_items: 30 };
  const calendarState = { ...state, items: [], selection };
  vi.mocked(apiFetch).mockResolvedValueOnce(response(calendarState)).mockResolvedValueOnce(response({ ...calendarState, state: undefined, status: "succeeded", job_id: "new-job", replayed: false, memory_status: "no_learning" })).mockResolvedValueOnce(response(calendarState));
  render(<ConnectionSyncPanel {...props} provider="calendar" inspectionOnly labelIds={[]} consent={{ ...props.consent, metadataLimit: 0, state: "unavailable" }} />);
  fireEvent.click(await screen.findByRole("button", { name: "Review original synchronized scope" }));
  expect(screen.getByLabelText("Sync window start")).toHaveValue(selection.window.start);
  expect(screen.getByRole("button", { name: "Sync selected context" })).toBeDisabled();
  fireEvent.click(screen.getByText(/I acknowledge this exact window/)); fireEvent.click(screen.getByRole("button", { name: "Sync selected context" }));
  await waitFor(() => expect(apiFetch).toHaveBeenCalledTimes(3));
  const input = JSON.parse(String(vi.mocked(apiFetch).mock.calls[1][1]?.body)).input;
  expect(input.goal_ref).toEqual(selection.goal_ref); expect(input.source_scope.consents).toEqual(selection.source_scope.consents);
  expect(input.source_scope.acknowledge_private_read).toBe(false); expect(input.source_scope.reset_cursor).toBe(false);
});
