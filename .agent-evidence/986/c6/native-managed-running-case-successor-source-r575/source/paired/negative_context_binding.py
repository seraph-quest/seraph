'Fixed test context checks before original manage resolves any executable.'
import hashlib
import json
import os
from pathlib import Path
import stat

FIXTURE = Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/managed-execution')
BOOTSTRAP = Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/bootstrap')
PHYSICAL = Path('/tmp/seraph-1009-n12-runtime')
CONTEXT_ROOT = Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/n12')
ROLE_CONTEXTS={'schema_guard': '/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/schema_guard-context.json', 'workspace_helper': '/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/workspace_helper-context.json', 'backend': '/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/backend-context.json'}


def private_directory(path):
    info = path.lstat()
    if path.resolve(strict=True) != path or not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError('unowned_private_fixture_directory')


def validate(context, env, root, values, dotenv_digest):
    if Path(root) != FIXTURE / 'source':
        raise RuntimeError('unreviewed_managed_source_root')
    private_directory(FIXTURE)
    private_directory(CONTEXT_ROOT)
    private_directory(PHYSICAL)
    expected_paths = {'LOCAL_WORKSPACE_DIR': str(PHYSICAL / 'workspace'),
        'LOCAL_LLM_LOG_DIR': str(PHYSICAL / 'llm-logs'), 'LOCAL_UV_CACHE_DIR': str(PHYSICAL / 'uv-cache'),
        'LOCAL_BACKEND_PORT': '18241', 'LOCAL_FRONTEND_PORT': '13241'}
    for key, value in expected_paths.items():
        if values.get(key) != value:
            raise RuntimeError('private_fixture_dotenv_binding_changed')
        if key.endswith('_DIR'):
            private_directory(Path(value))
    expected_env = {'PATH': str(FIXTURE / 'bin') + ':/usr/bin:/bin', 'LANG': 'C.UTF-8', 'TZ': 'UTC',
        'HOME': str(FIXTURE / 'home'), 'PYTHONPATH': str(BOOTSTRAP),
        'NODE_OPTIONS': '--require ' + str(BOOTSTRAP / 'node_preload.cjs'),
        'SERAPH_MANAGED_TEST_CONTEXT': ROLE_CONTEXTS[context['negative_startup_role']],
        'UV_OFFLINE': '1', 'UV_PYTHON_DOWNLOADS': 'never', 'UV_NO_SYNC': '1'}
    if env != expected_env or context['launcher_env'] != expected_env or context['dotenv_sha256'] != dotenv_digest:
        raise RuntimeError('sealed_launcher_environment_changed')
    if context['fixture_paths'] != expected_paths or context['client_ports'] != [18241, 13241]:
        raise RuntimeError('sealed_fixture_tuple_changed')
    private_directory(FIXTURE / 'home')
    if context.get('negative_startup_role') not in {'schema_guard','workspace_helper','backend'}:
        raise RuntimeError('one_exact_negative_role_required')
    roles = context['python_roles']
    if len(roles) != 3 or sorted(r['name'] for r in roles) != ['backend', 'schema_guard', 'workspace_helper']:
        raise RuntimeError('extra_duplicate_python_role')
    helper = 'import sys; from pathlib import Path; from src.workspace.production import ProductionWorkspace, prepare_lifecycle_directory; prepare_lifecycle_directory(ProductionWorkspace(host_root=Path(sys.argv[1]).resolve()))'
    expected = {
        'schema_guard': ['python3', '-B', str(FIXTURE / 'source/scripts/operator_schema_guard.py'), str(PHYSICAL / 'workspace/seraph.db')],
        'workspace_helper': ['python3', '-B', '-c', helper, str(PHYSICAL / 'workspace')],
        'backend': [str(FIXTURE / 'source/backend/.venv/bin/python3'), '-B', '-m', 'uvicorn', 'src.app:create_app', '--factory', '--host', '127.0.0.1', '--port', '18241']}
    for role in roles:
        cwd = str(FIXTURE / 'source/backend') if role['name'] == 'backend' else str(FIXTURE / 'source')
        if role['argv'] != expected[role['name']] or role['cwd'] != cwd or role['startup_path'] is not None or not role['fds']:
            raise RuntimeError('literal_managed_role_binding_changed')
    # Full source map and finite cached/startup closure are Root-sealed inputs.
    # This verifies exact declared files; no copy or runtime discovery occurs.
    for group in ['source_file_pins', 'executable_pins', 'startup_file_pins']:
        if not context[group]:
            raise RuntimeError('missing_sealed_launch_closure')
        for path, digest in context[group].items():
            if hashlib.sha256(Path(path).read_bytes()).hexdigest() != digest:
                raise RuntimeError('sealed_launch_source_drift')
    found_pth = {str(path) for directory in context['startup_directories'] for path in Path(directory).glob('*.pth')}
    declared_pth = {path for path in context['startup_file_pins'] if path.endswith('.pth')}
    if found_pth != declared_pth:
        raise RuntimeError('startup_pth_frontier_changed')
    cache = Path('/home/pawel/repos/seraph/frontend/node_modules')
    link = FIXTURE / 'source/frontend/node_modules'
    if not link.is_symlink() or os.readlink(link) != str(cache) or link.resolve(strict=True) != cache:
        raise RuntimeError('selected_vite_cache_branch_unavailable')
    if os.readlink(cache / '.bin/vite') != '../vite/bin/vite.js':
        raise RuntimeError('cached_vite_alias_changed')
    return context
