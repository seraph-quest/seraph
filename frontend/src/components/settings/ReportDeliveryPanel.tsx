import { useState } from "react";
import {
  ArtifactRow,
  boolLabel,
  dirStateLabel,
  dirStateTone,
  fetchJsonWithTimeout,
  type ArtifactStorageState,
  type ReportActionResult,
} from "./settingsData";

/**
 * End-of-day reports and email delivery (#1280 PR 3): moved unchanged from the
 * old first tab to Automation, beside the other scheduled work. Preview, send
 * and test call the same endpoints; send still needs a successful preview.
 */
export function ReportDeliveryPanel({ storage }: { storage: ArtifactStorageState }) {
  const { settings, failed, mountedRef, fetchSettings } = storage;
  const [reportAction, setReportAction] = useState<"idle" | "previewing" | "sending" | "testing">("idle");
  const [reportActionResult, setReportActionResult] = useState<ReportActionResult | null>(null);
  const [reportActionError, setReportActionError] = useState<string | null>(null);
  const previewReadyForSend =
    reportActionResult?.action === "manual-preview" &&
    reportActionResult?.status === "ok" &&
    reportActionResult?.receipt?.status === "succeeded";

  const runReportAction = async (action: "preview" | "send" | "test") => {
    if (reportAction !== "idle") return;
    if (action === "send" && !previewReadyForSend) {
      setReportActionError("Preview the report before sending.");
      return;
    }
    setReportAction(action === "preview" ? "previewing" : action === "send" ? "sending" : "testing");
    setReportActionError(null);
    try {
      const payload = (await fetchJsonWithTimeout(
        action === "test" ? "/api/settings/end-of-day-report/test-email" : "/api/settings/end-of-day-report/manual",
        20_000,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: action === "test"
            ? undefined
            : JSON.stringify({
                send_email: action === "send",
                preview_acknowledged: action === "send" && previewReadyForSend,
              }),
        },
      )) as ReportActionResult;
      if (!mountedRef.current) return;
      setReportActionResult(payload);
      await fetchSettings(() => !mountedRef.current);
    } catch {
      if (mountedRef.current) setReportActionError("Report action failed.");
    } finally {
      if (mountedRef.current) setReportAction("idle");
    }
  };

  if (failed || settings === null) {
    return <div className="px-1 text-[9px] text-retro-text/40">Report settings are not loaded yet.</div>;
  }
  return (
    <div className="px-1 flex flex-col gap-2">
            <div className="border-t border-retro-text/10 pt-2 mt-1">
              <div className="flex items-center justify-between gap-2 mb-1">
                <div className="text-[10px] text-retro-text">End-of-day reports</div>
                <div className={`text-[9px] uppercase tracking-wider ${settings.reports.enabled ? "text-green-400" : "text-retro-text/40"}`}>
                  {boolLabel(settings.reports.enabled)}
                </div>
              </div>
              <ArtifactRow label="Report dir" value={settings.reports.archive_dir} />
              <ArtifactRow
                label="Dir state"
                value={dirStateLabel(settings.reports.exists, settings.reports.writable, settings.reports.creation_error)}
                tone={dirStateTone(settings.reports.exists, settings.reports.writable, settings.reports.creation_error)}
              />
              <ArtifactRow label="Provider" value={settings.reports.analysis_provider} />
              <ArtifactRow label="Hour" value={`${settings.reports.hour}:00`} />
              <ArtifactRow
                label="Receipts"
                value={`${settings.reports.receipt_count ?? 0} receipts${settings.reports.last_receipt_at ? ` · latest ${settings.reports.last_receipt_at}` : ""}`}
              />
              <div className="flex flex-wrap gap-1 pt-1">
                <button
                  type="button"
                  disabled={reportAction !== "idle"}
                  onClick={() => void runReportAction("preview")}
                  className="border border-retro-text/20 px-2 py-1 text-[9px] uppercase tracking-wider text-retro-text/70 hover:text-retro-text disabled:opacity-40"
                >
                  Preview
                </button>
                  <button
                    type="button"
                    disabled={reportAction !== "idle" || !settings.email.enabled || !previewReadyForSend}
                    onClick={() => void runReportAction("send")}
                    className="border border-retro-text/20 px-2 py-1 text-[9px] uppercase tracking-wider text-retro-text/70 hover:text-retro-text disabled:opacity-40"
                  >
                  Send
                </button>
                <button
                  type="button"
                  disabled={reportAction !== "idle" || !settings.email.enabled}
                  onClick={() => void runReportAction("test")}
                  className="border border-retro-text/20 px-2 py-1 text-[9px] uppercase tracking-wider text-retro-text/70 hover:text-retro-text disabled:opacity-40"
                >
                  Test
                </button>
              </div>
              {reportActionError && (
                <div className="text-[9px] text-red-400">{reportActionError}</div>
              )}
              {reportActionResult && (
                <div className="border border-retro-text/10 px-2 py-1 text-[9px] text-retro-text/60">
                  {(reportActionResult.action ?? reportActionResult.status ?? "report")} ·{" "}
                  {reportActionResult.email?.status ?? reportActionResult.status ?? "ok"}
                  {reportActionResult.email?.recipient_hash || reportActionResult.recipient_hash
                    ? ` · recipient ${reportActionResult.email?.recipient_hash ?? reportActionResult.recipient_hash}`
                    : ""}
                  {reportActionResult.receipt?.receipt_sha256
                    ? ` · receipt ${reportActionResult.receipt.receipt_sha256.slice(0, 12)}`
                    : ""}
                </div>
              )}
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
                  label="Sender"
                  value={settings.email.sender_configured ? "Configured" : "Missing"}
                  tone={settings.email.sender_configured ? "good" : "warn"}
                />
                <ArtifactRow
                  label="Allowlist"
                  value={settings.email.allowlist_configured ? "Configured" : "Missing"}
                  tone={settings.email.allowlist_configured ? "good" : "warn"}
                />
              </div>
            </div>
    </div>
  );
}
