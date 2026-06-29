import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ArtifactStoragePanel } from "./ArtifactStoragePanel";

function mockResponse(data: unknown, ok = true) {
  return {
    ok,
    json: async () => data,
  };
}

function settingsFromScreenAnalysisFixture(screen: {
  enabled: boolean;
  provider: string;
  model: string;
  preserve_captures: boolean;
  archive_dir: string;
  framekeeper_screenshot_folder?: string;
  framekeeper_artifact_root?: string;
  capture_mode: string;
  cadence_seconds: number | null;
  daemon_connected: boolean;
  artifact_count: number;
  last_artifact_at: string | null;
}) {
  return {
    screen: {
      analysis_enabled: screen.enabled,
      provider: screen.provider,
      model: screen.model,
      capture_mode: screen.capture_mode,
      cadence_seconds: screen.cadence_seconds,
      daemon_connected: screen.daemon_connected,
      daemon_alive: screen.daemon_connected,
      artifact_count: screen.artifact_count,
      last_artifact_at: screen.last_artifact_at,
      budget: {
        min_seconds_between_captures: 0,
        max_daily_captures: 0,
        archive_retention_days: 365,
        archive_max_mb: 0,
      },
      preservation_enabled: screen.preserve_captures,
      archive_dir: screen.archive_dir,
      archive_dir_source: "screen-analysis-settings",
      exists: true,
      writable: true,
      creation_error: null,
      stored_artifacts: ["image", "provider_output", "analysis_json"],
      inspection_endpoint: "/api/observer/screen-artifacts",
      inspection_visibility: "localhost_only",
      daemon_status: {
        state: "running",
        screen_analysis: "active",
        capture_ready: true,
        last_error: null,
        last_error_kind: null,
        updated_at: "2026-06-20T18:34:25Z",
        status_source: "daemon-status-file",
      },
      control_env: {
        enabled: "SERAPH_PRESERVE_SCREEN_CAPTURES",
        archive_dir: "SERAPH_SCREEN_CAPTURE_ARCHIVE_DIR or SCREEN_CAPTURE_ARCHIVE_DIR",
      },
    },
    framekeeper: {
      enabled: true,
      provider: "framekeeper",
      screenshot_folder: screen.framekeeper_screenshot_folder ?? screen.framekeeper_artifact_root ?? "~/Library/Application Support/Framekeeper/artifacts",
      screenshot_folder_source: (screen.framekeeper_screenshot_folder ?? screen.framekeeper_artifact_root) ? "screen-analysis-settings" : "default",
      artifact_root: screen.framekeeper_screenshot_folder ?? screen.framekeeper_artifact_root ?? "~/Library/Application Support/Framekeeper/artifacts",
      artifact_root_source: (screen.framekeeper_screenshot_folder ?? screen.framekeeper_artifact_root) ? "screen-analysis-settings" : "default",
      image_count: 0,
      last_image_at: null,
      status: "empty",
      exists: false,
      readable: false,
      stored_artifacts: ["image"],
      auto_ingest_enabled: true,
      auto_ingest_interval_min: 5,
      auto_ingest_limit: 100,
      ingest_endpoint: "/api/observer/framekeeper/ingest",
      inspection_endpoint: "/api/observer/screen-artifacts",
      inspection_visibility: "localhost_only",
      control_env: {
        screenshot_folder: "SERAPH_FRAMEKEEPER_SCREENSHOT_FOLDER",
        artifact_root: "SERAPH_FRAMEKEEPER_ARTIFACT_ROOT",
        auto_ingest_enabled: "FRAMEKEEPER_INGEST_ENABLED",
        auto_ingest_interval: "FRAMEKEEPER_INGEST_INTERVAL_MIN",
        auto_ingest_limit: "FRAMEKEEPER_INGEST_LIMIT",
      },
    },
    reports: {
      enabled: false,
      hour: 21,
      analysis_provider: "deterministic-local",
      archive_dir: "/tmp/seraph-dev-data/artifacts/reports",
      archive_dir_source: "default",
      exists: true,
      writable: true,
      creation_error: null,
      stored_artifacts: ["report_text", "report_json"],
      receipt_count: 0,
      last_receipt_at: null,
      control_env: {
        archive_dir: "REPORT_ARCHIVE_DIR",
        enabled: "END_OF_DAY_REPORT_ENABLED",
        llm: "END_OF_DAY_REPORT_LLM_ENABLED",
      },
    },
    email: {
      enabled: false,
      preview_required: true,
      smtp_configured: false,
      recipient_configured: false,
      allowlist_configured: false,
      sender_configured: false,
      control_env: {
        enabled: "EMAIL_REPORTS_ENABLED",
        preview_required: "EMAIL_REPORTS_PREVIEW_REQUIRED",
        smtp_host: "SMTP_HOST",
        recipient: "EMAIL_REPORTS_TO",
        allowlist: "EMAIL_REPORTS_TO_ALLOWLIST",
      },
    },
  };
}

