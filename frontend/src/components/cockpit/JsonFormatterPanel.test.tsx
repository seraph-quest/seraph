import {fireEvent,render,screen,waitFor} from "@testing-library/react";
import {beforeEach,expect,it,vi} from "vitest";
import {apiFetch} from "../../lib/api";
import {readToolPending,toolStorageKey} from "../../lib/toolPackage";
import type {WorkBoardTask} from "../../types";
import {JsonFormatterPanel} from "./JsonFormatterPanel";
vi.mock("../../lib/api",()=>({apiFetch:vi.fn()}));
const task={task_id:"format-task",capability_id:"work.json-format.v1",task_revision:4} as WorkBoardTask;
const props={task,ownerPrincipalId:"operator:one",ownerSessionId:"session-one"};
const key=toolStorageKey("operator:one","session-one",task.task_id);
const state={task_id:task.task_id,task_revision:4,attempt_id:"original-attempt",job_id:"json-format:original",status:"running",deadline_at:"2026-10-03T16:00:00Z",cleanup_proven:true,recoverable:false,report_available:false,cancel_available:true,no_learning:true,recovery_limit:"Unknown execution remains blocked; exact original slot only."};
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
