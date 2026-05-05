---
title: 19. Strategy Claim Ledger
---

# 19. Strategy Claim Ledger

## Purpose

This is the M0 claim-control ledger for the world-class Seraph strategy.

It owns the exact wording gate for strategic claims about:

- world-class ambition
- capability-first product shape
- guardian moat
- memory
- security and trusted execution
- cockpit/operator control
- workflow and delegation
- selective reach
- benchmark-proof status

This file does not own the competition table. The source-backed competitor matrix remains in [18. Agent Competition Truth Table](./18-agent-competition-truth-table.md). This ledger turns that evidence and the implementation mirror into allowed wording, forbidden wording, proof paths, owners, milestones, issue links, and trust/operator surfaces.

No claim in docs, issues, or PRs should use stronger wording than the row below allows.

## Status Model

| Status | Meaning | Allowed posture |
| --- | --- | --- |
| `Backed` | The strategy rule or shipped surface has repo evidence and an operator/proof surface on `develop`. | Use direct shipped wording, but keep the claim scoped to the proven surface. |
| `Partially backed` | Foundations exist, but the claim is narrower than the strategy target or lacks full benchmark/comparison proof. | Use "foundations", "first surface", "closing the gap", or "being built toward". |
| `Behind` | Primary-source competitor evidence shows a stronger or broader surface than Seraph has shipped. | Name the gap directly and tie it to a milestone. |
| `At par` | Evidence supports rough parity, but not superiority. | Use parity wording only on the named axis. |
| `Unknown` | Evidence is insufficient or not source-backed. | Avoid competitive claims until refreshed. |
| `Aspirational` | The target is strategic but not yet proven on `develop`. | Use target language only: "should", "aims", "path", "requires". |

## Review Gate

Reviewers should block, weaken, or route through this ledger before allowing any unqualified use of:

- "world-class"
- "best"
- "greatest"
- "strongest"
- "superior"
- "ahead"
- "secure"
- "private"
- "trusted"
- "production-ready"
- "complete"
- "fully shipped"
- "only"
- "first"

Claims are acceptable only when they match the allowed wording and status in the ledger row, name a proof path, and expose a trust or operator surface.

## Claim Ledger

