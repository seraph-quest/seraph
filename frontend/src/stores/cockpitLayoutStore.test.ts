import { beforeEach, describe, expect, it, vi } from "vitest";
import { getDefaultPaneVisibility } from "../components/cockpit/layouts";

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
      paneVisibility: getDefaultPaneVisibility("default"),
      savedPaneVisibility: {
        default: getDefaultPaneVisibility("default"),
      },
    });
  });

  it("starts on the default layout", () => {
    expect(useCockpitLayoutStore.getState().activeLayoutId).toBe("default");
    expect(useCockpitLayoutStore.getState().inspectorVisible).toBe(true);
    expect(useCockpitLayoutStore.getState().paneVisibility.sessions_pane).toBe(true);
  });

  it("switches to a saved preset and loads its pane visibility", () => {
    useCockpitLayoutStore.getState().setLayout("focus");

    expect(useCockpitLayoutStore.getState().activeLayoutId).toBe("focus");
    expect(useCockpitLayoutStore.getState().inspectorVisible).toBe(true);
    expect(useCockpitLayoutStore.getState().paneVisibility.sessions_pane).toBe(false);
    expect(useCockpitLayoutStore.getState().paneVisibility.operator_timeline_pane).toBe(true);
  });

  it("toggles inspector visibility independently from the active layout", () => {
    useCockpitLayoutStore.getState().setLayout("review");
    useCockpitLayoutStore.getState().toggleInspector();

    expect(useCockpitLayoutStore.getState().activeLayoutId).toBe("review");
    expect(useCockpitLayoutStore.getState().inspectorVisible).toBe(false);
    expect(useCockpitLayoutStore.getState().paneVisibility.inspector_pane).toBe(false);
  });

  it("persists pane visibility changes for the active layout", () => {
    useCockpitLayoutStore.getState().setPaneVisible("presence_pane", false);

    expect(useCockpitLayoutStore.getState().paneVisibility.presence_pane).toBe(false);
    expect(useCockpitLayoutStore.getState().savedPaneVisibility.default?.presence_pane).toBe(false);
  });

  it("can hide non-core panes while keeping the core window set", () => {
    useCockpitLayoutStore.getState().hideNonCorePanes();

    expect(useCockpitLayoutStore.getState().paneVisibility.response_pane).toBe(true);
    expect(useCockpitLayoutStore.getState().paneVisibility.operator_timeline_pane).toBe(true);
    expect(useCockpitLayoutStore.getState().paneVisibility.conversation_pane).toBe(true);
    expect(useCockpitLayoutStore.getState().paneVisibility.audit_pane).toBe(false);
  });

  it("resets to the default layout with inspector visible", () => {
    useCockpitLayoutStore.setState({
      activeLayoutId: "focus",
      inspectorVisible: false,
      paneVisibility: {
        ...getDefaultPaneVisibility("focus"),
        inspector_pane: false,
      },
    });

    useCockpitLayoutStore.getState().resetLayout();

    expect(useCockpitLayoutStore.getState().activeLayoutId).toBe("default");
    expect(useCockpitLayoutStore.getState().inspectorVisible).toBe(true);
    expect(useCockpitLayoutStore.getState().paneVisibility.sessions_pane).toBe(true);
  });
});
