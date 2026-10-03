---
title: "ADR-013: Fixed Reviewed Local Tool Package"
---

# ADR-013: Fixed Reviewed Local Tool Package

**Status:** Accepted

**Decision class:** Target architecture

**Tracked work:** [#916](https://github.com/seraph-quest/seraph/issues/916), under
[#899](https://github.com/seraph-quest/seraph/issues/899).

## Acceptance provenance

The lead accepted the narrow fixed-package target on 2026-10-03, incorporating
all three findings from the independent architecture review. Reviewed proposal
SHA-256: `2c834cb1318b7e3c3541bb02c50926a71d21c9ac515180d574eb70b53a7d7bd2`.
Independent review SHA-256: `0b0d0dccbc02ee9e1b3cf86a9e7e51ee12599717f9e4095aaf432f3d17a894ab`.
Controlling lead decision SHA-256: `44d0a10c4036036c1a409126fad13b2ae3e48e5efa72e8a1126ab5a28aad3a02`.
Acceptance defines the target; it does not establish a working build, isolation,
macOS execution or shipped capability. Existing ADR-007/008/010/012 authority,
portable-core and durable job contracts remain in force.

## Fixed package and existing seams

Select exactly one bundled, locally reviewed `seraph.tool.json-format` version 1 package: a fixed Python-stdlib JSON formatter. Input is one UTF-8 JSON artifact `<=32` KiB, with duplicate keys/non-finite numbers rejected, depth `<=32` and at most 4096 nodes. Output is deterministic sorted-key, two-space UTF-8 JSON plus newline, `<=64` KiB. No code strings, command argv, arbitrary module imports, URLs, discovery or user-selected executable. Validate output independently outside the sandbox and compare parsed value and deterministic bytes to the original input before adopting it.

Reuse `extensions/capability_pack.py` schema-v2 review, exact package/content/authority digests, owner/session approvals, active pointer, revoke/quarantine/uninstall and lifecycle receipts. Existing `api/capability_packs.py` already exposes review, approvals, activation, uninstall and status; existing Cockpit capability-pack inspection/recovery remains the operator location. Its generic `execute-local` is trusted deterministic local processing, not isolated package execution: reserve the fixed package identity and reject this compatibility path for it. Add one typed WorkBoard capability/native job for fixed JSON formatting behind current contracts/repository/dispatcher/review. The focused internal executor belongs under existing `execution/`; no new execution queue, installer, registry, general plugin loader, WASM runtime or daemon.

The operator reviews the exact manifest and bounded permissions, explicitly
approves it, and invokes the typed task through existing controls. There is no
new installer, registry, update or rollback system and no package download on
invocation. Existing lifecycle active-pointer, pause/revoke/quarantine and
removal controls retain their semantics; adding installation/uninstallation
controls is not a requirement of this milestone. Display input/output schema,
network=false, secrets/env=[], one approved input inode, one output inode,
exact runtime/package/profile digest and finite policy before execution.

## Optional enforced profile

Select one native Linux x86_64 `json-python-bwrap-v1` profile using pinned verified patched bubblewrap v0.12.0 (a later release requires explicit matching review). This is an OPTIONAL execution backend, never a core prerequisite. macOS remains a peer core host; this Linux profile is visibly unsupported/Blocked there. Do not silently substitute local trusted execution, rootful Docker or an unproved macOS profile. The existing rootless Docker profile remains unchanged. Its missing CPU CFS enforcement on the inspected host cannot justify weakening that profile.

Use an immutable minimal copied runtime closure (interpreter, necessary stdlib and shared libraries) with a dependency/file digest manifest, not whole /usr, home, canonical workspace, vault, daemon socket or arbitrary package trees. Namespace root is constructed only from trusted paths; package/input mounts are read-only. Unshare user/mount/PID/IPC/UTS/network, clear environment, no_new_privs, dropped capabilities, new session, die-with-parent. Never pass `--not-a-security-boundary`. Pass fixed supervisor-produced argv only.

The fixed CPython 3.12.8 runtime uses a trusted minimal C embedding launcher
with matching headers and libPython. Before isolated initialization, it obtains
exactly four bytes from native `getrandom(GRND_NONBLOCK)`, with bounded EINTR
handling and no short-read, sleep, environment or seed fallback. It sets the
hash seed through isolated PyConfig with fixed program, module search and
bootstrap paths; environment, site loading and bytecode writes remain disabled.
No host entropy device or `/dev` bind enters the namespace. Launcher source,
binary, matching build headers and runtime libraries are digest-bound. The
trusted bootstrap still closes descriptors and enforces limits and the syscall
filter before any package code. This startup dependency is accepted by the
lead; actual runtime and OS proof still require independent cumulative review.

A trusted bootstrap inside the namespace sets hard CPU `<=2` seconds, address-space `<=128` MiB, file-size `<=64` KiB and bounded file descriptors before loading the fixed package. It installs an architecture-specific audited seccomp allowlist BEFORE untrusted package code: no fork/vfork/clone/clone3, exec/execveat, socket/network, mount, ptrace, namespace entry or further policy changes. The bootstrap must be immutable and cannot depend on package-controlled Python import search. Seccomp's startup ordering and actual minimal syscall set are proof gates, not assumed ready.

Single-process restriction plus actual PID-namespace supervisor accounting is the finite process policy; do not claim unavailable cgroup CPU/PID/memory quotas or rely on RLIMIT_NPROC for root-in-user-namespace enforcement. CPU uses a hard time budget rather than a CPU-rate quota. Absolute wall deadline `<=10` seconds from first accepted attempt, one attempt, no renewed deadline on recovery; bounded stdout/stderr `<=8` KiB each, output `<=64` KiB.

Provide an explicit read-only output directory containing only one pre-created writable regular output inode (`result.json`), with no writable directory entries, symlinks or other writable filesystem. This avoids pretending per-file RLIMIT_FSIZE bounds aggregate disk use. Do not provide a writable tmpfs or unrestricted output directory to package code. Runtime import/bytecode writes are disabled. The supervisor retains exact process identity and child-tree ownership until actual reap/cleanup; cancellation or parent death cannot be called complete merely because a timeout elapsed.

Preflight checks exact launcher/runtime/package/seccomp digests and safe ownership/ancestry, actual namespace and filter availability and finite policy. Missing enforcement Blocks before package code. Future actual attacks run only after preflight passes. An installed launcher or successful `true` namespace probe is not isolation acceptance.

## Mandatory launch and build corrections

The supervisor spawns with close_fds enabled and no application pass_fds.
Only explicitly constructed bounded sanitized stdin/stdout/stderr pipes cross
the trusted launcher boundary. Bootstrap closes every other descriptor before
loading package code. Approved input/output use fixed namespace paths, never
an inherited artifact descriptor. No application DB, file, listener, HTTP
client, Unix/network socket or logging descriptor may cross. Actual attack
proofs make intentionally inheritable preconnected Unix/network sockets and
host file/DB descriptors unusable, and inspect the resulting descriptor set.
A descriptor count limit or network namespace does not revoke an open FD.

The pinned bubblewrap build leaves assume_kernel unset and records its
effective default, preserving strict fallback behavior. No weakening flag,
setuid launcher or unreviewed kernel assumption is permitted. A changed build
option requires a matching explicit kernel preflight and independent review.
Pin exact source, build options, binary and runtime dependency digests.

The profile supports only native Linux x86_64. The seccomp filter validates
AUDIT_ARCH_X86_64 before inspecting syscall numbers and rejects the x32 syscall
bit (`0x40000000`) and all unsupported/alternate ABIs. Preflight actually verifies namespace,
filter, interpreter/runtime closure and limits. Other architectures and macOS
remain usable core hosts with this optional profile explicitly unsupported and
Blocked. No alternate ABI or trusted local fallback may be substituted.

## Authority, durability and whole acceptance

One stable capability identity; original authenticated live Root/session, active Goal/revision, task/attempt, package active-pointer/content/authority review, profile/dependency digests, input digest, priority and first absolute deadline bind admission, process launch and terminal adoption. No inference, credentials, network or canonical-memory learning. Immutable declared permissions cannot expand through package fields. Exact idempotent duplicate admission reuses the original job, never starts a second process. Package pause/revoke/quarantine/removal and session/Goal changes fence new launch and late artifact adoption; completed verified output remains read-only history.

Reserve one output slot before launch. Nofollow/held-directory write/readback verifies regular file, bounded size, exact package/job/fence and output schema/digest before canonical terminal success and WorkBoard input-consumption CAS. Record process disposition/actual cleanup separately from artifact truth. Restart may adopt an exact previously verified complete reserved output only under original identity/permission/deadline and actual cleanup proof; missing process identity/quiescence stays Blocked/Unknown, never automatic replay. Explicit existing controls inspect/reconcile cleanup, cancel and retry only when original budgets permit; never infer cleanup from an empty in-memory registry.

Actual proof plan: authenticated managed manifest review/approval/activation -> create/Ready/run native job -> enforced real package execution -> independently validated physical output/API/literal UI readback -> reload/restart continued use -> revoke/quarantine -> invocation visibly rejected, with explicit no_learning. File SQLite and private original workspace/DB/runtime/process/artifact receipts retained with source hashes. Actual same-profile adversarial variants cover host fake-secret/sentinel reads, credentials/sockets, network including loopback and Unix sockets, fork/exec/namespace escape, CPU spin, allocation, output flood/many-files/symlink attempts, parent death/cancel/restart cleanup, late result, wrong owner/package version/current Goal/root, digest tamper and exact replay. OS enforcement must cause the denial; mocks or fabricated receipts cannot establish containment. A macOS UI/backend blocked receipt does not establish macOS isolated execution.

## Build and proof boundary

Pinned source and dependencies may be prepared and built only in ignored
persistent repository-local directories with origins, exact options, hashes
and retained receipts. No host-global install, daemon/service change or /tmp
source tree is required or authorized by this target. Missing patched launcher,
runtime closure, architecture support or enforcement fails visibly closed;
reviewed metadata and a successful harmless namespace probe do not complete
the capability. Actual same-profile OS attacks and a fresh independent
implementation review precede any completion claim. Current host availability
and build failures belong in dated evidence rather than becoming core-host
prerequisites.

## Current primary sources (retrieved 2026-10-03)

- https://github.com/containers/bubblewrap/security/advisories/GHSA-pxhw-h44j-8pfx — setup symlink escape advisory, affected `<0.12.0`, published 2026-08-26.
- https://github.com/containers/bubblewrap/releases/tag/v0.12.0 — patched release; non-setuid; strict boundary must not use the new weakening option. GitHub shows signed tag, not local binary provenance proof.
- https://raw.githubusercontent.com/containers/bubblewrap/v0.12.0/meson.build and meson_options.txt — current exact build dependencies and optional features.
- https://raw.githubusercontent.com/containers/bubblewrap/v0.12.0/README.md — bubblewrap constructs namespaces; the caller owns policy. No blanket containment claim follows from its version.
- https://docs.kernel.org/userspace-api/seccomp_filter.html — seccomp/no_new_privs syscall boundary; seccomp alone is not a sandbox.
- https://docs.python.org/3.12/library/resource.html — platform-dependent process resource limits; no claim these apply to macOS identically.
