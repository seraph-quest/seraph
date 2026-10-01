---
slug: /current-app
title: Current App Guide
---

# Current App Guide

**Status:** Partial
**Scope:** current `develop` baseline plus the accepted Epic #736/#775 target;
open integration branches describe intended post-merge truth and label any
remaining partial boundaries inline

> **Epic #736/#775 OpenRouter phase:** the accepted active inference contract
> routes text, vision, and embedding work through the governed OpenRouter path
> and removes the GPU/model-server/VLM wrapper prerequisite. The historical GPU
> topology below remains documented as pre-#775 `develop` evidence and rollback
> diagnostics. On an open integration branch, the final reviewed Epic PR is the
> merge gate; after it lands, the active sections are shipped `develop` truth.
> The target contract is defined by [ADR-006](./decisions/006-openrouter-only-inference-phase.md).

This is the short operator-facing description of the current application. For
the target product and locked decisions, read the
[Project Constitution](./00-project-constitution.md). For exhaustive shipped
detail, read [Development Status](./STATUS.md).

## OpenRouter topology (Epic #736/#775)

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

The keyless implementation and health paths do not call OpenRouter or any local
GPU/VLM service. Deterministic tests, intercepted transport tests, static
configuration checks, and negative boundary checks are sufficient for the Epic
merge gate. Live provider quality, embedding, edge, voice, and Telegram
receipts are optional operator evidence and remain explicitly unverified until
a separately authorised canary supplies them.

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

### Configure the OpenRouter route (Epic #736/#775)

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

### Local cockpit interaction boundaries

The managed development cockpit may be opened over local HTTP for text and
operator controls. Browser microphone capture follows the browser secure
context rule: `http://localhost` is eligible, while a LAN HTTP origin such as
`http://192.168.1.26:3001` cannot request microphone permission. Seraph states
that limitation directly in the push-to-talk control. The two consent grants
remain available before a conversation exists, but recording stays disabled
until a session can own the durable audio request. Audio processing may still
show a governed degraded state when no audio transport is configured.

Onboarding presents an explicit **Skip onboarding** action in the cockpit. It
uses the live WebSocket when available and the authenticated REST profile
endpoint while the socket reconnects; the UI only reports success after one of
those paths accepts the update. If no compliant OpenRouter route is persisted,
chat reports that it was blocked before provider contact and tells the operator
which setup controls are missing. That state does not imply an uncertain
provider outcome and does not trigger an automatic retry.

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
closed while writers or the scheduler are active; lifecycle code hard-links
that lock inode into the staged generation before promotion, so the fence
survives the directory rename. Backup and restore reject a source manifest
that omits any declared required canonical path, and archives enforce both
per-member and cumulative uncompressed payload bounds. Public lifecycle
helpers require the production maintenance-fence context. Restore resets only empty
declared derived directories; a stored derived index that has no bounded
rebuild hook blocks promotion. Staged operator sessions and durable workflow
authority rows are revoked or blocked before promotion. The current slice
proves deterministic local inventory, archive validation, staged promotion,
rollback journaling, durable status receipts, and provider-independent
maintenance. The owner fence is exercised by a competing subprocess, and
restore journals bind the active, staged, previous, and promoted roots to
device/inode identity; status also blocks when the current root no longer
matches the last successful bind identity. Root replacement, symlink endpoints, archive traversal,
special-file members, and promotion disk errors fail closed while retaining a
recoverable journal. Rollback carries tombstones, revocations, configuration
history, unresolved cost liabilities, and existing target safety rows without
overwriting newer values, alongside session invalidation and workflow
authority blocking into the retained generation. The checked-in drill is an
isolated temporary workspace; it does not prove a live production-data restore,
retention/disk-pressure capacity, or an interrupted CLI recovery when the
active root is absent.

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

For a private LAN cockpit, replace the production host and origin allow-lists
with the exact LAN hostname/IP and browser origin (for example,
`seraph.lan,192.168.1.50` and `https://seraph.lan`). The same boundary is
enforced for unsafe HTTP requests and WebSocket upgrades. An authenticated
operator may use model-fabric setup and canaries from that configured LAN
boundary; loopback is only the unauthenticated local-development shortcut.

