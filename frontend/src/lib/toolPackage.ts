import { API_URL } from "../config/constants";
import { apiFetch } from "./api";
import type { WorkBoardTask } from "../types";
export const TOOL_CAPABILITY="work.json-format.v1";
const PACK="/api/capability-packs/seraph.tool.json-format";
const bytes=(v:string)=>new TextEncoder().encode(v).length;
const record=(v:unknown):v is Record<string,unknown>=>Boolean(v)&&typeof v==="object"&&!Array.isArray(v);
const id=(v:unknown):v is string=>typeof v==="string"&&/^[a-zA-Z0-9_.:-]{1,128}$/.test(v);
const sha=(v:unknown):v is string=>typeof v==="string"&&/^[a-f0-9]{64}$/.test(v);
export interface ToolProfile { pack_id:string; manifest:Record<string,unknown>;root_path:string;content_digest:string;authority_digest:string;profile:{status:string;reason?:string};lifecycle:Record<string,unknown>;no_learning:true }
export interface ToolState {task_id:string;task_revision:number;attempt_id:string;job_id:string;status:string;deadline_at:string;cleanup_proven:boolean;recoverable:boolean;report_available:boolean;cancel_available:boolean;recovery_limit:string;no_learning:true}
export type ToolPending=
 |{kind:"approve";goal_id:string;goal_revision:number;packet:ToolProfile;step:0|1|2|3;review_id:string|null;approval_id:string|null}
 |{kind:"create";goal_id:string;goal_revision:number;json_text:string;input_key:string;task_key:string;artifact_id:string|null}
 |{kind:"control";task_id:string;action:"recover"|"cancel";revision:number;key:string};
