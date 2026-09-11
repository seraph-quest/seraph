# Workstream 02: Execution Plane

## Status On This Branch

- [x] M2 execution completion is implemented in this batch and awaiting merge back to `develop`.
- [x] Branch-local #748 proof now runs an authenticated, offline inspect→plan→preview→approval→apply→test→diagnose→readback journey through a durable job and an isolated fixture worktree. It records source/patch/path/command/resource digests, job-owned artifacts and checkpoints, 300-second and two-attempt bounds, exact approval/session/authority checks, actual child-process exit/output, and process-group timeout/cancel cleanup plus replay dedupe. This remains a deterministic local fixture: arbitrary repository execution, host isolation, external provider transport, autonomous commit/push/merge, and production crash-proof semantics are outside the demonstrated boundary. The one-shot path computes its source digest locally; callers that omit `expected_source_digest` do not bind a separate inspect receipt to apply. Detached descendants remain operator-visible as unknown cleanup when process identity cannot be verified.

## Paired Research

- primary design doc: [05. Execution Plane](/research/execution-plane)

## Shipped On `develop`

- [x] 17 built-in tool capabilities registered through the native tool registry
- [x] shell execution through the sandboxed shell tool
- [x] browser automation through the browser tool
- [x] filesystem read/write tool surface
- [x] soul and goals tool surface for agent self-context
- [x] vault tool surface for controlled secret storage and retrieval
- [x] web-search tool surface
- [x] dynamic MCP tool loading and runtime-managed MCP server configuration
- [x] visible tool execution in chat, WebSocket, onboarding, strategist, and specialist flows
- [x] first-class reusable workflows loaded from defaults and workspace files, with tool, skill, and MCP-aware gating
- [x] starter packs that bundle useful default skills and workflows into operator-invocable capability sets
- [x] explicit workflow metadata for policy modes, execution boundaries, approval behavior, and risk level exposed to operator-facing APIs
- [x] first operator-facing workflow controls for enable/disable, reload, and draft-to-cockpit steering
- [x] workflow loading now rejects underdeclared runtime step tools, and tool/workflow metadata now expose secret-reference acceptance explicitly for injection-safe paths
- [x] workflow execution audit now carries structured workflow-run details, artifact-path lineage, and degraded-step visibility for cockpit/operator views
- [x] workflow run history now exposes boundary-aware replay context, approval counts, risk level, step tools, and artifact lineage through the workflows API
- [x] workflow run history now also exposes pending-approval details, awaiting-approval state, replay guardrails, and thread-aware recovery metadata instead of only recent run summaries
- [x] workflow run history now also persists approval-context snapshots and blocks replay/resume when the workflow trust boundary changes instead of reusing stale approval assumptions
- [x] workflow runtime now persists reusable checkpoint context for safe branches, records structured failed-step payloads for later recovery, and keeps workflow-run listing truthful when a later checkpoint cannot actually be resumed
- [x] typed source-capability surfaces now also expose connector-backed authenticated mutation planning, executable bounded `work_items.write` plus `code_activity.write` routes, scoped approval and audit metadata, native mutation/report planning tools, and provider-neutral report publication workflows that can target work items or PR-native code-activity flows instead of leaving privileged write paths as implicit connector lore
- [x] cockpit workflow surfaces now derive branch-family supervision from persisted lineage, including child/peer branch inspection plus latest-branch continue/open-parent controls, instead of treating every workflow run as an isolated replay row
- [x] built-in delegation now exposes a separate `vault_keeper` specialist for secret-management tools so generic memory delegation stops carrying direct vault read/write capability
- [x] capability bootstrap now limits automatic repair to low-risk local workflow or skill toggles, leaving policy lifts and capability-surface expansion as explicit operator actions instead of workflow-planning side effects, and generated repair bundles no longer batch-run multiple privileged mutations from one cockpit click
- [x] provider-neutral source capability discovery now exposes typed public-web tools, managed authenticated connectors, and explicit raw-MCP gaps so execution planning can inspect real external-source seams before composing a routine
- [x] wrapped MCP tools now preserve authenticated source context through audit, approval, and secret-ref layers, so workflow approval context and operator tool metadata can distinguish generic external MCP from authenticated external-source execution at runtime
- [x] high-risk extension configure and source-save paths now bind approvals to materially changed config targets or exact requested source content, and pending approval/operator/activity surfaces carry structured approval scope, lifecycle boundary, and trust-context metadata instead of flattening privileged mutations into generic approval rows
- [x] sync helper runtime-audit logging now fails soft when no event loop is active and uses tracked background tasks when a loop exists, so audit persistence no longer destabilizes sync execution paths in CI teardown
- [x] tool and MCP metadata now expose explicit `secret_ref_fields`, wrapped MCP tools preserve only declared injection-safe secret-bearing fields, and connector-backed authenticated mutation execution now rejects undeclared payload keys instead of forwarding arbitrary action arguments to managed runtimes
- [x] operator control-plane synthesis now exposes workspace governance modes, role inventory, usage rollups, runtime posture, review receipts, and blocked-workflow or approval handoff state through one operator API instead of leaving team-control context split across settings, audit, extensions, continuity, and workflow surfaces
- [x] managed background processes now bind recovery visibility to the originating session, so list/read/stop recovery handles fail closed outside that session instead of leaving cross-session process recovery broadly discoverable
- [x] process-runtime tool metadata and approval receipts now expose explicit session-partition plus background-execution trust context, and `start_process` now requires confirmation even when global approval mode is off so persistent runtime work no longer inherits the generic high-risk tool path
- [x] explicit managed-process stops and session teardown now terminate retained process groups within a bounded TERM/wait/KILL path, snapshot descendant PID/start-time identities before signaling, and expose stopped plus remaining-descendant status; identity loss, stale PIDs, signaling failures, incomplete snapshots, and surviving descendants remain operator-visible as unknown or failed with a recoverable handle, while disposable artifacts are removed only after verified cleanup
- [x] one-shot command timeouts and cancellations now expose unknown cleanup when detached or otherwise unverified descendants remain, retain the invocation worker root for bounded operator recovery, and include cleanup status in the audit receipt; process groups remain the bounded termination boundary and do not claim general process-tree isolation
- [x] session deletion holds a process-cleanup fence across memory flush and the database transaction, rejects same-session background starts and concurrent cleanup attempts during teardown, fails closed when any owned process stop is unknown/failed/conflicting, and records requested/stopped/unknown/failed/conflict cleanup counts; descendants created after the pre-stop snapshot or outside the process tree remain a documented residual
- [x] process execution now also uses disposable worker roots outside the workspace for direct command and background-process runtime state, MCP secret refs require explicit credential-egress allowlists before connector-bound execution can receive secrets, and workflow/operator surfaces preserve delegated plus branch-handoff trust partitions instead of flattening those execution boundaries into generic approval context
- [x] the operator API now also exposes a workspace-level background-session substrate that joins session-owned managed processes, workflow branch-handoff bundles, and session continuity snippets instead of leaving long-running background work split between process handles, workflow rows, and ad hoc session recovery
- [x] the operator API now also exposes searchable engineering-memory bundles for repositories and pull requests, grouping workflow continuity, approval targets, audit receipts, artifact follow-through, and matched session snippets by shared reference instead of leaving repo context fragmented across threads and operator surfaces
- [x] the operator API now also exposes an explicit continuity graph that links sessions, workflows, approvals, artifacts, notifications, deferred guardian items, and interventions through one evidence-backed graph instead of forcing operators to infer those relationships from separate background-session, timeline, and observer surfaces
- [x] the cockpit operator surface now composes background sessions, engineering-memory bundles, and the continuity graph into one background-supervision lane with direct continue/open-thread/latest-output/latest-branch/next-step control instead of making operators pivot across three separate continuity endpoints to resume long-running work
- [x] the operator workflow-orchestration surface now also compacts long-running runs into explicit state capsules with visible-versus-compacted step counts, artifact totals, recent-step labels, and preserved retry/checkpoint/repair paths instead of forcing operators to reconstruct durable recovery state from raw step lists alone
- [x] the operator workflow-orchestration surface now also exposes anticipatory repair drafts, backup-branch candidates, condensation-fidelity state, and thread-level endurance summaries, and the operator benchmark-proof surface now includes a dedicated `workflow_endurance_and_repair` suite instead of leaving long-horizon workflow quality buried inside generic runtime scenario output
- [x] the execution plane now has a dedicated `m2_execution_supremacy` suite and `/api/operator/m2-execution-benchmark` surface that gates terminal/process/sandbox, browser/HTTP/computer-use, filesystem patching, artifact registry, operator receipts, and the #435 adversarial security gauntlet together instead of spreading M2 completion across small slices
- [x] patch preview/apply receipts and workflow-run projections now expose stable artifact records with artifact IDs, producer lineage, content hashes, trust-boundary context, and recovery hints instead of leaving execution outputs as raw path strings only
- [x] workflow execution now also writes a minimal durable workflow state kernel: `workflow_run_states` and `workflow_step_states` persist run identity, lineage, step state, safe checkpoint context, retry or repair state, and redacted durable audit receipt metadata while unsafe secret or authenticated checkpoint payloads stay out of reusable checkpoints; delegated workflow artifacts now emit `workflow_artifact_reviews` rows with owner, reviewer, lineage, approval handoff, and trust-partition metadata; checkpoint resume tries durable state before audit projection; and `/api/operator/durable-workflow-engine` plus `durable_workflow_engine_v1` expose both deterministic proof fixtures and recent persisted state snapshots without claiming a full distributed workflow engine
- [x] Batch BX now layers production-oriented durable orchestration receipts on that kernel: `/api/operator/durable-workflow-engine-v2`, `production_durable_orchestration`, and `durable_workflow_engine_v2` prove lease ownership, revision-guarded idempotent transitions, trigger dedupe, unsafe-resume recovery blocking, and delegated-artifact adoption gates while still blocking LangGraph-class, exactly-once external scheduling, crash-proof, and full workflow parity claims

