import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MoltbookConnectionPanel } from "./MoltbookConnectionPanel";
import { moltbookStorageKey, readMoltbookPending, retainMoltbookPending, pendingApplied } from "../../lib/moltbook";
import type { MoltbookJob } from "../../lib/moltbook";

const response = (value: unknown) => Promise.resolve(new Response(JSON.stringify(value), { status: 200 }));
const connection = { configured: true, id: "connection-one", revision: 2, mode: "pending_claim", account_name: "",
  active_job_id: null, credential_is_consent: false, no_learning: true,
  consent: { actions: ["feed"], goal_id: "goal-one", goal_revision: 1, session: "root-one" } };
const jobId = "moltbook:" + "a".repeat(40);
afterEach(() => { vi.unstubAllGlobals(); sessionStorage.clear(); });
function metadata(url: string) {
  if (url.endsWith("/vault/keys")) return [{ key: "private-import" }];
  if (url.endsWith("/goals/tree")) return [{ id: "goal-one", title: "Personal feedback", status: "active", revision: 1 }];
  return connection;
}

describe("fixed Moltbook owner controls", () => {
  it("loads local metadata without a remote inspection or implicit consent", async () => {
    const fetch = vi.fn((url: unknown) => response(metadata(String(url))));
    vi.stubGlobal("fetch", fetch);
    render(<MoltbookConnectionPanel ownerPrincipalId="owner-one" ownerSessionId="root-one" />);
    await screen.findByText(/pending claim/);
    expect(fetch.mock.calls.every(([url]) => !String(url).includes("moltbook.com"))).toBe(true);
    expect(screen.getByText("Allow finite reads for this login and Goal").hasAttribute("disabled")).toBe(true);
    expect((screen.getByLabelText(/I authorize personal/) as HTMLInputElement).checked).toBe(false);
  });
  it("retains the full exact admitted request across a lost response and reload", async () => {
    const bodies: string[] = [];
    const fetch = vi.fn((url: unknown, init?: RequestInit) => {
      if (String(url).endsWith("/reads") && init?.method === "POST") {
        bodies.push(String(init.body));
        if (bodies.length === 1) return Promise.reject(Error("Response lost after test HTTP acceptance"));
        const body = JSON.parse(String(init.body));
        return response({ job_id: jobId, status: "accepted", revision: 0, deadline_at: "2026-10-03T20:00:00Z", attempt_count: 0,
          idempotency: { key: body.request_key }, no_learning: true, checkpoints: [] });
      }
      return response(metadata(String(url)));
    });
    vi.stubGlobal("fetch", fetch);
    const view = render(<MoltbookConnectionPanel ownerPrincipalId="owner-one" ownerSessionId="root-one" />);
    await screen.findByText(/pending claim/);
    fireEvent.click(screen.getByText("Prepare bounded read"));
    await screen.findByText("Retry exact retained request");
    const key = moltbookStorageKey("owner-one", "root-one");
    expect(readMoltbookPending(key)?.body.request_key).toBeTruthy();
    view.unmount();
    render(<MoltbookConnectionPanel ownerPrincipalId="owner-one" ownerSessionId="root-one" />);
    await screen.findByText("Retry exact retained request");
    fireEvent.click(screen.getByText("Retry exact retained request"));
    await screen.findByLabelText("Moltbook original job");
    expect(bodies).toHaveLength(2);
    expect(bodies[0]).toBe(bodies[1]);
    expect(readMoltbookPending(key)).toBeNull();
  });
  it("refuses mutation if exact request storage is unavailable", async () => {
    const fetch = vi.fn((url: unknown) => response(metadata(String(url))));
    vi.stubGlobal("fetch", fetch);
    render(<MoltbookConnectionPanel ownerPrincipalId="owner-one" ownerSessionId="root-one" />);
    await screen.findByText(/pending claim/);
    const spy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw Error("Storage unavailable"); });
    fireEvent.click(screen.getByText("Prepare bounded read"));
    await screen.findByRole("alert");
    expect(fetch.mock.calls.some(([url]) => String(url).endsWith("/reads"))).toBe(false);
    spy.mockRestore();
  });
  it("resets busy on login scope change and preserves the original scoped request", async () => {
    let release: ((value: Response) => void) | undefined;
    const fetch = vi.fn((url: unknown, init?: RequestInit) => {
      if (String(url).endsWith("/reads") && init?.method === "POST") return new Promise<Response>(resolve => { release = resolve; });
      return response(metadata(String(url)));
    });
    vi.stubGlobal("fetch", fetch);
    const view = render(<MoltbookConnectionPanel ownerPrincipalId="owner-one" ownerSessionId="root-one" />);
    await screen.findByText(/pending claim/);
    fireEvent.click(screen.getByText("Prepare bounded read"));
    await waitFor(() => expect(release).toBeDefined());
    view.rerender(<MoltbookConnectionPanel ownerPrincipalId="owner-one" ownerSessionId="root-two" />);
    await waitFor(() => expect(screen.getByText("Refresh local metadata and original job").hasAttribute("disabled")).toBe(false));
    expect(readMoltbookPending(moltbookStorageKey("owner-one", "root-one"))).not.toBeNull();
    expect(readMoltbookPending(moltbookStorageKey("owner-one", "root-two"))).toBeNull();
    release?.(new Response(JSON.stringify({ job_id: jobId, status: "accepted" }), { status: 200 }));
    await waitFor(() => expect(screen.queryByLabelText("Moltbook original job")).toBeNull());
  });
  it("reconciles only the exact applied execute request after reload and renders the manual challenge literally", async () => {
    const key = moltbookStorageKey("owner-one", "root-one");
    const pending = { method: "POST" as const, path: `/jobs/${jobId}/execute`,
      body: { request_key: "execute-one", expected_phase: "awaiting_create_approval", fencing_token: 1 } };
    retainMoltbookPending(key, pending);
    const applied = { job_id: jobId, status: "paused", revision: 9, deadline_at: "2026-10-03T20:00:00Z", attempt_count: 1,
      lease: { fencing_token: 2 }, no_learning: true, checkpoints: [{ checkpoint_id: "moltbook:state", payload: {
        phase: "awaiting_manual_answer", content_id: "content-one", challenge_text: "<script>stealKey()</script>",
        challenge_expires_at: "2026-10-03T20:00:00Z",
        executions: [{ request_key: "execute-one", phase: "awaiting_create_approval", fencing_token: 1 }] } }] };
    const fetch = vi.fn((url: unknown, _init?: RequestInit) => response(String(url).endsWith(`/jobs/${jobId}`) ? applied : metadata(String(url))));
    vi.stubGlobal("fetch", fetch);
    render(<MoltbookConnectionPanel ownerPrincipalId="owner-one" ownerSessionId="root-one" />);
    await screen.findByText("Retry exact retained request");
    fireEvent.click(screen.getByText("Refresh local metadata and original job"));
    await screen.findByText("<script>stealKey()</script>");
    await waitFor(() => expect(readMoltbookPending(key)).toBeNull());
    expect(document.querySelector("script")).toBeNull();
    expect(fetch.mock.calls.every(([, init]) => !(init as RequestInit | undefined)?.method || (init as RequestInit).method === "GET")).toBe(true);
  });
  it("keeps a wrong original attempt/phase retained instead of guessing from paused status", async () => {
    const key = moltbookStorageKey("owner-one", "root-one");
    const pending = { method: "POST" as const, path: `/jobs/${jobId}/execute`,
      body: { request_key: "execute-one", expected_phase: "awaiting_create_approval", fencing_token: 1 } };
    retainMoltbookPending(key, pending);
    const wrong = { job_id: jobId, status: "paused", revision: 9, deadline_at: "2026-10-03T20:00:00Z", attempt_count: 1,
      lease: { fencing_token: 3 }, no_learning: true, checkpoints: [{ checkpoint_id: "moltbook:state", payload: {
        phase: "awaiting_verify_approval", executions: [{ request_key: "execute-one", phase: "awaiting_verify_approval", fencing_token: 2 }] } }] };
    vi.stubGlobal("fetch", vi.fn((url: unknown) => response(String(url).endsWith(`/jobs/${jobId}`) ? wrong : metadata(String(url)))));
    render(<MoltbookConnectionPanel ownerPrincipalId="owner-one" ownerSessionId="root-one" />);
    await screen.findByText("Retry exact retained request");
    fireEvent.click(screen.getByText("Refresh local metadata and original job"));
    await screen.findByLabelText("Moltbook original job");
    expect(readMoltbookPending(key)).toEqual(pending);
  });
  it("reconciles cancellation only for the original job revision and fence", () => {
    const pending = { method: "POST" as const, path: `/jobs/${jobId}/cancel`, body: { request_key: "cancel-one", expected_revision: 4, fencing_token: 1 } };
    const job: MoltbookJob = { job_id: jobId, status: "unknown_external_effect", revision: 5, deadline_at: "2026-10-03T20:00:00Z", attempt_count: 1,
      no_learning: true, lease: { fencing_token: 1 }, checkpoints: [{ checkpoint_id: "moltbook:state", payload: {
      cancel_request: { request_key: "cancel-one", original_revision: 4, fencing_token: 1, cleanup: "pending" } } }] } as MoltbookJob;
    expect(pendingApplied(job, pending)).toBe(true);
    expect(pendingApplied({ ...job, job_id: "moltbook:"+"b".repeat(40) }, pending)).toBe(false);
    expect(pendingApplied(job, { ...pending, body: { ...pending.body, fencing_token: 2 } })).toBe(false);
  });
  it("inspects a completed original job after reload only in its original owner and login scope", async () => {
    const key = moltbookStorageKey("owner-one", "root-one");
    sessionStorage.setItem(key.replace("seraph.moltbook.v1:", "seraph.moltbook.job.v1:"), jobId);
    const fetch = vi.fn((url: unknown) => response(String(url).endsWith(`/jobs/${jobId}/output`)
      ? { no_learning: true, data: "<script>quoted only</script>" }
      : String(url).endsWith(`/jobs/${jobId}`) ? { job_id: jobId, status: "succeeded", no_learning: true,
        lease: { fencing_token: 3 }, attempt_count: 1,
        declared_authority: { operation: "create_post", account_name: "FixtureSeraph" },
        draft: { operation: "create_post", fields: { community: "introductions", title: "Exact public title", content: "<script>quoted only</script>" }, review: { community: "introductions", finished_at: "2026-10-03T20:00:00Z", private_binding: "diagnostic-only-digest" } },
        checkpoints: [{ checkpoint_id: "moltbook:state", payload: { phase: "published_verified" } }] } : metadata(String(url))));
    vi.stubGlobal("fetch", fetch);
    const view = render(<MoltbookConnectionPanel ownerPrincipalId="owner-one" ownerSessionId="root-one" />);
    await screen.findByText(/pending claim/);
    expect(fetch.mock.calls.some(([url]) => String(url).includes(`/jobs/${jobId}`))).toBe(false);
    fireEvent.click(screen.getByText("Refresh local metadata and original job"));
    await screen.findByLabelText("Moltbook literal private output");
    expect(document.querySelector("script")).toBeNull();
    expect(screen.getByLabelText("Canonical Moltbook public text").textContent).toBe("<script>quoted only</script>");
    expect(screen.getByText("Original binding diagnostics").closest("details")?.hasAttribute("open")).toBe(false);
    expect(screen.getByText("Title: Exact public title")).toBeInTheDocument();

    view.rerender(<MoltbookConnectionPanel ownerPrincipalId="owner-one" ownerSessionId="root-two" />);
    await waitFor(() => expect(screen.queryByLabelText("Moltbook original job")).toBeNull());
    const before = fetch.mock.calls.length;
    fireEvent.click(screen.getByText("Refresh local metadata and original job"));
    await waitFor(() => expect(fetch.mock.calls.length).toBeGreaterThan(before));
    expect(fetch.mock.calls.slice(before).some(([url]) => String(url).includes(`/jobs/${jobId}`))).toBe(false);
  });
});
