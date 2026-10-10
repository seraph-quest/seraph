---
id: first-discovery-capacity-selection
title: "ADR-034: First-discovery capacity selection"
---

# ADR-034: First-discovery capacity selection

**Status:** Accepted branch-local target. Implementation is permitted after the
lead records the independently reviewed contract commit. It need not wait for
the whole milestone merge. Promotion to `develop` and shipped truth require the
whole milestone merge and its acceptance. Runtime composition remains **Planned**;
this decision establishes no implemented admission or execution capability.

**Tracked work:** [#1007](https://github.com/seraph-quest/seraph/issues/1007).

**Relationship:** Supersedes only ADR-026's programme clause-2 Identity selector
precondition for the first original composed `goal_public_discovery_v1` admission,
before its first job insertion. The persisted-job selector after insertion,
retained-history selection, programme continuity and restore/rollback contracts
remain unchanged. [ADR-033](./033-task-method-composition-metadata.md) continues
to own the metadata ceiling **1359** and unfiltered **LIMIT 1360**. All other
ADR-026, ADR-027 and ADR-028 authority, body, lifecycle and capacity rules remain.

## Context

The original `GoalDiscoveryService.admit` receives only Goal/programme/grant
revision. Its original programme owner loads the canonical Goal and validates
the matching stored `GoalProgramme` before reading the issuer and Identity.
The existing immutable `GoalProgrammeAuthorityBinding` is derivable at that
point. Its possession is not authority.

The original complete `GoalDiscoveryAuthority` requires the actual staged plan
reference and is constructed after brief/plan staging. The existing admission
callback validates authority before inserting the first job. The persisted-row
selector therefore cannot supply the certificate for the earlier Identity read.
Creating a placeholder job, fabricating an artifact reference or performing
file effects before current authority validation is not a permitted repair.

The operator approved the narrow first-admission amendment on 2026-10-10.

## Decision

Solely for the first original composed `goal_public_discovery_v1` admission,
the actual started `GoalDiscoveryService` may supply the existing original
`CompositionSessionGuard` with a prospective **capacity selection**. Derive it
only from the current common33-certified canonical Goal's exact stored
`GoalProgramme` generation and its matching canonical issuer binding, using
the existing immutable `GoalProgrammeAuthorityBinding`. Select only that
binding's exact original owner Identity ID; caller/plugin-provided Identity
choices, arbitrary typed objects and wildcard/all-Identity scans are rejected.

The existing guard is the sole selection issuer. Authenticate the actual
service, original admission task, jobs owner, current reviewed host and existing
native branch, original session/connection/certificate and identical original
numeric frame. A copied, replaced, detached or stale selection cannot substitute
for that original owner. No new selector service, Source, grant, task, registry,
witness envelope, SQL table/index or authority schema is introduced.

Certify common33 before canonical Goal/issuer bodies. Derive and match the exact
stored generation, requested Goal/programme/grant revision and original fixed
discovery capability and issuer binding. Before the selected Identity body,
certify the exact existing Identity3 schema/locator and full selected row cost
under ADR-026. All metadata, raw row appearances, repeated reads, physical bytes
and prospective copies/outputs spend the **same original 128-distinct-reference /
1,048,576-byte frame**. There is no reset, renewal, cap increase, discount,
identity-specific allowance, new phase pool or second engine.

This prospective case replaces the persisted discovery declared-authority
selector only until the first original row is inserted. It authorizes capacity
selection and body preflight, **not** a grant, Source, job admission, contact,
execution, publication or learning. Current Identity, canonical issuer provenance,
Goal/programme/grant/capability/route/policy/budget/expiry and revocation checks
remain mandatory before any artifact/job effect. Issuer tombstone or explicit
Identity/programme revocation cannot be bypassed; the standing programme owner's
existing distinction between issuer browser logout/expiry and grant validity is
preserved.

Use the actual original operation/session/frame throughout common33 certification,
canonical reads, prospective selection, Identity preflight/current validation,
physical staging and original insertion. An independent session or newly created
budget before the later writer cannot establish this proof. Immediately after
each actual fresh `BEGIN IMMEDIATE`, recertify and rederive the same exact facts
on that current connection with the same spent/reserved frame before body-reading
callers or effects. Rollback/write/schema drift invalidates certificates without
refunding capacity. Insufficient capacity blocks before the next body/effect.

The eventual complete original `GoalDiscoveryAuthority`, original composed
binding and inserted row must match those exact facts in the original insertion
transaction. No stored-job retrofit, relabeling or historical backfill is allowed.
After insertion, subsequent claim/START and retained-history selection use the
unchanged original persisted-row selectors and authentic Source owners. ADR-026's
one-use protected START commit, matching delivered response, same original run
task, host/boot/fence/deadline and original unresolved-liability rules remain
mandatory. This decision grants no inference/result or financial-settlement
permission and does not change restore/rollback provenance or revocation ordering.

## Required acceptance and fail-closed behavior

Use genuine original programme issuance and actual service/host lifecycle.
Prove common33 before Goal/issuer bodies; the original guard's exact prospective
selection and Identity3 preflight before Identity body; unchanged current
authority before file/job effects; fresh writer recertification/rederivation
under the identical frame; and matching complete declaration/binding/insertion.
The later actual Source/claim/START journey remains independently required.

Reject copied/replaced selections; wrong service/task/jobs/host/session/connection/
frame; foreign or duplicate Goal/generation/issuer; revision/capability mismatch;
drift after staging or fresh BEGIN; revoked Identity, tombstone issuer, stale,
paused, revoked or expired programme; changed policy/route/budget; unsupported
schema, missing bodies or original capacity overflow. Failed pre-effect cases
leave rows/files unchanged and produce no claim/START. Verify browser logout
alone preserves existing standing provenance while actual revocation denies.

A fabricated job or plan reference, early file effect, all-Identity selector,
caller-provided authority, reconstructed historical Source or renewed frame
remains unsupported. Broader Goal APIs, credential closure, restore/rollback,
inference/result ownership and other unresolved contracts are not resolved by
this decision. Neither this ADR nor a capacity certificate establishes a ready
or shipped whole milestone.