## Working On Now

- [x] this workstream has now shipped both workflow-facing hardening slices through `execution-safety-hardening-v1` and `execution-safety-hardening-v2`
- [x] this workstream partnered on `cockpit-workflow-views-v1`
- [x] this workstream now also ships `workflow-timeline-and-approval-replay-v3`
- [x] this workstream now also ships `retire-village-and-editor-v1` and `execution-safety-hardening-v7` alongside richer workflow replay metadata, failed-step visibility, and retry-from-step control surfaces
- [x] this workstream now also ships `adapter-first-capability-contracts-v1` for source-capability discovery and connector-first external evidence planning
- [x] this workstream now also ships `authenticated-source-boundary-hardening-v1` for wrapper-stable source context and fail-closed authenticated workflow checkpoints
- [x] this workstream now also ships the first `authenticated-mutation-and-boundary-explainability-v1` Batch AL aggregate for scoped extension mutation approvals, boundary-aware workflow surfaces, and safer sync runtime-audit behavior
- [x] this workstream now also ships `connector-backed-authenticated-mutation-boundaries-v1` for typed source-write planning, scoped connector mutation approvals, and CI stabilization around the heaviest backend/frontend suites
- [x] this workstream now also ships the first `adapter-backed-authenticated-operations-and-report-workflows-v1` Batch AM aggregate for executable connector-backed source actions, provider-neutral report publication planning, structured mutation execution audit, and heavier backend shard weighting for the approval/context suites
- [x] this workstream now also ships the first Batch AT aggregate for field-scoped secret-reference execution surfaces, explicit per-tool secret-ref metadata, and allowlisted authenticated mutation payload enforcement
- [x] this workstream now also ships the second Batch AT aggregate for session-bound managed-process recovery containment and deterministic eval proof for the process-runtime recovery boundary
- [x] this workstream now also ships the first Batch AU aggregate for operator control-plane governance, usage/runtime posture synthesis, and collaboration-safe handoff receipts across approvals, workflows, audit, continuity, and extension state
- [x] this workstream now also ships the first Batch AW aggregate for broader authenticated adapter operations, PR-native `code_activity.write` create or review actions, fixed-argument bounded external execution, and report-publication contract selection that stays truthful across work-item versus pull-request publication paths
- [x] this workstream now also ships the first Batch BA aggregate for explicit background-process confirmation policy, richer managed-process trust receipts, and deterministic eval proof that the process-runtime boundary stays both session-partitioned and operator-visible
- [x] this workstream now also ships the second Batch BA aggregate for disposable worker lifecycle isolation, explicit credential-egress policy surfaced across tool/workflow/operator APIs, and preserved trust-partition receipts for delegated plus background-handoff execution
- [x] this workstream now also ships the first Batch BD aggregate for long-running workflow operating-layer density across the operator API, cockpit orchestration lane, and runtime eval harness, including long-run state compaction, queue-state supervision, denser repair paths, branch/output debugger context, and explicit long-horizon workflow proof
- [x] this workstream now also ships the first Batch BJ aggregate for anticipatory workflow repair, backup-branch planning, condensation-fidelity visibility, a named workflow-endurance benchmark suite, operator-readable failure taxonomy, and cockpit pre-repair plus backup-branch control
- [x] this workstream now ships the M2 completion batch for integrated execution proof: `m2_execution_supremacy`, artifact registry receipts, explicit #435 security-gauntlet scenarios, and repair of the PR #445 shard-8/shard-9 regressions inside the same milestone PR

