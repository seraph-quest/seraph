import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  BrowserTaskPolicy,
  GoalInfo,
  WorkBoardExecutionLimits,
  WorkBoardTask,
} from "../../types";
import { BrowserTaskForm } from "./BrowserTaskForm";

function response(payload: unknown, ok = true, status = ok ? 200 : 500) {
  return { ok, status, json: async () => payload };
}

const goal: GoalInfo = {
  id: "goal-1",
  parent_id: null,
  path: "/goal-1",
  level: "root",
  title: "Public research goal",
  description: "A bounded public browser goal",
  status: "active",
  domain: "research",
  start_date: null,
  due_date: null,
  sort_order: 0,
  revision: 3,
};

const policy: BrowserTaskPolicy = {
  policy_state: "confirmed",
  policy_source: "configured_site_policy",
  allowlist: { rules: ["public.example"], truncated: false, known: true },
  blocklist: { rules: ["private.example"], truncated: false, known: true },
  limits: {
    max_runtime_seconds: 180,
    hard_max_runtime_seconds: 180,
    max_actions: 8,
    max_navigations: 8,
    max_requests: 32,
    max_extract_bytes: 65_536,
    max_browser_contexts: 1,
    ready_capacity: 8,
    max_attempts: 2,
    max_outstanding_jobs: 8,
    inference: "none",
  },
};

function limits(overrides: Partial<WorkBoardExecutionLimits> = {}): WorkBoardExecutionLimits {
  return {
    goal_id: goal.id,
    goal_revision: goal.revision ?? 3,
    effective_max_runtime_seconds: 300,
    default_max_runtime_seconds: 300,
    hard_max_runtime_seconds: 900,
    attempt_limit: 2,
    max_outstanding_jobs: 8,
    limit_source: "goal_admission_budget",
    browser_task_policy: policy,
    ...overrides,
  };
}

function createdTask(): WorkBoardTask {
  return {
    task_id: "task-browser-1",
    creation_sequence: 1,
    owner_principal_id: "operator:one",
    owner_session_id: "operator-session-1",
    origin_session_id: "operator-session-1",
    origin_thread_id: null,
    goal_id: goal.id,
    goal_revision: goal.revision ?? 3,
    title: "Read approved public documentation",
    body: "Open the approved public pages and verify the requested extract.",
    capability_id: "browser.public-task.v1",
    input_artifact_id: "artifact-browser-1",
    typed_input_ref: null,
    typed_input_digest: null,
    executor_id: null,
    assignee_id: null,
    priority: 50,
    idempotency_scope: "task",
    idempotency_key: "browser-task-key",
    scheduled_at: null,
    status: "todo",
    block_kind: null,
    block_reason: null,
    block_source_status: null,
    cancel_requested_at: null,
    requires_review: false,
    reviewer_id: null,
    dependency_count: 0,
    completed_dependency_count: 0,
    dispatch_rank: null,
    recovery_action: null,
    readback_status: "not_started",
    verification_status: "not_started",
    task_revision: 1,
    result_refs: [],
    artifact_refs: [],
    latest_attempt: null,
    created_at: "2026-09-30T10:00:00Z",
    updated_at: "2026-09-30T10:00:00Z",
    completed_at: null,
    archived_at: null,
  };
}

function artifactReceipt() {
  return {
    artifact_id: "artifact-browser-1",
    typed_input_ref: "workspace-json:artifacts/work-board/inputs/artifact-browser-1.json",
    typed_input_digest: "a".repeat(64),
    capability_id: "browser.public-task.v1",
    goal_id: goal.id,
    goal_revision: goal.revision,
    expires_at: "2026-10-01T12:00:00Z",
  };
}

function taskReceipt() {
  return { task: createdTask(), idempotent_replay: false };
}

function isPost(init: RequestInit | undefined): boolean {
  return (init?.method ?? "GET").toUpperCase() === "POST";
}

function postBodies(fetchMock: ReturnType<typeof vi.fn>) {
  return fetchMock.mock.calls
    .filter(([, init]) => isPost(init as RequestInit | undefined))
    .map(([, init]) => JSON.parse(String((init as RequestInit).body)) as Record<string, unknown>);
}

