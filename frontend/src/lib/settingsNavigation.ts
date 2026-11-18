import { appEventBus } from "./appEventBus";

/** Settings sections that other surfaces may deep-link into (#1172). */
export type SettingsSectionId =
  /** The Model card (#1280); today the first tab, which keeps the "artifacts" id. */
  | "model"
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

const SETTINGS_SECTION_IDS: ReadonlySet<string> = new Set<SettingsSectionId>([
  "model", "artifacts", "general", "native", "telegram", "repo-sandbox", "policies", "audit", "workflows",
  "guardian", "calendar", "mail", "github", "moltbook", "forgejo", "skills", "discover", "mcp",
]);

/** Accept a server-sent section id only when the panel knows it. */
export function settingsSectionFrom(value: unknown): SettingsSectionId | undefined {
  return typeof value === "string" && SETTINGS_SECTION_IDS.has(value) ? value as SettingsSectionId : undefined;
}

/** Something inside a section to bring into view after it opens. */
export type SettingsFocusTarget = "model-card";

export interface SettingsOpenSectionEvent {
  section: SettingsSectionId;
  focus?: SettingsFocusTarget;
}

export const SETTINGS_OPEN_SECTION_EVENT = "settings:open-section";
/** DOM id of the Model card, the target of the "model-card" focus hint. */
export const SETTINGS_MODEL_CARD_ID = "settings-model-card";

/** Open the Settings panel on one section. The panel owns the open state. */
export function openSettingsSection(section: SettingsSectionId, focus?: SettingsFocusTarget): void {
  // "model" always brings the Model card into view.
  const target = focus ?? (section === "model" ? "model-card" : undefined);
  appEventBus.emit<SettingsOpenSectionEvent>(SETTINGS_OPEN_SECTION_EVENT, target ? { section, focus: target } : { section });
}