## Still To Do On `develop`

- [ ] richer live-provider browser/computer-use depth beyond the deterministic extract/html/screenshot proof and current local provider model
- [ ] deeper host/container-grade isolation beyond disposable worker roots, session-scoped process handles, sandbox timeout/nonzero receipts, and Batch BW secure-host hardening receipts
- [ ] broader live crash/restart replay across arbitrary tools, external trigger executors, exactly-once scheduling semantics, and long-running multi-agent field evidence beyond the deterministic v2 durable-orchestration receipts
- [ ] broader external system leverage without weakening trust boundaries or the new credential-egress contract

## M2 Batch Acceptance Notes

- execution capability metadata now has one M2 readiness gate: `m2_execution_supremacy`.
- file mutation now includes patch preview/apply receipts with unified diffs, before/after hashes, occurrence checks, rollback hints, and stable artifact records.
- browser and HTTP-style execution keeps internal/private network protections on initial browser requests, browser subrequests, final/redirected targets, and DNS-resolved private addresses.
- prior PR #445 shard failures are fixed in this batch: transient tiktoken loading no longer permanently disables retry, and memory-provider fixtures now declare network plus secret-management boundaries required by extension quarantine policy.
- M2 completion is one PR: #427 and #435 close only when this integrated benchmark, artifact registry, operator proof, docs, and serial shard validation all land together.