function fillMinimalForm() {
  fireEvent.change(screen.getByLabelText("Browser start URL"), { target: { value: "https://public.example/docs" } });
  fireEvent.change(screen.getByLabelText("Approved URL prefixes"), { target: { value: "https://public.example/docs\nhttps://public.example/reference" } });
  fireEvent.change(screen.getByLabelText("Browser action 1 URL"), { target: { value: "https://public.example/reference" } });
  fireEvent.change(screen.getByLabelText("Final check 1 value"), { target: { value: "public.example" } });
  fireEvent.click(screen.getByRole("checkbox", { name: /I consent to this one bounded task/i }));
}

async function waitForPolicyState(state: string) {
  const region = screen.getByRole("region", { name: "Global browser site policy" });
  await waitFor(() => expect(region).toHaveTextContent(`State: ${state}`));
}

function installDefaultFetch(fetchMock: ReturnType<typeof vi.fn>, policyValue: BrowserTaskPolicy | null = policy) {
  fetchMock.mockImplementation((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/execution-limits")) return Promise.resolve(response(limits({ browser_task_policy: policyValue })));
    if (url.endsWith("/input-artifacts")) return Promise.resolve(response(artifactReceipt()));
    if (url.endsWith("/tasks")) return Promise.resolve(response(taskReceipt()));
    if (url.endsWith("/api/goals/tree")) return Promise.resolve(response([goal]));
    return Promise.resolve(response({}, true));
  });
}

