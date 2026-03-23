import type { ChatMessage } from "../../types";
import { API_URL } from "../../config/constants";
import { useState } from "react";
import { appEventBus } from "../../lib/appEventBus";

interface MessageBubbleProps {
  message: ChatMessage;
}

const ROLE_STYLES: Record<string, string> = {
  user: "bg-retro-user/30 border-l-2 border-retro-user text-retro-text",
  agent: "bg-retro-agent/30 border-l-2 border-retro-highlight text-retro-text",
  step: "bg-retro-step/20 border-l-2 border-retro-step text-retro-text/70 text-[10px]",
  error: "bg-retro-error/20 border-l-2 border-retro-error text-red-300",
  proactive: "bg-retro-border/10 border-l-2 border-retro-border text-retro-text",
  approval: "bg-yellow-500/10 border-l-2 border-yellow-400 text-retro-text",
  clarification: "bg-cyan-500/10 border-l-2 border-cyan-300 text-retro-text",
};

const ROLE_LABELS: Record<string, string> = {
  user: "You",
  agent: "Seraph",
  step: "Step",
  error: "Error",
  proactive: "Seraph",
  approval: "Approval",
  clarification: "Clarify",
};

export function MessageBubble({ message }: MessageBubbleProps) {
  const [approvalStatus, setApprovalStatus] = useState(message.approvalStatus ?? "pending");
  const [submitting, setSubmitting] = useState(false);
  const style = ROLE_STYLES[message.role] ?? ROLE_STYLES.agent;
  const label = ROLE_LABELS[message.role] ?? "Agent";
  const isStep = message.role === "step";
  const isApproval = message.role === "approval";
  const isClarification = message.role === "clarification";

  const handleApproval = async (decision: "approve" | "deny") => {
    if (!message.approvalId || submitting || approvalStatus !== "pending") return;
    setSubmitting(true);
    try {
      const res = await fetch(`${API_URL}/api/approvals/${message.approvalId}/${decision}`, {
        method: "POST",
      });
      if (res.ok) {
        const data = await res.json();
        if (data?.status) {
          setApprovalStatus(data.status);
          if (decision === "approve" && data.status === "approved" && data.resume_message) {
            appEventBus.emit("approval-resume", {
              sessionId: data.session_id ?? null,
              message: data.resume_message,
            });
          }
        }
      }
    } catch {
      // ignore
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className={`message-enter px-3 py-2 rounded-sm ${style}`}>
      <div className="flex items-center gap-2 mb-1">
        <span className="text-[10px] uppercase tracking-wider text-retro-border font-bold">
          {label}
          {isStep && message.stepNumber ? ` ${message.stepNumber}` : ""}
        </span>
        {isStep && message.toolUsed && (
          <span className="text-[9px] text-retro-highlight/80 uppercase">
            [{message.toolUsed}]
          </span>
        )}
        {isApproval && message.riskLevel && (
          <span className="text-[9px] text-yellow-300 uppercase">
            [{message.riskLevel}]
          </span>
        )}
      </div>
      <div className="text-[11px] leading-relaxed break-words whitespace-pre-wrap">
        {message.content}
      </div>
      {isClarification && message.clarificationOptions && message.clarificationOptions.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-2">
          {message.clarificationOptions.map((option) => (
            <span
              key={option}
              className="text-[10px] px-2 py-1 border border-cyan-400/40 text-cyan-200 rounded-sm"
            >
              {option}
            </span>
          ))}
        </div>
      )}
      {isApproval && (
        <div className="mt-2 flex items-center gap-2">
          {approvalStatus === "pending" ? (
            <>
              <button
                onClick={() => handleApproval("approve")}
                disabled={submitting}
                className="text-[10px] px-2 py-1 border border-green-400 text-green-300 rounded-sm hover:bg-green-400/10 disabled:opacity-50"
              >
                Approve
              </button>
              <button
                onClick={() => handleApproval("deny")}
                disabled={submitting}
                className="text-[10px] px-2 py-1 border border-red-400 text-red-300 rounded-sm hover:bg-red-400/10 disabled:opacity-50"
              >
                Deny
              </button>
            </>
          ) : (
            <div className="text-[10px] text-retro-text/60 uppercase tracking-wider">
              {approvalStatus === "approved" && "Approved. Retrying your request..."}
              {approvalStatus === "consumed" && "Already approved and used."}
              {approvalStatus === "denied" && "Denied."}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
