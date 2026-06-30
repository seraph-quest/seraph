---
slug: /screenshot-folder-source
title: Screenshot Folder Source
---

# Screenshot Folder Source

Seraph does not connect to a screenshot app or service. It can scan a local directory that contains ordinary screenshot image files.

The producing app is intentionally anonymous to Seraph. Seraph does not call a recorder, read recorder metadata, require manifests, or expect any service handshake. The contract is just `.png`, `.jpg`, and `.jpeg` files in a configured folder.

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

The default is a generic Seraph-owned workspace folder so unconfigured Seraph never assumes a specific screenshot producer. To consume screenshots from another app, configure Seraph with that app's screenshot folder explicitly.

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

The artifact analysis endpoint currently returns Seraph-owned local image metadata analysis, including source, hash, byte size, file format, dimensions when detectable, observation id, and report readiness. This metadata analysis is computed from the image file in Seraph and is not the final semantic VLM analysis loop.

## Semantic Screenshot Analysis Contract

Seraph's intended screenshot intelligence loop analyzes new screenshots throughout the day, not only at report time. The first shipped contract for this richer loop lives in `backend/src/observer/screenshot_analysis_contract.py`.

The contract is Seraph-owned and producer-neutral:

- schema version: `seraph.screenshot_analysis.v1`
- prompt version: `seraph.screenshot_analysis.prompt.v1`
- model output format: strict JSON only
- visible screenshot text is untrusted data
- secrets, credentials, private messages, long raw code, and long raw logs must not be copied into observations
- uncertain fields must use `unknown`, `null`, or low confidence instead of guessed detail

The semantic analysis schema captures:

- one-sentence summary
- detailed privacy-safe observations
- activity type
- inferred project
- visible applications
- visible artifacts such as files, repos, PRs, issues, pages, or tools
- short non-sensitive visible text snippets
- apparent user intent
- goal-alignment status, evidence, and pushed-the-needle signal
- confidence
- sensitive-content flag
- privacy notes
- report tags

The VLM prompt requires the model to treat screenshot content as untrusted and return only the JSON shape defined by the contract. The parser rejects non-JSON output, unknown fields, invalid enum values, and out-of-range confidence values. It also redacts sensitive-looking strings before the analysis can become a durable observation.

This contract is the boundary for the next implementation slice. The current folder scan still persists metadata observations; the follow-up analyzer will call a configured VLM, validate the output with this contract, and persist the semantic analysis without requiring any direct connection to the screenshot producer.

End-of-day reports consume screenshot-folder `ScreenObservation` rows through the same report builder as other screen observations. Seraph records the observation source as `screenshot_folder` from its own stored capture-artifact details and includes report-safe screenshot samples using filenames, format, dimensions, and size. Reports do not rely on recorder manifests, sidecars, or service metadata.

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

## Remote VLM Analysis Target

Seraph can keep screenshot production separate from analysis while still using a GPU on another machine. The target shape is:

1. A producer writes ordinary screenshot image files to the configured folder.
2. Seraph scans that folder and owns observation/report persistence.
3. A separate image-analysis service accepts image bytes and forwards them to a private LAN/VPN vision-language model backend.

The reusable service repo is public under the Seraph organization:

