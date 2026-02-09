import { useRef, useEffect } from "react";
import { PhaserGame, type IRefPhaserGame } from "./game/PhaserGame";
import { ChatPanel } from "./components/chat/ChatPanel";
import { QuestPanel } from "./components/quest/QuestPanel";
import { HudButtons } from "./components/HudButtons";
import { SettingsPanel } from "./components/SettingsPanel";
import { useWebSocket } from "./hooks/useWebSocket";
import { useChatStore } from "./stores/chatStore";
import { EventBus } from "./game/EventBus";

export default function App() {
  const { sendMessage, skipOnboarding } = useWebSocket();
  const phaserRef = useRef<IRefPhaserGame>(null);
  // Bridge Phaser EventBus → React panel toggles
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
      <div id="game-viewport" className="fixed inset-0 z-0">
        <PhaserGame ref={phaserRef} />
      </div>
      <ChatPanel onSend={sendMessage} onSkipOnboarding={skipOnboarding} />
      <QuestPanel />
      <SettingsPanel />
      <HudButtons />
      <div className="crt-overlay" />
    </>
  );
}
