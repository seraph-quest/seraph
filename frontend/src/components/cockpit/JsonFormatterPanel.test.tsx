import {fireEvent,render,screen,waitFor} from "@testing-library/react";
import {beforeEach,expect,it,vi} from "vitest";
import {apiFetch} from "../../lib/api";
import {readToolPending,retainToolPending,toolStorageKey} from "../../lib/toolPackage";
import type {ToolPending,ToolProfile} from "../../lib/toolPackage";
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
 const mounted=render(<JsonFormatterPanel {...props}/>);const cancel=await screen.findByRole("button",{name:"Cancel formatter and verify cleanup"});await waitFor(()=>expect(cancel).toBeEnabled());fireEvent.click(cancel);await screen.findByText("Cancel response lost");mounted.unmount();render(<JsonFormatterPanel {...props}/>);
 await screen.findByRole("button",{name:"Retry exact formatter request"});fireEvent.click(screen.getByRole("button",{name:"Refresh formatter state"}));await waitFor(()=>expect(readToolPending(key)).toBeNull());expect(posts).toBe(1);
});
it("retains an uncertain cancellation when readback belongs to another attempt or fence",async()=>{
 let applied=false;
 vi.mocked(apiFetch).mockImplementation(async(_url,options)=>{if(options?.method==="POST"){applied=true;throw Error("Cancel response lost");}return new Response(JSON.stringify({...state,cancel_receipt:applied?{attempt_id:"foreign-attempt",board_fence:9,requested_revision:4,applied:true,cancel_requested_at:"2026-10-03T16:00:00Z"}:null}));});
 render(<JsonFormatterPanel {...props}/>);const cancel=await screen.findByRole("button",{name:"Cancel formatter and verify cleanup"});await waitFor(()=>expect(cancel).toBeEnabled());fireEvent.click(cancel);await screen.findByText("Cancel response lost");fireEvent.click(screen.getByRole("button",{name:"Refresh formatter state"}));await waitFor(()=>expect(readToolPending(key)?.kind).toBe("control"));expect(screen.getByRole("button",{name:"Retry exact formatter request"})).toBeEnabled();
});

it("reviews unsigned authored bytes and dispatches the server-derived capability with exact retained input",async()=>{
 const cap="pack.local.time-ledger-summary.summarize.v1",digest="a".repeat(64);let active=false;
 const calls:{path:string;body:Record<string,unknown>}[]=[];
 const packet=()=>({pack_id:"local.time-ledger-summary",manifest:{id:"local.time-ledger-summary",version:"1.0.0"},root_path:"/private/selected-package",content_digest:digest,authority_digest:"b".repeat(64),descriptor:{capability_id:cap,code_sha256:"c".repeat(64)},code_text:'print("<script>literal source</script>")',publisher_verified:false,signature_status:"unsigned-local",profile:{status:"available"},lifecycle:{active:active?{status:"active",goal_id:"goal-one",goal_revision:1,digest}:null},no_learning:true});
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
 fireEvent.change(screen.getByLabelText("Authored package Goal"),{target:{value:"goal-one"}});
 fireEvent.click(screen.getByRole("checkbox"));fireEvent.click(screen.getByRole("button",{name:"Review and approve exact authored package"}));
 await waitFor(()=>expect(screen.getByRole("button",{name:"Create authored package task"})).toBeEnabled());
 fireEvent.change(screen.getByLabelText("JSON document"),{target:{value:'{"schema_version":1,"rows":[]}'}});
 fireEvent.click(screen.getByRole("button",{name:"Create authored package task"}));await waitFor(()=>expect(created).toHaveBeenCalled());
 expect(calls.find(c=>c.path.endsWith("/review"))?.body).toMatchObject({root_path:"/private/selected-package",acknowledge_unsigned_local:true,content_digest:digest,goal_revision:1});
 expect(calls.find(c=>c.path.endsWith("/input-artifacts"))?.body).toMatchObject({capability_id:cap,input:{json_text:'{"schema_version":1,"rows":[]}',no_learning:true}});
 expect(calls.find(c=>c.path.endsWith("/tasks"))?.body).toMatchObject({capability_id:cap,input_artifact_id:"exact-input"});
 expect(sessionStorage.getItem(toolStorageKey("operator:one","session-one","authored-create"))).toBeNull();
});

