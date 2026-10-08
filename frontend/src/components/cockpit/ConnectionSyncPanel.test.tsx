import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { apiFetch } from "../../lib/api";
import { ConnectionSyncPanel } from "./ConnectionSyncPanel";
import { privateSyncItem, syncProjection } from "../../lib/connectionSync";
vi.mock("../../lib/api", () => ({ apiFetch: vi.fn() }));
const future = new Date(Date.now() + 86400000).toISOString();
const sha = "a".repeat(64), item = { provider: "gmail" as const, opaque_id: sha, revision: sha, content_digest: sha, privacy: "owner_private" as const, expires_at: future };
const props = { provider: "gmail" as const, ownerPrincipalId: "owner", ownerSessionId: "session", connectionId: "connection", connectionRevision: 2, connectionState: "active", labelIds: ["label-opaque"],
  consent: { id: "grant", revision: 3, goalId: "goal", goalRevision: 4, state: "active", expiresAt: future, metadataLimit: 50, privateLimit: 10 } };
const state = { connection_id: "connection", state: "ready", active_job_id: null, active_job_revision: null, cursor_revision: 1, scope_digest: sha,
  items: [item], coverage: { pages_read: 1, returned: 1, max_items: 50, partial: true, more_available: true }, freshness: { last_complete_at: new Date().toISOString(), expires_at: future }, recovery_action: null };
const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
beforeEach(() => { vi.mocked(apiFetch).mockReset(); });
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
it("holds unknown contact without replay and reconciles only exact original job revision", async () => {
  const unknown = { ...state, state: "unknown_external_effect", active_job_id: "old-job", active_job_revision: 7, recovery_action: "reconcile_existing_sync" };
  vi.mocked(apiFetch).mockResolvedValueOnce(response(unknown)).mockResolvedValueOnce(response({ job_id: "old-job", status: "failed", provider_contacts: 0 })).mockResolvedValueOnce(response(state));
  render(<ConnectionSyncPanel {...props} />); await screen.findByText(/Existing sync old-job/);
  expect(screen.getByRole("button", { name: "Sync selected context" })).toBeDisabled();
  fireEvent.click(screen.getByText(/I acknowledge reconciliation/)); fireEvent.click(screen.getByRole("button", { name: "Reconcile existing read-only sync" }));
  await waitFor(() => expect(apiFetch).toHaveBeenCalledTimes(3));
  expect(String(vi.mocked(apiFetch).mock.calls[1][0])).toContain("/sync/old-job/reconcile");
  expect(JSON.parse(String(vi.mocked(apiFetch).mock.calls[1][1]?.body))).toEqual({ expected_job_revision: 7, acknowledge_read_only_reconciliation: true });
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
  vi.mocked(apiFetch).mockResolvedValue(response({ ...state, state: "waiting", active_job_id: "rate-job", active_job_revision: 9,
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
