# Workstream 06: Embodied Interface

## Status On `develop`

- [ ] Workstream 06 is only partially shipped on `develop`.

## Paired Research

- primary design doc: [07. Embodied Interface](/research/embodied-interface)

## Shipped On `develop`

- [x] browser guardian workspace shell with dense multi-pane operator surfaces, fixed composer, sessions, priorities, recent outputs, pending approvals, latest response, guardian state, workflow runs, interventions, audit surface, live trace, desktop continuity, operator controls, and an operations inspector
- [x] dedicated cockpit workflow-run views with richer workflow inspector actions and artifact-lineage links
- [x] cockpit artifact and workflow inspectors can now draft compatible follow-on workflows directly from selected artifact paths
- [x] cockpit capability discovery now exposes tools, skills, workflows, MCP servers, starter packs, and blocked-state reasons in one operator-readable surface
- [x] first-class draggable and resizable cockpit panes with persisted positions and z-order
- [x] cockpit panes now snap to a shared 16px grid during drag, resize, and packed layout reset
- [x] persisted cockpit workspace presets for `default`, `focus`, and `review`, now implemented as function-based packed pane layouts with inspector visibility persistence, keyboard switching, and per-layout save/reset composition
- [x] cockpit-native bridge cues through live desktop status, pending native-notification state, deferred bundle visibility, recent intervention continuity, and browser-side native-presence controls in the operator surfaces
- [x] cockpit-native operator surface for workflow availability, skills, MCP servers, and live policy state with direct reload controls
- [x] separate Activity Ledger window that links workflow runs, approvals, notifications, queued interventions, recent guardian outputs, surfaced failures, tool steps, and attributed LLM calls back into one live browser control surface
- [x] flatter terminal-style workspace windows with close controls, visible resize grip, and a dedicated Windows menu for per-pane visibility and focus
- [x] cockpit capability preflight and autorepair flows for runbooks, starter packs, and workflows before the operator drafts or reruns them
- [x] cockpit capability bootstrap now applies only bounded low-risk local repair actions, while policy lifts, external server enables, installs, and starter-pack activation stay visible as explicit manual repair steps instead of silent operator-surface side effects, and multi-step privileged repair bundles now stop at a step-by-step execution boundary instead of chaining from one button
- [x] cockpit workflow views now expose richer step timestamps, duration, error summaries, recovery hints, and stored workflow diagnostics
- [x] cockpit now includes a first extension studio for workflows, skills, and MCP configs with validation, diagnostics, save flows, and repair handoff from operator surfaces
- [x] cockpit workflow views now expose first branch/resume checkpoints, lineage metadata, and resume drafts tied to existing inputs instead of only replay-from-start or retry-from-step cues
- [x] cockpit session continuity now restores the active thread on reload, preserves explicit fresh-thread semantics, and marks background thread activity in the session list
- [x] cockpit approvals, workflow runs, native notifications, queued interventions, and recent interventions now expose explicit continue/open-thread controls instead of forcing continuity guesswork
- [x] the cockpit presence pane and desktop shell now also expose synthesized continuity health, grouped thread follow-through, and explicit reach-repair/follow-up actions instead of leaving cross-surface recovery buried in raw route rows and item lists
- [x] the cockpit presence pane, desktop shell, and active triage now also surface imported capability-family attention and typed source-adapter degradation from the observer continuity contract instead of treating browser/native route health as the only actionable reach seam
- [x] cockpit operator-terminal density now also includes an active triage lane for pending approvals, workflow branch families, queued guardian items, and reach failures with direct continue, latest-output reuse, best-continuation comparison, next-step drafting, open-thread, latest-branch, approve or deny, and desktop-shell actions instead of forcing operators to scan four separate panes for the next action
- [x] cockpit operator-terminal density now also includes evidence shortcuts for approval context, recent trace, and artifact lineage plus keyboard-first inspect, approve, continue, open-thread, latest-branch, redirect, and evidence-inspect shortcuts so operators can act on active work without pane-hopping
- [x] cockpit workflow density now also exposes step-focus rows with direct step-context handoff, step-output handoff, repair or retry actions, richer workflow-row focus summaries, and keyboard-first top-workflow inspect/output shortcuts instead of leaving step-level debugging buried in generic timelines
- [x] cockpit workflow density now also exposes visual branch-debug summaries, explicit branch-origin and failure-lineage rows, and best-continuation controls with direct open, continue, and output reuse actions instead of leaving branch debugging as implicit lineage metadata
- [x] cockpit workflow density now also exposes family-history comparison summaries, family-output reuse, direct compare-output drafts across workflow branches, family-row checkpoint drill-in, direct family-row retry/repair controls, and bundled next-step planning drafts from workflow family state, so operators can compare sibling or ancestor runs, reuse the freshest useful family output, branch or retry from family checkpoints, repair failed family steps, and draft continuation plans without reconstructing lineage manually
- [x] cockpit artifact control now also exposes source-run provenance when lineage is uniquely visible, related family outputs, follow-on workflow rows, artifact next-step drafting, and keyboard-first artifact inspect/plan/run shortcuts instead of treating artifacts as generic file-context handoff only
- [x] onboarding can now inspect an explicitly user-linked webpage during the onboarding turn, so profile and workspace context can be grounded in a real source without widening onboarding into general web search
- [x] activity ledger rows now surface routing summaries, selected reason codes, rejected targets, native thread-source/continuation metadata, and per-call LLM token/cost attribution
- [x] activity ledger rows now group related request work into compact parent bundles with emoji/icon scanning, child tool/routing rows, and completion footers so the operator can browse a day of agent work without reconstructing it from raw trace output
- [x] cockpit is now the active browser shell on load rather than merely the default mode
- [x] priority and settings overlays now use workspace modal styling rather than legacy overlay frames
- [x] dormant village/editor runtime code and legacy browser entry points are removed from the active product path rather than treated as fallback surfaces

