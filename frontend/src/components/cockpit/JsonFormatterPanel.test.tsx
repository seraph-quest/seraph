import {fireEvent,render,screen,waitFor} from "@testing-library/react";
import {beforeEach,expect,it,vi} from "vitest";
import {apiFetch} from "../../lib/api";
import {readToolPending,toolStorageKey} from "../../lib/toolPackage";
import type {GoalInfo,WorkBoardTask} from "../../types";
import {JsonFormatterPanel} from "./JsonFormatterPanel";
vi.mock("../../lib/api",()=>({apiFetch:vi.fn()}));
const task={task_id:"format-task",capability_id:"work.json-format.v1",task_revision:4} as WorkBoardTask;
const props={task,ownerPrincipalId:"operator:one",ownerSessionId:"session-one"};
const key=toolStorageKey("operator:one","session-one",task.task_id);
const state={task_id:task.task_id,task_revision:4,board_fence:2,cancel_receipt:null,attempt_id:"original-attempt",job_id:"json-format:original",status:"running",deadline_at:"2026-10-03T16:00:00Z",cleanup_proven:true,recoverable:false,report_available:false,cancel_available:true,no_learning:true,recovery_limit:"Unknown execution remains blocked; exact original slot only."};
beforeEach(()=>{vi.restoreAllMocks();vi.mocked(apiFetch).mockReset();sessionStorage.clear();});
it("retains the exact original recovery before POST and retries unchanged after remount",async()=>{
 const bodies:string[]=[];vi.mocked(apiFetch).mockImplementation(async(_url,options)=>{if(options?.method!=="POST")return new Response(JSON.stringify(state));bodies.push(String(options.body));expect(readToolPending(key)?.kind).toBe("control");if(bodies.length===1)throw Error("Response uncertain");return new Response(JSON.stringify({recovery:{status:"succeeded"}}));});
 const mounted=render(<JsonFormatterPanel {...props}/>);fireEvent.click(await screen.findByRole("button",{name:"Inspect and recover original output"}));await screen.findByText("Response uncertain");mounted.unmount();render(<JsonFormatterPanel {...props}/>);
 fireEvent.click(await screen.findByRole("button",{name:"Retry exact formatter request"}));await waitFor(()=>expect(readToolPending(key)).toBeNull());expect(bodies).toHaveLength(2);expect(bodies[1]).toBe(bodies[0]);
});
it("fails closed when retention is unavailable or belongs to another task",async()=>{
 vi.mocked(apiFetch).mockResolvedValue(new Response(JSON.stringify(state)));vi.spyOn(Storage.prototype,"setItem").mockImplementation(()=>{throw Error("Storage unavailable");});
 render(<JsonFormatterPanel {...props}/>);fireEvent.click(await screen.findByRole("button",{name:"Inspect and recover original output"}));await screen.findByText(/Exact request retention is corrupt/);expect(vi.mocked(apiFetch).mock.calls.every(([,options])=>options?.method!=="POST")).toBe(true);expect(screen.getByRole("button",{name:"Inspect and recover original output"})).toBeDisabled();
});
it("renders injected output literally and prevents stale task-scope output",async()=>{
 vi.mocked(apiFetch).mockImplementation(async(url)=>String(url).endsWith("output")?new Response('<script>globalThis.formatterInjected=true</script>',{headers:{"content-type":"text/plain"}}):new Response(JSON.stringify({...state,report_available:true})));
 render(<JsonFormatterPanel {...props}/>);fireEvent.click(await screen.findByRole("button",{name:"Read verified JSON output"}));const output=await screen.findByLabelText("Verified literal JSON output");expect(output.textContent).toContain("<script>");expect(output.querySelector("script")).toBeNull();expect((globalThis as Record<string,unknown>).formatterInjected).toBeUndefined();
});
it("clears busy when an in-flight mutation's task scope changes",async()=>{
 let resolve:(value:Response)=>void=()=>{};vi.mocked(apiFetch).mockImplementation(async(url,options)=>options?.method==="POST"?new Promise<Response>(done=>{resolve=done;}):new Response(JSON.stringify({...state,task_id:String(url).includes("second-task")?"second-task":task.task_id})));
 const mounted=render(<JsonFormatterPanel {...props}/>);fireEvent.click(await screen.findByRole("button",{name:"Inspect and recover original output"}));await waitFor(()=>expect(screen.getByRole("button",{name:"Retry exact formatter request"})).toBeDisabled());
 mounted.rerender(<JsonFormatterPanel {...props} task={{...task,task_id:"second-task"}}/>);await waitFor(()=>expect(screen.getByRole("button",{name:"Inspect and recover original output"})).toBeEnabled());resolve(new Response('{}'));expect(readToolPending(key)?.kind).toBe("control");
});

