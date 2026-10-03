import { useCallback, useEffect, useRef, useState } from "react";
import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import type { WorkBoardTask } from "../../types";
import { EvidenceExecutionControls } from './EvidenceExecutionControls';

interface Claim {
  source_id: string;
  source_kind: string;
  source_digest: string;
  version: string;
  line_start: number;
  line_end: number;
  text: string;
  confidence: number | null;
  freshness: string;
  memory_id: string | null;
  model_context_allowed: boolean;
}
interface Packet {
  revision: number;
  digest: string | null;
  claims: Claim[];
  excluded_source_ids: string[];
  invalidated_count: number;
  blocked_sources: string[];
  allow_model_context: boolean;
}

const unavailable = "Evidence is unavailable or changed. Reload the task and review its source permissions.";
const digestPattern = /^[a-f0-9]{64}$/;
function validClaims(value: unknown): value is Claim[] {
  return Array.isArray(value) && value.length <= 16 && value.every((claim) =>
    claim && typeof claim === "object" && digestPattern.test(claim.source_id)
    && digestPattern.test(claim.source_digest) && typeof claim.source_kind === "string"
    && typeof claim.version === "string" && typeof claim.text === "string" && claim.text.length <= 1000
    && Number.isInteger(claim.line_start) && claim.line_start >= 1
    && Number.isInteger(claim.line_end) && claim.line_end >= claim.line_start
    && typeof claim.freshness === "string" && typeof claim.model_context_allowed === "boolean"
    && (claim.memory_id === null || typeof claim.memory_id === "string")
    && (claim.confidence === null || (typeof claim.confidence === "number" && Number.isFinite(claim.confidence))));
}
function validatedPacket(value: unknown): Packet {
  if (!value || typeof value !== "object") throw new Error(unavailable);
  const packet = value as Packet;
  if (!Number.isInteger(packet.revision) || packet.revision < 0
    || !(packet.digest === null ? packet.revision === 0 : typeof packet.digest === "string" && digestPattern.test(packet.digest))
    || !validClaims(packet.claims) || !Array.isArray(packet.excluded_source_ids)
    || packet.excluded_source_ids.length > 32 || !packet.excluded_source_ids.every((id) => digestPattern.test(id))
    || !Number.isInteger(packet.invalidated_count) || packet.invalidated_count < 0
    || !Array.isArray(packet.blocked_sources) || packet.blocked_sources.length > 32
    || !packet.blocked_sources.every((reason) => typeof reason === "string" && reason.length <= 256)
    || typeof packet.allow_model_context !== "boolean") throw new Error(unavailable);
  return packet;
}

