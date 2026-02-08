import { useRef } from "react";
import { GameContainer } from "./components/layout/GameContainer";
import { PhaserGame, type IRefPhaserGame } from "./game/PhaserGame";
import { ChatPanel } from "./components/chat/ChatPanel";
import { useWebSocket } from "./hooks/useWebSocket";

export default function App() {
  const { sendMessage } = useWebSocket();
  const phaserRef = useRef<IRefPhaserGame>(null);

  return (
    <GameContainer>
      <div className="h-[45vh] min-h-[200px] pixel-border overflow-hidden">
        <PhaserGame ref={phaserRef} />
      </div>
      <ChatPanel onSend={sendMessage} />
    </GameContainer>
  );
}
