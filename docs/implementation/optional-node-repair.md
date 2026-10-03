---
title: Optional Node Repository Repair
---

# Optional Node Repository Repair

**Status:** Branch-local implementation for [#912](https://github.com/seraph-quest/seraph/issues/912); not Shipped on `develop`.

[ADR-007](decisions/007-bounded-node-repair-supervision.md) owns the accepted
profile contract. The [current app guide](12-current-app-guide.md) owns the
surrounding Work, consent, approval and recovery journey. Seraph's core targets
macOS and Linux; this optional execution profile currently has actual native
supervision proof on Linux x86_64 only.

## Select an installed runtime

In Settings → **Repository sandbox**, select the Local backend and the **Node 24 / bounded
test and build scripts** profile. Enter the absolute path of an already
installed Node 24 executable, enable the backend using its checkbox and save.
Configuration alone does not start a repair or grant host execution. The server checks the installed runtime,
trusted helpers and Linux process facilities. Missing prerequisites block this
profile visibly. It does not download Node or dependencies, provision the host,
choose Python instead, or activate Docker as a fallback.

The ordinary Python profile remains the default. Unsupported macOS,
non-x86_64 Linux, unavailable pidfds/subreaper facilities and the unverified
Node Docker profile block Node execution without making them core startup
requirements. The proof runtime is Node v24.15.0; that is a tested mechanical
input, not a claim about the latest release.

## Request a bounded repair

In Work → Repository repair, choose an owned active Goal and current revision,
then provide the repository-relative source paths, allowed patch paths and
acceptance criteria. Package/lock/config and dependency inputs are immutable;
repairs change approved source files.

Use one test-selection token per line. The only selections are:

| Selection | Tokens in the form | Execution |
| --- | --- | --- |
| Test | `npm`, `test` | Inspected `test` script |
| Build | `npm`, `run`, `build` | Inspected `build` script |
| Build then test | `npm`, `run`, `build`, `test` | Build result, then test result |

The words name a finite server-owned selection. Seraph does not execute npm,
npx, a shell, lifecycle hooks or installation commands. Accepted package
scripts resolve to direct pinned Node argv: `node --test` with one to eight
exact `.test.js`/`.test.mjs` paths, `node` with an exact allowed entrypoint, or
`tsc --project` with an inspected JSON config and existing pinned TypeScript
compiler. Flags, globs, watch/server modes, shell expansion/chaining, other npm
scripts and dependency changes block admission.

A JavaScript test script can be `node --test tests/app.test.js`. A TypeScript
build can be `tsc --project tsconfig.json`, followed by an existing test script
that checks the generated code. Installed TypeScript and its lockfile must
agree. Existing dependencies are copied into private staging; source, config,
package/lock, script, compiler, executable and complete alias identities bind
the proposal and approval. Drift requires a fresh repair/approval.

## Review consent and exact execution approval

Inspect the selected private source before consenting to the governed model
route. The model proposes a bounded patch; it does not execute code. The
Inspector shows the exact inspected script bodies and direct argv before a
fresh `local_host_execution` approval.

The Inspector's **Recorded job preflight** is durable preparation evidence.
It is not a fresh global readiness claim. Missing or blocked recorded evidence
shows preparation blocked; local execution continues to require the exact
per-job approval. Fresh admission, approval and claim checks still reject stale
runtime, repository, Goal or settings authority.

Local execution has **no OS isolation guarantee**. Code runs as the Seraph host
user and can access host files and network. Resource enforcement is
`admission_and_wall_timeout_only`; CPU, memory and PID ceilings are unenforced.
The whole approved wall deadline includes preparation, commands, cleanup and
readback, with no extension after cleanup begins.

Source/dependency snapshots share a 64 MiB aggregate. Source is bounded to
2,000 files and 2 MiB per source file; the separate dependency manifest permits
512 files and 16 MiB per dependency file. Patch, output and captured streams
are capped at 1 MiB, 16 MiB and 1 MiB respectively.

## Read results and recover safely

Successful jobs retain private build/test/diff artifacts, hash-verified
readback and `no_learning`. The original repository and its Git metadata remain
unchanged. Actual local JavaScript and copied TypeScript build/test have been
proved through the authenticated durable job journey; acceptance model
transport was intercepted at the governed boundary, so no live provider or
model-quality claim is made.

The Linux per-job supervisor owns an exact PID/start token, job, attempt and
fence. It follows adopted descendants across `setsid` and closed pipes, signals
only identity-bound owned processes with pidfds, and requires reaping to ECHILD
before claiming cleanup. This is process supervision, not security isolation
against hostile host-user code or work handed to unrelated services.

Cancellation while a command is running can conservatively remain
`unknown_external_effect`. Supervisor loss, missing/reused restart identity,
or an exhausted cleanup/readback deadline also retain unknown status and the
physical slot. Disappearance alone cannot prove cleanup or permit automatic
replay. Another accepted job may queue and receive approvals, but it cannot
start its executor while that slot remains held. Use the shown same-root
recovery action; do not treat a cleared UI or absent PID as proof of success.

Marker updates, cancellation, and the supervisor start barrier share one bounded
private per-job process lock. A cancellation committed before the start token
prevents the staged command from starting; the supervisor can still return
its actual ECHILD cleanup receipt. The cancellation flag remains monotonic.

Same-root recovery can release a cancelled Node job's physical slot separately
from its unresolved task outcome. It rereads the private marker under that lock
and revalidates the original native Node job, immutable proposal/source,
original live operator root, exact dispatch/attempt/authority/fence/token,
PID/start identity, and originally approved supervisor source hash. Release
uses the current job revision CAS. Missing or mismatched proof keeps the slot
held. A changed current Goal or expired execution approval does not authorize
output adoption or replay, and does not prevent exact physical-only cleanup
under the original live operator root.

That release records `cleanup_receipt_verified`,
`process_cleanup_readback_sha256`, and `readback_scope=process_cleanup_only`.
It does not invent a content readback or exported artifact manifest, resolve
external or cost liability, or change the task/job's unknown outcome. A fresh
job still needs its own current Goal bindings, consent, and execution approval.

Optional native test fixtures discover an installed runtime from
`SERAPH_TEST_NODE_RUNTIME` or PATH, and TypeScript from
`SERAPH_TEST_TYPESCRIPT_ROOT` or the owning frontend dependency directory.
Unavailable native prerequisites skip only those optional execution fixtures;
portable grammar, platform, marker-lock and default Python checks still run.
