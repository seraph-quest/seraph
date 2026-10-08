"""Candidate negative tests; fixture bytes only, no vendor program execution."""
import importlib.util
import os
import hashlib
import tempfile
from functools import partial
from types import SimpleNamespace
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ci_prepare', BACKEND_ROOT / 'scripts/prepare_ci_shared_python.py')
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


@pytest.mark.parametrize('variable,value', [('GITHUB_ACTIONS', ''), ('GITHUB_ACTIONS', 'false'),
                                         ('RUNNER_ENVIRONMENT', 'self-hosted')])
def test_rejects_non_hosted_context_before_mutation(monkeypatch, variable, value):
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setenv('RUNNER_ENVIRONMENT', 'github-hosted')
    monkeypatch.setenv(variable, value)
    with pytest.raises(RuntimeError):
        helper.selected_prefix()


def test_rejects_wrong_patch_before_mutation(monkeypatch):
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setenv('RUNNER_ENVIRONMENT', 'github-hosted')
    monkeypatch.setattr(helper.sys, 'version_info', (3, 12, 14))
    with pytest.raises(RuntimeError, match='exact Python'):
        helper.selected_prefix()


def test_outside_file_remains_untouched(tmp_path):
    prefix = tmp_path / 'prefix'
    prefix.mkdir()
    outside = tmp_path / 'operator-file'
    outside.write_bytes(b'private fixture')
    outside.chmod(0o666)
    with pytest.raises(RuntimeError, match='outside'):
        helper.read_regular(outside, prefix)
    assert outside.read_bytes() == b'private fixture'
    assert outside.stat().st_mode & 0o777 == 0o666


def test_symlink_parent_rejected(selected_fixture):
    prefix, files, maps = selected_fixture
    linked = prefix / 'lib/python3.12/linked-directory'
    linked.symlink_to(prefix / 'lib', target_is_directory=True)
    before = snapshot(prefix)
    with pytest.raises(RuntimeError, match='linked/pth'):
        helper.plan(prefix, maps)
    assert snapshot(prefix) == before


def test_alias_escape_rejected_before_mutation(selected_fixture):
    prefix, files, maps = selected_fixture
    alias = prefix / 'lib/libpython3.12.so'
    alias.unlink()
    alias.symlink_to('/outside-library')
    before = (prefix / 'lib/python3.12/lib2to3/Grammar.pickle').stat().st_mode
    with pytest.raises(RuntimeError, match='alias mismatch'):
        helper.plan(prefix, maps)
    assert (prefix / 'lib/python3.12/lib2to3/Grammar.pickle').stat().st_mode == before


@pytest.fixture
def selected_fixture(monkeypatch):
    # Private repository fixture ancestors are trusted; no /tmp parent bypass.
    with tempfile.TemporaryDirectory(prefix='fixture-', dir=Path(__file__).parent) as directory:
        prefix = Path(directory) / 'selected'
        files = {'bin/python3.12': b'fixture executable',
                 'lib/libpython3.12.so.1.0': b'fixture shared library',
                 'lib/python3.12/config-3.12-x86_64-linux-gnu/libpython3.12.a': b'fixture unused archive',
                 'lib/python3.12/lib2to3/Grammar.pickle': b'fixture runtime grammar'}
        for relative, content in files.items():
            path = prefix / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            path.chmod(0o666 if relative.endswith('.pickle') else 0o755)
        for directory_path in prefix.rglob('*'):
            if directory_path.is_dir():
                directory_path.chmod(0o755)
        prefix.chmod(0o755)
        (prefix / 'lib/libpython3.12.so').symlink_to('libpython3.12.so.1.0')
        for name in ('EXECUTABLE', 'SHARED', 'ARCHIVE'):
            relative = getattr(helper, name)[0]
            monkeypatch.setattr(helper, name, (relative, len(files[relative]), hashlib.sha256(files[relative]).hexdigest()))
        info = (prefix / helper.SHARED[0]).stat()
        maps = f'1000-2000 r-xp 0000 {os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x} {info.st_ino} {prefix / helper.SHARED[0]}'
        yield prefix, files, maps


def snapshot(prefix):
    return {str(p.relative_to(prefix)): (p.read_bytes(), p.stat().st_mode & 0o777)
            for p in prefix.rglob('*') if p.is_file()}


def test_real_small_prefix_plan_apply_preserves_runtime(selected_fixture):
    prefix, files, maps = selected_fixture
    before = snapshot(prefix)
    external = prefix.parent
    external.chmod(0o777)
    for active in (prefix, prefix / 'bin', prefix / 'lib', prefix / 'lib/python3.12'):
        active.chmod(0o777)
    # Excluded writable cache contents stay untouched, never trusted/mutated.
    excluded = prefix / 'lib/python3.12/__pycache__'
    excluded.mkdir(mode=0o777)
    excluded.chmod(0o777)
    (excluded / 'ignored.pyc').write_bytes(b'excluded bytes')
    records = helper.plan(prefix, maps)
    try:
        receipt = helper.apply_plan(prefix, records)
    finally:
        records.tree.close()
    assert not (prefix / helper.ARCHIVE[0]).exists()
    assert external.stat().st_mode & 0o777 == 0o777
    for active in (prefix, prefix / 'bin', prefix / 'lib', prefix / 'lib/python3.12'):
        assert active.stat().st_mode & 0o777 == 0o755
    for relative, (content, mode) in before.items():
        if relative == helper.ARCHIVE[0]:
            continue
        path = prefix / relative
        assert path.read_bytes() == content
        assert path.stat().st_mode & 0o777 == mode & ~0o022
        assert path.stat().st_mode & 0o111 == mode & 0o111
    assert (excluded / 'ignored.pyc').read_bytes() == b'excluded bytes'
    assert excluded.stat().st_mode & 0o777 == 0o777
    assert receipt['loaded_libpython_sha256'] == helper.SHARED[2]


