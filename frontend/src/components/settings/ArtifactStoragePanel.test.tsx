import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ArtifactStoragePanel } from "./ArtifactStoragePanel";
import { isGreenModelFabricCanary } from "../../lib/modelFabric";

function mockResponse(data: unknown, ok = true) {
  return {
    ok,
    json: async () => data,
  };
}

it("does not treat persistence-degraded canaries as green", () => {
  expect(isGreenModelFabricCanary({
    profile_id: "local-text",
    capability: "text",
    outcome: "passed",
    error_code: "proof_persistence_failed",
    proof: null,
    receipt_persistence: "persisted",
    proof_persistence: "degraded",
  })).toBe(false);
});

function settingsFromScreenAnalysisFixture(screen: {
  enabled: boolean;
  provider: string;
  model: string;
  preserve_captures: boolean;
  archive_dir: string;
  screenshot_folder?: string;
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
    screenshot_folder: {
      enabled: true,
      provider: "screenshot_folder",
      path: screen.screenshot_folder ?? null,
      path_source: screen.screenshot_folder ? "screen-analysis-settings" : "not_loaded",
      image_count: 0,
      last_image_at: null,
      status: "empty",
      exists: false,
      readable: false,
      stored_artifacts: ["image"],
      analysis: {
        provider: "metadata unavailable",
        model: "",
        base_url_configured: false,
        observation_count: 0,
        analysis_status: {},
        analysis_backlog: 0,
        analysis_failures: 0,
        latest_observation_at: null,
        latest_analyzed_at: null,
        latest_failure: null,
        digest_count: 0,
        latest_digest_at: null,
        persistence: {
          db_lock_retries: 0,
          db_lock_failures: 0,
          selection_db_lock_retries: 0,
          selection_db_lock_failures: 0,
          persistence_db_lock_retries: 0,
          persistence_db_lock_failures: 0,
        },
      },
      auto_ingest_enabled: true,
      auto_ingest_interval_min: 5,
      auto_ingest_limit: 100,
      scan_endpoint: "/api/observer/screenshot-folder/scan",
      inspection_endpoint: "/api/observer/screen-artifacts",
      inspection_visibility: "localhost_only",
      control_env: {
        path: "SERAPH_SCREENSHOT_FOLDER",
        auto_ingest_enabled: "SCREENSHOT_FOLDER_INGEST_ENABLED",
        auto_ingest_interval: "SCREENSHOT_FOLDER_INGEST_INTERVAL_MIN",
        auto_ingest_limit: "SCREENSHOT_FOLDER_INGEST_LIMIT",
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
    window.localStorage.clear();
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    window.localStorage.clear();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("shows configured, attempted, actual text and VLM routes without probing on status reads", async () => {
    const artifactStorage = settingsFromScreenAnalysisFixture({
      enabled: true,
      provider: "local-vlm",
      model: "gemma-vlm",
      preserve_captures: true,
      archive_dir: "/tmp/seraph/artifacts",
      screenshot_folder: "/tmp/screenshots",
      capture_mode: "on_switch",
      cadence_seconds: null,
      daemon_connected: true,
      artifact_count: 2,
      last_artifact_at: null,
    });
    const profile = {
      id: "local-text",
      provider_kind: "openai-compatible",
      model: "gemma-text",
      api_base: "http://192.168.1.26:8000/v1",
      enabled: true,
      secret_configured: true,
      missing_secret: false,
      capabilities: ["text", "streaming"],
      transport_adapter: "litellm",
      model_fabric_eligible: true,
      model_fabric_exclusion_reason: null,
      routable: true,
      non_routable_reasons: [],
    };
    const vlmProfile = { ...profile, id: "local-vlm", model: "gemma-vlm", transport_adapter: "vlm_analyze_file" };
    const settingsPayload = {
      schema_version: "seraph.model-fabric.settings.v1",
      status: "ready",
      error_code: null,
      updated_at: "2026-07-10T12:00:00Z",
      profiles: [profile, vlmProfile],
      persisted_profile_ids: ["local-text", "local-vlm"],
      workload_policies: [{
        runtime_path: "interactive",
        egress_class: "local_only",
        cloud_egress_acknowledged: false,
        allowed_profile_ids: ["local-text"],
        allowed_provider_kinds: [],
        fallback_allowed: false,
      }],
      defaults: { egress_class: "local_only", fallback_allowed: false },
      canary_endpoint: "/api/settings/model-fabric/canary",
    };
    const workload = (profileId: string, model: string) => ({
      selected: { profile_id: profileId, model, adapter: "litellm", destination_class: "private", outcome: "selected", latency_ms: 0 },
      attempted: { profile_id: profileId, model, adapter: "litellm", destination_class: "private", outcome: "succeeded", latency_ms: 18 },
      attempt_count: 1,
      last_outcome: "succeeded",
      succeeded: { profile_id: profileId, model, adapter: "litellm", receipt_id: `receipt-${profileId}`, finished_at: "2026-07-10T12:01:00Z" },
      persistence: "persisted",
    });
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/settings/artifact-storage")) return Promise.resolve(mockResponse(artifactStorage));
      if (url.includes("/api/settings/model-fabric")) return Promise.resolve(mockResponse(settingsPayload));
      if (url.includes("/api/runtime/status")) return Promise.resolve(mockResponse({
        provider: "local",
        model: "gemma-text",
        model_fabric: {
          status: "ready",
          configuration_status: "ready",
          configuration_error: null,
          configured_chat_profile: "local-text",
          profiles: [profile, vlmProfile],
          proofs: [
            { profile_id: "local-text", capability: "text", status: "fresh", outcome: "passed", checked_at: "2026-07-10T12:00:00Z", expires_at: "2026-07-10T13:00:00Z" },
            { profile_id: "local-text", capability: "streaming", status: "stale", outcome: "passed", checked_at: "2026-07-09T12:00:00Z", expires_at: "2026-07-09T13:00:00Z" },
            { profile_id: "local-vlm", capability: "vision", status: "missing", outcome: null, checked_at: null, expires_at: null },
          ],
          topology: { text: ["interactive", "background", "report"], vlm: ["vision"] },
          workloads: { interactive: workload("local-text", "gemma-text"), vision: workload("local-vlm", "gemma-vlm") },
        },
      }));
      return Promise.reject(new Error(`unexpected ${url}`));
    });

    render(<ArtifactStoragePanel />);

    expect(await screen.findByText("Model fabric")).toBeInTheDocument();
    expect(screen.getByText("text: interactive, background, report · VLM: vision")).toBeInTheDocument();
    expect(screen.getByText(/local-text\/gemma-text:routable/)).toBeInTheDocument();
    const textRoute = screen.getByText(/selected local-text · attempted local-text:succeeded · actual local-text\/gemma-text/);
    expect(textRoute).toHaveClass("text-green-400");
    expect(screen.getByText(/selected local-vlm · attempted local-vlm:succeeded · actual local-vlm\/gemma-vlm/)).toHaveClass("text-green-400");
    expect(screen.getByText("1 fresh · 1 stale · 1 missing")).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes("/canary"))).toBe(false);
  });

  it("runs one manual bounded canary only after the operator clicks", async () => {
    const artifactStorage = settingsFromScreenAnalysisFixture({
      enabled: true,
      provider: "local-vlm",
      model: "gemma",
      preserve_captures: true,
      archive_dir: "/tmp/seraph/artifacts",
      capture_mode: "on_switch",
      cadence_seconds: null,
      daemon_connected: true,
      artifact_count: 0,
      last_artifact_at: null,
    });
    const profile = {
      id: "local-text", provider_kind: "openai-compatible", model: "gemma", api_base: "http://127.0.0.1:8000/v1",
      enabled: true, secret_configured: true, missing_secret: false, capabilities: ["text"], transport_adapter: "litellm",
      model_fabric_eligible: true, model_fabric_exclusion_reason: null, routable: true, non_routable_reasons: [],
      canary_timeout_seconds: 120,
    };
    const settingsPayload = {
      schema_version: "seraph.model-fabric.settings.v1", status: "ready", error_code: null, updated_at: null,
      profiles: [profile], persisted_profile_ids: ["local-text"], workload_policies: [],
      defaults: { egress_class: "local_only", fallback_allowed: false }, canary_endpoint: "/api/settings/model-fabric/canary",
    };
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/settings/artifact-storage")) return Promise.resolve(mockResponse(artifactStorage));
      if (url.includes("/api/settings/model-fabric/canary") && init?.method === "POST") {
        return Promise.resolve(mockResponse({
          profile_id: "local-text", capability: "text", outcome: "passed", error_code: null,
          proof: { proof_hash: "abcdef1234567890", profile_id: "local-text", model: "gemma", adapter: "litellm", capability: "text", outcome: "passed", checked_at: "2026-07-10T12:00:00Z", expires_at: "2026-07-10T13:00:00Z", proven_value: "verified" },
          receipt_persistence: "persisted", proof_persistence: "persisted",
        }));
      }
      if (url.includes("/api/settings/model-fabric")) return Promise.resolve(mockResponse(settingsPayload));
      if (url.includes("/api/runtime/status")) return Promise.resolve(mockResponse({ provider: "local", model: "gemma", model_fabric: { status: "ready", configuration_status: "ready", configuration_error: null, configured_chat_profile: "local-text", profiles: [profile], topology: { text: ["interactive"], vlm: ["vision"] }, workloads: {} } }));
      return Promise.reject(new Error(`unexpected ${url}`));
    });

    render(<ArtifactStoragePanel />);
    const runButton = await screen.findByRole("button", { name: "Run canary" });
    expect(screen.getByRole("option", { name: "health" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "latency ms" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "context tokens" })).not.toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "output tokens" })).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes("/canary"))).toBe(false);
    fireEvent.click(runButton);

    expect(await screen.findByText(/local-text\/text · passed · proof abcdef123456/)).toHaveClass("text-green-400");
    const canaryCalls = fetchMock.mock.calls.filter(([input]) => String(input).includes("/canary"));
    expect(canaryCalls).toHaveLength(1);
    expect(JSON.parse(String(canaryCalls[0][1]?.body))).toEqual({
      profile_id: "local-text", capability: "text", timeout_seconds: 120, proof_ttl_seconds: 3600,
    });
  });

  it("keeps retained model-fabric controls visible but disables canaries while metadata is stale", async () => {
    window.localStorage.setItem("seraph.settings.modelFabric.v1", JSON.stringify({
      schema_version: "seraph.model-fabric.settings.v1",
      status: "ready",
      error_code: null,
      updated_at: "2026-07-10T12:00:00Z",
      profiles: [{
        id: "retained-local", provider_kind: "openai-compatible", model: "retained-gemma", api_base: "http://127.0.0.1:8000/v1",
        enabled: true, secret_configured: true, missing_secret: false, capabilities: ["text"], transport_adapter: "litellm",
        model_fabric_eligible: true, model_fabric_exclusion_reason: null, routable: true, non_routable_reasons: [],
      }],
      persisted_profile_ids: ["retained-local"],
      workload_policies: [],
      defaults: { egress_class: "local_only", fallback_allowed: false },
      canary_endpoint: "/api/settings/model-fabric/canary",
    }));
    const artifactStorage = settingsFromScreenAnalysisFixture({
      enabled: true, provider: "local-vlm", model: "gemma", preserve_captures: true,
      archive_dir: "/tmp/seraph/artifacts", capture_mode: "on_switch", cadence_seconds: null,
      daemon_connected: true, artifact_count: 0, last_artifact_at: null,
    });
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/settings/artifact-storage")) return Promise.resolve(mockResponse(artifactStorage));
      return Promise.reject(new Error("metadata offline"));
    });

    render(<ArtifactStoragePanel />);

    expect(
      await screen.findByText("Model-fabric metadata is temporarily unavailable; showing last-known settings."),
    ).toBeInTheDocument();
    expect(screen.getByText(/retained-local\/retained-gemma:routable/)).toBeInTheDocument();
    expect(screen.getByText("stale")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run canary" })).toBeDisabled();
  });

  it("renders screen, report, and email artifact configuration", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({
        screen: {
          analysis_enabled: true,
          provider: "local-vlm",
          model: "gemma-4-26b",
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
        screenshot_folder: {
          enabled: true,
          provider: "screenshot_folder",
          path: "/Users/test/Pictures/Screenshots",
          path_source: "default",
          image_count: 15,
          last_image_at: "2026-06-20T18:40:00Z",
          status: "ready",
          exists: true,
          readable: true,
          stored_artifacts: ["image"],
          analysis: {
            provider: "local-vlm",
            model: "gemma-4-26b",
            base_url_configured: true,
            observation_count: 12,
            analysis_status: {
              succeeded: 9,
              failed: 1,
              pending: 2,
              needs_reanalysis: 0,
              unknown: 0,
            },
            analysis_backlog: 2,
            analysis_failures: 1,
            latest_observation_at: "2026-06-20T18:41:00Z",
            latest_analyzed_at: "2026-06-20T18:42:00Z",
            latest_failure: "provider unavailable",
            digest_count: 3,
            latest_digest_at: "2026-06-20T18:30:00Z",
            folder_image_count: 15,
            ingested_count: 12,
            remaining_to_ingest: 3,
            processed_count: 9,
            remaining_to_analyze: 3,
            folder_remaining_to_analyze: 6,
            persistence: {
              db_lock_retries: 4,
              db_lock_failures: 1,
              selection_db_lock_retries: 1,
              selection_db_lock_failures: 0,
              persistence_db_lock_retries: 3,
              persistence_db_lock_failures: 1,
            },
          },
          auto_ingest_enabled: true,
          auto_ingest_interval_min: 5,
          auto_ingest_limit: 100,
          scan_endpoint: "/api/observer/screenshot-folder/scan",
          inspection_endpoint: "/api/observer/screen-artifacts",
          inspection_visibility: "localhost_only",
          control_env: {
            path: "SERAPH_SCREENSHOT_FOLDER",
            auto_ingest_enabled: "SCREENSHOT_FOLDER_INGEST_ENABLED",
            auto_ingest_interval: "SCREENSHOT_FOLDER_INGEST_INTERVAL_MIN",
            auto_ingest_limit: "SCREENSHOT_FOLDER_INGEST_LIMIT",
          },
        },
        reports: {
          enabled: true,
          hour: 21,
          analysis_provider: "llm",
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
        local_runtime: {
          gateway_configured: true,
          llm_base_url_configured: true,
          vlm_base_url_configured: true,
          model: "openai/unsloth/gemma-4-26B-A4B-it-qat-GGUF",
          profiles: [
            {
              id: "screenshot_fast",
              runtime_path: "screenshot_image_analysis",
              priority: "background",
              reasoning: "off",
              max_tokens: 1400,
              timeout_seconds: 120,
            },
            {
              id: "chat_thinking",
              runtime_path: "chat_agent",
              priority: "interactive",
              reasoning: "on",
              max_tokens: 4096,
              timeout_seconds: 120,
            },
          ],
          profile_proof: {
            status: "unsafe",
            per_request_reasoning_control: "failed",
            safe_for_single_backend_profile_routing: false,
            receipt_count: 2,
            last_receipt_at: "2026-07-01T15:38:40Z",
            last_receipt_sha256: "b1254d311dd78e92c3b7681dca2885d224e03523add6f856ea34f6bf49832748",
            notes: ["screenshot_fast emitted visible reasoning markers"],
          },
          proof_command: "PYTHONPATH=. uv run python ../scripts/verify_local_gemma_profiles.py",
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

    expect(await screen.findByText("Seraph analysis")).toBeInTheDocument();
    expect(await screen.findByText(/15 images/)).toBeInTheDocument();
    expect(screen.getByText("Screenshot Folder")).toBeInTheDocument();
    expect(screen.getByText("scans a local screenshot folder; reports stay in Seraph")).toBeInTheDocument();
    expect(screen.getByText("Local screenshot images")).toBeInTheDocument();
    expect(screen.getByText("every 5m · up to 100 images")).toBeInTheDocument();
    expect(screen.getByText("local image files only")).toBeInTheDocument();
    expect(screen.getByText("local-vlm · gemma-4-26b")).toBeInTheDocument();
    expect(screen.getByText("Local Gemma runtime")).toBeInTheDocument();
    expect(screen.getByText("openai/unsloth/gemma-4-26B-A4B-it-qat-GGUF")).toBeInTheDocument();
    expect(screen.getByText("single backend profile routing not safe")).toBeInTheDocument();
    expect(screen.getByText("screenshot_fast emitted visible reasoning markers")).toBeInTheDocument();
    expect(screen.getByText("12 / 15 · remaining 3")).toBeInTheDocument();
    expect(screen.getByText("9 analyzed · 3 queued")).toBeInTheDocument();
    expect(screen.getByText("12 observations · 2 backlog · 1 failed")).toBeInTheDocument();
    expect(screen.getByText("4 retries · 1 failed · 3 writes")).toBeInTheDocument();
    expect(screen.getByText("3 windows · latest 2026-06-20T18:30:00Z")).toBeInTheDocument();
    expect(screen.getByText("provider unavailable")).toBeInTheDocument();
    expect(screen.queryByText("/api/observer/screenshot-folder/scan")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Scan folder" })).toBeInTheDocument();
    expect(screen.getByDisplayValue("local-vlm")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("detailed / 60s")).not.toBeInTheDocument();
    expect(screen.queryByText("offline - no new captures")).not.toBeInTheDocument();
    expect(screen.queryByText("Grant Screen Recording permission to the terminal/app running Seraph.")).not.toBeInTheDocument();
    expect(screen.queryByText(/1 captures/)).not.toBeInTheDocument();
    expect(screen.getAllByText("/api/observer/screen-artifacts (localhost only)")).toHaveLength(1);
    expect(screen.queryByText("SERAPH_PRESERVE_SCREEN_CAPTURES")).not.toBeInTheDocument();
    expect(screen.getAllByText("ready")).toHaveLength(2);
    expect(screen.getByText("End-of-day reports")).toBeInTheDocument();
    expect(screen.getByText("llm")).toBeInTheDocument();
    expect(screen.getByText("Email delivery")).toBeInTheDocument();
    expect(screen.getByText("SMTP")).toBeInTheDocument();
    expect(screen.getAllByText("Missing")).toHaveLength(3);
  });

  it("does not mark VLM direct route healthy without chat readiness proof", async () => {
    const artifactStorage = settingsFromScreenAnalysisFixture({
      enabled: true,
      provider: "local-vlm",
      model: "gemma-4-26b",
      preserve_captures: true,
      archive_dir: "/tmp/seraph-dev-data/artifacts/screen-captures",
      screenshot_folder: "/Users/test/Pictures/Screenshots",
      capture_mode: "on_switch",
      cadence_seconds: null,
      daemon_connected: true,
      artifact_count: 1,
      last_artifact_at: null,
    });
    (artifactStorage as any).local_runtime = {
      gateway_configured: true,
      llm_base_url_configured: true,
      vlm_base_url_configured: true,
      model: "openai/unsloth/gemma-4-26B-A4B-it-qat-GGUF",
      profiles: [],
      profile_proof: {
        status: "safe",
        per_request_reasoning_control: "passed",
        safe_for_single_backend_profile_routing: true,
        receipt_count: 1,
        last_receipt_at: "2026-07-03T18:00:00Z",
        last_receipt_sha256: "abc123",
        notes: [],
      },
      proof_command: "PYTHONPATH=. uv run python ../scripts/verify_local_gemma_profiles.py",
    };
    (artifactStorage as any).local_runtime.vlm_runtime = {
      mode: "gpu-server",
      configured: true,
      base_url: "http://192.168.1.26:8001",
      backend_url: "http://192.168.1.26:8000/v1",
      chat_api_base: "http://192.168.1.26:8001/v1",
      chat_completion_endpoint: "http://192.168.1.26:8001/v1/chat/completions",
      chat_health_endpoint: "http://192.168.1.26:8001/health/chat",
      queue_status_endpoint: "http://192.168.1.26:8001/queue/status",
      health_endpoint: "http://192.168.1.26:8001/health",
      backend_health_endpoint: "http://192.168.1.26:8001/health/backend",
      api_key_configured: true,
      feeder_window: 2,
      live_probe: {
        checked: true,
        reachable: true,
        health: { checked: true, ok: true, status_code: 200, error: "" },
        backend_health: { checked: true, ok: true, status_code: 200, error: "" },
        queue_status: { checked: true, ok: true, status_code: 200, error: "" },
      },
    };
    fetchMock.mockResolvedValueOnce(mockResponse(artifactStorage));

    render(<ArtifactStoragePanel />);

    expect(await screen.findByText("openai/unsloth/gemma-4-26B-A4B-it-qat-GGUF")).toBeInTheDocument();
    expect(screen.getByText("direct route failing · chat:missing")).toBeInTheDocument();
    expect(screen.queryByText("direct route ok")).not.toBeInTheDocument();
  });

  it("runs manual report preview and shows safe receipt metadata", async () => {
    const artifactStorage = settingsFromScreenAnalysisFixture({
      enabled: true,
      provider: "local-vlm",
      model: "gemma-4-26b",
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
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/settings/end-of-day-report/manual") && init?.method === "POST") {
        return Promise.resolve(mockResponse({
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
        }));
      }
      if (url.includes("/api/settings/artifact-storage")) return Promise.resolve(mockResponse(artifactStorage));
      return Promise.reject(new Error(`unavailable ${url}`));
    });

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

  it("runs a local screenshot folder scan from the settings panel", async () => {
    const artifactStorage = {
      ...settingsFromScreenAnalysisFixture({
        enabled: true,
        provider: "local-vlm",
        model: "gemma-4-26b",
        preserve_captures: true,
        archive_dir: "/tmp/seraph-dev-data/artifacts/screen-captures",
        capture_mode: "on_switch",
        cadence_seconds: null,
        daemon_connected: true,
        artifact_count: 0,
        last_artifact_at: null,
      }),
      screenshot_folder: {
        enabled: true,
        provider: "screenshot_folder",
        path: "/Users/test/Pictures/Screenshots",
        path_source: "SERAPH_SCREENSHOT_FOLDER",
        image_count: 3,
        last_image_at: "2026-06-20T18:40:00Z",
        status: "ready",
        exists: true,
        readable: true,
        stored_artifacts: ["image"],
        auto_ingest_enabled: true,
        auto_ingest_interval_min: 5,
        auto_ingest_limit: 100,
        scan_endpoint: "/api/observer/screenshot-folder/scan",
        inspection_endpoint: "/api/observer/screen-artifacts",
        inspection_visibility: "localhost_only",
        control_env: {
          path: "SERAPH_SCREENSHOT_FOLDER",
          auto_ingest_enabled: "SCREENSHOT_FOLDER_INGEST_ENABLED",
          auto_ingest_interval: "SCREENSHOT_FOLDER_INGEST_INTERVAL_MIN",
          auto_ingest_limit: "SCREENSHOT_FOLDER_INGEST_LIMIT",
        },
      },
    };
    const refreshedStorage = {
      ...artifactStorage,
      screenshot_folder: {
        ...artifactStorage.screenshot_folder,
        image_count: 4,
      },
    };
    let scanRequested = false;
    fetchMock.mockImplementation((url: string) => {
      if (url.includes("/api/observer/screenshot-folder/scan")) {
        scanRequested = true;
        return Promise.resolve(mockResponse({
          screenshot_folder: artifactStorage.screenshot_folder.path,
          scanned: 3,
          ingested: 1,
          skipped_duplicates: 2,
          rejected: [],
        }));
      }
      if (url.includes("/api/settings/artifact-storage")) {
        return Promise.resolve(mockResponse(scanRequested ? refreshedStorage : artifactStorage));
      }
      return Promise.resolve(mockResponse({}));
    });

    render(<ArtifactStoragePanel />);

    const scanButton = await screen.findByRole("button", { name: "Scan folder" });
    await waitFor(() => expect(scanButton).not.toBeDisabled());
    fireEvent.click(scanButton);

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/observer/screenshot-folder/scan"),
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({
            screenshot_folder: artifactStorage.screenshot_folder.path,
            limit: 100,
          }),
        }),
      ),
    );
    expect(await screen.findByText(/scanned 3 · added 1/)).toBeInTheDocument();
    expect(screen.getByText(/duplicates 2/)).toBeInTheDocument();
  });

  it("clears stale screenshot folder observations and refreshes metadata", async () => {
    const artifactStorage = settingsFromScreenAnalysisFixture({
      enabled: true,
      provider: "local-vlm",
      model: "gemma-4-26b",
      preserve_captures: true,
      archive_dir: "/tmp/seraph-dev-data/artifacts/screen-captures",
      capture_mode: "on_switch",
      cadence_seconds: null,
      daemon_connected: true,
      artifact_count: 0,
      last_artifact_at: null,
      screenshot_folder: "/Users/test/Pictures/Screenshots",
    });
    const storageWithStaleRows = {
      ...artifactStorage,
      screenshot_folder: {
        ...artifactStorage.screenshot_folder,
        analysis: {
          ...artifactStorage.screenshot_folder.analysis,
          stale_count: 2,
          source_missing_count: 1,
          stale_root_count: 1,
        },
      },
    };
    const refreshedStorage = {
      ...storageWithStaleRows,
      screenshot_folder: {
        ...storageWithStaleRows.screenshot_folder,
        analysis: {
          ...storageWithStaleRows.screenshot_folder.analysis,
          stale_count: 0,
          source_missing_count: 0,
          stale_root_count: 0,
        },
      },
    };

    let artifactReads = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/settings/screen-analysis/screenshot-folder/clear-stale") && init?.method === "POST") {
        return Promise.resolve(mockResponse({ archived: 2, source_missing: 1, stale_root: 1 }));
      }
      if (url.includes("/api/settings/artifact-storage")) {
        artifactReads += 1;
        return Promise.resolve(mockResponse(artifactReads === 1 ? storageWithStaleRows : refreshedStorage));
      }
      return Promise.reject(new Error(`unavailable ${url}`));
    });

    render(<ArtifactStoragePanel />);

    expect(await screen.findByText(/2 cleanup candidates · 1 missing · 1 old root/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Clear stale" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/settings/screen-analysis/screenshot-folder/clear-stale"),
        expect.objectContaining({ method: "POST" }),
      ),
    );
    await waitFor(() =>
      expect(screen.queryByText(/2 cleanup candidates · 1 missing · 1 old root/)).not.toBeInTheDocument(),
    );
  });

  it("saves a configured screenshot folder", async () => {
    const artifactStorage = settingsFromScreenAnalysisFixture({
      enabled: true,
      provider: "local-vlm",
      model: "gemma-4-26b",
      preserve_captures: true,
      archive_dir: "/tmp/seraph-dev-data/artifacts/screen-captures",
      capture_mode: "on_switch",
      cadence_seconds: null,
      daemon_connected: true,
      artifact_count: 0,
      last_artifact_at: null,
      screenshot_folder: "/Users/test/Pictures/Screenshots",
    });
    const nextRoot = "/Users/test/Screenshot Folder";
    const refreshedStorage = {
      ...artifactStorage,
      screenshot_folder: {
        ...artifactStorage.screenshot_folder,
        path: nextRoot,
        path_source: "screen-analysis-settings",
      },
    };
    fetchMock
      .mockResolvedValueOnce(mockResponse(artifactStorage))
      .mockResolvedValueOnce(mockResponse({ ok: true }))
      .mockResolvedValueOnce(mockResponse(refreshedStorage));

    render(<ArtifactStoragePanel />);

    const folderInput = await screen.findByDisplayValue(artifactStorage.screenshot_folder.path ?? "");
    fireEvent.change(folderInput, { target: { value: nextRoot } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/settings/screen-analysis"),
        expect.objectContaining({
          method: "PUT",
          body: JSON.stringify({ screenshot_folder: nextRoot }),
        }),
      ),
    );
    expect(await screen.findByDisplayValue(nextRoot)).toBeInTheDocument();
    expect(screen.getAllByText("screen-analysis-settings").length).toBeGreaterThanOrEqual(1);
  });

  it("opens the native screenshot folder picker and refreshes selected metadata", async () => {
    const artifactStorage = settingsFromScreenAnalysisFixture({
      enabled: true,
      provider: "local-vlm",
      model: "gemma-4-26b",
      preserve_captures: true,
      archive_dir: "/tmp/seraph-dev-data/artifacts/screen-captures",
      capture_mode: "on_switch",
      cadence_seconds: null,
      daemon_connected: true,
      artifact_count: 0,
      last_artifact_at: null,
      screenshot_folder: "/Users/test/Pictures/Screenshots",
    });
    const pickedRoot = "/Users/test/Desktop/screenshots";
    const refreshedStorage = {
      ...artifactStorage,
      screenshot_folder: {
        ...artifactStorage.screenshot_folder,
        path: pickedRoot,
        path_source: "screen-analysis-settings",
      },
    };
    let artifactReads = 0;
    fetchMock.mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/api/settings/screen-analysis/screenshot-folder/pick") && init?.method === "POST") {
        return Promise.resolve(mockResponse({
          screenshot_folder: pickedRoot,
          screenshot_folder_source: "screen-analysis-settings",
        }));
      }
      if (url.includes("/api/settings/artifact-storage")) {
        artifactReads += 1;
        return Promise.resolve(mockResponse(artifactReads === 1 ? artifactStorage : refreshedStorage));
      }
      return Promise.reject(new Error(`unavailable ${url}`));
    });

    render(<ArtifactStoragePanel />);

    fireEvent.click(await screen.findByRole("button", { name: "Choose" }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining("/api/settings/screen-analysis/screenshot-folder/pick"),
        expect.objectContaining({ method: "POST" }),
      ),
    );
    expect(await screen.findByDisplayValue(pickedRoot)).toBeInTheDocument();
    expect(screen.getByText("Choose opens the local folder picker; typing the path is the fallback.")).toBeInTheDocument();
  });

  it("does not refresh settings after a save resolves on an unmounted panel", async () => {
    const artifactStorage = {
      screen: {
        analysis_enabled: true,
        provider: "local-vlm",
        model: "gemma-4-26b",
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

    fetchMock.mockClear();

    unmount();
    resolveSave(mockResponse({ enabled: true, provider: "local-vlm", model: "gemma-4-26b" }));
    await Promise.resolve();

    expect(fetchMock.mock.calls.filter(([url]) => String(url).includes("/api/settings/capture-mode"))).toHaveLength(0);
  });

  it("keeps analysis controls visible when artifact metadata is unavailable", async () => {
    fetchMock.mockImplementation((url: string) => {
      if (url.includes("/api/settings/artifact-storage")) {
        return Promise.reject(new Error("artifact endpoint timed out"));
      }
      if (url.includes("/api/settings/screen-analysis")) {
        return Promise.resolve(mockResponse({
          enabled: true,
          provider: "local-vlm",
          model: "gemma-4-26b",
          preserve_captures: true,
          archive_dir: "/tmp/seraph-dev-data/artifacts/screen-captures",
          capture_mode: "on_switch",
          cadence_seconds: null,
          daemon_connected: true,
          artifact_count: 7,
          last_artifact_at: "2026-06-21T08:42:52Z",
        }));
      }
      return Promise.resolve(mockResponse({}));
    });

    render(<ArtifactStoragePanel />);

    expect(await screen.findByText("Seraph analysis", undefined, { timeout: 1_000 })).toBeInTheDocument();
    expect(await screen.findByText("Folder metadata is still loading; analysis controls are live.")).toBeInTheDocument();
    expect(await screen.findByDisplayValue("local-vlm")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("on_switch")).not.toBeInTheDocument();
    expect(screen.queryByText("Artifact storage settings unavailable.")).not.toBeInTheDocument();
    expect(screen.queryByText("Screenshot folder settings unavailable.")).not.toBeInTheDocument();
  });

  it("shows degraded metadata warning when artifact metadata has an invalid shape", async () => {
    fetchMock.mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/api/settings/artifact-storage")) {
        return Promise.resolve(mockResponse({ screen: { archive_dir: "/tmp/broken" } }));
      }
      if (url.includes("/api/settings/screen-analysis")) {
        return Promise.resolve(mockResponse({
          enabled: true,
          provider: "local-vlm",
          model: "gemma-4-26b",
          preserve_captures: true,
          archive_dir: "/tmp/seraph-dev-data/artifacts/screen-captures",
          capture_mode: "on_switch",
          cadence_seconds: null,
          daemon_connected: true,
          artifact_count: 0,
          last_artifact_at: null,
        }));
      }
      return Promise.reject(new Error(`unavailable ${url}`));
    });

    render(<ArtifactStoragePanel />);

    expect(await screen.findByText("Seraph analysis", undefined, { timeout: 1_000 })).toBeInTheDocument();
    expect(
      await screen.findByText("Folder metadata is still loading; analysis controls are live."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Folder metadata loading; analysis controls are live.")).not.toBeInTheDocument();
    expect(screen.queryByText("Screenshot folder settings unavailable.")).not.toBeInTheDocument();
  });

  it("aborts hung artifact metadata and keeps analysis controls visible", async () => {
    let artifactSignal: AbortSignal | undefined;
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      if (url.includes("/api/settings/artifact-storage")) {
        artifactSignal = init?.signal ?? undefined;
        return new Promise((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError")));
        });
      }
      if (url.includes("/api/settings/screen-analysis")) {
        return Promise.resolve(mockResponse({
          enabled: true,
          provider: "local-vlm",
          model: "gemma-4-26b",
          preserve_captures: true,
          archive_dir: "/tmp/seraph-dev-data/artifacts/screen-captures",
          capture_mode: "on_switch",
          cadence_seconds: null,
          daemon_connected: true,
          artifact_count: 0,
          last_artifact_at: null,
        }));
      }
      return Promise.resolve(mockResponse({}));
    });

    render(<ArtifactStoragePanel />);

    expect(
      await screen.findByText(
        "Folder metadata is still loading; analysis controls are live.",
        undefined,
        { timeout: 5_000 },
      ),
    ).toBeInTheDocument();
    expect(screen.getByDisplayValue("local-vlm")).toBeInTheDocument();
    expect(artifactSignal?.aborted).toBe(true);
    expect(screen.queryByText("Artifact storage settings unavailable.")).not.toBeInTheDocument();
    expect(screen.queryByText("Screenshot folder settings unavailable.")).not.toBeInTheDocument();
  }, 7_000);

  it("does not present fallback screenshot folder metadata as editable truth", async () => {
    fetchMock.mockRejectedValue(new Error("settings unavailable"));

    render(<ArtifactStoragePanel />);

    expect(await screen.findByText("Seraph analysis", undefined, { timeout: 1_000 })).toBeInTheDocument();
    expect(screen.getByLabelText("Screenshot folder")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Scan folder" })).toBeDisabled();
    expect(screen.queryByText("Seraph workspace artifacts/screenshot-folder")).not.toBeInTheDocument();
    expect(screen.getAllByText("not set").length).toBeGreaterThanOrEqual(1);
    expect(
      await screen.findByText("Settings metadata is temporarily unavailable; folder controls are disabled until real metadata returns."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Screenshot folder settings unavailable.")).not.toBeInTheDocument();
  });
});
