---
title: "Reviewed artifact pipeline reconstruction"
---

# Reviewed artifact pipeline reconstruction

**Document class:** Research; reconstructed implementation proposal

**Date:** 2026-10-03

**Tracked work:** [#914](https://github.com/seraph-quest/seraph/issues/914)

**Decision owner:** [ADR-010](/decisions/reviewed-artifact-pipelines), Accepted after fresh independent reconstruction review

## Provenance and recovery limit

The durable baseline is commit `0357d8a7997c5bdd3546464ca1941af5cc1a4111`.
Issue [comment 5957707692](https://github.com/seraph-quest/seraph/issues/914#issuecomment-5957707692)
records the lead's accepted fixed three-leaf architecture and independent review.
The previous proposal, ADR draft, review files, uncommitted implementation and
temporary runtime receipts were lost. Their recorded hashes establish provenance,
not current file availability or a reconstructed file's identity. This document
reconstructs the fixed contract, independently confirmed and accepted before
runtime implementation. The separate Luna MAX fidelity review on 2026-10-03
reported no findings; its SHA-256 is
`def11bc0b32adb3b163eb238225ed191f4455f7a65b837f8dafc7deceac89855`.
It does not claim existing execution, integration or Shipped truth.

## Operator outcome and placement

Work offers one reviewed finite public-source plan:
`browser.public-task.v1` → `work.evidence-dossier.v1` →
`work.local-evidence-report.v1`. The browser performs real bounded extraction;
the two CPU leaves produce a deterministic evidence dossier and local plaintext
report. They perform zero model requests, network requests, credential access or
subprocess execution. Every leaf records explicit `no_learning`.

The operation is metadata on existing non-executable `WorkBoardProposal`, with
versioned named output bindings, existing tasks, links, handoffs, attempts,
durable native jobs, private artifacts and readbacks. It sits beside the fixed
`procedure_v2` contract; it does not change its two-step limits. There is no new
executable parent, arbitrary DAG interpreter, generated tool, code execution,
model planner, scheduler lane or authority. At most four steps are admitted;
this implementation registers only the fixed three-leaf chain. Repository/PR
publication belongs to #911; PDF/CSV exports belong to #923.

## Review, identity and aggregate limits

Preview shows exact capability versions, source scope and permissions, typed
output/input schemas, priority, task/Goal revisions, original owner and live
canonical root, finite limits and approvals. Review approves the exact plan
version and changed scope; it does not approve future external effects. Existing
#915 exact typed-review proof and native-effect guards remain authoritative.

First admission fixes one absolute deadline of at most 300 seconds. Per-leaf
attempts are at most two; aggregate attempts are at most six. Browser runtime is
at most 180 seconds; each CPU runtime is at most 30 seconds; every output is at
most 64 KiB. Remaining active Goal limits and operation time narrow these bounds.
Plan revisions, retries, restarts, cancellation and readback cannot renew the
deadline, attempt counters or other consumed allowance. Priority uses existing
ready-task dispatch; CPU consumers cannot overtake unverified producers.

## Output verification and consumption

Named output bindings admit only the exact completed independently verified
producer output, including schema/content digests, source permission, original
live root, owner, immutable source attempt and current consumer Goal. Missing,
failed, Unknown, changed or unverifiable output blocks downstream work visibly.
Native browser ownership is strictly the server-owned mapping
`service:browser-task` / `browser_public_task`, never a user alias or wildcard.

Materialization reserves the unique key `(operation, plan version, producer
attempt, consumer slot)` durably before bounded no-follow private writing,
promotion and independent readback. Adoption uses exact operation/plan/task CAS.
Restart either recovers that exact reservation/file binding or marks it Blocked;
it does not create a second binding or silently trust a partial write. The
consumer recomputes canonical input and fingerprint digests from the exact
admitted immutable parent, handoff, permissions, limits and `no_learning`.
Only the two fixed CPU kinds are supported. Succeeded alone is insufficient:
actual settled-effect output readback precedes canonical bound-input consumption
CAS and successful task verification.

## Revision and recovery

Changing a current Goal/source freezes affected unfinished work using exact CAS,
cancels active work and waits for quiescence before a replacement is adopted.
Completed artifacts, immutable effects, attempts and Unknown liabilities remain
unchanged. A source replacement after its consumer completed is rejected and
requires a distinct finite reviewed operation. Replacing an unattempted consumer
updates only its current `WorkBoardLink` and current handoff, under exact
operation/plan/task CAS: invalidate old current binding before new materialization,
preserve old handoff versions and provenance immutably. Do not run old effects.

An expired operation remains expired. A distinct freshly reviewed finite
operation may reuse only exact independently verified completed output for an
unfinished consumer under the same original live root/owner, its current Goal
and source permission. It never adopts or replays old attempts, effects,
reservations, allowances or liabilities. Root change blocks reuse.

## Trust boundary and uncertain UI mutations

Public source text is quoted structured untrusted data, never instructions,
HTML or Markdown execution. Serve the report as `text/plain`; React renders it
literally in `<pre>`. Test an actual extracted prompt-injection string through
both CPU leaves and literal readback; verify no model/network/process/credential
path is invoked and no canonical learning is written.

Before a UI POST, persist and read back the exact bounded request (at most
16 KiB) in owner/session-scoped `sessionStorage`, reusing #915 recovery patterns.
Reload/lost responses permit only exact idempotent retry on finite known paths.
Corrupt or unavailable persistence fails closed. Recovery cannot invent a fresh
operation, adopt authority automatically or move to a new root.

## Narrow managed-runtime prerequisite

Issue [comment 5958835830](https://github.com/seraph-quest/seraph/issues/914#issuecomment-5958835830)
records a prior observed optional-extension metadata event-loop blocker; original
receipt files are missing. Reproduce current behavior rather than assuming a
past receipt is still present. The authorized narrow remedy is one contribution
index per list after governance sync, plus one pending off-loop metadata worker.
GET list/diagnostic/detail/lifecycle/connectors share the finite helper. Mutations
use fresh canonical checks/fallback. No completed-response/authority cache,
removed controls, new env setting, approval bypass or operation deadline renewal.

Caller wait is at most four seconds with shielding. One actual thread-completion
event retains the pending worker through caller timeout/abort, queued-before-start
cancellation and loop cancellation. Mark submitted only after actual
`run_in_executor` submission; failed submit marks worker done immediately.
Queued-not-started cancellation stays held until the actual thread finishes.
Recovery permits a new worker only after actual completion, preventing unbounded
resubmission. Preserve exception redaction and last-known UI values.

## Acceptance evidence

Use focused tests for exact schema/digest/root/owner/Goal/source/plan CAS,
concurrent revisions, attempts/deadline expiry, affected-step replacement,
no renewal, failed/Unknown producer, cancellation quiescence, priorities,
partial writes/restart, materialization idempotency and completed-output reuse.
Extension tests cover heartbeat, single-worker concurrency, timeout/abort/queued
cancellation/recovery and redacted exceptions.

The positive receipt must be authenticated Work UI → real Chromium → SQLite →
private typed handoff → actual CPU dossier/report → independent artifact/effect
readback, using only owned free ports 8014/3014 and `manage.sh -e dev local run`,
private workspace and blank provider keys. Existing 3001/8004 remain untouched.
Use actual bounded DOM extraction or a declared exact HTTPS source-bytes fixture
with real Chromium; intercepted source transport is labelled and does not prove
live public-network usefulness. Do not assume an `h1` exists. Prior Unknown rows
remain liabilities and cannot gain new deadlines for this acceptance run.

Retained evidence/checkpoints live under the persistent repository's private
`.agent-evidence/914` and owning worktree, never `/tmp`. Disposable pytest temps
may use `/tmp`. A fresh independent cumulative review gates final integration;
Linux receipts cannot claim Mac-native execution proof under ADR-008.
