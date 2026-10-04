import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ForgejoTitlePanel } from "./ForgejoTitlePanel";

const id = "forgejo:" + "a".repeat(40);
const scope = "seraph.forgejo.owner.root";
const connection = { configured: true, connection_id: "fixed", revision: 1, state: "active",
  provider_user_id: 1, provider_login: "fixture", read_consent_revision: 1,
  read_consent_expires_at: new Date(Date.now() + 600000).toISOString(), available: true, no_learning: true };
const job = { job_id: id, revision: 12, status: "unknown_external_effect", deadline_at: "2026-10-04T01:00:00Z",
  goal_id: "goal", owner_principal_id: "owner", operator_session_id: "root", lease: { fencing_token: 1 },
  declared_authority: { operation: "title" }, approval: null, forgejo: { phase: "unknown", capacity_closed: false } };
const response = (value: unknown) => Promise.resolve(new Response(JSON.stringify(value), { status: 200 }));
function metadata(url: unknown) {
  const path = String(url);
  if (path.endsWith("/goals/tree")) return [{ id: "goal", revision: 1, title: "Finite title", status: "active" }];
  if (path.endsWith("/vault/keys")) return [{ key: "owned-input" }];
  if (path.includes("/jobs/")) return job;
  return connection;
}
afterEach(() => { vi.unstubAllGlobals(); sessionStorage.clear(); });

describe("fixed Forgejo title controls", () => {
  it("reloads Unknown with GET only, discloses production/race/reservation and requires fresh separate acknowledgment", async () => {
    sessionStorage.setItem(scope + ".job", id);
    const fetch = vi.fn((url: unknown, _init?: RequestInit) => response(metadata(url))); vi.stubGlobal("fetch", fetch);
    render(<ForgejoTitlePanel ownerPrincipalId="owner" ownerSessionId="root" />);
    await screen.findByText(/Original title effect is Unknown/);
    expect(screen.getByText(/Production Codeberg execution is blocked/)).toBeTruthy();
    expect(screen.getByText(/provider has no atomic revision check/)).toBeTruthy();
    expect((screen.getByLabelText("Acknowledge Forgejo read-only recovery") as HTMLInputElement).checked).toBe(false);
    expect(screen.getByRole("button", { name: "Prepare read-only recovery" }).hasAttribute("disabled")).toBe(true);
    expect(screen.getByRole("button", { name: "Run original job once" }).hasAttribute("disabled")).toBe(true);
    expect(fetch.mock.calls.every(([, init]) => !(init as RequestInit | undefined)?.method)).toBe(true);
  });
  it("requires unchecked finite read consent and resets acknowledgment on another Root", async () => {
    const fetch = vi.fn((url: unknown, _init?: RequestInit) => response(metadata(url))); vi.stubGlobal("fetch", fetch);
    const view = render(<ForgejoTitlePanel ownerPrincipalId="owner" ownerSessionId="root" />);
    await screen.findByText(/Connection: active/);
    expect(screen.getByRole("button", { name: "Grant finite read consent" }).hasAttribute("disabled")).toBe(true);
    fireEvent.click(screen.getByLabelText("Acknowledge Forgejo finite private reads"));
    expect((screen.getByLabelText("Acknowledge Forgejo finite private reads") as HTMLInputElement).checked).toBe(true);
    view.rerender(<ForgejoTitlePanel ownerPrincipalId="owner" ownerSessionId="other" />);
    await waitFor(() => expect((screen.getByLabelText("Acknowledge Forgejo finite private reads") as HTMLInputElement).checked).toBe(false));
    expect(fetch.mock.calls.every(([, init]) => !(init as RequestInit | undefined)?.method)).toBe(true);
  });
  it("retains exact body on a lost response, reload does not POST, and only explicit retry repeats the same request", async () => {
    const bodies: string[] = [];
    const fetch = vi.fn((url: unknown, init?: RequestInit) => {
      if (init?.method === "PUT") {
        bodies.push(String(init.body));
        if (bodies.length === 1) return Promise.reject(Error("response lost"));
      }
      return response(metadata(url));
    }); vi.stubGlobal("fetch", fetch);
    const view = render(<ForgejoTitlePanel ownerPrincipalId="owner" ownerSessionId="root" />);
    await screen.findByText(/Connection: active/);
    fireEvent.click(screen.getByLabelText("Acknowledge Forgejo finite private reads"));
    fireEvent.click(screen.getByRole("button", { name: "Grant finite read consent" }));
    await screen.findByRole("button", { name: "Retry exact retained request" });
    expect(JSON.parse(bodies[0])).toEqual({ expected_revision: 1, duration_seconds: 900, read_ack: true });
    view.unmount(); render(<ForgejoTitlePanel ownerPrincipalId="owner" ownerSessionId="root" />);
    await screen.findByText(/Connection: active/);
    expect(bodies).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Retry exact retained request" }));
    await waitFor(() => expect(bodies).toHaveLength(2));
    expect(bodies[1]).toBe(bodies[0]);
    await waitFor(() => expect(sessionStorage.getItem(scope + ".pending")).toBeNull());
  });
  it.each([JSON.stringify({ method: "PUT", path: "/connection/read-consent", body: { expected_revision: 1, duration_seconds: 900, read_ack: 1 } }), "x".repeat(8193)])(
    "rejects corrupted or unbounded retained requests before parsing or contacting", async raw => {
      sessionStorage.setItem(scope + ".pending", raw);
      const fetch = vi.fn(); vi.stubGlobal("fetch", fetch);
      render(<ForgejoTitlePanel ownerPrincipalId="owner" ownerSessionId="root" />);
      await screen.findByRole("alert"); expect(fetch).not.toHaveBeenCalled();
      expect(screen.queryByRole("button", { name: "Retry exact retained request" })).toBeNull();
    });
  it("shows exact immutable numeric preview and title before creating its separately approved job", async () => {
    sessionStorage.setItem(scope + ".job", id);
    const target = { owner: "fixture", repository: "owned", repository_id: 9, issue_id: 21, issue_index: 3,
      old_title: "Old literal", new_title: "Reviewed literal", updated_at: "fixed-revision", timeline_digest: "b".repeat(64) };
    const fetch = vi.fn((url: unknown, _init?: RequestInit) => response(String(url).endsWith("/output") ? { target, no_change: false, no_learning: true }
      : String(url).includes("/jobs/") ? { ...job, status: "succeeded", declared_authority: { operation: "preview" }, forgejo: { phase: "verified", plaintext_digest: "c".repeat(64) } } : metadata(url)));
    vi.stubGlobal("fetch", fetch);
    render(<ForgejoTitlePanel ownerPrincipalId="owner" ownerSessionId="root" />);
    await screen.findByLabelText("Original Forgejo job");
    fireEvent.change(screen.getByLabelText("Forgejo finite Goal"), { target: { value: "goal" } });
    fireEvent.click(screen.getByRole("button", { name: "Read protected receipt" }));
    await screen.findByText("Old title: Old literal");
    expect(screen.getByText("Approved title: Reviewed literal")).toBeTruthy();
    expect(screen.getByText(/Issue ID 21 · repository ID 9 · issue #3/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Prepare independently approved title job" }).hasAttribute("disabled")).toBe(false);
    expect(fetch.mock.calls.every(([, init]) => !(init as RequestInit | undefined)?.method)).toBe(true);
  });
});
