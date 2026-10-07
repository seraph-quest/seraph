"""CI-only, system-interpreter packaging of an authenticated Python executable.

setup-python may already have run probes. Its processes must have exited; the
selected prefix stays quiescent during staging/publication. This is not an OS
isolation claim. No runtime guard or declared worker environment is changed.
"""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request

spec = importlib.util.spec_from_file_location('ci_shared', Path(__file__).with_name('prepare_ci_shared_python.py'))
shared = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shared)
require = shared.require
TOOL_URL = 'https://github.com/NixOS/patchelf/releases/download/0.19.2/patchelf-0.19.2-x86_64.tar.gz'
TOOL_ARCHIVE = (569787, '2abdc34fbe949c995a1baa4ec310896a085588a45a99cd0d726851bdfac2a4bb')
TOOL_BINARY = (1477296, '5d0eb32dc14d6f051a81c5a1c2ecfa8116fdd637a38a48863924e0613cdca4da')
TOOL_SHAPE = {
    '.': ('directory', 0), './share': ('directory', 0),
    './share/zsh': ('directory', 0), './share/zsh/site-functions': ('directory', 0),
    './share/zsh/site-functions/_patchelf': ('regular', 3709),
    './share/doc': ('directory', 0), './share/doc/patchelf': ('directory', 0),
    './share/doc/patchelf/README.md': ('regular', 3997),
    './share/man': ('directory', 0), './share/man/man1': ('directory', 0),
    './share/man/man1/patchelf.1': ('regular', 6231),
    './bin': ('directory', 0), './bin/patchelf': ('regular', 1477296),
}
RECEIPT = '.seraph-ci-loader.json'
RUNPATH = '$ORIGIN/../lib'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def tool_member(raw):
    """Authenticate before parsing; never extractall or restore archive owners."""
    import tarfile
    require((len(raw), digest(raw)) == TOOL_ARCHIVE, 'tool archive authentication failed')
    seen = set()
    total = 0
    binary = None
    with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
        for entry in archive:
            require(len(seen) < 256, 'tool archive count bound')
            path = PurePosixPath(entry.name)
            require(not path.is_absolute() and '..' not in path.parts and len(path.parts) <= 8,
                    'unsafe tool archive path')
            require(entry.name not in seen and entry.name in TOOL_SHAPE, 'unexpected tool archive entry')
            seen.add(entry.name)
            kind = 'directory' if entry.isdir() else 'regular' if entry.isreg() else 'special'
            require((kind, entry.size) == TOOL_SHAPE[entry.name] and not entry.pax_headers,
                    'unexpected tool archive shape')
            require(entry.uid == entry.gid == 0 and entry.mode == (0o755 if entry.isdir()
                    or entry.name == './bin/patchelf' else 0o644) and not entry.sparse,
                    'unexpected tool archive metadata')
            require(0 <= entry.size <= 8 * 1024 * 1024, 'tool member bound')
            total += entry.size
            require(total <= 32 * 1024 * 1024, 'tool archive total bound')
            if entry.name == './bin/patchelf':
                binary = archive.extractfile(entry).read(entry.size + 1)
    require(seen == set(TOOL_SHAPE) and binary is not None, 'incomplete tool archive')
    require((len(binary), digest(binary)) == TOOL_BINARY, 'tool executable authentication failed')
    return binary


