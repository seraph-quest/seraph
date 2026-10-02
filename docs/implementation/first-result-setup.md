---
title: Guided First Result Setup
---

# Guided First Result Setup

**Status:** Partial, branch-local implementation; not shipped on `develop`.
**Scope:** guided setup through the existing Goal, source-watch, typed-input,
Work queue, activity ledger, and artifact readback contracts.

Home offers new operators two starter journeys. **Local goal snapshot** creates
an owner-bound goal with an artifact-readback success criterion, queues a typed
snapshot task, reads the canonical goal through the registered governed
workflow, and opens its verified local artifact. **Public source baseline + local
snapshot** first reads one selected public HTTPS text source and records its
canonical baseline. It then pauses the watch, queues the local snapshot, and
shows both the source observation and snapshot readback receipts.

Both starters are deterministic and have $0 model spend. They do not need a
provider credential or model-egress consent. The public-source journey accepts
URLs without credentials, query parameters, or fragments. It reads at most
256 KiB, uses a reviewed one-hour source grant, permits one job at a time and
one attempt, limits runtime to 120 seconds, and sends zero notifications. The
source-watch scheduled job starts disabled; cadence edits retain that choice
unless scheduling is explicitly enabled. Pausing the watch also disables its
scheduled job. A first observation initializes a baseline; it is not a
material-change dossier or model research report. Future changed-source dossier
writes retain the existing approval boundary.

Review the permission and cost preview before choosing **Create and queue
bounded starter**. This action authorizes bounded scheduler execution. The
managed dispatcher checks current authority before moving the task from Todo
through admission and execution. **Refresh task result** reads the actual task
state without adding a polling loop. **Open task in Work** provides cancellation,
approval, and recovery controls. If a task remains Todo or Ready, inspect Work
and the managed runtime status; a stopped scheduler needs the managed Seraph
stack restored. Missing or expired goal consent is repaired in Goals, and source
policy is reviewed in Connections. Uncertain effects require reconciliation
of that same attempt; setup never automatically replays them.

Bounded draft and checkpoint fields persist in owner/session-scoped activity
records. They contain no provider keys or source content. Reload restores the
same task. Starter creation derives a stable identity from the authenticated
owner, session, and journey; canonical goal/watch primary keys settle concurrent
tab races, and changed starter inputs are rejected without creating orphan rows.
The initial public-source permission and its authorization checkpoint commit
atomically only for the winning new goal. Replaying that journey preserves
stored consent and expiry: disabled or revoked permission, expired budgets,
and interrupted disabled checkpoints require explicit recovery in Goals.
Replay never renews a grant, records another enable authorization, or recreates
a deleted initial grant.
Partial setup metadata failure preserves editable inputs while
blocking duplicate task creation until saved progress can be loaded. Keyboard
controls use native buttons, labels, fieldsets, and radio inputs; the layout
wraps on narrow screens. Existing users keep their workspace, and **Skip setup**
remains available.

Setup completion is a separate, explicit result-opening action. The backend
requires an owner-bound Done task, an authoritative succeeded durable root,
independent run-bound readback, an explicit `no_learning` receipt, and a matching
artifact digest reread through the existing no-follow workspace boundary. The
public starter also binds the selected URL, immutable typed watch input,
observed plan revision, canonical durable authority, owner/goal, and baseline
watch row. Only the explicit pause may advance that observed plan before result
opening; changing the source or plan requires a fresh verified observation.
The disabled schedule and paused watch are required.
Saving configuration, creating a task, or forging a completion step cannot mark
setup complete. Operator steps and the first verified result are recorded in
the existing activity ledger; no external analytics are added. A retry revalidates
the actual artifact and reconciles the legacy profile completion flag even when
the result-opening checkpoint already committed. A profile write failure keeps
the same accepted task and gives recovery guidance to reopen that verified
result; replay does not create another result checkpoint or grant. Public watch
creation likewise records one creation event for the winning canonical insert,
including concurrent setup requests.

Provider-free validation is in `backend/tests/test_first_result_setup.py` and
`frontend/src/components/cockpit/FirstResultSetup.test.tsx`. The backend journeys
exercise authenticated HTTP APIs, the real dispatcher, registered deterministic
workflow tools, canonical database writes, local filesystem output, and artifact
readback. The public HTTPS transport is intercepted at its external boundary;
this proves mechanics, not a live public-source usefulness claim. External
source availability remains unverified. A root-collected managed keyless receipt
verified the local browser → scheduler → durable readback → opened artifact →
on-disk digest → reload journey. The narrow 390×844 viewport had no horizontal
overflow. A compact-height CTA issue from that receipt was accepted and fixed
with a bounded scroll surface and wrapped step outline. Root verified the
physical 1280×577 review click and same-viewport queue → refresh → open journey,
including matching disk digest and immediate legacy Skip dismissal. These
receipts do not establish live public-source usefulness
or model-provider execution, and the integrated feature remains Partial.
