import {sha,mac,utf8,deletionOnly} from "./protocol.js";
const $=id=>document.getElementById(id);let draft=null,metadata=null,receipt=null,config=null;
const status=value=>{$("status").textContent=value;};
async function erase(){draft=null;metadata=null;receipt=null;$("text").value="";$("privacy").checked=false;await chrome.storage.session.remove(["draft","receipt","metadata"]);status("Local preview deleted. Any admitted ticket remains in the Task inspector until explicit discard; no resend.");}
async function current(){
  if(!draft||Date.now()>=draft.expires_at){await erase();throw Error("Preview expired");}
  const [result]=await chrome.scripting.executeScript({target:{tabId:draft.tab_id,documentIds:[draft.document_id]},func:()=>location.href});
  if(result?.documentId!==draft.document_id||result.result!==draft.url)throw Error("Source navigated; delete preview and select again");
  if(!$("privacy").checked||!deletionOnly(draft.text,$("text").value))throw Error("Explicit source review and deletion-only redaction required");
  if(!config?.credential||!config.pair)throw Error("Configure current owned pairing first");
}
async function post(action,body){
  const raw=JSON.stringify(body);if(utf8(raw).length>49152)throw Error("Envelope bound reached");
  const timeout=new AbortController(),timer=setTimeout(()=>timeout.abort(),17000);
  try{
    const response=await fetch(`${SERAPH_CORE_ORIGIN}/api/context/selected-text/paired/${action}`,{method:"POST",mode:"cors",credentials:"omit",redirect:"error",cache:"no-store",signal:timeout.signal,headers:{"Content-Type":"application/json","Authorization":`Bearer ${config.credential}`,"X-Seraph-Context-Mac":await mac(config.credential,action,body)},body:raw});
    const result=await response.json();if(!response.ok)throw Error(result.detail?.code??"Current authority denied");return result;
  }finally{clearTimeout(timer);}
}
async function action(fn){for(const id of ['request','check','send','configure'])$(id).disabled=true;try{await fn();}catch(e){status(`${e.message??e}. No automatic retry. Inspect the canonical Task receipt before another operation.`);}finally{for(const id of ['request','check','send','configure'])$(id).disabled=false;}}
$("configure").onclick=()=>void action(async()=>{const pair=JSON.parse($("pair").value);if(Object.keys(pair).sort().join()!==['device_id','extension_id','name','pairing_id','reference'].join()||Object.values(pair).some(v=>typeof v!=="string"||!v||v.length>256))throw Error("Exact closed pair locator required");const credential=$("credential").value;if(!credential||credential.length>4096)throw Error("Current credential required");config={pair,credential};await chrome.storage.session.set({config});$("credential").value="";status("Pairing held only in trusted browser session; it grants no Task execution.");});
$("request").onclick=()=>void action(async()=>{
  await current();if(metadata)throw Error("Original metadata already frozen; inspect its ticket");
  const query={pair:config.pair,request_uuid:crypto.randomUUID(),expires_at:Math.floor(Date.now()/1000)+110};
  const {target}=await post("target",query);const text=$("text").value,bytes=utf8(text);if(!bytes.length||bytes.length>32768)throw Error("Text size bound reached");
  metadata={schema_version:1,adapter_profile:"browser-selected-text-v1",adapter_version:"1",adapter_build_digest:SERAPH_ADAPTER_BUILD_DIGEST,pair:config.pair,target,capture_uuid:crypto.randomUUID(),source:{origin:draft.origin,path:draft.path,document_id:draft.document_id,frame_id:0,source_revision_digest:await sha(JSON.stringify([draft.document_id,draft.url,draft.captured_at,draft.text])),captured_at:draft.captured_at,reviewed_origin:true,protected_surface_checked:true},reviewed_utf8_sha256:await sha(text),reviewed_byte_count:bytes.length,expires_at:Math.min(query.expires_at,target.expires_at),request_uuid:crypto.randomUUID(),privacy_reviewed:true};
  await chrome.storage.session.set({metadata});receipt=await post("prepare",metadata);await chrome.storage.session.set({receipt});status(`Original ${receipt.job_id} · ${receipt.status}. Approve exact source/digest/bytes in the current Task inspector, then check this original ticket.`);
});
$("check").onclick=()=>void action(async()=>{await current();if(!metadata)throw Error("No original ticket");receipt=await post("ticket",{pair:config.pair,request_uuid:crypto.randomUUID(),expires_at:metadata.expires_at,capture_uuid:metadata.capture_uuid});status(`Original ${receipt.job_id} · ${receipt.status} · approval ${receipt.approval_status??"unavailable"}. No bytes sent.`);});
$("send").onclick=()=>void action(async()=>{await current();if(!metadata||!receipt||receipt.status!=="paused"||receipt.approval_status!=="approved")throw Error("Check current exact approved ticket first");if(await sha($("text").value)!==metadata.reviewed_utf8_sha256)throw Error("Reviewed bytes changed after metadata freeze");const result=await post("upload",{metadata,text:$("text").value});await erase();status(`${result.job_id} · ${result.status} · verified private D1 text · no learning or analysis. Read/discard in current Task inspector.`);});
$("delete").onclick=()=>void erase();
const saved=await chrome.storage.session.get(['config','metadata','receipt','error']);draft=(await chrome.runtime.sendMessage({type:"take-selected-preview"}))?.draft;config=saved.config;metadata=saved.metadata;receipt=saved.receipt;
if(draft&&Date.now()<draft.expires_at){$("text").value=draft.text;$("source").textContent=`${draft.origin}${draft.path} · preview expires ${new Date(draft.expires_at).toISOString()} · core ${SERAPH_CORE_ORIGIN}`;setTimeout(()=>void erase(),draft.expires_at-Date.now());}else{await erase();status(saved.error??"No deliberate selection available. Use the extension action or selection context menu on ordinary supported HTML.");}
if(config)$("pair").value=JSON.stringify(config.pair);
