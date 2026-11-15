---
title: "ADR-007: Bounded Node repair and Linux process supervision"
---

# ADR-007: Bounded Node Repair And Linux Process Supervision

**Status:** Accepted target for #912; branch-local implementation, not Shipped on `develop`

**Decision class:** Target architecture

**Owning work:** [#912](https://github.com/seraph-quest/seraph/issues/912); bounded original-producer recovery amendment [#1009](https://github.com/seraph-quest/seraph/issues/1009)

## Context

The native `engineering.repo-repair.v1` journey already owns repository
inspection, source/model consent, patch proposal, exact execution approval,
durable job and physical repair slot, staging, artifacts and readback. Adding
JavaScript and TypeScript requires one reviewed execution profile rather than
another agent runtime. The Python profile remains the default.

The current local worker's process-group cleanup does not detect a descendant
that forks, creates a new session and closes inherited output pipes. A real
Linux probe reproduced a successful direct exit followed by a detached child's
delayed write. An independently reviewed Linux subreaper probe detected,
terminated and reaped that child despite an empty original process group.

This decision records that accepted profile/supervision design. It does not
claim implementation, deployment acceptance, model repair quality or shipping.

## Decision

### One explicit Node profile

Add server-selected `repo-node24-npm-v1` to the existing executor/settings
contract. It uses an installed exact Node 24 runtime, frozen executable and
dependency identities, inspected package.json scripts, lockfile and config.
The reviewed proof runtime is Node v24.15.0; this is a pinned mechanical proof
input, not a claim about the latest or most secure release. No runtime or
dependency download is performed. Missing or unavailable selected prerequisites
block visibly; no Python, executor or inference fallback is selected.

Only finite named test/build commands are supported. Resolve inspected scripts
to server-built direct argv using absolute pinned Node, never npm, npx, a shell,
local `.bin`, command discovery or npm lifecycle hooks. This supports the listed
npm-script forms without claiming general npm-run semantics:

- Test: `node --test` with one to eight exact allowed `.test.js` or `.test.mjs`
  paths, or `node` with one exact allowed test entrypoint.
- Build: `node` with one exact allowed build entrypoint, or `tsc --project`
  with one exact inspected tsconfig JSON path and the pinned TypeScript
  dependency entrypoint.
- An explicitly selected build-then-test sequence records both actual results.
  Generated tests must stay inside a finite declared job-local output directory
  and be validated after build before execution.

Tokens are single-space-separated ASCII relative paths without traversal,
quotes, globs, expansion, leading-dash paths or shell metacharacters. Reject
caller flags, arbitrary scripts, workspace expansion, watch/serve modes,
recursive npm, install/exec/publish/rebuild, assignments and shell chaining.
Package/lock/config/toolchain/dependency execution inputs are immutable and
cannot become model patch targets. Source, patch and execution-input drift
invalidate approval or adoption.

Copy only an inspected bounded existing dependency tree into private staging.
Foreign package roots, escaping links and unknown executable aliases block;
any accepted `.bin` link must resolve inside the exact snapshot and is never
executed. Remove inherited Node/npm options, auth/provider/vault credentials
and uncontrolled PATH/HOME; use job-local home/cache and a minimal trusted
environment. Project/user npm configuration is never sourced.

Preserve the current 64 MiB aggregate source/dependency snapshot, 2,000 source
files, 2 MiB per source file, 1 MiB patch, 16 MiB output, 1 MiB streams and
180-second total wall deadline. The separate dependency manifest allows at
most 512 regular files and 16 MiB per dependency file within that aggregate.
Do not widen Python limits.

### Linux supervision and unknown recovery

Node/local uses an exclusive single-thread trusted per-job supervisor launched
through the pinned backend interpreter with isolated Python imports. It enables
and verifies `PR_SET_CHILD_SUBREAPER`, default SIGCHLD handling and pidfd
support before staged execution. The multithreaded backend never becomes the
subreaper and never reaps unrelated children through `waitpid(-1)`.

Extend the existing durable owner marker with exact supervisor PID/start token,
job/attempt/fencing token, authority/posture/stage bindings and supervisor-source
identity. A start barrier prevents staged dispatch until that identity is
durably acknowledged. Only this job's fixed finite command sequence belongs to
the supervisor. No spawn is allowed after cleanup begins.

Signals require exact owned process identity and pidfds. Bounded adopted-child
enumeration, termination and reaping continue until the still-owned supervisor
observes `waitpid(-1, WNOHANG | __WALL)` returning ECHILD. A process-group scan
or stage deletion cannot substitute for this terminal oracle. Leftover detached
descendants reject the execution even if subsequently cleaned. Cleanup uses
time reserved inside the original approved wall deadline, never an extension.

Cancellation requests address the exact supervisor and allow it to terminate
and reap its children. Missing/reused supervisor identity, abnormal supervisor
death, missing terminal proof, unreadable ownership or cleanup-budget exhaustion
produce an unknown outcome: retain the physical repair slot, block adoption
and automatic replay, and expose recovery. Supervisor disappearance alone is
never cleanup proof and never authorizes signalling unrelated processes.

Linux facilities must be verified for the selected profile. Unsupported
platforms, including macOS without separately reviewed equivalent proof,
remain visibly blocked. Docker is optional and Node/Docker remains blocked
until exact profile-specific image/toolchain/enforcement proof exists.

### Explicit local-host trust boundary

Local execution keeps `isolation_claim=none` and
`resource_enforcement=admission_and_wall_timeout_only`. CPU, memory and PID
ceilings are unenforced and must be visible before exact
`local_host_execution` approval. Source/dependency code retains host-user
filesystem and network access. Subreaper ancestry tracking is process
supervision, not security isolation: same-user code can attack same-user files
or processes, or arrange work through an unrelated service. Abnormal supervisor
death may leave detached code running; the unknown receipt prevents adoption
and slot release, not hostile host-user effects.

### Branch-local original-producer recovery amendment

[ADR-032](./032-repository-crash-recovery-evidence.md) amends the ADR-030
protocol summarized below: new admissions use v4 with post-material-fsync proof
and a separate immutable retry commitment. Startup also protects historical v3
producer-mode lineage when registration is missing. The v3 transport description
below is historical; it does not authorize new admission or upgrade old evidence.
All unaffected supervision, authority and physical-only settlement limits remain.

[ADR-030](./030-bounded-iterative-repository-work.md) accepts a separate Planned
mode for NEW originally sealed v3 iterative repository Roots. Before command
ACK, the original trusted supervisor registers its public verification identity
through the original process/control handshake and canonical writer. Its
Ed25519 key stays only in producer memory and never reaches staged children.
The producer owns irreversible no-spawn, child EOF/descriptor close/direct wait/
ECHILD, fixed finalizer and stage removal within the original cutoff. Outputs
and their directory are fsynced before signed envelope installation; the envelope
directory is fsynced afterward. Failed/incomplete durability retains Unknown.

Only this explicit `original_producer_durable_v1` transport replaces the lost
backend-parent pipe/wait predicate after restart. Authenticated originally
registered producer proof and exact current Source CAS replace that predicate;
reconstructed Popen, PID absence, free guard, copied marker or read-only Unknown
projection cannot. Ordinary executors keep their original ownership predicates.
The same configuration fence orders expiry Stop intent commit/readback before
cleanup staging and publication. Startup protects only exact original Root/
native/explicit-parent lineage before accounting and stale-job mutation.

Destroyed same-boot producer without authentic closure remains held Unknown.
ADR-030 separately permits gated actual same-host boot physical-only settlement,
without terminal task/result proof or cost forgiveness. This supersedes the
physical-release predicate ONLY for that originally registered v3 variant; no
historical v1/v2 upgrade, deadline/lease renewal or replay is allowed. It retains
`isolation_claim=none` and explicit same-host-user trust, not hostile-code
attestation. Unsupported host proof blocks visibly. These accepted branch-local
constraints neither enable the mode nor establish implementation or Shipped truth.

## Consequences

- Extend existing repair/job/approval/artifact/settings/UI seams; no new broad
  agent, scheduler, authority store or host provisioning is required.
- Keep Python default behavior and profile limits; describe its existing
  process-group-only cleanup truth without inventing descendant containment.
- Expose exact selected scripts/direct argv and execution-input hashes before
  local-host approval, actual test/build results afterward, private durable
  artifact/readback hashes and explicit `no_learning`.
- Generated tests are untrusted evidence and never automatic adequacy proof.
- Platform or cleanup failures remain operator-visible and fail closed.

## Verification

The implementation gate requires an authenticated typed Work request through
real source inspection, governed model transport intercepted only at its
external boundary, exact proposal/approval, real staged JS and TypeScript
test/build execution, durable artifacts/readback, UI and `no_learning`.

Prove running-command cancellation, nested timeout descendants, cleanup-deadline
exhaustion, durable restart with exact/missing/reused supervisor identity,
retained physical slot and no unrelated signals. Cover immutable script,
lockfile, dependency, toolchain/config/source drift; path/link and `.bin`
escapes; pre/post hook suppression; missing prerequisites; output bounds;
Python regressions and one-physical-repair semantics. Feasibility fixtures alone
do not establish product recovery or implementation completion.

Primary references checked 2026-10-02:
[Linux subreaper](https://man7.org/linux/man-pages/man2/PR_SET_CHILD_SUBREAPER.2const.html),
[Linux wait](https://man7.org/linux/man-pages/man2/waitpid.2.html),
[Linux pidfd signalling](https://man7.org/linux/man-pages/man2/pidfd_send_signal.2.html),
[npm run](https://docs.npmjs.com/cli/v11/commands/npm-run/), and
[Node 24 permission-model limitations](https://nodejs.org/download/release/v24.15.0/docs/api/permissions.html).
