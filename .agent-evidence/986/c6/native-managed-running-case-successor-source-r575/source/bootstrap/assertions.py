'Literal phase comparisons; no shared mutation mask or inferred authority.'
import json


def keyed(snapshot):
    assert len(snapshot['rows']) == 11
    return [(item['table'], item['row']) for item in snapshot['rows']]


def journal(row):
    value = json.loads(row['checkpoint_receipts_json'] or '[]')
    assert isinstance(value, list) and len(value) <= 50
    return value


def assert_exact(before, after):
    assert keyed(before) == keyed(after)
    assert before['accounting'] == after['accounting']


def assert_auth_refresh(before, after):
    assert before['accounting'] == after['accounting']
    found = 0
    for (table, old), (new_table, new) in zip(keyed(before), keyed(after)):
        assert table == new_table and set(old) == set(new)
        if table != 'operator_sessions':
            assert old == new
            continue
        found += 1
        expected = dict(old)
        for name in ('token_hash', 'last_seen_at', 'idle_expires_at'):
            expected[name] = new[name]
        assert expected == new and old['token_hash'] != new['token_hash']
        assert new['revoked_at'] is None and new['replaced_by_id'] is None
        assert new['is_bearer_tombstone'] == 0
        assert new['idle_expires_at'] <= new['absolute_expires_at']
    assert found == 1


def assert_cleanup_commit(before, after):
    'Compare genuine commit before terminal CAS, including all raw rows.'
    assert before['accounting'] == after['accounting']
    for index, ((table, old), (new_table, new)) in enumerate(zip(keyed(before), keyed(after))):
        assert table == new_table and set(old) == set(new)
        if index != 0:
            assert old == new
            continue
        prefix, history = journal(old), journal(new)
        assert history[:len(prefix)] == prefix and len(history) == len(prefix) + 2
        identities = [entry['checkpoint_id'] for entry in history[-2:]]
        assert identities[0].startswith('repository:cleanup:')
        assert identities[1] == identities[0].replace('repository:cleanup:', 'repository:readback:', 1)
        assert new['revision'] == old['revision'] + 1
        expected = dict(old)
        for field in ('revision', 'checkpoint_receipts_json'):
            expected[field] = new[field]
        assert expected == new
    assert after['api']['source_recovery']['public_actions'] == 'unavailable'


def assert_original_rows(before, after):
    assert_exact(before, after)


def assert_closure(packet):
    assert packet['diagnostic_origin'] == 'active_original_staged_signed_manifest'
    assert packet['current_owner_stage_checked'] is True
    assert packet['backend_restart_echild_observed'] is False
    cleanup = packet['process_cleanup']
    assert cleanup['cleanup_proven'] is True and cleanup['oracle'] == 'linux_subreaper_waitpid_echild'
    for name in ('command_output_drained', 'command_descriptors_closed', 'original_children_waited', 'no_spawn'):
        assert packet['supervisor_transport'][name] is True
    for command in packet['commands']:
        assert command['stdout_eof'] is True and command['stderr_eof'] is True
        assert command['waited'] is True and command['cleanup']['cleanup_proven'] is True
