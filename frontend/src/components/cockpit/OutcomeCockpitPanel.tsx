import type { ReactNode } from "react";

export type OutcomeCockpitState =
  | "loading"
  | "empty"
  | "active"
  | "awaiting_approval"
  | "stale"
  | "degraded"
  | "blocked"
  | "failed"
  | "unauthorized"
  | "partial_metadata"
  | "recovered";

export interface OutcomeGoalSummary {
  id: string;
  title: string;
  status: string;
  state: OutcomeCockpitState;
  revision?: number | null;
  progress?: number | null;
  criterionId?: string | null;
  criterionSummary?: string | null;
  latestExecution?: string | null;
  latestVerification?: string | null;
  latestUsefulness?: string | null;
  latestLearning?: string | null;
}

export interface OutcomeWorkSummary {
  id: string;
  label: string;
  status: string;
  state: OutcomeCockpitState;
  summary: string;
  updatedAt?: string | null;
  stepLabel?: string | null;
  artifactLabel?: string | null;
  threadLabel?: string | null;
  nextAction?: string | null;
  recoveryHint?: string | null;
  canInspect?: boolean;
  canContinue?: boolean;
  canRetry?: boolean;
  canBranch?: boolean;
}

export interface OutcomeApprovalSummary {
  id: string;
  toolLabel: string;
  summary: string;
  riskLevel: string;
  state: OutcomeCockpitState;
  createdAt?: string | null;
  actionStatus?: string | null;
  scope?: string[];
  permissions?: string[];
  threadLabel?: string | null;
  authorized?: boolean;
  ownerPrincipal?: string | null;
  ownerSession?: string | null;
  ownerSource?: string | null;
  ownerExpiry?: string | null;
}

export interface OutcomeRouteSummary {
  state: OutcomeCockpitState;
  provider: string;
  model: string;
  route: string;
  upstream?: string | null;
  egress?: string | null;
  budget?: string | null;
  queue?: string | null;
  detail?: string | null;
}

export interface OutcomeEvidenceSummary {
  state: OutcomeCockpitState;
  label: string;
  summary: string;
  source: string;
  createdAt?: string | null;
  provenance?: string | null;
  handle?: string | null;
}

export interface OutcomeResultSummary {
  state: OutcomeCockpitState;
  label: string;
  summary: string;
  source: string;
  execution: string;
  verification: string;
  usefulness: string;
  learning: string;
  createdAt?: string | null;
}

interface OutcomeAction {
  label: string;
  onClick?: () => void;
  disabled?: boolean;
  title?: string;
}

export interface OutcomeCockpitPanelProps {
  goal: OutcomeGoalSummary | null;
  work: OutcomeWorkSummary | null;
  approval: OutcomeApprovalSummary | null;
  route: OutcomeRouteSummary;
  evidence: OutcomeEvidenceSummary;
  result: OutcomeResultSummary;
  workLoadState?: OutcomeCockpitState;
  approvalLoadState?: "loading" | "ready" | "stale";
  onOpenPriorities?: () => void;
  onLoadWork?: () => void;
  onInspectWork?: () => void;
  onOpenThread?: () => void;
  onApprove?: () => void;
  onDeny?: () => void;
  onInspectEvidence?: () => void;
  onInspectOutcome?: () => void;
  onContinue?: () => void;
  onRetry?: () => void;
  onBranch?: () => void;
}

const LOCKED_STATES: OutcomeCockpitState[] = [
  "loading",
  "stale",
  "degraded",
  "unauthorized",
  "partial_metadata",
  "blocked",
];

const RECOVERY_LOCKED_STATES: OutcomeCockpitState[] = [
  ...LOCKED_STATES,
  "awaiting_approval",
  "failed",
];

function formatState(value: OutcomeCockpitState): string {
  return value.replace(/_/g, " ").toUpperCase();
}

