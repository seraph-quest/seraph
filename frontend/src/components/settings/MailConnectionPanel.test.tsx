import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MailConnectionPanel } from "./MailConnectionPanel";

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

const label = {
  label_id: "label-1",
  name: "Inbox",
  type: "system",
  connection_id: "connection-1",
  connection_revision: 2,
  revision: 1,
  state: "active",
};

const consent = {
  consent_id: "consent-1",
  connection_id: "connection-1",
  connection_revision: 2,
  goal_id: "goal-1",
  goal_revision: 4,
  label_ids: ["label-1"],
  window_days: 7,
  max_messages: 3,
  source_read_allowed: true,
  source_revision: 1,
  model_egress_allowed: false,
  model_revision: 1,
  allowed_body_fields: ["subject", "plainbody", "replyintent"],
  expires_at: "2026-10-02T12:00:00Z",
  state: "active",
  revision: 1,
};

const goals = [{ id: "goal-1", parent_id: null, path: "goal-1", level: "root", title: "Review important mail", description: null, status: "active", domain: "work", start_date: null, due_date: null, sort_order: 0, revision: 4, proactive_enabled: true }];

function scanResponse() {
  return {
    connection_id: "connection-1",
    connection_revision: 2,
    consent_id: "consent-1",
    source_consent_revision: 1,
    messages: [{ source_binding_id: "message-1", message_key: "message-key-1", thread_key: "thread-key-1", message_revision: "sha256:" + "a".repeat(64), received_at: "2026-10-01T09:00:00Z", subject: "Subject preview", preview: "Metadata preview only", read_status: "unread", fetched_at: "2026-10-01T09:01:00Z" }],
    coverage: { list_page_complete: true, more_available: false, returned: 1, max_messages: 3, window_days: 7 },
    provider_contact: true,
    control_job_id: "job-scan-1",
  };
}

function readResponse() {
  return {
    source_binding_id: "message-1",
    message_key: "message-key-1",
    thread_key: "thread-key-1",
    message_revision: "sha256:" + "a".repeat(64),
    subject: "Private subject",
    plain_text: "PRIVATE BODY SHOULD APPEAR ONLY AFTER ACK",
    truncated: false,
    read_status: "unread",
    received_at: "2026-10-01T09:00:00Z",
    fetched_at: "2026-10-01T09:02:00Z",
    provenance: { connection_id: "connection-1", connection_revision: 2, consent_id: "consent-1", source_consent_revision: 1, memory_status: "no_learning", egress: "local_only" },
    provider_contact: true,
    control_job_id: "job-read-1",
  };
}

describe("MailConnectionPanel", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (url.endsWith("/api/goals/tree")) return Promise.resolve(response(goals));
      if (url.endsWith("/api/capabilities/mail/connections") && method === "GET") return Promise.resolve(response({ connections: [connection] }));
      if (url.includes("/api/capabilities/mail/labels") && method === "GET") return Promise.resolve(response({ connection_id: "connection-1", connection_revision: 2, labels: [label], provider_contact: false }));
      if (url.includes("/api/capabilities/mail/read-consents") && method === "GET") return Promise.resolve(response({ consents: [consent], provider_contact: false }));
      if (url.endsWith("/api/capabilities/mail/connections") && method === "POST") return Promise.resolve(response({ connection }));
      if (url.includes("/verify")) return Promise.resolve(response({ connection_id: "connection-1", connection_revision: 2, verification: "provider_read_succeeded", label_count: 1, scope_status: "verified", provider_scopes_verified: true, provider_contact: true, control_job_id: "job-verify" }));
      if (url.endsWith("/labels/refresh")) return Promise.resolve(response({ connection_id: "connection-1", connection_revision: 2, labels: [label], coverage: { complete: true, count: 1 }, scope_status: "verified", provider_scopes_verified: true, provider_contact: true, control_job_id: "job-labels" }));
      if (url.endsWith("/read-consents") && method === "POST") return Promise.resolve(response({ consent }));
      if (url.includes("/messages/scan")) return Promise.resolve(response(scanResponse()));
      if (url.includes("/messages/message-1/read")) return Promise.resolve(response(readResponse()));
      if (url.includes("/model-consent")) return Promise.resolve(response({ consent: { ...consent, model_egress_allowed: true, model_revision: 2, revision: 2 } }));
      return Promise.resolve(response({ detail: { code: "not_found", message: "No test route" } }, false, 404));
    });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("keeps credentials write-only and does not contact Gmail during ordinary metadata loading", async () => {
    render(<MailConnectionPanel ownerPrincipalId="operator:single" ownerSessionId="session-1" />);
    await screen.findByText("Personal read-only");
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/verify") || String(url).includes("/labels/refresh") || String(url).includes("/messages/"))).toBe(false);
    fireEvent.change(screen.getByLabelText("Label"), { target: { value: "New read-only" } });
    fireEvent.change(screen.getByLabelText("Client ID"), { target: { value: "client-id" } });
    fireEvent.change(screen.getByLabelText("Refresh token"), { target: { value: "refresh-secret" } });
    fireEvent.click(screen.getByRole("button", { name: "Save connection" }));
    await waitFor(() => expect(screen.queryByDisplayValue("refresh-secret")).not.toBeInTheDocument());
    const post = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "POST");
    expect(JSON.stringify(post)).toContain("refresh-secret");
    expect(screen.queryByText("refresh-secret")).not.toBeInTheDocument();
  });

  it("requires source acknowledgement and keeps body private until a selected read acknowledgement", async () => {
    render(<MailConnectionPanel ownerPrincipalId="operator:single" ownerSessionId="session-1" />);
    await screen.findByText("Personal read-only");
    fireEvent.click(screen.getByRole("checkbox", { name: /Inbox/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /I acknowledge one bounded metadata\/body read/ }));
    fireEvent.click(screen.getByRole("button", { name: "Create source consent" }));
    await waitFor(() => expect(screen.getByText(/Current consents/)).toBeInTheDocument());
    fireEvent.change(screen.getByLabelText("Messages"), { target: { value: "3" } });
    fireEvent.click(screen.getByRole("button", { name: "Scan metadata \(provider read\)" }));
    await screen.findByRole("button", { name: /Subject preview/ });
    expect(screen.queryByText("PRIVATE BODY SHOULD APPEAR ONLY AFTER ACK")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Subject preview/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /I acknowledge one bounded full read/ }));
    fireEvent.click(screen.getByRole("button", { name: "Read selected message" }));
    await screen.findByText("PRIVATE BODY SHOULD APPEAR ONLY AFTER ACK");
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/messages/scan"))).toBe(true);
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/messages/message-1/read"))).toBe(true);
  });

  it("shows explicit provider verification as a separate action", async () => {
    render(<MailConnectionPanel ownerPrincipalId="operator:single" ownerSessionId="session-1" />);
    await screen.findByText("Personal read-only");
    fireEvent.click(screen.getByRole("button", { name: "Verify (provider read)" }));
    await waitFor(() => expect(screen.getByText(/Provider verification completed/)).toBeInTheDocument());
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/verify"))).toBe(true);
  });
});
