export type CockpitLayoutId = "default" | "focus" | "review";

export type CockpitPaneId =
  | "sessions_pane"
  | "goals_pane"
  | "outputs_pane"
  | "presence_pane"
  | "approvals_pane"
  | "operator_timeline_pane"
  | "response_pane"
  | "guardian_state_pane"
  | "workflows_pane"
  | "interventions_pane"
  | "audit_pane"
  | "trace_pane"
  | "inspector_pane"
  | "conversation_pane"
  | "desktop_shell_pane"
  | "operator_surface_pane";

export interface CockpitPaneDefinition {
  id: CockpitPaneId;
  label: string;
  group: "Core" | "Guardian" | "Operator" | "Continuity";
}

export interface CockpitLayoutDefinition {
  id: CockpitLayoutId;
  label: string;
  description: string;
  centerSingleColumn: boolean;
  paneVisibility: Record<CockpitPaneId, boolean>;
}

export const DEFAULT_COCKPIT_LAYOUT_ID: CockpitLayoutId = "default";

export const COCKPIT_PANES: CockpitPaneDefinition[] = [
  { id: "sessions_pane", label: "Sessions", group: "Core" },
  { id: "goals_pane", label: "Goals", group: "Core" },
  { id: "outputs_pane", label: "Recent Outputs", group: "Core" },
  { id: "response_pane", label: "Latest Response", group: "Guardian" },
  { id: "guardian_state_pane", label: "Guardian State", group: "Guardian" },
  { id: "operator_timeline_pane", label: "Activity Ledger", group: "Guardian" },
  { id: "workflows_pane", label: "Workflow Timeline", group: "Guardian" },
  { id: "interventions_pane", label: "Interventions", group: "Guardian" },
  { id: "approvals_pane", label: "Pending Approvals", group: "Operator" },
  { id: "audit_pane", label: "Audit Surface", group: "Operator" },
  { id: "trace_pane", label: "Live Trace", group: "Operator" },
  { id: "inspector_pane", label: "Operations Inspector", group: "Operator" },
  { id: "operator_surface_pane", label: "Operator Terminal", group: "Operator" },
  { id: "presence_pane", label: "Seraph Presence", group: "Continuity" },
  { id: "conversation_pane", label: "Conversation", group: "Continuity" },
  { id: "desktop_shell_pane", label: "Desktop Shell", group: "Continuity" },
];

export const COCKPIT_PANE_IDS = COCKPIT_PANES.map((pane) => pane.id);
export const CORE_PANE_IDS: CockpitPaneId[] = [
  "response_pane",
  "operator_timeline_pane",
  "conversation_pane",
  "operator_surface_pane",
];

const ALL_VISIBLE: Record<CockpitPaneId, boolean> = Object.fromEntries(
  COCKPIT_PANE_IDS.map((id) => [id, true]),
) as Record<CockpitPaneId, boolean>;

export const COCKPIT_LAYOUTS: Record<CockpitLayoutId, CockpitLayoutDefinition> = {
  default: {
    id: "default",
    label: "Default",
    description: "Full guardian workspace with state, evidence, approvals, and conversation.",
    centerSingleColumn: false,
    paneVisibility: {
      ...ALL_VISIBLE,
      outputs_pane: false,
      trace_pane: false,
    },
  },
  focus: {
    id: "focus",
    label: "Focus",
    description: "Trim side inventory and keep only the active guardian surface plus conversation.",
    centerSingleColumn: true,
    paneVisibility: {
      ...ALL_VISIBLE,
      sessions_pane: false,
      goals_pane: false,
      outputs_pane: false,
      approvals_pane: false,
      audit_pane: false,
      trace_pane: false,
      response_pane: false,
      workflows_pane: false,
      interventions_pane: false,
      inspector_pane: false,
      operator_surface_pane: false,
      desktop_shell_pane: false,
    },
  },
  review: {
    id: "review",
    label: "Review",
    description: "Bias toward interventions, audit, and trace for post-hoc inspection.",
    centerSingleColumn: true,
    paneVisibility: {
      ...ALL_VISIBLE,
      sessions_pane: false,
      goals_pane: false,
      outputs_pane: false,
      guardian_state_pane: false,
      presence_pane: false,
      conversation_pane: false,
      desktop_shell_pane: false,
      operator_surface_pane: false,
    },
  },
};

export function getCockpitLayout(id: CockpitLayoutId): CockpitLayoutDefinition {
  return COCKPIT_LAYOUTS[id] ?? COCKPIT_LAYOUTS[DEFAULT_COCKPIT_LAYOUT_ID];
}

export function getDefaultPaneVisibility(layoutId: CockpitLayoutId): Record<CockpitPaneId, boolean> {
  return { ...getCockpitLayout(layoutId).paneVisibility };
}
