import { useChatStore } from "../../stores/chatStore";
import { DialogFrame } from "./DialogFrame";
import { MessageList } from "./MessageList";
import { ChatInput } from "./ChatInput";
import { ThinkingIndicator } from "./ThinkingIndicator";
import { SessionList } from "./SessionList";

interface ChatPanelProps {
  onSend: (message: string) => void;
}

export function ChatPanel({ onSend }: ChatPanelProps) {
  const isAgentBusy = useChatStore((s) => s.isAgentBusy);
  const connectionStatus = useChatStore((s) => s.connectionStatus);
  const chatPanelOpen = useChatStore((s) => s.chatPanelOpen);
  const isConnected = connectionStatus === "connected";

  if (!chatPanelOpen) return null;

  return (
    <div className="chat-overlay">
      <DialogFrame
        title="Chat Log"
        className="flex-1 min-h-0 flex flex-col relative"
      >
        <SessionList />
        <div className="border-t border-retro-border/20 my-1" />
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
