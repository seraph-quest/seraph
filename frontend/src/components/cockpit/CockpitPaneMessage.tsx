import type { ChatMessage } from "../../types";
import { OpenSettingsButton } from "../chat/OpenSettingsButton";

/** One message in the advanced workspace's conversation pane. */
export function CockpitPaneMessage({ message, label, age }: { message: ChatMessage; label: string; age: string }) {
  return (
    <div className={`cockpit-message cockpit-message--${message.role}`}>
      <div className="cockpit-row-header">
        <span className="cockpit-role">{label}</span>
        <span className="cockpit-row-age">{age}</span>
      </div>
      <div className="cockpit-message-body">{message.content}</div>
      <OpenSettingsButton message={message} className="cockpit-dock-message-action" />
    </div>
  );
}
