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

The configured folder is the image root. Seraph does not append, require, or special-case a producer-side `captures/` subdirectory; if screenshots are written directly under `/Users/.../Desktop/screenshots`, that exact folder is the Seraph `screenshot_folder`.

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

The VLM prompt requires the model to treat screenshot content as untrusted and return only the JSON shape defined by the contract. The parser rejects non-JSON output, unknown fields, invalid enum values, and out-of-range confidence values. Seraph orchestration must not author semantic screenshot summaries or daily report conclusions with deterministic Python parsing; those reasoning steps belong to the configured LLM runtime.

This contract is the boundary between local image ingestion and screenshot understanding. The folder scan stores the local image metadata observation and marks semantic work as pending. The separate `screenshot_folder_analysis` scheduler job calls the configured Seraph-side analyzer, validates the output with this contract, and replaces the pending status on the existing observation without requiring any direct connection to the screenshot producer.

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

The settings panel saves `screenshot_folder` through `/api/settings/screen-analysis`. The manual scan action calls Seraph's local `/api/observer/screenshot-folder/scan` endpoint. Seraph can also run its own `screenshot_folder_ingest` scheduler job, controlled by `SCREENSHOT_FOLDER_INGEST_ENABLED`, `SCREENSHOT_FOLDER_INGEST_INTERVAL_MIN`, and `SCREENSHOT_FOLDER_INGEST_LIMIT`. The local default is enabled, every 1 minute, up to 100 images per tick so a newly mounted backlog is ingested quickly enough for the VLM lane to stay busy.

Semantic analysis is a second scheduler lane, controlled by `SCREENSHOT_FOLDER_ANALYSIS_LIMIT`, `SCREENSHOT_FOLDER_ANALYSIS_CONCURRENCY`, and `SCREENSHOT_FOLDER_ANALYSIS_INTERVAL_SECONDS`. In the one-GPU local topology, Seraph feeds a tiny analysis window on each scheduler tick: one active GPU request plus one queued background request by default. The VLM wrapper still runs GPU inference serially and owns priority ordering for the next accepted job; Seraph does not cancel an already-running GPU request. The analysis job starts only when screenshot-folder ingestion is enabled and the local VLM service answers `/health` with free queue capacity, then records `succeeded`, `failed`, or `skipped` scheduler receipts. A transient failed analysis is retried after a short cooldown up to a bounded attempt count, so one bad VLM response does not strand the item while repeated bad rows remain visible as failed. Candidate selection and result persistence use short DB sessions with bounded retry/backoff for SQLite lock errors. If a lock is exhausted, Seraph records the batch as degraded or failed and leaves pending work available for a later scheduler pass instead of holding the scheduler lane forever. This keeps folder scanning cheap, avoids long scheduler-owned batches, and lets the VLM queue stay fed without blocking the producer or duplicating observations.

Both paths only read local image files from the configured folder. They do not start, connect to, or query any screenshot producer.

The artifact-storage settings API also exposes Seraph-owned screenshot analysis status for the configured folder: observation count, total historical observation count, analyzer status mix, active backlog, active failures, stale cleanup counts, visual run count, visual suppression count, DB lock retry/failure counters, latest observation/analyzed timestamps, digest count, and latest digest timestamp. These metadata summaries are bounded with short degraded fallbacks so the Settings modal stays usable even when filesystem, DB, proof, or receipt metadata is slow. Filesystem summary degradation is explicit through `screenshot_folder.summary_status=partial` plus `summary_failure`, and analysis metadata degradation is explicit through `screenshot_folder.analysis.metadata_status=partial` plus `metadata_failure`. The UI shows these fields beside the local folder path and scan controls so the operator can see whether screenshots are being analyzed, compressed before VLM, blocked by local persistence contention, and rolled into report-ready digest windows without blocking status endpoints while a screenshot or VLM backlog drains.

