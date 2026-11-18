import { fireEvent, render, screen } from "@testing-library/react";
import { createRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { appEventBus } from "../../lib/appEventBus";
import { SETTINGS_OPEN_SECTION_EVENT } from "../../lib/settingsNavigation";
import type { ChatMessage } from "../../types";
import { CockpitConversationDock } from "./CockpitConversationDock";
import { CockpitPaneMessage } from "./CockpitPaneMessage";

const blocked: ChatMessage = { id: "e1", role: "error", content: "Chat is paused until you give your OK. Nothing was sent.",
  timestamp: 1, code: "provider_policy_revoked", recoveryAction: "give_consent", settingsSection: "model" };
const plain: ChatMessage = { id: "e2", role: "error", content: "Agent error", timestamp: 2 };

const listener = vi.fn();
afterEach(() => { appEventBus.off(SETTINGS_OPEN_SECTION_EVENT, listener); listener.mockReset(); });

function dock(messages: ChatMessage[]) {
  return render(<CockpitConversationDock open onToggle={() => {}} sessionTitle="t" sessions={[]} currentSessionId={null}
    onSwitchSession={() => {}} onNewSession={() => {}} messages={messages} isAgentBusy={false}
    activity={{ label: "idle", detail: "", tone: "neutral" }} roleLabel={(message) => message.role} ageLabel={() => "now"}
    composerValue="" onComposerChange={() => {}} onSubmit={() => {}} inputRef={createRef<HTMLInputElement>()}
    placeholder="" submitDisabled={false} />);
}

describe("Open Settings on blocked-chat errors (#1280)", () => {
  it("the conversation dock opens the Model card from a route-block error only", () => {
    appEventBus.on(SETTINGS_OPEN_SECTION_EVENT, listener);
    dock([blocked, plain]);
    const buttons = screen.getAllByRole("button", { name: "Open Settings" });
    expect(buttons).toHaveLength(1);
    fireEvent.click(buttons[0]);
    expect(listener).toHaveBeenCalledWith({ section: "model", focus: "model-card" });
  });

  it("the workspace conversation pane does the same", () => {
    appEventBus.on(SETTINGS_OPEN_SECTION_EVENT, listener);
    const { rerender } = render(<CockpitPaneMessage message={plain} label="error" age="now" />);
    expect(screen.queryByRole("button", { name: "Open Settings" })).toBeNull();
    rerender(<CockpitPaneMessage message={blocked} label="error" age="now" />);
    fireEvent.click(screen.getByRole("button", { name: "Open Settings" }));
    expect(listener).toHaveBeenCalledWith({ section: "model", focus: "model-card" });
  });
});
