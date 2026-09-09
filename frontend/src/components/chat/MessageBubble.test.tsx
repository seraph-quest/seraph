import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { MessageBubble } from "./MessageBubble";

describe("MessageBubble", () => {
  it("renders clarification options for clarification messages", () => {
    render(
      <MessageBubble
        message={{
          id: "clarify-1",
          role: "clarification",
          content: "Which city should I check?",
          timestamp: Date.now(),
          clarificationOptions: ["Wroclaw", "Warsaw"],
        }}
      />
    );

    expect(screen.getByText("Clarify")).toBeInTheDocument();
    expect(screen.getByText("Wroclaw")).toBeInTheDocument();
    expect(screen.getByText("Warsaw")).toBeInTheDocument();
  });

  it("sends approval decisions with browser credentials and surfaces failures", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: false,
      json: async () => ({ detail: { code: "approval_expired" } }),
    });
    vi.stubGlobal("fetch", fetchMock);

    render(
      <MessageBubble
        message={{
          id: "approval-1",
          role: "approval",
          content: "Approve the guarded action.",
          timestamp: Date.now(),
          approvalId: "approval-1",
          approvalStatus: "pending",
        }}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent("approval_expired");
    });
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining("/api/approvals/approval-1/approve"),
      expect.objectContaining({ method: "POST", credentials: "include" }),
    );
  });
});
