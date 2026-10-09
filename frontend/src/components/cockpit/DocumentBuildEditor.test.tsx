import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, expect, it, vi } from "vitest";
import { apiFetch } from "../../lib/api";
import { GeneralTaskPanel } from "./GeneralTaskPanel";
import { DocumentBuildEditor } from "./DocumentBuildEditor";
import { parseBuildPreview, parseBuildOutputs, validateBuildSpec, MEDIA, readBuildDownload } from "../../lib/documentBuild";
import type { BuildPreview, DocumentBuildSpec } from "../../lib/documentBuild";
import type { GeneralTaskPlanRead } from "../../lib/generalTask";
import type { GoalInfo, WorkBoardTask } from "../../types";

// Finite API responses exercise real React/API bindings; native rendering is proved separately by backend HTTP tests.
vi.mock("../../lib/api", () => ({ apiFetch: vi.fn() }));
const owner = { ownerPrincipalId: "owner", ownerSessionId: "session" };
const id = "11111111-1111-4111-8111-111111111111", sourceId = "22222222-2222-4222-8222-222222222222";
const sha = "a".repeat(64), deadline = "2099-01-01T00:00:00Z";
const goal = { id: "goal", revision: 3, title: "Document Goal", status: "active", owner_session_id: owner.ownerSessionId } as GoalInfo;
const spec: DocumentBuildSpec = { kind: "report", title: "Local report", sections: [{ heading: "Results", paragraphs: ["Private human content"], citation_refs: [] }], tables: [], citations: [], style_preset: "plain" };
const projection = { build_id: id, build_ref: `document-build:${id}`, revision: 1, state: "staged", goal_id: goal.id, goal_revision: 3, spec_digest: sha, selection_digest: sha, task_id: null, original_deadline: deadline, reason_code: null, quota_reserved_bytes: 25165824, no_learning: true, provider_contacts: 0 };
const review = { binding: { schema: "document-build-review.v1", owner_principal_id: owner.ownerPrincipalId, owner_session_id: owner.ownerSessionId, root_authority: sha, root_token_digest: sha, goal_id: goal.id, goal_revision: 3, build_id: id, build_revision: 1, generation: 1, spec_digest: sha, selection_digest: sha, source_binding_digest: sha, descriptor_digest: sha, policy_digest: sha, renderer_profile: "document-build-renderer.v1", renderer_profile_digest: sha, limits_digest: sha, formats: ["docx", "pdf"], original_deadline: deadline, task_id: null, task_revision: null, plan_revision: null, expires_at: deadline }, mac: sha };
const preview = { ...projection, spec, selection: [], review, limits: { spec_bytes: 65536, editable_bytes: 4194304, pdf_bytes: 4194304 }, formats: ["docx", "pdf"] } as BuildPreview;
const task = { task_id: "task", task_revision: 2, goal_id: goal.id, goal_revision: 3, owner_principal_id: owner.ownerPrincipalId, owner_session_id: owner.ownerSessionId, status: "triage", capability_id: "agent.task.v1", requires_review: true } as WorkBoardTask;
const taskBinding = { build_ref: projection.build_ref, build_revision: 1, spec_digest: sha, selection_digest: sha, source_binding: null, original_deadline: deadline };
const plan = { task_id: task.task_id, task_revision: 2, accepted: false, no_learning: true, task_input: { goal_ref: goal.id, evidence_refs: [], requested_output: { type: "object" }, inference_egress_acknowledged: false, intent: "Build the reviewed local document specification", document_build: taskBinding, limits: { max_steps: 1, max_inference_calls: 0, wall_seconds: 60, depth: 0, max_outstanding_children: 0, max_cost_microusd: 0 } }, plan: { schema_version: 1, revision: 1, steps: [{ step_id: "build", tool_id: "document_build", input: { build_ref: projection.build_ref, spec_digest: sha }, depends_on: [], output_contract: { type: "object" } }] }, descriptors: [{ tool_id: "document_build", version: "1", input_schema: {}, output_schema: {}, effects: ["owner_private_artifact_write"], permissions: ["document_local_use"], deadline: 60, verifier: "document_build_readback.v1", policy_digest: sha }], strategy: { status: "none", reason: null } } as GeneralTaskPlanRead;
const bound = { ...preview, state: "bound", task_id: task.task_id, revision: 2, review: { ...review, binding: { ...review.binding, build_revision: 2, task_id: task.task_id, task_revision: 2, plan_revision: 1 } } };
const output = { ...projection, revision: 5, state: "degraded", task_id: task.task_id, output: { editable_artifact: { artifact_ref: `${projection.build_ref}:editable`, sha256: sha, size_bytes: 10, media_type: MEDIA.docx }, pdf_artifact: null, source_refs: [], warnings: ["document_pdf_unavailable"] } };
const ack = "I reviewed this exact private content, sources, formats and local rendering limits.";
const response = (v: unknown, status = 200) => new Response(JSON.stringify(v), { status });
beforeEach(() => { vi.mocked(apiFetch).mockReset(); });

