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


class ManagedRestart:
    'Original manage command, same DB, race-safe owned backend SIGKILL.'

    def __init__(self, *, root, env, env_sha256, log, bootstrap_dir):
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
        context = json.loads(Path(self.env['SERAPH_MANAGED_TEST_CONTEXT']).read_text())
        self.context = context
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
        if hashlib.sha256((self.root / 'manage.sh').read_bytes()).hexdigest() != '6c25a178cbb6b1197a5774e478ae1bf009f56841ea209ef3d122d8f7c88d4568':
            raise RuntimeError('original_manage_drift')

    def run(self):
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
        null_fd = os.open('/dev/null', os.O_RDONLY | os.O_CLOEXEC)
        backend_fd = os.open(backend_log, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            role_path = self.bootstrap_dir / 'role_stdio.py'
            pin = self.context['source_file_pins'][str(role_path)]
            seal_preopened_stdio(self.context, controller_log_fd=self.log.fileno(),
                backend_log_fd=backend_fd, devnull_fd=null_fd, role_stdio_sha256=pin)
            # Retain the EXACT sealed O_RDONLY descriptor through Popen dup2.
            # Original close_fds closes all controller-only handles in child.
            self.process = subprocess.Popen(['./manage.sh', '-e', 'dev', 'local', 'run'],
                cwd=self.root, env=self.env, stdin=null_fd,
                stdout=self.log, stderr=self.log, close_fds=True, umask=0o077)
        finally:
            os.close(backend_fd); os.close(null_fd)
        self.managers.append((self.process, os.pidfd_open(self.process.pid)))

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
        if int(self.pid_file.read_text().strip()) != pid:
            raise RuntimeError('managed_pid_owner_changed')
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
                    str(self.root / 'daemon/.seraph-daemon.log')]
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
        from teardown import down_archive_remove
        down_archive_remove(self, context)


@pytest.fixture
def journey():
    # Root prepares this exact reviewed private context only after source GO.
    bootstrap = Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/bootstrap')
    path = Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/managed-execution/context.json')
    context = json.loads(path.read_text())
    for name in ['cache_inputs', 'browser_binding', 'policy_canary_transport', 'policy_setup', 'client_boundary', 'startup_controller', 'operator_http', 'assertions', 'inspector', 'probe_listeners', 'teardown', 'row_codec', 'native_markers', 'held_outcomes', 'controller_owned', 'root_prepare', 'journey']:
        module_path = bootstrap / (name + '.py')
        if hashlib.sha256(module_path.read_bytes()).hexdigest() != context['source_file_pins'][str(module_path)]:
            raise RuntimeError('private_journey_source_drift')
        spec = importlib.util.spec_from_file_location(name, module_path)
        module = importlib.util.module_from_spec(spec)
        import sys
        sys.modules[name] = module
        spec.loader.exec_module(module)
    from client_boundary import install_controller_boundary
    from journey import Journey
    from probe_listeners import create, assert_no_contact
    listeners = create()
    install_controller_boundary(18241, 13241)
    with open(context['private_controller_log'], 'ab', buffering=0) as log:
        manager = ManagedRestart(root=context['source_root'], env=context['launcher_env'],
            env_sha256=context['dotenv_sha256'], log=log, bootstrap_dir=bootstrap)
        owner = Journey(context, manager)
        try:
            yield owner
        finally:
            try:
                assert_no_contact(listeners)
                owner.close()
            finally:
                for listener in listeners:
                    listener.close()


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