export function TaskEvidencePanel({ task, ownerSessionId }: {
  task: WorkBoardTask; ownerSessionId?: string | null;
}) {
  const [packet, setPacket] = useState<Packet | null>(null);
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [source, setSource] = useState<Claim[] | null>(null);
  const [correction, setCorrection] = useState("");
  const [correcting, setCorrecting] = useState<Claim | null>(null);
  const generation = useRef(0);
  const scope = `${task.task_id}:${task.task_revision}:${ownerSessionId ?? ""}`;
  const currentScope = useRef(scope);
  currentScope.current = scope;
  const recovered = Boolean(ownerSessionId && task.owner_session_id !== ownerSessionId);
  const endpoint = `${API_URL}/api/work-board/tasks/${encodeURIComponent(task.task_id)}/evidence`;
  const canEdit = !recovered && task.status !== "running" && task.status !== "archived";

  const call = useCallback(async (url: string, method = "GET", body?: unknown) => {
    const response = await apiFetch(url, { method, ...(body !== undefined ? {
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    } : {}) });
    if (!response.ok) {
      throw new Error(unavailable);
    }
    return await response.json();
  }, []);

  useEffect(() => {
    const token = ++generation.current;
    setPacket(null); setSource(null); setCorrecting(null); setError(null);
    setBusy(false);
    setQuery("");
    void call(endpoint).then((value: unknown) => {
      const result = validatedPacket(value);
      if (token === generation.current) setPacket(result);
    }).catch((err: Error) => { if (token === generation.current) setError(err.message); });
    return () => { generation.current++; };
  }, [call, endpoint, scope, recovered]);

  async function action(operation: () => Promise<void>) {
    const actionScope = currentScope.current;
    setBusy(true); setError(null);
    try { await operation(); }
    catch (err) { if (currentScope.current === actionScope) setError((err as Error).message); }
    finally { if (currentScope.current === actionScope) setBusy(false); }
  }

  async function refresh(exclude?: string) {
    const actionScope = scope;
    const result = validatedPacket(await call(endpoint, exclude ? "PATCH" : "POST", {
      expected_task_revision: task.task_revision,
      expected_packet_revision: packet?.revision ?? 0,
      ...(query ? { query } : {}),
      ...(exclude ? { excluded_source_ids: [exclude] } : {}),
    }));
    if (currentScope.current === actionScope) { setPacket(result); setSource(null); }
  }

  async function correct() {
    if (!correcting?.memory_id || !correction.trim()) return;
    const actionScope = scope;
    await call(`${API_URL}/api/memory/corrections`, "POST", {
      content: correction.trim(), corrects_memory_id: correcting.memory_id,
      metadata: { goal_id: task.goal_id }, reason: "Operator correction from task evidence inspector",
    });
    if (currentScope.current === actionScope) { setCorrecting(null); setCorrection(""); await refresh(); }
  }

  return <section className="rounded border border-white/10 p-3" aria-label="Task evidence working set">
    <div className="font-semibold">Task evidence</div>
    <>
      {recovered && <p>Recovered evidence is read only. Create and review current work before adopting it.</p>}
      <p className="text-[10px] opacity-75">Selected goal only · lexical retrieval (embeddings not used) · no automatic learning</p>
      <label className="block">Evidence query<input aria-label="Evidence query" value={query} maxLength={200}
        className="ml-2 bg-black/20" onChange={(event) => setQuery(event.target.value)} /></label>
      <p className="text-[10px]">Private Mail and Calendar evidence stays local. Exclude it before using Specify or Decompose.</p>
      <button type="button" disabled={!canEdit || busy} onClick={() => void action(() => refresh())}>
        {busy ? "Updating evidence…" : "Refresh evidence"}</button>
      {error && <p role="alert">{error}</p>}
      {packet && <>
        <p>Packet revision {packet.revision} · {packet.claims.length} source spans</p>
        {packet.digest && <details><summary>Packet digest</summary><code>{packet.digest}</code></details>}
        <EvidenceExecutionControls endpoint={endpoint} taskId={task.task_id} taskRevision={task.task_revision}
          ownerSessionId={ownerSessionId} canEdit={canEdit} packetRevision={packet.revision} packetDigest={packet.digest} />
        {packet.invalidated_count > 0 && <p role="status">{packet.invalidated_count} stale or revoked spans removed. Refresh before task use.</p>}
        {packet.blocked_sources.length > 0 && <details><summary>Blocked sources</summary>
          {packet.blocked_sources.map((reason) => <p key={reason}>{reason}</p>)}</details>}
        {packet.excluded_source_ids.length > 0 && <p>{packet.excluded_source_ids.length} sources excluded from this task</p>}
        <p>{packet.allow_model_context ? "This reviewed packet is adopted for task model context." : "Local inspection only. Review these spans before adopting this exact packet."}</p>
        {packet.revision > 0 && packet.digest && <button type="button"
          disabled={!canEdit || busy || packet.invalidated_count > 0 || packet.claims.length === 0}
          onClick={() => void action(async () => {
            const actionScope = scope;
            const result = validatedPacket(await call(`${endpoint}/adoption`, "POST", {
              expected_task_revision: task.task_revision,
              expected_packet_revision: packet.revision,
              expected_packet_digest: packet.digest,
              allow_model_context: !packet.allow_model_context,
            }));
            if (currentScope.current === actionScope) setPacket(result);
          })}>{packet.allow_model_context ? "Reset packet adoption" : "Adopt reviewed packet for task model context"}</button>}
        {packet.claims.map((claim) => <article className="mt-2 rounded bg-black/20 p-2" key={`${claim.source_id}:${claim.line_start}`}>
          <p>{claim.text}</p>
          <p className="text-[10px]">{claim.source_kind} · lines {claim.line_start}–{claim.line_end} · {claim.freshness} · confidence {claim.confidence ?? "unknown"}</p>
          <details><summary>Source version and digest</summary><p>{claim.version}</p><code>{claim.source_digest}</code></details>
          {!claim.model_context_allowed && <p>Local inspection only; source-purpose model consent required</p>}
          <button type="button" disabled={busy} onClick={() => void action(async () => {
            const actionScope = scope;
            const result = await call(`${endpoint}/sources/${claim.source_id}`);
            if (!validClaims(result?.claims)) throw new Error(unavailable);
            if (currentScope.current === actionScope) setSource(result.claims);
          })}>Open source span</button>{" "}
          <button type="button" disabled={!canEdit || busy} onClick={() => void action(() => refresh(claim.source_id))}>Exclude source</button>
          {claim.memory_id && <>
            {" "}<button type="button" disabled={!canEdit || busy} onClick={() => { setCorrecting(claim); setCorrection(claim.text); }}>Correct canonical memory</button>
            {" "}<button type="button" disabled={!canEdit || busy} onClick={() => void action(async () => {
              await call(`${API_URL}/api/memory/${encodeURIComponent(claim.memory_id!)}/forget`, "POST", {
                mode: "archive", reason: "Operator forget from task evidence inspector",
              }); await refresh();
            })}>Forget canonical memory</button>
          </>}
        </article>)}
        {packet.claims.length === 0 && <p>No matching authorized evidence. Unsupported facts remain unknown.</p>}
      </>}
      {source && <div role="region" aria-label="Authorized source spans">
        {source.map((claim) => <p key={claim.line_start}>Line {claim.line_start}: {claim.text}</p>)}
        <button type="button" onClick={() => setSource(null)}>Close source</button>
      </div>}
      {correcting && <div><label>Canonical memory correction<textarea aria-label="Canonical memory correction"
        value={correction} onChange={(event) => setCorrection(event.target.value)} maxLength={2000} /></label>
        <button type="button" disabled={busy || !correction.trim()} onClick={() => void action(correct)}>Save correction</button>
        <button type="button" onClick={() => setCorrecting(null)}>Cancel correction</button></div>}
    </>
  </section>;
}
