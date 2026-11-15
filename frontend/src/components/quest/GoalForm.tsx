import { useEffect, useRef, useState } from "react";
import { GoalUpdateError, useQuestStore } from "../../stores/questStore";
import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import { GoalProgrammePanel } from "./GoalProgrammePanel";
import { createGuardianUuid, fetchGuardianPolicyWatches, GuardianInboxApiError, isGuardianGoalPolicy, saveGuardianPolicy } from "../../lib/guardianInbox";
import type {
  GoalAdmissionBudget,
  GoalCriterionVerifier,
  GoalInfo,
  GoalSuccessCriterion,
  GuardianGoalPolicy,
  GuardianPolicyWatch,
} from "../../types";

const LEVELS = ["daily", "weekly", "monthly", "quarterly", "annual", "vision"] as const;
const DOMAINS = ["productivity", "performance", "health", "influence", "growth"] as const;
const VERIFIERS: Array<{ value: GoalCriterionVerifier; label: string }> = [
  { value: "artifact_readback", label: "Artifact readback" },
  { value: "external_readback", label: "External readback" },
  { value: "operator_attestation", label: "Operator attestation" },
];

interface Props {
  goal?: GoalInfo;
  onClose: () => void;
}

function operatorTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

function isoAfterDays(days: number): string {
  const value = new Date();
  value.setDate(value.getDate() + days);
  return value.toISOString();
}

function dateInputValue(value: string | null | undefined): string {
  return value ? value.slice(0, 10) : "";
}

function defaultBudget(): GoalAdmissionBudget {
  return {
    reviewed_grant: false,
    grant_id: null,
    max_outstanding_jobs: 1,
    max_attempts: 1,
    max_runtime_seconds: 300,
    notifications_per_day: 0,
    period_started_at: new Date().toISOString(),
    period_expires_at: isoAfterDays(7),
    quiet_hours_start: 22,
    quiet_hours_end: 8,
    timezone: operatorTimezone(),
  };
}

function budgetWithDefaults(existing: GoalAdmissionBudget | null | undefined): GoalAdmissionBudget {
  return { ...defaultBudget(), ...(existing ?? {}) };
}

function targetValue(target: GoalSuccessCriterion["target"] | undefined): string {
  if (target === undefined || target === null) return "";
  return typeof target === "string" ? target : JSON.stringify(target, null, 2);
}

function parseTarget(value: string): GoalSuccessCriterion["target"] {
  const trimmed = value.trim();
  if (!trimmed) return "";
  if (!trimmed.startsWith("{")) return trimmed;
  const parsed: unknown = JSON.parse(trimmed);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("Target JSON must be an object or a plain text value.");
  }
  return parsed as Record<string, unknown>;
}

function evidenceValues(value: string): string[] {
  return value
    .split(/[\n,]/)
    .map((entry) => entry.trim())
    .filter(Boolean);
}

function numericValue(value: string): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

let budgetGrantSequence = 0;

function createBudgetGrantId(): string {
  const random = globalThis.crypto?.randomUUID?.();
  if (random) return `budget-review:${random}`;
  budgetGrantSequence += 1;
  return `budget-review:${Date.now()}:${budgetGrantSequence}`;
}

