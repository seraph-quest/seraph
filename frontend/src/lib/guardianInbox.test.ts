import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { applyGuardianInboxAction, GuardianInboxApiError, normalizeGuardianInboxItem, normalizeOpportunityPlanReference,
  normalizeOpportunityPlanOffer, normalizeOpportunityPlanPreview, readOpportunityPlanRequest, retainOpportunityPlanRequest } from "./guardianInbox";

describe("fixed opportunity plan wire", () => {
  const pending = { proposal_id: "proposal-1", kind: "opportunity_plan", proposal_revision: 1, parent_task_id: "task-1",
    parent_revision: 1, proposal_digest: null, expires_at: "2030-01-01T00:00:00Z", status: "pending_inference", blueprint_id: null };
  it("retains required pending IDs and nullable digest/blueprint without inventing metadata", () => {
    expect(normalizeOpportunityPlanReference(pending)).toEqual(pending);
    expect(normalizeOpportunityPlanReference({ ...pending, status: "proposed" })).toBeNull();
  });
  it.each([{ status: "ready" }, { kind: "arbitrary_executor" }, { proposal_revision: 0 }, { proposal_digest: "" },
    { blueprint_id: "invented" }, { generation_retry_allowed: true }, { provider_contact_state: "unknown", generation_retry_allowed: true },
    { recovery_reason: "raw_exception:secret" }, { parent_task_id: null }])("rejects malformed plan reference %j", (override) => {
    expect(normalizeOpportunityPlanReference({ ...pending, ...override })).toBeNull();
  });
  it("keeps eligible blueprints visible when the contacted daily cap blocks generation", () => {
    const offer = { available_blueprint_ids: ["public-browser-check", "public-evidence-report"], unavailable_reason: null,
      can_generate: false, generation_block_reason: "daily_plan_cap", proposal_ref: null };
    expect(normalizeOpportunityPlanOffer(offer)).toEqual(offer);
    expect(normalizeOpportunityPlanOffer({ ...offer, available_blueprint_ids: ["arbitrary"] })).toBeNull();
    expect(normalizeOpportunityPlanOffer({ ...offer, can_generate: true })).toBeNull();
  });
  it("requires exact request retention and rejects corrupted replay authority", () => {
    window.sessionStorage.clear();
    const request = { expected_opportunity_revision: 3, expected_goal_revision: 4, idempotency_key: "12345678-1234-4123-8123-123456789abc" };
    retainOpportunityPlanRequest("plan-wire-test", request);
    expect(readOpportunityPlanRequest("plan-wire-test")).toEqual(request);
    window.sessionStorage.setItem("plan-wire-test", JSON.stringify({ ...request, expected_goal_revision: 1.5 }));
    expect(() => readOpportunityPlanRequest("plan-wire-test")).toThrow(/corrupt/);
    window.sessionStorage.clear();
  });
  it("rejects preview input fabricated for an unverified CPU producer", () => {
    const checks = [{ kind: "url_host", value: "example.com" }, { kind: "url_path_prefix", value: "/source" }];
    const preview = { opportunity_id: "opportunity-1", opportunity_revision: 3, goal_id: "goal-1", goal_revision: 4,
      source_id: "source-1", source_digest: "a".repeat(64), watch_id: "watch-1", watch_revision: 2,
      blueprint_id: "public-evidence-report", review_expires_at: "2030-01-01T00:00:00Z", deadline_at: null, no_learning: true,
      steps: [{ slot: "public_source", capability_id: "browser.public-task.v1", output_schema: "browser_public_task_result",
        input_materialization: "bound", input: { schema_version: 1, start_url: "https://example.com/source", allowed_hosts: ["example.com"],
          approved_url_prefixes: ["https://example.com/source"], final_expected_checks: checks,
          actions: [{ kind: "navigate", url: "https://example.com/source", expected_checks: checks }, { kind: "extract", selector: "body", max_chars: 8192, expected_checks: checks }] },
        permissions: ["browser.public"], native_approvals: [], runtime_seconds: 180, output_bytes: 65536 },
      { slot: "evidence_dossier", capability_id: "work.evidence-dossier.v1", output_schema: "evidence_dossier.v1", input_materialization: "after_verified_producer", input: null,
        permissions: [], native_approvals: [], runtime_seconds: 30, output_bytes: 65536 },
      { slot: "local_report", capability_id: "work.local-evidence-report.v1", output_schema: "text/plain", input_materialization: "after_verified_producer", input: null,
        permissions: [], native_approvals: [], runtime_seconds: 30, output_bytes: 65536 }] };
    expect(normalizeOpportunityPlanPreview(preview)).toEqual(preview);
    expect(normalizeOpportunityPlanPreview({ ...preview, steps: [preview.steps[0], { ...preview.steps[1], input: { fabricated: true } }, preview.steps[2]] })).toBeNull();
    expect(normalizeOpportunityPlanPreview({ ...preview, no_learning: false })).toBeNull();
  });
});

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
