import { create } from "zustand";
import { createJSONStorage, persist, type StateStorage } from "zustand/middleware";

import {
  COCKPIT_PANE_IDS,
  COCKPIT_LAYOUT_IDS,
  CORE_PANE_IDS,
  DEFAULT_COCKPIT_LAYOUT_ID,
  getDefaultPaneVisibility,
  type CockpitLayoutId,
  type CockpitPaneId,
} from "../components/cockpit/layouts";

interface CockpitLayoutStore {
  activeLayoutId: CockpitLayoutId;
  inspectorVisible: boolean;
  paneVisibility: Record<CockpitPaneId, boolean>;
  savedPaneVisibility: Partial<Record<CockpitLayoutId, Record<CockpitPaneId, boolean>>>;
  setLayout: (layoutId: CockpitLayoutId) => void;
  toggleInspector: () => void;
  setPaneVisible: (paneId: CockpitPaneId, visible: boolean) => void;
  togglePaneVisible: (paneId: CockpitPaneId) => void;
  savePaneVisibility: (layoutId?: CockpitLayoutId) => void;
  resetPaneVisibility: (layoutId?: CockpitLayoutId) => void;
  showAllPanes: () => void;
  hideNonCorePanes: () => void;
  resetLayout: () => void;
}

interface PersistedCockpitLayoutStoreState {
  activeLayoutId?: CockpitLayoutId;
  inspectorVisible?: boolean;
  paneVisibility?: Partial<Record<CockpitPaneId, boolean>>;
  savedPaneVisibility?: Partial<Record<CockpitLayoutId, Partial<Record<CockpitPaneId, boolean>>>>;
}

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

function syncInspectorVisibility(visibility: Record<CockpitPaneId, boolean>) {
  return visibility.inspector_pane !== false;
}

function withSavedVisibility(
  savedPaneVisibility: Partial<Record<CockpitLayoutId, Record<CockpitPaneId, boolean>>>,
  layoutId: CockpitLayoutId,
  paneVisibility: Record<CockpitPaneId, boolean>,
) {
  return {
    ...savedPaneVisibility,
    [layoutId]: { ...paneVisibility },
  };
}

function isCockpitLayoutId(value: unknown): value is CockpitLayoutId {
  return value === "default" || value === "focus" || value === "review";
}

function normalizePaneVisibility(
  paneVisibility: Partial<Record<CockpitPaneId, boolean>> | undefined,
  layoutId: CockpitLayoutId,
  inspectorVisible?: boolean,
): Record<CockpitPaneId, boolean> {
  const nextVisibility = {
    ...getDefaultPaneVisibility(layoutId),
    ...(paneVisibility ?? {}),
  };
  if (inspectorVisible === false && paneVisibility?.inspector_pane === undefined) {
    nextVisibility.inspector_pane = false;
  }
  return nextVisibility;
}

function normalizeSavedPaneVisibility(
  savedPaneVisibility: PersistedCockpitLayoutStoreState["savedPaneVisibility"],
  activeLayoutId: CockpitLayoutId,
  paneVisibility: Record<CockpitPaneId, boolean>,
): Partial<Record<CockpitLayoutId, Record<CockpitPaneId, boolean>>> {
  const normalized = Object.fromEntries(
    COCKPIT_LAYOUT_IDS.map((layoutId) => [
      layoutId,
      normalizePaneVisibility(savedPaneVisibility?.[layoutId], layoutId),
    ]),
  ) as Partial<Record<CockpitLayoutId, Record<CockpitPaneId, boolean>>>;
  normalized[activeLayoutId] = { ...paneVisibility };
  return normalized;
}

