import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { GitHubActivityPanel } from "./GitHubActivityPanel";
import { GITHUB_ACTIVITY_GRANT_SENTENCE, githubActivityItems } from "../../lib/githubActivity";

const off = { source: { id: null, state: "not_connected", revision: 0 }, gh: { available: true, reason_code: null, entry_path: "/opt/homebrew/bin/gh" },
  grant_sentence: "server text", endpoints: ["GET /user", "GET /repos/{repository}/commits"], message: "GitHub source not connected.", last_run: null };
const on = { ...off, source: { id: "source-a", state: "active", revision: 1, identity_login: "octo", gh_entry_path: "/opt/homebrew/bin/gh",
  scope: { discover_from_activity: true, repositories: ["seraph-quest/seraph"], owners: [], exclude_repositories: [], kinds: ["commit"] },
  model_context_allowed: false }, message: "GitHub source on · @octo · uses your local gh login." };
const checked = { gh_version: "2.89.0", gh_entry_path: "/opt/homebrew/bin/gh", gh_real_path: "/opt/homebrew/Cellar/gh/2.89.0/bin/gh",
  identity_id: 101, identity_login: "octo" };
const response = (value: unknown, status = 200) => Promise.resolve(new Response(JSON.stringify(value), { status }));
afterEach(() => vi.unstubAllGlobals());

