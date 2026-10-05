---
id: seraph-vs-hermes-detailed
slug: /seraph-vs-hermes-detailed
title: Seraph and Hermes — detailed comparison
---

# Seraph and Hermes — detailed comparison

**Research date: October 5, 2026; access date: October 5, Europe/Warsaw.**
Start with the [overview](./seraph-vs-hermes-overview.md) for the short version.
This matrix compares concrete operator journeys, implementation boundaries and
the next useful action. It supplies research evidence; the
[Constitution](/) and ADRs own Seraph's
accepted target, and [STATUS](/status) owns shipped truth.

## Baselines and evidence vocabulary

Seraph source links pin `develop`
[`0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a`](https://github.com/seraph-quest/seraph/commit/0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a).
[PR #953](https://github.com/seraph-quest/seraph/pull/953) merged the
[#899 bounded-workflow program](https://github.com/seraph-quest/seraph/issues/899)
on October 4. These capabilities are `develop` implementations. The distinct
`main` release baseline is
[`d7711b70321e54f0bd7ccdeac5a7cd2a87a0b22f`](https://github.com/seraph-quest/seraph/commit/d7711b70321e54f0bd7ccdeac5a7cd2a87a0b22f);
this document does not claim the program is released there.

Hermes source links pin official `main`
[`93c9360a8da592cee43e8895403e91c7a920b0ce`](https://github.com/NousResearch/hermes-agent/commit/93c9360a8da592cee43e8895403e91c7a920b0ce),
committed October 5 at 06:05:42 UTC. The
[official latest-release API](https://api.github.com/repos/NousResearch/hermes-agent/releases/latest)
reported [v2026.9.24 / v0.21.5](https://github.com/NousResearch/hermes-agent/releases/tag/v2026.9.24),
published September 24. Its release is not marked immutable, and this research
did not resolve the tag object. Current-main findings are not stable-release
guarantees.

Seraph lifecycle uses exactly **Shipped**, **Partial**, **Experimental**,
**Planned**, **Deprecated** and **Excluded**, as defined by the
[documentation contract](/docs-contract). A broad journey
can be Partial while a named finite profile is implemented and available on
`develop`. Missing optional configuration is a readiness state, not a new
lifecycle label.

Evidence is separate: **documented**, **source-inspected**, **mock-tested**,
**runtime-verified** and **independently benchmarked**. Existing runtime receipts
are attributed to their original environment/date; they were not rerun here.
Focused current Seraph checks reported **55 passed / 6 failed** for model-related
tests and **39 passed / 2 failed** for source/procedure tests. The
[baseline report](./seraph-capability-baseline-2026-10-05.md) owns commands,
failures and residual risks. No live inference, installation, Hermes runtime or
independent comparative benchmark was run. Unknown means unverified, not absent.

## Capability matrix

Source keys link to immutable files in the evidence index below. Seraph lifecycle
labels apply to the journey described, not every function in its row.

| Journey and concrete outcome | Seraph implementation, lifecycle and limits | Hermes implementation and limits | Conclusion, uncertainty and action |
| --- | --- | --- | --- |
| **Goals:** keep an operator's finite goal moving toward an observable result. | **Partial.** Canonical goals, ownership/revision fences, context summaries, strategist and Work board are present [S1, S2]. Completion and consent remain finite. Source-inspected; live end-to-end usefulness unmeasured. | Documented + source-inspected standing `/goal`, contracts, subgoals, deterministic gates and waits [H1]. Single-session goals do not implicitly create Kanban cards. | Both have goal machinery. Neither this inspection nor an LLM completion judgment establishes actual success. [M2](/guardian-capability-roadmap#m2-evidence-cited-opportunities) must bind fresh evidence to the goal. |
| **Planning:** inspect and approve a plan before work begins. | **Partial.** Existing request/accept/reject contracts and triage permit one to five decomposition tasks, requiring registered capabilities and existing exact typed input references/digests [S3]. Fixed pipelines have exact review [S4]. Arbitrary autonomous effects remain Excluded. | Documented `/plan` saves a Markdown plan; goal contracts and Kanban provide additional work structures [H1, H2]. A plan prompt alone is not an enforced execution boundary. | Preserve the existing planner. [M3](/guardian-capability-roadmap#m3-reviewed-opportunity-plans) adds reviewed opportunity blueprints over finite executors; it must not mint arbitrary model-authored executable inputs. |
| **Proactive opportunities:** notice a material change and explain why it helps a goal. | **Partial.** Observer gathers goals/context and derives salience; strategist has opted-in goal web briefs/snapshots [S2]. Current source-watch materiality uses changed lines/characters and include/exclude terms [S14], which does not establish semantic goal relevance. | Documented schedules, goals and event-triggered runs [H1, H3, H4]. No independent evidence here of broadly useful autonomous personal interventions. | Automation is not measured usefulness. [M2](/guardian-capability-roadmap#m2-evidence-cited-opportunities) needs fresh cited semantic candidates and rejection paths. |
| **Events and schedules:** monitor permitted sources and deliver a useful update. | **Partial.** Scheduler, finite watch/procedure profiles and governed admission exist [S2, S5]. Quiet-hours, budget, authority and native readiness constrain delivery. | Documented cron, HMAC-authenticated webhooks, filters, event coalescing, optional event-triggered cron and direct delivery [H3, H4]. Authentication does not make payloads trusted. | Hermes has meaningful event/operational breadth. Preserve Seraph's bounded scheduling; test changed-source/quiet/no-change cases before expanding monitoring. |
| **Follow-through:** continue accepted work, pause, recover and read the result. | **Partial.** Durable jobs/Work board carry owner, deadline, attempts, authority, outputs and uncertainty [S6]. Recovery does not renew expired grants or replay unknown external writes. | Documented goals and cron history/recovery; source-inspected prioritized Kanban dispatch, max runtime, retries and process claims [H1, H3, H5]. Scope-backed restart safety depends on systemd host support. | Both have durable mechanisms. Run equivalent restart/cancel/unknown-effect journeys; do not equate every restart with resumable execution. |
| **Memory and personalization:** recall a preference with provenance. | **Partial.** Canonical memory, source-bound evidence and governed adoption are present [S7, S8]. Memory content is not automatically execution authority. | Documented local MEMORY.md/USER.md, SQLite session recall and optional external providers; source-inspected frozen prompt snapshot and write gate [H6, H7]. External providers introduce separate data destinations. | Measure factual recall, stale-evidence handling and user correction. Local state does not establish local inference or universally private operation. |
| **Outcome learning:** change future behavior only for a justified reason. | **Partial.** #919 already supports accept/reject/rollback of selection-only preferences from manual outcomes [S9]. Existing guardian feedback also weights acknowledgment/delivery [S15]; that is not explicit usefulness evidence. Quality remains unmeasured. | Documented skill creation, `/learn`, background improvement review and configurable memory/skill approvals [H2, H7]. Writes are free by default; effectiveness unmeasured. | [M4](/guardian-capability-roadmap#m4-reviewed-intervention-usefulness) narrowly extends existing store/review with explicit usefulness lineage. A delivered or acknowledged intervention cannot count as Helpful, and preference review grants no capability authority. |
| **Research:** investigate a finite question and adopt verified sources. | **Partial.** #901 finite read-only research children and #914 fixed browser → CPU dossier → report pipeline are implemented [S4, S10]. Verified handoffs, original deadlines and no-learning remain. Arbitrary DAGs/publication/export are outside that pipeline. | Documented search/extraction, browser, files, tool RPC and delegation [H8, H9]. Tool availability does not prove factual accuracy or source quality. | Build on completed research. Use matching questions, source-citation checks and independent readback; record research quality separately from mechanical completion. |
| **Coding and terminal:** repair a selected repository and publish a tested result. | **Partial.** Finite Python/Node repair and tested repository publication exist [S11]. Exact consent/patch/publication approvals, test/readback and native readiness apply. Host-user execution is not containment. | Documented broad terminal/file access across seven backends and async delegation [H8, H9]. Source editing or background process start does not establish repair success. | Hermes is broader; Seraph's bounded profiles are already real implementation. Compare complete repairs under equivalent permissions and cost limits. |
| **Browser and private context:** read an approved site or attach deliberate selected text. | **Partial.** Fixed private read and selected-text companion profiles exist [S12]. Selected text is explicit, previewed, finite and private; it is not background screenshots or automatic generic model context. macOS companion execution remains unverified. | Documented text/vision browser tools and Desktop preview/HUD [H8, H10]. Platform-specific behavior and data boundaries not exercised here. | Do not score broad browser support as equal to a tested private-site boundary. Test one concrete site/task and its permission/revocation/readback flow. |
| **Files and artifacts:** compare documents and open a durable result. | **Partial.** Addressable artifacts, evidence packets and private literal PDF/CSV comparison exist [S7, S12]. Literal line-total comparison is not general document understanding or malware certification. | Documented files/media and persisted terminal environments [H8]. Snapshot filesystem persistence is distinct from survival of live processes. | Verify artifact ownership, freshness and content, then test the selected result; neither registry entries nor downloadable files prove task success. |
| **Integrations and delegation:** coordinate bounded children or one external action. | **Partial.** Finite research children, connector contracts, paired Telegram control, exact Gmail reply, owned Calendar reschedule and Forgejo title profile exist [S10, S12]. Production Codeberg and real Google account acceptance are separate. | Documented plugin/MCP integrations, many messaging adapters and isolated-context async subagents [H8, H9, H11]. Child background processes remain child-owned; completion admission is not outbound success. | Hermes breadth is genuine. Seraph should retain reviewed finite side effects; use per-adapter live readback rather than counting adapters. |
| **Voice, messaging and continuity:** resume one trusted conversation elsewhere. | **Partial.** Cockpit and paired edges retain canonical ownership [S12]. Native/production voice, channel and platform readiness each need their own receipt. | Documented shared-state CLI/TUI/Desktop/gateway, local/cloud speech and Discord voice [H10, H11, H12]. Speech accuracy, latency and cross-platform reliability unmeasured. | Compare one resumed session and one voice task; retain provider/data-route disclosure. Native Desktop is already a Hermes strength. |
| **Sandbox, secrets, permissions and egress:** stop an unauthorized operation and inspect what left the host. | **Partial.** Trust grants, exact approvals, serial remote admission, revocation, reservations and optional Linux isolation exist [S6, S13]. Portable core does not establish OS containment or macOS sandbox execution. | Documented OS/whole-process isolation contract, command/file heuristics, secrets providers and optional iron-proxy; source-inspected profile secret scopes and host-network Compose defaults [H13, H14]. Plugins run with agent privileges. | Neither defaults nor approval prompts prove containment. Match threat models, mounts, subprocesses and egress before security comparison. |
| **Setup, reliability, latency and cost:** start a usable CPU core and know why a task is blocked. | **Shipped** CPU/core and effective-readiness surfaces within documented scope; **Partial** provider quality/account settlement/production proof [S12, S13]. OpenRouter-only active inference; GPU/VLM absent is supported. Purpose-specific routes are Planned. | Documented installer/setup/model/tools/doctor/service commands and provider choices [H15]. Source ordinary-turn/time budgets default unlimited/off [H16]; goals and Kanban have separate finite controls. Total cost and install speed unmeasured. | [M1](/guardian-capability-roadmap#m1-purpose-specific-inference) makes purpose routing explicit. [M5](/guardian-capability-roadmap#m5-verified-near-inference) stays blocked; no silent alternative provider. Measure cold start, completion latency and total billed usage. |

## Seraph evidence index

These source links pin the inspected `develop` commit. Implementation guides
describe the original proof boundary; the current focused check results belong
to the linked baseline report.

- **S1:** [canonical goals](https://github.com/seraph-quest/seraph/blob/0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a/backend/src/goals/contracts.py).
- **S2:** [observer and goal freshness](https://github.com/seraph-quest/seraph/blob/0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a/backend/src/observer/manager.py#L41), [strategist tick](https://github.com/seraph-quest/seraph/blob/0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a/backend/src/scheduler/jobs/strategist_tick.py#L1012).
- **S3:** [proposal contracts](https://github.com/seraph-quest/seraph/blob/0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a/backend/src/work_board/contracts.py#L356), [proposal triage](https://github.com/seraph-quest/seraph/blob/0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a/backend/src/work_board/triage.py#L996).
- **S4:** [reviewed pipeline preview/accept](https://github.com/seraph-quest/seraph/blob/0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a/backend/src/work_board/pipelines.py#L94), [verified output](https://github.com/seraph-quest/seraph/blob/0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a/backend/src/work_board/pipelines.py#L542), [#914](https://github.com/seraph-quest/seraph/issues/914).
- **S5:** [reviewed procedure runtime](https://github.com/seraph-quest/seraph/blob/0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a/backend/src/workflows/procedure_v2_runtime.py), [Current App Guide](/current-app).
- **S6:** [Work board dispatcher](https://github.com/seraph-quest/seraph/blob/0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a/backend/src/work_board/dispatcher.py), [governed inference admission](https://github.com/seraph-quest/seraph/blob/0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a/backend/src/model_fabric/remote_inference_admission.py).
- **S7:** [evidence packet contracts](https://github.com/seraph-quest/seraph/blob/0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a/backend/src/memory/evidence_working_set.py#L47), [adoption and execution use](https://github.com/seraph-quest/seraph/blob/0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a/backend/src/memory/evidence_working_set.py#L686), [#917](https://github.com/seraph-quest/seraph/issues/917).
- **S8:** [canonical-memory ADR](/decisions/canonical-memory-boundary).
- **S9:** [selection-only preference acknowledgment](https://github.com/seraph-quest/seraph/blob/0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a/backend/src/memory/procedure_preferences.py#L25), [explicit unmeasured quality](https://github.com/seraph-quest/seraph/blob/0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a/backend/src/memory/procedure_preferences.py#L50), [#919](https://github.com/seraph-quest/seraph/issues/919).
- **S10:** [fixed child creation and authority checks](https://github.com/seraph-quest/seraph/blob/0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a/backend/src/workflows/research_native.py#L20), [#901](https://github.com/seraph-quest/seraph/issues/901).
- **S11:** [repository repair](https://github.com/seraph-quest/seraph/blob/0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a/backend/src/workflows/repo_repair.py), [tested publication](https://github.com/seraph-quest/seraph/blob/0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a/backend/src/execution/repo_publication.py).
- **S12:** [pinned current application guide](https://github.com/seraph-quest/seraph/blob/0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a/docs/implementation/12-current-app-guide.md), [pinned status and proof limits](https://github.com/seraph-quest/seraph/blob/0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a/docs/implementation/STATUS.md).
- **S13:** [OpenRouter transport contract](https://github.com/seraph-quest/seraph/blob/0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a/backend/src/model_fabric/contracts.py#L22), [accounting](https://github.com/seraph-quest/seraph/blob/0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a/backend/src/model_fabric/accounting.py).
- **S14:** [source-watch material-change heuristic](https://github.com/seraph-quest/seraph/blob/0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a/backend/src/guardian/source_watch.py#L748).
- **S15:** [acknowledgment and delivery weighting](https://github.com/seraph-quest/seraph/blob/0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a/backend/src/guardian/feedback.py#L207).

## Hermes evidence index

These official-source links pin inspected `main`; documentation establishes
declared behavior, while only selected implementations were source-inspected.
No runtime or independent benchmark evidence was obtained.

- **H1:** [persistent goals and contracts](https://github.com/NousResearch/hermes-agent/blob/93c9360a8da592cee43e8895403e91c7a920b0ce/website/docs/user-guide/features/goals.md#L9), [gate execution](https://github.com/NousResearch/hermes-agent/blob/93c9360a8da592cee43e8895403e91c7a920b0ce/hermes_cli/goals.py#L396), [manager](https://github.com/NousResearch/hermes-agent/blob/93c9360a8da592cee43e8895403e91c7a920b0ce/hermes_cli/goals.py#L1088).
- **H2:** [plan mode](https://github.com/NousResearch/hermes-agent/blob/93c9360a8da592cee43e8895403e91c7a920b0ce/website/docs/user-guide/features/skills.md#L108), [skills and learning](https://github.com/NousResearch/hermes-agent/blob/93c9360a8da592cee43e8895403e91c7a920b0ce/website/docs/user-guide/features/skills.md#L117).
- **H3:** [cron lifecycle and recovery documentation](https://github.com/NousResearch/hermes-agent/blob/93c9360a8da592cee43e8895403e91c7a920b0ce/website/docs/user-guide/features/cron.md#L236), [execution history](https://github.com/NousResearch/hermes-agent/blob/93c9360a8da592cee43e8895403e91c7a920b0ce/website/docs/user-guide/features/cron.md#L382), [claim before dispatch](https://github.com/NousResearch/hermes-agent/blob/93c9360a8da592cee43e8895403e91c7a920b0ce/cron/scheduler_provider.py#L188).
- **H4:** [webhook routes and boundaries](https://github.com/NousResearch/hermes-agent/blob/93c9360a8da592cee43e8895403e91c7a920b0ce/website/docs/user-guide/messaging/webhooks.md#L79), [coalescing implementation](https://github.com/NousResearch/hermes-agent/blob/93c9360a8da592cee43e8895403e91c7a920b0ce/gateway/platforms/webhook_coalesce.py#L27).
- **H5:** [Kanban documentation](https://github.com/NousResearch/hermes-agent/blob/93c9360a8da592cee43e8895403e91c7a920b0ce/website/docs/user-guide/features/kanban.md), [priority then age](https://github.com/NousResearch/hermes-agent/blob/93c9360a8da592cee43e8895403e91c7a920b0ce/hermes_cli/kanban_db_dispatch.py#L2269), [runtime cap](https://github.com/NousResearch/hermes-agent/blob/93c9360a8da592cee43e8895403e91c7a920b0ce/hermes_cli/kanban_db_dispatch.py#L648).
- **H6:** [memory documentation](https://github.com/NousResearch/hermes-agent/blob/93c9360a8da592cee43e8895403e91c7a920b0ce/website/docs/user-guide/features/memory.md), [frozen snapshot and configured limits](https://github.com/NousResearch/hermes-agent/blob/93c9360a8da592cee43e8895403e91c7a920b0ce/tools/memory_tool.py#L1), [external providers](https://github.com/NousResearch/hermes-agent/blob/93c9360a8da592cee43e8895403e91c7a920b0ce/website/docs/user-guide/features/memory-providers.md#L9).
- **H7:** [write approval defaults and staging](https://github.com/NousResearch/hermes-agent/blob/93c9360a8da592cee43e8895403e91c7a920b0ce/tools/write_approval.py#L1), [memory-gate import fallback](https://github.com/NousResearch/hermes-agent/blob/93c9360a8da592cee43e8895403e91c7a920b0ce/tools/memory_tool.py#L84).
- **H8:** [toolsets, backends and process lifetime](https://github.com/NousResearch/hermes-agent/blob/93c9360a8da592cee43e8895403e91c7a920b0ce/website/docs/user-guide/features/tools.md).
- **H9:** [async delegation and completion boundary](https://github.com/NousResearch/hermes-agent/blob/93c9360a8da592cee43e8895403e91c7a920b0ce/website/docs/user-guide/features/delegation.md#L9).
- **H10:** [native Desktop and shared state](https://github.com/NousResearch/hermes-agent/blob/93c9360a8da592cee43e8895403e91c7a920b0ce/website/docs/user-guide/desktop.md).
- **H11:** [messaging gateway](https://github.com/NousResearch/hermes-agent/blob/93c9360a8da592cee43e8895403e91c7a920b0ce/website/docs/user-guide/messaging.md).
- **H12:** [voice and platform prerequisites](https://github.com/NousResearch/hermes-agent/blob/93c9360a8da592cee43e8895403e91c7a920b0ce/website/docs/user-guide/features/voice-mode.md#L9).
- **H13:** [authoritative security boundary](https://github.com/NousResearch/hermes-agent/blob/93c9360a8da592cee43e8895403e91c7a920b0ce/SECURITY.md#L32), [profile secret scoping](https://github.com/NousResearch/hermes-agent/blob/93c9360a8da592cee43e8895403e91c7a920b0ce/agent/secret_scope.py#L220).
- **H14:** [secret sources](https://github.com/NousResearch/hermes-agent/blob/93c9360a8da592cee43e8895403e91c7a920b0ce/website/docs/user-guide/secrets/index.md#L3), [optional iron-proxy](https://github.com/NousResearch/hermes-agent/blob/93c9360a8da592cee43e8895403e91c7a920b0ce/website/docs/user-guide/egress/index.md), [host-network default](https://github.com/NousResearch/hermes-agent/blob/93c9360a8da592cee43e8895403e91c7a920b0ce/docker-compose.yml#L35).
- **H15:** [installation and setup commands](https://github.com/NousResearch/hermes-agent/blob/93c9360a8da592cee43e8895403e91c7a920b0ce/README.md).
- **H16:** [ordinary turn/runtime defaults](https://github.com/NousResearch/hermes-agent/blob/93c9360a8da592cee43e8895403e91c7a920b0ce/hermes_cli/config_defaults.py#L75).

## Uncertainty and next evidence

Hermes's goal documentation describes judge errors as fail-open; current source
also pauses after five consecutive transport errors or three parse failures
([source](https://github.com/NousResearch/hermes-agent/blob/93c9360a8da592cee43e8895403e91c7a920b0ce/hermes_cli/goals.py#L1512)).
Its README's Honcho wording should not override the current provider docs, which
describe Honcho as catalog-installed. Approval, scanning and redaction remain
heuristics under Hermes's explicit security contract. Optional egress controls
and container tools do not imply default whole-process containment.

Seraph's existing deterministic/native receipts establish their named boundaries.
They do not establish live paid-model quality, broad integration reliability,
macOS adapter execution or improved operator decisions. The fresh failures must
be understood before asserting that the relevant current paths pass.

The proposed comparison experiment should pin both systems, declare their
permissions/providers/budgets, run one complete journey from each relevant row,
verify the outcome independently, and repeat interruption, recovery and
revocation cases. Score success, operator usefulness, failure visibility,
latency and total cost separately. This document makes no comparative
superiority claim.
