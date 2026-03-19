export interface WorkflowInputSpec {
  type: string;
  description: string;
  required?: boolean;
  default?: unknown;
}

export interface WorkflowRecommendedAction {
  type:
    | "toggle_skill"
    | "toggle_workflow"
    | "toggle_mcp_server"
    | "test_mcp_server"
    | "test_native_notification"
    | "set_tool_policy"
    | "set_mcp_policy"
    | "install_catalog_item"
    | "activate_starter_pack"
    | "draft_workflow"
    | "open_settings";
  label: string;
  name?: string;
  mode?: string;
  enabled?: boolean;
  target?: string;
}

export interface WorkflowInfo {
  name: string;
  tool_name: string;
  description: string;
  inputs: Record<string, WorkflowInputSpec>;
  requires_tools: string[];
  requires_skills: string[];
  user_invocable: boolean;
  enabled: boolean;
  step_count: number;
  file_path: string;
  policy_modes: string[];
  execution_boundaries: string[];
  risk_level: string;
  requires_approval: boolean;
  approval_behavior: string;
  is_available?: boolean;
  missing_tools?: string[];
  missing_skills?: string[];
  recommended_actions?: WorkflowRecommendedAction[];
  availability?: string;
}

function inputPlaceholder(
  name: string,
  spec: WorkflowInputSpec,
  artifactPath?: string,
): string {
  if (artifactPath && name === "file_path") return artifactPath;
  if (typeof spec.default === "string" && spec.default) return spec.default;
  if (typeof spec.default === "number" || typeof spec.default === "boolean") {
    return String(spec.default);
  }
  if (name === "file_path") return "notes/output.md";
  return `<${name}>`;
}

export function buildWorkflowDraft(workflow: WorkflowInfo, artifactPath?: string): string {
  const inputs = Object.entries(workflow.inputs).map(([name, spec]) => {
    const value = inputPlaceholder(name, spec, artifactPath);
    return `${name}=${JSON.stringify(value)}`;
  });

  const header = inputs.length
    ? `Run workflow "${workflow.name}" with ${inputs.join(", ")}.`
    : `Run workflow "${workflow.name}".`;

  const notes = [
    workflow.description,
    workflow.requires_approval
      ? `This workflow may require approval (${workflow.approval_behavior}).`
      : "This workflow should run without approval in the current policy mode.",
  ];

  return [header, ...notes].join("\n");
}