If a screenshot image disappears before analysis, Seraph records the observation status as `source_missing` instead of retrying it as a generic provider failure. If an incomplete observation points at an older configured screenshot root, the active settings summary classifies it as `stale_root`. `source_missing` and `stale_root` rows are excluded from active backlog/failure counts and from screenshot digest or end-of-day report inputs. The Settings panel shows stale cleanup candidates and exposes a localhost-only Clear Stale action that archives only stale incomplete rows; succeeded rows remain historical evidence even if their original image file has since been deleted.

The same surface exposes local Gemma runtime profile status: configured gateway state, active model, built-in profile contracts, latest profile-proof receipt, and whether single-backend profile routing is currently safe.

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

### Current Local Topology

The development topology is concrete and should be verified exactly before debugging screenshot or chat routing:

```text
Seraph frontend       http://127.0.0.1:3001
  -> Seraph backend   http://127.0.0.1:8004
  -> GPU VLM wrapper  http://192.168.1.26:8001
  -> GPU model server http://192.168.1.26:8000/v1
```

The VLM wrapper runs through Docker Compose on the GPU server to avoid local Python/runtime drift and to keep request admission next to the one GPU:

```bash
ssh jupyter
cd /home/pawel/repos/vlm-screenshot-server
HOST_BIND=0.0.0.0 HOST_PORT=8001 PORT=8001 \
  VLM_BASE_URL=http://192.168.1.26:8000/v1 \
  VLM_MODEL=unsloth/gemma-4-26B-A4B-it-qat-GGUF \
  CHAT_PROXY_ENABLED=true \
  CHAT_PROXY_API_KEY=<strong-token> \
  QUEUE_MAX_SIZE=1000 QUEUE_WORKERS=1 QUEUE_BACKGROUND_WORKERS=1 \
  docker compose up -d --build screenshot-vlm
```

The container publishes `192.168.1.26:8001` and forwards to `http://192.168.1.26:8000/v1`. Docker Desktop on the Mac is not part of the healthy product path for the wrapper.

SSH is only the admin channel for deploying, restarting, and inspecting the GPU-hosted wrapper. Seraph runtime traffic must stay on direct HTTP API calls to `SERAPH_VLM_BASE_URL=http://192.168.1.26:8001`; do not encode SSH forwards, SOCKS proxies, or tunnels into Seraph config or status receipts.

Required readiness checks from the Mac:

```bash
cd /Users/bigcube/Desktop/repos/seraph
./manage.sh -e dev local status
curl http://127.0.0.1:8004/health
curl http://192.168.1.26:8001/health
curl http://192.168.1.26:8001/health/backend
curl http://192.168.1.26:8001/queue/status
set -a && source .env.dev && set +a
curl http://192.168.1.26:8001/health/chat \
  -H "Authorization: Bearer $SERAPH_VLM_API_KEY"
PYTHONPATH=backend backend/.venv/bin/python scripts/diagnose_gpu_vlm_route.py
```

Interpretation:

- `127.0.0.1:8004/health` proves Seraph backend is running.
- `192.168.1.26:8001/health` proves the Dockerized GPU VLM wrapper is reachable from the Mac.
- `192.168.1.26:8001/health/backend` proves the wrapper can reach the GPU model server at `192.168.1.26:8000/v1`.
- `192.168.1.26:8001/queue/status` proves Seraph can observe admission pressure before feeding screenshot work.
- `192.168.1.26:8001/health/chat` proves the chat proxy is enabled and accepts Seraph's configured bearer key without running inference or adding GPU queue work.
- The direct-route diagnostic also checks authenticated chat readiness. This catches `CHAT_PROXY_ENABLED=false`, missing `CHAT_PROXY_API_KEY`, and Seraph/wrapper key mismatches that ordinary health checks cannot see.
- A `502` from `/health/backend` means the wrapper is up but the GPU backend edge is broken.
- A `disabled`, `auth_not_configured`, or `auth_failed` result from `/health/chat` means screenshot analysis may still work but Seraph chat is not ready.

