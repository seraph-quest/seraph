"""Fixed-path hosted Ubuntu CI mirror priority preparation; never runs APT.

Expected input bytes/hash are derived from runner-image ubuntu24/20261004.327,
not measured historical runner bytes. Unsupported live inputs fail closed.
This is a disposable-job publication operation, not OS isolation or rollback.
"""
import hashlib
import json
import os
from pathlib import Path
import platform
import stat
import sys
import time
import uuid

# Fixture tests replace only these metadata readers to simulate root ownership;
# production readers are the ordinary OS calls, with no environment override.
_fstat = os.fstat
_stat = os.stat

APT = Path('/etc/apt')
MIRROR = 'apt-mirrors.txt'
DEB822 = 'sources.list.d/ubuntu.sources'
URI = 'mirror+file:/etc/apt/apt-mirrors.txt'
ORIGINAL = (b'http://azure.archive.ubuntu.com/ubuntu/\tpriority:1\n'
            b'https://archive.ubuntu.com/ubuntu/\tpriority:2\n'
            b'https://security.ubuntu.com/ubuntu/\tpriority:3\n')
ORIGINAL_SHA256 = 'e0d6b0af979e4662d16357a27ae456cc6f21e26da031540f4df8706ebed1d583'
REPLACEMENT = (b'https://archive.ubuntu.com/ubuntu/\tpriority:1\n'
               b'https://security.ubuntu.com/ubuntu/\tpriority:2\n'
               b'http://azure.archive.ubuntu.com/ubuntu/\tpriority:3\n')


def require(value, reason):
    if not value:
        raise RuntimeError(reason)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode, info.st_uid, info.st_gid, info.st_nlink)


def directory_identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_mtime_ns, info.st_ctime_ns)


class HeldDirectories:
    """Hold nofollow parents of the two fixed files; no generic OS mutation."""
    def __init__(self, apt):
        require(apt.is_absolute() and '..' not in apt.parts, 'absolute ordinary APT directory required')
        self.entries = {}
        try:
            self.hold(apt)
            self.hold(apt / 'sources.list.d')
        except BaseException:
            self.close()
            raise

    def hold(self, path):
        if path in self.entries:
            return self.entries[path][0]
        require(len(self.entries) < 32, 'directory depth bound')
        parent = None if path == Path('/') else self.hold(path.parent)
        fd = os.open('/' if parent is None else path.name,
                     os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        info = _fstat(fd)
        if not (stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o7022):
            os.close(fd)
            raise RuntimeError('unsafe root-owned APT parent')
        self.entries[path] = (fd, info)
        return fd

    def check(self, path):
        fd, expected = self.entries[path]
        require(directory_identity(_fstat(fd)) == directory_identity(expected), 'directory identity drift')
        if path != Path('/'):
            self.check(path.parent)
            live = _stat(path.name, dir_fd=self.entries[path.parent][0], follow_symlinks=False)
            require(directory_identity(live) == directory_identity(expected), 'directory path drift')

    def own_change(self, path):
        """Our stage/rename updates directory timestamps, never its identity."""
        fd, before = self.entries[path]
        after = _fstat(fd)
        require(directory_identity(before)[:5] == directory_identity(after)[:5], 'directory changed during publication')
        self.entries[path] = fd, after
        self.check(path)

    def close(self):
        for fd, _ in self.entries.values():
            os.close(fd)
        self.entries.clear()


def read_file(tree, path, limit):
    tree.check(path.parent)
    fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                 dir_fd=tree.entries[path.parent][0])
    try:
        before = _fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_uid == 0 and before.st_nlink == 1
                and not before.st_mode & 0o7022 and 0 <= before.st_size <= limit, 'unsafe root-owned ordinary file')
        data = bytearray()
        deadline = time.monotonic() + 5
        while chunk := os.read(fd, 4096):
            data.extend(chunk)
            require(len(data) <= before.st_size and time.monotonic() < deadline, 'file read bound')
        require(len(data) == before.st_size and identity(_fstat(fd)) == identity(before), 'file read drift')
        live = _stat(path.name, dir_fd=tree.entries[path.parent][0], follow_symlinks=False)
        require(identity(live) == identity(before), 'file path drift')
        return before, bytes(data)
    finally:
        os.close(fd)


def validate_sources(data):
    try:
        text = data.decode('utf-8')
    except UnicodeError as error:
        raise RuntimeError('unsupported Ubuntu Deb822 bytes') from error
    uris = []
    uri_field = False
    for line in text.splitlines():
        if not line:
            uri_field = False
            continue
        if line.startswith('#'):
            continue
        require(not (uri_field and line[0].isspace()), 'unsupported continued Ubuntu URIs')
        field, separator, value = line.partition(':')
        uri_field = bool(separator and field.lower() == 'uris')
        if uri_field:
            require(field == 'URIs', 'unsupported Ubuntu URI field shape')
            uris.append(value.strip())
    require(len(uris) == 2 and uris == [URI, URI], 'unsupported Ubuntu Deb822 mirror sources')


