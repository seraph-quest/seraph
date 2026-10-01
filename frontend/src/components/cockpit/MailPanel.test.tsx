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
});

