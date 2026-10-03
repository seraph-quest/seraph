import "@testing-library/jest-dom/vitest";
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
afterEach(() => { vi.unstubAllGlobals(); sessionStorage.clear(); });

describe("Tasks publication control", () => {
  it("requires separate unchecked close consent and retains exact lost-response request across remount", async () => {
    const fixture = { ...publicationFixture(), status: "unknown_external_effect", approval_status: "consumed" };
    const bodies: string[] = [];
    let loseResponse = true;
    const closed = { ...fixture, revision: fixture.revision + 1, github_capacity_closure: {
      closure_id: "closure-a", artifact_id: "artifact-a", artifact_sha256: "a".repeat(64),
      closed_at: "2026-10-03T00:00:00Z", native_kind: "engineering.repo-publication.v1", observation_only: true } };
    const fetchMock = vi.fn((url: unknown, options?: RequestInit) => {
      if (String(url).endsWith("/close-capacity")) {
        bodies.push(String(options?.body));
        expect(sessionStorage.length).toBe(1);
        return loseResponse ? Promise.reject(new Error("response lost")) : response(closed);
      }
      if (String(url).includes("/repairs/")) return response({ repair_job_id: "repair-a", owner_principal_id: "owner-a", owner_session_id: "root-a", limit: 20, next_offset: null, jobs: [fixture] });
      if (String(url).endsWith("/patch")) return response({ patch: "diff", patch_sha256: fixture.preview.repair_binding.patch_sha256 });
      return response({ repository: "acme/example", revision: 2, active_fence: 3, active_job_id: fixture.job_id, mode: "disabled", credential_configured: true });
    });
    vi.stubGlobal("fetch", fetchMock);
    let view = render(<RepoPublicationPanel repair={repair} ownerPrincipalId="owner-a" ownerSessionId="root-a" />);
    const acknowledgment = /I authorize this separate capacity closure/;
    expect(await screen.findByText("Close publication capacity")).toBeDisabled();
    fireEvent.click(screen.getByLabelText(acknowledgment));
    fireEvent.click(screen.getByText("Close publication capacity"));
    await waitFor(() => expect(bodies).toHaveLength(1));
    view.unmount(); loseResponse = false;
    view = render(<RepoPublicationPanel repair={repair} ownerPrincipalId="owner-a" ownerSessionId="root-a" />);
    expect(await screen.findByText("Close publication capacity")).toBeDisabled();
    expect(bodies).toHaveLength(1);
    expect(screen.getByLabelText(acknowledgment)).not.toBeChecked();
    fireEvent.click(screen.getByLabelText(acknowledgment));
    fireEvent.click(screen.getByText("Close publication capacity"));
    await screen.findByText(/Capacity released/);
    expect(bodies).toHaveLength(2); expect(bodies[1]).toBe(bodies[0]);
    expect(sessionStorage.length).toBe(0);
    expect(screen.getByText("Reconcile destination")).toBeTruthy();
    expect(screen.queryByText(/Ready PR independently verified/)).toBeNull();
    view.unmount();
  });
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
