"""OS-enforced workspace view for agent process tools (#1103).

``run_command`` and ``start_process`` (``ProcessRuntimeManager``) execute every
agent command inside a bubblewrap sandbox whose workspace is a *view*:

- the workspace root and every ancestor directory of a protected registry path
  (the "synthetic levels": ``.``, ``artifacts`` and ``artifacts/mail``) are a
  private tmpfs, remounted read-only once their children are placed, so no
  entry can be created, renamed or deleted there;
- each entry present at a synthetic level at launch is classified with the
  #1102 registry (name, NFC/casefold and inode identity) and placed as:
  READ_WRITE -> ``--bind-fd``, READ_ONLY -> ``--ro-bind-fd``, DENIED -> a
  mode-0000 placeholder (present, unopenable); absent DENIED names also get a
  placeholder;
- below a READ_WRITE child the host tree is bound unchanged; symlinks anywhere
  resolve inside the view, so a link to protected state lands on its
  placeholder.

The kernel enforces the view, so script content (``python``/``node`` file
opens, ``sed w``, ``sed -i``, shell redirection reached from a script) cannot
reach DENIED state or write READ_ONLY state.

Every launch is fail-closed. A host that cannot provide the sandbox yields
``process_isolation_unavailable:<reason>``; there is no unprotected mode.
The view is built with ``openat2(RESOLVE_BENEATH | RESOLVE_NO_MAGICLINKS)``
from one root directory fd, every child is classified by ``fstat`` identity
of the fd that is then bound (bwrap re-checks that identity after mounting and
aborts on a swap), and a per-launch guard inside the sandbox re-verifies the
view and the dropped capabilities before ``exec``.

Reused from #1065 (``repo_isolation``): the runtime binding helpers, the
runtime-root safety check and the host secret/forbidden path lists.
"""
from __future__ import annotations

import ctypes
from dataclasses import dataclass, field
import errno
import hashlib
import json
import os
from pathlib import Path
import posixpath
import resource
import select
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable, Iterable, Literal, Mapping, Sequence

from src.execution.repo_isolation import (
    IsolationRuntime,
    _digest,
    _executable_paths,
    _file_sha256,
    _prefix_library_paths,
    _under_usr,
    host_forbidden_roots,
    host_secret_paths,
    runtime_root_problem,
)

READ_WRITE = "read_write"
READ_ONLY = "read_only"
DENIED = "denied"
_ACCESS_RANK = {READ_WRITE: 0, READ_ONLY: 1, DENIED: 2}

BLOCKED_PREFIX = "process_isolation_unavailable"
POSTURE_CHANGED = "process_isolation_posture_changed"
# Host reasons; the view adds ``workspace_view_unsafe:*`` per launch and the
# guard ``workspace_view_mismatch:*``.
HOST_REASONS = frozenset({
    "platform_unsupported", "bwrap_missing", "bwrap_unsupported", "openat2_unavailable", "userns_denied",
    "probe_failed", "probe_mismatch", "runtime_root_unsafe", "container_seccomp_profile_missing",
    "container_apparmor_denies_mount", "container_proc_masked",
})
_REMEDIES = {
    "platform_unsupported": "agent command tools need the Linux bubblewrap sandbox; macOS and Windows are not supported yet",
    "bwrap_missing": "install bubblewrap (apt install bubblewrap)",
    "bwrap_unsupported": "install bubblewrap 0.8 or newer (fd binds, --disable-userns, --perms)",
    "openat2_unavailable": "the kernel must provide openat2 (Linux 5.6 or newer)",
    "userns_denied": ("allow unprivileged user namespaces for bwrap: on Ubuntu install the bwrap-userns-restrict "
                      "AppArmor profile or set kernel.apparmor_restrict_unprivileged_userns=0"),
    "probe_failed": "the sandbox self-test did not complete; check that bwrap runs for this user",
    "probe_mismatch": "the sandbox self-test observed weaker isolation than required",
    "runtime_root_unsafe": "a command runtime lies in or contains $HOME, credentials or the workspace",
    "container_seccomp_profile_missing": ("start the backend container with "
                                          "security_opt seccomp=deploy/seccomp/seraph-backend.json"),
    "container_apparmor_denies_mount": ("load deploy/apparmor/seraph-backend and start the container with "
                                        "security_opt apparmor=seraph-backend"),
    "container_proc_masked": "start the backend container with security_opt systempaths=unconfined",
    "sandbox_startup_failed": "bubblewrap exited before the command started; the host is re-probed on the next command",
    "sandbox_startup_timeout": "the sandbox did not start in time; nothing ran",
    "workspace_view_mismatch": "the in-sandbox guard found the workspace view weaker than built; nothing ran",
    "workspace_view_unsafe": "the workspace cannot be represented safely in the sandbox view; fix the named entry",
}
NOT_VISIBLE_NOTE = "Host files outside the workspace are not visible."

READ_ONLY_LEVEL_NOTE = ("New files cannot be created, renamed or deleted at the workspace top level, "
                        "artifacts/ or artifacts/mail/.")
STALE_INODE_NOTE = ("Top-level workspace files are bound by inode at launch: if Seraph replaces one by rename "
                    "while the command runs, the command keeps the old file and its writes to it are lost.")
LOCALHOST_NOTE = "This process can reach localhost services, including Seraph's own API."

