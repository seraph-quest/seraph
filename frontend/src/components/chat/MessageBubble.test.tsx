import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { appEventBus } from "../../lib/appEventBus";
import { SETTINGS_OPEN_SECTION_EVENT } from "../../lib/settingsNavigation";

import { MessageBubble } from "./MessageBubble";

describe("MessageBubble", () => {
  it("offers Open Settings on a blocked-chat error and opens the Model card (#1280)", () => {
    const listener = vi.fn();
    appEventBus.on(SETTINGS_OPEN_SECTION_EVENT, listener);
    try {
      render(
        <MessageBubble
          message={{ id: "error-1", role: "error", content: "Chat is paused until you give your OK. Nothing was sent.",
            timestamp: Date.now(), code: "provider_policy_revoked", recoveryAction: "give_consent", settingsSection: "model" }}
        />
      );
      fireEvent.click(screen.getByRole("button", { name: "Open Settings" }));
      expect(listener).toHaveBeenCalledWith({ section: "model", focus: "model-card" });
    } finally {
      appEventBus.off(SETTINGS_OPEN_SECTION_EVENT, listener);
    }
  });

  it("shows no Open Settings button on an error without a settings section", () => {
    render(<MessageBubble message={{ id: "error-2", role: "error", content: "Agent error", timestamp: Date.now() }} />);
    expect(screen.queryByRole("button", { name: "Open Settings" })).toBeNull();
  });

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
