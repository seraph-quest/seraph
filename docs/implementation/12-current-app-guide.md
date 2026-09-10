---
slug: /current-app
title: Current App Guide
---

# Current App Guide

**Status:** Partial
**Scope:** current `develop` baseline; target changes in Epic #736 are not yet shipped

> **Branch-local Epic #736/#775 target:** the implementation branch narrows
> active inference to OpenRouter and removes the GPU/model-server/VLM wrapper
> prerequisite. The sections describing the current GPU topology below remain
> historical `develop` baseline until the reviewed migration PR lands. The
> target contract is defined by [ADR-006](./decisions/006-openrouter-only-inference-phase.md).

This is the short operator-facing description of the current application. For
the target product and locked decisions, read the
[Project Constitution](./00-project-constitution.md). For exhaustive shipped
detail, read [Development Status](./STATUS.md).

## Active branch topology (Epic #736/#775)

```text
Seraph frontend       http://127.0.0.1:3001
  -> Seraph backend   http://127.0.0.1:8004
  -> OpenRouter       https://openrouter.ai/api/v1
```

The backend and canonical workspace remain local on a CPU-capable host. All
active text, vision, and embedding inference is admitted through the governed
OpenRouter profile. A missing key, empty upstream allow-list, missing cloud
consent, absent budget, or unverified capability blocks the request and is
reported as configuration-required or degraded. Local model servers and the
VLM wrapper are retained only for historical diagnostics; they are not active
runtime prerequisites.

The production compose path keeps the backend on a private Docker network and
does not publish its API port. The managed direct local stack is a development
HTTP surface only; do not run `./manage.sh -e prod local up` or use it as a
production browser path because the production auth cookie is secure and
requires HTTPS. The production command is the private compose path:

```bash
python3 backend/production_preflight.py --env-file .env.prod --format json
./manage.sh -e prod identity
./manage.sh -e prod up -d
```

Expose the authenticated API and browser through a deployment-specific HTTPS
ingress. This slice does not provide that ingress or claim a live host
acceptance receipt; those deployment and identity edges remain separate work.

The CPU-host preflight reports core/auth/workspace readiness separately from
OpenRouter configuration. It performs no provider request and does not inspect
CUDA, model weights, a local model server, or the VLM wrapper. Missing
OpenRouter credentials or policy are visible as `configuration_required`; no
local fallback is selected. When run on the host, its
`canonical_workspace_mount` check is explicitly `deferred`: only the backend
container can read `/proc/self/mountinfo`. Compose sets the mount-check flag,
and the container preflight fails closed before startup when the bind evidence
is missing.

### Configure the OpenRouter route (branch-local #741)

Open the Settings panel's **OpenRouter setup** section to save the fixed
`https://openrouter.ai/api/v1` route without editing an environment file. Enter
one qualified `provider/model` ID (multiple model selection is rejected until a
governed selector exists), select the capabilities, choose the explicit
upstream allow-list, and set temperature, output-token, timeout, cloud-egress
acknowledgement, explicit deny data-retention policy, a positive finite spend
ceiling, and the bounded queue controls. These persisted controls are applied
to the active profile, caller cost/budget context, and remote admission lane
on every profile resolution; mutable legacy environment controls cannot
override them. Fallbacks are always
disabled; vision and embedding capabilities require zero-data-retention
acknowledgement.

The API-key field is write-only. A supplied key is stored through Seraph's
encrypted vault and the response exposes only `credential_configured` and a
short fingerprint plus the non-secret credential reference. Leaving the field
blank preserves the existing server-side reference and fingerprint. Credential
and configuration writes compensate a vault update if the configuration write
fails. The browser does not retain
the field, and saved configuration and status payloads contain no key value.
With no key, status is explicitly
`configuration_required` and the route is not silently usable. On restart the
backend hydrates a vault-backed credential before resolving the first route;
if the vault is unavailable or empty, the route remains blocked.

Saving and reading setup metadata never call OpenRouter. Capability proof is
available only through the explicit manual canary control, which is intentionally
omitted from keyless local tests. A real key and any canary remain operator-supplied
follow-up configuration.

