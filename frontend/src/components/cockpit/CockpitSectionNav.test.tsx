import { fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { describe, expect, it, vi } from "vitest";

import { CockpitSectionNav } from "./CockpitSectionNav";

describe("CockpitSectionNav", () => {
  it("renders the fixed accessible desktop order and selection", () => {
    const onSelect = vi.fn();
    render(<CockpitSectionNav activeSection="inbox" onSelect={onSelect} />);
    expect(screen.getByTestId("cockpit-section-nav").textContent).toContain("HomeInboxWorkGoalsLibraryConnections");
    expect(screen.getByTestId("cockpit-section-inbox")).toHaveAttribute("aria-current", "page");
    fireEvent.click(screen.getByTestId("cockpit-section-work"));
    expect(onSelect).toHaveBeenCalledWith("work");
  });

  it("exposes Library and Connections through the keyboard reachable More menu", () => {
    const onSelect = vi.fn();
    render(<CockpitSectionNav activeSection="home" onSelect={onSelect} />);
    fireEvent.click(screen.getByRole("button", { name: "More" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "Connections" }));
    expect(onSelect).toHaveBeenCalledWith("connections");
  });
});
