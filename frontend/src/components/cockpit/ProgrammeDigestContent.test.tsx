import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ProgrammeDigestContent } from "./ProgrammeDigestContent";
import { isProgrammeDigestSnapshot } from "./programmeDigestApi";
import { GuardianInboxPanel } from "./GuardianInboxPanel";
import { CockpitHome } from "./CockpitHome";

// Finite API fixtures establish UI bindings, not discovery or model quality.
function receipt() {
  return { programmes: [{ id: "programme-one", goal_id: "goal-one", grant_revision: 2, state: "active", reason_code: null as string | null,
    last_run: "2026-10-09T06:00:00Z", sources_checked: 3, output: "actual-brief", next_run: null as string | null,
    next_digest_at: "2026-10-10T06:00:00Z", remaining_allowance_microusd: 400, recovery: null }],
  notifications: { enabled: false, deadline_categories: [], digest_slots_remaining: 1, deadline_slots_remaining: 1, quiet_hours_active: true, delivery_debt: false },
  digests: [{ id: "daily-one", created_at: "2026-10-09T06:00:00Z", digest: { schema_version: "ProgrammeDigest.v1", local_date: "2026-10-09", timezone: "Europe/Warsaw", programme_ids: ["programme-one"], finding_ids: ["finding-one"], prepared_outputs: [], blocked_reasons: [] },
    findings: [{ id: "finding-one", goal_id: "goal-one", programme_id: "programme-one", job_id: "actual-job", text: "An actual source finding", task_id: null as string | null,
      citations: [{ source_id: "actual-source", first_line: 2, last_line: 4, span_sha256: "a".repeat(64) }], prepared_outputs: [], follow_through: null as null | { finding_id: string; desired_outcome: string; task_proposal_id: string; due_at: string | null; status: string }, actionable: true, recovery: null, source_freshness: "current" }] }] };
}
const reply = (data: unknown, status = 200) => ({ ok: status === 200, status, json: async () => data });

