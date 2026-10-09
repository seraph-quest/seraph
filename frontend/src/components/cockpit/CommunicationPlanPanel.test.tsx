import { StrictMode } from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { apiFetch } from "../../lib/api";
import { CommunicationPlanPanel } from "./CommunicationPlanPanel";
import type { GoalInfo, WorkBoardTask } from "../../types";
vi.mock("../../lib/api", () => ({ apiFetch: vi.fn() }));
vi.mock("./MailReplySendPanel", () => ({ MailReplySendPanel: (props: { taskId: string; onExactApproved: (v: unknown) => void; onReadback?: (v: { status: string; outcome: string }) => void }) => <div aria-label={`Native reply ${props.taskId}`}><button onClick={() => props.onExactApproved({ operation_id: `send:${props.taskId}`, exact_preview_digest: "c".repeat(64), approval_id: `approval:${props.taskId}`, expires_at: Date.now() / 1000 + 300 })}>Native exact approval {props.taskId}</button><button onClick={() => props.onReadback?.({ status: "completed", outcome: "sent" })}>Native sent readback {props.taskId}</button></div> }));
vi.mock("./CalendarReschedulePanel", () => ({ CalendarReschedulePanel: () => <div>Native exact calendar controls</div> }));
const owner = { ownerPrincipalId: "operator", ownerSessionId: "session" };
const goal = { id: "goal", title: "Owned Goal", revision: 3, status: "active", owner_session_id: "session" } as GoalInfo;
const task = { task_id: "communication-task", task_revision: 2, goal_id: "goal", goal_revision: 3, capability_id: "agent.task.v1", owner_principal_id: "operator", owner_session_id: "session", status: "done" } as WorkBoardTask;
const source = { source_id: "mail", capability_id: "work.mail-reply-draft.v1", source_revision: "message-revision", source_input_digest: "a".repeat(64), task_id: "source-one", attempt_id: "attempt", job_id: "job", artifact_path: "private.json", artifact_digest: "b".repeat(64), readback_id: "readback" };
const wire = { task_id: task.task_id, no_learning: true, plan: { source_refs: [source], reply_drafts: [{ source_ref: source, subject: "Private subject", body: "<script>inert private body</script>", caveats: [] }], meeting_preparations: [], reschedule_proposals: [], unresolved_questions: [] } };
const response = (v: unknown, status = 200) => new Response(JSON.stringify(v), { status });
beforeEach(() => { vi.mocked(apiFetch).mockReset(); sessionStorage.clear(); });
it("opens authenticated private content explicitly and mounts only the selected native reply", async () => {
  const other = { ...source, source_input_digest: "d".repeat(64), task_id: "source-two" };
  vi.mocked(apiFetch).mockResolvedValue(response({ ...wire, plan: { ...wire.plan, source_refs: [source, other], reply_drafts: [...wire.plan.reply_drafts, { source_ref: other, subject: "Other subject", body: "Other body", caveats: [] }] } }));
  render(<CommunicationPlanPanel {...owner} task={task} goals={[goal]} />);
  expect(apiFetch).not.toHaveBeenCalled();
  fireEvent.click(screen.getByText("Open current private communication plan"));
  expect(await screen.findByText("<script>inert private body</script>")).toBeInTheDocument();
  expect(document.querySelector("script")).toBeNull();
  expect(screen.queryByLabelText("Native reply source-one")).toBeNull();
  fireEvent.click(screen.getAllByText("Select this reply for independent exact review")[0]);
  expect(screen.getByLabelText("Native reply source-one")).toBeInTheDocument();
  expect(screen.queryByLabelText("Native reply source-two")).toBeNull();
  fireEvent.click(screen.getByText("Native exact approval source-one"));
  vi.mocked(apiFetch).mockImplementation(async (_url, init) => response({ task_id: task.task_id, no_learning: true, bundle: JSON.parse(String(init?.body)) }));
  fireEvent.click(screen.getByText("Validate selected independent exact approvals"));
  await screen.findByText(/Selected exact approvals are current/);
  const calls = vi.mocked(apiFetch).mock.calls;
  expect(calls).toHaveLength(2);
  expect(JSON.parse(String(calls[1][1]?.body))).toEqual({ selected_actions: [{ kind: "reply", source_input_digest: source.source_input_digest, operation_id: "send:source-one" }], exact_preview_digests: ["c".repeat(64)], approval_ids: ["approval:source-one"] });
  expect(calls.every(([url]) => !String(url).includes("execute"))).toBe(true);
});
it("discards old content on private GET revocation and Goal revision change", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response(wire)).mockResolvedValue(response({}, 403));
  const view = render(<CommunicationPlanPanel {...owner} task={task} goals={[goal]} />);
  fireEvent.click(screen.getByText("Open current private communication plan")); await screen.findByText("Private subject");
  fireEvent.click(screen.getByText("Open current private communication plan")); await screen.findByRole("alert");
  expect(screen.queryByText("Private subject")).toBeNull();
  view.rerender(<CommunicationPlanPanel {...owner} task={task} goals={[{ ...goal, revision: 4 }]} />);
  expect(screen.queryByText("Private subject")).toBeNull();
});
it("retains the identical uncertain create request with no automatic replacement", async () => {
  vi.mocked(apiFetch).mockRejectedValueOnce(Error("transport unconfirmed")).mockImplementation(async (_url, init) => {
    const body = JSON.parse(String(init?.body)); return response({ task: { ...task, idempotency_key: body.idempotency_key }, idempotent_replay: true });
  });
  const created = vi.fn(); render(<CommunicationPlanPanel {...owner} goals={[goal]} onCreated={created} />);
  fireEvent.change(screen.getByLabelText("Communication Goal"), { target: { value: "goal" } });
  fireEvent.click(screen.getByText("I reviewed these selected sources, finite cost limit and private preparation."));
  fireEvent.click(screen.getByText("Prepare one communication task")); await screen.findByRole("alert");
  expect(apiFetch).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByText("Reconcile exact communication request")); await waitFor(() => expect(created).toHaveBeenCalledTimes(1));
  expect(vi.mocked(apiFetch).mock.calls[0][1]?.body).toBe(vi.mocked(apiFetch).mock.calls[1][1]?.body);
});
it("fences late private responses after StrictMode unmount and fresh remount", async () => {
  let resolve!: (r: Response) => void;
  vi.mocked(apiFetch).mockImplementationOnce(() => new Promise(r => { resolve = r; })).mockResolvedValue(response(wire));
  const old = render(<StrictMode><CommunicationPlanPanel {...owner} task={task} goals={[goal]} /></StrictMode>);
  fireEvent.click(screen.getByText("Open current private communication plan"));
  const signal = vi.mocked(apiFetch).mock.calls[0][1]?.signal;
  old.unmount(); expect(signal?.aborted).toBe(true); resolve(response(wire));
  render(<StrictMode><CommunicationPlanPanel {...owner} task={task} goals={[goal]} /></StrictMode>);
  expect(screen.queryByText("Private subject")).toBeNull();
  fireEvent.click(screen.getByText("Open current private communication plan")); await screen.findByText("Private subject");
  expect(vi.mocked(apiFetch).mock.calls[1][1]?.signal).not.toBe(signal);
});
it("blur discards private content and fences a still-pending private read", async () => {
  let resolve!: (r: Response) => void;
  vi.mocked(apiFetch).mockImplementationOnce(() => new Promise(r => { resolve = r; }));
  render(<CommunicationPlanPanel {...owner} task={task} goals={[goal]} />);
  fireEvent.click(screen.getByText("Open current private communication plan")); fireEvent(window, new Event("blur"));
  resolve(response(wire)); await waitFor(() => expect(screen.queryByText("Private subject")).toBeNull());
  expect(vi.mocked(apiFetch).mock.calls[0][1]?.signal?.aborted).toBe(true);
});
it("creates only the explicitly selected source subset with the original native grammar", async () => {
  const created = vi.fn();
  const reply = { schema_version: 1 as const, connection_id: "read-connection", expected_connection_revision: 2,
    message_binding_id: "message-one", expected_message_revision: "original-revision", mail_consent_id: "consent", expected_source_consent_revision: 2,
    expected_model_consent_revision: 3, goal_id: "goal", expected_goal_revision: 3, reply_intent: "Confirm the meeting", style: "brief" as const };
  vi.mocked(apiFetch).mockImplementation(async (_url, init) => { const body = JSON.parse(String(init?.body)); return response({ task: { ...task, idempotency_key: body.idempotency_key } }); });
  render(<CommunicationPlanPanel {...owner} goals={[goal]} onCreated={created} selection={{ reply_inputs: [reply, { ...reply, message_binding_id: "message-two" }], meeting_inputs: [], reschedule_inputs: [] }} />);
  fireEvent.change(screen.getByLabelText("Communication Goal"), { target: { value: "goal" } });
  fireEvent.click(screen.getByLabelText("Reply source message-two"));
  fireEvent.click(screen.getByText("I reviewed these selected sources, finite cost limit and private preparation."));
  fireEvent.click(screen.getByText("Prepare one communication task")); await waitFor(() => expect(created).toHaveBeenCalledTimes(1));
  expect(JSON.parse(String(vi.mocked(apiFetch).mock.calls[0][1]?.body)).selection).toEqual({ reply_inputs: [reply], meeting_inputs: [], reschedule_inputs: [], acknowledge_private_review: true });
});
it("discards the private plan at its original task cutoff", async () => {
  vi.mocked(apiFetch).mockResolvedValue(response(wire));
  render(<CommunicationPlanPanel {...owner} task={task} goals={[goal]} deadlineAt={new Date(Date.now() + 100).toISOString()} />);
  fireEvent.click(screen.getByText("Open current private communication plan")); await screen.findByText("Private subject");
  await screen.findByText(/original preparation cutoff has passed/);
  expect(screen.queryByText("Private subject")).toBeNull();
});


