"""Synthetic file-publication fixtures; no /etc, root CLI, APT or network."""
import importlib.util
import os
from pathlib import Path
import stat

import pytest

spec = importlib.util.spec_from_file_location('ci_apt', Path(__file__).resolve().parents[1] / 'scripts/prepare_ci_apt_mirrors.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class FakeRoot:
    """Explicit root fixture metadata; synthetic APT modes remain real."""
    def __init__(self, original, ancestors=()):
        self.original = original
        self.st_uid = self.st_gid = 0
        if (original.st_dev, original.st_ino) in ancestors:
            self.st_mode = stat.S_IFDIR | 0o755
    def __getattr__(self, name):
        return getattr(self.original, name)


@pytest.fixture
def apt(monkeypatch, tmp_path):
    directory = tmp_path / 'apt'
    directory.mkdir(mode=0o755)
    (directory / 'sources.list.d').mkdir(mode=0o755)
    (directory / helper.MIRROR).write_bytes(helper.ORIGINAL)
    (directory / helper.MIRROR).chmod(0o644)
    (directory / helper.DEB822).write_text(
        'Types: deb\nURIs: ' + helper.URI + '\nSuites: noble noble-updates\nComponents: main\n'
        'Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n\nTypes: deb\nURIs: '
        + helper.URI + '\nSuites: noble-security\nComponents: main\n'
        'Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n')
    (directory / helper.DEB822).chmod(0o644)
    # Model the hosted root-owned 0755 ancestry above our synthetic tree;
    # do not change host permissions or mask unsafe modes inside the fixture.
    ancestors = {(path.stat().st_dev, path.stat().st_ino) for path in directory.parents}
    monkeypatch.setattr(helper, '_fstat', lambda fd: FakeRoot(os.fstat(fd), ancestors))
    monkeypatch.setattr(helper, '_stat', lambda *a, **kw: FakeRoot(os.stat(*a, **kw), ancestors))
    monkeypatch.setattr(helper.os, 'fchown', lambda fd, uid, gid: None if (uid, gid) == (0, 0) else pytest.fail('unexpected chown'))
    return directory


def mirror_state(apt):
    path = apt / helper.MIRROR
    info = path.stat()
    return path.read_bytes(), info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode)


def test_fixed_bytes_match_source_derived_input_and_same_urls():
    assert len(helper.ORIGINAL) == len(helper.REPLACEMENT) == 144
    assert helper.digest(helper.ORIGINAL) == helper.ORIGINAL_SHA256
    assert set(line.split(b'\t')[0] for line in helper.ORIGINAL.splitlines()) == set(
        line.split(b'\t')[0] for line in helper.REPLACEMENT.splitlines())
    assert helper.REPLACEMENT.splitlines()[0] == b'https://archive.ubuntu.com/ubuntu/\tpriority:1'


def test_publish_changes_only_mirror_order_and_preserves_deb822(apt):
    before = mirror_state(apt)
    sources = (apt / helper.DEB822).read_bytes()
    result = helper.publish(apt)
    after = mirror_state(apt)
    assert after[0] == helper.REPLACEMENT and after[1] == before[1] and after[2] != before[2]
    assert after[3] == before[3] == 0o644
    assert (apt / helper.DEB822).read_bytes() == sources
    assert result['deb822_content_unchanged'] is True and result['rollback_claim'] is False
    assert result['after']['sha256'] == helper.digest(helper.REPLACEMENT)
    assert not list(apt.glob('.seraph-apt-*'))


@pytest.mark.parametrize('case', ['bytes', 'mode', 'hardlink', 'symlink', 'deb_uri', 'deb_mode', 'deb_link', 'parent_mode',
                                  'deb_continued_uri', 'deb_mixedcase_uri', 'deb_comment_continued_uri'])
def test_unsupported_preflight_never_creates_stage_or_mutates_original(apt, case, monkeypatch):
    path = apt / helper.MIRROR
    if case == 'bytes': path.write_bytes(helper.ORIGINAL.replace(b'priority:1', b'priority:4'))
    elif case == 'mode': path.chmod(0o666)
    elif case == 'hardlink': os.link(path, apt / 'other')
    elif case == 'symlink':
        original = apt / 'actual'; path.rename(original); path.symlink_to(original.name)
    elif case == 'deb_uri': (apt / helper.DEB822).write_text('URIs: https://unexpected/\n')
    elif case == 'deb_mode': (apt / helper.DEB822).chmod(0o666)
    elif case == 'deb_link':
        source = apt / helper.DEB822; real = source.with_name('actual'); source.rename(real); source.symlink_to(real.name)
    elif case == 'deb_continued_uri':
        (apt / helper.DEB822).write_text('URIs: ' + helper.URI + '\n https://unexpected/\n\nURIs: ' + helper.URI + '\n')
    elif case == 'deb_mixedcase_uri':
        (apt / helper.DEB822).write_text('URIs: ' + helper.URI + '\nURIs: ' + helper.URI + '\nuRiS: https://unexpected/\n')
    elif case == 'deb_comment_continued_uri':
        (apt / helper.DEB822).write_text('URIs: ' + helper.URI + '\n# field comment\n https://unexpected/\n\nURIs: ' + helper.URI + '\n')
    else: apt.chmod(0o777)
    before = mirror_state(apt)
    with pytest.raises((RuntimeError, OSError)):
        helper.publish(apt)
    assert mirror_state(apt) == before
    assert not list(apt.glob('.seraph-apt-*'))


