import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { beforeEach, expect, it, vi } from "vitest";
import { apiFetch } from "../../lib/api";
import { TaskLessonReview } from "./TaskLessonReview";
import { ResearchMethodProposalForm } from "./ResearchMethodProposalForm";
import { validateResearchStrategy } from "../../lib/researchMethods";
import type { ResearchSource, ResearchStrategy } from "../../lib/researchMethods";
import type { WorkBoardTask } from "../../types";

// Explicit finite API payloads test real React/request bindings; the backend genuine dossier fixture proves source authority.
vi.mock("../../lib/api", () => ({ apiFetch: vi.fn() }));
const owner = { ownerPrincipalId: "operator", ownerSessionId: "session" };
const task = { task_id: "task", task_revision: 3, goal_id: "goal", goal_revision: 2, capability_id: "work.research-dossier.v1", owner_principal_id: "operator", owner_session_id: "session", status: "done", latest_attempt: { attempt_id: "attempt" } } as WorkBoardTask;
const scope = { goal_id: "goal", goal_revision: 2, family: "research" as const };
const source: ResearchSource = { task_id: "task", expected_revision: 3, attempt_id: "attempt", source_refs: ["artifact:verified", "readback:verified"], scope, eligible: true, source_current: true, supported_candidate_kind: "research_strategy", reason_code: "verified_completed_research_strategy_source", observed: { status: "completed", readback_digest: "c".repeat(64) } };
const strategy: ResearchStrategy = { schema_version: "ResearchStrategy.v1", query_templates: ["official dated release evidence"], source_preferences: ["official"], required_evidence_fields: ["date"], draft_sections: ["Evidence"], stop_conditions: ["Stop without attributed evidence"] };
const lesson = { schema_version: "task_method_proposal.v1", proposal_id: "proposal", task_id: "task", attempt_id: "attempt", revision: 1, status: "proposed", result: "candidate_inert", reason_code: "explicit_structured_research_method", scope, behavior_changed: false, source_current: true, correction: "", source_refs: source.source_refs, observed: source.observed, old_method: null, new_method: strategy };
const canonical = { proposal_id: "proposal", task_id: task.task_id, attempt_id: source.attempt_id, source_refs: source.source_refs, observed: source.observed, expected_revision: 1, artifact_digest: "a".repeat(64), scope_digest: "b".repeat(64), scope, old_method: null, new_method: strategy, active_binding: null, quality_evidence: "unmeasured", adoption_requires_current_owner: false, configured_baseline: false };
const response = (v: unknown, status = 200) => new Response(JSON.stringify(v), { status });
const fieldAck = "I reviewed these exact structured fields against the verified dossier evidence.";
beforeEach(() => { vi.mocked(apiFetch).mockReset(); });
function fields() {
  fireEvent.click(screen.getByRole("button", { name: "Add query template" }));
  fireEvent.change(screen.getByLabelText("Query template 1"), { target: { value: strategy.query_templates[0] } });
  fireEvent.click(screen.getByLabelText("official")); fireEvent.click(screen.getByLabelText("date"));
  fireEvent.click(screen.getByRole("button", { name: "Add draft section" }));
  fireEvent.change(screen.getByLabelText("Draft section 1"), { target: { value: "Evidence" } });
  fireEvent.change(screen.getByLabelText("Stop condition 1"), { target: { value: strategy.stop_conditions[0] } });
  fireEvent.click(screen.getByLabelText(fieldAck));
}

it("completes POST and private GET with live AbortSignals after StrictMode's effect probe", async () => {
  const signals: AbortSignal[] = [], candidate = vi.fn();
  vi.mocked(apiFetch).mockImplementation((_url, init) => new Promise((resolve, reject) => {
    const signal = init?.signal as AbortSignal;
    signals.push(signal);
    if (signal.aborted) { reject(new DOMException("Request aborted", "AbortError")); return; }
    signal.addEventListener("abort", () => reject(new DOMException("Request aborted", "AbortError")), { once: true });
    queueMicrotask(() => { if (!signal.aborted) resolve(response(lesson)); });
  }));
  const view = render(<StrictMode><ResearchMethodProposalForm task={task} source={source} owned onCandidate={candidate} onStale={vi.fn()} /></StrictMode>);
  fields(); fireEvent.click(screen.getByRole("button", { name: "Prepare private research strategy" }));
  await waitFor(() => expect(candidate).toHaveBeenCalledWith(lesson));
  expect(apiFetch).toHaveBeenCalledTimes(2);
  expect(signals.every(signal => !signal.aborted)).toBe(true);
  view.unmount(); expect(signals.every(signal => signal.aborted)).toBe(true);
});

