---
title: Task Evidence Working Sets
---

# Task Evidence Working Sets

**Branch status:** intended post-integration behavior for issue #913; this page
does not claim Shipped behavior on `develop`.

The work-board task inspector recalls evidence within its selected goal. Refresh
creates a private, bounded packet of citation references and digests in the
canonical workspace. The packet contains no copied source bodies. Generic task
events retain neutral packet revision/digest and source exclusion identifiers;
they do not return claim text, source spans or private paths.

The private task evidence API re-reads canonical source records and artifact
files before returning claims. Source spans carry their original owner,
version/digest, line location, confidence when known and freshness. Page/row
locations remain explicitly unknown when the source does not provide them.
Correction and forget use the existing canonical memory APIs. Excluding a source
affects this task packet, while canonical deletion/tombstones prevent its return
through refreshed packets. A source change invalidates the stored span rather
than silently replacing the exact evidence revision previously used.

Refresh and exclusion always create local-only packets. After the spans render,
the separate adoption action requires that exact packet revision and digest plus
the current task revision. It revalidates every cited source, without retrieving
new spans, before recording neutral adoption evidence. Refresh, exclusion and
source drift require a fresh review; reset removes model-context adoption.

Sources include operator-approved canonical facts and signed accepted task
memory, verified browser extracts, research/document outputs, local encrypted
Mail reply drafts and Calendar meeting preparation artifacts. Artifact reads
require the owner/goal/task/attempt/run relationship, artifact identity/digest,
independent verified readback and a bounded no-follow filesystem read. Mail and
Calendar use their existing capability-specific source/connection/consent and
message/event checks. Revoked, expired, deleted, unsupported or unavailable
sources remain absent with a visible blocked/degraded receipt. This feature
does not fetch new provider source data or decrypt credentials/cookies.

GoalSnapshot output belongs to its deterministic child run, while the board
attempt links the parent. Evidence resolves only the parent’s canonical
`board_child_readback` to that exact child, using the dispatcher’s named-service,
root, fence, goal-owner, authority, input and typed readback contracts. The shared
output-artifact selection resolver uses the same proof. Descendant scanning does
not authorize outputs.

Stable ownership recovery permits exact selected historical sources to be
inspected locally. A fresh task can recall a historical source only when the
identity recovery journal explicitly selected it and linked that fresh task.
Output artifacts require their own `output_artifact` selection; selecting a task
does not select all of its outputs. Original owner principal/root pairs remain
immutable. Recovered task packets are read only; old sessions never become
execution principals.

Specify and Decompose place the exact adopted packet in their existing governed
task prompt. The packet revision/digest is revalidated inside the provider-contact
transaction and recorded as neutral task-use evidence. Local read permission
does not grant cloud egress: the operator must explicitly adopt a reviewed packet
for task model context. Private or recovered sources remain blocked for the
generic strategist purpose; excluding those sources is the available recovery
until a reviewed source-purpose egress contract supports that use. Existing
capability-specific reply/preparation model consent does not cover this new
purpose.

Retrieval uses the existing memory lexical scoring on the CPU and reports
`lexical_degraded` with `remote_embeddings_not_used`. It introduces no inference
provider, mandatory embedding request, separate memory authority or scheduler.
Limits are 16 claim spans, 32 artifact candidates/exclusions, 128 bounded
canonical fact candidates and 64 KiB per source/private packet. Packet mutation
uses task and packet revision checks, and running/archived tasks reject it.
Retrieval and inspection report `no_learning`; only explicit canonical operator
correction/forget changes memory.

Focused validation is owned by `backend/tests/test_task_evidence.py` and
`frontend/src/components/cockpit/TaskEvidencePanel.test.tsx`. They exercise real
canonical DB/artifact/API reads, correction/tombstones, exclusion/CAS, cross-owner
and symlink/drift rejection, private derived source consent/revocation, exact
task prompt binding and inspector controls. Identity-continuity integration
requires the matching #900 scope helpers and real recovery tests; branch-local
helper absence is visibly blocked. No live provider usefulness or paid inference
receipt is implied by these mechanical checks.

The focused snapshot test executes the actual local dispatcher and registered
workflow `get_goals`/`write_file` steps, then reads its durable child artifact
through the evidence API and exact output selection helper. Other source-specific
seeded completion receipts remain complementary permission and drift checks.
