import { useChatStore } from "../../stores/chatStore";

export function SettingsView() {
  const onboardingCompleted = useChatStore((s) => s.onboardingCompleted);
  const restartOnboarding = useChatStore((s) => s.restartOnboarding);
  const loadSessions = useChatStore((s) => s.loadSessions);
  const setSettingsOpen = useChatStore((s) => s.setSettingsOpen);

  return (
    <div className="flex flex-col gap-2 p-2 h-full">
      <button
        onClick={() => setSettingsOpen(false)}
        className="text-[8px] text-retro-highlight hover:text-retro-border text-left uppercase tracking-wider"
      >
        &lt; Back
      </button>
      <div className="border-t border-retro-border/20 my-1" />
      <div className="text-[8px] text-retro-text/40 uppercase tracking-wider px-1">
        Settings
      </div>
      {onboardingCompleted === true && (
        <button
          onClick={async () => {
            await restartOnboarding();
            loadSessions();
            setSettingsOpen(false);
          }}
          className="text-[8px] text-retro-text/60 hover:text-retro-highlight text-left px-1 py-1 uppercase tracking-wider"
        >
          Restart intro
        </button>
      )}
      <div className="flex-1" />
      <div className="text-[7px] text-retro-text/20 px-1 pb-1">
        Seraph v0.1
      </div>
    </div>
  );
}
