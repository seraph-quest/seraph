import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MailPanel } from "./MailPanel";

function response(payload: unknown, ok = true, status = ok ? 200 : 500) {
  return { ok, status, json: async () => payload };
}

const verifiedDraft = {
  status: "verified",
  task_id: "mail-task-1",
  draft: { subject: "A private subject", plainbody: "A private draft body", caveats: ["Review before sending."] },
  message_revision: "sha256:" + "b".repeat(64),
  memory_status: "no_learning",
  sent: false,
  saved_to_provider: false,
};

describe("MailPanel", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    window.sessionStorage.clear();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("shows a private draft only through the selected Mail task and keeps copy local", async () => {
    fetchMock.mockResolvedValueOnce(response(verifiedDraft));
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText: vi.fn().mockResolvedValue(undefined) } });
    render(<MailPanel taskId="mail-task-1" ownerPrincipalId="operator:single" ownerSessionId="session-1" />);
    await screen.findByDisplayValue("A private draft body");
    fireEvent.change(screen.getByLabelText("Plain-text draft"), { target: { value: "Edited locally" } });
    fireEvent.click(screen.getByRole("button", { name: "Copy local draft" }));
    await screen.findByText(/Copied locally/);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toContain("/api/capabilities/mail/reply-tasks/mail-task-1/draft");
    expect(fetchMock.mock.calls.some(([, init]) => (init as RequestInit | undefined)?.method === "POST")).toBe(false);
  });

  it("retains an unknown task readback and does not retry it on remount", async () => {
    fetchMock.mockRejectedValueOnce(new Error("network closed"));
    const first = render(<MailPanel taskId="mail-task-unknown" ownerPrincipalId="operator:single" ownerSessionId="session-1" />);
    await screen.findByRole("alert");
    expect(screen.getByRole("alert")).toHaveTextContent(/unconfirmed/);
    first.rerender(<MailPanel taskId="mail-task-unknown" ownerPrincipalId="operator:single" ownerSessionId="session-1" />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(screen.getByRole("button", { name: "Refresh draft readback" })).toBeInTheDocument();
  });

  it("clears private state when the authenticated owner changes", async () => {
    fetchMock.mockResolvedValueOnce(response(verifiedDraft));
    const view = render(<MailPanel taskId="mail-task-1" ownerPrincipalId="operator:single" ownerSessionId="session-1" />);
    await screen.findByDisplayValue("A private draft body");
    fetchMock.mockResolvedValueOnce(response({ status: "pending", task_id: "mail-task-1", recovery_action: "wait_for_dispatch", memory_status: "no_learning" }));
    view.rerender(<MailPanel taskId="mail-task-1" ownerPrincipalId="operator:single" ownerSessionId="session-2" />);
    await waitFor(() => expect(screen.queryByDisplayValue("A private draft body")).not.toBeInTheDocument());
    expect(screen.getByText(/owner-scoped readback/)).toBeInTheDocument();
  });

  it("takes a typed accepted Inbox origin through explicit private read and local reply draft admission", async () => {
    const connection = {
      connection_id: "connection-1",
      service: "gmail_readonly",
      label: "Work Gmail",
      revision: 3,
      state: "active",
      scope_status: "verified",
      declared_scopes: ["https://www.googleapis.com/auth/gmail.readonly"],
      provider_scopes_verified: true,
      verified_setup_job_id: "setup-1",
    };
    const consent = {
      consent_id: "consent-1",
      connection_id: "connection-1",
      connection_revision: 3,
      goal_id: "goal-1",
      goal_revision: 4,
      label_ids: ["INBOX"],
      window_days: 7,
      max_messages: 1,
      sync_metadata_limit: 50,
      source_read_allowed: true,
      source_revision: 5,
      model_egress_allowed: true,
      model_revision: 6,
      allowed_body_fields: ["subject", "plainbody", "replyintent"],
      expires_at: new Date(Date.now() + 60 * 60 * 1000).toISOString(),
      state: "active",
      revision: 7,
    };
    const messageRevision = "sha256:" + "c".repeat(64);
    const watch = {
      watch_id: "watch-1", scheduled_job_id: "scheduled-watch-1", capability_id: "gmail.scan_metadata.v1",
      connection_id: "connection-1", connection_revision: 3, mail_consent_id: "consent-1", source_consent_revision: 5,
      goal_id: "goal-1", goal_revision: 4, label_ids: ["INBOX"], cadence: { kind: "hourly", timezone: "UTC", daily_hour: null, daily_minute: null },
      binding_revision: 2, expires_at: new Date(Date.now() + 60 * 60 * 1000).toISOString(), state: "active", watch_state: "baseline_complete",
      baseline_complete: true, last_observed_at: null, last_completed_occurrence_id: null, skipped_coverage_reason: null,
      list_page_complete: true, latest_occurrence: null,
    };
    const mailOrigin = {
      watch_id: "watch-1",
      message_binding_id: "message-binding-1",
      message_revision: messageRevision,
      status: "present",
      private: true as const,
    };
    const privateRead = {
      source_binding_id: "message-binding-1",
      message_key: "message-opaque-1",
      thread_key: "thread-opaque-1",
      message_revision: messageRevision,
      subject: "Private source subject",
      plain_text: "Private source body",
      truncated: false,
      read_status: "read",
      received_at: new Date().toISOString(),
      fetched_at: new Date().toISOString(),
      provenance: {
        connection_id: "connection-1",
        connection_revision: 3,
        consent_id: "consent-1",
        source_consent_revision: 5,
        memory_status: "no_learning",
        egress: "local_only",
      },
      provider_contact: true,
      control_job_id: "mail-control-1",
    };
    const ref = { provider: "gmail", opaque_id: "a".repeat(64), revision: "b".repeat(64), content_digest: "c".repeat(64), privacy: "owner_private", expires_at: consent.expires_at };
    const related = { classification: "local_related_context_not_model_input", memory_status: "no_learning", sources: [{ connection_ref: { id: "connection-1", revision: 3 }, item_refs: [ref], coverage: { partial: true, pages_read: 1 }, freshness: { expires_at: consent.expires_at } }] };
    const replyReceipt = {
      status: "accepted",
      task_id: "mail-reply-task-1",
      attempt_id: null,
      job_id: null,
      input_artifact_id: "artifact-1",
      input_digest: "sha256:" + "d".repeat(64),
      message_key: "message-opaque-1",
      message_revision: messageRevision,
      goal_id: "goal-1",
      goal_revision: 4,
      source_status: "present",
      effective_route: null,
      recovery_action: null,
      memory_status: "no_learning",
    };
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/connections/connection-1/sync")) return Promise.resolve(response({ connection_id: "connection-1", state: "ready", active_job_id: null, cursor_revision: 1, reservation_state: "available", external_effect_state: "none", unresolved_jobs: [], items: [ref], coverage: related.sources[0].coverage, freshness: related.sources[0].freshness, selection: { goal_ref: { id: "goal-1", revision: 4 }, connection_ref: { id: "connection-1", revision: 3 }, source_scope: { provider: "gmail", consents: [{ id: "consent-1", revision: 5 }], label_ids: ["INBOX"], thread_keys: [] }, window: { start: new Date().toISOString(), end: consent.expires_at }, max_items: 50 } }));
      if (url.endsWith("/api/capabilities/mail/connections")) return Promise.resolve(response({ connections: [connection] }));
      if (url.endsWith("/api/capabilities/mail/read-consents")) return Promise.resolve(response({ consents: [consent], provider_contact: false }));
      if (url.endsWith("/api/capabilities/mail/watches/watch-1")) return Promise.resolve(response({ watch }));
      if (url.endsWith("/api/capabilities/mail/messages/message-binding-1/read")) {
        expect(init?.method).toBe("POST");
        const body = JSON.parse(String(init?.body));
        expect(body.acknowledge_selected_body_read).toBe(true);
        expect(body.message_binding_id).toBe("message-binding-1");
        return Promise.resolve(response(privateRead));
      }
      if (url.endsWith("/api/capabilities/mail/reply-tasks")) {
        expect(init?.method).toBe("POST");
        const body = JSON.parse(String(init?.body));
        expect(body.connected_sources).toEqual([{ connection_ref: { id: "connection-1", revision: 3 }, item_refs: [ref] }]);
        expect(body.acknowledge_connected_sources).toBe(true);
        expect(JSON.stringify(body)).not.toContain("Private source body");
        expect(body.message_binding_id).toBe("message-binding-1");
        expect(body.expected_model_consent_revision).toBe(6);
        expect(body.reply_intent).toBe("Ask for the next available time.");
        return Promise.resolve(response(replyReceipt));
      }
      if (url.endsWith("/api/capabilities/mail/reply-tasks/mail-reply-task-1/draft")) return Promise.resolve(response({ ...verifiedDraft, task_id: "mail-reply-task-1", related_sources: related }));
      throw new Error(`Unexpected Mail request: ${url}`);
    });

    const prepared = vi.fn();
    render(<MailPanel taskId="triage-task-1" ownerPrincipalId="operator:single" ownerSessionId="session-1" mailOrigin={mailOrigin} goalId="goal-1" goalRevision={4} onPrepareCommunication={prepared} />);
    await screen.findByText(/accepted task origin/i);
    const readButton = await screen.findByRole("button", { name: "Read selected private message" });
    expect(screen.queryByText("Private source body")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: /selected message may be read/i }));
    fireEvent.click(readButton);
    await screen.findByText("Private source body");

    fireEvent.change(screen.getByLabelText("Reply intent"), { target: { value: "Ask for the next available time." } });
    fireEvent.click(await screen.findByLabelText(/Use local reference/));
    fireEvent.click(screen.getByRole("button", { name: "Request local reply draft" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Acknowledge the exact local related references");
    fireEvent.click(screen.getByLabelText(/I acknowledge these exact references/));
    const beforeHandoff = fetchMock.mock.calls.length;
    fireEvent.click(screen.getByText("Add this reviewed reply source to communication preparation"));
    expect(prepared).toHaveBeenCalledTimes(1);
    expect(prepared.mock.calls[0][0]).toMatchObject({ message_binding_id: "message-binding-1", expected_model_consent_revision: 6, expected_source_consent_revision: 5, goal_id: "goal-1", expected_goal_revision: 4 });
    expect(JSON.stringify(prepared.mock.calls[0][0])).not.toContain("Private source body");
    expect(prepared.mock.calls[0][0].idempotency_key).toBeUndefined();
    expect(fetchMock.mock.calls.length).toBe(beforeHandoff);
    fireEvent.click(screen.getByRole("button", { name: "Request local reply draft" }));
    await screen.findByDisplayValue("A private draft body");
    expect(fetchMock.mock.calls.some(([, init]) => (init as RequestInit | undefined)?.method === "POST")).toBe(true);
    expect(screen.getByLabelText("Task local related references")).toHaveTextContent("local_related_context_not_model_input");
    expect(screen.getByText(/sent: no · provider draft: no/)).toBeInTheDocument();
  });

  it("keeps an unknown reply admission key and blocks replacement on remount", async () => {
    const connection = {
      connection_id: "connection-1", service: "gmail_readonly", label: "Work Gmail", revision: 3, state: "active", scope_status: "verified",
      declared_scopes: ["https://www.googleapis.com/auth/gmail.readonly"], provider_scopes_verified: true, verified_setup_job_id: "setup-1",
    };
    const consent = {
      consent_id: "consent-1", connection_id: "connection-1", connection_revision: 3, goal_id: "goal-1", goal_revision: 4,
      label_ids: ["INBOX"], window_days: 7, max_messages: 1, source_read_allowed: true, source_revision: 5, model_egress_allowed: true,
      model_revision: 6, allowed_body_fields: ["subject", "plainbody", "replyintent"], expires_at: new Date(Date.now() + 60 * 60 * 1000).toISOString(), state: "active", revision: 7,
    };
    const origin = { watch_id: "watch-1", message_binding_id: "message-binding-unknown", message_revision: "sha256:" + "e".repeat(64), status: "present", private: true as const };
    const watch = {
      watch_id: "watch-1", scheduled_job_id: "scheduled-watch-1", capability_id: "gmail.scan_metadata.v1",
      connection_id: "connection-1", connection_revision: 3, mail_consent_id: "consent-1", source_consent_revision: 5,
      goal_id: "goal-1", goal_revision: 4, label_ids: ["INBOX"], cadence: { kind: "hourly", timezone: "UTC", daily_hour: null, daily_minute: null },
      binding_revision: 2, expires_at: new Date(Date.now() + 60 * 60 * 1000).toISOString(), state: "active", watch_state: "baseline_complete",
      baseline_complete: true, last_observed_at: null, last_completed_occurrence_id: null, skipped_coverage_reason: null,
      list_page_complete: true, latest_occurrence: null,
    };
    let replyAttempts = 0;
    let recoveryReads = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/api/capabilities/mail/connections")) return Promise.resolve(response({ connections: [connection] }));
      if (url.endsWith("/api/capabilities/mail/read-consents")) return Promise.resolve(response({ consents: [consent], provider_contact: false }));
      if (url.endsWith("/api/capabilities/mail/watches/watch-1")) return Promise.resolve(response({ watch }));
      if (url.endsWith("/api/capabilities/mail/messages/message-binding-unknown/read")) return Promise.resolve(response({
        source_binding_id: "message-binding-unknown", message_key: "message-opaque-1", thread_key: "thread-opaque-1", message_revision: origin.message_revision,
        subject: "Private source subject", plain_text: "Private source body", truncated: false, read_status: "read", received_at: new Date().toISOString(), fetched_at: new Date().toISOString(),
        provenance: { connection_id: "connection-1", connection_revision: 3, consent_id: "consent-1", source_consent_revision: 5, memory_status: "no_learning", egress: "local_only" }, provider_contact: true, control_job_id: "mail-control-1",
      }));
      if (url.endsWith("/api/capabilities/mail/reply-tasks")) {
        replyAttempts += 1;
        return Promise.reject(new Error("gateway closed"));
      }
      if (url.includes("/api/capabilities/mail/reply-tasks/recovery/")) {
        recoveryReads += 1;
        const key = decodeURIComponent(url.split("/recovery/")[1]);
        return Promise.resolve(response({ status: "not_found", idempotency_scope: "mail-reply-draft", idempotency_key: key, task_id: null, attempt_id: null, job_id: null, input_artifact_id: null, input_digest: null, request_digest: null, goal_id: null, goal_revision: null, memory_status: "no_learning", recovery_action: "retry_same_key" }));
      }
      throw new Error(`Unexpected Mail request: ${url} ${String(init?.method ?? "GET")}`);
    });
    const props = { taskId: "triage-task-unknown", ownerPrincipalId: "operator:single", ownerSessionId: "session-1", mailOrigin: origin, goalId: "goal-1", goalRevision: 4 } as const;
    const view = render(<MailPanel {...props} />);
    fireEvent.click(await screen.findByRole("checkbox", { name: /selected message may be read/i }));
    fireEvent.click(screen.getByRole("button", { name: "Read selected private message" }));
    await screen.findByText("Private source body");
    fireEvent.change(screen.getByLabelText("Reply intent"), { target: { value: "Follow up." } });
    fireEvent.click(screen.getByRole("button", { name: "Request local reply draft" }));
    await screen.findByText(/outcome is unknown/i);
    expect(replyAttempts).toBe(1);
    view.unmount();
    render(<MailPanel {...props} />);
    await screen.findByText(/original opaque request key is retained/i);
    expect(replyAttempts).toBe(1);
    const reconcile = screen.getByRole("button", { name: "Reconcile original draft" });
    fireEvent.click(reconcile);
    await waitFor(() => expect(recoveryReads).toBe(1));
    expect(replyAttempts).toBe(1);
    expect(screen.queryByRole("button", { name: "Request local reply draft" })).not.toBeInTheDocument();
  });
});
