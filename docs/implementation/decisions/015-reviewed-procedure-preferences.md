---
title: ADR-015 — Reviewed procedure preferences
---

# ADR-015: Reviewed procedure preferences

**Status:** Accepted target, October 3, 2026. Owning issue
[#919](https://github.com/seraph-quest/seraph/issues/919), Epic #899.
This accepts the independently reviewed V3 design; it does not establish
implementation completion, shipped behavior or measured quality improvement.

## Context

Reviewed procedures already have immutable owner-bound versions, package
review, activation and bounded native invocation. M5 supplies operator-reviewed
canonical memory, signatures and rollback, but its existing selection effect
names a capability rather than an exact procedure version. Task success alone
is neither positive user feedback nor authority to learn or execute.

## Decision

Support one existing registered template, `public-browser-check`, and only its
matching manual invocations. An explicit deterministic local recommendation
requires two distinct invocations with actual independently verified native
parent and fixed browser leaf readbacks and current explicit Helpful feedback.
Any current Harmful feedback vetoes the preference. Feedback corrections append
a replacement Helpful/Harmful decision with a reason and exact current event
CAS; execution, negative outcomes and earlier feedback remain immutable history.
New feedback requires the current ended invocation attempt and outcome. Its
decision applies only to the exact current task revision, latest attempt ID,
and fence. A later revision or attempt leaves the complete feedback chain
visible but ineffective and records `feedback_outcome_stale`/`no_learning` until
an explicit current-outcome correction names the historical tip and supplies a
reason. An exact historical request replay writes nothing and does not regrant
eligibility. The Library disables feedback before an attempt ends and separates
historical feedback from the current decision.
Failed, blocked, cancelled, unverified, stale and unreviewed outcomes are shown
and never count as positive votes. Any running or unresolved invocation blocks
recommendation and adoption. Insufficient evidence records `no_learning`.

The complete bounded inventory is server-resolved from exact owner, original
authenticated Root, Goal/revision and the actual manual invocation publisher's
`procedure-v2:{routine_id}:{version_id}` scope, before filtering any status,
proof or feedback. `LIMIT 21` detects the twenty-task cap; feedback `LIMIT 101`
detects the one-hundred-event cap. Never produce a positive preference from a
truncated or caller-selected success set. Malformed matching members block
rather than disappear. Governed scheduled invocations use a different scope
and are excluded from this evidence population; their invocation behavior is
unchanged.

A protected membership token covers sorted exact task IDs/count, exact latest
attempt/fence, typed input row/revision/metadata/payload binding, native parent
and fixed leaf identities/revisions/fences/lineage/authority/readback digests,
and every feedback chain tip/count/sequence digest. The bundle additionally
binds immutable plan, copied browser input, reviewed package and actual proof
file digests. Re-enumerate the complete set inside recommendation finalization
and adoption writers, and before later selection. Any inserted matching task,
even without an attempt or feedback, invalidates the old token. An accepted
preference cannot continue changing selection from its historical subset after
new matching outcomes appear.

Use one focused provider-free CPU capability
`memory.procedure-recommendation.v1` and native job
`work.procedure-recommendation.v1` in the existing Work runtime. Use existing
priority/admission, one attempt, no automatic retry, a deadline no later than
120 seconds or the original Root bound, explicit cancellation and private
deterministic JSON output/readback. No model or external write is required.
The entire serialized recommendation artifact/provenance/scope is capped at
128 KiB; individual proof files at 4 MiB and aggregate proof bytes at 16 MiB.
Measure actual sizes and fail closed. Store identifiers, hashes, counts and
fixed readback identities, not raw source text, input bodies, notes, tokens,
credentials or response headers.

Preview shows the exact outcome list, counts, corrections, exclusions, blocking
or no-learning reason, and exact existing procedure/version preference. Preview
and adopted Library suggestion both plainly disclose:

> Only matching manual invocations are counted. Governed scheduled invocations
> are excluded.

> This deterministic preference is not a measured quality improvement.

These disclosures remain visible after adoption and reload, adjacent to the
included outcomes; they are not confined to tooltips or an advanced inspector.

Adoption requires a separate explicit literal-True acknowledgment and exact
proposal revision, preview and source bundle. It writes a versioned canonical
pattern through the existing memory review/history/rollback path. The
specialized `procedure_recommendation.v1` action branches before the generic
M5 writer. Stage authentication, native proof, files, Vault/redaction and
signing-key material outside the writer. Do not call the legacy source
validator, asynchronous sanitizer, native resolver, key loader, filesystem or
nested session inside the immediate writer. Same-session DB-only checks then
validate the current canonical Root token-hash/owner/expiry/revocation,
active Goal/revision, package/version/routine, exact proposal/preview/deadline
and complete membership/feedback/readback/input CAS. Signed memory, accepted
proposal and adoption audit commit together or neither. A staged orphan file
does not grant authority.

The specialized signed scope explicitly covers the schema, owner/Root,
Goal/routine/version/plan/package/input identities and membership/bundle
digests. Existing M5 signature schemas remain unchanged; merely appending
fields to the existing whitelist does not authenticate them. Generic M5
capability comparison excludes this schema. Physical source proof and signature
are staged before later selection, followed by consistent canonical complete
membership and current-state revalidation. Multiple eligible preferences are
ambiguous and blocked, not last-wins.

Preference changes only the future suggestion/selection of that exact current
version in its Goal/revision and owner/Root scope. It never grants permission,
activates packages/procedures, invokes tasks, expands cadence or schedules,
changes parameters, generates code, enables models or transfers across Goals,
Roots or versions. Fresh invocation still uses all existing independent
authority, grant, approval, input, budget and deadline checks.

Preview expires within five minutes or the earlier original Root/job bound.
Exact key/body retries reconcile the existing mutation without renewed expiry;
changed bodies reject. GET, restart and reload never adopt or invoke. Rollback
targets the exact signed accepted memory/proposal/content/revision, restores
ordinary ordering and retains all canonical history. It does not restore
execution authority. Current source drift blocks new use; historical rollback
remains possible through its own current-Root-bound staged action.

## Consequences and validation

Prove actual authenticated local native procedure executions, explicit feedback
and correction, bounded recommendation job/artifact/readback, review/adoption,
changed future suggestion and rollback/restart in one operator journey. Source
membership insertions, feedback changes, Root revocation/expiry and source
changes between staging and each commit must fail closed. Writer-I/O guards
must reject any filesystem, Vault, signing-key load, native or nested DB work
inside canonical writers. Preserve ordinary M5 review/rollback/comparison and
existing procedure lifecycle regressions. Component and managed UI proof must
show both disclosures before adoption and in the adopted suggestion after
reload. Independent cumulative review is required before milestone completion.

Fixtures and intercepted network transports can prove mechanics, never measured
usefulness or production improvement. The guardian outcome-learning fixture
study is not a production learning source. Autonomous evolution, scheduler
learning, generated code and broad generalized procedure transfer remain out
of scope.
