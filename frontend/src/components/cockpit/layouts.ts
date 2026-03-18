export type CockpitLayoutId = "default" | "focus" | "review";

export type CockpitSectionId =
  | "rail"
  | "guardianState"
  | "interventions"
  | "audit"
  | "trace"
  | "inspector"
  | "conversation";

export interface CockpitLayoutDefinition {
  id: CockpitLayoutId;
  label: string;
  description: string;
  centerSingleColumn: boolean;
  sections: Record<CockpitSectionId, boolean>;
}

export const DEFAULT_COCKPIT_LAYOUT_ID: CockpitLayoutId = "default";

export const COCKPIT_LAYOUTS: Record<CockpitLayoutId, CockpitLayoutDefinition> = {
  default: {
    id: "default",
    label: "Default",
    description: "Full guardian cockpit with state, evidence, approvals, and conversation.",
    centerSingleColumn: false,
    sections: {
      rail: true,
      guardianState: true,
      interventions: true,
      audit: true,
      trace: true,
      inspector: true,
      conversation: true,
    },
  },
  focus: {
    id: "focus",
    label: "Focus",
    description: "Trim side inventory and keep only the active guardian surface plus conversation.",
    centerSingleColumn: true,
    sections: {
      rail: false,
      guardianState: true,
      interventions: true,
      audit: false,
      trace: false,
      inspector: true,
      conversation: true,
    },
  },
  review: {
    id: "review",
    label: "Review",
    description: "Bias toward interventions, audit, and trace for post-hoc inspection.",
    centerSingleColumn: true,
    sections: {
      rail: true,
      guardianState: false,
      interventions: true,
      audit: true,
      trace: true,
      inspector: true,
      conversation: true,
    },
  },
};

export function getCockpitLayout(id: CockpitLayoutId): CockpitLayoutDefinition {
  return COCKPIT_LAYOUTS[id] ?? COCKPIT_LAYOUTS[DEFAULT_COCKPIT_LAYOUT_ID];
}