it("uses normal fields in the existing panel, stages one closed spec and prepares the same inert native Task", async () => {
  const created = vi.fn(); let sent: unknown;
  vi.mocked(apiFetch).mockImplementation(async (url, init) => {
    if (String(url).endsWith("/builds")) { sent = JSON.parse(String(init?.body)); return response(projection); }
    if (String(url).endsWith("/preview")) return response(preview);
    if (String(url).endsWith("/prepare")) return response({ task, idempotent_replay: false });
    throw Error("Unexpected API call");
  });
  render(<GeneralTaskPanel {...owner} goals={[goal]} onCreated={created} />);
  fireEvent.click(screen.getByLabelText("Build a local editable document and PDF"));
  expect(screen.queryByLabelText("What should Seraph do?")).toBeNull();
  fireEvent.change(screen.getByLabelText("Document Goal"), { target: { value: goal.id } });
  fireEvent.change(screen.getByLabelText("Document title"), { target: { value: "Local report" } });
  fireEvent.change(screen.getByLabelText("Section 1 heading"), { target: { value: "Results" } });
  fireEvent.change(screen.getByLabelText("Section 1 paragraph 1"), { target: { value: "Private human content" } });
  fireEvent.click(screen.getByRole("button", { name: "Stage private build and review" }));
  await screen.findByRole("region", { name: "Signed private document review" });
  expect(sent).toMatchObject({ goal_id: goal.id, goal_revision: 3, source: null, spec });
  expect(Object.keys((sent as { spec: object }).spec)).toHaveLength(6);
  expect(screen.getByRole("button", { name: "Prepare inert document task" })).toBeDisabled();
  fireEvent.click(screen.getByLabelText(ack));
  fireEvent.click(screen.getByRole("button", { name: "Prepare inert document task" }));
  await waitFor(() => expect(created).toHaveBeenCalledWith(task));
  const request = JSON.parse(String(vi.mocked(apiFetch).mock.calls[2][1]?.body));
  expect(request).toMatchObject({ expected_revision: 1, review: preview.review });
  expect(request.accept).toBeUndefined(); expect(request.plan).toBeUndefined();
});

it("accepts through normal promote with the exact current task-bound signed review and no raw JSON editor", async () => {
  const changed = vi.fn();
  vi.mocked(apiFetch).mockImplementation(async url => response(String(url).endsWith("/plan") ? plan : bound));
  render(<GeneralTaskPanel {...owner} task={task} goals={[goal]} onChanged={changed} />);
  fireEvent.click(await screen.findByRole("button", { name: "Open current signed document review" }));
  await screen.findByRole("region", { name: "Signed private document review" });
  expect(screen.queryByLabelText("Typed plan steps")).toBeNull();
  expect(screen.queryByRole("button", { name: "Accept reviewed task plan" })).toBeNull();
  expect(screen.queryByText("Typed input and output contract")).toBeNull();
  fireEvent.click(screen.getByLabelText(ack));
  fireEvent.click(screen.getByRole("button", { name: "Accept reviewed document task" }));
  await waitFor(() => expect(changed).toHaveBeenCalled());
  expect(JSON.parse(String(vi.mocked(apiFetch).mock.calls[2][1]?.body))).toEqual({ action: "promote", expected_revision: 2, document_build_review: bound.review });
});

