---
id: seraph-capability-baseline-2026-10-05
title: Seraph capability baseline — 5 October 2026
---

# Seraph capability baseline

Research owned by [#954](https://github.com/seraph-quest/seraph/issues/954).
Inspected 5 October 2026. This is a dated source and test audit, not a runtime
certification. The Constitution/ADRs remain target authority; STATUS owns
develop truth. This document supports the [capability roadmap](/guardian-capability-roadmap).

## Revision and release boundary

| Surface | Exact revision / receipt | Interpretation |
| --- | --- | --- |
| `main` | `d7711b70321e54f0bd7ccdeac5a7cd2a87a0b22f`; [PR #880](https://github.com/seraph-quest/seraph/pull/880), merged 27 September | Released branch contains durable Work Board #864. A Git ref is not evidence of a running deployment. |
| `develop` | `0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a`; [PR #953](https://github.com/seraph-quest/seraph/pull/953), merged 4 October | Six-epic #899 program is integrated, not newly released on main. |
| Earlier proactive batch | `cb12276dbdb9d40494e5a7684d356d0d1a766ff8`; [PR #931](https://github.com/seraph-quest/seraph/pull/931), merged 2 October | Goal setup, source-watch Inbox, browser, finite procedures and connected journeys predate this roadmap. |

Fetched refs and inspected source on a clean feature branch from develop. No
runtime configuration, application code, credentials, deployment or paid
inference changed. The new comparisons pin competitor sources separately;
competitor default branches and released tags are not interchangeable.

## Reuse inventory

Paths below are verified existing paths at the develop revision above. Links
use that revision. Lifecycle refers to the bounded surface, not broad parity.
Source-inspected behavior does not establish live availability or usefulness.

| Operator journey / lifecycle | Existing owner and contracts | Boundaries to preserve / next gap |
| --- | --- | --- |
| Define a finite goal — Shipped bounded data/UI; useful autonomous progress Partial | [Goal](https://github.com/seraph-quest/seraph/blob/0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a/backend/src/db/models.py), [goal contracts](https://github.com/seraph-quest/seraph/blob/0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a/backend/src/goals/contracts.py): success criteria, budgets, revisions, owner and finite authority | `goal_conditioned_loop` evaluates supplied candidates; it is not a complete source-discovering planner. M2 adds explicit freshness for a selected public-watch assessment. |
| Notice changed permitted sources — Shipped finite watch and actionable Inbox; semantic relevance Partial | [source_watch.py](https://github.com/seraph-quest/seraph/blob/0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a/backend/src/guardian/source_watch.py), [inbox.py](https://github.com/seraph-quest/seraph/blob/0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a/backend/src/guardian/inbox.py): packets, dossier readback, outbox, disposition/action receipts, owner/version checks | Material-change filter is lexical. Source acceptance creates a Triage card; reviewed Work proposal acceptance later queues typed work. Do not recreate goals, watches or Inbox. |
| Queue and resume bounded work — Shipped native runtime; generic arbitrary execution Excluded from these milestones | [dispatcher.py](https://github.com/seraph-quest/seraph/blob/0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a/backend/src/work_board/dispatcher.py), [triage.py](https://github.com/seraph-quest/seraph/blob/0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a/backend/src/work_board/triage.py): tasks, attempts, typed inputs, grants, leases/fences, idempotency and dependencies | Two running board tasks is distinct from one remote inference. Accepted proposals become Todo and eligible dispatcher work. Contacted unknown effects do not replay. M3 reuses these transitions. |
| Read, research and report — Shipped bounded paths; general research agent Partial | [ADR-010](/decisions/reviewed-artifact-pipelines), [ADR-012](/decisions/finite-durable-readonly-research), [ADR-014](/decisions/bounded-evidence-dependencies) | Three-step browser→dossier→report, finite explicit sources/two children, three evidence consumers. No arbitrary DAG/source discovery is implied. |
| Fix and publish a repository change — Shipped bounded Python/Node and exact publication mechanics; broad coding-agent quality unmeasured | [ADR-007](/decisions/bounded-node-repair-supervision), [ADR-009](/decisions/tested-repository-publication), PR #953 | Execution modes have different isolation guarantees; do not call every terminal sandboxed. Preserve tests, exact review, approval and destination readback. No new coding platform in this roadmap. |
| Learn from outcomes — Shipped reviewed canonical/procedure mechanics; measured usefulness Partial | [feedback.py](https://github.com/seraph-quest/seraph/blob/0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a/backend/src/guardian/feedback.py), [m5.py](https://github.com/seraph-quest/seraph/blob/0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a/backend/src/memory/m5.py), [ADR-015](/decisions/reviewed-procedure-preferences) | Delivered/acknowledged have existing positive weights. Those are not explicit utility. Manual public-browser procedure population is narrow; M4 adds a separate signed opportunity-feedback scope. |
| Use connected actions — Shipped exact native paths, account-level operation not reverified here | [ADR-016](/decisions/exact-gmail-reply-send), [ADR-019](/decisions/exact-calendar-reschedule), [ADR-021](/decisions/exact-forgejo-issue-title), [ADR-022](/decisions/portable-selected-text-context) | Gmail exact reply, Calendar exact reschedule, Forgejo exact title, Telegram controls, selected text and Moltbook are bounded existing work. No generic agent permission follows from these. |
| Configure inference — Shipped governed OpenRouter path; independent purpose setup Partial | [configuration.py](https://github.com/seraph-quest/seraph/blob/0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a/backend/src/model_fabric/configuration.py), [inference_accounting.py](https://github.com/seraph-quest/seraph/blob/0ab60c3b4411f1b6c1e59c9986b7e45b9c61f69a/backend/src/workflows/inference_accounting.py) | Setup rejects multiple models/mixed embedding purposes. Shared witnessed accounting is durable. Namespace-derived vector tables exist; an active-generation pointer does not. Direct NEAR and local inference remain Excluded in the active phase. |

Canonical state is SQLite plus the existing private workspace artifacts and
signed memory/history. Configuration is the existing versioned file repository;
secrets remain in the vault. Derived vectors are not canonical memory. External
providers supply inference, not agent identity, tool authority or durable state.
Auth, Root/grants, source versions, proof freshness and budget checks are
revalidated at use; UI labels cannot replace them.

## Focused tests run in this research

Commands used the existing backend environment, external provider keys empty
and `TMPDIR=/private/tmp` on macOS. This per-command temporary path avoids the
audio sandbox's rejection of symlinked default `/var` temporary paths; it is
not a change to Seraph runtime configuration.

| Suites | Current result | Consequence |
| --- | --- | --- |
| `test_model_fabric_openrouter_policy.py`, `test_openrouter_setup_api.py`, `test_openrouter_setup_regressions.py` | **55 passed, 6 failed** in 25.41s | One expected-model mismatch, four setup saves returned503, one missing `inference_accounting_owners` table before the intended failure assertion. Root causes not established. M1 owns isolated reproduction and correct repairs. |
| `test_source_watch.py`, `test_work_board_m6_provider_free_journey.py`, `test_procedure_preference_signatures.py` | **39 passed, 2 failed** in 11.61s | Both M6 journeys expected `baseline_initialized` but got `blocked`. M2 owns investigation; no permission to weaken assertions or seed successful jobs. |

Use `.venv/bin/python -m pytest -q --no-cov` with these explicit suites from
`backend`. Initial runs without the canonical temporary path had additional
setup errors and are not the final receipt above. These failures prevent a
green-runtime claim; they do not prevent publishing reviewed planning docs.

PR #953 records a prior final follow-up of 242 passing local tests and a managed
CPU startup receipt. Those are attributed prior receipts, not rerun results.
This research did not launch the app, run paid canaries, exercise connected
accounts, verify a deployment or benchmark competitor task performance.

## Duplicate and stale-document audit

Open and closed issue searches covered models/embeddings/setup; goals/watch
freshness; planning/triage/pipelines; feedback/procedures; and NEAR/TEE.
Closed [#740](https://github.com/seraph-quest/seraph/issues/740),
[#884](https://github.com/seraph-quest/seraph/issues/884),
[#914](https://github.com/seraph-quest/seraph/issues/914),
[#889](https://github.com/seraph-quest/seraph/issues/889),
[#241](https://github.com/seraph-quest/seraph/issues/241) and
[#379](https://github.com/seraph-quest/seraph/issues/379) are prior art. The new
scopes add purpose-specific configuration, cited semantic opportunities,
opportunity-triggered finite plans and explicit feedback lineage; they do not
reopen generic model fabric, Inbox, weighted feedback or procedure foundations.
The six #899 epics #902–#907 are closed. No exact current NEAR verifier issue was
found; historical host isolation/attestation programs concern a different boundary.

### 2026-10-06 evaluation cancellation addendum

The operator cancelled harness-improvement and comparative evaluation campaigns;
[#771](https://github.com/seraph-quest/seraph/issues/771) is closed as not planned,
not delivered. This supersedes the 2026-10-05 audit's deferred evaluation ownership
and proposed study protocol. No campaign or synthetic quality substitute is
planned. The dated source audit remains evidence of its checked revision;
external usefulness and comparative quality remain unverified. Ordinary
regression and security checks remain required and do not establish improvement.
Existing #702, #695, #688, #666 and #664 remain unchanged.

The constitution's old process-local/future #743/#744 wording and STATUS's
branch-local #753 wording predate merged work. This pass corrects only those
affected statements and links this dated audit. Older comparison documents are
retained as dated research and point to the new comparisons; their old claims
must not be imported as current results. Closed issue prose and green checklist
boxes alone do not prove current capability or comparative superiority.
