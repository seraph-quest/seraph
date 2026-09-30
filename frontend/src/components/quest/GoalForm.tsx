import { useState } from "react";
import { GoalUpdateError, useQuestStore } from "../../stores/questStore";
import type {
  GoalAdmissionBudget,
  GoalCriterionVerifier,
  GoalInfo,
  GoalSuccessCriterion,
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
