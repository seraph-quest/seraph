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
import tempfile
import threading
from typing import Literal, Mapping, Sequence

PROBE_TIMEOUT_SECONDS = 10
# repo_worker.verify_exported_diff's private mkdtemp prefix.
VERIFIER_SCRATCH_PREFIX = "seraph-diff-verify-"
_ENV_NAME_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_")
NotIsolatedReason = Literal["bwrap_missing", "prlimit_missing", "userns_denied", "probe_failed",
                            "probe_mismatch", "runtime_root_unsafe"]
NOT_ISOLATED_REASONS = frozenset({"bwrap_missing", "prlimit_missing", "userns_denied", "probe_failed",
                                  "probe_mismatch", "runtime_root_unsafe"})


def _executable_paths(item: str | os.PathLike) -> set[Path]:
    """Every hop of the executable's symlink chain plus the resolved file.

    Symlink hops are recreated as symlinks inside the sandbox (``wrap``), so
    ``$ORIGIN``-relative library lookups still resolve against the real file.
    """
    paths: set[Path] = set()
    current = Path(os.path.normpath(Path(item).absolute()))
    for _ in range(40):
        paths.add(current)
        if not current.is_symlink():
            break
        current = Path(os.path.normpath(current.parent / os.readlink(current)))
    paths.add(Path(item).resolve())
    return paths


def _prefix_library_paths(prefix: Path, *, node: bool = False) -> set[Path]:
    """Only the library trees an interpreter/runtime prefix needs.

    Python: ``lib``/``lib64`` (stdlib or venv site-packages) and
    ``pyvenv.cfg``. Node: only ``lib/node_modules/npm`` (or nothing). Never the
    prefix itself, so a prefix such as ``~/.local`` exposes no sibling data.
    """
    paths: set[Path] = set()
    npm = prefix / "lib" / "node_modules" / "npm"
    if node:
        if npm.is_dir():
            paths.add(npm)
        return paths
    if (prefix / "pyvenv.cfg").is_file():
        paths.add(prefix / "pyvenv.cfg")
    for name in ("lib", "lib64"):
        library = prefix / name
        if library.is_dir() and not library.is_symlink() and any(library.glob("python3*")):
            paths.add(library)
    return paths


def runtime_root_problem(paths: Sequence[Path], *, forbidden_ancestors: Sequence[str | os.PathLike],
                         forbidden_trees: Sequence[str | os.PathLike]) -> str | None:
    """A bound runtime path must not be/contain $HOME, credentials or the workspace."""
    ancestors = [Path(os.path.normpath(path)) for path in forbidden_ancestors]
    trees = [Path(os.path.normpath(path)) for path in forbidden_trees]
    for path in map(Path, paths):
        if any(path == item or path in item.parents for item in ancestors):
            return str(path)
        if any(path == tree or tree in path.parents for tree in trees):
            return str(path)
    return None


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
        """The interpreter file plus only its prefixes' library trees (read-only)."""
        executable = Path(executable)
        paths = set(_executable_paths(executable))
        for prefix in {Path(sys.prefix), Path(sys.base_prefix)}:
            paths.update(_prefix_library_paths(prefix))
        paths = {path for path in paths if not _under_usr(path)}
        identity = _digest({"interpreter": str(executable), "paths": sorted(map(str, paths))})
        return cls(executable, tuple(sorted(paths)), identity)

    @property
    def roots_text(self) -> str:
        """Operator-visible list of everything bound from the host runtime."""
        return ";".join(sorted(map(str, self.read_only_paths)))


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
    runtime_roots: str = ""

    @property
    def isolated(self) -> bool:
        return self.variant == "isolated"

    def payload(self) -> dict:
        """Closed operator-visible projection (paths stay server-side)."""
        value = {"variant": self.variant, "isolation_claim": self.isolation_claim,
                 "network_isolation": self.network_isolation, "resource_enforcement": self.resource_enforcement,
                 "host_access": self.host_access, "limits_digest": self.limits_digest}
        if self.isolated:
            value.update(bwrap_sha256=self.bwrap_sha256, isolation_probe_sha256=self.probe_sha256,
                         runtime_roots_sha256=hashlib.sha256(self.runtime_roots.encode()).hexdigest())
        else:
            value["isolation_unavailable_reason"] = self.reason
        return value


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     default=str).encode()).hexdigest()


