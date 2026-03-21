import { create } from "zustand";
import { createJSONStorage, persist, type StateStorage } from "zustand/middleware";
import {
  COCKPIT_PANE_IDS,
  getDefaultPaneVisibility,
  type CockpitLayoutId,
  type CockpitPaneId,
} from "../components/cockpit/layouts";
import { useCockpitLayoutStore } from "./cockpitLayoutStore";

export interface PanelRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export const PANEL_MIN_SIZES: Record<string, { width: number; height: number }> = {
  chat: { width: 400, height: 200 },
  quest: { width: 240, height: 200 },
  settings: { width: 240, height: 200 },
  sessions_pane: { width: 224, height: 96 },
  goals_pane: { width: 224, height: 112 },
  outputs_pane: { width: 224, height: 96 },
  presence_pane: { width: 304, height: 224 },
  approvals_pane: { width: 224, height: 112 },
  operator_timeline_pane: { width: 304, height: 176 },
  response_pane: { width: 224, height: 112 },
  guardian_state_pane: { width: 240, height: 144 },
  workflows_pane: { width: 224, height: 112 },
  interventions_pane: { width: 224, height: 112 },
  audit_pane: { width: 224, height: 112 },
  trace_pane: { width: 224, height: 96 },
  inspector_pane: { width: 240, height: 144 },
  conversation_pane: { width: 240, height: 144 },
  desktop_shell_pane: { width: 224, height: 112 },
  operator_surface_pane: { width: 240, height: 144 },
};

const WORKSPACE_GAP = 16;
const WORKSPACE_LEFT = 16;
const WORKSPACE_TOP = 124;
const WORKSPACE_RIGHT = 16;
const WORKSPACE_BOTTOM = 140;
const PANEL_GRID_SIZE = 16;

const noopStorage: StateStorage = {
  getItem: () => null,
  setItem: () => {},
  removeItem: () => {},
};

function getSafeBrowserStorage(): StateStorage {
  const storage = globalThis.localStorage as Partial<StateStorage> | undefined;
  if (
    storage &&
    typeof storage.getItem === "function" &&
    typeof storage.setItem === "function" &&
    typeof storage.removeItem === "function"
  ) {
    return storage as StateStorage;
  }
  return noopStorage;
}

interface LayoutColumn {
  weight: number;
  panes: string[];
}

const LAYOUT_COLUMNS: Record<CockpitLayoutId, LayoutColumn[]> = {
  default: [
    { weight: 0.92, panes: ["sessions_pane", "goals_pane", "outputs_pane"] },
    { weight: 1.38, panes: ["response_pane", "guardian_state_pane", "interventions_pane"] },
    { weight: 0.98, panes: ["approvals_pane", "operator_timeline_pane", "workflows_pane"] },
    { weight: 0.94, panes: ["audit_pane", "trace_pane", "inspector_pane"] },
    { weight: 1.12, panes: ["presence_pane", "conversation_pane", "desktop_shell_pane", "operator_surface_pane"] },
  ],
  focus: [
    { weight: 1.28, panes: ["response_pane", "guardian_state_pane", "interventions_pane"] },
    { weight: 1.02, panes: ["operator_timeline_pane", "workflows_pane", "inspector_pane"] },
    { weight: 1.16, panes: ["presence_pane", "conversation_pane", "desktop_shell_pane", "operator_surface_pane"] },
  ],
  review: [
    { weight: 0.86, panes: ["sessions_pane", "approvals_pane"] },
    { weight: 1.1, panes: ["response_pane", "operator_timeline_pane", "workflows_pane", "interventions_pane"] },
    { weight: 1.08, panes: ["audit_pane", "trace_pane", "inspector_pane"] },
    { weight: 1.12, panes: ["presence_pane", "conversation_pane", "desktop_shell_pane", "operator_surface_pane"] },
  ],
};

function snap(value: number): number {
  return Math.round(value / PANEL_GRID_SIZE) * PANEL_GRID_SIZE;
}