The production backend's canonical workspace is the host path configured by
`BACKEND_DATA_PATH_PROD`, mounted only as `WORKSPACE_DIR=/app/data`. The managed
maintenance commands resolve that bind before doing any work and fail closed on
missing, symlinked, or ambiguous roots. Container preflight also requires
`SERAPH_PRODUCTION_MOUNT_SOURCE` to match the `/app/data` mount source reported
by `/proc/self/mountinfo`, and requires
`SERAPH_PRODUCTION_BIND_IDENTITY`. Generate the latter after the absolute
`BACKEND_DATA_PATH_PROD` directory exists; it binds the resolved configured
path to its device/inode identity so a different directory on the same device
cannot satisfy the container check. A writable directory or generic volume
alone is not treated as proof of the configured host bind:

```bash
./manage.sh -e prod backup
./manage.sh -e prod restore --archive <archive> --confirm
```

`./manage.sh -e prod identity` prints the redacted digest to place in
`SERAPH_PRODUCTION_BIND_IDENTITY`; it never prints the host path or secret
values. A successful managed `restore` or `rollback` returns the new digest in
`bind_identity_refresh`, and the next managed `./manage.sh -e prod up -d`
refreshes it automatically before Compose interpolation. Direct Compose users
must copy that receipt (or rerun `workspace_cli.py identity`) before starting
the container after an atomic root replacement.

Archives and restore staging are derived siblings of the host bind and are not
active workspace roots. Archives contain checksummed canonical files and
redacted secret metadata; recovery preserves the required vault key but drops
optional integration tokens. The production preflight requires dedicated
`/app/data` mount evidence from `/proc/self/mountinfo`, the configured source
identity match, and the configured path/device/inode identity match. The
backend holds
the same bind-local owner lock for its lifetime so backup and restore fail
closed while writers or the scheduler are active. Restore resets only empty
declared derived directories; a stored derived index that has no bounded
rebuild hook blocks promotion. Staged operator sessions and durable workflow
authority rows are revoked or blocked before promotion. The current slice
proves deterministic local inventory, archive validation, staged promotion,
rollback journaling, durable status receipts, and provider-independent
maintenance. Migration fencing beyond the backend owner lease,
retention/disk-pressure drills, and live operator receipts remain partial #742
acceptance work.

## Historical develop topology

The following topology describes the pre-#775 `develop` baseline and remains
in the repository as migration evidence. It is not an active route on this
branch and must not be used as setup guidance for the OpenRouter-only phase.

```text
Seraph frontend       http://127.0.0.1:3001
  -> Seraph backend   http://127.0.0.1:8004
  -> GPU VLM wrapper  http://192.168.1.26:8001
  -> GPU model server http://192.168.1.26:8000/v1
```

The accepted architecture decision places the core on the GPU host and uses a
paired Mac edge. Until their
migration tickets ship, the backend and frontend remain local and GPU services
are reached over documented HTTP APIs. `ssh jupyter` is an administrator path
for inventory and maintenance, not application transport or a required tunnel.

## Run The Current App

```bash
cp env.dev.example .env.dev
./manage.sh -e dev local up
./manage.sh -e dev local status
```

Open `http://127.0.0.1:3001` for the cockpit and
`http://127.0.0.1:8004/docs` for API documentation. Stop with
`./manage.sh -e dev local down`.

In a managed development shell, use `./manage.sh -e dev local run` and keep the
foreground session open while verifying live behavior. Direct `uvicorn`, Vite,
or detached child-process launches are not the supported lifecycle path.

Useful probes:

```bash
./manage.sh -e dev local status
curl -sS http://127.0.0.1:8004/health
curl -sS http://127.0.0.1:8004/api/runtime/status
curl -sS http://127.0.0.1:8004/api/settings/artifact-storage
```

For a production compose configuration, inspect the same receipt before
starting the backend:

```bash
python3 backend/production_preflight.py --env-file .env.prod --format json
./manage.sh -e prod up -d
./manage.sh -e prod health --format json
```

`/health` is the core process check. `/api/runtime/status` is the authenticated
runtime receipt and may report OpenRouter `configuration_required` while the
local core remains healthy. A live provider or host receipt requires a separate
operator-approved probe and is outside this offline launch check.

The managed health command is a read-only, keyless release-gate collector. It
writes a redacted JSON receipt to the canonical workspace under
`operator-receipts/epic-736-health/` and reports stable exit codes. It checks
OpenRouter policy, remote admission, durable/guardian/security/memory/evolution
source contracts, and the absence of configured local inference routes. It does
not call OpenRouter, a GPU/VLM service, Whisper, Piper, or a connector. Live
provider, embedding, edge, voice, and Telegram evidence is reported as
`skipped`/`degraded` until a separately authorised canary supplies a receipt;
those omissions never become a superiority claim.

