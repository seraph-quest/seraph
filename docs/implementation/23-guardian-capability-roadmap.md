---
id: guardian-capability-roadmap
title: Guardian capability roadmap
---

# Guardian capability roadmap

**Document class:** Target, accepted architecture and capability sequence under
[#954](https://github.com/seraph-quest/seraph/issues/954).
[Development Status](./STATUS.md) owns implemented capability scope and validation
limits. This roadmap supplies no deployment or superiority result; GitHub owns
queue, assignee, review and completion state.

Read the [baseline](/research/seraph-capability-baseline-2026-10-05),
[Hermes overview](/research/seraph-vs-hermes-overview) /
[detail](/research/seraph-vs-hermes-detailed), and
[IronClaw overview](/research/seraph-vs-ironclaw-overview) /
[detail](/research/seraph-vs-ironclaw-detailed).

## Decision and sequencing

Make existing capabilities cooperate around a fresh goal: permitted change →
cited relevance judgment → quiet prioritized opportunity → reviewed bounded
plan → native verified outcome → separately reviewed feedback preference.
Keep the current stack and owners. Do not replace Work Board, source watches,
memory, scheduler, browser, coding, research or integrations.

[ADR-023](./decisions/023-evidence-bound-guardian-opportunities.md) fixes M2–M4.
[ADR-024](./decisions/024-purpose-specific-openrouter-routes.md) fixes M1.
Both decisions were accepted through the reviewed #954 documentation merge.
ADR-006 retains ordinary OpenRouter inference; the accepted ADR-025 decision
permits only the separate optional M5 NEAR HTTPS capability. This roadmap does
not broaden those locked contracts.

Recommended delivery order is **M1, M2, M3, M4**. M2 technically uses the existing
strategist route and does not depend on M1; M3 depends on M2, M4 on M3. M5 is an
optional separate branch of work and blocks none of M1–M4. Each milestone is one
parent issue/project item, with internal checklist slices and one ready aggregate
PR. No child issue is necessary while ownership/acceptance remain one milestone.

## M1: Purpose-specific inference

**Issue:** [#955](https://github.com/seraph-quest/seraph/issues/955). Operator can select separate text, vision and embedding
models without losing chat when a different purpose is unavailable.

Extend the existing settings endpoint, profile selectors, screenshot caller,
embedder and panel. Exactly three slots share one vault key, one ceiling and one
serial inference lane. Migration preserves existing consent; new purposes need
explicit acknowledgment. Witnessed revoke→accounting→activate publication fails
closed across crashes. Existing vector namespaces isolate model/dimension/schema.

Internal slices: reproduce six setup failures; v2 schema/migration; all canonical
callers; proofs/consent and witnessed saves; embedding namespaces; settings UI;
provider-free full journey and review. No provider expansion, default model
invention, automatic reembedding or paid activation.

Acceptance: authenticated save/restart/readback; each purpose reaches only its
permitted intercepted adapter; unchanged text proof survives a vision-only edit;
one callback active; shared exhausted/unknown costs block every slot; interrupted
save stays revoked. Roll out disabled new purposes; rollback preserves liabilities,
namespace/tombstone history and requires explicit compatible configuration.

Competitive value is simpler honest setup, not provider-count parity. Risk is
the save/accounting boundary, addressed by the explicit fixed protocol and fault
tests in the ticket. The bounded implementation is owned by
**[#955](https://github.com/seraph-quest/seraph/issues/955)**; Development Status
records its implemented scope and evidence limits.

## M2: Evidence-cited opportunities

**Issue:** [#956](https://github.com/seraph-quest/seraph/issues/956). A fresh goal and selected public watch produce a source-cited
reason the change may matter, or stay quiet. Existing watch/Inbox remains usable.

Extend Goal policy and add one focused opportunity table, native bounded assessment,
existing Inbox projection and current intervention policy. Public verified changed
packets only; deterministic bounded excerpts; one strategist call; no tools; strict
citations; daily/pending limits; current Root/Goal/watch/policy at every boundary.
Default notifications zero. No private context expansion or arbitrary discovery.

Internal slices: current M6 blocked regression; additive migration/policy; packet
admission and dedupe; bounded judgment; Inbox/recovery; managed journey. Acceptance
includes actual source→dossier→opportunity persistence, invalid citations/source
correction rejection, quiet hours, restart/cancellation/unknown cost and foreground
priority. Rollout per-goal opt-in; rollback disables assessment and quiesces pending
jobs, retaining evidence. Model relevance is a judgment, not truth; first-lines
excerpts and the lexical prefilter may miss important changes.

## M3: Reviewed opportunity plans

**Issue:** [#957](https://github.com/seraph-quest/seraph/issues/957). The operator reviews a one-step browser check or existing
three-step public evidence report, then explicitly accepts and queues that plan.

Depends on M2. Extend WorkProposal with opportunity bindings and exactly two
server blueprints. Staging is advisory; explicit acceptance queues native work,
retaining every capability approval and current authority. Auto-stage is a separate
opt-in and never auto-accepts. No model-supplied URLs, commands, arbitrary DAG or
external write.

Internal slices: typed plan schema; current source materialization; proposal CAS
and TTL; truthful review/queue UI; native execution/readback/recovery. Acceptance
must execute actual local browser and file-backed dossier/report mechanisms with
only external boundaries intercepted, not seeded successes. Artifact proof and
explicit no_learning complete the bounded journey. Rollback disables new staging,
revokes future contact and uses existing task cancellation; it preserves late and
unknown effects. Main risk is confusing suggestion with approval; button copy and
negative tests make acceptance consequences explicit.

## M4: Reviewed intervention usefulness

**Issue:** [#958](https://github.com/seraph-quest/seraph/issues/958). Explicit judgments about actual outcomes can reorder later
eligible plan offers or suppress optional watch opportunities after separate review.

Depends on M3. Extend existing intervention feedback/history and MemoryProposal,
not a new memory store. Delivered/acknowledged are never usefulness votes. Exact
feedback population, verified helpful outcomes, signed scope, adoption, invalidation
and rollback are fixed in ADR-023. No personality inference, automatic adoption,
expanded permissions, raised cadence or modification of ADR-015's procedure population.

Internal slices: linked intervention/feedback migration; exact outcome binding;
finite provider-free recommendation; signed review/adoption; later offer and rollback;
privacy/deletion tests. Acceptance: two qualifying explicit helpful outcomes propose
a preference, operator adopts it, a later unjudged opportunity changes offer order,
new contrary feedback invalidates it and rollback restores ordinary order. Delivery
alone yields no_learning. Rollout suggestion-only until adoption; preserve history
and tombstones on rollback. Actual benefit remains an unrun human outcome question.

## M5: Optional NEAR HTTPS text inference {#m5-verified-near-inference}

**Issue:** [#959](https://github.com/seraph-quest/seraph/issues/959). The optional
capability accepts one bounded private question at the fixed NEAR Cloud HTTPS
endpoint, canonical z-ai/glm-5.3-flash, through inference.near-text.v1/near.text.
[ADR-025](./decisions/025-near-https-text-inference.md) defines the accepted narrow
ADR-006 exception; Development Status owns its implemented scope. Ordinary
OpenRouter slots remain unchanged. Provider reads plaintext; no TEE, E2EE,
measured deployment, response-signature or exact-instance assurance is claimed.

Existing current Root/Goal grants, witnessed plaintext consent, separate vault key,
shared one-active broker and deployment accounting bound the request. One inference
attempt, finite original deadline, private artifact/readback and explicit no_learning
are required. Authoritative billing settlement gates answer release and Done;
missing cost retains Unknown/full reserve, discards plaintext and never replays.
Keyless managed UI/runtime and intercepted HTTP/serial/revocation/accounting negatives
provide local acceptance. No paid call or harness campaign is merge acceptance.

The [dated NEAR research](/research/near-ai-trust-boundary-2026-10-05) preserves the
historical verified-inference investigation; its TEE corpus prerequisites no longer
block this revised HTTPS scope. Rollback disables NEAR and preserves debt/audit.
Live availability, price and answer usefulness remain unverified without separate
operator-authorized operational evidence.

## What better means

The target is more verified useful work per unit of operator attention, within
the same authority and spend. Feature count and fixture success are not superiority.
The operator cancelled harness-improvement and comparative evaluation campaigns
on 2026-10-06; [#771](https://github.com/seraph-quest/seraph/issues/771) is closed
as not planned, not delivered. This roadmap contains no pending campaign,
comparator protocol, model-credit prerequisite or synthetic replacement.

External usefulness and comparative quality remain Partial/unverified. Ordinary
provider-free regression, security, authority, readback and recovery checks remain
required for their owning capabilities. Deterministic fixtures establish only the
mechanics they exercise; they do not establish usefulness, learned quality or
harness improvement. Cancellation does not supply missing quality evidence or
permit a superiority claim.

## Review and handoff contract

Each issue contains the same twelve-section executable specification, exact files,
new schemas/paths, ordered steps, migrations, negative cases, commands, rollback and
stop conditions. Changed baseline/contracts require evidence back to the lead,
not an implementation agent's redesign. Exact cumulative pushed-head independent
review is mandatory; a relevant subsequent push invalidates prior review.

For future validation, use an existing absolute writable temporary directory:
`TMPDIR=/tmp` on Linux or `TMPDIR=/private/tmp` on macOS, checking the directory
before running commands. Historical macOS receipts retain their original path.
Temporary validation files do not authorize source worktrees under `/tmp`.

Planning critique accepted corrections to proposal dispatch, goal/watch revision
binding, no-push feedback ownership, deterministic excerpt selection, operation vs
review expiry, prospective learning populations, unchanged slot proof reuse and
witnessed configuration/key publication. The final PR records verification and
disposition. No research document itself grants execution authority.

### Starting prompt

```text
Implement only Seraph milestone #955:
https://github.com/seraph-quest/seraph/issues/955

First verify that the independently reviewed documentation PR linked to #954
has merged and ADR-024 is adopted. Until then, stop before product edits and
report that exact prerequisite. Read AGENTS.md, the Constitution, ADR-006,
ADR-024, the dated baseline, and all twelve sections of #955.

Create feat/guardian-purpose-routes from the required latest develop baseline.
Follow the prescribed steps and fixed three-slot, shared-accounting,
revoked-before-key-install publication, consent, proof and namespace contracts.
Reproduce and resolve the six known focused failures without weakening tests.
Retain the required provider-free migration, race/fault, serial-admission,
managed UI and restart/readback evidence. Use the required worker team and an
independent critic on the exact pushed cumulative diff; repeat review after
relevant pushes. Deliver one complete ready aggregate PR and accurate Project
state. No paid calls, deployment or main promotion. Stop on changed contracts,
missing authority or incompatible baseline, preserving evidence for the lead;
do not invent a replacement architecture or silently broaden scope.
```
