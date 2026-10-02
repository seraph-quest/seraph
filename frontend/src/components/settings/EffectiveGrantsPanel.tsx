import { useCallback, useEffect, useRef, useState } from "react";
import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";

type Grant = {
  grant_id: string; kind: string; capability_id?: string; record_id: string; boundary: string;
  purpose: string; source: string; destination: string; state: string;
  revision: number; expires_at: string | null; origin: string;
  stored_state?: string; blocked_reason?: string | null;
  affected_jobs: { job_id: string; state: string; kind: string }[];
  controls: string[]; limits: Record<string, unknown>;
};
type Inventory = { grants: Grant[]; unavailable: string[]; truncated: string[]; snapshot_digest: string };
type Pending = { grant_id: string; expected_revision: number; idempotency_key: string };
let requestSequence = 0;
function requestKey(): string {
  try {
    if (typeof globalThis.crypto?.randomUUID === "function") return globalThis.crypto.randomUUID();
    if (typeof globalThis.crypto?.getRandomValues === "function") {
      return Array.from(globalThis.crypto.getRandomValues(new Uint8Array(16)), value => value.toString(16).padStart(2, "0")).join("");
    }
  } catch { /* Gesture key only: server owner and revision remain authority. */ }
  return `grant-${Date.now().toString(36)}-${++requestSequence}-${Math.random().toString(36).slice(2)}`;
}

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}
function text(value: unknown): value is string { return typeof value === "string" && value.length <= 2048; }
function strings(value: unknown, max = 100): value is string[] { return Array.isArray(value) && value.length <= max && value.every(text); }
function validInventory(value: unknown): value is Inventory {
  if (!object(value) || !strings(value.unavailable) || !strings(value.truncated) || !text(value.snapshot_digest) || !Array.isArray(value.grants) || value.grants.length > 2048) return false;
  return value.grants.every(grant => object(grant)
    && ["grant_id", "kind", "record_id", "boundary", "purpose", "source", "destination", "state", "origin"].every(key => text(grant[key]))
    && Number.isSafeInteger(grant.revision) && Number(grant.revision) >= 0
    && (grant.expires_at === null || text(grant.expires_at))
    && (grant.capability_id === undefined || text(grant.capability_id))
    && (grant.stored_state === undefined || text(grant.stored_state))
    && (grant.blocked_reason == null || text(grant.blocked_reason))
    && strings(grant.controls, 4) && grant.controls.every(control => ["revoke", "deny", "host_local_reset"].includes(control))
    && object(grant.limits) && Object.keys(grant.limits).length <= 64
    && Object.entries(grant.limits).every(([name, item]) => text(name) && (item === null || text(item) || typeof item === "boolean" || (typeof item === "number" && Number.isFinite(item))))
    && Array.isArray(grant.affected_jobs) && grant.affected_jobs.length <= 100
    && grant.affected_jobs.every(job => object(job) && text(job.job_id) && text(job.state) && text(job.kind)));
}

async function request(path: string, init?: RequestInit) {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), 15_000);
  try {
    const response = await apiFetch(`${API_URL}/api/extensions/effective-grants${path}`, { ...init, signal: controller.signal });
    const value = await response.json();
    if (!response.ok) throw new Error(`Authority readback failed (${response.status}). Refresh the owning adapter before retrying.`);
    if (path === "" ? !validInventory(value) : !object(value) || !["local_revocation_confirmed", "partial_failure", "already_consumed"].includes(String(value.status)) || !validInventory(value.readback) || (value.status === "partial_failure" && !text(value.local_state))) {
      throw new Error("Authority metadata is incomplete. Last confirmed state is retained; refresh before using controls.");
    }
    return value;
  } finally { window.clearTimeout(timer); }
}

