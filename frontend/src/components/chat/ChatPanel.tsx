import { useChatStore } from "../../stores/chatStore";
import { DialogFrame } from "./DialogFrame";
import { MessageList } from "./MessageList";
import { ChatInput } from "./ChatInput";
import { ThinkingIndicator } from "./ThinkingIndicator";

interface ChatPanelProps {
  onSend: (message: string) => void;
}

export function ChatPanel({ onSend }: ChatPanelProps) {
  const isAgentBusy = useChatStore((s) => s.isAgentBusy);
  const connectionStatus = useChatStore((s) => s.connectionStatus);
  const isConnected = connectionStatus === "connected";

  return (
    <div className="chat-overlay">
      <DialogFrame
        title="Chat Log"
        className="flex-1 min-h-0 flex flex-col relative"
      >
        <div className="flex-1 min-h-0 flex flex-col">
          <MessageList />
          {isAgentBusy && <ThinkingIndicator />}
        </div>
        <ChatInput onSend={onSend} disabled={!isConnected || isAgentBusy} />
        {!isConnected && (
          <div className="absolute top-2 right-4 text-[7px] text-retro-error uppercase animate-blink">
            Disconnected
          </div>
        )}
      </DialogFrame>
    </div>
  );
}