describe("ArtifactStoragePanel", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("renders screen, report, and email artifact configuration", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({
        screen: {
          analysis_enabled: true,
          provider: "codex-local",
          model: "gpt-5.5",
          capture_mode: "detailed",
          cadence_seconds: 60,
          daemon_connected: false,
          artifact_count: 1,
          last_artifact_at: "2026-06-20T18:34:25.918618",
          preservation_enabled: true,
          archive_dir: "/Users/test/Library/Application Support/Seraph/artifacts/screen-captures",
          archive_dir_source: "screen-analysis-settings",
          exists: true,
          writable: true,
          creation_error: null,
          stored_artifacts: ["image", "provider_output", "analysis_json"],
          inspection_endpoint: "/api/observer/screen-artifacts",
          inspection_visibility: "localhost_only",
          daemon_status: {
            state: "running",
            screen_analysis: "capture_error",
            capture_ready: false,
            last_error: "Grant Screen Recording permission to the terminal/app running Seraph.",
            last_error_kind: "screen_capture_permission",
            updated_at: "2026-06-20T18:34:25Z",
            status_source: "daemon-status-file",
          },
          control_env: {
            enabled: "SERAPH_PRESERVE_SCREEN_CAPTURES",
            archive_dir: "SERAPH_SCREEN_CAPTURE_ARCHIVE_DIR or SCREEN_CAPTURE_ARCHIVE_DIR",
          },
        },
        framekeeper: {
          enabled: true,
          provider: "framekeeper",
          artifact_root: "/Users/test/Library/Application Support/Framekeeper/artifacts",
          artifact_root_source: "default",
          image_count: 2,
          last_image_at: "2026-06-20T18:40:00Z",
          status: "ready",
          exists: true,
          readable: true,
          stored_artifacts: ["image"],
          auto_ingest_enabled: true,
          auto_ingest_interval_min: 5,
          auto_ingest_limit: 100,
          ingest_endpoint: "/api/observer/framekeeper/ingest",
          inspection_endpoint: "/api/observer/screen-artifacts",
          inspection_visibility: "localhost_only",
          control_env: {
            artifact_root: "SERAPH_FRAMEKEEPER_ARTIFACT_ROOT",
            auto_ingest_enabled: "FRAMEKEEPER_INGEST_ENABLED",
            auto_ingest_interval: "FRAMEKEEPER_INGEST_INTERVAL_MIN",
            auto_ingest_limit: "FRAMEKEEPER_INGEST_LIMIT",
          },
        },
        reports: {
          enabled: true,
          hour: 21,
          analysis_provider: "deterministic-local",
          archive_dir: "/tmp/seraph-dev-data/artifacts/reports",
          archive_dir_source: "default",
          exists: true,
          writable: true,
          creation_error: null,
          stored_artifacts: ["report_text", "report_json"],
          control_env: {
            archive_dir: "REPORT_ARCHIVE_DIR",
            enabled: "END_OF_DAY_REPORT_ENABLED",
            llm: "END_OF_DAY_REPORT_LLM_ENABLED",
          },
        },
        email: {
          enabled: false,
          preview_required: true,
          smtp_configured: false,
          recipient_configured: false,
          allowlist_configured: false,
          control_env: {
            enabled: "EMAIL_REPORTS_ENABLED",
            preview_required: "EMAIL_REPORTS_PREVIEW_REQUIRED",
            smtp_host: "SMTP_HOST",
            recipient: "EMAIL_REPORTS_TO",
            allowlist: "EMAIL_REPORTS_TO_ALLOWLIST",
          },
        },
      }),
    );

    render(<ArtifactStoragePanel />);

    await waitFor(() => expect(screen.getByText("Seraph analysis")).toBeInTheDocument());
    expect(screen.getByText("Framekeeper Folder")).toBeInTheDocument();
    expect(screen.getByText("scans a local screenshot folder; reports stay in Seraph")).toBeInTheDocument();
    expect(screen.getByText("Framekeeper screenshots")).toBeInTheDocument();
    expect(screen.getByText(/2 images/)).toBeInTheDocument();
    expect(screen.getByText("every 5m · up to 100 images")).toBeInTheDocument();
    expect(screen.getByText("/api/observer/framekeeper/ingest")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Scan folder" })).toBeInTheDocument();
    expect(screen.getByDisplayValue("codex-local")).toBeInTheDocument();
    expect(screen.getByDisplayValue("detailed / 60s")).toBeInTheDocument();
    expect(screen.getByText("offline - no new captures")).toBeInTheDocument();
    expect(screen.getByText("Grant Screen Recording permission to the terminal/app running Seraph.")).toBeInTheDocument();
    expect(screen.getByText(/1 captures/)).toBeInTheDocument();
    expect(screen.getAllByText("/api/observer/screen-artifacts (localhost only)")).toHaveLength(2);
    expect(screen.getByText("SERAPH_PRESERVE_SCREEN_CAPTURES")).toBeInTheDocument();
    expect(screen.getAllByText("ready")).toHaveLength(3);
    expect(screen.getByText("End-of-day reports")).toBeInTheDocument();
    expect(screen.getByText("deterministic-local")).toBeInTheDocument();
    expect(screen.getByText("Email delivery")).toBeInTheDocument();
    expect(screen.getByText("SMTP")).toBeInTheDocument();
    expect(screen.getAllByText("Missing")).toHaveLength(3);
  });

  it("updates capture mode from settings", async () => {
    const artifactStorage = {
      screen: {
        analysis_enabled: true,
        provider: "codex-local",
        model: "gpt-5.5",
        capture_mode: "on_switch",
        cadence_seconds: null,
        daemon_connected: true,
        artifact_count: 0,
        last_artifact_at: null,
        preservation_enabled: true,
        archive_dir: "/tmp/seraph-dev-data/artifacts/screen-captures",
        archive_dir_source: "screen-analysis-settings",
        exists: true,
        writable: true,
        creation_error: null,
        stored_artifacts: ["image", "provider_output", "analysis_json"],
        inspection_endpoint: "/api/observer/screen-artifacts",
        inspection_visibility: "localhost_only",
        daemon_status: {
          state: "running",
          screen_analysis: "active",
          capture_ready: true,
          last_error: null,
          last_error_kind: null,
          updated_at: "2026-06-20T18:34:25Z",
          status_source: "daemon-status-file",
        },
        control_env: {
          enabled: "SERAPH_PRESERVE_SCREEN_CAPTURES",
          archive_dir: "SERAPH_SCREEN_CAPTURE_ARCHIVE_DIR or SCREEN_CAPTURE_ARCHIVE_DIR",
        },
      },
      reports: {
        enabled: false,
        hour: 21,
        analysis_provider: "deterministic-local",
        archive_dir: "/tmp/seraph-dev-data/artifacts/reports",
        archive_dir_source: "default",
        exists: true,
        writable: true,
        creation_error: null,
        stored_artifacts: ["report_text", "report_json"],
        control_env: {
          archive_dir: "REPORT_ARCHIVE_DIR",
          enabled: "END_OF_DAY_REPORT_ENABLED",
          llm: "END_OF_DAY_REPORT_LLM_ENABLED",
        },
      },
      email: {
        enabled: false,
        preview_required: true,
        smtp_configured: false,
        recipient_configured: false,
        allowlist_configured: false,
        control_env: {
          enabled: "EMAIL_REPORTS_ENABLED",
          preview_required: "EMAIL_REPORTS_PREVIEW_REQUIRED",
          smtp_host: "SMTP_HOST",
          recipient: "EMAIL_REPORTS_TO",
          allowlist: "EMAIL_REPORTS_TO_ALLOWLIST",
        },
      },
    };
    fetchMock
      .mockResolvedValueOnce(mockResponse(artifactStorage))
      .mockResolvedValueOnce(mockResponse({ mode: "detailed" }))
      .mockResolvedValueOnce(
        mockResponse({
          ...artifactStorage,
          screen: { ...artifactStorage.screen, capture_mode: "detailed", cadence_seconds: 60 },
        }),
      );

    render(<ArtifactStoragePanel />);

    const modeSelect = await screen.findByDisplayValue("on_switch");
    fireEvent.change(modeSelect, { target: { value: "detailed" } });

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/settings/capture-mode"),
        expect.objectContaining({
          method: "PUT",
          body: JSON.stringify({ mode: "detailed" }),
        }),
      ),
    );
    await waitFor(() => expect(screen.getByDisplayValue("detailed / 60s")).toBeInTheDocument());
  });

  it("runs manual report preview and shows safe receipt metadata", async () => {
    const artifactStorage = settingsFromScreenAnalysisFixture({
      enabled: true,
      provider: "codex-local",
      model: "gpt-5.5",
      preserve_captures: true,
      archive_dir: "/tmp/seraph-dev-data/artifacts/screen-captures",
      capture_mode: "on_switch",
      cadence_seconds: null,
      daemon_connected: true,
      artifact_count: 1,
      last_artifact_at: null,
    });
    artifactStorage.email.enabled = true;
    artifactStorage.email.smtp_configured = true;
    artifactStorage.email.recipient_configured = true;
    artifactStorage.email.allowlist_configured = true;
    artifactStorage.email.sender_configured = true;
    fetchMock
      .mockResolvedValueOnce(mockResponse(artifactStorage))
      .mockResolvedValueOnce(
        mockResponse({
          status: "ok",
          action: "manual-preview",
          report: {
            date: "2026-06-20",
            analysis_provider: "deterministic-local",
          },
          email: {
            status: "preview_only",
            reason: "manual_preview",
            recipient_hash: null,
          },
          receipt: {
            receipt_sha256: "abcdef1234567890",
            status: "succeeded",
          },
        }),
      )
      .mockResolvedValueOnce(mockResponse(artifactStorage));

    render(<ArtifactStoragePanel />);

    const sendButton = await screen.findByRole("button", { name: "Send" });
    expect(sendButton).toBeDisabled();
    fireEvent.click(await screen.findByRole("button", { name: "Preview" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/settings/end-of-day-report/manual"),
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ send_email: false, preview_acknowledged: false }),
        }),
      ),
    );
    expect(await screen.findByText(/manual-preview/)).toBeInTheDocument();
    expect(screen.getByText(/receipt abcdef123456/)).toBeInTheDocument();
    expect(screen.queryByText(/user@example/)).not.toBeInTheDocument();
    await waitFor(() => expect(sendButton).not.toBeDisabled());
  });

  it("runs a local Framekeeper folder scan from the settings panel", async () => {
    const artifactStorage = {
      ...settingsFromScreenAnalysisFixture({
        enabled: true,
        provider: "codex-local",
        model: "gpt-5.5",
        preserve_captures: true,
        archive_dir: "/tmp/seraph-dev-data/artifacts/screen-captures",
        capture_mode: "on_switch",
        cadence_seconds: null,
        daemon_connected: true,
        artifact_count: 0,
        last_artifact_at: null,
      }),
      framekeeper: {
        enabled: true,
        provider: "framekeeper",
        screenshot_folder: "/Users/test/Library/Application Support/Framekeeper/artifacts",
        screenshot_folder_source: "SERAPH_FRAMEKEEPER_SCREENSHOT_FOLDER",
        artifact_root: "/Users/test/Library/Application Support/Framekeeper/artifacts",
        artifact_root_source: "SERAPH_FRAMEKEEPER_SCREENSHOT_FOLDER",
        image_count: 3,
        last_image_at: "2026-06-20T18:40:00Z",
        status: "ready",
        exists: true,
        readable: true,
        stored_artifacts: ["image"],
        auto_ingest_enabled: true,
        auto_ingest_interval_min: 5,
        auto_ingest_limit: 100,
        ingest_endpoint: "/api/observer/framekeeper/ingest",
        inspection_endpoint: "/api/observer/screen-artifacts",
        inspection_visibility: "localhost_only",
        control_env: {
          screenshot_folder: "SERAPH_FRAMEKEEPER_SCREENSHOT_FOLDER",
          artifact_root: "SERAPH_FRAMEKEEPER_ARTIFACT_ROOT",
          auto_ingest_enabled: "FRAMEKEEPER_INGEST_ENABLED",
          auto_ingest_interval: "FRAMEKEEPER_INGEST_INTERVAL_MIN",
          auto_ingest_limit: "FRAMEKEEPER_INGEST_LIMIT",
        },
      },
    };
    const refreshedStorage = {
      ...artifactStorage,
      framekeeper: {
        ...artifactStorage.framekeeper,
        image_count: 4,
      },
    };
    fetchMock
      .mockResolvedValueOnce(mockResponse(artifactStorage))
      .mockResolvedValueOnce(
        mockResponse({
          artifact_root: artifactStorage.framekeeper.artifact_root,
          scanned: 3,
          ingested: 1,
          skipped_duplicates: 2,
          rejected: [],
        }),
      )
      .mockResolvedValueOnce(mockResponse(refreshedStorage));

    render(<ArtifactStoragePanel />);

    fireEvent.click(await screen.findByRole("button", { name: "Scan folder" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/observer/framekeeper/ingest"),
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({
            screenshot_folder: artifactStorage.framekeeper.screenshot_folder,
            limit: 100,
          }),
        }),
      ),
    );
    expect(await screen.findByText(/scanned 3 · added 1/)).toBeInTheDocument();
    expect(screen.getByText(/duplicates 2/)).toBeInTheDocument();
  });

  it("saves a configured Framekeeper screenshot folder", async () => {
    const artifactStorage = settingsFromScreenAnalysisFixture({
      enabled: true,
      provider: "codex-local",
      model: "gpt-5.5",
      preserve_captures: true,
      archive_dir: "/tmp/seraph-dev-data/artifacts/screen-captures",
      capture_mode: "on_switch",
      cadence_seconds: null,
      daemon_connected: true,
      artifact_count: 0,
      last_artifact_at: null,
    });
    const nextRoot = "/Users/test/Framekeeper Screens";
    const refreshedStorage = {
      ...artifactStorage,
      framekeeper: {
        ...artifactStorage.framekeeper,
        screenshot_folder: nextRoot,
        screenshot_folder_source: "screen-analysis-settings",
        artifact_root: nextRoot,
        artifact_root_source: "screen-analysis-settings",
      },
    };
    fetchMock
      .mockResolvedValueOnce(mockResponse(artifactStorage))
      .mockResolvedValueOnce(mockResponse({ ok: true }))
      .mockResolvedValueOnce(mockResponse(refreshedStorage));

    render(<ArtifactStoragePanel />);

    const folderInput = await screen.findByLabelText("Framekeeper screenshot folder");
    fireEvent.change(folderInput, { target: { value: nextRoot } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/settings/screen-analysis"),
        expect.objectContaining({
          method: "PUT",
          body: JSON.stringify({ framekeeper_screenshot_folder: nextRoot }),
        }),
      ),
    );
    expect(await screen.findByDisplayValue(nextRoot)).toBeInTheDocument();
    expect(screen.getAllByText("screen-analysis-settings").length).toBeGreaterThanOrEqual(1);
  });

  it("does not refresh settings after a save resolves on an unmounted panel", async () => {
    const artifactStorage = {
      screen: {
        analysis_enabled: true,
        provider: "codex-local",
        model: "gpt-5.5",
        capture_mode: "on_switch",
        cadence_seconds: null,
        daemon_connected: true,
        artifact_count: 0,
        last_artifact_at: null,
        preservation_enabled: true,
        archive_dir: "/tmp/seraph-dev-data/artifacts/screen-captures",
        archive_dir_source: "screen-analysis-settings",
        exists: true,
        writable: true,
        creation_error: null,
        stored_artifacts: ["image", "provider_output", "analysis_json"],
        inspection_endpoint: "/api/observer/screen-artifacts",
        inspection_visibility: "localhost_only",
        daemon_status: {
          state: "running",
          screen_analysis: "active",
          capture_ready: true,
          last_error: null,
          last_error_kind: null,
          updated_at: "2026-06-20T18:34:25Z",
          status_source: "daemon-status-file",
        },
        control_env: {
          enabled: "SERAPH_PRESERVE_SCREEN_CAPTURES",
          archive_dir: "SERAPH_SCREEN_CAPTURE_ARCHIVE_DIR or SCREEN_CAPTURE_ARCHIVE_DIR",
        },
      },
      reports: {
        enabled: false,
        hour: 21,
        analysis_provider: "deterministic-local",
        archive_dir: "/tmp/seraph-dev-data/artifacts/reports",
        archive_dir_source: "default",
        exists: true,
        writable: true,
        creation_error: null,
        stored_artifacts: ["report_text", "report_json"],
        control_env: {
          archive_dir: "REPORT_ARCHIVE_DIR",
          enabled: "END_OF_DAY_REPORT_ENABLED",
          llm: "END_OF_DAY_REPORT_LLM_ENABLED",
        },
      },
      email: {
        enabled: false,
        preview_required: true,
        smtp_configured: false,
        recipient_configured: false,
        allowlist_configured: false,
        control_env: {
          enabled: "EMAIL_REPORTS_ENABLED",
          preview_required: "EMAIL_REPORTS_PREVIEW_REQUIRED",
          smtp_host: "SMTP_HOST",
          recipient: "EMAIL_REPORTS_TO",
          allowlist: "EMAIL_REPORTS_TO_ALLOWLIST",
        },
      },
    };
    let resolveSave: (value: ReturnType<typeof mockResponse>) => void = () => {};
    fetchMock
      .mockResolvedValueOnce(mockResponse(artifactStorage))
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveSave = resolve;
          }),
      );

    const { unmount } = render(<ArtifactStoragePanel />);

    const modeSelect = await screen.findByDisplayValue("on_switch");
    fetchMock.mockClear();
    fireEvent.change(modeSelect, { target: { value: "detailed" } });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    unmount();
    resolveSave(mockResponse({ mode: "detailed" }));
    await Promise.resolve();

    expect(fetchMock.mock.calls.filter(([url]) => String(url).includes("/api/settings/capture-mode"))).toHaveLength(1);
  });

  it("explains on_switch mode even when the daemon is capture-ready", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({
        screen: {
          analysis_enabled: true,
          provider: "codex-local",
          model: "gpt-5.5",
          capture_mode: "on_switch",
          cadence_seconds: null,
          daemon_connected: true,
          artifact_count: 2,
          last_artifact_at: "2026-06-21T05:43:55",
          preservation_enabled: true,
          archive_dir: "/tmp/seraph-dev-data/artifacts/screen-captures",
          archive_dir_source: "screen-analysis-settings",
          exists: true,
          writable: true,
          creation_error: null,
          stored_artifacts: ["image", "provider_output", "analysis_json"],
          inspection_endpoint: "/api/observer/screen-artifacts",
          inspection_visibility: "localhost_only",
          daemon_status: {
            state: "running",
            screen_analysis: "active",
            capture_ready: true,
            last_error: null,
            last_error_kind: null,
            updated_at: "2026-06-21T06:02:58Z",
            status_source: "daemon-status-file",
          },
          control_env: {
            enabled: "SERAPH_PRESERVE_SCREEN_CAPTURES",
            archive_dir: "SERAPH_SCREEN_CAPTURE_ARCHIVE_DIR or SCREEN_CAPTURE_ARCHIVE_DIR",
          },
        },
        reports: {
          enabled: false,
          hour: 21,
          analysis_provider: "deterministic-local",
          archive_dir: "/tmp/seraph-dev-data/artifacts/reports",
          archive_dir_source: "default",
          exists: true,
          writable: true,
          creation_error: null,
          stored_artifacts: ["report_text", "report_json"],
          control_env: {
            archive_dir: "REPORT_ARCHIVE_DIR",
            enabled: "END_OF_DAY_REPORT_ENABLED",
            llm: "END_OF_DAY_REPORT_LLM_ENABLED",
          },
        },
        email: {
          enabled: false,
          preview_required: true,
          smtp_configured: false,
          recipient_configured: false,
          allowlist_configured: false,
          control_env: {
            enabled: "EMAIL_REPORTS_ENABLED",
            preview_required: "EMAIL_REPORTS_PREVIEW_REQUIRED",
            smtp_host: "SMTP_HOST",
            recipient: "EMAIL_REPORTS_TO",
            allowlist: "EMAIL_REPORTS_TO_ALLOWLIST",
          },
        },
      }),
    );

    render(<ArtifactStoragePanel />);

    const captureState = await screen.findByText("waiting for app/window switch");
    expect(captureState).toBeInTheDocument();
    expect(captureState).toHaveAttribute("title", "waiting for app/window switch");
  });

  it("keeps analysis controls visible when artifact metadata is unavailable", async () => {
    fetchMock
      .mockResolvedValueOnce(
        mockResponse({
          enabled: true,
          provider: "codex-local",
          model: "gpt-5.5",
          preserve_captures: true,
          archive_dir: "/tmp/seraph-dev-data/artifacts/screen-captures",
          capture_mode: "on_switch",
          cadence_seconds: null,
          daemon_connected: true,
          artifact_count: 7,
          last_artifact_at: "2026-06-21T08:42:52Z",
        }),
      )
      .mockRejectedValueOnce(new Error("artifact endpoint timed out"));

    render(<ArtifactStoragePanel />);

    expect(await screen.findByText("Seraph analysis", undefined, { timeout: 1_000 })).toBeInTheDocument();
    expect(screen.getByDisplayValue("codex-local")).toBeInTheDocument();
    expect(screen.getByDisplayValue("on_switch")).toBeInTheDocument();
    expect(screen.getByText("Archive metadata degraded; analysis controls are still live.")).toBeInTheDocument();
    expect(screen.queryByText("Artifact storage settings unavailable.")).not.toBeInTheDocument();
    expect(screen.queryByText("Framekeeper folder settings unavailable.")).not.toBeInTheDocument();
  });

  it("shows degraded metadata warning when artifact metadata has an invalid shape", async () => {
    fetchMock
      .mockResolvedValueOnce(
        mockResponse({
          enabled: true,
          provider: "codex-local",
          model: "gpt-5.5",
          preserve_captures: true,
          archive_dir: "/tmp/seraph-dev-data/artifacts/screen-captures",
          capture_mode: "on_switch",
          cadence_seconds: null,
          daemon_connected: true,
          artifact_count: 0,
          last_artifact_at: null,
        }),
      )
      .mockResolvedValueOnce(mockResponse({ screen: { archive_dir: "/tmp/broken" } }));

    render(<ArtifactStoragePanel />);

    expect(await screen.findByText("Seraph analysis", undefined, { timeout: 1_000 })).toBeInTheDocument();
    expect(
      await screen.findByText("Archive metadata degraded; analysis controls are still live."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Archive metadata loading; analysis controls are live.")).not.toBeInTheDocument();
    expect(screen.queryByText("Framekeeper folder settings unavailable.")).not.toBeInTheDocument();
  });

  it("ignores stale artifact metadata after a newer save refresh", async () => {
    let resolveInitialArtifact: (value: ReturnType<typeof mockResponse>) => void = () => {};
    const screenInitial = {
      enabled: true,
      provider: "codex-local",
      model: "gpt-5.5",
      preserve_captures: true,
      archive_dir: "/tmp/seraph-dev-data/artifacts/screen-captures",
      capture_mode: "on_switch",
      cadence_seconds: null,
      daemon_connected: true,
      artifact_count: 0,
      last_artifact_at: null,
    };
    const screenDetailed = {
      ...screenInitial,
      capture_mode: "detailed",
      cadence_seconds: 60,
    };
    const staleArtifactStorage = settingsFromScreenAnalysisFixture(screenInitial);
    const detailedArtifactStorage = settingsFromScreenAnalysisFixture(screenDetailed);

    fetchMock
      .mockResolvedValueOnce(mockResponse(screenInitial))
      .mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            resolveInitialArtifact = resolve;
          }),
      )
      .mockResolvedValueOnce(mockResponse({ mode: "detailed" }))
      .mockResolvedValueOnce(mockResponse(screenDetailed))
      .mockResolvedValueOnce(mockResponse(detailedArtifactStorage));

    render(<ArtifactStoragePanel />);

    const modeSelect = await screen.findByDisplayValue("on_switch");
    fireEvent.change(modeSelect, { target: { value: "detailed" } });

    await waitFor(() => expect(screen.getByDisplayValue("detailed / 60s")).toBeInTheDocument());
    resolveInitialArtifact(mockResponse(staleArtifactStorage));
    await Promise.resolve();

    expect(screen.getByDisplayValue("detailed / 60s")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("on_switch")).not.toBeInTheDocument();
  });

  it("aborts hung artifact metadata and keeps analysis controls visible", async () => {
    let artifactSignal: AbortSignal | undefined;
    fetchMock
      .mockResolvedValueOnce(
        mockResponse({
          enabled: true,
          provider: "codex-local",
          model: "gpt-5.5",
          preserve_captures: true,
          archive_dir: "/tmp/seraph-dev-data/artifacts/screen-captures",
          capture_mode: "on_switch",
          cadence_seconds: null,
          daemon_connected: true,
          artifact_count: 0,
          last_artifact_at: null,
        }),
      )
      .mockImplementationOnce((_url: string, init?: RequestInit) => {
        artifactSignal = init?.signal ?? undefined;
        return new Promise((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
        });
      });

    render(<ArtifactStoragePanel />);

    expect(await screen.findByText("Seraph analysis", undefined, { timeout: 5_000 })).toBeInTheDocument();
    expect(screen.getByDisplayValue("codex-local")).toBeInTheDocument();
    expect(
      await screen.findByText(
        "Archive metadata degraded; analysis controls are still live.",
        undefined,
        { timeout: 5_000 },
      ),
    ).toBeInTheDocument();
    expect(artifactSignal?.aborted).toBe(true);
    expect(screen.queryByText("Artifact storage settings unavailable.")).not.toBeInTheDocument();
    expect(screen.queryByText("Framekeeper folder settings unavailable.")).not.toBeInTheDocument();
  }, 7_000);
});
