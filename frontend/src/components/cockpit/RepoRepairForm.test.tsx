import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { GoalInfo } from "../../types";
import { REPO_REPAIR_TASK_BODY, RepoRepairForm } from "./RepoRepairForm";

function response(payload: unknown, ok = true, status = ok ? 200 : 500) {
  return { ok, status, json: async () => payload };
}

const goal: GoalInfo = {
  id: "goal-1",
  parent_id: null,
  path: "/goal-1",
  level: "root",
  title: "Repair project",
  description: "A bounded engineering goal",
  status: "active",
  domain: "engineering",
  start_date: null,
  due_date: null,
  sort_order: 0,
  revision: 3,
};

const artifact = {
  artifact_id: "artifact-repair-1",
  typed_input_ref: "workspace-json:artifacts/work-board/inputs/artifact-repair-1.json",
  typed_input_digest: "a".repeat(64),
  capability_id: "engineering.repo-repair.v1",
  goal_id: "goal-1",
  goal_revision: 3,
  expires_at: "2026-10-02T12:00:00Z",
};

function task(ownerSessionId = "operator-session-1") {
  return {
    task_id: "task-repair-1",
    title: "Repository repair: repo",
    body: REPO_REPAIR_TASK_BODY,
    capability_id: "engineering.repo-repair.v1",
    input_artifact_id: "artifact-repair-1",
    typed_input_digest: "a".repeat(64),
    goal_id: "goal-1",
    goal_revision: 3,
    owner_principal_id: "operator:one",
    owner_session_id: ownerSessionId,
    status: "todo",
  };
}

function isPost(init: RequestInit | undefined): boolean {
  return (init?.method ?? "GET").toUpperCase() === "POST";
}

function postCalls(fetchMock: ReturnType<typeof vi.fn>) {
  return fetchMock.mock.calls.filter(([, init]) => isPost(init as RequestInit | undefined));
}

function installFetch(fetchMock: ReturnType<typeof vi.fn>, taskValue = task()) {
  fetchMock.mockImplementation((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith("/input-artifacts")) return Promise.resolve(response(artifact));
    if (url.endsWith("/tasks")) return Promise.resolve(response({ task: taskValue, idempotent_replay: false }));
    return Promise.resolve(response({}));
  });
}

