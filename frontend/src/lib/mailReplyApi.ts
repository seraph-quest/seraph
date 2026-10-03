import { MailApiError, mailRequest } from "./mailApi";

export type ReplyRole = "gmail_reply_read" | "gmail_reply_send";
export interface ReplyProfile {
  connection_id: string; service: ReplyRole; label: string; revision: number;
  state: string; scope_status: string; declared_scopes: string[];
  verified_setup_job_id: string | null; provider_contact: false; setup_is_send_permission: false;
}
export interface ReplyJob {
  job_id: string; kind: string; status: string; revision: number; deadline_at: string;
  goal_id: string; goal_revision: number; outcome: string | null; contacts_spent: number;
  contact_may_have_occurred: boolean; transport_quiescent: boolean; cancel_requested: boolean;
  no_learning: true; failure_reason: string | null;
  preview?: { sender: string; recipient: string; subject: string; body: string; expires_at: number;
    approval_id: string; approval_status: string; decision_digest: string; mime_digest: string;
    request_digest: string; approval_fingerprint: string; reply_to_untrusted: true; recipient_delivery_proven: false };
  observations?: {outcome: string; auxiliary_job_id: string; no_learning: true}[];
}
export function replyScopes(role: ReplyRole): string[] {
  return ["https://www.googleapis.com/auth/gmail." + (role === "gmail_reply_read" ? "readonly" : "send"), "openid", "email"];
}
function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new MailApiError(200,"reply_receipt_invalid","The Gmail reply receipt is unconfirmed.");
  return value as Record<string, unknown>;
}
function text(v: unknown, cap = 256): v is string { return typeof v === "string" && v.length > 0 && v.length <= cap && !/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(v); }
function integer(v: unknown, min = 0): v is number { return Number.isSafeInteger(v) && (v as number) >= min; }
export function replyProfile(value: unknown): ReplyProfile {
  const v=record(value);
  if (!text(v.connection_id) || !["gmail_reply_read","gmail_reply_send"].includes(String(v.service)) || !text(v.label,200) || !integer(v.revision,1)
    || !["preparing","active","revoked","blocked","blocked_cleanup"].includes(String(v.state)) || !text(v.scope_status)
    || !Array.isArray(v.declared_scopes) || v.declared_scopes.length!==3 || v.declared_scopes.some(x=>!text(x))
    || (v.verified_setup_job_id!==null && !text(v.verified_setup_job_id)) || v.provider_contact!==false || v.setup_is_send_permission!==false) throw new MailApiError(200,"reply_profile_invalid","Reply profile metadata is unconfirmed.");
  const expected=replyScopes(v.service as ReplyRole).sort();
  if (JSON.stringify([...v.declared_scopes].sort())!==JSON.stringify(expected)) throw new MailApiError(200,"reply_profile_scope_invalid","Reply identity scopes differ from the separate reviewed profile.");
  return v as unknown as ReplyProfile;
}
export function replyJob(value: unknown): ReplyJob {
  const v=record(value);
  if (!text(v.job_id) || !["mail_reply_send_v1","mail_reply_identity_v1","mail_reply_observation_v1"].includes(String(v.kind)) || !text(v.status)
    || !integer(v.revision,1) || !text(v.deadline_at) || !Number.isFinite(Date.parse(v.deadline_at)) || !text(v.goal_id) || !integer(v.goal_revision,1)
    || !integer(v.contacts_spent) || (v.contacts_spent as number)>14 || typeof v.contact_may_have_occurred!=="boolean"
    || typeof v.transport_quiescent!=="boolean" || typeof v.cancel_requested!=="boolean" || v.no_learning!==true
    || (v.outcome!==null && !text(v.outcome)) || (v.failure_reason!==null && !text(v.failure_reason))) throw new MailApiError(200,"reply_job_invalid","The original reply job readback is unconfirmed.");
  if (v.preview!==undefined) {
    const p=record(v.preview);
    if (!text(p.sender,320) || !text(p.recipient,320) || !text(p.subject,200) || !text(p.body,4000)
      || typeof p.expires_at!=="number" || !Number.isFinite(p.expires_at) || !text(p.approval_id) || !text(p.approval_status)
      || !/^[a-f0-9]{64}$/.test(String(p.decision_digest)) || !text(p.mime_digest) || !text(p.request_digest)
      || !text(p.approval_fingerprint) || p.reply_to_untrusted!==true || p.recipient_delivery_proven!==false) throw new MailApiError(200,"reply_preview_invalid","The exact private preview is unconfirmed.");
  }
  if (v.observations!==undefined && (!Array.isArray(v.observations) || v.observations.length>16 || v.observations.some(x=>{const r=record(x);return !text(r.outcome)||!text(r.auxiliary_job_id)||r.no_learning!==true;}))) throw new MailApiError(200,"reply_observation_invalid","Read-only observation history is unconfirmed.");
  return v as unknown as ReplyJob;
}
const base="/api/capabilities/mail/";
function request(_path:string, body?:unknown, signal?:AbortSignal): RequestInit {
  return body===undefined ? {method:"GET",signal} : {method:"POST",signal,headers:{"Content-Type":"application/json"},body:JSON.stringify(body)};
}
export function listReplyProfiles(signal?:AbortSignal) { return mailRequest(base+"reply-profiles",request("",undefined,signal),value=>{const v=record(value);if(v.provider_contact!==false||!Array.isArray(v.profiles)||v.profiles.length>32) throw new MailApiError(200,"profiles_invalid","Reply profiles are unavailable.");return v.profiles.map(replyProfile);}); }
export function importReplyProfile(body:unknown,signal?:AbortSignal) { return mailRequest(base+"reply-profiles",request("",body,signal),v=>replyProfile(record(v).profile)); }
export function recoverReplyProfile(key:string,signal?:AbortSignal) { return mailRequest(base+"reply-profiles/recovery/"+encodeURIComponent(key),request("",undefined,signal),v=>{const r=record(v);if(r.provider_contact!==false)throw new MailApiError(200,"recovery_invalid","Setup readback is unconfirmed.");return r.profile===null?null:replyProfile(r.profile);}); }
export function revokeReplyProfile(id:string,body:unknown,signal?:AbortSignal) { return mailRequest(base+"reply-profiles/"+encodeURIComponent(id)+"/revoke",request("",body,signal),v=>replyProfile(record(v).profile)); }
export function pairReplyProfiles(body:unknown,signal?:AbortSignal) { return mailRequest(base+"reply-profiles/verify-pair",request("",body,signal),replyJob); }
export function previewReply(body:unknown,signal?:AbortSignal) { return mailRequest(base+"reply-sends/preview",request("",body,signal),replyJob); }
export function readReply(id:string,signal?:AbortSignal) { return mailRequest(base+"reply-sends/"+encodeURIComponent(id),request("",undefined,signal),replyJob); }
export function actReply(id:string,action:"decision"|"execute"|"cancel"|"observe",body:unknown,signal?:AbortSignal) { return mailRequest(base+"reply-sends/"+encodeURIComponent(id)+"/"+action,request("",body,signal),replyJob); }
export function recoverReplyOperation(kind:string,uuid:string,signal?:AbortSignal) { return mailRequest(base+"reply-operations/recovery/"+encodeURIComponent(kind)+"/"+encodeURIComponent(uuid),request("",undefined,signal),value=>{const v=record(value);if(v.provider_contact!==false)throw new MailApiError(200,"recovery_invalid","Operation readback is unconfirmed.");return v.job===null?null:replyJob(v.job);}); }
