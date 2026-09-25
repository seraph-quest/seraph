import { useCallback, useEffect, useMemo, useState } from "react";

import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import type { WorkBoardTask } from "../../types";

type ProposalStatus =
  | "proposed"
  | "accepted"
  | "rejected"
  | "no_learning"
  | "blocked"
  | "expired"
  | "rolled_back"
  | "pending_inference"
  | "accepting";

export interface TaskMemoryProposal {
  proposal_id: string;
  recovered_from_proposal_id?: string | null;
  task_id?: string;
  source_task_id?: string;
  attempt_id: string;
  workflow_run_id: string;
  status: ProposalStatus;
  proposed_text?: string | null;
  preview_text?: string | null;
  proposed_text_digest?: string | null;
  preview_text_digest?: string | null;
  memory_kind: string | null;
  memory_scope?: Record<string, unknown> | null;
  scope?: Record<string, unknown> | null;
  confidence: number | null;
  evidence_refs?: string[];
  source_refs?: string[];
  readback_ref?: string | null;
  reason_code: string;
  recovery_action: string;
  accepted_memory_id: string | null;
  accepted_memory_content_digest?: string | null;
  corrects_memory_id?: string | null;
  decision_effect?: "none" | "require_operator_confirmation";
  preferred_capability_id?: string | null;
  registered_capabilities?: { capability_id: string; version: string }[];
  revision: number;
  created_at: string;
  expires_at: string | null;
}

interface SafeDecisionReceipt {
  receipt_id: string;
  receipt_stage: "source_baseline" | "later_comparison";
  source_task_id: string | null;
  source_attempt_id: string | null;
  source_proposal_id?: string | null;
  source_proposal_revision?: number;
  source_baseline_receipt_id?: string | null;
  later_task_id: string;
  later_attempt_id: string | null;
  goal_id: string;
  before_input_digest: string;
  after_input_digest: string;
  before_action_id: string;
  after_action_id: string;
  accepted_memory_id: string | null;
  source_context_digest?: string;
  context_digest?: string;
  evidence_ids?: string[];
  retrieval_evidence_ids?: string[];
  admission_status?: string;
  integrity_status?: string;
  recovery_action?: string;
  receipt_binding_digest?: string;
  accepted_proposal_id?: string | null;
  accepted_memory_content_digest?: string | null;
  expected_task_intent_digest?: string;
  task_intent_digest?: string;
  accepted_proposal_revision?: number;
  decision_status: "changed" | "no_change" | "no_comparable" | "blocked";
  reason: string;
  created_at: string;
}

interface ProposalListResponse {
  proposals: TaskMemoryProposal[];
}

interface DecisionListResponse {
  receipts: SafeDecisionReceipt[];
}

interface RegisteredCapabilityContract {
  capability_id: string;
  version: string;
  input_schema: Record<string, unknown>;
}

interface GoalCandidateDecisionResponse {
  decision: {
    decision_status: "changed" | "no_change" | "no_comparable" | "blocked";
    reason: string;
    before_input_digest: string;
    after_input_digest: string;
    before_selected_capability_id: string | null;
    after_selected_capability_id: string | null;
    evidence_ids: string[];
    receipt_id: string;
  };
  selected: { capability_id: string | null; action: string; reason: string } | null;
}

interface ApiErrorBody {
  detail?: string | { code?: string; message?: string; recovery?: string };
}

interface RefreshOptions {
  preserveError?: boolean;
}

interface WorkBoardMemoryReviewProps {
  task: WorkBoardTask;
  ownerPrincipalId?: string | null;
  ownerSessionId?: string | null;
}

