import {useEffect,useRef,useState} from "react";
import {createPortal} from "react-dom";
import type {GoalInfo,WorkBoardTask} from "../../types";
import {TOOL_CAPABILITY,readToolProfile,readToolState,readToolOutput,readToolPending,reconcileToolCancel,submitTool,toolStorageKey} from "../../lib/toolPackage";
import type {ToolProfile,ToolState,ToolPending} from "../../lib/toolPackage";
interface Props {ownerPrincipalId?:string|null;ownerSessionId?:string|null;task?:WorkBoardTask;goals?:GoalInfo[];onClose?:()=>void;onCreated?:(task:WorkBoardTask)=>void|Promise<void>;onChanged?:()=>void|Promise<void>}
export function JsonFormatterPanel({ownerPrincipalId,ownerSessionId,task,goals=[],onClose,onCreated,onChanged}:Props){
 const [profile,setProfile]=useState<ToolProfile|null>(null),[state,setState]=useState<ToolState|null>(null),[pending,setPending]=useState<ToolPending|null>(null);
 const [error,setError]=useState<string|null>(null),[storageError,setStorageError]=useState(false),[busy,setBusy]=useState(false),[output,setOutput]=useState<string|null>(null);
 const [goalId,setGoalId]=useState(""),[json,setJson]=useState('{"example": true}'),[ack,setAck]=useState(false);
 const generation=useRef(0),controller=useRef<AbortController|null>(null);let key:string|null=null;
 try{if(ownerPrincipalId&&ownerSessionId)key=toolStorageKey(ownerPrincipalId,ownerSessionId,task?.task_id??"create");}catch{/* effect fails closed */}
 const goal=goals.find(g=>g.id===goalId&&g.ownership_access!=="recovered_read_only");const readOnly=task?.ownership_access==="recovered_read_only";
 useEffect(()=>{const version=++generation.current;controller.current?.abort();controller.current=null;setBusy(false);setProfile(null);setState(null);setOutput(null);setPending(null);setError(null);setStorageError(false);setAck(false);
  if(!key){setStorageError(true);return;}try{setPending(readToolPending(key));}catch{setStorageError(true);return;}
  const abort=new AbortController();void (task?readToolState(task.task_id,abort.signal).then(s=>{if(version===generation.current)setState(s);}):readToolProfile(abort.signal).then(p=>{if(version===generation.current)setProfile(p);})).catch(()=>{if(!abort.signal.aborted&&version===generation.current)setError("Current formatter admission or dependencies are unavailable. Refresh to inspect the block reason.");});
  return()=>{++generation.current;abort.abort();controller.current?.abort();};
 },[key,task?.task_id]);
 async function refresh(){const version=generation.current;try{if(task){const result=await readToolState(task.task_id);if(version===generation.current){setState(result);if(key&&reconcileToolCancel(key,result))setPending(null);}}else{const result=await readToolProfile();if(version===generation.current)setProfile(result);}if(version===generation.current)setError(null);}catch{if(version===generation.current)setError("Formatter state requires current readback.");}}
 async function submit(value:ToolPending){if(!key||busy||storageError||readOnly)return;const version=generation.current;const abort=new AbortController();controller.current=abort;setBusy(true);setPending(value);setError(null);
  try{const created=await submitTool(key,value,abort.signal);if(version!==generation.current)return;setPending(null);if(created)await onCreated?.(created);else{await refresh();await onChanged?.();}}
  catch(reason){if(version===generation.current){setError(reason instanceof Error?reason.message:"Formatter response uncertain");try{const retained=readToolPending(key);setPending(retained);if(retained===null)setStorageError(true);}catch{setStorageError(true);}}}
  finally{if(version===generation.current)setBusy(false);}
 }
 const disabled=busy||storageError||readOnly||Boolean(pending);const active=profile?.lifecycle.active as Record<string,unknown>|undefined;
 const contents=<section className="grid gap-3 rounded border border-white/15 bg-slate-950 p-4 text-slate-100" aria-label="Isolated JSON formatter">
  <div className="flex justify-between gap-2"><h3 className="font-semibold">Isolated JSON formatter</h3>{onClose&&<button type="button" onClick={onClose}>Close formatter</button>}</div>
  <p className="text-xs">One fixed reviewed package. No network, credentials, model calls or learning. Input ≤32 KiB; output ≤64 KiB; one attempt within its original 10 seconds. Native Linux x86_64 isolation is optional; unavailable dependencies block this feature.</p>
  {error&&<div role="alert">{error}</div>}{storageError&&<div role="alert">Exact request retention is corrupt or unavailable. No mutation can be sent.</div>}
  {pending&&<div><p>Uncertain request retained in this owner/session scope.</p><button type="button" disabled={busy||storageError||readOnly} onClick={()=>void submit(pending)}>Retry exact formatter request</button></div>}
  {!task&&<>
   <button type="button" disabled={busy} onClick={()=>void refresh()}>Refresh manifest and dependencies</button>
   <label>Formatter Goal<select aria-label="Formatter Goal" value={goalId} disabled={disabled} onChange={e=>{setGoalId(e.target.value);setAck(false);}}><option value="">Choose current Goal</option>{goals.filter(g=>g.ownership_access!=="recovered_read_only").map(g=><option key={g.id} value={g.id}>{g.title}</option>)}</select></label>
   <div role="status">Optional dependencies: {profile?.profile.status??"checking"}{profile?.profile.reason&&` · ${profile.profile.reason}`}</div>
   {profile&&<><div className="break-all text-xs">Package seraph.tool.json-format v1.0.0 · SHA-256 {profile.content_digest}</div><details><summary>Reviewed manifest and limits</summary><pre className="whitespace-pre-wrap break-all text-xs">{JSON.stringify(profile.manifest,null,2)}</pre></details>
    <label><input type="checkbox" disabled={disabled} checked={ack} onChange={e=>setAck(e.target.checked)}/>I review and approve this exact package, permissions and limits for the selected Goal.</label>
    <button type="button" disabled={disabled||!ack||!goal?.revision||profile.profile.status!=="available"} onClick={()=>{if(goal?.revision)void submit({kind:"approve",goal_id:goal.id,goal_revision:goal.revision,packet:profile,step:0,review_id:null,approval_id:null});}}>Review and approve exact formatter</button>
    <div>Package state: {String(active?.status??"not active")}</div>
   </>}
   <label>JSON document<textarea aria-label="JSON document" className="cockpit-input w-full" rows={8} value={json} disabled={disabled} onChange={e=>setJson(e.target.value)}/></label>
   <p className="text-xs">JSON is untrusted data and is shown literally. Duplicate keys and non-finite numbers are rejected by the server. The full retained UI request must fit 64 KiB.</p>
   <button type="button" disabled={disabled||!goal?.revision||active?.status!=="active"||active.goal_id!==goal.id} onClick={()=>{if(goal?.revision)void submit({kind:"create",goal_id:goal.id,goal_revision:goal.revision,json_text:json,input_key:"json-input:"+crypto.randomUUID(),task_key:"json-task:"+crypto.randomUUID(),artifact_id:null});}}>Create formatter task</button>
  </>}
  {task&&<><div role="status">{state?.status??task.status} · cleanup {state?.cleanup_proven?"verified":"unproven"} · no_learning</div><p className="text-xs">{state?.recovery_limit}</p>
   <button type="button" disabled={busy} onClick={()=>void refresh()}>Refresh formatter state</button>
   <button type="button" disabled={disabled||!state||state.report_available} onClick={()=>{if(state)void submit({kind:"control",task_id:task.task_id,action:"recover",revision:state.task_revision,attempt_id:state.attempt_id,board_fence:state.board_fence,key:"json-recover:"+crypto.randomUUID()});}}>Inspect and recover original output</button>
   <button type="button" disabled={disabled||!state?.cancel_available} onClick={()=>{if(state)void submit({kind:"control",task_id:task.task_id,action:"cancel",revision:state.task_revision,attempt_id:state.attempt_id,board_fence:state.board_fence,key:"json-cancel:"+crypto.randomUUID()});}}>Cancel formatter and verify cleanup</button>
   <button type="button" disabled={busy||!state?.report_available} onClick={()=>{const version=generation.current;void readToolOutput(task.task_id).then(value=>{if(version===generation.current)setOutput(value);}).catch(()=>{if(version===generation.current)setError("Literal output failed physical readback.");});}}>Read verified JSON output</button>
   {output!==null&&<pre aria-label="Verified literal JSON output" className="whitespace-pre-wrap break-all text-xs">{output}</pre>}
  </>}
 </section>;
 return task?.capability_id===TOOL_CAPABILITY||!task?(!task?createPortal(<div style={{zIndex:1100}} className="fixed inset-0 overflow-auto bg-black/70 p-6" role="dialog" aria-modal="true" aria-label="Create isolated JSON formatter"><div className="mx-auto max-w-2xl">{contents}</div></div>,document.body):contents):null;
}
