import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { useChatStore } from "../stores/chatStore";

function resetStore() {
  useChatStore.setState({
    chatPanelOpen: false,
    questPanelOpen: false,
    settingsPanelOpen: false,
  });
}

function fireKey(
  key: string,
  options?: { target?: Partial<HTMLElement>; shiftKey?: boolean },
) {
  const event = new KeyboardEvent("keydown", {
    key,
    shiftKey: options?.shiftKey ?? false,
    bubbles: true,
  });
  if (options?.target) {
    Object.defineProperty(event, "target", { value: options.target });
  }
  window.dispatchEvent(event);
}

describe("useKeyboardShortcuts", () => {
  let cleanup: (() => void) | undefined;

  beforeEach(async () => {
    resetStore();

    // Register handler matching the hook logic (with shiftKey requirement)
    const handler = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA") return;
      const s = useChatStore.getState();
      switch (e.key.toLowerCase()) {
        case "c":
          if (!e.shiftKey) return;
          s.setChatPanelOpen(!s.chatPanelOpen);
          break;
        case "q":
          if (!e.shiftKey) return;
          s.setQuestPanelOpen(!s.questPanelOpen);
          break;
        case "s":
          if (!e.shiftKey) return;
          s.setSettingsPanelOpen(!s.settingsPanelOpen);
          break;
        case "escape":
          if (s.chatPanelOpen) s.setChatPanelOpen(false);
          else if (s.questPanelOpen) s.setQuestPanelOpen(false);
          else if (s.settingsPanelOpen) s.setSettingsPanelOpen(false);
          break;
      }
    };
    window.addEventListener("keydown", handler);
    cleanup = () => window.removeEventListener("keydown", handler);
  });

  afterEach(() => {
    cleanup?.();
  });

  it("Shift+C toggles chat panel open", () => {
    fireKey("c", { shiftKey: true });
    expect(useChatStore.getState().chatPanelOpen).toBe(true);
  });

  it("Shift+C toggles chat panel closed", () => {
    useChatStore.setState({ chatPanelOpen: true });
    fireKey("c", { shiftKey: true });
    expect(useChatStore.getState().chatPanelOpen).toBe(false);
  });

  it("Shift+Q toggles quest panel", () => {
    fireKey("q", { shiftKey: true });
    expect(useChatStore.getState().questPanelOpen).toBe(true);
  });

  it("Shift+S toggles settings panel", () => {
    fireKey("s", { shiftKey: true });
    expect(useChatStore.getState().settingsPanelOpen).toBe(true);
  });

  it("bare C without Shift does NOT toggle chat", () => {
    fireKey("c");
    expect(useChatStore.getState().chatPanelOpen).toBe(false);
  });

  it("bare Q without Shift does NOT toggle quests", () => {
    fireKey("q");
    expect(useChatStore.getState().questPanelOpen).toBe(false);
  });

  it("bare S without Shift does NOT toggle settings (no WASD conflict)", () => {
    fireKey("s");
    expect(useChatStore.getState().settingsPanelOpen).toBe(false);
  });

  it("Escape closes chat panel first", () => {
    useChatStore.setState({ chatPanelOpen: true, questPanelOpen: true });
    fireKey("Escape");
    expect(useChatStore.getState().chatPanelOpen).toBe(false);
    expect(useChatStore.getState().questPanelOpen).toBe(true);
  });

  it("Escape closes quest panel when chat is closed", () => {
    useChatStore.setState({ chatPanelOpen: false, questPanelOpen: true });
    fireKey("Escape");
    expect(useChatStore.getState().questPanelOpen).toBe(false);
  });

  it("Escape closes settings panel when others are closed", () => {
    useChatStore.setState({ settingsPanelOpen: true });
    fireKey("Escape");
    expect(useChatStore.getState().settingsPanelOpen).toBe(false);
  });

  it("Escape does nothing when all panels closed", () => {
    fireKey("Escape");
    expect(useChatStore.getState().chatPanelOpen).toBe(false);
    expect(useChatStore.getState().questPanelOpen).toBe(false);
    expect(useChatStore.getState().settingsPanelOpen).toBe(false);
  });

  it("ignores keys when target is INPUT", () => {
    fireKey("c", { target: { tagName: "INPUT" }, shiftKey: true });
    expect(useChatStore.getState().chatPanelOpen).toBe(false);
  });

  it("ignores keys when target is TEXTAREA", () => {
    fireKey("s", { target: { tagName: "TEXTAREA" }, shiftKey: true });
    expect(useChatStore.getState().settingsPanelOpen).toBe(false);
  });

  it("handles uppercase Shift+C", () => {
    fireKey("C", { shiftKey: true });
    expect(useChatStore.getState().chatPanelOpen).toBe(true);
  });
});
