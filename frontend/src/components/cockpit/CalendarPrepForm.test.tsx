import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { CalendarPrepResponse, GoalInfo } from "../../types";
import { CalendarPrepForm } from "./CalendarPrepForm";

function response(payload: unknown, ok = true, status = ok ? 200 : 500) {
  return { ok, status, json: async () => payload };
}

const digest = "a".repeat(64);
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
const goal: GoalInfo = {
  id: "goal-1",
  parent_id: null,
  path: "/goal-1",
  level: "root",
  title: "Prepare meetings",
  description: "",
  status: "active",
  domain: "work",
  start_date: null,
  due_date: null,
  sort_order: 0,
  revision: 4,
};

describe("CalendarPrepForm", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("walks the explicit verify, finite consent, event, and prep sequence", async () => {
    const onCreated = vi.fn();
    const selectedEventListRevision = "b".repeat(64);
    fetchMock
      .mockResolvedValueOnce(response({ connections: [connection] }))
      .mockResolvedValueOnce(response({ connection, calendars: [{ calendar_id: "calendar-1", summary: "Work" }], calendar_list_revision: digest, pages_read: 1, truncated: false, provider_status: "verified" }))
      .mockResolvedValueOnce(response({ consent: { consent_id: "consent-1", connection_id: "connection-1", connection_revision: 2, goal_id: "goal-1", goal_revision: 4, allowed_fields: ["summary", "start", "end", "location"], window_minutes: 1440, max_events: 20, allow_remote_model: true, expires_at: "2026-10-01T09:00:00Z", state: "active", revision: 1, consent_digest: digest, created_at: "2026-09-30T09:00:00Z", updated_at: "2026-09-30T09:00:00Z" } }))
      .mockResolvedValueOnce(response({ events: [{ event_binding_id: "binding-1", event_binding_revision: 3, event_key: digest, event_revision: digest, calendar_list_revision: selectedEventListRevision, summary: "Planning", start: "2026-09-30T12:00:00Z", end: "2026-09-30T13:00:00Z", location: "Room 1", description: null, attendees: null }], consent_id: "consent-1", consent_revision: 1, connection_revision: 2, calendar_list_revision: digest, fetched_at: "2026-09-30T09:01:00Z", pages_read: 1, truncated: false }))
      .mockResolvedValueOnce(response({ input_artifact: { artifact_id: "artifact-1", typed_input_ref: "workspace-json:artifacts/work-board/inputs/artifact-1.json", typed_input_digest: digest, capability_id: "calendar.meeting-prep.v1", goal_id: "goal-1", goal_revision: 4, expires_at: "2026-10-01T09:00:00Z" }, task: { task_id: "task-1", title: "Prepare meeting", goal_id: "goal-1", goal_revision: 4, input_artifact_id: "artifact-1", capability_id: "calendar.meeting-prep.v1" }, idempotent_replay: false }));
    render(<CalendarPrepForm goals={[goal]} onCreated={onCreated} onClose={vi.fn()} />);
    await screen.findByText("Work calendar · active · revision 2");
    fireEvent.click(screen.getByRole("button", { name: "Verify calendars" }));
    await screen.findByRole("option", { name: "Work" });
    fireEvent.click(screen.getByRole("checkbox", { name: /Allow the governed OpenRouter model/i }));
    fireEvent.click(screen.getByRole("button", { name: "Create finite consent and read events" }));
    await screen.findByText(/Consent consent-1/);
    await screen.findByRole("option", { name: /Planning/ });
    expect(String(fetchMock.mock.calls[3]?.[0])).toContain("/api/calendar/connections/connection-1/events?consent_id=consent-1");
    fireEvent.change(screen.getByLabelText("Event binding"), { target: { value: "binding-1" } });
    fireEvent.click(screen.getByRole("button", { name: "Prepare meeting" }));
    await waitFor(() => expect(onCreated).toHaveBeenCalled());
    const prepCall = fetchMock.mock.calls[4];
    const prepBody = JSON.parse(String((prepCall?.[1] as RequestInit).body));
    expect(prepBody).toMatchObject({ title: "Prepare for meeting", input: { consent_id: "consent-1", event_binding_id: "binding-1", goal_id: "goal-1", goal_revision: 4, calendar_list_revision: selectedEventListRevision, purpose: "bounded preparation request" } });
    expect(prepBody.input.calendar_id).toBeUndefined();
    expect(prepBody.input.event_key).toBeUndefined();
    expect(prepBody.input.idempotency_key).toBeUndefined();
  });

  it("preserves an unknown prep intent and does not post a second task automatically", async () => {
    fetchMock
      .mockResolvedValueOnce(response({ connections: [connection] }))
      .mockResolvedValueOnce(response({ connection, calendars: [{ calendar_id: "calendar-1", summary: "Work" }], calendar_list_revision: digest, pages_read: 1, truncated: false, provider_status: "verified" }))
      .mockResolvedValueOnce(response({ consent: { consent_id: "consent-1", connection_id: "connection-1", connection_revision: 2, goal_id: "goal-1", goal_revision: 4, allowed_fields: ["summary", "start", "end", "location"], window_minutes: 1440, max_events: 20, allow_remote_model: true, expires_at: "2026-10-01T09:00:00Z", state: "active", revision: 1, consent_digest: digest, created_at: "2026-09-30T09:00:00Z", updated_at: "2026-09-30T09:00:00Z" } }))
      .mockResolvedValueOnce(response({ events: [{ event_binding_id: "binding-1", event_binding_revision: 3, event_key: digest, event_revision: digest, calendar_list_revision: digest, summary: "Planning", start: "2026-09-30T12:00:00Z", end: "2026-09-30T13:00:00Z", location: null, description: null, attendees: null }], consent_id: "consent-1", consent_revision: 1, connection_revision: 2, calendar_list_revision: digest, fetched_at: "2026-09-30T09:01:00Z", pages_read: 1, truncated: false }))
      .mockRejectedValueOnce(new Error("network closed"))
      .mockResolvedValueOnce(response({ input_artifact: { artifact_id: "artifact-1", typed_input_ref: "workspace-json:artifacts/work-board/inputs/artifact-1.json", typed_input_digest: digest, capability_id: "calendar.meeting-prep.v1", goal_id: "goal-1", goal_revision: 4, expires_at: "2026-10-01T09:00:00Z" }, task: { task_id: "task-1", title: "Prepare meeting", goal_id: "goal-1", goal_revision: 4, input_artifact_id: "artifact-1", capability_id: "calendar.meeting-prep.v1" }, idempotent_replay: false }));
    render(<CalendarPrepForm goals={[goal]} onCreated={vi.fn()} onClose={vi.fn()} />);
    await screen.findByText("Work calendar · active · revision 2");
    fireEvent.click(screen.getByRole("button", { name: "Verify calendars" }));
    await screen.findByRole("option", { name: "Work" });
    fireEvent.click(screen.getByRole("checkbox", { name: /Allow the governed OpenRouter model/i }));
    fireEvent.click(screen.getByRole("button", { name: "Create finite consent and read events" }));
    await screen.findByText(/Consent consent-1/);
    fireEvent.change(screen.getByLabelText("Event binding"), { target: { value: "binding-1" } });
    fireEvent.click(screen.getByRole("button", { name: "Prepare meeting" }));
    await waitFor(() => expect(screen.getByText(/outcome is unconfirmed/i)).toBeInTheDocument());
    expect(fetchMock).toHaveBeenCalledTimes(5);
    fireEvent.click(screen.getByRole("button", { name: "Retry exact request" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(6));
    expect((fetchMock.mock.calls[4]?.[1] as RequestInit).body).toBe((fetchMock.mock.calls[5]?.[1] as RequestInit).body);
  });

  it("restores a confirmed prep receipt so an unknown schedule can retry after remount", async () => {
    const prepReceipt = {
      input_artifact: {
        artifact_id: "artifact-1",
        typed_input_ref: "workspace-json:artifacts/work-board/inputs/artifact-1.json",
        typed_input_digest: digest,
        capability_id: "calendar.meeting-prep.v1",
        goal_id: "goal-1",
        goal_revision: 4,
        expires_at: "2026-10-01T09:00:00Z",
      },
      task: { task_id: "task-1", title: "Prepare meeting", goal_id: "goal-1", goal_revision: 4, input_artifact_id: "artifact-1", capability_id: "calendar.meeting-prep.v1" },
      idempotent_replay: false,
    } as unknown as CalendarPrepResponse;
    const scheduleRequest = {
      schema_version: 1 as const,
      consent_id: "consent-1",
      goal_id: "goal-1",
      goal_revision: 4,
      calendar_id: "calendar-1",
      cadence: { kind: "daily" as const, timezone: "UTC", daily_hour: 9, daily_minute: 0 },
      expires_at: "2026-10-01T09:00:00Z",
      idempotency_key: "calendar-schedule:original",
    };
    fetchMock
      .mockResolvedValueOnce(response({ connections: [connection] }))
      .mockResolvedValueOnce(response({ binding: {
        binding_id: "binding-1",
        scheduled_job_id: "scheduled-job-1",
        capability_id: "calendar.meeting-prep.v1",
        action_type: "prepare",
        goal_id: "goal-1",
        goal_revision: 4,
        input_artifact_id: "artifact-1",
        input_digest: digest,
        consent_kind: "calendar_readonly",
        consent_id: "consent-1",
        consent_revision: 1,
        consent_digest: digest,
        cadence: scheduleRequest.cadence,
        binding_revision: 1,
        expires_at: "2026-10-01T09:00:00Z",
        state: "active",
        last_slot_utc: null,
        created_at: "2026-09-30T09:00:00Z",
        updated_at: "2026-09-30T09:00:00Z",
        latest_occurrence: null,
      } }));
    const pending = { intent: { kind: "schedule" as const, request: scheduleRequest }, confirmedPrep: prepReceipt };
    const onCreated = vi.fn();
    render(<CalendarPrepForm goals={[goal]} initialPending={pending} onCreated={onCreated} onClose={vi.fn()} />);
    await screen.findByText(/Preparation created: task task-1/);
    fireEvent.click(screen.getByRole("button", { name: "Retry schedule" }));
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(prepReceipt.task, prepReceipt));
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(String((fetchMock.mock.calls[1]?.[1] as RequestInit).body)).toBe(JSON.stringify(scheduleRequest));
  });

  it("does not let a late verify response for connection A overwrite connection B", async () => {
    const connectionB = { ...connection, connection_id: "connection-2", label: "Personal calendar" };
    let resolveA: ((value: ReturnType<typeof response>) => void) | undefined;
    const verifyA = new Promise<ReturnType<typeof response>>((resolve) => { resolveA = resolve; });
    fetchMock
      .mockResolvedValueOnce(response({ connections: [connection, connectionB] }))
      .mockImplementationOnce(() => verifyA)
      .mockResolvedValueOnce(response({ connection: connectionB, calendars: [{ calendar_id: "calendar-b", summary: "Personal" }], calendar_list_revision: digest, pages_read: 1, truncated: false, provider_status: "verified" }));
    render(<CalendarPrepForm goals={[goal]} onCreated={vi.fn()} onClose={vi.fn()} />);
    await screen.findByText("Personal calendar · active · revision 2");
    fireEvent.change(screen.getByLabelText("Connection"), { target: { value: "connection-1" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify calendars" }));
    fireEvent.change(screen.getByLabelText("Connection"), { target: { value: "connection-2" } });
    fireEvent.click(screen.getByRole("button", { name: "Verify calendars" }));
    await screen.findByRole("option", { name: "Personal" });
    resolveA?.(response({ connection, calendars: [{ calendar_id: "calendar-a", summary: "Work" }], calendar_list_revision: digest, pages_read: 1, truncated: false, provider_status: "verified" }));
    await waitFor(() => expect(screen.getByRole("option", { name: "Personal" })).toBeInTheDocument());
    expect(screen.queryByRole("option", { name: "Work" })).not.toBeInTheDocument();
  });

  it("keeps mandatory consent fields selected and blocks stale or expired consent admission", async () => {
    fetchMock
      .mockResolvedValueOnce(response({ connections: [connection] }))
      .mockResolvedValueOnce(response({ connection, calendars: [{ calendar_id: "calendar-1", summary: "Work" }], calendar_list_revision: digest, pages_read: 1, truncated: false, provider_status: "verified" }));
    render(<CalendarPrepForm goals={[goal]} onCreated={vi.fn()} onClose={vi.fn()} />);
    await screen.findByText("Work calendar · active · revision 2");
    fireEvent.click(screen.getByRole("button", { name: "Verify calendars" }));
    await screen.findByRole("option", { name: "Work" });
    expect(screen.getByRole("checkbox", { name: "summary (required)" })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: "start (required)" })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: "end (required)" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Expires at (UTC)"), { target: { value: "2020-01-01T00:00" } });
    fireEvent.click(screen.getByRole("checkbox", { name: /Allow the governed OpenRouter model/i }));
    fireEvent.click(screen.getByRole("button", { name: "Create finite consent and read events" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/future and within seven days/i);
    fireEvent.change(screen.getByRole("combobox", { name: "Verified calendar" }), { target: { value: "foreign-calendar" } });
    expect(screen.getByRole("button", { name: "Create finite consent and read events" })).toBeDisabled();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("requires an explicit event refresh before discarding a stale prep intent", async () => {
    const event = { event_binding_id: "binding-1", event_binding_revision: 3, event_key: digest, event_revision: digest, calendar_list_revision: digest, summary: "Planning", start: "2026-09-30T12:00:00Z", end: "2026-09-30T13:00:00Z", location: null, description: null, attendees: null };
    fetchMock
      .mockResolvedValueOnce(response({ connections: [connection] }))
      .mockResolvedValueOnce(response({ connection, calendars: [{ calendar_id: "calendar-1", summary: "Work" }], calendar_list_revision: digest, pages_read: 1, truncated: false, provider_status: "verified" }))
      .mockResolvedValueOnce(response({ consent: { consent_id: "consent-1", connection_id: "connection-1", connection_revision: 2, goal_id: "goal-1", goal_revision: 4, allowed_fields: ["summary", "start", "end"], window_minutes: 1440, max_events: 20, allow_remote_model: true, expires_at: "2026-10-01T09:00:00Z", state: "active", revision: 1, consent_digest: digest, created_at: "2026-09-30T09:00:00Z", updated_at: "2026-09-30T09:00:00Z" } }))
      .mockResolvedValueOnce(response({ events: [event], consent_id: "consent-1", consent_revision: 1, connection_revision: 2, calendar_list_revision: digest, fetched_at: "2026-09-30T09:01:00Z", pages_read: 1, truncated: false }))
      .mockResolvedValueOnce(response({ detail: { code: "calendar_event_revision_stale", message: "event changed", recovery_action: null } }, false, 409))
      .mockResolvedValueOnce(response({ events: [{ ...event, event_binding_revision: 4, event_revision: "b".repeat(64) }], consent_id: "consent-1", consent_revision: 1, connection_revision: 2, calendar_list_revision: digest, fetched_at: "2026-09-30T09:02:00Z", pages_read: 1, truncated: false }));
    render(<CalendarPrepForm goals={[goal]} onCreated={vi.fn()} onClose={vi.fn()} />);
    await screen.findByText("Work calendar · active · revision 2");
    fireEvent.click(screen.getByRole("button", { name: "Verify calendars" }));
    await screen.findByRole("option", { name: "Work" });
    fireEvent.click(screen.getByRole("checkbox", { name: /Allow the governed OpenRouter model/i }));
    fireEvent.click(screen.getByRole("button", { name: "Create finite consent and read events" }));
    await screen.findByText(/Consent consent-1/);
    fireEvent.change(screen.getByLabelText("Event binding"), { target: { value: "binding-1" } });
    fireEvent.click(screen.getByRole("button", { name: "Prepare meeting" }));
    await screen.findByRole("button", { name: "Refresh event metadata" });
    expect(fetchMock).toHaveBeenCalledTimes(5);
    fireEvent.click(screen.getByRole("button", { name: "Refresh event metadata" }));
    await screen.findByRole("button", { name: "Start a new reconciled attempt" });
    expect(fetchMock).toHaveBeenCalledTimes(6);
    fireEvent.click(screen.getByRole("button", { name: "Start a new reconciled attempt" }));
    expect(screen.queryByRole("button", { name: "Retry exact request" })).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(6);
  });

  it("bounds schedule defaults and the submitted expiry by the 24-hour, consent, and prep limits", async () => {
    const now = Date.now();
    const consentExpiresAt = new Date(now + 6 * 60 * 60 * 1000).toISOString();
    const artifactExpiresAt = new Date(now + 3 * 60 * 60 * 1000).toISOString();
    const onCreated = vi.fn();
    fetchMock
      .mockResolvedValueOnce(response({ connections: [connection] }))
      .mockResolvedValueOnce(response({ connection, calendars: [{ calendar_id: "calendar-1", summary: "Work" }], calendar_list_revision: digest, pages_read: 1, truncated: false, provider_status: "verified" }))
      .mockResolvedValueOnce(response({ consent: { consent_id: "consent-1", connection_id: "connection-1", connection_revision: 2, goal_id: "goal-1", goal_revision: 4, allowed_fields: ["summary", "start", "end"], window_minutes: 1440, max_events: 20, allow_remote_model: true, expires_at: consentExpiresAt, state: "active", revision: 1, consent_digest: digest, created_at: "2026-09-30T09:00:00Z", updated_at: "2026-09-30T09:00:00Z" } }))
      .mockResolvedValueOnce(response({ events: [{ event_binding_id: "binding-1", event_binding_revision: 3, event_key: digest, event_revision: digest, calendar_list_revision: digest, summary: "Planning", start: "2026-09-30T12:00:00Z", end: "2026-09-30T13:00:00Z", location: null, description: null, attendees: null }], consent_id: "consent-1", consent_revision: 1, connection_revision: 2, calendar_list_revision: digest, fetched_at: "2026-09-30T09:01:00Z", pages_read: 1, truncated: false }))
      .mockResolvedValueOnce(response({ input_artifact: { artifact_id: "artifact-1", typed_input_ref: "workspace-json:artifacts/work-board/inputs/artifact-1.json", typed_input_digest: digest, capability_id: "calendar.meeting-prep.v1", goal_id: "goal-1", goal_revision: 4, expires_at: artifactExpiresAt }, task: { task_id: "task-1", title: "Prepare meeting", goal_id: "goal-1", goal_revision: 4, input_artifact_id: "artifact-1", capability_id: "calendar.meeting-prep.v1" }, idempotent_replay: false }))
      .mockResolvedValueOnce(response({ binding: {
        binding_id: "schedule-1",
        scheduled_job_id: "scheduled-job-1",
        capability_id: "calendar.meeting-prep.v1",
        action_type: "prepare",
        goal_id: "goal-1",
        goal_revision: 4,
        input_artifact_id: "artifact-1",
        input_digest: digest,
        consent_kind: "calendar_readonly",
        consent_id: "consent-1",
        consent_revision: 1,
        consent_digest: digest,
        cadence: { kind: "daily", timezone: "UTC", daily_hour: 9, daily_minute: 0 },
        binding_revision: 1,
        expires_at: artifactExpiresAt,
        state: "active",
        last_slot_utc: null,
        created_at: "2026-09-30T09:00:00Z",
        updated_at: "2026-09-30T09:00:00Z",
        latest_occurrence: null,
      } }));

    render(<CalendarPrepForm goals={[goal]} onCreated={onCreated} onClose={vi.fn()} />);
    await screen.findByText("Work calendar · active · revision 2");
    fireEvent.click(screen.getByRole("checkbox", { name: /Observe this calendar on a finite schedule/i }));
    const scheduleExpiry = document.querySelector('input[type="datetime-local"][max]') as HTMLInputElement;
    expect(screen.getByText(/Schedule limit: up to 24 hours/i)).toBeInTheDocument();
    expect(scheduleExpiry.max).toBeTruthy();
    expect(new Date(scheduleExpiry.max).getTime()).toBeLessThanOrEqual(Date.now() + 24 * 60 * 60 * 1000);
    expect(new Date(scheduleExpiry.value).getTime()).toBeLessThanOrEqual(new Date(scheduleExpiry.max).getTime());

    fireEvent.click(screen.getByRole("button", { name: "Verify calendars" }));
    await screen.findByRole("option", { name: "Work" });
    fireEvent.click(screen.getByRole("checkbox", { name: /Allow the governed OpenRouter model/i }));
    fireEvent.click(screen.getByRole("button", { name: "Create finite consent and read events" }));
    await screen.findByText(/Consent consent-1/);
    fireEvent.change(screen.getByLabelText("Event binding"), { target: { value: "binding-1" } });
    fireEvent.click(screen.getByRole("button", { name: "Prepare meeting" }));
    await waitFor(() => expect(onCreated).toHaveBeenCalled());

    const scheduleBody = JSON.parse(String((fetchMock.mock.calls[5]?.[1] as RequestInit).body));
    expect(Date.parse(scheduleBody.expires_at)).toBeLessThanOrEqual(Date.parse(artifactExpiresAt));
    expect(Date.parse(scheduleBody.expires_at)).toBeLessThanOrEqual(Date.parse(consentExpiresAt));
    const boundedMax = new Date((document.querySelector('input[type="datetime-local"][max]') as HTMLInputElement).max).getTime();
    expect(boundedMax).toBeLessThanOrEqual(Date.parse(artifactExpiresAt));
  });
});
