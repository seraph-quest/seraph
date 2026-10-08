import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { EffectiveGrantsPanel } from "./EffectiveGrantsPanel";
import { apiFetch } from "../../lib/api";
vi.mock("../../lib/api", () => ({ apiFetch: vi.fn() }));
const grant = { grant_id: "goal:a", kind: "goal", record_id: "a", boundary: "observation", purpose: "proactive_goal_work", source: "goal", destination: "governed_jobs", state: "active", revision: 3, expires_at: null, origin: "current_authenticated_root", affected_jobs: [{ job_id: "task-a", state: "queued", kind: "work_board_task" }], controls: ["revoke"], limits: {} };
const inventory = { grants: [grant], unavailable: [], truncated: [], snapshot_digest: "abc" };
function response(value: unknown) { return { ok: true, json: async () => value } as Response; }
beforeEach(() => { vi.resetAllMocks(); vi.unstubAllGlobals(); });
describe("Effective grant bindings", () => {
  it("identifies identical grants and revokes only the chosen record", async () => {
    const second={...grant,grant_id:"goal:b",record_id:"b"};
    vi.mocked(apiFetch).mockResolvedValueOnce(response({...inventory,grants:[grant,second]})).mockResolvedValueOnce(response({status:"local_revocation_confirmed",readback:inventory}));
    render(<EffectiveGrantsPanel sessionId="root-a" />);
    const reference=await screen.findByText("Record reference: b · Grant: goal:b");
    const row=reference.closest("article")!;
    fireEvent.click(within(row).getByText("Revoke local authority"));
    await screen.findByText(/Local revocation confirmed/);
    expect(JSON.parse(String(vi.mocked(apiFetch).mock.calls[1][1]?.body)).grant_id).toBe("goal:b");
  });
  it("supports browsers without randomUUID and keeps the random gesture key", async () => {
    const random=vi.fn((bytes:Uint8Array) => { bytes.fill(13); return bytes; });
    vi.stubGlobal("crypto",{getRandomValues:random});
    vi.mocked(apiFetch).mockResolvedValueOnce(response(inventory)).mockResolvedValueOnce(response({status:"partial_failure",local_state:"blocked_cleanup",readback:inventory})).mockResolvedValueOnce(response({status:"local_revocation_confirmed",readback:inventory}));
    render(<EffectiveGrantsPanel sessionId="root-a" />);
    fireEvent.click(await screen.findByText("Revoke local authority"));
    await screen.findByText(/External cleanup is unconfirmed/);
    fireEvent.click(screen.getByText("Retry exact revoke"));
    await screen.findByText(/Local revocation confirmed/);
    expect(random).toHaveBeenCalledTimes(1);
    expect(vi.mocked(apiFetch).mock.calls[1][1]?.body).toEqual(vi.mocked(apiFetch).mock.calls[2][1]?.body);
  });
  it("retains safe metadata and sends no revoke after malformed partial metadata", async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce(response(inventory)).mockResolvedValueOnce(response({...inventory,grants:[{...grant,affected_jobs:null}]}));
    render(<EffectiveGrantsPanel sessionId="root-a" />);
    await screen.findByText("Revoke local authority");
    fireEvent.click(screen.getByText("Refresh grants"));
    await screen.findByRole("alert");
    expect(screen.getByText(/task-a: queued/)).toBeTruthy();
    fireEvent.click(screen.getByText("Revoke local authority"));
    expect(vi.mocked(apiFetch).mock.calls.length).toBe(2);
  });
  it("rejects malformed revoke readback without replacing the last view", async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce(response(inventory)).mockResolvedValueOnce(response({status:"local_revocation_confirmed",readback:{grants:[]}}));
    render(<EffectiveGrantsPanel sessionId="root-a" />);
    fireEvent.click(await screen.findByText("Revoke local authority"));
    await screen.findByRole("alert");
    expect(screen.getByText(/task-a: queued/)).toBeTruthy();
    expect(screen.queryByText(/Local revocation confirmed/)).toBeNull();
    expect((screen.getByText("Retry exact revoke") as HTMLButtonElement).disabled).toBe(true);
  });
  it("reports a consumed approval without claiming undo", async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce(response(inventory)).mockResolvedValueOnce(response({status:"already_consumed",readback:{...inventory,grants:[]}}));
    render(<EffectiveGrantsPanel sessionId="root-a" />);
    fireEvent.click(await screen.findByText("Revoke local authority"));
    await screen.findByText(/approval was already consumed/);
    expect(screen.queryByText(/Local revocation confirmed/)).toBeNull();
    expect(screen.queryByText("Retry exact revoke")).toBeNull();
  });
  it("uses server revision and presents local-only revoke readback", async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce(response(inventory)).mockResolvedValueOnce(response({ status: "local_revocation_confirmed", readback: { ...inventory, grants: [{ ...grant, state: "denied" }] } }));
    render(<EffectiveGrantsPanel sessionId="root-a" />);
    await screen.findByText(/Related work: work_board_task task-a: queued/);
    fireEvent.click(screen.getByText("Revoke local authority"));
    await screen.findByText(/Local revocation confirmed/);
    const init = vi.mocked(apiFetch).mock.calls[1][1]!;
    const body = JSON.parse(String(init.body));
    expect(body.expected_revision).toBe(3);
    expect(body.grant_id).toBe("goal:a");
    expect(body.owner_principal_id).toBeUndefined();
    expect(screen.getByText(/External account access was not universally revoked/)).toBeTruthy();
  });
  it("retains last-known inventory while disabling stale controls", async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce(response(inventory)).mockRejectedValueOnce(new Error("metadata unavailable"));
    render(<EffectiveGrantsPanel sessionId="root-a" />);
    await screen.findByText("Revoke local authority");
    fireEvent.click(screen.getByText("Refresh grants"));
    await screen.findByRole("alert");
    expect(screen.getByText(/Last confirmed view is stale/)).toBeTruthy();
    expect((screen.getByText("Revoke local authority") as HTMLButtonElement).disabled).toBe(true);
  });
  it("keeps the exact key across partial failures", async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce(response(inventory)).mockResolvedValueOnce(response({status:"partial_failure",local_state:"blocked_cleanup",readback:inventory})).mockResolvedValueOnce(response({status:"local_revocation_confirmed",readback:inventory}));
    render(<EffectiveGrantsPanel sessionId="root-a" />);
    fireEvent.click(await screen.findByText("Revoke local authority"));
    await screen.findByText(/External cleanup is unconfirmed/);
    fireEvent.click(screen.getByText("Retry exact revoke"));
    await screen.findByText(/Local revocation confirmed/);
    expect(vi.mocked(apiFetch).mock.calls[1][1]?.body).toEqual(vi.mocked(apiFetch).mock.calls[2][1]?.body);
  });
  it("clears another root's private cached inventory", async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce(response(inventory)).mockResolvedValueOnce(response({...inventory,grants:[]}));
    const view=render(<EffectiveGrantsPanel sessionId="root-a" />);
    await screen.findByText("Revoke local authority");
    view.rerender(<EffectiveGrantsPanel sessionId="root-b" />);
    await screen.findByText("No grants in this login scope.");
    expect(screen.queryByText(/task-a/)).toBeNull();
  });
});
