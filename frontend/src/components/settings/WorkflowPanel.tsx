import { useCallback, useEffect, useState } from "react";

import { API_URL } from "../../config/constants";
import { useChatStore } from "../../stores/chatStore";
import { buildWorkflowDraft, type WorkflowInfo } from "./workflowDraft";

function WorkflowRow({
  workflow,
  onToggle,
  onDraft,
}: {
  workflow: WorkflowInfo;
  onToggle: (name: string, enabled: boolean) => void;
  onDraft: (workflow: WorkflowInfo) => void;
}) {
  const statusColor = workflow.enabled ? "bg-green-400" : "bg-retro-text/30";
  const approvalLabel = workflow.requires_approval ? workflow.approval_behavior : "direct";
  const isDraftable = workflow.user_invocable && workflow.enabled && workflow.is_available !== false;
  const availabilityHint = workflow.is_available === false
    ? [
        workflow.missing_tools && workflow.missing_tools.length > 0
          ? `missing tools: ${workflow.missing_tools.join(", ")}`
          : "",
        workflow.missing_skills && workflow.missing_skills.length > 0
          ? `missing skills: ${workflow.missing_skills.join(", ")}`
          : "",
      ].filter(Boolean).join(" · ")
    : "";

  return (
    <div className="flex items-start gap-1 px-1 py-0.5 border-b border-retro-text/10 last:border-b-0">
      <div className={`w-1.5 h-1.5 rounded-full flex-shrink-0 mt-1 ${statusColor}`} />
      <div className="flex-1 min-w-0">
        <div className="text-[10px] font-bold text-retro-text truncate">
          {workflow.name}
          {workflow.user_invocable && (
            <span className="text-retro-highlight/60 ml-1 font-normal">invocable</span>
          )}
        </div>
        <div className="text-[9px] text-retro-text/40">
          {workflow.description}
        </div>
        <div className="text-[9px] text-retro-text/30 uppercase tracking-wider">
          {workflow.step_count} steps · {workflow.risk_level} risk · {approvalLabel}
        </div>
        <div className="text-[9px] text-retro-text/20">
          tools: {workflow.requires_tools.join(", ") || "none"}
          {workflow.requires_skills.length > 0 && ` · skills: ${workflow.requires_skills.join(", ")}`}
        </div>
        {availabilityHint && (
          <div className="text-[9px] text-amber-300/70">
            unavailable now · {availabilityHint}
          </div>
        )}
      </div>
      <div className="flex items-center gap-1">
        {isDraftable && (
          <button
            onClick={() => onDraft(workflow)}
            className="text-[9px] text-retro-highlight hover:text-retro-text px-0.5"
            title="Draft a workflow command in the cockpit composer"
          >
            draft
          </button>
        )}
        <button
          onClick={() => onToggle(workflow.name, !workflow.enabled)}
          className={`text-[9px] px-0.5 ${workflow.enabled ? "text-green-400 hover:text-red-400" : "text-retro-text/40 hover:text-green-400"}`}
        >
          {workflow.enabled ? "on" : "off"}
        </button>
      </div>
    </div>
  );
}

export function WorkflowPanel() {
  const [workflows, setWorkflows] = useState<WorkflowInfo[]>([]);
  const [status, setStatus] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const setInterfaceMode = useChatStore((s) => s.setInterfaceMode);
  const setSettingsPanelOpen = useChatStore((s) => s.setSettingsPanelOpen);

  const fetchWorkflows = useCallback(async () => {
    try {
      const response = await fetch(`${API_URL}/api/workflows`);
      if (!response.ok) {
        setStatus("Failed to load workflows");
        return;
      }
      const payload = await response.json();
      setWorkflows(Array.isArray(payload.workflows) ? payload.workflows : []);
      setStatus(null);
    } catch {
      setStatus("Failed to load workflows");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void fetchWorkflows();
  }, [fetchWorkflows]);

  const handleToggle = async (name: string, enabled: boolean) => {
    setStatus(`${enabled ? "Enabling" : "Disabling"} ${name}...`);
    try {
      const response = await fetch(`${API_URL}/api/workflows/${name}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      });
      if (!response.ok) {
        setStatus(`Failed to update ${name}`);
        return;
      }
      await fetchWorkflows();
      setStatus(`${name} ${enabled ? "enabled" : "disabled"}`);
    } catch {
      setStatus(`Failed to update ${name}`);
    }
  };

  const handleReload = async () => {
    setStatus("Reloading workflows...");
    try {
      const response = await fetch(`${API_URL}/api/workflows/reload`, { method: "POST" });
      if (!response.ok) {
        setStatus("Failed to reload workflows");
        return;
      }
      await fetchWorkflows();
      setStatus("Workflows reloaded");
    } catch {
      setStatus("Failed to reload workflows");
    }
  };

  const handleDraft = (workflow: WorkflowInfo) => {
    const message = buildWorkflowDraft(workflow);
    setInterfaceMode("cockpit");
    setSettingsPanelOpen(false);
    window.setTimeout(() => {
      window.dispatchEvent(
        new CustomEvent("seraph-cockpit-compose", {
          detail: { message },
        }),
      );
    }, 0);
  };

  return (
    <div className="px-1">
      <div className="flex items-center justify-between gap-2 mb-1">
        <div className="text-[10px] uppercase tracking-wider text-retro-border font-bold">
          Workflows
        </div>
        <button
          onClick={handleReload}
          className="text-[9px] text-retro-text/40 hover:text-retro-highlight px-0.5 uppercase tracking-wider"
        >
          Reload
        </button>
      </div>
      {loading ? (
        <div className="text-[9px] text-retro-text/30 px-1">Loading workflows...</div>
      ) : workflows.length > 0 ? (
        <div className="border border-retro-text/10 rounded mb-1">
          {workflows.map((workflow) => (
            <WorkflowRow
              key={workflow.name}
              workflow={workflow}
              onToggle={handleToggle}
              onDraft={handleDraft}
            />
          ))}
        </div>
      ) : (
        <div className="text-[9px] text-retro-text/30 px-1">No workflows available</div>
      )}
      {status && <div className="text-[9px] text-retro-highlight px-1">{status}</div>}
    </div>
  );
}