it("connects verified dossier evidence through bounded fields, exact private candidate, canonical provenance and separate adoption/rollback", async () => {
  vi.mocked(apiFetch).mockImplementation(async (url, init) => {
    const path = String(url);
    if (path.includes("/sources/")) return response(source);
    if (path.endsWith("/research-methods")) return response(lesson);
    if (path.endsWith("/task-lessons/proposal")) return response(lesson);
    if (path.endsWith("/task-methods/proposal")) return response(canonical);
    if (path.endsWith("/task-methods/actions")) return response({ status: JSON.parse(String(init?.body)).action === "accept" ? "accepted" : "rolled_back" });
    throw Error("Unexpected finite API request");
  });
  render(<TaskLessonReview {...owner} task={task} />);
  fireEvent.click(screen.getByRole("button", { name: "Inspect lesson sources" }));
  await screen.findByRole("region", { name: "Structured research method proposal" });
  expect(screen.queryByLabelText("Private task correction")).toBeNull();
  expect(screen.getByRole("button", { name: "Prepare private research strategy" })).toBeDisabled();
  fields(); fireEvent.click(screen.getByRole("button", { name: "Prepare private research strategy" }));
  await screen.findByRole("region", { name: "Exact private lesson change" });
  const body = JSON.parse(String(vi.mocked(apiFetch).mock.calls[1][1]?.body));
  expect(body).toEqual({ task_id: source.task_id, attempt_id: source.attempt_id, source_refs: source.source_refs, scope, expected_revision: 3, strategy });
  expect(screen.getByRole("region", { name: "Private candidate source provenance" })).toHaveTextContent("Source Task task · Attempt attempt · observation completed");
  expect(screen.getByRole("region", { name: "Private candidate source provenance" })).toHaveTextContent("readback:verified");
  expect(screen.queryByRole("button", { name: "Adopt reviewed method" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Inspect canonical method and scope" }));
  await screen.findByRole("region", { name: "Canonical method source provenance" });
  expect(screen.getByRole("region", { name: "Canonical method source provenance" })).toHaveTextContent("artifact:verified");
  expect(screen.getByRole("button", { name: "Adopt reviewed method" })).toBeDisabled();
  fireEvent.click(screen.getByLabelText("I reviewed this exact method and verified source evidence."));
  fireEvent.click(screen.getByRole("button", { name: "Adopt reviewed method" }));
  await screen.findByText(/Review recorded/);
  expect(JSON.parse(String(vi.mocked(apiFetch).mock.calls[4][1]?.body))).toMatchObject({ proposal_id: "proposal", expected_revision: 1, artifact_digest: canonical.artifact_digest, scope_digest: canonical.scope_digest, action: "accept" });
  fireEvent.click(screen.getByRole("button", { name: "Inspect canonical method and scope" }));
  await screen.findByLabelText("Canonical proposed method");
  fireEvent.change(screen.getByLabelText("Review reason"), { target: { value: "Use future baseline" } });
  fireEvent.click(screen.getByRole("button", { name: "Rollback to baseline" }));
  await waitFor(() => expect(JSON.parse(String(vi.mocked(apiFetch).mock.calls[6][1]?.body)).action).toBe("rollback"));
});

it("renders original source provenance when the structured proposal is reopened without source state", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response(lesson));
  render(<TaskLessonReview {...owner} task={task} proposalId="proposal" />);
  const region = await screen.findByRole("region", { name: "Private candidate source provenance" });
  expect(region).toHaveTextContent("Source Task task · Attempt attempt"); expect(region).toHaveTextContent("readback:verified");
  expect(screen.queryByRole("region", { name: "Structured research method proposal" })).toBeNull();
});

it.each(["source", "attempt", "kind"])("blocks a stale or unsupported %s projection before showing the research form", async field => {
  const bad = { ...source, ...(field === "source" ? { source_current: false } : field === "attempt" ? { attempt_id: "other" } : { supported_candidate_kind: null }) };
  vi.mocked(apiFetch).mockResolvedValue(response(bad));
  render(<TaskLessonReview {...owner} task={task} />);
  fireEvent.click(screen.getByRole("button", { name: "Inspect lesson sources" }));
  await screen.findByRole("alert"); expect(screen.queryByRole("region", { name: "Structured research method proposal" })).toBeNull();
});

it("denies recovered source inspection and keeps a changed private candidate read-only", async () => {
  const view = render(<TaskLessonReview {...owner} task={{ ...task, ownership_access: "recovered_read_only" }} />);
  expect(screen.getByRole("button", { name: "Inspect lesson sources" })).toBeDisabled(); expect(apiFetch).not.toHaveBeenCalled();
  view.unmount(); vi.mocked(apiFetch).mockResolvedValue(response({ ...lesson, source_current: false, status: "blocked" }));
  render(<TaskLessonReview {...owner} task={task} proposalId="proposal" />);
  await screen.findByRole("region", { name: "Private candidate source provenance" });
  expect(screen.queryByRole("button", { name: "Inspect canonical method and scope" })).toBeNull();
});

it("freezes an uncertain request, suppresses double POST and explicitly retries identical structured fields", async () => {
  let reject!: (e: Error) => void;
  vi.mocked(apiFetch).mockImplementationOnce(() => new Promise((_r, fail) => { reject = fail; })).mockResolvedValueOnce(response(lesson)).mockResolvedValueOnce(response(lesson));
  const candidate = vi.fn();
  render(<ResearchMethodProposalForm task={task} source={source} owned onCandidate={candidate} onStale={vi.fn()} />);
  fields(); const button = screen.getByRole("button", { name: "Prepare private research strategy" }); fireEvent.click(button); fireEvent.click(button);
  expect(apiFetch).toHaveBeenCalledTimes(1); reject(Error("Unconfirmed transport"));
  await screen.findByRole("alert"); expect(screen.getByLabelText("Query template 1")).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Reconcile exact research request" }));
  await waitFor(() => expect(candidate).toHaveBeenCalledWith(lesson));
  expect(vi.mocked(apiFetch).mock.calls[0][1]?.body).toBe(vi.mocked(apiFetch).mock.calls[1][1]?.body);
});

it("retries only private readback after a known candidate was created", async () => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response(lesson)).mockRejectedValueOnce(Error("Readback transport unavailable")).mockResolvedValueOnce(response(lesson));
  const candidate = vi.fn();
  render(<ResearchMethodProposalForm task={task} source={source} owned onCandidate={candidate} onStale={vi.fn()} />);
  fields(); fireEvent.click(screen.getByRole("button", { name: "Prepare private research strategy" }));
  fireEvent.click(await screen.findByRole("button", { name: "Inspect prepared research candidate" }));
  await waitFor(() => expect(candidate).toHaveBeenCalled());
  expect(vi.mocked(apiFetch).mock.calls.filter(([, init]) => init?.method === "POST")).toHaveLength(1);
});

