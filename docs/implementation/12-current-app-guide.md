---
slug: /current-app
title: Current App Guide
---

# Current App Guide

## Current Implementation And Cordis Target

The application described here runs the existing Python/FastAPI backend and
React cockpit. [ADR-026](./decisions/026-all-plugin-cordis-architecture.md) accepts
an all-plugin Cordis agent-runtime composition target; its migration capabilities
are **Planned**. Continue using the current managed commands, workspace and
operator controls. The architecture decision by itself does not install Cordis,
replace the runtime or migrate stored data. The separately implemented optional
lifecycle host below does not migrate the agent loop or canonical ownership.

### Reviewed optional Cordis lifecycle host

This is the implementation contract for [#1006](https://github.com/seraph-quest/seraph/issues/1006)
under [ADR-026](./decisions/026-all-plugin-cordis-architecture.md). Availability
requires a revision containing its independently reviewed implementation merge;
receipts before that merge establish only the implementation under review.

The private `runtime/cordis/` npm package uses stock **Cordis 4.0.0-rc.10**,
integrity `sha512-xG90nPNQxR272cC4lR/m5LHevegIJvdddQBlKdEAdGz3n+zgH5lsgkg8o9fc2P3T/f+pO5D7FN1HZvkNBiABnw==`,
**npm 11.8.0**, and **Node 22.x from 22.12.0 or Node 24.x**. The frontend and
docs retain their own manifests and lockfiles. Install this package's locked
dependencies explicitly with that npm version (`npm ci --ignore-scripts
--omit=optional --no-audit --no-fund` inside `runtime/cordis/`), then build through
the managed command. Missing dependencies are a setup condition, never a reason
to install packages during app startup. The installed toolchain and package are
reviewed trusted application code, not a sandbox for arbitrary same-user code.
The lockfile SRI verifies stock package bytes during `npm ci`; the reported
`package_digest` binds host files/build and lock metadata, not unpacked
`node_modules`. Runtime trust includes that reviewed installation and excludes
malicious code changes by the same user.
[Stock Node 22.12.0](https://nodejs.org/en/download/archive/v22.12.0) bundles npm
10.9.0, so install **npm 11.8.0** explicitly before this package setup (for
example, `npm install --global npm@11.8.0` in your selected operator toolchain;
see [npm's explicit-version/global install contract](https://docs.npmjs.com/cli/v11/commands/npm-install/)).
Put that installation's actual `bin` directory before version-manager shims on
`PATH`, so `npm` resolves to its trusted `npm-cli.js` file, then use the
managed build command below with the absolute reviewed Node 22 executable.
The build checks its bundled npm first and falls back to a separately installed,
trusted JavaScript CLI on `PATH` only when it reports exactly 11.8.0. It executes
that CLI through the reviewed Node binary, never through a shell wrapper.
Missing or wrong npm stays a visible setup block; the managed build installs
nothing. Node 24's matching bundled npm remains preferred.
Managed version checks and builds pin npm's documented
[`--prefix`](https://docs.npmjs.com/cli/v11/using-npm/config/#prefix) to this
package, excluding parent workspace configuration. A package `.npmrc` or a file
at either reserved `.absent-user-npmrc`/`.absent-global-npmrc` path (including a
symlink) blocks the build with `npm_configuration_unreviewed`; remove that
unreviewed configuration before retrying. Operator npm configuration is not
loaded by these managed commands.

```bash
./manage.sh -e dev cordis status
./manage.sh -e dev cordis build
./manage.sh -e dev cordis probe
```

These finite commands accept `--node /absolute/path/to/node` for an explicitly
selected reviewed binary, do not persist the choice, and do not load `.env.dev`,
provider credentials, or the operator workspace. `status` is package preflight,
not live readiness. `build` uses already installed pins with no installation or
network fetch. `probe` starts, checks, quiesces, shuts down and positively reaps
the same production host without starting the API, database or provider. The
ordinary app remains managed through `local run/up/down/status`; its lifespan
automatically attempts this optional host only against the fixed reviewed
profile and validated build. Missing or unsupported Node (including 22.11), a
missing/stale build, invalid configuration or a failed child blocks dependent
Cordis readiness while the existing Python core and settings remain usable.

Each explicit `/api/runtime/status` refresh reads the current owned child's
required-service readiness over the same bounded control pipe, with one original
deadline of at most four seconds (below the browser's five-second budget).
`readiness.state=verified` and its Unix-millisecond `checked_at` describe that
successful readback only. Cached diagnostics are `unknown`; failed, expired or
capacity-blocked refreshes retain historical details without claiming Ready.
There is no periodic poll. A disposed required plugin fiber fails the child's
actual readiness check, and a hung child loses readiness within the deadline.

Python owns one directly spawned trusted child using a fixed absolute entrypoint
and working directory, closed inherited descriptors, and only `LANG=C.UTF-8`
and `TZ=UTC` in the child's environment. It inherits no `NODE_OPTIONS`,
`NODE_PATH`, home/config paths or credentials. Two anonymous pipes carry the
protocol; bounded stderr is separate. There is no listener, discovery, tunnel,
dynamic import, runtime installer or hot reload. The literal profile allows only
release-pinned reviewed plugin IDs and closed configurations/dependencies.
Profile changes require drain/dispose/restart and cannot grant authority.

Frames are four raw big-endian uint32 length bytes followed by 1..1048576 UTF-8
JSON bytes without a delimiter. Both sides reject duplicate keys, unknown
fields/methods, malformed or incomplete frames, nonfinite values, depth above 16
and more than 4096 nodes. Integer fields use actual safe JSON integers. Each boot
has a fresh 32-byte random nonce, exact package/composition digests and independent
strictly consecutive sequences starting at 1. Request IDs bind to those sequences;
responses require a matching unresolved request and identity/deadline. The closed
envelope is `protocol`, `boot_nonce`, `request_id`, `seq`, `kind`, `method`,
`invocation_ref`, `composition_epoch`, `composition_digest`, `package_digest`,
`deadline_at`, and `payload`. Unix-millisecond deadlines bound controls to 5 seconds
and unresolved calls to 32. Stderr is limited to 64 KiB per boot.

Only `bootstrap.hello`, `runtime.ready`, `runtime.status`, `runtime.quiesce`,
`runtime.shutdown` and `invocation.cancel` are admitted here. Lifecycle controls
have null invocation reference and null composition epoch. Cancellation requires
a bounded nonempty invocation reference and returns false for an unknown
invocation. Service methods and streams are absent until their separately owned
typed-service contract is implemented; no ownership epoch, second authority,
job queue, inference lane or agent loop is created by the host.

Admission closes before shutdown. The parent drains/cancels within 10 seconds or
the original earlier deadline, then uses TERM, a 2-second wait, KILL and positive
reap when required. Each owned resource is registered before acquisition.
Application resource cleanup is recorded independently from Cordis plugin-fiber
disposal, because upstream contains disposal errors and root-fiber disposal is
a restart operation. Unknown cleanup remains blocked and retains ownership;
restart cannot erase it. Current host resources are process-local, so positive
OS reap proves their pipe/timer/listener destruction even if graceful Cordis
disposal was unconfirmed.

The authenticated `/api/runtime/status` exposes the redacted `cordis_runtime`
snapshot as a **lifecycle_host**, including actual profile/plugin readiness,
recovery reason and independent cleanup/disposal state. Settings shows this state
without disabling artifact controls during host or metadata failures. It exposes
no nonce, PID, stderr, credentials or environment values. This host does not
change the effective `chat_agent` route or the Python owners of authority,
canonical storage and the shared serial inference lane.

Focused implementation checks cover real stock Cordis service registration and
dependency loss, framing/parser attacks, stale boot/replay/unsolicited output,
bounded admission/deadlines, stale builds, unknown cleanup and repeated actual
child reaping. A keyless managed Linux x64 probe was also executed with inherited
IPv4/IPv6 socket creation denied. This is Linux-host lifecycle/security proof,
not a native macOS receipt or inference/provider proof. macOS remains a peer
core-host target; unsupported optional native proofs do not block independent
core operation.

## Task conversation continuity

The bounded [#1021](https://github.com/seraph-quest/seraph/issues/1021)
continuation journey is **Shipped** in this milestone contract. Availability
requires installation of the reviewed milestone merge. The chat list includes a
task picker. Local context shows canonical status,
bounded task title and remaining intent, the current blocker, verified output
references, selected private reference counts, unanswered input,
the next permitted local review action and unresolved effects separately.
Missing narrative summaries use a bounded factual canonical timeline; no model
summary or provider call is needed. Open Work for full history and output controls.

**Continue in new chat** creates an owned conversation referencing the same
task ID. It does not create another task or change its revision. Selecting
historical Task and Goal records through ownership recovery permits read-only
context in a new login; historical transcripts and unselected artifacts remain
private. Continuing a recovered task renews no grant, approval, budget, effect or
model-egress permission. Current scope review remains required in Work.
The assistant context status reports that boundary separately. Existing direct
and agent chat compilers share one current-authority helper: only the current
Root's owned task facts and permitted opaque references enter the next prompt.
Selecting history grants no assistant access to its facts or sources. A selected
source reference requires current packet adoption and current per-source model
permission; private-source classification is independent from model permission.
Up to four corrections recorded through the authenticated operator comment
endpoint are readable locally with their canonical references. The packet checks
original task owner/Root authorship and the matching canonical provenance event
and current body integrity. Worker, review and older unclassified comments remain
neutral task notes in Work. Correction bodies and integrity hashes remain
local-only; the assistant receives their opaque IDs and timestamps plus an
explicit local review requirement. No correction body, source body or historical transcript is
copied into the task handoff. Ordinary current and recent chat transcript context
also requires the live current narrowed chat principal, current owned
conversation and model grant. Unscoped service/report calls receive no transcript
context; historical Task selection selects no transcript.
Deleted or changed output bytes invalidate the displayed reference. A stale
revision prompts context reload without replaying the previous chat action.
Recovery rollback removes access on the next context read.

Focused proof: `test_task_conversation_continuity.py` covers authenticated
SQLite links, replay, stale revisions, owner conflicts, selected recovery and
rollback, local artifact readback/deletion, bounded metadata and additive
migration. Literal intercepted async and streaming transports confirm the same
factual handoff and request digest as the agent context compiler; missing grants,
revoked Roots and recovered history block assistant context. Canonical evidence
adoption/revocation controls which source refs are included without copying text.
`SessionList.test.tsx` covers mocked continuation and visible recovery.
These provider-free mechanical checks establish no model-quality or live-provider
claim. The Python lifespan owns service activation and shutdown; this journey
does not introduce a Cordis bridge.

## Finite public goal programmes

The finite authority controls under [#1003](https://github.com/seraph-quest/seraph/issues/1003)
and [ADR-027](./decisions/027-standing-public-goal-programmes.md) are available
on `develop` revisions containing the independently reviewed implementation
merge. This section describes that post-merge control scope, which does not
include discovery or digest execution. Supply a separate public
brief locally, preview the exact public-web/local-artifact/inference ceiling,
and explicitly accept a daily programme lasting at most seven days. Private goal
text is never copied into this brief automatically. No query, URL or output path
is required. Zero budget or missing provider policy shows a blocked programme.

The original finite grant may outlive browser logout/expiry. Pause/revoke or
stable identity revocation stops new contacts and adoption; Goal/route correction
requires renewed review. Expiry produces a passive review state.
Saving a preview with a changed public brief immediately pauses the old
programme; abandoning that review does not restart it. An unchanged-brief
renewal pauses its predecessor on acceptance. A new login
cannot renew old authority; exact pause/revoke needs separately acknowledged
stable-owner recovery. The authority milestone alone does not run discovery or
produce a digest.

The public discovery executor under
[#1004](https://github.com/seraph-quest/seraph/issues/1004) is **Partial** until its
whole native execution and recovery implementation is independently reviewed and
merged. Its current Python owner accepts one occurrence for the current UTC day,
using the original finite programme, native job queue, artifact owner and cost
ledger. There is no catch-up after downtime. The fixed path plans at most three
queries, searches the fixed DuckDuckGo HTML route, selects at most four manifest
items, and prepares a cited local brief and inert checklist. Each occurrence has
the original deadline of at most 300 seconds and at most four governed inference
requests. The programme ceiling includes every daily occurrence and accounting
period, including unresolved reservations and contacted Unknown work.
Narrower accepted plan limits apply to the actual query, result, source and
inference counts and declared output bytes. Search uses each plan's per-request
time and response-byte cap within the original deadline. The fixed four-stage
graph consumes whole original physical outputs; alternate graphs and JSON
pointers are unsupported rather than silently ignored.

Use **Inspect discovery runs** to inspect canonical status, freshness/coverage,
held effects and cost liability. Selecting a completed brief performs an explicit
local readback under current owner and original generation checks. Private Goal
text is excluded from the public model/search path. Source quotations remain
untrusted data; mechanical citation/span verification does not establish semantic
truth. Full original brief and omitted source-line coverage stay visible. A brief
that cannot fit the existing bounded request becomes unsupported before contact;
source limits never silently truncate stored evidence. An unchanged later source
set produces a quiet brief. Prepared checklists are local proposals requiring
separate acceptance; discovery records `no_learning` and performs no external
mutation or digest delivery.
The inspector retains finite search-block reasons such as `search_captcha` and
`search_markup_drift` after authenticated owner recovery and lifecycle restart.
It exposes no raw query or response HTML and offers no provider replay. Complete
HTTP responses have their own physical body digests, distinct from derived
manifest and normalized-source digests. A known parser failure can have settled
readback while the original occurrence remains held for review; a timed-out or
oversized transfer keeps its unresolved contact evidence.

Explicit revoke, replacement generation, Goal correction or expiry may negatively
close an occurrence only when it was never claimed, has exactly its two initial
local readbacks, and has no cost row or contact evidence. The native cancellation
writer rechecks the exact original binding and denial cause. Browser logout is
not a cancellation cause. Claimed, malformed, Unknown or cost-bearing occurrences
remain visibly held for inspection; new authority does not replay or forgive
them. Unadopted staged files grant no contact authority. Missing, partial or
altered files block adoption rather than being overwritten as successful output.

Isolated Linux tests exercise Auth, explicit programme acceptance/logout,
canonical SQLite admission/accounting, the actual scheduler callback, owned
literal-loopback HTTP search/source responses and scripted governed inference
transport. They prove mechanical execution/readback and failure boundaries;
provider availability, real inference quality/spend and native macOS execution
remain unverified.

Isolated Linux SQLite/ASGI checks verify the finite configuration/control path,
logout/restart clocks, owner recovery and fail-closed correction boundaries.
They make no provider calls and establish neither macOS execution nor model
quality. The native contact/adoption executor still requires its separately
implemented and reviewed integration before activation.

## Daily programme digest and follow-through

The [#1005](https://github.com/seraph-quest/seraph/issues/1005) implementation
adds a passive daily programme digest and finding dispositions to Home and the
Guardian Inbox on `develop` revisions containing that milestone merge. Earlier
revisions require the independently reviewed merge; feature-branch receipts do
not establish their availability.
The existing Python scheduler considers the current operator IANA local day
after 08:00. A durable stable-owner/day receipt survives restart and Goal
deletion. Its bounded pending phase lets an original discovery finish before
finalizing the same digest; an expired cutoff, missing source, changed authority
or unresolved original work produces passive recovery. Missed days do not run in
a catch-up burst. Only the exact original current UTC source occurrence is
staged; unresolved current work never falls back to an older successful output.
Finalized-day ticks do not reopen historical programme artifacts. This adds no
inference, source fetch, execution queue or cost
ledger and does not change discovery's original UTC occurrence or deadlines.
Missing, unreadable or altered physical source files finalize the same receipt
with passive blocked recovery. Digest and status reads remain available with
unavailable source/output metadata; cited brief reads return a bounded review
reason. They do not create tasks or reopen an older successful source. An opted-in
deadline check retains a bounded negative memo, so unchanged ticks do not repeat
the failed physical read or consume a deadline-notice slot.

Inbox delivery is the default. Native notices need a separate explicit opt-in
and an already reviewed programme notification allowance. Across all programmes,
one owner can reserve one digest notice and one additional cited deadline notice
per local day. Canonical Goal quiet windows and focus/observer interruption gates
still apply. Slot reservation and the recipient-bound native outbox insert share
one SQLite writer. Disable/revoke before commit prevents admission; the daemon
claim also rechecks current consent, recipient, original day/generation and quiet
hours. Ambiguous display consumes the slot and is never automatically retried.
Deadline notices require an explicit ISO timestamp labelled `deadline:` or `due:`
in a physically reopened cited source span, within 48 hours, plus a matching
operator-declared category. Model urgency and finding prose supply no authority.

Use the same finding card to reopen its original brief/checklist, prepare a local
follow-through proposal, defer until a date, or dismiss it. Preparation checks the
original Goal revision, programme generation, artifact readback, current Root
and source age (at most 48 hours), including the final C1 publication writer
after physical staging. Expiry there creates no Task or bound proposal artifact;
private unbound staging remains subject to the existing input size bound and expiry
cleanup. It creates an inert existing C1 general-task
Triage card with a fixed local checklist plan, zero inference calls and no
external mutation. `FollowThroughIntent.task_proposal_id` names that canonical
Work task card; it is not a second proposal row or a new allowance. Review and
accept the plan separately in Tasks. Completion links the verified physical
local output back to the original finding. Corrections, missing/altered artifacts
or expired sources block preparation; a selected recovered Goal remains
read-only. All new dispositions, including defer and dismiss, require current
original Goal ownership and fresh source authority again in their final writer.
Private follow-through reads and exact action replay require that Goal's current
read scope; a recovered Root must explicitly select it. Defer/dismiss and
action-key replay/conflict survive reload. Operator
disposition receipts are limited to 128 per owner/day and 16 KiB each, without
evicting history to manufacture capacity.

Home reports actual native run/source/output receipts and remaining finite
allowance, including unresolved accounting reservations. **Next digest** is the
local 08:00 schedule. **Next source eligibility** shows current eligibility or
the next UTC daily occurrence when the active grant, allowance and running
discovery scheduler support it. Native outstanding work and unresolved costs
show a hold; inactive authority, an unavailable or paused scheduler, exhausted
allowance and expiry before the next occurrence show an explicit reason.
Eligibility is not an execution time: the existing scheduler, native admission
and current authority determine whether execution proceeds. Pause/review uses
existing programme controls. No activity
is invented when a source or programme is blocked.

Isolated Linux checks cover SQLite clock/DST/restart and cross-programme caps,
native opt-out/quiet claim fences, actual cited-deadline delivery slots across
two programmes, literal local HTTP discovery, a current-Root
finding-to-C1 proposal/acceptance journey, actual checklist file/readback and
same-card completion. Scripted inference replaces only its owned transport;
there are no real provider contacts or spending. These receipts do not establish
model quality or native desktop display on macOS.
Authenticated journeys also unlink, alter and truncate actual source files and
verify degraded reads, the original pending cutoff and no unchanged-tick replay.

## Optional NEAR HTTPS text question

[ADR-025](./decisions/025-near-https-text-inference.md) defines the optional
native text capability implemented under [#959](https://github.com/seraph-quest/seraph/issues/959).
It is available on `develop`, disabled by default. Its keyless managed and
intercepted receipts do not establish live provider availability or model quality.

In Settings, configure the optional NEAR text route with its dedicated write-only
credential and explicitly acknowledge provider plaintext access. The fixed
model is `z-ai/glm-5.3-flash` at `https://cloud-api.near.ai/v1`. The adapter uses
ordinary HTTPS certificate validation; no live provider TLS-handshake proof,
verified TEE or end-to-end encryption claim is supplied by the local receipts. A local `configured`
label reports credential, current consent and accounting readiness only.
The request reserve is a local hold rather than a provider-enforced maximum.
NEAR shares the deployment ledger, budget ceiling, serial inference lane and
global egress revocation fence with OpenRouter.

Use Work to submit one private text question against an active owned Goal and
finite purpose authority. The browser does not retain the question for automatic
replay. Review the native job and open its private result through current-owner
readback after authoritative billing settlement. The task requires human Review;
success does not approve it or change memory: the outcome records `no_learning`. Missing or invalid billing
withholds the answer and keeps the original liability visible. Restart and idempotent
request recovery preserve the original job without resending inference. Manual debt
settlement cannot recover discarded output or grant result adoption. Revoke or
expired authority blocks private reads; configuration alone grants no execution.

## From a goal to a verified result

The [#899 program](https://github.com/seraph-quest/seraph/issues/899) extends the
existing cockpit with a connected operator journey: keep an owned workspace,
choose a finite Goal, inspect effective permissions, queue a typed task, review
an exact approval where required, and open its verified result. Tasks, native
jobs, artifacts and memory keep their existing canonical owners. Availability
depends on the installed `develop` revision containing the complete reviewed
program merge; an open feature branch or configured adapter alone does not
establish Shipped availability. [Development Status](./STATUS.md) records scope
and remaining proof boundaries.

Start with the guided first result and use the Work board's attention and task
inspector controls to inspect approvals, source evidence, output and recovery.
Evidence working sets make citations inspectable; reviewed dependencies prevent
stale or conflicting evidence from silently driving later steps. Procedure
recommendations require explicit outcome feedback and separate adoption.
Ordinary successful execution does not grant learning authority. Deterministic
outputs and connected private actions can record an explicit `no_learning`
result without a model request.

Settings separates CPU/core readiness from the optional OpenRouter route and
execution adapters. Missing model setup stays configuration-required; no local
GPU/model fallback is selected. Repository repair, publication, literal document
comparison and reviewed package execution use their declared finite profiles.
Optional Linux sandbox availability is checked separately from portable core
operation; local host-user execution is not isolation. macOS native/companion
execution remains unverified where no platform receipt exists.

Connected controls make narrow effects explicit: one reviewed Gmail reply or
owned nonrecurring timed Calendar reschedule without attendees, a paired Telegram
task control, or the fixed Forgejo issue-title transaction. A lost response keeps
the original Unknown receipt; a separately permitted read-only observation cannot
replay that write.
Local Forgejo acceptance does not authorize production Codeberg, and local
revocation does not prove site logout. The optional Chromium companion attaches
only deliberately selected ordinary text after offline preview/redaction,
finite Task/Goal permission and exact native approval. Its private attachment
is not screenshot observation, automatic memory or generic model context.
Review the selected content: protected-surface checks cannot identify every
sensitive webpage. Recheck current permission before private reads, and use
the original receipt's bounded cleanup/recovery control rather than recreating
uncertain work. Continuity recovery never renews old execution authority.

The optional selected-text companion implements the bounded profile in
[ADR-022](./decisions/022-portable-selected-text-context.md) and
[#926](https://github.com/seraph-quest/seraph/issues/926). Its branch implementation
has an actual Linux Chromium journey: deliberate ordinary-page selection,
offline deletion-only redaction, exact Task/finite Goal permission and native
approval, signed send, encrypted private readback and capture-owned discard.
Settings uses the existing optional device pairing; legacy pairings require
explicit rotation. The Task inspector lists metadata and performs fresh current
authority checks for private reads. No screenshot, model analysis or learning
is created. Missing companions are locally blocked; macOS companion execution
has not been verified. Each target lasts at most 120 seconds, each text is at
most 32 KiB, and the owner quota is 64 charged captures / 2 MiB. Unsettled
physical publication or uncertain cleanup retains its charge and reports
`blocked_cleanup`; manual retry never renews the original authority or deadline.
Availability follows the reviewed installed revision described above.

Reviewed authored capability packages implement the bounded profile in
[ADR-020](./decisions/020-reviewed-authored-capability-packages.md) and
[#924](https://github.com/seraph-quest/seraph/issues/924). Static author/validate
commands produce data-only contracts; execution requires exact package review,
approval and activation through the existing optional isolated JSON Work job.
The first package summarizes a bounded time ledger. Unsupported execution
profiles remain visibly blocked while the shared core and static package review
remain usable. Actual Linux sandbox receipts do not establish macOS isolated
execution. Package installation alone never grants execution authority; profile
availability follows the reviewed installed revision described above.

Private invoice PDF/CSV comparison implements the bounded profile in
[ADR-017](./decisions/017-private-bounded-document-comparison.md) and
[#923](https://github.com/seraph-quest/seraph/issues/923). Its one bounded literal
line-total comparison produces cited formulas and private derived output.
Linux receipts cover private ingestion, native child supervision, readback and
the cockpit flow. Structural checks do not certify files malware-clean, and
literal comparison does not establish general document understanding. Missing
or ineffective parser resource limits block this profile; macOS native execution
remains unverified. Availability follows the reviewed installed revision above.

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
**Scope:** current `develop` baseline and its named partial proof boundaries;
new target architecture is owned by the Constitution and ADRs

### Bounded general document reading

**Status:** Partial; availability follows the independently reviewed installed
revision containing the milestone. [ADR-031](./decisions/031-bounded-general-documents.md) owns the
separate `document.read.v1` target; invoice comparison above remains unchanged.

The finite local profile selects one PDF, DOCX, XLSX or UTF-8 CSV against a
current bounded Goal. Reserve its exact byte length/digest, stream the original
to the authenticated document-source endpoint, seal its encrypted readback, then
request one explicit bounded selection. The private response includes physical
PDF page, DOCX paragraph/table or spreadsheet cell references and the immutable
original digest. Spreadsheet formula literals and cached values remain separate;
cached freshness is unknown and nothing evaluates a formula or macro.
Array-formula anchors retain the exact literal text and their inert cached value;
data-table formulas or formula objects without expression text explicitly block
the selected extraction, with no object representation returned as evidence.
Scanned, protected, malformed, external-link and oversized sources show unsupported state
and recovery instead of invented content. Private evidence never enters generic
task events, and selection grants no source/model egress or learning authority.

After a successful private read, select at most 16 individual cited paragraphs,
pages or table cells and acknowledge their local use. “Propose local preparation”
persists an inert one-step Work task; review and accept it in its existing Work
card. This path makes no model call and grants no model egress. After execution,
explicitly open the authenticated private preparation from that same card,
including after reload. It presents only the exact selected literal excerpts
with citations and separate inert formula/cached-value fields; cached freshness
remains unknown. The complete private view is capped at 16 KiB and is never
truncated. Missing or changed source/Goal/Root, incomplete native execution or
unverified readback blocks that view. Ordinary artifacts contain references and
digests, never these excerpts. Response loss offers an explicit retry of the
same proposal key; selection changes clear it and do not automatically create,
accept or execute another task.

The source index preserves redacted owned reservations after reload. Explicit
deletion requires the current revision, tombstones first and frees charged quota
only after exact private cleanup. It shows whether this host's actual bounded
cross-process upload-lock proof is ready; an unproved, stopped or changed
filesystem profile blocks new uploads with a readable recovery state. Restart
the managed document service to obtain a fresh local proof. That runtime check
applies on each peer host; this receipt does not establish native macOS execution.
Interrupted uploads show their upload writer kind and a separate cleanup-only
reconcile control. Recovery must exclusively acquire the original generation/
slot/nonce/source/owner-bound private lease inode before releasing the writer;
it never retries, seals or adopts partial bytes. A held, missing or replaced
lease remains charged. Unknown parser cleanup holds the host slot
across new requests and restarts. Original-reader reconciliation accepts only its
private nonce/job/digest/generation/PID-bound positive wait witness; a reap
without recorded output does not reconstruct evidence. The same source family
reuses current canonical input-artifact ownership, encryption and quota owners;
there is no second store or queue. Independent original deadlines and positive
supervision bound two lifetime attempts. Local extraction makes no provider call.
Parser prelaunch filesystem failure does not publish a host slot. After positive
reap, evidence adoption rechecks current Root and exact active Goal revision in
the same canonical writer; stale authority releases positively closed parser
capacity but adopts no evidence. A cancelled or unadopted encrypted candidate
can remain cleanup-required until exact private deletion, with quota retained.

Linux local format/process receipts establish only their checked mechanics.
macOS has a named native network-denial adapter but no native execution receipt;
unavailable resource or network confinement visibly blocks this optional profile
while the CPU core stays usable. This profile establishes literal extraction,
not OCR, malware certification, model understanding or general usefulness.

### Explicit local document builds

**Status:** Planned target with #1011 feature-branch implementation; installation
and independent full-journey review determine availability. The generation
subsection of [ADR-031](./decisions/031-bounded-general-documents.md#explicit-operator-authored-generation)
owns this contract separately from literal source extraction.

In Work → Describe a task, select **Build a local editable document and PDF**.
The editor authors report, brief or workbook fields and
validates their finite specification. Optional citations choose exact adopted
source leaves; private discovery and selection reuse the original evidence.
Create a private immutable build, review its signed specification/source/limits,
prepare its inert one-step task, then reload the task-bound review and explicitly
accept it through Work. Edits require a fresh immutable build and review. This
path does not compose with a model or automatically repair content. The retained
private-build list restores the original charged build and Task after reload;
discarding an unbound staged build requires an explicit retirement request.

One original C1 native child renders editable DOCX/XLSX and direct PDF from the
same validated specification. Its encrypted private build reserves 24 MiB in
addition to any source charge. Genuine queued comparisons/builds share priority
ordering and the source/comparison/build families share durable process capacity;
Unknown or unreaped writers block new launch. A denied build retains its original
queued child without claim/contact. Workbook literal strings remain strings;
only separately declared supported formulas are calculated in the generated file.
Imported formulas remain inert evidence.

Outputs appear on the same Work task only after original native success, positive
original supervisor closure and current-owner physical readback. The original
invocation must positively close stdin, consume complete bounded stdout EOF and
wait on its supervisor before issuing its private persisted supervision proof.
A parser witness alone or missing outer receipt after restart retains Unknown
and capacity; cleanup cannot reconstruct a callback from status or PID absence.
Physical capacity release does not settle Task, effect or cost state.
Authenticated downloads use fixed MIME
types and server basenames, with no-store/nosniff responses. Missing PDF retains
the verified editable output and a bounded warning. Output publication reserves
exact pending inventory before filesystem work and verifies actual private bytes
outside the final native writer. That writer consumes the source-issued private
readback proof and exact current authority/revision bindings without opening or
decrypting output files. A missing or changed proof retains the charge. Readback
and SQL are not filesystem-atomic: same-user replacement after the read remains
a local race, so later downloads and retirement revalidate physical bytes.

**Retire generated files** preserves separate retirement predicates. Successful
or degraded output requires the original terminal Task, positive supervision and
verified physical output. Outputless retirement instead requires the protected
`fully_cancelled` stop: blocked Task with native-cancel reason, ended cancelled
Attempt, cancelled parent and complete settled original child/effect/cost set,
with no adopted output. A never-launched build still needs that same protected
full stop before Goal deletion or the original cutoff; zero contact alone is
insufficient. Later current authenticated cleanup under Goal drift or expiry can
only reduce retained capacity. It grants no preview, download, renewed execution
or automatic replay.

Retirement tombstones reads before checking the exact fixed encrypted and pending
fragments, unlinking and fsyncing, then returns quota only after the unchanged
original owner/revision CAS. Missing/foreign files, partial unlink and Unknown
closure keep the charge and show recovery. The original source and Task audit
remain retained. One cleanup reconciliation reread is bounded to 4 KiB inside
its writer; a stalled filesystem can still stall that writer. Build metadata
reserves finite supervision headroom before launch (1,781-byte private codec
inside the 8,192-byte metadata ceiling). The shared historical inventory blocks
at its 4,096-row ceiling without eviction or a recovery bypass. The sequential
journey reaching a 4,097th historical row remains unexecuted; finite overflow
checks are not that proof.
Actual CPU/Linux receipts do not establish native macOS execution.

> **OpenRouter inference contract:** the integrated #736/#775 foundation
> routes ordinary text, vision, and embedding work through the governed OpenRouter path
> and removes the GPU/model-server/VLM wrapper prerequisite. The historical GPU
> topology below remains documented as pre-#775 `develop` evidence and rollback
> diagnostics, not a startup dependency. Named missing platform/provider receipts
> remain unverified; integration does not establish them.
> The target contract is defined by [ADR-006](./decisions/006-openrouter-only-inference-phase.md).
> [ADR-025](./decisions/025-near-https-text-inference.md) permits only the separate
> optional NEAR HTTPS text capability described above.

This is the short operator-facing description of the current application. For
the target product and locked decisions, read the
[Project Constitution](./00-project-constitution.md). For exhaustive shipped
detail, read [Development Status](./STATUS.md).

## Governed OpenRouter topology

```text
Seraph frontend       http://127.0.0.1:3001
  -> Seraph backend   http://127.0.0.1:8004
  -> OpenRouter       https://openrouter.ai/api/v1
```

The backend and canonical workspace remain local on a CPU-capable host.
[ADR-008](./decisions/008-portable-core-and-consented-context.md) defines macOS
and Linux as peer core-host targets. This accepted target does not establish
new platform-specific capture or execution readiness; each selected optional
adapter/profile must report its actual proof and availability. Ordinary
text, vision, and embedding inference is admitted through the governed
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
one qualified `provider/model` ID and explicit upstream allow-list for each
enabled purpose: text, vision, and embedding. Each purpose has its own
capabilities, temperature, output cap, timeout, and request-cost bound; an
absent or disabled purpose has no invented selection or fallback. Text covers
chat, reasoning, reports, and memory synthesis. Screenshot understanding uses
vision; vector operations use embedding. The three profiles share one key,
finite deployment ceiling, lifecycle witness, and serial admission lane.
Persisted controls govern caller bounds on every resolution; legacy environment
controls cannot override a saved v2 slot. Vision and embedding require separate
explicit purpose consent and zero data retention. Fallbacks remain disabled.

The API-key field is write-only. A supplied key is stored through Seraph's
encrypted vault and the response exposes only `credential_configured` and a
short fingerprint plus the non-secret credential reference. Leaving the field
blank preserves the existing server-side reference and fingerprint. Credential
and configuration updates first publish the complete target as revoked, then
install the key and review the existing accounting ceiling/reserve, then
activate the same target only against the exact intermediate revision. A
concurrent save returns HTTP409. Refresh current settings, review retained
edits and acknowledgments, and save explicitly; the browser never retries a
mutation against another backend. An interrupted save remains revoked or
continuity-degraded until explicit reconciliation and re-save. If final
publication is uncertain, credential compensation first reads the actual
configuration and witness; an exact active target keeps its new key. The browser does not retain
the field, and saved configuration and status payloads contain no key value.
With no key, status is explicitly
`configuration_required` and the route is not silently usable. On restart the
backend hydrates a vault-backed credential before resolving the first route;
if the vault is unavailable or empty, the route remains blocked.

Independent review identified malformed saved v2 controls or purpose consent
that could interrupt settings and status reads. Strict validation now reports
`degraded` / `configuration_unreadable`. Reading this configuration preserves
its saved bytes, performs no automatic repair or activation, and makes no
provider request.

Saving and reading setup metadata never call OpenRouter. The existing manual
canary targets an exact enabled purpose profile and returns capability proof,
including measured embedding dimension, without retaining the generated vector.
Status shows each purpose's model, readiness reason, and proof expiry. A
vision-only edit retains an unchanged text profile's proof; queued requests
still recheck the current global policy epoch before contact. Legacy v1 reads
preserve their exact capabilities and existing consent without rewriting files
or manufacturing proofs. A projection incompatible with a v2 slot is visibly
blocked until explicit reviewed save; stored v2 rejects legacy writes with
HTTP409. Local tests intercept provider transport; real availability, quality,
costs, and embedding usefulness remain externally unverified.

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

Home also has a bounded **Your first verified result** journey: choose
a deterministic local snapshot or public-source baseline plus snapshot, review
the $0 model cost and finite permissions, queue a typed task through the managed
dispatcher, and explicitly open its independently verified result. Progress is
owner/session-scoped in the existing activity ledger. The public watch starts
with scheduling disabled and is paused after observation. A baseline is not a
material-change dossier; both starters record `no_learning`. This remains
Partial within its declared profile limits. The managed keyless local journey and
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

The bounded #920 profile adds **Send neutral Telegram notice** in the current
task's inspector, available with the installed reviewed program revision.
Pair the operator/chat and grant Telegram transit consent in Settings
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
Availability requires the installed reviewed program revision; it never restores prior grants,
adopts ambiguous legacy history, or substitutes a retired execution root.

The task inspector also exposes local, goal-scoped
[evidence working sets](./task-evidence-working-sets.md). Citation references are
revalidated against canonical records and verified artifacts before inspection
or explicit model-context adoption. Exact selected historical records remain
read only, and historic/private source egress remains blocked for the generic
strategist purpose. Availability requires the installed reviewed program revision;
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

### Operator work board (Epic #864 M6)

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

#### Typed general tasks (#997)

Available on the installed `develop` revision containing
the independently reviewed [#997](https://github.com/seraph-quest/seraph/issues/997)
merge. This section describes the intended post-merge contract; it does not
establish model quality or complete the later interpreter and specialist-child
work in [#998](https://github.com/seraph-quest/seraph/issues/998) and
[#999](https://github.com/seraph-quest/seraph/issues/999).

The Work panel accepts ordinary intent, an owned Goal, requested output and
explicit limits. The operator does not author tool-specific forms to request a
task. Planning requires separate model-egress acknowledgment, a positive explicit
cost ceiling and the existing current policy, consent, capability and inference
admission checks. The canonical `general_task_planner` consumer uses the text
purpose, interactive workload and priority, `OPERATOR_INPUT` provenance, and
`text` plus `structured_output` capabilities. It uses the existing durable
inference accounting owner. Evidence content, credential references and tool
permission declarations are excluded from its model messages; unavailable secret
redaction blocks planning.

`POST /api/work-board/general-tasks` captures the server's current typed tool
descriptor snapshot and creates one ordinary `WorkBoardTask`. Its bounded plan
and input share an immutable private artifact. Repeating the same owner-scoped
request returns the same card without contacting the planner again. A valid
proposal remains in Triage for review. Invalid model output remains an editable
Triage card with a visible proposal error and no task execution Root. Missing
authority, route, consent or budget returns the current blocked reason rather
than silently selecting a fallback.

`GET /api/work-board/general-tasks/tools` reports descriptors, their digest and
excluded tools with reasons. `GET /api/work-board/tasks/{id}/plan` reads the
owner's plan and acceptance state. `POST /api/work-board/tasks/{id}/plan` edits
the same unattempted Triage card using exact task and plan revisions; it stages
a new immutable artifact and revokes the previous binding. The existing promote
action accepts only the reviewed current revision. The inspector keeps
acceptance unavailable while edits are unsaved. Plans contain at most sixteen
closed typed steps, literal inputs and declared dependency pointers; they carry
no executable expressions or permission grants.
Admission also proves that each registered output satisfies its step contract
and that the final step satisfies the requested output contract. Even equal
schemas require an inhabited, known safe supported contract; unsupported unique
array cardinalities block before effects. Regex matching permits only literal
ASCII patterns with optional start/end anchors, the exact SHA-256 pattern and
the native HTTP(S) URL prefix pattern. Other patterns and patternProperties
are rejected before matching, including finite enum/const candidates and input
validation. Incompatible contracts remain editable proposal errors.

Accepted work uses the existing dispatcher, `WorkflowRunState`, leases, fences,
effect journal and verified readback. Current owner, Goal, evidence, strategy
binding and descriptor policy/revision are checked before admission and each
step. Native tools retain current approval, audit and secret wrappers; MCP tools
require a complete trusted typed contract and current connection revision.
Task MCP output requires a finite schema and a bounded owned transport: raw
response bytes are capped before SDK/result JSON parsing, with strict depth,
node and serialized-output bounds. Oversized contacted output retains its
unresolved effect and cannot be adopted as an artifact or automatically retried.
The current task adapter requires a stateless inline identity-encoded MCP
response; sessionful, compressed or deferred responses and GET resumption are
blocked through the existing capability/recovery surface.
While a task guard is active, an oversized outgoing POST on that same MCP
connection is rejected before contact because its request binding cannot be
decoded within the bound. This can also reject a concurrent oversized
interactive POST; ordinary bounded interactive calls and other connections
remain usable. Without an active task guard, this outgoing restriction is inactive.
Unavailable or changed contracts block execution. An unknown contacted effect
is retained for reconciliation instead of automatic replay. Successful output
is a bounded private artifact with physical hash readback, and the card enters
Review. Completion records an explicit no-learning result; task success alone
does not update canonical memory.

A mediated tool approval pauses the same durable Root only after the existing
approval wrapper proves it has not contacted the tool. The card shows its exact
pending approval and original deadline. The exact unconsumed pending or already
approved request is bound without
changing its arguments, scope, fingerprint or expiry. One existing SQLite
writer commits that binding, the no-contact checkpoint/readback, paused Root
and blocked open attempt together. An interruption rolls the whole wait back;
an approved row without the wrapper proof is insufficient to recover a wait.
Approval alone does not queue work. The owner explicitly continues the current
task and plan revisions, original
attempt, approval, workflow revision and fence. Generic paused-job resume cannot
cross this wait. The canonical writer checks the current approval, policy,
inputs, descriptor and already verified physical outputs, then reacquires the
same attempt with a new lease fence. The tool wrapper consumes the approval
once immediately before contact. Expired, denied, revoked, stale or uncertain
bindings retain a visible recovery block.

Isolated API-to-dispatcher receipts exercise genuine current native file reads,
durable accounting, acceptance, physical artifact readback and restart replay.
Actual local MCP fixtures also exercise the current approval wrapper and
same-attempt continuation, including consecutive approvals and rejected stale,
revoked, uncertain or physically changed output bindings.
The planner HTTP response is intercepted inside the test: these receipts make no
external provider call or spend and make no usefulness or quality claim.

#### Bounded native task interpreter (#998)

**Intended Shipped scope after reviewed merge:** available on the installed
`develop` revision containing the independently reviewed
[#998](https://github.com/seraph-quest/seraph/issues/998) implementation merge.
It extends the typed task contract above. The existing Python dispatcher and
managed `local run/up/down/status`
lifecycle remain the execution owner. The optional Cordis host does not execute
this interpreter; [#1007](https://github.com/seraph-quest/seraph/issues/1007) owns
the later bridge migration.

Acceptance starts one bounded native parent and actual typed tool children.
Dependency inputs come from physically verified private child artifacts.
Admission, tool contact, approval continuation and final assembly recheck the
original Root, Goal, input binding, current policy and canonical task/attempt
fences. Verified child outputs remain separate from the parent's final readback;
only verified final assembly sends the card to Review. Artifacts remain private,
and execution records `no_learning` without a canonical-memory write.

Planning and any bounded continuation share the original accepted inference
group, call/cost allowances and absolute deadline. Only acknowledged,
budgeted planning can continue after a verified result; manual zero-cost plans
do not contact a model. Private tool inputs and output bodies do not enter
continuation messages. A paused edit uses the existing full-Plan replacement
contract through `/plan/revise`: only unadmitted rows are editable, while every
admitted, started or completed row must remain unchanged. Editing stays paused
and does not renew leases, attempts, fences, permission or deadlines.

The inspector exposes safe Pause, Resume and Cancel for native task work.
Pause requires verified quiescence; an active or unresolved original callback
blocks it. Resume continues the original uncontacted remaining work. Tool
approval binds the exact original child and manifest; explicit continuation
uses that same child attempt once under the original deadline. Missing or stale
bindings and historical root-only tool intents require reconciliation.
Restart can recover the same admitted, unclaimed child or assemble already
verified outputs. Running or Unknown effects cannot be replayed.

Cancel fences further admission and late output adoption immediately. The card
shows `pending` while the original callback remains unresolved, then
`callback_closed_outcome_debt` if its actual closure leaves an uncertain effect.
`fully_cancelled` requires positive original closure and no effect debt. When
the original Future physically returned and its output was already verified,
cleanup can settle debt using the same source-retained original root, exact
original intent, canonical call and artifact readbacks, and literal private
output bytes. This grants no new execution and adopts no parent output. A
settings change can still permit that cleanup read from the original root only
with its source witness. Missing, foreign or tampered evidence, a changed
original-root inode, or uncertain callback closure retains debt. Lease expiry
and process absence do not establish safe cancellation; they retain visible
Unknown recovery. A repeated cancellation
returns its original action receipt without renewing execution. Protected
history has fixed capacity reserved before contact; exhausted history rejects
admission early rather than losing space for cancellation and closure receipts.

Typed MCP discovery uses the stock client's original advertised input and output
schemas. The trusted declaration must exactly match the bounded closed input
schema, and current connection/policy identity remains bound at approval and
contact. Unsupported or changed metadata blocks the tool visibly. The
[three-tool integration journey](../../backend/tests/test_general_task_native_full_journey.py)
exercises actual file read/write, stock MCP JSON-RPC discovery and one approved
operation through an owned local HTTP transport, with physical private output
readback. [API recovery tests](../../backend/tests/test_general_task_native_dispatch_api.py)
cover paused full-Plan edits, original-child restart, held-callback cancellation,
original returned-readback cleanup after a workspace settings change,
full reserved history and corrupt-evidence recovery;
[UI tests](../../frontend/src/components/cockpit/GeneralTaskPanel.test.tsx)
cover exact control bindings and truthful cancellation states. These isolated
Linux receipts make no external provider call or quality claim. The contract is
OS-agnostic; native macOS execution remains unverified.

#### Bounded specialist delegation (#999)

**Intended Shipped scope after reviewed merge:** available on the installed
`develop` revision containing the independently reviewed
[#999](https://github.com/seraph-quest/seraph/issues/999) implementation merge.
It extends the accepted native task contract above, using the same Python
dispatcher, canonical Work cards and governed inference accounting owner.

An accepted plan can invoke the fixed `specialist_delegate` tool. The server
reserves the original parent/step association and publishes a real specialist
Work card with an immutable private input. Both cards expose reciprocal
navigation after canonical publication. This is informational lineage, not a
dependency edge or permission: Attempt/job identities are labeled reserved
until the existing dispatcher actually claims them. Missing or foreign cards
remain unavailable through the existing owner-filtered inspector.

When the parent has an accepted reviewed task method, the authenticated private
specialist reservation revalidates that parent's original immutable method pin.
The child executes its narrower server-reserved plan and allowed tool subset,
without selecting a fresh general method or inheriting the parent's full tool
sequence. Original owner, Goal, callback/fence, request and handoff bindings
remain mandatory. Ordinary public task admission still selects the current
method and requires its complete reviewed sequence; caller-supplied lineage
cannot authorize a narrowed plan.

The original group permits at most two active and four retained specialist
children and one specialist depth; a specialist cannot delegate recursively.
Planning and execution share the original task's allowance of at most twelve
inference calls, explicit cost ceiling and absolute deadline. No child obtains
a new inference lane, approval, Root or budget from its lineage. Selected
execution evidence is copied into a bounded private handoff under current
producer, Goal and owner checks. The child consumes that sealed copy; it does
not regain an arbitrary source path or inherit credentials. Changed source or
Vault metadata, missing or altered copied artifacts and unrelated pointers
block the governed use.

Specialist planning, stop compilation and partial-review cost readback use three
private indexed queries: one-row checks for missing or invalid classification,
then at most thirteen original-group rows to detect overflow beyond twelve.
Released, settled and Unknown history remains included; original evidence,
group, owner, Root, ordinal and deadline bindings still determine authority.
Canonical startup strictly classifies legacy rows in batches of at most 128;
malformed or unsupported evidence stays invalid for fail-closed admission.
Historical financial membership seals retain their original typed defaults and
field presence; migration neither normalizes retained evidence bytes nor reseals
existing journals. The private lookup
hint is not a grant or integrity seal against arbitrary direct database edits.

The stopped workspace CLI accepts the old schema and its sole added private
hint column for backup/restore and continuity readback. Restore leaves that hint
NULL; canonical startup migration must classify it before governed accounting
is ready. Other unexpected columns still fail closed. These bounded specialist
reads do not bound the existing global `_accounting_rows` baseline, which still
loads the deployment ledger.

The parent enters a durable wait rather than holding an active callback while
the child works. Reconciliation uses the original protected reservation and
actual child execution/readback. Restart can continue that retained wait or
adopt already verified physical child output without launching another child.
Only verified parent assembly reaches Review. A failed sibling retains the
successful child's private output and visible original accounting; it does not
convert partial work into successful final assembly. Outcomes record
`no_learning`; task or child success does not update canonical memory.

Cancel on the original root fences the complete original direct/specialist
descendant set and further output adoption. Held pause also stops admission;
it provides no new resume bridge for uncertain work. Original callbacks,
Unknown effects and contacted cost liabilities stay visible. A stop receipt,
lease expiry, absent process or reserved child identity does not prove positive
callback closure. Existing original-source closure and accounting readbacks
remain necessary for reconciliation; the partial-review action cannot supply
them or renew execution.

For eligible stopped work, the GeneralTask inspector shows producer-issued
verified specialist steps and exact private output references. Inspect each
selected artifact through the existing artifact inspector, then explicitly
acknowledge the remaining effects and accounting debt. Opening that inspector
alone is not a successful physical-readback claim. The existing task action
`accept_partial_results` binds the original task revision, Attempt, workflow,
manifest and plan revisions, selected steps and one UUID idempotency key.
The server physically verifies those selected outputs and records an immutable
`SpecialistPartialResult.v1` review artifact without changing the original
Task/Attempt, stop state or callback closure. This metadata review can occur
after the original execution cutoff; it grants no new contact.

Accepted selections remain **partial review pending debt**, never full task
success. The display separates the historical unresolved jobs/effects at
acceptance from current unresolved job, effect and cost-operation counts and
current cancellation state. Even zero current counts or later genuine stop
reconciliation cannot make that decision a full-success result or authorize
resume. An uncertain POST retains its exact original request for explicit
reconciliation; there is no automatic retry or new selection. Exact replay
after restart still requires the current authenticated paired owner before
stale-revision handling. Foreign or recovered read-only access cannot accept
or replay private decisions; changed selections, stale source and mismatched
bindings require inspection and recovery.

[Native specialist journeys](../../backend/tests/test_specialist_delegation_runtime.py),
[copied evidence tests](../../backend/tests/test_specialist_evidence_runtime.py),
[durable wait tests](../../backend/tests/test_specialist_durable_wait.py),
[stop tests](../../backend/tests/test_specialist_stop.py) and
[authenticated partial-review tests](../../backend/tests/test_specialist_partial.py)
exercise actual production owners and private physical outputs with only the
final approved model/MCP transports scripted. The
[partial UI tests](../../frontend/src/components/cockpit/GeneralTaskPanel.partial.test.tsx)
cover inspection, exact replay, owner changes and truthful historical/current
debt display. These isolated Linux receipts make no external provider call,
quality or general callback-recovery claim. Native macOS execution remains
unverified. The optional Cordis bridge remains separately owned by #1007.

The [reviewed-method specialist journey](../../backend/tests/test_specialist_reviewed_method.py)
completes a real native source task and its operator review, derives and accepts
its signed method, then executes a new parent under that immutable pin. Both
narrower children reach Done with distinct physical outputs; the new parent
reaches Review and still awaits its normal operator completion. Legitimate
continuations retain each callback's exact admitted revision and digest from
the verified finite consecutive revision manifest; the executed step must equal
the final frozen step and its private inputs must match current dependency
resolution. Missing revisions or altered source bytes block lesson eligibility.
Method compatibility uses detached descriptors from the genuinely started
dispatcher registry when available, with stage and final-writer rechecks. Those
descriptors describe compatibility only and grant no execution authority.

#### Bounded execution evidence (#917)

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
unverified. These receipts establish the bounded profile; availability requires
the installed reviewed program revision.

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

Availability of these bounded work-board profiles requires the installed
reviewed program revision. They do not claim full Hermes parity, autonomous
execution, memory superiority, or production readiness.

### Communication preparation (#1016)

**Intended Partial scope after independently reviewed merge:** this describes
the source branch for [#1016](https://github.com/seraph-quest/seraph/issues/1016).
Availability requires its implementation merge in the installed `develop`
revision. The existing Python lifecycle owns this composition; configuration
alone does not make Mail or Calendar ready.

In the private Mail review or Calendar event selection, explicitly add current
sources to communication preparation. Choose the same current owned Goal,
review the selected sources and finite cost limit, then prepare one bounded
task. Each reschedule candidate requires its selected meeting source. Existing
source-specific model consent and grants must remain current. The accepted
original task invokes the actual native Mail reply-draft and Calendar meeting-
preparation owners sequentially under its original shared inference group,
reserved limits and absolute cutoff. Internal source tasks cannot be promoted
or retried as independent work. Private source bodies do not enter the general
task planner; a new authority generation does not renew original reservations,
Unknown costs, attempts or effects.

Open the current private communication plan from its Work card. Its closed
five-field value contains exact source references, up to five reply drafts,
five meeting preparations, three reschedule proposals and bounded unresolved
questions. Drafts and briefs come from physically read-back original native
outputs. The encrypted aggregate is privately read and revalidated under the
current owner, Root, Goal, task and source permissions. Source drift hides only
affected entries and keeps their blocker visible; loss of Root or Goal authority
hides the whole private value. Historical recovered selection is read-only
metadata and grants no private-body access or fresh execution. Generic Calendar
readers remain root-only; the specialized private aggregate reader does not
expand their authority.

Select only the actions wanted, inspect each fresh exact native preview and
approve each independently. **Validate selected independent exact approvals**
checks an inert `ActionBundle`; it performs no effects and grants no collective
approval. Execute through each action's existing native controls. Mail follows
[ADR-016](./decisions/016-exact-gmail-reply-send.md): one exact send and independent
sender-Sent readback, which does not prove recipient delivery. Calendar follows
[ADR-019](./decisions/019-exact-calendar-reschedule.md): one conditional owned-event
patch and independent exact-event readback. A changed source, recipient, body,
connection or event invalidates the affected action; a conflict does not erase
another action's verified result. Unseen calendar occupancy remains unresolved
and grants no broad free/busy read.

Response loss keeps the original operation Unknown, with no automatic send,
patch, reversal or replacement. Use the original action's finite read-only
reconciliation controls. A separate current RecoveryGoal/read grant can observe
the original effect only while its original Root remains authorized and owned
transport closure is established. It cannot regrant the old write, change its
deadline or settle historical cost liability by observation. An unconfirmed
task admission retains its exact request in the current owner session's memory
for explicit reconciliation; it does not create a replacement automatically.
Preparation and actions explicitly record `no_learning`. Feedback must use a
separate governed lesson proposal.

The private view states the retention limit: access expiry does not delete
artifacts. **Clean up aggregate private plan** deliberately requests cleanup
at the exact current task revision. Only verified physical absence clears it
as deleted; unresolved deletion or response loss stays visible and retains the
original cleanup request for explicit reconciliation. Authoritative access
denial discards the private view without claiming deletion. Cleanup removes only
the encrypted aggregate, preserving original Mail drafts, Calendar briefs and
immutable approvals/effect history under their existing owners. Original
Calendar preparation briefs remain bounded local plaintext with private file
modes; aggregate encryption does not migrate that storage. No automatic TTL
deletion exists. Same-user filesystem replacement between the final inode check
and unlink remains a residual race; no atomic conditional unlink is claimed.

The [native composition journey](../../backend/tests/test_communication_native.py)
uses actual isolated authenticated tasks, source callbacks, encrypted physical
readback, separate native Mail/Calendar effects and literal provider readbacks,
with scripted inference only at its owned transport. The
[communication UI tests](../../frontend/src/components/cockpit/CommunicationPlanPanel.test.tsx)
cover independent selection, stale authority, response loss and cleanup states.
Current Linux validation still retains unresolved native process crashes
(ordinary exit 139 and guarded exit 245); one debugger run passing 24 tests does
not resolve them or establish runtime readiness. Only verified cleanup has a
captured authenticated API wire receipt; unresolved cleanup has service and UI
coverage. Live provider usefulness, native macOS execution and model quality
remain unverified.

### Task lesson drafts (proposal-only)

The supported proposal-only **Learn this** path is provider-free. It accepts an ordinary
completed or failed task with a verified current attempt and recorded native
tool steps or an exact current formatter capability contract snapshot. The
formatter draft preserves its registered identity, version and typed contract
digests; it never copies the task's JSON input or output. The source-discovery
endpoint returns the authoritative task
revision, attempt, references and Goal scope. An ordinary text correction can
draft a closed `TaskMethod.v1`: an existence check before using the source,
a verified-readback check, or preservation of source attribution. Unsupported
corrections and missing method receipts return explicit `no_change`; they are
preserved as private correction evidence and do not become executable steps.

The Inspector separates the observed outcome, explicit correction and inferred
draft, and displays the exact old and new method. `ResearchStrategy.v1` and
`TaskMethod.v1` are closed data schemas; candidate input cannot install tools,
change permissions, runtime limits, providers or credentials. Candidate content
is a private artifact; the existing `MemoryProposal` owns the inert review row,
and `EvolutionRuntime` records only content-free lesson receipts. Neither a
successful task nor a failure creates a positive preference vote. Changes to
the task, Goal, run, receipts or method steps block the old preview.

Automatic proposals require a separate per-task opt-in bound to the original
current operator, task intent and Goal revision. Enable and disable require the
exact authoritative policy revision; a stale request cannot overwrite a later
consent change. The terminal dispatcher callback
reauthenticates that exact Root without renewing it. Automatic creation uses only
the finite missing-source failure rule and is limited to two durable proposal starts per UTC
day across the owner; consent renewal does not renew that cap. It performs no
inference, source-body projection or trace egress. The callback is bounded to
five seconds and cannot undo an ordinary task's committed result. Private
staging runs off the event loop in a single retained worker. Timeout stops
proposal progression, records a content-free outcome for the exact original
attempt even after Root revocation, and retains staging capacity until the
actual worker finishes. Thread cancellation is never treated as cleanup.
After restart, recovery requires proof that the original process ended and
checks the exact private artifact; it records no-change and releases the
original slot without making a proposal. Replays and recovered starts still
count toward the daily cap.
Linux recovery uses the original kernel boot/process identity. macOS uses the
fixed native boot-session UUID and exact PID start seconds/microseconds; a
missing PID or failed native read remains unknown. A same-process retained
worker can complete even when native witness acquisition is unavailable.
Unknown restart witnesses retain capacity and expose `restart_witness_unknown`;
manual lesson review remains available. Native macOS execution was not verified
on the Linux validation host; finite ABI and recovery fixtures cover its contract.
Automatic outcomes have content-free, original-owner task events; source
discovery returns the latest matching outcome, including no-change, cap and
failure states. Automatic completion uses the canonical proposal and task event
receipt and performs no evolution-file work. Explicit inspection or manual
request replay repairs a missing evolution receipt off-loop from the canonical
proposal without another daily-cap charge; mirror work is capped at 1 MiB and
4096 entries. The canonical proposal remains inspectable if this advisory mirror
is oversized or unavailable; readback exposes a degraded mirror status and
preserves its existing bytes for recovery through the evolution owner.
Model reflection remains unavailable because no existing consent authorizes task-trace
egress. No model call or spend is needed to draft a supported lesson.

Generic memory acceptance rejects `task_method_proposal.v1`; a draft alone
never changes task execution. Isolated local checks establish these mechanics,
not learned quality or general usefulness.

The actual authenticated formatter-to-lesson failure journey is verified on the
implementation host: bubblewrap exits with `Failed RTM_NEWADDR: Operation not
permitted`, process cleanup is proven, no formatted output is adopted, and an
explicit correction creates an inspectable private inert method. The positive
formatter journey remains blocked by that host sandbox limitation; this receipt
does not establish successful formatter execution or lesson quality.

### Reviewed task methods

The [#1001](https://github.com/seraph-quest/seraph/issues/1001) capability is
available on `develop` revisions containing its independently reviewed milestone
merge. Earlier revisions require that merge; implementation-branch receipts do
not establish their availability. [ADR-028](./decisions/028-reviewed-task-methods.md)
defines the accepted boundary.

In the existing Task Inspector, inspect the original source, observed method,
explicit correction and exact typed candidate before accepting or rejecting it.
Acceptance atomically writes a signed canonical pattern and a signed active
selection for the stable operator, exact Goal revision and task family. Each
scope admits at most 16 immutable accepted versions; exhaustion requires review
and does not evict history. Current authority, source receipts, signatures and
selection revision are checked again by the canonical writer. A recovered login
can inspect an explicitly selected Goal but cannot adopt or roll back its method.
Missing, stale, revoked or tampered state is visibly blocked; it cannot silently
select baseline. A demonstrably never-selected scope retains ordinary baseline.

A subsequent general task applies supported registered tool sequences and
finite source-existence, verified-readback or attribution guards through its
existing PlanSpec, approvals and native execution. Fixed formatter capability
steps remain inspectable but unsupported for adoption by that consumer. An
explicit structured research candidate requires a completed research-dossier
task and verified original native output. When the server verifies that source,
the existing Task Inspector offers bounded research fields for query templates,
source preferences, evidence fields, draft sections and stop conditions. Review
the original Task, Attempt, Goal revision, source references and observed receipt
in both the private draft and canonical review; adoption requires an explicit
acknowledgment. Preparing a candidate stays inert. Reopening a known candidate
reads that original candidate, while an uncertain preparation requires explicit
reconciliation of the same request. Public discovery applies its approved
query templates, source preferences, evidence fields, draft sections and stop
conditions within the original finite programme grant. Research dossier itself
continues to use its explicit baseline. Candidate JSON cannot install tools,
grant permissions or change providers, budgets, credentials or runtime limits;
it is excluded from generic recall, prompts, semantic indexing and generic M5
selection.

**Rollback to baseline** changes future selection. An already admitted task or
discovery retains its original immutable version while current authority and
source remain valid. Tombstones, explicit revocation, changed Goal/source state
or expired original grants block the next owned boundary. Rollback does not
renew a contacted job, grant, deadline or allowance. The Inspector retains the
literal previous version and source for review.

Provider-free Linux receipts cover authenticated review, actual completed native
general-task source and subsequent physical output, and actual research-dossier
source followed by public discovery using its approved strategy. They also cover
future baseline after rollback, original-pin continuation, read-only recovery,
source/signature/selection tamper and revocation before contact. Scripted final
transport proves these mechanics; learned quality, provider usefulness and
native macOS execution remain unverified.

### Reviewed procedures v2 (M6 #889) {#reviewed-procedures-v2-m6-889-branch-local-target}

[ADR-015](./decisions/015-reviewed-procedure-preferences.md) accepts a narrow
reviewed-learning target: explicit feedback on matching manual invocations may
propose a Goal/version-local preference for an existing `public-browser-check`
version. The bounded implementation counts the complete matching
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
no model or account contact occurred. These receipts establish the bounded
preference profile; availability requires the installed reviewed program revision.
They do not establish measured quality improvement.

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

This remains **Partial** within the declared execution and provider-proof limits.
On October 1, 2026,
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

The #912 bounded profile adds explicit `repo-node24-npm-v1` execution behind
this same Work/API/durable-job journey. [ADR-007](decisions/007-bounded-node-repair-supervision.md)
owns its accepted contract; availability requires the installed reviewed program revision.
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

The bounded tested-publication profile implements the accepted
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
permanently rejected request. These bounded capability contracts require the
installed reviewed program revision and do not establish live GitHub/provider usefulness.

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
and this capability remains Partial within those explicit proof limits.

### Reviewed public evidence pipelines (#914) {#reviewed-public-evidence-pipelines-914-branch-local-target}

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

### Finite evidence research (#901)

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
truth. Every result records `no_learning`. This capability remains **Partial**
within its declared source and provider-proof limits. The October 3
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

### Profiled public form preparation (#1012)

`browser.interact.v2` adds a separate registered HTTPBin public form preview
under [ADR-029](./decisions/029-profiled-browser-interactions.md). The capability
is **Partial**: bounded public-form preparation and preview are available;
authenticated exact transactions (#1013), downloads and selected local desktop
draft actions (#1014) remain **Planned**.
The existing public browser v1 remains navigate/extract only. From the public
browser Work form, explicitly open the HTTPBin preparation controls, select a
current Goal, acknowledge one public document contact/site access logging, and
open the bounded job. Fill fields using the displayed current opaque nodes,
check/radio controls, and request the literal private preview. Submission stays
blocked; this public echo form does not place an order or mutate a business
object. It imports no credentials, cookies or authenticated session.

The profile pins the reviewed source bytes and exact GET `/forms/post`; any
source drift blocks before Chromium receives the document. Preparation disables
JavaScript, service workers, requests, WebSockets, downloads and popups. Fresh
DOM revisions and original Root/Goal authority are checked before every action.
At most 20 actions and 180 seconds occupy the existing shared browser lane.
Stale or ambiguous nodes stop with a fresh-snapshot requirement. Explicit
**Refresh current page snapshot** captures the original live page, consumes one
bounded action and replaces every opaque node reference. Cached history
inspection does not resolve an uncertain action. Refresh clears private input
and action acknowledgement; continuing requires a new explicit action against
the fresh nodes. At the action bound, only history and cleanup remain available.

Reload uses the read-only owned-job list or exact request-key lookup to inspect
completed/blocked history; it does not reopen a page or renew consent. Private
inputs and preview artifacts are encrypted and excluded from generic journals.
Success requires a current literal preview, private artifact readback and
positive browser cleanup. Closing without a current preview cancels the job.
Admission first requires the existing **Operator ownership and recovery**
control's **Enroll this authenticated scope** action and a live stable operator
identity. Refresh profiles after enrollment. Missing local Playwright files or
a verified native kernel boot UUID report inactive before any child starts.
The canonical physical reservation commits before the same-inode positive
marker is synced and the driver starts. A crash between reservation and marker
keeps unresolved read-only history; inspecting or replaying the request never
launches another browser or infers no-child proof from missing metadata.

Positive physical closure retains the exact lane witness until its cleanup-only
journal receipt commits. A durable CAS conflict keeps capacity reserved.
**Find physical browser cleanup** exposes only original job identity, proof
state and unchanged durable status to the same stable authenticated operator,
including after authenticating through existing ownership recovery. Explicit
acknowledgement records exact physical cleanup; it never adopts output, grants
success, renews old Root/Goal authority, retries an action or resolves an
external outcome. Exact receipt replay cannot clear a later job's reservation.

After lost owner-process cleanup, a changed verified native kernel boot UUID
proves original resources are gone; the same boot stays blocked and requires an
operator-managed reboot if no positive closure witness exists. Linux reads its
kernel boot UUID; Darwin uses the fixed read-only `kern.bootsessionuuid`.
Unavailable native proof or mismatched workspace/inode/job remains blocked.
Process absence, elapsed time and an unlocked file are never cleanup proof.
Every outcome records `no_learning`.

The local Linux Chromium/TCP/SQLite receipts exercise multi-field preparation,
select/check controls, policy denial, DOM drift, private readback and reloaded
history with external sockets/inference denied. These receipts establish those
mechanics; they do not establish a useful business form workflow or production
HTTPBin availability. macOS execution is unverified.

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

The bounded profile adds an owner- and session-bound Gmail readonly path with encrypted
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

### Scoped connected-source synchronization

Scoped Gmail and Calendar synchronization is **Planned** until reviewed
integration. Its bounded Python contract extends the existing connection,
finite source-consent, vault, canonical durable-job and private-artifact owners.
It introduces no second queue, credential-fetch surface, store or bridge.
New sync admission requires explicit enrollment through the existing operator
ownership flow (`POST /api/auth/ownership/enroll`). An unenrolled or revoked
identity returns `source_sync_operator_continuity_required` before creating a
job or contacting a provider. Native sync uses a fixed Linux boot/PID/start/namespace
witness or Darwin boot-session UUID and native PID/start-time witness. Unsupported
ABI or unavailable lifetime evidence returns `connection_sync_platform_unsupported`
before admission. Darwin parser and recovery fixtures run on Linux; native macOS
execution remains unverified. Existing source read-consents remain unchanged.

The existing Mail and Calendar connection routes own `POST /sync` and
`GET /sync`. `ConnectionSyncInput` contains only `goal_ref`, `connection_ref`,
`source_scope`, `window` and `max_items`. A request carries its own opaque
`request_uuid`. Metadata synchronization requires an explicit
`acknowledge_sync_metadata` grant; upgraded legacy consents retain a zero sync
limit. The bound is a seven-day fixed window, at most fifty metadata records
per provider/run and three pages. Gmail remains selected-label and optional
selected-thread scoped. Calendar uses at most three exact selected calendar
grants. A seven-day Calendar grant requires the explicit sync acknowledgement;
existing narrower grants are preserved. Separate private-item acknowledgement
selects at most ten bodies/details, with Gmail's existing ten-item body grant
unchanged. No sync operation admits inference, send or reschedule work.

Each connection has one fixed provider/scope digest. Changing it requires an
explicit cursor reset after the old reservation has been released through normal
completion or verified physical-only cleanup. Old unresolved effects remain
visible and unchanged. Before vault/provider contact, a serialized reservation mirrors
the existing claimed durable root; every contact, adoption and private read
revalidates the original owner/session, goal, connection, consent and vaulted
credential binding. The reservation grants no authority independently of the
canonical root's lease, fence and deadline. Provider contacts have a two-second
maximum timeout clamped to that original deadline; metadata concurrency is two
and the entire operation remains bounded by 120 seconds and original consent
expiry. Fifty is an upper cap, never a completeness guarantee.

`ConnectionCursor` contains exactly `connection_id`, `revision`, `scope_digest`,
`provider_cursor` and `last_complete_at`. The cursor and provider token remain
in an encrypted private page artifact; the connection row contains only its
canonical job/page pointer, CAS revision, scope digest and active-job reference.
Only complete page adoption with an artifact hash and durable readback advances
that pointer. Interrupted pages retain the last complete cursor and an explicit
unknown root. Exact replay cannot repeat an uncertain contact. OAuth refresh
POSTs and source GETs remain uncertain in the original effect ledger. Explicit
physical cleanup can release only the connection slot after positive original
callback quiescence or verified original process death; it does not settle the
Unknown job, adopt output, decrypt a page, advance the cursor or retry work.
Elapsed lease time is never proof of physical closure. A single rate-limit response
may schedule one thirty-second cooldown under the same root/grant/deadline;
another rate limit or an interruption stays visible for recovery. Restart does
not replay an unknown contact or clear its reservation by age. Physical-only
cleanup validates the authenticated stable operator identity and exact original
root/session/attempt/fence/reservation while preserving all Goal and output
fences. A changed or revoked Goal never authorizes private readback or output
adoption; old Unknown roots remain separately visible after a slot is released.
The fixed cleanup states are running, Unknown, cost liability, failed and succeeded.
An absent effect remains absent. Succeeded cleanup requires the final adopted
job/page/cursor revision, exact checkpoint artifact hash and successful readback,
and a settled ledger; cleanup never reads or adopts the private artifact.
A fresh Goal and explicit new source grant with acknowledged scope reset can
rebind a current versioned item. The new citation invalidates old task evidence
while original jobs, page artifacts, grants and effects remain immutable audit.

Generic citations are `SourceItemRef` values: provider, opaque local identity,
revision, content digest, privacy and expiry. Public discovery, API status and
browser persistence receive no provider token, body or calendar private detail.
`GET /sync` exposes coverage, freshness, the current root/revision, safe failure
and cooldown metadata, and the exact redacted metadata selection for reload.
Private readback requires a separate acknowledged owner API read and the original
grant must still be current. Revocation blocks sync/readback while retaining
audit. Explicit provider deletions become tombstones; list omission alone does
not prove deletion. The typed `collect_connected_source_items` callable reads
exact current citations for the current native Mail reply and Calendar meeting
preparation tasks without granting model egress. Each task can deliberately
select at most three connections and ten unique item references, then separately
acknowledge their use. The task Goal ID/revision must match every original sync
page. Current grants, connection/item revisions, content digest and expiry are
checked before the first primary provider/model contact and before final
adoption, including a metadata-only check in the canonical terminal writer.
The related reader adds no provider contacts; cached private source bytes and
references never enter the primary model prompt. Only classified related
references, coverage and freshness enter the existing result (encrypted Mail
draft; Calendar metadata). Missing lifecycle binding or stale/revoked authority
blocks execution without a source-provider fallback. Empty selections preserve
legacy input and authority digests. These paths record explicit `no_learning`. The app's owned
shutdown stops Cordis and profiled browser sessions while the current task
service and its registry remain owned, then releases that task owner before
stopping the Goal programme and source runtime. This order also covers startup
cancellation and preceding stop failures; the scheduler stops once in the outer
cleanup. It clears only the exact source dispatcher pointer before ordinary
DB shutdown; optional host failures do not replace source authority.

Isolated regression/security checks use disposable SQLite/workspaces, a local
paginated HTTP source fixture, separate-process reservation contention,
encrypted artifact readback and negative authority/schema/migration cases.
They establish these mechanics only. Real Google accounts, production operation
and model usefulness remain external-unverified; no inference or eval is part
of implementation or acceptance.

### Exact owned-calendar reschedule

[ADR-019](./decisions/019-exact-calendar-reschedule.md) accepts one literal
owned-event reschedule through separate exact owned-event/calendar-list identity
profiles, current owner/source proof, one approved conditional PATCH and strict
independent readback. Response loss remains Unknown with no resend; a separate
same-original-Root readonly observation preserves original liabilities. This
branch implementation is **Partial**: actual authenticated SQLite/Vault/native
jobs, approval, encrypted artifacts and managed operator UI have been exercised
with only external Google HTTP/DNS simulated. Live Google remains
**external-unverified**; availability of the bounded profile requires the
installed reviewed program revision.

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
reload inspection. These receipts establish bounded implementation behavior,
not live Gmail usefulness. Provider search indexing, changes between source read and send,
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

The Goal loop also offers a separately reviewed public opportunity assessment
policy over selected current public HTTPS watches. It stays disabled until the
operator confirms its finite limits. New verified material packets may produce
a literal cited judgment in the same Inbox; weak judgments remain silent history
and invalid or stale evidence remains blocked. Goal edits require explicit watch
and policy review. Accept creates a Triage review task, and cancel reports actual
quiescence while retaining Unknown inference liability. These judgments do not
execute a capability or update memory. Notifications remain a separate opt-in.
Current cited opportunities offer `Generate read-only plan` for the fixed public
browser-check and public-evidence-report blueprints. Generation makes one
governed request and stages a non-executable Triage card; a separately
acknowledged automatic-staging policy may stage it after assessment. Neither
accepts the plan. Work shows the exact typed preview before explicit acceptance,
then existing native approval and queue rules govern the browser read and, for
reports, automatic CPU dossier/report steps. These retain verified local
readback and `no_learning`. Two contacted generations per Goal per UTC day are
the fixed cap; blocked generation creates no new task or reservation.

Reload performs no Generate or Accept. The canonical Work proposal read may
adopt an already verified original native result without another contact while
the original authority and deadline remain current. Contacted Unknown retains
its cost liability and cannot resend. `Request durable cancellation` waits for
verified transport/context cleanup; a normal same-workspace restart preserves
the original proposal, Root and costs without renewing permission.

Explicit opportunity feedback is now separate from delivery and plan success.
Use **Helpful** only for a completed matching verified plan, or **Not helpful**
for an unwanted opportunity. **Recommend opportunity preference** runs one
provider-free private CPU attempt; zero or insufficient eligible votes returns
`no_learning`. Two verified Helpful outcomes may recommend display ordering;
two Not helpful judgments on one watch may recommend optional suppression.
Review the literal memory preview and separately check its acknowledgment before
adoption. Rollback restores ordinary suggestions without renewing permissions.
Source-watch reads, security/recovery notices and historical feedback remain
available. Preference memory cannot enter ordinary model retrieval. Current
Root, Goal, source, native outcome and complete feedback population are rechecked
at adoption and use; unjudged new work does not become a vote. These reversible
local mechanics do not establish measured usefulness or live provider quality.

New packets replace claimed work only while it has no
provider contact marker, and the existing execution owner must prove closure.
An unowned older execution leaves the latest candidate visibly waiting until
closure or its finite deadline. Input privacy checks include literal source
identifiers and URLs; excluded input blocks assessment while retaining the
verified source-watch result.

See [Guardian Intelligence](./05-guardian-intelligence.md#reviewed-goals-and-the-source-change-inbox)
for ownership, readback, idempotency, and bounded recovery contracts. Independent
review corrections for current authority, original output integrity, automatic
staging proof, canonical Work detail and cancellation cleanup were accepted.
Local source execution and live external/provider usefulness have separate evidence
boundaries; live external/provider usefulness remains **external-unverified**.
This capability remains **Partial**.

## Models And Runtime

Ordinary inference follows the accepted #775 OpenRouter route; ADR-025 permits
only the separate optional NEAR text capability described above. The UI and
`/api/runtime/status` report the effective gateway,
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

The optional [selected-text companion](https://github.com/seraph-quest/seraph/issues/926)
implements ADR-022's ordinary top-frame Chromium text profile within
[ADR-008](./decisions/008-portable-core-and-consented-context.md). A deliberate
gesture, offline preview with deletion-only redaction, current paired Task/Goal
permission and exact native approval precede encrypted private attachment.
Current-owner reads and tombstone-first discard remain separate from historical
execution success. Attachments bypass screenshot observation, model analysis,
learning and generic model ingestion. Linux companion receipts do not establish
macOS execution or native desktop capture; missing adapters block capture while
the portable core remains usable. Pairing alone grants neither execution nor
data egress, and protected-surface checks cannot identify every sensitive page.
Availability follows the reviewed installed revision described above.

### Portable selected browser text profile (#926) {#portable-selected-browser-text-926-branch-local-target}

[ADR-022](./decisions/022-portable-selected-text-context.md) accepts one optional
Chromium companion journey: deliberate ordinary top-frame selection, offline
preview and deletion-only redaction, signed metadata, exact original Root/Task/
finite Goal approval, signed matching bytes and encrypted private task readback.
The existing native job owns its immutable capture identity and irreversible
discard tombstone; pairing alone grants no execution or analysis. Text remains
untrusted D1 context with `no_learning` and no automatic model/evidence ingestion.
The companion's absence or unsupported protected surface is visibly blocked and
does not prevent either macOS or Linux from hosting the CPU core. Browser privacy
checks require operator review and cannot guarantee that ordinary prose has no
secrets. This bounded profile is **Partial**, available with the installed
reviewed program revision. Actual Linux Chromium companion receipts establish
the selected browser journey; macOS execution and native desktop capture remain
unverified.

## Memory

### Cockpit navigation and canonical inspector

The bounded cockpit capability provides six persistent cockpit sections: **Home**,
**Inbox**, **Work**, **Goals**, **Library**, and **Connections**. These select
existing cockpit surfaces and retain the conversation, selected task, and
window layout. Home is a bounded operational summary, not a second scheduler:
page counts are labelled as such, failed refreshes preserve last-known data,
and unavailable queue or spend evidence remains unavailable. Pending approvals
open the existing approval surface.

Home's bounded **Needs attention** snapshot deduplicates current-root task approvals, unknown outcomes, blocked or failed work, stale verification, and linked Inbox decisions. It retains last-confirmed metadata and uses explicit refresh. Attention opens the existing task inspector, rechecks the exact approval or owning readback, and returns keyboard focus to its originating Home or Inbox context. Recovered history remains read only. Pending approval timestamps include UTC offsets, so valid approvals retain their expiry instant in browsers in other timezones. Verified GitHub readback converges the original latest attempt only while the original owner, goal and connection authority remain valid; authority changes preserve settled-effect truth and a specific blocked task reason. Cost recovery links to Settings only after the owning API advertises the exact job/goal control. See [Attention and Recovery](./attention-recovery.md).

The rendered Warsaw browser journey is mechanically verified through real ASGI HTTP/WebSocket handlers and retained SQLite/artifacts, with intercepted public-source/GitHub transport and an explicit server-side test permission. Recreating that ASGI app against the same database proves persisted recovery. The bounded tested-publication profile separately verifies a managed backend restart, native Git production, response-loss recovery and explicit capacity closure against retained SQLite/artifacts, with simulated GitHub HTTP and non-local sockets denied. Governed GitHub Settings creates finite Root-bound write consent; the attention UI does not create it. Readback and capacity closure use separate explicit acknowledgments and preserve Unknown effect, cost and no-learning truth after consent stops or the Goal changes. Live external usefulness remains unverified.

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
enabled v2 embedding slot (or the existing explicit legacy
`embedding_model=openrouter/...` configuration), exact-profile cost accounting,
and fresh embedding/health/latency proofs with measured dimension. If any input
is absent or stale, the vector path remains blocked and the caller must use the
clearly labelled lexical/degraded path; it must not recreate the historical
local 384-dimensional index or silently fall back to another provider. Existing
un-namespaced local vectors are retained as migration evidence and require a
tracked rebuild from canonical memory before they can be used again. The saved
slot selects the existing schema/provider/model/dimension namespace. Search
checks that namespace's measured geometry and usable index before making a
query embedding; a missing index returns lexical/degraded results without a
provider call, empty-table creation, or old-namespace fallback. Restart restores
geometry from current exact proofs. New indexing remains explicit; canonical
source and deletion/tombstone checks still apply across selection changes.

Those proofs are runtime prerequisites for enabling remote vector operations,
not merge prerequisites. Keyless tests and health checks cover the blocked and
lexical/degraded paths without making a provider call.

The accepted memory-boundary decision requires goals, approved facts, jobs, artifacts, checkpoints,
approvals, and audit records stay canonical in Seraph-owned storage. Graph or
external memory integrations remain advisory providers with provenance,
conflict, deletion, export, and failure handling. This boundary authorizes no
benchmark or model call during implementation or acceptance; follow the
Constitution's [standing no-evals direction](./00-project-constitution.md#no-evals-and-provider-free-implementation).

## Fixed reviewed local JSON formatter

**Status:** Partial; availability requires the installed reviewed program
revision and the declared host/profile proof. [ADR-013](./decisions/013-fixed-reviewed-local-tool-package.md)
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
Availability requires the installed reviewed program revision and the selected
runtime's verified readiness; new-host containment still requires its own proof.

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
On revisions containing the independently reviewed
[#1031](https://github.com/seraph-quest/seraph/issues/1031) repair, async,
synchronous and streaming execution retain that same serial lease after positive
provider callback completion until charge or liability settlement and canonical
readback finish. A queued sibling cannot contact while an overrun is still being
committed. Failed settlement/readback retains the existing reconciliation hold;
an elapsed deadline or returned coroutine does not release it. Known charges stay
known when current authority later prevents result adoption. Positive
never-contacted denial proof retains its existing quiescence journal. Isolated
scripted-transport receipts establish these ownership mechanics, not provider
availability or model quality.
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

### Fixed private Moltbook browser read profile

[ADR-018](./decisions/018-fixed-moltbook-private-browser-read.md) accepts one
optional private Home document read through actual isolated Chromium and the
existing owner connection. The bounded implementation has mechanically
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

### Optional Moltbook adapter profile

The bounded `work.moltbook.v1` adapter uses **Settings → Moltbook** and
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
300-second job has one attempt and at most six contacts: one status GET, three other
fixed GETs, one creation POST and one verification POST. Restart, approval and
manual answer never renew those allowances.

The [official API instructions](https://www.moltbook.com/skill.md), checked
2026-10-03, document community feed/comment listings and verification.
Verification or exact-ID readback alone cannot establish public visibility.
Post publication requires exact-ID membership in one bounded public community
feed page (at most ten items), then one exact-post GET matching the full approved
text, verified ID, author and community. A truncated feed preview is membership
evidence rather than full text. Comments retain the bounded target-comment
listing and exact parent check. All contacts remain inside the original
six-contact/deadline allowance; explicit hidden/private/removed metadata rejects
adoption. An absent item remains
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
consent and source review. On October 4, 2026, the human-claimed `seraphquest`
account published one transparent introduction; its full public post and feed
membership were verified at [the published post](https://www.moltbook.com/post/9637612d-bb5b-458e-9cdb-0d52e27fe48a).
One bounded feedback GET returned seven comments, treated as untrusted external
content. The original native Unknown state, history and reservation remain
unchanged; the subsequent full-readback correction is independent of that
original job. Availability depends on the installed reviewed `develop`
revision. This single event establishes no general provider quality or
usefulness claim.

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

### Exact Forgejo issue title transaction profile {#exact-forgejo-issue-title-transaction-target}

[ADR-021](./decisions/021-exact-forgejo-issue-title.md) accepts one ordinary-issue title edit through the real Forgejo v15.0.9 editor, using backend-only session secrets and an exact separately approved title. The bounded profile has actual signed local Forgejo, Chromium, canonical job and cockpit evidence; availability requires the installed reviewed program revision. Live Codeberg is mechanically blocked pending a separate reviewed account/site/version activation path. Providing a key cannot enable live execution. Provider title history and notifications remain; the provider offers no atomic revision CAS or idempotency key. A lost response stays Unknown, read-only observation never resubmits, and reversal requires a new exact approval. Configuration grants neither Goal budget nor mutation permission.

In Settings → Forgejo, select an owned Vault credential and active Goal, explicitly grant a finite read window, and run the bounded backend session job. The title preview shows exact old/new titles and numeric repository/issue identity before a separate unchecked approval and one Save. Jobs retain their original deadlines and exact request identities across reload and restart. An exact historical retry returns the original receipt without another provider contact. A separate acknowledged GET-only recovery reports the observed title while retaining the original Unknown and reservation; equality cannot establish attribution. The live original Root may explicitly withdraw an accepted job with a protected never-started journal, zero attempts/fence and no lease, execution request, contacts, effects or output, even after provider READ revocation/expiry, credential rotation or an owned Goal correction. This SQL-only withdrawal grants no execution and cannot release a started Unknown job. Revocation invalidates usable session authority and current private reads, and clears the fetched output display. Every outcome records `no_learning`; local mechanism evidence makes no live-account or quality claim.
