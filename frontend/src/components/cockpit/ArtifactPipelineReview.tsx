import { useEffect, useRef, useState } from "react";
import type { WorkBoardTask } from "../../types";
import { pipelineReport, pipelineRequest, pipelineStorageKey, readPipelineStorage, writePipelineStorage,
  type ArtifactPipeline, type PipelinePending, type PipelineStorage } from "../../lib/artifactPipeline";

interface Props {
  task: WorkBoardTask; ownerPrincipalId: string; ownerSessionId: string; metadataConfirmed: boolean;
  onRefresh: () => Promise<void>; onOpenTask: (task: string) => void;
}

export function ArtifactPipelineReview({ task, ownerPrincipalId, ownerSessionId, metadataConfirmed, onRefresh, onOpenTask }: Props) {
  const key = pipelineStorageKey(ownerPrincipalId, ownerSessionId, task.task_id);
  const [operation, setOperation] = useState<ArtifactPipeline | null>(null);
  const [retained, setRetained] = useState<PipelineStorage>({ schema_version: 1, operation_id: null, pending: null });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [storageReady, setStorageReady] = useState(false);
  const [replacement, setReplacement] = useState("");
  const [report, setReport] = useState<string | null>(null);
  const generation = useRef(0);
  const activeRequest = useRef<AbortController | null>(null);
  const eligible = metadataConfirmed && task.owner_principal_id === ownerPrincipalId && task.owner_session_id === ownerSessionId && task.ownership_access !== "recovered_read_only";
  const relevant = Boolean(task.pipeline_operation_id) || task.capability_id === "browser.public-task.v1";
  const refresh = async () => {
    const id = retained.operation_id ?? task.pipeline_operation_id;
    if (!id || !eligible) return;
    const current = generation.current;
    setBusy(true); setError(null);
    try {
      const value = await pipelineRequest(`/api/work-board/pipelines/${id}`);
      if (current === generation.current) setOperation(value);
    } catch (caught) { if (current === generation.current) setError(caught instanceof Error ? caught.message : "Pipeline readback unavailable."); }
    finally { if (current === generation.current) setBusy(false); }
  };
  useEffect(() => {
    const current = ++generation.current;
    activeRequest.current?.abort();
    setOperation(null); setReport(null); setError(null); setStorageReady(false); setBusy(false);
    if (!eligible || !relevant) return;
    try {
      const value = readPipelineStorage(key, task.task_id);
      const id = value.operation_id ?? task.pipeline_operation_id ?? null;
      const stored = { ...value, operation_id: id };
      writePipelineStorage(key, task.task_id, stored);
      setRetained(stored); setStorageReady(true);
      if (id) {
        const controller = new AbortController(); activeRequest.current = controller;
        setBusy(true);
        void pipelineRequest(`/api/work-board/pipelines/${id}`, undefined, controller.signal)
          .then((value) => { if (generation.current === current) setOperation(value); })
          .catch(() => { if (generation.current === current && !controller.signal.aborted) setError("Pipeline readback unavailable; the exact retained request remains available."); })
          .finally(() => { if (generation.current === current) setBusy(false); });
      }
    } catch { setError("Pipeline request storage is corrupt or unavailable. Actions are blocked until its exact receipt can be recovered."); }
    return () => { generation.current += 1; activeRequest.current?.abort(); };
  }, [key, task.task_id, task.pipeline_operation_id, eligible, relevant]);

  const submit = async (pending: PipelinePending, retry = false) => {
    if (!eligible || !storageReady || busy || (retained.pending && !retry)) return;
    const current = generation.current;
    setBusy(true); setError(null); setReport(null);
    const controller = new AbortController(); activeRequest.current = controller;
    try {
      const stored = { ...retained, pending };
      // Exact bounded request + synchronous storage readback precede POST.
      writePipelineStorage(key, task.task_id, stored); setRetained(stored);
      const value = await pipelineRequest(pending.path, pending, controller.signal);
      if (current !== generation.current) return;
      const completed: PipelineStorage = { schema_version: 1, operation_id: value.operation_id, pending: null };
      writePipelineStorage(key, task.task_id, completed);
      setRetained(completed); setOperation(value);
      await onRefresh();
    } catch (caught) {
      if (current === generation.current && !controller.signal.aborted) setError(caught instanceof Error ? caught.message : "Outcome uncertain; retry the exact retained request.");
    } finally { if (current === generation.current) setBusy(false); }
  };
  const mutate = (action: string, body: Record<string, unknown>) => operation && void submit({ path: `/api/work-board/pipelines/${operation.operation_id}/${action}`, body });
  const disabled = busy || !eligible || !storageReady || Boolean(retained.pending);
  if (!relevant) return null;
  return <section className="rounded border border-white/10 p-3" aria-label="Artifact pipeline">
    <div className="font-semibold">Public evidence pipeline</div>
    <p className="text-xs">Public browser → evidence dossier → local plain-text report. Quoted source data; deterministic CPU; no_learning. One 300-second operation, 2 attempts per leaf, 6 total, 180 seconds browser, 30 seconds CPU, 64 KiB outputs and 40 KiB quoted inputs, bounded by the current Goal.</p>
    {error && <p role="alert" className="mt-2 text-xs">{error}</p>}
    {retained.pending && <div className="mt-2"><p className="text-xs">A mutation has an uncertain outcome. Its exact request is retained for this operator session.</p><button disabled={busy || !eligible || !storageReady} onClick={() => void submit(retained.pending!, true)}>Retry exact pipeline request</button></div>}
    {!operation && !task.pipeline_operation_id && <button disabled={disabled || !task.input_artifact_id || !["todo", "triage"].includes(task.status)} onClick={() => void submit({ path: `/api/work-board/tasks/${task.task_id}/pipeline-preview`, body: { expected_revision: task.task_revision, source_input_artifact_id: task.input_artifact_id, idempotency_key: crypto.randomUUID() } })}>Preview evidence pipeline</button>}
    {operation && <>
      <p className="mt-2 break-all text-xs">Operation {operation.operation_id} · plan {operation.plan_version} · {operation.status} · {operation.deadline_at ? `original deadline ${operation.deadline_at}` : "awaiting review"}</p>
      <p className="break-all text-xs">Source {operation.source_scope.start_url} · hosts {operation.source_scope.allowed_hosts.join(", ")} · allowed paths {operation.source_scope.approved_url_prefixes.join(", ")}</p>
      <p className="break-all text-xs">Review digest {operation.digest}</p>
      {operation.authority_frozen && <p role="status" className="text-xs">Unfinished work is frozen because its Goal or source permission changed. Review a replacement source or a distinct finite operation; original completed output and unresolved liabilities remain preserved.</p>}
      <ol>{operation.steps.map((step) => <li key={step.task_id}><button onClick={() => onOpenTask(step.task_id)}>{step.capability_id}: {step.status}</button>{step.block_reason && <span> · {step.block_reason}</span>}</li>)}</ol>
      <div className="mt-2 flex flex-wrap gap-2">
        <button disabled={busy || !eligible} onClick={() => void refresh()}>Refresh pipeline</button>
        {(operation.status === "proposed" || operation.pending_revision) && <button disabled={disabled} onClick={() => mutate("accept", { expected_revision: operation.revision, expected_parent_revision: operation.parent_revision, expected_digest: operation.digest })}>Approve exact pipeline plan</button>}
        {operation.status === "accepted" && !operation.pending_revision && <button disabled={disabled} onClick={() => mutate("advance", { expected_revision: operation.revision })}>Materialize verified next input</button>}
        {operation.pending_revision && <button disabled={disabled} onClick={() => mutate("quiesce", { expected_revision: operation.revision })}>Cancel and verify unfinished work</button>}
        {operation.status === "accepted" && <button disabled={disabled || operation.steps[0]?.status !== "done" || operation.steps.slice(1).some((step) => ["done", "review"].includes(step.status))} onClick={() => mutate("reuse-preview", { expected_revision: operation.revision, expected_parent_revision: operation.steps[0].task_revision, idempotency_key: crypto.randomUUID() })}>Review fresh operation using verified source</button>}
        {operation.steps[2]?.status === "done" && <button disabled={busy || !eligible} onClick={() => { const current = generation.current; setBusy(true); void pipelineReport(operation.operation_id).then((text) => { if (current === generation.current) setReport(text); }).catch(() => { if (current === generation.current) setError("Verified report unavailable."); }).finally(() => { if (current === generation.current) setBusy(false); }); }}>Read verified local report</button>}
      </div>
      {operation.status === "accepted" && !operation.pending_revision && <div className="mt-2"><label>Replacement input artifact <input value={replacement} onChange={(event) => setReplacement(event.target.value)} /></label><button disabled={disabled || !/^[a-zA-Z0-9:-]{1,128}$/.test(replacement) || operation.steps.slice(1).some((step) => ["done", "review"].includes(step.status))} onClick={() => mutate("revision", { expected_revision: operation.revision, source_input_artifact_id: replacement, idempotency_key: crypto.randomUUID() })}>Freeze and review replacement source</button></div>}
      <p className="mt-2 text-xs">Open each ready input task and use its existing Ready action to queue execution. Refresh this operation, then materialize the independently verified next input. Revisions preserve the original deadline and attempt counters.</p>
    </>}
    {report !== null && <pre className="mt-3 whitespace-pre-wrap break-all" aria-label="Verified local evidence report">{report}</pre>}
  </section>;
}
