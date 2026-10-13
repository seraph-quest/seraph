'Finite filesystem-only preparation; calling requires Root-reviewed manifest.\n\nNo interpreter/app imports, helper execution, installs, network or process launch.\nFailure retains the partial private fixture for inspection, never broad cleanup.\n'
import hashlib
import json
import os
from pathlib import Path
import re
import stat

ROOT = Path('/home/pawel/repos/seraph')
FIXTURE = ROOT / '.agent-evidence/986/c6/n12/managed-execution'
PHYSICAL = Path('/tmp/seraph-1009-n12-runtime')
ARCHIVES = ROOT / '.agent-evidence/986/c6/n12/managed-archives'
MANAGE = '213869f572840802565b09299310b4ea8bb39b416a0e919d37e9fe99e7d1bc7c'
VENV_CACHE = ROOT / '.agent-worktrees/986-a1-descriptor-projection/.agent-evidence/986/accepted-cache-r174/.venv'
PYTHON = Path('/home/pawel/.local/share/uv/python/cpython-3.12.8-linux-x86_64-gnu/bin/python3.12')
VENV_ALIASES = {'source/backend/.venv/bin/python': str(PYTHON),
    'source/backend/.venv/bin/python3': 'python', 'source/backend/.venv/bin/python3.12': 'python',
    'source/backend/.venv/lib64': 'lib'}