export function EffectiveGrantsPanel({ sessionId }: { sessionId: string | null }) {
  const [inventory, setInventory] = useState<Inventory | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [pending, setPending] = useState<Pending | null>(null);
  const generation = useRef(0);
  const load = useCallback(async () => {
    const version = ++generation.current;
    setLoading(true);
    try {
      const value = await request("") as Inventory;
      if (version === generation.current) { setInventory(value); setError(null); }
    } catch (e) {
      if (version === generation.current) setError(e instanceof Error ? e.message : "Authority metadata unavailable");
    } finally { if (version === generation.current) setLoading(false); }
  }, []);
  useEffect(() => {
    setInventory(null); setPending(null); setStatus(null); setError(null);
    void load();
    return () => { generation.current += 1; };
  }, [load, sessionId]);
  async function revoke(grant: Grant, retry?: Pending) {
    const version = generation.current;
    const body = retry ?? { grant_id: grant.grant_id, expected_revision: grant.revision, idempotency_key: requestKey() };
    setPending(body); setLoading(true);
    try {
      const result = await request("/revoke", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (version !== generation.current) return;
      setInventory(result.readback); setError(null);
      if (result.status === "already_consumed") {
        setStatus("The one-action approval was already consumed. Inspect its effect and readback; revocation cannot undo that action."); setPending(null);
      } else if (result.status === "partial_failure") {
        setStatus(`Local state: ${result.local_state}. External cleanup is unconfirmed. Retry this exact request and read adapter metadata.`);
      } else {
        setStatus("Local revocation confirmed. External account access was not universally revoked. Already contacted work may need reconciliation.");
        setPending(null);
      }
    } catch (e) {
      if (version === generation.current) setError(e instanceof Error ? e.message : "Revocation readback unavailable; retry the exact request");
    } finally { if (version === generation.current) setLoading(false); }
  }
  return <section aria-label="Effective permission grants" className="cockpit-panel">
    <h2>Effective permission grants</h2>
    <p>Current login scope. Observation, inference transfer and external changes require separate authority. Connected credentials and recovered history grant no permission.</p>
    <button type="button" disabled={loading} onClick={() => void load()}>Refresh grants</button>
    {error && <p role="alert">{error} {inventory && "Last confirmed view is stale and cannot authorize work."}</p>}
    {status && <p role="status">{status}</p>}
    {inventory?.unavailable.length ? <p role="alert">Unavailable: {inventory.unavailable.join(", ")}. Owner policy must be checked before dispatch.</p> : null}
    {inventory?.truncated.length ? <p role="status">Bounded inventory; inspect owning adapter for remaining records.</p> : null}
    {!inventory && loading && <p role="status">Loading grants…</p>}
    {inventory && !inventory.grants.length && <p>No grants in this login scope.</p>}
    {inventory?.grants.map(grant => <article key={grant.grant_id} className="cockpit-sublist-item">
      <p>Record reference: {grant.record_id} · Grant: {grant.grant_id}</p>
      <strong>{grant.boundary.replace(/_/g, " ")} · {grant.source} → {grant.destination}</strong>
      <p>{grant.capability_id} · {grant.purpose.replace(/_/g, " ")} · {grant.state} · revision {grant.revision}</p>
      {grant.blocked_reason && <p>Blocked: {grant.blocked_reason.replace(/_/g, " ")} · stored state {grant.stored_state}</p>}
      <p>{grant.expires_at ? `Expires ${grant.expires_at}` : "No separate expiry; finite login and owner policy still apply"} · {grant.origin.replace(/_/g, " ")}</p>
      <p>Related work: {grant.affected_jobs.length ? grant.affected_jobs.map(job => `${job.kind} ${job.job_id}: ${job.state}`).join(" · ") : "none listed"}</p>
      {!!Object.keys(grant.limits).length && <p>Limits: {Object.entries(grant.limits).filter(([, value]) => ["string", "number", "boolean"].includes(typeof value)).map(([name, value]) => `${name.replace(/_/g, " ")}: ${String(value)}`).join(" · ")}</p>}
      {grant.kind === "approval" && grant.state === "consumed" && <p>Spent one-action receipt. Effect/readback is unconfirmed in this inventory; inspect its owning capability. Revocation cannot undo the action.</p>}
      {grant.controls.includes("host_local_reset") && <p>Host-local reset required before fresh pairing. Stop services and run scripts/reset-node-pairing.py for this adapter and exact revision {grant.revision}; no browser takeover is available.</p>}
      {(grant.controls.includes("revoke") || grant.controls.includes("deny")) && <button type="button" disabled={loading || !!error || grant.state === "revoked"} onClick={() => void revoke(grant, pending?.grant_id === grant.grant_id ? pending : undefined)}>
        {pending?.grant_id === grant.grant_id ? "Retry exact revoke" : grant.kind.endsWith("_model") ? "Revoke source consent and transfer" : "Revoke local authority"}
      </button>}
    </article>)}
  </section>;
}
