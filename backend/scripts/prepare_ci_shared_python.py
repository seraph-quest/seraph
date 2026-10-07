"""CI-only preparation of Actions CPython's shared/stdlib runtime.

Loader packaging is a separate system-interpreter step with a new derived
executable identity. This step proves the actual selected library and preserves
the remaining runtime bytes. Product capture predicates remain unchanged.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import sysconfig
import time

VERSION = (3, 12, 15)
TOOLCACHE = Path('/opt/hostedtoolcache')
EXECUTABLE = ('bin/python3.12', 17752,
              '53dab24bf9e0e9c490088a53d71770b6f027b057678b313648e75f7fc87a675e')
SHARED = ('lib/libpython3.12.so.1.0', 29843656,
          'c7c1a2206db0fa089f8a252d1652d5ccb82669774dbb599427082ffc9eaaae85')
ARCHIVE = ('lib/python3.12/config-3.12-x86_64-linux-gnu/libpython3.12.a', 64406950,
           '4232dad323fc9bdadc4bb98b5e82a78200073dd861e3562ecf60bf8a23f69976')


def require(value, reason):
    if not value:
        raise RuntimeError(reason)


def identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode, info.st_uid, info.st_gid)


def same_record(left, right):
    return left is not None and identity(left[0]) == identity(right[0]) and left[1] == right[1]


class HeldDirectories:
    """Hold every directory from /; mutations use only selected-prefix FDs."""
    def __init__(self, prefix):
        require(prefix.is_absolute() and '..' not in prefix.parts, 'canonical prefix required')
        self.prefix = prefix
        self.entries = {}
        try:
            self.hold(Path('/'))
            self.hold(prefix)
            require(self.entries[prefix][1].st_uid == os.geteuid(), 'selected prefix owner mismatch')
        except BaseException:
            self.close()
            raise

    @staticmethod
    def directory_identity(info):
        return (info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode,
                info.st_mtime_ns, info.st_ctime_ns)

    def hold(self, path):
        if path in self.entries:
            self.check(path)
            return self.entries[path][0]
        require(len(self.entries) < 1000, 'directory count bound')
        parent_fd = self.hold(path.parent) if path != Path('/') else None
        fd = os.open(path.name if parent_fd is not None else '/',
                     os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, os.geteuid()}:
            os.close(fd)
            raise RuntimeError('foreign/non-directory parent')
        if path.is_relative_to(self.prefix) and info.st_mode & 0o7000:
            os.close(fd)
            raise RuntimeError('special active directory permissions')
        self.entries[path] = (fd, info)
        return fd

    def check(self, path):
        fd, expected = self.entries[path]
        current = os.fstat(fd)
        require(self.directory_identity(current) == self.directory_identity(expected),
                'held directory identity drift')
        if path != Path('/'):
            self.check(path.parent)
            parent_fd = self.entries[path.parent][0]
            live = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
            require(self.directory_identity(live) == self.directory_identity(expected),
                    'directory path identity drift')
        return current

    def open_file(self, path):
        require(path.is_relative_to(self.prefix), 'path outside selected prefix')
        parent_fd = self.hold(path.parent)
        self.check(path.parent)
        return os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)

    def close(self):
        for fd, info in self.entries.values():
            os.close(fd)
        self.entries.clear()


class RuntimePlan(dict):
    def __init__(self, records, tree):
        super().__init__(records)
        self.tree = tree


def no_linked_parents(path):
    # Compatibility helper for focused predicate tests; no permission shortcut.
    tree = HeldDirectories(path.parent)
    tree.close()


def read_regular(path, prefix, tree=None):
    require(path.is_relative_to(prefix), 'path outside selected prefix')
    owned_tree = tree is None
    tree = tree or HeldDirectories(prefix)
    try:
        descriptor = tree.open_file(path)
    except BaseException:
        if owned_tree:
            tree.close()
        raise
    try:
        before = os.fstat(descriptor)
        require(stat.S_ISREG(before.st_mode), 'not an ordinary file')
        require(before.st_uid == os.geteuid(), 'file not owned by provisioning user')
        require(before.st_nlink == 1, 'hardlinked file')
        require(before.st_size <= (128 if path == prefix / ARCHIVE[0] else 48) * 1024 * 1024, 'source file bound')
        require(not before.st_mode & 0o7000, 'special file permissions')
        digest = hashlib.sha256()
        read_bytes = 0
        deadline = time.monotonic() + 60
        while chunk := os.read(descriptor, 1024 * 1024):
            read_bytes += len(chunk)
            require(read_bytes <= before.st_size and time.monotonic() < deadline, 'bounded source read')
            digest.update(chunk)
        require(read_bytes == before.st_size, 'truncated source read')
        require(identity(before) == identity(os.fstat(descriptor)), 'file changed while read')
        return before, digest.hexdigest()
    finally:
        os.close(descriptor)
        if owned_tree:
            tree.close()


def selected_prefix():
    require(os.environ.get('GITHUB_ACTIONS') == 'true', 'GitHub Actions required')
    require(os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted', 'fresh hosted runner required')
    require(sys.platform == 'linux' and os.uname().machine == 'x86_64', 'Linux x64 required')
    require(tuple(sys.version_info[:3]) == VERSION, 'exact Python 3.12.15 required')
    require(os.environ.get('RUNNER_TOOL_CACHE') == str(TOOLCACHE), 'exact hosted toolcache required')
    prefix = TOOLCACHE / 'Python/3.12.15/x64'
    require(sys.base_prefix == str(prefix) == sys.prefix == os.environ.get('pythonLocation'),
            'selected base prefix and pythonLocation must agree; no venv/relocation')
    require(sys.executable == str(prefix / EXECUTABLE[0]), 'use exact canonical selected executable')
    require(sysconfig.get_config_var('Py_ENABLE_SHARED') == 1, 'genuine shared build required')
    require(sysconfig.get_config_var('LIBDIR') == str(prefix / 'lib'), 'LIBDIR mismatch')
    require(sysconfig.get_config_var('LDLIBRARY') == 'libpython3.12.so', 'LDLIBRARY mismatch')
    require(sysconfig.get_config_var('LIBPL') == str((prefix / ARCHIVE[0]).parent), 'LIBPL mismatch')
    require(sysconfig.get_config_var('LIBRARY') == Path(ARCHIVE[0]).name, 'LIBRARY mismatch')
    return prefix


def raise_walk_error(error):
    raise error


def plan(prefix, maps, executable_pin=None):
    """Complete unsupported-case checks before chmod/unlink is possible."""
    tree = HeldDirectories(prefix)
    try:
        return RuntimePlan(_plan(prefix, maps, tree, executable_pin), tree)
    except BaseException:
        tree.close()
        raise


def _plan(prefix, maps, tree, executable_pin=None, verify_loaded=True):
    libfd = tree.hold(prefix / 'lib')
    alias = os.stat('libpython3.12.so', dir_fd=libfd, follow_symlinks=False)
    require(stat.S_ISLNK(alias.st_mode) and alias.st_uid == os.geteuid()
            and os.readlink('libpython3.12.so', dir_fd=libfd) == Path(SHARED[0]).name,
            'genuine shared-library alias mismatch')
    tree.alias_identity = identity(alias)
    pinned = {}
    for relative, size, digest in (executable_pin or EXECUTABLE, SHARED, ARCHIVE):
        info, actual = read_regular(prefix / relative, prefix, tree)
        require(info.st_size == size and actual == digest, 'authenticated distribution bytes mismatch')
        pinned[relative] = (info, actual)
    library = pinned[SHARED[0]][0]
    device = f'{os.major(library.st_dev):02x}:{os.minor(library.st_dev):02x}'
    rows = [line.split(maxsplit=5) for line in maps.splitlines() if 'libpython' in line]
    require(not verify_loaded or rows and all(len(row) == 6 for row in rows), 'malformed/empty shared Python maps')
    require(not verify_loaded or any('x' in row[1] for row in rows), 'no actual loaded shared Python')
    require(not verify_loaded or all(len(row) == 6 and row[5] == str(prefix / SHARED[0])
                and row[3] == device and int(row[4]) == library.st_ino for row in rows),
            'loaded library inode/device/path mismatch')
    records = {relative: pinned[relative] for relative in (EXECUTABLE[0], SHARED[0])}
    stdlib = prefix / 'lib/python3.12'
    deadline = time.monotonic() + 60
    pending = [stdlib]
    while pending:
        directory = pending.pop()
        require(time.monotonic() < deadline, 'provisioning plan deadline')
        require(len(directory.relative_to(stdlib).parts) <= 16, 'stdlib depth bound')
        fd = tree.hold(directory)
        # scandir(FD) errors propagate. Excluded cache/site subtrees are neither
        # trusted nor mutated, exactly as in the existing product capture.
        with os.scandir(fd) as scan:
            entries = []
            for entry in scan:
                require(len(entries) < 8000 and time.monotonic() < deadline, 'directory entry/deadline bound')
                entries.append(entry)
        entries.sort(key=lambda entry: entry.name)
        for entry in entries:
            path = directory / entry.name
            info = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode) and entry.name in {'__pycache__', 'site-packages', 'dist-packages'}:
                continue
            if not stat.S_ISDIR(info.st_mode) and entry.name.endswith(('.pyc', '.pyo')):
                continue
            require(not stat.S_ISLNK(info.st_mode) and not entry.name.endswith('.pth'), 'linked/pth runtime entry')
            if stat.S_ISDIR(info.st_mode):
                pending.append(path)
            else:
                require(len(records) < 8000, 'runtime file count')
                records[str(path.relative_to(prefix))] = read_regular(path, prefix, tree)
    require(ARCHIVE[0] in records, 'missing declared static development archive')
    require([p for p in records if p.endswith('.a')] == [ARCHIVE[0]], 'unexpected static archive')
    for relative, expected in pinned.items():
        require(same_record(records.get(relative), expected), 'pinned identity changed during planning')
    return records


def apply_plan(prefix, records):
    tree = records.tree
    require(tree.prefix == prefix, 'plan prefix mismatch')
    # Revalidate the complete plan before the first mutation. The selected
    # CI-owned prefix must remain quiescent throughout this provisioning step.
    for relative, expected in records.items():
        require(same_record(read_regular(prefix / relative, prefix, tree), expected), 'prefix changed before mutation')
    tree.check(prefix / 'lib')
    libfd = tree.entries[prefix / 'lib'][0]
    require(identity(os.stat('libpython3.12.so', dir_fd=libfd, follow_symlinks=False)) == tree.alias_identity
            and os.readlink('libpython3.12.so', dir_fd=libfd) == Path(SHARED[0]).name,
            'shared-library alias identity drift')
    directory_modes = []
    for path in sorted(tree.entries, key=lambda item: len(item.parts)):
        if not path.is_relative_to(prefix):
            continue
        tree.check(path)
        fd, before = tree.entries[path]
        if before.st_mode & 0o022:
            require(before.st_uid == os.geteuid(), 'active directory not provisioning-owned')
    # All unsupported file and directory cases above have been checked before
    # the first mutation. External writable ancestors are never normalized.
    for path in sorted(tree.entries, key=lambda item: len(item.parts)):
        if not path.is_relative_to(prefix):
            continue
        tree.check(path)
        fd, before = tree.entries[path]
        if before.st_mode & 0o022:
            target_mode = stat.S_IMODE(before.st_mode) & ~0o022
            os.fchmod(fd, target_mode)
            after = os.fstat(fd)
            require(stat.S_IMODE(after.st_mode) == target_mode and not after.st_mode & 0o022
                    and (after.st_mode & 0o555) == (before.st_mode & 0o555)
                    and (after.st_dev, after.st_ino, after.st_uid, after.st_gid)
                    == (before.st_dev, before.st_ino, before.st_uid, before.st_gid),
                    'normalized active directory readback mismatch')
            tree.entries[path] = (fd, after)
            directory_modes.append({'path': str(path.relative_to(prefix)), 'before_mode': oct(stat.S_IMODE(before.st_mode)),
                'after_mode': oct(stat.S_IMODE(after.st_mode)), 'uid': before.st_uid, 'gid': before.st_gid,
                'device': before.st_dev, 'inode': before.st_ino})
    modes = []
    for relative, (before, digest) in records.items():
        if relative == ARCHIVE[0] or not before.st_mode & 0o022:
            continue
        path = prefix / relative
        descriptor = tree.open_file(path)
        try:
            require(identity(os.fstat(descriptor)) == identity(before), 'chmod target changed')
            os.fchmod(descriptor, stat.S_IMODE(before.st_mode) & ~0o022)
        finally:
            os.close(descriptor)
        modes.append({'path': relative, 'before_mode': oct(stat.S_IMODE(before.st_mode)),
                      'after_mode': oct(stat.S_IMODE(before.st_mode) & ~0o022), 'uid': before.st_uid, 'gid': before.st_gid,
                      'device': before.st_dev, 'inode': before.st_ino})
    archive = prefix / ARCHIVE[0]
    require(same_record(read_regular(archive, prefix, tree), records[ARCHIVE[0]]), 'archive identity changed')
    tree.check(archive.parent)
    os.unlink(archive.name, dir_fd=tree.entries[archive.parent][0])  # Pinned development archive only.
    parent_fd, parent_before = tree.entries[archive.parent]
    parent_after = os.fstat(parent_fd)
    require((parent_after.st_dev, parent_after.st_ino, parent_after.st_uid, parent_after.st_gid, parent_after.st_mode)
            == (parent_before.st_dev, parent_before.st_ino, parent_before.st_uid, parent_before.st_gid, parent_before.st_mode),
            'archive directory identity changed')
    tree.entries[archive.parent] = (parent_fd, parent_after)  # Our exact unlink changes directory timestamps.
    final_infos = {}
    for relative, (before, digest) in records.items():
        if relative == ARCHIVE[0]:
            continue
        after, actual = read_regular(prefix / relative, prefix, tree)
        require(actual == digest and after.st_size == before.st_size
                and stat.S_IMODE(after.st_mode) == (stat.S_IMODE(before.st_mode) & ~0o022)
                and (after.st_mode & 0o111) == (before.st_mode & 0o111)
                and not after.st_mode & 0o022, 'runtime content/executable bits changed')
        require(after.st_dev == before.st_dev and after.st_ino == before.st_ino
                and after.st_uid == before.st_uid and after.st_gid == before.st_gid,
                'runtime file identity changed')
        final_infos[relative] = after
    for row in modes:
        row['after_mode'] = oct(stat.S_IMODE(final_infos[row['path']].st_mode))
    for directory in tree.entries:
        current = tree.check(directory)
        if directory.is_relative_to(prefix):
            require(not current.st_mode & 0o022, 'active directory remains writable')
    external = []
    for directory in (Path('/'), TOOLCACHE.parent, TOOLCACHE, TOOLCACHE / 'Python', TOOLCACHE / 'Python/3.12.15'):
        if directory not in tree.entries or directory.is_relative_to(prefix):
            continue
        info = tree.entries[directory][1]
        after = tree.check(directory)
        external.append({'path': str(directory), 'uid': info.st_uid, 'gid': info.st_gid,
                         'device': info.st_dev, 'inode': info.st_ino,
                         'before_mode': oct(stat.S_IMODE(info.st_mode)),
                         'after_mode': oct(stat.S_IMODE(after.st_mode))})
    require(len(external) <= 6, 'external ancestor receipt bound')
    pins = []
    for relative in (EXECUTABLE[0], SHARED[0], ARCHIVE[0]):
        before, digest = records[relative]
        after = final_infos.get(relative)
        pins.append({'path': relative, 'sha256': digest, 'bytes': before.st_size,
                     'uid': before.st_uid, 'gid': before.st_gid, 'device': before.st_dev,
                     'inode': before.st_ino, 'before_mode': oct(stat.S_IMODE(before.st_mode)),
                     'after_mode': None if after is None else oct(stat.S_IMODE(after.st_mode))})
    return {'prefix': str(prefix), 'removed_static_development_archive': ARCHIVE[0],
        'selected_authenticated_files': pins,
        'verified_external_parents': external,
        'normalized_files': modes, 'normalized_active_directories': directory_modes,
        'outside_prefix_directories_mutated': False, 'same_owner_group_or_os_isolation_claim': False, 'runtime_content_preserved': True,
        'loaded_libpython_sha256': records[SHARED[0]][1],
        'remaining_files': len(records) - 1, 'claim': 'CI dependency preparation; capture still required'}


def prepare():
    prefix = selected_prefix()
    import importlib.util
    spec = importlib.util.spec_from_file_location('ci_loader', Path(__file__).with_name('prepare_ci_python_loader.py'))
    loader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loader)
    derivation = loader.read_receipt(prefix)
    records = plan(prefix, Path('/proc/self/maps').read_text(),
                   (EXECUTABLE[0], derivation['derived']['bytes'], derivation['derived']['sha256']))
    try:
        receipt = apply_plan(prefix, records)
        receipt.pop('runtime_content_preserved')
        receipt['shared_and_stdlib_content_preserved'] = True
        receipt['executable_loader_derivation'] = derivation
        print(json.dumps(receipt))
    finally:
        records.tree.close()


if __name__ == '__main__':
    prepare()
