---
title: 16. Agent Parity Execution Roadmap
---

# 16. Agent Parity Execution Roadmap

## Status On `develop`

- [ ] The full agent parity and targeted exceedance goal from [20. Seraph Agent Parity And Exceedance Goals](/research/seraph-agent-parity-and-exceedance-goals) is not complete on `develop`.

## Paired Research

- full goal document: [20. Seraph Agent Parity And Exceedance Goals](/research/seraph-agent-parity-and-exceedance-goals)
- competitor truth and source discipline: [18. Agent Competition Truth Table](/research/agent-competition-truth-table)
- claim gate: [19. Strategy Claim Ledger](/research/strategy-claim-ledger)
- world-class strategy: [17. Seraph World-Class Strategy](/research/seraph-world-class-strategy)
- superiority program: [11. Superiority Program](/research/superiority-program)

## Purpose

This file is the implementation-side roadmap for research document 20.

It answers how Seraph should implement the full parity and targeted exceedance goal without duplicating the closed M0-M9 milestone foundation, creating a second live queue, or drifting away from the guardian-workspace vision.

Active execution state remains in the GitHub Project, issues, and PRs. This roadmap owns durable sequencing, dependency logic, proof gates, and claim boundaries.

## Execution Boundary

Research document 20 is the competitor-pressure overlay. This roadmap is the implementation sequencing layer. The GitHub Project is the execution layer.

