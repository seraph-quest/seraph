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

  const github = (overrides: Record<string, unknown> = {}) => ({
    source: "archive",
    status: "partial",
    message: "GitHub activity is incomplete: seraph-quest/x: access denied.",
    item_count: 3,
    items_available: true,
    reason_code: null,
    identity_login: "octo",
    model_context: "included",
    counts: { pr_merged: 1, commit: 2 },
    repositories: ["seraph-quest/seraph"],
    limitations: [{ code: "access_denied", repository: "seraph-quest/x" }],
    items: [
      { ref: "G1", repository: "seraph-quest/seraph", kind: "pr_merged", number: 1262, short_sha: null, title: "fix: determinism",
        url: "https://github.com/seraph-quest/seraph/pull/1262", occurred_at: "2026-11-10T12:02:00+00:00", same_title_count: 1 },
      { ref: "G2", repository: "seraph-quest/seraph", kind: "commit", number: null, short_sha: "abcdef1", title: "a commit",
        url: "https://evil.example/commit/abc", occurred_at: "2026-11-10T11:00:00+00:00", same_title_count: 2 },
    ],
    ...overrides,
  });
  const withGitHub = (githubActivity: unknown, goalEvidence: unknown = null) => ({
    ...detail(item(), "Shipped [G1], touched [G2], cited [G9]."),
    github_activity: githubActivity,
    goal_evidence: goalEvidence,
  });

  async function openReport(payload: unknown) {
    routes({
      "/api/reports/end-of-day?limit=30": () => response({ items: [item({ github_activity_status: "partial", github_activity_item_count: 3 })], limit: 30 }),
      "/api/reports/end-of-day/episode-ready": () => response(payload),
    });
    render(<ReportsPanel />);
    fireEvent.click(await screen.findByTestId("report-row-episode-ready"));
    return screen.findByTestId("report-body");
  }

  it("links [G#] only to stored items with a GitHub URL and shows the evidence card", async () => {
    const body = await openReport(
      withGitHub(github(), {
        status: "mapped",
        goals: [
          { ref: "A1", title: "Create AGI", level: "vision", domain: "productivity", parent_ref: null, github: ["G1"], screen: true, rolled_up_from: ["A2"] },
          { ref: "A2", title: "Q4", level: "quarterly", domain: "productivity", parent_ref: "A1", github: ["G1"], screen: false, rolled_up_from: [] },
          { ref: "A3", title: "Family time", level: "annual", domain: "health", parent_ref: null, github: [], screen: false, rolled_up_from: [] },
        ],
        goals_without_evidence: ["A3"],
        github_without_goal: ["G2"],
        rejected: { unknown_evidence: 0, unknown_goals: 0, urls_removed: 0 },
      }),
    );
    expect(body.textContent).toBe("Shipped [G1], touched [G2], cited [G9].");
    const links = Array.from(body.querySelectorAll("a"));
    expect(links.map((link) => link.textContent)).toEqual(["[G1]"]);
    expect(links[0].getAttribute("href")).toBe("https://github.com/seraph-quest/seraph/pull/1262");
    expect(links[0].getAttribute("rel")).toBe("noreferrer noopener");

    const card = screen.getByTestId("report-github-evidence");
    expect(screen.getByTestId("report-github-status").textContent).toContain("GitHub activity is incomplete");
    expect(card.textContent).toContain("Uses the operator's local gh login (full gh scope; Seraph only issues read GETs).");
    expect(card.textContent).toContain("2 commit · 1 PR merged");
    expect(screen.getByTestId("report-github-limitations").textContent).toContain(
      "seraph-quest/x: access denied",
    );
    const cardLinks = Array.from(card.querySelectorAll("a"));
    expect(cardLinks.map((link) => link.getAttribute("href"))).toEqual(["https://github.com/seraph-quest/seraph/pull/1262"]);
    expect(card.textContent).toContain("G2 · commit · seraph-quest/seraph abcdef1 · a commit (2 commits with this title)");
    expect(card.innerHTML).not.toContain("evil.example");

    const table = screen.getByTestId("report-goal-evidence");
    expect(table.textContent).toContain("A2 · Q4 (quarterly) · under A1");
    expect(table.textContent).toContain("G1 · from A2");
    expect(table.textContent).toContain("Goals without evidence today: A3");
    expect(table.textContent).toContain("GitHub records not tied to a goal: G2");
  });

  it.each([
    ["not_connected", "GitHub source not connected."],
    ["failed", "GitHub source failed: gh did not answer within 30 s and was stopped."],
    ["blocked", "GitHub source blocked: gh is not logged in to github.com for this user."],
    ["stopped", "GitHub source stopped: gh is now logged in as @other, not @octo."],
    ["revoked", "GitHub source was switched off by the operator."],
    ["not_collected", "GitHub source is on, but no collection ran for this day."],
    ["unavailable", "GitHub evidence could not be read from the local store."],
  ])("states the %s GitHub state with its sentence", async (status, message) => {
    await openReport(withGitHub(github({ status, message, items: [], counts: {} })));
    expect(screen.getByTestId("report-github-status").textContent).toBe(message);
    const card = screen.getByTestId("report-github-evidence");
    expect(card.textContent?.includes("Seraph only issues read GETs")).toBe(status !== "not_connected");
    expect(card.querySelectorAll("a")).toHaveLength(0);
  });

  it("states limitations such as two active sources in plain words", async () => {
    await openReport(
      withGitHub(github({ limitations: [{ code: "multiple_active_sources" }, { code: "some_new_code", repository: "a/b" }] })),
    );
    const list = screen.getByTestId("report-github-limitations");
    expect(list.textContent).toContain("More than one ownership identity has an active GitHub source");
    expect(list.textContent).toContain("a/b: some new code");
  });

  it("shows counts only when the archive did not verify, and no evidence block for older reports", async () => {
    await openReport(withGitHub(github({ source: "episode_metadata", items_available: false, items: [], counts: {} })));
    expect(screen.getByTestId("report-github-evidence").textContent).toContain("3 items recorded. The item list is unavailable");
    expect(screen.getByTestId("report-body").querySelectorAll("a")).toHaveLength(0);
  });

  it("renders a report without GitHub evidence exactly as before", async () => {
    const body = await openReport(detail(item(), "Plain [G1] body."));
    expect(body.textContent).toBe("Plain [G1] body.");
    expect(body.querySelectorAll("a")).toHaveLength(0);
    expect(screen.queryByTestId("report-github-evidence")).toBeNull();
    expect(screen.queryByTestId("report-goal-evidence")).toBeNull();
  });

  it("does not fetch while the operator is not authenticated", async () => {
    render(<ReportsPanel authenticated={false} />);
    expect(screen.getByRole("status").textContent).toContain("Sign in");
    await waitFor(() => expect(apiFetch).not.toHaveBeenCalled());
  });
});
