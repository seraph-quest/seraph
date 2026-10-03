import { useEffect, useRef, useState } from "react";
import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";

export const GITHUB_ACTIONS = [
  ["github_issue_write", "Create issues"],
  ["github_comment_write", "Create issue comments"],
  ["github_git_objects_write", "Create tested Git objects"],
  ["github_new_branch_write", "Create a new feature branch"],
  ["github_ready_pr_write", "Create a ready pull request"],
] as const;
type Action = typeof GITHUB_ACTIONS[number][0];
type Connection = { id: string | null; repository: string | null; revision: number;
  mode: string; credential_configured: boolean; active_job_id: string | null;
  consent: { state: string; expires_at: string | null; actions: Action[]; root_bound: boolean } };

export function GitHubConnectionPanel({ ownerPrincipalId, ownerSessionId }: {
  ownerPrincipalId?: string | null; ownerSessionId?: string | null;
}) {
  const generation = useRef(0);
  const [connection, setConnection] = useState<Connection | null>(null);
  const [keys, setKeys] = useState<string[]>([]);
  const [repository, setRepository] = useState("");
  const [key, setKey] = useState("");
  const [actions, setActions] = useState<Action[]>([]);
  const [duration, setDuration] = useState(900);
  const [acknowledged, setAcknowledged] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function request(path: string, init: RequestInit = {}, signal?: AbortSignal) {
    const response = await apiFetch(`${API_URL}/api${path}`, { ...init, signal });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail?.code ?? "GitHub metadata unavailable");
    }
    return response.json();
  }
  async function refresh(signal?: AbortSignal) {
    const [value, available] = await Promise.all([
      request("/capabilities/github/connection", {}, signal), request("/vault/keys", {}, signal),
    ]);
    return { value: value as Connection, available: available as { key: string }[] };
  }
  useEffect(() => {
    const current = ++generation.current;
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 15_000);
    setAcknowledged(false); setActions([]); setConnection(null); setKey(""); setError(null);
    void refresh(controller.signal).then(({ value, available }) => {
      if (current !== generation.current) return;
      setConnection(value); setRepository(value.repository ?? ""); setKeys(available.map(item => item.key));
    }).catch(() => { if (current === generation.current) setError("GitHub metadata unavailable. Refresh settings to recover."); })
      .finally(() => window.clearTimeout(timer));
    return () => { ++generation.current; controller.abort(); window.clearTimeout(timer); };
    // Authority resets on owner/root changes, including a new login.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ownerPrincipalId, ownerSessionId]);

  async function mutate(stop = false) {
    if (!connection || pending) return;
    const current = generation.current;
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 15_000);
    setPending(true); setError(null);
    try {
      await request(stop ? "/capabilities/github/connection/revoke" : "/capabilities/github/connection", {
        method: stop ? "POST" : "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(stop ? { expected_revision: connection.revision } : {
          repository, vault_key: key, mode: "active", expected_revision: connection.revision,
          consent: { acknowledged, duration_seconds: duration, actions },
        }),
      }, controller.signal);
      const { value, available } = await refresh(controller.signal);
      if (current === generation.current) {
        setConnection(value); setKeys(available.map(item => item.key)); setAcknowledged(false);
      }
    } catch (failure) {
      if (current === generation.current) {
        setAcknowledged(false);
        setError(`${failure instanceof Error ? failure.message.replace(/_/g, " ") : "Outcome unconfirmed"}. Refresh metadata before another submission.`);
        // A transport failure never triggers a second mutation.
        try { const { value } = await refresh(controller.signal); if (current === generation.current) setConnection(value); } catch { /* retain visible last-known metadata */ }
      }
    } finally { window.clearTimeout(timer); if (current === generation.current) setPending(false); }
  }

  return <section className="space-y-3 px-1" aria-label="GitHub connection">
    <p>Allow named GitHub actions for this repository and current login. Every publication still requires its own exact approval.</p>
    <p role="status">{connection ? `${connection.consent.state.replace(/_/g, " ")} · ${connection.repository ?? "No repository"}` : "Loading GitHub connection"}</p>
    {connection?.consent.expires_at && <p>Expires {new Date(connection.consent.expires_at).toLocaleString()} · {connection.consent.root_bound ? "Current login" : "Different login; consent required"}</p>}
    {!!connection?.consent.actions.length && <p>Allowed: {connection.consent.actions.map(action => GITHUB_ACTIONS.find(item => item[0] === action)?.[1] ?? action).join(", ")}</p>}
    {connection?.active_job_id && <p>A publication holds this connection. Stop remains available; reconcile its uncertain effects before changing scope.</p>}
    <label className="block">Repository <input aria-label="GitHub repository" value={repository} onChange={event => { setRepository(event.target.value); setAcknowledged(false); }} placeholder="owner/repository" /></label>
    <label className="block">Vault credential <select aria-label="GitHub vault credential" value={key} onChange={event => { setKey(event.target.value); setAcknowledged(false); }}>
      <option value="">Select an existing vault key</option>{keys.map(value => <option key={value}>{value}</option>)}
    </select></label>
    <p>The credential supplies access; it does not grant consent.</p>
    <fieldset><legend>Allowed actions</legend>{GITHUB_ACTIONS.map(([value, label]) => <label className="block" key={value}>
      <input type="checkbox" checked={actions.includes(value)} onChange={event => { setActions(old => event.target.checked ? [...old, value] : old.filter(item => item !== value)); setAcknowledged(false); }} /> {label}
    </label>)}</fieldset>
    <label className="block">Duration <select aria-label="GitHub consent duration" value={duration} onChange={event => { setDuration(Number(event.target.value)); setAcknowledged(false); }}>
      <option value={300}>5 minutes</option><option value={900}>15 minutes</option><option value={3600}>1 hour</option>
    </select></label>
    <label className="block"><input type="checkbox" checked={acknowledged} onChange={event => setAcknowledged(event.target.checked)} /> I consent to these actions for this repository and current login until expiry.</label>
    <button disabled={pending || !connection || !!connection.active_job_id || !ownerSessionId || !acknowledged || !actions.length || !repository || !key} onClick={() => void mutate()}>Save explicit consent</button>
    <button disabled={pending || !connection?.id} onClick={() => void mutate(true)}>Stop GitHub writes</button>
    {error && <p role="alert">{error}</p>}
  </section>;
}
