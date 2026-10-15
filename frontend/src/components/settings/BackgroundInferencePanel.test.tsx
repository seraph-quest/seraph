import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { API_URL } from "../../config/constants";
import { BackgroundInferencePanel } from "./BackgroundInferencePanel";

function job(job_id: string, enabled = true, calls = 96) {
  return {
    job_id, label: job_id.replace(/_/g, " "), enabled, scope: "job", approx_calls_per_day: calls,
    estimate_basis: "one call per tick", last_skip: enabled ? null : { status: "skipped", reason: "disabled_by_operator", at: "2026-10-15T12:00:00+00:00" },
  };
}
function payload(strategistEnabled: boolean) {
  return { schema_version: "seraph.background-inference.v1", scheduler_enabled: true, error_code: null,
    jobs: [job("strategist_tick", strategistEnabled), job("daily_briefing", true, 1), job("weekly_activity_review", true, 0.14)] };
}
function response(value: unknown, ok = true) {
  return { ok, status: ok ? 200 : 503, json: async () => value } as Response;
}
afterEach(() => vi.unstubAllGlobals());

describe("BackgroundInferencePanel", () => {
  it("shows default-on jobs with call estimates and persists a per-job switch-off", async () => {
    const fetch = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => response(payload(init?.method !== "PUT")));
    vi.stubGlobal("fetch", fetch);
    render(<BackgroundInferencePanel />);
    const strategist = await screen.findByLabelText("Background inference: strategist tick");
    expect(strategist).toBeChecked();
    expect(screen.getByText(/~96\/day/)).toBeInTheDocument();
    expect(screen.getByText(/~1\/week/)).toBeInTheDocument();
    expect(screen.getByText(/up to ~97 text-route calls\/day enabled/)).toBeInTheDocument();
    fireEvent.click(strategist);
    await waitFor(() => expect(screen.getByLabelText("Background inference: strategist tick")).not.toBeChecked());
    const put = fetch.mock.calls.find(([, init]) => init?.method === "PUT")!;
    expect(put[0]).toBe(`${API_URL}/api/settings/background-inference`);
    expect(JSON.parse(String(put[1]?.body))).toEqual({ jobs: { strategist_tick: false } });
    expect(screen.getByText(/skipped: disabled by operator · 2026-10-15/)).toBeInTheDocument();
  });

  it("keeps last-known toggles visible when a refresh fails and reports a failed save", async () => {
    const fetch = vi.fn()
      .mockResolvedValueOnce(response(payload(true)))
      .mockResolvedValueOnce(response({}, false))
      .mockResolvedValueOnce(response({}, false));
    vi.stubGlobal("fetch", fetch);
    render(<BackgroundInferencePanel />);
    fireEvent.click(await screen.findByLabelText("Background inference: daily briefing"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Toggle was not saved (503)");
    expect(screen.getByLabelText("Background inference: daily briefing")).toBeChecked();
    expect(screen.getByLabelText("Background inference: strategist tick")).toBeEnabled();
  });

  it("refuses a single-toggle save over unreadable toggles and replaces them only after confirmation", async () => {
    const unreadable = { ...payload(true), error_code: "background_inference_settings_unreadable",
      jobs: payload(true).jobs.map((item) => ({ ...item, enabled: false, last_skip: null })) };
    const fetch = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => response(init?.method === "PUT"
      ? { ...payload(true), jobs: [job("strategist_tick", false), job("daily_briefing", true, 1), job("weekly_activity_review", false, 0.14)] }
      : unreadable));
    vi.stubGlobal("fetch", fetch);
    render(<BackgroundInferencePanel />);
    expect(await screen.findByText(/earlier choices are unknown/)).toBeInTheDocument();
    expect(screen.getByText(/Chat title generation/)).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("Background inference: daily briefing"));
    // Editing an unreadable set only changes the local draft.
    expect(fetch.mock.calls.some(([, init]) => init?.method === "PUT")).toBe(false);
    expect(screen.getByLabelText("Background inference: daily briefing")).toBeChecked();
    const replace = screen.getByRole("button", { name: "Replace stored toggles" });
    expect(replace).toBeDisabled();
    fireEvent.click(screen.getByLabelText("Confirm replacing unreadable background toggles"));
    fireEvent.click(replace);
    await waitFor(() => expect(screen.queryByText(/earlier choices are unknown/)).not.toBeInTheDocument());
    const put = fetch.mock.calls.find(([, init]) => init?.method === "PUT")!;
    expect(JSON.parse(String(put[1]?.body))).toEqual({
      jobs: { strategist_tick: false, daily_briefing: true, weekly_activity_review: false }, replace_unreadable: true,
    });
  });
});