## Working On Now

- [x] this workstream is back near the front of the repo-wide horizon through cockpit-density and operator-control work
- [x] this workstream shipped `cockpit-linked-evidence-panels-v2` and `saved-layouts-and-keyboard-control-v1`
- [x] this workstream partnered on `native-desktop-shell-v1`
- [x] this workstream partnered on `cross-surface-continuity-and-notification-controls`
- [x] this workstream now ships `cockpit-workflow-views-v1`
- [x] this workstream now ships `artifact-evidence-roundtrip-v2`
- [x] this workstream now ships `extension-operator-surface-v1`
- [x] this workstream now ships the denser operator-terminal layer with live operator feed, saved runbook macros, approval-aware workflow timeline actions, and a separate Activity Ledger window
- [x] this workstream now ships `workflow-step-focus-and-handoff-v1`
- [x] this workstream now ships `cockpit-density-and-cross-surface-command-control-v2` with active triage, denser evidence shortcuts, and keyboard-first operator control for approvals, workflow recovery, queued guardian items, degraded reach, artifacts, and trace
- [x] this workstream now also ships `workflow-branch-debugging-and-long-running-control-v1`
- [x] this workstream now also ships `workflow-history-comparison-and-family-output-control-v1`
- [x] this workstream now also ships `workflow-output-comparison-drafts-and-family-diff-control-v1`
- [x] this workstream now also ships `workflow-family-checkpoint-drilldown-and-step-control-v1`
- [x] this workstream now also ships `workflow-family-recovery-control-parity-v1`
- [x] this workstream now also ships `artifact-lineage-and-follow-on-control-v1`
- [x] this workstream now hands the queue forward to richer long-running control, broader keyboard/operator density, and deeper studio ergonomics rather than first-pass workflow family history, output reuse, comparison drafts, family-plan bundling, triage quick actions, the first best-continuation keyboard layer, and the first family-row follow-through parity layer

## Still To Do On `develop`

- [ ] richer capability installation, recommendation, and command-surface guidance inside the cockpit so shipped tools, skills, workflows, and blocked states become easier to bootstrap automatically, not only preflight, repair, bounded bootstrap, and first studio save flows
- [ ] broader long-running workflow control, richer keyboard/operator density, and more flexible workspace ergonomics inside the cockpit beyond the current workflow-run layer, branch debugger, family-history comparison/output-diff layer, Activity Ledger, pane model, extension studio, and saved-layout composition model
- [ ] richer ambient indicators and any surviving embodiment strictly subordinate to the cockpit
- [ ] stronger mobile and cross-surface UX coherence

## Current Branch Record

### `artifact-lineage-and-follow-on-control-v1`

- status: in progress on `feat/long-running-workflow-control-batch-ah-v1`
- root causes addressed:
  - the cockpit already had dense workflow-family control, but artifacts were still treated mostly as generic file-context handoff with one or two compatible workflow buttons
  - operators still had to jump back into workflow lineage views to understand which run produced an artifact, which related outputs existed in the same family, and what the best follow-on action was
  - keyboard-first control still had no artifact-specific planning path even though artifact evidence was already one of the freshest operator surfaces
- scope:
  - artifact inspectors now surface verified source-run provenance when available, explicit unresolved lineage when it is not, related family outputs, follow-on workflow rows, compare/use controls, and direct source-failure reuse
  - evidence shortcuts now draft artifact next-step plans instead of collapsing every artifact into generic file-context reuse
  - keyboard-first control now also includes artifact-specific inspect, next-step, and suggested-follow-on shortcuts
  - cockpit regression coverage now pins the richer artifact lineage/follow-on contract end-to-end
