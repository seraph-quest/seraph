---
title: "ADR-020: Reviewed Authored Capability Packages"
---

# ADR-020: Reviewed Authored Capability Packages

**Status:** Accepted

**Decision class:** Target architecture

**Tracked work:** [#924](https://github.com/seraph-quest/seraph/issues/924), under
[#899](https://github.com/seraph-quest/seraph/issues/899).

## Acceptance provenance

The lead accepted the independently reviewed v2 target on 2026-10-04. Design
SHA-256: `daefb60cf8e423c7de84b2c8420efdc14f0b9af07617f5e277b2f5e02fb33c2a`.
Independent review SHA-256:
`a6013c72be4b6851e188ad871e41612f2dd5fcc95c480b6a99f91fd064108955`.
The controlling correction makes author/validate CLI operations static only;
golden vectors are review data, never a pre-install execution surface.
Acceptance is a target, not shipped behavior or isolation proof.

## Decision

Extend ADR-013's existing package lifecycle and optional `json-python-bwrap-v1`
executor with one closed data-only authored adapter descriptor per package.
The first useful package is `local.time-ledger-summary`, whose derived capability
`pack.local.time-ledger-summary.summarize.v1` summarizes bounded category/minute
rows without network, secrets, inference or learning. A second small test package
must establish generic dispatch rather than a hard-coded example branch.

Use schema-v2 manifests, exact complete-package/content/authority/dependency
digests, unsigned-local provenance acknowledgement, existing review/approval,
activation, update, quarantine, rollback and uninstall controls. No installer,
registry, ledger, runtime upgrade, custom argv or executable dependency surface
is introduced. `contributes.adapters` contains at most the fixed
`adapters/adapter.json`; the descriptor binds one UTF-8 `adapter.py` of at most
32 KiB and exact code, input/output schema and runtime/profile digests.
Capability identity is server-derived as `pack.<package-id>.<adapter-id>.v1`;
built-in capabilities cannot be shadowed. Packages have at most 32 regular
files/512 KiB, with no links, devices or escaping paths.

Schemas use only closed objects, finite arrays/strings/integers, boolean, null
and literal const. Reject references, combinators, regexes, floats, defaults,
unknown keys and unbounded/recursive definitions. Schema limits are 8 KiB,
depth 8, 128 nodes, 32 fields/enumerands. Input is at most 32 KiB and output
64 KiB; canonical JSON rejects duplicate keys, non-finite values and excessive
depth/nodes. Operator input/output is literal data, never authority or markup.

Scaffolding and validation inspect these contracts without importing,
compiling, evaluating or executing authored Python, including in a sandbox.
Only an installed, exactly reviewed and active package may execute through the
existing typed Work/artifact/native-job path. The native job remains the sole
authority. Stage fixed reviewed bytes at the existing namespace paths; retain
the immutable bootstrap, launcher, seccomp, runtime closure, hard CPU 2 s,
address space 128 MiB, wall 10 s, one process/attempt, output inode and bounded
pipes. No host or unsupported-profile fallback is permitted. Shared CPU core
and static review remain usable on macOS and other unsupported hosts.

Pin exact package/code/schema/runtime/input/authority identities and original
finite deadline. A queued old version becomes stale after update. An already
released running job may continue only with its original staged bytes and
historically approved nonrevoked pin under current original Root/Goal authority;
this exception never grants a new process or replay. Every code/schema/resource
change requires a fresh exact version-delta review and approval. Quarantine
records its digest cause separately from operator revocation/uninstallation.
Only fresh reviewed rollback from digest quarantine may select a safe prior
nonrevoked version; unsafe-version tombstones remain. Uninstall withdraws all
installed-version authority and reads without erasing history or liabilities.

Preserve lifecycle-before-SQL lock order. Stage all filesystem/runtime/input/
output proofs outside pure SQLite writers. Writers check canonical owner,
Root/session, Goal/revision, task/attempt, lease/fence and staged pins; no
filesystem, Vault, HTTP, authored import or nested writer occurs inside them.
Actual process ownership, wait/reap and cleanup remain distinct from output
truth. Unknown cleanup holds capacity without age or PID-absence guesses.
One outstanding native execution is allowed per owner/package across Goals,
revisions, tasks and service instances. The existing pure SQL claim transaction
orders queued jobs by priority, creation time and identity, and retains an
unresolved original claim until exact positive cleanup. This is not a global
CPU lane: different owners/packages may execute independently. A bounded
owner history scan fails closed at 4096 rows or malformed original bindings.
Ordinary dispatcher priority is the scheduling boundary; cross-instance
priority fairness is not established by this package-local capacity fence.
Generic reconciliation defers to an exact current native owner with a live
lease and persisted released-process binding. It validates historical pins
outside the writer and current original Root/Goal/task/attempt facts in SQL;
cancellation takes precedence. Deferral proves neither output nor cleanup,
and never renews, replays or adopts. An expired or invalid lease remains an
explicit recovery/Unknown boundary until actual process proof is available.

Apply current private-read authority to both formatter and authored output
GET/snapshot availability. Physical verified output and a final canonical check
precede returned bytes; revoked/stale authority returns bounded reasons and
zero bytes. Execution deadline expiry alone does not revoke completed reads.
Clear scoped UI caches on owner/session/Goal/package changes and typed denial.
Preserve formatter original-value/deterministic-byte semantic verification;
generic schema-valid output is not a claim of semantic correctness or trusted
publisher identity. Record explicit `no_learning`.

## Verification and limitations

Require real Auth/SQLite/Vault/artifact/native execution and managed UI
review/install/activation/run/readback, exact v2 update and safe rollback,
queued/running version pins, concurrency/cancel/recovery and denied-read/cache
proof. Execute same-production-staging OS attacks and positive cleanup on the
actual pinned profile; historical #916 receipts alone are insufficient.
Retain exact sources, original private bytes and raw failures. Independent
cumulative review precedes integration. Linux receipts do not establish macOS
isolated execution. Unsigned code can return incorrect schema-valid results;
CPython/kernel/profile vulnerabilities and unsupported hosts remain explicit.

## Primary sources verified in accepted design (2026-10-04)

- https://github.com/containers/bubblewrap/releases/tag/v0.12.0
- https://docs.kernel.org/userspace-api/seccomp_filter.html
- https://docs.python.org/3.12/c-api/init_config.html
