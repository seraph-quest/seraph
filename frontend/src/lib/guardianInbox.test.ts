import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { applyGuardianInboxAction, GuardianInboxApiError } from "./guardianInbox";

describe("guardian inbox action receipts", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("fails with a typed 502 when the server action receipt has an unknown state", async () => {
    fetchMock.mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ id: "inbox-1", revision: 4, state: "future_state", receipt_id: "receipt-1" }),
    });

    const error = await applyGuardianInboxAction("inbox-1", {
      action: "accept_followup",
      expected_revision: 3,
      idempotency_key: "gesture-1",
    }).catch((value: unknown) => value);

    expect(error).toBeInstanceOf(GuardianInboxApiError);
    expect(error).toMatchObject({ status: 502, code: "invalid_inbox_action_receipt" });
  });
});