Production API access is authenticated with a server-side, single-operator
session. Generate a PBKDF2 password hash in the backend environment, store it
in the deployment secret store as `OPERATOR_AUTH_SECRET_HASH`, and set
`DEPLOYMENT_ENVIRONMENT=production`, `OPERATOR_AUTH_COOKIE_SECURE=true`, and
the exact operator host/origin allow-lists. The raw password and session cookie
must never be placed in browser configuration or logs. A manual provisioning
and login receipt is:

```bash
cd backend
uv run python -c 'from src.auth.service import encode_secret; print(encode_secret("REPLACE_ME"))'
curl -c /tmp/seraph.cookies -H 'Origin: https://cockpit.example' \
  -H 'Content-Type: application/json' \
  --data '{"password":"REPLACE_ME"}' \
  https://api.example/api/auth/login
curl -b /tmp/seraph.cookies -H 'Origin: https://cockpit.example' \
  https://api.example/api/auth/session
```

The login/session/refresh/logout endpoints are the canonical provisioning
surface for the current single-operator deployment. Host headers with ports
such as `127.0.0.1:8004` are normalized against the configured allow-list.
Unauthenticated API requests fail closed; the explicit unauthenticated bypass
is accepted only by test configuration. A WebSocket rechecks the session at a
bounded interval and closes when the session is revoked or expires. A cockpit
login form and multi-operator identity ownership remain #741 follow-up scope.

`/api/runtime/status` and `/api/settings/artifact-storage` are the active
operator receipts. They expose the effective OpenRouter route, consent,
allow-list, budget, admission state, and disabled local-runtime reason. A
configured key or model is not evidence that a live provider route is healthy.

## Current Capability Summary

**Shipped foundations:** the browser cockpit, FastAPI backend, conversation,
goals, memory and guardian state, settings, scheduler jobs, approvals, activity
and audit views, tools and workflows, artifacts, reports, extension adapters,
screen-observation storage, and VLM integration points exist on `develop`.

**Partial:** long-horizon guardian behavior, durable execution, isolation,
cross-surface identity, selected voice/messaging channels, and outcome-first UX
need Epic #736 milestones. Existing canaries or deterministic receipts do not
make those product capabilities complete.

## Models And Runtime

The branch-local #775 target changes the active contract to OpenRouter-only
inference. The UI and `/api/runtime/status` report the effective gateway,
model, verified-or-unknown upstream, consent, budget, and degraded state; a
configured key or model is not proof of a live route.

> **Branch-local #740 target, not shipped `develop` truth:** the model-fabric
> settings, canary, proof, receipt, and runtime-path status surfaces below remain
> intended behavior until the reviewed epic integration PR lands.

The pre-#775 GPU topology had three distinct inference transports:

- `LOCAL_LLM_API_BASE=http://192.168.1.26:8000/v1` is the chosen direct GPU text
  endpoint for Seraph text workloads;
- `SERAPH_VLM_BASE_URL=http://192.168.1.26:8001` also exposes an optional
  OpenAI-compatible text chat proxy under `/v1`;
- that same wrapper performs screenshot vision through `/v1/analyze-file`.

They share the model-fabric selection, trust, proof, and receipt vocabulary but
remain separate transport adapters. `/api/settings/model-fabric` reports
configured profiles and policies. The `model_fabric` field in
`/api/runtime/status` reports
selected and attempted routes, the last actual successful route, fallback,
degradation, receipt persistence, and fresh/stale/missing capability proof.
Settings retains last-known metadata if a refresh fails and labels it `STALE`.

Capability canaries are intended to run only from an explicit authenticated operator action through
`POST /api/settings/model-fabric/canary`. Each canary targets one exact profile,
capability, endpoint/model/adapter binding with a bounded deadline and no
fallback. Status and settings reads never launch inference.

**Integration dependency:** the fabric does not synthesize operator identity.
On the #740 epic branch, REST/WebSocket chat remains fail-closed with zero model
transport when ingress has not bound an authenticated principal. Issue #741
owns binding LAN/operator identity and making that interactive path functional;
#740 supplies the governed inference route but does not weaken identity checks.

