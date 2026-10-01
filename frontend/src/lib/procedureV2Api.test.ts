import { describe, expect, it, vi } from "vitest";

import {
  correlateInvocationReceipt,
  correlateScheduleReceipt,
  ProcedureV2ApiError,
  procedureV2Api,
  type ProcedureV2InvokeReceipt,
  type ProcedureV2InvokeRequest,
  type ProcedureV2ScheduleReceipt,
  type ProcedureV2ScheduleRequest,
} from "./procedureV2Api";

const digest = "a".repeat(64);

function response(payload: unknown, ok = true, status = ok ? 200 : 409) {
  return { ok, status, json: async () => payload };
}

function plan(templateId = "public-browser-check") {
  return {
    schema_version: 2,
    template_id: templateId,
    steps: [{
      step_id: "public_browser_check",
      capability_id: "browser.public-task.v1",
      capability_version: "1",
      typed_input_ref: "server-owned-ref",
      typed_input_digest: digest,
    }],
    parameters: [
      { name: "goal_id", kind: "goal_id", required: true },
      { name: "expected_goal_revision", kind: "goal_revision", required: true },
    ],
    verifier: "leaf_readbacks",
    limits: { max_steps: 2, max_total_seconds: 300 },
  };
}

function preview() {
  return {
    status: "preview",
    template_id: "public-browser-check",
    preview_digest: digest,
    expires_at: "2026-10-01T12:15:00Z",
    plan: plan(),
    source_refs: [{
      task_id: "task-1",
      task_revision: 3,
      attempt_id: "attempt-1",
      job_id: "job-1",
      artifact_ids_and_hashes: [{ artifact_id: "artifact-1", sha256: digest }],
      capability_id: "browser.public-task.v1",
      capability_version: "1",
      goal_id: "goal-1",
      goal_revision: 2,
    }],
    parameter_schema: plan().parameters,
    permissions: ["browser.public-task.v1"],
    limits: { max_steps: 2, max_total_seconds: 300 },
    version_diff: null,
  };
}

function previewForTemplate(templateId: "watch-and-public-browser" | "selected-meeting-prep") {
  const watch = templateId === "watch-and-public-browser";
  const steps = watch
    ? [
        { step_id: "source_watch", capability_id: "guardian.research-watch.v1", capability_version: "1", typed_input_ref: "server-owned-watch", typed_input_digest: digest },
        { step_id: "public_browser_check", capability_id: "browser.public-task.v1", capability_version: "1", typed_input_ref: "server-owned-browser", typed_input_digest: digest },
      ]
    : [{ step_id: "selected_meeting_prep", capability_id: "calendar.meeting-prep.v1", capability_version: "1", typed_input_ref: "server-owned-calendar", typed_input_digest: digest }];
  const parameters = watch
    ? [
        { name: "goal_id", kind: "goal_id", required: true },
        { name: "expected_goal_revision", kind: "goal_revision", required: true },
        { name: "source_watch_id", kind: "source_watch_id", required: true },
        { name: "expected_watch_revision", kind: "watch_revision", required: true },
      ]
    : [
        { name: "schema_version", kind: "schema_version", required: true },
        { name: "consent_id", kind: "consent_id", required: true },
        { name: "event_binding_id", kind: "event_binding_id", required: true },
        { name: "expected_event_binding_revision", kind: "event_binding_revision", required: true },
        { name: "expected_consent_revision", kind: "consent_revision", required: true },
        { name: "expected_connection_revision", kind: "connection_revision", required: true },
        { name: "event_revision", kind: "event_revision", required: true },
        { name: "calendar_list_revision", kind: "calendar_list_revision", required: true },
        { name: "goal_id", kind: "goal_id", required: true },
        { name: "goal_revision", kind: "goal_revision", required: true },
        { name: "purpose", kind: "purpose", required: true },
      ];
  const capabilities = steps.map((step) => step.capability_id);
  return {
    status: "preview",
    template_id: templateId,
    preview_digest: digest,
    expires_at: "2026-10-01T12:15:00Z",
    plan: { schema_version: 2, template_id: templateId, steps, parameters, verifier: "leaf_readbacks", limits: { max_steps: 2, max_total_seconds: 300 } },
    source_refs: steps.map((step, index) => ({
      task_id: `${templateId}-task-${index + 1}`,
      task_revision: 3,
      attempt_id: null,
      job_id: null,
      artifact_ids_and_hashes: [{ artifact_id: `${templateId}-artifact-${index + 1}`, sha256: digest }],
      capability_id: step.capability_id,
      capability_version: "1",
      goal_id: "goal-1",
      goal_revision: 2,
    })),
    parameter_schema: parameters,
    permissions: capabilities,
    limits: { max_steps: 2, max_total_seconds: 300 },
    version_diff: null,
  };
}

