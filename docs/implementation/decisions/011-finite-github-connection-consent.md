---
title: "ADR-011: Finite GitHub connection mutation consent"
---

# ADR-011: Finite GitHub connection mutation consent

**Status:** Accepted target for #911. Implementation and shipped capability are
not claimed.

**Decision class:** Additive connection and capability authority contract for #911

## Context

Credentials identify an external account but do not authorize Seraph to mutate
it. Existing authenticated principals do not issue blanket external mutation
authority. The GitHub adapter's legacy principal-grant requirement therefore
blocks the real tested-repair publication journey. Effective-grants inspection
and revocation do not create missing consent.

## Decision

Issue explicit finite GitHub mutation consent through the existing authenticated
connection save. Persist its owner/root, exact repository, allowlisted actions,
server-issued identity, binding digest and expiry on the existing connection row,
atomically tied to its revision. There is no parallel grant store or blanket
EXTERNAL_MUTATION addition to authenticated principals. Credential-only and
legacy configuration never issue consent.

Active save requires explicit acknowledgment, exact revision and selected finite
actions: issue creation, comment creation, Git object writes, new-ref creation and
ready-PR creation. Duration is at most one hour and never exceeds the current
authenticated root's known expiry. A fresh login root requires explicit new
consent; data continuity, token refresh and credentials never extend or transfer
consent. No merge, release or existing-ref update is added.

Connection consent authorizes a supported GitHub boundary, not a particular
publication. Every job also retains its exact approval or existing finite
standing-reviewed authority. ADR-009 publication still requires the same fresh
approval for local_host_execution and all exact named remote effects. Canonical
job, preview and approval bindings include original consent/root/revision. New
consent never adopts old jobs or unknown effects.

The existing GitHub adapter derives permission from canonical owner/root,
connection, repository, revision, action and expiry at each effect boundary and
the protected post-resolution transport handoff. Caller Booleans and cached
inventory cannot authorize writes. Scope updates are blocked while reserved;
revocation remains available, advances the local fence and retains contacted
liabilities. In-flight uncertainty is visible and is not external undo.

Reconciliation is GET-only under separately verified original effect/root/
connection identities. A read may record verified external effect truth, but
overall job/task success requires a fresh canonical check of original owner/root
and exact Goal ownership/revision before adapter finalization and task adoption.
Changed authority leaves an explicit blocked/incomplete job/task and retains the
verified effect. The current connection revision grants read-only observation
and never replaces original job, approval or write-consent bindings.
Stopped or expired mutation consent cannot authorize
POST replay; expired authentication or another fresh root cannot inherit contact
authority. Existing explicit ownership recovery remains distinct from consent.

Expose root-bound expiry/actions/effective state and explicit opt-in in existing
GitHub connection settings and the derived effective-grants inventory. Migrate
only GitHub follow-through, dispatcher, WorkBoard and routine prerequisite checks
to this exact scoped boundary; other adapters retain their existing policies.

## Consequences and verification

The implementation extends existing connection, approval, job, vault, audit and
protected HTTP seams. Future mail/calendar/browser designs may reuse the
credential-versus-consent distinction but are not accepted by this decision.

Acceptance requires a real authenticated default principal issuing finite consent,
independent metadata readback, actual tested repair, fresh publication approval,
bounded local Git and protected intercepted REST publication/readback. Missing,
legacy, expired, revoked, wrong-root/repository/revision/action and before-contact
revocation races must send zero bytes. Restart/unknown reconciliation must prove
no duplicate or replay and no new-root adoption. Independent cumulative review
is required before claiming the capability complete.

## Accepted V3 recovery and capacity-closure contract