- review findings fixed during implementation:
  - the first pass left a real TypeScript nullability hole in the artifact-family-output helper, which broke the frontend build
  - the first test pass also overfit exact draft strings and assumed the Recent outputs pane title would be the easiest stable selector, which was not true in the rendered window chrome
  - the first artifact-lineage pass could also attribute a file to the wrong workflow when several recent runs wrote the same path, and it used a narrower session-scoped workflow window than the artifact list itself
  - fixed by failing closed on nullable or ambiguous lineage helpers, resolving artifact provenance from a broader lineage window, tightening the artifact tests around stable visible contract rather than exact prose, and binding the new artifact control proof to the evidence shortcut plus inspector surfaces directly
- validation:
  - `cd frontend && NODE_OPTIONS=--experimental-require-module npx vitest run src/components/cockpit/CockpitView.test.tsx`
    - result: `55 passed`
  - `cd frontend && npm run build`
    - result: `passed`

### `onboarding-web-context-v1`

- status: in progress on `feat/onboarding-web-context-v1`
- root causes addressed:
  - onboarding was intentionally limited to guardian-record and goal-capture tools, but that also meant Seraph could not inspect a homepage, portfolio, company page, or similar source even when the user pasted the exact URL into onboarding
  - simply enabling general browser/search tools in onboarding would over-widen the boundary instead of honoring only explicit user-provided context
- scope:
  - onboarding now derives a narrow browser allowance from the current user message
  - `browse_webpage` is only exposed when the onboarding turn includes an explicit `http(s)` URL
  - onboarding instructions now bind the agent to the exact pasted URL set and explicitly forbid general browsing or web search in onboarding mode
  - the onboarding browser wrapper now also enforces that exact URL set at runtime instead of relying on prompt text alone
- validation:
  - `python3 -m py_compile backend/src/agent/onboarding.py backend/src/api/chat.py backend/src/api/ws.py backend/tests/test_tool_audit.py backend/tests/test_chat_api.py backend/tests/test_websocket.py`
    - result: `passed`
  - `cd backend && .venv/bin/python -m pytest tests/test_tool_audit.py tests/test_chat_api.py tests/test_websocket.py tests/test_browser_tool.py tests/test_onboarding_edge_cases.py -q`
    - result: `27 passed`
  - `cd docs && npm run build`
    - result: `passed`
  - `git diff --check`
    - result: `passed`
- review:
  - focused branch review against bugs, regressions, and hallucinated assumptions found two material issues in the first pass
  - the first implementation only described the “exact URL only” onboarding boundary in prompt text while still exposing the generic browser tool underneath, which would have widened onboarding to any globally allowed site instead of only the pasted URL set
  - the first URL normalizer also left trailing quotes on commonly pasted links, which could make the onboarding browser wrapper reject or mis-browse the page the user actually provided
  - fixed by wrapping `browse_webpage` in an onboarding-specific runtime gate bound to the extracted URL set and by normalizing quoted/punctuated links before both instruction rendering and runtime comparison

### `cockpit-density-and-cross-surface-command-control-v2`

- status: complete on `feat/cockpit-density-batch-k-v1` and ready for PR
- root causes addressed:
  - the cockpit already exposed approvals, workflow families, queued guardian items, artifacts, trace, and reach health, but operators still had to scan multiple panes and inspector stacks to find the next blocked or time-sensitive action
  - cockpit command control was still too mouse-heavy for active workflow supervision, especially when operators needed to inspect, approve, continue, redirect, or reopen the most urgent item quickly
  - the first Batch K density pass reused existing approval and workflow labels verbatim, which made the denser surface ambiguous and broke test assumptions about unique row text and generic action names
- scope:
  - the operator terminal now derives one active triage list from pending approvals, workflow families, queued guardian items, and non-ready reach routes
  - triage entries carry contextual labels plus direct continue, approve or deny, open-thread, latest-branch, inspect, and desktop-shell actions without leaving the operator terminal
  - the operator terminal now also surfaces evidence shortcuts for the freshest artifact output, recent trace evidence, and approval context so operators can inspect and act on linked evidence without pane-hopping
  - keyboard-first shortcuts now cover inspect, approve, continue, open-thread, workflow redirect, and evidence inspection flows for the top active items, while still deferring when focus is inside editable inputs
- review findings fixed before the batch stayed complete:
  - the first density pass duplicated approval and workflow row text exactly, which caused cockpit tests to fail because the new triage and evidence rows collided with older operator and studio assertions
  - the first workflow-density inspector query was too broad for the denser surface and could match duplicated approval context text outside the intended inspector stack
  - the branch-family helper could also point a branch row's "latest branch" control back at the parent/root run instead of a real branch descendant or peer
  - the first evidence-shortcut pass advertised `Shift+E` as "inspect latest evidence" while still picking the first inserted evidence row instead of the newest one, and the new evidence-shortcut test mock expected a compatible workflow draft without actually publishing that workflow
  - the artifact shortcut was still choosing the newest workflow before the newest artifact, so a run with fresher workflow metadata could hide a newer artifact produced by a different run
  - the first GitHub frontend run also showed the new evidence-shortcut test was asserting as soon as the section mounted, before the asynchronous workflow and approval rows had populated in CI
  - fixed by switching triage and evidence labels plus action `aria-label`s to contextual names, tightening the inspector test scope, excluding ancestor/root fallbacks from latest-branch resolution, selecting artifact shortcuts by actual artifact recency instead of workflow recency, sorting evidence shortcuts by actual recency before binding `Shift+E`, and waiting for the evidence rows themselves instead of the section shell before asserting dense-control behavior
