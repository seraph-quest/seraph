import { beforeEach, describe, expect, it, vi } from "vitest";

const { localStorageMock } = vi.hoisted(() => {
  let store: Record<string, string> = {};
  const mock = {
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
  Object.defineProperty(globalThis, "localStorage", { value: mock, configurable: true });
  return { localStorageMock: mock };
});

import { useCockpitLayoutStore } from "./cockpitLayoutStore";

describe("cockpitLayoutStore", () => {
  beforeEach(() => {
    localStorageMock.clear();
    vi.clearAllMocks();
    useCockpitLayoutStore.setState({
      activeLayoutId: "default",
      inspectorVisible: true,
    });
  });

  it("starts on the default layout", () => {
    expect(useCockpitLayoutStore.getState().activeLayoutId).toBe("default");
    expect(useCockpitLayoutStore.getState().inspectorVisible).toBe(true);
  });

  it("switches to a saved preset without mutating inspector visibility", () => {
    useCockpitLayoutStore.getState().setLayout("focus");

    expect(useCockpitLayoutStore.getState().activeLayoutId).toBe("focus");
    expect(useCockpitLayoutStore.getState().inspectorVisible).toBe(true);
  });

  it("toggles inspector visibility independently from the active layout", () => {
    useCockpitLayoutStore.getState().setLayout("review");
    useCockpitLayoutStore.getState().toggleInspector();

    expect(useCockpitLayoutStore.getState().activeLayoutId).toBe("review");
    expect(useCockpitLayoutStore.getState().inspectorVisible).toBe(false);
  });

  it("resets to the default layout with inspector visible", () => {
    useCockpitLayoutStore.setState({
      activeLayoutId: "focus",
      inspectorVisible: false,
    });

    useCockpitLayoutStore.getState().resetLayout();

    expect(useCockpitLayoutStore.getState().activeLayoutId).toBe("default");
    expect(useCockpitLayoutStore.getState().inspectorVisible).toBe(true);
  });
});
