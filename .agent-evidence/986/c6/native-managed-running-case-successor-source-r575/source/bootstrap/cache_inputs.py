'Typed immutable readonly cache inputs; private outputs stay unique-inode.'
import hashlib
import os
import re
from pathlib import Path
import stat

ROOTS = tuple(Path(value) for value in (
    '/home/pawel/repos/seraph/frontend/node_modules',
    '/home/pawel/repos/seraph/.agent-worktrees/986-a1-descriptor-projection/.agent-evidence/986/accepted-cache-r174/.venv',
    '/home/pawel/.local/share/uv/python/cpython-3.12.8-linux-x86_64-gnu',
    '/home/pawel/.cache/ms-playwright/chromium-1208',
    '/home/pawel/.cache/ms-playwright/chromium_headless_shell-1208',
    '/home/pawel/repos/seraph/.agent-worktrees/986-a1-host/.agent-evidence/986/a1-upstream/node-v24.13.1-linux-x64'))

# The actual original manager reaches these two canonical hardlinked system
# files through /usr/bin. They are readonly inputs, never private outputs.
# This is deliberately a two-file exception, not a system directory root.
SYSTEM_EXECUTABLES = {
    Path('/usr/lib/cargo/bin/coreutils/nohup'):
        'f84e76ea4da1e0c4e92254bd0d7dccdabe2c8b3098e734f27997dae3ee5d8e02',
    Path('/usr/lib/cargo/bin/coreutils/timeout'):
        'f84e76ea4da1e0c4e92254bd0d7dccdabe2c8b3098e734f27997dae3ee5d8e02',
}


def read_cache(path, record):
    path = Path(path)
    if re.search('(?i)\\.(db|sqlite|sqlite3)(-(wal|shm|journal))?$|-(wal|shm|journal)$', path.name):
        raise ValueError('database_cache_frontier_forbidden')
    if (set(record) != {'sha256', 'mode', 'uid', 'nlink', 'device', 'inode'}
            or not path.is_absolute() or path.resolve(strict=True) != path
            or not (any(root in path.parents for root in ROOTS) or path in SYSTEM_EXECUTABLES)):
        raise ValueError('typed_literal_readonly_cache_record_required')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        actual = {'mode': before.st_mode, 'uid': before.st_uid, 'nlink': before.st_nlink,
                  'device': before.st_dev, 'inode': before.st_ino}
        if not stat.S_ISREG(before.st_mode) or before.st_nlink < 1 or actual != {key: record[key] for key in actual}:
            raise ValueError('readonly_cache_metadata_changed')
        if path in SYSTEM_EXECUTABLES and (before.st_uid != 0 or before.st_mode & 0o022
                or not before.st_mode & 0o111 or record['sha256'] != SYSTEM_EXECUTABLES[path]):
            raise ValueError('exact_trusted_readonly_system_executable_required')
        chunks = []
        while True:
            block = os.read(fd, 1048576)
            if not block:
                break
            chunks.append(block)
        after = os.fstat(fd)
        raw = b''.join(chunks)
        if (len(raw) != before.st_size or hashlib.sha256(raw).hexdigest() != record['sha256']
                or any(getattr(before, name) != getattr(after, name) for name in
                       ('st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns'))):
            raise ValueError('readonly_cache_hash_or_identity_changed')
        return raw
    finally:
        os.close(fd)
