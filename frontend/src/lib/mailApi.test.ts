import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  MailApiError,
  createMailConnection,
  createMailWatch,
  listMailConnections,
  validateMailConnectionResponse,
  validateMailConsentResponse,
} from "./mailApi";

function response(payload: unknown, ok = true, status = ok ? 200 : 500) {
  return { ok, status, json: async () => payload };
}

const connection = {
  connection_id: "connection-1",
  service: "gmail_readonly",
  label: "Personal read-only",
  revision: 2,
  state: "active",
  scope_status: "scope_unverified",
  declared_scopes: ["https://www.googleapis.com/auth/gmail.readonly"],
  provider_scopes_verified: false,
  verified_setup_job_id: null,
};

describe("mailApi", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("loads owner metadata without a provider contact or caller owner fields", async () => {
    fetchMock.mockResolvedValueOnce(response({ connections: [connection] }));
    await expect(listMailConnections()).resolves.toEqual([connection]);
    expect(fetchMock).toHaveBeenCalledWith(expect.stringContaining("/api/capabilities/mail/connections"), expect.objectContaining({ method: "GET", credentials: "include" }));
    expect(JSON.stringify(fetchMock.mock.calls[0])).not.toContain("owner_principal");
  });

  it("keeps credentials in the one setup request and rejects malformed metadata", async () => {
    fetchMock.mockResolvedValueOnce(response({ connection }));
    await createMailConnection({
      schema_version: 1,
      service: "gmail_readonly",
      label: "Personal read-only",
      client_id: "client-id",
      client_secret: "client-secret",
      refresh_token: "refresh-token",
      declared_scopes: ["https://www.googleapis.com/auth/gmail.readonly"],
      idempotency_key: "setup-key-1",
    });
    const [, init] = fetchMock.mock.calls[0] as [RequestInfo, RequestInit];
    expect(JSON.parse(String(init.body))).toMatchObject({ refresh_token: "refresh-token", client_secret: "client-secret" });
    expect(() => validateMailConnectionResponse({ connection: { ...connection, declared_scopes: ["https://www.googleapis.com/auth/gmail.modify"] } })).toThrow(MailApiError);
  });

  it("does not accept source consent metadata that grants unexpected fields", () => {
    expect(() => validateMailConsentResponse({ consent: {
      consent_id: "consent-1",
      connection_id: "connection-1",
      connection_revision: 2,
      goal_id: "goal-1",
      goal_revision: 4,
      label_ids: ["label-1"],
      window_days: 7,
      max_messages: 2,
      source_read_allowed: true,
      source_revision: 1,
      model_egress_allowed: false,
      model_revision: 1,
      allowed_body_fields: ["subject", "unexpected"],
      expires_at: "2026-10-02T12:00:00Z",
      state: "active",
      revision: 1,
    } })).toThrow(MailApiError);
  });

  it("maps a network failure to an unconfirmed outcome without echoing request data", async () => {
    fetchMock.mockRejectedValueOnce(new Error("refresh-token-secret must not escape"));
    await expect(listMailConnections()).rejects.toMatchObject({ code: "mail_transport_unavailable", status: 0 });
    try {
      await listMailConnections();
    } catch (error) {
      expect(String(error)).not.toContain("refresh-token-secret");
    }
  });

  it("serializes the final scalar watch wire without inventing a nested cadence", async () => {
    fetchMock.mockResolvedValueOnce(response({
      watch: {
        watch_id: "watch-1",
        scheduled_job_id: "scheduled-1",
        capability_id: "gmail.scan_metadata.v1",
        connection_id: "connection-1",
        connection_revision: 2,
        mail_consent_id: "consent-1",
        source_consent_revision: 1,
        goal_id: "goal-1",
        goal_revision: 4,
        label_ids: ["label-1"],
        cadence: { kind: "hourly", timezone: "UTC", daily_hour: null, daily_minute: null },
        binding_revision: 1,
        expires_at: "2026-10-02T12:00:00Z",
        state: "active",
        watch_state: "not_started",
        baseline_complete: false,
        last_observed_at: null,
        last_completed_occurrence_id: null,
        skipped_coverage_reason: "baseline_pending",
        list_page_complete: false,
        latest_occurrence: null,
      },
      status: "accepted",
    }));
    await createMailWatch({
      schema_version: 1,
      connection_id: "connection-1",
      expected_connection_revision: 2,
      mail_consent_id: "consent-1",
      expected_source_consent_revision: 1,
      goal_id: "goal-1",
      expected_goal_revision: 4,
      label_ids: ["label-1"],
      cadence: "hourly",
      timezone: "UTC",
      expires_at: "2026-10-02T12:00:00Z",
      max_messages: 3,
      idempotency_key: "watch-key-1",
    });
    const [, init] = fetchMock.mock.calls[0] as [RequestInfo, RequestInit];
    expect(JSON.parse(String(init.body))).toMatchObject({ cadence: "hourly", timezone: "UTC" });
    expect(JSON.parse(String(init.body)).cadence).not.toEqual(expect.objectContaining({ kind: "hourly" }));
  });
});
