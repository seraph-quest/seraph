'Quiet test-only esbuild exec wrapper; stdout/stderr remain binary pipes.\n\nUses unchanged R3 AF_UNIX/io_uring denial algorithm plus R64 outbound denial.\nIt does not import the original command driver (which would launch a command).\n'
import ctypes
import errno
import hashlib
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import socket
import stat
import sys
# Original -I child keeps normal site startup; adjacent Source must stay immutable.
sys.dont_write_bytecode = True

_FAILURE_STAGE = 'entry'
_DIAGNOSTIC_STDERR_OWNED = False
_DIAGNOSTIC_FD_KINDS = {'0': None, '1': None, '2': None}


def check_frontend_stderr(context, info):
    source = Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/managed-execution/source')
    if context['source_root'] != str(source) or Path.cwd() != source / 'frontend':
        raise RuntimeError('esbuild_unowned_stderr')
    parents = (source.parent, source, source / 'logs')
    def identity(value):
        return (value.st_dev, value.st_ino, value.st_mode, value.st_uid, value.st_nlink)
    def directory_identities():
        result = []
        for parent in parents:
            value = parent.lstat()
            if (parent.resolve(strict=True) != parent or not stat.S_ISDIR(value.st_mode)
                    or value.st_uid != os.getuid() or stat.S_IMODE(value.st_mode) != 0o700):
                raise RuntimeError('esbuild_unowned_stderr')
            result.append(identity(value))
        return result
    directories = directory_identities()
    path = source / 'logs/seraph-local-frontend.log'
    before = path.lstat()
    flags = fcntl.fcntl(2, fcntl.F_GETFL)
    if (path.resolve(strict=True) != path or not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid() or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or identity(info) != identity(before)
            or os.readlink('/proc/self/fd/2') != str(path)
            or not flags & os.O_APPEND or flags & os.O_ACCMODE != os.O_WRONLY):
        raise RuntimeError('esbuild_unowned_stderr')
    after = path.lstat()
    retained = os.fstat(2)
    if (identity(after) != identity(before) or identity(retained) != identity(before)
            or path.resolve(strict=True) != path
            or os.readlink('/proc/self/fd/2') != str(path)
            or fcntl.fcntl(2, fcntl.F_GETFL) != flags
            or directory_identities() != directories):
        raise RuntimeError('esbuild_unowned_stderr')


def check_original_node_stream(context, descriptor):
    # Only two exact inherited esbuild stdio endpoints; no socket creation.
    if descriptor not in (0, 1) or type(descriptor) is not int:
        raise RuntimeError('esbuild_stdio_endpoint_denied')
    parent = os.getppid()
    directory = Path('/proc') / str(parent)
    def parent_identity():
        raw = (directory / 'stat').read_bytes()
        start = int(raw.rsplit(b')', 1)[1].split()[19])
        argv = [value.decode('utf-8') for value in (directory / 'cmdline').read_bytes().split(b'\0') if value]
        executable = (directory / 'exe').resolve(strict=True)
        cwd = (directory / 'cwd').resolve(strict=True)
        if (argv != context['reviewed_frontend_process_argv']
                or str(executable) != argv[0]
                or str(cwd) != str(Path(context['source_root']) / 'frontend')
                or context['executable_pins'].get(str(executable)) != hashlib.sha256(executable.read_bytes()).hexdigest()):
            raise RuntimeError('esbuild_original_node_parent_denied')
        return parent, start, str(executable), tuple(argv), str(cwd)
    original_parent = parent_identity()
    before = os.fstat(descriptor)
    if not stat.S_ISSOCK(before.st_mode):
        raise RuntimeError('esbuild_stdio_endpoint_denied')
    library = ctypes.CDLL(None, use_errno=True)
    library.getsockopt.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
    library.getsockopt.restype = ctypes.c_int
    for option, expected in ((39, socket.AF_UNIX), (3, socket.SOCK_STREAM)):
        value = ctypes.c_int()
        size = ctypes.c_uint(ctypes.sizeof(value))
        if (library.getsockopt(descriptor, socket.SOL_SOCKET, option, ctypes.byref(value), ctypes.byref(size)) != 0
                or size.value != ctypes.sizeof(value) or value.value != expected):
            raise RuntimeError('esbuild_stdio_socket_kind_denied')
    class Credentials(ctypes.Structure):
        _fields_ = [('pid', ctypes.c_int), ('uid', ctypes.c_uint), ('gid', ctypes.c_uint)]
    peer = Credentials()
    size = ctypes.c_uint(ctypes.sizeof(peer))
    if (library.getsockopt(descriptor, socket.SOL_SOCKET, socket.SO_PEERCRED, ctypes.byref(peer), ctypes.byref(size)) != 0
            or size.value != ctypes.sizeof(peer) or peer.pid != parent
            or peer.uid != os.getuid() or peer.gid != os.getgid()):
        raise RuntimeError('esbuild_stdio_peer_denied')
    after = os.fstat(descriptor)
    if (tuple(getattr(before, field) for field in ('st_dev', 'st_ino', 'st_mode'))
            != tuple(getattr(after, field) for field in ('st_dev', 'st_ino', 'st_mode'))
            or os.getppid() != parent or parent_identity() != original_parent):
        raise RuntimeError('esbuild_stdio_identity_changed')


