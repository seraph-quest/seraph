import {useEffect,useRef,useState} from "react";
import {createPortal} from "react-dom";
import type {GoalInfo,WorkBoardTask} from "../../types";
import {TOOL_CAPABILITY,isAuthoredCapability,readAuthoredProfile,readToolProfile,readToolState,readToolOutput,readToolPending,reconcileToolCancel,submitTool,toolStorageKey} from "../../lib/toolPackage";
import type {ToolProfile,ToolState,ToolPending} from "../../lib/toolPackage";
interface Props {authored?:boolean;ownerPrincipalId?:string|null;ownerSessionId?:string|null;task?:WorkBoardTask;goals?:GoalInfo[];onClose?:()=>void;onCreated?:(task:WorkBoardTask)=>void|Promise<void>;onChanged?:()=>void|Promise<void>}
export function JsonFormatterPanel({authored=false,ownerPrincipalId,ownerSessionId,task,goals=[],onClose,onCreated,onChanged}:Props){
 const authoredMode=authored||Boolean(task&&isAuthoredCapability(task.capability_id??""));
 const noun=authoredMode?"authored package":"formatter";
 const [packagePath,setPackagePath]=useState("");
 const [loadedScope,setLoadedScope]=useState<string|null>(null);
 const [storedProfile,setProfile]=useState<ToolProfile|null>(null),[storedState,setState]=useState<ToolState|null>(null),[pending,setPending]=useState<ToolPending|null>(null);
 const [error,setError]=useState<string|null>(null),[storageError,setStorageError]=useState(false),[busy,setBusy]=useState(false),[output,setOutput]=useState<{scope:string|null;text:string}|null>(null);
 const [goalId,setGoalId]=useState(""),[json,setJson]=useState('{"example": true}'),[ack,setAck]=useState(false);
 const generation=useRef(0),controller=useRef<AbortController|null>(null);let key:string|null=null;
 try{if(ownerPrincipalId&&ownerSessionId)key=toolStorageKey(ownerPrincipalId,ownerSessionId,task?.task_id??(authoredMode?"authored-create":"create"));}catch{/* effect fails closed */}
 const profile=loadedScope===key?storedProfile:null,state=loadedScope===key?storedState:null;
 const goal=goals.find(g=>g.id===goalId&&g.ownership_access!=="recovered_read_only");const readOnly=task?.ownership_access==="recovered_read_only";
 useEffect(()=>{const version=++generation.current;controller.current?.abort();controller.current=null;setBusy(false);setLoadedScope(key);setProfile(null);setState(null);setOutput(null);setPending(null);setError(null);setStorageError(false);setAck(false);
  if(!key){setStorageError(true);return;}try{setPending(readToolPending(key));}catch{setStorageError(true);return;}
  const abort=new AbortController();if(!task&&authoredMode)return()=>{++generation.current;abort.abort();controller.current?.abort();};void (task?readToolState(task.task_id,abort.signal).then(s=>{if(version===generation.current)setState(s);}):readToolProfile(abort.signal).then(p=>{if(version===generation.current)setProfile(p);})).catch(()=>{if(!abort.signal.aborted&&version===generation.current)setError("Current package admission or dependencies are unavailable. Refresh to inspect the block reason.");});
  return()=>{++generation.current;abort.abort();controller.current?.abort();};
 },[key,task?.task_id,authoredMode]);
 async function refresh(){const version=generation.current;try{if(task){const result=await readToolState(task.task_id);if(version===generation.current){setState(result);if(!result.report_available)setOutput(null);if(key&&reconcileToolCancel(key,result))setPending(null);}}else{const result=authoredMode?await readAuthoredProfile(packagePath):await readToolProfile();if(version===generation.current)setProfile(result);}if(version===generation.current)setError(null);}catch{if(version===generation.current){setOutput(null);setProfile(null);setError("Package state requires current readback.");}}}
 async function submit(value:ToolPending){if(!key||busy||storageError||readOnly)return;const version=generation.current;const abort=new AbortController();controller.current=abort;setBusy(true);setPending(value);setError(null);
  try{const created=await submitTool(key,value,abort.signal);if(version!==generation.current)return;setPending(null);if(created)await onCreated?.(created);else{await refresh();await onChanged?.();}}
  catch(reason){if(version===generation.current){setError(reason instanceof Error?reason.message:"Formatter response uncertain");try{const retained=readToolPending(key);setPending(retained);if(retained===null)setStorageError(true);}catch{setStorageError(true);}}}
  finally{if(version===generation.current)setBusy(false);}
 }
 const disabled=busy||storageError||readOnly||Boolean(pending);const active=profile?.lifecycle.active as Record<string,unknown>|undefined;
 const contents=<section className="grid gap-3 rounded border border-white/15 bg-slate-950 p-4 text-slate-100" aria-label={authoredMode?"Reviewed authored JSON package":"Isolated JSON formatter"}>
  <div className="flex justify-between gap-2"><h3 className="font-semibold">{authoredMode?"Reviewed authored JSON package":"Isolated JSON formatter"}</h3>{onClose&&<button type="button" onClick={onClose}>Close {noun}</button>}</div>
  <p className="text-xs">{authoredMode?"Unsigned local Python runs only after review and approval in the fixed isolated profile. Static inspection does not execute code or verify the publisher. ":"One fixed reviewed package. "}No network, credentials, model calls or learning. Input ≤32 KiB; output ≤64 KiB; one attempt within its original 10 seconds. Native Linux x86_64 isolation is optional; unavailable dependencies block this feature.</p>
  {error&&<div role="alert">{error}</div>}{storageError&&<div role="alert">Exact request retention is corrupt or unavailable. No mutation can be sent.</div>}
  {pending&&<div><p>Uncertain request retained in this owner/session scope.</p><button type="button" disabled={busy||storageError||readOnly} onClick={()=>void submit(pending)}>Retry exact formatter request</button></div>}
  {!task&&<>
   {authoredMode&&<label>Selected package directory<input aria-label="Selected package directory" value={packagePath} disabled={disabled} maxLength={4096} onChange={e=>{setPackagePath(e.target.value);setProfile(null);setAck(false);}}/></label>}
   <button type="button" disabled={busy} onClick={()=>void refresh()}>Refresh manifest and dependencies</button>
   <label>Formatter Goal<select aria-label="Formatter Goal" value={goalId} disabled={disabled} onChange={e=>{setGoalId(e.target.value);setAck(false);}}><option value="">Choose current Goal</option>{goals.filter(g=>g.ownership_access!=="recovered_read_only").map(g=><option key={g.id} value={g.id}>{g.title}</option>)}</select></label>
   <div role="status">Optional dependencies: {profile?.profile.status??"checking"}{profile?.profile.reason&&` · ${profile.profile.reason}`}</div>
   {profile&&<><div className="break-all text-xs">Package {profile.pack_id} v{String(profile.manifest.version)} · SHA-256 {profile.content_digest}</div>{profile.descriptor&&<div>Selected capability: {profile.descriptor.capability_id} · unsigned local author</div>}<details><summary>Reviewed manifest and limits</summary><pre className="whitespace-pre-wrap break-all text-xs">{JSON.stringify({manifest:profile.manifest,...(profile.descriptor?{adapter:profile.descriptor,code:profile.code_text}:{})},null,2)}</pre></details>
    <label><input type="checkbox" disabled={disabled} checked={ack} onChange={e=>setAck(e.target.checked)}/>I review and approve this exact package, permissions and limits for the selected Goal.</label>
    <button type="button" disabled={disabled||!ack||!goal?.revision||profile.profile.status!=="available"} onClick={()=>{if(goal?.revision)void submit({kind:"approve",goal_id:goal.id,goal_revision:goal.revision,packet:profile,step:0,review_id:null,approval_id:null});}}>Review and approve exact {noun}</button>
    <div>Package state: {String(active?.status??"not active")}</div>
   </>}
   <label>JSON document<textarea aria-label="JSON document" className="cockpit-input w-full" rows={8} value={json} disabled={disabled} onChange={e=>setJson(e.target.value)}/></label>
   <p className="text-xs">JSON is untrusted data and is shown literally. Duplicate keys and non-finite numbers are rejected by the server. The full retained UI request must fit 64 KiB.</p>
   <button type="button" disabled={disabled||!goal?.revision||active?.status!=="active"||active.goal_id!==goal.id||active.digest!==profile?.content_digest} onClick={()=>{if(goal?.revision)void submit({kind:"create",goal_id:goal.id,goal_revision:goal.revision,json_text:json,input_key:"json-input:"+crypto.randomUUID(),task_key:"json-task:"+crypto.randomUUID(),artifact_id:null,...(profile?.descriptor?{capability_id:profile.descriptor.capability_id}:{})});}}>Create {noun} task</button>
  </>}
  {task&&<><div role="status">{state?.status??task.status} · cleanup {state?.cleanup_proven?"verified":"unproven"} · no_learning</div><p className="text-xs">{state?.recovery_limit}</p>
   <button type="button" disabled={busy} onClick={()=>void refresh()}>Refresh formatter state</button>
   <button type="button" disabled={disabled||!state||state.report_available} onClick={()=>{if(state)void submit({kind:"control",task_id:task.task_id,action:"recover",revision:state.task_revision,attempt_id:state.attempt_id,board_fence:state.board_fence,key:"json-recover:"+crypto.randomUUID()});}}>Inspect and recover original output</button>
   <button type="button" disabled={disabled||!state?.cancel_available} onClick={()=>{if(state)void submit({kind:"control",task_id:task.task_id,action:"cancel",revision:state.task_revision,attempt_id:state.attempt_id,board_fence:state.board_fence,key:"json-cancel:"+crypto.randomUUID()});}}>Cancel formatter and verify cleanup</button>
   <button type="button" disabled={busy||!state?.report_available} onClick={()=>{const version=generation.current;void readToolOutput(task.task_id).then(value=>{if(version===generation.current)setOutput({scope:key,text:value});}).catch(()=>{if(version===generation.current){setOutput(null);setState(s=>s?{...s,report_available:false}:s);setError("Literal output failed current authority or physical readback.");}});}}>Read verified JSON output</button>
   {output!==null&&output.scope===key&&<pre aria-label="Verified literal JSON output" className="whitespace-pre-wrap break-all text-xs">{output.text}</pre>}
  </>}
 </section>;
 return task?.capability_id===TOOL_CAPABILITY||authoredMode||!task?(!task?createPortal(<div style={{zIndex:1100}} className="fixed inset-0 overflow-auto bg-black/70 p-6" role="dialog" aria-modal="true" aria-label={authoredMode?"Create reviewed authored package task":"Create isolated JSON formatter"}><div className="mx-auto max-w-2xl">{contents}</div></div>,document.body):contents):null;
}
