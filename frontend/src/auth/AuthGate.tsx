import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { useAuthStore } from "./authStore";

export function AuthGate({ children }: { children: ReactNode }) {
  const status = useAuthStore((state) => state.status);
  const error = useAuthStore((state) => state.error);
  const bootstrap = useAuthStore((state) => state.bootstrap);
  const login = useAuthStore((state) => state.login);
  const refresh = useAuthStore((state) => state.refresh);
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => { void bootstrap(); }, [bootstrap]);
  useEffect(() => {
    if (status !== "authenticated") return;
    const interval = window.setInterval(() => { void refresh(); }, 5 * 60 * 1000);
    return () => window.clearInterval(interval);
  }, [refresh, status]);

  if (status === "authenticated") return children;
  if (status === "bootstrapping") {
    return <main className="auth-lock" aria-label="Checking operator session"><p>CONNECTING TO SERAPH…</p></main>;
  }
  if (status === "setup_required") {
    return (
      <main className="auth-lock">
        <section className="auth-card glass-panel" aria-labelledby="auth-setup-title">
          <p className="auth-kicker">SERAPH GUARDIAN CORE</p>
          <h1 id="auth-setup-title">Operator access needs setup</h1>
          <p className="auth-copy">Configure the operator credential and this LAN host/origin on the server, then restart or retry. Access remains locked until the server confirms authentication is ready.</p>
          <button onClick={() => void bootstrap()} type="button">RETRY CONNECTION</button>
        </section>
      </main>
    );
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setSubmitting(true);
    const ok = await login(password);
    if (ok) setPassword("");
    setSubmitting(false);
  };

  return (
    <main className="auth-lock">
      <form className="auth-card glass-panel" onSubmit={submit}>
        <p className="auth-kicker">SERAPH GUARDIAN CORE</p>
        <h1>Operator access</h1>
        <p className="auth-copy">Authenticate to unlock this cockpit.</p>
        <label>Passphrase<input autoComplete="current-password" name="password" required type="password" value={password} onChange={(e) => setPassword(e.target.value)} /></label>
        {error && <p className="auth-error" role="alert">{error}</p>}
        <button disabled={submitting} type="submit">{submitting ? "AUTHENTICATING…" : "UNLOCK"}</button>
      </form>
    </main>
  );
}
