"""Positive native callback/process quiescence evidence; no authority or lifecycle."""
from __future__ import annotations

import asyncio
import ctypes
import os
import sys
import uuid
from pathlib import Path
from typing import Any


def _host_platform():
    return sys.platform


class _DarwinBsdInfo(ctypes.Structure):
    # Apple public proc_info.h, MAXCOMLEN=16; fixed supported ABI only.
    _fields_ = [(name, ctypes.c_uint32) for name in (
        "flags", "status", "xstatus", "pid", "ppid", "uid", "gid", "ruid", "rgid", "svuid", "svgid", "reserved")]
    _fields_ += [("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)]
    _fields_ += [(name, ctypes.c_uint32) for name in ("nfiles", "pgid", "jobc", "tdev", "tpgid")]
    _fields_ += [("nice", ctypes.c_int32), ("start_sec", ctypes.c_uint64), ("start_usec", ctypes.c_uint64)]


def _darwin_boot_id():
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    query = library.sysctlbyname
    query.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    query.restype = ctypes.c_int
    buffer, size = ctypes.create_string_buffer(37), ctypes.c_size_t(37)
    if query(b"kern.bootsessionuuid", buffer, ctypes.byref(size), None, 0) != 0 or size.value != 37:
        raise OSError("native boot-session witness unavailable")
    return str(uuid.UUID(buffer.value.decode("ascii")))


def _darwin_start(pid):
    if (sys.byteorder != "little" or ctypes.sizeof(_DarwinBsdInfo) != 136
            or _DarwinBsdInfo.pid.offset != 12 or _DarwinBsdInfo.start_sec.offset != 120
            or _DarwinBsdInfo.start_usec.offset != 128 or not 0 < pid <= 2147483647):
        raise OSError("native process witness ABI unsupported")
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    query = library.proc_pidinfo
    query.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    query.restype = ctypes.c_int
    info = _DarwinBsdInfo()
    if query(pid, 3, 0, ctypes.byref(info), 136) != 136 or info.pid != pid or not info.start_sec or info.start_usec >= 1000000:
        raise OSError("native process lifetime witness unknown")
    return info.start_sec, info.start_usec


def process_identity() -> dict[str, Any]:
    """Read the fixed native lifetime witness only at explicit admission."""
    pid = os.getpid()
    if _host_platform() == "darwin":
        boot = _darwin_boot_id()
        seconds, microseconds = _darwin_start(pid)
        return {"platform": "darwin", "boot_id": boot, "pid": pid,
                "pid_start_sec": seconds, "pid_start_usec": microseconds}
    if _host_platform() != "linux":
        raise OSError("Native process lifetime witness unsupported")
    boot = str(uuid.UUID(Path('/proc/sys/kernel/random/boot_id').read_text().strip()))
    stat = Path(f'/proc/{pid}/stat').read_text()
    start = stat[stat.rfind(')') + 2:].split()[19]
    namespace = os.stat('/proc/self/ns/pid').st_ino
    if not start.isdigit() or namespace <= 0 or not 0 < pid <= 2147483647:
        raise ValueError('Native process identity is invalid')
    return {'platform': 'linux', 'boot_id': boot, 'pid': pid,
            'pid_start_ticks': start, 'pid_namespace': namespace}


def positive_owner_death(witness: dict[str, Any]) -> str | None:
    try:
        platform = witness.get("platform")
        if platform != _host_platform() or platform not in {"linux", "darwin"}:
            return None
        if platform == "darwin":
            if _darwin_boot_id() != witness["boot_id"]:
                return 'darwin_boot_changed'
            # Missing PID or native lookup failure is unknown on Darwin.
            start = _darwin_start(witness["pid"])
            return 'positive_process_death' if start != (witness["pid_start_sec"], witness["pid_start_usec"]) else None
        current = process_identity()
        if current['boot_id'] != witness['boot_id']:
            return 'linux_boot_changed'
        if current['pid_namespace'] != witness['pid_namespace']:
            return None
        try:
            stat = Path(f"/proc/{witness['pid']}/stat").read_text()
        except FileNotFoundError:
            return 'positive_process_death'
        fields = stat[stat.rfind(')') + 2:].split()
        if fields[19] != witness['pid_start_ticks'] or fields[0] == 'Z':
            return 'positive_process_death'
    except (OSError, ValueError, IndexError, KeyError, TypeError):
        return None
    return None


class NativeCallbackOwners:
    def __init__(self) -> None:
        self.nonce = uuid.uuid4().hex
        self.callbacks: dict[str, tuple[asyncio.Task, dict[str, Any]]] = {}
        self.awaited: set[asyncio.Task] = set()

    def bind(self, job_id: str, witness: dict[str, Any]) -> None:
        task = asyncio.current_task()
        if task is None or job_id in self.callbacks:
            raise RuntimeError('The original native callback is already bound')
        self.callbacks[job_id] = (task, dict(witness))

    def proof(self, job_id: str, witness: dict[str, Any]) -> str | None:
        original = self.callbacks.get(job_id)
        if original and original[1] == witness:
            task = original[0]
            if task.done() and task in self.awaited:
                return 'owned_positive_close'
            return None
        return positive_owner_death(witness)
