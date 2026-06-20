import { useEffect, useState } from "react";
import { API_URL } from "../../config/constants";

interface ArtifactStorageSettings {
  screen: {
    preservation_enabled: boolean;
    archive_dir: string;
    archive_dir_source: string;
    stored_artifacts: string[];
    inspection_endpoint: string;
    inspection_visibility: string;
    control_env: Record<string, string>;
  };
  reports: {
    enabled: boolean;
    hour: number;
    analysis_provider: string;
    archive_dir: string;
    archive_dir_source: string;
    stored_artifacts: string[];
    control_env: Record<string, string>;
  };
  email: {
    enabled: boolean;
    preview_required: boolean;
    smtp_configured: boolean;
    recipient_configured: boolean;
    allowlist_configured: boolean;
    control_env: Record<string, string>;
  };
}

function boolLabel(value: boolean): string {
  return value ? "On" : "Off";
}

function sourceLabel(value: string): string {
  return value === "default" ? "default" : value;
}

function ArtifactRow({
  label,
  value,
  tone = "normal",
}: {
  label: string;
  value: string;
  tone?: "normal" | "good" | "warn";
}) {
  const toneClass =
    tone === "good" ? "text-green-400" : tone === "warn" ? "text-yellow-400" : "text-retro-text";

  return (
    <div className="grid grid-cols-[92px_minmax(0,1fr)] gap-2 text-[9px]">
      <div className="text-retro-text/30 uppercase tracking-wider">{label}</div>
      <div className={`${toneClass} min-w-0 truncate`} title={value}>
        {value}
      </div>
    </div>
  );
}

export function ArtifactStoragePanel() {
  const [settings, setSettings] = useState<ArtifactStorageSettings | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    async function fetchSettings() {
      try {
        const response = await fetch(`${API_URL}/api/settings/artifact-storage`);
        if (!response.ok) {
          if (!cancelled) setFailed(true);
          return;
        }
        const data = (await response.json()) as ArtifactStorageSettings;
        if (!cancelled) {
          setSettings(data);
          setFailed(false);
        }
      } catch {
        if (!cancelled) setFailed(true);
      }
    }

    void fetchSettings();
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="px-1">
      <div className="text-[10px] uppercase tracking-wider text-retro-border font-bold mb-2">
        Evidence Archive
      </div>
      <div className="border border-retro-text/10 rounded px-2 py-2 flex flex-col gap-2">
        {failed ? (
          <div className="text-[9px] text-red-400">Artifact storage settings unavailable.</div>
        ) : settings === null ? (
          <div className="text-[9px] text-retro-text/40">Loading artifact storage settings...</div>
        ) : (
          <>
            <div className="flex items-center justify-between gap-2">
              <div className="min-w-0">
                <div className="text-[10px] text-retro-text">Screen capture preservation</div>
                <div className="text-[9px] text-retro-text/40 truncate">
                  images, provider output, analysis JSON
                </div>
              </div>
              <div
                className={`text-[9px] uppercase tracking-wider ${
                  settings.screen.preservation_enabled ? "text-green-400" : "text-retro-text/40"
                }`}
              >
                {boolLabel(settings.screen.preservation_enabled)}
              </div>
            </div>

            <ArtifactRow label="Screen dir" value={settings.screen.archive_dir} />
            <ArtifactRow label="Source" value={sourceLabel(settings.screen.archive_dir_source)} />
            <ArtifactRow
              label="Inspect"
              value={`${settings.screen.inspection_endpoint} (${settings.screen.inspection_visibility.replace(/_/g, " ")})`}
              tone="good"
            />
            <ArtifactRow label="Enable via" value={settings.screen.control_env.enabled} />

            <div className="border-t border-retro-text/10 pt-2 mt-1">
              <div className="flex items-center justify-between gap-2 mb-1">
                <div className="text-[10px] text-retro-text">End-of-day reports</div>
                <div className={`text-[9px] uppercase tracking-wider ${settings.reports.enabled ? "text-green-400" : "text-retro-text/40"}`}>
                  {boolLabel(settings.reports.enabled)}
                </div>
              </div>
              <ArtifactRow label="Report dir" value={settings.reports.archive_dir} />
              <ArtifactRow label="Provider" value={settings.reports.analysis_provider} />
              <ArtifactRow label="Hour" value={`${settings.reports.hour}:00`} />
            </div>

            <div className="border-t border-retro-text/10 pt-2 mt-1">
              <div className="text-[10px] text-retro-text mb-1">Email delivery</div>
              <div className="flex flex-col gap-1">
                <ArtifactRow
                  label="Email"
                  value={boolLabel(settings.email.enabled)}
                  tone={settings.email.enabled ? "good" : "normal"}
                />
                <ArtifactRow
                  label="Preview"
                  value={settings.email.preview_required ? "Required" : "Not required"}
                  tone={settings.email.preview_required ? "warn" : "normal"}
                />
                <ArtifactRow
                  label="SMTP"
                  value={settings.email.smtp_configured ? "Configured" : "Missing"}
                  tone={settings.email.smtp_configured ? "good" : "warn"}
                />
                <ArtifactRow
                  label="Allowlist"
                  value={settings.email.allowlist_configured ? "Configured" : "Missing"}
                  tone={settings.email.allowlist_configured ? "good" : "warn"}
                />
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
