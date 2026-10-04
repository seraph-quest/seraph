"""Trusted fixed-profile bootstrap; no backend imports or package search paths.

Loaded by isolated CPython -I -S -B, before the reviewed package code. These
raw BPF instructions target only Linux native x86_64; alternative ABIs kill.
"""
import ctypes
import json
import os
import resource
import struct
import sys


class Filter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_ushort), ("jt", ctypes.c_ubyte),
                ("jf", ctypes.c_ubyte), ("k", ctypes.c_uint32)]


class Program(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(Filter))]


def install_filter():
    # seccomp_data: nr at 0, arch at 4, args[2] at 32. Check ABI FIRST.
    kill, denied, allowed = 0x80000000, 0x00050001, 0x7fff0000
    instructions = [(0x20, 0, 0, 4), (0x15, 1, 0, 0xc000003e),
                    (0x06, 0, 0, kill), (0x20, 0, 0, 0),
                    (0x45, 0, 1, 0x40000000), (0x06, 0, 0, kill)]
    # mmap/mprotect may never create executable memory. prlimit may only read.
    for number, forbidden_bit in ((9, 4), (10, 4)):
        instructions.extend([(0x15, 0, 5, number), (0x20, 0, 0, 32),
            (0x45, 0, 1, forbidden_bit), (0x06, 0, 0, denied),
            (0x06, 0, 0, allowed), (0x20, 0, 0, 0)])
    instructions.extend([(0x15, 0, 7, 302), (0x20, 0, 0, 32),
        (0x15, 0, 3, 0), (0x20, 0, 0, 36), (0x15, 0, 1, 0),
        (0x06, 0, 0, allowed), (0x06, 0, 0, denied), (0x20, 0, 0, 0)])
    # Only bounded private file/pipe I/O, allocation, clocks and process exit.
    # No socket/IPC/fork/clone/exec/mount/ptrace/policy-changing syscall.
    for number in (0, 1, 3, 4, 5, 6, 8, 11, 12, 13, 14, 15, 17, 19, 20,
                   24, 25, 28, 39, 60, 63, 72, 79, 89, 96, 97, 98, 100,
                   102, 104, 107, 108, 186, 202, 217, 228, 231, 257, 262,
                   267, 269, 318, 332):
        instructions.extend([(0x15, 0, 1, number), (0x06, 0, 0, allowed)])
    instructions.append((0x06, 0, 0, denied))
    filters = (Filter * len(instructions))(*(Filter(*item) for item in instructions))
    program = Program(len(instructions), filters)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                          ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    if libc.prctl(38, 1, 0, 0, 0) != 0 or libc.prctl(22, 2, ctypes.addressof(program), 0, 0) != 0:
        raise RuntimeError("tool_package_seccomp_unavailable")


def main():
    if sys.platform != "linux" or os.uname().machine != "x86_64" or struct.calcsize("P") != 8:
        raise RuntimeError("tool_package_unsupported_platform")
    # Even a trusted launcher must not forward its internal descriptors.
    hard = resource.getrlimit(resource.RLIMIT_NOFILE)[1]
    os.closerange(3, int(hard if hard != resource.RLIM_INFINITY else 1048576))
    resource.setrlimit(resource.RLIMIT_CPU, (2, 2))
    resource.setrlimit(resource.RLIMIT_AS, (128 * 1024 * 1024, 128 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_FSIZE, (65536, 65536))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    initial_fds = []
    for descriptor in range(64):
        try:
            os.fstat(descriptor)
            initial_fds.append(descriptor)
        except OSError:
            pass
    if initial_fds != [0, 1, 2]:
        raise RuntimeError("tool_package_inherited_descriptor")
    install_filter()
    with open("/proc/self/status", "r", encoding="utf-8") as source:
        status = {line.split(":", 1)[0]: line.split(":", 1)[1].strip()
                  for line in source if ":" in line}
    proof = {"profile": "json-python-bwrap-v1", "arch": "linux-x86_64",
             "initial_fds": initial_fds, "seccomp": status.get("Seccomp"),
             "no_new_privs": status.get("NoNewPrivs"), "effective_caps": status.get("CapEff"),
             "cpu_seconds": 2, "address_space_bytes": 128 * 1024 * 1024,
             "file_size_bytes": 65536, "max_fds": 64, "no_learning": True}
    if proof["seccomp"] != "2" or proof["no_new_privs"] != "1" or proof["effective_caps"] != "0000000000000000":
        raise RuntimeError("tool_package_enforcement_unavailable")
    print(json.dumps(proof, sort_keys=True), flush=True)
    if len(sys.argv) == 2 and sys.argv[1] == "preflight":
        return
    if len(sys.argv) != 1:
        raise RuntimeError("tool_package_arguments_invalid")
    with open("/package.py", "rb") as source:
        code = source.read(32769)
    if len(code) > 32768:
        raise RuntimeError("tool_package_code_limit")
    exec(compile(code, "/package.py", "exec"), {"__name__": "__main__"})


if __name__ == "__main__":
    main()
