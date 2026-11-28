"""The passive provider-worker entry, ``python -m src.runtime_plugins.worker`` (#1207, #1026 §7 phase 1).

A supervising parent (the ``python-provider-host`` plugin from #1208/#1210;
tests until then) spawns this process with ``worker_launch``: a fixed
absolute interpreter, ``-I -B``, a closed argv and a closed environment. The
worker then serves one length-framed stdio channel. It is passive: it answers
the parent's requests and never asks the parent for anything.

Order, which the design review fixes (items 2, 3, 4 and 7 on #1026):

1. ``prctl(PR_SET_DUMPABLE, 0)`` on Linux, before anything else and therefore
   before any secret is loaded. ``/proc/<pid>/environ``, ``mem`` and ptrace
   then refuse every non-root peer. A failure exits ``hardening_unavailable``.
2. The invocation is checked: interpreter flags, the exact argv, the closed
   environment and pipe-backed stdio. Any deviation exits
   ``invocation_refused`` before a setting, lock or database is touched.
3. Parent loss is armed: ``PR_SET_PDEATHSIG`` (``SIGHUP``) on Linux, then a
   check that the parent is still the one the environment names; on macOS the
   check, then a kqueue ``NOTE_EXIT`` watch. stdin EOF and a broken stdout
   are the portable signals. The parent must spawn from a long-lived thread:
   Linux sends PDEATHSIG when the forking thread exits.
4. The frame pipes move off fds 0 and 1 to private non-inheritable
   descriptors, so a later grandchild can neither inject frames nor keep the
   parent-loss pipe open.
5. Only now are settings (and with them the env file) loaded. The composition
   profile is validated, the worker takes the workspace owner lock and
   activates the mandatory providers first (``ProviderWorker``, #1024).
6. Frames are served until ``worker.shutdown``, a signal or parent loss, then
   the #1024 shutdown order runs and the lock is released last.

Every exit writes one JSON line to stderr (``seraph_worker_exit``) with the
exit name, code and a typed reason. It carries names and states only, never
a value from the environment, a setting or a secret.

Importing this module loads no setting and creates nothing; only ``main``
does work.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import signal
import stat
import struct
import sys
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Mapping

from .protocol import HEX64, MAX_FRAME, MAX_PENDING, TOKEN, ProtocolError, closed, decode_json, integer

BACKEND_ROOT = Path(__file__).resolve().parents[2]
WORKER_MODULE = "src.runtime_plugins.worker"
# The fixed ``-c`` program. ``-I`` implies safe-path (``-P``), so ``-m`` cannot
# find an uninstalled backend; this runs the same module as ``__main__``
# through ``runpy``, exactly as ``-m`` does, with the backend root (the only
# variable, ``argv[1]``) on ``sys.path``.
WORKER_BOOTSTRAP = ("import runpy,sys\n"
                    "sys.path.insert(0,sys.argv[1])\n"
                    f"runpy.run_module({WORKER_MODULE!r},run_name='__main__',alter_sys=True)\n")
INTERPRETER_FLAGS = ("-I", "-B")

PROTOCOL = 1
# Reserved control capacity (design review D1 and item 7): of MAX_PENDING
# in-flight requests, ordinary requests may hold at most MAX_PENDING - 4.
CONTROL_RESERVED = 4
ORDINARY_LIMIT = MAX_PENDING - CONTROL_RESERVED
CONTROL_METHODS = frozenset({"worker.hello", "worker.heartbeat", "worker.status", "worker.shutdown"})
ORDINARY_METHODS = frozenset({"worker.readiness"})
METHODS = CONTROL_METHODS | ORDINARY_METHODS
FRAME_FIELDS = frozenset({"protocol", "boot_nonce", "request_id", "seq", "kind", "method", "payload"})
PROVIDER_STATES = frozenset({"inactive", "active", "awaiting_facade", "degraded", "failed", "maintenance",
                             "deactivated"})

PARENT_PID_ENV = "SERAPH_WORKER_PARENT_PID"
SETTINGS_ENV_FILE_ENV = "SERAPH_SETTINGS_ENV_FILE"
WORKSPACE_ENV = "WORKSPACE_DIR"
# The closed environment, classified in the source inventory's vocabulary
# (``inventory.ENV_READ_KINDS``). Never a credential and never a composition
# flag: Python reads its env file itself, so the env file is the only source
# of settings and secrets besides the vault.
WORKER_ENV: Mapping[str, tuple[str, str]] = MappingProxyType({
    PARENT_PID_ENV: ("server_process", "the supervising parent's pid, checked once parent-death is armed"),
    SETTINGS_ENV_FILE_ENV: ("path", "the env file the worker's settings read; empty loads none"),
    WORKSPACE_ENV: ("path", "the workspace root"),
    "PATH": ("platform", "a fixed system search path"),
    "LANG": ("platform", "locale"),
    "LC_ALL": ("platform", "locale"),
    "LC_CTYPE": ("platform", "locale; also set by the interpreter's C-locale coercion (PEP 538)"),
    "TZ": ("platform", "time zone"),
})
REQUIRED_ENV = frozenset({PARENT_PID_ENV, SETTINGS_ENV_FILE_ENV})
# Set by the platform itself in every process, never by the parent.
PLATFORM_ENV: Mapping[str, Mapping[str, re.Pattern[str]]] = MappingProxyType({
    "darwin": MappingProxyType({"__CF_USER_TEXT_ENCODING": re.compile(r"0x[0-9A-Fa-f]+:0x[0-9A-Fa-f]+:0x[0-9A-Fa-f]+\Z")}),
})
WORKER_PATH = "/usr/bin:/bin"
_LOCALE_VALUE = re.compile(r"[A-Za-z0-9_.@-]{1,64}\Z")
_TZ_VALUE = re.compile(r"[A-Za-z0-9_+:/-]{1,64}\Z")

# The exit-code table (review item 4). Codes follow sysexits(3) where one fits.
EXIT_CODES: Mapping[str, int] = MappingProxyType({
    "stopped": 0,                     # worker.shutdown, SIGTERM or SIGINT; orderly
    "invocation_refused": 64,         # interpreter flags, argv, env or stdio not closed
    "protocol_violation": 65,         # malformed, unknown or out-of-order frame; slots exhausted
    "composition_refused": 66,        # the composition profile or a patch layer was refused
    "hardening_unavailable": 69,      # prctl failed on Linux; never runs unhardened
    "internal_error": 70,             # an unexpected exception
    "parent_lost": 71,                # stdin EOF, broken stdout, PDEATHSIG, kqueue NOTE_EXIT, wrong parent at start
    "workspace_owner_busy": 73,       # another process holds the workspace owner lock
    "workspace_lock_unavailable": 74,  # the lock path is unsafe, unavailable or the root moved
    "hangup": 75,                     # an unrequested SIGHUP that is not PDEATHSIG; orderly, but worker loss
    "mandatory_provider_failed": 78,  # state, authority, audit, tasks or vault failed to activate
})
EXIT_EVENT = "seraph_worker_exit"
# Linux prctl options (linux/prctl.h).
_PR_SET_PDEATHSIG, _PR_GET_PDEATHSIG, _PR_GET_DUMPABLE, _PR_SET_DUMPABLE = 1, 2, 3, 4
PARENT_DEATH_SIGNAL = signal.SIGHUP
_STOP_SIGNALS = ((signal.SIGTERM, "stopped", "worker_signal_terminate"),
                 (signal.SIGINT, "stopped", "worker_signal_interrupt"))
# SIGHUP is parent death only when it is Linux's PDEATHSIG: the kernel
# reparents the worker before delivering it, so the parent no longer matches.
# Any other SIGHUP (macOS, a terminal hangup, or one sent by a process) is an
# unrequested stop: orderly shutdown, but its own non-zero exit (``hangup``).


class WorkerExit(Exception):
    """End the worker with one row of ``EXIT_CODES`` and a typed reason."""

    def __init__(self, name: str, reason_code: str, **details: Any):
        super().__init__(f"{name}: {reason_code}")
        if name not in EXIT_CODES:
            raise ValueError(name)
        self.name, self.reason_code, self.details = name, reason_code, details


# ---------------------------------------------------------------- launch (parent side)

@dataclass(frozen=True)
class WorkerLaunch:
    argv: tuple[str, ...]
    env: Mapping[str, str]
    cwd: str


def worker_launch(*, interpreter: str | os.PathLike[str], parent_pid: int, settings_env_file: str,
                  workspace_dir: str | None = None, backend_root: str | os.PathLike[str] = BACKEND_ROOT,
                  locale: str | None = None, timezone: str | None = None) -> WorkerLaunch:
    """The exact argv and closed environment a parent spawns the worker with.

    Nothing is inherited from the caller's environment. ``interpreter`` is the
    pinned absolute interpreter (never resolved through ``PATH``) and
    ``settings_env_file`` the env file the worker reads itself (``""`` loads
    none); neither a key nor a secret value is ever passed.
    """
    interpreter, root = os.fspath(interpreter), os.fspath(backend_root)
    if not os.path.isabs(interpreter) or not os.path.isfile(interpreter) or not os.access(interpreter, os.X_OK):
        raise ValueError("worker_interpreter_not_pinned")
    if not os.path.isabs(root):
        raise ValueError("worker_backend_root_invalid")
    try:
        # The worker compares argv[1] with its own resolved root, so a symlinked path is resolved here.
        root = str(Path(root).resolve(strict=True))
    except OSError as exc:
        raise ValueError("worker_backend_root_invalid") from exc
    if not (Path(root) / "src" / "runtime_plugins" / "worker.py").is_file():
        raise ValueError("worker_backend_root_invalid")
    if type(parent_pid) is not int or parent_pid <= 1:
        raise ValueError("worker_parent_pid_invalid")
    env = {PARENT_PID_ENV: str(parent_pid), SETTINGS_ENV_FILE_ENV: settings_env_file, "PATH": WORKER_PATH}
    if workspace_dir is not None:
        env[WORKSPACE_ENV] = workspace_dir
    if locale is not None:
        env["LANG"] = locale
    if timezone is not None:
        env["TZ"] = timezone
    reason = _env_problem(env, platform=None)
    if reason is not None:
        raise ValueError(reason[0])
    return WorkerLaunch(argv=(interpreter, *INTERPRETER_FLAGS, "-c", WORKER_BOOTSTRAP, root),
                        env=MappingProxyType(env), cwd=root)


def _env_problem(env: Mapping[str, str], *, platform: str | None) -> tuple[str, list[str]] | None:
    """(reason, offending names) when ``env`` is not the closed worker environment."""
    injected = PLATFORM_ENV.get(platform, {}) if platform else {}
    unknown = sorted(name for name in env if name not in WORKER_ENV and name not in injected)
    if unknown:
        return "worker_env_not_closed", unknown
    missing = sorted(REQUIRED_ENV - env.keys())
    if missing:
        return "worker_env_incomplete", missing
    bad = []
    for name, value in env.items():
        if "\0" in value:
            bad.append(name)
        elif name in injected:
            if not injected[name].fullmatch(value):
                bad.append(name)
        elif name == PARENT_PID_ENV:
            if not (value.isascii() and value.isdigit()) or int(value) <= 1:
                bad.append(name)
        elif name in {SETTINGS_ENV_FILE_ENV, WORKSPACE_ENV}:
            if (value or name == WORKSPACE_ENV) and not os.path.isabs(value):
                bad.append(name)
        elif name == "PATH":
            if value != WORKER_PATH:
                bad.append(name)
        elif name == "TZ":
            # A zone name only: never an absolute or relative zoneinfo file path.
            zone = value[1:] if value.startswith(":") else value
            if not _TZ_VALUE.fullmatch(value) or zone.startswith("/") or ".." in zone:
                bad.append(name)
        elif not _LOCALE_VALUE.fullmatch(value):
            bad.append(name)
    return ("worker_env_invalid", sorted(bad)) if bad else None


# ---------------------------------------------------------------- frames

def validate_frame(value: Any, *, kind: str) -> dict[str, Any]:
    """One closed worker frame; ``kind`` is what the reader expects to receive."""
    frame = closed(value, FRAME_FIELDS)
    integer(frame["protocol"], PROTOCOL, PROTOCOL)
    integer(frame["seq"], 1)
    if type(frame["boot_nonce"]) is not str or not HEX64.fullmatch(frame["boot_nonce"]):
        raise ProtocolError("invalid boot identity")
    if type(frame["request_id"]) is not str or not TOKEN.fullmatch(frame["request_id"]):
        raise ProtocolError("invalid request identity")
    if frame["kind"] != kind or type(frame["method"]) is not str or frame["method"] not in METHODS:
        raise ProtocolError("unknown method or kind")
    (_validate_request if kind == "request" else _validate_response)(frame["method"], frame["payload"])
    return frame


def _validate_request(method: str, payload: Any) -> None:
    if method == "worker.readiness":
        if type(closed(payload, {"probe_writes"})["probe_writes"]) is not bool:
            raise ProtocolError("invalid readiness request")
    else:
        closed(payload, set())


def _providers(value: Any) -> None:
    if type(value) is not list or len(value) > 32:
        raise ProtocolError("invalid providers")
    for item in value:
        closed(item, {"provider", "state", "reason_code"})
        if (type(item["provider"]) is not str or not TOKEN.fullmatch(item["provider"])
                or item["state"] not in PROVIDER_STATES
                or (item["reason_code"] is not None and (type(item["reason_code"]) is not str
                                                         or not TOKEN.fullmatch(item["reason_code"])))):
            raise ProtocolError("invalid provider status")


def _validate_response(method: str, payload: Any) -> None:
    if method == "worker.hello":
        closed(payload, {"state", "pid", "workspace_lock", "activation_order", "providers", "mandatory_ready",
                         "env_names", "hardening"})
        integer(payload["pid"], 2)
        order, names = payload["activation_order"], payload["env_names"]
        if (payload["state"] != "ready" or payload["workspace_lock"] != "held" or payload["mandatory_ready"] is not True
                or type(order) is not list or not all(type(item) is str and TOKEN.fullmatch(item) for item in order)
                or type(names) is not list or not all(type(item) is str and TOKEN.fullmatch(item) for item in names)):
            raise ProtocolError("invalid hello")
        _providers(payload["providers"])
        hardening = closed(payload["hardening"], {"platform", "dumpable", "parent_death_signal", "parent_watch"})
        if (type(hardening["platform"]) is not str or not TOKEN.fullmatch(hardening["platform"])
                or hardening["dumpable"] not in (None, 0)
                or (hardening["parent_death_signal"] is not None and type(hardening["parent_death_signal"]) is not int)
                or type(hardening["parent_watch"]) is not list
                or not set(hardening["parent_watch"]) <= {"pdeathsig", "kqueue", "stdin_eof"}):
            raise ProtocolError("invalid hardening report")
    elif method == "worker.heartbeat":
        if closed(payload, {"state"})["state"] != "alive":
            raise ProtocolError("invalid heartbeat")
    elif method in {"worker.status", "worker.readiness"}:
        fields = {"providers", "mandatory_ready"} | ({"workspace_lock"} if method == "worker.status" else set())
        closed(payload, fields)
        _providers(payload["providers"])
        if type(payload["mandatory_ready"]) is not bool or payload.get("workspace_lock", "held") != "held":
            raise ProtocolError("invalid status")
    else:  # worker.shutdown: answered once the lock is released
        closed(payload, {"state", "workspace_lock"})
        if payload != {"state": "stopped", "workspace_lock": "released"}:
            raise ProtocolError("invalid shutdown acknowledgement")


def encode(frame: dict[str, Any], *, kind: str) -> bytes:
    validate_frame(frame, kind=kind)
    data = json.dumps(frame, separators=(",", ":"), allow_nan=False, ensure_ascii=False).encode("utf-8")
    if not 1 <= len(data) <= MAX_FRAME:
        raise ProtocolError("frame size exceeded")
    return struct.pack(">I", len(data)) + data


async def read_frame(reader: asyncio.StreamReader, *, kind: str) -> dict[str, Any] | None:
    """The next frame, or ``None`` when the pipe closed (at or inside a frame)."""
    try:
        header = await reader.readexactly(4)
        length = struct.unpack(">I", header)[0]
        if not 1 <= length <= MAX_FRAME:
            raise ProtocolError("frame size exceeded")
        data = await reader.readexactly(length)
    except asyncio.IncompleteReadError:
        return None
    return validate_frame(decode_json(data), kind=kind)


class SlotAdmission:
    """In-flight request slots with ``CONTROL_RESERVED`` kept for control frames.

    A parent that exceeds either bound broke the contract (its own admission
    must keep count, as the host bridge does), so it is a protocol violation.
    """

    def __init__(self, total: int = MAX_PENDING, reserved: int = CONTROL_RESERVED):
        self.total, self.reserved = total, reserved
        self.control = self.ordinary = 0

    def admit(self, *, control: bool) -> None:
        if self.control + self.ordinary >= self.total:
            raise ProtocolError("pending slots exhausted")
        if not control and self.ordinary >= self.total - self.reserved:
            raise ProtocolError("ordinary slots exhausted")
        if control:
            self.control += 1
        else:
            self.ordinary += 1

    def release(self, *, control: bool) -> None:
        if control:
            self.control -= 1
        else:
            self.ordinary -= 1


class FrameOutbox:
    """One writer; a queued control frame is always written before any queued ordinary frame.

    The outbound ``seq`` is stamped when a frame is popped for the wire, not
    when it is queued, so the parent always reads ``seq == previous + 1`` even
    after a control frame overtook queued ordinary frames. A queued item is a
    builder ``seq -> bytes``. A broken pipe drops the queue and calls
    ``on_broken`` once.
    """

    def __init__(self, sink: Callable[[bytes], Awaitable[None]], *, on_broken: Callable[[], None] | None = None,
                 on_error: Callable[[], None] | None = None):
        self._sink = sink
        self._on_broken = on_broken
        self._on_error = on_error
        self._control: deque[Callable[[int], bytes]] = deque()
        self._ordinary: deque[Callable[[int], bytes]] = deque()
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._closing = False
        self.broken = False
        self.sent = 0

    def put(self, build: Callable[[int], bytes], *, control: bool) -> None:
        if self.broken:
            return
        (self._control if control else self._ordinary).append(build)
        self._idle.clear()
        self._wake.set()

    async def run(self) -> None:
        while True:
            while not self._control and not self._ordinary:
                self._idle.set()
                if self._closing:
                    return
                self._wake.clear()
                await self._wake.wait()
            build = (self._control or self._ordinary).popleft()
            self.sent += 1
            try:
                data = build(self.sent)
            except Exception:  # noqa: BLE001 - a frame that cannot be encoded stops the worker
                self._fail(self._on_error)
                continue
            try:
                await self._sink(data)
            except (BrokenPipeError, ConnectionResetError, OSError):
                # The parent's read end is gone: nothing more can be delivered.
                self._fail(self._on_broken)

    def _fail(self, callback: Callable[[], None] | None) -> None:
        """Nothing more is delivered: drop the queue (so ``flush`` returns) and report once."""
        self.broken = True
        self._control.clear()
        self._ordinary.clear()
        if callback is not None:
            callback()

    async def flush(self) -> None:
        await self._idle.wait()

    def close(self) -> None:
        self._closing = True
        self._wake.set()


# ---------------------------------------------------------------- the worker process

def _libc():
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    return libc


def _harden() -> dict[str, Any]:
    """Step 1: not dumpable (Linux), before any other work and so before any secret."""
    if sys.platform != "linux":
        return {"platform": sys.platform, "dumpable": None, "parent_death_signal": None,
                "parent_watch": ["kqueue", "stdin_eof"] if sys.platform == "darwin" else ["stdin_eof"]}
    try:
        libc = _libc()
        if libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) != 0 or libc.prctl(_PR_GET_DUMPABLE, 0, 0, 0, 0) != 0:
            raise WorkerExit("hardening_unavailable", "worker_dumpable_not_cleared")
    except (OSError, AttributeError) as exc:
        raise WorkerExit("hardening_unavailable", "worker_prctl_unavailable") from exc
    return {"platform": "linux", "dumpable": 0, "parent_death_signal": None, "parent_watch": ["pdeathsig", "stdin_eof"]}


def _validate_invocation() -> int:
    """Step 2: the fixed interpreter flags, the exact argv, the closed env and pipe stdio."""
    flags = sys.flags
    if not (flags.isolated and flags.ignore_environment and flags.no_user_site and flags.safe_path
            and flags.dont_write_bytecode):
        raise WorkerExit("invocation_refused", "worker_interpreter_flags_invalid")
    if not os.path.isabs(sys.executable):
        raise WorkerExit("invocation_refused", "worker_interpreter_not_absolute")
    expected = [*INTERPRETER_FLAGS, "-c", WORKER_BOOTSTRAP, str(BACKEND_ROOT)]
    if list(sys.orig_argv[1:]) != expected or len(sys.argv) != 2 or sys.argv[1] != str(BACKEND_ROOT):
        raise WorkerExit("invocation_refused", "worker_argv_not_closed")
    problem = _env_problem(os.environ, platform=sys.platform)
    if problem is not None:
        raise WorkerExit("invocation_refused", problem[0], env_names=problem[1])
    for descriptor in (0, 1):
        try:
            mode = os.fstat(descriptor).st_mode
        except OSError as exc:
            raise WorkerExit("invocation_refused", "worker_stdio_not_pipes") from exc
        if not (stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)):
            raise WorkerExit("invocation_refused", "worker_stdio_not_pipes")
    return int(os.environ.get(PARENT_PID_ENV, "0"))


def _arm_parent_death(parent_pid: int, hardening: dict[str, Any]):
    """Step 3: PDEATHSIG (Linux) or a kqueue exit watch (macOS), then the reparent check.

    On Linux, arming first and checking second closes the race where the
    parent dies before the hook exists: the check then sees a different
    parent. On macOS the check comes first (the distinct mismatch reason), then
    the kqueue registration (ESRCH for a parent that exited since), then the
    check again, so a pid reused in between is never watched in its place.
    """
    watch = None
    if sys.platform == "linux":
        try:
            import ctypes
            libc = _libc()
            value = ctypes.c_int()
            if (libc.prctl(_PR_SET_PDEATHSIG, int(PARENT_DEATH_SIGNAL), 0, 0, 0) != 0
                    or libc.prctl(_PR_GET_PDEATHSIG, ctypes.addressof(value), 0, 0, 0) != 0
                    or value.value != int(PARENT_DEATH_SIGNAL)):
                raise WorkerExit("hardening_unavailable", "worker_pdeathsig_unavailable")
        except (OSError, AttributeError) as exc:
            raise WorkerExit("hardening_unavailable", "worker_prctl_unavailable") from exc
        hardening["parent_death_signal"] = int(PARENT_DEATH_SIGNAL)
    if os.getppid() != parent_pid:
        raise WorkerExit("parent_lost", "worker_parent_mismatch_at_start")
    if sys.platform == "darwin":
        # Checked first, then watched: a parent that exits in between makes the registration fail (ESRCH).
        import select
        watch = select.kqueue()
        try:
            watch.control([select.kevent(parent_pid, filter=select.KQ_FILTER_PROC,
                                         flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                         fflags=select.KQ_NOTE_EXIT)], 0, 0)
        except OSError as exc:
            watch.close()
            raise WorkerExit("parent_lost", "worker_parent_gone_at_start") from exc
        if os.getppid() != parent_pid:
            # The parent exited after the first check and its pid was reused before
            # the registration: the watch names another process.
            watch.close()
            raise WorkerExit("parent_lost", "worker_parent_gone_at_start")
    return watch


def _detach_stdio() -> tuple[int, int]:
    """Step 4: frames move to private descriptors; fd 0 reads /dev/null and fd 1 writes stderr."""
    frames_in, frames_out = os.dup(0), os.dup(1)  # non-inheritable (PEP 446)
    null = os.open(os.devnull, os.O_RDONLY)
    try:
        os.dup2(null, 0)
    finally:
        os.close(null)
    os.dup2(2, 1)
    return frames_in, frames_out


@dataclass
class _Outcome:
    name: str
    reason_code: str
    ack: dict[str, Any] | None = None


class _Session:
    """One parent's framed channel over the attached ``ProviderWorker``."""

    def __init__(self, provider_worker, reader: asyncio.StreamReader, outbox: FrameOutbox, *,
                 hardening: dict[str, Any], env_names: list[str], request_stop: Callable[[str, str], None]):
        self.worker, self.reader, self.outbox = provider_worker, reader, outbox
        self.request_stop = request_stop
        self.hardening, self.env_names = hardening, env_names
        self.admission = SlotAdmission()
        self.boot_nonce: str | None = None
        self.received = 0
        self.in_flight: set[str] = set()
        self.tasks: set[asyncio.Task] = set()

    async def serve(self, stop: asyncio.Event, stop_reason: list[tuple[str, str]]) -> _Outcome:
        stopped = asyncio.create_task(stop.wait(), name="worker-entry-stop")
        try:
            while True:
                read = asyncio.create_task(read_frame(self.reader, kind="request"), name="worker-entry-read")
                done, _ = await asyncio.wait({read, stopped}, return_when=asyncio.FIRST_COMPLETED)
                if stopped in done:
                    read.cancel()
                    await asyncio.gather(read, return_exceptions=True)
                    return _Outcome(*stop_reason[0])
                try:
                    frame = read.result()
                except ProtocolError as exc:
                    raise WorkerExit("protocol_violation", "worker_frame_invalid") from exc
                if frame is None:
                    return _Outcome("parent_lost", "worker_stdin_eof")
                outcome = self._dispatch(frame)
                if outcome is not None:
                    return outcome
        finally:
            stopped.cancel()
            await asyncio.gather(stopped, return_exceptions=True)

    def _dispatch(self, frame: dict[str, Any]) -> _Outcome | None:
        method = frame["method"]
        if frame["seq"] != self.received + 1:
            raise WorkerExit("protocol_violation", "worker_frame_out_of_order")
        self.received = frame["seq"]
        if self.boot_nonce is None:
            if method != "worker.hello":
                raise WorkerExit("protocol_violation", "worker_hello_required")
            self.boot_nonce = frame["boot_nonce"]
        elif method == "worker.hello":
            raise WorkerExit("protocol_violation", "worker_hello_repeated")
        elif frame["boot_nonce"] != self.boot_nonce:
            raise WorkerExit("protocol_violation", "worker_boot_nonce_mismatch")
        if frame["request_id"] in self.in_flight:
            raise WorkerExit("protocol_violation", "worker_request_id_in_flight")
        control = method in CONTROL_METHODS
        try:
            self.admission.admit(control=control)
        except ProtocolError as exc:
            raise WorkerExit("protocol_violation", "worker_slots_exhausted") from exc
        if not control:
            self.in_flight.add(frame["request_id"])
            task = asyncio.create_task(self._ordinary(frame), name="worker-entry-ordinary")
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            return None
        try:
            if method == "worker.shutdown":
                # Answered after the shutdown order released the lock.
                return _Outcome("stopped", "worker_shutdown_requested", ack=frame)
            self._respond(frame, self._control_payload(method), control=True)
            return None
        finally:
            self.admission.release(control=True)

    def _control_payload(self, method: str) -> dict[str, Any]:
        if method == "worker.heartbeat":
            return {"state": "alive"}
        projected = _projection(self.worker.snapshot())
        if method == "worker.status":
            return {**projected, "workspace_lock": "held"}
        return {"state": "ready", "pid": os.getpid(), "workspace_lock": "held",
                "activation_order": list(self.worker.activation_order), **projected,
                "env_names": self.env_names, "hardening": dict(self.hardening)}

    async def _ordinary(self, frame: dict[str, Any]) -> None:
        try:
            snapshot = await self.worker.readiness(probe_writes=frame["payload"]["probe_writes"])
            self._respond(frame, _projection(snapshot), control=False)
        except Exception:  # noqa: BLE001 - an admitted request that cannot be answered stops the worker
            self.request_stop("internal_error", "worker_ordinary_request_failed")
        finally:
            self.in_flight.discard(frame["request_id"])
            self.admission.release(control=False)

    def _respond(self, request: dict[str, Any], payload: dict[str, Any], *, control: bool) -> None:
        response = {"protocol": PROTOCOL, "boot_nonce": self.boot_nonce, "request_id": request["request_id"],
                    "seq": 1, "kind": "response", "method": request["method"], "payload": payload}
        encode(response, kind="response")  # validate now: a bad payload fails its own request, not the writer
        self.outbox.put(lambda seq: encode({**response, "seq": seq}, kind="response"), control=control)

    async def drain(self) -> None:
        """Finish every admitted ordinary request; none is abandoned mid-way."""
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)


