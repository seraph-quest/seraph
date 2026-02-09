import { useEffect } from "react";
import { useChatStore } from "../../stores/chatStore";

export function SessionList() {
  const sessions = useChatStore((s) => s.sessions);
  const sessionId = useChatStore((s) => s.sessionId);
  const onboardingCompleted = useChatStore((s) => s.onboardingCompleted);
  const loadSessions = useChatStore((s) => s.loadSessions);
  const switchSession = useChatStore((s) => s.switchSession);
  const newSession = useChatStore((s) => s.newSession);
  const deleteSession = useChatStore((s) => s.deleteSession);
  const restartOnboarding = useChatStore((s) => s.restartOnboarding);

  useEffect(() => {
    loadSessions();
  }, [loadSessions]);

  return (
    <div className="flex flex-col gap-1 max-h-[120px] overflow-y-auto retro-scrollbar">
      <button
        onClick={() => {
          newSession();
          loadSessions();
        }}
        className="text-[8px] text-retro-highlight hover:text-retro-border text-left px-2 py-1 uppercase tracking-wider"
      >
        + New Chat
      </button>
      {sessions.map((s) => (
        <div
          key={s.id}
          className={`flex items-center gap-1 px-2 py-1 cursor-pointer text-[8px] hover:bg-retro-accent/30 rounded-sm ${
            s.id === sessionId ? "bg-retro-accent/50 text-retro-highlight" : "text-retro-text/60"
          }`}
        >
          <button
            className="flex-1 text-left truncate"
            onClick={() => switchSession(s.id)}
          >
            {s.title}
          </button>
          <button
            onClick={(e) => {
              e.stopPropagation();
              deleteSession(s.id);
            }}
            className="text-retro-error/60 hover:text-retro-error text-[7px] px-1"
          >
            x
          </button>
        </div>
      ))}
      {onboardingCompleted === true && (
        <button
          onClick={async () => {
            await restartOnboarding();
            loadSessions();
          }}
          className="text-[7px] text-retro-text/40 hover:text-retro-highlight text-left px-2 py-1 uppercase tracking-wider"
        >
          Restart intro
        </button>
      )}
    </div>
  );
}
