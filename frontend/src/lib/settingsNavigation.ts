import { appEventBus } from "./appEventBus";

/**
 * The six Settings tabs (#1280 PR 2), in tab-bar order. The service tab is
 * "Accounts" because the cockpit already has a "Connections" page.
 */
export type SettingsTabId = "model" | "general" | "accounts" | "automation" | "extensions" | "diagnostics";

export const SETTINGS_TABS: ReadonlyArray<{ id: SettingsTabId; label: string }> = [
  { id: "model", label: "Model" },
  { id: "general", label: "General" },
  { id: "accounts", label: "Accounts" },
  { id: "automation", label: "Automation" },
  { id: "extensions", label: "Extensions" },
  { id: "diagnostics", label: "Diagnostics" },
];

/**
 * Sections that other surfaces may deep-link into (#1172). Every id from the
 * old 17-tab layout stays valid and resolves to the tab that now holds it.
 */
export type SettingsSectionId =
  | SettingsTabId
  /** The old first tab; the Model tab now. */
  | "artifacts"
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
  | "mcp"
  /** Background inference jobs, which moved from the Model fold to Automation. */
  | "background-jobs";

/** Where a section id lands: its tab, and the in-tab anchor to scroll to. */
export interface SettingsSectionTarget {
  tab: SettingsTabId;
  anchor?: string;
}

export const SETTINGS_SECTION_TARGETS: Readonly<Record<SettingsSectionId, SettingsSectionTarget>> = {
  model: { tab: "model" },
  artifacts: { tab: "model" },
  general: { tab: "general", anchor: "general" },
  policies: { tab: "general", anchor: "policies" },
  accounts: { tab: "accounts" },
  telegram: { tab: "accounts", anchor: "telegram" },
  calendar: { tab: "accounts", anchor: "calendar" },
  mail: { tab: "accounts", anchor: "mail" },
  github: { tab: "accounts", anchor: "github" },
  forgejo: { tab: "accounts", anchor: "forgejo" },
  moltbook: { tab: "accounts", anchor: "moltbook" },
  native: { tab: "accounts", anchor: "native" },
  automation: { tab: "automation" },
  workflows: { tab: "automation", anchor: "workflows" },
  guardian: { tab: "automation", anchor: "guardian" },
  "repo-sandbox": { tab: "automation", anchor: "repo-sandbox" },
  "background-jobs": { tab: "automation", anchor: "background-jobs" },
  extensions: { tab: "extensions" },
  skills: { tab: "extensions", anchor: "skills" },
  discover: { tab: "extensions", anchor: "discover" },
  mcp: { tab: "extensions", anchor: "mcp" },
  diagnostics: { tab: "diagnostics" },
  audit: { tab: "diagnostics", anchor: "audit" },
};

/** Resolve any current or legacy section id to its tab and anchor. */
export function resolveSettingsSection(section: SettingsSectionId): SettingsSectionTarget {
  return SETTINGS_SECTION_TARGETS[section];
}

/** DOM id of an in-tab anchor; the panel renders it and deep links scroll to it. */
export function settingsAnchorId(anchor: string): string {
  return `settings-section-${anchor}`;
}

/** Accept a server-sent section id only when the panel knows it. */
export function settingsSectionFrom(value: unknown): SettingsSectionId | undefined {
  return typeof value === "string" && Object.prototype.hasOwnProperty.call(SETTINGS_SECTION_TARGETS, value)
    ? value as SettingsSectionId
    : undefined;
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