## Current Slice Record

### `workflow-autonomy-supervision-and-artifact-control-v1`

- status: complete on `develop` via PR `#251`
- root cause addressed:
  - workflow recovery metadata already advertised retry-from-step and branch control, but later-step failures without reusable checkpoint state could still make the runs API behave as if that branch path was available
  - cockpit artifact chaining still assumed any workflow with a `file_path` input could consume any produced artifact, which made artifact-to-workflow handoff broad but not truthful
  - workflow run lineage already carried parent/root branch metadata, but the cockpit still presented runs as isolated rows, which meant operators could not inspect parent/peer/child branches or continue the latest branch directly from the workflow surface
- scope:
  - workflow loader metadata now carries explicit `artifact_input` and `artifact_types` fields so workflow inputs can declare real artifact handoff expectations instead of relying on name-only conventions
  - workflow runtime and audit payloads now persist reusable checkpoint context for safe branch-from-step reuse, preserve structured step/failure context on hard workflow failures, and carry control lineage such as parent/root run identity and branch depth
  - the workflows API now exposes truthful checkpoint availability without letting unsupported later-step checkpoints 409 the entire runs list; unsupported checkpoints stay visible but non-resumable until a caller explicitly requests them
  - cockpit workflow and artifact inspectors now bind artifacts only into compatible workflow inputs, surface checkpoint-driven branch/retry actions from concrete checkpoint candidates, expose attached approval decisions directly from workflow rows and inspectors, and derive workflow branch families from lineage so operators can open parent runs, inspect peer/child branches, and continue the latest branch without leaving the workflow surface
  - regression coverage now pins checkpoint reuse, hard-failure audit payload shape, unsupported-checkpoint handling, typed artifact draft binding, and cockpit artifact/branch-family supervision controls
- local regression fixed before the slice stayed complete:
  - the first API pass still auto-selected the failed step as the default resume target even when that checkpoint had no reusable state, which made some degraded `GET /api/workflows/runs` responses fail with `409 Conflict`
  - fixed by skipping unsupported checkpoints for implicit/default resume selection while keeping explicit `/resume-plan` requests fail-closed for those same checkpoints
- PR review follow-up fixed before merge:
  - workflow parent-run checkpoint lookup still treated `session_id + tool_name + fingerprint` as unique, which meant two runs with identical arguments could restore checkpoint state from the wrong sibling run
  - workflow audit-call fingerprinting still hashed raw control inputs while execution hashed normalized controls, which could split `tool_call` and `tool_result` events for semantically identical runs
  - fixed by carrying a unique `call_event_id` discriminator from the audit call into workflow result/failure payloads, using that discriminator in workflow run identity and checkpoint restore lookup, and fingerprinting audit-call payloads from the same normalized inputs that execution uses
- validation:
  - `python3 -m py_compile backend/src/tools/audit.py backend/src/workflows/loader.py backend/src/workflows/manager.py backend/src/workflows/run_identity.py backend/src/api/workflows.py backend/tests/test_workflows.py`
  - `cd backend && .venv/bin/python -m pytest tests/test_workflows.py -q`
    - result: `64 passed`
  - `cd frontend && NODE_OPTIONS=--experimental-require-module npm test -- --run src/components/settings/workflowDraft.test.ts src/components/cockpit/CockpitView.test.tsx`
    - result: `52 passed`
  - `cd frontend && npm run build`
    - result: `passed`
