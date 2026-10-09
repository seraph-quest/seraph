import { StrictMode } from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import wire from "../../lib/__fixtures__/channel-audio-task-wire-r6.json";
import type { GoalInfo } from "../../types";
import { PttAudioControl, type PttTaskRecovery } from "./PttAudioControl";

const ownerPrincipalId = wire.receipt.task.owner_principal_id, ownerSessionId = wire.receipt.task.owner_session_id;
const sessionId = wire.source.session_id;
const goal: GoalInfo = { id: wire.receipt.task.goal_id, revision: wire.receipt.task.goal_revision,
  owner_session_id: ownerSessionId, status: "active", title: "Current original Goal", parent_id: null,
  path: "current", level: "objective", description: null, domain: "work", start_date: null,
  due_date: null, sort_order: 0 };
function recovery(): { current: PttTaskRecovery | null } {
  const { intent: _privateText, ...input } = wire.request.task.input;
  return { current: { scope: JSON.stringify([ownerPrincipalId, ownerSessionId, sessionId]),
    requestId: wire.source.request_id, confirmedDigest: wire.request.confirmed_transcript_digest,
    createdTaskId: null, task: { ...wire.request.task, input: { ...input, evidence_refs: [], limits: {
      max_steps: 16, max_inference_calls: input.limits.max_inference_calls, wall_seconds: 900,
      depth: 0, max_outstanding_children: 2, max_cost_microusd: input.limits.max_cost_microusd } } } } };
}
const props = { ownerPrincipalId, ownerSessionId, sessionId, goals: [goal] };
afterEach(() => vi.unstubAllGlobals());

// Wire is from actual authenticated native capture/confirmation → fresh Goal
// Task POST/GET/replay, channel-completion-r4; only final browser API is mocked.
it("rereads canonical confirmed Message and checks the same uncertain request after a pane remount", async () => {
  const retained = recovery(), created = vi.fn();
  let posts = 0;
  const fetch = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === "POST") {
      if (++posts === 1) throw new TypeError("uncertain response");
      return { ok: true, json: async () => wire.receipt };
    }
    expect(String(url)).toBe(`/api/audio/ptt/${wire.source.request_id}`);
    return { ok: true, json: async () => wire.source };
  });
  vi.stubGlobal("fetch", fetch);
  const first = render(<StrictMode><PttAudioControl {...props} taskRecovery={retained} onTaskCreated={created} /></StrictMode>);
  await screen.findByRole("button", { name: "Check same Task request" });
  expect(fetch.mock.calls[0][1]?.signal?.aborted).toBe(true);
  expect(JSON.stringify(retained)).not.toContain(wire.source.transcript.text);
  fireEvent.click(screen.getByRole("button", { name: "Check same Task request" }));
  await screen.findByText("uncertain response");
  const originalBody = fetch.mock.calls.find(call => call[1]?.method === "POST")?.[1]?.body;
  first.unmount();
  render(<PttAudioControl {...props} taskRecovery={retained} onTaskCreated={created} />);
  await screen.findByRole("button", { name: "Check same Task request" });
  fireEvent.click(screen.getByRole("button", { name: "Check same Task request" }));
  await screen.findByText(/Review-only Task prepared. Audio accounting remains separate/);
  const submitted = fetch.mock.calls.filter(call => call[1]?.method === "POST");
  expect(submitted).toHaveLength(2);
  expect(submitted[1][1]?.body).toBe(originalBody);
  expect(created).toHaveBeenCalledWith(wire.receipt.task.task_id);
  expect(fetch.mock.calls.some(call => String(call[0]).includes("/process"))).toBe(false);
  expect(JSON.stringify(retained)).not.toContain(wire.source.transcript.text);
});

it("clears a denied current source and never posts a replacement request", async () => {
  const retained = recovery();
  const fetch = vi.fn().mockResolvedValue({ ok: false, status: 409 }); vi.stubGlobal("fetch", fetch);
  render(<PttAudioControl {...props} taskRecovery={retained} />);
  await waitFor(() => expect(retained.current).toBeNull());
  expect(screen.queryByRole("button", { name: "Check same Task request" })).not.toBeInTheDocument();
  expect(fetch).toHaveBeenCalledTimes(1);
});

it("fences a late confirmed private response after Root replacement", async () => {
  const retained = recovery(); let resolve!: (value: unknown) => void;
  const fetch = vi.fn((_url: RequestInfo | URL, _init?: RequestInit) => new Promise(done => { resolve = done; }));
  vi.stubGlobal("fetch", fetch);
  const view = render(<PttAudioControl {...props} taskRecovery={retained} />);
  const signal = fetch.mock.calls[0][1]?.signal;
  view.rerender(<PttAudioControl {...props} ownerSessionId="replacement-root" goals={[]} taskRecovery={retained} />);
  expect(signal?.aborted).toBe(true);
  await act(async () => resolve({ ok: true, json: async () => wire.source }));
  expect(retained.current).toBeNull();
  expect(screen.queryByRole("button", { name: "Check same Task request" })).not.toBeInTheDocument();
});
