import { useEffect } from "react";
import { useChatStore } from "../stores/chatStore";

export function useKeyboardShortcuts() {
  useEffect(() => {
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
    return () => window.removeEventListener("keydown", handler);
  }, []);
}
