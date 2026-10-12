---
id: all-plugin-cordis-architecture
title: "ADR-026: All-plugin Cordis agent architecture"
---

# ADR-026: All-plugin Cordis agent architecture

**Status:** Accepted target, effective on the independently reviewed documentation
merge to `develop`. Runtime migration capabilities remain **Planned**.

**Decision class:** Target runtime composition; no runtime implementation or
stored-data migration is established by this decision.

**Tracked work:** [#981](https://github.com/seraph-quest/seraph/issues/981).

**Relationship:** Refines the Constitution's four logical responsibilities into
plugin composition. It does not supersede their authority boundaries, ADR-001/003
provider/state ownership, ADR-006/024 ordinary inference, ADR-025's narrow NEAR
exception, ADR-008 portability, or ADR-013/020 package protections.

## Context

The operator selected an all-plugin Cordis architecture on October 8, 2026.
Seraph currently has a Python/FastAPI runtime and React cockpit. Existing
extension registries, skills, capability packs and MCP adapters are useful seams,
but do not mean the runtime is already composed of Cordis plugins.

DeepSeek Harness provides a useful contributor-guide and composition reference.
Its upstream instructions are not Seraph policy, and its vendored, patched Cordis
fork is not interchangeable with a stock Cordis release. The inspected sources
and the limited ideas adopted from them are recorded below.

## Decision

Compose Seraph's agent runtime from Cordis plugins. Agent loop and orchestration,
goals/policy/authority, capability execution and tools, model integration,
persistence and memory, job scheduling, audit, and interface/edge adapters are
runtime plugin responsibilities. Only the necessary entry point, composition
validation and startup/shutdown wiring remain outside plugins. Pure types,
contracts and utility libraries need not pretend to be runtime plugins.

The four-layer model remains a separation of responsibilities, not four large
packages or an exception allowing a monolithic guardian runtime outside Cordis:

| Logical responsibility | Plugin composition responsibility |
| --- | --- |
| Guardian kernel | Goal/policy/planning/intervention and memory-coordination services, including the agent loop, supplied by trusted plugins |
| Capability runtime | Typed capability registration, durable jobs, approvals, execution enforcement, checkpoints, artifacts and bounded admission supplied by trusted services/plugins |
| Model fabric | Governed inference route, transport, capability proof and accounting services/plugins; providers supply inference only |
| Interfaces and edges | Cockpit/API/transport and consented-context adapters consuming typed services; never independent authority or canonical state owners |

“All-plugin” describes composition, not optionality. Policy, authentication,
authority, durable ownership and accounting enforcement remain mandatory trusted
services for work that depends on them. They are not user-disableable controls
that can be bypassed by selecting another plugin.

### Service, configuration and lifecycle contract

- Define typed service interfaces and data contracts. Each service has an
  explicit owner, required/optional dependencies and a bounded public surface.
  Declare dependencies rather than locating peers through ambient globals or
  importing their private state. Service availability never substitutes for
  authenticated authority to invoke it.
- Validate plugin configuration before activation and expose invalid/missing
  dependencies as actionable states. Reuse settings, vault references and
  existing policy revisions rather than creating raw env switches or putting
  credentials in generic plugin configuration. A config update cannot silently
  expand permissions or restore revoked consent.
- Let the composition bootstrap validate required service identities and
  readiness before admitting governed work. Missing, unhealthy or disposed
  enforcement blocks dependent work; optional adapter failures leave independent
  core work usable and show degraded/blocked state. The bootstrap has no parallel
  policy engine or separate agent loop.
- Each plugin owns its registrations, timers, listeners, tasks, resources and
  shutdown obligations. Stop new admission before quiescence, cancel or drain
  within original bounds, dispose owned resources and preserve durable recovery
  receipts. Dependency loss fences late work and adoption. Unknown cleanup or
  cost remains Unknown/blocked; unloading cannot erase liability or justify
  replay. Seraph must implement and test this policy rather than assume Cordis
  disposal alone proves cleanup or always propagates cleanup failures.
- Extension points register typed capabilities, approved inference/transport
  adapters or bounded event consumers under the owning service contract. Events
  are notifications, not authorization or a second job queue. Model output and
  external messages remain untrusted input through the same validation boundary.

Official Cordis source supports explicit dependency declarations, service
registration, configuration handling and lifecycle-owned effects. These are
composition primitives, not a guarantee that an application meets the preceding
Seraph requirements. Exact package choice, supported APIs and version pins require
their own reviewed implementation contract; no API recipe is mandated here.

### Three distinct trust boundaries

| Category | Trust and execution rule |
| --- | --- |
| Trusted runtime plugins | Reviewed application code in the trusted runtime. May implement mandatory services but cannot bypass their authority contracts. In-process loading/dependency injection is not sandboxing. |
| Reviewed authored capability packs | Keep ADR-013/020 exact package/version/content/schema/runtime bindings, review and activation, finite grants, sandbox/profile enforcement, output verification, quarantine/revoke/uninstall and recovery. Loading an authored package as a trusted Cordis plugin is not permitted by this decision. |
| External adapters and MCP integrations | Cross explicit network/process/credential boundaries; validate external data, declared capability and current consent/approval on each governed operation. Adapter installation or provider credentials grant no authority. |

No dynamic marketplace, arbitrary plugin import, unrestricted discovery, implicit
plugin installation or new trust grant follows from this ADR. Reviewed application
deployment and reviewed authored-package activation remain distinct processes.
Dependency injection cannot contain malicious code, revoke an already leaked
credential, or replace OS/process/network isolation. The migration must select and
verify any isolation boundary it needs; unsupported profiles remain blocked.

### Preserved Seraph invariants

- Canonical goals, memory, jobs, artifacts, approvals and audit stay Seraph-owned.
  Persistence plugins implement the existing logical ownership and durable
  contracts, not one database or ledger per plugin. Retain identity, provenance,
  backup/restore, encryption/private readback, revocation fences and recovery.
- Ordinary inference remains governed OpenRouter with explicit upstream/purpose,
  consent, capabilities and budget. ADR-025 permits only its separately governed
  optional NEAR HTTPS text capability, including its provider-plaintext disclosure.
  No local/direct-vendor fallback or provider-owned agent runtime is introduced.
- Preserve the shared remote-inference admission bound of one, priority,
  deadlines, cancellation, idempotency, reservations, authoritative cost evidence
  and unresolved liabilities. Plugin/provider boundaries do not create extra
  lanes, independent budgets or blind retries.
- Observation, data egress, execution, result adoption and learning remain
  separate permissions. Check current original authority before the relevant
  effect/adoption/read; restart or reload never renews expired authority.
  Capability completion still requires real governed execution, evidence/readback
  and explicit memory update or `no_learning`, with operator-visible recovery.
- Core operation stays CPU-capable on macOS and Linux without model servers,
  GPUs, VLM wrappers, native capture or optional sandbox services. Optional
  platform profiles show their actual readiness/proof independently. Runtime
  traffic uses approved APIs; SSH remains administration only.

### Bounded native Memory composition target

The accepted **2026-10-09** refinement tracked in
[#1007](https://github.com/seraph-quest/seraph/issues/1007) defines bounded native
Memory composition within the existing Seraph-owned storage contract. The fixed
fourteen-service, thirty-four-method migration remains **Planned** until its
complete governed execution, readback, recovery and lifecycle journey is proved.
Accepting this target establishes no runtime migration or native Memory
availability.

Each native Memory operation has one original budget of at most **128 distinct
canonical references and 1,048,576 bytes**. Schema metadata, complete row
appearances, repeated reads, file bytes and prospective output/retention copies
share that budget. Numeric spent and reserved capacity survives phase changes;
there is no renewed allowance after staging, snapshot rollback or a later claim.
The conservative SQL read set retains the unchanged **33-table common superset**
of existing Core15, inventory, native.v3, nine Memory tables and actual
accounting, operator-session and Secret dependencies. All retained common rows
count regardless of owner, status or selected-graph membership. Solely when the
exact original goal_public_discovery_v1 programme closure requires it, the
bounded programme component below additionally certifies the existing selected
operator_identities(id, created_at, revoked_at) bodies in the same original
frame. This is the only retained-field exception; it adds no table, index,
ledger, grant or authority. All other R1G restrictions remain in force.

[ADR-033](./033-task-method-composition-metadata.md) supersedes only the
closed schema-object allowance and ceiling below: the exact existing
`task_method_active` metadata and four indexes yield 1359 objects / LIMIT 1360.
Its metadata-only allowance adds no common body or authority.
[ADR-035](./035-first-admission-saved-strategy-read.md) separately permits only
the original owner's transient current-pointer read for first composed discovery
admission under the same frame; it adds no retained/common table or authority.
The original numeric
clauses below retain their decision provenance and are governed by that narrow
supersession.

Before snapshot row bodies, certify the complete superset on the original current
SQLite connection under the existing maintenance lock. Bound all schema-object
metadata to the source-derived ceiling of **1354 objects**, with unfiltered
`LIMIT 1355` providing the overflow witness. This includes the existing supported
automatic indexes, both principal operator triggers, and the fixed search
metadata below. Bound complete column metadata with `table_xinfo`; the 33
canonical body tables require exact mapped columns and `hidden = 0` before
bodies. Unknown, generated, hidden, unsupported or overflowing canonical schema
blocks this capability before private bodies. The ceiling is a certification
limit, not a claim about the deployed object's exact count.

The existing search owner's six FTS tables are auxiliary **metadata only**:
`session_recall_fts`, `session_recall_fts_config`, `session_recall_fts_content`,
`session_recall_fts_data`, `session_recall_fts_docsize` and
`session_recall_fts_idx`. Its nine exact triggers are
`session_recall_{sessions,messages,episodes}_{ai,au,ad}`. Validate their complete
source-derived DDL, exact names/table associations and bounded supported-runtime
`table_xinfo` and index metadata. The virtual table has six declared columns
and exactly two expected hidden fields, `session_recall_fts` and `rank`, each
with `hidden = 1`; its fixed metadata must match the reviewed layout. Shadow
metadata is exact, with no wildcard prefix acceptance.
Unrecognized layouts, triggers, extra FTS instances and statistics tables remain
denied. FTS/shadow bodies are never read by composition preflight, confer no
authority and add no canonical body reference. Metadata bytes spend the same
original 128-reference/1 MiB operation frame. Ordinary search initialization and
rebuild remain owned by the existing startup search owner; this target adds no model,
index, table, ledger or grant.

This search metadata allowance is the independently reviewed FTS amendment:
target SHA-256
`340704498684acfb69a77efb28a9663b2b99d0ec93145d62a4facac0c7add046`,
review SHA-256
`ca7cf2f297c21b1b733854fb1cfaf5d31454ec9d485b4a617106f134385eac24`,
source-manifest SHA-256
`139ff6fab666b26bacd425dc31432fcfd380dd71e118a984c85eb024a8863308`.
Later implementation proof requires a genuine persistent full `init_db`, both
principal triggers, all nine search triggers and six FTS tables, exact bounded
metadata, unchanged search metadata and a populated mixed canonical closure.
The search-only SQLite 3.47 fixture receipt is limited evidence, not full
initialization or native Memory readiness.

Earlier named source-owner reads are independently certified before their bodies
under that same original budget: authenticated OperatorSession, actual replay
run, the fourteen composition inventory rows, selected Task/attempt/Goal and
proposal, Source InputArtifact, full Session and full Secret inventory. Secret
inventory uses actual `Secret.id`, including ownerless and multiple-same-owner
rows. Early Forget validates the selected Memory, full Session and matching
Tombstone before the original owner reads; its bounded scalar tombstone lookup
uses the existing validated unique locator and preserves genuine absence.
Snapshot certification cannot stand in for those earlier reads.

Immediately after every actual fresh `BEGIN IMMEDIATE`, the original native
writer recertifies the unchanged whole 33-table common superset and, when the
exact original programme closure requires it, the named conditional Identity
certificate below, before returning to any body-reading caller, including
current Root and Memory-owner validation. Both use the same remaining original
numeric frame; Identity certification supplies no new capacity or authority.
Rollback, writes or schema changes invalidate certificates; a new transaction
requires fresh certification without resetting the numeric budget. Current
original Root, Goal, grants, fence, epoch and deadline remain independently
required. Read headers and numeric frames supply no execution, adoption or
learning permission.

Only genuine SQLite int64 WorkBoard event identifiers receive canonical decimal
**composition addresses**, consistently across enqueue, lookup, membership,
sorting, touch and delta accounting. Persisted `event_id`, constructors, API
cursors and original body/digest values remain integers. Existing supported
string addresses and their digest codec remain unchanged. This narrow address
adapter introduces no generic integer coercion or event rewrite.

Each physical dependency is header-certified and debited before its bytes,
including selected input/output/checkpoint files, report sources, deployment and
accounting receipts, and the fixed 4097-byte Vault-key fallback read allowance.
The fixed original readers use same-path nofollow, nonblocking descriptor opens,
regular-file checks and bounded reads while retaining their original ownership,
mode, link, size, hash and envelope validation. FIFO, symlink, incompatible
identity or unbounded input fails closed. Repeated reads spend capacity again;
a file header or SQL hash supplies no cleanup or effect receipt.

Reserve real prospective constructor, output, audit and Original/Current/Unknown
retention capacity before the first write, using original constructors and exact
returned projections. If that capacity cannot be certified, deny the native
mutation before its effect. Genuine producer begin/capture/validate, original
returned Effect identity and readback remain mandatory. Preadmission overflow
creates no job; failure after a genuinely committed queued admission preserves
that original job, protected recovery bytes and unresolved liability. Unknown
effects, suppressed Current output and debt retain their original recovery
owners across restart; certificates cannot authorize replay or adoption.

The accepted own-run refinement keeps Core15/native.v3 raw codecs unchanged.
Only the Memory-reference tuple temporarily replaces three lexical values in
its own Current record: the wrapper `state_digest` and own-row `tuple_digest`
with 64 ASCII zeroes, and own-row `encoded_bytes` with integer zero. Every other
journal byte remains exact, including Original, unrelated records, whitespace
and escapes. Current `rows_digest` hashes the sorted closed rows using those
same two own-row placeholders. The complete actual Current wrapper digest is
independently required; normalization grants no authority. Original remains a
genuine historical prepublication snapshot, never a Current substitute.

Retained producer context carries only the closed locator
`{profile, invocation_ref, original_checkpoint_id, original_checkpoint_digest}`,
without duplicate Current rows or digests. Counts measure complete raw tuples,
including the actual self-containing journal. Resolve decimal count lengths in
at most 32 bounded iterations before computing cryptographic digests, then
assert exact raw counts and their sum. The source-selected causal delta is
`{schema_version: 1, new_rows: [{ref, values}], updates: [{ref, columns}], owner_events: [{table, key}]}`,
with actual sorted addresses and exact resolved SQLite bind values. Exclude only
derived Current; include Original, unrelated journal changes, context, status,
lease, result, revision and all other causal fields. Full prior/new raw rows and
journals remain separately CAS-bound and charged to the original frame.

Capture requires the registered actual Source operation, the same sealed M5
plan consumed by its original owner and the identical actual returned Effect.
Stage the complete result and locator before computing Current, then publish
context, Original and Current atomically. Completion requires the identical
original claim held by the still-live registered Source, current original
authority checks and all pending completion fields before one writer CAS
advances the raw row and Current. Copy/restart cannot recreate that Source.
Own-run target SHA-256 is
`c7788ef63aea67418516e40778973c29d6d1fdc97c45a9cae8549461ac4308f7`;
independent architecture review SHA-256 is
`b657e3092f9acab4b4f8ed61e81589f645b84a774005120f24f8859c211f57f8`.

Before Original exists, bounded read-only retention may recognize the complete
Memory job universe under the same current common33 certificate and original
frame, additionally requiring the named conditional Identity certificate below
when the exact original programme closure needs it. This does not broaden the
Memory pre-Original classifier or permit missing Original/Current after effect.
Require a closed candidate/context, original serialized arguments and execution
fingerprint, native binding and actual claim journal when claimed, an inert
pre-effect state, `result = null`, and no Original, Current or effect receipts.
Negative terminal states `cancelled`, `failed`, `unknown_external_effect` and
`cost_liability` may remain readable under these same checks; `succeeded` and
`degraded` cannot use this classifier. Retain the entire unnormalized WRS with its existing
Core codec and joins. Foreign, unbound, terminal, corrupt and overflowing
members cannot be omitted. Readability grants no execution, adoption or future
Source permission. After effect, Source-independent Original/Current validation
is mandatory; Unknown grants no success or replay. Generic cancellation and
recovery cannot leave stale validated Current or reconstruct original capacity
from snapshot counts. Missing genuine owner or original capacity blocks mutation
while preserving Original, fences and liabilities.

The [selected scheduler/Google dependency exception](#selected-scheduler-and-google-readonly-sync-dependency-exception-1007) below additionally permits only its named original selected owner dependencies. It does not broaden the programme-only Identity rule to generic reads or change common33.

### Bounded goal-programme continuity exception (#1007)

This accepted-target amendment supersedes the fixed33/no-field-extension rule,
the fresh-BEGIN certification wording and the pre-Original same33 wording above
only as specified here. It also supersedes the accepted #1007 R1G no-field-extension
clause only for this named programme component. The prior FTS metadata amendment
(adoption SHA256 044aa6742494234dae8e6e0d0f627d1f007661631e57f6cf5ff68e7e32823432)
already owns the 1354-object ceiling and unfiltered LIMIT1355; this exception
adds no metadata allowance and leaves its metadata-only FTS restrictions intact.
The later [ADR-033](./033-task-method-composition-metadata.md) supersedes that
schema inventory/ceiling only; this programme exception itself adds no metadata.
All other R1G contracts, ADR-027's Python programme owner and ADR-030 remain unchanged.

**1. One original capacity frame.** Each applicable operation retains one original maximum of128 distinct canonical references and1,048,576 bytes. The common33 superset remains unchanged. Only when the actual original programme closure requires it, an additional explicitly named certificate covers the existing operator_identities table's exact `(id,created_at,revoked_at)` body for the original selected identity IDs. Those rows, schema metadata, repeated reads, raw tuples, prospective copies and outputs spend the **same original** frame. No reset/renewal, cap increase, identity-specific pool or renewed phase budget. If common33 certification or Current construction already overflows, this allowance does not rescue it: block honestly before the next body/effect and preserve original recovery debt.

**2. Conditional selection and metadata before bodies.** Certify unchanged common33 first on the original current SQLite connection/transaction, preserving its current unfiltered row census and the effective source-derived metadata ceiling1359/LIMIT1360 under [ADR-033](./033-task-method-composition-metadata.md). Except for the sole first-admission capacity-selection case in [ADR-034](./034-first-discovery-capacity-selection.md), obtain candidate identity IDs only from already certified exact original DISCOVERY_KIND declared authority and the selected canonical Goal's matching stored programme generation/issuer binding. Deduplicate original IDs; no name prefix/wildcard/all-identity scan or active/current-status filter. ID possession alone is not authority.

Before any selected Identity body, certify table metadata against the actual three-column SQLModel owner: exact columns id,created_at,revoked_at; hidden0/no generated fields; original nullable/storage/PK layout and existing primary-key single BINARY ascending unique locator; exact source-owned supported indexes. Bounded table_xinfo4, index metadata and the original schema census provide overflow witnesses; debit complete accepted metadata before bodies. Unknown column/index/trigger/locator, unsupported schema or insufficient original budget blocks before private bodies. `_rowid_`, typeof and octet_length headers for each exact original identity determine complete raw-body upper cost; actual genuine int64 physical references and full three-field appearances enroll/spend the same original budget before fetching strings/timestamps. Missing or duplicate selected row blocks; genuine absent/revoked identity never becomes a new grant.

**3. Current certificate binding.** Bind both certificates to identical original connection/driver/transaction/schema/data-change state and numeric budget instance. Immediately after each actual fresh BEGIN IMMEDIATE recertify common33 and any required named identities before body-reading callers. Rollback/write/schema change invalidates both; recertification does not reset spent/reserved numbers. Current Goal/programme/grant/capability/identity/route/budget/expiry, original lease/fence/epoch/boot/deadline and actual Source remain independent checks. Certificate/header/hash proves neither execution nor adoption/learning authority.

**4. Finite programme continuity component.** Add an explicitly versioned `native-composition-programme.v1` component for exact original composed goal_public_discovery_v1 occurrences and their canonical closure. Seed every such composed job, including historical/terminal/unknown originals; use original service identity, null execution sessions, immutable GoalDiscoveryAuthority and actual plan/brief/checkpoint/artifact/effect identities. Retain complete selected Goal.goal_programmes_json raw bytes, all generations/preview and actual Goal owner/revision/status. Include original issuer provenance and selected Identity revocation, exact Goal parent FK closure when physically needed, and original discovery files through the original programme reader. No generic retain-all selector or independent grant store.

Reuse existing complete common33 raw Goal/OperatorSession tuple certification/codec where the original closure already needs these rows; do not introduce a second hand-maintained full Root field manifest. Full Root body is private accounting/provenance and contains token_hash; external witness carries only hashes/counts, never bearer hash/body. Full Goal raw tuple is justified by preserving the indivisible existing programmes/history and canonical row/FK ownership during original restore, rather than copying selected JSON grants into a fragment. Identity adds exactly its source-owned three fields. Whole row/repeated appearances charge honestly; shared physical references deduplicate only the distinct-reference count, not repeated byte spending.

Every complete selected raw row and original physical file must match source/destination/provenance readback and be retained under existing bounds. Missing target bytes or unsupported original row/file blocks; neither synthetic data nor a digest replaces physical retention. Original programme files are allowed only through exact `goal-programmes/<original programme ID>/…` reader, kind/slot/job ID/artifact-ID/digest/size/effect provenance. No arbitrary prefix fallback or new file store.

**5. Version compatibility.** Preserve bit-for-bit existing Core15, native.v3, Memory Original/Current and common33 raw tuple codecs/digests. New programme rows/relations/component are domain-separated by a frozen explicit programme manifest version/digest. A new explicit witness envelope version includes the unchanged native.v3 component plus present/absent programme component, its exact selected rows/relations/file digest counts. Old receipts/checkpoints remain exact old version; never reinterpret an old digest as new, insert a default component into history or silently upgrade while work is live. Stopped maintenance under the existing lifecycle/accounting lock validates exact prior witness/closure and no pending gap before publishing explicit transition. Unsupported old-reader rollback while programme component exists remains blocked with original liabilities retained. Existing64KiB receipt/1MiB checkpoint/delta128 limits still gate before publication.

**6. Restore and rollback original authority.** Integrate actual original restore/rollback issuer revocation **before** calculating/publishing final target witness, through the existing authority invalidation/continuity owner. Retain issuer principal/identity/tombstone provenance unchanged; revoke restored browser authentication, never mint or alias a session. Original finite programme validator continues to ignore issuer browser expiry/logout/revoked_at for standing grant validity, while requiring canonical non-tombstone issuer provenance, unrevoked Identity, current exact active Goal/programme/grant/capability/route/budget and programme expiry. Explicit programme/Identity revocation is not weakened by restoration.

**Source/destination authority conflicts — fail closed before mutation.** Before any retained SQL mutation, issuer invalidation, checkpoint/delta publication or promotion, compare every selected existing source and destination complete raw Goal tuple and complete raw Identity tuple under the same original frame. Any differing raw field blocks the entire restore or rollback with no partial target write, occurrence or contact. Goal comparison is deliberately all22 current source-owned fields, including the complete raw goal_programmes_json, owner_principal_id, owner_session_id, revision and status; Identity comparison is exactly id,created_at,revoked_at. Harmless Goal metadata differences also block: this is an explicit conservative limitation until a narrower safe comparator is separately proved and reviewed. Identical tuples may be retained. A genuinely absent destination may retain the verified original complete source tuple unchanged, including paused/revoked state; missing source blocks. Never overwrite either direction's non-null Identity revocation or paused/revoked programme generation with an active/unrevoked value. Do not synthesize JSON, merge histories, choose a maximum timestamp, add a grant ledger, drop a generation or silently relax the rule to obtain restoration. Later current programme validation still denies retained source revocation. Existing session invalidation remains the original authority owner and must precede final witness publication; jobs stay blocked with higher fences/epochs and no restored Source.

Required additional restore/rollback negatives: source Identity unrevoked/destination revoked and source revoked/destination unrevoked; unequal non-null Identity revoke timestamps; source active/destination paused or revoked and source paused or revoked/destination active; full-history/ownership/revision/status conflicts and conservative harmless Goal metadata conflict. Every existing-tuple mismatch stops before the first retained write and promotion, admits no new occurrence/contact and leaves the entire target unchanged. Equal revoked/paused rows and destination-absent revoked/paused original source copy remain denied by the current validator. No synthetic history union or partial recovery is permitted.

All restored original jobs remain blocked, leases cleared, higher revision/fence and all14 composition owners blocked with higher epoch/current recovery receipts. Restored claim/start/effect/output receipts are historical proof only; original live Source is absent. No Source/lease replay or original deadline/grant renewal. Fresh current programme checks cannot make an old restored attempt executable. New occurrences use the existing native issuance/idempotency/outstanding-debt contract only after current independent policy/readiness/authority requirements hold; restoring a programme never itself grants route consent or learning.

**7. Focused START owner.** Accept only the R34 exact same-journal protected `runtime-service-invocation:programme-start:<claim_ref>` receipt/payload and original Source-sealed publication interface. Actual started GoalDiscoveryService registration binds identical original NativeServiceClaim/scope/host/boot/run task. Consumer in actual bridge task stages new original physical witness, obtains fresh short policy writer, checks current programme/claim/composition/lease/fence/deadline and absence of start receipt, and publishes one exact original CAS with unchanged earlier protected entries. Publication proof binds DB/session/statement/raw prior/new journal/receipt and identical live Source; generic checkpoint mint, copy or replacement denies.

Only original positive commit plus matching delivered start response permits that same original run task to execute once. Lost ACK retains the immutable start receipt and held original state; restart cannot reconstruct Source or replay. Ordinary executeAccepted RPC stays≤30/earlier original invocation deadline; original occurrence remains≤300/earlier programme expiry. This amendment adds no inference result contract or money settlement permission.

A small selected Memory graph can therefore be blocked by excessive unrelated
retained history. This is an explicit native Memory capability limit. Ordinary
Forget, full audit history and existing privacy-deletion semantics remain
preserved; the bounded native path must not truncate those histories or weaken
their owners to become eligible.

All eleven findings from the independent target reviews were accepted in the
exact R1G refinement. Decision provenance is target SHA-256
`11a26a8182cfdd138f9af833e2b46631b918872aee2a4b479c32b54f7b242a17`,
independent review SHA-256
`ee9160423c3b3452bd9e94a9a6e15c5243c458c5e3eeac98c054c4a895bd67cf`,
and contract source-manifest SHA-256
`9b91f2563f13249caefcfb302816accabe80833ca6d7ee84b4920de0965402be`.
These identify accepted target/source-review provenance; implementation requires
actual populated-event, no-body overflow, current-writer ordering, bounded
physical-reader, original effect/readback and recovery proof plus independent
cumulative review. Historical receipts or module/service availability establish
no native Memory permission or completion.

### Selected scheduler and Google readonly-sync dependency exception (#1007)

The accepted **2026-10-11** narrow refinement for
[#1007](https://github.com/seraph-quest/seraph/issues/1007) permits only the
conditional read-dependency boundary below. It is an accepted implementation
target, not product activation, availability, validation or completion. The
fixed fourteen-service/thirty-four-method migration remains **Planned** until
its complete original-owner journey is proved. This target resolves the R1G
additional-read-dependency STOP only for these selected existing owners;
separate capability and implementation STOPs remain.

Supersede the R1G no-additional-read-dependency rule and the programme-only named-certificate restriction solely to permit conditional certificates for these existing source-owned tables when required by the exact selected original scheduler or Google readonly-sync operation: scheduled_jobs, scheduled_job_runs, governed_schedule_bindings, governed_schedule_occurrences, google_service_connections, calendar_read_consents, calendar_event_bindings, operator_identities, mail_read_consents, mail_label_bindings and mail_message_bindings. The permitted complete source-order column tuples are exactly the durable roster below, subject to independent source/schema/locator validation before implementation. This creates no table, index, field, grant, authority owner, generic body API or new job permission. The common33 superset, its codecs and existing components remain unchanged. OperatorIdentity keeps exactly id,created_at,revoked_at; this addition permits its original operator-root Google sync use, and does not weaken the separate programme rule.

Select rows only through the exact original request/claim and owner-derived relations below. Certify unchanged common33 and each required selected table/schema/locator and row header on the same current original connection/transaction before its private body. After every actual fresh BEGIN IMMEDIATE, recertify required current bodies; writes/rollback/schema changes invalidate certificates. All metadata, complete raw rows/ciphertexts, repeated appearances, files, parser/copy/serialization/output and prospective Original/Current/Unknown capacity use the same original 128-reference/1,048,576-byte frame. No per-component pool, reset, refund, headroom or legacy unmetered fallback. Original Source/current Root/grants/Identity/Goal/revocation/epoch/host/boot/lease/fence/deadline and effect/readback checks remain independent mandatory checks. Unknown rows, locators, dependencies, historical liabilities or costs stop before body/effect.

Retain these additions in separately finite selected scheduler and Google components only after their complete raw tuple/relationship/file grammar and explicit version compatibility are frozen and independently reviewed. This target assigns no new component version identifier or manifest digest; do not reinterpret old receipts, silently append an absent component to history or activate from this table list. Preserve exact Core15/native.v3/Memory/common33 and the existing programme component bytes. A stopped transition under original lifecycle/accounting owners must prove the old closure and no pending gap; unsupported old-reader rollback remains blocked with liabilities intact. Restoration never recreates a Source, Root/grant/lease or deadline. Preserve current issuer/authentication invalidation ordering; retained ownership/revocation conflict stops before mutation, and jobs retain original blocked/fenced recovery behavior.

Keep the current unfiltered schema ceiling1359/LIMIT1360 under [ADR-033](./033-task-method-composition-metadata.md); this amendment supplies no metadata headroom. Google retains original encrypted page bounds (256KiB plaintext/384KiB ciphertext, at most3 pages,20 items/page,50 total and10 private items), with every actual repeated file appearance paid; those maxima may exceed the native frame and honestly block. Keep ordinary host RPC at at most 30 seconds intersected with the earlier original invocation cutoff. Google retains its existing absolute 120-second job cutoff, without renewal; a shorter native RPC/cooldown may block a valid ordinary sync. Preserve actual negative local cleanup after positive authority revocation under original immutable provenance/current bearer/physical readback; it confers no positive effect, private release or replay permission.

#### Exact finite source-order column roster

The union contains **11 existing tables and 200 unique `(table,column)` pairs**. The scheduler seven-table scope contains136 fields; the Google seven-table scope contains129. Counting the three shared tables in both operation scopes gives **265 operation appearances**. These are derived inventory counts, not body fees, reference limits, certification or permission to select every listed row. Only the exact required owner-derived closure is eligible. Complete raw ciphertext/string/JSON fields are retained without projection or synthetic normalization; the physical schema, locators and raw codecs remain pre-activation review gates.

- `scheduled_jobs` (scheduler): `id, name, enabled, trigger_type, trigger_spec_json, action_type, action_spec_json, session_id, created_by_session_id, last_run_at, last_outcome, last_error, last_approval_id, created_at, updated_at`.
- `scheduled_job_runs` (scheduler): `id, scheduled_job_id, job_name, trigger_type, action_type, session_id, created_by_session_id, status, outcome, error, approval_id, started_at, finished_at, metadata_json`.
- `governed_schedule_bindings` (scheduler): `binding_id, scheduled_job_id, owner_principal_id, owner_session_id, goal_id, goal_revision, capability_id, action_type, input_artifact_id, input_digest, action_digest, consent_kind, read_consent_id, consent_revision, consent_digest, schedule_idempotency_key, schedule_request_digest, cadence_kind, timezone, daily_hour, daily_minute, binding_revision, expires_at, state, last_slot_utc, created_at, updated_at`.
- `governed_schedule_occurrences` (scheduler): `occurrence_id, binding_id, binding_revision, slot_utc, idempotency_key, request_digest, claim_token, fencing_token, lease_expires_at, state, work_board_task_id, durable_job_id, metadata_json, created_at, updated_at`.
- `google_service_connections` (scheduler and Google): `connection_id, owner_principal_id, owner_session_id, service, label, vault_secret_key, credential_fingerprint, declared_scopes_json, provider_scopes_json, scope_status, setup_idempotency_key, setup_request_digest, state, revision, verified_setup_job_id, sync_active_job_id, sync_scope_digest, sync_cursor_job_id, sync_cursor_page, sync_cursor_revision, revoke_idempotency_key, revoke_request_digest, created_at, updated_at`.
- `calendar_read_consents` (scheduler and Google): `consent_id, owner_principal_id, owner_session_id, connection_id, creation_idempotency_key, creation_request_digest, connection_revision, calendar_id, goal_id, goal_revision, allowed_fields_json, window_minutes, max_events, sync_metadata_limit, allow_remote_model, expires_at, state, revision, consent_digest, created_at, updated_at`.
- `calendar_event_bindings` (scheduler and Google): `event_binding_id, owner_principal_id, owner_session_id, connection_id, connection_revision, consent_id, consent_revision, calendar_id_private, provider_event_id_private, recurrence_identity_private, provider_identity_digest, event_key, event_revision, calendar_list_revision, fetched_at, state, revision, snapshot_digest, created_at, updated_at`.
- `operator_identities` (Google): `id, created_at, revoked_at`.
- `mail_read_consents` (Google): `consent_id, owner_principal_id, owner_session_id, connection_id, connection_revision, creation_idempotency_key, creation_request_digest, goal_id, goal_revision, label_ids_json, window_days, max_messages, sync_metadata_limit, source_read_allowed, source_revision, source_digest, source_reviewed_at, model_egress_allowed, model_revision, model_digest, model_reviewed_at, allowed_body_fields_json, revoke_idempotency_key, revoke_request_digest, expires_at, state, revision, created_at, updated_at`.
- `mail_label_bindings` (Google): `label_id, owner_principal_id, owner_session_id, connection_id, connection_revision, provider_label_id_ciphertext, provider_label_digest, label_name, label_type, state, revision, created_at, updated_at`.
- `mail_message_bindings` (Google): `message_binding_id, owner_principal_id, owner_session_id, connection_id, connection_revision, source_consent_id, source_consent_revision, source_label_scope_digest, provider_message_id_ciphertext, provider_thread_id_ciphertext, message_key, thread_key, message_revision, received_at, fetched_at, status, revision, created_at, updated_at`.

#### Selected keys and original owner seams

| Operation | Original selection and body relations | Existing source seam |
| --- | --- | --- |
| scheduler.register | Authenticated actual ScheduleCreate captures consent, Goal revision, cadence, expiry, idempotency and issued input artifact. Consent PK with owner principal/session; connection PK from consent; Goal/OperatorSession/WorkBoardInputArtifact use common33. Binding lookup uses owner/session/idempotency unique key. ScheduledJob PK derives from original binding. Never infer cadence or authority from artifact possession. | api/calendar.py original create path; scheduled_jobs.py:666 load_governed_calendar_input_authority; governed_schedules.py:1215 create_binding and its guarded serialized BEGIN. |
| scheduler.disable | Exact original binding PK plus owner/session and expected revision. ScheduledJob PK comes from binding; original deterministic ScheduledJobRun control receipt PK; occurrence binding/slot locators retain actual needed prior debt, not active-state-only history. Wire disabled maps existing pause, not a new state. | governed_schedules.py:1331 apply_control; protected immutable control receipt, existing fresh-BEGIN callback. |
| scheduler.dispatchDue | Exact original registered canonical callback and actual slot; binding PK, unique binding/slot occurrence, actual prior lease/fence/debt. limit 1..20 cannot license a global body scan. **Still STOP:** original callback returns None and stores ScheduledJobRun identity as occurrence.durable_job_id, not a DurableJob result. | scheduler/engine.py:425 slot forwarding; scheduled_jobs.py:2642 execute_scheduled_job; scheduled_jobs.py:1067 and governed_schedules.py:189 existing identity semantics. A genuine callback-to-DurableJob same-root handoff still requires a separate concrete source disposition. |
| connections.invokeBoundAdapter | Exact original SyncRequest connection PK and owner/session; OperatorIdentity PK from current OperatorSession; original Secret key from connection.vault_secret_key remains common33. Actual request consent IDs with owner/session/connection; actual requested label IDs; mail message owner/session/connection/message_key and Calendar event owner/session/connection/event_key/provider-identity uniqueness. Existing cursor/source WRS is common33. No arbitrary operation_ref reconstruction or all-owner/status-filter-only retention. | integrations/connection_sync.py:192 _authority, :284 _synchronize, :312 existing connection_source_sync spec, :513/:562 binding adoption; original adapter loop and positive durable writer APIs. |

The same original connection_source_sync job and original _synchronize task remain the Google admission/claim owner, zero inference/max_attempts1/idempotency unchanged. Source/claim/dispatcher plumbing may be enabled only after its actual owner validators, complete certificate/retention path and full capacity forecast exist; a whitelist or method closure is insufficient. Each positive effect/artifact/readback/checkpoint/resource/success writer must recheck Source and current authority inside its original transaction. Original readonly Google contact includes OAuth refresh liability; provider429 cooldown and encrypted page limits remain unchanged. Real protected checkpoint/readback yields the fixed receipt/artifact result and no_learning.

#### Remaining facts, acceptance and stop gates

The eleven source tuples are concrete. Physical SQLite layout/nullability/storage/collation, complete supported locator/index predicates, relationship codecs, original selected historical/Unknown seeds, vault/adapter physical closure and future constructor costs are not certified by this source roster. Source index names do not constitute physical verification. Actual Google selectors without an established bounded unique locator must be certified as such or stop; this target creates no index. A complete source-derived finite codec/version/envelope compatibility diff must be independently reviewed before product activation. dispatchDue real-job handoff remains a semantic blocker separate from dependency selection; do not claim all34 complete.

Acceptance for the adopted scope requires genuine original operator admission/claim → stock host → original owner effect → durable protected result/readback/no_learning; source-only fixtures do not satisfy it. Include missing/copied/stale Source, Root/Identity/consent/connection/Goal cutover, wrong task/session/frame/connection, fresh-BEGIN/retry races, unsupported schema/locator, no-body budget overflow, repeated-read/Unknown full-U failure, nonselected-row denial, revoked positive release with lawful negative cleanup, and old/new witness/restore/rollback negatives. No max-size operation is promised to fit the existing frame. Preserve ordinary non-native owner behavior and historical witness bytes. Before reviewed product activation there is no native activation to roll back; after an explicit transition, only the proven original compatibility/restore owner may dispose it, retaining unresolved debt.

Independent review accepted this conditional boundary and retained every
schema/locator, codec/version, Original/Current/Unknown, full-capacity,
restore/rollback and actual DurableJob handoff gate. Its non-blocking arithmetic
finding is accepted: the earlier handoff count264 was wrong; the complete names
and tuples above are unchanged, with200 unique columns and265 operation
appearances. Decision source SHA-256
`756f9735e3fa29ce0ddf385ec647dc9f75d5d0c023a8abb6561d648b064b334f`;
independent review SHA-256
`cfea1946471d9b0cdcb64aeb4a34fa465f2dcd0c588fa0b7e8e5f630c93c4e5c`.
These identify accepted target/review provenance only; no ignored evidence
packet supplies normative field scope or runtime authority. A ready PR remains
the whole #1007 milestone, not this documentation slice.

## Migration Boundaries And Open Decisions

This documentation batch does not add dependencies, plugin packages, a runtime
loader, compatibility shims, provider calls or data migration. Current module
owners and managed lifecycle remain authoritative until reviewed implementation
milestones replace them. New work should expose clear ownership/contracts and
bounded lifecycle seams without preemptively rewriting the current application.

Resolve the following in separately tracked migration design/implementation:

- Exact Cordis distribution/version and whether any fork is necessary; importing
  DeepSeek's patched fork or package names is not a default decision.
- Runtime language/process boundaries and how retained Python/native execution
  integrates with Cordis, including authenticated IPC if separate processes are
  selected. This ADR does not mandate rewriting all Python in TypeScript.
- Package/service granularity, composition validation, dependency failure rules,
  configuration transitions and any upgrade/reload support. Hot reload is not a
  shipped guarantee or an acceptance requirement inherited from upstream.
- Ordered vertical migration slices, coexistence/fencing, compatibility and
  rollback, plus state-format/schema changes only if independently justified.
  Do not copy an upstream persistence format or move the canonical workspace.

These are implementation choices within the selected all-plugin target, not an
invitation to revisit policy, portability or canonical ownership. Execution
sequencing and review status belong in issues/PRs/Project, not this document.

## Verification And Consequences

This decision requires source/path/link and contradiction checks, both docs
contract scripts, documentation typecheck/build and an independent review of the
cumulative changes. Review findings and their disposition are recorded with the
owning issue/PR; acceptance does not establish runtime or isolation proof.

Independent content review on **2026-10-08** found no material findings in the
contributor guide and target-composition changes. The accepted review refinements
keep runtime plugins separate from authored packs, distinguish inspected source
from executed evidence, and preserve current implementation and provider scope.
This is documentation/architecture review, not runtime or isolation acceptance.

Future migration slices must prove one complete operator journey through actual
plugin composition, including start/stop and dependency loss; missing mandatory
enforcement; wrong/revoked/expired authority; shared inference priority/serial
admission; durable jobs/artifacts/audit; no replay after uncertain effects;
readback, memory/no-learning and operator-visible recovery. Verify platform
claims on their actual hosts. Retain state and unresolved liabilities across
restart/rollback; do not weaken checks to make the new composition pass.

Composition makes extension ownership explicit but adds dependency/lifecycle and
trusted-code supply-chain risks. The selected framework does not reduce Seraph's
enforcement burden. Existing shipped behavior remains described by
[Development Status](../STATUS.md) and the [Current App Guide](../12-current-app-guide.md).

## Inspected Sources And Adaptation

Inspected on **2026-10-08**:

- DeepSeek Harness commit `5badb15009ae1756c3afe0ae0cef1faafc290ccc` (2026-10-03):
  [agent guide](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/AGENTS.md),
  [architecture](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/docs/architecture.md),
  [vendored-framework notice](https://github.com/deepseek-ai/deepseek-harness/blob/5badb15009ae1756c3afe0ae0cef1faafc290ccc/vendor/README.md).
  Adapted annotated repository navigation, practical command conventions and
  explicit plugin composition/lifecycle ownership. Did not adopt its directory
  layout, launch commands, package names, provider policies or persistence format.
- Official Cordis commit `f8ea3cd50f1a5724e8e715995bcde131c9c12b2c` (2026-09-08):
  [registry](https://github.com/cordiverse/cordis/blob/f8ea3cd50f1a5724e8e715995bcde131c9c12b2c/packages/core/src/registry.ts),
  [service reflection](https://github.com/cordiverse/cordis/blob/f8ea3cd50f1a5724e8e715995bcde131c9c12b2c/packages/core/src/reflect.ts),
  [events/effects](https://github.com/cordiverse/cordis/blob/f8ea3cd50f1a5724e8e715995bcde131c9c12b2c/packages/core/src/events.ts),
  [runtime lifecycle](https://github.com/cordiverse/cordis/blob/f8ea3cd50f1a5724e8e715995bcde131c9c12b2c/packages/core/src/fiber.ts).
  Used to check dependency, service, configuration and lifecycle primitives;
  framework capability does not establish Seraph implementation or security.
