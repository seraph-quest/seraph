import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import "@testing-library/jest-dom/vitest";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { FirstResultSetup } from "./FirstResultSetup";
import { useChatStore } from "../../stores/chatStore";
import type { SetupProgress } from "../../lib/firstResultSetup";

describe("FirstResultSetup", () => {
  let saved: SetupProgress | null;
  let phase: string;
  let unavailable: boolean;
  const fetchMock = vi.fn();
  const openTask = vi.fn();
  const openSection = vi.fn();
  const response = (payload: unknown, ok = true) => ({ ok, status: ok ? 200 : 503, json: async () => payload });
  beforeEach(() => {
    saved = null; phase = "todo"; unavailable = false;
    useChatStore.setState({ onboardingCompleted: false });
    fetchMock.mockReset(); openTask.mockReset(); openSection.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    fetchMock.mockImplementation(async (input: string, options?: RequestInit) => {
      const url = String(input);
      const body = options?.body ? JSON.parse(String(options.body)) : null;
      if (url.endsWith("/onboarding/progress")) {
        if (unavailable) return response({ detail: { code: "metadata_unavailable" } }, false);
        if (options?.method === "PUT") saved = body;
        return response({ progress: saved });
      }
      if (url.endsWith("/onboarding/starter")) return response({ goal: { id: "goal-1", revision: 1 }, watch: null });
      if (url.endsWith("/goals")) return response(options?.method === "POST" ? { id: "goal-1", revision: 1 } : []);
      if (url.endsWith("/input-artifacts")) return response({ artifact_id: "input-1" });
      if (url.endsWith("/tasks")) return response({ task: { task_id: "task-1", task_revision: 1, status: phase } });
      if (url.endsWith("/tasks/task-1/actions")) { phase = "ready"; return response({ task: { task_id: "task-1", status: phase, task_revision: 2 } }); }
      if (url.endsWith("/tasks/task-1")) return response({ task: { task_id: "task-1", task_revision: 2, status: phase, readback_status: phase === "done" ? "verified" : "pending", verification_status: phase === "done" ? "passed" : "pending" } });
      if (url.endsWith("/onboarding/result/task-1/open")) return response({ progress: { ...saved, step: "result_opened" }, content: "Goal snapshot\nMy first verified goal", file_path: "artifacts/first-result/journey.md", content_sha256: "a".repeat(64), readback_id: "readback-1", memory_status: "no_learning" });
      if (url.endsWith("/onboarding/skip")) return response({ onboarding_completed: true });
      return response({});
    });
  });
  afterEach(() => vi.unstubAllGlobals());
  const mount = () => render(<FirstResultSetup onOpenTask={openTask} onOpenSection={openSection} />);

  it("completes a keyboard-accessible local task journey only after result readback", async () => {
    mount();
    const review = await screen.findByRole("button", { name: "Review starter bounds" });
    await waitFor(() => expect(review).toBeEnabled());
    review.focus();
    await userEvent.keyboard("{Enter}");
    expect(await screen.findByText(/Model spend: \$0/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Create and queue bounded starter" }));
    await screen.findByText(/Typed task queued in Work/);
    expect(screen.queryByRole("button", { name: "Open verified first result" })).not.toBeInTheDocument();
    expect(saved?.step).toBe("task_saved");
    phase = "done";
    fireEvent.click(screen.getByRole("button", { name: "Refresh task result" }));
    fireEvent.click(await screen.findByRole("button", { name: "Open verified first result" }));
    expect(await screen.findByLabelText("Verified first result")).toHaveTextContent("Goal snapshot");
    expect(screen.getByRole("status")).toHaveTextContent("Setup is complete");
    expect(useChatStore.getState().onboardingCompleted).toBe(true);
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("openrouter"))).toBe(false);
  });

  it("uses a server-compatible output path when randomUUID is unavailable", async () => {
    vi.stubGlobal("crypto", {});
    mount();
    await waitFor(() => expect(screen.getByRole("button", { name: "Review starter bounds" })).toBeEnabled());
    fireEvent.click(screen.getByRole("button", { name: "Review starter bounds" }));
    fireEvent.click(await screen.findByRole("button", { name: "Create and queue bounded starter" }));
    await screen.findByText(/Typed task queued in Work/);
    expect(saved?.journey_id).toMatch(/^setup-[a-zA-Z0-9-]+$/);
    const reserved = fetchMock.mock.calls.find(([url]) => String(url).endsWith("/input-artifacts"));
    expect(JSON.parse(String(reserved?.[1]?.body)).input.file_path).toMatch(/^artifacts\/first-result\/[a-zA-Z0-9-]+\.md$/);
  });

  it("restores the same task across reload without creating duplicate work", async () => {
    saved = { starter: "local_snapshot", step: "admitted", journey_id: "resume", title: "Kept goal", source: "", goal_id: "goal-1", goal_revision: 1, task_id: "task-1" };
    phase = "ready";
    const first = mount();
    expect(await screen.findByText(/Task: task-1/)).toBeInTheDocument();
    first.unmount(); mount();
    expect(await screen.findByText(/Task: task-1/)).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([, options]) => options?.method === "POST")).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Open task in Work" }));
    expect(openTask).toHaveBeenCalledWith("task-1");
  });

  it("retains inputs through metadata failure, blocks duplicate creation, and keeps skip available", async () => {
    unavailable = true; mount();
    await screen.findByRole("alert");
    fireEvent.change(screen.getByLabelText("Goal title"), { target: { value: "Retained title" } });
    expect(screen.getByLabelText("Goal title")).toHaveValue("Retained title");
    expect(screen.getByRole("button", { name: "Review starter bounds" })).toBeDisabled();
    unavailable = false;
    fireEvent.click(screen.getByRole("button", { name: "Retry saved setup" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Review starter bounds" })).toBeEnabled());
    expect(screen.getByLabelText("Goal title")).toHaveValue("Retained title");
    fireEvent.click(screen.getByRole("button", { name: "Skip setup" }));
    await waitFor(() => expect(screen.queryByText("Your first verified result")).not.toBeInTheDocument());
    expect(useChatStore.getState().onboardingCompleted).toBe(true);
  });

  it("shows public-source bounds before granting consent and preserves the saved draft", async () => {
    mount();
    await waitFor(() => expect(screen.getByRole("button", { name: "Review starter bounds" })).toBeEnabled());
    fireEvent.click(screen.getByLabelText(/Public source baseline/));
    fireEvent.change(screen.getByLabelText("Public HTTPS text URL"), { target: { value: "https://example.org/updates.txt" } });
    fireEvent.click(screen.getByRole("button", { name: "Review starter bounds" }));
    expect(await screen.findByText(/at most 256 KiB/)).toHaveTextContent("first scan initializes a baseline");
    expect(saved?.source).toBe("https://example.org/updates.txt");
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes("/capabilities/source-watches"))).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Review source connections" }));
    expect(openSection).toHaveBeenCalledWith("connections");
  });

  it("finishes the public baseline then pauses its disabled schedule and opens both receipts", async () => {
    const base = fetchMock.getMockImplementation()!;
    let watchDone = false;
    let watchState = "active";
    fetchMock.mockImplementation(async (input: string, options?: RequestInit) => {
      const url = String(input);
      const body = options?.body ? JSON.parse(String(options.body)) : null;
      if (url.endsWith("/onboarding/starter")) return response({ goal: { id: "goal-1", revision: 1 }, watch: { id: "watch-1", plan_revision: 1 } });
      if (url.endsWith("/source-watches/watch-1")) { if (options?.method === "PATCH") watchState = "paused"; return response({ state: watchState, plan_revision: 1 }); }
      if (url.endsWith("/tasks") && body?.capability_id === "guardian.research-watch.v1") return response({ task: { task_id: "watch-task", task_revision: 1, status: "todo" } });
      if (url.endsWith("/tasks/watch-task")) return response({ task: { task_id: "watch-task", task_revision: 2, status: watchDone ? "done" : "todo", readback_status: watchDone ? "verified" : "pending", verification_status: watchDone ? "passed" : "pending" } });
      if (url.endsWith("/onboarding/result/task-1/open")) return response({ progress: { ...saved, step: "result_opened" }, content: "Public-source goal snapshot", file_path: "artifacts/first-result/public.md", readback_id: "snapshot-proof", content_sha256: "a".repeat(64), memory_status: "no_learning", observation: { status: "baseline_initialized", sources: [{ target: "https://example.org/updates.txt" }], baselines: [{ sha256: "b".repeat(64) }], readback_id: "source-proof", memory_status: "no_learning", schedule_state: "paused" } });
      return base(input, options);
    });
    mount();
    await waitFor(() => expect(screen.getByRole("button", { name: "Review starter bounds" })).toBeEnabled());
    fireEvent.click(screen.getByLabelText(/Public source baseline/));
    fireEvent.change(screen.getByLabelText("Public HTTPS text URL"), { target: { value: "https://example.org/updates.txt" } });
    fireEvent.click(screen.getByRole("button", { name: "Review starter bounds" }));
    fireEvent.click(await screen.findByRole("button", { name: "Create and queue bounded starter" }));
    await screen.findByText(/Task: watch-task/);
    watchDone = true;
    fireEvent.click(screen.getByRole("button", { name: "Refresh task result" }));
    fireEvent.click(await screen.findByRole("button", { name: "Pause watch and create local result" }));
    await screen.findByText(/Task: task-1/);
    expect(watchState).toBe("paused");
    phase = "done";
    fireEvent.click(screen.getByRole("button", { name: "Refresh task result" }));
    fireEvent.click(await screen.findByRole("button", { name: "Open verified first result" }));
    const result = await screen.findByLabelText("Verified first result");
    expect(result).toHaveTextContent("baseline_initialized");
    expect(result).toHaveTextContent("source-proof");
    expect(result).toHaveTextContent("Public-source goal snapshot");
    const starterCreate = fetchMock.mock.calls.find(([url, options]) => String(url).endsWith("/onboarding/starter") && options?.method === "POST");
    expect(JSON.parse(String(starterCreate?.[1]?.body)).source).toBe("https://example.org/updates.txt");
  });

  it("retains edited inputs when saved metadata returns after a failure", async () => {
    saved = { starter: "local_snapshot", step: "preview", journey_id: "saved", title: "Old title", source: "" };
    unavailable = true; mount();
    await screen.findByRole("alert");
    fireEvent.change(screen.getByLabelText("Goal title"), { target: { value: "Fresh retained title" } });
    unavailable = false;
    fireEvent.click(screen.getByRole("button", { name: "Retry saved setup" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Review starter bounds" })).toBeEnabled());
    expect(screen.getByLabelText("Goal title")).toHaveValue("Fresh retained title");
  });

  it("keeps existing users' workspace and onboarding state", () => {
    vi.stubGlobal("crypto", undefined);
    useChatStore.setState({ onboardingCompleted: true });
    mount();
    expect(screen.queryByText("Your first verified result")).not.toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