The login/session/refresh/logout endpoints are the canonical provisioning
surface for the current single-operator deployment. Host headers with ports
such as `127.0.0.1:8004` are normalized against the configured allow-list.
Unauthenticated API requests fail closed; the explicit unauthenticated bypass
is accepted only by test configuration. A WebSocket rechecks the session at a
bounded interval and closes when the session is revoked or expires. The cockpit
now gates protected UI startup on `/api/auth/session`, provides a login form,
and returns to login after a revoked or expired session. When no server-side
credential is configured it shows the PBKDF2 provisioning command and keeps the
core locked until the managed backend is restarted. Browser API requests use
credentialed cookies across the local frontend/backend ports; raw passwords and
session tokens never enter frontend state. Multi-operator identity ownership
remains outside the current single-operator boundary.

On the #895 corrective branch, a refresh rotates only the bearer hash while
retaining the active `OperatorSession.id` and absolute expiry. The retired hash
is stored in a revoked tombstone and cannot authenticate or become an owner.
Login, session, and successful refresh receipts expose
`ownership_continuity=stable` or `legacy_rebind_required` plus the explicit
`ownership_recovery_action`. A stable value means the same active owner remains
current across bearer refresh; logout, idle/absolute expiry, and a new
independent login still revoke or isolate the prior scope under the existing
contract. Existing pre-corrective replacement rows are reported as blocked
recovery: historical grants and approvals are not restored, and the cockpit
keeps an accessible notice directing the operator to review and recreate work
in the current scope. A separate audited recovery design is required before
historical scopes can be migrated.

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

### Operator work board (Epic #864 M6 branch-local target)

The M4 work-board slice adds an authenticated, single-operator Kanban surface
under `/api/work-board`. Seraph's SQLModel/SQLite workspace remains the
canonical task store; `WorkflowRunState` remains authoritative for execution,
leases, attempts, effects, checkpoints, and unknown external outcomes. A task
card can therefore show `Triage`, `Todo`, `Ready`, `Running`, `Blocked`,
`Review`, `Done`, or `Archived` while its durable workflow receipt remains the
execution evidence.

Review and handoff actions require a current owner session, the latest fenced
attempt, and an independently verified readback containing a stable artifact or
readback identity plus the producer's verification timestamp. A generic worker
summary cannot complete a card. Expired review cards require the named
reviewer's renewal path; unknown external effects require reconciliation and
are never replayed automatically. Manual Specify and Decompose requests are
provider-governed proposals that remain staged until operator acceptance; a
missing route, authority, or budget is visible as a blocked recovery state.

Epic #864 M5 adds a candidate from a verified task outcome only after
independent readback. The candidate keeps its task, attempt, workflow, artifact,
goal revision, content digest, provenance, confidence, and supersession evidence.
Accept, edit and accept, reject, and rollback are operator actions; only an
accepted candidate writes canonical memory. Failed, weak, unverified, or
irrelevant outcomes record `no_learning`.

Epic #864 M6 can draft a versioned declarative procedure from an operator-
selected, verified research task and its linked verified follow-through task.
The preview binds the source tasks, attempts, artifacts, readbacks, owner,
session, goal revision, and fixed capability steps. Operator acceptance and the
existing capability-pack review and activation lifecycle are required before
reuse. Each invocation gets fresh goal revision, grants, approvals, budget,
tasks, jobs, and readback. If a run needs publication, the operator inspects an
exact same-card preview, approves it through Pending approvals, and resumes the
same durable routine parent. The card reaches Done only after independent
readback.

This section describes the Epic #864 M6 integration-branch target while its
aggregate PR is under review. It does not claim full Hermes parity, autonomous
execution, memory superiority, or production readiness.

### Reviewed procedures v2 (M6 #889 branch-local target)

The #889 branch-local target turns verified Work Board outcomes into reusable,
owner-bound procedures. It has exactly three registered templates:

