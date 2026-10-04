import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, afterEach, describe, it, expect, vi } from "vitest";
import * as api from "../../lib/calendarRescheduleApi";
import { CalendarReschedulePanel } from "./CalendarReschedulePanel";
import type { RescheduleConsent, RescheduleJob, RescheduleProfile } from "../../lib/calendarRescheduleApi";
const props={ownerPrincipalId:"operator",ownerSessionId:"root-one",eventBindingId:"binding-one",eventBindingRevision:1,goalId:"goal",goalRevision:1,goals:[{id:"goal",title:"Current finite goal",revision:1}]};
const profile=(role:"read"|"write"):RescheduleProfile=>({connection_id:role,service:`calendar_reschedule_${role}`,label:role,revision:1,state:"active",scope_status:"verified",declared_scopes:api.rescheduleScopes(`calendar_reschedule_${role}`),verified_setup_job_id:"pair",provider_contact:false,setup_is_write_permission:false});
const consent=():RescheduleConsent=>({consent_id:"consent",revision:1,state:"active",expires_at:new Date(Date.now()+300000).toISOString(),goal_id:"goal",goal_revision:1,event_binding_id:"binding-one",event_binding_revision:1,read_connection_id:"read",read_connection_revision:1,write_connection_id:"write",write_connection_revision:1,provider_contact:false});
const job=():RescheduleJob=>({job_id:"native-original",request_uuid:"11111111-1111-4111-8111-111111111111",kind:"calendar_reschedule_v1",status:"paused",revision:2,goal_id:"goal",goal_revision:1,deadline_at:new Date(Date.now()+120000).toISOString(),source_task_id:"task",original_job_id:null,outcome:null,read_connection_id:"read",read_connection_revision:1,contact_may_have_occurred:false,contacts_spent:4,transport_quiescent:true,cancel_requested:false,cancel_request_uuid:null,failure_reason:null,private_read_available:false,private_read_reason:"current_permission_unavailable",no_learning:true,model_used:false,effective_route:"google_calendar_https"});
describe("Calendar exact operator controls",()=>{
  beforeEach(()=>{sessionStorage.clear();vi.spyOn(api,"listRescheduleProfiles").mockResolvedValue([profile("read"),profile("write")]);vi.spyOn(api,"listRescheduleConsents").mockResolvedValue([]);});
  afterEach(()=>vi.restoreAllMocks());
  async function choose(){fireEvent.click(screen.getByRole("button",{name:/Inspect local reschedule profiles/}));await screen.findByRole("option",{name:/read · active/});fireEvent.change(screen.getByLabelText("Reschedule read profile"),{target:{value:"read"}});fireEvent.change(screen.getByLabelText("Reschedule write profile"),{target:{value:"write"}});}
  it("keeps new finite consent acknowledgments unchecked after explicit pair verification",async()=>{
    const verify=vi.spyOn(api,"verifyReschedulePair").mockResolvedValue({...job(),kind:"calendar_reschedule_identity_v1",status:"succeeded"});
    const create=vi.spyOn(api,"createRescheduleConsent").mockResolvedValue(consent());
    render(<CalendarReschedulePanel {...props}/>);await choose();
    expect(screen.getByRole("button",{name:"Verify exact account and calendar ownership"})).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox",{name:/Read both account identities/}));fireEvent.click(screen.getByRole("button",{name:"Verify exact account and calendar ownership"}));
    await waitFor(()=>expect(verify).toHaveBeenCalledTimes(1));await waitFor(()=>expect(screen.getByRole("checkbox",{name:/Read the full owned event/})).toBeEnabled());
    for(const name of [/Read the full owned event/,/Read calendar-list metadata/,/Permit one exactly approved/]) expect(screen.getByRole("checkbox",{name})).not.toBeChecked();
    expect(screen.getByRole("button",{name:"Grant finite reschedule permission"})).toBeDisabled();expect(create).not.toHaveBeenCalled();
  });
  it("allows local expired permission revocation without pair verification or old goal contact",async()=>{
    vi.mocked(api.listRescheduleConsents).mockResolvedValue([{...consent(),goal_revision:99,expires_at:new Date(Date.now()-1000).toISOString()}]);
    const revoke=vi.spyOn(api,"revokeRescheduleConsent").mockResolvedValue({...consent(),revision:2,state:"revoked"});
    const verify=vi.spyOn(api,"verifyReschedulePair");render(<CalendarReschedulePanel {...props}/>);await choose();
    expect(screen.getByText(/Expired or changed permission retains its slot/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button",{name:"Revoke this permission locally"}));await waitFor(()=>expect(revoke).toHaveBeenCalledTimes(1));expect(verify).not.toHaveBeenCalled();
  });
  it("passes literal offset and IANA strings untouched and never retries an unconfirmed preview",async()=>{
    vi.mocked(api.listRescheduleConsents).mockResolvedValue([consent()]);
    const create=vi.spyOn(api,"createRescheduleTask").mockResolvedValue({task_id:"task",task_revision:1});
    const preview=vi.spyOn(api,"previewReschedule").mockRejectedValue(Error("lost response"));
    const recovered=vi.spyOn(api,"recoverRescheduleOperation").mockResolvedValue(job());
    const execute=vi.spyOn(api,"actReschedule");render(<CalendarReschedulePanel {...props}/>);await choose();
    fireEvent.change(screen.getByLabelText("New start with literal UTC offset"),{target:{value:"2027-10-31T02:30:00+02:00"}});
    fireEvent.change(screen.getByLabelText("New end with literal UTC offset"),{target:{value:"2027-10-31T02:30:00+01:00"}});
    fireEvent.change(screen.getByLabelText("Explicit IANA timezone"),{target:{value:"Europe/Warsaw"}});
    fireEvent.click(screen.getByRole("checkbox",{name:/Read a fresh exact preview/}));fireEvent.click(screen.getByRole("button",{name:"Create native task and exact approval preview"}));
    await screen.findByText(/request is unconfirmed/);expect(create.mock.calls[0][0]).toMatchObject({input:{new_start:{dateTime:"2027-10-31T02:30:00+02:00",timeZone:"Europe/Warsaw"},new_end:{dateTime:"2027-10-31T02:30:00+01:00",timeZone:"Europe/Warsaw"}}});
    fireEvent.click(screen.getByRole("button",{name:/Inspect original native receipt/}));await waitFor(()=>expect(recovered).toHaveBeenCalledTimes(1));expect(preview).toHaveBeenCalledTimes(1);expect(execute).not.toHaveBeenCalled();
    expect(screen.queryByRole("button",{name:"Approve this exact reschedule"})).not.toBeInTheDocument();
    const stored=Object.values(sessionStorage);expect(stored.join(" ")).not.toContain("Europe/Warsaw");expect(stored.join(" ")).not.toContain("2027-10-31");
  });
});