it.each(["owner", "revision", "spec", "expiry"])("withholds acceptance when signed %s binding changed", async field => {
  const b = structuredClone(bound);
  if (field === "owner") b.review.binding.owner_session_id = "other";
  if (field === "revision") b.review.binding.task_revision = 1;
  if (field === "spec") b.review.binding.spec_digest = "b".repeat(64);
  if (field === "expiry") b.review.binding.expires_at = "2000-01-01T00:00:00Z";
  vi.mocked(apiFetch).mockResolvedValue(response(b));
  render(<DocumentBuildEditor {...owner} goals={[goal]} task={task} read={plan} />);
  fireEvent.click(screen.getByRole("button", { name: "Open current signed document review" }));
  if (field === "expiry") expect(await screen.findByRole("button", { name: "Accept reviewed document task" })).toBeDisabled();
  else { await screen.findByRole("alert"); expect(screen.queryByRole("button", { name: "Accept reviewed document task" })).toBeNull(); }
  expect(apiFetch).toHaveBeenCalledTimes(1);
});

it("preserves literal spreadsheet prefixes and sends formulas only through the explicit formula control", async () => {
  vi.mocked(apiFetch).mockRejectedValue(new Error("Unconfirmed finite fixture"));
  render(<DocumentBuildEditor {...owner} goals={[goal]} />);
  fireEvent.change(screen.getByLabelText("Document Goal"), { target: { value: goal.id } });
  fireEvent.change(screen.getByLabelText("Document title"), { target: { value: "Workbook" } });
  fireEvent.change(screen.getByLabelText("Document kind"), { target: { value: "table_workbook" } });
  for (const value of ["=1+1", "+SUM(A1)", "-10", "@name"]) {
    fireEvent.click(screen.getByRole("button", { name: "Add cell" }));
    fireEvent.change(screen.getByLabelText(`Cell ${["=1+1", "+SUM(A1)", "-10", "@name"].indexOf(value) + 1} value`), { target: { value } });
  }
  fireEvent.click(screen.getByRole("button", { name: "Add cell" }));
  fireEvent.change(screen.getByLabelText("Cell 5 type"), { target: { value: "formula" } });
  fireEvent.change(screen.getByLabelText("Cell 5 formula"), { target: { value: "=SUM(A1:A2)" } });
  fireEvent.click(screen.getByRole("button", { name: "Stage private build and review" }));
  await screen.findByRole("alert");
  const payload = JSON.parse(String(vi.mocked(apiFetch).mock.calls[0][1]?.body));
  expect(payload.spec.tables[0].cells.map((c: { value: string }) => c.value)).toEqual(["=1+1", "+SUM(A1)", "-10", "@name"]);
  expect(payload.spec.tables[0].formulas).toEqual([{ sheet: "Sheet1", cell: "A5", expression: "=SUM(A1:A2)" }]);
  expect(payload.spec.tables[0].cached_values).toBeUndefined();
  expect(screen.getByLabelText("Document title")).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Reconcile exact private build request" }));
  await waitFor(() => expect(apiFetch).toHaveBeenCalledTimes(2));
  expect(vi.mocked(apiFetch).mock.calls[1][1]?.body).toBe(vi.mocked(apiFetch).mock.calls[0][1]?.body);
});

