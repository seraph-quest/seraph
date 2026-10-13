'Real managed acceptance only. No ASGITransport, fake owner or DB reseeding.\n\nThe private reviewed bootstrap supplies managed_original_journey after genuine\nproduct observer seams exist. Missing seam is a test failure, never a skip/pass.\nAll managed launches remain held until independent bootstrap review and release.\n'
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import select
import subprocess
import stat

import pytest


POSITIVE = Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/positive-output')
FIXTURE = Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/managed-execution')
MAX_CONTEXT = 16 * 1024 * 1024


def root_actual_inputs():
    import re
    path = Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/native-managed-running-case-successor-source-r575/source/controller/ROOT-ACTUAL-INPUTS.json')
    info = path.lstat()
    if path.resolve(strict=True) != path or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
        raise RuntimeError('actual_root_input_custody_required')
    value = json.loads(path.read_text())
    if set(value) != {'manifest_sha256', 'binding_sha256', 'census_sha256', 'generated_readback_sha256'} or any(type(x) is not str or re.fullmatch('[0-9a-f]{64}', x) is None for x in value.values()):
        raise RuntimeError('actual_root_input_hashes_required')
    return value


def stream_pin(path, expected, *, metadata=None, private_mode=None):
    # Never materialize the 125 MB accepted cache/library bodies.
    if path.resolve(strict=True) != path:
        raise RuntimeError('positive_closure_alias')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise RuntimeError('positive_closure_type')
        if private_mode is not None and (before.st_uid != os.getuid()
                or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) != private_mode):
            raise RuntimeError('positive_copied_permissions')
        if metadata is not None and any(getattr(before, attr) != metadata[key] for key, attr in
                [('device', 'st_dev'), ('inode', 'st_ino'), ('mode', 'st_mode'),
                 ('uid', 'st_uid'), ('nlink', 'st_nlink')]):
            raise RuntimeError('positive_readonly_metadata')
        digest = hashlib.sha256()
        while block := os.read(fd, 1048576):
            digest.update(block)
        after = os.fstat(fd)
        if digest.hexdigest() != expected or any(getattr(before, field) != getattr(after, field)
                for field in ('st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_gid', 'st_nlink',
                              'st_size', 'st_mtime_ns', 'st_ctime_ns')):
            raise RuntimeError('positive_closure_changed')
    finally:
        os.close(fd)


