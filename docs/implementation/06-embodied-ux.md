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
- [x] cockpit operator-terminal density now also includes an active triage lane for pending approvals, workflow branch families, queued guardian items, and reach failures with direct continue, open-thread, latest-branch, approve or deny, and desktop-shell actions instead of forcing operators to scan four separate panes for the next action
- [x] cockpit operator-terminal density now also includes evidence shortcuts for approval context, recent trace, and artifact lineage plus keyboard-first inspect, approve, continue, open-thread, redirect, and evidence-inspect shortcuts so operators can act on active work without pane-hopping
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
- [x] this workstream now ships `cockpit-density-and-cross-surface-command-control-v2` with active triage, denser evidence shortcuts, and keyboard-first operator control for approvals, workflow recovery, queued guardian items, degraded reach, artifacts, and trace
- [x] this workstream now hands the queue forward to a visual workflow debugger, richer cockpit density, and deeper studio ergonomics rather than first-pass branch/resume control

## Still To Do On `develop`

- [ ] richer capability installation, recommendation, and command-surface guidance inside the cockpit so shipped tools, skills, workflows, and blocked states become easier to bootstrap automatically, not only preflight, repair, bounded bootstrap, and first studio save flows
- [ ] richer workflow history, broader keyboard/operator control, visual branch/resume step-level visibility, and more flexible workspace ergonomics inside the cockpit beyond the first dedicated workflow-run layer, Activity Ledger, pane model, extension studio, and saved-layout composition model
- [ ] richer ambient indicators and any surviving embodiment strictly subordinate to the cockpit
- [ ] stronger mobile and cross-surface UX coherence

## Current Branch Record

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
