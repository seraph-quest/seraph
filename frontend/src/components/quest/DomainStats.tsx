const DOMAIN_LABELS: Record<string, string> = {
  productivity: "Productivity",
  performance: "Performance",
  health: "Health",
  influence: "Influence",
  growth: "Growth",
};

const DOMAIN_ORDER = ["productivity", "performance", "health", "influence", "growth"];

interface DomainStat {
  active: number;
  completed: number;
  total: number;
  progress: number;
}

interface Props {
  dashboard: {
    domains: Record<string, DomainStat>;
    active_count: number;
    completed_count: number;
    total_count: number;
  };
}

export function DomainStats({ dashboard }: Props) {
  return (
    <div className="px-1">
      <div className="text-[10px] uppercase tracking-wider text-retro-border font-bold mb-2">
        Priority Domains
      </div>
      <div className="cockpit-domain-stack">
        {DOMAIN_ORDER.map((domain) => {
          const stat = dashboard.domains[domain];
          const progress = stat?.progress ?? 0;
          const label = DOMAIN_LABELS[domain] ?? domain;

          return (
            <div key={domain} className="cockpit-domain-row">
              <span className="cockpit-domain-label" title={label}>{label}</span>
              <div className="cockpit-domain-bar">
                <div className="cockpit-domain-fill" style={{ width: `${progress}%` }} />
              </div>
              <span className="cockpit-domain-value">{progress}%</span>
            </div>
          );
        })}
      </div>
      <div className="mt-1 text-[10px] text-retro-text/40 text-center">
        {dashboard.completed_count}/{dashboard.total_count} priorities completed
      </div>
    </div>
  );
}
