import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { apiFetch } from "../../lib/api";
import type { GoalInfo } from "../../types";
import { BrowserInteractionPanel } from "./BrowserInteractionPanel";
vi.mock("../../lib/api", () => ({ apiFetch: vi.fn() }));
const owner = { ownerPrincipalId: "operator", ownerSessionId: "session" };
const goal = { id: "goal", title: "Goal", status: "active", revision: 2, owner_session_id: "session" } as GoalInfo;
const nodeId = "node-" + "a".repeat(32), digest = "b".repeat(64);
const profiles = { capability_id: "browser.interact.v2", profiles: [{ id: "httpbin.forms.v1", url: "https://httpbin.org/forms/post", name: "HTTPBin public form preview", read_effect: "One public document contact and site access logging", preparation: "offline", submission: "blocked_exact_effect_authority_required", max_actions: 20, max_runtime_seconds: 180, private_field_max_bytes: 2048 }] };
const snapshot = { capability_id: "browser.interact.v2", job_id: "job", revision: 1, fencing_token: 7, status: "running", no_learning: true, live: true, recovery: "none", page: { url: "https://httpbin.org/forms/post", origin: "https://httpbin.org", document_digest: digest, captured_at: "2026-10-08T00:00:00Z", accessible_nodes: [{ node_id: nodeId, role: "textbox", name: "Customer name", actions: ["fill"] }] }, history: [{ sequence: 1, kind: "navigate", status: "completed", phase: "public_read" }] };
const response = (v: unknown, status = 200) => new Response(JSON.stringify(v), { status });
beforeEach(() => { vi.mocked(apiFetch).mockReset(); });
async function open() {
  await screen.findByRole("option", { name: "HTTPBin public form preview" });
  fireEvent.change(screen.getByLabelText("Reviewed browser profile"), { target: { value: "httpbin.forms.v1" } });
  fireEvent.change(screen.getByLabelText("Interaction Goal"), { target: { value: "goal" } });
  fireEvent.click(screen.getByText(/Approve this exact public page contact/));
  fireEvent.click(screen.getByRole("button", { name: "Open reviewed public page" }));
  await screen.findByText("Job job · revision 1");
}
async function prepare() {
  fireEvent.change(screen.getByLabelText("Snapshot field"), { target: { value: nodeId } });
  fireEvent.change(screen.getByLabelText("Private prepared value"), { target: { value: "Private name" } });
  fireEvent.click(screen.getByText("Review this exact bounded action against the current snapshot."));
}
it("requires public read approval then invokes one opaque snapshot-bound private offline action", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response(profiles)).mockResolvedValueOnce(response(snapshot)).mockResolvedValueOnce(response({ ...snapshot, revision: 2, history: [...snapshot.history, { sequence: 2, kind: "fill", status: "completed", phase: "offline_prepare" }] }));
  render(<BrowserInteractionPanel {...owner} goals={[goal]} />); await open(); await prepare();
  fireEvent.click(screen.getByRole("button", { name: "Run reviewed browser action" }));
  await screen.findByText("Job job · revision 2");
  const body = JSON.parse(String(vi.mocked(apiFetch).mock.calls[2][1]?.body));
  expect(body.expected_revision).toBe(1); expect(body.fencing_token).toBe(7); expect(body.private_input).toBe("Private name");
  expect(body.action.locator_ref).toBe(nodeId); expect(body.action.expected_page_revision).toBe(digest);
  expect(body.action.input_value_ref).toMatch(/^value-[a-f0-9]{32}$/); expect(body.action.selector).toBeUndefined();
  expect(screen.getByLabelText("Private prepared value")).toHaveValue("");
  expect(screen.getByLabelText("Snapshot field")).toHaveValue("");
});
it("blocks stale action replay until original history inspection and preserves read-only recovery", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response(profiles)).mockResolvedValueOnce(response(snapshot)).mockResolvedValueOnce(response({ detail: { code: "browser_fresh_snapshot_required" } }, 409)).mockResolvedValueOnce(response({ ...snapshot, revision: 2, live: false, recovery: "read_only_history_new_job_required" }));
  render(<BrowserInteractionPanel {...owner} goals={[goal]} />); await open(); await prepare();
  fireEvent.click(screen.getByRole("button", { name: "Run reviewed browser action" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("browser_fresh_snapshot_required");
  expect(screen.getByRole("button", { name: "Run reviewed browser action" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Inspect original browser history" }));
  await screen.findByText(/read-only history · read_only_history_new_job_required/);
  expect(screen.getByRole("button", { name: "Run reviewed browser action" })).toBeDisabled();
  expect(vi.mocked(apiFetch).mock.calls[3][1]?.method).toBeUndefined();
});
it("retains exact uncertain public read request for explicit idempotent reconciliation", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response(profiles)).mockRejectedValueOnce(Error("Receipt lost")).mockResolvedValueOnce(response(snapshot));
  render(<BrowserInteractionPanel {...owner} goals={[goal]} />);
  await screen.findByRole("option", { name: "HTTPBin public form preview" });
  fireEvent.change(screen.getByLabelText("Reviewed browser profile"), { target: { value: "httpbin.forms.v1" } });
  fireEvent.change(screen.getByLabelText("Interaction Goal"), { target: { value: "goal" } });
  fireEvent.click(screen.getByText(/Approve this exact public page contact/));
  fireEvent.click(screen.getByRole("button", { name: "Open reviewed public page" }));
  await screen.findByRole("alert"); expect(apiFetch).toHaveBeenCalledTimes(2);
  fireEvent.click(screen.getByRole("button", { name: "Reconcile original browser read" }));
  await screen.findByText("Job job · revision 1");
  expect(vi.mocked(apiFetch).mock.calls[1][1]?.body).toBe(vi.mocked(apiFetch).mock.calls[2][1]?.body);
});
it("shows authenticated private extract literally and closes against the original fence", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response(profiles)).mockResolvedValueOnce(response(snapshot)).mockResolvedValueOnce(response({ ...snapshot, revision: 2, preview: [{ field: "name", value: "<script>literal</script>", checked: false }] })).mockResolvedValueOnce(response({ ...snapshot, revision: 3, status: "closed", live: false, recovery: "read_only_history_new_job_required" }));
  render(<BrowserInteractionPanel {...owner} goals={[goal]} />); await open();
  fireEvent.change(screen.getByLabelText("Browser action"), { target: { value: "extract" } });
  fireEvent.click(screen.getByText("Review this exact bounded action against the current snapshot."));
  fireEvent.click(screen.getByRole("button", { name: "Run reviewed browser action" }));
  const preview = await screen.findByRole("region", { name: "Private prepared form preview" }); expect(preview.textContent).toContain("<script>literal</script>"); expect(preview.querySelector("script")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Close private browser context" }));
  await screen.findByText("Job job · revision 3");
  expect(JSON.parse(String(vi.mocked(apiFetch).mock.calls[3][1]?.body))).toEqual({ expected_revision: 2, fencing_token: 7 });
});
it("clears private snapshot and fences late replies after current owner changes", async () => {
  let resolve!: (v: Response) => void;
  vi.mocked(apiFetch).mockResolvedValueOnce(response(profiles)).mockImplementationOnce(() => new Promise(r => { resolve = r; })).mockResolvedValueOnce(response(profiles));
  const mounted = render(<BrowserInteractionPanel {...owner} goals={[goal]} />);
  await screen.findByRole("option", { name: "HTTPBin public form preview" });
  fireEvent.change(screen.getByLabelText("Reviewed browser profile"), { target: { value: "httpbin.forms.v1" } }); fireEvent.change(screen.getByLabelText("Interaction Goal"), { target: { value: "goal" } });
  fireEvent.click(screen.getByText(/Approve this exact public page contact/)); fireEvent.click(screen.getByRole("button", { name: "Open reviewed public page" }));
  mounted.rerender(<BrowserInteractionPanel {...owner} ownerSessionId="foreign" goals={[goal]} />); resolve(response(snapshot));
  await waitFor(() => expect(apiFetch).toHaveBeenCalledTimes(3)); expect(screen.queryByText("Job job · revision 1")).toBeNull();
});
it("discovers durable original jobs after reload without admitting a browser context", async () => {
  const { page: _page, ...historical } = snapshot;
  vi.mocked(apiFetch).mockResolvedValueOnce(response(profiles)).mockResolvedValueOnce(response({ jobs: [{ ...historical, live: false, recovery: "read_only_history_new_job_required" }], has_more: false }));
  render(<BrowserInteractionPanel {...owner} goals={[goal]} />);
  await screen.findByRole("option", { name: "HTTPBin public form preview" });
  fireEvent.click(screen.getByRole("button", { name: "Find original browser jobs" }));
  fireEvent.click(await screen.findByRole("button", { name: "job · running · read-only history" }));
  expect(screen.getByText(/read-only history · read_only_history_new_job_required/)).toBeInTheDocument();
  expect(screen.queryByLabelText("Private prepared value")).toBeNull();
  expect(vi.mocked(apiFetch).mock.calls.every(([, init]) => !init?.method)).toBe(true);
});