const SAFE_ERROR_MESSAGES: Record<string, string> = {
  stale_proposal_revision: "The proposal revision is stale. Refresh the proposal, review the current revision, and try the action again.",
  stale_task_revision: "The task changed while this proposal was open. Refresh the task and review the proposal again before retrying.",
  stale_goal_revision: "The goal changed while this proposal was open. Refresh the task and review the proposal again before retrying.",
  stale_preview_digest: "The proposal preview changed while this proposal was open. Refresh the proposal and review the current text before retrying.",
  proposal_owner_mismatch: "This proposal belongs to another operator session. Open it from the task owner’s authenticated session and try again.",
  memory_owner_session_forbidden: "This memory review is limited to the task owner’s authenticated session. Switch to that session and try again.",
  memory_owner_session_unbound: "The memory review has no authenticated owner session. Reopen the task in the task owner’s session before trying again.",
  accepted_memory_owner_mismatch: "The accepted memory belongs to another operator session. Use the task owner’s authenticated session to undo it.",
  accepted_binding_unavailable: "Memory could not be signed. The proposal is blocked until its source is reverified and accepted again.",
  decision_receipt_signing_unavailable: "The decision receipt could not be signed. The choice is blocked until the workspace signing key is restored and the source is reviewed again.",
  owner_session_mismatch: "The authenticated operator session does not match this memory review. Switch to the task owner’s session and try again.",
};

function responseMessage(payload: unknown, fallback: string): string {
  const detail = payload && typeof payload === "object"
    ? (payload as ApiErrorBody).detail
    : null;
  const code = typeof detail === "string"
    ? detail.trim()
    : detail && typeof detail === "object"
      ? detail.code?.trim()
      : "";
  if (code && SAFE_ERROR_MESSAGES[code]) {
    return SAFE_ERROR_MESSAGES[code];
  }
  return fallback;
}

async function memoryRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await apiFetch(`${API_URL}${path}`, init);
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(responseMessage(payload, `Memory review failed (${response.status}).`));
  }
  return payload as T;
}

function safeDate(value: string | null): string {
  if (!value) return "No expiry";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "Unknown time" : date.toLocaleString();
}

function digestLabel(value: string): string {
  return value ? `sha256:${value.slice(0, 12)}` : "Not recorded";
}

