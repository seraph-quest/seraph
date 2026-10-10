---
id: task-method-composition-metadata
title: "ADR-033: Task-method composition metadata compatibility"
---

# ADR-033: Task-method composition metadata compatibility

**Status:** Accepted branch-local target, effective for implementation after
Root adopts the independently reviewed contract commit. Promotion to `develop`
and shipped truth require the whole milestone merge and its acceptance;
no separate documentation PR is required. Runtime composition remains
**Planned**; this decision establishes no activation or shipped implementation.

**Tracked work:** [#1007](https://github.com/seraph-quest/seraph/issues/1007).

**Relationship:** Supersedes only ADR-026's closed schema-object inventory and
1354-object ceiling / unfiltered LIMIT 1355. ADR-028's existing canonical
reviewed-method pointer remains owned by its original service. No other
ADR-026 or ADR-028 authority, body, lifecycle or capacity contract changes.

## Context

The existing application router loads the reviewed-method owner, whose
`TaskMethodActive` SQLModel registers `task_method_active` in the same canonical
metadata and database. A caller may register that model before or after an
initial database fixture through the existing `create_app` router import.
The previously closed composition schema inventory omits its table and four
indexes, so a genuine subsequent database rejects schema metadata before any
selected Auth body. Omitting the production model or changing import order
cannot establish compatibility with the complete supported application schema.

The operator approved the narrow metadata compatibility resolution on
2026-10-10. The accepted change preserves the closed inventory and its overflow
witness instead of excluding schema objects from enumeration.

## Decision

Allow exactly these existing schema objects as auxiliary composition metadata:

| Type | Exact name | Original table |
| --- | --- | --- |
| Table | `task_method_active` | `task_method_active` |
| Named index | `ix_task_method_active_goal_id` | `task_method_active` |
| Named index | `ix_task_method_active_owner_identity_id` | `task_method_active` |
| Automatic index | `sqlite_autoindex_task_method_active_1` | `task_method_active` |
| Automatic index | `sqlite_autoindex_task_method_active_2` | `task_method_active` |

The model's original primary key and unique constraint
`uq_task_method_active_scope(owner_identity_id, goal_id, goal_revision, family)`
account for exactly two automatic indexes. The two named indexes come from the
original `goal_id` and `owner_identity_id` fields. No third automatic index,
additional instance, wildcard name, trigger or statistics object is permitted
by this amendment. No new table or index is created by composition preflight.
The existing canonical database and reviewed-method writer retain ownership.

The complete source-derived supported ceiling is **1359 schema objects**;
enumerate the unfiltered `sqlite_schema` with **LIMIT 1360** to witness overflow.
Its disjoint category calculation is:

| Supported source category | Maximum objects |
| --- | ---: |
| Existing canonical/non-FTS table names plus this existing pointer | 91 |
| Existing named index inventory plus its two named indexes | 1146 |
| Existing bounded automatic indexes plus its two automatic indexes | 102 |
| `sqlite_sequence` | 1 |
| `alembic_version` table and its bounded automatic index | 2 |
| Existing principal insert/update triggers | 2 |
| Existing exact FTS tables | 6 |
| Existing exact FTS search triggers | 9 |
| **Total** | **1359** |

This is the sum of complete supported source categories, not a database fixture
count, dynamic headroom or a claim that all supported objects coexist on every
host. The prior inventory sums to 1354 through the same categories; only these
five existing objects extend it. Metadata bytes still debit the same original
operation frame before private bodies. Unknown names, unsupported object kinds,
unsupported layouts under existing schema checks, generated/hidden canonical
body columns and overflow continue to deny before private bodies.

`task_method_active` is **metadata only** for this allowance. Do not add a
composition body descriptor, scan pointer rows, read binding JSON, retain its
body or insert a witness component because its metadata is accepted. The
unchanged common **33-table** body superset and exact conditional
`operator_identities(id, created_at, revoked_at)` programme certificate remain
the only body additions already adopted under ADR-026. The shared original
**128 distinct references / 1,048,576 bytes**, repeated-read debits, prospective
reservations, current transaction certification and no-reset rules remain
unchanged. Existing raw codecs, leaves, stored witnesses and explicit version
transitions remain unchanged.

Metadata/read certificates confer no permission. Current original Root or
programme authority, Goal, grants, revocation, fence, epoch, Source, deadline,
physical effect/readback and Unknown/liability retention remain independent
requirements. This decision does not activate the reviewed-method service,
Cordis migration or any of the fixed fourteen services/thirty-four methods.

## Verification

Implementation must reconcile the exact existing model with the closed
inventory and its full supported ceiling together. Ordinary provider-free
regressions must cover genuine application-model registration before/after
fixture construction, actual Auth fixture activation and due/non-due touch,
expiry/logout commit and privacy, and schema/index/locator failures before
full OperatorSession body delivery. Retain exact error codes and SQL/private
output equality for denied operations. Unknown table/index/trigger names and
1359/1360 census boundary must deny or pass at their exact original boundary;
synthetic positive witnesses, filtered enumeration, relaxed names or renewed
body budgets are prohibited.

Run documentation contract/link checks and independent cumulative review before
implementation release. Ordinary regression execution establishes only the
checked local mechanics; no provider inference, eval, capability readiness or
shipping claim follows from this decision.

## Consequences

The actual reviewed-method schema can coexist with composition metadata
certification without requiring a new body owner or weakening unknown-schema
denial. Future schema additions still require source evidence and separately
reviewed contract disposition; the ceiling is not an automatic growth policy.
The small compatibility change does not resolve any other #1007 execution,
recovery, selected-programme or capacity limitation.