def test_foreign_file_owner_rejected_before_mutation(apt, monkeypatch):
    before = mirror_state(apt)
    original_fstat = helper._fstat
    def foreign(fd):
        info = original_fstat(fd)
        if (info.st_dev, info.st_ino) == before[1:3]:
            info.st_uid = 42
        return info
    monkeypatch.setattr(helper, '_fstat', foreign)
    with pytest.raises(RuntimeError, match='root-owned ordinary'):
        helper.publish(apt)
    assert mirror_state(apt) == before
    assert not list(apt.glob('.seraph-apt-*'))


@pytest.mark.parametrize('case', ['write_error', 'fsync_error', 'stage_bytes', 'stage_swap',
                                  'original_drift', 'deb_drift', 'rename_error'])
def test_prepublication_failure_cleans_only_owned_stage(apt, monkeypatch, case):
    before = mirror_state(apt)
    real_read = helper.read_file
    real_replace = helper.os.replace
    if case == 'write_error':
        monkeypatch.setattr(helper.os, 'write', lambda *a: (_ for _ in ()).throw(OSError('fixture write failure')))
    elif case == 'fsync_error':
        monkeypatch.setattr(helper.os, 'fsync', lambda *a: (_ for _ in ()).throw(OSError('fixture fsync failure')))
    elif case == 'rename_error':
        monkeypatch.setattr(helper.os, 'replace', lambda *a, **kw: (_ for _ in ()).throw(OSError('fixture rename failure')))
    else:
        def injected(tree, path, limit):
            if path.name.startswith('.seraph-apt-'):
                if case == 'stage_bytes': path.write_bytes(b'x' * 144)
                elif case == 'stage_swap':
                    replacement = apt / 'unexpected'; replacement.write_bytes(helper.REPLACEMENT); replacement.chmod(0o644)
                    real_replace(replacement, path)
                    tree.own_change(apt)
                elif case == 'original_drift': (apt / helper.MIRROR).write_bytes(helper.ORIGINAL[::-1])
                elif case == 'deb_drift': (apt / helper.DEB822).write_text('changed Deb822 fixture')
            return real_read(tree, path, limit)
        monkeypatch.setattr(helper, 'read_file', injected)
    with pytest.raises((RuntimeError, OSError)):
        helper.publish(apt)
    after = mirror_state(apt)
    if case == 'original_drift':
        assert after[0] == helper.ORIGINAL[::-1] and after[1:] == before[1:]
    else:
        assert after == before
    remaining = list(apt.glob('.seraph-apt-*'))
    # Never remove the unexpected swapped inode; all our owned stages cleaned.
    assert len(remaining) == (1 if case == 'stage_swap' else 0)


def test_postpublication_failure_aborts_without_rollback(apt, monkeypatch):
    before = mirror_state(apt)
    real_replace = helper.os.replace
    def publish_then_change(*args, **kwargs):
        real_replace(*args, **kwargs)
        (apt / helper.DEB822).write_text('postpublication drift')
    monkeypatch.setattr(helper.os, 'replace', publish_then_change)
    with pytest.raises(RuntimeError, match='Deb822 drift after'):
        helper.publish(apt)
    after = mirror_state(apt)
    assert after[0] == helper.REPLACEMENT and after[2] != before[2]
    assert not list(apt.glob('.seraph-apt-*'))


@pytest.mark.parametrize('case', ['not_root', 'not_hosted', 'wrong_image', 'wrong_ubuntu'])
def test_cli_context_rejects_before_fixed_path_access(monkeypatch, case):
    monkeypatch.setattr(helper.sys, 'argv', ['prepare_ci_apt_mirrors.py'])
    monkeypatch.setattr(helper.os, 'geteuid', lambda: 1 if case == 'not_root' else 0)
    monkeypatch.setattr(helper.sys, 'flags', type('Flags', (), {'isolated':1, 'no_site':1, 'dont_write_bytecode':1})())
    monkeypatch.setenv('GITHUB_ACTIONS', 'false' if case == 'not_hosted' else 'true')
    monkeypatch.setenv('RUNNER_ENVIRONMENT', 'github-hosted')
    monkeypatch.setenv('ImageOS', 'ubuntu22' if case == 'wrong_image' else 'ubuntu24')
    monkeypatch.setattr(helper.platform, 'freedesktop_os_release', lambda: {'ID':'ubuntu', 'VERSION_ID':'22.04' if case == 'wrong_ubuntu' else '24.04'})
    monkeypatch.setattr(helper, 'publish', lambda *a: pytest.fail('fixed production path reached'))
    with pytest.raises(RuntimeError):
        helper.main()
