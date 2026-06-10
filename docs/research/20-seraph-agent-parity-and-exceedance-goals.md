---
title: 20. Seraph Agent Parity And Exceedance Goals
---

# 20. Seraph Agent Parity And Exceedance Goals

## Purpose

Set the high-level feature target for bringing Seraph to parity with Hermes, OpenClaw, and IronClaw, then exceeding them without abandoning Seraph's guardian-workspace vision.

This document is a strategic goal document, not a live queue. Active work still belongs in the GitHub Project, issues, and PRs.

## Evidence And Review Posture

Snapshot date: June 9, 2026.

Internal source trail:

- [Development Status](/status)
- [Seraph Master Roadmap](/)
- [10. Competitive Benchmark](./10-competitive-benchmark.md)
- [11. Superiority Program](./11-superiority-program.md)
- [15. Reference Systems Refresh 2026-04](./15-reference-systems-refresh-2026-04.md)
- [16. Seraph State And Roadmap Corrections 2026-04](./16-seraph-state-and-roadmap-corrections-2026-04.md)
- [18. Agent Competition Truth Table](./18-agent-competition-truth-table.md)
- [19. Strategy Claim Ledger](./19-strategy-claim-ledger.md)

External sources checked for this refresh:

- [Hermes features overview](https://hermes-agent.nousresearch.com/docs/user-guide/features/overview/)
- [Hermes tools and toolsets](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools/)
- [OpenClaw overview](https://docs.openclaw.ai/)
- [OpenClaw Control UI](https://docs.openclaw.ai/web/control-ui)
- [OpenClaw browser](https://docs.openclaw.ai/tools/browser)
- [OpenClaw plugins](https://docs.openclaw.ai/tools/plugin)
- [IronClaw official site](https://www.ironclaw.com/)
- [IronClaw GitHub repository](https://github.com/nearai/ironclaw)
- [IronClaw feature parity matrix](https://github.com/nearai/ironclaw/blob/staging/FEATURE_PARITY.md)

Source note:

- [X post 2063645563241844823](https://x.com/IBuzovskyi/status/2063645563241844823) is used with access caveats. X oEmbed shows a June 7, 2026 YanXbt post with only `https://t.co/nmM5JJuuqI`; that short URL resolves to [X article 2063630561466265600](https://x.com/i/article/2063630561466265600), which returned X's unauthenticated "Nothing to see here" page during this review. The article body was retrieved through [fxtwitter API access](https://api.fxtwitter.com/IBuzovskyi/status/2063645563241844823) on June 9, 2026, but the retrieved payload is not archived in this repository. This document uses only conservative article claims and treats the article's benchmark claims as unverified unless an independent benchmark artifact is provided.

## Current Source Refresh

Primary-source refresh completed on June 9, 2026:

- Hermes still presents a broad agent platform rather than a narrow chat agent: the current feature overview lists toolsets, skills, persistent memory, checkpoints, scheduled tasks, subagent delegation, code execution, voice, browser automation, multimodal image/vision support, MCP, provider routing, fallback providers, credential pools, memory providers, API server, IDE integration, and plugins.
- Hermes' tools page still supports the parity pressure from broad built-in tools: web search/extract, terminal/process/file tools, browser text/vision, multimodal media tools, `todo`, `clarify`, `execute_code`, `delegate_task`, `memory`, `session_search`, `cronjob`, `send_message`, MCP tools, and container/SSH/cloud execution backends.
- OpenClaw still presents the raw reach/control-plane benchmark: the current overview frames OpenClaw as an any-OS gateway across Discord, Google Chat, iMessage, Matrix, Microsoft Teams, Signal, Slack, Telegram, WhatsApp, Zalo, WebChat, and mobile nodes, with the Gateway as source of truth for sessions, routing, and channel connections.
- OpenClaw's current Control UI docs show a real operator surface with pairing, browser-local identity, runtime config endpoint, chat/history behavior, PWA/web push, auth/device-identity constraints, content security policy, authenticated avatar/media routes, and debugging/testing flows.
- OpenClaw's browser docs still support the browser-mode pressure: OpenClaw documents openclaw-managed profiles, remote CDP profiles, and existing-session attachment through Chrome DevTools MCP, with remote CDP token handling called out as a secret-bearing path.
- OpenClaw's plugin docs still support ecosystem breadth pressure: plugins can extend channels, model providers, agent harnesses, tools, skills, speech, realtime transcription, voice, media understanding/generation, web fetch, web search, and other runtime capabilities.
- IronClaw's current official site still supports treating IronClaw as the secure-capability-host pressure point: it advertises encrypted vaults, host-boundary credential injection only for approved endpoints, per-tool Wasm containers with capability-based permissions and strict resource limits, TEE deployment on NEAR AI Cloud, leak detection, Rust implementation, and network allowlisting.
- IronClaw's current GitHub README and feature parity matrix still support treating IronClaw as a real runtime competitor, not only a security wrapper: the repository frames IronClaw as a secure personal AI assistant, and the parity matrix lists WebSocket control plane, Control UI endpoints, web dashboard with chat/memory/jobs/logs/extensions, channels, session management, HTTP API, diagnostics, TUI, webhooks, and WASM channels.

Refresh disposition:

- The existing Hermes/OpenClaw/IronClaw pressure summaries remain valid.
- No new public superiority or parity claim is introduced here.
- Future public wording still needs a fresh source check because these systems are active and source-stable claims can age quickly.

Claim boundary:

- This document may say Seraph has a scoped advantage where repo evidence supports it.
- It must not say Seraph is broadly superior, best, secure, production-ready, or fully at parity.
- Stronger claims must pass the claim rules in [19. Strategy Claim Ledger](./19-strategy-claim-ledger.md).

## Post-PR 473 Reconciliation

Verified on June 9, 2026: [PR #473](https://github.com/seraph-quest/seraph/pull/473) merged the aggregate agent-parity proof train into `develop`.

That merge changes the claim posture in four separate ways:

- Strategy artifact completion: this document is complete as the strategic parity/exceedance target. It maps competitor pressure to feature areas, proof gates, issue anchors, and claim boundaries.
- Proof-train completion: the current aggregate proof train is complete. It landed deterministic receipts for replay, cockpit efficiency, memory-provider quality, workflow endurance, one-channel reach, minimal durable workflow state, guardian arbitration, governed capability-pack hardening, and guardian-safe multimodal/voice proof.
- Parity floors reached: Seraph has reached the named proof floors that train was scoped to prove. Those are evidence-backed floor claims, not broad product parity claims.
- Remaining gaps: full parity and targeted exceedance are still not complete. The real remaining gaps are production-grade external orchestration guarantees beyond recorded-live crash studies, deeper secure execution isolation and privileged-path hardening beyond deterministic and incident-drill receipts, broader and harder live reach, generalized live intervention outcome superiority beyond bounded independent/task-scoped receipts, production-secure marketplace and solved third-party package-security claims beyond Batch CO bounded package-security/package-network receipts, blanket safe browser automation and full browser parity wording beyond Batch CP bounded browser/computer-use safety receipts, and final parity/exceedance verification.

Allowed short wording after PR #473: "The strategy artifact and aggregate proof train are complete; named parity proof floors are reached; full production-grade parity/exceedance remains open."

Forbidden short wording after PR #473: "Seraph is fully at parity", "Seraph is production-ready", "Seraph has IronClaw-class secure execution", or "Seraph has solved durable workflows/reach/operator control."

## Seraph Vision Boundary

Seraph should not become a generic gateway, plugin farm, or terminal-only coding shell.

The enduring product shape is:

- a workspace-first guardian that remembers, watches, and acts
- capability-first, but governed by memory, trust, supervision, and restraint
- proactive, but measured by intervention quality rather than notification volume
- extensible, but with capability contracts, manifests, trust levels, lifecycle gates, and operator-visible receipts
- broad enough to act across the user's work surfaces, but selective about reach so every new channel improves continuity or safety

## Competitor Pressure Summary

Seraph has already completed a five-wave Hermes/OpenClaw capability import program through core runtime primitives, packaged reach surfaces, selective browser/channel/automation imports, operator-surface governance, and deterministic proof. PR [#473](https://github.com/seraph-quest/seraph/pull/473) then landed the aggregate parity proof train for the current feature set.

The remaining question is no longer "did Seraph import those ideas at all?" or "does Seraph have proof anchors for the named parity floors?" The sharper question is whether those proof-backed surfaces are production-grade, broad enough, live enough, and guardian-integrated enough to stand beside the strongest reviewed public agent platforms. Today the honest answer is mixed: Seraph has deterministic parity-floor proof across the target areas, but full product parity and targeted exceedance remain open.

IronClaw is different. The repo treats IronClaw as a benchmark and security gauntlet, not as an imported package family. There is no equivalent `seraph.ironclaw-*` catalog family today, so IronClaw parity should be planned as a secure-capability-host program rather than a naming or packaging exercise.

### Hermes

Hermes now pressures Seraph on broad operator runtime capability. Current Hermes docs describe toolsets, persistent memory, skills, checkpoints, cron jobs, subagent delegation, code execution, voice, browser automation, MCP, and provider routing.

Main pressure on Seraph:

- match tool/runtime breadth
- improve skill growth and install ergonomics
- keep memory provider extensibility first-class
- strengthen scheduled/background work and subagent workflows
- improve dense CLI or command ergonomics without losing the cockpit

### OpenClaw

OpenClaw pressures Seraph on channel and control-plane breadth. Current OpenClaw docs frame it as a self-hosted gateway across many chat apps, WebChat, mobile nodes, a web control UI, multi-agent routing, plugins, skills, automation, browser modes, and node/device surfaces.

Main pressure on Seraph:

- match practical channel reach where it matters
- deepen cockpit/control-plane density
- improve plugin and package breadth without copying unsafe trust assumptions
- expand browser, node, webhook, poll, pub/sub, and automation surfaces
- improve multi-agent and workflow runtime ergonomics

### IronClaw

IronClaw pressures Seraph on security posture. Current IronClaw materials emphasize TEE/CVM deployment on NEAR AI Cloud, encrypted vaults, per-tool Wasm isolation, endpoint allowlists, leak detection, Rust implementation, local deployment, and a real runtime surface.

Main pressure on Seraph:

- narrow privileged execution seams
- add stronger host/tool isolation
- make secret handling architecturally enforced, not only policy-enforced
- add endpoint allowlists and network egress receipts
- prove trust boundaries with repeatable security-path benchmarks

## Seraph's Scoped Differentiators Today

### Guardian-shaped product intent

Seraph has a clearer guardian-specific product thesis than the reviewed competitor materials. The repo frames Seraph as a system that remembers, watches, and acts, with a power-user guardian workspace rather than a generic gateway or chat-only assistant shell.

This is a scoped differentiator, not a broad superiority claim. It only matters if guardian state changes real decisions.

### Policy-time guardian memory and intervention scaffolding

Seraph's strongest current differentiator is not just memory storage. It is the connection between structured memory, world-model synthesis, observer signals, intervention policy, feedback, and restraint.

Seraph should preserve this as the differentiator while matching competitor breadth. The win condition is not "more memory"; it is memory that changes when Seraph acts, defers, bundles, asks approval, asks for clarification, or stays silent.

### Benchmark and claim discipline

Seraph has unusually explicit internal claim discipline: benchmark docs, implementation mirrors, named proof surfaces, and a strategy claim ledger. This creates a strong foundation for honest progress, but it does not prove category superiority.

That foundation should become operator-visible product quality: every major action, failure, recovery, memory decision, and safety boundary needs a receipt the user can inspect.

### Governed extension shape

Seraph's bundled Hermes/OpenClaw-inspired catalog extensions already show the right import style: capability packs, connector packs, permissions, execution boundaries, data access, mutation rights, audit events, and trust metadata.

The differentiator is not raw package count. It is an ecosystem shape where extension power is subordinate to Seraph's guardian and trust model.

## Where Seraph Needs Improvement

### Raw execution and tool breadth

Hermes still sets a high floor for built-in operator tools, toolsets, code execution, delegation, cron, browser automation, messaging delivery, and provider routing.

Seraph now has capability inventory, runtime primitives, packaged surfaces, and deterministic proof receipts, but it must keep broadening the live runtime surface. Every new tool must declare ownership, permissions, trust boundaries, audit behavior, and recovery states.

### Channel reach and always-available operation

OpenClaw and Hermes are ahead on raw messaging/channel reach. Seraph now has a native-notification reach canary with pairing, revocation, retry, continuity, approval handoff, degraded-state UI, and audit receipts, but not yet broad daily-life availability across the user's real work and communication surfaces.

Seraph should not chase every channel equally. It should first make one or two reach channels excellent, safe, continuous, and guardian-aware.

### IronClaw-class execution isolation

Seraph has trust foundations and deterministic secure-host choke-point proof, but IronClaw raises the bar for architectural enforcement: per-tool Wasm or container isolation, encrypted credential handling, endpoint allowlists, leak detection, and TEE/CVM-style deployment options.

This is the sharpest gap because Seraph's guardian value depends on being trusted with sensitive context and real actions.

### Workflow endurance and operator control

Seraph ships meaningful workflow foundations, branch-family supervision, activity ledgers, checkpoints, recovery controls, a live endurance canary, and a minimal durable workflow state kernel. The gap is now production-grade endurance: long-running, multi-session, multi-agent, failure-repair work that remains easy to inspect and steer under real interruption, scheduler, concurrency, and crash conditions.

The next layer should make Seraph feel calmer under long work, not merely more powerful.

### Ecosystem maturity and installation ergonomics

Seraph's extension governance is promising and now has local governed-ecosystem plus pack-hardening receipts, but Hermes and OpenClaw pressure it on discoverability, install flows, skills, plugin inventories, third-party package maturity, and practical package breadth.

Seraph needs a marketplace-quality local flow before it can credibly scale third-party capability.

### Browser and computer-use reliability

Hermes and OpenClaw both document richer browser backends or browser-control modes than Seraph's current practical surface. Seraph now has replayable browser/desktop receipts, browser-provider packaging, and computer-use benchmark coverage, but still needs live remote/managed reliability, deeper website/task breadth, login/session partitioning proof, and failure recovery under real browser-state drift before computer-use claims become strong.

### Voice, media, and multimodal operation

Hermes and OpenClaw now make voice, media, image, and browser-vision surfaces part of the reviewed platform pressure. Seraph now has guardian-safe voice/media governance proof and packaged multimodal review foundations, but still lacks live broad voice runtime, production STT/TTS, and full multimodal parity.

## Goal Feature Set

The goal is to reach parity floors first, then exceed through guardian-specific integration.

| Feature area | Parity floor | Exceedance target | Current proof posture |
| --- | --- | --- | --- |
| Capability kernel | Toolsets, skills, workflows, MCP, browser, files, shell, cron, delegation, code execution, messaging, media, and provider routing are inventoried with clear enablement. | Every capability is guardian-governed: memory-aware, trust-scoped, policy-routed, auditable, recoverable, and visible in the cockpit. | Deterministic inventory and governance proof exists; live breadth and ecosystem maturity remain gated by M1/M2/M9 receipts. |
| Secure capability host | Tools and connectors have stronger isolation, secret handling, endpoint allowlists, network egress controls, and replay/resume boundary checks. | Seraph exceeds by combining isolation with guardian restraint, operator receipts, and automatic refusal or clarification when context is unsafe. | Deterministic secure-host receipts exist; Batch BW adds `production_secure_host_hardening`, `secure_capability_host_live_isolation_v2`, and `/api/operator/secure-capability-host-hardening` receipts for privileged-path hardening, while IronClaw-class and production-security wording remain blocked pending stronger architectural isolation proof. |
| Guardian memory | Canonical memory plus additive provider adapters reach parity with external memory-provider ecosystems. | Memory changes behavior: action choice, timing, channel, restraint, clarification, recovery, and follow-through improve from grounded recall. | Deterministic M6 and provider-quality proofs exist; Batch CM adds dimension-scoped `memory_provider_parity_matrix` receipts, while full memory-provider parity and memory superiority remain blocked. |
| Intervention intelligence | Proactive delivery uses salience, confidence, interruption cost, feedback, and user-model signals. | Seraph exceeds by acting less but with higher intervention quality: timely, restrained, context-aware interventions that learn from outcomes. | Deterministic act/defer/bundle/clarify/approval/stay-silent receipts exist; Batch CF adds recorded-live causal receipts and Batch CM adds independent cohort plus task-scoped causal receipts, while solved learning and generalized live-human-outcome superiority remain blocked. |
| Workflow endurance | Long-running workflows support steps, artifacts, checkpoints, branch/resume, repair, comparison, and background continuation. | Seraph exceeds by making long work legible: anticipatory repair, backup branches, cross-session continuity, operator handoff, and guardian-aware follow-through. | Endurance canary and minimal durable-state kernel exist; production long-running workflow-engine parity remains open. |
| Operator cockpit | Cockpit exposes capabilities, workflows, approvals, artifacts, activity, routing, spend, failures, recovery, and guardian state. | Seraph exceeds through calm dense supervision: fastest path from "what happened?" to "what should I do next?" | Scripted cockpit legibility and efficiency proof exists; Batch CH adds bounded recorded-live multi-operator usability receipts, but best/world-class cockpit or solved-control claims remain unclaimed. |
| Selective reach | At least one non-browser channel and one native/browser computer-use lane are excellent, not merely configured. | Reach is guardian-aware: continuity, approvals, memory, thread identity, and action safety survive channel shifts. | Native-notification canary and browser/native continuity proof exist; broad OpenClaw-style channel reach remains open. |
| Browser/computer use | Local browser, remote CDP, managed browser, session replay, snapshot, vision, and failure recovery are available behind clear trust boundaries. | Seraph exceeds by linking browser work to guardian state, workflow artifacts, approval scopes, and safe credential/session partitions. | Replayable browser/desktop receipts and provider packaging exist; Batch CH adds bounded local/managed/remote provider attestation, session/credential/download/upload boundaries, and recovery drills; Batch CP adds bounded `live_browser_task_depth`, `autonomous_browser_safety_controls`, `browser_session_partitioning_security`, `site_specific_recovery_drills`, `browser_provider_reliability_matrix`, `independent_browser_usability_review`, and `/api/operator/safe-autonomous-browser-computer-use` receipts, while blanket safe browser automation and full browser parity wording remain claim-ledger gated. |
| Ecosystem and marketplace | Skills, packs, connectors, runbooks, starter packs, and workflow runtimes have install/update/disable/diagnostic flows. | Seraph exceeds with governed evolution: compatibility checks, trust tiers, review gates, canary rollout, rollback receipts, semantic-drift prevention, independent package-security receipts, hostile package drills, package-network incident receipts, publisher/vulnerability freshness evidence, and rollback/quarantine diagnostics. | Local governed-ecosystem, pack-hardening, lifecycle, recorded-live attestation, and Batch CO bounded package-security/package-network proof exist; production-secure marketplace, solved third-party package security, full marketplace parity, and ecosystem superiority remain blocked. |
| Multimodal and voice | Voice, TTS/STT, browser vision, image/media analysis, and media delivery exist as governed capability families. | Seraph exceeds by using voice/media only where they improve guardian timing, accessibility, or situational awareness. | Guardian-safe voice/media governance proof exists; live voice and full multimodal runtime parity remain open. |

## Execution Mapping

This document does not create a live queue. It maps the parity floors to existing milestone and proof anchors so future execution can move without another strategic translation pass.

| Feature area | Milestone and issue scope | Named proof path |
| --- | --- | --- |
| Capability kernel | `M1` capability contract [#425](https://github.com/seraph-quest/seraph/issues/425), `M2` execution surface [#427](https://github.com/seraph-quest/seraph/issues/427), and `M9` governed ecosystem [#432](https://github.com/seraph-quest/seraph/issues/432). | Capability inventory and cockpit capability panes must show source, owner, trust level, provenance, permissions, boundary, health, dependencies, actions, and receipts before a capability counts as parity-ready. |
| Secure capability host | `M3` secure capability host [#428](https://github.com/seraph-quest/seraph/issues/428), IronClaw-class security gauntlet [#435](https://github.com/seraph-quest/seraph/issues/435), and Batch BW secure-host hardening [#477](https://github.com/seraph-quest/seraph/issues/477). | `trust_boundary_and_safety_receipts`, `secure_capability_host`, `production_secure_host_hardening`, and `secure_capability_host_live_isolation_v2` suites plus `/api/operator/secure-capability-host-hardening` must cover secret exfiltration, credential boundary leaks, SSRF/private IP access, shell injection, plugin permission creep, replay approval drift, prompt injection through external content, delegated/background execution privilege drift, allow/deny/recover receipts, and operator-readable claim boundaries. |
| Guardian memory | `M6` memory superiority and behavior-changing recall [#433](https://github.com/seraph-quest/seraph/issues/433), memory-provider quality gate [#441](https://github.com/seraph-quest/seraph/issues/441), and Batch CM memory-provider parity matrix [#507](https://github.com/seraph-quest/seraph/issues/507). | `guardian_memory_quality`, provider-quality proofs, and `memory_provider_parity_matrix` receipts must show long-horizon recall, contradiction handling, stale-memory override, source trust/privacy boundaries, provider usefulness, suppression of noisy provider evidence, canonical/advisory provider comparison, privacy-regression quarantine, delete/export receipts, and receipts showing memory changed behavior without allowing full provider-parity or memory-superiority claims. |
| Intervention intelligence | `M8` guardian brain [#431](https://github.com/seraph-quest/seraph/issues/431), guardian intervention benchmark [#437](https://github.com/seraph-quest/seraph/issues/437), and Batch CM independent outcome/causal proof [#507](https://github.com/seraph-quest/seraph/issues/507). | M8 intervention-quality scenarios plus `independent_outcome_cohort_review` and `task_scoped_causal_learning` receipts must prove act, defer, bundle, clarify, approval, and stay-silent choices across ambiguous evidence, stale memory, conflicting commitments, interruption cost, channel choice, risky capability use, no-action cases, independent evaluator metadata, sample and power rationale, consent/anonymization, adverse-event review, bounded outcome claims, task-scoped causal attribution, confounder boundaries, and rollback authority. |
| Workflow endurance | `M5` jobs/routines/workflows/delegation [#429](https://github.com/seraph-quest/seraph/issues/429) plus live workflow endurance canary [#440](https://github.com/seraph-quest/seraph/issues/440). | `workflow_endurance_and_repair` and `live_workflow_endurance_canary` proof must cover multi-session work, delegated ownership, checkpoint/branch, failure injection, recovery, artifact comparison, approval preservation, trust-boundary drift blocking, and a final audit trail visible from the cockpit. |
| Operator cockpit | `M7` cockpit legibility [#430](https://github.com/seraph-quest/seraph/issues/430) plus cockpit operator efficiency benchmark [#439](https://github.com/seraph-quest/seraph/issues/439). | M7 cockpit proof must measure inspect, approve, deny, pause, resume, retry, repair, branch, compare, revoke, and audit flows with time, clicks or keystrokes, error detectability, and operator-visible receipts. |
| Selective reach | `M4` channels/presence/device pairing [#426](https://github.com/seraph-quest/seraph/issues/426) plus one excellent reach channel canary [#438](https://github.com/seraph-quest/seraph/issues/438). | M4 reach proof must show pairing, revocation, health, retry, thread continuity, memory/context continuity, audit receipts, approval handoff, degraded-state UI, and one live external-message-to-audited-action flow. |
| Browser/computer use | `M2` execution [#427](https://github.com/seraph-quest/seraph/issues/427), `M3` trust boundaries [#428](https://github.com/seraph-quest/seraph/issues/428), `M4` selective reach [#426](https://github.com/seraph-quest/seraph/issues/426), Batch BY reach/browser/voice hardening [#479](https://github.com/seraph-quest/seraph/issues/479), and Batch CH browser provider/usability proof [#496](https://github.com/seraph-quest/seraph/issues/496). | `computer_use_browser_desktop`, `browser_computer_use_reliability_v2`, `managed_browser_provider_attestation`, `live_multi_operator_usability_study`, and `browser_computer_use_recovery_drill` proof must show replayable browser receipts, provider identity/evidence mode, login/session partitioning, cookie and credential boundary behavior, download/upload boundaries, fail-closed recovery, browser/native continuity, and operator-visible blocked claims. |
| Ecosystem and marketplace | `M9` governed ecosystem [#432](https://github.com/seraph-quest/seraph/issues/432), with capability kernel dependencies on `M1` [#425](https://github.com/seraph-quest/seraph/issues/425). | `m9_governed_ecosystem` proof must show manifest governance, lifecycle review gates, managed-connector degradation truth, marketplace composition, diagnostics/update triage, compatibility checks, rollback posture, and operator receipts. |
| Multimodal and voice | `M4` reach [#426](https://github.com/seraph-quest/seraph/issues/426), `M8` guardian brain [#431](https://github.com/seraph-quest/seraph/issues/431), `M9` governed packs [#432](https://github.com/seraph-quest/seraph/issues/432), and dedicated guardian-safe multimodal/voice proof [#467](https://github.com/seraph-quest/seraph/issues/467). | Multimodal/voice proof must show transcript/audit capture, privacy and channel boundaries, speech/media package governance, user correction or revocation, and a demonstrated guardian-value reason for voice/media use rather than raw feature presence. |

## IronClaw Secure-Capability-Host Plan

IronClaw parity is a secure-capability-host program. It should not become an `ironclaw-*` branding/import wave unless Seraph later needs a compatibility adapter.

### Parity floor

Seraph reaches the IronClaw pressure floor when `M3` plus the IronClaw-class gauntlet prove:

- per-capability isolation strategy for shell, browser, connector, workflow, delegation, background process, filesystem, provider fallback, and extension paths
- secret references that resolve only at declared injection-safe host boundaries
- endpoint allowlists or equivalent egress controls for secret-bearing connectors and browser/tool sessions
- network-egress receipts that explain where data could leave the host
- prompt-injection and hostile-content checks before external content can steer privileged actions
- replay/resume approval drift blocking when the capability boundary changes
- extension permission creep detection and lifecycle gating
- operator-visible receipts for what ran, where, with what data, why it was allowed, and how it failed closed

Batch BW narrows this gap with the `production_secure_host_hardening` and `secure_capability_host_live_isolation_v2` proof gates plus `/api/operator/secure-capability-host-hardening`. It does not prove full host/container isolation, TEE/Wasm/CVM isolation, secure/private-by-default posture, production-ready execution, or IronClaw-class secure execution.

### Exceedance target

Seraph exceeds the IronClaw pressure by combining host-level containment with guardian judgment:

- the guardian state can lower action posture, request clarification, or require approval when memory, observer confidence, source provenance, or user-model evidence makes a capability unsafe
- the cockpit explains both the mechanical boundary and the guardian reason for restraint
- recovery actions preserve memory, workflow, approval, and audit context instead of forcing the operator to reconstruct state
- secure execution is benchmarked as a user-facing trust surface, not only an internal runtime property

### Proof gates

The secure-capability-host program is complete for this strategy document only when:

- [#428](https://github.com/seraph-quest/seraph/issues/428) owns the milestone-level trust-boundary contract
- [#435](https://github.com/seraph-quest/seraph/issues/435) owns the IronClaw-class gauntlet cases
- the named M3 suite emits cockpit-readable safety receipts
- the claim ledger still blocks `secure`, `private`, `production-ready`, or `IronClaw-class secure execution` wording unless those exact paths pass
- any future TEE/Wasm/container work is treated as implementation means, not a substitute for permission, egress, replay, prompt-injection, and operator-receipt proof

## Strategic Delivery Order

1. Normalize the capability kernel and proof ledger.
2. Close secure capability-host gaps before expanding high-risk reach.
3. Make one reach channel and one browser/computer-use lane excellent.
4. Deepen workflow endurance and repair for real multi-session work.
5. Deepen guardian memory and intervention quality over the broadened substrate.
6. Mature ecosystem installation, diagnostics, review, and rollback.
7. Add voice/media only after trust, receipts, and channel continuity are solid.

## Non-goals

Seraph should not:

- copy OpenClaw's gateway-first identity
- copy unrestricted in-process plugin execution as the default extension model
- optimize for raw channel count over guardian continuity
- claim IronClaw-class security until isolation and secret-boundary proofs exist
- claim memory superiority from storage volume or provider count alone
- claim workflow superiority before endurance and repair are proven
- turn docs into a live queue or PR tracker

## Acceptance Standard

This goal document is complete as a strategy artifact when:

- each parity floor maps to an implementation milestone, issue scope, and named proof path
- each exceedance target preserves the guardian-workspace vision
- competitor claims have a current primary-source refresh date and source trail
- IronClaw parity is defined as a secure-capability-host program rather than an imported package family
- the claim ledger permits any stronger language before it is used
- the cockpit remains the receipt target for inspecting what Seraph did, why, with what authority, and how to recover

Post-PR #473 status: this strategy artifact is complete, and the aggregate proof train it spawned is complete. Batches BW-CP add secure-host, durable-orchestration, reach/browser/voice, live-learning/provider-outcome, marketplace-lifecycle, production operator-control/train-verification, recorded-live external-orchestration/crash-study, production-isolation/security-incident, live reach/media, human-outcome/causal-learning, marketplace-attestation, browser-provider attestation, multi-operator usability, browser recovery-drill, final source-backed readiness-audit, bounded production SLA/effectively-once orchestration receipts, bounded independent secure-host review/hostile-drill/recovery-authority receipts, bounded broad-channel SLA/voice-media-quality/mobile-execution continuity receipts, bounded independent outcome/task-scoped causal-learning/memory-provider parity-matrix receipts, bounded dense operator debugging/recovery-control receipts, bounded independent package-security/package-network operation receipts, and bounded browser/computer-use safety receipts to the production parity train, but that does not complete the full product ambition. Batch CO names those marketplace receipts as `independent_package_security_review`, `hostile_ecosystem_package_drills`, `package_network_incident_operations`, `publisher_trust_vulnerability_handling`, and `marketplace_rollback_quarantine_diagnostics` plus `/api/operator/production-marketplace-security`. Batch CP names the browser receipts as `live_browser_task_depth`, `autonomous_browser_safety_controls`, `browser_session_partitioning_security`, `site_specific_recovery_drills`, `browser_provider_reliability_matrix`, and `independent_browser_usability_review` plus `/api/operator/safe-autonomous-browser-computer-use`. Batch CQ is closed by merged PR #521 and completes the final source-backed claim-lift reconciliation for bounded proof-train wording only. The remaining product ambition still includes production-secure marketplace and solved third-party package-security claims beyond Batch CO bounded receipts, unconditional exactly-once/crash-proof workflow guarantees, blanket safe browser automation and full browser parity wording beyond Batch CP bounded redacted receipts, generalized live-human-outcome superiority beyond CM receipts, full always-available reach and voice/media parity beyond Batch CL receipts, hardware-backed or container-grade isolation implementation, external security certification, full parity, production readiness, and production superiority evidence. The post-CQ production-evidence train is tracked under parent issue [#475](https://github.com/seraph-quest/seraph/issues/475) through [#522](https://github.com/seraph-quest/seraph/issues/522)-[#530](https://github.com/seraph-quest/seraph/issues/530).

## Production-Grade Execution Roadmap

The GitHub Project tracks the production-grade parity train through parent issue [#475](https://github.com/seraph-quest/seraph/issues/475), completed huge batch issues [#476](https://github.com/seraph-quest/seraph/issues/476)-[#482](https://github.com/seraph-quest/seraph/issues/482), completed follow-on issues [#491](https://github.com/seraph-quest/seraph/issues/491)-[#497](https://github.com/seraph-quest/seraph/issues/497), completed full-completion residual-gap issues [#505](https://github.com/seraph-quest/seraph/issues/505)-[#512](https://github.com/seraph-quest/seraph/issues/512), and post-CQ production-evidence issues [#522](https://github.com/seraph-quest/seraph/issues/522)-[#530](https://github.com/seraph-quest/seraph/issues/530). That board-backed roadmap is an execution commitment, not evidence that production parity has shipped.

The batch order is:

1. readiness, claim gates, and integration proof harness
2. secure-host architectural isolation and privileged-path hardening
3. production durable orchestration and multi-agent workflow control
4. broad live reach, browser reliability, and voice/media runtime hardening
5. live guardian learning, intervention quality, and memory-provider outcome proof
6. marketplace-grade capability lifecycle, review, rollback, and ecosystem maturity
7. production operator cockpit control and end-to-end parity verification
8. follow-on recorded-live orchestration, isolation, reach/media, human-outcome, marketplace, browser-provider/usability, and final source-backed parity audit proof
9. full-completion residual-gap closure: orchestration SLA/effectively-once recovery, independent security/isolation review, broad reach plus production voice/media/mobile execution, independent guardian-learning outcome proof, dense operator debugging/recovery control, bounded production marketplace-security/package-network receipts, safe autonomous browser/computer-use, and final claim-lift audit
10. post-CQ production-evidence train: shipped-truth reconciliation, continuous orchestration SLOs, container-grade secure-host validation, broad reach and voice/media field operations, longitudinal learning/memory operations, dense operator mission control, marketplace registry/corpus operations, managed browser/computer-use reliability, and final post-evidence claim audit

Batch CI adds the final source-backed parity readiness audit surface through `final_source_backed_parity_audit`, `final_claim_ledger_reconciliation`, `operator_final_parity_readiness_report`, and `/api/operator/final-parity-readiness-report`. It reconciles current Hermes/OpenClaw/IronClaw source receipts, production-train PR and Project evidence, claim-ledger boundaries, residual gaps, and Critic/Contrarian dispositions. It does not permit full parity, production-ready, superiority, OpenClaw-class reach, IronClaw-class secure execution, safe browser automation, full browser parity, production-secure marketplace, or solved guardian-intelligence wording.

Batch CJ narrows the orchestration residual through `production_sla_orchestration`, `exactly_once_recovery_evidence`, `duplicate_side_effect_audit`, and `/api/operator/production-sla-orchestration`. It exposes provider windows, jitter budgets, replay windows, failure-injection methods, idempotency scopes, side-effect boundaries, duplicate suppression, reconciliation, operator recovery controls, final-audit linkage, and blocked claims. It does not permit unconditional exactly-once scheduling, crash-proof orchestration, a full distributed workflow engine, production-ready, full parity, or exceeded-reference-system wording.

Batch CK narrows the independent security residual through `independent_secure_host_review`, `live_hostile_isolation_drills`, `secure_host_recovery_authority`, and `/api/operator/independent-secure-host-review`. It exposes reviewer scope, finding remediation, live hostile drill receipts, isolation evidence matrices, operator recovery authority, final-audit linkage, and blocked claims. It does not permit secure/private-by-default execution, production security solved, IronClaw-class secure execution, TEE/CVM/Wasm/container isolation implemented, production-ready, full parity, or exceeded-reference-system wording.

Batch CL narrows the reach/media/mobile residual through `broad_channel_sla_operations`, `production_voice_media_quality_gates`, `mobile_execution_continuity`, and `/api/operator/production-reach-voice-mobile`. It exposes provider-window behavior, rate limits, abuse handling, degraded recovery, coverage-gap claim boundaries, STT/TTS/media quality and latency gates, correction/deletion privacy boundaries, provider-regression fallback, notification approval handoff, mobile action continuity, thread/memory recovery, offline recovery, revocation fail-closed behavior, final-audit linkage, and blocked claims. It does not permit OpenClaw-class reach, complete channel coverage, voice parity, multimodal parity, always-available operation, production-ready, full parity, or exceeded-reference-system wording.

Batch CM narrows the independent learning and memory-provider residual through `independent_outcome_cohort_review`, `task_scoped_causal_learning`, `memory_provider_parity_matrix`, and `/api/operator/independent-learning-memory-parity`. It exposes independent evaluator metadata, sample and power rationale, consent/anonymization, adverse-event review, bounded outcome claims, task-scoped counterfactual causal receipts, confounder boundaries, rollback authority, canonical/advisory memory-provider comparison, privacy-regression quarantine, delete/export receipts, final-audit linkage, and blocked claims. It does not permit guardian-intelligence superiority, solved live learning, live-human-outcome superiority, memory superiority, full memory-provider parity, production-ready, full parity, or exceeded-reference-system wording.

Batch CN narrows the operator-control residual through `long_work_debugging_recovery`, `operator_control_density`, `independent_operator_usability_accessibility`, and `/api/operator/dense-operator-recovery-control`. It exposes failed-workflow diagnosis, branch/output comparison, interruption resume, cross-batch residual-risk inspection, pause/resume/retry/repair/branch/compare/revoke/quarantine/handoff/rollback/audit controls, task-relative operator effort, independent usability/accessibility evidence, keyboard-only paths, recovery correctness, final-audit linkage, and blocked claims. It does not permit best/world-class cockpit, solved operator control, production-ready, full parity, or exceeded-reference-system wording.

Batch CQ closed the bounded proof-train claim-lift audit after CP. It permits only the exact bounded proof-train wording recorded in the claim ledger and keeps full parity and exceedance blocked. Batches CR-CZ now own the post-CQ production-evidence train and the later post-evidence claim audit.

This roadmap preserves the Seraph vision boundary: Seraph should exceed reference agents through guardian memory, restraint, trust, operator legibility, and recovery rather than by becoming an unrestricted plugin farm, raw channel gateway, or terminal-only coding shell.

## Team Review Record

Lead plan:

- Explorer pass `Avicenna`: Seraph vision, shipped truth, and non-negotiables.
- Capability import pass `Linnaeus`: Hermes/OpenClaw/IronClaw references and catalog-extension status.
- Critic/Contrarian pass `Boole`: claim strength, stale source risk, proof gaps, and scope drift.
- Execution mapping pass `Lovelace`: mapped every feature area to milestone, issue, and proof anchors; identified multimodal/voice as the only genuine missing dedicated issue.
- Follow-up Critic/Contrarian pass `Cicero`: confirmed the multimodal/voice issue was not duplicated, accepted creation after narrowing it to a proof/governance anchor, and requested the wording/source-map revisions now reflected above.

Critic disposition: accepted. The broad "better today" language was weakened to scoped differentiators, IronClaw security wording was narrowed to source-backed TEE/CVM/vault/Wasm/allowlist language, OpenClaw browser/control docs were added to the source trail, proof gates now name milestone families, the acceptance standard now applies to execution-batch readiness, and the sidebar ordering follows the dependency chain.

Follow-up disposition: accepted. The current-source refresh, execution mapping table, and IronClaw secure-capability-host plan now complete the original "still to do after this PR" items. Dedicated issue [#467](https://github.com/seraph-quest/seraph/issues/467) was created for guardian-safe multimodal and voice proof, added to the Seraph Execution project with required queue/lane/priority/size/status/review fields, and linked back into this document.

Post-merge disposition: accepted. [PR #473](https://github.com/seraph-quest/seraph/pull/473) merged the aggregate proof train, so this document now separates strategy completion and proof-train completion from the remaining product gaps. The stale reach PR [#462](https://github.com/seraph-quest/seraph/pull/462) was closed as superseded, and proof-slice issues [#438](https://github.com/seraph-quest/seraph/issues/438), [#467](https://github.com/seraph-quest/seraph/issues/467), [#470](https://github.com/seraph-quest/seraph/issues/470), [#471](https://github.com/seraph-quest/seraph/issues/471), and [#472](https://github.com/seraph-quest/seraph/issues/472) were closed with explicit claim-boundary comments. The X article is used only through the accessible fxtwitter retrieval and only for bounded Hermes-pressure claims; its benchmark claim remains unverified.