function WorkBoardMemoryReview({
  task,
  ownerPrincipalId,
  ownerSessionId,
}: WorkBoardMemoryReviewProps) {
  const [proposals, setProposals] = useState<TaskMemoryProposal[]>([]);
  const [receipts, setReceipts] = useState<SafeDecisionReceipt[]>([]);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [decisionEffects, setDecisionEffects] = useState<Record<string, "none" | "require_operator_confirmation">>({});
  const [preferredCapabilities, setPreferredCapabilities] = useState<Record<string, string>>({});
  const [correctionTargets, setCorrectionTargets] = useState<Record<string, string>>({});
  const [rollbackReasons, setRollbackReasons] = useState<Record<string, string>>({});
  const [capabilityContracts, setCapabilityContracts] = useState<RegisteredCapabilityContract[]>([]);
  const [candidateJson, setCandidateJson] = useState("[]");
  const [candidateResult, setCandidateResult] = useState<GoalCandidateDecisionResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const ownsTask = Boolean(
    ownerPrincipalId
      && ownerSessionId
      && ownerPrincipalId === task.owner_principal_id
      && ownerSessionId === task.owner_session_id,
  );
  const proposalUrl = useMemo(
    () => `/api/memory/task-proposals?task_id=${encodeURIComponent(task.task_id)}`,
    [task.task_id],
  );
  const decisionUrl = useMemo(
    () => `/api/memory/task-decisions?task_id=${encodeURIComponent(task.task_id)}`,
    [task.task_id],
  );

  const refresh = useCallback(async (signal?: AbortSignal, options: RefreshOptions = {}) => {
    setLoading(true);
    if (!options.preserveError) setError(null);
    try {
      const [proposalResponse, decisionResponse] = await Promise.all([
        memoryRequest<ProposalListResponse>(proposalUrl, { signal }),
        memoryRequest<DecisionListResponse>(decisionUrl, { signal }),
      ]);
      if (signal?.aborted) return;
      const nextProposals = Array.isArray(proposalResponse.proposals)
        ? proposalResponse.proposals
        : [];
      const nextReceipts = Array.isArray(decisionResponse.receipts)
        ? decisionResponse.receipts
        : [];
      setProposals(nextProposals);
      setReceipts(nextReceipts);
      setEdits((current) => Object.fromEntries(nextProposals.map((item) => [
        item.proposal_id,
        current[item.proposal_id] ?? item.proposed_text ?? item.preview_text ?? "",
      ])));
    } catch (caught) {
      if (!signal?.aborted && !options.preserveError) {
        setError(caught instanceof Error ? caught.message : "Could not load memory review receipts.");
      }
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  }, [decisionUrl, proposalUrl]);

  useEffect(() => {
    const controller = new AbortController();
    setProposals([]);
    setReceipts([]);
    setEdits({});
    setDecisionEffects({});
    setPreferredCapabilities({});
    setCorrectionTargets({});
    setRollbackReasons({});
    setCandidateResult(null);
    setNotice(null);
    if (ownsTask) void refresh(controller.signal);
    return () => controller.abort();
  }, [ownsTask, refresh, task.task_id]);

  const canCompare = ownsTask
    && Boolean(task.goal_id)
    && (task.status === "todo" || task.status === "ready");

  useEffect(() => {
    const controller = new AbortController();
    if (!canCompare) {
      setCapabilityContracts([]);
      return () => controller.abort();
    }
    void memoryRequest<{ capabilities: RegisteredCapabilityContract[] }>(
      "/api/memory/task-decision-capabilities",
      { signal: controller.signal },
    ).then((result) => {
      if (!controller.signal.aborted) {
        setCapabilityContracts(Array.isArray(result.capabilities) ? result.capabilities : []);
      }
    }).catch((caught: unknown) => {
      if (!controller.signal.aborted) {
        setError(caught instanceof Error ? caught.message : "Could not load typed capability contracts.");
      }
    });
    return () => controller.abort();
  }, [canCompare]);

  const createProposal = useCallback(async () => {
    const attempt = task.latest_attempt;
    if (!ownsTask || task.status !== "done" || !attempt?.attempt_id) return;
    setBusyAction("propose");
    setError(null);
    setNotice(null);
    try {
      const result = await memoryRequest<{ status: string; reason_code?: string }>(
        "/api/memory/task-proposals",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            task_id: task.task_id,
            attempt_id: attempt.attempt_id,
            expected_task_revision: task.task_revision,
          }),
        },
      );
      setNotice(result.status === "no_learning"
        ? `No learning recorded (${result.reason_code ?? "no_learning"}).`
        : `Learning review updated (${result.status}).`);
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not request a learning review.");
    } finally {
      setBusyAction(null);
    }
  }, [ownsTask, refresh, task.latest_attempt, task.status, task.task_id, task.task_revision]);

  const actOnProposal = useCallback(async (
    proposal: TaskMemoryProposal,
    action: "accept" | "edit_accept" | "reject" | "rollback" | "recover",
  ) => {
    if (!ownsTask) return;
    const reason = rollbackReasons[proposal.proposal_id]?.trim() ?? "";
    if (action === "rollback" && !reason) {
      setError("Enter a reason before undoing this memory change.");
      return;
    }
    const editedText = edits[proposal.proposal_id]?.trim() ?? "";
    if (action === "edit_accept" && (!editedText || editedText.length > 2_000)) {
      setError("Edited memory must contain between 1 and 2,000 characters.");
      return;
    }
    const decisionEffect = decisionEffects[proposal.proposal_id] ?? "none";
    const preferredCapability = preferredCapabilities[proposal.proposal_id]
      ?? proposal.preferred_capability_id
      ?? "";
    if (
      (action === "accept" || action === "edit_accept")
      && decisionEffect === "require_operator_confirmation"
      && !preferredCapability
    ) {
      setError("Choose a registered capability before allowing the reviewed memory to affect a later comparison.");
      return;
    }
    setBusyAction(`${proposal.proposal_id}:${action}`);
    setError(null);
    setNotice(null);
    try {
      const result = await memoryRequest<{ status: string; accepted_memory_id?: string | null }>(
        `/api/memory/task-proposals/${encodeURIComponent(proposal.proposal_id)}/actions`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            action,
            expected_revision: proposal.revision,
            ...(action === "edit_accept" ? { edited_text: editedText } : {}),
            ...(action === "accept" || action === "edit_accept"
              ? {
                  decision_effect: decisionEffect,
                  ...(preferredCapability ? { preferred_capability_id: preferredCapability } : {}),
                  ...(correctionTargets[proposal.proposal_id]?.trim()
                    ? { corrects_memory_id: correctionTargets[proposal.proposal_id].trim() }
                    : {}),
                }
              : {}),
            expected_preview_text_digest: proposal.proposed_text_digest ?? proposal.preview_text_digest ?? null,
            expected_task_revision: task.task_revision,
            expected_goal_revision: task.goal_revision,
            ...(action === "rollback" ? { reason } : {}),
          }),
        },
      );
      setNotice(result.accepted_memory_id
        ? `${result.status}: canonical memory ${result.accepted_memory_id}`
        : `Memory review ${result.status}.`);
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not update the memory proposal.");
      await refresh(undefined, { preserveError: true });
    } finally {
      setBusyAction(null);
    }
  }, [correctionTargets, decisionEffects, edits, ownsTask, preferredCapabilities, refresh, rollbackReasons, task.goal_revision, task.task_revision]);

  const compareCandidates = useCallback(async () => {
    if (!canCompare || !task.goal_id) return;
    let candidates: unknown;
    try {
      candidates = JSON.parse(candidateJson);
    } catch {
      setError("Candidate input must be valid JSON.");
      return;
    }
    if (!Array.isArray(candidates) || candidates.length < 1 || candidates.length > 20) {
      setError("Enter an ordered list with between 1 and 20 typed candidates.");
      return;
    }
    setBusyAction("compare");
    setError(null);
    setNotice(null);
    try {
      const result = await memoryRequest<GoalCandidateDecisionResponse>(
        `/api/goals/${encodeURIComponent(task.goal_id)}/candidate-set`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            task_id: task.task_id,
            expected_task_revision: task.task_revision,
            expected_goal_revision: task.goal_revision,
            candidates,
          }),
        },
      );
      setCandidateResult(result);
      setNotice(`Candidate comparison recorded (${result.decision.decision_status}). No capability was dispatched.`);
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Could not compare goal candidates.");
    } finally {
      setBusyAction(null);
    }
  }, [canCompare, candidateJson, refresh, task.goal_id, task.goal_revision, task.task_id, task.task_revision]);

  const canRequest = ownsTask && task.status === "done" && Boolean(task.latest_attempt?.attempt_id);

  return (
    <section className="rounded border border-white/10 p-3" aria-label="Verified outcome memory review">
      <div className="flex items-center justify-between gap-2">
        <div className="font-semibold">Learning from this task</div>
        {ownsTask && (
          <button
            type="button"
            className="cockpit-feedback-button"
            disabled={!canRequest || loading || busyAction !== null}
            onClick={() => void createProposal()}
          >
            {busyAction === "propose" ? "Checking evidence…" : "Review learning"}
          </button>
        )}
      </div>
      <p className="mt-1 text-[11px] opacity-75">
        Only verified evidence can become canonical memory. A later choice is recorded only within this authenticated operator session.
      </p>
      {!ownsTask && <div className="mt-2 text-amber-200" role="status">Memory review is available only to this task’s authenticated owner session.</div>}
      {ownsTask && task.status !== "done" && (
        <div className="mt-2 text-xs opacity-75" role="status">Learning review opens after the task reaches Done. The server still checks its run, readback, goal revision, and current fence.</div>
      )}
      {notice && <div className="mt-2 text-xs text-emerald-200" role="status">{notice}</div>}
      {error && <div className="mt-2 rounded border border-amber-500/40 p-2 text-xs" role="alert">{error}</div>}
      {loading && <div className="mt-2 text-xs opacity-75" role="status">Loading memory and decision receipts…</div>}
      {!loading && proposals.length === 0 && ownsTask && (
        <div className="mt-2 text-xs opacity-75">No memory proposal or no-learning receipt is recorded for this task.</div>
      )}
      <div className="mt-2 grid gap-2">
        {proposals.map((proposal) => {
          const waiting = busyAction?.startsWith(`${proposal.proposal_id}:`) ?? false;
          const proposalText = proposal.proposed_text ?? proposal.preview_text ?? "";
          const editableText = edits[proposal.proposal_id] ?? proposalText;
          const evidenceRefs = proposal.evidence_refs ?? proposal.source_refs ?? (proposal.readback_ref ? [proposal.readback_ref] : []);
          const scope = proposal.memory_scope ?? proposal.scope ?? {};
          return (
            <article key={proposal.proposal_id} className="rounded bg-black/20 p-2" aria-label={`Memory proposal ${proposal.status}`}>
              <div className="flex flex-wrap items-center justify-between gap-2">
                <strong>{proposal.status.replace(/_/g, " ")}</strong>
                <span className="text-[10px] opacity-70">revision {proposal.revision} · expires {safeDate(proposal.expires_at)}</span>
              </div>
              <div className="mt-1">Attempt {proposal.attempt_id} · workflow {proposal.workflow_run_id}</div>
              {proposal.recovered_from_proposal_id && (
                <div className="mt-1 text-[10px] text-sky-200">
                  Fresh recovery generation from proposal {proposal.recovered_from_proposal_id}
                </div>
              )}
              {proposalText && <p className="mt-2 whitespace-pre-wrap break-words">{proposalText}</p>}
              {(proposal.proposed_text_digest ?? proposal.preview_text_digest) && <div className="mt-1 font-mono text-[10px]">Proposal {digestLabel(proposal.proposed_text_digest ?? proposal.preview_text_digest ?? "")}</div>}
              {proposal.memory_kind && <div className="mt-1">Memory type {proposal.memory_kind}</div>}
              {typeof scope.preferred_capability_id === "string" && <div className="mt-1">Preferred registered capability: {scope.preferred_capability_id}</div>}
              {proposal.confidence !== null && <div className="mt-1">Confidence {proposal.confidence.toFixed(2)}</div>}
              {proposal.reason_code && <div className="mt-1">Reason {proposal.reason_code.replace(/_/g, " ")}</div>}
              {proposal.recovery_action && <div className="mt-1 text-amber-200">Recovery: {proposal.recovery_action.replace(/_/g, " ")}</div>}
              {proposal.corrects_memory_id && <div className="mt-1 text-amber-100">If accepted, this review will supersede prior canonical memory {proposal.corrects_memory_id}.</div>}
              {evidenceRefs.length > 0 && (
                <div className="mt-2" aria-label="Memory evidence references">
                  <div className="text-[10px] uppercase opacity-70">Verified evidence references</div>
                  <ul className="mt-1 list-inside list-disc break-all font-mono text-[10px]">
                    {evidenceRefs.map((reference) => <li key={reference}>{reference}</li>)}
                  </ul>
                </div>
              )}
              {proposal.status === "proposed" && ownsTask && (
                <div className="mt-2 grid gap-2">
                  <label>
                    Edit before acceptance
                    <textarea
                      className="cockpit-input mt-1 w-full"
                      maxLength={2_000}
                      rows={3}
                      value={editableText}
                      onChange={(event) => {
                        const value = event.currentTarget.value;
                        setEdits((current) => ({ ...current, [proposal.proposal_id]: value }));
                      }}
                    />
                  </label>
                  <label>
                    Future comparable decision
                    <select
                      className="cockpit-input mt-1 w-full"
                      value={decisionEffects[proposal.proposal_id] ?? "none"}
                      onChange={(event) => {
                        const value = event.currentTarget.value as "none" | "require_operator_confirmation";
                        setDecisionEffects((current) => ({ ...current, [proposal.proposal_id]: value }));
                      }}
                    >
                      <option value="none">Keep as advisory canonical memory</option>
                      <option value="require_operator_confirmation">May select the reviewed capability in proposal-only comparisons</option>
                    </select>
                    <span className="mt-1 block text-[10px] opacity-75">The later comparison remains a proposal. This choice cannot add capabilities, alter inputs, or bypass current grants, approvals, or admission.</span>
                  </label>
                  {(decisionEffects[proposal.proposal_id] ?? "none") === "require_operator_confirmation" && (
                    <label>
                      Preferred registered capability
                      <select
                        className="cockpit-input mt-1 w-full"
                        value={preferredCapabilities[proposal.proposal_id] ?? proposal.preferred_capability_id ?? ""}
                        onChange={(event) => {
                          const value = event.currentTarget.value;
                          setPreferredCapabilities((current) => ({ ...current, [proposal.proposal_id]: value }));
                        }}
                      >
                        <option value="">Choose a registered capability</option>
                        {(proposal.registered_capabilities ?? []).map((item) => (
                          <option key={item.capability_id} value={item.capability_id}>{item.capability_id} · v{item.version}</option>
                        ))}
                      </select>
                    </label>
                  )}
                  <label>
                    Existing canonical memory ID to supersede (optional)
                    <input
                      className="cockpit-input mt-1 w-full"
                      maxLength={255}
                      value={correctionTargets[proposal.proposal_id] ?? ""}
                      onChange={(event) => {
                        const value = event.currentTarget.value;
                        setCorrectionTargets((current) => ({ ...current, [proposal.proposal_id]: value }));
                      }}
                    />
                  </label>
                  <div className="flex flex-wrap gap-2">
                    <button type="button" className="cockpit-feedback-button" disabled={waiting || busyAction !== null} onClick={() => void actOnProposal(proposal, "accept")}>Accept proposal</button>
                    <button type="button" className="cockpit-feedback-button" disabled={waiting || busyAction !== null || !editableText.trim()} onClick={() => void actOnProposal(proposal, "edit_accept")}>Edit and accept</button>
                    <button type="button" className="cockpit-feedback-button" disabled={waiting || busyAction !== null} onClick={() => void actOnProposal(proposal, "reject")}>Reject</button>
                  </div>
                </div>
              )}
              {(proposal.status === "blocked" || proposal.status === "expired")
                && (proposal.recovery_action === "verify_source_and_reaccept"
                  || proposal.recovery_action === "request_verified_proposal_again")
                && ownsTask && (
                  <div className="mt-2 grid gap-2">
                    <p className="text-xs">Seraph will recheck this task’s current verified run and readback, then regenerate a fresh proposal. Review and accept it again before it can update canonical memory.</p>
                    <button
                      type="button"
                      className="cockpit-feedback-button self-start"
                      disabled={waiting || busyAction !== null}
                      onClick={() => void actOnProposal(proposal, "recover")}
                    >
                      {waiting ? "Rechecking verified source…" : "Verify source and review again"}
                    </button>
                  </div>
                )}
              {proposal.status === "accepted" && ownsTask && (
                <div className="mt-2 grid gap-2">
                  <div>Accepted canonical memory {proposal.accepted_memory_id ?? "receipt unavailable"}</div>
                  <label>
                    Undo reason
                    <input
                      className="cockpit-input mt-1 w-full"
                      maxLength={500}
                      value={rollbackReasons[proposal.proposal_id] ?? ""}
                      onChange={(event) => {
                        const value = event.currentTarget.value;
                        setRollbackReasons((current) => ({ ...current, [proposal.proposal_id]: value }));
                      }}
                    />
                  </label>
                  <button type="button" className="cockpit-feedback-button self-start" disabled={waiting || busyAction !== null || !(rollbackReasons[proposal.proposal_id] ?? "").trim()} onClick={() => void actOnProposal(proposal, "rollback")}>Undo memory change</button>
                </div>
              )}
            </article>
          );
        })}
      </div>
      {canCompare && (
        <div className="mt-3 grid gap-2 rounded border border-white/10 p-2" aria-label="Proposal-only candidate comparison">
          <div className="font-semibold">Compare a later goal decision</div>
          <p className="text-[11px] opacity-75">Supply candidates in selection order. The server validates each capability and input against its registered typed contract. This records a proposal only; it does not create a task or dispatch work.</p>
          <details>
            <summary className="cursor-pointer text-xs">Registered typed input contracts</summary>
            {capabilityContracts.length === 0
              ? <div className="mt-1 text-xs opacity-75">No typed capabilities are currently registered.</div>
              : capabilityContracts.map((item) => (
                  <pre key={item.capability_id} className="mt-2 overflow-auto rounded bg-black/30 p-2 text-[10px]">{`${item.capability_id} · v${item.version}\n${JSON.stringify(item.input_schema, null, 2)}`}</pre>
                ))}
          </details>
          <label>
            Ordered typed candidate JSON
            <textarea
              className="cockpit-input mt-1 w-full font-mono text-xs"
              rows={7}
              value={candidateJson}
              onChange={(event) => setCandidateJson(event.currentTarget.value)}
              aria-label="Ordered typed candidate JSON"
            />
          </label>
          <button type="button" className="cockpit-feedback-button self-start" disabled={busyAction !== null || loading || capabilityContracts.length === 0} onClick={() => void compareCandidates()}>
            {busyAction === "compare" ? "Comparing…" : "Compare candidates"}
          </button>
          {candidateResult && (
            <div role="status" className="rounded bg-black/20 p-2 text-xs">
              <div>{candidateResult.decision.decision_status.replace(/_/g, " ")} · {candidateResult.decision.reason}</div>
              <div className="mt-1 break-all">Before: {candidateResult.decision.before_selected_capability_id ?? "No comparable action"} → After: {candidateResult.decision.after_selected_capability_id ?? "No comparable action"}</div>
              <div className="mt-1 break-all font-mono text-[10px]">Input digests {digestLabel(candidateResult.decision.before_input_digest)} → {digestLabel(candidateResult.decision.after_input_digest)}</div>
              {candidateResult.decision.evidence_ids.length > 0 && <div className="mt-1 break-all text-[10px]">Verified source evidence: {candidateResult.decision.evidence_ids.join(", ")}</div>}
              <div className="mt-1 font-mono text-[10px]">Receipt {candidateResult.decision.receipt_id}</div>
            </div>
          )}
        </div>
      )}
      {receipts.length > 0 && (
        <div className="mt-3 grid gap-2" aria-label="Memory decision receipts">
          <div className="font-semibold">Comparable decision receipts</div>
          {receipts.map((receipt) => (
            <article key={receipt.receipt_id} className="rounded border border-white/10 bg-black/20 p-2">
              <div className="font-semibold">{receipt.decision_status.replace(/_/g, " ")} · {receipt.receipt_stage.replace(/_/g, " ")}</div>
              <div className="mt-1 break-all">Task {receipt.later_task_id} · goal {receipt.goal_id}</div>
              <div className="mt-1 break-all">Before: {receipt.before_action_id || "No comparable action"} → After: {receipt.after_action_id || "No comparable action"}</div>
              <div className="mt-1 break-all font-mono text-[10px]">Input digests {digestLabel(receipt.before_input_digest)} → {digestLabel(receipt.after_input_digest)}</div>
              <div className="mt-1">{receipt.reason}</div>
              {receipt.integrity_status && receipt.integrity_status !== "verified" && (
                <div className="mt-1 text-amber-200" role="status">
                  Receipt evidence is quarantined ({receipt.integrity_status.replace(/_/g, " ")}). Recompute from current verified source before relying on it.
                </div>
              )}
              {receipt.source_baseline_receipt_id && <div className="mt-1 font-mono text-[10px]">Compared against source baseline {receipt.source_baseline_receipt_id}</div>}
              {(receipt.evidence_ids ?? receipt.retrieval_evidence_ids ?? []).length > 0 && <div className="mt-1 break-all text-[10px]">Evidence IDs: {(receipt.evidence_ids ?? receipt.retrieval_evidence_ids ?? []).join(", ")}</div>}
              <div className="mt-1 text-[10px] opacity-70">Recorded {safeDate(receipt.created_at)}</div>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

export { WorkBoardMemoryReview };
