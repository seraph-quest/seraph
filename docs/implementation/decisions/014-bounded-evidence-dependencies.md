---
title: "ADR-014: Bounded Evidence Dependencies"
---

# ADR-014: Bounded Evidence Dependencies

**Status:** Accepted

**Decision class:** Target architecture

**Tracked work:** [#917](https://github.com/seraph-quest/seraph/issues/917), under [#899](https://github.com/seraph-quest/seraph/issues/899)

## Context

Canonical corrections, tombstones, private task evidence packets, reviewed Work
proposals and fixed artifact pipelines already exist. A source-derived proposal
may nevertheless become stale before acceptance, and evidence selection does
not yet imply an execution precondition. Extend those existing boundaries;
do not infer dependencies from similar text or introduce another memory store.

The lead accepted the finite V3 design after independent GPT-6 Luna MAX review
on 2026-10-03, report SHA-256
`238ee14dd928fa3171991957b76d0cb9b3323a35bc86cf562d181511e32b704d`.
That review inspected 18 exact source files at
`d9d886cdee4b682fb1f3dde4b0b9a24ae8794cc8` and resolved all three preceding
design findings. This ADR is accepted target architecture, not an implementation
receipt or a Shipped claim. ADR-003 canonical memory, ADR-008 peer-host targets,
ADR-010 pipeline limits and existing governed inference checks remain controlling.

## Decision

### Sources, consumers and permissions

Execution-use binding is an explicit, separately acknowledged option on an
existing owner/Goal task evidence packet. It neither grants model egress nor
changes executor inputs, permissions or approval. Unbound legacy work remains
visibly unbound and keeps its existing behavior.

Eligible sources are exact owner/Goal canonical operator facts and verified
completed outputs of these three registered producers:

| Eligible producer | Initial execution-bound consumer | Additional use boundary |
| --- | --- | --- |
| `browser.public-task.v1` | `browser.public-task.v1` | Every guarded navigation/subrequest admission |
| `work.evidence-dossier.v1` | `work.evidence-dossier.v1` | Current-source validation before CPU input/output use |
| `work.local-evidence-report.v1` | `work.local-evidence-report.v1` | Current-source validation before CPU input/output use |

The producer and consumer sets are independently enumerated; a producer does
not authorize an arbitrary consumer. Extend existing evidence eligibility only
for these exact CPU output types if absent. Mail, Calendar, advisory hits,
arbitrary workflow outputs, unverified outcomes and all other source/consumer
combinations remain unsupported for execution binding. Existing private
inspection is independent. Binding requests reject unsupported consumers;
claim, native admission/recovery and proposal acceptance also block unexpected
persisted dependency rows rather than ignoring them.

Memory tokens bind canonical ID, active/tombstone state, content/metadata digest,
updated revision/time and exact owner/Goal/operator provenance. Artifact tokens
bind canonical ID/digest/type, exact completed producer task/native job/attempt/
verified readback and current owner/Goal/read permission. A completed producer's
old execution Root is historical: ordinary expiry does not delete an owned
immutable output. Current read revocation, missing proof or physical drift does
block use. Browser-supplied versions and model output are never source authority.

Canonical private facts stay on the private evidence/context surface. Binding
does not splice them into public browser requests or CPU artifacts. Dossier and
report output continue quoting their explicit public producer inputs literally.
The governed triage path is a separate model-context consumer with its existing
purpose-bound cloud adoption, current policy, accounting and contact gates.
Rebinding never renews that consent or introduces a planner/model call.

### Bounded canonical representation

Add one focused `WorkBoardEvidenceDependency` table for active selected
source/span bindings: at most 16 per task within existing packet bounds of
32 sources, 16 claims and 64 KiB. Store task, owner/session/Goal, source kind/
canonical ID/opaque source ID, resolved token and source/span digest, packet
revision/digest, binding task revision and optional existing pipeline/slot.
Do not copy fact text. Index owner/session/source kind/canonical ID and task ID;
unique task/source/span identifies the current binding.

Replace or revoke active rows in the same task CAS; existing immutable Work
events retain bounded prior token/diff metadata. Do not create an inactive
shadow dependency ledger. A fixed pipeline traverses only its existing at-most-
four steps, retaining admitted deadlines, attempts, completed producers and
Unknown liabilities. There is no arbitrary graph, global Goal revision bump,
new scheduler lane or synchronous scan of every dependent task.

Owner/Goal affected-task projections use indexed stable keyset pagination, at
most 50 tasks per page and 16 dependencies per task. GETs are pure projections:
current tokens may show stale status, but inspection does not mutate tasks.
A strict explicit owner-scoped impact-evaluation POST may materialize at most
50 safety pauses per cursor. Bind its cursor to owner/source-token snapshot and
stable position; repeated evaluation of an already materialized pause is an
idempotent no-op. A stale cursor requires fresh inspection. This mutation never
accepts replacements, grants readiness or dispatches work.

### Current-source admission and ordering

For each of the three consumers, check dependencies in the immediate Ready
claim transaction before creating an attempt, at native admission/resumed
execution, immediately before actual source use, and before terminal artifact/
readback/Board adoption. Preserve checks through reconciliation and fixed
pipeline handoff/revision; clearing a binding must not bypass a stale guard.

Stage bounded no-follow physical artifact validation outside the writer, then
recheck canonical artifact/job/attempt/owner/permission tokens inside it and
check physical drift again before use. The writer does no filesystem, network,
credential-decryption or nested-session work. SQL ordering does not make host
files immutable; the cooperative workspace trust boundary remains explicit.

The contact/use admission point is the current-token check and its bounded
existing native checkpoint in the same committed immediate transaction. An
earlier check followed by an unchecked checkpoint is insufficient. This orders
admission against canonical correction; it does not make HTTP and SQL atomic
or hold the database writer across I/O. Correction first blocks stale admission.
Admission first preserves the actual attempt; later current-source checks stop
still-undispatched work. A contact already admitted cannot be retracted by a
later correction, declared absent or replayed.

Canonical correction or deletion changes its own bounded source rows/tombstones;
current-token mismatch invalidates affected bindings without eager task fan-out.
Claim/use guards remain authoritative even for never-inspected tasks. Commit a
bounded durable stale/blocked receipt before returning rejected dispatch;
do not roll it back with the rejection. Completed/Review outcomes remain;
running work gets a stale-input receipt without blind cancellation. Preserve
consumed attempts, unsettled effects and cost holds. Existing native recovery
owns cancellation/quiescence. Recovered read-only owners can inspect, but cannot
invalidate, approve, dispatch or adopt source authority.

### Evidence-only rebind

Use the existing private task evidence API/panel for a bounded server-resolved
preview. Its digest binds owner/session/Goal, exact task revision, old packet/
binding revision and digest, replacement packet, current token snapshot,
affected existing slots and unchanged executor-input digest. Show safe old IDs/
digests when old text is deleted or revoked; do not restore that text for a diff.
Preview grants no readiness or approval.

The strict accept request includes exact references, expected revisions/digests,
preview digest, canonical UUID idempotency key and literal execution-use
acknowledgment. Server tokens are authoritative. Stage physical checks outside
the writer, then recheck current canonical tokens and the complete digest in
the same immediate transaction as task CAS, packet/dependency replacement and
immutable Work event.

Extend `WorkBoardEvent` with nullable `mutation_idempotency_key` and
`mutation_request_digest`, a unique owner/session/key index and focused
compatibility migration. Existing ordinary events retain null keys. Request
fingerprints include task and mutation kind. Event metadata contains bounded
safe applied-result IDs, revisions and digests, never source text or public
tokens. Identical authorized retry returns the prior applied receipt; current
inspection is separate. Changed body/key bindings conflict. Reopen does not
renew work, and an old successful acknowledgment does not prove the current
binding valid. Do not add a preview store or repurpose proposal rows for rebind.

Evidence-only rebind cannot change title/body, capability, typed input,
destination, URL, output path, permissions or cloud consent. It removes only
the exact stale-evidence blocker when all existing Goal/task/native-source and
approval predicates hold. It never approves or dispatches work. A correction
requiring different executor content stays blocked pending distinct reviewed
typed specification.

### Reviewed typed task changes

Use existing `WorkBoardProposal` kind `specify`. Generation persists the exact
actually used evidence packet/digest and resolved token snapshot in canonical
proposal/request digests. Cover every source-derived proposal with a canonical
evidence-use receipt, even if execution binding is off. Older source-derived
proposals without a snapshot require regeneration or explicit eligible evidence
review; retain their history. Unbound proposals with no evidence use keep their
existing behavior, without an evidence-free default bypass.

Inside the existing immediate `accept_proposal` writer, immediately before task
CAS, recheck the stored snapshot, current source state/ownership/read permission,
packet/binding revision and proposal digest. Previously staged bounded physical
checks support pure canonical validation. Stale sources reject before any
title/body/input/status change and retain immutable proposal history.

For bound tasks, preserve exact dependencies unless that same reviewed request
includes an explicit replacement packet and execution-use acknowledgment.
Task revision, replacement rows and a distinct event change atomically. A new
capability outside the finite consumer set remains blocked while bound; do not
delete dependencies automatically or expand consent. Correction first prevents
acceptance; acceptance first preserves history, and a subsequent correction
blocks future claim/use. A claim guard alone cannot satisfy this acceptance gate.

### UI and recovery

Persist and read back the exact bounded owner/session-scoped request in
`sessionStorage` before POST. Reload performs inspection only; explicit retry
uses the same request/key. Corrupt or unavailable storage blocks mutation.
Separate evidence rebind from typed specification and show affected/unchanged
steps, stale reasons and exact reviewed inputs. Generic task/event projections
expose only opaque IDs and bounded reasons; private source text renders literally
on the authenticated evidence surface.

Rollback is a new reviewed revision using only currently eligible sources.
It does not resurrect deleted/revoked evidence, undo effects or dispatch work.
Pipeline updates require existing quiescence and retain original deadlines,
attempts and completed producer history. If a native boundary cannot carry the
declared guard, it remains visibly blocked; do not trim this matrix silently.

## Consequences

- Canonical memory remains authoritative; dependencies are execution
  preconditions and provenance, not a second memory system or automatic truth.
- Operators review the smallest eligible update without changing unrelated work
  or granting new cloud/execution authority.
- macOS and Linux remain peer CPU core-host targets; this decision adds no GPU,
  model service, host administration or platform-specific core prerequisite.
- This target claims neither implementation nor live provider usefulness,
  native Mac evidence, automatic learning or competitive superiority.

## Verification

Require actual file-backed SQLite owner/Goal tasks, canonical fact correction,
an unaffected sibling, completed real local producer, approved replacement,
future task execution/readback and explicit `no_learning`. Show changed future
triage context with only the provider HTTP boundary intercepted; a refreshed
badge alone is insufficient. No paid/free-tier inference or external account
mutation is authorized by these checks.

Prove correction versus claim/use separately for all three consumers and versus
proposal acceptance, including first-admission history preservation. Cover
unsupported consumers and unexpected rows; correction/deletion/read revocation,
physical drift, foreign/recovered owner, forged token/digest, stale packet,
active attempt, malformed bounds, rollback and Unknown liabilities. Test rebind
exact retry, changed body conflict, event uniqueness, partial transaction
failure and SQLite reopen. Verify pure GET, bounded idempotent impact pagination,
pipeline quiescence and original deadline/attempt preservation.

Require a managed authenticated UI journey through safe preview, distinct
explicit approvals, literal real artifact output, lost response, reload with
zero automatic POST and exact manual retry. Fresh independent cumulative review,
focused local checks, owning docs and issue/PR/Project readback precede the
whole-milestone PR's integration. Implementation state belongs in STATUS and
the owning guides only when supported by actual receipts.