it("reconciles a lost cancellation response after reload only with the original canonical attempt receipt",async()=>{
 let applied=false,posts=0;
 vi.mocked(apiFetch).mockImplementation(async(_url,options)=>{
  if(options?.method==="POST"){posts++;applied=true;throw Error("Cancel response lost");}
  return new Response(JSON.stringify({...state,task_revision:applied?6:4,cancel_receipt:applied?{attempt_id:state.attempt_id,board_fence:2,requested_revision:4,applied:true,cancel_requested_at:"2026-10-03T16:00:00Z"}:null}));
 });
 const mounted=render(<JsonFormatterPanel {...props}/>);fireEvent.click(await screen.findByRole("button",{name:"Cancel formatter and verify cleanup"}));await screen.findByText("Cancel response lost");mounted.unmount();render(<JsonFormatterPanel {...props}/>);
 await screen.findByRole("button",{name:"Retry exact formatter request"});fireEvent.click(screen.getByRole("button",{name:"Refresh formatter state"}));await waitFor(()=>expect(readToolPending(key)).toBeNull());expect(posts).toBe(1);
});
it("retains an uncertain cancellation when readback belongs to another attempt or fence",async()=>{
 let applied=false;
 vi.mocked(apiFetch).mockImplementation(async(_url,options)=>{if(options?.method==="POST"){applied=true;throw Error("Cancel response lost");}return new Response(JSON.stringify({...state,cancel_receipt:applied?{attempt_id:"foreign-attempt",board_fence:9,requested_revision:4,applied:true,cancel_requested_at:"2026-10-03T16:00:00Z"}:null}));});
 render(<JsonFormatterPanel {...props}/>);fireEvent.click(await screen.findByRole("button",{name:"Cancel formatter and verify cleanup"}));await screen.findByText("Cancel response lost");fireEvent.click(screen.getByRole("button",{name:"Refresh formatter state"}));await waitFor(()=>expect(readToolPending(key)?.kind).toBe("control"));expect(screen.getByRole("button",{name:"Retry exact formatter request"})).toBeEnabled();
});

