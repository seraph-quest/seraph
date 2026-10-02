import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CanonicalMemoryPanel } from "./CanonicalMemoryPanel";
import { fetchMemoryRecord } from "../../lib/memoryRecords";

function response(payload: unknown, ok = true, status = ok ? 200 : 403) {
  return { ok, status, json: async () => payload };
}

const memory = {
  id: "memory-1",
  kind: "preference",
  status: "active",
  summary: "Prefers concise updates",
  confidence: 0.9,
  created_at: "2026-09-30T08:00:00Z",
  updated_at: "2026-09-30T08:00:00Z",
  last_confirmed_at: "2026-09-30T08:00:00Z",
  source_session_id: "session-owner",
  safe_provenance: { source_type: "operator_correction" },
  links: [{ kind: "task", label: "Source task", href: "/api/work-board/tasks/task-1", id: "task-1" }],
  content: "Keep updates concise.",
  privacy_boundary: "operator session",
};

describe("CanonicalMemoryPanel", () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => vi.unstubAllGlobals());

  it("searches, opens redacted provenance, and sends reason-bound existing controls", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/memory/records/memory-1")) return Promise.resolve(response({ record: memory }));
      if (url.includes("/api/memory/records?")) return Promise.resolve(response({ records: [memory], next_cursor: null, last_confirmed_at: memory.last_confirmed_at }));
      if ((init?.method ?? "GET") === "POST") return Promise.resolve(response({ receipt_id: "receipt-1" }));
      return Promise.resolve(response({}));
    });
    render(<CanonicalMemoryPanel />);
    fireEvent.click(await screen.findByTestId("memory-record-memory-1"));
    expect(await screen.findByText("Keep updates concise.")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Memory control reason"), { target: { value: "Operator confirmed the correction" } });
    fireEvent.click(screen.getByRole("button", { name: "Audit" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([, init]) => (init as RequestInit | undefined)?.method === "POST")).toBe(true));
    const postCall = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "POST");
    expect(JSON.parse(String((postCall?.[1] as RequestInit).body))).toEqual({
      reason: "Operator confirmed the correction",
      privacy_boundary: "operator session",
    });
    expect(JSON.stringify(postCall?.[1])).not.toContain("source_session_id");
    expect(screen.getByText("Source task")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Archive" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "Redact" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "Delete" })).not.toBeInTheDocument();
  });

  it("shows a truthful forbidden/degraded read without exposing a record", async () => {
    fetchMock.mockResolvedValue(response({ detail: { code: "memory_owner_session_forbidden" } }, false, 403));
    render(<CanonicalMemoryPanel />);
    expect(await screen.findByRole("status")).toHaveTextContent("cannot access that memory record");
    expect(screen.getByText("No canonical records are confirmed for this session.")).toBeInTheDocument();
  });

  it("requires an explicit delete/export acknowledgement and clears a terminal 404 readback", async () => {
    let detailReads = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/memory/records/memory-1")) {
        detailReads += 1;
        return Promise.resolve(detailReads >= 3
          ? response({ detail: { code: "memory_not_found" } }, false, 404)
          : response({ record: memory }));
      }
      if (url.includes("/api/memory/records?")) return Promise.resolve(response({ records: [memory], next_cursor: null }));
      if (init?.method === "POST") return Promise.resolve(response({ receipt_id: "delete-receipt" }));
      return Promise.resolve(response({}));
    });
    render(<CanonicalMemoryPanel />);

    fireEvent.click(await screen.findByTestId("memory-record-memory-1"));
    await screen.findByText("Keep updates concise.");
    fireEvent.change(screen.getByLabelText("Memory control reason"), { target: { value: "Operator requested terminal removal" } });
    fireEvent.click(screen.getByRole("button", { name: "Delete/export record" }));
    expect(screen.getByRole("status")).toHaveTextContent("Confirm the delete/export boundary");

    fireEvent.click(screen.getByRole("checkbox", { name: /permanently tombstones/i }));
    fireEvent.click(screen.getByRole("button", { name: "Delete/export record" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("no longer available"));
    const postCall = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "POST");
    expect(String(postCall?.[0])).toContain("/api/memory/live-controls/actions");
    expect(JSON.parse(String((postCall?.[1] as RequestInit).body))).toEqual({
      action: "propagate_delete_export",
      acknowledged: true,
      memory_id: "memory-1",
      reason: "Operator requested terminal removal",
      privacy_boundary: "operator session",
    });
    expect(screen.getByText("Select a memory record to inspect provenance and controls.")).toBeInTheDocument();
  });

  it("maps the backend metadata projection without dropping safe links or state", async () => {
    const backendRecord = {
      id: "memory-backend",
      kind: "preference",
      status: "superseded",
      summary: "Backend shaped record",
      confidence: 0.8,
      created_at: "2026-09-30T08:00:00Z",
      updated_at: "2026-09-30T08:10:00Z",
      last_confirmed_at: "2026-09-30T08:10:00Z",
      source_session_id: "session-owner",
      safe_provenance: {
        source_types: ["work_board_m5"],
        source_task_id: "task-123",
        artifact_ref: "artifact-123",
        privacy_boundary: "operator_private",
      },
      links: {
        self: "/api/memory/records/memory-backend",
        audit: "/api/memory/audit?memory_id=memory-backend",
        source_task_id: "task-123",
        artifact_ref: "artifact-123",
      },
      content: "Redacted content",
      redaction_state: "available",
      sources: [{ id: "source-1", source_type: "operator", source_session_id: "session-owner", source_message_id: "message-1", snippet: "Redacted source" }],
      source_state: { count: 1, types: ["operator"], verified: true },
      conflict_state: { has_conflicts: true, edges: [{ memory_id: "memory-other", edge_type: "contradicts" }] },
      tombstone_state: "none",
      audit_links: ["/api/memory/audit?memory_id=memory-backend"],
    };
    fetchMock.mockResolvedValue(response(backendRecord));

    const parsed = await fetchMemoryRecord("memory-backend");
    expect(parsed.links).toEqual(expect.arrayContaining([
      expect.objectContaining({ kind: "self", href: "/api/memory/records/memory-backend" }),
      expect.objectContaining({ kind: "audit", href: "/api/memory/audit?memory_id=memory-backend" }),
    ]));
    expect(parsed.audit_links).toEqual([expect.objectContaining({ href: "/api/memory/audit?memory_id=memory-backend" })]);
    expect(parsed.redaction_state).toBe("available");
    expect(parsed.source_state).toEqual(expect.objectContaining({ count: 1, verified: true }));
    expect(parsed.conflict_state).toEqual(expect.objectContaining({ has_conflicts: true }));
    expect(parsed.tombstone).toBeNull();
    expect(parsed.privacy_boundary).toBe("operator_private");
  });

  it("routes typed task and readback links to the owning Work Board before generic link handling", async () => {
    const linkedRecord = {
      ...memory,
      id: "memory-linked",
      summary: "Linked memory",
      safe_provenance: { source_type: "work_board_m5", source_task_id: "task-owner" },
      links: [
        { kind: "task", label: "Source task", id: "task-owner", href: "/api/work-board/tasks/task-owner" },
        { kind: "readback", label: "Verified readback", id: "readback-1", href: "/api/memory/records/readback-1" },
        { kind: "artifact", label: "Verified artifact", id: "artifact-1" },
      ],
    };
    const onOpenTask = vi.fn();
    const onInspectLink = vi.fn();
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/memory/records/memory-linked")) return Promise.resolve(response({ record: linkedRecord }));
      if (url.includes("/api/memory/records?")) return Promise.resolve(response({ records: [linkedRecord], next_cursor: null }));
      return Promise.resolve(response({}));
    });
    render(<CanonicalMemoryPanel onOpenTask={onOpenTask} onInspectLink={onInspectLink} />);
    fireEvent.click(await screen.findByTestId("memory-record-memory-linked"));
    await screen.findByText("Keep updates concise.");

    fireEvent.click(screen.getByRole("button", { name: /Open Work Board memory review · Source task/ }));
    expect(onOpenTask).toHaveBeenCalledWith("task-owner");
    fireEvent.click(screen.getByRole("button", { name: /Open owning Work Board · Verified readback/ }));
    expect(onOpenTask).toHaveBeenCalledWith("task-owner");
    fireEvent.click(screen.getByRole("button", { name: /Open owning Work Board · Verified artifact/ }));
    expect(onOpenTask).toHaveBeenCalledWith("task-owner");
    expect(onInspectLink).not.toHaveBeenCalled();
  });

  it("does not let an older list request overwrite the newer selection", async () => {
    let resolveFirst: (value: ReturnType<typeof response>) => void = () => {};
    let listCalls = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (!url.includes("/api/memory/records?")) return Promise.resolve(response({ record: memory }));
      listCalls += 1;
      if (listCalls === 1) {
        return new Promise<ReturnType<typeof response>>((resolve) => { resolveFirst = resolve; });
      }
      return Promise.resolve(response({ records: [{ ...memory, id: "memory-new", summary: "Newer record" }], next_cursor: null }));
    });
    render(<CanonicalMemoryPanel />);
    const search = screen.getByLabelText("Search canonical memory");
    fireEvent.change(search, { target: { value: "newer" } });
    fireEvent.keyDown(search, { key: "Enter" });
    expect(await screen.findByTestId("memory-record-memory-new")).toBeInTheDocument();
    resolveFirst(response({ records: [{ ...memory, id: "memory-old", summary: "Older record" }], next_cursor: null }));
    await waitFor(() => expect(screen.queryByTestId("memory-record-memory-old")).not.toBeInTheDocument());
  });
});