def _projection(snapshot: dict[str, Any]) -> dict[str, Any]:
    """The closed provider view: names, states and typed reasons only."""
    return {"providers": [{"provider": item["provider"], "state": item["state"], "reason_code": item["reason_code"]}
                          for item in snapshot["providers"]],
            "mandatory_ready": bool(snapshot["mandatory_ready"])}


async def _run(frames_in: int, frames_out: int, parent_pid: int, parent_watch, hardening: dict[str, Any],
               report: dict[str, Any]) -> _Outcome:
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    stop_reason: list[tuple[str, str]] = []

    def request_stop(name: str, reason_code: str) -> None:
        if not stop_reason:
            stop_reason.append((name, reason_code))
        stop.set()

    for signum, name, reason_code in _STOP_SIGNALS:
        loop.add_signal_handler(signum, request_stop, name, reason_code)

    def hangup() -> None:
        if sys.platform == "linux" and os.getppid() != parent_pid:
            request_stop("parent_lost", "worker_parent_death_signal")
        else:
            request_stop("hangup", "worker_signal_hangup")
    loop.add_signal_handler(PARENT_DEATH_SIGNAL, hangup)
    if parent_watch is not None:
        def parent_exited() -> None:
            # The kqueue descriptor stays readable: stop watching, then stop.
            loop.remove_reader(parent_watch.fileno())
            request_stop("parent_lost", "worker_parent_exited")
        loop.add_reader(parent_watch.fileno(), parent_exited)
    reader = asyncio.StreamReader(limit=MAX_FRAME + 4)
    in_file, out_file = os.fdopen(frames_in, "rb", buffering=0), os.fdopen(frames_out, "wb", buffering=0)
    read_transport, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), in_file)
    write_transport, write_protocol = await loop.connect_write_pipe(
        lambda: asyncio.StreamReaderProtocol(asyncio.StreamReader()), out_file)
    stream = asyncio.StreamWriter(write_transport, write_protocol, None, loop)

    async def sink(data: bytes) -> None:
        if write_transport.is_closing():
            raise BrokenPipeError
        stream.write(data)
        await stream.drain()

    outbox = FrameOutbox(sink, on_broken=lambda: request_stop("parent_lost", "worker_stdout_broken"),
                         on_error=lambda: request_stop("internal_error", "worker_frame_encode_failed"))
    writer = asyncio.create_task(outbox.run(), name="worker-entry-writer")
    try:
        return await _host(reader, outbox, stop, stop_reason, request_stop, hardening, report)
    finally:
        outbox.close()
        await asyncio.gather(writer, return_exceptions=True)
        read_transport.close()
        write_transport.close()
        for signum in (*(item[0] for item in _STOP_SIGNALS), PARENT_DEATH_SIGNAL):
            loop.remove_signal_handler(signum)
        if parent_watch is not None:
            loop.remove_reader(parent_watch.fileno())