describe("RepoRepairForm", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("publishes only finite named Node script selections", async () => {
    installFetch(fetchMock);
    const onCreated = vi.fn();
    render(<RepoRepairForm goals={[goal]} ownerPrincipalId="operator:one" ownerSessionId="operator-session-1" onCreated={onCreated} onClose={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Repair source paths"), { target: { value: "src/app.ts" } });
    fireEvent.change(screen.getByLabelText("Repair allowed paths"), { target: { value: "src/app.ts\ntests/app.test.js" } });
    fireEvent.change(screen.getByLabelText("Repair test arguments"), { target: { value: "npm\nrun\nbuild\ntest" } });
    fireEvent.click(screen.getByRole("button", { name: "Create repository repair task" }));
    await waitFor(() => expect(onCreated).toHaveBeenCalled());
    const body = JSON.parse(String(postCalls(fetchMock)[0]?.[1]?.body));
    expect(body.input.test_args).toEqual(["npm", "run", "build", "test"]);
  });

  it("creates the strict input artifact before the opaque Todo task envelope", async () => {
    installFetch(fetchMock);
    const onCreated = vi.fn();

    render(<RepoRepairForm goals={[goal]} ownerPrincipalId="operator:one" ownerSessionId="operator-session-1" onCreated={onCreated} onClose={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Repository path"), { target: { value: "repos/seraph" } });
    fireEvent.change(screen.getByLabelText("Repair source paths"), { target: { value: "backend/src/api/work_board.py" } });
    fireEvent.change(screen.getByLabelText("Repair allowed paths"), { target: { value: "backend/src/api/work_board.py\nbackend/tests/test_work_board.py" } });
    fireEvent.change(screen.getByLabelText("Repair test arguments"), { target: { value: "pytest\nbackend/tests/test_work_board.py\n-q" } });
    fireEvent.click(screen.getByRole("button", { name: "Create repository repair task" }));

    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(
      expect.objectContaining({ task_id: "task-repair-1" }),
      { artifactId: "artifact-repair-1", digest: "a".repeat(64) },
    ));
    expect(postCalls(fetchMock)).toHaveLength(2);
    const artifactBody = JSON.parse(String(postCalls(fetchMock)[0]?.[1]?.body)) as Record<string, unknown>;
    const taskBody = JSON.parse(String(postCalls(fetchMock)[1]?.[1]?.body)) as Record<string, unknown>;
    expect(artifactBody).toMatchObject({
      schema_version: 1,
      capability_id: "engineering.repo-repair.v1",
      goal_id: "goal-1",
      goal_revision: 3,
      idempotency_key: expect.stringMatching(/^repo-repair-input:/),
  });
    expect(artifactBody.input).toEqual({
      repository_path: "repos/seraph",
      problem_statement: "Describe the bounded repository failure and the smallest safe repair.",
      acceptance_criteria: ["The focused test passes."],
      source_paths: ["backend/src/api/work_board.py"],
      allowed_paths: ["backend/src/api/work_board.py", "backend/tests/test_work_board.py"],
      test_args: ["pytest", "backend/tests/test_work_board.py", "-q"],
      evidence_refs: [],
    });
    expect(taskBody).toMatchObject({
      title: "Repository repair: repos/seraph",
      body: REPO_REPAIR_TASK_BODY,
      goal_id: "goal-1",
      goal_revision: 3,
      status: "todo",
      capability_id: "engineering.repo-repair.v1",
      input_artifact_id: "artifact-repair-1",
      idempotency_scope: "task",
      idempotency_key: expect.stringMatching(/^repo-repair-task:/),
    });
    expect(taskBody).not.toHaveProperty("typed_input_ref");
    expect(taskBody).not.toHaveProperty("typed_input_digest");
  });

  it("keeps source-like and secret-like problem text in the typed artifact only", async () => {
    installFetch(fetchMock);
    const privateProblem = "Inspect src/private_config.py; observed API_TOKEN=unknown-secret-value must stay private.";
    render(<RepoRepairForm goals={[goal]} ownerPrincipalId="operator:one" ownerSessionId="operator-session-1" onCreated={vi.fn()} onClose={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Repair problem statement"), { target: { value: privateProblem } });
    fireEvent.click(screen.getByRole("button", { name: "Create repository repair task" }));

    await waitFor(() => expect(postCalls(fetchMock)).toHaveLength(2));
    const calls = postCalls(fetchMock);
    const artifactBody = JSON.parse(String(calls[0]?.[1]?.body)) as Record<string, unknown>;
    const taskBody = JSON.parse(String(calls[1]?.[1]?.body)) as Record<string, unknown>;
    expect((artifactBody.input as Record<string, unknown>).problem_statement).toBe(privateProblem);
    expect(taskBody).toMatchObject({
      title: "Repository repair: repo",
      body: REPO_REPAIR_TASK_BODY,
    });
    expect(JSON.stringify(taskBody)).not.toContain("src/private_config.py");
    expect(JSON.stringify(taskBody)).not.toContain("unknown-secret-value");
    expect(JSON.stringify(taskBody)).not.toContain("API_TOKEN");
  });

  it("rejects unsafe metadata locally without creating an artifact or task", async () => {
    installFetch(fetchMock);
    render(<RepoRepairForm goals={[goal]} ownerPrincipalId="operator:one" ownerSessionId="operator-session-1" onCreated={vi.fn()} onClose={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Repair source paths"), { target: { value: ".env" } });
    fireEvent.click(screen.getByRole("button", { name: "Create repository repair task" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("protected path");
    expect(postCalls(fetchMock)).toHaveLength(0);
  });

  it("fails closed when the task receipt belongs to another owner session", async () => {
    installFetch(fetchMock, task("operator-session-other"));
    const onCreated = vi.fn();
    render(<RepoRepairForm goals={[goal]} ownerPrincipalId="operator:one" ownerSessionId="operator-session-1" onCreated={onCreated} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Create repository repair task" }));

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("receipt was not confirmed"));
    expect(onCreated).not.toHaveBeenCalled();
    expect(screen.getByRole("status")).toHaveTextContent("Exact request is retained");
  });

  it("retries an uncertain artifact request with the exact same body and keys", async () => {
    let artifactAttempts = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/input-artifacts")) {
        artifactAttempts += 1;
        return artifactAttempts === 1 ? Promise.reject(new TypeError("network down")) : Promise.resolve(response(artifact));
      }
      if (url.endsWith("/tasks")) return Promise.resolve(response({ task: task(), idempotent_replay: true }));
      return Promise.resolve(response({}));
    });
    const onCreated = vi.fn();
    render(<RepoRepairForm goals={[goal]} ownerPrincipalId="operator:one" ownerSessionId="operator-session-1" onCreated={onCreated} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Create repository repair task" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Exact request is retained"));
    expect(screen.queryByRole("button", { name: /Discard exact request/i })).not.toBeInTheDocument();
    const firstBody = String(postCalls(fetchMock)[0]?.[1]?.body);
    fireEvent.click(screen.getByRole("button", { name: "Retry exact request" }));
    await waitFor(() => expect(onCreated).toHaveBeenCalled());
    const calls = postCalls(fetchMock);
    expect(calls).toHaveLength(3);
    expect(String(calls[0]?.[1]?.body)).toBe(firstBody);
    expect(String(calls[1]?.[1]?.body)).toBe(firstBody);
    expect(JSON.parse(String(calls[2]?.[1]?.body))).toMatchObject({ input_artifact_id: "artifact-repair-1" });
  });

  it("retries an uncertain task request with the same task body and key without recreating the artifact", async () => {
    let taskAttempts = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/input-artifacts")) return Promise.resolve(response(artifact));
      if (url.endsWith("/tasks")) {
        taskAttempts += 1;
        return taskAttempts === 1
          ? Promise.reject(new TypeError("task response lost"))
          : Promise.resolve(response({ task: task(), idempotent_replay: true }));
      }
      return Promise.resolve(response({}));
    });
    const onCreated = vi.fn();
    render(<RepoRepairForm goals={[goal]} ownerPrincipalId="operator:one" ownerSessionId="operator-session-1" onCreated={onCreated} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Create repository repair task" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Exact request is retained"));
    const callsAfterFailure = postCalls(fetchMock);
    expect(callsAfterFailure).toHaveLength(2);
    const firstTaskBody = String(callsAfterFailure[1]?.[1]?.body);
    fireEvent.click(screen.getByRole("button", { name: "Retry exact request" }));
    await waitFor(() => expect(onCreated).toHaveBeenCalled());
    const calls = postCalls(fetchMock);
    expect(calls).toHaveLength(3);
    expect(String(calls[1]?.[1]?.body)).toBe(firstTaskBody);
    expect(String(calls[2]?.[1]?.body)).toBe(firstTaskBody);
    expect(calls.filter(([input]) => String(input).endsWith("/input-artifacts"))).toHaveLength(1);
  });

  it("retains the exact request when the task digest does not match the reserved artifact", async () => {
    installFetch(fetchMock, { ...task(), typed_input_digest: "b".repeat(64) });
    const onCreated = vi.fn();
    render(<RepoRepairForm goals={[goal]} ownerPrincipalId="operator:one" ownerSessionId="operator-session-1" onCreated={onCreated} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Create repository repair task" }));

    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("receipt was not confirmed"));
    expect(onCreated).not.toHaveBeenCalled();
    expect(screen.getByRole("status")).toHaveTextContent("Exact request is retained");
    const calls = postCalls(fetchMock);
    expect(calls).toHaveLength(2);
    const taskBody = String(calls[1]?.[1]?.body);
    fireEvent.click(screen.getByRole("button", { name: "Retry exact request" }));
    await waitFor(() => expect(postCalls(fetchMock)).toHaveLength(3));
    expect(String(postCalls(fetchMock)[2]?.[1]?.body)).toBe(taskBody);
    expect(onCreated).not.toHaveBeenCalled();
  });

  it("clears the private draft when the authenticated owner session changes", async () => {
    installFetch(fetchMock);
    const view = render(<RepoRepairForm goals={[goal]} ownerPrincipalId="operator:one" ownerSessionId="operator-session-1" onCreated={vi.fn()} onClose={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("Repair problem statement"), { target: { value: "Private operator details" } });
    view.rerender(<RepoRepairForm goals={[goal]} ownerPrincipalId="operator:one" ownerSessionId="operator-session-2" onCreated={vi.fn()} onClose={vi.fn()} />);
    await waitFor(() => expect(screen.getByLabelText("Repair problem statement")).toHaveValue("Describe the bounded repository failure and the smallest safe repair."));
    expect(screen.getByRole("alert")).toHaveTextContent("authenticated owner session changed");
    });

  it("shows the server-owned executor receipt without accepting executor authority in the task form", async () => {
    const metadata = {
      executor_kind: "local",
      executor_profile: "local:repo-python-pytest-v1",
      executor_posture: {
        kind: "local",
        profile: "repo-python-pytest-v1",
        isolation_claim: "none",
        network_isolation: "not_verified",
        resource_enforcement: "admission_and_wall_timeout_only",
        host_access: "explicit_job_approval_required",
        image_digest: null,
        limits_digest: "a".repeat(64),
      },
      executor_posture_digest: "9cc8184ce2062898d42e10984571c18ccc2d5892db5269a4c10aec27d00ba522",
      local_host_approval_required: true,
      preparation_ready: true,
      execution_ready: false,
      preflight: { ok: true, status: "ready", reason: "local_staging_available" },
    };
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/api/settings/repo-sandbox")) return Promise.resolve(response(metadata));
      if (url.endsWith("/input-artifacts")) return Promise.resolve(response(artifact));
      if (url.endsWith("/tasks")) return Promise.resolve(response({ task: task(), idempotent_replay: false }));
      return Promise.resolve(response({}));
    });

    const onCreated = vi.fn();
    render(<RepoRepairForm goals={[goal]} ownerPrincipalId="operator:one" ownerSessionId="operator-session-1" onCreated={onCreated} onClose={vi.fn()} />);
    expect(await screen.findByText("local:repo-python-pytest-v1")).toBeInTheDocument();
    expect(screen.getByText(/this form has no authority to grant it/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/execution backend/i)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Create repository repair task" }));
    await waitFor(() => expect(onCreated).toHaveBeenCalled());
    const posts = postCalls(fetchMock);
    expect(posts).toHaveLength(2);
    expect(String(posts[1]?.[1]?.body)).not.toContain("executor_kind");
  });
});