it("retains the exact authored update or safe rollback action before any lifecycle POST",async()=>{
 const digest="d".repeat(64),cap="pack.local.time-ledger-summary.summarize.v1";
 let quarantined=false;const actions:string[]=[];
 vi.mocked(apiFetch).mockImplementation(async(url,options)=>{
  const path=String(url),body=JSON.parse(String(options?.body??"{}"));
  if(path.endsWith("/inspect"))return new Response(JSON.stringify({pack_id:"local.time-ledger-summary",manifest:{id:"local.time-ledger-summary",version:"2.0.0"},root_path:"/private/new-version",content_digest:digest,authority_digest:"b".repeat(64),descriptor:{capability_id:cap},code_text:"# literal reviewed code",publisher_verified:false,signature_status:"unsigned-local",profile:{status:"available"},lifecycle:{active:{status:quarantined?"quarantined":"active",goal_id:"goal-one",goal_revision:1,digest:"a".repeat(64)}},no_learning:true}));
  if(path.endsWith("/review"))return new Response(JSON.stringify({review:{review_id:"version-review"}}));
  if(path.endsWith("/approvals")){actions.push(body.action);expect(readToolPending(toolStorageKey("operator:one","session-one","authored-create"))).toMatchObject({kind:"approve",action:body.action,packet:{content_digest:digest}});return new Response(JSON.stringify({approval:{approval_id:"version-approval"}}));}
  return new Response('{}');
 });
 render(<JsonFormatterPanel authored ownerPrincipalId="operator:one" ownerSessionId="session-one" goals={[{id:"goal-one",revision:1,title:"Ledger"} as GoalInfo]}/>);
 fireEvent.change(screen.getByLabelText("Selected package directory"),{target:{value:"/private/new-version"}});fireEvent.click(screen.getByRole("button",{name:"Refresh manifest and dependencies"}));await screen.findByText(/Selected capability:/);
 fireEvent.change(screen.getByLabelText("Authored package Goal"),{target:{value:"goal-one"}});fireEvent.click(screen.getByRole("checkbox"));fireEvent.click(screen.getByRole("button",{name:"Review and approve exact authored package"}));await waitFor(()=>expect(actions).toEqual(["update"]));await waitFor(()=>expect(screen.getByRole("checkbox")).toBeEnabled());
 quarantined=true;fireEvent.click(screen.getByRole("button",{name:"Refresh manifest and dependencies"}));await screen.findByText("Package state: quarantined");expect(screen.getByRole("button",{name:"Review and approve exact authored package"})).toBeDisabled();fireEvent.click(screen.getByRole("button",{name:"Review and approve safe rollback"}));await waitFor(()=>expect(actions).toEqual(["update","rollback"]));
 expect(vi.mocked(apiFetch).mock.calls.some(([url])=>String(url).endsWith("/update"))).toBe(true);
 expect(vi.mocked(apiFetch).mock.calls.some(([url])=>String(url).endsWith("/rollback"))).toBe(true);
});

it("removes cached literal output on current-read denial and immediately on owner/session/task change",async()=>{
 let deny=false;vi.mocked(apiFetch).mockImplementation(async(url)=>String(url).endsWith("output")?(deny?new Response('{}',{status:409}):new Response('PRIVATE ORIGINAL OUTPUT',{headers:{"content-type":"text/plain"}})):new Response(JSON.stringify({...state,report_available:true})));
 const mounted=render(<JsonFormatterPanel {...props}/>);const read=await screen.findByRole("button",{name:"Read verified JSON output"});await waitFor(()=>expect(read).toBeEnabled());fireEvent.click(read);await screen.findByText('PRIVATE ORIGINAL OUTPUT');
 deny=true;fireEvent.click(screen.getByRole("button",{name:"Read verified JSON output"}));await screen.findByRole("alert");expect(screen.queryByText('PRIVATE ORIGINAL OUTPUT')).toBeNull();expect(screen.getByRole("button",{name:"Read verified JSON output"})).toBeDisabled();
 deny=false;fireEvent.click(screen.getByRole("button",{name:"Refresh formatter state"}));await waitFor(()=>expect(screen.getByRole("button",{name:"Read verified JSON output"})).toBeEnabled());fireEvent.click(screen.getByRole("button",{name:"Read verified JSON output"}));await screen.findByText('PRIVATE ORIGINAL OUTPUT');
 mounted.rerender(<JsonFormatterPanel {...props} ownerPrincipalId="operator:other" ownerSessionId="other-root" task={{...task,task_id:"other-task"}}/>);expect(screen.queryByText('PRIVATE ORIGINAL OUTPUT')).toBeNull();
});