- subagent review:
  - two focused review passes were started for bugs, regressions, and hallucinated assumptions after the runtime plus cockpit slice landed
  - both reviewer runs stalled before returning usable findings, and the follow-up branch-family supervision slice used the same direct diff verification plus targeted backend/frontend validation instead of claiming an unreturned clean review

### `capability-execution-host-v1` (#747 remediation on this branch)

- [x] Adopted local filesystem and process calls cross one authority-wrapper-owned execution host after the existing principal, session, revocation, and approval checks. The public host resolves fixed module-owned adapters, rejects constructor handler injection, and rejects an adopted-name wrapper that is not the exact bundled adapter.
- [x] The host binds owner, capability/version, normalized local destination, request digest, limits, and fencing material into a bounded HMAC effect identity. Retries with the same binding are deduplicated without persisting caller-controlled idempotency keys; cross-owner reuse produces a distinct effect key.
- [x] A `0600` atomic local journal records prepared, started, succeeded, failed, and uncertain states with digest-only arguments and bounded output summaries. Its journal and effect MACs derive from configured server secret material (`CAPABILITY_JOURNAL_SECRET` or the operator secret/hash) and fail closed when no key is available; a path-only key is never accepted. An OS lock serializes claims across processes, integrity/schema checks fail closed, and post-effect receipt failures become uncertain and deny retry. Restart reconciliation marks unfinished effects uncertain and refuses automatic replay.
- [x] Capability execution resolves the authenticated runtime principal, operator session, durable job identity, and lease fence from server-bound context. Approval-bearing effects require a repository-issued, consumed, MAC-bound approval receipt; caller-provided authority or approval flags are not accepted. Consuming an approval before effect execution is fail-closed across a crash: an interrupted handoff can lose the approval, but cannot authorize an unapproved retry.
- [x] **Branch-local #748 follow-up; intended until the epic integration PR lands:** native software-engineering service runs bind approval rows to the canonical service owner principal and a stable service approval-owner session; the native consumer supplies both bindings, while generic approval selection and consumption remain fail-closed for missing or mismatched identity fields.
- [x] Journal corruption or incomplete restart recovery blocks the host and is exposed through the authenticated `/api/operator/capability-execution` recovery projection. The projection is redacted to effect state, bounded error codes, and timestamps so operators can see repair-required and uncertain states without receiving effect arguments or workspace paths.
- [x] Native software-engineering process and workspace-patch paths use the same governed host, including the durable job owner and lease fence in their effect binding.
- [x] The local host accepts only `local://`, `workspace://`, and `seraph://` destinations; browser and connector network effects remain outside this adopted set and are denied at this boundary.
- [x] Process children retain the existing workspace/path and process-group controls, with CPU, address-space, PID, file-output, timeout, bounded foreground display, and bounded background-log reads applied at the child boundary. Foreground stdout/stderr are drained into capped buffers before return, workspace file reads are byte-bounded before decoding, network-capable git/npm/uv subcommands and flags are denied at argument validation, Git positional operands, pathspecs, and path-valued options are resolved against the bound workspace before child creation, and arbitrary workspace executable paths are rejected; workspace scripts run only as arguments to the allowlisted interpreter commands.
- [x] Native software-engineering adapters receive raw process receipts only through a module-owned internal host seam; the public capability request and adapter arguments reject the legacy `__seraph_raw_result` escape hatch.
- [ ] Full database-backed multi-process lease/fencing proof and host/container-grade isolation remain deferred. Filesystem writes now use descriptor-relative, no-follow opens for the workspace path, while the host still runs in the Seraph process and cannot claim container or OS-user isolation. Arbitrary workspace executables are therefore disabled; interpreter script marker checks are defense in depth rather than a network boundary. Detached descendants can still produce an operator-visible `unknown` cleanup state under the existing process contract.
- [ ] Browser and connector adapters remain outside this adopted local capability set and therefore have no execution authority through this host until they define their own authenticated, approval-bound, and redacted boundary.

## Non-Goals

- adding tools just to increase the count
- unbounded process execution with weak policy control

## Interface Checklist

- [x] native tools are auto-discoverable through the registry
- [x] MCP tools can be added and removed without code changes
- [x] tool execution is visible to the user

## Acceptance Checklist

- [x] Seraph can browse, search, read/write local files, inspect goals, and use the shell
- [x] Seraph can use connected MCP servers in the current runtime
- [x] Seraph can execute richer cross-tool workflows than it could before the reusable workflow runtime
- [x] Seraph can expose workflow replay and safety context back to the operator instead of treating runs as opaque
