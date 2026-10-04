import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../../lib/calendarRescheduleApi";
import { CalendarApiError } from "../../lib/calendar";
import type { RescheduleProfile } from "../../lib/calendarRescheduleApi";
import { CalendarRescheduleProfiles } from "./CalendarRescheduleProfiles";

const retry={expected_revision:3,idempotency_key:"11111111-1111-4111-8111-111111111111",revoke_request_digest:"a".repeat(64)};
const profile=(state:RescheduleProfile["state"]):RescheduleProfile=>({connection_id:"profile",service:"calendar_reschedule_write",label:"Owned write",revision:state==="active"?2:state==="revoked"?4:3,state,scope_status:"verified",declared_scopes:api.rescheduleScopes("calendar_reschedule_write"),verified_setup_job_id:null,provider_contact:false,setup_is_write_permission:false,cleanup_retry:state==="blocked_cleanup"?retry:null});
describe("Reschedule credential cleanup controls",()=>{
  beforeEach(()=>sessionStorage.clear());afterEach(()=>vi.restoreAllMocks());
  it("refreshes typed cleanup failure then retries only the original server-derived UUID/digest",async()=>{
    const list=vi.spyOn(api,"listRescheduleProfiles").mockResolvedValueOnce([profile("active")]).mockResolvedValueOnce([profile("blocked_cleanup")]).mockResolvedValueOnce([profile("revoked")]);
    const revoke=vi.spyOn(api,"revokeRescheduleProfile").mockRejectedValueOnce(new CalendarApiError(503,"calendar_reschedule_credential_cleanup_blocked","cleanup blocked","retry_cleanup")).mockResolvedValueOnce(profile("revoked"));
    render(<CalendarRescheduleProfiles ownerPrincipalId="owner" ownerSessionId="root"/>);
    fireEvent.click(screen.getByRole("button",{name:"Refresh local reschedule profile metadata"}));await screen.findByRole("button",{name:"Revoke reschedule profile"});
    fireEvent.click(screen.getByRole("button",{name:"Revoke reschedule profile"}));await screen.findByText(/Cleanup is blocked; inspect metadata/);
    expect(revoke).toHaveBeenCalledTimes(1);expect(list).toHaveBeenCalledTimes(2);
    expect(screen.getByText(/Authority is revoked. Vault credential cleanup is unverified/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button",{name:"Retry original local credential cleanup"}));await screen.findByText(/Vault credential unavailable/);
    expect(revoke.mock.calls[1]).toEqual(["profile",retry,expect.any(AbortSignal)]);
    expect(revoke).toHaveBeenCalledTimes(2);expect(list).toHaveBeenCalledTimes(3);
    expect(screen.getByRole("button",{name:"Revoke reschedule profile"})).toBeDisabled();
  });
  it("clears a previous Root cleanup projection and offers no automatic reconciliation",async()=>{
    vi.spyOn(api,"listRescheduleProfiles").mockResolvedValue([profile("blocked_cleanup")]);const revoke=vi.spyOn(api,"revokeRescheduleProfile");
    const view=render(<CalendarRescheduleProfiles ownerPrincipalId="owner" ownerSessionId="root"/>);
    fireEvent.click(screen.getByRole("button",{name:"Refresh local reschedule profile metadata"}));await screen.findByRole("button",{name:"Retry original local credential cleanup"});
    view.rerender(<CalendarRescheduleProfiles ownerPrincipalId="owner" ownerSessionId="new-root"/>);
    await waitFor(()=>expect(screen.queryByRole("button",{name:"Retry original local credential cleanup"})).not.toBeInTheDocument());expect(revoke).not.toHaveBeenCalled();
  });
  it("rejects malformed or mismatched cleanup metadata before controls",()=>{
    expect(api.rescheduleProfile(profile("blocked_cleanup")).cleanup_retry).toEqual(retry);
    for(const update of [{state:"active"},{cleanup_retry:{...retry,expected_revision:2}},{cleanup_retry:{...retry,revoke_request_digest:"bad"}},{cleanup_retry:{...retry,extra:true}}])
      expect(()=>api.rescheduleProfile({...profile("blocked_cleanup"),...update})).toThrow(CalendarApiError);
  });
});