export function GoalForm({ goal, onClose }: Props) {
  const createGoal = useQuestStore((s) => s.createGoal);
  const updateGoal = useQuestStore((s) => s.updateGoal);

  const [title, setTitle] = useState(goal?.title ?? "");
  const [level, setLevel] = useState(goal?.level ?? "weekly");
  const [domain, setDomain] = useState(goal?.domain ?? "productivity");
  const [description, setDescription] = useState(goal?.description ?? "");
  const [dueDate, setDueDate] = useState(dateInputValue(goal?.due_date));
  const existingCriterion = goal?.success_criterion ?? null;
  const [criterionEnabled, setCriterionEnabled] = useState(Boolean(existingCriterion));
  const [criterionTouched, setCriterionTouched] = useState(false);
  const [criterionId, setCriterionId] = useState(existingCriterion?.criterion_id ?? "criterion-1");
  const [criterionDescription, setCriterionDescription] = useState(existingCriterion?.description ?? "");
  const [verifierKind, setVerifierKind] = useState<GoalCriterionVerifier | "">(
    existingCriterion?.verifier_kind ?? "",
  );
  const [criterionTarget, setCriterionTarget] = useState(targetValue(existingCriterion?.target));
  const [evidenceRefs, setEvidenceRefs] = useState(existingCriterion?.evidence_refs.join("\n") ?? "");
  const [proactiveEnabled, setProactiveEnabled] = useState(goal?.proactive_enabled ?? false);
  const [proactiveTouched, setProactiveTouched] = useState(false);
  const [newBudgetGrantId] = useState(() => createBudgetGrantId());
  const [budget, setBudget] = useState<GoalAdmissionBudget>(() => budgetWithDefaults(goal?.admission_budget));
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  const isEdit = !!goal;

  const setBudgetField = <K extends keyof GoalAdmissionBudget>(field: K, value: GoalAdmissionBudget[K]) => {
    setBudget((current) => ({ ...current, [field]: value }));
  };

  const buildCriterion = (): GoalSuccessCriterion | null => {
    if (!criterionEnabled) return null;
    const id = criterionId.trim();
    const criterionText = criterionDescription.trim();
    if (!id || !criterionText) {
      throw new Error("A criterion ID and description are required when verification is enabled.");
    }
    let parsedTarget: GoalSuccessCriterion["target"];
    try {
      parsedTarget = parseTarget(criterionTarget);
    } catch (err) {
      throw new Error(err instanceof Error ? err.message : "Criterion target is not valid JSON.");
    }
    return {
      criterion_id: id,
      description: criterionText,
      verifier_kind: verifierKind || null,
      target: parsedTarget,
      evidence_refs: evidenceValues(evidenceRefs),
    };
  };

  const handleSubmit = async () => {
    if (!title.trim()) {
      setError("Title is required");
      return;
    }
    setError("");
    let criterion: GoalSuccessCriterion | null = null;
    if (criterionEnabled) {
      try {
        criterion = buildCriterion();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Criterion is incomplete");
        return;
      }
    }
    if (proactiveEnabled && budget.reviewed_grant && !budget.grant_id?.trim()) {
      setError("A reviewed grant reference is required before reviewed limits can be enabled.");
      return;
    }
    if (proactiveEnabled && budget.period_expires_at && budget.period_started_at) {
      if (new Date(budget.period_expires_at).getTime() <= new Date(budget.period_started_at).getTime()) {
        setError("The authority period must expire after it starts.");
        return;
      }
    }

    setSaving(true);
    try {
      const criterionPatch = criterionEnabled
        ? { success_criterion: criterion }
        : isEdit && existingCriterion && criterionTouched
          ? { success_criterion: null }
          : {};
      const proactivePatch = proactiveEnabled || proactiveTouched
        ? {
            proactive_enabled: proactiveEnabled,
            ...(proactiveEnabled ? { admission_budget: { ...budget, grant_id: budget.grant_id?.trim() || null } } : {}),
          }
        : {};

      if (isEdit) {
        await updateGoal(goal.id, {
          title: title.trim(),
          level,
          domain,
          description: description.trim() || undefined,
          due_date: dueDate || null,
          ...criterionPatch,
          ...proactivePatch,
          ...(typeof goal.revision === "number" ? { expected_revision: goal.revision } : {}),
        });
      } else {
        await createGoal({
          title: title.trim(),
          level,
          domain,
          description: description.trim() || undefined,
          due_date: dueDate || undefined,
          ...(criterionEnabled ? { success_criterion: criterion } : {}),
          ...(proactiveEnabled
            ? { proactive_enabled: true, admission_budget: { ...budget, grant_id: budget.grant_id?.trim() || null } }
            : {}),
        });
      }
      onClose();
    } catch (err) {
      if (err instanceof GoalUpdateError && err.code === "stale_goal_revision") {
        setError("This priority changed elsewhere. Your draft is still here; refresh and review before saving again.");
      } else {
        setError(err instanceof GoalUpdateError ? err.message : "Failed to save");
      }
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="cockpit-modal-shell cockpit-modal-shell--raised">
      <button
        type="button"
        className="cockpit-modal-backdrop"
        onClick={onClose}
        aria-label="Close priority editor"
      />
      <section className="cockpit-modal-card cockpit-modal-card--goal-editor">
        <div className="cockpit-modal-header">
          <div>
            <div className="cockpit-card-title">{isEdit ? "Edit Priority" : "New Priority"}</div>
            <div className="cockpit-card-meta">stored in the structured goal system</div>
          </div>
          <button
            type="button"
            className="cockpit-modal-close"
            aria-label="Close priority editor"
            title="Close priority editor"
            onClick={onClose}
          >
            x
          </button>
        </div>
        <div className="cockpit-modal-body cockpit-modal-form cockpit-tone-scope cockpit-goals-scope">
          <input
            type="text"
            placeholder="Priority title"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            className="w-full bg-transparent text-[11px] text-slate-100 border-b border-white/10 px-0.5 py-1 outline-none focus:border-cyan-300 placeholder:text-slate-500"
            autoFocus
          />

          <div className="flex gap-2">
            <div className="flex-1">
              <div className="text-[10px] text-slate-400 mb-1 uppercase tracking-[0.16em]">Level</div>
              <select
                value={level}
                onChange={(e) => setLevel(e.target.value)}
                className="w-full bg-slate-950/70 text-[11px] text-slate-100 border border-white/10 rounded-md px-2 py-1.5 outline-none focus:border-cyan-300"
              >
                {LEVELS.map((l) => (
                  <option key={l} value={l}>{l}</option>
                ))}
              </select>
            </div>
            <div className="flex-1">
              <div className="text-[10px] text-slate-400 mb-1 uppercase tracking-[0.16em]">Domain</div>
              <select
                value={domain}
                onChange={(e) => setDomain(e.target.value)}
                className="w-full bg-slate-950/70 text-[11px] text-slate-100 border border-white/10 rounded-md px-2 py-1.5 outline-none focus:border-cyan-300"
              >
                {DOMAINS.map((d) => (
                  <option key={d} value={d}>{d}</option>
                ))}
              </select>
            </div>
          </div>

          <textarea
            placeholder="Description (optional)"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={3}
            className="w-full bg-slate-950/70 text-[11px] text-slate-100 border border-white/10 rounded-md px-2 py-2 outline-none focus:border-cyan-300 resize-none placeholder:text-slate-500"
          />

          <div>
            <div className="text-[10px] text-slate-400 mb-1 uppercase tracking-[0.16em]">
              Due date (optional)
            </div>
            <input
              type="date"
              value={dueDate}
              onChange={(e) => setDueDate(e.target.value)}
              className="bg-slate-950/70 text-[11px] text-slate-100 border border-white/10 rounded-md px-2 py-1.5 outline-none focus:border-cyan-300"
            />
          </div>

          <section className="border border-white/10 rounded-md p-3 space-y-2" aria-label="Success criterion">
            <label className="flex items-center gap-2 text-[10px] uppercase tracking-[0.14em] text-slate-300">
              <input
                type="checkbox"
                checked={criterionEnabled}
                onChange={(e) => {
                  setCriterionEnabled(e.target.checked);
                  setCriterionTouched(true);
                }}
              />
              Configure success criterion
            </label>
            {criterionEnabled && (
              <div className="space-y-2">
                <input
                  aria-label="Criterion ID"
                  value={criterionId}
                  onChange={(e) => { setCriterionId(e.target.value); setCriterionTouched(true); }}
                  placeholder="Criterion ID"
                  className="w-full bg-slate-950/70 text-[11px] text-slate-100 border border-white/10 rounded-md px-2 py-1.5 outline-none focus:border-cyan-300"
                />
                <input
                  aria-label="Criterion description"
                  value={criterionDescription}
                  onChange={(e) => { setCriterionDescription(e.target.value); setCriterionTouched(true); }}
                  placeholder="What observable outcome proves progress?"
                  className="w-full bg-slate-950/70 text-[11px] text-slate-100 border border-white/10 rounded-md px-2 py-1.5 outline-none focus:border-cyan-300"
                />
                <div className="flex gap-2">
                  <select
                    aria-label="Criterion verifier"
                    value={verifierKind}
                    onChange={(e) => { setVerifierKind(e.target.value as GoalCriterionVerifier | ""); setCriterionTouched(true); }}
                    className="flex-1 bg-slate-950/70 text-[11px] text-slate-100 border border-white/10 rounded-md px-2 py-1.5 outline-none focus:border-cyan-300"
                  >
                    <option value="">Choose verifier (optional)</option>
                    {VERIFIERS.map((verifier) => <option key={verifier.value} value={verifier.value}>{verifier.label}</option>)}
                  </select>
                  <input
                    aria-label="Criterion target"
                    value={criterionTarget}
                    onChange={(e) => { setCriterionTarget(e.target.value); setCriterionTouched(true); }}
                    placeholder="Target or JSON object"
                    className="flex-1 bg-slate-950/70 text-[11px] text-slate-100 border border-white/10 rounded-md px-2 py-1.5 outline-none focus:border-cyan-300"
                  />
                </div>
                <textarea
                  aria-label="Criterion evidence references"
                  value={evidenceRefs}
                  onChange={(e) => { setEvidenceRefs(e.target.value); setCriterionTouched(true); }}
                  placeholder="Evidence references, one per line"
                  rows={2}
                  className="w-full bg-slate-950/70 text-[11px] text-slate-100 border border-white/10 rounded-md px-2 py-2 outline-none focus:border-cyan-300 resize-none"
                />
              </div>
            )}
          </section>

          <section className="border border-white/10 rounded-md p-3 space-y-3" aria-label="Standing goal authority">
            <label className="flex items-center gap-2 text-[10px] uppercase tracking-[0.14em] text-slate-300">
              <input
                type="checkbox"
                checked={proactiveEnabled}
                onChange={(e) => {
                  setProactiveEnabled(e.target.checked);
                  setProactiveTouched(true);
                }}
              />
              Enable standing goal observations
            </label>
            {proactiveEnabled && (
              <div className="space-y-2">
                <div className="text-[10px] leading-relaxed text-slate-400">
                  Review these finite limits before a source watch can admit work. This form records a current grant reference;
                  the server rechecks authority at execution time.
                </div>
                <label className="flex items-center gap-2 text-[10px] text-slate-300">
                  <input
                    type="checkbox"
                    checked={budget.reviewed_grant}
                    onChange={(e) => setBudget((current) => ({
                      ...current,
                      reviewed_grant: e.target.checked,
                      grant_id: e.target.checked ? (current.grant_id?.trim() || newBudgetGrantId) : current.grant_id,
                    }))}
                  />
                  I reviewed these limits and the local output scope
                </label>
                <input
                  aria-label="Reviewed grant reference"
                  value={budget.grant_id ?? ""}
                  readOnly
                  placeholder="Generated after limits are reviewed"
                  className="w-full bg-slate-950/70 text-[11px] text-slate-100 border border-white/10 rounded-md px-2 py-1.5 outline-none focus:border-cyan-300"
                />
                {budget.grant_id ? (
                  <div className="flex items-center justify-between gap-2 text-[10px] text-slate-400">
                    <span>Advanced receipt reference is retained across retries.</span>
                    <button
                      type="button"
                      className="cockpit-action cockpit-action--ghost"
                      onClick={() => setBudget((current) => ({ ...current, grant_id: createBudgetGrantId() }))}
                    >
                      Rotate review reference
                    </button>
                  </div>
                ) : null}
                <div className="grid grid-cols-2 gap-2">
                  <label className="text-[10px] text-slate-400">
                    Max outstanding jobs
                    <input
                      aria-label="Maximum outstanding jobs"
                      type="number"
                      min={1}
                      max={16}
                      value={budget.max_outstanding_jobs}
                      onChange={(e) => setBudgetField("max_outstanding_jobs", numericValue(e.target.value))}
                      className="mt-1 w-full bg-slate-950/70 text-[11px] text-slate-100 border border-white/10 rounded-md px-2 py-1.5"
                    />
                  </label>
                  <label className="text-[10px] text-slate-400">
                    Max attempts per job
                    <input
                      aria-label="Maximum attempts"
                      type="number"
                      min={1}
                      max={3}
                      value={budget.max_attempts}
                      onChange={(e) => setBudgetField("max_attempts", numericValue(e.target.value))}
                      className="mt-1 w-full bg-slate-950/70 text-[11px] text-slate-100 border border-white/10 rounded-md px-2 py-1.5"
                    />
                  </label>
                  <label className="text-[10px] text-slate-400">
                    Runtime limit (seconds)
                    <input
                      aria-label="Maximum runtime seconds"
                      type="number"
                      min={1}
                      max={900}
                      value={budget.max_runtime_seconds}
                      onChange={(e) => setBudgetField("max_runtime_seconds", numericValue(e.target.value))}
                      className="mt-1 w-full bg-slate-950/70 text-[11px] text-slate-100 border border-white/10 rounded-md px-2 py-1.5"
                    />
                  </label>
                  <label className="text-[10px] text-slate-400">
                    Notifications per day
                    <input
                      aria-label="Notifications per day"
                      type="number"
                      min={0}
                      max={100}
                      value={budget.notifications_per_day}
                      onChange={(e) => setBudgetField("notifications_per_day", numericValue(e.target.value))}
                      className="mt-1 w-full bg-slate-950/70 text-[11px] text-slate-100 border border-white/10 rounded-md px-2 py-1.5"
                    />
                  </label>
                </div>
                <div className="grid grid-cols-2 gap-2">
                  <label className="text-[10px] text-slate-400">
                    Operator timezone (IANA)
                    <input
                      aria-label="Operator timezone"
                      value={budget.timezone}
                      onChange={(e) => setBudgetField("timezone", e.target.value)}
                      className="mt-1 w-full bg-slate-950/70 text-[11px] text-slate-100 border border-white/10 rounded-md px-2 py-1.5"
                    />
                  </label>
                  <label className="text-[10px] text-slate-400">
                    Authority period expires
                    <input
                      aria-label="Authority period expiry"
                      type="date"
                      value={dateInputValue(budget.period_expires_at)}
                      onChange={(e) => setBudgetField("period_expires_at", e.target.value ? `${e.target.value}T23:59:59.000Z` : null)}
                      className="mt-1 w-full bg-slate-950/70 text-[11px] text-slate-100 border border-white/10 rounded-md px-2 py-1.5"
                    />
                  </label>
                </div>
                <div className="grid grid-cols-2 gap-2">
                  <label className="text-[10px] text-slate-400">
                    Quiet hours start
                    <select
                      aria-label="Quiet hours start"
                      value={budget.quiet_hours_start ?? 22}
                      onChange={(e) => setBudgetField("quiet_hours_start", numericValue(e.target.value))}
                      className="mt-1 w-full bg-slate-950/70 text-[11px] text-slate-100 border border-white/10 rounded-md px-2 py-1.5"
                    >
                      {Array.from({ length: 24 }, (_, hour) => <option key={hour} value={hour}>{String(hour).padStart(2, "0")}:00</option>)}
                    </select>
                  </label>
                  <label className="text-[10px] text-slate-400">
                    Quiet hours end
                    <select
                      aria-label="Quiet hours end"
                      value={budget.quiet_hours_end ?? 8}
                      onChange={(e) => setBudgetField("quiet_hours_end", numericValue(e.target.value))}
                      className="mt-1 w-full bg-slate-950/70 text-[11px] text-slate-100 border border-white/10 rounded-md px-2 py-1.5"
                    >
                      {Array.from({ length: 24 }, (_, hour) => <option key={hour} value={hour}>{String(hour).padStart(2, "0")}:00</option>)}
                    </select>
                  </label>
                </div>
                <div className="text-[10px] text-amber-300/80">
                  Review records these limits and a stable grant correlation only; it does not issue authority or bypass current capability checks.
                </div>
              </div>
            )}
          </section>

          <div className="text-[10px] text-amber-300/80" role="note">
            {goal?.guardian_policy?.assessment_enabled
              ? "Editing this priority invalidates its assessment policy. Review and rebind its source watches, then explicitly review the policy in the Goal loop."
              : "Cited opportunity assessments stay off until this priority has a finite reviewed grant and current public source watches, then you explicitly save its assessment policy in the Goal loop."}
          </div>

          {goal ? <GoalProgrammePanel key={goal.id} goal={goal} goalDraftChanged={
            title !== goal.title || description !== (goal.description ?? "") || level !== goal.level
            || domain !== goal.domain || dueDate !== dateInputValue(goal.due_date)
            || criterionTouched || proactiveTouched
          } /> : <p className="text-xs">Save your priority, then reopen it to configure a finite public programme.</p>}

          {error && <div className="text-[10px] text-rose-400" role="alert">{error}</div>}

          <div className="flex gap-2 pt-1">
            <button
              onClick={handleSubmit}
              disabled={saving}
              className="cockpit-action cockpit-action--primary disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {saving ? "Saving..." : isEdit ? "Update" : "Create"}
            </button>
            <button
              onClick={onClose}
              className="cockpit-action cockpit-action--ghost"
            >
              Cancel
            </button>
          </div>
        </div>
      </section>
    </div>
  );
}

/** A policy-only save never edits a Goal, rebinds a watch, or dispatches work. */
export function GuardianPolicyForm({ goal }: { goal: GoalInfo }) {
  const [currentGoal, setCurrentGoal] = useState(goal);
  const [watches, setWatches] = useState<GuardianPolicyWatch[]>([]);
  const [metadataError, setMetadataError] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [requiresRefresh, setRequiresRefresh] = useState(false);
  const [enabled, setEnabled] = useState(goal.guardian_policy?.assessment_enabled ?? false);
  const [selected, setSelected] = useState<string[]>(goal.guardian_policy?.source_watch_ids ?? []);
  const [reviewDue, setReviewDue] = useState(goal.guardian_policy?.review_due_at ?? boundedReviewDue(goal));
  const [assessmentCap, setAssessmentCap] = useState(goal.guardian_policy?.max_assessments_per_utc_day ?? 1);
  const [planCap, setPlanCap] = useState(goal.guardian_policy?.max_plan_proposals_per_utc_day ?? 0);
  const [notificationCap, setNotificationCap] = useState(goal.guardian_policy?.max_notification_per_utc_day ?? 0);
  const [autoStage, setAutoStage] = useState(goal.guardian_policy?.auto_stage_plan ?? false);
  const [ackStage, setAckStage] = useState(false);
  const [ackNotifications, setAckNotifications] = useState(false);
  const saveRequest = useRef<{ signature: string; key: string; policy: GuardianGoalPolicy } | null>(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();
    void fetchGuardianPolicyWatches(controller.signal).then((rows) => {
      if (mounted.current && !controller.signal.aborted) { setWatches(rows); setMetadataError(""); }
    }).catch(() => {
      if (mounted.current && !controller.signal.aborted) setMetadataError("Source watch metadata is unavailable. Last-known selections are retained; refresh before enabling new assessments.");
    });
    return () => { mounted.current = false; controller.abort(); };
  }, [goal.id]);

  useEffect(() => {
    setCurrentGoal((previous) => {
      const sameScope = previous.id === goal.id && previous.owner_session_id === goal.owner_session_id
        && previous.ownership_access === goal.ownership_access;
      // A parent tree refresh can lag behind an explicit save/metadata receipt.
      // Scope changes must still replace metadata, including recovered reads.
      if (sameScope && ((goal.revision ?? 0) < (previous.revision ?? 0)
        || (goal.revision === previous.revision
          && (goal.guardian_policy_revision ?? 0) < (previous.guardian_policy_revision ?? 0)))) return previous;
      return goal;
    });
    if (goal.owner_session_id !== currentGoal.owner_session_id || goal.ownership_access !== currentGoal.ownership_access) {
      setAckStage(false); setAckNotifications(false); saveRequest.current = null;
    }
  }, [goal]);
  const eligible = watches.filter((watch) => watch.goal_id === currentGoal.id && watch.goal_revision === currentGoal.revision
    && watch.state === "active" && watch.sources.length > 0 && watch.sources.every((source) => source.kind === "public_https_text"));
  const staleSelection = selected.some((id) => !eligible.some((watch) => watch.id === id));
  const policyReviewRequired = currentGoal.guardian_assessment_state === "goal_review_required"
    || Boolean(currentGoal.guardian_policy?.assessment_enabled && (currentGoal.guardian_policy.goal_revision !== currentGoal.revision
      || Date.parse(currentGoal.guardian_policy.review_due_at) <= Date.now()));

  const refreshPolicy = async () => {
    setBusy(true);
    const results = await Promise.allSettled([
      apiFetch(`${API_URL}/api/goals`).then(async (response) => {
        if (!response.ok) throw new Error("Goal metadata unavailable");
        const rows: unknown = await response.json();
        if (!Array.isArray(rows)) throw new Error("Goal metadata incomplete");
        const row = rows.find((item) => item.id === goal.id) as GoalInfo | undefined;
        if (!row || !Number.isInteger(row.revision) || !Number.isInteger(row.guardian_policy_revision)
          || typeof row.title !== "string" || typeof row.status !== "string"
          || (row.guardian_policy !== null && !isGuardianGoalPolicy(row.guardian_policy))
          || !["disabled", "enabled", "goal_review_required"].includes(row.guardian_assessment_state ?? "")) throw new Error("Goal metadata incomplete");
        return row;
      }),
      fetchGuardianPolicyWatches(),
    ]);
    if (!mounted.current) return;
    const [goalResult, watchResult] = results;
    if (goalResult.status === "fulfilled") setCurrentGoal(goalResult.value);
    if (watchResult.status === "fulfilled") setWatches(watchResult.value);
    if (results.every((result) => result.status === "fulfilled")) {
      setMetadataError(""); setRequiresRefresh(false); setError(""); saveRequest.current = null;
      setNotice("Current revisions refreshed. Review your retained selections and consent before saving; watches were not renewed.");
    } else setMetadataError("Metadata refresh is incomplete. Last-known Goal and watch selections are retained.");
    setBusy(false);
  };

  const save = async () => {
    if (requiresRefresh || busy || currentGoal.ownership_access === "recovered_read_only") return;
    setError(""); setNotice("");
    const budget = currentGoal.admission_budget;
    const grantId = budget?.grant_id ?? currentGoal.guardian_policy?.grant_id;
    const due = Date.parse(reviewDue);
    if (!Number.isInteger(currentGoal.revision) || !currentGoal.owner_session_id || !grantId) {
      setError("A saved Goal with its current original Root and reviewed finite grant is required."); return;
    }
    if (enabled && (!currentGoal.proactive_enabled || !budget?.reviewed_grant || !budget.period_expires_at
      || Date.parse(budget.period_expires_at) <= Date.now())) {
      setError("Review the current finite Goal grant before enabling assessments."); return;
    }
    if (selected.length < 1 || selected.length > 3 || (enabled && staleSelection)) {
      setError("Select one to three active public watches already bound to this Goal revision. Review/rebind stale watches in Work first."); return;
    }
    if (!Number.isFinite(due) || due <= Date.now() || due > Date.now() + 7 * 86400000
      || (enabled && budget?.period_expires_at && due > Date.parse(budget.period_expires_at))) {
      setError("Policy review must be in the future, within seven days and the current Goal grant. The server also caps it by Root expiry."); return;
    }
    if (!Number.isInteger(assessmentCap) || assessmentCap < 1 || assessmentCap > 4
      || !Number.isInteger(planCap) || planCap < 0 || planCap > 2
      || !Number.isInteger(notificationCap) || notificationCap < 0 || notificationCap > 2) {
      setError("Use whole-number daily limits: assessments 1–4, advisory proposals and notifications 0–2."); return;
    }
    if (autoStage && (!ackStage || planCap === 0)) { setError("Separately acknowledge advisory staging and set a nonzero proposal limit."); return; }
    if (notificationCap > 0 && !ackNotifications) { setError("Separately acknowledge optional notifications before setting a nonzero limit."); return; }
    const policy: GuardianGoalPolicy = {
      schema_version: "seraph.guardian.policy.v1", assessment_enabled: enabled, auto_stage_plan: autoStage,
      confirmed_at: new Date().toISOString(), review_due_at: new Date(due).toISOString(),
      grant_id: grantId, original_root_id: currentGoal.owner_session_id, goal_revision: currentGoal.revision!,
      source_watch_ids: selected, max_assessments_per_utc_day: assessmentCap, max_plan_proposals_per_utc_day: planCap,
      max_notification_per_utc_day: notificationCap, minimum_gap_seconds: 1800,
    };
    const signature = JSON.stringify({ ...policy, confirmed_at: undefined, policy_revision: currentGoal.guardian_policy_revision ?? 0, ackStage, ackNotifications });
    if (saveRequest.current?.signature !== signature) saveRequest.current = { signature, key: createGuardianUuid(), policy };
    setBusy(true);
    try {
      const result = await saveGuardianPolicy(currentGoal.id, {
        expected_goal_revision: currentGoal.revision!, expected_policy_revision: currentGoal.guardian_policy_revision ?? 0,
        idempotency_key: saveRequest.current!.key, policy: saveRequest.current!.policy, acknowledge_auto_stage_plan: ackStage, acknowledge_notifications: ackNotifications,
      });
      if (!mounted.current) return;
      setCurrentGoal((previous) => ({ ...previous, revision: result.goal_revision, guardian_policy_revision: result.guardian_policy_revision,
        guardian_policy: result.guardian_policy, guardian_assessment_state: result.assessment_state as GoalInfo["guardian_assessment_state"] }));
      setReviewDue(result.guardian_policy.review_due_at); setAckStage(false); setAckNotifications(false); saveRequest.current = null;
      setNotice("Assessment policy saved. Only future verified public changes are eligible; saving does not dispatch work or renew watches.");
    } catch (failure) {
      if (!mounted.current) return;
      if (failure instanceof GuardianInboxApiError && failure.status === 409) {
        setRequiresRefresh(true); setError("Goal or policy authority changed. Your draft is retained. Refresh policy metadata and review before explicitly saving again.");
      } else {
        setError(failure instanceof Error ? failure.message : "Policy save outcome is unknown. Refresh to inspect its current revision.");
        if (!(failure instanceof GuardianInboxApiError) || failure.status >= 500) setRequiresRefresh(true);
      }
    } finally { if (mounted.current) setBusy(false); }
  };

  return <section aria-label="Cited opportunity assessment policy" className="border border-white/10 rounded-md p-3 space-y-2 mt-2">
    <div className="cockpit-card-title">Cited opportunity assessments</div>
    <p className="text-[10px] text-slate-400">Finite, public-watch model judgments. Inbox only by default; no execution or learning follows a score. Policy revision {currentGoal.guardian_policy_revision ?? 0}.</p>
    {policyReviewRequired ? <div role="status">Goal review required. Review/rebind watches, then explicitly save a current policy.</div> : null}
    {currentGoal.guardian_policy ? <div>Confirmed {currentGoal.guardian_policy.confirmed_at} · review due {currentGoal.guardian_policy.review_due_at}</div> : <div>Assessments disabled · no policy confirmed</div>}
    <div>Current confirmed policy: {currentGoal.guardian_assessment_state ?? "disabled"} · advisory auto-stage {currentGoal.guardian_policy?.auto_stage_plan ? "enabled" : "disabled"}.</div>
    <p className="text-[10px] text-slate-400">Controls below are your unsaved draft. Metadata refresh retains this draft; only Save assessment policy confirms changes.</p>
    {metadataError ? <div role="status">{metadataError}</div> : null}
    <label className="block"><input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} /> Enable bounded public opportunity assessments</label>
    <fieldset><legend>Select 1–3 current public source watches</legend>
      {eligible.map((watch) => <label className="block" key={watch.id}><input type="checkbox" aria-label={`Assessment watch ${watch.id}`} checked={selected.includes(watch.id)}
        onChange={(event) => setSelected((ids) => event.target.checked ? [...ids, watch.id] : ids.filter((id) => id !== watch.id))}
        disabled={!selected.includes(watch.id) && selected.length >= 3} />{watch.sources.map((source) => source.label || source.source_key).join(", ")} · {watch.id}</label>)}
      {eligible.length === 0 ? <div>No active public watch binds the current Goal revision. Use existing Work watch review first.</div> : null}
      {staleSelection ? <div role="status">Retained watch selections are stale or unavailable: {selected.filter((id) => !eligible.some((watch) => watch.id === id)).join(", ")}. No automatic rebind.</div> : null}
      {selected.filter((id) => !eligible.some((watch) => watch.id === id)).map((id) => <button type="button" key={id} onClick={() => setSelected((ids) => ids.filter((value) => value !== id))}>Remove retained watch {id}</button>)}
    </fieldset>
    <label className="block">Review due (UTC)<input aria-label="Assessment review due UTC" type="datetime-local" value={reviewDue.slice(0, 16)} onChange={(event) => setReviewDue(`${event.target.value}:00Z`)} /></label>
    <label className="block">Assessments per UTC day<input aria-label="Assessments per UTC day" type="number" min={1} max={4} value={assessmentCap} onChange={(event) => setAssessmentCap(Number(event.target.value))} /></label>
    <label className="block">Advisory proposals per UTC day<input aria-label="Advisory proposals per UTC day" type="number" min={0} max={2} value={planCap} onChange={(event) => setPlanCap(Number(event.target.value))} /></label>
    <label className="block"><input type="checkbox" checked={autoStage} onChange={(event) => setAutoStage(event.target.checked)} /> Enable silent non-executable Triage plan staging</label>
    <p className="text-[10px] text-slate-400">Separately consented staging may silently create a non-executable Triage plan. Saving this permission never accepts or executes a plan.</p>
    <label className="block"><input type="checkbox" checked={ackStage} onChange={(event) => setAckStage(event.target.checked)} /> I separately acknowledge advisory staging never accepts or executes a plan</label>
    <label className="block">Optional notifications per UTC day<input aria-label="Opportunity notifications per UTC day" type="number" min={0} max={2} value={notificationCap} onChange={(event) => setNotificationCap(Number(event.target.value))} /></label>
    <label className="block"><input type="checkbox" checked={ackNotifications} onChange={(event) => setAckNotifications(event.target.checked)} /> I separately acknowledge optional notifications and quiet-hour limits</label>
    <div>Minimum assessment gap: 30 minutes. Root and Goal expiry remain authoritative.</div>
    {error ? <div role="alert">{error}</div> : null}{notice ? <div role="status">{notice}</div> : null}
    <button type="button" onClick={() => void save()} disabled={busy || requiresRefresh || currentGoal.ownership_access === "recovered_read_only"}>Save assessment policy</button>
    <button type="button" onClick={() => void refreshPolicy()} disabled={busy}>Refresh policy metadata</button>
  </section>;
}

function boundedReviewDue(goal: GoalInfo): string {
  const expiry = goal.admission_budget?.period_expires_at ? Date.parse(goal.admission_budget.period_expires_at) : Infinity;
  return new Date(Math.min(Date.now() + 7 * 86400000, Number.isFinite(expiry) ? expiry : Infinity)).toISOString();
}
