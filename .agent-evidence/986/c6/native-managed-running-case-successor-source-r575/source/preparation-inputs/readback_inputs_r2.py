'Full destination byte/link readback, no application imports or DB reads.'
import hashlib
import json
import os
from pathlib import Path
import stat

OUT = Path(__file__).resolve().parent
FIXTURE = Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/managed-execution')


def main():
    manifest = json.loads((OUT / 'compiled-manifest-r2.json').read_bytes())
    observed = {}
    for name, record in manifest['files'].items():
        path = FIXTURE / name
        info = path.lstat()
        if path.resolve(strict=True) != path or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != record['mode']:
            raise ValueError('copied_member_identity_or_mode_changed')
        raw = path.read_bytes()
        after = path.stat()
        if hashlib.sha256(raw).hexdigest() != record['sha256'] or any(getattr(info, k) != getattr(after, k)
                for k in ('st_dev', 'st_ino', 'st_size', 'st_mode', 'st_uid', 'st_nlink', 'st_mtime_ns', 'st_ctime_ns')):
            raise ValueError('copied_member_bytes_changed')
        observed[name] = {'sha256': record['sha256'], 'device': info.st_dev, 'inode': info.st_ino,
                          'mode': info.st_mode, 'uid': info.st_uid, 'nlink': info.st_nlink}
    expected_backend = set(manifest['final_backend_map'])
    actual_backend = set()
    actual_links = set()
    backend = FIXTURE / 'source/backend'
    for directory, dirs, names in os.walk(backend, followlinks=False):
        for name in names + dirs:
            path = Path(directory) / name
            if path.is_symlink():
                actual_links.add('source/backend/' + str(path.relative_to(backend)))
            elif path.is_file():
                actual_backend.add(str(path.relative_to(backend)))
    if actual_backend != expected_backend:
        raise ValueError('complete_augmented_backend_frontier_changed')
    if actual_links != {name for name in manifest['links'] if name.startswith('source/backend/')}:
        raise ValueError('complete_backend_symlink_frontier_changed')
    for name, record in manifest['links'].items():
        path = FIXTURE / name
        if not path.is_symlink() or os.readlink(path) != record['target']:
            raise ValueError('literal_link_readback_changed')
    generated = {}
    for name in ['controller.log', 'source/logs/seraph-local-backend.log']:
        path = FIXTURE / name
        before = path.lstat()
        if (path.resolve(strict=True) != path or not stat.S_ISREG(before.st_mode)
                or before.st_uid != os.getuid() or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o600 or before.st_size != 0):
            raise ValueError('fresh_private_empty_diagnostic_log_changed')
        if path.read_bytes() != b'':
            raise ValueError('diagnostic_log_nonempty_before_launch')
        after = path.lstat()
        if any(getattr(before, key) != getattr(after, key) for key in
               ('st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns')):
            raise ValueError('diagnostic_log_changed_during_readback')
        generated[name] = {'device': before.st_dev, 'inode': before.st_ino, 'mode': before.st_mode,
                           'uid': before.st_uid, 'nlink': before.st_nlink, 'size': before.st_size}
    failures = FIXTURE / 'journey/startup-failures'
    info = failures.lstat()
    if (failures.resolve(strict=True) != failures or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700 or list(failures.iterdir())):
        raise ValueError('fresh_private_empty_failure_directory_changed')
    generated['journey/startup-failures'] = {'device': info.st_dev, 'inode': info.st_ino,
        'mode': info.st_mode, 'uid': info.st_uid, 'entries': 0}
    fd = os.open(OUT / 'destination-readback-r2.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump({'execution_authorized': False, 'files': observed,
                   'backend_regular_members': len(expected_backend), 'diagnostic_generated': generated}, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())


if __name__ == '__main__':
    main()
