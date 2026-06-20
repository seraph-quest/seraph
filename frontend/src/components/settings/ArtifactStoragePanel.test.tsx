import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ArtifactStoragePanel } from "./ArtifactStoragePanel";

function mockResponse(data: unknown, ok = true) {
  return {
    ok,
    json: async () => data,
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
          preservation_enabled: true,
          archive_dir: "/Users/test/Library/Application Support/Seraph/artifacts/screen-captures",
          archive_dir_source: "default",
          exists: true,
          writable: true,
          creation_error: null,
          stored_artifacts: ["image", "provider_output", "analysis_json"],
          inspection_endpoint: "/api/observer/screen-artifacts",
          inspection_visibility: "localhost_only",
          control_env: {
            enabled: "SERAPH_PRESERVE_SCREEN_CAPTURES",
            archive_dir: "SERAPH_SCREEN_CAPTURE_ARCHIVE_DIR or SCREEN_CAPTURE_ARCHIVE_DIR",
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

    await waitFor(() => expect(screen.getByText("Screen capture preservation")).toBeInTheDocument());
    expect(screen.getByText("Evidence Archive")).toBeInTheDocument();
    expect(screen.getByText("images, provider output, analysis JSON")).toBeInTheDocument();
    expect(screen.getByText("/api/observer/screen-artifacts (localhost only)")).toBeInTheDocument();
    expect(screen.getByText("SERAPH_PRESERVE_SCREEN_CAPTURES")).toBeInTheDocument();
    expect(screen.getAllByText("ready")).toHaveLength(2);
    expect(screen.getByText("End-of-day reports")).toBeInTheDocument();
    expect(screen.getByText("deterministic-local")).toBeInTheDocument();
    expect(screen.getByText("Email delivery")).toBeInTheDocument();
    expect(screen.getByText("SMTP")).toBeInTheDocument();
    expect(screen.getAllByText("Missing")).toHaveLength(2);
  });
});
