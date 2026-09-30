import { fireEvent, render, screen } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { describe, expect, it, vi } from "vitest";

import { GuardianCandidateInspector } from "./GuardianCandidateInspector";
import type { GuardianInboxItem } from "../../types";

const candidate: GuardianInboxItem = {
  id: "candidate-1",
  revision: 4,
  state: "pending",
  degraded: false,
  source_kind: "source_packet",
  source_id: "packet-1",
  title: "Verified packet ready",
  summary: "A bounded redacted summary.",
  why_now: "The source changed.",
  goal_id: "goal-1",
  goal_revision: 2,
  watch_id: "watch-1",
  plan_revision: 3,
  task_id: "task-1",
  expires_at: "2030-01-01T00:00:00Z",
  snoozed_until: null,
  evidence_refs: [{ artifact_id: "artifact-1", file_path: "guardian/packet.md", content_sha256: "a".repeat(64), status: "verified", verification: "readback" }],
  evidence_previews: [{ artifact_id: "artifact-1", file_path: "guardian/packet.md", sha256: "a".repeat(64), owner_session_id: "session-1", text: "Redacted preview", trust: "verified" }],
  job: { id: "job-1", status: "succeeded", attempt_count: 1, max_attempts: 2, readback_id: "readback-1", verified_at: "2030-01-01T00:00:00Z", digest: "b".repeat(64), readback_status: "verified", readbacks: [{ target_path: "guardian/packet.md", readback_id: "readback-1", verified_at: "2030-01-01T00:00:00Z", digest: "b".repeat(64), status: "succeeded" }] },
  allowed_actions: ["accept_followup", "snooze"],
  evidence_status: "verified",
  source_status: "succeeded",
  source_freshness: "current",
  verification_status: "passed",
  memory_status: "no_learning",
  policy_reason: "Budget and authority remain separate.",
  recovery_action: null,
  evidence_url: null,
  task_url: "/api/work-board/tasks/task-1",
  watch_url: "/api/capabilities/source-watches/watch-1",
};

describe("GuardianCandidateInspector", () => {
  it("renders the bounded dossier, readbacks, safe links, and server actions", () => {
    const onAction = vi.fn();
    const onInspectArtifact = vi.fn();
    render(<GuardianCandidateInspector item={candidate} actionsEnabled onAction={onAction} onInspectArtifact={onInspectArtifact} onOpenTask={vi.fn()} />);

    expect(screen.getByRole("heading", { name: "Verified packet ready" })).toHaveFocus();
    expect(screen.getByText(/Policy boundary:.*Budget and authority remain separate\./)).toBeInTheDocument();
    expect(screen.getByText(/readback-1 · succeeded/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open source watch" })).toHaveAttribute("href", "/api/capabilities/source-watches/watch-1");
    expect(screen.getByText(/guardian\/packet\.md/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Accept follow-up" }));
    expect(onAction).toHaveBeenCalledWith("accept_followup");
    fireEvent.click(screen.getByRole("button", { name: /Open evidence artifact-1/ }));
    expect(onInspectArtifact).toHaveBeenCalledWith(candidate.evidence_refs[0], candidate.evidence_previews?.[0]);
    expect(screen.queryByRole("link", { name: /evil/i })).not.toBeInTheDocument();
  });

  it("does not invent actions for a terminal or unknown candidate", () => {
    render(<GuardianCandidateInspector item={{ ...candidate, state: "expired", allowed_actions: [], degraded: true }} />);
    expect(screen.queryByRole("button", { name: "Accept follow-up" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Snooze" })).not.toBeInTheDocument();
    expect(screen.getByText("degraded")).toBeInTheDocument();
  });

  it("does not steal focus when the same candidate receives refreshed metadata", () => {
    const onClose = vi.fn();
    const { rerender } = render(<GuardianCandidateInspector item={candidate} onClose={onClose} />);
    const closeButton = screen.getByRole("button", { name: "Close candidate inspector" });
    closeButton.focus();
    expect(closeButton).toHaveFocus();

    rerender(<GuardianCandidateInspector item={{ ...candidate, summary: "Updated metadata." }} onClose={onClose} />);

    expect(screen.getByRole("button", { name: "Close candidate inspector" })).toHaveFocus();
    expect(screen.getByText("Updated metadata.")).toBeInTheDocument();
  });

  it("shows the server-provided snooze time and keeps the absent reason explicit", () => {
    render(<GuardianCandidateInspector item={{ ...candidate, state: "snoozed", snoozed_until: "2030-01-01T00:00:00Z" }} />);

    expect(screen.getByText(/Snoozed until/)).toBeInTheDocument();
    expect(screen.getByText(/reason unavailable/)).toBeInTheDocument();
  });
});
