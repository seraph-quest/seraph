---
slug: /current-app
title: Current App Guide
---

# Current App Guide

The private invoice PDF/CSV comparison target is **Planned** under
[ADR-017](./decisions/017-private-bounded-document-comparison.md) and
[#923](https://github.com/seraph-quest/seraph/issues/923). Its one bounded literal
line-total comparison produces cited formulas and private derived output.
Structural checks do not certify files malware-clean. This branch must complete
private ingestion, native child supervision, readback and cockpit proof before
changing shipped `develop` truth.

The accepted operator flow selects one PDF and one CSV against a bounded Goal,
reserves their immutable private pair, and creates the typed comparison on the
Work board. Ordinary parser capacity or priority refusal keeps that original
bounded attempt queued; it does not create a new attempt or terminal failure.
The task inspector reads a cited literal report and prepares a verified CSV for
an explicit save link. Owner/session/task changes and typed output denials clear
cached output. Completed execution history remains visible when current
Root/Goal authority blocks later private reads. The comparison records an
explicit `no_learning` result and makes no model/provider call.

Cancellation can leave the generic task card Blocked while its document
inspector records the actual cancelled parser and reap state. Unknown cleanup
holds capacity; the original exact witness permits cleanup reconciliation.
Recovery adopts recorded verified output without reparsing. Only a known
terminated interruption may use the remaining original retry allowance;
exit-zero with missing output remains lost-output/nonretryable in this version.
Use the document inspector's original-attempt recovery controls. Generic board
retry/unblock and workflow pause/resume/revoke/retry cannot replace a linked
document attempt or bypass its retry and positive cleanup requirements.

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

The backend and canonical workspace remain local on a CPU-capable host.
[ADR-008](./decisions/008-portable-core-and-consented-context.md) defines macOS
and Linux as peer core-host targets. This accepted target does not establish
new platform-specific capture or execution readiness; each selected optional
adapter/profile must report its actual proof and availability. All
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

Home also has a branch-local **Your first verified result** journey: choose
a deterministic local snapshot or public-source baseline plus snapshot, review
the $0 model cost and finite permissions, queue a typed task through the managed
dispatcher, and explicitly open its independently verified result. Progress is
owner/session-scoped in the existing activity ledger. The public watch starts
with scheduling disabled and is paused after observation. A baseline is not a
material-change dossier; both starters record `no_learning`. This remains
Partial on the open integration branch. The managed keyless local journey and
artifact digest are verified; live public-source usefulness remains unverified. See
[Guided First Result Setup](./first-result-setup.md) for bounds and recovery.

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

The historical ADR-004 target placed the core on the GPU host and used a
paired Mac edge. ADR-008 supersedes that fixed placement with an
operator-selected macOS or Linux core host. Historical GPU services
were reached over documented HTTP APIs. `ssh jupyter` is an administrator path
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

Branch-local #920 adds **Send neutral Telegram notice** in the current task's
inspector. Pair the operator/chat and grant Telegram transit consent in Settings
first. Explicit status review sends enum metadata; exact denial and cancellation
use current canonical authority. Sensitive approval and recovery remain in the
cockpit. Unknown delivery requires visible outbox recovery or an explicit fresh
notice that retires old controls. The provider-free adapter is **Partial** and
does not prove live Telegram; the [owning reach contract](./04-presence-and-reach.md#paired-telegram-task-controls-branch-local-920-partial)
records the bounds and validation surface.

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

The #900 ownership implementation adds a separately reviewed private continuity
credential and exact read-only recovery contract, described in
[Operator ownership and recovery](./19-operator-ownership-and-recovery.md).
This is branch-local intended post-program behavior until reviewed promotion to
`develop`; it never restores prior grants,
adopts ambiguous legacy history, or substitutes a retired execution root.

The task inspector also exposes local, goal-scoped
[evidence working sets](./task-evidence-working-sets.md). Citation references are
revalidated against canonical records and verified artifacts before inspection
or explicit model-context adoption. Exact selected historical records remain
read only, and historic/private source egress remains blocked for the generic
strategist purpose. This is branch-local intended post-program behavior;
provider usefulness remains externally unverified.

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

#### Bounded execution evidence (#917 branch-local target)

The existing task evidence inspector can bind an exact reviewed packet for
execution with a separate unchecked acknowledgment. This grants no model
egress, external permission, approval or dispatch. Eligible sources are current
owner/Goal canonical operator facts and verified browser, evidence-dossier or
local-report outputs; each artifact producer binds only its matching consumer,
as specified by [ADR-014](decisions/014-bounded-evidence-dependencies.md).
Current source, Root, Goal, task and packet bindings are checked before new use,
including each browser subrequest. Mail and Calendar evidence remains private
inspection only and cannot become an execution dependency or generic model
context through a purpose flag.

Read-only affected-task inspection changes nothing. A separately reviewed
bounded impact page can pause stale pending work while preserving running,
completed, Review and other blocked tasks. Explicit replacement binding clears
only the exact evidence pause to Todo; it preserves executor inputs, attempts
and deadlines and does not approve or dispatch. Reload inspects retained exact
requests without automatically resending them. Historical terminal replay
remains readable after a correction without granting new use or adoption.

Specify generation stores the protected actually-used source snapshot. Only
the original native job's exact verified generated-advisory-output readback can
settle that proposal for reconciliation or acceptance; a generic successful
effect cannot. Proposal acceptance binds the exact canonical prepared input to
the same task, and pending input binding is displayed separately from dispatch
readiness. A generated proposal is advisory output, not execution of its task.
The isolated October 3 operator journey exercised governed generation,
acceptance, native browser readback, future artifact use, correction, safety
pause and explicit rebinding with no learning. Model and public HTTP responses
were intercepted and nonlocal sockets denied; live provider quality remains
unverified. This is branch-local Target evidence, not Shipped `develop` truth.

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

### Reviewed procedures v2 (M6 #889) {#reviewed-procedures-v2-m6-889-branch-local-target}

[ADR-015](./decisions/015-reviewed-procedure-preferences.md) accepts a narrow
reviewed-learning target: explicit feedback on matching manual invocations may
propose a Goal/version-local preference for an existing `public-browser-check`
version. The integration-branch implementation counts the complete matching
manual population before filtering: two distinct native readback-verified
outcomes with current Helpful feedback and no current Harmful feedback can
produce a proposal. Corrections append an exact predecessor-bound decision;
failed, unreviewed, stale, and unresolved members remain visible and cannot
become successful votes. Insufficient evidence returns `no_learning`.
Feedback is enabled only after the current attempt ends. A decision binds the
exact task revision, latest ended attempt ID, and fence; later changes retain
the full history but make the old decision ineffective. The Library labels
stale historical feedback separately, and a new explicit correction requires
the historical tip and a reason. Historical replay cannot restore eligibility;
stale feedback records `feedback_outcome_stale` and `no_learning`.

The Library runs the provider-free `memory.procedure-recommendation.v1`
capability through the existing routine API and durable native job runtime.
Its Inspector declares local read/artifact permissions, zero inference budget,
one attempt, no automatic retry, a maximum 120-second original deadline,
20 manual invocations, 100 feedback events, and 128 KiB of serialized metadata.
Private source proofs have 4 MiB individual and 16 MiB aggregate bounds. An
explicit owned cancellation creates no preference or positive readback.

Preview, acknowledgment, signed canonical-memory adoption, and rollback remain
separate explicit actions. Future selection rechecks the complete current
population and exact original owner/Root, Goal, reviewed version, package, and
source bindings; another matching invocation or correction makes an old
preference unavailable. Selection loads reviewed version metadata only and
never authorizes or starts invocation or scheduling. Rollback retains the
outcomes, corrections, proposal, and signed memory history.

Preview and adopted Library suggestions both display the exact included list
and count, explain that scheduled invocations are excluded, and disclose that
the deterministic preference is not a measured quality improvement. The actual
isolated managed journey exercised native parent/fixed-leaf execution, feedback,
insufficient evidence, adoption, read-only selection/reload, Harmful correction,
and rollback. Public HTTP responses were intercepted and nonlocal sockets denied;
no model or account contact occurred. This is branch-local implementation
evidence; whole independent review and Shipped `develop` truth remain separate
gates.

The #889 reviewed procedure surface turns verified Work Board outcomes into reusable,
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

This remains **Partial** and is not Shipped `develop` truth. On October 1, 2026,
the managed CPU-host journey prepared, reviewed, installed and activated a
public-browser procedure, then executed its native leaf and verified both the
parent and leaf readbacks. The leaf artifact hash matched the stored file.
Twelve native vertical tests additionally exercised actual SQLite, Chromium,
package files and intercepted Calendar/model boundaries. These are mechanical
execution receipts; Calendar account/model usefulness and production readiness
remain **external-unverified**. Fresh independent reviews accepted preparation recovery,
interface authority/recovery and the dispatcher compatibility corrections.
After the managed backend restart, a new public-browser invocation completed
with parent and leaf readbacks, a matching artifact hash and explicit
`no_learning`. A separate live response-loss check retained the lifecycle
recovery gate across reload, blocked fresh preparation, and cleared it only
after an explicit exact authority refresh before resume. The implementation
has independent preparation, interface-authority, and dispatcher recovery
validation.

### Governed repository repair (M4 #887) {#governed-repository-repair-m4-887-branch-local-target}

The #912 branch target adds the explicit `repo-node24-npm-v1` profile behind
this same Work/API/durable-job journey. [ADR-007](decisions/007-bounded-node-repair-supervision.md)
owns its accepted contract; it is not shipped `develop` truth.
[Optional Node Repository Repair](./optional-node-repair.md) describes installed
runtime selection, finite selectors, exact approval and unknown recovery. Settings select
an already installed absolute Node 24 executable. Only finite inspected `npm
test`, `npm run build`, or `npm run build test` selections are accepted; Seraph
maps their script bodies to direct Node argv and never runs npm, shell bodies,
installation or lifecycle hooks. An inspected TypeScript `tsc --project` build
uses copied existing compiler dependencies. Package/lock/config/script/source,
dependency manifests and trusted toolchain hashes bind approval and readback.
Dependencies have separate 512-file/16MiB-per-file bounds inside the shared
64MiB snapshot aggregate; source, patch and output bounds remain explicit.

Native Node supervision is an optional Linux x86_64 capability, verified with
pidfds and a dedicated per-job subreaper. Other platforms or missing facilities
block this selected profile visibly; they do not block Seraph's Python/core
startup or select a fallback. Approval shows exact build/test argv and requires
fresh `local_host_execution`. The Inspector labels preparation evidence as the
recorded job preflight; it does not replace fresh approval/claim checks.
This is host-user execution with no OS isolation
or enforced CPU, memory and PID ceiling. Detached descendants must be reaped
before success. Supervisor death, missing/reused restart identity or exhausted
cleanup/readback deadline remain `unknown_external_effect` and retain the
physical slot until durable evidence resolves them. Model transport in local
acceptance tests is intercepted at the governed boundary; no live provider or
paid-inference proof is claimed.

The M4 capability adds a bounded repository-repair path to the existing Work Board
execution boundary. In **Work → Repository repair**, an authenticated operator
selects an owned active Goal and current revision, then supplies the strict
repository-relative source and focused-test input. The form rejects protected
paths, unsafe references, out-of-scope tests, and unbounded values before the
server creates the typed owner/session-bound input artifact. Seraph then
publishes a `Todo` task with the artifact ID; the artifact reservation and task
publication use separate idempotency keys, and the task carries no caller-
supplied typed reference or raw source path authority.

The pending repair draft is private to the authenticated owner/session. An
unknown artifact or task response retains the exact original keys and payload;
the operator must reconcile or retry that exact request before another
mutation is allowed. Artifact reads expose only safe identity, digest, expiry,
and lifecycle metadata. The server then inspects a private snapshot, pauses
for explicit code-egress consent, and asks the governed `strategist_agent`
route for one strict patch proposal. The operator reviews the exact proposal
and approval target before the selected executor's staged test/readback path
can run.

The repair path binds the source packet, model request and response, patch,
approval, owner/session, Goal, attempt, and durable job by digest. It exposes
blocked, stale, revoked, and unknown recovery states and records
`memory_status=no_learning`. Tests run against the bounded staged snapshot and
do not claim to modify the original checkout. Source text, prompts, model
responses, and credentials stay out of generic operator projections.

The selected repair executor is server-owned settings, visible in Settings →
Repository sandbox and in the Work inspector. A fresh settings document selects
the disabled **Trusted local staged runner** by default. Local execution uses a
private staged directory and fixed test argv as the host user; it has no OS
isolation guarantee, does not claim CPU, memory, PID, network, or filesystem
confinement, and requires a separate exact `local_host_execution` approval for
each job. The approval surface says **Approve local tests on this host** and
shows the host-user filesystem, network, and resource boundary. Settings
selection is never execution permission.

The optional **Docker rootless** profile requires a strict Linux rootless
daemon, pinned image, and independently verified fixed limits. The optional
**Docker rootful** profile uses an existing configured daemon and must
independently verify its non-root worker, network, read-only, capability, image,
and resource posture. Missing or drifted evidence blocks the selected profile;
Seraph does not silently switch between local, rootless, and rootful execution.
The legacy
`engineering.repo-change.v1` path remains strict rootless-only and is shown as a
separate preflight.

The settings and repair status APIs expose a complete typed `executor_posture`
display projection alongside the exact server receipt in
`executor_posture_raw`; `executor_posture_digest_basis=executor_posture_raw`
documents that the unchanged `executor_posture_digest` binds the raw receipt,
not display-only defaults. A blocked Docker receipt therefore reports
`unverified` isolation, network, and resource labels without turning them into
execution authority, while local `host_access` remains visible as the explicit
per-job approval boundary.

On a first managed local start, a newly created workspace is private (`0700`),
settings descendants repaired on the current-owner write path are private,
and the persisted selector file is `0600`. A pre-existing broad workspace or
foreign-owned, symlinked, or otherwise untrusted settings path remains
blocked. Use a private workspace beneath trusted ancestors and retry; Seraph
does not automatically chmod an existing workspace root or shared ancestor.
Saving selectors never starts Docker or changes host resource limits. A legacy
settings document without an executor selector remains rootless-only until the
operator explicitly selects another backend.

The tested-publication integration branch implements the accepted
[tested repair publication](./decisions/009-tested-repository-publication.md)
and [finite GitHub consent](./decisions/011-finite-github-connection-consent.md)
targets. Its selected `repo-python-pytest-publication-v1` profile reports the
recorded successful preflight from `ok=true`; a `ready` label alone cannot
establish that proof. Preparation does not approve local host execution, and
display normalization does not change the raw authority receipt or digest.
The copied Python environment and fixed Git producer claim no OS isolation.

Publication requires a separate exact preview and approval after the repair's
verified test readback. GitHub settings separately ask for unchecked consent
to named actions on the selected repository, credential and current login,
with a finite expiry. Stopping writes preserves an uncertain job's reservation.
An explicit current-revision readback acknowledgment permits only destination
GETs; it does not replay writes or revive the original approval. A changed Goal
can retain a positive observation while the original task remains Unknown.

The separate unchecked capacity-close control uses complete positive
destination proof and the original producer's verified quiescence to release
only the exact connection reservation. It preserves Unknown effects, costs
and the explicit `no_learning` outcome, and permanently fences the old job
against new writes. Reload discovers owned history without automatic consent,
publication, reconciliation or closure. An ambiguously answered close request
retains its exact body and key; only the server's consistent canonical
inspection can identify an applied receipt or permit explicit discard of a
permanently rejected request. These are branch-local capability contracts,
not a claim of shipped `develop` behavior or live GitHub/provider usefulness.

This remains **Partial**. Intercepted model transport and executor
mechanics prove request, authority, recovery, and readback contracts only. A
local technical preflight can make preparation ready, but execution remains
blocked until the exact per-job host approval is recorded. If Docker CPU,
memory, pids, network, image, or daemon posture cannot be verified, the
selected Docker path blocks without falling back. No live provider/account
canary, paid inference, kernel resource-enforcement receipt, or
original-repository write is claimed. Repair execution uses one durable
`repo-repair-execution` slot across Goals and recovery, releases the
`remote-inference` claim before approval or test dispatch, and sets one
absolute execution deadline for staging, process startup/wait, output drain,
cleanup, readback, and publication; it does not reset that deadline per phase.
Cancellation or unproven cleanup/readback/publication remains blocked or
unknown and is reconciled against the same job/attempt rather than replayed as
a fresh proposal or execution. The final native verifier covers one local API
journey, two approved Goals sharing one physical native worker, same-job API
cancellation with fresh reconciliation, staged subprocess/filesystem
readback, unchanged source/.git state, verified cleanup, and `no_learning`.
Its deterministic model transport is intercepted. Local execution intentionally
has no OS confinement. Provider/account usefulness, live OpenRouter quality,
and Docker resource enforcement remain **external-unverified**. The original
repository is not claimed changed,
and this capability remains Partial rather than Shipped `develop` truth.

### Reviewed public evidence pipelines (#914 branch-local target)

The existing task inspector offers a reviewed, fixed chain from
`browser.public-task.v1` to `work.evidence-dossier.v1` and
`work.local-evidence-report.v1`. Its non-executable operation metadata lives on
the existing Work Board proposal. Each leaf uses the existing task, priority,
claim, cancellation and durable job paths. The two CPU leaves use deterministic
local transformations with no model, network, credential access, subprocess or
learning. Public extracts remain quoted structured data, and the verified
report is served as `text/plain` and rendered literally in the cockpit.

Preview an unattempted public browser task and approve the exact source scope
and plan digest. After a producer completes, explicitly materialize its
independently verified next input. Each producer must have canonical native
identity, a settled matching artifact/readback and actual unchanged output
bytes; browser output also needs its typed context-cleanup receipt. The chain
has one absolute deadline of at most 300 seconds from first admission,
further narrowed by the current Goal grant, two attempts per leaf and six
aggregate attempts, 180 seconds per browser leaf and 30 seconds per CPU leaf.
Outputs are bounded to 64 KiB and quoted consumer inputs to 40 KiB.

Goal or source-permission changes durably freeze unfinished claims. Source
replacement requires exact operation and task revisions plus verified
quiescence. It preserves the original deadline, attempt counters, completed
outputs, unresolved liabilities and immutable old handoffs. An unattempted
consumer retains its task identity while only its current link and current
handoff binding change. A completed consumer requires a distinct operation.
A freshly reviewed finite operation may reuse only exact independently
verified completed output for unfinished consumers in the same original
workspace and owner scope, under the current consumer Goal and source policy.
Original attempts, effects and liabilities remain with their original operation.

Materialization reserves its exact operation/version/producer-attempt/consumer
key before writing private bounded files. Interrupted writes resume only that
binding. Reload recovery retains and rereads a bounded exact mutation request
in owner/session-scoped session storage before POST; corruption or unavailable
storage blocks actions, and an uncertain outcome exposes explicit exact retry.

This capability remains **Partial**. The October 3 managed CPU-host receipt
used real operator authentication, actual public HTTPS body extraction, native
browser execution, both deterministic CPU leaves, durable consumed inputs and
verified report readback under the original deadline. This establishes that
finite chain; broader DAGs, model planning, publication and PDF/CSV export are
outside its contract. [ADR-010](./decisions/010-reviewed-artifact-pipelines.md)
owns the accepted target.

### Finite evidence research (#901 branch-local target)

The Work Board offers a finite research dossier form and a focused task
inspector. Select a current Goal, one or two perspective instructions, and at
most four explicit public HTTPS text sources or independently verified
completed Board artifacts. Source acknowledgement is separate from the
existing governed model-egress consent, capability proof and accounting gates.
Each perspective has one synthesis contact; the parent makes no model call.
The original parent deadline is at most 300 seconds, further narrowed by the
Goal, and recovery cannot renew deadlines, attempts or cost allowances.

Explicit recovery reserves a current phase on the same original Board attempt.
It can continue the persisted source-ready, prompt-ready or funded queue, or
adopt an exact reserved physical child output with settled accounting without
another provider POST. A current Running worker without that completed-output
proof remains blocked from recovery. Contacted work without verified output
retains Unknown cost and effects; it is never blindly retried. Generic retry
and unblock cannot create a replacement attempt for an admitted research task.

Cancellation first fences the original operation. It closes only verified
lease-free waits or actual owned workers whose awaited completion is recorded.
An expired lease or an empty process registry does not prove quiescence.
Completed child outputs and actual overrun charges remain visible. Uncertain
source or provider work remains Unknown, and missing proof keeps cancellation
pending. The inspector offers exact retained-request retry and explicit cost
recovery guidance. Corrupt or unavailable owner/session-scoped session storage
blocks mutations; the full retained request is bounded to 16 KiB.

The dossier is served as verified `text/plain` and rendered literally.
Citation checks establish matching supplied spans and digests, not semantic
truth. Every result records `no_learning`. This capability remains a
branch-local **Target** and **Partial** operational evidence. The October 3
managed CPU-host journey used actual operator authentication, governed settings
and capability admission, public HTTPS source reads, two native child jobs,
settled accounting, durable input consumption and physical dossier readback.
Its exact retained creation request survived a lost response and browser reload;
the completed dossier rendered literal injection text without executing it.
Only the provider HTTP boundary was intercepted. This verifies the local
runtime and operator journey; it does not establish live provider quality or
actual upstream billing. Independent review corrections require the exact
original child group and current authority before presenting recovery, keep
generic retry/unblock fenced, and preserve one inspector per selected task.
[ADR-012](./decisions/012-finite-durable-readonly-research.md) owns the accepted
target. Local source/output and current policy metadata reads inside selected
SQLite writers are bounded by their contracts; their lock duration and physical
filesystem race limits remain relevant.

### Bounded public browser tasks

The bounded capability adds `browser.public-task.v1` through the existing Work
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

This remains **Partial** and is not Shipped `develop` behavior. Local fixture
execution and production-network evidence are separate proof boundaries;
authenticated browsing and general computer use remain outside this capability.

### Bounded calendar meeting preparation

This remains **Partial** and is not Shipped `develop` behavior. Provider-free
tests establish local mechanics; a working Google account and live model
usefulness remain explicitly **external-unverified** without an authorized
canary.

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

### Bounded Gmail source, watch, and reply drafting (M7 #890) {#bounded-gmail-source-watch-and-reply-drafting-m7-890-branch-local-target}

The branch adds an owner- and session-bound Gmail readonly path with encrypted
credentials, explicit source and model consent, opaque local message bindings,
bounded metadata watches, and private reply drafts. A watch uses the existing
governed scheduler with the exact `hourly` or `6h` cadence shape, starts in
`not_started`, and may create at most three neutral inbox notices during the
finite Goal notification period. The 512-key seen cursor fails closed with
`coverage_blocked` rather than forgetting older messages. Inbox acceptance is
the explicit human-triage step; scanning never creates an executable reply or
contacts a model per message.

Reply drafting reads the selected message twice around one governed
OpenRouter inference admission. The reviewed source is passed in full up to
8 KiB, model output is exactly `{subject, body, caveats}`, and the server
attaches the message revision before writing a 0600 encrypted private draft.
The private artifact has durable checkpoint, hash, readback, exact-key replay,
and unknown-recovery states. Generic Work Board, inbox, notifications, and
browser storage do not expose the private body or operator intent. The bounded
reply intent is a local 0600 typed-input artifact and remains a plaintext
residual within the local-workspace trust boundary; the source body and draft
remain encrypted private artifacts. Lost setup, reply, and watch responses can
be resolved by their original opaque key under the current authenticated
owner/session. Watch notices obey the Goal's finite quiet-hours and
notification allowance across the whole period; uncertain or over-capacity
coverage stays visibly blocked for reconciliation.

This remains **Partial**. Focused SQLite and intercepted-transport tests prove the bounded
mechanics; CPU-only/keyless OpenRouter operation, a real Google account, live
Gmail usefulness, send operation, and paid model canary remain
**external-unverified**. See the M7 mail wire contract in issue #890 for the
exact request, recovery, privacy, and readback boundaries.

### Exact owned-calendar reschedule

[ADR-019](./decisions/019-exact-calendar-reschedule.md) accepts one literal
owned-event reschedule through separate exact owned-event/calendar-list identity
profiles, current owner/source proof, one approved conditional PATCH and strict
independent readback. Response loss remains Unknown with no resend; a separate
same-original-Root readonly observation preserves original liabilities. This
branch implementation is **Partial**: actual authenticated SQLite/Vault/native
jobs, approval, encrypted artifacts and managed operator UI have been exercised
with only external Google HTTP/DNS simulated. Live Google remains
**external-unverified**, and the whole implementation still requires independent
review before integration; this is not Shipped `develop` truth.

In Settings, import separate exact read and write profiles. In Calendar meeting
preparation, readonly event selection may leave model consent unchecked;
preparation and scheduled observation still require their explicit model grant.
Select the returned event, verify the exact profile pair, and confirm the three
unchecked finite reschedule permissions. Enter RFC3339 seconds with a literal
UTC offset and an explicit IANA timezone. Create the native task and preview,
approve its exact content, then execute once. Requested `sendUpdates=none` does
not promise that Google reminders or other provider behavior cannot send messages.

The owned task detail exposes the separate Calendar native job. Its local
Root/task-scoped identifiers are discovery hints: canonical metadata must match
the selected task before private content or controls become available. Metadata
remains inspectable after the original Goal closes, while private preview access
uses current permission checks. For an original Unknown with proved transport
closure, choose a new reviewed finite recovery Goal and explicitly acknowledge
four readonly contacts through the exact original read profile. The auxiliary
observation appears separately and never changes the original Unknown, deadline,
authority or write liability. Neither response loss nor reopening automatically
resends a write or observation. Expired active reschedule permission must be
explicitly revoked locally before a fresh grant; expiry never renews permission.
All reschedule and recovery results explicitly use no model and no learning.

Reschedule profile revocation invalidates local authority before credential
cleanup. Owner-scoped Vault deletion and verified unavailable readback happen
outside the native SQLite writer. An interrupted or failed cleanup appears as
`blocked_cleanup`, which permits no provider contacts or private preview reads.
Settings refreshes that metadata and offers an explicit bounded local retry of
the original revoke UUID/digest with the current profile revision. Cleanup never
renews profile authority or changes original native execution history. A verified
unavailable credential is logical cleanup; encrypted audit bytes may remain and
physical erasure is not claimed.

### Exact Gmail reply send

**Partial:** The [ADR-016](./decisions/016-exact-gmail-reply-send.md) operator
path imports two separate grants in Settings → Mail: readonly + identity and
send + identity. Importing credentials grants no provider contact. Choose a
current finite Goal and explicitly verify both grants against the same mailbox.
Every refresh requires the exact observed scopes and the same stable identity;
legacy Mail source access remains separate.

Open a completed private Mail draft in the Work Board inspector. Load the local
reply profiles, explicitly authorize a source/identity read, and prepare the
private preview. It shows the verified sender, one recipient, original Subject,
literal saved body and expiry. Local copy edits are not sent. Approve these
exact bytes separately, then send once. The original native operation has a
120-second deadline, one attempt and at most 14 contacts across preview and
execution. Strict independent readback proves the sender's Sent-mailbox state;
recipient delivery is not proven. Attachments, HTML, aliases and automatic
learning are excluded.

An uncertain send remains **Unknown; no resend**. Inspect the original receipt
manually after response loss or reload. Cancellation waits for actual owned
transport closure and cannot erase a possibly accepted send. With the original
live operator session, explicitly select a separate current finite readonly
RecoveryGoal and authorize one Sent observation (at most nine contacts). Its
auxiliary job and bounded observation history can confirm Sent state while the
original status, deadline, Goal, approval, write effect and liability remain
unchanged. A revoked/replaced operator Root, unproven worker closure, incomplete
or ambiguous search, or unavailable private artifact blocks this path. A new
Root cannot adopt the old send authority.

Readonly recovery requires the original worker's versioned transport-closure
receipt, bound to that send's intent, execution fence and complete contact
history. Every new contact invalidates prior-phase closure in its reservation
transaction. A response-lost reservation can qualify when its actual HTTP client
has positively closed; an expired lease, generic restart recovery or an empty
process registry cannot establish closure. Older receipts without this proof
remain blocked for recovery; the app does not reconstruct or resend them.

Focused acceptance uses actual authenticated SQLite, encrypted Vault/private
artifacts, canonical draft admission/accounting, approval, native send and
readonly recovery. Only external Google HTTP is intercepted for send/recovery;
draft preparation separately intercepts the governed OpenRouter HTTP setup
route and records real usage/effective-route receipts. The managed UI verifies
literal multiline rendering, separate approval, sender Sent readback, an
accepted-response-loss Unknown send, separate readonly observation and manual
reload inspection. This is branch-local implementation evidence, not a live
Gmail or Shipped claim. Provider search indexing, changes between source read and send,
and human review of untrusted incoming Reply-To remain explicit limitations.

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
boundaries; live external/provider usefulness remains **external-unverified**.
This capability remains **Partial**.

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

**Planned:** explicitly selected desktop context on macOS or Linux attaches
reviewed content to an exact owned task under ADR-008. Existing Mac-native
capture remains a separate implementation fact. Optional paired edges and
local capture adapters require their own readiness receipts; pairing must not
implicitly authorize execution or data egress. Task attachments must bypass
general screenshot observation and automatic analysis.

## Memory

### Cockpit navigation and canonical inspector

The bounded cockpit capability provides six persistent cockpit sections: **Home**,
**Inbox**, **Work**, **Goals**, **Library**, and **Connections**. These select
existing cockpit surfaces and retain the conversation, selected task, and
window layout. Home is a bounded operational summary, not a second scheduler:
page counts are labelled as such, failed refreshes preserve last-known data,
and unavailable queue or spend evidence remains unavailable. Pending approvals
open the existing approval surface.

Home's branch-local **Needs attention** snapshot deduplicates current-root task approvals, unknown outcomes, blocked or failed work, stale verification, and linked Inbox decisions. It retains last-confirmed metadata and uses explicit refresh. Attention opens the existing task inspector, rechecks the exact approval or owning readback, and returns keyboard focus to its originating Home or Inbox context. Recovered history remains read only. Pending approval timestamps include UTC offsets, so valid approvals retain their expiry instant in browsers in other timezones. Verified GitHub readback converges the original latest attempt only while the original owner, goal and connection authority remain valid; authority changes preserve settled-effect truth and a specific blocked task reason. Cost recovery links to Settings only after the owning API advertises the exact job/goal control. See [Attention and Recovery](./attention-recovery.md).

The rendered Warsaw browser journey is mechanically verified through real ASGI HTTP/WebSocket handlers and retained SQLite/artifacts, with intercepted public-source/GitHub transport and an explicit server-side test permission. Recreating that ASGI app against the same database proves persisted recovery. The branch-local tested-publication milestone separately verifies a managed backend restart, native Git production, response-loss recovery and explicit capacity closure against retained SQLite/artifacts, with simulated GitHub HTTP and non-local sockets denied. Governed GitHub Settings creates finite Root-bound write consent; the attention UI does not create it. Readback and capacity closure use separate explicit acknowledgments and preserve Unknown effect, cost and no-learning truth after consent stops or the Goal changes. Live external usefulness remains unverified.

This remains **Partial**; live provider and external-account usefulness remain
explicitly **external-unverified**.

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

## Fixed reviewed local JSON formatter

**Status:** Target on the Epic integration branch; this is not a `develop`
Shipped claim. [ADR-013](./decisions/013-fixed-reviewed-local-tool-package.md)
defines one optional fixed package, `seraph.tool.json-format` v1.0.0, exposed as
`work.json-format.v1`. Work → **Isolated JSON formatter** shows its exact manifest,
content digest, permissions and limits. Select a current Goal, review and
approve that exact package, then create a JSON task. The existing dispatcher
owns Ready/admission, priority and execution; there is no installer or registry.

Input is UTF-8 JSON up to 32 KiB; duplicate keys, non-finite numbers and excessive
structure are rejected. Output is sorted, two-space JSON up to 64 KiB. One
attempt has an original absolute deadline of at most ten seconds. The native
profile enforces namespaces, a syscall filter, single-process execution, two
CPU seconds, 128 MiB address space, bounded descriptors and 8 KiB stdout/stderr.
These are hard CPU-time/address-space bounds, not CPU-rate or RSS quotas. It
has no network, credential, model or canonical-memory learning permission.

The optional dependency profile supports native Linux x86_64 with the exact
reviewed CPython 3.12.8 closure. Other architectures, macOS, missing builds or
missing kernel enforcement visibly block this feature while the portable core
remains usable. Dependency metadata availability is not isolation proof: the
actual trusted supervisor/bootstrap must establish enforcement before package
work. Runtime traffic does not use Docker, a rootful service or a tunnel.

Preparation is an explicit offline operator task, separate from invocation.
Use the pinned [bubblewrap source commit](https://github.com/containers/bubblewrap/tree/2a76602a8c71f36c1527cf9fc3417d9149822e0c)
with Meson 1.9.1, Ninja and matching local libcap headers/library. Build in a
repository-local private directory with `selinux=disabled`, `man=disabled`,
`tests=false`, `bash_completion=disabled`, `zsh_completion=disabled`; leave
`assume_kernel` unset and verify its effective empty default. The approved
bubblewrap binary SHA-256 is
`95a4c13e9652537a941aea7c714516f199f477312eadd4f172845e0e0b4f87f5`.
A differing binary remains Blocked pending review; Seraph does not install or
download a replacement during use.

Build the committed trusted C embedding launcher against the matching local
CPython headers/libPython, without a host-global install. From the repository
root, the reviewed compiler invocation is:

```bash
tool_python_prefix=$(backend/.venv/bin/python -c 'import sys; print(sys.base_prefix)')
mkdir -p build/916-embedding-r5
cc -O2 -fstack-protector-strong -D_FORTIFY_SOURCE=2 -Wall -Wextra -Werror \
  -Wl,-z,relro,-z,now -Wl,-rpath,/runtime/lib \
  -I "$tool_python_prefix/include/python3.12" \
  backend/src/execution/tool_package_launcher.c \
  -L "$tool_python_prefix/lib" -lpython3.12 \
  -o build/916-embedding-r5/isolated-python
sha256sum build/916-embedding-r5/isolated-python
```

The approved launcher digest is
`409611f20b3c59146155e6dad7e274e062bc9b756085c81d479bd6365da8fb54`.
The launcher obtains a bounded native hash seed before isolated initialization;
no `/dev` device bind or package-controlled import path is permitted.

Manually prepare `<canonical workspace>/artifacts/tool-package-runtime/json-python-bwrap-v1`
under private, owner-controlled ancestry. All directories must be mode 0700;
regular files mode 0600, with the launcher, bubblewrap and dynamic loader mode
0700. Copy only the fixed mapping returned by
`src.execution.tool_package_profile.expected_runtime_files()` beneath `rootfs/`,
plus the reviewed bubblewrap binary at `bwrap`. Create empty regular placeholders
`rootfs/input.json`, `rootfs/package.py`, `rootfs/out/result.json` and an empty
`rootfs/proc` directory. `profile.json` is a JSON object with exactly `schema: 1`,
`profile: "json-python-bwrap-v1"`, the pinned `source_commit`, `assume_kernel: ""`,
and `files`, mapping `bwrap` and each `rootfs/` closure path to its SHA-256.
No links, devices, extra files or writable directory entries enter the package
namespace. `inspect_runtime()` validates this complete closure; the operator
profile readback reports a block if any dependency differs. The actual OS
preflight and attack receipts remain required before claiming containment on
a new host. Private test receipts are evidence, never provisioning inputs.

After execution, the task inspector shows cleanup and **no_learning**. **Read
verified JSON output** reopens the exact physical artifact and serves it as
`text/plain` with `nosniff`; React displays literal text. Cancel records the
original attempt's intent and waits for actual supervisor reap. If a response
is lost, the bounded request remains in owner/session/task-scoped storage;
manual refresh clears a pending cancel only after matching canonical
attempt/fence/intent readback. Cleanup truth is shown separately from intent.

**Inspect and recover original output** can adopt only the exact reserved,
finished output with actual reap proof under the original Root/Goal/permission
and unexpired deadline. It creates no process, attempt or renewed allowance.
Missing ownership, uncertain cleanup, an active lease or an expired original
deadline stays Blocked/Unknown with an explicit reason. Produced but unadopted
bytes may remain private audit evidence; they do not authorize Done or learning. Positive artifact and verified-output receipts commit together with native success only after the same writer rechecks current Root, Goal, attempt, lease and cancellation authority. Physical output and artifact metadata are staged before that writer; a late correction retains cleanup and produced audit bytes without adopted success.
Pause/revoke/quarantine fence new work and late adoption; completed verified
output remains read-only history.

The authenticated managed acceptance used actual local API/lifecycle review,
approval, task creation, native OS execution, SQLite/artifact readback and a
same-Root cold restart through `manage.sh`. The original output remained
readable in the literal UI after restart, with one attempt and its unchanged
deadline. Separate actual SQLite cases prove cancellation, logout/current Goal
revocation, finished-output recovery and lifecycle contention. No provider
transport was intercepted or contacted, and no macOS isolation proof is claimed.
Independent cumulative review remains the whole-milestone gate.

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
The canonical job repository owns deployment accounting reservations and their
original UTC calendar month and settings revision. The deployment owner does
not change with login, enrollment, or root identity. Actual OpenRouter account
`usage.cost` rounds upward to whole micro USD; upstream BYOK charges and credit
purchase fees are outside this accounting scope. Missing cost remains held,
including across restart and month rollover. Server-reviewed per-request
reservations and a monthly admission ceiling bound local admission; they do not
prove the provider cannot charge more. An over-bound charge is retained in full
and blocks further admission until an authenticated settings request explicitly
reviews an adequate request reserve. That review records the exact settled
operation sequence and settlement revision. Cap edits, unrelated settings
saves and month rollover cannot clear it, and the review cannot cover an
unknown operation that settles later.

Every observed UTC month change, including normal calendar rollover, blocks
fresh admission until the operator acknowledges the exact server-observed
month and accounting revision in Settings > Artifacts. This deliberate review
prevents clock jumps from granting a fresh allowance. Correction preserves the
trusted month high-water; future-attributed settled charges conservatively
count against current capacity and original operation months remain unchanged.
Settlement remains available while the period requires review. The review
cannot grant provider access, resume a job, or adopt a result.

Settings > Artifacts exposes committed, reserved, unknown, and remaining cost,
and exact-operation reconciliation with operation/job identity, revision,
evidence digest, and an idempotency key. Manual declared amounts remain
externally unverified and cannot revive a grant, resume a job, or adopt output.
Deployment-wide egress revocation preserves credentials and cost history;
re-grant requires an explicit current settings revision.

The managed launcher retains one profile descriptor at
`docker-data/<dev|prod>/workspace-lifecycle`, independent of the configured
workspace path, outside the restorable root (Docker mounts it at
`/app/workspace-lifecycle`). A different or empty root cannot initialize a new
budget under an existing deployment descriptor. Legacy receipt
migration runs under the existing maintenance fence. Restore and rollback must
retain the latest ledger matching its external high-water witness before
promotion. Missing mount proof, missing witness, or stale ledger visibly blocks
billable egress. A crash between witness persistence and SQLite commit also
blocks pending continuity reconciliation; a new budget cannot repair it. The
trusted directory retains one bounded content-free transaction checkpoint.
With the runtime stopped, `./manage.sh -e prod accounting-reconcile --confirm`
repairs only the exact base revision and witnessed digest under the existing
maintenance fence. It retains every unknown/contact liability and changes no
job execution or grant authority. Arbitrarily older snapshots still require
the latest retained ledger rather than this one-transaction delta. This
does not claim tamper resistance against a host administrator replacing every
trusted store.

The retained receipt also binds the owning configuration's monotonic egress
epoch and digest. Copying older settings blocks inference; restore and rollback
publish the archived policy revoked at a newer epoch before promotion. Only
`egress_revoked`, `egress_revision`, and `egress_revocation_key` may differ from
the archived policy during this reconciliation, and staged bytes must match
the trusted digest. Every other canonical file and policy field retains its
archive hash check. `./manage.sh -e prod accounting-reconcile --policy --confirm`
repairs the latest interrupted publication; a pending active publication
becomes revoked and needs fresh current-revision settings review. Exact clock
review is also available under stopped-runtime maintenance with
`accounting-reconcile --period YYYY-MM --expected-revision N --confirm`, where
the period must equal the current server-observed UTC month. Managed
`accounting-rebind --from-root /absolute/prior/root --confirm` fences both roots,
retains the latest ledger and unresolved liabilities, revokes copied provider
authority, then changes the descriptor binding. Every ledger-linked job must
match the source's immutable invocation and authority bindings. Existing and
missing target rows receive source evidence in a blocked state with no lease
and a revision/fence newer than both generations; conflicting or missing source
jobs reject the transaction. Exact interrupted retry retains that fresh fence
and preserves audit/FK rows. It grants no job execution.

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
finalize a typed job. Calendar and Mail typed jobs may recover a never-contacted
reservation through the existing Work Board artifact and attempt binding,
current original root/session/grants/policy, immutable input digest, and
unexpired deadline. Their reservation identity and priority order remain
stable. Digest-only ephemeral callbacks remain visibly blocked after restart;
contacted work never auto-replays. This bounded slice remains Partial: live
provider quality and billing evidence, and managed Docker deployment receipts,
are separate operational evidence. It must not be described as exactly-once or
crash-proof execution.

## Failure And Recovery

### Fixed private Moltbook browser read target

[ADR-018](./decisions/018-fixed-moltbook-private-browser-read.md) accepts one
optional private Home document read through actual isolated Chromium and the
existing owner connection. The branch-local implementation has mechanically
verified local authenticated TCP, Chromium, encrypted readback and operator
controls; it is not Shipped production-account truth. Normal production execution
is hard blocked: providing a key or consent cannot enable live Home. Activation
requires separately authorized effect, identity linkage and account/site evidence
and a reviewed production execution path. Existing registration keys and claim
material are outside local validation; the fixed agents/me alternative remains
an unimplemented fallback, never a silent replacement for approved Home.

The separately unchecked finite consent names account summary and own-post
activity fields and explains that the complete Home response is fetched while
role/unrelated instructions are discarded. Home may deliver/consume a due
briefing and produce access bookkeeping; it is not side-effect-free and grants
no role invocation, heartbeat, notification mark-read or business mutation.
The fixed native job allows one Home contact and two identity checks, no redirect,
refresh or replay after possible contact, with private field citations and
explicit no_learning. Unknown contact/cleanup remains visible and reserved until
positive exact settlement. Local acceptance requires an actual authenticated
local TCP site and real Chromium; those receipts cannot waive production gates.
One immutable Home source envelope binds each field's explicit source reference,
JSON pointer and value digest within 16 KiB provenance. Only the allowlisted
result is retained encrypted (64 KiB plaintext/96 KiB ciphertext); discarded
role text, unrelated feeds and raw headers stay ephemeral. Private readback
requires current Root/Goal/connection authority and never grants model context.
Original job history and exact applied-request replay stay read only after a
cold restart; a new execution cannot inherit the prior deadline or consent.
Expired or revoked authority denies private decryption. In-flight cancellation
can positively close Chromium while transfer closure remains Unknown; that
uncertainty keeps the exact reservation and browser lane quarantined. Local
response fixtures and Linux-host proof establish neither production effects nor
macOS host readiness, and no measured-quality or learning claim is made.

### Optional Moltbook adapter target

The branch-local `work.moltbook.v1` adapter uses **Settings → Moltbook** and
existing owner Vault, Goal, approval and durable-job records. Linux and macOS
core operation does not depend on Moltbook. Selecting a Vault credential is
local configuration with no Goal or remote-use permission implied. Account
registration and human claiming remain explicit owner setup on the official
service. Seraph does not automate email, identity checks or challenge solving.
Keys never enter prompts, generic skill HTTP calls, logs or output artifacts.

Local metadata refresh never contacts Moltbook. Explicit consent binds the
current login, Goal revision, connection revision, credential and named actions,
with personal noncommercial use and no redistribution. Explicit account inspect
establishes claimed or pending truth. Reads produce private literal JSON with
no learning; feed defaults to one item. Oversized or unsupported metadata blocks
visibly rather than being silently truncated into success.

Public introduction posts/comments need a completed public `introductions`
community receipt from the same authority within five minutes, acknowledgment
of community purpose and exact draft approval. Content verification retains
the same job: the human supplies a two-decimal answer and separately approves
that answer for the original content, challenge and expiry. The immutable
300-second job has one attempt and six contacts: one status GET, three other
fixed GETs, one creation POST and one verification POST. Restart, approval and
manual answer never renew those allowances.

The [official API instructions](https://www.moltbook.com/skill.md), checked
2026-10-03, document community feed/comment listings and verification.
Verification or exact-ID readback alone cannot establish public visibility.
Publication requires matching verified ID, author, text, target and parent
in one bounded public community-feed or target-comment listing; explicit
hidden/private/removed metadata rejects adoption. An absent item remains
unconfirmed. No invented positive visibility field, extra pagination, alternate
origin or retry is used.

Before replying, one bounded feed page from the reviewed public introductions
community must contain the exact target once; direct-ID access alone is not
public permission. Explicit pending/failed or hidden targets block before any
POST. This replaces the direct target GET within the same three-GET allowance.

Controls retain the exact request in owner/login-scoped session storage before
POST; corrupt or unavailable storage prevents submission. Execution binds its
action ID, phase and fence. Replaying creation reconciles only that request,
even when verification has since been approved. Local refresh clears pending
controls only from matching canonical receipts. Cancellation commits intent,
fences further contact/adoption and waits for actual transport closure. Known
remote content remains, never deleted. Unsettled contact stays Unknown with
operation capacity held; an empty process registry or expired lease is not
cleanup proof. Explicit recovery inspects an existing wait or adopts exact
original output after trusted readback and physical digest checks under current
authority; it never repeats HTTP. Provider cooldowns persist without automatic
retry. Transport closure does not claim forced termination of system DNS threads.
Definitive failed preflight reads retain their HTTP status and response digest.
When no creation or verification POST occurred, all read receipts are settled,
the owning worker has verified awaited transport closure, and current authority
still matches, the original capacity slot is released. A 429 cooldown remains
in force: the complete failed GET receipt and its once-observed bounded cooldown
are committed atomically; the connection API reports the absolute cooldown
with an explicit UTC offset. This also holds when another service has requested
cancellation. That settlement preserves every original authority check except
the cancellation and existing-cooldown contact fences; it authorizes no contact
or output adoption. Cancellation releases a settled 429 slot only with matching
original connection, revision and credential binding plus canonical cooldown
covering the observed expiry. Stale Goal or Root cannot establish that proof,
so the slot stays visibly held. Later work requires an explicit new admission
after the cooldown expires. An
uncertain transfer or authority drift retains capacity for explicit inspection.

Independent bounded review required public-list visibility proof and durable
cooldowns; those fixes control this operator contract. The October 3 managed
CPU-host journey used actual authentication, owner Vault, Goal, two exact
approvals, native jobs, SQLite, private artifacts and literal UI output. Only
the Moltbook HTTP boundary was intercepted. The same original operation was
read back after a managed cold restart, expired original consent and an exact
admission replay, with no new provider contact or deadline renewal. A bounded
owner/login-scoped job reference supports explicit local refresh after reload;
it stores no credential, content or verification secret and grants no authority.
Completed exact admission replay is read-only; new work still requires current
consent and source review. This target does not claim shipped `develop`
behavior, live claimed-account usefulness or successful live public outreach.

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
- [ADR-008: Portable Core And Consented Context](./decisions/008-portable-core-and-consented-context.md)
- [ADR-006: OpenRouter-only inference phase](./decisions/006-openrouter-only-inference-phase.md)

### Exact Forgejo issue title transaction target

[ADR-021](./decisions/021-exact-forgejo-issue-title.md) accepts one ordinary-issue title edit through the real Forgejo v15.0.9 editor, using backend-only session secrets and an exact separately approved title. This branch-local target remains incomplete until actual local execution and whole review. Live Codeberg is mechanically blocked pending separate account/site/version acceptance. Provider title history and notifications remain; the provider offers no atomic revision CAS or idempotency key. A lost response stays Unknown, read-only observation never resubmits, and reversal requires a new exact approval. Configuration grants neither Goal budget nor mutation permission.
