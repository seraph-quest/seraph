"""INERT proposal. Root review/release required; never imports a project helper."""
import ast
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

ROOT = Path('/home/pawel/repos/seraph')
PACKET = ROOT / '.agent-evidence/986/c6/native-managed-running-case-successor-source-r575'
OUT = ROOT / '.agent-evidence/986/c6/native-managed-running-case-successor-source-r575/staging'
TARGET = ROOT / '.agent-evidence/986/c6/n12'
RUNTIME = Path('/tmp/seraph-1009-n12-runtime')
LEDGER = PACKET / 'source/controller/ROOT-ACTUAL-INPUTS.json'
HEX = re.compile('[0-9a-f]{64}')
STABLE = ('st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_gid', 'st_nlink',
          'st_size', 'st_mtime_ns', 'st_ctime_ns')


def require(condition, code):
    if not condition:
        raise RuntimeError(code)


def unchanged(a, b):
    return all(getattr(a, field) == getattr(b, field) for field in STABLE)


def no_alias(path):
    require(path.is_absolute() and path.resolve(strict=True) == path,
            'canonical_input_required')


def read(path, expected=None, materialize=True, cache=False):
    """Stable streaming digest; semantic inputs stay below 64 MiB."""
    path = Path(path)
    no_alias(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and before.st_nlink >= 1,
                'regular_input_required')
        # Original compiler cache reader has no invented per-file cap. Cache
        # verification streams fixed hash-pinned inputs without materialization.
        require((cache and not materialize) or before.st_size <= 64 * 1024 * 1024,
                'input_size_bound')
        if expected is not None:
            require(set(expected) == {'sha256', 'size'} and
                    isinstance(expected['size'], int) and not isinstance(expected['size'], bool) and
                    expected['size'] >= 0 and HEX.fullmatch(expected['sha256']) and
                    before.st_size == expected['size'], 'typed_input_record')
        chunks = [] if materialize else None
        h = hashlib.sha256()
        total = 0
        while True:
            block = os.read(fd, 1048576)
            if not block:
                break
            total += len(block)
            h.update(block)
            if chunks is not None:
                chunks.append(block)
        after = os.fstat(fd)
        require(total == before.st_size and unchanged(before, after) and
                unchanged(after, path.lstat()), 'input_changed_during_read')
        if expected is not None:
            require(h.hexdigest() == expected['sha256'], 'input_hash_changed')
        return (b''.join(chunks) if chunks is not None else None), h.hexdigest(), before
    finally:
        os.close(fd)


def load(path):
    return json.loads(read(path)[0])


def verify_records(records, count):
    require(type(records) is dict and len(records) == count, 'record_count')
    for name, record in records.items():
        read(name, record, materialize=False)


def absent(path):
    require(not os.path.lexists(path), 'exclusive_destination_already_exists')



def output_chain(path, *, leaf_private=False):
    """Owned output ancestry below the fixed private evidence boundary."""
    protected = ROOT / '.agent-evidence/986'
    require(path.is_absolute() and path.is_relative_to(protected),
            'fixed_output_evidence_scope')
    # Every actual component is canonical, a directory and never a symlink.
    # System/repository ancestors retain their actual owners/modes; no chmod.
    components = list(reversed(path.parents)) + [path]
    for component in components:
        info = component.lstat()
        require(stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode) and
                component.resolve(strict=True) == component, 'output_chain_alias_or_type')
        if component == protected or component.is_relative_to(protected):
            require(info.st_uid == os.getuid(), 'output_chain_owner')
        if component == protected:
            require(stat.S_IMODE(info.st_mode) == 0o700, 'protective_evidence_mode')
    if leaf_private:
        require(stat.S_IMODE(path.lstat().st_mode) == 0o700, 'private_output_packet_mode')