REQUIRED = {'case_id', 'nonce', 'files', 'links', 'source_roots', 'context',
            'bootstrap_pins', 'cache_pins', 'startup_pins', 'executable_pins',
            'browser_pins', 'final_backend_map', 'original_requests', 'native_observer',
            'private_secret_sha256', 'dotenv_assignments', 'readonly_cache_metadata'}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def private_directory(path):
    info = path.lstat()
    if (path.resolve(strict=True) != path or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError('literal_owned0700_directory_required:' + str(path))


def frontier(path):
    # BEFORE any metadata or content lookup; DBs only arise during later runtime.
    if re.search('(?i)\\.(db|sqlite|sqlite3)(-(wal|shm|journal))?$|-(wal|shm|journal)$', path.name):
        raise ValueError('database_source_frontier_forbidden')


def read(path, pin):
    path = Path(path)
    frontier(path)
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError('source_alias_forbidden')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError('unique_regular_source_required')
        chunks = []
        while True:
            block = os.read(fd, 1048576)
            if not block:
                break
            chunks.append(block)
        after = os.fstat(fd)
        raw = b''.join(chunks)
        if (len(raw) != before.st_size or any(getattr(before, name) != getattr(after, name)
                for name in ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns'))
                or digest(raw) != pin):
            raise ValueError('source_pin_or_read_identity_changed')
        return raw
    finally:
        os.close(fd)


def relative(name):
    path = Path(name)
    if not name or path.is_absolute() or str(path) != name or any(v in {'.', '..'} for v in path.parts):
        raise ValueError('literal_relative_member_required')
    frontier(path)
    return path


def absent(path):
    try:
        path.lstat()
    except FileNotFoundError:
        return
    raise ValueError('fresh_fixture_leaf_required:' + str(path))


def write(path, raw, mode=0o600):
    if not path.parent.exists():
        mkdirs(path.parent)
    # Every generated ancestor is checked again before the O_EXCL leaf open.
    ancestor = path.parent
    while ancestor in (FIXTURE, PHYSICAL) or FIXTURE in ancestor.parents or PHYSICAL in ancestor.parents:
        private_directory(ancestor)
        ancestor = ancestor.parent
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    try:
        os.fchmod(fd, mode)
        view = memoryview(raw)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError('partial_private_write')
            view = view[count:]
        os.fsync(fd)
    finally:
        os.close(fd)


def mkdirs(path):
    if path.exists():
        private_directory(path)
        return
    if path not in (FIXTURE, PHYSICAL) and FIXTURE not in path.parents and PHYSICAL not in path.parents:
        raise ValueError('generated_directory_outside_fixture')
    mkdirs(path.parent)
    path.mkdir(mode=0o700)
    private_directory(path)


def missing_inputs(manifest):
    if not isinstance(manifest, dict):
        return sorted(REQUIRED)
    return sorted(name for name in REQUIRED if name not in manifest or manifest[name] is None
                  or (name != 'links' and not manifest[name]))


def prepare(manifest, *, manifest_sha256, private_secret):
    "Create only fixed destinations after complete read-only validation.\n\n    private_secret is Root's fresh LOCAL operator secret, not ambient env.\n    Any missing authentic input returns an explicit list before mutation.\n    "
    missing = missing_inputs(manifest)
    if missing:
        return {'prepared': False, 'missing_inputs': missing}
    if set(manifest) != REQUIRED or digest(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()) != manifest_sha256:
        raise ValueError('exact_reviewed_manifest_required')
    if (not re.fullmatch('[0-9a-f]{64}', manifest['nonce'])
            or not re.fullmatch('[A-Za-z0-9_-]{1,32}', manifest['case_id'])
            or not isinstance(private_secret, str) or not re.fullmatch('[A-Za-z0-9_-]{32,128}', private_secret)
            or digest(private_secret.encode()) != manifest['private_secret_sha256']):
        raise ValueError('private_nonce_case_secret_binding_required')
    context = manifest['context']
    expected = {'source_root': str(FIXTURE / 'source'), 'journey_directory': str(FIXTURE / 'journey'),
        'receipt_dir': str(FIXTURE / 'receipts'), 'local_secret_file': str(FIXTURE / 'operator-secret'),
        'private_controller_log': str(FIXTURE / 'controller.log')}
    if any(context.get(key) != value for key, value in expected.items()) or context['nonce'] != manifest['nonce']:
        raise ValueError('fixed_private_context_changed')
    if (context['client_ports'] != [18241, 13241] or len(context['python_roles']) != 3
            or sorted(role['name'] for role in context['python_roles']) != ['backend', 'schema_guard', 'workspace_helper']):
        raise ValueError('exact_roles_ports_required')
    for group in ('bootstrap_pins', 'cache_pins', 'startup_pins', 'executable_pins', 'browser_pins'):
        for name, pin in manifest[group].items():
            if name in manifest['readonly_cache_metadata']:
                from cache_inputs import read_cache
                record = manifest['readonly_cache_metadata'][name]
                if record['sha256'] != pin:
                    raise ValueError('readonly_cache_pin_binding_changed')
                read_cache(name, record)
            else:
                read(name, pin)
    if context['readonly_cache_metadata'] != manifest['readonly_cache_metadata'] or context['browser_pins'] != manifest['browser_pins']:
        raise ValueError('browser_cache_context_not_exact_manifest')
    expected_env = {'PATH': str(FIXTURE / 'bin') + ':/usr/bin:/bin', 'LANG': 'C.UTF-8', 'TZ': 'UTC',
        'HOME': str(FIXTURE / 'home'), 'PYTHONPATH': str(ROOT / '.agent-evidence/986/c6/n12/bootstrap'),
        'NODE_OPTIONS': '--require ' + str(ROOT / '.agent-evidence/986/c6/n12/bootstrap/node_preload.cjs'),
        'SERAPH_MANAGED_TEST_CONTEXT': str(FIXTURE / 'context.json'),
        'UV_OFFLINE': '1', 'UV_PYTHON_DOWNLOADS': 'never', 'UV_NO_SYNC': '1'}
    if context['launcher_env'] != expected_env:
        raise ValueError('sealed_preimport_environment_changed')
    roots = [Path(name) for name in manifest['source_roots']]
    if any(not root.is_absolute() or root.resolve(strict=True) != root or ROOT not in root.parents for root in roots):
        raise ValueError('literal_repo_source_roots_required')
    verified = {}
    for name, item in manifest['files'].items():
        member = relative(name)
        if name in {'source/.env.dev', 'context.json', 'operator-secret', 'controller.log', 'preparation-readback.json'}:
            raise ValueError('generated_private_member_cannot_be_copied')
        if set(item) not in ({'source', 'sha256', 'mode'}, {'source', 'sha256', 'mode', 'input_class'}) or (item.get('input_class') != 'readonly-cache' and item['mode'] not in (0o600, 0o700)):
            raise ValueError('closed_source_member_record_required')
        source = Path(item['source'])
        if item.get('input_class') == 'readonly-cache':
            from cache_inputs import read_cache
            record = manifest['readonly_cache_metadata'].get(str(source))
            if (record is None or record['sha256'] != item['sha256']
                    or not name.startswith('source/backend/.venv/')
                    or VENV_CACHE not in source.parents
                    or item['mode'] != stat.S_IMODE(record['mode'])
                    or item['mode'] not in (0o664, 0o775)):
                raise ValueError('copied_cache_record_required')
            verified[name] = read_cache(source, record)
        else:
            if 'input_class' in item or not any(root in source.parents for root in roots):
                raise ValueError('source_outside_reviewed_roots')
            verified[name] = read(source, item['sha256'])
    for name in manifest['files']:
        if any(str(parent) in manifest['files'] for parent in Path(name).parents if str(parent) != '.'):
            raise ValueError('file_directory_member_collision')
    if digest(verified.get('source/manage.sh', b'')) != MANAGE:
        raise ValueError('unchanged_original_manage_required')
    if not manifest['final_backend_map'] or any('source/backend/' + name not in verified
            or digest(verified['source/backend/' + name]) != pin
            for name, pin in manifest['final_backend_map'].items()):
        raise ValueError('full_final_copied_backend_map_required')
    actual_backend = {name.removeprefix('source/backend/'): digest(raw) for name, raw in verified.items()
                      if name.startswith('source/backend/')}
    if actual_backend != manifest['final_backend_map']:
        raise ValueError('backend_members_not_exact_map')
    expected_source_pins = dict(manifest['bootstrap_pins'])
    expected_source_pins.update({str(FIXTURE / name): digest(raw) for name, raw in verified.items()})
    expected_startup_pins = {
        str(FIXTURE / 'source/backend/.venv' / Path(path).relative_to(VENV_CACHE))
        if Path(path).is_relative_to(VENV_CACHE) else path: pin
        for path, pin in manifest['startup_pins'].items()}
    if (context['source_file_pins'] != expected_source_pins
            or context['startup_file_pins'] != expected_startup_pins
            or context['executable_pins'] != manifest['executable_pins']):
        raise ValueError('postcopy_context_closure_not_exact_input_maps')
    observer = manifest['native_observer']
    if set(observer) != {'applicable', 'selector', 'generator_pin', 'hook_pin', 'same_epoch', 'six_final_pins', 'request_sha256'}:
        raise ValueError('closed_native_observer_binding_required')
    if observer['applicable'] and (not observer['same_epoch'] or len(observer['six_final_pins']) != 6
            or any(actual_backend.get(name) != pin for name, pin in observer['six_final_pins'].items())):
        raise ValueError('native_final_epoch_binding_required')
    if context['reviewed_case']['expected_original_state'] == 'unknown_external_effect' and observer['applicable']:
        raise ValueError('Unknown_after_executor_return_is_not_native_delay_crossproduct')
    links = manifest['links']
    declared_venv = {name for name, item in links.items() if item.get('kind') == 'copied-venv-alias'}
    if declared_venv != set(VENV_ALIASES):
        raise ValueError('exact_four_copied_venv_aliases_required')
    for name, item in links.items():
        relative(name)
        if any(str(parent) in verified for parent in Path(name).parents if str(parent) != '.'):
            raise ValueError('link_parent_file_collision')
        keys = {'target', 'target_sha256', 'kind'}
        if item.get('kind') == 'copied-venv-alias':
            keys |= {'source', 'source_link_identity'}
        if name in verified or set(item) != keys:
            raise ValueError('closed_unique_link_record_required')
        target = Path(item['target'])
        if item['kind'] == 'copied-venv-alias':
            source = VENV_CACHE / name.removeprefix('source/backend/.venv/')
            if item['source'] != str(source) or item['target'] != VENV_ALIASES.get(name):
                raise ValueError('literal_original_venv_alias_required')
            before = source.lstat()
            identity = {'device': before.st_dev, 'inode': before.st_ino, 'mode': before.st_mode,
                        'uid': before.st_uid, 'nlink': before.st_nlink}
            if (not stat.S_ISLNK(before.st_mode) or before.st_uid != os.getuid()
                    or before.st_nlink != 1 or identity != item['source_link_identity']
                    or os.readlink(source) != item['target'] or source.parent.resolve(strict=True) != source.parent):
                raise ValueError('original_venv_alias_identity_changed')
            if target.is_absolute():
                from cache_inputs import read_cache
                record = manifest['readonly_cache_metadata'].get(str(PYTHON))
                if (record is None or record['sha256'] != item['target_sha256']
                        or manifest['executable_pins'].get(str(PYTHON)) != item['target_sha256']):
                    raise ValueError('absolute_venv_python_cache_pin_required')
                read_cache(PYTHON, record)
            elif item['target_sha256'] is not None or '..' in target.parts or len(target.parts) != 1:
                raise ValueError('literal_contained_venv_alias_required')
            after = source.lstat()
            if any(getattr(before, key) != getattr(after, key) for key in
                   ('st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_nlink', 'st_mtime_ns', 'st_ctime_ns')):
                raise ValueError('source_venv_alias_changed_during_read')
        elif item['kind'] == 'cache-file':
            if manifest['executable_pins'].get(str(target)) != item['target_sha256']:
                raise ValueError('exact_cached_executable_link_required')
        elif item['kind'] == 'node-cache':
            if name != 'source/frontend/node_modules' or target != ROOT / 'frontend/node_modules':
                raise ValueError('exact_original_node_cache_link_required')
            if target.resolve(strict=True) != target or not target.is_dir():
                raise ValueError('node_cache_root_alias')
        else:
            raise ValueError('unsupported_external_link_kind')
    values = dict(manifest['dotenv_assignments'])
    fixed = {'LOCAL_WORKSPACE_DIR': str(PHYSICAL / 'workspace'), 'LOCAL_LLM_LOG_DIR': str(PHYSICAL / 'llm-logs'),
        'LOCAL_UV_CACHE_DIR': str(PHYSICAL / 'uv-cache'), 'LOCAL_BACKEND_PORT': '18241',
        'LOCAL_FRONTEND_PORT': '13241', 'DAEMON_ENABLED': 'false', 'PROXY_ENABLED': 'false',
        'OPERATOR_AUTH_IDLE_SECONDS': '30', 'OPERATOR_AUTH_ABSOLUTE_SECONDS': '3600',
        'OPERATOR_AUTH_ALLOW_UNAUTHENTICATED_TESTS': 'false',
        'OPERATOR_AUTH_ALLOWED_ORIGINS': 'http://127.0.0.1:13241'}
    if any(values.get(name) != value for name, value in fixed.items()) or 'OPERATOR_AUTH_SECRET' in values:
        raise ValueError('actual_private_dotenv_contract_required')
    credentials = {'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'OPENROUTER_API_KEY', 'LLM_API_KEY',
                   'FALLBACK_LLM_API_KEY', 'LOCAL_LLM_API_KEY'}
    allowed = set(fixed) | credentials | {'OPERATOR_AUTH_ALLOWED_HOSTS', 'OPERATOR_AUTH_ALLOWED_ORIGINS',
                                        'OPERATOR_AUTH_TRUSTED_PROXY_IPS'}
    if set(values) - allowed or any(values.get(key, '') for key in credentials):
        raise ValueError('provider_credentials_or_extra_assignments')
    values['OPERATOR_AUTH_SECRET'] = private_secret
    if any(not isinstance(value, str) or "'" in value or '\n' in value or '\r' in value for value in values.values()):
        raise ValueError('shell_literal_only_values')
    dotenv = ''.join(name + "='" + value + "'\n" for name, value in sorted(values.items())).encode()
    if digest(dotenv) != context['dotenv_sha256']:
        raise ValueError('sealed_dotenv_digest_changed')
    absent(FIXTURE); absent(PHYSICAL)
    if FIXTURE.parent.resolve(strict=True) != FIXTURE.parent:
        raise ValueError('durable_parent_alias')
    tmp = Path('/tmp').lstat()
    if not stat.S_ISDIR(tmp.st_mode) or not tmp.st_mode & stat.S_ISVTX or Path('/tmp').resolve(strict=True) != Path('/tmp'):
        raise ValueError('literal_sticky_tmp_required')
    if ARCHIVES.exists():
        private_directory(ARCHIVES)
    else:
        ARCHIVES.mkdir(mode=0o700)
    FIXTURE.mkdir(mode=0o700); PHYSICAL.mkdir(mode=0o700)
    for path in (FIXTURE / 'source', FIXTURE / 'home', FIXTURE / 'bin', FIXTURE / 'journey',
                 FIXTURE / 'receipts', FIXTURE / 'native-diagnostics',
                 FIXTURE / 'source/pids', FIXTURE / 'source/logs', PHYSICAL / 'workspace',
                 PHYSICAL / 'llm-logs', PHYSICAL / 'uv-cache'):
        path.mkdir(mode=0o700)
    for name, raw in verified.items():
        write(FIXTURE / name, raw, manifest['files'][name]['mode'])
    for name, item in links.items():
        destination = FIXTURE / name
        if not destination.parent.exists():
            mkdirs(destination.parent)
        private_directory(destination.parent)
        destination.symlink_to(item['target'])
    write(FIXTURE / 'source/.env.dev', dotenv)
    write(FIXTURE / 'operator-secret', (private_secret + '\n').encode())
    write(FIXTURE / 'controller.log', b'')
    # Source/cache/roles must already be exact in the manifest; never infer them
    # from application imports or process probes during preparation.
    write(FIXTURE / 'context.json', (json.dumps(context, sort_keys=True) + '\n').encode())
    members = {}
    for directory, dirs, files in os.walk(FIXTURE, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            name = str(path.relative_to(FIXTURE))
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                if name not in links or os.readlink(path) != links[name]['target']:
                    raise ValueError('postcopy_link_closure_changed')
                members[name] = {'target': os.readlink(path)}
                if links[name]['kind'] == 'copied-venv-alias':
                    expected = FIXTURE / 'source/backend/.venv/lib' if name.endswith('/lib64') else PYTHON
                    if path.resolve(strict=True) != expected:
                        raise ValueError('copied_venv_alias_traversal_changed')
            elif stat.S_ISDIR(info.st_mode):
                private_directory(path)
            elif stat.S_ISREG(info.st_mode):
                cache_record = manifest['files'].get(name, {})
                expected_mode = (cache_record.get('mode') if cache_record.get('input_class') == 'readonly-cache'
                                 and name.startswith('source/backend/.venv/') else None)
                if (info.st_uid != os.getuid() or info.st_nlink != 1
                        or (expected_mode is not None and stat.S_IMODE(info.st_mode) != expected_mode)
                        or (expected_mode is None and stat.S_IMODE(info.st_mode) not in (0o600, 0o700))):
                    raise ValueError('postcopy_file_permissions_changed')
                pin = digest(path.read_bytes())
                if name in verified and pin != digest(verified[name]):
                    raise ValueError('postcopy_source_hash_changed')
                members[name] = {'sha256': pin, 'mode': stat.S_IMODE(info.st_mode)}
            else:
                raise ValueError('unexpected_postcopy_special_file')
    if members['source/manage.sh']['sha256'] != MANAGE:
        raise ValueError('copied_original_manage_drift')
    report = {'prepared': True, 'case_id': manifest['case_id'], 'manifest_sha256': manifest_sha256,
              'members': members, 'physical_root': str(PHYSICAL), 'execution_authorized': False}
    write(FIXTURE / 'preparation-readback.json', (json.dumps(report, sort_keys=True) + '\n').encode())
    return report
