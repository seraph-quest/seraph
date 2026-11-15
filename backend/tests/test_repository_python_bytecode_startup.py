"""A disposable, site-enabled CPython startup behavior oracle.

The negative control copies only base-interpreter inputs into private storage;
it never launches the dependency venv or changes an adopted cache frontier.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


_BASE = Path('/home/pawel/.local/share/uv/python/cpython-3.12.8-linux-x86_64-gnu')
_PYTHON_SHA = '0dc3a692fa85fcdb7f1a5877d2adf179809ac417a07ffde2373c832863800a15'
_LIBPYTHON_SHA = 'b8f547cdf6a3b892600cc9eb5151ff893ad4525ff5b0b3dc595533a624777048'
_BASE_INPUTS_SHA = '234e022221460d42251a36dfa27a18c923f3220dd63176637b573eafb23cb2ea'


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _private_file(path, data, *, executable=False):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                 0o700 if executable else 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)


def _base_inputs():
    assert sys.version_info[:3] == (3, 12, 8), 'requires reviewed cached CPython 3.12.8 route'
    assert Path(sys.executable).resolve() == _BASE / 'bin/python3.12'
    assert _sha(_BASE / 'bin/python3.12') == _PYTHON_SHA
    assert _sha(_BASE / 'lib/libpython3.12.so.1.0') == _LIBPYTHON_SHA
    paths = [_BASE / 'bin/python3.12', _BASE / 'lib/libpython3.12.so.1.0']
    paths.extend(sorted(path for path in (_BASE / 'lib/python3.12').rglob('*')
                        if path.is_file() and 'site-packages' not in path.relative_to(_BASE).parts
                        and '__pycache__' not in path.relative_to(_BASE).parts
                        and path.suffix not in {'.pyc', '.pyo'}))
    assert len(paths) <= 1024
    assert sum(path.stat().st_size for path in paths) <= 64 * 1024 * 1024
    for path in paths:
        assert not path.is_symlink() and path.resolve().is_relative_to(_BASE)
    inputs = {path.relative_to(_BASE).as_posix(): _sha(path) for path in paths}
    assert hashlib.sha256(json.dumps(inputs, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest() == _BASE_INPUTS_SHA
    return inputs


def _clone_runtime(directory, inputs):
    base = directory / 'base'
    base.mkdir(mode=0o700)
    for relative, digest in inputs.items():
        source = _BASE / relative
        target = base / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        raw = source.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == digest
        _private_file(target, raw, executable=relative == 'bin/python3.12')
    venv = directory / 'venv'
    (venv / 'bin').mkdir(parents=True, mode=0o700)
    (venv / 'bin/python').symlink_to(base / 'bin/python3.12')
    _private_file(venv / 'pyvenv.cfg',
                  ('home = ' + str(base / 'bin') + '\ninclude-system-site-packages = false\n').encode())
    site = venv / 'lib/python3.12/site-packages'
    site.mkdir(parents=True, mode=0o700)
    # Runs during addpackage, before script code and execsitecustomize. Only
    # builtin open and builtin sys are needed to observe the interpreter flag.
    _private_file(site / 'startup_probe.py', b'import sys\n'
                  b'with open(__file__ + ".observation", "x") as stream:\n'
                  b' stream.write(str(int(sys.dont_write_bytecode)))\n')
    _private_file(site / 'startup_probe.pth', b'import startup_probe\n')
    _private_file(site / 'later_source.py', b'value = "actual source import"\n')
    script = directory / 'script.py'
    _private_file(script, b'import json, sys, later_source\n'
                  b'with open("/proc/self/maps") as stream:\n'
                  b' libraries=sorted(set(line.split()[-1] for line in stream if "libpython3.12" in line))\n'
                  b'print(json.dumps({"prefix":sys.prefix,"base_prefix":sys.base_prefix,\n'
                  b' "isolated":sys.flags.isolated,"no_site":sys.flags.no_site,\n'
                  b' "dont_write_bytecode":sys.dont_write_bytecode,"value":later_source.value,\n'
                  b' "paths":sys.path,"libraries":libraries}))\n')
    return base, venv, site, script


def test_bytecode_flag_precedes_pth_and_negative_control_is_private(tmp_path):
    if not _BASE.exists():
        pytest.skip('exact pinned CPython 3.12.8 runtime fixture unavailable; host canary not accepted')
    inputs = _base_inputs()
    before = dict(inputs)
    for flag, expected in ((True, True), (False, False)):
        directory = tmp_path / ('with-B' if flag else 'without-B')
        directory.mkdir(mode=0o700)
        base, venv, site, script = _clone_runtime(directory, inputs)
        assert not list(directory.rglob('*.pyc'))
        argv = [str(venv / 'bin/python'), '-I']
        if flag:
            argv.append('-B')
        argv.append(str(script))
        result = subprocess.run(argv, cwd=directory,
                                env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'TZ': 'UTC'},
                                stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
                                check=True)
        actual = json.loads(result.stdout)
        assert not result.stderr
        paths = actual.pop('paths')
        assert paths and all(Path(path).is_relative_to(base) or Path(path).is_relative_to(venv)
                             for path in paths)
        assert actual.pop('libraries') == [str(base / 'lib/libpython3.12.so.1.0')]
        assert actual == {'prefix': str(venv), 'base_prefix': str(base),
                          'isolated': 1, 'no_site': 0,
                          'dont_write_bytecode': expected, 'value': 'actual source import'}
        assert (site / 'startup_probe.py.observation').read_text() == str(int(expected))
        pycs = list(directory.rglob('*.pyc'))
        if flag:
            assert not pycs
        else:
            assert list((site / '__pycache__').glob('startup_probe.*.pyc'))
            assert list((site / '__pycache__').glob('later_source.*.pyc'))
            assert all(path.resolve().is_relative_to(directory) for path in pycs)
        assert {relative: _sha(_BASE / relative) for relative in inputs} == before
        assert {relative: _sha(base / relative) for relative in inputs} == before
