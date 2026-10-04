export const utf8=value=>new TextEncoder().encode(value);
export async function sha(value){return hex(await crypto.subtle.digest("SHA-256",typeof value==="string"?utf8(value):value));}
export async function sourceRevision(source){return sha(JSON.stringify(["seraph.selected-context.source.v1",source.origin,source.path,source.document_id,0,source.captured_at]));}
function hex(value){return [...new Uint8Array(value)].map(x=>x.toString(16).padStart(2,"0")).join("");}
function ordered(value){if(Array.isArray(value))return value.map(ordered);if(value&&typeof value==="object")return Object.keys(value).sort().map(k=>[k,ordered(value[k])]);return value;}
function ascii(value){return JSON.stringify(value).replace(/[\u007f-\uffff]/g,c=>`\\u${c.charCodeAt(0).toString(16).padStart(4,"0")}`);}
export async function mac(credential,action,body){
  if(!["target","prepare","ticket","upload"].includes(action))throw Error("Closed action required");
  const key=await crypto.subtle.importKey("raw",utf8(credential),{name:"HMAC",hash:"SHA-256"},false,["sign"]);
  const derived=await crypto.subtle.sign("HMAC",key,utf8("seraph.selected-context.key.v1\0"));
  const signing=await crypto.subtle.importKey("raw",derived,{name:"HMAC",hash:"SHA-256"},false,["sign"]);
  return hex(await crypto.subtle.sign("HMAC",signing,utf8(`seraph.selected-context.${action}.v1\0${ascii(ordered(body))}`)));
}
export function deletionOnly(original,reviewed){let offset=0;for(const c of reviewed){offset=original.indexOf(c,offset);if(offset<0)return false;offset+=c.length;}return !!reviewed;}
