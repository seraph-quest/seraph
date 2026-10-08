import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { TaskEvidencePanel } from "./TaskEvidencePanel";
import type { WorkBoardTask } from "../../types";

const fetchMock = vi.fn();
vi.mock("../../lib/api", () => ({ apiFetch: (...args: unknown[]) =>
  String(args[0]).includes('/execution-binding') ? Promise.resolve({ ok: true, json: async () => ({
    task_id: String(args[0]).includes('task-b') ? 'task-b' : 'task-a', task_revision: 3,
    binding_state: 'unbound', binding_count: 0, applied_result: null }) }) : fetchMock(...args) }));
const task = { task_id: "task-a", task_revision: 3, goal_id: "goal-a", owner_session_id: "root-a", status: "triage" } as WorkBoardTask;
const claim = { source_id: "a".repeat(64), source_kind: "canonical_memory", source_digest: "b".repeat(64),
  version: "2026-10-02", line_start: 1, line_end: 1, text: "Aurora launches October 20", confidence: .95,
  freshness: "recent", memory_id: "memory-a", model_context_allowed: true };
const packet = { revision: 1, digest: "c".repeat(64), claims: [claim], excluded_source_ids: [],
  invalidated_count: 0, blocked_sources: [], allow_model_context: false };
const response = (value: unknown) => ({ ok: true, json: async () => value });

describe("TaskEvidencePanel", () => {
  beforeEach(() => { fetchMock.mockReset(); fetchMock.mockResolvedValue(response(packet)); });

  it("opens a revalidated source, excludes it using packet/task revisions, and labels lexical mode", async () => {
    render(<TaskEvidencePanel task={task} ownerSessionId="root-a" />);
    expect(await screen.findByText(claim.text)).toBeInTheDocument();
    expect(screen.getByText(/lexical retrieval/)).toBeInTheDocument();
    fetchMock.mockResolvedValueOnce(response({ claims: [claim] }));
    fireEvent.click(screen.getByRole("button", { name: "Open source span" }));
    expect(await screen.findByRole("region", { name: "Authorized source spans" })).toHaveTextContent(claim.text);
    fetchMock.mockResolvedValueOnce(response({ ...packet, revision: 2, claims: [], excluded_source_ids: [claim.source_id] }));
    fireEvent.click(screen.getByRole("button", { name: "Exclude source" }));
    await waitFor(() => expect(screen.getByText(/1 sources excluded/)).toBeInTheDocument());
    const init = fetchMock.mock.calls[fetchMock.mock.calls.length - 1][1];
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(init.body)).not.toHaveProperty("allow_model_context");
    expect(JSON.parse(init.body)).toMatchObject({ expected_task_revision: 3, expected_packet_revision: 1,
      excluded_source_ids: [claim.source_id] });
  });

  it("adopts only a rendered exact packet, resets consent, and keeps refresh local", async () => {
    let resolve!: (value: unknown) => void;
    fetchMock.mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
    render(<TaskEvidencePanel task={task} ownerSessionId="root-a" />);
    expect(screen.queryByRole("button", { name: /Adopt reviewed/ })).not.toBeInTheDocument();
    resolve(response(packet));
    await screen.findByText(claim.text);
    fetchMock.mockResolvedValueOnce(response({ ...packet, allow_model_context: true }));
    fireEvent.click(screen.getByRole("button", { name: /Adopt reviewed/ }));
    await screen.findByRole("button", { name: "Reset packet adoption" });
    const adoption = fetchMock.mock.calls[fetchMock.mock.calls.length - 1];
    expect(adoption[0]).toMatch(/\/evidence\/adoption$/);
    expect(JSON.parse(adoption[1].body)).toEqual({ expected_task_revision: 3,
      expected_packet_revision: 1, expected_packet_digest: packet.digest, allow_model_context: true });
    fetchMock.mockResolvedValueOnce(response(packet));
    fireEvent.click(screen.getByRole("button", { name: "Reset packet adoption" }));
    await screen.findByRole("button", { name: /Adopt reviewed/ });
    expect(JSON.parse(fetchMock.mock.calls[fetchMock.mock.calls.length - 1][1].body).allow_model_context).toBe(false);
    fetchMock.mockResolvedValueOnce(response({ ...packet, revision: 2, digest: "d".repeat(64) }));
    fireEvent.click(screen.getByRole("button", { name: "Refresh evidence" }));
    await screen.findByText(/Packet revision 2/);
    expect(JSON.parse(fetchMock.mock.calls[fetchMock.mock.calls.length - 1][1].body)).not.toHaveProperty("allow_model_context");
    expect(screen.getByRole("button", { name: /Adopt reviewed/ })).toBeInTheDocument();
  });

  it("keeps the last reviewed packet when partial metadata is malformed", async () => {
    render(<TaskEvidencePanel task={task} ownerSessionId="root-a" />);
    await screen.findByText(claim.text);
    fetchMock.mockResolvedValueOnce(response({ revision: 2, claims: null }));
    fireEvent.click(screen.getByRole("button", { name: "Refresh evidence" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Evidence is unavailable or changed");
    expect(screen.getByText(claim.text)).toBeInTheDocument();
    expect(screen.getByText(/Packet revision 1/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Refresh evidence" })).toBeEnabled();
  });

  it("corrects through the existing canonical API before refreshing evidence", async () => {
    render(<TaskEvidencePanel task={task} ownerSessionId="root-a" />);
    await screen.findByText(claim.text);
    fireEvent.click(screen.getByRole("button", { name: "Correct canonical memory" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Canonical memory correction" }), { target: { value: "Aurora launches October 25" } });
    fireEvent.click(screen.getByRole("button", { name: "Save correction" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/api/memory/corrections"))).toBe(true));
    const correction = fetchMock.mock.calls.find(([url]) => String(url).endsWith("/api/memory/corrections"))!;
    expect(JSON.parse(correction[1].body)).toMatchObject({ corrects_memory_id: "memory-a", content: "Aurora launches October 25",
      metadata: { goal_id: "goal-a" } });
  });

  it("keeps recovered evidence readable and its mutation controls disabled", async () => {
    render(<TaskEvidencePanel task={task} ownerSessionId="new-root" />);
    await screen.findByText(claim.text);
    expect(screen.getByText(/Recovered evidence is read only/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open source span" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Refresh evidence" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Exclude source" })).toBeDisabled();
  });

  it("discards a stale fetch after selecting another owner/task", async () => {
    let resolve!: (value: unknown) => void;
    fetchMock.mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
    const { rerender } = render(<TaskEvidencePanel task={task} ownerSessionId="root-a" />);
    fetchMock.mockResolvedValueOnce(response({ ...packet, claims: [] }));
    rerender(<TaskEvidencePanel task={{ ...task, task_id: "task-b" }} ownerSessionId="root-a" />);
    await screen.findByText(/No matching authorized evidence/);
    resolve(response(packet));
    await waitFor(() => expect(screen.queryByText(claim.text)).not.toBeInTheDocument());
  });
});
