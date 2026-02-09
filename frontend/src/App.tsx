import { useRef, useEffect } from "react";
import { PhaserGame, type IRefPhaserGame } from "./game/PhaserGame";
import { ChatPanel } from "./components/chat/ChatPanel";
import { QuestPanel } from "./components/quest/QuestPanel";
import { useWebSocket } from "./hooks/useWebSocket";
import { useChatStore } from "./stores/chatStore";
import { EventBus } from "./game/EventBus";

export default function App() {
  const { sendMessage } = useWebSocket();
  const phaserRef = useRef<IRefPhaserGame>(null);
  const setChatPanelOpen = useChatStore((s) => s.setChatPanelOpen);
  const setQuestPanelOpen = useChatStore((s) => s.setQuestPanelOpen);

  // Bridge Phaser EventBus → React panel toggles
  useEffect(() => {
    const openChat = () => setChatPanelOpen(true);
    const openQuest = () => setQuestPanelOpen(true);

    EventBus.on("open-chat", openChat);
    EventBus.on("open-quest-log", openQuest);

    return () => {
      EventBus.off("open-chat", openChat);
      EventBus.off("open-quest-log", openQuest);
    };
  }, [setChatPanelOpen, setQuestPanelOpen]);

  return (
    <>
      <div id="game-viewport" className="fixed inset-0 z-0">
        <PhaserGame ref={phaserRef} />
      </div>
      <ChatPanel onSend={sendMessage} />
      <QuestPanel />
      <div className="crt-overlay" />
    </>
  );
}