async def _host(reader, outbox: FrameOutbox, stop: asyncio.Event, stop_reason: list[tuple[str, str]],
                request_stop: Callable[[str, str], None], hardening: dict[str, Any],
                report: dict[str, Any]) -> _Outcome:
    """Step 5 and 6: settings load from here; composition, lock, mandatory-first activation, serve, shutdown."""
    from src.workspace.production import DuplicateWorkspaceOwnerError, ProductionWorkspaceError
    from . import worker as worker_module
    from .profiles import CompositionProfileInvalid, boot_profile, release_profile
    try:
        boot_profile()
    except CompositionProfileInvalid as exc:
        raise WorkerExit("composition_refused", "composition_profile_invalid", detail=exc.reason) from exc
    try:
        # The passive entry always owns the workspace (#1026 §1), in every deployment environment.
        provider_worker = worker_module.attach(worker_module.ProviderWorker(hold_workspace_lock=True))
        report["providers"] = provider_worker
        try:
            provider_worker.acquire_workspace()
        except DuplicateWorkspaceOwnerError as exc:
            provider_worker.detach()
            raise WorkerExit("workspace_owner_busy", exc.reason_code) from exc
        except ProductionWorkspaceError as exc:
            provider_worker.detach()
            raise WorkerExit("workspace_lock_unavailable", getattr(exc, "reason_code", "workspace_lock_unavailable")) from exc
        report["workspace_lock"] = "held"
        session = _Session(provider_worker, reader, outbox, hardening=hardening, env_names=sorted(os.environ),
                           request_stop=request_stop)
        outcome: _Outcome | None = None
        try:
            try:
                await provider_worker.activate()
            except worker_module.ProviderActivationError as exc:
                raise WorkerExit("mandatory_provider_failed", exc.reason_code, provider=exc.provider) from exc
            outcome = await session.serve(stop, stop_reason)
        finally:
            # The #1024 shutdown order (app.py's provider-worker lifespan); the lock goes last.
            provider_worker.begin_shutdown()
            try:
                await session.drain()
            finally:
                try:
                    try:
                        await provider_worker.wait_maintenance()
                    finally:
                        provider_worker.close_gate()
                finally:
                    try:
                        await provider_worker.close_state()
                    finally:
                        try:
                            provider_worker.release_workspace()
                            report["workspace_lock"] = "released"
                        finally:
                            report["providers"] = _projection(provider_worker.snapshot())["providers"]
                            provider_worker.detach()
        if outcome.ack is not None:
            session._respond(outcome.ack, {"state": "stopped", "workspace_lock": "released"}, control=True)
        await outbox.flush()
        return outcome
    finally:
        release_profile()