it.each([401, 403, 409, "provenance"])("clears private fields and source after definitive known-candidate read denial %s", async status => {
  vi.mocked(apiFetch).mockResolvedValueOnce(response(lesson)).mockResolvedValueOnce(typeof status === "number" ? response({ detail: "denied" }, status) : response({ ...lesson, source_refs: ["artifact:changed"] }));
  const candidate = vi.fn(), stale = vi.fn();
  render(<ResearchMethodProposalForm task={task} source={source} owned onCandidate={candidate} onStale={stale} />);
  fields(); fireEvent.click(screen.getByRole("button", { name: "Prepare private research strategy" }));
  await waitFor(() => expect(stale).toHaveBeenCalledTimes(1));
  expect(candidate).not.toHaveBeenCalled();
  expect(screen.queryByLabelText("Query template 1")).toBeNull();
  expect(screen.getByLabelText("Stop condition 1")).toHaveValue("");
  expect(screen.queryByRole("button", { name: "Inspect prepared research candidate" })).toBeNull();
  expect(screen.queryByText(/Fields are frozen/)).toBeNull();
  expect(screen.getByRole("button", { name: "Prepare private research strategy" })).toBeDisabled();
  expect(apiFetch).toHaveBeenCalledTimes(2);
});

it.each(["unmount", "owner", "revision"])("aborts and fences late private GET after %s", async change => {
  let resolve!: (r: Response) => void, signal!: AbortSignal;
  vi.mocked(apiFetch).mockResolvedValueOnce(response(lesson)).mockImplementationOnce((_url, init) => {
    signal = init?.signal as AbortSignal;
    return new Promise(r => { resolve = r; }); // Deliberately ignores abort to verify the late-result fence too.
  });
  const candidate = vi.fn(), stale = vi.fn();
  const props = { task, source, owned: true, onCandidate: candidate, onStale: stale };
  const view = render(<StrictMode><ResearchMethodProposalForm {...props} /></StrictMode>);
  fields(); fireEvent.click(screen.getByRole("button", { name: "Prepare private research strategy" }));
  await waitFor(() => expect(apiFetch).toHaveBeenCalledTimes(2));
  expect(signal.aborted).toBe(false);
  if (change === "unmount") view.unmount();
  else view.rerender(<StrictMode><ResearchMethodProposalForm {...props} task={change === "owner" ? { ...task, owner_session_id: "other" } : { ...task, task_revision: 4 }} /></StrictMode>);
  expect(signal.aborted).toBe(true); await act(async () => { resolve(response(lesson)); });
  expect(candidate).not.toHaveBeenCalled();
  expect(stale).not.toHaveBeenCalled();
  expect(screen.queryByRole("button", { name: "Inspect prepared research candidate" })).toBeNull();
});

