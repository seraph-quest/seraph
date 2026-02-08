import type { ChatMessage } from "../../types";

interface MessageBubbleProps {
  message: ChatMessage;
}

const ROLE_STYLES: Record<string, string> = {
  user: "bg-retro-user/30 border-l-2 border-retro-user text-retro-text",
  agent: "bg-retro-agent/30 border-l-2 border-retro-highlight text-retro-text",
  step: "bg-retro-step/20 border-l-2 border-retro-step text-retro-text/70 text-[10px]",
  error: "bg-retro-error/20 border-l-2 border-retro-error text-red-300",
};

const ROLE_LABELS: Record<string, string> = {
  user: "You",
  agent: "Seraph",
  step: "Step",
  error: "Error",
};

export function MessageBubble({ message }: MessageBubbleProps) {
  const style = ROLE_STYLES[message.role] ?? ROLE_STYLES.agent;
  const label = ROLE_LABELS[message.role] ?? "Agent";
  const isStep = message.role === "step";

  return (
    <div className={`message-enter px-3 py-2 rounded-sm ${style}`}>
      <div className="flex items-center gap-2 mb-1">
        <span className="text-[9px] uppercase tracking-wider text-retro-border font-bold">
          {label}
          {isStep && message.stepNumber ? ` ${message.stepNumber}` : ""}
        </span>
        {isStep && message.toolUsed && (
          <span className="text-[8px] text-retro-highlight/80 uppercase">
            [{message.toolUsed}]
          </span>
        )}
      </div>
      <div className="text-[11px] leading-relaxed break-words whitespace-pre-wrap">
        {message.content}
      </div>
    </div>
  );
}
