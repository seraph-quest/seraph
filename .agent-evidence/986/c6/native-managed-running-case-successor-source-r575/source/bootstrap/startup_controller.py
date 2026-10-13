'Cooperative exact receipt ACK and actual owned listener readback.'
import json
import os
from pathlib import Path
import time


def start(pid):
    return int(Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19])


def _startup_json_if_complete(path, *, binary=False):
    # Visible O_EXCL receipt/ACK files may still be inside their original write.
    # No incomplete or malformed JSON grants ACK, import, or role authority.
    try:
        return True, json.loads(path.read_bytes() if binary else path.read_text())
    except json.JSONDecodeError:
        return False, None


def acknowledge_startups(context):
    expected_roles = {r['name']: r for r in context['python_roles']}
    acknowledged = set()
    cutoff = time.monotonic() + 10
    directory = Path(context['receipt_dir'])
    while time.monotonic() < cutoff:
        pending_publication = False
        for path in directory.glob('*.json'):
            complete, receipt = _startup_json_if_complete(path)
            if not complete:
                pending_publication = True
                continue
            pid = receipt['pid']
            if not Path(f'/proc/{pid}').exists() or start(pid) != receipt['start']:
                continue
            role = expected_roles[receipt['role']]
            startup = receipt['startup']
            if (startup['dont_write_bytecode'] is not True or startup['no_site'] != 0
                    or startup['sys_path'] != role['startup_path']
                    or startup['python_realpath'] != context['python_realpath']):
                raise AssertionError('actual_original_B_SITE_startup_drift')
            if receipt['role'] == 'backend':
                private = Path(context['source_root']) / 'backend/.venv'
                if (startup['prefix'] != str(private) or startup['exec_prefix'] != str(private)
                        or startup['virtualenv_origin'] != str(private / 'lib/python3.12/site-packages/_virtualenv.py')):
                    raise AssertionError('actual_private_backend_startup_unproved')

            status = Path(f'/proc/{pid}/status').read_text()
            if (receipt['argv'] != role['argv'] or receipt['fds'] != role['fds']
                    or receipt['application_imported'] is not False or receipt['nonce'] != context['nonce']
                    or receipt['filter_sha256'] != context['reviewed_filter_sha256']
                    or 'NoNewPrivs:\t1' not in status or 'Seccomp:\t2' not in status
                    or os.readlink(f'/proc/{pid}/cwd') != role['cwd']):
                raise AssertionError('original_preimport_receipt_drift')
            ack = path.with_suffix('.ack')
            if not ack.exists():
                fd = os.open(ack, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, 'w') as stream:
                    json.dump({'nonce': context['nonce'], 'pid': pid, 'start_ticks': receipt['start'],
                               'program_sha256': receipt['filter_sha256']}, stream)
                    stream.flush(); os.fsync(stream.fileno())
            acknowledged.add(receipt['role'])
        if acknowledged == set(expected_roles) and not pending_publication:
            return
        time.sleep(0.01)
    raise AssertionError('three_original_startup_roles_unproved')


def backend_identity(context):
    directory = Path(context['receipt_dir'])
    candidates = []
    pending_publication = False
    for path in directory.glob('*.json'):
        complete, value = _startup_json_if_complete(path)
        if not complete:
            pending_publication = True
            continue
        if value['role'] == 'backend' and Path(f"/proc/{value['pid']}").exists() and start(value['pid']) == value['start']:
            candidates.append(value)
    if len(candidates) != 1 or pending_publication:
        raise AssertionError('unique_owned_backend_required')
    value = candidates[0]
    return value['pid'], value['start'], value['argv']


def wait_owned_api(context):
    # ACK permits app import but does not constitute API readiness.
    from urllib.request import ProxyHandler, build_opener
    from urllib.error import URLError
    cutoff = time.monotonic() + 10
    opener = build_opener(ProxyHandler({}))
    while time.monotonic() < cutoff:
        try:
            pid, ticks, _argv = backend_identity(context)
            listener_readback(context, pid, ticks)
            with opener.open('http://127.0.0.1:18241/health', timeout=1) as response:
                if response.status == 200:
                    assert start(pid) == ticks
                    return
        except (URLError, FileNotFoundError, AssertionError):
            pass
        time.sleep(0.02)
    raise AssertionError('owned_managed_api_not_ready')


def listener_readback(context, pid, ticks):
    if start(pid) != ticks:
        raise AssertionError('listener_owner_start_changed')
    owned = {os.readlink(p) for p in Path(f'/proc/{pid}/fd').iterdir() if p.exists()}
    selected = []
    for name in ['tcp', 'tcp6']:
        for line in Path('/proc/net/' + name).read_text().splitlines()[1:]:
            fields = line.split()
            if fields[3] == '0A' and 'socket:[' + fields[9] + ']' in owned:
                selected.append((name, fields[1]))
    if selected != [('tcp', '0100007F:' + format(18241, '04X'))] or start(pid) != ticks:
        raise AssertionError('literal_owned_backend_listener_unproved')