describe("GitHub activity source panel", () => {
  it("is off by default and starts nothing on load", async () => {
    const fetch = vi.fn(() => response(off));
    vi.stubGlobal("fetch", fetch);
    render(<GitHubActivityPanel ownerPrincipalId="owner-a" ownerSessionId="root-a" />);
    await screen.findByText("GitHub source not connected.");
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(String((fetch.mock.calls[0] as unknown[])[0])).toMatch(/\/api\/capabilities\/github\/activity$/);
    expect(screen.getByText("Enable").hasAttribute("disabled")).toBe(true);
    expect(screen.getByText("Collect now").hasAttribute("disabled")).toBe(true);
  });

  it("keeps Enable disabled until the login was checked and the acknowledgement ticked, then sends the pinned identity", async () => {
    const fetch = vi.fn((url: unknown, init?: RequestInit) => {
      const path = String(url);
      if (path.endsWith("/gh/check")) return response(checked);
      if (init?.method === "PUT") return response(on);
      return response(off);
    });
    vi.stubGlobal("fetch", fetch);
    render(<GitHubActivityPanel ownerPrincipalId="owner-a" ownerSessionId="root-a" />);
    await screen.findByText("GitHub source not connected.");
    const enable = screen.getByText("Enable");
    const acknowledgement = screen.getByLabelText(/Use my local gh login/) as HTMLInputElement;
    expect(acknowledgement.disabled).toBe(true);
    fireEvent.click(screen.getByText("Check gh login"));
    await screen.findByText(/logged in as @octo/);
    expect(JSON.parse((fetch.mock.calls.find(([url]) => String(url).endsWith("/gh/check"))![1] as RequestInit).body as string))
      .toEqual({ acknowledge_gh_login_check: true });
    expect(screen.getByText(GITHUB_ACTIVITY_GRANT_SENTENCE)).toBeTruthy();
    expect(enable.hasAttribute("disabled")).toBe(true);
    fireEvent.change(screen.getByLabelText("Always read repositories"), { target: { value: "seraph-quest/seraph" } });
    fireEvent.click(screen.getByLabelText(/Use my local gh login/));
    expect(enable.hasAttribute("disabled")).toBe(false);
    fireEvent.click(enable);
    await screen.findByText("On · @octo · uses your local gh login");
    const put = fetch.mock.calls.find(([, init]) => init?.method === "PUT")![1] as RequestInit;
    expect(JSON.parse(put.body as string)).toEqual({ expected_revision: 0,
      scope: { discover_from_activity: true, repositories: ["seraph-quest/seraph"], owners: [], exclude_repositories: [] },
      acknowledge_gh_ambient_login: true, expected_identity_id: 101, expected_gh_entry_path: "/opt/homebrew/bin/gh",
      allow_model_context: false });
  });

  it("always shows the full-scope sentence while on, and switches off with the current revision", async () => {
    const fetch = vi.fn((url: unknown, _init?: RequestInit) => response(String(url).endsWith("/revoke") ? { ...on, source: { ...on.source, state: "revoked", revision: 2 }, message: "GitHub source was switched off by the operator. Stored activity from earlier collections is shown." } : on));
    vi.stubGlobal("fetch", fetch);
    render(<GitHubActivityPanel ownerPrincipalId="owner-a" ownerSessionId="root-a" />);
    await screen.findByText("On · @octo · uses your local gh login");
    expect(screen.getByText(GITHUB_ACTIVITY_GRANT_SENTENCE)).toBeTruthy();
    fireEvent.click(screen.getByText("Switch off"));
    await screen.findByText(/switched off by the operator/);
    const revoke = fetch.mock.calls.find(([url]) => String(url).endsWith("/revoke"))![1] as RequestInit;
    expect(JSON.parse(revoke.body as string)).toEqual({ expected_revision: 1 });
    expect(screen.queryByText(GITHUB_ACTIVITY_GRANT_SENTENCE)).toBeNull();
  });

  it("collects now and links only GitHub URLs", async () => {
    const day = { message: "ok", items: [
      { id: "1", repository: "seraph-quest/seraph", kind: "pr_merged", number: 12, title: "Merged thing", url: "https://github.com/seraph-quest/seraph/pull/12", occurred_at: "2026-11-17T10:00:00+00:00" },
      { id: "2", repository: "seraph-quest/seraph", kind: "commit", number: null, title: "Odd link", url: "https://evil.example/x", occurred_at: "2026-11-17T11:00:00+00:00" },
    ] };
    vi.stubGlobal("fetch", vi.fn((url: unknown) => {
      const path = String(url);
      if (path.endsWith("/collect")) return response({ status: "complete", message: "GitHub activity collected as @octo up to 21:00: 2 items in 1 repositories.", run: { report_date: "2026-11-17" } });
      if (path.includes("/items")) return response(day);
      return response(on);
    }));
    render(<GitHubActivityPanel ownerPrincipalId="owner-a" ownerSessionId="root-a" />);
    await screen.findByText("On · @octo · uses your local gh login");
    fireEvent.click(screen.getByText("Collect now"));
    await screen.findByText(/2 items in 1 repositories/);
    const link = screen.getByText("Merged thing").closest("a")!;
    expect(link.getAttribute("href")).toBe("https://github.com/seraph-quest/seraph/pull/12");
    expect(link.getAttribute("rel")).toBe("noreferrer noopener");
    expect(screen.getByText("Odd link").closest("a")).toBeNull();
  });

  it("resets the check and acknowledgement after a root change", async () => {
    vi.stubGlobal("fetch", vi.fn((url: unknown) => response(String(url).endsWith("/gh/check") ? checked : off)));
    const view = render(<GitHubActivityPanel ownerPrincipalId="owner-a" ownerSessionId="root-a" />);
    await screen.findByText("GitHub source not connected.");
    fireEvent.click(screen.getByText("Check gh login"));
    await screen.findByText(/logged in as @octo/);
    fireEvent.click(screen.getByLabelText(/Use my local gh login/));
    view.rerender(<GitHubActivityPanel ownerPrincipalId="owner-a" ownerSessionId="root-b" />);
    await waitFor(() => expect(screen.queryByText(/logged in as @octo/)).toBeNull());
    await screen.findByText("GitHub source not connected.");
    expect((screen.getByLabelText(/Use my local gh login/) as HTMLInputElement).checked).toBe(false);
  });

  it("shows the backend error code and the no-gh state", async () => {
    vi.stubGlobal("fetch", vi.fn((url: unknown) => String(url).endsWith("/gh/check")
      ? response({ detail: { code: "gh_not_logged_in" } }, 409)
      : response({ ...off, gh: { available: false, reason_code: "gh_unavailable", entry_path: null } })));
    render(<GitHubActivityPanel ownerPrincipalId="owner-a" ownerSessionId="root-a" />);
    await screen.findByText(/only works when Seraph's backend runs directly on your computer/);
    fireEvent.click(screen.getByText("Check gh login"));
    expect((await screen.findByRole("alert")).textContent).toContain("gh not logged in");
  });
});

describe("githubActivityItems", () => {
  it("drops non-GitHub and malformed URLs", () => {
    const value = githubActivityItems({ items: [{ id: "a", repository: "r/s", kind: "commit", occurred_at: "x", url: "javascript:alert(1)" }] });
    expect(value.items[0].url).toBeNull();
  });
});
