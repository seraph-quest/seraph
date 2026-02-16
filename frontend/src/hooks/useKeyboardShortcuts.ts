import { useEffect } from "react";
import { useChatStore } from "../stores/chatStore";
import { usePanelLayoutStore } from "../stores/panelLayoutStore";

export function useKeyboardShortcuts() {
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA") return;

      const s = useChatStore.getState();
      const { bringToFront } = usePanelLayoutStore.getState();

      switch (e.key.toLowerCase()) {
        case "c":
          if (!e.shiftKey) return;
          if (!s.chatPanelOpen) bringToFront("chat");
          s.setChatPanelOpen(!s.chatPanelOpen);
          break;
        case "q":
          if (!e.shiftKey) return;
          if (!s.questPanelOpen) bringToFront("quest");
          s.setQuestPanelOpen(!s.questPanelOpen);
          break;
        case "s":
          if (!e.shiftKey) return;
          if (!s.settingsPanelOpen) bringToFront("settings");
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
    return () => window.removeEventListener("keydown", handler);
  }, []);
}
