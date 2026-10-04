import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MailReplySendPanel } from "./MailReplySendPanel";

const profile=(service:string,id:string)=>({connection_id:id,service,label:id,revision:2,state:"active",scope_status:"verified",declared_scopes:["openid","email","https://www.googleapis.com/auth/gmail."+(service==="gmail_reply_read"?"readonly":"send")],verified_setup_job_id:"identity-job",provider_contact:false,setup_is_send_permission:false});
const props={taskId:"task-1",messageRevision:"sha256:"+"a".repeat(64),ownerPrincipalId:"owner-1",ownerSessionId:"root-1"};
const job=(changes:Record<string,unknown>={})=>({request_uuid:"preview-request",source_task_id:"task-1",original_job_id:null,cancel_request_uuid:null,job_id:"send-job",kind:"mail_reply_send_v1",status:"paused",revision:8,deadline_at:new Date(Date.now()+120000).toISOString(),goal_id:"goal-1",goal_revision:1,outcome:null,contacts_spent:5,contact_may_have_occurred:false,transport_quiescent:true,cancel_requested:false,no_learning:true,failure_reason:null,
  preview:{sender:"mailbox@example.test",recipient:"reply@example.test",subject:"Original subject",body:"<script>alert(1)</script> is literal",expires_at:Date.now()/1000+120,approval_id:"approval-1",approval_status:"pending",decision_digest:"b".repeat(64),mime_digest:"c".repeat(64),request_digest:"d".repeat(64),approval_fingerprint:"fingerprint",reply_to_untrusted:true,recipient_delivery_proven:false},...changes});
function response(value:unknown){return {ok:true,status:200,json:async()=>value};}

describe("exact Gmail send controls",()=>{
  const fetchMock=vi.fn();
  beforeEach(()=>{sessionStorage.clear();fetchMock.mockReset();vi.stubGlobal("fetch",fetchMock);});
  afterEach(()=>{vi.unstubAllGlobals();vi.restoreAllMocks();});
  async function preview(){
    fetchMock.mockResolvedValueOnce(response({profiles:[profile("gmail_reply_read","read-1"),profile("gmail_reply_send","send-1")],provider_contact:false}));
    const view=render(<MailReplySendPanel {...props}/>);
    fireEvent.click(screen.getByRole("button",{name:"Load local reply profiles"}));
    await screen.findByRole("option",{name:/read-1/});
    fireEvent.click(screen.getByRole("checkbox",{name:/Read this selected source/}));
    fetchMock.mockImplementationOnce(async(_url,init)=>response(job({request_uuid:JSON.parse(init.body).request_uuid})));
    fireEvent.click(screen.getByRole("button",{name:"Prepare exact send preview"}));
    await screen.findByText("<script>alert(1)</script> is literal");return view;
  }
  it("renders exact untrusted bytes literally and requires separate approval then send",async()=>{
    await preview();expect(document.querySelector("script")).toBeNull();expect(screen.queryByRole("button",{name:/Send approved reply once/})).not.toBeInTheDocument();
    const result=job();fetchMock.mockResolvedValueOnce(response({...result,preview:{...result.preview,approval_status:"approved"}}));
    fireEvent.click(screen.getByRole("button",{name:"Approve these exact bytes and recipient"}));
    await screen.findByRole("button",{name:/Send approved reply once/});
    expect(fetchMock.mock.calls.filter(([url])=>String(url).endsWith("/execute"))).toHaveLength(0);
    expect(JSON.stringify([...Array(sessionStorage.length)].map((_,i)=>sessionStorage.getItem(sessionStorage.key(i)!)))).not.toContain("alert(1)");
  });
  it("retains lost execute response across reload with no automatic POST and reconciles Unknown",async()=>{
    const view=await preview();const original=job();fetchMock.mockResolvedValueOnce(response({...original,preview:{...original.preview,approval_status:"approved"}}));fireEvent.click(screen.getByRole("button",{name:"Approve these exact bytes and recipient"}));
    const send=await screen.findByRole("button",{name:/Send approved reply once/});fetchMock.mockRejectedValueOnce(new Error("response lost"));fireEvent.click(send);
    await screen.findByRole("alert");const pending=JSON.parse(sessionStorage.getItem(sessionStorage.key(0)!)!);expect(pending.pending.action).toBe("execute");expect(pending.pending.body).toEqual({});
    const count=fetchMock.mock.calls.length;
    // Remount through the actual scoped recovery metadata; never auto resend.
    view.unmount();render(<MailReplySendPanel {...props}/>);
    expect(fetchMock.mock.calls.length).toBe(count);
    fetchMock.mockResolvedValueOnce(response(job({status:"unknown_external_effect",revision:12,contact_may_have_occurred:true,contacts_spent:13,failure_reason:"reply_readback_unconfirmed"})));
    fireEvent.click(screen.getByRole("button",{name:"Inspect original reply readback"}));
    await screen.findByText(/Unknown; no resend/);expect(screen.queryByRole("button",{name:"Retry exact original action"})).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([url])=>String(url).endsWith("/execute"))).toHaveLength(1);
  });
  it("does not adopt a readback from another task",async()=>{
    const key="seraph:mail-exact-reply:owner-1:root-1:task-1";sessionStorage.setItem(key,JSON.stringify({version:1,jobId:"send-job",pending:null}));render(<MailReplySendPanel {...props}/>);
    fetchMock.mockResolvedValueOnce(response(job({source_task_id:"foreign-task"})));fireEvent.click(screen.getByRole("button",{name:"Inspect original reply readback"}));
    await screen.findByRole("alert");expect(screen.queryByText("<script>alert(1)</script> is literal")).not.toBeInTheDocument();expect(JSON.parse(sessionStorage.getItem(key)!).jobId).toBe("send-job");
  });
  it("resets inflight busy/private state when Root scope changes",async()=>{
    let finish:(v:unknown)=>void=()=>{};fetchMock.mockImplementationOnce(()=>new Promise(resolve=>{finish=resolve;}));const view=render(<MailReplySendPanel {...props}/>);fireEvent.click(screen.getByRole("button",{name:"Load local reply profiles"}));
    expect(screen.getByRole("button",{name:"Load local reply profiles"})).toBeDisabled();view.rerender(<MailReplySendPanel {...props} ownerSessionId="root-2"/>);
    expect(screen.getByRole("button",{name:"Load local reply profiles"})).toBeEnabled();finish(response({profiles:[profile("gmail_reply_read","foreign-read")],provider_contact:false}));
    await waitFor(()=>expect(screen.queryByRole("option",{name:/foreign-read/})).not.toBeInTheDocument());
  });
  it("fails closed on corrupt recovery storage",()=>{sessionStorage.setItem("seraph:mail-exact-reply:owner-1:root-1:task-1",'{"version":1,"jobId":"send-job","pending":{"action":"execute","body":{"raw":"untrusted MIME"}}}');render(<MailReplySendPanel {...props}/>);expect(screen.getByRole("alert")).toHaveTextContent(/storage is corrupt/);expect(screen.getByRole("button",{name:"Load local reply profiles"})).toBeDisabled();expect(fetchMock).not.toHaveBeenCalled();});
});
