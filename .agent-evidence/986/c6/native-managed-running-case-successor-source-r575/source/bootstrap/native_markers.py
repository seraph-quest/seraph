'Read transient diagnostics against actual committed registration, never proof.'
import json
import os
from pathlib import Path
import stat
import time

EPOCHS = Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/n12/managed-observed-epochs')


def selected(context, registration):
    case = context['native_case_id']
    if not isinstance(case, str) or not case or any(value not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-' for value in case):
        raise AssertionError('literal_native_case_changed')
    directory = EPOCHS / case / 'diagnostics'
    if context['native_diagnostic_directory'] != str(directory):
        raise AssertionError('literal_native_diagnostic_directory_changed')
    metadata = directory.lstat()
    if (directory.resolve(strict=True) != directory or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) != 0o700):
        raise AssertionError('private_native_diagnostic_directory_unproved')
    names = sorted(directory.iterdir())
    if len(names) > 24:
        raise AssertionError('native_marker_count_exceeded')
    total = 0
    found = []
    for path in names:
        if path.name not in {str(index).zfill(2) + '.json' for index in range(1, 25)}:
            raise AssertionError('unexpected_native_diagnostic_filename')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid()
                    or stat.S_IMODE(before.st_mode) != 0o600 or before.st_nlink != 1):
                raise AssertionError('native_marker_alias_or_metadata_changed')
            raw = os.read(fd, 3073)
            after = os.fstat(fd)
            if before.st_size != len(raw) or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                return None  # Writer has not closed its transient diagnostic FD.
        finally:
            os.close(fd)
        total += len(raw)
        if total > 3072:
            raise AssertionError('native_marker_byte_limit_exceeded')
        if not raw.endswith(b'\n'):
            return None  # Retry until bounded wait; missing markers fail observation.
        value = json.loads(raw)
        if set(value) != {'case', 'label', 'ordinal', 'nonce', 'pid', 'start', 'cutoff'}:
            raise AssertionError('native_marker_grammar_changed')
        ready = registration['ready']
        if (value['case'] != context['native_case_id'] or value['ordinal'] != int(path.stem)
                or value['nonce'] != ready['nonce'] or value['pid'] != ready['pid']
                or value['start'] != ready['start_identity']
                or value['cutoff'] != registration['monotonic_deadline']):
            raise AssertionError('native_marker_committed_owner_or_cutoff_changed')
        if value['label'] == context['reviewed_case']['child_observer_selector'] + ':returned':
            proc = Path('/proc') / str(value['pid'])
            ticks = (proc / 'stat').read_text().rsplit(')', 1)[1].split()[19]
            if ticks != value['start'] or time.monotonic() >= value['cutoff']:
                raise AssertionError('native_selected_process_or_cutoff_not_live')
            found.append(value)
    if len(found) > 1:
        raise AssertionError('native_selected_return_marker_duplicated')
    return found[0] if found else None
