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
    interfaceMode: "village",
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

  it("Shift+C dispatches cockpit composer focus without toggling legacy chat in cockpit mode", () => {
    useChatStore.setState({ interfaceMode: "cockpit" });
    const dispatchSpy = vi.spyOn(window, "dispatchEvent");

    handleGlobalKeyboardShortcut(makeEvent("c", { shiftKey: true }));

    expect(dispatchSpy).toHaveBeenCalledTimes(1);
    expect(dispatchSpy.mock.calls[0]?.[0]).toBeInstanceOf(CustomEvent);
    expect((dispatchSpy.mock.calls[0]?.[0] as CustomEvent).type).toBe("seraph-cockpit-focus-composer");
    expect(useChatStore.getState().chatPanelOpen).toBe(false);
  });

  it("Shift+1/2/3 switch cockpit layouts", () => {
    useChatStore.setState({ interfaceMode: "cockpit" });

    handleGlobalKeyboardShortcut(makeEvent("@", { shiftKey: true, code: "Digit2" }));
    expect(useCockpitLayoutStore.getState().activeLayoutId).toBe("focus");

    handleGlobalKeyboardShortcut(makeEvent("#", { shiftKey: true, code: "Digit3" }));
    expect(useCockpitLayoutStore.getState().activeLayoutId).toBe("review");

    handleGlobalKeyboardShortcut(makeEvent("!", { shiftKey: true, code: "Digit1" }));
    expect(useCockpitLayoutStore.getState().activeLayoutId).toBe("default");
  });

  it("Shift+I toggles inspector visibility in cockpit mode", () => {
    useChatStore.setState({ interfaceMode: "cockpit" });

    handleGlobalKeyboardShortcut(makeEvent("I", { shiftKey: true, code: "KeyI" }));
    expect(useCockpitLayoutStore.getState().inspectorVisible).toBe(false);

    handleGlobalKeyboardShortcut(makeEvent("I", { shiftKey: true, code: "KeyI" }));
    expect(useCockpitLayoutStore.getState().inspectorVisible).toBe(true);
  });

  it("Shift+K and Ctrl+K open the cockpit palette", () => {
    useChatStore.setState({ interfaceMode: "cockpit" });
    const dispatchSpy = vi.spyOn(window, "dispatchEvent");

    handleGlobalKeyboardShortcut(makeEvent("K", { shiftKey: true, code: "KeyK" }));
    handleGlobalKeyboardShortcut(makeEvent("k", { ctrlKey: true, code: "KeyK" }));

    expect(dispatchSpy).toHaveBeenCalledTimes(2);
    expect((dispatchSpy.mock.calls[0]?.[0] as CustomEvent).type).toBe("seraph-cockpit-open-palette");
    expect((dispatchSpy.mock.calls[1]?.[0] as CustomEvent).type).toBe("seraph-cockpit-open-palette");
  });

  it("Shift+C toggles legacy chat panel outside cockpit mode", () => {
    handleGlobalKeyboardShortcut(makeEvent("c", { shiftKey: true }));
    expect(useChatStore.getState().chatPanelOpen).toBe(true);
  });

  it("Escape closes legacy panels in priority order outside cockpit mode", () => {
    useChatStore.setState({
      chatPanelOpen: true,
      questPanelOpen: true,
      settingsPanelOpen: true,
    });

    handleGlobalKeyboardShortcut(makeEvent("Escape"));
    expect(useChatStore.getState().chatPanelOpen).toBe(false);
    expect(useChatStore.getState().questPanelOpen).toBe(true);
  });

  it("ignores shortcuts while typing in inputs", () => {
    useChatStore.setState({ interfaceMode: "cockpit" });

    handleGlobalKeyboardShortcut(makeEvent("2", { shiftKey: true, target: { tagName: "INPUT" } }));
    handleGlobalKeyboardShortcut(makeEvent("i", { shiftKey: true, target: { tagName: "TEXTAREA" } }));

    expect(useCockpitLayoutStore.getState().activeLayoutId).toBe("default");
    expect(useCockpitLayoutStore.getState().inspectorVisible).toBe(true);
  });
});
