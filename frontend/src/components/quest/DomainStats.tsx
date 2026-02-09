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
      <div className="text-[8px] uppercase tracking-wider text-retro-border font-bold mb-2">
        Five Pillars
      </div>
      <div className="flex flex-col gap-1">
        {DOMAIN_ORDER.map((domain) => {
          const stat = dashboard.domains[domain];
          const progress = stat?.progress ?? 0;
          const label = DOMAIN_LABELS[domain] ?? domain;

          return (
            <div key={domain} className="flex items-center gap-2 text-[9px]">
              <span className="w-[80px] text-retro-text/70 truncate">{label}</span>
              <div className="flex-1 h-[6px] bg-retro-bg rounded-sm overflow-hidden pixel-border-thin">
                <div
                  className="h-full bg-retro-border transition-all duration-500"
                  style={{ width: `${progress}%` }}
                />
              </div>
              <span className="w-[28px] text-right text-retro-highlight text-[8px]">
                {progress}%
              </span>
            </div>
          );
        })}
      </div>
      <div className="mt-1 text-[8px] text-retro-text/40 text-center">
        {dashboard.completed_count}/{dashboard.total_count} quests completed
      </div>
    </div>
  );
}
