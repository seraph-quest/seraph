export function ThinkingIndicator() {
  return (
    <div className="flex items-center gap-1 px-3 py-2 text-retro-highlight">
      <span className="text-[10px] uppercase tracking-wider">Thinking</span>
      <span className="flex gap-[2px]">
        <span className="thinking-dot text-[10px]">.</span>
        <span className="thinking-dot text-[10px]">.</span>
        <span className="thinking-dot text-[10px]">.</span>
      </span>
    </div>
  );
}