describe("procedureV2Api", () => {
  it("posts an exact fixed source request and rejects a plan that exceeds the 2/300 limits", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response(preview()));
    vi.stubGlobal("fetch", fetchMock);
    try {
      const result = await procedureV2Api.previewFromTasks({
        template_id: "public-browser-check",
        source_tasks: [{ task_id: "task-1", expected_revision: 3 }],
        name: "Public check",
        idempotency_key: "preview-key",
      });
      expect(result.plan.limits).toEqual({ max_steps: 2, max_total_seconds: 300 });
      expect(JSON.parse(fetchMock.mock.calls[0][1].body as string)).toEqual({
        template_id: "public-browser-check",
        source_tasks: [{ task_id: "task-1", expected_revision: 3 }],
        name: "Public check",
        idempotency_key: "preview-key",
      });

      const malformed = { ...preview(), plan: { ...plan(), limits: { max_steps: 3, max_total_seconds: 301 } } };
      fetchMock.mockResolvedValueOnce(response(malformed));
      await expect(procedureV2Api.previewFromTasks({
        template_id: "public-browser-check",
        source_tasks: [{ task_id: "task-1", expected_revision: 3 }],
        name: "Public check",
        idempotency_key: "preview-key-2",
      })).rejects.toMatchObject({ code: "procedure_response_invalid" });
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("rejects parsed plans that drift from the registered step, parameter, or digest contract", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    try {
      const malformedPlans = [
        { ...plan(), steps: [{ ...plan().steps[0], step_id: "unregistered_step" }] },
        { ...plan(), steps: [{ ...plan().steps[0], capability_id: "shell.arbitrary.v1" }] },
        { ...plan(), steps: [{ ...plan().steps[0], capability_version: "2" }] },
        { ...plan(), steps: [{ ...plan().steps[0], typed_input_digest: "z".repeat(64) }] },
        { ...plan(), parameters: [...plan().parameters].reverse() },
        { ...plan(), parameters: [{ ...plan().parameters[0], required: false }, plan().parameters[1]] },
        { ...plan(), steps: [{ ...plan().steps[0], typed_input_ref: "../private" }] },
      ];
      for (const malformedPlan of malformedPlans) {
        fetchMock.mockResolvedValueOnce(response({ ...preview(), plan: malformedPlan }));
        await expect(procedureV2Api.previewFromTasks({
          template_id: "public-browser-check",
          source_tasks: [{ task_id: "task-1", expected_revision: 3 }],
          name: "Public check",
          idempotency_key: `malformed-${fetchMock.mock.calls.length}`,
        })).rejects.toMatchObject({ code: "procedure_response_invalid" });
      }
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("validates the complete parameter key order for watch and meeting templates", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    try {
      const watch = previewForTemplate("watch-and-public-browser");
      fetchMock.mockResolvedValueOnce(response(watch));
      const watchResult = await procedureV2Api.previewFromTasks({ template_id: "watch-and-public-browser", source_tasks: [{ task_id: "watch-task", expected_revision: 3 }, { task_id: "browser-task", expected_revision: 3 }], name: "Watch", idempotency_key: "watch-preview" });
      expect(watchResult.plan.parameters).toEqual(watch.plan.parameters);

      const meeting = previewForTemplate("selected-meeting-prep");
      fetchMock.mockResolvedValueOnce(response(meeting));
      const meetingResult = await procedureV2Api.previewFromTasks({ template_id: "selected-meeting-prep", source_tasks: [{ task_id: "meeting-task", expected_revision: 3 }], name: "Meeting", idempotency_key: "meeting-preview" });
      expect(meetingResult.plan.parameters).toEqual(meeting.plan.parameters);

      fetchMock.mockResolvedValueOnce(response({ ...meeting, plan: { ...meeting.plan, parameters: [...meeting.plan.parameters, { name: "expected_goal_revision", kind: "goal_revision", required: true }] } }));
      await expect(procedureV2Api.previewFromTasks({ template_id: "selected-meeting-prep", source_tasks: [{ task_id: "meeting-task", expected_revision: 3 }], name: "Meeting", idempotency_key: "meeting-extra" })).rejects.toMatchObject({ code: "procedure_response_invalid" });
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("parses the backend v2 package runbook contract without inventing v1 tool fields", async () => {
    const packagePreview = {
      routine_id: "routine-v2",
      version: 1,
      pack_id: "seraph.routine.v2",
      digest,
      installed_package_digest: digest,
      review_id: null,
      status: "not_reviewed",
      manifest: {
        schema_version: 2,
        display_name: "Public check",
        summary: "Fixed v2 procedure",
        version: "2.0.1",
        authority: { tools: ["public_browser_check"], filesystem: ["artifact_read", "artifact_write"], network: false, secrets: [], approval: "always" },
        resources: { max_runtime_seconds: 300, max_artifact_bytes: 10_000_000, max_inference_cost_microusd: 0, inference_priority: "approved_operator" },
        data_policy: { classes: ["public"], egress: [] },
      },
      runbook: {
        title: "Seraph reviewed guardian procedure",
        summary: "A reviewed, owner-bound fixed procedure definition.",
        procedure: {
          schema_version: 2,
          capability_id: "guardian-routine.v2",
          template_id: "public-browser-check",
          plan_digest: digest,
          steps: [{ id: "public_browser_check", capability_id: "browser.public-task.v1", capability_version: "1" }],
        },
        bindings: { workflow_sha256: digest, runbook_sha256: digest, source_provenance_sha256: digest, plan_digest: digest },
      },
    };
    const fetchMock = vi.fn().mockResolvedValue(response(packagePreview));
    vi.stubGlobal("fetch", fetchMock);
    try {
      const parsed = await procedureV2Api.packagePreview("routine-v2", 1, 4);
      expect(parsed.manifest.schema_version).toBe(2);
      expect(parsed.runbook.procedure).toMatchObject({ schema_version: 2, capability_id: "guardian-routine.v2", template_id: "public-browser-check" });
      expect(parsed.runbook.procedure.steps).toEqual([{ id: "public_browser_check", capability_id: "browser.public-task.v1", capability_version: "1" }]);
      expect(parsed.runbook.bindings).toMatchObject({ workflow_sha256: digest, runbook_sha256: digest, plan_digest: digest });

      fetchMock.mockResolvedValueOnce(response({ ...packagePreview, runbook: { ...packagePreview.runbook, bindings: { ...packagePreview.runbook.bindings, runbook_sha256: undefined } } }));
      await expect(procedureV2Api.packagePreview("routine-v2", 1, 4)).rejects.toMatchObject({ code: "procedure_response_invalid" });
      fetchMock.mockResolvedValueOnce(response({ ...packagePreview, runbook: { ...packagePreview.runbook, procedure: { ...packagePreview.runbook.procedure, plan_digest: undefined } } }));
      await expect(procedureV2Api.packagePreview("routine-v2", 1, 4)).rejects.toMatchObject({ code: "procedure_response_invalid" });
      for (const network of ["false", 0, [], {}]) {
        fetchMock.mockResolvedValueOnce(response({ ...packagePreview, manifest: { ...packagePreview.manifest, authority: { ...packagePreview.manifest.authority, network } } }));
        await expect(procedureV2Api.packagePreview("routine-v2", 1, 4)).rejects.toMatchObject({ code: "procedure_response_invalid" });
      }
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("keeps authenticated source owner pairs and leaves incomplete rows unverified", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({
      tasks: [
        {
          task_id: "owned-task",
          title: "Owned task",
          status: "done",
          capability_id: "browser.public-task.v1",
          owner_principal_id: "operator:one",
          owner_session_id: "session-1",
          goal_id: "goal-1",
          goal_revision: 4,
          task_revision: 3,
          readback_status: "verified",
          verification_status: "passed",
        },
        {
          task_id: "incomplete-task",
          title: "Incomplete task",
          status: "done",
          capability_id: "browser.public-task.v1",
          goal_id: "goal-1",
          goal_revision: 4,
          task_revision: 3,
          readback_status: "verified",
          verification_status: "passed",
        },
      ],
    }));
    vi.stubGlobal("fetch", fetchMock);
    try {
      await expect(procedureV2Api.listSourceTasks()).resolves.toEqual([
        expect.objectContaining({ task_id: "owned-task", owner_principal_id: "operator:one", owner_session_id: "session-1" }),
        expect.objectContaining({ task_id: "incomplete-task", owner_principal_id: null, owner_session_id: null }),
      ]);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("rejects routine readback with missing owner identity or a mismatched nested routine id", async () => {
    const routine = {
      id: "routine-1",
      owner_principal_id: "operator:one",
      state: "prepared",
      revision: 1,
      current_version: 1,
      name: "Owned routine",
      versions: [{
        id: "version-1",
        routine_id: "routine-1",
        version: 1,
        workflow_sha256: digest,
        runbook_sha256: digest,
        installed_package_digest: null,
        source_provenance: {},
        source_repository: null,
        source_action: null,
        source_issue_number: null,
        created_at: "2026-10-01T12:00:00Z",
        installed_at: null,
      }],
      package: { status: "not_installed", digest: null, review_id: null },
    };
    const fetchMock = vi.fn().mockResolvedValue(response(routine));
    vi.stubGlobal("fetch", fetchMock);
    try {
      await expect(procedureV2Api.getRoutine("routine-1")).resolves.toMatchObject({ id: "routine-1", owner_principal_id: "operator:one" });
      fetchMock.mockResolvedValueOnce(response({ ...routine, owner_principal_id: "" }));
      await expect(procedureV2Api.getRoutine("routine-1")).rejects.toMatchObject({ code: "procedure_response_invalid" });
      fetchMock.mockResolvedValueOnce(response({ ...routine, versions: [{ ...routine.versions[0], routine_id: "routine-other" }] }));
      await expect(procedureV2Api.getRoutine("routine-1")).rejects.toMatchObject({ code: "procedure_response_invalid" });
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it.each([
    ["fetch", () => new Promise<never>(() => {})],
    ["body parsing", () => Promise.resolve({ ok: true, status: 200, json: () => new Promise<never>(() => {}) })],
  ])("bounds a non-cooperative %s within the hard request deadline", async (_kind, implementation) => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn(implementation));
    try {
      const pending = procedureV2Api.listSourceTasks();
      const assertion = expect(pending).rejects.toMatchObject({ code: "procedure_request_timeout", retryable: true });
      await vi.advanceTimersByTimeAsync(15_000);
      await assertion;
    } finally {
      vi.useRealTimers();
      vi.unstubAllGlobals();
    }
  });

  it("preserves caller abort instead of waiting for the hard deadline", async () => {
    const controller = new AbortController();
    vi.stubGlobal("fetch", vi.fn(() => new Promise<never>(() => {})));
    try {
      const pending = procedureV2Api.listSourceTasks(controller.signal);
      controller.abort();
      await expect(pending).rejects.toMatchObject({ name: "AbortError" });
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("preserves nullable pre-dispatch attempt and job identities in an accepted invocation", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({
      status: "accepted",
      routine_id: "routine-1",
      version: 1,
      schema_version: 2,
      template_id: "public-browser-check",
      invocation_uuid: "invoke-1",
      scope: "procedure-v2:routine-1:version-1",
      goal_id: "goal-2",
      goal_revision: 4,
      task_id: "task-2",
      attempt_id: null,
      job_id: null,
      input_artifact_id: "artifact-2",
      input_digest: digest,
      plan_digest: digest,
      revision: 3,
      audit_receipt_id: "receipt-2",
      recovery_action: null,
    }));
    vi.stubGlobal("fetch", fetchMock);
    try {
      const result = await procedureV2Api.invoke("routine-1", {
        version: 1,
        expected_routine_revision: 3,
        goal_id: "goal-2",
        expected_goal_revision: 4,
        parameters: { goal_id: "goal-2", expected_goal_revision: 4 },
        invocation_uuid: "invoke-1",
      });
      expect(result).toMatchObject({ task_id: "task-2", attempt_id: null, job_id: null });
      expect(fetchMock.mock.calls[0][0]).toContain("/api/capabilities/routines/routine-1/invoke-v2");
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("rejects invocation receipts that do not correlate to the captured route, scope, or typed request", () => {
    const request: ProcedureV2InvokeRequest = {
      version: 1,
      expected_routine_revision: 3,
      goal_id: "goal-2",
      expected_goal_revision: 4,
      parameters: { goal_id: "goal-2", expected_goal_revision: 4 },
      invocation_uuid: "invoke-1",
    };
    const receipt: ProcedureV2InvokeReceipt = {
      status: "accepted",
      routine_id: "routine-1",
      version: 1,
      schema_version: 2,
      template_id: "public-browser-check",
      invocation_uuid: "invoke-1",
      scope: "procedure-v2:routine-1:version-1",
      goal_id: "goal-2",
      goal_revision: 4,
      task_id: "task-2",
      attempt_id: null,
      job_id: null,
      input_artifact_id: "artifact-2",
      input_digest: digest,
      plan_digest: digest,
      revision: 3,
      audit_receipt_id: null,
      recovery_action: null,
    };
    const mismatches: Array<[string, Partial<ProcedureV2InvokeReceipt>]> = [
      ["routine", { routine_id: "routine-other" }],
      ["version", { version: 2 }],
      ["uuid", { invocation_uuid: "invoke-other" }],
      ["goal", { goal_id: "goal-other" }],
      ["goal revision", { goal_revision: 5 }],
      ["template", { template_id: "watch-and-public-browser" }],
      ["scope", { scope: "procedure-v2:routine-1:version-other" }],
    ];
    for (const [, mismatch] of mismatches) {
      expect(() => correlateInvocationReceipt({ ...receipt, ...mismatch }, request, "routine-1", "version-1"))
        .toThrowError(ProcedureV2ApiError);
      try {
        correlateInvocationReceipt({ ...receipt, ...mismatch }, request, "routine-1", "version-1");
      } catch (error) {
        expect(error).toMatchObject({ code: "procedure_response_invalid" });
      }
    }
    expect(() => correlateInvocationReceipt(receipt, request, "routine-1", "version-1")).not.toThrow();
    expect(() => correlateInvocationReceipt(receipt, request, "routine-1", null)).toThrow(/server-owned routine version/);
  });

  it("rejects schedule receipts that do not correlate to the finite request and registered action", () => {
    const request: ProcedureV2ScheduleRequest = {
      version: 1,
      expected_routine_revision: 3,
      goal_id: "goal-2",
      expected_goal_revision: 4,
      parameters: { goal_id: "goal-2", expected_goal_revision: 4 },
      cadence: { kind: "daily", timezone: "UTC", daily_hour: 9, daily_minute: 0 },
      expires_at: "2026-10-05T09:00:00Z",
      idempotency_key: "schedule-1",
    };
    const receipt: ProcedureV2ScheduleReceipt = {
      status: "scheduled",
      scheduled_job_id: "job-1",
      binding_id: "binding-1",
      revision: 1,
      action_type: "guardian.run_procedure.v2",
      routine_id: "routine-1",
      version: 1,
      template_id: "public-browser-check",
      goal_id: "goal-2",
      goal_revision: 4,
      schedule_idempotency_key: "schedule-1",
      input_digest: digest,
      next_run: "2026-10-02T09:00:00Z",
      expires_at: request.expires_at,
      state: "active",
      pause_route: "/api/governed-schedules/binding-1",
      recovery_action: null,
      audit_receipt_id: null,
    };
    const mismatches: Array<[string, Partial<ProcedureV2ScheduleReceipt>]> = [
      ["routine", { routine_id: "routine-other" }],
      ["version", { version: 2 }],
      ["goal", { goal_id: "goal-other" }],
      ["goal revision", { goal_revision: 5 }],
      ["key", { schedule_idempotency_key: "schedule-other" }],
      ["action", { action_type: "other.action" as ProcedureV2ScheduleReceipt["action_type"] }],
      ["binding", { binding_id: "" }],
      ["job", { scheduled_job_id: "" }],
      ["template", { template_id: "selected-meeting-prep" }],
    ];
    for (const [, mismatch] of mismatches) {
      expect(() => correlateScheduleReceipt({ ...receipt, ...mismatch }, request, "routine-1"))
        .toThrowError(ProcedureV2ApiError);
      try {
        correlateScheduleReceipt({ ...receipt, ...mismatch }, request, "routine-1");
      } catch (error) {
        expect(error).toMatchObject({ code: "procedure_response_invalid" });
      }
    }
    expect(correlateScheduleReceipt(receipt, request, "routine-1")).toBe(receipt);
  });

  it("rejects a prepared response that omits the server-bound install approval", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({
      status: "prepared",
      binding_id: "binding-1",
      routine_id: "routine-1",
      version_id: "version-1",
      version: 1,
      schema_version: 2,
      template_id: "public-browser-check",
      revision: 1,
      request_digest: digest,
      preview_digest: digest,
      preview_expires_at: "2026-10-01T12:15:00Z",
      install_job_id: "routine-install:routine-1:v1",
      approval_id: null,
    }));
    vi.stubGlobal("fetch", fetchMock);
    try {
      await expect(procedureV2Api.prepareFromTasks({
        template_id: "public-browser-check",
        source_tasks: [{ task_id: "task-1", expected_revision: 3 }],
        name: "Public check",
        idempotency_key: "prepare-missing-approval",
        preview_digest: digest,
      })).rejects.toMatchObject({ code: "procedure_response_invalid" });
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("rejects a prepared response that omits canonical approval status and expiry", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({
      status: "prepared",
      binding_id: "binding-1",
      routine_id: "routine-1",
      version_id: "version-1",
      version: 1,
      schema_version: 2,
      template_id: "public-browser-check",
      revision: 1,
      request_digest: digest,
      preview_digest: digest,
      preview_expires_at: "2026-10-01T12:15:00Z",
      install_job_id: "routine-install:routine-1:v1",
      approval_id: "approval-1",
    }));
    vi.stubGlobal("fetch", fetchMock);
    try {
      await expect(procedureV2Api.prepareFromTasks({
        template_id: "public-browser-check",
        source_tasks: [{ task_id: "task-1", expected_revision: 3 }],
        name: "Public check",
        idempotency_key: "prepare-missing-approval-state",
        preview_digest: digest,
      })).rejects.toMatchObject({ code: "procedure_response_invalid" });
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("rejects a routine readback with statusless install approval metadata", async () => {
    const routine = {
      id: "routine-statusless",
      owner_principal_id: "operator:one",
      state: "prepared",
      revision: 1,
      current_version: 1,
      name: "Statusless approval",
      versions: [{
        id: "version-statusless",
        routine_id: "routine-statusless",
        version: 1,
        workflow_sha256: digest,
        runbook_sha256: digest,
        installed_package_digest: null,
        source_provenance: {},
        source_repository: null,
        source_action: null,
        source_issue_number: null,
        created_at: "2026-10-01T12:00:00Z",
        installed_at: null,
        schema_version: 2,
        template_id: "public-browser-check",
        procedure_binding: {
          binding_id: "binding-statusless",
          state: "prepared",
          revision: 1,
          preview_digest: digest,
          preview_expires_at: "2026-10-01T12:15:00Z",
          install_job_id: "install-statusless",
          approval_id: "approval-statusless",
        },
      }],
      package: { status: "not_installed", digest: null, review_id: null },
    };
    const fetchMock = vi.fn().mockResolvedValue(response(routine));
    vi.stubGlobal("fetch", fetchMock);
    try {
      await expect(procedureV2Api.getRoutine("routine-statusless")).rejects.toMatchObject({ code: "procedure_response_invalid" });
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("retains the server-owned install approval lifecycle metadata", async () => {
    const routine = {
      id: "routine-approval",
      owner_principal_id: "operator:one",
      state: "prepared",
      revision: 2,
      current_version: 1,
      name: "Approval metadata",
      versions: [{
        id: "version-approval",
        routine_id: "routine-approval",
        version: 1,
        workflow_sha256: digest,
        runbook_sha256: digest,
        installed_package_digest: null,
        source_provenance: {},
        source_repository: null,
        source_action: null,
        source_issue_number: null,
        created_at: "2026-10-01T12:00:00Z",
        installed_at: null,
        schema_version: 2,
        template_id: "public-browser-check",
        procedure_binding: {
          binding_id: "binding-approval",
          state: "prepared",
          revision: 2,
          preview_digest: digest,
          preview_expires_at: "2026-10-01T12:15:00Z",
          install_job_id: "install-approval",
          approval_id: "approval-approval",
          install_approval_status: "expired",
          install_approval_expires_at: "2026-10-01T12:10:00Z",
          install_recovery_action: "create_fresh_preview",
        },
      }],
      package: { status: "not_installed", digest: null, review_id: null },
    };
    const fetchMock = vi.fn().mockResolvedValue(response(routine));
    vi.stubGlobal("fetch", fetchMock);
    try {
      const result = await procedureV2Api.getRoutine("routine-approval");
      expect(result.versions[0].procedure_binding).toMatchObject({
        install_approval_status: "expired",
        install_approval_expires_at: "2026-10-01T12:10:00Z",
        install_recovery_action: "create_fresh_preview",
      });
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("keeps watch and meeting parameters in their exact typed envelopes", async () => {
    const receipt = {
      status: "accepted",
      routine_id: "routine-1",
      version: 1,
      schema_version: 2,
      template_id: "watch-and-public-browser",
      invocation_uuid: "invoke-watch",
      scope: "procedure-v2:routine-1:version-1",
      goal_id: "goal-2",
      goal_revision: 4,
      task_id: "task-watch",
      attempt_id: "attempt-watch",
      job_id: "job-watch",
      input_artifact_id: "artifact-watch",
      input_digest: digest,
      plan_digest: digest,
      revision: 3,
      audit_receipt_id: null,
      recovery_action: null,
    };
    const fetchMock = vi.fn().mockResolvedValue(response(receipt));
    vi.stubGlobal("fetch", fetchMock);
    try {
      await procedureV2Api.invoke("routine-1", {
        version: 1,
        expected_routine_revision: 3,
        goal_id: "goal-2",
        expected_goal_revision: 4,
        parameters: { goal_id: "goal-2", expected_goal_revision: 4, source_watch_id: "watch-1", expected_watch_revision: 7 },
        invocation_uuid: "invoke-watch",
      });
      const watchBody = JSON.parse(fetchMock.mock.calls[0][1].body as string) as Record<string, unknown>;
      expect(watchBody.parameters).toEqual({ goal_id: "goal-2", expected_goal_revision: 4, source_watch_id: "watch-1", expected_watch_revision: 7 });

      fetchMock.mockResolvedValueOnce(response({ ...receipt, template_id: "selected-meeting-prep", invocation_uuid: "invoke-meeting", task_id: "task-meeting" }));
      await procedureV2Api.invoke("routine-1", {
        version: 1,
        expected_routine_revision: 3,
        goal_id: "goal-2",
        expected_goal_revision: 4,
        parameters: {
          schema_version: 1,
          consent_id: "consent-1",
          event_binding_id: "event-binding-1",
          expected_event_binding_revision: 2,
          expected_consent_revision: 3,
          expected_connection_revision: 4,
          event_revision: digest,
          calendar_list_revision: digest,
          goal_id: "goal-2",
          goal_revision: 4,
          purpose: "bounded preparation request",
        },
        invocation_uuid: "invoke-meeting",
      });
      const meetingBody = JSON.parse(fetchMock.mock.calls[1][1].body as string) as Record<string, unknown>;
      expect(meetingBody.parameters).toEqual({
        schema_version: 1,
        consent_id: "consent-1",
        event_binding_id: "event-binding-1",
        expected_event_binding_revision: 2,
        expected_consent_revision: 3,
        expected_connection_revision: 4,
        event_revision: digest,
        calendar_list_revision: digest,
        goal_id: "goal-2",
        goal_revision: 4,
        purpose: "bounded preparation request",
      });
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("uses the existing package lifecycle and governed schedule control routes", async () => {
    const packagePreview = {
      routine_id: "routine-1", version: 1, pack_id: "pack-1", digest, installed_package_digest: digest, review_id: "review-1", status: "active",
      manifest: { display_name: "Public check", summary: "Fixed", version: "1", authority: { tools: [], filesystem: [], network: false, secrets: [], approval: "operator" }, resources: { max_runtime_seconds: 300, max_artifact_bytes: 1000, max_inference_cost_microusd: 0, inference_priority: "normal" }, data_policy: { classes: [], egress: [] } },
      runbook: { title: "Public check", summary: "Fixed", procedure: { schema_version: 1, capability_id: "guardian-routine.v1", steps: [{ id: "guardian_watch_run", capability: "guardian_watch_run", tool: "guardian_watch_run" }] }, bindings: { workflow_sha256: digest, legacy_runbook_sha256: digest, source_provenance_sha256: digest } },
    };
    const schedule = { status: "scheduled", scheduled_job_id: "schedule-1", binding_id: "binding-1", revision: 1, action_type: "guardian.run_procedure.v2", routine_id: "routine-1", version: 1, template_id: "public-browser-check", goal_id: "goal-1", goal_revision: 4, schedule_idempotency_key: "schedule-1", input_digest: digest, next_run: "2026-10-02T09:00:00Z", expires_at: "2026-10-05T09:00:00Z", state: "active", pause_route: "/api/governed-schedules/binding-1", recovery_action: null };
    const activeRoutine = {
      id: "routine-1",
      owner_principal_id: "operator:one",
      state: "active",
      revision: 4,
      current_version: 1,
      name: "Public check",
      versions: [{
        id: "version-1",
        routine_id: "routine-1",
        version: 1,
        workflow_sha256: digest,
        runbook_sha256: digest,
        installed_package_digest: digest,
        source_provenance: {},
        source_repository: null,
        source_action: null,
        source_issue_number: null,
        created_at: "2026-10-01T12:00:00Z",
        installed_at: "2026-10-01T12:00:00Z",
      }],
      package: { status: "active", digest, review_id: "review-1" },
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(packagePreview))
      .mockResolvedValueOnce(response({ digest, status: "active" }))
      .mockResolvedValueOnce(response(activeRoutine))
      .mockResolvedValueOnce(response(schedule))
      .mockResolvedValueOnce(response({ ...schedule, status: "paused", state: "paused", revision: 2 }))
      .mockResolvedValueOnce(response({ ...schedule, status: "revoked", state: "revoked", revision: 3 }));
    vi.stubGlobal("fetch", fetchMock);
    try {
      await procedureV2Api.packagePreview("routine-1", 1, 3);
      await procedureV2Api.activatePackage("routine-1", 1, 3, "approval-1");
      await procedureV2Api.lifecycle("routine-1", "activate", { version: 1, expected_routine_revision: 3 });
      await procedureV2Api.schedule("routine-1", {
        version: 1,
        expected_routine_revision: 3,
        goal_id: "goal-1",
        expected_goal_revision: 4,
        parameters: { goal_id: "goal-1", expected_goal_revision: 4 },
        cadence: { kind: "daily", timezone: "Europe/Warsaw", daily_hour: 9, daily_minute: 0 },
        expires_at: "2026-10-05T09:00:00Z",
        idempotency_key: "schedule-1",
      });
      await procedureV2Api.scheduleControl("binding-1", { action: "pause", expected_binding_revision: 1, idempotency_key: "pause-1" });
      await procedureV2Api.revokeSchedule("binding-1", { expected_binding_revision: 2, idempotency_key: "revoke-1", reason: "operator requested revoke" });
      expect(fetchMock.mock.calls.map((call) => `${call[1]?.method ?? "GET"} ${String(call[0])}`)).toEqual([
        expect.stringContaining("POST http"),
        expect.stringContaining("POST http"),
        expect.stringContaining("POST http"),
        expect.stringContaining("POST http"),
        expect.stringContaining("PATCH http"),
        expect.stringContaining("POST http"),
      ]);
      expect(String(fetchMock.mock.calls[2][0])).toContain("/api/capabilities/routines/routine-1/activate");
      expect(fetchMock.mock.calls[2][1].body).toBe(JSON.stringify({ version: 1, expected_routine_revision: 3 }));
      expect(fetchMock.mock.calls[4][1].body).toContain('"action":"pause"');
      expect(fetchMock.mock.calls[5][1].body).toContain('"reason":"operator requested revoke"');
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("reads back the canonical routine after a committed pause or revoke receipt", async () => {
    const routine = {
      id: "routine-lifecycle",
      owner_principal_id: "operator:single",
      state: "paused",
      revision: 4,
      current_version: 1,
      name: "Stable lifecycle routine",
      versions: [{
        id: "version-lifecycle",
        routine_id: "routine-lifecycle",
        version: 1,
        workflow_sha256: digest,
        runbook_sha256: digest,
        installed_package_digest: digest,
        source_provenance: {},
        source_repository: null,
        source_action: null,
        source_issue_number: null,
        created_at: "2026-10-01T12:00:00Z",
        installed_at: "2026-10-01T12:00:00Z",
      }],
      package: { status: "active", digest, review_id: "review-lifecycle" },
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({ status: "paused", routine_id: routine.id, reason: "operator requested pause" }))
      .mockResolvedValueOnce(response(routine))
      .mockResolvedValueOnce(response({ status: "revoked", routine_id: routine.id, reason: "operator requested revoke" }))
      .mockResolvedValueOnce(response({ ...routine, state: "revoked", revision: 5 }));
    vi.stubGlobal("fetch", fetchMock);
    try {
      const paused = await procedureV2Api.lifecycle(routine.id, "pause", { expected_routine_revision: 3, reason: "operator requested pause" });
      expect(paused.state).toBe("paused");
      const revoked = await procedureV2Api.lifecycle(routine.id, "revoke", { expected_routine_revision: 4, reason: "operator requested revoke" });
      expect(revoked.state).toBe("revoked");
      expect(fetchMock.mock.calls.map((call) => `${call[1]?.method ?? "GET"} ${String(call[0])}`)).toEqual([
        expect.stringContaining("POST http://localhost:8004/api/capabilities/routines/routine-lifecycle/pause"),
        expect.stringContaining("GET http://localhost:8004/api/capabilities/routines/routine-lifecycle"),
        expect.stringContaining("POST http://localhost:8004/api/capabilities/routines/routine-lifecycle/revoke"),
        expect.stringContaining("GET http://localhost:8004/api/capabilities/routines/routine-lifecycle"),
      ]);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("maps the governed error envelope without exposing raw provider details", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({
      detail: {
        code: "procedure_preview_expired",
        message: "The reviewed preview has expired",
        recovery_action: "preview_again",
        retryable: false,
        binding_id: null,
        audit_receipt_id: null,
        provider_secret: "must never be surfaced",
      },
    }, false, 409));
    vi.stubGlobal("fetch", fetchMock);
    try {
      try {
        await procedureV2Api.prepareFromTasks({
          template_id: "public-browser-check",
          source_tasks: [{ task_id: "task-1", expected_revision: 3 }],
          name: "Public check",
          idempotency_key: "prepare-key",
          preview_digest: digest,
        });
        throw new Error("expected prepare to fail");
      } catch (error) {
        expect(error).toBeInstanceOf(ProcedureV2ApiError);
        expect((error as ProcedureV2ApiError).code).toBe("procedure_preview_expired");
        expect((error as ProcedureV2ApiError).recoveryAction).toBe("preview_again");
        expect((error as Error).message).not.toContain("provider_secret");
      }
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