@pytest.mark.parametrize('case', ['empty_maps', 'malformed_maps', 'wrong_inode', 'unknown_archive',
                                  'hardlink', 'symlink', 'outside_symlink', 'changed_archive'])
def test_unsafe_preflight_has_zero_mutations(selected_fixture, case):
    prefix, files, maps = selected_fixture
    extra = prefix / 'lib/python3.12/late-module'
    if case == 'empty_maps': maps = ''
    elif case == 'malformed_maps': maps = 'libpython'
    elif case == 'wrong_inode': maps = maps.replace(str((prefix / helper.SHARED[0]).stat().st_ino), '0')
    elif case == 'unknown_archive': extra.with_suffix('.a').write_bytes(b'unknown archive')
    elif case == 'hardlink': os.link(prefix / 'lib/python3.12/lib2to3/Grammar.pickle', extra)
    elif case == 'symlink': extra.symlink_to(prefix / helper.SHARED[0])
    elif case == 'outside_symlink':
        outside = prefix.parent / 'outside';outside.write_bytes(b'outside fixture');extra.symlink_to(outside)
    elif case == 'changed_archive': (prefix / helper.ARCHIVE[0]).write_bytes(b'changed archive')
    before = snapshot(prefix)
    with pytest.raises(RuntimeError):
        helper.plan(prefix, maps)
    assert snapshot(prefix) == before
    assert (prefix / helper.ARCHIVE[0]).exists()


def test_identity_includes_archive_inode_size_owner_and_mode(tmp_path):
    archive = tmp_path / 'libpython3.12.a'
    archive.write_bytes(b'fixture static development archive')
    before = helper.identity(archive.stat())
    replacement = tmp_path / 'replacement'
    replacement.write_bytes(archive.read_bytes())
    os.replace(replacement, archive)
    assert helper.identity(archive.stat()) != before


@pytest.mark.parametrize('change', ['directory_replacement', 'new_entry', 'file_content', 'alias_replacement'])
def test_post_plan_drift_refuses_before_any_mutation(selected_fixture, change):
    prefix, files, maps = selected_fixture
    records = helper.plan(prefix, maps)
    try:
        if change == 'directory_replacement':
            original = prefix / 'lib/python3.12/lib2to3'
            original.rename(original.with_name('old-lib2to3'))
            original.mkdir(mode=0o755)
            (original / 'Grammar.pickle').write_bytes(b'replacement fixture')
        elif change == 'new_entry':
            (prefix / 'lib/python3.12/new-module.py').write_bytes(b'new entry')
        elif change == 'file_content':
            (prefix / 'lib/python3.12/lib2to3/Grammar.pickle').write_bytes(b'changed runtime fixture')
        else:
            alias = prefix / 'lib/libpython3.12.so'
            alias.unlink()
            alias.symlink_to('libpython3.12.so.1.0')
        before = snapshot(prefix)
        with pytest.raises(RuntimeError):
            helper.apply_plan(prefix, records)
        assert snapshot(prefix) == before
        assert (prefix / helper.ARCHIVE[0]).exists()
    finally:
        records.tree.close()


def test_fifo_preflight_fails_without_blocking_or_mutation(selected_fixture):
    prefix, files, maps = selected_fixture
    fifo = prefix / 'lib/python3.12/late-fifo'
    os.mkfifo(fifo)
    before = snapshot(prefix)
    with pytest.raises(RuntimeError, match='ordinary file'):
        helper.plan(prefix, maps)
    assert snapshot(prefix) == before
    assert fifo.exists()


def test_scandir_error_propagates_with_zero_mutation(selected_fixture, monkeypatch):
    prefix, files, maps = selected_fixture
    real_scandir = helper.os.scandir
    def denied(directory):
        if isinstance(directory, int):
            raise PermissionError('fixture permission denial')
        return real_scandir(directory)
    before = snapshot(prefix)
    with monkeypatch.context() as patch:
        patch.setattr(helper.os, 'scandir', denied)
        with pytest.raises(PermissionError):
            helper.plan(prefix, maps)
    assert snapshot(prefix) == before


def test_foreign_file_uid_denied_with_zero_mutation(selected_fixture, monkeypatch):
    prefix, files, maps = selected_fixture
    target = (prefix / helper.EXECUTABLE[0]).stat()
    real_fstat = helper.os.fstat
    def foreign_owner(descriptor):
        info = real_fstat(descriptor)
        if (info.st_dev, info.st_ino) != (target.st_dev, target.st_ino):
            return info
        return SimpleNamespace(**{name: (info.st_uid + 1 if name == 'st_uid' else getattr(info, name))
                                  for name in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns',
                                               'st_mode', 'st_uid', 'st_gid', 'st_nlink')})
    before = snapshot(prefix)
    with monkeypatch.context() as patch:
        patch.setattr(helper.os, 'fstat', foreign_owner)
        with pytest.raises(RuntimeError, match='not owned'):
            helper.plan(prefix, maps)
    assert snapshot(prefix) == before
