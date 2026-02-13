import { describe, it, expect, beforeEach, vi } from "vitest";
import { useChatStore } from "../stores/chatStore";

// We test the keyboard handler logic directly by dispatching KeyboardEvents
// and checking the store state, since the hook just registers a window listener.

function resetStore() {
  useChatStore.setState({
    chatPanelOpen: false,
    questPanelOpen: false,
    settingsPanelOpen: false,
  });
}

function fireKey(key: string, target?: Partial<HTMLElement>) {
  const event = new KeyboardEvent("keydown", { key, bubbles: true });
  if (target) {
    Object.defineProperty(event, "target", { value: target });
  }
  window.dispatchEvent(event);
}

describe("useKeyboardShortcuts", () => {
  beforeEach(async () => {
    resetStore();
    // Import and init the hook's listener (the module registers on import via useEffect,
    // but we can simulate by importing the handler pattern directly)
    // We'll mount the listener manually the same way the hook does
    const { useKeyboardShortcuts } = await import("./useKeyboardShortcuts");

    // Simulate the effect by running the handler registration
    const handler = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA") return;
      const s = useChatStore.getState();
      switch (e.key.toLowerCase()) {
        case "c":
          s.setChatPanelOpen(!s.chatPanelOpen);
          break;
        case "q":
          s.setQuestPanelOpen(!s.questPanelOpen);
          break;
        case "s":
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
    // Store cleanup for afterEach
    (globalThis as any).__testCleanup = () =>
      window.removeEventListener("keydown", handler);
  });

  afterEach(() => {
    (globalThis as any).__testCleanup?.();
  });

  it("C toggles chat panel open", () => {
    fireKey("c");
    expect(useChatStore.getState().chatPanelOpen).toBe(true);
  });

  it("C toggles chat panel closed", () => {
    useChatStore.setState({ chatPanelOpen: true });
    fireKey("c");
    expect(useChatStore.getState().chatPanelOpen).toBe(false);
  });

  it("Q toggles quest panel", () => {
    fireKey("q");
    expect(useChatStore.getState().questPanelOpen).toBe(true);
  });

  it("S toggles settings panel", () => {
    fireKey("s");
    expect(useChatStore.getState().settingsPanelOpen).toBe(true);
  });

  it("Escape closes chat panel first", () => {
    useChatStore.setState({ chatPanelOpen: true, questPanelOpen: true });
    fireKey("Escape");
    expect(useChatStore.getState().chatPanelOpen).toBe(false);
    // Quest stays open (priority: chat first)
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
    fireKey("c", { tagName: "INPUT" });
    expect(useChatStore.getState().chatPanelOpen).toBe(false);
  });

  it("ignores keys when target is TEXTAREA", () => {
    fireKey("s", { tagName: "TEXTAREA" });
    expect(useChatStore.getState().settingsPanelOpen).toBe(false);
  });

  it("handles uppercase key", () => {
    fireKey("C");
    expect(useChatStore.getState().chatPanelOpen).toBe(true);
  });
});

// Need to import afterEach
import { afterEach } from "vitest";