describe("Programme digest binding", () => {
  const fetchMock = vi.fn();
  beforeEach(() => { fetchMock.mockReset(); vi.stubGlobal("fetch", fetchMock); });
  afterEach(() => vi.unstubAllGlobals());

  it("prepares inert C1 lineage and retains same-card Work navigation after reload", async () => {
    const data = receipt(); const open = vi.fn();
    fetchMock.mockImplementation((url, init) => {
      if (String(url).endsWith("/actions")) {
        expect(String(url)).toContain("/api/guardian/inbox/programme-findings/finding-one/actions");
        expect(init.credentials).toBe("include");
        expect(JSON.parse(init.body)).toEqual({ action: "accept_followup", desired_outcome: "Prepare cited checklist", idempotency_key: expect.any(String) });
        data.digests[0].findings[0].task_id = "task-from-finding";
        data.digests[0].findings[0].follow_through = { finding_id: "finding-one", desired_outcome: "Prepare cited checklist", task_proposal_id: "c1-proposal", due_at: null, status: "prepared" };
        return Promise.resolve(reply({ finding_id: "finding-one", task_id: "task-from-finding" }));
      }
      return Promise.resolve(reply(data));
    });
    const view = render(<ProgrammeDigestContent ownerKey="operator:root" onOpenTask={open} />);
    const card = await screen.findByRole("article", { name: "Programme finding" });
    expect(within(card).getByText(/source actual-source · lines 2–4/)).toBeInTheDocument();
    const prepare = within(card).getByRole("button", { name: "Prepare next step for review" });
    expect(prepare).toBeDisabled();
    fireEvent.change(within(card).getByLabelText("Desired outcome"), { target: { value: "Prepare cited checklist" } });
    fireEvent.click(prepare);
    fireEvent.click(await screen.findByRole("button", { name: "Review prepared proposal in Work" }));
    expect(open).toHaveBeenCalledWith("task-from-finding");
    view.unmount();
    render(<ProgrammeDigestContent ownerKey="operator:root" onOpenTask={open} />);
    fireEvent.click(await screen.findByRole("button", { name: "Review prepared proposal in Work" }));
    expect(fetchMock.mock.calls.filter(([, init]) => init.method === "POST")).toHaveLength(1);
  });

  it("keeps notifications separately opt-in and binds caps, quiet hours and passive recovery", async () => {
    const data = receipt(); data.programmes[0].state = "blocked"; data.programmes[0].reason_code = "programme_review_required";
    fetchMock.mockResolvedValue(reply(data));
    render(<ProgrammeDigestContent ownerKey="operator:root" />);
    await screen.findByText(/Delivery · Inbox only · digest slots 1\/1 · deadline slots 1\/1 · Quiet hours active/);
    expect(screen.getByRole("button", { name: "Pause programme" })).toBeDisabled();
    expect(fetchMock.mock.calls.filter(([, init]) => init.method === "POST")).toHaveLength(0);
    fireEvent.change(screen.getByLabelText("Relevant deadline categories"), { target: { value: "research, funding" } });
    fireEvent.click(screen.getByRole("button", { name: "Opt in to bounded notifications" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/programme-notifications"))).toBe(true));
    const post = fetchMock.mock.calls.find(([url]) => String(url).endsWith("/programme-notifications"))!;
    expect(JSON.parse(post[1].body)).toEqual({ enabled: true, deadline_categories: ["research", "funding"] });
  });

  it("persists defer and dismiss through API and disables stale findings", async () => {
    const data = receipt(); data.digests[0].findings[0].source_freshness = "stale";
    fetchMock.mockResolvedValue(reply(data));
    render(<ProgrammeDigestContent ownerKey="operator:root" />);
    const card = await screen.findByRole("article", { name: "Programme finding" });
    fireEvent.change(within(card).getByLabelText("Desired outcome"), { target: { value: "Checklist" } });
    expect(within(card).getByRole("button", { name: "Prepare next step for review" })).toBeDisabled();
    fireEvent.change(within(card).getByLabelText("Planned follow-up"), { target: { value: new Date(Date.now() + 86400000).toISOString().slice(0, 16) } });
    fetchMock.mockImplementation((url) => Promise.resolve(reply(String(url).endsWith("/actions") ? { finding_id: "finding-one" } : data)));
    fireEvent.click(within(card).getByRole("button", { name: "Defer finding" }));
    await waitFor(() => expect(fetchMock.mock.calls.filter(([, init]) => init.method === "POST")).toHaveLength(1));
    await waitFor(() => expect(screen.getByRole("button", { name: "Dismiss finding" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Dismiss finding" }));
    await waitFor(() => expect(fetchMock.mock.calls.filter(([, init]) => init.method === "POST")).toHaveLength(2));
    const posts = fetchMock.mock.calls.filter(([, init]) => init.method === "POST").map(([, init]) => JSON.parse(init.body));
    expect(posts[0]).toMatchObject({ action: "snooze", until: expect.any(String) });
    expect(posts[1]).toMatchObject({ action: "dismiss" });
  });

  it("blocks mutation after an uncertain action or failed refresh and never retries automatically", async () => {
    const data = receipt();
    fetchMock.mockImplementation((url) => String(url).endsWith("/actions") ? Promise.reject(new Error("lost response")) : Promise.resolve(reply(data)));
    render(<ProgrammeDigestContent ownerKey="operator:root" />);
    fireEvent.click(await screen.findByRole("button", { name: "Dismiss finding" }));
    await screen.findByText(/lost response.*no automatic retry/);
    expect(screen.getByRole("button", { name: "Dismiss finding" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Refresh programmes" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Opt in to bounded notifications" })).toBeEnabled());
    expect(screen.getByRole("button", { name: "Dismiss finding" })).toBeDisabled();
    expect(fetchMock.mock.calls.filter(([, init]) => init.method === "POST")).toHaveLength(1);
  });

  it("rejects unbound finding payloads and clears private receipts when owner changes", async () => {
    const data = receipt(); expect(isProgrammeDigestSnapshot(data)).toBe(true);
    const malformed = receipt(); malformed.digests[0].digest.finding_ids = [];
    expect(isProgrammeDigestSnapshot(malformed)).toBe(false);
    fetchMock.mockResolvedValue(reply(data));
    const view = render(<ProgrammeDigestContent ownerKey="operator:root" />);
    await screen.findByText("An actual source finding");
    fetchMock.mockResolvedValue(reply({ detail: { code: "owner_denied" } }, 403));
    view.rerender(<ProgrammeDigestContent ownerKey="other:root" />);
    await screen.findByText("owner_denied");
    expect(screen.queryByText("An actual source finding")).not.toBeInTheDocument();
  });

  it("binds actual programme receipts inside the existing Inbox and Home Goals surfaces", async () => {
    const data = receipt(); data.digests[0].findings[0].task_id = "native-task";
    const open = vi.fn(); const section = vi.fn();
    fetchMock.mockImplementation((url) => Promise.resolve(reply(String(url).endsWith("/programme-digests") ? data : { items: [] })));
    const view = render(<GuardianInboxPanel currentOwnerPrincipalId="operator" currentRootId="root" pollIntervalMs={0} onOpenTask={open} />);
    fireEvent.click(await screen.findByRole("button", { name: "Review prepared proposal in Work" }));
    expect(open).toHaveBeenCalledWith("native-task");
    view.unmount();
    render(<CockpitHome owner={{ principalId: "operator", sessionId: "root" }} onOpenSection={section} />);
    await screen.findByText(/sources checked 3/);
    expect(screen.getByText(/output · actual-brief/)).toBeInTheDocument();
    expect(screen.getByText(/Next digest/)).toBeInTheDocument();
    expect(screen.queryByText(/Next source run/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Open daily digest in Inbox" }));
    expect(section).toHaveBeenCalledWith("inbox");
    expect(fetchMock.mock.calls.filter(([, init]) => init.method === "POST")).toHaveLength(0);
  });

  it("reads original source and output through authenticated discovery readback without fetching source URLs", async () => {
    const data = receipt();
    fetchMock.mockImplementation((url, init) => {
      expect(init.credentials).toBe("include");
      if (String(url).endsWith("/brief")) {
        expect(String(url)).toContain("/api/goals/goal-one/programmes/programme-one/discovery/actual-job/brief");
        return Promise.resolve(reply({ job_id: "actual-job", programme_id: "programme-one", physical_readback: true, no_learning: true,
          brief: { findings: [{ text: "Original cited text https://untrusted.invalid" }] }, prepared_artifacts: [{ content: "Actual prepared draft" }] }));
      }
      return Promise.resolve(reply(data));
    });
    render(<ProgrammeDigestContent ownerKey="operator:root" />);
    fireEvent.click(await screen.findByRole("button", { name: "Read discovery brief and prepared outputs" }));
    const readback = await screen.findByLabelText("Current discovery source and output readback");
    expect(readback).toHaveTextContent("Original cited text https://untrusted.invalid");
    expect(readback).toHaveTextContent("Actual prepared draft");
    expect(fetchMock.mock.calls).toHaveLength(2);
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });
});
