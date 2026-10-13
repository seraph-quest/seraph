'Actual managed HTTP/SQLite journey; private artifacts never confer authority.'
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from datetime import datetime, timezone

from operator_http import OperatorHTTP


class Journey:
    def __init__(self, context, manager):
        self.context, self.manager = context, manager
        self.directory = Path(context['journey_directory'])
        self.http = OperatorHTTP(backend_port=18241, frontend_port=13241,
                                private_secret_file=context['local_secret_file'])
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.future = None
        self.native_pidfds = []
        self.held_receipt = None

    def wait(self, reader, predicate, *, seconds=5):
        cutoff = time.monotonic() + seconds
        while time.monotonic() < cutoff:
            result = reader()
            if predicate(result):
                return result
            time.sleep(0.02)
        raise AssertionError('bounded_original_observation_unproved')

    def private_read(self, name):
        path = self.directory / name
        return json.loads(path.read_text()) if path.exists() else None

    def admit_original_via_authenticated_api(self, *, state='running', cutpoint=None):
        case = dict(self.context['reviewed_case'])
        if cutpoint is not None and case['cutpoint'] != cutpoint:
            raise AssertionError('prepared_original_epoch_selector_mismatch')
        if case['expected_original_state'] != state:
            raise AssertionError('original_state_must_come_from_reviewed_real_case')
        self.manager.run()
        # Controller separately validates/ACKs every startup receipt before
        # this authenticated contact; fixture cannot bypass that handshake.
        self.context_owner_ack_startup()
        self.http.login()
        self.http.request('PUT', '/api/settings/repo-sandbox', case['sandbox_settings'])
        from policy_setup import configure_original_policy
        self.policy_canary_observations = configure_original_policy(self.http, case['model_fabric_settings'])
        goal = self.http.create_reviewed_goal(case['goal_request'])
        mutation = self.http.create_original_repository_task(goal_ref=goal['id'], goal_revision=goal['revision'],
            original_repository_input=case['repository_input'], original_task_limits=case['task_limits'],
            idempotency_key=case['idempotency_key'])
        task_id = mutation['task']['task_id']
        detail = self.wait(lambda: self.http.task(task_id), lambda d: bool(d['task'].get('repository_review')))
        root_id = detail['task']['repository_review']['repository_job_id']
        fd = os.open(self.directory / 'original-root.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump({'job_id': root_id}, stream); stream.flush(); os.fsync(stream.fileno())
        preview = self.http.repo(root_id, preview=True)
        original = {'job_id': root_id, 'task_id': task_id, 'session': self.http.original_session,
                    'state': state, 'task_title': detail['task']['title']}
        # Original consent synchronously awaits its real C1 model callback.
        # Run that HTTP request concurrently so the controller can inspect the
        # original precontact epoch before the scripted transport is entered.
        consent = self.pool.submit(self.http.acknowledge_reviewed_iteration,
            root_id, preview, idempotency_key=case['idempotency_key'] + ':consent')
        self.wait(lambda: self.private_read('precontact-ready.json'), lambda d: d is not None)
        self.assert_exact_source_contact_epoch(original)
        self.release('contact-reviewed.json')
        consent.result(timeout=5)
        current = self.http.repo(root_id)
        proposal, approval = current['patch_proposal'], current['approval']
        self.http.request('POST', '/api/approvals/' + self.http.identifier(approval['approval_id']) + '/approve')
        payload = {'approval_id': approval['approval_id'], 'proposal_id': proposal['proposal_id'],
            'expected_proposal_revision': proposal['revision'], 'expected_job_revision': current['revision'],
            'idempotency_key': case['idempotency_key'] + ':execute'}
        route = '/api/workflows/repo-repair/' + root_id + '/resume'
        self.future = self.pool.submit(self.http.request, 'POST', route, payload)
        return original

    def assert_exact_source_contact_epoch(self, original):
        packet = self.private_read('source-contact-epoch.json')
        assert packet['root_id'] == original['job_id']
        expected_tables = ['workflow_run_states'] * 3 + ['work_board_tasks', 'work_board_attempts',
            'work_board_input_artifacts', 'work_board_tasks', 'work_board_attempts', 'goals',
            'operator_sessions', 'work_board_input_artifacts']
        assert [row['table'] for row in packet['rows']] == expected_tables
        identities = packet['row_ids']
        assert len(identities) == 11 and [row['table'] for row in identities] == expected_tables
        assert len({(item['table'], tuple(item['key'])) for item in identities}) == 11
        from row_codec import read
        assert read(self.context, self.manager.db, identities) == packet['rows']

    def context_owner_ack_startup(self):
        # Supplied by the concrete controller subclass below, not a public API.
        from startup_controller import acknowledge_startups, wait_owned_api
        acknowledge_startups(self.context)
        wait_owned_api(self.context)
        self.manager.capture_original_managed_roles()

    def read_original_authority_and_accounting(self, original, *, include_api=True):
        api = self.http.repo(original['job_id']) if include_api else None
        identifiers = self.wait(lambda: self.private_read('current-eleven.json'), lambda d: d is not None)
        assert identifiers['root_id'] == original['job_id'] and len(identifiers['row_ids']) == 11
        db = sqlite3.connect('file:' + str(self.manager.db) + '?mode=ro', uri=True)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN')
            rows = []
            for identity in identifiers['row_ids']:
                table, columns = identity['table'], identity['columns']
                if not table.replace('_', '').isalnum() or any(not c.replace('_', '').isalnum() for c in columns):
                    raise AssertionError('original_sql_identity_invalid')
                query = 'SELECT * FROM "' + table + '" WHERE ' + ' AND '.join('"' + c + '"=?' for c in columns)
                matches = db.execute(query, identity['key']).fetchall()
                assert len(matches) == 1
                rows.append({'table': table, 'row': dict(matches[0])})
            # Literal all accounting/ledger rows, no reconstruction from API.
            accounting = {}
            for table in self.context['reviewed_accounting_tables']:
                if not table.replace('_', '').isalnum():
                    raise AssertionError('accounting_table_invalid')
                accounting[table] = [dict(row) for row in db.execute('SELECT * FROM "' + table + '" ORDER BY rowid')]
            return {'rows': rows, 'accounting': accounting, 'api': api}
        finally:
            db.close()

    def refresh_original_session_via_http(self, original):
        refreshed = self.http.refresh_original_session()
        assert refreshed['session_id'] == original['session']['session_id']
        return refreshed

    def rollback_original_completion_transaction(self, original):
        packet = self.wait(lambda: self.private_read('sql-rollback.json'), lambda d: d is not None)
        assert packet['root_id'] == original['job_id'] and packet['before'] == packet['after']
        self.release('retry-original.json')
        retry = self.wait(lambda: self.private_read('sql-retry.json'), lambda d: d is not None)
        assert retry['root_id'] == original['job_id'] and retry['same_original_result'] is True

    def await_actual_unknown(self, original):
        marker = self.wait(lambda: self.private_read('unknown_first_stage.json'), lambda item: item is not None)
        assert marker['root_id'] == original['job_id'] and marker['nonce'] == self.context['nonce']
        assert marker['cleanup_appended'] is False and marker['readback_appended'] is False
        value = self.wait(lambda: self.http.repo(original['job_id']),
                          lambda item: item['status'] == 'unknown_external_effect')
        assert value['no_learning'] is True
        # Actual Source quarantine must precede FIRST immutable cleanup staging.
        assert self.private_read('sql-rollback.json') is None
        assert self.private_read('append-committed.json') is None
        return value

    def phase_refresh_retry_restart(self, original):
        from assertions import assert_auth_refresh, assert_cleanup_commit, assert_exact
        rollback = self.wait(lambda: self.private_read('sql-rollback.json'), lambda d: d is not None)
        assert rollback['root_id'] == original['job_id'] and rollback['before'] == rollback['after']
        pre_refresh = self.read_original_authority_and_accounting(original)
        self.refresh_original_session_via_http(original)
        refreshed = self.read_original_authority_and_accounting(original)
        assert_auth_refresh(pre_refresh, refreshed)
        self.release('retry-original.json')
        self.wait(lambda: self.private_read('sql-retry.json'), lambda d: d is not None)
        # Observer worker holds after real retry commit, before terminal CAS.
        self.wait(lambda: self.private_read('append-committed.json'), lambda d: d is not None)
        committed = self.read_original_authority_and_accounting(original)
        assert_cleanup_commit(refreshed, committed)
        if original.get('state') == 'unknown_external_effect':
            self.release('continue-original.json')
            self.future.result(timeout=5)
            current = self.read_original_authority_and_accounting(original)
            assert_exact(committed, current)
            assert current['api']['status'] == 'unknown_external_effect'
            self.assert_physical_eof_echild_and_auth_api_ui(original)
            return
        self.kill_owned_backend_and_restart_same_database(original)
        restarted = self.read_original_authority_and_accounting(original)
        self.assert_restart_preserves_original_committed_receipts(committed, restarted)
        self.assert_physical_eof_echild_and_auth_api_ui(original)

    def assert_retired_bearer_denied(self, original):
        retired = self.http.clone()
        self.refresh_original_session_via_http(original)
        retired.request('GET', '/api/auth/session', expected=401)
        current = self.http.current_session()
        assert current['session_id'] == original['session']['session_id']

    def assert_retired_owner_denied(self, original, kind):
        current = self.http.repo(original['job_id'])
        route = '/api/_managed-test/original-reconcile'
        body = {'job_id': original['job_id'], 'expected_job_revision': current['revision']}
        if kind == 'expired':
            expiry = datetime.fromisoformat(self.http.current_session()['idle_expires_at']).replace(tzinfo=timezone.utc)
            seconds = (expiry - datetime.now(timezone.utc)).total_seconds()
            if not 0 <= seconds <= 35:
                raise AssertionError('reviewed_bounded_real_auth_expiry_not_prepared')
            # No calls/timestamp edits during real expiry; original middleware
            # performs its ordinary durable retirement on the next request.
            time.sleep(seconds + 0.05)
            self.http.request('POST', route, body, expected=401)
        elif kind in {'logout', 'identity_revoked'}:
            retired = self.http.clone()
            endpoint = '/api/auth/logout' if kind == 'logout' else '/api/auth/ownership/revoke'
            self.http.request('POST', endpoint, expected=204)
            retired.request('POST', route, body, expected=401)
        elif kind == 'new_login':
            fresh = self.http.clone(empty=True)
            session = fresh.login(start_new_scope=True)
            assert session['session_id'] != original['session']['session_id']
            fresh.request('POST', route, body, expected=409)
        else:
            raise AssertionError('unsupported_auth_negative')

    def release(self, name):
        fd = os.open(self.directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump({'nonce': self.context['nonce']}, stream); stream.flush(); os.fsync(stream.fileno())

    def kill_owned_backend_and_restart_same_database(self, original, *, reconcile=True):
        from startup_controller import backend_identity, listener_readback
        pid, start, cmdline = backend_identity(self.context)
        identity = self.manager.kill_and_restart(pid=pid, start=start, cmdline=cmdline,
            listener_readback=lambda p, s: listener_readback(self.context, p, s))
        self.context_owner_ack_startup()
        new_pid, new_start, _argv = backend_identity(self.context)
        assert (new_pid, new_start) != (pid, start)
        assert self.manager.read_runtime_database(new_pid, new_start) == identity
        if reconcile:
            self.reconcile_original_via_current_auth(original)

    def reconcile_original_via_current_auth(self, original):
        current = self.http.repo(original['job_id'])
        return self.http.request('POST', '/api/_managed-test/original-reconcile',
                          {'job_id': original['job_id'], 'expected_job_revision': current['revision']})

    def reconcile_when_original_closed(self, original):
        def attempt():
            current = self.http.repo(original['job_id'])
            value = self.http.request('POST', '/api/_managed-test/original-reconcile',
                {'job_id': original['job_id'], 'expected_job_revision': current['revision']}, expected=(200, 409))
            if 'detail' not in value:
                return value
            detail = value['detail']
            if (detail.get('code') != 'repository_source_recovery_blocked'
                    or detail.get('original_reason') != 'pending_original_producer'):
                expected = self.context['reviewed_case'].get('expected_original_denial')
                if expected is not None and detail == expected:
                    from held_outcomes import preserve
                    self.held_receipt = preserve(self.context, self.manager,
                        root_id=original['job_id'], actual_denial=value, native_pidfds=self.native_pidfds)
                    return {'managed_actual_held_outcome': self.held_receipt}
                raise AssertionError('original_reconcile_nonpending_denial')
            return None
        return self.wait(attempt, lambda value: value is not None, seconds=15)

    def start_original_unknown_reconcile(self, original):
        self.await_actual_unknown(original)
        try:
            self.future.result(timeout=5)
        except AssertionError as error:
            if not str(error).startswith('managed HTTP status '):
                raise
        else:
            raise AssertionError('original_unknown_delivery_request_did_not_fail')
        self.kill_owned_backend_and_restart_same_database(original, reconcile=False)
        self.future = self.pool.submit(self.reconcile_original_via_current_auth, original)

    def assert_restart_preserves_original_committed_receipts(self, committed, restarted):
        from assertions import journal
        before, after = committed['rows'][0]['row'], restarted['rows'][0]['row']
        original = journal(before)
        current = journal(after)
        assert current[:len(original)] == original
        for entry in original:
            if entry['checkpoint_id'].startswith(('repository:cleanup:', 'repository:readback:')):
                assert sum(row['checkpoint_id'] == entry['checkpoint_id'] for row in current) == 1
        for index, (old, new) in enumerate(zip(committed['rows'], restarted['rows'])):
            assert old['table'] == new['table']
            for field in ('deadline_at', 'absolute_expires_at', 'created_at', 'input_digest', 'fencing_token',
                          'owner_principal_id', 'operator_session_id', 'goal_id', 'goal_revision'):
                if field in old['row']:
                    assert old['row'][field] == new['row'][field]
        assert restarted['api']['no_learning'] is True
        assert restarted['api']['source_recovery']['public_actions'] == 'unavailable'
        # Accounting conservation/final Task/Attempt transitions require the
        # independently reviewed original terminal constructor expectation.
        # No inference/accounting writer belongs to original final adoption.
        assert restarted['accounting'] == committed['accounting']
        packet = self.wait(lambda: self.private_read('terminal-constructors.json'), lambda d: d is not None)
        assert packet['origin'] == 'original_finite_constructor_before_commit'
        assert packet['original_postcommit_readback'] is True and packet['kind'] == 'terminal'
        from row_codec import read
        expected = [{'table': entry['identity']['table'], 'raw': entry['raw']} for entry in packet['expected']]
        assert read(self.context, self.manager.db, [entry['identity'] for entry in packet['expected']]) == expected
        assert len({(entry['identity']['table'], tuple(entry['identity']['key'])) for entry in packet['expected']}) == len(expected)
        staged = self.private_read('current-eleven.json')
        by_key = {(entry['identity']['table'], tuple(entry['identity']['key'])): entry['raw'] for entry in packet['expected']}
        assert all((row['table'], tuple(row['key'])) in by_key for row in staged['row_ids'])

    def assert_same_receipt_cutoffs_commitment_and_current_auth(self, before, after):
        # Raw rows are complete. Expected deltas must be individually explained
        # by actual original publication/refresh, never a blanket field mask.
        from assertions import assert_original_rows
        assert_original_rows(before, after)

    def assert_preimport_and_inherited_denial_before_admission(self):
        self.manager.run()
        self.context_owner_ack_startup()
        # Every generation must produce actual inherited isolated exec evidence.
        from startup_controller import backend_identity
        pid, _start, _argv = backend_identity(self.context)
        proof = self.wait(lambda: self.private_read('inherited-denial-' + str(pid) + '.json'), lambda d: d is not None)
        assert proof['NoNewPrivs'] == 1 and proof['Seccomp'] == 2
        assert {'raw_socket', 'raw_connect', 'addressed_sendto', 'io_uring_setup',
                'io_uring_enter', 'io_uring_register'} <= set(proof['checks'])

    def wait_for_original_observer(self, cutpoint, original):
        if cutpoint in {'intent_before_popen', 'registration_commit_before_ack', 'cleanup_artifact_fsync_before_append'}:
            return self.wait(lambda: self.private_read(cutpoint + '.json'), lambda d: d is not None)
        if cutpoint == 'append_commit_before_terminal_cas':
            return self.wait(lambda: self.private_read('append-committed.json'), lambda d: d is not None)
        if cutpoint == 'cleanup_readback_sql_rollback':
            return self.wait(lambda: self.private_read('sql-rollback.json'), lambda d: d is not None)
        from assertions import journal
        from native_markers import selected
        def diagnostic():
            epoch = self.read_original_authority_and_accounting(original)
            registrations = [entry['payload'] for entry in journal(epoch['rows'][0]['row'])
                             if entry['checkpoint_id'].startswith('repository:producer:')]
            if not registrations:
                return None
            assert len(registrations) == 1
            value = selected(self.context, registrations[0])
            return dict(value, root_id=original['job_id']) if value else None
        return self.wait(diagnostic, lambda value: value is not None)

    def assert_actual_syscall_and_original_owner(self, observation, original):
        assert observation['root_id'] == original['job_id']
        assert self.private_read('current-eleven.json')['root_id'] == original['job_id']
        # Original registered producer PID/start/protocol identity must be
        # read by the canonical helper; diagnostic marker is not that authority.
        current = self.http.repo(original['job_id'])
        assert current['job_id'] == original['job_id'] and current['no_learning'] is True
        from assertions import journal
        snapshot = self.read_original_authority_and_accounting(original)
        registrations = [entry['payload'] for entry in journal(snapshot['rows'][0]['row'])
                         if entry['checkpoint_id'].startswith('repository:producer:')]
        if registrations and not self.native_pidfds:
            assert len(registrations) == 1
            ready = registrations[0]['ready']
            try:
                fd = os.pidfd_open(ready['pid'])
            except ProcessLookupError:
                # Post-result Source cuts may occur after genuine child reap.
                # Absence is not completion; the original staged accessor must
                # still validate signed material and current guard/PID absence.
                return
            try:
                proc = Path('/proc') / str(ready['pid'])
                ticks = (proc / 'stat').read_text().rsplit(')', 1)[1].split()[19]
                assert ticks == ready['start_identity'] and proc.stat().st_uid == os.getuid()
            except BaseException:
                os.close(fd)
                raise
            self.native_pidfds.append({'pid': ready['pid'], 'start': ready['start_identity'], 'pidfd': fd})

    def assert_no_replay_duplicate_append_charge_or_deadline_renewal(self, before, after):
        assert before['accounting'] == after['accounting']
        for old, new in zip(before['rows'], after['rows']):
            for key in ['deadline_at', 'absolute_expires_at', 'created_at', 'input_digest']:
                if key in old['row']:
                    assert old['row'][key] == new['row'][key]
        # Full raw changed rows remain in caller's private receipt for exact
        # independent current-Source journal/accounting readback review.

    def assert_physical_eof_echild_and_auth_api_ui(self, original):
        current = self.http.repo(original['job_id'])
        assert current['source_recovery']['public_actions'] == 'unavailable'
        physical = self.private_read('original-physical-closure.json')
        if physical is None:
            raise AssertionError('actual_original_physical_EOF_ECHILD_readback_unavailable')
        from assertions import assert_closure
        assert_closure(physical)
        self.assert_authenticated_api_and_inspector(original)

    def assert_authenticated_api_and_inspector(self, original):
        current = self.http.repo(original['job_id'])
        assert current['no_learning'] is True
        packet = current['source_recovery']
        if packet is not None:
            assert packet['public_actions'] in {'unavailable', 'reconcile_original_cleanup'}
        # Required cutpoint inspection never implicitly retries a public action.
        # Current committed-Unknown public denial is explicit in its own case.
        from inspector import assert_actual_inspector
        assert_actual_inspector(self.context, self.http, original, current)

    def close(self):
        from controller_owned import finish
        if self.held_receipt is not None:
            finish(self.manager, self.context, held_receipt=self.held_receipt)
            self.pool.shutdown(wait=False, cancel_futures=True)
            return
        # Native ownership is verified by canonical staged closure; teardown
        # never signals a native PID to manufacture it.
        physical = self.private_read('original-physical-closure.json')
        if self.future is not None:
            if physical is None and not getattr(self, 'no_spawn_held', None):
                raise AssertionError('native_closure_required_before_fixture_disposal')
            if physical is not None:
                from assertions import assert_closure
                assert_closure(physical)
        if physical is not None:
            finish(self.manager, self.context, original_checked_closure=physical)
        else:
            self.manager.down_archive_remove(self.context)  # Proven no-spawn case only.
        for entry in self.native_pidfds:
            os.close(entry['pidfd'])
        self.pool.shutdown(wait=True, cancel_futures=True)

    def run_source_cutpoint_case(self, original, cutpoint):
        from assertions import assert_exact, assert_cleanup_commit, assert_closure
        observation = self.wait_for_original_observer(cutpoint, original)
        self.assert_actual_syscall_and_original_owner(observation, original)
        before = self.read_original_authority_and_accounting(original)
        self.kill_owned_backend_and_restart_same_database(original, reconcile=False)
        if cutpoint == 'intent_before_popen':
            current = self.http.repo(original['job_id'])
            result = self.http.request('POST', '/api/_managed-test/original-reconcile',
                {'job_id': original['job_id'], 'expected_job_revision': current['revision']}, expected=409)
            assert result['detail']['code'] == 'original_producer_registration_missing'
            after = self.read_original_authority_and_accounting(original)
            assert_exact(before, after)
            assert after['api']['source_recovery'] is None
            from assertions import journal
            receipts = journal(after['rows'][0]['row'])
            assert not any(entry['checkpoint_id'].startswith('repository:producer:') for entry in receipts)
            reservations = [entry for entry in receipts if entry['checkpoint_id'] in
                            {'repo-repair-execution-reservation', 'repo-repair-execution-release'}]
            assert reservations and reservations[-1]['payload']['status'] == 'held'
            assert observation['point'] == 'intent_before_popen'
            assert observation['backend_pid'] == self.manager.last_killed_pid
            self.no_spawn_held = {'root_id': original['job_id'], 'point': cutpoint,
                                 'backend_pid': self.manager.last_killed_pid}
            self.assert_authenticated_api_and_inspector(original)
            return
        if cutpoint == 'registration_commit_before_ack':
            self.reconcile_when_original_closed(original)
            after = self.read_original_authority_and_accounting(original)
            assert_cleanup_commit(before, after)
            assert after['api']['source_recovery']['state'] == 'held_partial'
            assert after['api']['source_recovery']['physical_hold'] is True
            physical = self.private_read('original-physical-closure.json')
            assert_closure(physical)
            assert physical['commands'] == []
            self.assert_authenticated_api_and_inspector(original)
            return
        reconciled = self.reconcile_when_original_closed(original)
        if 'managed_actual_held_outcome' in reconciled:
            assert self.held_receipt['completion_proven'] is False
            return
        after = self.read_original_authority_and_accounting(original)
        projection = after['api']['source_recovery']
        if projection['state'] == 'held_partial':
            assert_cleanup_commit(before, after)
            assert projection['physical_hold'] is True
            assert_closure(self.private_read('original-physical-closure.json'))
            self.assert_authenticated_api_and_inspector(original)
            return
        self.assert_restart_preserves_original_committed_receipts(before, after)
        self.assert_physical_eof_echild_and_auth_api_ui(original)

    def run_selected_case(self, name):
        'Closed16 dispatch; unsupported genuine positive fixture denies before admission.'
        from case_matrix import selected
        case = selected(name)
        if name in {'public-unknown-http', 'public-unknown-inspector'}:
            return self.run_public_unknown_case(name)
        original = self.admit_original_via_authenticated_api(
            state=case['expected_original_state'], cutpoint=case['cutpoint'])
        if name.startswith('auth-refresh-'):
            if case['expected_original_state'] == 'unknown_external_effect':
                self.await_actual_unknown(original)
                self.start_original_unknown_reconcile(original)
            self.phase_refresh_retry_restart(original)
        else:
            self.run_source_cutpoint_case(original, case['cutpoint'])

    def run_public_unknown_case(self, name):
        'Actual task cancellation then successful callback/delivery-failure quarantine.'
        from assertions import assert_auth_refresh, assert_cleanup_commit
        from held_outcomes import preserve
        original = self.admit_original_via_authenticated_api(state='unknown_external_effect')
        marker = self.wait(lambda: self.private_read('public_stop_before_uncertainty.json'),
                           lambda value: value is not None)
        assert marker['root_id'] == original['job_id'] and marker['nonce'] == self.context['nonce']
        detail = self.http.task(original['task_id'])
        self.http.request('POST', '/api/tasks/' + self.http.identifier(original['task_id']) + '/actions',
            {'action': 'cancel', 'expected_revision': detail['task']['task_revision']}, expected=200)
        stopping = self.http.repo(original['job_id'])
        assert stopping['repository_stop']['pending'] is True
        assert stopping['repository_stop']['reason'] == 'operator_cancelled'
        self.release('release-public_stop_before_uncertainty.json')
        self.await_actual_unknown(original)
        self.wait(lambda: self.future.done(), lambda value: value is True)
        # Original resume must have delivered its designated failure, not success.
        assert isinstance(self.future.exception(), AssertionError)
        assert str(self.future.exception()).startswith('managed HTTP status ')
        before = self.read_original_authority_and_accounting(original)
        self.refresh_original_session_via_http(original)
        refreshed = self.read_original_authority_and_accounting(original)
        assert_auth_refresh(before, refreshed)
        current = refreshed['api']
        assert current['source_recovery']['public_actions'] == 'reconcile_original_cleanup'
        assert current['repository_stop']['pending'] is True
        if name == 'public-unknown-http':
            pending = self.pool.submit(self.http.reconcile_original_cleanup_unknown, original['job_id'])
        elif name == 'public-unknown-inspector':
            from inspector import assert_actual_inspector
            pending = self.pool.submit(assert_actual_inspector, self.context, self.http, original, current,
                                       reconcile_unknown=True)
        else:
            raise AssertionError('unsupported_public_unknown_case')
        rollback = self.wait(lambda: self.private_read('sql-rollback.json'), lambda value: value is not None)
        assert rollback['root_id'] == original['job_id'] and rollback['before'] == rollback['after']
        self.release('retry-original.json')
        retry = self.wait(lambda: self.private_read('sql-retry.json'), lambda value: value is not None)
        assert retry['same_original_result'] is True and retry['original_scoped_cold_owner'] is True
        self.wait(lambda: self.private_read('append-committed.json'), lambda value: value is not None)
        committed = self.read_original_authority_and_accounting(original, include_api=False)
        assert_cleanup_commit(refreshed, committed)
        self.release('continue-original.json')
        pending.result(timeout=5)
        actual = self.http.repo(original['job_id'])
        assert actual['status'] == 'unknown_external_effect' and actual['no_learning'] is True
        assert actual['source_recovery']['physical_hold'] is True
        assert actual['source_recovery']['state'] == 'original_cleanup_committed'
        assert actual['source_recovery']['public_actions'] == 'unavailable'
        protected = self.read_original_authority_and_accounting(original)
        self.http.assert_public_recovery_held(original['job_id'], revision=actual['revision'])
        after = self.read_original_authority_and_accounting(original)
        assert protected['rows'] == after['rows'] and protected['accounting'] == after['accounting']
        self.held_receipt = preserve(self.context, self.manager, root_id=original['job_id'],
            actual_denial=actual, native_pidfds=self.native_pidfds)
        return self.held_receipt