def _not_isolated(reason: NotIsolatedReason, limits: IsolationLimits) -> IsolationPosture:
    # The values the local executor has always declared; only the reason is new.
    return IsolationPosture("not_isolated", "none", "not_verified", "admission_and_wall_timeout_only",
                            "explicit_job_approval_required", reason=reason, limits_digest=limits.digest)


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
    if _disable_userns_supported(posture.bwrap_path):
        # No nested user namespaces: removes a large kernel attack surface.
        command += ["--unshare-user", "--disable-userns", "--assert-userns-disabled"]
    for name, value in sorted(variables.items()):
        command += ["--setenv", name, value]
    command += ["--ro-bind", "/usr", "/usr"]
    for name in ("bin", "sbin", "lib", "lib64", "lib32"):
        if Path("/" + name).is_symlink() and os.readlink("/" + name).lstrip("/").startswith("usr/"):
            command += ["--symlink", os.readlink("/" + name).lstrip("/"), "/" + name]
    command += ["--ro-bind", "/etc", "/etc", "--proc", "/proc", "--dev", "/dev",
                "--size", str(limits.tmp_bytes), "--tmpfs", "/tmp"]
    seen: set[Path] = set()
    read_only = [_absolute(path) for path in (*runtime.read_only_paths, *stage.read_only)]
    # Parents before children so nested binds stay visible.
    for path in sorted(set(read_only), key=lambda item: len(item.parts)):
        if _under_usr(path) or path in seen:
            continue
        seen.add(path)
        if path.is_symlink():
            # Recreate the link itself; its target is bound separately.
            command += ["--symlink", os.readlink(path), str(path)]
        else:
            command += ["--ro-bind", str(path), str(path)]
    for path in sorted({_absolute(path) for path in stage.writable}, key=lambda item: len(item.parts)):
        if _under_usr(path):
            raise ValueError("writable stage paths cannot be under /usr")
        command += ["--bind", str(path), str(path)]
    # Everything not explicitly bound writable (the root tmpfs and its skeleton
    # directories) becomes read-only, so nothing outside the stage binds and
    # the size-bounded /tmp can grow in memory.
    command += ["--remount-ro", "/"]
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
try:
    os.unshare(os.CLONE_NEWUSER)
    nested = "allowed"
except (OSError, AttributeError):
    nested = "denied"
print(json.dumps({"interfaces": interfaces, "visible": visible, "limits": limits, "uid": os.getuid(),
                  "nested_userns": nested}))
