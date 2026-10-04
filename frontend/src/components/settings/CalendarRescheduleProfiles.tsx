import { useEffect, useRef, useState } from "react";
import { importRescheduleProfile, listRescheduleProfiles, recoverRescheduleProfile, rescheduleScopes, revokeRescheduleProfile } from "../../lib/calendarRescheduleApi";
import type { RescheduleProfile, RescheduleRole } from "../../lib/calendarRescheduleApi";

interface Props { ownerPrincipalId?: string | null; ownerSessionId?: string | null }

export function CalendarRescheduleProfiles({ ownerPrincipalId, ownerSessionId }: Props) {
  const key = ownerPrincipalId && ownerSessionId ? `seraph:calendar-reschedule-profile:${encodeURIComponent(ownerPrincipalId)}:${encodeURIComponent(ownerSessionId)}` : null;
  const [profiles, setProfiles] = useState<RescheduleProfile[]>([]);
  const [role, setRole] = useState<RescheduleRole>("calendar_reschedule_read");
  const [label, setLabel] = useState("");
  const [clientId, setClientId] = useState("");
  const [secret, setSecret] = useState("");
  const [token, setToken] = useState("");
  const [ack, setAck] = useState(false);
  const [pending, setPending] = useState<string | null>(null);
  const [storageBlocked, setStorageBlocked] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const generation = useRef(0);
  const controller = useRef<AbortController | null>(null);

  useEffect(() => {
    generation.current++; controller.current?.abort();
    setProfiles([]); setPending(null); setBusy(false); setMessage(null); setStorageBlocked(false);
    setClientId(""); setSecret(""); setToken(""); setAck(false);
    if (key) try {
      const saved = sessionStorage.getItem(key);
      if (saved && !/^[a-f0-9-]{36}$/.test(saved)) throw Error("invalid setup UUID");
      setPending(saved);
    } catch { setStorageBlocked(true); }
    return () => { generation.current++; controller.current?.abort(); };
  }, [key]);

  async function run(action: (signal: AbortSignal, version: number) => Promise<void>) {
    if (!key || busy || storageBlocked) return;
    const version = generation.current, abort = new AbortController();
    controller.current = abort; setBusy(true); setMessage(null);
    try { await action(abort.signal, version); }
    catch { if (version === generation.current) setMessage("The operation is unconfirmed. Inspect its original local receipt before another import."); }
    finally { if (version === generation.current) { setBusy(false); controller.current = null; } }
  }

  async function refresh(signal: AbortSignal, version: number) {
    const rows = await listRescheduleProfiles(signal);
    if (version === generation.current) setProfiles(rows);
  }

  async function save(signal: AbortSignal, version: number) {
    if (!key || pending || !ack) return;
    const uuid = crypto.randomUUID();
    sessionStorage.setItem(key, uuid);
    if (sessionStorage.getItem(key) !== uuid) { setStorageBlocked(true); return; }
    setPending(uuid);
    // The retained receipt key never contains credentials. Clear secrets from
    // the form immediately after capturing this one request, including loss.
    const body = { service: role, label, client_id: clientId, client_secret: secret || null,
      refresh_token: token, declared_scopes: rescheduleScopes(role), acknowledge_separate_identity_profile: true,
      idempotency_key: uuid };
    setClientId(""); setSecret(""); setToken(""); setAck(false);
    const profile = await importRescheduleProfile(body, signal);
    if (version !== generation.current) return;
    sessionStorage.removeItem(key); setPending(null);
    setProfiles(rows => [profile, ...rows.filter(row => row.connection_id !== profile.connection_id)]);
    setMessage("Imported locally. Verify the exact same account and selected owned calendar from the reschedule form. Import grants no provider contact or write permission.");
  }

  return <section className="rounded border border-amber-500/30 p-3 mt-3" aria-label="Exact Calendar reschedule profiles">
    <h3 className="font-semibold">Exact Calendar reschedule profiles</h3>
    <p className="text-[11px]">Separate owned-event read and write profiles. Both expose subscribed calendar-list metadata to verify the selected data owner and timezone. Event access is limited to calendars this account owns. Every token refresh must prove the exact scopes and stable account identity.</p>
    <button className="cockpit-feedback-button" disabled={busy || !key || storageBlocked} onClick={() => void run(refresh)}>Refresh local reschedule profile metadata</button>
    <form className="grid gap-2 mt-2" onSubmit={event => { event.preventDefault(); void run(save); }}>
      <label>Reschedule profile role<select value={role} disabled={busy || !!pending} onChange={event => setRole(event.target.value as RescheduleRole)}>
        <option value="calendar_reschedule_read">Owned-event readonly and identity</option>
        <option value="calendar_reschedule_write">Owned-event write and identity</option>
      </select></label>
      <p className="text-[10px]">Exact scopes: {rescheduleScopes(role).join(" · ")}</p>
      <label>Reschedule profile label<input className="cockpit-input w-full" value={label} maxLength={200} disabled={busy || !!pending} onChange={event => setLabel(event.target.value)} /></label>
      <label>Reschedule client ID<input className="cockpit-input w-full" value={clientId} maxLength={4096} autoComplete="off" disabled={busy || !!pending} onChange={event => setClientId(event.target.value)} /></label>
      <label>Reschedule client secret (optional)<input className="cockpit-input w-full" type="password" value={secret} maxLength={4096} autoComplete="new-password" disabled={busy || !!pending} onChange={event => setSecret(event.target.value)} /></label>
      <label>Reschedule refresh token<input className="cockpit-input w-full" type="password" value={token} maxLength={8192} autoComplete="new-password" disabled={busy || !!pending} onChange={event => setToken(event.target.value)} /></label>
      <label><input type="checkbox" checked={ack} disabled={busy || !!pending} onChange={event => setAck(event.target.checked)} /> I separately granted exactly these scopes for this identity profile.</label>
      <button className="cockpit-feedback-button" disabled={busy || !key || storageBlocked || !!pending || !ack || !label || !clientId || !token}>Import separate Calendar reschedule profile</button>
    </form>
    {pending && <p role="status">Original import receipt retained; credentials are cleared. <button className="cockpit-feedback-button" disabled={busy || storageBlocked} onClick={() => void run(async (signal, version) => {
      const profile = await recoverRescheduleProfile(pending, signal);
      if (version !== generation.current) return;
      if (profile?.state === "active") {
        sessionStorage.removeItem(key!); setPending(null); await refresh(signal, version);
        setMessage("The original local import is confirmed. No credentials were replayed.");
      } else setMessage("Original import is absent or unfinished. Keep this receipt key; no automatic import occurred.");
    })}>Inspect original profile import</button></p>}
    {profiles.map(profile => <div key={profile.connection_id} className="text-[11px] mt-2">{profile.label} · {profile.service} · {profile.state} · {profile.scope_status}
      <button className="cockpit-feedback-button" disabled={busy || profile.state !== "active" || storageBlocked} onClick={() => void run(async (signal, version) => {
        await revokeRescheduleProfile(profile.connection_id, { expected_revision: profile.revision, idempotency_key: crypto.randomUUID() }, signal);
        await refresh(signal, version);
      })}>Revoke reschedule profile</button>
    </div>)}
    {storageBlocked && <p role="alert">Private setup receipt storage is unavailable. Imports are blocked.</p>}
    {message && <p role="status">{message}</p>}
  </section>;
}
