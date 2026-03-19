import { beforeEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  let store: Record<string, string> = {};
  const localStorageMock = {
    getItem: vi.fn((key: string) => store[key] ?? null),
    setItem: vi.fn((key: string, value: string) => {
      store[key] = value;
    }),
    removeItem: vi.fn((key: string) => {
      delete store[key];
    }),
    clear: vi.fn(() => {
      store = {};
    }),
  };
  Object.defineProperty(globalThis, "localStorage", {
    value: localStorageMock,
    configurable: true,
  });
});

import { useChatStore } from "../stores/chatStore";
import { useCockpitLayoutStore } from "../stores/cockpitLayoutStore";
import { handleGlobalKeyboardShortcut } from "./useKeyboardShortcuts";

function resetStores() {
  useChatStore.setState({
    chatPanelOpen: false,
    questPanelOpen: false,
    settingsPanelOpen: false,
  });
  useCockpitLayoutStore.setState({
    activeLayoutId: "default",
    inspectorVisible: true,
  });
}

function makeEvent(
  key: string,
  options?: { target?: Partial<HTMLElement>; shiftKey?: boolean; ctrlKey?: boolean; metaKey?: boolean; code?: string },
): KeyboardEvent {
  const event = new KeyboardEvent("keydown", {
    key,
    code: options?.code,
    shiftKey: options?.shiftKey ?? false,
    ctrlKey: options?.ctrlKey ?? false,
    metaKey: options?.metaKey ?? false,
    bubbles: true,
  });
  if (options?.target) {
    Object.defineProperty(event, "target", { value: options.target });
  }
  return event;
}

describe("handleGlobalKeyboardShortcut", () => {
  beforeEach(() => {
    resetStores();
    vi.restoreAllMocks();
  });

  it("Shift+C dispatches cockpit composer focus without toggling a legacy chat panel", () => {
    const dispatchSpy = vi.spyOn(window, "dispatchEvent");

    handleGlobalKeyboardShortcut(makeEvent("c", { shiftKey: true }));

    expect(dispatchSpy).toHaveBeenCalledTimes(1);
    expect(dispatchSpy.mock.calls[0]?.[0]).toBeInstanceOf(CustomEvent);
    expect((dispatchSpy.mock.calls[0]?.[0] as CustomEvent).type).toBe("seraph-cockpit-focus-composer");
    expect(useChatStore.getState().chatPanelOpen).toBe(false);
  });

  it("Shift+1/2/3 switch cockpit layouts", () => {
    handleGlobalKeyboardShortcut(makeEvent("@", { shiftKey: true, code: "Digit2" }));
    expect(useCockpitLayoutStore.getState().activeLayoutId).toBe("focus");

    handleGlobalKeyboardShortcut(makeEvent("#", { shiftKey: true, code: "Digit3" }));
    expect(useCockpitLayoutStore.getState().activeLayoutId).toBe("review");

    handleGlobalKeyboardShortcut(makeEvent("!", { shiftKey: true, code: "Digit1" }));
    expect(useCockpitLayoutStore.getState().activeLayoutId).toBe("default");
  });

  it("Shift+I toggles inspector visibility in cockpit mode", () => {
    handleGlobalKeyboardShortcut(makeEvent("I", { shiftKey: true, code: "KeyI" }));
    expect(useCockpitLayoutStore.getState().inspectorVisible).toBe(false);

    handleGlobalKeyboardShortcut(makeEvent("I", { shiftKey: true, code: "KeyI" }));
    expect(useCockpitLayoutStore.getState().inspectorVisible).toBe(true);
  });

  it("Shift+K and Ctrl+K open the cockpit palette", () => {
    const dispatchSpy = vi.spyOn(window, "dispatchEvent");

    handleGlobalKeyboardShortcut(makeEvent("K", { shiftKey: true, code: "KeyK" }));
    handleGlobalKeyboardShortcut(makeEvent("k", { ctrlKey: true, code: "KeyK" }));

    expect(dispatchSpy).toHaveBeenCalledTimes(2);
    expect((dispatchSpy.mock.calls[0]?.[0] as CustomEvent).type).toBe("seraph-cockpit-open-palette");
    expect((dispatchSpy.mock.calls[1]?.[0] as CustomEvent).type).toBe("seraph-cockpit-open-palette");
  });

  it("Shift+Q and Shift+S toggle cockpit overlays", () => {
    handleGlobalKeyboardShortcut(makeEvent("q", { shiftKey: true }));
    expect(useChatStore.getState().questPanelOpen).toBe(true);

    handleGlobalKeyboardShortcut(makeEvent("s", { shiftKey: true }));
    expect(useChatStore.getState().settingsPanelOpen).toBe(true);
  });

  it("Escape closes overlays in priority order", () => {
    useChatStore.setState({
      questPanelOpen: true,
      settingsPanelOpen: true,
    });

    handleGlobalKeyboardShortcut(makeEvent("Escape"));
    expect(useChatStore.getState().questPanelOpen).toBe(false);
    expect(useChatStore.getState().settingsPanelOpen).toBe(true);

    handleGlobalKeyboardShortcut(makeEvent("Escape"));
    expect(useChatStore.getState().questPanelOpen).toBe(false);
    expect(useChatStore.getState().settingsPanelOpen).toBe(false);
  });

  it("ignores shortcuts while typing in inputs", () => {
    handleGlobalKeyboardShortcut(makeEvent("2", { shiftKey: true, target: { tagName: "INPUT" } }));
    handleGlobalKeyboardShortcut(makeEvent("i", { shiftKey: true, target: { tagName: "TEXTAREA" } }));

    expect(useCockpitLayoutStore.getState().activeLayoutId).toBe("default");
    expect(useCockpitLayoutStore.getState().inspectorVisible).toBe(true);
  });
});