| ID | Claim area | Allowed wording | Forbidden wording | Status | Evidence | Proof path | Owner | Milestone | Issue link | Trust/operator surface |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `SCL-001` | World-class strategy | "Seraph is being built toward a world-class capability-first guardian agent OS/workspace." "Seraph's world-class path requires proof across M0-M9." | "Seraph is world-class." "Seraph is the greatest/best guardian agent OS." "Seraph is already stronger than the category." | `Aspirational` | [17. Seraph World-Class Strategy](./17-seraph-world-class-strategy.md); `docs/implementation/11-world-class-strategy-delivery.md`; [18. Agent Competition Truth Table](./18-agent-competition-truth-table.md). | All M0-M9 milestone proof, with claim-specific benchmark rows and operator receipts before any achieved world-class wording. | Lead / GitHub Project / #436 | M0-M9 | [#436](https://github.com/seraph-quest/seraph/issues/436) | This ledger, implementation status, GitHub Project issue state, benchmark reports, cockpit receipts. |
| `SCL-002` | Capability-first product shape | "Seraph is capability-first by strategy." "Seraph is being built as a capability-first guardian workspace." "Capabilities are the top product priority, with guardian intelligence as the differentiating operating mode." | "Seraph already has the broadest capability OS." "Seraph has full parity with Hermes/OpenClaw/Claude Code/CrewAI." "Capability breadth is solved." | `Backed` as strategy rule; `Partially backed` as shipped breadth claim | Strategy locked product shape in [17](./17-seraph-world-class-strategy.md); milestone stack and current shipped foundations in `docs/implementation/11-world-class-strategy-delivery.md` and `docs/implementation/STATUS.md`; competitor pressure in [18](./18-agent-competition-truth-table.md). | M1 capability manifest contract plus M2 execution and M9 ecosystem proofs; inventory must show capability source, trust level, owner, boundary, health, dependencies, and available actions. | #424 / #425 / #436 | M1, M2, M9 | [#424](https://github.com/seraph-quest/seraph/issues/424), [#425](https://github.com/seraph-quest/seraph/issues/425), [#436](https://github.com/seraph-quest/seraph/issues/436) | Capability palette, capability overview API, cockpit capability panes, extension lifecycle, activity/audit receipts. |
| `SCL-003` | Guardian moat | "Guardian intelligence is Seraph's differentiating moat target." "Seraph has guardian-state and intervention foundations." "Seraph ships deterministic M8 intervention-quality receipts for guardian judgment over capability choice and restraint." | "Seraph has superior guardian intelligence." "Seraph is the best guardian." "Seraph's guardian moat is proven." | `Partially backed` for foundations and deterministic M8 receipts; `Aspirational` for superiority | Guardian strategy in [17](./17-seraph-world-class-strategy.md); shipped guardian state, world model, feedback, restraint, intervention surfaces, live state-scoped M8 guardian-brain receipts, and `m8_guardian_intervention_quality` receipts in `docs/implementation/STATUS.md` and `docs/implementation/11-world-class-strategy-delivery.md`. | #437 guardian intervention quality benchmark plus M8 receipts showing guardian state changes act/defer/bundle/clarify/approval/stay-silent choices without lowering approval boundaries. | #431 / #437 | M8 | [#431](https://github.com/seraph-quest/seraph/issues/431), [#437](https://github.com/seraph-quest/seraph/issues/437) | Guardian-state surface, `/api/operator/m8-guardian-brain`, intervention policy diagnostics, continuity graph, cockpit guardian-state and restraint surfaces. |
| `SCL-004` | Memory foundations | "Seraph ships guardian memory and world-model foundations." "Seraph has a first additive memory-provider surface." "Memory superiority still requires M6 proof." | "Seraph has best-in-class memory." "Seraph has solved long-term memory." "Seraph's memory is superior to Hermes/OpenClaw/IronClaw." | `Backed` for foundations; `Partially backed` for memory quality; `Aspirational` for superiority | [14. Seraph Memory SOTA Roadmap](./14-seraph-memory-sota-roadmap.md); `docs/implementation/STATUS.md`; M6 delivery section in `docs/implementation/11-world-class-strategy-delivery.md`; benchmark axes in [18](./18-agent-competition-truth-table.md). | #433 memory superiority benchmark covering long-horizon recall, contradiction handling, stale-memory suppression, provider quality, and receipts showing memory changed behavior. | #433 / #441 | M6 | [#433](https://github.com/seraph-quest/seraph/issues/433), [#441](https://github.com/seraph-quest/seraph/issues/441) | Memory API, guardian-state memory diagnostics, provider inventory, `guardian_memory_quality` suite, operator-visible memory failure reports. |
| `SCL-005` | Security and trusted execution | "Seraph ships trust/control foundations: approvals, audit, policy, sandboxing, secret handling, and scoped execution receipts." "Seraph still needs M3 proof for secure-execution parity." | "Seraph is secure by default." "Seraph is private by default." "Seraph has IronClaw-class secure execution." "Seraph is production-ready." | `Partially backed`; `Behind` on IronClaw-class posture; blanket security/privacy claims not allowed | Trusted execution pillar in [17](./17-seraph-world-class-strategy.md); M3 delivery section in `docs/implementation/11-world-class-strategy-delivery.md`; `docs/implementation/STATUS.md`; IronClaw pressure in [18](./18-agent-competition-truth-table.md). | #435 security parity gauntlet plus M3 boundary evals for browser, connector, delegation, background, provider fallback, filesystem, process, and workflow paths. | #428 / #435 | M3 | [#428](https://github.com/seraph-quest/seraph/issues/428), [#435](https://github.com/seraph-quest/seraph/issues/435) | Approvals, audit log, tool policy surfaces, vault/secret-ref receipts, trust-boundary benchmark suite, cockpit safety receipts. |
| `SCL-006` | Cockpit/operator control | "Seraph ships a real cockpit/operator surface." "Seraph has cockpit foundations for supervised capability control." "The target is world-class cockpit legibility." | "Seraph has the best supervised-agent cockpit." "Seraph's cockpit is world-class." "Seraph is ahead of OpenClaw/OpenHands/Claude Code on cockpit quality." | `Backed` for existence; `Partially backed` for density; `Aspirational` for best-in-category | Cockpit pillar in [17](./17-seraph-world-class-strategy.md); M7 delivery section and #439 in `docs/implementation/11-world-class-strategy-delivery.md`; shipped cockpit surface in `docs/implementation/STATUS.md`; operator-surface pressure in [18](./18-agent-competition-truth-table.md). | #439 cockpit operator efficiency benchmark plus M7 receipts showing fast inspection of capability state, execution history, approvals, routing, spend, artifacts, interventions, and recovery. | #430 / #439 | M7 | [#430](https://github.com/seraph-quest/seraph/issues/430), [#439](https://github.com/seraph-quest/seraph/issues/439) | Browser cockpit, Activity Ledger, operator terminal, active triage, workflow views, capability panes, approval and recovery controls. |
| `SCL-007` | Workflow and delegation | "Seraph ships workflow supervision foundations." "Seraph has a first long-running workflow operating layer." "Seraph needs endurance proof before claiming LangGraph/Cursor/Devin-class workflow quality." | "Seraph has solved long-running work." "Seraph has LangGraph-class durable workflows." "Seraph is ahead of Devin/Cursor on delegated engineering work." | `Backed` for foundations; `Partially backed` for endurance; `Aspirational` for category parity/superiority | Supervised workflow layer in [17](./17-seraph-world-class-strategy.md); M5 delivery section and #440 in `docs/implementation/11-world-class-strategy-delivery.md`; shipped workflow/history/checkpoint surfaces in `docs/implementation/STATUS.md`; adjacent-agent pressure in [18](./18-agent-competition-truth-table.md). | #440 live workflow endurance canary plus M5 scenarios for branch, checkpoint, resume, compare, repair, handoff, background ownership, routines, and delegated execution. | #429 / #440 | M5 | [#429](https://github.com/seraph-quest/seraph/issues/429), [#440](https://github.com/seraph-quest/seraph/issues/440) | Workflow history, step records, branch-family supervision, background-session substrate, continuity graph, cockpit workflow controls, `workflow_endurance_and_repair` suite. |
| `SCL-008` | Selective reach | "Seraph has selective reach foundations." "Seraph should extend reach only where it improves guardian value, continuity, or execution." "Seraph is behind broader channel agents on raw reach." | "Seraph has broad Hermes/OpenClaw-class reach." "Seraph has complete channel coverage." "Seraph is ahead on reach." | `Partially backed`; `Behind` on raw channel breadth | Selective reach pillar in [17](./17-seraph-world-class-strategy.md); M4 delivery section and #438 in `docs/implementation/11-world-class-strategy-delivery.md`; `docs/implementation/STATUS.md`; Hermes/OpenClaw/Claude Code channel pressure in [18](./18-agent-competition-truth-table.md). | #438 one excellent reach channel canary plus M4 proof that each added channel preserves continuity, action safety, and operator control. | #426 / #438 | M4 | [#426](https://github.com/seraph-quest/seraph/issues/426), [#438](https://github.com/seraph-quest/seraph/issues/438) | Desktop shell, channel routing, observer continuity, presence surface inventory, operator timeline, Activity Ledger, `computer_use_browser_desktop` suite where browser/native reach is involved. |
| `SCL-009` | Benchmark-proof status | "Seraph has benchmark-proof foundations." "Seraph exposes named benchmark suites for several key claims." "Every major strategy claim still needs explicit proof before stronger wording." | "Seraph is fully benchmark-proof." "All superiority claims are proven." "The strategy is complete." | `Partially backed` | Benchmark/proof pillar in [17](./17-seraph-world-class-strategy.md); Cross-Cutting Benchmark-Grade Proof in `docs/implementation/11-world-class-strategy-delivery.md`; `docs/implementation/STATUS.md`; [18](./18-agent-competition-truth-table.md). | M0 ledger gate plus named suites for memory, workflow endurance, trust boundaries, computer use, governed improvement, and operator-readable receipts on `develop`. | #424 / #436 | M0, cross-cutting M1-M9 | [#424](https://github.com/seraph-quest/seraph/issues/424), [#436](https://github.com/seraph-quest/seraph/issues/436) | Operator benchmark report, runtime eval harness, benchmark suite details, Activity Ledger receipts, implementation status. |
| `SCL-010` | Guardian moat over capability substrate | "Seraph's intended advantage is combining capability execution with guardian memory, trust, supervision, and restraint." "This remains the world-class path, not a fully proven outcome." | "Seraph already beats generic agents because it has guardian context." "Seraph is uniquely safe and capable." "Competitors cannot match this moat." | `Aspirational` with `Partially backed` foundations | Strategy thesis in [17](./17-seraph-world-class-strategy.md); M2-M8 delivery requirements in `docs/implementation/11-world-class-strategy-delivery.md`; adjacent-agent benchmark pressure in [18](./18-agent-competition-truth-table.md). | Combined proof across M2 execution, M3 trust, M5 workflows, M6 memory, M7 cockpit, and M8 guardian judgment, showing capability choice, restraint, recovery, and follow-through improved by guardian state. | Lead / #424 / #431 / #436 | M2-M8 | [#424](https://github.com/seraph-quest/seraph/issues/424), [#431](https://github.com/seraph-quest/seraph/issues/431), [#436](https://github.com/seraph-quest/seraph/issues/436) | Guardian-state diagnostics, capability receipts, approval/audit trail, workflow and activity ledgers, cockpit recovery actions. |

