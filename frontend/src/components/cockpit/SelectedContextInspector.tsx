import { useEffect, useRef, useState } from "react";
import { apiFetch } from "../../lib/api";
import { API_URL } from "../../config/constants";
import type { WorkBoardTask } from "../../types";

interface Pair { extension_id: string; reference: string; name: string; pairing: { device_id?: string; pairing_id?: string; paired?: boolean } }
interface Capture { job_id: string; kind: string; source_task_id: string; status: string; revision: number; deadline_at: string; source: {origin: string; path: string}; reviewed_utf8_sha256: string; reviewed_byte_count: number; approval_status?: string; approval_decision_digest?: string; cleanup_state?: string; tombstone?: {request_uuid?: string; original_expected_revision?: number}; text?: string }
const base="/api/context/selected-text";
async function call<T>(path:string, method="GET", body?:unknown):Promise<T>{
  const r=await apiFetch(`${API_URL}${base}${path}`,{method,...(body?{headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}:{})});
  if(!r.ok){const v=await r.json().catch(()=>({}));throw Error(v.detail?.code??"selected_context_unavailable");}return r.json() as Promise<T>;
}
export function SelectedContextInspector({task,ownerPrincipalId,ownerSessionId}:{task:WorkBoardTask;ownerPrincipalId?:string|null;ownerSessionId?:string|null}){
  const [pairs,setPairs]=useState<Pair[]>([]),[pair,setPair]=useState(""),[stateRevision,setStateRevision]=useState(0),[ack,setAck]=useState(false);
  const [captures,setCaptures]=useState<Capture[]>([]),[message,setMessage]=useState(""),[busy,setBusy]=useState(false),[cursor,setCursor]=useState<string|null>(null);
  const generation=useRef(0);const scoped=`${ownerPrincipalId}:${ownerSessionId}:${task.task_id}`;
  const owned=task.owner_principal_id===ownerPrincipalId&&task.owner_session_id===ownerSessionId;
  function bound(c:Capture){if(c.kind!=="selected_context_v1"||c.source_task_id!==task.task_id||!/^selected-context:[a-f0-9]{32}$/.test(c.job_id))throw Error("selected_context_locator_changed");return c;}
  function clear(){setCaptures(v=>v.map(c=>({...c,text:undefined})));}
  useEffect(()=>{generation.current++;setPairs([]);setPair("");setAck(false);setCaptures([]);setMessage("");setCursor(null);return()=>{generation.current++;};},[scoped]);
  useEffect(()=>{window.addEventListener("blur",clear);document.addEventListener("visibilitychange",clear);return()=>{window.removeEventListener("blur",clear);document.removeEventListener("visibilitychange",clear);};},[]);
  useEffect(()=>{if(!captures.some(c=>c.text))return;const timer=setTimeout(clear,Math.max(0,Math.min(...captures.filter(c=>c.text).map(c=>Date.parse(c.deadline_at)))-Date.now()));return()=>clearTimeout(timer);},[captures]);
  async function run(fn:(v:number)=>Promise<void>){if(busy||!owned)return;const v=generation.current;setBusy(true);setMessage("");clear();try{await fn(v);}catch(e){if(v===generation.current){clear();setMessage(e instanceof Error?e.message:"Attachment unavailable");}}finally{if(v===generation.current)setBusy(false);}}
  async function refresh(v:number, next=""){
    const rows=await call<{captures:Capture[];next_cursor:string|null}>(`/tasks/${encodeURIComponent(task.task_id)}/captures${next?`?cursor=${encodeURIComponent(next)}`:""}`);
    const p=await call<{pairings:Pair[];state_revision:number}>("/pairings");if(v!==generation.current)return;
    setCaptures(old=>next?[...old.map(c=>({...c,text:undefined})),...rows.captures.map(bound)]:rows.captures.map(bound));setCursor(rows.next_cursor);setPairs(p.pairings);setStateRevision(p.state_revision);setAck(false);
  }
  async function receipt(c:Capture){return bound(await call<Capture>(`/tasks/${encodeURIComponent(task.task_id)}/captures/${encodeURIComponent(c.job_id)}`));}
  return <section aria-label="Private selected text attachments" className="rounded border border-sky-500/30 p-3 text-xs">
    <h3 className="font-semibold">Private selected text attachments</h3>
    <p>Optional Chromium companion · ordinary selected text only · no screenshots, model analysis or learning. Preview stays in the companion until explicit send. Permission lasts at most 120 seconds; 32 KiB per capture, 64 retained captures / 2 MiB per owner. The Mac/Linux core works without the companion.</p>
    {!owned&&<p role="alert">Current original owner Root required. Private controls are blocked.</p>}
    <button type="button" disabled={busy||!owned} onClick={()=>void run(v=>refresh(v))}>Refresh selected text receipts and paired devices</button>
    <label>Existing paired device<select value={pair} disabled={busy} onChange={e=>{setPair(e.target.value);setAck(false);}}><option value="">Choose owned pairing</option>{pairs.map((p,i)=><option key={`${p.extension_id}:${p.reference}`} value={String(i)}>{p.name} · {p.pairing.device_id??"re-pair required"}</option>)}</select></label>
    <label><input type="checkbox" checked={ack} disabled={busy} onChange={e=>setAck(e.target.checked)}/> Permit this paired companion to request selected-text metadata for this exact Task and current finite Goal. Each capture still needs exact approval.</label>
    <button type="button" disabled={busy||!owned||!ack||pair===""||!pairs[Number(pair)]?.pairing.device_id} onClick={()=>void run(async v=>{
      const p=pairs[Number(pair)];await call(`/tasks/${encodeURIComponent(task.task_id)}/target`,"POST",{pair:{extension_id:p.extension_id,reference:p.reference,name:p.name,device_id:p.pairing.device_id,pairing_id:p.pairing.pairing_id},expected_state_revision:stateRevision,expected_task_revision:task.task_revision,goal_id:task.goal_id,goal_revision:task.goal_revision,acknowledge_local_selected_text:ack});if(v===generation.current){setAck(false);setMessage("Finite target published. In the companion, review the source, fetch this target and request approval; no text has been sent.");}
    })}>Permit exact Task selected text for 120 seconds</button>
    <p>Install the optional unpacked companion from companions/selected-text after reviewing its exact core host permission. Configure the credential returned once by Settings → Native companion pairing. Legacy pairings require deliberate rotation or re-pairing; missing adapters remain locally blocked.</p>
    {captures.map(c=><article key={c.job_id} className="mt-2 border-t p-2"><p>{c.status} · {c.source.origin}{c.source.path} · {c.reviewed_byte_count} bytes · digest {c.reviewed_utf8_sha256} · deadline {c.deadline_at} · {c.cleanup_state??"private capture"}</p>
      {c.status==="paused"&&!c.tombstone&&<button type="button" disabled={busy} onClick={()=>void run(async v=>{const fresh=await receipt(c);if(fresh.approval_status!=="pending"||!fresh.approval_decision_digest)throw Error("Exact approval unavailable");await call(`/tasks/${encodeURIComponent(task.task_id)}/captures/${encodeURIComponent(c.job_id)}/decision`,"POST",{decision:"approved",expected_digest:fresh.approval_decision_digest});await refresh(v);})}>Approve this exact digest and source</button>}
      {c.status==="succeeded"&&!c.tombstone&&<button type="button" disabled={busy} onClick={()=>void run(async v=>{await receipt(c);const fresh=bound(await call<Capture>(`/tasks/${encodeURIComponent(task.task_id)}/captures/${encodeURIComponent(c.job_id)}/private`));if(v===generation.current)setCaptures(old=>old.map(item=>item.job_id===fresh.job_id?fresh:{...item,text:undefined}));})}>Read current private text</button>}
      {(!c.tombstone||c.cleanup_state==="blocked_cleanup")&&<button type="button" disabled={busy} onClick={()=>void run(async v=>{const fresh=await receipt(c);const body=fresh.tombstone?.request_uuid?{expected_revision:fresh.tombstone.original_expected_revision,request_uuid:fresh.tombstone.request_uuid}:{expected_revision:fresh.revision,request_uuid:crypto.randomUUID()};try{await call(`/tasks/${encodeURIComponent(task.task_id)}/captures/${encodeURIComponent(c.job_id)}/discard`,"POST",body);}finally{await refresh(v);}})}>Tombstone and discard capture-owned text</button>}
      {c.text!==undefined&&<pre className="whitespace-pre-wrap">{c.text}</pre>}
    </article>)}
    {cursor&&<button type="button" disabled={busy} onClick={()=>void run(v=>refresh(v,cursor))}>Read next 32 metadata receipts</button>}
    {message&&<p role="status">{message}</p>}
  </section>;
}
