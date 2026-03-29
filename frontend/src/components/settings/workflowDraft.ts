export interface WorkflowInputSpec {
  type: string;
  description: string;
  required?: boolean;
  default?: unknown;
  artifact_input?: boolean;
  artifact_types?: string[];
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
  source?: string;
  extension_id?: string | null;
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
  runtime_profile?: string;
  output_surface?: string;
  output_surface_artifact_types?: string[];
}

function inputPlaceholder(
  name: string,
  spec: WorkflowInputSpec,
  artifactBinding?: { inputName: string; artifactPath: string } | null,
): string {
  if (artifactBinding && artifactBinding.inputName === name) return artifactBinding.artifactPath;
  if (typeof spec.default === "string" && spec.default) return spec.default;
  if (typeof spec.default === "number" || typeof spec.default === "boolean") {
    return String(spec.default);
  }
  if (name === "file_path") return "notes/output.md";
  return `<${name}>`;
}

function normalizedArtifactTypes(value: string[] | undefined): string[] {
  if (!Array.isArray(value)) return [];
  const seen = new Set<string>();
  const normalized: string[] = [];
  value.forEach((item) => {
    if (typeof item !== "string") return;
    const text = item.trim();
    if (!text || seen.has(text)) return;
    seen.add(text);
    normalized.push(text);
  });
  return normalized;
}

export function inferArtifactTypes(
  artifactPath: string,
  hintedTypes?: string[],
): string[] {
  const normalizedPath = artifactPath.trim().toLowerCase();
  const extension = normalizedPath.includes(".")
    ? normalizedPath.slice(normalizedPath.lastIndexOf("."))
    : "";
  const inferred = new Set<string>(["workspace_file", "file"]);
  normalizedArtifactTypes(hintedTypes).forEach((item) => inferred.add(item));
  if (extension === ".md" || extension === ".mdx") {
    inferred.add("markdown_document");
    inferred.add("text_document");
    inferred.add("note");
    inferred.add("report");
  } else if (extension === ".txt") {
    inferred.add("text_document");
    inferred.add("note");
  } else if (extension === ".json") {
    inferred.add("json_document");
  } else if (extension === ".csv") {
    inferred.add("csv_document");
    inferred.add("table");
  } else if (extension === ".pdf") {
    inferred.add("pdf_document");
    inferred.add("report");
  } else if ([".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"].includes(extension)) {
    inferred.add("image");
  }
  return Array.from(inferred);
}

export function workflowArtifactInputs(
  workflow: WorkflowInfo,
): Array<{ inputName: string; spec: WorkflowInputSpec; legacy: boolean }> {
  const explicit = Object.entries(workflow.inputs).reduce<Array<{ inputName: string; spec: WorkflowInputSpec; legacy: boolean }>>(
    (entries, [inputName, spec]) => {
      if (spec.artifact_input === true) {
        entries.push({ inputName, spec, legacy: false });
      }
      return entries;
    },
    [],
  );
  if (explicit.length > 0) {
    return explicit;
  }
  if (workflow.inputs.file_path) {
    return [
      {
        inputName: "file_path",
        spec: {
          ...workflow.inputs.file_path,
          artifact_input: true,
          artifact_types: normalizedArtifactTypes(workflow.inputs.file_path.artifact_types).length > 0
            ? workflow.inputs.file_path.artifact_types
            : ["workspace_file", "file"],
        },
        legacy: true,
      },
    ];
  }
  return [];
}

export function workflowAcceptsArtifact(
  workflow: WorkflowInfo,
  artifactPath: string,
  producedArtifactTypes?: string[],
): boolean {
  const artifactInputs = workflowArtifactInputs(workflow);
  if (artifactInputs.length === 0) return false;
  const candidateTypes = inferArtifactTypes(artifactPath, producedArtifactTypes);
  return artifactInputs.some(({ spec }) => {
    const requiredTypes = normalizedArtifactTypes(spec.artifact_types);
    if (requiredTypes.length === 0) return true;
    return requiredTypes.some((requiredType) => candidateTypes.includes(requiredType));
  });
}

export function workflowArtifactInputName(
  workflow: WorkflowInfo,
  artifactPath: string,
  producedArtifactTypes?: string[],
): string | null {
  const artifactInputs = workflowArtifactInputs(workflow);
  if (artifactInputs.length === 0) return null;
  const candidateTypes = inferArtifactTypes(artifactPath, producedArtifactTypes);
  const match = artifactInputs.find(({ spec }) => {
    const requiredTypes = normalizedArtifactTypes(spec.artifact_types);
    if (requiredTypes.length === 0) return true;
    return requiredTypes.some((requiredType) => candidateTypes.includes(requiredType));
  });
  return match?.inputName ?? null;
}

export function buildWorkflowDraft(
  workflow: WorkflowInfo,
  artifactPath?: string,
  producedArtifactTypes?: string[],
): string {
  const artifactInputName = artifactPath
    ? workflowArtifactInputName(workflow, artifactPath, producedArtifactTypes)
    : null;
  const artifactBinding = artifactPath && artifactInputName
    ? { inputName: artifactInputName, artifactPath }
    : null;
  const inputs = Object.entries(workflow.inputs).map(([name, spec]) => {
    const value = inputPlaceholder(name, spec, artifactBinding);
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

  if (artifactBinding) {
    notes.push(`Artifact handoff: ${artifactBinding.inputName} is bound to ${artifactBinding.artifactPath}.`);
  }

  if (workflow.is_available === false) {
    const blockers = [
      workflow.missing_tools?.length
        ? `missing tools: ${workflow.missing_tools.join(", ")}`
        : "",
      workflow.missing_skills?.length
        ? `missing skills: ${workflow.missing_skills.join(", ")}`
        : "",
    ].filter(Boolean);
    if (blockers.length > 0) {
      notes.push(`Current blockers: ${blockers.join(" · ")}.`);
    }
  }

  return [header, ...notes].join("\n");
}