- `public-browser-check`: one `browser.public-task.v1` leaf;
- `watch-and-public-browser`: `guardian.research-watch.v1` followed by
  `browser.public-task.v1`, with a current watch revision and material-change
  gate; and
- `selected-meeting-prep`: one `calendar.meeting-prep.v1` leaf using the
  existing M5 calendar input contract.

Every plan is schema v2, uses the registered step IDs, capability IDs and
versions, verifies native leaf readbacks, and is limited to two steps and 300
seconds. A preview records the selected task, attempt, job, artifact digest,
readback, owner/session, goal revision, typed input reference and digest. The
browser input needed for later execution is copied into the immutable
server-owned version; routine list/detail responses expose only safe provenance
and digests, never the copied input body.

Preparation creates an exact routine parent, immutable version, install job,
and owner/session-bound approval. The install approval expires after five
minutes. Installation remains blocked until the server confirms that exact
approval as current and approved. A pending approval routes the operator to
the existing Pending approvals review surface; missing, expired, denied, or
stale approval metadata asks for a fresh preview/rebind. A consumed approval
keeps its exact receipt addressable for reconciliation. Package preview,
operator review, activation approval and package activation remain explicit
controls. Activating the package does not activate the procedure: the Library
requires a separate **Activate procedure** action and a current server
readback before enabling invocation or scheduling. A paused procedure uses
**Resume procedure** under the same current package and version checks.
Unconfirmed activation requires an authority refresh before another lifecycle
mutation. Pause, revoke and selected-version rollback remain explicit controls.
No arbitrary approval ID or automatic activation is accepted.

Invocation is manual and requires a fresh active goal revision, current
owner/session authority, current grants and budgets, exact template
parameters, and a new task/input artifact. Public and watch procedures can
also create a finite governed schedule; meeting preparation remains a manual
selected-event invocation. A schedule is bounded by the seven-day procedure
limit and the earlier reviewed goal-budget expiry, uses the existing governed
schedule controls, and can be paused, resumed, or revoked explicitly. Quiet
hours, proactive consent, finite period, outstanding-job, attempt, runtime,
and notification budgets remain admission fences; a quiet-hours or budget
refusal is visible as deferred or blocked work rather than a hidden retry.
Each occurrence also checks its pinned procedure revision, version, plan and
current package lifecycle before publishing a task. A paused, revoked or
changed procedure requires explicit review; the scheduler does not silently
rebind its accepted schedule to newer authority.

All routine, approval, invocation, schedule, and recovery state is bound to
the authenticated owner session. Changing owner/session clears the old
selection and metadata state, and late responses cannot populate the new
session. Unknown or ambiguous mutations retain their bounded exact key and
body for explicit reconciliation/retry; they are never replayed automatically.
Definitive pre-effect rejection can ask for a fresh preview. Native leaf
readbacks are required before parent success, and every terminal result carries
an explicit `no_learning` outcome. Failed, blocked, revoked, expired, and
unknown cleanup states remain operator-visible and retain their recovery
boundary.
Calendar steps recheck the current procedure parent before each provider or
model boundary. Parent cancellation, lease reclaim, expiry and package
revocation cannot be replaced by a child's stored lineage fields.

The v2 procedure surface is designed for the CPU-host contract. The cockpit,
canonical state, artifact metadata, and fixed browser/calendar controls remain
usable when optional local model, GPU, VLM, connector, or provider services are
absent. Governed model work, where the selected meeting path requires it, still
uses the active OpenRouter admission, consent, and budget checks; this branch
has not performed a live provider or account canary.

This is a branch-local target, not Shipped `develop` truth. On October 1, 2026,
the managed CPU-host journey prepared, reviewed, installed and activated a
public-browser procedure, then executed its native leaf and verified both the
parent and leaf readbacks. The leaf artifact hash matched the stored file.
Twelve native vertical tests additionally exercised actual SQLite, Chromium,
package files and intercepted Calendar/model boundaries. These are mechanical
execution receipts; Calendar account/model usefulness and production readiness
remain unverified. Fresh independent reviews accepted preparation recovery,
interface authority/recovery and the dispatcher compatibility corrections.
After the managed backend restart, a new public-browser invocation completed
with parent and leaf readbacks, a matching artifact hash and explicit
`no_learning`. A separate live response-loss check retained the lifecycle
recovery gate across reload, blocked fresh preparation, and cleared it only
after an explicit exact authority refresh before resume. The implementation
PR carries the branch-specific review and validation receipts.

