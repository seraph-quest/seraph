import { useRef } from "react";
import { PhaserGame, type IRefPhaserGame } from "./game/PhaserGame";
import { ChatPanel } from "./components/chat/ChatPanel";
import { useWebSocket } from "./hooks/useWebSocket";

export default function App() {
  const { sendMessage } = useWebSocket();
  const phaserRef = useRef<IRefPhaserGame>(null);

  return (
    <>
      <div id="game-viewport" className="fixed inset-0 z-0">
        <PhaserGame ref={phaserRef} />
      </div>
      <ChatPanel onSend={sendMessage} />
      <div className="crt-overlay" />
    </>
  );
}