def _emit(report: dict[str, Any]) -> None:
    providers = report.get("providers")
    if providers is not None and not isinstance(providers, list):
        providers = _projection(providers.snapshot())["providers"]
    line = json.dumps({**report, "providers": providers}, sort_keys=True, separators=(",", ":")) + "\n"
    try:
        os.write(2, line.encode("utf-8"))
    except OSError:
        pass


def main() -> int:
    """Run the passive worker until shutdown or parent loss; return its ``EXIT_CODES`` value."""
    report: dict[str, Any] = {"event": EXIT_EVENT, "exit": "internal_error", "exit_code": EXIT_CODES["internal_error"],
                              "reason_code": "worker_internal_error", "details": {}, "providers": None,
                              "workspace_lock": "never_held"}
    try:
        hardening = _harden()
        parent_pid = _validate_invocation()
        parent_watch = _arm_parent_death(parent_pid, hardening)
        try:
            frames_in, frames_out = _detach_stdio()
            outcome = asyncio.run(_run(frames_in, frames_out, parent_pid, parent_watch, hardening, report))
        finally:
            if parent_watch is not None:
                parent_watch.close()
        report.update(exit=outcome.name, exit_code=EXIT_CODES[outcome.name], reason_code=outcome.reason_code)
    except WorkerExit as exc:
        report.update(exit=exc.name, exit_code=EXIT_CODES[exc.name], reason_code=exc.reason_code,
                      details=exc.details)
    except BaseException as exc:  # noqa: BLE001 - one typed exit; the type name only, never a message
        report["details"] = {"exception": type(exc).__name__}
    finally:
        _emit(report)
    return report["exit_code"]


__all__ = ["BACKEND_ROOT", "CONTROL_METHODS", "CONTROL_RESERVED", "EXIT_CODES", "EXIT_EVENT", "FrameOutbox",
           "INTERPRETER_FLAGS", "METHODS", "ORDINARY_LIMIT", "ORDINARY_METHODS", "REQUIRED_ENV", "SlotAdmission",
           "WORKER_BOOTSTRAP", "WORKER_ENV", "WorkerExit", "WorkerLaunch", "encode", "main", "read_frame",
           "validate_frame", "worker_launch"]