function stateMessage(value: OutcomeCockpitState): string {
  switch (value) {
    case "loading":
      return "Loading endpoint state…";
    case "empty":
      return "No current receipt is available in this window.";
    case "awaiting_approval":
      return "Operator decision required before this action can continue.";
    case "stale":
      return "Last-known state is retained. Refresh before changing authority.";
    case "degraded":
      return "Endpoint metadata is degraded; recovery information may be incomplete.";
    case "blocked":
      return "Backend reports a block. No success is inferred by this cockpit.";
    case "failed":
      return "Backend reported a failed attempt. Inspect the receipt before retrying.";
    case "unauthorized":
      return "Operator authority is unavailable. Effect controls are disabled.";
    case "partial_metadata":
      return "Required metadata is unavailable; the outcome remains unknown.";
    case "recovered":
      return "Backend recovery or completion receipt is available for inspection.";
    default:
      return "Current backend state is available for inspection.";
  }
}

function display(value: string | number | null | undefined, fallback = "unknown"): string {
  if (value === null || value === undefined || String(value).trim() === "") return fallback;
  return String(value);
}

function actionLocked(state: OutcomeCockpitState, authorized = true): boolean {
  return !authorized || LOCKED_STATES.includes(state);
}

function StateBadge({ state }: { state: OutcomeCockpitState }) {
  return (
    <span className="cockpit-outcome-state" data-testid="outcome-state" data-state={state}>
      {formatState(state)}
    </span>
  );
}

function ActionButton({ action }: { action: OutcomeAction }) {
  if (!action.onClick && action.disabled) return null;
  return (
    <button
      type="button"
      className="cockpit-outcome-action"
      onClick={action.onClick}
      disabled={action.disabled}
      title={action.title}
    >
      {action.label}
    </button>
  );
}

function Card({
  id,
  label,
  state,
  children,
  actions,
}: {
  id: string;
  label: string;
  state: OutcomeCockpitState;
  children: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <article className="cockpit-outcome-card" data-testid={id} data-state={state}>
      <div className="cockpit-outcome-card-header">
        <span className="cockpit-outcome-card-label">{label}</span>
        <StateBadge state={state} />
      </div>
      <div className="cockpit-outcome-card-body">{children}</div>
      {actions ? <div className="cockpit-outcome-actions">{actions}</div> : null}
    </article>
  );
}

function ValueRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="cockpit-outcome-value-row">
      <span className="cockpit-outcome-value-label">{label}</span>
      <span className="cockpit-outcome-value">{value}</span>
    </div>
  );
}

