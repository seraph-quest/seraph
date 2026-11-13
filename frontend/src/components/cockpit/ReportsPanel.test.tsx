import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom/vitest";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { apiFetch } from "../../lib/api";
import { ReportsPanel } from "./ReportsPanel";

vi.mock("../../lib/api", () => ({ apiFetch: vi.fn() }));

const response = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
const sha = "a".repeat(64);
const item = (overrides: Record<string, unknown> = {}) => ({
  episode_id: "episode-ready",
  date: "2026-11-10",
  timezone: "Europe/Berlin",
  created_at: "2026-11-10T21:00:00+00:00",
  observed_at: "2026-11-10T21:00:00+00:00",
  status: "ready",
  blocked_reason: null,
  analysis_provider: "llm:openrouter:test-model",
  active_goal_count: 3,
  completed_goal_count: 1,
  total_tracked_minutes: 95,
  ...overrides,
});
const blockedItem = item({
  episode_id: "episode-blocked",
  date: "2026-11-09",
  status: "blocked",
  blocked_reason: "guardian_owner_unavailable",
  analysis_provider: "blocked:guardian_owner_unavailable",
});
const detail = (base: Record<string, unknown>, body: string) => ({
  ...base,
  body,
  summary: {
    source: "archive",
    total_observations: 4,
    total_tracked_minutes: 95,
    switch_count: 4,
    by_activity: { coding: 4200, browsing: 1500 },
    by_project: { seraph: 4200 },
    by_source: { screenshot_folder: 5700 },
  },
  artifacts: { report_text_sha256: sha, report_json_sha256: sha, raw_artifact_path_exposed: false, report_json_path: "/private/leak.json" },
});

function routes(map: Record<string, () => Response | Promise<Response>>) {
  vi.mocked(apiFetch).mockImplementation(async (input) => {
    const url = String(input);
    const key = Object.keys(map).find((path) => url.endsWith(path));
    if (!key) throw new Error(`unexpected request ${url}`);
    return map[key]();
  });
}

beforeEach(() => {
  vi.mocked(apiFetch).mockReset();
});

describe("ReportsPanel", () => {
  it("lists stored reports and loads the selected report detail", async () => {
    routes({
      "/api/reports/end-of-day?limit=30": () => response({ items: [item(), blockedItem], limit: 30 }),
      "/api/reports/end-of-day/episode-ready": () => response(detail(item(), "Day summary\n\nGoals vs day: aligned.")),
    });
    render(<ReportsPanel />);

    const row = await screen.findByTestId("report-row-episode-ready");
    expect(row.textContent).toContain("2026-11-10");
    expect(row.textContent).toContain("Ready");
    expect(row.textContent).toContain("llm:openrouter:test-model");
    expect(screen.getByTestId("report-row-episode-blocked").textContent).toContain("Blocked · guardian_owner_unavailable");

    fireEvent.click(row);
    const body = await screen.findByTestId("report-body");
    expect(body.tagName).toBe("PRE");
    expect(body).toHaveClass("whitespace-pre-wrap", "break-words");
    expect(body.textContent).toBe("Day summary\n\nGoals vs day: aligned.");
    expect(screen.getByRole("list", { name: "Activity mix" }).textContent).toContain("coding · 1h 10m");
    expect(screen.getByRole("list", { name: "Project mix" }).textContent).toContain("seraph");
    expect(screen.getByRole("list", { name: "Source mix" }).textContent).toContain("screenshot_folder");
    expect(screen.getByTestId("report-detail").textContent).toContain("3 active · 1 completed");
    expect(screen.getByTestId("cockpit-reports-section").textContent).not.toContain("/private/leak.json");
    expect(vi.mocked(apiFetch).mock.calls.every(([, init]) => !init?.method || init.method === "GET")).toBe(true);
  });

  it("shows a blocked report as a blocked notice, not as a report", async () => {
    routes({
      "/api/reports/end-of-day?limit=30": () => response({ items: [blockedItem], limit: 30 }),
      "/api/reports/end-of-day/episode-blocked": () =>
        response(detail(blockedItem, "End-of-day report analysis was blocked by the guardian runtime.")),
    });
    render(<ReportsPanel />);

    fireEvent.click(await screen.findByTestId("report-row-episode-blocked"));
    const notice = await screen.findByTestId("report-blocked-notice");
    expect(notice).toHaveAttribute("role", "status");
    expect(notice.textContent).toContain("blocked (guardian_owner_unavailable)");
    expect(notice.textContent).toContain("not a report");
    expect(screen.queryByTestId("report-body")).toBeNull();
  });

  it("explains the empty state and offers no generation control", async () => {
    routes({ "/api/reports/end-of-day?limit=30": () => response({ items: [], limit: 30 }) });
    render(<ReportsPanel />);

    const empty = await screen.findByTestId("reports-empty");
    expect(empty.textContent).toContain("No stored reports yet");
    expect(empty.textContent).toContain("scheduled end-of-day job");
    expect(screen.queryByRole("button", { name: /generate/i })).toBeNull();
  });

  it("keeps the last loaded list when a refresh fails", async () => {
    let calls = 0;
    routes({
      "/api/reports/end-of-day?limit=30": () => (++calls === 1 ? response({ items: [item()], limit: 30 }) : response({ detail: "x" }, 500)),
    });
    render(<ReportsPanel />);
    await screen.findByTestId("report-row-episode-ready");

    fireEvent.click(screen.getByRole("button", { name: "Refresh reports" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Showing the last loaded list");
    expect(alert.textContent).toContain("HTTP 500");
    expect(screen.getByTestId("report-row-episode-ready")).toBeInTheDocument();
  });

  it("rejects a malformed list response instead of rendering it", async () => {
    routes({ "/api/reports/end-of-day?limit=30": () => response({ items: [item({ status: "succeeded" })], limit: 30 }) });
    render(<ReportsPanel />);
    expect((await screen.findByRole("alert")).textContent).toContain("could not be read");
    expect(screen.queryByTestId("report-row-episode-ready")).toBeNull();
  });

  it("shows a detail fetch error without losing the list", async () => {
    routes({
      "/api/reports/end-of-day?limit=30": () => response({ items: [item()], limit: 30 }),
      "/api/reports/end-of-day/episode-ready": () => response({ detail: "End-of-day report not found" }, 404),
    });
    render(<ReportsPanel />);
    fireEvent.click(await screen.findByTestId("report-row-episode-ready"));
    expect((await screen.findByRole("alert")).textContent).toContain("HTTP 404");
    expect(screen.getByTestId("report-row-episode-ready")).toBeInTheDocument();
  });

  it("does not fetch while the operator is not authenticated", async () => {
    render(<ReportsPanel authenticated={false} />);
    expect(screen.getByRole("status").textContent).toContain("Sign in");
    await waitFor(() => expect(apiFetch).not.toHaveBeenCalled());
  });
});
