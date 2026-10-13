'Finite filesystem compiler. Never imports applications or launches a process.\n\nRoot supplies private values and actual startup census separately. Preparation\nis an explicit second command after review, not a side effect of compilation.\n'
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys

ROOT = Path('/home/pawel/repos/seraph')
BASE = ROOT / '.agent-evidence/986/c6/n12'
INPUT_BASE = ROOT / '.agent-evidence/986/c6/native-startup-coherent-epoch-r253'
OLD = Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/native-startup-coherent-epoch-r253/preparation-inputs')
INPUTS = BASE / 'preparation-inputs'
OUT = INPUTS
VENV = ROOT / '.agent-worktrees/986-a1-descriptor-projection/.agent-evidence/986/accepted-cache-r174/.venv'
FIXTURE = BASE / 'managed-execution'
COMMITTED_SOURCE = INPUT_BASE / 'committed-source'
PRODUCT_COMMIT = 'e43ad4b20dcc2ca6baa90e31ff38146f1d50c7ea'
CASE = {'id': 'auth-refresh-running', 'cutpoint': 'cleanup_readback_sql_rollback',
        'native_selector': None, 'expected_original_state': 'running', 'physical': 'completed'}
ALIASES = {'bin/python': '/home/pawel/.local/share/uv/python/cpython-3.12.8-linux-x86_64-gnu/bin/python3.12',
           'bin/python3': 'python', 'bin/python3.12': 'python', 'lib64': 'lib'}