export function OutcomeCockpitPanel({
  goal,
  work,
  approval,
  route,
  evidence,
  result,
  workLoadState = "partial_metadata",
  approvalLoadState = "ready",
  onOpenPriorities,
  onLoadWork,
  onInspectWork,
  onOpenThread,
  onApprove,
  onDeny,
  onInspectEvidence,
  onInspectOutcome,
  onContinue,
  onRetry,
  onBranch,
}: OutcomeCockpitPanelProps) {
  const approvalLocked = approvalLoadState !== "ready"
    || actionLocked(approval?.state ?? "empty", approval?.authorized !== false);
  const approvalCardState: OutcomeCockpitState = approval?.state
    ?? (approvalLoadState === "loading" ? "loading" : approvalLoadState === "stale" ? "stale" : "empty");
  const recoveryLocked = !(
    work?.state
    && !RECOVERY_LOCKED_STATES.includes(work.state)
  ) || !work;
  const workLoading = workLoadState === "loading";
  const workNeedsLoad = !work && ["partial_metadata", "stale", "degraded"].includes(workLoadState);

  return (
    <section
      className="cockpit-outcome"
      data-testid="outcome-cockpit-panel"
      aria-labelledby="outcome-cockpit-title"
    >
      <div className="cockpit-outcome-header">
        <div>
          <div id="outcome-cockpit-title" className="cockpit-outcome-title">Outcome cockpit</div>
          <div className="cockpit-outcome-subtitle">
            Current goal, governed work, next decision, evidence, and recovery
          </div>
        </div>
        <span className="cockpit-outcome-keyhint">keyboard reachable</span>
      </div>

      <p className="cockpit-outcome-status" role="status" aria-live="polite">
        Inspect backend receipts before treating a work item as complete. {stateMessage(
          approval?.state
            ?? (approvalLoadState === "loading" ? "loading" : approvalLoadState === "stale" ? "stale" : null)
            ?? work?.state
            ?? route.state,
        )}
      </p>

      <div className="cockpit-outcome-grid">
        <Card
          id="outcome-goal-card"
          label="Current goal"
          state={goal?.state ?? "empty"}
          actions={(
            <ActionButton
              action={{
                label: "Open priorities",
                onClick: onOpenPriorities,
                disabled: !onOpenPriorities,
              }}
            />
          )}
        >
          {goal ? (
            <>
              <div className="cockpit-outcome-primary">{goal.title}</div>
              <div className="cockpit-outcome-meta">
                {display(goal.status)}
                {goal.revision != null ? ` · revision ${goal.revision}` : " · revision unavailable"}
              </div>
              <ValueRow label="progress" value={goal.progress == null ? "unknown" : `${goal.progress}% reported`} />
              <ValueRow label="criterion" value={display(goal.criterionSummary, "success criterion unavailable")} />
              <ValueRow label="execution" value={display(goal.latestExecution)} />
              <ValueRow label="verification" value={display(goal.latestVerification)} />
            </>
          ) : (
            <div className="cockpit-outcome-empty">No active goal is available from the goals endpoint.</div>
          )}
        </Card>

        <Card
          id="outcome-work-card"
          label="Active work / next action"
          state={work?.state ?? workLoadState}
          actions={(
            <>
              {workNeedsLoad && onLoadWork ? (
                <ActionButton action={{ label: workLoading ? "Loading current work…" : "Load current work", onClick: onLoadWork, disabled: workLoading }} />
              ) : null}
              {work && onInspectWork ? <ActionButton action={{ label: "Inspect work", onClick: onInspectWork }} /> : null}
            </>
          )}
        >
          {work ? (
            <>
              <div className="cockpit-outcome-primary">{work.label}</div>
              <div className="cockpit-outcome-meta">
                {display(work.status)}
                {work.updatedAt ? ` · updated ${work.updatedAt}` : " · update time unavailable"}
              </div>
              <div className="cockpit-outcome-copy">Backend summary: {display(work.summary, "Work summary unavailable")}</div>
              <ValueRow label="next" value={display(work.nextAction, "next action unavailable")} />
              <ValueRow label="step" value={display(work.stepLabel, "step unavailable")} />
              <ValueRow label="thread" value={display(work.threadLabel, "thread unavailable")} />
              <ValueRow label="artifact" value={display(work.artifactLabel, "no artifact receipt linked")} />
            </>
          ) : (
            <div className="cockpit-outcome-empty">
              {workLoading ? "Loading current work endpoint…" : "Current workflow/job data is unavailable until the workflow runs endpoint is loaded."}
            </div>
          )}
        </Card>

        <Card
          id="outcome-approval-card"
          label="Next decision / approval"
          state={approvalCardState}
          actions={(
            <>
              {approval && onOpenThread ? <ActionButton action={{ label: "Inspect thread", onClick: onOpenThread }} /> : null}
              {approval && onApprove ? (
                <ActionButton
                  action={{
                    label: "Approve",
                    onClick: onApprove,
                    disabled: approvalLocked,
                    title: approvalLocked ? "Approval authority is stale, unavailable, or blocked." : undefined,
                  }}
                />
              ) : null}
              {approval && onDeny ? (
                <ActionButton
                  action={{
                    label: "Deny",
                    onClick: onDeny,
                    disabled: approvalLocked,
                    title: approvalLocked ? "Approval authority is stale, unavailable, or blocked." : undefined,
                  }}
                />
              ) : null}
            </>
          )}
        >
          {approval ? (
            <>
              <div className="cockpit-outcome-primary">{approval.toolLabel}</div>
              <div className="cockpit-outcome-copy">{approval.summary}</div>
              <ValueRow label="risk" value={display(approval.riskLevel)} />
              <ValueRow label="action" value={display(approval.actionStatus, "pending")}/>
              <ValueRow label="scope" value={approval.scope?.length ? approval.scope.join(" · ") : "scope unavailable"} />
              <ValueRow label="permission scope" value={approval.permissions?.length ? approval.permissions.join(" · ") : "permission scope unavailable"} />
              <ValueRow label="thread" value={display(approval.threadLabel, "thread unavailable")} />
              <ValueRow label="owner principal" value={display(approval.ownerPrincipal)} />
              <ValueRow label="owner session" value={display(approval.ownerSession)} />
              <ValueRow label="owner source" value={display(approval.ownerSource)} />
              <ValueRow label="owner expiry" value={display(approval.ownerExpiry)} />
            </>
          ) : (
            <div className="cockpit-outcome-empty">
              {approvalLoadState === "loading"
                ? "Loading the current approval endpoint…"
                : approvalLoadState === "stale"
                  ? "The approvals endpoint is stale or unavailable. Effect controls remain locked until refresh."
                  : "No pending approval is reported by the approvals endpoint."}
            </div>
          )}
        </Card>

        <Card id="outcome-route-card" label="Effective route" state={route.state}>
          <div className="cockpit-outcome-primary">{display(route.route)}</div>
          <ValueRow label="provider" value={display(route.provider)} />
          <ValueRow label="model" value={display(route.model)} />
          <ValueRow label="upstream" value={display(route.upstream)} />
          <ValueRow label="egress" value={display(route.egress)} />
          <ValueRow label="budget" value={display(route.budget, "cost ceiling unknown")} />
          <ValueRow label="queue" value={display(route.queue, "queue status unavailable")} />
          {route.detail ? <div className="cockpit-outcome-note">{route.detail}</div> : null}
        </Card>

        <Card
          id="outcome-evidence-card"
          label="Evidence"
          state={evidence.state}
          actions={onInspectEvidence && evidence.handle ? <ActionButton action={{ label: "Inspect evidence", onClick: onInspectEvidence }} /> : undefined}
        >
          <div className="cockpit-outcome-primary">{display(evidence.label)}</div>
          <div className="cockpit-outcome-copy">{display(evidence.summary, "No evidence summary available")}</div>
          <ValueRow label="source" value={display(evidence.source)} />
          <ValueRow label="provenance" value={display(evidence.provenance, "provenance unavailable")} />
          <ValueRow label="created" value={display(evidence.createdAt, "time unavailable")} />
          {!evidence.handle ? <div className="cockpit-outcome-note">No inspect handle is available for this evidence.</div> : null}
        </Card>

        <Card
          id="outcome-result-card"
          label="Backend outcome receipt"
          state={result.state}
          actions={onInspectOutcome ? <ActionButton action={{ label: "Inspect outcome", onClick: onInspectOutcome }} /> : undefined}
        >
          <div className="cockpit-outcome-primary">{display(result.label)}</div>
          <div className="cockpit-outcome-copy">{display(result.summary, "No outcome receipt is available")}</div>
          <ValueRow label="execution" value={display(result.execution)} />
          <ValueRow label="verification" value={display(result.verification)} />
          <ValueRow label="usefulness" value={display(result.usefulness)} />
          <ValueRow label="learning" value={display(result.learning)} />
          <ValueRow label="source" value={display(result.source)} />
        </Card>

        <Card
          id="outcome-recovery-card"
          label="Recovery / inspect"
          state={work?.state ?? workLoadState}
          actions={(
            <>
              {work && onInspectWork ? <ActionButton action={{ label: "Inspect current run", onClick: onInspectWork }} /> : null}
              {work?.canContinue && onContinue ? (
                  <ActionButton action={{ label: "Continue run", onClick: onContinue, disabled: recoveryLocked }} />
              ) : null}
              {work?.canRetry && onRetry ? (
                <ActionButton action={{ label: "Retry backend step", onClick: onRetry, disabled: recoveryLocked }} />
              ) : null}
              {work?.canBranch && onBranch ? (
                <ActionButton action={{ label: "Branch checkpoint", onClick: onBranch, disabled: recoveryLocked }} />
              ) : null}
            </>
          )}
        >
          {work ? (
            <>
              <div className="cockpit-outcome-copy">{display(work.recoveryHint, "No backend recovery hint is available.")}</div>
              <ValueRow label="continue" value={work.canContinue ? "available" : "unavailable"} />
              <ValueRow label="retry" value={work.canRetry ? "available" : "unavailable"} />
              <ValueRow label="branch" value={work.canBranch ? "available" : "unavailable"} />
              {recoveryLocked ? <div className="cockpit-outcome-note">Recovery controls are locked until current authority is usable.</div> : null}
            </>
          ) : (
            <div className="cockpit-outcome-empty">Load current work to expose backend recovery controls.</div>
          )}
        </Card>
      </div>
    </section>
  );
}
