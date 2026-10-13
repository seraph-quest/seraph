'Bounded exec-descendant negative probe; never run before explicit release.\n\nOnly supplied fixture-owned loopback tuples and a fixture-owned Unix socket.\nIt tests inherited denial, not a substitute for actual original-child readback.\n'
import ctypes
import errno
import json
import os
from pathlib import Path
import socket
import sys


def main():
    manifest = Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/managed-execution/probe-endpoints.json')
    info = manifest.lstat()
    if manifest.resolve() != manifest or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError('unowned_probe_endpoint_manifest')
    endpoints = json.loads(manifest.read_text())
    unix_path = str(manifest.parent / 'dummy-provider.sock')
    expected = [[2, '127.0.0.1', 18241], [10, '::1', 18242], [1, unix_path]]
    if [item['tuple'] for item in endpoints] != expected:
        raise RuntimeError('nonfixture_probe_endpoint')
    for endpoint in endpoints:
        pid = endpoint['pid']
        ticks = int(Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19])
        fd_path = Path(f"/proc/{pid}/fd/{endpoint['fd']}")
        owned = fd_path.stat()
        if (ticks != endpoint['start'] or owned.st_uid != os.getuid()
                or [owned.st_dev, owned.st_ino] != endpoint['socket_identity']):
            raise RuntimeError('probe_listener_owner_drift')
        if endpoint['tuple'][0] in {2, 10}:
            name = 'tcp' if endpoint['tuple'][0] == 2 else 'tcp6'
            address = ('0100007F' if name == 'tcp' else '00000000000000000000000001000000') + ':' + format(endpoint['tuple'][2], '04X')
            matches = [line.split() for line in Path('/proc/net/' + name).read_text().splitlines()[1:]]
            if not any(row[1] == address and row[3] == '0A' and row[9] == str(owned.st_ino) for row in matches):
                raise RuntimeError('probe_exact_listener_unproved')
        else:
            matches = [line.split() for line in Path('/proc/net/unix').read_text().splitlines()[1:]]
            if not any(len(row) == 8 and row[6] == str(owned.st_ino) and row[7] == unix_path for row in matches):
                raise RuntimeError('probe_exact_unix_listener_unproved')
    checks = []
    for family, address in [(socket.AF_INET, ('127.0.0.1', 18241)),
                            (socket.AF_INET6, ('::1', 18242)),
                            (socket.AF_UNIX, unix_path)]:
        with socket.socket(family, socket.SOCK_STREAM) as client:
            try:
                client.connect(address)
            except PermissionError as error:
                assert error.errno == errno.EPERM
            else:
                raise RuntimeError('inherited_connect_denial_missing')
        checks.append('python_connect_' + str(family))
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    # amd64 only, consistent with reviewed guard. Non-null addressed send uses
    # invalid FD deliberately: seccomp must deny before descriptor validation.
    if os.uname().machine != 'x86_64':
        raise RuntimeError('unsupported_probe_architecture')
    for name, number, args in [('raw_socket', 41, (2, 3, 0)),
            ('raw_connect', 42, (-1, 0, 0)),
            ('addressed_sendto', 44, (-1, 0, 0, 0, 1, 16)),
            ('sendmsg', 46, (-1, 0, 0)), ('recvmsg', 47, (-1, 0, 0)),
            ('recvmmsg', 299, (-1, 0, 0, 0, 0)),
            ('sendmmsg', 307, (-1, 0, 0, 0)),
            ('io_uring_setup', 425, (0, 0)),
            ('io_uring_enter', 426, (-1, 0, 0, 0, 0, 0)),
            ('io_uring_register', 427, (-1, 0, 0, 0))]:
        ctypes.set_errno(0)
        result = libc.syscall(ctypes.c_long(number), *[ctypes.c_long(v) for v in args])
        if result != -1 or ctypes.get_errno() != errno.EPERM:
            raise RuntimeError('raw_denial_missing:' + name)
        checks.append(name)
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    if status['NoNewPrivs'].strip() != '1' or status['Seccomp'].strip() != '2':
        raise RuntimeError('inherited_seccomp_missing')
    start = int(Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19])
    print(json.dumps({'pid': os.getpid(), 'start': start, 'checks': checks,
                      'NoNewPrivs': 1, 'Seccomp': 2}, sort_keys=True))


if __name__ == '__main__':
    main()
