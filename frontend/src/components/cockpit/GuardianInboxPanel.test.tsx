import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { StrictMode } from "react";

import { GuardianInboxPanel } from "./GuardianInboxPanel";

function response(payload: unknown, ok = true, status = ok ? 200 : 409) {
  return { ok, status, json: async () => payload };
}

const item = {
  id: "inbox-1",
  revision: 3,
  state: "pending",
  source_kind: "source_packet",
  source_id: "packet-1",
  title: "Watched source changed",
  summary: "A verified local dossier is ready for your review",
  why_now: "Two material source keys changed.",
  goal_id: "goal-1",
  goal_revision: 4,
  watch_id: "watch-1",
  plan_revision: 2,
  task_id: null,
  expires_at: "2026-10-07T12:00:00Z",
  evidence_refs: [{ artifact_id: "artifact-dossier", content_sha256: "abc123" }],
  evidence_status: "verified",
  source_status: "succeeded",
  source_freshness: "current",
  verification_status: "passed",
  memory_status: "no_learning",
  policy_reason: null,
  allowed_actions: ["accept_followup", "snooze", "dismiss"],
};

describe("GuardianInboxPanel", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("loads a safe item without mutating or invoking a provider on mount", async () => {
    fetchMock.mockResolvedValueOnce(response({ items: [item], next_cursor: null, last_confirmed_at: "2026-09-30T10:00:00Z" }));
    render(<GuardianInboxPanel pollIntervalMs={0} />);

    expect(await screen.findByText("Watched source changed · pending")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ credentials: "include" });
    expect(fetchMock.mock.calls.some(([, init]) => (init as RequestInit | undefined)?.method === "POST")).toBe(false);
  });

  it("reuses the same action key when a stale action is retried", async () => {
    fetchMock
      .mockResolvedValueOnce(response({ items: [item] }))
      .mockResolvedValueOnce(response({ detail: { code: "stale_inbox_revision", recovery: "Refresh the item." } }, false, 409))
      .mockResolvedValueOnce(response({
        id: "inbox-1",
        revision: 4,
        state: "accepted",
        task_id: "task-1",
        receipt_id: "receipt-1",
      }));
    render(<GuardianInboxPanel pollIntervalMs={0} />);
    fireEvent.click(await screen.findByRole("button", { name: "Accept follow-up" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("stale_inbox_revision");
    fireEvent.click(screen.getByRole("button", { name: "Accept follow-up" }));

    await waitFor(() => expect(screen.getByText("Accept follow-up recorded.")).toBeInTheDocument());
    const firstBody = JSON.parse(String((fetchMock.mock.calls[1][1] as RequestInit).body));
    const secondBody = JSON.parse(String((fetchMock.mock.calls[2][1] as RequestInit).body));
    expect(firstBody).toEqual({
      action: "accept_followup",
      expected_revision: 3,
      idempotency_key: expect.any(String),
    });
    expect(secondBody).toEqual(firstBody);
  });

  it("keeps a failed snooze payload stable and requires refresh before changed input creates a new gesture", async () => {
    fetchMock
      .mockResolvedValueOnce(response({ items: [item] }))
      .mockResolvedValueOnce(response({ detail: { code: "stale_inbox_revision", recovery: "Refresh the inbox." } }, false, 409))
      .mockResolvedValueOnce(response({ items: [{ ...item, revision: 4 }] }))
      .mockResolvedValueOnce(response({
        id: "inbox-1",
        revision: 5,
        state: "snoozed",
        receipt_id: "receipt-snooze-1",
      }));
    render(<GuardianInboxPanel pollIntervalMs={0} />);
    const snooze = await screen.findByLabelText("Snooze Watched source changed");
    fireEvent.change(snooze, { target: { value: "2030-01-02T03:04" } });
    fireEvent.click(screen.getByRole("button", { name: "Snooze" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("stale_inbox_revision");
    expect(snooze).not.toBeDisabled();

    fireEvent.change(snooze, { target: { value: "2031-02-03T04:05" } });
    fireEvent.click(screen.getByRole("button", { name: "Snooze" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Refresh the inbox before sending a new request");
    expect(fetchMock).toHaveBeenCalledTimes(2);

    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    fireEvent.click(screen.getByRole("button", { name: "Snooze" }));
    await waitFor(() => expect(screen.getByText("Snooze recorded.")).toBeInTheDocument());

    const firstBody = JSON.parse(String((fetchMock.mock.calls[1][1] as RequestInit).body));
    const secondBody = JSON.parse(String((fetchMock.mock.calls[3][1] as RequestInit).body));
    expect(new Date(firstBody.until).toISOString()).toBe(new Date("2030-01-02T03:04").toISOString());
    expect(new Date(secondBody.until).toISOString()).toBe(new Date("2031-02-03T04:05").toISOString());
    expect(secondBody.idempotency_key).not.toBe(firstBody.idempotency_key);
    expect(secondBody.expected_revision).toBe(4);
  });

  it("retains the last-known inbox when a refresh fails", async () => {
    fetchMock
      .mockResolvedValueOnce(response({ items: [item] }))
      .mockResolvedValueOnce(response({ detail: { code: "inbox_unavailable", message: "temporary failure" } }, false, 503));
    render(<GuardianInboxPanel pollIntervalMs={0} />);
    expect(await screen.findByText("Watched source changed · pending")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(await screen.findByRole("status")).toHaveTextContent("last-known items");
    expect(screen.getByText("Watched source changed · pending")).toBeInTheDocument();
  });

  it("opens an accepted task through the existing work-board focus callback", async () => {
    const onOpenTask = vi.fn();
    const accepted = { ...item, state: "accepted", task_id: "task-1", allowed_actions: [] };
    fetchMock
      .mockResolvedValueOnce(response({ items: [accepted] }))
      .mockResolvedValueOnce(response({ ...accepted, links: { board_task: "/api/work-board/tasks/task-1" } }));
    render(<GuardianInboxPanel pollIntervalMs={0} onOpenTask={onOpenTask} />);
    fireEvent.click(await screen.findByRole("button", { name: "View evidence and task" }));
    fireEvent.click(await screen.findByRole("link", { name: /Open accepted task task-1/ }));
    expect(onOpenTask).toHaveBeenCalledWith("task-1");
  });

  it("passes backend-shaped verified artifact metadata to the authorized inspector callback", async () => {
    const onInspectArtifact = vi.fn();
    const backendRef = {
      kind: "dossier",
      artifact_type: "guardian_decision_dossier",
      file_path: "guardian/source-watches/watch-1/packets/packet-1.md",
      artifact_id: "artifact:dossier:packet-1",
      sha256: "a".repeat(64),
      status: "verified",
      verification: "cached_readback",
      last_verified_at: "2026-09-30T10:00:00Z",
    };
    fetchMock
      .mockResolvedValueOnce(response({ items: [{ ...item, evidence_refs: [] }] }))
      .mockResolvedValueOnce(response({
        ...item,
        evidence_refs: [backendRef],
        evidence_previews: [{
          artifact_id: backendRef.artifact_id,
          artifact_type: backendRef.artifact_type,
          file_path: backendRef.file_path,
          sha256: backendRef.sha256,
          owner_session_id: "operator-session-1",
          workflow_run_id: "guardian-job-1",
          text: "bounded redacted dossier preview",
          trust: "untrusted_source_evidence",
        }],
        job: {
          id: "guardian-job-1",
          status: "succeeded",
          attempt_count: 1,
          max_attempts: 1,
          readback_id: "guardian_readback:packet-1",
          verified_at: "2026-09-30T10:00:00Z",
          digest: "b".repeat(64),
          readback_status: "verified",
          readbacks: [
            {
              target_path: "guardian/source-watches/watch-1/packets/packet-1.md",
              readback_id: "guardian_readback:packet-1",
              verified_at: "2026-09-30T10:00:00Z",
              digest: "b".repeat(64),
              status: "succeeded",
            },
            {
              target_path: "guardian/source-watches/watch-1/tasks/packet-1.md",
              readback_id: "guardian_readback:task-1",
              verified_at: "2026-09-30T10:01:00Z",
              digest: "c".repeat(64),
              status: "succeeded",
            },
          ],
        },
      }));
    render(<GuardianInboxPanel pollIntervalMs={0} onInspectArtifact={onInspectArtifact} />);
    fireEvent.click(await screen.findByRole("button", { name: "View evidence and task" }));
    expect(await screen.findByText("durable job guardian-job-1 · succeeded")).toBeInTheDocument();
    expect(screen.getByText(/packets\/packet-1\.md · guardian_readback:packet-1 · succeeded/)).toBeInTheDocument();
    expect(screen.getByText(/tasks\/packet-1\.md · guardian_readback:task-1 · succeeded/)).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: /Inspect guardian evidence artifact:dossier:packet-1/ }));

    expect(onInspectArtifact).toHaveBeenCalledWith(expect.objectContaining({
      artifact_id: backendRef.artifact_id,
      file_path: backendRef.file_path,
      content_sha256: backendRef.sha256,
      status: "verified",
      verification: "cached_readback",
    }), expect.objectContaining({
      text: "bounded redacted dossier preview",
      owner_session_id: "operator-session-1",
      trust: "untrusted_source_evidence",
    }));
  });

  it("keeps verified detail through a list poll and reloads after its binding changes", async () => {
    const ownerSessionId = "operator-session-1";
    const firstRef = {
      artifact_id: "artifact-dossier-1",
      file_path: "guardian/source-watches/watch-1/packets/packet-1.md",
      sha256: "a".repeat(64),
      owner_session_id: ownerSessionId,
      status: "verified",
    };
    const secondRef = {
      ...firstRef,
      artifact_id: "artifact-dossier-2",
      sha256: "b".repeat(64),
    };
    const firstItem = { ...item, evidence_refs: [firstRef] };
    const firstDetail = {
      ...firstItem,
      evidence_previews: [{
        artifact_id: firstRef.artifact_id,
        file_path: firstRef.file_path,
        sha256: firstRef.sha256,
        owner_session_id: ownerSessionId,
        text: "first verified preview",
      }],
      job: {
        id: "job-first",
        status: "succeeded",
        attempt_count: 1,
        max_attempts: 1,
        readbacks: [{
          target_path: firstRef.file_path,
          readback_id: "readback-first",
          status: "succeeded",
          digest: firstRef.sha256,
        }],
      },
    };
    const secondItem = { ...firstItem, revision: firstItem.revision + 1, evidence_refs: [secondRef] };
    const secondDetail = {
      ...secondItem,
      evidence_previews: [{
        artifact_id: secondRef.artifact_id,
        file_path: secondRef.file_path,
        sha256: secondRef.sha256,
        owner_session_id: ownerSessionId,
        text: "second verified preview",
      }],
      job: {
        id: "job-second",
        status: "succeeded",
        attempt_count: 2,
        max_attempts: 2,
        readbacks: [{
          target_path: secondRef.file_path,
          readback_id: "readback-second",
          status: "succeeded",
          digest: secondRef.sha256,
        }],
      },
    };
    let listCalls = 0;
    let detailCalls = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/guardian/inbox?") && !url.includes("/api/guardian/inbox/inbox-1")) {
        listCalls += 1;
        const list = listCalls < 3 ? firstItem : secondItem;
        return Promise.resolve(response({ items: [list], next_cursor: null }));
      }
      if (url.endsWith("/api/guardian/inbox/inbox-1")) {
        detailCalls += 1;
        return Promise.resolve(response(detailCalls === 1 ? firstDetail : secondDetail));
      }
      return Promise.resolve(response({}));
    });

    render(<GuardianInboxPanel pollIntervalMs={25} />);
    fireEvent.click(await screen.findByRole("button", { name: "View evidence and task" }));
    expect(await screen.findByText("durable job job-first · succeeded")).toBeInTheDocument();
    expect(screen.getByText(/readback-first · succeeded/)).toBeInTheDocument();

    await waitFor(() => expect(listCalls).toBeGreaterThanOrEqual(2), { timeout: 500 });
    expect(screen.getByText("durable job job-first · succeeded")).toBeInTheDocument();
    expect(screen.getByText(/readback-first · succeeded/)).toBeInTheDocument();

    await waitFor(() => expect(screen.getByText("durable job job-second · succeeded")).toBeInTheDocument(), { timeout: 750 });
    expect(screen.queryByText("durable job job-first · succeeded")).not.toBeInTheDocument();
    expect(screen.getByText(/readback-second · succeeded/)).toBeInTheDocument();
  });

  it("degrades missing or unknown server state without exposing actions", async () => {
    fetchMock.mockResolvedValueOnce(response({
      items: [
        { ...item, id: "inbox-unknown", state: "future_state", allowed_actions: ["accept_followup", "dismiss"] },
        { ...item, id: "inbox-missing", state: undefined, allowed_actions: ["accept_followup", "dismiss"] },
      ],
    }));
    render(<GuardianInboxPanel pollIntervalMs={0} />);

    expect((await screen.findAllByText("degraded · server state is not recognized; actions are unavailable")).length).toBe(2);
    expect(screen.queryByRole("button", { name: "Accept follow-up" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Dismiss" })).not.toBeInTheDocument();
  });

  it("accepts deferred results after the StrictMode effect replay", async () => {
    const pending: Array<(value: unknown) => void> = [];
    fetchMock.mockImplementation(() => new Promise((resolve) => pending.push(resolve)));
    render(
      <StrictMode>
        <GuardianInboxPanel pollIntervalMs={0} />
      </StrictMode>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    await act(async () => {
      pending[1](response({ items: [item] }));
    });
    expect(await screen.findByText("Watched source changed · pending")).toBeInTheDocument();
    await act(async () => {
      pending[0](response({ items: [item] }));
    });
  });
});
