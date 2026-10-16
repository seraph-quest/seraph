---
title: "ADR-034: Quarantined browser downloads and selected desktop draft actions"
---

# ADR-034: Quarantined browser downloads and selected desktop draft actions

**Status:** Proposed (implementation candidate for #1014; lead acceptance pending review)

**Decision class:** Target architecture

**Tracked work:** [#1014](https://github.com/seraph-quest/seraph/issues/1014)
(C5.3).

**Extends:** [ADR-029](./029-profiled-browser-interactions.md) with bounded
public downloads and pre-contact upload refusal, and
[ADR-022](./022-portable-selected-text-context.md) with one local desktop
replacement of the selected text. Authenticated-session downloads wait for the
exact authenticated transaction work in
[#1013](https://github.com/seraph-quest/seraph/issues/1013).

## Context

Operators want to keep a public PDF or spreadsheet a browser task reached, and
to rewrite a selected paragraph in a local editor, without granting arbitrary
file execution, uploads, global desktop search or coordinate automation.
Before the Cordis bridge integrates these effects, the current Python lifecycle
and authority owners remain the only owners; no bridge contract is imported.

## Decision

### Downloads (`browser.download.v1`)

- Interfaces: `BrowserFileRef{artifact_id, digest, mime, size, permission}`
  with `permission = read_only_quarantined`.
- A `DownloadGrant` names at most two distinct exact file URLs and the exact
  redirect targets allowed for them. Every granted URL must pass the browser
  capability's public-URL parser: HTTPS on port 443, no credentials, fragments,
  control characters or ambiguous path segments. An out-of-grant URL or
  redirect aborts before that target is contacted; at most three redirects are
  followed.
- The owned transport checks the configured site policy, resolves each host
  once on the browser transport's dedicated executor, requires every resolved
  address to be globally routable and connects to that pinned address with the
  original Host and SNI. It sends no proxy environment, requests identity
  encoding, refuses encoded bodies and stops reading past 10 MiB. One
  wall-clock deadline covers each file including redirects, and another the
  whole browser task; teardown always follows.
- A JavaScript-free isolated Chromium context (the existing launch options:
  no permissions, credentials, storage state or service workers) navigates to
  each granted URL. Every browser request is intercepted, and granted bytes
  reach the browser only as an attachment, so it never renders them.
- The request occupies the one shared browser lane with the same staged
  cleanup witness as other browser tasks. Positive teardown confirms the
  witness. An unknown launch result, a failed close or a cancellation while
  closing keeps the lane quarantined (`browser_cleanup_required`). A crash
  leaves the durable witness, so later browser work stays blocked until cleanup
  is proved.
- One request key has one outcome. The request record is sealed with a digest.
  Replaying a completed or failed request returns the recorded result without
  contacting the network again. A different body for the same key is refused.
  An interrupted request reports `download_request_outcome_unknown`.
- Content is identified from the bytes (PDF, DOCX, XLSX, CSV, plain text) and
  the claimed type must agree. The following are refused: executables and
  scripts (including after a byte-order mark); HTML, SVG, XML, script-like
  markup, PostScript and RTF presented as text; binary text; other archives;
  OOXML with macros, ActiveX or OLE objects, embedded files, external
  workbook links or non-hyperlink external relationships. Filenames are one
  plain basename whose extension matches the verified type.
- Each quarantine path is walked from the workspace root with no-follow
  directory descriptors. The quarantine components must belong to this user
  and be closed to others, and the shared `artifacts` parent must not be
  world-writable. Under one exclusive job lock, admission counts the existing
  files and creates the new one exclusively, so a job never holds more than
  two. The file is made read-only (0400) and read back, then adopted as an
  artifact record with the exact source metadata (requested URL, final URL,
  redirect chain, claimed type). A failure removes everything that admission
  created.
- Sidecar metadata is sealed with its own digest. Inspection and revocation
  recompute the expected file name from the job, digest and filename, and the
  artifact identity from those values. Any disagreement is refused as
  untrusted. Inspection returns metadata and a bounded inert preview marked
  `untrusted_content`; Seraph never opens, renders or executes a download.
  Revocation removes the file and its record. Both are owner-scoped.

### Uploads

`set_input_files`, file-control selection and upload requests remain
unsupported. The action grammar rejects upload action kinds before any page
event. An opened file chooser is never answered and blocks every later
action. Profiled pages refuse file controls. Download contexts abort any
non-GET or body-carrying request before the owned transport contacts anything.
No upload is prepared speculatively.

### Selected desktop drafts (`desktop.draft.replace_selected_text.v1`)

- Interfaces: `DesktopSurfaceRef{paired_edge_id, app_id, window_id,
  snapshot_revision}` and `DesktopAction{kind: inspect | select_text |
  replace_selected_text, expected_selection_digest, replacement_artifact_ref}`.
- Scope is the focused editable text control of the currently active window
  only; there is no global search, coordinate fallback, clipboard or hotkey.
  Identity is structural: process and application, the window's position in
  that application, and the control's child path. Window titles and control
  names are display-only.
- Only plain editable text roles are allowed. Password and terminal roles are
  refused, as are payment-like control names. Exact application names are
  refused for terminals, browsers (form submission), password managers and
  secret stores, authentication prompts, desktop shells and system settings.
  The name list is a deny list, not an exhaustive guarantee.
- The local operator flow:
  1. A countdown lets the operator focus the target window and select the
     text; the surface is then captured.
  2. The exact selected text and replacement are shown with control, bidi,
     zero-width and unusual separator characters escaped visibly, plus their
     byte and code-point counts. The text is labelled untrusted.
  3. The operator confirms by typing the preview digest prefix on the
     controlling terminal (`/dev/tty`). Non-TTY stdin is refused.
  4. The write proceeds only once focus has returned, within the countdown,
     to the identical app, window and control with identical text and
     selection. Any change aborts without writing.
- The executor re-verifies the surface and selection digest before the write.
  The adapter resolves the accessibility node once, verifies that node and
  writes to it. It re-checks the node's focus, selection and length
  immediately before `delete_text`. AT-SPI has no compare-and-swap, so a change
  in the short window before `delete_text`, or between `delete_text` and
  `insert_text`, is not prevented. The readback by control identity, which
  requires the exact expected text, detects it.
- A failed or mismatching write is an unknown local mutation: the current text
  is offered for inspection and the replacement is never reapplied.
- Each platform adapter advertises its actual readiness. The Linux probe needs
  the `gir1.2-atspi-2.0` typelib and a session bus. It calls `Atspi.init()`,
  requires `org.a11y.Status.IsEnabled` and reaches the desktop. macOS
  Accessibility is not shipped and reports unavailable. An unavailable adapter
  blocks only this action; the browser, the paired edge transport and the rest
  of the daemon remain usable.
- The core never dispatches desktop writes. The paired edge transport keeps
  `action_authority = false` and reports the local probe in its status.

## Consequences

- Downloads are verified, inspectable and never executed; uploads are rejected
  before any contact; desktop replacement is preview-exact with readback, and a
  focus or selection change prevents the write.
- Authenticated downloads, macOS Accessibility, select-text actions driven by
  the core, and any upload path require separate decisions and proof.
