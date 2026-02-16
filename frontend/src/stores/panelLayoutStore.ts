import { create } from "zustand";
import { persist } from "zustand/middleware";

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
};

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
  };
}

interface PanelLayoutStore {
  panels: Record<string, PanelRect>;
  zStack: string[];

  setRect: (id: string, rect: Partial<PanelRect>) => void;
  bringToFront: (id: string) => void;
  resetPanel: (id: string) => void;
  getZIndex: (id: string) => number;
}

export const usePanelLayoutStore = create<PanelLayoutStore>()(
  persist(
    (set, get) => ({
      panels: defaultPanels(),
      zStack: ["chat", "quest", "settings"],

      setRect: (id, rect) =>
        set((state) => ({
          panels: {
            ...state.panels,
            [id]: { ...state.panels[id], ...rect },
          },
        })),

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

      getZIndex: (id) => {
        const idx = get().zStack.indexOf(id);
        return 50 + (idx === -1 ? 0 : idx);
      },
    }),
    {
      name: "seraph_panel_layout",
      partialize: (state) => ({
        panels: state.panels,
        zStack: state.zStack,
      }),
    },
  ),
);