- validation:
  - `cd frontend && NODE_OPTIONS=--experimental-require-module npx vitest run src/components/cockpit/CockpitView.test.tsx`
    - result: `52 passed`
  - `cd frontend && npm run build`
    - result: `passed`
  - `git diff --check`
- review:
  - focused branch review against bugs, regressions, and hallucinated assumptions found the branch-family latest-branch bug plus the stale-evidence `Shift+E` shortcut bug and the inconsistent evidence-shortcut workflow mock above
  - no additional material issues remained after those fixes and the targeted frontend validation

### `workflow-step-focus-and-handoff-v1`

- status: complete on `feat/workflow-operator-density-batch-o-v1` and ready for PR
- root causes addressed:
  - the cockpit already showed workflow timelines and artifacts, but failed or recoverable steps were still too easy to miss because the operator had to reconstruct the hottest step from generic timeline rows and inspector stacks
  - workflow rows still exposed replay and approval controls, but they did not offer a direct handoff path from the current failed step or the latest workflow output back into the command surface
  - the first shortcut pass for `Shift+W` just inspected the top workflow triage entry, which could point at the newest branch instead of the most operator-useful run with step-focus evidence
- scope:
  - workflow timeline rows now surface prioritized step-focus summaries plus direct `Use failure context` and `Use latest output` actions
  - the workflow inspector now promotes focused step rows with direct context handoff, output reuse, repair, retry, and compatible follow-on workflow actions
  - keyboard-first control now also includes `Shift+W` for top-workflow inspection and `Shift+U` for the latest workflow output handoff
- review findings fixed before PR:
  - `Shift+W` initially chose the newest workflow triage entry instead of the best step-bearing run, so the shortcut could open a branch without the dense step context the shortcut was supposed to surface
  - the new step-focus layer also exposed stale uniqueness assumptions in older cockpit tests that expected certain workflow summary strings and `Use Output` controls to appear only once
  - fixed by preferring the best step-bearing workflow candidate for `Shift+W` and by tightening the older tests so they assert presence rather than accidental uniqueness
- validation:
  - `cd frontend && NODE_OPTIONS=--experimental-require-module npx vitest run src/components/cockpit/CockpitView.test.tsx -t "surfaces evidence shortcuts and keyboard-first triage control"`
    - result: `passed`
  - `cd frontend && NODE_OPTIONS=--experimental-require-module npx vitest run src/components/cockpit/CockpitView.test.tsx -t "surfaces workflow approval, artifact, and trace density inside the inspector"`
    - result: `passed`
  - `cd frontend && NODE_OPTIONS=--experimental-require-module npx vitest run src/components/cockpit/CockpitView.test.tsx -t "surfaces workflow runs in the cockpit inspector"`
    - result: `passed`
  - `cd frontend && npm run build`
    - result: `passed`
  - `cd docs && npm run build`
    - result: `passed`
  - `git diff --check`

### `workflow-branch-debugging-and-long-running-control-v1`

- status: complete on `feat/workflow-branch-debug-batch-s-v1` and ready for PR
- root causes addressed:
  - the cockpit already carried truthful branch/checkpoint metadata, but operators still had to infer branch origin, the best continuation target, and recent family failures from scattered chips, action rows, and timestamps
  - branch-family debugging still favored "latest branch" over "best continuation", which is not the same thing once a family has multiple resumable or failed descendants
  - failed-branch history was still visible only indirectly through generic timelines or parent/child rows, making long-running workflow recovery harder than it needed to be
- scope:
  - workflow rows now include concise branch-debug summaries for origin, best continuation, and latest family failure
  - the workflow inspector now exposes explicit branch-origin, best-continuation, and failure-lineage rows with direct open, continue, and output reuse actions
  - the branch-debug surface stays on the existing workflow-family lineage model instead of inventing a second workflow-debug state machine
- review findings fixed before PR:
  - the first pass surfaced multiple legitimate `recovery ready` and `Open Parent` affordances, which broke older uniqueness assumptions in the branch-family cockpit test
  - fixed by tightening the branch-family test to assert presence of the new debugger surface without treating duplicated valid affordances as regressions
- validation:
  - `cd frontend && NODE_OPTIONS=--experimental-require-module npx vitest run src/components/cockpit/CockpitView.test.tsx -t "surfaces workflow branch families and can continue the latest branch"`
    - result: `passed`
  - `cd frontend && npm run build`
    - result: `passed`
  - `cd docs && npm run build`
    - result: `passed`
  - `git diff --check`
    - result: `passed`

### `workflow-history-comparison-and-family-output-control-v1`

