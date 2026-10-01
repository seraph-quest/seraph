import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { applyGuardianInboxAction, GuardianInboxApiError, normalizeGuardianInboxItem } from "./guardianInbox";

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

  it("normalizes only the private opaque Mail origin from a detail projection", () => {
    const item = normalizeGuardianInboxItem({
      id: "candidate-1",
      revision: 2,
      state: "accepted",
      source_kind: "mail_notice",
      source_id: "message-opaque-1",
      title: "New message in watched mailbox",
      summary: "Open the private Mail view.",
      why_now: "A metadata-only watch observed a message.",
      goal_id: "goal-1",
      goal_revision: 4,
      watch_id: "watch-1",
      plan_revision: 1,
      expires_at: "2026-10-01T12:00:00Z",
      evidence_refs: [],
      allowed_actions: [],
      mail: {
        message_binding_id: "binding-1",
        message_revision: "sha256:" + "a".repeat(64),
        status: "present",
        private: true,
        subject: "must not cross the normalizer",
        plain_text: "must not cross the normalizer",
      },
    });

    expect(item?.mail).toEqual({
      watch_id: "watch-1",
      message_binding_id: "binding-1",
      message_revision: "sha256:" + "a".repeat(64),
      status: "present",
      private: true,
    });
    expect(item).not.toHaveProperty("subject");
    expect(item).not.toHaveProperty("plain_text");
  });

  it("drops a private Mail origin when the exact producing watch is absent", () => {
    const item = normalizeGuardianInboxItem({
      id: "candidate-without-watch",
      revision: 1,
      state: "accepted",
      source_kind: "mail_notice",
      source_id: "message-opaque-1",
      goal_id: "goal-1",
      goal_revision: 4,
      plan_revision: 1,
      expires_at: "2026-10-01T12:00:00Z",
      evidence_refs: [],
      allowed_actions: [],
      mail: { message_binding_id: "binding-1", message_revision: "sha256:" + "a".repeat(64), status: "present", private: true },
    });
    expect(item?.mail).toBeNull();
  });
});