async function openPrivatePlan() {
  fireEvent.click(screen.getByText("Open current private communication plan"));
  await screen.findByText("Private subject");
}
const cleanupVerified = { status: "cleanup_verified", absent: true, no_learning: true };
const cleanupUnresolved = { status: "cleanup_unresolved", absent: false, no_learning: true };

it("shows exact aggregate retention limits and cleans up only on explicit positive absence", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response(wire)).mockResolvedValueOnce(response(cleanupVerified));
  render(<CommunicationPlanPanel {...owner} task={task} goals={[goal]} />); await openPrivatePlan();
  expect(screen.getByLabelText("Private plan retention limitation")).toHaveTextContent("Access expiry does not delete retained artifacts");
  expect(screen.getByLabelText("Private plan retention limitation")).toHaveTextContent("original Mail drafts, Calendar briefs");
  expect(apiFetch).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByText("Clean up aggregate private plan"));
  await screen.findByLabelText("Aggregate private plan cleanup result");
  expect(screen.queryByText("Private subject")).toBeNull();
  expect(screen.getByLabelText("Aggregate private plan cleanup result")).toHaveTextContent("absence verified");
  const [url, init] = vi.mocked(apiFetch).mock.calls[1];
  expect(String(url)).toMatch(/\/tasks\/communication-task\/communications\/cleanup$/);
  expect(init?.method).toBe("POST"); expect(JSON.parse(String(init?.body))).toEqual({ expected_task_revision: 2 });
  expect(apiFetch).toHaveBeenCalledTimes(2);
});

