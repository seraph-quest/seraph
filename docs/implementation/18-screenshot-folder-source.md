---
slug: /screenshot-folder-source
title: Screenshot Folder Source
---

# Screenshot Folder Source

Seraph does not connect to a screenshot app or service. It can scan a local directory that contains ordinary screenshot image files.

Framekeeper is one possible producer for that directory, but Seraph does not call Framekeeper, read Framekeeper metadata, require manifests, or expect any service handshake. The contract is just `.png`, `.jpg`, and `.jpeg` files in a configured folder.

## Boundary

The screenshot producer owns:

- screenshot capture
- operating-system permissions
- capture cadence and pause state
- folder selection
- retention and storage cleanup
- blocklists
- its own UI and activity state

Seraph owns:

- scanning a configured directory for image files
- computing Seraph-side image hashes
- duplicate detection
- local image artifact analysis and future provider-backed image analysis through Seraph settings
- `ScreenObservation` persistence
- report generation

The screenshot producer must not write Seraph-specific sidecars, observations, analysis output, reports, or manifests for this path. Seraph must not require a live producer connection.

## Configuration

Seraph resolves the screenshot folder in this order:

1. `SERAPH_SCREENSHOT_FOLDER`
2. Seraph settings key `screenshot_folder`
3. Seraph workspace default `artifacts/screenshot-folder`

The default is a generic Seraph-owned workspace folder so unconfigured Seraph never assumes a specific screenshot producer. To consume Framekeeper output, configure Seraph with Framekeeper's screenshot folder explicitly.

Seraph does not migrate or resolve producer-specific screenshot keys. API requests, stored settings, and environment configuration use only `screenshot_folder` or `SERAPH_SCREENSHOT_FOLDER`. `artifact_root` and producer-specific key names are not part of the current contract.

## Folder Scan

Seraph exposes one on-demand local scan endpoint for its own UI and scheduler:

```http
POST /api/observer/screenshot-folder/scan
```

Optional JSON body:

```json
{
  "screenshot_folder": "/path/to/screenshots",
  "limit": 100
}
```

If `screenshot_folder` is omitted, Seraph uses the configured folder. For each new image, Seraph computes SHA-256 plus local image facts such as byte size, file format, and dimensions when detectable. Seraph stores those Seraph-owned facts in observation details, persists a `ScreenObservation`, and leaves analysis and report generation inside Seraph.

The request model is intentionally strict: legacy `artifact_root` or producer-specific fields are rejected instead of being treated as screenshot-folder aliases.

The artifact analysis endpoint returns Seraph-owned local image analysis, including source, hash, byte size, file format, dimensions when detectable, observation id, and report readiness. This analysis is computed from the image file in Seraph.

End-of-day reports consume screenshot-folder `ScreenObservation` rows through the same report builder as other screen observations. Seraph records the observation source as `screenshot_folder` from its own stored capture-artifact details and includes report-safe screenshot samples using filenames, format, dimensions, and size. Reports do not rely on Framekeeper manifests, sidecars, or service metadata.

Configure a narrow, trusted screenshot directory. Seraph rejects obvious broad roots such as the filesystem root, home folder, Desktop, Downloads, and Seraph workspace root.

## Settings Surface

The Seraph settings UI describes this as a local screenshot folder, not as Seraph-owned capture or a connected service. The folder status includes:

- configured screenshot folder
- configuration source
- image count
- latest image timestamp
- local image-file scan status
- manual local folder scan action
- editable saved folder when no env override is present
- inspection endpoint
- stored artifact type: `image`

The settings panel saves `screenshot_folder` through `/api/settings/screen-analysis`. The manual scan action calls Seraph's local `/api/observer/screenshot-folder/scan` endpoint. Seraph can also run its own `screenshot_folder_ingest` scheduler job, controlled by `SCREENSHOT_FOLDER_INGEST_ENABLED`, `SCREENSHOT_FOLDER_INGEST_INTERVAL_MIN`, and `SCREENSHOT_FOLDER_INGEST_LIMIT`.

Both paths only read local image files from the configured folder. They do not start, connect to, or query any screenshot producer.

## Verification

This branch verifies the image-source path with:

```bash
cd /Users/bigcube/Desktop/repos/seraph/backend
UV_CACHE_DIR=/tmp/seraph-uv-cache uv run pytest tests/test_observer_screen_artifacts.py tests/test_settings_api.py::test_artifact_storage_exposes_screenshot_folder_status tests/test_screenshot_folder_ingest_job.py

cd /Users/bigcube/Desktop/repos/seraph/frontend
npm run test -- ArtifactStoragePanel.test.tsx

cd /Users/bigcube/Desktop/repos/seraph
PYTHONPYCACHEPREFIX=/tmp/seraph-pycache python3 -m py_compile backend/src/observer/screenshot_folder_source.py backend/src/scheduler/jobs/screenshot_folder_ingest.py backend/src/scheduler/engine.py backend/src/api/observer.py backend/src/api/settings.py
git diff --check
```