function getWorkspaceFrame() {
  const width = typeof window !== "undefined" ? window.innerWidth : 1440;
  const height = typeof window !== "undefined" ? window.innerHeight : 900;
  return {
    x: WORKSPACE_LEFT,
    y: WORKSPACE_TOP,
    width: width - WORKSPACE_LEFT - WORKSPACE_RIGHT,
    height: height - WORKSPACE_TOP - WORKSPACE_BOTTOM,
  };
}

function distributeHeights(
  ids: string[],
  availableHeight: number,
): number[] {
  if (ids.length === 0) return [];

  const gaps = WORKSPACE_GAP * (ids.length - 1);
  const usable = Math.max(0, availableHeight - gaps);
  const mins = ids.map((id) => PANEL_MIN_SIZES[id]?.height ?? 96);
  const minTotal = mins.reduce((sum, value) => sum + value, 0);

  if (usable <= minTotal) {
    const compact = snap(usable / ids.length);
    const heights = ids.map((id) => Math.max(PANEL_MIN_SIZES[id]?.height ?? 96, compact));
    return heights;
  }

  const base = mins.slice();
  let remaining = usable - minTotal;
  const growthWeights = ids.map((id) => {
    if (id === "guardian_state_pane" || id === "inspector_pane" || id === "conversation_pane") return 2;
    if (id === "operator_timeline_pane") return 2;
    if (id === "operator_surface_pane" || id === "response_pane") return 1.5;
    if (id === "presence_pane") return 1.25;
    return 1;
  });
  const totalWeight = growthWeights.reduce((sum, value) => sum + value, 0);

  const heights = base.map((value, index) => {
    if (index === ids.length - 1) return value;
    const extra = snap((remaining * growthWeights[index]) / totalWeight);
    remaining -= extra;
    return value + extra;
  });
  heights[heights.length - 1] += remaining;

  return heights;
}

export function getPackedCockpitPanels(
  layoutId: CockpitLayoutId,
  paneVisibility: Partial<Record<CockpitPaneId, boolean>> = getDefaultPaneVisibility(layoutId),
): Record<string, PanelRect> {
  const frame = getWorkspaceFrame();
  const columns = LAYOUT_COLUMNS[layoutId]
    .map((column) => ({
      weight: column.weight,
      panes: column.panes.filter((id) => paneVisibility[id as CockpitPaneId] !== false),
    }))
    .filter((column) => column.panes.length > 0);
  const columnCount = columns.length;
  const totalGap = WORKSPACE_GAP * (columnCount - 1);
  const availableWidth = frame.width - totalGap;
  const totalWeight = columns.reduce((sum, column) => sum + column.weight, 0);
  const columnWidths: number[] = [];
  let assignedWidth = 0;
  columns.forEach((column, index) => {
    if (index === columnCount - 1) {
      const minWidth = Math.max(...column.panes.map((id) => PANEL_MIN_SIZES[id]?.width ?? 224));
      columnWidths.push(snap(Math.max(minWidth, availableWidth - assignedWidth)));
      return;
    }
    const rawWidth = (availableWidth * column.weight) / totalWeight;
    const minWidth = Math.max(...column.panes.map((id) => PANEL_MIN_SIZES[id]?.width ?? 224));
    const width = snap(Math.max(minWidth, rawWidth));
    columnWidths.push(width);
    assignedWidth += width;
  });
  const panels: Record<string, PanelRect> = {};

  columns.forEach((column, columnIndex) => {
    const x = snap(
      frame.x +
        columnWidths.slice(0, columnIndex).reduce((sum, width) => sum + width, 0) +
        WORKSPACE_GAP * columnIndex,
    );
    const heights = distributeHeights(column.panes, frame.height);
    let y = snap(frame.y);

    column.panes.forEach((id, rowIndex) => {
      const width =
        columnIndex === columnCount - 1
          ? snap(frame.x + frame.width - x)
          : columnWidths[columnIndex];
      const height =
        rowIndex === column.panes.length - 1
          ? snap(frame.y + frame.height - y)
          : heights[rowIndex];
      panels[id] = {
        x,
        y,
        width: Math.max(PANEL_MIN_SIZES[id]?.width ?? 224, width),
        height: Math.max(PANEL_MIN_SIZES[id]?.height ?? 96, height),
      };
      y = snap(y + height + WORKSPACE_GAP);
    });
  });

  return panels;
}