- Do not recreate M0-M9 milestone issues; those are foundation anchors.
- Do not create duplicate issues for the current live proof anchors: [#438](https://github.com/seraph-quest/seraph/issues/438), [#439](https://github.com/seraph-quest/seraph/issues/439), [#440](https://github.com/seraph-quest/seraph/issues/440), [#441](https://github.com/seraph-quest/seraph/issues/441), and [#467](https://github.com/seraph-quest/seraph/issues/467).
- Do not parent new work under closed historical batch [#422](https://github.com/seraph-quest/seraph/issues/422).
- Do not create an `ironclaw-*` import family. IronClaw pressure is implemented as secure-capability-host proof.
- Do not treat raw channel count, plugin count, provider count, or voice/media presence as parity.
- Do not claim `secure`, `private`, `production-ready`, `superior`, `best`, `fully at parity`, or `IronClaw-class` unless [19. Strategy Claim Ledger](/research/strategy-claim-ledger) allows that exact wording.

## Board Receipts

Verified on June 9, 2026 through GitHub Project GraphQL reads.

| Issue | Role | Project item | Queue | Lane | Priority | Size | Status | PR | Code Review |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| [#468 Batch BQ: agent parity proof execution train](https://github.com/seraph-quest/seraph/issues/468) | current parent hub for the proof train | `PVTI_lADOD4qAvs4BS6n3zgvLeHs` | Now | Docs / Meta | P0 | L | In Progress | Open | Pending |
| [#467 P1: Guardian-safe multimodal and voice proof](https://github.com/seraph-quest/seraph/issues/467) | active multimodal/voice proof-governance anchor | `PVTI_lADOD4qAvs4BS6n3zgvLV0A` | Now | Presence and Reach | P1 | M | In Progress | Not Ready | Not Ready |
| [#470 Batch BR: durable workflow state after endurance canary](https://github.com/seraph-quest/seraph/issues/470) | active durable workflow state follow-on | `PVTI_lADOD4qAvs4BS6n3zgvMBK8` | Now | Runtime Reliability | P1 | L | In Progress | Not Ready | Not Ready |
| [#471 Batch BS: guardian arbitration and live-learning receipts](https://github.com/seraph-quest/seraph/issues/471) | blocked guardian arbitration follow-on | `PVTI_lADOD4qAvs4BS6n3zgvMBLE` | Blocked | Guardian Intelligence | P1 | L | Todo | Not Ready | Not Ready |
| [#472 Batch BT: governed capability-pack hardening receipts](https://github.com/seraph-quest/seraph/issues/472) | blocked ecosystem hardening follow-on | `PVTI_lADOD4qAvs4BS6n3zgvMBLI` | Blocked | Ecosystem and Leverage | P1 | L | Todo | Not Ready | Not Ready |

The current parent hub is [#468](https://github.com/seraph-quest/seraph/issues/468). It coordinates the live proof train without splitting it into micro-issues:

- [#457](https://github.com/seraph-quest/seraph/issues/457) / [PR #458](https://github.com/seraph-quest/seraph/pull/458): live long-horizon replay and proof substrate
- [#439](https://github.com/seraph-quest/seraph/issues/439) / [PR #459](https://github.com/seraph-quest/seraph/pull/459): cockpit operator efficiency benchmark
- [#441](https://github.com/seraph-quest/seraph/issues/441) / [PR #460](https://github.com/seraph-quest/seraph/pull/460): memory provider quality gate
- [#440](https://github.com/seraph-quest/seraph/issues/440) / [PR #461](https://github.com/seraph-quest/seraph/pull/461): workflow endurance canary
- [#438](https://github.com/seraph-quest/seraph/issues/438) / [PR #462](https://github.com/seraph-quest/seraph/pull/462): one excellent reach channel canary
- [#467](https://github.com/seraph-quest/seraph/issues/467): guardian-safe multimodal and voice proof gate

Blocked roadmap follow-ons are tracked separately so the board can represent the full goal without marking future work active:

- [#470](https://github.com/seraph-quest/seraph/issues/470): durable workflow state after endurance canary
- [#471](https://github.com/seraph-quest/seraph/issues/471): guardian arbitration and live-learning receipts
- [#472](https://github.com/seraph-quest/seraph/issues/472): governed capability-pack hardening receipts

## Roadmap Phases

| Phase | Batch | Implementation goal | Proof gate | Dependency rule | Issue anchors |
| ---: | --- | --- | --- | --- | --- |
| 0 | Board, proof, and claim normalization | Keep execution state, parent linkage, review state, proof gates, and claim wording current before new readiness claims. | Project receipt plus claim-ledger review. | Must precede any new public parity or targeted exceedance wording. | [#468](https://github.com/seraph-quest/seraph/issues/468), [#436](https://github.com/seraph-quest/seraph/issues/436) |
| 1 | Secure execution host v1 | Privileged paths fail closed across browser credentials, cookies, workspace escape, providers, connectors, delegation, shell, workflow replay, filesystem, background process, and secret egress. | `secure_capability_host` plus `trust_boundary_and_safety_receipts`, with cockpit-readable adversarial receipts. | Must precede high-risk reach, marketplace expansion, and stronger browser/computer-use claims. | historical [#428](https://github.com/seraph-quest/seraph/issues/428), [#435](https://github.com/seraph-quest/seraph/issues/435), [#455](https://github.com/seraph-quest/seraph/issues/455) |
| 2 | Live long-horizon eval replay v1 | Add live-ish fake providers, replay fixtures, failure taxonomy, CI receipts, and operator evidence across memory, workflow, reach, security, cockpit, and intervention paths. | Cross-surface replay harness with deterministic and live-ish receipts. | Feeds every later quality claim; no broad parity claim should skip it. | [#457](https://github.com/seraph-quest/seraph/issues/457), [PR #458](https://github.com/seraph-quest/seraph/pull/458) |
| 3 | Cockpit operator efficiency benchmark | Measure inspect, approve, deny, pause, resume, retry, repair, branch, compare, revoke, audit, confidence, and error detectability on real operator tasks. | M7 benchmark over workflow, memory, trust, and reach scenarios, not synthetic UI density alone. | Needs replay receipts so the same operator tasks can be re-run and compared. | [#439](https://github.com/seraph-quest/seraph/issues/439), [PR #459](https://github.com/seraph-quest/seraph/pull/459) |
| 4 | Memory provider quality gate | External memory evidence declares provenance, confidence, privacy boundary, freshness, conflict behavior, usefulness, suppression, and canonical writeback rules. | `guardian_memory_quality` plus provider-quality receipts showing behavior change, suppression, or refusal. | Must land before memory-provider breadth is treated as a guardian advantage. | [#441](https://github.com/seraph-quest/seraph/issues/441), [PR #460](https://github.com/seraph-quest/seraph/pull/460) |
| 5 | Workflow endurance canary | Multi-session work proves checkpoint, branch, interruption, delegated ownership, failure injection, recovery, artifact comparison, approval preservation, trust-boundary drift blocking, and audit trail. | `workflow_endurance_and_repair` plus `live_workflow_endurance_canary` with cockpit-visible final audit trail. | Can prove endurance canary behavior, but must not claim a durable workflow engine yet. | [#440](https://github.com/seraph-quest/seraph/issues/440), [PR #461](https://github.com/seraph-quest/seraph/pull/461) |
| 6 | Durable workflow engine v1 | Move workflow state beyond audit-projected receipts into minimal durable state: crash-safe resume, heartbeat or reactive trigger receipts, retry, repair, persisted snapshots, durable audit receipts, and delegated artifact review lifecycle. | Durable-state tests plus operator recovery receipts across crash, resume, failed-step, and delegated-artifact paths. | Active after the workflow endurance canary; claim stays bounded to a minimal state kernel. | [#470](https://github.com/seraph-quest/seraph/issues/470) |
| 7 | One excellent reach channel canary | One selected external channel proves pairing, revocation, health, retry, thread continuity, memory/context continuity, approval handoff, degraded UI, and external-message-to-audited-action flow. | M4 reach proof plus Activity Ledger and cockpit receipts. | Should follow security, replay, memory-quality, and workflow-endurance proof unless the Project explicitly promotes it. | [#438](https://github.com/seraph-quest/seraph/issues/438), [PR #462](https://github.com/seraph-quest/seraph/pull/462) |
| 8 | Guardian world-model learning quality v2 | Deepen stale and conflicting evidence arbitration, salience/confidence calibration, false-positive and false-negative accounting, restraint, and follow-through. | Live-ish intervention replay with act, defer, bundle, clarify, approval, and stay-silent outcomes. | Depends on replay substrate and memory-quality proof; reach scenarios should only join after continuity is proven. | [#471](https://github.com/seraph-quest/seraph/issues/471) |
| 9 | Governed extension marketplace hardening | Mature pack review, verification, compatibility semantics, supply-chain policy, provider trust downgrade handling, rollback posture, and authoring ergonomics. | `m9_governed_ecosystem` extension-review, downgrade, compatibility, and rollback receipts. | Depends on secure execution and M1/M9 manifest contracts; do not claim production marketplace security. | [#472](https://github.com/seraph-quest/seraph/issues/472) |
| 10 | Guardian-safe multimodal and voice proof | Voice, TTS/STT, browser vision, image/media analysis, and media delivery exist only as governed capability families with owner, trust, permission, data-access, audit, privacy, continuity, correction/deletion, and revocation receipts. | `guardian_safe_multimodal_voice` plus `/api/operator/guardian-safe-multimodal-voice`, showing channel safety plus actual guardian value. | Active as a proof-gate slice; does not claim live broad voice/media runtime, voice parity, or multimodal parity. | [#467](https://github.com/seraph-quest/seraph/issues/467) |

## Feature Traceability

| Research feature area | Canonical implementation phase | Guardian value gate | Blocked claim if failing |
| --- | --- | --- | --- |
| Capability kernel | Phases 0, 1, 9 | Every capability must expose source, owner, trust level, permission, health, dependency, recovery, and operator receipt before memory or guardian policy can rely on it. | capability parity, governed ecosystem depth |
| Secure capability host | Phase 1 | The cockpit must explain both the mechanical boundary and the guardian reason for restraint, approval, refusal, or clarification. | secure execution, IronClaw-class security, production security |
| Guardian memory | Phase 4 | Memory must change action choice, timing, channel, restraint, clarification, recovery, or follow-through, not merely increase stored context. | memory superiority, behavior-changing recall |
| Intervention intelligence | Phase 8 | Better intervention means fewer bad interventions, clearer deferrals, stronger clarification, safer approvals, and grounded stay-silent decisions. | intervention superiority, guardian brain advantage |
| Workflow endurance | Phases 5 and 6 | Long work must stay legible across interruptions, branches, failures, approvals, artifacts, and delegated ownership. | workflow superiority, durable workflow readiness |
| Operator cockpit | Phase 3 | The operator must move from "what happened?" to "what should I do next?" through receipts and controls, not source diving. | cockpit efficiency, operator superiority |
| Selective reach | Phase 7 | Reach must preserve memory, thread identity, approvals, audit, and recovery across surfaces while rejecting channel sprawl. | OpenClaw-style reach parity, always-available operation |
| Browser/computer use | Phases 1, 2, 7 | Browser work must carry session partitioning, credential boundaries, replay receipts, and workflow/artifact continuity. | browser/computer-use parity, safe browser automation |
| Ecosystem and marketplace | Phase 9 | Extensions must remain subordinate to Seraph's trust, review, compatibility, rollback, and guardian-governance model. | marketplace maturity, governed ecosystem superiority |
| Multimodal and voice | Phase 10 | Voice/media must improve timing, accessibility, situational awareness, or intervention quality, and must show transcript/audit/privacy receipts. | multimodal parity, voice parity, guardian-safe voice |

## Proof Template

Every batch that claims progress against research document 20 should include:

- proof suite: deterministic test, benchmark, replay, or canary name
- receipt surface: cockpit, Activity Ledger, operator API, audit payload, PR artifact, or issue comment
- negative cases: prompt injection, stale evidence, credential drift, replay drift, provider downgrade, channel loss, revocation, ambiguity, or unsafe memory
- operator-visible pass criteria: what a user can inspect without reading code
- blocked claims: the words or claims that remain disallowed if the proof fails
- linked issue or PR: the GitHub object that owns acceptance and review

## Strategic Order

The durable order is:

1. Normalize board, proof, and claim gates.
2. Harden secure execution host boundaries.
3. Land live long-horizon replay proof.
4. Measure cockpit operator efficiency.
5. Gate external memory-provider quality.
6. Prove workflow endurance, then promote durable workflow state.
7. Prove one excellent reach channel.
8. Deepen guardian world-model learning quality.
9. Harden governed ecosystem and marketplace foundations.
10. Add guardian-safe multimodal and voice only after reach, trust, and guardian-value proof are ready.

This order intentionally differs from raw competitor feature order. Seraph should expand capability breadth only when the guardian, trust, memory, workflow, and receipt layers can carry it.

## Critic / Contrarian Disposition

Accepted:

- Research document 20 should remain a competitor-pressure overlay; implementation order belongs here and live execution belongs in the Project.
- The roadmap must not duplicate closed M0-M9 anchors or the active #438/#439/#440/#441/#467 proof issues.
- Closed [#422](https://github.com/seraph-quest/seraph/issues/422) must not be used as the parent for new work.
- Selective reach must not outrun secure execution, replay proof, cockpit legibility, memory quality, and workflow endurance unless the Project deliberately promotes it.
- Board receipt language must be backed by live Project reads; this doc includes only verified Project fields for #468 and #467.
- Feature rows need guardian value gates, not only parity surfaces.

Rejected:

- The roadmap should not remove implementation sequencing entirely. The user explicitly asked for a roadmap to implement the full research document, so this file records durable sequencing while keeping active task state in GitHub.

Accepted after follow-up:

- The remaining roadmap-level follow-ons were created as Project items. [#470](https://github.com/seraph-quest/seraph/issues/470) is active after the workflow-endurance canary; [#471](https://github.com/seraph-quest/seraph/issues/471) and [#472](https://github.com/seraph-quest/seraph/issues/472) remain blocked until their prerequisite proof batches land.
- A duplicate secure execution host v1 ticket was not created because closed [#455](https://github.com/seraph-quest/seraph/issues/455) already owns that batch; future secure-host work needs a narrowed follow-up with named uncovered negative cases.
