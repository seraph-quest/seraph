import "@testing-library/jest-dom/vitest";
import { describe, expect, it, vi } from "vitest";

import { createCalendarPrep, validateCalendarEventsResponse, validateCalendarPrepResponse, validateCalendarResultPreview } from "./calendar";

const digest = "a".repeat(64);
const timestamp = "2026-09-30T10:00:00Z";

describe("calendar wire validators", () => {
  it("accepts the frozen minimal prep artifact and task envelope without manufacturing fields", () => {
    const result = validateCalendarPrepResponse({
      input_artifact: { artifact_id: "artifact-1", typed_input_ref: "workspace-json:artifacts/work-board/inputs/artifact-1.json", typed_input_digest: digest, capability_id: "calendar.meeting-prep.v1", goal_id: "goal-1", goal_revision: 2, expires_at: timestamp },
      task: {
        task_id: "task-1",
        title: "Prepare meeting",
        goal_id: "goal-1",
        goal_revision: 2,
        input_artifact_id: "artifact-1",
        capability_id: "calendar.meeting-prep.v1",
      },
      idempotent_replay: false,
    });
    expect(result.input_artifact).toEqual({ artifact_id: "artifact-1", typed_input_ref: "workspace-json:artifacts/work-board/inputs/artifact-1.json", typed_input_digest: digest, capability_id: "calendar.meeting-prep.v1", goal_id: "goal-1", goal_revision: 2, expires_at: timestamp });
  });

  it("rejects alternate success envelopes and preview status without a strict joined result", () => {
    expect(() => validateCalendarPrepResponse({ input_artifact: {}, task: {}, idempotent_replay: false, receipt: {} })).toThrow();
    expect(validateCalendarResultPreview({
      schema_version: 1,
      capability_id: "calendar.meeting-prep.v1",
      artifact_id: "artifact-1",
      readback_id: "readback-1",
      file_path: "artifacts/work-board/calendar/result-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json",
      content_sha256: digest,
      event_key: digest,
      event_revision: digest,
      summary: "Ready",
      agenda: ["Review launch"],
      questions: [],
      risks: [],
      preparation_steps: ["Bring the latest brief"],
    })).not.toBeNull();
  });

  it.each([
    ["artifact goal", (payload: Record<string, unknown>) => {
      (payload.input_artifact as Record<string, unknown>).goal_id = "goal-other";
    }],
    ["task goal", (payload: Record<string, unknown>) => {
      (payload.task as Record<string, unknown>).goal_revision = 9;
    }],
    ["artifact capability", (payload: Record<string, unknown>) => {
      (payload.input_artifact as Record<string, unknown>).capability_id = "other.capability.v1";
    }],
  ] as const)("rejects a returned %s that is not bound to the submitted goal/capability", async (_label, mutate) => {
    const request = {
      schema_version: 1 as const,
      input: {
        schema_version: 1 as const,
        consent_id: "consent-1",
        event_binding_id: "binding-1",
        expected_event_binding_revision: 1,
        expected_consent_revision: 1,
        expected_connection_revision: 1,
        event_revision: digest,
        calendar_list_revision: digest,
        goal_id: "goal-1",
        goal_revision: 2,
        purpose: "bounded preparation request" as const,
      },
      title: "Prepare meeting",
      idempotency_key: "calendar-prep:test",
    };
    const payload: Record<string, unknown> = {
      input_artifact: {
        artifact_id: "artifact-1",
        typed_input_ref: "workspace-json:artifacts/work-board/inputs/artifact-1.json",
        typed_input_digest: digest,
        capability_id: "calendar.meeting-prep.v1",
        goal_id: "goal-1",
        goal_revision: 2,
        expires_at: timestamp,
      },
      task: {
        task_id: "task-1",
        title: "Prepare meeting",
        goal_id: "goal-1",
        goal_revision: 2,
        input_artifact_id: "artifact-1",
        capability_id: "calendar.meeting-prep.v1",
      },
      idempotent_replay: false,
    };
    mutate(payload);
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => payload });
    vi.stubGlobal("fetch", fetchMock);
    try {
      await expect(createCalendarPrep(request)).rejects.toMatchObject({ code: "receipt_invalid" });
      expect(fetchMock).toHaveBeenCalledTimes(1);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("preserves provider all-day dates and accepts only ordered UTC date-times", () => {
    const base = {
      event_binding_id: "binding-1",
      event_binding_revision: 1,
      event_key: digest,
      event_revision: digest,
      calendar_list_revision: digest,
      summary: "Planning",
      location: null,
      description: null,
      attendees: null,
    };
    expect(validateCalendarEventsResponse({
      events: [{ ...base, start: "2026-10-01", end: "2026-10-02" }],
      consent_id: "consent-1",
      consent_revision: 1,
      connection_revision: 1,
      calendar_list_revision: digest,
      fetched_at: timestamp,
      pages_read: 1,
      truncated: false,
    }).events[0]).toMatchObject({ start: "2026-10-01", end: "2026-10-02" });
    expect(validateCalendarEventsResponse({
      events: [{ ...base, start: "2026-10-01T10:00:00Z", end: "2026-10-01T11:00:00+00:00" }],
      consent_id: "consent-1",
      consent_revision: 1,
      connection_revision: 1,
      calendar_list_revision: digest,
      fetched_at: timestamp,
      pages_read: 1,
      truncated: false,
    }).events[0]).toMatchObject({ start: "2026-10-01T10:00:00Z", end: "2026-10-01T11:00:00+00:00" });
  });

  it.each([
    ["naive date-time", "2026-10-01T10:00:00", "2026-10-01T11:00:00Z"],
    ["local offset", "2026-10-01T10:00:00-04:00", "2026-10-01T11:00:00-04:00"],
    ["invalid date", "2026-02-30", "2026-03-01"],
    ["mixed date and date-time", "2026-10-01", "2026-10-01T11:00:00Z"],
    ["reversed interval", "2026-10-01T12:00:00Z", "2026-10-01T11:00:00Z"],
  ])("rejects %s event boundaries", (_label, start, end) => {
    expect(() => validateCalendarEventsResponse({
      events: [{
        event_binding_id: "binding-1",
        event_binding_revision: 1,
        event_key: digest,
        event_revision: digest,
        calendar_list_revision: digest,
        summary: "Planning",
        start,
        end,
        location: null,
        description: null,
        attendees: null,
      }],
      consent_id: "consent-1",
      consent_revision: 1,
      connection_revision: 1,
      calendar_list_revision: digest,
      fetched_at: timestamp,
      pages_read: 1,
      truncated: false,
    })).toThrow();
  });
});
