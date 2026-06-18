import { describe, expect, it } from "vitest";

import {
  buildWorkflowDraft,
  type WorkflowInfo,
  workflowAcceptsArtifact,
} from "./workflowDraft";

const workflow: WorkflowInfo = {
  name: "web-brief-to-file",
  tool_name: "workflow_web_brief_to_file",
  description: "Search the web and save a note.",
  inputs: {
    query: { type: "string", description: "Search query", required: true },
    file_path: { type: "string", description: "Output path", required: true },
  },
  requires_tools: ["web_search", "write_file"],
  requires_skills: [],
  user_invocable: true,
  enabled: true,
  step_count: 2,
  file_path: "/tmp/web-brief.md",
  policy_modes: ["balanced", "full"],
  execution_boundaries: ["external_read", "workspace_write"],
  risk_level: "medium",
  requires_approval: false,
  approval_behavior: "never",
};

describe("buildWorkflowDraft", () => {
  it("builds a workflow draft with placeholders", () => {
    expect(buildWorkflowDraft(workflow)).toBe(
      'Run workflow "web-brief-to-file" with query="<query>", file_path="notes/output.md".\n' +
        "Search the web and save a note.\n" +
        "This workflow should run without approval in the current policy mode.",
    );
  });

  it("uses an artifact path when provided", () => {
    expect(buildWorkflowDraft(workflow, "notes/existing.md")).toContain(
      'file_path="notes/existing.md"',
    );
  });

  it("binds explicit artifact inputs instead of assuming file_path", () => {
    const artifactWorkflow: WorkflowInfo = {
      ...workflow,
      name: "summarize-artifact",
      tool_name: "workflow_summarize_artifact",
      inputs: {
        source_path: {
          type: "string",
          description: "Existing artifact",
          required: true,
          artifact_input: true,
          artifact_types: ["markdown_document", "workspace_file"],
        },
        destination_path: {
          type: "string",
          description: "Output path",
          required: true,
        },
      },
    };

    expect(buildWorkflowDraft(artifactWorkflow, "notes/existing.md")).toBe(
      'Run workflow "summarize-artifact" with source_path="notes/existing.md", destination_path="<destination_path>".\n'
      + "Search the web and save a note.\n"
      + "This workflow should run without approval in the current policy mode.\n"
      + "Artifact handoff: source_path is bound to notes/existing.md.",
    );
  });

  it("matches artifacts against declared workflow input types", () => {
    const artifactWorkflow: WorkflowInfo = {
      ...workflow,
      name: "summarize-artifact",
      tool_name: "workflow_summarize_artifact",
      inputs: {
        source_path: {
          type: "string",
          description: "Existing artifact",
          required: true,
          artifact_input: true,
          artifact_types: ["markdown_document"],
        },
      },
    };

    expect(workflowAcceptsArtifact(artifactWorkflow, "notes/existing.md")).toBe(true);
    expect(workflowAcceptsArtifact(artifactWorkflow, "images/mockup.png")).toBe(false);
  });
});