function defaultPanels(): Record<string, PanelRect> {
  const w = typeof window !== "undefined" ? window.innerWidth : 1280;
  const h = typeof window !== "undefined" ? window.innerHeight : 720;
  return {
    chat: {
      x: Math.round((w - 760) / 2),
      y: h - 350 - 16,
      width: 760,
      height: 350,
    },
    quest: {
      x: w - 320 - 16,
      y: h - 420 - 16,
      width: 320,
      height: 420,
    },
    settings: {
      x: w - 280 - 16,
      y: h - 340 - 16,
      width: 280,
      height: 340,
    },
    ...getPackedCockpitPanels("default", getDefaultPaneVisibility("default")),
  };
}

function visibleCockpitPaneIds(
  paneVisibility: Partial<Record<CockpitPaneId, boolean>> = getDefaultPaneVisibility("default"),
) {
  return COCKPIT_PANE_IDS.filter((paneId) => paneVisibility[paneId] !== false);
}

function defaultZStack(): string[] {
  return [
    "chat",
    "quest",
    "settings",
    "sessions_pane",
    "goals_pane",
    "outputs_pane",
    "presence_pane",
    "approvals_pane",
    "operator_timeline_pane",
    "response_pane",
    "guardian_state_pane",
    "workflows_pane",
    "interventions_pane",
    "audit_pane",
    "trace_pane",
    "inspector_pane",
    "conversation_pane",
    "desktop_shell_pane",
    "operator_surface_pane",
  ];
}

interface PanelLayoutStore {
  panels: Record<string, PanelRect>;
  cockpitLayouts: Partial<Record<CockpitLayoutId, Record<string, PanelRect>>>;
  zStack: string[];

  setRect: (id: string, rect: Partial<PanelRect>) => void;
  bringToFront: (id: string) => void;
  resetPanel: (id: string) => void;
  applyCockpitLayout: (layoutId: CockpitLayoutId, paneVisibility?: Partial<Record<CockpitPaneId, boolean>>) => void;
  saveCockpitLayout: (layoutId: CockpitLayoutId) => void;
  resetCockpitLayout: (layoutId: CockpitLayoutId, paneVisibility?: Partial<Record<CockpitPaneId, boolean>>) => void;
  syncCockpitPaneStack: (paneVisibility: Partial<Record<CockpitPaneId, boolean>>) => void;
  getZIndex: (id: string) => number;
}

