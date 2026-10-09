import { afterEach, describe, expect, it, vi } from "vitest";
import { decodeHomeContinuation, fetchHomeContinuation } from "./homeContinuation";
import { homeFixture } from "./homeContinuation.fixture";

afterEach(() => vi.unstubAllGlobals());
describe("closed Home continuation", () => {
  it("accepts historical rollback and Unknown without claiming execution eligibility", () => { expect(decodeHomeContinuation(homeFixture())).toEqual(homeFixture()); });
  it.each([
    (p: any) => { p.body = "private"; },
    (p: any) => { p.active_goals.items[0].title = "x".repeat(513); },
    (p: any) => { p.active_goals.items[0].target.href = "https://arbitrary"; },
    (p: any) => { p.active_goals.items[0].target.goal_id = "foreign"; },
    (p: any) => { p.active_goals.items[0].status = "completed"; },
    (p: any) => { p.task_next_actions.items[0].method.status = "ready"; },
    (p: any) => { p.task_next_actions.items[0].method.target.version = "other"; },
    (p: any) => { p.prepared_outputs.items[0].method.method_id = "forged"; },
    (p: any) => { p.blocked_items.items[0].reason_code = "private arbitrary reason"; },
    (p: any) => { p.task_next_actions.items[0].priority = 101; },
    (p: any) => { p.active_goals.source_as_of = "2026-10-09T10:00:00"; },
    (p: any) => { p.active_goals.state = "empty"; },
    (p: any) => { p.approvals.items = p.active_goals.items; },
    (p: any) => { delete p.programme_status.items[0].next_digest_at; },
    (p: any) => { p.programme_status.items[0].next_digest_at = "2026-10-09T08:00:00+02:00"; },
    (p: any) => { p.task_next_actions.items[0].method.lifecycle = "unavailable"; },
  ])("rejects unsupported or mismatched metadata %#", mutate => { const p = homeFixture(); mutate(p); expect(() => decodeHomeContinuation(p)).toThrow(); });
  it("accepts an unavailable passive digest without calculating a replacement", () => { const p = homeFixture(); const row = p.programme_status.items[0]; if (row.kind !== "programme") throw Error(); row.next_digest_at = null; expect(decodeHomeContinuation(p).programme_status.items[0]).toEqual(row); });
  it("retains the original baseline admission time without creating a method identity or navigation", () => { const p = homeFixture(); const row = p.task_next_actions.items[0]; if (row.kind !== "task_next_action") throw Error(); row.method = { status: "baseline", method_id: null, version: null, digest: null, admitted_at: p.as_of, lifecycle: "active_metadata", reason_code: null, target: null }; expect(decodeHomeContinuation(p).task_next_actions.items[0]).toEqual(row); row.method.admitted_at = null; expect(() => decodeHomeContinuation(p)).not.toThrow(); row.method.method_id = "forged"; expect(() => decodeHomeContinuation(p)).toThrow(); row.method.method_id = null; row.method.admitted_at = "2026-10-09T10:00:00"; expect(() => decodeHomeContinuation(p)).toThrow(); });
  it("bounds the aggregate rather than each section", () => { const p = homeFixture(); p.active_goals.items = Array(20).fill(p.active_goals.items[0]); expect(() => decodeHomeContinuation(p)).toThrow(/bounded/); });
  it("preserves full UTF-8 Goal identity without normalization", () => { const p = homeFixture(); const row = p.active_goals.items[0]; if (row.kind !== "active_goal") throw Error(); row.goal_id = "💡".repeat(128); row.target.goal_id = row.goal_id; expect(decodeHomeContinuation(p).active_goals.items[0]).toEqual(row); row.goal_id += "x"; row.target.goal_id = row.goal_id; expect(() => decodeHomeContinuation(p)).toThrow(); });
  it("uses one authenticated GET and passes the original cursor unchanged", async () => { const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify(homeFixture()), { headers: { "X-Continuation-Cursor": "opaque-original" } })); vi.stubGlobal("fetch", fetch); const result = await fetchHomeContinuation(new AbortController().signal, "previous-original"); expect(fetch).toHaveBeenCalledTimes(1); expect(String(fetch.mock.calls[0][0])).toContain("limit=20&cursor=previous-original"); expect(fetch.mock.calls[0][1]).toMatchObject({ credentials: "include" }); expect(result.nextCursor).toBe("opaque-original"); });
  it("rejects an invalid cursor header without presenting a fresh snapshot", async () => { vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(homeFixture()), { headers: { "X-Continuation-Cursor": "private/unsupported" } }))); await expect(fetchHomeContinuation(new AbortController().signal)).rejects.toThrow(/cursor/); });
  it.each(["target","kind","state","label","body","recovered"])("denies closed Inbox mismatch %s", change=>{
    const p:any=homeFixture();const row:any={kind:"inbox_decision",inbox_id:"decision",inbox_revision:1,source_kind:"mail_notice",state:"pending",title:"New message in watched mailbox",source_availability:"present",goal_id:"goal-1",goal_revision:1,snoozed_until:null,expires_at:p.as_of,source_at:p.as_of,ownership_access:"current",target:{kind:"inbox",inbox_id:"decision",inbox_revision:1}};
    p.task_next_actions.items=[row];
    if(change==="target")row.target.inbox_id="foreign";if(change==="kind")row.source_kind="private_packet";if(change==="state")row.state="queued";if(change==="label")row.title="Verified private message subject";if(change==="body")row.body="PRIVATE_BODY";if(change==="recovered")row.ownership_access="recovered_read_only";
    expect(()=>decodeHomeContinuation(p)).toThrow();
  });

});