@contextmanager
def held_directory(path):
    """Anchor every component, retaining canonical directory custody."""
    no_alias(path)
    descriptors = []
    try:
        current = Path('/')
        fd = os.open(current, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        descriptors.append(fd)
        for component in path.parts[1:]:
            current = current / component
            before = current.lstat()
            require(stat.S_ISDIR(before.st_mode) and not stat.S_ISLNK(before.st_mode),
                    'anchored_directory_type')
            next_fd = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                              dir_fd=fd)
            descriptors.append(next_fd)
            require(unchanged(before, os.fstat(next_fd)) and unchanged(before, current.lstat()),
                    'anchored_directory_changed')
            fd = next_fd
        yield fd
        no_alias(path)
        actual = path.lstat()
        held = os.fstat(fd)
        require((actual.st_dev, actual.st_ino, actual.st_mode, actual.st_uid, actual.st_gid) ==
                (held.st_dev, held.st_ino, held.st_mode, held.st_uid, held.st_gid),
                'anchored_directory_path_changed')
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def preflight():
    require(os.getuid() == 1000, 'actual_host_uid_required')
    output_chain(TARGET.parent)
    output_chain(OUT, leaf_private=True)
    absent(OUT / 'ROOT-stage-receipt.json')
    seals = load(Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/native-managed-running-case-successor-source-r575/staging/REQUIRED-SEALS.json'))
    verify_records(seals, 18)
    preview = load(OUT / 'REQUIRED-SEALS.PREVIEW.json')
    require(preview['source_only'] is True and preview['genuine_fixed_count'] == 15 and
            preview['required_final_count'] == 18 and len(preview['fixed_source_records']) == 15 and
            preview['future_current_review_report_record'] is None and
            preview['future_current_review_pins_record'] is None and
            preview['future_root_release_record'] is None and preview['operative_seals'] is None,
            'original_fifteen_fixed_source_seals')
    expected_seals = dict(preview['fixed_source_records'])
    release_path = Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/ROOT-native-n12-source-preflight-release-r575.json')
    release_raw, release_sha, release_info = read(release_path)
    release = json.loads(release_raw)
    require(release['state'] == 'ROOT_RELEASE_NATIVE_SOURCE_PREFLIGHT_ONLY' and
            release['packet_pins_sha256'] == read(PACKET / 'PINS.json', materialize=False)[1] and
            release['candidate_sha256'] == read(PACKET / 'CANDIDATE-PINS.json', materialize=False)[1] and
            release['reviewed_stager_sha256'] == read(Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/native-managed-running-case-successor-source-r575/staging/root-preflight-stage.py'), materialize=False)[1] and
            release['stage_release'] is None and release['native_phase_release'] is None and
            release['runtime_success'] is None and release['same_epoch_retry'] is False,
            'actual_root_source_only_release')
    def actual_record(record, expected_path=None):
        require(type(record) is dict and set(record) == {'path', 'sha256', 'size'} and
                type(record['path']) is str and Path(record['path']).is_absolute(),
                'actual_root_typed_binding')
        if expected_path is not None:
            require(record['path'] == str(expected_path), 'actual_root_binding_path')
        read(record['path'], {'sha256': record['sha256'], 'size': record['size']}, materialize=False)
        return {'sha256': record['sha256'], 'size': record['size']}
    successor_record = release['source_successor_pins']
    actual_record(successor_record, OUT.parent / 'PINS.json')
    current_report = release['current_review_report']
    current_review = release['current_review_pins']
    report_record = actual_record(current_report)
    review_record = actual_record(current_review)
    require(current_report['path'] != current_review['path'] and
            current_report['path'] not in expected_seals and current_review['path'] not in expected_seals and
            '/NATIVE-MANAGED-JOURNEY-N10-SOURCE-CRITIC-R411/' not in current_report['path'] and
            '/NATIVE-MANAGED-JOURNEY-N10-SOURCE-CRITIC-R411/' not in current_review['path'],
            'fresh_review_not_historical_approval')
    review_pins = load(Path(current_review['path']))
    # Original review PINS formats use both path-bearing records and keyed maps.
    # Match actual typed records, not prose, statuses, substrings or claimed counts.
    review_bindings = {}
    def collect_bindings(value):
        if type(value) is dict:
            if {'path', 'sha256', 'size'} <= set(value) and type(value['path']) is str:
                path = Path(value['path'])
                path = path if path.is_absolute() else ROOT / path
                review_bindings[str(path)] = {'sha256': value['sha256'], 'size': value['size']}
            for name, row in value.items():
                if type(name) is str and type(row) is dict and {'sha256', 'size'} <= set(row):
                    path = Path(name)
                    if path.is_absolute() or name.startswith('.agent-evidence/'):
                        path = path if path.is_absolute() else ROOT / path
                        review_bindings[str(path)] = {'sha256': row['sha256'], 'size': row['size']}
                collect_bindings(row)
        elif type(value) is list:
            for child in value:
                collect_bindings(child)
    collect_bindings(review_pins)
    for path, record in ((OUT.parent / 'PINS.json', actual_record(successor_record)),
                         (Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/native-managed-running-case-successor-source-r575/staging/root-preflight-stage.py'), {'sha256': release['reviewed_stager_sha256'],
                           'size': read(Path('/home/pawel/repos/seraph/.agent-evidence/986/c6/native-managed-running-case-successor-source-r575/staging/root-preflight-stage.py'), materialize=False)[2].st_size}),
                         (PACKET / 'CANDIDATE-PINS.json', {'sha256': release['candidate_sha256'],
                           'size': read(PACKET / 'CANDIDATE-PINS.json', materialize=False)[2].st_size}),
                         (Path(current_report['path']), report_record)):
        require(review_bindings.get(str(path)) == record, 'current_review_exact_source_binding')
    expected_seals[current_report['path']] = report_record
    expected_seals[current_review['path']] = review_record
    expected_seals[str(release_path)] = {'sha256': release_sha, 'size': release_info.st_size}
    require(len(expected_seals) == 18 and seals == expected_seals, 'exact_actual_seal_frontier')
    require(seals[str(PACKET / 'CANDIDATE-PINS.json')]['sha256'] ==
            '1041c0d27eeae9af77d81e5a33c44ba4aebea549e967b57e9a9ee797a61709c7', 'packet_seal')
    pins = load(PACKET / 'CANDIDATE-PINS.json')
    require(set(pins) == {'actual_fields', 'files', 'inputs', 'runtime_release', 'source_only'},
            'pins_schema')
    require(pins['source_only'] is True and pins['runtime_release'] is False and
            pins['actual_fields'] is None, 'packet_state')
    verify_records(pins['files'], 73)
    verify_records(pins['inputs'], 240)  # Every reviewed inherited source/input; no skips.
    source = load(PACKET / 'SOURCE-PINS.json')
    verify_records(source, 57)
    targets = load(PACKET / 'TARGET-SOURCE-MAP.json')
    require(targets == load(OUT / 'STAGED-DESTINATIONS.json') and len(targets) == 55,
            'exact_target_map')
    for name, record in targets.items():
        require(type(record) is dict and set(record) == {'proposal_source', 'sha256'} and
                Path(name).is_relative_to(TARGET) and Path(name) != TARGET and
                record['proposal_source'] in source and
                record['sha256'] == source[record['proposal_source']]['sha256'], 'target_binding')
    helper = load(PACKET / 'final-helper-pins.PROPOSED.json')
    require(len(helper) == 37 and helper == load(PACKET / 'source/paired/HELPER-PINS.json') and
            helper == load(PACKET / 'source/preparation-inputs/final-helper-pins.PROPOSED.json'),
            'helper_map_copies')
    for name, digest in helper.items():
        require(Path(name).parent == TARGET / 'bootstrap' and
                targets[name]['sha256'] == digest, 'helper_target_binding')
    template = load(PACKET / 'input-template.PROPOSED.json')
    require(template['bootstrap_pins'] == helper and template['nonce'] is None and
            template['private_secret_sha256'] is None, 'template_original_map')
    paired_targets = load(PACKET / 'PAIRED-TARGET-SOURCE-MAP.json')
    require(type(paired_targets) is dict and len(paired_targets) == 10 and
            all(targets[k] == v for k, v in paired_targets.items()), 'paired_target_subset')
    # Current candidate schema pins all ten paired source units directly via SOURCE-PINS,
    # rather than the predecessor's separate PAIRED-PINS wrapper.
    for record in paired_targets.values():
        require(record['proposal_source'] in source and
                source[record['proposal_source']]['sha256'] == record['sha256'], 'paired_source_binding')
    overlays = load(PACKET / 'CURRENT-EIGHT-OVERLAYS.json')
    require(set(overlays) == {'base_commit', 'overlays'} and
            overlays['base_commit'] == 'e43ad4b20dcc2ca6baa90e31ff38146f1d50c7ea' and
            len(overlays['overlays']) == 8, 'overlay_schema')
    for record in overlays['overlays'].values():
        require(set(record) == {'source', 'sha256'}, 'overlay_record')
        require(read(record['source'], materialize=False)[1] == record['sha256'], 'overlay_hash')
    audit = load(PACKET / 'EMBEDDED-PRODUCER-SHA-AUDIT.json')
    require(set(audit) == {'sha_literals', 'local_dictionary_pairs',
            'verified_external_dictionary_inputs', 'unresolved'} and
            len(audit['sha_literals']) == 56 and audit['local_dictionary_pairs'] == 7 and
            audit['unresolved'] == [], 'embedded_audit_state')
    actual_literals = []
    for name in source:
        path = Path(name)
        if path.suffix == '.py':
            raw = read(path)[0]
            for node in ast.walk(ast.parse(raw)):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and HEX.fullmatch(node.value):
                    actual_literals.append((str(path.relative_to(PACKET)), node.lineno, node.value))
    declared = []
    for entry in audit['sha_literals']:
        require(set(entry) == {'consumer', 'line', 'sha256', 'local_producers'}, 'embedded_entry_schema')
        declared.append((entry['consumer'], entry['line'], entry['sha256']))
        producers = entry['local_producers']
        require(type(producers) is list and
                sorted(producers) == sorted(name for name in (str(Path(path).relative_to(PACKET)) for path in source)
                    if source[str(PACKET / name)]['sha256'] == entry['sha256']), 'embedded_producer_inventory')
        for producer in producers:
            src = str(PACKET / producer)
            require(src in source and source[src]['sha256'] == entry['sha256'], 'embedded_producer_hash')
    require(sorted(actual_literals) == sorted(declared), 'embedded_literal_complete_inventory')
    for name, digest in audit['verified_external_dictionary_inputs'].items():
        require(HEX.fullmatch(digest) and read(name, materialize=False)[1] == digest, 'embedded_external_hash')
    path_audit = load(PACKET / 'PATH-CLOSURE.COMPLETE.json')
    require(path_audit['old_n4_components'] == [] and path_audit['active_parent_namespace'] == 'n12' and
            path_audit['R314_audit_claim_rejected'] is True and
            len(path_audit['derived_bindings']) == 60, 'component_path_audit')
    # Detect exact namespace components in actual Python/JSON/non-Python source;
    # relative ROOT / '.agent-evidence/...' constants must not escape this check.
    old_namespace = re.compile(r'(?:^|/)\.agent-evidence/986/c6/n4(?:-runtime)?(?:/|$)')
    def inspect_strings(value):
        if isinstance(value, str):
            require(not old_namespace.search(value), 'stale_active_namespace')
        elif isinstance(value, dict):
            for key, child in value.items():
                inspect_strings(key); inspect_strings(child)
        elif isinstance(value, list):
            for child in value: inspect_strings(child)
    for name in source:
        path = Path(name); raw = read(path)[0]
        if path.suffix == '.py':
            for node in ast.walk(ast.parse(raw)):
                if isinstance(node, ast.Constant): inspect_strings(node.value)
        elif path.suffix == '.json': inspect_strings(json.loads(raw))
        else:
            for line in raw.decode().splitlines(): inspect_strings(line)
    # Immutable cache/tool bytes are checked without executing them or importing site.
    for category in ('cache_pins', 'browser_pins', 'executable_pins', 'startup_pins'):
        require(type(template[category]) is dict, 'cache_category_schema')
        for name, expected in template[category].items():
            require(type(expected) is str and HEX.fullmatch(expected), 'cache_pin_schema')
            path = Path(name)
            # Original declared cache paths can be readonly symlink aliases; digest
            # the canonical current target, never change the expected hash or alias.
            resolved = path.resolve(strict=True)
            before_link = path.lstat()
            require(read(resolved, materialize=False, cache=True)[1] == expected, 'cache_tool_hash')
            require(unchanged(before_link, path.lstat()) and path.resolve(strict=True) == resolved,
                    'cache_alias_drift')
    old = ROOT / '.agent-evidence/986/c6/native-startup-coherent-epoch-r253/preparation-inputs'
    snapshot_path = ROOT / '.agent-evidence/986/c6/native-cache-baseline-root-capture-r341/readonly-cache-metadata.json'
    require(read(snapshot_path, materialize=False)[1] == 'd43efc2e51c62608cfc3f21be5149074d6ee01e73d0273bec35b6d985c8fd996', 'adopted_cache_snapshot_sha')
    snapshot = load(snapshot_path)
    require(len(snapshot) == 27839, 'original_cache_metadata_count')
    extra_path = OUT.parent / 'ORIGINAL-THREE-METADATA.json'
    require(read(extra_path, materialize=False)[1] == '81c18b655055ee2ccd078f6acc9d8673957788a690d9e47f2f718a11834243b2',
            'original_extra_metadata_sha')
    extra = load(extra_path)
    require(len(extra) == 3 and not (set(snapshot) & set(extra)) and
            sum(record['uid'] == 0 for record in extra.values()) == 2 and
            sum(record['uid'] == 1000 for record in extra.values()) == 1,
            'original_three_metadata_frontier')
    snapshot.update(extra)
    require(len(snapshot) == 27842 and
            sum(record['uid'] == 0 for record in snapshot.values()) == 2 and
            all(record['uid'] in (0, 1000) for record in snapshot.values()),
            'original_complete_metadata_frontier')
    for name, expected in snapshot.items():
        require(set(expected) == {'device', 'inode', 'mode', 'nlink', 'sha256', 'uid'},
                'original_cache_metadata_schema')
        _, digest, info = read(name, materialize=False, cache=True)
        require(expected == {'device': info.st_dev, 'inode': info.st_ino,
                'mode': info.st_mode, 'nlink': info.st_nlink, 'sha256': digest,
                'uid': info.st_uid}, 'original_cache_metadata_changed')
    venv = ROOT / '.agent-worktrees/986-a1-descriptor-projection/.agent-evidence/986/accepted-cache-r174/.venv'
    vmap = load(old / 'venv-map.json')
    require(len(vmap) == 15122 and {str(p) for p in venv.rglob('*')
            if p.is_file() or p.is_symlink()} == set(vmap), 'private_cache_exact_frontier')
    aliases = {'bin/python': '/home/pawel/.local/share/uv/python/cpython-3.12.8-linux-x86_64-gnu/bin/python3.12',
               'bin/python3': 'python', 'bin/python3.12': 'python', 'lib64': 'lib'}
    regular_count = 0
    alias_count = 0
    for name, record in vmap.items():
        path = Path(name)
        relative = str(path.relative_to(venv))
        if 'sha256' in record:
            require(set(record) == {'mode', 'sha256'}, 'private_cache_regular_schema')
            _, digest, info = read(path, materialize=False, cache=True)
            require(digest == record['sha256'] and stat.S_IMODE(info.st_mode) == record['mode'] and
                    info.st_uid == 1000 and info.st_nlink == 1, 'private_cache_regular_identity')
            regular_count += 1
        else:
            require(set(record) == {'resolved', 'resolved_sha256', 'symlink'} and
                    relative in aliases and stat.S_ISLNK(path.lstat().st_mode) and
                    record['symlink'] == aliases[relative] and os.readlink(path) == aliases[relative],
                    'four_original_private_aliases')
            resolved = path.resolve(strict=True)
            require(str(resolved) == record['resolved'], 'private_alias_resolved_identity')
            if relative == 'lib64':
                require(record['resolved_sha256'] is None and resolved == venv / 'lib' and
                        stat.S_ISDIR(resolved.lstat().st_mode), 'private_directory_alias')
            else:
                require(read(resolved, materialize=False, cache=True)[1] == record['resolved_sha256'],
                        'private_executable_alias')
            alias_count += 1
    require(regular_count == 15118 and alias_count == 4, 'private_cache_typed_counts')
    host = load(ROOT / '.agent-evidence/986/c6/ROOT-native-short-root-ACTUAL-HOST-tool-readback-r282.json')
    require(host['kind'] == 'actual_unsandboxed_readonly_host_tool_metadata' and
            host['runtime_started'] is False and len(host['tools']) == 2, 'host_tool_schema')
    for name, expected in host['tools'].items():
        _, digest, info = read(name, materialize=False, cache=True)
        actual = {'uid': info.st_uid, 'gid': info.st_gid, 'mode': info.st_mode,
                  'nlink': info.st_nlink, 'size': info.st_size, 'sha256': digest}
        require(actual == expected, 'exact_actual_host_tool_identity')
    require(load(PACKET / 'ROOT-ACTUAL-INPUTS.PROPOSED.json') ==
            dict(manifest_sha256=None, binding_sha256=None, census_sha256=None,
                 generated_readback_sha256=None), 'future_actual_ledger_schema')
    for path in (TARGET, RUNTIME, LEDGER, OUT / 'ROOT-stage-receipt.json'):
        absent(path)
    no_alias(TARGET.parent)
    require(TARGET.parent.lstat().st_uid == 1000, 'target_parent_owner')
    return targets, source, len(pins['files']), len(pins['inputs'])


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def emit(path, raw):
    absent(path)
    with held_directory(path.parent) as parent_fd:
        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=parent_fd)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'wb', closefd=False) as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(fd)
            written = os.fstat(fd)
            os.fsync(parent_fd)
            expected = {'sha256': hashlib.sha256(raw).hexdigest(), 'size': len(raw)}
            _, digest, info = read(path, expected, materialize=False)
            require(unchanged(written, info) and unchanged(written, os.fstat(fd)) and
                    digest == expected['sha256'] and stat.S_ISREG(info.st_mode) and
                    info.st_uid == os.getuid() and info.st_nlink == 1 and
                    stat.S_IMODE(info.st_mode) == 0o600, 'emitted_private_readback')
            os.fsync(parent_fd)
        finally:
            os.close(fd)


