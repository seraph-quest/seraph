import { create } from "zustand";
import { createJSONStorage, persist } from "zustand/middleware";

import { DEFAULT_COCKPIT_LAYOUT_ID, type CockpitLayoutId } from "../components/cockpit/layouts";

interface CockpitLayoutStore {
  activeLayoutId: CockpitLayoutId;
  inspectorVisible: boolean;
  setLayout: (layoutId: CockpitLayoutId) => void;
  toggleInspector: () => void;
  resetLayout: () => void;
}

export const useCockpitLayoutStore = create<CockpitLayoutStore>()(
  persist(
    (set) => ({
      activeLayoutId: DEFAULT_COCKPIT_LAYOUT_ID,
      inspectorVisible: true,
      setLayout: (layoutId) => set({ activeLayoutId: layoutId }),
      toggleInspector: () => set((state) => ({ inspectorVisible: !state.inspectorVisible })),
      resetLayout: () =>
        set({
          activeLayoutId: DEFAULT_COCKPIT_LAYOUT_ID,
          inspectorVisible: true,
        }),
    }),
    {
      name: "seraph_cockpit_layout",
      storage: createJSONStorage(() => localStorage),
      partialize: (state) => ({
        activeLayoutId: state.activeLayoutId,
        inspectorVisible: state.inspectorVisible,
      }),
    },
  ),
);
