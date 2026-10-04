import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { OperatorOwnershipRecovery } from "./OperatorOwnershipRecovery";
import type { OperatorSession } from "./OperatorAuthGate";

const session: OperatorSession = { authenticated: true, principal_id: "operator:single", session_id: "new-root",
  idle_expires_at: "2099-01-01T00:00:00Z", absolute_expires_at: "2099-01-02T00:00:00Z",
  ownership_continuity: "stable", ownership_recovery_action: null, operator_identity_id: "private-identity" };
const records = [{ kind: "goal", record_id: "old-goal", source_session_id: "old-root", label: "Private proved goal", state: "active" }];
function response(payload: unknown, status = 200) { return { ok: status < 400, status, json: async () => payload } as Response; }

describe("OperatorOwnershipRecovery", () => {
  const fetchMock = vi.fn();
  beforeEach(() => { fetchMock.mockReset(); vi.stubGlobal("fetch", fetchMock); });
  afterEach(() => vi.unstubAllGlobals());

  it("requires exact selection, preview and explicit confirmation, preserving uncertain requests", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/preview")) return Promise.resolve(response({ preview_digest: "a".repeat(64), records }));
      if (url.endsWith("/confirm")) return Promise.resolve(response({ journal_id: "journal-1", state: "confirmed" }));
      return Promise.resolve(response({ records, journals: [], truncated: false }));
    });
    render(<OperatorOwnershipRecovery session={session} onSessionChanged={vi.fn()} />);
    expect(fetchMock).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Operator ownership and recovery" }));
    fireEvent.click(screen.getByRole("button", { name: "Review privately recoverable records" }));
    fireEvent.click(await screen.findByLabelText(/goal: Private proved goal/));
    fireEvent.click(screen.getByRole("button", { name: "Preview selected recovery" }));
    const confirm = await screen.findByRole("button", { name: "Confirm selected recovery" });
    expect(confirm).toBeDisabled();
    fireEvent.click(screen.getByLabelText(/Confirm selected reads only/));
    fireEvent.click(confirm);
    await screen.findByText(/Recovery is confirmed/);
    const call = fetchMock.mock.calls.find(([input]) => String(input).endsWith("/confirm"));
    const body = JSON.parse(String(call?.[1]?.body));
    expect(body.selections).toEqual([{ kind: "goal", record_id: "old-goal" }]);
    expect(body.acknowledge_read_only).toBe(true);
    expect(body).not.toHaveProperty("owner_session_id");
    expect(body).not.toHaveProperty("operator_identity_id");
    expect(call?.[1]?.credentials).toBe("include");
    expect(window.localStorage.length).toBe(0);
  });

  it("enrolls only the current proved session and keeps raw code out of displayed metadata", async () => {
    const refresh = vi.fn().mockResolvedValue(true);
    fetchMock.mockResolvedValue(response({ operator_identity_id: "new-identity", recovery_code: "private-one-time-code" }));
    render(<OperatorOwnershipRecovery session={{ ...session, operator_identity_id: null }} onSessionChanged={refresh} />);
    fireEvent.click(screen.getByRole("button", { name: "Operator ownership and recovery" }));
    fireEvent.click(screen.getByRole("button", { name: "Enroll this authenticated scope" }));
    await screen.findByRole("button", { name: "Download recovery code once" });
    expect(screen.queryByText("private-one-time-code")).not.toBeInTheDocument();
    expect(JSON.parse(String(fetchMock.mock.calls[0][1].body))).toEqual({});
    expect(refresh).toHaveBeenCalledOnce();
    expect(window.localStorage.getItem("recovery_code")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Clear pending recovery code" }));
    expect(screen.queryByRole("button", { name: "Download recovery code once" })).not.toBeInTheDocument();
  });

  it("retains last confirmed inventory through degraded metadata", async () => {
    fetchMock.mockResolvedValueOnce(response({ records, journals: [], truncated: false }))
      .mockResolvedValueOnce(response({ detail: { code: "ownership_storage_unavailable" } }, 503));
    render(<OperatorOwnershipRecovery session={session} onSessionChanged={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Operator ownership and recovery" }));
    fireEvent.click(screen.getByRole("button", { name: "Review privately recoverable records" }));
    await screen.findByLabelText(/goal: Private proved goal/);
    fireEvent.click(screen.getByRole("button", { name: "Review privately recoverable records" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("last confirmed selection"));
    expect(screen.getByLabelText(/goal: Private proved goal/)).toBeInTheDocument();
  });
  it("bounds an unresponsive recovery request and keeps confirmed inventory", async () => {
    fetchMock.mockResolvedValueOnce(response({ records, journals: [], truncated: false }))
      .mockImplementationOnce(() => new Promise(() => {}));
    render(<OperatorOwnershipRecovery session={session} onSessionChanged={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Operator ownership and recovery" }));
    fireEvent.click(screen.getByRole("button", { name: "Review privately recoverable records" }));
    await screen.findByLabelText(/goal: Private proved goal/);
    vi.useFakeTimers();
    fireEvent.click(screen.getByRole("button", { name: "Review privately recoverable records" }));
    await act(async () => { await vi.advanceTimersByTimeAsync(15_001); });
    vi.useRealTimers();
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("timed out"));
    expect(screen.getByLabelText(/goal: Private proved goal/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Review privately recoverable records" })).not.toBeDisabled();
    expect(fetchMock.mock.calls[1][1].signal.aborted).toBe(true);
  });

});
