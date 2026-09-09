import { fireEvent, render, screen, within } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { describe, expect, it, vi } from "vitest";
import type { ComponentProps } from "react";

import {
  OutcomeCockpitPanel,
  type OutcomeApprovalSummary,
  type OutcomeEvidenceSummary,
  type OutcomeGoalSummary,
  type OutcomeResultSummary,
  type OutcomeRouteSummary,
  type OutcomeWorkSummary,
} from "./OutcomeCockpitPanel";

const endpointFixture = {
  goal: {
    id: "goal-1",
    title: "Ship the guardian slice",
    status: "in_progress",
    revision: 4,
    progress: 42,
    success_criterion: { criterion_id: "criterion-1", description: "A backend receipt is read back", verifier_kind: "artifact_readback" },
  },
  workflow: {
    id: "run-1",
    workflow_name: "guardian-follow-up",
    status: "failed",
    summary: "The evidence step timed out",
    updated_at: "2026-09-09T10:00:00Z",
    step_records: [{ id: "evidence", status: "failed" }],
    retry_from_step_draft: "Retry the evidence step",
  },
  approval: {
    id: "approval-1",
    tool_name: "filesystem:workspace",
    summary: "Write the bounded report",
    risk_level: "high",
    status: "pending",
    created_at: "2026-09-09T09:58:00Z",
    lifecycle_boundaries: ["workspace/write"],
    permissions: { workspace_write: true },
  },
  runtime: {
    route: "openrouter",
    provider: "openrouter",
    model: "x-ai/grok-4.1-fast",
    upstream: "openrouter-primary",
    egress: "cloud_acknowledged",
    budget: "250 µUSD ceiling",
  },
  evidence: {
    label: "workspace/report.md",
    summary: "Artifact receipt from the filesystem adapter",
    source: "filesystem write",
    provenance: "run-1",
    handle: "artifact-1",
  },
  outcome: {
    label: "goal_loop_outcome",
    summary: "Backend receipt says the attempt failed",
    execution: "failed",
    verification: "unknown",
    usefulness: "unknown",
    learning: "no_learning",
  },
} as const;

function fixtureModel(overrides: {
  goal?: Partial<OutcomeGoalSummary>;
  work?: Partial<OutcomeWorkSummary>;
  approval?: Partial<OutcomeApprovalSummary>;
  route?: Partial<OutcomeRouteSummary>;
  evidence?: Partial<OutcomeEvidenceSummary>;
  result?: Partial<OutcomeResultSummary>;
} = {}) {
  const goal: OutcomeGoalSummary = {
    id: endpointFixture.goal.id,
    title: endpointFixture.goal.title,
    status: endpointFixture.goal.status,
    state: "active",
    revision: endpointFixture.goal.revision,
    progress: endpointFixture.goal.progress,
    criterionId: endpointFixture.goal.success_criterion.criterion_id,
    criterionSummary: endpointFixture.goal.success_criterion.description,
    ...overrides.goal,
  };
  const work: OutcomeWorkSummary = {
    id: endpointFixture.workflow.id,
    label: endpointFixture.workflow.workflow_name,
    status: endpointFixture.workflow.status,
    state: "failed",
    summary: endpointFixture.workflow.summary,
    updatedAt: endpointFixture.workflow.updated_at,
    stepLabel: endpointFixture.workflow.step_records[0].id,
    canInspect: true,
    canRetry: true,
    ...overrides.work,
  };
  const approval: OutcomeApprovalSummary = {
    id: endpointFixture.approval.id,
    toolLabel: endpointFixture.approval.tool_name,
    summary: endpointFixture.approval.summary,
    riskLevel: endpointFixture.approval.risk_level,
    state: "awaiting_approval",
    createdAt: endpointFixture.approval.created_at,
    scope: [...endpointFixture.approval.lifecycle_boundaries],
    permissions: Object.keys(endpointFixture.approval.permissions),
    authorized: true,
    ...overrides.approval,
  };
  const route: OutcomeRouteSummary = {
    state: "active",
    route: endpointFixture.runtime.route,
    provider: endpointFixture.runtime.provider,
    model: endpointFixture.runtime.model,
    upstream: endpointFixture.runtime.upstream,
    egress: endpointFixture.runtime.egress,
    budget: endpointFixture.runtime.budget,
    ...overrides.route,
  };
  const evidence: OutcomeEvidenceSummary = {
    state: "active",
    ...endpointFixture.evidence,
    ...overrides.evidence,
  };
  const result: OutcomeResultSummary = {
    state: "failed",
    source: "goal loop endpoint",
    ...endpointFixture.outcome,
    ...overrides.result,
  };
  return { goal, work, approval, route, evidence, result };
}

