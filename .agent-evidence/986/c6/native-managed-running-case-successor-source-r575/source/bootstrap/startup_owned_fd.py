'One certified cached-interpreter resource; never a descriptor-number grant.\n\nNo opens of the sysfs resource, closes, reads of its contents, or child transfers.\nThe existing role/receipt full-frontier equality remains mandatory.\n'
import errno
import fcntl
import hashlib
import os
from pathlib import Path
import stat
import sys

PYTHON = Path('/home/pawel/.local/share/uv/python/cpython-3.12.8-linux-x86_64-gnu/bin/python3.12')
PYTHON_SHA = '0dc3a692fa85fcdb7f1a5877d2adf179809ac417a07ffde2373c832863800a15'
TARGET = '/sys/kernel/mm/transparent_hugepage/enabled'
# Root actual R100 failure and R102 before/after, not a guessed future inode.
IDENTITY = {'device': 25, 'inode': 2846, 'mode': 33188, 'uid': 0,
            'nlink': 1, 'open_flags': 32768, 'descriptor_flags': 0}
CERTIFIED_TRACE_SHA = 'f29c3f6a3de57d6907181a806dbf5d9a35af6c1e00e548fa7fce31b5ffa1766b'
CERTIFIED_SNAPSHOT_SHA = 'b9b07fd7b1e6a3738dc11e6e5237845c6e03c768ff4ab11de5557470b16cab21'


def certify_snapshot(snapshot, standard):
    'Pure finite predicate; full stdio identity and exactly one extra resource.'
    if set(standard) != {'0', '1', '2'}:
        raise RuntimeError('startup_owned_stdio_shape')
    if not all(snapshot.get(key, {}).get('identity') == value for key, value in standard.items()):
        raise RuntimeError('startup_owned_stdio_drift')
    extra = set(snapshot) - set(standard)
    if len(extra) != 1:
        raise RuntimeError('startup_owned_extra_frontier')
    name = next(iter(extra))
    item = snapshot[name]
    if (not name.isdecimal() or int(name) < 3 or item.get('target') != TARGET
            or item.get('identity') != IDENTITY or item.get('stable') is not True
            or not stat.S_ISREG(IDENTITY['mode'])
            or IDENTITY['open_flags'] & os.O_ACCMODE != os.O_RDONLY):
        raise RuntimeError('startup_owned_resource_drift')
    return {name: dict(IDENTITY)}


def inspect_runtime():
    if (Path(sys.executable).resolve(strict=True) != PYTHON
            or hashlib.sha256(PYTHON.read_bytes()).hexdigest() != PYTHON_SHA):
        raise RuntimeError('startup_owned_interpreter_drift')
    snapshot = {}
    names = os.listdir('/proc/self/fd')
    if len(names) > 128:
        raise RuntimeError('startup_owned_frontier_bound')
    for name in names:
        try:
            fd = int(name)
            before = os.fstat(fd)
            target = os.readlink('/proc/self/fd/' + name)
            flags, fdflags = fcntl.fcntl(fd, fcntl.F_GETFL), fcntl.fcntl(fd, fcntl.F_GETFD)
            after = os.fstat(fd)
            stable = (before == after and target == os.readlink('/proc/self/fd/' + name)
                      and flags == fcntl.fcntl(fd, fcntl.F_GETFL)
                      and fdflags == fcntl.fcntl(fd, fcntl.F_GETFD))
            snapshot[name] = {'target': target, 'stable': stable,
                'identity': {'device': before.st_dev, 'inode': before.st_ino,
                    'mode': before.st_mode, 'uid': before.st_uid, 'nlink': before.st_nlink,
                    'open_flags': flags, 'descriptor_flags': fdflags}}
        except OSError as error:
            if error.errno != errno.EBADF:
                raise
    return snapshot


def project_prepared_frontier(snapshot, retained):
    "Exclude only the existing seal's exact three owned preparation handles.\n\n    Original Popen close_fds and standard redirections remove these handles.\n    R102 shows cached interpreter startup acquires the first free slot3 after\n    exec. Parent runtime slot is NOT projected. The future child must validate\n    exact target/identity and full sealed frontier before application imports.\n    "
    if (len(retained) != 3 or any(not name.isdecimal() or int(name) < 3
                                for name in retained)):
        raise RuntimeError('startup_owned_retained_shape')
    for name, expected in retained.items():
        if snapshot.get(name) != expected or expected.get('stable') is not True:
            raise RuntimeError('startup_owned_retained_drift')
    # Trusted controller legitimately retains listeners, pidfds and esbuild
    # stderr. These are NOT child permissions and are never transferred:
    # original Popen close_fds=True and no pass_fds map only owned stdio.
    # Certify one unique runtime resource here; full closure belongs to child.
    resources = {key: item for key, item in snapshot.items()
                 if item.get('target') == TARGET}
    if len(resources) != 1 or next(iter(resources)) in retained:
        raise RuntimeError('startup_owned_resource_not_unique')
    standard = {key: snapshot[key]['identity'] for key in ('0', '1', '2')}
    certify_snapshot(dict({key: snapshot[key] for key in standard}, **resources), standard)
    return {'3': dict(IDENTITY)}


def verify_role(observed, expected):
    standard = {key: expected[key] for key in ('0', '1', '2')}
    extra = certify_snapshot(inspect_runtime(), standard)
    if expected != dict(standard, **extra) or observed != expected:
        raise RuntimeError('startup_owned_complete_frontier_drift')
    return extra