export const usePanelLayoutStore = create<PanelLayoutStore>()(
  persist(
    (set, get) => ({
      panels: defaultPanels(),
      cockpitLayouts: {
        default: getPackedCockpitPanels("default", getDefaultPaneVisibility("default")),
        focus: getPackedCockpitPanels("focus", getDefaultPaneVisibility("focus")),
        review: getPackedCockpitPanels("review", getDefaultPaneVisibility("review")),
      },
      zStack: defaultZStack(),

      setRect: (id, rect) =>
        set((state) => {
          const nextPanel = { ...state.panels[id], ...rect };
          const nextState: Partial<PanelLayoutStore> = {
            panels: {
              ...state.panels,
              [id]: nextPanel,
            },
          };
          if (COCKPIT_PANE_IDS.includes(id as (typeof COCKPIT_PANE_IDS)[number])) {
            const activeLayoutId = useCockpitLayoutStore.getState().activeLayoutId;
            nextState.cockpitLayouts = {
              ...state.cockpitLayouts,
              [activeLayoutId]: {
                ...(state.cockpitLayouts[activeLayoutId] ?? getPackedCockpitPanels(
                  activeLayoutId,
                  useCockpitLayoutStore.getState().paneVisibility,
                )),
                [id]: nextPanel,
              },
            };
          }
          return nextState as PanelLayoutStore;
        }),

      bringToFront: (id) =>
        set((state) => {
          const stack = state.zStack.filter((s) => s !== id);
          stack.push(id);
          return { zStack: stack };
        }),

      resetPanel: (id) =>
        set((state) => ({
          panels: {
            ...state.panels,
            [id]: defaultPanels()[id],
          },
        })),

      applyCockpitLayout: (layoutId, paneVisibility = getDefaultPaneVisibility(layoutId)) =>
        set((state) => {
          const packed = getPackedCockpitPanels(layoutId, paneVisibility);
          const saved = state.cockpitLayouts[layoutId] ?? {};
          const visiblePaneIds = Object.keys(packed);
          const resolvedPanels: Record<string, PanelRect> = { ...packed };
          visiblePaneIds.forEach((id) => {
            if (saved[id]) {
              resolvedPanels[id] = saved[id]!;
            }
          });
          return {
            panels: {
              ...state.panels,
              ...resolvedPanels,
            },
            cockpitLayouts: {
              ...state.cockpitLayouts,
              [layoutId]: resolvedPanels,
            },
            zStack: [
              ...state.zStack.filter((id) => !COCKPIT_PANE_IDS.includes(id as (typeof COCKPIT_PANE_IDS)[number])),
              ...visibleCockpitPaneIds(paneVisibility),
            ],
          };
        }),

      saveCockpitLayout: (layoutId) =>
        set((state) => {
          const currentPanels: Record<string, PanelRect> = {};
          for (const paneId of COCKPIT_PANE_IDS) {
            if (state.panels[paneId]) {
              currentPanels[paneId] = state.panels[paneId];
            }
          }
          return {
            cockpitLayouts: {
              ...state.cockpitLayouts,
              [layoutId]: currentPanels,
            },
          };
        }),

      resetCockpitLayout: (layoutId, paneVisibility = getDefaultPaneVisibility(layoutId)) =>
        set((state) => {
          const packed = getPackedCockpitPanels(layoutId, paneVisibility);
          return {
            panels: {
              ...state.panels,
              ...packed,
            },
            cockpitLayouts: {
              ...state.cockpitLayouts,
              [layoutId]: packed,
            },
          };
        }),

      syncCockpitPaneStack: (paneVisibility) =>
        set((state) => {
          const visiblePaneSet = new Set(visibleCockpitPaneIds(paneVisibility));
          const nonCockpit = state.zStack.filter((id) => !COCKPIT_PANE_IDS.includes(id as (typeof COCKPIT_PANE_IDS)[number]));
          const visibleExisting = state.zStack.filter((id) => visiblePaneSet.has(id as CockpitPaneId));
          const missingVisible = visibleCockpitPaneIds(paneVisibility).filter((id) => !visibleExisting.includes(id));
          return {
            zStack: [...nonCockpit, ...visibleExisting, ...missingVisible],
          };
        }),

      getZIndex: (id) => {
        const idx = get().zStack.indexOf(id);
        return 50 + (idx === -1 ? 0 : idx);
      },
    }),
    {
      name: "seraph_panel_layout",
      storage: createJSONStorage(getSafeBrowserStorage),
      partialize: (state) => ({
        panels: state.panels,
        cockpitLayouts: state.cockpitLayouts,
        zStack: state.zStack,
      }),
      merge: (persisted, current) => {
        const persistedState = (persisted ?? {}) as Partial<PanelLayoutStore>;
        const mergedPanels = {
          ...current.panels,
          ...(persistedState.panels ?? {}),
        };
        const mergedZStack = [...(persistedState.zStack ?? [])];
        for (const panelId of defaultZStack()) {
          if (!mergedZStack.includes(panelId)) {
            mergedZStack.push(panelId);
          }
        }
        return {
          ...current,
          ...persistedState,
          panels: mergedPanels,
          cockpitLayouts: persistedState.cockpitLayouts ?? current.cockpitLayouts,
          zStack: mergedZStack,
        };
      },
    },
  ),
);
