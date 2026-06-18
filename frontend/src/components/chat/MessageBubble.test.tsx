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
});
