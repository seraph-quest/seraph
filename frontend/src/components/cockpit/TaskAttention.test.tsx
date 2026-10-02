import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { TaskApprovalReview } from "./TaskApprovalReview";
import { TaskEffectRecovery } from "./TaskEffectRecovery";
import { AttentionList } from "./AttentionList";
import { useAttentionNavigation } from "../../hooks/useAttentionNavigation";
import type { AttentionItem, AttentionOwner } from "../../lib/cockpitAttention";
import type { WorkBoardTask } from "../../types";

const owner = { principalId: "owner", sessionId: "root" };
const task = { task_id: "task", task_revision: 4, goal_id: "goal", goal_revision: 2, owner_principal_id: "owner", owner_session_id: "root", title: "Review source", status: "blocked", capability_id: "work.github-followthrough.v1", block_kind: "unknown_effect", recovery_action: "reconcile_external_effect", latest_attempt: { attempt_id: "attempt", workflow_run_id: "run" } } as WorkBoardTask;
const approval = { id: "approval", status: "pending", owner_principal_id: "owner", operator_session_id: "root", session_id: "root", conversation_id: "root", summary: "Review the bounded destination", expires_at: "2099-01-01T00:00:00Z", approval_scope: { action: "create_issue", target: { provider: "github", repository: "example/repo" } }, approval_context: { authority: { job_id: "run", goal_id: "goal", goal_revision: 2 } } };
const job = { job_id: "run", operation_id: "operation", goal_id: "goal", goal_revision: 2, status: "unknown_external_effect", effects: [] };
const item: AttentionItem = { id: "task:task", kind: "task", taskId: "task", approvalId: "approval", title: "Review source", reason: "Needs approval", updatedAt: "2026-10-01T10:00:00Z", goalId: "goal", threadId: "thread", recoveryAction: "approve_existing_run", readOnly: false, metadataConfirmed: true, priority: 0 };
function response(payload: unknown, status = 200) { return { ok: status === 200, status, json: async () => payload }; }

