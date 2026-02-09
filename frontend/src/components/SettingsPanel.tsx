import { useChatStore } from "../stores/chatStore";
import { DialogFrame } from "./chat/DialogFrame";

export function SettingsPanel() {
  const settingsPanelOpen = useChatStore((s) => s.settingsPanelOpen);
  const setSettingsPanelOpen = useChatStore((s) => s.setSettingsPanelOpen);
  const onboardingCompleted = useChatStore((s) => s.onboardingCompleted);
  const restartOnboarding = useChatStore((s) => s.restartOnboarding);
  const loadSessions = useChatStore((s) => s.loadSessions);

  if (!settingsPanelOpen) return null;

  return (
    <div className="settings-overlay">
      <DialogFrame
        title="Settings"
        className="flex-1 min-h-0 flex flex-col"
        onClose={() => setSettingsPanelOpen(false)}
      >
        <div className="flex-1 min-h-0 overflow-y-auto retro-scrollbar flex flex-col gap-2 pb-1">
          <div className="px-1">
            <div className="text-[8px] uppercase tracking-wider text-retro-border font-bold mb-2">
              General
            </div>
            {onboardingCompleted === true && (
              <button
                onClick={async () => {
                  await restartOnboarding();
                  loadSessions();
                  setSettingsPanelOpen(false);
                }}
                className="text-[8px] text-retro-text/60 hover:text-retro-highlight text-left px-1 py-1 uppercase tracking-wider"
              >
                Restart intro
              </button>
            )}
          </div>
          <div className="flex-1" />
          <div className="text-[7px] text-retro-text/20 px-1 pb-1">
            Seraph v0.1
          </div>
        </div>
      </DialogFrame>
    </div>
  );
}
