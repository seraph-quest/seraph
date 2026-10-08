import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { apiFetch } from "../../lib/api";
import type { GoalInfo } from "../../types";
import { DocumentReader } from "./DocumentReader";
vi.mock("../../lib/api", () => ({ apiFetch: vi.fn() }));
const props = { ownerPrincipalId: "operator", ownerSessionId: "session" };
const goal = { id: "goal", title: "Owned Goal", status: "active", revision: 3, owner_session_id: "session" } as GoalInfo;
const digest = "00".repeat(32), id = "11111111-1111-4111-8111-111111111111";
const source = { artifact_id: id, artifact_ref: `document-source:${id}`, revision: 1, state: "reserved", format: "csv", source_digest: digest, goal_id: "goal", goal_revision: 3, reason_code: null, cleanup: "quiescent", writer_kind: null, no_learning: true, provider_contacts: 0 };
const response = (v: unknown, status = 200) => new Response(JSON.stringify(v), { status });
beforeEach(() => { vi.mocked(apiFetch).mockReset(); Object.defineProperty(crypto, "subtle", { configurable: true, value: { digest: vi.fn().mockResolvedValue(new Uint8Array(32).buffer) } }); });
function fill() {
  const file = new File(["name,value\nexample,1"], "table.csv", { type: "text/csv" });
  Object.defineProperty(file, "arrayBuffer", { value: async () => new TextEncoder().encode("name,value\nexample,1").buffer });
  fireEvent.change(screen.getByLabelText("Document Goal"), { target: { value: goal.id } });
  fireEvent.change(screen.getByLabelText("Selected document"), { target: { files: [file] } });
  fireEvent.click(screen.getByText(/Store this exact selected file privately/));
}
async function upload() { fill(); fireEvent.click(screen.getByRole("button", { name: "Store selected document" })); await waitFor(() => expect(screen.getByRole("button", { name: "Read cited document evidence" })).toBeEnabled()); }
it("reserves exact digest, uploads raw file, seals and displays cited inert cells", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response(source)).mockResolvedValueOnce(response({ ...source, revision: 2, state: "uploading" })).mockResolvedValueOnce(response({ ...source, revision: 3, state: "sealed" })).mockResolvedValueOnce(response({ status: "succeeded", provider_contacts: 0, cleanup: "wait_reaped", evidence: { source_digest: digest, no_learning: true, warnings: ["CSV formulas were not evaluated"], sections: [{ source_ref: "source#sheet=CSV&row=1", text: "<script>literal</script>", table_cells: [{ source_ref: "source#sheet=CSV&cell=A1", text: "=1+1", formula: "=1+1", cached_value: null }] }] } }));
  render(<DocumentReader {...props} goals={[goal]} />); await upload();
  const reserve = JSON.parse(String(vi.mocked(apiFetch).mock.calls[0][1]?.body));
  expect(reserve.source.sha256).toBe(digest); expect(reserve.no_learning).toBe(true); expect(reserve.goal_revision).toBe(3);
  expect(vi.mocked(apiFetch).mock.calls[1][1]?.headers).toEqual({ "Content-Type": "application/octet-stream" });
  expect(vi.mocked(apiFetch).mock.calls[1][1]?.body).toBeInstanceOf(File);
  fireEvent.click(screen.getByRole("button", { name: "Read cited document evidence" }));
  await screen.findByRole("region", { name: "Cited document evidence" });
  expect(screen.getByText("<script>literal</script>").querySelector("script")).toBeNull();
  expect(screen.getByText("source#sheet=CSV&cell=A1")).toBeInTheDocument();
  expect(screen.getByText("Formula (inert)")).toBeInTheDocument();
});
it("shows parser block and supports exact revision cleanup after an interrupted upload", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response(source)).mockRejectedValueOnce(Error("Upload disconnected"))
    .mockResolvedValueOnce(response({ ...source, revision: 4, state: "cleanup_required", reason_code: "document_upload_failed_cleanup_required" }))
    .mockResolvedValueOnce(response({ ...source, revision: 5, state: "deleted" }));
  render(<DocumentReader {...props} goals={[goal]} />); fill(); fireEvent.click(screen.getByRole("button", { name: "Store selected document" }));
  await screen.findByRole("alert"); expect(apiFetch).toHaveBeenCalledTimes(2);
  expect(screen.getByRole("button", { name: "Read cited document evidence" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Inspect document source" }));
  await screen.findByText(/Source cleanup_required/);
  fireEvent.click(screen.getByRole("button", { name: "Delete private source and verify cleanup" }));
  await screen.findByText(/Source deleted/);
  expect(String(vi.mocked(apiFetch).mock.calls[3][0])).toContain("expected_revision=4");
  expect(vi.mocked(apiFetch).mock.calls[3][1]?.method).toBe("DELETE");
});
it("does not adopt evidence with a changed source digest", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response(source)).mockResolvedValueOnce(response({ ...source, revision: 2 })).mockResolvedValueOnce(response({ ...source, revision: 3, state: "sealed" })).mockResolvedValueOnce(response({ status: "succeeded", provider_contacts: 0, evidence: { source_digest: "ff".repeat(32), no_learning: true, warnings: [], sections: [] } }));
  render(<DocumentReader {...props} goals={[goal]} />); await upload(); fireEvent.click(screen.getByRole("button", { name: "Read cited document evidence" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("does not match");
  expect(screen.queryByRole("region", { name: "Cited document evidence" })).toBeNull();
});
it("requires current owner Goal and explicit private upload acknowledgment", () => {
  render(<DocumentReader {...props} goals={[goal, { ...goal, id: "foreign", owner_session_id: "other" }]} />);
  expect(screen.getByLabelText("Document Goal").querySelectorAll("option")).toHaveLength(2);
  expect(screen.getByRole("button", { name: "Store selected document" })).toBeDisabled();
  expect(apiFetch).not.toHaveBeenCalled();
});
it("discovers retained private receipts after reload and requires cleanup witness for the original parser", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response({ sources: [{ ...source, revision: 9, state: "sealed", cleanup: "unknown_writer_retained", writer_kind: "parser" }], next_offset: null, no_learning: true })).mockResolvedValueOnce(response({}, 409));
  render(<DocumentReader {...props} goals={[goal]} />);
  fireEvent.click(screen.getByRole("button", { name: "Refresh retained document sources" }));
  const retained = await screen.findByRole("button", { name: /csv · sealed · document-source:/ }); fireEvent.click(retained);
  expect(screen.getByText(/Cleanup is unknown/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Reconcile original document reader cleanup" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Inspect the retained source");
  expect(String(vi.mocked(apiFetch).mock.calls[1][0])).toContain("/reconcile?expected_revision=9");
  expect(screen.getByText(/cleanup unknown_writer_retained/)).toBeInTheDocument();
  expect(vi.mocked(apiFetch).mock.calls.some(([, init]) => init?.method === "PUT")).toBe(false);
});
it("reconciles the exact upload writer without offering parser recovery or restarting upload", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response({ sources: [{ ...source, revision: 6, state: "uploading", cleanup: "unknown_writer_retained", writer_kind: "upload" }], next_offset: null, no_learning: true }))
    .mockResolvedValueOnce(response({ ...source, revision: 7, state: "cleanup_required" }));
  render(<DocumentReader {...props} goals={[goal]} />);
  fireEvent.click(screen.getByRole("button", { name: "Refresh retained document sources" }));
  fireEvent.click(await screen.findByRole("button", { name: /csv · uploading · document-source:/ }));
  expect(screen.queryByRole("button", { name: "Reconcile original document reader cleanup" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Reconcile original document upload cleanup" }));
  await screen.findByText(/Source cleanup_required/);
  expect(String(vi.mocked(apiFetch).mock.calls[1][0])).toContain("/reconcile-upload?expected_revision=6");
  expect(vi.mocked(apiFetch).mock.calls.some(([, init]) => init?.method === "PUT")).toBe(false);
  expect(screen.getByRole("button", { name: "Read cited document evidence" })).toBeDisabled();
});
it("shows unavailable upload proof while retained source inspection and local reading remain usable", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response({ sources: [{ ...source, state: "sealed" }], next_offset: null, no_learning: true, upload_readiness: "blocked" }));
  render(<DocumentReader {...props} goals={[goal]} />);
  fireEvent.click(screen.getByRole("button", { name: "Refresh retained document sources" }));
  fireEvent.click(await screen.findByRole("button", { name: /csv · sealed · document-source:/ }));
  expect(screen.getByText(/New uploads are blocked until this host proves/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Inspect document source" })).toBeEnabled();
  expect(screen.getByRole("button", { name: "Read cited document evidence" })).toBeEnabled();
});