"""

_cache: dict[tuple, IsolationPosture] = {}
_userns_flag_cache: dict[str, bool] = {}


def _disable_userns_supported(bwrap_path: str) -> bool:
    """bubblewrap >= 0.8 can forbid nested user namespaces."""
    if bwrap_path not in _userns_flag_cache:
        try:
            completed = subprocess.run([bwrap_path, "--help"], capture_output=True, timeout=5,
                                       env={"PATH": "/usr/bin:/bin"})
            _userns_flag_cache[bwrap_path] = b"--disable-userns" in completed.stdout + completed.stderr
        except (OSError, subprocess.SubprocessError):
            _userns_flag_cache[bwrap_path] = False
    return _userns_flag_cache[bwrap_path]
_cache_lock = threading.Lock()


def probe(runtime: IsolationRuntime, limits: IsolationLimits, *, hidden_paths: Sequence[str | os.PathLike] = (),
          forbidden_ancestors: Sequence[str | os.PathLike] = (), forbidden_trees: Sequence[str | os.PathLike] = (),
          use_cache: bool = True) -> IsolationPosture:
    """Verify, inside an actual sandbox, that isolation holds on this host.

    ``hidden_paths`` (e.g. ``~/.ssh``, the Seraph database and vault) must not
    exist inside. A runtime path that is or contains a ``forbidden_ancestors``
    entry ($HOME, ~/.local, ~/.config, the workspace) or lies inside a
    ``forbidden_trees`` entry (credential directories) is refused, because it
    would expose more than the snapshot. No packet is sent.
    """
    if runtime_root_problem(runtime.read_only_paths, forbidden_ancestors=forbidden_ancestors,
                            forbidden_trees=forbidden_trees):
        return _not_isolated("runtime_root_unsafe", limits)
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        return _not_isolated("bwrap_missing", limits)
    prlimit = shutil.which("prlimit")
    if prlimit is None or not Path("/usr/bin/prlimit").exists():
        return _not_isolated("prlimit_missing", limits)
    bwrap_path = str(Path(bwrap).resolve())
    hidden = sorted(str(_absolute(Path(path))) for path in hidden_paths)
    key = (_file_sha256(Path(bwrap_path)), runtime.identity, str(runtime.interpreter), limits.digest,
           tuple(hidden), str(Path(prlimit).resolve()), _disable_userns_supported(bwrap_path))
    if use_cache:
        with _cache_lock:
            if key in _cache:
                return _cache[key]
    candidate = IsolationPosture("isolated", "bwrap_userns", "netns_loopback_only_verified",
                                 "rlimit_cpu_as_fsize_nproc_and_wall_timeout", "snapshot_bind_only",
                                 bwrap_path=bwrap_path, bwrap_sha256=key[0], prlimit_path=str(Path(prlimit).resolve()),
                                 limits_digest=limits.digest, runtime_roots=runtime.roots_text)
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
    if (observed.get("interfaces") != ["lo"] or observed.get("visible") != [] or observed.get("limits") != expected_limits
            or (key[-1] and observed.get("nested_userns") != "denied")):
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


# ---------------------------------------------------------------- integration

ADDRESS_SPACE_BYTES = 8 * 1024 ** 3
HOME_SECRETS = (".ssh", ".gnupg", ".aws", ".config/gh", ".netrc", ".git-credentials", ".docker/config.json")
WORKSPACE_SECRETS = ("seraph.db", "seraph.db-wal", "seraph.db-shm", ".vault-key", "google_calendar_token.json")


def host_secret_paths(workspace_dir: str | os.PathLike | None = None) -> tuple[str, ...]:
    """Leaf secrets that must be absent inside the sandbox.

    Leaves, never ancestors: a bound stage under the workspace makes its
    ancestors exist as empty skeleton directories inside the sandbox.
    """
    paths = [Path.home() / name for name in HOME_SECRETS]
    if workspace_dir is not None:
        from src.workspace.state_registry import canonical_workspace_database_path
        root = Path(workspace_dir).expanduser().absolute()
        database = Path(canonical_workspace_database_path(str(root)))
        paths += [root / name for name in WORKSPACE_SECRETS]
        paths += [database, Path(str(database) + "-wal"), Path(str(database) + "-shm")]
    return tuple(sorted({str(Path(os.path.normpath(path))) for path in paths}))


def host_forbidden_roots(workspace_dir: str | os.PathLike | None = None) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(forbidden ancestors, forbidden trees) for runtime bindings on this host."""
    home = Path.home()
    ancestors = [home, home / ".local", home / ".config", home / ".ssh", home / ".gnupg"]
    if workspace_dir is not None:
        ancestors.append(Path(workspace_dir).expanduser().absolute())
    trees = [home / ".ssh", home / ".gnupg", home / ".config", home / ".aws", home / ".docker"]
    if workspace_dir is not None:
        trees.append(Path(workspace_dir).expanduser().absolute())
    return (tuple(str(Path(os.path.normpath(path))) for path in ancestors),
            tuple(str(Path(os.path.normpath(path))) for path in trees))


