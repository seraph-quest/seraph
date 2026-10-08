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

// Positive consent fixtures stay finite and live relative to this test run.
const fixtureExpiry = new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString();

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
  expires_at: fixtureExpiry,
  state: "active",
  revision: 1,
};

function watchProjection(state: "active" | "paused" = "active", bindingRevision = state === "active" ? 1 : 2) {
  return {
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
    binding_revision: bindingRevision,
    expires_at: fixtureExpiry,
    state,
    watch_state: "active",
    baseline_complete: true,
    last_observed_at: "2026-10-01T09:00:00Z",
    last_completed_occurrence_id: null,
    skipped_coverage_reason: null,
    list_page_complete: true,
    latest_occurrence: null,
  };
}

function controlBinding(state: "active" | "paused" | "revoked", bindingRevision: number) {
  return {
    binding_id: "watch-1",
    scheduled_job_id: "scheduled-1",
    capability_id: "gmail.scan_metadata.v1",
    action_type: "gmail.scan_metadata.v1",
    goal_id: "goal-1",
    goal_revision: 4,
    input_artifact_id: "artifact-1",
    input_digest: "sha256:" + "a".repeat(64),
    consent_kind: "mail_read",
    consent_id: "consent-1",
    consent_revision: 1,
    consent_digest: "sha256:" + "b".repeat(64),
    cadence: { kind: "hourly", timezone: "UTC", daily_hour: null, daily_minute: null },
    binding_revision: bindingRevision,
    expires_at: fixtureExpiry,
    state,
    last_slot_utc: null,
    created_at: "2026-10-01T08:00:00Z",
    updated_at: "2026-10-01T09:00:00Z",
    latest_occurrence: null,
  };
}

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
    window.sessionStorage.clear();
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (url.endsWith("/api/goals/tree")) return Promise.resolve(response(goals));
      if (url.endsWith("/api/capabilities/mail/connections") && method === "GET") return Promise.resolve(response({ connections: [connection] }));
      if (url.endsWith("/api/capabilities/mail/watches") && method === "GET") return Promise.resolve(response({ watches: [] }));
      if (url.includes("/api/capabilities/mail/watches/recovery/") && method === "GET") {
        const key = decodeURIComponent(url.split("/recovery/")[1]);
        return Promise.resolve(response({ status: "not_found", idempotency_scope: "mail-watch", idempotency_key: key, watch: null, watch_id: null, input_artifact_id: null, input_digest: null, request_digest: null, goal_id: null, goal_revision: null, recovery_action: "retry_same_key", memory_status: "no_learning" }));
      }
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

  it("fails closed when opaque recovery storage cannot be written", async () => {
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("storage unavailable"); });
    render(<MailConnectionPanel ownerPrincipalId="operator:single" ownerSessionId="session-1" />);
    await screen.findByText("Personal read-only");
    fireEvent.change(screen.getByLabelText("Label"), { target: { value: "Blocked setup" } });
    fireEvent.change(screen.getByLabelText("Client ID"), { target: { value: "client-id" } });
    fireEvent.change(screen.getByLabelText("Refresh token"), { target: { value: "refresh-secret" } });
    fireEvent.click(screen.getByRole("button", { name: "Save connection" }));
    await screen.findByText(/recovery storage is unavailable/i);
    expect(fetchMock.mock.calls.some(([, init]) => (init as RequestInit | undefined)?.method === "POST")).toBe(false);
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

  it("renders active saved watches and verifies pause through the exact CAS receipt and readback", async () => {
    let current = watchProjection();
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (url.endsWith("/api/goals/tree")) return Promise.resolve(response(goals));
      if (url.endsWith("/api/capabilities/mail/connections") && method === "GET") return Promise.resolve(response({ connections: [connection] }));
      if (url.endsWith("/api/capabilities/mail/watches") && method === "GET") return Promise.resolve(response({ watches: [current] }));
      if (url.endsWith("/api/capabilities/mail/watches/watch-1") && method === "GET") return Promise.resolve(response({ watch: current }));
      if (url.endsWith("/api/governed-schedules/watch-1") && method === "PATCH") {
        current = watchProjection("paused", 2);
        return Promise.resolve(response({ binding: controlBinding("paused", 2) }));
      }
      if (url.includes("/api/capabilities/mail/labels") && method === "GET") return Promise.resolve(response({ connection_id: "connection-1", connection_revision: 2, labels: [label], provider_contact: false }));
      if (url.includes("/api/capabilities/mail/read-consents") && method === "GET") return Promise.resolve(response({ consents: [consent], provider_contact: false }));
      return Promise.resolve(response({ detail: { code: "not_found", message: "No test route" } }, false, 404));
    });
    render(<MailConnectionPanel ownerPrincipalId="operator:single" ownerSessionId="session-1" />);
    await screen.findByText("Watch watch-1");
    expect(screen.getByText(/active · binding revision 1/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Pause watch" }));
    await waitFor(() => expect(screen.getAllByText(/paused · binding revision 2/).length).toBeGreaterThan(0));
    const patchCall = fetchMock.mock.calls.find(([, init]) => (init as RequestInit | undefined)?.method === "PATCH");
    expect(patchCall).toBeDefined();
    expect(JSON.parse(String((patchCall?.[1] as RequestInit).body))).toMatchObject({ action: "pause", expected_binding_revision: 1 });
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith("/api/capabilities/mail/watches/watch-1"))).toBe(true);
  });

  it("retains an unknown watch key across same-owner remount and clears it on owner change", async () => {
    const first = render(<MailConnectionPanel ownerPrincipalId="operator:single" ownerSessionId="session-1" />);
    await screen.findByText("Personal read-only");
    await screen.findByRole("checkbox", { name: /Inbox/ });
    fireEvent.click(screen.getByRole("checkbox", { name: /Inbox/ }));
    fireEvent.change(screen.getByLabelText("Messages"), { target: { value: "3" } });
    fetchMock.mockRejectedValueOnce(new Error("network closed"));
    fireEvent.click(screen.getByRole("button", { name: "Create metadata watch" }));
    await screen.findByText(/unconfirmed outcome/);
    const postCount = fetchMock.mock.calls.filter(([, init]) => (init as RequestInit | undefined)?.method === "POST").length;
    expect(postCount).toBe(1);
    expect(window.sessionStorage.length).toBe(1);

    first.unmount();
    const remounted = render(<MailConnectionPanel ownerPrincipalId="operator:single" ownerSessionId="session-1" />);
    await screen.findByText(/unconfirmed outcome/);
    const reconcile = screen.getByRole("button", { name: "Reconcile original watch request" });
    expect(reconcile).toBeEnabled();
    fireEvent.click(reconcile);
    await waitFor(() => expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/api/capabilities/mail/watches/recovery/"))).toBe(true));
    expect(fetchMock.mock.calls.filter(([, init]) => (init as RequestInit | undefined)?.method === "POST").length).toBe(1);

    remounted.rerender(<MailConnectionPanel ownerPrincipalId="operator:other" ownerSessionId="session-2" />);
    await waitFor(() => expect(screen.queryByText(/unconfirmed outcome/)).not.toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Reconcile original watch request" })).not.toBeInTheDocument();
  });

  it("keeps an unknown setup key across remount and reconciles without another credential POST", async () => {
    let setupPosts = 0;
    let recoveryReads = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (url.endsWith("/api/goals/tree")) return Promise.resolve(response(goals));
      if (url.endsWith("/api/capabilities/mail/connections") && method === "GET") return Promise.resolve(response({ connections: [] }));
      if (url.endsWith("/api/capabilities/mail/watches") && method === "GET") return Promise.resolve(response({ watches: [] }));
      if (url.endsWith("/api/capabilities/mail/connections") && method === "POST") {
        setupPosts += 1;
        return Promise.reject(new Error("setup response lost"));
      }
      if (url.includes("/api/capabilities/mail/connections/recovery/") && method === "GET") {
        recoveryReads += 1;
        return Promise.resolve(response({ status: "replayed", idempotency_scope: "mail-connection-setup", idempotency_key: decodeURIComponent(url.split("/recovery/")[1]), connection_id: connection.connection_id, request_digest: "sha256:" + "a".repeat(64), connection, memory_status: "no_learning", recovery_action: null }));
      }
      return Promise.resolve(response({ detail: { code: "not_found", message: "No test route" } }, false, 404));
    });
    const first = render(<MailConnectionPanel ownerPrincipalId="operator:single" ownerSessionId="session-1" />);
    fireEvent.change(screen.getByLabelText("Label"), { target: { value: "Recovery setup" } });
    fireEvent.change(screen.getByLabelText("Client ID"), { target: { value: "client-id" } });
    fireEvent.change(screen.getByLabelText("Refresh token"), { target: { value: "refresh-secret" } });
    fireEvent.click(screen.getByRole("button", { name: "Save connection" }));
    await screen.findByRole("status");
    expect(setupPosts).toBe(1);
    first.unmount();
    render(<MailConnectionPanel ownerPrincipalId="operator:single" ownerSessionId="session-1" />);
    await screen.findByRole("status");
    fireEvent.click(screen.getByRole("button", { name: "Reconcile with metadata" }));
    await screen.findByText("Personal read-only");
    expect(recoveryReads).toBe(1);
    expect(setupPosts).toBe(1);
  });

  it("ignores a delayed setup response after the authenticated owner changes", async () => {
    let owner = "first";
    let resolveSetup!: (value: unknown) => void;
    const setupResponse = new Promise((resolve) => { resolveSetup = resolve; });
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (url.endsWith("/api/goals/tree")) return Promise.resolve(response(goals));
      if (url.endsWith("/api/capabilities/mail/connections") && method === "GET") return Promise.resolve(response({ connections: owner === "first" ? [connection] : [] }));
      if (url.endsWith("/api/capabilities/mail/watches") && method === "GET") return Promise.resolve(response({ watches: [] }));
      if (url.endsWith("/api/capabilities/mail/connections") && method === "POST") return setupResponse;
      return Promise.resolve(response({ detail: { code: "not_found", message: "No test route" } }, false, 404));
    });
    const view = render(<MailConnectionPanel ownerPrincipalId="operator:first" ownerSessionId="session-1" />);
    await screen.findByText("Personal read-only");
    fireEvent.change(screen.getByLabelText("Label"), { target: { value: "Delayed setup" } });
    fireEvent.change(screen.getByLabelText("Client ID"), { target: { value: "client-id" } });
    fireEvent.change(screen.getByLabelText("Refresh token"), { target: { value: "refresh-secret" } });
    fireEvent.click(screen.getByRole("button", { name: "Save connection" }));
    await waitFor(() => expect(fetchMock.mock.calls.filter(([, init]) => (init as RequestInit | undefined)?.method === "POST")).toHaveLength(1));
    expect(window.sessionStorage.length).toBe(1);

    owner = "second";
    view.rerender(<MailConnectionPanel ownerPrincipalId="operator:second" ownerSessionId="session-2" />);
    await waitFor(() => expect(screen.queryByText("Personal read-only")).not.toBeInTheDocument());
    resolveSetup(response({ connection }));
    await Promise.resolve();
    await Promise.resolve();
    expect(screen.queryByText("Personal read-only")).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([, init]) => (init as RequestInit | undefined)?.method === "POST")).toHaveLength(1);
  });

  it("ignores a delayed source-consent receipt after the owner changes", async () => {
    let resolveConsent!: (value: unknown) => void;
    const consentResponse = new Promise((resolve) => { resolveConsent = resolve; });
    let owner = "first";
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (url.endsWith("/api/goals/tree")) return Promise.resolve(response(goals));
      if (url.endsWith("/api/capabilities/mail/connections") && method === "GET") return Promise.resolve(response({ connections: owner === "first" ? [connection] : [] }));
      if (url.endsWith("/api/capabilities/mail/watches") && method === "GET") return Promise.resolve(response({ watches: [] }));
      if (url.includes("/api/capabilities/mail/labels") && method === "GET") return Promise.resolve(response({ connection_id: "connection-1", connection_revision: 2, labels: [label], provider_contact: false }));
      if (url.includes("/api/capabilities/mail/read-consents") && method === "GET") return Promise.resolve(response({ consents: owner === "first" ? [consent] : [], provider_contact: false }));
      if (url.endsWith("/api/capabilities/mail/read-consents") && method === "POST") return consentResponse;
      return Promise.resolve(response({ detail: { code: "not_found", message: "No test route" } }, false, 404));
    });
    const view = render(<MailConnectionPanel ownerPrincipalId="operator:first" ownerSessionId="session-1" />);
    await screen.findByText("Personal read-only");
    fireEvent.click(screen.getByRole("checkbox", { name: /Inbox/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: /I acknowledge one bounded metadata\/body read/ }));
    fireEvent.click(screen.getByRole("button", { name: "Create source consent" }));
    await waitFor(() => expect(fetchMock.mock.calls.filter(([, init]) => (init as RequestInit | undefined)?.method === "POST")).toHaveLength(1));

    owner = "second";
    view.rerender(<MailConnectionPanel ownerPrincipalId="operator:second" ownerSessionId="session-2" />);
    await waitFor(() => expect(screen.queryByText("Personal read-only")).not.toBeInTheDocument());
    resolveConsent(response({ consent }));
    await Promise.resolve();
    await Promise.resolve();
    expect(screen.queryByText("consent-1")).not.toBeInTheDocument();
  });

  it("ignores a delayed model-consent receipt after the selected owner changes", async () => {
    let resolveModel!: (value: unknown) => void;
    const modelResponse = new Promise((resolve) => { resolveModel = resolve; });
    let owner = "first";
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = init?.method ?? "GET";
      if (url.endsWith("/api/goals/tree")) return Promise.resolve(response(goals));
      if (url.endsWith("/api/capabilities/mail/connections") && method === "GET") return Promise.resolve(response({ connections: owner === "first" ? [connection] : [] }));
      if (url.endsWith("/api/capabilities/mail/watches") && method === "GET") return Promise.resolve(response({ watches: [] }));
      if (url.includes("/api/capabilities/mail/labels") && method === "GET") return Promise.resolve(response({ connection_id: "connection-1", connection_revision: 2, labels: [label], provider_contact: false }));
      if (url.includes("/api/capabilities/mail/read-consents") && method === "GET") return Promise.resolve(response({ consents: owner === "first" ? [consent] : [], provider_contact: false }));
      if (url.includes("/model-consent") && method === "POST") return modelResponse;
      return Promise.resolve(response({ detail: { code: "not_found", message: "No test route" } }, false, 404));
    });
    const view = render(<MailConnectionPanel ownerPrincipalId="operator:first" ownerSessionId="session-1" />);
    await screen.findByText("Personal read-only");
    await screen.findByText("consent-1");
    fireEvent.click(screen.getByRole("checkbox", { name: /I acknowledge exactly these text fields/ }));
    fireEvent.click(screen.getByRole("button", { name: "Allow model for draft" }));
    await waitFor(() => expect(fetchMock.mock.calls.filter(([, init]) => (init as RequestInit | undefined)?.method === "POST")).toHaveLength(1));

    owner = "second";
    view.rerender(<MailConnectionPanel ownerPrincipalId="operator:second" ownerSessionId="session-2" />);
    await waitFor(() => expect(screen.queryByText("Personal read-only")).not.toBeInTheDocument());
    resolveModel(response({ consent: { ...consent, model_egress_allowed: true, model_revision: 2, revision: 2 } }));
    await Promise.resolve();
    await Promise.resolve();
    expect(screen.queryByText("Model consent enabled")).not.toBeInTheDocument();
  });
});
