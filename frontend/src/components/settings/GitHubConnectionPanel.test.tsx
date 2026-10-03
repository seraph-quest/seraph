import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { GitHubConnectionPanel } from "./GitHubConnectionPanel";

const metadata = { id: "connection-a", repository: "acme/example", revision: 3, mode: "active", credential_configured: true, active_job_id: null,
  consent: { state: "needs_consent", expires_at: null, actions: [], root_bound: false } };
const response = (value: unknown) => Promise.resolve(new Response(JSON.stringify(value), { status: 200 }));
afterEach(() => vi.unstubAllGlobals());

describe("finite GitHub connection consent", () => {
  it("requires explicit unchecked consent and named actions, saves exact revision then reads metadata", async () => {
    const fetch = vi.fn((url: unknown, init?: RequestInit) => response(String(url).endsWith("/vault/keys") ? [{ key: "github-token" }] : init?.method === "PUT" ? { ...metadata, revision: 4 } : metadata));
    vi.stubGlobal("fetch", fetch);
    render(<GitHubConnectionPanel ownerPrincipalId="owner-a" ownerSessionId="root-a" />);
    await screen.findByText(/needs consent/);
    const save = screen.getByText("Save explicit consent");
    expect(save.hasAttribute("disabled")).toBe(true);
    fireEvent.change(screen.getByLabelText("GitHub vault credential"), { target: { value: "github-token" } });
    fireEvent.click(screen.getByLabelText("Create issues"));
    expect(save.hasAttribute("disabled")).toBe(true);
    fireEvent.click(screen.getByLabelText(/I consent/));
    fireEvent.click(save);
    await waitFor(() => expect(fetch.mock.calls.some(([, init]) => init?.method === "PUT")).toBe(true));
    const saved = fetch.mock.calls.find(([, init]) => init?.method === "PUT")![1]!;
    expect(JSON.parse(saved.body as string)).toEqual({ repository: "acme/example", vault_key: "github-token", mode: "active", expected_revision: 3,
      consent: { acknowledged: true, duration_seconds: 900, actions: ["github_issue_write"] } });
    await waitFor(() => expect((screen.getByLabelText(/I consent/) as HTMLInputElement).checked).toBe(false));
    expect(fetch.mock.calls.filter(([url, init]) => String(url).endsWith("/connection") && !init?.method).length).toBe(2);
  });
  it("keeps stop available during a reservation and prevents scope edits", async () => {
    vi.stubGlobal("fetch", vi.fn(url => response(String(url).endsWith("/vault/keys") ? [] : { ...metadata, active_job_id: "unknown-job" })));
    render(<GitHubConnectionPanel ownerPrincipalId="owner-a" ownerSessionId="root-a" />);
    await screen.findByText(/publication holds/);
    expect(screen.getByText("Stop GitHub writes").hasAttribute("disabled")).toBe(false);
    expect(screen.getByText("Save explicit consent").hasAttribute("disabled")).toBe(true);
  });
  it("clears consent after a root change", async () => {
    vi.stubGlobal("fetch", vi.fn(url => response(String(url).endsWith("/vault/keys") ? [] : metadata)));
    const view = render(<GitHubConnectionPanel ownerPrincipalId="owner-a" ownerSessionId="root-a" />);
    await screen.findByText(/needs consent/);
    fireEvent.click(screen.getByLabelText(/I consent/));
    view.rerender(<GitHubConnectionPanel ownerPrincipalId="owner-a" ownerSessionId="root-b" />);
    await waitFor(() => expect((screen.getByLabelText(/I consent/) as HTMLInputElement).checked).toBe(false));
  });
});