def publish(apt):
    """Fixture API may use synthetic owned directories; CLI always fixes APT."""
    tree = HeldDirectories(apt)
    stage = None
    stage_info = None
    try:
        original_info, original = read_file(tree, apt / MIRROR, 144)
        require(len(original) == 144 and original == ORIGINAL and digest(original) == ORIGINAL_SHA256,
                'unsupported source-derived mirror input')
        sources_info, sources = read_file(tree, apt / DEB822, 65536)
        validate_sources(sources)
        before_receipt = {'path': str(apt / MIRROR), 'sha256': digest(original), 'bytes': len(original),
                          'mode': oct(stat.S_IMODE(original_info.st_mode)),
                          'uid': original_info.st_uid, 'gid': original_info.st_gid}
        print(json.dumps({'phase': 'authenticated_live_input', 'mirror': before_receipt,
                          'deb822_sha256': digest(sources), 'expected_input_provenance': 'tagged runner-image source'}), flush=True)
        stage = '.seraph-apt-' + uuid.uuid4().hex + '.tmp'
        tree.check(apt)
        fd = os.open(stage, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=tree.entries[apt][0])
        stage_info = _fstat(fd)
        try:
            require(stat.S_ISREG(stage_info.st_mode) and stage_info.st_uid == 0
                    and stage_info.st_nlink == 1, 'unsafe mirror stage')
            tree.own_change(apt)
            data = memoryview(REPLACEMENT)
            while data:
                count = os.write(fd, data)
                require(count > 0, 'stage write failure')
                data = data[count:]
            os.fchown(fd, original_info.st_uid, original_info.st_gid)
            os.fchmod(fd, stat.S_IMODE(original_info.st_mode))
            os.fsync(fd)
            stage_info = _fstat(fd)
        finally:
            os.close(fd)
        checked_stage, staged = read_file(tree, apt / stage, 144)
        require(identity(checked_stage) == identity(stage_info) and staged == REPLACEMENT
                and (checked_stage.st_uid, checked_stage.st_gid, stat.S_IMODE(checked_stage.st_mode))
                == (original_info.st_uid, original_info.st_gid, stat.S_IMODE(original_info.st_mode)),
                'staged mirror readback mismatch')
        current_info, current = read_file(tree, apt / MIRROR, 144)
        deb_info, deb = read_file(tree, apt / DEB822, 65536)
        require(identity(current_info) == identity(original_info) and current == original,
                'original mirror drift before publication')
        require(identity(deb_info) == identity(sources_info) and deb == sources, 'Deb822 drift before publication')
        tree.check(apt / 'sources.list.d')
        tree.check(apt)
        os.replace(stage, MIRROR, src_dir_fd=tree.entries[apt][0], dst_dir_fd=tree.entries[apt][0])
        stage = None
        tree.own_change(apt)
        os.fsync(tree.entries[apt][0])
        published_info, published = read_file(tree, apt / MIRROR, 144)
        # Atomic rename may change ctime; retain every other staged identity field.
        require(identity(published_info)[:4] == identity(stage_info)[:4]
                and identity(published_info)[5:] == identity(stage_info)[5:]
                and published == REPLACEMENT, 'published mirror readback mismatch')
        final_sources_info, final_sources = read_file(tree, apt / DEB822, 65536)
        require(identity(final_sources_info) == identity(sources_info) and final_sources == sources,
                'Deb822 drift after publication')
        result = {'phase': 'mirror_priority_published', 'before': before_receipt,
                  'after': {'path': str(apt / MIRROR), 'sha256': digest(published), 'bytes': len(published),
                            'mode': oct(stat.S_IMODE(published_info.st_mode)),
                            'uid': published_info.st_uid, 'gid': published_info.st_gid},
                  'deb822_sha256': digest(final_sources), 'deb822_content_unchanged': True,
                  'mirrors': REPLACEMENT.decode().splitlines(), 'rollback_claim': False}
        print(json.dumps(result), flush=True)
        return result
    finally:
        # Never unlink an unexpected replacement, and never roll back published
        # state. A later verification error aborts the ephemeral setup step.
        if stage is not None and stage_info is not None:
            try:
                live = _stat(stage, dir_fd=tree.entries[apt][0], follow_symlinks=False)
                if (live.st_dev, live.st_ino, live.st_uid) == (stage_info.st_dev, stage_info.st_ino, 0):
                    os.unlink(stage, dir_fd=tree.entries[apt][0])
            except FileNotFoundError:
                pass
        tree.close()


def main():
    require(len(sys.argv) == 1 and os.geteuid() == 0, 'root fixed-path CLI required')
    require(sys.platform == 'linux' and os.uname().machine == 'x86_64'
            and sys.flags.isolated and sys.flags.no_site and sys.flags.dont_write_bytecode,
            'isolated Linux x64 system interpreter required')
    require(os.environ.get('GITHUB_ACTIONS') == 'true'
            and os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted'
            and os.environ.get('ImageOS') == 'ubuntu24', 'GitHub hosted Ubuntu24 image required')
    release = platform.freedesktop_os_release()
    require(release.get('ID') == 'ubuntu' and release.get('VERSION_ID') == '24.04', 'Ubuntu24.04 required')
    publish(APT)


if __name__ == '__main__':
    main()
