---
title: "ADR-031: Bounded General Documents"
---

# ADR-031: Bounded General Documents

**Status:** Accepted

**Decision class:** Target architecture

**Tracked work:** [#1010](https://github.com/seraph-quest/seraph/issues/1010)

## Decision

Extend [ADR-017](./017-private-bounded-document-comparison.md) with a separate
`document.read.v1` capability. The invoice comparison grammar, limits, admission,
private pair storage and recovery remain unchanged. General reads accept only
owner-selected canonical artifact identities, never caller filesystem paths.
Resolve the owner, immutable digest and private source before delivering bytes
to a fixed parser process. No source content enters generic task events.

One streamed `document-source.v1` reservation uses the same canonical
WorkBoardInputArtifact row, current owner/Root/Goal revisions and existing
document-pair encrypted publication, fenced writer, finite quota and positive
cleanup primitives. The distinct family has one source slot, a 32 MiB charge,
16 MiB source cap (DOCX 10 MiB) and the original nonrenewing 300-second ingest
window. Mixed-family quota and writer scans preserve host/owner limits while
invoice routes accept only their original family. Sealing verifies exact private
readback before metadata adoption. One exact bounded selection binds a source;
another selection requires a fresh reservation. Evidence is immutable encrypted
private output with exact readback. Deletion tombstones before physical cleanup
and returns capacity only after positive absence; unknown writers remain charged.

Upload writers hold an exclusive kernel lock on one stable private generation/
slot lease inode before publishing `live_writer`, through awaited stream closure,
source filesystem I/O and canonical settlement. The immutable active binding
includes owner, workspace, nonce, source digest, generation and slot plus exact
device/inode identity. Cancellation awaits positive settlement; a crashed owner
retains capacity until cleanup-only reconciliation exclusively acquires that
same verified inode and commits the exact binding CAS. It does not resume, seal
or adopt a partial upload. Missing, replaced, symlinked or held leases remain
unknown. A precommit orphan can reuse that one stable inode only while the
canonical writer proves no live binding and exclusive acquisition proves
quiescence; no TTL, PID absence or fresh filename substitutes for closure.

The existing document lifecycle runs one bounded local lock-contract probe per
start, outside SQL writers, with an empty-environment fixed child, independent
open descriptors, real cross-process exclusion/release and positive wait within
a two-second original bound. Its private readiness receipt binds the canonical
workspace, source-directory device/inode, process and boot nonce; stop, restart,
directory/workspace change or an unproved profile blocks new general-source
uploads with visible recovery. Each host must execute this runtime check;
macOS is a peer host, not categorically excluded by a Linux receipt. Native
macOS acceptance remains unexecuted. These locks enforce the cooperative writer
protocol, not filesystem confidentiality or hostile-host isolation. The existing
invoice profile gains bounded writer cleanup without a new boot prerequisite.

`DocumentReadInput` declares `artifact_ref`, `format` (pdf/docx/xlsx/csv),
`selection`, and `page_sheet_limits`. `DocumentEvidence` contains source-linked
sections (`source_ref`, `text`, `table_cells`), warnings and `source_digest`.
Physical PDF pages, DOCX body paragraph/table ordinals and sheet/cell coordinates
identify extraction locations; they do not claim PDF geometry or rendered pages.
Source digests bind original bytes, and literals remain untrusted data.

Use pinned pypdf, python-docx and openpyxl from the backend manifest/lockfile,
and standard csv. No runtime installation. Text PDFs are capped at 100 pages;
DOCX at 10 MiB; selected spreadsheet content at 100,000 populated cells; serialized
evidence at 1 MiB. ZIP member count, aggregate expansion, member size, compression
ratio and traversal checks precede Office parsing. Reject protected, malformed,
scanned/OCR, macro-enabled and external-link packages with specific recovery.
Read spreadsheet formulas literally and cached values separately; report that
cached freshness is unknown. Never evaluate formulas, macros or linked sources.
Ordinary and array-formula anchor cells preserve their literal expression text;
array ranges are not evaluated or expanded into inferred results. Data-table
formula objects have no literal expression and are explicitly unsupported, as
are unknown non-string formula objects and missing expression text. Object
representations and process addresses never become formula evidence.
Skipped pages/sheets are named in bounded warnings.

Exact package preflight limits are 2,048 ZIP entries, 16 MiB per expanded member,
32 MiB aggregate declared expansion and a 100:1 maximum member compression ratio.
Reject duplicate names, absolute paths, `..`, backslashes, drive/colon names,
symlinks and encrypted entries. XML DTD/entity declarations, macro-enabled content
types and external relationships are unsupported. Spreadsheet coordinate traversal
also has a 100,000-cell rectangular bound; very sparse huge sheets are unsupported.

Each process receives bounded anonymous-pipe input, resource limits, a finite
wall deadline, empty credential environment and kernel network-denial where
supported (Linux seccomp; macOS native sandbox profile). Actual socket-denial
self-checks precede source delivery; unavailable or ineffective confinement
visibly blocks parsing. macOS execution is unproven until a native receipt exists.
Positive kill/wait
cleanup precedes capacity reuse; timeout or crash never invents evidence. The
service has explicit start/stop, one bounded active local parser and no import-time
activation. A short canonical `BEGIN IMMEDIATE` acquisition scans existing
document-source rows and retains the global parser slot in their fenced writer
metadata across requests and app restarts. A new in-process lock cannot free an
unknown persisted parser. Shutdown closes the parent pipe and waits for the
existing supervisor's positive reap; it does not kill the witness owner.
No new queue, ownership ledger, composition epoch or future bridge
is introduced before #1007; the second landing integrates current typed callables.
Exact parser bounds are 512 MiB address space, 10 CPU seconds, 64 open descriptors,
zero core dump bytes and an independent 30-second wall alarm. The original parent
process window is at most 40 seconds (35 seconds communication and five seconds
cleanup), capped by the immutable original execution deadline; shutdown and
cancellation share its remaining allowance. Input has at most 8 KiB request JSON, 16 MiB source pipe input and
1 MiB plus 4 KiB result-envelope pipe output. Linux uses libseccomp default-allow
with EPERM denial of socket/socketpair/connect/bind/listen/accept/send/receive,
exec and process creation syscalls; macOS uses the native libsandbox deny-network,
deny-process-exec and deny-process-fork profile. These targeted profiles establish
network/process denial, not a general filesystem confidentiality sandbox; resource
limits alone establish neither. Inherited descriptors close and credentials never
enter the fixed child environment. Kernel socket denial is proved before the
readiness handshake authorizes delivering private source bytes.
An owner crash before committing positive reap leaves the source reader unknown
and charged; elapsed time is not cleanup proof. Explicit bounded original-reader
reconciliation reads the exact existing supervisor's private nonce/job/digest/
generation/PID-bound positive wait witness before releasing that slot. Missing
output remains visibly unavailable and is never invented from a reap receipt.
Two lifetime parser attempts share one original 70-second execution window capped
by the source/Goal/budget and authenticated Root expiry; replay never renews it.
Private completed-output reads use current owner/Root/Goal authority separately.
Fallible parent-directory preparation precedes the parser reservation commit;
known no-child failures release only the exact original binding. After launch,
positive supervised wait remains mandatory. Evidence adoption checks the exact
writer/generation/attempt/deadline, current authenticated Root and freshly read
active Goal revision in the same canonical writer. Stale authority or late
cancellation permits cleanup only, never an evidence receipt. Unadopted encrypted
output is removed through exact ciphertext/inode checks and positive absence
outside the SQL writer, or retained as explicitly charged cleanup-required
output; unknown cleanup does not make that output readable.

Local extraction performs no inference or learning. Returning private evidence
to the selecting authenticated owner is separate from source/model egress.
C1 local preparation uses a separate exact task-specific local-source acknowledgement,
one static `document_prepare` step, `inference_egress_acknowledged=false` and zero
inference calls, inference cost and children. Bind at most 16 unique existing leaf
citations to the adopted source revision and canonical metadata digest, which
covers its original owner/Root, Goal revision, generation, source/read-selection
digests and encrypted evidence receipt. Table containers cannot implicitly include
unselected cells. The complete authenticated selected-literal preparation is at
most 16,384 UTF-8 bytes including citations, formulas, cached values and metadata;
oversize and missing selections block instead of truncating. Generic task input,
events, checkpoints and the physically verified step artifact carry provenance
references and digests only. The async native owner rechecks current source,
Root/Goal, original job deadline, attempt and lease fence before private bytes
and in the same writer as successful readback adoption. The private view verifies
that exact succeeded task/readback and fresh source authority before delivering
the selected literal excerpts. It never reparses, adopts arbitrary output or
automatically accepts a task. Any later model route separately requires exact
source-and-model egress consent, existing authority, budgets and the shared
inference broker. Selection grants
neither provider consent nor instruction authority. Every result says no_learning.

## Verification and limits

Use real disposable format fixtures and private artifact readback, owner/digest
negatives, ZIP traversal/bombs, formula/cached values, parser timeout/crash and
positive cleanup tests. Deny provider sockets and verify zero provider contact.
Operator UI must show cited evidence or a specific unsupported/recovery state.
Tests establish literal extraction mechanics, never model quality. This decision
does not claim shipping, OCR, malware certification, arbitrary file reading,
general document understanding, or platform confinement without receipts.

Independent target review accepted the declared numeric/profile boundaries.
Its lifecycle finding was accepted and fixed: global current-row admission and
original supervised positive-wait recovery survive app restarts; shutdown keeps
the witness owner alive. Exact pushed implementation review remains required
before promotion; target acceptance alone does not claim shipping.
