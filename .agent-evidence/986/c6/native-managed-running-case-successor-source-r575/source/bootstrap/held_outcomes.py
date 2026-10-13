'Preserve real no-proof holds without converting process death into closure.'
import hashlib
import json
import os
from pathlib import Path
import select
import sqlite3
import stat

PHYSICAL = Path('/tmp/seraph-1009-n12-runtime')
FIXTURE = Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/managed-execution')


def applicability(state, native_selector):
    if state == 'unknown_external_effect' and native_selector is not None:
        return {'applicable': False, 'reason': 'actual_Unknown_quarantine_follows_executor_return_not_native_delay'}
    return {'applicable': True}


def assert_backend_stopped(manager):
    'Must be called before private negative adapter touches SQL/material.'
    if not getattr(manager, 'stopped_backend', None):
        raise RuntimeError('retained_backend_death_receipt_required')
    receipt = manager.stopped_backend
    poll = select.poll(); poll.register(receipt['pidfd'], select.POLLIN)
    if not poll.poll(0):
        raise RuntimeError('owned_backend_not_dead')
    path = Path('/proc') / str(receipt['pid'])
    if path.exists():
        start = int((path / 'stat').read_text().rsplit(')', 1)[1].split()[19])
        if start == receipt['start']:
            raise RuntimeError('backend_still_present_before_mutation')
    actual = manager.db.stat()
    if [actual.st_dev, actual.st_ino] != receipt['database_identity']:
        raise RuntimeError('same_database_identity_changed')
    return receipt['database_identity']


def preserve(context, manager, *, root_id, actual_denial, native_pidfds):
    'No cleanup/signal; store literal readonly owner/DB/process hold evidence.\n\n    actual_denial is the actual authenticated original-helper response, never\n    a synthetic held transition. Native pidfds were retained at observed READY.\n    '
    if not isinstance(actual_denial, dict) or not isinstance(actual_denial.get('detail'), dict):
        raise RuntimeError('actual_original_denial_required')
    if context['journey_directory'] != str(FIXTURE / 'journey'):
        raise RuntimeError('literal_hold_receipt_directory_required')
    directory = FIXTURE / 'journey'
    info = directory.lstat()
    if directory.resolve(strict=True) != directory or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeError('private_hold_directory_required')
    db = manager.db
    if db != PHYSICAL / 'workspace/seraph.db' or db.resolve(strict=True) != db:
        raise RuntimeError('literal_same_database_required')
    before = db.stat()
    connection = sqlite3.connect('file:' + str(db) + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute('BEGIN')
        rows = connection.execute('SELECT * FROM workflow_run_states WHERE run_identity=?', (root_id,)).fetchall()
        if len(rows) != 1:
            raise RuntimeError('authentic_original_root_missing')
        root = dict(rows[0])
        journal = json.loads(root['checkpoint_receipts_json'] or '[]')
        reservations = [entry['payload'] for entry in journal if entry['checkpoint_id'] in
                        {'repo-repair-execution-reservation', 'repo-repair-execution-release'}]
        if not reservations or reservations[-1]['status'] != 'held':
            raise RuntimeError('actual_source_liability_not_held')
        processes = []
        for item in native_pidfds:
            poll = select.poll(); poll.register(item['pidfd'], select.POLLIN)
            dead = bool(poll.poll(0))
            # A dead pidfd establishes death only, never EOF/ECHILD/durability.
            processes.append({'pid': item['pid'], 'start': item['start'], 'pidfd_dead': dead})
        after = db.stat()
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise RuntimeError('hold_database_identity_changed')
        raw_root = json.dumps(root, sort_keys=True, default=lambda value: {'blob_hex': bytes(value).hex()})
        receipt = {'root_id': root_id, 'actual_denial': actual_denial, 'reservation': reservations[-1],
                   'full_root_row_sha256': hashlib.sha256(raw_root.encode()).hexdigest(),
                   'database_identity': [before.st_dev, before.st_ino], 'native_processes': processes,
                   'completion_proven': False, 'fixture_preserved': True, 'signal_sent': False,
                   'archive_remove_authorized': False}
    finally:
        connection.close()
    path = directory / 'held-outcome.json'
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        raw = (json.dumps(receipt, sort_keys=True) + '\n').encode()
        offset = 0
        while offset < len(raw):
            count = os.write(fd, raw[offset:])
            if count <= 0:
                raise OSError('held_receipt_write_incomplete')
            offset += count
        os.fsync(fd)
    finally:
        os.close(fd)
    return receipt
