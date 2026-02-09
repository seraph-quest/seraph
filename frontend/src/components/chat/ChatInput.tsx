import { useState, type FormEvent, type KeyboardEvent } from "react";

interface ChatInputProps {
  onSend: (message: string) => void;
  disabled: boolean;
}

export function ChatInput({ onSend, disabled }: ChatInputProps) {
  const [value, setValue] = useState("");

  const handleSubmit = (e: FormEvent) => {
    e.preventDefault();
    const trimmed = value.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed);
    setValue("");
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSubmit(e);
    }
  };

  return (
    <form onSubmit={handleSubmit} className="flex gap-2 mt-2">
      <div className="flex-1 min-w-0 pixel-border-thin">
        <input
          type="text"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={handleKeyDown}
          disabled={disabled}
          placeholder={disabled ? "Waiting..." : "Type your message..."}
          className="w-full bg-retro-bg text-retro-text font-pixel text-[11px] px-3 py-2 outline-none placeholder:text-retro-text/30 disabled:opacity-50"
        />
      </div>
      <button
        type="submit"
        disabled={disabled || !value.trim()}
        className="pixel-border-thin bg-retro-accent text-retro-border font-pixel text-[10px] px-4 py-2 hover:bg-retro-border hover:text-retro-bg transition-colors disabled:opacity-30 disabled:cursor-not-allowed uppercase tracking-wider"
      >
        Send
      </button>
    </form>
  );
}