def stage(targets, source):
    # All validation above is complete before the first mkdir/write. A partial
    # failure stays preserved at its actual location; never roll back/delete.
    output_chain(TARGET.parent)
    output_chain(OUT, leaf_private=True)
    absent(OUT / 'ROOT-stage-receipt.json')
    with held_directory(TARGET.parent), held_directory(OUT):
        output_chain(TARGET.parent)
        output_chain(OUT, leaf_private=True)
        with held_directory(TARGET.parent) as target_parent_fd:
            os.mkdir(TARGET.name, mode=0o700, dir_fd=target_parent_fd)
            os.fsync(target_parent_fd)
            target_fd = os.open(TARGET.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                dir_fd=target_parent_fd)
            try:
                target_info = os.fstat(target_fd)
                require(stat.S_ISDIR(target_info.st_mode) and target_info.st_uid == 1000 and
                        stat.S_IMODE(target_info.st_mode) == 0o700 and
                        unchanged(target_info, TARGET.lstat()), 'exact_private_target_root')
            finally:
                os.close(target_fd)
        for name, record in sorted(targets.items()):
            path = Path(name)
            relative = path.parent.relative_to(TARGET)
            parent = TARGET
            for part in relative.parts:
                parent = parent / part
                if not os.path.lexists(parent):
                    with held_directory(parent.parent) as parent_fd:
                        os.mkdir(parent.name, mode=0o700, dir_fd=parent_fd)
                        os.fsync(parent_fd)
                info = parent.lstat()
                require(parent.resolve(strict=True) == parent and stat.S_ISDIR(info.st_mode) and
                        info.st_uid == 1000 and stat.S_IMODE(info.st_mode) == 0o700, 'private_stage_parent')
            raw = read(record['proposal_source'], source[record['proposal_source']])[0]
            emit(path, raw)
            _, digest, info = read(path, source[record['proposal_source']], materialize=False)
            require(digest == record['sha256'] and info.st_uid == 1000 and info.st_nlink == 1 and
                    stat.S_IMODE(info.st_mode) == 0o600, 'staged_readback')
        receipt = {'state': 'SOURCE_ONLY_STAGED', 'targets': targets, 'staged_files': 55,
                   'helper_count': 37, 'native_observations': None, 'runtime_started': False,
                   'n4_assets_unchanged': True, 'actual_ledger_created': False}
        emit(OUT / 'ROOT-stage-receipt.json', (json.dumps(receipt, sort_keys=True, indent=2) + '\n').encode())


if __name__ == '__main__':
    require(sys.argv[1:] in (['preflight'], ['stage']), 'explicit_root_action_required')
    try:
        targets, source, packet_files, external_inputs = preflight()
        if sys.argv[1] == 'stage':
            stage(targets, source)
        print(json.dumps({'state': 'STAGED' if sys.argv[1] == 'stage' else 'PREFLIGHT_ONLY',
                          'packet_files': packet_files, 'external_inputs': external_inputs, 'source_pins': len(source),
                          'targets': len(targets), 'runtime_started': False}))
    except Exception:
        print(json.dumps({'state': 'DENY', 'runtime_started': False,
                          'partial_stage_preserved_if_created': True}))
        raise SystemExit(1)