PROBE_TIMEOUT_SECONDS = 15
STARTUP_TIMEOUT_SECONDS = 15
MAX_LEVEL_CHILDREN = 512
MAX_DENIED_WALK_ENTRIES = 50_000
VIEW_TMPFS_BYTES = 1024 * 1024
SANDBOX_TMP_BYTES = 256 * 1024 * 1024
_FD_HEADROOM = 64
_FORBIDDEN_VIEW_ROOTS = ("/usr", "/etc", "/proc", "/dev", "/sys", "/run")
# Every flag the sandbox uses; the probe requires each one in ``bwrap --help``.
REQUIRED_BWRAP_FLAGS = (
    "--args", "--bind-fd", "--ro-bind-fd", "--remount-ro", "--cap-drop", "--disable-userns",
    "--assert-userns-disabled", "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
    "--unshare-net", "--die-with-parent", "--perms", "--size", "--tmpfs", "--file", "--dir", "--symlink",
    "--proc", "--dev", "--chdir", "--ro-bind", "--bind",
)


class ProcessIsolationBlocked(ValueError):
    """A launch refused because the OS sandbox or view cannot be provided."""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        message = code if not detail else f"{code} ({detail})"
        super().__init__(message)


def blocked(reason: str, diagnostic: str = "") -> ProcessIsolationBlocked:
    base = reason.split(":", 1)[0]
    detail = "; ".join(part for part in (_REMEDIES.get(base, ""), diagnostic) if part)
    return ProcessIsolationBlocked(f"{BLOCKED_PREFIX}:{reason}", detail)


# ------------------------------------------------------------------ openat2

RESOLVE_NO_MAGICLINKS = 0x02
RESOLVE_NO_SYMLINKS = 0x04
RESOLVE_BENEATH = 0x08
_SYS_OPENAT2 = 437  # same number on every Linux architecture (unified table)


class _OpenHow(ctypes.Structure):
    _fields_ = [("flags", ctypes.c_uint64), ("mode", ctypes.c_uint64), ("resolve", ctypes.c_uint64)]


_libc = None


def _syscall():
    global _libc
    if _libc is None:
        _libc = ctypes.CDLL(None, use_errno=True)
        _libc.syscall.restype = ctypes.c_long
    return _libc.syscall


def openat2(dirfd: int, path: str, flags: int, resolve: int) -> int:
    """``openat2(2)`` (Python has no wrapper). Always ``O_CLOEXEC``."""
    if sys.platform != "linux":
        raise OSError(errno.ENOSYS, "openat2 is Linux-only")
    how = _OpenHow(flags | os.O_CLOEXEC, 0, resolve)
    encoded = os.fsencode(path)
    fd = _syscall()(ctypes.c_long(_SYS_OPENAT2), ctypes.c_int(dirfd), ctypes.c_char_p(encoded),
                    ctypes.byref(how), ctypes.c_size_t(ctypes.sizeof(how)))
    if fd < 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), path)
    return int(fd)


def _beneath(dirfd: int, path: str, flags: int, *, symlinks: bool = False) -> int:
    resolve = RESOLVE_BENEATH | RESOLVE_NO_MAGICLINKS | (0 if symlinks else RESOLVE_NO_SYMLINKS)
    return openat2(dirfd, path, flags, resolve)


def openat2_available() -> bool:
    try:
        fd = openat2(-100, "/", os.O_PATH | os.O_DIRECTORY, 0)  # AT_FDCWD
    except OSError:
        return False
    os.close(fd)
    return True


# --------------------------------------------------------------- the view

def synthetic_levels(protected: Iterable[str]) -> frozenset[str]:
    """The workspace root plus every ancestor directory of a protected path."""
    levels = {"."}
    for logical in protected:
        parts = logical.split("/")
        for depth in range(1, len(parts)):
            levels.add("/".join(parts[:depth]))
    return frozenset(levels)


def _strictest(*accesses: str) -> str:
    return max(accesses, key=_ACCESS_RANK.__getitem__)


def _anonymous_file() -> int:
    """An unlinked private file (memfd where the interpreter exposes it)."""
    if hasattr(os, "memfd_create"):
        return os.memfd_create("seraph-process-sandbox", os.MFD_CLOEXEC)
    return os.open(tempfile.gettempdir(), os.O_TMPFILE | os.O_RDWR | os.O_CLOEXEC, 0o600)


def _logical(level: str, name: str) -> str:
    return name if level == "." else f"{level}/{name}"


@dataclass
class WorkspaceView:
    """bwrap arguments and bound fds for one launch; ``close`` releases the fds."""
    root: Path
    args: list[str]
    fds: list[int]
    denied: tuple[str, ...]
    read_only: tuple[tuple[str, bool], ...]
    absent: tuple[str, ...]
    levels: tuple[str, ...]
    digest: str
    entries: int

    def close(self) -> None:
        while self.fds:
            try:
                os.close(self.fds.pop())
            except OSError:
                pass

    def summary(self) -> dict:
        return {"workspace_view": "os_enforced", "workspace_view_sha256": self.digest,
                "workspace_view_denied_paths": len(self.denied),
                "workspace_view_read_only_paths": len(self.read_only),
                "workspace_view_read_only_levels": list(self.levels)}