def positive_context():
    actual_input_hashes = root_actual_inputs()
    manifest_path = POSITIVE / 'compiled-manifest-r2.json'
    binding_path = POSITIVE / 'compiled-binding-r2.json'
    stream_pin(manifest_path, actual_input_hashes['manifest_sha256'], private_mode=0o600)
    stream_pin(binding_path, actual_input_hashes['binding_sha256'], private_mode=0o600)
    manifest = json.loads(manifest_path.read_text())
    binding = json.loads(binding_path.read_text())
    if (binding['diagnostic_only'] is not False or binding['manifest_sha256'] !=
            hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            or binding['census_sha256'] != actual_input_hashes['census_sha256']):
        raise RuntimeError('authentic_positive_compiler_required')
    # Full copied files, all links, all cache/tool identities and unchanged 37 helpers.
    for relative, record in manifest['files'].items():
        if Path(relative).is_absolute() or '..' in Path(relative).parts:
            raise RuntimeError('positive_member_escape')
        stream_pin(FIXTURE / relative, record['sha256'], private_mode=record['mode'])
    for relative, record in manifest['links'].items():
        path = FIXTURE / relative
        info = path.lstat()
        if not stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid() or os.readlink(path) != record['target']:
            raise RuntimeError('positive_copied_link_changed')
        target = path.resolve(strict=True)
        if record['target_sha256'] is not None:
            stream_pin(target, record['target_sha256'])
    for path, record in manifest['readonly_cache_metadata'].items():
        stream_pin(Path(path), record['sha256'], metadata=record)
    for group in ('bootstrap_pins', 'startup_pins', 'executable_pins', 'browser_pins', 'cache_pins'):
        for path, pin in manifest[group].items():
            stream_pin(Path(path).resolve(strict=True), pin)
    if len(manifest['bootstrap_pins']) != 37:
        raise RuntimeError('original_runtime_helper_count_changed')
    expected_source_leaves = {relative.removeprefix('source/') for relative in manifest['files']
                if relative.startswith('source/')} | {relative.removeprefix('source/')
                for relative in manifest['links'] if relative.startswith('source/')}
    expected_source_leaves.add('.env.dev')
    # Original no-CREATE runner preparation declares this generated input.
    generated_path = Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/preparation-inputs/destination-readback-r2.json')
    stream_pin(generated_path, actual_input_hashes['generated_readback_sha256'], private_mode=0o600)
    generated = json.loads(generated_path.read_text())['diagnostic_generated']
    original_log = generated['source/logs/seraph-local-backend.log']
    if (set(original_log) != {'device', 'inode', 'mode', 'uid', 'nlink', 'size'}
            or original_log['size'] != 0 or original_log['uid'] != os.getuid()
            or original_log['nlink'] != 1 or original_log['mode'] != stat.S_IFREG | 0o600):
        raise RuntimeError('original_generated_backend_log_record_changed')
    stream_pin(FIXTURE / 'source/logs/seraph-local-backend.log',
        'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855',
        metadata=original_log, private_mode=0o600)
    expected_source_leaves.add('logs/seraph-local-backend.log')
    source_root = FIXTURE / 'source'
    observed = set()
    for directory, dirs, files in os.walk(source_root, followlinks=False):
        info = Path(directory).lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise RuntimeError('positive_copied_directory_changed')
        for name in dirs + files:
            path = Path(directory) / name
            if not path.is_dir() or path.is_symlink():
                observed.add(str(path.relative_to(source_root)))
    if observed != expected_source_leaves:
        raise RuntimeError('positive_copied_source_frontier_changed')
    # No synthetic runtime fields: this is exactly the original compiler context.
    return manifest['context']