it("reviews unsigned authored bytes and dispatches the server-derived capability with exact retained input",async()=>{
 const cap="pack.local.time-ledger-summary.summarize.v1",digest="a".repeat(64);let active=false;
 const calls:{path:string;body:Record<string,unknown>}[]=[];
 const packet=()=>({pack_id:"local.time-ledger-summary",manifest:{id:"local.time-ledger-summary",version:"1.0.0"},root_path:"/private/selected-package",content_digest:digest,authority_digest:"b".repeat(64),descriptor:{capability_id:cap,code_sha256:"c".repeat(64)},code_text:'print("<script>literal source</script>")',publisher_verified:false,signature_status:"unsigned-local",profile:{status:"available"},lifecycle:{active:active?{status:"active",goal_id:"goal-one",digest}:null},no_learning:true});
 vi.mocked(apiFetch).mockImplementation(async(url,options)=>{
  const path=String(url),body=JSON.parse(String(options?.body??"{}")) as Record<string,unknown>;calls.push({path,body});
  if(path.endsWith("/inspect"))return new Response(JSON.stringify(packet()));
  if(path.endsWith("/review"))return new Response(JSON.stringify({review:{review_id:"exact-review"}}));
  if(path.endsWith("/approvals"))return new Response(JSON.stringify({approval:{approval_id:"exact-approval"}}));
  if(path.endsWith("/activate")){active=true;return new Response('{}');}
  if(path.endsWith("/input-artifacts"))return new Response(JSON.stringify({artifact_id:"exact-input",capability_id:cap,goal_id:"goal-one",goal_revision:1,typed_input_digest:digest}));
  if(path.endsWith("/tasks"))return new Response(JSON.stringify({task:{task_id:"created-authored",capability_id:cap,input_artifact_id:"exact-input",goal_id:"goal-one",goal_revision:1}}));
  return new Response('{}');
 });
 const created=vi.fn();render(<JsonFormatterPanel authored ownerPrincipalId="operator:one" ownerSessionId="session-one" goals={[{id:"goal-one",revision:1,title:"Ledger"} as GoalInfo]} onCreated={created}/>);
 fireEvent.change(screen.getByLabelText("Selected package directory"),{target:{value:"/private/selected-package"}});
 fireEvent.click(screen.getByRole("button",{name:"Refresh manifest and dependencies"}));await screen.findByText(/Selected capability:/);
 fireEvent.change(screen.getByLabelText("Formatter Goal"),{target:{value:"goal-one"}});
 fireEvent.click(screen.getByRole("checkbox"));fireEvent.click(screen.getByRole("button",{name:"Review and approve exact authored package"}));
 await waitFor(()=>expect(screen.getByRole("button",{name:"Create authored package task"})).toBeEnabled());
 fireEvent.change(screen.getByLabelText("JSON document"),{target:{value:'{"schema_version":1,"rows":[]}'}});
 fireEvent.click(screen.getByRole("button",{name:"Create authored package task"}));await waitFor(()=>expect(created).toHaveBeenCalled());
 expect(calls.find(c=>c.path.endsWith("/review"))?.body).toMatchObject({root_path:"/private/selected-package",acknowledge_unsigned_local:true,content_digest:digest,goal_revision:1});
 expect(calls.find(c=>c.path.endsWith("/input-artifacts"))?.body).toMatchObject({capability_id:cap,input:{json_text:'{"schema_version":1,"rows":[]}',no_learning:true}});
 expect(calls.find(c=>c.path.endsWith("/tasks"))?.body).toMatchObject({capability_id:cap,input_artifact_id:"exact-input"});
 expect(sessionStorage.getItem(toolStorageKey("operator:one","session-one","authored-create"))).toBeNull();
});

it("removes cached literal output on current-read denial and immediately on owner/session/task change",async()=>{
 let deny=false;vi.mocked(apiFetch).mockImplementation(async(url)=>String(url).endsWith("output")?(deny?new Response('{}',{status:409}):new Response('PRIVATE ORIGINAL OUTPUT',{headers:{"content-type":"text/plain"}})):new Response(JSON.stringify({...state,report_available:true})));
 const mounted=render(<JsonFormatterPanel {...props}/>);fireEvent.click(await screen.findByRole("button",{name:"Read verified JSON output"}));await screen.findByText('PRIVATE ORIGINAL OUTPUT');
 deny=true;fireEvent.click(screen.getByRole("button",{name:"Read verified JSON output"}));await screen.findByRole("alert");expect(screen.queryByText('PRIVATE ORIGINAL OUTPUT')).toBeNull();expect(screen.getByRole("button",{name:"Read verified JSON output"})).toBeDisabled();
 deny=false;fireEvent.click(screen.getByRole("button",{name:"Refresh formatter state"}));await waitFor(()=>expect(screen.getByRole("button",{name:"Read verified JSON output"})).toBeEnabled());fireEvent.click(screen.getByRole("button",{name:"Read verified JSON output"}));await screen.findByText('PRIVATE ORIGINAL OUTPUT');
 mounted.rerender(<JsonFormatterPanel {...props} ownerPrincipalId="operator:other" ownerSessionId="other-root" task={{...task,task_id:"other-task"}}/>);expect(screen.queryByText('PRIVATE ORIGINAL OUTPUT')).toBeNull();
});
