import { useEffect, useRef, useState } from "react";
import { setupRequest, setupRecovery, validSetupSource, type SetupProgress, type SetupResult, type SetupTask } from "../../lib/firstResultSetup";
import { useChatStore } from "../../stores/chatStore";
import "./FirstResultSetup.css";

interface Props {
  onOpenTask?: (taskId: string) => void;
  onOpenSection: (section: "goals" | "connections" | "work") => void;
}

function newProgress(): SetupProgress {
  const journeyId = globalThis.crypto?.randomUUID?.() ?? `setup-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  return { starter: "local_snapshot", step: "choose", journey_id: journeyId, title: "My first verified goal", source: "" };
}

export function FirstResultSetup({ onOpenTask, onOpenSection }: Props) {
  const onboardingCompleted = useChatStore((state) => state.onboardingCompleted);
  const [progress, setProgress] = useState<SetupProgress>(newProgress);
  const [task, setTask] = useState<SetupTask | null>(null);
  const [result, setResult] = useState<SetupResult | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [dismissed, setDismissed] = useState(false);
  const current = useRef(progress);
  const draftEdited = useRef(false);
  const draftWrite = useRef<Promise<unknown> | null>(null);
  current.current = progress;

  const hydrate = (saved: SetupProgress | null) => {
    if (!saved) return;
    const next = draftEdited.current && !saved.goal_id
      ? { ...saved, starter: current.current.starter, title: current.current.title, source: current.current.source }
      : saved;
    setProgress(next); current.current = next;
  };

  const editDraft = (next: SetupProgress) => {
    draftEdited.current = true;
    setProgress(next);
  };

  const persist = async (next: SetupProgress) => {
    setProgress(next); // A failed metadata write must not discard the draft.
    current.current = next;
    await setupRequest("/user/onboarding/progress", "PUT", next);
  };

  useEffect(() => {
    if (onboardingCompleted !== false) return;
    let active = true;
    void setupRequest<{ progress: SetupProgress | null }>("/user/onboarding/progress").then((payload) => {
      if (!active) return;
      hydrate(payload.progress);
      setLoaded(true);
    }).catch(() => { if (active) { setError("Saved setup metadata is unavailable. Retry loading before creating work; your inputs remain editable."); setLoaded(false); } });
    return () => { active = false; };
  }, [onboardingCompleted]);

  // Save only bounded drafts, after typing settles. No keys or source content.
  useEffect(() => {
    if (!loaded || progress.goal_id || busy || progress.step === "result_opened") return;
    if (progress.source && !validSetupSource(progress.source)) return;
    const timer = window.setTimeout(() => {
      draftWrite.current = setupRequest("/user/onboarding/progress", "PUT", progress).catch(() => setError("Draft could not be saved. Keep this page open and retry; your inputs are retained."));
    }, 750);
    return () => window.clearTimeout(timer);
  }, [loaded, progress, busy]);

  const action = async (run: () => Promise<void>) => {
    if (busy) return;
    setBusy(true); setError("");
    try { await draftWrite.current; await run(); } catch (cause) { setError(cause instanceof Error ? cause.message : "Setup request failed."); }
    finally { setBusy(false); }
  };

  const readTask = async (id: string) => {
    const detail = await setupRequest<{ task: SetupTask }>(`/work-board/tasks/${encodeURIComponent(id)}`);
    setTask(detail.task);
    return detail.task;
  };

  const refresh = async () => {
    const next = current.current;
    if (next.task_id) await readTask(next.task_id);
    else if (next.watch_task_id) await readTask(next.watch_task_id);
  };

  useEffect(() => {
    if (loaded && (progress.task_id || progress.watch_task_id)) void action(refresh);
  }, [loaded, progress.task_id, progress.watch_task_id]);

  const createTask = async (next: SetupProgress, capability: string, input: object, watch = false) => {
    const suffix = watch ? "watch" : "snapshot";
    const reservation = await setupRequest<{ artifact_id: string }>("/work-board/input-artifacts", "POST", {
      schema_version: 1, capability_id: capability, goal_id: next.goal_id, goal_revision: next.goal_revision,
      input, idempotency_key: `first-result:${next.journey_id}:${suffix}:input`,
    });
    const created = await setupRequest<{ task: SetupTask }>("/work-board/tasks", "POST", {
      title: watch ? "Observe my public source baseline" : "Write my first goal snapshot",
      body: "First-result setup. Explicit no-learning; inspect the verified result before completing setup.",
      goal_id: next.goal_id, goal_revision: next.goal_revision, status: "todo", capability_id: capability,
      input_artifact_id: reservation.artifact_id, priority: 80, requires_review: false,
      idempotency_key: `first-result:${next.journey_id}:${suffix}:task`,
    });
    const taskId = created.task.task_id;
    await persist({ ...next, step: "task_saved", ...(watch ? { watch_task_id: taskId } : { task_id: taskId }), input_artifact_id: reservation.artifact_id });
    setTask(created.task);
    setStatus("Typed task queued in Work. The managed scheduler verifies authority before admission and execution; refresh to inspect the actual result.");
  };

  const prepare = async () => {
    let next = current.current;
    if (!next.title.trim()) throw new Error("Enter a goal title.");
    if (next.starter === "public_watch" && !validSetupSource(next.source)) throw new Error("Use a public HTTPS text URL without credentials, query parameters, or fragments.");
    if (!next.goal_id || (next.starter === "public_watch" && !next.watch_id)) {
      const prepared = await setupRequest<{ goal: { id: string; revision: number }; watch: { id: string; plan_revision: number } | null }>("/user/onboarding/starter", "POST", next);
      next = { ...next, goal_id: prepared.goal.id, goal_revision: prepared.goal.revision, step: prepared.watch ? "watch_saved" : "goal_saved",
        ...(prepared.watch ? { watch_id: prepared.watch.id, plan_revision: prepared.watch.plan_revision } : {}) };
      await persist(next);
    }
    if (next.starter === "public_watch" && !next.watch_task_id) {
      await createTask(next, "guardian.research-watch.v1", { watch_id: next.watch_id, expected_plan_revision: next.plan_revision }, true);
    } else if (!next.task_id) {
      if (next.watch_task_id) {
        const watched = await readTask(next.watch_task_id);
        if (watched.status !== "done" || watched.verification_status !== "passed") throw new Error("Finish the verified public source observation before creating the local result.");
        const watch = await setupRequest<{ state: string; plan_revision: number }>(`/capabilities/source-watches/${encodeURIComponent(next.watch_id!)}`);
        if (watch.state !== "paused") await setupRequest(`/capabilities/source-watches/${encodeURIComponent(next.watch_id!)}`, "PATCH", { expected_plan_revision: watch.plan_revision, state: "paused" });
      }
      await createTask(next, "workflow.goal-snapshot-to-file", { file_path: `artifacts/first-result/${next.journey_id}.md` });
    }
  };

  const openResult = async () => {
    const opened = await setupRequest<SetupResult>(`/user/onboarding/result/${encodeURIComponent(current.current.task_id!)}/open`, "POST");
    setResult(opened); setProgress(opened.progress); current.current = opened.progress;
    setStatus("First result verified and opened. Setup is complete; no memory learning was applied.");
    useChatStore.getState().setOnboardingCompleted(true);
  };

  if ((onboardingCompleted !== false && !result) || dismissed) return null;
  const hasWork = Boolean(progress.goal_id);
  return (
    <section className="cockpit-outcome-card first-result-setup" aria-labelledby="first-result-title" aria-busy={busy}>
      <h3 id="first-result-title">Your first verified result</h3>
      <p>Choose one useful starter. Your existing goals, conversation, and workspace stay available.</p>
      <ol aria-label="Setup progress"><li>Choose a starter</li><li>Review permissions and bounds</li><li>Admit a typed task</li><li>Open its verified result</li></ol>
      <p>Saved step: {progress.step.replace(/_/g, " ")}</p>
      {!hasWork && <fieldset disabled={busy}><legend>Starter journey</legend>
        <label><input type="radio" name="first-result-starter" checked={progress.starter === "local_snapshot"} onChange={() => editDraft({ ...progress, starter: "local_snapshot" })} /> Local goal snapshot · no credentials</label>
        <label><input type="radio" name="first-result-starter" checked={progress.starter === "public_watch"} onChange={() => editDraft({ ...progress, starter: "public_watch" })} /> Public source baseline + local snapshot · no credentials</label>
        <label>Goal title<input value={progress.title} onChange={(event) => editDraft({ ...progress, title: event.target.value })} maxLength={200} /></label>
        {progress.starter === "public_watch" && <label>Public HTTPS text URL<input type="url" value={progress.source} onChange={(event) => editDraft({ ...progress, source: event.target.value })} placeholder="https://example.org/updates.txt" /></label>}
        <button type="button" disabled={!loaded} onClick={() => void action(async () => { await persist({ ...current.current, step: "preview" }); })}>Review starter bounds</button>
      </fieldset>}
      {(progress.step !== "choose" || hasWork) && <div className="first-result-preview">
        <h4>Permission and cost preview</h4>
        <p>Model spend: $0. No provider key or cloud model consent is needed. Both starters use deterministic local processing.</p>
        <p>Local read: your owner-bound goals. Local write: one snapshot under artifacts/first-result, followed by independent digest readback. Memory: explicit no_learning.</p>
        {progress.starter === "public_watch" && <p>Source: {progress.source}. One public HTTPS text read, at most 256 KiB. Consent expires in one hour; one job at a time, one attempt, 120 seconds, zero notifications. The first scan initializes a baseline; it is not a research report or material-change dossier. The watch is paused after observation. Future changed-source local dossiers require approval each run; no external writes are granted.</p>}
        <p>Priority 80 in the existing Work lane. Cancellation and uncertain-outcome recovery stay in Work; no automatic replay.</p>
        <p>Queuing this starter authorizes its bounded execution through the managed scheduler. If it remains Todo or Ready, inspect Work and the managed runtime status; start the managed Seraph stack to restore scheduler execution.</p>
        {!progress.task_id && !progress.watch_task_id && <button type="button" disabled={busy || !loaded} onClick={() => void action(prepare)}>Create and queue bounded starter</button>}
      </div>}
      {task && <div><p>Task: {task.task_id} · {task.status} · readback {task.readback_status ?? "pending"} · verification {task.verification_status ?? "pending"}</p>
        {task.block_reason && <p>{task.block_reason} · {setupRecovery(task.block_reason)}</p>}
        <button type="button" disabled={busy} onClick={() => void action(refresh)}>Refresh task result</button>
        <button type="button" onClick={() => onOpenTask?.(task.task_id)}>Open task in Work</button>
        {progress.watch_task_id && !progress.task_id && task.status === "done" && <button type="button" disabled={busy} onClick={() => void action(prepare)}>Pause watch and create local result</button>}
        {progress.task_id && task.status === "done" && !result && <button type="button" disabled={busy} onClick={() => void action(openResult)}>Open verified first result</button>}
      </div>}
      {result && <div aria-label="Verified first result"><p>Artifact: {result.file_path} · readback {result.readback_id} · SHA-256 {result.content_sha256} · {result.memory_status}</p>
        {result.observation && <p>Verified public observation: {result.observation.status}. {result.observation.sources.map((source) => source.target).join(", ")} · baseline SHA-256 {result.observation.baselines.map((baseline) => baseline.sha256).join(", ")} · readback {result.observation.readback_id} · {result.observation.memory_status} · schedule {result.observation.schedule_state}</p>}
        <pre>{result.content}</pre>
      </div>}
      {error && <div role="alert"><p>{error}</p><p>{setupRecovery(error)}</p></div>}
      {error && loaded && progress.goal_id && <button type="button" disabled={busy} onClick={() => void action(async () => { await persist(current.current); if (current.current.task_id || current.current.watch_task_id) await refresh(); else await prepare(); })}>Save and resume this step</button>}
      {status && <p role="status">{status}</p>}
      {!loaded && <button type="button" disabled={busy} onClick={() => void action(async () => { const payload = await setupRequest<{ progress: SetupProgress | null }>("/user/onboarding/progress"); hydrate(payload.progress); setLoaded(true); })}>Retry saved setup</button>}
      <div className="source-watch-actions"><button type="button" onClick={() => onOpenSection("goals")}>Review goal permissions</button><button type="button" onClick={() => onOpenSection("connections")}>Review source connections</button>
        <button type="button" onClick={() => useChatStore.getState().setSettingsPanelOpen(true)}>Open runtime settings</button>
        {!result && <button type="button" disabled={busy} onClick={() => void action(async () => { await setupRequest("/user/onboarding/skip", "POST"); useChatStore.getState().setOnboardingCompleted(true); setDismissed(true); })}>Skip setup</button>}
      </div>
      {!result && <p>Skipping closes setup. Any accepted task remains visible in Work, where you can cancel it.</p>}
    </section>
  );
}
