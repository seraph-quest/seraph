import { afterEach, describe, expect, it, vi } from "vitest";
import { generalTaskRequest } from "./generalTask";

function response(value: unknown, status: number) {
  return { ok: false, status, json: async () => value } as Response;
}
afterEach(() => vi.unstubAllGlobals());

describe("generalTaskRequest", () => {
  it("names captured-channel recovery instead of a stale revision", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response({ detail: { code: "channel_capture_origin_changed" } }, 409)));
    await expect(generalTaskRequest("/tasks/t/plan")).rejects.toThrow(/needs operator recovery; refreshing will not clear it/);
    vi.stubGlobal("fetch", vi.fn(async () => response({ detail: { code: "channel_output_signer_unavailable" } }, 409)));
    await expect(generalTaskRequest("/tasks/t/plan")).rejects.toThrow(/workspace vault key cannot be read/);
  });

  it("keeps the revision message for other conflicts and unreadable bodies", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => response({ detail: { code: "task_revision_changed" } }, 409)));
    await expect(generalTaskRequest("/tasks/t/plan")).rejects.toThrow(/revision changed/);
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 409, json: async () => { throw new Error("no body"); } }) as unknown as Response));
    await expect(generalTaskRequest("/tasks/t/plan")).rejects.toThrow(/revision changed/);
  });
});