- repo: [seraph-quest/vlm-screenshot-server](https://github.com/seraph-quest/vlm-screenshot-server)
- purpose: Dockerized FastAPI screenshot analysis wrapper for OpenAI-compatible VLM backends
- endpoints: `POST /v1/analyze-file` for multipart uploads and `POST /v1/analyze` for base64 image payloads
- run modes: API wrapper only, or API wrapper plus a GPU `vllm/vllm-openai` backend via `docker-compose.gpu.yml`

For an RTX 3090 Ti 24 GB server, the current preferred Gemma-first target is Unsloth's Gemma 4 26B-A4B quantized GGUF/Dynamic 4-bit path. Unsloth's Gemma 4 docs list practical 4-bit memory footprints for this card class, including the 26B-A4B family in the high-teens GB range.

Example GPU-server setup with `llama.cpp`:

```bash
curl -LsSf https://llama.app/install.sh | sh

llama serve \
  -hf unsloth/gemma-4-26B-A4B-it-GGUF:UD-Q4_K_M \
  --host 0.0.0.0 \
  --port 8000 \
  --ctx-size 32768 \
  --chat-template-kwargs '{"enable_thinking":false}'
```

The VLM backend then exposes an OpenAI-compatible API at:

```text
http://GPU_SERVER_IP:8000/v1
```

Run the screenshot-analysis wrapper:

```bash
git clone https://github.com/seraph-quest/vlm-screenshot-server.git
cd vlm-screenshot-server
cp .env.example .env
```

Use:

```env
VLM_BASE_URL=http://GPU_SERVER_IP:8000/v1
VLM_MODEL=unsloth/gemma-4-26B-A4B-it-GGUF:UD-Q4_K_M
VLM_API_KEY=
VLM_TIMEOUT_SECONDS=180
VLM_MAX_TOKENS=700
VLM_TEMPERATURE=0
REDACT_VISIBLE_TEXT=true
```

Then start the wrapper:

```bash
docker compose up --build
```

Test the wrapper with a screenshot:

```bash
curl -F "file=@/path/to/screenshot.png" \
  http://GPU_SERVER_IP:8088/v1/analyze-file
```

Seraph-side first-class `local-vlm` wiring is available behind explicit settings:

```env
SERAPH_SCREEN_ANALYSIS_PROVIDER=local-vlm
SERAPH_LOCAL_VLM_BASE_URL=http://GPU_SERVER_IP:8088
SERAPH_LOCAL_VLM_MODEL=unsloth/gemma-4-26B-A4B-it-GGUF:UD-Q4_K_M
```

When configured, screenshot-folder ingestion posts the screenshot image plus Seraph's strict analysis prompt to `/v1/analyze-file`, validates the returned JSON against `seraph.screenshot_analysis.v1`, and stores the privacy-safe semantic payload inside the Seraph `ScreenObservation`.
If the provider is not configured or fails, ingestion still stores the screenshot metadata observation and records a bounded analyzer status instead of retrying the same image as a new screenshot.

Each screenshot observation carries Seraph-owned analysis status details:

- `pending` when the screenshot was ingested but no semantic provider was configured
- `succeeded` when a validated semantic analysis payload was stored
- `failed` when the provider call or schema validation failed
- `needs_reanalysis` when a stored semantic payload was produced by an older prompt, schema, or configured model

Duplicate screenshot files are still suppressed by image SHA-256, so the same image cannot accidentally create a second semantic observation.
Reanalysis is explicit and local-only through `POST /api/observer/screen-artifacts/{observation_id}/reanalyze`; callers must provide one of `prompt_version_changed`, `model_version_changed`, `provider_failure_retry`, or `manual_operator_request`.
Reanalysis replaces the semantic analysis/status details on the existing observation and preserves the original screenshot hash and file mtime-derived capture timestamp.

## Rolling Observation Digests

Seraph condenses analyzed screenshot-folder observations into rolling memory episodes before daily report generation. The scheduler job is `screenshot_observation_digest`.

Default settings:

- `SCREENSHOT_OBSERVATION_DIGEST_ENABLED=true`
- `SCREENSHOT_OBSERVATION_DIGEST_INTERVAL_MIN=15`
- `SCREENSHOT_OBSERVATION_DIGEST_WINDOW_MIN=30`
- `SCREENSHOT_OBSERVATION_DIGEST_MAX_CHARS=6000`

Each digest stores a `MemoryEpisode` with source tool `screenshot_observation_digest` and schema `seraph.screenshot_observation_digest.v1`. Digest metadata includes the digest key, window start/end, source screenshot observation ids, observation count, content character count, and payload SHA-256.

The digest is intentionally text-only and privacy-bounded. It never embeds raw screenshots, image hashes, full visible text, or provider transcripts. It includes compact redacted progression notes, activity/project/app mixes, analysis status counts, confidence, blocker evidence, drift evidence, and the source observation ids needed for traceability.

Digest writes are idempotent per window and schema. If a window has not changed, Seraph keeps the existing episode. If new observations appear in the same window, Seraph updates the same episode instead of creating duplicates.

Current model notes:

- Gemma 4 26B-A4B quantized via Unsloth is the preferred 24 GB GPU benchmark target.
- Gemma 4 12B QAT/Q4 is the safer fallback if 26B-A4B is too slow or unavailable.
- MiniCPM-V 4.5 quantized variants remain the strongest small non-Gemma comparison.
- Qwen2.5-VL-32B-Instruct-AWQ is the known Qwen VLM stress test for quality, but it is memory-sensitive on 24 GB.
- Qwen3 3.6B or "Qwen 3.6" should not be used for screenshot analysis unless the exact checkpoint is confirmed as a vision-language model. Text-only Qwen3 checkpoints do not replace a VLM.

Sources checked June 30, 2026:

- [Unsloth Gemma 4 models](https://unsloth.ai/docs/models/gemma-4)
- [Unsloth Gemma 4 26B-A4B GGUF](https://huggingface.co/unsloth/gemma-4-26B-A4B-it-GGUF)
- [seraph-quest/vlm-screenshot-server](https://github.com/seraph-quest/vlm-screenshot-server)

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
