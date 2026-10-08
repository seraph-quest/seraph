import { fireEvent, render, screen, waitFor, cleanup } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SessionList } from "./SessionList";

const mocks = vi.hoisted(() => ({ fetch: vi.fn(), read: vi.fn(), continue: vi.fn(),
  load: vi.fn(), switch: vi.fn(), clear: vi.fn(), newSession: vi.fn(), delete: vi.fn(), rename: vi.fn() }));
vi.mock("../../lib/api", () => ({ apiFetch: mocks.fetch }));
vi.mock("../../lib/taskContinuity", () => ({ readTaskContext: mocks.read, continueTask: mocks.continue }));
vi.mock("../../stores/chatStore", () => ({ useChatStore: (selector: (value: unknown) => unknown) => selector({
  sessions: [], sessionId: null, sessionContinuity: {}, loadSessions: mocks.load,
  switchSession: mocks.switch, clearSessionContinuity: mocks.clear, newSession: mocks.newSession,
  deleteSession: mocks.delete, renameSession: mocks.rename,
}) }));

const packet = { task_id: "task-one", goal_id: "goal-one", revision: 7, status: "blocked",
  summary: "Task is blocked; 1 verified output reference(s).", summary_kind: "factual_canonical_timeline",
  conversation_ids: [], verified_artifact_refs: ["opaque-output"], private_source_refs: ["opaque-private-source"],
  open_questions: ["What input remains?"], next_actions: ["Review current scope in Work before any execution or egress"],
  unresolved_effect: "unknown_external_effect", ownership_access: "recovered_read_only", evidence_state: "available", truncated: false,
  assistant_context_state: "current_scope_review_required", source_egress: [{ source_id: "opaque-private-source", private_source: true, model_context_allowed: false }] };

beforeEach(() => {
  vi.resetAllMocks();
  mocks.fetch.mockResolvedValue({ ok: true, json: async () => ({ tasks: [{ task_id: "task-one", title: "Same durable task" }] }) });
  mocks.read.mockResolvedValue(packet);
  mocks.continue.mockResolvedValue("new-owned-chat");
});
afterEach(cleanup);

async function chooseTask() {
  render(<SessionList />);
  await screen.findByRole("option", { name: "Same durable task" });
  fireEvent.change(screen.getByRole("combobox", { name: "Task to continue" }), { target: { value: "task-one" } });
  await screen.findByText(packet.summary);
}

describe("task conversation continuity", () => {
  it("shows recovered read-only context, unanswered input, permitted action and Unknown separately", async () => {
    await chooseTask();
    expect(screen.getByText(/Recovered history is read-only/)).toBeInTheDocument();
    expect(screen.getByText(/Unanswered questions: What input remains/)).toBeInTheDocument();
    expect(screen.getByText(/Next permitted action: Review current scope/)).toBeInTheDocument();
    expect(screen.getByText(/Unresolved effect: Unknown/)).toBeInTheDocument();
    expect(screen.queryByText("opaque-private-source")).not.toBeInTheDocument();
  });

  it("continues the exact task revision in a new owned chat", async () => {
    await chooseTask();
    fireEvent.click(screen.getByRole("button", { name: "Continue in new chat" }));
    await waitFor(() => expect(mocks.switch).toHaveBeenCalledWith("new-owned-chat", "restored"));
    expect(mocks.continue).toHaveBeenCalledWith(packet, expect.any(String));
    expect(mocks.load).toHaveBeenCalled();
    expect(mocks.newSession).not.toHaveBeenCalled();
  });

  it("reloads a stale revision without replaying the former chat action", async () => {
    await chooseTask();
    mocks.continue.mockRejectedValue(new Error("task_context_revision_stale"));
    fireEvent.click(screen.getByRole("button", { name: "Continue in new chat" }));
    await screen.findByRole("alert");
    expect(mocks.switch).not.toHaveBeenCalled();
    mocks.read.mockResolvedValue({ ...packet, revision: 8 });
    fireEvent.click(screen.getByRole("button", { name: "Reload task context" }));
    await screen.findByText(packet.summary);
    expect(mocks.continue).toHaveBeenCalledTimes(1);
  });

  it("keeps controls usable through unavailable task metadata", async () => {
    mocks.fetch.mockResolvedValue({ ok: false, json: async () => ({}) });
    render(<SessionList />);
    await screen.findByRole("alert");
    expect(screen.getByRole("button", { name: "+ New Chat" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Reload task context" })).toBeEnabled();
  });
});