it("selects actual private citation leaves and uses the server-issued binding without a preparation Task", async () => {
  const source = { artifact_ref: `document-source:${sourceId}`, revision: 7, goal_id: goal.id, goal_revision: 3, state: "sealed" };
  const binding = { artifact_ref: source.artifact_ref, source_revision: 7, metadata_digest: sha, citation_refs: ["pdf#page=1"], selection_digest: sha, acknowledge_local_use: true };
  vi.mocked(apiFetch).mockImplementation(async (url, init) => {
    const path = String(url);
    if (path.endsWith("/sources")) return response({ sources: [source] });
    if (path.includes("/citations?")) return response({ artifact_ref: source.artifact_ref, source_revision: 7, citations: [{ source_ref: "pdf#page=1", text: "Actual selected quote" }], next_offset: null });
    if (path.endsWith("/selection")) { expect(JSON.parse(String(init?.body))).toEqual({ expected_revision: 7, citation_refs: ["pdf#page=1"], acknowledge_local_use: true }); return response({ source: binding, goal_id: goal.id, goal_revision: 3 }); }
    if (path.endsWith("/builds")) return response(projection);
    return response(preview);
  });
  render(<DocumentBuildEditor {...owner} goals={[goal]} />);
  fireEvent.change(screen.getByLabelText("Document Goal"), { target: { value: goal.id } });
  fireEvent.change(screen.getByLabelText("Document title"), { target: { value: "Cited report" } });
  fireEvent.click(screen.getByRole("button", { name: "Choose citations from an adopted source" }));
  fireEvent.click(await screen.findByRole("button", { name: /Open source document-source:/ }));
  fireEvent.click(await screen.findByRole("checkbox", { name: /pdf#page=1/ }));
  fireEvent.click(screen.getByRole("checkbox", { name: "Cite Source 1 in section 1" }));
  fireEvent.click(screen.getByRole("button", { name: "Stage private build and review" }));
  await screen.findByRole("region", { name: "Signed private document review" });
  const body = JSON.parse(String(vi.mocked(apiFetch).mock.calls[3][1]?.body));
  expect(body.source).toEqual(binding); expect(body.spec.sections[0].citation_refs).toEqual(["pdf#page=1"]);
  expect(vi.mocked(apiFetch).mock.calls.every(([url]) => !String(url).includes("/preparations"))).toBe(true);
});

it("clears private fields and fences a late signed preview when the operator changes", async () => {
  let resolve!: (r: Response) => void;
  vi.mocked(apiFetch).mockImplementation(() => new Promise(r => { resolve = r; }));
  const view = render(<DocumentBuildEditor {...owner} goals={[goal]} task={task} read={plan} />);
  fireEvent.click(screen.getByRole("button", { name: "Open current signed document review" }));
  view.rerender(<DocumentBuildEditor ownerPrincipalId="other" ownerSessionId="other" goals={[]} task={task} read={plan} />);
  resolve(response(bound));
  await waitFor(() => expect(screen.queryByText("Private human content")).toBeNull());
  expect(screen.queryByRole("region", { name: "Signed private document review" })).toBeNull();
  expect(screen.getByRole("button", { name: "Open current signed document review" })).toBeDisabled();
});

it("keeps missing PDF truthful, shows retained editable output and retires only an original terminal Task", async () => {
  const terminal = { ...task, status: "done", task_revision: 7, latest_attempt: { attempt_id: "attempt", ended_at: "2026-10-09T12:00:00Z" } } as WorkBoardTask;
  vi.mocked(apiFetch).mockImplementation(async (_url, init) => response(init?.method === "DELETE" ? { ...projection, state: "deleted", quota_reserved_bytes: 0, task_id: task.task_id } : output));
  render(<DocumentBuildEditor {...owner} goals={[goal]} task={terminal} read={{ ...plan, task_revision: 7, accepted: true }} />);
  fireEvent.click(screen.getByRole("button", { name: "Read verified document outputs" }));
  expect(await screen.findByRole("region", { name: "Verified document outputs" })).toHaveTextContent("PDF unavailable");
  expect(screen.getByRole("button", { name: "Download editable document" })).toBeEnabled();
  expect(screen.getByRole("button", { name: "Download PDF" })).toBeDisabled();
  fireEvent.click(screen.getByLabelText("Retire the original private build and outputs after verified terminal cleanup."));
  fireEvent.click(screen.getByRole("button", { name: "Retire private document build" }));
  await waitFor(() => expect(vi.mocked(apiFetch).mock.calls[1][1]?.method).toBe("DELETE"));
  expect(JSON.parse(String(vi.mocked(apiFetch).mock.calls[1][1]?.body))).toMatchObject({ expected_revision: 5, expected_task_revision: 7, attempt_id: "attempt" });
});

it("discovers charged unbound builds on reload and retires them without inventing Task authority", async () => {
  vi.mocked(apiFetch).mockImplementation(async (_url, init) => response(init?.method === "DELETE" ? { ...projection, state: "deleted", quota_reserved_bytes: 0 } : { builds: [projection], next_offset: null, no_learning: true }));
  render(<DocumentBuildEditor {...owner} goals={[goal]} />);
  fireEvent.click(screen.getByRole("button", { name: "Refresh retained private builds" }));
  fireEvent.click(await screen.findByRole("button", { name: /staged · Goal goal/ }));
  fireEvent.click(screen.getByLabelText("Discard this unbound private build"));
  fireEvent.click(screen.getByRole("button", { name: "Retire unbound private build" }));
  await waitFor(() => expect(apiFetch).toHaveBeenCalledTimes(2));
  const body = JSON.parse(String(vi.mocked(apiFetch).mock.calls[1][1]?.body));
  expect(body.expected_revision).toBe(1); expect(body.attempt_id).toBeUndefined(); expect(body.expected_task_revision).toBeUndefined();
});

it("rejects malformed preview/output contracts and exact cell bound errors", () => {
  expect(() => parseBuildPreview({ ...preview, review: { ...review, binding: { ...review.binding, extra_authority: true } } })).toThrow();
  expect(() => parseBuildOutputs({ ...output, output: { ...output.output, pdf_artifact: output.output.editable_artifact } })).toThrow();
  expect(() => validateBuildSpec({ ...spec, kind: "table_workbook", tables: [{ sheet_names: ["Sheet1"], cells: [{ sheet: "Sheet1", cell: "BM1", value: "text" }], formulas: [], formats: [] }] })).toThrow("Sheet1!BM1");
  expect(() => validateBuildSpec({ ...spec, title: "\u0000" })).toThrow("unsupported");
});

it("downloads only authenticated bytes matching the manifest hash and MIME", async () => {
  const bytes = new TextEncoder().encode("physical document");
  const hash = [...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))].map(n => n.toString(16).padStart(2, "0")).join("");
  vi.mocked(apiFetch).mockResolvedValue(new Response(bytes, { headers: { "Content-Type": MEDIA.docx } }));
  const artifact = { artifact_ref: `${projection.build_ref}:editable`, sha256: hash, size_bytes: bytes.length, media_type: MEDIA.docx };
  expect((await readBuildDownload(id, "editable", artifact)).size).toBe(bytes.length);
  vi.mocked(apiFetch).mockResolvedValue(new Response(bytes, { headers: { "Content-Type": MEDIA.docx } }));
  await expect(readBuildDownload(id, "editable", { ...artifact, sha256: sha })).rejects.toThrow("hash changed");
});

it("keeps one document build and Task through the complete operator review, acceptance, output and retirement story", async () => {
  let prepared = false, accepted = false;
  const created = vi.fn(), changed = vi.fn();
  const terminal = { ...task, status: "done", latest_attempt: { attempt_id: "original-attempt", ended_at: "2026-10-09T12:00:00Z" } } as WorkBoardTask;
  vi.mocked(apiFetch).mockImplementation(async (url, init) => {
    const path = String(url);
    if (init?.method === "DELETE") return response({ ...projection, state: "deleted", task_id: task.task_id, quota_reserved_bytes: 0 });
    if (path.endsWith("/builds")) return response(projection);
    if (path.endsWith("/preview")) return response(prepared ? bound : preview);
    if (path.endsWith("/prepare")) { prepared = true; return response({ task, idempotent_replay: false }); }
    if (path.endsWith("/actions")) { accepted = true; return response({ task }); }
    if (path.endsWith("/plan")) return response({ ...plan, accepted });
    if (path.endsWith("/outputs")) return response(output);
    throw Error("Unexpected story API request");
  });
  const mounted = render(<GeneralTaskPanel {...owner} goals={[goal]} onCreated={created} onChanged={changed} />);
  fireEvent.click(screen.getByLabelText("Build a local editable document and PDF"));
  fireEvent.change(screen.getByLabelText("Document Goal"), { target: { value: goal.id } });
  fireEvent.change(screen.getByLabelText("Document title"), { target: { value: "Local report" } });
  fireEvent.change(screen.getByLabelText("Section 1 paragraph 1"), { target: { value: "Private human content" } });
  fireEvent.click(screen.getByRole("button", { name: "Stage private build and review" }));
  await screen.findByRole("region", { name: "Signed private document review" });
  fireEvent.click(screen.getByLabelText(ack)); fireEvent.click(screen.getByRole("button", { name: "Prepare inert document task" }));
  await waitFor(() => expect(created).toHaveBeenCalledWith(task));
  mounted.rerender(<GeneralTaskPanel {...owner} goals={[goal]} task={task} onChanged={changed} />);
  fireEvent.click(await screen.findByRole("button", { name: "Open current signed document review" }));
  await screen.findByRole("region", { name: "Signed private document review" });
  fireEvent.click(screen.getByLabelText(ack)); fireEvent.click(screen.getByRole("button", { name: "Accept reviewed document task" }));
  await waitFor(() => expect(changed).toHaveBeenCalledTimes(1));
  mounted.rerender(<GeneralTaskPanel {...owner} goals={[goal]} task={terminal} onChanged={changed} />);
  fireEvent.click(await screen.findByRole("button", { name: "Refresh current task plan" }));
  fireEvent.click(await screen.findByRole("button", { name: "Read verified document outputs" }));
  await screen.findByRole("region", { name: "Verified document outputs" });
  fireEvent.click(screen.getByLabelText("Retire the original private build and outputs after verified terminal cleanup."));
  fireEvent.click(screen.getByRole("button", { name: "Retire private document build" }));
  await waitFor(() => expect(changed).toHaveBeenCalledTimes(2));
  const calls = vi.mocked(apiFetch).mock.calls;
  expect(calls.filter(([url]) => String(url).endsWith("/builds"))).toHaveLength(1);
  expect(calls.filter(([url]) => String(url).endsWith("/prepare"))).toHaveLength(1);
  expect(calls.find(([url]) => String(url).endsWith("/actions"))?.[0]).toContain(`/tasks/${task.task_id}/actions`);
  expect(JSON.parse(String(calls.find(([, init]) => init?.method === "DELETE")?.[1]?.body))).toMatchObject({ attempt_id: "original-attempt", expected_task_revision: task.task_revision });
});

it("discards a late private preview after the same Task revision changes", async () => {
  let resolve!: (r: Response) => void;
  vi.mocked(apiFetch).mockImplementation(() => new Promise(r => { resolve = r; }));
  const mounted = render(<DocumentBuildEditor {...owner} goals={[goal]} task={task} read={plan} />);
  fireEvent.click(screen.getByRole("button", { name: "Open current signed document review" }));
  mounted.rerender(<DocumentBuildEditor {...owner} goals={[goal]} task={{ ...task, task_revision: 3 }} read={{ ...plan, task_revision: 3 }} />);
  resolve(response(bound));
  await waitFor(() => expect(screen.queryByRole("region", { name: "Signed private document review" })).toBeNull());
  expect(screen.getByRole("button", { name: "Open current signed document review" })).toBeEnabled();
});

it("shows a sanitized exact formula cell denial and permits explicit correction after a definitive 422", async () => {
  vi.mocked(apiFetch).mockResolvedValue(response({ detail: { code: "document_build_fields_invalid", errors: [{ field: ["spec"], code: "document_formula_invalid", sheet: "Sheet1", cell: "A1" }] } }, 422));
  render(<DocumentBuildEditor {...owner} goals={[goal]} />);
  fireEvent.change(screen.getByLabelText("Document Goal"), { target: { value: goal.id } });
  fireEvent.change(screen.getByLabelText("Document title"), { target: { value: "Formula correction" } });
  fireEvent.change(screen.getByLabelText("Document kind"), { target: { value: "table_workbook" } });
  fireEvent.click(screen.getByRole("button", { name: "Add cell" }));
  fireEvent.change(screen.getByLabelText("Cell 1 type"), { target: { value: "formula" } });
  fireEvent.change(screen.getByLabelText("Cell 1 formula"), { target: { value: "=UNSUPPORTED(1)" } });
  fireEvent.click(screen.getByRole("button", { name: "Stage private build and review" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Sheet1!A1");
  expect(screen.getByLabelText("Cell 1 formula")).toBeEnabled();
  fireEvent.change(screen.getByLabelText("Cell 1 formula"), { target: { value: "=SUM(1,2)" } });
  expect(screen.queryByRole("button", { name: "Reconcile exact private build request" })).toBeNull();
  expect(apiFetch).toHaveBeenCalledTimes(1);
});

it("rejects a document Task readback that introduces inference behind the private editor", async () => {
  vi.mocked(apiFetch).mockResolvedValue(response({ ...plan, task_input: { ...plan.task_input, limits: { ...plan.task_input.limits, max_inference_calls: 1 } } }));
  render(<GeneralTaskPanel {...owner} goals={[goal]} task={task} />);
  expect(await screen.findByRole("alert")).toHaveTextContent("fixed zero-inference");
  expect(screen.queryByRole("button", { name: "Open current signed document review" })).toBeNull();
});