### Bounded public browser tasks

The integration target adds `browser.public-task.v1` through the existing Work
Board dispatcher and durable job repository. Its form creates an owner-bound,
immutable input artifact before creating a Todo task; the operator does not
type a workspace path or digest. Artifact metadata reads return identity,
digest, expiry, and lifecycle state, never the input body. Creation and task
binding have separate idempotency keys, and an uncertain response requires
explicit reconciliation or an exact retry rather than an automatic POST.

The grammar permits HTTPS navigation and bounded DOM extraction only. Exact
host and URL-prefix consent narrows the configured global site policy; query
strings are part of that consent. Every request checks all resolved addresses
and connects to a checked global address with the original Host and TLS name.
Redirects require fresh consent and DNS checks. The ephemeral browser disables
JavaScript and service workers and rejects authentication, cookies, popups,
downloads, uploads, forms, and arbitrary scripts. An allowed public GET can
still have site-specific effects; finite consent does not prove universal
absence of mutation.

Each response is limited to 256 KiB. The transport requests identity encoding
and rejects compressed responses before decoding. A site that needs scripts,
authentication, or a compressed response can therefore be unavailable to this
capability even when it works in the operator's ordinary browser.

One browser-task context occupies the cross-process task lane. At most eight
browser tasks can be Ready globally. Each task has at most eight actions,
eight navigations including the initial page, 32 requests, 64 KiB of serialized
output, and 180 seconds, further narrowed by the current goal budget. This
lane uses no model inference and does not acquire the remote-inference lane.
Blocked resource and method callbacks share the 32-receipt progress limit.
Overflow stops further checkpoints and prevents a successful artifact; blocked
resources remain aborted and do not count as dispatched network effects.
Missing browser prerequisites block execution while the CPU cockpit, task
creation, and evidence inspection remain usable.

A blocked durable job still occupies its goal's outstanding-work budget until
it reaches a terminal state. A known admission-budget refusal reports
`goal_budget_outstanding_limit` before browser launch and releases the browser
lane; it does not authorize raising the goal budget or replaying the blocked
job. A malformed or expired deadline before launch records explicit
no-context cleanup. Once launch is entered, unverified cleanup remains unknown.

Execution uses one deadline and reserves time for teardown. Awaited browser
operations use the remaining budget. A stalled operating-system file operation
can outlast this cooperative deadline; late work cannot publish success, and
unverified cleanup holds the browser lane for explicit recovery.

Current task, attempt, goal, session, artifact, and durable lease authority are
rechecked before actions and requests. Success requires expected checks,
artifact readback, and explicit `no_learning`. Cancellation records context
cleanup. Unverified cleanup retains the browser resource and shows
`browser_cleanup_required`; later browser tasks remain unclaimed. Recovery
requires outcome reconciliation and a managed backend restart after checking
cleanup. Restart releases the process-owned resource; it does not authorize
replaying an unknown job. An ambiguous dispatched request or process restart
requires outcome reconciliation and is never blindly replayed. Work Board
status remains separate from the durable execution status; last-known metadata is not a
readiness or success receipt.

Opening a browser result in the existing artifact inspector requests an explicit
owner-bound preview. The backend rereads only the job's canonical result file,
checks its size and digest against the artifact and readback, and returns typed
extracts and checks. The inspector renders the extracted text as plain text.
Routine job and task reads remain metadata-only; a missing or altered result
shows an unavailable preview rather than unchecked file content.

This is an intended post-epic contract, not Shipped `develop` behavior. The
milestone PR owns actual browser, transport, API, interface, and recovery
validation. Local fixture execution and production-network evidence must be
identified separately; authenticated browsing and general computer use remain
outside this capability.

### Bounded calendar meeting preparation

