import { useEffect } from "react";
import { useChatStore } from "../stores/chatStore";
import { useCockpitLayoutStore } from "../stores/cockpitLayoutStore";
import { usePanelLayoutStore } from "../stores/panelLayoutStore";

export function handleGlobalKeyboardShortcut(event: KeyboardEvent) {
  const tag = (event.target as HTMLElement)?.tagName;
  const code = event.code;
  if (tag === "INPUT" || tag === "TEXTAREA") return;

  const s = useChatStore.getState();
  const { bringToFront, applyCockpitLayout } = usePanelLayoutStore.getState();
  const cockpitLayout = useCockpitLayoutStore.getState();

  if ((event.metaKey || event.ctrlKey) && code === "KeyK") {
    event.preventDefault();
    window.dispatchEvent(new CustomEvent("seraph-cockpit-open-palette"));
    return;
  }
  if (event.shiftKey && code === "Digit1") {
    cockpitLayout.setLayout("default");
    applyCockpitLayout("default", cockpitLayout.inspectorVisible);
    return;
  }
  if (event.shiftKey && code === "Digit2") {
    cockpitLayout.setLayout("focus");
    applyCockpitLayout("focus", cockpitLayout.inspectorVisible);
    return;
  }
  if (event.shiftKey && code === "Digit3") {
    cockpitLayout.setLayout("review");
    applyCockpitLayout("review", cockpitLayout.inspectorVisible);
    return;
  }
  if (event.shiftKey && code === "KeyI") {
    cockpitLayout.toggleInspector();
    return;
  }
  if (event.shiftKey && code === "KeyK") {
    window.dispatchEvent(new CustomEvent("seraph-cockpit-open-palette"));
    return;
  }

  switch (event.key.toLowerCase()) {
    case "c":
      if (!event.shiftKey) return;
      window.dispatchEvent(new CustomEvent("seraph-cockpit-focus-composer"));
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
      if (s.questPanelOpen) s.setQuestPanelOpen(false);
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
