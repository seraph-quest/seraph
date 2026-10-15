"""OS-level isolation for repository test/build commands (#1065).

``probe`` decides, by running a fixed check inside the sandbox, whether this
host can actually provide isolation. ``wrap`` turns one command argv into a
bubblewrap + prlimit argv. There is no silent fallback: an unsupported host
yields an explicit ``not_isolated`` posture with a reason, and ``wrap``
refuses to produce an argv for it.

The sandbox:
- unshares every namespace (network: loopback only; pid; ipc; uts; user);
- mounts read-only ``/usr`` (merged-usr symlinks), ``/etc`` and the declared
  runtime prefixes, a size-bounded private ``/tmp``, and only the declared
  stage directories; nothing else from ``$HOME`` or the Seraph workspace;
- clears the environment and sets only declared variables;
- dies with its parent, and every process inside dies with the pid namespace;
- applies RLIMIT_CPU/AS/FSIZE outside (they are inherited) and RLIMIT_NPROC
  inside the new user namespace, where it counts only the sandbox's processes.

Unprivileged ``unshare`` is not used: Ubuntu's AppArmor userns restriction
denies it, while the distro's bubblewrap profile is allowed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from typing import Literal, Mapping, Sequence

PROBE_TIMEOUT_SECONDS = 10
_ENV_NAME_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_")
NotIsolatedReason = Literal["bwrap_missing", "prlimit_missing", "userns_denied", "probe_failed",
                            "probe_mismatch"]


class IsolationUnavailable(RuntimeError):
    """Raised instead of ever running a command without the claimed isolation."""


@dataclass(frozen=True)
class IsolationLimits:
    cpu_seconds: int = 60
    address_space_bytes: int = 4 * 1024 ** 3
    file_size_bytes: int = 64 * 1024 ** 2
    max_processes: int = 256
    tmp_bytes: int = 256 * 1024 ** 2

    def __post_init__(self):
        for name in ("cpu_seconds", "address_space_bytes", "file_size_bytes", "max_processes", "tmp_bytes"):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")

    @property
    def digest(self) -> str:
        return _digest(self.__dict__)


@dataclass(frozen=True)
class IsolationRuntime:
    """Executables the command needs. ``interpreter`` runs the probe."""
    interpreter: Path
    read_only_paths: tuple[Path, ...] = ()
    identity: str = ""

    @classmethod
    def for_python(cls, executable: str | os.PathLike = sys.executable) -> "IsolationRuntime":
        """The current interpreter, its venv and base prefixes (read-only)."""
        executable = Path(executable)
        paths = {Path(sys.prefix), Path(sys.base_prefix), executable.parent, executable.resolve().parent}
        identity = _digest({"interpreter": str(executable), "paths": sorted(map(str, paths))})
        return cls(executable, tuple(sorted(paths)), identity)


@dataclass(frozen=True)
class StagePaths:
    """Only these host paths become visible, at their own absolute paths."""
    writable: tuple[Path, ...] = ()
    read_only: tuple[Path, ...] = ()
    cwd: Path | None = None


@dataclass(frozen=True)
class IsolationPosture:
    variant: Literal["isolated", "not_isolated"]
    isolation_claim: str
    network_isolation: str
    resource_enforcement: str
    host_access: str
    reason: NotIsolatedReason | None = None
    bwrap_path: str | None = None
    bwrap_sha256: str | None = None
    prlimit_path: str | None = None
    limits_digest: str = ""
    probe_sha256: str | None = None

    @property
    def isolated(self) -> bool:
        return self.variant == "isolated"

    def payload(self) -> dict:
        """Closed operator-visible projection (paths stay server-side)."""
        value = {"variant": self.variant, "isolation_claim": self.isolation_claim,
                 "network_isolation": self.network_isolation, "resource_enforcement": self.resource_enforcement,
                 "host_access": self.host_access, "limits_digest": self.limits_digest}
        if self.isolated:
            value.update(bwrap_sha256=self.bwrap_sha256, isolation_probe_sha256=self.probe_sha256)
        else:
            value["isolation_unavailable_reason"] = self.reason
        return value


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     default=str).encode()).hexdigest()


def _not_isolated(reason: NotIsolatedReason, limits: IsolationLimits) -> IsolationPosture:
    return IsolationPosture("not_isolated", "none", "not_isolated", "admission_and_wall_timeout_only",
                            "operator_user_full_access", reason=reason, limits_digest=limits.digest)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _absolute(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("isolation paths must be absolute")
    return Path(os.path.normpath(path))


def _under_usr(path: Path) -> bool:
    return path == Path("/usr") or Path("/usr") in path.parents


def wrap(argv: Sequence[str], stage: StagePaths, runtime: IsolationRuntime, limits: IsolationLimits,
         posture: IsolationPosture, *, env: Mapping[str, str] | None = None) -> list[str]:
    """Return the isolated argv for one command, or refuse."""
    if not isinstance(posture, IsolationPosture) or not posture.isolated:
        raise IsolationUnavailable("isolation posture is not isolated; refusing to wrap")
    if posture.limits_digest != limits.digest:
        raise IsolationUnavailable("isolation limits changed since the probe")
    if not argv or any(type(item) is not str or "\x00" in item for item in argv):
        raise ValueError("argv must be non-empty strings")
    variables = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp", "TMPDIR": "/tmp",
                 "LANG": "C.UTF-8", "TZ": "UTC", **(env or {})}
    for name, value in variables.items():
        if (type(name) is not str or not name or not set(name) <= _ENV_NAME_CHARS
                or type(value) is not str or "\x00" in value):
            raise ValueError("invalid sandbox environment")
    command = [posture.prlimit_path, f"--cpu={limits.cpu_seconds}:{limits.cpu_seconds}",
               f"--as={limits.address_space_bytes}:{limits.address_space_bytes}",
               f"--fsize={limits.file_size_bytes}:{limits.file_size_bytes}", "--",
               posture.bwrap_path, "--unshare-all", "--die-with-parent", "--new-session", "--clearenv"]
    for name, value in sorted(variables.items()):
        command += ["--setenv", name, value]
    command += ["--ro-bind", "/usr", "/usr"]
    for name in ("bin", "sbin", "lib", "lib64", "lib32"):
        if Path("/" + name).is_symlink() and os.readlink("/" + name).lstrip("/").startswith("usr/"):
            command += ["--symlink", os.readlink("/" + name).lstrip("/"), "/" + name]
    command += ["--ro-bind", "/etc", "/etc", "--proc", "/proc", "--dev", "/dev",
                "--size", str(limits.tmp_bytes), "--tmpfs", "/tmp"]
    seen: set[Path] = set()
    read_only = [_absolute(path) for path in (runtime.interpreter.parent, *runtime.read_only_paths, *stage.read_only)]
    # Parents before children so nested binds stay visible.
    for path in sorted(set(read_only), key=lambda item: len(item.parts)):
        if _under_usr(path) or path in seen:
            continue
        seen.add(path)
        command += ["--ro-bind", str(path), str(path)]
    for path in sorted({_absolute(path) for path in stage.writable}, key=lambda item: len(item.parts)):
        if _under_usr(path):
            raise ValueError("writable stage paths cannot be under /usr")
        command += ["--bind", str(path), str(path)]
    if stage.cwd is not None:
        command += ["--chdir", str(_absolute(stage.cwd))]
    # RLIMIT_NPROC inside the new user namespace counts only sandbox processes.
    command += ["--", "/usr/bin/prlimit", f"--nproc={limits.max_processes}:{limits.max_processes}", "--", *argv]
    return command


_PROBE_SOURCE = r"""
import json, os, resource, socket, sys
checks = json.loads(sys.argv[1])
interfaces = sorted(name for _, name in socket.if_nameindex())
visible = [path for path in checks["hidden"] if os.path.lexists(path)]
limits = {name: list(resource.getrlimit(getattr(resource, name)))
          for name in ("RLIMIT_CPU", "RLIMIT_AS", "RLIMIT_FSIZE", "RLIMIT_NPROC")}