const blockedAuthoredPacket={pack_id:"local.time-ledger-summary",manifest:{id:"local.time-ledger-summary",version:"1.0.0"},root_path:"/private/selected-package",content_digest:"a".repeat(64),authority_digest:"b".repeat(64),descriptor:{capability_id:"pack.local.time-ledger-summary.summarize.v1"},code_text:"# literal authored code",publisher_verified:false,signature_status:"unsigned-local",profile:{status:"blocked",reason:"unsupported_host"},lifecycle:{active:{status:"active",goal_id:"goal-one",goal_revision:1,digest:"a".repeat(64)}},no_learning:true};
const authoredProps={authored:true,ownerPrincipalId:"operator:one",ownerSessionId:"session-one",goals:[{id:"goal-one",revision:1,title:"Ledger"} as GoalInfo]};
const staticKey=toolStorageKey("operator:one","session-one","authored-create");

it("saves one retained static review on a blocked host without approval, activation or execution",async()=>{
 const posts:{path:string;body:Record<string,unknown>}[]=[];
 vi.mocked(apiFetch).mockImplementation(async(url,options)=>{
  if(options?.method!=="POST")return new Response(JSON.stringify(state));
  const path=String(url),body=JSON.parse(String(options?.body??"{}"));posts.push({path,body});
  if(path.endsWith("/inspect"))return new Response(JSON.stringify(blockedAuthoredPacket));
  expect(path).toMatch(/\/local.time-ledger-summary\/review$/);
  expect(readToolPending(staticKey)).toMatchObject({kind:"review",goal_id:"goal-one",goal_revision:1,packet:blockedAuthoredPacket});
  return new Response(JSON.stringify({review:{review_id:"durable-static-review"}}));
 });
 const mounted=render(<JsonFormatterPanel {...authoredProps}/>);
 fireEvent.change(screen.getByLabelText("Selected package directory"),{target:{value:blockedAuthoredPacket.root_path}});fireEvent.click(screen.getByRole("button",{name:"Refresh manifest and dependencies"}));await screen.findByText(/unsupported_host/);
 fireEvent.change(screen.getByLabelText("Authored package Goal"),{target:{value:"goal-one"}});fireEvent.click(screen.getByRole("checkbox"));
 expect(screen.getByRole("button",{name:"Review and approve exact authored package"})).toBeDisabled();expect(screen.getByRole("button",{name:"Create authored package task"})).toBeDisabled();
 fireEvent.click(screen.getByRole("button",{name:"Save static package review"}));await screen.findByText(/Static package review recorded for Goal goal-one/);
 expect(posts.filter(p=>p.path.endsWith("/review"))).toEqual([{path:expect.stringMatching(/\/local.time-ledger-summary\/review$/),body:{goal_id:"goal-one",goal_revision:1,root_path:blockedAuthoredPacket.root_path,content_digest:blockedAuthoredPacket.content_digest,authority_digest:blockedAuthoredPacket.authority_digest,acknowledge_unsigned_local:true}}]);
 expect(posts.every(p=>p.path.endsWith("/inspect")||p.path.endsWith("/review"))).toBe(true);expect(readToolPending(staticKey)).toBeNull();expect(screen.getByRole("button",{name:"Create authored package task"})).toBeDisabled();
 mounted.rerender(<JsonFormatterPanel {...props}/>);await screen.findByRole("button",{name:"Refresh formatter state"});expect(screen.queryByRole("button",{name:"Save static package review"})).toBeNull();expect(screen.queryByText(/Static package review recorded/)).toBeNull();
});