export function toolStorageKey(principal:string,session:string,scope:string){if(!id(principal)||!id(session)||!id(scope))throw Error("Current owner/session required");return `seraph.tool.json.v1:${encodeURIComponent(principal)}:${encodeURIComponent(session)}:${encodeURIComponent(scope)}`;}
function profile(value:unknown):ToolProfile{
 if(!record(value)||value.pack_id!=="seraph.tool.json-format"||!record(value.manifest)||value.manifest.id!==value.pack_id||value.manifest.version!=="1.0.0"||!sha(value.content_digest)||!sha(value.authority_digest)||typeof value.root_path!=="string"||value.root_path.length>4096||!record(value.profile)||typeof value.profile.status!=="string"||!record(value.lifecycle)||value.no_learning!==true)throw Error("Exact fixed package readback unavailable");return value as unknown as ToolProfile;
}
function validate(value:unknown):ToolPending{
 if(!record(value))throw Error("Retained formatter request corrupt");
 if(value.kind==="approve"){
  if(Object.keys(value).sort().join()!==["kind","goal_id","goal_revision","packet","step","review_id","approval_id"].sort().join()||!id(value.goal_id)||!Number.isSafeInteger(value.goal_revision)||Number(value.goal_revision)<1||![0,1,2,3].includes(Number(value.step))||(value.review_id!==null&&!id(value.review_id))||(value.approval_id!==null&&!id(value.approval_id)))throw Error("Retained review request corrupt");profile(value.packet);
 }else if(value.kind==="create"){
  if(Object.keys(value).sort().join()!==["kind","goal_id","goal_revision","json_text","input_key","task_key","artifact_id"].sort().join()||!id(value.goal_id)||!Number.isSafeInteger(value.goal_revision)||Number(value.goal_revision)<1||typeof value.json_text!=="string"||bytes(value.json_text)>32768||!id(value.input_key)||!id(value.task_key)||(value.artifact_id!==null&&!id(value.artifact_id)))throw Error("Retained formatter creation corrupt");JSON.parse(value.json_text);
 }else if(value.kind==="control"){
  if(Object.keys(value).sort().join()!==["kind","task_id","action","revision","key"].sort().join()||!id(value.task_id)||!["recover","cancel"].includes(String(value.action))||!Number.isSafeInteger(value.revision)||Number(value.revision)<1||!id(value.key))throw Error("Retained formatter control corrupt");
 }else throw Error("Unknown retained formatter request");
 return value as unknown as ToolPending;
}
function scoped(key:string,pending:ToolPending){const parts=key.split(":");const scope=decodeURIComponent(parts[parts.length-1]??"");if(scope!==(pending.kind==="control"?pending.task_id:"create"))throw Error("Formatter request belongs to another scope");}
export function readToolPending(key:string):ToolPending|null{const raw=sessionStorage.getItem(key);if(raw===null)return null;if(bytes(raw)>65536)throw Error("Retained formatter request exceeds 64 KiB");const value=validate(JSON.parse(raw));scoped(key,value);return value;}
export function retainToolPending(key:string,pending:ToolPending){validate(pending);scoped(key,pending);const raw=JSON.stringify(pending);if(bytes(raw)>65536)throw Error("Full retained formatter request exceeds 64 KiB");sessionStorage.setItem(key,raw);if(sessionStorage.getItem(key)!==raw)throw Error("Request retention failed; no mutation sent");readToolPending(key);}
async function request(path:string,body?:unknown,signal?:AbortSignal):Promise<unknown>{const response=await apiFetch(`${API_URL}${path}`,{signal,...(body?{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}:{})});if(!response.ok)throw Error(`Formatter requires current readback (${response.status}); retain and retry the exact request.`);return response.json();}
export async function readToolProfile(signal?:AbortSignal){return profile(await request(PACK+"/profile",undefined,signal));}
function clear(key:string){sessionStorage.removeItem(key);if(sessionStorage.getItem(key)!==null)throw Error("Confirmed request could not be cleared");}
export async function submitTool(key:string,pending:ToolPending,signal?:AbortSignal):Promise<WorkBoardTask|null>{
 retainToolPending(key,pending);
 if(pending.kind==="approve"){
  const packet=pending.packet;const base={goal_id:pending.goal_id,content_digest:packet.content_digest,authority_digest:packet.authority_digest};
  if(pending.step===0){const result=await request(PACK+"/review",{...base,goal_revision:pending.goal_revision},signal);if(!record(result)||!record(result.review)||!id(result.review.review_id))throw Error("Review receipt unavailable");pending={...pending,step:1,review_id:result.review.review_id};retainToolPending(key,pending);}
  if(pending.step===1){const result=await request(PACK+"/approvals",{...base,action:"activate",digest:packet.content_digest,version:"1.0.0"},signal);if(!record(result)||!record(result.approval)||!id(result.approval.approval_id))throw Error("Approval receipt unavailable");pending={...pending,step:2,approval_id:result.approval.approval_id};retainToolPending(key,pending);}
  if(pending.step===2){await request(PACK+`/approvals/${pending.approval_id}/approve`,{},signal);pending={...pending,step:3};retainToolPending(key,pending);}
  if(pending.step===3){await request(PACK+"/activate",{...base,manifest:packet.manifest,root_path:packet.root_path,review_id:pending.review_id,approval_id:pending.approval_id},signal);}
  clear(key);return null;
 }
 if(pending.kind==="create"){
  if(!pending.artifact_id){const result=await request("/api/work-board/input-artifacts",{schema_version:1,capability_id:TOOL_CAPABILITY,goal_id:pending.goal_id,goal_revision:pending.goal_revision,input:{schema_version:1,json_text:pending.json_text,no_learning:true},idempotency_key:pending.input_key},signal);if(!record(result)||!id(result.artifact_id)||result.capability_id!==TOOL_CAPABILITY||result.goal_id!==pending.goal_id||result.goal_revision!==pending.goal_revision||!sha(result.typed_input_digest))throw Error("Input receipt mismatch");pending={...pending,artifact_id:result.artifact_id};retainToolPending(key,pending);}
  const result=await request("/api/work-board/tasks",{title:"Format selected JSON",capability_id:TOOL_CAPABILITY,goal_id:pending.goal_id,goal_revision:pending.goal_revision,status:"todo",input_artifact_id:pending.artifact_id,idempotency_key:pending.task_key},signal);
  if(!record(result)||!record(result.task)||!id(result.task.task_id)||result.task.capability_id!==TOOL_CAPABILITY||result.task.input_artifact_id!==pending.artifact_id||result.task.goal_id!==pending.goal_id||result.task.goal_revision!==pending.goal_revision)throw Error("Task receipt mismatch");clear(key);return result.task as unknown as WorkBoardTask;
 }
 await request(`/api/work-board/tasks/${pending.task_id}`+(pending.action==="recover"?"/tool-package/recover":"/actions"),pending.action==="recover"?{expected_revision:pending.revision,idempotency_key:pending.key}:{action:"cancel",expected_revision:pending.revision},signal);clear(key);return null;
}
export async function readToolState(task:string,signal?:AbortSignal):Promise<ToolState>{if(!id(task))throw Error("Invalid task");const value=await request(`/api/work-board/tasks/${task}/tool-package`,undefined,signal);if(!record(value)||value.task_id!==task||value.no_learning!==true||typeof value.status!=="string"||!Number.isSafeInteger(value.task_revision)||typeof value.cleanup_proven!=="boolean"||typeof value.recoverable!=="boolean"||typeof value.report_available!=="boolean"||typeof value.cancel_available!=="boolean")throw Error("Formatter state unavailable");return value as unknown as ToolState;}
export async function readToolOutput(task:string,signal?:AbortSignal){if(!id(task))throw Error("Invalid task");const response=await apiFetch(`${API_URL}/api/work-board/tasks/${task}/tool-package-output`,{signal});if(!response.ok||!response.headers.get("content-type")?.startsWith("text/plain"))throw Error("Verified literal output unavailable");const text=await response.text();if(bytes(text)>65536)throw Error("Output bound exceeded");return text;}
