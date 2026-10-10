---
title: "ADR-032: Repository crash-recovery evidence"
---

# ADR-032: Repository Crash-Recovery Evidence

**Status:** Accepted branch-local target for #1009; capability Planned, not Shipped on `develop`

**Decision class:** Target architecture

**Owning work:** [#1009](https://github.com/seraph-quest/seraph/issues/1009)

## Context

[ADR-030](./030-bounded-iterative-repository-work.md) defines the bounded original-
producer recovery target. Three evidence rules require a narrower replacement:
absent registration cannot prove that dispatch never happened; a finished time
signed before the envelope's final fsync cannot prove timely durable material
completion; and a historical full-row audit digest cannot serve as a retry
identity when legitimate authentication activity changes current session bytes.

At source baseline `7c16313e34647fd9cae49f5d297bc64e69181df3`,
`repo_repair_source._repository_startup_protected_lineage` skips a Root without a
producer marker before considering its execution intent. The intent is committed
before the original producer starts. `repo_original_producer.child_main` captures
finished times before writing and syncing `completion.json`; ownerless verification
cannot observe the original process's later failure. These are source-inspected
gaps, not runtime receipts. This decision accepts no implementation, activation,
successful task, release, or merge claim.

## Decision

### Precedence and unchanged authority

This ADR supersedes only the following ADR-030 clauses:

1. **Sealed finite inventories** and **Original producer registration before
   dispatch**, only for new version dispatch and admission of the revised protocol.
2. **Producer closure and durable transport** and **Stop ordering and startup
   protection**, only for durable timing evidence and registration-independent
   startup protection. In particular, replace the paragraph beginning “Absent
   registration on an undispatched v3 Root”.
3. **Existing cleanup artifact and committed-Unknown verification**, only for the
   additional immutable retry commitment and its versioned artifact grammar.
4. **Pure Unknown projection and independent cleanup authority**, only to permit
   NEW v4 original producers to reach the SAME existing registered private Source
   cleanup-publication witness through authentic v2 registration/completion and
   the required durability proof. Historical v3 rules and every current authority,
   physical, fence, full-row and liability check remain unchanged.

All other ADR-030 rules remain authoritative: one physical repair lane and shared
serial inference lane; original Task/Attempt/Root/Goal/owner and reservations;
original cutoffs, iteration/cost caps, manual execution approval, consent,
Stop-before-cleanup ordering, current authority and Source TTL, held Unknown debt,
continuous fence/guard, complete fresh staged-to-writer comparison, and no blind
retry, replacement execution channel, authority renewal, automatic learning or
publication. Later proof of historical timing cannot turn an expired task into a
successful task or authorize new execution.

### New admissions and finite version dispatch

Only NEW Roots may seal `repository.checkpoint_inventory.v4`. Its identity vector
is exactly v3's vector: `10 + 13n` identities for original cap `1 <= n <= 3`, hence
23, 36 or 49. Keep checkpoint key `repository:inventory:v1`, the existing identity
names, 50-record inventory/history-union ceiling and 16,384-byte per-record bound.
There is no new attestation checkpoint slot and no increase in journal capacity.
The inventory's existing six-key grammar remains unchanged apart from the schema
discriminator; seal its full server-derived vector before effects.

A v4 Root requires `repository.original_producer.v2` registration and
`repository.original_producer_completion.v2` completion. Their existing exact
31-key registration and 12-key completion-body grammars remain unchanged; their
new discriminators, pinned source digests and new completion signature domain
pin this protocol. The completion envelope remains exactly `completion` and
`signature`. Its signature domain is
`seraph.repository.original_producer_completion.v2` followed by one NUL byte.
The finished fields retain their diagnostic event meaning; they are insufficient
without the post-fsync attestation below.

V4 material transport uses exactly `transport_kind=original_producer_durable_v2`;
do not reuse v1 transport dispatch for the revised protocol.

Dispatch by the originally sealed version, never by whichever artifact is found.
Mixed/unknown schemas, duplicate/subset/superset/foreign identities, absent
version, changed pinned source or caller-selected upgrades deny. Retain the
current 16-KiB control-message and 2-MiB admission/completion-envelope bounds,
selected executor aggregate output bound, and descriptor-safe storage grammar.

### Startup protection without an absence inference

Before BOTH accounting recovery and stale-job mutation, protect every positively
identified sealed producer-mode v3/v4 Root, including Unknown and genuinely
undispatched Roots, whether registration is present, missing or corrupt. Existing
`repository:execution:<iteration_id>` intent supplies additional launch-risk
evidence; registration absence never supplies a never-dispatched proof. No
classification permits replay of a missing command.

Use the existing startup configuration fence and IMMEDIATE SQL epoch, indexed
canonical original native mapping and keyset pages of 64. Establish at most THREE
exact WorkflowRunState IDs: repository Root, its original native invocation and
its explicitly bound original parent. No recursive traversal, Goal/sibling-wide
exclusion, substring matching, filesystem reads under SQL or guessed foreign IDs.
Each mutation writer revalidates current mapping, mode and rows. Preserve protected
status, expired leases, revisions, fences, results/effects/checkpoints and full
reservation/Unknown liability byte-for-byte; unrelated work retains ordinary
recovery and wholly protected accounts remain exact.

Proven historical ordinary v1/v2 lineage keeps its existing generic recovery.
Missing/corrupt/contradictory mode or inventory cannot choose that fallback:
hold the IDs already established by exact mapping and expose bounded diagnostics.
Protection is deferral only; it grants no physical cleanup, financial settlement,
successful result or renewed authority.

### Post-fsync original-producer durability attestation

The qualifying MATERIAL bundle is the immutable material outputs plus
`completion.json`. Retain the existing mandatory five output names and the
profile's optional build stdout/stderr, complete manifest/readback and current
aggregate byte bound. After the original producer's irreversible no-spawn,
child EOF/close/wait/ECHILD, source/tree checks and stage removal, every material
file fsync and every necessary containing-directory fsync must return successfully.
Sync referenced outputs first and envelope directory last; descriptor binding,
no-follow, 0700 directory, 0600 single-link files and literal before/after identities
remain mandatory. No replacement, discovery or copied bundle can supply authority.

ONLY AFTER that final material barrier returns, the original signer samples both
clocks and checks their ORIGINAL cutoffs. It then signs the following closed
attestation, persisted at the admission-predeclared sibling filename
`durability.json`, bounded to 16,384 UTF-8 bytes including its envelope. This is one
original producer write, not a recovery writer or retry. The public verification
key comes exclusively from authentic canonical registration; the private key
remains in the original producer's memory.

The envelope has EXACTLY two keys: `durability` (the object below) and `signature`
(canonical base64 encoding of one 64-byte Ed25519 signature). Its signature covers
`seraph.repository.original_producer_durability.v1` followed by one NUL byte and
the canonical JSON bytes of `durability`, using the existing sorted-key, compact
UTF-8 JSON serialization.

The `durability` object has EXACTLY these 12 keys:

| Key | Exact type and binding |
| --- | --- |
| `schema` | Literal `repository.original_producer_durability.v1` |
| `completion_sha256` | Lowercase 64-hex SHA256 of the exact canonical material completion envelope bytes |
| `registration_digest` | Lowercase 64-hex digest of the exact canonical v2 registration |
| `admission_digest` | Lowercase 64-hex digest of the original admission bytes |
| `ready_digest` | Lowercase 64-hex digest of the original registered ready projection |
| `boot_id` | Original registered boot string, nonempty and at most 128 characters |
| `nonce` | Original registered lowercase 64-hex nonce |
| `observed_after_fsync_monotonic` | Finite JSON number, excluding bool, greater than zero, sampled only after the material barrier |
| `deadline_monotonic` | Finite JSON number, excluding bool, equal to original registered monotonic cutoff |
| `observed_after_fsync_wall` | Canonical timezone-aware UTC ISO timestamp sampled only after the material barrier |
| `execution_wall_cutoff` | Exact original registered execution-wall cutoff string |
| `original_deadline_at` | Exact original registered Root-wall cutoff string |

Require `observed_after_fsync_monotonic < deadline_monotonic` and observed UTC wall
time strictly earlier than BOTH original wall cutoffs. Exact bytes/digests and all
registration/ready/admission/boot/nonce/cutoff bindings must match, not merely parse.
Unknown keys, non-finite numbers, bool-as-number, malformed timestamps/signatures,
late observation, missing file, changed material bytes or failed fsync deny and
leave the work held. The signer never manufactures the attestation from a prior
timestamp, recovery observation, elapsed estimate, process absence or free guard.

Attestation transport and persistence are explicitly OUTSIDE the measured material-
barrier condition. It is evidence that the bound material bundle was already
durable before the signed observation, not an additional timed material result.
It may arrive and persist after the original cutoff IF the signed post-material-
barrier observation was before BOTH original clock cutoffs. Late observation
denies; late transport is not late observation. Retain the existing one-second
serialization reserve as an opportunity inside the original window, not a
guaranteed syscall duration or a fresh execution/proof deadline. Check BOTH
original cutoffs before EACH attestation write, link and fsync syscall; initiate
none after observed expiry. An already admitted blocking syscall may return
after cutoff: initiate no remaining proof operation then, close descriptors and
stop, without retry. Its already-in-flight operation may nevertheless make a
complete valid proof visible/durable for later readback. Expose storage errors/
stalls and hold guard/capacity until actual closure; missing/incomplete/invalid
proof remains held. There is no new administrative window, no commands and no
real-time fsync guarantee. Ownerless readback does not require proof that the
attestation's own final fsync finished on time. Moving a
timestamp before the attestation's own fsync cannot prove when that fsync completed,
and this protocol makes no such claim.

Recovery verifies literal material and attestation under the original guard and
same-boot host assumptions, current authority and existing ordered Stop/deadline
gates. Historical timely durability is not present permission: expired/Stopped
work permits negative cleanup only, no successful expired task, next-iteration
preparation or publication. This trust model retains `isolation_claim=none`; it
is not hostile-host clock attestation or hardware isolation.

### Immutable retry identity

The existing CAS `rows_digest` remains byte-for-byte the first-stage full-row audit
commitment with actual domain `repository.source_completion_rows.v1`. It includes
the original full context, proposal, approval and ordered reservation vector;
never remove fields, normalize timestamps or reinterpret it as historical/current
equality. A new DISTINCT retry commitment supplies immutable retry identity.

For authentic v4 publication ONLY, the immutable cleanup artifact has EXACTLY five
outer keys: `schema` (literal `repository.original_cleanup_artifact.v2`),
`physical_projection`, `source_completion_cas`, `source_append_metadata` and
`recovery_commitment`. Physical projection and existing eight-key CAS retain their
exact grammar. Append metadata remains exactly the two original cleanup/readback
objects in that order, with original `checkpoint_id`, `safe=true`, `created_at`.
Keep the existing deterministic artifact path, 1-MiB artifact bound, record limits
and inventory IDs; no new CAS key, checkpoint slot or full-row preimage is added.

`recovery_commitment` is a projection object, not an extra digest field. The
complete immutable cleanup artifact's SHA256 and literal readback anchor it.
If an in-memory comparison/address needs a commitment digest, derive SHA256 of
`repository.source_recovery_immutable.v1` followed by one NUL byte and canonical
JSON of recovery_commitment. Store no additional field and accept no caller-
supplied derived value; that derived hash cannot replace whole-artifact readback.
It has EXACTLY these 14 keys:

| Key | Exact type and binding |
| --- | --- |
| `schema` | Literal `repository.recovery_commitment.v1` |
| `domain` | Literal `repository.source_recovery_immutable.v1` |
| `root_key` | Exact `workflow_run_states:` plus `str(existing Stop _key(original Root))`; not run_identity |
| `before_revision` | Strict nonnegative integer equal to original CAS.before_revision |
| `root_static_digest` | Lowercase 64-hex digest of the explicit original Root static projection below |
| `root_updated_at` | Exact original Root `model_dump(mode="json")` updated_at string, without normalization |
| `journal_prefix_count` | Strict nonnegative integer count of the full original preappend journal array |
| `journal_prefix_digest` | Lowercase 64-hex digest of that exact ordered journal array, including complete wrappers, payload and constructor metadata |
| `owner_identity` | Exact four-key immutable session projection below |
| `non_root_static` | Exact ten-entry ordered array below |
| `producer_registration_digest` | Lowercase 64-hex digest equal to original CAS producer_registration_digest |
| `producer_completion_digest` | Lowercase 64-hex digest equal to original CAS producer_completion_digest |
| `stop_digest` | Null or lowercase 64-hex digest, exactly matching original CAS |
| `unknown_projection_digest` | Null or lowercase 64-hex digest, exactly matching original CAS |

Construct it ONCE at the first authentic private Source staging of the v4
completion, before either cleanup/readback append. Its root_static_digest and
journal prefix bind the ACTUAL Root at that staging epoch, whether Running or
already Unknown, not a restored earlier Running predecessor. Bind the original prefix and
before_revision separately. Do not hash complete future wrappers into their own
artifact; this avoids a self-hash cycle. Existing original metadata/payload/journal
comparisons remain exact on retry, not newly generated constructor timestamps.

`owner_identity` is EXACTLY `{id, principal_id, created_at, absolute_expires_at}`
from the original actual OperatorSession. id/principal_id are strict strings;
timestamps are their exact model_dump JSON strings without normalization.
Bearer token hash, authentication touch and idle expiry are not immutable identity.
Their current values remain mandatory in authentication and fresh full-row writer
comparison; excluding them from this historical commitment grants no authority.

Each `non_root_static` entry has EXACTLY `slot`, `table`, `key`, `digest`.
key is `str(existing Stop _key(actual mapped row))`; digest is lowercase 64-hex.
Require these exact slots/order/tables and canonical native/input/Task/Attempt
mapping, never incidental context-array ordering:

| Slot | Table |
| --- | --- |
| `original_parent` | `workflow_run_states` |
| `native_invocation` | `workflow_run_states` |
| `parent_task` | `work_board_tasks` |
| `parent_attempt` | `work_board_attempts` |
| `parent_input` | `work_board_input_artifacts` |
| `goal` | `goals` |
| `operator_session` | `operator_sessions` |
| `repository_task` | `work_board_tasks` |
| `repository_attempt` | `work_board_attempts` |
| `repository_input` | `work_board_input_artifacts` |

Running and Stop/Unknown staging MUST both materialize the same ELEVEN context
slots: Root plus these ten. The current Running context omits the original parent
input artifact; implementation must stage and validate that mapped row before
commitment construction rather than preserve a ten-row incidental vector.

V4 Stop uses `repository.stop_intent.v2` and `repository.stop_snapshot.v2` with
unchanged exact keys except schema and revised static-row hashes. Intent retains
seven keys, or nine for automatic-limit reasons; snapshot retains four. For EVERY
v4 static digest use SHA256 of `repository.stop_static.v2` followed by one NUL byte
and canonical JSON of the explicit projection. operator_session projection is
owner_identity EXACTLY. Other non-Root projections retain only existing Stop
`_static` exclusions: updated_at for every row; original parent additionally
revision/checkpoint_receipts_json; native invocation additionally revision/status/
failure_reason/heartbeat_at. No other fields are omitted. Preserve declared
baseline field sets and reject unknown fields for this version.

V4 Unknown evidence uses `repository.stop_uncertainty_successor.v2`, retaining
the existing exact 12-key grammar, seven-field predecessor/successor projections
P/Q, exact transition and checkpoint ID `repository:stop-uncertainty-successor:v1`.
Its predecessor_digest, successor_digest and reconstructed current digest use
SHA256 of `repository.stop_static.v2` followed by one NUL byte and canonical JSON
of the same explicit Root static projection used by v4 Stop. Every producer,
reader and known-post helper dispatches this hash by the sealed v4 inventory;
never combine a framed Stop hash with unframed Unknown Root hashes.
stop_digest and unknown_projection_digest retain the existing canonical
whole-object SHA256, including their new schema discriminator. Historical v1-v3
schema/hash semantics remain literal and unchanged.

The Root projection has exactly the following 61 declared WorkflowRunState fields,
excluding ONLY updated_at, revision, checkpoint_receipts_json and heartbeat_at:

```text
id, run_identity, root_run_identity, parent_run_identity, workflow_name, tool_name,
session_id, conversation_id, operator_session_id, status, branch_kind,
branch_depth, run_fingerprint, arguments_json, approval_context_json,
checkpoint_context_json, artifact_paths_json, continued_error_steps_json,
last_completed_step_id, error, started_at, finished_at, metadata_json,
record_schema_version, parent_job_id, parent_fencing_token, job_kind, owner_kind,
owner_principal_id, service_id, goal_id, goal_revision, plan_revision,
candidate_id, source_task_id, selected_context_reserved_bytes, capability_version,
input_digest, authority_digest, budget_digest, idempotency_scope, idempotency_key,
idempotency_binding, priority, dependencies_json, resource_claims_json,
declared_authority_json, deadline_at, lease_owner, lease_expires_at, fencing_token,
attempt_count, max_attempts, failure_reason, artifact_receipts_json,
effect_receipts_json, github_read_revision_json, github_read_observation_history_json,
github_capacity_closure_json, result_digest, result_summary
```

Its digest uses the same v4 Stop static domain. The exact journal-prefix digest
uses SHA256 of `repository.source_recovery_journal.v1` followed by one NUL byte
and canonical JSON of the full ordered preappend record array. All canonical JSON
uses existing sorted-key compact UTF-8 serialization. Do not substitute a set,
IDs-only list, prefix guess or payload-only digest.

Both Running retries and Unknown readback validate the commitment against its
ACTUAL first-staged Root epoch. An uncommitted retry requires that same Root,
static projection, prefix, revision and exact updated_at. A committed Unknown
readback strips ONLY the exact last two bound cleanup/readback wrappers and
decrements revision once in a local dictionary, under the existing exact post-R+1
proof. Its seven Unknown fields remain unchanged and root_updated_at remains
literal; compare this reconstructed PREAPPEND UNKNOWN to the commitment.
Separately validate the earlier Stop/P/Q predecessor and successor evidence.
Do not compare a restored Running predecessor directly to an actual-Unknown
commitment. A Running-to-Unknown transition under an orphan Running commitment
changes the epoch and remains held; this version grants no such retry transition.
R+3, changed prefix/wrapper/payload/static row/identity or unrelated Root bytes
deny; no ORM rollback or reconstructed authority is allowed.

Every retry independently stages fresh physical and complete current rows,
including original proposal/approval and full ordered reservation vector; compares
them exactly against actual IMMEDIATE-writer rows under the same fence/guard; and
authenticates the SAME active original session, current bearer, idle/absolute
expiry, revocation/replacement/tombstone state and ownership. Legitimate session
touch/refresh may leave immutable identity unchanged; revoked/replaced ownership
cannot pass current authority. No session, lease, grant, budget, cutoff or approval
is renewed. The immutable commitment is evidence, never a substitute for the full
fresh comparison or the separately registered private physical/current stage.

### Historical evidence and rollout boundary

Do not rewrite or retro-register historical v1/v2 Roots. Do not retroconstruct the
new attestation or retry commitment for v1-v3. Existing v3 benefits from conservative
startup protection, but an unsupported historical running orphan stays held.
Genuine existing live-process evidence is unchanged where it met the old contract;
an ownerless old envelope does not acquire the new timely-durability claim.
New v4 admission does not improve historical authority or forgive held debt.
Selected Source recovery remains blocked until the implementation and its required
provider-free acceptance exist; this ADR changes no endpoint, runtime or stored row.

## Acceptance And Evidence Limits

Implementation acceptance must exercise actual provider-free same-database managed
backend kill/restart and authenticated recovery, preserving one lane, original
cutoffs and held debt. Required cutpoints include intent-before-Popen,
registration-before-ACK, erased registration after command dispatch, missing or
contradictory mode, every material fsync/link boundary, a material barrier crossing
either cutoff, barrier-before-attestation, attestation loss/corruption, later
attestation arrival, interrupted/zero-command prefixes, and current owner/session
revocation/touch, source/Goal/proposal/approval/reservation/journal drift.

Check exact version dispatch, inventory counts/bounds and attestation/retry schema
negatives; historical evidence must not upgrade. Verify current authority and full
fresh row comparisons separately from immutable retry identity, and committed
Unknown's existing exact revision/suffix rules. Compare protected rows/liabilities
byte-for-byte and prove unrelated recovery remains ordinary. Fail-held outcomes
are required results, not permission for blind retry or weakened predicates.

**Product validation: NOT_RUN**, because this work is scoped to the contract and
implementation handoff. Future isolated regression/integration/security and actual
local-effect checks are not evals or model-quality claims. Documentation validation
and independent review receipts belong to the owning PR; they do not establish
product implementation, activation or runtime success.

## Consequences

Producer-mode registration loss conservatively holds even undispatched work.
Recovery requires new original timing and retry evidence rather than guessing
from process absence or historical mutable rows. Later proof arrival preserves
historical facts but cannot extend task authority or release unresolved debt.

## Source Pointers

Baseline `7c16313e34647fd9cae49f5d297bc64e69181df3`, source-inspected:

- `backend/src/workflows/repo_repair_source.py`: startup classifier L53–150;
  inventory parser/vector L491–599; execution intent and dispatch L2473–2505.
- `backend/src/workflows/repo_repair_source_recovery.py`: exact registration keys
  L120–130 and reader L133–220; commit-before-ACK owner L1143–1301; audit CAS and
  original cleanup artifact publication L1882–2010.
- `backend/src/workflows/repo_repair_stop.py`: existing static exclusions L131–146;
  full eleven-row Stop context L231–233; intent/snapshot construction L732–758.
- `backend/src/db/models.py`: declared Root field set L1819 onward and immutable
  session identity fields L3097–3106.
- `backend/src/execution/repo_original_producer.py`: bounds/domains L32–36;
  ownerless physical stage L367–443; admission/Popen L514–551; immutable file write
  L707–725; completion verifier L793–858; current pre-final-fsync stamps L903–925.
- `backend/src/execution/repo_original_producer_finalizer.py`: original physical
  deadline and closure checks L12–58; durable stage removal and output bound L126–158.
- `backend/src/workflows/inference_accounting.py`: startup protection L258–280.
- `backend/src/workflows/job_runtime.py`: stale/targeted protection L7955–7966
  and L8178–8186.

The source pointers locate extension seams and gaps; they do not establish the
proposed v4 protocol as implemented or runtime verified.