This is an intended post-epic contract, not Shipped `develop` behavior. The
reviewed milestone and epic PRs own implementation validation. Provider-free
tests establish local mechanics; a working Google account and live model
usefulness remain explicitly external-unverified without an authorized canary.

Calendar settings accept an operator-supplied OAuth client and refresh token as
write-only fields stored in the encrypted vault. Explicit verification obtains
a bounded calendar list. Saving setup does not grant event access or model
egress, and reading verified setup metadata does not repeat a provider call.
An interrupted setup retains its original key. Retries during the 30-second
preparation window preserve the pending result; a later retry reconciles stale
vault material without storing a second credential. Missing material becomes
blocked; unverified cleanup remains visibly blocked for reconciliation.
Event consent binds the authenticated owner/session, an active goal, one
verified calendar, selected fields, a finite window, an event limit, and expiry.
Remote preparation requires an explicit model-egress choice.

Work offers a meeting-preparation form using verified calendars and redacted
events. Selecting an event creates an immutable typed input and a Todo task;
the existing task controls govern admission and execution. Preparation rereads
the selected event, synthesizes one bounded brief through the governed
strategist route, and rereads the event again. Changed or revoked authority
blocks publication. Completion requires artifact readback and an explicit
`no_learning` receipt. Work shows the actual route and outcome.

An optional finite schedule observes the consented calendar without making a
model call. It creates preparation tasks for operator review, coalesces missed
slots, and deduplicates unchanged event revisions. Pause and revoke are explicit
controls. The current schedule lasts at most 24 hours, with earlier consent,
goal, or input expiry tightening that limit. The form shows the effective bound;
requests beyond it are refused rather than failing later without explanation.
An uncertain previous read holds the observation lane until server
reconciliation; an expired lease alone cannot free it. A refreshed list has its
own read digest, while an unchanged event retains the selection provenance
pinned by its existing task.

Settings shows the latest scan outcome separately from the schedule's state.
The server can settle a failed scan as blocked after proving that its actual
read transport has closed, without claiming that the scan succeeded. This
cleanup can finish after consent or session revocation; another scan still
requires current authority. A failed close or a crash without settlement proof
keeps the lane quarantined. Calendar read cleanup cannot settle model charges
or replay the old scan.
Cancellation finishes the scheduler run receipt while preserving any unknown
occurrence. Revocation during proposal publication prevents a new task and
tombstones its unbound input; uncertain artifact cleanup requires recovery.

Unknown request outcomes retain their exact key and body. Closing and reopening
Settings preserves credential-free controls only for the same authenticated
owner/session. Setup credentials stay in component memory and are cleared on
unmount or authentication failure. A deliberate new attempt requires a
confirmed stale refusal or a reconciled known result; refreshing metadata alone
cannot authorize duplicate work.

The existing artifact inspector renders a verified brief as plain text.
Missing, altered, revoked, or expired evidence leaves metadata and recovery
visible without exposing an unchecked brief. Routine metadata reads do not
load provider event bodies or brief content. This capability does not modify
calendar events, send communications, or learn preferences from meeting content.

### Reviewed source-change follow-up

Goals expose their success criterion, finite proactive budget, quiet hours, and
review boundary. Source watches offer hourly, six-hourly, or daily cadence and
retain per-run approval by default. A verified material change appears in the
Guardian intervention inbox with local evidence, freshness, expiry, and recovery
state. Accepting it creates one goal-linked **Triage** task in the existing Work
Board; it does not execute that task or grant additional permissions. Snooze and
dismiss persist without learning. Zero notification allowance keeps the inbox
usable without sending a message.

