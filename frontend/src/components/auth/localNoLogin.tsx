// Local single-operator mode without login (#1161). Kept outside the auth
// gate module so consumers that mock the gate still render the badge.

type LocalModeSession = { auth_mode?: string; local_no_login?: boolean };

export function isLocalNoLoginSession(session: LocalModeSession | null | undefined): boolean {
  return session?.auth_mode === "local_no_login" && session.local_no_login === true;
}

export const LOCAL_NO_LOGIN_BADGE = "No login (local only)";

export function LocalNoLoginBadge({ className = "" }: { className?: string }) {
  return (
    <span
      role="status"
      title="Seraph is running without a login. Any local process or page that passes the loopback Host/Origin checks has operator authority."
      className={`cockpit-auth-local-badge inline-flex items-center border border-amber-400/50 bg-amber-400/15 px-2 py-0.5 text-[10px] uppercase tracking-wide text-amber-200 ${className}`}
    >
      {LOCAL_NO_LOGIN_BADGE}
    </span>
  );
}
