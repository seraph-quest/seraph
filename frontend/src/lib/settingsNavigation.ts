import { appEventBus } from "./appEventBus";

/** Settings sections that other surfaces may deep-link into (#1172). */
export type SettingsSectionId =
  | "artifacts"
  | "general"
  | "native"
  | "telegram"
  | "repo-sandbox"
  | "policies"
  | "audit"
  | "workflows"
  | "guardian"
  | "calendar"
  | "mail"
  | "github"
  | "moltbook"
  | "forgejo"
  | "skills"
  | "discover"
  | "mcp";

export const SETTINGS_OPEN_SECTION_EVENT = "settings:open-section";

/** Open the Settings panel on one section. The panel owns the open state. */
export function openSettingsSection(section: SettingsSectionId): void {
  appEventBus.emit<{ section: SettingsSectionId }>(SETTINGS_OPEN_SECTION_EVENT, { section });
}