def runtime_for(files: Sequence[str | os.PathLike], *, interpreter: str | os.PathLike = sys.executable) -> IsolationRuntime:
    """Read-only host paths for the executables a command needs.

    Each executable file (as configured and resolved) plus only its install
    prefix's library tree (Python ``lib``/``pyvenv.cfg``, Node's npm). Parent
    directories such as ``~/.local`` or ``$HOME`` are never bound wholesale.
    """
    base = IsolationRuntime.for_python(interpreter)
    paths: set[Path] = set(base.read_only_paths)
    for item in files:
        paths.update(_executable_paths(item))
        node = Path(item).resolve().name.startswith("node")
        for candidate in (Path(item).absolute(), Path(item).resolve()):
            prefix = candidate.parent.parent if candidate.parent.name == "bin" else candidate.parent
            paths.update(_prefix_library_paths(prefix, node=node))
    paths = {Path(os.path.normpath(path)) for path in paths if not _under_usr(path)}
    identity = _digest({"interpreter": str(interpreter), "paths": sorted(map(str, paths))})
    return IsolationRuntime(Path(interpreter), tuple(sorted(paths)), identity)


def limits_for(config) -> IsolationLimits:
    """Map the operator's repository sandbox settings onto rlimits."""
    # RLIMIT_AS bounds virtual reservations, not resident memory: V8 and
    # glibc arenas reserve far more than they touch, so it is a fixed ceiling.
    return IsolationLimits(cpu_seconds=int(config.max_cpu_seconds),
                           address_space_bytes=ADDRESS_SPACE_BYTES,
                           file_size_bytes=max(int(config.max_snapshot_bytes), int(config.max_file_bytes)),
                           max_processes=int(config.max_pids), tmp_bytes=int(config.max_snapshot_bytes))


def executor_posture_fields(posture: IsolationPosture) -> dict:
    """Flat executor-posture fields (durable authority stores postures flat)."""
    fields = {"isolation_claim": posture.isolation_claim, "network_isolation": posture.network_isolation,
              "resource_enforcement": posture.resource_enforcement, "host_access": posture.host_access,
              "isolation_variant": posture.variant}
    if posture.isolated:
        fields.update(isolation_bwrap_sha256=posture.bwrap_sha256, isolation_probe_sha256=posture.probe_sha256,
                      isolation_runtime_roots=posture.runtime_roots)
    else:
        fields["isolation_unavailable_reason"] = posture.reason
    return fields


ISOLATION_FIELDS = frozenset({"isolation_variant", "isolation_bwrap_sha256", "isolation_probe_sha256",
                              "isolation_runtime_roots", "isolation_unavailable_reason"})


def job_spec(posture: IsolationPosture, runtime: IsolationRuntime, limits: IsolationLimits,
             hidden_paths: Sequence[str], forbidden_ancestors: Sequence[str] = (),
             forbidden_trees: Sequence[str] = ()) -> dict:
    """JSON handoff to the supervisor, which re-probes and must match exactly."""
    return {"payload": posture.payload(), "interpreter": str(runtime.interpreter),
            "read_only_paths": [str(path) for path in runtime.read_only_paths], "identity": runtime.identity,
            "limits": dict(limits.__dict__), "hidden_paths": list(hidden_paths),
            "forbidden_ancestors": list(forbidden_ancestors), "forbidden_trees": list(forbidden_trees)}


