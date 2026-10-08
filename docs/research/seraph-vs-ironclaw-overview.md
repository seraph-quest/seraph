---
id: seraph-vs-ironclaw-overview
slug: /seraph-vs-ironclaw-overview
title: Seraph and IronClaw — reader overview
---

# Seraph and IronClaw

Comparative, privacy and security wording remains bounded by the
[strategy claim ledger](./19-strategy-claim-ledger.md).

**Dated research:** 5 October 2026, Europe/Warsaw. [#954](https://github.com/seraph-quest/seraph/issues/954) owns this comparison. This is an evidence and alternatives document; the [Constitution](/) and ADRs retain accepted-target authority.

Seraph and IronClaw both offer a personal agent with persistent context, tools, background work and operator controls. Seraph's clearest practical direction is connecting fresh goals to cited opportunities, reviewed useful work and separately reviewed learning. IronClaw offers relevant patterns in unified durable process ownership, scoped sandbox execution, memory providers and installable extensions. Neither repository inventory establishes that one system produces better human outcomes.

Seraph already has substantial bounded capability. The reviewed #899 program connects Goals, Work tasks, native jobs, evidence, approvals, private results and recovery, including finite research, repository repair/publication, document comparison and narrow connected actions. The next roadmap should extend those journeys instead of recreating their infrastructure. Read the [shared Seraph baseline](./seraph-capability-baseline-2026-10-05.md) for exact release and platform boundaries.

## What an operator can reasonably expect

| Journey | Seraph | IronClaw | Useful conclusion |
| --- | --- | --- | --- |
| Keep goals and decide what deserves attention | Explicit Goals, tasks, evidence and guarded goal-loop machinery; automatic useful opportunity discovery and outcome improvement remain unproven. | Heartbeat checklists and scheduled/reactive routines provide proactive mechanisms; they do not demonstrate goal achievement. | Prioritize [fresh goals and cited opportunities](/guardian-capability-roadmap#m2-evidence-cited-opportunities). |
| Ask for useful bounded work and inspect the result | Existing finite workflows retain owner, permission, budget, approval, artifact and recovery boundaries. | Parallel jobs, subagents, extensions and scoped shell execution; some Reborn automation/background-delivery work remains partial. | Extend [reviewed bounded plans](/guardian-capability-roadmap#m3-reviewed-opportunity-plans) over existing jobs. |
| Remember what helped | Canonical memory and selection-only procedure preferences require provenance and separate adoption. Success alone does not authorize learning. | Persistent workspace memory, identity files, memory-provider seam and skill distillation/refinement machinery. | Build [reviewed usefulness learning](/guardian-capability-roadmap#m4-reviewed-intervention-usefulness) with feedback evidence, not automatic success-based promotion. |
| Use tools, files and connected services | Bounded code repair, research, private literal document comparison, authored packages and exact Gmail/Calendar/Forgejo actions have distinct proof limits. Browser/voice/reach inventories include narrower and historical evidence. | WASM/MCP extensions, Docker shell, files, messaging and NEAR search integration; official parity inventory still marks browser automation and realtime voice missing. | Compare actual journeys and installed profiles; avoid feature-count rankings. |
| Protect prompts sent to a model | Active inference is governed OpenRouter; no NEAR hardware verifier is implemented. | Inspected NEAR adapter is standard authenticated Chat transport; provider selection itself does not verify TEE evidence. | Treat [verified NEAR inference](/guardian-capability-roadmap#m5-verified-near-inference) as conditional blocked work with a reviewed ADR exception. |

## The NEAR distinction

OpenRouter currently lists a NEAR AI upstream. That makes a NEAR-backed model route technically expressible within Seraph's current gateway policy, subject to its normal consent, capability and budget gates. This research did not establish live configured NEAR execution in Seraph or end-client attestation forwarding through OpenRouter.

NEAR's official Python SDK can verify gateway and model evidence, use model-key encryption and explicitly verify exact response bytes. A first optional direct NEAR Cloud route can be tightly scoped to text, no tools and no streaming, with encryption required and output withheld until a model signature passes. It needs explicit model/software/trust pins and a reviewed exception to ADR-006. Local cryptographic replay tests can prove implementation mechanics; separately authorized live evidence is required for operational use.

The protocol still cannot prove a complete model-to-gateway-to-final-response chain or identify the exact serving instance from a shared signer key. Seraph's local state, tools and logs would remain outside the inference TEE. The [NEAR trust annex](./near-ai-trust-boundary-2026-10-05.md) explains each boundary and blocker.

## Evidence and limits

IronClaw was inspected at release **v1.4.1**, published **29 September 2026**, commit **`b011eb3932a0e1ab8bbe833893599ca9ae4925c5`**, and main **`b0b999d96781516ee05e6ba961d6f3ead900da96`**, dated **10 September**. Relevant browser, delegation, automation, sandbox, memory and NEAR adapter claims were checked against release source. Main and the release are distinct snapshots; migration-era README, parity and crate documentation sometimes disagree. Implementation source and declared limits are recorded in the [detailed comparison](./seraph-vs-ironclaw-detailed.md).

Official sources: [IronClaw release](https://github.com/nearai/ironclaw/releases/tag/ironclaw-v1.4.1), [release capability inventory](https://github.com/nearai/ironclaw/blob/b011eb3932a0e1ab8bbe833893599ca9ae4925c5/FEATURE_PARITY.md), [NEAR SDK](https://github.com/nearai/inference-sdk/tree/b9930893a9f560e66898e1616111c5ac2241686c), [NEAR verification](https://docs.near.ai/cloud/verification/cloud-api/response-signatures), [OpenRouter NEAR endpoints](https://openrouter.ai/api/v1/models/z-ai/glm-5.3-flash/endpoints). No comparative live-agent benchmark, learner study, model-quality measurement or paid inference ran for this research. Missing evidence is uncertainty, not proof of absence.
