'Test-only pre-application server/helper boundary. Failure exits, never continues.\n\nThis hook requires reviewed cached Python startup closure: site runs .pth before\nsitecustomize. It makes no claim to guard an unreviewed .pth startup path.\n'
import hashlib
import errno
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
import stat

_FAILURE_PHASE = 'bootstrap_entry'
_FIXTURE = Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/managed-execution')
_FAILURE_CODES = {'private_managed_no_sync_required', 'dotenv_helper_drift',
    'private_context_required', 'unreviewed_python_role_or_early_application_import',
    'startup_not_single_thread', 'startup_path_drift', 'interpreter_drift',
    'guard_source_drift', 'unsupported_server_guard_platform', 'descriptor_frontier_drift',
    'guard_ack_changed', 'inherited_denial_probe_drift', 'backend_hook_source_drift',
    'closure_diagnostic_source_drift', 'managed_observer_source_drift', 'guard_ack_timeout'}


def _startup_failure(error):
    'Failure observation only. Never emits a startup receipt or authority.\n\n    Capture the original FD frontier BEFORE opening diagnostic descriptors.\n    No traceback/locals/environment/nonce is serialized. Diagnostic errors\n    are swallowed by the caller, which unconditionally exits78.\n    '
    message = str(error)
    context = {}
    context_path = _FIXTURE / 'context.json'
    info = context_path.lstat()
    if (context_path.resolve(strict=True) == context_path and stat.S_ISREG(info.st_mode)
            and info.st_uid == os.getuid() and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_size <= 16 * 1024 * 1024):
        context = json.loads(context_path.read_bytes())
    sensitive = [value for value in (os.environ.get('OPERATOR_AUTH_SECRET'), context.get('nonce'))
                 if isinstance(value, str)]

    def private_strings(values):
        raw = json.dumps(values, separators=(',', ':')).encode()
        if (not isinstance(values, list) or len(values) > 128 or len(raw) > 32768
                or any(not isinstance(value, str) or len(value) > 4096
                       or any(secret in value for secret in sensitive) for value in values)):
            return {'capture': 'bounded_or_sensitive_omission', 'sha256': hashlib.sha256(raw).hexdigest()}
        return values

    argv = [value.decode() for value in Path('/proc/self/cmdline').read_bytes().rstrip(b'\0').split(b'\0')]
    cwd = str(Path.cwd())
    pid = os.getpid()
    process = Path('/proc/self/stat').read_text()
    start = int(process[process.rfind(')') + 2:].split()[19])
    fds = {}
    names = os.listdir('/proc/self/fd')
    if len(names) > 128:
        raise ValueError('diagnostic_fd_bound')
    for name in names:
        try:
            descriptor = int(name)
            info = os.fstat(descriptor)
            target_before = os.readlink('/proc/self/fd/' + name)
            open_flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
            descriptor_flags = fcntl.fcntl(descriptor, fcntl.F_GETFD)
            target_after = os.readlink('/proc/self/fd/' + name)
            after = os.fstat(descriptor)
            fields = ('st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_nlink',
                      'st_size', 'st_mtime_ns', 'st_ctime_ns')
            stable = (target_before == target_after and all(
                getattr(info, key) == getattr(after, key) for key in fields)
                and open_flags == fcntl.fcntl(descriptor, fcntl.F_GETFL)
                and descriptor_flags == fcntl.fcntl(descriptor, fcntl.F_GETFD))
            secret_paths = [context.get(key) for key in ('local_secret_file', 'dotenv')]
            sensitive_target = (any(secret in target_before for secret in sensitive)
                or any(value and value in target_before for value in secret_paths)
                or any(part in target_before for part in
                       ('operator-secret', '.env.dev', 'private-values.json')))
            target = ('redacted_sensitive_path' if sensitive_target else
                      target_before if len(target_before) <= 4096 else 'omitted_overlong_target')
            fds[name] = {'device': info.st_dev, 'inode': info.st_ino, 'mode': info.st_mode,
                'uid': info.st_uid, 'nlink': info.st_nlink,
                'open_flags': open_flags, 'descriptor_flags': descriptor_flags,
                'target': target if stable else 'changed_during_snapshot',
                'stable_identity_and_target': stable}
        except OSError as failure:
            if failure.errno != errno.EBADF:
                raise
    proposed = []
    for role in context.get('python_roles', [])[:3]:
        name = role.get('name')
        expected_fds = role.get('fds')
        fd_keys = {'device', 'inode', 'mode', 'uid', 'nlink', 'open_flags', 'descriptor_flags'}
        if (not isinstance(expected_fds, dict) or len(expected_fds) > 128
                or any(not isinstance(key, str) or not key.isdecimal() or not isinstance(value, dict)
                       or set(value) != fd_keys or any(type(item) is not int for item in value.values())
                       for key, value in expected_fds.items())):
            expected_fds = None
        proposed.append({'name': name if name in {'schema_guard', 'workspace_helper', 'backend'} else None,
            'argv': private_strings(role.get('argv')),
            'cwd': private_strings([role.get('cwd')]), 'startup_path': private_strings(role.get('startup_path')),
            'fds': expected_fds, 'fds_equal': expected_fds == {key: {field: item[field] for field in fd_keys} for key, item in fds.items()},
            'argv_equal': role.get('argv') == argv, 'cwd_equal': role.get('cwd') == cwd})
    record = {'schema': 'managed.startup.failure.v1', 'acceptance': False,
        'phase': _FAILURE_PHASE, 'pid': pid, 'start_ticks': start,
        'exception': {'class': type(error).__module__ + '.' + type(error).__name__,
            'code': message if message in _FAILURE_CODES else None,
            'message_sha256': hashlib.sha256(message.encode()).hexdigest()},
        'actual_argv': private_strings(argv), 'actual_cwd': private_strings([cwd]),
        'actual_startup_path': private_strings(list(sys.path)), 'actual_fds': fds,
        'proposed_roles': proposed,
        'application_already_imported': any(name == 'src' or name.startswith('src.') for name in sys.modules)}
    raw = (json.dumps(record, sort_keys=True) + '\n').encode()
    if len(raw) > 65536:
        raise ValueError('diagnostic_byte_bound')
    journey = _FIXTURE / 'journey'
    before = journey.lstat()
    if (journey.resolve(strict=True) != journey or not stat.S_ISDIR(before.st_mode)
            or before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) != 0o700):
        raise ValueError('private_diagnostic_parent_required')
    parent_fd = os.open(journey, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    directory_fd = None
    output_fd = None
    try:
        if (os.fstat(parent_fd).st_dev, os.fstat(parent_fd).st_ino) != (before.st_dev, before.st_ino):
            raise ValueError('diagnostic_parent_changed')
        try:
            os.mkdir('startup-failures', mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        directory_fd = os.open('startup-failures', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
        directory = os.fstat(directory_fd)
        if directory.st_uid != os.getuid() or stat.S_IMODE(directory.st_mode) != 0o700:
            raise ValueError('private_diagnostic_directory_required')
        if len(os.listdir(directory_fd)) >= 64:
            raise ValueError('diagnostic_file_bound')
        output_fd = os.open(str(pid) + '-' + str(start) + '.json',
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600, dir_fd=directory_fd)
        os.fchmod(output_fd, 0o600)
        offset = 0
        while offset < len(raw):
            written = os.write(output_fd, raw[offset:])
            if written <= 0:
                raise OSError('diagnostic_write_incomplete')
            offset += written
        os.fsync(output_fd)
        os.fsync(directory_fd)
    finally:
        if output_fd is not None:
            os.close(output_fd)
        if directory_fd is not None:
            os.close(directory_fd)
        os.close(parent_fd)


def _startup_json_if_complete(path, *, binary=False):
    # Visible O_EXCL receipt/ACK files may still be inside their original write.
    # No incomplete or malformed JSON grants ACK, import, or role authority.
    try:
        return True, json.loads(path.read_bytes() if binary else path.read_text())
    except json.JSONDecodeError:
        return False, None


def bootstrap():
    global _FAILURE_PHASE
    _FAILURE_PHASE = 'context_read'
    context_path = Path(os.environ['SERAPH_MANAGED_TEST_CONTEXT'])
    context = json.loads(context_path.read_text())
    _FAILURE_PHASE = 'private_no_sync'
    if os.environ.get('UV_NO_SYNC') != '1':
        raise RuntimeError('private_managed_no_sync_required')
    _FAILURE_PHASE = 'dotenv_verify'
    dotenv_path = Path(__file__).with_name('managed_fixture_dotenv.py')
    if hashlib.sha256(dotenv_path.read_bytes()).hexdigest() != '8354a1c5e83a76eecad4694b8ac7fc6ad0a2a1515294f765ae54568fc868b9be':
        raise RuntimeError('dotenv_helper_drift')
    dotenv_spec = importlib.util.spec_from_file_location('_managed_private_dotenv', dotenv_path)
    dotenv = importlib.util.module_from_spec(dotenv_spec)
    dotenv_spec.loader.exec_module(dotenv)
    dotenv.check_loaded_environment(dotenv.read_dotenv(context['dotenv']), os.environ)
    if context_path.resolve() != context_path or context_path.stat().st_mode & 0o077:
        raise RuntimeError('private_context_required')
    _FAILURE_PHASE = 'role_match'
    argv = Path('/proc/self/cmdline').read_bytes().rstrip(b"\0").split(b"\0")
    argv = [value.decode() for value in argv]
    roles = [role for role in context['python_roles']
             if argv == role['argv'] and str(Path.cwd()) == role['cwd']]
    if len(roles) != 1 or any(name == 'src' or name.startswith('src.') for name in sys.modules):
        raise RuntimeError('unreviewed_python_role_or_early_application_import')
    role = roles[0]
    if role['name'] in {'schema_guard', 'workspace_helper'}:
        alias = _FIXTURE / 'bin/python3'
        canonical = Path('/home/pawel/.local/share/uv/python/cpython-3.12.8-linux-x86_64-gnu/bin/python3.12')
        if (argv[0] != 'python3'
                or os.environ.get('PATH') != str(_FIXTURE / 'bin') + ':/usr/bin:/bin'
                or not alias.is_symlink() or os.readlink(alias) != str(canonical)
                or alias.resolve(strict=True) != canonical
                or os.path.realpath(sys.executable) != str(canonical)
                or Path('/proc/self/exe').resolve(strict=True) != canonical
                or hashlib.sha256(canonical.read_bytes()).hexdigest() != context['executable_pins'].get(str(canonical))):
            raise RuntimeError('exact_shell_python_alias_drift')
    # Non-authority actual startup readback; no path vector is synthesized.
    import site
    startup = {'version': list(sys.version_info[:3]), 'dont_write_bytecode': sys.dont_write_bytecode,
        'isolated': sys.flags.isolated, 'no_site': sys.flags.no_site, 'executable': sys.executable,
        'python_realpath': os.path.realpath(sys.executable), 'prefix': sys.prefix, 'exec_prefix': sys.exec_prefix,
        'base_prefix': sys.base_prefix, 'base_exec_prefix': sys.base_exec_prefix,
        'sys_path': list(sys.path), 'site_packages': site.getsitepackages(), 'user_site_enabled': site.ENABLE_USER_SITE,
        'virtualenv_origin': getattr(sys.modules.get('_virtualenv'), '__file__', None)}
    if not sys.dont_write_bytecode or sys.flags.no_site or sys.version_info[:3] != (3, 12, 8):
        raise RuntimeError('original_B_normal_SITE_required')
    _FAILURE_PHASE = 'single_thread'
    if len(os.listdir('/proc/self/task')) != 1:
        raise RuntimeError('startup_not_single_thread')
    # Dedicated negative-only hook. Never a successful role receipt or ACK.
    # Existing closed positive/negative context variants, never an enable flag.
    _FAILURE_PHASE = 'startup_context_variant'
    base_keys = {'browser_args_sha256', 'browser_pins', 'client_ports', 'dotenv', 'dotenv_sha256', 'executable_pins', 'fixture_paths', 'journey_directory', 'launcher_env', 'local_secret_file', 'node_addons', 'node_children', 'node_options', 'node_roles', 'nonce', 'original_requests', 'private_controller_log', 'python_realpath', 'python_roles', 'readonly_cache_metadata', 'receipt_dir', 'reviewed_backend_process_argv', 'reviewed_browser_args', 'reviewed_browser_executable', 'reviewed_case', 'reviewed_filter_sha256', 'reviewed_frontend_process_argv', 'reviewed_vite_descendant_argv', 'source_file_pins', 'source_root', 'startup_directories', 'startup_file_pins'}
    negative_keys = {'negative_startup_role', 'negative_copy_metadata'}
    roles = context.get('python_roles')
    role_keys = {'name', 'argv', 'cwd', 'startup_path', 'fds', 'stdio_binding_state'}
    if (not isinstance(roles, list) or len(roles) != 3
            or any(not isinstance(r, dict) or set(r) != role_keys for r in roles)
            or sorted(r['name'] for r in roles) != ['backend', 'schema_guard', 'workspace_helper']):
        raise RuntimeError('exact_startup_context_roles_required')
    all_null = all(r['startup_path'] is None for r in roles)
    all_positive = all(isinstance(r['startup_path'], list) and bool(r['startup_path'])
        and all(isinstance(value, str) for value in r['startup_path']) for r in roles)
    if all_null:
        if set(context) != base_keys | negative_keys or context.get('negative_startup_role') != role['name']:
            raise RuntimeError('exact_negative_startup_context_required')
        validator_path = Path(__file__).parent.parent / 'negative_context_binding.py'
        if hashlib.sha256(validator_path.read_bytes()).hexdigest() != 'abfddd557b61f193e20564aca85217ec22019336943645c7d4fd41ea6854159c':
            raise RuntimeError('negative_startup_validator_drift')
        validator_spec = importlib.util.spec_from_file_location('_managed_negative_startup_context', validator_path)
        validator = importlib.util.module_from_spec(validator_spec)
        validator_spec.loader.exec_module(validator)
        validator.validate(context, context['launcher_env'], context['source_root'],
            dotenv.read_dotenv(context['dotenv']), hashlib.sha256(Path(context['dotenv']).read_bytes()).hexdigest())
        if context.get('negative_startup_role') != role['name']:
            raise RuntimeError('exact_negative_startup_role_required')
        if os.path.realpath(sys.executable) != context['python_realpath']:
            raise RuntimeError('interpreter_drift')
        guard_path = Path(__file__).with_name('server_guard.py')
        if hashlib.sha256(guard_path.read_bytes()).hexdigest() != '2390acb1aded0518771bf8ecb2875eea8a4a7423759ee842617e3e33ea76914d':
            raise RuntimeError('guard_source_drift')
        spec = importlib.util.spec_from_file_location('_negative_server_guard', guard_path)
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        observed = {}
        for name in os.listdir('/proc/self/fd'):
            try: observed[name] = guard.fd_identity(int(name))
            except OSError as error:
                if error.errno != guard.errno.EBADF: raise
        owned_path = Path(__file__).with_name('startup_owned_fd.py')
        if hashlib.sha256(owned_path.read_bytes()).hexdigest() != '6d6e1e5fad9700fa953af81e1802910fc98a34e51c96154f19889ca6cd370614':
            raise RuntimeError('startup_owned_helper_drift')
        owned_spec = importlib.util.spec_from_file_location('_negative_startup_owned', owned_path)
        owned = importlib.util.module_from_spec(owned_spec)
        owned_spec.loader.exec_module(owned)
        owned.verify_role(observed, role['fds'])
        digest = guard.install_filter()
        if any(name == 'src' or name.startswith('src.') or name == 'uvicorn' or name.startswith('uvicorn.') for name in sys.modules):
            raise RuntimeError('early_application_import')
        target = Path(context['journey_directory']) / ('negative-startup-' + role['name'] + '.json')
        guard.private_path(target.parent, directory=True)
        record = {'acceptance': False, 'exit_code': 78, 'role': role['name'],
            'pid': os.getpid(), 'start': guard.process_start(os.getpid()),
            'parent_pid': os.getppid(), 'parent_start': guard.process_start(os.getppid()),
            'argv': argv, 'cwd': str(Path.cwd()), 'startup': startup,
            'fds': observed, 'filter_sha256': digest,
            'application_imported': False, 'normal_receipt': False, 'ack': False,
            'helper_main': False, 'uvicorn_main': False,
            'source_context_sha256': hashlib.sha256(context_path.read_bytes()).hexdigest()}
        raw = (json.dumps(record, sort_keys=True) + '\n').encode()
        if len(raw) > 65536: raise RuntimeError('negative_startup_record_bound')
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, 'wb') as output:
            output.write(raw); output.flush(); os.fsync(output.fileno())
        os._exit(78)  # No vector comparison, normal receipt/ACK, script/-c/-m body.
    elif not all_positive or set(context) != base_keys:
        raise RuntimeError('exact_positive_startup_context_required')
    _FAILURE_PHASE = 'startup_path'
    if sys.path != role['startup_path']:
        raise RuntimeError('startup_path_drift')
    _FAILURE_PHASE = 'interpreter'
    if os.path.realpath(sys.executable) != context['python_realpath']:
        raise RuntimeError('interpreter_drift')
    _FAILURE_PHASE = 'guard_source'
    guard_path = Path(__file__).with_name('server_guard.py')
    if hashlib.sha256(guard_path.read_bytes()).hexdigest() != '2390acb1aded0518771bf8ecb2875eea8a4a7423759ee842617e3e33ea76914d':
        raise RuntimeError('guard_source_drift')
    spec = importlib.util.spec_from_file_location('_managed_test_server_guard', guard_path)
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)
    if sys.platform != 'linux' or sys.byteorder != 'little' or guard.struct.calcsize('P') != 8 or os.uname().machine != 'x86_64':
        raise RuntimeError('unsupported_server_guard_platform')
    _FAILURE_PHASE = 'descriptor_census'
    observed = {}
    for name in os.listdir('/proc/self/fd'):
        try:
            observed[name] = guard.fd_identity(int(name))
        except OSError as error:
            if error.errno != guard.errno.EBADF:
                raise
    # Each helper has its own literal complete frontier; backend uses its log
    # frontier. No descriptor-number exemptions or parent FD assumptions.
    _FAILURE_PHASE = 'descriptor_match'
    owned_path = Path(__file__).with_name('startup_owned_fd.py')
    if hashlib.sha256(owned_path.read_bytes()).hexdigest() != '6d6e1e5fad9700fa953af81e1802910fc98a34e51c96154f19889ca6cd370614':
        raise RuntimeError('startup_owned_helper_drift')
    owned_spec = importlib.util.spec_from_file_location('_managed_startup_owned_fd', owned_path)
    owned = importlib.util.module_from_spec(owned_spec)
    owned_spec.loader.exec_module(owned)
    owned.verify_role(observed, role['fds'])
    _FAILURE_PHASE = 'filter_install'
    digest = guard.install_filter()
    receipt = {'role': role['name'], 'pid': os.getpid(),
               'start': guard.process_start(os.getpid()), 'argv': argv,
               'filter_sha256': digest, 'fds': observed,
               'application_imported': False, 'nonce': context['nonce'], 'startup': startup, 'cwd': str(Path.cwd())}
    _FAILURE_PHASE = 'receipt_write'
    target = Path(context['receipt_dir']) / (str(os.getpid()) + '.json')
    guard.private_path(target.parent, directory=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(receipt, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    _FAILURE_PHASE = 'ack_wait'
    cutoff = time.monotonic() + 10
    ack = target.with_suffix('.ack')
    expected = {'nonce': context['nonce'], 'pid': receipt['pid'],
                'start_ticks': receipt['start'], 'program_sha256': digest}
    while time.monotonic() < cutoff:
        if ack.exists():
            guard.private_path(ack)
            complete, actual_ack = _startup_json_if_complete(ack, binary=True)
            if not complete:
                time.sleep(0.01)
                continue
            if actual_ack != expected:
                raise RuntimeError('guard_ack_changed')
            if role['name'] == 'backend':
                _FAILURE_PHASE = 'backend_observers'
                # Test observers import the original package before uvicorn's
                # unchanged default app-dir insertion. Only this exact sealed
                # source/cwd is projected, after startup checks and kernel ACK.
                backend_dir = _FIXTURE / 'source/backend'
                if (role['cwd'] != str(backend_dir) or Path.cwd() != backend_dir
                        or backend_dir.resolve(strict=True) != backend_dir):
                    raise RuntimeError('exact_backend_source_directory_drift')
                sys.path.insert(0, str(backend_dir))
                import subprocess
                probe_path = Path(__file__).with_name('denial_probe.py')
                if hashlib.sha256(probe_path.read_bytes()).hexdigest() != context['source_file_pins'][str(probe_path)]:
                    raise RuntimeError('inherited_denial_probe_drift')
                probe = subprocess.run([sys.executable, '-I', str(probe_path)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    close_fds=True, env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}, timeout=5, check=True)
                proof_path = Path(context['journey_directory']) / ('inherited-denial-' + str(os.getpid()) + '.json')
                proof_fd = os.open(proof_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(proof_fd, 'wb') as proof:
                    proof.write(probe.stdout); proof.flush(); os.fsync(proof.fileno())
                hook_path = Path(__file__).with_name('backend_hooks.py')
                if hashlib.sha256(hook_path.read_bytes()).hexdigest() != context['source_file_pins'][str(hook_path)]:
                    raise RuntimeError('backend_hook_source_drift')
                hook_spec = importlib.util.spec_from_file_location('_managed_original_backend_hooks', hook_path)
                hook = importlib.util.module_from_spec(hook_spec)
                hook_spec.loader.exec_module(hook)
                closure_path = Path(__file__).with_name('closure_diagnostics.py')
                if hashlib.sha256(closure_path.read_bytes()).hexdigest() != context['source_file_pins'][str(closure_path)]:
                    raise RuntimeError('closure_diagnostic_source_drift')
                closure_spec = importlib.util.spec_from_file_location('_managed_closure_diagnostics', closure_path)
                closure = importlib.util.module_from_spec(closure_spec)
                closure_spec.loader.exec_module(closure)
                restorers = [closure.install(context)]
                emit, released, policy_observed = hook.install(context, extra_restorers=restorers)
                def exact_module(name):
                    path = Path(__file__).with_name(name + '.py')
                    if hashlib.sha256(path.read_bytes()).hexdigest() != context['source_file_pins'][str(path)]:
                        raise RuntimeError('managed_observer_source_drift')
                    spec = importlib.util.spec_from_file_location('_managed_' + name, path)
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    return module
                restorers.append(exact_module('terminal_diagnostics').install(context))
                case = json.loads((Path(context['journey_directory']) / 'case.json').read_text())
                restorers.append(exact_module('backend_observer').install(context,
                    selected=case['cutpoint'], state=case['expected_original_state'],
                    emit=emit, released=released))
                # Kernel ACK already accepted above. Install once for this
                # backend generation; normal app shutdown follows request
                # quiescence and restores in exact reverse installation order.
                restorers.append(exact_module('policy_canary_transport').install(context,
                    observed=policy_observed))
            return
        time.sleep(0.01)
    raise RuntimeError('guard_ack_timeout')


try:
    bootstrap()
except BaseException as failure:
    # CPython normally reports/ignores sitecustomize errors; that is unsafe here.
    try:
        _startup_failure(failure)
    except BaseException:
        pass
    os._exit(78)
