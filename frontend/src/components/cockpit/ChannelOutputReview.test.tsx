import { StrictMode } from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import wire from "../../lib/__fixtures__/channel-output-wire-r1.json";
import { ChannelOutputReview } from "./ChannelOutputReview";

const handle = `sco1.YWN0dWFsLXNpc25lZC1oYW5kbGU.${"1".repeat(64)}`;
const base = { handle, ownerPrincipalId: "actual-owner", ownerSessionId: wire.owner_session_id, goalScope: "actual-goal:1" };
afterEach(() => vi.unstubAllGlobals());

it("uses a fresh StrictMode controller and opens only the actual authenticated output receipt", async () => {
  const verified = vi.fn(), discard = vi.fn();
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => wire });
  vi.stubGlobal("fetch", fetch);
  render(<StrictMode><ChannelOutputReview {...base} onVerified={verified} onDiscard={discard} /></StrictMode>);
  await screen.findByText(/Verified output is open for private review/);
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(fetch.mock.calls[0][1].signal.aborted).toBe(true);
  expect(fetch.mock.calls[1][1].signal.aborted).toBe(false);
  expect(verified).toHaveBeenCalledTimes(1);
  expect(verified.mock.calls[0][0]).toMatchObject({ taskId: wire.task_id, attemptId: wire.attempt_id,
    ownerSessionId: wire.owner_session_id, reference: { artifact_id: wire.reference.artifact_id } });
  expect(fetch.mock.calls.every(call => call[0].includes("/api/telegram/output-review?handle="))).toBe(true);
});

it("discards a late private receipt on Root or Goal revision change", async () => {
  const verified = vi.fn(), discard = vi.fn();
  const resolves: ((value: unknown) => void)[] = [];
  const fetch = vi.fn((_url: RequestInfo | URL, _init?: RequestInit) => new Promise(resolve => resolves.push(resolve)));
  vi.stubGlobal("fetch", fetch);
  const view = render(<ChannelOutputReview {...base} onVerified={verified} onDiscard={discard} />);
  const firstSignal = fetch.mock.calls[0][1]?.signal;
  view.rerender(<ChannelOutputReview {...base} goalScope="actual-goal:2" onVerified={verified} onDiscard={discard} />);
  expect(firstSignal?.aborted).toBe(true);
  await act(async () => resolves[0]({ ok: true, json: async () => wire }));
  expect(verified).not.toHaveBeenCalled();
  await act(async () => resolves[1]({ ok: false, status: 409 }));
  await screen.findByText(/Output access changed or expired/);
  expect(discard).toHaveBeenCalledTimes(1);
});

it("does not fetch or clear ordinary inspections when there is no output link", async () => {
  const fetch = vi.fn(), discard = vi.fn(); vi.stubGlobal("fetch", fetch);
  render(<ChannelOutputReview {...base} handle={null} onVerified={vi.fn()} onDiscard={discard} />);
  await waitFor(() => expect(fetch).not.toHaveBeenCalled());
  expect(discard).not.toHaveBeenCalled();
});