SYSTEM_EXECUTABLES = {
    '/usr/lib/cargo/bin/coreutils/nohup': 'f84e76ea4da1e0c4e92254bd0d7dccdabe2c8b3098e734f27997dae3ee5d8e02',
    '/usr/lib/cargo/bin/coreutils/timeout': 'f84e76ea4da1e0c4e92254bd0d7dccdabe2c8b3098e734f27997dae3ee5d8e02',
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def private_dotenv_digest(assignments, private_secret):
    values = dict(assignments)
    if 'OPERATOR_AUTH_SECRET' in values:
        raise ValueError('private_secret_must_be_root_late_input')
    values['OPERATOR_AUTH_SECRET'] = private_secret
    if any(not isinstance(value, str) or "'" in value or '\n' in value or '\r' in value for value in values.values()):
        raise ValueError('shell_literal_only_values')
    # Exact existing materializer expression: sorted, single quoted, LF,
    # UTF-8 bytes. The plaintext remains local and is never written or printed.
    raw = ''.join(name + "='" + value + "'\n" for name, value in sorted(values.items())).encode()
    return digest(raw)


def reviewed_filter_digest(helper_root, helper_pins):
    path = Path(helper_root) / 'server_guard.py'
    expected = '2390acb1aded0518771bf8ecb2875eea8a4a7423759ee842617e3e33ea76914d'
    if helper_pins.get(str(path)) != expected:
        raise ValueError('original_reviewed_filter_source_required')
    verified(path, expected)
    spec = importlib.util.spec_from_file_location('_reviewed_static_filter_only', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # Pinned stdlib imports/constants/definitions only.
    instructions = module.filter_instructions()
    if module.INSTALL_STAGE != 'not_started' or module.INSTALL_ERRNO is not None:
        raise ValueError('static_filter_must_not_install_or_initialize')
    # Original install_filter return expression, with NO installation/syscalls.
    return digest(module.canonical(instructions))


def load(path):
    return json.loads(Path(path).read_bytes())


def verified(path, pin):
    path = Path(path)
    if re.search('(?i)\\.(db|sqlite|sqlite3)(-(wal|shm|journal))?$|-(wal|shm|journal)$', path.name):
        raise ValueError('database_frontier')
    if path.resolve(strict=True) != path:
        raise ValueError('regular_origin_alias')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('regular_origin_required')
        h = hashlib.sha256()
        total = 0
        while True:
            raw = os.read(fd, 1048576)
            if not raw:
                break
            h.update(raw)
            total += len(raw)
        after = os.fstat(fd)
        fields = ('st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        if total != before.st_size or any(getattr(before, k) != getattr(after, k) for k in fields) or h.hexdigest() != pin:
            raise ValueError('origin_bytes_or_metadata_drift:' + str(path))
        return {'sha256': pin, 'mode': before.st_mode, 'uid': before.st_uid,
                'nlink': before.st_nlink, 'device': before.st_dev, 'inode': before.st_ino}
    finally:
        os.close(fd)


def write_private(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(json.dumps(value, sort_keys=True, indent=2).encode() + b'\n')
        stream.flush()
        os.fsync(stream.fileno())


def validate_recipe(manifest, expected):
    case = manifest['context']['reviewed_case']
    if manifest['case_id'] != 'auth-refresh-running' or {k: case.get(k) for k in CASE} != CASE:
        raise ValueError('exact_auth_refresh_running_matrix_required')
    requests = manifest['original_requests']
    fields = {'goal_request', 'repository_input', 'task_limits', 'sandbox_settings',
              'model_fabric_settings', 'model_output', 'idempotency_key'}
    if set(requests) != fields or requests != expected:
        raise ValueError('exact_original_recipe_required')
    if set(case) != set(CASE) | fields or any(case[k] != requests[k] for k in fields):
        raise ValueError('reviewed_case_recipe_not_bound')
    for field in fields - {'idempotency_key'}:
        if type(requests[field]) is not dict:
            raise ValueError('typed_original_request_required')
    if not isinstance(requests['idempotency_key'], str) or not requests['idempotency_key']:
        raise ValueError('idempotency_key_required')
    if not re.fullmatch('[0-9a-f]{40}', requests['repository_input']['base_commit'] or ''):
        raise ValueError('actual_repository_commit_required')
    if requests['model_output']['base_snapshot_sha256'] is not None:
        raise ValueError('source_preview_is_bound_by_original_precontact_transport')


def compile_manifest(private_path, census_path, helper_pins_path, *, negative_only=False):
    private = load(private_path)
    if set(private) != {'nonce', 'private_secret', 'source_commit', 'base_commit'}:
        raise ValueError('closed_private_late_values_required')
    if not re.fullmatch('[0-9a-f]{64}', private['nonce']) or not re.fullmatch('[A-Za-z0-9_-]{32,128}', private['private_secret']):
        raise ValueError('private_nonce_secret_shape')
    if not re.fullmatch('[0-9a-f]{40}', private['source_commit']):
        raise ValueError('root_reviewed_source_commit_required')
    if private['source_commit'] != PRODUCT_COMMIT:
        raise ValueError('exact_committed_product_required')
    census = None if negative_only else load(census_path)
    if not negative_only and set(census) != {'schema_guard', 'workspace_helper', 'backend'}:
        raise ValueError('exact_three_actual_startup_censuses_required')
    helper_pins = load(helper_pins_path)
    helpers = {str(Path(p)): pin for p, pin in helper_pins.items()}
    parents = {str(Path(p).parent) for p in helpers}
    if set(helpers) != set(load(INPUTS / 'final-helper-pins.PROPOSED.json')):
        raise ValueError('exact_complete_helper_frontier_required')
    if len(parents) != 1:
        raise ValueError('one_reviewed_bootstrap_packet_required')
    helper_root = next(iter(parents))
    for p, pin in helpers.items():
        verified(p, pin)
    manifest = load(OLD / 'input-template-r2.json')
    old_helper_root = '/home/pawel/repos/seraph/.agent-evidence/986/c6/native-startup-coherent-epoch-r253/bootstrap'
    def relocate(value):
        if isinstance(value, str):
            return value.replace(old_helper_root, helper_root).replace(str(INPUT_BASE), str(BASE)).replace('/home/pawel/repos/seraph/.agent-evidence/986/c6/native-startup-coherent-runtime-r253', '/tmp/seraph-1009-n12-runtime')
        if isinstance(value, list):
            return [relocate(v) for v in value]
        if isinstance(value, dict):
            return {relocate(k): relocate(v) for k, v in value.items()}
        return value
    manifest = relocate(manifest)
    manifest['bootstrap_pins'] = helpers
    manifest['context']['launcher_env']['UV_NO_SYNC'] = '1'
    manifest['case_id'] = 'auth-refresh-running'
    manifest['nonce'] = manifest['context']['nonce'] = private['nonce']
    manifest['context']['reviewed_case'] = CASE
    manifest['private_secret_sha256'] = digest(private['private_secret'].encode())
    manifest['context']['dotenv_sha256'] = private_dotenv_digest(manifest['dotenv_assignments'], private['private_secret'])
    manifest['context']['dotenv'] = str(FIXTURE / 'source/.env.dev')
    manifest['context']['reviewed_filter_sha256'] = reviewed_filter_digest(helper_root, helpers)
    setup_executables = {
        'bin/python3': '/home/pawel/.local/share/uv/python/cpython-3.12.8-linux-x86_64-gnu/bin/python3.12',
        'bin/uv': '/home/pawel/.local/bin/uv',
        'bin/node': '/home/pawel/repos/seraph/.agent-worktrees/986-a1-host/.agent-evidence/986/a1-upstream/node-v24.13.1-linux-x64/bin/node',
    }
    for name, target in setup_executables.items():
        manifest['links'][name] = {'kind': 'cache-file', 'target': target,
            'target_sha256': manifest['executable_pins'][target]}
    product = load(OLD / 'committed-source-map.json')
    exact = load(OLD / 'export-source-pins.json')
    if exact['product_inputs_sha256'] != digest((INPUT_BASE / 'CURRENT-PRODUCT.json').read_bytes()):
        raise ValueError('reviewed_current_source_overlay_binding_changed')
    historical = {name: record for name, record in product.items() if name.startswith('backend/')}
    if {name: record['sha256'] for name, record in historical.items()} != exact['backend_regular_files']:
        raise ValueError('full_current_backend_frontier_required')
    if {name: record['sha256'] for name, record in product.items() if name.startswith('frontend/')} != exact['frontend_regular_files']:
        raise ValueError('full_current_frontend_frontier_required')
    manifest['source_roots'] = [str(COMMITTED_SOURCE)]
    manifest['files'] = {}
    for name, record in product.items():
        origin = COMMITTED_SOURCE / name
        if record['origin'] != str(origin): raise ValueError('immutable_committed_origin_required')
        verified(origin, record['sha256'])
        manifest['files']['source/' + name] = {'source': str(origin), 'sha256': record['sha256'], 'mode': 0o700 if record['mode']=='100755' else 0o600}
    for record in manifest['files'].values():
        verified(record['source'], record['sha256'])
    metadata = {}
    for group in ('cache_pins', 'startup_pins', 'browser_pins', 'executable_pins'):
        for path, pin in manifest[group].items():
            if path in SYSTEM_EXECUTABLES:
                record = verified(path, pin)
                if pin != SYSTEM_EXECUTABLES[path] or record['uid'] != 0 or record['mode'] & 0o022 or not record['mode'] & 0o111:
                    raise ValueError('actual_host_trusted_system_metadata_required')
                metadata[path] = record
            elif path.startswith(('/home/pawel/.cache/', '/home/pawel/.local/share/uv/', str(ROOT) + '/')):
                metadata[path] = verified(path, pin)
            else:
                verified(path, pin)
    snapshot_path = ROOT / '.agent-evidence/986/c6/native-cache-baseline-root-capture-r341/readonly-cache-metadata.json'
    verified(snapshot_path, 'd43efc2e51c62608cfc3f21be5149074d6ee01e73d0273bec35b6d985c8fd996')
    snapshot = load(snapshot_path)
    for path, record in snapshot.items():
        current = verified(path, record['sha256'])
        if current != record:
            raise ValueError('sealed_cache_metadata_drift:' + path)
    if any(path in snapshot and record != snapshot[path] for path, record in metadata.items()):
        raise ValueError('cache_origin_identity_drift')
    metadata.update(snapshot)
    venv_map = load(OLD / 'venv-map.json')
    if len(venv_map) != 15122:
        raise ValueError('exact_private_cache_frontier_required')
    actual_venv = {str(p) for p in VENV.rglob('*') if p.is_file() or p.is_symlink()}
    if actual_venv != set(venv_map):
        raise ValueError('private_cache_frontier_drift')
    for path, record in venv_map.items():
        relative = str(Path(path).relative_to(VENV))
        if 'sha256' in record:
            metadata[path] = verified(path, record['sha256'])
            manifest['files']['source/backend/.venv/' + relative] = {
                'source': path, 'sha256': record['sha256'],
                'mode': record['mode'],
                'input_class': 'readonly-cache'}
        elif relative not in ALIASES or record['symlink'] != ALIASES[relative]:
            raise ValueError('exact_four_original_aliases_required')
    for relative, target in ALIASES.items():
        source = VENV / relative
        info = source.lstat()
        if not stat.S_ISLNK(info.st_mode) or os.readlink(source) != target:
            raise ValueError('original_alias_drift')
        manifest['links']['source/backend/.venv/' + relative] = {
            'kind': 'copied-venv-alias', 'source': str(source), 'target': target,
            'target_sha256': manifest['executable_pins'][target] if Path(target).is_absolute() else None,
            'source_link_identity': {'device': info.st_dev, 'inode': info.st_ino,
                'mode': info.st_mode, 'uid': info.st_uid, 'nlink': info.st_nlink}}
    manifest['readonly_cache_metadata'] = metadata
    manifest['context']['readonly_cache_metadata'] = metadata
    manifest['context']['browser_pins'] = manifest['browser_pins']
    args = ['--disable-background-networking', '--disable-component-update', '--no-first-run',
            '--no-default-browser-check', '--disable-sync', '--metrics-recording-only', '--no-sandbox']
    manifest['context']['reviewed_browser_executable'] = '/home/pawel/.cache/ms-playwright/chromium-1208/chrome-linux64/chrome'
    manifest['context']['reviewed_browser_args'] = args
    manifest['context']['browser_args_sha256'] = digest(json.dumps(args, separators=(',', ':')).encode())
    manifest['final_backend_map'] = {name.removeprefix('source/backend/'): record['sha256']
        for name, record in manifest['files'].items() if name.startswith('source/backend/')}
    manifest['context']['source_file_pins'] = dict(helpers)
    manifest['context']['source_file_pins'].update({str(FIXTURE / name): record['sha256'] for name, record in manifest['files'].items()})
    clone = FIXTURE / 'source/backend/.venv'
    manifest['context']['startup_file_pins'] = {
        str(clone / Path(path).relative_to(VENV)) if Path(path).is_relative_to(VENV) else path: pin
        for path, pin in manifest['startup_pins'].items()}
    manifest['context']['startup_directories'] = [
        str(clone / Path(path).relative_to(VENV)) if Path(path).is_relative_to(VENV) else path
        for path in manifest['context']['startup_directories']]
    manifest['context']['executable_pins'] = manifest['executable_pins']
    for role in manifest['context']['python_roles']:
        if negative_only:
            role['startup_path'] = None
            role['fds'] = None
            if role['name'] == 'backend': manifest['context']['reviewed_backend_process_argv'] = role['argv']
            continue
        observed = census[role['name']]
        if (observed['application_imported'] is not False or observed['python_realpath'] != manifest['context']['python_realpath']
                or observed['startup']['dont_write_bytecode'] is not True or observed['startup']['no_site'] != 0):
            raise ValueError('authentic_preapp_census_required')
        if role['name'] in {'schema_guard', 'workspace_helper'} and role['argv'][:2] != ['python3', '-B']:
            raise ValueError('exact_original_shell_B_alias_required')
        if role['name'] == 'backend':
            manifest['context']['reviewed_backend_process_argv'] = role['argv']
        if observed['argv'] != role['argv'] or observed['cwd'] != role['cwd']:
            raise ValueError('literal_role_census_mismatch')
        role['startup_path'] = observed['startup_path']
        role['fds'] = None  # Derived by actual preopened stdio, never synthesized.
    requests = manifest['original_requests']
    requests['repository_input']['base_commit'] = private['base_commit']
    requests['model_output']['base_snapshot_sha256'] = None
    expected = relocate(load(OLD / 'original-requests.json'))
    expected['repository_input']['base_commit'] = private['base_commit']
    expected['model_output']['base_snapshot_sha256'] = None
    manifest['context']['reviewed_case'] = dict(CASE, **requests)
    validate_recipe(manifest, expected)
    manifest['context']['original_requests'] = requests
    case_path = OUT / 'compiled-case-r2.json'
    write_private(case_path, manifest['context']['reviewed_case'])
    manifest['files']['journey/case.json'] = {'source': str(case_path),
        'sha256': digest(case_path.read_bytes()), 'mode': 0o600}
    manifest['source_roots'].append(str(OUT))
    manifest['context']['source_file_pins'][str(FIXTURE / 'journey/case.json')] = digest(case_path.read_bytes())
    write_private(OUT / 'compiled-manifest-r2.json', manifest)
    write_private(OUT / 'compiled-binding-r2.json', {'manifest_sha256': digest(canonical(manifest)),
        'source_commit': private['source_commit'], 'recipe_sha256': digest(canonical(expected)),
        'diagnostic_only': negative_only, 'census_sha256': None if negative_only else digest(Path(census_path).read_bytes()), 'helper_pins_sha256': digest(Path(helper_pins_path).read_bytes()),
        'backend_regular_members': len(manifest['final_backend_map']), 'venv_aliases': ALIASES})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('private_values')
    parser.add_argument('startup_census')
    parser.add_argument('helper_pins')
    parser.add_argument('--negative-only', action='store_true')
    options = parser.parse_args()
    compile_manifest(options.private_values, options.startup_census, options.helper_pins, negative_only=options.negative_only)


if __name__ == '__main__':
    main()
