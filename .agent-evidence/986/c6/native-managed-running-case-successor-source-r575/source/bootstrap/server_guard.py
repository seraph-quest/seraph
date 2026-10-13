'R64 cooperative backend boundary. Source only until independent release.\n\nNot a host/adversarial sandbox; Vite/browser are outside this filter. Unknown\nstartup descriptors fail, without exemptions or ownership guessed from numbers.\n'
import ctypes
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import sys
import time

POLICY = {'version': 'r64-amd64-v1', 'audit_arch': 0xC000003E,
    'deny': [42, 46, 47, 299, 307, 425, 426, 427],
    'socket': 'unix-stream-zero-protocol; inet/inet6-stream',
    'socketpair': 'unix-stream-or-seqpacket-zero-protocol',
    'socket_flags': 'nonblock/cloexec-only', 'sendto': 'null-64bit-destination-only',
    'non_native_abi': 'kill-process', 'x32': 'kill-process',
    'scope': 'marked-backend-and-inherited-descendants'}

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()

def process_start(pid):
    text = Path(f"/proc/{pid}/stat").read_text()
    return int(text[text.rfind(')') + 2:].split()[19])

def process_status(pid):
    return dict(line.split(':', 1) for line in Path(f"/proc/{pid}/status").read_text().splitlines() if ':' in line)