@dataclass(frozen=True)
class CommandIsolation:
    posture: IsolationPosture
    runtime: IsolationRuntime
    limits: IsolationLimits
    stage_root: Path
    extra_writable: tuple[Path, ...] = ()
    hidden_paths: tuple[str, ...] = ()

    def command(self, argv: Sequence[str], cwd: Path, env: Mapping[str, str]) -> tuple[list[str], dict]:
        """(argv, Popen env) for one repository command; unchanged when not isolated.

        Visible: the stage (read-only), the command's cwd and the stage's
        home/tmp (writable), and any regular file named by an absolute argv
        entry (read-only, e.g. the trusted verifier's scratch patch file).
        """
        if not self.posture.isolated:
            return list(argv), dict(env)
        writable = (Path(cwd), *(path for path in self.extra_writable if path.exists()))
        named = []
        scratch_root = Path(tempfile.gettempdir()).resolve()
        for item in argv[1:]:
            path = Path(item)
            if not path.is_absolute() or path.is_symlink() or not path.is_file() or _under_usr(path):
                continue
            path = Path(os.path.normpath(path))
            if any(path == Path(hidden) or Path(hidden) in path.parents for hidden in self.hidden_paths):
                raise IsolationUnavailable("a repository command named a hidden host secret")
            # Only the trusted diff verifier's private scratch file is bound;
            # any other absolute path stays invisible inside the sandbox.
            if (self.stage_root not in path.parents and path.parent.parent == scratch_root
                    and path.parent.name.startswith(VERIFIER_SCRATCH_PREFIX)):
                named.append(path)
        stage = StagePaths(writable=writable, read_only=(self.stage_root, *named), cwd=Path(cwd))
        return (wrap(argv, stage, self.runtime, self.limits, self.posture, env=dict(env)),
                {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})


def activate(spec: Mapping, *, stage_root: Path, extra_writable: Sequence[Path] = ()) -> CommandIsolation:
    """Supervisor side: re-probe now and require the exact approved posture."""
    runtime = IsolationRuntime(Path(spec["interpreter"]), tuple(Path(path) for path in spec["read_only_paths"]),
                               str(spec["identity"]))
    limits = IsolationLimits(**spec["limits"])
    current = probe(runtime, limits, hidden_paths=spec["hidden_paths"],
                    forbidden_ancestors=spec.get("forbidden_ancestors", ()), forbidden_trees=spec.get("forbidden_trees", ()))
    if current.payload() != spec["payload"]:
        raise IsolationUnavailable("isolation_posture_changed")
    return CommandIsolation(current, runtime, limits, Path(stage_root), tuple(Path(path) for path in extra_writable),
                            tuple(spec["hidden_paths"]))


def approval_summary_prefix(posture_fields: Mapping) -> str:
    """"NOT ISOLATED (<reason>): " for any posture that is not the isolated variant."""
    fields = posture_fields if isinstance(posture_fields, Mapping) else {}
    if fields.get("isolation_variant") == "isolated":
        return ""
    return f"NOT ISOLATED ({fields.get('isolation_unavailable_reason') or 'isolation_not_probed'}): "


def valid_posture_fields(posture: Mapping) -> bool:
    """Closed check of the flat isolation fields of a persisted executor posture."""
    import re
    variant = posture.get("isolation_variant")
    present = ISOLATION_FIELDS & set(posture)
    if variant == "isolated":
        expected = {"isolation_variant", "isolation_bwrap_sha256", "isolation_probe_sha256", "isolation_runtime_roots"}
        flat = ("bwrap_userns", "netns_loopback_only_verified", "rlimit_cpu_as_fsize_nproc_and_wall_timeout",
                "snapshot_bind_only")
        return (present == expected and all(isinstance(posture[key], str)
                and re.fullmatch(r"[0-9a-f]{64}", posture[key])
                for key in expected - {"isolation_variant", "isolation_runtime_roots"})
                and isinstance(posture["isolation_runtime_roots"], str)
                and all(part.startswith("/") for part in posture["isolation_runtime_roots"].split(";") if part)
                and tuple(posture.get(key) for key in ("isolation_claim", "network_isolation",
                    "resource_enforcement", "host_access")) == flat)
    if variant == "not_isolated":
        return (present == {"isolation_variant", "isolation_unavailable_reason"}
                and posture["isolation_unavailable_reason"] in NOT_ISOLATED_REASONS)
    return not present