Run the direct-route receipt from the normal operator shell that starts Seraph
when Codex/Desktop reports a LAN failure. A normal Terminal-launched receipt on
July 3, 2026 proved `ssh -o BatchMode=yes -o ConnectTimeout=5 jupyter true`
exits `0` for this GPU host, even though Codex/Desktop-launched direct SSH can
report `No route to host` for the same alias. If Codex reports `No route to
host` or connection failures for `192.168.1.26` while the operator shell can
reach `jupyter`, record the Codex result as an agent-network limitation, not as
evidence that the product topology requires a tunnel.

Codex maintenance access is allowed to use `ssh jupyter` for GPU-host
administration. That route has confirmed host `jupyter`, user `pawel`, and the
GPU wrapper repo at `/home/pawel/repos/vlm-screenshot-server`. Use it for
inventory, Docker Compose checks, process inspection, listener checks, and log
reads. Do not use it as a Seraph runtime base URL or a passing direct-route
acceptance receipt.

Fast metadata endpoints must not block on live GPU route probes. `/api/runtime/status` and `/api/settings/artifact-storage` expose the configured and effective VLM runtime shape with `vlm_runtime.live_probe.checked=false` and `reason=deferred_fast_metadata`; that keeps cockpit and settings refreshes usable even when the LAN route is slow, down, or unreachable from the Codex process. The explicit route receipts remain the wrapper `/health`, `/health/backend`, `/queue/status`, authenticated `/health/chat`, and `scripts/diagnose_gpu_vlm_route.py` checks below. Diagnostic SSH forwards are still not a substitute for a passing direct `SERAPH_VLM_BASE_URL` route receipt.

`scripts/diagnose_gpu_vlm_route.py` is the operator-shell receipt command for the direct route. It reads `SERAPH_VLM_BASE_URL`, `SERAPH_VLM_API_KEY`, and `LOCAL_VLM_MODEL`/`LOCAL_MODEL`, prints sanitized JSON, and exits non-zero when the direct wrapper route, chat check, or requested image check fails. Loopback and localhost base URLs are rejected by default so a tunnel cannot accidentally pass as the direct-route receipt. Use `--allow-non-direct-base-url` only for explicitly labeled diagnostic bridge checks. Use `--image /path/to/screenshot.png` when the validation receipt also needs a wrapper-level `/v1/analyze-file` check.

For an RTX 3090 Ti 24 GB server, the current preferred Gemma-first target is Unsloth's Gemma 4 26B-A4B quantized GGUF/Dynamic 4-bit path. Unsloth's Gemma 4 docs list practical 4-bit memory footprints for this card class, including the 26B-A4B family in the high-teens GB range.

Example GPU-server setup with `llama.cpp`:

```bash
curl -LsSf https://llama.app/install.sh | sh

llama serve \
  -hf unsloth/gemma-4-26B-A4B-it-GGUF:UD-Q4_K_M \
  --host 0.0.0.0 \
  --port 8000 \
  --ctx-size 32768 \
  --reasoning off \
  --no-mmproj-offload
```

`--no-mmproj-offload` is currently the stable workaround for RTX 3090 Ti BF16 mmproj CUDA failures. Remove it only after a GPU-server/library change is verified with the profile-proof harness.

The GPU model backend exposes an OpenAI-compatible API on the GPU host at:

```text
http://192.168.1.26:8000/v1
```

Run the screenshot-analysis wrapper on the GPU server with Docker Compose. The
repo on the GPU host is `/home/pawel/repos/vlm-screenshot-server`; the wrapper
publishes `http://192.168.1.26:8001` and forwards to the local GPU model
backend above.

```bash
ssh jupyter
cd /home/pawel/repos/vlm-screenshot-server
cp .env.example .env
```

Use:

```env
HOST=0.0.0.0
PORT=8001
HOST_BIND=0.0.0.0
HOST_PORT=8001
VLM_BASE_URL=http://192.168.1.26:8000/v1
VLM_MODEL=unsloth/gemma-4-26B-A4B-it-qat-GGUF
VLM_API_KEY=
VLM_TIMEOUT_SECONDS=180
VLM_MAX_TOKENS=700
VLM_TEMPERATURE=0
REDACT_VISIBLE_TEXT=true
QUEUE_MAX_SIZE=1000
QUEUE_WORKERS=1
QUEUE_BACKGROUND_WORKERS=1
```