print(json.dumps({"interfaces": interfaces, "visible": visible, "limits": limits, "uid": os.getuid()}))
"""

_cache: dict[tuple, IsolationPosture] = {}
_cache_lock = threading.Lock()


def probe(runtime: IsolationRuntime, limits: IsolationLimits, *, hidden_paths: Sequence[str | os.PathLike] = (),
          use_cache: bool = True) -> IsolationPosture:
    """Verify, inside an actual sandbox, that isolation holds on this host.

    ``hidden_paths`` (e.g. ``~/.ssh``, the Seraph database and vault) must not
    exist inside. No packet is sent: the network check only lists interfaces.
    """
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        return _not_isolated("bwrap_missing", limits)
    prlimit = shutil.which("prlimit")
    if prlimit is None or not Path("/usr/bin/prlimit").exists():
        return _not_isolated("prlimit_missing", limits)
    bwrap_path = str(Path(bwrap).resolve())
    hidden = sorted(str(_absolute(Path(path))) for path in hidden_paths)
    key = (_file_sha256(Path(bwrap_path)), runtime.identity, str(runtime.interpreter), limits.digest,
           tuple(hidden), str(Path(prlimit).resolve()))
    if use_cache:
        with _cache_lock:
            if key in _cache:
                return _cache[key]
    candidate = IsolationPosture("isolated", "bwrap_userns", "netns_loopback_only_verified",
                                 "rlimit_cpu_as_fsize_nproc_and_wall_timeout", "snapshot_bind_only",
                                 bwrap_path=bwrap_path, bwrap_sha256=key[0], prlimit_path=str(Path(prlimit).resolve()),
                                 limits_digest=limits.digest)
    argv = wrap([str(runtime.interpreter), "-I", "-S", "-c", _PROBE_SOURCE, json.dumps({"hidden": hidden})],
                StagePaths(), runtime, limits, candidate)
    try:
        completed = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                                   timeout=PROBE_TIMEOUT_SECONDS, env={"PATH": "/usr/bin:/bin"}, close_fds=True)
    except (OSError, subprocess.TimeoutExpired):
        return _not_isolated("probe_failed", limits)
    if completed.returncode != 0:
        denied = b"Operation not permitted" in completed.stderr or b"setting up uid map" in completed.stderr
        return _not_isolated("userns_denied" if denied else "probe_failed", limits)
    try:
        observed = json.loads(completed.stdout.decode())
    except (UnicodeDecodeError, ValueError):
        return _not_isolated("probe_failed", limits)
    expected_limits = {"RLIMIT_CPU": [limits.cpu_seconds] * 2, "RLIMIT_AS": [limits.address_space_bytes] * 2,
                       "RLIMIT_FSIZE": [limits.file_size_bytes] * 2, "RLIMIT_NPROC": [limits.max_processes] * 2}
    if observed.get("interfaces") != ["lo"] or observed.get("visible") != [] or observed.get("limits") != expected_limits:
        return _not_isolated("probe_mismatch", limits)
    posture = IsolationPosture(**{**candidate.__dict__, "probe_sha256": _digest({"source": _PROBE_SOURCE,
                                                                                    "observed": observed})})
    if use_cache:
        with _cache_lock:
            _cache[key] = posture
    return posture


def clear_probe_cache() -> None:
    with _cache_lock:
        _cache.clear()
