---
id: guardian-capability-roadmap
title: Guardian capability roadmap
---

# Guardian capability roadmap

**Document class:** Target, reviewed architecture and capability sequence under
[#954](https://github.com/seraph-quest/seraph/issues/954). All new capabilities
below are **Planned**; no implementation, deployment or superiority result is
claimed. GitHub owns queue, assignee, review and completion state.

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
Both are branch-local target decisions until the independently reviewed #954
documentation PR is merged. The implementation agent must not silently adopt
an unmerged change to a locked contract. ADR-006 remains active; M5's exception
is deliberately not adopted by this roadmap.

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
tests in the ticket. First implementation ticket: **[#955](https://github.com/seraph-quest/seraph/issues/955)**, after the #954
ADR adoption prerequisite is merged.

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

## M5: Verified NEAR inference

**Issue:** [#959](https://github.com/seraph-quest/seraph/issues/959). Optional **blocked** target: one encrypted, nonstreaming,
text-only canonical-model request to the NEAR Cloud gateway, verified before Seraph
releases plaintext output. No tools, images, embeddings, streaming, Responses API,
fallback, agent hosting or exact-serving-instance claim.

The [NEAR boundary research](/research/near-ai-trust-boundary-2026-10-05) is
the evidence contract. A future reviewed ADR-006 exception must include exact SDK
artifact/hash, deployment measurements/build identities, trust-root versions,
strict advisory policy and replayable cryptographic fixture provenance. These
external trust facts are not invented in this task. Until fixed and independently
reviewed, the implementation agent stops before adding a transport/dependency.

The ticket fixes the smallest integration and fail-closed behavior; its unblock
checklist supplies the remaining exact acceptance requirements. Fresh gateway,
model/GPU, same-connection identity and provider_tee response checks plus E2EE are
mandatory. Failed/missing/stale verification produces blocked/unknown, zero
released answer and no memory/tool use. Existing admission/accounting owns all
costs, including response-verification failure after contact. Local fixture
acceptance and later separately authorized live operation remain separate.

This could offer an explicit confidentiality choice. It does not make Seraph's
host, local storage, logs or connectors confidential, nor repair the protocol's
documented incomplete response chain/shared signing-key limitation. Rollback
disables route authority, quiesces calls and retains liabilities/receipts.

## What better means

The target is more verified useful work per unit of operator attention, within
the same authority and spend. Feature count and fixture success are not superiority.
Reuse deferred [#771](https://github.com/seraph-quest/seraph/issues/771) for an
eventual comparative evaluation; do not make a new dashboard or harness a milestone.

Pre-register 12 permitted public research/maintenance journeys and 12 scripted
watch changes (relevant, irrelevant, duplicate, stale, malicious and missing source),
with blinded outcome rubrics before running. Use five independent repetitions per
system/task, randomized order and clean copies of the same source corpus. Pin each
system/release, model, prompt budget, permissions, host and tool versions; publish
deviations. Run a matched-model/budget track where supported and a clearly separate
native-recommended track. No missing feature is scored as a crash; report unsupported.

| Metric | Fixed measurement |
| --- | --- |
| Task success | fraction achieving predeclared source/result readback, with failure reasons |
| Useful proactivity | explicit helpful judgments / judged opportunities; report unjudged separately |
| Unwanted interruption | explicit unwanted pushes / delivered pushes and pushes per operator-day |
| Recovery burden | operator actions and minutes to recover injected restart/cancel/transport failures |
| Memory usefulness | blinded later-task success with accepted preference vs baseline, deletion correctness separately |
| Operator effort | active review/correction minutes and decisions per completed outcome |
| Privacy | observed destination/payload classes against exact granted authority; any unauthorized egress is a failure |
| Latency/cost | median/p95 time to verified result; reported billed/estimated/unknown costs separately, plus local resource use |

For a claim of improvement require no unauthorized effect/egress, no material
regression in task success or unwanted interruption, and a predeclared improvement
in success or operator effort with uncertainty intervals. Human usefulness needs
consented real operators; scripted relevance fixtures prove mechanics only. Report
sample sizes, missing runs, model differences and uncertainty. **All comparative
evaluations here are unrun.** They do not block provider-free implementation merge
when its local acceptance is complete; external usefulness remains Partial/unverified.

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
