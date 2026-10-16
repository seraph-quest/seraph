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
- **The guard.** A fixed Python check runs inside the sandbox before `exec`.
  It requires CapEff and CapBnd to be 0. It verifies that every DENIED path
  fails to open, every READ_ONLY path refuses writes, every level and
  absent protected name refuses `O_CREAT|O_EXCL`, and that all fds above 2
  are closed. It reports `ok` on a status pipe. A launch without that
  sentinel maps to `process_isolation_unavailable:*` and is never reported
  as a command result.
- **Workspace swaps and shutdown.** `_replace_root`, the shared step of
  restore, recovery and rollback, stops and reaps every process-tool child
  first. Backend shutdown does the same before releasing the workspace owner
  lease.

### Posture, probe and approvals

- **Host probe.** It runs once per bwrap binary and runtime, and is cached.
  It requires every flag the sandbox uses (`bwrap_unsupported` otherwise) and
  `openat2`. It builds a synthetic view in a private temporary directory and
  checks it under both network modes, with typed reasons:
  - `platform_unsupported`, `bwrap_missing`, `bwrap_unsupported`,
    `openat2_unavailable`, `userns_denied`, `probe_failed`, `probe_mismatch`,
    `runtime_root_unsafe`;
  - in containers: `container_seccomp_profile_missing`,
    `container_apparmor_denies_mount`, `container_proc_masked`.
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

### Container deployment

The backend container runs process tools in the same sandbox. Its Compose
service sets:

- `security_opt: seccomp=./deploy/seccomp/seraph-backend.json`. This is
  Docker's default profile plus only:
  - `clone` with user, mount, pid, net, ipc or uts namespace flags (a cgroup
    namespace stays denied);
  - `unshare` without the cgroup or time namespace flags;
  - `mount`, `umount2` and `pivot_root`.

  `setns` stays denied and `clone3` stays ENOSYS.
- `apparmor=seraph-backend` (`deploy/apparmor/seraph-backend`). This is
  `docker-default` without its blanket `deny mount`, plus `userns`, `mount`
  and `pivot_root`. The only additional `/proc/sys` write it permits is
  `user/max_user_namespaces`, which `--disable-userns` sets inside its own
  user namespace.
- `systempaths=unconfined`. Docker masks `/proc` paths, and the kernel
  refuses a fresh procfs mount for a pid namespace under a partially masked
  `/proc`.
- `no-new-privileges:true` and `cap_drop: [ALL]`.

The image runs as `USER seraph` (uid and gid 10001). A workspace owned by an
earlier root container is refused at startup (`workspace_ownership_mismatch`)
until a one-time `chown` migration. The probe runs at container startup and
before each launch.

## Consequences

- **Kernel surface (containers).** Inside the backend container, user, mount,
  pid, net, ipc and uts namespace creation and mount operations become
  reachable. This is the same kernel attack surface as on a bare-metal
  Ubuntu host with the bwrap AppArmor profile, and wider than Docker's
  default seccomp, which reserves these syscalls for `CAP_SYS_ADMIN`.
  `systempaths=unconfined` also exposes `/proc` entries that Docker masks
  (for example `/proc/kcore` and `/proc/sys`). They stay unreadable or
  unwritable to the non-root, capability-free container user through file
  permissions and the AppArmor denies. The cgroup namespace, `setns`,
  `clone3`, the new mount API and `bpf` stay denied.
- **AppArmor loading.** On AppArmor hosts the profile must be loaded once:
  `sudo apparmor_parser -r -W deploy/apparmor/seraph-backend`. AppArmor 3
  parsers (Ubuntu 23.04 and earlier) do not know the `userns` rule. Delete
  that line there; those kernels do not mediate user namespaces.
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
- **Follow-ups:** a macOS Seatbelt adapter
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
- `test_container_process_sandbox.py` parses Compose, the Dockerfile and both
  profiles.
- The CI job `container-process-sandbox` builds the image. Under the shipped
  profiles it must report an isolated posture and refuse `.vault-key` to a
  real command. Under Docker's defaults it must report a typed blocked
  reason.
