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
- A `DownloadGrant` names at most two exact public file URLs and the exact
  redirect targets allowed for them. An out-of-grant URL or redirect aborts
  before that target is contacted; at most three redirects are followed.
- A JavaScript-free isolated Chromium context (the existing launch options:
  no permissions, credentials, storage state or service workers) navigates to
  each granted URL. Every browser request is intercepted. Granted bytes come
  only from the owned bounded transport (public addresses only, HTTPS, no
  proxy environment) and are delivered to the browser as an attachment, so the
  browser never renders them.
- Each file is at most 10 MiB; declared and streamed sizes beyond that abort
  before adoption. Content is identified from the bytes (PDF, DOCX, XLSX, CSV,
  plain text); the claimed type must agree. Executables, scripts, HTML/SVG/XML,
  other archives and binary text are refused. Filenames are one plain basename
  whose extension matches the verified type.
- Accepted bytes are written exclusively with no-follow into a private (0700)
  quarantine directory as a read-only (0400) file, read back and digest-checked,
  and only then adopted as an artifact record with the exact source metadata
  (requested URL, final URL, redirect chain, claimed type). Inspection returns
  metadata and a bounded inert preview; Seraph never opens, renders or executes
  a download. Revocation removes the file and its record. Inspection and
  revocation are owner-scoped.

### Uploads

`set_input_files`, file-control selection and upload requests remain
unsupported. Upload action kinds are rejected by the action grammar before any
page event; an opened file chooser is never answered and blocks every later
action; profiled pages refuse file controls; download contexts abort any
non-GET or body-carrying request before the owned transport contacts anything.
No upload is prepared speculatively.

### Selected desktop drafts (`desktop.draft.replace_selected_text.v1`)

- Interfaces: `DesktopSurfaceRef{paired_edge_id, app_id, window_id,
  snapshot_revision}` and `DesktopAction{kind: inspect | select_text |
  replace_selected_text, expected_selection_digest, replacement_artifact_ref}`.
- Scope is the focused editable text control of the currently active window
  only; there is no global search, coordinate fallback, clipboard or hotkey.
- Terminals, password fields, browsers (form submission), payment-like fields
  and system settings are refused.
- The local daemon shows the exact selected text and the exact replacement and
  requires a typed confirmation of the preview digest. Immediately before the
  write, and again at the adapter's write site, it re-reads focus and requires
  the identical app, window, control, text and selection digest; any change
  aborts without writing. After the write it reads the control back and
  requires the exact expected text.
- A failed or mismatching write is an unknown local mutation: the current text
  is offered for inspection and the replacement is never reapplied.
- Each platform adapter advertises its actual readiness. Linux uses AT-SPI2
  when the `gir1.2-atspi-2.0` typelib and a session bus are present. macOS
  Accessibility is not shipped and reports unavailable. An unavailable adapter
  blocks only this action; the browser, paired edge transport and the rest of
  the daemon remain usable.
- The core never dispatches desktop writes. The paired edge transport keeps
  `action_authority = false` and reports the local probe in its status.

## Consequences

- Downloads are verified, inspectable and never executed; uploads are rejected
  before any contact; desktop replacement is preview-exact with readback, and a
  focus or selection change prevents the write.
- Authenticated downloads, macOS Accessibility, select-text actions driven by
  the core, and any upload path require separate decisions and proof.
