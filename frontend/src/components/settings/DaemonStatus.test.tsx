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
        connected: true,
        last_post: 123,
        active_window: "VS Code — main.py",
        has_screen_context: true,
        capture_mode: "balanced",
        pending_notification_count: 2,
        last_native_notification_at: "2026-03-18T10:00:00Z",
        last_native_notification_title: "Seraph desktop shell",
        last_native_notification_outcome: "queued_test",
      }),
    );
    fetchMock.mockResolvedValueOnce(
      mockResponse({
        pending_count: 1,
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
      }),
    );

    render(<DaemonStatus />);

    await waitFor(() => expect(screen.getByText("Desktop link live")).toBeInTheDocument());
    expect(screen.getByText("Balanced")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
    expect(screen.getByText("Queued test notification")).toBeInTheDocument();
    expect(screen.getAllByText("Seraph desktop shell")).toHaveLength(2);
    expect(screen.getByText("Native presence is connected. This is a test notification.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Dismiss" })).toBeInTheDocument();
  });

  it("queues a test notification and refreshes the visible native status", async () => {
    fetchMock
      .mockResolvedValueOnce(
        mockResponse({
          connected: true,
          last_post: 123,
          active_window: "Cursor — notes.md",
          has_screen_context: false,
          capture_mode: "on_switch",
          pending_notification_count: 0,
          last_native_notification_at: null,
          last_native_notification_title: null,
          last_native_notification_outcome: null,
        }),
      )
      .mockResolvedValueOnce(
        mockResponse({
          pending_count: 0,
          notifications: [],
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
          connected: true,
          last_post: 123,
          active_window: "Cursor — notes.md",
          has_screen_context: false,
          capture_mode: "on_switch",
          pending_notification_count: 1,
          last_native_notification_at: "2026-03-18T10:00:00Z",
          last_native_notification_title: "Seraph desktop shell",
          last_native_notification_outcome: "queued_test",
        }),
      );
    fetchMock.mockResolvedValueOnce(
      mockResponse({
        pending_count: 1,
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
          connected: true,
          last_post: 123,
          active_window: "Arc — Guardian Cockpit",
          has_screen_context: true,
          capture_mode: "balanced",
          pending_notification_count: 2,
          last_native_notification_at: "2026-03-18T10:00:00Z",
          last_native_notification_title: "Seraph desktop shell",
          last_native_notification_outcome: "queued_test",
        }),
      )
      .mockResolvedValueOnce(
        mockResponse({
          pending_count: 2,
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
        }),
      )
      .mockResolvedValueOnce(mockResponse({ dismissed: true }))
      .mockResolvedValueOnce(
        mockResponse({
          connected: true,
          last_post: 123,
          active_window: "Arc — Guardian Cockpit",
          has_screen_context: true,
          capture_mode: "balanced",
          pending_notification_count: 1,
          last_native_notification_at: "2026-03-18T10:02:00Z",
          last_native_notification_title: "Seraph desktop shell",
          last_native_notification_outcome: "dismissed",
        }),
      )
      .mockResolvedValueOnce(
        mockResponse({
          pending_count: 1,
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
