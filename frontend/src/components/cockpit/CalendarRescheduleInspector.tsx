import { useEffect, useRef, useState } from "react";
import * as api from "../../lib/calendarRescheduleApi";
import type { RescheduleJob, RescheduleProfile } from "../../lib/calendarRescheduleApi";
interface Props { taskId: string; ownerPrincipalId?: string | null; ownerSessionId?: string | null; goals: { id: string; title: string; revision?: number; status?: string }[] }
interface Hint { jobId: string; uuid: string; auxiliaryUuid?: string }
const button="cockpit-feedback-button";
export function CalendarRescheduleInspector({taskId,ownerPrincipalId,ownerSessionId,goals}:Props) {
  const key=ownerPrincipalId&&ownerSessionId?api.rescheduleTaskReceiptKey(ownerPrincipalId,ownerSessionId,taskId):null;
  const [hint,setHint]=useState<Hint|null>(null);const [jobId,setJobId]=useState("");
  const [job,setJob]=useState<RescheduleJob|null>(null);const [auxiliary,setAuxiliary]=useState<RescheduleJob|null>(null);
  const [profiles,setProfiles]=useState<RescheduleProfile[]>([]);const [goalId,setGoalId]=useState("");const [ack,setAck]=useState(false);
  const [busy,setBusy]=useState(false);const [message,setMessage]=useState<string|null>(null);const [storageBlocked,setStorageBlocked]=useState(false);
  const generation=useRef(0);const controller=useRef<AbortController|null>(null);const retainedHint=useRef<Hint|null>(null);
  function bound(value:RescheduleJob) {
    if(value.kind!=="calendar_reschedule_v1"||value.source_task_id!==taskId)throw Error("Canonical original belongs to another task");
    return {...value,preview:undefined};
  }
  function retain(value:Hint){if(!key)throw Error("Current Root unavailable");sessionStorage.setItem(key,JSON.stringify(value));if(sessionStorage.getItem(key)!==JSON.stringify(value))throw Error("Receipt storage unavailable");retainedHint.current=value;setHint(value);}
  useEffect(()=>{
    generation.current++;controller.current?.abort();retainedHint.current=null;setHint(null);setJob(null);setAuxiliary(null);setJobId("");setProfiles([]);setGoalId("");setAck(false);setBusy(false);setMessage(null);setStorageBlocked(false);
    if(key)try{const saved=sessionStorage.getItem(key);if(saved){const value=JSON.parse(saved) as Hint;if(typeof value.jobId!=="string"||value.jobId.length>256||!/^calendar-reschedule:[a-f0-9]{32}$/.test(value.jobId)||!/^[a-f0-9-]{36}$/.test(value.uuid)||(value.auxiliaryUuid!==undefined&&!/^[a-f0-9-]{36}$/.test(value.auxiliaryUuid)))throw Error("Invalid opaque hint");retainedHint.current=value;setHint(value);setJobId(value.jobId);}}
    catch{setStorageBlocked(true);}
    return()=>{generation.current++;controller.current?.abort();};
  },[key]);
  useEffect(()=>{const clear=()=>setJob(value=>value?{...value,preview:undefined}:value);window.addEventListener("blur",clear);document.addEventListener("visibilitychange",clear);return()=>{window.removeEventListener("blur",clear);document.removeEventListener("visibilitychange",clear);};},[]);
  useEffect(()=>{if(!job?.preview)return;const timer=setTimeout(()=>setJob(value=>value?{...value,preview:undefined}:value),Math.max(0,job.preview.expires_at*1000-Date.now()));return()=>clearTimeout(timer);},[job?.preview]);
  async function run(action:(signal:AbortSignal,version:number)=>Promise<void>){
    if(!key||busy||storageBlocked)return;const version=generation.current,abort=new AbortController();controller.current=abort;setBusy(true);setMessage(null);setJob(value=>value?{...value,preview:undefined}:value);
    const timer=setTimeout(()=>abort.abort(),125000);
    try{await action(abort.signal,version);}catch{if(version===generation.current){setJob(null);setAuxiliary(null);setProfiles([]);setAck(false);setMessage("Canonical receipt unavailable or mismatched. Private content and controls are cleared. Inspect the original hint; no operation was retried.");}}
    finally{clearTimeout(timer);if(version===generation.current){setBusy(false);controller.current=null;}}
  }
  async function current(signal:AbortSignal,version:number){
    const value=bound(await api.readReschedule(jobId,signal));if(version!==generation.current)throw Error("Owner/task changed");
    retain({jobId:value.job_id,uuid:value.request_uuid,...(retainedHint.current?.jobId===value.job_id&&retainedHint.current.auxiliaryUuid?{auxiliaryUuid:retainedHint.current.auxiliaryUuid}:{})});setJob(value);return value;
  }
  async function inspect(signal:AbortSignal,version:number){
    const value=await current(signal,version);const p=await api.listRescheduleProfiles(signal);if(version!==generation.current)return;
    setProfiles(p.filter(row=>row.service==="calendar_reschedule_read"&&row.connection_id===value.read_connection_id&&row.revision===value.read_connection_revision&&row.state==="active"));setAck(false);
    if(retainedHint.current?.auxiliaryUuid){const found=await api.recoverRescheduleOperation("calendar_reschedule_observation_v1",retainedHint.current.auxiliaryUuid,signal);if(found&&(found.kind!=="calendar_reschedule_observation_v1"||found.original_job_id!==value.job_id))throw Error("Auxiliary original mismatch");if(version===generation.current)setAuxiliary(found);}
  }
  const read=profiles[0],goal=goals.find(g=>g.id===goalId);
  return <section className="rounded border border-amber-500/30 p-3" aria-label="Exact Calendar native operation">
    <h3 className="font-semibold">Exact Calendar native operation</h3>
    <p className="text-xs">The work task stores the literal input. This separate native job owns approval, progress, conditional-write liability and readonly recovery. Local identifiers are discovery hints; current canonical metadata must match this owned task before any control.</p>
    <label>Original Calendar native job ID<input className="cockpit-input w-full" value={jobId} maxLength={256} disabled={busy} onChange={e=>{setJobId(e.target.value);setJob(null);setAuxiliary(null);setProfiles([]);setAck(false);}}/></label>
    <button type="button" className={button} disabled={busy||!key||storageBlocked||!jobId} onClick={()=>void run(inspect)}>Inspect canonical original Calendar receipt</button>
    {job&&<div role="status" className="text-xs mt-2">Original {job.job_id} · {job.status} · {job.outcome??job.failure_reason??"awaiting outcome"} · contacts {job.contacts_spent}/13 · original deadline {job.deadline_at} · {job.transport_quiescent?"transport closed":"closure unproved"} · {job.private_read_available?"current private read available":job.private_read_reason??"private preview blocked"}.
      {job.private_read_available&&<button type="button" className={button} disabled={busy} onClick={()=>void run(async(signal,version)=>{const value=await current(signal,version);if(!value.private_read_available)throw Error("Current private read denied");const privateValue=await api.inspectPrivateReschedule(value.job_id,signal);bound(privateValue);if(version===generation.current)setJob(privateValue);})}>Read current private exact preview</button>}
      {job.preview&&<div><p>{job.preview.title} · {job.preview.account_email} · {job.preview.calendar_id}</p><p>Original {job.preview.old_start.dateTime} → {job.preview.old_end.dateTime}; proposed {job.preview.new_start.dateTime} → {job.preview.new_end.dateTime} ({job.preview.new_start.timeZone})</p><p>UTC {job.preview.new_start_utc} → {job.preview.new_end_utc} · source ETag {job.preview.source_etag} · request digest {job.preview.request_digest} · {job.preview.notification_policy}</p>
        {job.status==="paused"&&<><button type="button" className={button} disabled={busy||job.preview.approval_status!=="pending"} onClick={()=>void run(async(signal,version)=>{const value=await current(signal,version);await api.actReschedule(value.job_id,"decision",{decision:"approved",expected_digest:job.preview!.decision_digest},signal);await inspect(signal,version);})}>Approve this recovered exact preview</button><button type="button" className={button} disabled={busy||job.preview.approval_status!=="approved"} onClick={()=>void run(async(signal,version)=>{const value=await current(signal,version);await api.actReschedule(value.job_id,"execute",{},signal);await inspect(signal,version);})}>Execute recovered approval once</button></>}
      </div>}
      {["queued","running","paused"].includes(job.status)&&<button type="button" className={button} disabled={busy} onClick={()=>void run(async(signal,version)=>{const value=await current(signal,version);await api.actReschedule(value.job_id,"cancel",{expected_revision:value.revision,request_uuid:crypto.randomUUID()},signal);await inspect(signal,version);})}>Cancel original Calendar native job</button>}
      {job.status==="unknown_external_effect"&&<div><p>Original Unknown, deadline, authority and write liability remain unchanged. An observation never resends.</p><p>Exact original readonly profile: {job.read_connection_id} · revision {job.read_connection_revision} · {read?"currently active":"unavailable; recovery blocked"}.</p>
        <label>New finite Calendar recovery goal<select className="cockpit-input w-full" value={goalId} disabled={busy} onChange={e=>{setGoalId(e.target.value);setAck(false);}}><option value="">Choose current reviewed finite goal</option>{goals.filter(g=>g.revision&&(!g.status||g.status==="active")).map(g=><option key={g.id} value={g.id}>{g.title} · revision {g.revision}</option>)}</select></label>
        <label><input type="checkbox" checked={ack} disabled={busy||!read} onChange={e=>setAck(e.target.checked)}/> I permit four readonly contacts for this original event under the new finite goal.</label>
        <button type="button" className={button} disabled={busy||!read||!goal?.revision||!ack||!job.transport_quiescent||!!hint?.auxiliaryUuid} onClick={()=>void run(async(signal,version)=>{
          const value=await current(signal,version);if(value.status!=="unknown_external_effect"||!value.transport_quiescent||!goal?.revision||!read||read.connection_id!==value.read_connection_id||read.revision!==value.read_connection_revision)throw Error("Recovery authority changed");
          const uuid=crypto.randomUUID();retain({jobId:value.job_id,uuid:value.request_uuid,auxiliaryUuid:uuid});
          const observed=await api.actReschedule(value.job_id,"observe",{expected_original_revision:value.revision,read_connection_id:value.read_connection_id,expected_read_revision:value.read_connection_revision,goal_id:goal.id,goal_revision:goal.revision,acknowledge_readonly_recovery:true,request_uuid:uuid},signal);
          if(observed.kind!=="calendar_reschedule_observation_v1"||observed.original_job_id!==value.job_id)throw Error("Auxiliary original mismatch");
          if(version===generation.current){setAuxiliary(observed);setAck(false);}await current(signal,version);
        })}>Observe original Calendar event without resending</button>
      </div>}
      {job.observations?.map(o=><p key={o.auxiliary_job_id}>Original history: observation {o.auxiliary_job_id} · {o.outcome}; original liability retained.</p>)}
    </div>}
    {auxiliary&&<div role="status" className="text-xs">Separate readonly auxiliary {auxiliary.job_id} · {auxiliary.status} · {auxiliary.outcome??auxiliary.failure_reason??"unconfirmed"} · {auxiliary.contacts_spent}/4 contacts. Original write status and liability retained.</div>}
    {hint?.auxiliaryUuid&&!auxiliary&&<p role="status">Original observation UUID retained. Inspect its canonical receipt; no automatic observation or resend.</p>}
    {storageBlocked&&<p role="alert">Private opaque receipt storage unavailable; provider controls blocked.</p>}{message&&<p role="alert">{message}</p>}
  </section>;
}