describe("BrowserTaskForm", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("creates the strict input artifact before the opaque-id task envelope", async () => {
    installDefaultFetch(fetchMock);
    const onCreated = vi.fn();

    render(<BrowserTaskForm goals={[goal]} onCreated={onCreated} onClose={vi.fn()} />);
    await waitForPolicyState("confirmed");
    fillMinimalForm();
    fireEvent.click(screen.getByRole("button", { name: "Add extract" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Browser action 2 selector" }), { target: { value: "main h1" } });
    fireEvent.change(screen.getByRole("textbox", { name: /Action 2 check 1 value/i }), { target: { value: "Documentation" } });
    fireEvent.click(screen.getByRole("checkbox", { name: /I consent to this one bounded task/i }));
    fireEvent.click(screen.getByRole("button", { name: "Create public browser task" }));

    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(
      expect.objectContaining({ task_id: "task-browser-1" }),
      expect.objectContaining({ artifactId: "artifact-browser-1", actionCount: 2, digest: "a".repeat(64) }),
    ));
    const calls = fetchMock.mock.calls.filter(([, init]) => isPost(init as RequestInit | undefined));
    expect(calls).toHaveLength(2);
    expect(String(calls[0]?.[0])).toContain("/api/work-board/input-artifacts");
    expect(String(calls[1]?.[0])).toContain("/api/work-board/tasks");

    const [artifactBody, taskBody] = postBodies(fetchMock);
    expect(artifactBody).toMatchObject({
      schema_version: 1,
      capability_id: "browser.public-task.v1",
      goal_id: "goal-1",
      goal_revision: 3,
      idempotency_key: expect.stringMatching(/^browser-input:/),
    });
    expect(artifactBody.input).toMatchObject({
      schema_version: 1,
      start_url: "https://public.example/docs",
      allowed_hosts: ["public.example"],
      approved_url_prefixes: ["https://public.example/docs", "https://public.example/reference"],
    });
    expect(artifactBody.input).not.toHaveProperty("typed_input_ref");
    expect(artifactBody.input).not.toHaveProperty("owner_session_id");
    expect(artifactBody.input).not.toHaveProperty("budget");
    expect(taskBody).toEqual({
      title: "Read approved public documentation",
      body: "Open the approved public pages and verify the requested extract.",
      goal_id: "goal-1",
      goal_revision: 3,
      status: "todo",
      capability_id: "browser.public-task.v1",
      input_artifact_id: "artifact-browser-1",
      idempotency_key: expect.stringMatching(/^browser-task:/),
    });
    expect(taskBody).not.toHaveProperty("typed_input_ref");
    expect(taskBody).not.toHaveProperty("typed_input_digest");
    expect(screen.getByRole("status", { name: "" })).toHaveTextContent("a".repeat(64));
    expect(screen.getByText(/2 browser actions/)).toBeInTheDocument();
  });

  it("shows unknown or truncated policy metadata without claiming readiness", async () => {
    const unknownPolicy: BrowserTaskPolicy = {
      ...policy,
      policy_state: "unknown",
      policy_source: null,
      allowlist: { rules: [], known: false, truncated: false },
      blocklist: { rules: ["public.example"], known: true, truncated: true },
    };
    installDefaultFetch(fetchMock, unknownPolicy);

    render(<BrowserTaskForm goals={[goal]} onCreated={vi.fn()} onClose={vi.fn()} />);
    await waitForPolicyState("unknown");
    expect(screen.getByText("Allowlist: Unavailable")).toBeInTheDocument();
    expect(screen.getByText(/Blocklist: public.example \(truncated; not exhaustive\)/)).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Global browser site policy" })).toHaveTextContent("does not claim readiness");
    expect(fetchMock.mock.calls.some(([, init]) => isPost(init as RequestInit | undefined))).toBe(false);
  });

  it("replays an unknown artifact request with the same frozen body and no automatic retry", async () => {
    let artifactAttempts = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/execution-limits")) return Promise.resolve(response(limits()));
      if (url.endsWith("/input-artifacts")) {
        artifactAttempts += 1;
        return artifactAttempts === 1
          ? Promise.reject(new TypeError("network down"))
          : Promise.resolve(response(artifactReceipt()));
      }
      if (url.endsWith("/tasks")) return Promise.resolve(response(taskReceipt()));
      return Promise.resolve(response({}));
    });
    const onCreated = vi.fn();

    render(<BrowserTaskForm goals={[goal]} onCreated={onCreated} onClose={vi.fn()} />);
    await waitForPolicyState("confirmed");
    fillMinimalForm();
    fireEvent.click(screen.getByRole("button", { name: "Create public browser task" }));
    await screen.findByRole("button", { name: "Retry exact request" });
    expect(artifactAttempts).toBe(1);
    expect(onCreated).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Retry exact request" }));
    await waitFor(() => expect(onCreated).toHaveBeenCalled());
    const bodies = postBodies(fetchMock);
    const artifactBodies = bodies.filter((body) => body.input);
    expect(artifactBodies).toHaveLength(2);
    expect(artifactBodies[0]).toEqual(artifactBodies[1]);
  });

  it("replays only the task envelope after an unknown task result", async () => {
    let taskAttempts = 0;
    installDefaultFetch(fetchMock);
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/execution-limits")) return Promise.resolve(response(limits()));
      if (url.endsWith("/input-artifacts")) return Promise.resolve(response(artifactReceipt()));
      if (url.endsWith("/tasks")) {
        taskAttempts += 1;
        return taskAttempts === 1
          ? Promise.reject(new TypeError("connection lost after commit"))
          : Promise.resolve(response(taskReceipt()));
      }
      return Promise.resolve(response({}));
    });
    const onCreated = vi.fn();

    render(<BrowserTaskForm goals={[goal]} onCreated={onCreated} onClose={vi.fn()} />);
    await waitForPolicyState("confirmed");
    fillMinimalForm();
    fireEvent.click(screen.getByRole("button", { name: "Create public browser task" }));
    await screen.findByRole("button", { name: "Retry exact request" });
    expect(taskAttempts).toBe(1);
    fireEvent.click(screen.getByRole("button", { name: "Retry exact request" }));
    await waitFor(() => expect(onCreated).toHaveBeenCalled());

    const bodies = postBodies(fetchMock);
    const artifactBodies = bodies.filter((body) => body.capability_id === "browser.public-task.v1" && body.input);
    const taskBodies = bodies.filter((body) => body.input_artifact_id);
    expect(artifactBodies).toHaveLength(1);
    expect(taskBodies).toHaveLength(2);
    expect(taskBodies[0]).toEqual(taskBodies[1]);
  });

  it("keeps the exact request recoverable when artifact reservation times out", async () => {
    let artifactAttempts = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/execution-limits")) return Promise.resolve(response(limits()));
      if (url.endsWith("/input-artifacts")) {
        artifactAttempts += 1;
        return new Promise((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => reject(new Error("aborted")), { once: true });
        });
      }
      return Promise.resolve(response({}));
    });

    render(<BrowserTaskForm goals={[goal]} onCreated={vi.fn()} onClose={vi.fn()} />);
    await waitForPolicyState("confirmed");
    vi.useFakeTimers();
    try {
      fillMinimalForm();
      fireEvent.click(screen.getByRole("button", { name: "Create public browser task" }));
      expect(artifactAttempts).toBe(1);
      await act(async () => { await vi.advanceTimersByTimeAsync(15_000); });
      expect(screen.getByRole("button", { name: "Retry exact request" })).toBeInTheDocument();
      expect(screen.getByText(/timed out without a receipt/)).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it("clears a definitively rejected artifact request so a corrected submission gets a new key", async () => {
    let artifactAttempts = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/execution-limits")) return Promise.resolve(response(limits()));
      if (url.endsWith("/input-artifacts")) {
        artifactAttempts += 1;
        return artifactAttempts === 1
          ? Promise.resolve(response({ detail: { code: "input_invalid", message: "The exact prefix is invalid." } }, false, 422))
          : Promise.resolve(response(artifactReceipt()));
      }
      if (url.endsWith("/tasks")) return Promise.resolve(response(taskReceipt()));
      return Promise.resolve(response({}));
    });
    const onCreated = vi.fn();

    render(<BrowserTaskForm goals={[goal]} onCreated={onCreated} onClose={vi.fn()} />);
    await waitForPolicyState("confirmed");
    fillMinimalForm();
    fireEvent.click(screen.getByRole("button", { name: "Create public browser task" }));
    await screen.findByRole("alert");
    const firstBody = postBodies(fetchMock)[0];
    fireEvent.change(screen.getByLabelText("Browser task title"), { target: { value: "Corrected public read" } });
    fireEvent.click(screen.getByRole("button", { name: "Create public browser task" }));
    await waitFor(() => expect(onCreated).toHaveBeenCalled());
    const bodies = postBodies(fetchMock);
    expect(bodies[1]?.idempotency_key).not.toBe(firstBody.idempotency_key);
  });

  it("keeps a stale-goal request visible until the operator refreshes goal metadata", async () => {
    let taskCalls = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/execution-limits")) return Promise.resolve(response(limits()));
      if (url.endsWith("/input-artifacts")) return Promise.resolve(response(artifactReceipt()));
      if (url.endsWith("/tasks")) {
        taskCalls += 1;
        return taskCalls === 1
          ? Promise.resolve(response({ detail: { code: "goal_revision_stale", message: "Goal revision changed." } }, false, 409))
          : Promise.resolve(response(taskReceipt()));
      }
      if (url.endsWith("/api/goals/tree")) return Promise.resolve(response([{ ...goal, revision: 4 }]));
      return Promise.resolve(response({}));
    });
    render(<BrowserTaskForm goals={[goal]} onCreated={vi.fn()} onClose={vi.fn()} />);
    await waitForPolicyState("confirmed");
    fillMinimalForm();
    fireEvent.click(screen.getByRole("button", { name: "Create public browser task" }));
    await screen.findByRole("button", { name: "Refresh goal metadata and edit" });
    fireEvent.click(screen.getByRole("button", { name: "Refresh goal metadata and edit" }));
    await waitFor(() => expect(screen.getByDisplayValue("4")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: "Retry exact request" })).not.toBeInTheDocument();
    expect(screen.getByText(/Goal metadata refreshed/)).toBeInTheDocument();
  });

  it("aborts an in-flight artifact request when the form unmounts", async () => {
    let artifactSignal: AbortSignal | undefined;
    let releaseArtifact: (() => void) | undefined;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/execution-limits")) return Promise.resolve(response(limits()));
      if (url.endsWith("/input-artifacts")) {
        artifactSignal = init?.signal ?? undefined;
        return new Promise((resolve) => { releaseArtifact = () => resolve(response(artifactReceipt())); });
      }
      return Promise.resolve(response({}));
    });
    const rendered = render(<BrowserTaskForm goals={[goal]} onCreated={vi.fn()} onClose={vi.fn()} />);
    await waitForPolicyState("confirmed");
    fillMinimalForm();
    fireEvent.click(screen.getByRole("button", { name: "Create public browser task" }));
    await waitFor(() => expect(artifactSignal).toBeDefined());
    rendered.unmount();
    expect(artifactSignal?.aborted).toBe(true);
    releaseArtifact?.();
  });

  it("retains an ambiguous 429 artifact response for exact retry", async () => {
    let artifactAttempts = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/execution-limits")) return Promise.resolve(response(limits()));
      if (url.endsWith("/input-artifacts")) {
        artifactAttempts += 1;
        return artifactAttempts === 1
          ? Promise.resolve(response({ detail: { code: "rate_limited", message: "Try again." } }, false, 429))
          : Promise.resolve(response(artifactReceipt()));
      }
      if (url.endsWith("/tasks")) return Promise.resolve(response(taskReceipt()));
      return Promise.resolve(response({}));
    });
    const onCreated = vi.fn();

    render(<BrowserTaskForm goals={[goal]} onCreated={onCreated} onClose={vi.fn()} />);
    await waitForPolicyState("confirmed");
    fillMinimalForm();
    fireEvent.click(screen.getByRole("button", { name: "Create public browser task" }));
    await screen.findByRole("button", { name: "Retry exact request" });
    const firstBody = postBodies(fetchMock)[0];
    fireEvent.click(screen.getByRole("button", { name: "Retry exact request" }));
    await waitFor(() => expect(onCreated).toHaveBeenCalled());
    expect(postBodies(fetchMock)[0]).toEqual(postBodies(fetchMock)[1]);
    expect(postBodies(fetchMock)[1]?.idempotency_key).toBe(firstBody?.idempotency_key);
  });

  it("treats a malformed 2xx artifact receipt as unknown and preserves the exact request", async () => {
    let artifactAttempts = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/execution-limits")) return Promise.resolve(response(limits()));
      if (url.endsWith("/input-artifacts")) {
        artifactAttempts += 1;
        return artifactAttempts === 1 ? Promise.resolve(response({})) : Promise.resolve(response(artifactReceipt()));
      }
      if (url.endsWith("/tasks")) return Promise.resolve(response(taskReceipt()));
      return Promise.resolve(response({}));
    });
    const onCreated = vi.fn();

    render(<BrowserTaskForm goals={[goal]} onCreated={onCreated} onClose={vi.fn()} />);
    await waitForPolicyState("confirmed");
    fillMinimalForm();
    fireEvent.click(screen.getByRole("button", { name: "Create public browser task" }));
    await screen.findByRole("button", { name: "Retry exact request" });
    const firstBody = postBodies(fetchMock)[0];
    fireEvent.click(screen.getByRole("button", { name: "Retry exact request" }));
    await waitFor(() => expect(onCreated).toHaveBeenCalled());
    expect(postBodies(fetchMock)[0]).toEqual(postBodies(fetchMock)[1]);
    expect(postBodies(fetchMock)[1]?.idempotency_key).toBe(firstBody?.idempotency_key);
  });

  it("counts the initial page load against the eight-navigation cap", async () => {
    installDefaultFetch(fetchMock);
    render(<BrowserTaskForm goals={[goal]} onCreated={vi.fn()} onClose={vi.fn()} />);
    await waitForPolicyState("confirmed");
    fillMinimalForm();
    for (let index = 0; index < 7; index += 1) {
      fireEvent.click(screen.getByRole("button", { name: "Add navigate" }));
    }
    screen.getAllByLabelText(/Browser action \d+ URL/).forEach((input) => {
      fireEvent.change(input, { target: { value: "https://public.example/reference" } });
    });
    const consent = screen.getByRole("checkbox", { name: /I consent to this one bounded task/i });
    fireEvent.click(consent);
    fireEvent.click(screen.getByRole("button", { name: "Create public browser task" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(/initial page load counts as one navigation/i);
    expect(postBodies(fetchMock)).toHaveLength(0);
  });

  it("does not close while the browser outcome is unknown", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/execution-limits")) return Promise.resolve(response(limits()));
      if (url.endsWith("/input-artifacts")) return Promise.reject(new TypeError("connection lost"));
      return Promise.resolve(response({}));
    });
    const onClose = vi.fn();
    render(<BrowserTaskForm goals={[goal]} onCreated={vi.fn()} onClose={onClose} />);
    await waitForPolicyState("confirmed");
    fillMinimalForm();
    fireEvent.click(screen.getByRole("button", { name: "Create public browser task" }));
    await screen.findByRole("button", { name: "Retry exact request" });
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent(/closing would discard/i);
  });
});