def private_path(path, *, directory=False):
    path = Path(path)
    info = path.lstat()
    if (not path.is_absolute() or path.resolve(strict=True) != path
            or info.st_uid != os.getuid() or info.st_mode & 0o077
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise ValueError('unowned_private_path')
    return path

def fd_identity(fd):
    info = os.fstat(fd)
    return {'device': info.st_dev, 'inode': info.st_ino, 'mode': info.st_mode,
        'uid': info.st_uid, 'nlink': info.st_nlink,
        'open_flags': fcntl.fcntl(fd, fcntl.F_GETFL),
        'descriptor_flags': fcntl.fcntl(fd, fcntl.F_GETFD)}

def snapshot_fds(log_path):
    log = private_path(log_path)
    log_info = log.stat()
    null_info = Path('/dev/null').stat()
    # listdir returns a completed list. Its closed enumeration slot is omitted
    # only on EBADF. Any still-live extra descriptor, including a directory,
    # is rejected; no scanner/writer descriptor is held during this census.
    names = os.listdir('/proc/self/fd')
    observed = {}
    for name in names:
        try:
            observed[name] = fd_identity(int(name))
        except OSError as error:
            if error.errno != errno.EBADF:
                raise
    if set(observed) != {'0', '1', '2'}:
        raise ValueError('unknown_startup_descriptor')
    for name, identity in observed.items():
        expected = null_info if name == '0' else log_info
        if (identity['device'], identity['inode'], identity['mode'], identity['uid'], identity['nlink']) != (
                expected.st_dev, expected.st_ino, expected.st_mode, expected.st_uid, expected.st_nlink):
            raise ValueError('standard_descriptor_identity_changed')
        access = identity['open_flags'] & os.O_ACCMODE
        if access != (os.O_RDONLY if name == '0' else os.O_WRONLY):
            raise ValueError('standard_descriptor_access_changed')
        if name != '0' and not identity['open_flags'] & os.O_APPEND:
            raise ValueError('standard_log_not_append')
        if identity['descriptor_flags'] != 0:
            raise ValueError('standard_descriptor_flags_changed')
        target = os.readlink(f"/proc/self/fd/{name}")
        if target != ('/dev/null' if name == '0' else str(log)):
            raise ValueError('standard_descriptor_path_changed')
        if fd_identity(int(name)) != identity:
            raise ValueError('descriptor_changed_during_census')
    return observed

def filter_instructions():
    'Classic BPF with forward labels; full 64-bit sendto pointer check.\n\n    Layout/constants: linux/{filter,seccomp,bpf_common}.h and amd64 unistd_64.h.\n    Int socket arguments use their kernel-consumed low 32 bits. No pointed\n    sockaddr inspection is claimed. All non-native and x32 calls kill process.\n    '
    entries, labels = [], {}

    def label(name):
        if name in labels:
            raise ValueError('duplicate_filter_label')
        labels[name] = len(entries)

    def load(offset):
        entries.append((0x20, 0, 0, offset))  # LD|W|ABS

    def jump(destination):
        entries.append((0x05, 0, 0, destination))  # JMP|JA

    def compare(code, value, yes, no):
        entries.append((code, 0, 1, value))
        jump(yes)
        jump(no)

    load(4)  # seccomp_data.arch
    compare(0x15, 0xC000003E, 'native', 'kill')
    label('native')
    load(0)  # seccomp_data.nr
    compare(0x45, 0x40000000, 'kill', 'dispatch')  # JSET x32 bit
    label('dispatch')
    for number, target in [(41, 'socket'), (53, 'socketpair'), (44, 'sendto'), *[(n, 'deny') for n in POLICY['deny']]]:
        next_label = f"after-{number}"
        compare(0x15, number, target, next_label)
        label(next_label)
    jump('allow')

    label('socket')
    load(16)  # args[0], domain
    compare(0x15, 1, 'unix-stream', 'inet')
    label('inet')
    compare(0x15, 2, 'stream-type', 'inet6')
    label('inet6')
    compare(0x15, 10, 'stream-type', 'deny')
    label('stream-type')
    load(24)  # args[1], type
    entries.append((0x54, 0, 0, (~(0x800 | 0x80000)) & 0xFFFFFFFF))
    compare(0x15, 1, 'ip-protocol', 'deny')
    label('ip-protocol')
    load(32)
    compare(0x15, 0, 'allow', 'tcp-protocol')
    label('tcp-protocol')
    compare(0x15, 6, 'allow', 'deny')

    label('unix-stream')
    load(24)
    entries.append((0x54, 0, 0, (~(0x800 | 0x80000)) & 0xFFFFFFFF))
    compare(0x15, 1, 'zero-protocol', 'deny')

    label('socketpair')
    load(16)
    compare(0x15, 1, 'pair-type', 'deny')
    label('pair-type')
    load(24)
    entries.append((0x54, 0, 0, (~(0x800 | 0x80000)) & 0xFFFFFFFF))
    compare(0x15, 1, 'zero-protocol', 'pair-seqpacket')
    label('pair-seqpacket')
    compare(0x15, 5, 'zero-protocol', 'deny')
    label('zero-protocol')
    load(32)
    compare(0x15, 0, 'allow', 'deny')

    label('sendto')
    load(48)  # args[4] destination pointer, low 32 bits
    compare(0x15, 0, 'sendto-upper', 'deny')
    label('sendto-upper')
    load(52)
    compare(0x15, 0, 'allow', 'deny')

    label('allow')
    entries.append((0x06, 0, 0, 0x7FFF0000))
    label('deny')
    entries.append((0x06, 0, 0, 0x00050000 | errno.EPERM))
    label('kill')
    entries.append((0x06, 0, 0, 0x80000000))

    result = []
    for index, (code, jt, jf, value) in enumerate(entries):
        if isinstance(value, str):
            value = labels[value] - index - 1
            if value < 0:
                raise ValueError('backward_filter_jump')
        result.append((code, jt, jf, value))
    if len(result) > 4096:
        raise ValueError('filter_bound')
    return result


INSTALL_STAGE = 'not_started'
INSTALL_ERRNO = None


def install_filter():
    global INSTALL_STAGE, INSTALL_ERRNO
    INSTALL_STAGE, INSTALL_ERRNO = 'filter_construct', None
    class SockFilter(ctypes.Structure):
        _fields_ = [('code', ctypes.c_ushort), ('jt', ctypes.c_ubyte), ('jf', ctypes.c_ubyte), ('k', ctypes.c_uint)]

    class SockProgram(ctypes.Structure):
        _fields_ = [('length', ctypes.c_ushort), ('filters', ctypes.POINTER(SockFilter))]

    instructions = filter_instructions()
    filters = (SockFilter * len(instructions))(*(SockFilter(*row) for row in instructions))
    program = SockProgram(len(instructions), filters)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.restype = ctypes.c_int
    libc.syscall.restype = ctypes.c_long
    # PR_SET_NO_NEW_PRIVS=38. Explicit long arguments avoid ctypes int truncation.
    INSTALL_STAGE = 'filter_nnp'
    nnp_result = libc.prctl(38, ctypes.c_ulong(1), ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0))
    if nnp_result != 0:
        INSTALL_ERRNO = ctypes.get_errno() if nnp_result == -1 else None
        raise ValueError('no_new_privs_failed')
    # Native amd64 seccomp syscall317; SET_MODE_FILTER=1, FILTER_FLAG_TSYNC=1.
    # Positive TSYNC return is the unsynchronized TID and MUST also fail closed.
    INSTALL_STAGE = 'filter_tsync'
    tsync_result = libc.syscall(ctypes.c_long(317), ctypes.c_uint(1), ctypes.c_uint(1), ctypes.byref(program))
    if tsync_result != 0:
        INSTALL_ERRNO = ctypes.get_errno() if tsync_result == -1 else None
        raise ValueError('filter_tsync_failed')
    INSTALL_STAGE = 'filter_status'
    status = process_status(os.getpid())
    if status.get('NoNewPrivs', '').strip() != '1' or status.get('Seccomp', '').strip() != '2':
        raise ValueError('kernel_status_unavailable')
    return hashlib.sha256(canonical(instructions)).hexdigest()



