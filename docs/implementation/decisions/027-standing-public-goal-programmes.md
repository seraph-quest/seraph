---
id: standing-public-goal-programmes
title: "ADR-027: Finite standing public goal programmes"
---

# ADR-027: Finite standing public goal programmes

**Status:** Accepted target under
[#1003](https://github.com/seraph-quest/seraph/issues/1003), effective on
the independently reviewed implementation merge. The source branch describes
the intended post-merge authority contract; it does not establish Shipped
availability before that merge. [Development Status](../STATUS.md) owns implementation truth.

**Decision class:** Narrow public service-authority exception to
[ADR-023](./023-evidence-bound-guardian-opportunities.md).

## Context

Legacy opportunities require a live original authenticated Root at admission,
precontact and adoption. Existing public source watches have a finite service
identity lifetime. A separately reviewed programme should discover public
sources from an operator's chosen brief across browser logout and expiry without
copying private goal text, extending browser sessions or transferring operator
control. The current Python owners remain authoritative until a separately
reviewed Cordis ownership cutover under [ADR-026](./026-all-plugin-cordis-architecture.md).

## Decision

The operator locally supplies and reviews a `public_brief` separately from the
private Goal title and description. No model draft or automatic goal-text copy
is included in this milestone; any later drafting requires its own current
goal-text egress permission. Source/model prose remains untrusted data and
cannot expand the brief, grant, capabilities, URLs, priority, limits or learning.

One finite `GoalProgramme.v1` generation binds canonical Goal ID/revision,
public-brief SHA256, grant revision, stable active operator identity, issuing
Root ID/principal provenance, current governed route epoch and policy digest,
exact capability IDs, server-assigned local artifact prefix, inference ceiling,
cadence, notification limits, confirmation time and original expiry. The server
sets expiry from the preview clock; acceptance does not restart it. Daily is
the only cadence, seven days is the maximum grant duration, and at most one
programme run may be outstanding. The deployment inference broker remains
the single serial lane and canonical accounting owner.

The complete exact preview requires explicit acknowledgments of public-web
search/read, the local artifact prefix and finite inference ceiling before
acceptance. Keys, configuration, goal status, a model suggestion, or a recovered
read scope grant no programme authority. Zero budget or missing governed policy
retains a configured-but-blocked generation with a recovery reason.

Preview never grants the proposed new generation execution authority. Saving a
preview with a changed public brief immediately pauses the previous active or
blocked generation, before acceptance, because a purpose correction must fence
future runs and late adoption. The preview response names the paused programme
IDs and the UI explains this effect before saving the changed brief. Abandoning
the review does not resume the old generation. An unchanged-brief renewal pauses
its predecessor only upon acceptance.

The live-original-Root requirement is superseded **only** for these explicitly
reviewed public programme capabilities at new admission, precontact and result
adoption: `guardian.goal-discovery.v1`, `guardian.query-plan.v1`,
`guardian.public-search.v1`, `source.public-extract.v1`, and
`guardian.prepare-brief.v1`. Their concrete execution remains separately bounded
by the accepted capability contract. This is no authority for private sources,
workspace egress, authenticated browsing, external mutation, code execution,
learning, arbitrary tools or grant renewal. Legacy watches/opportunities retain
their existing contracts and receive no implicit exception.

The service checks the original generation before every contact and adoption:
active stable identity, retained authentic issuer provenance, exact current Goal
and public-brief/grant revisions, capability identity, unchanged route epoch and
policy digest, budget, deadline and explicit programme state. Browser logout or
expiry alone does not revoke or renew a reviewed finite public generation.
Programme pause/revoke, stable identity revoke, Goal correction/inactivation,
route change or original grant expiry fences future contacts and late adoption.
Already contacted work retains its receipts and unresolved cost/effect liability;
no contacted Unknown attempt is blindly retried.

The authority owner's fresh read-return is a validation seam, not by itself an
execution permit or atomic contact fence. The existing native job/effect owner
must atomically validate these canonical bindings when claiming the original
contact attempt; that CAS is the contact linearization point. Physical network
or filesystem work occurs outside short database writers. Local result adoption
has a separate canonical publication CAS that rechecks the original generation
and all current bindings; a pause, revoke or correction before this adoption
withholds late output. Contact already linearized retains its original liability.
No executor may activate before its native contact/adoption integration is
implemented and independently reviewed. This authority milestone introduces no
alternative effect ledger or inference queue.

The current Python seam is `GoalProgrammeAuthorityBinding` plus
`GoalProgrammeService.validate_current_binding(db=..., binding=..., policy=...)`.
The immutable binding includes exact Goal/programme/grant revisions, stable
identity and issuer, public-brief digest, capability, route epoch/digest, original
expiry and cost ceiling. The validator reloads canonical Goal/identity/issuer
facts in the caller's same native writer; it opens no session and performs no
physical I/O. Its result has effect only when that owner commits its own original
contact or adoption CAS in the same transaction. Current policy and physical
evidence are staged and fenced by their existing owners before the writer;
neither stale snapshots nor a caller-selected route become authority. Discovery
executor integration remains required before that executor activates.

The discovery executor holds the existing `configuration_mutation_lock` across
current policy staging and the native contact/adoption CAS commit, so policy
revocation cannot interleave with a claimed transition. Read policy before
opening the database writer; release the lock after that writer commits, before
physical provider or filesystem I/O. The contact owner binds the exact existing
job, original attempt, lease/fence and effect-intent identity in its transition.
Adoption binds the same original attempt plus the staged immutable artifact
reference/digest, checks current generation authority again and atomically
publishes only that output. This milestone supplies authority validation and
controls; it does not supply or claim those discovery execution transitions.

Renewal creates a new immutable generation and pauses its predecessor. It
cannot extend an old attempt's expiry, route binding, budget, effects or adoption
permission. Expiry projects one passive review-due state: no automatic renewal,
notification, model request or fresh execution follows. History and liabilities
remain visible. When retained generation capacity is full, new acceptance is
blocked rather than deleting history to manufacture capacity.

## Ownership, recovery and storage

Use the existing canonical `Goal.goal_programmes_json` nullable additive column
for programme reviews and retained generations, separate from the closed legacy
guardian-policy schema. Existing rows default to no programme. No additional
workspace, queue, inference ledger or composition ownership state is introduced.
Native jobs/effect journals, private artifacts, approvals and accounting retain
their existing owners. The programme service explicitly starts/stops with the
current Python lifecycle and activates no timer or work on import.

New login inherits no execution or mutation authority. Programme metadata reads
from another Root require existing explicitly selected Goal read recovery. That
read recovery is never used by mutation paths. A fresh current Root may pause
or revoke an exact programme/current grant revision only after a separate
explicit recovery acknowledgment, proving the same active stable identity and
the original issuer binding. This narrow reduction of authority cannot accept,
renew, adopt, extend or replay old work. New programme acceptance still requires
the current canonical Goal owner and a complete new preview/review.

The canonical original Root is retained as provenance, including after logout;
a bearer tombstone or missing/changed issuer identity does not prove ownership.
Missing dependencies, invalid state or incomplete identity fail closed with
operator-visible recovery. Service shutdown prevents fresh calls without
altering durable authority history or resetting its clocks.

## Verification and consequences

Require isolated SQLite migration/rerun, schema, exact-review, private/public
separation, owner isolation, explicit recovery, revocation, Goal/route correction,
zero-budget, clock-expiry and logout/restart checks. Deny external inference and
external network contacts; local scripted mechanics establish no model quality
or usefulness. Programme execution must later demonstrate the complete governed
public discovery journey with actual local artifacts and readback under the
existing no-evals/provider-free implementation boundary.

Independent Critic/Contrarian disposition and validation receipts belong in the
implementation PR. This exception adds durable service authority risk; finite
original clocks, exact generations, current identity/policy fences and explicit
operator revocation are mandatory. It does not claim that in-process dependency
injection is isolation or that all future public sources are supported.

## Accepted public discovery execution contract

[#1004](https://github.com/seraph-quest/seraph/issues/1004) extends this authority
contract with `guardian.goal-discovery.v1` under the current Python owner. This
accepted target is independent of the later Cordis research cutover. It does not
introduce composition ownership state, another queue, an inference ledger, or a
synthetic browser Root.

`GoalResearchPlanSpecV1` is the sole closed plan schema. Its canonical `goal_id`
is the existing bounded safe-reference string (1–128 characters), byte-equal to
the live Goal and immutable programme binding. Existing eight-character and
setup Goal IDs are retained without UUID conversion. Plan, programme and
idempotency identifiers remain UUIDs; programme and grant aliases identify one
immutable generation. The four stages are exactly `plan_queries`,
`search_public`, `extract_sources`, and `prepare_brief`, with their version-1
capabilities. Search emits both the immutable manifest and its bound opaque-ID
selection; extraction never accepts a model-provided URL.

One original daily UTC occurrence enters the canonical native job queue. The
original deadline is at most 300 seconds and is assigned once, intersected with
the original grant expiry. No queue wait, restart, new plan ID, or renewal grants
more time. There are at most three queries, fifteen combined deduplicated search
results, four selected sources, and four total governed inference requests.
The existing broker remains the single remote lane. Generation spend includes
all settled charges and reserved, contacted or Unknown liabilities across every
accounting period. A previous outstanding occurrence for the same canonical Goal
and stable operator identity retains its hold across new generations. A different
Goal for the same identity is not counted as that Goal's discovery occurrence.

Public search is a pinned HTTPS POST only to
`https://html.duckduckgo.com/html/`, with only bounded `q`, empty `b`, and fixed
locale `kl` form fields. It permits neither credentials/cookies/proxies nor
redirects, DDGS routing, fallback backends, or retries. Each transfer is bounded
to twenty seconds and 512 KiB. Recognized no-result markup is distinct from
CAPTCHA, unexpected markup, and response loss. Selected sources additionally
require current site policy, public address pinning, a 256 KiB raw cap, and
supported UTF-8 text/plain or text/html. Normalized stored text is at most
64 KiB; unsupported content is never silently truncated.

The complete reviewed public brief is staged and physically read back through
the existing safe artifact owner before admission, then adopted by the original
native writer. Unadopted staging is not authority and cannot grant contact.
Partial or altered immutable files fail readback; they are not overwritten to
manufacture successful recovery. The 2,048-character legacy question, 8 KiB
serialized prompt, 16 KiB child, and 64 KiB dossier limits remain intact. An
unsupported full brief or prompt produces explicit coverage before that contact.

`DiscoveryBrief.v1` has findings, citations, uncertainties, prepared artifact
references, inert proposed next steps, and native coverage. Coverage binds the
entire original brief digest/byte count and each represented snapshot/span
digest, while disclosing omitted lines. Mechanical citations do not establish
semantic truth. Prepared checklists remain local and require separate acceptance
for further work. First useful evidence can produce findings immediately; later
unchanged URL/content pairs produce a quiet outcome. All outcomes explicitly
record `no_learning`.

The native service starts/stops in the existing App lifecycle and the dispatcher
holds only its exact server-owned pointer. Scheduler registration is bounded and
coalesced, admitting the current UTC slot without historical catch-up. Before
every contact and adoption, physical artifacts are reopened outside the short
writer, then their original job/input/authority/checkpoint digests and current
programme/Goal/identity/route are checked under the existing configuration fence.
An optional current `StrategyResolver` supplies the pinned typed method binding;
absence remains an explicit baseline with no future-module import.
An active binding applies only canonical `ResearchStrategy.v1` directives:
query templates during query planning, finite source preferences and evidence
fields during selection, and draft sections, evidence fields and stop conditions
during brief preparation. The full accepted data stays immutable and must pass
vault-aware safety checks before staging; each request contains only its stage's
projection, with the original binding rechecked before contact and adoption.
Unsafe, unrepresentable or oversized inputs fail closed within the existing
8 KiB prompt bound; they cannot add tools, permissions, contacts or private Goal
context. An absent strategy preserves the baseline request shapes.

Authenticated operators inspect retained canonical occurrence history and
explicitly select a completed local brief for current-authority readback. A new
login uses the existing selected read-only Goal recovery. It cannot renew old
work, replay a provider, hide Unknown effects, or release accounting liability.
Implementation availability and unresolved recovery limits are recorded in
[Development Status](../STATUS.md); isolated local scripted transport receipts
are not provider reachability, model-quality, or overall review approval.

The initial independent review identified two holds: a read-return alone cannot
linearize contacts/adoption, and changed-brief preview must disclose its pause
effect. Both findings are accepted in this branch contract: the DB-only binding
validator supports the existing native writer, executor activation remains
blocked pending its actual integration, and preview explicitly reports paused
programme IDs. A fresh cumulative review must verify the repaired implementation
before merge; this disposition alone is not a review-passed claim.