it("retains private content and factual native readback when absence is unresolved", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response(wire)).mockResolvedValueOnce(response(cleanupUnresolved));
  render(<CommunicationPlanPanel {...owner} task={task} goals={[goal]} />); await openPrivatePlan();
  fireEvent.click(screen.getByLabelText("Select this reply for independent exact review"));
  fireEvent.click(screen.getByText("Native sent readback source-one"));
  expect(screen.getByText("Reply result: completed · sent")).toBeInTheDocument();
  fireEvent.click(screen.getByText("Clean up aggregate private plan"));
  await screen.findByLabelText("Aggregate private plan cleanup result");
  expect(screen.getByText("Private subject")).toBeInTheDocument();
  expect(screen.getByText("Reply result: completed · sent")).toBeInTheDocument();
  expect(screen.getByLabelText("Aggregate private plan cleanup result")).toHaveTextContent("Physical absence is not verified");
  expect(screen.queryByText(/absence verified\./)).toBeNull(); expect(apiFetch).toHaveBeenCalledTimes(2);
});

it.each(["lost response", "unavailable service", "inconsistent absence", "extra binding", "producer not closed"])("retains private content and original cleanup after %s", async mutation => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response(wire));
  if (mutation === "lost response") vi.mocked(apiFetch).mockRejectedValueOnce(Error("response lost"));
  else if (mutation === "unavailable service") vi.mocked(apiFetch).mockResolvedValueOnce(response({}, 503));
  else if (mutation === "producer not closed") vi.mocked(apiFetch).mockResolvedValueOnce(response({}, 409));
  else if (mutation === "extra binding") vi.mocked(apiFetch).mockResolvedValueOnce(response({ ...cleanupVerified, task_id: "other-task" }));
  else vi.mocked(apiFetch).mockResolvedValueOnce(response({ ...cleanupVerified, absent: false }));
  render(<CommunicationPlanPanel {...owner} task={task} goals={[goal]} />); await openPrivatePlan();
  fireEvent.click(screen.getByText("Clean up aggregate private plan")); await screen.findByRole("alert");
  expect(screen.getByText("Private subject")).toBeInTheDocument();
  expect(screen.queryByLabelText("Aggregate private plan cleanup result")).toBeNull();
  expect(screen.getByText("Reconcile exact communication cleanup")).toBeEnabled();
  expect(screen.getByLabelText("Select this reply for independent exact review")).toBeDisabled();
  expect(apiFetch).toHaveBeenCalledTimes(2);
  const original = vi.mocked(apiFetch).mock.calls[1];
  vi.mocked(apiFetch).mockResolvedValueOnce(response(cleanupVerified));
  fireEvent.click(screen.getByText("Reconcile exact communication cleanup"));
  await screen.findByLabelText("Aggregate private plan cleanup result");
  expect(vi.mocked(apiFetch).mock.calls[2][0]).toBe(original[0]); expect(vi.mocked(apiFetch).mock.calls[2][1]?.body).toBe(original[1]?.body);
  expect(vi.mocked(apiFetch).mock.calls.every(([url]) => !/execute|send|reschedule|general-tasks/.test(String(url)))).toBe(true);
});