def initialize(config):
    if (sys.platform != 'linux' or os.uname().machine != 'x86_64'
            or struct.calcsize('P') != 8 or sys.byteorder != 'little'):
        raise ValueError('unsupported_native_abi')
    if os.environ.get('SERAPH_LOCAL_SERVICE') != 'backend':
        raise ValueError('unmarked_backend')
    if 'src.app' in sys.modules or any(n.startswith('uvicorn') for n in sys.modules):
        raise ValueError('late_guard')
    if os.listdir('/proc/self/task') != [str(os.getpid())]:
        raise ValueError('startup_threads_unowned')
    if Path.cwd() != Path(config['root']) / 'backend' or str(Path(sys.executable)) != config['interpreter']:
        raise ValueError('backend_identity_changed')
    if sys.argv != config['backend_argv']:
        raise ValueError('backend_argv_changed')
    if Path('/proc/self/cmdline').read_bytes().rstrip(b"\0").split(b"\0") != [os.fsencode(v) for v in config['process_argv']]:
        raise ValueError('backend_process_argv_changed')
    startup = snapshot_fds(config['backend_log'])
    program = install_filter()
    receipt = {'version': 1, 'nonce': config['nonce'], 'pid': os.getpid(),
        'start_ticks': process_start(os.getpid()), 'cwd': str(Path.cwd()),
        'executable': str(Path(sys.executable)), 'argv': list(sys.argv),
        'process_argv': config['process_argv'], 'policy_sha256': hashlib.sha256(canonical(POLICY)).hexdigest(),
        'program_sha256': program, 'startup_fds': startup, 'source_pins': config['source_pins'],
        'cache_inventory': config['cache_inventory'], 'no_new_privs': 1, 'seccomp': 2,
        'app_imported': False, 'kernel_denials_tested': False}
    path = Path(config['guard_receipt'])
    private_path(path.parent, directory=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as output:
        output.write(canonical(receipt)); output.flush(); os.fsync(output.fileno())
    # No app import before independently checked parent ACK. This is a
    # cooperative owner gate, not resistance to hostile same-UID edits.
    cutoff = time.monotonic() + 10
    while time.monotonic() < cutoff:
        ack = Path(config['guard_ack'])
        if ack.exists():
            private_path(ack)
            value = json.loads(ack.read_bytes())
            if value != {'nonce': config['nonce'], 'pid': receipt['pid'],
                    'start_ticks': receipt['start_ticks'], 'program_sha256': program}:
                raise ValueError('guard_ack_changed')
            return receipt
        time.sleep(0.01)
    raise ValueError('guard_ack_timeout')