it("clears fields and fences a late POST when the original operator changes", async () => {
  let resolve!: (r: Response) => void; vi.mocked(apiFetch).mockImplementation(() => new Promise(r => { resolve = r; }));
  const view = render(<TaskLessonReview {...owner} task={task} />);
  fireEvent.click(screen.getByRole("button", { name: "Inspect lesson sources" })); resolve(response(source));
  await screen.findByRole("region", { name: "Structured research method proposal" }); fields();
  fireEvent.click(screen.getByRole("button", { name: "Prepare private research strategy" }));
  view.rerender(<TaskLessonReview {...owner} ownerSessionId="other" task={task} />); resolve(response(lesson));
  await waitFor(() => expect(screen.queryByLabelText("Query template 1")).toBeNull());
  expect(screen.queryByRole("region", { name: "Exact private lesson change" })).toBeNull();
  expect(vi.mocked(apiFetch).mock.calls.filter(([url]) => String(url).endsWith("/task-lessons/proposal"))).toHaveLength(0);
});

it("enforces actual finite schema/text bounds and allows correction after definitive validation denial", async () => {
  expect(() => validateResearchStrategy({ ...strategy, query_templates: ["a", "b", "c", "d"] })).toThrow();
  expect(() => validateResearchStrategy({ ...strategy, source_preferences: ["arbitrary"] })).toThrow();
  expect(() => validateResearchStrategy({ ...strategy, stop_conditions: [] })).toThrow();
  expect(() => validateResearchStrategy({ ...strategy, draft_sections: ["a".repeat(1001)] })).toThrow();
  expect(() => validateResearchStrategy({ ...strategy, draft_sections: ["😀".repeat(1000)] })).not.toThrow();
  vi.mocked(apiFetch).mockResolvedValue(response({ detail: { code: "research_method_candidate_unsafe" } }, 422));
  render(<ResearchMethodProposalForm task={task} source={source} owned onCandidate={vi.fn()} onStale={vi.fn()} />);
  fields(); fireEvent.click(screen.getByRole("button", { name: "Prepare private research strategy" }));
  await screen.findByRole("alert"); expect(screen.getByLabelText("Query template 1")).toBeEnabled();
  expect(apiFetch).toHaveBeenCalledTimes(1);
});