class _ViewBuilder:
    def __init__(self, root: Path, protected: Sequence[tuple[str, str]], access_for: Callable[[str], str],
                 *, max_children: int):
        self.root = root
        self.protected = {logical: access for logical, access in protected}
        self.access_for = access_for
        self.max_children = max_children
        self.levels = synthetic_levels(self.protected)
        self.args: list[str] = []
        self.fds: list[int] = []
        self.denied: list[str] = []
        self.read_only: list[tuple[str, bool]] = []
        self.absent: list[str] = []
        self.record: list[list] = []
        self.walked = 0
        self._placeholder_fd: int | None = None

    def fail(self, reason: str):
        self.close()
        raise blocked(f"workspace_view_unsafe:{reason}")

    def close(self) -> None:
        for fd in self.fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self.fds.clear()

    def dest(self, logical: str) -> str:
        return str(self.root) if logical == "." else str(self.root / logical)

    def placeholder(self, logical: str, *, directory: bool) -> None:
        if directory:
            self.args += ["--perms", "0000", "--dir", self.dest(logical)]
        else:
            # Each --file consumes its fd, so every placeholder gets its own.
            fd = _anonymous_file()
            self.fds.append(fd)
            self.args += ["--perms", "0000", "--file", str(fd), self.dest(logical)]
        self.denied.append(self.dest(logical))

    def build(self) -> WorkspaceView:
        root = self.root
        if (str(root) == "/" or any(str(root) == item or str(root).startswith(item + "/")
                                    for item in _FORBIDDEN_VIEW_ROOTS)):
            self.fail("root_unbindable")
        try:
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            self.fail("root_unbindable")
        self.root_fd = root_fd
        try:
            if (os.fstat(root_fd).st_dev, os.fstat(root_fd).st_ino) != (os.stat(root).st_dev, os.stat(root).st_ino):
                self.fail("root_unbindable")
            identities = self._identity_map(root_fd)
            self.args += ["--perms", "0755", "--size", str(VIEW_TMPFS_BYTES), "--tmpfs", str(root)]
            ordered = sorted(self.levels - {"."}, key=lambda item: (item.count("/"), item))
            for level in ordered:
                self.args += ["--dir", self.dest(level)]
            for level in [".", *ordered]:
                self._level(root_fd, level, identities)
            self.args += ["--remount-ro", str(root)]
        finally:
            os.close(root_fd)
        digest = _digest({"root": str(root), "entries": self.record, "levels": sorted(self.levels)})
        return WorkspaceView(root, self.args, self.fds, tuple(self.denied), tuple(self.read_only),
                             tuple(self.absent), tuple(sorted(self.levels, key=lambda item: (item.count("/"), item))),
                             digest, len(self.record))

    def _identity_map(self, root_fd: int) -> dict[tuple[int, int], str]:
        identities: dict[tuple[int, int], str] = {}
        for logical, access in self.protected.items():
            try:
                fd = _beneath(root_fd, logical, os.O_PATH | os.O_NOFOLLOW)
            except OSError:
                continue
            try:
                metadata = os.fstat(fd)
            finally:
                os.close(fd)
            key = (metadata.st_dev, metadata.st_ino)
            identities[key] = _strictest(identities.get(key, READ_WRITE), access)
        return identities

    def _level(self, root_fd: int, level: str, identities) -> None:
        try:
            level_fd = root_fd if level == "." else _beneath(root_fd, level, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            level_fd = None
        except OSError:
            self.fail(f"synthetic_level_not_directory:{level}")
        present: set[str] = set()
        try:
            if level_fd is not None:
                names = sorted(os.listdir(level_fd))
                if len(names) > self.max_children:
                    self.fail("too_many_entries")
                self._require_fd_headroom(len(names))
                for name in names:
                    logical = _logical(level, name)
                    present.add(logical)
                    if logical in self.levels:
                        continue
                    self._child(level_fd, level, name, logical, identities)
        finally:
            if level_fd is not None and level_fd != root_fd:
                os.close(level_fd)
        for logical, access in sorted(self.protected.items()):
            if (posixpath.dirname(logical) or ".") != level or logical in present or logical in self.levels:
                continue
            self.absent.append(self.dest(logical))
            if access == DENIED:
                self.placeholder(logical, directory=False)
            self.record.append([logical, "absent", access])

    def _require_fd_headroom(self, wanted: int) -> None:
        soft, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft == resource.RLIM_INFINITY:
            return
        try:
            open_now = len(os.listdir("/proc/self/fd"))
        except OSError:
            open_now = 0
        if open_now + len(self.fds) + wanted + _FD_HEADROOM > soft:
            self.fail("too_many_entries")

    def _child(self, level_fd: int, level: str, name: str, logical: str, identities) -> None:
        try:
            fd = _beneath(level_fd, name, os.O_PATH | os.O_NOFOLLOW)
        except OSError as exc:
            if exc.errno == errno.EMFILE:
                self.fail("too_many_entries")
            self.fail(f"entry_unreadable:{logical}")
        metadata = os.fstat(fd)
        access = _strictest(self.access_for(logical), identities.get((metadata.st_dev, metadata.st_ino), READ_WRITE))
        kind = ("symlink" if stat.S_ISLNK(metadata.st_mode) else "dir" if stat.S_ISDIR(metadata.st_mode)
                else "file")
        self.record.append([logical, kind, access, metadata.st_dev, metadata.st_ino])
        if kind == "symlink":
            os.close(fd)
            if access != READ_WRITE:
                # A protected name never becomes a link into writable content.
                self.placeholder(logical, directory=False)
                return
            target = os.readlink(name, dir_fd=level_fd)
            if not self._symlink_within_view(level, logical, target):
                self.fail(f"symlink_escapes_view:{logical}")
            self.args += ["--symlink", target, self.dest(logical)]
            return
        if access == DENIED:
            os.close(fd)
            if kind == "file" and stat.S_ISREG(metadata.st_mode) and metadata.st_nlink > 1:
                self.fail(f"hard_link_alias:{logical}")
            if kind == "dir":
                self._walk_denied(level_fd, name, logical)
            self.placeholder(logical, directory=kind == "dir")
            return
        self.fds.append(fd)
        if access == READ_ONLY:
            # READ_ONLY files may have extra hard links (uv/npm install from a
            # cache outside the workspace); link(2) across the read-only bind
            # fails with EXDEV, so no new alias can be made from inside.
            self.args += ["--ro-bind-fd", str(fd), self.dest(logical)]
            self.read_only.append((self.dest(logical), kind == "dir"))
        else:
            self.args += ["--bind-fd", str(fd), self.dest(logical)]

    def _walk_denied(self, parent_fd: int, name: str, logical: str) -> None:
        """Refuse a DENIED tree holding a regular file reachable by another name."""
        try:
            top = _beneath(parent_fd, name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            return  # unreadable even to Seraph: nothing an alias could expose
        pending = [(top, logical)]
        try:
            while pending:
                directory_fd, directory = pending.pop()
                try:
                    for entry in sorted(os.listdir(directory_fd)):
                        self.walked += 1
                        if self.walked > MAX_DENIED_WALK_ENTRIES:
                            # The hard-link proof needs every DENIED entry;
                            # past the bound the launch is refused, never
                            # assumed safe.
                            self.fail(f"denied_tree_too_large:{logical}")
                        child = f"{directory}/{entry}"
                        try:
                            entry_fd = _beneath(directory_fd, entry, os.O_PATH | os.O_NOFOLLOW)
                        except OSError:
                            continue
                        try:
                            metadata = os.fstat(entry_fd)
                        finally:
                            os.close(entry_fd)
                        if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink > 1:
                            self.fail(f"hard_link_alias:{child}")
                        if stat.S_ISDIR(metadata.st_mode):
                            try:
                                pending.append((_beneath(directory_fd, entry,
                                                         os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW), child))
                            except OSError:
                                continue
                finally:
                    os.close(directory_fd)
        finally:
            for directory_fd, _ in pending:
                os.close(directory_fd)

    def _symlink_within_view(self, level: str, logical: str, target: str) -> bool:
        """A level symlink is recreated only when its target stays beneath the view."""
        if not target:
            return False
        if target.startswith("/"):
            relative = os.path.relpath(posixpath.normpath(target), str(self.root))
        else:
            relative = posixpath.normpath(posixpath.join("" if level == "." else level, target))
        if relative == ".." or relative.startswith("../") or relative.startswith("/"):
            return False
        if relative == ".":
            return True
        try:
            fd = _beneath(self.root_fd, relative, os.O_PATH, symlinks=True)
        except FileNotFoundError:
            return True  # dangling, lexically inside: resolves within the view
        except OSError:
            return False  # EXDEV (escapes), ELOOP, ...
        os.close(fd)
        return True


def build_workspace_view(root: str | os.PathLike, protected: Sequence[tuple[str, str]],
                         access_for: Callable[[str], str], *,
                         max_children: int = MAX_LEVEL_CHILDREN) -> WorkspaceView:
    """Classify the synthetic levels of ``root`` into bwrap view arguments.

    ``protected`` lists every (logical path, access) the registry protects;
    ``access_for`` classifies one logical path. Raises ``ProcessIsolationBlocked``
    (``workspace_view_unsafe:*``) for anything the view cannot represent safely.
    """
    builder = _ViewBuilder(Path(os.path.normpath(Path(root).absolute())), protected, access_for,
                           max_children=max_children)
    try:
        return builder.build()
    except BaseException:
        builder.close()
        raise


# ------------------------------------------------------------------- guard

GUARD_SOURCE = r"""
import json, os, signal, sys
spec = json.loads(sys.argv[1])
status = spec["status_fd"]
def fail(reason):
    try:
        os.write(status, ("mismatch:" + reason + "\n").encode())
    finally:
        os._exit(125)
fields = {}
with open("/proc/self/status") as handle:
    for line in handle:
        key, _, value = line.partition(":")
        fields[key] = value.strip()
if int(fields.get("CapEff", "1"), 16) or int(fields.get("CapBnd", "1"), 16):
    fail("capabilities_present")
def refused(path, flags):
    try:
        fd = os.open(path, flags | os.O_NONBLOCK | os.O_CLOEXEC, 0o600)
    except OSError:
        return True
    os.close(fd)
    if flags & os.O_CREAT:
        try:
            os.unlink(path)
        except OSError:
            pass
    return False
probe = ".seraph-view-probe-%d" % os.getpid()
for path in spec["denied"]:
    if not refused(path, os.O_RDONLY):
        fail("denied_readable:" + path)
for path, directory in spec["read_only"]:
    target, flags = (os.path.join(path, probe), os.O_WRONLY | os.O_CREAT | os.O_EXCL) if directory else (path, os.O_WRONLY)
    if not refused(target, flags):
        fail("read_only_writable:" + path)
for path in spec["levels"]:
    if not refused(os.path.join(path, probe), os.O_WRONLY | os.O_CREAT | os.O_EXCL):
        fail("level_writable:" + path)
for path in spec["absent"]:
    if not refused(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL):
        fail("absent_creatable:" + path)
for name in ("SIGPIPE", "SIGXFZ", "SIGXFSZ", "SIGTERM"):
    if hasattr(signal, name):
        signal.signal(getattr(signal, name), signal.SIG_DFL)
os.write(status, b"ok\n")
# Close every inherited fd above stderr, however high (the listing fd is
# closed when listdir returns, so it is never in the list).
for fd in sorted(int(name) for name in os.listdir("/proc/self/fd")):
    if fd > 2:
        try:
            os.close(fd)
        except OSError:
            pass
argv = sys.argv[2:]
try:
    os.execvpe(argv[0], argv, os.environ)
except OSError:
    print("process_target_exec_unavailable", file=sys.stderr)
    os._exit(127)
"""

PROBE_SOURCE = r"""
import json, os, socket, sys
checks = json.loads(sys.argv[1])
def attempt(path, flags):
    try:
        os.close(os.open(path, flags, 0o600))
        return "ok"
    except OSError as exc:
        return exc.errno
try:
    os.unshare(os.CLONE_NEWUSER)
    nested = "allowed"
except (OSError, AttributeError):
    nested = "denied"
try:
    init = open("/proc/1/cmdline", "rb").read().split(b"\0")[0].decode(errors="replace")
except OSError:
    init = ""
print(json.dumps({
    "interfaces": sorted(name for _, name in socket.if_nameindex()),
    "visible": [path for path in checks["hidden"] if os.path.lexists(path)],
    "nested_userns": nested,
    "pid1": os.path.basename(init),
    "rw": attempt(checks["rw"], os.O_WRONLY | os.O_CREAT | os.O_TRUNC),
    "ro": attempt(checks["ro"], os.O_WRONLY),
    "denied": attempt(checks["denied"], os.O_RDONLY),
    "level": attempt(checks["level"], os.O_WRONLY | os.O_CREAT | os.O_EXCL),
}))
"""


# --------------------------------------------------------------- posture

@dataclass(frozen=True)
class ProcessIsolationPosture:
    variant: Literal["isolated", "unavailable"]
    reason: str | None = None
    bwrap_path: str | None = None
    bwrap_sha256: str | None = None
    probe_sha256: str | None = None
    runtime_roots: str = ""
    in_container: bool = False

    @property
    def isolated(self) -> bool:
        return self.variant == "isolated"

    @property
    def code(self) -> str:
        return f"{BLOCKED_PREFIX}:{self.reason}"

    def approval_fields(self) -> dict:
        """Fields bound into the approval fingerprint (paths stay server-side)."""
        fields = {"process_isolation_variant": self.variant,
                  "process_isolation_claim": "bwrap_userns_workspace_view",
                  "process_isolation_bwrap_sha256": self.bwrap_sha256,
                  "process_isolation_probe_sha256": self.probe_sha256,
                  "process_isolation_runtime_roots_sha256": hashlib.sha256(self.runtime_roots.encode()).hexdigest()}
        fields["process_isolation_posture_sha256"] = _digest(fields)
        return fields

    @property
    def digest(self) -> str:
        return self.approval_fields()["process_isolation_posture_sha256"]


def _unavailable(reason: str, *, in_container: bool = False) -> ProcessIsolationPosture:
    return ProcessIsolationPosture("unavailable", reason=reason, in_container=in_container)


def in_container() -> bool:
    """True inside Docker/Podman/Kubernetes (used only to type a probe failure)."""
    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        return True
    try:
        text = Path("/proc/1/cgroup").read_text(errors="replace")
    except OSError:
        return False
    return any(marker in text for marker in ("docker", "containerd", "kubepods", "libpod"))


_help_cache: dict[str, frozenset[str]] = {}


def bwrap_flags(bwrap_path: str) -> frozenset[str]:
    if bwrap_path not in _help_cache:
        try:
            completed = subprocess.run([bwrap_path, "--help"], capture_output=True, timeout=5,
                                       env={"PATH": "/usr/bin:/bin"})
            text = (completed.stdout + completed.stderr).decode(errors="replace")
        except (OSError, subprocess.SubprocessError):
            text = ""
        _help_cache[bwrap_path] = frozenset(token for token in text.split() if token.startswith("--"))
    return _help_cache[bwrap_path]


def classify_startup_failure(stderr: str, *, container: bool) -> str:
    """Map a bwrap startup diagnostic to a typed host reason."""
    text = stderr or ""
    if "Can't mount proc" in text or "mount proc" in text:
        return "container_proc_masked" if container else "probe_failed"
    userns = ("No permissions to create new namespace" in text or "setting up uid map" in text
              or "Creating new namespace failed" in text or "unshare" in text.lower() and "permitted" in text)
    if userns:
        return "container_seccomp_profile_missing" if container else "userns_denied"
    if "Operation not permitted" in text or "Permission denied" in text:
        if container:
            return "container_apparmor_denies_mount" if "mount" in text.lower() else "container_seccomp_profile_missing"
        return "userns_denied"
    return "probe_failed"


def sandbox_path(executables: Mapping[str, Path]) -> str:
    """PATH inside the sandbox: the bound executable directories, then /usr."""
    entries: list[str] = []
    for path in executables.values():
        directory = str(Path(path).parent)
        if directory not in entries and not _under_usr(Path(directory)):
            entries.append(directory)
    return ":".join([*entries, "/usr/local/bin", "/usr/bin", "/bin"])


def _asdf_shim_target(path: Path, name: str) -> Path | None:
    """The real binary behind an asdf shim, from the shim header and ~/.tool-versions."""
    try:
        if path.stat().st_size > 4096:
            return None
        text = path.read_text(errors="replace")
    except OSError:
        return None
    if not text.startswith("#!") or "asdf exec" not in text:
        return None
    plugins = {}
    for line in text.splitlines():
        if line.startswith("# asdf-plugin:"):
            parts = line.split()
            if len(parts) >= 4:
                plugins.setdefault(parts[2], []).append(parts[3])
    data_dir = path.parent.parent
    try:
        versions = (Path.home() / ".tool-versions").read_text(errors="replace").splitlines()
    except OSError:
        versions = []
    for plugin, available in plugins.items():
        selected = next((line.split()[1:] for line in versions if line.split()[:1] == [plugin]), [])
        for version in [*selected, *reversed(available)]:
            candidate = data_dir / "installs" / plugin / version / "bin" / name
            if version in available and candidate.is_file():
                return candidate
    return None


def resolve_executables(names: Iterable[str], path_env: str) -> dict[str, Path]:
    """Host executables for allowlisted names, version-manager shims resolved."""
    resolved: dict[str, Path] = {}
    for name in names:
        found = shutil.which(name, path=path_env)
        if found is None:
            continue
        path = Path(found).absolute()
        resolved[name] = _asdf_shim_target(path, name) or path
    return resolved


def process_runtime(executables: Mapping[str, Path], *, interpreter: str | os.PathLike = sys.executable
                    ) -> IsolationRuntime:
    """Read-only host paths for the interpreter and the allowlisted executables.

    Each executable file and its symlink hops; Node additionally gets only its
    bundled npm tree; the interpreter its library trees (``for_python``).
    Directories such as ``~/.local/bin`` are never bound wholesale.
    """
    base = IsolationRuntime.for_python(interpreter)
    paths: set[Path] = set(base.read_only_paths)
    for path in executables.values():
        paths.update(_executable_paths(path))
        real = Path(path).resolve()
        if real.name.startswith("node") and real.parent.name == "bin":
            paths.update(_prefix_library_paths(real.parent.parent, node=True))
    paths = {Path(os.path.normpath(path)) for path in paths if not _under_usr(path)}
    identity = _digest({"interpreter": str(interpreter), "paths": sorted(map(str, paths))})
    return IsolationRuntime(Path(interpreter), tuple(sorted(paths)), identity)


def workspace_venv_runtime_paths(cwd: Path, workspace_root: Path, *, forbidden_ancestors: Sequence[str],
                                 forbidden_trees: Sequence[str]) -> tuple[Path, ...]:
    """Base interpreters of workspace virtualenvs (uv/venv ``pyvenv.cfg``).

    A workspace ``.venv`` symlinks to an interpreter outside the workspace;
    its prefix is bound read-only when it is a real Python installation that
    lies outside the forbidden trees, otherwise the launch is blocked
    (``runtime_root_unsafe``).
    """
    candidates: list[Path] = []
    current = Path(cwd)
    while True:
        candidates += [current / ".venv" / "pyvenv.cfg", current / "pyvenv.cfg"]
        if current == workspace_root or workspace_root not in current.parents:
            break
        current = current.parent
    paths: set[Path] = set()
    for config in candidates:
        try:
            if config.is_symlink() or not config.is_file() or config.stat().st_size > 4096:
                continue
            lines = config.read_text(errors="replace").splitlines()
        except OSError:
            continue
        home = next((line.split("=", 1)[1].strip() for line in lines
                     if line.split("=", 1)[0].strip() == "home" and "=" in line), "")
        if not home.startswith("/"):
            continue
        bin_dir = Path(os.path.normpath(home))
        if bin_dir == workspace_root or workspace_root in bin_dir.parents:
            continue  # inside the workspace: already visible through the view
        prefix = bin_dir.parent
        libraries = _prefix_library_paths(prefix)
        if not any((library / name / "os.py").is_file() for library in libraries if library.is_dir()
                   for name in (os.listdir(library) if library.is_dir() else ()) if name.startswith("python3")):
            continue  # not a Python installation
        found: set[Path] = set(libraries)
        for name in ("python3", "python"):
            if (bin_dir / name).exists():
                found.update(_executable_paths(bin_dir / name))
        found = {path for path in found if not _under_usr(path)}
        if runtime_root_problem(sorted(found), forbidden_ancestors=forbidden_ancestors, forbidden_trees=forbidden_trees):
            raise blocked("runtime_root_unsafe")
        paths.update(found)
    return tuple(sorted(paths))


# ------------------------------------------------------- command assembly

@dataclass
class SandboxLaunch:
    argv: list[str]
    pass_fds: list[int]
    status_read_fd: int
    view: WorkspaceView
    _owned: list[int] = field(default_factory=list)

    def close_child_fds(self) -> None:
        """Release the fds only the child needed (after spawn or on failure)."""
        for fd in self._owned:
            try:
                os.close(fd)
            except OSError:
                pass
        self._owned.clear()
        self.view.close()

    def close(self) -> None:
        self.close_child_fds()
        try:
            os.close(self.status_read_fd)
        except OSError:
            pass


def _runtime_args(runtime_paths: Iterable[Path]) -> list[str]:
    args: list[str] = []
    seen: set[Path] = set()
    for path in sorted({Path(os.path.normpath(item)) for item in runtime_paths}, key=lambda item: len(item.parts)):
        if _under_usr(path) or path in seen:
            continue
        seen.add(path)
        if path.is_symlink():
            args += ["--symlink", os.readlink(path), str(path)]
        else:
            args += ["--ro-bind", str(path), str(path)]
    return args


def sandbox_launch(target: Sequence[str], *, posture: ProcessIsolationPosture, runtime: IsolationRuntime,
                   view: WorkspaceView, cwd: Path, writable: Sequence[Path] = (), network: Literal["none", "host"],
                   extra_runtime_paths: Sequence[Path] = (), bootstrap: Sequence[str] = ()) -> SandboxLaunch:
    """The argv (bootstrap -> bwrap -> guard -> target) and fds for one launch."""
    if not posture.isolated:
        raise blocked(posture.reason or "probe_failed")
    if not target or any(type(item) is not str or "\x00" in item for item in target):
        raise ValueError("argv must be non-empty strings")
    args = ["--unshare-user", "--unshare-ipc", "--unshare-pid", "--unshare-uts"]
    if network == "none":
        args.append("--unshare-net")
    # No --new-session: the leader keeps the bootstrap's session and process
    # group so the manager's group tree-kill reaches every sandbox process.
    args += ["--die-with-parent", "--cap-drop", "ALL", "--disable-userns", "--assert-userns-disabled",
             "--ro-bind", "/usr", "/usr"]
    for name in ("bin", "sbin", "lib", "lib64", "lib32"):
        if Path("/" + name).is_symlink() and os.readlink("/" + name).lstrip("/").startswith("usr/"):
            args += ["--symlink", os.readlink("/" + name).lstrip("/"), "/" + name]
    args += ["--ro-bind", "/etc", "/etc", "--proc", "/proc", "--dev", "/dev",
             "--size", str(SANDBOX_TMP_BYTES), "--tmpfs", "/tmp"]
    args += _runtime_args([*runtime.read_only_paths, *extra_runtime_paths])
    for path in sorted({Path(os.path.normpath(item)) for item in writable}, key=lambda item: len(item.parts)):
        if _under_usr(path) or path == view.root or view.root in path.parents:
            raise ValueError("writable sandbox paths must lie outside /usr and the workspace")
        args += ["--bind", str(path), str(path)]
    args += view.args
    args += ["--remount-ro", "/", "--chdir", str(cwd)]
    status_read, status_write = os.pipe2(os.O_CLOEXEC)
    args_fd = _anonymous_file()
    try:
        os.write(args_fd, b"".join(os.fsencode(item) + b"\0" for item in args))
        os.lseek(args_fd, 0, os.SEEK_SET)
    except OSError:
        for fd in (status_read, status_write, args_fd):
            os.close(fd)
        raise
    spec = {"status_fd": status_write, "denied": list(view.denied), "read_only": [list(item) for item in view.read_only],
            "levels": [str(view.root) if level == "." else str(view.root / level) for level in view.levels],
            "absent": list(view.absent)}
    argv = [*bootstrap, posture.bwrap_path, "--args", str(args_fd), "--",
            str(runtime.interpreter), "-I", "-S", "-B", "-c", GUARD_SOURCE, json.dumps(spec), *target]
    return SandboxLaunch(argv, [args_fd, status_write, *view.fds], status_read, view, [args_fd, status_write])


def await_startup(popen: subprocess.Popen, status_read_fd: int, *, deadline: float) -> str | None:
    """None once the guard reported ``ok``; otherwise the startup failure reason.

    bwrap exits before ``exec`` without the guard's sentinel when it cannot
    build the sandbox; the guard reports ``mismatch:<reason>`` when the view
    or capabilities are not what was requested.
    """
    buffer = b""
    while b"\n" not in buffer:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "sandbox_startup_timeout"
        ready, _, _ = select.select([status_read_fd], [], [], min(0.05, remaining))
        if ready:
            chunk = os.read(status_read_fd, 512)
            if not chunk:
                break
            buffer += chunk
            continue
        if popen.poll() is not None:
            # The writers are gone; drain anything written before exit.
            os.set_blocking(status_read_fd, False)
            try:
                buffer += os.read(status_read_fd, 512)
            except (BlockingIOError, OSError):
                pass
            break
    line = buffer.split(b"\n", 1)[0].decode(errors="replace")
    if line == "ok":
        return None
    if line.startswith("mismatch:"):
        return "workspace_view_mismatch:" + line[len("mismatch:"):]
    return "sandbox_startup_failed"


# ------------------------------------------------------------------ probe

_cache: dict[tuple, ProcessIsolationPosture] = {}
_cache_lock = threading.Lock()


def probe(runtime: IsolationRuntime, *, hidden_paths: Sequence[str] = (), forbidden_ancestors: Sequence[str] = (),
          forbidden_trees: Sequence[str] = (), use_cache: bool = True) -> ProcessIsolationPosture:
    """Verify, inside an actual sandbox built with the exact flags, that the view holds.

    The probe builds a synthetic view in a private temporary directory (never
    the workspace), runs the guard and a fixed check under both network
    modes, and requires: loopback-only network when unshared, no nested user
    namespaces, a bwrap init as pid 1, CapEff/CapBnd 0 (guard), writable
    READ_WRITE, read-only READ_ONLY, unopenable DENIED, a non-creatable level
    and no hidden host path. No packet is sent.
    """
    container = in_container()
    if sys.platform != "linux":
        return _unavailable("platform_unsupported", in_container=container)
    if runtime_root_problem(runtime.read_only_paths, forbidden_ancestors=forbidden_ancestors,
                            forbidden_trees=forbidden_trees):
        return _unavailable("runtime_root_unsafe", in_container=container)
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        return _unavailable("bwrap_missing", in_container=container)
    bwrap_path = str(Path(bwrap).resolve())
    if not set(REQUIRED_BWRAP_FLAGS) <= bwrap_flags(bwrap_path):
        return _unavailable("bwrap_unsupported", in_container=container)
    if not openat2_available():
        return _unavailable("openat2_unavailable", in_container=container)
    hidden = sorted(str(Path(os.path.normpath(path))) for path in hidden_paths)
    key = (_file_sha256(Path(bwrap_path)), runtime.identity, str(runtime.interpreter), tuple(hidden), container)
    if use_cache:
        with _cache_lock:
            if key in _cache:
                return _cache[key]
    candidate = ProcessIsolationPosture("isolated", bwrap_path=bwrap_path, bwrap_sha256=key[0],
                                        runtime_roots=runtime.roots_text, in_container=container)
    observed = []
    for network in ("none", "host"):
        result = _run_probe(candidate, runtime, hidden, network=network)
        if isinstance(result, str):
            return _unavailable(result, in_container=container)
        observed.append(result)
    unshared, shared = observed
    expected = {"visible": [], "nested_userns": "denied", "pid1": "bwrap", "rw": "ok", "ro": errno.EROFS,
                "denied": errno.EACCES, "level": errno.EROFS}
    for result in observed:
        if any(result.get(name) != value for name, value in expected.items()):
            return _unavailable("probe_mismatch", in_container=container)
    if unshared.get("interfaces") != ["lo"]:
        return _unavailable("probe_mismatch", in_container=container)
    posture = ProcessIsolationPosture(**{**candidate.__dict__, "probe_sha256": _digest(
        {"source": PROBE_SOURCE, "guard": GUARD_SOURCE, "flags": REQUIRED_BWRAP_FLAGS,
         "observed": [{**item, "interfaces": None} for item in observed]})})
    if use_cache:
        with _cache_lock:
            _cache[key] = posture
    return posture


def _run_probe(candidate: ProcessIsolationPosture, runtime: IsolationRuntime, hidden: Sequence[str], *,
               network: Literal["none", "host"]) -> dict | str:
    with tempfile.TemporaryDirectory(prefix="seraph-process-probe-") as scratch:
        root = Path(scratch).resolve() / "view"
        (root / "rw").mkdir(parents=True)
        (root / "ro.txt").write_text("ro\n")
        (root / "denied.txt").write_text("denied\n")
        protected = [("ro.txt", READ_ONLY), ("denied.txt", DENIED), ("absent.json", DENIED)]
        accesses = dict(protected)
        try:
            view = build_workspace_view(root, protected, lambda logical: accesses.get(logical, READ_WRITE))
        except ProcessIsolationBlocked:
            return "probe_failed"
        checks = {"hidden": list(hidden), "rw": str(root / "rw" / "probe.txt"), "ro": str(root / "ro.txt"),
                  "denied": str(root / "denied.txt"), "level": str(root / "created.txt")}
        try:
            launch = sandbox_launch([str(runtime.interpreter), "-I", "-S", "-c", PROBE_SOURCE, json.dumps(checks)],
                                    posture=candidate, runtime=runtime, view=view, cwd=root, network=network)
        except (OSError, ValueError):
            view.close()
            return "probe_failed"
        try:
            process = subprocess.Popen(launch.argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, pass_fds=launch.pass_fds, close_fds=True,
                                       env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, start_new_session=True)
        except OSError:
            launch.close()
            return "probe_failed"
        launch.close_child_fds()
        try:
            started = await_startup(process, launch.status_read_fd, deadline=time.monotonic() + PROBE_TIMEOUT_SECONDS)
            stdout, stderr = process.communicate(timeout=PROBE_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            return "probe_failed"
        finally:
            launch.close()
        if started is not None:
            if started.startswith("workspace_view_mismatch"):
                return "probe_mismatch"
            return classify_startup_failure(stderr.decode(errors="replace"), container=candidate.in_container)
        if process.returncode != 0:
            return "probe_failed"
        try:
            return json.loads(stdout.decode())
        except (UnicodeDecodeError, ValueError):
            return "probe_failed"


def clear_probe_cache() -> None:
    with _cache_lock:
        _cache.clear()
    _help_cache.clear()


def host_probe_inputs(workspace_dir: str | os.PathLike) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """(hidden home secrets, forbidden ancestors, forbidden trees) for this host."""
    ancestors, trees = host_forbidden_roots(workspace_dir)
    return host_secret_paths(None), ancestors, trees


def approval_lines(network: Literal["none", "host"], view_counts: Mapping[str, int] | None = None) -> list[str]:
    """Operator-visible approval text for a sandboxed process launch."""
    counts = view_counts or {}
    lines = ["Runs in an OS sandbox (bubblewrap, all capabilities dropped).",
             ("Runtime-owned workspace state is unreadable"
              + (f" ({counts['denied']})" if "denied" in counts else "")
              + " or read-only" + (f" ({counts['read_only']})" if "read_only" in counts else "") + "."),
             READ_ONLY_LEVEL_NOTE, NOT_VISIBLE_NOTE, STALE_INODE_NOTE]
    if network == "host":
        lines.append(LOCALHOST_NOTE)
    else:
        lines.append("Network: loopback only inside the sandbox.")
    return lines