Then start the wrapper:

```bash
docker compose up -d --build
```

Test the wrapper and backend from the Mac/operator shell through direct HTTP
API calls:

```bash
curl http://192.168.1.26:8001/health
curl http://192.168.1.26:8001/health/backend
curl http://192.168.1.26:8001/queue/status
curl -F "file=@/path/to/screenshot.png" \
  http://192.168.1.26:8001/v1/analyze-file
```

Seraph-side first-class `local-vlm` wiring is available behind explicit settings:

```env
SCREEN_ANALYSIS_PROVIDER=local-vlm
SERAPH_VLM_MODE=gpu-server
SERAPH_VLM_BASE_URL=http://192.168.1.26:8001
SERAPH_VLM_BACKEND_URL=http://192.168.1.26:8000/v1
SERAPH_VLM_API_KEY=<same-token-as-wrapper-CHAT_PROXY_API_KEY>
SERAPH_VLM_FEEDER_WINDOW=2
LOCAL_VLM_MODEL=unsloth/gemma-4-26B-A4B-it-qat-GGUF
LOCAL_MODEL=openai/unsloth/gemma-4-26B-A4B-it-qat-GGUF
LOCAL_RUNTIME_PATHS=screenshot_observation_digest,end_of_day_goal_report,chat_agent,onboarding_agent,orchestrator_agent,strategist_agent,session_consolidation
RUNTIME_PROFILE_PREFERENCES="chat_agent=local-gemma-chat-thinking;onboarding_agent=local-gemma-chat-thinking;orchestrator_agent=local-gemma-chat-thinking;strategist_agent=local-gemma-strategist-fast;end_of_day_goal_report=local-gemma-report-thinking;screenshot_observation_digest=local-gemma-report-thinking"
SCREEN_DERIVED_LLM_ALLOW_REMOTE=false
SCREEN_DERIVED_LLM_REQUIRE_PROFILE_PROOF=true
```

Quote `RUNTIME_PROFILE_PREFERENCES` whenever it contains semicolons. The managed local launcher sources `.env.dev` as shell, so an unquoted value is split into partial shell assignments and Seraph can silently lose the `chat_agent` profile preference.

The production-like local topology is direct private LAN, not an SSH tunnel:

```text
Seraph backend     http://127.0.0.1:8004
  -> GPU VLM       http://192.168.1.26:8001
  -> llama.cpp     http://192.168.1.26:8000/v1
```

SSH forwarding is acceptable only as a diagnostic bridge for an agent sandbox that cannot open the LAN route. It is not the operator runtime contract, and Seraph status must not require or imply a tunnel.

The user should not have to set up tunnels to access Seraph's GPU VLM API. If a
tunnel is needed for a Codex diagnostic session, keep it out of `.env.dev`, do
not use it as a passing acceptance receipt, and prefer an operator-shell
`scripts/diagnose_gpu_vlm_route.py` receipt against
`http://192.168.1.26:8001`.

When Codex needs to administer the GPU host, use `ssh jupyter` for admin work
only. Keep that maintenance route labeled separately from the product topology
above.

When configured, `screenshot_folder_analysis` posts the screenshot image plus Seraph's strict analysis prompt to `/v1/analyze-file`, validates the returned JSON against `seraph.screenshot_analysis.v1`, and stores the privacy-safe semantic payload inside the existing Seraph `ScreenObservation`.
If the provider is not configured or fails, Seraph still keeps the screenshot metadata observation and records a bounded analyzer status instead of retrying the same image as a new screenshot.

## GPU/VLM Queue Architecture

The GPU path has two layers:

- Seraph owns observation persistence, pending-work selection, priority metadata, privacy constraints, and report consumption.
- The VLM wrapper owns request admission, priority-aware queuing, GPU worker slots, model invocation, and wrapper-level health.

