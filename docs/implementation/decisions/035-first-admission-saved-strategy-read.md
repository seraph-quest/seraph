---
id: first-admission-saved-strategy-read
title: "ADR-035: First-admission saved-strategy read"
---

# ADR-035: First-admission saved-strategy read

**Status:** Accepted branch-local target following the operator's explicit
approval of the reviewed R90-R2 exception on 2026-10-11. Runtime composition
remains **Planned**. This decision establishes no implemented pointer read,
lawful admission, resource fit, runtime activation or shipped capability.

**Tracked work:** [#1007](https://github.com/seraph-quest/seraph/issues/1007).

**Relationship:** Supersedes only [ADR-033](./033-task-method-composition-metadata.md)'s
no-descriptor/no-body-read rule for the dedicated original TaskMethod owner's
current strategy selection during the first original composed
`goal_public_discovery_v1` admission, before first job insertion.
[ADR-034](./034-first-discovery-capacity-selection.md) still owns original
operation establishment and prospective Identity capacity selection.
[ADR-028](./028-reviewed-task-methods.md) still owns current selection, immutable
admitted pins, signatures, version validation and generic method JSON exclusion.
All other ADR-026/028/033/034 rules remain unchanged.

## Context and approved scope

Fresh admission must select the actual current saved strategy through
`CurrentMethod.resolve`; later `validate_pinned` authenticates an already-admitted
version without consulting the current pointer. ADR-033's metadata compatibility
alone cannot certify either signed pointer JSON body. The operator approved the
named read exception, including the R90-R2 requirement that the original owners
establish the same guarded operation before canonical authority and strategy
reads. This amendment does not assert that the current caller is wired that way.

The reviewed proposal and its source-accounting correction are recorded by:

- R90 proposal SHA-256 `d484ef627cb8006bffb2db2e3e2b2165db9697cfc2d430f45df3b99a12ff476b`.
- R90-R2 precision addendum SHA-256 `7663bb50cb141983ea911da66221400a513e1761c758f47621465b6d2c80f475`;
  input manifest `fbebabb55010813db496bee338128f39e592b684b9978e9f2c2c018df5d46248`.
- Independent R2 review SHA-256 `f6f063dd825c4006d6d4af404770b70d3fe72748810d160b63c6707b884ff547`;
  review pins `188a3af89be1e9d496e775ff08ae04c6a31fbacd119468e5cbc714a2b25ae91d`.

These identify the reviewed exception and limits. The historic proposal's
unaccepted status records its state before this decision; it is not a runtime
receipt or a competing accepted contract.

## Exact pointer and bounded locators

The existing owner remains `CurrentMethod`, through `TaskMethodStrategyResolver`,
with the actual started `GoalDiscoveryService` as admission consumer. Certify
only the complete existing `TaskMethodActive` / `task_method_active` row, keyed
by original `id`, in its original mapped order:

`id, owner_identity_id, goal_id, goal_revision, family, revision, binding_json,
previous_binding_json, baseline, signature_key_id, signature_mac, updated_at`.

Preserve all 12 fields' actual SQL types, nullability, primary key and rowid
layout. Both `binding_json` and `previous_binding_json` must be preflighted:
the original pointer MAC authenticates both parsed values with scope, revision
and baseline. A 64 KiB proposal/review limit is not a pointer-read certificate or
separate allowance. Reject extra, missing, generated or hidden columns. Bound
complete `table_xinfo` by 13 rows including its overflow witness.

Validate exactly the original primary-key locator, two named indexes and two
automatic indexes. The compound unique scope remains
`uq_task_method_active_scope(owner_identity_id, goal_id, goal_revision, family)`:
four ordered BINARY ascending key columns, unique, nonpartial, original
constraint origin and expected auxiliary rowid entry. Extend only the existing
certificate engine for this closed compound scope; no arbitrary predicate,
generic selector, new schema object or second engine is authorized. ADR-033's
complete 1359-object metadata ceiling and unfiltered LIMIT 1360 remain unchanged.

Derive exact owner Identity, Goal ID/current revision and fixed `research`
family from ADR-034's guard-authenticated canonical programme/issuer selection
and unchanged current programme/Identity validation. Caller-provided Identity,
strategy pin, scope or typed-object possession cannot select or authorize a body.
Before the full pointer read, use that exact scoped header locator with LIMIT 2
to detect ambiguity, delivering bounded original `id`, genuine int64 `_rowid_`
and storage/length headers. Prepay the complete selected raw row before either
JSON is delivered or parsed. No all-pointer scan, wildcard, prefix, status
filter or unbounded identifier delivery is permitted.

Preserve the original history-first and absent-pointer behavior. A closed
header-only existence probe for the exact Goal/revision/family uses LIMIT 1 to
detect a foreign-owner pointer without delivering its binding/body. Charge its
complete locator/header output and metadata; enroll encountered physical
references in the original frame. Absence evidence is transaction-current and
cannot certify an arbitrary scope. Accepted-history Proposal bodies retain the
original `MAX_VERSIONS + 1` overflow and malformed-family checks. A foreign-owner
pointer, accepted history with missing pointer, duplicate/missing selected row
or unavailable signing key blocks. Only genuine never-selected absence or an
authentic signed explicit baseline permits the original baseline behavior.

## Original owner, ordering and accounting

Establish the real started service, original admission task, jobs owner, reviewed
host and existing native branch through their original lifecycle. For the managed
current-slot candidate pass, establish that same original task's bounded
candidate/admission lifetime before its earliest Goal census as specified by
ADR-034; this decision does not independently establish a tick owner. Allocate
one original `HeaderReadBudget` before entering
`database.get_session(header_budget=...)`, including its pre-yield probes. Use
that exact session/connection/frame for candidate discovery and every selected
admission's authority, jobs lookup, strategy resolution, staging, insertion and
original queue/publication exit, without per-candidate renewal.

Before the first full canonical Goal/issuer/Identity read, use the existing
`prepare_composition_session(db, fresh=False, header_budget=original_frame)`
issuer with its genuine lifecycle witness, maintenance/accounting lock,
compatible schema-version-2 base and pending-checkpoint requirements. A
metadata-only read guard, direct guard construction, assigned `db.info`,
fabricated witness or `fresh=True` cannot manufacture first-admission readiness.
The issuer's rollback requires a new current certificate without resetting
spent numbers; its own snapshot reads must also be certified and charged.

On that installed full guard, certify common33, use the existing private
`_select_programme_admission` for the exact canonical programme/issuer facts,
then certify Identity3 and run unchanged current authority checks. Only then
resolve the current pointer through its dedicated original owner on the same
DB and spent frame. `validate_pinned` cannot substitute for fresh selection.
Preserve pointer HMAC, strict `ActiveMethodBinding`, canonical MemoryProposal/
Memory joins, source token/current Task/attempt/run/Goal checks, tombstones,
candidate digest and signed M5 provenance validation.

The pointer is a conditional transient owner read, not a 34th common snapshot
table. Keep the common33 superset and conditional Identity3 unchanged. Metadata,
locators, full raw appearances, repeated reads, JSON parsing/materialization,
returned candidate/pin copies and prospective outputs share the same original
**128-distinct-reference / 1,048,576-byte** spent/reserved frame. Distinct-reference
deduplication never discounts repeated bytes. There is no reset, refund,
renewal, cap increase, independent pointer allowance or phase pool.

Every exact common33 history/version body appearance must explicitly debit
`charge_row` before delivery on the current certificate and frame. Merely
entering `snapshot_reads` validates a certificate/context; it does not debit
ORM bodies. Registration of a descriptor also does not prepay parser, return or
output construction. Reserve future row/file/checkpoint/output construction
before its effects. Insufficient capacity denies before the next body/effect.

Immediately after each actual fresh `BEGIN IMMEDIATE`, recertify common33 and
required Identity3, rederive current scope, and repeat pointer header/selection/
HMAC/version checks on that same connection and spent/reserved frame before
the next body/effect. Staging followed by pointer drift blocks rather than
repinning staged data. Rollback/write/schema drift invalidates evidence without
refund. End prospective selection at original insertion/unwind; later claim/
START and retained-history paths keep their original persisted selectors.

## Authority and retention limits

Read/cost certificates confer no grant or execution permission. Preserve current
Goal/programme/Identity authority, issuer provenance, grants, explicit revocation,
fence, epoch, Source, deadline and revocation-before-publication ordering.
Browser logout/expiry retains the original standing-provenance distinction;
it cannot revive a browser Root or bypass actual revocation. Protected START,
matching delivered response and unresolved-liability STOPs remain mandatory.

This decision permits only transient current selection. Do not retain/copy
pointer bodies into Core15, native.v3, Memory Original/Current, programme witness,
leaf, manifest or envelope. Existing codecs, historical selectors and explicit
version transitions remain exact. No pointer writer/CAS/review/rollback path,
restored Source, handle issuance, future handle, cache pool or new authority is
made composition-compatible by this read exception. Generic TaskMethod bodies
remain excluded from ordinary recall, prompts, semantic indexes and selection.

If implementation requires pointer retention, historical authentication, restore/
rollback continuity or unsupported stored method relations, STOP for a separate
source-backed contract decision. Common33 membership does not prove selected
graph membership, physical retention or restore correctness. This decision
claims no complete saved-strategy restoration closure.

## Acceptance before implementation is relied on

Provider-free ordinary regressions and independent cumulative review must prove
the actual original owner path and same session/frame; schema/compound locator
and pre-body reservations; genuine selected row/MAC/canonical version; absent,
foreign and duplicate pointers; both signed JSON sizes; repeated/copy/output
exhaustion; fresh-BEGIN drift and unchanged revocation/provenance; and unchanged
files/jobs on pre-effect denial. Do not use an unbounded scan, standalone
resolver session, forced baseline, synthetic witness or renewed budget as proof.

Original caller integration, complete quantitative resource fit, native dispatch,
actual Source/claim/START, retention/restore and whole-milestone acceptance remain
unproved. This accepted target supplies no runtime, inference, quality or shipping
claim and authorizes no provider calls or evals.