def assert_sealed_context(context, sealed):
    # Immediately before Popen, while the real sealed stdio descriptors remain live.
    if sealed['context'] is not context or sealed['actual_preopened_stdio'] is not True:
        raise RuntimeError('original_stdio_owner_required')
    path = FIXTURE / 'context.json'
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) != 0o600
                or not 0 < before.st_size <= MAX_CONTEXT):
            raise RuntimeError('authoritative_context_serialized_bound')
        raw = bytearray()
        while block := os.read(fd, min(1048576, MAX_CONTEXT + 1 - len(raw))):
            raw.extend(block)
            if len(raw) > MAX_CONTEXT:
                raise RuntimeError('authoritative_context_serialized_bound')
        after = os.fstat(fd)
        if (len(raw) != before.st_size or hashlib.sha256(raw).hexdigest() != sealed['context_sha256']
                or raw != (json.dumps(context, sort_keys=True) + '\n').encode()
                or any(getattr(before, key) != getattr(after, key) for key in
                       ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns'))):
            raise RuntimeError('actual_stdio_context_readback_changed')
    finally:
        os.close(fd)


def install_original_controller_pidfds():
    # Reuse the reviewed controller-only owner; never a PID signal fallback.
    path = Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/pidfd_compat.py')
    stream_pin(path, 'e8b7160de15c1020e7dbbf97abf364c2bf6cf34c5f467b81208bab4bc459289e', private_mode=0o600)
    spec = importlib.util.spec_from_file_location('_original_controller_pidfd_compat', path)
    compatibility = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(compatibility)
    compatibility.install()  # Actual self descriptor + send0 before Popen.


class ManagedRestart:
    'Original manage command, same DB, race-safe owned backend SIGKILL.'

    def __init__(self, *, root, env, env_sha256, log, bootstrap_dir, context, initial_stdio):
        self.root = Path(root).resolve(strict=True)
        self.env = dict(env)  # Explicit private launcher env, never os.environ.
        allowed_env = {'PATH', 'LANG', 'TZ', 'HOME', 'PYTHONPATH', 'NODE_OPTIONS',
                       'SERAPH_MANAGED_TEST_CONTEXT', 'UV_OFFLINE', 'UV_PYTHON_DOWNLOADS', 'UV_NO_SYNC'}
        if set(self.env) - allowed_env or self.env.get('UV_OFFLINE') != '1' or self.env.get('UV_PYTHON_DOWNLOADS') != 'never' or self.env.get('UV_NO_SYNC') != '1':
            raise RuntimeError('unreviewed_launcher_environment')
        self.log = log
        self.bootstrap_dir = Path(bootstrap_dir).resolve(strict=True)
        dotenv_path = self.bootstrap_dir / 'managed_fixture_dotenv.py'
        if hashlib.sha256(dotenv_path.read_bytes()).hexdigest() != '8354a1c5e83a76eecad4694b8ac7fc6ad0a2a1515294f765ae54568fc868b9be':
            raise RuntimeError('dotenv_helper_drift')
        spec = importlib.util.spec_from_file_location('_managed_test_dotenv', dotenv_path)
        dotenv = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(dotenv)
        self.dotenv = dotenv.read_dotenv(self.root / '.env.dev')
        self.db = Path(self.dotenv['LOCAL_WORKSPACE_DIR']) / 'seraph.db'
        self.pid_file = self.root / 'pids/seraph-local-backend.pid'
        self.process = None
        self.managers = []
        expected_node_options = '--require ' + str(self.bootstrap_dir / 'node_preload.cjs')
        if self.env.get('NODE_OPTIONS') != expected_node_options:
            raise RuntimeError('additional_or_missing_node_preload')
        self.context = context
        self.initial_stdio = initial_stdio
        binding_path = self.bootstrap_dir / 'context_binding.py'
        binding_spec = importlib.util.spec_from_file_location('_managed_context_binding', binding_path)
        binding = importlib.util.module_from_spec(binding_spec)
        binding_spec.loader.exec_module(binding)
        binding.validate(context, self.env, self.root, self.dotenv,
                         hashlib.sha256((self.root / '.env.dev').read_bytes()).hexdigest())
        if context['node_options'] != expected_node_options or context['dotenv'] != str(self.root / '.env.dev'):
            raise RuntimeError('private_context_binding_drift')
        if {role['name'] for role in context['python_roles']} != {'schema_guard', 'workspace_helper', 'backend'}:
            raise RuntimeError('incomplete_managed_python_roles')
        if hashlib.sha256((self.root / '.env.dev').read_bytes()).hexdigest() != env_sha256:
            raise RuntimeError('private_keyless_dotenv_drift')
        if hashlib.sha256((self.root / 'manage.sh').read_bytes()).hexdigest() != '213869f572840802565b09299310b4ea8bb39b416a0e919d37e9fe99e7d1bc7c':
            raise RuntimeError('original_manage_drift')

    def run(self):
        install_original_controller_pidfds()
        # Original manage detaches services and remains in tail -f. Restart may
        # start a second original manager, reusing its existing frontend.
        if len(self.managers) >= 2:
            raise RuntimeError('bounded_two_generation_limit')
        # Fresh fixture directories must remain private before BOTH starts.
        for name in ('pids', 'logs'):
            directory = self.root / name
            info = directory.lstat()
            if (directory.resolve(strict=True) != directory or not stat.S_ISDIR(info.st_mode)
                    or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
                raise RuntimeError('private_managed_runtime_directory_changed')
        # Every original start seals actual private stdio, including restart.
        from root_prepare import seal_preopened_stdio
        backend_log = self.root / 'logs/seraph-local-backend.log'
        if not backend_log.parent.exists():
            backend_log.parent.mkdir(mode=0o700)
        if self.initial_stdio is not None:
            null_fd, backend_fd, sealed = self.initial_stdio
            self.initial_stdio = None
        else:
            null_fd = os.open('/dev/null', os.O_RDONLY | os.O_CLOEXEC)
            try:
                backend_fd = os.open(backend_log, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            except BaseException:
                os.close(null_fd)
                raise
            try:
                role_path = self.bootstrap_dir / 'role_stdio.py'
                pin = self.context['source_file_pins'][str(role_path)]
                sealed = seal_preopened_stdio(self.context, controller_log_fd=self.log.fileno(),
                    backend_log_fd=backend_fd, devnull_fd=null_fd, role_stdio_sha256=pin)
            except BaseException:
                os.close(backend_fd); os.close(null_fd)
                raise
        try:
            # Retain the EXACT sealed O_RDONLY descriptor through Popen dup2.
            # Original close_fds closes all controller-only handles in child.
            assert_sealed_context(self.context, sealed)
            self.process = subprocess.Popen(['./manage.sh', '-e', 'dev', 'local', 'run'],
                cwd=self.root, env=self.env, stdin=null_fd,
                stdout=self.log, stderr=self.log, close_fds=True, umask=0o077)
        finally:
            os.close(backend_fd); os.close(null_fd)
        self.managers.append((self.process, os.pidfd_open(self.process.pid)))

    def _live_original_managers(self):
        if not self.managers or self.process is None:
            raise RuntimeError('original_live_manager_required')
        live = set()
        for process, descriptor in self.managers:
            poll = select.poll(); poll.register(descriptor, select.POLLIN)
            if process.poll() is None and not poll.poll(0):
                if Path(f'/proc/{process.pid}').stat().st_uid != os.getuid():
                    raise RuntimeError('original_manager_uid_changed')
                live.add(process.pid)
        if not live or self.process.pid not in live:
            raise RuntimeError('original_live_manager_required')
        return live

    def _owned_parent_chain(self, pid, managers):
        # Current kernel ancestry to actual Popen objects, never receipt permission.
        for _ in range(16):
            fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
            pid = int(fields[1])
            if pid in managers:
                return
            if pid <= 1:
                break
        raise RuntimeError('original_launcher_manager_lineage_unproved')

    def capture_original_managed_roles(self):
        # Called ONLY after original startup ACK and owned API barrier while parent lives.
        from startup_controller import backend_identity
        from teardown import identity
        managers = self._live_original_managers()
        service_pid, service_start, service_argv = backend_identity(self.context)
        if service_argv != self.context['reviewed_backend_process_argv']:
            raise RuntimeError('original_backend_role_changed')
        expected_launcher = ['uv', 'run', 'python', *service_argv[1:]]
        uv = Path('/home/pawel/.local/bin/uv').resolve(strict=True)
        pin = self.context['executable_pins'].get(str(uv))
        if pin is None:
            raise RuntimeError('original_uv_executable_pin_required')
        stream_pin(uv, pin)
        records = {}
        opened = []
        try:
            for name, path, expected in (
                    ('backend_launcher', self.pid_file, expected_launcher),
                    ('frontend_launcher', self.root/'pids/seraph-local-frontend.pid',
                     self.context['reviewed_frontend_process_argv'])):
                pid = int(path.read_text().strip())
                value = identity(pid)
                if value['argv'] != [x.encode() for x in expected] or value['uid'] != os.getuid():
                    raise RuntimeError('exact_original_launcher_role_unproved')
                self._owned_parent_chain(pid, managers)
                if name == 'backend_launcher' and Path(f'/proc/{pid}/exe').resolve(strict=True) != uv:
                    raise RuntimeError('original_uv_launcher_executable_changed')
                descriptor = os.pidfd_open(pid); opened.append(descriptor)
                if identity(pid) != value:
                    raise RuntimeError('original_launcher_capture_race')
                records[name] = (value, descriptor)
            service = identity(service_pid)
            if (service['start'] != service_start or service['argv'] != [x.encode() for x in service_argv]
                    or service['uid'] != os.getuid()):
                raise RuntimeError('original_backend_service_identity_changed')
            fields = Path(f'/proc/{service_pid}/stat').read_text().rsplit(')', 1)[1].split()
            if int(fields[1]) != records['backend_launcher'][0]['pid']:
                raise RuntimeError('original_backend_launcher_service_lineage_unproved')
            descriptor = os.pidfd_open(service_pid); opened.append(descriptor)
            if identity(service_pid) != service:
                raise RuntimeError('original_service_capture_race')
            records['backend_service'] = (service, descriptor)
            self._live_original_managers()
            previous = getattr(self, 'original_managed_roles', {})
            self.original_managed_roles = records
            for _value, old_descriptor in previous.values():
                os.close(old_descriptor)
        except BaseException:
            for descriptor in opened:
                os.close(descriptor)
            raise

    def verify_original_managed_roles(self):
        from teardown import identity
        managers = self._live_original_managers()
        records = getattr(self, 'original_managed_roles', None)
        if not records or set(records) != {'backend_launcher','frontend_launcher','backend_service'}:
            raise RuntimeError('original_live_role_capture_required')
        for name, (value, descriptor) in records.items():
            poll = select.poll(); poll.register(descriptor, select.POLLIN)
            if poll.poll(0) or identity(value['pid']) != value:
                raise RuntimeError('original_live_role_changed')
            self._owned_parent_chain(value['pid'], managers)
        for name, path in [('backend_launcher',self.pid_file),
                           ('frontend_launcher',self.root/'pids/seraph-local-frontend.pid')]:
            if int(path.read_text().strip()) != records[name][0]['pid']:
                raise RuntimeError('original_launcher_pidfile_changed')
        fields = Path(f"/proc/{records['backend_service'][0]['pid']}/stat").read_text().rsplit(')',1)[1].split()
        if int(fields[1]) != records['backend_launcher'][0]['pid']:
            raise RuntimeError('original_backend_launcher_service_lineage_changed')
        return {name:value for name,(value,_descriptor) in records.items()}

    def read_runtime_database(self, pid, start):
        def ticks():
            return int(Path(f"/proc/{pid}/stat").read_text().rsplit(')', 1)[1].split()[19])
        if ticks() != start:
            raise RuntimeError('database_owner_changed')
        environment = dict(value.split(b"=", 1) for value in
            Path(f"/proc/{pid}/environ").read_bytes().split(b"\0") if b"=" in value)
        if environment.get(b"WORKSPACE_DIR") != self.dotenv['LOCAL_WORKSPACE_DIR'].encode():
            raise RuntimeError('runtime_workspace_changed')
        info = self.db.stat()
        matches = []
        for path in Path(f"/proc/{pid}/fd").iterdir():
            try:
                target = os.readlink(path)
                observed = path.stat()
            except FileNotFoundError:
                continue
            if target == str(self.db) and (observed.st_dev, observed.st_ino) == (info.st_dev, info.st_ino):
                matches.append(path.name)
        if not matches or ticks() != start:
            raise RuntimeError('actual_runtime_database_fd_unproved')
        return info.st_dev, info.st_ino

    def kill_and_restart(self, *, pid, start, cmdline, listener_readback):
        roles = self.verify_original_managed_roles()
        service = roles['backend_service']
        if service['pid'] != pid or service['start'] != start or service['argv'] != [x.encode() for x in cmdline]:
            raise RuntimeError('managed_service_owner_changed')
        before = self.read_runtime_database(pid, start)
        descriptor = os.pidfd_open(pid)
        try:
            status = Path(f"/proc/{pid}/status").read_text()
            ticks = Path(f"/proc/{pid}/stat").read_text().rsplit(')', 1)[1].split()[19]
            actual = Path(f"/proc/{pid}/cmdline").read_bytes().rstrip(b"\0").split(b"\0")
            if (int(ticks) != start or actual != [v.encode() for v in cmdline]
                    or f"Uid:\t{os.getuid()}\t" not in status):
                raise RuntimeError('backend_pid_start_argv_uid_changed')
            # Authentic owner proves literal bind address/port/socket inode here.
            # The outbound filter itself does not constrain bind.
            listener_readback(pid, start)
            signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            death = select.poll()
            death.register(descriptor, select.POLLIN)
            if not death.poll(5000):
                raise RuntimeError('owned_backend_death_unproved')
            self.last_killed_pid = pid
        finally:
            os.close(descriptor)
        after = self.db.stat()
        assert (after.st_dev, after.st_ino) == before
        self.run()
        # Caller waits for actual new backend receipt/health, then invokes
        # read_runtime_database(new_pid,new_start) and compares this identity.
        return before

    def reap_log_controllers(self):
        expected = ['-f', str(self.root / 'logs/seraph-local-backend.log'),
                    str(self.root / 'logs/seraph-local-frontend.log'),
                    str(self.root / 'daemon/seraph-daemon.log')]
        for manager, manager_fd in self.managers:
            try:
                if manager.poll() is None:
                    children = Path(f"/proc/{manager.pid}/task/{manager.pid}/children").read_text().split()
                    for child in children:
                        child_fd = os.pidfd_open(int(child))
                        try:
                            cmd = Path(f"/proc/{child}/cmdline").read_bytes().rstrip(b"\0").split(b"\0")
                            if (os.readlink(f"/proc/{child}/exe") == '/usr/bin/tail'
                                    and cmd[1:] == [v.encode() for v in expected]
                                    and Path(f"/proc/{child}").stat().st_uid == os.getuid()):
                                signal.pidfd_send_signal(child_fd, signal.SIGTERM)
                        finally:
                            os.close(child_fd)
                    # This pidfd is captured from our Popen immediately. Never
                    # signal its process group, backend or original producer.
                    signal.pidfd_send_signal(manager_fd, signal.SIGTERM)
                manager.wait(timeout=5)
            finally:
                os.close(manager_fd)

    def down_archive_remove(self, context):
        if self.process is not None and not any(manager is self.process for manager, _fd in self.managers):
            raise RuntimeError('original_manager_pidfd_registration_unproved')
        from teardown import down_archive_remove
        down_archive_remove(self, context)


@pytest.fixture
def journey():
    # Root prepares this exact reviewed private context only after source GO.
    bootstrap = Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/bootstrap')
    path = Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/managed-execution/context.json')
    context = positive_context()
    for name in ['cache_inputs', 'browser_binding', 'policy_canary_transport', 'policy_setup', 'client_boundary', 'startup_controller', 'operator_http', 'assertions', 'inspector', 'probe_listeners', 'teardown', 'row_codec', 'native_markers', 'held_outcomes', 'controller_owned', 'root_prepare', 'journey']:
        module_path = bootstrap / (name + '.py')
        if hashlib.sha256(module_path.read_bytes()).hexdigest() != context['source_file_pins'][str(module_path)]:
            raise RuntimeError('private_journey_source_drift')
        spec = importlib.util.spec_from_file_location(name, module_path)
        module = importlib.util.module_from_spec(spec)
        import sys
        sys.modules[name] = module
        spec.loader.exec_module(module)
    from types import SimpleNamespace
    binder_path = Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/native-managed-running-case-successor-source-r575/source/controller/browser_readback_inputs.py')
    if hashlib.sha256(binder_path.read_bytes()).hexdigest() != '9169c3b1c8523a2717b41b814fe1ac5a038b06cf77f228433ca4b323ac33b179':
        raise RuntimeError('external_browser_binder_changed')
    binder_spec = importlib.util.spec_from_file_location('_r267_browser_binder', binder_path)
    binder = importlib.util.module_from_spec(binder_spec)
    sys.modules[binder_spec.name] = binder
    binder_spec.loader.exec_module(binder)
    frontier_path = Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/native-startup-coherent-epoch-r253/browser-readback-inputs-r89/frontend-frontier.json')
    if hashlib.sha256(frontier_path.read_bytes()).hexdigest() != 'b477e73e418ccca85e052232fc08540770ae6a2c7322601babcc154068803aad':
        raise RuntimeError('original_browser_frontier_changed')
    frontier = json.loads(frontier_path.read_text())
    import inspector, client_boundary
    original_inspector = SimpleNamespace(assert_actual_inspector=inspector.assert_actual_inspector)
    def bound_inspector(actual_context, http, original, current):
        return binder.assert_actual_inspector_bound(actual_context, http, original, current,
            frontier=frontier, inspector=original_inspector, client_boundary=client_boundary)
    inspector.assert_actual_inspector = bound_inspector
    from client_boundary import install_controller_boundary
    from journey import Journey
    from probe_listeners import create, assert_no_contact
    listeners = create()
    install_controller_boundary(18241, 13241)
    with open(context['private_controller_log'], 'ab', buffering=0) as log:
        from root_prepare import seal_preopened_stdio
        null_fd = os.open('/dev/null', os.O_RDONLY | os.O_CLOEXEC)
        try:
            backend_fd = os.open(FIXTURE / 'source/logs/seraph-local-backend.log',
                os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC)
        except BaseException:
            os.close(null_fd)
            raise
        manager = None
        owner = None
        transferred = False
        try:
            sealed = seal_preopened_stdio(context, controller_log_fd=log.fileno(),
                backend_log_fd=backend_fd, devnull_fd=null_fd,
                role_stdio_sha256=context['source_file_pins'][str(bootstrap / 'role_stdio.py')])
            manager = ManagedRestart(root=context['source_root'], env=context['launcher_env'],
                env_sha256=context['dotenv_sha256'], log=log, bootstrap_dir=bootstrap,
                context=context, initial_stdio=(null_fd, backend_fd, sealed))
            transferred = True
            owner = Journey(context, manager)
            yield owner
        finally:
            try:
                assert_no_contact(listeners)
                if owner is not None:
                    owner.close()
            finally:
                if not transferred or manager.initial_stdio is not None:
                    os.close(backend_fd); os.close(null_fd)
                    if manager is not None:
                        manager.initial_stdio = None
                for listener in listeners:
                    listener.close()
                inspector.assert_actual_inspector = original_inspector.assert_actual_inspector


def test_managed_child_egress_denial_precedes_job(journey):
    journey.assert_preimport_and_inherited_denial_before_admission()


@pytest.mark.parametrize('state', ['running', 'unknown_external_effect'])
def test_managed_auth_refresh_same_receipt(journey, state):
    original = journey.admit_original_via_authenticated_api(state=state)
    if state == 'unknown_external_effect':
        journey.await_actual_unknown(original)
        journey.start_original_unknown_reconcile(original)
    journey.phase_refresh_retry_restart(original)


def test_managed_retired_bearer_denied(journey):
    original = journey.admit_original_via_authenticated_api()
    journey.assert_retired_bearer_denied(original)


@pytest.mark.parametrize('kind', ['logout', 'identity_revoked', 'new_login', 'expired'])
def test_managed_current_authority_retirement_denied(journey, kind):
    original = journey.admit_original_via_authenticated_api()
    journey.assert_retired_owner_denied(original, kind)


CUTPOINTS = (
    'intent_before_popen', 'registration_commit_before_ack',
    'output_file_fsync', 'output_directory_fsync', 'completion_file_fsync',
    'material_directory_fsync', 'durability_link', 'durability_directory_fsync',
    'proof_before_notification', 'cleanup_artifact_fsync_before_append',
    'cleanup_readback_sql_rollback', 'append_commit_before_terminal_cas',
)


@pytest.mark.parametrize('cutpoint', CUTPOINTS)
def test_managed_original_producer_crash_cutpoints(journey, cutpoint):
    original = journey.admit_original_via_authenticated_api(cutpoint=cutpoint)
    journey.run_source_cutpoint_case(original, cutpoint)