The service contract is intentionally producer-neutral and model-neutral. The current verified target is local Gemma through an OpenAI-compatible gateway; if a future Gemini-backed service is used, it must honor the same Seraph request fields, status semantics, and privacy contract before it is considered equivalent.

Priority lanes sent by Seraph:

- `chat_thinking`: `interactive`; operator chat must preempt screenshot backlog.
- `report_thinking`: `high`; report and digest synthesis outrank bulk screenshot analysis.
- `screenshot_fast`: `normal`; screenshots are bounded background work that should run whenever higher-priority lanes are empty or have spare worker capacity.

The wrapper should implement a work-conserving queue:

1. Accept Seraph profile metadata from multipart form fields and `X-Seraph-*` headers.
2. Dispatch available GPU worker capacity to the highest-priority ready lane.
3. Never leave the GPU idle while accepted work exists, except during health failure, backoff, shutdown, or an explicit operator pause.
4. Preserve fairness so `normal` screenshot batches cannot starve `interactive` chat, while `interactive` bursts cannot permanently prevent older screenshot work from draining once the high-priority lane is empty.
5. Return bounded failures quickly enough for Seraph to mark an observation `failed` or leave it pending for explicit retry, rather than blocking the scheduler indefinitely.

Seraph-side controls:

- `SCREENSHOT_FOLDER_ANALYSIS_INTERVAL_SECONDS` controls how often Seraph checks for the next background screenshot job.
- `SCREENSHOT_FOLDER_ANALYSIS_LIMIT` caps the number of pending or retryable failed observations eligible for background analysis policy. The scheduled feeder clamps each live tick to the configured concurrency window, so a large backlog drains over repeated short ticks instead of one long scheduler-owned batch.
- `SCREENSHOT_FOLDER_ANALYSIS_CONCURRENCY` caps concurrent Seraph HTTP calls admitted into the wrapper feeder window, not concurrent GPU inference. In the one-GPU local topology the default is `2`, which means one active background request plus one queued background request. Priority is enforced at wrapper admission for the next job, never by interrupting the job already running on the GPU.
- `SERAPH_VLM_FEEDER_WINDOW` bounds how much work Seraph may keep active or queued in the wrapper. The default is `2`: enough to avoid GPU idle time between short screenshot jobs, but small enough that newly arrived interactive chat becomes the next accepted high-priority job after the current GPU job finishes.
- `SCREENSHOT_FOLDER_ANALYSIS_JOB_TIMEOUT_SECONDS` is the per-image base timeout. The scheduled job applies it to the small feeder batch selected for the current tick, preventing a locked SQLite write or hung wrapper call from leaving the scheduler permanently stuck.
- `GUARDIAN_STATE_TIMEOUT_SECONDS` bounds chat context assembly. If guardian/operator context is slow or degraded, chat falls back to a minimal agent context instead of leaving the operator stuck at "responding" before the model request is dispatched.
- `LOCAL_RUNTIME_CONTEXT_WINDOW_TOKENS` is Seraph's configured prompt budget for local Gemma-compatible chat backends. It must match the GPU server `--ctx-size` operationally; the current local target is `32768`.
- `LOCAL_RUNTIME_PROMPT_SAFETY_RATIO`, `LOCAL_RUNTIME_TOOL_RESERVE_TOKENS`, and `LOCAL_RUNTIME_MIN_SECTION_TOKENS` control deterministic prompt compaction for local runtime profiles. Seraph compacts guardian state, observer context, memories, active skills, and conversation history before creating the `ToolCallingAgent`, while preserving the fixed Seraph identity instructions. `FallbackLiteLLMModel.generate` and `completion_with_fallback_sync` also run a final profile-aware message compaction pass for local-profile targets, preserving the current user turn and reserving the effective output-token budget before LiteLLM sees the request. Local profile status exposes the configured context window, safety ratio, tool reserve, and prompt budget so operators can verify the runtime contract. This is the Seraph-side guardrail that prevents oversized local prompts from reaching the backend as raw `exceed_context_size_error`.
- `LOCAL_MODEL` must be set for the built-in `local-gemma-*` runtime profiles to register. Use the LiteLLM `openai/` prefix for this value because Seraph talks to the Docker wrapper through an OpenAI-compatible API. `SERAPH_VLM_BASE_URL` now supplies the OpenAI-compatible chat path as `${SERAPH_VLM_BASE_URL}/v1` when `LOCAL_LLM_API_BASE` is not set. Keep `LOCAL_VLM_MODEL` as the raw wrapper/backend model name. Without `LOCAL_MODEL`, `chat_agent=local-gemma-chat-thinking` cannot resolve and Seraph can fall back to the cloud default profile.
- Fresh profiles use `onboarding_agent` before normal chat. Configure `onboarding_agent=local-gemma-chat-thinking` alongside `chat_agent=local-gemma-chat-thinking`, or the first "Hello" from a new operator can still route through the cloud default while the normal chat profile is correctly registered.
- If delegation is enabled, chat uses `orchestrator_agent`, so `orchestrator_agent=local-gemma-chat-thinking` must also be configured. Otherwise the delegated chat surface can still route through the cloud default while the local chat profile is correctly registered.
- Scheduled strategist/proactive checks use `strategist_agent`, so `strategist_agent=local-gemma-strategist-fast` must be configured with the other local chat-style paths. The strategist decision path is a bounded direct JSON completion, not a multi-step tool-calling agent loop, because local Gemma can otherwise keep retrying parse-wobbly JSON as malformed tool calls. The strategist profile disables thinking so the JSON lands in `message.content` instead of being consumed as hidden reasoning.
- Lightweight onboarding and greeting-style local chat turns use a bounded direct completion path instead of the full `ToolCallingAgent` loop. The local Gemma backend is reliable for normal chat completions, but it is not safe to make every "Hello" exercise multi-step tool-call JSON parsing. Direct local chat is capped to 512 output tokens, uses local-only runtime routing, and leaves non-lightweight work on the normal agent/tool path.
- The analysis job checks VLM queue capacity before taking work, avoiding needless queue churn during outages or while a chat/report job is already active or queued.
- The analysis job uses an in-process lock so overlapping scheduler ticks cannot stampede the wrapper.
- The scan path never calls the VLM; it only creates pending observations and returns promptly.