def elf(data):
    """Bounded ELF64 metadata, without loading or invoking vendor code."""
    require(len(data) >= 64 and data[:6] == b'\x7fELF\x02\x01', 'unsupported ELF')
    header = struct.unpack_from('<HHIQQQIHHHHHH', data, 16)
    require(data[6] == 1 and header[2] == 1 and header[1] == 62 and header[8] == 56
            and 0 < header[9] <= 256, 'unsupported ELF ABI')
    require(header[4] + header[8] * header[9] <= len(data), 'ELF header bounds')
    programs = [struct.unpack_from('<IIQQQQQQ', data, header[4] + i * 56)
                for i in range(header[9])]
    require(all(p[2] + p[5] <= len(data) for p in programs), 'ELF segment bounds')
    dynamic = [p for p in programs if p[0] == 2]
    interpreter = [p for p in programs if p[0] == 3]
    require(len(dynamic) == 1 and len(interpreter) <= 1, 'unsupported ELF segments')
    tags = []
    require(dynamic[0][5] % 16 == 0 and dynamic[0][5] <= 1024 * 1024, 'ELF dynamic bound')
    for offset in range(dynamic[0][2], dynamic[0][2] + dynamic[0][5], 16):
        tag, value = struct.unpack_from('<qQ', data, offset)
        if tag == 0:
            break
        tags.append((tag, value))
    else:
        raise RuntimeError('unterminated ELF dynamic')
    tables = [v for t, v in tags if t == 5]
    sizes = [v for t, v in tags if t == 10]
    require(len(tables) == len(sizes) == 1 and 0 < sizes[0] <= len(data), 'ELF strings bounds')
    loads = [p for p in programs if p[0] == 1 and p[3] <= tables[0]
             and tables[0] + sizes[0] <= p[3] + p[5]]
    require(len(loads) == 1, 'ELF strings mapping')
    start = loads[0][2] + tables[0] - loads[0][3]
    strings = data[start:start + sizes[0]]
    def text(value):
        require(0 <= value < len(strings), 'ELF string offset')
        end = strings.find(b'\0', value)
        require(end >= 0, 'unterminated ELF string')
        return strings[value:end].decode('ascii')
    interp = None
    if interpreter:
        p = interpreter[0]
        raw = data[p[2]:p[2] + p[5]]
        require(raw.endswith(b'\0') and b'\0' not in raw[:-1], 'ELF interpreter')
        interp = raw[:-1].decode('ascii')
    return {'class': 64, 'endian': 'little', 'osabi': data[7], 'abi_version': data[8],
            'flags': header[6], 'type': header[0], 'machine': header[1], 'interpreter': interp,
            'needed': [text(v) for t, v in tags if t == 1],
            'runpath': [text(v) for t, v in tags if t == 29],
            'rpath': [text(v) for t, v in tags if t == 15]}


def validate_derived(original, derived, prefix):
    before, after = elf(original), elf(derived)
    require(before['type'] in {2, 3} and before['interpreter'] == '/lib64/ld-linux-x86-64.so.2'
            and before['needed'] == ['libpython3.12.so.1.0', 'libc.so.6']
            and before['runpath'] == [str(prefix / 'lib')] and not before['rpath'],
            'unsupported original executable ELF')
    require(all(before[key] == after[key] for key in
                ('class', 'endian', 'osabi', 'abi_version', 'flags', 'type', 'machine', 'interpreter', 'needed'))
            and after['runpath'] == [RUNPATH] and not after['rpath'], 'derived ELF mismatch')
    return before, after


def file_bytes(path, tree, authenticated_vendor_input=False, max_bytes=48 * 1024 * 1024):
    fd = tree.open_file(path)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_uid == os.geteuid()
                and before.st_nlink == 1 and not before.st_mode & 0o7000
                and (authenticated_vendor_input or not before.st_mode & 0o022)
                and before.st_size <= max_bytes, 'unsafe sealed file')
        chunks = []
        count = 0
        deadline = time.monotonic() + 15
        while chunk := os.read(fd, 1024 * 1024):
            count += len(chunk)
            require(count <= before.st_size and time.monotonic() < deadline, 'file read bound')
            chunks.append(chunk)
        require(count == before.st_size and shared.identity(before) == shared.identity(os.fstat(fd)),
                'sealed file drift')
        return before, b''.join(chunks)
    finally:
        os.close(fd)


def download_tool():
    class HTTPSRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, response, code, message, headers, new_url):
            require(new_url.startswith('https://'), 'non-HTTPS tool redirect')
            return super().redirect_request(request, response, code, message, headers, new_url)
    def expired(signum, frame):
        raise TimeoutError('tool network deadline')
    require(signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0), 'unexpected provisioning timer')
    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 30)
    try:
        with urllib.request.build_opener(HTTPSRedirect).open(TOOL_URL, timeout=30) as response:
            require(response.geturl().startswith('https://'), 'non-HTTPS tool response')
            raw = response.read(TOOL_ARCHIVE[0] + 1)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    return tool_member(raw)


def write_private(path, data, mode, dir_fd=None):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
    try:
        view = memoryview(data)
        while view:
            count = os.write(fd, view)
            require(count > 0, 'private output write failure')
            view = view[count:]
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)


