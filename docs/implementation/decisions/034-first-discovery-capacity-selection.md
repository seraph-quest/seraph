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

[ADR-035](./035-first-admission-saved-strategy-read.md) separately accepts the
original owner's bounded transient current-strategy pointer read after this
decision's original guard/Identity/current-authority checks. It preserves the
same operation, session, spent frame and fresh-BEGIN ordering; this capacity
selector alone never authorizes pointer bodies, retention or restore.

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

For the managed current-slot `goal_public_discovery_v1` candidate pass, the
original operation belongs to the existing started `GoalDiscoveryService` and
the same actual `asyncio.current_task()` executing the existing registered
`_async_job_wrapper(run_goal_discovery_tick, ..., job_id="goal_public_discovery")`.
Before any session pre-yield probe or Goal census/body, consume that original
wrapper's current principal through `get_current_trust_principal()`: it must be
the existing `TrustPrincipal` with `PrincipalType.SERVICE`, exact principal ID
`service:scheduler:goal_public_discovery`, `authenticated=True`, `revoked=False`,
empty grants, and its actual wrapper-generated job ID with prefix
`scheduler:goal_public_discovery:`. For this operation-owner boundary, the accepted source-owned entry is the exact
currently registered `goal_public_discovery` wrapper callable, including direct
invocation of that exact registered callable. This does not prove, require or
claim an APScheduler trigger event. Registration/callable identity proves the
accepted entry path, not why it was invoked. Unwrapped `run_goal_discovery_tick`
or `tick` invocation and reconstructed/look-alike wrappers are not accepted.
The exact registration, callback, loop, principal-object, actual-task and live
one-pass marker checks below remain mandatory; direct wrapper invocation grants
no Goal authority, inference permission, budget or new slot semantics.


The existing registered scheduler wrapper may establish one private live
invocation marker solely for this original `goal_public_discovery` callback.
Creation is confined to the real `_async_job_wrapper` invocation after its
ordinary no-grants principal is bound and before awaiting the exact registered
`run_goal_discovery_tick` callback. Authenticate the actual current scheduler's
`goal_public_discovery` registration and its exact wrapper callable identity;
a newly constructed look-alike wrapper or matching job-ID string is insufficient.
Bind the marker to that callable/registration, exact callback, existing loop,
actual `asyncio.current_task()`, exact current principal object and its actual
execution job ID. It is a private invocation-lifetime marker, not a grant,
Source, Goal authority, budget, durable receipt or registry entry.

Before any candidate session pre-yield probe or Goal census/body, the original
discovery owner consumes that live marker for one current-slot candidate pass
and checks the unchanged required principal fields and exact principal-object,
registration/callback/loop and actual-task identities. Tick and direct admission
cannot create or reconstruct it. A missing, expired, already-consumed,
copied/mismatched marker, replaced registration, copied/rebound principal,
child/inherited-context task or unwrapped tick/callback invocation stops before
those reads. Retain the same actual task and live marker through that one pass
and its unwind, alongside the unchanged original service/jobs/dispatcher/
reviewed-host/native-branch lifecycle. The wrapper invalidates it in `finally`
and restores its prior private context together with its ordinary runtime
context. Cancellation or exception cannot leave a usable marker. Inherited
context possession never substitutes for originating-task identity; copying
the exact principal object alone supplies no marker. No new grant, public
setter/constructor, owner registry, durable ScheduledJobRun or Goal authority
is permitted. All other R331 text and original admission authority remain
unchanged.
This scheduler principal proves operation ownership only; unchanged canonical
Goal/programme/Identity and current authority checks still govern admission.
Its lifetime begins before the earliest Goal candidate census and its pre-yield
probes, and ends after every selected admission and its original queue/publication
exit, or unwind. Allocate one original `HeaderReadBudget` before entering the one
original session. All candidates and selected admissions in that pass retain
that same session/connection and spent/reserved frame: no per-candidate renewal,
new task, owner, grant, authority, allowance or budget. Existing recovery before
the pass and later queued-job execution remain outside this admission lifetime
under their unchanged original contracts. Direct admission retains its separate
original contract; it cannot borrow a prior tick's frame or create a current-slot
owner retroactively.

Before any candidate Goal body, establish the genuine full composition issuer
with `fresh=False`, its original lifecycle/base/checkpoint/accounting lock, and
current common33 certification. Use only the existing source-owned bounded Goal
header/census surface and physical-reference overflow witness. Reserve and
explicitly debit full raw appearances, programme parsing/validation, ordering
and candidate/call copies before their delivery or construction. Preserve the
existing active predicate, sort_order/id order and current-day/no-catch-up
semantics after paid body delivery. No silent candidate limit, unguarded JSON
extraction, malformed-generation-to-empty conversion or all-Identity scan is
permitted. Capacity exhaustion stops this pass before its next body/effect;
another candidate cannot obtain a fresh frame. ADR-034's prospective selection
and ADR-035's conditional current-pointer read remain applicable only to each
actual original first insertion under their unchanged current-authority checks.

A is the actual insertion writer: each fresh `BEGIN IMMEDIATE` recertifies and
rederives current common33/required Identity3, canonical programme/issuer and
current authority, and the current pointer/HMAC/version, comparing the staged
pin without repinning. Matching insertion ends prospective selection. B is the
original persisted queue CAS writer on that same operation/session/frame, using
unchanged persisted selectors, immutable `validate_pinned` and current authority,
deadline/fence/readback. Any discovery continuation must authenticate this exact
lifetime; the existing native-memory-only restriction must remain effective for
every other caller. C is conditional publication at that same B writer's session
exit after successful CAS and before commit, not a third `BEGIN` or an after-commit
publication. Recertify invalidated evidence before later bodies and debit physical
checkpoint/delta/output/readback and all repeated appearances on the same frame.
Rollback, drift or denial refunds nothing and renews no deadline or authority.

On a blocked composed pass, emit at most one bounded warning through existing
managed backend logs with a closed stable denial category. Cover candidate
enumeration and admission failures; do not expose private values, identifiers or
arbitrary exception text. Use a pre-insertion category only when the original
operation proves that state; otherwise use a generic blocked-admission category.
A genuine empty pass needs no warning. Do not fabricate a discovery job,
ScheduledJobRun, audit row or durable UI receipt. This log surface establishes
visibility only, not durable adoption, successful admission or permission.

The original 128-distinct-reference/1,048,576-byte maximum, repeated-read/copy
charges, unfiltered schema LIMIT1360/1359 ceiling, common33 and conditional
Identity3 remain unchanged. No new witness, Source, Root, authority ledger,
retention component or document/parser capacity is granted. Unknown complete
closure or output cost, missing authentic owner, incompatible lifecycle/schema,
current-authority/pin drift or exhaustion remains STOP before the next effect.


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