**Status:** Accepted target on 2026-10-03 by the
[owning #911 decision](https://github.com/seraph-quest/seraph/issues/911#issuecomment-5966748762).
This accepts the reviewed contract below; it does not claim implementation or
shipped behavior. Both prior review findings were accepted and addressed.

The accepted immutable proposal SHA-256 is
`039602b2d90efb35906a8805094ada3839034bf42778b8377c90744b3738bb83`;
the supplement SHA-256 is
`4d1916125d7e0f553db52d50e298355c74ccc82e896411da053ec35c84409c46`.
Independent design review passed with SHA-256
`f5feb4d15081f501f787b98a36f5045bdb18858a225bbe3fcb6f3102637d5580`.
The following text preserves the accepted V3 normative details in this canonical
ADR; proposal/review hashes identify the frozen evidence, not runtime proof.

### Operator behavior and exact request

Add an explicit capacity-close action to the existing publication recovery panel.
Its separate checkbox starts unchecked and explains that only this job's occupied
GitHub connection capacity will close; publication remains Unknown or blocked.
Neither publication approval nor mutation consent acknowledges this action.
POST /capabilities/github/repo-publication/jobs/{job_id}/close-capacity accepts
only this strict extra-forbidden request:

    acknowledged_capacity_close: bool, required and exactly literal True
    expected_job_revision: int, required, > 0, bool rejected
    expected_connection_revision: int, required, > 0, bool rejected
    expected_connection_fence: int, required, > 0, bool rejected
    idempotency_key: UUID string, required, canonical lower-case UUID
    remote_commit_id: optional 40-character lower-case hex Git SHA
    pr_number: optional positive strict int, bool rejected

The server derives principal/root, job/attempt/native capability, source/base,
immutable write consent, connection/repository/vault, all intent bodies and
process proof from canonical rows and private artifacts. Optional remote IDs are
bounded lookup hints; they cannot supply proof or alter expected intent bytes.
Numbers/strings such as 1 or "true" are not acknowledgment. No arbitrary expected
JSON, intent set, credential, grant, proof digest or process identity is accepted.

A durable close checkpoint binds canonical request bytes/digest and idempotency
key to this exact original job/root. After a lost response, the identical request
returns its one existing closure identity and canonical receipt with zero GET or
POST contact, even though successful close advanced the job revision. This
lookup precedes stale expected-revision rejection but still requires a current
original live root and the original connection/repository/credential identity;
it returns historical closure only and grants no authority. A different body,
different key seeking a second close, stale unrelated revision, changed root or
credential identity rejects. Exact simultaneous retries serialize on the guard
and canonical CAS; no second close or release occurs.

### Immutable scope and whole-operation bounds

This seam accepts exactly engineering.repo-publication.v1 and github_followthrough_v1 user jobs in unleased
unknown_external_effect, blocked or failed recovery state with the exact original
live root and occupied reservation. Fixed legacy follow-through recovery/closure is defined below, preserving its
existing known-ID positive GET reconciliation and explicit unverifiable blockers. Closure must reject a leased/running,
succeeded, cancelled or already differently closed job. Old deadlines, attempts,
Unknown status, source/test evidence, task/Goal/approval/finance history and all
contact/reservation evidence remain unchanged. Only job receipt revision and
the canonical connection reservation change on successful close.

After one preflight snapshot, set one 120-second monotonic readback/close deadline
and aggregate limits: at most 4,096 protected GET requests, 192 MiB raw response
bytes, and 128 MiB decoded object bytes. Reject immutable inputs with more than
2,000 expected files, more than 64 MiB aggregate file bytes, or a file larger
than 2 MiB; these match the local Git producer bounds and are not new allowance.
Every GET still respects the adapter's stricter per-response byte limit and
remaining wall time. A too-large valid object can therefore remain blocked.
The 4,096-call bound accommodates two complete 2,000-file passes plus bounded
commit/tree/ref/PR metadata and at most 20 pages of 100 changed paths. Reject
excess/truncated trees or PR pagination; never silently omit a path or infer
absence. Reuse already validated immutable payload bytes within one window to
avoid redundant reads while retaining a complete exact intent association.
Elapsed deadline or any aggregate/per-call limit retains capacity. A later
explicit retry is another bounded GET-only operation; it never extends the old
job deadline, resumes execution or replays any POST.

Inventory the entire canonical original effect ledger under the expected job
revision before contact. Every potentially contacted local or remote intent must
have positive exact evidence: current protected adapter GETs or previously
canonical sealed positive readbacks for that same original serialized intent.
Previously canonical readbacks remain historical effect truth but cannot bypass
the current read-root/connection/vault checks. No 404, ambiguous response,
unsealed receipt, caller JSON, missing ledger row or truncation proves absence
or closure. Verify the ledger is unchanged at the final transaction.

Partial blob/tree/commit/ref publication must be recoverable without a PR. Derive
blob identities and bytes from immutable original producer artifacts; trees from
the complete exact local tree; commits from the exact original REST intent
message/tree/parent/author/committer/date; refs from original new-ref intent.
If the REST commit identity is unknown, only a supplied locator with full exact
GET validation or an already recorded matching remote identity is usable;
otherwise report a blocker rather than search unboundedly or fabricate absence.
For a PR intent require the exact ready open PR repository/head/base/title/body,
complete tested tree and changed-path membership. Singular GET /git/ref/heads/
{branch} verifies a ref; plural /git/refs is POST-only. No omitted-intent route
may release capacity. Existing observed artifacts and original effects survive.

### Producer lifetime protocol and guard

The trusted per-job supervisor contract below owns actual child lifetime, guard,
reap and durable terminal proof independently of request-parent lifetime.

### Exact transaction and staged artifact boundary

Everything that can authenticate, open another DB session, contact a provider,
resolve DNS, inspect/wait for a process, read/hash/write a file or decrypt a
credential happens BEFORE BEGIN IMMEDIATE. Acquire the producer guard first;
preflight-authenticate the original root; complete bounded GETs and sealed
semantic verification; check producer lifetime/result; construct a bounded
private content-addressed closure artifact (maximum 256 KiB), write with
descriptor-relative no-follow exclusive creation, verify exact bytes, and build
its prospective registration. Existing identical bytes are idempotent; a
different existing file fails. A staged orphan after rollback is not canonical
evidence and is harmless on exact retry.

Then use ONE canonical DB session and ONE short SQLite transaction. No calls to
authenticate_session, GitHubReadbackAuthority.validate, vault_repository,
filesystem helpers, other sessions, process waits, DNS or HTTP occur inside it.
Using the same DB session re-read/check the original OperatorSession row: exact
principal/root, not revoked/replaced/tombstone, current idle/absolute expiry;
exact job owner/root/native kind, revision/attempt/authority/input/run fingerprints,
unleased recovery state, immutable original write binding, complete ledger digest
and producer terminal proof; exact connection ID/repository/owner/current read
revision/mode/active_job_id/active_fence; exact vault row ID/owner/name/update and
encrypted-value digest; and closure idempotency/permanent-fence state. Compare
vault row identity/digest to the preflight snapshot using an in-memory pure
digest of already loaded row values, without decryption or nested repository.
Time validity uses current UTC and no grace. If any binding changed, roll back.

Atomically register the staged private artifact, append the exact closure
checkpoint/idempotency binding and permanent old-job execution fence, increment
only receipt revision, and CAS-clear this exact connection active_job_id using
the expected revision and fence. Preserve active_fence unchanged; next normal
reservation increments it. All these DB changes commit together or none do.
No contacted effect or finance liability is freed/removed. Hold producer guard
through commit; release afterwards. Interrupted staging or a failed CAS retains
the original reservation and unknown evidence.

Permanent fence is canonical, not an in-memory flag: closed job can never
execute, approved-resume, claim/recover a lease for execution, spawn a local
producer, pass a POST handoff, reserve or reacquire this/another connection,
finalize succeeded, or be adopted by a task. Audit every one of these routes.
Cancellation/reconciliation cannot remove this fence or release capacity again.
A successor needs a distinct job, fresh finite consent, preview and exact approval.

### Durable GitHub read-revision receipt and narrow WorkBoard adoption

The protected adapter mints a typed sealed receipt from actual GET response raw
bytes AND validated endpoint semantics, original serialized effect body and
canonical effect identity. It records semantic payload digest separately from
raw payload digest; canonical JSON alone is not raw-byte proof. Persist its
server-derived envelope on canonical recovery effect/checkpoint/artifact rows:
schema/receipt ID, original root/principal, exact native job kind and public
capability mapping, job/attempt/count/revision/authority/input/run fingerprints,
latest WorkBoard attempt/task/fence where bound, immutable original write-consent
binding, current READ connection revision, connection ID/repository, original
vault ID/owner/update/encrypted-value digest, protected GET path/raw+semantic
payload digests, exact original effect ID/type/path/body digest/idempotency and
verified readback ID/time/digest. Caller supplied mapping or unsealed proof
cannot create this canonical receipt. Bound server-only alias mapping is exactly
github_followthrough_v1 -> work.github-followthrough.v1; no wildcard alias.

Adapter finalization first verifies current canonical original root and Goal
owner/revision. A changed Goal allows durable effect observation only and retains
Unknown/blocked job truth. Task adoption later reads the PERSISTED canonical
receipt in the same board CAS session and verifies current root, current Goal,
task owner/root/revision, latest attempt/fence, exact root native kind/public
capability/idempotency/input/authority/run fingerprints, exact successful sealed
effect readback, original immutable write binding and current connection read
revision/vault identity. A changed connection since GET requires fresh GET.
Allow active, disabled or reconcile_only mode only for this GET-based seam.
Remove blanket EXTERNAL_MUTATION only within this exact reconciliation branch;
keep every #915 latest-attempt, unknown block, root and Goal safety guard. No new
root/consent/current read revision replaces original write authority. No closed
root may be adopted. This exact V3 contract is accepted for implementation by the owning decision linked above.

### Post-closure read-only observation without reservation

Choose the minimal useful option: a separate closure-bound GET authority, derived
from the canonical durable closure checkpoint, works WITHOUT active_job_id or
the old active fence. It binds original live root/principal, closed job and exact
closure identity/history, original immutable write binding, original connection
ID/repository/vault identity/digest, and a separately acknowledged current READ
connection revision. It never uses or modifies a successor's reservation.
Every DNS/handoff/response boundary rechecks these current identities. Root
expiry/revoke/replacement, connection replacement/repository change or credential
rotation blocks contact. Fresh login/new consent cannot inherit authority.

Post-close GET observations may append bounded private readback evidence under
the same durable observation-only contract, but cannot write remotely, finalize
success, adopt a task, change Unknown/Goal/approval/finance history, release more
capacity, reacquire a reservation or weaken the permanent fence. Requesting
observation requires its own literal True acknowledgment and exact current read
revision, with the same whole-operation GET bounds. Discovery/GET projections
do not automatically acknowledge, prepare, execute, reconcile or close.

### Durable operator projection and proof

Existing exact-owner/root repair/publication discovery and remount projections
show closure identity/time/job/connection, all potentially contacted effects
observed, capacity closed, job/task still Unknown or blocked, no_learning and
unchanged approval/Goal/finance history. Unresolved-first pagination is bounded
before filtering and validates fixed native kind, immutable dependency, repair
and original root. Controls remain explicit and initially unchecked. No automatic
POST follows discovery or stopped mutation consent.

Required proof before implementation completion: actual inherited-guard fixed
Git child survives parent crash and prevents closure; direct reap/drain/group-empty
terminal proof is required after exit; missing legacy proof stays blocked;
partial blob/tree/commit/ref/PR intent sets positively close only exact capacity;
404/truncation/over-limit/raw-payload semantic mismatch has zero release; all
root/Goal/connection/vault revisions and races fail closed; short transaction has
no nested auth/DB/file/network/process calls; concurrent/lost-response retry
creates one artifact/checkpoint/fence/CAS release; late worker and every reserve/
POST/resume route reject old closed job; future exact-root GET works without
reservation and cannot disturb a successor; WorkBoard restart uses canonical
persisted read revision and preserves all #915 guards. Intercept actual protected
transport only; no GitHub account write or paid inference is authorized.

### V3 supervisor ownership and useful parent-crash recovery

The publication producer has a trusted, per-job supervisor process whose lifetime
is independent of the API/request/producer parent. It is a fixed job-owned helper,
not a daemon, scheduler, general executor or new authority store. Its helper
digest/interpreter/runtime identity, fixed argv/environment, finite inputs,
original approved job/root/attempt/authority/fence, stage identity and private
unpredictable token are captured in preview/approval and canonical admission.
The supervisor receives no GitHub credential, network authority or arbitrary
command selector. Its fixed Git commands retain existing hooks/config/filter/
protocol exclusions and original 30-second producer wall and output bounds.

Reuse existing repo_worker PID-start/process-group/quiescence and bounded output
primitives, plus repo_sandbox's private no-follow atomic marker practices where
semantics fit. These current primitives do not themselves survive parent death;
only the new bounded supervisor owns the direct child and pipes independently.
No shared native executor behavior or host provisioning is redesigned.

The caller acquires/passes the job-private guard to the supervisor; the supervisor
retains exclusive guard ownership through direct child completion, output drain,
positive process-group-empty readback and durable terminal marker publication.
Every allowed fixed Git child inherits the guard descriptor. The caller's death
does not close the supervisor's descriptor, pipes or ability to reap its child.
Before child input is written, the supervisor atomically records exact boot ID,
supervisor/child PID-start identities, group, command/input digests and original
admission token/fence. The supervisor handles only the one admitted fixed command
stream or complete fixed producer invocation and never waits for an unbounded
parent input. Every authority-sensitive next command requires a bounded current
authorization response; parent disappearance prevents new commands, while the
already admitted child may finish within its original remaining deadline.

After request-parent death, the trusted supervisor continues draining its owned
child's bounded pipes, directly reaps it, proves the recorded process group empty
within the original deadline, and captures immutable stage/output digests and
whether the complete producer or only an admitted prefix finished. It writes and
fsyncs a private no-follow content-addressed terminal marker, bound to the exact
approved supervisor helper identity/token/fence/admitted inputs, before releasing
the guard. Recovery verifies actual marker bytes, admission binding, immutable
output bytes and positive terminal facts; then registers that supervisor-minted
proof through the existing canonical checkpoint seam. It never claims an
incomplete local producer prefix is a tested publishable commit. Prefix completion
can establish producer quiescence for capacity closure only when all potentially
contacted local/remote intents independently have their required positive proof.

A supervisor dying before durable terminal proof, missing/legacy producer proof,
unreadable process identity, surviving descendants, timeout without positive
cleanup, changed stage/output/helper or dropped descriptor stays explicitly
blocked. No lock absence alone, recovered/reused PID kill, retroactive fixture or
caller marker establishes completion. Optional host mechanisms remain blocked
until actual supervision/guard/quiescence proof exists there; Linux proof does
not claim macOS readiness. Shared macOS/Linux CPU core remains unaffected and
host-user execution continues to declare isolation_claim=none.

Required actual process proof: start the real fixed Git child at a bounded
producer command with its supervisor owning guard/pipes; kill the actual request
parent; prove the child and supervisor survive and closure is denied while live;
allow that child to naturally exit; observe supervisor direct reap/output drain/
group-empty/immutable digest marker; restart canonical recovery and positively
close exact capacity with no new command or POST. Separately kill the supervisor
before proof and prove named permanent blocking, without a fake terminal row.

### V3 exact legacy follow-through coverage

Supported native kinds are exactly engineering.repo-publication.v1 and
github_followthrough_v1. Public capability mapping is fixed server-side:
engineering.repo-publication.v1 stays itself; github_followthrough_v1 maps only
to work.github-followthrough.v1 when canonical declared authority agrees. No
caller-provided capability selector, alias wildcard or unrelated adapter enters
this contract. Native kinds retain separate fixed routers:

- publication: POST /capabilities/github/repo-publication/jobs/{job_id}/close-capacity
- legacy: POST /capabilities/github/jobs/{job_id}/close-capacity

Both derive and call the same narrowly internal canonical closure transaction,
permanent fence and exact root/connection/vault/revision/idempotency contracts.
The legacy strict request uses the common literal-True/job-revision/connection-
revision/fence/idempotency fields and optional positive strict remote_id only;
publication lookup fields are rejected. There is no local producer requirement
for a legacy job with no local process effect: mark that element non-applicable
from canonical fixed kind and complete effect inventory, not a caller Boolean.
The legacy closure read window remains at most 120 seconds, with at most four
GETs, 4 MiB aggregate response bytes and the adapter's existing 1-MiB per-response
limit; it cannot raise the shared maximum. One complete original github_publication
effect is expected; any additional unrelated or unidentified potentially
contacted intent blocks closure rather than being ignored.

Preserve and prove the useful existing ordinary reconciliation path: a known
positive issue/comment ID, original live root/principal/immutable prepared body,
exact original write binding and requested current READ revision permit protected
semantic GET under active, disabled or reconcile_only connection mode. Matching
GET proof is persisted with current read revision and actual payload digests;
current canonical Goal/root is checked before finalization. The original job
may succeed and release only its matching reservation through normal existing
reconciliation, with WorkBoard adoption separately preserving every #915 guard.
New consent or fresh root cannot adopt it.

If positive effect truth exists but overall original Goal/authority no longer
permits finalization/adoption, explicit legacy capacity close provides the narrow
alternative: verify the complete original effect against the actual known-ID
protected semantic GET and permanently fence the original job before atomic
exact reservation clear, retaining old Unknown/blocked/task/Goal/approval/finance
history. Subsequent closure-bound GET observations work without old reservation
and cannot adopt, succeed, replay, release again or affect a successor.

Missing remote IDs remain remote_id_required; no unknown-ID listing/search is
added. 404, non-positive/ambiguous/malformed/too-large readback, missing original
prepared bytes or incomplete canonical effect inventory remain visibly blocked
with occupied capacity and named reason. These cases are intentionally outside
finite positive closure support; neither legacy nor publication promises to
release unverifiable uncertainty. This is a stated capability limitation, not
false closure or absence proof. Acceptance includes positive stopped-consent
legacy reconciliation/finalization/release, stale-Goal exact legacy closure,
all missing-ID/non-positive/too-large blockers, restart durable read-revision
adoption, and zero POST/new-root adoption on every route.
