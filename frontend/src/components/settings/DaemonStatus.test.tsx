import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DaemonStatus } from "./DaemonStatus";

function mockResponse(data: unknown, ok = true) {
  return {
    ok,
    json: async () => data,
  };
}

describe("DaemonStatus", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("renders native presence status details from the daemon endpoint", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({
        daemon: {
          connected: true,
          last_post: 123,
          active_window: "VS Code — main.py",
          has_screen_context: true,
          capture_mode: "balanced",
          pending_notification_count: 2,
          last_native_notification_at: "2026-03-18T10:00:00Z",
          last_native_notification_title: "Seraph desktop shell",
          last_native_notification_outcome: "queued_test",
        },
        notifications: [
          {
            id: "notif-1",
            intervention_id: null,
            title: "Seraph desktop shell",
            body: "Native presence is connected. This is a test notification.",
            intervention_type: "test",
            urgency: 1,
            created_at: "2026-03-18T10:00:00Z",
          },
        ],
        queued_insights: [
          {
            id: "queue-1",
            intervention_id: "intervention-2",
            content_excerpt: "Bundle this until the browser is back.",
            intervention_type: "advisory",
            urgency: 3,
            reasoning: "high_interruption_cost",
            created_at: "2026-03-18T10:01:00Z",
          },
        ],
        queued_insight_count: 1,
        recent_interventions: [
          {
            id: "intervention-1",
            session_id: "session-1",
            intervention_type: "alert",
            content_excerpt: "Desktop fallback is active.",
            policy_action: "act",
            policy_reason: "available_capacity",
            delivery_decision: "deliver",
            latest_outcome: "notification_acked",
            transport: "native_notification",
            notification_id: "notif-1",
            feedback_type: "acknowledged",
            updated_at: "2026-03-18T10:02:00Z",
            continuity_surface: "native_notification",
          },
        ],
        reach: {
          route_statuses: [
            {
              route: "live_delivery",
              label: "Live delivery",
              status: "fallback_active",
              summary: "Live delivery is falling back to native notification because no live browser session is connected for websocket delivery.",
              selected_transport: "native_notification",
              selected_mode: "fallback",
              repair_hint: "Keep a cockpit tab connected or route this delivery class to native notifications first.",
            },
          ],
        },
      }),
    );

    render(<DaemonStatus />);

    await waitFor(() => expect(screen.getByText("Desktop link live")).toBeInTheDocument());
    expect(screen.getByText("Linked")).toBeInTheDocument();
    expect(screen.queryByText("Balanced")).not.toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
    expect(screen.getByText("Queued test notification")).toBeInTheDocument();
    expect(screen.getAllByText("Seraph desktop shell")).toHaveLength(2);
    expect(screen.getByText("Native presence is connected. This is a test notification.")).toBeInTheDocument();
    expect(screen.getByText("Deferred bundle items")).toBeInTheDocument();
    expect(screen.getByText("Bundle this until the browser is back.")).toBeInTheDocument();
    expect(screen.getByText("Delivery routing")).toBeInTheDocument();
    expect(screen.getByText("Live delivery")).toBeInTheDocument();
    expect(screen.getByText(/falling back to native notification/i)).toBeInTheDocument();
    expect(screen.getByText("Recent guardian continuity")).toBeInTheDocument();
    expect(screen.getByText("Desktop fallback is active.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Dismiss" })).toBeInTheDocument();
  });

  it("surfaces configured-off daemon state with recovery guidance", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({
        daemon: {
          connected: false,
          daemon_alive: false,
          last_post: null,
          active_window: null,
          has_screen_context: false,
          capture_mode: "on_switch",
          daemon_state: "disabled",
          last_error: "Native daemon is configured off for this environment.",
          last_error_kind: "configured_off",
          status_reason: "Native desktop presence is disabled by DAEMON_ENABLED=false.",
          recovery_hint: "Set DAEMON_ENABLED=true in .env.dev and start it with ./manage.sh -e dev daemon start when native desktop presence is wanted.",
          pending_notification_count: 0,
          last_native_notification_at: null,
          last_native_notification_title: null,
          last_native_notification_outcome: null,
        },
        notifications: [],
        queued_insights: [],
        queued_insight_count: 0,
        recent_interventions: [],
      }),
    );

    render(<DaemonStatus />);

    await waitFor(() => expect(screen.getByText("Daemon disabled")).toBeInTheDocument());
    expect(screen.getByText("Disabled")).toBeInTheDocument();
    expect(screen.getByText("Native desktop presence is disabled by DAEMON_ENABLED=false.")).toBeInTheDocument();
    expect(screen.getByText(/Set DAEMON_ENABLED=true/)).toBeInTheDocument();
  });

  it("surfaces macOS automation permission failures with an actionable hint", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({
        daemon: {
          connected: false,
          daemon_alive: false,
          last_post: null,
          active_window: null,
          has_screen_context: false,
          capture_mode: "on_switch",
          daemon_state: "error",
          last_error: "System Events got an error: Not authorised to send Apple events. (-1743)",
          last_error_kind: "automation_permission_denied",
          status_reason: "macOS Automation permission is blocking native desktop presence.",
          recovery_hint: "Grant Automation permission for the terminal running Seraph to control System Events in System Settings > Privacy & Security > Automation, then restart with ./manage.sh -e dev daemon start.",
          pending_notification_count: 0,
          last_native_notification_at: null,
          last_native_notification_title: null,
          last_native_notification_outcome: null,
        },
        notifications: [],
        queued_insights: [],
        queued_insight_count: 0,
        recent_interventions: [],
      }),
    );

    render(<DaemonStatus />);

    await waitFor(() => expect(screen.getByText("Automation permission needed")).toBeInTheDocument());
    expect(screen.getByText("Permission")).toBeInTheDocument();
    expect(screen.getByText(/System Events got an error/)).toBeInTheDocument();
    expect(screen.getByText(/Privacy & Security > Automation/)).toBeInTheDocument();
  });

  it("queues a test notification and refreshes the visible native status", async () => {
    fetchMock
      .mockResolvedValueOnce(
        mockResponse({
          daemon: {
            connected: true,
            last_post: 123,
            active_window: "Cursor — notes.md",
            has_screen_context: false,
            capture_mode: "on_switch",
            pending_notification_count: 0,
            last_native_notification_at: null,
            last_native_notification_title: null,
            last_native_notification_outcome: null,
          },
          notifications: [],
          queued_insights: [],
          queued_insight_count: 0,
          recent_interventions: [],
        }),
      )
      .mockResolvedValueOnce(
        mockResponse({
          id: "notif-1",
          title: "Seraph desktop shell",
          body: "Native presence is connected. This is a test notification.",
          intervention_type: "test",
          urgency: 1,
          created_at: "2026-03-18T10:00:00Z",
        }),
      )
      .mockResolvedValueOnce(
        mockResponse({
          daemon: {
            connected: true,
            last_post: 123,
            active_window: "Cursor — notes.md",
            has_screen_context: false,
            capture_mode: "on_switch",
            pending_notification_count: 1,
            last_native_notification_at: "2026-03-18T10:00:00Z",
            last_native_notification_title: "Seraph desktop shell",
            last_native_notification_outcome: "queued_test",
          },
          notifications: [
            {
              id: "notif-1",
              intervention_id: null,
              title: "Seraph desktop shell",
              body: "Native presence is connected. This is a test notification.",
              intervention_type: "test",
              urgency: 1,
              created_at: "2026-03-18T10:00:00Z",
            },
          ],
          queued_insights: [],
          queued_insight_count: 0,
          recent_interventions: [],
        }),
      );

    render(<DaemonStatus />);

    await waitFor(() => expect(screen.getByText("Desktop link live")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: "Send test notification" }));

    await waitFor(() => expect(screen.getByText("Queued")).toBeInTheDocument());
    expect(
      fetchMock.mock.calls.some(
        ([url, options]) =>
          typeof url === "string" &&
          url.includes("/api/observer/notifications/test") &&
          options?.method === "POST",
      ),
    ).toBe(true);
    expect(screen.getByText("1")).toBeInTheDocument();
  });

  it("dismisses queued native notifications from the browser controls", async () => {
    fetchMock
      .mockResolvedValueOnce(
        mockResponse({
          daemon: {
            connected: true,
            last_post: 123,
            active_window: "Arc — Guardian Cockpit",
            has_screen_context: true,
            capture_mode: "balanced",
            pending_notification_count: 2,
            last_native_notification_at: "2026-03-18T10:00:00Z",
            last_native_notification_title: "Seraph desktop shell",
            last_native_notification_outcome: "queued_test",
          },
          notifications: [
            {
              id: "notif-1",
              intervention_id: null,
              title: "Seraph desktop shell",
              body: "Native presence is connected. This is a test notification.",
              intervention_type: "test",
              urgency: 1,
              created_at: "2026-03-18T10:00:00Z",
            },
            {
              id: "notif-2",
              intervention_id: null,
              title: "Seraph alert",
              body: "Second desktop notification.",
              intervention_type: "alert",
              urgency: 5,
              created_at: "2026-03-18T10:01:00Z",
            },
          ],
          queued_insights: [],
          queued_insight_count: 0,
          recent_interventions: [],
        }),
      )
      .mockResolvedValueOnce(mockResponse({ dismissed: true }))
      .mockResolvedValueOnce(
        mockResponse({
          daemon: {
            connected: true,
            last_post: 123,
            active_window: "Arc — Guardian Cockpit",
            has_screen_context: true,
            capture_mode: "balanced",
            pending_notification_count: 1,
            last_native_notification_at: "2026-03-18T10:02:00Z",
            last_native_notification_title: "Seraph desktop shell",
            last_native_notification_outcome: "dismissed",
          },
          notifications: [
            {
              id: "notif-2",
              intervention_id: null,
              title: "Seraph alert",
              body: "Second desktop notification.",
              intervention_type: "alert",
              urgency: 5,
              created_at: "2026-03-18T10:01:00Z",
            },
          ],
          queued_insights: [],
          queued_insight_count: 0,
          recent_interventions: [],
        }),
      );

    render(<DaemonStatus />);

    await waitFor(() => expect(screen.getByText("Seraph alert")).toBeInTheDocument());
    await userEvent.click(screen.getAllByRole("button", { name: "Dismiss" })[0]);

    await waitFor(() => expect(screen.getByText("Desktop queue updated")).toBeInTheDocument());
    expect(
      fetchMock.mock.calls.some(
        ([url, options]) =>
          typeof url === "string" &&
          url.includes("/api/observer/notifications/notif-1/dismiss") &&
          options?.method === "POST",
      ),
    ).toBe(true);
    expect(screen.getByText("Desktop notification dismissed in browser")).toBeInTheDocument();
  });
});
