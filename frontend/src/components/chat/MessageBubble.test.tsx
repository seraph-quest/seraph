import { render, screen } from "@testing-library/react";
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

  it("uses the exact host approval label and disclosure for local execution", () => {
    render(
      <MessageBubble
        message={{
          id: "approval-local-1",
          role: "approval",
          content: "A local staged test run needs approval.",
          timestamp: Date.now(),
          approvalId: "approval-local-1",
          approvalStatus: "pending",
          requiredPermissions: ["local_host_execution", "workspace_write"],
        }}
      />,
    );

    expect(screen.getByRole("button", { name: "Approve local tests on this host" })).toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent(/filesystem, network, and host resource access/i);
    expect(screen.getByLabelText("Approval permission scope")).toHaveTextContent("local_host_execution · workspace_write");
    expect(screen.getByRole("button", { name: "Deny" })).toBeInTheDocument();
  });
});