## Short Replacement Rules

| Risky wording | Replacement |
| --- | --- |
| "Seraph is world-class" | "Seraph is being built toward a world-class target" |
| "Seraph is the best/strongest/greatest" | "Seraph's strategy is to become..." |
| "Seraph has superior guardian intelligence" | "Seraph has guardian-intelligence foundations and aims to prove superiority through #437/#431/#433" |
| "Seraph has best-in-class memory" | "Seraph ships memory foundations; memory superiority requires M6 proof" |
| "Seraph is secure/private by default" | "Seraph ships specific trust foundations: approvals, audit, policy, sandboxing, secret handling, and receipts" |
| "Seraph has the best cockpit" | "Seraph ships a real cockpit; world-class cockpit legibility remains an M7 target" |
| "Seraph has solved workflows" | "Seraph ships workflow supervision foundations and needs M5 endurance proof" |
| "Seraph has broad reach" | "Seraph has selective reach foundations and is behind broader channel agents on raw reach" |
| "Seraph is fully benchmark-proof" | "Seraph has benchmark-proof foundations; every major claim still needs a named proof row" |

## Completion Gate

This ledger is M0-complete when:

- each high-risk strategic claim maps to one row above or a narrower future row
- [17. Seraph World-Class Strategy](./17-seraph-world-class-strategy.md) points here before using world-class or superiority language
- `docs/implementation/11-world-class-strategy-delivery.md` points here before shipped strategic claims
- [18. Agent Competition Truth Table](./18-agent-competition-truth-table.md) remains the competitor-truth input rather than the claim wording owner
- reviewers can reject unsupported claims by citing claim ID, status, allowed wording, and proof path
