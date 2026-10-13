'Derive literal role FD identities from actual private preopened stdio.\n\nNever invent pipe inodes. Caller retains handles through the corresponding\noriginal managed spawn; actual preimport role census/receipt must match.\n'
import fcntl
import hashlib
import importlib.util
import os
from pathlib import Path
import stat

FIXTURE = Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/managed-execution')


def seal(context, *, controller_log_fd, backend_log_fd, devnull_fd):
    if (len({controller_log_fd, backend_log_fd, devnull_fd}) != 3
            or any(type(fd) is not int or fd < 3 for fd in
                   (controller_log_fd, backend_log_fd, devnull_fd))):
        raise RuntimeError('exact_retained_stdio_handles_required')
    expected = {controller_log_fd: FIXTURE / 'controller.log',
                backend_log_fd: FIXTURE / 'source/logs/seraph-local-backend.log',
                devnull_fd: Path('/dev/null')}
    identities = {}
    retained = {}
    for descriptor, path in expected.items():
        before = os.fstat(descriptor)
        actual = path.stat()
        if path.resolve(strict=True) != path or (before.st_dev, before.st_ino) != (actual.st_dev, actual.st_ino):
            raise RuntimeError('actual_preopened_stdio_identity_changed')
        flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
        if descriptor == devnull_fd:
            if not stat.S_ISCHR(before.st_mode) or flags & os.O_ACCMODE != os.O_RDONLY:
                raise RuntimeError('literal_readonly_devnull_required')
        elif (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
              or before.st_nlink != 1 or stat.S_IMODE(before.st_mode) != 0o600
              or flags & os.O_ACCMODE != os.O_WRONLY or not flags & os.O_APPEND):
            raise RuntimeError('actual_private_append_stdio_required')
        identities[descriptor] = {'device': before.st_dev, 'inode': before.st_ino,
            'mode': before.st_mode, 'uid': before.st_uid, 'nlink': before.st_nlink,
            'open_flags': flags, 'descriptor_flags': 0}
        actual_identity = dict(identities[descriptor])
        actual_identity['descriptor_flags'] = fcntl.fcntl(descriptor, fcntl.F_GETFD)
        retained[str(descriptor)] = {'identity': actual_identity,
            'target': str(path), 'stable': True}
        # Popen dup2 standard IO and unchanged shell redirections clear CLOEXEC
        # on descriptors0/1/2; actual startup census must establish this result.
    owned_path = Path(__file__).with_name('startup_owned_fd.py')
    if hashlib.sha256(owned_path.read_bytes()).hexdigest() != '6d6e1e5fad9700fa953af81e1802910fc98a34e51c96154f19889ca6cd370614':
        raise RuntimeError('startup_owned_helper_drift')
    owned_spec = importlib.util.spec_from_file_location('_managed_startup_owned_fd', owned_path)
    owned = importlib.util.module_from_spec(owned_spec)
    owned_spec.loader.exec_module(owned)
    parent_snapshot = owned.inspect_runtime()
    runtime_entry = owned.project_prepared_frontier(parent_snapshot, retained)
    roles = context['python_roles']
    if len(roles) != 3 or sorted(role['name'] for role in roles) != ['backend', 'schema_guard', 'workspace_helper']:
        raise RuntimeError('exact_three_roles_required')
    for role in roles:
        output = backend_log_fd if role['name'] == 'backend' else controller_log_fd
        role['fds'] = {'0': dict(identities[devnull_fd]), '1': dict(identities[output]), '2': dict(identities[output])}
        if role['name'] == 'backend':
            # Original manage.sh reopens this exact log with shell >>. Preserve
            # the authenticated parent's O_NOFOLLOW status in retained checks;
            # only child stdout/stderr omit that open-time bit after reopen.
            for standard in ('1', '2'):
                role['fds'][standard]['open_flags'] &= ~os.O_NOFOLLOW
        role['fds'].update(runtime_entry)
    return context
