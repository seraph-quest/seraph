---
slug: /framekeeper-image-source
title: Framekeeper Image Source
---

# Framekeeper Image Source

Framekeeper is not a connected Seraph service. It is a separate screenshot recorder that writes image files to a local directory. Seraph can use that directory as an optional image source.

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

Seraph resolves the Framekeeper screenshot root in this order:

1. `SERAPH_FRAMEKEEPER_ARTIFACT_ROOT`
2. Seraph settings key `framekeeper_artifact_root`
3. macOS default `~/Library/Application Support/Framekeeper/artifacts`

Framekeeper screenshots are expected to be ordinary `.png`, `.jpg`, or `.jpeg` files. Seraph scans recursively and ignores non-image files.

## Ingestion

Seraph exposes an on-demand ingestion endpoint:

```http
POST /api/observer/framekeeper/ingest
```

Optional JSON body:

```json
{
  "artifact_root": "/path/to/framekeeper/artifacts",
  "limit": 100
}
```

If `artifact_root` is omitted, Seraph uses the configured root. For each new image, Seraph computes SHA-256, stores a duplicate marker in observation details, persists a `ScreenObservation`, and leaves analysis and report generation inside Seraph.

The artifact analysis endpoint returns Seraph-owned local image analysis for Framekeeper screenshots, including source, hash, byte size, file format, dimensions when detectable, observation id, and report readiness. This analysis is computed from the image file in Seraph; Framekeeper still writes only screenshots.

End-of-day reports consume Framekeeper-derived `ScreenObservation` rows through the same report builder as other screen observations. Seraph records the observation source as `framekeeper` from its own stored capture-artifact details, so reports can show Framekeeper image activity without requiring any Framekeeper manifest or service connection.

Configure a narrow, trusted screenshot directory. Do not point ingestion at a broad home, downloads, desktop, or project folder. A local caller that can invoke this endpoint can cause Seraph to read image files under the supplied path, hash them, and persist observations.

## Settings Surface

The Seraph settings UI describes this as a Framekeeper source, not as Seraph-owned capture. The source status includes:

- configured root
- configuration source
- image count
- latest image timestamp
- ingest endpoint
- inspection endpoint
- stored artifact type: `image`

This keeps Seraph controls focused on analysis and reporting while Framekeeper stays responsible for screenshot production.

## Verification

This branch verifies the image-source path with:

```bash
cd /Users/bigcube/Desktop/repos/seraph/backend
UV_CACHE_DIR=/tmp/seraph-uv-cache uv run pytest tests/test_observer_screen_artifacts.py tests/test_settings_api.py::test_artifact_storage_exposes_framekeeper_source_status

cd /Users/bigcube/Desktop/repos/seraph/frontend
npm run test -- ArtifactStoragePanel.test.tsx
npm run build

cd /Users/bigcube/Desktop/repos/seraph
PYTHONPYCACHEPREFIX=/tmp/seraph-pycache python3 -m py_compile backend/src/observer/framekeeper_source.py backend/src/api/observer.py backend/src/api/settings.py
git diff --check
```