See [Guardian Intelligence](./05-guardian-intelligence.md#reviewed-goals-and-the-source-change-inbox)
for ownership, readback, idempotency, and bounded recovery contracts. Local
source execution and live external/provider usefulness have separate evidence
boundaries. Until integration, the open PR owns branch validation truth.

## Models And Runtime

The accepted #775 phase changes the active contract to OpenRouter-only
inference. The UI and `/api/runtime/status` report the effective gateway,
model, verified-or-unknown upstream, consent, budget, and degraded state; a
configured key or model is not proof of a live route. Model-fabric settings,
canaries, proof, receipt, and runtime-path status surfaces are explicit
operator controls. Their local deterministic and intercepted-transport checks
do not require a provider key or network call; a live canary remains an
operator-authorised optional check.

The transport detail below documents the pre-#775 GPU topology for historical
evidence and rollback diagnosis. It is not an active setup path:

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

**Identity dependency:** the fabric does not synthesize operator identity.
REST/WebSocket chat remains fail-closed with zero model transport when ingress
has not bound an authenticated principal. The completed model-fabric and
authenticated-ingress milestones preserve that boundary; governed inference
does not weaken identity checks.

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

### Cockpit navigation and canonical inspector

The integration target provides six persistent cockpit sections: **Home**,
**Inbox**, **Work**, **Goals**, **Library**, and **Connections**. These select
existing cockpit surfaces and retain the conversation, selected task, and
window layout. Home is a bounded operational summary, not a second scheduler:
page counts are labelled as such, failed refreshes preserve last-known data,
and unavailable queue or spend evidence remains unavailable. Pending approvals
open the existing approval surface.

Connections labels Seraph presence as unknown until a complete continuity
payload has been confirmed. A failed refresh retains previously confirmed
values with explicit stale and last-confirmed labels. Missing metadata does
not establish a clear queue, ready reach, or active proactive guidance; the
operator can continue using Work while continuity metadata is unavailable.

Inbox has one active owner for the candidate list and selected inspector.
Selected detail reads at most 20 append-only action receipts, with explicit
history truncation. New optional action reasons are bounded and server-redacted;
legacy receipts show unavailable reasons rather than invented explanations.
The receipt projection preserves action response and exact replay semantics.
Last-known evidence remains readable during a failed refresh, while actions
require current confirmed detail. Page-scoped filters do not claim global
search. Accepting a follow-up opens its existing Triage task for review; it does
not authorize execution. Work shows the Inbox origin only after an
authenticated candidate lookup confirms the exact accepted task relation;
the origin link returns to that decision. Inbox owns disposition controls and
the adjacent evidence inspector remains read-only.

Library reads canonical records through authenticated, owner-session-scoped
`GET /api/memory/records` and `GET /api/memory/records/{id}`. Search is literal
SQL text matching with bounded pagination; it performs no embedding or model
request. The list is metadata-first. Selecting a record loads redacted content,
bounded provenance, source/conflict state, and authorized task, artifact,
readback, and audit references. Explicit history can inspect superseded or
archived records; tombstoned, ownerless, and foreign-session records are not
discoverable. This session boundary does not establish cross-login identity
continuity. Task-linked artifact and readback references open the existing
Work evidence route. Artifact inspection requires the authenticated job,
parent lineage, identity and digest; a parent readback effect is matched by
its exact effect and content digests rather than a fabricated artifact handle.
Evidence-load failures appear in Work with a bounded explanation and retry
guidance, including when the advanced operator pane is closed.

Ordinary correction, pin, and archive/redact use existing canonical memory
controls with a reason and an authoritative refresh. Strong deletion uses the
separate acknowledged delete/export live control; archive is not deletion.
Reviewed task learning opens the existing Work Board memory review. Its signed
proposal and correction path supplies later comparison authority; a generic
text correction does not acquire that authority. No automatic learning follows
from opening Library, Home, or Inbox.

These are the intended post-merge contracts. Milestone PRs carry branch-specific
validation until the reviewed epic PR lands on `develop`; live provider quality
and external-account behavior remain explicitly unverified.

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

Those proofs are runtime prerequisites for enabling remote vector operations,
not merge prerequisites. Keyless tests and health checks cover the blocked and
lexical/degraded paths without making a provider call.

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

The accepted #775 phase uses one shared bounded `remote_inference`
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
finalize a typed job. This bounded slice remains Partial because durable queue
restart adoption and automatic provider-cost reservation/reconciliation are
outside the current process-local contract. Those limits remain
operator-visible and must not be described as exactly-once or crash-proof
execution.

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