def transform(tool, source, output):
    # Only output is writable; no shell, inherited loader controls or target run.
    process = subprocess.Popen([str(tool), '--set-rpath', RUNPATH, '--output', str(output), str(source)],
                               env={'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C'},
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        require(process.wait(timeout=30) == 0, 'patchelf failed')
    except BaseException:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        raise


def derive(prefix, binary, run_transform=transform):
    require((len(binary), digest(binary)) == TOOL_BINARY, 'tool executable authentication failed')
    require(not os.path.lexists(prefix / RECEIPT), 'derivation receipt already exists')
    # Create sibling stage before capturing parent metadata; same filesystem
    # permits atomic replace. This stage never enters the product runtime closure.
    with tempfile.TemporaryDirectory(prefix='.seraph-loader-', dir=prefix.parent) as directory:
        stage = Path(directory)
        tool, output = stage / 'patchelf', stage / 'python3.12'
        write_private(tool, binary, 0o500)
        write_private(output, b'', 0o600)
        stage_tree = shared.HeldDirectories(stage)
        source_tree = shared.HeldDirectories(prefix)
        try:
            records = shared.RuntimePlan(shared._plan(prefix, '', source_tree, verify_loaded=False), source_tree)
            original_info, original = file_bytes(prefix / shared.EXECUTABLE[0], source_tree, True)
            require((len(original), digest(original)) == shared.EXECUTABLE[1:], 'vendor executable drift')
            original_elf = elf(original)
            # Validate original metadata before invoking any authenticated tool.
            require(original_elf['interpreter'] == '/lib64/ld-linux-x86-64.so.2'
                    and original_elf['needed'] == ['libpython3.12.so.1.0', 'libc.so.6']
                    and original_elf['runpath'] == [str(prefix / 'lib')] and not original_elf['rpath']
                    and original_elf['type'] in {2, 3}, 'unsupported original executable ELF')
            tool_info, tool_data = file_bytes(tool, stage_tree)
            require((len(tool_data), digest(tool_data)) == TOOL_BINARY, 'staged tool drift')
            output_info = output.stat()
            require(original_info.st_dev == output_info.st_dev, 'cross-filesystem staging')
            run_transform(tool, prefix / shared.EXECUTABLE[0], output)
            require(shared.identity(file_bytes(tool, stage_tree)[0]) == shared.identity(tool_info),
                    'tool identity drift')
            stage_tree.check(stage)
            fd = stage_tree.open_file(output)
            try:
                current = os.fstat(fd)
                require((current.st_dev, current.st_ino, current.st_uid, current.st_gid, current.st_nlink,
                         stat.S_IMODE(current.st_mode)) ==
                        (output_info.st_dev, output_info.st_ino, os.geteuid(), output_info.st_gid, 1, 0o600),
                        'staged executable replaced or unsafe')
                os.fchmod(fd, 0o500)
            finally:
                os.close(fd)
            derived_info, derived = file_bytes(output, stage_tree)
            before_elf, after_elf = validate_derived(original, derived, prefix)
            # The sole removed .a is not runtime exposure. Final product capture
            # independently includes actual package bytes and enforces all bounds.
            active_bytes = sum(info.st_size for path, (info, _) in records.items()
                               if path != shared.ARCHIVE[0])
            require(active_bytes - len(original) + len(derived) <= 96 * 1024 * 1024, 'derived closure quota')
            for relative, expected in records.items():
                require(shared.same_record(shared.read_regular(prefix / relative, prefix, source_tree), expected),
                        'vendor closure drift before publication')
            source_tree.check(prefix / 'bin')
            require(not os.path.lexists(prefix / RECEIPT), 'derivation receipt appeared before publication')
            stage_tree.check(stage)
            require(shared.identity(file_bytes(output, stage_tree)[0]) == shared.identity(derived_info),
                    'staged output drift')
            receipt = {'schema': 1, 'prefix': str(prefix), 'operation': ['--set-rpath', RUNPATH],
                       'vendor_executable': list(shared.EXECUTABLE), 'vendor_shared': list(shared.SHARED),
                       'vendor_executable_identity': list(shared.identity(original_info)),
                       'tool_source': TOOL_URL, 'tool_version': '0.19.2',
                       'tool_identity': list(shared.identity(tool_info)),
                       'tool_archive': list(TOOL_ARCHIVE), 'tool_binary': list(TOOL_BINARY),
                       'derived': {'bytes': len(derived), 'sha256': digest(derived),
                                   'identity': list(shared.identity(derived_info))},
                       'elf_before': before_elf, 'elf_after': after_elf,
                       'claim': 'CI derived executable; unchanged vendor shared/stdlib; no OS isolation'}
            # No selected process may be active. No rollback claim on later error.
            os.replace(output.name, 'python3.12', src_dir_fd=stage_tree.entries[stage][0],
                       dst_dir_fd=source_tree.entries[prefix / 'bin'][0])
            fd = os.open('python3.12', os.O_RDONLY | os.O_NOFOLLOW,
                         dir_fd=source_tree.entries[prefix / 'bin'][0])
            try:
                published = os.fstat(fd)
                require(all(getattr(published, key) == getattr(derived_info, key)
                            for key in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_mode',
                                        'st_uid', 'st_gid', 'st_nlink')), 'published identity mismatch')
                # Our rename can change ctime; bind the actual published identity.
                receipt['derived']['identity'] = list(shared.identity(published))
                actual = b''
                while chunk := os.read(fd, 1024 * 1024):
                    actual += chunk
                    require(len(actual) <= len(derived), 'published executable read bound')
                require(actual == derived and shared.identity(os.fstat(fd)) == shared.identity(published),
                        'published executable byte drift')
            finally:
                os.close(fd)
            source_tree.check(prefix)
            write_private(RECEIPT, (json.dumps(receipt, sort_keys=True) + '\n').encode(), 0o400,
                          dir_fd=source_tree.entries[prefix][0])
            return receipt
        finally:
            source_tree.close()
            stage_tree.close()