- status: in progress on `feat/workflow-history-comparison-batch-t-v1`
- root causes addressed:
  - Batch S made branch origin and best continuation visible, but operators still had to infer how sibling, child, and ancestor runs differed or which family output was most useful for reuse
  - workflow history across a family still favored one-run inspection instead of giving the operator a denser comparison surface tied to the current workflow
- scope:
  - workflow-family rows now expose comparison summaries such as newer or older state, status deltas, and output count against the current inspected run
  - the workflow inspector now surfaces family-output rows across the workflow family, each with direct open-run and use-output actions
  - child and peer branch rows now also expose direct output reuse when that branch carries a reusable artifact
- review findings fixed during implementation:
  - the first pass treated current-run outputs as family outputs, which let the new family-output surface duplicate the existing current-run `Use Output` path instead of highlighting sibling and ancestor artifacts
  - the first pass also used non-unique family-output action labels and left the peer-branch output path unpinned in the cockpit test
  - fixed by excluding the inspected run from family-output inventory, qualifying family-output actions by source run, and expanding the branch-family test to cover peer-output reuse while tightening older assertions that relied on accidental uniqueness
- validation:
  - `cd frontend && NODE_OPTIONS=--experimental-require-module npx vitest run src/components/cockpit/CockpitView.test.tsx -t "surfaces workflow branch families and can continue the latest branch"`
    - result: `passed`
  - `cd frontend && NODE_OPTIONS=--experimental-require-module npx vitest run src/components/cockpit/CockpitView.test.tsx`
    - result: `53 passed`
  - `cd frontend && npm run build`
    - result: `passed`
  - `cd docs && npm run build`
    - result: `passed`
  - `git diff --check`
    - result: `passed`

### `workflow-output-comparison-drafts-and-family-diff-control-v1`

- status: in progress on `feat/workflow-output-comparison-batch-u-v1`
- root causes addressed:
  - Batch T made workflow family history and family-output reuse visible, but operators still had to manually reconstruct a comparison prompt when they wanted to inspect how the current run differed from a child, peer, ancestor, or family-output run
  - the cockpit already knew the relevant family relationships and output paths, but it did not turn that lineage into a direct comparison action
- scope:
  - branch-family rows now expose direct compare-output drafts when both the inspected workflow and the related run have reusable outputs
  - family-output inventory rows now also expose compare actions when the inspected workflow has its own reusable output
  - comparison actions stay fail closed when either side lacks a reusable artifact instead of advertising a fake diff path
- review findings fixed during implementation:
  - this surface reuses the same dense inspector rows that already produced stale uniqueness assumptions in earlier cockpit tests, so the first review focus was making the new compare actions source-aware and testable instead of adding another ambiguous generic `Compare` control
  - fixed by adding source-qualified compare action labels, deriving comparison drafts only from truthful output pairs, and expanding the branch-family cockpit test to pin current-vs-child and current-vs-peer comparison drafts directly
- validation:
  - `cd frontend && NODE_OPTIONS=--experimental-require-module npx vitest run src/components/cockpit/CockpitView.test.tsx -t "surfaces workflow branch families and can continue the latest branch"`
    - result: `passed`

### `workflow-family-action-bundles-and-continuation-planning-v1`

- status: in progress on `feat/workflow-family-action-bundles-batch-v1`
- root causes addressed:
  - Batch U made workflow family comparison and output reuse actionable, but operators still had to manually reconstruct one coherent next-step prompt from best continuation, latest family failure, and reusable family outputs
  - the cockpit already had the truthful family state needed for that synthesis, but it still exposed those signals as separate actions instead of one bundled planning handoff
- scope:
  - the workflow inspector now exposes a direct `Draft Next Step` family action that bundles the current output, best continuation, latest family failure, and reusable family outputs when those signals actually exist
  - keyboard-first control now also includes `Shift+P` for drafting that family next-step bundle from the primary workflow target
  - the bundled family-plan draft stays fail closed when the inspected workflow has no truthful family continuation or output context to hand off
- review findings fixed during implementation:
  - this surface depends on the same dense lineage fixture used by the branch-family cockpit test, so the first review focus was making the new family-plan draft deterministic instead of adding another unpinned action row
  - the first pass also rendered the visible `Draft Next Step` action only when a best continuation row existed, even though the bundled planner can also operate from current output plus family-output or failure context alone
  - fixed by moving the action onto its own workflow-family row and expanding the cockpit test coverage to pin both the full bundled draft and the no-best-continuation fallback
- validation:
  - `cd frontend && NODE_OPTIONS=--experimental-require-module npx vitest run src/components/cockpit/CockpitView.test.tsx -t "surfaces workflow branch families and can continue the latest branch"`
    - result: `passed`
  - `cd frontend && NODE_OPTIONS=--experimental-require-module npx vitest run src/components/cockpit/CockpitView.test.tsx`
    - result: `54 passed`
  - `cd frontend && npm run build`
    - result: `passed`
  - `cd docs && npm run build`
    - result: `passed`
  - `git diff --check`
    - result: `passed`

