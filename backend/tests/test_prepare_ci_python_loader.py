"""Synthetic protocol fixtures only; never execute a tool or vendor interpreter."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import struct
import tarfile

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ci_loader_fixture', BACKEND_ROOT / 'scripts/prepare_ci_python_loader.py')
loader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loader)
TOOL = b'fixture authenticated packaging tool, not executable'


@pytest.fixture(autouse=True)
def prohibit_real_network_and_tool_execution(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('pure loader fixture attempted network or tool execution')
    monkeypatch.setattr(loader, 'download_tool', forbidden)
    monkeypatch.setattr(loader.subprocess, 'Popen', forbidden)
    monkeypatch.setattr(loader, 'transform', forbidden)


def pin(data):
    return len(data), hashlib.sha256(data).hexdigest()


def elf_fixture(runpath, *, needed=('libpython3.12.so.1.0', 'libc.so.6'),
                interpreter='/lib64/ld-linux-x86-64.so.2', rpath=None, machine=62):
    # Independent minimal ELF64 fixture with explicit load/dynamic/interpreter segments.
    strings = bytearray(b'\0')
    def add(text):
        offset = len(strings); strings.extend(text.encode() + b'\0'); return offset
    tags = [(1, add(name)) for name in needed]
    tags += [(29, add(runpath))]
    if rpath is not None: tags += [(15, add(rpath))]
    tags += [(5, 0x400000 + 512), (10, len(strings)), (0, 0)]
    data = bytearray(2048)
    data[:16] = b'\x7fELF\x02\x01\x01' + b'\0' * 9
    struct.pack_into('<HHIQQQIHHHHHH', data, 16, 3, machine, 1, 0, 64, 0, 0, 64, 56, 3, 0, 0, 0)
    interp = interpreter.encode() + b'\0'
    for index, row in enumerate([(1, 5, 0, 0x400000, 0, len(data), len(data), 4096),
        (2, 6, 256, 0x400100, 0, len(tags)*16, len(tags)*16, 8),
        (3, 4, 1024, 0x400400, 0, len(interp), len(interp), 1)]):
        struct.pack_into('<IIQQQQQQ', data, 64 + index*56, *row)
    for index, tag in enumerate(tags): struct.pack_into('<qQ', data, 256 + index*16, *tag)
    data[512:512+len(strings)] = strings
    data[1024:1024+len(interp)] = interp
    return bytes(data)


def archive_fixture(shape, *, mutate=None):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz', format=tarfile.USTAR_FORMAT) as archive:
        for name, (kind, size) in shape.items():
            entry = tarfile.TarInfo(name); entry.uid = entry.gid = 0
            entry.type = tarfile.DIRTYPE if kind == 'directory' else tarfile.REGTYPE
            entry.mode = 0o755 if kind == 'directory' or name == './bin/patchelf' else 0o644
            entry.size = size
            content = TOOL if name == './bin/patchelf' else b'd' * size
            if mutate: content = mutate(entry, content)
            archive.addfile(entry, io.BytesIO(content) if entry.isreg() else None)
    return stream.getvalue()


@pytest.fixture
def tool_archive(monkeypatch):
    shape = dict(loader.TOOL_SHAPE)
    shape['./bin/patchelf'] = ('regular', len(TOOL))
    raw = archive_fixture(shape)
    monkeypatch.setattr(loader, 'TOOL_SHAPE', shape)
    monkeypatch.setattr(loader, 'TOOL_ARCHIVE', pin(raw))
    monkeypatch.setattr(loader, 'TOOL_BINARY', pin(TOOL))
    return raw, shape


@pytest.fixture
def selected(monkeypatch, tmp_path):
    prefix = tmp_path / 'selected'
    original = elf_fixture(str(prefix / 'lib'))
    files = {loader.shared.EXECUTABLE[0]: original,
             loader.shared.SHARED[0]: b'fixture genuine shared bytes',
             loader.shared.ARCHIVE[0]: b'fixture unused static archive',
             'lib/python3.12/active.py': b'fixture active runtime module'}
    for relative, data in files.items():
        path = prefix / relative; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data); path.chmod(0o755)
    (prefix / 'lib/libpython3.12.so').symlink_to('libpython3.12.so.1.0')
    for name in ('EXECUTABLE', 'SHARED', 'ARCHIVE'):
        relative = getattr(loader.shared, name)[0]
        monkeypatch.setattr(loader.shared, name, (relative, *pin(files[relative])))
    monkeypatch.setattr(loader, 'TOOL_BINARY', pin(TOOL))
    return prefix, original, elf_fixture(loader.RUNPATH)


def original_state(prefix):
    path = prefix / loader.shared.EXECUTABLE[0]
    info = path.stat()
    return path.read_bytes(), info.st_dev, info.st_ino, stat.S_IMODE(info.st_mode)


def transform_bytes(data):
    def transform(tool, source, output):
        output.write_bytes(data)
    return transform


def test_authenticated_thirteen_entry_archive_returns_only_tool(tool_archive):
    raw, shape = tool_archive
    assert len(shape) == 13
    assert loader.tool_member(raw) == TOOL


def test_changed_archive_rejected_before_parse(tool_archive):
    raw, _ = tool_archive
    with pytest.raises(RuntimeError, match='authentication'):
        loader.tool_member(raw[:-1] + bytes([raw[-1] ^ 1]))


def test_authenticated_incomplete_archive_rejected(tool_archive, monkeypatch):
    _, shape = tool_archive
    reduced = dict(shape); del reduced['./share/doc/patchelf/README.md']
    raw = archive_fixture(reduced)
    monkeypatch.setattr(loader, 'TOOL_ARCHIVE', pin(raw))
    with pytest.raises(RuntimeError, match='incomplete'): loader.tool_member(raw)


@pytest.mark.parametrize('case', ['owner', 'mode', 'link', 'path', 'inner_bytes'])
def test_authenticated_archive_shape_and_inner_pin_fail_closed(tool_archive, monkeypatch, case):
    _, shape = tool_archive
    def change(entry, content):
        if entry.name != './bin/patchelf': return content
        if case == 'owner': entry.uid = 42
        elif case == 'mode': entry.mode = 0o777
        elif case == 'link': entry.type = tarfile.SYMTYPE; entry.linkname = '/outside'; entry.size = 0
        elif case == 'path': entry.name = '../escape'
        else: content = b'x' * len(content)
        return content
    raw = archive_fixture(shape, mutate=change)
    # Authenticate this synthetic archive separately, retaining known inner fixture pin.
    monkeypatch.setattr(loader, 'TOOL_ARCHIVE', pin(raw))
    with pytest.raises(RuntimeError): loader.tool_member(raw)


@pytest.mark.parametrize('case', ['needed', 'interpreter', 'rpath', 'runpath', 'machine', 'truncated'])
def test_unsupported_derived_elf_rejected(selected, case):
    prefix, original, derived = selected
    changes = {'needed': {'needed': ('libc.so.6',)}, 'interpreter': {'interpreter': '/wrong/loader'},
               'rpath': {'rpath': '/wrong'}, 'runpath': {}, 'machine': {'machine': 3}}
    if case == 'truncated': derived = derived[:80]
    else: derived = elf_fixture('/wrong' if case == 'runpath' else loader.RUNPATH, **changes[case])
    with pytest.raises(RuntimeError): loader.validate_derived(original, derived, prefix)


def test_valid_derivation_has_new_identity_and_sealed_bound_receipt(selected):
    prefix, original, derived = selected
    before = original_state(prefix)
    receipt = loader.derive(prefix, TOOL, run_transform=transform_bytes(derived))
    assert original_state(prefix)[0] == derived
    assert original_state(prefix)[2] != before[2]
    assert receipt['vendor_executable'][1:] == list(pin(original))
    assert receipt['derived']['sha256'] == hashlib.sha256(derived).hexdigest()
    assert receipt['derived']['sha256'] != receipt['vendor_executable'][2]
    assert stat.S_IMODE((prefix / loader.RECEIPT).stat().st_mode) == 0o400
    assert loader.read_receipt(prefix) == receipt
    assert (prefix / loader.shared.SHARED[0]).read_bytes() == b'fixture genuine shared bytes'
    assert not list(prefix.parent.glob('.seraph-loader-*'))


@pytest.mark.parametrize('case', ['timeout', 'tool_error', 'stage_swap', 'tool_drift', 'wrong_elf', 'output_mode'])
def test_failed_stage_preserves_original_bytes_inode_mode(selected, case):
    prefix, _, derived = selected
    before = original_state(prefix)
    def transform(tool, source, output):
        if case == 'timeout': raise TimeoutError('fixture transform deadline')
        if case == 'tool_error': raise RuntimeError('fixture tool failed')
        if case == 'stage_swap':
            replacement = output.with_name('replacement'); replacement.write_bytes(derived)
            os.replace(replacement, output)
        else: output.write_bytes(b'invalid ELF' if case == 'wrong_elf' else derived)
        if case == 'tool_drift': tool.chmod(0o700); tool.write_bytes(b'x' * len(TOOL)); tool.chmod(0o500)
        if case == 'output_mode': output.chmod(0o666)
    with pytest.raises((RuntimeError, TimeoutError)):
        loader.derive(prefix, TOOL, run_transform=transform)
    assert original_state(prefix) == before
    assert not (prefix / loader.RECEIPT).exists()
    assert not list(prefix.parent.glob('.seraph-loader-*'))


def test_tool_pin_mismatch_cannot_start_transform(selected):
    prefix, _, _ = selected; before = original_state(prefix)
    def forbidden(*args): pytest.fail('unauthenticated tool invoked')
    with pytest.raises(RuntimeError): loader.derive(prefix, TOOL + b'changed', run_transform=forbidden)
    assert original_state(prefix) == before


def test_dangling_existing_receipt_blocks_before_original_mutation(selected):
    prefix, _, _ = selected; before = original_state(prefix)
    (prefix / loader.RECEIPT).symlink_to(prefix / 'missing-private-receipt')
    def forbidden(*args): pytest.fail('existing receipt allowed transformation')
    with pytest.raises(RuntimeError, match='already exists'):
        loader.derive(prefix, TOOL, run_transform=forbidden)
    assert original_state(prefix) == before


@pytest.mark.parametrize('content', [b'not-json', b'[]', b'{}', b'x' * 16385])
def test_malformed_or_oversized_receipt_never_authenticates(selected, content):
    prefix, _, _ = selected
    path = prefix / loader.RECEIPT; path.write_bytes(content); path.chmod(0o400)
    with pytest.raises(RuntimeError): loader.read_receipt(prefix)


def test_missing_receipt_never_authenticates_selected_source(selected):
    prefix, _, _ = selected
    with pytest.raises(OSError): loader.read_receipt(prefix)


@pytest.mark.parametrize('relative', ['lib/libpython3.12.so.1.0', 'lib/python3.12/active.py'])
def test_late_vendor_closure_drift_prevents_executable_publication(selected, relative):
    prefix, _, derived = selected; before = original_state(prefix)
    def transform(tool, source, output):
        output.write_bytes(derived)
        (prefix / relative).write_bytes(b'changed fixture runtime bytes')
    with pytest.raises(RuntimeError): loader.derive(prefix, TOOL, run_transform=transform)
    assert original_state(prefix) == before
    assert not (prefix / loader.RECEIPT).exists()


def test_late_source_drift_is_not_overwritten_by_derived_publication(selected):
    prefix, original, derived = selected
    before = original_state(prefix)
    tampered = original + b'fixture injected source drift'
    def transform(tool, source, output):
        output.write_bytes(derived); source.write_bytes(tampered)
    with pytest.raises(RuntimeError): loader.derive(prefix, TOOL, run_transform=transform)
    after = original_state(prefix)
    assert after[0] == tampered and after[1:] == before[1:]
    assert not (prefix / loader.RECEIPT).exists()


def test_real_sparse_fixture_closure_over_quota_cannot_publish(selected):
    prefix, _, derived = selected; before = original_state(prefix)
    # Actual bounded per-file sizes, not mocked stat/hash/quota authority.
    for index in range(3):
        path = prefix / f'lib/python3.12/quota-{index}.dat'
        with path.open('wb') as stream: stream.truncate(32 * 1024 * 1024)
        path.chmod(0o644)
    with pytest.raises(RuntimeError, match='quota'):
        loader.derive(prefix, TOOL, run_transform=transform_bytes(derived))
    assert original_state(prefix) == before
    assert not (prefix / loader.RECEIPT).exists()


@pytest.mark.parametrize('case', ['bytes', 'inode', 'mode', 'receipt_operation', 'receipt_extra', 'receipt_identity', 'receipt_mode', 'receipt_schema_bool'])
def test_published_receipt_rejects_live_or_schema_tamper(selected, case):
    prefix, _, derived = selected
    receipt = loader.derive(prefix, TOOL, run_transform=transform_bytes(derived))
    executable = prefix / loader.shared.EXECUTABLE[0]
    sealed = prefix / loader.RECEIPT
    if case == 'bytes': executable.chmod(0o700); executable.write_bytes(derived + b'x'); executable.chmod(0o500)
    elif case == 'inode':
        replacement = executable.with_name('replacement'); replacement.write_bytes(derived); replacement.chmod(0o500)
        os.replace(replacement, executable)
    elif case == 'mode': executable.chmod(0o700)
    elif case == 'receipt_mode': sealed.chmod(0o600)
    else:
        if case == 'receipt_operation': receipt['operation'] = ['--force-rpath']
        elif case == 'receipt_extra': receipt['arbitrary_digest'] = '0' * 64
        elif case == 'receipt_schema_bool': receipt['schema'] = True
        else: receipt['derived']['identity'][1] += 1
        sealed.chmod(0o600); sealed.write_text(json.dumps(receipt)); sealed.chmod(0o400)
    with pytest.raises(RuntimeError): loader.read_receipt(prefix)
