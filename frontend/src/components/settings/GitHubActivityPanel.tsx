import { useEffect, useRef, useState } from "react";
import { API_URL } from "../../config/constants";
import { apiFetch } from "../../lib/api";
import {
  GITHUB_ACTIVITY_GRANT_SENTENCE, ghLoginCheck, githubActivityItems, githubActivityStatus, parseRepositoryList,
  validOwner, validRepository,
  type GhLoginCheck, type GitHubActivityItem, type GitHubActivityStatus,
} from "../../lib/githubActivity";

// The operator's own GitHub activity through their local gh login (#1264).
// Off by default; the backend checks the login, pins it and owns every gh start.
export function GitHubActivityPanel({ ownerPrincipalId, ownerSessionId }: {
  ownerPrincipalId?: string | null; ownerSessionId?: string | null;
}) {
  const generation = useRef(0);
  const [status, setStatus] = useState<GitHubActivityStatus | null>(null);
  const [check, setCheck] = useState<GhLoginCheck | null>(null);
  const [ghPath, setGhPath] = useState("");
  const [discover, setDiscover] = useState(true);
  const [repositories, setRepositories] = useState("");
  const [owners, setOwners] = useState("");
  const [excluded, setExcluded] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [modelContext, setModelContext] = useState(false);
  const [kinds, setKinds] = useState<string[] | null>(null);
  const [items, setItems] = useState<GitHubActivityItem[] | null>(null);
  const [runMessage, setRunMessage] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function request(path: string, init: RequestInit = {}, signal?: AbortSignal) {
    const response = await apiFetch(`${API_URL}/api/capabilities/github/activity${path}`, { ...init, signal });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      const code = typeof body.detail?.code === "string" ? body.detail.code : "GitHub activity metadata unavailable";
      throw new Error(code);
    }
    return response.json();
  }
  const post = (path: string, body: unknown, method = "POST") => request(path, {
    method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });

  useEffect(() => {
    const current = ++generation.current;
    const controller = new AbortController();
    setStatus(null); setCheck(null); setAcknowledged(false); setModelContext(false); setItems(null);
    setRunMessage(null); setError(null);
    void request("", {}, controller.signal).then(value => {
      if (current !== generation.current) return;
      const next = githubActivityStatus(value);
      setStatus(next);
      if (next.source.scope) {
        setDiscover(next.source.scope.discover_from_activity);
        setRepositories(next.source.scope.repositories.join(", "));
        setOwners(next.source.scope.owners.join(", "));
        setExcluded(next.source.scope.exclude_repositories.join(", "));
        if (next.source.scope.kinds.length) setKinds(next.source.scope.kinds);
      }
      setModelContext(next.source.model_context_allowed);
    }).catch(() => { if (current === generation.current) setError("GitHub activity metadata unavailable. Refresh settings to recover."); });
    return () => { ++generation.current; controller.abort(); };
    // Consent resets on owner/root changes, including a new login.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ownerPrincipalId, ownerSessionId]);

  async function run(action: () => Promise<void>) {
    if (pending) return;
    const current = generation.current;
    setPending(true); setError(null);
    try {
      await action();
    } catch (failure) {
      if (current === generation.current) {
        setError(`${failure instanceof Error ? failure.message.replace(/_/g, " ") : "Outcome unconfirmed"}.`);
        try { const value = githubActivityStatus(await request("")); if (current === generation.current) setStatus(value); } catch { /* keep last-known state */ }
      }
    } finally { if (current === generation.current) setPending(false); }
  }

  const checkLogin = () => run(async () => {
    const current = generation.current;
    setCheck(null); setAcknowledged(false);
    const value = ghLoginCheck(await post("/gh/check", {
      acknowledge_gh_login_check: true, ...(ghPath.trim() ? { gh_entry_path: ghPath.trim() } : {}),
    }));
    if (current === generation.current) setCheck(value);
  });

  const enable = () => run(async () => {
    if (!check || !status) return;
    const current = generation.current;
    const value = githubActivityStatus(await post("/source", {
      expected_revision: status.source.revision,
      scope: { discover_from_activity: discover, repositories: parseRepositoryList(repositories),
        owners: parseRepositoryList(owners), exclude_repositories: parseRepositoryList(excluded),
        ...(kinds ? { kinds } : {}) },
      acknowledge_gh_ambient_login: true, expected_identity_id: check.identity_id,
      expected_gh_entry_path: check.gh_entry_path, allow_model_context: modelContext,
    }, "PUT"));
    if (current === generation.current) { setStatus(value); setAcknowledged(false); setCheck(null); }
  });

  const switchOff = () => run(async () => {
    if (!status) return;
    const current = generation.current;
    const value = githubActivityStatus(await post("/revoke", { expected_revision: status.source.revision }));
    if (current === generation.current) setStatus(value);
  });

  // A source of another ownership identity (#1270): stop only; it never becomes this login's.
  const stopOther = (id: string, revision: number) => run(async () => {
    const current = generation.current;
    await post(`/other-identity-sources/${encodeURIComponent(id)}/stop`, { expected_revision: revision });
    const value = githubActivityStatus(await request(""));
    if (current === generation.current) setStatus(value);
  });

  const collectNow = () => run(async () => {
    const current = generation.current;
    const result = await post("/collect", {});
    const day = githubActivityItems(await request(""
      + (typeof result?.run?.report_date === "string" ? `/items?report_date=${encodeURIComponent(result.run.report_date)}` : "/items")));
    const value = githubActivityStatus(await request(""));
    if (current === generation.current) {
      setRunMessage(typeof result?.message === "string" ? result.message : day.message);
      setItems(day.items); setStatus(value);
    }
  });

  const source = status?.source;
  const active = source?.state === "active";
  const login = check?.identity_login ?? source?.identity_login ?? null;
  const scopeValid = [...parseRepositoryList(repositories), ...parseRepositoryList(excluded)].every(validRepository)
    && parseRepositoryList(owners).every(validOwner);
  const canEnable = !pending && !!status && !status.ownership_required && !!check && acknowledged && !!ownerSessionId && scopeValid
    && (discover || parseRepositoryList(repositories).length > 0);
  // The full-scope sentence stays visible whenever a source exists or a login was checked.
  const showSentence = !!check || (!!source && source.state !== "not_connected");

  return <section className="space-y-3 px-1" aria-label="GitHub activity for reports">
    <h3>GitHub activity for reports</h3>
    <p>Collect your own GitHub activity (commits, pull requests and issues) as evidence, using the gh command already logged in on this computer. Off until you enable it.</p>
    <p role="status">{!status ? "Loading GitHub activity source" : active
      ? `On · @${source?.identity_login ?? "unknown"} · uses your local gh login`
      : status.message || source?.state.replace(/_/g, " ")}</p>
    {showSentence && <p>{GITHUB_ACTIVITY_GRANT_SENTENCE}</p>}
    <p>The source belongs to your ownership identity: any of your logins can see and switch it off, and a new login or an expired one does not stop it.</p>
    {status?.ownership_required && <p role="note">This login has no ownership identity. If you enabled this source before, recover that ownership first: log in with your recovery code, or from the browser that holds your continuity cookie. Enrolling a new identity (Operator ownership and recovery, the bar above the cockpit) leaves an earlier source with the old one.</p>}
    {!!status?.other_identity_sources.length && <ul aria-label="GitHub sources of other ownership identities">
      {status.other_identity_sources.map(other => <li key={other.id}>{other.message || `A source of another ownership identity is ${other.state}.`}
        {other.stoppable && <button disabled={pending} onClick={() => void stopOther(other.id, other.revision)}>Stop that source</button>}
      </li>)}
    </ul>}
    {status && !status.gh.available && <p>No usable gh command was found ({(status.gh.reason_code ?? "unavailable").replace(/_/g, " ")}). The local gh login only works when Seraph's backend runs directly on your computer.</p>}
    {status?.last_run && <p>Last collection ({status.last_run.report_date}): {status.last_run.message}</p>}

    <label className="block">gh path (optional) <input aria-label="gh path" value={ghPath} placeholder="/opt/homebrew/bin/gh"
      onChange={event => { setGhPath(event.target.value); setCheck(null); setAcknowledged(false); }} /></label>
    <button disabled={pending || !status} onClick={() => void checkLogin()}>Check gh login</button>
    {check && <p>gh {check.gh_version} at {check.gh_entry_path}, logged in as @{check.identity_login}</p>}

    <fieldset><legend>Repositories</legend>
      <label className="block"><input type="checkbox" checked={discover} onChange={event => { setDiscover(event.target.checked); setAcknowledged(false); }} /> Repositories I was active in that day</label>
      <label className="block">Always read <input aria-label="Always read repositories" value={repositories} placeholder="owner/name, owner/name"
        onChange={event => { setRepositories(event.target.value); setAcknowledged(false); }} /></label>
      <label className="block">Only discover under owners <input aria-label="Discovery owners" value={owners} placeholder="owner"
        onChange={event => { setOwners(event.target.value); setAcknowledged(false); }} /></label>
      <label className="block">Never read <input aria-label="Excluded repositories" value={excluded} placeholder="owner/name"
        onChange={event => { setExcluded(event.target.value); setAcknowledged(false); }} /></label>
      {!scopeValid && <p role="note">Use owner/name for repositories and plain account names for owners.</p>}
      <p>Discovery uses GitHub's event feed, which can lag by minutes to hours; listing your main repository makes it reliable.</p>
    </fieldset>

    <label className="block"><input type="checkbox" checked={acknowledged} disabled={!check}
      onChange={event => setAcknowledged(event.target.checked)} /> Use my local gh login{login ? ` (@${login})` : ""} to collect my GitHub activity. gh holds my full GitHub access for this account. Seraph will only send the read-only requests listed here.</label>
    {!!status?.endpoints.length && <ul aria-label="GitHub activity requests">{status.endpoints.map(endpoint => <li key={endpoint}><code>{endpoint}</code></li>)}</ul>}
    <label className="block"><input type="checkbox" checked={modelContext} onChange={event => setModelContext(event.target.checked)} /> Include this activity in the end-of-day report's model analysis.</label>

    <button disabled={!canEnable} onClick={() => void enable()}>{active ? "Save and keep on" : "Enable"}</button>
    <button disabled={pending || !active} onClick={() => void switchOff()}>Switch off</button>
    <button disabled={pending || !active} onClick={() => void collectNow()}>Collect now</button>
    {runMessage && <p>{runMessage}</p>}
    {items && <ul aria-label="GitHub activity items">{items.map(item => <li key={item.id}>
      {item.repository} · {item.kind.replace(/_/g, " ")} · {item.url
        ? <a href={item.url} target="_blank" rel="noreferrer noopener">{item.title || item.number || "link"}</a>
        : <span>{item.title}</span>}
    </li>)}</ul>}
    {error && <p role="alert">{error}</p>}
  </section>;
}
