import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SelectedContextInspector } from "./SelectedContextInspector";
import { apiFetch } from "../../lib/api";
import type { WorkBoardTask } from "../../types";

vi.mock("../../lib/api",()=>({apiFetch:vi.fn()}));
const task={task_id:"task",owner_principal_id:"owner",owner_session_id:"root",task_revision:1,goal_id:"goal",goal_revision:1} as WorkBoardTask;
const props={task,ownerPrincipalId:"owner",ownerSessionId:"root"};
const capture=()=>({job_id:"selected-context:"+"a".repeat(32),kind:"selected_context_v1",source_task_id:"task",status:"succeeded",revision:3,deadline_at:new Date(Date.now()+60000).toISOString(),source:{origin:"https://example.com",path:"/docs"},reviewed_byte_count:8,reviewed_utf8_sha256:"b".repeat(64)});
function reply(value:unknown,status=200){return new Response(JSON.stringify(value),{status,headers:{"Content-Type":"application/json"}});}
async function refresh(){fireEvent.click(screen.getByRole("button",{name:/Refresh selected text/}));await waitFor(()=>expect(screen.getByRole("button",{name:/Refresh selected text/})).not.toBeDisabled());}
describe("Current private selected context",()=>{
  beforeEach(()=>vi.mocked(apiFetch).mockImplementation(async url=>reply(String(url).endsWith("/pairings")?{pairings:[],state_revision:1}:{captures:[capture()],next_cursor:null})));
  afterEach(()=>vi.clearAllMocks());
  it("rejects a foreign task locator before private reads",async()=>{
    vi.mocked(apiFetch).mockImplementation(async url=>reply(String(url).endsWith("/pairings")?{pairings:[],state_revision:1}:{captures:[{...capture(),source_task_id:"foreign"}],next_cursor:null}));
    render(<SelectedContextInspector {...props}/>);await refresh();
    expect(screen.getByRole("status")).toHaveTextContent("selected_context_locator_changed");
    expect(screen.queryByRole("button",{name:"Read current private text"})).not.toBeInTheDocument();
    expect(vi.mocked(apiFetch).mock.calls.some(([url])=>String(url).endsWith("/private"))).toBe(false);
  });
  it("reads canonical metadata first and clears plaintext on blur and Root change",async()=>{
    const view=render(<SelectedContextInspector {...props}/>);await refresh();
    vi.mocked(apiFetch).mockImplementation(async url=>reply({...capture(),...(String(url).endsWith("/private")?{text:"private!"}:{})}));
    fireEvent.click(screen.getByRole("button",{name:"Read current private text"}));await screen.findByText("private!");
    const urls=vi.mocked(apiFetch).mock.calls.map(([url])=>decodeURIComponent(String(url)));expect(urls[urls.length-2]).toMatch(/captures\/selected-context:[a-f0-9]{32}$/);expect(urls[urls.length-1]).toMatch(/\/private$/);
    fireEvent.blur(window);expect(screen.queryByText("private!")).not.toBeInTheDocument();
    view.rerender(<SelectedContextInspector {...props} ownerSessionId="new-root"/>);
    expect(screen.queryByRole("button",{name:"Read current private text"})).not.toBeInTheDocument();
    expect(screen.getByRole("button",{name:/Refresh selected text/})).toBeDisabled();
  });
  it("requires an unchecked explicit target permission",()=>{
    render(<SelectedContextInspector {...props}/>);
    expect(screen.getByRole("checkbox")).not.toBeChecked();expect(screen.getByRole("button",{name:/Permit exact Task/})).toBeDisabled();
  });
});
