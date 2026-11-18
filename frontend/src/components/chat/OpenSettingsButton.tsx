import type { ChatMessage } from "../../types";
import { openSettingsSection } from "../../lib/settingsNavigation";

/**
 * "Open Settings" under a blocked-chat error (#1280): opens the section the
 * backend named, with the Model card in view. Renders nothing otherwise.
 */
export function OpenSettingsButton({ message, className }: { message: ChatMessage; className?: string }) {
  if (message.role !== "error" || !message.settingsSection) return null;
  const section = message.settingsSection;
  return (
    <button
      type="button"
      className={className}
      onClick={() => openSettingsSection(section, section === "model" || section === "artifacts" ? "model-card" : undefined)}
    >
      Open Settings
    </button>
  );
}