Named profiles such as `codex-openai` and `claude-anthropic` are API model-route
names only. They do not invoke Codex CLI, Claude Code, or another external agent
runtime. Seraph owns planning, capabilities, tools, approvals, and receipts.

## Screen Awareness And Reports

**Shipped foundation:** the observer can ingest permitted context, store screen
artifacts, route screenshot analysis through the governed OpenRouter vision
adapter when cloud consent and capability configuration are present, and feed
report infrastructure. Capture, analysis, and report synthesis are separate
stages and expose separate failures.

**Planned:** a paired, revocable Mac edge supplies observation and native
interaction to the GPU core. Pairing must not implicitly authorize execution or
data egress.

## Memory

**Shipped foundation:** Seraph owns canonical local memory and can augment
retrieval through guarded provider integrations.

On this branch, remote embedding is a separately admitted OpenRouter
capability. Memory vector writes and vector search require an explicit
`embedding_model=openrouter/...` configuration plus a persisted exact-profile
cost record and fresh embedding health/latency proofs. If any of those inputs
is absent or stale, the vector path remains blocked and the caller must use the
clearly labelled lexical/degraded path; it must not recreate the historical
local 384-dimensional index or silently fall back to another provider. Existing
un-namespaced local vectors are retained as migration evidence and require a
tracked rebuild from canonical memory before they can be used again.

The accepted memory-boundary decision requires goals, approved facts, jobs, artifacts, checkpoints,
approvals, and audit records stay canonical in Seraph-owned storage. Graph or
external memory systems may be benchmarked only as advisory providers with
provenance, conflict, deletion, export, and failure handling.

## Actions, Workflows, And Extensions

**Shipped foundation:** tools, workflows, skills, runbooks, starter packs, MCP
integrations, connectors, approvals, activity receipts, workflow history, and
extension governance exist in the current codebase.

**Partial:** breadth and deterministic receipts are not equivalent to a fully
durable or isolated runtime. The target gives every capability typed
inputs/outputs, permissions, limits, checkpoints, artifacts, and visible
receipts without depending on an external coding-agent runtime.

The earlier command-backed runtime direction from
[#613](https://github.com/seraph-quest/seraph/issues/613) and
[#615](https://github.com/seraph-quest/seraph/issues/615) is explicitly reversed
by Epic #736 and removal issue #739. Those tickets remain historical evidence,
not current setup guidance.

## Remote inference admission

The branch-local #775 target uses one shared bounded `remote_inference`
admission lane: the active request finishes, then the highest-priority ready
request runs. Interactive work outranks scheduled and background work. Queue,
consent, cancellation, and uncertain remote outcomes remain operator-visible.
The current process-local lane does not yet provide durable queue persistence or
provider cost reservation/reconciliation; those limits remain tracked by
#743/#744 and are not silently treated as complete.

The typed durable job contract persists a monotonic row revision alongside its
owner fencing token. Claim, heartbeat, expired-lease transfer, and terminal
transitions are compare-and-swap writes against the expected state, revision,
and fence. Lease-bound receipt and transition writes also check the expiry in
the database update predicate. Startup recovery runs before scheduler
registration and marks work with an unresolved effect or provider cost as
`unknown_external_effect` or `cost_liability`; an operator must record a typed
effect-specific destination readback or cost settlement before the job can be
requeued. Failed jobs with an unresolved effect use the same exact-effect
reconciliation path, while deadline-expired or attempt-exhausted retries are
rejected. Corrupt or missing effect history on an effect-bound failure blocks
retry. A concurrent duplicate admission returns the original durable row after
the unique idempotency fence, and the legacy projection cannot reset or
finalize a typed job. This branch-local slice remains Partial until its tracked
review and integration receipts land on `develop`.

## Failure And Recovery

- Trust effective API/UI state, not a default provider label.
- Keep settings usable through partial metadata failure and show last-known state.
- Treat Codex/Desktop LAN failures as an environment limitation when an
  operator-shell receipt proves the route; do not redesign around an SSH tunnel.
- Never describe branch-local or target behavior as Shipped before merge and
  validation on `develop`.

## Next Reading

- [Project Constitution](./00-project-constitution.md)
- [Development Status](./STATUS.md)
- [Documentation Contract](./08-docs-contract.md)
- [ADR-004: GPU Core And Paired Mac Edge](./decisions/004-gpu-core-mac-edge-topology.md)
- [ADR-006: OpenRouter-only inference phase](./decisions/006-openrouter-only-inference-phase.md)
