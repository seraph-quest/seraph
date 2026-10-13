'Closed managed cases; a return marker never determines physical success.'

SOURCE = {
    'intent_before_popen': ('intent_before_popen', 'registration_missing'),
    'registration_commit_before_ack': ('registration_commit_before_ack', 'partial_or_pending'),
    'cleanup_artifact_fsync_before_append': ('cleanup_artifact_fsync_before_append', 'completed'),
    'cleanup_readback_sql_rollback': ('cleanup_readback_sql_rollback', 'completed'),
    'append_commit_before_terminal_cas': ('append_commit_before_terminal_cas', 'completed'),
}
NATIVE = {
    'output_file_fsync': 'material-fsync:manifest.json',
    'output_directory_fsync': 'material-output-directory-fsync',
    'completion_file_fsync': 'material-fsync:completion.json',
    'material_directory_fsync': 'material-final-directory-fsync',
    'durability_link': 'proof-link',
    'durability_directory_fsync': 'proof-directory-fsync',
    'proof_before_notification': 'proof-directory-fsync',
}


def specifications():
    cases = []
    for name, (_point, physical) in SOURCE.items():
        cases.append({'id': name, 'cutpoint': name, 'native_selector': None,
                      'expected_original_state': 'running', 'physical': physical})
    for name, selector in NATIVE.items():
        cases.append({'id': name, 'cutpoint': name, 'native_selector': selector,
            'expected_original_state': 'running', 'physical': 'original_signed_result_or_retained_hold'})
    for state in ('running', 'unknown_external_effect'):
        cases.append({'id': 'auth-refresh-' + state, 'cutpoint': 'cleanup_readback_sql_rollback',
            'native_selector': None, 'expected_original_state': state,
            'physical': 'held_unknown' if state == 'unknown_external_effect' else 'completed'})
    for mode in ('http', 'inspector'):
        cases.append({'id': 'public-unknown-' + mode, 'cutpoint': 'cleanup_readback_sql_rollback',
            'native_selector': None, 'expected_original_state': 'unknown_external_effect',
            'physical': 'held_unknown'})
    return cases


def selected(name):
    matches = [case for case in specifications() if case['id'] == name]
    if len(matches) != 1:
        raise ValueError('unsupported_closed_managed_case')
    return matches[0]