it("retains a lost static review exactly across reload and isolates it from another owner",async()=>{
 const bodies:string[]=[];
 vi.mocked(apiFetch).mockImplementation(async(url,options)=>{
  if(String(url).endsWith("/inspect"))return new Response(JSON.stringify(blockedAuthoredPacket));
  expect(String(url)).toMatch(/\/local.time-ledger-summary\/review$/);bodies.push(String(options?.body));expect(readToolPending(staticKey)?.kind).toBe("review");
  if(bodies.length===1)throw Error("Static review response lost");
  return new Response(JSON.stringify({review:{review_id:"durable-static-review"}}));
 });
 const mounted=render(<JsonFormatterPanel {...authoredProps}/>);
 fireEvent.change(screen.getByLabelText("Selected package directory"),{target:{value:blockedAuthoredPacket.root_path}});fireEvent.click(screen.getByRole("button",{name:"Refresh manifest and dependencies"}));await screen.findByText(/unsupported_host/);
 fireEvent.change(screen.getByLabelText("Authored package Goal"),{target:{value:"goal-one"}});fireEvent.click(screen.getByRole("checkbox"));fireEvent.click(screen.getByRole("button",{name:"Save static package review"}));await screen.findByText("Static review response lost");
 mounted.rerender(<JsonFormatterPanel {...authoredProps} ownerPrincipalId="operator:other" ownerSessionId="other-root"/>);expect(screen.queryByRole("button",{name:"Retry exact authored package request"})).toBeNull();expect(readToolPending(toolStorageKey("operator:other","other-root","authored-create"))).toBeNull();expect(readToolPending(staticKey)?.kind).toBe("review");expect(bodies).toHaveLength(1);
 mounted.unmount();render(<JsonFormatterPanel {...authoredProps}/>);fireEvent.click(await screen.findByRole("button",{name:"Retry exact authored package request"}));await screen.findByText(/Static package review recorded for Goal goal-one/);
 expect(readToolPending(staticKey)).toBeNull();expect(bodies).toHaveLength(2);expect(bodies[1]).toBe(bodies[0]);expect(vi.mocked(apiFetch).mock.calls.every(([url])=>String(url).endsWith("/inspect")||String(url).endsWith("/review"))).toBe(true);expect(screen.getByRole("button",{name:"Create authored package task"})).toBeDisabled();
});

it.each(["activate","update","rollback"] as const)("requires fresh matching availability before retained %s and preserves lost-response owner scope",async(action)=>{
 const pending:ToolPending={kind:"approve",goal_id:"goal-one",goal_revision:1,packet:blockedAuthoredPacket as ToolProfile,step:3,review_id:"original-review",approval_id:"original-approval",action};
 retainToolPending(staticKey,pending);const original=sessionStorage.getItem(staticKey);let available=false;
 const bodies:string[]=[];const paths:string[]=[];
 vi.mocked(apiFetch).mockImplementation(async(url,options)=>{
  const path=String(url);paths.push(path);
  if(path.endsWith("/inspect"))return new Response(JSON.stringify({...blockedAuthoredPacket,profile:available?{status:"available"}:blockedAuthoredPacket.profile}));
  expect(path).toMatch(new RegExp(`/local.time-ledger-summary/${action}$`));bodies.push(String(options?.body));
  expect(sessionStorage.getItem(staticKey)).toBe(original);
  if(bodies.length===1)throw Error("Lifecycle response lost");
  return new Response('{}');
 });
 const mounted=render(<JsonFormatterPanel {...authoredProps}/>);
 fireEvent.click(await screen.findByRole("button",{name:"Retry exact authored package request"}));await screen.findByText(/Current authored execution profile unavailable/);
 expect(paths).toHaveLength(1);expect(paths[0]).toMatch(/\/authored\/inspect$/);expect(bodies).toHaveLength(0);expect(sessionStorage.getItem(staticKey)).toBe(original);
 available=true;fireEvent.click(screen.getByRole("button",{name:"Retry exact authored package request"}));await screen.findByText("Lifecycle response lost");expect(sessionStorage.getItem(staticKey)).toBe(original);
 mounted.rerender(<JsonFormatterPanel {...authoredProps} ownerPrincipalId="operator:other" ownerSessionId="other-root"/>);expect(screen.queryByRole("button",{name:"Retry exact authored package request"})).toBeNull();expect(sessionStorage.getItem(staticKey)).toBe(original);expect(bodies).toHaveLength(1);
 mounted.unmount();render(<JsonFormatterPanel {...authoredProps}/>);fireEvent.click(await screen.findByRole("button",{name:"Retry exact authored package request"}));await waitFor(()=>expect(readToolPending(staticKey)).toBeNull());
 expect(bodies).toHaveLength(2);expect(bodies[1]).toBe(bodies[0]);
 expect(JSON.parse(bodies[0])).toEqual(action==="rollback"?{goal_id:"goal-one",content_digest:pending.packet.content_digest,authority_digest:pending.packet.authority_digest,approval_id:"original-approval"}:{goal_id:"goal-one",content_digest:pending.packet.content_digest,authority_digest:pending.packet.authority_digest,manifest:pending.packet.manifest,root_path:pending.packet.root_path,review_id:"original-review",approval_id:"original-approval"});
 expect(paths.every(path=>path.endsWith("/inspect")||path.endsWith("/"+action))).toBe(true);
});

