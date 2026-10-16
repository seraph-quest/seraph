import { StrictMode } from "react";
import { act, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import wire from "../../lib/__fixtures__/channel-task-review-wire-r11.json";
import { decodeChannelTaskReview } from "../../lib/channelCapture";
import { ChannelTaskReview } from "./ChannelTaskReview";

const base = { handle: "stc1:original_server_token", ownerPrincipalId: "actual-owner",
  ownerSessionId: wire.original_root_id, goalScope: `${wire.goal_id}:${wire.goal_revision}` };
afterEach(() => vi.unstubAllGlobals());

it("opens the actual authenticated review wire once with a fresh StrictMode controller", async () => {
  const verified = vi.fn(), discard = vi.fn();
  const fetch = vi.fn().mockResolvedValue({ ok: true, json: async () => wire });
  vi.stubGlobal("fetch", fetch);
  render(<StrictMode><ChannelTaskReview {...base} onVerified={verified} onDiscard={discard} /></StrictMode>);
  await screen.findByText(/Current Task is open in Work for review/);
  expect(verified).toHaveBeenCalledExactlyOnceWith({ taskId: wire.task_id, taskRevision: wire.task_revision,
    goalId: wire.goal_id, goalRevision: wire.goal_revision });
  expect(fetch).toHaveBeenCalledTimes(2);
  expect(fetch.mock.calls[0][1].signal.aborted).toBe(true);
  expect(fetch.mock.calls[1][1].signal.aborted).toBe(false);
  expect(fetch.mock.calls.every(call => call[0].includes("/api/telegram/task-review?handle="))).toBe(true);
});

it("discards late review on Goal change and clears an opened review after revocation", async () => {
  const verified = vi.fn(), discard = vi.fn();
  const resolves: ((value: unknown) => void)[] = [];
  vi.stubGlobal("fetch", vi.fn(() => new Promise(resolve => resolves.push(resolve))));
  const view = render(<ChannelTaskReview {...base} onVerified={verified} onDiscard={discard} />);
  view.rerender(<ChannelTaskReview {...base} goalScope="changed:2" onVerified={verified} onDiscard={discard} />);
  await act(async () => resolves[0]({ ok: true, json: async () => wire }));
  expect(verified).not.toHaveBeenCalled();
  await act(async () => resolves[1]({ ok: true, json: async () => wire }));
  expect(verified).toHaveBeenCalledTimes(1);
  view.rerender(<ChannelTaskReview {...base} ownerSessionId="new-root" onVerified={verified} onDiscard={discard} />);
  expect(discard).toHaveBeenCalledTimes(1);
  await act(async () => resolves[2]({ ok: false }));
  await screen.findByText(/Review access changed or expired/);
});

it("rejects foreign Root, extra authority fields and generic Task focus shapes", () => {
  expect(() => decodeChannelTaskReview(wire, "foreign-root")).toThrow();
  expect(() => decodeChannelTaskReview({ ...wire, approved: true }, wire.original_root_id)).toThrow();
  expect(() => decodeChannelTaskReview({ task_id: wire.task_id }, wire.original_root_id)).toThrow();
});
