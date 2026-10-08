---
id: standing-public-goal-programmes
title: "ADR-027: Finite standing public goal programmes"
---

# ADR-027: Finite standing public goal programmes

**Status:** Branch-local proposed accepted target under
[#1003](https://github.com/seraph-quest/seraph/issues/1003), effective only on
the independently reviewed implementation merge. This file does not establish
Shipped availability. [Development Status](../STATUS.md) owns implementation truth.

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