### `workflow-triage-quick-actions-and-follow-through-control-v1`

- status: in progress on `feat/workflow-triage-quick-actions-batch-w-v1`
- root causes addressed:
  - Batch V made bundled workflow-family planning available once the operator opened the inspector, but the active triage lane still treated workflows mostly as inspect-first rows
  - the triage lane already knew when a workflow had reusable output, a distinct best continuation, or a latest branch, but those follow-through actions still required an inspector hop
- scope:
  - workflow triage rows now expose direct `use output`, `compare best`, and `draft next step` actions when the underlying family/output state is truthful
  - keyboard-first workflow control now also includes `Shift+L` for opening the latest branch on the primary workflow target
  - compare-best stays fail closed when the current run and best continuation resolve to the same output path
- review findings fixed during implementation:
  - the first pass would have allowed `compare best` to compare a workflow output against the same file path, which is not a real comparison surface
  - the first pass also still let some workflows treat the current run as its own “best continuation,” which would have made the new triage actions surface self-comparisons and weaker next-step guidance
  - fixed by preferring a distinct family continuation over the current run when available and by suppressing triage comparison when both sides resolve to the same output path
- validation:
  - `cd frontend && NODE_OPTIONS=--experimental-require-module npx vitest run src/components/cockpit/CockpitView.test.tsx -t "surfaces active triage for approvals, workflows, queued guardian items, and reach failures"`
    - result: `passed`
  - `cd frontend && NODE_OPTIONS=--experimental-require-module npx vitest run src/components/cockpit/CockpitView.test.tsx -t "surfaces workflow branch families and can continue the latest branch"`
    - result: `passed`

### `workflow-triage-branch-debug-and-best-continuation-control-v1`

- status: complete on `feat/workflow-triage-branch-debug-batch-x-v1`, intended for the aggregate Batch X PR for `#283`
- root causes addressed:
  - Batch W made triage useful for output reuse, comparison, and next-step drafting, but the hottest workflow rows still hid failure-context reuse and direct best-continuation control behind an inspector hop
  - the operator shell still needed broader keyboard-first workflow branch-debug control beyond inspect/latest-branch/output-only shortcuts
- scope:
  - workflow triage rows now expose direct `use failure`, `open best`, and `continue best` actions when the underlying failed-step and family-continuation state is truthful
  - keyboard-first workflow control now also includes `Shift+F` for failure-context reuse on the primary workflow target
  - the latest family failure used for triage reuse is now sorted by actual workflow recency instead of unsorted family order
- review findings fixed during implementation:
  - the first pass was reusing the first failed family run instead of the newest failed family run, which could surface stale failure context from older branches
  - the first pass also needed explicit distinct-continuation gating so triage would not surface `open best` or `continue best` for a workflow that only resolved back to itself
  - fixed by sorting failure lineage by workflow recency before extracting failure context and by suppressing best-continuation triage actions unless a distinct family continuation actually exists
- validation:
  - `cd frontend && NODE_OPTIONS=--experimental-require-module npx vitest run src/components/cockpit/CockpitView.test.tsx -t "surfaces active triage for approvals, workflows, queued guardian items, and reach failures"`
    - result: `passed`
  - `cd frontend && NODE_OPTIONS=--experimental-require-module npx vitest run src/components/cockpit/CockpitView.test.tsx`
    - result: `passed`
  - `cd frontend && npm run build`
    - result: `passed`
  - `cd docs && npm run build`
    - result: `passed`
  - `git diff --check`
    - result: `passed`

### `workflow-triage-recovery-controls-v1`

- status: complete on `feat/workflow-triage-recovery-controls-batch-y-v1`, intended for the aggregate Batch Y PR for `#285`
- root causes addressed:
  - Batch X made workflow triage strong for follow-through, but the hottest failed or blocked workflows still hid retry-step, repair-step, and replay-repair controls behind the workflow timeline or inspector
  - keyboard-first workflow control still had no direct recovery draft path for the primary workflow target
- scope:
  - workflow triage rows now expose direct `retry step`, `repair step`, and `repair replay` actions when the underlying workflow state truthfully supports them
  - keyboard-first workflow control now also includes `Shift+T` for drafting the top workflow recovery path, preferring retry-step over generic rerun when available
  - triage recovery controls stay fail closed when replay is blocked without repair actions or when the failed step has no repair path
- review findings fixed during implementation:
  - the first triage recovery test still used an approval-style failed-step action, which did not exercise the same repair path as the operator surface and produced a false red assertion
  - fixed by aligning the test fixture with a real repair action and by proving replay-repair separately with a blocked workflow that exposes replay repair actions without a retry-step path
