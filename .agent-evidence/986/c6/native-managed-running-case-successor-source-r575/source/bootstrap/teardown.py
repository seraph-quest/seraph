'Cooperative private original manage-down; no native signalling or broad kill.'
import hashlib
import json
import os
from pathlib import Path
import select
import shutil
import stat
import subprocess

PHYSICAL = Path('/tmp/seraph-1009-n12-runtime')
ARCHIVES = Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/managed-archives')


def identity(pid):
    path = Path('/proc') / str(pid)
    ticks = int((path / 'stat').read_text().rsplit(')', 1)[1].split()[19])
    return {'pid': pid, 'start': ticks, 'argv': (path / 'cmdline').read_bytes().rstrip(b'\0').split(b'\0'),
            'cwd': os.readlink(path / 'cwd'), 'uid': path.stat().st_uid}


def down_archive_remove(manager, context):
    if not manager.managers:
        return
    roles = manager.verify_original_managed_roles()
    roots = [roles[name]['pid'] for name in ('backend_launcher','backend_service','frontend_launcher')]
    if (manager.root / 'pids/seraph-daemon.pid').exists():
        raise RuntimeError('unexpected_daemon_teardown_owner')
    captured = {}
    frontier = list(roots)
    try:
        while frontier:
            pid = frontier.pop()
            if pid in captured:
                continue
            value = identity(pid)
            allowed = [context['reviewed_backend_process_argv'], context['reviewed_frontend_process_argv']]
            allowed += context['reviewed_vite_descendant_argv']
            allowed += [[x.decode() for x in roles['backend_launcher']['argv']]]
            if value['uid'] != os.getuid() or value['argv'] not in [[v.encode() for v in argv] for argv in allowed]:
                raise RuntimeError('unowned_or_native_live_descendant_before_down')
            fd = os.pidfd_open(pid)
            if identity(pid) != value:
                os.close(fd)
                raise RuntimeError('teardown_identity_race')
            captured[pid] = (value, fd)
            frontier += [int(v) for v in Path(f'/proc/{pid}/task/{pid}/children').read_text().split()]
        owned_sockets = set()
        for pid in captured:
            for path in Path(f'/proc/{pid}/fd').iterdir():
                try:
                    owned_sockets.add(os.readlink(path))
                except FileNotFoundError:
                    pass
        for table in ('tcp', 'tcp6'):
            for line in Path('/proc/net/' + table).read_text().splitlines()[1:]:
                fields = line.split()
                if fields[3] == '0A' and int(fields[1].split(':')[1], 16) in (18241, 13241):
                    if table != 'tcp' or not fields[1].startswith('0100007F:') or 'socket:[' + fields[9] + ']' not in owned_sockets:
                        raise RuntimeError('unowned_fixture_listener_before_original_down')
        result = subprocess.run(['./manage.sh', '-e', 'dev', 'local', 'down'], cwd=manager.root,
            env=manager.env, stdin=subprocess.DEVNULL, stdout=manager.log, stderr=manager.log,
            close_fds=True, timeout=35, check=True)
        for value, descriptor in captured.values():
            poll = select.poll(); poll.register(descriptor, select.POLLIN)
            if not poll.poll(5000):
                raise RuntimeError('original_managed_down_owned_death_unproved')
        manager.reap_log_controllers()
        for _value, original_descriptor in manager.original_managed_roles.values():
            os.close(original_descriptor)
        manager.original_managed_roles = {}
        # Archive AFTER actual process death, retaining DB and WAL bytes rather
        # than rebuilding SQL. No disposable source or Git tree lives in /tmp.
        metadata = PHYSICAL.lstat()
        if (PHYSICAL.resolve(strict=True) != PHYSICAL or not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.getuid() or metadata.st_mode & 0o077):
            raise RuntimeError('physical_fixture_disposal_identity')
        inventory = {}
        for directory, dirs, files in os.walk(PHYSICAL, followlinks=False):
            for name in dirs + files:
                path = Path(directory) / name
                info = path.lstat()
                if info.st_uid != os.getuid() or stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                    raise RuntimeError('physical_archive_frontier_not_owned_regular')
                if stat.S_ISREG(info.st_mode):
                    inventory[str(path.relative_to(PHYSICAL))] = hashlib.sha256(path.read_bytes()).hexdigest()
        archive = ARCHIVES / context['nonce']
        if archive.exists() or archive.is_symlink() or ARCHIVES.resolve(strict=True) != ARCHIVES:
            raise RuntimeError('private_archive_must_be_prepared_absent_exact')
        archive_parent = ARCHIVES.stat()
        if archive_parent.st_uid != os.getuid() or archive_parent.st_mode & 0o077:
            raise RuntimeError('private_archive_parent_required')
        shutil.copytree(PHYSICAL, archive)
        for relative, digest in inventory.items():
            if hashlib.sha256((archive / relative).read_bytes()).hexdigest() != digest:
                raise RuntimeError('physical_archive_drift')
        database = manager.db.stat()
        report = {'files': inventory, 'same_database_identity': [database.st_dev, database.st_ino],
                  'managed_down_exit': result.returncode,
                  'owned_dead': [{'pid': v['pid'], 'start': v['start']} for v, _ in captured.values()]}
        descriptor = os.open(archive / 'teardown-readback.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(report, stream, sort_keys=True); stream.flush(); os.fsync(stream.fileno())
        if (PHYSICAL.lstat().st_dev, PHYSICAL.lstat().st_ino) != (metadata.st_dev, metadata.st_ino):
            raise RuntimeError('physical_fixture_changed_before_disposal')
        shutil.rmtree(PHYSICAL)
    finally:
        for _value, descriptor in captured.values():
            os.close(descriptor)
