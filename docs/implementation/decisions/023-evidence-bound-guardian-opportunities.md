---
id: evidence-bound-guardian-opportunities
title: "ADR-023: Evidence-bound guardian opportunities and follow-through"
---

# ADR-023: Evidence-bound guardian opportunities and follow-through

**Status:** Target proposed for independent review under [#954](https://github.com/seraph-quest/seraph/issues/954). No implementation or Shipped claim.

**Decision class:** Additive guardian contract over the existing four layers.

## Context and boundaries

The inspected develop revision `0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a`
already has source watches, goals, intervention policy, an actionable inbox,
reviewed Work proposals, finite research, public evidence pipelines, durable
native jobs, signed memory and reviewed procedures. Their existence is not
evidence that source changes are semantically relevant or interventions useful.
`source_watch.material_change` uses lexical thresholds; `triage.create_proposal`
is operator-invoked and requires previously materialized typed inputs.

Extend these owners. Keep FastAPI/Pydantic, SQLModel/SQLite, existing private
artifacts and vault, Work proposals, native durable runtime, APScheduler and
the single governed inference broker. No new agent framework, graph database,
vector store, workflow engine, queue service, dashboard or external memory.
ADRs 003/006/008/010/012/014/015 remain controlling. This adds a separate
opportunity schema; it does not expand research children, procedure grammar,
private-artifact egress, effect approvals or the three-consumer evidence set.

## Shared authority and evidence contract

The [strategy claim ledger](/research/strategy-claim-ledger) governs
security, privacy and comparative wording; this ADR accepts bounded behavior,
not a claim of measured benefit or superiority.

One current authenticated original Root owns each finite grant and operation.
Stable operator data ownership is not execution authority after new login.
Every new admission, precontact, proposal acceptance, source use and result
adoption checks Root, exact Goal/revision, guardian_policy_revision and the
policy's bound goal_revision, watch/source version and purpose
consent, route/policy epoch, grant expiry, deadline and existing evidence tokens.
Corrections before a use boundary block it; already contacted work retains
liability and cannot be undone by a SQL transaction. Physical proof is staged
outside short pure database writers; no filesystem, vault or network I/O inside
`BEGIN IMMEDIATE`. Reuse native leases/fences and source permissions.

Model output and source prose are data. Neither can grant a tool, create a URL,
pick a credential, change a budget/priority, remove a verifier, expand scope or
write memory. Exact cited spans establish provenance only, not semantic truth.
Use literal plain-text rendering and mark assessments as model judgments.

## M2: Fresh goals and semantic opportunities

### Canonical data

Add nullable `Goal.guardian_policy_json` (closed schema below) and
`Goal.guardian_policy_revision:int=0`. `Goal.revision` remains the execution
revision; policy edits increment only `guardian_policy_revision`. Policy
`goal_revision` binds the current Goal revision. Ordinary Goal edits invalidate
the policy; the operator first uses existing watch review/rebind, then explicitly
saves a new policy after selected watches already bind that current Goal. No
automatic watch or grant renewal. Do not repurpose `updated_at` as consent.
Old rows remain NULL and retain existing manual/source-watch behavior. New
semantic assessment is disabled until a deliberate authenticated save.

`seraph.guardian.policy.v1` contains `assessment_enabled:bool`,
`auto_stage_plan:bool=false`, `confirmed_at:UTC`, `review_due_at:UTC`,
`grant_id`, `original_root_id`, `goal_revision:int`, `source_watch_ids:list[UUID]` (1–3),
`max_assessments_per_utc_day:int` (1–4),
`max_plan_proposals_per_utc_day:int` (0–2),
`max_notification_per_utc_day:int` (0–2, default0),
`minimum_gap_seconds:int=1800`, `schema_version`.
All integers are strict, booleans literal, unknown fields forbidden. Server
sets confirmed_at; review_due_at is at most seven days and no later than the
current reviewed Goal budget and Root absolute expiry. Budget-less or expired
authority cannot enable the policy. `auto_stage_plan=true` requires a separate
unchecked acknowledgment and nonzero proposal cap; no execution follows.
Save/renewal starts a new policy revision and never renews old operations,
notifications, grants, inference holds or effect authority.

Add one focused table `GuardianOpportunity` (`guardian_opportunities`) owned by
guardian assessment, with:

| Field | Contract |
| --- | --- |
| `id`, `owner_principal_id`, `original_root_id`, `goal_id` | UUID/verified canonical identities; no caller-selected owner |
| `goal_revision`, `policy_revision`, `watch_id`, `watch_revision` | strict positive integer versions and canonical watch identity |
| `source_packet_id`, `source_digest`, `source_token_json` | exact existing verified packet plus versioned canonical evidence token, ≤16 KiB; no raw source copy |
| `dedupe_key` | SHA256 over owner/Goal/revision/watch/source-packet semantic digest/policy schema; unique with owner |
| `status`, `revision`, `created_at`, `expires_at`, `assessment_deadline_at` | state below, CAS revision; assessment_deadline_at=min(admission+120s, Root/Goal/policy/source expiry); opportunity expires_at=min(source packet/Root/Goal/policy expiry); separate plan review deadline=min(stage+5min, opportunity expiry, all current bindings); historical results retained |
| `job_id`, `proposal_id`, `intervention_id`, `result_artifact_id` | nullable links to existing owners, never alternative execution state |
| `reason_code`, `assessment_json` | bounded safe code; validated result metadata ≤16 KiB, private source references only |

Index `(owner_principal_id,goal_id,created_at,id)` and
`(status,created_at,id)`. Unique dedupe must serialize simultaneous scheduler
ticks and API calls. Additive migration in `backend/src/db/engine.py` runs before create_all;
use explicit indexes for populated databases and test rerun/old rows.

States: `queued -> assessing -> proposed|silent|blocked|unknown`;
`proposed -> planned|dismissed|expired|blocked`; `planned` links the existing
proposal/tasks, whose execution remains authoritative. `cancel_requested` is
projected while a worker is active; final `cancelled` needs native quiescence.
No reset from unknown to queued after provider contact. Blocking does not erase
the observed source or contacted cost. A proven never-contacted failure may
resume the same job only inside original limits, max attempts2.

### Assessment and events

At the existing successful source-watch packet publication seam, enqueue only
a new verified PUBLIC HTTPS source packet for an explicitly selected watch.
Workspace/private/mail/calendar/selected-text inputs are excluded in v1;
existing watch behavior is unchanged. Require the existing material-change
filter to pass. This intentionally may miss small or filtered meaningful changes;
no claim of comprehensive monitoring. Do not backfill old packets on enabling.

Internal `guardian.source_packet_verified.v1` is a typed call into the existing
scheduler/DB admission, not a message bus. Payload is only packet ID/watch
revision/Goal revision. Durable unique row precedes scheduling; ordinary ticks
recover queued rows by indexed keyset pages≤20. No synchronous model call inside
watch completion. Debounce to at most one assessment per Goal per15 minutes;
retain at most one pending latest packet per watch, explicitly record replaced
uncontacted candidate as `silent/coalesced`; never erase contacted work.

The new native capability `guardian.opportunity-assess.v1` has one model call
through existing strategist route, text+structured-output, no tools, no stream.
One/two public source excerpts, aggregate≤4 KiB, full prompt≤12 KiB UTF8,
output≤1024 requested tokens and16 KiB parsed JSON; no extra history or implicit
memory. At most45s provider time and120s queued+execution time within the native
original300s admission maximum; the separate assessment deadline is tighter.
Do not reuse assessment_deadline_at as opportunity review expiry. One remote request identity and reservation,
no contacted retry; priority is existing background/normal, never interactive.
At most one pending assessment per Goal, two per owner, sixteen host-wide.
Enforce counts and UTC daily consumed call slots in the opportunity admission
transaction, counting unknown/contacted attempts; new policy revisions do not
reset daily counts. Existing deployment/owner cost reservations still apply.

Durable evidence is a NEW bounded private immutable `seraph.opportunity.evidence.v1` snapshot staged from `SourceObservation.after_excerpt`, already redacted by `_safe_excerpt`. Select first two material public observations sorted by `source_key`; normalized LF whole lines1–200 fit aggregate4KiB, never split a line/codepoint. Snapshot contains packet ID/checkpoint SHA/watch revision/Goal revision and per-source source_key/identity_digest/target URL binding/new_hash/redacted excerpt+SHA256. Never persist baseline_text, raw source or changed_text. Stage via existing private-artifact ownership at packet creation after policy and workspace-write authority, before approval completion; nullable packet snapshot artifact ID/digest enter the exact local-write approval scope. Old/recovered packets missing snapshots block only assessment; no refetch, reconstruction or watch failure. Opportunity source_token binds exact immutable artifact readback plus packet. citations.source_id is source_key; line numbers refer to normalized redacted evidence lines, not original web lines. Span hashes cover exact offered LF bytes. Empty fit blocks source_excerpt_unavailable; later material may be missed.

Closed result:

```json
{"schema_version":"seraph.opportunity.assessment.v1","relevance":3,"confidence":"medium","summary":"The API removal affects your migration goal.","reason":"A cited release note removes the selected endpoint.","citations":[{"source_id":"server-offered-id","start_line":12,"end_line":14,"span_sha256":"server-computed-sha256"}],"suggested_blueprint":"public-evidence-report","abstain_reason":null}
```

`relevance` strict0..4; `confidence=low|medium|high`; summary≤240 chars,
reason≤1000; citations1..4, exact offered ranges1..200 and span digest; allowed
blueprints `public-evidence-report|public-browser-check|none`; abstain reason
null or ≤240 chars. Server derives all IDs, ranking and authority. Invalid
JSON/extra fields/missing or mismatched citations blocks, with no second call.
Valid relevance < 3, low confidence, none, or abstention stays silent with visible
history; it does not create an action. Model score is not calibrated probability.

M2 adds nullable GuardianIntervention owner_principal_id/original_root_id/goal_id/goal_revision/opportunity_id lineage and opportunity type/delivery not_requested.
On assessing→proposed, atomically create exactly one linked GuardianIntervention
with type `opportunity`, including Inbox-only proposals; no notification means
delivery_status=`not_requested`. Existing legacy learning weights exclude that
type. Delivery updates the same row and never implies a vote. Silent/blocked
assessments have no feedback row; feedback rejects with409
`opportunity_not_proposed`.

Publish a proposed opportunity through the existing GuardianInbox projection
using `source_kind=guardian_opportunity`, preserving accept/snooze/dismiss and
current-source checks. Do not copy source text into public Work events.
Stale Goal shows `goal_review_required` in the existing Goal/Inbox surfaces;
no background inference or repeated pushes until reviewed. Missing source,
credential, budget or proof shows bounded blocked reason and recovery.

### Act, ask, stay quiet

Autonomous authority covers only the already consented public watch read,
bounded assessment and (if separately opted in) advisory proposal staging.
Default delivery is Inbox only, no external notification. The user explicitly
accepts an offered plan; each native capability retains its own approval gate.
No calendar/mail/GitHub/browser mutation, code execution, private egress,
memory adoption or grant renewal follows from relevance.

Native push, if explicitly enabled, passes existing intervention/quiet-hours
policy AND stricter new policy: at most2/owner/UTC day,1/Goal/UTC day and30min
minimum owner gap. A model cannot label urgency to bypass these caps. Count
durable delivery intents, including Unknown deliveries, atomically before
handoff. Snooze suppresses until its existing deadline; dismiss suppresses the
same dedupe key forever. No notification retry on ambiguous delivery. Expired
goals produce one passive review-needed state rather than repeated reminders.
No-action/source failure is an outcome, not a reason for a model retry loop.

## M3: Review an executable plan for an opportunity

Reuse `WorkBoardProposal`, `create_proposal`, `accept_proposal`, the existing
typed inputs, native registry, pipeline materialization, links and dispatcher.
Keep the existing pipeline proposal kind unchanged; only the single-browser proposal adds kind `opportunity_plan`. Add nullable `opportunity_id` plus
`opportunity_revision` to proposal rows. Add a unique index for one active
proposal per opportunity via the canonical opportunity CAS, not a global graph.
Do not weaken existing Specify/Decompose or native authority.

First version offers two exact server blueprints, not arbitrary executable
model graphs:

1. `public-browser-check`: existing `browser.public-task.v1` on ONE already
   approved source URL and its existing bounded navigate/extract input.
2. `public-evidence-report`: existing ADR-010 browser→CPU evidence dossier→CPU
   local report, unchanged three steps/deadlines/attempts/readback semantics.

Select lexicographically smallest cited source_key valid in the immutable snapshot. Derive exactly one URL from the matching SourceSpec target with exact identity_digest and current watch. Require public HTTPS without query, fragment or userinfo (reject if present), current browser allowlist membership and existing browser permission; never expand an allowlist. The planned fixed server input factory emits existing BrowserTaskInput (backend/src/browser/task_runner.py): schema_version=1, start_url=URL, allowed_hosts=[parsed host], approved_url_prefixes=[URL]; actions=[navigate URL, extract selector="body" max_chars=8192], both with expected_checks url_host=host and url_path_prefix=exact path; final_expected_checks are the same checks. No semantic text-match promise: actual browser readback proves the returned current page, which may have changed since the watch. Report uses unchanged existing pipeline preview/accept, and defers CPU input materialization until verified producer output; no upfront fake CPU inputs.

No new source discovery, parameters, runtime commands, external writes or
dynamic code. If the source cannot satisfy the existing browser policy, offer
no executable plan. The model may select one server-offered blueprint and
write bounded title/explanation; the server materializes inputs and IDs from
current source/Goal state, never from model paths or URLs. An unsupported
desired action stays a clearly stated unmet need, not a fabricated capability.

Plan staging uses at most one additional strategist call (same45s/12KiB prompt/
1024-token/16KiB output bounds), one consumed daily proposal slot, and current
public-source egress. Its closed `seraph.opportunity.plan.v1` result contains
`schema_version`, `blueprint_id` from the server-offered enum, `title` of
1–160 characters, `reason` of 1–1000 characters, and 1–4 `citations` using
the same exact offered source/span references as the assessment schema.
Reject unknown keys and a total UTF8 JSON size above16KiB. No second
corrective call. It reuses existing durable
proposal admission/recovery,5-minute review TTL capped by Root/Goal/policy,
and Unknown contact accounting. `auto_stage_plan=false` requires explicit
Generate plan; true stages silently in Inbox and never accepts it.

Stage the existing Triage source card and proposal together by opportunity CAS
so duplicates cannot create orphan tasks. The card is non-executable Triage.
The explicit button reads **Accept and queue this read-only plan** and shows
exact Goal/source, three or one steps, inputs/outputs, permissions, runtime
limits and required native approvals. This acceptance authorizes the existing
dispatch path: use the existing accept transaction to create only the selected
blueprint's Todo tasks and dependency links,
bind existing ADR-014 evidence to eligible three consumers, then transition the
opportunity to planned. Current Goal/source corrections invalidate the preview.
The existing dispatcher may promote accepted Todo tasks and execute them under
fresh authority checks and each capability's native approval gate. There is no
invented separate Ready review. Staging never accepts or dispatches a plan;
the explicit acceptance is the queue authorization. It grants no private egress
or broader effect authority.

`GuardianOpportunity` links result lineage for display only. Success means exact
native producer readback plus dependent report artifact verified by the existing
owners. Task completion without artifact proof remains incomplete. Show same
Inbox→Work card→literal report→explicit no_learning journey and preserve failed,
blocked, cancelled and Unknown status without promoting model assertions.

## M4: Review usefulness and reduce unwanted interventions

Extend `GuardianIntervention` and existing `backend/src/guardian/feedback.py`,
`backend/src/guardian/learning_evidence.py`, `backend/src/memory/m5.py` and memory review UI. No second generic
feedback store. M2 owns nullable intervention lineage `owner_principal_id`, `original_root_id`,
`goal_id`, `goal_revision`, `opportunity_id`, opportunity type and delivery_status=not_requested.
M4 adds only `outcome_binding_json`, `feedback_revision:int=0` and `feedback_history_json` (≤32 KiB, ≤100 entries).
Legacy intervention rows remain unchanged and cannot enter this new population.
Current helpful/not_helpful fields are projections of the new append-only
history for these rows, with exact idempotency and revision CAS. Never overwrite
the historical event to correct it. Population scope is exact owner/Goal/revision.

Separate `delivered`, `acknowledged`, accepted plan, verified output, actual
Goal progress and explicit `helpful|not_helpful` feedback. Existing delivery
weights .7 and acknowledgment .85 do not count as usefulness in this schema.
A user can judge an unwanted notification not_helpful without executing it.
Helpful for plan selection requires a completed matching plan with current
verified native outcome. Nonresponse supplies no vote. A changed attempt/source/
Goal, deleted evidence or unresolved result removes eligibility, retaining history.

Provider-free recommendation capability
`memory.opportunity-preference.v1` reads the complete latest30-day exact scoped
population containing only opportunities with an explicit current feedback tip
(eligible verified helpful or explicit not_helpful), exact owner/Goal/revision,
and feedback timestamp within30days. Unjudged opportunities never enter it.
Cap at100 opportunities/100 feedback events (query101 detects overflow and
blocks); snapshot cutoff is generation time. New unjudged prospective work does
not invalidate a preference. New/changed eligible feedback, feedback aging out
of30days or deleted/stale source/outcome does. Re-enumerate eligible feedback
IDs/tips before every use. It produces only `prefer_blueprint` for one of the two
blueprints when at least2 distinct verified Helpful outcomes and zero current
not_helpful exist for that blueprint; otherwise no_learning. It may separately
recommend `suppress_watch` after2 explicit not_helpful judgments for that exact
watch and no Helpful. If both rules conflict or population is incomplete, abstain.
No inference, automatic generalization, inferred personality or trained weights.

Use existing MemoryProposal review/adoption/history/rollback with a separate
signed scope `guardian_opportunity_preference.v1`. Bind exact population IDs,
feedback tips, Goal/watch versions, blueprint, native readbacks and source tokens;
re-enumerate before preview, adoption and later use. Adoption requires a separate
literal acknowledgment; success/delivery never adopts it. At most one active
preference of each action per scope, conflicting choices block. Preview expires
in5min and original Root bound. One CPU job,30s,1attempt; private JSON≤64KiB.

Preference only orders eligible server blueprint offers for display (accepted
preferred blueprint first, then stable `blueprint_id`) or suppresses optional
watch opportunities. It cannot override source/capability validation or the
advisory model recommendation to create tasks. It cannot increase urgency,
cadence, budgets or permissions,
execute a task, or suppress security/recovery notices. New matching feedback or
changed population invalidates the preference until a new review. Rollback restores
ordinary suggestions, retains history/tombstones and does not restore grants.
ADR-015's manual public-browser-check procedure population remains unchanged.
This different population is visibly labelled opportunity feedback, not measured
quality improvement. Export/delete use existing canonical-memory ownership;
deleted source text is never reconstructed from the history.

## APIs, events, errors and compatibility

Extend existing Goal, Inbox, Work and memory APIs; these are proposed additions:

| Route | Strict request | Response/effect |
| --- | --- | --- |
| `PUT /api/goals/{goal_id}/guardian-policy` | expected Goal/policy revisions, policy, UUID idempotency, literal assessment and optional plan/notification acknowledgments | new exact revisions; no dispatch |
| `GET /api/guardian/opportunities` | owner-derived Goal filter; opaque cursor, limit≤20 | bounded metadata/current availability only |
| `POST /api/guardian/opportunities/{id}/cancel` | expected opportunity revision, UUID | cancel_requested until native quiescence, then cancelled; no manual recover |
| `POST /api/guardian/opportunities/{id}/plan` | expected opportunity/Goal revisions, UUID | canonical proposal reference or blocked reason; no acceptance |
| Existing Inbox disposition and Work proposal accept | exact opportunity/source revision extensions | existing durable events and proposal/task IDs |
| `POST /api/guardian/opportunities/{id}/feedback` | expected feedback revision, helpful/not_helpful, reason≤500 chars, UUID | append history, exact receipt; no learning adoption |
| `POST /api/guardian/opportunities/{id}/recommendation` | expected opportunity/feedback revisions, UUID | existing MemoryProposal ID or no_learning |

Use existing authenticated origin/CSRF/owner conventions. HTTP409 for stale
revision/idempotency conflict/expired authority,422 for schema/limits,403 for
foreign/denied source or missing consent,503 for unavailable canonical store.
Stable reason codes include `goal_review_required`, `source_stale`,
`source_excerpt_unavailable`, `opportunity_not_proposed`,
`assessment_budget_exhausted`, `inference_unverified`, `outcome_unknown`,
`opportunity_duplicate`, `proposal_stale`, `feedback_outcome_stale`,
`learning_population_incomplete`. No raw credentials, source bytes or traces
in generic errors. Existing events carry IDs/digests/reasons, not private text.

`POST /api/guardian/opportunities/{id}/cancel` accepts expected_opportunity_revision and UUID idempotency. Authenticate owner/current original Root, CAS opportunity intent and use existing native DurableJobRepository.record_checkpoint for cancel_requested; worker cancellation/revocation proceeds through the existing execution owner. Return cancel_requested until actual quiescence, then DurableJobRepository.transition_job(..., "cancelled", cancellation_authority_check=...) settles cancelled. There is no manual recover endpoint. Ticks may resume proven never-contacted same jobs max2 within original deadlines only. Contacted Unknown cannot resume; operator reviews existing cost-liability surface. Missing bindings stay blocked and require a new future source packet after explicit policy review; never reset old opportunities.

## Recovery, migration, rollout and rollback

Additive schema only; new goal policy off, new feedback lineage nullable, no
historical data adoption/backfill. Put explicit pre-create_all ALTER/index
steps in backend/src/db/engine.py and test a populated previous schema plus repeated startup.
Accepted native jobs remain their own execution truth; no mirroring job states
as guardian authority. Persist admission/output reservations before I/O, verify
physical artifacts, then CAS adoption. Restart consumes the same request/key,
original deadline and daily/cost allowance. Contacted missing output is Unknown;
verified persisted output can be adopted once under still-current authority.
Cancel revokes future contact/use, waits for actual native quiescence, retains
late output as unadopted history and never refunds unknown cost.

Rollout M2 off by default per Goal, M3 auto-stage off, M4 suggestion-only until
explicit adoption. Disable policy first on rollback, cancel/quiesce pending work
through existing controls, preserve known/Unknown effects, feedback and memory
tombstones. Revert application code only after additive columns are safe for
old readers; retain DB data and signed history. No reset/destructive migration.

## Verification

Each milestone must produce the useful local journey, actual file-backed SQLite,
real artifacts/readbacks and managed UI evidence. Intercept only provider/source
transport where named. Prove current and expired goals, exact source citations,
duplicate concurrent ticks, pending cap/day rollover, quiet/snoozed/dismissed
states, prompt injection, invalid model paths, source correction before contact/
acceptance, cancelled/Unknown restart and one active inference callback.
M3 executes the real accepted native browser/report path rather than seeded
successful rows. M4 changes a later suggestion after separately reviewed
feedback and then reverses it; two delivered messages alone must not qualify.
Negative foreign-owner/Root, stale proof, budget, capability and deleted-source
cases fail closed. Relevant existing tests must pass; known baseline failures
are investigated in their owning milestone, never skipped silently.

No task-quality, attention-benefit, memory-benefit or competitor-superiority
claim follows from deterministic transport tests. The comparative study remains
explicitly unrun. Future PRs require independent review of the exact pushed
cumulative diff, including relevant subsequent pushes.