- validation:
  - `cd frontend && NODE_OPTIONS=--experimental-require-module npx vitest run src/components/cockpit/CockpitView.test.tsx -t "surfaces active triage for approvals, workflows, queued guardian items, and reach failures"`
    - result: `passed`
  - `cd frontend && NODE_OPTIONS=--experimental-require-module npx vitest run src/components/cockpit/CockpitView.test.tsx`
    - result: `passed`
  - `cd frontend && npm run build`
    - result: `passed`
  - `cd docs && npm run build`
    - result: `passed`
  - `git diff --check`
    - result: `passed`

### `workflow-keyboard-continuation-and-comparison-control-v1`

- status: in progress on `feat/workflow-keyboard-continuation-batch-z-v1`
- root causes addressed:
  - Batch Y made the triage lane dense and truthful, but the highest-value best-continuation actions were still partly mouse-only even though the same triage state was already available to the keyboard layer
  - the keyboard surface could inspect the latest branch or draft the next step, but it still could not directly open the best continuation, continue that branch, or compare against it
- scope:
  - keyboard-first workflow control now also includes `Shift+B` for opening the best continuation on the primary workflow target
  - keyboard-first workflow control now also includes `Shift+N` for continuing the best continuation on the primary workflow target when that continuation is distinct and resumable
  - keyboard-first workflow control now also includes `Shift+G` for comparing the current workflow output against the best continuation when both sides have distinct reusable outputs
- review findings fixed during implementation:
  - the shortcut layer is intentionally reusing the same `inspectWorkflowBestContinuation`, `continueWorkflowBestContinuation`, and `queueWorkflowBestContinuationComparison` helpers as the triage buttons so the keyboard path cannot drift into a second continuation model
  - compare-best remains fail closed for self-referential continuations or identical output paths, and continue-best remains fail closed when the resolved best continuation is not actually continuable
- validation:
  - `cd frontend && NODE_OPTIONS=--experimental-require-module npx vitest run src/components/cockpit/CockpitView.test.tsx -t "surfaces active triage for approvals, workflows, queued guardian items, and reach failures"`
    - result: `passed`
  - `cd frontend && NODE_OPTIONS=--experimental-require-module npx vitest run src/components/cockpit/CockpitView.test.tsx`
    - result: `passed`
  - `cd frontend && npm run build`
    - result: `passed`
  - `cd docs && npm run build`
    - result: `passed`
  - `git diff --check`
    - result: `passed`

### `workflow-family-row-control-parity-and-failure-follow-through-v1`

- status: in progress on `feat/workflow-checkpoint-control-batch-aa-v1`
- root causes addressed:
  - child-branch rows already exposed denser long-running actions, but ancestor, peer, and failure-lineage rows still dropped back to thinner one-off affordances even when they had reusable outputs or continuable context
  - the inspector therefore showed the family history truthfully, but did not let operators act on that truth with consistent density across the whole family surface
- scope:
  - ancestor rows now also expose direct output reuse whenever the ancestor carries a reusable output
  - peer rows now also expose direct continuation when the peer branch is genuinely continuable
  - failure-lineage rows now also expose direct continue and failure-context reuse when the failed branch actually supports those actions
- review findings fixed during implementation:
  - the first pass of the new failure-lineage control would have relied on a branch-family fixture that did not actually carry failed step-record context, which would have produced a false-positive test instead of proving the real inspector contract
  - fixed by upgrading the branch-family test fixture so the degraded child branch carries a real failed checkpoint step and by pinning the new ancestor/peer/failure-lineage actions directly in the existing family-history spec
- validation:
  - `cd frontend && timeout 60 env NODE_OPTIONS=--experimental-require-module npx vitest run src/components/cockpit/CockpitView.test.tsx -t "surfaces workflow branch families and can continue the latest branch"`
    - result: `passed`

### `workflow-family-checkpoint-drilldown-and-step-control-v1`

- status: in progress on `feat/workflow-family-checkpoint-control-batch-ab-v1`
- root causes addressed:
  - the workflow inspector could already continue family runs generically, but best-continuation, ancestor, peer, and failure-lineage rows still hid the actual checkpoint branch/retry actions that explain what that continuation would do
  - step-aware checkpoint control was therefore denser for the selected run timeline than for the related family rows that often carry the more relevant continuation target
- scope:
  - best-continuation, ancestor, peer, child, and failure-lineage rows now expose the existing checkpoint branch/retry drafts directly when those family runs publish checkpoint candidates
  - the new controls reuse the same `workflowCheckpointActions()` helper as the selected workflow timeline instead of inventing a second family-row checkpoint model
  - family-row checkpoint controls stay fail closed when the related run has no checkpoint candidates
- review findings fixed during implementation:
  - the first branch-family fixture still relied on continuation text alone for the failure-lineage row, which would not have proven the new step-aware action against a real failed checkpoint payload
  - fixed by upgrading the child-branch fixture to carry an explicit failed checkpoint step plus concrete checkpoint candidates for the relevant family rows and then pinning the new controls in the existing family-history spec
