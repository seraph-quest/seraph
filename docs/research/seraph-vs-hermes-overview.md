---
id: seraph-vs-hermes-overview
slug: /seraph-vs-hermes-overview
title: Seraph and Hermes — overview
---

# Seraph and Hermes — overview

**Research date: October 5, 2026.** This comparison describes inspected
implementations and documented journeys. It does not rank the agents or establish
which one produces better results. The [detailed comparison](./seraph-vs-hermes-detailed.md)
contains the capability matrix and pinned sources.

Seraph's useful distinction is its operator-controlled guardian contract: connect
explicit goals and permitted context to bounded proposals, reviewed execution,
inspectable results and canonical memory. Hermes offers a broad personal-agent
environment with persistent goals, procedural skills, durable task dispatch,
schedules, messaging and a native Desktop interface. Both already contain
substantial work beyond a chat window.

## What was compared

Seraph's inspected `develop` baseline is
[`0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a`](https://github.com/seraph-quest/seraph/commit/0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a).
The bounded-workflow program [#899](https://github.com/seraph-quest/seraph/issues/899)
landed through [PR #953](https://github.com/seraph-quest/seraph/pull/953) on
October 4. That is `develop` availability, not evidence of a release from `main`.
The separate `main` baseline is
[`d7711b70321e54f0bd7ccdeac5a7cd2a87a0b22f`](https://github.com/seraph-quest/seraph/commit/d7711b70321e54f0bd7ccdeac5a7cd2a87a0b22f).

Hermes was inspected at official `main` commit
[`93c9360a8da592cee43e8895403e91c7a920b0ce`](https://github.com/NousResearch/hermes-agent/commit/93c9360a8da592cee43e8895403e91c7a920b0ce).
Its official latest-release endpoint reported
[v2026.9.24 / v0.21.5](https://github.com/NousResearch/hermes-agent/releases/tag/v2026.9.24),
published September 24. Features present on moving `main` must not all be
attributed to that stable tag.

The [Seraph baseline report](./seraph-capability-baseline-2026-10-05.md) separates
source inspection, existing receipts and fresh focused checks. The model-focused
checks reported 55 passed and 6 failed; source/procedure checks reported 39 passed
and 2 failed. Those failures remain unresolved evidence. Neither a large passing
test count nor an implemented endpoint establishes live model usefulness.

## What each already offers

| Operator journey | Seraph baseline | Hermes baseline |
| --- | --- | --- |
| Keep a goal moving | Canonical finite goals, observer context, strategist decisions and a Work board with reviewed proposals. The complete evidence-to-useful-action loop remains Partial. | Persistent single-session `/goal`, completion contracts, deterministic shell gates and bounded continuation; a separate Kanban board handles dependencies and workers. |
| Run research and practical work | Finite read-only research children, a fixed reviewed public-evidence pipeline, native repository repair/publication, evidence working sets and narrow connected actions are already implemented. Their declared limits matter. | Broad terminal, browser, web, file, plugin/MCP and delegation surfaces; durable Kanban and cron provide operational scheduling and recovery. Outcome quality is unmeasured here. |
| Remember what matters | Canonical memory, evidence dependencies and separately reviewed procedure preferences. Existing manual procedure feedback is not measured intervention usefulness. | Local notes/user profile, session recall, learned skills and optional external memory providers. Memory and skill write approvals are configurable. |
| Continue across interfaces | Cockpit and paired voice/messaging/context edges; readiness and acceptance remain adapter/platform specific. | CLI/TUI, native Desktop and many messaging adapters share agent state; voice includes local and cloud speech options. Adapter reliability was not tested. |
| Control execution and data | Typed permissions, approvals, durable reservations, effective-route receipts and revocation. Host-user execution is not OS isolation. | Configurable command/file controls, secret sources and optional egress controls. Its security policy identifies OS containment as the meaningful boundary. |

These are different tradeoffs, not a scorecard. Hermes's breadth, Desktop and
durable work surfaces are genuine comparison inputs. Seraph's finite contracts
and reviewed provenance are meaningful implementation properties; their effect
on real operator outcomes still needs measurement.

## The next Seraph work

The accepted target belongs to the [Project Constitution](/)
and its ADRs. This research supports the sequence in the
[guardian capability roadmap](/guardian-capability-roadmap):

1. [M1: purpose-specific inference](/guardian-capability-roadmap#m1-purpose-specific-inference)
   makes effective OpenRouter routes explicit for each purpose.
2. [M2: fresh goals and evidence-cited opportunities](/guardian-capability-roadmap#m2-evidence-cited-opportunities)
   connects fresh permitted context to a concrete goal with inspectable citations.
3. [M3: reviewed plans and bounded execution](/guardian-capability-roadmap#m3-reviewed-opportunity-plans)
   turns an accepted opportunity into existing Work board proposals and native
   executors, retaining finite authority and approval.
4. [M4: reviewed usefulness preferences](/guardian-capability-roadmap#m4-reviewed-intervention-usefulness)
   uses explicit intervention feedback to propose a limited, reversible preference.
5. [M5: verified optional NEAR inference](/guardian-capability-roadmap#m5-verified-near-inference)
   remains blocked pending independent verification and a decision consistent
   with the OpenRouter-only contract.

This sequence builds on completed finite capabilities. It does not reopen
research, pipeline, evidence or procedure implementation as if they were missing,
and it does not authorize arbitrary autonomous side effects.

## What would settle the comparison

Run the same concrete journeys on pinned versions with declared permissions,
comparable providers and budgets. Read back the result, interrupt execution,
inspect recovery and data handling, and record operator usefulness separately
from mechanical success. No Hermes runtime or independent comparative benchmark
was run for this research. The correct conclusion today is a sourced capability
comparison with named uncertainties.
