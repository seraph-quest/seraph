---
title: "ADR-012: Finite Durable Read-only Research"
---

# ADR-012: Finite Durable Read-only Research

**Status:** Accepted

**Decision class:** Target architecture

**Date:** 2026-10-03

**Tracked work:** [#901](https://github.com/seraph-quest/seraph/issues/901), under [#899](https://github.com/seraph-quest/seraph/issues/899)

## Accepted design and provenance

The lead accepted the independently reviewed V4 contract in
[owning comment5967234437](https://github.com/seraph-quest/seraph/issues/901#issuecomment-5967234437)
before research runtime implementation. This is an accepted target, not a
claim that research execution, provider readiness or shipped behavior exists.
The design source checkpoint is `d9d886cdee4b682fb1f3dde4b0b9a24ae8794cc8`.

| Reviewed V4 artifact | SHA-256 |
| --- | --- |
| Architecture proposal | `daf51dbc8a0ee103132e33cf9ecdfda079f037e3dc3cb571b023977ea707d2e5` |
| ADR draft | `1dee39d350ed8ffb5ca8c4ca94644e98f2e04edbbc30f8155220813a35cc6bf7` |
| Source/evidence manifest | `2230eae9e090fc5f122c9fbb30cd9dec5fd8534b80b18eb99757e5e25574440c` |

A separate GPT-6 Luna MAX Critic/Contrarian pass returned **DESIGN PASS**;
review report SHA-256
`c37f8e45ca77848f6db69eab33afe867e477bf772944f927c1d5bb98dbdfca4e`.
The accepted findings removed an unnecessary exact native tokenizer gate,
required atomic accounting checks before contact for pre-funded siblings and
ordinary callers, preserved denied reservations through broker settlement,
and clarified reserved allowances versus actual upstream charges. No material
design finding remained. Earlier private versions remain historical evidence;
V4 is the authoritative reviewed contract. The complete normative boundaries
and proof requirements are recorded below without depending on private links.

## Context

Existing legacy delegation and managed specialists run synchronously in memory.
Existing canonical children require a Running parent with its current live lease
and fence. Existing remote inference already has one governed priority lane,
durable contact/cost accounting, deployment/owner continuity and Unknown-cost
recovery. Existing WorkBoard, artifact review and typed source citations supply
the operator and evidence surfaces. None of the inspected seams alone supplies
an authenticated, recoverable research parent that releases its lease while its
fixed children work. The existing same-owner budget is also not a fixed parent
operation allowance. Source references and exact hashes accompany the proposal.


## Decision

Use one finite research parent and at most two same-kind children through
existing WorkBoard, durable jobs, model fabric, accounting, source and artifact
seams. Preserve legacy synchronous delegation and generic child fences.

## Fixed capability and schema

The stable IDs are `work.research-dossier.v1` (parent) and
`work.readonly-research-child.v1` (one child kind). Native server job kinds are
strict internal `research_dossier` and `readonly_research_child`, never user
aliases, wildcard service mappings or executable text.

Parent input: operation ID/idempotency key; existing current WorkBoard owner,
operator session, original LiveRoot and Goal ID/revision; question (UTF-8 <=2 KiB);
one or two perspective instructions (<=1 KiB each); explicit source IDs assigned
to fixed child slots; permissions and exact local artifact digests/public URLs;
fixed limits, route/policy/consent revision and aggregate allowance `C`. The server
canonicalizes and hashes the full immutable admitted input. The model cannot
choose another source, child, tool, route, budget, path, Goal or permission.

Each child input has the immutable parent job ID, **creation** Board attempt ID,
creation Board fence, creation durable-job fence, root/owner/Goal binding, slot
index 0 or 1, original deadline, source manifest digest, source/model-egress
permission revision and its fixed cost subset `b_i`. Each slot has one stable
source-preparation intent and one stable inference operation ID. Each child's
output is strict JSON: perspective, at most eight claims, at most two citations
per claim, uncertainty and at most eight stated contradictions. Unknown fields,
tool calls, executable instructions, arbitrary paths and oversized values reject.
Model claim text is attributed synthesis, even after mechanically valid citations.
Invalid citations remain visibly unverified and cannot become adopted claims.

Parent output is a deterministic UTF-8 `text/plain` dossier and a typed source/
child/adoption manifest, each <=64 KiB. React renders text literally; artifact
responses use `nosniff`. There is no parent model/planner call and no Markdown/HTML
execution. Contradiction sections list child-declared, cited disagreements; exact
same-span conflicting assertions can additionally be grouped deterministically.
The assembler does not claim to detect all semantic contradictions.

## Finite admission envelope

These are accepted hard ceilings, narrowed by current Goal, source, provider and
policy limits. Changing them requires a reviewed superseding architecture decision;
implementation cannot silently expand them.

| Quantity | Bound fixed before admission |
| --- | --- |
| Child kinds / instances / depth | One kind; one or two instances; depth one. |
| Parent deadline | First admission +300 seconds, capped by remaining current Goal grant. |
| Child work | Source preparation then exactly one synthesis; max120 seconds per child including its waiting time, always capped by parent deadline. |
| Parent execution | Three finite phases: create, fund, assemble; at most two source preparations and two synthesis calls, then one dossier assembly. No agent/tool iteration. |
| Sources | At most four distinct selected sources total, at most two per child. Shared sources reuse one exact immutable prepared artifact; no implicit discovery. |
| Public reads | At most four GET intents total, one exact approved URL per source, 20-second transport bound per read; no redirects, retry, subresources or URL selected by prose. |
| Source size | <=64 KiB raw bytes per source; <=256 KiB aggregate. Text/plain UTF-8 only for this capability version. Other MIME, encoding, cookies or larger body blocks visibly. |
| Source excerpts | Explicit approved line ranges; <=4 KiB quoted source material per child, with exact normalized-text digest and span digest. No silent truncation or invented span. |
| Prompt bytes / input tokens | Canonical complete messages <=8 KiB UTF-8, including all system/user/source/perspective/metadata text. No hard native input-token count is claimed; any token estimate is explicitly diagnostic. No tokenizer downloads, counter requests or new service prerequisite. |
| Output tokens / bytes | Requested provider output limit <=min(1,024,current policy) and <=16 KiB strict child JSON; at most2,048 requested output tokens aggregate. Provider response overflow stops and preserves any contacted cost. |
| Remote lane / retries | Existing single priority-aware remote lane. One stable model intent per slot; provider contact <=45 seconds, capped by child/parent deadline and current policy. No post-contact retry, fallback provider or automatic re-synthesis. Exact never-contacted recovery can rebind the same intent; it does not add calls or budget. |
| Source retries | No second network read after a persisted contact marker. Restart adopts exact durable source output, or marks read outcome Unknown/Blocked. |
| Output storage | Existing private owner-bound workspace, bounded nofollow write/promote/readback; source artifacts plus child outputs and final dossier only. |
| Cost | `b_i` is the existing trusted server request reservation bound, never a caller estimate or absolute upstream billing cap. At funding `sum(b_i) <= C` and current owner/deployment/Goal allowance checks hold. Actual provider overrun is retained and blocks further contacts, including already funded siblings. |

The reviewed V4 contract uses prompt bytes. Existing installed cl100k
counting/approximation cannot establish exact Claude or arbitrary-route input
tokens; that claim is unnecessary for a hard byte/output/call/time envelope.
The reviewed route findings document current official OpenRouter sources
checked on 2026-10-03 and an actual unauthenticated metadata GET for the existing real model
candidate `anthropic/claude-sonnet-4` with `max_tokens`-supporting Amazon Bedrock
endpoints. Metadata is not authenticated readiness/privacy/quality proof. Existing
operator-approved route, source-egress, credential, privacy and current-policy
checks still decide readiness; absence of its native tokenizer alone does not
block this byte-based capability. No provider call/download was made for design.
Disable context compression explicitly, omit all tools/search/multimodal payload,
and bind the exact complete message serialization to accounting and final request.
No extra history/memory/prompts may appear after the byte/digest check. Narrow
provider output and timeout at the actual adapter boundary; setup limits can only
narrow, never widen the research envelope.

The existing server reservation B comes from reviewed setup request bound or
deployment ceiling, not exact token-price derivation. Current output parameters
and finite payload/time/calls constrain work; they do not mathematically prevent
an upstream bill above B or C. Record actual provider charge and keep missing
charge Unknown. Preserve current deployment/owner continuity and overrun behavior,
with the shared contact correction below protecting pre-funded work too. Do not
clip actual debt or present reservations as an absolute billing cap.

Version updates, restart, operator retry, waiting and lease reclaim never renew
the first deadline, source-read count, call count, cost subsets or original Root.
Failure of one child does not create a replacement slot. An incomplete dossier may
show verified completed evidence, but unresolved/Unknown children remain labelled
and the parent cannot claim complete research. A new research request is a
distinct reviewed finite operation, not renewal or liability adoption.

## Waiting and immutable lineage

1. Claim the parent through existing WorkBoard and durable-job authority. In one
   canonical transaction create the fixed child intents and immutable checkpoint
   containing creation attempt/fences, slots, bounds, source permissions and
   original Root/Goal. CAS parent revision plus live Board/durable fences. A
   committed duplicate returns the exact same slots; disagreement fails closed.
2. Move the parent durable job to Paused with typed `research_wait_sources` and
   release its lease. Release the Board lease while preserving the same active
   Board attempt (`ended_at` remains null), using a research-only repository CAS.
   Board projects Blocked with a typed waiting reason; UI says Waiting for
   sources, not Failed. This waiting binding prevents ordinary orphan recovery
   from creating another attempt or misclassifying an intentionally absent lease.
   Canonical job/checkpoint is authoritative if projection synchronization was
   interrupted; reconciliation repairs only the exact Board binding.
3. Fixed children prepare sources and exact immutable prompts, record bounded
   actual readback and become Paused `research_prompt_ready`, releasing leases.
   Local permissions and model-egress consent are separate: selecting a local
   artifact does not by itself permit sending it upstream. Parent funding resume
   is eligible only when every selected slot has an exact prompt-ready proof.
4. Reclaim the **same** parent attempt by research-specific CAS; current execution
   fences increment. Creation fences/checkpoint never change. Fund both selected
   model intents atomically as below, then parent waits lease-free again under
   `research_wait_children`. Children queue synthesis through the existing broker.
5. When all selected child outputs are terminal and verified, reclaim that same
   parent attempt for deterministic readback/adoption/assembly. Adoption is a
   distinct exact CAS, requiring a current live parent execution lease and all
   current source/Goal/owner/Root permissions. Original creation lineage is
   compared, never overwritten with the newly reclaimed parent fence.

The research exception is a named typed internal guard only for the exact two
canonical kinds. It permits a child to run while its exact original parent is
Paused in one of the research wait states, or Running in an exact typed research
funding/assembly phase under its separately verified current lease. It also checks real canonical current
parent cancellation/deadline, original creation attempt/token, current owner,
original live Root, Goal ID/revision and source/egress permission revisions. It
never treats an arbitrary Paused parent as permission. All generic parent-fence
logic remains the default. The typed exception must apply consistently to child
claim, heartbeat, checkpoint/artifact writes, cost bind/contact, effect/readback
and terminal CAS; an admission-only exception would fail later boundaries.

No stale runner can fund/adopt/write by citing the creation fence: final parent
adoption additionally requires its fresh current lease and execution fences.
Board/child cancellation and source/Goal revocation freeze unfinished work via a
committed authority transaction; do not mutate then raise out of a rollback-on-
exception session. Terminal transactions recheck authority atomically, preserving
the already reviewed #914 freeze and handoff boundaries.

## Atomic aggregate accounting through the existing ledger

Model financial reservations cannot bind an invented pre-source prompt digest.
Therefore source preparation precedes funding, and neither source child may
contact a model during that phase. Initial parent admission records the fixed
reviewed `C`, trusted maximum slot subsets `b_i`, stable call IDs and policy/route
revision. This is a bounded allowance declaration, **not** a claim that money has
already been reserved. The UI distinguishes preparing sources from funded work.

Once exact prompts exist, a research-only repository operation uses the existing
accounting write transaction and continuity witness. It locks/rechecks canonical
parent creation/current execution and both prompt-ready children. It validates
current trusted per-call reservation `B_i <= b_i`, `sum(B_i) <= C`, current owner/deployment/
period/ceiling/consent/policy revision and exact prompt digests. It creates **both
canonical `InferenceCostReservation` rows or neither**, with the existing held
`reserved` state, each under its actual child job ID and stable operation ID. There
is no charged parent call, no duplicate parent financial reservation and no
second ledger/queue. The existing deployment and owner sums include these exact
two held rows immediately. One-child requests follow the same single-slot method.

The specialized funding method may create a reservation for an exact Paused
prompt-ready research child while the funding parent has its current live lease.
This exception is not added to generic `reserve_inference_cost`, which retains its
Running-child lease requirement. Store the fixed research parent/group, slot,
creation lineage and prompt-ready fence as typed accounting evidence/bindings
beside the canonical cost row. At child's subsequent claim, a research-only
never-contacted rebind CAS checks every immutable reservation field and creation
lineage and binds the same row to the child's **current** live lease/fence. Contact
still uses existing current job lease, exact payload/policy digest and durable
pre-contact marker. The broker does not grant this exception by request metadata.

Accounting replay of the funding transaction is all-or-nothing: both rows match
all fixed fields and the existing committed witness, or Blocked; a partially
matching row is never topped up or silently funded separately. Existing account
continuity semantics apply to this multi-row transaction; persist one matching
transaction witness for the complete row set and read it back before releasing
children. Fail/ambiguous commit blocks contact until exact recovery proves the
complete group. There is no independent unlocked group counter.

Reserved/contacted/Unknown amounts remain held exactly as current accounting
requires. A denial due to current unreviewed overrun additionally retains the
pre-funded row through normal broker failure cleanup as specified below. Known settled charges and Unknown upper bounds count against the
original operation allowance, even after parent cancellation, source revocation,
restart or deadline. Releasing a provably never-contacted slot does not authorize
a third call or expand the other slot. No automatic reallocation, period reset,
policy revision adoption, cheaper caller estimate or budget-renewal retry. If
current trusted `B_i` exceeds its fixed slot or the policy changed, block and ask
for a distinct finite reviewed operation; do not transplant old cost liability.

This accepted target requires strict two-row funding of real paused prompt-ready
children plus exact never-contacted current-fence rebind. Design review accepts
the boundary; implementation and actual runtime proof remain required.


## Shared atomic contact guard and durable held denial

At the design source checkpoint, the independent critic identified a seam gap: canonical
`workflows/inference_accounting.py:415-446` checks continuity, reservation/policy,
current job lease, Goal/principal and deadline, then marks `contact_started`, but
does not check current unreviewed overrun. The first child can settle above B
after the sibling was already funded. Checking only new reservation/snapshot
does not protect that existing row. Fix the **shared** contact seam for ordinary
canonical callers too; this is not a research-only unlocked snapshot check.

Within the same existing `_accounting_begin` SQLite `BEGIN IMMEDIATE` transaction,
load fresh canonical account/all rows and hold the existing continuity lock.
After continuity/reservation/policy/current-lease/owner/Root/Goal/deadline checks,
recompute current authorized accounting period, settings/ceiling continuity and
`unreviewed_overruns` from that transaction's actual rows/witness. Refuse contact
when current canonical accounting is blocked. In particular, any settled actual
charge>B not covered by the existing exact reviewed-overrun entries denies a
pre-funded sibling **before** `contact_started` and before provider callback.
Preserve the existing correct canonical checks; do not introduce another ledger,
nested transaction, unlocked precheck or snapshot cache. The existing serial
broker remains the lane owner; waiting/funded parents hold no lane.

On an overrun denial, keep the sibling cost row in existing `reserved` state,
with unchanged operation ID/payload/policy/B/slot/deadline and no contact time.
Append typed canonical `provider_contact_denied` reason/binding evidence and an
operator-visible accounting-block reason, then persist the matching accounting
witness **inside that same transaction**. Commit the denied outcome normally
before signalling an error to the runner. Mutating then raising out of the
rollback-on-exception session would erase it. Use a committed typed denied result
or raise only after normal context exit; no commit inside unrelated terminal work.
The actual first overrun charge remains unchanged and never clipped to B/C.

The full caller path must honor that durable state. At the design source checkpoint,
`model_fabric/accounting.py:183-186,228-256`, execute/finally calls
`_finish_accounting` with default `blocked_before_contact` whenever the transient
handle says not contacted. Current `settle_inference_cost` at:483-488 releases a
reserved row for that reason. Thus simply throwing the new overrun error would
incorrectly release the hold. Shared finish/settlement must consult the canonical
held-denial evidence under the settlement transaction, rather than trusting
`handle.contacted=False`, a caller reason or ephemeral flag. Ordinary automatic
failure cleanup returns/preserves the held row and projects Blocked; it cannot
erase contact denial, create capacity for further work or mask the original
exception. This applies consistently to async, sync and stream broker paths.
No arbitrary caller can clear an accounting block by supplying another reason.

Existing explicit proven never-contacted cancellation/expiry can release only
after its authorized current cancellation/deadline and worker quiescence prove
no future callback/contact. Such release does not forgive the first child's
actual overrun or expand another slot. Existing reviewed accounting recovery is
not bypassed: cleared deployment overrun alone does not automatically restart an
expired/cancelled/revoked research operation, renew its allowance or replay a call.
Exact still-current pre-contact recovery must satisfy every original authority,
digest, deadline, call-slot and cost field. Unknown contacted work remains held.

Atomicity means competing settle/contact writes serialize through the same
existing canonical accounting writer and continuity witness. If the overrun
settlement commits first, subsequent sibling contact must observe it and deny.
If a contact transaction legitimately commits first, later settlement cannot
retroactively retract a contact already recorded; preserve its original bound/
liability and explain this ordering in the receipt. Do not claim instantaneous
upstream cancellation or a race-free billing guarantee. Normal serial broker
sequence must settle the first call before invoking the second contact path.

Required actual negative: reserve both calls, settle first actual>B, execute the
second through the real broker callback/finally, and prove provider callback
count remains zero, contact timestamp null, row still reserved, durable denial
and Blocked visible. Reopen file SQLite and independent witness readback to prove
the hold/charge/reason persist. Add barrier-driven settle-before-contact and
contact-first ordering tests, failed-witness/commit denial, and the equivalent
ordinary nonresearch caller. A token diagnostic failure or optimistic caller
estimate never bypasses these canonical financial/contact fences.

## Source, output, restart and cancellation trust boundaries

Local input is an explicitly selected existing owner-bound immutable artifact
with independent digest/readback and current source permission, not an arbitrary
filesystem path, inbox, memory search or private repository crawl. Public input
is an exact approved HTTPS text URL read through the existing pinned transport;
no discovery, redirects, JS, authenticated browser, cookies, proxy environment,
downloads or access to loopback/private/link-local addresses. Record source
permission, URL/version, transport route, raw SHA, normalized-text SHA, selected
line ranges and exact span SHA. Permission checks occur before read, egress and
adoption. Text normalization rules are versioned and immutable.

All source and model output is untrusted quoted structured data. Synthesis has
no tools, subprocesses, filesystem writes, network clients, credentials, nested
agents, dynamic registries or memory-write authority. Only the server's fixed
read adapter and governed model-fabric call perform their declared operations.
Prompts visibly delimit source data; actual malicious source/output tests must
prove that claimed permissions, tool requests, new URLs and instructions cannot
change the admitted manifests. Prompting alone is not a security boundary.

Persist a finite output reservation before any physical artifact write; bind its
key to operation/slot/creation lineage/current child attempt. Write to the existing
private workspace using descriptor-relative nofollow bounded write, atomic
promotion and actual SHA/readback before canonical artifact/readback CAS. Restart
can adopt a previously written file only if it matches the exact reservation,
schema, owner, permissions and digest; an unrelated same-named file is Blocked.
An actual provider result persisted before child/parent adoption can be verified
and adopted without another model request. A contacted request without exact
verified durable output is Unknown; provider contact cannot be retried blindly.

Parent cancellation atomically marks the parent and all unfinished fixed children
for cancellation, prevents funding/contact/adoption, releases only proven unused
pre-contact reservations and preserves completed evidence and contacted Unknown
cost. Running provider contact may not be physically retractable; late responses
are retained as non-adopted audit/output evidence with original liability and
cannot resurrect the parent. Do not show Cancelled until actual source/model
worker quiescence or show a clear unresolved/Unknown cleanup state. Priorities
remain server-owned; waiting parents hold no model lane. Exact deadline checks
also occur at output promotion/terminal/adoption so late results cannot complete
an expired operation.


## Operator interface

Extend the existing WorkBoard inspector/proposal and Goal/source selector, not a
new broad agent screen. Show selected perspectives, source/egress grants, fixed
deadline, aggregate funded/held/settled/Unknown cost, per-slot source and call
state, cancellation/blocked reason, and exact original-operation recovery.
Plain-text dossier and cited spans are inspectable through existing artifacts.
Continue chat/settings and other cockpit reads while research waits/runs.
Uncertain submission persists and reads back the exact bounded request in
owner/session-scoped storage before POST; reload retries the same idempotency key,
never creates another operation or moves Root. Preserve existing #915 review
proofs and #912 process cleanup. Reuse the reviewed #914 exact pending request pattern now integrated at the
canonical d9 design base; preserve its fixed capability/CPU proof boundaries.


## Alternatives

- Run existing general web_researcher/managed agents as durable children: broad
  tools, model-selected source authority and RAM ownership violate this scope.
- Keep a live parent lease throughout child work: holds capacity and makes restart
  ownership brittle instead of persisting an explicit waiting contract.
- Relax generic paused-parent fences: expands routine/service authority beyond
  this fixed research kind.
- Independently give each child the full owner/parent cap: does not establish an
  aggregate subset or atomic funding.
- Reserve guessed prompt digests before source preparation: breaks canonical
  accounting identity. Fund actual immutable prompts together instead.
- Another model planner/parent synthesis, executor, registry or inference lane:
  adds unneeded authority/cost and undermines existing priority/accounting.
- Treat deterministic fixture or valid citation as quality/completion: proof
  receipts validate mechanics only; semantic quality remains unverified.


## Consequences

The focused additions are a typed waiting/recovery guard, atomic group funding
plus never-contacted rebind inside existing accounting, deterministic research
source/output adapters and existing WorkBoard UI projection. The generic job and
financial fences remain the default. These specialized seams require negative
tests at every authority boundary and actual restart proof, not code inspection.
Text/plain/no-discovery and strict byte/output/call/time envelope deliberately
limit the reviewed V4 contract; exact native tokenizer availability is not a prerequisite.
An unavailable dependency is Blocked and visible, not configured-as-success.


## Verification

The minimum real journey is authenticated
WorkBoard parent + two same-kind children + file-backed SQLite + actual local
and public source reads + deterministic artifact readback + managed UI continued
use. Only the provider boundary is intercepted and labelled; product authority,
parent/child execution, cost reservations, persistence and files remain real.

| Group | Required positive and negative observations |
| --- | --- |
| Admission/lineage | One/two slots only, depth1, exact duplicate admission, malformed/extra kind/third/nested child rejected; generic paused-parent children still rejected; real waiting releases both leases and preserves exact creation attempt. |
| Aggregate funding/contact | Barrier-driven all-or-neither paired funding; exact rows, deployment+owner+operation reservation subsets/witness; insufficient budget/policy drift/false estimate block; failed/ambiguous commit no contact. First child settles actual>B, then already funded sibling real broker callback/finally denied atomically with reserved hold, null contact, durable denial/Blocked after file-SQLite reopen. Settle/contact race barriers and ordinary nonresearch caller protected; actual debt never clipped; explicit proven cancel/expiry alone releases unused hold. No parent call. |
| Serial/fairness | Actual broker admits at most1 contact; interactive priority wins after current call; bounded background fairness follows existing broker; waiting parent does not occupy lane; queued deadline expiry and cancellation release only never-contacted amounts. |
| Recovery | Separate runtime restart while source-ready, model queued, contact-started, physical output written before DB readback, and child verified before parent adoption; same operation/deadline/calls/allowance; exact output adoption once; Unknown contact no second provider call. |
| Authority/readback | Actual foreign owner/session/Root/Goal/source revision, wrong creation or current fence, late/duplicate output, physical digest/schema/symlink/path/oversize mismatch rejected; current permission revocation freezes unfinished canonical rows in a committed fresh session. |
| Trust/output | Actual injection/tool/URL/escalation cannot affect authority; separate source egress consent, invalid citations labelled, literal UI, contradictions/uncertainty/no_learning. Full-message multibyte/escaping byte overflow, extra history/tools/compression, output/setup narrowing and adapter timeout-widening negatives. Native token estimates diagnostic; actual cost missing/overrun preserves debt and blocks contacts. |
| Managed journey | Existing manage.sh dev local run on isolated owned free ports/workspace/blank keys; actual login/Origin guards, submit+cancel+restart/recovery, exact dossier/source/model receipt DB/file/UI IDs and digests, continued cockpit read/navigation. Provider interception never passed off as paid/live quality proof. |

No Done/Shipped claim until relevant actual proof, merged final-source managed
readback and independent cumulative Luna MAX review pass with material findings
resolved or explicitly root-dispositioned. No #771 benchmark endpoint or language
framework comparison substitutes for this journey. This ADR records accepted architecture. Current guide/STATUS must describe
actual behavior and missing boundaries until the whole vertical slice is verified.


## Current-source limits

Official OpenRouter sources checked on 2026-10-03 are
[output parameters](https://openrouter.ai/docs/api_reference/parameters),
[provider routing](https://openrouter.ai/docs/guides/routing/provider-selection),
[usage accounting](https://openrouter.ai/docs/cookbook/administration/usage-accounting),
[message transforms](https://openrouter.ai/docs/guides/features/message-transforms)
and [Claude Sonnet4 endpoint metadata](https://openrouter.ai/api/v1/models/anthropic/claude-sonnet-4/endpoints).
The reviewed public metadata GET is a route candidate receipt, not credential,
consent, privacy eligibility, model-quality or actual paid generation proof.
Current trusted per-call bounds are reviewed policy, not a mathematical total
bill guarantee. Exact input-token estimates are not claimed. macOS and Linux
remain peer core-host targets; no optional OS service is a core prerequisite.
