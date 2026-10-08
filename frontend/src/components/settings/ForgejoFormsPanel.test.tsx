import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { ForgejoFormsPanel } from "./ForgejoFormsPanel";

const connection = { configured: true, revision: 7, form_profiles_revision: 3,
  reviewed_form_profile_ids: [], state: "active", available: true, provider_login: "fixture",
  provider_user_id: 1, read_consent_expires_at: new Date(Date.now() + 600000).toISOString() };
const props = { connection, goalId: "goal", goalRevision: 1, ownerPrincipalId: "owner",
  ownerSessionId: "root", onConnection: vi.fn() };
afterEach(() => { vi.unstubAllGlobals(); sessionStorage.clear(); vi.clearAllMocks(); });

it("requires separate unchecked activation and sends sorted profiles with both revision fences", async () => {
  const fetch = vi.fn((_url: unknown, _init?: RequestInit) => Promise.resolve(new Response(JSON.stringify(connection), { status: 200 })));
  vi.stubGlobal("fetch", fetch);
  render(<ForgejoFormsPanel {...props} />);
  fireEvent.click(screen.getByLabelText("forgejo.issue-create.v1"));
  fireEvent.click(screen.getByLabelText("forgejo.issue-comment.v1"));
  expect(screen.getByRole("button", { name: "Save reviewed form availability" })).toBeDisabled();
  expect(fetch).not.toHaveBeenCalled();
  fireEvent.click(screen.getByLabelText("Acknowledge reviewed Forgejo form profiles"));
  fireEvent.click(screen.getByLabelText("forgejo.issue-create.v1"));
  expect(screen.getByLabelText("Acknowledge reviewed Forgejo form profiles")).not.toBeChecked();
  fireEvent.click(screen.getByLabelText("forgejo.issue-create.v1"));
  fireEvent.click(screen.getByLabelText("Acknowledge reviewed Forgejo form profiles"));
  fireEvent.click(screen.getByRole("button", { name: "Save reviewed form availability" }));
  await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
  expect(JSON.parse(String(fetch.mock.calls[0][1]?.body))).toEqual({ expected_revision: 7,
    expected_form_profiles_revision: 3, profile_ids: ["forgejo.issue-comment.v1", "forgejo.issue-create.v1"], profile_ack: true });
  expect(fetch.mock.calls[0][1]?.method).toBe("PUT");
});

it("reloads a lost-response Unknown through GET only and leaves missing exact IDs unresolved", async () => {
  const id = "forgejo:" + "a".repeat(40);
  sessionStorage.setItem("seraph.forgejo.forms.owner.root.job", id);
  const fetch = vi.fn((_url: unknown, _init?: RequestInit) => Promise.resolve(new Response(JSON.stringify({ job_id: id, revision: 4,
    status: "unknown_external_effect", deadline_at: "2026-10-08T18:00:00Z", attempt_count: 1,
    owner: { principal_id: "owner" }, operator_session_id: "root", lease: { fencing_token: 1 },
    declared_authority: { operation: "form-submit" }, approval: null, forgejo: { phase: "unknown" } }), { status: 200 })));
  vi.stubGlobal("fetch", fetch);
  render(<ForgejoFormsPanel {...props} />);
  await screen.findByText(/No trustworthy exact ID was retained/);
  expect(screen.getByRole("button", { name: "Run exact form job once" })).toBeDisabled();
  expect(screen.queryByRole("button", { name: "Prepare exact-ID GET-only recovery" })).toBeNull();
  expect(fetch.mock.calls).toHaveLength(1);
  expect(fetch.mock.calls[0][1]?.method).toBeUndefined();
  expect(sessionStorage.getItem("seraph.forgejo.forms.owner.root.job")).toBe(id);
});

it("requires the saved private literal preview before exact effect acknowledgement", async () => {
  const id = "forgejo:" + "b".repeat(40), preview = "forgejo:" + "c".repeat(40);
  sessionStorage.setItem("seraph.forgejo.forms.owner.root.job", id);
  let finish: (value: Response) => void = () => undefined;
  const pending = { job_id: id, revision: 4, status: "accepted", deadline_at: "2026-10-08T18:00:00Z",
    attempt_count: 0, owner: { principal_id: "owner" }, operator_session_id: "root", lease: { fencing_token: 0 },
    declared_authority: { operation: "form-submit" }, approval: { id: "exact-approval", status: "pending" },
    forgejo: { phase: "awaiting_exact_approval", approval_scope: { private_preview_job_id: preview } } };
  const fetch = vi.fn((url: unknown, init?: RequestInit) => {
    if (String(url).endsWith("/output")) return new Promise<Response>(resolve => { finish = resolve; });
    return Promise.resolve(new Response(JSON.stringify(init?.method === "POST"
      ? { ...pending, approval: { ...pending.approval, status: "approved" } } : pending), { status: 200 }));
  });
  vi.stubGlobal("fetch", fetch);
  render(<ForgejoFormsPanel {...props} />);
  await screen.findByLabelText("Acknowledge exact Forgejo form effect");
  expect(screen.getByLabelText("Acknowledge exact Forgejo form effect")).toBeDisabled();
  expect(screen.getByRole("button", { name: "Approve saved exact form once" })).toBeDisabled();
  finish(new Response(JSON.stringify({ no_learning: true, target: { profile: "forgejo.issue-comment.v1",
    provider_user_id: 7, repository_id: 3, issue_id: 4, issue_index: 1, owner: "fixture", repository: "owned",
    title: "", content: "Protected exact body", encoded_body_digest: "a".repeat(64), page_digest: "b".repeat(64) } }), { status: 200 }));
  await screen.findByText("Protected exact body");
  expect(screen.getByLabelText("Acknowledge exact Forgejo form effect")).not.toBeChecked();
  fireEvent.click(screen.getByLabelText("Acknowledge exact Forgejo form effect"));
  fireEvent.click(screen.getByRole("button", { name: "Approve saved exact form once" }));
  await waitFor(() => expect(fetch.mock.calls.some(([, init]) => init?.method === "POST")).toBe(true));
  const approval = fetch.mock.calls.find(([, init]) => init?.method === "POST")!;
  expect(JSON.parse(String(approval[1]?.body))).toEqual({ approval_id: "exact-approval", decision: "approved", exact_ack: true });
  expect(JSON.stringify(sessionStorage)).not.toContain("Protected exact body");
});
