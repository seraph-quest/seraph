import { useChatStore } from "../../stores/chatStore";
import { DialogFrame } from "./DialogFrame";
import { MessageList } from "./MessageList";
import { ChatInput } from "./ChatInput";
import { ThinkingIndicator } from "./ThinkingIndicator";
import { ChatSidebar } from "./ChatSidebar";

interface ChatPanelProps {
  onSend: (message: string) => void;
  onSkipOnboarding?: () => void;
}

export function ChatPanel({ onSend, onSkipOnboarding }: ChatPanelProps) {
  const isAgentBusy = useChatStore((s) => s.isAgentBusy);
  const connectionStatus = useChatStore((s) => s.connectionStatus);
  const chatPanelOpen = useChatStore((s) => s.chatPanelOpen);
  const chatMaximized = useChatStore((s) => s.chatMaximized);
  const toggleChatMaximized = useChatStore((s) => s.toggleChatMaximized);
  const setChatPanelOpen = useChatStore((s) => s.setChatPanelOpen);
  const onboardingCompleted = useChatStore((s) => s.onboardingCompleted);
  const isConnected = connectionStatus === "connected";

  if (!chatPanelOpen) return null;

  return (
    <div className={`chat-overlay${chatMaximized ? " maximized" : ""}`}>
      <DialogFrame
        title="Chat Log"
        className="flex-1 min-h-0 flex flex-row relative"
        onMaximize={toggleChatMaximized}
        maximized={chatMaximized}
        onClose={() => setChatPanelOpen(false)}
      >
        <ChatSidebar />
        <div className="flex-1 min-w-0 min-h-0 flex flex-col">
          {onboardingCompleted === false && onSkipOnboarding && (
            <button
              onClick={onSkipOnboarding}
              className="text-[7px] text-retro-text/40 hover:text-retro-highlight px-2 py-0.5 text-right uppercase tracking-wider"
            >
              Skip intro &gt;&gt;
            </button>
          )}
          <div className="flex-1 min-h-0 flex flex-col">
            <MessageList />
            {isAgentBusy && <ThinkingIndicator />}
          </div>
          <ChatInput onSend={onSend} disabled={!isConnected || isAgentBusy} />
        </div>
        {!isConnected && (
          <div className="absolute top-2 right-4 text-[7px] text-retro-error uppercase animate-blink">
            Disconnected
          </div>
        )}
      </DialogFrame>
    </div>
  );
}