it.each([401, 403, 404, 410])("clears denied private content without inventing deletion on %s", async status => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response(wire)).mockResolvedValueOnce(response({}, status));
  render(<CommunicationPlanPanel {...owner} task={task} goals={[goal]} />); await openPrivatePlan();
  fireEvent.click(screen.getByText("Clean up aggregate private plan")); await screen.findByRole("alert");
  expect(screen.queryByText("Private subject")).toBeNull();
  expect(screen.getByRole("alert")).toHaveTextContent("physical deletion has not been verified");
  expect(screen.queryByLabelText("Aggregate private plan cleanup result")).toBeNull();
  expect(screen.queryByText("Reconcile exact communication cleanup")).toBeNull();
});

it.each(["owner", "task revision", "Goal revision", "unmount", "blur"])("fences a delayed cleanup receipt after %s changes", async mutation => {
  let finish!: (value: Response) => void;
  vi.mocked(apiFetch).mockResolvedValueOnce(response(wire)).mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  const view = render(<CommunicationPlanPanel {...owner} task={task} goals={[goal]} />); await openPrivatePlan();
  fireEvent.click(screen.getByText("Clean up aggregate private plan"));
  expect(screen.getByText("Private subject")).toBeInTheDocument();
  const signal = vi.mocked(apiFetch).mock.calls[1][1]?.signal;
  if (mutation === "owner") view.rerender(<CommunicationPlanPanel ownerPrincipalId="foreign" ownerSessionId="foreign" task={task} goals={[goal]} />);
  if (mutation === "task revision") view.rerender(<CommunicationPlanPanel {...owner} task={{ ...task, task_revision: 3 }} goals={[goal]} />);
  if (mutation === "Goal revision") view.rerender(<CommunicationPlanPanel {...owner} task={task} goals={[{ ...goal, revision: 4 }]} />);
  if (mutation === "unmount") view.unmount();
  if (mutation === "blur") fireEvent(window, new Event("blur"));
  await act(async () => { finish(response(cleanupVerified)); });
  expect(signal?.aborted).toBe(true); expect(screen.queryByLabelText("Aggregate private plan cleanup result")).toBeNull();
  expect(screen.queryByText("Private subject")).toBeNull(); expect(apiFetch).toHaveBeenCalledTimes(2);
});

it("times out cleanup without discarding private content or making an automatic retry", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response(wire)).mockImplementationOnce(() => new Promise<Response>(() => {}));
  render(<CommunicationPlanPanel {...owner} task={task} goals={[goal]} />); await openPrivatePlan();
  vi.useFakeTimers(); fireEvent.click(screen.getByText("Clean up aggregate private plan"));
  await act(async () => { await vi.advanceTimersByTimeAsync(6500); });
  expect(screen.getByRole("alert")).toHaveTextContent("timed out"); expect(screen.getByText("Private subject")).toBeInTheDocument();
  expect(screen.getByText("Reconcile exact communication cleanup")).toBeEnabled(); expect(apiFetch).toHaveBeenCalledTimes(2); vi.useRealTimers();
});
