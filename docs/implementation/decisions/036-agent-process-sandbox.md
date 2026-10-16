---
title: "ADR-036: OS-enforced workspace view for agent commands"
---

# ADR-036: OS-enforced workspace view for agent commands

**Status:** Proposed (implementation candidate for #1103; lead acceptance pending review)

**Decision class:** Target architecture

**Tracked work:** [#1103](https://github.com/seraph-quest/seraph/issues/1103).
The design note, operator decisions and v2 addendum on that issue are the
reviewed inputs.

**Extends:** the #1102 agent workspace classification
(`WorkspaceStateRegistry.agent_protected_paths`, `AgentAccessIndex`) and the
#1065 repository command sandbox (`backend/src/execution/repo_isolation.py`).

## Context

#1102 makes the agent file tools, and the path arguments of the process tools,
refuse registry-classified secret and runtime-owned workspace state. But
`run_command` and `start_process` run interpreters (`python`, `node`), `sed`
scripts and shells whose file opens come from script content, not from
arguments. Before this decision, an agent that could run commands could still
read `.vault-key` or `seraph.db`, or rewrite `extensions-state.json`, through
those channels. Argument parsing cannot close that gap. The kernel can.

## Decision

### The workspace view

Every `ProcessRuntimeManager` launch, from chat tools, capability-execution
adapters or native software-engineering runs, executes as
`process_limit_bootstrap` → `bwrap` → guard → target. There is no per-caller
opt-out. The bootstrap still applies the fixed rlimits. Inside the sandbox the
workspace, at its resolved path, is a view:

- **Synthetic levels.** The workspace root and every ancestor of a protected
  path are synthetic levels. A CI test pins the set to exactly `.`,
  `artifacts` and `artifacts/mail`. Each level is a private tmpfs, remounted
  read-only after its children are placed, so commands cannot create, rename
  or delete entries there.
- **Children of a level.** Each entry existing at launch is opened with
  `openat2(RESOLVE_BENEATH | RESOLVE_NO_MAGICLINKS | RESOLVE_NO_SYMLINKS)`
  from one root directory fd. It is classified by name (NFC and casefold)
  and by the `fstat` identity of that fd. The fd itself is then bound:
  - READ_WRITE: `--bind-fd`;
  - READ_ONLY: `--ro-bind-fd`;
  - DENIED: a mode-0000 placeholder.
  Absent DENIED names also get placeholders.
  `.seraph-workspace-maintenance.lock` is DENIED here, although file tools
  may read it.
- **Fd swap check.** bwrap re-checks each fd bind's inode after mounting and
  aborts on a swap.
- **Symlinks.** A symlink at a synthetic level is recreated only when its
  target resolves beneath the view. Otherwise the launch blocks with
  `workspace_view_unsafe:symlink_escapes_view:<path>`. A protected name that is
  a symlink becomes a DENIED placeholder.
- **Below a READ_WRITE child,** the host tree is bound unchanged.
- **Outside the workspace,** the sandbox sees only:
  - read-only `/usr` and `/etc`;
  - a fresh `/proc` and `/dev`;
  - a bounded private `/tmp`;
  - the per-invocation worker root (writable; HOME, TMPDIR and caches);
  - read-only runtime files: the interpreter's library trees and the
    allowlisted executables. Version-manager shims are resolved to their
    real binary. The external base interpreter of a workspace `.venv` is
    included when it is a real Python installation outside the forbidden
    trees; otherwise the launch is blocked with `runtime_root_unsafe`.
- **Launch-time blocks.** These block the launch with
  `process_isolation_unavailable:workspace_view_unsafe:<reason>`:
  - a DENIED regular file with `st_nlink > 1`, anywhere in a DENIED tree
    (`hard_link_alias:<path>`);
  - more than 512 entries at a level, or too little `RLIMIT_NOFILE`
    headroom (`too_many_entries`);
  - a DENIED tree with more than 50,000 entries, which the hard-link proof
    cannot cover (`denied_tree_too_large:<path>`); the launch is refused,
    never assumed safe;
  - a level that is not a directory.

  READ_ONLY files may have extra hard links, because uv and npm installs
  link from caches outside the workspace. No new alias can be made from
  inside the sandbox.

### Process semantics

- `--unshare-user --unshare-pid --unshare-ipc --unshare-uts`, `--die-with-parent`,
  `--cap-drop ALL`, `--disable-userns --assert-userns-disabled`.
- **Network.** `run_command` also gets `--unshare-net` (loopback only).
  Local endpoints trust 127.0.0.1, so a shared namespace would reach them.
  `start_process` shares the host network, and its approval says: "This
  process can reach localhost services, including Seraph's own API."
  `--share-net` is not used, because it requires `--unshare-all`, which also
  creates a cgroup namespace. Omitting `--unshare-net` shares the network.
- **Sessions and signals.** There is no `--new-session`, so the leader (the
  bwrap monitor) keeps the bootstrap's PID, session and process group, and
  the manager's verified group tree-kill reaches every sandbox process. The
  bootstrap's `--sandbox-monitor` mode makes the monitor ignore SIGTERM. The
  guard restores the default before `exec`, so a group SIGTERM still stops
  the target gracefully.
- **Namespace teardown.** When the leader dies or the command exits, the pid
  namespace is torn down. Double-forked and `setsid` children die with it.
  A signal death is reported as exit code 128 + signal (bwrap's monitor).
- **Launcher thread.** `--die-with-parent` is `PR_SET_PDEATHSIG`, which
  fires when the *thread* that forked the sandbox exits. Every sandbox is
  therefore forked from one long-lived launcher thread, so a background
  process outlives the short-lived tool-call thread that requested it and
  ends only when stopped or when the backend process exits.
- **Resource limits.** The bootstrap keeps the fixed process profile
  (CPU 300 s, NPROC 64, FSIZE 1 MiB). `RLIMIT_AS` rises from 512 MiB to a
  fixed 8 GiB ceiling, matching #1065's repository sandbox: it bounds
  virtual reservations, not resident memory, and V8 reserves more than
  512 MiB before running any script, so `node` and `npm` could not start
  at all under the old value.
- **The guard.** A fixed Python check runs inside the sandbox before `exec`.
  It requires CapEff and CapBnd to be 0. It verifies that every DENIED path
  fails to open, every READ_ONLY path refuses writes, and every level and
  absent protected name refuses `O_CREAT|O_EXCL`. It reports `ok` on a
  status pipe, then closes every inherited fd above stderr (all of them,
  listed from `/proc/self/fd`) and restores the default SIGTERM, SIGPIPE
  and SIGXFSZ dispositions before `exec`. A launch without that
  sentinel maps to `process_isolation_unavailable:*` and is never reported
  as a command result.
- **Workspace swaps and shutdown.** Restore, interrupted-restore recovery
  and rollback hold a launch fence for the whole operation: new launches are
  refused (`workspace maintenance is in progress`), launches already past
  the gate are awaited until their leader is registered, and every
  process-tool child is stopped and reaped once, before the first root
  rename. A restore refuses to start if that cannot be proven. Recovery and
  rollback are the repair primitives, so they log the failure and continue
  rather than strand a half-moved workspace. Backend shutdown stops and
  reaps every child under the same gate before releasing the workspace
  owner lease.

### Posture, probe and approvals

- **Host probe.** It runs once per bwrap binary and runtime, and is cached.
  It requires every flag the sandbox uses (`bwrap_unsupported` otherwise) and
  `openat2`. It builds a synthetic view in a private temporary directory and
  checks it under both network modes, with typed reasons:
  - `platform_unsupported`, `bwrap_missing`, `bwrap_unsupported`,
    `openat2_unavailable`, `userns_denied`, `probe_failed`, `probe_mismatch`,
    `runtime_root_unsafe`;
  - `container_unsupported` inside any container (see Deployment posture).
- **Blocked hosts.** Hosts that cannot provide the sandbox, including macOS
  and Windows, block `run_command` and `start_process` with
  `process_isolation_unavailable:<reason>` before any approval is created.
  There is no unprotected mode.
- **Approval binding.** The approval context binds the posture digest
  (`process_isolation_posture_sha256`), so a posture change invalidates
  earlier approvals. At launch, the manager compares the consumed approval's
  digest with the current posture and refuses a mismatch with
  `process_isolation_posture_changed`. The approval text lists the view,
  including the stale-inode behaviour below and the network line.

### Deployment posture

**The native host is the primary and recommended runtime for agent
commands.** Run Seraph on the host (`./manage.sh -e dev local up`, or the
equivalent production host install) to use `run_command` and `start_process`.

**Docker is supported without agent command tools.** Inside a container
(detected from `/.dockerenv`, `/run/.containerenv`, the `container` or
Kubernetes environment, cgroup v1 paths, or a root filesystem mounted from a
container runtime's storage), the probe reports
`process_isolation_unavailable:container_unsupported`. Its remedy is to run
Seraph on the host, with #1135 tracked for container support. The block is
the same as on any other host without the sandbox: it is typed, it is
returned before any approval, and it is logged once at startup. Every other
backend capability keeps working in the container.

Why (operator decision, 2026-10-16, on #1103):
- **Precedent.** The operator's stated rationale is that this matches
  IronClaw and Hermes, which run agent commands on the native host first.
  This ADR records that decision and does not independently re-verify those
  products.
- **Complexity.** Running bubblewrap inside Docker needs a custom seccomp
  profile, a custom AppArmor profile that must be loaded on the host, an
  unmasked `/proc`, a non-root image user and a workspace ownership
  migration. That widens the container's kernel surface and adds deployment
  failure modes.
- **Follow-up.** The container work is preserved on
  `backup/1103-docker-sandbox` and tracked in
  [#1135](https://github.com/seraph-quest/seraph/issues/1135).

## Consequences

- **Read-only levels (parity cost).** Commands cannot create, rename or
  delete entries at the workspace top level, `artifacts/` or
  `artifacts/mail/`. This affects `sed -i soul.md`, rename-replace editors
  and `./out.txt` written with `cwd` at the root. In-place writes to existing
  top-level files still work. The agent file tools are unaffected.
- **Stale inodes.** A top-level file is bound by inode at launch. If Seraph
  replaces it by rename while a command runs, the command keeps the old file,
  and its writes to it are lost.
- **Host files and processes.** Host files outside the workspace and the
  runtime are not visible. Hard-coded `/tmp` writes go to a private tmpfs.
  `PYTHONPATH` entries outside the bound runtimes stop resolving. Other host
  processes are not visible.
- **Residual risks:**
  - a pre-existing hard-link alias of a READ_ONLY file, made outside Seraph,
    stays writable through that alias;
  - a protected path created on the host during a run is invisible inside
    that run;
  - correctness depends on `agent_protected_paths()` declaring every
    protected name.
- **Follow-ups:** agent commands inside containers
  ([#1135](https://github.com/seraph-quest/seraph/issues/1135)), a macOS Seatbelt adapter
  ([#1129](https://github.com/seraph-quest/seraph/issues/1129)), and a network policy for
  `start_process` ([#1130](https://github.com/seraph-quest/seraph/issues/1130)).

## Verification

- `backend/tests/test_process_isolation.py` uses real bwrap. It covers:
  - every script channel: python and node reads and writes, `sed w`,
    `sed -i`, shell redirection, rename and unlink;
  - aliases (symlink, case variant, hard link), absent names, and an
    unchanged host;
  - ordinary work, git, pytest, uv, npm, and real uv/npm extension installs;
  - daemon teardown on stop and timeout, stale inodes, and swap and shutdown
    reaping;
  - typed host reasons, blocking before approval, posture changes, guard
    mismatch and startup failure.
- `test_process_launch_sites.py` allowlists every subprocess launch site.
- Container detection covers each signal, and a container blocks agent
  commands with `container_unsupported` and the host remedy, while argument
  validation and the other process tools keep working.
