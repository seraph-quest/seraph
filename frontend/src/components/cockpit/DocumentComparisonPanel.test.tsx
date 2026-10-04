import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useLayoutEffect, type ComponentProps } from "react";
import { beforeEach, expect, it, vi } from "vitest";
import { apiFetch } from "../../lib/api";
import type { WorkBoardTask } from "../../types";
import { DocumentComparisonPanel } from "./DocumentComparisonPanel";
vi.mock("../../lib/api", () => ({ apiFetch: vi.fn() }));
const task = { task_id: "document-one", capability_id: "work.document-compare.v1", status: "done", task_revision: 5 } as WorkBoardTask;
const props = { task, ownerPrincipalId: "operator:one", ownerSessionId: "session-one" };
beforeEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); vi.mocked(apiFetch).mockReset(); sessionStorage.clear(); });
it("prepares literal verified CSV bytes and revokes replaced and unmounted Blob URLs", async () => {
  const csv = 'SKU,FORMULA\nPEN-01,"2 * 3.75"\n';
  vi.mocked(apiFetch).mockImplementation(async () => new Response(JSON.stringify({ text: csv })));
  const create = vi.fn().mockReturnValueOnce("blob:first").mockReturnValueOnce("blob:second");
  const revoke = vi.fn();
  vi.stubGlobal("URL", class extends URL { static createObjectURL = create; static revokeObjectURL = revoke; });
  const view = render(<DocumentComparisonPanel {...props} />);
  fireEvent.click(screen.getByRole("button", { name: "Prepare verified derived CSV" }));
  const anchor = await screen.findByRole("link", { name: "Save verified derived CSV" });
  expect(anchor).toHaveAttribute("href", "blob:first");
  expect(anchor).toHaveAttribute("download", "invoice-comparison.csv");
  expect(vi.mocked(apiFetch).mock.calls[0][0]).toContain(`/tasks/${task.task_id}/document-output/csv`);
  const blob = create.mock.calls[0][0] as Blob;
  const bytes = await new Promise<string>(resolve => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result)); reader.readAsText(blob); });
  expect(bytes).toBe(csv);
  fireEvent.click(screen.getByRole("button", { name: "Prepare verified derived CSV" }));
  await waitFor(() => expect(anchor).toHaveAttribute("href", "blob:second"));
  expect(revoke).toHaveBeenCalledWith("blob:first");
  view.unmount(); expect(revoke).toHaveBeenCalledWith("blob:second");
});
it("removes private output and revokes its Blob URL on owner, task, and logout changes", async () => {
  const create = vi.fn().mockReturnValue("blob:private"), revoke = vi.fn();
  vi.stubGlobal("URL", class extends URL { static createObjectURL = create; static revokeObjectURL = revoke; });
  vi.mocked(apiFetch).mockImplementation(async () => new Response(JSON.stringify({ text: "SKU,QTY\nPEN-01,2\n" })));
  const view = render(<DocumentComparisonPanel {...props} />);
  fireEvent.click(screen.getByRole("button", { name: "Read verified cited report" }));
  await screen.findByLabelText("Verified cited document report");
  fireEvent.click(screen.getByRole("button", { name: "Prepare verified derived CSV" }));
  await screen.findByRole("link", { name: "Save verified derived CSV" });
  view.rerender(<DocumentComparisonPanel {...props} ownerSessionId="session-two" />);
  expect(screen.queryByLabelText("Verified cited document report")).toBeNull();
  expect(screen.queryByRole("link", { name: "Save verified derived CSV" })).toBeNull();
  expect(revoke).toHaveBeenCalledWith("blob:private");
  fireEvent.click(screen.getByRole("button", { name: "Prepare verified derived CSV" }));
  await screen.findByRole("link", { name: "Save verified derived CSV" });
  fireEvent.click(screen.getByRole("button", { name: "Read verified cited report" }));
  await screen.findByLabelText("Verified cited document report");
  view.rerender(<DocumentComparisonPanel {...props} task={{ ...task, task_id: "document-two" }} />);
  expect(screen.queryByLabelText("Verified cited document report")).toBeNull();
  expect(screen.queryByRole("link", { name: "Save verified derived CSV" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Prepare verified derived CSV" }));
  await screen.findByRole("link", { name: "Save verified derived CSV" });
  fireEvent.click(screen.getByRole("button", { name: "Read verified cited report" }));
  await screen.findByLabelText("Verified cited document report");
  view.rerender(<DocumentComparisonPanel task={task} />);
  expect(screen.queryByLabelText("Verified cited document report")).toBeNull();
  expect(screen.queryByRole("link", { name: "Save verified derived CSV" })).toBeNull();
});
it("renders report markup as literal text", async () => {
  vi.mocked(apiFetch).mockResolvedValue(new Response(JSON.stringify({ text: "<script>privateInvoice()</script>\nPEN-01: page 1, extraction line 3; CSV row 2" })));
  const view = render(<DocumentComparisonPanel {...props} />);
  fireEvent.click(screen.getByRole("button", { name: "Read verified cited report" }));
  await screen.findByText(/<script>privateInvoice/);
  expect(view.container.querySelector("script")).toBeNull();
});
it("clears cached report and CSV on a typed authority denial and on unavailable inspector readback", async () => {
  const revoke = vi.fn();
  vi.stubGlobal("URL", class extends URL { static createObjectURL = vi.fn().mockReturnValue("blob:authority-bound"); static revokeObjectURL = revoke; });
  let denied = false;
  vi.mocked(apiFetch).mockImplementation(async url => {
    if (String(url).endsWith("document-comparison")) return new Response(JSON.stringify({ status: "succeeded", reason_code: "stale_goal_revision", report_available: false }));
    return denied ? new Response(JSON.stringify({ detail: { code: "stale_goal_revision" } }), { status: 409 }) : new Response(JSON.stringify({ text: "private literal output" }));
  });
  render(<DocumentComparisonPanel {...props} />);
  fireEvent.click(screen.getByRole("button", { name: "Read verified cited report" }));
  await screen.findByText("private literal output");
  fireEvent.click(screen.getByRole("button", { name: "Prepare verified derived CSV" }));
  await screen.findByRole("link", { name: "Save verified derived CSV" });
  denied = true;
  fireEvent.click(screen.getByRole("button", { name: "Read verified cited report" }));
  await screen.findByRole("alert");
  expect(screen.queryByText("private literal output")).toBeNull();
  expect(screen.queryByRole("link", { name: "Save verified derived CSV" })).toBeNull();
  expect(revoke).toHaveBeenCalledWith("blob:authority-bound");
  expect(screen.getByRole("button", { name: "Prepare verified derived CSV" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Read original parser and recovery state" }));
  await screen.findByText(/succeeded · stale_goal_revision/);
  expect(screen.getByRole("button", { name: "Read verified cited report" })).toBeDisabled();
});
function CommitInspector({ panelProps, inspect }: { panelProps: ComponentProps<typeof DocumentComparisonPanel>; inspect: () => void }) {
  useLayoutEffect(inspect);
  return <DocumentComparisonPanel {...panelProps} />;
}
it.each([
  { label: "principal", next: { ...props, ownerPrincipalId: "operator:two" } },
  { label: "session", next: { ...props, ownerSessionId: "session-two" } },
  { label: "task", next: { ...props, task: { ...task, task_id: "document-two" } } },
  { label: "logout", next: { task } },
])("hides private report and CSV before passive cleanup on $label change", async ({ next }) => {
  vi.stubGlobal("URL", class extends URL { static createObjectURL = vi.fn().mockReturnValue("blob:old-scope"); static revokeObjectURL = vi.fn(); });
  vi.mocked(apiFetch).mockImplementation(async () => new Response(JSON.stringify({ text: "old scope private bytes" })));
  const atCommit: { report: string | null; link: boolean }[] = [];
  const inspect = () => { atCommit.push({ report: document.querySelector("pre")?.textContent ?? null, link: Boolean(document.querySelector("a[download]")) }); };
  const view = render(<CommitInspector panelProps={props} inspect={inspect} />);
  fireEvent.click(screen.getByRole("button", { name: "Read verified cited report" }));
  await screen.findByLabelText("Verified cited document report");
  fireEvent.click(screen.getByRole("button", { name: "Prepare verified derived CSV" }));
  await screen.findByRole("link", { name: "Save verified derived CSV" });
  atCommit.length = 0;
  view.rerender(<CommitInspector panelProps={next} inspect={inspect} />);
  expect(atCommit[0]).toEqual({ report: null, link: false });
});
