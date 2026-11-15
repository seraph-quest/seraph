import { blockedRuntimeDomains, guardianRuntimeLabel } from "../../lib/cordisRuntime";
import type { GuardianRuntimeStatus, RuntimeDomainStatus } from "../../lib/cordisRuntime";

/**
 * Cockpit banner for runtime owners that block work (#1020): a blocked guardian
 * loop or any non-ready runtime domain, each with the server's own fix. Shown
 * only when something needs the operator; it never infers a state.
 */
export function RuntimeOwnerBanner({ guardian, domains }: { guardian: GuardianRuntimeStatus | null; domains: RuntimeDomainStatus[] }) {
  const pending = blockedRuntimeDomains(domains);
  if (guardian?.loop !== "blocked" && pending.length === 0) return null;
  return <section role="status" aria-label="Runtime owners needing action" className="cockpit-hint-strip">
    {guardian?.loop === "blocked" && <span className="cockpit-hint-chip">
      {guardianRuntimeLabel(guardian)}{guardian.recovery ? ` · fix: ${guardian.recovery}` : ""}
    </span>}
    {pending.map(item => <span key={item.domain} className="cockpit-hint-chip">
      {item.domain}: {item.state}{item.reason ? ` · ${item.reason}` : ""}{item.recovery ? ` · fix: ${item.recovery}` : ""}
    </span>)}
  </section>;
}