Observability receipts:

- `screenshot_folder_ingest` records scanned, ingested, duplicate, and rejected counts.
- `screenshot_folder_analysis` records scanned, analyzed, failed, skipped, duration, and concurrency.
- artifact-storage status exposes observation count, analyzer status mix, backlog, failures, visual-run compression counts, DB lock retry/failure counters, and latest analyzed timestamp.
- local Gemma profile proof receipts verify that profile-specific request controls are accepted by the shared backend.

This means "GPU constantly working" is enforced as a joint contract: Seraph continuously feeds bounded pending representative work when the VLM is healthy, and the wrapper must keep GPU workers busy from its priority queue whenever accepted work exists. Seraph should not keep the GPU busy with visually duplicate screenshots when a cheap local comparison can preserve the elapsed time as one visual-state run.

## Local Gemma Profile Proof

Seraph ships a proof harness for local Gemma profile behavior:

```bash
PYTHONPATH=. WORKSPACE_DIR=/tmp/seraph-dev-data \
  uv run python ../scripts/verify_local_gemma_profiles.py \
  --base-url http://192.168.1.26:8001/v1 \
  --timeout-seconds 120
```

The harness verifies `screenshot_fast`, `report_thinking`, `chat_thinking`, and `strategist_fast` against the live OpenAI-compatible gateway, writes a sanitized JSON receipt under `local-runtime-profile-receipts`, and reports whether one backend is safe for profile routing.

Current verified receipt from 2026-07-01:

- path: `/private/tmp/seraph-dev-data/local-runtime-profile-receipts/20260701T163430Z-ac5f047522024ebe835824efc70c12d3.json`
- sha256: `6ea55a9fd469a9c97b1fd5554e1d585682527b2356bd6d559e3db75332b56974`
- profile contract sha256: `30387cf96b71c50e85dd0bb5f3a7d82b012879b7341be8b621cefdc52b30c51a`
- conclusion: `per_request_reasoning_control=verified`
- routing state: `safe_for_single_backend_profile_routing=true`
- notes: none