def read_receipt(prefix):
    tree = shared.HeldDirectories(prefix)
    try:
        receipt_info, raw = file_bytes(prefix / RECEIPT, tree, max_bytes=16384)
        require(stat.S_IMODE(receipt_info.st_mode) == 0o400, 'unsealed derivation receipt')
        require(len(raw) <= 16384, 'derivation receipt bound')
        try:
            receipt = json.loads(raw)
        except (ValueError, RecursionError) as error:
            raise RuntimeError('malformed derivation receipt') from error
        require(isinstance(receipt, dict), 'derivation receipt object required')
        require(set(receipt) == {'schema', 'prefix', 'operation', 'vendor_executable', 'vendor_shared',
                                'vendor_executable_identity', 'tool_source', 'tool_version', 'tool_identity',
                                'tool_archive', 'tool_binary', 'derived', 'elf_before', 'elf_after', 'claim'},
                'derivation receipt fields')
        require(type(receipt['schema']) is int and receipt['schema'] == 1 and receipt['prefix'] == str(prefix)
                and receipt['operation'] == ['--set-rpath', RUNPATH]
                and receipt['vendor_executable'] == list(shared.EXECUTABLE)
                and receipt['vendor_shared'] == list(shared.SHARED)
                and receipt['tool_archive'] == list(TOOL_ARCHIVE)
                and receipt['tool_binary'] == list(TOOL_BINARY)
                and receipt['tool_source'] == TOOL_URL and receipt['tool_version'] == '0.19.2',
                'derivation provenance mismatch')
        for key, expected_size in (('vendor_executable_identity', shared.EXECUTABLE[1]),
                                   ('tool_identity', TOOL_BINARY[0])):
            recorded = receipt[key]
            require(isinstance(recorded, list) and len(recorded) == 8
                    and all(type(value) is int and value >= 0 for value in recorded)
                    and recorded[2] == expected_size and recorded[6] == os.geteuid(),
                    'derivation source identity receipt')
        require(stat.S_IMODE(receipt['tool_identity'][5]) == 0o500, 'unsealed tool receipt')
        require(receipt['claim'] == 'CI derived executable; unchanged vendor shared/stdlib; no OS isolation'
                and set(receipt['derived']) == {'bytes', 'sha256', 'identity'}, 'derivation receipt claims')
        info, current = file_bytes(prefix / shared.EXECUTABLE[0], tree)
        require(receipt['derived'] == {'bytes': len(current), 'sha256': digest(current),
                                      'identity': list(shared.identity(info))}, 'derived receipt identity mismatch')
        after = elf(current)
        require(after == receipt['elf_after'] and after['runpath'] == [RUNPATH] and not after['rpath']
                and all(after[key] == receipt['elf_before'][key]
                        for key in ('class', 'endian', 'osabi', 'abi_version', 'flags', 'type',
                                    'machine', 'interpreter', 'needed'))
                and after['needed'] == ['libpython3.12.so.1.0', 'libc.so.6']
                and after['interpreter'] == '/lib64/ld-linux-x86-64.so.2'
                and after['type'] in {2, 3} and after['machine'] == 62
                and receipt['elf_before']['runpath'] == [str(prefix / 'lib')]
                and not receipt['elf_before']['rpath'], 'derived receipt ELF mismatch')
        return receipt
    finally:
        tree.close()


def main():
    require(os.environ.get('GITHUB_ACTIONS') == 'true'
            and os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted', 'fresh hosted runner required')
    require(sys.platform == 'linux' and os.uname().machine == 'x86_64'
            and sys.base_prefix == '/usr' and sys.flags.isolated and sys.flags.no_site
            and sys.flags.dont_write_bytecode, 'isolated Ubuntu system Python required')
    prefix = shared.TOOLCACHE / 'Python/3.12.15/x64'
    require(os.environ.get('RUNNER_TOOL_CACHE') == str(shared.TOOLCACHE)
            and os.environ.get('pythonLocation') == str(prefix), 'exact selected hosted prefix required')
    print(json.dumps(derive(prefix, download_tool())))


if __name__ == '__main__':
    main()
