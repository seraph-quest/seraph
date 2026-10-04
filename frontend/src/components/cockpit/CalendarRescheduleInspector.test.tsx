import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../../lib/calendarRescheduleApi";
import type { RescheduleJob, RescheduleProfile } from "../../lib/calendarRescheduleApi";
import { CalendarRescheduleInspector } from "./CalendarRescheduleInspector";

const originalId = "calendar-reschedule:" + "a".repeat(32);
const uuid = "11111111-1111-4111-8111-111111111111";
const props = { taskId: "task", ownerPrincipalId: "operator", ownerSessionId: "root", goals: [{ id: "new-goal", title: "New finite recovery goal", revision: 1, status: "active" }] };
const original = (): RescheduleJob => ({ job_id: originalId, request_uuid: uuid, kind: "calendar_reschedule_v1", status: "unknown_external_effect", revision: 5, goal_id: "closed-goal", goal_revision: 1, deadline_at: "2026-01-01T00:00:00Z", source_task_id: "task", original_job_id: null, outcome: null, read_connection_id: "read", read_connection_revision: 2, contact_may_have_occurred: true, contacts_spent: 12, transport_quiescent: true, cancel_requested: false, cancel_request_uuid: null, failure_reason: "response_lost", private_read_available: false, private_read_reason: "goal_changed", no_learning: true, model_used: false, effective_route: "google_calendar_https" });
const profile = (): RescheduleProfile => ({ connection_id: "read", service: "calendar_reschedule_read", label: "Original read", revision: 2, state: "active", scope_status: "verified", declared_scopes: api.rescheduleScopes("calendar_reschedule_read"), verified_setup_job_id: "identity", provider_contact: false, setup_is_write_permission: false });
function retain(extra = {}) { sessionStorage.setItem(api.rescheduleTaskReceiptKey("operator", "root", "task"), JSON.stringify({ jobId: originalId, uuid, ...extra })); }
async function inspect() { fireEvent.click(screen.getByRole("button", { name: "Inspect canonical original Calendar receipt" })); await screen.findByText(/Original Unknown, deadline, authority/); }

describe("Calendar task-detail canonical recovery", () => {
  beforeEach(() => { sessionStorage.clear(); vi.spyOn(api, "readReschedule").mockResolvedValue(original()); vi.spyOn(api, "listRescheduleProfiles").mockResolvedValue([profile()]); });
  afterEach(() => vi.restoreAllMocks());

  it("rejects an opaque hint for another task before private reads or controls", async () => {
    retain(); vi.mocked(api.readReschedule).mockResolvedValue({ ...original(), source_task_id: "another-task", private_read_available: true });
    const privateRead = vi.spyOn(api, "inspectPrivateReschedule"), act = vi.spyOn(api, "actReschedule");
    render(<CalendarRescheduleInspector {...props} />);
    fireEvent.click(screen.getByRole("button", { name: "Inspect canonical original Calendar receipt" }));
    await screen.findByRole("alert");
    expect(api.listRescheduleProfiles).not.toHaveBeenCalled(); expect(privateRead).not.toHaveBeenCalled(); expect(act).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: /Observe original/ })).not.toBeInTheDocument();
  });

  it("reopens metadata after the old Goal closes and preserves the original Unknown beside the auxiliary", async () => {
    retain(); const auxiliary = { ...original(), job_id: "auxiliary", kind: "calendar_reschedule_observation_v1" as const, original_job_id: originalId, status: "succeeded", outcome: "verified_reschedule_observation", contacts_spent: 4 };
    const act = vi.spyOn(api, "actReschedule").mockResolvedValue(auxiliary);
    const view = render(<CalendarRescheduleInspector {...props} />); await inspect();
    expect(screen.queryByRole("button", { name: "Read current private exact preview" })).not.toBeInTheDocument();
    const acknowledgment = screen.getByRole("checkbox", { name: /four readonly contacts/ }); expect(acknowledgment).not.toBeChecked();
    fireEvent.change(screen.getByLabelText("New finite Calendar recovery goal"), { target: { value: "new-goal" } });
    expect(screen.getByRole("button", { name: /Observe original/ })).toBeDisabled();
    fireEvent.click(acknowledgment); fireEvent.click(screen.getByRole("button", { name: /Observe original/ }));
    await screen.findByText(/Separate readonly auxiliary auxiliary/);
    expect(act.mock.calls[0].slice(0, 2)).toEqual([originalId, "observe"]);
    expect(act.mock.calls[0][2]).toMatchObject({ expected_original_revision: 5, read_connection_id: "read", expected_read_revision: 2, goal_id: "new-goal", goal_revision: 1, acknowledge_readonly_recovery: true });
    expect(screen.getByText(/Original calendar-reschedule:.*unknown_external_effect/)).toBeInTheDocument();
    const retained = JSON.parse(sessionStorage.getItem(api.rescheduleTaskReceiptKey("operator", "root", "task"))!);
    expect(retained.auxiliaryUuid).toMatch(/^[a-f0-9-]{36}$/); expect(Object.keys(retained).sort()).toEqual(["auxiliaryUuid", "jobId", "uuid"]);
    view.unmount(); vi.spyOn(api, "recoverRescheduleOperation").mockResolvedValue(auxiliary);
    render(<CalendarRescheduleInspector {...props} />); await inspect(); await screen.findByText(/Separate readonly auxiliary auxiliary/);
    expect(act).toHaveBeenCalledTimes(1); expect(screen.getByRole("button", { name: /Observe original/ })).toBeDisabled();
  });

  it("retains an unconfirmed observation UUID across reopen and never submits it twice", async () => {
    retain(); const act = vi.spyOn(api, "actReschedule").mockRejectedValue(Error("response lost"));
    const view = render(<CalendarRescheduleInspector {...props} />); await inspect();
    fireEvent.change(screen.getByLabelText("New finite Calendar recovery goal"), { target: { value: "new-goal" } });
    fireEvent.click(screen.getByRole("checkbox", { name: /four readonly contacts/ })); fireEvent.click(screen.getByRole("button", { name: /Observe original/ })); await screen.findByRole("alert");
    const saved = JSON.parse(sessionStorage.getItem(api.rescheduleTaskReceiptKey("operator", "root", "task"))!); expect(saved.auxiliaryUuid).toBeTruthy();
    view.unmount(); const recover = vi.spyOn(api, "recoverRescheduleOperation").mockResolvedValue(null);
    render(<CalendarRescheduleInspector {...props} />); await inspect(); await waitFor(() => expect(recover).toHaveBeenCalledWith("calendar_reschedule_observation_v1", saved.auxiliaryUuid, expect.any(AbortSignal)));
    expect(screen.getByRole("button", { name: /Observe original/ })).toBeDisabled(); expect(act).toHaveBeenCalledTimes(1);
  });

  it("clears metadata and controls when the current Root or task changes", async () => {
    retain(); const view = render(<CalendarRescheduleInspector {...props} />); await inspect();
    view.rerender(<CalendarRescheduleInspector {...props} taskId="other-task" ownerSessionId="other-root" />);
    expect(screen.queryByText(/Original Unknown, deadline, authority/)).not.toBeInTheDocument();
    expect(screen.getByLabelText("Original Calendar native job ID")).toHaveValue(""); expect(screen.getByRole("button", { name: "Inspect canonical original Calendar receipt" })).toBeDisabled();
    expect(api.readReschedule).toHaveBeenCalledTimes(1);
  });
});
