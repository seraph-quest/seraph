import { useEffect } from "react";
import { useChatStore } from "../stores/chatStore";
import { useCockpitLayoutStore } from "../stores/cockpitLayoutStore";
import { usePanelLayoutStore } from "../stores/panelLayoutStore";

export function handleGlobalKeyboardShortcut(event: KeyboardEvent) {
  const tag = (event.target as HTMLElement)?.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA") return;

  const s = useChatStore.getState();
  const { bringToFront } = usePanelLayoutStore.getState();
  const cockpitLayout = useCockpitLayoutStore.getState();

  if (s.interfaceMode === "cockpit") {
    switch (event.key.toLowerCase()) {
      case "c":
        if (!event.shiftKey) return;
        window.dispatchEvent(new CustomEvent("seraph-cockpit-focus-composer"));
        return;
      case "1":
        if (!event.shiftKey) return;
        cockpitLayout.setLayout("default");
        return;
      case "2":
        if (!event.shiftKey) return;
        cockpitLayout.setLayout("focus");
        return;
      case "3":
        if (!event.shiftKey) return;
        cockpitLayout.setLayout("review");
        return;
      case "i":
        if (!event.shiftKey) return;
        cockpitLayout.toggleInspector();
        return;
      default:
        break;
    }
  }

  switch (event.key.toLowerCase()) {
    case "c":
      if (!event.shiftKey) return;
      if (!s.chatPanelOpen) bringToFront("chat");
      s.setChatPanelOpen(!s.chatPanelOpen);
      return;
    case "q":
      if (!event.shiftKey) return;
      if (!s.questPanelOpen) bringToFront("quest");
      s.setQuestPanelOpen(!s.questPanelOpen);
      return;
    case "s":
      if (!event.shiftKey) return;
      if (!s.settingsPanelOpen) bringToFront("settings");
      s.setSettingsPanelOpen(!s.settingsPanelOpen);
      return;
    case "escape":
      if (s.chatPanelOpen) s.setChatPanelOpen(false);
      else if (s.questPanelOpen) s.setQuestPanelOpen(false);
      else if (s.settingsPanelOpen) s.setSettingsPanelOpen(false);
      return;
    default:
      return;
  }
}

export function useKeyboardShortcuts() {
  useEffect(() => {
    window.addEventListener("keydown", handleGlobalKeyboardShortcut);
    return () => window.removeEventListener("keydown", handleGlobalKeyboardShortcut);
  }, []);
}