def main():
    global _FAILURE_STAGE, _DIAGNOSTIC_STDERR_OWNED
    _FAILURE_STAGE = 'selected_child'
    context = json.loads(Path(sys.argv[1]).read_text())
    command, argv = sys.argv[2], sys.argv[3:]
    if (command != '/home/pawel/repos/seraph/frontend/node_modules/@esbuild/linux-x64/bin/esbuild'
            or argv != ['--service=0.25.12', '--ping']):
        raise RuntimeError('nonselected_esbuild_service')
    matches = [c for c in context['node_children'] if c['kind'] == 'esbuild'
               and c['command'] == command and c['argv'] == argv
               and c['cwd'] == str(Path.cwd()) and c['stdio'] == ['pipe', 'pipe', 'inherit']]
    if len(matches) != 1 or hashlib.sha256(Path(command).read_bytes()).hexdigest() != matches[0]['executable_sha256']:
        raise RuntimeError('esbuild_child_drift')
    # Diagnostic may write only after the existing exact owned-stderr predicate.
    _FAILURE_STAGE = 'owned_stderr'
    check_frontend_stderr(context, os.fstat(2))
    _DIAGNOSTIC_STDERR_OWNED = True
    _FAILURE_STAGE = 'fd_frontier'
    # No inherited socket, extra descriptor, credential or Node launch state.
    names = os.listdir('/proc/self/fd')
    observed = set()
    for name in names:
        try:
            info = os.fstat(int(name))
        except OSError as error:
            if error.errno == errno.EBADF:
                continue
            raise
        if name in _DIAGNOSTIC_FD_KINDS:
            _DIAGNOSTIC_FD_KINDS[name] = stat.S_IFMT(info.st_mode)
        if name in {'0', '1'}:
            check_original_node_stream(context, int(name))
        if name == '2':
            check_frontend_stderr(context, info)
        observed.add(name)
    if observed != {'0', '1', '2'}:
        raise RuntimeError('esbuild_unknown_descriptor')
    _FAILURE_STAGE = 'guard_install'
    guard_path = Path(__file__).with_name('server_guard.py')
    if hashlib.sha256(guard_path.read_bytes()).hexdigest() != '2390acb1aded0518771bf8ecb2875eea8a4a7423759ee842617e3e33ea76914d':
        raise RuntimeError('guard_drift')
    spec = importlib.util.spec_from_file_location('_managed_esbuild_guard', guard_path)
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    if sys.platform != 'linux' or os.uname().machine != 'x86_64':
        raise RuntimeError('unsupported_guard_platform')
    guard.install_filter()

    class Comparison(ctypes.Structure):
        _fields_ = [('arg', ctypes.c_uint), ('op', ctypes.c_uint),
                    ('datum_a', ctypes.c_uint64), ('datum_b', ctypes.c_uint64)]

    lib = ctypes.CDLL('libseccomp.so.2', use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int,
                                         ctypes.c_uint, ctypes.POINTER(Comparison)]
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    _FAILURE_STAGE = 'offline_filter'
    ctx = lib.seccomp_init(0x7fff0000)
    if not ctx:
        raise RuntimeError('offline_guard_unavailable')
    try:
        for name in ('socket', 'socketpair'):
            number = lib.seccomp_syscall_resolve_name(name.encode())
            comparison = Comparison(0, 1, socket.AF_UNIX, 0)
            if number < 0 or lib.seccomp_rule_add_array(ctx, 0x50000 | errno.EPERM,
                    number, 1, ctypes.byref(comparison)) != 0:
                raise RuntimeError('offline_socket_rule_unavailable')
        for name in ('io_uring_setup', 'io_uring_enter', 'io_uring_register'):
            number = lib.seccomp_syscall_resolve_name(name.encode())
            if number < 0 or lib.seccomp_rule_add_array(ctx, 0x50000 | errno.EPERM, number, 0, None) != 0:
                raise RuntimeError('offline_io_uring_rule_unavailable')
        if lib.seccomp_load(ctx) != 0:
            raise RuntimeError('offline_guard_load_failed')
    finally:
        lib.seccomp_release(ctx)
    _FAILURE_STAGE = 'selected_exec'
    os.execve(command, [command, *argv], {'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'TZ': 'UTC'})


if __name__ == '__main__':
    try:
        main()
    except BaseException as error:
        if _DIAGNOSTIC_STDERR_OWNED:
            try:
                codes = ('esbuild_child_drift', 'esbuild_original_node_parent_denied', 'esbuild_stdio_endpoint_denied', 'esbuild_stdio_identity_changed', 'esbuild_stdio_peer_denied', 'esbuild_stdio_socket_kind_denied', 'esbuild_unknown_descriptor', 'esbuild_unowned_stderr', 'guard_drift', 'nonselected_esbuild_service', 'offline_guard_load_failed', 'offline_guard_unavailable', 'offline_io_uring_rule_unavailable', 'offline_socket_rule_unavailable', 'unsupported_guard_platform')
                code = str(error) if type(error) is RuntimeError and str(error) in codes else 'unclassified_failure'
                error_class = type(error).__name__ if type(error) in (RuntimeError, OSError, ValueError, KeyError, TypeError) else 'other'
                error_number = error.errno if isinstance(error, OSError) and type(error.errno) is int and 0 <= error.errno <= 4096 else None
                diagnostic = {'state': 'ESBUILD_WRAPPER_DENIED', 'stage': _FAILURE_STAGE, 'code': code, 'error_class': error_class, 'errno': error_number, 'fd_kinds': _DIAGNOSTIC_FD_KINDS}
                os.write(2, ('ESBUILD_WRAPPER_DIAGNOSTIC ' + json.dumps(diagnostic, sort_keys=True) + '\n').encode('ascii'))
            except BaseException:
                pass
        os._exit(78)
