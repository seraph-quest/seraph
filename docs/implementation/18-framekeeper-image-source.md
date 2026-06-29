---
slug: /framekeeper-image-source
title: Framekeeper Screenshot Folder
---

# Framekeeper Screenshot Folder

Framekeeper is not a connected Seraph service. It is a separate screenshot recorder that writes image files to a local directory. Seraph can scan that directory as an optional screenshot folder.

## Boundary

Framekeeper owns:

- screenshot capture
- screenshot permissions
- screenshot cadence and pause state
- screenshot folder selection
- screenshot retention and storage cleanup
- blocklists
- recent screenshot activity in the Framekeeper UI

Seraph owns:

- scanning the configured directory for images
- computing Seraph-side image hashes
- duplicate detection
- local image artifact analysis and future provider-backed image analysis through Seraph settings
- `ScreenObservation` persistence
- report generation

Framekeeper does not write manifests, sidecars, analysis output, observations, reports, or Seraph-specific metadata. Seraph does not require a live connection to Framekeeper.

## Configuration

Seraph resolves the Framekeeper screenshot folder in this order:

1. `SERAPH_FRAMEKEEPER_SCREENSHOT_FOLDER`
2. Legacy env fallback `SERAPH_FRAMEKEEPER_ARTIFACT_ROOT`
3. Seraph settings key `framekeeper_screenshot_folder`
4. Legacy settings key `framekeeper_artifact_root`
5. macOS default `~/Library/Application Support/Framekeeper/artifacts`

Framekeeper screenshots are expected to be ordinary `.png`, `.jpg`, or `.jpeg` files. Seraph scans recursively and ignores non-image files.

## Folder Scan

Seraph exposes an on-demand local folder-scan endpoint for its own UI and scheduler:

```http
POST /api/observer/framekeeper/ingest
```

Optional JSON body:

```json
{
  "screenshot_folder": "/path/to/framekeeper/artifacts",
  "limit": 100
}
```

If `screenshot_folder` is omitted, Seraph uses the configured folder. The legacy `artifact_root` request key is still accepted for older clients. For each new image, Seraph computes SHA-256, stores a duplicate marker in observation details, persists a `ScreenObservation`, and leaves analysis and report generation inside Seraph. The endpoint name still contains `ingest` for compatibility with older Seraph clients, but the behavior is only a local directory scan.

The artifact analysis endpoint returns Seraph-owned local image analysis for Framekeeper screenshots, including source, hash, byte size, file format, dimensions when detectable, observation id, and report readiness. This analysis is computed from the image file in Seraph; Framekeeper still writes only screenshots.

End-of-day reports consume Framekeeper-derived `ScreenObservation` rows through the same report builder as other screen observations. Seraph records the observation source as `framekeeper` from its own stored capture-artifact details, so reports can show Framekeeper image activity without requiring any Framekeeper manifest or service connection. Source mix includes both observation counts and tracked minutes, because Framekeeper screenshots are point-in-time images and may not carry a duration.

Configure a narrow, trusted screenshot directory. Do not point folder scanning at a broad home, downloads, desktop, or project folder. A local caller that can invoke this endpoint can cause Seraph to read image files under the supplied path, hash them, and persist observations.

## Settings Surface

The Seraph settings UI describes this as a Framekeeper screenshot folder, not as Seraph-owned capture or a connected service. The folder status includes:

- configured screenshot folder
- configuration source
- image count
- latest image timestamp
- local image-file scan status
- manual local folder scan action
- editable saved folder when no env override is present
- inspection endpoint
- stored artifact type: `image`

The settings panel saves `framekeeper_screenshot_folder` through `/api/settings/screen-analysis`; an empty value resets Seraph to the default folder unless `SERAPH_FRAMEKEEPER_SCREENSHOT_FOLDER` or its legacy env fallback is set. The manual scan action calls Seraph's local `/api/observer/framekeeper/ingest` endpoint to scan the configured screenshot folder. Seraph can also run its own `framekeeper_image_ingest` scheduler job, controlled by `FRAMEKEEPER_INGEST_ENABLED`, `FRAMEKEEPER_INGEST_INTERVAL_MIN`, and `FRAMEKEEPER_INGEST_LIMIT`. Both paths only read local image files from the configured folder. They do not start Framekeeper, connect to a Framekeeper service, or ask Framekeeper for metadata. This keeps Seraph controls focused on analysis and reporting while Framekeeper stays responsible for screenshot production.

## Verification

This branch verifies the image-source path with:

```bash
cd /Users/bigcube/Desktop/repos/seraph/backend
UV_CACHE_DIR=/tmp/seraph-uv-cache uv run pytest tests/test_observer_screen_artifacts.py tests/test_settings_api.py::test_artifact_storage_exposes_framekeeper_source_status
UV_CACHE_DIR=/tmp/seraph-uv-cache uv run pytest tests/test_framekeeper_ingest_job.py

cd /Users/bigcube/Desktop/repos/seraph/frontend
npm run test -- ArtifactStoragePanel.test.tsx
npm run build

cd /Users/bigcube/Desktop/repos/seraph
PYTHONPYCACHEPREFIX=/tmp/seraph-pycache python3 -m py_compile backend/src/observer/framekeeper_source.py backend/src/scheduler/jobs/framekeeper_ingest.py backend/src/scheduler/engine.py backend/src/api/observer.py backend/src/api/settings.py
git diff --check
```