function renderFixture(overrides: Parameters<typeof fixtureModel>[0] = {}, props: Partial<ComponentProps<typeof OutcomeCockpitPanel>> = {}) {
  return render(<OutcomeCockpitPanel {...fixtureModel(overrides)} {...props} />);
}

describe("OutcomeCockpitPanel", () => {
  it("binds endpoint-shaped goal, workflow, approval, route, evidence, and outcome fixtures", () => {
    const onApprove = vi.fn();
    const onInspectWork = vi.fn();
    renderFixture({}, { onApprove, onInspectWork, onOpenPriorities: vi.fn() });

    expect(screen.getByTestId("outcome-goal-card")).toHaveAttribute("data-state", "active");
    expect(screen.getByText("Ship the guardian slice")).toBeInTheDocument();
    expect(within(screen.getByTestId("outcome-route-card")).getAllByText("openrouter").length).toBeGreaterThan(0);
    expect(screen.getByText("workspace/report.md")).toBeInTheDocument();
    expect(screen.getByTestId("outcome-result-card")).toHaveAttribute("data-state", "failed");

    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    fireEvent.click(screen.getByRole("button", { name: "Inspect work" }));
    expect(onApprove).toHaveBeenCalledOnce();
    expect(onInspectWork).toHaveBeenCalledOnce();
  });

  it("keeps stale authority visible while locking effect controls", () => {
    const onApprove = vi.fn();
    const onContinue = vi.fn();
    renderFixture(
      {
        work: { state: "stale", canContinue: true },
        approval: { state: "stale" },
        route: { state: "stale" },
      },
      { onApprove, onContinue },
    );

    expect(screen.getByTestId("outcome-approval-card")).toHaveAttribute("data-state", "stale");
    expect(screen.getByTestId("outcome-route-card")).toHaveAttribute("data-state", "stale");
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Continue run" })).toBeDisabled();
    expect(screen.getByText(/Last-known state is retained/)).toBeInTheDocument();
    expect(onApprove).not.toHaveBeenCalled();
    expect(onContinue).not.toHaveBeenCalled();
  });

  it("surfaces degraded route and unauthorized approval without guessing recovery", () => {
    renderFixture(
      {
        approval: { state: "unauthorized", authorized: false },
        route: { state: "degraded", detail: "gateway metadata unavailable" },
        evidence: {
          state: "partial_metadata",
          label: "No artifact receipt",
          summary: "Audit events are present, but the artifact is unavailable",
          source: "audit endpoint",
          handle: null,
        },
        result: {
          state: "partial_metadata",
          execution: "unknown",
          verification: "unknown",
        },
      },
      { onApprove: vi.fn(), onDeny: vi.fn() },
    );

    expect(screen.getByTestId("outcome-approval-card")).toHaveAttribute("data-state", "unauthorized");
    expect(screen.getByTestId("outcome-route-card")).toHaveAttribute("data-state", "degraded");
    expect(screen.getByText("gateway metadata unavailable")).toBeInTheDocument();
    expect(screen.getByTestId("outcome-evidence-card")).toHaveAttribute("data-state", "partial_metadata");
    expect(within(screen.getByTestId("outcome-result-card")).getByText("verification").parentElement).toHaveTextContent("unknown");
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Deny" })).toBeDisabled();
  });
});
