"""Trusted fixed child, bounded anonymous pipes and fail-closed confinement."""
from __future__ import annotations
import ctypes
import errno
import importlib.util
import json
from pathlib import Path
import signal
import struct
import sys

_IO_URING_SYSCALLS = ("io_uring_setup", "io_uring_enter", "io_uring_register")


def _probe_io_uring_denial(syscalls):
    """Use invalid, contact-free arguments; only actual EPERM proves denial."""
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    arguments = {
        "io_uring_setup": (ctypes.c_uint(0), ctypes.c_void_p()),
        "io_uring_enter": (ctypes.c_int(-1), ctypes.c_uint(0), ctypes.c_uint(0),
            ctypes.c_uint(0), ctypes.c_void_p(), ctypes.c_size_t(0)),
        "io_uring_register": (ctypes.c_int(-1), ctypes.c_uint(0), ctypes.c_void_p(), ctypes.c_uint(0)),
    }
    for name in _IO_URING_SYSCALLS:
        ctypes.set_errno(0)
        result = libc.syscall(ctypes.c_long(syscalls[name]), *arguments[name])
        if result != -1 or ctypes.get_errno() != errno.EPERM:
            raise RuntimeError("io_uring confinement ineffective")


def confine():
    import resource
    import mmap
    resource.setrlimit(resource.RLIMIT_AS, (512*1024*1024,)*2)
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    for limit, expected in ((resource.RLIMIT_AS, (512*1024*1024,)*2),
        (resource.RLIMIT_CPU, (10, 10)), (resource.RLIMIT_CORE, (0, 0)),
        (resource.RLIMIT_NOFILE, (64, 64))):
        if resource.getrlimit(limit) != expected:
            raise RuntimeError("resource confinement ineffective")
    positive = mmap.mmap(-1, 8*1024*1024); positive.close()
    try:
        over = mmap.mmap(-1, 512*1024*1024+mmap.PAGESIZE)
    except MemoryError:
        pass
    except OSError as exc:
        if exc.errno != errno.ENOMEM:
            raise RuntimeError("resource confinement ineffective") from None
    else:
        over.close()
        raise RuntimeError("resource confinement ineffective")
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.setitimer(signal.ITIMER_REAL, 30)
    if sys.platform == "linux":
        library = ctypes.CDLL("libseccomp.so.2", use_errno=True)
        library.seccomp_init.argtypes = [ctypes.c_uint32]
        library.seccomp_init.restype = ctypes.c_void_p
        library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
        library.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
        library.seccomp_load.argtypes = [ctypes.c_void_p]
        library.seccomp_release.argtypes = [ctypes.c_void_p]
        context = library.seccomp_init(0x7fff0000)
        if not context:
            raise RuntimeError("confinement unavailable")
        io_uring_syscalls = {}
        try:
            for name in ("socket", "socketpair", "connect", "bind", "listen", "accept", "accept4",
                "sendto", "sendmsg", "recvfrom", "recvmsg", "execve", "execveat", "fork", "vfork", "clone", "clone3",
                *_IO_URING_SYSCALLS):
                syscall = library.seccomp_syscall_resolve_name(name.encode())
                if name in _IO_URING_SYSCALLS:
                    if syscall < 0:
                        raise RuntimeError("mandatory io_uring confinement unavailable")
                    io_uring_syscalls[name] = syscall
                if syscall >= 0 and library.seccomp_rule_add(context, 0x50000 | errno.EPERM, syscall, 0) != 0:
                    raise RuntimeError("confinement unavailable")
            if library.seccomp_load(context) != 0:
                raise RuntimeError("confinement unavailable")
        finally:
            library.seccomp_release(context)
        _probe_io_uring_denial(io_uring_syscalls)
    elif sys.platform == "darwin":
        # Existing native sandbox-exec primitive; no platform receipt implied.
        library = ctypes.CDLL("/usr/lib/libsandbox.dylib")
        library.sandbox_init.argtypes = [ctypes.c_char_p, ctypes.c_uint64, ctypes.POINTER(ctypes.c_char_p)]
        error = ctypes.c_char_p()
        if library.sandbox_init(b"(version 1)(allow default)(deny network*)(deny process-exec)(deny process-fork)", 0, ctypes.byref(error)) != 0:
            raise RuntimeError("confinement unavailable")
    else:
        raise RuntimeError("confinement unavailable")
    # Prove network denial before private input. No live network destination.
    import socket
    try:
        probe = socket.socket()
    except PermissionError:
        return
    else:
        probe.close()
        raise RuntimeError("network confinement ineffective")


def main():
    try:
        confine()
        ready = {"state": "ready", "network_denied": True}
        if len(sys.argv) == 2:
            ready["nonce"] = sys.argv[1]
        print(json.dumps(ready), flush=True)
        header = sys.stdin.buffer.read(8)
        if len(header) != 8:
            raise ValueError()
        request_size, source_size = struct.unpack("!II", header)
        if not 0 < request_size <= 8192 or not 0 < source_size <= 16*1024*1024:
            raise ValueError()
        request_raw = sys.stdin.buffer.read(request_size)
        source = sys.stdin.buffer.read(source_size)
        if len(request_raw) != request_size or len(source) != source_size or sys.stdin.buffer.read(1):
            raise ValueError()
        request = json.loads(request_raw)
        spec = importlib.util.spec_from_file_location("document_read_parser", Path(__file__).with_name("document_read_parser.py"))
        parser = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(parser)
        evidence = parser.extract(source, request)
        output = parser.canonical({"status": "succeeded", "evidence": evidence,
            "cleanup": "parser_exit_required", "provider_contacts": 0})
    except Exception as exc:
        reason = str(exc) if exc.__class__.__name__ == "DocumentReadError" else (
            "document_confinement_unavailable" if isinstance(exc, (RuntimeError, OSError, ImportError)) else "document_parser_failed")
        output = json.dumps({"status": "blocked", "reason": reason, "no_learning": True, "provider_contacts": 0}).encode()
    if len(output) > 1024*1024 + 4096:
        return 2
    sys.stdout.buffer.write(output); sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
