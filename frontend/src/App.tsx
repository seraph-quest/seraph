import { useEffect } from "react";
import { QuestPanel } from "./components/quest/QuestPanel";
import { SettingsPanel } from "./components/SettingsPanel";
import { useWebSocket } from "./hooks/useWebSocket";
import { useKeyboardShortcuts } from "./hooks/useKeyboardShortcuts";
import { useChatStore } from "./stores/chatStore";
import { EventBus } from "./game/EventBus";
import { CockpitView } from "./components/cockpit/CockpitView";

export default function App() {
  const { sendMessage, skipOnboarding } = useWebSocket();
  useKeyboardShortcuts();
  const interfaceMode = useChatStore((s) => s.interfaceMode);
  const setInterfaceMode = useChatStore((s) => s.setInterfaceMode);
  // Bridge Phaser EventBus → React panel toggles
  useEffect(() => {
    if (interfaceMode !== "cockpit") {
      setInterfaceMode("cockpit");
    }
  }, [interfaceMode, setInterfaceMode]);

  useEffect(() => {
    const toggleChat = () => {
      const s = useChatStore.getState();
      s.setChatPanelOpen(!s.chatPanelOpen);
    };
    const toggleQuest = () => {
      const s = useChatStore.getState();
      s.setQuestPanelOpen(!s.questPanelOpen);
    };

    EventBus.on("toggle-chat", toggleChat);
    EventBus.on("toggle-quest-log", toggleQuest);

    return () => {
      EventBus.off("toggle-chat", toggleChat);
      EventBus.off("toggle-quest-log", toggleQuest);
    };
  }, []);

  return (
    <>
      <CockpitView onSend={sendMessage} onSkipOnboarding={skipOnboarding} />
      <QuestPanel />
      <SettingsPanel />
    </>
  );
}
