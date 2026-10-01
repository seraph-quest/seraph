import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { CalendarConnectionPanel } from "./CalendarConnectionPanel";

function response(payload: unknown, ok = true, status = ok ? 200 : 500) {
  return { ok, status, json: async () => payload };
}

const connection = {
  connection_id: "connection-1",
  service: "calendar_readonly",
  label: "Work calendar",
  credential_fingerprint: "fingerprint-1",
  state: "active",
  revision: 2,
  created_at: "2026-09-30T09:00:00Z",
  updated_at: "2026-09-30T09:30:00Z",
};

describe("CalendarConnectionPanel", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    vi.stubGlobal("confirm", vi.fn(() => true));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("keeps credentials write-only and verifies only after an explicit action", async () => {
    fetchMock
      .mockResolvedValueOnce(response({ connections: [] }))
      .mockResolvedValueOnce(response({ connection }))
      .mockResolvedValueOnce(response({
        connection,
        calendars: [{ calendar_id: "calendar-1", summary: "Work" }],
        calendar_list_revision: "a".repeat(64),
        pages_read: 1,
        truncated: false,
        provider_status: "verified",
      }));
    render(<CalendarConnectionPanel />);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    fireEvent.change(screen.getByLabelText("Label"), { target: { value: "Work calendar" } });
    fireEvent.change(screen.getByLabelText("Client ID"), { target: { value: "client-id" } });
    fireEvent.change(screen.getByLabelText("Refresh token"), { target: { value: "refresh-secret-value" } });
    fireEvent.click(screen.getByRole("button", { name: "Save connection" }));
    await waitFor(() => expect(screen.getByText("Work calendar")).toBeInTheDocument());
    expect(screen.queryByText("refresh-secret-value")).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([, init]) => (init as RequestInit | undefined)?.method === "POST")).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByRole("button", { name: "Verify and list calendars" }));
    await waitFor(() => expect(screen.getByText(/Verified revision 2/)).toBeInTheDocument());
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("preserves the exact setup body and idempotency key after an unknown outcome", async () => {
    fetchMock
      .mockResolvedValueOnce(response({ connections: [] }))
      .mockRejectedValueOnce(new Error("network closed"))
      .mockResolvedValueOnce(response({ connection }));
    render(<CalendarConnectionPanel />);
    fireEvent.change(screen.getByLabelText("Label"), { target: { value: "Work calendar" } });
    fireEvent.change(screen.getByLabelText("Client ID"), { target: { value: "client-id" } });
    fireEvent.change(screen.getByLabelText("Refresh token"), { target: { value: "refresh-secret-value" } });
    fireEvent.click(screen.getByRole("button", { name: "Save connection" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("unconfirmed"));
    const firstPost = fetchMock.mock.calls[1];
    fireEvent.click(screen.getByRole("button", { name: "Retry exact setup" }));
    await waitFor(() => expect(screen.getByText("Work calendar")).toBeInTheDocument());
    const retryPost = fetchMock.mock.calls[2];
    expect(JSON.parse(String((firstPost?.[1] as RequestInit).body))).toEqual(JSON.parse(String((retryPost?.[1] as RequestInit).body)));
    expect((firstPost?.[1] as RequestInit).body).toBe((retryPost?.[1] as RequestInit).body);
  });

  it("clears the write-only draft when the authenticated session is rejected", async () => {
    fetchMock
      .mockResolvedValueOnce(response({ connections: [] }))
      .mockResolvedValueOnce(response({ detail: { code: "auth_required", message: "Sign in again", recovery_action: "login" } }, false, 401));
    render(<CalendarConnectionPanel />);
    fireEvent.change(screen.getByLabelText("Label"), { target: { value: "Work calendar" } });
    fireEvent.change(screen.getByLabelText("Client ID"), { target: { value: "client-id" } });
    fireEvent.change(screen.getByLabelText("Client secret (optional)"), { target: { value: "client-secret" } });
    fireEvent.change(screen.getByLabelText("Refresh token"), { target: { value: "refresh-secret-value" } });
    fireEvent.click(screen.getByRole("button", { name: "Save connection" }));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/session expired/i));
    expect(screen.queryByDisplayValue("client-id")).not.toBeInTheDocument();
    expect(screen.queryByDisplayValue("client-secret")).not.toBeInTheDocument();
    expect(screen.queryByDisplayValue("refresh-secret-value")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save connection" })).toBeInTheDocument();
  });

  it("restores an unknown verify request across remount for the same authenticated session", async () => {
    fetchMock
      .mockResolvedValueOnce(response({ connections: [connection] }))
      .mockRejectedValueOnce(new Error("network closed"))
      .mockResolvedValueOnce(response({ connections: [connection] }))
      .mockResolvedValueOnce(response({
        connection,
        calendars: [{ calendar_id: "calendar-1", summary: "Work" }],
        calendar_list_revision: "a".repeat(64),
        pages_read: 1,
        truncated: false,
        provider_status: "verified",
      }));
    const first = render(<CalendarConnectionPanel ownerPrincipalId="operator:one" ownerSessionId="auth-session-one" />);
    await screen.findByText("Work calendar");
    fireEvent.click(screen.getByRole("button", { name: "Verify and list calendars" }));
    await screen.findByRole("alert");
    const firstBody = (fetchMock.mock.calls[1]?.[1] as RequestInit).body;
    first.unmount();

    render(<CalendarConnectionPanel ownerPrincipalId="operator:one" ownerSessionId="auth-session-one" />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Retry exact verify" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Retry exact verify" }));
    await screen.findByText(/Verified revision 2/);
    expect((fetchMock.mock.calls[3]?.[1] as RequestInit).body).toBe(firstBody);
  });

  it("does not restore a pending control to a different authenticated session", async () => {
    fetchMock
      .mockResolvedValueOnce(response({ connections: [connection] }))
      .mockRejectedValueOnce(new Error("network closed"))
      .mockResolvedValueOnce(response({ connections: [connection] }));
    const first = render(<CalendarConnectionPanel ownerPrincipalId="operator:one" ownerSessionId="auth-session-two" />);
    await screen.findByText("Work calendar");
    fireEvent.click(screen.getByRole("button", { name: "Verify and list calendars" }));
    await screen.findByRole("alert");
    first.unmount();

    render(<CalendarConnectionPanel ownerPrincipalId="operator:one" ownerSessionId="auth-session-three" />);
    await screen.findByText("Work calendar");
    expect(screen.queryByRole("button", { name: "Retry exact verify" })).not.toBeInTheDocument();
  });

  it("keeps a 429 control intent and disables competing connection mutations while it is in flight", async () => {
    let resolveVerify: ((value: ReturnType<typeof response>) => void) | undefined;
    const verifyResponse = new Promise<ReturnType<typeof response>>((resolve) => { resolveVerify = resolve; });
    fetchMock
      .mockResolvedValueOnce(response({ connections: [connection] }))
      .mockImplementationOnce(() => verifyResponse);
    render(<CalendarConnectionPanel ownerPrincipalId="operator:rate" ownerSessionId="auth-session-rate" />);
    await screen.findByText("Work calendar");
    fireEvent.click(screen.getByRole("button", { name: "Verify and list calendars" }));
    expect(screen.getByRole("button", { name: "Revoke" })).toBeDisabled();
    resolveVerify?.(response({ detail: { code: "rate_limited", message: "retry later" } }, false, 429));
    await screen.findByRole("alert");
    expect(screen.getByRole("button", { name: "Retry exact verify" })).toBeInTheDocument();
  });

  it("retains an exact verify intent when the bounded request times out", async () => {
    fetchMock.mockResolvedValueOnce(response({ connections: [connection] }));
    fetchMock.mockImplementationOnce((_input: RequestInfo | URL, init?: RequestInit) => new Promise((_resolve, reject) => {
      init?.signal?.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
    }));
    render(<CalendarConnectionPanel ownerPrincipalId="operator:timeout" ownerSessionId="auth-session-timeout" />);
    await screen.findByText("Work calendar");
    vi.useFakeTimers();
    try {
      fireEvent.click(screen.getByRole("button", { name: "Verify and list calendars" }));
      await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
      expect(screen.getByRole("button", { name: "Retry exact verify" })).toBeInTheDocument();
      expect(screen.getByRole("alert")).toHaveTextContent(/unconfirmed|timed out/i);
    } finally {
      vi.useRealTimers();
    }
  });

  it("requires explicit metadata reconciliation before allowing a stale verify to start a new attempt", async () => {
    fetchMock
      .mockResolvedValueOnce(response({ connections: [connection] }))
      .mockResolvedValueOnce(response({ detail: { code: "calendar_connection_revision_stale", message: "revision changed" } }, false, 409))
      .mockResolvedValueOnce(response({ connections: [{ ...connection, revision: 3 }] }))
      .mockResolvedValueOnce(response({
        connection: { ...connection, revision: 3 },
        calendars: [{ calendar_id: "calendar-1", summary: "Work" }],
        calendar_list_revision: "a".repeat(64),
        pages_read: 1,
        truncated: false,
        provider_status: "verified",
      }));
    render(<CalendarConnectionPanel ownerPrincipalId="operator:stale" ownerSessionId="auth-session-stale" />);
    await screen.findByText("Work calendar");
    fireEvent.click(screen.getByRole("button", { name: "Verify and list calendars" }));
    await screen.findByRole("alert");
    expect(screen.queryByRole("button", { name: "Start new verify attempt" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Refresh metadata" }));
    await screen.findByRole("button", { name: "Start new verify attempt" });
    fireEvent.click(screen.getByRole("button", { name: "Start new verify attempt" }));
    fireEvent.click(screen.getByRole("button", { name: "Verify and list calendars" }));
    await screen.findByText(/Verified revision 3/);
    expect(fetchMock).toHaveBeenCalledTimes(4);
  });

  it("shows the active binding and its bounded unknown occurrence receipt", async () => {
    const schedule = {
      binding_id: "binding-1",
      scheduled_job_id: "job-1",
      capability_id: "calendar.observe_due_events.v1",
      action_type: "calendar.observe_due_events.v1",
      goal_id: "goal-1",
      goal_revision: 3,
      input_artifact_id: "artifact-1",
      input_digest: "a".repeat(64),
      consent_kind: "calendar_read",
      consent_id: "consent-1",
      consent_revision: 2,
      consent_digest: "b".repeat(64),
      cadence: { kind: "hourly", timezone: "UTC", daily_hour: null, daily_minute: null },
      binding_revision: 2,
      expires_at: "2026-10-01T12:00:00Z",
      state: "active",
      last_slot_utc: null,
      created_at: "2026-09-30T09:00:00Z",
      updated_at: "2026-09-30T09:30:00Z",
      latest_occurrence: {
        occurrence_id: "occurrence-1",
        binding_revision: 2,
        slot_utc: "2026-09-30T10:00:00Z",
        state: "unknown",
        task_id: null,
        job_id: "run-1",
        failure_code: "provider_timeout<script>",
        recovery_action: "reconcile_external_effect",
        updated_at: "2026-09-30T10:05:00Z",
      },
    };
    fetchMock
      .mockResolvedValueOnce(response({ connections: [] }))
      .mockResolvedValueOnce(response({ bindings: [schedule] }));
    const { container } = render(<CalendarConnectionPanel />);
    await screen.findByText("No calendar connection is configured.");
    fireEvent.click(screen.getByRole("button", { name: "Load schedules" }));
    const receipt = await screen.findByRole("region", { name: "Latest governed occurrence" });
    expect(screen.getByText("active · revision 2")).toBeInTheDocument();
    expect(receipt).toHaveTextContent("Latest occurrence: unknown");
    expect(receipt).toHaveTextContent("provider_timeout<script>");
    expect(receipt).toHaveTextContent("reconcile_external_effect");
    expect(container.querySelector("script")).toBeNull();
  });
});
