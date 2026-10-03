import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RepoPublicationPanel } from "./RepoPublicationPanel";
import { publicationFixture } from "../../lib/repoPublication.fixture";
import type { WorkBoardRepoRepairProjection } from "../../types";

const repair = { job_id: "repair-a", revision: 4, proposal: { proposal_id: "proposal-a", revision: 3 } } as WorkBoardRepoRepairProjection;
function response(value: unknown) { return Promise.resolve(new Response(JSON.stringify(value), { status: 200, headers: { "Content-Type": "application/json" } })); }
function emptyDiscovery() { return response({ repair_job_id: "repair-a", owner_principal_id: "owner-a", owner_session_id: "root-a", limit: 20, next_offset: null, jobs: [] }); }
function fill() {
  for (const [name, value] of [["expected base commit", "b".repeat(40)], ["branch name", "feat/fix"], ["commit message", "Fix"], ["title", "Reviewed PR"], ["body", "Reviewed text"]]) fireEvent.change(screen.getByLabelText(name), { target: { value } });
}
afterEach(() => { vi.unstubAllGlobals(); });

describe("Tasks publication control", () => {
  it("rediscovers the original uncertain job with stopped consent using GET only", async () => {
    const fixture = { ...publicationFixture(), status: "unknown_external_effect", approval_status: "consumed" };
    const fetchMock = vi.fn((url: unknown, _options?: RequestInit) => String(url).includes("/repairs/")
      ? response({ repair_job_id: "repair-a", owner_principal_id: "owner-a", owner_session_id: "root-a", limit: 20, next_offset: null, jobs: [fixture] })
      : response({ repository: "acme/example", revision: 2, mode: "disabled", credential_configured: true, consent: { state: "github_consent_revoked", root_bound: true, actions: [] } }));
    vi.stubGlobal("fetch", fetchMock);
    render(<RepoPublicationPanel repair={repair} ownerPrincipalId="owner-a" ownerSessionId="root-a" />);
    expect(await screen.findByLabelText("Existing publication")).toBeTruthy();
    expect(screen.getByText("Reconcile destination").hasAttribute("disabled")).toBe(true);
    expect(screen.queryByText("Prepare exact publication preview")).toBeNull();
    expect(fetchMock.mock.calls.every(([, options]) => (options?.method ?? "GET") === "GET")).toBe(true);
  });
  it("shows exact patch/local access and requires a separate fresh approval", async () => {
    const open = vi.fn(); const fixture = publicationFixture();
    const fetch = vi.fn((url: unknown) => String(url).includes("/repairs/") ? emptyDiscovery() : String(url).endsWith("/connection") ? response({ repository: "acme/example", revision: 1, mode: "active", credential_configured: true, consent: { state: "active", root_bound: true, actions: ["github_git_objects_write", "github_new_branch_write", "github_ready_pr_write"] } }) : String(url).endsWith("/patch") ? response({ patch: "--- a/app.py\n+++ b/app.py", patch_sha256: "e".repeat(64) }) : response(fixture));
    vi.stubGlobal("fetch", fetch);
    render(<RepoPublicationPanel repair={repair} ownerPrincipalId="owner-a" ownerSessionId="root-a" onOpenApprovals={open} />);
    await screen.findByText("Repository: acme/example"); fill(); fireEvent.click(screen.getByText("Prepare exact publication preview"));
    await screen.findByText("Review local execution and remote publication approval");
    expect(screen.queryByText("Execute approved publication")).toBeNull();
    fireEvent.click(screen.getByText("Review local execution and remote publication approval")); expect(open).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls.filter(([url]) => String(url).endsWith("/execute"))).toHaveLength(0);
    expect(screen.getByText(/host-user access/)).toBeTruthy();
  });
  it("retains validated preview but disables controls on malformed readback", async () => {
    const fixture = publicationFixture(); let malformed = false;
    vi.stubGlobal("fetch", vi.fn((url: unknown) => String(url).includes("/repairs/") ? emptyDiscovery() : String(url).endsWith("/connection") ? response({ repository: "acme/example", revision: 1, mode: "active", credential_configured: true, consent: { state: "active", root_bound: true, actions: ["github_git_objects_write", "github_new_branch_write", "github_ready_pr_write"] } }) : String(url).endsWith("/patch") ? response({ patch: "diff", patch_sha256: "e".repeat(64) }) : response(malformed ? { status: "succeeded" } : fixture)));
    render(<RepoPublicationPanel repair={repair} ownerPrincipalId="owner-a" ownerSessionId="root-a" onOpenApprovals={() => undefined} />);
    await screen.findByText("Repository: acme/example"); fill(); fireEvent.click(screen.getByText("Prepare exact publication preview"));
    await screen.findByText("Review local execution and remote publication approval"); malformed = true;
    fireEvent.click(screen.getByText("Refresh exact job")); await screen.findByRole("alert");
    expect(screen.getByText("Review local execution and remote publication approval").hasAttribute("disabled")).toBe(true);
    expect(screen.getByText(/Reviewed PR/)).toBeTruthy();
  });
  it("ignores old root metadata even across an ABA rerender", async () => {
    let release: (value: Response) => void = () => undefined;
    const pending = new Promise<Response>((resolve) => { release = resolve; });
    const fetch = vi.fn().mockReturnValueOnce(pending).mockImplementation(() => response({ repository: "fresh/repo", revision: 1, mode: "active", credential_configured: true, consent: { state: "active", root_bound: true, actions: ["github_git_objects_write", "github_new_branch_write", "github_ready_pr_write"] } }));
    vi.stubGlobal("fetch", fetch);
    const view = render(<RepoPublicationPanel repair={repair} ownerPrincipalId="owner-a" ownerSessionId="root-a" />);
    view.rerender(<RepoPublicationPanel repair={repair} ownerPrincipalId="owner-a" ownerSessionId="root-b" />);
    view.rerender(<RepoPublicationPanel repair={repair} ownerPrincipalId="owner-a" ownerSessionId="root-a" />);
    await screen.findByText("Repository: fresh/repo");
    await act(async () => { release(new Response(JSON.stringify({ repository: "stale/private", revision: 9, mode: "active", credential_configured: true, consent: { state: "active", root_bound: true, actions: ["github_git_objects_write", "github_new_branch_write", "github_ready_pr_write"] } }))); });
    await waitFor(() => expect(screen.queryByText("Repository: stale/private")).toBeNull());
  });
});
