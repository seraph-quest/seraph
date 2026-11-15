---
title: Attention and Recovery
---

# Attention and Recovery

**Status:** Partial, branch-local implementation; not shipped on `develop`.
**Scope:** composition of existing Home, Guardian Inbox, task inspector,
approvals, owning capability readback and Settings accounting controls.

Home shows **Needs attention and next steps** within its six-section local
metadata page. One aggregate page contains at most 20 rows. Explicit Task
priority and due work precede approvals and recovery in the server selection;
pending or snoozed Inbox decisions follow recovery and precede Goal context.
Inbox labels identify watched-source changes, watched Mail or a proposed public
opportunity without copying private evidence. Queued or other opportunity history
does not appear as a pending decision. The Inbox inspector checks current evidence
and allowed actions. Counts describe this page, not all pending work.

**Refresh Home** explicitly obtains a new page. Pagination retains the original
creation cutoff and five-minute/session expiry; each page rereads current metadata.
Existing decisions may become eligible on later pages. A changed original cursor
anchor requires a restart, and paging never renews its expiry. Failed reads retain
last-confirmed metadata with a visible stale label. Goal rows show bounded titles;
the existing selected Goal status and criterion appear only when its current owner,
ID and revision match the returned Goal. Missing labels or criterion remain explicit.

Opening a task from attention or Inbox uses the existing Work inspector.
**Return to Home attention** or **Return to Inbox decision** restores the
originating view and keyboard focus. Goal inspection reads the owning goal
tree once and selects only the exact goal present in that response. Thread
inspection uses the existing thread loader. All navigation bindings use the
finite authenticated principal and operator root; a root change discards
retained origins and asynchronous responses. Recovered selected history stays
visibly read only, with navigation available and effect controls unavailable.

Pending approval timestamps include UTC offsets, so valid approvals retain
their expiry instant in browsers in other timezones. The task approval review
reads the actual pending approval and revalidates
task revision, latest attempt, durable run, goal revision and current owner
before and after the owning approval decision. Missing, conflicting, expired,
foreign or stale metadata withholds controls. Approving a row records the
decision; execution and verified completion remain the task's canonical state.
Standalone approvals continue to use the existing approval pane.

Unknown effects never authorize a blind retry. For a GitHub follow-through
task advertising reconciliation, the inspector reads the exact owning job
from the latest attempt and requests its existing GET-only destination
readback. It retains the original operation identity and displays the actual
verified or unresolved receipt. Successful readback atomically annotates only
matching failed-readback diagnostics as resolved, preserving their original
status and exact parent linkage. The existing dispatcher adopts the latest
ended unknown GitHub attempt only after its exact root, proof, original finite
owner, goal and connection revision remain valid. Expiry, revocation or goal
changes retain the verified owning receipt and a specific blocked task reason;
they cannot revive authority. Unsupported capabilities explicitly say that
no readback control is available; existing inspect, cancel and review actions
remain governed by their owning API. Cost recovery opens the existing Settings
accounting inspector only when its API advertises settlement for the exact
job, owner and goal revision. The attention UI constructs no settlement amount
or evidence claim.

The earlier rendered Warsaw attention/recovery journey was mechanically verified through real ASGI
HTTP/WebSocket handlers and retained SQLite/artifacts, with intercepted
public-source/GitHub transport and an explicit server-side test permission.
Recreating the ASGI app against the same database proves persisted recovery,
not a managed-host backend process restart. Production GitHub write-consent
creation remains a separate incomplete boundary owned by the tested-publication
milestone; this UI does not create that consent. Live external usefulness
remains unverified. That historical receipt does not establish rendered-browser
verification of the newer single-source Home continuation and Inbox integration.