The current local wrapper normalizes Gemma channel markers for the `screenshot_fast` / `reasoning=off` path before Seraph parses or stores the response. The proof receipt verifies that callers do not receive visible reasoning markers after gateway normalization and that the shared local backend can serve the configured profile contract. It does not prove the backend performed no internal reasoning.

Seraph blocks screen-derived digest/report LLM calls unless the resolved runtime profile is the verified `local-gemma-report-thinking` profile and the latest proof receipt matches the configured local model, local base URL, and current Seraph profile contract hash. Generic local profiles such as `local` or `local-ollama` are not enough for screenshot-derived report text because they do not carry the verified Gemma profile request contract. When remote screen routing is not explicitly enabled, those calls also run in local-runtime-only mode so fallback/reroute candidates are filtered to local profiles and a local outage fails closed instead of trying a remote fallback. Operators may set `SCREEN_DERIVED_LLM_ALLOW_REMOTE=true` only when they intentionally want screenshot-derived text to leave the local runtime boundary.

Each screenshot observation carries Seraph-owned analysis status details:

- `pending` when the screenshot was ingested but no semantic provider was configured
- `succeeded` when a validated semantic analysis payload was stored
- `failed` when the provider call or schema validation failed
- `needs_reanalysis` when a stored semantic payload was produced by an older prompt, schema, or configured model

Duplicate screenshot files are still suppressed by image SHA-256, so the same image cannot accidentally create a second semantic observation.

Before VLM enqueue, Seraph also applies a conservative local visual-run dedupe gate against the current screenshot-folder representative. This gate computes a small grayscale fingerprint locally, compares only the new image against the current representative, and suppresses only extremely similar byte-different screenshots with matching dimensions and format. Suppression updates a `screenshot_visual_run` detail on the representative with `representative_path`, `first_seen`, `last_seen`, `suppressed_count`, `latest_suppressed_path`, and reason counts. The representative observation keeps the elapsed duration, so reports treat the interval as time spent in the same visual state rather than deleting it. A long unchanged run refreshes after the bounded dedupe window instead of suppressing forever, and manual reanalysis still targets the representative observation.
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

The digest is intentionally text-only and privacy-bounded. It never embeds raw screenshots, image hashes, full visible text, or provider transcripts. It is generated by the configured LLM from stored LLM screenshot analyses and carries the source observation ids needed for traceability.

Digest writes are idempotent per window and schema. If a window has not changed, Seraph keeps the existing episode. If new observations appear in the same window, Seraph updates the same episode instead of creating duplicates.

End-of-day reports consume these rolling digest episodes as the day-level screenshot evidence layer. The report builder passes digest text and goal context to the configured LLM runtime. Daily synthesis and critical goal comparison are LLM-authored; deterministic Python code may orchestrate, validate, persist, and deliver artifacts, but must not author the semantic report conclusions.

The final report stores digest count and source screenshot observation ids in report metadata for traceability. It still does not copy raw screenshots, image hashes, long visible text, or provider transcripts into the report.

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

Full screenshot intelligence loop receipt:

- local screenshot image file is scanned from the configured folder
- Seraph computes its own hash and mtime-derived capture timestamp
- Seraph runs semantic analysis through its own analyzer boundary
- duplicate image ingestion remains hash-based plus conservative current-representative visual-run suppression before VLM
- rolling digest stores redacted text and source observation ids
- end-of-day report consumes digest text and compares against active goals
- settings status shows observation, analyzer, backlog, failure, and digest counts
- no screenshot producer service, manifest, or sidecar is required

Focused receipt command:

```bash
cd /Users/bigcube/Desktop/repos/seraph/backend
UV_CACHE_DIR=/tmp/seraph-uv-cache uv run pytest tests/test_screenshot_intelligence_loop.py
```

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
