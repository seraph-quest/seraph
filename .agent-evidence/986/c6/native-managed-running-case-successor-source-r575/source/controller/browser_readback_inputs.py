'Trusted controller-only Inspector inputs; no product authority or API writes.'
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Mapping

FIXTURE = Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/managed-execution')
CACHE = Path('/home/pawel/repos/seraph/frontend/node_modules')
OPTIMIZED = frozenset(('react', 'react-dom', 'react-dom/client',
    'react/jsx-dev-runtime', 'react/jsx-runtime', 'motion/react',
    'zustand', 'zustand/middleware'))


@dataclass(frozen=True)
class BrowserInputs:
    paths: tuple[tuple[int, str], ...]
    screenshot: str
    source_hashes: tuple[tuple[str, str], ...]
    optimizer_hashes: tuple[tuple[str, str], ...]


def _read(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid():
            raise ValueError('unowned_browser_input')
        chunks = []
        while block := os.read(fd, 65536):
            chunks.append(block)
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
                before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size,
                after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError('browser_input_changed')
        return b''.join(chunks)
    finally:
        os.close(fd)


SCANNER = Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/bootstrap/optimizer_import_scan.mjs')
SCANNER_SHA = '7e974f8d76f5be06a266fbe8bd2acbe3681ac662f5fc95c45caef76cc711386e'
PARSER_NODE = Path('/home/pawel/repos/seraph/.agent-worktrees/986-a1-host/.agent-evidence/986/a1-upstream/node-v24.13.1-linux-x64/bin/node')
PARSER_NODE_SHA = 'd95de52ccb76fb2c5775bf176f29f3025e4b19352c092aad51ce5d930717359f'


def _assert_optimizer_imports(contents):
    'One owned metadata-only parser child; never execute an optimizer target.'
    import subprocess
    if (hashlib.sha256(_read(SCANNER)).hexdigest() != SCANNER_SHA
            or hashlib.sha256(_read(PARSER_NODE)).hexdigest() != PARSER_NODE_SHA):
        raise ValueError('browser_optimizer_parser_changed')
    payload = json.dumps({'schema': 'browser-optimizer-import-scan.v1',
        'modules': [{'name': name, 'source': body.decode('utf-8', errors='strict')}
            for name, body in contents]}, separators=(',', ':')).encode()
    if len(payload) > 33554432:
        raise ValueError('browser_optimizer_parser_bound')
    # Trusted controller-owned public syntax parser, not a managed runtime role.
    # Closed env/fds and timeout below bound this child locally. The original
    # controller policy makes no OS containment or absolute-deadline claim.
    # R3 applies separately to the offline unit invocation only.
    try:
        result = subprocess.run([str(PARSER_NODE), '--max-old-space-size=128', str(SCANNER)],
            input=payload, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'TZ': 'UTC'},
            close_fds=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError('browser_optimizer_parser_unavailable') from exc
    if result.returncode != 0 or result.stdout != b'{"schema":"browser-optimizer-import-scan.v1","ok":true}\n':
        raise ValueError('browser_optimizer_import_unowned')


def bind_inputs(context: Mapping, original: Mapping, current: Mapping,
                frontier: Mapping) -> BrowserInputs:
    'Read actual owned Vite optimizer outputs after startup, before cookies.'
    source = FIXTURE / 'source'
    journey = FIXTURE / 'journey'
    if (context['source_root'] != str(source)
            or context['journey_directory'] != str(journey)
            or not re.fullmatch('[0-9a-f]{32}', original['task_id'])
            or not re.fullmatch('repository:[0-9a-f]{64}', original['job_id'])
            or current['job_id'] != original['job_id']):
        raise ValueError('browser_original_binding_changed')
    if (source / 'frontend/node_modules').resolve() != CACHE:
        raise ValueError('browser_cache_binding_changed')
    pins = context['source_file_pins']
    paths = {'/', '/@vite/client', '/@vite/env', '/@react-refresh'}
    source_hashes = []
    for relative, expected in frontier['source_files'].items():
        if (not relative.startswith('src/') and relative not in {'index.html', 'vite.config.ts'}) or '..' in Path(relative).parts:
            raise ValueError('browser_frontier_path_invalid')
        path = source / 'frontend' / relative
        actual = hashlib.sha256(_read(path)).hexdigest()
        if actual != expected or pins[str(path)] != expected:
            raise ValueError('browser_frontend_source_changed')
        source_hashes.append((relative, actual))
        if relative.startswith('src/'):
            paths.add('/' + relative)
    deps = CACHE / '.vite/deps'
    metadata_bytes = _read(deps / '_metadata.json')
    metadata = json.loads(metadata_bytes)
    if set(metadata['optimized']) != OPTIMIZED or len(metadata['chunks']) > 32:
        raise ValueError('browser_optimizer_frontier_changed')
    filenames = [item['file'] for group in ('optimized', 'chunks')
                 for item in metadata[group].values()]
    if len(set(filenames)) != len(filenames):
        raise ValueError('browser_optimizer_duplicate')
    optimizer_hashes = [('_metadata.json', hashlib.sha256(metadata_bytes).hexdigest())]
    optimizer_contents = []
    for name in filenames:
        if not re.fullmatch('[A-Za-z0-9_-]+\\.js', name):
            raise ValueError('browser_optimizer_path_invalid')
        content = _read(deps / name)
        optimizer_contents.append((name, content))
        optimizer_hashes.append((name, hashlib.sha256(content).hexdigest()))
        paths.add('/node_modules/.vite/deps/' + name)
    _assert_optimizer_imports(optimizer_contents)
    if _read(deps / '_metadata.json') != metadata_bytes:
        raise ValueError('browser_optimizer_changed')
    for name, expected in optimizer_hashes:
        if hashlib.sha256(_read(deps / name)).hexdigest() != expected:
            raise ValueError('browser_optimizer_changed')
    task = original['task_id']
    root = original['job_id']
    paths.update(('/api/auth/session', '/api/work-board/tasks',
        '/api/work-board/events', '/api/goals/tree',
        '/api/work-board/tasks/' + task,
        '/api/work-board/tasks/' + task + '/proposals',
        '/api/workflows/repo-repair/' + root.replace(':', '%3A')))
    return BrowserInputs(tuple((13241, path) for path in sorted(paths)),
        str(journey / 'inspector.png'), tuple(sorted(source_hashes)),
        tuple(sorted(optimizer_hashes)))


def assert_actual_inspector_bound(context, http, original, current, *,
                                  frontier, inspector, client_boundary):
    'Adapter calls this instead of changing the frozen original Inspector.'
    inputs = bind_inputs(context, original, current, frontier)
    receipt = {'allowed_paths': [{'port': p, 'path': s} for p, s in inputs.paths],
               'source_hashes': dict(inputs.source_hashes),
               'optimizer_hashes': dict(inputs.optimizer_hashes),
               'screenshot': inputs.screenshot, 'methods': ['GET'],
               'websockets': 'closed', 'claim': 'trusted_client_boundary_only'}
    target = FIXTURE / 'journey/ui-readback-inputs.json'
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ValueError('browser_receipt_unowned')
        os.fchmod(fd, 0o600)
        os.ftruncate(fd, 0)
        data = (json.dumps(receipt, sort_keys=True, indent=2) + '\n').encode()
        with os.fdopen(fd, 'wb', closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)
    bound = dict(context)
    bound['browser_allowed_paths'] = receipt['allowed_paths']
    bound['private_inspector_screenshot'] = inputs.screenshot
    original_install = client_boundary.install_browser_boundary

    async def install_readonly(browser_context, backend_port, frontend_port, paths):
        await original_install(browser_context, backend_port, frontend_port, paths)

        async def request(route):
            if route.request.method != 'GET':
                await route.abort()
            else:
                await route.fallback()

        async def websocket(route):
            await route.close()

        await browser_context.route('**/*', request)
        await browser_context.route_web_socket('**/*', websocket)

    client_boundary.install_browser_boundary = install_readonly
    try:
        return inspector.assert_actual_inspector(bound, http, original, current)
    finally:
        client_boundary.install_browser_boundary = original_install