- validation:
  - `cd frontend && timeout 60 env NODE_OPTIONS=--experimental-require-module npx vitest run src/components/cockpit/CockpitView.test.tsx -t "surfaces workflow branch families and can continue the latest branch"`
    - result: `passed`
  - `cd frontend && timeout 120 env NODE_OPTIONS=--experimental-require-module npx vitest run src/components/cockpit/CockpitView.test.tsx`
    - result: `54 passed`
  - `cd frontend && npm run build`
    - result: `passed`
  - `cd docs && npm run build`
    - result: `passed`
  - `git diff --check`
    - result: `passed`

### `workflow-family-recovery-control-parity-v1`

- status: in progress on `feat/workflow-family-recovery-batch-ac-v1`
- root causes addressed:
  - family rows could already continue and branch from checkpoints, but they still hid the direct `retry step` and `repair step` actions that the triage surface exposed for the same failed workflow state
  - that left the workflow inspector inconsistent: the selected timeline and triage lane were recovery-aware, while best-continuation and failure-lineage family rows still stopped at generic continuation
- scope:
  - best-continuation and related family rows now expose direct `retry step` and `repair step` controls when the underlying failed step actually supports those actions
  - the new controls reuse `failedWorkflowStep()`, `repairWorkflowReplay()`, and existing recovery helpers instead of inventing a second family-row recovery model
  - family-row recovery controls stay fail closed when no failed step or repair action exists
- review findings fixed during implementation:
  - the first family-row recovery pass only exercised retry from existing continuation text, so it still did not prove that family-row `repair step` was grounded in a real failed-step recovery payload
  - fixed by upgrading the child-branch fixture to carry explicit `recovery_actions` on the failed checkpoint step and then pinning the new best-continuation and failure-lineage actions in the branch-family spec
- validation:
  - `cd frontend && timeout 60 env NODE_OPTIONS=--experimental-require-module npx vitest run src/components/cockpit/CockpitView.test.tsx -t "surfaces workflow branch families and can continue the latest branch"`
    - result: `passed`
  - `cd frontend && timeout 120 env NODE_OPTIONS=--experimental-require-module npx vitest run src/components/cockpit/CockpitView.test.tsx`
    - result: `passed`
  - `cd frontend && npm run build`
    - result: `passed`
  - `cd docs && npm run build`
    - result: `passed`
  - `git diff --check`
    - result: `passed`
  - `cd frontend && timeout 120 env NODE_OPTIONS=--experimental-require-module npx vitest run src/components/cockpit/CockpitView.test.tsx`
    - result: `passed`
  - `cd frontend && npm run build`
    - result: `passed`
  - `cd docs && npm run build`
    - result: `passed`
  - `git diff --check`
    - result: `passed`

## Non-Goals

- cosmetic polish detached from guardian value
- reviving the retired village/editor line as an active product surface
- game aesthetics without meaningful life-state reflection

## Acceptance Checklist

- [x] the interface feels intentionally different from a generic chatbot shell
- [x] tool use and agent activity are visible in the cockpit
- [x] the primary workflow surface is now a guardian workspace and the browser no longer boots into the village shell
- [x] the cockpit now has linked evidence, artifact, and approval density beyond the first shell
- [x] cockpit artifacts can now round-trip back into the command bar for the next operator step
- [x] cockpit artifacts can now also seed compatible follow-on workflow drafts directly from the inspector
- [x] the cockpit now supports draggable/resizable panes with 16px grid snapping
- [x] the cockpit now supports persisted packed `default`, `focus`, and `review` layouts plus keyboard switching for core navigation
- [x] the cockpit now supports saving and resetting each core workspace layout rather than only switching between fixed presets
- [x] the pane model now also supports per-pane hide/show state instead of forcing every preset-visible pane to stay permanently mounted
- [x] the cockpit now exposes first-class operator visibility for workflows, skills, MCP servers, and live policy state
- [x] the cockpit now exposes first-class operator visibility for tools, starter packs, blocked-state reasons, session continuity state, and preflight/autorepair outcomes
- [x] the cockpit now exposes first-class operator visibility for approval-thread recovery and cross-surface continue/open-thread actions
- [x] the cockpit now exposes first-class operator visibility for bounded capability bootstrap, richer workflow step diagnostics, and routing-summary timeline rows
- [x] the cockpit now exposes a first-class Activity Ledger window instead of leaving workflow, approval, notification, intervention, failure, and LLM-spend continuity split across separate panes
- [x] settings and priorities now present as workspace-styled modal overlays instead of legacy shell overlays
- [x] the docs and active repo now treat the village/editor line as removed history rather than as a fallback product branch
- [ ] the cockpit still needs richer install/recommend/repair guidance beyond the first "what can I do now?" capability surface, blocked-state explanation, starter-pack view, preflight/autorepair, and bounded bootstrap layer
- [ ] the cockpit still needs broader workflow history, deeper branch/resume step control, and more flexible workspace ergonomics
- [ ] the environment reflects the human’s life state and Seraph’s guidance with much higher fidelity
