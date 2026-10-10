---
title: "ADR-030: Bounded iterative repository work and original-producer recovery"
---

# ADR-030: Bounded Iterative Repository Work And Original-Producer Recovery

**Status:** Accepted branch-local target for #1009; capability Planned, not Shipped on `develop`

**Decision class:** Target architecture

**Owning work:** [#1009](https://github.com/seraph-quest/seraph/issues/1009)

**Recovery amendment:** [ADR-032](./032-repository-crash-recovery-evidence.md)
supersedes startup classification by registration absence, ownerless deadline
evidence, and mutable-row retry identity. Its v4 protocol governs new admissions;
the v1/v2/v3 grammars below remain versioned historical contracts, not upgrade
instructions. All other bounded-work, authority, accounting, publication and
physical-closure requirements remain in force. Capability status stays Planned.

## Context

Bounded repository work extends the existing authenticated repository repair
Source, native Task adapter, physical repair lane and original accounting group.
A backend crash can lose its process-local completion witness even when the
original trusted supervisor completed its commands. Restart cannot reconstruct
that witness or pretend to own the vanished parent's process handle. Conversely,
an Unknown transition erases lease fields needed to verify an earlier immutable
Stop snapshot; accepting a freshly recomputed whole-Root hash would conceal drift.

This decision adopts the reviewed R207R6 durable-producer target together with
the final R212R3 predecessor projection and the reviewed R31 existing-artifact
and committed-Unknown verification refinements below. Earlier R160 drafting
evidence is historical and grants no authority. Architecture acceptance does not establish
implementation or enable recovery. Ordinary executor, publication and cutover
contracts remain in force outside the explicit mode below.

## Decision

### Original bounded work and consent

Use the existing closed seven-field `RepoWorkInput` and five-field
`RepoIteration` contract. Original source inspection and protected input/native
mapping bind one original C1 child to one repository Root. Copied DTOs, digests,
references, old unscoped Tasks and generic paused state cannot grant authority.
All iterations retain the original Task/Attempt, Root, Goal, owner session,
shared accounting group, fences and cutoff. Intersect the repository ceilings
of three iterations and 900 seconds with original C1 calls/cost/cutoff and
current canonical Goal limits. No new ledger, queue, lease or authority renewal
is introduced. Contacted and Unknown liabilities remain held at their full bound.

Planning, continuations and repository inference share the original reservation
writer and serial inference lane. Every model request requires fresh exact
consent for selected original source, actual tested cumulative diff, selected
command diagnostics, redaction version, route/model/options and the complete
serialized request, bounded to 64 KiB. Input patch and tested Git cumulative
diff have distinct provenance. Each patch needs exact manual execution approval;
that approval does not grant inference egress consent. Outcomes record
`no_learning=true`. Optional publication retains its separate preview, manual
approval, connection consent, private success witness and readback; Unknown
cannot create publication authority or replay an ambiguous write.

### Sealed finite inventories

Keep checkpoint key `repository:inventory:v1` and server-created schema
discriminators `repository.checkpoint_inventory.v1`, `.v2` and `.v3`.
At the original maximum of three iterations their exact inventories are:

| Version | Exact count | Authority |
| --- | --- | --- |
| v1 | 44 | Unchanged historical inventory and ordinary predicates |
| v2 | 45 | v1 plus `repository:stop-uncertainty-successor:v1`; read-only Pending projection |
| v3 | 49 | v2 plus three `repository:producer:<iteration_id>` records and `repository:physical-cleanup:v1` |

For smaller original iteration caps use the corresponding exact server-derived
vector. Seal version and full vector at NEW Root admission, before effects.
Keep the 50-record and 16-KiB-per-record limits over the actual inventory/history
union. Unknown/mixed versions, duplicate/subset/superset/foreign identities,
missing schema or caller-selected upgrades deny. Never rewrite or retro-register
historical v1/v2 Roots. Only originally sealed v3 can use this recovery protocol.

### Original producer registration before dispatch

The original trusted per-iteration supervisor owns durable completion, export
and stage cleanup. Its closed `repository.original_producer.v1` registration
binds exact Root/job/Task/Attempt/native invocation/iteration, original owner,
fences, execution/approval/source/profile/runtime/plan digests, original wall
and same-boot monotonic cutoffs, cleanup reserve, PID/start/boot/host identity,
pinned interpreter/supervisor/finalizer, nonce, public verification key and
descriptor-bound private completion directory/stage/guard.

After verified subreaper and pidfd readiness, that supervisor generates a fresh
Ed25519 private key using the existing cryptography dependency. The key stays
only in its memory; no request, marker, database, environment, log, output or
staged child receives it. Register the public key through the original live
process/control handshake, checking actual PID/start/boot and pinned sources.
The original configuration fence and SQLite IMMEDIATE CAS commit registration
against the exact current original rows BEFORE the first command ACK. Commit
without ACK dispatches zero commands; it grants neither replay nor success.

Each fixed command consumes an ordinal-bound current authorization on the SAME
original channel and within the same original plan/cutoff. Repeated/out-of-order
ordinals deny. Channel EOF permanently stops further dispatch. Restart cannot
open a replacement execution channel or run missing commands. Intended child
descriptors exclude the key and lane guard.

Completion storage is a deterministic sibling outside the removable stage under
the original private artifact root. Verify trusted owner-controlled ancestors,
0700 directory, 0600 single-link regular files, no-follow opened descriptors
and before/after device/inode identity. Admission, bounded original output
names, one completion envelope and optional bounded diagnostic prefix are
predeclared; discovery/glob or copied directories cannot replace the original
binding. Staging checks literal bundle/admission/output equivalence, original
cutoffs, current Source artifact TTL and producer guard closure before issuing
any private completion witness. Missing/changed storage fails held.

### Producer closure and durable transport

A fixed private finalizer preserves original input/base/tree/runtime,
tested-input/diff, command/readback/output checks and limits for the actual
Python or Node profile. It accepts original admission and owned observations,
not raw untrusted supervisor-result JSON or a generic finalization request.
The original producer drains child streams to EOF, closes descriptors, waits
direct children, terminates/reaps owned adopted descendants and observes Linux
ECHILD. It irreversibly enters no-spawn state, verifies/exports outputs,
removes only the descriptor-bound stage and records complete manifest/readback.
Cleanup reserve and a fixed one-second serialization/fsync reserve come from
the ORIGINAL wall window; insufficient time blocks dispatch. Recovery never
supplies a fresh execution/cleanup deadline.

The closed `repository.original_producer_completion.v1` envelope signs
domain-separated canonical bytes binding admission/registration digest, nonce,
identities/cutoffs, exact sequence outcome, streams/commands/output inventory,
source/tree readbacks, stage removal, child EOF/close/wait/ECHILD and original
finished times. Signing follows physical closure; no spawn follows signing.
Fsync each immutable output, install referenced names and fsync their directory
BEFORE linking the envelope without replacement, then fsync its containing
directory AGAIN. If directories differ, fsync output directory first and
envelope directory last. Failed fsync, mismatched bytes or incomplete bundle
deny. Durable bundle completion and later SQLite commit are separate points;
there is no cross-filesystem/database atomicity claim.

For this mode ONLY, `transport_kind=original_producer_durable_v1` replaces the
lost backend-parent pipe/wait predicate with authenticated original-producer
child transport, irreversible terminal state, complete durable bundle and no
live producer guard. The live backend may additionally wait its original
process. Restart never fabricates that process handle or its pipe/wait fields.
PID absence or guard freedom alone is insufficient. Ordinary executors retain
their existing transport predicates.

### Pure Unknown projection and independent cleanup authority

`repository:stop-uncertainty-successor:v1` retains its closed 12-key grammar:
schema, job_id, stop_digest, root_key, predecessor_digest, successor_digest,
authority_digest, fencing_token, from_revision, to_revision,
predecessor_projection and successor_projection. P and Q each contain EXACTLY
status, failure_reason, finished_at, lease_owner, lease_expires_at, result_digest
and result_summary. Capture actual original model JSON P before erasure and Q
from the actual transition, atomically with that transition. Never infer erased
values, normalize timestamps or repair old incomplete WIP records.

The Source-owned pure validator consumes the run's actual protected journal,
performs no filesystem/configuration/provider I/O and returns non-authorizing
immutable evidence. Require strict nonnegative integer revisions, one-step
`to_revision=from_revision+1`, and actual current revision EXACTLY `to_revision`.
P.status is running with saved nonempty original lease owner/expiry. Q.status
is unknown_external_effect; reason is the original callback/process
closure-unproven literal; finished_at and both lease fields are null;
result_summary is `result recorded`. Its strict digest uniquely matches the
closed `{no_learning:true, operator_action:reconcile_original_callback|reconcile_original_process,
iteration_id:<original>}` among at most three original server-derived identities.
Require exactly one match without another field or caller iteration choice.
Preserve actual datetime-model JSON round trips, nullable strict primitives,
1024-byte lease owner and 4096-byte reason/summary limits, lowercase 64-hex
digests and complete 16-KiB record limit; overflow denies without truncation.

From current full Root JSON J, replace ONLY those seven fields with P in a
LOCAL verification dictionary. Excluding only existing bookkeeping
updated_at/revision/checkpoint_receipts_json/heartbeat_at, its hash must equal
both predecessor_digest and the ORIGINAL immutable Stop Root static hash.
Independently require J's seven fields equal Q and its static hash equal
successor_digest. Original Stop bytes/digest, authority/fence/Root key and all
non-Root hashes remain exact. Changed nonstatus fields cannot be excused by
recomputed successor hashes. Never restore P to the database or ORM object.

For historical v3, only a NEW registered private Source completion witness from
authentic originally registered v3 producer evidence grants cleanup publication.
For NEW v4 Roots, ADR-032 permits the SAME existing private witness only through
authentic v2 registration/completion and its required durability proof, with all
current authority and physical checks preserved. The witness
binds producer registration/completion/output, Stop/projection digests and full
current Root/non-Root/authority/reservation rows. Copied JSON, deserialized
witnesses and v2 Pending metadata cannot construct it. One Source owner serves
live and recovery paths, stages physical facts outside SQL, and continuously
holds the configuration fence through its IMMEDIATE writer's exact current
row/revision/fence CAS. Append cleanup/readback, diagnostics and proposal
verification atomically; identical successors dedupe, differing successors
rollback. No reconstructed process callback/job map grants dispatch.

The writer reruns the SAME pure validator with current==to. Direct
Unknown-before-Stop retains exact original Stop/current equality without
inventing a successor. A genuine cleanup commit advances revision ONCE and
binds closed before/post revision and row proof in existing cleanup/readback
`source_completion_cas` metadata, not a new inventory ID. After commit re-stage
Stop context under the same fence before terminal CAS. v2's reader and the
ordinary Unknown current==to rule are never relaxed.

### Existing cleanup artifact and committed-Unknown verification

For future originally registered v3 publication, the existing deterministic
cleanup artifact has EXACTLY `physical_projection`, `source_completion_cas`
and `source_append_metadata`. The physical projection remains exact. CAS keeps
its eight keys: before_revision, post_revision, iteration_id,
producer_registration_digest, producer_completion_digest, stop_digest,
unknown_projection_digest and rows_digest. Append metadata is EXACTLY two
objects in cleanup-then-readback order, each containing only checkpoint_id,
safe=true and created_at. IDs are `repository:cleanup:<original iteration>` and
`repository:readback:<same iteration>`, the existing reserved identities. No new
path, inventory ID, CAS key, full-row preimage or public input is introduced.
Keep the existing 1-MiB artifact, 16-KiB record and 50-record bounds. Historical
raw/two-key artifacts cannot enter this path or be upgraded.

Source privately constructs and registers both exact metadata values before
writing the artifact. created_at is the actual original Source pending-record
constructor time, not producer finish or SQL commit time. Retry reuses those
literal values, never caller timestamps or newly chosen replacements. Bind
exact payload, original journal prefix, ordinal, actual task/fence/physical and
row epoch. Keep complete wrappers and state_digest out of their own artifact
to avoid a self-hash cycle; compare their complete metadata and payload digests
to the resulting journal. Ordinary append constructors remain unchanged.

rows_digest is the immutable audit commitment to the FIRST actual Source
staging's ordered full context identities/JSON, original proposal and approval,
and complete reservation vector ordered by operation identity. No field or
timestamp is removed or normalized. A legitimate auth touch can change current
session bytes; this receipt neither asserts historical/current row equality nor
grants current permission, and cannot reconstruct erased session history. A
same-receipt retry must independently stage fresh genuine physical evidence and
the complete current owner/session/Root/Task/Goal/native/Source/profile/TTL/
cutoff/proposal/approval/accounting/reservation vector, then compare that full
vector exactly against the actual IMMEDIATE-writer rows under the same fence.
Original group/Root membership, reservation status/cost bounds and full held
liability remain mandatory. No lease, budget, deadline or approval is renewed.

A DISTINCT hash-only verifier recognizes only the exact committed Unknown
successor: current revision = CAS.post_revision = CAS.before_revision+1 =
Unknown.to_revision+1. Preserve Root.updated_at literally. Its only accepted
Root changes are the exact final two cleanup/readback wrappers and that one
revision increment. Remove ONLY those bound wrappers and decrement revision
once in a local verification dictionary. Revalidate the complete existing
Unknown evidence shape (root_json, successor_json, stop_digest,
predecessor_digest, current_digest, revision, fencing_token); its digest must
equal unknown_projection_digest. Revalidate the full R212 grammar, original
revision pair, Stop/authority/fence and seven-field predecessor/Unknown semantics;
original Stop Root predecessor hash, reconstructed Unknown successor hash,
literal Stop snapshot and every non-Root static hash still match exactly.
Never restore an ORM row, fabricate a precontext or admit current>to through
the ordinary validator. R+3, a third append, changed prefix/metadata/payload,
updated_at or unrelated Root bytes deny.

Only fresh authentic signed producer/physical proof and literal artifact
verification may issue the separately registered weak-identity current-context
stage. Bind actual service/jobs, task/thread, owner/fence, active physical scope, original
registration, envelope/CAS and exact current Root/rows; copies, DTOs, digests,
serialized stages and metadata cannot register it. Perform file/source checks
before SQL and registry checks in memory inside SQL. Recheck the complete
current Source/proposal/approval/reservation vector in IMMEDIATE and read back
under the same fence/guard. This branch recovers only the original signed
completion/readback disposition: no new append, proposal delta, revision,
dispatch, release or finalization grant. Typed context cannot outlive the active
registered stage; scope exit revokes its witness.

### Stop ordering and startup protection

An existing Stop intent or expired original deadline allows negative cleanup
only. Under ONE continuous configuration fence, derive original automatic
limit evidence, persist immutable Stop intent in its original writer, COMMIT
and re-read/revalidate intent/current rows BEFORE reading/adopting producer
cleanup bundles or publishing cleanup facts. Preserve original pre-intent
snapshot behavior; an orphan snapshot is no cleanup grant. Failure before
intent is blocked; after durable intent missing proof remains held Pending.
Re-stage context again after cleanup commit before original terminal CAS.

A private lock-held Stop-intent helper preserves existing shape/events/CAS
and never reacquires the non-reentrant fence, signals, waits or stages cleanup.
Only the owner-issued private held-lock token permits this entry, not a public
boolean. Public Stop retains its ordinary lock/cancellation sequencing.
Recovery never calls public Stop or iteration preparation inside the held lock.

Before BOTH startup accounting recovery and stale-job mutation, classify
genuine originally registered v3 lineage under the startup fence/IMMEDIATE
epoch, including already-Unknown Roots. Indexed original native mapping and
canonical binding establish at most THREE WorkflowRunState IDs: Root, native
invocation and explicitly bound original parent; no recursive traversal,
sibling/Goal-wide exclusion or JSON substring authority. Use bounded keyset
pages and original metadata bounds, no filesystem reads under SQL.
The staged private deferral is evidence only and each mutation writer
revalidates exact mapping/registration/current rows before touching members
or their reservations. Preserve their status, full expired leases, revisions,
fences, results/effects/checkpoints and full held liability byte-for-byte.
Reserved debt is not relabeled never-contacted/released. Unrelated account
updates reflect only unrelated changes; wholly protected accounts stay exact.
Incomplete/ambiguous provenance blocks mutation of already established exact
lineage and exposes diagnostics; it never supplies guessed foreign IDs.
The former exception for absent registration on an allegedly undispatched v3
Root is superseded by ADR-032: absence cannot prove that execution never started.
Protect sealed producer-mode v3/v4 lineage even without registration; preserve
ordinary historical v1/v2 recovery only under ADR-032's positive classification.
Corrupt or contradictory evidence is never absence.

### Explicit Source recovery and held debt

Only authenticated Source POST `/api/workflows/repo-repair/{job_id}/source-recovery`
accepts exactly nonnegative StrictInt `expected_job_revision` and action
`reconcile_original_cleanup` or `settle_original_host_boot_cleanup`, extras
forbidden. Current authenticated SAME owner/session, original native/Task/
Attempt/Root/Goal/configuration/profile/TTL and expected revision are checked;
caller keys, paths, boot IDs, Stop reasons, witnesses or iterations grant nothing.
Return actual current/post-CAS revision; stale requests conflict without silent
mutation retry. No legacy settlement, generic resume, inference or command
dispatch occurs. Existing `/resume` retains sole execution approval consumption.

Reconciliation may publish authentic complete original proof or retry the SAME
durable Stop through the ordered gate. Still-live producer is Pending. Zero-
command/interrupted prefix, cancellation or missing checks are `held_partial`
cleanup facts only, not eligible failed results or next-iteration preparation.
A full original requested-check failure can prepare the existing next Source
review ONLY after cleanup, below original cap and within current original
authority, outside the completed recovery fence under preparation's own checks.
It cannot execute inference, grant consent or run a missing command. Profile-
defined build-failure test skips remain explicit, never fabricated test results.
At cap/expiry use the locked negative Stop path. Projection states distinguish
pending_original_producer, held_unknown, held_partial, continuation_ready,
original_cleanup_committed, original_stop_committed and physical_cleanup_only;
show reason, physical hold, original result and no_learning, never key/raw path.

Same-boot destroyed producer without authenticated closure stays Unknown with
held canonical/physical debt; no bounded automatic release is promised.
Separate registered native-host proof may establish an ACTUAL DIFFERENT kernel
boot on the SAME trusted canonical host, persistent machine/workspace identity,
trusted host proc mount/namespace and preserved original storage/registration.
Container restart, caller/edited boot values, foreign host and missing historical
registration deny. Unsupported native-host boundary blocks visibly.

This physical-only attempt has a fixed five-second administrative I/O cap,
never an old execution extension. Remove only the descriptor-bound original
stage and prove absence without signals/dispatch. Commit physical-cleanup and
existing release with exact `readback_scope=original_host_boot_cleanup`,
Unknown outcome and task readback unverified. Only the original repository
physical hold changes; Root/Task/C1 result, contacted costs and Unknown
accounting stay held. No terminal/certificate/publication authority is issued.
Local guard/quarantine release follows durable commit. Current owner/Root/
Goal/configuration/profile gates still apply; old Source TTL is not renewed.

### Trust and preservation

Retain `isolation_claim=none` and host-user execution trust. Signing authenticates
the original trusted producer under that assumption, not hostile-code isolation;
same-user code can attack files/processes or the signer. CPU/memory/PID ceilings
are not newly enforced. Unsupported Linux prerequisites or native-host proof
block this selected mode visibly; core startup and ordinary selected executors
retain their contracts. No real reboot or host provisioning is authorized by
this decision. Publication's effect transaction and other accepted cutovers
are unchanged.

## Consequences

Recovery attempts are bounded and operator-visible while unresolved debt can
remain held indefinitely. Durable transport can survive lost parent notification
without manufacturing process ownership. Exact read-only Unknown metadata
preserves immutable Stop linkage but cannot substitute for physical proof.
New v4 admission under ADR-032 cannot improve old historical authority. Status and Guide
describe this capability as Planned until its implementation and receipts land.
The adopted same-receipt rollback retry must survive a legitimate auth touch
with all fresh current checks. ADR-032 defines the separate immutable commitment
for new v4 Running and Unknown retries; the old mixed audit digest cannot supply
that authority. Implementation and acceptance of this amendment remain required.
Public Source recovery actions remain fail-closed 503 until acceptance.
Actual same-database managed backend kill/restart and API/UI recovery are still
unproved; branch-local mechanical receipts do not enable these actions.

## Verification

Require actual backend SIGKILL/restart against the SAME database/workspace,
original registration/commit-before-ACK and per-command EOF cases, complete
versus interrupted check outcomes, subreaper/EOF/wait/ECHILD/no-spawn closure,
file/directory fsync crash boundaries, key/receipt substitution and fixed-file
identity/size negatives. Prove startup BEFORE any accounting/stale mutation in
all candidate orders with running and already-Unknown Roots, reserved/contacted
liabilities, unrelated recovery unchanged, malformed provenance and rollback.
Exercise exact seven-field predecessor/current revision/nonstatus drift,
registered producer versus copied evidence, completion/Stop race and writer
rollback, exact post-revision retry, continuous Settings fence/no deadlock and
expiry Stop commit/readback-before-cleanup. Prove actual rollback then
due-auth-touch same-receipt retry for Running and Unknown, exact original
constructor metadata reuse, complete current reservation
membership/status/cost negatives, DB-only CAS/wrapper rewrite against unchanged
artifact, updated_at/suffix/prefix/R+3 tamper and copied/revoked/task/fence stage
rejection. Verify typed authenticated API/UI states, actual accounting/physical-
release readback and historical v1/v2 denial
of new authority. Native-host boot proof must distinguish actual trusted boot
from namespace/fixture changes without treating a test as an authorized reboot.
Provider-free ordinary checks and fresh independent cumulative review are
required; architecture acceptance or parser fixtures are no completion proof.
