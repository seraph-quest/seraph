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
  it.each(["inconclusive", "permanently_stale_not_applied", "applied"])("inspects retained %s without automatically sending or discarding a request", async state => {
    const fixture = { ...publicationFixture(), status: "unknown_external_effect", approval_status: "consumed" };
    const key = `seraph:github-capacity-close:v1:owner-a:root-a:${fixture.job_id}:engineering.repo-publication.v1`;
    const body = { acknowledged_capacity_close: true, expected_job_revision: fixture.revision,
      expected_connection_revision: 2, expected_connection_fence: 3, idempotency_key: "12345678-1234-1234-1234-123456789abc" };
    sessionStorage.setItem(key, JSON.stringify(body));
    const closure = { closure_id: "closure-a", artifact_id: "artifact-a", artifact_sha256: "a".repeat(64),
      closed_at: "2026-10-03T00:00:00Z", native_kind: "engineering.repo-publication.v1", observation_only: true };
    let inspections = 0;
    const posts: Record<string, unknown>[] = [];
    vi.stubGlobal("fetch", vi.fn((url: unknown, options?: RequestInit) => {
      if (String(url).includes("pending_capacity_close=")) {
        inspections++;
        expect(options?.method ?? "GET").toBe("GET");
        expect(JSON.parse(new URL(String(url), "http://localhost").searchParams.get("pending_capacity_close")!)).toEqual(body);
        return response({ ...fixture, revision: fixture.revision + 1, github_capacity_closure: state === "applied" ? closure : null,
          pending_capacity_close: { state, job_id: fixture.job_id, job_revision: fixture.revision + 1,
            request: body, request_digest: "a".repeat(64), closure: state === "applied" ? closure : null } });
      }
      if (String(url).endsWith("/close-capacity")) { posts.push(JSON.parse(String(options?.body))); return Promise.reject(new Error("response lost")); }
      if (String(url).includes("/repairs/")) return response({ repair_job_id: "repair-a", owner_principal_id: "owner-a", owner_session_id: "root-a", limit: 20, next_offset: null, jobs: [fixture] });
      return response({ repository: "acme/example", revision: 4, active_fence: 3, active_job_id: fixture.job_id, mode: "disabled", credential_configured: true });
    }));
    render(<RepoPublicationPanel repair={repair} ownerPrincipalId="owner-a" ownerSessionId="root-a" />);
    await screen.findByText("Inspect retained close request");
    expect(inspections).toBe(0); expect(posts).toHaveLength(0);
    fireEvent.click(screen.getByText("Inspect retained close request"));
    if (state === "applied") {
      await screen.findByText(/Capacity released/); expect(sessionStorage.getItem(key)).toBeNull();
    } else if (state === "inconclusive") {
      await screen.findByText(/Closure outcome is inconclusive/);
      expect(screen.queryByText("Discard rejected request")).toBeNull();
      expect(sessionStorage.getItem(key)).toBe(JSON.stringify(body));
    } else {
      await screen.findByText("Discard rejected request");
      expect(sessionStorage.getItem(key)).toBe(JSON.stringify(body));
      fireEvent.click(screen.getByLabelText(/I authorize this separate capacity closure/));
      fireEvent.click(screen.getByText("Discard rejected request"));
      await waitFor(() => expect(sessionStorage.getItem(key)).toBeNull());
      expect(inspections).toBe(2); expect(posts).toHaveLength(0);
      expect(screen.getByLabelText(/I authorize this separate capacity closure/)).not.toBeChecked();
      await waitFor(() => expect(screen.getByText("Close publication capacity")).toBeDisabled());
      fireEvent.click(screen.getByLabelText(/I authorize this separate capacity closure/));
      fireEvent.click(screen.getByText("Close publication capacity"));
      await waitFor(() => expect(posts).toHaveLength(1));
      expect(posts[0].idempotency_key).not.toBe(body.idempotency_key);
      expect(posts[0].expected_job_revision).toBe(fixture.revision + 1);
      expect(posts[0].expected_connection_revision).toBe(4);
    }
    expect(posts.length).toBe(state === "permanently_stale_not_applied" ? 1 : 0);
  });

  it("keeps the retained request when inspection response is unavailable or echoes an altered body", async () => {
    const fixture = { ...publicationFixture(), status: "unknown_external_effect", approval_status: "consumed" };
    const key = `seraph:github-capacity-close:v1:owner-a:root-a:${fixture.job_id}:engineering.repo-publication.v1`;
    const body = { acknowledged_capacity_close: true, expected_job_revision: fixture.revision,
      expected_connection_revision: 2, expected_connection_fence: 3, idempotency_key: "12345678-1234-1234-1234-123456789abc" };
    sessionStorage.setItem(key, JSON.stringify(body));
    let altered = false;
    vi.stubGlobal("fetch", vi.fn((url: unknown) => {
      if (String(url).includes("pending_capacity_close=")) return altered ? response({ ...fixture,
        pending_capacity_close: { state: "permanently_stale_not_applied", job_id: fixture.job_id, job_revision: fixture.revision,
          request: { ...body, expected_job_revision: 999 }, request_digest: "a".repeat(64), closure: null } }) : Promise.reject(new Error("timeout"));
      if (String(url).includes("/repairs/")) return response({ repair_job_id: "repair-a", owner_principal_id: "owner-a", owner_session_id: "root-a", limit: 20, next_offset: null, jobs: [fixture] });
      return response({ repository: "acme/example", revision: 2, active_fence: 3, mode: "disabled", credential_configured: true });
    }));
    render(<RepoPublicationPanel repair={repair} ownerPrincipalId="owner-a" ownerSessionId="root-a" />);
    await screen.findByText("Inspect retained close request");
    fireEvent.click(screen.getByText("Inspect retained close request"));
    await screen.findByText("timeout");
    altered = true; fireEvent.click(screen.getByText("Inspect retained close request"));
    await screen.findByText("Pending close inspection binding changed");
    expect(sessionStorage.getItem(key)).toBe(JSON.stringify(body));
    expect(screen.queryByText("Discard rejected request")).toBeNull();
  });
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
    await screen.findByText("Repository: acme/example");
    expect(await screen.findByText("Close publication capacity")).toBeDisabled();
    expect(screen.getByLabelText(acknowledgment)).not.toBeChecked();
    fireEvent.click(screen.getByLabelText(acknowledgment));
    await waitFor(() => expect(screen.getByText("Close publication capacity")).toBeEnabled());
    fireEvent.click(screen.getByText("Close publication capacity"));
    await waitFor(() => expect(bodies).toHaveLength(1));
    view.unmount(); loseResponse = false;
    view = render(<RepoPublicationPanel repair={repair} ownerPrincipalId="owner-a" ownerSessionId="root-a" />);
    await screen.findByText("Repository: acme/example");
    expect(await screen.findByText("Close publication capacity")).toBeDisabled();
    expect(bodies).toHaveLength(1);
    expect(screen.getByLabelText(acknowledgment)).not.toBeChecked();
    fireEvent.click(screen.getByLabelText(acknowledgment));
    await waitFor(() => expect(screen.getByText("Close publication capacity")).toBeEnabled());
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
