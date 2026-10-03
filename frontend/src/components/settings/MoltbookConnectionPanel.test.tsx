import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MoltbookConnectionPanel } from "./MoltbookConnectionPanel";
import { moltbookStorageKey, readMoltbookPending } from "../../lib/moltbook";

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
});