export const useCockpitLayoutStore = create<CockpitLayoutStore>()(
  persist(
    (set) => ({
      activeLayoutId: DEFAULT_COCKPIT_LAYOUT_ID,
      inspectorVisible: true,
      paneVisibility: getDefaultPaneVisibility(DEFAULT_COCKPIT_LAYOUT_ID),
      savedPaneVisibility: {
        [DEFAULT_COCKPIT_LAYOUT_ID]: getDefaultPaneVisibility(DEFAULT_COCKPIT_LAYOUT_ID),
      },
      setLayout: (layoutId) =>
        set((state) => {
          const paneVisibility =
            state.savedPaneVisibility[layoutId] ?? getDefaultPaneVisibility(layoutId);
          return {
            activeLayoutId: layoutId,
            paneVisibility,
            inspectorVisible: syncInspectorVisibility(paneVisibility),
          };
        }),
      toggleInspector: () =>
        set((state) => {
          const paneVisibility = {
            ...state.paneVisibility,
            inspector_pane: !state.paneVisibility.inspector_pane,
          };
          return {
            paneVisibility,
            inspectorVisible: syncInspectorVisibility(paneVisibility),
            savedPaneVisibility: withSavedVisibility(
              state.savedPaneVisibility,
              state.activeLayoutId,
              paneVisibility,
            ),
          };
        }),
      setPaneVisible: (paneId, visible) =>
        set((state) => {
          const paneVisibility = {
            ...state.paneVisibility,
            [paneId]: visible,
          };
          return {
            paneVisibility,
            inspectorVisible: syncInspectorVisibility(paneVisibility),
            savedPaneVisibility: withSavedVisibility(
              state.savedPaneVisibility,
              state.activeLayoutId,
              paneVisibility,
            ),
          };
        }),
      togglePaneVisible: (paneId) =>
        set((state) => {
          const paneVisibility = {
            ...state.paneVisibility,
            [paneId]: !state.paneVisibility[paneId],
          };
          return {
            paneVisibility,
            inspectorVisible: syncInspectorVisibility(paneVisibility),
            savedPaneVisibility: withSavedVisibility(
              state.savedPaneVisibility,
              state.activeLayoutId,
              paneVisibility,
            ),
          };
        }),
      savePaneVisibility: (layoutId) =>
        set((state) => {
          const targetLayoutId = layoutId ?? state.activeLayoutId;
          return {
            savedPaneVisibility: withSavedVisibility(
              state.savedPaneVisibility,
              targetLayoutId,
              state.paneVisibility,
            ),
          };
        }),
      resetPaneVisibility: (layoutId) =>
        set((state) => {
          const targetLayoutId = layoutId ?? state.activeLayoutId;
          const paneVisibility = getDefaultPaneVisibility(targetLayoutId);
          return {
            paneVisibility,
            inspectorVisible: syncInspectorVisibility(paneVisibility),
            savedPaneVisibility: withSavedVisibility(
              state.savedPaneVisibility,
              targetLayoutId,
              paneVisibility,
            ),
          };
        }),
      showAllPanes: () =>
        set((state) => {
          const paneVisibility = Object.fromEntries(
            COCKPIT_PANE_IDS.map((paneId) => [paneId, true]),
          ) as Record<CockpitPaneId, boolean>;
          return {
            paneVisibility,
            inspectorVisible: true,
            savedPaneVisibility: withSavedVisibility(
              state.savedPaneVisibility,
              state.activeLayoutId,
              paneVisibility,
            ),
          };
        }),
      hideNonCorePanes: () =>
        set((state) => {
          const paneVisibility = Object.fromEntries(
            COCKPIT_PANE_IDS.map((paneId) => [paneId, CORE_PANE_IDS.includes(paneId)]),
          ) as Record<CockpitPaneId, boolean>;
          return {
            paneVisibility,
            inspectorVisible: syncInspectorVisibility(paneVisibility),
            savedPaneVisibility: withSavedVisibility(
              state.savedPaneVisibility,
              state.activeLayoutId,
              paneVisibility,
            ),
          };
        }),
      resetLayout: () =>
        set({
          activeLayoutId: DEFAULT_COCKPIT_LAYOUT_ID,
          paneVisibility: getDefaultPaneVisibility(DEFAULT_COCKPIT_LAYOUT_ID),
          inspectorVisible: true,
        }),
    }),
    {
      name: "seraph_cockpit_layout",
      version: 1,
      storage: createJSONStorage(getSafeBrowserStorage),
      migrate: (persistedState) => {
        const legacyState = (persistedState ?? {}) as PersistedCockpitLayoutStoreState;
        const activeLayoutId = isCockpitLayoutId(legacyState.activeLayoutId)
          ? legacyState.activeLayoutId
          : DEFAULT_COCKPIT_LAYOUT_ID;
        const paneVisibility = normalizePaneVisibility(
          legacyState.paneVisibility,
          activeLayoutId,
          legacyState.inspectorVisible,
        );
        return {
          activeLayoutId,
          inspectorVisible: syncInspectorVisibility(paneVisibility),
          paneVisibility,
          savedPaneVisibility: normalizeSavedPaneVisibility(
            legacyState.savedPaneVisibility,
            activeLayoutId,
            paneVisibility,
          ),
        };
      },
      partialize: (state) => ({
        activeLayoutId: state.activeLayoutId,
        inspectorVisible: state.inspectorVisible,
        paneVisibility: state.paneVisibility,
        savedPaneVisibility: state.savedPaneVisibility,
      }),
    },
  ),
);