it.each([
 ["package",{pack_id:"local.other-package",manifest:{id:"local.other-package",version:"1.0.0"},descriptor:{capability_id:"pack.local.other-package.summarize.v1"}}],
 ["content",{content_digest:"c".repeat(64)}],
 ["authority",{authority_digest:"c".repeat(64)}],
 ["descriptor",{descriptor:{capability_id:"pack.local.time-ledger-summary.other.v1"}}],
 ["manifest",{manifest:{id:"local.time-ledger-summary",version:"2.0.0"}}],
] as const)("denies retained activation when fresh %s pins differ",async(_name,change)=>{
 const pending:ToolPending={kind:"approve",goal_id:"goal-one",goal_revision:1,packet:blockedAuthoredPacket as ToolProfile,step:3,review_id:"original-review",approval_id:"original-approval"};retainToolPending(staticKey,pending);const original=sessionStorage.getItem(staticKey);
 vi.mocked(apiFetch).mockImplementation(async(url)=>{expect(String(url)).toMatch(/\/authored\/inspect$/);return new Response(JSON.stringify({...blockedAuthoredPacket,...change,profile:{status:"available"}}));});
 render(<JsonFormatterPanel {...authoredProps}/>);fireEvent.click(await screen.findByRole("button",{name:"Retry exact authored package request"}));await screen.findByText(/Current authored package pins changed/);expect(vi.mocked(apiFetch)).toHaveBeenCalledTimes(1);expect(sessionStorage.getItem(staticKey)).toBe(original);
});

it("checks fresh availability before starting combined authored review and approval",async()=>{
 let available=true;const paths:string[]=[];
 vi.mocked(apiFetch).mockImplementation(async(url)=>{const path=String(url);paths.push(path);expect(path).toMatch(/\/authored\/inspect$/);return new Response(JSON.stringify({...blockedAuthoredPacket,profile:available?{status:"available"}:blockedAuthoredPacket.profile}));});
 render(<JsonFormatterPanel {...authoredProps}/>);fireEvent.change(screen.getByLabelText("Selected package directory"),{target:{value:blockedAuthoredPacket.root_path}});fireEvent.click(screen.getByRole("button",{name:"Refresh manifest and dependencies"}));await screen.findByText(/Selected capability:/);fireEvent.change(screen.getByLabelText("Authored package Goal"),{target:{value:"goal-one"}});fireEvent.click(screen.getByRole("checkbox"));available=false;
 fireEvent.click(screen.getByRole("button",{name:"Review and approve exact authored package"}));await screen.findByText(/Current authored execution profile unavailable/);expect(paths).toHaveLength(2);expect(readToolPending(staticKey)).toMatchObject({kind:"approve",step:0,review_id:null,approval_id:null});expect(screen.getByRole("button",{name:"Create authored package task"})).toBeDisabled();
});

it.each(["revoke","uninstall"] as const)("keeps retained %s usable with a blocked authored profile",async(action)=>{
 retainToolPending(staticKey,{kind:"approve",goal_id:"goal-one",goal_revision:1,packet:blockedAuthoredPacket as ToolProfile,step:3,review_id:null,approval_id:"withdrawal-approval",action});
 vi.mocked(apiFetch).mockImplementation(async(url)=>{expect(String(url)).toMatch(new RegExp(`/local.time-ledger-summary/${action}$`));return new Response('{}');});
 render(<JsonFormatterPanel {...authoredProps}/>);fireEvent.click(await screen.findByRole("button",{name:"Retry exact authored package request"}));await waitFor(()=>expect(readToolPending(staticKey)).toBeNull());expect(vi.mocked(apiFetch)).toHaveBeenCalledTimes(1);
});
