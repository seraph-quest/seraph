import { blockedRuntimeDomains, guardianRuntimeLabel, lifecycleNeedsAction, lifecycleRuntimeLabel } from "../../lib/cordisRuntime";
import type { GuardianRuntimeStatus, LifecycleRuntimeStatus, RuntimeDomainStatus } from "../../lib/cordisRuntime";

/**
 * Cockpit banner for runtime owners that block work (#1020, #1025): a blocked
 * guardian loop, any non-ready runtime domain, or a scheduler/dispatch
 * lifecycle that is not active (a deliberately disabled scheduler is not
 * shown), each with the server's own fix. Shown only when something needs the
 * operator; it never infers a state.
 */
export function RuntimeOwnerBanner({ guardian, domains, lifecycle = [] }: {
  guardian: GuardianRuntimeStatus | null; domains: RuntimeDomainStatus[]; lifecycle?: LifecycleRuntimeStatus[];
}) {
  const pending = blockedRuntimeDomains(domains);
  // A non-ready owner row already names the domain; its lifecycle chip would repeat it.
  const pendingIds = new Set(pending.map(item => item.domain));
  const stopped = lifecycle.filter(item => lifecycleNeedsAction(item) && !pendingIds.has(item.domain));
  if (guardian?.loop !== "blocked" && pending.length === 0 && stopped.length === 0) return null;
  return <section role="status" aria-label="Runtime owners needing action" className="cockpit-hint-strip">
    {guardian?.loop === "blocked" && <span className="cockpit-hint-chip">
      {guardianRuntimeLabel(guardian)}{guardian.recovery ? ` · fix: ${guardian.recovery}` : ""}
    </span>}
    {pending.map(item => <span key={item.domain} className="cockpit-hint-chip">
      {item.domain}: {item.state}{item.reason ? ` · ${item.reason}` : ""}{item.recovery ? ` · fix: ${item.recovery}` : ""}
    </span>)}
    {stopped.map(item => <span key={`lifecycle:${item.domain}`} className="cockpit-hint-chip">
      {lifecycleRuntimeLabel(item)}{item.recovery ? ` · fix: ${item.recovery}` : ""}
    </span>)}
  </section>;
}