describe("same-task attention recovery", () => {
  const fetchMock = vi.fn();
  beforeEach(() => { fetchMock.mockReset(); vi.stubGlobal("fetch", fetchMock); });
  afterEach(() => vi.unstubAllGlobals());

  it("revalidates exact approval before and after decision without resuming or claiming task completion", async () => {
    const refresh = vi.fn().mockResolvedValue(undefined);
    fetchMock.mockImplementation((url: string) => Promise.resolve(response(url.includes("/api/work-board/") ? { task } : url.includes("/pending?") ? [approval] : { ...approval, approval_id: "approval", status: "approved", resume_message: "NEVER_AUTO_SEND" })));
    render(<TaskApprovalReview task={task} owner={owner} approvalId="approval" metadataConfirmed onRefresh={refresh} />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Approve exact action" })).toBeEnabled());
    await userEvent.click(screen.getByRole("button", { name: "Approve exact action" }));
    expect(await screen.findByText(/Exact approval approved/)).toBeInTheDocument();
    expect(refresh).toHaveBeenCalledOnce();
    const writes = fetchMock.mock.calls.filter(([, init]) => init?.method === "POST");
    expect(writes).toHaveLength(1);
    expect(writes[0][0]).toContain("/api/approvals/approval/approve");
    expect(fetchMock.mock.calls.filter(([url]) => String(url).includes("/api/work-board/tasks/task"))).toHaveLength(3);
    expect(screen.queryByText("NEVER_AUTO_SEND")).not.toBeInTheDocument();
  });

  it("withholds decision when attempt changes after the cached approval was displayed", async () => {
    let changed = false;
    fetchMock.mockImplementation((url: string) => Promise.resolve(response(url.includes("/api/work-board/") ? { task: changed ? { ...task, latest_attempt: { ...task.latest_attempt, workflow_run_id: "new-run" } } : task } : [approval])));
    render(<TaskApprovalReview task={task} owner={owner} metadataConfirmed onRefresh={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Approve exact action" })).toBeEnabled());
    changed = true; fireEvent.click(screen.getByRole("button", { name: "Approve exact action" }));
    expect(await screen.findByText(/attempt or owner changed/)).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
  });

  it.each(["expired", "foreign", "missing", "degraded"])("fails closed on %s approval metadata", async (failure) => {
    fetchMock.mockImplementation((url: string) => Promise.resolve(url.includes("/api/work-board/") ? response({ task }) : response(failure === "missing" ? [] : [{ ...approval, ...(failure === "expired" ? { expires_at: "2000-01-01T00:00:00Z" } : failure === "foreign" ? { operator_session_id: "other" } : {}) }], failure === "degraded" ? 503 : 200)));
    render(<TaskApprovalReview task={task} owner={owner} metadataConfirmed onRefresh={vi.fn()} />);
    await screen.findByRole("status");
    expect(screen.getByRole("button", { name: "Approve exact action" })).toBeDisabled();
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
  });

  it("retains readonly metadata without requesting approval or readback", () => {
    render(<TaskApprovalReview task={{ ...task, ownership_access: "recovered_read_only" }} owner={owner} metadataConfirmed onRefresh={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Approve exact action" })).toBeDisabled();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("reconciles only the originally bound job and displays actual verified receipt without sending or retrying", async () => {
    const refresh = vi.fn().mockResolvedValue(undefined);
    fetchMock.mockImplementation((url: string) => Promise.resolve(response(url.includes("/api/work-board/") ? { task } : url.endsWith("/reconcile") ? { ...job, status: "succeeded", effects: [{ effect_type: "github_publication", status: "succeeded", details: { verified: true } }] } : job)));
    render(<TaskEffectRecovery task={task} owner={owner} metadataConfirmed onRefresh={refresh} />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Reconcile recorded GitHub effect" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Reconcile recorded GitHub effect" }));
    expect(await screen.findByText(/Independent readback is verified/)).toHaveTextContent("Task completion remains the board's confirmed state");
    const writes = fetchMock.mock.calls.filter(([, init]) => init?.method === "POST");
    expect(writes).toHaveLength(1); expect(writes[0][0]).toContain("/api/capabilities/github/jobs/run/reconcile"); expect(writes[0][1].body).toBe("{}");
    expect(refresh).toHaveBeenCalledOnce();
  });

  it("keeps unverified readback unknown and never retries after a failed recovery fetch", async () => {
    let phase = 0;
    fetchMock.mockImplementation((url: string) => Promise.resolve(response(url.includes("/api/work-board/") ? { task } : job, phase && url.endsWith("/reconcile") ? 503 : 200)));
    render(<TaskEffectRecovery task={task} owner={owner} metadataConfirmed onRefresh={vi.fn()} />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Reconcile recorded GitHub effect" })).toBeEnabled());
    phase = 1; fireEvent.click(screen.getByRole("button", { name: "Reconcile recorded GitHub effect" }));
    expect(await screen.findByText(/Recovery readback unavailable/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reconcile recorded GitHub effect" })).toBeDisabled();
    expect(fetchMock.mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
  });

  it("rejects a spliced owning job and offers no speculative reconciliation for other capabilities", async () => {
    fetchMock.mockImplementation((url: string) => Promise.resolve(response(url.includes("/api/work-board/") ? { task } : { ...job, goal_id: "other-goal" })));
    const view = render(<TaskEffectRecovery task={task} owner={owner} metadataConfirmed onRefresh={vi.fn()} />);
    expect(await screen.findByText(/not bound to this exact task and goal/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reconcile recorded GitHub effect" })).toBeDisabled();
    view.rerender(<TaskEffectRecovery task={{ ...task, capability_id: "another.capability" }} owner={owner} metadataConfirmed onRefresh={vi.fn()} />);
    expect(screen.getByText(/no supported readback control/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reconcile recorded GitHub effect" })).not.toBeInTheDocument();
  });

  it("supports keyboard activation and restored focus with wrapping scrollable controls", async () => {
    const open = vi.fn();
    const view = render(<AttentionList items={[item]} confirmedAt="confirmed" available onOpen={open} focusItemId="task:task" />);
    const button = screen.getByRole("button", { name: /Review source/ });
    expect(button).toHaveFocus(); await userEvent.keyboard("{Enter}"); expect(open).toHaveBeenCalledWith(item);
    view.rerender(<AttentionList items={[]} confirmedAt="confirmed" available onOpen={open} focusItemId="task:task" />);
    expect(screen.getByLabelText("Attention snapshot")).toHaveFocus();
  });
});

function NavigationHarness({ scope }: { scope: AttentionOwner }) {
  const navigation = useAttentionNavigation(scope);
  return <><button onClick={() => navigation.fromHome(item)}>Open task</button><button onClick={() => navigation.returnContext()}>Return</button><span>{navigation.origin?.taskId ?? "no origin"}</span><span>{navigation.homeFocusId ?? "no focus"}</span></>;
}
it("binds return context to authenticated root and discards it on root transition", async () => {
  const view = render(<NavigationHarness scope={owner} />);
  fireEvent.click(screen.getByText("Open task")); expect(screen.getByText("task")).toBeInTheDocument();
  fireEvent.click(screen.getByText("Return")); expect(screen.getByText("task:task")).toBeInTheDocument();
  await act(async () => view.rerender(<NavigationHarness scope={{ ...owner, sessionId: "new-root" }} />));
  expect(screen.getByText("no origin")).toBeInTheDocument(); expect(screen.getByText("no focus")).toBeInTheDocument();
});
