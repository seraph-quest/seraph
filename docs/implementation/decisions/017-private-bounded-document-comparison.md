---
title: "ADR-017: Private Bounded Document Comparison"
---

# ADR-017: Private Bounded Document Comparison

**Status:** Accepted

**Decision class:** Target architecture

**Tracked work:** [#923](https://github.com/seraph-quest/seraph/issues/923),
under [#899](https://github.com/seraph-quest/seraph/issues/899)

## Context

The operator selects one text invoice PDF and one CSV and needs an exact,
literal comparison with a discrepancy report and derived CSV. Private document
content must not become provider input, memory, public Board payload or code.
This decision preserves [ADR-008](./008-portable-core-and-consented-context.md):
Linux and macOS remain peer core hosts; optional parser readiness is separate.

## Decision

`work.document-compare.v1` supports only `compare-line-totals-by-sku`, through
native job `document_invoice_compare_v1`. Inference and remote effects are
non-applicable. Every outcome explicitly records `no_learning`.

Reserve the exact owner/Root/Goal-bound source pair on the existing WorkBoard
input-artifact row before streaming. Typed JSON contains server-minted private
references and expected digests/lengths; its existing 64 KiB cap stays unchanged.
Publish immutable encrypted sources with private nofollow paths and no-clobber
publication. Only exact decrypted readback permits final metadata CAS and task
binding. Missing pairs, changed authority and interrupted generations cannot
become executable. No filesystem, Vault or HTTP work belongs in SQL writers.

Quota checks and reservation insertion share one `BEGIN IMMEDIATE` transaction;
generation occupancy checks and acquisition use the same canonical writer.
Limits are two pending pairs per owner, sixteen per host, one active upload
per owner/two per host, and a 16 MiB reserved charge per pair under 64 MiB owner
and 256 MiB host totals. Pending, blocked and unclean expired/revoked/unknown
generations remain counted. One live and two lifetime generations are allowed;
retry requires positive prior quiescence and verified cleanup. The original
ingest window is 300 seconds capped by Root/Goal; replay never renews it.

Original PDF is at most 2 MiB/ten pages; CSV at most 1 MiB/2,000 data rows;
joined SKUs at most 1,000; extracted text at most 128 KiB; each field/line128
bytes. PDF starts on physical page1 with `INVOICE USD` and `SKU QTY UNIT_PRICE`.
CSV has exactly `SKU,QTY,UNIT_PRICE`. Reject duplicate SKUs and unaccounted text.
SKU grammar is `[A-Z0-9][A-Z0-9_.-]{0,31}`: the root accepted internal hyphens
to correct the reviewed representative pair's `PEN-01`/`BOOK-02` mismatch;
leading signs/formula prefixes, whitespace, controls, Unicode and slashes stay
excluded. Quantity is unsigned integer1..1,000,000 without leading zeros;
price is USD `(0|[1-9][0-9]{0,6})\.[0-9]{2}`. Decimal precision32 preserves exact
multiplication/subtraction. Missing is empty, never zero. Every result records
source SHA-256, physical PDF page/extraction ordinal or CSV row and operands/
formula. Extraction ordinals are not geometric PDF line claims.

Pin pypdf6.19.0 and use its context-local configuration in a trusted child.
Support only classic xref, one Contents stream per page, uncompressed or one
FlateDecode/Predictor1, fixed standard Type1 fonts and named standard encoding.
Reject arrays, XObjects/images, embedded fonts/ToUnicode, forms/annotations,
attachments/actions and unsupported layouts before extraction. Require strict
bounded Flate eof/no-tail validation; permissive decoder recovery cannot produce
success. Enforce2 MiB per decoded stream/4 MiB aggregate before each decode.
Structural and protected-path checks do not certify files malware-clean.

One document child may run host-wide through existing native job admission.
Before source bytes, persist positive supervised identity and prove a host
resource self-check: exact256 MiB AS set/get, below-budget mapping success and
above-budget ENOMEM/MemoryError. Unsupported/ineffective limits visibly block
this capability. Each child has independent30s default-disposition alarm and
10s CPU limit; the parent enforces bounded output and positive reap. Two attempts
maximum share one original70s job deadline capped by authority. Unknown child
keeps capacity held; lease expiry/empty registry are not cleanup proof.
This trusted subprocess is not an OS confidentiality or network sandbox.

Report64 KiB, CSV256 KiB and manifest32 KiB are immutable private outputs with
reserved identities and exact readback before adoption. Recovery adopts complete
reserved output without reparsing; explicit retry may rerun only a positively
terminated transient interruption within the original allowance. Unsupported
grammar/resource excess, partial output or authority/source drift remains blocked.

## Consequences

- Many invoices with logos/custom fonts or complex layouts are unsupported;
  show the reason without silently flattening or claiming partial success.
- No document inference, provider contact, general upload-to-agent route, generated
  parser command or memory learning follows from selection.
- Native macOS receipts and actual parent-death supervision are separate proof;
  Linux evidence cannot establish them. No mandatory Mac receipt blocks shared work.
- This accepted target is Planned until the full tracked vertical slice and
  independent review land; it does not claim shipping on `develop`.

## Verification

Prove authenticated streamed private ingest/readback, owner/Root/Goal negatives,
atomic distinct-owner quota/generation races, unchanged original bytes, actual
pinned PDF/CSV Decimal execution and cited formula outputs. Exercise malformed,
truncated/expanding streams, duplicate/formula/oversize layouts, effective child
limits, priority, cancellation, parent death, restart and output recovery before
capacity reuse. Complete a managed cockpit selection/job/report/download journey
with no model/provider call and independent cumulative review. Record actual
platform receipts separately and keep unsupported optional profiles visible.
